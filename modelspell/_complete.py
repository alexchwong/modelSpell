"""Ordinary completion, configuration checks, route selection and dispatch (spec §6).

The dispatch and the opt-in HTTP 429 retry loop (spec v9 §6.11) are shared with complete_with_tools().
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Sequence

from . import _http
from ._cancel import CancelHandle, _Stop, _Watch
from ._json import _prepare_provider
from ._routes import Acc, chat, lmstudio_native, responses
from ._sse import iter_sse
from ._timing import _COMPLETION_TIMEOUT, _MAX_RETRY_WAIT, _check_retry_wait, _check_timeout
from ._types import KINDS, Endpoint, Message, ModelInfo, Result, RetryEvent
from .errors import AuthError, BackendError, Cancelled, ConfigError, ModelSpellError, UnsupportedSetting

_REASONING = ("off", "low", "medium", "high")
_MODEL_RULE = "model must be a non-empty str or a ModelInfo with a non-empty id"
# Generation settings checked and sent from the support tables, in spec order (v6 §6.8).
_SAMPLING = ("max_tokens", "temperature", "top_p", "top_k", "min_p", "repetition_penalty", "presence_penalty",
             "frequency_penalty", "seed", "stop")


def _num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite(value: Any) -> bool:
    """A finite number (not bool), before the integer encoding check (v6 §6.8.3)."""
    return _num(value) and (isinstance(value, int) or math.isfinite(value))


def _int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _in(lo: float, hi: float | None) -> Callable[[Any], bool]:
    return lambda v: _finite(v) and lo <= v and (hi is None or v <= hi)


def _stop(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and bool(value) and all(isinstance(s, str) and s for s in value)


# Value checks (v6 §6.8.1): kind-independent, from each setting's meaning.
_VALUE_CHECKS: dict[str, tuple[Callable[[Any], bool], str]] = {
    "max_tokens": (lambda v: _int(v) and v >= 1, "an int >= 1"),
    "temperature": (_in(0, None), "a finite number >= 0"),
    "top_p": (_in(0, 1), "a finite number between 0 and 1"),
    "top_k": (lambda v: _int(v) and v >= 0, "an int >= 0"),
    "min_p": (_in(0, 1), "a finite number between 0 and 1"),
    "repetition_penalty": (_in(0, None), "a finite number >= 0"),
    "presence_penalty": (_finite, "a finite number"),
    "frequency_penalty": (_finite, "a finite number"),
    "seed": (_int, "an int"),
    "stop": (_stop, "a non-empty list or tuple of non-empty strings"),
}


@dataclass(frozen=True)
class _Route:
    module: Any
    fields: Mapping[str, str]   # generation argument -> wire name (the support table)
    ranges: Mapping[str, tuple]  # documented per-route ranges
    label: str
    hint: str | None             # _http.Call.route


def _route(kind: str, reasoning: str | None) -> _Route:
    """The one route-selection rule (spec §6.3), shared by complete(), supported_settings(), check_settings()."""
    if kind == "lmstudio" and reasoning == "off":
        m = lmstudio_native
        return _Route(m, m.FIELDS, m.RANGES, m.LABEL, "lmstudio_native")
    if kind == "lmstudio":
        m = responses
        return _Route(m, m.FIELDS, m.RANGES, m.LABEL, "responses")
    return _Route(chat, chat.FIELDS[kind], chat.RANGES[kind], chat.LABEL[kind], None)


def _check_kind_reasoning(kind: Any, reasoning: Any) -> _Route:
    if kind not in KINDS:
        raise ConfigError(f"kind must be one of {KINDS}, got {kind!r}")
    if reasoning is not None and reasoning not in _REASONING:
        raise ConfigError(f"reasoning must be None or one of {_REASONING}")
    if reasoning is not None and kind == "openai_compat":
        raise UnsupportedSetting("openai_compat has no reasoning control; pass backend fields via extra=")
    return _route(kind, reasoning)


def _supported(kind: str, route: _Route) -> frozenset[str]:
    names = set(route.fields)
    if kind != "openai_compat":
        names.add("reasoning")
    if kind == "openrouter":
        names.add("provider")
    return frozenset(names)


def _unsupported_message(name: str, route: _Route) -> str:
    if route.module is responses:  # spec v7 §6.7.2 (M21-5): suggest reasoning="off" only where it helps
        if name in lmstudio_native.FIELDS:
            return (f'{name} is not supported on {route.label}; use reasoning="off", '
                    "or pass it through extra (effect unverified)")
        return (f"{name} is not supported on {route.label} or the LM Studio native route; "
                "pass it through extra (effect unverified)")
    return f"{name} is not supported on {route.label}; pass it through extra"


def _check_integer_encoding(name: str, value: int) -> None:
    try:
        int.__repr__(value)
    except ValueError:
        pass
    else:
        return
    raise ConfigError(f"{name} exceeds Python's integer digit limit for JSON encoding")


def _check_settings(kind: Any, reasoning: Any, settings: Mapping[str, Any], provider: Any) -> _Route:
    """The generation-setting checks (spec v6 §6.8.4), shared by check_settings() and complete()."""
    route = _check_kind_reasoning(kind, reasoning)
    for name in _SAMPLING:
        value = settings.get(name)
        if value is None:
            continue
        ok, rule = _VALUE_CHECKS[name]
        if not ok(value):
            raise ConfigError(f"{name} must be None or {rule}")
        if name not in route.fields:
            raise UnsupportedSetting(_unsupported_message(name, route))
        if name in route.ranges:
            lo, hi = route.ranges[name]
            if (lo is not None and value < lo) or value > hi:
                bounds = f"<= {hi}" if lo is None else f"between {lo} and {hi}"
                raise ConfigError(f"{name} must be {bounds} on {route.label}")
        if isinstance(value, int):
            _check_integer_encoding(name, value)
    if provider is not None:
        if kind != "openrouter":
            raise UnsupportedSetting("provider routing is only supported on openrouter")
        if not isinstance(provider, Mapping):
            raise ConfigError("provider must be a mapping")
    return route


def supported_settings(
    kind: Literal["lmstudio", "openrouter", "openai_compat"],
    reasoning: Literal["off", "low", "medium", "high"] | None = None,
) -> frozenset[str]:
    """Names of the generation settings modelSpell sends on the route for this kind and reasoning.

    Pure: no network, environment or state. Says nothing about whether a model, provider or server
    applies a setting it is sent. Raises as complete() would for the same kind and reasoning.
    """
    return _supported(kind, _check_kind_reasoning(kind, reasoning))


def check_settings(
    kind: Literal["lmstudio", "openrouter", "openai_compat"],
    *,
    reasoning: Literal["off", "low", "medium", "high"] | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    min_p: float | None = None,
    repetition_penalty: float | None = None,
    presence_penalty: float | None = None,
    frequency_penalty: float | None = None,
    seed: int | None = None,
    stop: list[str] | tuple[str, ...] | None = None,
    provider: Mapping[str, Any] | None = None,
) -> None:
    """Run complete()'s generation and provider checks without an endpoint, key, environment or network.

    Raises the same ConfigError / UnsupportedSetting, with the same message, that complete() would
    raise for these settings; returns None when they all pass.
    """
    _check_settings(kind, reasoning, dict(
        max_tokens=max_tokens, temperature=temperature, top_p=top_p, top_k=top_k, min_p=min_p,
        repetition_penalty=repetition_penalty, presence_penalty=presence_penalty,
        frequency_penalty=frequency_penalty, seed=seed, stop=stop), provider)
    _prepare_provider(provider)


def _message(item: Any) -> dict | None:
    """One text message as its wire dict, or None if `item` is not a text message."""
    if isinstance(item, Mapping):
        if set(item) != {"role", "content"}:
            raise ConfigError("a message mapping must have exactly the keys 'role' and 'content'")
        item = Message(item["role"], item["content"])
    elif not isinstance(item, Message):
        return None
    return {"role": item.role, "content": item.content}


def _messages(input: Any) -> list[dict]:
    if isinstance(input, str):
        return [{"role": "user", "content": input}]
    if not isinstance(input, Sequence) or not input:
        raise ConfigError("input must be a str or a non-empty sequence of messages")
    out = []
    for item in input:
        message = _message(item)
        if message is None:
            from ._tools import ToolOutput, ToolTurn  # deferred: _tools imports this module
            if isinstance(item, (ToolTurn, ToolOutput)):
                raise ConfigError(f"{type(item).__name__} entries need complete_with_tools(), not complete()")
            raise ConfigError(f"unsupported message item: {type(item).__name__}")
        out.append(message)
    return out


def _model_arg(model: Any) -> tuple[str, ModelInfo | None]:
    """The model id to send, and the caller's discovery entry if one was passed (spec v7 §6.1, M21-7)."""
    if isinstance(model, ModelInfo):
        if not isinstance(model.id, str) or not model.id:
            raise ConfigError(_MODEL_RULE)
        return model.id, model
    if not isinstance(model, str) or not model:
        raise ConfigError(_MODEL_RULE)
    return model, None


