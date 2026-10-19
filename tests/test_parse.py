"""Non-streaming response parsing: P1–P9 (spec §12.4)."""
import json

import pytest

from conftest import FIXTURES, Reply, ep, fx_json, json_reply, sse_reply
from expected import EXPECTED, check
from modelspell import BackendError, ResponseFormatError, Usage, complete
from modelspell._reasoning import _reasoning_fragment
from modelspell._usage import parse_native_usage, parse_usage

REASONING = {"chat": None, "responses": None, "native": "off"}
JSON_CASES = [k for k in EXPECTED if (FIXTURES / k[0] / f"{k[1]}.json").exists()]


def call(server, kind, reply, reasoning=None, **kw):
    server.reply(reply)
    return complete(ep(server, kind), "m", "x", reasoning=reasoning, **kw)


def body(obj) -> Reply:
    return Reply(body=json.dumps(obj).encode(), headers={"Content-Type": "application/json"})


@pytest.mark.parametrize("route,stem", JSON_CASES)
def test_p1_fixture_results(server, route, stem):
    kind, fields, details = EXPECTED[(route, stem)]
    rel = f"{route}/{stem}.json"
    result = call(server, kind, json_reply(rel), REASONING[route])
    check(result, fields, details)
    doc = fx_json(rel)
    assert result.raw_usage == doc.get("stats" if route == "native" else "usage")


def test_p2_empty_and_truncated_do_not_raise(server):
    doc = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "t"}]},
                        "finish_reason": "length"}]}
    r = call(server, "openai_compat", body(doc))
    assert r.text == "" and r.truncated is True
    r = call(server, "lmstudio", body({"status": "completed", "output": []}))
    assert r.text == "" and r.finish_reason == "stop"


def test_p3_cost_only_on_openrouter(server):
    with_cost = {"choices": [{"message": {"content": "a"}}], "usage": {"prompt_tokens": 1, "cost": 0.25}}
    assert call(server, "openrouter", body(with_cost)).cost == 0.25
    no_cost = {"choices": [{"message": {"content": "a"}}], "usage": {"prompt_tokens": 1}}
    assert call(server, "openrouter", body(no_cost)).cost is None
    assert call(server, "openai_compat", body(with_cost)).cost is None
    resp = {"status": "completed", "output": [], "usage": {"input_tokens": 1, "cost": 0.25}}
    assert call(server, "lmstudio", body(resp)).cost is None


def test_p4_usage_normalisation():
    assert parse_usage({"prompt_tokens": 3, "completion_tokens": 4}) == Usage(3, 4, 7)
    assert parse_usage({"input_tokens": 3, "output_tokens": 4, "total_tokens": 9,
                        "output_tokens_details": {"reasoning_tokens": 2},
                        "input_tokens_details": {"cached_tokens": 1}}) == Usage(3, 4, 9, 2, 1)
    assert parse_usage({"prompt_tokens": -1, "completion_tokens": True, "total_tokens": "9"}) is None
    assert parse_usage({"prompt_tokens": 1, "completion_tokens": "x"}) == Usage(1, None, None)
    assert parse_usage(None) is None and parse_usage({}) is None
    assert parse_native_usage({"input_tokens": 2, "total_output_tokens": 3, "reasoning_output_tokens": 1}) == Usage(2, 3, 5, 1)
    assert parse_native_usage({"input_tokens": False}) is None


def test_p5_reasoning_alias_selection():
    mirrored = {"reasoning": "same", "reasoning_details": [{"type": "reasoning.text", "text": "same"}]}
    assert _reasoning_fragment(mirrored) == "same"
    assert _reasoning_fragment({"reasoning_details": [{"type": "reasoning.encrypted", "text": "secret"}],
                                "reasoning_content": "plain"}) == "plain"
    assert _reasoning_fragment({"reasoning_details": [{"type": "reasoning.summary", "summary": "sum"}]}) == "sum"
    assert _reasoning_fragment({"reasoning": "", "thinking": [{"text": "t"}]}) == "t"


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_p6_responses_failed(server, status):
    with pytest.raises(BackendError):
        call(server, "lmstudio", body({"status": status, "output": [], "error": {"message": "boom"}}))


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("kw,stem,truncated", [
    (dict(max_tokens=1500), "native_cap_synthetic", True),
    (dict(max_tokens=1501), "native_cap_synthetic", False),
    (dict(max_tokens=10), "native_no_stats_synthetic", False),
    (dict(), "native_cap_synthetic", False),
    (dict(max_tokens=1000, extra={"max_output_tokens": 2000}), "native_cap_synthetic", False),
    (dict(extra={"max_output_tokens": 1500}), "native_cap_synthetic", True),
    (dict(extra={"max_output_tokens": True}), "native_cap_synthetic", False),
])
def test_p7_native_truncation_heuristic(server, stream, kw, stem, truncated):
    if stream and stem == "native_no_stats_synthetic":
        reply = sse_reply("", [b'data: {"type":"message.delta","content":"ans"}\n\n',
                               b'data: {"type":"chat.end","result":{"output":[{"type":"message","content":"ans"}]}}\n\n'])
    else:
        reply = sse_reply(f"native/{stem}.sse") if stream else json_reply(f"native/{stem}.json")
    r = call(server, "lmstudio", reply, "off", on_text=(lambda s: None) if stream else None, **kw)
    assert r.truncated is truncated and r.finish_reason is None


@pytest.mark.parametrize("kind,raw", [
    ("openai_compat", b"not json"), ("openai_compat", b"[1, 2]"), ("openai_compat", b'{"id": "x"}'),
    ("openai_compat", b'{"choices": [{"message": {"content": 5}}]}'), ("openai_compat", b'{"choices": []}'),
    ("lmstudio", b"null"),
])
def test_p8_malformed_bodies(server, kind, raw):
    with pytest.raises(ResponseFormatError):
        call(server, kind, Reply(body=raw))


def test_p9_native_without_response_id(server):
    assert call(server, "lmstudio", json_reply("native/native_text_synthetic.json"), "off").response_id is None
