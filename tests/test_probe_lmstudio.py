"""Offline checks for the LM Studio probe diagnostic (0.2.1 spec v6, Section 13.4).

Only deterministic properties are tested: which requests are sent, how, and how output is printed. The
stand-in returns canned text; nothing here asserts anything about what a model or server does.
"""
import json
from types import SimpleNamespace

import pytest

import probe_lmstudio as probe
from modelspell import BackendError, ModelInfo, supported_settings

MODELS = [ModelInfo(id="m", name=None, context_length=None, loaded=None, supports_tools=None,
                    supports_reasoning=None, reasoning_options=None, pricing=None,
                    raw={"key": "m", "format": "gguf", "loaded_instances": [{"id": "m"}]})]


class Recorder:
    """Records every complete() call and returns a canned answer."""

    def __init__(self, transport_override=None):
        self.calls = []
        self.transport_override = transport_override

    def __call__(self, endpoint, model, prompt, *, reasoning=None, extra=None, **named):
        self.calls.append(dict(model=model, prompt=prompt, reasoning=reasoning, extra=extra, **named))
        transport = self.transport_override or ("lmstudio_native" if reasoning == "off" else "responses")
        return SimpleNamespace(text=f"answer {len(self.calls)}", reasoning=None,
                               usage=SimpleNamespace(output_tokens=5, reasoning_tokens=0),
                               finish_reason=None if transport == "lmstudio_native" else "stop",
                               truncated=False, model=getattr(model, "id", model), transport=transport)


def run(monkeypatch, capsys, fake, *args):
    monkeypatch.setattr(probe, "complete", fake)
    monkeypatch.setattr(probe, "list_models", lambda endpoint: MODELS)
    code = probe.main(["--url", "http://127.0.0.1:9/v1", "--model", "m", "--session", "t", *args])
    return code, capsys.readouterr().out.splitlines()


def expected_requests(reasoning, runs=3):
    total = 0
    for p in probe.PROBES:
        for via, wire in probe.variants(p, reasoning):
            total += len(probe.plan(p, via, wire, runs))
    return total


@pytest.mark.parametrize("reasoning", ["off", "low", "default"])
def test_request_shapes(monkeypatch, capsys, reasoning):
    fake = Recorder()
    code, out = run(monkeypatch, capsys, fake, "--reasoning", reasoning)
    level = probe.REASONING[reasoning]
    supported = supported_settings("lmstudio", level)
    assert code == 0 and len(fake.calls) == expected_requests(level)
    for call in fake.calls:
        assert call["reasoning"] == level and call["max_tokens"] == 1500 and call["timeout"] == 600.0
        named = {k for k in call if k not in ("model", "prompt", "reasoning", "extra", "max_tokens", "timeout", "temperature")}
        assert named <= supported                                     # never a refused named argument
        assert call["extra"] is None or len(call["extra"]) == 1
    sent_extra = {next(iter(c["extra"])) for c in fake.calls if c["extra"]}
    if "repetition_penalty" in supported:
        assert not {"repetition_penalty", "repeat_penalty"} & sent_extra
    else:
        assert {"repetition_penalty", "repeat_penalty"} <= sent_extra  # both wire names tried
    assert "zz_not_a_field" in sent_extra


def test_arms_alternate_and_seed_alternate_is_last(monkeypatch, capsys):
    _, out = run(monkeypatch, capsys, Recorder(), "--probes", "seed", "--runs", "2")
    arms = [json.loads(l[2:])["arm"] for l in out if l.startswith("P ")]
    assert arms == ["baseline", "treatment", "baseline", "treatment", "alternate"]


def test_every_line_is_json_and_config_has_discovery(monkeypatch, capsys):
    _, out = run(monkeypatch, capsys, Recorder())
    for line in out:
        if line.startswith(("P ", "P-CONFIG ")):
            json.loads(line.split(" ", 1)[1])
    config = json.loads(next(l for l in out if l.startswith("P-CONFIG "))[9:])
    assert config["model"]["format"] == "gguf" and config["transport"] == "responses"
    summary = out[out.index(next(l for l in out if l.startswith("SUMMARY"))):]
    for p in probe.PROBES:
        assert any(l.strip().startswith(p.name) for l in summary), p.name


