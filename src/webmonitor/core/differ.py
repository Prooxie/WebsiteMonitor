"""Content hashing and human-readable diffs.

Change *detection* is a hash comparison, not a diff: BLAKE2b over the normalized
content is faster than SHA-256 on 64-bit CPUs and lets us store 32 bytes per
target instead of the whole document. Diffs are computed only after a change is
already known to exist, purely to describe it in the notification.
"""

from __future__ import annotations

import difflib
import hashlib
import html as html_escape
import logging
from typing import Final

from webmonitor.models import ChangeEvent, Target

__all__ = [
    "build_change_event",
    "compute_hash",
    "render_diff_html",
    "render_diff_text",
]

logger = logging.getLogger(__name__)

#: 32 bytes is ample collision resistance here and half the width of SHA-256.
_DIGEST_SIZE: Final[int] = 32

#: Diffs longer than this are truncated before going into an email.
_MAX_DIFF_LINES: Final[int] = 200


def compute_hash(content: str) -> str:
    """Hash normalized content.

    Args:
        content: The normalized content produced by the extractor.

    Returns:
        A 64-character hexadecimal BLAKE2b digest.
    """
    return hashlib.blake2b(content.encode("utf-8"), digest_size=_DIGEST_SIZE).hexdigest()


def _split(content: str) -> list[str]:
    """Split content into comparable lines.

    Whitespace-collapsed content is a single long line, which produces a useless
    diff. Splitting on sentence-ish boundaries would be guesswork, so we split on
    newlines and fall back to chunking very long single lines.

    Args:
        content: Normalized content.

    Returns:
        A list of lines suitable for :mod:`difflib`.
    """
    lines = content.splitlines()
    if len(lines) > 1:
        return lines

    single = lines[0] if lines else ""
    if len(single) <= 120:
        return [single] if single else []

    # Chunk on word boundaries so the diff highlights phrases, not characters.
    words = single.split(" ")
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for word in words:
        if length + len(word) + 1 > 100 and current:
            chunks.append(" ".join(current))
            current, length = [], 0
        current.append(word)
        length += len(word) + 1
    if current:
        chunks.append(" ".join(current))
    return chunks


def build_change_event(target: Target, old_content: str, new_content: str) -> ChangeEvent:
    """Assemble a :class:`~webmonitor.models.ChangeEvent` with diff statistics.

    Args:
        target: The target whose content changed.
        old_content: Previous normalized content.
        new_content: Freshly fetched normalized content.

    Returns:
        The populated change event.
    """
    old_lines = _split(old_content)
    new_lines = _split(new_content)

    added = removed = 0
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1

    return ChangeEvent(
        target=target,
        old_content=old_content,
        new_content=new_content,
        added_lines=added,
        removed_lines=removed,
    )


def render_diff_text(event: ChangeEvent, *, max_lines: int = _MAX_DIFF_LINES) -> str:
    """Render a unified diff as plain text.

    Args:
        event: The change to describe.
        max_lines: Truncate the diff beyond this many lines.

    Returns:
        A unified diff, or a placeholder when the contents are identical.
    """
    diff = list(
        difflib.unified_diff(
            _split(event.old_content),
            _split(event.new_content),
            fromfile="previous",
            tofile="current",
            lineterm="",
            n=2,
        )
    )
    if not diff:
        return "(no textual difference)"

    if len(diff) > max_lines:
        omitted = len(diff) - max_lines
        diff = diff[:max_lines]
        diff.append(f"... {omitted} more diff lines omitted ...")

    return "\n".join(diff)


def render_diff_html(event: ChangeEvent, *, max_lines: int = _MAX_DIFF_LINES) -> str:
    """Render a colour-coded diff as a self-contained HTML fragment.

    Styles are inlined because email clients strip ``<style>`` blocks.

    Args:
        event: The change to describe.
        max_lines: Truncate the diff beyond this many lines.

    Returns:
        An HTML fragment safe to embed in an email body.
    """
    rows: list[str] = []
    count = 0

    matcher = difflib.SequenceMatcher(
        None, _split(event.old_content), _split(event.new_content), autojunk=False
    )
    old_lines = _split(event.old_content)
    new_lines = _split(event.new_content)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if count >= max_lines:
            rows.append(
                '<tr><td style="padding:6px 10px;color:#888;font-style:italic">'
                "... diff truncated ...</td></tr>"
            )
            break

        if tag == "equal":
            continue

        for line in old_lines[i1:i2]:
            rows.append(_diff_row("-", line, "#3b1219", "#ff6b81"))
            count += 1
        for line in new_lines[j1:j2]:
            rows.append(_diff_row("+", line, "#0f2f1a", "#4ade80"))
            count += 1

    if not rows:
        rows.append('<tr><td style="padding:6px 10px;color:#888">(no textual difference)</td></tr>')

    return (
        '<table cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;'
        "font-family:Consolas,Menlo,monospace;font-size:13px;background:#16181d;"
        'border-radius:8px;overflow:hidden">' + "".join(rows) + "</table>"
    )


def _diff_row(marker: str, line: str, background: str, colour: str) -> str:
    """Build one row of the HTML diff table.

    Args:
        marker: ``"+"`` or ``"-"``.
        line: The line content, escaped by this function.
        background: CSS background colour for the row.
        colour: CSS text colour for the row.

    Returns:
        A ``<tr>`` element as a string.
    """
    escaped = html_escape.escape(line)
    return (
        f'<tr><td style="padding:4px 10px;background:{background};color:{colour};'
        f'white-space:pre-wrap;word-break:break-word">'
        f'<span style="opacity:.6">{marker}</span> {escaped}</td></tr>'
    )
