"""AlbumTagRefreshWorker: re-reads tags for one album group and rescans its
folder(s) for new files, off the GUI thread.

Reported: the GUI froze while refreshing tags on a Music Videos album --
a captured stall trace showed the main thread stuck inside mutagen's tag
reading (metadata.py's read_track_meta -> _read_raw_bpm_key_fallback ->
mutagen._util._openfile) while handling _show_tree_menu's "Refresh Tags"
action, which used to do this inline. The album's folder was backed by a
slow network share, so the file I/O blocked the whole window.

Tests call run() directly (not start()) -- the worker has no Qt-specific
behavior beyond the finished_refresh signal, so exercising the real logic
synchronously is simpler and just as meaningful, matching the pattern
already used for LibraryScanThread in tests/test_library_scan_thread.py.
"""
import inspect
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import billsmusic.workers as workers
import billsmusic.window as window_module
from billsmusic.window import PlayerWindow
from billsmusic.workers import AlbumTagRefreshWorker


def _meta(path, album, **extra):
    entry = {"path": path, "album": album, "title": os.path.basename(path)}
    entry.update(extra)
    return entry


def test_refreshes_only_tracks_in_the_target_album(monkeypatch):
    monkeypatch.setattr(os.path, "isdir", lambda path: False)  # no folder rescan
    read_calls = []

    def fake_read_track_meta(path):
        read_calls.append(path)
        return _meta(path, "Greatest Hits", title="REFRESHED")

    monkeypatch.setattr(workers, "read_track_meta", fake_read_track_meta)

    full_meta_list = [
        _meta("a.mp4", "Greatest Hits"),
        _meta("b.mp4", "Greatest Hits"),
        _meta("c.mp4", "Other Album"),
    ]
    worker = AlbumTagRefreshWorker("Greatest Hits", full_meta_list)

    results = {}
    worker.finished_refresh.connect(lambda refreshed: results.setdefault("refreshed", refreshed))
    worker.run()

    assert sorted(read_calls) == ["a.mp4", "b.mp4"]
    refreshed = results["refreshed"]
    assert len(refreshed) == 3
    other = next(m for m in refreshed if m["path"] == "c.mp4")
    assert other["album"] == "Other Album" and other["title"] != "REFRESHED"
    for path in ("a.mp4", "b.mp4"):
        entry = next(m for m in refreshed if m["path"] == path)
        assert entry["title"] == "REFRESHED"


def test_rescans_album_folder_and_picks_up_new_files(monkeypatch, tmp_path):
    folder = tmp_path / "Greatest Hits"
    folder.mkdir()
    existing = folder / "a.mp4"
    existing.write_bytes(b"x")
    new_file = folder / "b.mp4"
    new_file.write_bytes(b"x")
    ignored = folder / "notes.txt"
    ignored.write_bytes(b"x")

    monkeypatch.setattr(workers, "SUPPORTED_EXTENSIONS", {".mp4"})

    def fake_read_track_meta(path):
        return _meta(path, "Greatest Hits")

    monkeypatch.setattr(workers, "read_track_meta", fake_read_track_meta)

    full_meta_list = [_meta(str(existing), "Greatest Hits")]
    worker = AlbumTagRefreshWorker("Greatest Hits", full_meta_list)

    results = {}
    worker.finished_refresh.connect(lambda refreshed: results.setdefault("refreshed", refreshed))
    worker.run()

    refreshed_paths = {m["path"] for m in results["refreshed"]}
    assert str(existing) in refreshed_paths
    assert str(new_file) in refreshed_paths
    assert not any(p.endswith("notes.txt") for p in refreshed_paths)


def test_does_not_touch_other_albums_folders(monkeypatch, tmp_path):
    target_folder = tmp_path / "target"
    target_folder.mkdir()
    other_folder = tmp_path / "other"
    other_folder.mkdir()
    (other_folder / "extra.mp4").write_bytes(b"x")

    monkeypatch.setattr(workers, "SUPPORTED_EXTENSIONS", {".mp4"})
    monkeypatch.setattr(workers, "read_track_meta", lambda path: _meta(path, "Target"))

    full_meta_list = [
        _meta(str(target_folder / "a.mp4"), "Target"),
        _meta(str(other_folder / "b.mp4"), "Other"),
    ]
    worker = AlbumTagRefreshWorker("Target", full_meta_list)

    results = {}
    worker.finished_refresh.connect(lambda refreshed: results.setdefault("refreshed", refreshed))
    worker.run()

    refreshed_paths = {m["path"] for m in results["refreshed"]}
    # The other album's folder must never be walked -- its untouched entry
    # passes through unchanged, and the extra file sitting in its folder
    # must not appear.
    assert not any("extra.mp4" in p for p in refreshed_paths)


def test_emits_exactly_once():
    worker = AlbumTagRefreshWorker("Empty Album", [])
    emit_count = []
    worker.finished_refresh.connect(lambda refreshed: emit_count.append(refreshed))
    worker.run()
    assert len(emit_count) == 1
    assert emit_count[0] == []


