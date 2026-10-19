"""complete(): validation, route selection, extra merge, dispatch (spec §6)."""
from __future__ import annotations

from typing import Any, Callable, Literal, Mapping, Sequence

from . import _http
from ._routes import Acc, chat, lmstudio_native, responses
from ._sse import iter_sse
from ._types import Endpoint, Message, Result
from .errors import AuthError, Cancelled, ConfigError, UnsupportedSetting

_REASONING = ("off", "low", "medium", "high")


def _num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _messages(input: Any) -> list[dict]:
    if isinstance(input, str):
        return [{"role": "user", "content": input}]
    if not isinstance(input, Sequence) or not input:
        raise ConfigError("input must be a str or a non-empty sequence of messages")
    out = []
    for item in input:
        if isinstance(item, Mapping):
            if set(item) != {"role", "content"}:
                raise ConfigError("a message mapping must have exactly the keys 'role' and 'content'")
            item = Message(item["role"], item["content"])
        elif not isinstance(item, Message):
            raise ConfigError(f"unsupported message item: {type(item).__name__}")
        out.append({"role": item.role, "content": item.content})
    return out


def _validate(endpoint, model, reasoning, max_tokens, temperature, provider, extra, timeout, callbacks):
    if not isinstance(endpoint, Endpoint):
        raise ConfigError("endpoint must be an Endpoint")
    if not isinstance(model, str) or not model:
        raise ConfigError("model must be a non-empty str")
    if reasoning is not None and reasoning not in _REASONING:
        raise ConfigError(f"reasoning must be None or one of {_REASONING}")
    if reasoning is not None and endpoint.kind == "openai_compat":
        raise UnsupportedSetting("openai_compat has no reasoning control; pass backend fields via extra=")
    if max_tokens is not None and not (isinstance(max_tokens, int) and not isinstance(max_tokens, bool)
                                       and max_tokens >= 1):
        raise ConfigError("max_tokens must be None or an int >= 1")
    if temperature is not None and not (_num(temperature) and temperature >= 0):
        raise ConfigError("temperature must be None or a number >= 0")
    if provider is not None:
        if endpoint.kind != "openrouter":
            raise UnsupportedSetting("provider routing is only supported on openrouter")
        if not isinstance(provider, Mapping):
            raise ConfigError("provider must be a mapping")
    if extra is not None and (not isinstance(extra, Mapping) or {"model", "stream"} & set(extra)):
        raise ConfigError("extra must be a mapping without the keys 'model' or 'stream'")
    if not (_num(timeout) and timeout > 0):
        raise ConfigError("timeout must be a number > 0")
    if any(cb is not None and not callable(cb) for cb in callbacks):
        raise ConfigError("on_text and on_reasoning must be None or callable")


def complete(
    endpoint: Endpoint,
    model: str,
    input: str | Sequence[Message | Mapping[str, str]],
    *,
    reasoning: Literal["off", "low", "medium", "high"] | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    provider: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
    timeout: float = 900.0,
    on_text: Callable[[str], None] | None = None,
    on_reasoning: Callable[[str], None] | None = None,
) -> Result:
    _validate(endpoint, model, reasoning, max_tokens, temperature, provider, extra, timeout,
              (on_text, on_reasoning))
    messages = _messages(input)
    key = endpoint._resolve_key()
    if endpoint.kind == "openrouter" and not key:
        raise AuthError(f"no API key: set api_key or the {endpoint.api_key_env} environment variable")

    if endpoint.kind == "lmstudio" and reasoning == "off":
        route, url, hint = lmstudio_native, f"{endpoint._native_root}/api/v1/chat", "lmstudio_native"
    elif endpoint.kind == "lmstudio":
        route, url, hint = responses, f"{endpoint.base_url}/responses", "responses"
    else:
        route, url, hint = chat, f"{endpoint.base_url}/chat/completions", None

    streaming = on_text is not None or on_reasoning is not None
    body = route.build_body(messages, model, reasoning=reasoning, max_tokens=max_tokens,
                            temperature=temperature, provider=provider, streaming=streaming,
                            kind=endpoint.kind)
    body.update(extra or {})
    cap = body.get("max_output_tokens")
    cap = cap if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 1 else None

    call = _http.Call(url=url, key=key, timeout=timeout, model=model, route=hint)
    response = _http.post(call, body, endpoint.headers)
    acc = Acc(route.TRANSPORT, on_text, on_reasoning)
    try:
        if not streaming:
            return route.parse_response(_http.read_json(call, response), call, kind=endpoint.kind, token_cap=cap)
        return route.parse_stream(iter_sse(call, response), acc, call, kind=endpoint.kind, token_cap=cap)
    except Cancelled as exc:
        exc.partial = acc.partial()
        raise
    finally:
        response.close()
