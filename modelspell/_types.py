"""Public data types (spec §4, §5, §8.1, §10.1)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal, Mapping

from .errors import ConfigError

KINDS = ("lmstudio", "openrouter", "openai_compat")
ROLES = ("system", "user", "assistant")
_DEFAULT_URL = {"lmstudio": "http://localhost:1234/v1", "openrouter": "https://openrouter.ai/api/v1"}


@dataclass(frozen=True)
class Endpoint:
    kind: Literal["lmstudio", "openrouter", "openai_compat"]
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    headers: Mapping[str, str] | None = field(default=None, hash=False)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ConfigError(f"kind must be one of {KINDS}, got {self.kind!r}")
        url = self.base_url
        if url is None:
            if self.kind == "openai_compat":
                raise ConfigError("openai_compat requires base_url")
            url = _DEFAULT_URL[self.kind]
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ConfigError("base_url must start with http:// or https://")
        object.__setattr__(self, "base_url", url.rstrip("/"))
        if self.api_key_env is None and self.kind == "openrouter":
            object.__setattr__(self, "api_key_env", "OPENROUTER_API_KEY")
        if self.headers is not None:
            headers = dict(self.headers)
            if any(str(k).lower() == "authorization" for k in headers):
                raise ConfigError("set the key through api_key, not headers['Authorization']")
            object.__setattr__(self, "headers", headers)

    def __repr__(self) -> str:
        key = "'***'" if self.api_key else "None"
        return (f"Endpoint(kind={self.kind!r}, base_url={self.base_url!r}, api_key={key}, "
                f"api_key_env={self.api_key_env!r}, headers={self.headers!r})")

    def _resolve_key(self) -> str | None:
        if isinstance(self.api_key, str) and self.api_key.strip():
            return self.api_key.strip()
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "").strip() or None
        return None

    @property
    def _native_root(self) -> str:
        return self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant"]
    content: str

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ConfigError(f"message role must be one of {ROLES}, got {self.role!r}")
        if not isinstance(self.content, str):
            raise ConfigError("message content must be a str")


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    reasoning_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None


@dataclass(frozen=True)
class Result:
    text: str
    reasoning: str | None
    usage: Usage | None
    cost: float | None
    finish_reason: str | None
    truncated: bool
    model: str | None
    provider: str | None
    response_id: str | None
    transport: Literal["chat_completions", "responses", "lmstudio_native"]
    reasoning_details: tuple[dict, ...] | None = field(hash=False)
    raw_usage: dict | None = field(hash=False)


@dataclass(frozen=True)
class Pricing:
    prompt: float | None
    completion: float | None
    raw: dict = field(hash=False)


@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str | None
    context_length: int | None
    loaded: bool | None
    supports_tools: bool | None
    supports_reasoning: bool | None
    reasoning_options: tuple[str, ...] | None
    pricing: Pricing | None
    raw: dict = field(hash=False)


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    slug: str | None
    context_length: int | None
    max_completion_tokens: int | None
    quantization: str | None
    pricing: Pricing | None
    supported_parameters: tuple[str, ...]
    raw: dict = field(hash=False)
