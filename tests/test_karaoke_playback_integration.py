"""Karaoke playback-integration behaviour: mixed Up Next queue ordering,
pause/resume/seek CDG sync, exactly-once completion, manual Stop not
advancing, Cast forcing local output, Party Mode reusing the single
backend/document, Mini Player staying operational, and clean shutdown
mid-ZIP-preparation or mid-playback."""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtGui, QtWidgets

from billsmusic.media_type import MediaType
from billsmusic.window import PlayerWindow
from billsmusic.worker_registry import WorkerLifetimeRegistry

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


# -- 9. mixed Up Next queue -------------------------------------------------

class _PlayDirectSpy:
    def __init__(self, result=True):
        self.calls = []
        self.result = result

    def __call__(self, path, crossfade=False, index=None, immediate_crossfade=False):
        self.calls.append(path)
        return self.result


def test_next_track_advances_through_a_mixed_music_karaoke_video_queue():
    spy = _PlayDirectSpy()
    window = SimpleNamespace(
        track_transition_mode="crossfade",
        current_path="song1.mp3",
        queue=["song1.mp3", "duet.cdg", "clip.mp4", "show.zip", "song2.mp3"],
        queue_played=[True, False, False, False, False],
        queue_playlist_entries=[None, None, None, None, None],
        _current_media_type=MediaType.AUDIO,
        fade_active=False,
        prebuffer_active=False,
        sleep_timer=SimpleNamespace(is_stop_after_track=False),
        _audio_log=lambda message: None,
        _audio_name=lambda path: path,
        _play_path_direct=spy,
        _record_track_completion=lambda reason: True,
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None),
        statusBar=lambda: SimpleNamespace(showMessage=lambda *a, **kw: None),
        _mixed_transition_state="idle",
        # This fixture never actually updates _current_media_type between
        # iterations (it's a stub, not a running app), so it can't
        # represent v1.0.71's Audio<->Video mixed transitions faithfully --
        # this test's own purpose is queue-advancement mechanics across
        # mixed media types, already covered without that lifecycle. See
        # test_mixed_media_audio_video_transitions.py for the real thing.
        _mixed_media_transition_eligible=lambda path: None,
    )
    window._crossfade_eligible_for_transition = (
        lambda path: PlayerWindow._crossfade_eligible_for_transition(window, path)
    )

    def _queue_entry_is_missing(row):
        return False

    window._queue_entry_is_missing = _queue_entry_is_missing

    def _next_unplayed_queue_row():
        for i, played in enumerate(window.queue_played):
            if not played:
                return i
        return None

    window._next_unplayed_queue_row = _next_unplayed_queue_row

    def _mark_queue_row_played(row):
        window.queue_played[row] = True

    window._mark_queue_row_played = _mark_queue_row_played

    for expected_path in ("duet.cdg", "clip.mp4", "show.zip", "song2.mp3"):
        PlayerWindow._next_track(window, "manual-next")

    assert spy.calls == ["duet.cdg", "clip.mp4", "show.zip", "song2.mp3"]
    # Duplicate/adjacent media-type transitions all still route through the
    # normal (non-crossfade) branch -- covered in detail elsewhere, this
    # just confirms mixed-type advancement itself works end to end.


# -- 11/12. pause/resume/seek CDG sync (via the extracted _sync_karaoke_position) --

def _karaoke_sync_window(position_seconds, use_builtin=True):
    karaoke_widget = MagicMock()
    party_mode = SimpleNamespace(isVisible=lambda: False, set_karaoke_position=MagicMock())
    window = SimpleNamespace(
        _current_media_type=MediaType.KARAOKE,
        _karaoke_document=object(),
        karaoke_widget=karaoke_widget,
        party_mode=party_mode,
        _use_builtin_player=lambda: use_builtin,
        simple_player=SimpleNamespace(get_pos=lambda: position_seconds),
        active_player=None,
    )
    return window, karaoke_widget, party_mode


