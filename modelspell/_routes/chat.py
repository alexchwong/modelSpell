"""Chat Completions route (spec §7.1, §8.4, §8.5)."""
from __future__ import annotations

import json

from . import Acc, id_or_none, make_result, str_or_none
from .._reasoning import _reasoning_fragment, _text_fragment
from .._usage import parse_cost, parse_usage
from ..errors import BackendError, ResponseFormatError

TRANSPORT = "chat_completions"
EFFORT = {"off": "none", "low": "low", "medium": "medium", "high": "high"}


# Support table (spec v6 §6.7): generation argument -> wire name, per kind. The setting checks,
# build_body() and supported_settings() all read these mappings. reasoning and provider are
# handled separately (§6.1).
_COMMON = {"temperature": "temperature", "max_tokens": "max_tokens", "top_p": "top_p", "top_k": "top_k",
           "min_p": "min_p", "presence_penalty": "presence_penalty", "frequency_penalty": "frequency_penalty",
           "seed": "seed", "stop": "stop"}
FIELDS = {
    "openrouter": {**_COMMON, "repetition_penalty": "repetition_penalty"},
    "openai_compat": dict(_COMMON),  # top_k/min_p are extensions; repetition_penalty is not sent (M2-3)
}
# Documented per-route ranges (spec v6 §6.8.2): argument -> (low or None, high), inclusive.
RANGES = {
    "openrouter": {"temperature": (None, 2), "repetition_penalty": (None, 2),
                   "presence_penalty": (-2, 2), "frequency_penalty": (-2, 2)},
    "openai_compat": {},
}
LABEL = {"openrouter": "the OpenRouter route", "openai_compat": "the openai_compat route"}


def build_body(messages, model, *, reasoning, provider, settings, streaming, kind) -> dict:
    body = {"model": model, "messages": messages, "stream": streaming}
    for arg, wire in FIELDS[kind].items():
        if settings.get(arg) is not None:
            body[wire] = list(settings[arg]) if arg == "stop" else settings[arg]
    if kind == "openrouter":
        if provider is not None:
            body["provider"] = dict(provider)
        if reasoning is not None:
            body["reasoning"] = {"effort": EFFORT[reasoning]}
    return body


def _usage_fields(raw, kind) -> dict:
    raw = raw if isinstance(raw, dict) else None
    return dict(usage=parse_usage(raw), cost=parse_cost(raw) if kind == "openrouter" else None,
                raw_usage=dict(raw) if raw is not None else None)


def parse_response(doc, call, *, kind, token_cap) -> object:
    try:
        choice = doc["choices"][0]
        message = choice["message"]
        if not isinstance(message, dict):
            raise TypeError
    except (KeyError, IndexError, TypeError):
        raise call.error(ResponseFormatError, "unexpected chat completion shape: missing choices[0].message",
                         status=200) from None
    content = message.get("content")
    if content is None:
        content = ""
    elif not isinstance(content, str):
        raise call.error(ResponseFormatError, "unexpected chat completion shape: content is not a string",
                         status=200)
    details = message.get("reasoning_details")
    finish = choice.get("finish_reason")
    finish = str(finish) if finish is not None else None
    return make_result(
        TRANSPORT, text=content, reasoning=_reasoning_fragment(message) or None,
        reasoning_details=list(details) if isinstance(details, list) else None,
        finish_reason=finish, truncated=finish == "length",
        model=str_or_none(doc.get("model")), provider=str_or_none(doc.get("provider")),
        response_id=id_or_none(doc.get("id")), **_usage_fields(doc.get("usage"), kind))


def _is_error(doc: dict) -> bool:
    choices = doc.get("choices")
    return bool(doc.get("error")) or (isinstance(choices, list) and any(
        isinstance(c, dict) and c.get("finish_reason") == "error" for c in choices))


def parse_stream(events, acc: Acc, call, *, kind, token_cap) -> object:
    saw_event = saw_done = False
    details: list = []
    raw_usage = None
    for doc in events:
        if doc == "[DONE]":
            saw_done = True
            acc.terminal()
            break
        saw_event = True
        if _is_error(doc):
            raise call.error(BackendError, "provider reported an error mid-stream", status=200,
                             body=json.dumps(doc))
        if isinstance(doc.get("usage"), dict):
            raw_usage = doc["usage"]
        for key, conv in (("id", id_or_none), ("model", str_or_none), ("provider", str_or_none)):
            if conv(doc.get(key)):
                acc.meta["response_id" if key == "id" else key] = doc[key]
        choices = doc.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            continue
        choice = choices[0]
        delta = choice.get("delta")
        delta = delta if isinstance(delta, dict) else {}
        acc.add_reasoning(_reasoning_fragment(delta))
        if isinstance(delta.get("reasoning_details"), list):
            details.extend(delta["reasoning_details"])
        acc.add_text(_text_fragment(delta.get("content")))
        if choice.get("finish_reason") is not None:
            acc.meta["finish_reason"] = str(choice["finish_reason"])
    if not saw_event:
        raise call.error(BackendError, "provider returned no SSE events")
    finish = acc.meta.get("finish_reason")
    if not saw_done and finish is None:
        raise call.error(BackendError, "stream ended before completion")
    return acc.result(truncated=finish == "length", reasoning_details=details or None,
                      **_usage_fields(raw_usage, kind))


