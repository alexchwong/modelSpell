"""Offline acceptance checks for ordinary completion configuration and shared limits."""
import sys
import traceback
from decimal import Decimal
from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock

import pytest

from conftest import Reply, canned, ep
from examples import batch_selection
from modelspell import (ConfigError, Endpoint, ToolDefinition, check_completion, check_settings,
                        check_tool_request, choose_provider, complete, complete_with_tools,
                        list_models, list_providers)

MODEL = "test/model"
TOOL = ToolDefinition("lookup", "Look up a label.", {"type": "object"})


@pytest.mark.parametrize("name", ["input", "messages", "extra", "on_text", "on_reasoning", "cancel", "on_retry", "tools"])
def test_unknown_options_precede_typed_checks(name):
    with pytest.raises(ConfigError, match=rf"unknown completion option: {name}$"):
        check_completion(None, "", timeout=None, **{"zzz": True, name: None})
    with pytest.raises(TypeError):
        complete(None, "", unknown=True)


def test_endpoint_type_parity():
    with pytest.raises(ConfigError) as preflight:
        check_completion("openrouter", MODEL)
    with pytest.raises(ConfigError) as runtime:
        complete("openrouter", MODEL, "ping")
    assert str(preflight.value) == str(runtime.value) == "endpoint must be an Endpoint"


@pytest.mark.parametrize("timeout", [
    pytest.param(value, id=label) for label, value in (
        ("zero", 0), ("negative", -1), ("over-cap", 1_000_001), ("bool", True),
        ("none", None), ("nan", float("nan")), ("infinity", float("inf")),
        ("negative-infinity", float("-inf")), ("oversized-int", 10**5000), ("string", "15"),
    )
])
def test_invalid_timeout_fails_before_credentials_or_send(server, monkeypatch, timeout):
    endpoint = ep(server, "openrouter")
    resolve = Mock(side_effect=AssertionError("credential lookup"))
    monkeypatch.setattr(Endpoint, "_resolve_key", resolve)
    for call in (
        lambda: check_completion(endpoint, MODEL, timeout=timeout),
        lambda: complete(endpoint, MODEL, "ping", timeout=timeout),
        lambda: complete_with_tools(endpoint, MODEL, "ping", tools=[TOOL], timeout=timeout,
                                    provider={"order": ["p"], "allow_fallbacks": False}),
        lambda: list_models(endpoint, timeout=timeout),
        lambda: list_providers(endpoint, MODEL, timeout=timeout),
        lambda: choose_provider(endpoint, MODEL, timeout=timeout),
    ):
        with pytest.raises(ConfigError, match="^timeout .*1000000 seconds"):
            call()
    resolve.assert_not_called()
    assert server.connections == 0


@pytest.mark.parametrize("limit", [1_000_000, 1_000_000.0])
def test_inclusive_time_cap(server, limit):
    endpoint = ep(server, "openrouter")
    server.reply(Reply(body=b'{"data": []}'), Reply(body=b'{"data": {"endpoints": []}}'), canned("chat", False))
    check_completion(endpoint, MODEL, timeout=limit, max_retry_wait=limit)
    assert list_models(endpoint, timeout=limit) == []
    assert list_providers(endpoint, MODEL, timeout=limit) == []
    selected = choose_provider(endpoint, MODEL, mode="managed", timeout=limit)
    assert selected.policy is None
    complete(endpoint, MODEL, "ping", timeout=limit, max_retry_wait=limit)
    pin = {"order": ["p"], "allow_fallbacks": False}
    check_tool_request("openrouter", tools=[TOOL], provider=pin, max_retry_wait=limit)
    complete_with_tools(endpoint, MODEL, "ping", tools=[TOOL], provider=pin, timeout=limit, max_retry_wait=limit)
    assert [r["method"] for r in server.requests] == ["GET", "GET", "POST", "POST"]


