import inspect

from billsmusic.library_cache import (
    LIBRARY_CACHE_SCHEMA_VERSION,
    audio_fingerprint_matches,
    dedupe_meta_list_by_path,
    lrc_fingerprint_matches,
    metadata_with_lrc_fingerprint,
    migrate_cache_meta_list,
    migrate_cache_record,
    stable_folder_signature,
)
from billsmusic.window import PlayerWindow


def fingerprint(size=100, mtime=200, lrc_size=None, lrc_mtime=None):
    return {
        "size": size,
        "mtime_ns": mtime,
        "lrc_size": lrc_size,
        "lrc_mtime_ns": lrc_mtime,
    }


def test_cache_schema_and_stable_signature_are_deterministic():
    assert LIBRARY_CACHE_SCHEMA_VERSION == 3
    first = {
        "Z:/Music/B.flac": fingerprint(2, 20),
        "Z:/Music/A.flac": fingerprint(1, 10, 5, 50),
    }
    second = dict(reversed(list(first.items())))
    assert stable_folder_signature("Z:/Music", first) == stable_folder_signature(
        "z:/music/", second
    )


def test_audio_and_lrc_changes_are_compared_independently():
    old = fingerprint(100, 200, 10, 300)
    assert audio_fingerprint_matches(old, fingerprint(100, 200, 99, 999))
    assert not audio_fingerprint_matches(old, fingerprint(101, 200, 10, 300))
    assert lrc_fingerprint_matches(old, fingerprint(999, 999, 10, 300))
    assert not lrc_fingerprint_matches(old, fingerprint(100, 200, None, None))


def test_lrc_only_change_reuses_metadata_and_updates_lyrics_flag():
    cached = {
        "path": "Z:/Music/Song.flac",
        "title": "Song",
        "has_lrc_sidecar": False,
        "has_embedded_synced_lyrics": False,
        "has_synced_lyrics": False,
    }
    updated = metadata_with_lrc_fingerprint(
        cached, fingerprint(100, 200, 10, 300)
    )
    assert updated["title"] == "Song"
    assert updated["has_lrc_sidecar"] is True
    assert updated["has_synced_lyrics"] is True
    assert cached["has_lrc_sidecar"] is False


def test_removing_lrc_preserves_embedded_synced_lyrics():
    cached = {
        "has_lrc_sidecar": True,
        "has_embedded_synced_lyrics": True,
        "has_synced_lyrics": True,
    }
    updated = metadata_with_lrc_fingerprint(
        cached, fingerprint(100, 200, None, None)
    )
    assert updated["has_lrc_sidecar"] is False
    assert updated["has_synced_lyrics"] is True


def test_dedupe_meta_list_by_path_drops_exact_duplicates():
    # Overlapping library folders can cause the same track to be scanned
    # twice; this is the regression for albums showing every track doubled.
    meta_list = [
        {"path": "Z:/Music/Bombshell Energy/01 Ur So Cool.mp3", "title": "Ur So Cool"},
        {"path": "Z:/Music/Bombshell Energy/01 Ur So Cool.mp3", "title": "Ur So Cool"},
        {"path": "Z:/Music/Bombshell Energy/02 Pass That Dutch.mp3", "title": "Pass That Dutch"},
    ]
    deduped = dedupe_meta_list_by_path(meta_list)
    assert [m["path"] for m in deduped] == [
        "Z:/Music/Bombshell Energy/01 Ur So Cool.mp3",
        "Z:/Music/Bombshell Energy/02 Pass That Dutch.mp3",
    ]


def test_dedupe_meta_list_by_path_ignores_case_and_slash_differences():
    meta_list = [
        {"path": "Z:/Music/Song.flac"},
        {"path": r"Z:\Music\SONG.flac"},
    ]
    deduped = dedupe_meta_list_by_path(meta_list)
    assert len(deduped) == 1


def test_dedupe_meta_list_by_path_keeps_records_with_no_path():
    # Malformed entries shouldn't be silently dropped; they just can't be
    # deduplicated by path so they pass through untouched.
    meta_list = [{"title": "No path here"}, {"title": "Also no path"}]
    assert dedupe_meta_list_by_path(meta_list) == meta_list


def test_dedupe_meta_list_by_path_preserves_order_and_unique_entries():
    meta_list = [
        {"path": "A.mp3"},
        {"path": "B.mp3"},
        {"path": "C.mp3"},
    ]
    assert dedupe_meta_list_by_path(meta_list) == meta_list


def test_scan_and_backfill_handlers_dedupe_before_saving_the_cache():
    # Both handlers must dedupe BEFORE _save_cache, or a corrupted cache
    # keeps reproducing the same duplicate rows on every future scan.
    for method in (PlayerWindow._on_scan_finished, PlayerWindow._on_backfill_finished):
        source = inspect.getsource(method)
        assert "dedupe_meta_list_by_path(meta_list)" in source
        dedupe_line = source.index("dedupe_meta_list_by_path(meta_list)")
        save_cache_line = source.index("self._save_cache(folders, meta_list)")
        assert dedupe_line < save_cache_line, method.__name__


def test_migrate_cache_record_infers_media_type_from_path():
    # A v2 record has no media_type at all; migration must infer it from
    # the path alone, without touching any other field or reading the file.
    v2_record = {"path": "Z:/Music/Song.mp3", "title": "Song"}
    migrated = migrate_cache_record(v2_record)
    assert migrated["media_type"] == "audio"
    assert migrated["title"] == "Song"
    # Original record is untouched -- migration returns a new dict.
    assert "media_type" not in v2_record


def test_migrate_cache_record_infers_video_from_path():
    migrated = migrate_cache_record({"path": "Z:/Videos/Clip.mp4"})
    assert migrated["media_type"] == "video"


def test_migrate_cache_record_leaves_existing_media_type_alone():
    # A record that already has media_type (current-schema) must not be
    # reclassified/overwritten, even if it looks inconsistent with the path.
    record = {"path": "Z:/Music/Song.mp3", "media_type": "video"}
    assert migrate_cache_record(record)["media_type"] == "video"


def test_migrate_cache_meta_list_backfills_only_missing_records():
    meta_list = [
        {"path": "Z:/Music/A.mp3"},
        {"path": "Z:/Videos/B.mp4", "media_type": "video"},
        {"path": "Z:/Music/C.flac"},
    ]
    migrated = migrate_cache_meta_list(meta_list)
    assert [m["media_type"] for m in migrated] == ["audio", "video", "audio"]


def test_startup_cache_restore_also_dedupes():
    # A library that was already fully scanned (and thus already had a
    # duplicate baked in) never runs _on_scan_finished/_on_backfill_finished
    # again on ordinary startup -- it loads the cache file directly (in this
    # staged startup method) rather than through a fresh scan. Without
    # deduping here too, the fix above never actually reaches a user who
    # isn't the one triggering a fresh scan/rescan.
    source = inspect.getsource(PlayerWindow._startup_restore_library)
    assert 'dedupe_meta_list_by_path(cache["meta"])' in source
