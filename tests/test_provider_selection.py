"""Offline OpenRouter selection using the same HTTP transport as real completions."""
import json
import threading
import time
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from conftest import KEY, Reply, ep
from modelspell import (CancelHandle, Cancelled, ConfigError, ProviderSelectionError, ToolDefinition,
                        check_completion, check_settings, check_tool_request, choose_provider, complete, complete_with_tools)
from modelspell._http import Call
from modelspell._json import thaw

MODEL = "example/model"
TOOL = ToolDefinition("route_check", "Inert provider acceptance check.", {"type": "object", "properties": {}})


def reply(value, status=200, **kwargs):
    return Reply(status=status, body=json.dumps(value).encode(), **kwargs)


def row(slug, tps=70, latency=0.1, price="0.0000001", **raw):
    return {"tag": slug, "provider_name": slug.split("/")[0], "throughput_last_30m": {"p50": tps},
            "latency_last_30m": {"p50": latency}, "pricing": {"prompt": price, "completion": price,
                                                            "request": "0"},
            "supported_parameters": ["tools", "reasoning", "temperature", "max_tokens"], **raw}


def endpoints(*rows):
    return reply({"data": {"endpoints": rows}})


def done(provider="test", finish="stop", message=None, **extra):
    return reply({"provider": provider, "choices": [{"finish_reason": finish,
                                                     "message": {"content": "ok"} if message is None else message}],
                  **extra})


@pytest.mark.parametrize("preset,floor,winner", [
    ("cheapest", None, "slow"), ("balanced", None, "middle"), ("balanced", 100, "quick"),
    ("fastest", None, "quick"),
])
def test_rankings_and_thresholds(server, preset, floor, winner):
    rows = [row("at-20", 20, 0, "0"), row("at-50", 50, 5, "0.000000003"),
            row("at-100", 100, 6, "0.000000004"), row("slow", 30, 5, "0.000000002"),
            row("middle", 75, 3, "0.000000003"), row("quick", 120, 0, "0.000000005"),
            row("no-speed", None, 0, "0"), row("no-pricing", 1000, None, None)]
    # Cheapest: exact boundary 20 is excluded; Balanced: 50 is excluded, and middle
    # beats the slower finishing equal-price candidates. Fastest chooses at-20
    # only when expected output is tiny, so with 200 tokens quick has the best time.
    rows[2]["pricing"] = {"prompt": "0.000000004", "completion": "0.000000004"}
    server.reply(endpoints(*rows), done())
    selected = choose_provider(ep(server, "openrouter"), MODEL, preset=preset, balanced_min_tps=floor)
    assert selected.slug == winner
    assert selected.evidence["attempts"][0]["sent"] is True
    assert [req["method"] for req in server.requests] == ["GET", "POST"]


def test_exact_balanced_floor_reports_best_and_never_probes(server):
    server.reply(endpoints(row("equal", 100), row("unknown", None)))
    with pytest.raises(ProviderSelectionError) as caught:
        choose_provider(ep(server, "openrouter"), MODEL, balanced_min_tps=100)
    assert caught.value.evidence["threshold"] == caught.value.evidence["best_observed_tps"] == 100
    assert [r["method"] for r in server.requests] == ["GET"]


def test_overlaps_duplicates_cost_and_missing_telemetry(server):
    base = row("alpha", 1000, 0, "0")
    descendant = row("alpha/tier", 10, service_tier="priority")
    duplicate = row("dup", 1000, 0, "0")
    with_fee = row("fee", pricing={"prompt": "0", "completion": "0", "request": "0.0001"})
    discounted = row("discount", price="0.00000015", discount=0.5)
    server.reply(endpoints(base, descendant, duplicate, {**duplicate, "tag": "Dup"}, with_fee,
                           discounted, row("regular"), row("missing", None, price="0")), done())
    selection = choose_provider(ep(server, "openrouter"), MODEL)
    assert selection.slug == "discount"
    assert selection.evidence["scope"] == "specific as published"
    excluded = {r["slug"]: r.get("excluded") for r in selection.evidence["discovery"]}
    assert excluded["alpha"] == excluded["dup"] == excluded["Dup"] == "ambiguous tag"
    assert excluded["alpha/tier"] == excluded["missing"] == "throughput floor"


