"""Client-executed function calling: T1–T16 (spec v9 §12.11, §21)."""
import copy
import json
import pickle
from decimal import Decimal

import pytest

from conftest import KEY, Reply, ep, fx, json_reply, sse_reply
from modelspell import (AuthError, BackendError, CancelHandle, Cancelled, ConfigError, Endpoint, Message, ModelInfo,
                        ResponseFormatError, ToolCall, ToolDefinition, ToolOutput, ToolTurn, UnsupportedSetting,
                        check_tool_request, complete, complete_with_tools)

SCHEMA = {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"],
          "additionalProperties": False}
READ = ToolDefinition("read_label", "Read the fixture label.", SCHEMA)
NOTE = ToolDefinition("write_note", "Write a fixture note.", {"type": "object", "properties": {
    "text": {"type": "string"}}, "required": ["text"], "additionalProperties": False})
TOOLS = [READ, NOTE]
PIN = {"order": ["deepinfra/turbo"], "allow_fallbacks": False}
HISTORY = [Message("user", "Read the labels for items 007 and 008.")]
NOOP = lambda s: None  # noqa: E731

ROUTES = {  # kind -> (fixture stem, route dir)
    "openrouter": ("chat", "or_tool_call_synthetic"),
    "openai_compat": ("chat", "compat_tool_call_synthetic"),
    "lmstudio": ("responses", "resp_tool_call_synthetic"),
}
TEXT_REPLY = {
    "chat": {False: "chat/or_text_synthetic.json", True: "chat/or_text_synthetic.sse"},
    "responses": {False: "responses/resp_text_synthetic.json", True: "responses/resp_text_synthetic.sse"},
}


def pin(kind):
    return {"provider": PIN} if kind == "openrouter" else {}


def fixture_reply(kind, stream):
    d, stem = ROUTES[kind]
    return sse_reply(f"{d}/{stem}.sse") if stream else json_reply(f"{d}/{stem}.json")


def text_reply(kind, stream):
    rel = TEXT_REPLY[ROUTES[kind][0]][stream]
    return sse_reply(rel) if stream else json_reply(rel)


def call(server, kind, input=HISTORY, *, stream=False, tools=TOOLS, endpoint=None, **kw):
    kw = {**pin(kind), **kw}
    if stream:
        kw.setdefault("on_text", NOOP)
    return complete_with_tools(endpoint or ep(server, kind), "m", input, tools=tools, **kw)


def chat_doc(message, finish="tool_calls", **extra):
    return {"id": "gen-x", "choices": [{"index": 0, "finish_reason": finish, "message": message}], **extra}


def json_doc(doc) -> Reply:
    return Reply(body=json.dumps(doc).encode(), headers={"Content-Type": "application/json"})


def sse_docs(docs, done=True) -> Reply:
    body = "".join(f"data: {json.dumps(d)}\n\n" for d in docs) + ("data: [DONE]\n\n" if done else "")
    return Reply(body=[body.encode()], headers={"Content-Type": "text/event-stream"})


def tc(i, id, name, args):
    return {"id": id, "type": "function", "function": {"name": name, "arguments": args}}


def chunk(delta=None, finish=None, **extra):
    choice = {"index": 0, "delta": delta or {}}
    if finish is not None:
        choice["finish_reason"] = finish
    return {"id": "gen-x", "choices": [choice], **extra}


def resp_doc(output, status="completed", **extra):
    return {"id": "resp_x", "model": "m", "status": status, "output": output, **extra}


def fc(call_id, name="read_label", args='{"key":"007"}', **extra):
    return {"type": "function_call", "id": f"fc_{call_id}", "call_id": call_id, "name": name, "arguments": args,
            **extra}


# --- T1: preflight (check_tool_request) and the same checks before sending -----------------------

GOOD = [("lmstudio", {}), ("lmstudio", {"reasoning": "high"}), ("openai_compat", {}),
        ("openrouter", {"provider": PIN}), ("openrouter", {"provider": {"order": ["deepinfra"], "allow_fallbacks": False,
                                                                        "only": ["deepinfra"], "ignore": ["together"],
                                                                        "require_parameters": True}})]


@pytest.mark.parametrize("kind,kw", GOOD)
def test_t1_preflight_accepts(kind, kw):
    assert check_tool_request(kind, tools=TOOLS, **kw) is None


