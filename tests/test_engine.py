"""Tests for the monitoring engine: detection, baselines and backoff."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from webmonitor.core.engine import MonitorEngine
from webmonitor.core.fetcher import Fetcher
from webmonitor.models import ChangeEvent, CheckStatus, RenderMode, Target
from webmonitor.notifiers.base import Notifier
from webmonitor.notifiers.dispatcher import Dispatcher

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from webmonitor.config import AppSettings
    from webmonitor.storage import Storage

PAGE = "<html><body><div class='content'>{body}</div></body></html>"


class RecordingNotifier(Notifier):
    """A notifier that records what it was asked to send."""

    name = "recording"
    label = "Recording"

    def __init__(self) -> None:
        """Start with an empty record."""
        self.events: list[ChangeEvent] = []

    @property
    def is_configured(self) -> bool:
        """Always ready.

        Returns:
            ``True``.
        """
        return True

    async def send(self, event: ChangeEvent) -> None:
        """Record the event.

        Args:
            event: The change to record.
        """
        self.events.append(event)


class StubRenderer:
    """A renderer that returns canned HTML without a browser."""

    def __init__(self, html: str) -> None:
        """Store the HTML to return.

        Args:
            html: Markup handed back from :meth:`render`.
        """
        self.html = html
        self.calls = 0

    async def render(self, url: str, *, timeout: float = 30.0) -> str:
        """Return the canned HTML.

        Args:
            url: Ignored.
            timeout: Ignored.

        Returns:
            The stored markup.
        """
        self.calls += 1
        return self.html


@pytest.fixture
def notifier() -> RecordingNotifier:
    """Provide a recording notifier.

    Returns:
        A fresh :class:`RecordingNotifier`.
    """
    return RecordingNotifier()


@pytest.fixture
def dispatcher(notifier: RecordingNotifier) -> Dispatcher:
    """Provide a dispatcher with cooldown disabled.

    Args:
        notifier: The recording notifier to register.

    Returns:
        A :class:`~webmonitor.notifiers.dispatcher.Dispatcher`.
    """
    return Dispatcher([notifier], cooldown_seconds=0.0)


@pytest.fixture
async def engine(
    settings: AppSettings, storage: Storage, dispatcher: Dispatcher
) -> AsyncIterator[MonitorEngine]:
    """Provide an engine with a live fetcher but no running loop.

    Args:
        settings: Settings fixture.
        storage: In-memory storage fixture.
        dispatcher: Dispatcher fixture.

    Yields:
        A :class:`~webmonitor.core.engine.MonitorEngine` ready for
        :meth:`~webmonitor.core.engine.MonitorEngine.check_target`.
    """
    instance = MonitorEngine(settings, storage, dispatcher)
    async with Fetcher(settings.network) as fetcher:
        instance._fetcher = fetcher
        yield instance


@respx.mock
async def test_first_check_reports_change_for_baseline(
    engine: MonitorEngine, target: Target
) -> None:
    """With no stored hash, any content counts as a change at the result level."""
    respx.get(target.url).mock(return_value=httpx.Response(200, html=PAGE.format(body="v1")))

    result = await engine.check_target(target)

    assert result.status is CheckStatus.CHANGED
    assert result.content_hash is not None
    assert result.content is not None


@respx.mock
async def test_baseline_does_not_notify(
    engine: MonitorEngine, storage: Storage, target: Target, notifier: RecordingNotifier
) -> None:
    """A newly added target must not fire an alert on its very first check."""
    storage.upsert_target(target)
    respx.get(target.url).mock(return_value=httpx.Response(200, html=PAGE.format(body="v1")))

    await engine._process(target)

    assert notifier.events == []
    stored = storage.get_target(target.id)
    assert stored is not None
    assert stored.content_hash is not None


@respx.mock
async def test_real_change_notifies(
    engine: MonitorEngine, storage: Storage, target: Target, notifier: RecordingNotifier
) -> None:
    """The second, differing check is the one that alerts."""
    storage.upsert_target(target)
    route = respx.get(target.url)

    route.mock(return_value=httpx.Response(200, html=PAGE.format(body="v1")))
    await engine._process(storage.get_target(target.id))

    route.mock(return_value=httpx.Response(200, html=PAGE.format(body="v2 changed")))
    await engine._process(storage.get_target(target.id))

    assert len(notifier.events) == 1
    assert "v2 changed" in notifier.events[0].new_content


@respx.mock
async def test_unchanged_content_does_not_notify(
    engine: MonitorEngine, storage: Storage, target: Target, notifier: RecordingNotifier
) -> None:
    """Identical content across polls is silent."""
    storage.upsert_target(target)
    respx.get(target.url).mock(return_value=httpx.Response(200, html=PAGE.format(body="same")))

    await engine._process(storage.get_target(target.id))
    await engine._process(storage.get_target(target.id))

    assert notifier.events == []


@respx.mock
async def test_change_outside_the_selector_is_ignored(
    engine: MonitorEngine, storage: Storage, target: Target, notifier: RecordingNotifier
) -> None:
    """Only the watched element matters - this is the core promise of the app."""
    storage.upsert_target(target)
    route = respx.get(target.url)

    route.mock(
        return_value=httpx.Response(
            200, html="<div class='ads'>ad A</div><div class='content'>stable</div>"
        )
    )
    await engine._process(storage.get_target(target.id))

    route.mock(
        return_value=httpx.Response(
            200, html="<div class='ads'>ad B</div><div class='content'>stable</div>"
        )
    )
    await engine._process(storage.get_target(target.id))

    assert notifier.events == []


@respx.mock
async def test_304_is_reported_as_not_modified(engine: MonitorEngine, target: Target) -> None:
    """A conditional hit short-circuits before parsing."""
    respx.get(target.url).mock(return_value=httpx.Response(304))
    stored = target.with_updates(content_hash="existing", etag='W/"v1"')

    result = await engine.check_target(stored)

    assert result.status is CheckStatus.NOT_MODIFIED
    assert result.bytes_transferred == 0


@respx.mock
async def test_no_conditional_headers_without_a_baseline(
    engine: MonitorEngine, target: Target
) -> None:
    """A 304 with no stored snapshot would leave nothing to compare against."""
    route = respx.get(target.url).mock(
        return_value=httpx.Response(200, html=PAGE.format(body="v1"))
    )

    await engine.check_target(target.with_updates(etag='W/"stale"', content_hash=None))

    assert "If-None-Match" not in route.calls.last.request.headers


@respx.mock
async def test_selector_miss_is_distinct_from_error(engine: MonitorEngine, target: Target) -> None:
    """A page that loads but lacks the element gets its own status."""
    respx.get(target.url).mock(
        return_value=httpx.Response(200, html="<html><body><p>nothing here</p></body></html>")
    )

    result = await engine.check_target(target.with_updates(render_mode=RenderMode.STATIC))

    assert result.status is CheckStatus.SELECTOR_MISS


@respx.mock
async def test_http_failure_becomes_an_error_result(engine: MonitorEngine, target: Target) -> None:
    """Network failures are captured, not raised into the scheduler."""
    respx.get(target.url).mock(return_value=httpx.Response(500))

    result = await engine.check_target(target)

    assert result.status is CheckStatus.ERROR
    assert result.error


@respx.mock
async def test_auto_mode_falls_back_to_the_browser(engine: MonitorEngine, target: Target) -> None:
    """A client-rendered page is retried through the renderer."""
    respx.get(target.url).mock(
        return_value=httpx.Response(200, html="<html><body><div id='root'></div></body></html>")
    )
    renderer = StubRenderer(PAGE.format(body="rendered by JS"))
    engine.set_renderer(renderer)

    result = await engine.check_target(target.with_updates(render_mode=RenderMode.AUTO))

    assert renderer.calls == 1
    assert result.status is CheckStatus.CHANGED
    assert result.rendered is True
    assert result.content is not None
    assert "rendered by JS" in result.content


@respx.mock
async def test_auto_target_is_promoted_to_browser_mode(
    engine: MonitorEngine, storage: Storage, target: Target
) -> None:
    """Promotion stops the doomed static fetch happening on every cycle."""
    storage.upsert_target(target)
    respx.get(target.url).mock(
        return_value=httpx.Response(200, html="<html><body><div id='root'></div></body></html>")
    )
    engine.set_renderer(StubRenderer(PAGE.format(body="js content")))

    await engine._process(storage.get_target(target.id))

    stored = storage.get_target(target.id)
    assert stored is not None
    assert stored.render_mode is RenderMode.BROWSER


async def test_browser_mode_without_a_renderer_errors(
    engine: MonitorEngine, target: Target
) -> None:
    """Asking for a render with no renderer is a clear error, not a hang."""
    engine.set_renderer(None)

    result = await engine.check_target(target.with_updates(render_mode=RenderMode.BROWSER))

    assert result.status is CheckStatus.ERROR
    assert "renderer" in (result.error or "")


@respx.mock
async def test_quiet_checks_widen_the_interval(
    engine: MonitorEngine, storage: Storage, target: Target
) -> None:
    """Adaptive backoff is what keeps request volume down on stable pages."""
    storage.upsert_target(target)
    respx.get(target.url).mock(return_value=httpx.Response(200, html=PAGE.format(body="same")))

    await engine._process(storage.get_target(target.id))
    first = storage.get_target(target.id)
    await engine._process(storage.get_target(target.id))
    second = storage.get_target(target.id)

    assert first is not None and second is not None
    assert second.interval_current > first.interval_current
    assert second.interval_current <= target.interval_max


@respx.mock
async def test_a_change_resets_the_interval(
    engine: MonitorEngine, storage: Storage, target: Target
) -> None:
    """After a change the target is watched closely again."""
    storage.upsert_target(target.with_updates(interval_current=600, content_hash="old"))
    respx.get(target.url).mock(return_value=httpx.Response(200, html=PAGE.format(body="new")))

    await engine._process(storage.get_target(target.id))

    stored = storage.get_target(target.id)
    assert stored is not None
    assert stored.interval_current == target.interval_min


@respx.mock
async def test_failures_back_off_and_reset(
    engine: MonitorEngine, storage: Storage, target: Target
) -> None:
    """Consecutive failures widen the retry gap; success clears the counter."""
    storage.upsert_target(target)
    route = respx.get(target.url)

    route.mock(return_value=httpx.Response(500))
    await engine._process(storage.get_target(target.id))
    await engine._process(storage.get_target(target.id))

    failed = storage.get_target(target.id)
    assert failed is not None
    assert failed.failure_count == 2

    route.mock(return_value=httpx.Response(200, html=PAGE.format(body="back")))
    await engine._process(storage.get_target(target.id))

    recovered = storage.get_target(target.id)
    assert recovered is not None
    assert recovered.failure_count == 0


@respx.mock
async def test_snapshot_is_written_before_the_hash(
    engine: MonitorEngine, storage: Storage, target: Target
) -> None:
    """The stored hash is a promise that a matching snapshot exists."""
    storage.upsert_target(target)
    respx.get(target.url).mock(return_value=httpx.Response(200, html=PAGE.format(body="v1")))

    await engine._process(storage.get_target(target.id))

    stored = storage.get_target(target.id)
    snapshot = storage.latest_snapshot(target.id)
    assert stored is not None and snapshot is not None
    assert stored.content_hash == snapshot.content_hash


@respx.mock
async def test_stats_track_savings_from_304s(engine: MonitorEngine, target: Target) -> None:
    """The status bar's "saved by 304s" figure comes from here."""
    respx.get(target.url).mock(return_value=httpx.Response(200, html=PAGE.format(body="v1")))
    engine.stats.record(await engine.check_target(target))

    respx.get(target.url).mock(return_value=httpx.Response(304))
    engine.stats.record(
        await engine.check_target(target.with_updates(content_hash="h", etag='W/"v1"'))
    )

    assert engine.stats.checks == 2
    assert engine.stats.not_modified == 1
    assert engine.stats.bytes_saved > 0
