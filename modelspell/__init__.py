"""ModelSpell: one normalised call shape for LM Studio, OpenRouter and OpenAI-compatible servers."""
from ._complete import check_settings, complete, supported_settings
from ._discovery import list_models, list_providers
from ._version import __version__  # noqa: F401  (single source: _version.py)
from ._types import Endpoint, Message, ModelInfo, Pricing, ProviderInfo, Result, Usage
from .errors import (AuthError, BackendError, Cancelled, ConfigError, ConnectError, ModelSpellError,
                     RequestTimeout, ResponseFormatError, UnsupportedOperation, UnsupportedSetting)

__all__ = [
    "complete", "list_models", "list_providers", "supported_settings", "check_settings",
    "Endpoint", "Message", "Result", "Usage", "ModelInfo", "ProviderInfo", "Pricing",
    "ModelSpellError", "ConfigError", "UnsupportedSetting", "UnsupportedOperation",
    "AuthError", "ConnectError", "RequestTimeout", "BackendError", "ResponseFormatError",
    "Cancelled",
]
