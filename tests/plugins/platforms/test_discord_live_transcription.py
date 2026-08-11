import asyncio
import base64
import copy
import json
import logging
import struct

import pytest

from hermes_cli.config_defaults import DEFAULT_CONFIG
from plugins.platforms.discord import live_transcription
from plugins.platforms.discord.live_transcription import (
    DiscordLiveTranscriptionController,
    LiveTranscriptionConfig,
    LiveTranscriptionError,
    OpenAIRealtimeTranscriptionSession,
    build_live_session_update,
    normalize_discord_stt_mode,
    pcm48_stereo_to_pcm24_mono,
    resolve_openai_realtime_api_key,
)


class FakeWebSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def recv(self):
        return json.dumps(await self.incoming.get())

    async def close(self):
        self.closed = True


async def _wait_for_sent_type(ws, event_type, count=1):
    for _ in range(100):
        if sum(event.get("type") == event_type for event in ws.sent) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"timed out waiting for {count} {event_type} event(s)")


def test_mode_aliases_are_strict_and_backward_safe():
    assert normalize_discord_stt_mode(None) == "configured"
    assert normalize_discord_stt_mode("default") == "configured"
    assert normalize_discord_stt_mode("configured") == "configured"
    assert normalize_discord_stt_mode("contextual") == "openai_contextual"
    assert normalize_discord_stt_mode("openai-contextual") == "openai_contextual"
    assert normalize_discord_stt_mode("live-high") == "openai_live_high"
    assert normalize_discord_stt_mode("openai_live_high") == "openai_live_high"
    with pytest.raises(ValueError, match="unsupported Discord STT mode"):
        normalize_discord_stt_mode("surprise-paid-backend")


def test_pcm48_stereo_to_pcm24_mono_downmixes_and_decimates():
    source = struct.pack(
        "<12h",
        1000,
        3000,
        30000,
        30000,
        -2000,
        -4000,
        -30000,
        -30000,
        200,
        400,
        1234,
        5678,
    )
    converted = pcm48_stereo_to_pcm24_mono(source)
    assert struct.unpack("<3h", converted) == (16000, -16500, 1878)


def test_pcm_converter_rejects_partial_stereo_frame():
    with pytest.raises(ValueError, match="whole pairs of 48 kHz stereo frames"):
        pcm48_stereo_to_pcm24_mono(b"\x00\x01")


def test_session_update_matches_gpt_live_transcribe_contract():
    config = LiveTranscriptionConfig(
        prompt="German private voice chat about AI.",
        keywords=("Hermes Agent", "Mac mini"),
        languages=("de", "en"),
    )
    update = build_live_session_update(config)
    transcription = update["session"]["audio"]["input"]["transcription"]
    assert update["type"] == "session.update"
    assert update["session"]["type"] == "transcription"
    assert update["session"]["audio"]["input"]["format"] == {
        "type": "audio/pcm",
        "rate": 24000,
    }
    assert update["session"]["audio"]["input"]["turn_detection"] is None
    assert transcription == {
        "model": "gpt-live-transcribe",
        "prompt": "German private voice chat about AI.",
        "keywords": ["Hermes Agent", "Mac mini"],
        "languages": ["de", "en"],
        "delay": "high",
    }


def test_invalid_context_is_rejected_before_provider_call():
    with pytest.raises(ValueError, match="keyword"):
        LiveTranscriptionConfig(keywords=("bad\nkeyword",))
    with pytest.raises(ValueError, match="delay"):
        LiveTranscriptionConfig(delay="turbo")
    with pytest.raises(ValueError, match="requires delay=high"):
        LiveTranscriptionConfig(delay="low")
    with pytest.raises(ValueError, match="api.openai.com"):
        LiveTranscriptionConfig(
            endpoint="wss://attacker.example/v1/realtime?intent=transcription"
        )


