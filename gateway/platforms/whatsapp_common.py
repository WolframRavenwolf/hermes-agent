"""
Transport-agnostic WhatsApp behavior shared by the Baileys bridge adapter
and the official WhatsApp Cloud API adapter.

The mixin provides:
- Allow-list / DM / group gating
- Mention detection (explicit @-mentions + configurable regex patterns)
- Quoted-reply-to-bot detection
- Broadcast / Channel / Newsletter filtering
- WhatsApp-flavored markdown conversion
- Outgoing chunk length budgeting

It is the *behavior layer*. Transport-specific concerns (subprocess management,
HTTP webhooks, Graph API calls, media upload protocols) live in each adapter.

Mixin contract — the adapter must set these on ``self`` before any of the
mixin's methods are called (typically in ``__init__``):

    self.config        # gateway.config.PlatformConfig
    self.name          # str — adapter name (used in log lines)
    self._dm_policy             # str: "open" | "allowlist" | "disabled"
    self._allow_from            # set[str]
    self._group_policy          # str: "open" | "allowlist" | "disabled"
    self._group_allow_from      # set[str]
    self._mention_patterns      # list[re.Pattern]
    self._reply_prefix          # Optional[str]

Class attributes ``MAX_MESSAGE_LENGTH`` and ``DEFAULT_REPLY_PREFIX`` are
defined on the mixin and may be overridden per-adapter if needed.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sysconfig
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Dict, Optional

try:  # pragma: no cover - availability is platform-specific
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None

try:  # pragma: no cover - availability is platform-specific
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX
    _msvcrt = None

from hermes_constants import find_node_executable, with_hermes_node_path
from utils import env_int

from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
from agent.secret_scope import get_secret as _scoped_get_secret


def _get_wsecret(name, default=None):
    """Scope-aware WHATSAPP_* read with the default-profile startup fallback.

    Secondary profiles run under ``_profile_runtime_scope`` -- the scope is
    authoritative and a scoped miss returns ``default`` (no cross-profile
    borrow). The DEFAULT profile's adapter constructs and sends *unscoped*
    under multiplexing, where a bare ``get_secret`` would raise
    ``UnscopedSecretError`` and crash its WhatsApp path; there ``os.environ``
    is that profile's own value, so fall back to it. Same pattern as the
    Slack ``SLACK_APP_TOKEN`` read (#59739).
    """
    try:
        val = _scoped_get_secret(name, default)
    except _UnscopedSecretError:
        val = os.getenv(name)
    return val if val is not None else default

logger = logging.getLogger(__name__)


class WhatsAppBehaviorMixin:
    """Shared behavior for all WhatsApp adapters (Baileys + Cloud API).

    See module docstring for the attribute contract the host adapter must
    satisfy. This mixin owns no state of its own — every value it touches
    is either a class attribute or set by the adapter's ``__init__``.
    """

    # WhatsApp message limits — practical UX limit, not protocol max.
    # WhatsApp allows ~65K but long messages are unreadable on mobile.
    MAX_MESSAGE_LENGTH: int = 4096
    supports_code_blocks = True  # WhatsApp renders fenced code blocks (monospace)

    DEFAULT_REPLY_PREFIX: str = "⚕ *Hermes Agent*\n────────────\n"

    _OUTBOUND_INVISIBLE_CHARS_RE = re.compile(r"[\u200b\u2060\u2063\ufeff]")
    _OUTBOUND_ODD_SPACE_RE = re.compile(r"[\u00a0\u1680\u180e\u2000-\u200a\u202f\u205f\u3000]")

    @classmethod
    def _sanitize_outbound_text(cls, content: str) -> str:
        """Remove invisible formatting chars that leak badly in WhatsApp.

        Some provider/gateway formatting paths can emit unicode like WORD
        JOINER (U+2060) plus NARROW NO-BREAK SPACE (U+202F). WhatsApp may
        render those as mojibake-looking prefixes (``⁠ text``) instead of
        invisible spacing. Keep normal text and emoji joiners intact, but
        strip known zero-width format chars and normalize odd unicode spaces.
        """
        if not content:
            return content
        content = cls._OUTBOUND_INVISIBLE_CHARS_RE.sub("", content)
        return cls._OUTBOUND_ODD_SPACE_RE.sub(" ", content)

    @property
    def enforces_own_access_policy(self) -> bool:
        """WhatsApp gates DM/group access at intake via dm_policy/group_policy."""
        return True

    # ------------------------------------------------------------------ config
    def _effective_reply_prefix(self) -> str:
        """Return the prefix to add to outgoing replies in self-chat mode.

        Subclasses that don't have a self-chat concept (the Cloud API
        adapter) can override this to always return ``""`` or apply a
        different policy.
        """
        whatsapp_mode = _get_wsecret("WHATSAPP_MODE", default="self-chat") or "self-chat"
        if whatsapp_mode != "self-chat":
            return ""
        if self._reply_prefix is not None:
            return self._reply_prefix.replace("\\n", "\n")
        env_prefix = _get_wsecret("WHATSAPP_REPLY_PREFIX")
        if env_prefix is not None:
            return env_prefix.replace("\\n", "\n")
        return self.DEFAULT_REPLY_PREFIX

    def _outgoing_chunk_limit(self) -> int:
        """Reserve room for the reply prefix so the final message fits."""
        prefix_len = len(self._effective_reply_prefix())
        # Keep enough space for truncate_message's pagination indicator and
        # code-fence repair even if a user configures a very long prefix.
        return max(1024, self.MAX_MESSAGE_LENGTH - prefix_len)

    def _whatsapp_require_mention(self) -> bool:
        configured = self.config.extra.get("require_mention")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in {"true", "1", "yes", "on"}
            return bool(configured)
        return (_get_wsecret("WHATSAPP_REQUIRE_MENTION", default="false") or "false").lower() in {
            "true",
            "1",
            "yes",
            "on",
        }

    def _whatsapp_free_response_chats(self) -> set[str]:
        raw = self.config.extra.get("free_response_chats")
        if raw is None:
            raw = _get_wsecret("WHATSAPP_FREE_RESPONSE_CHATS", default="") or ""
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}

    @staticmethod
    def _coerce_allow_list(raw) -> set[str]:
        """Parse allow_from / group_allow_from from config or env var."""
        if raw is None:
            return set()
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}

    def _live_dm_allow_from(self) -> set[str]:
        """Allowlist currently enforced for DM intake / strict DM auth.

        Source precedence matches construction: explicit config wins over any
        env carrier. When the adapter was seeded from an env var, re-read that
        same key so pairing approve/revoke takes effect without restart
        (including an empty value while the key is still present). When the key
        is absent — sole-entry revoke calls ``remove_env_value`` — treat the
        allowlist as empty instead of falling back to the construction-time
        snapshot. Config-seeded adapters keep the in-memory snapshot, which
        pairing revoke purges in place — a lower-precedence or stale env value
        must not broaden access.
        """
        source = getattr(self, "_dm_allowlist_source", None)
        if isinstance(source, str) and source != "config":
            if source in os.environ:
                return self._coerce_allow_list(os.environ.get(source, ""))
            # Key removed (e.g. sole-entry pairing revoke) — do not revive the
            # stale construction snapshot.
            return set()
        return set(self._allow_from or ())

    # ------------------------------------------------------------------ JID helpers
    @staticmethod
    def _normalize_whatsapp_id(value: Optional[str]) -> str:
        if not value:
            return ""
        normalized = str(value).strip()
        if ":" in normalized and "@" in normalized:
            normalized = normalized.replace(":", "@", 1)
        return normalized

    @staticmethod
    def _is_broadcast_chat(chat_id: str) -> bool:
        """True for WhatsApp pseudo-chats that aren't real conversations.

        Covers Status updates (Stories) and Channel/Newsletter broadcasts.
        These show up as inbound messages on Baileys but the agent should
        never reply — answering a Story update spams the contact's status
        feed, and Channel posts aren't addressable in the first place.
        """
        if not chat_id:
            return False
        cid = chat_id.strip().lower()
        if cid == "status@broadcast":
            return True
        # @broadcast suffix covers status@broadcast plus any future
        # broadcast-list variants. @newsletter is the Channel JID suffix.
        if cid.endswith("@broadcast") or cid.endswith("@newsletter"):
            return True
        return False

    # ------------------------------------------------------------------ gating
    def _open_dm_opted_in(self) -> bool:
        if os.getenv("GATEWAY_ALLOW_ALL_USERS", "").lower() in {"true", "1", "yes"}:
            return True
        return (_get_wsecret("WHATSAPP_ALLOW_ALL_USERS", default="") or "").lower() in {"true", "1", "yes"}

    @staticmethod
    def _matches_whatsapp_allowlist(candidate: str, allow_from) -> bool:
        """Match a WhatsApp identifier against an allowlist across phone/LID forms.

        WhatsApp delivers inbound senders in LID form (``<id>@lid``) while
        operators usually configure allowlists with phone numbers, and vice
        versa. A raw set-membership check therefore never matches a known
        contact. Resolve both the candidate and each allowlist entry through
        the bridge's ``lid-mapping-*.json`` files (the shared
        ``gateway.whatsapp_identity`` helper that the gateway authz and
        session-key paths already use) so either configured form resolves to
        the inbound form.
        """
        if not allow_from:
            return False
        # Fast path: exact match against the raw configured value (e.g. a full
        # ``@g.us`` group JID or an entry that already matches verbatim).
        if candidate in allow_from:
            return True

        from gateway.whatsapp_identity import (
            expand_whatsapp_aliases,
            normalize_whatsapp_identifier,
        )

        candidate_aliases = expand_whatsapp_aliases(candidate)
        if not candidate_aliases:
            return False
        for entry in allow_from:
            if entry == "*":
                return True
            if normalize_whatsapp_identifier(entry) in candidate_aliases:
                return True
            # Entry may itself be an unmapped form; expand it too so a phone
            # allowlist entry resolves when the inbound sender arrived as a LID.
            if expand_whatsapp_aliases(entry) & candidate_aliases:
                return True
        return False

    def _is_dm_allowed(self, sender_id: str) -> bool:
        """Strict DM authorization — pairing does not imply access."""
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            return self._matches_whatsapp_allowlist(sender_id, self._live_dm_allow_from())
        if self._dm_policy == "open":
            return self._open_dm_opted_in()
        return False

    def _is_dm_intake_allowed(self, sender_id: str) -> bool:
        """Whether a DM may reach the gateway intake (pairing handshake path)."""
        principal = str(sender_id or "").strip()
        if not principal:
            return False
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            return self._matches_whatsapp_allowlist(principal, self._live_dm_allow_from())
        if self._dm_policy == "pairing":
            return True
        if self._dm_policy == "open":
            return self._open_dm_opted_in()
        return False

    def _is_group_allowed(self, chat_id: str) -> bool:
        """Check whether a group chat should be processed."""
        if self._group_policy == "disabled":
            return False
        if self._group_policy == "allowlist":
            return self._matches_whatsapp_allowlist(chat_id, self._group_allow_from)
        if self._group_policy == "pairing":
            return False
        if self._group_policy == "open":
            return True
        return False

    def _compile_mention_patterns(self):
        patterns = self.config.extra.get("mention_patterns")
        if patterns is None:
            raw = (_get_wsecret("WHATSAPP_MENTION_PATTERNS", default="") or "").strip()
            if raw:
                try:
                    patterns = json.loads(raw)
                except Exception:
                    patterns = [
                        part.strip() for part in raw.splitlines() if part.strip()
                    ]
                    if not patterns:
                        patterns = [
                            part.strip() for part in raw.split(",") if part.strip()
                        ]
        if patterns is None:
            return []
        if isinstance(patterns, str):
            patterns = [patterns]
        if not isinstance(patterns, list):
            logger.warning(
                "[%s] whatsapp mention_patterns must be a list or string; got %s",
                self.name,
                type(patterns).__name__,
            )
            return []

        compiled = []
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            try:
                compiled.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                logger.warning(
                    "[%s] Invalid WhatsApp mention pattern %r: %s",
                    self.name,
                    pattern,
                    exc,
                )
        if compiled:
            logger.info(
                "[%s] Loaded %d WhatsApp mention pattern(s)", self.name, len(compiled)
            )
        return compiled

    def _bot_ids_from_message(self, data: Dict[str, Any]) -> set[str]:
        bot_ids = set()
        for candidate in data.get("botIds") or []:
            normalized = self._normalize_whatsapp_id(candidate)
            if normalized:
                bot_ids.add(normalized)
        return bot_ids

    def _message_is_reply_to_bot(self, data: Dict[str, Any]) -> bool:
        quoted_participant = self._normalize_whatsapp_id(data.get("quotedParticipant"))
        if not quoted_participant:
            return False
        return quoted_participant in self._bot_ids_from_message(data)

    def _message_mentions_bot(self, data: Dict[str, Any]) -> bool:
        bot_ids = self._bot_ids_from_message(data)
        if not bot_ids:
            return False
        mentioned_ids = {
            nid
            for candidate in (data.get("mentionedIds") or [])
            if (nid := self._normalize_whatsapp_id(candidate))
        }
        if mentioned_ids & bot_ids:
            return True

        body = str(data.get("body") or "")
        lower_body = body.lower()
        for bot_id in bot_ids:
            bare_id = bot_id.split("@", 1)[0].lower()
            if bare_id and (f"@{bare_id}" in lower_body or bare_id in lower_body):
                return True
        return False

    def _message_matches_mention_patterns(self, data: Dict[str, Any]) -> bool:
        if not self._mention_patterns:
            return False
        body = str(data.get("body") or "")
        return any(pattern.search(body) for pattern in self._mention_patterns)

    def _clean_bot_mention_text(self, text: str, data: Dict[str, Any]) -> str:
        if not text:
            return text
        bot_ids = self._bot_ids_from_message(data)
        cleaned = text
        for bot_id in bot_ids:
            bare_id = bot_id.split("@", 1)[0]
            if bare_id:
                cleaned = re.sub(
                    rf"@{re.escape(bare_id)}\b[,:\-]*\s*", "", cleaned
                )
        return cleaned.strip() or text

    def _should_process_message(self, data: Dict[str, Any]) -> bool:
        chat_id_raw = str(data.get("chatId") or "")
        # WhatsApp uses pseudo-chats for Status updates (Stories) and
        # Channel/Newsletter broadcasts. These are not real conversations
        # and the agent should never reply to them — even in self-chat mode
        # where the bridge may surface them as "fromMe" events.
        if self._is_broadcast_chat(chat_id_raw):
            return False
        is_group = data.get("isGroup", False)
        if is_group:
            chat_id = chat_id_raw
            if not self._is_group_allowed(chat_id):
                return False
        else:
            sender_id = str(data.get("senderId") or data.get("from") or "")
            if not self._is_dm_intake_allowed(sender_id):
                return False
            # DMs that pass the policy gate are always processed
            return True
        # Group messages: check mention / free-response settings
        chat_id = str(data.get("chatId") or "")
        if chat_id in self._whatsapp_free_response_chats():
            return True
        if not self._whatsapp_require_mention():
            return True
        body = str(data.get("body") or "").strip()
        if body.startswith("/"):
            return True
        if self._message_is_reply_to_bot(data):
            return True
        if self._message_mentions_bot(data):
            return True
        return self._message_matches_mention_patterns(data)

    # ------------------------------------------------------------------ formatting
    def format_message(self, content: str) -> str:
        """Convert standard markdown to WhatsApp-compatible formatting.

        WhatsApp supports: *bold*, _italic_, ~strikethrough~, ```code```,
        and monospaced `inline`. Standard markdown uses different syntax
        for bold/italic/strikethrough, so we convert here.

        Code blocks (``` fenced) and inline code (`) are protected from
        conversion via placeholder substitution.
        """
        if not content:
            return content

        content = self._sanitize_outbound_text(content)

        # --- 1. Protect fenced code blocks from formatting changes ---
        _FENCE_PH = "\x00FENCE"
        fences: list[str] = []

        def _save_fence(m: re.Match) -> str:
            fences.append(m.group(0))
            return f"{_FENCE_PH}{len(fences) - 1}\x00"

        result = re.sub(r"```[\s\S]*?```", _save_fence, content)

        # --- 2. Protect inline code ---
        _CODE_PH = "\x00CODE"
        codes: list[str] = []

        def _save_code(m: re.Match) -> str:
            codes.append(m.group(0))
            return f"{_CODE_PH}{len(codes) - 1}\x00"

        result = re.sub(r"`[^`\n]+`", _save_code, result)

        # --- 3. Convert markdown formatting to WhatsApp syntax ---
        # Italic: standard Markdown *text* → WhatsApp _text_.  Do this before
        # bold conversion so **bold** does not become italic by accident.  The
        # lookarounds avoid list bullets and bold delimiters.
        result = re.sub(
            r"(?<!\*)\*(?!\s|\*)([^*\n]*?\S[^*\n]*?)\*(?!\*)",
            r"_\1_",
            result,
        )
        # Bold: **text** or __text__ → *text*
        result = re.sub(r"\*\*(.+?)\*\*", r"*\1*", result)
        result = re.sub(r"__(.+?)__", r"*\1*", result)
        # Strikethrough: ~~text~~ → ~text~
        result = re.sub(r"~~(.+?)~~", r"~\1~", result)
        # _text_ is already WhatsApp italic — leave as-is

        # --- 4. Convert markdown headers to bold text ---
        # # Header → *Header*. Strip any *...* wrapping already produced
        # by step 3 (e.g. "# **Title**" → "*Title*", not "**Title**",
        # which WhatsApp renders with literal asterisks).
        def _header_to_bold(m: re.Match) -> str:
            inner = m.group(1).strip()
            while len(inner) > 1 and inner.startswith("*") and inner.endswith("*"):
                inner = inner[1:-1].strip()
            return f"*{inner}*"

        result = re.sub(
            r"^#{1,6}\s+(.+)$", _header_to_bold, result, flags=re.MULTILINE
        )

        # --- 5. Convert markdown links: [text](url) → text (url) ---
        result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", result)

        # --- 6. Restore protected sections ---
        for i, fence in enumerate(fences):
            result = result.replace(f"{_FENCE_PH}{i}\x00", fence)
        for i, code in enumerate(codes):
            result = result.replace(f"{_CODE_PH}{i}\x00", code)

        return result


# ---------------------------------------------------------------------------
# Shared bridge directory resolution for adapter, pairing, CLI, and doctor
# ---------------------------------------------------------------------------

_RUNTIME_INVENTORY_FIELD = "hermesRuntimeFiles"
_DEPENDENCY_STAMP = ".hermes-pkg-hash"
_FRAMED_HASH_DOMAIN = b"hermes-whatsapp-framed-files-v1\0"
_MANIFEST_EDGE_WHITESPACE = frozenset(
    "\u0009\u000a\u000b\u000c\u000d\u001c\u001d\u001e\u001f\u0020"
    "\u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
# The OS lock serializes independent Hermes processes; this RLock also keeps
# threads in one process from racing (POSIX advisory locks are process-scoped).
_WHATSAPP_BRIDGE_RUNTIME_LOCK = threading.RLock()
# ``None`` derives the wait from the configured npm timeout. Tests and
# embedders may override this with a finite number for deterministic behavior.
_WHATSAPP_BRIDGE_LOCK_TIMEOUT_SECONDS: Optional[float] = None
_WHATSAPP_BRIDGE_LOCK_POLL_SECONDS = 0.05


def _bridge_package_candidates() -> tuple[Path, ...]:
    """Return source/editable and wheel-data bridge candidates in priority order."""
    source_tree_bridge = (
        Path(__file__).resolve().parents[2] / "scripts" / "whatsapp-bridge"
    )
    data_root = sysconfig.get_path("data")
    if not data_root:
        return (source_tree_bridge,)
    return (
        source_tree_bridge,
        Path(data_root) / "scripts" / "whatsapp-bridge",
    )


def _read_bridge_package(bridge_dir: Path) -> Optional[dict[str, Any]]:
    try:
        package = json.loads(
            (Path(bridge_dir) / "package.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError):
        return None
    return package if isinstance(package, dict) else None


def _validated_runtime_files(package: Any) -> tuple[str, ...]:
    """Validate and return the manifest-owned, traversal-safe runtime order."""
    if not isinstance(package, dict):
        return ()
    raw_files = package.get(_RUNTIME_INVENTORY_FIELD)
    if not isinstance(raw_files, list) or not raw_files:
        return ()

    files: list[str] = []
    seen: set[str] = set()
    for value in raw_files:
        if not isinstance(value, str):
            return ()
        name = value
        if (
            not name
            or name[0] in _MANIFEST_EDGE_WHITESPACE
            or name[-1] in _MANIFEST_EDGE_WHITESPACE
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
            or "\x00" in name
            or Path(name).is_absolute()
            or name in seen
        ):
            return ()
        seen.add(name)
        files.append(name)

    # These are structural requirements, not a second inventory. Imported
    # helpers remain declared only by hermesRuntimeFiles in package.json.
    if not {"bridge.js", "package.json", "package-lock.json"}.issubset(seen):
        return ()
    return tuple(files)


def _bridge_runtime_files(bridge_dir: Path) -> Optional[tuple[str, ...]]:
    """Return managed inventory, ``()`` if invalid, or ``None`` for legacy."""
    package = _read_bridge_package(Path(bridge_dir))
    if package is None or _RUNTIME_INVENTORY_FIELD not in package:
        return None
    return _validated_runtime_files(package)


def _canonical_runtime_files() -> tuple[str, ...]:
    for candidate in _bridge_package_candidates():
        runtime_files = _bridge_runtime_files(candidate)
        if runtime_files:
            return runtime_files
    return ()


# The production inventory is loaded from package.json. This compatibility
# export lets existing Python callers/tests inspect it without becoming a
# second source of truth.
WHATSAPP_BRIDGE_RUNTIME_FILES = _canonical_runtime_files()

# Project-owned native tests ship in release artifacts, but are not executable
# runtime inputs and must not be mistaken for user-owned state during refresh.
WHATSAPP_BRIDGE_PROJECT_TEST_FILES = (
    "allowlist.test.mjs",
    "bridge.native.test.mjs",
    "bridge.reconnect.test.mjs",
    "bridge.sendqueue.test.mjs",
    "outbound_ids.test.mjs",
    "owner_message_gate.test.mjs",
)


def _file_content_hash(path: Path) -> str:
    """Return the first 16 hex chars of a file's SHA-256, or ``""``."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def _framed_files_hash(
    directory: Path,
    filenames: tuple[str, ...],
    *,
    truncate: Optional[int] = None,
) -> str:
    """Hash canonical filenames and bytes with unambiguous length framing."""
    try:
        digest = hashlib.sha256(_FRAMED_HASH_DOMAIN)
        for name in filenames:
            name_bytes = name.encode("utf-8")
            content = (Path(directory) / name).read_bytes()
            digest.update(len(name_bytes).to_bytes(4, "big"))
            digest.update(name_bytes)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
        value = digest.hexdigest()
        return value[:truncate] if truncate is not None else value
    except (OSError, UnicodeError, OverflowError):
        return ""


