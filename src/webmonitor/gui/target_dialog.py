"""Dialog for creating and editing a watched target."""

from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from webmonitor.models import Normalization, RenderMode, Target

__all__ = ["TargetDialog"]

logger = logging.getLogger(__name__)

#: Friendly labels for the render modes, in dialog order.
_RENDER_LABELS: tuple[tuple[str, RenderMode], ...] = (
    ("Auto - try fast fetch, use browser only if needed", RenderMode.AUTO),
    ("Static - plain HTTP fetch (fastest)", RenderMode.STATIC),
    ("Browser - always render JavaScript (slowest)", RenderMode.BROWSER),
)


class TargetDialog(QDialog):
    """Create or edit a :class:`~webmonitor.models.Target`."""

    def __init__(
        self,
        target: Target | None = None,
        *,
        available_channels: tuple[tuple[str, str], ...] = (),
        parent: QWidget | None = None,
    ) -> None:
        """Build the dialog.

        Args:
            target: Existing target to edit, or ``None`` to create a new one.
            available_channels: ``(name, label)`` pairs for the notification
                channel checkboxes.
            parent: Parent widget.
        """
        super().__init__(parent)
        self._target = target
        self._channel_boxes: dict[str, QCheckBox] = {}

        self.setWindowTitle("Edit target" if target else "New target")
        self.setMinimumWidth(560)
        self._build_ui(available_channels)
        if target:
            self._load(target)

    def _build_ui(self, available_channels: tuple[tuple[str, str], ...]) -> None:
        """Construct the dialog's widgets.

        Args:
            available_channels: ``(name, label)`` pairs for channel checkboxes.
        """
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(18, 18, 18, 18)

        # ---------------- What to watch ----------------
        what = QGroupBox("What to watch")
        what_form = QFormLayout(what)
        what_form.setSpacing(10)

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Shop - new arrivals")

        self._url_edit = QLineEdit()
        self._url_edit.setPlaceholderText("https://example.com/products")

        self._selector_edit = QLineEdit()
        self._selector_edit.setProperty("monospace", "true")
        self._selector_edit.setPlaceholderText("Leave empty to watch the whole page")

        selector_hint = QLabel("Use “Pick element” in the Inspector to fill this in.")
        selector_hint.setProperty("dim", "true")

        what_form.addRow("Name", self._name_edit)
        what_form.addRow("URL", self._url_edit)
        what_form.addRow("CSS selector", self._selector_edit)
        what_form.addRow("", selector_hint)
        layout.addWidget(what)

        # ---------------- How often ----------------
        timing = QGroupBox("How often")
        timing_form = QFormLayout(timing)
        timing_form.setSpacing(10)

        interval_row = QHBoxLayout()
        self._min_spin = QSpinBox()
        self._min_spin.setRange(10, 86400)
        self._min_spin.setValue(300)
        self._min_spin.setSuffix(" s")
        self._min_spin.setToolTip("Fastest polling rate, used right after a change")

        self._max_spin = QSpinBox()
        self._max_spin.setRange(10, 604800)
        self._max_spin.setValue(3600)
        self._max_spin.setSuffix(" s")
        self._max_spin.setToolTip("Slowest polling rate, reached after long quiet periods")

        interval_row.addWidget(QLabel("from"))
        interval_row.addWidget(self._min_spin)
        interval_row.addWidget(QLabel("up to"))
        interval_row.addWidget(self._max_spin)
        interval_row.addStretch(1)
        timing_form.addRow("Check interval", interval_row)

        self._mode_combo = QComboBox()
        for label, _mode in _RENDER_LABELS:
            self._mode_combo.addItem(label)
        timing_form.addRow("Fetch mode", self._mode_combo)

        self._proxy_edit = QLineEdit()
        self._proxy_edit.setPlaceholderText("Optional, e.g. http://user:pass@host:3128")
        timing_form.addRow("Proxy", self._proxy_edit)

        self._enabled_check = QCheckBox("Monitoring enabled")
        self._enabled_check.setChecked(True)
        timing_form.addRow("", self._enabled_check)
        layout.addWidget(timing)

        # ---------------- Ignore noise ----------------
        noise = QGroupBox("Ignore noise")
        noise_layout = QVBoxLayout(noise)
        noise_layout.setSpacing(6)

        self._text_only = QCheckBox("Compare visible text only (ignore HTML markup)")
        self._text_only.setChecked(True)
        self._text_only.setToolTip(
            "Strongly recommended. Immune to CSS class churn and attribute noise."
        )
        self._strip_scripts = QCheckBox("Strip scripts, styles and comments")
        self._strip_scripts.setChecked(True)
        self._collapse_ws = QCheckBox("Collapse whitespace")
        self._collapse_ws.setChecked(True)
        self._ignore_numbers = QCheckBox("Ignore numbers (view counts, prices, timestamps)")
        self._ignore_attrs = QCheckBox("Ignore HTML attributes")
        self._ignore_attrs.setToolTip("Only applies when comparing markup rather than text.")

        for box in (
            self._text_only,
            self._strip_scripts,
            self._collapse_ws,
            self._ignore_numbers,
            self._ignore_attrs,
        ):
            noise_layout.addWidget(box)

        self._ignore_patterns = QLineEdit()
        self._ignore_patterns.setProperty("monospace", "true")
        self._ignore_patterns.setPlaceholderText(
            "Optional regex patterns to strip, separated by ;;"
        )
        noise_layout.addWidget(self._ignore_patterns)
        layout.addWidget(noise)

        # ---------------- Notify ----------------
        if available_channels:
            notify = QGroupBox("Notify via")
            notify_layout = QVBoxLayout(notify)
            hint = QLabel("Leave all unticked to use every configured channel.")
            hint.setProperty("dim", "true")
            notify_layout.addWidget(hint)
            for name, label in available_channels:
                box = QCheckBox(label)
                self._channel_boxes[name] = box
                notify_layout.addWidget(box)
            layout.addWidget(notify)

        # ---------------- Buttons ----------------
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        save = buttons.button(QDialogButtonBox.StandardButton.Save)
        save.setProperty("accent", "true")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._text_only.toggled.connect(lambda checked: self._ignore_attrs.setEnabled(not checked))
        self._ignore_attrs.setEnabled(not self._text_only.isChecked())

    def prefill(self, url: str = "", selector: str = "", name: str = "") -> None:
        """Pre-populate fields, typically from the inspector.

        Args:
            url: URL to insert.
            selector: CSS selector to insert.
            name: Suggested display name.
        """
        if url:
            self._url_edit.setText(url)
        if selector:
            self._selector_edit.setText(selector)
        if name and not self._name_edit.text():
            self._name_edit.setText(name)

    def _load(self, target: Target) -> None:
        """Populate the form from an existing target.

        Args:
            target: The target being edited.
        """
        self._name_edit.setText(target.name)
        self._url_edit.setText(target.url)
        self._selector_edit.setText(target.selector)
        self._min_spin.setValue(target.interval_min)
        self._max_spin.setValue(target.interval_max)
        self._proxy_edit.setText(target.proxy or "")
        self._enabled_check.setChecked(target.enabled)

        for index, (_label, mode) in enumerate(_RENDER_LABELS):
            if mode is target.render_mode:
                self._mode_combo.setCurrentIndex(index)
                break

        rules = target.normalization
        self._text_only.setChecked(rules.text_only)
        self._strip_scripts.setChecked(rules.strip_scripts)
        self._collapse_ws.setChecked(rules.collapse_whitespace)
        self._ignore_numbers.setChecked(rules.ignore_numbers)
        self._ignore_attrs.setChecked(rules.ignore_attributes)
        self._ignore_patterns.setText(";;".join(rules.ignore_patterns))

        for name, box in self._channel_boxes.items():
            box.setChecked(name in target.channels)

    def _on_accept(self) -> None:
        """Validate the form and accept the dialog if it is sound."""
        if not self._url_edit.text().strip():
            QMessageBox.warning(self, "URL required", "Enter the URL you want to watch.")
            self._url_edit.setFocus()
            return

        if self._max_spin.value() < self._min_spin.value():
            QMessageBox.warning(
                self,
                "Check interval",
                "The maximum interval must be at least the minimum interval.",
            )
            self._max_spin.setFocus()
            return

        self.accept()

    def result_target(self) -> Target:
        """Build a target from the current form values.

        Returns:
            A new :class:`~webmonitor.models.Target`, or an updated copy of the
            one passed to the constructor, preserving its id and stored state.
        """
        url = self._url_edit.text().strip()
        if "://" not in url:
            url = f"https://{url}"

        name = self._name_edit.text().strip() or url
        patterns = tuple(
            part.strip() for part in self._ignore_patterns.text().split(";;") if part.strip()
        )

        normalization = Normalization(
            text_only=self._text_only.isChecked(),
            strip_scripts=self._strip_scripts.isChecked(),
            collapse_whitespace=self._collapse_ws.isChecked(),
            ignore_numbers=self._ignore_numbers.isChecked(),
            ignore_attributes=self._ignore_attrs.isChecked(),
            ignore_patterns=patterns,
        )

        channels = tuple(channel for channel, box in self._channel_boxes.items() if box.isChecked())
        mode = _RENDER_LABELS[self._mode_combo.currentIndex()][1]

        fields = {
            "name": name,
            "url": url,
            "selector": self._selector_edit.text().strip(),
            "enabled": self._enabled_check.isChecked(),
            "render_mode": mode,
            "interval_min": self._min_spin.value(),
            "interval_max": self._max_spin.value(),
            "normalization": normalization,
            "channels": channels,
            "proxy": self._proxy_edit.text().strip() or None,
        }

        if self._target is not None:
            # Changing what is compared invalidates the stored baseline, so drop
            # it rather than alerting on the first check after an edit.
            invalidated = (
                self._target.selector != fields["selector"]
                or self._target.normalization != normalization
                or self._target.url != url
            )
            updates: dict[str, object] = dict(fields)
            if invalidated:
                updates |= {"content_hash": None, "etag": None, "last_modified": None}
                logger.info("Baseline reset for %r after an edit", name)
            updates["interval_current"] = min(
                max(self._target.interval_current, fields["interval_min"]),  # type: ignore[arg-type]
                fields["interval_max"],  # type: ignore[arg-type]
            )
            return self._target.with_updates(**updates)

        return Target(**fields)  # type: ignore[arg-type]

    @staticmethod
    def suggest_name(url: str, selector: str) -> str:
        """Derive a readable default name from a URL.

        Args:
            url: The target URL.
            selector: The chosen selector, used as a fallback hint.

        Returns:
            A short suggested name such as ``"example.com/products"``.
        """
        from urllib.parse import urlparse

        parsed = urlparse(url if "://" in url else f"https://{url}")
        host = parsed.netloc.removeprefix("www.")
        path = parsed.path.rstrip("/")
        if path and len(path) < 30:
            return f"{host}{path}"
        return host or selector or "New target"
