"""Opt-in live checks (spec §12.9, §17). Run: pytest -m live -rs

LM Studio:    MODELSPELL_LIVE_LMSTUDIO_URL, MODELSPELL_LIVE_LMSTUDIO_MODEL
OpenRouter:   OPENROUTER_API_KEY, MODELSPELL_LIVE_OPENROUTER_MODEL, MODELSPELL_LIVE_OPENROUTER_PROVIDER (optional)
llama-server: MODELSPELL_LIVE_LLAMASERVER_URL (including /v1), MODELSPELL_LIVE_LLAMASERVER_MODEL  (V2, owner checks)
Tool round trips (VT): the same variables; run with -k vt -s.
Owner checks: MODELSPELL_LIVE_PRESET (Quick, Cancellation, Usage, Full; default Full). Run with -s to see the record.
Each call is checked on its own; live results are never compared with each other.
"""
import json
import os
import urllib.request

import pytest

from modelspell import (AuthError, BackendError, ConnectError, Endpoint, ModelSpellError, RequestTimeout,
                        choose_provider, complete, list_models, list_providers, supported_settings)
from scripts.provider_live import attribution

pytestmark = pytest.mark.live
LMS_URL, LMS_MODEL = os.environ.get("MODELSPELL_LIVE_LMSTUDIO_URL"), os.environ.get("MODELSPELL_LIVE_LMSTUDIO_MODEL")
OR_KEY, OR_MODEL = os.environ.get("OPENROUTER_API_KEY"), os.environ.get("MODELSPELL_LIVE_OPENROUTER_MODEL")
OR_PROVIDER = os.environ.get("MODELSPELL_LIVE_OPENROUTER_PROVIDER")
need_lms = pytest.mark.skipif(not (LMS_URL and LMS_MODEL), reason="LM Studio live env vars not set")
need_or = pytest.mark.skipif(not (OR_KEY and OR_MODEL), reason="OpenRouter live env vars not set")
LLS_URL = os.environ.get("MODELSPELL_LIVE_LLAMASERVER_URL")
LLS_MODEL = os.environ.get("MODELSPELL_LIVE_LLAMASERVER_MODEL")
need_lls = pytest.mark.skipif(not (LLS_URL and LLS_MODEL), reason="llama-server live env vars not set")
PROMPT = [{"role": "system", "content": "Reply with one short word."}, {"role": "user", "content": "Say ok."}]


def _streamed(endpoint, model, **kw):
    texts, reasoning = [], []
    r = complete(endpoint, model, PROMPT, on_text=texts.append, on_reasoning=reasoning.append, **kw)
    assert "".join(texts) == r.text and "".join(reasoning) == (r.reasoning or "")
    return r


@pytest.fixture(scope="module")
def lms_info():
    models = list_models(Endpoint(kind="lmstudio", base_url=LMS_URL))
    info = next((m for m in models if m.id == LMS_MODEL), None)
    assert info is not None, f"{LMS_MODEL!r} not returned by list_models; ids: {[m.id for m in models]}"
    return info


@need_lms
@pytest.mark.parametrize("reasoning", [None, "high", "off"])
@pytest.mark.parametrize("stream", [False, True])
def test_live_lmstudio(reasoning, stream, lms_info):
    e = Endpoint(kind="lmstudio", base_url=LMS_URL)
    kw = dict(reasoning=reasoning, max_tokens=1024, timeout=600)
    model = lms_info if reasoning == "off" else LMS_MODEL
    r = _streamed(e, model, **kw) if stream else complete(e, model, PROMPT, **kw)
    assert r.transport == ("lmstudio_native" if reasoning == "off" else "responses")
    assert r.text or r.truncated
    if reasoning == "off":
        assert r.response_id is None  # store:false honoured on the native route


@need_lms
def test_live_lmstudio_models():
    assert any(m.id == LMS_MODEL for m in list_models(Endpoint(kind="lmstudio", base_url=LMS_URL)))


