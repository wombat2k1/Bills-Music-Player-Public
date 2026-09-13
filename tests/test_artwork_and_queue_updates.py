import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtGui, QtWidgets

import billsmusic.artwork as artwork_module
import billsmusic.window as window_module
from billsmusic.artwork import ArtworkManager
from billsmusic.window import PlayerWindow


_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _png_bytes():
    image = QtGui.QImage(16, 16, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor("magenta"))
    data = QtCore.QByteArray()
    buffer = QtCore.QBuffer(data)
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(data)


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        _app().processEvents()
        time.sleep(0.005)
    assert predicate()


def test_artwork_read_decode_and_scale_run_off_gui_thread(monkeypatch):
    _app()
    calls = []

    def read(path):
        calls.append(threading.current_thread().name)
        return _png_bytes()

    monkeypatch.setattr(artwork_module, "read_cover_bytes", read)
    manager = ArtworkManager(max_pending=4, max_cache=4)
    results = []
    manager.request("album", ["track.flac"], None, (40, 40), 1, results.append)
    _wait_until(lambda: len(results) == 1)

    assert calls and all(name != "MainThread" for name in calls)
    assert results[0].thread_name != "MainThread"
    assert results[0].image.size().width() <= 40
    manager.shutdown()


def test_duplicate_artwork_requests_decode_once_and_memory_cache_hits(monkeypatch):
    _app()
    calls = []

    def read(path):
        calls.append(path)
        time.sleep(0.02)
        return _png_bytes()

    monkeypatch.setattr(artwork_module, "read_cover_bytes", read)
    manager = ArtworkManager(max_pending=4, max_cache=2)
    results = []
    manager.request("album", ["one.flac"], None, (40, 40), 1, results.append)
    manager.request("album", ["one.flac"], None, (40, 40), 2, results.append)
    _wait_until(lambda: len(results) == 2)
    manager.request("album", ["one.flac"], None, (40, 40), 3, results.append)
    _wait_until(lambda: len(results) == 3)

    assert calls == ["one.flac"]
    assert results[-1].source == "memory-cache"
    assert {result.generation for result in results} == {1, 2, 3}
    manager.shutdown()


def test_missing_artwork_negative_cache_prevents_repeat_extraction(monkeypatch):
    _app()
    calls = []
    events = []
    monkeypatch.setattr(
        artwork_module,
        "read_cover_bytes",
        lambda path: calls.append(path) or None,
    )
    manager = ArtworkManager(max_pending=4, max_cache=2)
    manager.diagnostic.connect(events.append)
    manager.request("missing", ["one.flac"], None, (40, 40), 1, lambda result: None)
    _wait_until(lambda: any(
        event["event"] == "negative_cache_store" for event in events
    ))
    assert manager.request(
        "missing", ["one.flac"], None, (40, 40), 2, lambda result: None
    ) is False
    assert calls == ["one.flac"]
    assert events[-1]["event"] == "negative_cache_hit"
    assert events[-1]["status"] == "not_found"
    manager.shutdown()


class _Diagnostics:
    def __init__(self):
        self.events = []

    def record(self, *args, **kwargs):
        self.events.append((args, kwargs))


