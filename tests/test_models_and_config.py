"""Tests for domain models, settings layering and the secret store."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from webmonitor.config import AppSettings, load_settings
from webmonitor.models import CheckStatus, Normalization, RenderMode, Target, utc_now
from webmonitor.secrets_store import SecretRef, SecretStore

if TYPE_CHECKING:
    from pathlib import Path

    from _pytest.monkeypatch import MonkeyPatch


# --------------------------------------------------------------------- models
def test_target_requires_a_url() -> None:
    """An empty URL is rejected at construction."""
    with pytest.raises(ValueError, match="url"):
        Target(name="Bad", url="")


def test_target_rejects_non_positive_interval() -> None:
    """A zero interval would spin the scheduler."""
    with pytest.raises(ValueError, match="interval_min"):
        Target(name="Bad", url="https://example.com", interval_min=0)


def test_target_clamps_an_inverted_range() -> None:
    """A max below min is corrected rather than left to misbehave."""
    target = Target(name="T", url="https://example.com", interval_min=600, interval_max=60)

    assert target.interval_max == 600
    assert target.interval_current == 600


def test_target_ids_are_unique() -> None:
    """Each target gets its own database key."""
    first = Target(name="A", url="https://a.example")
    second = Target(name="B", url="https://b.example")

    assert first.id != second.id


def test_with_updates_does_not_mutate_the_original() -> None:
    """GUI and worker threads share targets, so copies must be real copies."""
    original = Target(name="Original", url="https://example.com")

    updated = original.with_updates(name="Changed")

    assert original.name == "Original"
    assert updated.name == "Changed"
    assert updated.id == original.id


def test_is_first_run_tracks_the_baseline() -> None:
    """A target only stops being "new" once it has a content hash."""
    target = Target(name="T", url="https://example.com")

    assert target.is_first_run is True
    assert target.with_updates(content_hash="abc").is_first_run is False


def test_quiet_statuses_are_classified() -> None:
    """The scheduler backs off on quiet outcomes only."""
    assert CheckStatus.UNCHANGED.is_quiet is True
    assert CheckStatus.NOT_MODIFIED.is_quiet is True
    assert CheckStatus.CHANGED.is_quiet is False
    assert CheckStatus.ERROR.is_quiet is False


def test_next_due_is_immediate_before_the_first_check() -> None:
    """A never-checked target is due at once."""
    target = Target(name="T", url="https://example.com")

    assert target.next_due() is None
    assert target.with_updates(last_checked=utc_now()).next_due() is not None


def test_normalization_round_trips() -> None:
    """Rules survive the JSON round trip used by the database."""
    rules = Normalization(text_only=False, ignore_numbers=True, ignore_patterns=("a\\d+", "b\\d+"))

    restored = Normalization.from_dict(rules.to_dict())

    assert restored == rules


def test_normalization_from_empty_dict_uses_defaults() -> None:
    """A missing column loads as defaults rather than failing."""
    assert Normalization.from_dict(None) == Normalization()
    assert Normalization.from_dict({}) == Normalization()


def test_normalization_ignores_unknown_keys() -> None:
    """A config written by a newer version still loads."""
    restored = Normalization.from_dict({"text_only": False, "future_option": True})

    assert restored.text_only is False


def test_render_mode_serialises_as_its_value() -> None:
    """``StrEnum`` members store as plain strings in SQLite."""
    assert RenderMode.AUTO == "auto"
    assert RenderMode("browser") is RenderMode.BROWSER


# ------------------------------------------------------------------- settings
def test_defaults_are_valid() -> None:
    """A fresh install works with no configuration at all."""
    settings = AppSettings()

    assert settings.network.timeout > 0
    assert settings.scheduler.max_concurrent_checks >= 1
    assert settings.smtp.enabled is False


def test_settings_round_trip_through_disk(tmp_path: Path) -> None:
    """Saving then loading preserves values."""
    path = tmp_path / "settings.json"
    settings = AppSettings()
    settings.smtp.host = "smtp.example.com"
    settings.smtp.recipients = ["a@example.com"]
    settings.scheduler.max_concurrent_checks = 12

    settings.save(path)
    loaded = load_settings(path)

    assert loaded.smtp.host == "smtp.example.com"
    assert loaded.smtp.recipients == ["a@example.com"]
    assert loaded.scheduler.max_concurrent_checks == 12


def test_environment_overrides_the_file(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """A deliberate env override must beat a stale settings.json."""
    path = tmp_path / "settings.json"
    settings = AppSettings()
    settings.smtp.host = "from-file.example.com"
    settings.save(path)

    monkeypatch.setenv("WM_SMTP__HOST", "from-env.example.com")
    loaded = load_settings(path)

    assert loaded.smtp.host == "from-env.example.com"


def test_corrupt_settings_file_falls_back_to_defaults(tmp_path: Path) -> None:
    """A broken config must not prevent the app from starting."""
    path = tmp_path / "settings.json"
    path.write_text("{ this is not json", encoding="utf-8")

    loaded = load_settings(path)

    assert loaded.network.timeout == AppSettings().network.timeout


def test_missing_settings_file_uses_defaults(tmp_path: Path) -> None:
    """First run has no file yet."""
    assert load_settings(tmp_path / "absent.json").theme == "dark"


def test_recipients_accept_a_comma_separated_string() -> None:
    """Environment variables can only carry strings."""
    settings = AppSettings(smtp={"recipients": "a@x.com, b@x.com"})

    assert settings.smtp.recipients == ["a@x.com", "b@x.com"]


def test_smtp_is_configured_needs_every_field() -> None:
    """Partial configuration must not be reported as ready."""
    assert AppSettings().smtp.is_configured is False

    complete = AppSettings(
        smtp={
            "enabled": True,
            "host": "smtp.example.com",
            "sender": "me@example.com",
            "recipients": ["you@example.com"],
        }
    )
    assert complete.smtp.is_configured is True


def test_invalid_port_is_rejected() -> None:
    """Pydantic validation guards the settings file."""
    with pytest.raises(ValueError, match="port"):
        AppSettings(smtp={"port": 99999})


def test_settings_save_is_atomic(tmp_path: Path) -> None:
    """The temp file used during save must not be left behind."""
    path = tmp_path / "settings.json"

    AppSettings().save(path)

    assert path.exists()
    assert not (tmp_path / "settings.json.tmp").exists()


# -------------------------------------------------------------------- secrets
def test_environment_variable_wins_over_the_keyring(monkeypatch: MonkeyPatch) -> None:
    """Headless and CI runs inject credentials this way."""
    monkeypatch.setenv("WM_SMTP_PASSWORD", "from-env")

    assert SecretStore().get(SecretRef.SMTP_PASSWORD) == "from-env"


def test_missing_secret_returns_none() -> None:
    """An unset secret is ``None``, never an empty string."""
    store = SecretStore(service="WebsiteMonitor-test-nonexistent")

    assert store.get("definitely_not_set_anywhere") is None


def test_telegram_token_reads_from_environment(monkeypatch: MonkeyPatch) -> None:
    """Both well-known secrets support the same override mechanism."""
    monkeypatch.setenv("WM_TELEGRAM_TOKEN", "123:ABC")

    assert SecretStore().get(SecretRef.TELEGRAM_TOKEN) == "123:ABC"