BAD = [
    ("lmstudio", dict(reasoning="off"), UnsupportedSetting),
    ("openai_compat", dict(reasoning="low"), UnsupportedSetting),
    ("lmstudio", dict(top_k=4), UnsupportedSetting),
    ("openrouter", dict(provider=PIN, temperature=3), ConfigError),
    ("lmstudio", dict(provider=PIN), UnsupportedSetting),
    ("bogus", dict(), ConfigError),
    ("lmstudio", dict(tools=[]), ConfigError),
    ("lmstudio", dict(tools=READ), ConfigError),
    ("lmstudio", dict(tools=[READ, READ]), ConfigError),
    ("lmstudio", dict(tools=[{"name": "x"}]), ConfigError),
    ("openrouter", dict(), ConfigError),
    ("openrouter", dict(provider={"order": ["a", "b"], "allow_fallbacks": False}), ConfigError),
    ("openrouter", dict(provider={"order": [""], "allow_fallbacks": False}), ConfigError),
    ("openrouter", dict(provider={"order": "deepinfra", "allow_fallbacks": False}), ConfigError),
    ("openrouter", dict(provider={"order": ["deepinfra"]}), ConfigError),
    ("openrouter", dict(provider={"order": ["deepinfra"], "allow_fallbacks": 0}), ConfigError),
    ("openrouter", dict(provider={**PIN, "only": ["deepinfra/turbo", "x"]}), ConfigError),
    ("openrouter", dict(provider={**PIN, "only": ["deepinfra"]}), ConfigError),
    ("openrouter", dict(provider={**PIN, "ignore": ["deepinfra/turbo"]}), ConfigError),
    ("openrouter", dict(provider={**PIN, "ignore": ["DeepInfra"]}), ConfigError),
    ("openrouter", dict(provider={**PIN, "require_parameters": False}), ConfigError),
    ("openrouter", dict(provider={**PIN, "max_price": {"prompt": float("inf")}}), ConfigError),
    ("lmstudio", dict(rate_limit_retries=4), ConfigError),
    ("lmstudio", dict(rate_limit_retries=-1), ConfigError),
    ("lmstudio", dict(rate_limit_retries=True), ConfigError),
    ("lmstudio", dict(rate_limit_retries=1.0), ConfigError),
    ("lmstudio", dict(max_retry_wait=-1), ConfigError),
    ("lmstudio", dict(max_retry_wait=float("inf")), ConfigError),
    ("lmstudio", dict(max_retry_wait=float("nan")), ConfigError),
    ("lmstudio", dict(max_retry_wait=True), ConfigError),
]


@pytest.mark.parametrize("kind,kw,exc", BAD)
def test_t1_preflight_and_completion_refuse_with_zero_posts(server, kind, kw, exc):
    kw = dict(kw)
    tools = kw.pop("tools", TOOLS)
    with pytest.raises(exc) as pre:
        check_tool_request(kind, tools=tools, **kw)
    if kind == "bogus":
        return
    with pytest.raises(exc) as full:
        complete_with_tools(ep(server, kind), "m", "x", tools=tools, **kw)
    assert str(pre.value) == str(full.value)
    assert server.requests == [] and server.connections == 0


def test_t1_unknown_keywords_raise_type_error(server):
    with pytest.raises(TypeError):
        check_tool_request("lmstudio", tools=TOOLS, extra={"x": 1})
    with pytest.raises(TypeError):
        check_tool_request("lmstudio", tools=TOOLS, temprature=0.1)
    with pytest.raises(TypeError):
        complete_with_tools(ep(server, "lmstudio"), "m", "x", tools=TOOLS, extra={"x": 1})
    assert server.connections == 0


def test_t1_preflight_needs_no_endpoint_key_or_network(monkeypatch):
    import socket

    def boom(*a, **k):
        raise AssertionError("network used")
    monkeypatch.setattr(socket, "socket", boom)
    check_tool_request("openrouter", tools=TOOLS, provider=PIN, temperature=0.2, max_tokens=50)


def test_t1_call_specific_checks_before_sending(server):
    for kw in (dict(timeout=0), dict(on_text="x"), dict(cancel="x"), dict(on_retry=1)):
        with pytest.raises(ConfigError):
            call(server, "lmstudio", **kw)
    with pytest.raises(ConfigError):
        complete_with_tools("not an endpoint", "m", "x", tools=TOOLS)
    with pytest.raises(AuthError):
        complete_with_tools(Endpoint(kind="openrouter", base_url=server.url + "/v1"), "m", "x", tools=TOOLS,
                            provider=PIN)
    assert server.connections == 0


# --- T2: ToolDefinition ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["", "a" * 65, "a b", "é", "x\n", "x.y", 5, None])
def test_t2_definition_name_rule(name):
    with pytest.raises(ConfigError):
        ToolDefinition(name, "d", SCHEMA)


def test_t2_definition_accepts_names_and_values():
    ToolDefinition("a" * 64, "", {})
    ToolDefinition("A-z_09", "d", {"type": "object", "x": [1, 2.5, Decimal("1.25"), None, True, "s", (1,), {"k": []}]})


def _cycle():
    d = {"type": "object"}
    d["self"] = d
    return d


@pytest.mark.parametrize("params", [
    [], "schema", {"type": "string"}, {1: "x"}, {"x": float("inf")}, {"x": float("nan")}, {"x": Decimal("NaN")},
    {"x": Decimal("Infinity")}, {"x": {1, 2}}, {"x": b"bytes"}, {"x": object()}, _cycle(),
])
def test_t2_definition_rejects_malformed_parameters(params):
    with pytest.raises(ConfigError):
        ToolDefinition("t", "d", params)
    with pytest.raises(ConfigError):
        ToolDefinition("t", 5, SCHEMA)