def test_live_config_inherits_context_from_openai_stt_section():
    config = LiveTranscriptionConfig.from_hermes_config(
        {
            "stt": {
                "openai": {
                    "prompt": "German Discord chat.",
                    "keywords": ["Hermes Agent"],
                    "languages": ["de", "en"],
                }
            },
            "discord": {
                "voice_stt": {
                    "openai_live": {
                        "delay": "high",
                    }
                }
            },
        }
    )
    assert config.prompt == "German Discord chat."
    assert config.keywords == ("Hermes Agent",)
    assert config.languages == ("de", "en")


def test_live_config_uses_global_language_fallback_and_cli_json_lists():
    fallback = LiveTranscriptionConfig.from_hermes_config(
        {"stt": {"language": "de"}}
    )
    assert fallback.languages == ("de",)

    cli = LiveTranscriptionConfig.from_hermes_config(
        {
            "stt": {
                "openai": {
                    "keywords": '["Hermes Agent","gpt-live-transcribe"]',
                    "languages": '["de","en"]',
                }
            }
        }
    )
    assert cli.keywords == ("Hermes Agent", "gpt-live-transcribe")
    assert cli.languages == ("de", "en")


def test_live_config_uses_global_language_with_merged_empty_openai_defaults():
    merged = copy.deepcopy(DEFAULT_CONFIG)
    merged["stt"]["language"] = "de"
    merged["stt"]["openai"]["language"] = ""
    merged["stt"]["openai"]["languages"] = "[]"

    config = LiveTranscriptionConfig.from_hermes_config(merged)

    assert config.languages == ("de",)


def test_live_config_rejects_malformed_cli_json_list():
    with pytest.raises(ValueError, match="JSON list"):
        LiveTranscriptionConfig.from_hermes_config(
            {"stt": {"openai": {"languages": '["de"'}}}
        )


def test_realtime_key_resolution_uses_only_direct_openai_credentials(monkeypatch):
    values = {
        "VOICE_TOOLS_OPENAI_KEY": "voice-key",
        "OPENAI_API_KEY": "general-key",
    }
    monkeypatch.setattr(
        "hermes_cli.config.get_env_value",
        lambda name: values.get(name),
    )
    assert resolve_openai_realtime_api_key({}) == "voice-key"
    assert resolve_openai_realtime_api_key(
        {"stt": {"openai": {"api_key": "config-key"}}}
    ) == "config-key"


