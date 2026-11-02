"""Owner-check battery against fakes and stand-ins: O1–O7 (techspec 0.3.0 §9.10, AT-7)."""
import threading
import time

import pytest

from conftest import KEY, Reply, fx, json_reply, sse_reply
from modelspell import (AuthError, Cancelled, Endpoint, ModelInfo, Result, Usage)
from ui import owner_checks as oc
from ui.owner_checks import Battery, Timing

FAST = Timing(first_token_after=0.2, non_stream_after=0.2, bound=1.0, watchdog=20.0, cleanup=2.0)
SSE = {"Content-Type": "text/event-stream"}


def or_ep(server, key=KEY, kind="openrouter"):
    return Endpoint(kind=kind, base_url=server.url + "/v1", api_key=key if kind == "openrouter" else None)


def held(server, index):
    """Trigger: the hold of the server's request `index` (0-based) has begun."""
    return lambda: server.hold_event(index)


def full_triggers(server):
    return {"cancel_first_token": held(server, 2), "cancel_non_stream": held(server, 4)}  # Full's request order


def content_events(n: int) -> bytes:
    return b"".join(f'data: {{"choices":[{{"delta":{{"content":"{i} "}}}}]}}\n\n'.encode() for i in range(1, n + 1))


def full_replies(usage: bool = True) -> list[Reply]:
    """One reply per request of the Full preset, in check order (cancel_pre_send/reused send nothing)."""
    sse, js = ("chat/or_text_synthetic.sse", "chat/or_text_synthetic.json") if usage else \
        ("chat/compat_text_synthetic.sse", "chat/compat_text_synthetic.json")
    return [sse_reply(sse), json_reply(js),
            Reply(body=[], hold=True, headers=SSE),                                    # first token: held before data
            Reply(body=[content_events(5)], hold=True, headers=SSE),                   # mid-stream: five, then held
            Reply(body=[], hold=True, headers={"Content-Type": "application/json"}),   # non-stream: held after headers
            sse_reply(sse)]                                                            # cancel_late


def states(record) -> dict:
    return {r["check"]: r["state"] for r in record.rows}


def result(text="ok", reasoning=None, cost=0.0001, usage=True, transport="chat_completions") -> Result:
    return Result(text=text, reasoning=reasoning, usage=Usage(5, 1, 6) if usage else None, cost=cost,
                  finish_reason="stop", truncated=False, model="m", provider="P", response_id=None,
                  transport=transport, reasoning_details=None, raw_usage=None)


def cancelled(sent: bool, text: str = "") -> Cancelled:
    exc = Cancelled()
    exc.sent = sent
    exc.partial = Result(text=text, reasoning=None, usage=None, cost=None, finish_reason="cancelled",
                         truncated=False, model=None, provider=None, response_id=None,
                         transport="chat_completions", reasoning_details=None, raw_usage=None)
    return exc


def is_padded(messages) -> bool:
    return messages[-1]["content"] == oc.PADDED[0][-1]["content"]


def is_count(messages) -> bool:
    return messages[-1]["content"] == oc.COUNT[0][-1]["content"]


def wait_cancel(handle, limit=10.0):
    deadline = time.monotonic() + limit
    while not handle.cancelled:
        assert time.monotonic() < deadline, "stand-in never saw cancel()"
        time.sleep(0.005)


def short_result(on_text):
    if on_text:
        on_text("ok")
    return result("ok")


# --- O1: a well-behaved backend passes -------------------------------------------------------------

def test_o1_well_behaved_backend_passes(server):
    server.reply(*full_replies())
    b = Battery(or_ep(server), "m", "Full", timing=FAST,
                triggers=full_triggers(server))
    rec = b.run()
    assert states(rec) == {c: "pass" for c in oc.CHECKS}, [(r["check"], r["note"]) for r in rec.rows]
    assert rec.summary["verdict"] == "pass" and len(server.requests) == 6
    import json
    lines = rec.jsonl().splitlines()
    assert lines[0].startswith("OC-CONFIG ") and json.loads(lines[0][10:])["preset"] == "Full"
    assert all(json.loads(line[3:])["check"] for line in lines[1:])
    assert KEY not in rec.jsonl() + rec.markdown()
    assert rec.summary["reported_cost_subtotal"] is not None