def test_t2_definition_is_a_deep_snapshot():
    src = {"type": "object", "properties": {"key": {"enum": ["a"]}}}
    d = ToolDefinition("t", "d", src)
    src["properties"]["key"]["enum"].append("b")
    src["type"] = "string"
    assert d.parameters["type"] == "object" and d.parameters["properties"]["key"]["enum"] == ("a",)
    with pytest.raises(TypeError):
        d.parameters["type"] = "x"
    with pytest.raises(AttributeError):
        d.parameters.x = 1
    assert hash(d) == hash(ToolDefinition("t", "d", {"type": "object", "properties": {"key": {"enum": ["a"]}}}))
    assert copy.deepcopy(d) == d


# --- T3: ToolOutput and ToolCall values -----------------------------------------------------------

@pytest.mark.parametrize("args", [("", "x"), ("id", ""), (None, "x"), ("id", 5)])
def test_t3_tool_output_requires_non_empty_text(args):
    with pytest.raises(ConfigError):
        ToolOutput(*args)


def test_t3_tool_call_value():
    assert ToolCall("a", "b", "{bad") == ToolCall("a", "b", "{bad")
    with pytest.raises(ConfigError):
        ToolCall("a", "b", {"x": 1})


# --- T4: ToolTurn is created only by the parser ---------------------------------------------------

def test_t4_tool_turn_construction_and_immutability(server):
    with pytest.raises(TypeError):
        ToolTurn()
    server.reply(fixture_reply("lmstudio", False))
    turn = call(server, "lmstudio")
    with pytest.raises(AttributeError):
        turn.clean = False
    with pytest.raises(AttributeError):
        turn._replay = None
    with pytest.raises(TypeError):
        pickle.dumps(turn)
    with pytest.raises(TypeError):
        turn.replay[0]["type"] = "x"
    with pytest.raises(TypeError):
        turn.route_identity["model"] = "y"
    for dup in (copy.copy(turn), copy.deepcopy(turn)):
        assert dup.clean and dup.tool_calls == turn.tool_calls and dup.replay == turn.replay


# --- T5: call and continue (acceptance 1) ---------------------------------------------------------

EXPECTED_CALLS = {
    "openrouter": (ToolCall("call_a", "read_label", '{"key": "007"}'),
                   ToolCall("call_b", "read_label", '{"key": "008", "n": 9007199254740993}')),
    "openai_compat": (ToolCall("call_x", "read_label", '{"key":"007"}'),),
    "lmstudio": (ToolCall("call_r1", "read_label", '{"key":"007"}'),),
}


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize("kind", sorted(ROUTES))
def test_t5_call_and_continue(server, kind, stream):
    texts = []
    server.reply(fixture_reply(kind, stream), text_reply(kind, stream))
    kw = {"on_text": texts.append} if stream else {}
    turn = call(server, kind, stream=stream, **kw)
    assert turn.clean and turn.tool_calls == EXPECTED_CALLS[kind]
    first = server.requests[0]["body"]
    # outputs supplied in reverse order are serialised in the originating call order
    outputs = [ToolOutput(c.id, json.dumps({"label": c.id})) for c in reversed(turn.tool_calls)]
    follow = call(server, kind, [*HISTORY, turn, *outputs], stream=stream, **kw)
    assert len(server.requests) == 2
    second = server.requests[1]["body"]
    assert follow.clean and follow.tool_calls == () and follow.result.text
    if kind == "lmstudio":
        assert server.requests[0]["path"] == "/v1/responses"
        assert first["store"] is False and first["tools"] == [
            {"type": "function", "name": t.name, "description": t.description, "parameters": t.parameters}
            for t in TOOLS]
        replayed = json.loads(fx("responses/resp_tool_call_synthetic.json"))["output"]
        assert second["input"] == [{"role": "user", "content": HISTORY[0].content}, *replayed,
                                   {"type": "function_call_output", "call_id": "call_r1", "output": '{"label": "call_r1"}'}]
        assert replayed[0]["encrypted_content"] == "opaque=="
    else:
        assert server.requests[0]["path"] == "/v1/chat/completions"
        assert [t["function"]["name"] for t in first["tools"]] == ["read_label", "write_note"]
        assert first["tools"][0]["type"] == "function"
        assert "store" not in first
        assistant = second["messages"][1]
        assert assistant["role"] == "assistant"
        assert [c["id"] for c in assistant["tool_calls"]] == [c.id for c in turn.tool_calls]
        assert [c["function"]["arguments"] for c in assistant["tool_calls"]] == [c.arguments_json for c in turn.tool_calls]
        assert second["messages"][2:] == [{"role": "tool", "tool_call_id": c.id, "content": json.dumps({"label": c.id})}
                                          for c in turn.tool_calls]
    if kind == "openrouter":
        assert first["provider"] == {**PIN, "require_parameters": True}
        assert assistant["content"] == "Checking both."
        assert assistant["reasoning_details"] == [{"type": "reasoning.text", "text": "Need labels.",
                                                   "signature": "sig-1", "format": "anthropic-claude-v1", "index": 0}]
        assert turn.result.cost == 0.0004 and turn.result.provider == "DeepInfra"
    if kind == "openai_compat":
        assert assistant["content"] is None
        assert turn.result.reasoning == "think"
        assert "reasoning" not in assistant and "reasoning_content" not in assistant
    if stream and kind != "openai_compat":
        assert "".join(texts).startswith({"openrouter": "Checking both.", "lmstudio": "Looking."}[kind])


