"""Developer test UI for modelSpell. Not part of the library and never shipped in the wheel.

Run from the repository root:  python -m ui [--port N] [--no-browser]

Binds to 127.0.0.1 only. Every request must carry the per-launch session token (the page URL's
`t` parameter; API calls send it as X-Session-Token), so other web pages in the browser cannot
drive the server or spend API credit. API keys are write-only: held in this process's memory,
keyed by (kind, base_url) so a key is never sent to a different endpoint, and never returned.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import modelspell
from modelspell import Cancelled, Endpoint, ModelSpellError

INDEX = pathlib.Path(__file__).with_name("index.html")
DEFAULT_SETTINGS = {"kind": "lmstudio", "base_url": "", "model": "", "provider": "", "reasoning": "",
                    "max_tokens": None}


class UIServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int = 0):
        super().__init__(("127.0.0.1", port), Handler)
        self.token = secrets.token_urlsafe(24)
        self.settings = dict(DEFAULT_SETTINGS)
        self.keys: dict[tuple[str, str], str] = {}
        self.index = INDEX.read_bytes()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/?t={self.token}"

    def endpoint(self, body: dict) -> Endpoint:
        """Endpoint from request overrides (kind/base_url/api_key) falling back to saved settings."""
        kind = body.get("kind") or self.settings["kind"]
        base_url = body.get("base_url") if "base_url" in body else self.settings["base_url"]
        probe = Endpoint(kind=kind, base_url=base_url or None)
        key = body.get("api_key") or self.keys.get((probe.kind, probe.base_url))
        return Endpoint(kind=kind, base_url=probe.base_url, api_key=key)


def _jsonable(obj):
    return dataclasses.asdict(obj) if dataclasses.is_dataclass(obj) else obj


class Handler(BaseHTTPRequestHandler):
    server: UIServer

    def log_message(self, *a):
        pass

    def _send(self, status: int, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorised(self) -> bool:
        supplied = self.headers.get("X-Session-Token") or parse_qs(urlparse(self.path).query).get("t", [""])[0]
        return secrets.compare_digest(supplied, self.server.token)

    def _body(self) -> dict:
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.loads(raw or b"{}")
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        return body

    def do_GET(self):  # noqa: N802
        if not self._authorised():
            return self._send(403, {"error": "missing or invalid session token"})
        path = urlparse(self.path).path
        if path == "/":
            return self._send(200, self.server.index, "text/html; charset=utf-8")
        if path == "/api/state":
            return self._send(200, self._state())
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self._authorised():
            return self._send(403, {"error": "missing or invalid session token"})
        routes = {"/api/config": self._config, "/api/models": self._models,
                  "/api/providers": self._providers, "/api/complete": self._complete}
        handler = routes.get(urlparse(self.path).path)
        if handler is None:
            return self._send(404, {"error": "not found"})
        try:
            handler(self._body())
        except (ModelSpellError, ValueError, TypeError) as exc:
            self._send(400, {"error": f"{type(exc).__name__}: {exc}"})

    def _state(self) -> dict:
        s = self.server
        try:
            e = s.endpoint({})
            key_set = bool(e._resolve_key())
        except ModelSpellError:
            key_set = False
        return {**s.settings, "key_set": key_set, "version": modelspell.__version__}

    def _config(self, body: dict):
        s = self.server
        new = {k: body.get(k, s.settings[k]) for k in DEFAULT_SETTINGS}
        e = Endpoint(kind=new["kind"], base_url=new["base_url"] or None)  # validates kind/base_url
        if "api_key" in body and body["api_key"] is not None:
            if body["api_key"]:
                s.keys[(e.kind, e.base_url)] = str(body["api_key"])
            else:
                s.keys.pop((e.kind, e.base_url), None)
        s.settings = new
        self._send(200, self._state())

    def _models(self, body: dict):
        models = modelspell.list_models(self.server.endpoint(body))
        self._send(200, {"models": [{k: v for k, v in _jsonable(m).items() if k != "raw"} for m in models]})

    def _providers(self, body: dict):
        model = body.get("model") or self.server.settings["model"]
        providers = modelspell.list_providers(self.server.endpoint(body), model)
        self._send(200, {"providers": [{k: v for k, v in _jsonable(p).items() if k != "raw"} for p in providers]})

    def _complete(self, body: dict):
        s = self.server.settings
        endpoint = self.server.endpoint({})
        messages = ([{"role": "system", "content": body["system"]}] if body.get("system") else []) + \
            [{"role": "user", "content": str(body.get("prompt") or "")}]
        kwargs = dict(reasoning=s["reasoning"] or None, max_tokens=s["max_tokens"] or None,
                      provider={"order": [s["provider"]], "allow_fallbacks": False}
                      if s["provider"] and endpoint.kind == "openrouter" else None)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        lock = threading.Lock()

        def emit(event: dict):
            try:
                with lock:
                    self.wfile.write(json.dumps(event).encode() + b"\n")
                    self.wfile.flush()
            except OSError:
                raise Cancelled("browser disconnected") from None  # closes the upstream request

        try:
            result = modelspell.complete(endpoint, s["model"], messages,
                                         on_text=lambda t: emit({"type": "text", "text": t}),
                                         on_reasoning=lambda t: emit({"type": "reasoning", "text": t}),
                                         **kwargs)
            emit({"type": "result", "result": _jsonable(result)})
        except Cancelled:
            return
        except ModelSpellError as exc:
            try:
                emit({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
            except Cancelled:
                pass


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="modelSpell developer test UI (127.0.0.1 only)")
    parser.add_argument("--port", type=int, default=0, help="port to bind (default: any free port)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    args = parser.parse_args(argv)
    server = UIServer(args.port)
    print(f"[modelSpell ui] {server.url}")
    print("[modelSpell ui] this machine only; the address carries a one-time session token. Ctrl-C to stop.")
    if not args.no_browser:
        threading.Timer(0.3, lambda: webbrowser.open(server.url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