@pytest.mark.parametrize("value", [
    pytest.param(value, id=label) for label, value in (
        ("negative", -1), ("over-cap", 1_000_001), ("bool", False), ("none", None),
        ("nan", float("nan")), ("infinity", float("inf")), ("negative-infinity", float("-inf")),
        ("oversized-int", 10**5000),
    )
])
def test_retry_wait_rejections_match_both_preflights(server, value):
    endpoint = ep(server, "openai_compat")
    options = {"max_retry_wait": value}
    for call in (
        lambda: check_completion(endpoint, MODEL, **options),
        lambda: complete(endpoint, MODEL, "ping", **options),
        lambda: check_tool_request(endpoint.kind, tools=[TOOL], **options),
        lambda: complete_with_tools(endpoint, MODEL, "ping", tools=[TOOL], **options),
    ):
        with pytest.raises(ConfigError, match="^max_retry_wait .* between 0 and 1000000 seconds$"):
            call()
    assert server.connections == 0


@pytest.mark.parametrize("retries", [None, True, -1, 4, 1.5])
def test_ordinary_retry_count_matches_preflight(server, retries):
    endpoint = ep(server, "openai_compat")
    with pytest.raises(ConfigError) as preflight:
        check_completion(endpoint, MODEL, rate_limit_retries=retries)
    with pytest.raises(ConfigError) as runtime:
        complete(endpoint, MODEL, "ping", rate_limit_retries=retries)
    assert str(preflight.value) == str(runtime.value) == "rate_limit_retries must be an int from 0 to 3"
    assert server.connections == 0


def test_generation_integer_digit_limit_matches_both_completion_paths(server):
    limit = getattr(sys, "get_int_max_str_digits", lambda: 0)()
    if not limit:
        pytest.skip("Python integer digit limit is inactive")
    settings = {"max_tokens": 10**limit}
    endpoint = ep(server, "openrouter")
    pin = {"order": ["p"], "allow_fallbacks": False}
    errors = []
    for call in (
        lambda: check_completion(endpoint, MODEL, **settings),
        lambda: check_settings(endpoint.kind, **settings),
        lambda: complete(endpoint, MODEL, "ping", **settings),
        lambda: check_tool_request(endpoint.kind, tools=[TOOL], provider=pin, **settings),
        lambda: complete_with_tools(endpoint, MODEL, "ping", tools=[TOOL], provider=pin, **settings),
        lambda: choose_provider(endpoint, MODEL, tools=True, settings=settings),
    ):
        with pytest.raises(ConfigError, match="^max_tokens .*digit limit") as caught:
            call()
        assert caught.value.__context__ is None
        errors.append(str(caught.value))
    assert len(set(errors)) == 1 and server.connections == 0


class IntSubclass(int):
    pass


class UnsupportedValue:
    def __repr__(self):
        return "VALUE_MARKER"


@pytest.mark.parametrize("case", ["nan", "key", "cycle", "unsupported", "decimal-overflow", "subclass", "digits"])
def test_ordinary_provider_preparation_errors_are_fixed_and_unchained(server, case):
    nested = {"nan": [float("nan")], "key": {2: "VALUE_MARKER"}, "cycle": {},
              "unsupported": UnsupportedValue(), "decimal-overflow": Decimal("1e400"),
              "subclass": IntSubclass(1), "digits": None}[case]
    if case == "cycle":
        nested["VALUE_MARKER"] = nested
    if case == "digits":
        limit = getattr(sys, "get_int_max_str_digits", lambda: 0)()
        if not limit:
            pytest.skip("Python integer digit limit is inactive")
        nested = 10**limit
    policy = {"KEY_MARKER": nested}
    endpoint = ep(server, "openrouter")
    for call in (
        lambda: check_settings(endpoint.kind, provider=policy),
        lambda: check_completion(endpoint, MODEL, provider=policy),
        lambda: complete(endpoint, MODEL, "ping", provider=policy),
        lambda: choose_provider(endpoint, MODEL, provider=policy),
    ):
        with pytest.raises(ConfigError) as caught:
            call()
        error = caught.value
        assert str(error) == "provider must be a JSON object with string keys and encodable JSON values"
        assert error.__cause__ is error.__context__ is None
        trace = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        assert "KEY_MARKER" not in trace and "VALUE_MARKER" not in trace
    assert server.connections == 0


