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
    """Accumulates streamed text/reasoning and forwards non-empty fragments to callbacks.

    With a watch, every fragment passes the admission point first (spec v8 §6.10): the watch's lock
    is released before the fragment is added and its callback runs.
    """

    def __init__(self, transport: str, on_text: Callable | None, on_reasoning: Callable | None, watch=None):
        self.transport, self._on_text, self._on_reasoning = transport, on_text, on_reasoning
        self._watch = watch
        self.text: list[str] = []
        self.reasoning: list[str] = []
        self.meta: dict[str, Any] = {}  # model, provider, response_id, raw_usage, finish_reason, ...
        self.callback_error: BaseException | None = None  # the object a callback raised, if any

    def _call(self, callback: Callable, s: str) -> None:
        try:
            callback(s)
        except BaseException as exc:
            self.callback_error = exc  # complete() tells a callback's exception from its own by identity
            raise

    def add_text(self, s: str) -> None:
        if s:
            if self._watch is not None:
                self._watch.admit()
            self.text.append(s)
            if self._on_text:
                self._call(self._on_text, s)

    def add_reasoning(self, s: str) -> None:
        if s:
            if self._watch is not None:
                self._watch.admit()
            self.reasoning.append(s)
            if self._on_reasoning:
                self._call(self._on_reasoning, s)

    def terminal(self) -> None:
        """The route read its terminal event: the streaming commit point."""
        if self._watch is not None:
            self._watch.commit()

    def result(self, **kw: Any) -> Result:
        fields = {**self.meta, **kw}
        return make_result(self.transport, text="".join(self.text),
                           reasoning="".join(self.reasoning) or None, **fields)

    def partial(self) -> Result:
        return self.result(finish_reason="cancelled", truncated=False, usage=None, cost=None,
                           raw_usage=None)
