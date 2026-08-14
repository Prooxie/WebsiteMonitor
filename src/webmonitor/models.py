"""Domain models shared by every layer of the application.

These are deliberately plain dataclasses with no I/O and no Qt imports, so the
monitoring core stays unit-testable without a display or a database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

__all__ = [
    "ChangeEvent",
    "CheckResult",
    "CheckStatus",
    "Normalization",
    "RenderMode",
    "Snapshot",
    "Target",
    "utc_now",
]


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime.

    Returns:
        The current moment in UTC. Used everywhere instead of
        :meth:`datetime.utcnow`, which returns a naive datetime and is
        deprecated in modern Python.
    """
    return datetime.now(UTC)


class RenderMode(StrEnum):
    """How a target's HTML should be obtained.

    Attributes:
        STATIC: Plain HTTP GET. Fast and cheap - roughly 20ms and a few KB.
        BROWSER: Render in QtWebEngine so client-side JavaScript runs first.
            Costs ~1-3 seconds and considerably more memory.
        AUTO: Try ``STATIC`` first and transparently promote to ``BROWSER`` when
            the selector matches nothing, which is the usual signature of a
            client-rendered page.
    """

    STATIC = "static"
    BROWSER = "browser"
    AUTO = "auto"


class CheckStatus(StrEnum):
    """Outcome of a single check of one target.

    Attributes:
        UNCHANGED: Content fetched and its hash matched the stored one.
        CHANGED: Content fetched and its hash differed from the stored one.
        NOT_MODIFIED: Server answered ``304 Not Modified``; no body transferred.
        SELECTOR_MISS: Page loaded but the CSS selector matched no elements.
        ERROR: Network, HTTP or parsing failure. See ``CheckResult.error``.
    """

    UNCHANGED = "unchanged"
    CHANGED = "changed"
    NOT_MODIFIED = "not_modified"
    SELECTOR_MISS = "selector_miss"
    ERROR = "error"

    @property
    def is_quiet(self) -> bool:
        """Whether this outcome means "nothing to tell the user about".

        Returns:
            ``True`` for outcomes that represent a successful check with no
            change, which is what the adaptive scheduler backs off on.
        """
        return self in (CheckStatus.UNCHANGED, CheckStatus.NOT_MODIFIED)