@need_or
def test_live_openrouter_stream_with_cost():
    r = _streamed(Endpoint(kind="openrouter"), OR_MODEL, reasoning="low", max_tokens=1024)
    assert r.cost is not None and r.transport == "chat_completions"


@need_or
def test_live_openrouter_discovery():
    e = Endpoint(kind="openrouter")
    assert any(m.id == OR_MODEL for m in list_models(e))
    assert list_providers(e, OR_MODEL)


@pytest.fixture(scope="module")
def or_ordinary():
    endpoint = Endpoint("openrouter")
    options = {"mode": "manual", "pin": OR_PROVIDER} if OR_PROVIDER is not None else {}
    return choose_provider(endpoint, OR_MODEL, settings={"max_tokens": 64}, **options)


@pytest.fixture(scope="module")
def or_tools():
    endpoint = Endpoint("openrouter")
    options = {"mode": "manual", "pin": OR_PROVIDER} if OR_PROVIDER is not None else {}
    return choose_provider(endpoint, OR_MODEL, tools=True, output_tokens=2048, **options)


@need_or
def test_live_v1_discovery_to_routing_round_trip(or_ordinary):
    e = Endpoint("openrouter")
    choice = choose_provider(e, OR_MODEL, mode="manual", pin=or_ordinary.slug, provider=or_ordinary.policy)
    r = complete(e, OR_MODEL, PROMPT, max_tokens=64, provider=choice.policy)
    state, reason = attribution(choice.slug, choice.evidence["discovery"], r.provider, choice.evidence["scope"])
    print("V1", choice.slug, r.provider, state, reason)
    assert state != "conflicting"
    if state == "unverified":
        pytest.skip(reason)


# --- V2 (spec v6 §17): backend compatibility of the generation-settings tables ---------------------
#
# Records only what can be observed. Outcomes: "accepted" (request succeeded; whether the setting
# was applied is NOT established, so every accepted setting is effect_unverified), "refused" (the
# backend returned an HTTP 4xx naming nothing else; to be classified by hand as a route, model or
# provider limitation), or "unrelated" (auth, availability, rate limit, network: re-run, changes
# nothing). Every record is printed as one JSON line prefixed "V2 "; copy them into
# tests/fixtures/README.md. Refusals of a *supported* setting fail the test so they are not missed.

V2_VALUES = dict(temperature=0.7, max_tokens=64, top_p=0.9, top_k=20, min_p=0.05, repetition_penalty=1.1,
                 presence_penalty=0.5, frequency_penalty=0.5, seed=42, stop=["\n\n\n\n"])
V2_PROMPT = [{"role": "user", "content": "Say ok."}]
CHAT_WIRE = {n: n for n in V2_VALUES}  # wire names used when probing a route through extra


def _v2_attempt(endpoint, model, *, meta, setting, value, via="argument", **kw):
    rec = dict(backend=meta["backend"], version=meta.get("version"), route=meta["route"], model=getattr(model, "id", model),
               provider=meta.get("provider"), setting=setting,
               value=value, via=via)
    if "pin_source" in meta:
        rec["pin_source"] = meta["pin_source"]
    if "model_reasoning_options" in meta:
        rec["model_reasoning_options"] = meta["model_reasoning_options"]
    try:
        r = complete(endpoint, model, V2_PROMPT, on_text=lambda s: None, timeout=300, **kw)
        rec.update(outcome="accepted", effect="effect_unverified", status=200, served_by=r.provider)
    except (AuthError, ConnectError, RequestTimeout) as exc:
        rec.update(outcome="unrelated", error=type(exc).__name__)
    except BackendError as exc:
        refused = exc.status is not None and 400 <= exc.status < 500 and exc.status not in (401, 403, 404, 408, 429)
        rec.update(outcome="refused" if refused else "unrelated", status=exc.status, error=type(exc).__name__,
                   detail=str(exc)[:300])
    except ModelSpellError as exc:
        rec.update(outcome="unrelated", error=f"{type(exc).__name__}: {exc}"[:300])
    print("V2 " + json.dumps(rec))
    return rec


