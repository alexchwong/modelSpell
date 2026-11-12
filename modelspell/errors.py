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
    @property
    def diagnostic(self) -> dict:
        """Bounded structural details safe for verification reports."""
        return getattr(self, "_diagnostic", {"parser": "response rejected", "structure": None,
                                              "finish_reason": None, "reported_provider": None})


class Cancelled(ModelSpellError):
    """The call was cancelled: by a callback raising it, or through a CancelHandle (spec v8 §9.1).

    partial: the Result accumulated before cancellation took effect. sent: False guarantees that no
    byte of the request was written; True means it may have been. Both are None until modelSpell
    has handled the exception.
    """

    def __init__(self, message: str | None = None):
        super().__init__(*([message] if message is not None else []))
        self.partial = None
        self.sent = None
