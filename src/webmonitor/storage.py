"""SQLite persistence for targets, snapshots and change events.

Design notes
------------
* **WAL journalling** lets the GUI read history while the monitoring thread
  writes results, without either blocking the other.
* **Snapshot bodies are zlib-compressed BLOBs.** Watched page fragments are
  markup, which compresses roughly 4-8x, and we keep a rolling history per
  target.
* **The API is synchronous.** The async engine calls it through
  :func:`asyncio.to_thread`, which keeps this module trivially testable and
  avoids pulling in an async SQLite driver for what is a sub-millisecond query.
* A single connection is shared behind a re-entrant lock with
  ``check_same_thread=False``; SQLite serialises writes internally and our write
  volume is a handful of rows per check.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import zlib
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from webmonitor import paths
from webmonitor.models import ChangeEvent, Normalization, RenderMode, Snapshot, Target

__all__ = ["Storage"]

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    id               TEXT PRIMARY KEY,
    name             TEXT    NOT NULL,
    url              TEXT    NOT NULL,
    selector         TEXT    NOT NULL DEFAULT '',
    enabled          INTEGER NOT NULL DEFAULT 1,
    render_mode      TEXT    NOT NULL DEFAULT 'auto',
    interval_min     INTEGER NOT NULL DEFAULT 300,
    interval_max     INTEGER NOT NULL DEFAULT 3600,
    interval_current INTEGER NOT NULL DEFAULT 300,
    normalization    TEXT    NOT NULL DEFAULT '{}',
    channels         TEXT    NOT NULL DEFAULT '[]',
    etag             TEXT,
    last_modified    TEXT,
    content_hash     TEXT,
    last_checked     TEXT,
    last_changed     TEXT,
    failure_count    INTEGER NOT NULL DEFAULT 0,
    proxy            TEXT,
    created_at       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id    TEXT    NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    content_hash TEXT    NOT NULL,
    content      BLOB    NOT NULL,
    captured_at  TEXT    NOT NULL,
    byte_size    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id     TEXT    NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    detected_at   TEXT    NOT NULL,
    added_lines   INTEGER NOT NULL DEFAULT 0,
    removed_lines INTEGER NOT NULL DEFAULT 0,
    summary       TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_snapshots_target
    ON snapshots(target_id, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_target
    ON events(target_id, detected_at DESC);
"""


def _iso(value: datetime | None) -> str | None:
    """Serialise a datetime to ISO-8601 text.

    Args:
        value: The datetime, or ``None``.

    Returns:
        ISO-8601 string, or ``None`` when given ``None``.
    """
    return value.isoformat() if value else None


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string written by :func:`_iso`.

    Args:
        value: The stored text, or ``None``.

    Returns:
        The parsed datetime, or ``None`` if the value is empty or malformed.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        logger.warning("Discarding unparseable timestamp %r", value)
        return None


