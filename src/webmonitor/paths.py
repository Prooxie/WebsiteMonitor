"""Cross-platform application directories.

Implemented locally rather than via ``platformdirs`` to keep the dependency set
small; the core layer must stay importable without Qt, so ``QStandardPaths`` is
not an option here either.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Final

__all__ = ["APP_NAME", "config_file", "data_dir", "database_file", "log_dir", "log_file"]

APP_NAME: Final[str] = "WebsiteMonitor"

#: Set ``WM_DATA_DIR`` to relocate all state - handy for tests and portable installs.
_ENV_OVERRIDE: Final[str] = "WM_DATA_DIR"


def data_dir() -> Path:
    """Return the directory holding config, database and logs, creating it.

    Honours ``WM_DATA_DIR`` when set. Otherwise uses ``%LOCALAPPDATA%`` on
    Windows, ``~/Library/Application Support`` on macOS and
    ``$XDG_DATA_HOME`` (falling back to ``~/.local/share``) elsewhere.

    Returns:
        An existing directory path.
    """
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        path = Path(override).expanduser()
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        path = Path(base) / APP_NAME
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        path = Path(base) / APP_NAME

    path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir() -> Path:
    """Return the log directory, creating it if needed.

    Returns:
        An existing ``logs`` directory inside :func:`data_dir`.
    """
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_file() -> Path:
    """Return the path of the JSON settings file.

    Returns:
        Path to ``settings.json``. The file may not exist yet.
    """
    return data_dir() / "settings.json"


def database_file() -> Path:
    """Return the path of the SQLite database.

    Returns:
        Path to ``monitor.db``. The file may not exist yet.
    """
    return data_dir() / "monitor.db"


def log_file() -> Path:
    """Return the path of the rotating application log.

    Returns:
        Path to ``webmonitor.log``.
    """
    return log_dir() / "webmonitor.log"