def test_realtime_key_resolution_fails_without_direct_key(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.get_env_value", lambda _name: None)
    with pytest.raises(ValueError, match="direct OpenAI API key"):
        resolve_openai_realtime_api_key({"stt": {"use_gateway": True}})


def test_default_websocket_connector_rejects_every_redirect(monkeypatch):
    from websockets.asyncio.client import connect
    from websockets.exceptions import SecurityError

    monkeypatch.setattr(
        connect,
        "process_redirect",
        lambda _self, _exc: "wss://attacker.invalid/capture",
    )
    connector = live_transcription._create_no_redirect_websocket_connect(
        LiveTranscriptionConfig().endpoint
    )

    assert isinstance(connector.process_redirect(Exception("redirect")), SecurityError)


@pytest.mark.asyncio
async def test_realtime_connection_uses_non_debug_non_propagating_logger(caplog):
    ws = FakeWebSocket()
    await ws.incoming.put({"type": "session.updated"})
    captured = {}

    async def connect(*args, **kwargs):
        captured.update(kwargs)
        return ws

    session = OpenAIRealtimeTranscriptionSession(
        api_key="synthetic-test-key",
        config=LiveTranscriptionConfig(),
        websocket_connect=connect,
    )
    await session.start()

    transport_logger = captured["logger"]
    assert transport_logger.isEnabledFor(logging.DEBUG) is False
    assert transport_logger.propagate is False
    assert any(isinstance(handler, logging.NullHandler) for handler in transport_logger.handlers)
    with caplog.at_level(logging.DEBUG):
        transport_logger.debug("Authorization: Bearer synthetic-marker")
        transport_logger.debug("secret transcript marker")
    assert "synthetic-marker" not in caplog.text
    assert "secret transcript marker" not in caplog.text

    await session.close()


@pytest.mark.asyncio
async def test_persistent_session_reconciles_out_of_order_completions_by_item_id():
    ws = FakeWebSocket()
    await ws.incoming.put({"type": "session.created"})
    await ws.incoming.put({"type": "session.updated"})

    async def connect(*args, **kwargs):
        assert kwargs["additional_headers"]["Authorization"] == "Bearer test-key"
        assert kwargs["compression"] is None
        return ws

    session = OpenAIRealtimeTranscriptionSession(
        api_key="test-key",
        config=LiveTranscriptionConfig(),
        websocket_connect=connect,
    )
    await session.start()
    assert ws.sent[0]["type"] == "session.update"

    await session.append_pcm24(b"\x01\x00" * 240)
    commit_one = asyncio.create_task(session.commit())
    await _wait_for_sent_type(ws, "input_audio_buffer.commit", 1)

    await session.append_pcm24(b"\x02\x00" * 240)
    commit_two = asyncio.create_task(session.commit())
    await _wait_for_sent_type(ws, "input_audio_buffer.commit", 2)

    await ws.incoming.put(
        {"type": "input_audio_buffer.committed", "item_id": "item_one"}
    )
    await ws.incoming.put(
        {"type": "input_audio_buffer.committed", "item_id": "item_two"}
    )
    await ws.incoming.put(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "item_two",
            "transcript": "zweiter Turn",
            "usage": {"type": "duration", "seconds": 0.01},
        }
    )
    await ws.incoming.put(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "item_one",
            "transcript": "erster Turn",
            "usage": {"type": "duration", "seconds": 0.01},
        }
    )

    first, second = await asyncio.gather(commit_one, commit_two)
    assert first["item_id"] == "item_one"
    assert first["transcript"] == "erster Turn"
    assert second["item_id"] == "item_two"
    assert second["transcript"] == "zweiter Turn"
    assert first["provider"] == "openai_realtime"
    assert first["model"] == "gpt-live-transcribe"

    append_events = [
        event for event in ws.sent if event["type"] == "input_audio_buffer.append"
    ]
    assert len(append_events) == 2
    assert base64.b64decode(append_events[0]["audio"]) == b"\x01\x00" * 240

    await session.close()
    assert ws.closed is True


@pytest.mark.asyncio
async def test_rejected_session_setup_closes_half_open_websocket():
    ws = FakeWebSocket()
    await ws.incoming.put(
        {
            "type": "error",
            "error": {
                "code": "invalid_session",
                "message": "must not leak provider detail secret-value",
            },
        }
    )

    async def connect(*args, **kwargs):
        return ws

    session = OpenAIRealtimeTranscriptionSession(
        api_key="test-key",
        config=LiveTranscriptionConfig(),
        websocket_connect=connect,
    )
    with pytest.raises(LiveTranscriptionError, match="invalid_session") as exc:
        await session.start()
    assert "secret-value" not in str(exc.value)
    assert ws.closed is True


@pytest.mark.asyncio
async def test_close_linearizes_against_blocked_start_and_leaves_no_reader():
    ws = FakeWebSocket()
    await ws.incoming.put({"type": "session.updated"})
    release_connect = asyncio.Event()

    async def blocked_connect(_endpoint, **_kwargs):
        await release_connect.wait()
        return ws

    session = OpenAIRealtimeTranscriptionSession(
        api_key="test-key",
        config=LiveTranscriptionConfig(),
        websocket_connect=blocked_connect,
    )
    start_task = asyncio.create_task(session.start())
    await asyncio.sleep(0)
    close_task = asyncio.create_task(session.close())
    release_connect.set()
    await asyncio.gather(start_task, close_task)

    assert ws.closed is True
    assert session._ws is None
    assert session._reader_task is None


