"""ModelSpell: one normalised call shape for LM Studio, OpenRouter and OpenAI-compatible servers."""
from ._complete import complete
from ._discovery import list_models, list_providers
from ._version import __version__  # noqa: F401  (single source: _version.py)
from ._types import Endpoint, Message, ModelInfo, Pricing, ProviderInfo, Result, Usage
from .errors import (AuthError, BackendError, Cancelled, ConfigError, ConnectError, ModelSpellError,
                     RequestTimeout, ResponseFormatError, UnsupportedOperation, UnsupportedSetting)

__all__ = [
    "complete", "list_models", "list_providers",
    "Endpoint", "Message", "Result", "Usage", "ModelInfo", "ProviderInfo", "Pricing",
    "ModelSpellError", "ConfigError", "UnsupportedSetting", "UnsupportedOperation",
    "AuthError", "ConnectError", "RequestTimeout", "BackendError", "ResponseFormatError",
    "Cancelled",
]
