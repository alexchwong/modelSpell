"""Request construction: R1–R9 (spec §12.3; R1, R2, R4, R5 extended in spec v6). R10–R16: test_settings.py."""
import json
import pathlib

import pytest

from conftest import KEY, MSGS, canned, ep, route_of
from modelspell import (AuthError, ConfigError, Endpoint, Message, UnsupportedSetting, complete)

GOLDEN = {k: v for k, v in json.loads((pathlib.Path(__file__).parent / "golden/requests.json").read_text()).items()
          if not k.startswith("_")}
NOOP = lambda s: None  # noqa: E731


def _sub(value):
    return MSGS if value == "MSGS" else value


def run(server, kind, *, stream=False, input=MSGS, endpoint=None, **args):
    server.reply(canned(route_of(kind, args.get("reasoning")), stream))
    complete(endpoint or ep(server, kind), "m", input, on_text=NOOP if stream else None, **args)
    assert len(server.requests) == 1  # E4: one request per call
    return server.requests[-1]


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_r1_golden(server, name):
    g = GOLDEN[name]
    req = run(server, g["kind"], stream=g["stream"], input=g.get("input", MSGS), **g["args"])
    assert req["method"] == "POST" and req["path"] == g["path"]
    assert req["body"] == {k: _sub(v) for k, v in g["body"].items()}


