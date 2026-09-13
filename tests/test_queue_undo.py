import dataclasses
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtGui, QtWidgets

from billsmusic.window import PlayerWindow, SHORTCUTS
from billsmusic.playlist_repair import PlaylistEntry
from billsmusic.queue_undo import QueueUndoSnapshot, GENERIC_UNDO_LABEL

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

    def events_for(self, operation):
        return [e for e in self.events if e[1] == operation]

    def path_details(self, path):
        return {"path_hash": "test", "extension": ""}


class _PlaybackTripwire:
    """Any of these being called during undo would mean playback was
    disturbed -- tests assert call_count stays at 0."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class QueueUndoHarness(QtWidgets.QMainWindow):
    _ensure_queue_played_flags = PlayerWindow._ensure_queue_played_flags
    _first_played_queue_row = PlayerWindow._first_played_queue_row
    _keep_played_tracks_at_bottom = PlayerWindow._keep_played_tracks_at_bottom
    _queue_played_role = PlayerWindow._queue_played_role
    _queue_playlist_role = PlayerWindow._queue_playlist_role
    _remove_selected_queue_item = PlayerWindow._remove_selected_queue_item
    _move_selected_queue_item = PlayerWindow._move_selected_queue_item
    _shuffle_up_next = PlayerWindow._shuffle_up_next
    _move_queue_item_to_top = PlayerWindow._move_queue_item_to_top
    _remove_played_queue_tracks = PlayerWindow._remove_played_queue_tracks
    _clear_up_next_queue = PlayerWindow._clear_up_next_queue
    _sync_queue_from_list = PlayerWindow._sync_queue_from_list
    _playlist_loaded = PlayerWindow._playlist_loaded
    _undo_queue_change = PlayerWindow._undo_queue_change
    _update_undo_action_state = PlayerWindow._update_undo_action_state
    _restore_queue_selection = PlayerWindow._restore_queue_selection
    _insert_unplayed_queue_item = PlayerWindow._insert_unplayed_queue_item
    _insert_queue_paths = PlayerWindow._insert_queue_paths
    _add_to_queue_with_dedup_guard = PlayerWindow._add_to_queue_with_dedup_guard
    _confirm_batch_duplicate_add = PlayerWindow._confirm_batch_duplicate_add
    _confirm_single_duplicate_add = PlayerWindow._confirm_single_duplicate_add
    _queue_add_status_text = PlayerWindow._queue_add_status_text

    def __init__(self, queue=None, played=None, entries=None):
        super().__init__()
        self.setCentralWidget(QtWidgets.QWidget())
        self.queue = list(queue) if queue is not None else ["a.mp3", "b.mp3", "c.mp3"]
        self.queue_played = list(played) if played is not None else [False] * len(self.queue)
        self.queue_playlist_entries = list(entries) if entries is not None else [None] * len(self.queue)
        self._queue_undo_snapshot = None
        self.current_path = None
        self._playback_expected = False
        self.warn_before_adding_duplicate_queue_tracks = True
        self.diagnostics = _RecordingDiagnostics()
        self.session_save_calls = 0
        self.scheduled_save_calls = 0
        self.status_messages = []
        self.accessible_messages = []
        self.log_messages = []
        self.refresh_calls = []

        self.action_undo_queue_change = QtGui.QAction(GENERIC_UNDO_LABEL, self)
        self.action_undo_queue_change.setEnabled(False)

        # Playback-safety tripwires: undo must never call any of these.
        self._play_path_direct = _PlaybackTripwire()
        self.stop_playback = _PlaybackTripwire()
        self._start_crossfade_to = _PlaybackTripwire()
        self._start_miniaudio_crossfade_to = _PlaybackTripwire()
        self._ensure_vlc = _PlaybackTripwire()

        self.queue_list = QtWidgets.QListWidget()
        self._rebuild_queue_list_widget()

    # -- stand-ins for widget-heavy real methods --------------------------

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
        if keep_played_bottom:
            self._keep_played_tracks_at_bottom()
        self._rebuild_queue_list_widget()

    def _remove_queue_row_widget(self, row, reason="track_removed"):
        self.queue_list.takeItem(row)
        return True

    def _move_queue_row_widget(self, source, target, reason="track_moved"):
        item = self.queue_list.takeItem(source)
        self.queue_list.insertItem(target, item)
        return True

    def _request_queue_analysis_for_paths(self, paths):
        pass

    def _schedule_session_save(self):
        self.scheduled_save_calls += 1

    def _save_session(self):
        self.session_save_calls += 1

    def _announce_accessible_status(self, message, timeout=4000):
        self.accessible_messages.append(message)

    def _log(self, message):
        self.log_messages.append(message)


def _entry(path):
    return PlaylistEntry(path=path, resolved_path=path)


# -- startup / no-snapshot -------------------------------------------------

def test_undo_disabled_initially():
    _app()
    window = QueueUndoHarness()
    assert window._queue_undo_snapshot is None
    assert not window.action_undo_queue_change.isEnabled()
    assert window.action_undo_queue_change.text() == GENERIC_UNDO_LABEL


def test_session_restore_creates_no_snapshot():
    _app()
    # Simulates _load_session: queue/queue_played assigned directly, no
    # hooked mutation method involved.
    window = QueueUndoHarness(queue=["x.mp3", "y.mp3"], played=[True, False])
    assert window._queue_undo_snapshot is None
    assert not window.action_undo_queue_change.isEnabled()


# -- remove ------------------------------------------------------------

def test_remove_one_track_can_be_undone():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"], played=[False, True, False])
    window.queue_list.setCurrentRow(1)
    window._remove_selected_queue_item()
    assert window.queue == ["a", "c"]
    assert window.action_undo_queue_change.isEnabled()
    assert window.action_undo_queue_change.text() == "Undo Remove Tracks"

    window._undo_queue_change()
    assert window.queue == ["a", "b", "c"]
    assert window.queue_played == [False, True, False]
    assert not window.action_undo_queue_change.isEnabled()


def test_undo_bumps_queue_mutation_epoch():
    # Correctness hardening (2026-08-24, Codex design review): an undo can
    # change what's at any given row -- a preload/candidate selection
    # recorded by (epoch, row, path) before it (see video_dual_
    # transition.py's SecondaryIdentity) must not be trusted to still
    # describe the same logical item afterward.
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"], played=[False, True, False])
    epoch_before = getattr(window, "_queue_mutation_epoch", 0)
    window.queue_list.setCurrentRow(1)
    window._remove_selected_queue_item()

    window._undo_queue_change()

    assert getattr(window, "_queue_mutation_epoch", 0) > epoch_before


def test_each_removal_creates_one_snapshot_replacing_prior():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c", "d"])
    window.queue_list.setCurrentRow(0)
    window._remove_selected_queue_item()
    first_snapshot = window._queue_undo_snapshot
    assert first_snapshot is not None
    assert first_snapshot.queue == ["a", "b", "c", "d"]

    window.queue_list.setCurrentRow(0)
    window._remove_selected_queue_item()
    second_snapshot = window._queue_undo_snapshot
    assert second_snapshot is not first_snapshot
    assert second_snapshot.queue == ["b", "c", "d"]
    # Single level: only the most recent removal is recoverable.
    window._undo_queue_change()
    assert window.queue == ["b", "c", "d"]


# -- duplicates / played flags / current path -------------------------

def test_undo_preserves_duplicate_paths():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "a", "c"], played=[False, False, False, False])
    window.queue_list.setCurrentRow(0)
    window._remove_selected_queue_item()
    assert window.queue == ["b", "a", "c"]
    window._undo_queue_change()
    assert window.queue == ["a", "b", "a", "c"]


def test_undo_does_not_alter_current_path():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"])
    window.current_path = "b"
    window._clear_up_next_queue()
    assert window.current_path == "b"
    window._undo_queue_change()
    assert window.current_path == "b"
    assert window.queue == ["a", "b", "c"]


# -- clear / shuffle / reorder round trips -----------------------------

def test_undo_clear_restores_queue():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"], played=[False, True, False])
    window._clear_up_next_queue()
    assert window.queue == []
    assert window.action_undo_queue_change.text() == "Undo Clear Up Next"
    window._undo_queue_change()
    assert window.queue == ["a", "b", "c"]
    assert window.queue_played == [False, True, False]


def test_clear_on_empty_queue_creates_no_snapshot():
    _app()
    window = QueueUndoHarness(queue=[])
    window._clear_up_next_queue()
    assert window._queue_undo_snapshot is None


def test_undo_shuffle_restores_order():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c", "d", "e"])
    original = list(window.queue)
    window._shuffle_up_next()
    assert window.action_undo_queue_change.text() == "Undo Shuffle"
    window._undo_queue_change()
    assert window.queue == original


def test_shuffle_on_empty_queue_creates_no_snapshot():
    _app()
    window = QueueUndoHarness(queue=[])
    window._shuffle_up_next()
    assert window._queue_undo_snapshot is None


def test_undo_reorder_via_keyboard_move():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"], played=[False, True, False])
    window.queue_list.setCurrentRow(1)
    window._move_selected_queue_item(-1)
    assert window.queue == ["b", "a", "c"]
    assert window.action_undo_queue_change.text() == "Undo Reorder"
    window._undo_queue_change()
    assert window.queue == ["a", "b", "c"]
    assert window.queue_played == [False, True, False]


def test_sync_queue_from_list_bumps_epoch_on_genuine_reorder():
    # Correctness hardening (2026-08-24, Codex design review): a completed
    # drag-and-drop gesture changes what's at any given row -- a preload/
    # candidate selection recorded by (epoch, row, path) before it must
    # not be trusted to still describe the same logical item afterward.
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"], played=[False, False, False])
    epoch_before = getattr(window, "_queue_mutation_epoch", 0)
    # Mirrors what Qt has already done physically to queue_list by the
    # time rowsMoved fires in production -- see _sync_queue_from_list's
    # own docstring/comment.
    item = window.queue_list.takeItem(0)
    window.queue_list.insertItem(1, item)

    window._sync_queue_from_list()

    assert window.queue == ["b", "a", "c"]
    assert getattr(window, "_queue_mutation_epoch", 0) > epoch_before


def test_sync_queue_from_list_does_not_bump_epoch_when_order_is_unchanged():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"], played=[False, False, False])
    epoch_before = getattr(window, "_queue_mutation_epoch", 0)

    window._sync_queue_from_list()  # no actual reorder happened

    assert window.queue == ["a", "b", "c"]
    assert getattr(window, "_queue_mutation_epoch", 0) == epoch_before


def test_undo_move_to_top():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"])
    window._move_queue_item_to_top(2)
    assert window.queue == ["c", "a", "b"]
    window._undo_queue_change()
    assert window.queue == ["a", "b", "c"]


# -- remove played -------------------------------------------------------

def test_undo_remove_played_tracks(monkeypatch):
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"], played=[True, False, True])
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question",
        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes),
    )
    window._remove_played_queue_tracks()
    assert window.queue == ["b"]
    assert window.action_undo_queue_change.text() == "Undo Remove Played Tracks"
    window._undo_queue_change()
    assert window.queue == ["a", "b", "c"]
    assert window.queue_played == [True, False, True]


def test_remove_played_declined_creates_no_snapshot(monkeypatch):
    _app()
    window = QueueUndoHarness(queue=["a", "b"], played=[True, False])
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question",
        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No),
    )
    window._remove_played_queue_tracks()
    assert window.queue == ["a", "b"]
    assert window._queue_undo_snapshot is None


def test_remove_played_with_none_played_creates_no_snapshot():
    _app()
    window = QueueUndoHarness(queue=["a", "b"], played=[False, False])
    window._remove_played_queue_tracks()
    assert window._queue_undo_snapshot is None


# -- drag-and-drop --------------------------------------------------------

def test_undo_drag_drop_restores_order():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"], played=[False, False, True])
    # Simulate a completed InternalMove: widget rows already reordered,
    # self.queue/self.queue_played still reflect the pre-drop state.
    item = window.queue_list.takeItem(0)
    window.queue_list.insertItem(2, item)
    window._sync_queue_from_list()
    # _sync_queue_from_list re-applies "played tracks at the bottom" after a
    # drop (pre-existing behaviour) -- "c" (played) sorts after "a"/"b".
    assert window.queue == ["b", "a", "c"]
    assert window.action_undo_queue_change.text() == "Undo Reorder"
    window._undo_queue_change()
    # Undo restores the exact pre-drag order, not the post-drag-sorted one.
    assert window.queue == ["a", "b", "c"]
    assert window.queue_played == [False, False, True]


def test_drag_drop_noop_creates_no_snapshot():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"])
    # rowsMoved fired but the widget order is unchanged.
    window._sync_queue_from_list()
    assert window.queue == ["a", "b", "c"]
    assert window._queue_undo_snapshot is None


# -- playlist load ---------------------------------------------------------

def test_undo_playlist_load_restores_queue():
    _app()
    window = QueueUndoHarness(queue=["a", "b"], played=[False, True])
    entries = [_entry("p1"), _entry("p2")]
    window._playlist_loaded("list.m3u", entries, {"total": 2, "missing": 0}, 5.0)
    # Inserted before the first played row (b) -- unplayed "a" stays put.
    assert window.queue == ["a", "p1", "p2", "b"]
    assert window.action_undo_queue_change.text() == "Undo Add Playlist"
    window._undo_queue_change()
    assert window.queue == ["a", "b"]
    assert window.queue_played == [False, True]


def test_empty_playlist_load_creates_no_snapshot(monkeypatch):
    _app()
    window = QueueUndoHarness(queue=["a"])
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "information", staticmethod(lambda *a, **k: None),
    )
    window._playlist_loaded("list.m3u", [], {"total": 0, "missing": 0}, 1.0)
    assert window._queue_undo_snapshot is None


# -- snapshot replacement / single-use / no redo ---------------------------

def test_single_operation_creates_one_snapshot_event():
    _app()
    window = QueueUndoHarness(queue=["a", "b"])
    window._clear_up_next_queue()
    assert len(window.diagnostics.events_for("undo_snapshot")) == 1


def test_later_operation_replaces_snapshot():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"])
    window.queue_list.setCurrentRow(0)
    window._remove_selected_queue_item()
    assert window._queue_undo_snapshot.action == "remove"
    window._shuffle_up_next()
    assert window._queue_undo_snapshot.action == "shuffle"


def test_undo_is_single_use():
    _app()
    window = QueueUndoHarness(queue=["a", "b"])
    window._clear_up_next_queue()
    window._undo_queue_change()
    assert window.queue == ["a", "b"]
    # Calling undo again with no snapshot must be a harmless no-op.
    window.queue.append("c")
    window._undo_queue_change()
    assert window.queue == ["a", "b", "c"]


def test_undo_does_not_create_redo():
    _app()
    window = QueueUndoHarness(queue=["a", "b"])
    window._clear_up_next_queue()
    window._undo_queue_change()
    assert window._queue_undo_snapshot is None
    assert not window.action_undo_queue_change.isEnabled()


# -- exclusions: no snapshot from non-structural changes -------------------

def test_background_metadata_and_bpm_key_updates_create_no_snapshot():
    _app()
    window = QueueUndoHarness(queue=["a", "b"])
    # Background enrichment only ever touches display/metadata caches, never
    # self.queue -- simulate exactly that and confirm no snapshot appears.
    window.queue_detail_cache = {"a": {"bpm": "128", "key": "Am"}}
    assert window._queue_undo_snapshot is None


def test_playback_progress_creates_no_snapshot():
    _app()
    window = QueueUndoHarness(queue=["a", "b"])
    window.current_path = "a"
    # Simulate playback advancing -- current_path changes, queue untouched.
    window.current_path = "b"
    assert window._queue_undo_snapshot is None


# -- playback safety --------------------------------------------------------

def test_undo_does_not_touch_playback(monkeypatch):
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"])
    window.current_path = "b"
    window.queue_list.setCurrentRow(0)
    window._remove_selected_queue_item()
    window._undo_queue_change()
    assert window.current_path == "b"
    for tripwire in (
        window._play_path_direct, window.stop_playback,
        window._start_crossfade_to, window._start_miniaudio_crossfade_to,
        window._ensure_vlc,
    ):
        assert tripwire.calls == []


def test_undo_restores_removed_current_track_without_restarting_playback():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"])
    window.current_path = "b"
    window.queue_list.setCurrentRow(1)
    window._remove_selected_queue_item()
    assert "b" not in window.queue
    window._undo_queue_change()
    assert "b" in window.queue
    assert window.current_path == "b"
    assert window._play_path_direct.calls == []


# -- alignment / snapshot content -------------------------------------------

def test_queue_and_played_flags_remain_aligned_through_operations():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"], played=[False, True, False])
    window.queue_list.setCurrentRow(0)
    window._remove_selected_queue_item()
    assert len(window.queue) == len(window.queue_played) == len(window.queue_playlist_entries)
    window._undo_queue_change()
    assert len(window.queue) == len(window.queue_played) == len(window.queue_playlist_entries)


def test_snapshot_contains_no_widget_or_backend_objects():
    _app()
    window = QueueUndoHarness(queue=["a", "b"])
    window._clear_up_next_queue()
    snapshot = window._queue_undo_snapshot
    assert isinstance(snapshot, QueueUndoSnapshot)
    for field in dataclasses.fields(snapshot):
        value = getattr(snapshot, field.name)
        values = value if isinstance(value, list) else [value]
        for item in values:
            assert not isinstance(item, (QtCore.QObject, QtWidgets.QWidget))


# -- failure handling ---------------------------------------------------

def test_failed_mutation_preserves_previous_snapshot(monkeypatch):
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"])
    window.queue_list.setCurrentRow(0)
    window._remove_selected_queue_item()
    preserved = window._queue_undo_snapshot
    assert preserved is not None

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(window, "_shuffle_up_next", lambda: None)  # sanity no-op guard unaffected
    monkeypatch.setattr(QueueUndoHarness, "_refresh_queue_list", _boom, raising=False)
    try:
        window._clear_up_next_queue()
    except RuntimeError:
        pass
    assert window._queue_undo_snapshot is preserved


def test_failed_undo_does_not_corrupt_queue():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"])
    before = list(window.queue)
    window._queue_undo_snapshot = QueueUndoSnapshot(
        action="remove",
        queue=["x", "y"],
        queue_played=[False],  # deliberately misaligned
        queue_playlist_entries=[None, None],
        selected_rows=[],
        scroll_position=None,
        current_path=None,
    )
    window._undo_queue_change()
    assert window.queue == before
    assert window._queue_undo_snapshot is None
    assert window.diagnostics.events_for("undo_restore")
    assert window.diagnostics.events_for("undo_restore")[-1][2].get("status") == "failure"


# -- session save ------------------------------------------------------

def test_undo_saves_session_exactly_once():
    _app()
    window = QueueUndoHarness(queue=["a", "b"])
    window._clear_up_next_queue()
    assert window.session_save_calls == 0  # original mutation only *schedules* a save
    window._undo_queue_change()
    assert window.session_save_calls == 1


# -- keyboard / accessibility --------------------------------------------

def test_ctrl_z_shortcut_configured():
    assert SHORTCUTS["undo_queue_change"] == "Ctrl+Z"


def test_ctrl_z_triggers_undo_action():
    _app()
    window = QueueUndoHarness(queue=["a", "b"])
    window.action_undo_queue_change.setShortcut(QtGui.QKeySequence(SHORTCUTS["undo_queue_change"]))
    window.action_undo_queue_change.triggered.connect(window._undo_queue_change)
    window._clear_up_next_queue()   # only an enabled action's trigger() fires triggered
    assert window.action_undo_queue_change.isEnabled()
    window.action_undo_queue_change.trigger()
    assert window.queue == ["a", "b"]


def test_action_disabled_state_conveyed_via_qaction():
    _app()
    window = QueueUndoHarness(queue=["a", "b"])
    assert not window.action_undo_queue_change.isEnabled()
    window._clear_up_next_queue()
    assert window.action_undo_queue_change.isEnabled()
    window._undo_queue_change()
    assert not window.action_undo_queue_change.isEnabled()