@pytest.mark.parametrize("kind", sorted(ROUTES))
def test_t5_stream_matches_nonstream(make_server, kind):
    a, b = make_server(), make_server()
    a.reply(fixture_reply(kind, False))
    b.reply(fixture_reply(kind, True))
    n, s = call(a, kind), call(b, kind, stream=True)
    assert n.tool_calls == s.tool_calls
    for f in ("text", "reasoning", "usage", "finish_reason", "truncated"):
        assert getattr(n.result, f) == getattr(s.result, f), f
    if kind != "lmstudio":
        assert n.replay == s.replay


def test_t5_openrouter_reasoning_text_replay_without_details(server):
    doc = chat_doc({"role": "assistant", "content": "", "reasoning": "hmm", "tool_calls": [tc(0, "c1", "read_label", "{}")]})
    server.reply(json_doc(doc), text_reply("openrouter", False))
    turn = call(server, "openrouter")
    call(server, "openrouter", [*HISTORY, turn, ToolOutput("c1", "ok")])
    assistant = server.requests[1]["body"]["messages"][1]
    assert assistant["reasoning"] == "hmm" and "reasoning_details" not in assistant and assistant["content"] is None


def test_t5_provider_policy_copy_and_model_info(server):
    policy = {"order": ("deepinfra",), "allow_fallbacks": False, "sort": "price"}
    server.reply(fixture_reply("openrouter", False))
    info = ModelInfo("m", None, None, None, True, None, None, None, {})
    complete_with_tools(ep(server, "openrouter"), info, "x", tools=TOOLS, provider=policy)
    assert policy == {"order": ("deepinfra",), "allow_fallbacks": False, "sort": "price"}
    assert server.requests[0]["body"]["provider"] == {"order": ["deepinfra"], "allow_fallbacks": False,
                                                      "sort": "price", "require_parameters": True}
    assert server.requests[0]["body"]["model"] == "m"


def test_t5_narrower_function_set_and_final_answer_turn(server):
    server.reply(fixture_reply("lmstudio", False), fixture_reply("lmstudio", False))
    turn = call(server, "lmstudio")
    again = call(server, "lmstudio", [*HISTORY, turn, ToolOutput("call_r1", "blue"),
                                      Message("user", "Answer now.")], tools=[READ])
    assert [t["name"] for t in server.requests[1]["body"]["tools"]] == ["read_label"]
    # the model proposed calls again on a final-answer turn: returned as data; dispatch is the caller's choice
    assert again.clean and again.tool_calls


# --- T6: repair feedback correlated under the original IDs (acceptance 2) ---------------------------

REPAIR_CALLS = [tc(0, "c_unknown", "launch", '{"x": 1}'), tc(1, "c_schema", "read_label", '{"key": 7}'),
                tc(2, "c_json", "read_label", '{"key": "00')]


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_t6_repair_feedback(server, stream):
    if stream:
        frags = [{"index": i, "id": c["id"], "type": "function",
                  "function": {"name": c["function"]["name"], "arguments": c["function"]["arguments"]}}
                 for i, c in enumerate(REPAIR_CALLS)]
        first = sse_docs([chunk({"tool_calls": frags[:2]}), chunk({"tool_calls": frags[2:]}), chunk(finish="tool_calls")])
    else:
        first = json_doc(chat_doc({"role": "assistant", "content": None, "tool_calls": REPAIR_CALLS}))
    server.reply(first, text_reply("openai_compat", stream))
    turn = call(server, "openai_compat", stream=stream)
    assert [(c.id, c.name, c.arguments_json) for c in turn.tool_calls] == [
        (c["id"], c["function"]["name"], c["function"]["arguments"]) for c in REPAIR_CALLS]
    errors = [ToolOutput(c.id, json.dumps({"error": f"invalid call {c.name}"})) for c in turn.tool_calls]
    call(server, "openai_compat", [*HISTORY, turn, *errors], stream=stream)
    body = server.requests[1]["body"]
    assert body["messages"][1]["tool_calls"] == REPAIR_CALLS
    assert body["messages"][2:] == [{"role": "tool", "tool_call_id": o.call_id, "content": o.content} for o in errors]


def test_t6_responses_repair_feedback(server):
    out = [fc("c1", "launch", '{"x":1}'), fc("c2", "read_label", '{"key":')]
    server.reply(json_doc(resp_doc(out)), text_reply("lmstudio", False))
    turn = call(server, "lmstudio")
    assert [c.arguments_json for c in turn.tool_calls] == ['{"x":1}', '{"key":']
    call(server, "lmstudio", [*HISTORY, turn, ToolOutput("c2", "bad json"), ToolOutput("c1", "unknown tool")])
    assert server.requests[1]["body"]["input"][1:] == [*out, {"type": "function_call_output", "call_id": "c1",
                                                             "output": "unknown tool"},
                                                       {"type": "function_call_output", "call_id": "c2",
                                                        "output": "bad json"}]


