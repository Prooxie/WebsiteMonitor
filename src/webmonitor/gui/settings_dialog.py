"""Settings dialog: notification channels, network and scheduler tuning.

Passwords and tokens entered here go straight to the OS keyring through
:class:`~webmonitor.secrets_store.SecretStore` and are never written to
``settings.json``.
"""

from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from webmonitor.config import AppSettings
from webmonitor.gui.engine_controller import AsyncTaskRunner
from webmonitor.notifiers.dispatcher import Dispatcher
from webmonitor.secrets_store import KeyringUnavailableError, SecretRef, SecretStore

__all__ = ["SettingsDialog"]

logger = logging.getLogger(__name__)

#: Placeholder shown in a password field that already holds a stored secret, so
#: the real value is never round-tripped through the widget.
_KEPT = "•" * 12


class SettingsDialog(QDialog):
    """Edit application settings and notification credentials."""

    def __init__(
        self,
        settings: AppSettings,
        dispatcher: Dispatcher,
        secrets: SecretStore,
        parent: QWidget | None = None,
    ) -> None:
        """Build the dialog.

        Args:
            settings: Settings object, mutated in place on save.
            dispatcher: Used to send test notifications.
            secrets: Credential store for passwords and tokens.
            parent: Parent widget.
        """
        super().__init__(parent)
        self._settings = settings
        self._dispatcher = dispatcher
        self._secrets = secrets
        self._runner = AsyncTaskRunner(self)
        self._runner.finished.connect(self._on_test_finished)

        self.setWindowTitle("Settings")
        self.setMinimumSize(620, 620)
        self._build_ui()
        self._load()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        """Construct the tabbed settings form."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        if not self._secrets.is_available():
            warning = QLabel(
                "⚠  No system keyring is available. Passwords cannot be saved here — "
                "set the WM_SMTP_PASSWORD and WM_TELEGRAM_TOKEN environment "
                "variables instead."
            )
            warning.setWordWrap(True)
            warning.setProperty("dim", "true")
            layout.addWidget(warning)

        tabs = QTabWidget()
        tabs.addTab(self._build_email_tab(), "Email")
        tabs.addTab(self._build_telegram_tab(), "Telegram")
        tabs.addTab(self._build_desktop_tab(), "Desktop")
        tabs.addTab(self._build_advanced_tab(), "Advanced")
        layout.addWidget(tabs, stretch=1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setProperty("accent", "true")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_email_tab(self) -> QWidget:
        """Build the SMTP settings tab.

        Returns:
            The populated tab widget.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        self._email_enabled = QCheckBox("Send email when a watched element changes")
        layout.addWidget(self._email_enabled)

        group = QGroupBox("SMTP server")
        form = QFormLayout(group)
        form.setSpacing(10)

        self._smtp_host = QLineEdit()
        self._smtp_host.setPlaceholderText("smtp.gmail.com")

        port_row = QHBoxLayout()
        self._smtp_port = QSpinBox()
        self._smtp_port.setRange(1, 65535)
        self._smtp_port.setValue(587)
        self._smtp_ssl = QCheckBox("Implicit TLS (port 465)")
        port_row.addWidget(self._smtp_port)
        port_row.addWidget(self._smtp_ssl)
        port_row.addStretch(1)

        self._smtp_user = QLineEdit()
        self._smtp_user.setPlaceholderText("Leave empty to use the sender address")
        self._smtp_sender = QLineEdit()
        self._smtp_sender.setPlaceholderText("you@gmail.com")
        self._smtp_password = QLineEdit()
        self._smtp_password.setEchoMode(QLineEdit.EchoMode.Password)
        self._smtp_password.setPlaceholderText("App password")
        self._smtp_recipients = QLineEdit()
        self._smtp_recipients.setPlaceholderText("me@example.com, someone@example.com")

        form.addRow("Server", self._smtp_host)
        form.addRow("Port", port_row)
        form.addRow("Username", self._smtp_user)
        form.addRow("From", self._smtp_sender)
        form.addRow("Password", self._smtp_password)
        form.addRow("To", self._smtp_recipients)
        layout.addWidget(group)

        note = QLabel(
            "Gmail and Outlook reject account passwords. Create an app-specific "
            "password and paste that here. It is stored in the Windows Credential "
            "Manager, not in any config file."
        )
        note.setWordWrap(True)
        note.setProperty("dim", "true")
        layout.addWidget(note)

        self._email_status = QLabel()
        self._email_status.setProperty("dim", "true")
        test_row = QHBoxLayout()
        test_btn = QPushButton("Send test email")
        test_btn.clicked.connect(lambda: self._test("email"))
        test_row.addWidget(test_btn)
        test_row.addWidget(self._email_status, stretch=1)
        layout.addLayout(test_row)

        layout.addStretch(1)
        return page

    def _build_telegram_tab(self) -> QWidget:
        """Build the Telegram settings tab.

        Returns:
            The populated tab widget.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        self._tg_enabled = QCheckBox("Send a Telegram message when a watched element changes")
        layout.addWidget(self._tg_enabled)

        group = QGroupBox("Bot")
        form = QFormLayout(group)
        form.setSpacing(10)

        self._tg_token = QLineEdit()
        self._tg_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._tg_token.setPlaceholderText("123456789:AA...")
        self._tg_chat = QLineEdit()
        self._tg_chat.setPlaceholderText("Your numeric chat ID")

        form.addRow("Bot token", self._tg_token)
        form.addRow("Chat ID", self._tg_chat)
        layout.addWidget(group)

        note = QLabel(
            "Setup: message @BotFather on Telegram and run /newbot to get a token. "
            "Then send your new bot any message and open "
            "https://api.telegram.org/bot<TOKEN>/getUpdates to read your chat ID.\n\n"
            "This reaches your phone instantly and costs nothing, which is why it "
            "is here instead of SMS."
        )
        note.setWordWrap(True)
        note.setProperty("dim", "true")
        layout.addWidget(note)

        self._tg_status = QLabel()
        self._tg_status.setProperty("dim", "true")
        test_row = QHBoxLayout()
        test_btn = QPushButton("Send test message")
        test_btn.clicked.connect(lambda: self._test("telegram"))
        test_row.addWidget(test_btn)
        test_row.addWidget(self._tg_status, stretch=1)
        layout.addLayout(test_row)

        layout.addStretch(1)
        return page

    def _build_desktop_tab(self) -> QWidget:
        """Build the desktop notification tab.

        Returns:
            The populated tab widget.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        self._desktop_enabled = QCheckBox("Show a desktop notification on change")
        layout.addWidget(self._desktop_enabled)

        group = QGroupBox("Behaviour")
        form = QFormLayout(group)
        self._desktop_duration = QSpinBox()
        self._desktop_duration.setRange(1000, 60000)
        self._desktop_duration.setSingleStep(1000)
        self._desktop_duration.setSuffix(" ms")
        form.addRow("Display for", self._desktop_duration)
        layout.addWidget(group)

        self._desktop_status = QLabel()
        self._desktop_status.setProperty("dim", "true")
        test_row = QHBoxLayout()
        test_btn = QPushButton("Show test notification")
        test_btn.clicked.connect(lambda: self._test("desktop"))
        test_row.addWidget(test_btn)
        test_row.addWidget(self._desktop_status, stretch=1)
        layout.addLayout(test_row)

        layout.addStretch(1)
        return page

    def _build_advanced_tab(self) -> QWidget:
        """Build the network, scheduler and appearance tab.

        Returns:
            The populated tab widget.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        network = QGroupBox("Network")
        network_form = QFormLayout(network)
        network_form.setSpacing(10)

        self._timeout = QDoubleSpinBox()
        self._timeout.setRange(1.0, 300.0)
        self._timeout.setSuffix(" s")

        self._retries = QSpinBox()
        self._retries.setRange(0, 10)

        self._proxy = QLineEdit()
        self._proxy.setPlaceholderText("http://user:pass@host:3128")

        self._user_agent = QLineEdit()
        self._http2 = QCheckBox("Use HTTP/2 when the server supports it")
        self._verify_tls = QCheckBox("Verify TLS certificates")

        network_form.addRow("Timeout", self._timeout)
        network_form.addRow("Retries", self._retries)
        network_form.addRow("Proxy", self._proxy)
        network_form.addRow("User agent", self._user_agent)
        network_form.addRow("", self._http2)
        network_form.addRow("", self._verify_tls)
        layout.addWidget(network)

        scheduler = QGroupBox("Scheduler")
        scheduler_form = QFormLayout(scheduler)
        scheduler_form.setSpacing(10)

        self._concurrency = QSpinBox()
        self._concurrency.setRange(1, 64)
        self._concurrency.setToolTip("How many targets may be checked at the same time")

        self._adaptive = QCheckBox("Slow down polling for pages that rarely change")
        self._adaptive.setToolTip(
            "Grows each target's interval while it stays quiet and resets it the "
            "moment something changes. Cuts request volume substantially."
        )
        self._check_on_start = QCheckBox("Check every target immediately when monitoring starts")

        self._history = QSpinBox()
        self._history.setRange(1, 500)
        self._history.setToolTip("Snapshots kept per target for diffing and history")

        scheduler_form.addRow("Concurrent checks", self._concurrency)
        scheduler_form.addRow("Snapshot history", self._history)
        scheduler_form.addRow("", self._adaptive)
        scheduler_form.addRow("", self._check_on_start)
        layout.addWidget(scheduler)

        appearance = QGroupBox("Application")
        appearance_form = QFormLayout(appearance)
        self._theme = QComboBox()
        self._theme.addItems(["dark", "light", "system"])
        self._log_level = QComboBox()
        self._log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self._tray = QCheckBox("Keep running in the tray when the window is closed")
        self._autostart = QCheckBox("Start monitoring when the app launches")

        appearance_form.addRow("Theme", self._theme)
        appearance_form.addRow("Log level", self._log_level)
        appearance_form.addRow("", self._tray)
        appearance_form.addRow("", self._autostart)
        layout.addWidget(appearance)

        layout.addStretch(1)
        return page

    # -------------------------------------------------------------- state
    def _load(self) -> None:
        """Populate every widget from the settings object."""
        settings = self._settings

        smtp = settings.smtp
        self._email_enabled.setChecked(smtp.enabled)
        self._smtp_host.setText(smtp.host)
        self._smtp_port.setValue(smtp.port)
        self._smtp_ssl.setChecked(smtp.use_ssl)
        self._smtp_user.setText(smtp.username)
        self._smtp_sender.setText(smtp.sender)
        self._smtp_recipients.setText(", ".join(smtp.recipients))
        if self._secrets.get(SecretRef.SMTP_PASSWORD):
            self._smtp_password.setText(_KEPT)

        telegram = settings.telegram
        self._tg_enabled.setChecked(telegram.enabled)
        self._tg_chat.setText(telegram.chat_id)
        if self._secrets.get(SecretRef.TELEGRAM_TOKEN):
            self._tg_token.setText(_KEPT)

        self._desktop_enabled.setChecked(settings.desktop.enabled)
        self._desktop_duration.setValue(settings.desktop.duration_ms)

        network = settings.network
        self._timeout.setValue(network.timeout)
        self._retries.setValue(network.max_retries)
        self._proxy.setText(network.proxy)
        self._user_agent.setText(network.user_agent)
        self._http2.setChecked(network.http2)
        self._verify_tls.setChecked(network.verify_tls)

        scheduler = settings.scheduler
        self._concurrency.setValue(scheduler.max_concurrent_checks)
        self._history.setValue(scheduler.snapshot_history)
        self._adaptive.setChecked(scheduler.adaptive_intervals)
        self._check_on_start.setChecked(scheduler.check_on_start)

        self._theme.setCurrentText(settings.theme)
        self._log_level.setCurrentText(settings.log_level)
        self._tray.setChecked(settings.minimize_to_tray)
        self._autostart.setChecked(settings.start_monitoring_on_launch)

        self._refresh_statuses()

    def _refresh_statuses(self) -> None:
        """Update the per-channel readiness labels."""
        for name, label in (
            ("email", self._email_status),
            ("telegram", self._tg_status),
            ("desktop", self._desktop_status),
        ):
            notifier = self._dispatcher.get(name)
            label.setText(notifier.describe_status() if notifier else "Not available")

    def _store_secret(self, key: str, widget: QLineEdit) -> None:
        """Persist a secret unless the field still holds the placeholder.

        Args:
            key: One of the :class:`~webmonitor.secrets_store.SecretRef` constants.
            widget: The password field to read.
        """
        value = widget.text()
        if value == _KEPT:
            return  # Unchanged; leave the stored secret alone.
        try:
            self._secrets.set(key, value)
        except KeyringUnavailableError as exc:
            QMessageBox.warning(
                self,
                "Could not save credential",
                f"{exc}\n\nThe rest of your settings were saved.",
            )

    def _on_save(self) -> None:
        """Write the form back into settings, persist it, and close."""
        settings = self._settings

        settings.smtp.enabled = self._email_enabled.isChecked()
        settings.smtp.host = self._smtp_host.text().strip()
        settings.smtp.port = self._smtp_port.value()
        settings.smtp.use_ssl = self._smtp_ssl.isChecked()
        settings.smtp.username = self._smtp_user.text().strip()
        settings.smtp.sender = self._smtp_sender.text().strip()
        settings.smtp.recipients = [
            part.strip() for part in self._smtp_recipients.text().split(",") if part.strip()
        ]

        settings.telegram.enabled = self._tg_enabled.isChecked()
        settings.telegram.chat_id = self._tg_chat.text().strip()

        settings.desktop.enabled = self._desktop_enabled.isChecked()
        settings.desktop.duration_ms = self._desktop_duration.value()

        settings.network.timeout = self._timeout.value()
        settings.network.max_retries = self._retries.value()
        settings.network.proxy = self._proxy.text().strip()
        settings.network.user_agent = self._user_agent.text().strip()
        settings.network.http2 = self._http2.isChecked()
        settings.network.verify_tls = self._verify_tls.isChecked()

        settings.scheduler.max_concurrent_checks = self._concurrency.value()
        settings.scheduler.snapshot_history = self._history.value()
        settings.scheduler.adaptive_intervals = self._adaptive.isChecked()
        settings.scheduler.check_on_start = self._check_on_start.isChecked()

        settings.theme = self._theme.currentText()
        settings.log_level = self._log_level.currentText()
        settings.minimize_to_tray = self._tray.isChecked()
        settings.start_monitoring_on_launch = self._autostart.isChecked()

        self._store_secret(SecretRef.SMTP_PASSWORD, self._smtp_password)
        self._store_secret(SecretRef.TELEGRAM_TOKEN, self._tg_token)

        try:
            settings.save()
        except OSError as exc:
            QMessageBox.critical(self, "Could not save settings", str(exc))
            return

        logger.info("Settings saved")
        self.accept()

    # --------------------------------------------------------------- test
    def _test(self, channel: str) -> None:
        """Send a test notification through one channel.

        The current form values are committed to the in-memory settings first,
        so the user can test without saving and reopening the dialog.

        Args:
            channel: Registered channel name.
        """
        self._commit_for_test(channel)
        self._set_status(channel, "Sending…")
        self._runner.run(
            channel,
            lambda: self._dispatcher.test_channel(channel),
            success_message="Sent - check that it arrived",
        )

    def _commit_for_test(self, channel: str) -> None:
        """Apply just enough of the form to make a test send meaningful.

        Args:
            channel: The channel about to be tested.
        """
        if channel == "email":
            self._settings.smtp.enabled = True
            self._settings.smtp.host = self._smtp_host.text().strip()
            self._settings.smtp.port = self._smtp_port.value()
            self._settings.smtp.use_ssl = self._smtp_ssl.isChecked()
            self._settings.smtp.username = self._smtp_user.text().strip()
            self._settings.smtp.sender = self._smtp_sender.text().strip()
            self._settings.smtp.recipients = [
                part.strip() for part in self._smtp_recipients.text().split(",") if part.strip()
            ]
            self._store_secret(SecretRef.SMTP_PASSWORD, self._smtp_password)
        elif channel == "telegram":
            self._settings.telegram.enabled = True
            self._settings.telegram.chat_id = self._tg_chat.text().strip()
            self._store_secret(SecretRef.TELEGRAM_TOKEN, self._tg_token)
        elif channel == "desktop":
            self._settings.desktop.enabled = True
            self._settings.desktop.duration_ms = self._desktop_duration.value()

    def _set_status(self, channel: str, message: str) -> None:
        """Write a message into a channel's status label.

        Args:
            channel: Registered channel name.
            message: Text to display.
        """
        label = {
            "email": self._email_status,
            "telegram": self._tg_status,
            "desktop": self._desktop_status,
        }.get(channel)
        if label is not None:
            label.setText(message)

    def _on_test_finished(self, channel: str, ok: bool, message: str) -> None:
        """Report the outcome of a test send.

        Args:
            channel: Channel that was tested.
            ok: Whether the send succeeded.
            message: Success text or the error description.
        """
        self._set_status(channel, ("✓ " if ok else "✗ ") + message)
