import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets

from billsmusic.window import PlayerWindow
from billsmusic.playlist_repair import PlaylistEntry
from billsmusic.queue_undo import GENERIC_UNDO_LABEL

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


class _RecordingDiagnostics:
    def __init__(self):
        self.events = []

    def record(self, category, operation, **kwargs):
        self.events.append((category, operation, kwargs))

    def path_details(self, path):
        return {"path_hash": "test", "extension": ""}


def _raise_if_called(*args, **kwargs):
    raise AssertionError("a confirmation dialog was shown when it should not have been")


class DedupGuardHarness(QtWidgets.QMainWindow):
    _ensure_queue_played_flags = PlayerWindow._ensure_queue_played_flags
    _first_played_queue_row = PlayerWindow._first_played_queue_row
    _keep_played_tracks_at_bottom = PlayerWindow._keep_played_tracks_at_bottom
    _queue_played_role = PlayerWindow._queue_played_role
    _queue_playlist_role = PlayerWindow._queue_playlist_role
    _insert_unplayed_queue_item = PlayerWindow._insert_unplayed_queue_item
    _insert_queue_paths = PlayerWindow._insert_queue_paths
    _add_to_queue_with_dedup_guard = PlayerWindow._add_to_queue_with_dedup_guard
    _queue_add_status_text = PlayerWindow._queue_add_status_text
    _playlist_loaded = PlayerWindow._playlist_loaded
    _undo_queue_change = PlayerWindow._undo_queue_change
    _update_undo_action_state = PlayerWindow._update_undo_action_state
    _restore_queue_selection = PlayerWindow._restore_queue_selection
    _sync_queue_from_list = PlayerWindow._sync_queue_from_list

    def __init__(self, queue=None, played=None, entries=None, warn=True):
        super().__init__()
        self.setCentralWidget(QtWidgets.QWidget())
        self.queue = list(queue) if queue is not None else []
        self.queue_played = list(played) if played is not None else [False] * len(self.queue)
        self.queue_playlist_entries = list(entries) if entries is not None else [None] * len(self.queue)
        self._queue_undo_snapshot = None
        self.current_path = None
        self._loaded_playlist_entries = []
        self._loaded_playlist_filename = None
        self.warn_before_adding_duplicate_queue_tracks = warn
        self.diagnostics = _RecordingDiagnostics()
        self.refresh_calls = []
        self.refresh_cached_details_only_calls = []
        self.analysis_requested_paths = []
        self.scheduled_save_calls = 0
        self.accessible_messages = []

        from billsmusic.queue_undo import GENERIC_UNDO_LABEL as _label
        from PyQt6 import QtGui
        self.action_undo_queue_change = QtGui.QAction(_label, self)
        self.action_undo_queue_change.setEnabled(False)

        # dialogs must never actually pop up in these tests -- callers
        # override with monkeypatch when duplicates are expected.
        self._confirm_batch_duplicate_add = _raise_if_called
        self._confirm_single_duplicate_add = _raise_if_called

        self.queue_list = QtWidgets.QListWidget()
        self._rebuild_queue_list_widget()

    def _rebuild_queue_list_widget(self):
        self.queue_list.clear()
        for path, played, entry in zip(self.queue, self.queue_played, self.queue_playlist_entries):
            item = QtWidgets.QListWidgetItem(path)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, path)
            item.setData(self._queue_played_role(), played)
            item.setData(self._queue_playlist_role(), entry)
            self.queue_list.addItem(item)

    def _refresh_queue_list(self, keep_played_bottom=True, cached_details_only=False, reason="structural_change"):
        self.refresh_calls.append(reason)
        self.refresh_cached_details_only_calls.append(cached_details_only)
        if keep_played_bottom:
            self._keep_played_tracks_at_bottom()
        self._rebuild_queue_list_widget()

    def _request_queue_analysis_for_paths(self, paths):
        self.analysis_requested_paths.append(list(paths))

    def _schedule_session_save(self):
        self.scheduled_save_calls += 1

    def _save_session(self):
        pass

    def _log(self, message):
        pass

    def _announce_accessible_status(self, message, timeout=4000):
        self.accessible_messages.append(message)


def _entry(path):
    return PlaylistEntry(path=path, resolved_path=path)


# -- no duplicates: silent, single refresh/save/undo-snapshot --------------

