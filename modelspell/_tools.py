"""Client-executed function calling: complete_with_tools(), check_tool_request() and tool types (spec v9 §21).

modelSpell sends the offered functions, returns complete calls only from a cleanly finished turn,
and replays a caller-owned transcript. It never executes, authorises or schema-checks a call;
the caller (AgentWiz) does all of that.
"""
from __future__ import annotations

import copy
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Sequence

from . import _http
from ._cancel import CancelHandle, _Watch
from ._complete import (_Retry, _check_kind_reasoning, _check_on_retry, _check_retry_limits, _check_settings,
                        _dispatch, _message, _model_arg)
from ._json import FrozenMap, JSONValueError, canon, dumps, freeze
from ._routes import Acc, lmstudio_native, responses
from ._sse import iter_sse
from ._timing import _COMPLETION_TIMEOUT, _MAX_RETRY_WAIT, _check_timeout
from ._types import Endpoint, Message, ModelInfo, Result, RetryEvent
from .errors import AuthError, ConfigError, ResponseFormatError, UnsupportedSetting

_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SEAL = object()  # only the response parser builds a replayable ToolTurn (spec v9 §21.3)


# --- public value types ------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolDefinition:
    """A function offered to the model. `parameters` is a JSON Schema object, snapshotted deeply.

    Accepted schema values: str-keyed mappings, lists or tuples, str, bool, None, int, finite float
    and finite Decimal. Ints and Decimals are sent with their exact digits.
    """
    name: str
    description: str
    parameters: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME.fullmatch(self.name):
            raise ConfigError(f"tool name must match ^[A-Za-z0-9_-]{{1,64}}$, got {self.name!r}")
        if not isinstance(self.description, str):
            raise ConfigError(f"tool {self.name!r}: description must be a str")
        if not isinstance(self.parameters, Mapping):
            raise ConfigError(f"tool {self.name!r}: parameters must be a JSON Schema object (a mapping)")
        try:
            frozen = freeze(self.parameters, "parameters")
        except JSONValueError as exc:
            raise ConfigError(f"tool {self.name!r}: {exc}") from None
        if "type" in frozen and frozen["type"] != "object":
            raise ConfigError(f"tool {self.name!r}: parameters must describe an object (type 'object')")
        object.__setattr__(self, "parameters", frozen)


@dataclass(frozen=True)
class ToolCall:
    """One complete call from a cleanly finished turn. `arguments_json` is the provider's string, verbatim."""
    id: str
    name: str
    arguments_json: str

    def __post_init__(self) -> None:
        for field_name in ("id", "name", "arguments_json"):
            if not isinstance(getattr(self, field_name), str):
                raise ConfigError(f"ToolCall.{field_name} must be a str")


@dataclass(frozen=True)
class ToolOutput:
    """The caller's result or validation-error payload for one call, as text."""
    call_id: str
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not self.call_id:
            raise ConfigError("ToolOutput.call_id must be a non-empty str")
        if not isinstance(self.content, str) or not self.content:
            raise ConfigError("ToolOutput.content must be a non-empty str")