def test_position_sync_reflects_backend_position_while_playing():
    window, karaoke_widget, _party = _karaoke_sync_window(4.2)
    PlayerWindow._sync_karaoke_position(window)
    karaoke_widget.set_position.assert_called_once_with(4200)


def test_position_sync_freezes_when_backend_position_is_unchanged_paused():
    # Pausing stops the backend's own position from advancing -- there is
    # no separate karaoke pause flag, the picture freezes because the
    # value fed in doesn't change.
    window, karaoke_widget, _party = _karaoke_sync_window(4.2)
    PlayerWindow._sync_karaoke_position(window)
    PlayerWindow._sync_karaoke_position(window)
    assert [call.args[0] for call in karaoke_widget.set_position.call_args_list] == [4200, 4200]


def test_position_sync_after_seek_reflects_new_backend_position():
    window, karaoke_widget, _party = _karaoke_sync_window(4.2)
    PlayerWindow._sync_karaoke_position(window)
    window.simple_player = SimpleNamespace(get_pos=lambda: 90.0)  # user seeked far forward
    PlayerWindow._sync_karaoke_position(window)
    assert [call.args[0] for call in karaoke_widget.set_position.call_args_list] == [4200, 90000]


def test_position_sync_no_op_when_not_karaoke():
    window, karaoke_widget, _party = _karaoke_sync_window(4.2)
    window._current_media_type = MediaType.AUDIO
    PlayerWindow._sync_karaoke_position(window)
    karaoke_widget.set_position.assert_not_called()


def test_position_sync_also_updates_visible_party_mode():
    window, karaoke_widget, party_mode = _karaoke_sync_window(1.0)
    party_mode.isVisible = lambda: True
    PlayerWindow._sync_karaoke_position(window)
    party_mode.set_karaoke_position.assert_called_once_with(1000)


# -- 13/14. exactly-once completion, manual Stop doesn't advance -----------

def _karaoke_completion_window(spy, completed):
    window = SimpleNamespace(
        track_transition_mode="crossfade",
        current_path="song.cdg",  # the stable karaoke identity, not the mp3
        queue=[],
        _playback_context_paths=["song.cdg", "next_song.mp3"],
        _current_media_type=MediaType.KARAOKE,
        fade_active=False,
        prebuffer_active=False,
        sleep_timer=SimpleNamespace(is_stop_after_track=False),
        _next_unplayed_queue_row=lambda: None,
        _audio_log=lambda message: None,
        _audio_name=lambda path: path,
        _play_path_direct=spy,
        _record_track_completion=lambda reason: completed.append(reason) or True,
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None),
        _mixed_transition_state="idle",
    )
    window._playback_fallback_paths = lambda: window._playback_context_paths
    window._crossfade_eligible_for_transition = (
        lambda path: PlayerWindow._crossfade_eligible_for_transition(window, path)
    )
    return window


def test_karaoke_backing_mp3_end_of_media_advances_exactly_once():
    # The backing MP3 plays through the normal audio backend, so it
    # completes via the same quiet-end/near-end/normal-end reasons regular
    # audio already uses -- no separate "karaoke-ended" reason needed.
    spy = _PlayDirectSpy()
    completed = []
    window = _karaoke_completion_window(spy, completed)

    PlayerWindow._next_track(window, "normal-end")

    assert completed == ["normal-end"]
    assert spy.calls == ["next_song.mp3"]


def test_manual_next_on_karaoke_does_not_double_record_completion():
    spy = _PlayDirectSpy()
    completed = []
    window = _karaoke_completion_window(spy, completed)

    PlayerWindow._next_track(window, "manual-next")

    assert completed == []
    assert spy.calls == ["next_song.mp3"]


