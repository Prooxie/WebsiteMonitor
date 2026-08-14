"""Native desktop toast notifications.

Qt's :class:`QSystemTrayIcon` already speaks the platform's native notification
API - Action Center on Windows, Notification Center on macOS, libnotify on
Linux - so no extra dependency is needed.

To keep this package free of Qt imports, the GUI injects a *sink* callable that
performs the actual ``showMessage`` call. Without a sink the channel degrades to
a log line rather than failing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from webmonitor.notifiers.base import Notifier

if TYPE_CHECKING:
    from webmonitor.config import DesktopConfig
    from webmonitor.models import ChangeEvent

__all__ = ["DesktopNotifier", "ToastSink"]

logger = logging.getLogger(__name__)

#: ``(title, body, duration_ms) -> None``, supplied by the GUI layer.
ToastSink = Callable[[str, str, int], None]


class DesktopNotifier(Notifier):
    """Shows a native toast when a watched element changes."""

    name = "desktop"
    label = "Desktop notification"

    def __init__(self, config: DesktopConfig, sink: ToastSink | None = None) -> None:
        """Initialise the channel.

        Args:
            config: Desktop notification settings.
            sink: Callable that displays the toast. Normally installed later by
                the GUI via :meth:`set_sink`.
        """
        self._config = config
        self._sink = sink

    def set_sink(self, sink: ToastSink | None) -> None:
        """Install the callable that actually displays toasts.

        Args:
            sink: The display callable, or ``None`` to detach it during shutdown.
        """
        self._sink = sink

    @property
    def is_configured(self) -> bool:
        """Whether toasts can be displayed.

        Returns:
            ``True`` when the channel is enabled and a sink is installed.
        """
        return bool(self._config.enabled and self._sink is not None)

    def describe_status(self) -> str:
        """Explain the channel's state.

        Returns:
            A short status string for the settings dialog.
        """
        if not self._config.enabled:
            return "Disabled"
        if self._sink is None:
            return "Unavailable (no system tray)"
        return "Ready"

    async def send(self, event: ChangeEvent) -> None:
        """Display a toast describing the change.

        Args:
            event: The detected change.

        Note:
            Never raises. A failed toast is a cosmetic problem and must not
            abort delivery through the other channels.
        """
        if not self.is_configured:
            logger.debug("Desktop notifications unavailable; skipping toast")
            return

        title = f"Change detected: {event.target.name}"
        body = f"{event.target.url}\n+{event.added_lines} added, -{event.removed_lines} removed"

        try:
            assert self._sink is not None  # narrowed by is_configured
            self._sink(title, body, self._config.duration_ms)
            logger.debug("Desktop toast shown for %s", event.target.name)
        except Exception:
            logger.exception("Failed to show desktop toast")
