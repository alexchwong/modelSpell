"""Exception hierarchy (spec §9.1)."""
from __future__ import annotations


class ModelSpellError(Exception):
    pass


class ConfigError(ModelSpellError):
    pass


class UnsupportedSetting(ModelSpellError):
    pass


class UnsupportedOperation(ModelSpellError):
    pass


class AuthError(ModelSpellError):
    def __init__(self, message: str = "", status: int | None = None):
        super().__init__(message)
        self.status = status


class ConnectError(ModelSpellError):
    pass


class RequestTimeout(ModelSpellError):
    pass


class BackendError(ModelSpellError):
    def __init__(self, message: str = "", status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class ResponseFormatError(BackendError):
    pass


class Cancelled(ModelSpellError):
    def __init__(self, message: str | None = None):
        super().__init__(*([message] if message is not None else []))
        self.partial = None