@pytest.mark.asyncio
async def test_close_linearizes_against_blocked_append_send():
    ws = FakeWebSocket()
    await ws.incoming.put({"type": "transcription_session.updated"})
    send_started = asyncio.Event()
    release_send = asyncio.Event()
    original_send = ws.send

    async def send(payload):
        event = json.loads(payload)
        if event.get("type") == "input_audio_buffer.append":
            send_started.set()
            await release_send.wait()
        await original_send(payload)

    ws.send = send

    async def connect(*_args, **_kwargs):
        return ws

    session = OpenAIRealtimeTranscriptionSession(
        api_key="test-key",
        config=LiveTranscriptionConfig(),
        websocket_connect=connect,
    )
    await session.start()
    append_task = asyncio.create_task(session.append_pcm24(b"\x00\x00" * 240))
    await send_started.wait()
    close_task = asyncio.create_task(session.close())
    await asyncio.sleep(0)
    assert ws.closed is False

    release_send.set()
    with pytest.raises(LiveTranscriptionError, match="closed"):
        await append_task
    await close_task
    assert ws.closed is True


@pytest.mark.asyncio
async def test_websocket_send_timeout_bounds_blocked_provider():
    ws = FakeWebSocket()
    await ws.incoming.put({"type": "session.updated"})
    block_send = [False]
    original_send = ws.send

    async def send(payload):
        if block_send[0]:
            await asyncio.Event().wait()
        await original_send(payload)

    ws.send = send

    async def connect(*_args, **_kwargs):
        return ws

    session = OpenAIRealtimeTranscriptionSession(
        api_key="test-key",
        config=LiveTranscriptionConfig(send_timeout_seconds=0.01),
        websocket_connect=connect,
    )
    await session.start()
    block_send[0] = True
    with pytest.raises(asyncio.TimeoutError):
        await session.append_pcm24(b"\x00\x00" * 240)
    await session.close()


@pytest.mark.asyncio
async def test_session_rolls_over_before_provider_sixty_minute_limit_between_turns():
    now = [0.0]
    sockets = [FakeWebSocket(), FakeWebSocket()]
    for ws in sockets:
        await ws.incoming.put({"type": "transcription_session.updated"})
    calls = []

    async def connect(*args, **kwargs):
        calls.append((args, kwargs))
        return sockets[len(calls) - 1]

    session = OpenAIRealtimeTranscriptionSession(
        api_key="test-key",
        config=LiveTranscriptionConfig(max_session_seconds=3300),
        websocket_connect=connect,
        clock=lambda: now[0],
    )
    await session.start()
    now[0] = 3301.0

    assert await session.rollover_if_due() is True
    assert sockets[0].closed is True
    assert len(calls) == 2
    assert session.rollover_count == 1
    await session.close()


@pytest.mark.asyncio
async def test_async_provider_error_without_pending_commit_fails_next_append_immediately():
    ws = FakeWebSocket()
    await ws.incoming.put({"type": "transcription_session.updated"})

    async def connect(*args, **kwargs):
        return ws

    session = OpenAIRealtimeTranscriptionSession(
        api_key="test-key",
        config=LiveTranscriptionConfig(),
        websocket_connect=connect,
    )
    await session.start()
    await ws.incoming.put(
        {
            "type": "error",
            "error": {"code": "bad_audio", "message": "secret-value"},
        }
    )
    await asyncio.sleep(0)

    with pytest.raises(LiveTranscriptionError, match="bad_audio") as exc:
        await session.append_pcm24(struct.pack("<h", 1))
    assert "secret-value" not in str(exc.value)
    await session.close()


@pytest.mark.asyncio
async def test_duplicate_commit_ack_fails_closed_without_turn_misrouting():
    ws = FakeWebSocket()
    await ws.incoming.put({"type": "transcription_session.updated"})

    async def connect(*_args, **_kwargs):
        return ws

    session = OpenAIRealtimeTranscriptionSession(
        api_key="test-key",
        config=LiveTranscriptionConfig(),
        websocket_connect=connect,
    )
    await session.start()
    await session.append_pcm24(b"\x01\x00" * 240)
    first = asyncio.create_task(session.commit())
    await _wait_for_sent_type(ws, "input_audio_buffer.commit", 1)
    await session.append_pcm24(b"\x02\x00" * 240)
    second = asyncio.create_task(session.commit())
    await _wait_for_sent_type(ws, "input_audio_buffer.commit", 2)

    await ws.incoming.put(
        {"type": "input_audio_buffer.committed", "item_id": "duplicate"}
    )
    await ws.incoming.put(
        {"type": "input_audio_buffer.committed", "item_id": "duplicate"}
    )

    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, LiveTranscriptionError) for result in results)
    assert session._pending_by_item == {}
    await session.close()