@pytest.mark.parametrize("tools", [False, True])
def test_effective_policy_preflights_and_wire_bytes(server, tools):
    requested = {"only": ["alpha"], "ignore": ["alpha/ignored"], "quantizations": ["fp4"],
                 "data_collection": "deny", "zdr": True, "max_price": {"prompt": Decimal("0.5"), "request": 0}}
    good = row("alpha/fast", price="0.0000005", quantization="fp4")
    ignored = row("alpha/ignored", price="0", quantization="fp4")
    too_costly = row("alpha/price", price="0.0000005001", quantization="fp4")
    other = row("other", price="0", quantization="fp4")
    server.reply(endpoints(ignored, too_costly, other, row("alpha/unknown", price="0"), good),
                 reply({"error": {}}, 403), done(finish="length", message={"reasoning": "thinking"}))
    endpoint = ep(server, "openrouter")
    check_completion(endpoint, MODEL, provider=requested, reasoning="low", temperature=0.2)
    chosen = choose_provider(endpoint, MODEL, provider=requested, tools=tools,
                             settings={"reasoning": "low", "temperature": 0.2})
    assert requested["only"] == ["alpha"] and type(requested["max_price"]["prompt"]) is Decimal
    policy = {"only": ["alpha/fast"], "ignore": ["alpha/ignored"], "quantizations": ["fp4"],
              "data_collection": "deny", "zdr": True, "max_price": {"prompt": 0.5, "request": 0},
              "order": ["alpha/fast"], "allow_fallbacks": False}
    if tools:
        policy["require_parameters"] = True
    assert thaw(chosen.policy) == policy
    check_settings("openrouter", provider=chosen.policy)
    check_completion(endpoint, MODEL, provider=chosen.policy, reasoning="low", temperature=0.2)
    assert json.loads(json.dumps(chosen.policy, default=dict)) == policy
    check_tool_request("openrouter", provider=chosen.policy, tools=[TOOL])
    fn = complete_with_tools if tools else complete
    options = {"tools": [TOOL]} if tools else {}
    fn(endpoint, MODEL, "Reply OK.", provider=policy, max_tokens=64, reasoning="low", temperature=0.2, **options)
    fn(endpoint, MODEL, "Reply OK.", provider=chosen.policy, max_tokens=64, reasoning="low",
       temperature=0.2, **options)
    assert server.requests[2]["raw"] == server.requests[3]["raw"] == server.requests[4]["raw"]
    with pytest.raises(FrozenInstanceError):
        chosen.slug = "changed"
    with pytest.raises(TypeError):
        chosen.policy["max_price"]["prompt"] = 1


@pytest.mark.parametrize("provider", [{"sort": {"by": "price"}}, {"order": ["x"]}, {"allow_fallbacks": True},
                                     {"max_price": {"prompt": "Infinity"}}, {"zdr": 1},
                                     {"quantizations": "fp8"}, {"unsupported": True}])
def test_invalid_automatic_policy_before_discovery(server, provider):
    with pytest.raises(ConfigError):
        choose_provider(ep(server, "openrouter"), MODEL, provider=provider)
    assert not server.requests


def test_manual_verbatim_and_provider_scope(server):
    server.reply(endpoints(row("Test/Variant"), row("Test/Other")), done())
    endpoint = ep(server, "openrouter")
    selection = choose_provider(endpoint, MODEL, mode="manual", pin="Test/Variant")
    check_completion(endpoint, MODEL, provider=selection.policy)
    complete(endpoint, MODEL, "ping", provider=selection.policy)
    assert server.requests[1]["body"]["provider"]["order"] == ["Test/Variant"]
    server.reply(endpoints(row("Test/Variant"), row("Test/Other")))
    base = choose_provider(endpoint, MODEL, mode="manual", pin="Test")
    assert base.policy["order"] == ("Test",) and base.evidence["scope"] == "provider-level"
    with pytest.raises(ProviderSelectionError, match="Test/Variant"):
        choose_provider(endpoint, MODEL, mode="manual", pin="absent")
    assert len([r for r in server.requests if r["method"] == "POST"]) == 1
    with pytest.raises(ConfigError, match="Automatic or Manual"):
        choose_provider(endpoint, MODEL, mode="managed", tools=True)


@pytest.mark.parametrize("status", [401, 403, 404, 503])
def test_optional_zdr_failure_leaves_constraint_on_probe(server, status):
    server.reply(endpoints(row("test", model_id="example/dated-2026")), reply({"error": {}}, status), done())
    selected = choose_provider(ep(server, "openrouter"), MODEL, provider={"zdr": True})
    assert selected.evidence["catalogue"] == f"HTTP {status}"
    assert selected.evidence["pool"] == "fallback"
    assert server.requests[1]["path"].endswith("/endpoints/zdr")
    assert server.requests[2]["body"]["provider"]["zdr"] is True