def test_errors_are_recorded_and_the_run_continues(monkeypatch, capsys):
    def refuse(endpoint, model, prompt, **kw):
        raise BackendError("bad field", status=400)
    code, out = run(monkeypatch, capsys, refuse, "--probes", "control,top_k")
    records = [json.loads(l[2:]) for l in out if l.startswith("P ")]
    assert code == 0 and len(records) == 7
    assert all(r["outcome"] == "error" and r["status"] == 400 for r in records)


def test_discovered_model_is_passed_once(monkeypatch, capsys):
    fake = Recorder()
    discoveries = []
    monkeypatch.setattr(probe, "complete", fake)

    def list_models(endpoint):
        discoveries.append(endpoint)
        return MODELS

    monkeypatch.setattr(probe, "list_models", list_models)
    assert probe.main(["--url", "http://127.0.0.1:9/v1", "--model", "m", "--reasoning", "off"]) == 0
    assert len(discoveries) == 1 and fake.calls
    assert all(call["model"] is MODELS[0] for call in fake.calls)
    config = json.loads(capsys.readouterr().out.splitlines()[0][9:])
    assert config["model_arg"] == "ModelInfo"


def test_unmatched_model_falls_back_to_id(monkeypatch, capsys):
    fake = Recorder()
    monkeypatch.setattr(probe, "complete", fake)
    monkeypatch.setattr(probe, "list_models", lambda endpoint: [SimpleNamespace(id="other", raw={})])
    assert probe.main(["--url", "http://127.0.0.1:9/v1", "--model", "m"]) == 0
    assert fake.calls and all(call["model"] == "m" for call in fake.calls)
    config = json.loads(capsys.readouterr().out.splitlines()[0][9:])
    assert config["model_arg"] == "id"


def test_failed_answers_are_not_counted_as_distinct(monkeypatch, capsys):
    def refuse(*args, **kw):
        raise BackendError("no reasoning configuration", status=400)

    code, out = run(monkeypatch, capsys, refuse)
    summary = "\n".join(out[out.index(next(line for line in out if line.startswith("SUMMARY"))):])
    assert code == 0 and "no usable answers" in summary and "distinct" not in summary


def test_describe_uses_only_ok_answers():
    records = [dict(arm="baseline", outcome="ok", text="answer"),
               dict(arm="baseline", outcome="error", error="BackendError", text=""),
               dict(arm="treatment", outcome="ok", text="answer")]
    summary = probe.describe(probe.BY_NAME["top_k"], records)
    assert "baseline 1 distinct of 1" in summary and "baseline 1 ok of 2" in summary
    records.append(dict(arm="alternate", outcome="error", error="BackendError", detail="failed"))
    assert probe.describe(probe.BY_NAME["seed"], records) == "no usable answers: BackendError"


def test_discovery_failure_falls_back_to_id(monkeypatch, capsys):
    fake = Recorder()
    monkeypatch.setattr(probe, "complete", fake)

    def list_models(endpoint):
        raise BackendError("discovery failed", status=500)

    monkeypatch.setattr(probe, "list_models", list_models)
    assert probe.main(["--url", "http://127.0.0.1:9/v1", "--model", "m", "--probes", "control"]) == 0
    assert fake.calls[0]["model"] == "m"
    config = json.loads(capsys.readouterr().out.splitlines()[0][9:])
    assert config["model_arg"] == "id" and config["model"]["status"] == 500


def test_routing_regression_stops_the_run(monkeypatch, capsys):
    code, out = run(monkeypatch, capsys, Recorder(transport_override="chat_completions"), "--probes", "top_p")
    assert code == 1 and any(l.startswith("ABORTED: routing regression") for l in out)


@pytest.mark.parametrize("args", [["--runs", "1"], ["--runs", "x"], ["--probes", "nope"], ["--max-tokens", "0"]])
def test_argument_errors(monkeypatch, args):
    monkeypatch.setattr(probe, "complete", Recorder())
    with pytest.raises(SystemExit) as exc:
        probe.main(["--url", "http://127.0.0.1:9/v1", "--model", "m", *args])
    assert exc.value.code == 2


@pytest.mark.parametrize("record", [None, [], {}, {"transport": []}, {"transport": {}}, {"text": 42},
                                    {"transport": "responses", "text": "x", "truncated": False, "finish_reason": "stop"}])
def test_outcome_never_raises(record):
    assert probe.outcome(record) in ("error", "truncated", "incomplete", "blank", "ok")
