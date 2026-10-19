"""Opt-in live checks (spec §12.9, §17). Run: pytest -m live -rs

LM Studio:  MODELSPELL_LIVE_LMSTUDIO_URL, MODELSPELL_LIVE_LMSTUDIO_MODEL
OpenRouter: OPENROUTER_API_KEY, MODELSPELL_LIVE_OPENROUTER_MODEL
Each call is checked on its own; live results are never compared with each other.
"""
import os

import pytest

from modelspell import Endpoint, complete, list_models, list_providers

pytestmark = pytest.mark.live
LMS_URL, LMS_MODEL = os.environ.get("MODELSPELL_LIVE_LMSTUDIO_URL"), os.environ.get("MODELSPELL_LIVE_LMSTUDIO_MODEL")
OR_KEY, OR_MODEL = os.environ.get("OPENROUTER_API_KEY"), os.environ.get("MODELSPELL_LIVE_OPENROUTER_MODEL")
need_lms = pytest.mark.skipif(not (LMS_URL and LMS_MODEL), reason="LM Studio live env vars not set")
need_or = pytest.mark.skipif(not (OR_KEY and OR_MODEL), reason="OpenRouter live env vars not set")
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
