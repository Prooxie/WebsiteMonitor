"""Credential storage backed by the operating system's secret store.

This module replaces the previous ``bcrypt``-based approach, which could not
work: bcrypt is a deliberately one-way password *hash*, so a hashed SMTP
password can never be turned back into the plaintext that ``SMTP.login`` needs.
Hashing is the right tool for *verifying* a password someone types at you; it is
the wrong tool for *storing* a password you must later present to a third party.

What we need is reversible storage guarded by the OS, which is exactly what
``keyring`` provides - Windows Credential Manager, macOS Keychain or
SecretService. Secrets therefore never touch ``settings.json``, and the config
file is safe to commit or sync.

Environment variables take precedence over the keyring so that CI, containers
and headless runs can inject credentials without a desktop session.
"""

from __future__ import annotations

import logging
import os
from typing import Final

__all__ = ["KeyringUnavailableError", "SecretRef", "SecretStore"]

logger = logging.getLogger(__name__)

SERVICE_NAME: Final[str] = "WebsiteMonitor"


class KeyringUnavailableError(RuntimeError):
    """Raised when no usable keyring backend is present on the system."""


class SecretRef:
    """Well-known secret keys, kept in one place to avoid typo-driven bugs.

    Attributes:
        SMTP_PASSWORD: Password or app-specific token for the SMTP account.
        TELEGRAM_TOKEN: Bot token issued by BotFather.
        TWILIO_AUTH_TOKEN: Reserved for the SMS channel, not wired up yet.
    """

    SMTP_PASSWORD: Final[str] = "smtp_password"
    TELEGRAM_TOKEN: Final[str] = "telegram_token"
    TWILIO_AUTH_TOKEN: Final[str] = "twilio_auth_token"


def _env_var_for(key: str) -> str:
    """Map a secret key to its environment-variable override name.

    Args:
        key: One of the :class:`SecretRef` constants.

    Returns:
        The upper-cased, ``WM_``-prefixed variable name, e.g. ``WM_SMTP_PASSWORD``.
    """
    return f"WM_{key.upper()}"


class SecretStore:
    """Read and write secrets, preferring environment variables over the keyring.

    The keyring module is imported lazily so that importing this module - and
    therefore the whole core layer - never fails on a machine without a
    functioning secret backend.
    """

    def __init__(self, service: str = SERVICE_NAME) -> None:
        """Initialise the store.

        Args:
            service: Service name under which secrets are filed in the OS store.
        """
        self._service = service
        self._cache: dict[str, str | None] = {}

    @staticmethod
    def _backend() -> object:
        """Import and return the keyring module.

        Returns:
            The imported ``keyring`` module.

        Raises:
            KeyringUnavailableError: If keyring is missing or has only the
                inert "fail" backend available.
        """
        try:
            import keyring
            from keyring.backends.fail import Keyring as FailKeyring
        except ImportError as exc:  # pragma: no cover - depends on install
            raise KeyringUnavailableError("keyring is not installed") from exc

        if isinstance(keyring.get_keyring(), FailKeyring):  # pragma: no cover - env specific
            raise KeyringUnavailableError(
                "No usable keyring backend found. Set the WM_* environment "
                "variables instead, e.g. WM_SMTP_PASSWORD."
            )
        return keyring

    def get(self, key: str) -> str | None:
        """Retrieve a secret.

        Args:
            key: One of the :class:`SecretRef` constants.

        Returns:
            The secret value, or ``None`` if it is set nowhere. Environment
            variables win over the keyring; keyring failures are logged and
            treated as "not set" rather than raised, so a missing backend
            degrades one notification channel instead of the whole app.
        """
        env_value = os.environ.get(_env_var_for(key))
        if env_value:
            return env_value

        if key in self._cache:
            return self._cache[key]

        try:
            keyring = self._backend()
            value = keyring.get_password(self._service, key)  # type: ignore[attr-defined]
        except KeyringUnavailableError as exc:
            logger.warning("Secret %r unavailable: %s", key, exc)
            value = None
        except Exception:  # pragma: no cover - backend specific
            logger.exception("Unexpected keyring failure reading %r", key)
            value = None

        self._cache[key] = value
        return value

    def set(self, key: str, value: str) -> None:
        """Store a secret in the OS keyring.

        Args:
            key: One of the :class:`SecretRef` constants.
            value: Plaintext secret. Passing an empty string deletes the entry.

        Raises:
            KeyringUnavailableError: If there is no usable backend, so the GUI
                can tell the user their password was *not* saved rather than
                silently dropping it.
        """
        if not value:
            self.delete(key)
            return

        keyring = self._backend()
        keyring.set_password(self._service, key, value)  # type: ignore[attr-defined]
        self._cache[key] = value
        logger.info("Stored secret %r in the system keyring", key)

    def delete(self, key: str) -> None:
        """Remove a secret from the keyring if present.

        Args:
            key: One of the :class:`SecretRef` constants.
        """
        self._cache.pop(key, None)
        try:
            keyring = self._backend()
            keyring.delete_password(self._service, key)  # type: ignore[attr-defined]
        except KeyringUnavailableError as exc:
            logger.warning("Cannot delete secret %r: %s", key, exc)
        except Exception:
            logger.debug("Secret %r was not present in the keyring", key)

    def is_available(self) -> bool:
        """Report whether the OS keyring can be used.

        Returns:
            ``True`` if secrets can be persisted; ``False`` means the user must
            rely on ``WM_*`` environment variables.
        """
        try:
            self._backend()
        except KeyringUnavailableError:
            return False
        return True
