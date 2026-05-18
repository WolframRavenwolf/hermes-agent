"""
Tests for MEDIA tag extraction from tool results.

Verifies that MEDIA tags (e.g., from TTS tool) are only extracted from
messages in the CURRENT turn, not from the full conversation history.
This prevents voice messages from accumulating and being sent multiple
times per reply. (Regression test for #160)
"""

import pytest
import re

from gateway.platforms.base import BasePlatformAdapter


def extract_media_tags_fixed(result_messages, history_len):
    """
    Extract MEDIA tags from tool results, but ONLY from new messages
    (those added after history_len). This is the fixed behavior.
    
    Args:
        result_messages: Full list of messages including history + new
        history_len: Length of history before this turn
        
    Returns:
        Tuple of (media_tags list, has_voice_directive bool)
    """
    media_tags = []
    has_voice_directive = False
    
    # Only process new messages from this turn
    new_messages = result_messages[history_len:] if len(result_messages) > history_len else []
    
    for msg in new_messages:
        if msg.get("role") == "tool" or msg.get("role") == "function":
            content = msg.get("content", "")
            if "MEDIA:" in content:
                for match in re.finditer(r'MEDIA:(\S+)', content):
                    path = match.group(1).strip().rstrip('",}')
                    if path:
                        media_tags.append(f"MEDIA:{path}")
                if "[[audio_as_voice]]" in content:
                    has_voice_directive = True
    
    return media_tags, has_voice_directive


def extract_media_tags_broken(result_messages):
    """
    The BROKEN behavior: extract MEDIA tags from ALL messages including history.
    This causes TTS voice messages to accumulate and be re-sent on every reply.
    """
    media_tags = []
    has_voice_directive = False
    
    for msg in result_messages:
        if msg.get("role") == "tool" or msg.get("role") == "function":
            content = msg.get("content", "")
            if "MEDIA:" in content:
                for match in re.finditer(r'MEDIA:(\S+)', content):
                    path = match.group(1).strip().rstrip('",}')
                    if path:
                        media_tags.append(f"MEDIA:{path}")
                if "[[audio_as_voice]]" in content:
                    has_voice_directive = True
    
    return media_tags, has_voice_directive


class TestMediaExtraction:
    """Tests for MEDIA tag extraction from tool results."""
    
    def test_media_tags_not_extracted_from_history(self):
        """MEDIA tags from previous turns should NOT be extracted again."""
        # Simulate conversation history with a TTS call from a previous turn
        history = [
            {"role": "user", "content": "Say hello as audio"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "text_to_speech"}}]},
            {"role": "tool", "tool_call_id": "1", "content": '{"success": true, "media_tag": "[[audio_as_voice]]\\nMEDIA:/path/to/audio1.ogg"}'},
            {"role": "assistant", "content": "I've said hello for you!"},
        ]
        
        # New turn: user asks a simple question
        new_messages = [
            {"role": "user", "content": "What time is it?"},
            {"role": "assistant", "content": "It's 3:30 AM."},
        ]
        
        all_messages = history + new_messages
        history_len = len(history)
        
        # Fixed behavior: should extract NO media tags (none in new messages)
        tags, voice_directive = extract_media_tags_fixed(all_messages, history_len)
        assert tags == [], "Fixed extraction should not find tags in history"
        assert voice_directive is False
        
        # Broken behavior: would incorrectly extract the old media tag
        broken_tags, broken_voice = extract_media_tags_broken(all_messages)
        assert len(broken_tags) == 1, "Broken extraction finds tags in history"
        assert "audio1.ogg" in broken_tags[0]
    
    def test_media_tags_extracted_from_current_turn(self):
        """MEDIA tags from the current turn SHOULD be extracted."""
        # History without TTS
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        
        # New turn with TTS call
        new_messages = [
            {"role": "user", "content": "Say goodbye as audio"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "2", "function": {"name": "text_to_speech"}}]},
            {"role": "tool", "tool_call_id": "2", "content": '{"success": true, "media_tag": "[[audio_as_voice]]\\nMEDIA:/path/to/audio2.ogg"}'},
            {"role": "assistant", "content": "I've said goodbye!"},
        ]
        
        all_messages = history + new_messages
        history_len = len(history)
        
        # Fixed behavior: should extract the new media tag
        tags, voice_directive = extract_media_tags_fixed(all_messages, history_len)
        assert len(tags) == 1, "Should extract media tag from current turn"
        assert "audio2.ogg" in tags[0]
        assert voice_directive is True
    
    def test_multiple_tts_calls_in_history_not_accumulated(self):
        """Multiple TTS calls in history should NOT accumulate in new responses."""
        # History with multiple TTS calls
        history = [
            {"role": "user", "content": "Say hello"},
            {"role": "tool", "tool_call_id": "1", "content": 'MEDIA:/audio/hello.ogg'},
            {"role": "assistant", "content": "Done!"},
            {"role": "user", "content": "Say goodbye"},
            {"role": "tool", "tool_call_id": "2", "content": 'MEDIA:/audio/goodbye.ogg'},
            {"role": "assistant", "content": "Done!"},
            {"role": "user", "content": "Say thanks"},
            {"role": "tool", "tool_call_id": "3", "content": 'MEDIA:/audio/thanks.ogg'},
            {"role": "assistant", "content": "Done!"},
        ]
        
        # New turn: no TTS
        new_messages = [
            {"role": "user", "content": "What time is it?"},
            {"role": "assistant", "content": "3 PM"},
        ]
        
        all_messages = history + new_messages
        history_len = len(history)
        
        # Fixed: no tags
        tags, _ = extract_media_tags_fixed(all_messages, history_len)
        assert tags == [], "Should not accumulate tags from history"
        
        # Broken: would have 3 tags (all the old ones)
        broken_tags, _ = extract_media_tags_broken(all_messages)
        assert len(broken_tags) == 3, "Broken version accumulates all history tags"
    
    def test_deduplication_within_current_turn(self):
        """Multiple MEDIA tags in current turn should be deduplicated."""
        history = []
        
        # Current turn with multiple tool calls producing same media
        new_messages = [
            {"role": "user", "content": "Multiple TTS"},
            {"role": "tool", "tool_call_id": "1", "content": 'MEDIA:/audio/same.ogg'},
            {"role": "tool", "tool_call_id": "2", "content": 'MEDIA:/audio/same.ogg'},  # duplicate
            {"role": "tool", "tool_call_id": "3", "content": 'MEDIA:/audio/different.ogg'},
            {"role": "assistant", "content": "Done!"},
        ]
        
        all_messages = history + new_messages
        
        tags, _ = extract_media_tags_fixed(all_messages, 0)
        # Even though same.ogg appears twice, deduplication happens after extraction
        # The extraction itself should get both, then caller deduplicates
        assert len(tags) == 3  # Raw extraction gets all
        
        # Deduplication as done in the actual code:
        seen = set()
        unique = [t for t in tags if t not in seen and not seen.add(t)]
        assert len(unique) == 2  # After dedup: same.ogg and different.ogg


