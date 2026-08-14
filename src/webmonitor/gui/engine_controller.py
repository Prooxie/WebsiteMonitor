"""Bridges the asyncio monitoring engine to the Qt GUI thread.

The engine owns an asyncio loop on a plain worker thread. Qt signals are the
crossing point: emitting a signal from a non-owning thread to a receiver that
lives on the GUI thread produces a queued connection, so slots run on the GUI
thread without any locking of our own.

This is also what fixes the original design, where ``Monitor.start()`` ran its
``while`` loop directly inside the button handler and froze the window.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Coroutine
from typing import Any

from PySide6.QtCore import QObject, Signal

from webmonitor.config import AppSettings
from webmonitor.core.engine import EngineCallbacks, MonitorEngine, Renderer
from webmonitor.notifiers.dispatcher import Dispatcher
from webmonitor.storage import Storage

__all__ = ["AsyncTaskRunner", "EngineController"]

logger = logging.getLogger(__name__)


class EngineController(QObject):
    """Owns the engine thread and re-publishes engine events as Qt signals.

    Signals:
        resultReady: ``(CheckResult, Target)`` after every check.
        changeDetected: ``(ChangeEvent,)`` when a change is confirmed.
        statusChanged: ``(str,)`` with a human-readable status line.
        errorOccurred: ``(Target, str)`` when a check fails.
        runningChanged: ``(bool,)`` when monitoring starts or stops.
    """

    resultReady = Signal(object, object)
    changeDetected = Signal(object)
    statusChanged = Signal(str)
    errorOccurred = Signal(object, str)
    runningChanged = Signal(bool)

    def __init__(
        self,
        settings: AppSettings,
        storage: Storage,
        dispatcher: Dispatcher,
        parent: QObject | None = None,
    ) -> None:
        """Initialise the controller.

        Args:
            settings: Application settings.
            storage: Repository shared with the GUI.
            dispatcher: Notification fan-out.
            parent: Parent object, which must live on the GUI thread.
        """
        super().__init__(parent)
        self._settings = settings
        self._storage = storage
        self._dispatcher = dispatcher

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._engine: MonitorEngine | None = None
        self._renderer: Renderer | None = None
        self._ready = threading.Event()

    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        """Whether the engine thread is alive.

        Returns:
            ``True`` while monitoring is active.
        """
        return self._thread is not None and self._thread.is_alive()

    @property
    def stats(self) -> Any:
        """Session counters from the engine.

        Returns:
            The engine's :class:`~webmonitor.core.engine.EngineStats`, or
            ``None`` when the engine is not running.
        """
        return self._engine.stats if self._engine else None

    def set_renderer(self, renderer: Renderer | None) -> None:
        """Install the browser renderer used for JavaScript-heavy targets.

        Args:
            renderer: The renderer, applied to a running engine immediately.
        """
        self._renderer = renderer
        if self._engine is not None:
            self._engine.set_renderer(renderer)

    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start monitoring on a background thread.

        Does nothing if monitoring is already running.
        """
        if self.is_running:
            logger.debug("Engine already running")
            return

        self._ready.clear()
        self._thread = threading.Thread(
            target=self._thread_main, name="webmonitor-engine", daemon=True
        )
        self._thread.start()

        # Wait briefly for the loop so an immediately following request_check
        # is not dropped on the floor.
        if not self._ready.wait(timeout=5.0):
            logger.warning("Engine loop did not signal readiness within 5s")

        self.runningChanged.emit(True)

    def stop(self, *, timeout: float = 12.0) -> None:
        """Ask the engine to stop and wait for its thread to finish.

        Args:
            timeout: Seconds to wait for a clean shutdown before giving up. The
                thread is a daemon, so a stuck engine cannot block process exit.
        """
        if not self.is_running:
            return

        engine, loop = self._engine, self._loop
        if engine is not None and loop is not None:
            loop.call_soon_threadsafe(engine.request_stop)

        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning("Engine thread did not stop within %.0fs", timeout)

        self._thread = None
        self._loop = None
        self._engine = None
        self.runningChanged.emit(False)

    def check_now(self, target_id: str) -> None:
        """Request an immediate check of one target.

        Args:
            target_id: The target to check. Ignored when the engine is stopped.
        """
        engine, loop = self._engine, self._loop
        if engine is None or loop is None:
            self.statusChanged.emit("Start monitoring first to run a check")
            return
        loop.call_soon_threadsafe(engine.request_check, target_id)

    def wake(self) -> None:
        """Nudge the engine to re-read targets, e.g. after an edit."""
        engine, loop = self._engine, self._loop
        if engine is not None and loop is not None:
            loop.call_soon_threadsafe(engine.wake)

    def submit(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Run a coroutine on the engine's loop, fire and forget.

        Args:
            coro: The coroutine to schedule. Closed immediately if the engine is
                not running, so it never leaks an un-awaited coroutine warning.
        """
        loop = self._loop
        if loop is None:
            coro.close()
            self.statusChanged.emit("Engine is not running")
            return
        asyncio.run_coroutine_threadsafe(coro, loop)

    # ------------------------------------------------------------------
    def _thread_main(self) -> None:
        """Entry point for the engine thread: own a loop and run the engine."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        callbacks = EngineCallbacks(
            on_result=lambda result, target: self.resultReady.emit(result, target),
            on_change=lambda event: self.changeDetected.emit(event),
            on_status=lambda message: self.statusChanged.emit(message),
            on_error=lambda target, message: self.errorOccurred.emit(target, message),
        )

        self._engine = MonitorEngine(
            self._settings,
            self._storage,
            self._dispatcher,
            callbacks=callbacks,
            renderer=self._renderer,
        )

        try:
            self._ready.set()
            loop.run_until_complete(self._engine.run())
        except Exception:
            logger.exception("Engine thread terminated with an error")
        finally:
            try:
                self._cancel_pending(loop)
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
                self._ready.set()
                logger.debug("Engine loop closed")

    @staticmethod
    def _cancel_pending(loop: asyncio.AbstractEventLoop) -> None:
        """Cancel any tasks still pending when the loop winds down.

        Args:
            loop: The loop being shut down.
        """
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))


class AsyncTaskRunner(QObject):
    """Runs one-off coroutines off the GUI thread and reports the outcome.

    Used for things like "send a test notification", which must work whether or
    not the monitoring engine happens to be running.

    Signals:
        finished: ``(str, bool, str)`` - task name, success flag, message.
    """

    finished = Signal(str, bool, str)

    def __init__(self, parent: QObject | None = None) -> None:
        """Initialise the runner.

        Args:
            parent: Parent object, which must live on the GUI thread.
        """
        super().__init__(parent)

    def run(
        self,
        name: str,
        factory: Callable[[], Coroutine[Any, Any, Any]],
        success_message: str = "Done",
    ) -> None:
        """Execute a coroutine on a throwaway thread.

        Args:
            name: Identifier echoed back in the :attr:`finished` signal.
            factory: Zero-argument callable returning the coroutine. A factory
                rather than a coroutine so it is created on the worker thread's
                loop, not the caller's.
            success_message: Message reported when the coroutine returns.
        """

        def worker() -> None:
            try:
                asyncio.run(factory())
            except Exception as exc:
                logger.warning("Async task %r failed: %s", name, exc)
                self.finished.emit(name, False, str(exc))
            else:
                self.finished.emit(name, True, success_message)

        threading.Thread(target=worker, name=f"webmonitor-task-{name}", daemon=True).start()
