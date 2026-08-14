"""Email delivery over SMTP.

The password comes from :mod:`webmonitor.secrets_store` - the OS keyring or a
``WM_SMTP_PASSWORD`` environment variable - and is never written to
``settings.json``.

``smtplib`` is blocking, so the whole send is offloaded to a worker thread with
:func:`asyncio.to_thread`; a slow mail server therefore cannot stall the polling
loop.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import TYPE_CHECKING

from webmonitor.core.differ import render_diff_html, render_diff_text
from webmonitor.notifiers.base import Notifier, NotifierError
from webmonitor.secrets_store import SecretRef, SecretStore

if TYPE_CHECKING:
    from webmonitor.config import SmtpConfig
    from webmonitor.models import ChangeEvent

__all__ = ["EmailNotifier"]

logger = logging.getLogger(__name__)


class EmailNotifier(Notifier):
    """Sends an HTML email containing a colour-coded diff."""

    name = "email"
    label = "Email"

    def __init__(self, config: SmtpConfig, secrets: SecretStore | None = None) -> None:
        """Initialise the channel.

        Args:
            config: SMTP settings.
            secrets: Secret store used to look up the password. A default store
                is created when omitted.
        """
        self._config = config
        self._secrets = secrets or SecretStore()

    @property
    def is_configured(self) -> bool:
        """Whether an email can be sent.

        Returns:
            ``True`` when the SMTP settings are complete and a password is
            available.
        """
        return bool(self._config.is_configured and self._password())

    def _password(self) -> str | None:
        """Look up the SMTP password.

        Returns:
            The password, or ``None`` if it has not been stored.
        """
        return self._secrets.get(SecretRef.SMTP_PASSWORD)

    def describe_status(self) -> str:
        """Explain the channel's state.

        Returns:
            A short status string for the settings dialog.
        """
        if not self._config.enabled:
            return "Disabled"
        if not self._config.host:
            return "No SMTP server set"
        if not self._config.recipients:
            return "No recipients set"
        if not self._password():
            return "No password in keyring (or set WM_SMTP_PASSWORD)"
        return f"Ready - {len(self._config.recipients)} recipient(s)"

    def _build_message(self, event: ChangeEvent) -> EmailMessage:
        """Compose the multipart email.

        Args:
            event: The detected change.

        Returns:
            An :class:`EmailMessage` with both plain-text and HTML alternatives.
        """
        message = EmailMessage()
        message["Subject"] = f"[Website Monitor] {event.target.name} changed"
        message["From"] = formataddr(("Website Monitor", self._config.sender))
        message["To"] = ", ".join(self._config.recipients)
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain="websitemonitor.local")

        timestamp = event.detected_at.strftime("%Y-%m-%d %H:%M:%S UTC")

        message.set_content(
            f"{event.target.name} changed.\n\n"
            f"URL:      {event.target.url}\n"
            f"Selector: {event.target.selector or '(whole page)'}\n"
            f"Detected: {timestamp}\n"
            f"Change:   +{event.added_lines} / -{event.removed_lines} lines\n\n"
            f"{render_diff_text(event)}\n"
        )

        message.add_alternative(
            self._html_body(event, timestamp),
            subtype="html",
        )
        return message

    @staticmethod
    def _html_body(event: ChangeEvent, timestamp: str) -> str:
        """Render the HTML alternative.

        Args:
            event: The detected change.
            timestamp: Pre-formatted detection time.

        Returns:
            A complete HTML document.
        """
        import html as html_escape

        target = event.target
        return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:24px;background:#0f1115;
 font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#e6e8eb">
  <div style="max-width:680px;margin:0 auto">
    <h2 style="margin:0 0 4px;font-size:20px;color:#fff">
      {html_escape.escape(target.name)} changed
    </h2>
    <p style="margin:0 0 20px;color:#9aa4b2;font-size:13px">{timestamp}</p>

    <table style="width:100%;border-collapse:collapse;margin-bottom:20px;font-size:13px">
      <tr>
        <td style="padding:6px 0;color:#9aa4b2;width:90px">URL</td>
        <td style="padding:6px 0"><a href="{html_escape.escape(target.url)}"
            style="color:#60a5fa">{html_escape.escape(target.url)}</a></td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#9aa4b2">Selector</td>
        <td style="padding:6px 0;font-family:Consolas,monospace;color:#e6e8eb">
          {html_escape.escape(target.selector or "(whole page)")}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#9aa4b2">Change</td>
        <td style="padding:6px 0">
          <span style="color:#4ade80">+{event.added_lines}</span> /
          <span style="color:#ff6b81">-{event.removed_lines}</span> lines
        </td>
      </tr>
    </table>

    {render_diff_html(event)}

    <p style="margin:24px 0 0;color:#6b7280;font-size:11px">
      Sent by Website Monitor.
    </p>
  </div>
</body></html>"""

    def _send_blocking(self, message: EmailMessage, password: str) -> None:
        """Perform the blocking SMTP conversation.

        Args:
            message: The composed email.
            password: SMTP password.

        Raises:
            NotifierError: If the server rejected the connection, the login or
                the message.
        """
        config = self._config
        context = ssl.create_default_context()

        try:
            if config.use_ssl:
                server: smtplib.SMTP = smtplib.SMTP_SSL(
                    config.host, config.port, timeout=config.timeout, context=context
                )
            else:
                server = smtplib.SMTP(config.host, config.port, timeout=config.timeout)

            with server:
                server.ehlo()
                if not config.use_ssl:
                    server.starttls(context=context)
                    server.ehlo()
                server.login(config.username or config.sender, password)
                server.send_message(message)

        except smtplib.SMTPAuthenticationError as exc:
            raise NotifierError(
                "SMTP authentication failed. For Gmail and Outlook you must use "
                f"an app-specific password, not your account password ({exc.smtp_code})."
            ) from exc
        except smtplib.SMTPException as exc:
            raise NotifierError(f"SMTP error: {exc}") from exc
        except (OSError, ssl.SSLError) as exc:
            raise NotifierError(f"Could not reach {config.host}:{config.port} - {exc}") from exc

    async def send(self, event: ChangeEvent) -> None:
        """Send the change notification by email.

        Args:
            event: The detected change.

        Raises:
            NotifierError: If the channel is unconfigured or delivery failed.
        """
        password = self._password()
        if not self._config.is_configured or not password:
            raise NotifierError(f"Email channel not ready: {self.describe_status()}")

        message = self._build_message(event)
        await asyncio.to_thread(self._send_blocking, message, password)
        logger.info(
            "Email sent to %s for target %r", ", ".join(self._config.recipients), event.target.name
        )
