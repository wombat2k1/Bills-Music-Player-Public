from pathlib import Path
from types import SimpleNamespace

import pytest

import billsmusic.playlist_repair as repair
from billsmusic.performance_diagnostics import PerformanceDiagnostics
from billsmusic.playlist_repair import (
    PlaylistEntry,
    apply_replacements,
    load_m3u,
    save_m3u,
    suggest_replacement,
)
from billsmusic.window import PlayerWindow


def _write_playlist(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_valid_absolute_and_relative_paths_load_in_original_order(tmp_path):
    absolute = tmp_path / "absolute.mp3"
    relative = tmp_path / "relative.flac"
    absolute.touch()
    relative.touch()
    playlist = _write_playlist(
        tmp_path / "mix.m3u8",
        ["#EXTM3U", str(absolute), "relative.flac"],
    )

    entries, counts = load_m3u(str(playlist))

    assert [entry.resolved_path for entry in entries] == [
        str(absolute), str(relative)
    ]
    assert counts == {
        "valid": 2, "missing": 0, "relative_resolved": 1, "total": 2
    }


def test_missing_duplicate_and_extinf_metadata_are_preserved(tmp_path):
    playlist = _write_playlist(
        tmp_path / "missing.m3u",
        [
            "#EXTM3U",
            "#EXTINF:245,Artist Name - Tïtle",
            "gone.mp3",
            "gone.mp3",
        ],
    )

    entries, counts = load_m3u(str(playlist))

    assert len(entries) == 2
    assert all(entry.is_missing for entry in entries)
    assert entries[0].artist == "Artist Name"
    assert entries[0].display_title == "Tïtle"
    assert entries[0].duration_seconds == 245
    assert [entry.original_path for entry in entries] == [
        "gone.mp3", "gone.mp3"
    ]
    assert counts["missing"] == 2


def test_utf8_m3u8_filename_loads(tmp_path):
    track = tmp_path / "Beyoncé – Déjà Vu.flac"
    track.touch()
    playlist = _write_playlist(tmp_path / "utf8.m3u8", [track.name])
    entries, _ = load_m3u(str(playlist))
    assert entries[0].playable_path == str(track)


def test_playlist_registry_classification_does_not_filter_unsupported_rows(tmp_path):
    supported = tmp_path / "song.mp3"
    unsupported = tmp_path / "notes.txt"
    supported.touch()
    unsupported.touch()
    playlist = _write_playlist(tmp_path / "mixed.m3u", [supported.name, unsupported.name])

    entries, counts = load_m3u(str(playlist))

    assert len(entries) == 2
    assert counts["valid"] == 2
    assert entries[0].is_supported_media is True
    assert entries[1].is_supported_media is False


def test_filesystem_existence_is_checked_once_for_duplicate_paths(tmp_path):
    playlist = _write_playlist(tmp_path / "dupes.m3u", ["gone.mp3", "gone.mp3"])
    calls = []

    def exists(path):
        calls.append(path)
        return False

    entries, _ = load_m3u(str(playlist), exists=exists)
    assert len(entries) == 2
    assert len(calls) == 1


def _missing(path="Old Song.mp3", artist=None, title=None):
    return PlaylistEntry(
        path=path, original_path=path, artist=artist,
        display_title=title, is_missing=True,
    )


def test_exact_filename_and_artist_title_matching():
    records = [
        {"path": "D:/Music/Old Song.mp3", "artist": "Someone", "title": "Other"},
        {"path": "D:/Music/new.flac", "artist": "Artist", "title": "Title"},
    ]
    by_name = suggest_replacement(_missing(), records)
    by_tags = suggest_replacement(
        _missing("unknown.mp3", "Artist", "Title"), records
    )
    assert (by_name.confidence, by_name.preselected) == ("Exact", True)
    assert (by_tags.confidence, by_tags.preselected) == ("Exact", True)


def test_changed_extension_is_strong_and_preselected():
    suggestion = suggest_replacement(
        _missing("Song.mp3"),
        [{"path": "D:/Music/Song.flac", "title": "Song", "artist": ""}],
    )
    assert suggestion.confidence == "Strong"
    assert suggestion.preselected is True


def test_possible_and_ambiguous_matches_are_never_preselected():
    possible = suggest_replacement(
        _missing("unknown.mp3", "Wrong Artist", "Same Title"),
        [{"path": "one.flac", "artist": "Right Artist", "title": "Same Title"}],
    )
    ambiguous = suggest_replacement(
        _missing("Song.mp3"),
        [
            {"path": "A/Song.mp3", "title": "Song"},
            {"path": "B/Song.mp3", "title": "Song"},
        ],
    )
    assert possible.confidence == "Possible"
    assert possible.preselected is False
    assert ambiguous.path is None
    assert ambiguous.preselected is False


def test_manual_replacement_validation_and_selective_application(tmp_path):
    existing = tmp_path / "replacement.flac"
    existing.touch()
    entries = [_missing("one.mp3"), _missing("two.mp3")]
    with pytest.raises(ValueError):
        entries[0].with_replacement(str(tmp_path / "absent.mp3"))

    updated = apply_replacements(entries, {1: str(existing)})

    assert updated[0].is_missing
    assert updated[1].playable_path == str(existing)
    assert entries[1].is_missing


def test_repair_preserves_order_and_duplicate_entries(tmp_path):
    replacement = tmp_path / "song.flac"
    replacement.touch()
    first = _missing("same.mp3")
    entries = [first, _missing("middle.mp3"), first]

    updated = apply_replacements(entries, {0: str(replacement)})

    assert len(updated) == 3
    assert updated[0].playable_path == str(replacement)
    assert updated[1].original_path == "middle.mp3"
    assert updated[2].is_missing


def test_save_writes_repairs_preserves_missing_and_creates_bounded_backup(tmp_path):
    playlist = _write_playlist(tmp_path / "mix.m3u8", ["old contents"])
    replacement = tmp_path / "replacement.flac"
    replacement.touch()
    entries = [
        _missing("old.mp3").with_replacement(str(replacement)),
        _missing("still-gone.mp3"),
    ]

    save_m3u(str(playlist), entries)

    text = playlist.read_text(encoding="utf-8")
    assert str(replacement) in text
    assert "still-gone.mp3" in text
    assert (tmp_path / "mix.m3u8.bak").read_text(
        encoding="utf-8"
    ).strip() == "old contents"
    save_m3u(str(playlist), entries)
    assert len(list(tmp_path.glob("mix.m3u8.bak*"))) == 1


def test_backup_failure_prevents_overwrite(tmp_path, monkeypatch):
    playlist = _write_playlist(tmp_path / "mix.m3u", ["original"])
    monkeypatch.setattr(
        repair.shutil, "copy2",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("denied")),
    )
    with pytest.raises(OSError, match="backup"):
        save_m3u(str(playlist), [_missing("gone.mp3")])
    assert playlist.read_text(encoding="utf-8").strip() == "original"