def test_manual_stop_on_karaoke_does_not_advance_the_queue():
    next_calls = []
    party_mode = SimpleNamespace(return_to_normal_layout=lambda: None)
    window = SimpleNamespace(
        _current_media_type=MediaType.KARAOKE,
        _karaoke_generation=1,
        karaoke_widget=MagicMock(),
        _karaoke_audio_path="song.mp3",
        _karaoke_document=object(),
        party_mode=party_mode,
        _video_backend=MagicMock(),
        _detach_video_from_party_mode=lambda: None,
        _show_normal_display_page=lambda: None,
        _resume_deferred_queue_analysis=lambda: None,
        cast_active=False,
        _cancel_fade=lambda: None,
        _stop_all=lambda: None,
        _cancel_playback_watchdog=lambda: None,
        _playback_expected=True,
        _playback_intentionally_paused=True,
        beat=SimpleNamespace(setPlaying=lambda v: None),
        btn_pause=SimpleNamespace(setText=lambda t: None, setAccessibleName=lambda t: None),
        _reset_progress=lambda: None,
        _announce_accessible_status=lambda message: None,
        _current_playback_attempt=None,
    )
    window._next_track = lambda reason: next_calls.append(reason)
    window._exit_karaoke_fullscreen = lambda: None
    window._stop_video_for_audio_transition = (
        lambda: PlayerWindow._stop_video_for_audio_transition(window)
    )
    window._sync_now_playing_overlay_for_media_type = lambda: None
    window._cancel_current_playback_attempt = (
        lambda reason: PlayerWindow._cancel_current_playback_attempt(window, reason)
    )

    PlayerWindow.stop_playback(window)

    assert next_calls == []
    assert window._karaoke_generation == 2  # invalidated -- late events can't act
    assert window._current_media_type == MediaType.AUDIO


# -- 16. Cast forces local output -------------------------------------------

def test_karaoke_forces_local_output_when_cast_is_active(tmp_path):
    cdg_path = tmp_path / "song.cdg"
    cdg_path.write_bytes(b"x")
    calls = SimpleNamespace(force_local_output=0, messages=[])
    window = SimpleNamespace(
        cast_active=True,
        _current_media_type=MediaType.AUDIO,
        _karaoke_generation=0,
        _karaoke_prepare_worker=None,
        _karaoke_audio_path=None,
        _karaoke_document=None,
        karaoke_widget=MagicMock(),
        right_display_stack=MagicMock(),
        _karaoke_output_page=MagicMock(),
        party_mode=None,
        track_index_by_path={},
        diagnostics=SimpleNamespace(
            record=lambda *a, **kw: None,
            path_details=lambda path: {"path_hash": "x"},
        ),
        statusBar=lambda: SimpleNamespace(
            showMessage=lambda text, *a, **kw: calls.messages.append(text)
        ),
        _current_playback_attempt=None,
        _worker_registry=WorkerLifetimeRegistry(),
    )
    window._stop_video_for_audio_transition = lambda: None
    window._cancel_fade = lambda: None
    window._stop_all = lambda: None
    window._activate_track_ui = lambda index, path: None
    window._refresh_visualiser_lifecycle = lambda reason: None
    window._force_local_output_for_video = lambda: setattr(
        calls, "force_local_output", calls.force_local_output + 1
    )
    window._on_karaoke_prepared = lambda *a, **kw: None
    window._on_karaoke_prepare_failed = lambda *a, **kw: None
    window._release_karaoke_worker = lambda worker: None
    window._advance_playback_attempt_state = lambda attempt_id, state: None

    started_workers = []

    class _FakeWorker:
        def __init__(self, generation, path):
            started_workers.append((generation, path))

        def cancel(self):
            pass

        prepared = SimpleNamespace(connect=lambda fn: None)
        failed = SimpleNamespace(connect=lambda fn: None)
        finished = SimpleNamespace(connect=lambda fn: None)

        def start(self):
            pass

    import billsmusic.window as window_module
    original_worker_cls = window_module.KaraokePrepareWorker
    window_module.KaraokePrepareWorker = _FakeWorker
    try:
        result = PlayerWindow._play_karaoke_path_direct(window, str(cdg_path))
    finally:
        window_module.KaraokePrepareWorker = original_worker_cls

    assert result is True
    assert calls.force_local_output == 1
    assert calls.messages and "This Computer" in calls.messages[0]
    assert window._current_media_type == MediaType.KARAOKE
    assert started_workers == [(1, str(cdg_path))]


