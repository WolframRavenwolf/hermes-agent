"""
Transport-agnostic WhatsApp behavior shared by the Baileys bridge adapter and the
Cloud API adapter: allow-list / DM / group gating, mention detection, quoted-reply-
to-bot detection, broadcast filtering, WhatsApp markdown conversion, chunk budgeting.

Mixin contract — the host adapter sets these on ``self`` before calling any mixin
method: ``config`` (PlatformConfig), ``name``, ``_dm_policy`` / ``_group_policy``
("open" | "allowlist" | "disabled"), ``_allow_from`` / ``_group_allow_from`` (set[str]),
``_mention_patterns`` (list[re.Pattern]), ``_reply_prefix`` (Optional[str]).
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
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Dict, Optional

try:  # pragma: no cover - platform-specific locking
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None
try:  # pragma: no cover - Windows
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX
    _msvcrt = None

from gateway.platforms._shared import get_scoped_secret as _get_wsecret


logger = logging.getLogger(__name__)

_TRUTHY = {"true", "1", "yes", "on"}
_OPTIN_TRUTHY = {"true", "1", "yes"}


def _stash(pattern: str, text: str, tag: str) -> tuple[str, list[str]]:
    """Replace every ``pattern`` match with a ``\\x00<tag><n>\\x00`` placeholder."""
    saved: list[str] = []

    def keep(m: re.Match) -> str:
        saved.append(m.group(0))
        return f"\x00{tag}{len(saved) - 1}\x00"

    return re.sub(pattern, keep, text), saved


def _header_to_bold(m: re.Match) -> str:
    """``# Header`` → ``*Header*``, stripping already-bolded ``*...*`` so ``# **Title**``
    doesn't render with literal asterisks."""
    inner = m.group(1).strip()
    while len(inner) > 1 and inner.startswith("*") and inner.endswith("*"):
        inner = inner[1:-1].strip()
    return f"*{inner}*"