# --- O2: a complete() that ignores cancellation is caught (M30-14) --------------------------------

def test_o2_ignored_cancellation_is_caught():
    timing = Timing(first_token_after=0.05, non_stream_after=0.05, bound=0.2, watchdog=10.0, cleanup=1.0)

    def standin(endpoint, model, messages, *, cancel, on_text=None, on_reasoning=None, **kw):
        if cancel.cancelled:
            return short_result(on_text)
        if on_text and is_padded(messages):
            wait_cancel(cancel)
            for _ in range(5):
                on_text("x")
            return result("xxxxx")
        if on_text and is_count(messages):
            n = 0
            while not cancel.cancelled:
                on_text("n")
                n += 1
                time.sleep(0.005)
            for _ in range(5):
                on_text("n")
            return result("n" * (n + 5))
        if is_count(messages):
            wait_cancel(cancel)
            time.sleep(2 * timing.bound)
            return result("1 2 3")
        return short_result(on_text)

    rec = Battery(Endpoint(kind="openrouter", api_key="k"), "m", "Full", timing=timing, complete=standin).run()
    s = states(rec)
    assert s["cancel_pre_send"] == s["cancel_reused"] == "fail"
    assert s["cancel_first_token"] == s["cancel_mid_stream"] == "fail"
    assert s["cancel_non_stream"] == "inconclusive"
    assert s["stream_usage"] == s["nonstream_usage"] == s["cancel_late"] == "pass"
    assert rec.summary["verdict"] == "fail"


# --- O3: missing usage is caught where it matters ------------------------------------------------

def test_o3_missing_usage_openrouter_fails(server):
    server.reply(*full_replies(usage=False))
    rec = Battery(or_ep(server), "m", "Full", timing=FAST,
                  triggers=full_triggers(server)).run()
    s = states(rec)
    assert s.pop("stream_usage") == s.pop("nonstream_usage") == "fail"
    assert set(s.values()) == {"pass"}, rec.rows


def test_o3_missing_usage_compat_inconclusive(server):
    server.reply(sse_reply("chat/compat_text_synthetic.sse"), json_reply("chat/compat_text_synthetic.json"))
    rec = Battery(or_ep(server, kind="openai_compat"), "m", "Usage", timing=FAST).run()
    assert states(rec) == {"stream_usage": "inconclusive", "nonstream_usage": "inconclusive"}
    assert rec.summary["verdict"] == "incomplete" and rec.summary["reported_cost_subtotal"] is None


# --- O4: states and verdicts ---------------------------------------------------------------------

def test_o4_auth_error_blocks_the_run():
    calls = []

    def standin(*a, **kw):
        calls.append(1)
        raise AuthError("no key", status=401)

    rec = Battery(Endpoint(kind="openrouter", api_key="k"), "m", "Full", timing=FAST, complete=standin).run()
    assert set(states(rec).values()) == {"blocked"} and len(rec.rows) == 8 and len(calls) == 1
    assert "0 of 8 passed" in rec.markdown() and rec.summary["verdict"] == "incomplete"


def test_o4_result_before_trigger_is_inconclusive():
    def standin(endpoint, model, messages, *, cancel, on_text=None, **kw):
        return short_result(on_text)

    rec = Battery(Endpoint(kind="openrouter", api_key="k"), "m", "Quick",
                  timing=Timing(first_token_after=5, watchdog=10), complete=standin).run()
    assert states(rec)["cancel_first_token"] == "inconclusive"


def _one_more_then_cancelled(endpoint, model, messages, *, cancel, on_text=None, **kw):
    delivered = []

    def deliver(s):
        delivered.append(s)
        on_text(s)

    if cancel.cancelled:
        raise cancelled(False)
    if on_text and is_padded(messages):
        wait_cancel(cancel)
        deliver("late")
        raise cancelled(True, "".join(delivered))
    if on_text and is_count(messages):
        while not cancel.cancelled:
            deliver("n ")
            time.sleep(0.005)
        deliver("n ")
        raise cancelled(True, "".join(delivered))
    return short_result(on_text)