def test_t6_exact_numbers_on_the_wire(server):
    big = 2 ** 53 + 1
    dec = Decimal("0.1000000000000000000000000001")
    params = {"type": "object", "properties": {"id": {"type": "string", "const": "007"},
                                               "n": {"type": "integer", "maximum": big, "multipleOf": dec},
                                               "nested": {"type": "array", "items": [{"enum": [1, 1.5, None, True]}]}},
              "additionalProperties": False}
    server.reply(fixture_reply("lmstudio", False))
    call(server, "lmstudio", tools=[ToolDefinition("exact", "Exact numbers.", params)])
    raw = server.requests[0]["raw"]
    assert f'"maximum": {big}'.encode() in raw and b'"multipleOf": 0.1000000000000000000000000001' in raw
    assert b'"const": "007"' in raw and b'"additionalProperties": false' in raw
    assert b'"enum": [1, 1.5, null, true]' in raw
    parsed = json.loads(raw, parse_float=Decimal)
    assert parsed["tools"][0]["parameters"]["properties"]["n"] == {"type": "integer", "maximum": big, "multipleOf": dec}


# --- T7: the completed-turn boundary (acceptance 3) -------------------------------------------------

LOOKS_COMPLETE = [tc(0, "c1", "read_label", '{"key":"007"}')]


@pytest.mark.parametrize("finish,truncated", [("length", True), ("content_filter", False), ("weird", False)])
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_t7_chat_diagnostic_finishes(server, finish, truncated, stream):
    if stream:
        frag = {"index": 0, **LOOKS_COMPLETE[0]}
        server.reply(sse_docs([chunk({"content": "partial", "tool_calls": [frag]}), chunk(finish=finish)]))
    else:
        server.reply(json_doc(chat_doc({"role": "assistant", "content": "partial", "tool_calls": LOOKS_COMPLETE}, finish)))
    turn = call(server, "openai_compat", stream=stream)
    assert not turn.clean and turn.tool_calls == () and turn.replay is None
    assert turn.result.finish_reason == finish and turn.result.truncated is truncated and turn.result.text == "partial"
    with pytest.raises(ConfigError, match="diagnostic"):
        call(server, "openai_compat", [*HISTORY, turn])
    assert len(server.requests) == 1


def test_t7_chat_stop_is_clean_with_or_without_calls(server):
    server.reply(json_doc(chat_doc({"role": "assistant", "content": None, "tool_calls": LOOKS_COMPLETE}, "stop")),
                 json_doc(chat_doc({"role": "assistant", "content": "done"}, "stop")))
    with_calls = call(server, "openai_compat")
    assert with_calls.clean and len(with_calls.tool_calls) == 1
    text_only = call(server, "openai_compat")
    assert text_only.clean and text_only.tool_calls == ()
    assert text_only.replay == ({"role": "assistant", "content": "done"},)
    server.reply(json_doc(chat_doc({"role": "assistant", "content": "ok"}, "stop")))
    call(server, "openai_compat", [*HISTORY, text_only, Message("user", "next")])
    assert server.requests[-1]["body"]["messages"][1] == {"role": "assistant", "content": "done"}


CHAT_ERRORS = [
    ("done_without_finish", sse_docs([chunk({"tool_calls": [{"index": 0, **LOOKS_COMPLETE[0]}]})]), ResponseFormatError),
    ("finish_without_done", sse_docs([chunk({"tool_calls": [{"index": 0, **LOOKS_COMPLETE[0]}]}), chunk(finish="tool_calls")],
                                     done=False), ResponseFormatError),
    ("neither", sse_docs([chunk({"tool_calls": [{"index": 0, **LOOKS_COMPLETE[0]}]})], done=False), BackendError),
    ("tool_calls_without_calls", sse_docs([chunk({"content": "x"}), chunk(finish="tool_calls")]), ResponseFormatError),
    ("error_finish", sse_docs([chunk(finish="error")]), BackendError),
    ("extra_choice", sse_docs([{"choices": [{"index": 0, "delta": {}}, {"index": 1, "delta": {}}]}]), ResponseFormatError),
    ("choice_index_1", sse_docs([{"choices": [{"index": 1, "delta": {}}]}]), ResponseFormatError),
    ("no_id", sse_docs([chunk({"tool_calls": [{"index": 0, "function": {"name": "a", "arguments": "{}"}}]}),
                        chunk(finish="tool_calls")]), ResponseFormatError),
    ("no_name", sse_docs([chunk({"tool_calls": [{"index": 0, "id": "c", "function": {"arguments": "{}"}}]}),
                          chunk(finish="tool_calls")]), ResponseFormatError),
    ("no_index", sse_docs([chunk({"tool_calls": [{"id": "c", "function": {"name": "a", "arguments": "{}"}}]}),
                           chunk(finish="tool_calls")]), ResponseFormatError),
    ("conflicting_id", sse_docs([chunk({"tool_calls": [{"index": 0, "id": "c", "function": {"name": "a"}}]}),
                                 chunk({"tool_calls": [{"index": 0, "id": "d", "function": {"arguments": "{}"}}]}),
                                 chunk(finish="tool_calls")]), ResponseFormatError),
    ("non_string_args", sse_docs([chunk({"tool_calls": [{"index": 0, "id": "c", "function": {"name": "a", "arguments": {}}}]}),
                                  chunk(finish="tool_calls")]), ResponseFormatError),
    ("duplicate_ids", sse_docs([chunk({"tool_calls": [{"index": 0, **LOOKS_COMPLETE[0]}, {"index": 1, **LOOKS_COMPLETE[0]}]}),
                                chunk(finish="tool_calls")]), ResponseFormatError),
]


