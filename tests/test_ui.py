"""Developer UI server (ui/server.py): U1–U16 (spec v8 §18.3). The HTML/JS is not tested."""
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


def all_events(resp):
    return [json.loads(line) for line in resp.read().splitlines() if line]


def events(resp):
    """The events after /api/complete's first event, {"type": "started", "id": ...} (spec v8 §18.3)."""
    return [e for e in all_events(resp) if e["type"] != "started"]


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
    server.reply(Reply(body=fx("discovery/openrouter_endpoints_synthetic.json")),
                 sse_reply("chat/or_reasoning_synthetic.sse"))
    configure(ui, server, kind="openrouter", api_key=KEY, provider="deepinfra/turbo", model="a/model-one")
    evs = events(request(ui, "/api/complete", {"prompt": "task", "system": "sys",
                                               "settings": {"reasoning": "high", "max_tokens": 64}}, raw=True))
    result = evs[-1]["result"]
    assert evs[-1]["type"] == "result"
    assert "".join(e["text"] for e in evs if e["type"] == "text") == result["text"] == "ans"
    assert "".join(e["text"] for e in evs if e["type"] == "reasoning") == result["reasoning"]
    body = server.requests[1]["body"]
    assert body["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    assert body["provider"] == {"order": ["deepinfra/turbo"], "allow_fallbacks": False}
    assert body["reasoning"] == {"effort": "high"} and body["max_tokens"] == 64 and body["stream"] is True


def test_automatic_ui_uses_shared_selector_and_effective_pin(ui, server):
    def row(tag, speed, price):
        return {"tag": tag, "provider_name": tag, "throughput_last_30m": {"p50": speed},
                "pricing": {"prompt": price, "completion": price}}

    discovery = {"data": {"endpoints": [row("too-slow", 100, "0"), row("fast/route", 120, "0.0000001")]}}
    server.reply(Reply(body=json.dumps(discovery).encode()), canned("chat", False), canned("chat", True))
    configure(ui, server, kind="openrouter", api_key=KEY, model="a/model-one",
              provider_mode="automatic", balanced_min_tps=100)
    result = complete_with(ui, {"max_tokens": 64})[-1]
    assert result["type"] == "result"
    assert [r["method"] for r in server.requests] == ["GET", "POST", "POST"]
    probe, actual = [r["body"] for r in server.requests[1:]]
    assert probe["provider"] == actual["provider"] == {"order": ["fast/route"], "allow_fallbacks": False}
    assert probe["messages"] != actual["messages"]


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
    assert json.loads(resp.readline())["type"] == "started"
    assert json.loads(resp.readline())["type"] == "text"
    resp.close()
    assert server.client_closed.wait(10)  # the UI's Cancelled closed the upstream connection


# --- spec v6 §18.3: generation settings in the request panel (U6–U10) ---

ALL_PAGE = dict(reasoning="low", max_tokens=64, temperature=0.7, top_p=0.9, top_k=40, min_p=0.05,
                repetition_penalty=1.1, presence_penalty=0.5, frequency_penalty=-0.5, seed=7, stop=["END", "###"])


def complete_with(ui, settings, **body):
    return events(request(ui, "/api/complete", {"prompt": "x", "settings": settings, **body}, raw=True))


def test_u6_openrouter_all_settings_reach_the_body(ui, server):
    server.reply(Reply(body=fx("discovery/openrouter_endpoints_synthetic.json")), canned("chat", True))
    configure(ui, server, kind="openrouter", api_key=KEY, provider="deepinfra/turbo", model="a/model-one")
    evs = complete_with(ui, ALL_PAGE)
    body = server.requests[1]["body"]
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


# --- spec v8 §18.3: Stop through the handle and the owner checks (U11–U16) ---

import time  # noqa: E402

from modelspell import Result, Usage  # noqa: E402
from ui import owner_checks  # noqa: E402

FAST = owner_checks.Timing(first_token_after=0.2, non_stream_after=0.2, bound=1.0, watchdog=20.0, cleanup=2.0)
SSE = {"Content-Type": "text/event-stream"}


def ui_with(battery_options):
    srv = UIServer(0, battery_options=battery_options)
    threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True).start()
    return srv


