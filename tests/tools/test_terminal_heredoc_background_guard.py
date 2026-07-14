"""Regression tests for conservative heredoc-aware background detection.

The guard may ignore ampersands only in quoted heredoc bodies sent to known
non-shell interpreters. Unknown, expandable, or shell-consumed bodies stay
visible so process-management guidance cannot be bypassed.
"""

from tools.terminal_tool import (
    _foreground_background_guidance as guidance,
    _strip_quotes,
)

AMP = chr(38)
NL = chr(10)


class TestInertQuotedHeredocPayloadAllowed:
    def test_python_bitwise_and(self):
        command = (
            "python3 - <<'PY'" + NL
            + "mode = current " + AMP + " 0o777" + NL
            + "print(mode)" + NL
            + "PY"
        )
        assert guidance(command) is None

    def test_applescript_string_concatenation(self):
        command = (
            "osascript <<'APPLESCRIPT'" + NL
            + 'set output to "count " ' + AMP + " (count of items)" + NL
            + "APPLESCRIPT"
        )
        assert guidance(command) is None

    def test_double_quoted_numeric_delimiter(self):
        command = (
            'python3 - <<"123"' + NL
            + "mode = current " + AMP + " mask" + NL
            + "123"
        )
        assert guidance(command) is None

    def test_quoted_delimiter_with_punctuation(self):
        command = (
            "python3 - <<'END.X'" + NL
            + "mode = current " + AMP + " mask" + NL
            + "END.X"
        )
        assert guidance(command) is None

    def test_dash_delimiter_with_tab_indented_close(self):
        command = (
            "python3 - <<-'PY'" + NL
            + "\tmode = current " + AMP + " mask" + NL
            + "\tPY"
        )
        assert guidance(command) is None

    def test_multiple_quoted_heredocs_on_one_opener(self):
        command = (
            "python3 - <<'A' 3<<'B'" + NL
            + "one " + AMP + " two" + NL
            + "A" + NL
            + "three " + AMP + " four" + NL
            + "B"
        )
        assert guidance(command) is None


class TestUnsafeHeredocPayloadRemainsVisible:
    def test_unquoted_payload_is_still_scanned(self):
        command = "cat <<EOF" + NL + "FaceTime " + AMP + " Privacy" + NL + "EOF"
        assert guidance(command) is not None

    def test_unquoted_command_substitution_is_still_scanned(self):
        command = (
            "cat <<EOF" + NL
            + "$(nohup sleep 10 >/dev/null 2>" + AMP + "1 " + AMP + ")" + NL
            + "EOF"
        )
        assert guidance(command) is not None

    def test_shell_interpreter_payload_is_still_scanned(self):
        command = (
            "bash <<'EOF'" + NL
            + "nohup sleep 10 >/dev/null 2>" + AMP + "1 " + AMP + NL
            + "EOF"
        )
        assert guidance(command) is not None

    def test_python_elsewhere_does_not_authorize_bash_heredoc(self):
        command = (
            "python3 -c 'pass'; bash <<'EOF'" + NL
            + "nohup sleep 10 " + AMP + NL
            + "EOF"
        )
        assert guidance(command) is not None

    def test_pipeline_python_does_not_authorize_bash_heredoc(self):
        command = (
            "bash <<'EOF' | python3" + NL
            + "nohup sleep 10 " + AMP + NL
            + "EOF"
        )
        assert guidance(command) is not None

    def test_line_continuation_does_not_authorize_later_bash_heredoc(self):
        command = (
            "python3 -c 'pass'; \\" + NL
            + "bash <<'EOF'" + NL
            + "nohup sleep 10 " + AMP + NL
            + "EOF"
        )
        assert guidance(command) is not None

    def test_nested_substitution_does_not_authorize_bash_heredoc(self):
        command = (
            "python3 -c $(bash <<'SH'" + NL
            + "nohup sleep 100 >/dev/null 2>" + AMP + "1 " + AMP + NL
            + "printf pass" + NL
            + "SH" + NL
            + ")"
        )
        assert guidance(command) is not None