def _v2_supported(endpoint, model, kind, reasoning, **meta):
    """V2 check 1: each supported setting once, at a non-default valid value, streaming."""
    base = dict(reasoning=reasoning) if reasoning is not None else {}
    if kind == "openrouter" and meta.get("policy"):
        base["provider"] = meta["policy"]
    records = []
    for name in sorted(supported_settings(kind, reasoning) - {"reasoning", "provider"}):
        kw = dict(base, max_tokens=V2_VALUES["max_tokens"])
        kw[name] = V2_VALUES[name]
        records.append(_v2_attempt(endpoint, model, setting=name, value=V2_VALUES[name], meta=meta, **kw))
    return records


def _v2_extra(endpoint, model, cases, reasoning=None, **meta):
    """V2 checks 2–4: values sent through extra, which bypasses modelSpell's checks."""
    base = dict(max_tokens=V2_VALUES["max_tokens"])
    if reasoning is not None:
        base["reasoning"] = reasoning
    if meta.get("policy"):
        base["provider"] = meta["policy"]
    return [_v2_attempt(endpoint, model, setting=wire, value=value, via="extra", extra={wire: value}, meta=meta, **base)
            for wire, value in cases]


def _v2_require_acceptance(records):
    if any(r["outcome"] == "accepted" for r in records):
        return
    failures = list(dict.fromkeys((r.get("status"), r.get("error")) for r in records))
    detail = next((r["detail"] for r in records if r.get("detail")), "")[:200]
    raise AssertionError("No request reached a model; this run verifies nothing. "
                         f"Distinct (status, error) pairs: {failures!r}; detail: {detail}")


def _v2_fail_on_refusals(records):
    refused = [r for r in records if r["outcome"] == "refused"]
    assert not refused, "supported setting refused; classify per spec v6 §17 before release: " + json.dumps(refused)


@need_or
def test_live_v2_openrouter(or_ordinary):
    e = Endpoint("openrouter")
    if OR_PROVIDER is not None:
        complete(e, OR_MODEL, V2_PROMPT, provider=or_ordinary.policy, max_tokens=64)
    meta = dict(backend="openrouter", route="chat_completions", provider=or_ordinary.slug,
                pin_source=or_ordinary.evidence["pin_source"], policy=or_ordinary.policy)
    records = _v2_supported(e, OR_MODEL, "openrouter", None, **meta)
    ranges = [("temperature", 2), ("temperature", 2.01), ("repetition_penalty", 2), ("repetition_penalty", 2.01)]
    for name in ("presence_penalty", "frequency_penalty"):
        ranges += [(name, -2), (name, -2.01), (name, 2), (name, 2.01)]
    _v2_extra(e, OR_MODEL, ranges, **meta)
    _v2_require_acceptance(records)
    _v2_fail_on_refusals(records)


@need_lms
def test_live_v2_lmstudio_native(lms_info):
    e = Endpoint(kind="lmstudio", base_url=LMS_URL)
    meta = dict(backend="lmstudio", route="lmstudio_native",
                model_reasoning_options=list(lms_info.reasoning_options) if lms_info.reasoning_options else None)
    records = _v2_supported(e, lms_info, "lmstudio", "off", **meta)
    _v2_extra(e, lms_info, [("temperature", 1), ("temperature", 1.01)], reasoning="off", **meta)
    _v2_require_acceptance(records)
    _v2_fail_on_refusals(records)