def whatsapp_bridge_source_hash(path: Path) -> str:
    """Hash the complete managed runtime, with a legacy custom-file fallback.

    A directory whose package.json declares ``hermesRuntimeFiles`` is managed:
    every declared file must be readable and participates in manifest order.
    A bridge without that metadata retains the historical bridge.js plus
    optional bridge_helpers.js handshake used by custom single-file bridges.
    """
    path = Path(path)
    package_path = path.parent / "package.json"
    package = _read_bridge_package(path.parent)
    if package is None and package_path.exists():
        # A present package.json that cannot be parsed as an object is not a
        # legacy custom bridge; treating it as one would bypass manifest
        # validation and let malformed managed runtimes hash as healthy.
        return ""
    runtime_files = _bridge_runtime_files(path.parent)
    if runtime_files is None and package is None and WHATSAPP_BRIDGE_RUNTIME_FILES:
        managed_markers = set(WHATSAPP_BRIDGE_RUNTIME_FILES) - {
            "bridge.js",
            "bridge_helpers.js",
            "package.json",
        }
        if path.name == "bridge.js" and any(
            (path.parent / name).exists() for name in managed_markers
        ):
            # A damaged managed runtime may have lost package.json itself.
            # Reuse the packaged manifest-derived inventory so the missing
            # file makes the hash invalid instead of masquerading as custom.
            runtime_files = WHATSAPP_BRIDGE_RUNTIME_FILES
    if runtime_files is not None:
        if not runtime_files:
            return ""
        return _framed_files_hash(path.parent, runtime_files, truncate=16)

    helper_path = path.with_name("bridge_helpers.js")
    if helper_path.is_file():
        return _framed_files_hash(
            path.parent, (path.name, helper_path.name), truncate=16
        )
    # Preserve the legacy hash for truly single-file custom bridges. There is
    # no file-boundary ambiguity when exactly one byte string participates.
    return _file_content_hash(path)