def test_o4_one_admitted_callback_after_cancel():
    rec = Battery(Endpoint(kind="openrouter", api_key="k"), "m", "Quick",
                  timing=Timing(first_token_after=0.05, watchdog=10), complete=_one_more_then_cancelled).run()
    s = states(rec)
    assert s["cancel_first_token"] == "inconclusive"  # step 3.6
    assert s["cancel_mid_stream"] == "pass", rec.rows  # step 3.3 allows one admitted callback


def test_o4_final_only_after_commit_is_inconclusive():
    def standin(endpoint, model, messages, *, cancel, on_text=None, on_reasoning=None, **kw):
        if on_text and is_padded(messages):
            wait_cancel(cancel)
            on_text("ans")
            on_reasoning("why")
            return result("ans", reasoning="why")
        return short_result(on_text)

    rec = Battery(Endpoint(kind="openrouter", api_key="k"), "m", "Quick",
                  timing=Timing(first_token_after=0.05, watchdog=10), complete=standin).run()
    assert states(rec)["cancel_first_token"] == "inconclusive"  # step 2: one callback per channel


def test_o4_late_cancelled_before_sending_is_inconclusive():
    timing = Timing(first_token_after=0.05, bound=0.1, watchdog=10)

    def standin(endpoint, model, messages, *, cancel, on_text=None, **kw):
        if on_text and is_padded(messages):
            wait_cancel(cancel)
            time.sleep(2 * timing.bound)
            raise cancelled(False)
        return short_result(on_text)

    rec = Battery(Endpoint(kind="openrouter", api_key="k"), "m", "Quick", timing=timing, complete=standin).run()
    assert states(rec)["cancel_first_token"] == "inconclusive"  # step 3.2 precedes 3.5


def test_o4_verdict_function_and_guards():
    def rows(*st):
        return [{"state": s} for s in st]

    assert oc.verdict(rows("pass", "pass")) == "pass"
    assert oc.verdict(rows("pass", "inconclusive")) == "incomplete"
    assert oc.verdict(rows("pass", "blocked")) == "incomplete"
    assert oc.verdict(rows("inconclusive", "inconclusive")) == "incomplete"
    assert oc.verdict(rows("fail", "pass", "blocked")) == "fail"
    assert oc.verdict(rows("pass", "pass"), stopped=True) == "incomplete"
    with pytest.raises(ValueError):
        oc.validate_preset(["stream_usage"] * (oc.MAX_REQUESTS + 1))
    assert oc.validate_preset(oc.PRESETS["Full"]) == 6
    for v, expected in (("fail", "fail"), ("incomplete", "skip"), ("pass", "pass")):
        rec = oc.Record(config={}, rows=[{"check": "c", "state": "pass" if v == "pass" else "inconclusive"}],
                        summary={"verdict": v})
        assert oc.pytest_outcome(rec)[0] == expected


# --- O5: Stop and stuck calls --------------------------------------------------------------------

def drain(b, until="done", limit=30.0):
    events, deadline = [], time.monotonic() + limit
    while time.monotonic() < deadline:
        ev = b.next_event(0.1)
        if ev is not None:
            events.append(ev)
            if ev["type"] == until:
                return events
    raise AssertionError(f"no {until} event within {limit}s")


def test_o5a_stop_during_non_stream_check(server):
    server.reply(*full_replies())
    never = threading.Event()
    b = Battery(or_ep(server), "m", "Full", timing=FAST,
                triggers={"cancel_first_token": held(server, 2), "cancel_non_stream": lambda: never})
    b.start()
    deadline = time.monotonic() + 30
    while not (len(server.requests) == 5 and (b.current or {}).get("check") == "cancel_non_stream"):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    t0 = time.monotonic()
    assert b.stop() is True
    events = drain(b)
    assert time.monotonic() - t0 < 1.5
    done = events[-1]
    last = [e for e in events if e["type"] == "check"][-1]["record"]
    assert last["check"] == "cancel_non_stream" and last["state"] == "inconclusive"
    assert last["note"] == oc.NOTE_STOPPED
    assert done["stopped"] is True and done["verdict"] == "incomplete"
    time.sleep(0.2)
    assert len(server.requests) == 5 and b.stop() is False
    deadline = time.monotonic() + 5
    while not b.finished:
        assert time.monotonic() < deadline
        time.sleep(0.01)


