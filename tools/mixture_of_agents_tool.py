#!/usr/bin/env python3
"""
Mixture-of-Agents Tool Module

This module implements a provider-aware Mixture-of-Agents (MoA) workflow:
multiple frontier models generate independent reference answers in parallel,
then a current top-tier judge/aggregator model synthesizes the final answer.

Based on the research paper: "Mixture-of-Agents Enhances Large Language Model
Capabilities" by Junlin Wang et al. (arXiv:2406.04692v1).

Architecture:
1. Reference models generate diverse initial responses in parallel.
2. A judge/aggregator model synthesizes those responses into one answer.
3. The reference roster can mix direct subscription-backed providers and paid
   aggregators, avoiding unnecessary OpenRouter spend for models available via
   first-party subscriptions.

Default Models:
- References:
  - GPT-5.5 via OpenAI Codex / ChatGPT subscription
  - Claude Opus 4.6 via Anthropic subscription
  - Gemini 3.1 Pro Preview via OpenRouter
  - DeepSeek V4 Pro via OpenRouter
  - Kimi K2.6 via OpenRouter
- Aggregator/Judge: the current configured main model (usually GPT-5.5 or
  Claude Opus 4.6), resolved at call time.
"""

from __future__ import annotations

import json
import logging
import asyncio
import datetime
from typing import Dict, Any, List, Optional, Union

from agent.auxiliary_client import (
    OMIT_TEMPERATURE,
    _fixed_temperature_for_model,
    resolve_provider_client,
)
from agent.auxiliary_client import extract_content_or_reasoning
from tools.debug_helpers import DebugSession
import sys

logger = logging.getLogger(__name__)

ModelSpec = Union[str, Dict[str, str]]

