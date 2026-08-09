"""Gateway lifecycle guard for cron job creation (#30719).

An agent running inside a gateway can schedule a cron job that calls
``hermes gateway restart`` (or ``launchctl kickstart ai.hermes.gateway``
or ``systemctl restart hermes-gateway``).  When the cron fires, the
gateway dies, the supervisor (launchd KeepAlive / systemd Restart=)
revives it, auto-resume picks up the offending session, and the resumed
turn re-runs the same logic — a SIGTERM-respawn loop every ~10 seconds
until manually broken.

This module rejects cron job specs whose prompt or script contains a
direct shell-level gateway-lifecycle command.  It is enforced at
``cron.jobs.create_job`` so it fires on every job-creation path: the
``hermes cron create`` CLI subcommand AND the agent's ``cronjob`` model
tool (which calls ``create_job`` directly, bypassing the CLI layer).

The pattern is intentionally command-shaped: it anchors on a concrete
command identifier (``hermes gateway``, ``launchctl ... hermes-gateway``,
``systemctl ... hermes-gateway``, ``pkill`` against the gateway) so it
cannot fire on prose.  A cron ``prompt`` is fed to a future LLM, not a
shell, so an over-broad substring match on English ("Kong API gateway
autoscaling and restart behavior") would produce a high false-positive
rate without preventing the actual foot-gun, which requires a real
command shape.

This is a defence-in-depth layer.  ``tools/terminal_tool.py`` blocks direct
commands and shell scripts they reference when ``_HERMES_GATEWAY=1``. It also
rejects ``launchctl submit`` in gateway sessions because launchd treats that
primitive as a persistent KeepAlive job, not a one-shot task. ``hermes gateway
stop|restart`` separately refuse to self-target from inside the gateway.
Blocking cron specs at creation time as well means the agent gets an immediate,
informative rejection instead of scheduling a job that will only fail
(silently) when it fires.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import shlex
import stat
from pathlib import Path
from typing import Callable, Iterator, Optional


class GatewayLifecycleBlocked(ValueError):
    """Raised when a cron job spec contains a gateway-lifecycle command."""


# Shell-level command shapes that target the gateway lifecycle. Each branch
# is anchored on a concrete command identifier so a match can only fire on
# actual shell-command-shaped strings, not on prose.
_GATEWAY_LIFECYCLE_PATTERN = re.compile(
    r"(?i)"
    # Branch A: `hermes gateway restart|stop` — the canonical foot-gun.
    # `start` is intentionally excluded: starting a gateway from inside a
    # gateway is benign (a no-op or "already running" error), and a
    # legitimate cron job might start a sibling profile's gateway.
    r"(?:hermes\s+gateway\s+(?:restart|stop))"
    # Branch B: launchctl ops on a hermes-gateway label. macOS launchd
    # labels look like `ai.hermes.gateway` / `hermes-gateway`. Requiring the
    # gateway identifier prevents blocking unrelated hermes services (e.g.
    # `launchctl unload ai.hermes.update-checker.plist`).
    # `submit` and `bootstrap` are included alongside the direct verbs
    # (kickstart/etc.): `launchctl submit -l ai.hermes.gateway-<suffix> --
    # <helper-script>` (or `launchctl bootstrap gui/<uid> <plist>`) creates
    # a NEW keepalive job wrapping an arbitrary helper, which is how a
    # blocked direct restart/kill gets laundered into a persistent restart
    # loop instead (#62891) — same foot-gun, indirect shape. Neutral-label
    # submissions that dodge this text anchor are caught separately by
    # `contains_launchctl_submit_command` (execution-aware, label-independent).
    r"|(?:launchctl\s+(?:kickstart|unload|load|stop|restart|submit|bootstrap)\b[^\n]*\bhermes[.\-]?gateway)"
    # Branch C: systemctl ops on a hermes-gateway unit.
    r"|(?:systemctl\s+(?:-\S+\s+)*(?:restart|stop|start)\b[^\n]*\bhermes[.\-]?gateway)"
    # Branch D: pkill / kill targeting the hermes gateway process. Both
    # token orders because real reproductions show both.
    r"|(?:p?kill\b[^\n]*\bhermes\b[^\n]*\bgateway)"
    r"|(?:p?kill\b[^\n]*\bgateway\b[^\n]*\bhermes)"
)


# A backslash immediately followed by a newline is a POSIX shell line
# continuation — the shell joins the two lines before parsing. Every branch
# above uses `[^\n]*` between its verb and the gateway identifier so the
# match can't span unrelated lines of a longer cron prompt/script, but that
# also means a real multi-line shell invocation split across continuation
# lines (e.g. `launchctl submit \` / `  -l ai.hermes.gateway-... \` / `  -- ...`,
# the exact reported shape in #62891) would otherwise slip past. Collapse
# continuations to a single space before matching, mirroring what the shell
# itself does, rather than loosening `[^\n]*` and risking false positives
# across genuinely separate lines.
_SHELL_LINE_CONTINUATION = re.compile(r"\\\r?\n[ \t]*")

# systemctl accepts global options before or after its verb, and the verbs that
# can stop a live service are broader than restart/stop.
_SYSTEMCTL_ALWAYS_BLOCKED_ACTIONS = frozenset(
    {
        "start",
        "stop",
        "restart",
        "kill",
        "try-restart",
        "reload-or-restart",
        "reload-or-try-restart",
        "condrestart",
    }
)
_SYSTEMCTL_NOW_BLOCKED_ACTIONS = frozenset({"disable", "mask"})
_SYSTEMCTL_GATEWAY_UNIT_RE = re.compile(
    r"^(?:ai[.\-])?hermes[.\-]?gateway"
    r"(?:[-@][a-z0-9_.@\-]+)?(?:\.service)?$",
    re.IGNORECASE,
)
_SYSTEMCTL_SHELL_SEPARATORS = frozenset({";", "&", "|", "(", ")"})


def _systemctl_shellish_tokens(text: str) -> list[str]:
    """Tokenize one command-shaped line, retaining common shell separators."""
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return re.findall(r"[A-Za-z0-9_@./:=+\-]+|[;&|()]+", text)


def _is_systemctl_token(token: str) -> bool:
    return token.rsplit("/", 1)[-1].casefold() == "systemctl"


def _is_gateway_systemd_unit(token: str) -> bool:
    normalized = token.strip("'\"`$,:")
    normalized = normalized.rsplit("/", 1)[-1]
    return bool(_SYSTEMCTL_GATEWAY_UNIT_RE.fullmatch(normalized))


def _systemctl_tokens_target_gateway_lifecycle(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens):
        if not _is_systemctl_token(token):
            continue
        invocation: list[str] = []
        for candidate in tokens[index + 1 :]:
            if candidate and set(candidate) <= _SYSTEMCTL_SHELL_SEPARATORS:
                break
            invocation.append(candidate)
        normalized = {candidate.casefold() for candidate in invocation}
        if not any(_is_gateway_systemd_unit(candidate) for candidate in invocation):
            continue
        if normalized & _SYSTEMCTL_ALWAYS_BLOCKED_ACTIONS:
            return True
        if normalized & _SYSTEMCTL_NOW_BLOCKED_ACTIONS and "--now" in normalized:
            return True
    return False


def _contains_systemctl_gateway_lifecycle(text: str) -> bool:
    """Scan systemctl invocations with token/quote/option normalization."""
    normalized_text = _SHELL_LINE_CONTINUATION.sub(" ", text)
    for line in normalized_text.splitlines() or [normalized_text]:
        tokens = _systemctl_shellish_tokens(line)
        if _systemctl_tokens_target_gateway_lifecycle(tokens):
            return True
        for token in tokens:
            if "systemctl" not in token.casefold() or not any(
                char.isspace() for char in token
            ):
                continue
            nested = _systemctl_shellish_tokens(token)
            if nested != [token] and _systemctl_tokens_target_gateway_lifecycle(nested):
                return True
    if "\n" in normalized_text:
        for token in _systemctl_shellish_tokens(normalized_text):
            if "\n" not in token or "systemctl" not in token.casefold():
                continue
            nested = _systemctl_shellish_tokens(token)
            if nested != [token] and _systemctl_tokens_target_gateway_lifecycle(nested):
                return True
    return False


def _strip_leading_full_line_shell_comments(text: str) -> str:
    """Remove only inert leading shell comments, preserving later shell text."""
    lines = text.splitlines(keepends=True)
    first_executable = 0
    while first_executable < len(lines):
        stripped = lines[first_executable].lstrip()
        if not stripped.strip() or stripped.startswith("#"):
            first_executable += 1
            continue
        break
    return "".join(lines[first_executable:])


def contains_gateway_lifecycle_command(
    text: str,
    *,
    ignore_full_line_shell_comments: bool = False,
) -> bool:
    """Return True if *text* contains a gateway lifecycle command pattern."""
    if not text:
        return False
    if ignore_full_line_shell_comments:
        text = _strip_leading_full_line_shell_comments(text)
    normalized = _SHELL_LINE_CONTINUATION.sub(" ", text)
    return bool(
        _GATEWAY_LIFECYCLE_PATTERN.search(normalized)
        or _contains_systemctl_gateway_lifecycle(normalized)
    )


_SHELL_EXECUTABLES = frozenset({"sh", "bash", "dash", "ksh", "zsh"})
_SHELL_OPTIONS_WITH_VALUES = frozenset({"-O", "+O", "-o", "+o"})
_MAX_REFERENCED_SCRIPT_BYTES = 1024 * 1024
_MAX_REFERENCED_SCRIPT_DEPTH = 8
_CONTROL_CHARS = frozenset(";&|()")




_ReadRemoteScriptFn = Callable[[str], Optional[str]]


def _iter_command_segments(command: str) -> Iterator[list[str]]:
    """Yield shell-tokenized command segments, honoring quotes and comments."""
    normalized = command.replace("\\\n", "")
    for line in normalized.splitlines() or [normalized]:
        try:
            lexer = shlex.shlex(
                line,
                posix=True,
                punctuation_chars=";&|()",
            )
            lexer.whitespace_split = True
            lexer.commenters = "#"
            tokens = list(lexer)
        except ValueError:
            continue

        segment: list[str] = []
        for token in tokens:
            if token and set(token) <= _CONTROL_CHARS:
                if segment:
                    yield segment
                    segment = []
                continue
            segment.append(token)
        if segment:
            yield segment


def _command_token_index(segment: list[str]) -> Optional[int]:
    """Return the executable token index after simple env assignments."""
    for index, token in enumerate(segment):
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            continue
        return index
    return None


def is_direct_canonical_restart_helper_command(
    command: str,
    *,
    script_path: str | Path,
    cwd: Optional[str] = None,
    expected_sha256: Optional[str] = None,
) -> bool:
    """Allow only one direct invocation of the trusted restart entrypoint.

    The command may contain inert leading call-shot comments, but no environment
    assignments, shell wrapper, control operator, redirection, or internal
    worker arguments. Normal restart options and a complete hash-bound
    maintenance handoff use a strict option grammar.
    """
    segments = list(_iter_command_segments(command))
    if len(segments) != 1:
        return False
    segment = segments[0]
    index = _command_token_index(segment)
    if index != 0:
        return False
    arguments = segment[1:]
    values: dict[str, str] = {}
    flags: set[str] = set()
    value_options = {
        "--delay",
        "--stability",
        "--maintenance-script",
        "--maintenance-sha256",
        "--expected-version",
        "--expected-head",
        "--require-platform",
    }
    arg_index = 0
    while arg_index < len(arguments):
        option = arguments[arg_index]
        if option == "--dry-run":
            if option in flags:
                return False
            flags.add(option)
            arg_index += 1
            continue
        if option not in value_options or option in values:
            return False
        if arg_index + 1 >= len(arguments):
            return False
        values[option] = arguments[arg_index + 1]
        arg_index += 2

    if any(
        not value.isdigit()
        for option, value in values.items()
        if option in {"--delay", "--stability"}
    ):
        return False
    maintenance_path = values.get("--maintenance-script")
    maintenance_sha256 = values.get("--maintenance-sha256")
    if bool(maintenance_path) != bool(maintenance_sha256):
        return False
    if maintenance_path and not Path(maintenance_path).is_absolute():
        return False
    if maintenance_sha256 and not re.fullmatch(
        r"[0-9a-fA-F]{64}", maintenance_sha256
    ):
        return False
    expected_head = values.get("--expected-head")
    if expected_head and not re.fullmatch(r"[0-9a-fA-F]{40}", expected_head):
        return False
    expected_version = values.get("--expected-version")
    if expected_version is not None and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._+-]*", expected_version
    ):
        return False
    if maintenance_path and not (expected_version and expected_head):
        return False
    required_platform = values.get("--require-platform")
    if required_platform is not None:
        if not maintenance_path:
            return False
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", required_platform):
            return False

    candidate = _resolve_terminal_script_path(segment[0], cwd)
    canonical = Path(script_path).expanduser()
    try:
        candidate_lstat = candidate.lstat()
        canonical_lstat = canonical.lstat()
        if not stat.S_ISREG(candidate_lstat.st_mode) or not stat.S_ISREG(
            canonical_lstat.st_mode
        ):
            return False
        if candidate.resolve(strict=True) != canonical.resolve(strict=True):
            return False
        metadata = candidate.stat()
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
            return False
        if expected_sha256 is not None:
            if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
                return False
            if metadata.st_size > _MAX_REFERENCED_SCRIPT_BYTES:
                return False
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            if not hmac.compare_digest(digest, expected_sha256.casefold()):
                return False
        return True
    except OSError:
        return False


def contains_launchctl_submit_command(command: str) -> bool:
    """Detect an executed ``launchctl submit``/``bootstrap``, not quoted text.

    Label-independent by design: the label of a submitted/bootstrapped job is
    chosen by whoever writes it, so a neutral name (``ai.hermes.svc-reload-tmp``)
    defeats any label-anchored regex (#62891, second reproduction). Both verbs
    register a NEW persistent launchd job (``submit`` jobs get KeepAlive
    semantics; ``bootstrap`` loads an arbitrary plist), which is never safe to
    do from inside the gateway process.
    """
    for segment in _iter_command_segments(command):
        index = _command_token_index(segment)
        if index is None:
            continue
        if Path(segment[index]).name == "launchctl":
            arguments = segment[index + 1 :]
            if arguments and arguments[0].lower() in {"submit", "bootstrap"}:
                return True
    return False


def _resolve_terminal_script_path(candidate: str, cwd: Optional[str]) -> Path:
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        path = Path(cwd or Path.cwd()) / path
    return path


def _iter_referenced_shell_scripts(
    command: str,
    *,
    cwd: Optional[str] = None,
) -> Iterator[Path]:
    """Yield scripts executed directly or through a POSIX shell."""
    for segment in _iter_command_segments(command):
        index = _command_token_index(segment)
        if index is None:
            continue
        executable = segment[index]
        executable_name = Path(executable).name

        if executable_name in {".", "source"}:
            if len(segment) > index + 1:
                yield _resolve_terminal_script_path(segment[index + 1], cwd)
            continue

        if executable_name in _SHELL_EXECUTABLES:
            arguments = segment[index + 1 :]
            arg_index = 0
            while arg_index < len(arguments):
                argument = arguments[arg_index]
                if argument == "--":
                    arg_index += 1
                    break
                if argument in {"-c", "--command"}:
                    break
                if argument in _SHELL_OPTIONS_WITH_VALUES:
                    arg_index += 2
                    continue
                if argument.startswith("-"):
                    arg_index += 1
                    continue
                break
            if arg_index < len(arguments) and arguments[arg_index] not in {
                "-c",
                "--command",
            }:
                yield _resolve_terminal_script_path(arguments[arg_index], cwd)
            continue

        # A bare "/" token is pathlib's division operator in Python sources
        # (e.g. `Path.home() / ".hermes"`), not an executable reference.
        # Resolving it walks to the filesystem root and fails the
        # regular-file check below, hard-blocking innocent .py scripts
        # (#77131). Skip pure-separator tokens.
        if executable.strip("/"):
            if "/" in executable or executable.endswith((".sh", ".bash", ".zsh")):
                yield _resolve_terminal_script_path(executable, cwd)


def _iter_shell_command_payloads(command: str) -> Iterator[str]:
    """Yield code passed through ``sh|bash|... -c`` for recursive scanning."""
    for segment in _iter_command_segments(command):
        index = _command_token_index(segment)
        if index is None or Path(segment[index]).name not in _SHELL_EXECUTABLES:
            continue
        arguments = segment[index + 1 :]
        for arg_index, argument in enumerate(arguments[:-1]):
            if argument in {"-c", "--command"}:
                yield arguments[arg_index + 1]
                break


def _resolve_script_directory(script_path: str) -> Optional[str]:
    """Return the directory *script_path* resolves to, handling relative names."""
    try:
        path = _resolve_script_path(script_path)
        if path.is_absolute():
            return str(path.parent)
    except Exception:
        pass
    return None


def _read_referenced_script(path: Path) -> tuple[Optional[str], bool]:
    """Return ``(text, unsafe)`` using bounded, regular-file-only reads."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except (OSError, ValueError):
        return None, False
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return None, True
        # Read a bounded chunk first — even for oversized files, the first
        # chunk tells us if this is a binary (NUL bytes) that should be
        # skipped as "nothing to scan" rather than failing closed (#76762).
        data = os.read(descriptor, _MAX_REFERENCED_SCRIPT_BYTES + 1)
    except OSError:
        return None, False
    finally:
        os.close(descriptor)
    # A NUL byte in the first chunk means this is a binary (ELF/Mach-O/
    # PE), not a shell script — scanning its decoded contents would
    # tokenize machine code and feed junk paths into the recursion
    # (including a `ValueError: embedded null byte` from Path.resolve,
    # #76762). Treat it as "nothing to scan" rather than unsafe: a binary
    # executed by the user is not a referenced *shell script*.
    if b"\x00" in data:
        return None, False
    if len(data) > _MAX_REFERENCED_SCRIPT_BYTES:
        return None, True
    return data.decode("utf-8", errors="replace"), False


def _contains_unsafe_gateway_action(
    command: str,
    *,
    cwd: Optional[str],
    depth: int,
    visited: set[Path],
    read_remote_script: Optional[_ReadRemoteScriptFn] = None,
    ignore_full_line_shell_comments: bool = False,
) -> bool:
    if contains_gateway_lifecycle_command(
        command,
        ignore_full_line_shell_comments=ignore_full_line_shell_comments,
    ) or contains_launchctl_submit_command(command):
        return True
    if depth >= _MAX_REFERENCED_SCRIPT_DEPTH:
        return True

    for payload in _iter_shell_command_payloads(command):
        if _contains_unsafe_gateway_action(
            payload,
            cwd=cwd,
            depth=depth + 1,
            visited=visited,
            read_remote_script=read_remote_script,
            ignore_full_line_shell_comments=ignore_full_line_shell_comments,
        ):
            return True

    for script_path in _iter_referenced_shell_scripts(command, cwd=cwd):
        try:
            resolved = script_path.resolve(strict=False)
        except (OSError, ValueError):
            # OSError: unreadable/long paths. ValueError: embedded NUL byte
            # from a binary's decoded contents tokenized as a path — a
            # guarded path must never crash the guard (#76762).
            resolved = script_path
        if resolved in visited:
            continue
        visited.add(resolved)
        script_text, unsafe = _read_referenced_script(script_path)
        if unsafe:
            return True
        if script_text is None and read_remote_script is not None:
            # Local path missing; try the remote backend if one is available.
            script_text = read_remote_script(str(script_path))
        if script_text and "\x00" in script_text:
            script_text = None
        if not script_text:
            continue
        # Relative references inside a script resolve against that script's
        # directory, not the original command's cwd.
        script_dir = _resolve_script_directory(str(resolved)) or cwd
        if script_text and _contains_unsafe_gateway_action(
            script_text,
            cwd=script_dir,
            depth=depth + 1,
            visited=visited,
            read_remote_script=read_remote_script,
            ignore_full_line_shell_comments=ignore_full_line_shell_comments,
        ):
            return True
    return False


def contains_gateway_lifecycle_command_or_referenced_script(
    command: str,
    *,
    cwd: Optional[str] = None,
    read_remote_script: Optional[_ReadRemoteScriptFn] = None,
    ignore_full_line_shell_comments: bool = False,
) -> bool:
    """Detect lifecycle/submit commands, including bounded nested scripts."""
    return _contains_unsafe_gateway_action(
        command,
        cwd=cwd,
        depth=0,
        visited=set(),
        read_remote_script=read_remote_script,
        ignore_full_line_shell_comments=ignore_full_line_shell_comments,
    )




def _resolve_script_path(script_path: str) -> Path:
    """Resolve a cron ``script`` value the same way the scheduler does.

    The scheduler (``cron.scheduler``) resolves a bare/relative script path
    under ``<HERMES_HOME>/scripts/`` and only accepts absolute paths as-is.
    We MUST mirror that here so the guard scans the file that will actually
    run — otherwise a job whose script lives at the scheduler's real location
    (``~/.hermes/scripts/restart.sh``) but is passed as the bare name
    ``restart.sh`` would read as a nonexistent relative path and silently
    scan prompt-only content, letting the command through.
    """
    from hermes_constants import get_hermes_home

    raw = Path(script_path).expanduser()
    if raw.is_absolute():
        return raw
    return get_hermes_home() / "scripts" / raw


def _read_script_for_scanning(script_path: str) -> str:
    """Read a cron script with the bounded terminal-script scanner.

    Non-regular or oversized inputs fail closed by returning a lifecycle-shaped
    sentinel, while missing/unreadable paths remain empty so ordinary scheduler
    path validation can report them.
    """
    script_text, unsafe = _read_referenced_script(_resolve_script_path(script_path))
    if unsafe:
        return "hermes gateway restart"
    return script_text or ""


def check_gateway_lifecycle(
    prompt: Optional[str],
    script: Optional[str] = None,
) -> None:
    """Raise ``GatewayLifecycleBlocked`` if *prompt* or *script* contains a
    gateway-lifecycle command pattern.

    ``prompt`` is scanned directly.  ``script``, when supplied, is read from
    disk and concatenated for the scan.  Both are considered together so a
    job cannot slip through by splitting the command across the prompt and
    the script.

    Callers should let the exception propagate when they want the create to
    fail with a ``ValueError``-shaped error (the agent's ``cronjob`` tool
    surfaces this as a tool error; the CLI prints it in red and exits 1).
    """
    combined = prompt or ""
    python_script = False
    if script:
        python_script = _resolve_script_path(script).suffix == ".py"
        script_text = _read_script_for_scanning(script)
        if script_text:
            combined = f"{combined}\n{script_text}"

    if python_script:
        # Python is executed by the interpreter, never through a POSIX
        # shell: the shell-script reference walk is a false-positive
        # generator on Python sources (pathlib's "/" operator resolves to
        # the filesystem root and trips the regular-file check, blocking
        # every innocent .py cron script, #77131). The direct command
        # regex below still scans the full text, so a literal
        # `hermes gateway restart` embedded in a .py script is still
        # blocked. Non-regular/oversized script files still fail closed
        # via the lifecycle-shaped sentinel in _read_script_for_scanning.
        unsafe = contains_gateway_lifecycle_command(combined)
    else:
        script_dir = _resolve_script_directory(script) if script else None
        unsafe = contains_gateway_lifecycle_command_or_referenced_script(
            combined,
            cwd=script_dir,
        )
    if unsafe:
        raise GatewayLifecycleBlocked(
            "Blocked: cron job contains a gateway lifecycle command or persistent "
            "launchctl submit operation. This is blocked to prevent agent-driven "
            "SIGTERM-respawn loops under launchd/systemd supervision "
            "(#30719). Run `hermes gateway restart` from a shell outside "
            "the running gateway instead."
        )