def test_r1_g9_native_url_from_trailing_slash_base(server, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", server.url)  # the server acts as proxy and sees the absolute URL
    server.reply(canned("native", False))
    complete(Endpoint(kind="lmstudio", base_url="http://host:1234/v1/"), "m", MSGS, reasoning="off")
    assert server.requests[0]["path"] == "http://host:1234/api/v1/chat"


def test_r1_default_urls():
    assert Endpoint(kind="lmstudio").base_url == "http://localhost:1234/v1"
    assert Endpoint(kind="openrouter").base_url == "https://openrouter.ai/api/v1"


@pytest.mark.parametrize("kind,reasoning", [("lmstudio", None), ("lmstudio", "off"), ("openrouter", None),
                                            ("openai_compat", None)])
def test_r2_none_args_omitted(server, kind, reasoning):
    body = run(server, kind, reasoning=reasoning)["body"]
    for k in ("temperature", "max_tokens", "max_output_tokens", "provider"):
        assert k not in body
    if reasoning is None:
        assert "reasoning" not in body
    for k in ("top_p", "top_k", "min_p", "repetition_penalty", "repeat_penalty", "presence_penalty",
              "frequency_penalty", "seed", "stop"):
        assert k not in body


ZEROS = dict(temperature=0, top_k=0, min_p=0, presence_penalty=0, seed=0)


@pytest.mark.parametrize("kind,reasoning,names", [
    ("openrouter", None, list(ZEROS)),
    ("openai_compat", None, list(ZEROS)),
    ("lmstudio", "off", ["temperature", "top_k", "min_p"]),
    ("lmstudio", None, ["temperature"]),
])
def test_r2_zero_is_a_value(server, kind, reasoning, names):
    body = run(server, kind, reasoning=reasoning, **{n: ZEROS[n] for n in names})["body"]
    for n in names:
        assert body[n] == 0 and body[n] is not False, n


@pytest.mark.parametrize("kind", ["lmstudio", "openrouter", "openai_compat"])
@pytest.mark.parametrize("reasoning", [None, "off", "low", "medium", "high"])
def test_r3_reasoning_mapping(server, kind, reasoning):
    if kind == "openai_compat" and reasoning is not None:
        with pytest.raises(UnsupportedSetting):
            complete(ep(server, kind), "m", "x", reasoning=reasoning)
        assert server.requests == []
        return
    req = run(server, kind, reasoning=reasoning)
    body = req["body"]
    if kind == "openrouter":
        expected = None if reasoning is None else {"effort": "none" if reasoning == "off" else reasoning}
        assert body.get("reasoning") == expected and req["path"] == "/v1/chat/completions"
    elif kind == "lmstudio" and reasoning == "off":
        assert body["reasoning"] == "off" and req["path"] == "/api/v1/chat"
    elif kind == "lmstudio":
        assert body.get("reasoning") == (None if reasoning is None else {"effort": reasoning})
        assert req["path"] == "/v1/responses"
    else:
        assert "reasoning" not in body


def test_r4_extra_overrides_last_and_never_changes_route(server):
    body = run(server, "lmstudio", max_tokens=10, extra={"max_output_tokens": 20, "reasoning": {"effort": "low"}})["body"]
    assert body["max_output_tokens"] == 20 and body["reasoning"] == {"effort": "low"}
    assert server.requests[0]["path"] == "/v1/responses"
    server.requests.clear()
    body = run(server, "lmstudio", reasoning="off", extra={"store": True})["body"]
    assert body["store"] is True
    for bad in ({"model": "x"}, {"stream": True}):
        with pytest.raises(ConfigError):
            complete(ep(server, "openai_compat"), "m", "x", extra=bad)


def test_r4_extra_overrides_new_settings_and_may_add_unsupported_fields(server):
    assert run(server, "openrouter", top_k=40, extra={"top_k": 5})["body"]["top_k"] == 5
    server.requests.clear()
    body = run(server, "openai_compat", extra={"repeat_penalty": 1.1})["body"]
    assert body["repeat_penalty"] == 1.1
    server.requests.clear()
    # extra may override a value the setting checks approved (backward compatible; §6.4)
    assert run(server, "openrouter", temperature=1.0, extra={"temperature": 3})["body"]["temperature"] == 3


BAD_CALLS = [
    ("openai_compat", dict(model="")), ("openai_compat", dict(reasoning="max")),
    ("openai_compat", dict(max_tokens=0)), ("openai_compat", dict(max_tokens=True)), ("openai_compat", dict(max_tokens=1.5)),
    ("openai_compat", dict(temperature=-1)), ("openai_compat", dict(temperature=True)),
    ("openai_compat", dict(provider={"order": ["x"]})), ("lmstudio", dict(provider={"order": ["x"]})),
    ("openrouter", dict(provider=["x"])), ("openai_compat", dict(extra=[("a", 1)])),
    ("openai_compat", dict(timeout=0)), ("openai_compat", dict(timeout=True)),
    ("openai_compat", dict(on_text="nope")), ("openai_compat", dict(on_reasoning=1)),
    ("openai_compat", dict(input=[])), ("openrouter", dict(endpoint_key=None)),
    # spec v6 additions (R10–R13 cover these exhaustively in test_settings.py)
    ("openai_compat", dict(temperature=float("inf"))), ("openai_compat", dict(temperature=float("nan"))),
    ("openrouter", dict(temperature=2.5)), ("lmstudio", dict(reasoning="off", temperature=1.5)),
    ("lmstudio", dict(top_k=40)), ("openai_compat", dict(repetition_penalty=1.1)),
    ("openrouter", dict(stop="END")), ("openrouter", dict(top_p=1.5)),
]


@pytest.mark.parametrize("kind,kw", BAD_CALLS)
def test_r5_invalid_arguments_make_no_request(server, kind, kw):
    kw = dict(kw)
    model, input = kw.pop("model", "m"), kw.pop("input", "x")
    endpoint = ep(server, kind, api_key=None) if kw.pop("endpoint_key", 1) is None else ep(server, kind)
    with pytest.raises((ConfigError, UnsupportedSetting, AuthError)):
        complete(endpoint, model, input, **kw)
    assert server.requests == []


@pytest.mark.parametrize("msgs,system,user", [
    ([{"role": "user", "content": "u"}], None, "u"),
    (MSGS, "sys", "task"),
    ([{"role": "system", "content": "a"}, {"role": "system", "content": ""}, {"role": "system", "content": "b"},
      {"role": "user", "content": "u"}], "a\n\nb", "u"),
    ([{"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"}], None, "[USER]\nu1\n\n[ASSISTANT]\na1"),
])
def test_r6_native_flattening(server, msgs, system, user):
    body = run(server, "lmstudio", reasoning="off", input=msgs)["body"]
    assert body["input"] == user and body.get("system_prompt") == system


def test_r7_headers(server, monkeypatch):
    h = run(server, "lmstudio")["headers"]
    assert "authorization" not in h and h["content-type"] == "application/json"
    server.requests.clear()
    h = run(server, "openai_compat", endpoint=ep(server, "openai_compat", api_key="k", headers={"X-Title": "t"}))["headers"]
    assert h["authorization"] == "Bearer k" and h["x-title"] == "t"


def test_r8_input_normalisation(server):
    mixed = [Message("system", "sys"), {"role": "user", "content": "task"}]
    assert run(server, "openai_compat", input=mixed)["body"]["messages"] == MSGS
    server.requests.clear()
    assert run(server, "openai_compat", input="hi")["body"]["messages"] == [{"role": "user", "content": "hi"}]
    for bad in ([{"role": "tool", "content": "x"}], [{"role": "user", "content": "x", "name": "n"}],
                [{"role": "user", "content": 1}], [], ["just a string"]):
        with pytest.raises(ConfigError):
            complete(ep(server, "openai_compat"), "m", bad)
    with pytest.raises(ConfigError):
        Message("bot", "x")


@pytest.mark.parametrize("stream", [False, True])
def test_r9_store_and_previous_response_id(server, stream):
    assert run(server, "lmstudio", reasoning="off", stream=stream)["body"]["store"] is False
    server.requests.clear()
    assert "store" not in run(server, "lmstudio", stream=stream)["body"]
    server.requests.clear()
    assert run(server, "lmstudio", stream=stream, extra={"store": False})["body"]["store"] is False
    for kind, r in (("lmstudio", None), ("lmstudio", "off"), ("openrouter", None)):
        server.requests.clear()
        assert "previous_response_id" not in run(server, kind, reasoning=r, stream=stream)["body"]


def test_key_not_in_endpoint_repr():
    e = Endpoint(kind="openrouter", api_key=KEY)
    assert "***" in repr(e) and KEY not in repr(e)
    assert "api_key=None" in repr(Endpoint(kind="lmstudio"))
