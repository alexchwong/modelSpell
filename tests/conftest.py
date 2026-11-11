"""Local scripted HTTP server fixture (spec §12.1) and shared helpers."""
from __future__ import annotations

import http.client
import json
import pathlib
import select
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from modelspell import Endpoint

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
TLS_CERT = FIXTURES / "tls" / "localhost.crt"   # test certificate (spec v8 §12.1); not a response fixture
TLS_KEY = FIXTURES / "tls" / "localhost.key"


def server_tls_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(TLS_CERT, TLS_KEY)
    return ctx


def _peer_closed(sock) -> bool:
    """True once the client has closed `sock` (polled by hold modes). Works for plain and TLS sockets."""
    if isinstance(sock, ssl.SSLSocket):
        if not (sock.pending() or select.select([sock], [], [], 0.05)[0]):
            return False
        sock.settimeout(0.05)
        try:
            return not sock.recv(1024)
        except (ssl.SSLWantReadError, socket.timeout, TimeoutError):
            return False
        except (OSError, ValueError):
            return True
    if not select.select([sock], [], [], 0.05)[0]:
        return False
    try:
        return not sock.recv(1, socket.MSG_PEEK)
    except OSError:
        return True
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
    hold_headers: bool = False        # read the request, then send nothing until release or client close
    reset: bool = False               # after the chunks, close the connection with a TCP reset


class TestServer:
    def __init__(self, tls: bool = False):
        self.tls = tls
        self.requests: list[dict] = []
        self.replies: list[Reply] = []
        self.release = threading.Event()
        self.client_closed = threading.Event()
        self.reached = threading.Event()  # set when any hold begins (headers hold, or body hold)
        self.connections = 0              # TCP connections accepted
        self._holds: dict[int, threading.Event] = {}
        self._holds_lock = threading.Lock()
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
                srv.requests.append({"method": self.command, "path": self.path, "body": body, "raw": raw,
                                     "headers": {k.lower(): v for k, v in self.headers.items()}})
                index = len(srv.requests) - 1
                r = srv.replies.pop(0) if len(srv.replies) > 1 else (srv.replies[0] if srv.replies else Reply())
                time.sleep(r.delay)
                if r.hold_headers:
                    srv.reached.set()
                    srv.hold_event(index).set()
                    self._hold()
                    return
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
                    if r.reset:
                        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\0\0\0\0\0\0\0")
                        self.connection.close()
                        self.close_connection = True
                        return
                    if r.hold:
                        srv.reached.set()
                        srv.hold_event(index).set()
                        self._hold()
                except OSError:
                    srv.client_closed.set()

            def _hold(self):
                sock = self.connection
                while not srv.release.is_set():
                    if _peer_closed(sock):
                        srv.client_closed.set()
                        return

            do_GET = do_POST = _handle

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def process_request(self, request, client_address):
                srv.connections += 1
                super().process_request(request, client_address)

            def finish_request(self, request, client_address):  # runs in the handler thread
                if srv.tls:
                    try:
                        request = server_tls_context().wrap_socket(request, server_side=True)
                    except (OSError, ssl.SSLError):
                        return
                super().finish_request(request, client_address)

        self.httpd = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, args=(0.02,), daemon=True).start()

    @property
    def url(self) -> str:
        scheme = "https" if self.tls else "http"
        return f"{scheme}://127.0.0.1:{self.httpd.server_address[1]}"

    def hold_event(self, index: int) -> threading.Event:
        """Set when the hold of the request with this index (0-based, in arrival order) begins."""
        with self._holds_lock:
            return self._holds.setdefault(index, threading.Event())

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


# --- fixtures for cancellation tests (spec v8 §12.1) ---------------------------------------------

@pytest.fixture
def trust_test_cert(monkeypatch):
    """urllib's default HTTPS context trusts the test certificate through SSL_CERT_FILE."""
    monkeypatch.setenv("SSL_CERT_FILE", str(TLS_CERT))
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)


@pytest.fixture
def tls_server(trust_test_cert):
    s = TestServer(tls=True)
    yield s
    s.close()


