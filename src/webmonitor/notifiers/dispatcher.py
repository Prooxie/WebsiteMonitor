"""Fan-out delivery across notification channels.

Two properties matter here:

**Isolation.** Channels are delivered concurrently and their failures are
independent. A dead SMTP server must not stop the Telegram message, so results
are gathered with ``return_exceptions=True`` and reported per channel.

**Suppression.** A page that flaps between two states would otherwise generate
an unbounded stream of alerts. A per-target cooldown collapses repeat
notifications within a short window.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass

from webmonitor.models import ChangeEvent, utc_now
from webmonitor.notifiers.base import Notifier, NotifierError

__all__ = ["DeliveryReport", "Dispatcher"]

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class DeliveryReport:
    """Outcome of dispatching one event across channels.

    Attributes:
        delivered: Names of channels that accepted the message.
        failed: Mapping of channel name to the error text it produced.
        skipped: Names of channels that were not configured or not selected.
    """

    delivered: tuple[str, ...] = ()
    failed: dict[str, str] | None = None
    skipped: tuple[str, ...] = ()

    @property
    def any_delivered(self) -> bool:
        """Whether at least one channel accepted the message.

        Returns:
            ``True`` if ``delivered`` is non-empty.
        """
        return bool(self.delivered)

    def summary(self) -> str:
        """Render a one-line summary for the log.

        Returns:
            Text such as ``"delivered: email, telegram; failed: desktop"``.
        """
        parts: list[str] = []
        if self.delivered:
            parts.append(f"delivered: {', '.join(self.delivered)}")
        if self.failed:
            parts.append(f"failed: {', '.join(self.failed)}")
        if self.skipped:
            parts.append(f"skipped: {', '.join(self.skipped)}")
        return "; ".join(parts) or "no channels"


class Dispatcher:
    """Routes change events to the configured notification channels."""

    def __init__(
        self,
        notifiers: Iterable[Notifier] = (),
        *,
        cooldown_seconds: float = 60.0,
    ) -> None:
        """Initialise the dispatcher.

        Args:
            notifiers: Channels to register up front.
            cooldown_seconds: Minimum gap between notifications for the same
                target. Set to ``0`` to disable suppression.
        """
        self._notifiers: dict[str, Notifier] = {n.name: n for n in notifiers}
        self._cooldown = cooldown_seconds
        self._last_sent: dict[str, float] = {}

    def register(self, notifier: Notifier) -> None:
        """Add or replace a channel.

        Args:
            notifier: The channel to register, keyed by its ``name``.
        """
        self._notifiers[notifier.name] = notifier
        logger.debug("Registered notifier %r", notifier.name)

    def get(self, name: str) -> Notifier | None:
        """Look up a channel by name.

        Args:
            name: The channel's ``name`` attribute.

        Returns:
            The channel, or ``None`` if it is not registered.
        """
        return self._notifiers.get(name)

    @property
    def channels(self) -> tuple[Notifier, ...]:
        """All registered channels.

        Returns:
            A tuple of channels in registration order.
        """
        return tuple(self._notifiers.values())

    def _select(self, event: ChangeEvent) -> tuple[list[Notifier], list[str]]:
        """Decide which channels should receive an event.

        Args:
            event: The change being dispatched.

        Returns:
            A tuple of the channels to use and the names of those skipped.
        """
        requested = set(event.target.channels)
        chosen: list[Notifier] = []
        skipped: list[str] = []

        for notifier in self._notifiers.values():
            # An empty per-target channel list means "everything configured".
            if requested and notifier.name not in requested:
                skipped.append(notifier.name)
                continue
            if not notifier.is_configured:
                skipped.append(notifier.name)
                continue
            chosen.append(notifier)

        return chosen, skipped

    def _in_cooldown(self, target_id: str) -> bool:
        """Check whether a target is still inside its notification cooldown.

        Args:
            target_id: The target's id.

        Returns:
            ``True`` if a notification for this target was sent too recently.
        """
        if self._cooldown <= 0:
            return False
        last = self._last_sent.get(target_id)
        if last is None:
            return False
        return (utc_now().timestamp() - last) < self._cooldown

    async def dispatch(self, event: ChangeEvent, *, force: bool = False) -> DeliveryReport:
        """Deliver an event to every applicable channel, concurrently.

        Args:
            event: The change to announce.
            force: Bypass the per-target cooldown. Used for manual test sends.

        Returns:
            A :class:`DeliveryReport` describing what happened on each channel.
        """
        if not force and self._in_cooldown(event.target.id):
            logger.info(
                "Suppressing notification for %r: within %.0fs cooldown",
                event.target.name,
                self._cooldown,
            )
            return DeliveryReport(skipped=tuple(self._notifiers))

        chosen, skipped = self._select(event)
        if not chosen:
            logger.warning(
                "Change detected on %r but no channel is configured to report it",
                event.target.name,
            )
            return DeliveryReport(skipped=tuple(skipped))

        results = await asyncio.gather(
            *(self._deliver(notifier, event) for notifier in chosen),
            return_exceptions=True,
        )

        delivered: list[str] = []
        failed: dict[str, str] = {}
        for notifier, result in zip(chosen, results, strict=True):
            if isinstance(result, BaseException):
                failed[notifier.name] = str(result)
                logger.error("Channel %r failed: %s", notifier.name, result)
            else:
                delivered.append(notifier.name)

        if delivered:
            self._last_sent[event.target.id] = utc_now().timestamp()

        report = DeliveryReport(
            delivered=tuple(delivered),
            failed=failed or None,
            skipped=tuple(skipped),
        )
        logger.info("Notification for %r - %s", event.target.name, report.summary())
        return report

    @staticmethod
    async def _deliver(notifier: Notifier, event: ChangeEvent) -> None:
        """Send through one channel, normalising unexpected errors.

        Args:
            notifier: The channel to use.
            event: The change to announce.

        Raises:
            NotifierError: On any failure, so the gather above records it
                against this channel only.
        """
        try:
            await notifier.send(event)
        except NotifierError:
            raise
        except Exception as exc:
            raise NotifierError(f"{type(exc).__name__}: {exc}") from exc

    async def test_channel(self, name: str) -> None:
        """Send a test message through one channel.

        Args:
            name: The channel's registered name.

        Raises:
            NotifierError: If the channel is unknown or the send failed.
        """
        notifier = self._notifiers.get(name)
        if notifier is None:
            raise NotifierError(f"No such notification channel: {name}")
        await notifier.send_test()
        logger.info("Test notification sent through %r", name)