@pytest.mark.parametrize("name,reply,exc", CHAT_ERRORS, ids=[c[0] for c in CHAT_ERRORS])
def test_t7_chat_stream_errors(server, name, reply, exc):
    server.reply(reply)
    with pytest.raises(exc):
        call(server, "openai_compat", stream=True)


CHAT_JSON_ERRORS = [
    ("missing_finish", chat_doc({"role": "assistant", "content": None, "tool_calls": LOOKS_COMPLETE}, None), ResponseFormatError),
    ("tool_calls_without_calls", chat_doc({"role": "assistant", "content": "x"}), ResponseFormatError),
    ("two_choices", {"choices": [{"index": 0, "finish_reason": "stop", "message": {"content": "a"}},
                                 {"index": 1, "finish_reason": "stop", "message": {"content": "b"}}]}, ResponseFormatError),
    ("no_id", chat_doc({"content": None, "tool_calls": [{"type": "function", "function": {"name": "a", "arguments": "{}"}}]}),
     ResponseFormatError),
    ("object_args", chat_doc({"content": None, "tool_calls": [tc(0, "c", "a", "{}") | {"function": {"name": "a", "arguments": {}}}]}),
     ResponseFormatError),
    ("error", {"error": {"message": "x", "code": 429}}, BackendError),
    ("error_finish", chat_doc({"content": None}, "error"), BackendError),
]


@pytest.mark.parametrize("name,doc,exc", CHAT_JSON_ERRORS, ids=[c[0] for c in CHAT_JSON_ERRORS])
def test_t7_chat_json_errors(server, name, doc, exc):
    server.reply(json_doc(doc))
    with pytest.raises(exc):
        call(server, "openai_compat")
    assert len(server.requests) == 1


def test_t7_broken_stream_mid_call_yields_nothing(server):
    body = f"data: {json.dumps(chunk({'tool_calls': [{'index': 0, 'id': 'c', 'function': {'name': 'a', 'arguments': '{'}}]}))}\n\n"
    server.reply(Reply(body=[body.encode()], headers={"Content-Type": "text/event-stream"}, reset=True))
    with pytest.raises(BackendError):
        call(server, "openai_compat", stream=True)


@pytest.mark.parametrize("reason,truncated", [("max_output_tokens", True), ("content_filter", False)])
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_t7_responses_incomplete_is_diagnostic(server, reason, truncated, stream):
    doc = resp_doc([fc("c1")], "incomplete", incomplete_details={"reason": reason})
    if stream:
        server.reply(sse_docs([{"type": "response.output_item.done", "output_index": 0, "item": fc("c1")},
                               {"type": "response.incomplete", "response": doc}], done=False))
    else:
        server.reply(json_doc(doc))
    turn = call(server, "lmstudio", stream=stream)
    assert not turn.clean and turn.tool_calls == () and turn.result.truncated is truncated
    assert turn.result.finish_reason == reason


RESP_JSON_ERRORS = [
    ("missing_status", resp_doc([fc("c1")], status=None), ResponseFormatError),
    ("in_progress", resp_doc([fc("c1")], status="in_progress"), ResponseFormatError),
    ("unfinished_call_item", resp_doc([fc("c1", status="in_progress")]), ResponseFormatError),
    ("no_call_id", resp_doc([{"type": "function_call", "name": "a", "arguments": "{}"}]), ResponseFormatError),
    ("duplicate_call_id", resp_doc([fc("c1"), fc("c1")]), ResponseFormatError),
    ("no_output", {"id": "r", "status": "completed"}, ResponseFormatError),
    ("failed", resp_doc([], status="failed", error={"message": "boom"}), BackendError),
]


@pytest.mark.parametrize("name,doc,exc", RESP_JSON_ERRORS, ids=[c[0] for c in RESP_JSON_ERRORS])
def test_t7_responses_json_errors(server, name, doc, exc):
    server.reply(json_doc(doc))
    with pytest.raises(exc):
        call(server, "lmstudio")


def _resp_stream(final, final_type="response.completed", extra=()):
    return sse_docs([*extra, {"type": final_type, "response": final}], done=False)