def test_missing_path_is_rejected_before_any_audio_backend(tmp_path):
    calls = []

    class Diagnostics:
        def path_details(self, path):
            return {"path_hash": "safe"}

        def record(self, *args, **kwargs):
            calls.append(("diagnostic", args, kwargs))

    harness = SimpleNamespace(
        _audio_log=lambda message: calls.append(("log", message)),
        _audio_name=lambda path: Path(path).name,
        diagnostics=Diagnostics(),
        statusBar=lambda: SimpleNamespace(showMessage=lambda *args: None),
        _mixed_transition_state="idle",
        _next_playback_attempt_id=1,
        _current_playback_attempt=None,
    )
    harness._begin_playback_attempt = (
        lambda *a, **kw: PlayerWindow._begin_playback_attempt(harness, *a, **kw)
    )
    result = PlayerWindow._play_path_direct(
        harness, str(tmp_path / "missing.flac")
    )
    assert result is False
    assert not any(call[0] in {"vlc", "miniaudio", "bass"} for call in calls)


def test_diagnostics_redact_playlist_paths_by_default(tmp_path):
    diagnostics = PerformanceDiagnostics(
        level="basic", directory=str(tmp_path / "diagnostics")
    )
    details = diagnostics.path_details(r"Z:\Private Music\Artist\Song.flac")
    diagnostics.shutdown()
    assert "path" not in details
    assert "path_hash" in details