class ToolTurn:
    """A completed model turn from complete_with_tools(). Created only by modelSpell's response parser.

    clean is True exactly when `replay` holds the validated snapshot; only then can the turn be
    appended to history, and only then does `tool_calls` hold calls. A diagnostic turn
    (clean False) has replay None and tool_calls (). Shallow and deep copies stay replayable;
    pickling is unsupported.
    """

    __slots__ = ("_result", "_replay", "_identity", "_tool_calls", "_seal")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("ToolTurn is created only by complete_with_tools()")

    @classmethod
    def _make(cls, result: Result, replay: tuple | None, identity: FrozenMap) -> "ToolTurn":
        self = object.__new__(cls)
        calls = _calls_from(replay, identity["transport"]) if replay is not None else ()
        for name, value in (("_result", result), ("_replay", replay), ("_identity", identity),
                            ("_tool_calls", calls), ("_seal", _SEAL)):
            object.__setattr__(self, name, value)
        return self

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("ToolTurn is read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("ToolTurn is read-only")

    @property
    def result(self) -> Result:
        return self._result

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        return self._tool_calls

    @property
    def replay(self) -> tuple[Mapping[str, Any], ...] | None:
        """The assistant content sent on continuation, read-only: one Chat message or Responses output items."""
        return self._replay

    @property
    def route_identity(self) -> Mapping[str, Any]:
        return self._identity

    @property
    def clean(self) -> bool:
        return self._replay is not None

    def __copy__(self) -> "ToolTurn":
        return ToolTurn._make(self._result, self._replay, self._identity)

    def __deepcopy__(self, memo) -> "ToolTurn":
        return ToolTurn._make(copy.deepcopy(self._result, memo), self._replay, self._identity)

    def __reduce_ex__(self, protocol):
        raise TypeError("ToolTurn cannot be pickled; replay is supported only for in-memory turns")

    def __repr__(self) -> str:
        return (f"ToolTurn(clean={self.clean}, tool_calls={self._tool_calls!r}, "
                f"finish_reason={self._result.finish_reason!r}, transport={self._identity['transport']!r})")


def _calls_from(replay: tuple, transport: str) -> tuple[ToolCall, ...]:
    if transport == "chat_completions":
        return tuple(ToolCall(c["id"], c["function"]["name"], c["function"]["arguments"])
                     for message in replay for c in message.get("tool_calls", ()))
    return tuple(ToolCall(item["call_id"], item["name"], item["arguments"])
                 for item in replay if item.get("type") == "function_call")


# --- shared validators (preflight and completion) -------------------------------------------------

def _check_tools(tools: Any) -> tuple[ToolDefinition, ...]:
    if not isinstance(tools, (list, tuple)) or not tools:
        raise ConfigError("tools must be a non-empty list or tuple of ToolDefinition")
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, ToolDefinition):
            raise ConfigError(f"tools must contain only ToolDefinition, got {type(tool).__name__}")
        if tool.name in names:
            raise ConfigError(f"duplicate tool name {tool.name!r}")
        names.add(tool.name)
    return tuple(tools)


_PIN_RULE = ("tool requests on openrouter need an explicit provider pin: provider={'order': [<one slug>], "
             "'allow_fallbacks': False}")


def _base(slug: str) -> str:
    return slug.split("/", 1)[0].lower()


def _tool_provider(kind: str, provider: Any) -> FrozenMap | None:
    """The effective OpenRouter routing policy of a tool request (spec v9 §21.4), or None off OpenRouter."""
    if kind != "openrouter":
        return None  # _check_settings has refused a provider mapping on other kinds
    if provider is None:
        raise ConfigError(_PIN_RULE)
    try:
        policy = freeze(provider, "provider")
    except JSONValueError as exc:
        raise ConfigError(f"provider must be a JSON mapping: {exc}") from None
    order = policy.get("order")
    if not (isinstance(order, tuple) and len(order) == 1 and isinstance(order[0], str) and order[0].strip()):
        raise ConfigError(f"{_PIN_RULE}; provider.order must hold exactly one non-empty slug")
    slug = order[0]
    if policy.get("allow_fallbacks") is not False:
        raise ConfigError(f"{_PIN_RULE}; provider.allow_fallbacks must be False")
    if "only" in policy and policy["only"] != (slug,):
        raise ConfigError(f"provider.only must be absent or exactly [{slug!r}] on a tool request")
    if "ignore" in policy:
        ignore = policy["ignore"]
        if not isinstance(ignore, tuple) or not all(isinstance(e, str) for e in ignore):
            raise ConfigError("provider.ignore must be a list of slugs")
        for entry in ignore:
            if entry.lower() in (slug.lower(), _base(slug)):
                raise ConfigError(f"provider.ignore entry {entry!r} excludes the pinned provider {slug!r}")
    if "require_parameters" in policy and policy["require_parameters"] is not True:
        raise ConfigError("tool requests on openrouter require provider.require_parameters=True")
    return FrozenMap({**policy, "require_parameters": True})


