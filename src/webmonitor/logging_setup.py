"""Logging configuration: rotating file output plus a console stream.

Every log record carries the target id when one is in scope, which is what makes
a multi-target log readable after the fact.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from typing import Final

from webmonitor import paths

__all__ = ["GuiLogHandler", "setup_logging"]

_FILE_FORMAT: Final[str] = "%(asctime)s | %(levelname)-8s | %(name)-32s | %(message)s"
_CONSOLE_FORMAT: Final[str] = "%(levelname)-8s | %(name)-24s | %(message)s"
_MAX_BYTES: Final[int] = 5 * 1024 * 1024
_BACKUP_COUNT: Final[int] = 5


class GuiLogHandler(logging.Handler):
    """A handler that forwards formatted records to a callback.

    The GUI installs one of these to mirror the log into its Activity tab. The
    callback is invoked on whichever thread emitted the record, so a Qt consumer
    must marshal to the GUI thread with a queued signal.
    """

    def __init__(self, callback: object, level: int = logging.INFO) -> None:
        """Initialise the handler.

        Args:
            callback: A one-argument callable receiving the formatted string.
            level: Minimum level to forward.
        """
        super().__init__(level)
        self._callback = callback
        self.setFormatter(logging.Formatter(_CONSOLE_FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        """Format a record and hand it to the callback.

        Args:
            record: The record to forward. Any exception raised by the callback
                is swallowed via :meth:`handleError`, because a failing log sink
                must never break the code that logged.
        """
        try:
            self._callback(self.format(record))  # type: ignore[operator]
        except Exception:  # pragma: no cover - defensive
            self.handleError(record)


def setup_logging(level: str = "INFO", *, console: bool = True) -> None:
    """Configure the root logger.

    Safe to call more than once; existing handlers are cleared first so repeated
    calls do not duplicate every line.

    Args:
        level: Level name such as ``"DEBUG"`` or ``"INFO"``. An unrecognised
            name falls back to ``INFO``.
        console: Whether to also log to stderr. Disabled for windowed builds
            where there is no console attached.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(numeric_level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    file_handler = logging.handlers.RotatingFileHandler(
        paths.log_file(),
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
    file_handler.setLevel(numeric_level)
    root.addHandler(file_handler)

    if console and sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
        stream.setLevel(numeric_level)
        root.addHandler(stream)

    # These are chatty at DEBUG and drown out our own records.
    for noisy in ("httpx", "httpcore", "hpack", "asyncio", "keyring"):
        logging.getLogger(noisy).setLevel(max(numeric_level, logging.WARNING))

    logging.getLogger(__name__).info("Logging initialised at %s -> %s", level, paths.log_file())