# --- tool turns (0.4.0) -----------------------------------------------------------------------------
# A tool turn's assistant message is replayed from its snapshot (spec v9 §21). Only these finish
# reasons are clean; any other explicit finish yields a diagnostic turn; "error" is a BackendError.
_CLEAN_FINISH = ("stop", "tool_calls")
_DETAIL_CONCAT = ("text", "summary", "data", "signature")


def build_tool_body(entries, model, *, reasoning, provider, settings, streaming, kind, tools) -> dict:
    messages: list = []
    for tag, value in entries:
        if tag == "message":
            messages.append(value)
        elif tag == "turn":
            messages.extend(value.replay)
        else:
            messages.append({"role": "tool", "tool_call_id": value.call_id, "content": value.content})
    body = build_body(messages, model, reasoning=reasoning, provider=provider, settings=settings,
                      streaming=streaming, kind=kind)
    body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                       "parameters": t.parameters}} for t in tools]
    return body


def _bad(call, message: str) -> Exception:
    return call.error(ResponseFormatError, f"unexpected tool-call response: {message}", status=200)


def _nonempty(value) -> bool:
    return isinstance(value, str) and value != ""


def _complete_call(call, raw, where: str) -> dict:
    """One non-streamed tool call, validated: {"id", "name", "arguments"}."""
    if not isinstance(raw, dict):
        raise _bad(call, f"{where} is not an object")
    if raw.get("type", "function") != "function":
        raise _bad(call, f"{where} has type {raw.get('type')!r}")
    function = raw.get("function")
    if not isinstance(function, dict):
        raise _bad(call, f"{where} has no function object")
    if not _nonempty(raw.get("id")):
        raise _bad(call, f"{where} has no id")
    if not _nonempty(function.get("name")):
        raise _bad(call, f"{where} has no function name")
    if not isinstance(function.get("arguments"), str):
        raise _bad(call, f"{where} has no argument string")
    return {"id": raw["id"], "name": function["name"], "arguments": function["arguments"]}


def _unique(call, calls: list[dict]) -> None:
    ids = [c["id"] for c in calls]
    if len(set(ids)) != len(ids):
        raise _bad(call, "duplicate tool call id in one turn")


def _clean(call, finish: str, calls: list[dict]) -> bool:
    if finish == "tool_calls" and not calls:
        raise _bad(call, 'finish_reason "tool_calls" without an assembled call')
    return finish in _CLEAN_FINISH


def _assemble_details(fragments: list) -> list:
    """Streamed reasoning_details: fragments of one block (same int index and type) are merged in order."""
    out: list = []
    for frag in fragments:
        if not isinstance(frag, dict):
            out.append(frag)
            continue
        last = out[-1] if out and isinstance(out[-1], dict) else None
        index = frag.get("index")
        if (last is not None and isinstance(index, int) and not isinstance(index, bool)
                and last.get("index") == index and last.get("type") == frag.get("type")):
            for key, value in frag.items():
                if key in _DETAIL_CONCAT and isinstance(value, str) and isinstance(last.get(key), str):
                    last[key] += value
                elif value is not None and last.get(key) is None:
                    last[key] = value
            continue
        out.append(dict(frag))
    return out