class QueueHarness(QtWidgets.QWidget):
    _queue_played_role = PlayerWindow._queue_played_role
    _queue_playlist_role = PlayerWindow._queue_playlist_role
    _ensure_queue_played_flags = PlayerWindow._ensure_queue_played_flags
    _keep_played_tracks_at_bottom = PlayerWindow._keep_played_tracks_at_bottom
    _queue_column_label = PlayerWindow._queue_column_label
    _queue_title_marquee = PlayerWindow._queue_title_marquee
    _refresh_queue_list = PlayerWindow._refresh_queue_list
    _update_queue_rows = PlayerWindow._update_queue_rows
    _queue_spinner_tick = PlayerWindow._queue_spinner_tick
    _on_queue_analysis_ready = PlayerWindow._on_queue_analysis_ready
    _request_queue_analysis = PlayerWindow._request_queue_analysis
    _start_legacy_queue_enrichment = (
        PlayerWindow._start_legacy_queue_enrichment
    )
    _request_next_legacy_queue_metadata = (
        PlayerWindow._request_next_legacy_queue_metadata
    )
    _on_queue_metadata_ready = PlayerWindow._on_queue_metadata_ready
    _insert_queue_row_widget = PlayerWindow._insert_queue_row_widget
    _remove_queue_row_widget = PlayerWindow._remove_queue_row_widget
    _shutdown_queue_row_marquee = PlayerWindow._shutdown_queue_row_marquee
    _move_queue_row_widget = PlayerWindow._move_queue_row_widget
    _insert_unplayed_queue_item = PlayerWindow._insert_unplayed_queue_item
    _first_played_queue_row = PlayerWindow._first_played_queue_row
    _queue_add = PlayerWindow._queue_add
    _record_queue_event_loop_return = (
        PlayerWindow._record_queue_event_loop_return
    )
    _display_meta_for_path = PlayerWindow._display_meta_for_path

    def __init__(self):
        super().__init__()
        self.queue_list = QtWidgets.QListWidget()
        self.queue = ["duplicate.flac", "other.flac", "duplicate.flac"]
        self.queue_played = [False, False, False]
        self._meta_list = []
        self.queue_detail_cache = {
            "duplicate.flac": {
                "time": "3:00", "bitrate": "900k", "key": "--", "bpm": "--"
            },
            "other.flac": {
                "time": "2:00", "bitrate": "800k", "key": "Am", "bpm": "120"
            },
        }
        self.queue_analysis_pending = {"duplicate.flac"}
        self.queue_bpm_key_pending_fields = {"duplicate.flac": {"bpm", "key"}}
        self._queue_priority_deferred_paths = set()
        self.queue_spinner_index = 0
        self.queue_spinner_timer = QtCore.QTimer(self)
        self._queue_row_widgets = {}
        self.diagnostics = _Diagnostics()
        self.current_path = ""
        self.synced = 0
        self._meta_by_path = {}
        self._legacy_queue_metadata_backlog = []
        self._queue_metadata_pending = None
        self._closing = False
        self.queue_analysis_worker = None
        self.stored_analysis = []
        self.dj_info_calls = []

    def _queue_track_details(self, path, cached_details_only=False):
        return self.queue_detail_cache[path]

    def _sync_mini_player(self):
        self.synced += 1

    def _store_queue_analysis(self, path, result, signature=None):
        self.stored_analysis.append((path, result, signature))

    def _update_dj_info(self, info=None, path=None):
        self.dj_info_calls.append((info, path))

    _load_cached_audio_tags = PlayerWindow._load_cached_audio_tags

    def _audio_log(self, message):
        pass

    def _audio_name(self, path):
        return path

    def _schedule_session_save(self):
        pass


class QueueCacheHarness:
    _cached_queue_analysis = PlayerWindow._cached_queue_analysis
    _queue_track_details = PlayerWindow._queue_track_details

    def __init__(self):
        self.queue_analysis_cache = {
            "X:/music/track.flac": {
                "mtime": 1,
                "size": 2,
                "result": {"key": "Am", "bpm": "120"},
            }
        }
        self.queue_detail_cache = {}

    def _file_signature(self, path):
        raise AssertionError("cached startup rendering touched the filesystem")


def test_cached_startup_queue_details_do_not_validate_network_files():
    harness = QueueCacheHarness()

    details = harness._queue_track_details(
        "X:/music/track.flac", cached_details_only=True
    )

    assert details["key"] == "Am"
    assert details["bpm"] == "120"


class QueueCachePersistenceHarness(QueueCacheHarness):
    _store_queue_analysis = PlayerWindow._store_queue_analysis

    def _save_queue_analysis_cache(self):
        pass

    def _schedule_queue_analysis_cache_save(self):
        pass


def test_time_and_rate_survive_queue_cache_restart():
    writer = QueueCachePersistenceHarness()
    writer._store_queue_analysis(
        "X:/music/track.flac",
        {
            "time": "4:07", "bitrate": "1411k",
            "key": "Am", "bpm": "120",
        },
        signature={"mtime": 10, "size": 20},
    )
    saved_cache = writer.queue_analysis_cache

    reader = QueueCachePersistenceHarness()
    reader.queue_analysis_cache = saved_cache
    reader.queue_detail_cache = {}
    details = reader._queue_track_details(
        "X:/music/track.flac", cached_details_only=True
    )

    assert details == {
        "time": "4:07", "bitrate": "1411k",
        "key": "Am", "bpm": "120",
    }


