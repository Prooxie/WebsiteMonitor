"""Watch specific elements of web pages and get notified the moment they change.

The package is split into three layers that never import "upward":

``webmonitor.core``
    Pure async monitoring logic - fetching, extracting, diffing, scheduling.
    Has no knowledge of Qt and can run headless.
``webmonitor.notifiers``
    Pluggable delivery channels behind a single :class:`~webmonitor.notifiers.base.Notifier`
    protocol.
``webmonitor.gui``
    PySide6 presentation layer, including the QtWebEngine element inspector.
"""

from __future__ import annotations

__version__ = "0.2.0"
__all__ = ["__version__"]
