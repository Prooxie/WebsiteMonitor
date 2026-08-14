"""Headless monitoring core: fetch, extract, diff, schedule.

Nothing in this package imports Qt. That boundary is what lets the engine run in
a background thread, be unit-tested without a display, and later be reused by a
console-only build.
"""

from __future__ import annotations

__all__: list[str] = []