def test_no_duplicates_adds_silently_with_no_dialog():
    _app()
    window = DedupGuardHarness(queue=["a.mp3"])
    outcome = window._add_to_queue_with_dedup_guard(["b.mp3", "c.mp3"])
    assert window.queue == ["a.mp3", "b.mp3", "c.mp3"]
    assert outcome.added_count == 2
    assert outcome.duplicate_count == 0
    assert not outcome.cancelled
    assert window.refresh_calls == ["tracks_added"]
    assert window.scheduled_save_calls == 1
    assert window._queue_undo_snapshot.action == "add"
    assert window.action_undo_queue_change.text() == "Undo Add to Up Next"
    assert window.accessible_messages == ["Added 2 tracks to Up Next"]


def test_single_track_no_duplicate_uses_singular_wording():
    _app()
    window = DedupGuardHarness(queue=[])
    window._add_to_queue_with_dedup_guard(["a.mp3"])
    assert window.accessible_messages == ["Added track to Up Next"]


def test_empty_batch_is_a_full_noop():
    _app()
    window = DedupGuardHarness(queue=["a.mp3"])
    outcome = window._add_to_queue_with_dedup_guard([])
    assert window.queue == ["a.mp3"]
    assert outcome.added_count == 0
    assert window.refresh_calls == []
    assert window.scheduled_save_calls == 0
    assert window._queue_undo_snapshot is None


# -- multi-track duplicate dialog -------------------------------------------

def test_batch_duplicate_add_new_only_skips_duplicates_preserving_order():
    _app()
    window = DedupGuardHarness(queue=["dup1.mp3", "dup2.mp3"])
    seen_args = {}

    def _confirm(duplicate_count, new_count, total_count):
        seen_args["args"] = (duplicate_count, new_count, total_count)
        return "new_only"

    window._confirm_batch_duplicate_add = _confirm
    outcome = window._add_to_queue_with_dedup_guard(
        ["new1.mp3", "dup1.mp3", "new2.mp3", "dup2.mp3"]
    )
    assert seen_args["args"] == (2, 2, 4)
    assert window.queue == ["dup1.mp3", "dup2.mp3", "new1.mp3", "new2.mp3"]
    assert outcome.added_count == 2
    assert outcome.duplicate_count == 2
    assert window.accessible_messages == ["2 tracks added; 2 duplicates skipped."]


def test_batch_duplicate_add_all_preserves_all_including_duplicates():
    _app()
    window = DedupGuardHarness(queue=["dup1.mp3"])
    window._confirm_batch_duplicate_add = lambda *a: "all"
    outcome = window._add_to_queue_with_dedup_guard(["new1.mp3", "dup1.mp3"])
    assert window.queue == ["dup1.mp3", "new1.mp3", "dup1.mp3"]
    assert outcome.added_count == 2
    assert outcome.duplicate_count == 1
    assert window.accessible_messages == ["2 tracks added, including 1 duplicate."]


def test_batch_duplicate_cancel_is_a_full_noop():
    _app()
    window = DedupGuardHarness(queue=["dup1.mp3"])
    window._confirm_batch_duplicate_add = lambda *a: "cancel"
    outcome = window._add_to_queue_with_dedup_guard(["new1.mp3", "dup1.mp3"])
    assert window.queue == ["dup1.mp3"]
    assert outcome.cancelled
    assert window.refresh_calls == []
    assert window.scheduled_save_calls == 0
    assert window._queue_undo_snapshot is None
    assert window.accessible_messages == []


def test_single_duplicate_among_several_tracks_uses_batch_dialog_singular_count():
    _app()
    window = DedupGuardHarness(queue=["dup1.mp3"])
    seen_args = {}

    def _confirm(duplicate_count, new_count, total_count):
        seen_args["args"] = (duplicate_count, new_count, total_count)
        return "new_only"

    window._confirm_batch_duplicate_add = _confirm
    window._add_to_queue_with_dedup_guard(["new1.mp3", "dup1.mp3", "new2.mp3"])
    assert seen_args["args"] == (1, 2, 3)


# -- single-track duplicate dialog -------------------------------------------

def test_single_track_duplicate_add_again_creates_separate_entry():
    _app()
    window = DedupGuardHarness(queue=["a.mp3"], played=[False])
    window._confirm_single_duplicate_add = lambda: "add_again"
    outcome = window._add_to_queue_with_dedup_guard(["a.mp3"])
    assert window.queue == ["a.mp3", "a.mp3"]
    assert len(window.queue_played) == 2
    assert outcome.added_count == 1
    assert outcome.duplicate_count == 1
    assert window.accessible_messages == ["1 track added, including 1 duplicate."]


def test_single_track_duplicate_cancel_is_a_full_noop():
    _app()
    window = DedupGuardHarness(queue=["a.mp3"])
    window._confirm_single_duplicate_add = lambda: "cancel"
    outcome = window._add_to_queue_with_dedup_guard(["a.mp3"])
    assert window.queue == ["a.mp3"]
    assert outcome.cancelled
    assert window.scheduled_save_calls == 0
    assert window._queue_undo_snapshot is None
    assert window.accessible_messages == []


