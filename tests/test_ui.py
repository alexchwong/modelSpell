"""Developer UI server (ui/server.py): U1–U10 (spec v6 §18.3). The HTML/JS is not tested."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from conftest import KEY, Reply, canned, fx, sse_reply
import modelspell
from ui.server import UIServer


@pytest.fixture
def ui():
    srv = UIServer(0)
    threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def request(ui, path, body=None, token=True, raw=False):
    base = f"http://127.0.0.1:{ui.server_address[1]}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Session-Token"] = ui.token
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers=headers, method="GET" if body is None else "POST")
    resp = urllib.request.build_opener().open(req, timeout=10)
    return resp if raw else json.loads(resp.read())


def status_of(fn):
    try:
        fn()
    except urllib.error.HTTPError as e:
        return e.code
    return 200


def configure(ui, server, kind="openai_compat", **kw):
    return request(ui, "/api/config", {"kind": kind, "base_url": server.url + "/v1", "model": "m", **kw})


def events(resp):
    return [json.loads(line) for line in resp.read().splitlines() if line]


def test_u1_token_required(ui):
    base = f"http://127.0.0.1:{ui.server_address[1]}"
    assert status_of(lambda: urllib.request.urlopen(base + "/")) == 403
    assert status_of(lambda: urllib.request.urlopen(base + "/?t=wrong")) == 403
    assert status_of(lambda: request(ui, "/api/state", token=False)) == 403
    assert status_of(lambda: request(ui, "/api/config", {}, token=False)) == 403
    assert b"modelSpell dev UI" in urllib.request.urlopen(ui.url).read()


def test_u2_key_is_write_only_and_bound_to_its_endpoint(ui, make_server):
    a, b = make_server(), make_server()
    state = configure(ui, a, api_key=KEY)
    assert state["key_set"] is True and KEY not in json.dumps(state)
    assert KEY not in json.dumps(request(ui, "/api/state"))
    # switching endpoint must not carry the key to the new server
    b.reply(canned("chat", False))
    assert configure(ui, b)["key_set"] is False
    events(request(ui, "/api/complete", {"prompt": "x"}, raw=True))
    assert "authorization" not in b.requests[0]["headers"]
    # switching back finds the stored key again; an empty string clears it
    a.reply(canned("chat", False))
    configure(ui, a)
    events(request(ui, "/api/complete", {"prompt": "x"}, raw=True))
    assert a.requests[0]["headers"]["authorization"] == f"Bearer {KEY}"
    assert configure(ui, a, api_key="")["key_set"] is False


def test_u3_complete_streams_events_matching_result(ui, server):
    server.reply(sse_reply("chat/or_reasoning_synthetic.sse"))
    configure(ui, server, kind="openrouter", api_key="k", provider="deepinfra/turbo")
    evs = events(request(ui, "/api/complete", {"prompt": "task", "system": "sys",
                                               "settings": {"reasoning": "high", "max_tokens": 64}}, raw=True))
    result = evs[-1]["result"]
    assert evs[-1]["type"] == "result"
    assert "".join(e["text"] for e in evs if e["type"] == "text") == result["text"] == "ans"
    assert "".join(e["text"] for e in evs if e["type"] == "reasoning") == result["reasoning"]
    body = server.requests[0]["body"]
    assert body["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    assert body["provider"] == {"order": ["deepinfra/turbo"], "allow_fallbacks": False}
    assert body["reasoning"] == {"effort": "high"} and body["max_tokens"] == 64 and body["stream"] is True


def test_u4_backend_error_is_an_error_event(ui, server):
    server.reply(Reply(status=500, body=b"boom"))
    configure(ui, server)
    evs = events(request(ui, "/api/complete", {"prompt": "x"}, raw=True))
    assert evs == [{"type": "error", "error": evs[0]["error"]}] and "BackendError" in evs[0]["error"]


def test_u4_bad_config_is_400(ui):
    assert status_of(lambda: request(ui, "/api/config", {"kind": "nope"})) == 400
    assert status_of(lambda: request(ui, "/api/config", {"kind": "openai_compat", "base_url": ""})) == 400


def test_u3_discovery_passthrough(ui, server):
    server.reply(Reply(body=fx("discovery/compat_models_synthetic.json")))
    configure(ui, server)
    models = request(ui, "/api/models", {})["models"]
    assert [m["id"] for m in models] == ["alpha", "beta", "gamma", "zeta"] and "raw" not in models[0]
    server.reply(Reply(body=fx("discovery/openrouter_endpoints_synthetic.json")))
    providers = request(ui, "/api/providers", {"kind": "openrouter", "base_url": server.url + "/v1",
                                                "api_key": "k", "model": "a/model-one"})["providers"]
    assert [p["slug"] for p in providers] == ["deepinfra/turbo", "Together", None]


def test_u5_browser_disconnect_cancels_upstream(ui, server):
    delta = b'data: {"choices":[{"delta":{"content":"tok "}}]}\n\n'
    server.reply(Reply(body=[delta] * 200, chunk_delay=0.02, hold=True))
    configure(ui, server)
    resp = request(ui, "/api/complete", {"prompt": "x"}, raw=True)
    assert json.loads(resp.readline())["type"] == "text"
    resp.close()
    assert server.client_closed.wait(10)  # the UI's Cancelled closed the upstream connection


# --- spec v6 §18.3: generation settings in the request panel (U6–U10) ---

ALL_PAGE = dict(reasoning="low", max_tokens=64, temperature=0.7, top_p=0.9, top_k=40, min_p=0.05,
                repetition_penalty=1.1, presence_penalty=0.5, frequency_penalty=-0.5, seed=7, stop=["END", "###"])


def complete_with(ui, settings, **body):
    return events(request(ui, "/api/complete", {"prompt": "x", "settings": settings, **body}, raw=True))


def test_u6_openrouter_all_settings_reach_the_body(ui, server):
    server.reply(canned("chat", True))
    configure(ui, server, kind="openrouter", api_key="k", provider="deepinfra/turbo")
    evs = complete_with(ui, ALL_PAGE)
    body = server.requests[0]["body"]
    for name, value in ALL_PAGE.items():
        if name != "reasoning":
            assert body[name] == value, name
    assert body["reasoning"] == {"effort": "low"}
    provider = {"order": ["deepinfra/turbo"], "allow_fallbacks": False}
    assert evs[-1]["type"] == "result" and evs[-1]["settings_sent"] == {**ALL_PAGE, "provider": provider}


def test_u6_lmstudio_native_names(ui, server):
    server.reply(canned("native", True))
    configure(ui, server, kind="lmstudio")
    evs = complete_with(ui, {"reasoning": "off", "top_k": 40, "repetition_penalty": 1.1})
    req = server.requests[0]
    assert req["path"] == "/api/v1/chat"
    assert req["body"]["top_k"] == 40 and req["body"]["repeat_penalty"] == 1.1
    assert "repetition_penalty" not in req["body"]
    assert evs[-1]["settings_sent"] == {"reasoning": "off", "top_k": 40, "repetition_penalty": 1.1}


def test_u6_nulls_and_absent_keys_are_not_sent(ui, server):
    server.reply(canned("chat", True))
    configure(ui, server)
    evs = complete_with(ui, {"temperature": None, "top_p": None, "seed": 5, "reasoning": None})
    body = server.requests[0]["body"]
    assert "temperature" not in body and "top_p" not in body and body["seed"] == 5
    assert evs[-1]["settings_sent"] == {"seed": 5}
    server.requests.clear()
    server.reply(canned("chat", True))
    evs = complete_with(ui, {"reasoning": ""})  # "" means backend default, even on openai_compat
    assert evs[-1]["type"] == "result" and evs[-1]["settings_sent"] == {}
    assert "reasoning" not in server.requests[0]["body"]


def test_u7_state_supported(ui, server):
    for kind in ("lmstudio", "openrouter"):
        state = configure(ui, server, kind=kind)
        assert set(state["supported"]) == {"", "off", "low", "medium", "high"}
        for level, names in state["supported"].items():
            assert names == sorted(modelspell.supported_settings(kind, level or None))
    state = configure(ui, server, kind="openai_compat")
    assert list(state["supported"]) == [""]
    assert state["supported"][""] == sorted(modelspell.supported_settings("openai_compat"))
    assert "reasoning" not in state["supported"][""]
    assert request(ui, "/api/state")["supported"] == state["supported"]
    assert "reasoning" not in state and "max_tokens" not in state


@pytest.mark.parametrize("settings", [["x"], "temperature=1", 5, {"temperature": 1, "provider": {"order": ["x"]}},
                                      {"bogus": 1}, {"extra": {"a": 1}}])
def test_u8_malformed_settings_are_400(ui, server, settings):
    configure(ui, server)
    assert status_of(lambda: request(ui, "/api/complete", {"prompt": "x", "settings": settings})) == 400
    assert server.requests == []


@pytest.mark.parametrize("kind,settings,cls", [
    ("lmstudio", {"reasoning": "low", "top_k": 40}, "UnsupportedSetting"),
    ("openrouter", {"temperature": 3}, "ConfigError"),
    ("openrouter", {"stop": "END"}, "ConfigError"),
])
def test_u8_modelspell_refusals_are_one_error_event(ui, server, kind, settings, cls):
    configure(ui, server, kind=kind, api_key="k" if kind == "openrouter" else None)
    evs = complete_with(ui, settings)
    assert len(evs) == 1 and evs[0]["type"] == "error" and evs[0]["error"].startswith(cls + ":")
    assert server.requests == []


def test_u9_config_refuses_moved_fields(ui, server):
    before = configure(ui, server, kind="lmstudio")
    for body in ({"reasoning": "high"}, {"max_tokens": 64}, {"kind": "openrouter", "reasoning": ""},
                 {"max_tokens": None}):
        assert status_of(lambda: request(ui, "/api/config", body)) == 400
    assert request(ui, "/api/state") == before


def test_u10_zero_values_survive(ui, server):
    server.reply(canned("chat", True))
    configure(ui, server, kind="openrouter", api_key="k")
    evs = complete_with(ui, {"temperature": 0, "top_k": 0, "seed": 0})
    body = server.requests[0]["body"]
    for name in ("temperature", "top_k", "seed"):
        assert body[name] == 0 and body[name] is not False
    assert evs[-1]["settings_sent"] == {"temperature": 0, "top_k": 0, "seed": 0}
