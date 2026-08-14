"""Regression test for re-arming the compression budget after tool progress."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.conversation_loop import _provider_confirms_completed_compaction
from run_agent import AIAgent


def _tool_call():
    return SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(name="web_search", arguments='{"query": "x"}'),
    )


def _usage(prompt_tokens: int | None):
    if prompt_tokens is None:
        return None
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=1,
        total_tokens=prompt_tokens + 1,
    )


def _tool_response(prompt_tokens: int | None):
    message = SimpleNamespace(
        content=None,
        reasoning_content=None,
        reasoning=None,
        tool_calls=[_tool_call()],
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
        model="test/model",
        usage=_usage(prompt_tokens),
    )


def _final_response(prompt_tokens: int | None = None):
    message = SimpleNamespace(
        content="done",
        reasoning_content=None,
        reasoning=None,
        tool_calls=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model",
        usage=_usage(prompt_tokens),
    )


def _malformed_response():
    return SimpleNamespace(choices=[], model="test/model", usage=None)


def _tool_definition():
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }


@pytest.mark.parametrize(
    (
        "prompt_tokens",
        "later_prompt_tokens",
        "expected_compactions",
        "provider_recovery",
    ),
    [
        (50, None, 2, False),
        (150, None, 1, False),
        (50, None, 2, True),
        (None, 50, 1, True),
    ],
    ids=[
        "pressure-cleared-rearms",
        "pressure-still-high-stays-capped",
        "pressure-cleared-rearms-after-provider-recovery",
        "fallback-usage-less-verdict-is-one-shot",
    ],
)
def test_pre_api_compression_budget_rearms_only_after_pressure_clears(
    prompt_tokens: int | None,
    later_prompt_tokens: int | None,
    expected_compactions: int,
    provider_recovery: bool,
):
    """Only provider-confirmed headroom starts a new pressure episode."""
    with (
        patch("run_agent.get_tool_definitions", return_value=[_tool_definition()]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("agent.model_metadata.get_model_context_length", return_value=256_000),
        patch("agent.context_compressor.get_model_context_length", return_value=256_000),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            max_iterations=6,
        )

    agent.client = MagicMock()
    responses = [_tool_response(prompt_tokens), _final_response(later_prompt_tokens)]
    if provider_recovery:
        responses.insert(0, _malformed_response())
        agent._fallback_chain = [object()]

        def _activate_like_real_fallback():
            # Production fallback activation calls ContextCompressor.update_model(),
            # which invalidates the previous model's pending-verdict state.
            compressor._verify_compaction_cleared_threshold = False
            compressor.awaiting_real_usage_after_compression = False
            compressor.last_prompt_tokens = 0
            compressor.last_real_prompt_tokens = 0
            compressor.last_rough_tokens_when_real_prompt_fit = 0
            compressor.last_compression_rough_tokens = 0
            compressor._last_compression_made_progress = False
            compressor.threshold_tokens = 100
            return True

        agent._try_activate_fallback = MagicMock(
            side_effect=_activate_like_real_fallback
        )
    agent.client.chat.completions.create.side_effect = responses
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent._disable_streaming = True
    agent.tool_delay = 0
    agent.save_trajectories = False
    agent.max_compression_attempts = 1

    compress_calls = []
    compressor = MagicMock()
    compressor.protect_first_n = 3
    compressor.protect_last_n = 20
    compressor.threshold_tokens = 100
    compressor.context_length = 1_000
    compressor.last_prompt_tokens = -1
    compressor._verify_compaction_cleared_threshold = False
    compressor.awaiting_real_usage_after_compression = False
    compressor.should_compress.side_effect = lambda tokens: (
        tokens >= compressor.threshold_tokens
        and (later_prompt_tokens is None or not compress_calls)
    )
    compressor.should_compress_info.return_value = (False, None)
    compressor.should_compress_preflight.return_value = False
    compressor.should_defer_preflight_to_real_usage.return_value = False
    compressor.get_active_compression_failure_cooldown.return_value = None
    compressor.select_context.return_value = None
    compressor.get_automatic_compaction_status_message.return_value = ""

    def _update_from_response(usage):
        # Mirror the real compressor: the next provider usage reading
        # consumes the completed-compaction verification latch.
        compressor.last_prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        compressor._verify_compaction_cleared_threshold = False
        compressor.awaiting_real_usage_after_compression = False

    compressor.update_from_response.side_effect = _update_from_response
    agent.compression_enabled = True
    agent.context_compressor = compressor

    estimate_values = iter([200, 190, 200, 10])
    _last_estimate = [10]

    def _next_estimate(*_args, **_kwargs):
        # The provider-recovery variant re-runs the pre-API preflight after
        # fallback activation (#84733), consuming an extra estimate reading.
        # Hold the final low-pressure value once the scripted sequence is
        # exhausted instead of raising StopIteration.
        try:
            _last_estimate[0] = next(estimate_values)
        except StopIteration:
            pass
        return _last_estimate[0]

    def _fake_compress(messages, _system_message, **_kwargs):
        compress_calls.append(messages)
        # Arm the same provider-verification boundary the real compression
        # path arms after a completed compaction.
        compressor._verify_compaction_cleared_threshold = True
        compressor.awaiting_real_usage_after_compression = True
        return list(messages), "compressed prompt"

    def _fake_execute_tool_calls(assistant_message, messages, *_args):
        tool_call = assistant_message.tool_calls[0]
        messages.append(
            {
                "role": "tool",
                "name": tool_call.function.name,
                "tool_call_id": tool_call.id,
                "content": "ok",
            }
        )

    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
        for i in range(30)
    ]

    with (
        patch(
            "agent.turn_context.estimate_request_tokens_rough",
            return_value=10,
        ),
        patch(
            "agent.conversation_loop.estimate_messages_tokens_rough",
            side_effect=_next_estimate,
        ),
        patch(
            "agent.conversation_loop._estimate_tools_tokens_rough",
            return_value=0,
        ),
        patch.object(agent, "_compress_context", side_effect=_fake_compress),
        patch.object(agent, "_execute_tool_calls", side_effect=_fake_execute_tool_calls),
        patch.object(agent, "_flush_messages_to_session_db", return_value=True),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch(
            "agent.conversation_loop._provider_confirms_completed_compaction",
            wraps=_provider_confirms_completed_compaction,
        ) as provider_confirmation,
    ):
        result = agent.run_conversation("do a lot of tool work", conversation_history=history)

    assert result["completed"] is True
    assert result["final_response"] == "done"
    if provider_recovery:
        agent._try_activate_fallback.assert_called_once_with()
    if later_prompt_tokens is not None:
        assert provider_confirmation.call_args.kwargs[
            "completed_compaction_pending"
        ] is False, "a usage-less successful response must consume the verdict"
    assert len(compress_calls) == expected_compactions, (
        "same-turn compression must re-arm only after the provider confirms "
        f"headroom; got {len(compress_calls)} compactions for "
        f"prompt_tokens={prompt_tokens}"
    )
