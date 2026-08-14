"""Behavioral tests for provider-confirmed compression-budget rearming.

``compression_attempts`` is a shared per-turn backstop (pre-API gate,
overflow/413 handlers, post-tool gate). Before the rearm fix, successful
pre-API compactions consumed it permanently: a marathon tool turn burned all
attempts on compactions that worked, the pre-API gate went dark for the rest
of the turn, and context could grow until the provider rejected the request
with ``max compression attempts``.

The budget is rearmed only when a completed history compaction is followed by
a real provider prompt count below the configured threshold. Rough estimates,
usage-less responses, no-progress compactions, and output-cap retries cannot
reopen the anti-thrash cap.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.conversation_loop import _should_rearm_compression_budget
from run_agent import AIAgent


class TestRearmDecision:
    def test_provider_confirmed_recovery_rearms(self):
        assert _should_rearm_compression_budget(
            2,
            completed_compaction_pending=True,
            prompt_tokens=7_999,
            threshold_tokens=10_000,
        )

    def test_output_cap_attempt_cannot_masquerade_as_history_recovery(self):
        assert not _should_rearm_compression_budget(
            2,
            completed_compaction_pending=False,
            prompt_tokens=7_999,
            threshold_tokens=10_000,
        )

    @pytest.mark.parametrize(
        ("attempts", "pending", "prompt_tokens", "threshold_tokens"),
        [
            (0, True, 7_999, 10_000),
            (2, False, 7_999, 10_000),
            (2, True, 0, 10_000),
            (2, True, 10_000, 10_000),
            (2, True, 10_001, 10_000),
            (2, True, 7_999, 0),
        ],
    )
    def test_unverified_or_pressured_response_keeps_budget_burned(
        self, attempts, pending, prompt_tokens, threshold_tokens
    ):
        assert not _should_rearm_compression_budget(
            attempts,
            completed_compaction_pending=pending,
            prompt_tokens=prompt_tokens,
            threshold_tokens=threshold_tokens,
        )


def _tool_call(i: int):
    return SimpleNamespace(
        id=f"call_{i}",
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


def _tool_response(i: int, prompt_tokens: int | None):
    msg = SimpleNamespace(
        content=None,
        reasoning_content=None,
        reasoning=None,
        tool_calls=[_tool_call(i)],
    )
    choice = SimpleNamespace(message=msg, finish_reason="tool_calls")
    return SimpleNamespace(
        choices=[choice], model="test/model", usage=_usage(prompt_tokens)
    )


def _stop_response(prompt_tokens: int | None):
    msg = SimpleNamespace(
        content="done",
        reasoning_content=None,
        reasoning=None,
        tool_calls=None,
    )
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(
        choices=[choice], model="test/model", usage=_usage(prompt_tokens)
    )


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


THRESHOLD = 10_000

# Large enough to cross the pre-API threshold on each tool iteration, while
# remaining below the per-result persistence truncation threshold.
BIG_TOOL_RESULT = "x" * 60_000


def _coherent_compressor() -> MagicMock:
    compressor = MagicMock()
    compressor.protect_first_n = 3
    compressor.protect_last_n = 20
    compressor.threshold_tokens = THRESHOLD
    compressor.context_length = 200_000
    compressor.last_prompt_tokens = 0
    compressor._verify_compaction_cleared_threshold = False
    compressor.awaiting_real_usage_after_compression = False
    compressor.should_compress.side_effect = lambda tokens=None: (
        tokens or 0
    ) >= THRESHOLD
    compressor.should_defer_preflight_to_real_usage.return_value = False
    compressor.get_active_compression_failure_cooldown.return_value = None

    def _update_from_response(usage):
        compressor.last_prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        compressor._verify_compaction_cleared_threshold = False
        compressor.awaiting_real_usage_after_compression = False

    compressor.update_from_response.side_effect = _update_from_response
    return compressor


@pytest.fixture()
def agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        instance = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            max_iterations=20,
        )
    instance.client = MagicMock()
    instance._cached_system_prompt = "You are helpful."
    instance._use_prompt_caching = False
    instance._disable_streaming = True
    instance.tool_delay = 0
    instance.save_trajectories = False
    instance.compression_enabled = True
    instance.context_compressor = _coherent_compressor()
    return instance


def _run_marathon_turn(
    agent, n_tool_iterations: int, *, provider_prompt_tokens: int | None
):
    """Drive one turn through repeated, separated context-pressure episodes."""
    responses = [
        _tool_response(i, provider_prompt_tokens) for i in range(n_tool_iterations)
    ]
    responses.append(_stop_response(provider_prompt_tokens))
    agent.client.chat.completions.create.side_effect = responses

    compress_calls = []

    def _fake_compress(messages, _system_message, **_kwargs):
        compress_calls.append(len(messages))
        agent.context_compressor._verify_compaction_cleared_threshold = True
        agent.context_compressor.awaiting_real_usage_after_compression = True
        compacted = [
            dict(message, content="[summarized]")
            if isinstance(message, dict)
            and len(str(message.get("content") or "")) > 5_000
            else message
            for message in messages
        ]
        return compacted, "compressed prompt"

    with (
        patch.object(agent, "_compress_context", side_effect=_fake_compress),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch(
            "run_agent.handle_function_call",
            lambda name, args, task_id=None, **kwargs: json.dumps(
                {"ok": True, "payload": BIG_TOOL_RESULT}
            ),
        ),
    ):
        result = agent.run_conversation("do a lot of tool work")

    return result, compress_calls


class TestCompressionBudgetRearm:
    def test_marathon_turn_completes_after_more_than_three_recoveries(self, agent):
        """Eight successful pressure/compaction/recovery phases finish."""
        assert agent.max_compression_attempts == 3
        result, compress_calls = _run_marathon_turn(
            agent,
            n_tool_iterations=8,
            provider_prompt_tokens=THRESHOLD - 1,
        )

        assert result["completed"] is True
        assert result["final_response"] == "done"
        assert len(compress_calls) > 3, (
            "provider-confirmed compactions must rearm the same-turn budget; "
            f"got only {len(compress_calls)} compactions for 8 pressure phases"
        )

    @pytest.mark.parametrize("provider_prompt_tokens", [None, THRESHOLD])
    def test_unverified_or_pressured_compaction_stays_capped(
        self, agent, provider_prompt_tokens
    ):
        """Missing usage or real usage at threshold cannot recycle the cap."""
        result, compress_calls = _run_marathon_turn(
            agent,
            n_tool_iterations=8,
            provider_prompt_tokens=provider_prompt_tokens,
        )

        assert result["completed"] is True
        assert len(compress_calls) <= agent.max_compression_attempts, (
            "without provider-confirmed headroom the per-turn cap must hold; "
            f"got {len(compress_calls)} compactions"
        )