@pytest.mark.asyncio
async def test_unknown_early_items_are_bounded_and_fail_closed():
    ws = FakeWebSocket()
    await ws.incoming.put({"type": "transcription_session.updated"})

    async def connect(*_args, **_kwargs):
        return ws

    session = OpenAIRealtimeTranscriptionSession(
        api_key="test-key",
        config=LiveTranscriptionConfig(),
        websocket_connect=connect,
    )
    await session.start()
    for index in range(300):
        await ws.incoming.put(
            {
                "type": "conversation.item.input_audio_transcription.delta",
                "item_id": f"unknown-{index}",
                "delta": "x",
            }
        )
    for _ in range(100):
        if session._terminal_error is not None:
            break
        await asyncio.sleep(0)

    assert session._terminal_error is not None
    assert len(session._early_events) <= session.MAX_EARLY_ITEM_IDS
    await session.close()


class FakeRealtimeSession:
    def __init__(self, result=None):
        self.appended = []
        self.append_attempts = 0
        self.generation = 1
        self.commit_calls = 0
        self.clear_calls = 0
        self.close_calls = 0
        self.append_error: Exception | None = None
        self.result = result or {
            "success": True,
            "transcript": "Hallo Hermes",
            "provider": "openai_realtime",
            "model": "gpt-live-transcribe",
            "delay": "high",
            "item_id": "item_1",
        }

    async def append_pcm24(self, pcm):
        self.append_attempts += 1
        if self.append_error is not None:
            raise self.append_error
        self.appended.append(pcm)

    async def commit(self):
        self.commit_calls += 1
        return dict(self.result)

    async def clear(self):
        self.clear_calls += 1

    async def close(self):
        self.close_calls += 1


@pytest.mark.asyncio
async def test_controller_commits_only_when_full_source_utterance_was_streamed():
    fake = FakeRealtimeSession()
    controller = DiscordLiveTranscriptionController(
        api_key="test-key",
        config=LiveTranscriptionConfig(),
        session_factory=lambda: fake,
    )
    source = struct.pack("<8h", 100, 300, 0, 0, -200, -400, 0, 0)
    await controller.append_pcm48(42, source)
    result = await controller.finish_utterance(42, expected_source_bytes=len(source))
    assert result["success"] is True
    assert result["transcript"] == "Hallo Hermes"
    assert fake.commit_calls == 1
    assert len(fake.appended) == 1
    assert struct.unpack("<2h", fake.appended[0]) == (100, -150)


@pytest.mark.asyncio
async def test_controller_fails_closed_when_initial_pcm_was_not_streamed():
    fake = FakeRealtimeSession()
    controller = DiscordLiveTranscriptionController(
        api_key="test-key",
        config=LiveTranscriptionConfig(),
        session_factory=lambda: fake,
    )
    source = struct.pack("<8h", 100, 300, 0, 0, -200, -400, 0, 0)
    await controller.append_pcm48(42, source)
    result = await controller.finish_utterance(
        42,
        expected_source_bytes=len(source) + 3840,
    )
    assert result["success"] is False
    assert result["error"] == "incomplete_live_stream"
    assert result["provider"] == "openai_realtime"
    assert fake.commit_calls == 0
    assert fake.clear_calls == 1


