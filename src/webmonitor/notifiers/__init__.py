"""Pluggable notification channels.

Every channel implements :class:`~webmonitor.notifiers.base.Notifier` and is
registered with the :class:`~webmonitor.notifiers.dispatcher.Dispatcher`. Adding
a channel - SMS through Twilio, a Discord or Slack webhook, ntfy, a desktop
webhook - means writing one class and registering it; nothing else changes.

None of these modules import Qt. The desktop channel receives a *sink* callable
from the GUI instead, which keeps the whole package headless-testable.
"""

from __future__ import annotations

from webmonitor.notifiers.base import Notifier, NotifierError
from webmonitor.notifiers.desktop import DesktopNotifier
from webmonitor.notifiers.dispatcher import Dispatcher
from webmonitor.notifiers.email_notifier import EmailNotifier
from webmonitor.notifiers.telegram import TelegramNotifier

__all__ = [
    "DesktopNotifier",
    "Dispatcher",
    "EmailNotifier",
    "Notifier",
    "NotifierError",
    "TelegramNotifier",
]