def whatsapp_bridge_dependency_fingerprint(bridge_dir: Path) -> str:
    """Hash dependency manifests with the same canonical framing as Node."""
    return _framed_files_hash(
        Path(bridge_dir), ("package.json", "package-lock.json")
    )


class WhatsAppBridgeDependencyError(RuntimeError):
    """A deterministic WhatsApp dependency transaction could not complete."""


class WhatsAppBridgeBusyError(WhatsAppBridgeDependencyError):
    """Another process retained the bounded bridge transaction lock."""


class WhatsAppBridgeUnavailableError(WhatsAppBridgeDependencyError):
    """A required local executable is unavailable without a PATH fallback."""


class WhatsAppBridgeStateError(WhatsAppBridgeDependencyError):
    """Persistent bridge state could not be copied without following links."""


def _is_windows_reparse_point(metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _canonical_bridge_identity(bridge_dir: Path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(bridge_dir)))


def _whatsapp_bridge_lock_root() -> Path:
    """Return a canonical private lock root outside the replaceable runtime."""
    from hermes_constants import get_hermes_home

    hermes_home = Path(os.path.realpath(os.fspath(get_hermes_home())))
    return hermes_home / ".whatsapp-bridge-locks"


def _whatsapp_bridge_transaction_lock_path(bridge_dir: Path) -> Path:
    """Return a stable Hermes-home lock path outside the replaceable runtime."""
    identity = hashlib.sha256(
        os.fsencode(_canonical_bridge_identity(Path(bridge_dir)))
    ).hexdigest()
    return _whatsapp_bridge_lock_root() / f"{identity}.transaction.lock"


