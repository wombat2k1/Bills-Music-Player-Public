import pytest

import billsmusic.workers as workers
from billsmusic.workers import LibraryScanThread


@pytest.fixture(autouse=True)
def _fake_folders_are_directories(monkeypatch):
    # run() gates each folder on os.path.isdir(folder) before calling the
    # window's _scan_folder -- our fake folder paths ("F1", "F2", ...)
    # aren't real directories, so tell it they are.
    monkeypatch.setattr(workers.os.path, "isdir", lambda path: True)


def _fingerprint(path, size=100, mtime_ns=1000):
    return {"size": size, "mtime_ns": mtime_ns}


def _old_style_meta(path):
    """A cached record from before genre/year/bpm/key existed."""
    return {"path": path, "title": path, "artist": "Artist", "album": "Album"}


def _backfilled_meta(path):
    return {
        "path": path, "title": path, "artist": "Artist", "album": "Album",
        "genre": "Rock", "year": "1999", "bpm": "120", "key": "Am",
    }


class FakeWindow:
    def __init__(self, folders_and_tracks):
        """folders_and_tracks: {folder_path: [(track_path, meta_dict), ...]}"""
        self._folders_and_tracks = folders_and_tracks
        self.saved_cache_calls = []
        folders = []
        meta = []
        for folder, tracks in folders_and_tracks.items():
            fingerprints = {path: _fingerprint(path) for path, _ in tracks}
            folders.append({
                "path": folder,
                "signature": "sig-" + folder,
                "tracks": [path for path, _ in tracks],
                "file_fingerprints": fingerprints,
            })
            meta.extend(m for _, m in tracks)
        self._cache = {"folders": folders, "meta": meta}

    def _load_cache(self):
        return self._cache

    def _save_cache(self, folders, meta_list):
        self.saved_cache_calls.append(
            (
                {f["path"] for f in folders},
                {m["path"]: m for m in meta_list},
            )
        )

    def _scan_folder(self, folder, progress=None, should_cancel=None):
        tracks = [path for path, _ in self._folders_and_tracks[folder]]
        fingerprints = {path: _fingerprint(path) for path in tracks}
        return "sig-" + folder, tracks, fingerprints


def _fake_read_track_meta(path):
    return _backfilled_meta(path)


def test_normal_scan_ignores_backfill_fields_and_reuses_matching_fingerprint(monkeypatch):
    monkeypatch.setattr(workers, "read_track_meta", _fake_read_track_meta)
    window = FakeWindow({"F1": [("F1/a.mp3", _old_style_meta("F1/a.mp3"))]})
    thread = LibraryScanThread(window, add_folder=None, force_full_reread=False)
    meta_list = []
    thread.finished_scan.connect(lambda ml, fe: meta_list.extend(ml))
    thread.run()
    # Fingerprint matches and force_full_reread is off -- old-style record
    # reused as-is, even though it's missing genre/year/bpm/key.
    assert meta_list == [_old_style_meta("F1/a.mp3")]
    assert window.saved_cache_calls == []


def test_force_full_reread_rereads_old_records_despite_matching_fingerprint(monkeypatch):
    monkeypatch.setattr(workers, "read_track_meta", _fake_read_track_meta)
    window = FakeWindow({"F1": [("F1/a.mp3", _old_style_meta("F1/a.mp3"))]})
    thread = LibraryScanThread(window, add_folder=None, force_full_reread=True)
    meta_list = []
    thread.finished_scan.connect(lambda ml, fe: meta_list.extend(ml))
    thread.run()
    assert meta_list == [_backfilled_meta("F1/a.mp3")]


def test_force_full_reread_skips_already_backfilled_records(monkeypatch):
    def fail(path):
        raise AssertionError(f"read_track_meta should not be called for {path}")

    monkeypatch.setattr(workers, "read_track_meta", fail)
    window = FakeWindow({"F1": [("F1/a.mp3", _backfilled_meta("F1/a.mp3"))]})
    thread = LibraryScanThread(window, add_folder=None, force_full_reread=True)
    meta_list = []
    thread.finished_scan.connect(lambda ml, fe: meta_list.extend(ml))
    thread.run()
    # Already has genre/year/bpm/key -- must be reused, not re-read, even
    # though force_full_reread is on. This is what makes an interrupted
    # backfill resumable instead of restarting from scratch.
    assert meta_list == [_backfilled_meta("F1/a.mp3")]


def test_normal_scan_never_writes_checkpoints(monkeypatch):
    monkeypatch.setattr(workers, "read_track_meta", _fake_read_track_meta)
    tracks = [(f"F1/{i}.mp3", _old_style_meta(f"F1/{i}.mp3")) for i in range(301)]
    window = FakeWindow({"F1": tracks})
    thread = LibraryScanThread(window, add_folder=None, force_full_reread=False)
    thread.run()
    assert window.saved_cache_calls == []


def test_backfill_checkpoints_periodically_during_a_large_folder(monkeypatch):
    monkeypatch.setattr(workers, "read_track_meta", _fake_read_track_meta)
    tracks = [(f"F1/{i}.mp3", _old_style_meta(f"F1/{i}.mp3")) for i in range(301)]
    window = FakeWindow({"F1": tracks})
    thread = LibraryScanThread(window, add_folder=None, force_full_reread=True)
    thread.run()
    # One checkpoint at the 300th re-read, plus one at the folder boundary.
    assert len(window.saved_cache_calls) >= 2


def test_checkpoint_snapshot_preserves_untouched_folders(monkeypatch):
    monkeypatch.setattr(workers, "read_track_meta", _fake_read_track_meta)
    big_folder_tracks = [(f"F1/{i}.mp3", _old_style_meta(f"F1/{i}.mp3")) for i in range(301)]
    window = FakeWindow({
        "F1": big_folder_tracks,
        "F2": [("F2/z.mp3", _old_style_meta("F2/z.mp3"))],
    })
    thread = LibraryScanThread(window, add_folder=None, force_full_reread=True)
    thread.run()
    # The very first checkpoint happens mid-way through F1, before F2 has
    # been touched at all -- its snapshot must still account for F2's
    # folder and track, reused unchanged from the original cache.
    first_folders, first_meta = window.saved_cache_calls[0]
    assert "F2" in first_folders
    assert first_meta["F2/z.mp3"] == _old_style_meta("F2/z.mp3")


def test_checkpoint_does_not_duplicate_already_processed_paths(monkeypatch):
    monkeypatch.setattr(workers, "read_track_meta", _fake_read_track_meta)
    tracks = [(f"F1/{i}.mp3", _old_style_meta(f"F1/{i}.mp3")) for i in range(301)]
    window = FakeWindow({"F1": tracks})
    thread = LibraryScanThread(window, add_folder=None, force_full_reread=True)
    thread.run()
    _, first_meta = window.saved_cache_calls[0]
    # Exactly one meta record per path in the mid-scan checkpoint -- no
    # double-counting between "already reread" and "cached fallback".
    assert len(first_meta) == 301
