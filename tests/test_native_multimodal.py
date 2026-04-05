"""Tests for native multimodal image support.

Covers: token estimation, session DB serialization, context compressor
flattening, persist_user_message_override with multimodal content,
and user message construction with image_paths.
"""
import json
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

class TestTokenEstimationMultimodal:
    """estimate_messages_tokens_rough must handle multimodal content arrays."""

    def test_text_only_unchanged(self):
        """Plain text messages still work as before."""
        from agent.model_metadata import estimate_messages_tokens_rough
        msgs = [{"role": "user", "content": "Hello world"}]
        result = estimate_messages_tokens_rough(msgs)
        assert result > 0

    def test_image_block_fixed_cost(self):
        """Image blocks should use ~1600 token estimate, not base64 char count."""
        from agent.model_metadata import estimate_messages_tokens_rough
        # A 100KB base64 string would estimate as ~25000 tokens with old logic
        fake_b64 = "A" * 100_000
        msgs = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "What is this?"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{fake_b64}"}}
            ]
        }]
        result = estimate_messages_tokens_rough(msgs)
        # Should be ~1600 (image) + ~3 (text) = ~1603, NOT ~25003
        assert result < 5000, f"Image tokens overestimated: {result}"
        assert result >= 1600, f"Image tokens underestimated: {result}"

    def test_multiple_images(self):
        """Multiple images should each add ~1600 tokens."""
        from agent.model_metadata import estimate_messages_tokens_rough
        msgs = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Compare these"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBB"}},
            ]
        }]
        result = estimate_messages_tokens_rough(msgs)
        assert result >= 3200  # 2 images × 1600

    def test_anthropic_image_format(self):
        """Anthropic-native image blocks (type=image) also get fixed cost."""
        from agent.model_metadata import estimate_messages_tokens_rough
        msgs = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "A" * 50000}}
            ]
        }]
        result = estimate_messages_tokens_rough(msgs)
        assert result < 5000


# ---------------------------------------------------------------------------
# Context compressor - multimodal flattening
# ---------------------------------------------------------------------------

class TestCompressorMultimodal:
    """_flatten_multimodal_content must strip image data for summarization."""

    def test_plain_string_passthrough(self):
        from agent.context_compressor import ContextCompressor
        assert ContextCompressor._flatten_multimodal_content("hello") == "hello"

    def test_none_returns_empty(self):
        from agent.context_compressor import ContextCompressor
        assert ContextCompressor._flatten_multimodal_content(None) == ""
        assert ContextCompressor._flatten_multimodal_content("") == ""

    def test_image_blocks_replaced(self):
        from agent.context_compressor import ContextCompressor
        content = [
            {"type": "text", "text": "Check this out"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,HUGE_DATA"}},
        ]
        result = ContextCompressor._flatten_multimodal_content(content)
        assert "Check this out" in result
        assert "[Attached image]" in result
        assert "HUGE_DATA" not in result

    def test_anthropic_image_format(self):
        from agent.context_compressor import ContextCompressor
        content = [
            {"type": "text", "text": "Look"},
            {"type": "image", "source": {"type": "base64", "data": "X" * 10000}},
        ]
        result = ContextCompressor._flatten_multimodal_content(content)
        assert "[Attached image]" in result
        assert "X" * 100 not in result


# ---------------------------------------------------------------------------
# Session DB - multimodal serialization round-trip
# ---------------------------------------------------------------------------

class TestSessionDBMultimodal:
    """Multimodal content must survive append → get_messages round-trip."""

    def test_multimodal_content_round_trip(self, tmp_path):
        from hermes_state import SessionDB
        db_path = tmp_path / "test.db"
        state = SessionDB(db_path)

        session_id = "test-session-mm"
        state.create_session(session_id, source="test")

        # Multimodal content with text + image
        content = [
            {"type": "text", "text": "What is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQ"}},
        ]
        state.append_message(session_id, "user", content=content)

        # Load back
        messages = state.get_messages_as_conversation(session_id)
        assert len(messages) == 1
        loaded = messages[0]["content"]
        assert isinstance(loaded, list), f"Expected list, got {type(loaded)}"
        assert loaded[0]["type"] == "text"
        assert loaded[0]["text"] == "What is this?"
        assert loaded[1]["type"] == "image_url"

    def test_plain_string_still_works(self, tmp_path):
        from hermes_state import SessionDB
        db_path = tmp_path / "test.db"
        state = SessionDB(db_path)

        session_id = "test-session-plain"
        state.create_session(session_id, source="test")

        state.append_message(session_id, "user", content="Hello world")
        messages = state.get_messages_as_conversation(session_id)
        assert messages[0]["content"] == "Hello world"

    def test_string_starting_with_bracket_not_parsed(self, tmp_path):
        """A plain string that starts with '[' should NOT be parsed as JSON
        unless it's actually valid JSON array with 'type' keys."""
        from hermes_state import SessionDB
        db_path = tmp_path / "test.db"
        state = SessionDB(db_path)

        session_id = "test-session-bracket"
        state.create_session(session_id, source="test")

        content = "[The user sent an image~ Here's what I can see: a cat]"
        state.append_message(session_id, "user", content=content)
        messages = state.get_messages_as_conversation(session_id)
        # Should remain a string (invalid JSON)
        assert isinstance(messages[0]["content"], str)
        assert messages[0]["content"] == content

    def test_json_array_without_type_not_promoted(self, tmp_path):
        """A valid JSON array that doesn't look like multimodal content
        should remain a string (e.g., tool results)."""
        from hermes_state import SessionDB
        db_path = tmp_path / "test.db"
        state = SessionDB(db_path)

        session_id = "test-session-json-array"
        state.create_session(session_id, source="test")

        # This is valid JSON but NOT multimodal content
        content = '[{"name": "foo", "value": 42}]'
        state.append_message(session_id, "user", content=content)
        messages = state.get_messages_as_conversation(session_id)
        # Should remain a string (no "type" key in dicts)
        assert isinstance(messages[0]["content"], str)


# ---------------------------------------------------------------------------
# persist_user_message_override with multimodal content
# ---------------------------------------------------------------------------

class TestPersistOverrideMultimodal:
    """_apply_persist_user_message_override must preserve image blocks."""

    def test_override_replaces_text_preserves_images(self):
        from run_agent import AIAgent

        agent = AIAgent.__new__(AIAgent)
        agent._persist_user_message_idx = 0
        agent._persist_user_message_override = "Clean text only"

        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Original with [synthetic prefix]"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,ABC"}},
            ]
        }]

        agent._apply_persist_user_message_override(messages)

        content = messages[0]["content"]
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "Clean text only"}
        assert content[1]["type"] == "image_url"  # Image preserved

    def test_override_plain_string_unchanged(self):
        from run_agent import AIAgent

        agent = AIAgent.__new__(AIAgent)
        agent._persist_user_message_idx = 0
        agent._persist_user_message_override = "Override"

        messages = [{"role": "user", "content": "Original"}]
        agent._apply_persist_user_message_override(messages)
        assert messages[0]["content"] == "Override"