class SilentListener:
    """Accepts TCP connections and never reads. With tls=True, completes the TLS handshake first."""

    def __init__(self, tls: bool = False, rcvbuf: int | None = None):
        self.tls = tls
        self.accepted = threading.Event()
        self.handshaken = threading.Event()
        self.socks: list = []
        self._stop = False
        self.lsock = socket.socket()
        if rcvbuf:
            self.lsock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self.lsock.bind(("127.0.0.1", 0))
        self.lsock.listen(8)
        self.lsock.settimeout(0.05)
        self.port = self.lsock.getsockname()[1]
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self._stop:
            try:
                conn, _ = self.lsock.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            conn.settimeout(None)
            self.socks.append(conn)
            self.accepted.set()
            if self.tls:
                try:
                    tconn = server_tls_context().wrap_socket(conn, server_side=True)
                    self.socks.append(tconn)
                    self.handshaken.set()
                except (OSError, ssl.SSLError):
                    pass

    def has_bytes(self) -> bool:
        return any(select.select([s], [], [], 0)[0] for s in self.socks[:1])

    def url(self, scheme: str = "http") -> str:
        return f"{scheme}://127.0.0.1:{self.port}/v1"

    def close(self):
        self._stop = True
        for s in self.socks:
            try:
                s.close()
            except OSError:
                pass
        self.lsock.close()


@pytest.fixture
def silent_listener():
    made = []

    def make(tls: bool = False, rcvbuf: int | None = None) -> SilentListener:
        made.append(SilentListener(tls, rcvbuf))
        return made[-1]

    yield make
    for listener in made:
        listener.close()


@pytest.fixture
def hooked_send(monkeypatch):
    """Sets `entered` before forwarding any write larger than 1 MiB through http.client."""
    entered = threading.Event()
    original = http.client.HTTPConnection.send

    def send(self, data):
        if isinstance(data, (bytes, bytearray)) and len(data) > (1 << 20):
            entered.set()
        return original(self, data)

    monkeypatch.setattr(http.client.HTTPConnection, "send", send)
    return entered


@pytest.fixture
def held_connect():
    """An address whose TCP connect does not complete (a listener with a full backlog), or skip."""
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(0)
    addr = lsock.getsockname()
    fillers = []
    for _ in range(8):
        f = socket.socket()
        f.setblocking(False)
        f.connect_ex(addr)
        fillers.append(f)
    time.sleep(0.1)
    probe = socket.socket()
    probe.settimeout(0.5)
    try:
        probe.connect(addr)
        held = False
    except OSError:
        held = True
    finally:
        probe.close()
    if not held:
        for f in fillers:
            f.close()
        lsock.close()
        pytest.skip("this platform does not hold a TCP connect")
    yield f"http://127.0.0.1:{addr[1]}/v1"
    for f in fillers:
        f.close()
    lsock.close()


class HeldResolution:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()


@pytest.fixture
def held_resolution(monkeypatch):
    """socket.getaddrinfo waits for `release` (once) before resolving; `entered` is set on entry."""
    held = HeldResolution()
    original = socket.getaddrinfo

    def getaddrinfo(*a, **k):
        if not held.entered.is_set():
            held.entered.set()
            held.release.wait(30)
        return original(*a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return held


@pytest.fixture
def two_addresses(monkeypatch):
    """getaddrinfo yields a refused address, then runs `between()`, then the live address."""

    def install(live_port: int, between):
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
        dead.close()  # nothing listens: connection refused

        def getaddrinfo(host, port, *a, **k):
            yield (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", dead_port))
            between()
            yield (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", live_port))

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

    return install


class HeldProxy:
    """A TCP 'proxy' that reads a CONNECT request, then holds without answering (or answers `status`)."""

    def __init__(self, status: int | None = None):
        self.status = status
        self.got_connect = threading.Event()
        self.socks: list = []
        self.lsock = socket.socket()
        self.lsock.bind(("127.0.0.1", 0))
        self.lsock.listen(4)
        self.port = self.lsock.getsockname()[1]
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            conn, _ = self.lsock.accept()
        except OSError:
            return
        self.socks.append(conn)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk
        if data.startswith(b"CONNECT "):
            self.got_connect.set()
        if self.status is not None:
            conn.sendall(f"HTTP/1.1 {self.status} Forbidden\r\nContent-Length: 0\r\n\r\n".encode())

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        for s in self.socks:
            s.close()
        self.lsock.close()


@pytest.fixture
def held_proxy(monkeypatch):
    made = []

    def make(status: int | None = None) -> HeldProxy:
        made.append(HeldProxy(status))
        for var in ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("HTTPS_PROXY", made[-1].url)
        return made[-1]

    yield make
    for proxy in made:
        proxy.close()
