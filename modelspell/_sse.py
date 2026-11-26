"""Shared SSE line handling (spec §8.9)."""
from __future__ import annotations

import http.client
import json
import time
from typing import Iterator

from ._cancel import _Watch
from ._http import Call
from .errors import BackendError, RequestTimeout, ResponseFormatError


def iter_sse(call: Call, response, watch: _Watch | None = None) -> Iterator[dict | str]:
    """Yield "[DONE]" or one JSON object per `data:` line. Malformed data raises.

    End of input commits the call (spec v8 §6.10) when a watch is given; if cancellation took
    effect first, the commit raises instead and complete() reports Cancelled.

    With `call.idle_timeout` set, a stream that delivers no `data:` event for that many seconds raises
    RequestTimeout even while the server keeps it open with comment lines. OpenRouter sends `: OPENROUTER
    PROCESSING` comments while a request waits on its upstream, and each one restarts the socket read timeout,
    so without this a stalled upstream holds the call until the caller's own deadline.
    """
    lines = iter(response)
    idle = call.idle_timeout
    last_data = time.monotonic()
    while True:
        try:
            raw_line = next(lines)
        except StopIteration:
            if watch is not None:
                watch.commit()
            return
        except TimeoutError:
            raise call.error(RequestTimeout, f"request timed out after {call.timeout}s at {call.url}") from None
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise call.error(BackendError, f"stream failed for {call.model!r}: {exc}") from None
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line or line.startswith(":") or not line.startswith("data:"):
            if idle is not None and time.monotonic() - last_data > idle:
                raise call.error(RequestTimeout, f"stream idle: no data for {idle}s (keep-alives only) at {call.url}")
            continue
        data = line[5:].strip()
        if not data:
            continue
        last_data = time.monotonic()
        if data == "[DONE]":
            yield data
            continue
        try:
            document = json.loads(data)
        except ValueError:
            document = None
        if not isinstance(document, dict):
            raise call.error(ResponseFormatError, "malformed SSE data event", status=200, body=data, limit=600)
        yield document
