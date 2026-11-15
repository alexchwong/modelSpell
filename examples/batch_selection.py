"""Reuse an OpenRouter selection across one authorized, compatible batch."""
from __future__ import annotations

from typing import Sequence

from modelspell import Endpoint, ModelInfo, Result, check_completion, choose_provider, complete


def run_batch(
    endpoint: Endpoint,
    model: str | ModelInfo,
    prompts: Sequence[str],
    *,
    estimated_input_tokens: int,
) -> list[Result]:
    """Run prompts after the host has authorized the workload and its budgets."""
    if not prompts:
        return []

    generation = {"reasoning": "low", "max_tokens": 128, "temperature": 0.2}
    transport = {"timeout": 120.0, "rate_limit_retries": 2, "max_retry_wait": 10.0}
    policy = {"zdr": True}
    check_completion(endpoint, model, provider=policy, **generation, **transport)

    model_id = model.id if isinstance(model, ModelInfo) else model
    selection = choose_provider(
        endpoint, model_id, mode="automatic", provider=policy, settings=generation,
        input_tokens=estimated_input_tokens, output_tokens=generation["max_tokens"],
        timeout=30.0, max_attempts=2,
    )
    return [
        complete(endpoint, model, prompt, provider=selection.policy, **generation, **transport)
        for prompt in prompts
    ]
