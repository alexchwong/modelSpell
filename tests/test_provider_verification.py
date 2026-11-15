"""Offline verification decisions and build/incident provenance."""
import json
from types import SimpleNamespace

import pytest

from conftest import KEY, Reply, ep
from modelspell import ProviderSelection
from scripts import provider_live as live
from scripts import verify_provider_refactor as verify


def build():
    return {"commit": "a" * 40, "version": "0.4.0", "wheel_sha256": "b" * 64}


def report():
    return {"run_id": "offline-run", "build": build(), "complete_vt": False, "cases": [], "incidents": [],
            "provenance": {**dict.fromkeys(("payload_clean_start", "payload_clean_end", "harness_clean_start",
                                            "harness_clean_end", "committed_harness", "installed_wheel",
                                            "wheel_smoke", "wheel_verified_end"), True),
                           "head_end": "a" * 40, "builder_commit": "a" * 40},
            "overrides": {"model": None, "max_input_price": 0.50, "max_output_price": 1.50}}


def incident(rec, index):
    identity = "/".join(rec["build"][k] for k in verify.BUILD_KEYS)
    return {"id": f"{rec['run_id']}/{identity}/{index}", "case": live.VT,
            "run_id": rec["run_id"], "build": rec["build"].copy()}


def passed(rec):
    verify.add_case(rec, "Offline suite", "PASS")
    verify.add_case(rec, live.VT, "PASS")
    rec["complete_vt"] = True
    return rec


@pytest.mark.parametrize("field,value", [
    ("payload_clean_start", False), ("payload_clean_end", False), ("harness_clean_start", False),
    ("installed_wheel", False), ("wheel_verified_end", False), ("head_end", "c" * 40),
    ("builder_commit", "c" * 40),
])
def test_ineligible_provenance_leaves_gate_pending_and_failure_takes_precedence(field, value):
    rec = passed(report())
    rec["provenance"][field] = value
    assert verify.outcomes(rec) == 3 and rec["release_gate"] == "pending"
    verify.add_case(rec, "Other check", "FAIL", "observed correctness failure")
    assert verify.outcomes(rec) == 1 and rec["release_gate"] == "pending"


def test_other_cases_may_be_blocked_when_gate_passes():
    rec = passed(report())
    assert verify.outcomes(rec) == 0
    verify.add_case(rec, "Manual / V1", "BLOCKED", "attribution unavailable")
    assert verify.outcomes(rec) == 2 and rec["release_gate"] == "pass"


def test_cumulative_triage_preserves_the_original(tmp_path):
    rec = passed(report())
    rec["incidents"] = [incident(rec, 1), incident(rec, 2)]
    assert verify.outcomes(rec) == 3
    original = tmp_path / "report.json"
    verify.save(original, rec)
    before = original.read_bytes()
    assert verify.triage(original, rec["incidents"][0]["id"], "upstream", "Malformed choices") == 3
    assert verify.triage(original, rec["incidents"][1]["id"], "upstream", "Malformed delta") == 0
    assert original.read_bytes() == before
    assert len(list(tmp_path.glob("triage-*.json"))) == 2


@pytest.mark.parametrize("complete,eligible,verdict,code", [
    (False, True, "upstream", 3), (True, False, "upstream", 3),
    (True, True, "modelspell", 1),
])
def test_triage_cannot_manufacture_evidence(tmp_path, complete, eligible, verdict, code):
    rec = passed(report())
    rec["complete_vt"] = complete
    rec["provenance"]["payload_clean_start"] = eligible
    rec["incidents"] = [incident(rec, 1)]
    path = tmp_path / "report.json"
    verify.save(path, rec)
    assert verify.triage(path, rec["incidents"][0]["id"], verdict, "Test evidence") == code


@pytest.mark.parametrize("field", ["run_id", "commit", "wheel_sha256"])
def test_triage_rejects_mismatched_identity(tmp_path, field):
    rec = passed(report())
    bad = incident(rec, 1)
    rec["incidents"] = [bad]
    if field == "run_id":
        bad["run_id"] = "another"
    else:
        bad["build"][field] = "f" * len(bad["build"][field])
    path = tmp_path / "report.json"
    verify.save(path, rec)
    with pytest.raises(verify.VerificationError, match="does not belong"):
        verify.triage(path, bad["id"], "upstream", "Test evidence")


def test_report_rejects_literal_key_before_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    target = tmp_path / "report.json"
    with pytest.raises(verify.VerificationError, match="credential") as caught:
        verify.save(target, {"field": KEY})
    assert KEY not in str(caught.value) and not target.exists()
    assert "OPENROUTER_API_KEY" not in verify.clean_environment()


