"""Tests for hashing and diff rendering."""

from __future__ import annotations

from webmonitor.core.differ import (
    build_change_event,
    compute_hash,
    render_diff_html,
    render_diff_text,
)
from webmonitor.models import Target


def test_hash_is_stable_and_sensitive() -> None:
    """Identical input hashes alike; a one-character change does not."""
    assert compute_hash("hello") == compute_hash("hello")
    assert compute_hash("hello") != compute_hash("hellp")


def test_hash_is_fixed_width_hex() -> None:
    """A 32-byte BLAKE2b digest renders as 64 hex characters."""
    digest = compute_hash("x" * 100_000)

    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_hash_handles_unicode() -> None:
    """Non-ASCII content must not raise on encoding."""
    assert compute_hash("naïve café 日本語 🎉")


def test_change_event_counts_added_and_removed(target: Target) -> None:
    """Line counts drive the notification headline."""
    event = build_change_event(target, "line one\nline two", "line one\nline two\nline three")

    assert event.added_lines == 1
    assert event.removed_lines == 0


def test_change_event_counts_replacements(target: Target) -> None:
    """A replaced line counts as both an addition and a removal."""
    event = build_change_event(target, "alpha\nbeta", "alpha\ngamma")

    assert event.added_lines == 1
    assert event.removed_lines == 1


def test_summary_is_human_readable(target: Target) -> None:
    """The summary is what a toast or SMS shows."""
    event = build_change_event(target, "a", "b")

    assert target.name in event.summary
    assert "+" in event.summary


def test_diff_text_marks_both_sides(target: Target) -> None:
    """A unified diff shows the removed and added lines."""
    event = build_change_event(target, "old line", "new line")

    diff = render_diff_text(event)

    assert "-old line" in diff
    assert "+new line" in diff


def test_diff_text_truncates(target: Target) -> None:
    """Huge diffs are cut so an email stays sendable."""
    old = "\n".join(f"line {i}" for i in range(500))
    new = "\n".join(f"changed {i}" for i in range(500))
    event = build_change_event(target, old, new)

    diff = render_diff_text(event, max_lines=20)

    assert "omitted" in diff
    assert len(diff.splitlines()) <= 21


def test_diff_html_escapes_markup(target: Target) -> None:
    """Page content must not be able to inject HTML into the email body."""
    event = build_change_event(target, "safe", '<script>alert("xss")</script>')

    html = render_diff_html(event)

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_diff_html_is_a_table(target: Target) -> None:
    """The email diff renders as an inline-styled table."""
    event = build_change_event(target, "before", "after")

    html = render_diff_html(event)

    assert html.startswith("<table")
    assert "before" in html
    assert "after" in html


def test_identical_content_reports_no_difference(target: Target) -> None:
    """Diffing equal strings produces a placeholder, not an empty string."""
    event = build_change_event(target, "same", "same")

    assert "no textual difference" in render_diff_text(event)


def test_long_single_line_is_chunked(target: Target) -> None:
    """Whitespace-collapsed content is one long line; chunking keeps diffs useful."""
    old = " ".join(f"word{i}" for i in range(200))
    new = old.replace("word50", "CHANGED")
    event = build_change_event(target, old, new)

    diff = render_diff_text(event)

    assert "CHANGED" in diff
    # Chunking means we report a small edit, not a wholesale replacement.
    assert event.added_lines < 5
