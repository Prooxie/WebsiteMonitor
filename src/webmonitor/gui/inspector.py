"""The embedded browser: element inspector and offscreen page renderer.

Two things live here, both built on QtWebEngine's Chromium:

:class:`InspectorView`
    The interactive pane. Loads a page, lets the user hover and click an element
    exactly like a browser's own inspector, and reports back a CSS selector.
    Python and JavaScript talk over a :class:`QWebChannel`.

:class:`PageRenderer`
    A headless page used by the monitoring engine when a target needs its
    JavaScript executed before the watched element exists. It satisfies the
    :class:`~webmonitor.core.engine.Renderer` protocol.

Both share the default web profile deliberately: a site you logged into in the
inspector stays logged in when the engine renders it, which is what makes
watching pages behind a login work at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import QFile, QIODevice, QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

__all__ = ["InspectorView", "PageRenderer", "PickerBridge"]

logger = logging.getLogger(__name__)

_PICKER_JS_PATH = Path(__file__).parent / "resources" / "picker.js"

#: Time to let client-side scripts settle after ``loadFinished`` before reading
#: the DOM. Most single-page apps have painted well within this.
_SETTLE_MS = 900


def _read_picker_source() -> str:
    """Load the picker script from disk.

    Returns:
        The JavaScript source, or an empty string if the file is missing, in
        which case picking is disabled but the browser still works.
    """
    try:
        return _PICKER_JS_PATH.read_text(encoding="utf-8")
    except OSError:
        logger.exception("Could not read picker script at %s", _PICKER_JS_PATH)
        return ""


def _read_qwebchannel_source() -> str:
    """Load ``qwebchannel.js`` from the Qt resource bundle.

    Returns:
        The JavaScript source, or an empty string if the resource is
        unavailable, which disables the JS-to-Python bridge.
    """
    file = QFile(":/qtwebchannel/qwebchannel.js")
    if not file.open(QIODevice.OpenModeFlag.ReadOnly):
        logger.error("qwebchannel.js resource is unavailable; element picking will not work")
        return ""
    try:
        return bytes(file.readAll().data()).decode("utf-8")
    finally:
        file.close()


class PickerBridge(QObject):
    """The object JavaScript calls into, exposed over :class:`QWebChannel`.

    The JS-facing method names (``elementPicked`` and friends) are Qt *slots*;
    the Python-facing signals are named differently on purpose, because a Qt
    class cannot carry a signal and a slot under one attribute name.

    Signals:
        picked: Emitted with the decoded pick payload.
        cancelled: Emitted when the user aborts picking.
        hovered: Emitted with the selector under the cursor.
    """

    picked = Signal(dict)
    cancelled = Signal()
    hovered = Signal(str)

    @Slot(str)
    def elementPicked(self, payload: str) -> None:  # noqa: N802 - JS-facing name
        """Handle a pick reported by ``picker.js``.

        Args:
            payload: JSON string with the selector and a content preview.
        """
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            logger.error("Malformed pick payload: %r", payload[:200])
            return
        logger.info("Element picked: %s (%s matches)", data.get("selector"), data.get("matches"))
        self.picked.emit(data)

    @Slot(str)
    def pickCancelled(self, _payload: str = "") -> None:  # noqa: N802 - JS-facing name
        """Handle the user pressing Escape during a pick.

        Args:
            _payload: Unused placeholder argument from the JS side.
        """
        self.cancelled.emit()

    @Slot(str)
    def hoverChanged(self, selector: str) -> None:  # noqa: N802 - JS-facing name
        """Handle the hovered element changing.

        Args:
            selector: Candidate CSS selector for the hovered element.
        """
        self.hovered.emit(selector)


def _install_scripts(profile: QWebEngineProfile) -> None:
    """Install the bridge bootstrap and picker scripts into a profile.

    Registering on the profile means every page loaded through it gets the
    scripts automatically, including after navigation, without any per-load
    bookkeeping.

    Args:
        profile: The web profile to instrument. Existing scripts with the same
            names are replaced, so calling this twice is harmless.
    """
    scripts = profile.scripts()
    for name in ("wm_channel", "wm_picker"):
        for existing in scripts.find(name):
            scripts.remove(existing)

    channel_source = _read_qwebchannel_source()
    if channel_source:
        bootstrap = QWebEngineScript()
        bootstrap.setName("wm_channel")
        bootstrap.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        bootstrap.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        bootstrap.setRunsOnSubFrames(False)
        bootstrap.setSourceCode(
            channel_source
            + """
            (function () {
              if (typeof qt === "undefined" || !qt.webChannelTransport) return;
              new QWebChannel(qt.webChannelTransport, function (channel) {
                window.__wmBridge = channel.objects.wmBridge;
              });
            })();
            """
        )
        scripts.insert(bootstrap)

    picker_source = _read_picker_source()
    if picker_source:
        picker = QWebEngineScript()
        picker.setName("wm_picker")
        picker.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
        picker.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        picker.setRunsOnSubFrames(False)
        picker.setSourceCode(picker_source)
        scripts.insert(picker)

    logger.debug("Injected inspector scripts into profile %r", profile.storageName())


class InspectorView(QWidget):
    """Browser pane with a URL bar and a DevTools-style element picker.

    Signals:
        selectorPicked: Emitted with ``(selector, preview_text)`` when the user
            confirms an element.
        urlChanged: Emitted with the current page URL as a string.
        pickingChanged: Emitted with the picker's active state.
    """

    selectorPicked = Signal(str, str)
    urlChanged = Signal(str)
    pickingChanged = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the inspector pane.

        Args:
            parent: Parent widget.
        """
        super().__init__(parent)
        self._picking = False
        self._build_ui()
        self._wire_channel()

    # ---------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        """Lay out the URL bar, browser view and selector readout."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # --- navigation row ---
        nav = QHBoxLayout()
        nav.setSpacing(6)

        self._back_btn = self._flat_button("◀", "Back")
        self._forward_btn = self._flat_button("▶", "Forward")
        self._reload_btn = self._flat_button("⟳", "Reload")

        self._url_edit = QLineEdit()
        self._url_edit.setPlaceholderText("https://example.com  -  press Enter to load")
        self._url_edit.returnPressed.connect(self._on_url_entered)

        self._pick_btn = QPushButton("Pick element")
        self._pick_btn.setProperty("accent", "true")
        self._pick_btn.setToolTip(
            "Click, then hover the page and click the element to watch.\n"
            "Arrow Up selects the parent, Enter confirms, Escape cancels."
        )
        self._pick_btn.clicked.connect(self.toggle_picking)

        for widget in (self._back_btn, self._forward_btn, self._reload_btn):
            nav.addWidget(widget)
        nav.addWidget(self._url_edit, stretch=1)
        nav.addWidget(self._pick_btn)
        layout.addLayout(nav)

        # --- browser ---
        self._view = QWebEngineView(self)
        self._view.setMinimumHeight(320)
        layout.addWidget(self._view, stretch=1)

        # --- selector readout ---
        readout = QHBoxLayout()
        readout.setSpacing(8)
        caption = QLabel("Selector")
        caption.setProperty("dim", "true")
        self._selector_label = QLabel("—")
        self._selector_label.setProperty("mono", "true")
        self._selector_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._match_label = QLabel("")
        self._match_label.setProperty("dim", "true")

        readout.addWidget(caption)
        readout.addWidget(self._selector_label, stretch=1)
        readout.addWidget(self._match_label)
        layout.addLayout(readout)

        self._back_btn.clicked.connect(self._view.back)
        self._forward_btn.clicked.connect(self._view.forward)
        self._reload_btn.clicked.connect(self._view.reload)
        self._view.urlChanged.connect(self._on_view_url_changed)
        self._view.loadFinished.connect(self._on_load_finished)

    @staticmethod
    def _flat_button(text: str, tooltip: str) -> QPushButton:
        """Create a borderless icon-style button.

        Args:
            text: Glyph to display.
            tooltip: Hover text.

        Returns:
            The configured button.
        """
        button = QPushButton(text)
        button.setProperty("flat", "true")
        button.setToolTip(tooltip)
        button.setFixedWidth(38)
        return button

    def _wire_channel(self) -> None:
        """Register the bridge object and install the injected scripts."""
        page = self._view.page()
        profile = page.profile()
        _install_scripts(profile)

        self._bridge = PickerBridge(self)
        self._channel = QWebChannel(page)
        self._channel.registerObject("wmBridge", self._bridge)
        page.setWebChannel(self._channel)

        self._bridge.picked.connect(self._on_picked)
        self._bridge.cancelled.connect(self._on_pick_cancelled)
        self._bridge.hovered.connect(self._on_hover)

    # ------------------------------------------------------------ actions
    def load(self, url: str) -> None:
        """Navigate to a URL.

        Args:
            url: Absolute or scheme-less URL. ``https://`` is assumed when no
                scheme is given.
        """
        if not url.strip():
            return
        if "://" not in url:
            url = f"https://{url}"
        self._url_edit.setText(url)
        self._view.setUrl(QUrl(url))

    def current_url(self) -> str:
        """Return the URL currently displayed.

        Returns:
            The page URL as a string.
        """
        return self._view.url().toString()

    def toggle_picking(self) -> None:
        """Start picking, or stop it if already active."""
        self.stop_picking() if self._picking else self.start_picking()

    def start_picking(self) -> None:
        """Activate the element picker in the loaded page."""
        self._picking = True
        self._pick_btn.setText("Cancel pick")
        self._pick_btn.setProperty("accent", "false")
        self._restyle(self._pick_btn)
        self._view.page().runJavaScript("window.__wmPicker && window.__wmPicker.start();")
        self.pickingChanged.emit(True)
        logger.debug("Element picking started")

    def stop_picking(self) -> None:
        """Deactivate the element picker."""
        self._picking = False
        self._pick_btn.setText("Pick element")
        self._pick_btn.setProperty("accent", "true")
        self._restyle(self._pick_btn)
        self._view.page().runJavaScript("window.__wmPicker && window.__wmPicker.stop();")
        self.pickingChanged.emit(False)

    def highlight(self, selector: str) -> None:
        """Outline an existing selector so the user can verify it.

        Args:
            selector: CSS selector to highlight. Empty clears the highlight.
        """
        payload = json.dumps(selector)
        self._view.page().runJavaScript(
            f"window.__wmPicker && window.__wmPicker.preview({payload});",
            self._on_highlight_result,
        )
        self._selector_label.setText(selector or "—")

    def page_html(self, callback: object) -> None:
        """Fetch the live DOM of the displayed page.

        Args:
            callback: One-argument callable receiving the HTML string.
        """
        self._view.page().runJavaScript(
            "document.documentElement.outerHTML",
            callback,  # type: ignore[arg-type]
        )

    @staticmethod
    def _restyle(widget: QWidget) -> None:
        """Force Qt to re-evaluate stylesheet rules after a property change.

        Args:
            widget: The widget whose dynamic property changed.
        """
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)

    # ------------------------------------------------------------- events
    def _on_url_entered(self) -> None:
        """Load whatever the user typed in the URL bar."""
        self.load(self._url_edit.text())

    def _on_view_url_changed(self, url: QUrl) -> None:
        """Mirror browser-driven navigation into the URL bar.

        Args:
            url: The new page URL.
        """
        text = url.toString()
        self._url_edit.setText(text)
        self.urlChanged.emit(text)

    def _on_load_finished(self, ok: bool) -> None:
        """Reset picker state after navigation.

        Args:
            ok: Whether the load succeeded.
        """
        if not ok:
            logger.warning("Page failed to load: %s", self.current_url())
        if self._picking:
            # The injected script is fresh after navigation; re-arm it.
            QTimer.singleShot(
                120,
                lambda: self._view.page().runJavaScript(
                    "window.__wmPicker && window.__wmPicker.start();"
                ),
            )

    def _on_picked(self, data: dict[str, Any]) -> None:
        """Handle a confirmed element pick.

        Args:
            data: Payload from ``picker.js``.
        """
        selector = str(data.get("selector", ""))
        preview = str(data.get("text", ""))
        matches = int(data.get("matches", 0) or 0)

        self._selector_label.setText(selector or "—")
        self._match_label.setText(
            "1 match" if matches == 1 else f"{matches} matches" if matches else "no match"
        )
        self.stop_picking()
        self.selectorPicked.emit(selector, preview)

    def _on_pick_cancelled(self) -> None:
        """Handle the user aborting a pick."""
        self.stop_picking()

    def _on_hover(self, selector: str) -> None:
        """Show the candidate selector while the cursor moves.

        Args:
            selector: Candidate CSS selector.
        """
        self._selector_label.setText(selector or "—")

    def _on_highlight_result(self, count: object) -> None:
        """Display how many elements a highlighted selector matched.

        Args:
            count: Match count returned by the JS preview helper; ``-1`` marks
                an invalid selector.
        """
        try:
            value = int(count)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        if value < 0:
            self._match_label.setText("invalid selector")
        elif value == 0:
            self._match_label.setText("no match")
        elif value == 1:
            self._match_label.setText("1 match")
        else:
            self._match_label.setText(f"{value} matches")