class Storage:
    """Repository over the SQLite database.

    Usable as a context manager, which closes the connection on exit.
    """

    def __init__(self, path: Path | None = None) -> None:
        """Open (and if necessary create) the database.

        Args:
            path: Database file. Defaults to
                :func:`webmonitor.paths.database_file`. Pass ``":memory:"`` as a
                :class:`~pathlib.Path` alternative in tests via
                :meth:`in_memory`.
        """
        self._path = path or paths.database_file()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self._path,
            check_same_thread=False,
            isolation_level=None,  # autocommit; we manage transactions explicitly
        )
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._migrate()
        logger.info("Storage ready at %s", self._path)

    @classmethod
    def in_memory(cls) -> Storage:
        """Create a throwaway in-memory database.

        Returns:
            A :class:`Storage` backed by ``:memory:``, for tests.
        """
        return cls(Path(":memory:"))

    def _configure(self) -> None:
        """Apply the pragmas that matter for our read/write mix."""
        with self._lock:
            # WAL: concurrent GUI reads during engine writes.
            # NORMAL: fsync at checkpoints only. A crash can lose the last
            # check's timing metadata, which we happily re-derive.
            self._conn.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA synchronous = NORMAL;
                PRAGMA foreign_keys = ON;
                PRAGMA temp_store = MEMORY;
                PRAGMA mmap_size = 268435456;
                PRAGMA cache_size = -16000;
                """
            )

    def _migrate(self) -> None:
        """Create tables and record the schema version."""
        with self._lock:
            self._conn.executescript(_SCHEMA)
            current = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if current < SCHEMA_VERSION:
                self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                logger.info("Schema migrated from version %s to %s", current, SCHEMA_VERSION)

    # ------------------------------------------------------------------
    # Targets
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_target(row: sqlite3.Row) -> Target:
        """Rehydrate a :class:`Target` from a database row.

        Args:
            row: A row from the ``targets`` table.

        Returns:
            The reconstructed target.
        """
        return Target(
            id=row["id"],
            name=row["name"],
            url=row["url"],
            selector=row["selector"],
            enabled=bool(row["enabled"]),
            render_mode=RenderMode(row["render_mode"]),
            interval_min=row["interval_min"],
            interval_max=row["interval_max"],
            interval_current=row["interval_current"],
            normalization=Normalization.from_dict(json.loads(row["normalization"] or "{}")),
            channels=tuple(json.loads(row["channels"] or "[]")),
            etag=row["etag"],
            last_modified=row["last_modified"],
            content_hash=row["content_hash"],
            last_checked=_parse_dt(row["last_checked"]),
            last_changed=_parse_dt(row["last_changed"]),
            failure_count=row["failure_count"],
            proxy=row["proxy"],
            created_at=_parse_dt(row["created_at"]) or datetime.now(),
        )

    def upsert_target(self, target: Target) -> None:
        """Insert or update a target.

        Args:
            target: The target to persist. Matched on :attr:`Target.id`.
        """
        params: dict[str, Any] = {
            "id": target.id,
            "name": target.name,
            "url": target.url,
            "selector": target.selector,
            "enabled": int(target.enabled),
            "render_mode": target.render_mode.value,
            "interval_min": target.interval_min,
            "interval_max": target.interval_max,
            "interval_current": target.interval_current,
            "normalization": json.dumps(target.normalization.to_dict()),
            "channels": json.dumps(list(target.channels)),
            "etag": target.etag,
            "last_modified": target.last_modified,
            "content_hash": target.content_hash,
            "last_checked": _iso(target.last_checked),
            "last_changed": _iso(target.last_changed),
            "failure_count": target.failure_count,
            "proxy": target.proxy,
            "created_at": _iso(target.created_at),
        }
        columns = ", ".join(params)
        placeholders = ", ".join(f":{key}" for key in params)
        updates = ", ".join(f"{key}=excluded.{key}" for key in params if key != "id")

        with self._lock:
            self._conn.execute(
                f"INSERT INTO targets ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                params,
            )

    def get_target(self, target_id: str) -> Target | None:
        """Fetch one target by id.

        Args:
            target_id: The target's id.

        Returns:
            The target, or ``None`` if no such row exists.
        """
        with self._lock:
            row = self._conn.execute("SELECT * FROM targets WHERE id = ?", (target_id,)).fetchone()
        return self._row_to_target(row) if row else None

    def list_targets(self, *, enabled_only: bool = False) -> list[Target]:
        """Fetch all targets.

        Args:
            enabled_only: Restrict to targets with ``enabled = 1``.

        Returns:
            Targets ordered by creation time.
        """
        query = "SELECT * FROM targets"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY created_at ASC"

        with self._lock:
            rows = self._conn.execute(query).fetchall()
        return [self._row_to_target(row) for row in rows]

    def delete_target(self, target_id: str) -> None:
        """Delete a target and, by cascade, its snapshots and events.

        Args:
            target_id: The target's id.
        """
        with self._lock:
            self._conn.execute("DELETE FROM targets WHERE id = ?", (target_id,))
        logger.info("Deleted target %s", target_id)

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------
    def add_snapshot(self, snapshot: Snapshot, *, keep: int = 20) -> None:
        """Store a snapshot and prune the target's history.

        Args:
            snapshot: The capture to store. Its content is zlib-compressed.
            keep: How many of the most recent snapshots to retain.
        """
        blob = zlib.compress(snapshot.content.encode("utf-8"), level=6)
        with self._lock:
            self._conn.execute(
                "INSERT INTO snapshots (target_id, content_hash, content, captured_at, byte_size) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    snapshot.target_id,
                    snapshot.content_hash,
                    blob,
                    _iso(snapshot.captured_at),
                    snapshot.byte_size,
                ),
            )
            # Keep the newest `keep` rows; delete the rest in one statement.
            self._conn.execute(
                "DELETE FROM snapshots WHERE target_id = ? AND id NOT IN "
                "(SELECT id FROM snapshots WHERE target_id = ? "
                " ORDER BY captured_at DESC, id DESC LIMIT ?)",
                (snapshot.target_id, snapshot.target_id, keep),
            )

    def latest_snapshot(self, target_id: str) -> Snapshot | None:
        """Fetch the most recent snapshot for a target.

        Args:
            target_id: The target's id.

        Returns:
            The newest snapshot with its content decompressed, or ``None``.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM snapshots WHERE target_id = ? "
                "ORDER BY captured_at DESC, id DESC LIMIT 1",
                (target_id,),
            ).fetchone()
        if not row:
            return None
        return Snapshot(
            target_id=row["target_id"],
            content_hash=row["content_hash"],
            content=zlib.decompress(row["content"]).decode("utf-8"),
            captured_at=_parse_dt(row["captured_at"]) or datetime.now(),
            byte_size=row["byte_size"],
        )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    def add_event(self, event: ChangeEvent) -> None:
        """Record a detected change.

        Args:
            event: The change to record.
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (target_id, detected_at, added_lines, removed_lines, summary) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    event.target.id,
                    _iso(event.detected_at),
                    event.added_lines,
                    event.removed_lines,
                    event.summary,
                ),
            )

    def recent_events(self, limit: int = 100, target_id: str | None = None) -> list[dict[str, Any]]:
        """Fetch recent change events, newest first.

        Args:
            limit: Maximum number of rows.
            target_id: Restrict to a single target when given.

        Returns:
            A list of dictionaries with the event columns plus ``target_name``.
        """
        query = (
            "SELECT e.*, t.name AS target_name, t.url AS target_url "
            "FROM events e JOIN targets t ON t.id = e.target_id"
        )
        params: list[Any] = []
        if target_id:
            query += " WHERE e.target_id = ?"
            params.append(target_id)
        query += " ORDER BY e.detected_at DESC LIMIT ?"
        params.append(limit)

        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def vacuum(self) -> None:
        """Reclaim space after bulk deletions."""
        with self._lock:
            self._conn.execute("VACUUM")

    def close(self) -> None:
        """Checkpoint the WAL and close the connection."""
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                logger.debug("WAL checkpoint on close failed", exc_info=True)
            self._conn.close()
        logger.info("Storage closed")

    def __enter__(self) -> Storage:
        """Enter the context manager.

        Returns:
            This storage instance.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the database on context exit.

        Args:
            exc_type: Exception class, if one is propagating.
            exc: Exception instance, if one is propagating.
            tb: Traceback, if one is propagating.
        """
        self.close()