RESP_STREAM_ERRORS = [
    ("completed_event_incomplete_status", _resp_stream(resp_doc([], "incomplete")), ResponseFormatError),
    ("incomplete_event_completed_status", _resp_stream(resp_doc([]), "response.incomplete"), ResponseFormatError),
    ("missing_status", _resp_stream(resp_doc([], status=None)), ResponseFormatError),
    ("no_terminal", sse_docs([{"type": "response.output_text.delta", "delta": "x"}], done=False), BackendError),
    ("args_differ", _resp_stream(resp_doc([fc("c1", args='{"key":"008"}')]), extra=[
        {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "fc_c1", "delta": '{"key":"007"}'}]),
     ResponseFormatError),
    ("call_id_differs", _resp_stream(resp_doc([fc("c1")]), extra=[
        {"type": "response.output_item.added", "output_index": 0, "item": fc("c2", args="")}]), ResponseFormatError),
    ("streamed_call_missing_from_terminal", _resp_stream(resp_doc([{"type": "message", "content": []}]), extra=[
        {"type": "response.output_item.done", "output_index": 0, "item": fc("c1")}]), ResponseFormatError),
    ("mid_call_without_terminal_output", _resp_stream(resp_doc([]), extra=[
        {"type": "response.output_item.added", "output_index": 0, "item": fc("c1", args="")},
        {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "fc_c1", "delta": "{"}]),
     ResponseFormatError),
    ("conflicting_item_id", _resp_stream(resp_doc([fc("c1")]), extra=[
        {"type": "response.output_item.added", "output_index": 0, "item": fc("c1", args="")},
        {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "other", "delta": "{"}]),
     ResponseFormatError),
    ("failed", _resp_stream(resp_doc([], "failed"), "response.failed"), BackendError),
    ("error_event", sse_docs([{"type": "error", "code": 429, "message": "slow down"}], done=False), BackendError),
]


@pytest.mark.parametrize("name,reply,exc", RESP_STREAM_ERRORS, ids=[c[0] for c in RESP_STREAM_ERRORS])
def test_t7_responses_stream_errors(server, name, reply, exc):
    server.reply(reply)
    with pytest.raises(exc):
        call(server, "lmstudio", stream=True)


def test_t7_responses_stream_without_terminal_output_uses_done_items(server):
    items = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}, fc("c1")]
    server.reply(_resp_stream(resp_doc([]), extra=[
        {"type": "response.output_item.done", "output_index": 0, "item": items[0]},
        {"type": "response.output_item.added", "output_index": 1, "item": fc("c1", args="")},
        {"type": "response.function_call_arguments.delta", "output_index": 1, "item_id": "fc_c1", "delta": '{"key":"007"}'},
        {"type": "response.output_item.done", "output_index": 1, "item": items[1]}]))
    turn = call(server, "lmstudio", stream=True)
    assert turn.clean and turn.tool_calls == (ToolCall("c1", "read_label", '{"key":"007"}'),)
    assert turn.replay == tuple(items)


# --- T8: replay identity ---------------------------------------------------------------------------

def _turn(server, kind="openrouter", **kw):
    server.reply(fixture_reply(kind, False), text_reply(kind, False))
    return call(server, kind, **kw)


@pytest.mark.parametrize("change", ["model", "base_url", "reasoning", "provider", "kind"])
def test_t8_mismatched_identity_is_refused(server, make_server, change):
    turn = _turn(server, provider=PIN)
    endpoint, model, kw = ep(server, "openrouter"), "m", {"provider": PIN}
    if change == "model":
        model = "other"
    elif change == "base_url":
        endpoint = ep(make_server(), "openrouter")
    elif change == "reasoning":
        kw["reasoning"] = "high"
    elif change == "provider":
        kw["provider"] = {"order": ["together"], "allow_fallbacks": False}
    else:
        endpoint, kw = ep(server, "openai_compat"), {}
    outputs = [ToolOutput(c.id, "x") for c in turn.tool_calls]
    with pytest.raises(ConfigError, match="different route"):
        complete_with_tools(endpoint, model, [*HISTORY, turn, *outputs], tools=TOOLS, **kw)
    assert len(server.requests) == 1


def test_t8_equivalent_identity_continues(server):
    turn = _turn(server, provider={"order": ("deepinfra/turbo",), "allow_fallbacks": False})
    fresh = {"allow_fallbacks": False, "order": ["deepinfra/turbo"], "require_parameters": True}
    rotated = Endpoint(kind="openrouter", base_url=server.url.upper().replace("HTTP://", "http://") + "/v1/",
                       api_key="rotated-key")
    outputs = [ToolOutput(c.id, "x") for c in turn.tool_calls]
    follow = complete_with_tools(rotated, "m", [*HISTORY, turn, *outputs], tools=TOOLS, provider=fresh)
    assert follow.clean and len(server.requests) == 2
    assert server.requests[1]["headers"]["authorization"] == "Bearer rotated-key"
    assert turn.route_identity["provider"]["require_parameters"] is True


def test_t8_scalar_types_are_part_of_identity(server):
    turn = _turn(server, provider={**PIN, "max_price": {"prompt": 1}})
    with pytest.raises(ConfigError, match="provider differs"):
        call(server, "openrouter", [*HISTORY, turn, *[ToolOutput(c.id, "x") for c in turn.tool_calls]],
             provider={**PIN, "max_price": {"prompt": 1.0}})


