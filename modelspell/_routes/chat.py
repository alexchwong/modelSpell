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