@pytest.mark.parametrize("tag,reported,expected", [
    ("google-vertex/global", " Google ", "confirmed"), ("mancer/fp8", "MANCER 2", "confirmed"),
    ("google-vertex/global", "Mancer 2", "conflicting"), ("mancer/fp8", "", "unverified"),
    ("mancer/fp8", "Unknown provider", "unverified"),
    ("ambiguous", "Mancer 2", "unverified"), ("mancer/fp8", "Duplicate", "unverified"),
])
def test_attribution_uses_published_names(tag, reported, expected):
    rows = [{"slug": "google-vertex/global", "provider_name": "Google"},
            {"slug": "mancer/fp8", "provider_name": "Mancer 2"},
            {"slug": "ambiguous/a", "provider_name": "Provider A"},
            {"slug": "ambiguous/b", "provider_name": "Provider B"},
            {"slug": "duplicate-a", "provider_name": "Duplicate"},
            {"slug": "duplicate-b", "provider_name": "Duplicate"}]
    scope = "provider-level" if tag == "ambiguous" else "specific as published"
    assert live.attribution(tag, rows, reported, scope)[0] == expected


@pytest.mark.parametrize("subcause", ["no tool call", "truncation", "another diagnostic finish",
                                     "continuation that did not use the tool result"])
def test_named_fallback_after_model_behaviour(monkeypatch, subcause):
    rec = report()
    runner = live.LiveRun(rec, rec["overrides"])
    attempted = []

    def selection(model, **kwargs):
        assert kwargs["reserve"] == 6
        attempted.append(model)
        runner.reserve(1)
        return ProviderSelection("p", {"zdr": True}, {"scope": "specific as published", "quantization": None})

    monkeypatch.setattr(runner, "selection", selection)
    monkeypatch.setattr(runner, "vt_turns", lambda model, selection, stream:
                        subcause if model == live.MODELS[0] and stream else None)
    runner.case_run(live.VT, runner.named)
    assert attempted == list(live.MODELS[:2])
    assert rec["complete_vt"] and verify.outcomes(rec) == 0
    assert rec["cases"][0]["observations"][0]["sequences"][1]["reason"] == subcause


def test_named_budget_and_correctness_are_terminal(monkeypatch):
    rec = report()
    runner = live.LiveRun(rec, rec["overrides"])
    calls = []

    def selection(model, **kw):
        calls.append(model)
        return ProviderSelection("p", {}, {"scope": "specific as published", "quantization": None})

    monkeypatch.setattr(runner, "selection", selection)
    monkeypatch.setattr(runner, "vt_turns", lambda *args: (_ for _ in ()).throw(live.Incorrect("route conflict")))
    runner.case_run(live.VT, runner.named)
    assert calls == [live.MODELS[0]]
    assert verify.outcomes(rec) == 1 and rec["release_gate"] == "fail"

    rec = report()
    runner = live.LiveRun(rec, rec["overrides"])
    monkeypatch.setattr(runner, "selection", selection)
    runner.budget = 6
    runner.case_run(live.VT, runner.named)
    assert calls == [live.MODELS[0]]
    assert verify.outcomes(rec) == 3 and rec["cases"][0]["reason"] == "budget exhausted"


def test_named_real_synthetic_http_vt_and_earlier_incident(server, monkeypatch):
    rec = report()
    runner = live.LiveRun(rec, rec["overrides"])
    runner.endpoint = ep(server, "openrouter")
    monkeypatch.setattr(live.secrets, "token_hex", lambda _: "verify-label")

    def response(doc):
        return Reply(body=json.dumps(doc).encode())

    def done(content="verify-label", *, stream=False, calls=None):
        field = "delta" if stream else "message"
        choice = {field: {"content": content}, "finish_reason": "tool_calls" if calls else "stop"}
        if calls:
            choice[field]["tool_calls"] = calls
        doc = {"provider": "Sample Provider", "choices": [choice]}
        if stream:
            return Reply(body=f"data: {json.dumps(doc)}\n\ndata: [DONE]\n\n".encode())
        return response(doc)

    def tool(stream):
        return done(None, stream=stream, calls=[{
            **({"index": 0} if stream else {}), "type": "function", "id": "fixture-call",
            "function": {"name": "read_label", "arguments": "{}"}}])

    discovery = response({"data": {"endpoints": [
        {"tag": tag, "provider_name": "Sample Provider" if tag == "sample" else "Earlier Provider",
         "throughput_last_30m": {"p50": 90}, "pricing": {"prompt": "0", "completion": "0"},
         "supported_parameters": ["tools", "max_tokens"], "model_id": "example/permaslug"}
        for tag in ("earlier", "sample")]}})
    server.reply(discovery, response({"data": []}), done({KEY: KEY}), done(), tool(False), done(), done(),
                 tool(True), done(stream=True), done(stream=True))
    runner.case_run(live.VT, runner.named)
    assert rec["complete_vt"] and verify.completed_configuration(rec)
    assert len(rec["incidents"]) == 1 and verify.outcomes(rec) == 3
    assert rec["cases"][0]["posts"] == rec["cases"][0]["reserved"] == 8
    assert KEY not in json.dumps(rec)
    assert len([r for r in server.requests if r["method"] == "POST"]) == 8
    for request in server.requests[3:]:
        assert request["body"]["provider"]["zdr"] is True
        assert request["body"]["provider"]["order"] == ["sample"]
    assert verify.outcomes(rec, {rec["incidents"][0]["id"]: "upstream"}) == 0
