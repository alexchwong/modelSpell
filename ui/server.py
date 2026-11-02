"""Developer test UI for modelSpell. Not part of the library and never shipped in the wheel.

Run from the repository root:  python -m ui [--port N] [--no-browser]

Binds to 127.0.0.1 only. Every request must carry the per-launch session token (the page URL's
`t` parameter; API calls send it as X-Session-Token), so other web pages in the browser cannot
drive the server or spend API credit. API keys are write-only: held in this process's memory,
keyed by (kind, base_url) so a key is never sent to a different endpoint, and never returned.

Connection settings (model source, base URL, key, model, OpenRouter provider) are held once per
process. Generation settings are not stored here: the page sends them with each /api/complete
request (spec v6 §18.3).

Stop cancels the running request through a modelspell.CancelHandle (POST /api/complete/stop), so it
acts at any stage, including before the first token. The owner checks (ui/owner_checks.py) run from
POST /api/checks; at most one battery runs at a time, and its slot stays occupied until every
worker thread has exited (spec v8 §18.3).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import modelspell
from modelspell import CancelHandle, Cancelled, Endpoint, ModelSpellError

from . import owner_checks

INDEX = pathlib.Path(__file__).with_name("index.html")
DEFAULT_SETTINGS = {"kind": "lmstudio", "base_url": "", "model": "", "provider": ""}
# The generation arguments the page may send with /api/complete. provider comes from the dialog.
PAGE_SETTINGS = ("reasoning", "max_tokens", "temperature", "top_p", "top_k", "min_p", "repetition_penalty",
                 "presence_penalty", "frequency_penalty", "seed", "stop")
MOVED = ("reasoning", "max_tokens")  # accepted by /api/config before 0.2.0; now per request
LEVELS = ("", "off", "low", "medium", "high")  # "" stands for None (backend default)


class UIServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int = 0, battery_options: dict | None = None):
        super().__init__(("127.0.0.1", port), Handler)
        self.token = secrets.token_urlsafe(24)
        self.settings = dict(DEFAULT_SETTINGS)
        self.keys: dict[tuple[str, str], str] = {}
        self.index = INDEX.read_bytes()
        self.lock = threading.Lock()
        self.handles: dict[str, CancelHandle] = {}   # running /api/complete requests, by request ID
        self.battery: owner_checks.Battery | None = None
        self.battery_options = dict(battery_options or {})  # test-only: timing, triggers, complete stand-ins

    def checks_running(self) -> bool:
        """The owner-check slot is occupied until the battery has finished (every worker exited)."""
        b = self.battery
        return b is not None and not b.finished

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
                  "/api/providers": self._providers, "/api/complete": self._complete,
                  "/api/complete/stop": self._complete_stop, "/api/checks": self._checks,
                  "/api/checks/stop": self._checks_stop}
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
        kind = s.settings["kind"]
        levels = ("",) if kind == "openai_compat" else LEVELS
        supported = {lv: sorted(modelspell.supported_settings(kind, lv or None)) for lv in levels}
        b = s.battery
        running = s.checks_running()
        return {**s.settings, "key_set": key_set, "supported": supported, "version": modelspell.__version__,
                "presets": {name: owner_checks.preset_info(name) for name in owner_checks.PRESETS},
                "default_preset": owner_checks.DEFAULT_PRESET, "checks_running": running,
                "checks_stuck": bool(running and b is not None and b.ended)}

    def _config(self, body: dict):
        s = self.server
        moved = [k for k in MOVED if k in body]
        if moved:
            return self._send(400, {"error": f"{', '.join(moved)}: moved to the request panel; "
                                             "send it with /api/complete"})
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
        settings = body.get("settings", {})
        if not isinstance(settings, dict):
            return self._send(400, {"error": "settings must be a JSON object"})
        unknown = sorted(set(settings) - set(PAGE_SETTINGS))
        if unknown:
            return self._send(400, {"error": f"unknown settings: {', '.join(unknown)}"})
        endpoint = self.server.endpoint({})
        messages = ([{"role": "system", "content": body["system"]}] if body.get("system") else []) + \
            [{"role": "user", "content": str(body.get("prompt") or "")}]
        # Values pass to complete() unchanged; only null/absent are dropped and reasoning "" means None.
        # Never `value or None`: that would drop explicit zeros (U10). modelSpell does all value checks.
        kwargs = {k: v for k, v in settings.items() if v is not None and not (k == "reasoning" and v == "")}
        if s["provider"] and endpoint.kind == "openrouter":
            kwargs["provider"] = {"order": [s["provider"]], "allow_fallbacks": False}
        handle, request_id = CancelHandle(), secrets.token_urlsafe(9)
        with self.server.lock:
            self.server.handles[request_id] = handle
        try:
            self._stream_complete(endpoint, s["model"], messages, kwargs, handle, request_id)
        finally:
            with self.server.lock:
                self.server.handles.pop(request_id, None)

    def _ndjson_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _stream_complete(self, endpoint, model, messages, kwargs, handle, request_id):
        self._ndjson_start()
        lock = threading.Lock()

        def emit(event: dict):
            try:
                with lock:
                    self.wfile.write(json.dumps(event).encode() + b"\n")
                    self.wfile.flush()
            except OSError:
                raise Cancelled("browser disconnected") from None  # closes the upstream request

        try:
            emit({"type": "started", "id": request_id})
            result = modelspell.complete(endpoint, model, messages,
                                         on_text=lambda t: emit({"type": "text", "text": t}),
                                         on_reasoning=lambda t: emit({"type": "reasoning", "text": t}),
                                         cancel=handle, **kwargs)
            # settings_sent: what was passed to complete() and sent, not a statement of what was applied
            emit({"type": "result", "result": _jsonable(result), "settings_sent": kwargs})
        except Cancelled as exc:
            if not handle.cancelled:
                return  # the browser disconnected: nothing to tell it
            try:
                emit({"type": "cancelled", "sent": exc.sent, "partial": _jsonable(exc.partial)})
            except Cancelled:
                pass
        except ModelSpellError as exc:
            try:
                emit({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
            except Cancelled:
                pass

    def _complete_stop(self, body: dict):
        request_id = body.get("id")
        if not isinstance(request_id, str) or not request_id:
            return self._send(400, {"error": "id is required"})
        with self.server.lock:
            handle = self.server.handles.get(request_id)
        if handle is None:
            return self._send(200, {"already_finished": True})
        handle.cancel()
        self._send(200, {"stopping": True})

    # --- owner checks (techspec 0.3.0 §10.2) ---------------------------------------------------

    def _checks(self, body: dict):
        srv = self.server
        preset = body.get("preset") or owner_checks.DEFAULT_PRESET
        route = body.get("route") or "default"
        if preset not in owner_checks.PRESETS:
            return self._send(400, {"error": f"unknown preset {preset!r}"})
        if route not in owner_checks.ROUTES:
            return self._send(400, {"error": f"unknown route {route!r}"})
        endpoint = srv.endpoint({})
        if route == "native" and endpoint.kind != "lmstudio":
            return self._send(400, {"error": "the native route exists only for LM Studio"})
        if not srv.settings["model"]:
            return self._send(400, {"error": "choose a model in Model settings first"})
        with srv.lock:
            if srv.checks_running():
                b = srv.battery
                msg = ("a request from an earlier run is still shutting down; a new run can start once it has"
                       if b is not None and b.ended else "owner checks are already running")
                return self._send(409, {"error": msg})
            battery = owner_checks.Battery(endpoint, srv.settings["model"], preset, route=route,
                                           provider_pin=srv.settings["provider"] or None, **srv.battery_options)
            srv.battery = battery
            battery.start()
        self._ndjson_start()
        try:
            while True:
                ev = battery.next_event(1.0)
                if ev is None:
                    cur = battery.current
                    if cur is None or battery.ended:
                        continue
                    ev = {"type": "progress", "check": cur["check"],
                          "elapsed_s": round(time.monotonic() - cur["started"], 1)}
                self.wfile.write(json.dumps(ev).encode() + b"\n")
                self.wfile.flush()
                if ev["type"] == "done":
                    return
        except OSError:
            battery.stop()  # the browser went away: noticed at the next write, about once a second

    def _checks_stop(self, body: dict):
        b = self.server.battery
        if b is None or not b.stop():
            return self._send(200, {"already_finished": True})
        self._send(200, {"stopping": True})


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
