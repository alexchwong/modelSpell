"""Streaming: S1–S13 (spec §12.5)."""
import random
import threading

import pytest

from conftest import FIXTURES, Reply, ep, fx, json_reply, sse_reply
from expected import EXPECTED, check
from modelspell import BackendError, Cancelled, RequestTimeout, ResponseFormatError, complete

KIND = {"chat": None, "responses": "lmstudio", "native": "lmstudio"}
SUCCESS = [("chat", "or_text_synthetic"), ("chat", "or_reasoning_synthetic"), ("chat", "or_reasoning_field_synthetic"),
           ("chat", "compat_text_synthetic"), ("responses", "resp_text_synthetic"),
           ("responses", "resp_reasoning_synthetic"), ("responses", "resp_final_only_synthetic"),
           ("native", "native_text_synthetic"), ("native", "native_reasoning_synthetic"),
           ("native", "native_cap_synthetic")]
PAIRS = [k for k in SUCCESS if (FIXTURES / k[0] / f"{k[1]}.json").exists()]


def kind_of(route, stem):
    return KIND[route] or ("openai_compat" if stem.startswith("compat") else "openrouter")


def stream(server, route, stem, chunks=None, reply=None, on_text=None, on_reasoning=None, **kw):
    server.reply(reply or sse_reply(f"{route}/{stem}.sse", chunks))
    texts, reasonings = [], []
    r = complete(ep(server, kind_of(route, stem)), "m", "x", reasoning="off" if route == "native" else None,
                 on_text=on_text or texts.append, on_reasoning=on_reasoning or reasonings.append, **kw)
    return r, texts, reasonings


def events(raw: bytes) -> list[bytes]:
    return [e + b"\n\n" for e in raw.split(b"\n\n") if e]


@pytest.mark.parametrize("route,stem", SUCCESS)
def test_s1_chunk_boundary_invariance(make_server, route, stem):
    raw = fx(f"{route}/{stem}.sse")
    rng = random.Random(stem)
    cuts = sorted(rng.sample(range(1, len(raw)), 20))
    splits = [[raw], [raw[i:i + 1] for i in range(len(raw))],
              [raw[a:b] for a, b in zip([0] + cuts, cuts + [len(raw)])]]
    results = [stream(make_server(), route, stem, chunks)[0] for chunks in splits]
    assert results[0] == results[1] == results[2]
    if (route, stem) in EXPECTED:
        kind, fields, details = EXPECTED[(route, stem)]
        check(results[0], fields, details)


@pytest.mark.parametrize("route,stem", PAIRS)
def test_s2_stream_matches_nonstream(make_server, route, stem):
    s = stream(make_server(), route, stem)[0]
    srv = make_server().reply(json_reply(f"{route}/{stem}.json"))
    n = complete(ep(srv, kind_of(route, stem)), "m", "x", reasoning="off" if route == "native" else None)
    for f in ("text", "reasoning", "usage", "finish_reason", "truncated"):
        assert getattr(s, f) == getattr(n, f), f


@pytest.mark.parametrize("route,stem", SUCCESS)
def test_s3_callback_invariants(server, route, stem):
    r, texts, reasonings = stream(server, route, stem)
    assert "".join(texts) == r.text and "".join(reasonings) == (r.reasoning or "")
    assert all(texts) and all(reasonings)
    if stem == "or_text_synthetic":
        assert texts == ["Hél", "lo 🌍", " world"]


def test_s3_responses_final_only_called_once(server):  # S9
    r, texts, reasonings = stream(server, "responses", "resp_final_only_synthetic")
    assert texts == ["ans"] and reasonings == ["Let me think."]


MALFORMED = {
    "chat": b'data: {"choices":[{"delta":{"content":"a"}}]}\n\ndata: {oops\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
    "responses": b'data: {"type":"response.output_text.delta","delta":"a"}\n\ndata: [1,2]\n\ndata: {"type":"response.completed","response":{"status":"completed"}}\n\n',
    "native": b'data: {"type":"message.delta","content":"a"}\n\ndata: "str"\n\ndata: {"type":"chat.end","result":{}}\n\n',
}


@pytest.mark.parametrize("route", sorted(MALFORMED))
def test_s4_malformed_data_raises_and_closes(server, route):
    with pytest.raises(ResponseFormatError) as e:
        stream(server, route, "x", reply=Reply(body=[MALFORMED[route]], hold=True))
    assert e.value.status == 200
    assert server.client_closed.wait(5)


def test_s4_fixture_and_split_object(server):
    with pytest.raises(ResponseFormatError):
        stream(server, "chat", "or_malformed_synthetic")
    split = b'data: {"choices":[{"delta":\ndata: {"content":"a"}}]}\n\ndata: [DONE]\n\n'
    with pytest.raises(ResponseFormatError):
        stream(server, "chat", "x", reply=Reply(body=[split]))


