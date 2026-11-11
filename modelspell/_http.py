"""HTTP transport: per-call no-redirect opener, error mapping, redaction (spec §9)."""
from __future__ import annotations

import email.utils
import errno
import functools
import http.client
import json
import os
import selectors
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from ._cancel import _Watch
from .errors import (AuthError, BackendError, ConnectError, ModelSpellError,
                     RequestTimeout, ResponseFormatError)

_VERSION_HINT = {
    "responses": "LM Studio 0.3.29+ is required for /v1/responses",
    "lmstudio_native": "LM Studio 0.4.0+ is required for /api/v1/...",
}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # urllib then raises HTTPError with the 3xx status (§9.5)


@dataclass(frozen=True)
class Call:
    """Per-call context used for requests and for building redacted errors."""
    url: str
    key: str | None
    timeout: float
    model: str | None = None
    route: str | None = None  # "responses" | "lmstudio_native" | None

    def redact(self, text: Any) -> str:
        text = str(text)
        return text.replace(self.key, "***") if self.key else text

    def error(self, cls: type[ModelSpellError], message: str, *, limit: int = 1000,
              body: Any = None, **kw: Any) -> ModelSpellError:
        if body is not None:
            kw["body"] = self.redact(body)[:limit]  # redact first, truncate second
        return cls(self.redact(message), **kw)


# --- the watched transport (spec v8 §9.6) ----------------------------------------------------------
_IN_PROGRESS = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EAGAIN, getattr(errno, "WSAEWOULDBLOCK", -1)}


def _watched_connection(watch: _Watch, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    """socket.create_connection, with each attempt registered and initiated atomically (M30-15)."""
    host, port = address
    wait = None if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout
    err: OSError | None = None
    for af, socktype, proto, _, sa in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
        sock = socket.socket(af, socktype, proto)
        dup = None
        try:
            if source_address:
                sock.bind(source_address)
            dup, code = watch.start_connect(sock, sa)   # _Stop if cancelled: nothing initiated
            if code in _IN_PROGRESS:
                with selectors.DefaultSelector() as sel:  # not select.select: no FD_SETSIZE limit
                    sel.register(sock, selectors.EVENT_WRITE)
                    if not sel.select(wait):
                        raise TimeoutError("timed out")
                code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if code:
                raise OSError(code, os.strerror(code))
            sock.settimeout(wait)                       # back to blocking-with-timeout, as before
            return sock
        except OSError as exc:
            err = exc
            if dup is not None:
                watch.release(dup)
            sock.close()
        except BaseException:
            if dup is not None:
                watch.release(dup)
            sock.close()
            raise
    raise err if err is not None else OSError("getaddrinfo returned no addresses")


class _WatchedConnectionMixin:
    def __init__(self, *args, watch: _Watch, **kwargs):
        super().__init__(*args, **kwargs)
        self._watch = watch
        self._create_connection = functools.partial(_watched_connection, watch)

    def connect(self):
        self._watch.in_connect = True  # proxy tunnel CONNECT bytes are not the request
        try:
            super().connect()
        finally:
            self._watch.in_connect = False

    def send(self, data):
        if self.sock is None and self.auto_open:
            self.connect()  # http.client connects inside send(); connect first so `sent` is exact
        if not self._watch.in_connect and not self._watch.sent:
            self._watch.begin_send()
        return super().send(data)


class _WatchedHTTPConnection(_WatchedConnectionMixin, http.client.HTTPConnection):
    pass


class _WatchedHTTPSConnection(_WatchedConnectionMixin, http.client.HTTPSConnection):
    pass


class _WatchedHandlerMixin:
    def __init__(self, watch: _Watch):
        super().__init__()
        self._watch = watch

    def do_open(self, http_class, req, **kwargs):
        cls = (_WatchedHTTPSConnection if issubclass(http_class, http.client.HTTPSConnection)
               else _WatchedHTTPConnection)
        return super().do_open(functools.partial(cls, watch=self._watch), req, **kwargs)


class _WatchedHTTPHandler(_WatchedHandlerMixin, urllib.request.HTTPHandler):
    pass


class _WatchedHTTPSHandler(_WatchedHandlerMixin, urllib.request.HTTPSHandler):
    pass


def _request(call: Call, headers: dict[str, str], data: bytes | None, extra_headers,
             watch: _Watch | None = None) -> Any:
    hdrs = dict(headers)
    if call.key:
        hdrs["Authorization"] = f"Bearer {call.key}"
    hdrs.update(extra_headers or {})
    req = urllib.request.Request(call.url, data=data, headers=hdrs,
                                 method="POST" if data is not None else "GET")
    if watch is None:  # discovery (MS-6): 0.2.1's opener
        opener = urllib.request.build_opener(_NoRedirectHandler())  # includes ProxyHandler
    else:  # build_opener omits its default HTTP(S) handlers when subclasses are given
        opener = urllib.request.build_opener(_NoRedirectHandler(), _WatchedHTTPHandler(watch),
                                             _WatchedHTTPSHandler(watch))
    try:
        return opener.open(req, timeout=call.timeout)
    except urllib.error.HTTPError as exc:
        raise _http_error(call, exc) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise call.error(RequestTimeout, f"request timed out after {call.timeout}s at {call.url}") from None
        raise call.error(ConnectError, f"endpoint unreachable at {call.url}: {exc.reason}") from None
    except TimeoutError:
        raise call.error(RequestTimeout, f"request timed out after {call.timeout}s at {call.url}") from None
    except (OSError, http.client.HTTPException) as exc:
        raise call.error(ConnectError, f"endpoint unreachable at {call.url}: {exc}") from None


def _http_error(call: Call, exc: urllib.error.HTTPError) -> ModelSpellError:
    code = exc.code
    try:
        body = exc.read(65536).decode("utf-8", errors="replace")
    except Exception:
        body = ""
    finally:
        exc.close()
    if 300 <= code < 400:
        location = (exc.headers.get("Location") if exc.headers else None) or "<no Location header>"
        msg = (f"endpoint redirected (HTTP {code}) from {call.url} to {location}; "
               "redirects are not followed — set base_url to the final URL")
        return call.error(BackendError, msg, status=code, body=body)
    if code in (401, 403):
        return call.error(AuthError, f"authentication failed (HTTP {code}) for {call.url}: "
                          f"{call.redact(body)[:1000]}", status=code)
    if code in (404, 405) and call.route in _VERSION_HINT:
        return call.error(BackendError, f"HTTP {code} at {call.url}: {_VERSION_HINT[call.route]}",
                          status=code, body=body)
    error = call.error(BackendError, f"provider returned HTTP {code} for {call.model!r} at {call.url}: "
                       f"{call.redact(body)[:1000]}", status=code, body=body)
    if code == 429:  # 0.4.0 §5: the origin stays private; only this path marks an error retryable
        error._transport_429 = _TRANSPORT_429
        error._retry_after = retry_after(exc.headers.get("Retry-After") if exc.headers else None)
    return error


# --- HTTP 429 (0.4.0) ---------------------------------------------------------------------------
_TRANSPORT_429 = object()  # identity marker: a BackendError that came from this module's HTTP 429 path
_DIGITS = frozenset("0123456789")


def _now() -> float:
    return time.time()


def retry_after(value: Any) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or HTTP-date), or None when absent or malformed."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 64:
        return None
    if set(value) <= _DIGITS:
        return float(int(value))
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None or when.tzinfo is None:
        return None
    return max(0.0, when.timestamp() - _now())


