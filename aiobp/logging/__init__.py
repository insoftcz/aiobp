"""Logger with color support"""

import contextlib
from types import TracebackType
from typing import Optional, Union

from . import log
from .custom import LoggingConfig, add_devel_log_level, setup_logging

__all__ = ["LoggingConfig", "add_devel_log_level", "log", "setup_logging", "suppress_and_log"]


_MuteArg = Optional[Union[type[BaseException], tuple[type[BaseException], ...], list[type[BaseException]]]]


class suppress_and_log(contextlib.suppress):  # noqa: N801
    """Suppress exception and log trace"""

    def __init__(  # pyright: ignore[reportMissingSuperCall]
        self,
        *exceptions: type[BaseException],
        mute: _MuteArg = None,
        message: str = "Suppressed exception",
    ) -> None:
        self._exceptions: tuple[type[BaseException], ...] = exceptions
        self._mute: tuple[type[BaseException], ...] = (
            () if mute is None
            else (mute,) if isinstance(mute, type)
            else tuple(mute)
        )
        self._message: str = message

    def __exit__(
        self,
        exctype: Optional[type[BaseException]],
        excinst: Optional[BaseException],
        exctb: Optional[TracebackType],
    ) -> bool:
        """Check for exception when code execution leaves context"""
        suppress = exctype is not None and issubclass(exctype, self._exceptions)
        if suppress and not issubclass(exctype, self._mute):
            log.exception(self._message)
        return suppress