def test_store_queue_analysis_debounces_the_cache_write():
    # v1.0.67 MainThread I/O hardening: this used to call
    # _save_queue_analysis_cache() -- a full JSON serialise+write of the
    # whole, up-to-2000-entry cache -- unconditionally on every single
    # completed background BPM/key result. It must now only *schedule* a
    # debounced write, the same way session saves already are.
    _app()
    save_calls = []
    writer = QueueCachePersistenceHarness()
    writer._queue_analysis_save_timer = QtCore.QTimer()
    writer._queue_analysis_save_timer.setSingleShot(True)
    writer._queue_analysis_save_timer.setInterval(10)
    writer._queue_analysis_save_timer.timeout.connect(lambda: save_calls.append(1))
    writer._schedule_queue_analysis_cache_save = PlayerWindow._schedule_queue_analysis_cache_save.__get__(writer)

    writer._store_queue_analysis(
        "X:/music/track.flac", {"time": "4:07"}, signature={"mtime": 10, "size": 20},
    )
    assert save_calls == []  # not written synchronously

    QtCore.QTimer.singleShot(50, _app().quit)
    _app().exec()
    assert save_calls == [1]


class CachedStartupQueueHarness(QueueHarness):
    _cached_queue_analysis = PlayerWindow._cached_queue_analysis
    _queue_track_details = PlayerWindow._queue_track_details

    def __init__(self, rows=180):
        super().__init__()
        self.queue = [f"X:/network/track-{index}.flac" for index in range(rows)]
        self.queue_played = [index % 7 == 0 for index in range(rows)]
        self.queue_playlist_entries = [None] * rows
        self.queue_detail_cache = {}
        self.queue_analysis_cache = {
            path: {
                "mtime": 1,
                "size": 2,
                "result": {"key": "Am", "bpm": "120"},
            }
            for path in self.queue
        }

    def _file_signature(self, path):
        raise AssertionError("startup queue restoration performed os.stat")


def test_cached_startup_restores_180_rows_without_filesystem_access():
    _app()
    harness = CachedStartupQueueHarness()

    harness._refresh_queue_list(
        cached_details_only=True, reason="startup_cache_restore_test"
    )

    assert harness.queue_list.count() == 180
    refresh = [
        event for event in harness.diagnostics.events
        if event[0][1] == "refresh_up_next"
    ][-1]
    details = refresh[1]["details"]
    assert details["filesystem_calls"] == 0
    assert details["tag_reads"] == 0
    assert details["linear_library_searches"] == 0
    assert details["rows_inserted"] == 180


def test_analysis_result_updates_all_duplicate_rows_without_full_refresh():
    _app()
    harness = QueueHarness()
    harness._refresh_queue_list(reason="test_setup")
    refresh_events = len(harness.diagnostics.events)
    harness._on_queue_analysis_ready(
        "duplicate.flac", {"key": "F#m", "bpm": "128"}
    )

    rows = harness._queue_row_widgets["duplicate.flac"]
    assert len(rows) == 2
    assert [row["key"].text() for row in rows] == ["F#m", "F#m"]
    assert [row["bpm"].text() for row in rows] == ["128", "128"]
    assert harness.queue_list.count() == 3
    assert len(harness.diagnostics.events) == refresh_events + 1
    assert harness.diagnostics.events[-1][0][1] == "targeted_up_next_update"


def test_analysis_result_updates_duplicate_rows_for_an_mp4_path():
    # Video's audio track now reaches genuine BPM/Key analysis -- the
    # completion path is media-type-agnostic, but this proves it directly
    # for a video-classified path rather than relying only on the .flac
    # case above.
    _app()
    harness = QueueHarness()
    harness.queue = ["clip.mp4", "other.flac", "clip.mp4"]
    harness.queue_played = [False, False, False]
    harness.queue_detail_cache["clip.mp4"] = {
        "time": "3:52", "bitrate": "318k", "key": "--", "bpm": "--"
    }
    harness.queue_analysis_pending = set()
    harness.queue_bpm_key_pending_fields = {"clip.mp4": {"bpm", "key"}}
    harness._refresh_queue_list(reason="test_setup")

    harness._on_queue_analysis_ready("clip.mp4", {"key": "Am", "bpm": "117"})

    rows = harness._queue_row_widgets["clip.mp4"]
    assert len(rows) == 2
    assert [row["key"].text() for row in rows] == ["Am", "Am"]
    assert [row["bpm"].text() for row in rows] == ["117", "117"]


