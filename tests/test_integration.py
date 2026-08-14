"""End-to-end tests against a real HTTP server on a real socket.

Everything below the GUI is exercised together: sockets, conditional requests,
parsing, hashing, SQLite, the scheduler loop and notification fan-out. Unit
tests with mocked transports cannot catch a broken ``ETag`` round trip or a
scheduler that never wakes up; these can.
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

import pytest

from webmonitor.core.engine import MonitorEngine
from webmonitor.core.fetcher import Fetcher
from webmonitor.models import ChangeEvent, CheckStatus, Target
from webmonitor.notifiers.base import Notifier
from webmonitor.notifiers.dispatcher import Dispatcher

if TYPE_CHECKING:
    from collections.abc import Iterator

    from webmonitor.config import AppSettings
    from webmonitor.storage import Storage


class _State:
    """Mutable page state shared with the request handler.

    Attributes:
        body: Text placed inside the watched element.
        etag: Value served in the ``ETag`` header, or ``None`` to omit it.
        requests: Count of requests received.
        conditional_hits: Count of requests answered with ``304``.
    """

    def __init__(self) -> None:
        """Start with a known baseline page and no traffic recorded."""
        self.body = "version one"
        self.etag: str | None = None
        self.requests = 0
        self.conditional_hits = 0


class _Handler(BaseHTTPRequestHandler):
    """Serves a small page whose watched element can be mutated by a test."""

    state: _State

    def do_GET(self) -> None:
        """Serve the page, honouring ``If-None-Match`` when an ETag is set."""
        self.state.requests += 1

        if self.state.etag and self.headers.get("If-None-Match") == self.state.etag:
            self.state.conditional_hits += 1
            self.send_response(304)
            self.send_header("ETag", self.state.etag)
            self.end_headers()
            return

        payload = (
            "<html><body>"
            "<div class='ads'>rotating advert 12345</div>"
            f"<div class='content'>{self.state.body}</div>"
            "</body></html>"
        ).encode()

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        if self.state.etag:
            self.send_header("ETag", self.state.etag)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: object) -> None:
        """Silence the default stderr access log."""


class CountingNotifier(Notifier):
    """Records delivered events for assertions."""

    name = "counting"
    label = "Counting"

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
        """Record the delivered event.

        Args:
            event: The change being delivered.
        """
        self.events.append(event)


@pytest.fixture
def server() -> Iterator[tuple[str, _State]]:
    """Run a throwaway HTTP server on an ephemeral port.

    Yields:
        A tuple of the base URL and the mutable page state.
    """
    state = _State()
    handler = type("BoundHandler", (_Handler,), {"state": state})
    httpd = HTTPServer(("127.0.0.1", 0), handler)

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/", state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def notifier() -> CountingNotifier:
    """Provide a recording notifier.

    Returns:
        A fresh :class:`CountingNotifier`.
    """
    return CountingNotifier()


async def _engine(
    settings: AppSettings, storage: Storage, notifier: CountingNotifier
) -> tuple[MonitorEngine, Dispatcher]:
    """Build an engine wired to a recording dispatcher.

    Args:
        settings: Settings fixture.
        storage: Storage fixture.
        notifier: The notifier to register.

    Returns:
        The engine and its dispatcher.
    """
    dispatcher = Dispatcher([notifier], cooldown_seconds=0.0)
    return MonitorEngine(settings, storage, dispatcher), dispatcher


async def test_detects_a_real_change_over_http(
    settings: AppSettings,
    storage: Storage,
    server: tuple[str, _State],
    notifier: CountingNotifier,
) -> None:
    """Baseline, then a genuine edit, produces exactly one notification."""
    url, state = server
    target = Target(name="Live", url=url, selector="div.content", interval_min=1, interval_max=2)
    storage.upsert_target(target)

    engine, _ = await _engine(settings, storage, notifier)
    async with Fetcher(settings.network) as fetcher:
        engine._fetcher = fetcher

        await engine._process(storage.get_target(target.id))
        assert notifier.events == []  # baseline is silent

        state.body = "version two - the thing changed"
        await engine._process(storage.get_target(target.id))

    assert len(notifier.events) == 1
    assert "version two" in notifier.events[0].new_content


async def test_ignores_noise_outside_the_selector(
    settings: AppSettings,
    storage: Storage,
    server: tuple[str, _State],
    notifier: CountingNotifier,
) -> None:
    """The advert div changes every request; the watched element does not."""
    url, _state = server
    target = Target(name="Live", url=url, selector="div.content", interval_min=1)
    storage.upsert_target(target)

    engine, _ = await _engine(settings, storage, notifier)
    async with Fetcher(settings.network) as fetcher:
        engine._fetcher = fetcher
        for _ in range(3):
            await engine._process(storage.get_target(target.id))

    assert notifier.events == []


async def test_conditional_requests_are_honoured(
    settings: AppSettings,
    storage: Storage,
    server: tuple[str, _State],
    notifier: CountingNotifier,
) -> None:
    """With an ETag in play the second poll must be answered 304."""
    url, state = server
    state.etag = 'W/"fixed-v1"'
    target = Target(name="Live", url=url, selector="div.content", interval_min=1)
    storage.upsert_target(target)

    engine, _ = await _engine(settings, storage, notifier)
    async with Fetcher(settings.network) as fetcher:
        engine._fetcher = fetcher
        await engine._process(storage.get_target(target.id))
        result = await engine.check_target(storage.get_target(target.id))

    assert state.conditional_hits >= 1
    assert result.status is CheckStatus.NOT_MODIFIED
    assert result.bytes_transferred == 0


async def test_scheduler_loop_polls_and_stops(
    settings: AppSettings,
    storage: Storage,
    server: tuple[str, _State],
    notifier: CountingNotifier,
) -> None:
    """The real ``run()`` loop checks due targets and shuts down on request."""
    url, state = server
    settings.scheduler.check_on_start = True
    target = Target(name="Live", url=url, selector="div.content", interval_min=1, interval_max=2)
    storage.upsert_target(target)

    engine, _ = await _engine(settings, storage, notifier)
    task = asyncio.create_task(engine.run())

    # Let the baseline pass complete, then change the page and wait for the
    # scheduler to come back around on its own.
    await asyncio.sleep(1.5)
    state.body = "changed while the loop was running"
    await asyncio.sleep(2.5)

    engine.request_stop()
    await asyncio.wait_for(task, timeout=10)

    assert engine.is_running is False
    assert state.requests >= 2
    assert len(notifier.events) == 1


async def test_manual_check_request_is_serviced(
    settings: AppSettings,
    storage: Storage,
    server: tuple[str, _State],
    notifier: CountingNotifier,
) -> None:
    """The check-now action must bypass the interval and run promptly."""
    url, state = server
    settings.scheduler.check_on_start = False
    target = Target(
        name="Live", url=url, selector="div.content", interval_min=3600, interval_max=7200
    )
    storage.upsert_target(target)

    engine, _ = await _engine(settings, storage, notifier)
    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.4)

    engine.request_check(target.id)  # establishes the baseline
    await asyncio.sleep(1.2)
    state.body = "manually triggered change"
    engine.request_check(target.id)
    await asyncio.sleep(1.5)

    engine.request_stop()
    await asyncio.wait_for(task, timeout=10)

    assert len(notifier.events) == 1


async def test_unreachable_host_is_recorded_not_raised(
    settings: AppSettings, storage: Storage, notifier: CountingNotifier
) -> None:
    """A dead target must not take the scheduler down with it."""
    target = Target(
        name="Dead",
        # Port 1 on loopback refuses connections immediately.
        url="http://127.0.0.1:1/",
        selector="div.content",
        interval_min=1,
    )
    storage.upsert_target(target)

    engine, _ = await _engine(settings, storage, notifier)
    async with Fetcher(settings.network) as fetcher:
        engine._fetcher = fetcher
        await engine._process(storage.get_target(target.id))

    stored = storage.get_target(target.id)
    assert stored is not None
    assert stored.failure_count == 1
    assert notifier.events == []
