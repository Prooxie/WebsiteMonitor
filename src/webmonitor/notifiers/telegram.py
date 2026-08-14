"""Telegram bot delivery.

Free, instant, and it reaches a phone - which is what most people actually want
when they say "SMS". Setup is two steps: talk to `@BotFather
<https://t.me/BotFather>`_ to get a bot token, then send your bot a message and
read your chat id from ``https://api.telegram.org/bot<TOKEN>/getUpdates``.

The bot token lives in the keyring, never in ``settings.json``.
"""

from __future__ import annotations

import html as html_escape
import logging
from typing import TYPE_CHECKING, Any, Final

import httpx

from webmonitor.core.differ import render_diff_text
from webmonitor.notifiers.base import Notifier, NotifierError
from webmonitor.secrets_store import SecretRef, SecretStore

if TYPE_CHECKING:
    from webmonitor.config import TelegramConfig
    from webmonitor.models import ChangeEvent

__all__ = ["TelegramNotifier"]

logger = logging.getLogger(__name__)

#: Telegram rejects messages longer than 4096 UTF-16 code units.
_MAX_MESSAGE_CHARS: Final[int] = 3800


class TelegramNotifier(Notifier):
    """Sends a formatted message to a Telegram chat."""

    name = "telegram"
    label = "Telegram"

    def __init__(self, config: TelegramConfig, secrets: SecretStore | None = None) -> None:
        """Initialise the channel.

        Args:
            config: Telegram settings.
            secrets: Secret store used to look up the bot token. A default store
                is created when omitted.
        """
        self._config = config
        self._secrets = secrets or SecretStore()

    @property
    def is_configured(self) -> bool:
        """Whether a message can be sent.

        Returns:
            ``True`` when enabled with both a chat id and a bot token.
        """
        return bool(self._config.is_configured and self._token())

    def _token(self) -> str | None:
        """Look up the bot token.

        Returns:
            The token, or ``None`` if it has not been stored.
        """
        return self._secrets.get(SecretRef.TELEGRAM_TOKEN)

    def describe_status(self) -> str:
        """Explain the channel's state.

        Returns:
            A short status string for the settings dialog.
        """
        if not self._config.enabled:
            return "Disabled"
        if not self._config.chat_id:
            return "No chat ID set"
        if not self._token():
            return "No bot token in keyring (or set WM_TELEGRAM_TOKEN)"
        return f"Ready - chat {self._config.chat_id}"

    @staticmethod
    def _build_text(event: ChangeEvent) -> str:
        """Compose the message body in Telegram's HTML parse mode.

        Args:
            event: The detected change.

        Returns:
            An HTML-formatted string within Telegram's length limit.
        """
        escape = html_escape.escape
        header = (
            f"<b>{escape(event.target.name)} changed</b>\n"
            f'<a href="{escape(event.target.url)}">{escape(event.target.url)}</a>\n'
            f"<code>{escape(event.target.selector or '(whole page)')}</code>\n\n"
            f"+{event.added_lines} added, -{event.removed_lines} removed\n"
        )

        budget = _MAX_MESSAGE_CHARS - len(header) - 32
        diff = render_diff_text(event, max_lines=40)
        if len(diff) > budget:
            diff = diff[:budget] + "\n... truncated ..."

        return f"{header}\n<pre>{escape(diff)}</pre>"

    async def send(self, event: ChangeEvent) -> None:
        """Send the change notification to Telegram.

        Args:
            event: The detected change.

        Raises:
            NotifierError: If the channel is unconfigured, the network call
                failed, or the Bot API returned an error.
        """
        token = self._token()
        if not self._config.is_configured or not token:
            raise NotifierError(f"Telegram channel not ready: {self.describe_status()}")

        url = f"{self._config.api_base.rstrip('/')}/bot{token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": self._config.chat_id,
            "text": self._build_text(event),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            async with httpx.AsyncClient(timeout=self._config.timeout) as client:
                response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise NotifierError(f"Could not reach the Telegram API: {exc}") from exc

        if response.status_code != 200:
            # Surface Telegram's own description; it is genuinely helpful
            # ("chat not found", "bot was blocked by the user", ...).
            detail = ""
            try:
                detail = response.json().get("description", "")
            except ValueError:
                detail = response.text[:200]
            raise NotifierError(f"Telegram API error {response.status_code}: {detail}")

        logger.info("Telegram message sent for target %r", event.target.name)
