"""Opt-in live checks (spec §12.9, §17). Run: pytest -m live -rs

LM Studio:    MODELSPELL_LIVE_LMSTUDIO_URL, MODELSPELL_LIVE_LMSTUDIO_MODEL
OpenRouter:   OPENROUTER_API_KEY, MODELSPELL_LIVE_OPENROUTER_MODEL, MODELSPELL_LIVE_OPENROUTER_PROVIDER (optional)
llama-server: MODELSPELL_LIVE_LLAMASERVER_URL (including /v1), MODELSPELL_LIVE_LLAMASERVER_MODEL  (V2 only)
Each call is checked on its own; live results are never compared with each other.
"""
import json
import os
import urllib.request

import pytest

from modelspell import (AuthError, BackendError, ConnectError, Endpoint, ModelSpellError, RequestTimeout,
                        complete, list_models, list_providers, supported_settings)

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


@need_lms
@pytest.mark.parametrize("reasoning", [None, "high", "off"])
@pytest.mark.parametrize("stream", [False, True])
def test_live_lmstudio(reasoning, stream):
    e = Endpoint(kind="lmstudio", base_url=LMS_URL)
    kw = dict(reasoning=reasoning, max_tokens=1024, timeout=600)
    r = _streamed(e, LMS_MODEL, **kw) if stream else complete(e, LMS_MODEL, PROMPT, **kw)
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


@need_or
def test_live_v1_discovery_to_routing_round_trip():
    """V1 (§17). Record the printed evidence in tests/fixtures/README.md."""
    e = Endpoint(kind="openrouter")
    providers = list_providers(e, OR_MODEL)
    print("V1 entries:", [(p.name, p.slug, "tag" in p.raw, "provider_name" in p.raw) for p in providers])
    by_name = {}
    for p in providers:
        by_name.setdefault(p.name, []).append(p.slug)
    multi = [n for n, tags in by_name.items() if len(tags) > 1]
    print("V1 providers with >=2 endpoints:", {n: by_name[n] for n in multi})
    choice = next((p for p in providers if p.slug and p.name in multi), None) or \
        next((p for p in providers if p.slug), None)
    if choice is None:
        pytest.skip("V1: no endpoint entry carries a tag")
    r = complete(e, OR_MODEL, PROMPT, max_tokens=256, provider={"order": [choice.slug], "allow_fallbacks": False})
    assert r.provider == choice.name, (r.provider, choice)


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
    rec = dict(backend=meta["backend"], version=meta.get("version"), route=meta["route"], model=model,
               provider=meta.get("provider"), setting=setting,
               value=value, via=via)
    if "pin_source" in meta:
        rec["pin_source"] = meta["pin_source"]
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
    if kind == "openrouter" and meta.get("provider"):
        base["provider"] = {"order": [meta["provider"]], "allow_fallbacks": False}
    records = []
    for name in sorted(supported_settings(kind, reasoning) - {"reasoning", "provider"}):
        kw = dict(base, max_tokens=V2_VALUES["max_tokens"])
        kw[name] = V2_VALUES[name]
        records.append(_v2_attempt(endpoint, model, setting=name, value=V2_VALUES[name], meta=meta, **kw))
    return records


def _v2_extra(endpoint, model, cases, reasoning=None, provider_pin=None, **meta):
    """V2 checks 2–4: values sent through extra, which bypasses modelSpell's checks."""
    base = dict(max_tokens=V2_VALUES["max_tokens"])
    if reasoning is not None:
        base["reasoning"] = reasoning
    if provider_pin:
        base["provider"] = {"order": [provider_pin], "allow_fallbacks": False}
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
def test_live_v2_openrouter():
    e = Endpoint(kind="openrouter")
    providers = list_providers(e, OR_MODEL)
    slugs = [p.slug for p in providers if p.slug]
    if OR_PROVIDER is not None:
        assert OR_PROVIDER in slugs, (
            f"MODELSPELL_LIVE_OPENROUTER_PROVIDER={OR_PROVIDER!r} does not match an available slug; "
            f"available slugs: {slugs!r}")
        pin, pin_source = OR_PROVIDER, "env"
    else:
        pin, pin_source = next(iter(slugs), None), "first_listed"
    meta = dict(backend="openrouter", route="chat_completions", provider=pin, pin_source=pin_source)
    records = _v2_supported(e, OR_MODEL, "openrouter", None, **meta)
    ranges = [("temperature", 2), ("temperature", 2.01), ("repetition_penalty", 2), ("repetition_penalty", 2.01)]
    for name in ("presence_penalty", "frequency_penalty"):
        ranges += [(name, -2), (name, -2.01), (name, 2), (name, 2.01)]
    _v2_extra(e, OR_MODEL, ranges, provider_pin=pin, **meta)
    _v2_require_acceptance(records)
    _v2_fail_on_refusals(records)


@need_lms
def test_live_v2_lmstudio_native():
    e = Endpoint(kind="lmstudio", base_url=LMS_URL)
    meta = dict(backend="lmstudio", route="lmstudio_native")
    records = _v2_supported(e, LMS_MODEL, "lmstudio", "off", **meta)
    _v2_extra(e, LMS_MODEL, [("temperature", 1), ("temperature", 1.01)], reasoning="off", **meta)
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