class WhatsAppBehaviorMixin:
    """Shared behavior for all WhatsApp adapters (Baileys + Cloud API); owns no state
    of its own — see the module docstring for the host adapter's attribute contract."""

    # Practical UX limit, not the ~65K protocol max (long messages are unreadable on mobile).
    MAX_MESSAGE_LENGTH: int = 4096
    supports_code_blocks = True  # WhatsApp renders fenced code blocks (monospace)

    DEFAULT_REPLY_PREFIX: str = "⚕ *Hermes Agent*\n────────────\n"

    _OUTBOUND_INVISIBLE_CHARS_RE = re.compile(r"[\u200b\u2060\u2063\ufeff]")
    _OUTBOUND_ODD_SPACE_RE = re.compile(r"[\u00a0\u1680\u180e\u2000-\u200a\u202f\u205f\u3000]")

    @classmethod
    def _sanitize_outbound_text(cls, content: str) -> str:
        """Strip zero-width format chars (WORD JOINER etc.) and normalize odd unicode
        spaces — WhatsApp renders them as mojibake prefixes. Emoji joiners are kept."""
        if not content:
            return content
        return cls._OUTBOUND_ODD_SPACE_RE.sub(" ", cls._OUTBOUND_INVISIBLE_CHARS_RE.sub("", content))

    @property
    def enforces_own_access_policy(self) -> bool:
        """WhatsApp gates DM/group access at intake via dm_policy/group_policy."""
        return True

    def _effective_reply_prefix(self) -> str:
        """Prefix for outgoing replies in self-chat mode (Cloud API overrides to ``""``)."""
        if (_get_wsecret("WHATSAPP_MODE", default="self-chat") or "self-chat") != "self-chat":
            return ""
        if self._reply_prefix is not None:
            return self._reply_prefix.replace("\\n", "\n")
        env_prefix = _get_wsecret("WHATSAPP_REPLY_PREFIX")
        if env_prefix is not None:
            return env_prefix.replace("\\n", "\n")
        return self.DEFAULT_REPLY_PREFIX

    def _outgoing_chunk_limit(self) -> int:
        """Reserve room for the reply prefix; floor keeps space for pagination/fence repair."""
        return max(1024, self.MAX_MESSAGE_LENGTH - len(self._effective_reply_prefix()))

    def _whatsapp_require_mention(self) -> bool:
        configured = self.config.extra.get("require_mention")
        if configured is None:
            configured = _get_wsecret("WHATSAPP_REQUIRE_MENTION", default="false") or "false"
        if isinstance(configured, str):
            return configured.lower() in _TRUTHY
        return bool(configured)

    def _whatsapp_free_response_chats(self) -> set[str]:
        raw = self.config.extra.get("free_response_chats")
        if raw is None:
            raw = _get_wsecret("WHATSAPP_FREE_RESPONSE_CHATS", default="") or ""
        return self._coerce_allow_list(raw)

    @staticmethod
    def _coerce_allow_list(raw) -> set[str]:
        """Parse allow_from / group_allow_from from config (list) or env var (CSV)."""
        if raw is None:
            return set()
        parts = raw if isinstance(raw, list) else str(raw).split(",")
        return {str(part).strip() for part in parts if str(part).strip()}

    def _select_dm_allowlist(self, extra: Dict[str, Any], env_keys, read_env) -> Any:
        """Pick the raw DM allowlist by key *presence*: ``allow_from``/``allowFrom`` in config (an
        explicit empty list stays authoritative), then the first truthy env carrier. Records the
        winning source in ``_dm_allowlist_source`` so live DM checks keep the same precedence."""
        for key in ("allow_from", "allowFrom"):
            if key in extra:
                self._dm_allowlist_source = "config"
                return extra.get(key)
        for env in env_keys:
            if read_env(env):
                self._dm_allowlist_source = env
                return read_env(env)
        self._dm_allowlist_source = None
        return None

    def _live_dm_allow_from(self) -> set[str]:
        """Allowlist currently enforced for DM intake / strict DM auth. Env-seeded adapters re-read
        the same key so pairing approve/revoke takes effect without restart; a removed key (sole-entry
        revoke) means empty, not the construction snapshot. Config-seeded adapters keep the in-memory
        set (pairing revoke purges it in place) — a stale env value must not broaden access."""
        source = getattr(self, "_dm_allowlist_source", None)
        if isinstance(source, str) and source != "config":
            return self._coerce_allow_list(os.environ[source]) if source in os.environ else set()
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
        """Status updates (Stories) and Channel/Newsletter broadcasts — never reply
        (answering a Story spams the status feed; Channel posts aren't addressable)."""
        cid = (chat_id or "").strip().lower()
        return cid == "status@broadcast" or cid.endswith(("@broadcast", "@newsletter"))

    # ------------------------------------------------------------------ gating
    def _open_dm_opted_in(self) -> bool:
        if os.getenv("GATEWAY_ALLOW_ALL_USERS", "").lower() in _OPTIN_TRUTHY:
            return True
        return (_get_wsecret("WHATSAPP_ALLOW_ALL_USERS", default="") or "").lower() in _OPTIN_TRUTHY

    @staticmethod
    def _matches_whatsapp_allowlist(candidate: str, allow_from) -> bool:
        """Match a WhatsApp identifier against an allowlist across phone/LID forms. Inbound senders
        arrive as ``<id>@lid`` while allowlists hold phone numbers (or vice versa), so resolve both
        sides through the bridge's lid-mapping files via ``gateway.whatsapp_identity``."""
        if not allow_from:
            return False
        if candidate in allow_from:
            return True
        from gateway.whatsapp_identity import expand_whatsapp_aliases, normalize_whatsapp_identifier
        candidate_aliases = expand_whatsapp_aliases(candidate)
        if not candidate_aliases:
            return False
        return any(
            entry == "*"
            or normalize_whatsapp_identifier(entry) in candidate_aliases
            or expand_whatsapp_aliases(entry) & candidate_aliases
            for entry in allow_from
        )

    def _is_dm_allowed(self, sender_id: str) -> bool:
        """Strict DM authorization — pairing does not imply access."""
        if self._dm_policy == "allowlist":
            return self._matches_whatsapp_allowlist(sender_id, self._live_dm_allow_from())
        return self._dm_policy == "open" and self._open_dm_opted_in()

    def _is_dm_intake_allowed(self, sender_id: str) -> bool:
        """Whether a DM may reach the gateway intake (pairing handshake path)."""
        principal = str(sender_id or "").strip()
        if not principal:
            return False
        if self._dm_policy == "allowlist":
            return self._matches_whatsapp_allowlist(principal, self._live_dm_allow_from())
        if self._dm_policy == "pairing":
            return True
        return self._dm_policy == "open" and self._open_dm_opted_in()

    def _is_group_allowed(self, chat_id: str) -> bool:
        """Check whether a group chat should be processed."""
        if self._group_policy == "allowlist":
            return self._matches_whatsapp_allowlist(chat_id, self._group_allow_from)
        return self._group_policy == "open"

    def _compile_mention_patterns(self):
        patterns = self.config.extra.get("mention_patterns")
        if patterns is None:
            raw = (_get_wsecret("WHATSAPP_MENTION_PATTERNS", default="") or "").strip()
            if raw:
                try:
                    patterns = json.loads(raw)
                except Exception:
                    # Plain text: one pattern per line, else comma-separated.
                    patterns = [p.strip() for p in raw.splitlines() if p.strip()]
                    patterns = patterns or [p.strip() for p in raw.split(",") if p.strip()]
        if patterns is None:
            return []
        if isinstance(patterns, str):
            patterns = [patterns]
        if not isinstance(patterns, list):
            logger.warning("[%s] whatsapp mention_patterns must be a list or string; got %s", self.name, type(patterns).__name__)
            return []
        compiled = []
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            try:
                compiled.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                logger.warning("[%s] Invalid WhatsApp mention pattern %r: %s", self.name, pattern, exc)
        if compiled:
            logger.info("[%s] Loaded %d WhatsApp mention pattern(s)", self.name, len(compiled))
        return compiled

    def _bot_ids_from_message(self, data: Dict[str, Any]) -> set[str]:
        return {nid for c in (data.get("botIds") or []) if (nid := self._normalize_whatsapp_id(c))}

    def _message_is_reply_to_bot(self, data: Dict[str, Any]) -> bool:
        quoted_participant = self._normalize_whatsapp_id(data.get("quotedParticipant"))
        return bool(quoted_participant) and quoted_participant in self._bot_ids_from_message(data)

    def _message_mentions_bot(self, data: Dict[str, Any]) -> bool:
        bot_ids = self._bot_ids_from_message(data)
        if not bot_ids:
            return False
        mentioned = {nid for c in (data.get("mentionedIds") or []) if (nid := self._normalize_whatsapp_id(c))}
        if mentioned & bot_ids:
            return True
        lower_body = str(data.get("body") or "").lower()
        return any(
            bare and (f"@{bare}" in lower_body or bare in lower_body)
            for bare in (bot_id.split("@", 1)[0].lower() for bot_id in bot_ids)
        )

    def _message_matches_mention_patterns(self, data: Dict[str, Any]) -> bool:
        body = str(data.get("body") or "")
        return any(pattern.search(body) for pattern in self._mention_patterns or ())

    def _clean_bot_mention_text(self, text: str, data: Dict[str, Any]) -> str:
        if not text:
            return text
        cleaned = text
        for bot_id in self._bot_ids_from_message(data):
            bare_id = bot_id.split("@", 1)[0]
            if bare_id:
                cleaned = re.sub(rf"@{re.escape(bare_id)}\b[,:\-]*\s*", "", cleaned)
        return cleaned.strip() or text

    def _should_process_message(self, data: Dict[str, Any]) -> bool:
        chat_id = str(data.get("chatId") or "")
        # Broadcast pseudo-chats are filtered even in self-chat mode (fromMe events).
        if self._is_broadcast_chat(chat_id):
            return False
        if not data.get("isGroup", False):
            # DMs that pass the policy gate are always processed
            return self._is_dm_intake_allowed(str(data.get("senderId") or data.get("from") or ""))
        if not self._is_group_allowed(chat_id):
            return False
        # Group messages: check mention / free-response settings
        if chat_id in self._whatsapp_free_response_chats() or not self._whatsapp_require_mention():
            return True
        return (
            str(data.get("body") or "").strip().startswith("/")
            or self._message_is_reply_to_bot(data)
            or self._message_mentions_bot(data)
            or self._message_matches_mention_patterns(data)
        )

    # ------------------------------------------------------------------ formatting
    def format_message(self, content: str) -> str:
        """Convert markdown to WhatsApp syntax (*bold*, _italic_, ~strike~); fenced and
        inline code are protected via placeholder substitution."""
        if not content:
            return content
        result, fences = _stash(r"```[\s\S]*?```", self._sanitize_outbound_text(content), "FENCE")
        result, codes = _stash(r"`[^`\n]+`", result, "CODE")
        # Italic *text* → _text_ BEFORE bold so **bold** doesn't become italic;
        # lookarounds skip list bullets and bold delimiters.
        result = re.sub(r"(?<!\*)\*(?!\s|\*)([^*\n]*?\S[^*\n]*?)\*(?!\*)", r"_\1_", result)
        result = re.sub(r"\*\*(.+?)\*\*", r"*\1*", result)
        result = re.sub(r"__(.+?)__", r"*\1*", result)
        result = re.sub(r"~~(.+?)~~", r"~\1~", result)
        result = re.sub(r"^#{1,6}\s+(.+)$", _header_to_bold, result, flags=re.MULTILINE)
        result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", result)  # [text](url) → text (url)
        for tag, saved in (("FENCE", fences), ("CODE", codes)):
            for i, original in enumerate(saved):
                result = result.replace(f"\x00{tag}{i}\x00", original)
        return result


