"""Offline regression checks for the live V2 harness; no credentials or network required."""
import json
from types import SimpleNamespace

import pytest

import test_live as live
from modelspell import Endpoint, ModelInfo, supported_settings


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
                                 provider_pin=provider_slug, **meta)
        names, values = zip(*cases)

    assert len(calls) == len(records) == len(names)
    for kw, rec, name, value in zip(calls, records, names, values):
        assert kw["max_tokens"] == live.V2_VALUES["max_tokens"]
        assert kw["timeout"] == 300 and callable(kw["on_text"])
        assert kw.get("reasoning") == reasoning
        assert not {"meta", "backend", "route", "version"} & kw.keys()
        if provider_slug:
            assert kw["provider"] == {"order": [provider_slug], "allow_fallbacks": False}
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