"""The contract every notification channel implements."""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from webmonitor.models import ChangeEvent

__all__ = ["Notifier", "NotifierError"]


class NotifierError(Exception):
    """Delivery through a channel failed.

    Raised rather than swallowed so the dispatcher can report exactly which
    channel failed and why, without one broken channel suppressing the others.
    """


class Notifier(abc.ABC):
    """Abstract base for a delivery channel.

    Subclasses must be safe to call from an asyncio event loop. Channels backed
    by blocking libraries (``smtplib``, for instance) should offload their work
    with :func:`asyncio.to_thread`.
    """

    #: Stable machine name, used in settings and in per-target channel lists.
    name: str = "base"

    #: Human-readable label for the GUI.
    label: str = "Base"

    @property
    @abc.abstractmethod
    def is_configured(self) -> bool:
        """Whether this channel has everything it needs to deliver.

        Returns:
            ``True`` when enabled and fully configured, including any secret
            that lives in the keyring.
        """

    @abc.abstractmethod
    async def send(self, event: ChangeEvent) -> None:
        """Deliver a change notification.

        Args:
            event: The detected change.

        Raises:
            NotifierError: If delivery failed.
        """

    async def send_test(self) -> None:
        """Deliver a test message so the user can verify their settings.

        The default implementation synthesises a fake change event and sends it
        through :meth:`send`.

        Raises:
            NotifierError: If delivery failed.
        """
        from webmonitor.models import ChangeEvent, Target

        event = ChangeEvent(
            target=Target(
                name="Test notification",
                url="https://example.com",
                selector="div.example",
            ),
            old_content="This is the previous content.",
            new_content="This is the current content, and it has changed.",
            added_lines=1,
            removed_lines=1,
        )
        await self.send(event)

    def describe_status(self) -> str:
        """Explain in one line why the channel is or is not usable.

        Returns:
            A short status string for the settings dialog.
        """
        return "Ready" if self.is_configured else "Not configured"
