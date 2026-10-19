"""Dev-only K3 smoke test: one complete() call against an inline stub server, using the installed wheel."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
    os.environ.pop(name, None)
    os.environ.pop(name.lower(), None)

import modelspell  # noqa: E402

REPLY = json.dumps({"choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}]}).encode()


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Length", str(len(REPLY)))
        self.end_headers()
        self.wfile.write(REPLY)

    def log_message(self, *a):
        pass


httpd = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
endpoint = modelspell.Endpoint(kind="openai_compat", base_url=f"http://127.0.0.1:{httpd.server_address[1]}/v1")
result = modelspell.complete(endpoint, "m", "ping")
assert result.text == "pong", result
print("smoke ok:", modelspell.__file__)
httpd.shutdown()