def _check_completion_config(
    endpoint, model, reasoning, settings, provider, timeout, rate_limit_retries, max_retry_wait,
) -> tuple[_Route, str, ModelInfo | None, dict | None]:
    if not isinstance(endpoint, Endpoint):
        raise ConfigError("endpoint must be an Endpoint")
    model_id, info = _model_arg(model)
    route = _check_settings(endpoint.kind, reasoning, settings, provider)
    policy = _prepare_provider(provider)
    _check_timeout(timeout)
    _check_retry_limits(rate_limit_retries, max_retry_wait)
    return route, model_id, info, policy


# --- opt-in HTTP 429 recovery (spec v9 §6.11) ----------------------------------------------------
_BACKOFF = (1.0, 2.0, 4.0)  # nominal wait before attempts 2, 3, 4
MAX_RATE_LIMIT_RETRIES = len(_BACKOFF)


def _check_retry_limits(rate_limit_retries: Any, max_retry_wait: Any) -> None:
    if not (_int(rate_limit_retries) and 0 <= rate_limit_retries <= MAX_RATE_LIMIT_RETRIES):
        raise ConfigError(f"rate_limit_retries must be an int from 0 to {MAX_RATE_LIMIT_RETRIES}")
    _check_retry_wait(max_retry_wait)


