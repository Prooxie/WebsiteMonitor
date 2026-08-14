"""The polling engine: decides what to check, when, and what to do about it.

Scheduling strategy
-------------------
Each target carries its own *adaptive* interval. After a quiet check the
interval grows by ``backoff_factor`` up to ``interval_max``; the moment a change
lands it snaps back to ``interval_min``. A page that changes twice a year is
therefore polled hourly, while one that just changed is watched closely for a
while. Random jitter is folded into each new interval so that a dozen targets
added in the same minute do not stampede the network together.

Fetch strategy, cheapest first:

1. Conditional GET. A ``304`` ends the check having transferred no body at all.
2. Full GET plus Lexbor extraction of just the watched element.
3. Only if the selector matches nothing and the mode allows it, a real browser
   render - which is 50-100x more expensive and so is a last resort. When an
   ``AUTO`` target needs the browser, it is promoted to ``BROWSER`` permanently
   so the wasted static attempt happens once, not forever.

The engine never imports Qt. Browser rendering arrives through the
:class:`Renderer` protocol, which the GUI layer satisfies with QtWebEngine.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from webmonitor.config import AppSettings
from webmonitor.core.differ import build_change_event, compute_hash
from webmonitor.core.extractor import ExtractionError, Extractor
from webmonitor.core.fetcher import Fetcher, FetchError
from webmonitor.models import (
    ChangeEvent,
    CheckResult,
    CheckStatus,
    RenderMode,
    Snapshot,
    Target,
    utc_now,
)
from webmonitor.notifiers.dispatcher import Dispatcher
from webmonitor.storage import Storage

__all__ = ["EngineCallbacks", "MonitorEngine", "Renderer"]

logger = logging.getLogger(__name__)

#: How long the loop sleeps when nothing is scheduled, so that newly added
#: targets and stop requests are still noticed promptly.
_IDLE_TICK_SECONDS = 5.0


class Renderer(Protocol):
    """Renders a URL with a real browser engine, running its JavaScript."""

    async def render(self, url: str, *, timeout: float = 30.0) -> str:
        """Load a URL and return the resulting DOM as HTML.

        Args:
            url: Absolute URL to load.
            timeout: Seconds to wait for the page to settle.

        Returns:
            The serialised DOM after scripts have run.

        Raises:
            Exception: Implementation-specific failure, caught by the engine and
                turned into an error result.
        """
        ...


@dataclass(slots=True)
class EngineCallbacks:
    """Hooks the GUI installs to observe the engine.

    Callbacks fire on the engine's own thread. A Qt consumer must marshal to the
    GUI thread, which the application layer does by emitting queued signals.

    Attributes:
        on_result: Called after every check with the result and the target.
        on_change: Called when a change is confirmed, before notification.
        on_status: Called with a short human-readable status line.
        on_error: Called with the target and an error message on failure.
    """

    on_result: Callable[[CheckResult, Target], None] | None = None
    on_change: Callable[[ChangeEvent], None] | None = None
    on_status: Callable[[str], None] | None = None
    on_error: Callable[[Target, str], None] | None = None

    def emit_result(self, result: CheckResult, target: Target) -> None:
        """Invoke :attr:`on_result`, swallowing consumer errors.

        Args:
            result: The check result.
            target: The target that was checked.
        """
        self._safe(self.on_result, result, target)

    def emit_change(self, event: ChangeEvent) -> None:
        """Invoke :attr:`on_change`, swallowing consumer errors.

        Args:
            event: The confirmed change.
        """
        self._safe(self.on_change, event)

    def emit_status(self, message: str) -> None:
        """Invoke :attr:`on_status`, swallowing consumer errors.

        Args:
            message: Status line to display.
        """
        self._safe(self.on_status, message)

    def emit_error(self, target: Target, message: str) -> None:
        """Invoke :attr:`on_error`, swallowing consumer errors.

        Args:
            target: The target that failed.
            message: Description of the failure.
        """
        self._safe(self.on_error, target, message)

    @staticmethod
    def _safe(callback: Callable[..., None] | None, *args: object) -> None:
        """Call a callback and log, rather than propagate, any exception.

        Args:
            callback: The callback, possibly ``None``.
            *args: Arguments to pass through.
        """
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Engine callback raised")


@dataclass(slots=True)
class EngineStats:
    """Counters for the session, shown in the GUI status bar.

    Attributes:
        checks: Total checks performed.
        changes: Changes detected.
        errors: Failed checks.
        not_modified: Checks answered with ``304``.
        bytes_saved: Bytes avoided by conditional requests, estimated from the
            average body size seen for that target.
        bytes_transferred: Body bytes actually downloaded.
    """

    checks: int = 0
    changes: int = 0
    errors: int = 0
    not_modified: int = 0
    bytes_saved: int = 0
    bytes_transferred: int = 0
    _avg_body: dict[str, int] = field(default_factory=dict)

    def record(self, result: CheckResult) -> None:
        """Fold one check result into the counters.

        Args:
            result: The result to record.
        """
        self.checks += 1
        if result.status is CheckStatus.CHANGED:
            self.changes += 1
        elif result.status is CheckStatus.ERROR:
            self.errors += 1
        elif result.status is CheckStatus.NOT_MODIFIED:
            self.not_modified += 1
            self.bytes_saved += self._avg_body.get(result.target_id, 0)

        if result.bytes_transferred:
            self.bytes_transferred += result.bytes_transferred
            previous = self._avg_body.get(result.target_id)
            self._avg_body[result.target_id] = (
                result.bytes_transferred
                if previous is None
                else (previous + result.bytes_transferred) // 2
            )


class MonitorEngine:
    """Runs the check loop over all enabled targets."""

    def __init__(
        self,
        settings: AppSettings,
        storage: Storage,
        dispatcher: Dispatcher,
        *,
        callbacks: EngineCallbacks | None = None,
        renderer: Renderer | None = None,
    ) -> None:
        """Initialise the engine.

        Args:
            settings: Application settings.
            storage: Repository for targets, snapshots and events.
            dispatcher: Notification fan-out.
            callbacks: Observation hooks for the GUI.
            renderer: Browser renderer for JavaScript-heavy pages. Without one,
                ``BROWSER`` targets report a selector miss.
        """
        self._settings = settings
        self._storage = storage
        self._dispatcher = dispatcher
        self._callbacks = callbacks or EngineCallbacks()
        self._renderer = renderer
        self._extractor = Extractor()

        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._semaphore = asyncio.Semaphore(settings.scheduler.max_concurrent_checks)
        self._fetcher: Fetcher | None = None
        self._running = False
        self._forced: set[str] = set()
        self.stats = EngineStats()

    @property
    def is_running(self) -> bool:
        """Whether the check loop is active.

        Returns:
            ``True`` between :meth:`run` starting and stopping.
        """
        return self._running

    def set_renderer(self, renderer: Renderer | None) -> None:
        """Install or replace the browser renderer.

        Args:
            renderer: The renderer, or ``None`` to disable browser rendering.
        """
        self._renderer = renderer

    def request_stop(self) -> None:
        """Ask the loop to finish after the in-flight checks complete.

        Thread-safe in the sense that it only sets asyncio events, but it must
        be called from the engine's own loop; the application layer wraps it in
        :meth:`asyncio.loop.call_soon_threadsafe`.
        """
        self._stop.set()
        self._wake.set()

    def request_check(self, target_id: str) -> None:
        """Queue an immediate check of one target, ignoring its interval.

        Args:
            target_id: The target to check now.
        """
        self._forced.add(target_id)
        self._wake.set()

    def wake(self) -> None:
        """Interrupt the idle sleep, e.g. after targets were edited."""
        self._wake.set()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    async def run(self) -> None:
        """Run the check loop until :meth:`request_stop` is called."""
        self._running = True
        self._stop.clear()
        self._callbacks.emit_status("Monitoring started")
        logger.info("Engine started")

        try:
            async with Fetcher(self._settings.network) as fetcher:
                self._fetcher = fetcher
                if self._settings.scheduler.check_on_start:
                    targets = await asyncio.to_thread(self._storage.list_targets, enabled_only=True)
                    self._forced.update(target.id for target in targets)

                while not self._stop.is_set():
                    delay = await self._run_once()
                    await self._sleep(delay)
        except asyncio.CancelledError:
            logger.info("Engine cancelled")
            raise
        except Exception:
            logger.exception("Engine loop crashed")
            self._callbacks.emit_status("Monitoring stopped after an internal error")
            raise
        finally:
            self._running = False
            self._fetcher = None
            logger.info("Engine stopped")
            self._callbacks.emit_status("Monitoring stopped")

    async def _run_once(self) -> float:
        """Check every due target once.

        Returns:
            Seconds to sleep before the next pass.
        """
        targets = await asyncio.to_thread(self._storage.list_targets, enabled_only=True)
        if not targets:
            return _IDLE_TICK_SECONDS

        forced, self._forced = self._forced, set()
        due = [t for t in targets if t.id in forced or self._seconds_until_due(t) <= 0]

        if due:
            logger.debug("Checking %d due target(s)", len(due))
            await asyncio.gather(
                *(self._guarded_check(target) for target in due),
                return_exceptions=True,
            )
            # Re-read so the sleep below uses the freshly written timestamps.
            targets = await asyncio.to_thread(self._storage.list_targets, enabled_only=True)

        if not targets:
            return _IDLE_TICK_SECONDS
        return min(
            (self._seconds_until_due(t) for t in targets),
            default=_IDLE_TICK_SECONDS,
        )

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake early on stop or on :meth:`wake`.

        Args:
            seconds: Maximum time to sleep, clamped to a sane range.
        """
        delay = max(0.5, min(seconds, _IDLE_TICK_SECONDS * 12))
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=delay)

    @staticmethod
    def _seconds_until_due(target: Target) -> float:
        """Compute how long until a target is due.

        Args:
            target: The target to schedule.

        Returns:
            Seconds remaining, or ``0`` when the target is due now or has never
            been checked.
        """
        if target.last_checked is None:
            return 0.0
        elapsed = (utc_now() - target.last_checked).total_seconds()
        return max(0.0, target.interval_current - elapsed)

    async def _guarded_check(self, target: Target) -> None:
        """Check one target under the concurrency semaphore.

        Args:
            target: The target to check. Exceptions are logged, never raised,
                so one bad target cannot take the pass down.
        """
        async with self._semaphore:
            try:
                await self._process(target)
            except Exception:
                logger.exception("Unhandled error while checking %r", target.name)

    # ------------------------------------------------------------------
    # Single-target pipeline
    # ------------------------------------------------------------------
    async def _process(self, target: Target) -> None:
        """Run the full check-compare-notify-persist pipeline for one target.

        Args:
            target: The target to process.
        """
        result = await self.check_target(target)
        self.stats.record(result)
        updated = self._apply_result(target, result)

        # Snapshot before target: the stored content_hash is a promise that a
        # matching snapshot exists. Writing the hash first and crashing would
        # leave the next check diffing against nothing and crying wolf.
        old_content = ""
        if result.status is CheckStatus.CHANGED and result.content is not None:
            previous = await asyncio.to_thread(self._storage.latest_snapshot, target.id)
            old_content = previous.content if previous else ""
            await asyncio.to_thread(
                self._storage.add_snapshot,
                Snapshot(
                    target_id=target.id,
                    content_hash=result.content_hash or "",
                    content=result.content,
                    byte_size=result.bytes_transferred,
                ),
                keep=self._settings.scheduler.snapshot_history,
            )

        await asyncio.to_thread(self._storage.upsert_target, updated)
        self._callbacks.emit_result(result, updated)

        if result.status is CheckStatus.ERROR and result.error:
            self._callbacks.emit_error(updated, result.error)
            return

        if result.status is not CheckStatus.CHANGED or result.content is None:
            return

        # A first-ever check establishes the baseline. Alerting on it would mean
        # every newly added target immediately emails the user, which is noise.
        if target.is_first_run:
            logger.info("Baseline captured for %r", target.name)
            self._callbacks.emit_status(f"Baseline captured for {target.name}")
            return

        event = build_change_event(updated, old_content, result.content)
        await asyncio.to_thread(self._storage.add_event, event)

        logger.info("Change detected on %r (%s)", target.name, event.summary)
        self._callbacks.emit_change(event)
        await self._dispatcher.dispatch(event)

    async def check_target(self, target: Target) -> CheckResult:
        """Fetch, extract and compare one target without persisting anything.

        Args:
            target: The target to check.

        Returns:
            A :class:`~webmonitor.models.CheckResult` describing the outcome.
        """
        loop = asyncio.get_running_loop()
        started = loop.time()

        if target.render_mode is RenderMode.BROWSER:
            return await self._check_via_browser(target, started)

        assert self._fetcher is not None, "check_target called outside run()"

        # Only replay validators when we actually hold matching content; a 304
        # with no stored snapshot would leave us with nothing to compare.
        send_validators = target.content_hash is not None
        try:
            response = await self._fetcher.fetch(
                target.url,
                etag=target.etag if send_validators else None,
                last_modified=target.last_modified if send_validators else None,
                proxy=target.proxy,
            )
        except FetchError as exc:
            return CheckResult(
                target_id=target.id,
                status=CheckStatus.ERROR,
                error=str(exc),
                elapsed_ms=(loop.time() - started) * 1000,
            )

        if response.not_modified:
            return CheckResult(
                target_id=target.id,
                status=CheckStatus.NOT_MODIFIED,
                etag=response.etag,
                last_modified=response.last_modified,
                elapsed_ms=response.elapsed_ms,
            )

        try:
            content = self._extractor.extract(response.text, target.selector, target.normalization)
        except ExtractionError as exc:
            return CheckResult(
                target_id=target.id,
                status=CheckStatus.ERROR,
                error=str(exc),
                elapsed_ms=(loop.time() - started) * 1000,
                bytes_transferred=response.bytes_transferred,
            )

        if content is None:
            # Empty static match on an AUTO target is the classic signature of a
            # client-rendered page: try once with a real browser.
            if target.render_mode is RenderMode.AUTO and self._renderer is not None:
                logger.info(
                    "Selector missed statically on %r; retrying with the browser engine",
                    target.name,
                )
                return await self._check_via_browser(target, started)

            return CheckResult(
                target_id=target.id,
                status=CheckStatus.SELECTOR_MISS,
                etag=response.etag,
                last_modified=response.last_modified,
                error=f"Selector {target.selector!r} matched no elements",
                elapsed_ms=response.elapsed_ms,
                bytes_transferred=response.bytes_transferred,
            )

        digest = compute_hash(content)
        changed = digest != target.content_hash
        return CheckResult(
            target_id=target.id,
            status=CheckStatus.CHANGED if changed else CheckStatus.UNCHANGED,
            content=content if changed else None,
            content_hash=digest,
            etag=response.etag,
            last_modified=response.last_modified,
            elapsed_ms=response.elapsed_ms,
            bytes_transferred=response.bytes_transferred,
        )

    async def _check_via_browser(self, target: Target, started: float) -> CheckResult:
        """Check a target by rendering it in the browser engine.

        Args:
            target: The target to check.
            started: Loop timestamp when the overall check began.

        Returns:
            A :class:`~webmonitor.models.CheckResult` with ``rendered`` set, which
            is what promotes an ``AUTO`` target to ``BROWSER`` in
            :meth:`_apply_result`.
        """
        loop = asyncio.get_running_loop()

        if self._renderer is None:
            return CheckResult(
                target_id=target.id,
                status=CheckStatus.ERROR,
                error="Browser rendering requested but no renderer is available",
                elapsed_ms=(loop.time() - started) * 1000,
            )

        try:
            html = await self._renderer.render(target.url, timeout=self._settings.network.timeout)
        except Exception as exc:
            return CheckResult(
                target_id=target.id,
                status=CheckStatus.ERROR,
                error=f"Browser render failed: {exc}",
                elapsed_ms=(loop.time() - started) * 1000,
                rendered=True,
            )

        try:
            content = self._extractor.extract(html, target.selector, target.normalization)
        except ExtractionError as exc:
            return CheckResult(
                target_id=target.id,
                status=CheckStatus.ERROR,
                error=str(exc),
                elapsed_ms=(loop.time() - started) * 1000,
                rendered=True,
            )

        elapsed = (loop.time() - started) * 1000
        if content is None:
            return CheckResult(
                target_id=target.id,
                status=CheckStatus.SELECTOR_MISS,
                error=f"Selector {target.selector!r} matched no elements after rendering",
                elapsed_ms=elapsed,
                rendered=True,
            )

        digest = compute_hash(content)
        changed = digest != target.content_hash
        return CheckResult(
            target_id=target.id,
            status=CheckStatus.CHANGED if changed else CheckStatus.UNCHANGED,
            content=content if changed else None,
            content_hash=digest,
            elapsed_ms=elapsed,
            bytes_transferred=len(html.encode("utf-8")),
            rendered=True,
        )

    # ------------------------------------------------------------------
    # Result folding
    # ------------------------------------------------------------------
    def _apply_result(self, target: Target, result: CheckResult) -> Target:
        """Fold a check result back into the target's stored state.

        Args:
            target: The target as it was before the check.
            result: What the check produced.

        Returns:
            An updated copy carrying new validators, hash, timestamps and the
            next adaptive interval.
        """
        now = utc_now()
        changes: dict[str, object] = {"last_checked": now}

        if result.status is CheckStatus.ERROR:
            failures = target.failure_count + 1
            changes["failure_count"] = failures
            changes["interval_current"] = self._error_interval(target, failures)
            return target.with_updates(**changes)

        changes["failure_count"] = 0

        if result.etag is not None:
            changes["etag"] = result.etag
        if result.last_modified is not None:
            changes["last_modified"] = result.last_modified

        if result.status is CheckStatus.CHANGED:
            changes["content_hash"] = result.content_hash
            changes["last_changed"] = now
            # Something is happening on this page - watch it closely again.
            changes["interval_current"] = target.interval_min
        else:
            # UNCHANGED, NOT_MODIFIED and SELECTOR_MISS all back off. A miss is
            # surfaced to the user through the result callback; there is no
            # point re-fetching a page whose selector no longer matches at the
            # same rate as one that is genuinely being watched.
            changes["interval_current"] = self._quiet_interval(target)

        # An AUTO target that only yields content through the browser is
        # promoted, so we stop paying for a doomed static fetch every cycle.
        if result.rendered and target.render_mode is RenderMode.AUTO:
            changes["render_mode"] = RenderMode.BROWSER
            logger.info("Promoted %r to browser rendering", target.name)

        return target.with_updates(**changes)

    def _quiet_interval(self, target: Target) -> int:
        """Compute the next interval after a check that found no change.

        Args:
            target: The target that was checked.

        Returns:
            The next interval in seconds, grown by the backoff factor, jittered,
            and clamped to the target's configured bounds.
        """
        config = self._settings.scheduler
        if not config.adaptive_intervals:
            return target.interval_min

        grown = target.interval_current * config.backoff_factor
        return self._jitter(min(grown, target.interval_max), target)

    def _error_interval(self, target: Target, failures: int) -> int:
        """Compute the retry interval after a failed check.

        Args:
            target: The target that failed.
            failures: Consecutive failure count, including this one.

        Returns:
            An exponentially backed-off interval in seconds, clamped to
            ``interval_max``.
        """
        base = self._settings.scheduler.error_backoff_base * (2 ** min(failures - 1, 6))
        return self._jitter(min(base, float(target.interval_max)), target)

    def _jitter(self, seconds: float, target: Target) -> int:
        """Apply random jitter and clamp to the target's bounds.

        Args:
            seconds: The nominal interval.
            target: The target, for its min and max bounds.

        Returns:
            A jittered integer interval within ``[interval_min, interval_max]``.
        """
        ratio = self._settings.scheduler.jitter_ratio
        if ratio > 0:
            seconds *= random.uniform(1.0 - ratio, 1.0 + ratio)
        return int(max(target.interval_min, min(seconds, target.interval_max)))
