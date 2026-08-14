"""Application composition root.

Everything is constructed here and passed down explicitly - there are no module
level singletons - which is what makes the pieces substitutable in tests.
"""

from __future__ import annotations

import logging
import os
import sys

from webmonitor import __version__
from webmonitor.config import AppSettings, load_settings
from webmonitor.logging_setup import setup_logging
from webmonitor.notifiers import DesktopNotifier, Dispatcher, EmailNotifier, TelegramNotifier
from webmonitor.secrets_store import SecretStore
from webmonitor.storage import Storage

__all__ = ["build_dispatcher", "run"]

logger = logging.getLogger(__name__)


def build_dispatcher(settings: AppSettings, secrets: SecretStore) -> Dispatcher:
    """Create the dispatcher with every channel registered.

    Channels are registered whether or not they are configured; an unconfigured
    channel reports itself as such and is skipped at send time. That keeps the
    settings dialog able to list and test all of them.

    Args:
        settings: Application settings.
        secrets: Credential store shared by the channels.

    Returns:
        A dispatcher ready to be handed to the engine.
    """
    dispatcher = Dispatcher()
    dispatcher.register(DesktopNotifier(settings.desktop))
    dispatcher.register(EmailNotifier(settings.smtp, secrets))
    dispatcher.register(TelegramNotifier(settings.telegram, secrets))
    return dispatcher


def _configure_qt_environment() -> None:
    """Set Chromium and Qt options that must be in place before startup.

    Notes:
        ``QTWEBENGINE_CHROMIUM_FLAGS`` is only applied if the user has not set
        it themselves, so a deliberate override from the shell still wins.
    """
    os.environ.setdefault(
        "QTWEBENGINE_CHROMIUM_FLAGS",
        # Chromium's own cache is redundant for us and would mask changes; the
        # renderer is only ever used to execute JavaScript, never to serve a
        # cached copy of a page we are trying to diff.
        "--disable-features=Translate,MediaRouter --disable-background-timer-throttling",
    )
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")


def run(argv: list[str] | None = None) -> int:
    """Start the GUI application.

    Args:
        argv: Command-line arguments. Defaults to :data:`sys.argv`.

    Returns:
        The process exit code.
    """
    args = list(sys.argv if argv is None else argv)

    if "--version" in args:
        print(f"Website Monitor {__version__}")
        return 0

    _configure_qt_environment()

    # Imported after the environment is prepared and, critically, before
    # QApplication is constructed: QtWebEngine requires its widgets module to be
    # imported first or it aborts at startup.
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox

    from webmonitor.gui.main_window import MainWindow, make_app_icon
    from webmonitor.gui.theme import build_stylesheet, palette_for

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)

    settings = load_settings()
    setup_logging(settings.log_level, console=sys.stderr is not None)
    logger.info("Website Monitor %s starting", __version__)

    app = QApplication(args)
    app.setApplicationName("Website Monitor")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("WebsiteMonitor")
    # The tray keeps the app alive while the window is hidden.
    app.setQuitOnLastWindowClosed(not settings.minimize_to_tray)

    palette = palette_for(settings.theme)
    app.setStyleSheet(build_stylesheet(palette))
    app.setWindowIcon(make_app_icon(palette))

    secrets = SecretStore()
    if not secrets.is_available():
        logger.warning(
            "No system keyring available; email and Telegram credentials must "
            "come from the WM_SMTP_PASSWORD / WM_TELEGRAM_TOKEN environment variables"
        )

    try:
        storage = Storage()
    except Exception as exc:
        logger.exception("Could not open the database")
        QMessageBox.critical(
            None,
            "Website Monitor",
            f"Could not open the database:\n\n{exc}",
        )
        return 1

    dispatcher = build_dispatcher(settings, secrets)

    window = MainWindow(settings, storage, dispatcher, secrets)
    window.show()

    exit_code = app.exec()
    logger.info("Website Monitor exited with code %s", exit_code)
    return exit_code