class TestInactiveMarkersCannotHideShellTail:
    def test_marker_in_comment_does_not_hide_background_command(self):
        command = ": # <<EOF" + NL + "nohup sleep 10 " + AMP
        assert guidance(command) is not None

    def test_marker_in_multiline_quote_does_not_hide_background_command(self):
        command = "printf '<<EOF" + NL + "literal'" + NL + "sleep 100 " + AMP
        assert guidance(command) is not None

    def test_here_string_does_not_hide_background_command(self):
        command = "cat <<<EOF" + NL + "nohup sleep 10 " + AMP
        assert guidance(command) is not None

    def test_delimiter_prefix_does_not_hide_background_command(self):
        command = (
            "cat <<EOF.txt" + NL
            + "payload" + NL
            + "EOF.txt" + NL
            + "nohup sleep 10 " + AMP
        )
        assert guidance(command) is not None

    def test_line_continuation_keeps_opener_background_visible(self):
        command = (
            "python3 - <<'PY' \\" + NL
            + "  >/dev/null " + AMP + NL
            + "print('ok')" + NL
            + "PY"
        )
        assert guidance(command) is not None

    def test_double_quoted_backslash_delimiter_preserves_real_tail(self):
        command = (
            'python3 - <<"E\\OF"' + NL
            + 'print("ok")' + NL
            + "E\\OF" + NL
            + "nohup sleep 10 " + AMP + NL
            + "EOF"
        )
        assert guidance(command) is not None


class TestRealBackgroundingStillBlocked:
    def test_trailing_background(self):
        assert guidance("python3 server.py " + AMP) is not None

    def test_unspaced_trailing_background(self):
        assert guidance("sleep 10" + AMP) is not None

    def test_unspaced_inline_background(self):
        assert guidance("sleep 10" + AMP + "echo done") is not None

    def test_unspaced_background_before_newline(self):
        assert guidance("sleep 10" + AMP + NL + "echo done") is not None

    def test_inline_background(self):
        assert guidance("sleep 100 " + AMP + " echo done") is not None

    def test_help_flag_does_not_bypass_background_detection(self):
        assert guidance("sleep 100 " + AMP + " echo --help") is not None

    def test_active_substitution_inside_double_quotes_is_scanned(self):
        command = 'echo "$(nohup sleep 10 >/dev/null 2>' + AMP + '1 ' + AMP + ')"'
        assert guidance(command) is not None

    def test_apostrophes_inside_double_quotes_do_not_hide_substitution(self):
        command = (
            'echo "it\'s $(nohup sleep 100 >/dev/null 2>'
            + AMP + '1 ' + AMP + ') that\'s all"'
        )
        assert guidance(command) is not None

    def test_active_substitution_inside_arithmetic_is_scanned(self):
        command = (
            "echo $(( $(sleep 100 >/dev/null 2>"
            + AMP + "1 " + AMP + " echo 1) + 1 ))"
        )
        assert guidance(command) is not None

    def test_active_backtick_wrapper_is_scanned(self):
        assert guidance('echo "`nohup sleep 100`"') is not None

    def test_escaped_literal_ampersand_is_allowed(self):
        assert guidance("printf foo\\" + AMP) is None

    def test_comment_ampersands_are_allowed(self):
        assert guidance("echo ok # R" + AMP + "D" + AMP) is None

    def test_arithmetic_ampersand_is_allowed(self):
        assert guidance("echo $((1 " + AMP + " 1))") is None

    def test_background_on_heredoc_opener(self):
        command = "python3 - <<'PY' " + AMP + NL + "print('ok')" + NL + "PY"
        assert guidance(command) is not None

    def test_background_after_heredoc(self):
        command = (
            "python3 - <<'PY'" + NL
            + "print('ok')" + NL
            + "PY" + NL
            + "long_running " + AMP
        )
        assert guidance(command) is not None


class TestStripQuotesHeredoc:
    def test_inert_body_is_removed_but_shell_tail_is_preserved(self):
        command = (
            "python3 - <<'PY'" + NL
            + "x = left " + AMP + " right" + NL
            + "PY" + NL
            + "sleep 10 " + AMP
        )
        stripped = _strip_quotes(command)
        assert "x = left " + AMP + " right" not in stripped
        assert "sleep 10 " + AMP in stripped

    def test_normal_heredoc_requires_unindented_terminator(self):
        command = (
            "python3 - <<'PY'" + NL
            + "payload" + NL
            + "\tPY" + NL
            + "still payload " + AMP + " text" + NL
            + "PY"
        )
        assert guidance(command) is None

    def test_dash_heredoc_does_not_accept_space_indented_terminator(self):
        command = (
            "python3 - <<-'PY'" + NL
            + "payload" + NL
            + " PY" + NL
            + "still payload " + AMP + " text" + NL
            + "PY"
        )
        assert guidance(command) is None
