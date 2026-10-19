"""Shared SSE line handling (spec §8.9)."""
from __future__ import annotations

import http.client
import json
from typing import Iterator

from ._http import Call
from .errors import BackendError, RequestTimeout, ResponseFormatError


def iter_sse(call: Call, response) -> Iterator[dict | str]:
    """Yield "[DONE]" or one JSON object per `data:` line. Malformed data raises."""
    lines = iter(response)
    while True:
        try:
            raw_line = next(lines)
        except StopIteration:
            return
        except TimeoutError:
            raise call.error(RequestTimeout, f"request timed out after {call.timeout}s at {call.url}") from None
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise call.error(BackendError, f"stream failed for {call.model!r}: {exc}") from None
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line or line.startswith(":") or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data:
            continue
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