class Blocking:
    """A stand-in complete() that ignores cancel and blocks until released."""

    def __init__(self):
        self.release = threading.Event()
        self.calls = 0

    def __call__(self, *a, **kw):
        self.calls += 1
        self.release.wait(60)
        return result()


def test_o5b_stuck_call_hits_the_watchdog():
    stand = Blocking()
    b = Battery(Endpoint(kind="openrouter", api_key="k"), "m", "Usage",
                timing=Timing(watchdog=0.5, cleanup=0.5), complete=stand)
    b.start()
    events = drain(b)
    row = [e for e in events if e["type"] == "check"][-1]["record"]
    assert row["state"] == "fail" and row["note"] == oc.NOTE_WATCHDOG
    assert events[-1]["stuck"] is True and b.ended and not b.finished and stand.calls == 1
    stand.release.set()
    deadline = time.monotonic() + 5
    while not b.finished:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert b.next_event(0.2) is None  # the late result emits nothing


def test_o5c_stop_wakes_a_trigger_wait_and_nothing_later_starts():
    calls, never = [], threading.Event()

    def standin(endpoint, model, messages, *, cancel, on_text=None, **kw):
        calls.append("padded" if is_padded(messages) else ("count" if is_count(messages) else "short"))
        if is_padded(messages):
            wait_cancel(cancel)
            raise cancelled(True)
        return short_result(on_text)

    b = Battery(Endpoint(kind="openrouter", api_key="k"), "m", "Quick", timing=Timing(watchdog=10),
                triggers={"cancel_first_token": lambda: never}, complete=standin)
    b.start()
    deadline = time.monotonic() + 10
    while (b.current or {}).get("check") != "cancel_first_token" or len(calls) < 2:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    b.stop()
    events = drain(b)
    rows = [e["record"] for e in events if e["type"] == "check"]
    assert [r["check"] for r in rows] == ["stream_usage", "cancel_first_token"]
    assert rows[-1]["note"] == oc.NOTE_STOPPED and calls == ["short", "padded"]


def test_o5d_run_is_bounded():
    stand = Blocking()
    b = Battery(Endpoint(kind="openrouter", api_key="k"), "m", "Usage",
                timing=Timing(watchdog=0.5, cleanup=0.5), complete=stand)
    t0 = time.monotonic()
    rec = b.run()
    assert time.monotonic() - t0 < 0.5 + 0.5 + 2.0
    assert rec.rows[-1]["state"] == "fail" and rec.summary["stuck"] and rec.summary["cleanup_pending"]
    stand.release.set()


def test_o5e_held_discovery():
    release, completes = threading.Event(), []

    def list_models(endpoint, timeout):
        release.wait(60)
        return []

    b = Battery(Endpoint(kind="lmstudio"), "qwen", "Full", route="native", timing=Timing(cleanup=0.5),
                list_models=list_models, complete=lambda *a, **k: completes.append(1))
    out = {}
    t = threading.Thread(target=lambda: out.update(rec=b.run()), daemon=True)
    t.start()
    deadline = time.monotonic() + 10
    while (b.current or {}).get("check") != "preparation":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    b.stop()
    t.join(0.5 + 2.0)
    assert not t.is_alive()
    rec = out["rec"]
    assert rec.summary["stopped"] and rec.summary["cleanup_pending"] and completes == [] and rec.rows == []
    release.set()
    time.sleep(0.1)
    assert b.next_event(0.2) is None


# --- O6: route selector --------------------------------------------------------------------------

