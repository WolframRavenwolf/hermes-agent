"""OpenAI Realtime transcription support for Discord voice channels.

This module is deliberately independent from discord.py. The Discord adapter
owns authorization and lifecycle; this module owns PCM conversion, the
Realtime WebSocket protocol, and per-user turn accounting.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import sys
import time
from array import array
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Mapping, Optional
from urllib.parse import parse_qs, urlparse


DEFAULT_LIVE_ENDPOINT = (
    "wss://api.openai.com/v1/realtime?intent=transcription"
)
LIVE_MODEL = "gpt-live-transcribe"
LIVE_DELAYS = frozenset({"minimal", "low", "medium", "high", "xhigh"})
DISCORD_STT_MODES = frozenset(
    {"configured", "openai_contextual", "openai_live_high"}
)
_MODE_ALIASES = {
    "": "configured",
    "default": "configured",
    "configured": "configured",
    "contextual": "openai_contextual",
    "openai-contextual": "openai_contextual",
    "openai_contextual": "openai_contextual",
    "live-high": "openai_live_high",
    "openai-live-high": "openai_live_high",
    "openai_live_high": "openai_live_high",
}
_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?$")
_OPENAI_WEBSOCKET_LOGGER = logging.Logger(
    "hermes.openai_realtime_transcription.transport",
    level=logging.WARNING,
)
_OPENAI_WEBSOCKET_LOGGER.addHandler(logging.NullHandler())
_OPENAI_WEBSOCKET_LOGGER.propagate = False


class LiveTranscriptionError(RuntimeError):
    """A Realtime transcription connection or event-contract failure."""


def _create_no_redirect_websocket_connect(uri: str, **kwargs: Any) -> Any:
    """Create a quiet OpenAI WebSocket connector that rejects every redirect."""
    from websockets.asyncio.client import connect
    from websockets.exceptions import SecurityError

    class _NoRedirectConnect(connect):
        def process_redirect(self, exc: Exception) -> Exception | str:
            result = super().process_redirect(exc)
            if isinstance(result, str):
                return SecurityError("OpenAI Realtime redirects are disabled")
            return result

    kwargs.setdefault("logger", _OPENAI_WEBSOCKET_LOGGER)
    return _NoRedirectConnect(uri, **kwargs)


def normalize_discord_stt_mode(value: Any) -> str:
    """Normalize supported mode aliases without silently guessing."""
    key = str(value or "").strip().lower()
    mode = _MODE_ALIASES.get(key)
    if mode is None:
        supported = ", ".join(sorted(DISCORD_STT_MODES))
        raise ValueError(
            f"unsupported Discord STT mode {value!r}; expected one of {supported}"
        )
    return mode


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ()
        if stripped.startswith("["):
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError("expected a valid JSON list of strings") from exc
            if not isinstance(value, list):
                raise ValueError("expected a valid JSON list of strings")
        else:
            return (stripped,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    raise ValueError("expected a string or list of strings")


@dataclass(frozen=True)
class LiveTranscriptionConfig:
    """Validated configuration for one OpenAI transcription session."""

    model: str = LIVE_MODEL
    delay: str = "high"
    prompt: str = ""
    keywords: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    endpoint: str = DEFAULT_LIVE_ENDPOINT
    completion_timeout_seconds: float = 20.0
    send_timeout_seconds: float = 5.0
    max_session_seconds: float = 3300.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "model", str(self.model or LIVE_MODEL).strip())
        object.__setattr__(self, "delay", str(self.delay or "high").strip().lower())
        object.__setattr__(self, "prompt", str(self.prompt or "").strip())
        object.__setattr__(self, "keywords", _string_tuple(self.keywords))
        object.__setattr__(self, "languages", _string_tuple(self.languages))
        object.__setattr__(self, "endpoint", str(self.endpoint or "").strip())
        object.__setattr__(
            self,
            "completion_timeout_seconds",
            float(self.completion_timeout_seconds),
        )
        object.__setattr__(
            self,
            "send_timeout_seconds",
            float(self.send_timeout_seconds),
        )
        object.__setattr__(
            self,
            "max_session_seconds",
            float(self.max_session_seconds),
        )

        if self.model != LIVE_MODEL:
            raise ValueError(f"unsupported live transcription model: {self.model}")
        if self.delay not in LIVE_DELAYS:
            raise ValueError(f"unsupported live transcription delay: {self.delay}")
        if self.delay != "high":
            raise ValueError("openai_live_high requires delay=high")
        parsed_endpoint = urlparse(self.endpoint)
        if (
            parsed_endpoint.scheme != "wss"
            or parsed_endpoint.hostname != "api.openai.com"
            or parsed_endpoint.path != "/v1/realtime"
            or parse_qs(parsed_endpoint.query).get("intent") != ["transcription"]
        ):
            raise ValueError(
                "openai_live_high endpoint must be "
                "wss://api.openai.com/v1/realtime?intent=transcription"
            )
        if self.completion_timeout_seconds <= 0:
            raise ValueError("completion timeout must be positive")
        if self.send_timeout_seconds <= 0:
            raise ValueError("send timeout must be positive")
        if not 60 <= self.max_session_seconds < 3600:
            raise ValueError("max session duration must be between 60 and 3599 seconds")
        if len(self.prompt) > 5000:
            raise ValueError("live transcription prompt exceeds 5000 characters")
        for keyword in self.keywords:
            if any(character in keyword for character in "<>\r\n"):
                raise ValueError(
                    "live transcription keyword contains a forbidden character"
                )
        for language in self.languages:
            if not _LANGUAGE_RE.fullmatch(language):
                raise ValueError(
                    f"invalid live transcription language code: {language!r}"
                )

    @classmethod
    def from_hermes_config(cls, config: Mapping[str, Any]) -> "LiveTranscriptionConfig":
        """Build settings from ``stt.openai`` and ``discord.voice_stt``."""
        stt = config.get("stt") if isinstance(config, Mapping) else {}
        stt = stt if isinstance(stt, Mapping) else {}
        openai_cfg = stt.get("openai")
        openai_cfg = openai_cfg if isinstance(openai_cfg, Mapping) else {}

        discord_cfg = config.get("discord") if isinstance(config, Mapping) else {}
        discord_cfg = discord_cfg if isinstance(discord_cfg, Mapping) else {}
        voice_stt = discord_cfg.get("voice_stt")
        voice_stt = voice_stt if isinstance(voice_stt, Mapping) else {}
        live_cfg = voice_stt.get("openai_live")
        live_cfg = live_cfg if isinstance(live_cfg, Mapping) else {}

        languages: tuple[str, ...] = ()
        for configured_languages in (
            live_cfg.get("languages"),
            live_cfg.get("language"),
            openai_cfg.get("languages"),
            openai_cfg.get("language"),
            stt.get("language"),
        ):
            languages = _string_tuple(configured_languages)
            if languages:
                break

        return cls(
            model=live_cfg.get("model", LIVE_MODEL),
            delay=live_cfg.get("delay", "high"),
            prompt=live_cfg.get("prompt", openai_cfg.get("prompt", "")),
            keywords=_string_tuple(
                live_cfg.get("keywords", openai_cfg.get("keywords", ()))
            ),
            languages=_string_tuple(languages),
            endpoint=live_cfg.get("endpoint", DEFAULT_LIVE_ENDPOINT),
            completion_timeout_seconds=live_cfg.get(
                "completion_timeout_seconds", 20.0
            ),
            send_timeout_seconds=live_cfg.get("send_timeout_seconds", 5.0),
            max_session_seconds=live_cfg.get("max_session_seconds", 3300.0),
        )


def resolve_openai_realtime_api_key(config: Mapping[str, Any]) -> str:
    """Resolve a direct OpenAI key without routing through managed gateways."""
    stt = config.get("stt") if isinstance(config, Mapping) else {}
    stt = stt if isinstance(stt, Mapping) else {}
    openai_cfg = stt.get("openai")
    openai_cfg = openai_cfg if isinstance(openai_cfg, Mapping) else {}
    configured = str(openai_cfg.get("api_key") or "").strip()
    if configured:
        return configured

    from hermes_cli.config import get_env_value
    for name in ("VOICE_TOOLS_OPENAI_KEY", "OPENAI_API_KEY"):
        value = str(get_env_value(name) or "").strip()
        if value:
            return value
    raise ValueError(
        "openai_live_high requires a direct OpenAI API key in "
        "stt.openai.api_key, VOICE_TOOLS_OPENAI_KEY, or OPENAI_API_KEY"
    )


def build_live_session_update(config: LiveTranscriptionConfig) -> Dict[str, Any]:
    """Build the documented dedicated Realtime transcription session update."""
    transcription: Dict[str, Any] = {
        "model": config.model,
        "delay": config.delay,
    }
    if config.prompt:
        transcription["prompt"] = config.prompt
    if config.keywords:
        transcription["keywords"] = list(config.keywords)
    if config.languages:
        transcription["languages"] = list(config.languages)
    return {
        "type": "session.update",
        "session": {
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": transcription,
                    "turn_detection": None,
                }
            },
        },
    }


def pcm48_stereo_to_pcm24_mono(pcm: bytes) -> bytes:
    """Downmix signed 16-bit 48 kHz stereo PCM to 24 kHz mono PCM.

    Discord's decoder emits interleaved little-endian stereo frames. Each output
    sample averages both channels across two consecutive 48 kHz frames. This is
    a deterministic 2-tap low-pass before the exact 2:1 downsample and avoids
    the worst aliasing of plain frame decimation without adding a DSP dependency.
    """
    if len(pcm) % 8:
        raise ValueError("PCM must contain whole pairs of 48 kHz stereo frames")
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    converted = array(
        "h",
        (
            (
                int(samples[index])
                + int(samples[index + 1])
                + int(samples[index + 2])
                + int(samples[index + 3])
            )
            // 4
            for index in range(0, len(samples), 4)
        ),
    )
    if sys.byteorder != "little":
        converted.byteswap()
    return converted.tobytes()


@dataclass
class _PendingTurn:
    future: asyncio.Future
    item_id: Optional[str] = None
    delta_parts: list[str] = field(default_factory=list)


class OpenAIRealtimeTranscriptionSession:
    """One persistent, multi-turn OpenAI transcription WebSocket session."""

    MAX_EARLY_ITEM_IDS = 16
    MAX_EARLY_EVENTS_PER_ITEM = 8
    MAX_ITEM_TOMBSTONES = 64

    def __init__(
        self,
        *,
        api_key: str,
        config: LiveTranscriptionConfig,
        websocket_connect: Optional[Callable[..., Any]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not str(api_key or "").strip():
            raise ValueError("OpenAI Realtime API key is required")
        self._api_key = str(api_key).strip()
        self.config = config
        self._websocket_connect = websocket_connect
        self._clock = clock
        self._ws = None
        self._reader_task: Optional[asyncio.Task] = None
        self._start_lock = asyncio.Lock()
        self._rollover_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._connected_at: Optional[float] = None
        self._rollover_count = 0
        self._generation = 0
        self._terminal_error: Optional[LiveTranscriptionError] = None
        self._pending_unassigned: Deque[_PendingTurn] = deque()
        self._pending_by_item: Dict[str, _PendingTurn] = {}
        self._early_events: Dict[str, list[Dict[str, Any]]] = defaultdict(list)
        self._item_tombstones: set[str] = set()
        self._item_tombstone_order: Deque[str] = deque()
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise LiveTranscriptionError("OpenAI Realtime session is closed")
        if self._ws is not None and self._reader_task and not self._reader_task.done():
            return
        async with self._start_lock:
            if self._closed:
                raise LiveTranscriptionError("OpenAI Realtime session is closed")
            if self._ws is not None and self._reader_task and not self._reader_task.done():
                return
            stale_ws = self._ws
            self._ws = None
            self._reader_task = None
            self._connected_at = None
            if stale_ws is not None:
                try:
                    await stale_ws.close()
                except Exception:
                    pass
            connect = self._websocket_connect
            if connect is None:
                connect = _create_no_redirect_websocket_connect

            self._ws = await connect(
                self.config.endpoint,
                additional_headers={
                    "Authorization": f"Bearer {self._api_key}",
                },
                compression=None,
                open_timeout=10,
                close_timeout=5,
                max_size=4 * 1024 * 1024,
                logger=_OPENAI_WEBSOCKET_LOGGER,
            )
            try:
                await self._send_json(build_live_session_update(self.config))
                await self._wait_for_session_updated()
            except BaseException:
                failed_ws = self._ws
                self._ws = None
                if failed_ws is not None:
                    try:
                        await failed_ws.close()
                    except Exception:
                        pass
                raise
            self._connected_at = self._clock()
            self._generation += 1
            self._terminal_error = None
            self._reader_task = asyncio.create_task(self._reader_loop())

    @property
    def rollover_count(self) -> int:
        return self._rollover_count

    @property
    def generation(self) -> int:
        return self._generation

    async def rollover_if_due(self) -> bool:
        """Reconnect between turns before the provider's 60-minute limit."""
        connected_at = self._connected_at
        if connected_at is None:
            return False
        if self._clock() - connected_at < self.config.max_session_seconds:
            return False
        if self._pending_unassigned or self._pending_by_item:
            return False
        async with self._rollover_lock:
            connected_at = self._connected_at
            if connected_at is None:
                return False
            if self._clock() - connected_at < self.config.max_session_seconds:
                return False
            if self._pending_unassigned or self._pending_by_item:
                return False
            async with self._send_lock:
                await self._close_transport()
            await self.start()
            self._rollover_count += 1
            return True

    async def _wait_for_session_updated(self) -> None:
        assert self._ws is not None
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
            event = json.loads(raw)
            event_type = str(event.get("type") or "")
            if event_type == "error":
                raise LiveTranscriptionError(self._safe_error_type(event))
            if event_type in {"session.updated", "transcription_session.updated"}:
                return

    @staticmethod
    def _safe_error_type(event: Mapping[str, Any]) -> str:
        error = event.get("error")
        if isinstance(error, Mapping):
            code = str(error.get("code") or error.get("type") or "api_error")
        else:
            code = "api_error"
        return f"OpenAI Realtime transcription error: {code}"

    async def _send_json(self, event: Mapping[str, Any]) -> None:
        if self._ws is None:
            raise LiveTranscriptionError("OpenAI Realtime session is not connected")
        await asyncio.wait_for(
            self._ws.send(
                json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            ),
            timeout=self.config.send_timeout_seconds,
        )
        if self._closed:
            raise LiveTranscriptionError("OpenAI Realtime session is closed")

    async def append_pcm24(self, pcm: bytes) -> None:
        if not pcm or len(pcm) % 2:
            raise ValueError("live PCM must be non-empty signed 16-bit mono audio")
        self._raise_terminal_error()
        await self.start()
        self._raise_terminal_error()
        async with self._send_lock:
            await self._send_json(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm).decode("ascii"),
                }
            )

    async def commit(self) -> Dict[str, Any]:
        self._raise_terminal_error()
        await self.start()
        self._raise_terminal_error()
        loop = asyncio.get_running_loop()
        pending = _PendingTurn(future=loop.create_future())
        async with self._send_lock:
            self._pending_unassigned.append(pending)
            try:
                await self._send_json({"type": "input_audio_buffer.commit"})
            except BaseException:
                self._remove_pending(pending)
                raise
        try:
            return await asyncio.wait_for(
                asyncio.shield(pending.future),
                timeout=self.config.completion_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            self._remove_pending(pending)
            raise LiveTranscriptionError(
                "OpenAI Realtime transcription completion timed out"
            ) from exc

    async def clear(self) -> None:
        if self._ws is None:
            return
        async with self._send_lock:
            await self._send_json({"type": "input_audio_buffer.clear"})

    async def _reader_loop(self) -> None:
        try:
            while self._ws is not None:
                raw = await self._ws.recv()
                event = json.loads(raw)
                await self._observe_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_terminal_error(
                exc
                if isinstance(exc, LiveTranscriptionError)
                else LiveTranscriptionError(
                    f"OpenAI Realtime receive failed: {type(exc).__name__}"
                )
            )

    async def _observe_event(self, event: Dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "error":
            self._record_terminal_error(
                LiveTranscriptionError(self._safe_error_type(event))
            )
            return
        if event_type == "input_audio_buffer.committed":
            item_id = str(event.get("item_id") or "").strip()
            if not item_id:
                self._record_terminal_error(
                    LiveTranscriptionError(
                        "OpenAI Realtime commit acknowledgement had no item_id"
                    )
                )
                return
            if item_id in self._pending_by_item or item_id in self._item_tombstones:
                self._record_terminal_error(
                    LiveTranscriptionError(
                        "OpenAI Realtime duplicate commit acknowledgement"
                    )
                )
                return
            if not self._pending_unassigned:
                self._record_terminal_error(
                    LiveTranscriptionError(
                        "OpenAI Realtime unexpected commit acknowledgement"
                    )
                )
                return
            pending = self._pending_unassigned.popleft()
            pending.item_id = item_id
            self._pending_by_item[item_id] = pending
            for early in self._early_events.pop(item_id, []):
                await self._observe_event(early)
            return
        if event_type not in {
            "conversation.item.input_audio_transcription.delta",
            "conversation.item.input_audio_transcription.completed",
            "conversation.item.input_audio_transcription.failed",
        }:
            return
        item_id = str(event.get("item_id") or "").strip()
        if item_id in self._item_tombstones:
            return
        pending = self._pending_by_item.get(item_id)
        if pending is None:
            if not item_id:
                self._record_terminal_error(
                    LiveTranscriptionError(
                        "OpenAI Realtime transcription event had no item_id"
                    )
                )
                return
            if not self._pending_unassigned:
                self._record_terminal_error(
                    LiveTranscriptionError(
                        "OpenAI Realtime transcription event had no pending turn"
                    )
                )
                return
            if (
                item_id not in self._early_events
                and len(self._early_events) >= self.MAX_EARLY_ITEM_IDS
            ):
                self._record_terminal_error(
                    LiveTranscriptionError(
                        "OpenAI Realtime early-item limit exceeded"
                    )
                )
                return
            events = self._early_events[item_id]
            if len(events) >= self.MAX_EARLY_EVENTS_PER_ITEM:
                self._record_terminal_error(
                    LiveTranscriptionError(
                        "OpenAI Realtime early-event limit exceeded"
                    )
                )
                return
            events.append(event)
            return
        if event_type.endswith(".delta"):
            delta = str(event.get("delta") or "")
            if delta:
                pending.delta_parts.append(delta)
            return
        self._pending_by_item.pop(item_id, None)
        self._remember_item_tombstone(item_id)
        if event_type.endswith(".failed"):
            if not pending.future.done():
                pending.future.set_exception(
                    LiveTranscriptionError("OpenAI Realtime transcription failed")
                )
            return
        if not pending.future.done():
            pending.future.set_result(
                {
                    "success": True,
                    "transcript": str(event.get("transcript") or "").strip(),
                    "partial_transcript": "".join(pending.delta_parts),
                    "provider": "openai_realtime",
                    "model": self.config.model,
                    "delay": self.config.delay,
                    "item_id": item_id,
                    "usage": event.get("usage"),
                    "session_rollovers": self._rollover_count,
                }
            )

    def _remember_item_tombstone(self, item_id: str) -> None:
        if not item_id or item_id in self._item_tombstones:
            return
        self._item_tombstones.add(item_id)
        self._item_tombstone_order.append(item_id)
        while len(self._item_tombstone_order) > self.MAX_ITEM_TOMBSTONES:
            expired = self._item_tombstone_order.popleft()
            self._item_tombstones.discard(expired)

    def _remove_pending(self, pending: _PendingTurn) -> None:
        try:
            self._pending_unassigned.remove(pending)
        except ValueError:
            pass
        if pending.item_id:
            self._pending_by_item.pop(pending.item_id, None)
        if not pending.future.done():
            pending.future.cancel()

    def _fail_all(self, error: BaseException) -> None:
        pending = list(self._pending_unassigned) + list(
            self._pending_by_item.values()
        )
        self._pending_unassigned.clear()
        self._pending_by_item.clear()
        self._early_events.clear()
        seen = set()
        for turn in pending:
            if id(turn) in seen:
                continue
            seen.add(id(turn))
            if not turn.future.done():
                turn.future.set_exception(error)

    def _record_terminal_error(self, error: LiveTranscriptionError) -> None:
        self._terminal_error = error
        self._fail_all(error)

    def _raise_terminal_error(self) -> None:
        if self._terminal_error is not None:
            raise self._terminal_error

    async def _close_transport(self) -> None:
        reader = self._reader_task
        self._reader_task = None
        if reader is not None and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        ws = self._ws
        self._ws = None
        self._connected_at = None
        self._early_events.clear()
        self._item_tombstones.clear()
        self._item_tombstone_order.clear()
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def close(self) -> None:
        async with self._start_lock:
            self._closed = True
            async with self._send_lock:
                await self._close_transport()
        self._fail_all(LiveTranscriptionError("OpenAI Realtime session closed"))


class DiscordLiveTranscriptionController:
    """Per-user persistent sessions plus full-utterance source accounting."""

    MAX_SESSIONS = 4

    def __init__(
        self,
        *,
        api_key: str,
        config: LiveTranscriptionConfig,
        session_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.config = config
        self._api_key = api_key
        self._session_factory = session_factory
        self._sessions: Dict[int, Any] = {}
        self._source_bytes: Dict[int, int] = defaultdict(int)
        self._turn_errors: Dict[int, str] = {}
        self._turn_generations: Dict[int, int] = {}
        self._operation_lock = asyncio.Lock()
        self._closed = False

    def _get_session(self, user_id: int):
        if self._closed:
            raise LiveTranscriptionError("Discord Live STT controller is closed")
        session = self._sessions.get(user_id)
        if session is None:
            if len(self._sessions) >= self.MAX_SESSIONS:
                raise LiveTranscriptionError("Discord Live STT session limit reached")
            if self._session_factory is not None:
                session = self._session_factory()
            else:
                session = OpenAIRealtimeTranscriptionSession(
                    api_key=self._api_key,
                    config=self.config,
                )
            self._sessions[user_id] = session
        return session

    async def append_pcm48(self, user_id: int, pcm: bytes) -> None:
        async with self._operation_lock:
            await self._append_pcm48(user_id, pcm)

    async def _append_pcm48(self, user_id: int, pcm: bytes) -> None:
        if self._closed:
            raise LiveTranscriptionError("Discord Live STT controller is closed")
        if not pcm or user_id in self._turn_errors:
            return
        try:
            pcm24 = pcm48_stereo_to_pcm24_mono(pcm)
            session = self._get_session(user_id)
            if self._source_bytes[user_id] == 0:
                rollover = getattr(session, "rollover_if_due", None)
                if rollover is not None:
                    await rollover()
            await session.append_pcm24(pcm24)
            if self._closed:
                raise LiveTranscriptionError("Discord Live STT controller is closed")
            generation = int(getattr(session, "generation", 0))
            previous_generation = self._turn_generations.get(user_id)
            if previous_generation is None:
                self._turn_generations[user_id] = generation
            elif previous_generation != generation:
                self._turn_errors[user_id] = "session_generation_changed"
                return
            self._source_bytes[user_id] += len(pcm)
        except Exception as exc:
            if self._closed:
                raise LiveTranscriptionError(
                    "Discord Live STT controller is closed"
                ) from exc
            self._turn_errors[user_id] = type(exc).__name__

    async def finish_utterance(
        self,
        user_id: int,
        *,
        expected_source_bytes: int,
    ) -> Dict[str, Any]:
        async with self._operation_lock:
            return await self._finish_utterance(
                user_id,
                expected_source_bytes=expected_source_bytes,
            )

    async def _finish_utterance(
        self,
        user_id: int,
        *,
        expected_source_bytes: int,
    ) -> Dict[str, Any]:
        if self._closed:
            return self._failure("live_controller_closed")
        streamed = self._source_bytes.pop(user_id, 0)
        turn_error = self._turn_errors.pop(user_id, None)
        self._turn_generations.pop(user_id, None)
        session = self._sessions.get(user_id)
        if turn_error:
            await self._drop_session(user_id)
            return self._failure("live_stream_failed")
        if streamed != int(expected_source_bytes) or session is None:
            await self._discard_current_buffer(session)
            return self._failure("incomplete_live_stream")
        try:
            return await session.commit()
        except Exception as exc:
            await self._drop_session(user_id)
            return self._failure(f"live_commit_failed:{type(exc).__name__}")

    async def _discard_current_buffer(self, session: Any) -> None:
        if session is None:
            return
        try:
            await session.clear()
        except Exception:
            for user_id, candidate in list(self._sessions.items()):
                if candidate is session:
                    await self._drop_session(user_id)
                    break

    def _failure(self, error: str) -> Dict[str, Any]:
        return {
            "success": False,
            "transcript": "",
            "error": error,
            "provider": "openai_realtime",
            "model": self.config.model,
            "delay": self.config.delay,
        }

    async def _drop_session(self, user_id: int) -> None:
        session = self._sessions.pop(user_id, None)
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass

    async def abort_user(self, user_id: int) -> None:
        """Discard all accounting and provider state for one user."""
        async with self._operation_lock:
            self._source_bytes.pop(user_id, None)
            self._turn_errors.pop(user_id, None)
            self._turn_generations.pop(user_id, None)
            await self._drop_session(user_id)

    async def close(self) -> None:
        self._closed = True
        async with self._operation_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._source_bytes.clear()
            self._turn_errors.clear()
            self._turn_generations.clear()
            await asyncio.gather(
                *(session.close() for session in sessions),
                return_exceptions=True,
            )
