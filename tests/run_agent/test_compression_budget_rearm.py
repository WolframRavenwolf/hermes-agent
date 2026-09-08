"""Regression test for re-arming the compression budget after tool progress."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _tool_call():
    return SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(name="web_search", arguments='{"query": "x"}'),
    )


def _tool_response(prompt_tokens: int):
    message = SimpleNamespace(
        content=None,
        reasoning_content=None,
        reasoning=None,
        tool_calls=[_tool_call()],
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
        model="test/model",
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=1,
            total_tokens=prompt_tokens + 1,
        ),
    )


def _final_response():
    message = SimpleNamespace(
        content="done",
        reasoning_content=None,
        reasoning=None,
        tool_calls=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model",
        usage=None,
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
    ("prompt_tokens", "expected_compactions", "provider_recovery"),
    [(50, 1, False), (150, 1, False), (50, 2, True)],
    ids=[
        "pressure-cleared-anchored-no-recompaction",
        "pressure-still-high-stays-capped",
        "pressure-cleared-rearms-after-provider-recovery",
    ],
)
def test_pre_api_compression_budget_rearms_only_after_pressure_clears(
    prompt_tokens: int,
    expected_compactions: int,
    provider_recovery: bool,
):
    """Only provider-confirmed headroom starts a new pressure episode.

    Usage-anchored accounting update: once the provider reports
    ``prompt_tokens=50`` for the full transcript, later pre-API checks anchor
    on that real reading plus a delta estimate of the few appended messages —
    the scripted whole-history rough estimate (200) no longer drives the
    decision, so the pressure-cleared case performs exactly ONE compaction
    (the pre-anchor one). The budget-rearm mechanics remain covered by the
    provider-recovery variant, whose first response carries no usage (no
    anchor) and therefore still compacts on the rough estimate.
    """
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
    responses = [_tool_response(prompt_tokens), _final_response()]
    if provider_recovery:
        responses.insert(0, _malformed_response())
        agent._fallback_chain = [object()]
        agent._try_activate_fallback = MagicMock(return_value=True)
    agent.client.chat.completions.create.side_effect = responses
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent._disable_streaming = True
    agent.tool_delay = 0
    agent.save_trajectories = False
    agent.max_compression_attempts = 1

    compressor = MagicMock()
    compressor.protect_first_n = 3
    compressor.protect_last_n = 20
    compressor.threshold_tokens = 100
    compressor.context_length = 1_000
    compressor.last_prompt_tokens = -1
    compressor._verify_compaction_cleared_threshold = False
    compressor.awaiting_real_usage_after_compression = False
    compressor.should_compress.side_effect = lambda tokens: tokens >= 100
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

    compress_calls = []

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
    ):
        result = agent.run_conversation("do a lot of tool work", conversation_history=history)

    assert result["completed"] is True
    assert result["final_response"] == "done"
    assert len(compress_calls) == expected_compactions, (
        "same-turn compression must re-arm only after the provider confirms "
        f"headroom; got {len(compress_calls)} compactions for "
        f"prompt_tokens={prompt_tokens}"
    )


@pytest.mark.parametrize("first_compaction_site", ["pre_api", "prologue"])
@pytest.mark.parametrize(
    ("first_usage", "completed_boundary", "expected_recovery"),
    [(5_000, True, True), (None, True, False), (0, True, False),
     (10_000, True, False), (5_000, False, False)],
    ids=["confirmed", "no-usage-one-shot", "zero-usage-one-shot",
         "still-pressured-one-shot", "no-completed-boundary"],
)
def test_completed_compaction_survives_fallback_and_noop(
    first_compaction_site, first_usage, completed_boundary, expected_recovery,
):
    """A new pressure episode must compact BEFORE the next provider request.

    Exercise the real compressor's update_model/update_from_response and the
    native usage anchor, not a mock latch or a scripted whole-history token
    sequence. A small completed rewrite still looks pressured locally. After
    fallback, an unproductive pass cannot erase that pending boundary. Only
    its next successful provider response can confirm it; a later fitting
    response after no-usage/high-usage must not resurrect the old verdict.
    """
    from contextlib import nullcontext

    from agent.model_metadata import anchored_context_tokens, estimate_request_tokens_rough

    with (
        patch("run_agent.get_tool_definitions", return_value=[_tool_definition()]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("agent.model_metadata.get_model_context_length", return_value=100_000),
        patch("agent.context_compressor.get_model_context_length", return_value=100_000),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            max_iterations=8,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent._disable_streaming = True
    agent.tool_delay = 0
    agent.save_trajectories = False
    agent.max_compression_attempts = 1
    agent.compression_enabled = True
    compressor = agent.context_compressor
    compressor.threshold_tokens = 10_000
    events = []
    compaction_attempts = []
    feedback_seen = []
    growth_pressure = []

    def compress(messages, _system_message, **_kwargs):
        compaction_attempts.append(len(feedback_seen))
        # The second pass (on the fallback) finds no eligible rewrite. It
        # cannot qualify for rearming by itself, nor erase an older completed
        # boundary whose first successful response is still outstanding.
        compressor._verify_compaction_cleared_threshold = False
        if len(compaction_attempts) == 2 or not completed_boundary:
            events.append("noop")
            return messages, "You are helpful."
        events.append("compact")
        if len(compaction_attempts) == 1:
            compacted = [dict(m, content=m["content"][:76_000]) for m in messages]
        else:
            compacted = [dict(m, content="[summarized]") if len(m.get("content") or "") > 5_000 else m for m in messages]
        compressor.record_completed_compaction()
        compressor.last_compression_rough_tokens = estimate_request_tokens_rough(compacted)
        compressor.last_prompt_tokens = -1
        compressor.awaiting_real_usage_after_compression = True
        agent._usage_anchor = None
        return compacted, "You are helpful."

    def fallback():
        events.append("fallback")
        # Invoke the real model-scoped reset, rather than toggling invented
        # mock properties. No provider resolution or credential access.
        compressor.update_model("test/fallback", 100_000, provider=agent.provider)
        compressor.threshold_tokens = 10_000
        assert not compressor._verify_compaction_cleared_threshold
        assert not compressor.awaiting_real_usage_after_compression
        agent.model = "test/fallback"
        agent._fallback_index = 1
        agent._usage_anchor = None
        return True

    agent._fallback_chain = [object()]
    agent._try_activate_fallback = MagicMock(side_effect=fallback)
    responses = [_malformed_response()]
    first_response = _tool_response(first_usage or 0)
    if first_usage is None:
        first_response.usage = None
    responses.append(first_response)
    # For negative one-shot cases, an unrelated later fitting response must
    # not restore the pending verdict. Keep the intervening tool result tiny.
    if first_usage != 5_000:
        later = _tool_response(5_000)
        later.choices[0].message.tool_calls[0].id = "call_later"
        later.choices[0].message.tool_calls[0].function.arguments = '{"query": "later"}'
        responses.append(later)
    responses.append(_final_response())
    response_iter = iter(responses)

    def respond(**_kwargs):
        response = next(response_iter)
        events.append("request" if not response.choices else "response")
        if response.choices and response.choices[0].finish_reason == "tool_calls":
            feedback_seen.append(response.usage)
        return response

    agent.client.chat.completions.create.side_effect = respond

    def execute_tools(assistant_message, messages, *_args):
        tc = assistant_message.tool_calls[0]
        # The final tool response creates REAL appended-token pressure, which
        # the 0.21 anchor must include; the old rough-only 0.20 oracle would
        # miss the distinction between confirmed headroom and new growth.
        grow = len(feedback_seen) == len(responses) - 2
        messages.append({
            "role": "tool", "name": tc.function.name,
            "tool_call_id": tc.id, "content": "g" * 90_000 if grow else "ok",
        })
        events.append("growth" if grow else "small-tool")
        if grow:
            pressure = anchored_context_tokens(messages, agent._usage_anchor)
            assert pressure is not None and pressure > compressor.threshold_tokens
            assert not compressor.should_defer_preflight_to_real_usage(pressure)
            assert compressor.should_compress(pressure)
            growth_pressure.append(pressure)

    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 80_000 if i == 0 else f"msg {i}"}
        for i in range(30)
    ]
    with (
        patch("agent.turn_context.estimate_request_tokens_rough", return_value=10)
        if first_compaction_site == "pre_api" else nullcontext(),
        patch.object(agent, "_compress_context", side_effect=compress),
        patch.object(agent, "_execute_tool_calls", side_effect=execute_tools),
        patch.object(agent, "_flush_messages_to_session_db", return_value=True),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("continue working", conversation_history=history)

    assert result["completed"] is True
    assert result["final_response"] == "done"
    agent._try_activate_fallback.assert_called_once_with()
    assert growth_pressure
    assert events[:4] == ["compact" if completed_boundary else "noop", "request", "fallback", "noop"]
    after_growth = events[events.index("growth") + 1:]
    assert after_growth == (["compact", "response"] if expected_recovery else ["response"]), events


def test_prologue_recovery_clears_block_even_with_unspent_loop_budget():
    """A prologue boundary can block preflight before the loop spends a token."""
    from agent.model_metadata import anchored_context_tokens

    with (
        patch("run_agent.get_tool_definitions", return_value=[_tool_definition()]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("agent.context_compressor.get_model_context_length", return_value=100_000),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890", model="test/model",
            base_url="https://openrouter.ai/api/v1", quiet_mode=True,
            skip_context_files=True, skip_memory=True, max_iterations=5,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent._disable_streaming = True
    agent.tool_delay = 0
    agent.save_trajectories = False
    agent.compression_enabled = True
    agent.max_compression_attempts = 1
    compressor = agent.context_compressor
    compressor.threshold_tokens = 10_000
    events = []

    def compress(messages, _system_message, **_kwargs):
        events.append("compact")
        compacted = [dict(m, content=m["content"][:76_000]) for m in messages]
        compressor.record_completed_compaction()
        compressor.last_prompt_tokens = -1
        compressor.awaiting_real_usage_after_compression = True
        agent._usage_anchor = None
        return compacted, "You are helpful."

    responses = iter([_tool_response(5_000), _final_response()])

    def respond(**_kwargs):
        events.append("response")
        return next(responses)

    agent.client.chat.completions.create.side_effect = respond

    def execute_tools(assistant_message, messages, *_args):
        tc = assistant_message.tool_calls[0]
        messages.append({"role": "tool", "name": tc.function.name,
                         "tool_call_id": tc.id, "content": "g" * 90_000})
        pressure = anchored_context_tokens(messages, agent._usage_anchor)
        assert pressure > compressor.threshold_tokens
        assert not compressor.should_defer_preflight_to_real_usage(pressure)
        assert compressor.should_compress(pressure)
        events.append("growth")

    history = [{"role": "user", "content": "x" * 80_000},
               {"role": "assistant", "content": "working"}]
    with (
        patch.object(agent, "_compress_context", side_effect=compress),
        patch.object(agent, "_execute_tool_calls", side_effect=execute_tools),
        patch.object(agent, "_flush_messages_to_session_db", return_value=True),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("continue working", conversation_history=history)

    assert result["completed"] is True
    assert events == ["compact", "response", "growth", "compact", "response"]