def _validate_private_lock_root(lock_root: Path) -> None:
    """Create a 0700 real directory and reject aliases/reparse points."""
    try:
        os.makedirs(lock_root, mode=0o700, exist_ok=True)
        metadata = lock_root.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or _is_windows_reparse_point(metadata)
        ):
            raise OSError(errno.ELOOP, "lock root is a symlink or reparse point")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise OSError(errno.EACCES, "lock root is not owned by the current user")
        if os.name != "nt":
            os.chmod(lock_root, 0o700)
            metadata = lock_root.lstat()
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise OSError(errno.EACCES, "lock root is accessible by other users")
    except OSError as exc:
        detail = _bounded_redacted_dependency_output(str(exc))
        raise WhatsAppBridgeDependencyError(
            f"Could not secure the WhatsApp bridge lock directory: {detail}"
        ) from exc


def _validate_lock_file_metadata(
    path_metadata: os.stat_result,
    descriptor_metadata: os.stat_result,
) -> None:
    if (
        not stat.S_ISREG(path_metadata.st_mode)
        or not stat.S_ISREG(descriptor_metadata.st_mode)
        or stat.S_ISLNK(path_metadata.st_mode)
        or _is_windows_reparse_point(path_metadata)
        or _is_windows_reparse_point(descriptor_metadata)
        or not _same_file_identity(path_metadata, descriptor_metadata)
    ):
        raise OSError(errno.ELOOP, "lock path is not the opened regular file")
    if hasattr(os, "getuid") and descriptor_metadata.st_uid != os.getuid():
        raise OSError(errno.EACCES, "lock file is not owned by the current user")
    if os.name != "nt" and stat.S_IMODE(descriptor_metadata.st_mode) & 0o077:
        raise OSError(errno.EACCES, "lock file is accessible by other users")


def _secure_open_whatsapp_bridge_lock(lock_path: Path) -> BinaryIO:
    """Open the stable lock inode without following attacker-planted links."""
    _validate_private_lock_root(lock_path.parent)
    before: Optional[os.stat_result]
    try:
        before = lock_path.lstat()
    except FileNotFoundError:
        before = None
    if before is not None and (
        stat.S_ISLNK(before.st_mode) or _is_windows_reparse_point(before)
    ):
        raise WhatsAppBridgeDependencyError(
            "Could not open the WhatsApp bridge transaction lock: the lock "
            "path is a symlink or reparse point."
        )

    flags = os.O_CREAT | os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        path_metadata = lock_path.lstat()
        descriptor_metadata = os.fstat(descriptor)
        _validate_lock_file_metadata(path_metadata, descriptor_metadata)
        if before is not None and not _same_file_identity(before, path_metadata):
            raise OSError(errno.EAGAIN, "lock path changed while it was opened")
        return os.fdopen(descriptor, "r+b", buffering=0)
    except OSError as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        detail = _bounded_redacted_dependency_output(str(exc))
        raise WhatsAppBridgeDependencyError(
            f"Could not open the WhatsApp bridge transaction lock: {detail}"
        ) from exc


def _uses_windows_file_locking() -> bool:
    return os.name == "nt"


def _try_acquire_whatsapp_bridge_file_lock(lock_file) -> None:
    """Attempt one non-blocking, portable advisory lock acquisition."""
    lock_file.seek(0)
    if _uses_windows_file_locking():
        if _msvcrt is None:
            raise OSError(errno.ENOSYS, "Windows file locking is unavailable")
        _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_NBLCK, 1)
    else:
        if _fcntl is None:
            raise OSError(errno.ENOSYS, "POSIX file locking is unavailable")
        _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)


def _release_whatsapp_bridge_file_lock(lock_file) -> None:
    lock_file.seek(0)
    if _uses_windows_file_locking():
        if _msvcrt is None:
            raise OSError(errno.ENOSYS, "Windows file locking is unavailable")
        _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_UNLCK, 1)
    else:
        if _fcntl is None:
            raise OSError(errno.ENOSYS, "POSIX file locking is unavailable")
        _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)


def _whatsapp_bridge_lock_is_busy(exc: OSError) -> bool:
    return (
        isinstance(exc, BlockingIOError)
        or exc.errno in {errno.EACCES, errno.EAGAIN}
        or getattr(exc, "winerror", None) in {33, 36}
    )


