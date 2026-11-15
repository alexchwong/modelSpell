"""LM Studio Responses route (spec §7.2, §8.6, §8.7); parsing helpers ported from NEL."""
from __future__ import annotations

import json
from typing import Any

from . import Acc, id_or_none, make_result, str_or_none
from .._reasoning import _text_fragment
from .._usage import parse_usage
from ..errors import BackendError, ResponseFormatError

TRANSPORT = "responses"
_TERMINAL = {"response.completed", "response.incomplete", "response.failed"}


# Support table (spec v7 §6.7): the generation settings OpenAI's Responses reference defines, which
# LM Studio's Responses page refers to (M21-2). The other seven sampling arguments are refused.
FIELDS = {"temperature": "temperature", "max_tokens": "max_output_tokens", "top_p": "top_p"}
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
                acc.terminal()
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


# --- tool turns (0.4.0) -----------------------------------------------------------------------------
# Stateless replay (spec v9 §21): complete output items are sent back as input items in their
# original order, with store: false. The terminal status decides whether a turn is clean.
_REPLAY_TYPES = ("message", "function_call", "reasoning")
_TERMINAL_STATUS = {"response.completed": "completed", "response.incomplete": "incomplete"}


def build_tool_body(entries, model, *, reasoning, provider, settings, streaming, kind, tools) -> dict:
    items: list = []
    for tag, value in entries:
        if tag == "message":
            items.append(value)
        elif tag == "turn":
            items.extend(value.replay)
        else:
            items.append({"type": "function_call_output", "call_id": value.call_id, "output": value.content})
    body = build_body(items, model, reasoning=reasoning, provider=provider, settings=settings,
                      streaming=streaming, kind=kind)
    body["store"] = False
    body["tools"] = [{"type": "function", "name": t.name, "description": t.description,
                      "parameters": t.parameters} for t in tools]
    return body


def _bad(call, message: str) -> Exception:
    return call.error(ResponseFormatError, f"unexpected tool-call response: {message}", status=200)


def _nonempty(value) -> bool:
    return isinstance(value, str) and value != ""


def _check_calls(call, output: list, completed: bool) -> None:
    """Validate the function_call items of a terminal output list."""
    seen = set()
    for i, item in enumerate(output):
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        where = f"output[{i}]"
        if not _nonempty(item.get("call_id")):
            raise _bad(call, f"{where} has no call_id")
        if not _nonempty(item.get("name")):
            raise _bad(call, f"{where} has no name")
        if not isinstance(item.get("arguments"), str):
            raise _bad(call, f"{where} has no argument string")
        if item.get("id") is not None and not isinstance(item.get("id"), str):
            raise _bad(call, f"{where} has a non-string id")
        if completed and "status" in item and item["status"] != "completed":
            raise _bad(call, f"completed response contains an unfinished call item ({where})")
        if item["call_id"] in seen:
            raise _bad(call, "duplicate call_id in one turn")
        seen.add(item["call_id"])


def _terminal_status(call, doc: dict) -> str:
    _failed(call, doc, {"failed", "cancelled"})
    status = _status(doc)
    if not status:
        raise _bad(call, "missing response status")
    if status not in ("completed", "incomplete"):
        raise _bad(call, f"non-terminal response status {doc.get('status')!r}")
    return status


def _replay_items(output: list) -> list:
    return [item for item in output if isinstance(item, dict) and item.get("type") in _REPLAY_TYPES]


def parse_tool_response(doc, call, *, kind):
    """A non-streamed tool turn: (Result, output items or None for a diagnostic turn)."""
    status = _terminal_status(call, doc)
    output = doc.get("output")
    if not isinstance(output, list):
        raise _bad(call, "missing output list")
    if status == "completed":
        _check_calls(call, output, True)
    result = make_result(TRANSPORT, text=_responses_output(doc), reasoning=_responses_reasoning(doc) or None,
                         model=str_or_none(doc.get("model")), response_id=id_or_none(doc.get("id")),
                         **_finish(doc), **_usage_fields(doc.get("usage")))
    return result, (_replay_items(output) if status == "completed" else None)


def _index(call, doc: dict) -> int:
    index = doc.get("output_index")
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise _bad(call, f"{doc.get('type')} without a valid output_index")
    return index


