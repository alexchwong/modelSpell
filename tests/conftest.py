"""Local scripted HTTP server fixture (spec §12.1) and shared helpers."""
from __future__ import annotations

import json
import pathlib
import select
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from modelspell import Endpoint

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
PROXY_VARS = [v for n in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY") for v in (n, n.lower())]
MSGS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
KEY = "sk-SENTINEL-1234567890"


@dataclass
class Reply:
    status: int = 200
    body: bytes | list[bytes] = b""   # list => sent chunk by chunk with flush(), no Content-Length
    headers: dict = field(default_factory=dict)
    delay: float = 0.0                # before the first byte
    chunk_delay: float = 0.0
    hold: bool = False                # keep the body open after the chunks until release is set


class TestServer:
    def __init__(self):
        self.requests: list[dict] = []
        self.replies: list[Reply] = []
        self.release = threading.Event()
        self.client_closed = threading.Event()
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw
                srv.requests.append({"method": self.command, "path": self.path, "body": body,
                                     "headers": {k.lower(): v for k, v in self.headers.items()}})
                r = srv.replies.pop(0) if len(srv.replies) > 1 else (srv.replies[0] if srv.replies else Reply())
                time.sleep(r.delay)
                try:
                    self.send_response(r.status)
                    for k, v in r.headers.items():
                        self.send_header(k, v)
                    chunks = r.body if isinstance(r.body, list) else [r.body]
                    if not isinstance(r.body, list) and not r.hold:
                        self.send_header("Content-Length", str(len(r.body)))
                    self.end_headers()
                    for c in chunks:
                        self.wfile.write(c)
                        self.wfile.flush()
                        time.sleep(r.chunk_delay)
                    if r.hold:
                        self._hold()
                except OSError:
                    srv.client_closed.set()

            def _hold(self):
                sock = self.connection
                while not srv.release.is_set():
                    if select.select([sock], [], [], 0.05)[0]:
                        try:
                            data = sock.recv(1, socket.MSG_PEEK)
                        except OSError:
                            data = b""
                        if not data:
                            srv.client_closed.set()
                            return

            do_GET = do_POST = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, args=(0.02,), daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def reply(self, *replies: Reply) -> "TestServer":
        self.replies = list(replies)
        return self

    def close(self):
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture(autouse=True)
def _clean_env(request, monkeypatch):
    if request.node.get_closest_marker("live"):
        return
    for var in PROXY_VARS + ["OPENROUTER_API_KEY"]:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def make_server():
    servers = []

    def make() -> TestServer:
        servers.append(TestServer())
        return servers[-1]

    yield make
    for s in servers:
        s.close()


@pytest.fixture
def server(make_server) -> TestServer:
    return make_server()


def fx(rel: str) -> bytes:
    return (FIXTURES / rel).read_bytes()


def fx_json(rel: str):
    return json.loads(fx(rel))


def json_reply(rel: str, **kw) -> Reply:
    return Reply(body=fx(rel), headers={"Content-Type": "application/json"}, **kw)


def sse_reply(rel: str, chunks: list[bytes] | None = None, **kw) -> Reply:
    return Reply(body=chunks if chunks is not None else [fx(rel)], headers={"Content-Type": "text/event-stream"}, **kw)


def ep(server: TestServer, kind: str, **kw) -> Endpoint:
    if kind == "openrouter":
        kw.setdefault("api_key", "test-key")
    return Endpoint(kind=kind, base_url=server.url + "/v1", **kw)


# canned success responses per (route, streaming)
CANNED = {
    ("chat", False): "chat/or_text_synthetic.json", ("chat", True): "chat/or_text_synthetic.sse",
    ("responses", False): "responses/resp_text_synthetic.json", ("responses", True): "responses/resp_text_synthetic.sse",
    ("native", False): "native/native_text_synthetic.json", ("native", True): "native/native_text_synthetic.sse",
}


def canned(route: str, streaming: bool) -> Reply:
    rel = CANNED[(route, streaming)]
    return sse_reply(rel) if streaming else json_reply(rel)


def route_of(kind: str, reasoning) -> str:
    return "chat" if kind != "lmstudio" else ("native" if reasoning == "off" else "responses")