@dataclass(slots=True, frozen=True)
class Normalization:
    """Rules applied to extracted content before it is hashed.

    Normalization is the single biggest lever on false-positive rate. Most
    "changes" on a real page are rotating ad ids, CSRF tokens, view counters and
    reformatted whitespace - not the content the user cares about.

    Attributes:
        text_only: Compare rendered text and discard markup entirely. Immune to
            class-name churn from CSS frameworks.
        strip_scripts: Remove ``<script>``, ``<style>``, ``<noscript>`` and
            HTML comments before comparing.
        collapse_whitespace: Collapse every run of whitespace to a single space
            and strip the ends.
        ignore_numbers: Replace digit runs with ``#``. Neutralises counters,
            prices and timestamps.
        ignore_attributes: Drop all HTML attributes, keeping only tag structure
            and text. Ignored when ``text_only`` is set.
        ignore_patterns: Regular expressions removed from the content before
            hashing, for site-specific noise.
    """

    text_only: bool = True
    strip_scripts: bool = True
    collapse_whitespace: bool = True
    ignore_numbers: bool = False
    ignore_attributes: bool = False
    ignore_patterns: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dictionary.

        Returns:
            A dictionary suitable for :func:`json.dumps`, with the pattern tuple
            flattened to a list.
        """
        return {
            "text_only": self.text_only,
            "strip_scripts": self.strip_scripts,
            "collapse_whitespace": self.collapse_whitespace,
            "ignore_numbers": self.ignore_numbers,
            "ignore_attributes": self.ignore_attributes,
            "ignore_patterns": list(self.ignore_patterns),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Normalization:
        """Rebuild a :class:`Normalization` from stored JSON.

        Args:
            data: Previously serialised mapping, or ``None`` for defaults.

        Returns:
            A normalization instance; unknown keys are ignored so that configs
            written by newer versions still load.
        """
        if not data:
            return cls()
        return cls(
            text_only=bool(data.get("text_only", True)),
            strip_scripts=bool(data.get("strip_scripts", True)),
            collapse_whitespace=bool(data.get("collapse_whitespace", True)),
            ignore_numbers=bool(data.get("ignore_numbers", False)),
            ignore_attributes=bool(data.get("ignore_attributes", False)),
            ignore_patterns=tuple(data.get("ignore_patterns", ()) or ()),
        )


@dataclass(slots=True)
class Target:
    """A single watched element on a single page.

    Attributes:
        id: Stable identifier, generated once and reused as the database key.
        name: Human-readable label shown in the GUI.
        url: Absolute URL to fetch.
        selector: CSS selector produced by the element inspector. An empty
            string means "the whole document".
        enabled: Whether the scheduler should check this target.
        render_mode: See :class:`RenderMode`.
        interval_min: Floor for the adaptive poll interval, in seconds.
        interval_max: Ceiling for the adaptive poll interval, in seconds.
        interval_current: The interval actually in force right now. Grows while
            the page is quiet and snaps back to ``interval_min`` on change.
        normalization: Pre-hash cleanup rules.
        channels: Names of notifier channels to fire on change. Empty means
            "every enabled channel".
        etag: Last ``ETag`` response header, replayed as ``If-None-Match``.
        last_modified: Last ``Last-Modified`` header, replayed as
            ``If-Modified-Since``.
        content_hash: BLAKE2b digest of the last accepted content.
        last_checked: When this target was last polled.
        last_changed: When a change was last detected.
        failure_count: Consecutive failures, used for error backoff.
        proxy: Optional per-target proxy URL, overriding the global setting.
        created_at: Creation timestamp.
    """

    name: str
    url: str
    selector: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    enabled: bool = True
    render_mode: RenderMode = RenderMode.AUTO
    interval_min: int = 300
    interval_max: int = 3600
    interval_current: int = 300
    normalization: Normalization = field(default_factory=Normalization)
    channels: tuple[str, ...] = ()
    etag: str | None = None
    last_modified: str | None = None
    content_hash: str | None = None
    last_checked: datetime | None = None
    last_changed: datetime | None = None
    failure_count: int = 0
    proxy: str | None = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        """Clamp interval fields so a bad config cannot wedge the scheduler.

        Raises:
            ValueError: If ``url`` is empty or ``interval_min`` is not positive.
        """
        if not self.url:
            raise ValueError("Target.url must not be empty")
        if self.interval_min <= 0:
            raise ValueError("Target.interval_min must be positive")
        if self.interval_max < self.interval_min:
            self.interval_max = self.interval_min
        self.interval_current = max(
            self.interval_min, min(self.interval_current, self.interval_max)
        )

    @property
    def is_first_run(self) -> bool:
        """Whether this target has never produced a baseline.

        Returns:
            ``True`` when no content hash is stored yet, meaning the next check
            establishes the baseline and must not raise a change alert.
        """
        return self.content_hash is None

    def next_due(self) -> datetime | None:
        """Compute when this target should next be checked.

        Returns:
            The due time, or ``None`` if the target has never been checked and
            is therefore due immediately.
        """
        if self.last_checked is None:
            return None
        from datetime import timedelta

        return self.last_checked + timedelta(seconds=self.interval_current)

    def with_updates(self, **changes: Any) -> Target:
        """Return a copy with the given fields replaced.

        Args:
            **changes: Field names and their new values.

        Returns:
            A new :class:`Target`; the original is left untouched so that GUI
            and worker threads never mutate a shared object.
        """
        return replace(self, **changes)


@dataclass(slots=True, frozen=True)
class Snapshot:
    """A stored capture of a target's normalized content.

    Attributes:
        target_id: Owning target's id.
        content_hash: BLAKE2b digest of ``content``.
        content: The normalized content this hash was computed from.
        captured_at: When the capture was taken.
        byte_size: Length of the raw response body, for statistics.
    """

    target_id: str
    content_hash: str
    content: str
    captured_at: datetime = field(default_factory=utc_now)
    byte_size: int = 0


@dataclass(slots=True, frozen=True)
class CheckResult:
    """What one poll of one target produced.

    Attributes:
        target_id: The target that was checked.
        status: Outcome classification.
        content: Normalized content, when a body was actually fetched.
        content_hash: Digest of ``content``.
        etag: Fresh ``ETag`` header, if the server sent one.
        last_modified: Fresh ``Last-Modified`` header, if the server sent one.
        error: Human-readable failure description when ``status`` is
            :attr:`CheckStatus.ERROR`.
        elapsed_ms: Wall-clock duration of the check.
        bytes_transferred: Body bytes actually read. Zero on a 304, which is the
            whole point of conditional requests.
        rendered: Whether QtWebEngine was used instead of a plain HTTP GET.
    """

    target_id: str
    status: CheckStatus
    content: str | None = None
    content_hash: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None
    elapsed_ms: float = 0.0
    bytes_transferred: int = 0
    rendered: bool = False


@dataclass(slots=True, frozen=True)
class ChangeEvent:
    """A confirmed change, ready to be handed to the notifiers.

    Attributes:
        target: The target whose content changed.
        old_content: Previous normalized content.
        new_content: Freshly fetched normalized content.
        detected_at: When the change was observed.
        added_lines: Count of lines present only in the new content.
        removed_lines: Count of lines present only in the old content.
    """

    target: Target
    old_content: str
    new_content: str
    detected_at: datetime = field(default_factory=utc_now)
    added_lines: int = 0
    removed_lines: int = 0

    @property
    def summary(self) -> str:
        """A one-line description suitable for a toast or SMS.

        Returns:
            Text such as ``"Shop page: +3 / -1 lines"``.
        """
        return f"{self.target.name}: +{self.added_lines} / -{self.removed_lines} lines"
