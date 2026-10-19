"""Model and provider discovery (spec §10)."""
from __future__ import annotations

import urllib.parse
from typing import Any

from . import _http
from ._types import Endpoint, ModelInfo, Pricing, ProviderInfo
from .errors import AuthError, ConfigError, ResponseFormatError, UnsupportedOperation


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _str(v: Any) -> str | None:
    return v if isinstance(v, str) else None


def _price(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f >= 0 else None


def _pricing(v: Any) -> Pricing | None:
    return Pricing(_price(v.get("prompt")), _price(v.get("completion")), dict(v)) if isinstance(v, dict) else None


def _list(doc: Any, *path: str) -> list:
    for key in path:
        doc = doc.get(key) if isinstance(doc, dict) else None
    if not isinstance(doc, list):
        raise ResponseFormatError(f"unexpected discovery response shape: expected list at {'.'.join(path)}",
                                  status=200)
    return doc


def _model(id, raw, **kw) -> ModelInfo:
    fields = dict(name=None, context_length=None, loaded=None, supports_tools=None,
                  supports_reasoning=None, reasoning_options=None, pricing=None)
    fields.update(kw)
    return ModelInfo(id=id, raw=raw, **fields)


def _call(endpoint: Endpoint, url: str, timeout: float, route: str | None = None) -> _http.Call:
    if not (isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0):
        raise ConfigError("timeout must be a number > 0")
    return _http.Call(url=url, key=endpoint._resolve_key(), timeout=timeout, route=route)


def list_models(endpoint: Endpoint, *, timeout: float = 15.0) -> list[ModelInfo]:
    out: list[ModelInfo] = []
    if endpoint.kind == "openrouter":
        doc = _http.get_json(_call(endpoint, f"{endpoint.base_url}/models", timeout), endpoint.headers)
        for row in _list(doc, "data"):
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                continue
            params = row.get("supported_parameters")
            params = params if isinstance(params, list) else None
            out.append(_model(row["id"], row, name=_str(row.get("name")),
                              context_length=_int(row.get("context_length")),
                              supports_tools=None if params is None else "tools" in params,
                              supports_reasoning=None if params is None else "reasoning" in params,
                              pricing=_pricing(row.get("pricing"))))
    elif endpoint.kind == "lmstudio":
        call = _call(endpoint, f"{endpoint._native_root}/api/v1/models", timeout, route="lmstudio_native")
        for row in _list(_http.get_json(call, endpoint.headers), "models"):
            if not isinstance(row, dict) or row.get("type") != "llm" or not isinstance(row.get("key"), str) \
                    or not row["key"]:
                continue
            caps = row.get("capabilities") if isinstance(row.get("capabilities"), dict) else {}
            reasoning = caps.get("reasoning") if isinstance(caps.get("reasoning"), dict) else {}
            opts = reasoning.get("allowed_options")
            opts = tuple(str(o) for o in opts) if isinstance(opts, list) else None
            instances = row.get("loaded_instances")
            tools = caps.get("trained_for_tool_use")
            out.append(_model(row["key"], row, name=_str(row.get("display_name")),
                              context_length=_int(row.get("max_context_length")),
                              loaded=len(instances) > 0 if isinstance(instances, list) else None,
                              supports_tools=tools if isinstance(tools, bool) else None,
                              supports_reasoning=opts is not None and any(o != "off" for o in opts),
                              reasoning_options=opts))
    else:  # openai_compat: port of NEL _ids()
        doc = _http.get_json(_call(endpoint, f"{endpoint.base_url}/models", timeout), endpoint.headers)
        rows = doc.get("data") if isinstance(doc, dict) else doc
        if not isinstance(rows, list):
            raise ResponseFormatError("unexpected discovery response shape: expected a model list", status=200)
        for row in rows:
            value = (row.get("id") or row.get("slug") or row.get("name")) if isinstance(row, dict) else row
            if isinstance(value, str) and value.strip():
                out.append(_model(value.strip(), row if isinstance(row, dict) else {"id": row}))
    seen: dict[str, ModelInfo] = {}
    for m in out:
        seen.setdefault(m.id, m)
    return sorted(seen.values(), key=lambda m: m.id)


def list_providers(endpoint: Endpoint, model_id: str, *, timeout: float = 15.0) -> list[ProviderInfo]:
    if endpoint.kind != "openrouter":
        raise UnsupportedOperation("list_providers is only supported on openrouter")
    if not isinstance(model_id, str) or "/" not in model_id or not all(model_id.split("/", 1)):
        raise ConfigError("model_id must have the form 'author/slug'")
    if not endpoint._resolve_key():
        raise AuthError(f"no API key: set api_key or the {endpoint.api_key_env} environment variable")
    author, slug = (urllib.parse.quote(p, safe=":-._~") for p in model_id.split("/", 1))
    call = _call(endpoint, f"{endpoint.base_url}/models/{author}/{slug}/endpoints", timeout)
    out = []
    for row in _list(_http.get_json(call, endpoint.headers), "data", "endpoints"):
        if not isinstance(row, dict):
            continue
        name = next((v for v in (row.get("provider_name"), row.get("name")) if isinstance(v, str) and v), None)
        if name is None:
            continue
        tag = row.get("tag")
        params = row.get("supported_parameters")
        out.append(ProviderInfo(
            name=name, slug=tag if isinstance(tag, str) and tag else None,
            context_length=_int(row.get("context_length")),
            max_completion_tokens=_int(row.get("max_completion_tokens")),
            quantization=_str(row.get("quantization")), pricing=_pricing(row.get("pricing")),
            supported_parameters=tuple(p for p in params if isinstance(p, str)) if isinstance(params, list) else (),
            raw=row))
    return out