_WHATSAPP_DEPENDENCY_STAMP = ".hermes-pkg-hash"


def whatsapp_bridge_dependency_fingerprint(bridge_dir: Path) -> str:
    """Return a stable fingerprint for the checked-in bridge manifests."""
    digest = hashlib.sha256()
    for name in ("package.json", "package-lock.json"):
        path = bridge_dir / name
        try:
            content = path.read_bytes()
        except OSError:
            return ""
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def whatsapp_bridge_dependencies_fresh(bridge_dir: Path) -> bool:
    """Return whether installed bridge dependencies match both manifests."""
    node_modules = bridge_dir / "node_modules"
    fingerprint = whatsapp_bridge_dependency_fingerprint(bridge_dir)
    if not node_modules.is_dir() or not fingerprint:
        return False
    try:
        recorded = (node_modules / _WHATSAPP_DEPENDENCY_STAMP).read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return False
    return recorded == fingerprint


def record_whatsapp_bridge_dependency_fingerprint(bridge_dir: Path) -> bool:
    """Stamp a successful explicit install with its manifest fingerprint."""
    fingerprint = whatsapp_bridge_dependency_fingerprint(bridge_dir)
    if not fingerprint:
        return False
    try:
        (bridge_dir / "node_modules" / _WHATSAPP_DEPENDENCY_STAMP).write_text(
            fingerprint, encoding="utf-8"
        )
    except OSError:
        return False
    return True