# -- 17. Party Mode reuses the single backend/document ----------------------

def test_party_mode_karaoke_reuses_the_same_document_no_second_player():
    document = object()
    show_calls = []
    party_mode = SimpleNamespace(
        isVisible=lambda: True,
        show_karaoke=lambda doc: show_calls.append(doc),
        _video_active=False,
    )
    window = SimpleNamespace(
        _current_media_type=MediaType.KARAOKE,
        _karaoke_document=document,
        party_mode=party_mode,
    )

    PlayerWindow._attach_video_to_party_mode(window)

    # The exact same document object the main window is already
    # rendering -- never a second CdgDocument built from a fresh parse,
    # never a second audio player constructed.
    assert show_calls == [document]


def test_party_mode_returns_to_normal_layout_without_restarting_karaoke():
    return_calls = []
    party_mode = SimpleNamespace(
        _video_active=True,
        return_to_normal_layout=lambda: return_calls.append(1),
    )
    window = SimpleNamespace(
        _current_media_type=MediaType.KARAOKE,
        party_mode=party_mode,
    )

    PlayerWindow._detach_video_from_party_mode(window)

    assert return_calls == [1]


# -- 18. Mini Player stays operational --------------------------------------

class _MiniPlayerOwnerHarness(QtWidgets.QMainWindow):
    """Trimmed down from test_mini_player.py's OwnerHarness -- only what
    MiniPlayerWindow's constructor and sync_from_owner() actually touch.
    Deliberately does NOT define _cached_artwork_pixmap, so _sync_artwork
    takes the same "no provider" placeholder path a real owner would for
    any track type with no cover art available."""

    _sync_mini_player = PlayerWindow._sync_mini_player
    _ensure_queue_played_flags = PlayerWindow._ensure_queue_played_flags

    def __init__(self, current_path, meta_by_path):
        super().__init__()
        self.setCentralWidget(QtWidgets.QWidget())
        self.action_previous = QtGui.QAction(self)
        self.action_play_pause_alternate = QtGui.QAction(self)
        self.action_stop = QtGui.QAction(self)
        self.action_next = QtGui.QAction(self)
        self.action_mute = QtGui.QAction(self)
        self.action_volume_up = QtGui.QAction(self)
        self.action_volume_down = QtGui.QAction(self)
        self.slider_volume = QtWidgets.QSlider()
        self.slider_volume.setRange(0, 100)
        self.slider_volume.setValue(70)
        self.slider_progress = QtWidgets.QSlider()
        self.slider_progress.setRange(0, 1000)
        self.slider_progress.setValue(100)
        self.label_remaining = QtWidgets.QLabel("-2:30")
        self.scrubbing = False
        self.current_path = current_path
        self._meta_by_path = meta_by_path
        self.queue = []
        self.queue_played = []
        self.master_volume = 70
        self._muted = False
        self._playback_expected = True
        self._playback_intentionally_paused = False
        self._last_progress_length_ms = 180000
        self.mini_player = None
        self.mini_player_geometry = {"x": 80, "y": 80, "width": 420, "height": 190}
        self.mini_player_always_on_top = False

    def _save_user_settings(self):
        pass

    def _progress_release(self):
        self.scrubbing = False