def is_transport_429(exc: BaseException) -> bool:
    return getattr(exc, "_transport_429", None) is _TRANSPORT_429


def encode(body: dict) -> bytes:
    """The request body of an ordinary completion, encoded once (0.3.0's bytes exactly)."""
    return json.dumps(body).encode("utf-8")


def post(call: Call, body: dict, extra_headers, watch: _Watch | None = None) -> Any:
    return post_data(call, encode(body), extra_headers, watch)


def post_data(call: Call, data: bytes, extra_headers, watch: _Watch | None = None) -> Any:
    """POST a prepared body. Retries reuse the same bytes (0.4.0 §5)."""
    return _request(call, {"Content-Type": "application/json"}, data, extra_headers, watch)


def read_json(call: Call, response, dict_only: bool = True, watch: _Watch | None = None) -> Any:
    try:
        raw = response.read()
    except TimeoutError:
        raise call.error(RequestTimeout, f"request timed out after {call.timeout}s at {call.url}") from None
    except (OSError, http.client.HTTPException) as exc:
        raise call.error(BackendError, f"response read failed for {call.model!r}: {exc}") from None
    if watch is not None:
        watch.commit()  # the whole body read: the non-streaming commit point
    text = raw.decode("utf-8", errors="replace")
    try:
        document = json.loads(text)
    except ValueError:
        document = None
    if document is None or (dict_only and not isinstance(document, dict)):
        raise call.error(ResponseFormatError, "malformed response body",
                         status=response.status, body=text, limit=600) from None
    return document


def get_json(call: Call, extra_headers) -> Any:
    response = _request(call, {"Accept": "application/json"}, None, extra_headers)
    try:
        return read_json(call, response, dict_only=False)
    finally:
        response.close()
