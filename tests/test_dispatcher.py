"""Tests for notification fan-out, isolation and suppression."""

from __future__ import annotations

import asyncio

import pytest

from webmonitor.models import ChangeEvent, Target
from webmonitor.notifiers.base import Notifier, NotifierError
from webmonitor.notifiers.dispatcher import Dispatcher


class FakeNotifier(Notifier):
    """A configurable notifier for exercising the dispatcher."""

    def __init__(
        self,
        name: str,
        *,
        configured: bool = True,
        fail_with: str | None = None,
        delay: float = 0.0,
    ) -> None:
        """Set up the fake.

        Args:
            name: Channel name.
            configured: What :attr:`is_configured` reports.
            fail_with: When set, :meth:`send` raises with this message.
            delay: Seconds to sleep inside :meth:`send`.
        """
        self.name = name
        self.label = name.title()
        self._configured = configured
        self._fail_with = fail_with
        self._delay = delay
        self.sent: list[ChangeEvent] = []

    @property
    def is_configured(self) -> bool:
        """Whether this channel claims to be usable.

        Returns:
            The value passed to the constructor.
        """
        return self._configured

    async def send(self, event: ChangeEvent) -> None:
        """Record or fail, per configuration.

        Args:
            event: The change being delivered.

        Raises:
            NotifierError: If the fake was built with ``fail_with``.
        """
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail_with:
            raise NotifierError(self._fail_with)
        self.sent.append(event)


@pytest.fixture
def event(target: Target) -> ChangeEvent:
    """Provide a change event.

    Args:
        target: The target fixture.

    Returns:
        A :class:`~webmonitor.models.ChangeEvent`.
    """
    return ChangeEvent(
        target=target, old_content="before", new_content="after", added_lines=1, removed_lines=1
    )


async def test_delivers_to_every_configured_channel(event: ChangeEvent) -> None:
    """With no per-target selection, all ready channels receive the event."""
    email, telegram = FakeNotifier("email"), FakeNotifier("telegram")
    dispatcher = Dispatcher([email, telegram], cooldown_seconds=0)

    report = await dispatcher.dispatch(event)

    assert set(report.delivered) == {"email", "telegram"}
    assert len(email.sent) == 1
    assert len(telegram.sent) == 1


async def test_skips_unconfigured_channels(event: ChangeEvent) -> None:
    """An unconfigured channel is skipped, not attempted and failed."""
    ready, not_ready = FakeNotifier("email"), FakeNotifier("telegram", configured=False)
    dispatcher = Dispatcher([ready, not_ready], cooldown_seconds=0)

    report = await dispatcher.dispatch(event)

    assert report.delivered == ("email",)
    assert "telegram" in report.skipped


async def test_one_failure_does_not_block_the_others(event: ChangeEvent) -> None:
    """Channel isolation: a dead SMTP server must not suppress Telegram."""
    broken = FakeNotifier("email", fail_with="SMTP refused the connection")
    working = FakeNotifier("telegram")
    dispatcher = Dispatcher([broken, working], cooldown_seconds=0)

    report = await dispatcher.dispatch(event)

    assert report.delivered == ("telegram",)
    assert report.failed is not None
    assert "SMTP refused" in report.failed["email"]
    assert len(working.sent) == 1


async def test_unexpected_exceptions_are_normalised(event: ChangeEvent) -> None:
    """A channel raising something other than NotifierError is still contained."""

    class ExplodingNotifier(FakeNotifier):
        """A notifier that raises an unexpected error type."""

        async def send(self, event: ChangeEvent) -> None:
            """Raise a non-NotifierError.

            Args:
                event: Ignored.

            Raises:
                ValueError: Always.
            """
            raise ValueError("something unexpected")

    dispatcher = Dispatcher([ExplodingNotifier("boom"), FakeNotifier("ok")], cooldown_seconds=0)

    report = await dispatcher.dispatch(event)

    assert report.delivered == ("ok",)
    assert report.failed is not None
    assert "ValueError" in report.failed["boom"]


