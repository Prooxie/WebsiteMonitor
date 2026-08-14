"""The application's main window."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from PySide6.QtCore import QSize, Qt, QTimer, Slot
from PySide6.QtGui import QAction, QCloseEvent, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QSystemTrayIcon,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from webmonitor.config import AppSettings
from webmonitor.gui.engine_controller import EngineController
from webmonitor.gui.inspector import InspectorView, PageRenderer
from webmonitor.gui.settings_dialog import SettingsDialog
from webmonitor.gui.target_dialog import TargetDialog
from webmonitor.gui.theme import Palette, build_stylesheet, palette_for
from webmonitor.logging_setup import GuiLogHandler
from webmonitor.models import ChangeEvent, CheckResult, CheckStatus, Target, utc_now
from webmonitor.notifiers.dispatcher import Dispatcher
from webmonitor.secrets_store import SecretStore
from webmonitor.storage import Storage

__all__ = ["MainWindow", "make_app_icon"]

logger = logging.getLogger(__name__)

#: Minimum height of a target row, enough for a name plus its detail line.
_ROW_HEIGHT = 58

_STATUS_COLOURS = {
    CheckStatus.CHANGED: "#3b82f6",
    CheckStatus.UNCHANGED: "#22c55e",
    CheckStatus.NOT_MODIFIED: "#22c55e",
    CheckStatus.SELECTOR_MISS: "#f59e0b",
    CheckStatus.ERROR: "#ef4444",
}


def make_app_icon(palette: Palette) -> QIcon:
    """Draw the application icon.

    Generated rather than shipped as a file so the package stays a pure source
    distribution with no binary assets to keep in sync.

    Args:
        palette: Theme colours to draw with.

    Returns:
        A multi-resolution icon.
    """
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        radius = size * 0.22
        painter.setBrush(QColor(palette.accent))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(0, 0, size, size, radius, radius)

        # A stylised viewfinder: outer ring plus centre dot.
        inset = size * 0.28
        pen_width = max(1.0, size * 0.075)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        pen = painter.pen()
        pen.setColor(QColor("#ffffff"))
        pen.setWidthF(pen_width)
        pen.setStyle(Qt.PenStyle.SolidLine)
        painter.setPen(pen)
        painter.drawEllipse(int(inset), int(inset), int(size - 2 * inset), int(size - 2 * inset))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#ffffff"))
        dot = size * 0.13
        painter.drawEllipse(int((size - dot) / 2), int((size - dot) / 2), int(dot), int(dot))
        painter.end()

        icon.addPixmap(pixmap)
    return icon


class TargetRow(QWidget):
    """The row widget shown for one target in the sidebar list."""

    def __init__(self, target: Target, parent: QWidget | None = None) -> None:
        """Build the row.

        Args:
            target: The target to display.
            parent: Parent widget.
        """
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(10)

        self._dot = QLabel("●")
        self._dot.setFixedWidth(12)
        layout.addWidget(self._dot, alignment=Qt.AlignmentFlag.AlignTop)

        text_column = QVBoxLayout()
        text_column.setSpacing(2)
        self._name = QLabel()
        self._name.setStyleSheet("font-weight:600;")
        self._detail = QLabel()
        self._detail.setProperty("dim", "true")
        self._detail.setStyleSheet("font-size:11px;")
        text_column.addWidget(self._name)
        text_column.addWidget(self._detail)
        layout.addLayout(text_column, stretch=1)

        self.update_target(target)

    def update_target(self, target: Target) -> None:
        """Refresh the row's text and status colour.

        Args:
            target: The target's current state.
        """
        self._name.setText(target.name)

        if not target.enabled:
            colour, state = "#6b7280", "paused"
        elif target.failure_count:
            colour, state = "#ef4444", f"{target.failure_count} failure(s)"
        elif target.last_changed:
            colour = "#22c55e"
            state = f"changed {_relative(target.last_changed)}"
        elif target.content_hash:
            colour, state = "#22c55e", "watching"
        else:
            colour, state = "#8b94a6", "no baseline yet"

        self._dot.setStyleSheet(f"color:{colour};font-size:14px;")

        checked = _relative(target.last_checked) if target.last_checked else "never checked"
        self._detail.setText(
            f"{state}  ·  {checked}  ·  every {_duration(target.interval_current)}"
        )
        self.setToolTip(f"{target.url}\n{target.selector or '(whole page)'}")


def _relative(moment: datetime | None) -> str:
    """Render a timestamp as a short relative string.

    Args:
        moment: The time to describe, or ``None``.

    Returns:
        Text such as ``"4m ago"``, or ``"never"`` when given ``None``.
    """
    if moment is None:
        return "never"
    seconds = (utc_now() - moment).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _duration(seconds: int) -> str:
    """Render a number of seconds compactly.

    Args:
        seconds: The duration.

    Returns:
        Text such as ``"90s"``, ``"5m"`` or ``"2h"``.
    """
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


class MainWindow(QMainWindow):
    """Top-level window: target list, inspector and activity log."""

    def __init__(
        self,
        settings: AppSettings,
        storage: Storage,
        dispatcher: Dispatcher,
        secrets: SecretStore,
    ) -> None:
        """Assemble the window and wire up the engine.

        Args:
            settings: Application settings.
            storage: Repository for targets and history.
            dispatcher: Notification fan-out.
            secrets: Credential store, passed through to the settings dialog.
        """
        super().__init__()
        self._settings = settings
        self._storage = storage
        self._dispatcher = dispatcher
        self._secrets = secrets
        self._palette = palette_for(settings.theme)
        self._targets: dict[str, Target] = {}
        self._force_quit = False

        self.setWindowTitle("Website Monitor")
        self.resize(1320, 860)
        self.setWindowIcon(make_app_icon(self._palette))

        self._controller = EngineController(settings, storage, dispatcher, self)
        self._renderer = PageRenderer(self)
        self._controller.set_renderer(self._renderer)

        self._build_toolbar()
        self._build_central()
        self._build_statusbar()
        self._build_tray()
        self._connect()

        self._reload_targets()
        self._install_log_handler()

        # Keeps the "4m ago" captions honest without polling the database.
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._refresh_rows)
        self._tick.start(20_000)

        if settings.start_monitoring_on_launch:
            QTimer.singleShot(600, self._start_monitoring)

    # ---------------------------------------------------------------- UI
    def _build_toolbar(self) -> None:
        """Create the top toolbar and its actions."""
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(toolbar)

        self._start_action = QAction("▶  Start monitoring", self)
        self._start_action.triggered.connect(self._toggle_monitoring)
        toolbar.addAction(self._start_action)

        toolbar.addSeparator()

        self._add_action = QAction("+  Add target", self)
        self._add_action.triggered.connect(self._add_target)
        toolbar.addAction(self._add_action)

        self._check_action = QAction("⟳  Check now", self)
        self._check_action.triggered.connect(self._check_selected)
        toolbar.addAction(self._check_action)

        spacer = QWidget()
        spacer.setSizePolicy(
            spacer.sizePolicy().horizontalPolicy().Expanding,  # type: ignore[attr-defined]
            spacer.sizePolicy().verticalPolicy(),
        )
        toolbar.addWidget(spacer)

        settings_action = QAction("⚙  Settings", self)
        settings_action.triggered.connect(self._open_settings)
        toolbar.addAction(settings_action)

    def _build_central(self) -> None:
        """Create the splitter, target sidebar and right-hand tabs."""
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        # ---- sidebar ----
        sidebar = QWidget()
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(12, 12, 6, 12)
        sidebar_layout.setSpacing(10)

        header = QLabel("Targets")
        header.setProperty("heading", "true")
        sidebar_layout.addWidget(header)

        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._show_context_menu)
        self._list.itemDoubleClicked.connect(lambda _item: self._edit_selected())
        self._list.currentItemChanged.connect(self._on_selection_changed)
        sidebar_layout.addWidget(self._list, stretch=1)

        self._empty_hint = QLabel(
            "No targets yet.\n\nOpen a page in the Inspector, click "
            "“Pick element”, then click what you want to watch."
        )
        self._empty_hint.setWordWrap(True)
        self._empty_hint.setProperty("dim", "true")
        self._empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sidebar_layout.addWidget(self._empty_hint)

        button_row = QHBoxLayout()
        add_button = QPushButton("Add")
        add_button.setProperty("accent", "true")
        add_button.clicked.connect(self._add_target)
        remove_button = QPushButton("Remove")
        remove_button.setProperty("danger", "true")
        remove_button.clicked.connect(self._remove_selected)
        button_row.addWidget(add_button)
        button_row.addWidget(remove_button)
        sidebar_layout.addLayout(button_row)

        sidebar.setMinimumWidth(280)
        sidebar.setMaximumWidth(460)
        splitter.addWidget(sidebar)

        # ---- tabs ----
        tabs = QTabWidget()

        inspector_page = QWidget()
        inspector_layout = QVBoxLayout(inspector_page)
        inspector_layout.setContentsMargins(12, 12, 12, 12)
        self._inspector = InspectorView()
        inspector_layout.addWidget(self._inspector)
        tabs.addTab(inspector_page, "Inspector")

        activity_page = QWidget()
        activity_layout = QVBoxLayout(activity_page)
        activity_layout.setContentsMargins(12, 12, 12, 12)
        activity_layout.setSpacing(10)

        events_label = QLabel("Detected changes")
        events_label.setProperty("heading", "true")
        activity_layout.addWidget(events_label)

        self._events = QListWidget()
        self._events.setMaximumHeight(260)
        activity_layout.addWidget(self._events)

        log_label = QLabel("Log")
        log_label.setProperty("heading", "true")
        activity_layout.addWidget(log_label)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setProperty("monospace", "true")
        self._log.setMaximumBlockCount(2000)
        activity_layout.addWidget(self._log, stretch=1)

        tabs.addTab(activity_page, "Activity")
        self._tabs = tabs
        splitter.addWidget(tabs)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 1000])
        self.setCentralWidget(splitter)

    def _build_statusbar(self) -> None:
        """Create the status bar labels."""
        self._status_label = QLabel("Idle")
        self._stats_label = QLabel("")
        self._stats_label.setProperty("dim", "true")
        self.statusBar().addWidget(self._status_label, stretch=1)
        self.statusBar().addPermanentWidget(self._stats_label)

    def _build_tray(self) -> None:
        """Create the system tray icon and its menu."""
        self._tray = QSystemTrayIcon(make_app_icon(self._palette), self)
        self._tray.setToolTip("Website Monitor")

        menu = QMenu()
        show_action = menu.addAction("Show window")
        show_action.triggered.connect(self._restore_window)
        menu.addSeparator()
        self._tray_toggle = menu.addAction("Start monitoring")
        self._tray_toggle.triggered.connect(self._toggle_monitoring)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self._quit)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

        # Hand the desktop channel a way to actually show a toast.
        desktop = self._dispatcher.get("desktop")
        if desktop is not None and hasattr(desktop, "set_sink"):
            desktop.set_sink(self._show_toast)

    def _install_log_handler(self) -> None:
        """Mirror the application log into the Activity tab."""
        handler = GuiLogHandler(self._append_log_threadsafe)
        logging.getLogger().addHandler(handler)
        self._log_handler = handler

    def _connect(self) -> None:
        """Connect engine and inspector signals to their slots."""
        self._controller.resultReady.connect(self._on_result)
        self._controller.changeDetected.connect(self._on_change)
        self._controller.statusChanged.connect(self._on_status)
        self._controller.errorOccurred.connect(self._on_error)
        self._controller.runningChanged.connect(self._on_running_changed)

        self._inspector.selectorPicked.connect(self._on_selector_picked)

    # ----------------------------------------------------------- targets
    def _reload_targets(self) -> None:
        """Rebuild the sidebar from the database."""
        selected = self._selected_target_id()
        self._list.clear()
        self._targets.clear()

        targets = self._storage.list_targets()
        for target in targets:
            self._targets[target.id] = target
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, target.id)
            row = TargetRow(target)
            # sizeHint() is asked before the widget is polished, so it comes
            # back short and clips the second line. Floor it.
            hint = row.sizeHint()
            hint.setHeight(max(hint.height(), _ROW_HEIGHT))
            item.setSizeHint(hint)
            self._list.addItem(item)
            self._list.setItemWidget(item, row)
            if target.id == selected:
                self._list.setCurrentItem(item)

        self._empty_hint.setVisible(not targets)
        self._list.setVisible(bool(targets))
        self._update_stats()

    def _refresh_rows(self) -> None:
        """Re-render every row so relative timestamps stay accurate."""
        for index in range(self._list.count()):
            item = self._list.item(index)
            target = self._targets.get(item.data(Qt.ItemDataRole.UserRole))
            widget = self._list.itemWidget(item)
            if target is not None and isinstance(widget, TargetRow):
                widget.update_target(target)
        self._update_stats()

    def _selected_target_id(self) -> str | None:
        """Return the id of the selected target.

        Returns:
            The target id, or ``None`` when nothing is selected.
        """
        item = self._list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _selected_target(self) -> Target | None:
        """Return the selected target.

        Returns:
            The target, or ``None`` when nothing is selected.
        """
        target_id = self._selected_target_id()
        return self._targets.get(target_id) if target_id else None

    def _channel_choices(self) -> tuple[tuple[str, str], ...]:
        """List the registered notification channels for the target dialog.

        Returns:
            ``(name, label)`` pairs.
        """
        return tuple((n.name, n.label) for n in self._dispatcher.channels)

    def _add_target(self) -> None:
        """Open the dialog to create a new target."""
        dialog = TargetDialog(available_channels=self._channel_choices(), parent=self)

        url = self._inspector.current_url()
        if url and url != "about:blank":
            dialog.prefill(url=url, name=TargetDialog.suggest_name(url, ""))

        if dialog.exec() != TargetDialog.DialogCode.Accepted:
            return

        target = dialog.result_target()
        self._storage.upsert_target(target)
        logger.info("Added target %r (%s)", target.name, target.url)
        self._reload_targets()
        self._controller.wake()

    def _edit_selected(self) -> None:
        """Open the dialog to edit the selected target."""
        target = self._selected_target()
        if target is None:
            return

        dialog = TargetDialog(target, available_channels=self._channel_choices(), parent=self)
        if dialog.exec() != TargetDialog.DialogCode.Accepted:
            return

        updated = dialog.result_target()
        self._storage.upsert_target(updated)
        logger.info("Updated target %r", updated.name)
        self._reload_targets()
        self._controller.wake()

    def _remove_selected(self) -> None:
        """Delete the selected target after confirmation."""
        target = self._selected_target()
        if target is None:
            return

        confirm = QMessageBox.question(
            self,
            "Remove target",
            f"Stop watching “{target.name}” and delete its history?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self._storage.delete_target(target.id)
        self._reload_targets()
        self._controller.wake()

    def _check_selected(self) -> None:
        """Queue an immediate check of the selected target."""
        target = self._selected_target()
        if target is None:
            self._on_status("Select a target first")
            return
        self._controller.check_now(target.id)
        self._on_status(f"Checking {target.name}…")

    def _toggle_enabled(self) -> None:
        """Pause or resume the selected target."""
        target = self._selected_target()
        if target is None:
            return
        updated = target.with_updates(enabled=not target.enabled)
        self._storage.upsert_target(updated)
        self._reload_targets()
        self._controller.wake()

    def _show_context_menu(self, position: Any) -> None:
        """Show the right-click menu for a target row.

        Args:
            position: Click position in list-widget coordinates.
        """
        target = self._selected_target()
        if target is None:
            return

        menu = QMenu(self)
        menu.addAction("Check now", self._check_selected)
        menu.addAction("Pause" if target.enabled else "Resume", self._toggle_enabled)
        menu.addSeparator()
        menu.addAction("Open in Inspector", self._open_selected_in_inspector)
        menu.addAction("Edit…", self._edit_selected)
        menu.addSeparator()
        menu.addAction("Remove", self._remove_selected)
        menu.exec(self._list.mapToGlobal(position))

    def _open_selected_in_inspector(self) -> None:
        """Load the selected target's page and highlight its element."""
        target = self._selected_target()
        if target is None:
            return
        self._tabs.setCurrentIndex(0)
        self._inspector.load(target.url)
        if target.selector:
            QTimer.singleShot(2500, lambda: self._inspector.highlight(target.selector))

    def _on_selection_changed(self) -> None:
        """Enable or disable actions that need a selection."""
        has_selection = self._selected_target() is not None
        self._check_action.setEnabled(has_selection)

    # -------------------------------------------------------- monitoring
    def _toggle_monitoring(self) -> None:
        """Start monitoring if stopped, stop it if running."""
        self._stop_monitoring() if self._controller.is_running else self._start_monitoring()

    def _start_monitoring(self) -> None:
        """Start the engine, warning if there is nothing to watch."""
        if not self._storage.list_targets(enabled_only=True):
            QMessageBox.information(
                self,
                "Nothing to monitor",
                "Add at least one enabled target before starting.",
            )
            return
        self._controller.start()

    def _stop_monitoring(self) -> None:
        """Stop the engine."""
        self._controller.stop()

    @Slot(bool)
    def _on_running_changed(self, running: bool) -> None:
        """Update controls to reflect the engine state.

        Args:
            running: Whether monitoring is active.
        """
        self._start_action.setText("■  Stop monitoring" if running else "▶  Start monitoring")
        self._tray_toggle.setText("Stop monitoring" if running else "Start monitoring")
        self._tray.setToolTip(
            "Website Monitor - monitoring" if running else "Website Monitor - idle"
        )

    @Slot(object, object)
    def _on_result(self, result: CheckResult, target: Target) -> None:
        """Record a check result and refresh that target's row.

        Args:
            result: What the check produced.
            target: The target's updated state.
        """
        self._targets[target.id] = target
        for index in range(self._list.count()):
            item = self._list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) != target.id:
                continue
            widget = self._list.itemWidget(item)
            if isinstance(widget, TargetRow):
                widget.update_target(target)
            break

        if result.status is CheckStatus.SELECTOR_MISS:
            self._on_status(f"{target.name}: selector matched nothing")
        self._update_stats()

    @Slot(object)
    def _on_change(self, event: ChangeEvent) -> None:
        """Add a detected change to the Activity tab.

        Args:
            event: The confirmed change.
        """
        timestamp = event.detected_at.astimezone().strftime("%H:%M:%S")
        item = QListWidgetItem(
            f"{timestamp}   {event.target.name}   +{event.added_lines} / -{event.removed_lines}"
        )
        item.setForeground(QColor(_STATUS_COLOURS[CheckStatus.CHANGED]))
        self._events.insertItem(0, item)
        while self._events.count() > 200:
            self._events.takeItem(self._events.count() - 1)

    @Slot(str)
    def _on_status(self, message: str) -> None:
        """Show a status message in the status bar.

        Args:
            message: Text to display.
        """
        self._status_label.setText(message)

    @Slot(object, str)
    def _on_error(self, target: Target, message: str) -> None:
        """Report a failed check in the status bar.

        Args:
            target: The target that failed.
            message: Failure description.
        """
        self._on_status(f"{target.name}: {message}")

    def _update_stats(self) -> None:
        """Refresh the counters on the right of the status bar."""
        stats = self._controller.stats
        total = len(self._targets)
        enabled = sum(1 for target in self._targets.values() if target.enabled)

        if stats is None:
            self._stats_label.setText(f"{enabled}/{total} enabled")
            return

        saved = ""
        if stats.bytes_saved > 1024:
            saved = f"  ·  {stats.bytes_saved / 1024:.0f} KB saved by 304s"
        self._stats_label.setText(
            f"{enabled}/{total} enabled  ·  {stats.checks} checks  ·  "
            f"{stats.changes} changes  ·  {stats.errors} errors{saved}"
        )

    # ------------------------------------------------------------ picker
    @Slot(str, str)
    def _on_selector_picked(self, selector: str, preview: str) -> None:
        """Offer to create a target from a freshly picked element.

        Args:
            selector: The generated CSS selector.
            preview: A text preview of the element's contents.
        """
        url = self._inspector.current_url()
        dialog = TargetDialog(available_channels=self._channel_choices(), parent=self)
        dialog.prefill(
            url=url,
            selector=selector,
            name=TargetDialog.suggest_name(url, selector),
        )
        if preview:
            snippet = preview[:200].replace("\n", " ")
            self._on_status(f"Picked: {snippet}")

        if dialog.exec() != TargetDialog.DialogCode.Accepted:
            return

        target = dialog.result_target()
        self._storage.upsert_target(target)
        logger.info("Added target %r from picker", target.name)
        self._reload_targets()
        self._controller.wake()

    # ------------------------------------------------------------- misc
    def _open_settings(self) -> None:
        """Open the settings dialog and apply anything that changed live."""
        dialog = SettingsDialog(self._settings, self._dispatcher, self._secrets, self)
        if dialog.exec() != SettingsDialog.DialogCode.Accepted:
            return

        palette = palette_for(self._settings.theme)
        if palette != self._palette:
            self._palette = palette
            app = QApplication.instance()
            if app is not None:
                app.setStyleSheet(build_stylesheet(palette))  # type: ignore[attr-defined]
            self.setWindowIcon(make_app_icon(palette))
            self._tray.setIcon(make_app_icon(palette))

        logging.getLogger().setLevel(self._settings.log_level)

        if self._controller.is_running:
            self._on_status("Settings saved - restart monitoring to apply network changes")

    def _show_toast(self, title: str, body: str, duration_ms: int) -> None:
        """Display a native notification through the tray icon.

        Args:
            title: Notification title.
            body: Notification body.
            duration_ms: How long to display it.
        """
        self._tray.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information, duration_ms)

    def _append_log_threadsafe(self, line: str) -> None:
        """Queue a log line for display on the GUI thread.

        Args:
            line: The formatted log record. Called from arbitrary threads, so
                the actual widget write is marshalled with a zero-delay timer.
        """
        QTimer.singleShot(0, lambda: self._log.appendPlainText(line))

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Restore the window when the tray icon is clicked.

        Args:
            reason: How the icon was activated.
        """
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._restore_window()

    def _restore_window(self) -> None:
        """Bring the window back from the tray."""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit(self) -> None:
        """Shut down for real, bypassing minimise-to-tray."""
        self._force_quit = True
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        """Minimise to the tray, or shut everything down on a real quit.

        Args:
            event: The close event.
        """
        if self._settings.minimize_to_tray and not self._force_quit:
            event.ignore()
            self.hide()
            self._tray.showMessage(
                "Website Monitor",
                "Still running in the tray. Right-click the icon to quit.",
                QSystemTrayIcon.MessageIcon.Information,
                4000,
            )
            return

        logger.info("Shutting down")
        self._tick.stop()

        desktop = self._dispatcher.get("desktop")
        if desktop is not None and hasattr(desktop, "set_sink"):
            desktop.set_sink(None)

        logging.getLogger().removeHandler(self._log_handler)
        self._controller.stop()
        self._tray.hide()

        try:
            self._settings.save()
        except OSError:
            logger.exception("Could not save settings on exit")

        self._storage.close()
        event.accept()