@pytest.fixture
def make_ui():
    made = []

    def make(**battery_options):
        made.append(ui_with(battery_options))
        return made[-1]

    yield make
    for srv in made:
        srv.shutdown()
        srv.server_close()


def read_event(resp):
    line = resp.readline()
    return json.loads(line) if line else None


def read_until_done(resp, limit=30.0):
    out, deadline = [], time.monotonic() + limit
    while time.monotonic() < deadline:
        ev = read_event(resp)
        if ev is None:
            break
        out.append(ev)
        if ev["type"] == "done":
            break
    return out


def until(cond, limit=30.0):
    deadline = time.monotonic() + limit
    while not cond():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


def a_result(text="ok"):
    return Result(text=text, reasoning=None, usage=Usage(5, 1, 6), cost=0.0001, finish_reason="stop",
                  truncated=False, model="m", provider="P", response_id=None, transport="chat_completions",
                  reasoning_details=None, raw_usage=None)


def test_u11_new_routes_need_the_token(ui):
    for path in ("/api/complete/stop", "/api/checks", "/api/checks/stop"):
        assert status_of(lambda: request(ui, path, {}, token=False)) == 403


def test_u12_stop_through_the_handle(ui, server):
    server.reply(Reply(hold_headers=True))
    configure(ui, server)
    resp = request(ui, "/api/complete", {"prompt": "x"}, raw=True)
    started = read_event(resp)
    assert started["type"] == "started" and started["id"]
    assert server.reached.wait(30)
    t0 = time.monotonic()
    assert request(ui, "/api/complete/stop", {"id": started["id"]}) == {"stopping": True}
    rest = [json.loads(line) for line in resp.read().splitlines() if line]
    assert time.monotonic() - t0 < 1.0
    assert len(rest) == 1 and rest[0]["type"] == "cancelled" and rest[0]["sent"] is True
    assert rest[0]["partial"]["finish_reason"] == "cancelled"
    assert server.client_closed.wait(5)
    assert request(ui, "/api/complete/stop", {"id": started["id"]}) == {"already_finished": True}
    assert status_of(lambda: request(ui, "/api/complete/stop", {})) == 400


def test_u13_owner_check_route(make_ui, server):
    gate = threading.Event()
    ui = make_ui(timing=FAST, triggers={"cancel_first_token": lambda: gate})
    server.reply(sse_reply("chat/or_text_synthetic.sse"), Reply(body=[], hold=True, headers=SSE),
                 Reply(body=[b"".join(f'data: {{"choices":[{{"delta":{{"content":"{i}"}}}}]}}\n\n'.encode()
                                      for i in range(5))], hold=True, headers=SSE))
    configure(ui, server, kind="openrouter", api_key=KEY)
    resp = request(ui, "/api/checks", {"preset": "Quick"}, raw=True)
    assert read_event(resp)["type"] == "plan"
    first = read_event(resp)
    assert first["type"] == "check" and first["record"]["check"] == "stream_usage"
    assert request(ui, "/api/state")["checks_running"] is True
    assert status_of(lambda: request(ui, "/api/checks", {"preset": "Quick"})) == 409
    gate.set()
    evs = [first] + read_until_done(resp)
    kinds = [e["type"] for e in evs if e["type"] != "progress"]
    assert kinds == ["check", "check", "check", "done"]
    done = evs[-1]
    assert done["verdict"] == "pass", done["record_markdown"]
    lines = done["record_jsonl"].splitlines()
    assert all(json.loads(line.split(" ", 1)[1]) for line in lines)
    assert KEY not in done["record_jsonl"] + done["record_markdown"]
    until(lambda: request(ui, "/api/state")["checks_running"] is False)


