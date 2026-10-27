"""LM Studio Responses route (spec §7.2, §8.6, §8.7); parsing helpers ported from NEL."""
from __future__ import annotations

import json
from typing import Any

from . import Acc, id_or_none, make_result, str_or_none
from .._reasoning import _text_fragment
from .._usage import parse_usage
from ..errors import BackendError

TRANSPORT = "responses"
_TERMINAL = {"response.completed", "response.incomplete", "response.failed"}


# Support table (spec v6 §6.7): only temperature and max_tokens are sent on this route; LM Studio's
# Responses documentation lists no sampling fields it honours (§7.2's rule, M2-3).
FIELDS = {"temperature": "temperature", "max_tokens": "max_output_tokens"}
RANGES: dict = {}
LABEL = 'the LM Studio Responses route (reasoning other than "off")'


def build_body(messages, model, *, reasoning, provider, settings, streaming, kind) -> dict:
    body = {"model": model, "input": messages, "stream": streaming}
    for arg, wire in FIELDS.items():
        if settings.get(arg) is not None:
            body[wire] = settings[arg]
    if reasoning is not None:
        body["reasoning"] = {"effort": reasoning}
    return body


def _items(doc: dict, kind: str) -> list[dict]:
    output = doc.get("output")
    return [i for i in output if isinstance(i, dict) and i.get("type") == kind] if isinstance(output, list) else []


def _responses_output(doc: dict) -> str:
    return "".join(part["text"] for item in _items(doc, "message")
                   if isinstance(item.get("content"), list)
                   for part in item["content"]
                   if isinstance(part, dict) and part.get("type") in {"output_text", "text"}
                   and isinstance(part.get("text"), str))


def _responses_reasoning(doc: dict) -> str:
    parts = []
    for item in _items(doc, "reasoning"):
        text = next((t for t in (_text_fragment(item.get(k)) for k in ("summary", "content", "text")) if t), "")
        parts.append(text)
    return "".join(parts)


def _status(doc: dict) -> str:
    return str(doc.get("status") or "").strip().lower()


def _incomplete_reason(doc: dict) -> str:
    details = doc.get("incomplete_details")
    return str(details.get("reason") or "").strip() if isinstance(details, dict) else ""


def _finish(doc: dict) -> dict[str, Any]:
    status = _status(doc)
    if status == "incomplete":
        finish = _incomplete_reason(doc) or "incomplete"
    else:
        finish = "stop" if status == "completed" else (status or None)
    truncated = status == "incomplete" and _incomplete_reason(doc).lower() in {"max_output_tokens", "max_tokens", "length"}
    return dict(finish_reason=finish, truncated=truncated, reasoning_details=_items(doc, "reasoning") or None)


def _usage_fields(raw) -> dict:
    raw = raw if isinstance(raw, dict) else None
    return dict(usage=parse_usage(raw), raw_usage=dict(raw) if raw is not None else None)


def _failed(call, doc: dict, statuses) -> None:
    if _status(doc) in statuses:
        raise call.error(BackendError, f"response ended with status {doc.get('status')!r}", status=200,
                         body=json.dumps(doc.get("error")))


def parse_response(doc, call, *, kind, token_cap) -> object:
    _failed(call, doc, {"failed", "cancelled"})
    return make_result(TRANSPORT, text=_responses_output(doc), reasoning=_responses_reasoning(doc) or None,
                       model=str_or_none(doc.get("model")), response_id=id_or_none(doc.get("id")),
                       **_finish(doc), **_usage_fields(doc.get("usage")))


def parse_stream(events, acc: Acc, call, *, kind, token_cap) -> object:
    saw_event = False
    final = None
    raw_usage = None
    for doc in events:
        if doc == "[DONE]":
            continue
        saw_event = True
        etype = str(doc.get("type") or "")
        if etype == "response.output_text.delta":
            acc.add_text(_text_fragment(doc.get("delta")))
        elif "reasoning" in etype.lower() and etype.lower().endswith(".delta"):
            acc.add_reasoning(_text_fragment(doc.get("delta")))
        if etype == "error":
            raise call.error(BackendError, "provider reported an error mid-stream", status=200,
                             body=json.dumps(doc))
        nested = doc.get("response")
        if isinstance(nested, dict):
            if id_or_none(nested.get("id")):
                acc.meta["response_id"] = nested["id"]
            if str_or_none(nested.get("model")):
                acc.meta["model"] = nested["model"]
            if isinstance(nested.get("usage"), dict):
                raw_usage = nested["usage"]
            if etype in _TERMINAL:
                final = nested
                break  # deviation #18: stop at the terminal event
    if not saw_event:
        raise call.error(BackendError, "provider returned no SSE response events")
    if final is not None:
        if not acc.text:
            acc.add_text(_responses_output(final))
        if not acc.reasoning:
            acc.add_reasoning(_responses_reasoning(final))
    fields = _finish(final or {})
    if final is not None:
        _failed(call, final, {"failed"})
    else:
        raise call.error(BackendError, "stream ended before completion")
    return acc.result(**fields, **_usage_fields(raw_usage))