@contextmanager
def _exclusive_whatsapp_bridge_transaction(
    bridge_dir: Path,
    *,
    timeout: Optional[float] = None,
):
    """Fail-closed cross-process lock for one replaceable bridge directory.

    The lock file lives beside ``HERMES_HOME``, never inside ``bridge_dir``,
    and is deliberately retained: unlinking an advisory lock file allows a new
    process to lock a different inode while an existing owner still holds the
    old one.
    """
    lock_path = _whatsapp_bridge_transaction_lock_path(bridge_dir)
    if timeout is None:
        configured_timeout = _WHATSAPP_BRIDGE_LOCK_TIMEOUT_SECONDS
        timeout = (
            configured_timeout
            if configured_timeout is not None
            else max(30, env_int("WHATSAPP_NPM_INSTALL_TIMEOUT", 300) + 30)
        )
    try:
        timeout = max(0.0, float(timeout))
    except (TypeError, ValueError, OverflowError) as exc:
        raise WhatsAppBridgeDependencyError(
            "WhatsApp bridge lock timeout must be a finite number."
        ) from exc
    if timeout != timeout or timeout == float("inf"):
        raise WhatsAppBridgeDependencyError(
            "WhatsApp bridge lock timeout must be bounded."
        ) from None

    lock_file = _secure_open_whatsapp_bridge_lock(lock_path)
    try:
        if os.fstat(lock_file.fileno()).st_size == 0:
            lock_file.write(b"\0")
            lock_file.flush()
    except OSError as exc:
        try:
            lock_file.close()
        except OSError:
            pass
        detail = _bounded_redacted_dependency_output(str(exc))
        raise WhatsAppBridgeDependencyError(
            f"Could not initialize the WhatsApp bridge transaction lock: {detail}"
        ) from exc

    acquired = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                _try_acquire_whatsapp_bridge_file_lock(lock_file)
                acquired = True
                break
            except OSError as exc:
                if not _whatsapp_bridge_lock_is_busy(exc):
                    detail = _bounded_redacted_dependency_output(str(exc))
                    raise WhatsAppBridgeDependencyError(
                        f"Could not lock the WhatsApp bridge runtime: {detail}"
                    ) from exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WhatsAppBridgeBusyError(
                        "Timed out waiting for another Hermes process to finish "
                        "the WhatsApp bridge transaction."
                    ) from exc
                time.sleep(min(_WHATSAPP_BRIDGE_LOCK_POLL_SECONDS, remaining))
        yield
    finally:
        cleanup_errors: list[str] = []
        if acquired:
            try:
                _release_whatsapp_bridge_file_lock(lock_file)
            except OSError as exc:
                cleanup_errors.append(str(exc))
        try:
            lock_file.close()
        except OSError as exc:
            cleanup_errors.append(str(exc))
        if cleanup_errors:
            detail = _bounded_redacted_dependency_output("; ".join(cleanup_errors))
            logger.warning(
                "[whatsapp] WhatsApp bridge lock release needed descriptor-close "
                "fallback: %s",
                detail,
            )


def _bounded_redacted_dependency_output(output: str) -> str:
    """Return a short diagnostic without credentials or registry URLs."""
    text = str(output or "").strip()
    if not text:
        return "no output"
    try:
        from agent.redact import redact_sensitive_text

        text = redact_sensitive_text(text, force=True)
    except Exception:
        pass
    text = re.sub(r"(?i)\bhttps?://\S+", "<redacted-url>", text)
    bounded = "\n".join(text.splitlines()[-10:])
    if len(bounded) > 1200:
        bounded = bounded[-1200:]
    return bounded or "no output"


def _bridge_package_version(bridge_dir: Path) -> str:
    """Return the package.json version, or an empty string for invalid input."""
    package = _read_bridge_package(Path(bridge_dir))
    if package is None:
        return ""
    version = package.get("version")
    return str(version).strip() if version is not None else ""


def _bridge_runtime_signature(bridge_dir: Path) -> tuple[str, ...]:
    """Fingerprint a complete manifest-managed runtime."""
    runtime_files = _bridge_runtime_files(Path(bridge_dir))
    version = _bridge_package_version(bridge_dir)
    source_hash = whatsapp_bridge_source_hash(Path(bridge_dir) / "bridge.js")
    if not runtime_files or not version or not source_hash:
        return ()
    return (version, source_hash)


def _bridge_dir_is_writable(bridge_dir: Path) -> bool:
    """Probe whether npm may safely mutate *bridge_dir*."""
    if not bridge_dir.is_dir():
        return False
    probe = bridge_dir / f".hermes-write-test-{uuid.uuid4().hex}"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _path_exists_without_following(path: Path) -> bool:
    return os.path.lexists(path)


