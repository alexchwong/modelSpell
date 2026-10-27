"""LM Studio native chat route (spec §7.3, §8.8)."""
from __future__ import annotations

from . import Acc, id_or_none, make_result, str_or_none
from .._usage import parse_native_usage
from ..errors import BackendError

TRANSPORT = "lmstudio_native"


def flatten(messages: list[dict]) -> tuple[str | None, str]:
    """Port of NEL _lmstudio_native_prompt()."""
    system_prompt = "\n\n".join(m["content"] for m in messages if m["role"] == "system" and m["content"])
    conversation = [m for m in messages if m["role"] != "system"]
    if len(conversation) == 1 and conversation[0]["role"] == "user":
        user_input = conversation[0]["content"]
    else:
        user_input = "\n\n".join(f"[{m['role'].upper()}]\n{m['content']}" for m in conversation)
    return system_prompt or None, user_input


# Support table (spec v6 §6.7): generation argument -> wire name on POST /api/v1/chat.
FIELDS = {"temperature": "temperature", "max_tokens": "max_output_tokens", "top_p": "top_p", "top_k": "top_k",
          "min_p": "min_p", "repetition_penalty": "repeat_penalty"}
RANGES = {"temperature": (None, 1)}  # spec v6 §6.8.2
LABEL = 'the LM Studio native route (reasoning="off")'


def build_body(messages, model, *, reasoning, provider, settings, streaming, kind) -> dict:
    system_prompt, user_input = flatten(messages)
    body = {"model": model, "input": user_input, "reasoning": "off", "store": False, "stream": streaming}
    for arg, wire in FIELDS.items():
        if settings.get(arg) is not None:
            body[wire] = settings[arg]
    if system_prompt is not None:
        body["system_prompt"] = system_prompt
    return body


def _output(doc: dict) -> tuple[str, str, list]:
    text, reasoning, details = [], [], []
    for item in doc.get("output") if isinstance(doc.get("output"), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            continue
        kind = str(item.get("type") or "").strip().lower()
        if kind == "message":
            text.append(item["content"])
        elif kind == "reasoning":
            reasoning.append(item["content"])
            details.append(item)
    return "".join(text), "".join(reasoning), details


def _meta(doc: dict, token_cap: int | None) -> dict:
    stats = doc.get("stats") if isinstance(doc.get("stats"), dict) else None
    usage = parse_native_usage(stats)
    truncated = (token_cap is not None and usage is not None and usage.output_tokens is not None
                 and usage.output_tokens >= token_cap)  # deviation #8: heuristic
    return dict(usage=usage, raw_usage=dict(stats) if stats is not None else None, truncated=truncated,
                finish_reason=None, response_id=id_or_none(doc.get("response_id")),
                model=str_or_none(doc.get("model_instance_id")))


def parse_response(doc, call, *, kind, token_cap) -> object:
    text, reasoning, details = _output(doc)
    return make_result(TRANSPORT, text=text, reasoning=reasoning or None, reasoning_details=details,
                       **_meta(doc, token_cap))


def parse_stream(events, acc: Acc, call, *, kind, token_cap) -> object:
    saw_event = False
    final = None
    error = None
    for doc in events:
        if doc == "[DONE]":
            continue
        saw_event = True
        etype = str(doc.get("type") or "").strip().lower()
        content = doc.get("content")
        if etype == "message.delta" and isinstance(content, str):
            acc.add_text(content)
        elif etype == "reasoning.delta" and isinstance(content, str):
            acc.add_reasoning(content)
        elif etype == "error":
            err = doc.get("error")
            error = (str(err.get("message") or err.get("type") or "") if isinstance(err, dict) else "") \
                or "LM Studio native chat stream error"
        elif etype == "chat.end" and isinstance(doc.get("result"), dict):
            final = doc["result"]
            break  # deviation #18: stop at the terminal event
    if not saw_event:
        raise call.error(BackendError, "provider returned no SSE native chat events")
    details = None
    if final is not None:
        text, reasoning, details = _output(final)
        if not acc.text:
            acc.add_text(text)
        if not acc.reasoning:
            acc.add_reasoning(reasoning)
    if error:
        raise call.error(BackendError, f"LM Studio native chat error: {error}", status=200, body=error)
    if final is None:
        raise call.error(BackendError, "stream ended before completion")
    return acc.result(reasoning_details=details, **_meta(final, token_cap))
