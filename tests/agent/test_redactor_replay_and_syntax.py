"""Regression coverage for non-destructive secret redaction.

Backports and adapts the behavior proven upstream by #54061 and #54136,
plus a security-preserving narrow exception inspired by open PR #47348.
"""

import json
from unittest.mock import MagicMock

import pytest

from agent.chat_completion_helpers import build_assistant_message
from agent.redact import redact_sensitive_text


class _FakeToolCall:
    def __init__(self, tc_id: str, name: str, arguments: str):
        self.id = tc_id
        self.type = "function"
        self.function = MagicMock()
        self.function.name = name
        self.function.arguments = arguments
        self.extra_content = None

    def __getattr__(self, _name):
        return None


class _FakeAssistantMessage:
    def __init__(self, content: str, tool_calls: list[_FakeToolCall]):
        self.content = content
        self.tool_calls = tool_calls
        self.function_call = None
        self.reasoning_content = None
        self.model_extra = None
        self.reasoning_details = None

    def __getattr__(self, _name):
        return None


class _FakeAgent:
    stream_delta_callback = None
    _stream_callback = None
    reasoning_callback = None
    verbose_logging = False

    def _extract_reasoning(self, _msg):
        return None

    def _strip_think_blocks(self, text):
        return text

    def _needs_thinking_reasoning_pad(self):
        return False

    def _split_responses_tool_id(self, _raw):
        return (None, None)

    def _derive_responses_function_call_id(self, _call_id, _response_item_id):
        return None

    def _deterministic_call_id(self, _name, _args, index):
        return f"det_{index}"


def _build_tool_arguments(arguments: str) -> str:
    tool_call = _FakeToolCall("call_1", "terminal", arguments)
    message = _FakeAssistantMessage("ok", [tool_call])
    built = build_assistant_message(_FakeAgent(), message, "tool_calls")
    return built["tool_calls"][0]["function"]["arguments"]


@pytest.mark.parametrize(
    "name",
    [
        "GIT_" + "AUTHOR_NAME",
        "GIT_" + "AUTHOR_EMAIL",
        "GIT_" + "AUTHOR_DATE",
        "AU" + "TH_BEFORE",
        "AU" + "TH_AFTER",
        "SSH_" + "AUTH_SOCK",
    ],
)
def test_operational_metadata_assignments_are_not_secret_fields(name):
    text = f"{name}='snapshot-value'"
    assert redact_sensitive_text(text, force=True) == text


@pytest.mark.parametrize(
    "name",
    [
        "BASIC_" + "AUTH",
        "AUTH_" + "KEY",
        "AUTH_" + "TOKEN",
        "ACCESS_" + "TOKEN_VALUE",
        "MY_" + "CREDENTIAL",
        "CREDENTIAL_" + "VALUE",
        "SECRET_" + "KEY",
    ],
)
def test_actual_secret_field_names_remain_redacted(name):
    value = "opaquevalue1234567890"
    result = redact_sensitive_text(f"{name}='{value}'", force=True)
    assert value not in result


def test_auth_header_masking_preserves_closing_quotes():
    header = "Author" + "ization: Bearer "
    for quote in ("'", '"'):
        text = f"curl -H {quote}{header}shortvalue{quote}"
        result = redact_sensitive_text(text, force=True)
        assert "shortvalue" not in result
        assert result.count(quote) == 2
        assert result.endswith(quote)


def test_auth_header_masking_preserves_escaped_closing_quote_syntax():
    import ast

    header = "Author" + "ization: Bearer "
    source = f'payload = "{{\\"header\\":\\"{header}shortvalue\\"}}"'
    ast.parse(source)
    result = redact_sensitive_text(source, force=True)
    assert "shortvalue" not in result
    ast.parse(result)


def test_replayable_tool_arguments_remain_byte_exact():
    password_name = "PG" + "PASSWORD"
    command = f"{password_name}='opaquevalue1234567890' psql -h 127.0.0.1"
    arguments = json.dumps({"command": command})
    assert _build_tool_arguments(arguments) == arguments


def test_replayable_auth_header_arguments_remain_byte_exact():
    header = "Author" + "ization: Bearer "
    arguments = json.dumps({"command": f"curl -H '{header}shortvalue' https://example.invalid"})
    assert _build_tool_arguments(arguments) == arguments


def test_multiline_connection_template_does_not_consume_following_code():
    scheme = "postgres" + "ql://"
    text = (
        f'return f"{scheme}{{user}}:{{password}}@{{host}}"\n'
        "@decorator\n"
        "def validate(): ..."
    )
    result = redact_sensitive_text(text, force=True, code_file=True)
    assert result == text


def test_multiline_connection_template_keeps_line_boundaries_in_default_mode():
    scheme = "postgres" + "ql://"
    text = (
        f'return f"{scheme}{{user}}:{{password}}@{{host}}"\n'
        "@decorator\n"
        "def validate(): ..."
    )
    result = redact_sensitive_text(text, force=True)
    assert "@decorator" in result
    assert "def validate(): ..." in result
    assert result.count("\n") == text.count("\n")


def test_literal_connection_password_remains_redacted_in_code_mode():
    scheme = "postgres" + "ql://"
    password = "literalpassword123456"
    text = f"{scheme}admin:{password}@db.internal/app"
    result = redact_sensitive_text(text, force=True, code_file=True)
    assert password not in result