@pytest.mark.parametrize("model", [MODEL, MODEL + ":free"])
@pytest.mark.parametrize("identity", ["example/dated-2026", None])
def test_exact_zdr_pair_and_model_metadata(server, model, identity):
    permaslug = "example/dated-2026"
    rows = endpoints(row("cheaper", price="0", model_id="wrong/dated"),
                     row("test", model_id=identity))
    catalogue = reply({"data": [{"tag": "test", "model_id": permaslug},
                               {"tag": "cheaper", "model_id": "wrong/model"}]})
    metadata = reply({"data": [{"id": model, "canonical_slug": permaslug},
                               {"id": MODEL + ":wrong", "canonical_slug": "wrong/model"}]})
    server.reply(rows, catalogue, *([metadata] if identity is None else []), done())
    selected = choose_provider(ep(server, "openrouter"), model, provider={"zdr": True})
    assert selected.slug == "test" and selected.evidence["pool"] == "catalogue"
    assert selected.evidence["catalogue_matches"][0]["permaslug"] == permaslug
    assert server.requests[-1]["body"]["model"] == model
    assert len(server.requests) == (4 if identity is None else 3)


def test_unresolved_identity_does_not_match_by_tag(server):
    server.reply(endpoints(row("cheap", price="0"), row("catalogue")),
                 reply({"data": [{"tag": "catalogue", "model_id": "example/dated-2026"}]}),
                 reply({"data": [{"id": MODEL + ":wrong", "canonical_slug": "example/dated-2026"}]}), done())
    choice = choose_provider(ep(server, "openrouter"), MODEL, provider={"zdr": True})
    assert choice.slug == "cheap" and choice.evidence["pool"] == "fallback"
    assert choice.evidence["unresolved_canonical_identity"] == ("cheap", "catalogue")


def test_inference_401_is_terminal_and_secret_free(server):
    server.reply(endpoints(row("a"), row("b")), reply({"error": {"message": KEY}}, 401), done())
    with pytest.raises(ProviderSelectionError) as caught:
        choose_provider(ep(server, "openrouter", api_key=KEY), MODEL)
    assert len(server.requests) == 2
    assert caught.value.evidence["attempts"][0]["status"] == 401
    assert KEY not in repr(caught.value.evidence)
    assert KEY not in repr(Call("http://localhost", KEY, 1))


def test_ambiguous_404_and_response_format_incident_survive_later_success(server):
    server.reply(endpoints(row("a"), row("b"), row("c")),
                 reply({"error": {"message": KEY}}, 404), done(message={"content": {KEY: KEY}}), done())
    selected = choose_provider(ep(server, "openrouter", api_key=KEY), MODEL)
    assert selected.slug == "c"
    attempts = selected.evidence["attempts"]
    assert [a["outcome"] for a in attempts] == ["no route", "response rejected", "accepted"]
    assert attempts[1]["diagnostic"]["structure"]["choices[0].message.content"] == "dict"
    assert KEY not in repr(selected)


def test_retry_budget_and_provider_only_rotation(server, monkeypatch):
    from modelspell import _provider
    scheduled = []
    monkeypatch.setattr(_provider, "_wait", lambda handle, delay: scheduled.append(delay))
    server.reply(endpoints(row("a"), row("b")),
                 reply({"error": {}}, 429, headers={"Retry-After": "3"}),
                 reply({"error": {"metadata": {"provider_name": "a", "provider_code": 429}}}, 429),
                 done())
    selected = choose_provider(ep(server, "openrouter"), MODEL, max_attempts=3)
    attempts = selected.evidence["attempts"]
    assert selected.slug == "b"
    assert scheduled == [3]
    assert [a["slug"] for a in attempts] == ["a", "a", "b"]
    assert all(a["reserved"] and a["sent"] for a in attempts)


@pytest.mark.parametrize("during_probe", [False, True])
@pytest.mark.parametrize("deadline", [False, True])
def test_slow_socket_cancellation_and_deadline_are_distinct(server, during_probe, deadline):
    hold = Reply(hold_headers=True)
    server.reply(endpoints(row("a")), hold) if during_probe else server.reply(hold)
    caller = CancelHandle()
    errors = []

    def select():
        try:
            choose_provider(ep(server, "openrouter"), MODEL, cancel=caller, timeout=0.5 if deadline else 5)
        except (Cancelled, ProviderSelectionError) as exc:
            errors.append(exc)

    started = time.monotonic()
    thread = threading.Thread(target=select)
    thread.start()
    assert server.reached.wait(2)
    if not deadline:
        caller.cancel()
    thread.join(2)
    assert not thread.is_alive() and time.monotonic() - started < 2
    assert server.client_closed.wait(1)
    (error,) = errors
    if deadline:
        assert isinstance(error, ProviderSelectionError) and error.reason == "deadline expired"
        assert not caller.cancelled
    else:
        assert isinstance(error, Cancelled) and error.sent is True
