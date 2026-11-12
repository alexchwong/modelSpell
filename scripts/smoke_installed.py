"""Dev-only K3 smoke test against an inline stub server, using the installed wheel.

Runs one complete() call and one two-turn complete_with_tools() exchange (spec v9 §18.2).
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
    os.environ.pop(name, None)
    os.environ.pop(name.lower(), None)

import modelspell  # noqa: E402

TEXT = {"choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}]}
CALL = {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
    "role": "assistant", "content": None,
    "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read_label", "arguments": "{\"key\":\"007\"}"}}]}}]}
BODIES = []


class Stub(BaseHTTPRequestHandler):
    def do_GET(self):
        self.reply({"data": {"endpoints": [{"tag": "test/fast", "provider_name": "Test",
                    "pricing": {"prompt": "0", "completion": "0"},
                    "throughput_last_30m": {"p50": 90}}]}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        BODIES.append(body)
        tool_turn = "tools" in body and not any(m.get("role") == "tool" for m in body["messages"])
        self.reply(CALL if tool_turn else TEXT)

    def reply(self, value):
        reply = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *a):
        pass


httpd = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
endpoint = modelspell.Endpoint(kind="openai_compat", base_url=f"http://127.0.0.1:{httpd.server_address[1]}/v1")
result = modelspell.complete(endpoint, "m", "ping")
assert result.text == "pong", result

tools = [modelspell.ToolDefinition("read_label", "Read the fixture label.", {
    "type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"], "additionalProperties": False})]
modelspell.check_tool_request("openai_compat", tools=tools)
history = [modelspell.Message("user", "Read the label for item 007.")]
turn = modelspell.complete_with_tools(endpoint, "m", history, tools=tools)
assert turn.clean and turn.tool_calls == (modelspell.ToolCall("call_1", "read_label", '{"key":"007"}'),), turn
(call,) = turn.tool_calls
follow = modelspell.complete_with_tools(endpoint, "m", [*history, turn, modelspell.ToolOutput(call.id, "blue")],
                                        tools=tools)
assert follow.clean and follow.tool_calls == () and follow.result.text == "pong", follow
assert BODIES[-1]["messages"][-1] == {"role": "tool", "tool_call_id": "call_1", "content": "blue"}, BODIES[-1]
router = modelspell.Endpoint("openrouter", endpoint.base_url, api_key="synthetic-smoke-key")
selection = modelspell.choose_provider(router, "test/model", provider={"max_price": {"prompt": 1}})
assert selection.slug == "test/fast" and selection.evidence["attempts"][0]["sent"]
assert modelspell.complete(router, "test/model", "ping", provider=selection.policy).text == "pong"
print("smoke ok:", modelspell.__file__)
httpd.shutdown()