_WHATSAPP_BRIDGE_LOCK_TIMEOUT_SECONDS: Optional[float] = None
_WHATSAPP_BRIDGE_LOCK_POLL_SECONDS = 0.05


class WhatsAppBridgeDependencyError(RuntimeError):
    """A deterministic WhatsApp dependency transaction could not complete."""


class WhatsAppBridgeBusyError(WhatsAppBridgeDependencyError):
    """Another process retained the bounded bridge transaction lock."""


class WhatsAppBridgeUnavailableError(WhatsAppBridgeDependencyError):
    """A required local executable is unavailable without a PATH fallback."""


def _is_windows_reparse_point(metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _canonical_bridge_identity(bridge_dir: Path) -> str:
    """Canonical location for bridge I/O, not a physical lock identity."""
    return os.path.normcase(os.path.realpath(os.fspath(bridge_dir)))


def _whatsapp_bridge_transaction_lock_path(bridge_dir: Path) -> Path:
    """One retained native-user lock, independent of profiles and target changes."""
    from hermes_constants import _get_platform_default_hermes_home

    return (
        _get_platform_default_hermes_home()
        / ".whatsapp-bridge-locks" / "transaction.lock"
    )


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
    """Fail-closed per-user maintenance lock; yield the validated target path.

    All explicit bridge maintenance for this native OS home serializes, including
    different profiles and targets. Retain the lock outside replaceable roots:
    unlinking it lets new callers lock a different inode than an existing owner.
    Cooperating callers must share the native home; this is not cross-user locking.
    """
    from utils import env_int

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
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                # A peer can lock its first byte before our initialization write.
                # Windows mandatory write/flush conflicts use the same bounded retry.
                if os.fstat(lock_file.fileno()).st_size == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                _try_acquire_whatsapp_bridge_file_lock(lock_file)
                acquired = True
                break
            except OSError as exc:
                if not _whatsapp_bridge_lock_is_busy(exc):
                    detail = _bounded_redacted_dependency_output(str(exc))
                    raise WhatsAppBridgeDependencyError(
                        f"Could not lock the WhatsApp bridge dependencies: {detail}"
                    ) from exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WhatsAppBridgeBusyError(
                        "Timed out waiting for another Hermes process to finish "
                        "the WhatsApp bridge transaction."
                    ) from exc
                time.sleep(min(_WHATSAPP_BRIDGE_LOCK_POLL_SECONDS, remaining))
        # Resolve and validate only after acquisition: the previous owner may
        # replace the bridge root, or create a previously absent mirror.
        canonical = Path(_canonical_bridge_identity(bridge_dir))
        try:
            metadata = canonical.lstat()
        except FileNotFoundError:
            pass  # The shared owner also covers creation of an absent target.
        else:
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or _is_windows_reparse_point(metadata)
                or metadata.st_dev < 0
                or metadata.st_ino <= 0
            ):
                raise WhatsAppBridgeDependencyError(
                    "WhatsApp bridge target has no usable physical directory identity."
                )
        yield canonical
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


