import json
import os

from billsmusic.recently_played import (
    HISTORY_LIMIT,
    ListenTracker,
    RecentlyPlayedEntry,
    RecentlyPlayedRepository,
    add_entry,
    make_entry,
    qualification_threshold,
)


def _entry(path, played_at="2026-07-20T18:32:11Z"):
    return RecentlyPlayedEntry(
        path=path,
        title=os.path.basename(path),
        artist="Artist",
        album="Album",
        duration_seconds=240.0,
        played_at=played_at,
    )


def test_known_and_unknown_duration_thresholds():
    assert qualification_threshold(20.0) == 10.0
    assert qualification_threshold(240.0) == 30.0
    assert qualification_threshold(0.0) == 30.0


def test_track_is_not_recorded_immediately_and_active_time_qualifies():
    tracker = ListenTracker()
    tracker.reset("track.flac", 4, 4.0)
    assert not tracker.update(
        0.0, 0.0, active=True, paused=False, seeking=False, closing=False
    )
    assert not tracker.update(
        1.0, 1.0, active=True, paused=False, seeking=False, closing=False
    )
    assert tracker.update(
        2.0, 2.0, active=True, paused=False, seeking=False, closing=False
    )
    assert tracker.recorded


def test_pause_seek_and_non_advancing_time_do_not_accumulate():
    tracker = ListenTracker()
    tracker.reset("track.flac", 1, 60.0)
    tracker.update(
        0.0, 0.0, active=True, paused=False, seeking=False, closing=False
    )
    tracker.update(
        1.0, 1.0, active=True, paused=True, seeking=False, closing=False
    )
    tracker.update(
        2.0, 40.0, active=True, paused=False, seeking=True, closing=False
    )
    tracker.update(
        3.0, 40.0, active=True, paused=False, seeking=False, closing=False
    )
    assert tracker.listened_seconds == 0.0


def test_generation_records_at_most_once_and_recovery_preserves_total():
    tracker = ListenTracker()
    tracker.reset("track.flac", 8, 2.0)
    tracker.update(
        0.0, 0.0, active=True, paused=False, seeking=False, closing=False
    )
    assert tracker.update(
        1.0, 1.0, active=True, paused=False, seeking=False, closing=False
    )
    listened = tracker.listened_seconds
    assert not tracker.update(
        2.0, 2.0, active=True, paused=False, seeking=False, closing=False
    )
    assert tracker.listened_seconds == listened
    assert tracker.generation == 8


def test_consecutive_duplicates_update_but_nonconsecutive_replays_remain():
    first = _entry("one.flac", "2026-01-01T00:00:00Z")
    updated = _entry("one.flac", "2026-01-02T00:00:00Z")
    entries = add_entry([first], updated)
    assert len(entries) == 1
    assert entries[0].played_at == updated.played_at

    entries = add_entry(entries, _entry("two.flac"))
    entries = add_entry(entries, _entry("one.flac", "2026-01-03T00:00:00Z"))
    assert [entry.path for entry in entries] == [
        "one.flac", "two.flac", "one.flac"
    ]


def test_history_is_newest_first_and_bounded():
    entries = []
    for index in range(HISTORY_LIMIT + 20):
        entries = add_entry(entries, _entry(f"{index}.flac"))
    assert len(entries) == HISTORY_LIMIT
    assert entries[0].path == f"{HISTORY_LIMIT + 19}.flac"


def test_repository_round_trip_and_atomic_temp_cleanup(tmp_path):
    path = tmp_path / "recently_played.json"
    messages = []
    repository = RecentlyPlayedRepository(str(path), messages.append)
    repository.save([_entry("one.flac")])
    loaded = repository.load()
    assert [entry.path for entry in loaded] == ["one.flac"]
    assert not list(tmp_path.glob("*.tmp"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1


def test_malformed_and_unsupported_history_load_safely(tmp_path):
    path = tmp_path / "recently_played.json"
    repository = RecentlyPlayedRepository(str(path))
    path.write_text("{broken", encoding="utf-8")
    assert repository.load() == []
    path.write_text(
        json.dumps({"version": 99, "entries": []}), encoding="utf-8"
    )
    assert repository.load() == []


def test_invalid_entries_are_ignored(tmp_path):
    path = tmp_path / "recently_played.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {"bad": "entry"},
                    {
                        "path": "ok.flac", "title": "OK", "artist": "",
                        "album": "", "duration_seconds": 0, "played_at": "",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    loaded = RecentlyPlayedRepository(str(path)).load()
    assert [entry.path for entry in loaded] == ["ok.flac"]


def test_make_entry_uses_timezone_aware_utc_timestamp():
    entry = make_entry("track.flac", "Track", "Artist", "Album", 20)
    assert entry.played_at.endswith("Z")
