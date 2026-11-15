"""Offline regression checks for the live V2 harness; no credentials or network required."""
import json
from types import SimpleNamespace

import pytest

import test_live as live
from modelspell import Endpoint, ModelInfo, ProviderSelection, supported_settings


@pytest.mark.parametrize("kind,reasoning", [
    ("openrouter", None), ("lmstudio", "off"), ("lmstudio", "low"), ("openai_compat", None),
])
@pytest.mark.parametrize("via", ["argument", "extra"])
def test_v2_metadata_does_not_consume_request_arguments(monkeypatch, capsys, kind, reasoning, via):
    calls = []
    endpoint = Endpoint(kind=kind, base_url="http://localhost:1234/v1")
    provider_slug = "example/endpoint" if kind == "openrouter" else None
    route = "chat_completions" if kind != "lmstudio" else (
        "lmstudio_native" if reasoning == "off" else "responses")
    meta = dict(backend=kind, route=route, version="test-build")
    if provider_slug:
        meta["provider"] = provider_slug
        meta["policy"] = {"order": [provider_slug], "allow_fallbacks": False, "max_price": {"prompt": 1}}

    def complete(endpoint_arg, model, prompt, **kw):
        assert endpoint_arg is endpoint and model == "example/model" and prompt == live.V2_PROMPT
        calls.append(kw)
        kw["on_text"]("ok")
        return SimpleNamespace(provider="Reported provider")

    monkeypatch.setattr(live, "complete", complete)
    if via == "argument":
        names = sorted(supported_settings(kind, reasoning) - {"reasoning", "provider"})
        records = live._v2_supported(endpoint, "example/model", kind, reasoning, **meta)
        values = [live.V2_VALUES[name] for name in names]
    else:
        cases = [("temperature", 2), ("temperature", 2.01)]
        records = live._v2_extra(endpoint, "example/model", cases, reasoning=reasoning,
                                 **meta)
        names, values = zip(*cases)

    assert len(calls) == len(records) == len(names)
    for kw, rec, name, value in zip(calls, records, names, values):
        assert kw["max_tokens"] == live.V2_VALUES["max_tokens"]
        assert kw["timeout"] == 300 and callable(kw["on_text"])
        assert kw.get("reasoning") == reasoning
        assert not {"meta", "backend", "route", "version"} & kw.keys()
        if provider_slug:
            assert kw["provider"] is meta["policy"]
        else:
            assert "provider" not in kw
        if via == "argument":
            assert kw[name] == value and "extra" not in kw
        else:
            assert kw["extra"] == {name: value}
        assert rec == dict(backend=kind, version="test-build", route=route, model="example/model",
                           provider=provider_slug, setting=name, value=value, via=via,
                           outcome="accepted", effect="effect_unverified", status=200,
                           served_by="Reported provider")

    lines = capsys.readouterr().out.splitlines()
    assert all(line.startswith("V2 ") for line in lines)
    assert [json.loads(line[3:]) for line in lines] == records


@pytest.mark.parametrize("options", [None, ("off", "on")])
def test_v2_model_info_identity_and_metadata(monkeypatch, capsys, options):
    info = ModelInfo(id="m", name=None, context_length=None, loaded=None, supports_tools=None,
                     supports_reasoning=None, reasoning_options=options, pricing=None, raw={})
    calls = []

    def complete(endpoint, model, prompt, **kw):
        assert model is info
        calls.append(model)
        return SimpleNamespace(provider=None)

    monkeypatch.setattr(live, "complete", complete)
    endpoint = Endpoint(kind="lmstudio")
    meta = dict(backend="lmstudio", route="lmstudio_native",
                model_reasoning_options=list(options) if options else None)
    records = live._v2_supported(endpoint, info, "lmstudio", "off", **meta)
    records += live._v2_extra(endpoint, info, [("temperature", 1)], reasoning="off", **meta)
    assert len(calls) == len(records) > 1
    assert all(rec["model"] == "m" and rec["model_reasoning_options"] is meta["model_reasoning_options"]
               for rec in records)
    assert [json.loads(line[3:]) for line in capsys.readouterr().out.splitlines()] == records

def _fake_selection(slug):
    return ProviderSelection(slug, {"order": [slug], "allow_fallbacks": False},
                             {"scope": "specific as published", "discovery": [
                                 {"slug": "deepinfra/turbo", "provider_name": "DeepInfra"},
                                 {"slug": "together", "provider_name": "Together"}]})


@pytest.mark.parametrize("stream", [False, True])
def test_vt_round_trip_against_the_fake(server, capsys, stream):
    from conftest import ep, json_reply, sse_reply
    tool = sse_reply("chat/or_tool_call_synthetic.sse") if stream else json_reply("chat/or_tool_call_synthetic.json")
    blue = b'{"provider":"DeepInfra","choices":[{"message":{"content":"blue"},"finish_reason":"stop"}]}'
    from conftest import Reply
    if stream:
        blue = [b'data: {"provider":"DeepInfra","choices":[{"delta":{"content":"blue"},"finish_reason":"stop"}]}\n\n'
                b'data: [DONE]\n\n']
    server.reply(tool, Reply(body=blue), Reply(body=blue))
    chosen = _fake_selection("deepinfra/turbo")
    turn, follow = live._vt_round_trip(ep(server, "openrouter"), "m", stream=stream, backend="fake",
                                       selection=chosen, provider=chosen.policy)
    assert len(server.requests) == 3 and turn.clean and follow.clean
    assert [t["function"]["name"] for t in server.requests[2]["body"]["tools"]] == ["read_label"]
    assert server.requests[2]["body"]["stream"] is stream
    records = [json.loads(line[3:]) for line in capsys.readouterr().out.splitlines() if line.startswith("VT ")]
    assert len([r for r in records if r.get("attribution") == "confirmed"]) == 3
    assert any(r.get("calls") == ["read_label", "read_label"] for r in records)


def test_vt_round_trip_provider_mismatch_fails(server):
    from conftest import ep, json_reply
    server.reply(json_reply("chat/or_tool_call_synthetic.json"), json_reply("chat/or_text_synthetic.json"))
    chosen = _fake_selection("together")
    with pytest.raises(AssertionError, match="Together|DeepInfra|together"):
        live._vt_round_trip(ep(server, "openrouter"), "m", stream=False, backend="fake",
                            selection=chosen, provider=chosen.policy)