# Provider-aware reference models.  Strings remain accepted at runtime for
# backwards compatibility, but defaults should use explicit provider routing so
# subscription-backed models do not accidentally go through OpenRouter.
REFERENCE_MODELS: List[Dict[str, str]] = [
    {"provider": "openai-codex", "model": "gpt-5.5"},
    {"provider": "anthropic", "model": "claude-opus-4.6"},
    {"provider": "openrouter", "model": "google/gemini-3.1-pro-preview"},
    {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
    {"provider": "openrouter", "model": "moonshotai/kimi-k2.6"},
]

# The judge should track the currently configured top model.  Resolving this at
# call time means switching Hermes from GPT to Claude (or back) automatically
# changes the MoA judge without another code edit.
AGGREGATOR_MODEL: Dict[str, str] = {"provider": "main", "model": "current"}

# Fallback judge if the runtime config cannot be read.
FALLBACK_AGGREGATOR_MODEL: Dict[str, str] = {"provider": "openai-codex", "model": "gpt-5.5"}
PROVIDER_DEFAULT_JUDGES: Dict[str, str] = {
    "openai-codex": "gpt-5.5",
    "anthropic": "claude-opus-4.6",
}

# Temperature settings optimized for MoA performance.
REFERENCE_TEMPERATURE = 0.6  # Balanced creativity for diverse perspectives
AGGREGATOR_TEMPERATURE = 0.4  # Focused synthesis for consistency

# Failure handling configuration.
MIN_SUCCESSFUL_REFERENCES = 1  # Minimum successful reference models needed to proceed

# Maximum-reasoning default passed to providers/endpoints that support it.
DEFAULT_REASONING_CONFIG: Dict[str, Any] = {"enabled": True, "effort": "xhigh"}

# System prompt for the aggregator model (from the research paper, with
# provider wording modernized because our references are not all open-source).
AGGREGATOR_SYSTEM_PROMPT = """You have been provided with a set of responses from various frontier models to the latest user query. Your task is to synthesize these responses into a single, high-quality response. It is crucial to critically evaluate the information provided in these responses, recognizing that some of it may be biased or incorrect. Your response should not simply replicate the given answers but should offer a refined, accurate, and comprehensive reply to the instruction. Ensure your response is well-structured, coherent, and adheres to the highest standards of accuracy and reliability.

Responses from models:"""

_debug = DebugSession("moa_tools", env_var="MOA_TOOLS_DEBUG")


def _construct_aggregator_prompt(system_prompt: str, responses: List[str]) -> str:
    """Construct the final system prompt for the aggregator."""
    response_text = "\n".join([f"{i+1}. {response}" for i, response in enumerate(responses)])
    return f"{system_prompt}\n\n{response_text}"


def _infer_provider_for_model(model: str) -> str:
    """Best-effort provider inference for legacy model-only configs."""
    model_lower = (model or "").strip().lower()
    if not model_lower:
        return FALLBACK_AGGREGATOR_MODEL["provider"]
    if "/" in model_lower:
        # Vendor/model slugs are aggregator-native; preserve them through OpenRouter
        # rather than pairing them with a first-party subscription provider.
        return "openrouter"
    if model_lower.startswith("gpt-"):
        return "openai-codex"
    if model_lower.startswith("claude-"):
        return "anthropic"
    return "openrouter"


def _read_current_main_model_spec() -> Dict[str, str]:
    """Return the current configured main provider/model for judge routing.

    This intentionally reads config.yaml at call time so `/model` or config
    changes are picked up without changing the MoA source.  If config is absent
    or incomplete, fall back to the subscription-backed GPT-5.5 route.
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        model_cfg = cfg.get("model", {})
        if isinstance(model_cfg, dict):
            provider = str(model_cfg.get("provider") or "").strip().lower()
            model = str(model_cfg.get("default") or model_cfg.get("model") or "").strip()
        elif isinstance(model_cfg, str):
            provider = "auto"
            model = model_cfg.strip()
        else:
            provider = ""
            model = ""
    except Exception as exc:
        logger.debug("Could not read current main model for MoA judge: %s", exc)
        provider = ""
        model = ""

    if not provider or provider == "auto":
        provider = _infer_provider_for_model(model)
    if not model or model == "current":
        model = PROVIDER_DEFAULT_JUDGES.get(provider)
        if not model:
            return dict(FALLBACK_AGGREGATOR_MODEL)
    return {"provider": provider, "model": model}


def _normalize_model_spec(spec: ModelSpec) -> Dict[str, str]:
    """Normalize a model spec into `{provider, model}` form.

    Backwards compatibility: a bare string is interpreted as an OpenRouter
    model slug, matching the original MoA tool semantics.
    """
    if isinstance(spec, str):
        return {"provider": "openrouter", "model": spec.strip()}
    if not isinstance(spec, dict):
        raise TypeError(f"Invalid MoA model spec {spec!r}; expected string or dict")

    provider = str(spec.get("provider") or "openrouter").strip().lower()
    model = str(spec.get("model") or "").strip()
    if not provider:
        provider = "openrouter"
    if not model:
        raise ValueError(f"MoA model spec missing model: {spec!r}")
    return {"provider": provider, "model": model}


def _resolve_runtime_model_spec(spec: ModelSpec) -> Dict[str, str]:
    """Normalize a spec and resolve `{main,current}` to the live main model."""
    normalized = _normalize_model_spec(spec)
    if normalized["provider"] == "main" or normalized["model"] == "current":
        return _read_current_main_model_spec()
    return normalized


def _model_key(spec: Dict[str, str]) -> str:
    """Stable human/log key for a provider-aware model spec."""
    return f"{spec['provider']}/{spec['model']}"


def _model_public_spec(spec: ModelSpec) -> Dict[str, str]:
    """Return a JSON-serializable resolved provider/model spec."""
    return dict(_resolve_runtime_model_spec(spec))


def _temperature_for_model(
    spec: Dict[str, str],
    resolved_model: str,
    client: Any,
    requested_temperature: Optional[float],
) -> Optional[float]:
    """Return the temperature to send, or None to omit it.

    Some models/providers reject custom temperature values (Codex/GPT family,
    Kimi server-managed routes, selected provider endpoints).  This centralizes
    the omission logic so MoA does not generate avoidable 400s.
    """
    if requested_temperature is None:
        return None

    provider = spec.get("provider", "")
    model_lower = (resolved_model or spec.get("model", "")).lower()
    if provider == "openai-codex" or model_lower.startswith("gpt-") or "/gpt-" in model_lower:
        return None

    try:
        fixed = _fixed_temperature_for_model(resolved_model or spec.get("model"), getattr(client, "base_url", ""))
        if fixed is OMIT_TEMPERATURE:
            return None
        if fixed is not None:
            return fixed
    except Exception:
        # Temperature is a quality hint, not a hard requirement.  If the helper
        # fails, fall back to the caller's requested value.
        pass

    return requested_temperature


def _build_api_params(
    *,
    spec: Dict[str, str],
    resolved_model: str,
    client: Any,
    messages: List[Dict[str, str]],
    temperature: Optional[float],
    max_tokens: Optional[int],
) -> Dict[str, Any]:
    """Build chat.completions-compatible kwargs for a routed MoA call."""
    api_params: Dict[str, Any] = {
        "model": resolved_model,
        "messages": messages,
        "extra_body": {"reasoning": dict(DEFAULT_REASONING_CONFIG)},
    }
    if max_tokens is not None:
        api_params["max_tokens"] = max_tokens

    resolved_temperature = _temperature_for_model(spec, resolved_model, client, temperature)
    if resolved_temperature is not None:
        api_params["temperature"] = resolved_temperature

    return api_params


async def _query_model_spec(
    spec: ModelSpec,
    messages: List[Dict[str, str]],
    temperature: Optional[float],
    max_tokens: Optional[int],
) -> tuple[Dict[str, str], str, str]:
    """Resolve a provider-aware model spec, execute one async model call."""
    runtime_spec = _resolve_runtime_model_spec(spec)
    client, resolved_model = resolve_provider_client(
        runtime_spec["provider"], runtime_spec["model"], async_mode=True
    )
    if client is None or not resolved_model:
        raise RuntimeError(
            f"No client available for MoA model {_model_key(runtime_spec)}"
        )

    api_params = _build_api_params(
        spec=runtime_spec,
        resolved_model=resolved_model,
        client=client,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    response = await client.chat.completions.create(**api_params)
    content = extract_content_or_reasoning(response)
    return runtime_spec, resolved_model, content


async def _run_reference_model_safe(
    model: ModelSpec,
    user_prompt: str,
    temperature: float = REFERENCE_TEMPERATURE,
    max_tokens: int = 32000,
    max_retries: int = 6,
) -> tuple[str, str, bool]:
    """Run a single reference model with retry logic and graceful failure."""
    runtime_spec = _resolve_runtime_model_spec(model)
    model_name = _model_key(runtime_spec)

    for attempt in range(max_retries):
        try:
            logger.info("Querying %s (attempt %s/%s)", model_name, attempt + 1, max_retries)

            _runtime_spec, _resolved_model, content = await _query_model_spec(
                runtime_spec,
                [{"role": "user", "content": user_prompt}],
                temperature,
                max_tokens,
            )
            model_name = _model_key(_runtime_spec)

            if not content:
                logger.warning(
                    "%s returned empty content (attempt %s/%s), retrying",
                    model_name,
                    attempt + 1,
                    max_retries,
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(min(2 ** (attempt + 1), 60))
                    continue
            logger.info("%s responded (%s characters)", model_name, len(content))
            return model_name, content, True

        except Exception as e:
            error_str = str(e)
            if "invalid" in error_str.lower():
                logger.warning("%s invalid request error (attempt %s): %s", model_name, attempt + 1, error_str)
            elif "rate" in error_str.lower() or "limit" in error_str.lower():
                logger.warning("%s rate limit error (attempt %s): %s", model_name, attempt + 1, error_str)
            else:
                logger.warning("%s unknown error (attempt %s): %s", model_name, attempt + 1, error_str)

            if attempt < max_retries - 1:
                sleep_time = min(2 ** (attempt + 1), 60)
                logger.info("Retrying in %ss...", sleep_time)
                await asyncio.sleep(sleep_time)
            else:
                error_msg = f"{model_name} failed after {max_retries} attempts: {error_str}"
                logger.error("%s", error_msg, exc_info=True)
                return model_name, error_msg, False


async def _run_aggregator_model(
    system_prompt: str,
    user_prompt: str,
    temperature: float = AGGREGATOR_TEMPERATURE,
    max_tokens: int = 32000,
    aggregator_model: Optional[ModelSpec] = None,
) -> str:
    """Run the judge/aggregator model to synthesize the final response."""
    judge_spec = _resolve_runtime_model_spec(aggregator_model or AGGREGATOR_MODEL)
    logger.info("Running aggregator model: %s", _model_key(judge_spec))

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    _runtime_spec, _resolved_model, content = await _query_model_spec(
        judge_spec,
        messages,
        temperature,
        max_tokens,
    )

    # Retry once on empty content (reasoning-only response or transient stream issue).
    if not content:
        logger.warning("Aggregator returned empty content, retrying once")
        _runtime_spec, _resolved_model, content = await _query_model_spec(
            judge_spec,
            messages,
            temperature,
            max_tokens,
        )

    logger.info("Aggregation complete (%s characters)", len(content))
    return content


async def mixture_of_agents_tool(
    user_prompt: str,
    reference_models: Optional[List[ModelSpec]] = None,
    aggregator_model: Optional[ModelSpec] = None,
) -> str:
    """Process a complex query using the Mixture-of-Agents methodology.

    Default architecture: five provider-aware reference models in parallel,
    followed by one current-main-model judge call (six model calls total).
    """
    start_time = datetime.datetime.now()

    ref_models: List[ModelSpec] = reference_models or REFERENCE_MODELS
    judge_model: ModelSpec = aggregator_model or AGGREGATOR_MODEL

    debug_call_data = {
        "parameters": {
            "user_prompt": user_prompt[:200] + "..." if len(user_prompt) > 200 else user_prompt,
            "reference_models": [_model_public_spec(m) for m in ref_models],
            "aggregator_model": _model_public_spec(judge_model),
            "reference_temperature": REFERENCE_TEMPERATURE,
            "aggregator_temperature": AGGREGATOR_TEMPERATURE,
            "min_successful_references": MIN_SUCCESSFUL_REFERENCES,
        },
        "error": None,
        "success": False,
        "reference_responses_count": 0,
        "failed_models_count": 0,
        "failed_models": [],
        "final_response_length": 0,
        "processing_time_seconds": 0,
        "models_used": {},
    }

    try:
        logger.info("Starting Mixture-of-Agents processing...")
        logger.info("Query: %s", user_prompt[:100])
        logger.info("Using %s reference models in 2-layer MoA architecture", len(ref_models))

        # Layer 1: Generate diverse responses from reference models.
        logger.info("Layer 1: Generating reference responses...")
        model_results = await asyncio.gather(*[
            _run_reference_model_safe(model, user_prompt, REFERENCE_TEMPERATURE)
            for model in ref_models
        ])

        successful_responses = []
        failed_models = []

        for model_name, content, success in model_results:
            if success:
                successful_responses.append(content)
            else:
                failed_models.append(model_name)

        successful_count = len(successful_responses)
        failed_count = len(failed_models)

        logger.info("Reference model results: %s successful, %s failed", successful_count, failed_count)
        if failed_models:
            logger.warning("Failed models: %s", ', '.join(failed_models))

        if successful_count < MIN_SUCCESSFUL_REFERENCES:
            raise ValueError(
                f"Insufficient successful reference models ({successful_count}/{len(ref_models)}). "
                f"Need at least {MIN_SUCCESSFUL_REFERENCES} successful responses."
            )

        debug_call_data["reference_responses_count"] = successful_count
        debug_call_data["failed_models_count"] = failed_count
        debug_call_data["failed_models"] = failed_models

        # Layer 2: Aggregate responses using the judge model.
        logger.info("Layer 2: Synthesizing final response...")
        aggregator_system_prompt = _construct_aggregator_prompt(
            AGGREGATOR_SYSTEM_PROMPT,
            successful_responses,
        )

        final_response = await _run_aggregator_model(
            aggregator_system_prompt,
            user_prompt,
            AGGREGATOR_TEMPERATURE,
            aggregator_model=judge_model,
        )

        end_time = datetime.datetime.now()
        processing_time = (end_time - start_time).total_seconds()
        logger.info("MoA processing completed in %.2f seconds", processing_time)

        result = {
            "success": True,
            "response": final_response,
            "models_used": {
                "reference_models": [_model_public_spec(m) for m in ref_models],
                "aggregator_model": _model_public_spec(judge_model),
            },
        }

        debug_call_data["success"] = True
        debug_call_data["final_response_length"] = len(final_response)
        debug_call_data["processing_time_seconds"] = processing_time
        debug_call_data["models_used"] = result["models_used"]

        _debug.log_call("mixture_of_agents_tool", debug_call_data)
        _debug.save()

        return json.dumps(result, indent=2, ensure_ascii=False)

    except Exception as e:
        error_msg = f"Error in MoA processing: {str(e)}"
        logger.error("%s", error_msg, exc_info=True)

        end_time = datetime.datetime.now()
        processing_time = (end_time - start_time).total_seconds()

        result = {
            "success": False,
            "response": "MoA processing failed. Please try again or use a single model for this query.",
            "models_used": {
                "reference_models": [_model_public_spec(m) for m in ref_models],
                "aggregator_model": _model_public_spec(judge_model),
            },
            "error": error_msg,
        }

        debug_call_data["error"] = error_msg
        debug_call_data["processing_time_seconds"] = processing_time
        _debug.log_call("mixture_of_agents_tool", debug_call_data)
        _debug.save()

        return json.dumps(result, indent=2, ensure_ascii=False)


def _client_available_for_spec(spec: ModelSpec) -> bool:
    try:
        runtime_spec = _resolve_runtime_model_spec(spec)
        client, resolved_model = resolve_provider_client(
            runtime_spec["provider"], runtime_spec["model"], async_mode=False
        )
        return client is not None and bool(resolved_model)
    except Exception as exc:
        logger.debug("MoA requirement check failed for %r: %s", spec, exc)
        return False


def check_moa_requirements() -> bool:
    """Check if enough routed providers are available for the default MoA."""
    successful_refs = sum(1 for spec in REFERENCE_MODELS if _client_available_for_spec(spec))
    return (
        successful_refs >= MIN_SUCCESSFUL_REFERENCES
        and _client_available_for_spec(AGGREGATOR_MODEL)
    )


def get_moa_configuration() -> Dict[str, Any]:
    """Get the current MoA configuration settings."""
    resolved_refs = [_model_public_spec(m) for m in REFERENCE_MODELS]
    resolved_judge = _model_public_spec(AGGREGATOR_MODEL)
    return {
        "reference_models": resolved_refs,
        "aggregator_model": resolved_judge,
        "reference_temperature": REFERENCE_TEMPERATURE,
        "aggregator_temperature": AGGREGATOR_TEMPERATURE,
        "reasoning": DEFAULT_REASONING_CONFIG,
        "min_successful_references": MIN_SUCCESSFUL_REFERENCES,
        "total_reference_models": len(REFERENCE_MODELS),
        "total_model_calls": len(REFERENCE_MODELS) + 1,
        "failure_tolerance": f"{len(REFERENCE_MODELS) - MIN_SUCCESSFUL_REFERENCES}/{len(REFERENCE_MODELS)} reference models can fail",
    }


if __name__ == "__main__":
    print("🤖 Mixture-of-Agents Tool Module")
    print("=" * 50)

    api_available = check_moa_requirements()
    if not api_available:
        print("❌ Not enough MoA provider credentials available")
        print("Configure OpenAI Codex, Anthropic, and OpenRouter credentials via `hermes auth` / .env.")
        sys.exit(1)

    print("✅ MoA routed providers available")
    print("🛠️  MoA tools ready for use!")

    config = get_moa_configuration()
    print("\n⚙️  Current Configuration:")
    print("  🤖 Reference models:")
    for m in config["reference_models"]:
        print(f"     - {m['provider']}: {m['model']}")
    print(f"  🧠 Aggregator model: {config['aggregator_model']['provider']}: {config['aggregator_model']['model']}")
    print(f"  📞 Total model calls: {config['total_model_calls']}")
    print(f"  🌡️  Reference temperature: {config['reference_temperature']}")
    print(f"  🌡️  Aggregator temperature: {config['aggregator_temperature']}")
    print(f"  🛡️  Failure tolerance: {config['failure_tolerance']}")
    print(f"  📊 Minimum successful references: {config['min_successful_references']}")

    if _debug.active:
        print(f"\n🐛 Debug mode ENABLED - Session ID: {_debug.session_id}")
        print("   Debug logs will be saved under ./logs/")
    else:
        print("\n🐛 Debug mode disabled (set MOA_TOOLS_DEBUG=true to enable)")

    print("\nBest use cases:")
    print("  - Complex mathematical proofs and calculations")
    print("  - Advanced coding problems and algorithm design")
    print("  - Multi-step analytical reasoning tasks")
    print("  - Problems requiring diverse domain expertise")
    print("  - Tasks where single models show limitations")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
from tools.registry import registry

MOA_SCHEMA = {
    "name": "mixture_of_agents",
    "description": "Route a hard problem through multiple frontier LLMs collaboratively. Makes 6 model calls by default (5 reference models + 1 current-main-model judge) with maximum reasoning effort — use sparingly for genuinely difficult problems. Best for: complex math, advanced algorithms, multi-step analytical reasoning, problems benefiting from diverse perspectives.",
    "parameters": {
        "type": "object",
        "properties": {
            "user_prompt": {
                "type": "string",
                "description": "The complex query or problem to solve using multiple AI models. Should be a challenging problem that benefits from diverse perspectives and collaborative reasoning."
            }
        },
        "required": ["user_prompt"]
    }
}

registry.register(
    name="mixture_of_agents",
    toolset="moa",
    schema=MOA_SCHEMA,
    handler=lambda args, **kw: mixture_of_agents_tool(user_prompt=args.get("user_prompt", "")),
    check_fn=check_moa_requirements,
    requires_env=[],
    is_async=True,
    emoji="🧠",
)