def _check_tool_request(kind: Any, reasoning: Any, settings: Mapping[str, Any], provider: Any, tools: Any,
                        rate_limit_retries: Any, max_retry_wait: Any):
    route = _check_kind_reasoning(kind, reasoning)
    if route.module is lmstudio_native:
        raise UnsupportedSetting('tool requests are not supported on LM Studio with reasoning="off" '
                                 "(the native chat route); use the default or a low/medium/high setting")
    route = _check_settings(kind, reasoning, settings, provider)
    tools = _check_tools(tools)
    policy = _tool_provider(kind, provider)
    _check_retry_limits(rate_limit_retries, max_retry_wait)
    return route, tools, policy


def check_tool_request(
    kind: Literal["lmstudio", "openrouter", "openai_compat"],
    *,
    tools: Sequence[ToolDefinition],
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
    rate_limit_retries: int = 0,
    max_retry_wait: float = _MAX_RETRY_WAIT,
) -> None:
    """Run complete_with_tools()'s route, setting, tool, provider-pin and retry checks without I/O.

    Needs no endpoint, key, transcript or network. Raises what complete_with_tools() would raise for
    these arguments; returns None when they all pass.
    """
    _check_tool_request(kind, reasoning, dict(
        max_tokens=max_tokens, temperature=temperature, top_p=top_p, top_k=top_k, min_p=min_p,
        repetition_penalty=repetition_penalty, presence_penalty=presence_penalty,
        frequency_penalty=frequency_penalty, seed=seed, stop=stop), provider, tools,
        rate_limit_retries, max_retry_wait)


# --- route identity and transcript --------------------------------------------------------------

_DEFAULT_PORT = {"http": 80, "https": 443}


def _norm_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host if port in (None, _DEFAULT_PORT.get(scheme)) else f"{host}:{port}"
    if parts.username is not None or parts.password is not None:
        netloc = f"{parts.netloc.rsplit('@', 1)[0]}@{netloc}"
    return urllib.parse.urlunsplit((scheme, netloc, parts.path.rstrip("/"), parts.query, ""))


def _identity(endpoint: Endpoint, model_id: str, transport: str, reasoning: Any,
              policy: FrozenMap | None) -> FrozenMap:
    return FrozenMap({"kind": endpoint.kind, "base_url": _norm_url(endpoint.base_url), "model": model_id,
                      "transport": transport, "reasoning": reasoning, "provider": policy})


def _check_turn(turn: ToolTurn, identity: FrozenMap, pos: int) -> None:
    if getattr(turn, "_seal", None) is not _SEAL or type(turn) is not ToolTurn:
        raise ConfigError(f"input[{pos}]: ToolTurn was not created by complete_with_tools()")
    if turn._replay is None:
        raise ConfigError(f"input[{pos}]: a diagnostic ToolTurn (clean is False) cannot be appended to history")
    theirs = turn._identity
    for key in identity:
        if canon(theirs.get(key)) != canon(identity[key]):
            raise ConfigError(f"input[{pos}]: ToolTurn came from a different route ({key} differs); "
                              "replay needs the same kind, base URL, model, transport, reasoning and provider")


def _entries(input: Any, identity: FrozenMap) -> list[tuple[str, Any]]:
    """The validated transcript: ("message", dict) | ("turn", ToolTurn) | ("output", ToolOutput), outputs in call order."""
    if isinstance(input, str):
        return [("message", {"role": "user", "content": input})]
    if not isinstance(input, Sequence) or not input:
        raise ConfigError("input must be a str or a non-empty sequence of messages, ToolTurns and ToolOutputs")
    out: list[tuple[str, Any]] = []
    pending: tuple[tuple[str, ...], dict[str, ToolOutput]] | None = None
    for pos, item in enumerate(input):
        if isinstance(item, ToolOutput):
            if pending is None:
                raise ConfigError(f"input[{pos}]: ToolOutput {item.call_id!r} does not follow a tool-call turn")
            ids, got = pending
            if item.call_id not in ids:
                raise ConfigError(f"input[{pos}]: ToolOutput {item.call_id!r} matches no call of the preceding turn")
            if item.call_id in got:
                raise ConfigError(f"input[{pos}]: duplicate ToolOutput for call {item.call_id!r}")
            got[item.call_id] = item
            if len(got) == len(ids):
                out.extend(("output", got[i]) for i in ids)
                pending = None
            continue
        if pending is not None:
            missing = [i for i in pending[0] if i not in pending[1]]
            raise ConfigError(f"input[{pos}]: missing ToolOutput for calls {missing} before the next entry")
        if isinstance(item, ToolTurn):
            _check_turn(item, identity, pos)
            out.append(("turn", item))
            if item.tool_calls:
                pending = (tuple(c.id for c in item.tool_calls), {})
            continue
        message = _message(item)
        if message is None:
            raise ConfigError(f"input[{pos}]: unsupported entry {type(item).__name__}")
        out.append(("message", message))
    if pending is not None:
        missing = [i for i in pending[0] if i not in pending[1]]
        raise ConfigError(f"missing ToolOutput for calls {missing} at the end of input")
    return out


