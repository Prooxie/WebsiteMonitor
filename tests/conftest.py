"""Shared pytest fixtures.

Every fixture here keeps state off the developer's real machine: the database is
in-memory and ``WM_DATA_DIR`` is redirected into ``tmp_path``, so running the
suite never touches the live application data directory under LOCALAPPDATA.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from webmonitor.config import AppSettings
from webmonitor.models import Normalization, Target
from webmonitor.storage import Storage

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from _pytest.monkeypatch import MonkeyPatch


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path: Path, monkeypatch: MonkeyPatch) -> Path:
    """Redirect application data to a temporary directory for every test.

    Args:
        tmp_path: Pytest's per-test temporary directory.
        monkeypatch: Environment patcher.

    Returns:
        The temporary data directory.
    """
    data_dir = tmp_path / "appdata"
    data_dir.mkdir()
    monkeypatch.setenv("WM_DATA_DIR", str(data_dir))
    return data_dir


@pytest.fixture(autouse=True)
def clean_secret_env(monkeypatch: MonkeyPatch) -> None:
    """Remove credential environment variables that would leak between tests.

    Args:
        monkeypatch: Environment patcher.
    """
    for name in ("WM_SMTP_PASSWORD", "WM_TELEGRAM_TOKEN", "WM_TWILIO_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def storage() -> Iterator[Storage]:
    """Provide an in-memory database.

    Yields:
        A :class:`~webmonitor.storage.Storage` that is closed on teardown.
    """
    store = Storage.in_memory()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def settings() -> AppSettings:
    """Provide settings tuned for fast, deterministic tests.

    Returns:
        An :class:`~webmonitor.config.AppSettings` with retries and jitter
        disabled so that timing assertions are stable.
    """
    config = AppSettings()
    config.network.max_retries = 0
    config.network.timeout = 5.0
    config.scheduler.jitter_ratio = 0.0
    config.scheduler.check_on_start = False
    return config


@pytest.fixture
def target() -> Target:
    """Provide a simple target for tests.

    Returns:
        A :class:`~webmonitor.models.Target` watching ``div.content``.
    """
    return Target(
        name="Example",
        url="https://example.com/page",
        selector="div.content",
        interval_min=60,
        interval_max=600,
        normalization=Normalization(),
    )


@pytest.fixture
def sample_html() -> str:
    """Provide a small HTML document exercising the extractor.

    Returns:
        An HTML document with a watched element, noise tags and numbers.
    """
    return """
    <html><head><title>Test</title>
      <style>.content { color: red; }</style>
    </head>
    <body>
      <div class="header">Ignore me</div>
      <div class="content">
        <h2>Product list</h2>
        <ul>
          <li>Widget - 19.99 EUR</li>
          <li>Gadget - 24.50 EUR</li>
        </ul>
        <script>track('view', 12345);</script>
        <!-- a comment -->
      </div>
      <div class="footer">Also ignore</div>
    </body></html>
    """
