"""Reasoning text helpers, ported verbatim from NEL model_client.py (spec §8.3)."""
from __future__ import annotations

from typing import Any


def _text_fragment(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text") or item.get("content")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _reasoning_details_fragment(details: Any) -> str:
    """Prefer non-encrypted `text`; fall back to `summary` only when no item has text."""
    if not isinstance(details, list):
        return ""
    text_parts: list[str] = []
    summary_parts: list[str] = []
    for item in details:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip().lower()
        text = item.get("text")
        summary = item.get("summary")
        if isinstance(text, str) and text and item_type != "reasoning.encrypted":
            text_parts.append(text)
        elif isinstance(summary, str) and summary:
            summary_parts.append(summary)
    return "".join(text_parts) if text_parts else "".join(summary_parts)


def _reasoning_fragment(delta: dict[str, Any]) -> str:
    """Select exactly one reasoning representation; never concatenate aliases."""
    structured = _reasoning_details_fragment(delta.get("reasoning_details"))
    if structured:
        return structured
    for key in ("reasoning", "reasoning_content", "thinking"):
        text = _text_fragment(delta.get(key))
        if text:
            return text
    return ""
