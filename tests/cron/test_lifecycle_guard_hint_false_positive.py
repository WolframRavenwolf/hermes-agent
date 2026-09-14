"""Referenced-script comment/hint false positives (#106723).

The original #106735 implementation exempted all unquoted script comments for
all callers. Narrow that allowance explicitly: terminal opts into ignoring
only an initial blank/full-line-comment preamble. Cron/default callers and
comments after executable text remain conservative, including inline shell
payloads whose raw outer command still contains the lifecycle text.
"""

from __future__ import annotations

import pytest

from cron.lifecycle_guard import (
    GatewayLifecycleBlocked,
    check_gateway_lifecycle,
    contains_gateway_lifecycle_command,
    contains_gateway_lifecycle_command_or_referenced_script as guard,
)


def test_comment_hint_requires_explicit_terminal_opt_in(tmp_path):
    script = tmp_path / "papercuts"
    script.write_text(
        "#!/bin/bash\n"
        "# hint: Run `hermes gateway restart` from a separate shell\n"
        "echo ok\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    command = f"{script} stats"
    assert guard(command, cwd=str(tmp_path)) is True
    assert guard(
        command, cwd=str(tmp_path), ignore_full_line_shell_comments=True
    ) is False
    with pytest.raises(GatewayLifecycleBlocked):
        check_gateway_lifecycle(command)
    with pytest.raises(GatewayLifecycleBlocked):
        check_gateway_lifecycle(None, str(script))


def test_real_lifecycle_in_referenced_script_still_blocked(tmp_path):
    script = tmp_path / "bad.sh"
    script.write_text("#!/bin/bash\nhermes gateway restart\n", encoding="utf-8")
    assert guard(f"bash {script}", cwd=str(tmp_path)) is True


def test_trailing_comment_hint_remains_conservatively_blocked(tmp_path):
    script = tmp_path / "help.sh"
    script.write_text(
        "#!/bin/bash\n"
        "echo ok  # recovery: hermes gateway restart\n",
        encoding="utf-8",
    )
    assert guard(f"bash {script}", cwd=str(tmp_path)) is True
    assert guard(
        f"bash {script}", cwd=str(tmp_path), ignore_full_line_shell_comments=True
    ) is True


def test_comment_does_not_hide_real_lifecycle_on_same_line(tmp_path):
    script = tmp_path / "mixed.sh"
    script.write_text(
        "#!/bin/bash\n"
        "hermes gateway stop  # also documented here\n",
        encoding="utf-8",
    )
    assert guard(f"bash {script}", cwd=str(tmp_path)) is True


def test_hash_in_parameter_expansion_does_not_hide_lifecycle(tmp_path):
    """``${#var}`` / ``$#`` must not be treated as comment starters."""
    script = tmp_path / "len.sh"
    script.write_text(
        "#!/bin/bash\n"
        'echo ${#HOME} $# ; hermes gateway restart\n',
        encoding="utf-8",
    )
    assert guard(f"bash {script}", cwd=str(tmp_path)) is True


def test_top_level_lifecycle_command_still_blocked():
    assert guard("hermes gateway restart") is True
    assert guard("hermes gateway stop") is True
    assert guard("hermes gateway uninstall") is True


def test_cron_prompt_prose_still_blocked():
    """Top-level prompt scanning must keep matching command-shaped prose."""
    assert contains_gateway_lifecycle_command(
        "then run hermes gateway restart"
    ) is True
    with pytest.raises(GatewayLifecycleBlocked):
        check_gateway_lifecycle("then run hermes gateway restart", None)


@pytest.mark.parametrize("fallback", ["none", "walk", "direct"])
@pytest.mark.parametrize("body, expected", [
    ("# hermes gateway restart\nprintf safe", False),
    ("# hint\nhermes gateway restart", True),
    ("# hint\nlaunchctl submit -l com.example.helper -- /bin/true", True),
])
def test_terminal_opt_in_survives_exception_fallbacks(monkeypatch, fallback, body, expected):
    import cron.lifecycle_guard as lifecycle_guard

    def fail(*args, **kwargs):
        raise RuntimeError("injected scan failure")

    if fallback != "none":
        monkeypatch.setattr(lifecycle_guard, "_contains_unsafe_gateway_action", fail)
    if fallback == "direct":
        monkeypatch.setattr(lifecycle_guard, "_direct_lifecycle_scan", fail)
    assert guard(body, ignore_full_line_shell_comments=True) is expected
    assert guard(body) is True


@pytest.mark.parametrize("form", ["direct", "local", "remote"])
@pytest.mark.parametrize("limit", [
    "_MAX_LIFECYCLE_SCAN_BYTES",
    "_MAX_LIFECYCLE_SCAN_LINES",
    "_MAX_LIFECYCLE_SCAN_LINE_BYTES",
])
def test_terminal_preamble_is_charged_before_stripping(monkeypatch, tmp_path, form, limit):
    import cron.lifecycle_guard as lifecycle_guard

    body = "# " + "x" * 80 + "\n# hint\n# hint\n# hint\nprintf safe"
    command = body if form == "direct" else "bash a.sh"
    if form == "local":
        (tmp_path / "a.sh").write_text(body, encoding="utf-8")
    remote = (lambda path: body) if form == "remote" else None
    assert guard(
        command, cwd=str(tmp_path), read_remote_script=remote,
        ignore_full_line_shell_comments=True,
    ) is False
    monkeypatch.setattr(lifecycle_guard, limit, 4 if limit.endswith("LINES") else 64)
    assert guard(
        command, cwd=str(tmp_path), read_remote_script=remote,
        ignore_full_line_shell_comments=True,
    ) is True


@pytest.mark.parametrize("limit, cap", [
    ("_MAX_REFERENCED_SCRIPT_BYTES", 16),
    ("_MAX_REFERENCED_SCRIPT_DEPTH", 2),
    ("_MAX_LIFECYCLE_SCAN_PATHS", 1),
    ("_MAX_LIFECYCLE_SCAN_BYTES", 96),
])
def test_terminal_preamble_keeps_referenced_walk_limits(monkeypatch, tmp_path, limit, cap):
    import cron.lifecycle_guard as lifecycle_guard

    (tmp_path / "a.sh").write_text("# " + "x" * 40 + "\nbash b.sh\n", encoding="utf-8")
    (tmp_path / "b.sh").write_text("# " + "x" * 40 + "\nprintf safe\n", encoding="utf-8")
    assert guard(
        "bash a.sh", cwd=str(tmp_path), ignore_full_line_shell_comments=True,
    ) is False
    monkeypatch.setattr(lifecycle_guard, limit, cap)
    assert guard(
        "bash a.sh", cwd=str(tmp_path), ignore_full_line_shell_comments=True,
    ) is True