# ---------------------------------------------------------------------------
# window.py wiring: _refresh_album_tags kicks off the worker instead of
# blocking, _on_album_tags_refreshed applies its result.
# ---------------------------------------------------------------------------

class _FakeSignal:
    def __init__(self):
        self._slot = None

    def connect(self, slot):
        self._slot = slot


class _FakeWorker:
    """Stands in for AlbumTagRefreshWorker -- records construction args and
    start() calls without doing any real threading or file I/O, so these
    tests prove _refresh_album_tags hands off to a worker rather than
    running the read_track_meta loop itself."""
    instances = []

    def __init__(self, album, full_meta_list):
        self.album = album
        self.full_meta_list = list(full_meta_list)
        self.finished_refresh = _FakeSignal()
        self.finished = _FakeSignal()
        self.started = False
        _FakeWorker.instances.append(self)

    def start(self):
        self.started = True

    def isRunning(self):
        return False

    def cancel(self):
        pass


def test_refresh_album_tags_hands_off_to_a_background_worker(monkeypatch):
    _FakeWorker.instances.clear()
    monkeypatch.setattr(window_module, "AlbumTagRefreshWorker", _FakeWorker)
    from billsmusic.worker_registry import WorkerLifetimeRegistry
    window = SimpleNamespace(
        _full_meta_list=[{"path": "a.mp4", "album": "Target"}],
        _album_tag_refresh_thread=None,
        _cancel_metadata_backfill=lambda: None,
        statusBar=lambda: SimpleNamespace(showMessage=lambda *a, **kw: None),
        _worker_registry=WorkerLifetimeRegistry(),
        _maybe_resume_final_shutdown=lambda: None,
    )
    window._on_album_tags_refreshed = lambda refreshed: PlayerWindow._on_album_tags_refreshed(window, refreshed)

    PlayerWindow._refresh_album_tags(window, {"album": "Target"})

    assert len(_FakeWorker.instances) == 1
    worker = _FakeWorker.instances[0]
    assert worker.album == "Target"
    assert worker.full_meta_list == [{"path": "a.mp4", "album": "Target"}]
    assert worker.started is True
    assert window._album_tag_refresh_thread is worker
    # The completion handler must actually be wired up, not left for
    # nothing to call.
    assert worker.finished_refresh._slot is window._on_album_tags_refreshed


def test_refresh_album_tags_is_a_no_op_while_one_is_already_running(monkeypatch):
    def _fail(*a, **kw):
        raise AssertionError("must not construct a second worker while one is running")

    monkeypatch.setattr(window_module, "AlbumTagRefreshWorker", _fail)
    window = SimpleNamespace(
        _full_meta_list=[],
        _album_tag_refresh_thread=SimpleNamespace(isRunning=lambda: True),
        _cancel_metadata_backfill=lambda: None,
        statusBar=lambda: SimpleNamespace(showMessage=lambda *a, **kw: None),
    )

    PlayerWindow._refresh_album_tags(window, {"album": "Target"})  # must not raise


def test_refresh_album_tags_does_nothing_without_an_album():
    window = SimpleNamespace(_cancel_metadata_backfill=lambda: (_ for _ in ()).throw(
        AssertionError("must return before touching anything else")
    ))
    PlayerWindow._refresh_album_tags(window, {})  # no "album" key -- must not raise


def test_on_album_tags_refreshed_applies_the_result_and_clears_the_thread():
    calls = []
    window = SimpleNamespace(
        _album_tag_refresh_thread=object(),
        _local_full_meta_list_backing=[],
        _rebuild_library_search_index=lambda: calls.append("rebuild_index"),
        _refresh_library_view_after_change=lambda: calls.append("refresh_view"),
        _load_cache=lambda: {"folders": ["F1"]},
        _save_cache=lambda folders, meta: calls.append(("save_cache", folders, meta)),
        statusBar=lambda: SimpleNamespace(showMessage=lambda *a, **kw: calls.append("status")),
    )
    refreshed = [{"path": "a.mp4", "album": "Target"}]

    PlayerWindow._on_album_tags_refreshed(window, refreshed)

    assert window._album_tag_refresh_thread is None
    # Written directly to the Local backing store, not through the
    # ambient _full_meta_list setter -- album tag refresh is a Local-
    # only operation (Plex albums never reach _refresh_album_tags), and
    # the ambient setter would misfile this under Plex if the user
    # happened to be viewing the Plex tab (see the real-device Local-
    # library-loss regression this pattern guards against).
    assert window._local_full_meta_list_backing == refreshed
    assert "rebuild_index" in calls
    assert "refresh_view" in calls
    assert ("save_cache", ["F1"], refreshed) in calls


def test_refresh_album_tags_no_longer_reads_tags_on_the_calling_thread():
    # Regression guard: the whole point of this fix is that the GUI thread
    # must never call read_track_meta or os.walk directly from this method
    # again -- assert it via source inspection so a future edit that
    # reintroduces inline I/O here fails loudly.
    source = inspect.getsource(PlayerWindow._refresh_album_tags)
    assert "read_track_meta" not in source
    assert "os.walk" not in source