def _track(call, streamed: dict, index: int, item_id) -> dict:
    entry = streamed.setdefault(index, {"id": None, "added": None, "done": None, "deltas": [], "args_done": None})
    if item_id is not None:
        if not isinstance(item_id, str):
            raise _bad(call, f"output item {index} has a non-string id")
        if entry["id"] is None:
            entry["id"] = item_id
        elif entry["id"] != item_id:
            raise _bad(call, f"output item {index} has conflicting ids")
    return entry


def _reconcile(call, streamed: dict, done_items: dict, final: dict) -> list:
    """The canonical output list: the terminal output, checked against the streamed call items."""
    output = final.get("output")
    if isinstance(output, list) and output:
        for index, entry in streamed.items():
            item = output[index] if index < len(output) else None
            if not isinstance(item, dict) or item.get("type") != "function_call":
                raise _bad(call, f"streamed call item {index} is not in the terminal output")
            if entry["id"] is not None and item.get("id") not in (None, entry["id"]):
                raise _bad(call, f"call item {index} id differs from the stream")
            for source in (entry["added"], entry["done"]):
                if isinstance(source, dict):
                    for key in ("call_id", "name"):
                        if source.get(key) not in (None, "") and source.get(key) != item.get(key):
                            raise _bad(call, f"call item {index} {key} differs from the stream")
            if isinstance(entry["done"], dict) and entry["done"].get("arguments") not in (None, item.get("arguments")):
                raise _bad(call, f"call item {index} arguments differ from the stream")
            if entry["args_done"] is not None and entry["args_done"] != item.get("arguments"):
                raise _bad(call, f"call item {index} arguments differ from the stream")
            if entry["deltas"] and "".join(entry["deltas"]) != item.get("arguments"):
                raise _bad(call, f"call item {index} arguments differ from the streamed fragments")
        return output
    for index, entry in streamed.items():  # no terminal output: every call item must have finished
        if entry["done"] is None:
            raise _bad(call, f"stream ended mid-call (output item {index})")
    return [done_items[i] for i in sorted(done_items)]


def parse_tool_stream(events, acc: Acc, call, *, kind):
    """A streamed tool turn: (Result, output items or None for a diagnostic turn)."""
    saw_event = False
    final = final_type = None
    raw_usage = None
    streamed: dict[int, dict] = {}   # output_index -> function_call tracking
    done_items: dict[int, dict] = {}  # output_index -> output_item.done item, any type
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
        if etype in ("response.output_item.added", "response.output_item.done"):
            item = doc.get("item")
            if not isinstance(item, dict):
                raise _bad(call, f"{etype} without an item")
            index = _index(call, doc)
            if etype.endswith(".done"):
                done_items[index] = item
            if item.get("type") == "function_call":
                acc.admit()
                entry = _track(call, streamed, index, item.get("id"))
                entry["done" if etype.endswith(".done") else "added"] = item
        elif etype in ("response.function_call_arguments.delta", "response.function_call_arguments.done"):
            acc.admit()
            entry = _track(call, streamed, _index(call, doc), doc.get("item_id"))
            if etype.endswith(".delta"):
                if not isinstance(doc.get("delta"), str):
                    raise _bad(call, "argument fragment is not a string")
                entry["deltas"].append(doc["delta"])
            else:
                if not isinstance(doc.get("arguments"), str):
                    raise _bad(call, "argument string is not a string")
                entry["args_done"] = doc["arguments"]
        nested = doc.get("response")
        if isinstance(nested, dict):
            if id_or_none(nested.get("id")):
                acc.meta["response_id"] = nested["id"]
            if str_or_none(nested.get("model")):
                acc.meta["model"] = nested["model"]
            if isinstance(nested.get("usage"), dict):
                raw_usage = nested["usage"]
            if etype in _TERMINAL:
                final, final_type = nested, etype
                acc.terminal()
                break
    if not saw_event:
        raise call.error(BackendError, "provider returned no SSE response events")
    if final is None:
        raise call.error(BackendError, "stream ended before completion")
    status = _terminal_status(call, final)
    if _TERMINAL_STATUS.get(final_type) != status:
        raise _bad(call, f"{final_type} carries status {final.get('status')!r}")
    if not acc.text:
        acc.add_text(_responses_output(final))
    if not acc.reasoning:
        acc.add_reasoning(_responses_reasoning(final))
    result = acc.result(**_finish(final), **_usage_fields(raw_usage))
    if status != "completed":
        return result, None
    output = _reconcile(call, streamed, done_items, final)
    _check_calls(call, output, True)
    return result, _replay_items(output)
