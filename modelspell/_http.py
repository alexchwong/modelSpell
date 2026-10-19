"""HTTP transport: per-call no-redirect opener, error mapping, redaction (spec §9)."""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

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


def _request(call: Call, headers: dict[str, str], data: bytes | None, extra_headers) -> Any:
    hdrs = dict(headers)
    if call.key:
        hdrs["Authorization"] = f"Bearer {call.key}"
    hdrs.update(extra_headers or {})
    req = urllib.request.Request(call.url, data=data, headers=hdrs,
                                 method="POST" if data is not None else "GET")
    opener = urllib.request.build_opener(_NoRedirectHandler())  # includes ProxyHandler
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
    return call.error(BackendError, f"provider returned HTTP {code} for {call.model!r} at {call.url}: "
                      f"{call.redact(body)[:1000]}", status=code, body=body)


def post(call: Call, body: dict, extra_headers) -> Any:
    return _request(call, {"Content-Type": "application/json"},
                    json.dumps(body).encode("utf-8"), extra_headers)


def read_json(call: Call, response, dict_only: bool = True) -> Any:
    try:
        text = response.read().decode("utf-8", errors="replace")
    except TimeoutError:
        raise call.error(RequestTimeout, f"request timed out after {call.timeout}s at {call.url}") from None
    except (OSError, http.client.HTTPException) as exc:
        raise call.error(BackendError, f"response read failed for {call.model!r}: {exc}") from None
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