def test_mini_player_sync_reads_karaoke_track_generically():
    from billsmusic.mini_player import MiniPlayerWindow

    _app()
    owner = _MiniPlayerOwnerHarness(
        current_path="song.cdg",
        meta_by_path={
            "song.cdg": {"title": "Duet", "artist": "A & B", "album": "Karaoke"},
        },
    )
    mini = MiniPlayerWindow(owner)

    mini.sync_from_owner(force=True)

    assert mini.title_label._full_text == "Duet"
    assert mini.artist_label._full_text == "A & B"
    # No artwork available for a karaoke source -- falls back to the same
    # generic "no artwork" placeholder every other track type without
    # cover art already uses (a "♪" glyph), not a crash or blank widget.
    assert mini.artwork.text() == "♪"


# -- 20. clean shutdown mid-ZIP-preparation or mid-playback ------------------

def _karaoke_shutdown_fake_window(worker, registry):
    # Phase C2 (worker lifetime / shutdown ownership, 2026-09-11): karaoke
    # cancel/wait is no longer inline in the shutdown method -- the real
    # dispatch site (_start_karaoke_playback) registers the worker with
    # WorkerLifetimeRegistry, and _request_shutdown's only involvement is
    # its one self._worker_registry.shutdown_all() call, so this fake
    # window needs every attribute _request_shutdown touches directly
    # (not behind getattr/try-except) in order to actually reach it.
    return SimpleNamespace(
        _shutdown_requested=False,
        _closing=False,
        _shutdown_pending=False,
        _playback_generation=0,
        _plex_audio_load_token=0,
        _crossfade_load_token=0,
        _library_search_generation=0,
        _playback_recovery_active=False,
        _karaoke_generation=1,
        _karaoke_prepare_worker=worker,
        _video_backend=None,
        _worker_registry=registry,
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None),
        _cancel_current_playback_attempt=lambda reason: None,
        _cancel_playback_watchdog=lambda: None,
        _audio_log=lambda message: None,
        _cancel_pending_library_apply=lambda: None,
        _cancel_fade=lambda: None,
        _stop_all=lambda: None,
        _cancel_metadata_backfill=lambda: None,
        _mixed_transition_state="idle",
        _maybe_resume_final_shutdown=lambda: None,
    )


def test_shutdown_cancels_and_waits_for_an_in_flight_karaoke_prepare_worker():
    from billsmusic.worker_registry import WorkerLifetimeRegistry

    worker = MagicMock()
    worker.isRunning.return_value = False
    worker.wait.return_value = True
    registry = WorkerLifetimeRegistry()
    registry.register("karaoke_prepare", cancel=worker.cancel, thread=worker, wait_ms=5000)
    window = _karaoke_shutdown_fake_window(worker, registry)

    PlayerWindow._request_shutdown(window)

    worker.cancel.assert_called_once()
    worker.wait.assert_called_once_with(5000)
    # wait() positively returned True -- the registry releases ownership.
    assert registry.active_count() == 0
    # The actual _karaoke_prepare_worker nulling happens via the worker's
    # own real `finished` signal -> _release_karaoke_worker (not fired by
    # this MagicMock stand-in, unlike a real QThread) -- proven directly:
    PlayerWindow._release_karaoke_worker(window, worker, registry_token=None)
    assert window._karaoke_prepare_worker is None
    assert window._karaoke_generation == 2


def test_shutdown_tolerates_a_karaoke_worker_that_will_not_stop():
    from billsmusic.worker_registry import WorkerLifetimeRegistry

    worker = MagicMock()
    worker.isRunning.return_value = True
    worker.wait.return_value = False
    registry = WorkerLifetimeRegistry()
    registry.register("karaoke_prepare", cancel=worker.cancel, thread=worker, wait_ms=5000)
    window = _karaoke_shutdown_fake_window(worker, registry)

    PlayerWindow._request_shutdown(window)  # must not raise

    worker.cancel.assert_called_once()
    # wait() timed out (still running) -- UNKNOWN != FINISHED, so the
    # registry keeps it registered rather than dropping the final strong
    # reference to a possibly-still-live QThread, and the window's own
    # reference is left untouched too.
    assert registry.active_count() == 1
    assert window._karaoke_prepare_worker is worker
