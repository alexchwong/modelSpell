"""Time bounds shared by completion, discovery and provider selection."""
from typing import Any

from .errors import ConfigError

_COMPLETION_TIMEOUT = 900.0
_MAX_RETRY_WAIT = 30.0
_TIME_CAP = 1_000_000


def _check_timeout(timeout: Any) -> None:
    if not (isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
            and 0 < timeout <= _TIME_CAP):
        raise ConfigError(f"timeout must be a finite number > 0 and <= {_TIME_CAP} seconds")


def _check_retry_wait(max_retry_wait: Any) -> None:
    if not (isinstance(max_retry_wait, (int, float)) and not isinstance(max_retry_wait, bool)
            and 0 <= max_retry_wait <= _TIME_CAP):
        raise ConfigError(f"max_retry_wait must be a finite number between 0 and {_TIME_CAP} seconds")