# --- complete_with_tools() ---------------------------------------------------------------------

def _freeze_replay(call: _http.Call, items: list) -> tuple:
    try:
        return freeze(items, "replay")
    except JSONValueError as exc:
        raise call.error(ResponseFormatError, f"unexpected tool-call response: {exc}", status=200) from None


def complete_with_tools(
    endpoint: Endpoint,
    model: str | ModelInfo,
    input: str | Sequence[Message | Mapping[str, str] | ToolTurn | ToolOutput],
    *,
    tools: Sequence[ToolDefinition],
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
    on_text: Callable[[str], None] | None = None,
    on_reasoning: Callable[[str], None] | None = None,
    cancel: CancelHandle | None = None,
    rate_limit_retries: int = 0,
    max_retry_wait: float = _MAX_RETRY_WAIT,
    on_retry: Callable[[RetryEvent], None] | None = None,
) -> ToolTurn:
    """One model turn offering `tools`. Sends one HTTP POST unless rate_limit_retries opts in to 429 retries."""
    settings = dict(max_tokens=max_tokens, temperature=temperature, top_p=top_p, top_k=top_k, min_p=min_p,
                    repetition_penalty=repetition_penalty, presence_penalty=presence_penalty,
                    frequency_penalty=frequency_penalty, seed=seed, stop=stop)
    if not isinstance(endpoint, Endpoint):
        raise ConfigError("endpoint must be an Endpoint")
    model_id, _ = _model_arg(model)
    route, tools, policy = _check_tool_request(endpoint.kind, reasoning, settings, provider, tools,
                                               rate_limit_retries, max_retry_wait)
    _check_timeout(timeout)
    if any(cb is not None and not callable(cb) for cb in (on_text, on_reasoning)):
        raise ConfigError("on_text and on_reasoning must be None or callable")
    if cancel is not None and not isinstance(cancel, CancelHandle):
        raise ConfigError("cancel must be None or a CancelHandle")
    _check_on_retry(on_retry)
    module = route.module
    identity = _identity(endpoint, model_id, module.TRANSPORT, reasoning, policy)
    entries = _entries(input, identity)
    key = endpoint._resolve_key()
    if endpoint.kind == "openrouter" and not key:
        raise AuthError(f"no API key: set api_key or the {endpoint.api_key_env} environment variable")

    url = f"{endpoint.base_url}/responses" if module is responses else f"{endpoint.base_url}/chat/completions"
    streaming = on_text is not None or on_reasoning is not None
    body = module.build_tool_body(entries, model_id, reasoning=reasoning, provider=policy,
                                  settings={k: v for k, v in settings.items() if v is not None},
                                  streaming=streaming, kind=endpoint.kind, tools=tools)
    data = dumps(body).encode("utf-8")
    call = _http.Call(url=url, key=key, timeout=timeout, model=model_id, route=route.hint)

    def parse(response, acc: Acc, watch: _Watch) -> ToolTurn:
        if streaming:
            result, items = module.parse_tool_stream(iter_sse(call, response, watch), acc, call, kind=endpoint.kind)
        else:
            result, items = module.parse_tool_response(_http.read_json(call, response, watch=watch), call,
                                                       kind=endpoint.kind)
        return ToolTurn._make(result, _freeze_replay(call, items) if items is not None else None, identity)

    return _dispatch(call, data, endpoint.headers, cancel=cancel, transport=module.TRANSPORT, on_text=on_text,
                     on_reasoning=on_reasoning, retry=_Retry(rate_limit_retries, float(max_retry_wait), on_retry),
                     parse=parse)

