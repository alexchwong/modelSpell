"""Usage and cost normalisation (spec §8.2); ports NEL normalize_provider_usage() + fix."""
from __future__ import annotations

from typing import Any

from ._types import Usage


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _sub(raw: dict, outer: str, inner: str) -> int | None:
    d = raw.get(outer)
    return _int(d.get(inner)) if isinstance(d, dict) else None


def _first(*values: int | None) -> int | None:
    return next((v for v in values if v is not None), None)


def _make(i, o, total=None, **rest) -> Usage | None:
    if total is None and i is not None and o is not None:
        total = i + o
    usage = Usage(i, o, total, **rest)
    return None if all(v is None for v in vars(usage).values()) else usage


def parse_usage(raw: Any) -> Usage | None:
    """Chat Completions / Responses `usage` dict."""
    if not isinstance(raw, dict):
        return None

    def pick(*names):  # first present key wins (NEL alias rule)
        return _int(next((raw.get(n) for n in names if raw.get(n) is not None), None))

    return _make(
        pick("prompt_tokens", "input_tokens"),
        pick("completion_tokens", "output_tokens"),
        pick("total_tokens"),
        reasoning_tokens=_first(_sub(raw, "completion_tokens_details", "reasoning_tokens"),
                                _sub(raw, "output_tokens_details", "reasoning_tokens")),
        cached_input_tokens=_first(_sub(raw, "prompt_tokens_details", "cached_tokens"),
                                   _sub(raw, "input_tokens_details", "cached_tokens")),
        cache_write_tokens=_sub(raw, "prompt_tokens_details", "cache_write_tokens"),
    )


def parse_native_usage(stats: Any) -> Usage | None:
    """LM Studio native `stats` dict."""
    if not isinstance(stats, dict):
        return None
    return _make(_int(stats.get("input_tokens")), _int(stats.get("total_output_tokens")),
                 reasoning_tokens=_int(stats.get("reasoning_output_tokens")))


def parse_cost(raw: Any) -> float | None:
    cost = raw.get("cost") if isinstance(raw, dict) else None
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        return float(cost)
    return None
