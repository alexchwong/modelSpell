"""Opt-in HTTP 429 recovery: L1–L16 (spec v9 §6.11, §12.12)."""
import dataclasses
import email.utils
import json
import threading
import time
import urllib.request

import pytest

from conftest import KEY, Reply, canned, ep, json_reply, sse_reply
from modelspell import (BackendError, CancelHandle, Cancelled, ConfigError, ResponseFormatError, RetryEvent,
                        ToolDefinition, complete, complete_with_tools)
from modelspell import _http
from modelspell._cancel import _Watch

TOOLS = [ToolDefinition("read_label", "Read.", {"type": "object"})]
PIN = {"order": ["deepinfra"], "allow_fallbacks": False, "sort": "price"}
NOOP = lambda s: None  # noqa: E731


def r429(retry_after=None, body=b'{"error":{"message":"rate limited"}}'):
    headers = {"Content-Type": "application/json"}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return Reply(status=429, body=body, headers=headers)


def ok(entry, stream):
    if entry == "tools":
        return sse_reply("chat/or_tool_call_synthetic.sse") if stream else json_reply("chat/or_tool_call_synthetic.json")
    return canned("chat", stream)


def run(server, entry="complete", *, stream=False, **kw):
    endpoint = ep(server, "openrouter")
    if stream:
        kw.setdefault("on_text", NOOP)
    if entry == "tools":
        return complete_with_tools(endpoint, "m", "x", tools=TOOLS, provider=PIN, **kw)
    return complete(endpoint, "m", "x", provider=PIN, **kw)


@pytest.fixture
def pauses(monkeypatch):
    """Record each backoff delay and skip the wait; cancellation is still honoured."""
    seen = []
    original = _Watch.pause

    def pause(self, delay):
        seen.append(delay)
        original(self, 0)
    monkeypatch.setattr(_Watch, "pause", pause)
    return seen


ENTRIES = pytest.mark.parametrize("entry", ["complete", "tools"])
STREAMS = pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])


# --- L1: default is one attempt ----------------------------------------------------------------------

@ENTRIES
@STREAMS
def test_l1_default_makes_one_request(server, pauses, entry, stream):
    server.reply(r429("0"), ok(entry, stream))
    with pytest.raises(BackendError) as info:
        run(server, entry, stream=stream)
    assert info.value.status == 429 and len(server.requests) == 1 and pauses == []


# --- L2: 429 -> 429 -> success ------------------------------------------------------------------------

@ENTRIES
@STREAMS
def test_l2_recovers_with_identical_requests(server, pauses, entry, stream):
    events = []
    server.reply(r429(), r429(), ok(entry, stream))
    out = run(server, entry, stream=stream, rate_limit_retries=2, on_retry=events.append)
    assert (out.clean if entry == "tools" else out.text)
    assert len(server.requests) == 3
    assert len({r["raw"] for r in server.requests}) == 1  # byte-identical bodies
    assert server.requests[0]["body"]["provider"]["order"] == ["deepinfra"]  # routing retained
    assert server.requests[0]["body"]["provider"]["sort"] == "price"
    assert events == [RetryEvent(1, 2, 1.0, 429), RetryEvent(2, 3, 2.0, 429)]
    assert pauses == [1.0, 2.0]