def test_u14_owner_check_stop(make_ui, server):
    never = threading.Event()
    ui = make_ui(timing=FAST, triggers={"cancel_first_token": lambda: server.hold_event(0),
                                        "cancel_non_stream": lambda: never})
    server.reply(Reply(body=[], hold=True, headers=SSE),
                 Reply(body=[b"".join(f'data: {{"choices":[{{"delta":{{"content":"{i}"}}}}]}}\n\n'.encode()
                                      for i in range(5))], hold=True, headers=SSE),
                 Reply(body=[], hold=True, headers={"Content-Type": "application/json"}))
    configure(ui, server, kind="openrouter", api_key=KEY)
    resp = request(ui, "/api/checks", {"preset": "Cancellation"}, raw=True)
    until(lambda: len(server.requests) == 3 and (ui.battery.current or {}).get("check") == "cancel_non_stream")
    t0 = time.monotonic()
    assert request(ui, "/api/checks/stop", {}) == {"stopping": True}
    seen = read_until_done(resp)
    assert time.monotonic() - t0 < 1.5
    after_stop = [e for e in seen if e["type"] not in ("progress",)]
    checks = [e["record"] for e in after_stop if e["type"] == "check"]
    stopped = [c for c in checks if c["check"] == "cancel_non_stream"]
    assert len(stopped) == 1 and stopped[0]["state"] == "inconclusive"
    assert stopped[0]["note"] == owner_checks.NOTE_STOPPED
    assert after_stop[-1]["type"] == "done" and after_stop[-1]["stopped"] is True
    assert not any(c["check"] in ("cancel_late", "cancel_reused") for c in checks)
    assert len(server.requests) == 3
    until(lambda: request(ui, "/api/state")["checks_running"] is False)


def _quick_standin(endpoint, model, messages, *, cancel, on_text=None, **kw):
    if cancel.cancelled:
        from modelspell import Cancelled
        exc = Cancelled()
        exc.sent = False
        raise exc
    if on_text:
        on_text("ok")
    return a_result()


def test_u15_presets_in_state_and_default_full(make_ui, server):
    ui = make_ui(timing=owner_checks.Timing(first_token_after=0.05, non_stream_after=0.05, watchdog=10),
                 complete=_quick_standin)
    configure(ui, server, kind="openrouter", api_key=KEY)
    state = request(ui, "/api/state")
    assert state["default_preset"] == "Full"
    for name, checks in owner_checks.PRESETS.items():
        p = state["presets"][name]
        assert p["checks"] == list(checks) and p["calls"] == len(checks)
        assert p["requests"] == owner_checks.validate_preset(checks)
    assert state["presets"]["Full"]["requests"] == 6
    resp = request(ui, "/api/checks", {}, raw=True)
    evs = read_until_done(resp)
    assert evs[0]["type"] == "plan" and evs[0]["preset"] == "Full" and len(evs[0]["checks"]) == 8
    assert evs[-1]["type"] == "done"
    until(lambda: request(ui, "/api/state")["checks_running"] is False)
    assert status_of(lambda: request(ui, "/api/checks", {"route": "native"})) == 400
    assert status_of(lambda: request(ui, "/api/checks", {"preset": "Bogus"})) == 400


def test_u16_stuck_slot(make_ui, server):
    release = threading.Event()

    def blocking(*a, **kw):
        release.wait(60)
        return a_result()

    ui = make_ui(timing=owner_checks.Timing(watchdog=0.5, cleanup=0.5), complete=blocking)
    configure(ui, server, kind="openrouter", api_key=KEY)
    evs = read_until_done(request(ui, "/api/checks", {"preset": "Usage"}, raw=True))
    assert evs[-1]["type"] == "done" and evs[-1]["stuck"] is True
    state = request(ui, "/api/state")
    assert state["checks_running"] is True and state["checks_stuck"] is True
    assert status_of(lambda: request(ui, "/api/checks", {"preset": "Usage"})) == 409
    release.set()
    until(lambda: request(ui, "/api/state")["checks_running"] is False, 10)
    evs = read_until_done(request(ui, "/api/checks", {"preset": "Usage"}, raw=True))
    assert evs[-1]["type"] == "done" and evs[-1]["stuck"] is False