def test_current_track_dj_display_updates_from_completed_analysis_no_mutagen():
    # Regression for a proven real-device GUI-thread stall: MainThread
    # caught inside Mutagen MP4 atom reading via exactly
    # _on_queue_analysis_ready -> _update_dj_info -> _read_tags ->
    # read_full_tag_display. _load_cached_audio_tags builds the DJ
    # display purely from in-memory caches -- no file I/O at all.
    def _boom(*a, **kw):
        raise AssertionError(
            "_on_queue_analysis_ready must never re-open the file with "
            "Mutagen after a background worker already returned metadata"
        )

    original = window_module.MutagenFile
    window_module.MutagenFile = _boom
    try:
        _app()
        harness = QueueHarness()
        harness.current_path = "duplicate.flac"

        harness._on_queue_analysis_ready(
            "duplicate.flac", {"key": "F#m", "bpm": "128"},
        )

        assert len(harness.dj_info_calls) == 1
        info, path = harness.dj_info_calls[0]
        assert path == "duplicate.flac"
        assert info is not None
        assert info.title  # a real Track object, not None
    finally:
        window_module.MutagenFile = original


def test_on_queue_metadata_ready_never_touches_mutagen_for_current_track():
    def _boom(*a, **kw):
        raise AssertionError("metadata-ready callback must never call MutagenFile")

    original = window_module.MutagenFile
    window_module.MutagenFile = _boom
    try:
        _app()
        harness = QueueHarness()
        harness.current_path = "duplicate.flac"

        harness._on_queue_metadata_ready("duplicate.flac", {"time": "3:15"})

        assert len(harness.dj_info_calls) == 1
    finally:
        window_module.MutagenFile = original


def test_legacy_time_and_rate_are_enriched_one_track_at_a_time():
    _app()
    harness = QueueHarness()
    harness.queue_detail_cache["duplicate.flac"]["time"] = "--"
    harness.queue_detail_cache["duplicate.flac"]["bitrate"] = "--"
    requests = []
    harness.queue_analysis_worker = type(
        "Worker",
        (),
        {"request_metadata": lambda self, path: requests.append(path)},
    )()
    harness._refresh_queue_list(reason="test_setup")
    original_items = [
        harness.queue_list.item(row)
        for row in range(harness.queue_list.count())
    ]

    harness._start_legacy_queue_enrichment()

    assert requests == ["duplicate.flac"]
    assert harness._queue_metadata_pending == "duplicate.flac"
    harness._on_queue_metadata_ready(
        "duplicate.flac",
        {
            "time": "4:07", "bitrate": "1411k",
            "_mtime": 10, "_size": 20,
        },
    )
    rows = harness._queue_row_widgets["duplicate.flac"]
    assert [row["time"].text() for row in rows] == ["4:07", "4:07"]
    assert [row["bitrate"].text() for row in rows] == ["1411k", "1411k"]
    assert [
        harness.queue_list.item(row)
        for row in range(harness.queue_list.count())
    ] == original_items
    stored_path, stored_result, stored_signature = harness.stored_analysis[-1]
    assert stored_path == "duplicate.flac"
    assert stored_result["time"] == "4:07"
    assert stored_result["bitrate"] == "1411k"
    assert stored_signature == {"mtime": 10, "size": 20}


def test_single_track_add_inserts_one_row_and_preserves_existing_widgets():
    _app()
    harness = QueueHarness()
    harness._refresh_queue_list(reason="test_setup")
    existing_items = [
        harness.queue_list.item(row)
        for row in range(harness.queue_list.count())
    ]
    harness.queue_detail_cache["new.flac"] = {
        "time": "--", "bitrate": "--", "key": "--", "bpm": "--"
    }

    harness._queue_add("new.flac")

    assert harness.queue_list.count() == 4
    assert all(
        harness.queue_list.item(row) is existing_items[row]
        for row in range(3)
    )
    event = harness.diagnostics.events[-1]
    assert event[0][1] == "incremental_up_next_update"
    assert event[1]["details"]["reason"] == "track_added"


def test_incremental_remove_and_move_preserve_unaffected_widgets():
    _app()
    harness = QueueHarness()
    harness._refresh_queue_list(reason="test_setup")
    first = harness.queue_list.item(0)
    second = harness.queue_list.item(1)
    third = harness.queue_list.item(2)

    assert harness._move_queue_row_widget(1, 0, reason="track_moved")
    assert harness.queue_list.item(0) is second
    assert harness.queue_list.item(1) is first
    assert harness.queue_list.item(2) is third

    assert harness._remove_queue_row_widget(1, reason="track_removed")
    assert harness.queue_list.item(0) is second
    assert harness.queue_list.item(1) is third


# ---------------------------------------------------------------------------
# MarqueeLabel real-object-lifetime regression (2026-08-31 Codex audit,
# section 15): a queue row's MarqueeLabel has its own real 20ms QTimer --
# these tests use the real widget/event loop (not a stub), pumping
# QApplication.processEvents() across real row removal/rebuild cycles, to
# prove no queued tick/paint can run on a tearing-down MarqueeLabel and no
# AttributeError/RuntimeError escapes.
# ---------------------------------------------------------------------------