class TestBasePlatformExtractMedia:
    """Regression tests for gateway MEDIA: tag parsing."""

    def test_extract_media_accepts_real_absolute_media_path(self):
        """A real-looking absolute media path should still be delivered."""
        media, cleaned = BasePlatformAdapter.extract_media(
            "Here is the image.\nMEDIA:/share/hermes/gallery/example.png"
        )

        assert media == [("/share/hermes/gallery/example.png", False)]
        assert "MEDIA:" not in cleaned

    def test_extract_media_accepts_document_and_platform_media_extensions(self):
        """Documented attachment types such as Markdown and HEIC must still work."""
        media, cleaned = BasePlatformAdapter.extract_media(
            "Docs:\nMEDIA:/tmp/report.md\nPhoto:\nMEDIA:/tmp/photo.heic"
        )

        assert media == [("/tmp/report.md", False), ("/tmp/photo.heic", False)]
        assert "MEDIA:" not in cleaned

    def test_extract_media_ignores_prompt_placeholder_path(self):
        """Documentation examples must not be treated as real attachments."""
        content = "Use MEDIA:/absolute/path/to/file in your response."

        media, cleaned = BasePlatformAdapter.extract_media(content)

        assert media == []
        assert cleaned == content

    def test_extract_media_ignores_angle_bracket_placeholder(self):
        """Tool hints such as MEDIA:<screenshot_path> are placeholders, not files."""
        content = "Share it with MEDIA:<screenshot_path>."

        media, cleaned = BasePlatformAdapter.extract_media(content)

        assert media == []
        assert cleaned == content

    def test_extract_media_ignores_code_spans(self):
        """MEDIA: examples inside inline or fenced code are documentation, not output."""
        content = (
            "Inline `MEDIA:/share/hermes/gallery/example.png`\n"
            "```text\nMEDIA:/share/hermes/gallery/example2.png\n```"
        )

        media, cleaned = BasePlatformAdapter.extract_media(content)

        assert media == []
        assert cleaned == content