@pytest.mark.asyncio
async def test_controller_drops_failed_stream_session_before_next_turn():
    sessions = [FakeRealtimeSession(), FakeRealtimeSession()]
    first = sessions[0]
    first.append_error = LiveTranscriptionError("bad_audio")

    controller = DiscordLiveTranscriptionController(
        api_key="test-key",
        config=LiveTranscriptionConfig(),
        session_factory=lambda: sessions.pop(0),
    )
    source = struct.pack("<8h", 100, 300, 0, 0, -200, -400, 0, 0)
    await controller.append_pcm48(42, source)
    await controller.append_pcm48(42, source)
    result = await controller.finish_utterance(
        42,
        expected_source_bytes=len(source),
    )

    assert result["success"] is False
    assert result["error"] == "live_stream_failed"
    assert first.append_attempts == 1
    assert first.close_calls == 1
    assert controller._sessions == {}


@pytest.mark.asyncio
async def test_controller_rejects_pcm_split_across_session_generations():
    session = FakeRealtimeSession()
    controller = DiscordLiveTranscriptionController(
        api_key="test-key",
        config=LiveTranscriptionConfig(),
        session_factory=lambda: session,
    )
    source = struct.pack("<8h", 100, 300, 0, 0, -200, -400, 0, 0)

    await controller.append_pcm48(42, source)
    session.generation = 2
    await controller.append_pcm48(42, source)
    result = await controller.finish_utterance(
        42,
        expected_source_bytes=len(source) * 2,
    )

    assert result["success"] is False
    assert result["error"] == "live_stream_failed"
    assert session.close_calls == 1


@pytest.mark.asyncio
async def test_controller_closes_all_per_user_sessions():
    sessions = []

    def factory():
        session = FakeRealtimeSession()
        sessions.append(session)
        return session

    controller = DiscordLiveTranscriptionController(
        api_key="test-key",
        config=LiveTranscriptionConfig(),
        session_factory=factory,
    )
    source = struct.pack("<4h", 100, 300, 0, 0)
    await controller.append_pcm48(1, source)
    await controller.append_pcm48(2, source)
    await controller.close()
    assert len(sessions) == 2
    assert [session.close_calls for session in sessions] == [1, 1]


@pytest.mark.asyncio
async def test_controller_close_linearizes_against_blocked_append():
    append_started = asyncio.Event()
    release_append = asyncio.Event()

    class BlockingSession(FakeRealtimeSession):
        async def append_pcm24(self, pcm):
            append_started.set()
            await release_append.wait()
            await super().append_pcm24(pcm)

    session = BlockingSession()
    controller = DiscordLiveTranscriptionController(
        api_key="test-key",
        config=LiveTranscriptionConfig(),
        session_factory=lambda: session,
    )
    source = struct.pack("<4h", 100, 300, 0, 0)
    append_task = asyncio.create_task(controller.append_pcm48(42, source))
    await append_started.wait()
    close_task = asyncio.create_task(controller.close())
    await asyncio.sleep(0)
    release_append.set()

    with pytest.raises(LiveTranscriptionError, match="closed"):
        await append_task
    await close_task
    assert controller._sessions == {}
    assert controller._source_bytes == {}


@pytest.mark.asyncio
async def test_closed_controller_cannot_create_new_user_sessions():
    sessions = []

    def factory():
        session = FakeRealtimeSession()
        sessions.append(session)
        return session

    controller = DiscordLiveTranscriptionController(
        api_key="test-key",
        config=LiveTranscriptionConfig(),
        session_factory=factory,
    )
    await controller.close()

    with pytest.raises(LiveTranscriptionError, match="closed"):
        await controller.append_pcm48(42, b"\x00" * 8)
    assert sessions == []


@pytest.mark.asyncio
async def test_controller_caps_concurrent_user_sessions():
    controller = DiscordLiveTranscriptionController(
        api_key="test-key",
        config=LiveTranscriptionConfig(),
        session_factory=FakeRealtimeSession,
    )
    source = struct.pack("<4h", 100, 300, 0, 0)
    for user_id in range(1, 6):
        await controller.append_pcm48(user_id, source)

    assert len(controller._sessions) == controller.MAX_SESSIONS
    result = await controller.finish_utterance(
        5,
        expected_source_bytes=len(source),
    )
    assert result["success"] is False
    assert result["error"] == "live_stream_failed"
    await controller.close()