async def test_per_target_channel_selection(target: Target) -> None:
    """A target may restrict itself to a subset of channels."""
    email, telegram = FakeNotifier("email"), FakeNotifier("telegram")
    dispatcher = Dispatcher([email, telegram], cooldown_seconds=0)
    scoped = ChangeEvent(
        target=target.with_updates(channels=("telegram",)),
        old_content="a",
        new_content="b",
    )

    report = await dispatcher.dispatch(scoped)

    assert report.delivered == ("telegram",)
    assert email.sent == []


async def test_cooldown_suppresses_a_rapid_repeat(event: ChangeEvent) -> None:
    """A flapping page must not produce an unbounded alert stream."""
    notifier = FakeNotifier("email")
    dispatcher = Dispatcher([notifier], cooldown_seconds=300)

    first = await dispatcher.dispatch(event)
    second = await dispatcher.dispatch(event)

    assert first.any_delivered is True
    assert second.any_delivered is False
    assert len(notifier.sent) == 1


async def test_force_bypasses_the_cooldown(event: ChangeEvent) -> None:
    """Manual test sends must always go out."""
    notifier = FakeNotifier("email")
    dispatcher = Dispatcher([notifier], cooldown_seconds=300)

    await dispatcher.dispatch(event)
    await dispatcher.dispatch(event, force=True)

    assert len(notifier.sent) == 2


async def test_cooldown_is_per_target(target: Target) -> None:
    """One noisy target must not mute a different one."""
    notifier = FakeNotifier("email")
    dispatcher = Dispatcher([notifier], cooldown_seconds=300)
    other = Target(name="Other", url="https://other.example")

    await dispatcher.dispatch(ChangeEvent(target=target, old_content="a", new_content="b"))
    await dispatcher.dispatch(ChangeEvent(target=other, old_content="a", new_content="b"))

    assert len(notifier.sent) == 2


async def test_channels_are_delivered_concurrently(event: ChangeEvent) -> None:
    """Fan-out is parallel, so total time tracks the slowest channel."""
    slow = [FakeNotifier(f"ch{i}", delay=0.1) for i in range(4)]
    dispatcher = Dispatcher(slow, cooldown_seconds=0)

    loop = asyncio.get_running_loop()
    started = loop.time()
    await dispatcher.dispatch(event)
    elapsed = loop.time() - started

    assert elapsed < 0.35  # sequential would be ~0.4s


async def test_no_configured_channels_reports_cleanly(event: ChangeEvent) -> None:
    """Detecting a change with nowhere to send it is not a crash."""
    dispatcher = Dispatcher([FakeNotifier("email", configured=False)], cooldown_seconds=0)

    report = await dispatcher.dispatch(event)

    assert report.any_delivered is False
    assert "email" in report.skipped


async def test_test_channel_rejects_unknown_names() -> None:
    """The settings dialog gets a clear error for a missing channel."""
    dispatcher = Dispatcher([FakeNotifier("email")])

    with pytest.raises(NotifierError, match="No such notification channel"):
        await dispatcher.test_channel("carrier-pigeon")


async def test_test_channel_sends_a_synthetic_event() -> None:
    """The default ``send_test`` builds its own event so channels need no extra code."""
    notifier = FakeNotifier("email")
    dispatcher = Dispatcher([notifier])

    await dispatcher.test_channel("email")

    assert len(notifier.sent) == 1
    assert notifier.sent[0].target.name == "Test notification"


def test_report_summary_is_readable() -> None:
    """The log line should name what happened on each channel."""
    from webmonitor.notifiers.dispatcher import DeliveryReport

    report = DeliveryReport(
        delivered=("email",), failed={"telegram": "bad token"}, skipped=("desktop",)
    )

    summary = report.summary()

    assert "delivered: email" in summary
    assert "failed: telegram" in summary
    assert "skipped: desktop" in summary