class TestGatewayToolResultMediaExtraction:
    """Regression tests for gateway tool-result MEDIA tag scanning."""

    def test_tool_result_media_scanner_ignores_documentation_placeholders(self):
        """Inline-code MEDIA examples inside skill/tool JSON must not be appended."""
        from gateway.run import _extract_tool_result_media_tags

        messages = [
            {
                "role": "tool",
                "content": (
                    '{"content": "- **MEDIA: paths** — Raw `MEDIA:/path/to/file.jpg` '
                    'in docs is useless. Convert to descriptive references."}'
                ),
            }
        ]

        tags, has_voice = _extract_tool_result_media_tags(messages, history_media_paths=set())

        assert tags == []
        assert has_voice is False

    def test_tool_result_media_scanner_keeps_real_tts_media(self):
        """Real TTS MEDIA tags in tool results must still be appended for delivery."""
        from gateway.run import _extract_tool_result_media_tags

        messages = [
            {
                "role": "tool",
                "content": (
                    '{"success": true, "media_tag": '
                    '"[[audio_as_voice]]\\nMEDIA:/share/hermes/audio/voice.ogg"}'
                ),
            }
        ]

        tags, has_voice = _extract_tool_result_media_tags(messages, history_media_paths=set())

        assert tags == ["MEDIA:/share/hermes/audio/voice.ogg"]
        assert has_voice is True

    def test_tool_result_media_scanner_skips_history_paths(self):
        """Current tool results must not re-append paths already seen in history."""
        from gateway.run import _extract_tool_result_media_tags

        messages = [
            {"role": "tool", "content": "MEDIA:/share/hermes/audio/old.ogg"},
            {"role": "tool", "content": "MEDIA:/share/hermes/audio/new.ogg"},
        ]

        tags, has_voice = _extract_tool_result_media_tags(
            messages,
            history_media_paths={"/share/hermes/audio/old.ogg"},
        )

        assert tags == ["MEDIA:/share/hermes/audio/new.ogg"]
        assert has_voice is False

    def test_append_tool_media_when_final_response_has_placeholder_media_text(self):
        """A placeholder MEDIA mention in final text must not suppress real tool media."""
        from gateway.run import _append_missing_tool_result_media_tags

        final_response = "Docs mention `MEDIA:/path/to/file.jpg`, but that is just an example."
        messages = [
            {
                "role": "tool",
                "content": (
                    '{"success": true, "media_tag": '
                    '"[[audio_as_voice]]\\nMEDIA:/share/hermes/audio/voice.ogg"}'
                ),
            }
        ]

        updated = _append_missing_tool_result_media_tags(
            final_response,
            messages,
            history_media_paths=set(),
        )

        assert updated == (
            final_response
            + "\n[[audio_as_voice]]\nMEDIA:/share/hermes/audio/voice.ogg"
        )

    def test_append_tool_media_dedupes_against_deliverable_final_response_tags(self):
        """Existing valid final-response MEDIA tags should not block distinct tool media."""
        from gateway.run import _append_missing_tool_result_media_tags

        final_response = "Primary image:\nMEDIA:/share/hermes/gallery/primary.png"
        messages = [
            {"role": "tool", "content": "MEDIA:/share/hermes/gallery/primary.png"},
            {"role": "tool", "content": "MEDIA:/share/hermes/gallery/secondary.png"},
        ]

        updated = _append_missing_tool_result_media_tags(
            final_response,
            messages,
            history_media_paths=set(),
        )

        assert updated == final_response + "\nMEDIA:/share/hermes/gallery/secondary.png"

    def test_append_voice_tool_media_does_not_reclassify_existing_final_media(self):
        """Voice directives must not globally reclassify existing final-response media."""
        from gateway.run import _append_missing_tool_result_media_tags

        final_response = "Primary image:\nMEDIA:/share/hermes/gallery/primary.png"
        messages = [
            {
                "role": "tool",
                "content": "[[audio_as_voice]]\nMEDIA:/share/hermes/audio/voice.ogg",
            },
        ]

        updated = _append_missing_tool_result_media_tags(
            final_response,
            messages,
            history_media_paths=set(),
        )

        assert updated == final_response + "\nMEDIA:/share/hermes/audio/voice.ogg"
        assert "[[audio_as_voice]]" not in updated

    def test_append_tool_media_when_final_response_is_empty(self):
        """Tool-result media should still be deliverable without visible text."""
        from gateway.run import _append_missing_tool_result_media_tags

        messages = [
            {
                "role": "tool",
                "content": "[[audio_as_voice]]\nMEDIA:/share/hermes/audio/voice.ogg",
            },
        ]

        updated = _append_missing_tool_result_media_tags(
            "",
            messages,
            history_media_paths=set(),
        )

        assert updated == "[[audio_as_voice]]\nMEDIA:/share/hermes/audio/voice.ogg"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