# -- preference off: silent duplicates, no dialog work -----------------------

def test_preference_off_adds_duplicates_silently():
    _app()
    window = DedupGuardHarness(queue=["a.mp3"], warn=False)
    # both confirm methods raise if called -- proves they're never invoked.
    outcome = window._add_to_queue_with_dedup_guard(["a.mp3", "b.mp3"])
    assert window.queue == ["a.mp3", "a.mp3", "b.mp3"]
    assert outcome.added_count == 2
    assert outcome.duplicate_count == 1
    assert window.accessible_messages == ["2 tracks added, including 1 duplicate."]


# -- undo round-trip for the new "add" action --------------------------------

def test_undo_after_guarded_add_restores_previous_queue():
    _app()
    window = DedupGuardHarness(queue=["a.mp3"], played=[False])
    window._add_to_queue_with_dedup_guard(["b.mp3", "c.mp3"])
    assert window.queue == ["a.mp3", "b.mp3", "c.mp3"]
    window._undo_queue_change()
    assert window.queue == ["a.mp3"]
    assert window.queue_played == [False]


# -- playlist call site: critical correctness detail -------------------------

def test_playlist_add_new_only_keeps_loaded_playlist_entries_unfiltered():
    """`_loaded_playlist_entries`/`_loaded_playlist_filename` are later used
    verbatim by 'Save Repaired Playlist' to rewrite the whole M3U file.
    Choosing 'Add New Only' must never leave them pointing at the
    dedup-filtered subset, or a later repair-save would truncate the
    user's playlist file."""
    _app()
    window = DedupGuardHarness(queue=["already_queued.mp3"], played=[False])
    entries = [_entry("already_queued.mp3"), _entry("fresh.mp3")]
    window._confirm_single_duplicate_add = _raise_if_called
    window._confirm_batch_duplicate_add = lambda *a: "new_only"
    window._playlist_loaded("list.m3u", entries, {"total": 2, "missing": 0}, 5.0)
    assert window.queue == ["already_queued.mp3", "fresh.mp3"]
    # Full original list preserved regardless of the filtered queue insert.
    assert window._loaded_playlist_entries == entries
    assert window._loaded_playlist_filename == "list.m3u"


def test_playlist_add_all_preserves_duplicates_and_order():
    _app()
    window = DedupGuardHarness(queue=["a.mp3"], played=[False])
    entries = [_entry("a.mp3"), _entry("b.mp3")]
    window._confirm_batch_duplicate_add = lambda *a: "all"
    window._playlist_loaded("list.m3u", entries, {"total": 2, "missing": 0}, 5.0)
    # inserted at _first_played_queue_row(): "a.mp3" isn't marked played, so
    # the playlist entries land after it, not before.
    assert window.queue == ["a.mp3", "a.mp3", "b.mp3"]
    assert window._loaded_playlist_entries == entries


def test_playlist_no_duplicates_shows_no_dialog():
    _app()
    window = DedupGuardHarness(queue=["existing.mp3"], played=[False])
    entries = [_entry("p1"), _entry("p2")]
    window._playlist_loaded("list.m3u", entries, {"total": 2, "missing": 0}, 5.0)
    assert window.queue == ["existing.mp3", "p1", "p2"]
    assert window._loaded_playlist_entries == entries


def test_playlist_cancel_leaves_queue_and_loaded_entries_untouched():
    _app()
    window = DedupGuardHarness(queue=["a.mp3"], played=[False])
    entries = [_entry("a.mp3")]
    window._confirm_single_duplicate_add = lambda: "cancel"
    window._playlist_loaded("list.m3u", entries, {"total": 1, "missing": 0}, 5.0)
    assert window.queue == ["a.mp3"]
    assert window._loaded_playlist_entries == []
    assert window._loaded_playlist_filename is None


# -- freeze fix: batch adds must never trigger a synchronous GUI-thread
# metadata read (an album's worth of uncached tracks previously froze the
# window for as long as the whole batch took to tag-parse one file at a
# time on the GUI thread) ----------------------------------------------

def test_batch_add_refreshes_with_cached_details_only():
    _app()
    window = DedupGuardHarness(queue=[])
    window._add_to_queue_with_dedup_guard(["a.mp3", "b.mp3", "c.mp3"])
    assert window.refresh_cached_details_only_calls == [True]