def _replay_message(text: str, reasoning, details, calls: list[dict], kind: str) -> dict:
    message: dict = {"role": "assistant", "content": text if text else (None if calls else "")}
    if calls:
        message["tool_calls"] = [{"id": c["id"], "type": "function",
                                  "function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]
    if kind == "openrouter":  # llama-server (openai_compat): reasoning is kept for diagnostics only
        if details:
            message["reasoning_details"] = list(details)
        elif reasoning:
            message["reasoning"] = reasoning
    return message


def parse_tool_response(doc, call, *, kind):
    """A non-streamed tool turn: (Result, [assistant message] or None for a diagnostic turn)."""
    if _is_error(doc):
        raise call.error(BackendError, "provider reported an error", status=200, body=json.dumps(doc))
    choices = doc.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise _bad(call, "expected exactly one choice")
    choice = choices[0]
    if choice.get("index", 0) != 0:
        raise _bad(call, "unexpected choice index")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise _bad(call, "missing choices[0].message")
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise _bad(call, "content is not a string")
    raw_calls = message.get("tool_calls")
    if raw_calls is not None and not isinstance(raw_calls, list):
        raise _bad(call, "tool_calls is not a list")
    calls = [_complete_call(call, c, f"tool_calls[{i}]") for i, c in enumerate(raw_calls or [])]
    _unique(call, calls)
    finish = choice.get("finish_reason")
    if finish is None:
        raise _bad(call, "missing finish_reason")
    finish = str(finish)
    details = message.get("reasoning_details")
    details = list(details) if isinstance(details, list) else None
    result = make_result(
        TRANSPORT, text=content or "", reasoning=_reasoning_fragment(message) or None,
        reasoning_details=details, finish_reason=finish, truncated=finish == "length",
        model=str_or_none(doc.get("model")), provider=str_or_none(doc.get("provider")),
        response_id=id_or_none(doc.get("id")), **_usage_fields(doc.get("usage"), kind))
    if not _clean(call, finish, calls):
        return result, None
    return result, [_replay_message(result.text, result.reasoning, details, calls, kind)]


def _merge_fragment(call, calls: dict, frag) -> None:
    if not isinstance(frag, dict):
        raise _bad(call, "tool-call fragment is not an object")
    index = frag.get("index")
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise _bad(call, "tool-call fragment without a valid index")
    entry = calls.setdefault(index, {"id": None, "name": None, "arguments": []})
    if frag.get("type") not in (None, "function"):
        raise _bad(call, f"tool call {index} has type {frag.get('type')!r}")
    function = frag.get("function")
    if function is not None and not isinstance(function, dict):
        raise _bad(call, f"tool call {index} has a non-object function")
    function = function or {}
    for key, value in (("id", frag.get("id")), ("name", function.get("name"))):
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise _bad(call, f"tool call {index} has a non-string {key}")
        if entry[key] is None:
            entry[key] = value
        elif entry[key] != value:
            raise _bad(call, f"tool call {index} has conflicting {key} fragments")
    arguments = function.get("arguments")
    if arguments is not None:
        if not isinstance(arguments, str):
            raise _bad(call, f"tool call {index} has a non-string argument fragment")
        entry["arguments"].append(arguments)


def parse_tool_stream(events, acc: Acc, call, *, kind):
    """A streamed tool turn: (Result, [assistant message] or None for a diagnostic turn)."""
    saw_event = saw_done = False
    fragments: list = []
    pending: dict[int, dict] = {}
    raw_usage = None
    for doc in events:
        if doc == "[DONE]":
            saw_done = True
            acc.terminal()
            break
        saw_event = True
        if _is_error(doc):
            raise call.error(BackendError, "provider reported an error mid-stream", status=200,
                             body=json.dumps(doc))
        if isinstance(doc.get("usage"), dict):
            raw_usage = doc["usage"]
        for key, conv in (("id", id_or_none), ("model", str_or_none), ("provider", str_or_none)):
            if conv(doc.get(key)):
                acc.meta["response_id" if key == "id" else key] = doc[key]
        choices = doc.get("choices")
        if choices is None or choices == []:
            continue  # usage-only chunk
        if (not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict)
                or choices[0].get("index", 0) != 0):
            raise _bad(call, "unexpected additional choices in stream")
        choice = choices[0]
        delta = choice.get("delta")
        delta = delta if isinstance(delta, dict) else {}
        acc.add_reasoning(_reasoning_fragment(delta))
        if isinstance(delta.get("reasoning_details"), list):
            fragments.extend(delta["reasoning_details"])
        acc.add_text(_text_fragment(delta.get("content")))
        tool_deltas = delta.get("tool_calls")
        if tool_deltas is not None:
            if not isinstance(tool_deltas, list):
                raise _bad(call, "tool_calls delta is not a list")
            for frag in tool_deltas:
                acc.admit()
                _merge_fragment(call, pending, frag)
        if choice.get("finish_reason") is not None:
            acc.meta["finish_reason"] = str(choice["finish_reason"])
    if not saw_event:
        raise call.error(BackendError, "provider returned no SSE events")
    finish = acc.meta.get("finish_reason")
    if not saw_done:
        if finish is None:
            raise call.error(BackendError, "stream ended before completion")
        raise _bad(call, "stream ended without [DONE]")
    if finish is None:
        raise _bad(call, "stream ended without finish_reason")
    calls = []
    for index in sorted(pending):
        entry = pending[index]
        if entry["id"] is None:
            raise _bad(call, f"tool call {index} has no id")
        if entry["name"] is None:
            raise _bad(call, f"tool call {index} has no function name")
        calls.append({"id": entry["id"], "name": entry["name"], "arguments": "".join(entry["arguments"])})
    _unique(call, calls)
    details = _assemble_details(fragments) or None
    result = acc.result(truncated=finish == "length", reasoning_details=details, **_usage_fields(raw_usage, kind))
    if not _clean(call, finish, calls):
        return result, None
    return result, [_replay_message(result.text, result.reasoning, details, calls, kind)]
