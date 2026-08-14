"""PySide6 presentation layer.

Importing this package pulls in Qt, so nothing under :mod:`webmonitor.core` or
:mod:`webmonitor.notifiers` may import from here. The dependency runs one way
only, which is what keeps the monitoring logic headless-testable.
"""

from __future__ import annotations

__all__: list[str] = []