def _check_on_retry(on_retry: Any) -> None:
    if on_retry is not None and not callable(on_retry):
        raise ConfigError("on_retry must be None or callable")


@dataclass(frozen=True)
class _Retry:
    retries: int
    max_wait: float
    on_retry: Callable[[RetryEvent], None] | None


class _RetryCallbackError:
    """Holds the exception on_retry raised, so dispatch tells it from the package's own errors."""
    __slots__ = ("error",)

    def __init__(self) -> None:
        self.error: BaseException | None = None


def _post(call: _http.Call, data: bytes, headers, watch: _Watch, retry: _Retry, holder: _RetryCallbackError):
    """POST `data`, retrying a transport HTTP 429 when opted in. Nothing else is retried."""
    attempt, waited = 1, 0.0
    while True:
        try:
            return _http.post_data(call, data, headers, watch)
        except BackendError as exc:
            if not _http.is_transport_429(exc) or attempt > retry.retries:
                raise
            delay = max(_BACKOFF[attempt - 1], exc._retry_after or 0.0)
            if waited + delay > retry.max_wait:
                raise
            watch.end_attempt()
            if retry.on_retry is not None:
                try:
                    retry.on_retry(RetryEvent(attempt, attempt + 1, float(delay), exc.status))
                except BaseException as cb_exc:
                    holder.error = cb_exc
                    raise
            watch.pause(delay)
            waited += delay
            attempt += 1


def _dispatch(call: _http.Call, data: bytes, headers, *, cancel, transport: str, on_text, on_reasoning,
              retry: _Retry, parse: Callable[[Any, Acc, _Watch], Any]) -> Any:
    """Send a prepared request through the watched transport and parse it (spec v8 §6.10, v9 §6.11)."""
    # spec v8 §6.10, §9.6: every completion goes through the watched transport (M30-9). Without a
    # handle the watch can never be cancelled. Pre-send errors above take precedence (M30-7).
    watch = _Watch(cancel)
    acc = Acc(transport, on_text, on_reasoning, watch)
    holder = _RetryCallbackError()
    try:
        with watch:  # raises _Stop at once if the handle is already cancelled
            response = _post(call, data, headers, watch, retry, holder)
            try:
                return parse(response, acc, watch)
            finally:
                response.close()
    except Cancelled as exc:
        if exc is acc.callback_error or exc is holder.error:  # raised by a callback (MS-5)
            exc.partial, exc.sent = acc.partial(), watch.sent if exc is holder.error else True
        raise
    except _Stop:
        raise _cancelled(call, acc, watch) from None
    except ModelSpellError as exc:
        if (exc is not acc.callback_error and exc is not holder.error
                and watch.cancelled_before_commit()):
            raise _cancelled(call, acc, watch) from None
        raise


def _cancelled(call: _http.Call, acc: Acc, watch: _Watch) -> Cancelled:
    """The Cancelled for a cancellation through the handle (spec v8 §6.10)."""
    exc = call.error(Cancelled, "cancelled" if watch.sent else "cancelled before sending")
    exc.partial, exc.sent = acc.partial(), watch.sent
    return exc


