"""Cancellation through a CancelHandle: C1–C9 (spec v8 §12.10).

Order is set by hooks (events the fake or a patched standard-library function sets, or events a
test sets to release a hold), never by sleeps. Each bound is checked by joining the worker with a
timeout equal to the bound; every wait for a hook has a 30-second watchdog.
"""
import inspect
import socket
import threading
import time

import pytest

from conftest import Reply, ep, fx, json_reply, sse_reply
from modelspell import (AuthError, BackendError, CancelHandle, Cancelled, ConfigError, ConnectError, Endpoint,
                        RequestTimeout, UnsupportedSetting, complete)

BOUND = 1.0      # MS-2
WATCHDOG = 30.0  # a hook that never fires is a hang
BIG = "x" * (32 << 20)  # 32 MiB: cannot fit in the socket buffers of a peer that does not read
NOOP = lambda s: None  # noqa: E731


class Worker:
    """complete() in a thread; records the result or exception and when it ended."""

    def __init__(self, fn):
        self.out: dict = {}
        self.thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self.thread.start()

    def _run(self, fn):
        try:
            self.out["result"] = fn()
        except BaseException as exc:  # noqa: BLE001
            self.out["error"] = exc
        self.out["ended"] = time.monotonic()

    def ends_within(self, bound: float = BOUND):
        self.thread.join(bound)
        assert not self.thread.is_alive(), f"complete() still running {bound}s after cancel()"
        return self.out

    def cancelled(self, bound: float = BOUND) -> Cancelled:
        out = self.ends_within(bound)
        assert isinstance(out.get("error"), Cancelled), out
        return out["error"]


def wait(event, what="hook"):
    assert event.wait(WATCHDOG), f"{what} not reached within {WATCHDOG}s"


def check_partial(exc: Cancelled, text: str = "", sent=None):
    p = exc.partial
    assert p is not None and p.finish_reason == "cancelled" and p.usage is None and p.cost is None
    assert p.raw_usage is None and p.truncated is False and p.text == text
    if sent is not None:
        assert exc.sent is sent


def events(raw: bytes) -> list[bytes]:
    return [e + b"\n\n" for e in raw.split(b"\n\n") if e]


def compat(url: str) -> Endpoint:
    return Endpoint(kind="openai_compat", base_url=url)


# --- C1: every stage ends promptly, with the right `sent` (AT-1, AT-3) ---------------------------

def test_c1a_pre_send(server):
    h = CancelHandle()
    h.cancel()
    with pytest.raises(Cancelled) as e:
        complete(ep(server, "openai_compat"), "m", "x", cancel=h)
    check_partial(e.value, sent=False)
    assert server.connections == 0 and server.requests == []


def test_c1b_resolution(server, held_resolution):
    h = CancelHandle()
    w = Worker(lambda: complete(ep(server, "openai_compat"), "m", "x", cancel=h))
    wait(held_resolution.entered, "name resolution")
    h.cancel()
    held_resolution.release.set()  # the bound runs from here (MS-2's exception)
    check_partial(w.cancelled(), sent=False)
    assert server.connections == 0


def test_c1c_connect(held_connect, monkeypatch):
    initiated = threading.Event()
    original = socket.socket.connect_ex

    def connect_ex(self, address):  # hook: the non-blocking connect has been initiated
        code = original(self, address)
        initiated.set()
        return code

    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    h = CancelHandle()
    w = Worker(lambda: complete(compat(held_connect), "m", "x", cancel=h, timeout=60))
    wait(initiated, "connect")
    h.cancel()
    check_partial(w.cancelled(), sent=False)


def test_c1d_tls_handshake(silent_listener, trust_test_cert):
    lst = silent_listener()
    h = CancelHandle()
    w = Worker(lambda: complete(compat(lst.url("https")), "m", "x", cancel=h, timeout=60))
    wait(lst.accepted, "accept")
    deadline = time.monotonic() + WATCHDOG
    while not lst.has_bytes():  # the client hello has arrived: the handshake is under way
        assert time.monotonic() < deadline
        time.sleep(0.01)
    h.cancel()
    check_partial(w.cancelled(), sent=False)