def _path_exists_without_following(path: Path) -> bool:
    return os.path.lexists(path)


def _remove_path_without_following(path: Path) -> None:
    if not _path_exists_without_following(path):
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def _minimal_whatsapp_npm_environment(env: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Keep npm launch/build and transport settings without unrelated secrets."""
    from hermes_constants import get_hermes_home, with_hermes_node_path

    allowed = {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PYTHON",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
        "XDG_CACHE_HOME",
    }
    transport_allowed = {
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "NODE_EXTRA_CA_CERTS",
        "NPM_CONFIG_CA",
        "NPM_CONFIG_CACHE",
        "NPM_CONFIG_CAFILE",
        "NPM_CONFIG_HTTPS_PROXY",
        "NPM_CONFIG_NOPROXY",
        "NPM_CONFIG_PROXY",
        "NPM_CONFIG_REGISTRY",
        "NPM_CONFIG_STRICT_SSL",
        "NPM_CONFIG_USERCONFIG",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
    }
    # Preserve proxy/npm spelling and coexistence so their own precedence applies.
    base = {
        (key if key.upper() in transport_allowed else key.upper()): value
        for key, value in (os.environ if env is None else env).items()
        if key.upper() in allowed or key.upper() in transport_allowed
    }
    # Match the native npm lifecycle's profile npmrc fallback, but honor every
    # explicit userconfig spelling (including an intentionally empty value).
    if not any(key.upper() == "NPM_CONFIG_USERCONFIG" for key in base):
        npmrc = get_hermes_home() / "npmrc"
        if npmrc.is_file():
            base["NPM_CONFIG_USERCONFIG"] = os.fspath(npmrc)
    return with_hermes_node_path(base)


def _ensure_whatsapp_bridge_dependencies(
    bridge_dir: Path, *, npm: Optional[str], env: Optional[dict[str, str]]
) -> bool:
    """Implement a staged node_modules replacement under the caller's lock."""
    from hermes_constants import find_node_executable
    from hermes_cli._subprocess_compat import windows_hide_flags
    from utils import env_int

    bridge_dir = Path(bridge_dir)
    node_modules = bridge_dir / "node_modules"
    fingerprint = whatsapp_bridge_dependency_fingerprint(bridge_dir)
    if not fingerprint:
        raise WhatsAppBridgeDependencyError(
            "WhatsApp dependency manifests are missing or unreadable."
        )
    if whatsapp_bridge_dependencies_fresh(bridge_dir):
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

        npm_bin = npm if npm is not None else find_node_executable("npm")
        if not npm_bin:
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
                encoding="utf-8",
                errors="replace",
                creationflags=windows_hide_flags(),
                timeout=npm_install_timeout,
                env=_minimal_whatsapp_npm_environment(env),
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
        (staged_modules / _WHATSAPP_DEPENDENCY_STAMP).write_text(
            fingerprint, encoding="utf-8"
        )

        rejected_modules = staging / ".rejected-node_modules"

        def rollback_activation() -> None:
            nonlocal backup, committed
            committed = False
            rollback_errors: list[str] = []
            # A rename may succeed and then raise a control exception before a
            # Python flag is assigned. Reserved paths tell us which moves ran.
            if (
                not _path_exists_without_following(staged_modules)
                and _path_exists_without_following(node_modules)
            ):
                try:
                    os.replace(node_modules, rejected_modules)
                except BaseException as rollback_error:
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
                except BaseException as rollback_error:
                    rollback_errors.append(
                        "could not restore the prior tree: "
                        + _bounded_redacted_dependency_output(str(rollback_error))
                    )
            if rollback_errors:
                raise WhatsAppBridgeDependencyError("; ".join(rollback_errors))

        try:
            if _path_exists_without_following(node_modules):
                backup = bridge_dir / f".node_modules.backup-{uuid.uuid4().hex}"
                os.replace(node_modules, backup)
            os.replace(staged_modules, node_modules)
            if (
                whatsapp_bridge_dependency_fingerprint(staging) != fingerprint
                or whatsapp_bridge_dependency_fingerprint(bridge_dir) != fingerprint
                or not whatsapp_bridge_dependencies_fresh(bridge_dir)
            ):
                raise WhatsAppBridgeDependencyError(
                    "Dependency manifests or their fingerprint changed after promotion"
                )
            committed = True
        except BaseException as activation_error:
            reason = (
                "Could not activate WhatsApp dependencies "
                f"({_bounded_redacted_dependency_output(str(activation_error))});"
            )
            try:
                rollback_activation()
            except BaseException as rollback_failure:
                preserve_staging = True
                detail = _bounded_redacted_dependency_output(str(rollback_failure))
                recovery_detail = _bounded_redacted_dependency_output(
                    ", ".join(str(path) for path in (backup, staging) if path is not None)
                )
                diagnostic = (
                    f"{reason} Rollback also failed ({detail}). "
                    f"Recovery data was preserved at: {recovery_detail}."
                )
                if isinstance(activation_error, Exception):
                    raise WhatsAppBridgeDependencyError(diagnostic) from activation_error
                activation_error.add_note(diagnostic)
            else:
                if isinstance(activation_error, Exception):
                    raise WhatsAppBridgeDependencyError(
                        f"{reason} the prior dependency state was restored."
                    ) from activation_error
            # Preserve KeyboardInterrupt/SystemExit identity and exit semantics.
            raise

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


def ensure_whatsapp_bridge_dependencies(
    bridge_dir: Path, *, npm: Optional[str] = None, env: Optional[dict[str, str]] = None
) -> bool:
    """Explicit maintenance: True when replaced, False when already fresh.

    A stable native-user lock covers freshness through verified promotion. Caught
    failures restore the prior dependency tree; failed rollback retains recovery
    data. This is not a power-loss journal or atomic visibility to runtime readers.
    The updater may supply its resolved npm and build environment; only launch,
    home, temporary/cache, npm transport settings and node-gyp's PYTHON reach npm.
    """
    try:
        with _exclusive_whatsapp_bridge_transaction(bridge_dir) as canonical:
            return _ensure_whatsapp_bridge_dependencies(canonical, npm=npm, env=env)
    except WhatsAppBridgeDependencyError:
        raise
    except Exception as exc:
        detail = _bounded_redacted_dependency_output(str(exc))
        raise WhatsAppBridgeDependencyError(
            f"WhatsApp dependency transaction failed: {detail}"
        ) from exc


_RUNTIME_INVENTORY_FIELD = "hermesRuntimeFiles"
_DEPENDENCY_STAMP = ".hermes-pkg-hash"
_FRAMED_HASH_DOMAIN = b"hermes-whatsapp-framed-files-v1\0"
_MANIFEST_EDGE_WHITESPACE = frozenset(
    "\u0009\u000a\u000b\u000c\u000d\u001c\u001d\u001e\u001f\u0020"
    "\u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)

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
    if helper_path.exists():
        return _framed_files_hash(
            path.parent, (path.name, helper_path.name), truncate=16
        )
    # Preserve the legacy hash for truly single-file custom bridges. There is
    # no file-boundary ambiguity when exactly one byte string participates.
    return _file_content_hash(path)


class WhatsAppBridgeStateError(WhatsAppBridgeDependencyError):
    """Persistent bridge state could not be copied without following links."""


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
    managed_files: tuple[str, ...] = (),
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
        *(_bridge_runtime_files(destination) or ()),
        *managed_files,
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
            if _is_windows_reparse_point(entry_metadata) and not stat.S_ISLNK(entry_metadata.st_mode):
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
                continue
            raise WhatsAppBridgeStateError("Unsupported persistent WhatsApp state file type.")

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



def _bridge_dir_is_writable(bridge_dir: Path) -> bool:
    """Inspect access permissions without creating a probe file."""
    return bridge_dir.is_dir() and os.access(bridge_dir, os.R_OK | os.W_OK | os.X_OK)


def _validate_runtime_root(path: Path, role: str) -> None:
    if not _path_exists_without_following(path):
        return
    metadata = path.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
            or _is_windows_reparse_point(metadata)):
        raise WhatsAppBridgeStateError(
            f"The {role} WhatsApp bridge root must be a real directory, "
            "not a symlink, junction, reparse point, or file."
        )


def _runtime_bridge_paths(bundled_bridge, persistent_bridge) -> tuple[Path, Path]:
    from hermes_constants import get_hermes_home

    persistent = (Path(persistent_bridge) if persistent_bridge is not None else
                  get_hermes_home() / "scripts" / "whatsapp-bridge")
    # Maintenance callers pass the discovered path. If that is the default
    # mirror, refresh it from the installed bundle rather than from itself.
    if bundled_bridge is None or (
        persistent_bridge is None and Path(bundled_bridge).absolute() == persistent.absolute()
    ):
        bundled = _bundled_whatsapp_bridge_dir()
    else:
        bundled = Path(bundled_bridge)
    return bundled, persistent


def resolve_whatsapp_bridge_dir(
    bundled_bridge: Optional[Path] = None,
    persistent_bridge: Optional[Path] = None,
) -> Path:
    """Discover the runtime without installing, copying, locking, or probing writes.

    Prefer an existing safe mirror. Otherwise use an accessible writable bundle,
    or return the intended mirror path for explicit maintenance to prepare.
    """
    bundled, persistent = _runtime_bridge_paths(bundled_bridge, persistent_bridge)
    _validate_runtime_root(persistent, "persistent")
    if persistent.is_dir():
        return persistent
    _validate_runtime_root(bundled, "bundled")
    if (bundled / "bridge.js").is_file() and _bridge_dir_is_writable(bundled):
        return bundled
    return persistent


def prepare_whatsapp_bridge_runtime(
    bundled_bridge: Optional[Path] = None,
    persistent_bridge: Optional[Path] = None,
    *,
    npm: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
) -> Path:
    """Explicit maintenance: prepare dependencies and refresh a managed mirror.

    The retained native-user lock covers selection, staging and verified
    promotion. Caught failures restore the prior runtime; quarantined or
    uncertain state remains available for recovery. This is bounded rollback,
    not a crash journal or atomic visibility to concurrent runtime readers.
    """
    bundled, persistent = _runtime_bridge_paths(bundled_bridge, persistent_bridge)
    try:
        # Validate lexical roots before the native lock canonicalizes its target.
        _validate_runtime_root(bundled, "bundled")
        _validate_runtime_root(persistent, "persistent")
        with _exclusive_whatsapp_bridge_transaction(persistent):
            _validate_runtime_root(bundled, "bundled")
            _validate_runtime_root(persistent, "persistent")
            selected = resolve_whatsapp_bridge_dir(bundled, persistent)
            bundled = Path(_canonical_bridge_identity(bundled))
            selected = Path(_canonical_bridge_identity(selected))
            source_hash = whatsapp_bridge_source_hash(bundled / "bridge.js")
            fingerprint = whatsapp_bridge_dependency_fingerprint(bundled)
            inventory = _bridge_runtime_files(bundled)
            if not source_hash or not fingerprint or inventory == ():
                raise WhatsAppBridgeStateError("Bundled WhatsApp runtime is incomplete; preparation failed.")
            if selected == bundled:
                _ensure_whatsapp_bridge_dependencies(selected, npm=npm, env=env)
                if whatsapp_bridge_source_hash(bundled / "bridge.js") != source_hash:
                    raise WhatsAppBridgeStateError("Bundled WhatsApp sources changed during preparation.")
                return selected
            if not inventory:
                raise WhatsAppBridgeStateError("Bundled WhatsApp runtime has no valid hermesRuntimeFiles inventory.")
            return _prepare_whatsapp_bridge_mirror(
                bundled, selected, inventory, source_hash, fingerprint, npm=npm, env=env
            )
    except WhatsAppBridgeDependencyError:
        raise
    except Exception as exc:
        raise WhatsAppBridgeStateError(
            "WhatsApp runtime preparation failed: " + _bounded_redacted_dependency_output(str(exc))
        ) from exc


def _prepare_whatsapp_bridge_mirror(
    bundled: Path, live: Path, inventory: tuple[str, ...], source_hash: str,
    fingerprint: str, *, npm: Optional[str], env: Optional[dict[str, str]],
) -> Path:
    """Stage and promote the mirror while the public preparation owner holds the lock."""
    def sources_match(directory: Path) -> bool:
        return (
            _bridge_runtime_files(directory) == inventory
            and whatsapp_bridge_source_hash(directory / "bridge.js") == source_hash
            and whatsapp_bridge_dependency_fingerprint(directory) == fingerprint
        )

    if live.is_dir() and sources_match(live):
        _ensure_whatsapp_bridge_dependencies(live, npm=npm, env=env)
        if not sources_match(bundled) or not sources_match(live):
            raise WhatsAppBridgeStateError("WhatsApp sources changed during preparation.")
        return live

    staging: Optional[Path] = None
    backup: Optional[Path] = None
    preserve_staging = False
    committed = False
    had_runtime = live.is_dir()

    def copy_state(source: Path, destination: Path, *, overwrite: bool = True) -> None:
        nonlocal preserve_staging
        preserve_staging = True  # An interrupted copy may contain newer state.
        _copy_persistent_bridge_state(
            source, destination, overwrite=overwrite, managed_files=inventory
        )
        preserve_staging = False

    try:
        live.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{live.name}.staging-", dir=live.parent))
        if had_runtime:
            copy_state(live, staging)
        _copy_bundled_bridge_sources(bundled, staging)
        # Never reacquire the same native-user lock through the public installer.
        _ensure_whatsapp_bridge_dependencies(staging, npm=npm, env=env)
        if not sources_match(staging) or not sources_match(bundled):
            raise WhatsAppBridgeStateError("Bundled or staged sources changed while npm ci ran.")
        if had_runtime:
            copy_state(live, staging)
        if (not sources_match(bundled) or not sources_match(staging)
                or not whatsapp_bridge_dependencies_fresh(staging)):
            raise WhatsAppBridgeStateError("Runtime sources or dependency stamp changed before promotion.")
        if had_runtime:
            backup = live.with_name(f".{live.name}.backup-{uuid.uuid4().hex}")
            os.replace(live, backup)
        os.replace(staging, live)
        if not sources_match(live) or not whatsapp_bridge_dependencies_fresh(live):
            raise WhatsAppBridgeStateError("Promoted runtime or dependency fingerprint changed before commit.")
        if backup is not None:
            copy_state(backup, live, overwrite=False)
        if not sources_match(live) or not whatsapp_bridge_dependencies_fresh(live):
            raise WhatsAppBridgeStateError("Promoted runtime or dependency fingerprint changed before commit.")
        committed = True
    except BaseException as error:
        # Rename can succeed and then raise before a Python flag is assigned.
        # The reserved stage/backup paths record which moves actually happened.
        rollback_errors: list[str] = []
        if (staging is not None and not _path_exists_without_following(staging)
                and _path_exists_without_following(live)):
            preserve_staging = True  # Never discard writes to the promoted tree.
            try:
                os.replace(live, staging)
            except BaseException as failure:
                rollback_errors.append("could not quarantine the promoted runtime: " +
                                       _bounded_redacted_dependency_output(str(failure)))
        restored = False
        if backup is not None and _path_exists_without_following(backup):
            if _path_exists_without_following(live):
                rollback_errors.append("could not restore the prior runtime because the live path could not be vacated")
            else:
                try:
                    os.replace(backup, live)
                    backup = None
                    restored = True
                except BaseException as failure:
                    rollback_errors.append("could not restore the prior runtime: " +
                                           _bounded_redacted_dependency_output(str(failure)))
        detail = "WhatsApp runtime preparation failed (" + _bounded_redacted_dependency_output(str(error)) + "); "
        if rollback_errors:
            preserve_staging = True
            detail += "rollback also failed (" + "; ".join(rollback_errors) + "). "
        elif restored:
            detail += "rolled back to the prior runtime. "
        else:
            detail += "the prior runtime was retained. "
        if preserve_staging:
            recovery = [str(p) for p in (backup, staging, live)
                        if p is not None and _path_exists_without_following(p)]
            detail += "Recovery data was preserved at: " + ", ".join(recovery) + "."
        logger.error("[whatsapp] %s", detail)
        if isinstance(error, Exception):
            raise WhatsAppBridgeStateError(detail) from error
        error.add_note(detail)
        raise
    finally:
        if staging is not None and _path_exists_without_following(staging) and not preserve_staging:
            try:
                _remove_path_without_following(staging)
            except BaseException as cleanup_error:
                logger.warning(
                    "[whatsapp] Could not remove state-bearing bridge staging at %s; "
                    "it remains available for recovery: %s", staging,
                    _bounded_redacted_dependency_output(str(cleanup_error)),
                )
    if committed and backup is not None and _path_exists_without_following(backup):
        try:
            _remove_path_without_following(backup)
        except Exception as cleanup_error:
            logger.warning(
                "[whatsapp] Bridge update succeeded but the state-bearing backup remains at %s: %s",
                backup, _bounded_redacted_dependency_output(str(cleanup_error)),
            )
    return live