def check_completion(
    endpoint: Endpoint,
    model: str | ModelInfo,
    *,
    reasoning: Literal["off", "low", "medium", "high"] | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    min_p: float | None = None,
    repetition_penalty: float | None = None,
    presence_penalty: float | None = None,
    frequency_penalty: float | None = None,
    seed: int | None = None,
    stop: list[str] | tuple[str, ...] | None = None,
    provider: Mapping[str, Any] | None = None,
    timeout: float = _COMPLETION_TIMEOUT,
    rate_limit_retries: int = 0,
    max_retry_wait: float = _MAX_RETRY_WAIT,
    **unknown: Any,
) -> None:
    """Check an ordinary completion's target, typed settings and transport options offline.

    Raises as complete() would for those options. Unknown keywords take precedence.
    Needs no credentials or messages and does not establish server or model acceptance.
    """
    if unknown:
        raise ConfigError(f"unknown completion option: {min(unknown)}")
    settings = dict(max_tokens=max_tokens, temperature=temperature, top_p=top_p, top_k=top_k, min_p=min_p,
                    repetition_penalty=repetition_penalty, presence_penalty=presence_penalty,
                    frequency_penalty=frequency_penalty, seed=seed, stop=stop)
    _check_completion_config(endpoint, model, reasoning, settings, provider, timeout,
                             rate_limit_retries, max_retry_wait)


def complete(
    endpoint: Endpoint,
    model: str | ModelInfo,
    input: str | Sequence[Message | Mapping[str, str]],
    *,
    reasoning: Literal["off", "low", "medium", "high"] | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    min_p: float | None = None,
    repetition_penalty: float | None = None,
    presence_penalty: float | None = None,
    frequency_penalty: float | None = None,
    seed: int | None = None,
    stop: list[str] | tuple[str, ...] | None = None,
    provider: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
    timeout: float = _COMPLETION_TIMEOUT,
    on_text: Callable[[str], None] | None = None,
    on_reasoning: Callable[[str], None] | None = None,
    cancel: CancelHandle | None = None,
    rate_limit_retries: int = 0,
    max_retry_wait: float = _MAX_RETRY_WAIT,
    on_retry: Callable[[RetryEvent], None] | None = None,
) -> Result:
    settings = dict(max_tokens=max_tokens, temperature=temperature, top_p=top_p, top_k=top_k, min_p=min_p,
                    repetition_penalty=repetition_penalty, presence_penalty=presence_penalty,
                    frequency_penalty=frequency_penalty, seed=seed, stop=stop)
    route, model_id, info, policy = _check_completion_config(
        endpoint, model, reasoning, settings, provider, timeout, rate_limit_retries, max_retry_wait)
    if extra is not None and (not isinstance(extra, Mapping) or {"model", "stream"} & set(extra)):
        raise ConfigError("extra must be a mapping without the keys 'model' or 'stream'")
    if any(cb is not None and not callable(cb) for cb in (on_text, on_reasoning)):
        raise ConfigError("on_text and on_reasoning must be None or callable")
    if cancel is not None and not isinstance(cancel, CancelHandle):
        raise ConfigError("cancel must be None or a CancelHandle")
    _check_on_retry(on_retry)
    messages = _messages(input)
    key = endpoint._resolve_key()
    if endpoint.kind == "openrouter" and not key:
        raise AuthError(f"no API key: set api_key or the {endpoint.api_key_env} environment variable")

    if route.module is lmstudio_native:
        url = f"{endpoint._native_root}/api/v1/chat"
    elif route.module is responses:
        url = f"{endpoint.base_url}/responses"
    else:
        url = f"{endpoint.base_url}/chat/completions"

    streaming = on_text is not None or on_reasoning is not None
    # spec v7 §7.3 (M21-8): a model with no reasoning configuration has nothing to turn off, and LM Studio
    # refuses reasoning="off" for it, so the native body omits the field. Only when the caller passed a ModelInfo.
    body_reasoning = reasoning
    if route.module is lmstudio_native and info is not None and not info.reasoning_options:
        body_reasoning = None
    body = route.module.build_body(messages, model_id, reasoning=body_reasoning, provider=policy,
                                   settings={k: v for k, v in settings.items() if v is not None},
                                   streaming=streaming, kind=endpoint.kind)
    body.update(extra or {})
    cap = body.get("max_output_tokens")
    cap = cap if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 1 else None

    call = _http.Call(url=url, key=key, timeout=timeout, model=model_id, route=route.hint)

    def parse(response, acc: Acc, watch: _Watch) -> Result:
        if not streaming:
            return route.module.parse_response(_http.read_json(call, response, watch=watch), call,
                                               kind=endpoint.kind, token_cap=cap)
        return route.module.parse_stream(iter_sse(call, response, watch), acc, call,
                                         kind=endpoint.kind, token_cap=cap)

    return _dispatch(call, _http.encode(body), endpoint.headers, cancel=cancel, transport=route.module.TRANSPORT,
                     on_text=on_text, on_reasoning=on_reasoning,
                     retry=_Retry(rate_limit_retries, float(max_retry_wait), on_retry), parse=parse)