def test_l2_retry_event_is_frozen_with_exact_fields():
    assert [f.name for f in dataclasses.fields(RetryEvent)] == ["attempt", "next_attempt", "delay", "status"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        RetryEvent(1, 2, 1.0, 429).delay = 0


# --- L3/L4: exhaustion and zero retries -------------------------------------------------------------

@ENTRIES
def test_l3_exhaustion_raises_the_last_redacted_429(server, pauses, entry):
    server.reply(r429(body=f'{{"error":"first {KEY}"}}'.encode()), r429(body=f'{{"error":"last {KEY}"}}'.encode()))
    endpoint = ep(server, "openrouter", api_key=KEY)
    with pytest.raises(BackendError) as info:
        if entry == "tools":
            complete_with_tools(endpoint, "m", "x", tools=TOOLS, provider=PIN, rate_limit_retries=1)
        else:
            complete(endpoint, "m", "x", rate_limit_retries=1)
    assert info.value.status == 429 and "last" in info.value.body and KEY not in info.value.body
    assert KEY not in str(info.value) and len(server.requests) == 2 and pauses == [1.0]


def test_l4_three_retries_use_nominal_backoff(server, pauses):
    server.reply(r429())
    with pytest.raises(BackendError):
        run(server, rate_limit_retries=3)
    assert len(server.requests) == 4 and pauses == [1.0, 2.0, 4.0]


# --- L5: Retry-After ----------------------------------------------------------------------------------

def test_l5_retry_after_values(server, pauses, monkeypatch):
    monkeypatch.setattr(_http, "_now", lambda: 1_000_000_000.0)
    future = email.utils.formatdate(1_000_000_000.0 + 7, usegmt=True)
    past = email.utils.formatdate(1_000_000_000.0 - 50, usegmt=True)
    server.reply(r429("3"), r429(future), r429("soon"), r429(past), canned("chat", False))
    with pytest.raises(BackendError):
        run(server, rate_limit_retries=3, max_retry_wait=100)
    server.requests.clear()
    pauses.clear()
    server.reply(r429("3"), r429(future), r429("soon"), canned("chat", False))
    run(server, rate_limit_retries=3, max_retry_wait=100)
    assert pauses == [3.0, 7.0, 4.0] and len(server.requests) == 4


def test_l5_retry_after_parser(monkeypatch):
    monkeypatch.setattr(_http, "_now", lambda: 1_000_000_000.0)
    assert _http.retry_after("0") == 0.0 and _http.retry_after(" 12 ") == 12.0
    assert _http.retry_after(email.utils.formatdate(1_000_000_010.0, usegmt=True)) == 10.0
    assert _http.retry_after(email.utils.formatdate(999_999_000.0, usegmt=True)) == 0.0
    for bad in (None, "", "-1", "1.5", "soon", "9" * 65, "Mon, 99 Foo 2026 00:00:00 GMT"):
        assert _http.retry_after(bad) is None, bad


def test_l5_too_long_retry_after_surfaces_at_once(server, pauses):
    server.reply(r429("999"), canned("chat", False))
    with pytest.raises(BackendError) as info:
        run(server, rate_limit_retries=3)
    assert info.value.status == 429 and len(server.requests) == 1 and pauses == []


# --- L6: cumulative wait limit ------------------------------------------------------------------------

def test_l6_max_retry_wait(server, pauses):
    server.reply(r429())
    with pytest.raises(BackendError):
        run(server, rate_limit_retries=3, max_retry_wait=0)
    assert len(server.requests) == 1 and pauses == []
    server.requests.clear()
    with pytest.raises(BackendError):
        run(server, rate_limit_retries=3, max_retry_wait=2.5)
    assert len(server.requests) == 2 and pauses == [1.0]


# --- L7/L8: only a transport 429 is retried ---------------------------------------------------------

NOT_RETRIED = [
    ("500", Reply(status=500, body=b"oops"), BackendError),
    ("400", Reply(status=400, body=b"bad"), BackendError),
    ("503_retry_after", Reply(status=503, body=b"busy", headers={"Retry-After": "1"}), BackendError),
    ("malformed", Reply(body=b"not json", headers={"Content-Type": "application/json"}), ResponseFormatError),
    ("stream_error_429", Reply(body=[b'data: {"error": {"code": 429, "message": "rate"}}\n\n'],
                               headers={"Content-Type": "text/event-stream"}), BackendError),
]


@pytest.mark.parametrize("name,reply,exc", NOT_RETRIED, ids=[n[0] for n in NOT_RETRIED])
def test_l7_other_failures_are_not_retried(server, pauses, name, reply, exc):
    server.reply(reply, canned("chat", True))
    with pytest.raises(exc):
        run(server, stream=name.startswith("stream"), rate_limit_retries=3)
    assert len(server.requests) == 1 and pauses == []


def test_l7_auth_and_timeout_are_not_retried(server, pauses):
    from modelspell import AuthError, RequestTimeout
    server.reply(Reply(status=401, body=b"no"))
    with pytest.raises(AuthError):
        run(server, rate_limit_retries=3)
    server.requests.clear()
    server.reply(Reply(delay=1.0, body=b"{}"))
    with pytest.raises(RequestTimeout):
        run(server, rate_limit_retries=3, timeout=0.2)
    assert len(server.requests) == 1 and pauses == []


def test_l8_callback_raising_a_429_is_not_retried(server, pauses):
    raised = BackendError("callback", status=429)

    def on_text(s):
        raise raised
    server.reply(canned("chat", True))
    with pytest.raises(BackendError) as info:
        run(server, rate_limit_retries=3, on_text=on_text)
    assert info.value is raised and len(server.requests) == 1 and pauses == []


# --- L9: cancellation ---------------------------------------------------------------------------------

def test_l9_cancel_before_send(server):
    handle = CancelHandle()
    handle.cancel()
    with pytest.raises(Cancelled) as info:
        run(server, rate_limit_retries=3, cancel=handle)
    assert info.value.sent is False and server.connections == 0


@ENTRIES
def test_l9_cancel_during_backoff_wakes_the_wait(server, entry):
    handle = CancelHandle()
    scheduled = threading.Event()
    server.reply(r429("20"), ok(entry, False))
    threading.Thread(target=lambda: (scheduled.wait(10), handle.cancel()), daemon=True).start()
    start = time.monotonic()
    with pytest.raises(Cancelled) as info:
        run(server, entry, rate_limit_retries=1, cancel=handle, on_retry=lambda e: scheduled.set())
    assert time.monotonic() - start < 10
    assert info.value.sent is True and len(server.requests) == 1
    assert handle._watches == set()


def test_l9_cancel_during_a_later_attempt(server, pauses):
    handle = CancelHandle()
    server.reply(r429(), Reply(hold_headers=True))
    threading.Thread(target=lambda: (server.hold_event(1).wait(10), handle.cancel()), daemon=True).start()
    with pytest.raises(Cancelled) as info:
        run(server, rate_limit_retries=1, cancel=handle)
    assert info.value.sent is True and len(server.requests) == 2


def test_l9_cancel_from_on_retry_stops_before_the_next_attempt(server):
    handle = CancelHandle()
    server.reply(r429(), canned("chat", False))
    with pytest.raises(Cancelled) as info:
        run(server, rate_limit_retries=2, cancel=handle, on_retry=lambda e: handle.cancel())
    assert info.value.sent is True and len(server.requests) == 1


# --- L10: on_retry errors propagate -----------------------------------------------------------------

def test_l10_on_retry_exception_propagates(server, pauses):
    class Boom(Exception):
        pass
    boom = Boom()

    def on_retry(event):
        raise boom
    server.reply(r429(), canned("chat", False))
    with pytest.raises(Boom) as info:
        run(server, rate_limit_retries=2, on_retry=on_retry)
    assert info.value is boom and len(server.requests) == 1 and pauses == []
    server.requests.clear()
    server.reply(r429(), canned("chat", False))
    own = BackendError("from on_retry", status=500)
    handle = CancelHandle()

    def cancel_and_raise(event):
        handle.cancel()
        raise own
    with pytest.raises(BackendError) as info:
        run(server, rate_limit_retries=2, on_retry=cancel_and_raise, cancel=handle)
    assert info.value is own and len(server.requests) == 1  # not converted to Cancelled


# --- L11: timeout per attempt -------------------------------------------------------------------------

@ENTRIES
def test_l11_each_attempt_gets_the_full_timeout(server, pauses, monkeypatch, entry):
    seen = []
    original = urllib.request.OpenerDirector.open

    def open(self, req, data=None, timeout=None):
        seen.append(timeout)
        return original(self, req, data, timeout)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", open)
    server.reply(r429(), r429(), ok(entry, False))
    run(server, entry, rate_limit_retries=2)
    assert seen == [900.0, 900.0, 900.0]  # the default, far beyond v1's 120-second limit, on every attempt
    seen.clear()
    server.reply(r429(), ok(entry, False))
    run(server, entry, rate_limit_retries=1, timeout=33.0)
    assert seen == [33.0, 33.0]


# --- L12: concurrency ---------------------------------------------------------------------------------

def test_l12_concurrent_calls_keep_their_own_retry_state(make_server, pauses):
    servers = [make_server() for _ in range(4)]
    for i, s in enumerate(servers):
        s.reply(*([r429()] * i), canned("chat", False))
    events = {i: [] for i in range(4)}
    errors = []

    def worker(i):
        try:
            run(servers[i], rate_limit_retries=3, on_retry=events[i].append)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    for i, s in enumerate(servers):
        assert len(s.requests) == i + 1
        assert [e.attempt for e in events[i]] == list(range(1, i + 1))


# --- L13: validation ----------------------------------------------------------------------------------

@pytest.mark.parametrize("kw", [dict(rate_limit_retries=4), dict(rate_limit_retries=-1), dict(rate_limit_retries=True),
                                dict(rate_limit_retries=None), dict(max_retry_wait=-0.1),
                                dict(max_retry_wait=float("inf")), dict(max_retry_wait="5"), dict(on_retry="x")])
@ENTRIES
def test_l13_invalid_retry_arguments(server, kw, entry):
    with pytest.raises(ConfigError):
        run(server, entry, **kw)
    assert server.connections == 0


def test_l13_retry_does_not_change_the_body(server, pauses):
    server.reply(canned("chat", False))
    run(server)
    run(server, rate_limit_retries=3, max_retry_wait=5.0, on_retry=NOOP)
    assert server.requests[0]["raw"] == server.requests[1]["raw"]
    assert "rate_limit_retries" not in json.loads(server.requests[1]["raw"])