@pytest.mark.parametrize("tls", [False, True], ids=["http", "https"])
def test_c1ef_sending(silent_listener, hooked_send, trust_test_cert, tls):
    lst = silent_listener(tls=tls, rcvbuf=65536)
    h = CancelHandle()
    w = Worker(lambda: complete(compat(lst.url("https" if tls else "http")), "m", BIG, cancel=h, timeout=60))
    wait(hooked_send, "request write")
    h.cancel()
    check_partial(w.cancelled(), sent=True)


@pytest.fixture(params=["http", "https"])
def any_server(request, make_server, tls_server):
    return tls_server if request.param == "https" else make_server()


def test_c1g_before_headers(any_server):
    any_server.reply(Reply(hold_headers=True))
    h = CancelHandle()
    w = Worker(lambda: complete(ep(any_server, "openai_compat"), "m", "x", cancel=h, timeout=60))
    wait(any_server.reached)
    h.cancel()
    check_partial(w.cancelled(), sent=True)
    assert len(any_server.requests) == 1
    assert any_server.client_closed.wait(5)


def test_c1h_before_first_fragment(any_server):
    any_server.reply(Reply(body=[], hold=True, headers={"Content-Type": "text/event-stream"}))
    h = CancelHandle()
    texts = []
    w = Worker(lambda: complete(ep(any_server, "openai_compat"), "m", "x", cancel=h, on_text=texts.append,
                                timeout=60))
    wait(any_server.reached)
    h.cancel()
    check_partial(w.cancelled(), sent=True, text="")
    assert texts == []


def _first_two_contents(route: str) -> tuple[list[bytes], list[str]]:
    """The route's text fixture cut after its first two content events, and those two fragments."""
    raw = fx({"chat": "chat/or_text_synthetic.sse", "responses": "responses/resp_text_synthetic.sse",
              "native": "native/native_text_synthetic.sse"}[route])
    marker = {"chat": b'"content": "', "responses": b"output_text.delta", "native": b"message.delta"}[route]
    out, n = [], 0
    for e in events(raw):
        out.append(e)
        if marker in e:
            n += 1
            if n == 2:
                break
    return out, ["Hél", "lo 🌍"]


@pytest.mark.parametrize("route,tls", [("chat", False), ("responses", False), ("native", False), ("chat", True)])
def test_c1i_mid_stream(make_server, tls_server, route, tls):
    server = tls_server if tls else make_server()
    chunks, expected = _first_two_contents(route)
    server.reply(Reply(body=chunks, hold=True, headers={"Content-Type": "text/event-stream"}))
    kind = {"chat": "openrouter", "responses": "lmstudio", "native": "lmstudio"}[route]
    texts, two = [], threading.Event()

    def on_text(s):
        texts.append(s)
        if len(texts) == 2:
            two.set()

    h = CancelHandle()
    w = Worker(lambda: complete(ep(server, kind), "m", "x", reasoning="off" if route == "native" else None,
                                on_text=on_text, cancel=h, timeout=60))
    wait(two, "two fragments")
    h.cancel()
    exc = w.cancelled()
    check_partial(exc, sent=True, text="".join(expected))
    assert texts == expected


