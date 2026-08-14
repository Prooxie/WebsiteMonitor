"""Tests for the SQLite repository."""

from __future__ import annotations

from webmonitor.models import ChangeEvent, Normalization, RenderMode, Snapshot, Target, utc_now
from webmonitor.storage import Storage


def test_target_round_trips_every_field(storage: Storage) -> None:
    """A stored target comes back with all its fields intact."""
    original = Target(
        name="Shop",
        url="https://shop.example/new",
        selector="ul.items > li",
        render_mode=RenderMode.BROWSER,
        interval_min=120,
        interval_max=900,
        normalization=Normalization(ignore_numbers=True, ignore_patterns=("noise-\\d+",)),
        channels=("email", "telegram"),
        etag='W/"abc"',
        last_modified="Wed, 21 Oct 2015 07:28:00 GMT",
        content_hash="deadbeef",
        proxy="http://proxy:3128",
    )

    storage.upsert_target(original)
    loaded = storage.get_target(original.id)

    assert loaded is not None
    assert loaded.name == original.name
    assert loaded.selector == original.selector
    assert loaded.render_mode is RenderMode.BROWSER
    assert loaded.channels == ("email", "telegram")
    assert loaded.normalization.ignore_numbers is True
    assert loaded.normalization.ignore_patterns == ("noise-\\d+",)
    assert loaded.etag == 'W/"abc"'
    assert loaded.proxy == "http://proxy:3128"


def test_upsert_updates_rather_than_duplicates(storage: Storage, target: Target) -> None:
    """Saving the same id twice updates the existing row."""
    storage.upsert_target(target)
    storage.upsert_target(target.with_updates(name="Renamed"))

    targets = storage.list_targets()

    assert len(targets) == 1
    assert targets[0].name == "Renamed"


def test_list_targets_can_filter_to_enabled(storage: Storage) -> None:
    """``enabled_only`` is what the scheduler uses to skip paused targets."""
    storage.upsert_target(Target(name="On", url="https://a.example"))
    storage.upsert_target(Target(name="Off", url="https://b.example", enabled=False))

    assert len(storage.list_targets()) == 2
    assert len(storage.list_targets(enabled_only=True)) == 1


def test_missing_target_returns_none(storage: Storage) -> None:
    """Looking up an unknown id yields ``None`` rather than raising."""
    assert storage.get_target("does-not-exist") is None


def test_snapshot_content_survives_compression(storage: Storage, target: Target) -> None:
    """Snapshot bodies are zlib-compressed on write and restored on read."""
    storage.upsert_target(target)
    content = "Product list\n" + ("Widget 19.99\n" * 200)

    storage.add_snapshot(
        Snapshot(target_id=target.id, content_hash="hash-1", content=content, byte_size=4096)
    )
    loaded = storage.latest_snapshot(target.id)

    assert loaded is not None
    assert loaded.content == content
    assert loaded.content_hash == "hash-1"


def test_snapshot_history_is_pruned(storage: Storage, target: Target) -> None:
    """Only the newest ``keep`` snapshots are retained per target."""
    storage.upsert_target(target)

    for index in range(10):
        storage.add_snapshot(
            Snapshot(
                target_id=target.id,
                content_hash=f"hash-{index}",
                content=f"content {index}",
                captured_at=utc_now(),
            ),
            keep=3,
        )

    latest = storage.latest_snapshot(target.id)

    assert latest is not None
    assert latest.content == "content 9"


def test_latest_snapshot_of_unknown_target_is_none(storage: Storage) -> None:
    """A target with no captures yet has no snapshot."""
    assert storage.latest_snapshot("nope") is None


def test_events_are_listed_newest_first(storage: Storage, target: Target) -> None:
    """The Activity tab relies on this ordering."""
    storage.upsert_target(target)

    for index in range(3):
        storage.add_event(
            ChangeEvent(
                target=target,
                old_content="a",
                new_content="b",
                added_lines=index,
                removed_lines=0,
            )
        )

    events = storage.recent_events()

    assert len(events) == 3
    assert events[0]["target_name"] == target.name
    assert "target_url" in events[0]


def test_deleting_a_target_cascades(storage: Storage, target: Target) -> None:
    """Removing a target takes its snapshots and events with it."""
    storage.upsert_target(target)
    storage.add_snapshot(Snapshot(target_id=target.id, content_hash="h", content="c"))
    storage.add_event(ChangeEvent(target=target, old_content="a", new_content="b"))

    storage.delete_target(target.id)

    assert storage.get_target(target.id) is None
    assert storage.latest_snapshot(target.id) is None
    assert storage.recent_events() == []


def test_malformed_timestamp_does_not_break_loading(storage: Storage, target: Target) -> None:
    """A corrupt date column degrades to ``None`` instead of crashing startup."""
    storage.upsert_target(target)
    storage._conn.execute(
        "UPDATE targets SET last_checked = 'not-a-date' WHERE id = ?", (target.id,)
    )

    loaded = storage.get_target(target.id)

    assert loaded is not None
    assert loaded.last_checked is None