def _pump(seconds=0.12):
    app = _app()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


def test_marquee_real_timer_ticks_normally_before_removal():
    _app()
    harness = QueueHarness()
    harness._refresh_queue_list(reason="test_setup")
    label = harness._queue_row_widgets["duplicate.flac"][0]["title"]
    assert label._shutting_down is False
    assert label._timer.isActive()

    _pump(0.12)  # several real 20ms ticks

    assert label._offset > 0.0 or label._edge_pause_ticks > 0, (
        "the real timer should have advanced (or edge-paused) the marquee "
        "at least once by now"
    )


def test_marquee_shutdown_stops_ticking_and_survives_further_pumping():
    _app()
    harness = QueueHarness()
    harness._refresh_queue_list(reason="test_setup")
    label = harness._queue_row_widgets["duplicate.flac"][0]["title"]
    _pump(0.06)

    label.shutdown()
    assert label._shutting_down is True
    assert not label._timer.isActive()
    offset_at_shutdown = label._offset

    # Real, further event-loop pumping must not raise, and must not move
    # the label's state at all -- no queued tick may still run.
    _pump(0.12)
    assert label._offset == offset_at_shutdown

    # A stray direct call (as if some other code still held a reference)
    # must also be a safe no-op, not an exception.
    label._tick()
    label.setText("should be ignored")
    assert label._offset == offset_at_shutdown


def test_marquee_real_removal_and_deferred_delete_raises_nothing():
    """Drives the actual production path (_remove_queue_row_widget ->
    MarqueeLabel.shutdown() -> widget.deleteLater()) and pumps the real
    event loop long enough for Qt's deferred deletion to genuinely run,
    across several repeated remove/reinsert cycles -- the exact "queue-row
    removal/reinsert" pattern the real incident correlated with. Any
    unhandled AttributeError/RuntimeError from a queued tick/paint firing
    during teardown would surface here as a real exception."""
    _app()
    harness = QueueHarness()
    harness._refresh_queue_list(reason="test_setup")

    for cycle in range(4):
        label = harness._queue_row_widgets["duplicate.flac"][0]["title"]
        assert label._shutting_down is False

        assert harness._remove_queue_row_widget(0, reason="track_removed")
        assert label._shutting_down is True, (
            f"cycle {cycle}: removed row's marquee was not shut down"
        )

        # Real event-loop pumping: lets the deferred-deleted wrapper widget
        # (and this label as its child) actually get destroyed by Qt, and
        # lets any already-queued timer tick attempt to fire (it must now
        # be a safe no-op per the guard, having already been stopped and
        # disconnected by shutdown() above).
        _pump(0.08)

        # Reinsert the same track so the next cycle has a fresh row/label.
        harness.queue.insert(0, "duplicate.flac")
        harness.queue_played.insert(0, False)
        harness._refresh_queue_list(reason="track_added")

    # No exception reached this point -- the whole point of the test.
    assert harness.queue_list.count() == len(harness.queue)


def test_marquee_full_rebuild_shuts_down_every_old_row_label():
    _app()
    harness = QueueHarness()
    harness._refresh_queue_list(reason="test_setup")
    old_labels = [
        entries[0]["title"] for entries in harness._queue_row_widgets.values()
    ]
    assert len(old_labels) == 2  # "duplicate.flac", "other.flac"

    harness._refresh_queue_list(reason="rebuild")

    for label in old_labels:
        assert label._shutting_down is True, (
            "a full Up Next rebuild must shut down every old row's "
            "MarqueeLabel before discarding _queue_row_widgets"
        )
        assert not label._timer.isActive()

    new_labels = [
        entries[0]["title"] for entries in harness._queue_row_widgets.values()
    ]
    assert all(label._shutting_down is False for label in new_labels)
    assert all(label not in old_labels for label in new_labels)

    _pump(0.08)  # let the old rows' deferred deletion actually run


def test_spinner_updates_target_rows_without_rebuilding_queue():
    _app()
    harness = QueueHarness()
    harness._refresh_queue_list(reason="test_setup")
    original_items = [harness.queue_list.item(i) for i in range(3)]
    harness._queue_spinner_tick()

    assert [harness.queue_list.item(i) for i in range(3)] == original_items
    assert harness._queue_row_widgets["duplicate.flac"][0]["key"].text() == "/"