def test_o6_default_route_uses_responses(server):
    server.reply(sse_reply("responses/resp_text_synthetic.sse"), json_reply("responses/resp_text_synthetic.json"))
    rec = Battery(Endpoint(kind="lmstudio", base_url=server.url + "/v1"), "m", "Usage", timing=FAST).run()
    assert [r["path"] for r in server.requests] == ["/v1/responses"] * 2
    assert rec.config["route"] == "default" and rec.config["transport"] == "responses"


def test_o6_native_with_id_discovers(server):
    server.reply(Reply(body=fx("discovery/lmstudio_models_synthetic.json")), sse_reply("native/native_text_synthetic.sse"),
                 json_reply("native/native_text_synthetic.json"))
    b = Battery(Endpoint(kind="lmstudio", base_url=server.url + "/v1"), "qwen/qwen3-8b", "Usage", route="native",
                timing=FAST)
    b.start()
    events = drain(b)
    assert [e["type"] for e in events][:2] == ["plan", "stage"] and events[0]["discovery_requests"] == 1
    paths = [r["path"] for r in server.requests]
    assert paths == ["/api/v1/models", "/api/v1/chat", "/api/v1/chat"]
    assert all(r["body"]["model"] == "qwen/qwen3-8b" for r in server.requests[1:])
    rec = b.record()
    assert rec.config["route"] == "native" and rec.config["model_arg"] == "ModelInfo"
    assert '"model_arg": "ModelInfo"' in rec.jsonl()


def test_o6_native_model_missing_from_discovery(server):
    server.reply(Reply(body=fx("discovery/lmstudio_models_synthetic.json")), sse_reply("native/native_text_synthetic.sse"),
                 json_reply("native/native_text_synthetic.json"))
    rec = Battery(Endpoint(kind="lmstudio", base_url=server.url + "/v1"), "absent", "Usage", route="native",
                  timing=FAST).run()
    assert rec.config["model_arg"] == "id"


def test_o6_native_with_model_info_skips_discovery(server):
    server.reply(sse_reply("native/native_text_synthetic.sse"), json_reply("native/native_text_synthetic.json"))
    info = ModelInfo("qwen/qwen3-8b", None, None, None, None, True, ("off", "on"), None, {})
    rec = Battery(Endpoint(kind="lmstudio", base_url=server.url + "/v1"), info, "Usage", route="native",
                  timing=FAST).run()
    assert [r["path"] for r in server.requests] == ["/api/v1/chat"] * 2
    assert rec.config["model_arg"] == "ModelInfo"


def test_o6_discovery_error_blocks_everything(server):
    server.reply(Reply(status=500, body=b"boom"))
    rec = Battery(Endpoint(kind="lmstudio", base_url=server.url + "/v1"), "m", "Full", route="native",
                  timing=FAST).run()
    assert set(states(rec).values()) == {"blocked"} and len(rec.rows) == 8
    assert [r["path"] for r in server.requests] == ["/api/v1/models"]


def test_o6_native_refused_off_lmstudio():
    with pytest.raises(ValueError, match="native"):
        Battery(Endpoint(kind="openrouter", api_key="k"), "m", route="native")


# --- O7: cost subtotal (M30-19) ------------------------------------------------------------------

def test_o7_cost_subtotal():
    short_streamed = []

    def standin(endpoint, model, messages, *, cancel, on_text=None, **kw):
        if cancel.cancelled and not (is_padded(messages) or is_count(messages)):
            raise cancelled(False)  # cancel_pre_send, cancel_reused
        if is_padded(messages) or is_count(messages):
            raise cancelled(True)   # the three in-flight checks
        if on_text is None:
            return result(cost=0.001)                                  # nonstream_usage
        short_streamed.append(1)
        on_text("ok")
        return result(cost=0.001 if len(short_streamed) == 1 else 0.0005)  # stream_usage, cancel_late

    rec = Battery(Endpoint(kind="openrouter", api_key="k"), "m", "Full", timing=Timing(watchdog=10),
                  complete=standin).run()
    s = rec.summary
    assert s["reported_cost_subtotal"] == pytest.approx(0.0025)
    assert s["unknown_cost"] == 3 and s["cost_incomplete"] is True
    assert "reported cost subtotal (incomplete) $0.002500" in rec.markdown()