@need_lms
def test_live_v2_lmstudio_responses():
    e = Endpoint(kind="lmstudio", base_url=LMS_URL)
    meta = dict(backend="lmstudio", route="responses")
    records = _v2_supported(e, LMS_MODEL, "lmstudio", "low", **meta)
    unsupported = sorted(set(V2_VALUES) - supported_settings("lmstudio", "low"))
    _v2_extra(e, LMS_MODEL, [(CHAT_WIRE[n], V2_VALUES[n]) for n in unsupported], reasoning="low", **meta)
    _v2_require_acceptance(records)
    _v2_fail_on_refusals(records)


def _llama_version() -> str | None:
    root = LLS_URL[:-3] if LLS_URL.rstrip("/").endswith("/v1") else LLS_URL
    try:
        with urllib.request.urlopen(root.rstrip("/") + "/props", timeout=10) as r:  # noqa: S310 (test only)
            props = json.loads(r.read())
        return str(props.get("build_info") or props.get("version") or "") or None
    except Exception:
        return None


@need_lls
def test_live_v2_llamaserver_openai_compat():
    """Checking one openai_compat server never verifies the others (spec v6 §17)."""
    e = Endpoint(kind="openai_compat", base_url=LLS_URL)
    meta = dict(backend="llama-server", route="chat_completions", version=_llama_version())
    records = _v2_supported(e, LLS_MODEL, "openai_compat", None, **meta)
    # checks 3 and 4: the one unsupported setting, under both server names
    _v2_extra(e, LLS_MODEL, [("repetition_penalty", 1.1), ("repeat_penalty", 1.1)], **meta)
    _v2_require_acceptance(records)
    _v2_fail_on_refusals(records)


# --- owner checks (techspec 0.3.0 §9.7): the developer's route to the UI's battery ----------------
#
# Each test prints the full record, then acts on the run verdict: fail -> the test fails;
# incomplete (inconclusive, blocked or stopped rows) -> skipped with the reason; pass -> passes.
# Battery.run() is bounded, so a stuck call becomes a fail row, never a hung test.

PRESET = os.environ.get("MODELSPELL_LIVE_PRESET", "Full")


def _owner_checks(battery):
    from ui.owner_checks import pytest_outcome
    record = battery.run()
    print("\n" + record.markdown() + record.jsonl())
    action, reason = pytest_outcome(record)
    if action == "fail":
        pytest.fail(reason)
    if action == "skip":
        pytest.skip(reason)


@need_or
def test_live_owner_checks_openrouter():
    from ui.owner_checks import Battery
    _owner_checks(Battery(Endpoint(kind="openrouter"), OR_MODEL, PRESET, provider_pin=OR_PROVIDER))


@need_lms
@pytest.mark.parametrize("route", ["default", "native"])
def test_live_owner_checks_lmstudio(route, lms_info):
    from ui.owner_checks import Battery
    model = lms_info if route == "native" else LMS_MODEL  # native: a ModelInfo, so no discovery request
    _owner_checks(Battery(Endpoint(kind="lmstudio", base_url=LMS_URL), model, PRESET, route=route))


@need_lls
def test_live_owner_checks_llamaserver():
    from ui.owner_checks import Battery
    _owner_checks(Battery(Endpoint(kind="openai_compat", base_url=LLS_URL), LLS_MODEL, PRESET))


# --- VT: tool-call round trips (spec v9 §17, §21.6) ---------------------------------------------------
#
# Each run prints one `VT {...}` JSON line per turn. Request acceptance and the observed model outcome
# are recorded separately: a model that answers without calling verifies nothing (the test skips).
# Failure injection (malformed arguments, unknown names) is covered offline by tests/test_tools.py;
# a parser-built turn cannot be forged live.

def _vt_tools():
    from modelspell import ToolDefinition
    obj = {"type": "object", "additionalProperties": False}
    return [ToolDefinition("read_label", "Read the label of a fixture item. Always call this to learn a label.",
                           {**obj, "properties": {"key": {"type": "string"}}, "required": ["key"]}),
            ToolDefinition("write_note", "Write a note. Never needed for labels.",
                           {**obj, "properties": {"text": {"type": "string"}}, "required": ["text"]})]


