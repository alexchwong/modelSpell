"""Per-route body building and response parsing; shared stream accumulator."""
from __future__ import annotations

from typing import Any, Callable

from .._types import Result


def make_result(transport: str, **kw: Any) -> Result:
    fields = dict(text="", reasoning=None, usage=None, cost=None, finish_reason=None, truncated=False,
                  model=None, provider=None, response_id=None, reasoning_details=None, raw_usage=None)
    fields.update(kw)
    if isinstance(fields["reasoning_details"], list):
        fields["reasoning_details"] = tuple(fields["reasoning_details"]) or None
    return Result(transport=transport, **fields)


def str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def id_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


class Acc:
    """Accumulates streamed text/reasoning and forwards non-empty fragments to callbacks."""

    def __init__(self, transport: str, on_text: Callable | None, on_reasoning: Callable | None):
        self.transport, self._on_text, self._on_reasoning = transport, on_text, on_reasoning
        self.text: list[str] = []
        self.reasoning: list[str] = []
        self.meta: dict[str, Any] = {}  # model, provider, response_id, raw_usage, finish_reason, ...

    def add_text(self, s: str) -> None:
        if s:
            self.text.append(s)
            if self._on_text:
                self._on_text(s)

    def add_reasoning(self, s: str) -> None:
        if s:
            self.reasoning.append(s)
            if self._on_reasoning:
                self._on_reasoning(s)

    def result(self, **kw: Any) -> Result:
        fields = {**self.meta, **kw}
        return make_result(self.transport, text="".join(self.text),
                           reasoning="".join(self.reasoning) or None, **fields)

    def partial(self) -> Result:
        return self.result(finish_reason="cancelled", truncated=False, usage=None, cost=None,
                           raw_usage=None)