# --- T9: transcript pairing --------------------------------------------------------------------------

def test_t9_pairing_rules(server):
    turn = _turn(server)
    a, b = (ToolOutput(c.id, "x") for c in turn.tool_calls)
    cases = {
        "missing": [*HISTORY, turn, a],
        "missing_before_message": [*HISTORY, turn, a, Message("user", "x"), b],
        "duplicate": [*HISTORY, turn, a, a, b],
        "orphan": [*HISTORY, a],
        "unknown": [*HISTORY, turn, a, ToolOutput("nope", "x")],
        "after_message": [*HISTORY, turn, Message("user", "x"), a, b],
        "extra_after_pairs": [*HISTORY, turn, a, b, a],
        "bad_item": [*HISTORY, 5],
        "empty": [],
    }
    for name, input in cases.items():
        with pytest.raises(ConfigError):
            call(server, "openrouter", input)
    assert len(server.requests) == 1


def test_t9_ids_are_matched_within_their_turn(server):
    same = json_doc(chat_doc({"role": "assistant", "content": None, "tool_calls": [tc(0, "dup", "read_label", "{}")]}))
    server.reply(same, same, text_reply("openai_compat", False))
    t1 = call(server, "openai_compat")
    t2 = call(server, "openai_compat", [*HISTORY, t1, ToolOutput("dup", "one")])
    call(server, "openai_compat", [*HISTORY, t1, ToolOutput("dup", "one"), t2, ToolOutput("dup", "two")])
    messages = server.requests[2]["body"]["messages"]
    assert [m.get("content") for m in messages if m["role"] == "tool"] == ["one", "two"]
    with pytest.raises(ConfigError, match="does not follow"):
        call(server, "openai_compat", [*HISTORY, t1, ToolOutput("dup", "one"), t2, ToolOutput("dup", "two"),
                                       ToolOutput("dup", "three")])


def test_t9_complete_rejects_tool_entries_before_sending(server):
    turn = _turn(server, kind="openai_compat")
    for input in ([*HISTORY, turn], [*HISTORY, ToolOutput("c", "x")]):
        with pytest.raises(ConfigError, match="complete_with_tools"):
            complete(ep(server, "openai_compat"), "m", input)
    assert len(server.requests) == 1


def test_t9_forged_turn_is_refused(server):
    forged = object.__new__(ToolTurn)
    with pytest.raises(ConfigError, match="not created"):
        call(server, "openai_compat", [*HISTORY, forged])
    assert server.requests == []


# --- T10: Stop ---------------------------------------------------------------------------------------

def test_t10_cancel_mid_stream_exposes_no_calls(server):
    handle = CancelHandle()
    server.reply(fixture_reply("openrouter", True))

    def on_text(s):
        handle.cancel()
    with pytest.raises(Cancelled) as info:
        call(server, "openrouter", stream=True, on_text=on_text, cancel=handle)
    assert info.value.sent is True and info.value.partial.text == "Checking "
    assert info.value.partial.finish_reason == "cancelled"


def test_t10_pre_cancelled_handle_sends_nothing(server):
    handle = CancelHandle()
    handle.cancel()
    with pytest.raises(Cancelled) as info:
        call(server, "lmstudio", cancel=handle)
    assert info.value.sent is False and server.connections == 0


def test_t10_callback_exception_propagates(server):
    server.reply(fixture_reply("lmstudio", True))

    class Boom(Exception):
        pass

    def on_text(s):
        raise Boom
    with pytest.raises(Boom):
        call(server, "lmstudio", stream=True, on_text=on_text)


# --- T11: diagnostics stay separate from replay ------------------------------------------------------

def test_t11_mutating_result_data_cannot_alter_replay(server):
    turn = _turn(server)
    before = copy.deepcopy(turn.replay)
    turn.result.reasoning_details[0]["text"] = "tampered"
    turn.result.raw_usage["prompt_tokens"] = -1
    assert turn.replay == before and turn.replay[0]["reasoning_details"][0]["text"] == "Need labels."
    call(server, "openrouter", [*HISTORY, turn, *[ToolOutput(c.id, "x") for c in turn.tool_calls]])
    assert server.requests[1]["body"]["messages"][1]["reasoning_details"][0]["text"] == "Need labels."


def test_t11_copied_turn_replays(server):
    turn = _turn(server)
    outputs = [ToolOutput(c.id, "x") for c in turn.tool_calls]
    call(server, "openrouter", [*HISTORY, copy.deepcopy(turn), *outputs])
    assert len(server.requests) == 2


def test_t11_key_never_in_tool_errors(server):
    server.reply(json_doc({"error": {"message": f"bad key {KEY}"}}))
    with pytest.raises(BackendError) as info:
        complete_with_tools(ep(server, "openrouter", api_key=KEY), "m", "x", tools=TOOLS, provider=PIN)
    assert KEY not in str(info.value) and KEY not in info.value.body