def test_c1j_non_streaming_body(any_server):
    body = fx("chat/compat_text_synthetic.json")
    any_server.reply(Reply(body=[body[:len(body) // 2]], hold=True, headers={"Content-Type": "application/json"}))
    h = CancelHandle()
    w = Worker(lambda: complete(ep(any_server, "openai_compat"), "m", "x", cancel=h, timeout=60))
    wait(any_server.reached)
    h.cancel()
    check_partial(w.cancelled(), sent=True, text="")


def test_c1k_held_https_proxy_tunnel(held_proxy):
    proxy = held_proxy()
    h = CancelHandle()
    w = Worker(lambda: complete(compat("https://unreachable.invalid/v1"), "m", "x", cancel=h, timeout=60))
    wait(proxy.got_connect, "CONNECT")
    h.cancel()
    check_partial(w.cancelled(), sent=False)  # the CONNECT bytes do not count


def test_c1l_between_resolved_addresses(server, two_addresses):
    h = CancelHandle()
    two_addresses(server.httpd.server_address[1], h.cancel)
    server.reply(json_reply("chat/compat_text_synthetic.json"))
    w = Worker(lambda: complete(ep(server, "openai_compat"), "m", "x", cancel=h, timeout=60))
    check_partial(w.cancelled(WATCHDOG), sent=False)
    assert server.connections == 0


# --- C2: late cancel, reuse, concurrency (AT-2) --------------------------------------------------

def test_c2a_b_late_cancel_and_reuse(server):
    server.reply(sse_reply("chat/or_text_synthetic.sse"))
    h = CancelHandle()
    r = complete(ep(server, "openrouter"), "m", "x", on_text=NOOP, cancel=h)
    copy = type(r)(**{f: getattr(r, f) for f in r.__dataclass_fields__})
    h.cancel()
    assert r == copy and r.text == "Héllo 🌍 world"
    n = len(server.requests)
    with pytest.raises(Cancelled) as e:
        complete(ep(server, "openrouter"), "m", "x", on_text=NOOP, cancel=h)
    check_partial(e.value, sent=False)
    assert len(server.requests) == n


def test_c2c_concurrent_cancel(server):
    server.reply(Reply(hold_headers=True))
    h = CancelHandle()
    w = Worker(lambda: complete(ep(server, "openai_compat"), "m", "x", cancel=h, timeout=60))
    wait(server.reached)
    errors, go = [], threading.Barrier(16)

    def canceller():
        go.wait()
        try:
            h.cancel()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=canceller) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(WATCHDOG)
    assert errors == []
    check_partial(w.cancelled(), sent=True)


def test_c2d_shared_handle(make_server):
    a, b = make_server(), make_server()
    for s in (a, b):
        s.reply(Reply(hold_headers=True))
    h = CancelHandle()
    wa = Worker(lambda: complete(ep(a, "openai_compat"), "m", "x", cancel=h, timeout=60))
    wb = Worker(lambda: complete(ep(b, "openai_compat"), "m", "x", cancel=h, timeout=60))
    wait(a.reached)
    wait(b.reached)
    h.cancel()
    wa.cancelled()
    wb.cancelled()


def test_c2e_state_and_repr():
    h = CancelHandle()
    assert h.cancelled is False and repr(h) == "CancelHandle(cancelled=False)"
    h.cancel()
    h.cancel()
    assert h.cancelled is True and "cancelled=True" in repr(h)
    assert hash(h) == hash(h) and h != CancelHandle()


# --- C3: errors after cancellation become Cancelled; errors before stay as they are -------------

def test_c3a_b_reset_versus_cancel(make_server):
    chunks = events(fx("chat/or_text_synthetic.sse"))[:3]
    s = make_server().reply(Reply(body=chunks, reset=True, headers={"Content-Type": "text/event-stream"}))
    with pytest.raises(BackendError):
        complete(ep(s, "openrouter"), "m", "x", on_text=NOOP, cancel=CancelHandle())
    s = make_server().reply(Reply(body=chunks, hold=True, headers={"Content-Type": "text/event-stream"}))
    h = CancelHandle()
    w = Worker(lambda: complete(ep(s, "openrouter"), "m", "x", on_text=NOOP, cancel=h, timeout=60))
    wait(s.reached)
    h.cancel()
    w.cancelled()


def test_c3c_held_error_body(server):
    server.reply(Reply(status=429, body=[b'{"error": "rate'], hold=True))
    h = CancelHandle()
    w = Worker(lambda: complete(ep(server, "openai_compat"), "m", "x", cancel=h, timeout=60))
    wait(server.reached)
    h.cancel()
    exc = w.cancelled()
    assert exc.sent is True and not isinstance(exc, BackendError)


def test_c3d_timeout_without_cancel(server):
    server.reply(Reply(delay=1.0, body=fx("chat/compat_text_synthetic.json")))
    with pytest.raises(RequestTimeout):
        complete(ep(server, "openai_compat"), "m", "x", timeout=0.3, cancel=CancelHandle())


def test_c3e_refused_proxy_tunnel(held_proxy):
    held_proxy(status=403)
    with pytest.raises(ConnectError):  # as in 0.2.1
        complete(compat("https://unreachable.invalid/v1"), "m", "x", cancel=CancelHandle(), timeout=10)


def test_c3f_connection_refused():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    url = f"http://127.0.0.1:{port}/v1"
    with pytest.raises(ConnectError, match=url):
        complete(compat(url), "m", "x", cancel=CancelHandle())


# --- C4: callback cancellation unchanged (AT-4) --------------------------------------------------

def test_c4_callback_cancellation_carries_sent(server):
    raised = Cancelled()
    texts = []

    def on_text(s):
        texts.append(s)
        if len(texts) == 2:
            raise raised

    server.reply(Reply(body=events(fx("chat/or_text_synthetic.sse"))[:4], hold=True))
    with pytest.raises(Cancelled) as e:
        complete(ep(server, "openrouter"), "m", "x", on_text=on_text)
    assert e.value is raised and e.value.sent is True
    check_partial(e.value, text="Héllo 🌍")
    assert server.client_closed.wait(5)


# --- C5: a callback that cancels its own call (MS-3) ---------------------------------------------

def _chat_event(content=None, reasoning=None) -> bytes:
    import json
    delta = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning"] = reasoning
    return ("data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n").encode()


def test_c5_callback_cancels_own_call(server):
    server.reply(Reply(body=[b"".join(_chat_event(f"t{i}") for i in range(5))], hold=True))
    h, texts = CancelHandle(), []

    def on_text(s):
        texts.append(s)
        if len(texts) == 2:
            h.cancel()

    with pytest.raises(Cancelled) as e:
        complete(ep(server, "openrouter"), "m", "x", on_text=on_text, cancel=h, timeout=60)
    assert texts == ["t0", "t1"] and e.value.sent is True
    check_partial(e.value, text="t0t1")


def test_c5_reasoning_channel_only(server):
    flush = b"".join([_chat_event(reasoning="r1"), _chat_event("t1"), _chat_event(reasoning="r2"),
                      _chat_event("t2"), _chat_event(reasoning="r3")])
    server.reply(Reply(body=[flush], hold=True))
    h, reasons = CancelHandle(), []

    def on_reasoning(s):
        reasons.append(s)
        if len(reasons) == 2:
            h.cancel()

    with pytest.raises(Cancelled) as e:
        complete(ep(server, "openrouter"), "m", "x", on_reasoning=on_reasoning, cancel=h, timeout=60)
    assert reasons == ["r1", "r2"] and e.value.partial.reasoning == "r1r2"
    assert e.value.partial.text == "t1" and e.value.sent is True


# --- C6: commit point and callback exceptions (MS-4, MS-5) ---------------------------------------

def test_c6a_final_only_fallback_after_commit(server):
    server.reply(sse_reply("responses/resp_final_only_synthetic.sse"))
    h, texts = CancelHandle(), []

    def on_text(s):
        texts.append(s)
        h.cancel()

    r = complete(ep(server, "lmstudio"), "m", "x", on_text=on_text, on_reasoning=NOOP, cancel=h)
    assert r.text == "ans" and r.reasoning == "Let me think." and texts == ["ans"]


def test_c6b_cancel_after_return(server):
    server.reply(Reply(body=[fx("chat/or_text_synthetic.sse")], hold=True))
    h = CancelHandle()
    r = complete(ep(server, "openrouter"), "m", "x", on_text=NOOP, cancel=h, timeout=10)
    assert server.client_closed.wait(5)  # closed by the call itself (S13)
    h.cancel()
    assert r.text == "Héllo 🌍 world" and r.finish_reason == "stop"


FAILED = (b'data: {"type":"response.output_text.delta","delta":"a"}\n\n'
          b'data: {"type":"response.failed","response":{"status":"failed","error":{"message":"x"}}}\n\n')


def test_c6c_failed_terminal_after_cancel(make_server):
    s = make_server().reply(Reply(body=[FAILED], hold=True))
    h = CancelHandle()
    with pytest.raises(Cancelled):
        complete(ep(s, "lmstudio"), "m", "x", on_text=lambda t: h.cancel(), cancel=h, timeout=10)
    s = make_server().reply(Reply(body=[FAILED], hold=True))
    with pytest.raises(BackendError):
        complete(ep(s, "lmstudio"), "m", "x", on_text=NOOP, cancel=CancelHandle(), timeout=10)


@pytest.mark.parametrize("err", [ValueError("boom"), ConfigError("x")], ids=["ValueError", "ConfigError"])
def test_c6de_callback_exception_after_cancel(server, err):
    server.reply(Reply(body=events(fx("chat/or_text_synthetic.sse"))[:3], hold=True))
    h = CancelHandle()

    def on_text(s):
        h.cancel()
        raise err

    with pytest.raises(type(err)) as e:
        complete(ep(server, "openrouter"), "m", "x", on_text=on_text, cancel=h, timeout=10)
    assert e.value is err


# --- C7: validation precedes cancellation (M30-7) ------------------------------------------------

def test_c7_validation_precedes_cancellation(server):
    h = CancelHandle()
    h.cancel()
    with pytest.raises(ConfigError):
        complete(ep(server, "openai_compat"), "m", "x", temperature=-1, cancel=h)
    with pytest.raises(UnsupportedSetting):
        complete(ep(server, "lmstudio"), "m", "x", top_k=5, cancel=h)
    with pytest.raises(AuthError):
        complete(Endpoint(kind="openrouter", base_url=server.url + "/v1"), "m", "x", cancel=h)
    assert server.connections == 0
    with pytest.raises(ConfigError, match="cancel"):
        complete(ep(server, "openai_compat"), "m", "x", cancel="x")
    assert server.connections == 0


# --- C8: additive API (AT-6) ---------------------------------------------------------------------

V021 = [("endpoint", "POSITIONAL_OR_KEYWORD", inspect.Parameter.empty),
        ("model", "POSITIONAL_OR_KEYWORD", inspect.Parameter.empty),
        ("input", "POSITIONAL_OR_KEYWORD", inspect.Parameter.empty)] + \
    [(n, "KEYWORD_ONLY", None) for n in ("reasoning", "max_tokens", "temperature", "top_p", "top_k", "min_p",
                                         "repetition_penalty", "presence_penalty", "frequency_penalty", "seed",
                                         "stop", "provider", "extra")] + \
    [("timeout", "KEYWORD_ONLY", 900.0), ("on_text", "KEYWORD_ONLY", None), ("on_reasoning", "KEYWORD_ONLY", None)]


V040 = [("rate_limit_retries", "KEYWORD_ONLY", 0), ("max_retry_wait", "KEYWORD_ONLY", 30.0),
        ("on_retry", "KEYWORD_ONLY", None)]  # spec v9 §6.11: additive again in 0.4.0


def test_c8_additive_signature():
    params = list(inspect.signature(complete).parameters.values())
    assert [(p.name, p.kind.name, p.default) for p in params[:len(V021)]] == V021
    cancel = params[len(V021)]
    assert (cancel.name, cancel.kind, cancel.default) == ("cancel", inspect.Parameter.KEYWORD_ONLY, None)
    assert [(p.name, p.kind.name, p.default) for p in params[len(V021) + 1:]] == V040
    assert Cancelled().sent is None and Cancelled().partial is None


# --- C9: admission from another thread (MS-3 as clarified) ---------------------------------------

def test_c9_held_callback(server):
    server.reply(Reply(body=[b"".join(_chat_event(f"t{i}") for i in range(5))], hold=True))
    h, texts = CancelHandle(), []
    entered, release = threading.Event(), threading.Event()

    def on_text(s):
        texts.append(s)
        if len(texts) == 1:
            entered.set()
            release.wait(WATCHDOG)

    w = Worker(lambda: complete(ep(server, "openrouter"), "m", "x", on_text=on_text, cancel=h, timeout=60))
    wait(entered, "first callback")
    t0 = time.monotonic()
    h.cancel()
    assert time.monotonic() - t0 < 0.5  # cancel() never waits for caller code
    assert w.thread.is_alive() and not release.is_set()
    release.set()
    exc = w.cancelled()
    assert texts == ["t0"]
    check_partial(exc, sent=True, text="t0")