class PageRenderer(QObject):
    """Offscreen page renderer satisfying the engine's ``Renderer`` protocol.

    QtWebEngine is strictly single-threaded and lives on the GUI thread, while
    the engine runs its own asyncio loop on a worker thread. The two are bridged
    with a queued signal outbound and :meth:`asyncio.loop.call_soon_threadsafe`
    inbound.

    Renders are serialised with an :class:`asyncio.Lock`: a second concurrent
    Chromium page would multiply memory use for no throughput gain, since these
    are network-bound.
    """

    _renderRequested = Signal(str, float, object)

    def __init__(self, parent: QObject | None = None) -> None:
        """Create the renderer and its hidden page.

        Args:
            parent: Parent object, normally the main window, so the page is
                destroyed with it.
        """
        super().__init__(parent)
        self._page = QWebEnginePage(QWebEngineProfile.defaultProfile(), self)
        self._lock = asyncio.Lock()
        self._pending: tuple[Any, Any] | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        # Connected once for the object's lifetime; the handler is always the
        # same, so reconnecting per render would only churn (and warn).
        self._timer.timeout.connect(self._on_timeout)

        self._renderRequested.connect(self._start_render, Qt.ConnectionType.QueuedConnection)
        self._page.loadFinished.connect(self._on_load_finished)

    async def render(self, url: str, *, timeout: float = 30.0) -> str:
        """Load a URL in the hidden page and return the rendered DOM.

        Args:
            url: Absolute URL to load.
            timeout: Seconds to wait before giving up.

        Returns:
            The serialised DOM after scripts have run.

        Raises:
            TimeoutError: If the page did not finish within ``timeout``.
            RuntimeError: If the page reported a load failure.
        """
        async with self._lock:
            loop = asyncio.get_running_loop()
            future: asyncio.Future[str] = loop.create_future()
            self._renderRequested.emit(url, timeout, (loop, future))
            try:
                return await asyncio.wait_for(future, timeout=timeout + 10)
            except TimeoutError:
                logger.warning("Render of %s timed out", url)
                raise

    @Slot(str, float, object)
    def _start_render(self, url: str, timeout: float, box: tuple[Any, Any]) -> None:
        """Begin a render on the GUI thread.

        Args:
            url: URL to load.
            timeout: Seconds before the watchdog fires.
            box: ``(loop, future)`` used to deliver the result back.
        """
        self._pending = box
        self._timer.stop()
        self._timer.start(int(timeout * 1000))
        self._page.setUrl(QUrl(url))

    def _on_load_finished(self, ok: bool) -> None:
        """Extract the DOM once the page has loaded.

        Args:
            ok: Whether Chromium reported a successful load.
        """
        if self._pending is None:
            return
        if not ok:
            self._deliver_error(RuntimeError("Page failed to load"))
            return
        # Give client-side frameworks a moment to paint before reading the DOM.
        QTimer.singleShot(_SETTLE_MS, self._extract_html)

    def _extract_html(self) -> None:
        """Read ``document.documentElement.outerHTML`` from the hidden page."""
        if self._pending is None:
            return
        self._page.runJavaScript("document.documentElement.outerHTML", self._deliver_html)

    def _deliver_html(self, html: object) -> None:
        """Deliver a successful render back to the waiting coroutine.

        Args:
            html: The DOM string returned by JavaScript.
        """
        self._timer.stop()
        box, self._pending = self._pending, None
        if box is None:
            return
        loop, future = box
        if future.done():
            return
        loop.call_soon_threadsafe(future.set_result, str(html or ""))

    def _deliver_error(self, error: Exception) -> None:
        """Deliver a failure back to the waiting coroutine.

        Args:
            error: The exception to raise in the caller.
        """
        self._timer.stop()
        box, self._pending = self._pending, None
        if box is None:
            return
        loop, future = box
        if future.done():
            return
        loop.call_soon_threadsafe(future.set_exception, error)

    def _on_timeout(self) -> None:
        """Abort a render that overran its deadline."""
        if self._pending is None:
            return
        self._page.triggerAction(QWebEnginePage.WebAction.Stop)
        self._deliver_error(TimeoutError("Render timed out"))