VT_PROMPT = [{"role": "system", "content": "Use the read_label tool to answer. Answer in one word."},
             {"role": "user", "content": "What is the label of item 007?"}]


def _vt_record(**fields):
    print("VT " + json.dumps(fields, sort_keys=True))


def _vt_round_trip(endpoint, model, *, stream, backend, selection=None, **kw):
    from modelspell import Message, ToolOutput, complete_with_tools
    tools = _vt_tools()
    stream_kw = {"on_text": lambda s: None} if stream else {}
    meta = dict(backend=backend, model=model, stream=stream, reasoning=kw.get("reasoning"),
                requested_provider=selection.slug if selection else None)
    turn = complete_with_tools(endpoint, model, VT_PROMPT, tools=tools, max_tokens=2048, timeout=600,
                               **stream_kw, **kw)
    def record_attribution(number, result):
        if selection is None:
            return
        state, reason = attribution(selection.slug, selection.evidence["discovery"], result.provider,
                                    selection.evidence["scope"])
        _vt_record(**meta, turn=number, attribution=state, reason=reason, reported=result.provider)
        assert state != "conflicting", (selection.slug, result.provider)

    record_attribution(1, turn.result)
    _vt_record(**meta, turn=1, accepted=True, clean=turn.clean, finish=turn.result.finish_reason,
               calls=[c.name for c in turn.tool_calls], served_by=turn.result.provider)
    if not turn.tool_calls:
        pytest.skip(f"{backend}: the model answered without a call (clean={turn.clean}); round trip not observed")
    outputs = [ToolOutput(c.id, '{"label": "blue"}' if c.name == "read_label" else '{"error": "not allowed"}')
               for c in turn.tool_calls]
    history = [*VT_PROMPT, turn, *outputs]
    follow = complete_with_tools(endpoint, model, history, tools=tools, max_tokens=2048, timeout=600,
                                 **stream_kw, **kw)
    record_attribution(2, follow.result)
    _vt_record(**meta, turn=2, accepted=True, clean=follow.clean, finish=follow.result.finish_reason,
               calls=[c.name for c in follow.tool_calls], text=follow.result.text[:80], served_by=follow.result.provider)
    # narrower authorised set and a final-answer request with retained definitions (acceptance only)
    narrowed = complete_with_tools(endpoint, model, [*history, Message("user", "Answer now in one word.")],
                                   tools=tools[:1], max_tokens=2048, timeout=600, **stream_kw, **kw)
    record_attribution(3, narrowed.result)
    _vt_record(**meta, turn="narrowed_final", accepted=True, clean=narrowed.clean,
               finish=narrowed.result.finish_reason, calls=[c.name for c in narrowed.tool_calls])
    if selection is not None and (not follow.clean or follow.tool_calls
                                  or follow.result.text.strip().casefold() != "blue"):
        pytest.skip("OpenRouter continuation did not use the fixture result")
    return turn, follow


@need_lms
@pytest.mark.parametrize("reasoning", [None, "high"])
@pytest.mark.parametrize("stream", [False, True])
def test_live_vt_lmstudio(stream, reasoning):
    _vt_round_trip(Endpoint(kind="lmstudio", base_url=LMS_URL), LMS_MODEL, stream=stream, backend="lmstudio",
                   reasoning=reasoning)


@need_or
@pytest.mark.parametrize("stream", [False, True])
def test_live_vt_openrouter(stream, or_tools):
    e = Endpoint(kind="openrouter")
    _vt_round_trip(e, OR_MODEL, stream=stream, backend="openrouter", selection=or_tools, provider=or_tools.policy)


@need_lls
@pytest.mark.parametrize("stream", [False, True])
def test_live_vt_llamaserver(stream):
    _vt_round_trip(Endpoint(kind="openai_compat", base_url=LLS_URL), LLS_MODEL, stream=stream,
                   backend=f"llama-server {_llama_version()}")