def _remove_path_without_following(path: Path) -> None:
    if not _path_exists_without_following(path):
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def _validate_state_directory(
    path: Path,
    *,
    expected: Optional[os.stat_result] = None,
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise WhatsAppBridgeStateError(
            f"Could not inspect persistent WhatsApp state: "
            f"{_bounded_redacted_dependency_output(str(exc))}"
        ) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or _is_windows_reparse_point(metadata)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise WhatsAppBridgeStateError(
            "Persistent WhatsApp state contains a symlink, junction, or reparse "
            "point where a real directory is required."
        )
    if expected is not None and not _same_file_identity(expected, metadata):
        raise WhatsAppBridgeStateError(
            "Persistent WhatsApp state changed while it was being copied."
        )
    return metadata


def _secure_state_directory_entries(
    directory: Path,
    expected: os.stat_result,
) -> list[tuple[str, os.stat_result]]:
    """Snapshot one real directory without following a replacement symlink."""
    directory_flags = getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if directory_flags and os.scandir in os.supports_fd:
        descriptor: Optional[int] = None
        try:
            descriptor = os.open(
                directory,
                os.O_RDONLY | directory_flags | nofollow | getattr(os, "O_CLOEXEC", 0),
            )
            opened = os.fstat(descriptor)
            if (
                not _same_file_identity(expected, opened)
                or not stat.S_ISDIR(opened.st_mode)
                or _is_windows_reparse_point(opened)
            ):
                raise WhatsAppBridgeStateError(
                    "Persistent WhatsApp state changed while it was being copied."
                )
            with os.scandir(descriptor) as entries:
                return [
                    (entry.name, entry.stat(follow_symlinks=False))
                    for entry in entries
                ]
        except WhatsAppBridgeStateError:
            raise
        except OSError as exc:
            raise WhatsAppBridgeStateError(
                f"Could not enumerate persistent WhatsApp state: "
                f"{_bounded_redacted_dependency_output(str(exc))}"
            ) from exc
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    try:
        entries = [
            (entry.name, entry.stat(follow_symlinks=False))
            for entry in os.scandir(directory)
        ]
    except OSError as exc:
        raise WhatsAppBridgeStateError(
            f"Could not enumerate persistent WhatsApp state: "
            f"{_bounded_redacted_dependency_output(str(exc))}"
        ) from exc
    _validate_state_directory(directory, expected=expected)
    return entries


def _copy_state_file(source: Path, destination: Path, *, overwrite: bool) -> None:
    source_metadata = source.lstat()
    if not stat.S_ISREG(source_metadata.st_mode):
        raise WhatsAppBridgeStateError(
            "Persistent WhatsApp state changed file type while it was being copied."
        )
    source_descriptor: Optional[int] = None
    temporary = destination.with_name(
        f".{destination.name}.state-copy-{uuid.uuid4().hex}"
    )
    try:
        source_descriptor = os.open(
            source,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        opened_metadata = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(opened_metadata.st_mode)
            or _is_windows_reparse_point(opened_metadata)
            or not _same_file_identity(source_metadata, opened_metadata)
        ):
            raise WhatsAppBridgeStateError(
                "Persistent WhatsApp state changed while a file was opened."
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(source_descriptor, "rb") as source_file:
            source_descriptor = None
            with temporary.open("xb") as destination_file:
                shutil.copyfileobj(source_file, destination_file)
            final_metadata = os.fstat(source_file.fileno())
        if (
            not _same_file_identity(opened_metadata, final_metadata)
            or opened_metadata.st_size != final_metadata.st_size
            or opened_metadata.st_mtime_ns != final_metadata.st_mtime_ns
        ):
            raise WhatsAppBridgeStateError(
                "Persistent WhatsApp state changed while a file was copied."
            )

        if overwrite:
            if _path_exists_without_following(destination) and destination.is_dir():
                _remove_path_without_following(destination)
            os.replace(temporary, destination)
        else:
            try:
                os.link(temporary, destination, follow_symlinks=False)
            except FileExistsError:
                pass
            finally:
                temporary.unlink(missing_ok=True)
    except WhatsAppBridgeStateError:
        raise
    except OSError as exc:
        raise WhatsAppBridgeStateError(
            f"Could not copy persistent WhatsApp state: "
            f"{_bounded_redacted_dependency_output(str(exc))}"
        ) from exc
    finally:
        if source_descriptor is not None:
            try:
                os.close(source_descriptor)
            except OSError:
                pass
        try:
            temporary.unlink(missing_ok=True)
        except OSError as cleanup_error:
            logger.warning(
                "[whatsapp] Could not remove a state-copy staging file at %s; "
                "it remains available for recovery: %s",
                temporary,
                _bounded_redacted_dependency_output(str(cleanup_error)),
            )


def _copy_persistent_bridge_state(
    source: Path,
    destination: Path,
    *,
    overwrite: bool = True,
) -> None:
    """Copy state without following roots/reparse points or clobbering live data."""
    source = Path(source)
    destination = Path(destination)
    if not _path_exists_without_following(source):
        return
    source_root_metadata = _validate_state_directory(source)
    source_runtime = _bridge_runtime_files(source) or ()
    replaceable = {
        *WHATSAPP_BRIDGE_RUNTIME_FILES,
        *source_runtime,
        *WHATSAPP_BRIDGE_PROJECT_TEST_FILES,
        "node_modules",
    }

    def copy_directory(
        current_source: Path,
        current_destination: Path,
        expected: os.stat_result,
        *,
        root: bool,
    ) -> None:
        current_metadata = _validate_state_directory(
            current_source, expected=expected
        )
        if _path_exists_without_following(current_destination):
            destination_metadata = current_destination.lstat()
            if (
                stat.S_ISLNK(destination_metadata.st_mode)
                or _is_windows_reparse_point(destination_metadata)
                or not stat.S_ISDIR(destination_metadata.st_mode)
            ):
                if not overwrite:
                    return
                _remove_path_without_following(current_destination)
        current_destination.mkdir(parents=True, exist_ok=True)

        for name, entry_metadata in _secure_state_directory_entries(
            current_source, current_metadata
        ):
            if root and name in replaceable:
                continue
            entry = current_source / name
            target = current_destination / name
            if _is_windows_reparse_point(entry_metadata):
                raise WhatsAppBridgeStateError(
                    "Persistent WhatsApp state contains an interior junction or "
                    "reparse point."
                )
            if stat.S_ISLNK(entry_metadata.st_mode):
                try:
                    link_target = os.readlink(entry)
                    if not _same_file_identity(entry_metadata, entry.lstat()):
                        raise WhatsAppBridgeStateError(
                            "Persistent WhatsApp state changed while a symlink "
                            "was copied."
                        )
                    if overwrite:
                        _remove_path_without_following(target)
                    os.symlink(link_target, target)
                except FileExistsError:
                    if overwrite:
                        raise
                continue
            if stat.S_ISDIR(entry_metadata.st_mode):
                copy_directory(
                    entry,
                    target,
                    entry_metadata,
                    root=False,
                )
                continue
            if stat.S_ISREG(entry_metadata.st_mode):
                if not _same_file_identity(entry_metadata, entry.lstat()):
                    raise WhatsAppBridgeStateError(
                        "Persistent WhatsApp state changed before a file was copied."
                    )
                _copy_state_file(entry, target, overwrite=overwrite)

        _validate_state_directory(current_source, expected=current_metadata)

    copy_directory(
        source,
        destination,
        source_root_metadata,
        root=True,
    )


def _copy_bundled_bridge_sources(source: Path, destination: Path) -> None:
    """Copy the complete manifest-owned executable bridge into staging."""
    runtime_files = _bridge_runtime_files(source)
    if not runtime_files:
        raise FileNotFoundError(
            f"bundled WhatsApp runtime has no valid {_RUNTIME_INVENTORY_FIELD}"
        )
    for name in runtime_files:
        source_file = source / name
        if not source_file.is_file():
            raise FileNotFoundError(
                f"bundled WhatsApp runtime is missing {source_file}"
            )
        shutil.copy2(source_file, destination / name)


def _whatsapp_bridge_dependencies_are_fresh(bridge_dir: Path, fingerprint: str) -> bool:
    node_modules = bridge_dir / "node_modules"
    if not fingerprint or not node_modules.is_dir():
        return False
    try:
        return (node_modules / _DEPENDENCY_STAMP).read_text(
            encoding="utf-8"
        ).strip() == fingerprint
    except OSError:
        return False


def _minimal_whatsapp_npm_environment() -> dict[str, str]:
    """Keep only process-launch, home, temp, and cache variables for npm."""
    allowed = {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "NPM_CONFIG_CACHE",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
        "XDG_CACHE_HOME",
    }
    base = {
        key.upper(): value
        for key, value in os.environ.items()
        if key.upper() in allowed
    }
    return with_hermes_node_path(base)


def _ensure_whatsapp_bridge_dependencies(bridge_dir: Path) -> bool:
    """Implement a staged node_modules replacement under the caller's lock."""
    bridge_dir = Path(bridge_dir)
    node_modules = bridge_dir / "node_modules"
    dependency_manifests = tuple(
        bridge_dir / name for name in ("package.json", "package-lock.json")
    )
    fingerprint = whatsapp_bridge_dependency_fingerprint(bridge_dir)
    if not fingerprint:
        if not any(path.is_file() for path in dependency_manifests) and node_modules.exists():
            # A legacy/custom single-file bridge can own a preinstalled
            # dependency tree without Hermes package metadata.
            return False
        raise WhatsAppBridgeDependencyError(
            "WhatsApp dependency manifests are missing or unreadable."
        )
    if _whatsapp_bridge_dependencies_are_fresh(bridge_dir, fingerprint):
        return False

    staging: Optional[Path] = None
    backup: Optional[Path] = None
    committed = False
    preserve_staging = False
    try:
        staging = Path(
            tempfile.mkdtemp(prefix=".node_modules.staging-", dir=bridge_dir)
        )
        for name in ("package.json", "package-lock.json"):
            shutil.copy2(bridge_dir / name, staging / name)

        npm_bin = find_node_executable("npm")
        if npm_bin is None:
            raise WhatsAppBridgeUnavailableError(
                "npm is unavailable; install or repair Node.js before preparing "
                "WhatsApp dependencies."
            )
        npm_install_timeout = env_int("WHATSAPP_NPM_INSTALL_TIMEOUT", 300)
        try:
            install_result = subprocess.run(
                [npm_bin, "ci", "--silent"],
                cwd=str(staging),
                capture_output=True,
                text=True,
                timeout=npm_install_timeout,
                env=_minimal_whatsapp_npm_environment(),
            )
        except subprocess.TimeoutExpired as exc:
            raise WhatsAppBridgeDependencyError(
                "WhatsApp dependency installation timed out."
            ) from exc
        except OSError as exc:
            detail = _bounded_redacted_dependency_output(str(exc))
            raise WhatsAppBridgeDependencyError(
                f"Could not run npm ci for WhatsApp: {detail}"
            ) from exc

        if install_result.returncode != 0:
            detail = _bounded_redacted_dependency_output(
                install_result.stderr or install_result.stdout or ""
            )
            raise WhatsAppBridgeDependencyError(
                f"npm ci failed for WhatsApp dependencies: {detail}"
            )

        staged_modules = staging / "node_modules"
        if not staged_modules.is_dir():
            raise WhatsAppBridgeDependencyError(
                "npm ci succeeded without creating node_modules."
            )
        if whatsapp_bridge_dependency_fingerprint(staging) != fingerprint:
            raise WhatsAppBridgeDependencyError(
                "Dependency manifests changed while npm ci was running."
            )
        if whatsapp_bridge_dependency_fingerprint(bridge_dir) != fingerprint:
            raise WhatsAppBridgeDependencyError(
                "Live dependency manifests changed while npm ci was running."
            )
        (staged_modules / _DEPENDENCY_STAMP).write_text(
            fingerprint, encoding="utf-8"
        )

        rejected_modules = staging / ".rejected-node_modules"

        def rollback_activation(reason: str, *, move_live: bool) -> None:
            nonlocal backup, committed, preserve_staging
            rollback_errors: list[str] = []
            if move_live and _path_exists_without_following(node_modules):
                try:
                    os.replace(node_modules, rejected_modules)
                except OSError as rollback_error:
                    rollback_errors.append(
                        "could not quarantine the promoted tree: "
                        + _bounded_redacted_dependency_output(str(rollback_error))
                    )
            if (
                backup is not None
                and _path_exists_without_following(backup)
                and not _path_exists_without_following(node_modules)
            ):
                try:
                    os.replace(backup, node_modules)
                    backup = None
                except OSError as rollback_error:
                    rollback_errors.append(
                        "could not restore the prior tree: "
                        + _bounded_redacted_dependency_output(str(rollback_error))
                    )
            committed = False
            if rollback_errors:
                preserve_staging = True
                recovery_paths = [
                    str(path)
                    for path in (backup, rejected_modules)
                    if path is not None and _path_exists_without_following(path)
                ]
                recovery_detail = ", ".join(recovery_paths) or "live node_modules"
                raise WhatsAppBridgeDependencyError(
                    f"{reason} Rollback also failed ({'; '.join(rollback_errors)}). "
                    f"Recovery data was preserved at: {recovery_detail}."
                )
            restored = "the prior tree was restored" if backup is None else (
                "the mismatched tree was removed"
            )
            raise WhatsAppBridgeDependencyError(f"{reason} {restored}.")

        if _path_exists_without_following(node_modules):
            backup = bridge_dir / f".node_modules.backup-{uuid.uuid4().hex}"
            os.replace(node_modules, backup)
        try:
            os.replace(staged_modules, node_modules)
            committed = True
        except Exception as promotion_error:
            try:
                rollback_activation(
                    "Could not activate WhatsApp dependencies "
                    f"({_bounded_redacted_dependency_output(str(promotion_error))});",
                    move_live=False,
                )
            except WhatsAppBridgeDependencyError as rollback_failure:
                raise rollback_failure from promotion_error

        post_staging_fingerprint = whatsapp_bridge_dependency_fingerprint(staging)
        post_live_fingerprint = whatsapp_bridge_dependency_fingerprint(bridge_dir)
        if (
            post_staging_fingerprint != fingerprint
            or post_live_fingerprint != fingerprint
            or not _whatsapp_bridge_dependencies_are_fresh(
                bridge_dir, fingerprint
            )
        ):
            rollback_activation(
                "Dependency manifests or their fingerprint changed after promotion;",
                move_live=True,
            )

        if backup is not None and _path_exists_without_following(backup):
            try:
                _remove_path_without_following(backup)
            except Exception as cleanup_error:
                detail = _bounded_redacted_dependency_output(str(cleanup_error))
                logger.warning(
                    "[whatsapp] Installed dependencies but could not remove "
                    "the old node_modules backup at %s: %s",
                    backup,
                    detail,
                )
            else:
                backup = None
        return True
    except WhatsAppBridgeDependencyError:
        raise
    except Exception as exc:
        detail = _bounded_redacted_dependency_output(str(exc))
        raise WhatsAppBridgeDependencyError(
            f"WhatsApp dependency transaction failed: {detail}"
        ) from exc
    finally:
        if (
            staging is not None
            and _path_exists_without_following(staging)
            and not preserve_staging
        ):
            try:
                _remove_path_without_following(staging)
            except Exception as cleanup_error:
                # Cleanup is subordinate to the transaction outcome. Before
                # activation an already-raised DependencyError remains primary;
                # after activation cleanup cannot turn committed success into a
                # new, untyped failure.
                detail = _bounded_redacted_dependency_output(str(cleanup_error))
                phase = "after activation" if committed else "after failure"
                logger.warning(
                    "[whatsapp] Could not remove dependency staging %s: %s",
                    phase,
                    detail,
                )
        # A backup left after a failed rollback is the last known dependency
        # tree. Never remove it automatically.


def ensure_whatsapp_bridge_dependencies(bridge_dir: Path) -> bool:
    """Ensure exact lockfile dependencies without mutating the live tree.

    Returns ``True`` when a staged tree was promoted and ``False`` when the
    existing fingerprint stamp was already current.
    """
    bridge_dir = Path(bridge_dir)
    with _WHATSAPP_BRIDGE_RUNTIME_LOCK:
        with _exclusive_whatsapp_bridge_transaction(bridge_dir):
            return _ensure_whatsapp_bridge_dependencies(bridge_dir)


def _bundled_whatsapp_bridge_dir() -> Path:
    """Find bridge data in a source tree/editable install or wheel data scheme."""
    candidates = _bridge_package_candidates()
    return next(
        (
            candidate
            for candidate in candidates
            if (candidate / "bridge.js").is_file()
        ),
        candidates[0],
    )


def _resolve_whatsapp_bridge_dir(
    bundled_bridge: Optional[Path] = None,
    persistent_bridge: Optional[Path] = None,
    *,
    install_writable: Optional[bool] = None,
) -> Path:
    """Resolve and transactionally refresh the writable WhatsApp runtime.

    Writable source checkouts run directly from their bundled runtime. For a
    read-only wheel/container install, a changed bundle is copied to a sibling
    staging directory and ``npm ci`` is completed there before the live mirror
    is touched. Promotion uses rename-with-backup; a failed promotion restores
    the prior functional directory. Failed installs likewise leave old sources
    and dependencies intact. Unknown files (including custom auth/session
    state) are copied through the staged replacement.

    The optional path and writability arguments make the filesystem operation
    directly testable; ordinary callers should call this function with no
    arguments. This function is the sole resolver used by the adapter,
    dashboard pairing, CLI onboarding, and doctor.
    """
    if bundled_bridge is None:
        bundled_bridge = _bundled_whatsapp_bridge_dir()
    else:
        bundled_bridge = Path(bundled_bridge)

    if persistent_bridge is None:
        from hermes_constants import get_hermes_home

        persistent_bridge = get_hermes_home() / "scripts" / "whatsapp-bridge"
    else:
        persistent_bridge = Path(persistent_bridge)

    if install_writable is None:
        install_writable = _bridge_dir_is_writable(bundled_bridge)
    if install_writable:
        return bundled_bridge

    if _path_exists_without_following(persistent_bridge):
        persistent_metadata = persistent_bridge.lstat()
        if (
            stat.S_ISLNK(persistent_metadata.st_mode)
            or _is_windows_reparse_point(persistent_metadata)
            or not stat.S_ISDIR(persistent_metadata.st_mode)
        ):
            raise WhatsAppBridgeStateError(
                "The persistent WhatsApp bridge root must be a real directory, "
                "not a symlink, junction, reparse point, or file."
            )

    bundled_signature = _bridge_runtime_signature(bundled_bridge)
    bundled_dependency_fingerprint = whatsapp_bridge_dependency_fingerprint(
        bundled_bridge
    )
    if not bundled_signature or not bundled_dependency_fingerprint:
        fallback = (
            persistent_bridge if persistent_bridge.is_dir() else bundled_bridge
        )
        logger.warning(
            "[whatsapp] Bundled bridge runtime at %s is incomplete; using %s "
            "without updating.",
            bundled_bridge,
            fallback,
        )
        return fallback

    if (
        persistent_bridge.is_dir()
        and _bridge_runtime_signature(persistent_bridge) == bundled_signature
    ):
        return persistent_bridge

    staging: Optional[Path] = None
    backup: Optional[Path] = None
    had_persistent_runtime = persistent_bridge.is_dir()
    preserve_staging = False
    promoted = False

    try:
        persistent_bridge.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{persistent_bridge.name}.staging-",
                dir=persistent_bridge.parent,
            )
        )
        # Preserve custom auth/session state in the candidate, but deliberately
        # exclude old project sources, native tests, and node_modules.
        if had_persistent_runtime:
            _copy_persistent_bridge_state(persistent_bridge, staging)
        _copy_bundled_bridge_sources(bundled_bridge, staging)

        # The public installer acquires the same cross-process transaction
        # lock. The resolver already owns that lock for the live target, so use
        # the unlocked implementation for its private staging directory.
        _ensure_whatsapp_bridge_dependencies(staging)
        if _bridge_runtime_signature(staging) != bundled_signature:
            raise RuntimeError("staged bridge sources changed while npm ci ran")

        # Capture user state once more immediately before promotion in case a
        # pairing process wrote it while npm ci was running.
        if had_persistent_runtime:
            _copy_persistent_bridge_state(persistent_bridge, staging)

        if had_persistent_runtime:
            backup = persistent_bridge.with_name(
                f".{persistent_bridge.name}.backup-{uuid.uuid4().hex}"
            )
            os.replace(persistent_bridge, backup)
        try:
            os.replace(staging, persistent_bridge)
            promoted = True
        except Exception as promotion_error:
            raise RuntimeError(
                f"bridge promotion failed: {promotion_error}"
            ) from promotion_error

        # Promotion is not the commit point: verify the live manifests, runtime
        # hash, dependency stamp, and exact dependency fingerprint again before
        # discarding any prior runtime.
        if (
            _bridge_runtime_signature(persistent_bridge) != bundled_signature
            or whatsapp_bridge_dependency_fingerprint(persistent_bridge)
            != bundled_dependency_fingerprint
            or not _whatsapp_bridge_dependencies_are_fresh(
                persistent_bridge, bundled_dependency_fingerprint
            )
        ):
            raise RuntimeError(
                "promoted bridge runtime or dependency fingerprint changed "
                "before commit"
            )

        # Every supported production launch passes an explicit --session path
        # outside this runtime (adapter, CLI, and dashboard), and bridge media
        # writes use explicit cache env paths. A running official bridge therefore
        # has no mutable writer in the moved backup and must not be destructively
        # killed for an update. Unknown historical state is nevertheless merged
        # once, without overwriting files created or changed in the new live tree.
        if backup is not None and backup.exists():
            _copy_persistent_bridge_state(
                backup,
                persistent_bridge,
                overwrite=False,
            )
            try:
                _remove_path_without_following(backup)
            except Exception as cleanup_error:
                logger.warning(
                    "[whatsapp] Bridge update succeeded but the state-bearing "
                    "backup remains at %s because cleanup failed: %s",
                    backup,
                    _bounded_redacted_dependency_output(str(cleanup_error)),
                )
            else:
                backup = None
        logger.info(
            "[whatsapp] Updated persistent bridge runtime at %s from bundled "
            "version %s.",
            persistent_bridge,
            bundled_signature[0],
        )
        return persistent_bridge
    except Exception as exc:
        failure_detail = _bounded_redacted_dependency_output(str(exc))
        rollback_detail: Optional[str] = None
        rolled_back = False
        restored_prior = False
        rollback_errors: list[str] = []
        if promoted and persistent_bridge.exists() and staging is not None:
            try:
                os.replace(persistent_bridge, staging)
                promoted = False
                rolled_back = True
            except OSError as rollback_error:
                rollback_errors.append(
                    "could not quarantine the promoted runtime: "
                    + _bounded_redacted_dependency_output(str(rollback_error))
                )
        if backup is not None and backup.exists():
            if persistent_bridge.exists():
                rollback_errors.append(
                    "could not restore the prior runtime because the live path "
                    "could not be vacated"
                )
            else:
                try:
                    os.replace(backup, persistent_bridge)
                    backup = None
                    rolled_back = True
                    restored_prior = True
                except OSError as rollback_error:
                    rollback_errors.append(
                        "could not restore the prior runtime: "
                        + _bounded_redacted_dependency_output(str(rollback_error))
                    )
        if rollback_errors:
            preserve_staging = True
            rollback_detail = "; ".join(rollback_errors)
            recovery_paths = [
                str(path)
                for path in (backup, staging, persistent_bridge)
                if path is not None and _path_exists_without_following(path)
            ]
            recovery_detail = ", ".join(recovery_paths) or "the bridge parent"
            logger.error(
                "[whatsapp] Bridge update failed (%s) and rollback also "
                "failed (%s). Recovery data was preserved at: %s",
                failure_detail,
                rollback_detail,
                recovery_detail,
            )
            raise WhatsAppBridgeStateError(
                f"Bridge update failed ({failure_detail}); rollback also failed "
                f"({rollback_detail}). Recovery data was preserved at: "
                f"{recovery_detail}."
            ) from exc

        fallback = (
            persistent_bridge if persistent_bridge.is_dir() else bundled_bridge
        )
        combined_failure = failure_detail
        if rolled_back:
            rollback_result = (
                "rolled back to the prior runtime"
                if restored_prior
                else "quarantined the uncommitted runtime"
            )
            combined_failure = f"{failure_detail}; {rollback_result}"
        if had_persistent_runtime:
            logger.warning(
                "[whatsapp] Bridge update failed (%s); keeping the existing "
                "functional persistent runtime at %s. The bundled update was "
                "not activated.",
                combined_failure,
                fallback,
            )
        else:
            logger.warning(
                "[whatsapp] Could not prepare persistent bridge runtime (%s); "
                "using bundled runtime at %s.",
                combined_failure,
                bundled_bridge,
            )
        return fallback
    finally:
        if (
            staging is not None
            and _path_exists_without_following(staging)
            and not preserve_staging
        ):
            try:
                _remove_path_without_following(staging)
            except Exception as cleanup_error:
                logger.warning(
                    "[whatsapp] Could not remove state-bearing bridge staging "
                    "at %s; it remains available for recovery: %s",
                    staging,
                    _bounded_redacted_dependency_output(str(cleanup_error)),
                )
        # A backup left after a failed rollback or cleanup contains the last
        # functional runtime and possibly auth state. Never delete it silently.


def resolve_whatsapp_bridge_dir(
    bundled_bridge: Optional[Path] = None,
    persistent_bridge: Optional[Path] = None,
    *,
    install_writable: Optional[bool] = None,
) -> Path:
    """Cross-process serialized wrapper around the transactional resolver."""
    if persistent_bridge is None:
        from hermes_constants import get_hermes_home

        persistent_bridge = get_hermes_home() / "scripts" / "whatsapp-bridge"
    else:
        persistent_bridge = Path(persistent_bridge)

    with _WHATSAPP_BRIDGE_RUNTIME_LOCK:
        with _exclusive_whatsapp_bridge_transaction(persistent_bridge):
            return _resolve_whatsapp_bridge_dir(
                bundled_bridge=bundled_bridge,
                persistent_bridge=persistent_bridge,
                install_writable=install_writable,
            )
