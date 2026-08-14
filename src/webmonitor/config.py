"""Application settings: typed, validated, file-backed and env-overridable.

Layering, highest priority first:

1. ``WM_*`` environment variables (``WM_SMTP__HOST``, ``WM_NETWORK__TIMEOUT``, ...).
2. ``settings.json`` in the platform data directory.
3. The defaults declared below.

Secrets are pointedly absent from this module - they live in
:mod:`webmonitor.secrets_store`, so ``settings.json`` never contains a password.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from webmonitor import paths

__all__ = [
    "AppSettings",
    "DesktopConfig",
    "NetworkConfig",
    "SchedulerConfig",
    "SmtpConfig",
    "TelegramConfig",
    "load_settings",
]

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 WebsiteMonitor/0.2"
)


class SmtpConfig(BaseModel):
    """Outgoing email configuration. The password lives in the keyring.

    Attributes:
        enabled: Whether the email channel is active.
        host: SMTP server hostname.
        port: SMTP server port. 587 for STARTTLS, 465 for implicit TLS.
        username: Login name; usually the same as ``sender``.
        sender: Address placed in the ``From`` header.
        recipients: Addresses placed in the ``To`` header.
        use_ssl: Connect with implicit TLS (``SMTP_SSL``) instead of STARTTLS.
        timeout: Socket timeout in seconds.
    """

    enabled: bool = False
    host: str = ""
    port: int = Field(default=587, ge=1, le=65535)
    username: str = ""
    sender: str = ""
    recipients: list[str] = Field(default_factory=list)
    use_ssl: bool = False
    timeout: float = Field(default=20.0, gt=0)

    @field_validator("recipients", mode="before")
    @classmethod
    def _split_recipients(cls, value: Any) -> Any:
        """Allow a comma-separated string, which is what env vars can carry.

        Args:
            value: Raw value from JSON or the environment.

        Returns:
            A list of trimmed addresses when given a string, else ``value``.
        """
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def is_configured(self) -> bool:
        """Whether enough is filled in to attempt a send.

        Returns:
            ``True`` when enabled and host, sender and recipients are all set.
        """
        return bool(self.enabled and self.host and self.sender and self.recipients)


class TelegramConfig(BaseModel):
    """Telegram bot channel. The bot token lives in the keyring.

    Attributes:
        enabled: Whether the Telegram channel is active.
        chat_id: Target chat, channel or user id.
        api_base: Bot API root; overridable for self-hosted API servers.
        timeout: Request timeout in seconds.
    """

    enabled: bool = False
    chat_id: str = ""
    api_base: str = "https://api.telegram.org"
    timeout: float = Field(default=15.0, gt=0)

    @property
    def is_configured(self) -> bool:
        """Whether the channel has the settings it needs.

        Returns:
            ``True`` when enabled and a chat id is present. The token is checked
            separately at send time because it comes from the keyring.
        """
        return bool(self.enabled and self.chat_id)


class DesktopConfig(BaseModel):
    """Native desktop toast notifications, delivered through the Qt tray icon.

    Attributes:
        enabled: Whether toasts are shown.
        duration_ms: How long the toast stays on screen.
        play_sound: Whether to accompany the toast with the system alert sound.
    """

    enabled: bool = True
    duration_ms: int = Field(default=8000, ge=1000, le=60000)
    play_sound: bool = True


class NetworkConfig(BaseModel):
    """HTTP client behaviour.

    Attributes:
        timeout: Per-request timeout in seconds.
        max_retries: Retry attempts for transient failures, with exponential
            backoff plus jitter between them.
        user_agent: ``User-Agent`` header. Some sites serve different markup to
            unknown agents, which would look like a change.
        proxy: Global proxy URL, e.g. ``http://user:pass@host:3128``. Individual
            targets may override it.
        verify_tls: Whether to verify certificates. Disable only for known
            self-signed internal hosts.
        http2: Enable HTTP/2 multiplexing.
        max_connections: Upper bound on the shared connection pool.
        max_response_bytes: Hard cap on downloaded body size, guarding against
            a watched URL that starts returning a huge file.
    """

    timeout: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)
    user_agent: str = DEFAULT_USER_AGENT
    proxy: str = ""
    verify_tls: bool = True
    http2: bool = True
    max_connections: int = Field(default=20, ge=1, le=200)
    max_response_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)


class SchedulerConfig(BaseModel):
    """Polling engine behaviour.

    Attributes:
        max_concurrent_checks: How many targets may be in flight at once. The
            practical limit is politeness to the watched servers, not local CPU.
        adaptive_intervals: Grow a target's interval while it stays quiet and
            reset it on change. Cuts request volume substantially on pages that
            rarely move.
        backoff_factor: Multiplier applied to the interval after a quiet check.
        error_backoff_base: First retry delay after a failure, in seconds;
            doubles per consecutive failure up to ``interval_max``.
        jitter_ratio: Random fraction of the interval added or subtracted so
            that many targets do not fire in lockstep.
        snapshot_history: Number of historical snapshots retained per target.
        check_on_start: Whether to poll every enabled target immediately when
            monitoring starts, rather than waiting out the first interval.
    """

    max_concurrent_checks: int = Field(default=8, ge=1, le=64)
    adaptive_intervals: bool = True
    backoff_factor: float = Field(default=1.5, ge=1.0, le=4.0)
    error_backoff_base: float = Field(default=60.0, gt=0)
    jitter_ratio: float = Field(default=0.1, ge=0.0, le=0.5)
    snapshot_history: int = Field(default=20, ge=1, le=500)
    check_on_start: bool = True


class AppSettings(BaseSettings):
    """Root settings object.

    Attributes:
        smtp: Email channel configuration.
        telegram: Telegram channel configuration.
        desktop: Desktop toast configuration.
        network: HTTP client configuration.
        scheduler: Polling engine configuration.
        theme: GUI theme, ``dark``, ``light`` or ``system``.
        minimize_to_tray: Keep running in the tray when the window is closed.
        start_monitoring_on_launch: Begin polling as soon as the app opens.
        log_level: Root logging level name.
    """

    model_config = SettingsConfigDict(
        env_prefix="WM_",
        env_nested_delimiter="__",
        extra="ignore",
        validate_assignment=True,
    )

    smtp: SmtpConfig = Field(default_factory=SmtpConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    desktop: DesktopConfig = Field(default_factory=DesktopConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)

    theme: str = "dark"
    minimize_to_tray: bool = True
    start_monitoring_on_launch: bool = False
    log_level: str = "INFO"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Put environment variables ahead of values loaded from the JSON file.

        Pydantic's default order gives ``init`` the highest priority, but we
        pass the JSON file contents *as* init kwargs, so without this override a
        stale ``settings.json`` would shadow a deliberate env override.

        Args:
            settings_cls: The settings class being built.
            init_settings: Values passed to ``__init__`` (our JSON file).
            env_settings: Values read from the environment.
            dotenv_settings: Values read from a ``.env`` file.
            file_secret_settings: Values read from a secrets directory.

        Returns:
            Sources in descending priority order.
        """
        return (env_settings, dotenv_settings, init_settings, file_secret_settings)

    def save(self, path: Path | None = None) -> None:
        """Write the settings to disk as pretty-printed JSON.

        The write goes to a temporary file that is then moved into place, so an
        interrupted save cannot leave a truncated config behind.

        Args:
            path: Destination file. Defaults to :func:`webmonitor.paths.config_file`.
        """
        target = path or paths.config_file()
        payload = self.model_dump(mode="json")
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(target)
        logger.debug("Settings written to %s", target)


def load_settings(path: Path | None = None) -> AppSettings:
    """Load settings from disk, falling back to defaults.

    A malformed or unreadable file is logged and ignored rather than being
    allowed to prevent startup; the user can then fix it from the GUI.

    Args:
        path: Settings file to read. Defaults to
            :func:`webmonitor.paths.config_file`.

    Returns:
        A validated :class:`AppSettings` with environment overrides applied.
    """
    source = path or paths.config_file()
    data: dict[str, Any] = {}

    if source.exists():
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
            logger.info("Loaded settings from %s", source)
        except (OSError, json.JSONDecodeError):
            logger.exception("Could not read %s; falling back to defaults", source)
            data = {}

    try:
        return AppSettings(**data)
    except Exception:
        logger.exception("Settings in %s failed validation; using defaults", source)
        return AppSettings()