def test_batch_add_requests_background_analysis_for_the_new_paths():
    _app()
    window = DedupGuardHarness(queue=["already_queued.mp3"])
    window._add_to_queue_with_dedup_guard(["b.mp3", "c.mp3"])
    assert window.analysis_requested_paths == [["b.mp3", "c.mp3"]]


def test_playlist_add_requests_analysis_for_resolved_paths_not_entries():
    # final_items for a playlist load are PlaylistEntry objects, not plain
    # path strings -- the analysis request must go through the same
    # path_of() extractor used for the dedup comparison, not assume strings.
    _app()
    window = DedupGuardHarness(queue=[])
    entries = [_entry("p1.mp3"), _entry("p2.mp3")]
    window._playlist_loaded("list.m3u", entries, {"total": 2, "missing": 0}, 5.0)
    assert window.analysis_requested_paths == [["p1.mp3", "p2.mp3"]]


def test_cancelled_batch_add_requests_no_analysis():
    _app()
    window = DedupGuardHarness(queue=["a.mp3"], played=[False])
    window._confirm_single_duplicate_add = lambda: "cancel"
    window._add_to_queue_with_dedup_guard(["a.mp3"])
    assert window.analysis_requested_paths == []


def test_play_album_and_artist_next_also_avoid_synchronous_reads():
    # "Play Album Next" / "Play Artist Next" insert via
    # _insert_unplayed_queue_item directly (not the dedup-guard funnel), so
    # they need their own cached_details_only=True + background-analysis
    # follow-up -- checked by source inspection since the menu is built from
    # a live QTreeWidgetItem selection, not easily driven in isolation.
    # Both the Plex-sourced and Local album/artist context menus in
    # _show_tree_menu share this logic via _handle_album_group_queue_action
    # (Plex Stage 2), so that's what's inspected here, not _show_tree_menu
    # itself.
    import inspect

    source = inspect.getsource(PlayerWindow._handle_album_group_queue_action)
    assert 'self._refresh_queue_list(cached_details_only=True, reason="tracks_added")' in source
    assert source.count("self._request_queue_analysis_for_paths(") == 2


# -- _request_queue_analysis_for_paths: cache-first lookup ------------------

class _AnalysisRecorder:
    def __init__(self):
        self.details_calls = []
        self.requested = []

    def _queue_track_details(self, path, cached_details_only=False):
        self.details_calls.append((path, cached_details_only))
        return {"bitrate": "--", "time": "--", "key": "--", "bpm": "--"}

    def _request_queue_analysis(self, path, details):
        self.requested.append((path, details))


def test_request_queue_analysis_for_paths_uses_cache_when_present():
    from types import SimpleNamespace

    recorder = _AnalysisRecorder()
    window = SimpleNamespace(
        queue_detail_cache={"cached.mp3": {"time": "1:00", "bitrate": "128k", "key": "Cm", "bpm": "120"}},
        _queue_track_details=recorder._queue_track_details,
        _request_queue_analysis=recorder._request_queue_analysis,
        _queue_priority_paths=lambda: ["cached.mp3"],
    )
    PlayerWindow._request_queue_analysis_for_paths(window, ["cached.mp3"])
    assert recorder.details_calls == []  # cache hit -- no lookup needed
    assert recorder.requested == [
        ("cached.mp3", {"time": "1:00", "bitrate": "128k", "key": "Cm", "bpm": "120"})
    ]


def test_request_queue_analysis_for_paths_falls_back_to_cached_details_only_lookup():
    from types import SimpleNamespace

    recorder = _AnalysisRecorder()
    window = SimpleNamespace(
        queue_detail_cache={},
        _queue_track_details=recorder._queue_track_details,
        _request_queue_analysis=recorder._request_queue_analysis,
        _queue_priority_paths=lambda: ["new.mp3"],
    )
    PlayerWindow._request_queue_analysis_for_paths(window, ["new.mp3"])
    assert recorder.details_calls == [("new.mp3", True)]  # never a synchronous read
    assert recorder.requested == [
        ("new.mp3", {"bitrate": "--", "time": "--", "key": "--", "bpm": "--"})
    ]


def test_request_queue_analysis_for_paths_skips_falsy_paths():
    from types import SimpleNamespace

    recorder = _AnalysisRecorder()
    window = SimpleNamespace(
        queue_detail_cache={},
        _queue_track_details=recorder._queue_track_details,
        _request_queue_analysis=recorder._request_queue_analysis,
        _queue_priority_paths=lambda: ["real.mp3"],
    )
    PlayerWindow._request_queue_analysis_for_paths(window, [None, "", "real.mp3"])
    assert recorder.details_calls == [("real.mp3", True)]
    assert [path for path, _ in recorder.requested] == ["real.mp3"]