def test_ordinary_and_managed_policies_use_same_json_wire_values(server):
    endpoint = ep(server, "openrouter")
    server.reply(canned("chat", False))
    price = Decimal("0.1234567890123456789012345678")
    policy = {"zdr": True, "max_price": MappingProxyType({"prompt": price}), "ignore": ("a", "b")}
    check_completion(endpoint, MODEL, provider=policy)
    selected = choose_provider(endpoint, MODEL, mode="managed", provider=policy)
    check_completion(endpoint, MODEL, provider=selected.policy)
    assert server.requests == []
    for effective in (policy, selected.policy):
        complete(endpoint, MODEL, "ping", provider=effective)
    assert server.requests[0]["raw"] == server.requests[1]["raw"]
    assert server.requests[0]["body"]["provider"] == {
        "zdr": True, "max_price": {"prompt": float(price)}, "ignore": ["a", "b"]}
    empty = choose_provider(endpoint, MODEL, mode="managed", provider={})
    assert empty.policy is not None and dict(empty.policy) == {}
    complete(endpoint, MODEL, "ping", provider=empty.policy)
    assert server.requests[-1]["body"]["provider"] == {}


def test_tool_policy_keeps_decimal_digits_and_range(server):
    endpoint = ep(server, "openrouter")
    server.reply(canned("chat", False))
    price = "0.1234567890123456789012345678"
    policy = {"order": ["p"], "allow_fallbacks": False,
              "max_price": {"prompt": Decimal(price), "completion": Decimal("1e400")}}
    check_tool_request(endpoint.kind, tools=[TOOL], provider=policy)
    complete_with_tools(endpoint, MODEL, "ping", tools=[TOOL], provider=policy)
    assert f'"max_price": {{"prompt": {price}, "completion": 1E+400}}'.encode() in server.requests[0]["raw"]


@pytest.fixture
def batch_calls(monkeypatch):
    calls = Mock()
    for name in ("check_completion", "choose_provider", "complete"):
        monkeypatch.setattr(batch_selection, name, getattr(calls, name))
    return calls


def test_batch_example_empty(batch_calls):
    assert batch_selection.run_batch(None, "", [], estimated_input_tokens=100) == []
    assert batch_calls.mock_calls == []


def test_batch_example_refuses_before_selection(batch_calls):
    batch_calls.check_completion.side_effect = ConfigError("invalid")
    with pytest.raises(ConfigError, match="invalid"):
        batch_selection.run_batch(None, MODEL, ["ping"], estimated_input_tokens=100)
    assert [c[0] for c in batch_calls.mock_calls] == ["check_completion"]


def test_batch_example_preflights_and_selects_once(batch_calls):
    from modelspell import ModelInfo

    endpoint = Endpoint("openrouter")
    model = ModelInfo(MODEL, None, None, None, None, None, None, None, {})
    effective = MappingProxyType({"zdr": True, "order": ("p",), "allow_fallbacks": False})
    batch_calls.choose_provider.return_value = SimpleNamespace(policy=effective)
    batch_calls.complete.side_effect = ["first", "second"]
    assert batch_selection.run_batch(endpoint, model, ["a", "b"], estimated_input_tokens=100) == ["first", "second"]
    assert [c[0] for c in batch_calls.mock_calls] == ["check_completion", "choose_provider", "complete", "complete"]
    assert batch_calls.check_completion.call_args.args == (endpoint, model)
    selection = batch_calls.choose_provider.call_args
    assert selection.args == (endpoint, MODEL)
    assert selection.kwargs["input_tokens"] == 100 and selection.kwargs["output_tokens"] == 128
    for call, prompt in zip(batch_calls.complete.call_args_list, ["a", "b"]):
        assert call.args == (endpoint, model, prompt)
        assert call.kwargs["provider"] is effective
        assert {k: v for k, v in call.kwargs.items() if k != "provider"} == {
            k: v for k, v in batch_calls.check_completion.call_args.kwargs.items() if k != "provider"}