def test_s4_ignored_lines(server):
    raw = (b": keep-alive\n\nevent: x\nid: 1\nretry: 5\n\n\n"
           b'data: {"choices":[{"delta":{"content":"a"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
           b"data: {never read\n\n")
    assert stream(server, "chat", "x", reply=Reply(body=[raw]))[0].text == "a"


def test_s5_final_usage_chunk(server):
    r = stream(server, "chat", "or_text_synthetic")[0]
    assert r.usage.total_tokens == 16 and r.cost == 0.00012


def test_s6_midstream_error(server):
    raw = fx("chat/or_midstream_error_synthetic.sse").replace(b"data: [DONE]\n\n", b"")
    with pytest.raises(BackendError) as e:
        stream(server, "chat", "x", reply=Reply(body=[raw], hold=True))
    assert e.value.status == 200 and "upstream provider failed" in e.value.body
    assert server.client_closed.wait(5)
    with pytest.raises(BackendError):
        stream(server, "chat", "or_error_only_synthetic")
    with pytest.raises(BackendError):
        stream(server, "responses", "x", reply=Reply(body=[b'data: {"type":"error","message":"bad"}\n\n']))


@pytest.mark.parametrize("route,stem", [("chat", "or_no_completion_synthetic"),
                                        ("responses", "resp_no_completion_synthetic"),
                                        ("native", "native_no_end_synthetic")])
def test_s7_stream_ended_early(server, route, stem):
    with pytest.raises(BackendError, match="stream ended before completion"):
        stream(server, route, stem)


@pytest.mark.parametrize("route", ["chat", "responses", "native"])
def test_s8_no_events_no_fallback(server, route):
    with pytest.raises(BackendError):
        stream(server, route, "x", reply=Reply(body=[b": nothing here\n\n"]))
    assert len(server.requests) == 1


def test_s10_native_error_then_chat_end(server):
    texts = []
    with pytest.raises(BackendError, match="model crashed"):
        stream(server, "native", "native_error_synthetic", on_text=texts.append)
    assert texts == ["part"]


def test_s11_cancellation(server):
    texts = []

    def on_text(s):
        texts.append(s)
        if len(texts) == 2:
            raise Cancelled()

    with pytest.raises(Cancelled) as e:
        stream(server, "chat", "x", on_text=on_text,
               reply=Reply(body=events(fx("chat/or_text_synthetic.sse"))[:4], hold=True))
    assert e.value.partial.text == "Héllo 🌍" and e.value.partial.finish_reason == "cancelled"
    assert e.value.partial.usage is None and e.value.partial.cost is None and not e.value.partial.truncated
    assert server.client_closed.wait(5)


def test_s12_callback_exception_propagates_unwrapped(server):
    err = ValueError("boom")

    def on_text(s):
        raise err

    with pytest.raises(ValueError) as e:
        stream(server, "chat", "x", on_text=on_text,
               reply=Reply(body=events(fx("chat/or_text_synthetic.sse"))[:3], hold=True))
    assert e.value is err
    assert server.client_closed.wait(5)


def _run_with_watchdog(fn):
    out = {}

    def target():
        try:
            out["result"] = fn()
        except BaseException as exc:  # noqa: BLE001
            out["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(30)
    assert not t.is_alive(), "complete() hung"
    return out


TERMINAL = [
    ("responses", "resp_text_synthetic", None),
    ("responses", "x", fx("responses/resp_incomplete_synthetic.json").replace(b"\n", b"")),
    ("native", "native_text_synthetic", None),
    ("native", "native_error_synthetic", None),
    ("chat", "or_text_synthetic", None),
]


@pytest.mark.parametrize("route,stem,incomplete", TERMINAL)
def test_s13_terminal_event_ends_read(server, route, stem, incomplete):
    raw = (b'data: {"type":"response.incomplete","response":' + incomplete + b"}\n\n") if incomplete \
        else fx(f"{route}/{stem}.sse")
    out = _run_with_watchdog(lambda: stream(server, route, stem, timeout=2, reply=Reply(body=[raw], hold=True))[0])
    if stem == "native_error_synthetic":
        assert isinstance(out.get("error"), BackendError) and not isinstance(out["error"], RequestTimeout)
    else:
        assert "error" not in out, out.get("error")
    assert server.client_closed.wait(30)


@pytest.mark.parametrize("route,raw", [
    ("responses", b'data: {"type":"response.output_text.delta","delta":"a"}\n\ndata: {"type":"response.completed"}\n\n'),
    ("native", b'data: {"type":"message.delta","content":"a"}\n\ndata: {"type":"chat.end"}\n\n'),
])
def test_s13_terminal_without_payload_keeps_reading(server, route, raw):
    out = _run_with_watchdog(lambda: stream(server, route, "x", timeout=1, reply=Reply(body=[raw], hold=True)))
    assert isinstance(out.get("error"), RequestTimeout)
