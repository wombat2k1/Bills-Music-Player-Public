"""Astra F11: a Cast completion must only act for the request, and the
PlaybackAttempt, that created it.

CastPlaybackController's `loaded`/`failed` signals carry no request identity,
and the controller's own generation check runs on its worker thread BEFORE the
queued signal reaches the GUI. A completion already emitted when the user
presses Stop, returns to local output, selects another track or starts another
Cast request was then delivered to _on_cast_loaded/_on_cast_failed, which
acted for whatever PlaybackAttempt happened to be current.

These tests drive the real controller threads and queued Qt delivery with a
fake Chromecast whose receiver response is held per request, plus the real
window handlers, local playback, attempt lifecycle and queue commit.
"""
import os
import threading
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets

import billsmusic.window as window_module
import billsmusic.workers as workers_module
from billsmusic.cast_service import CastDevice, CastPlaybackController
from billsmusic.media_type import MediaType
from billsmusic.playback_attempt import PlaybackAttemptState
from billsmusic.window import PlayerWindow
from billsmusic.worker_registry import WorkerLifetimeRegistry

_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _spin(predicate=lambda: False, timeout=0.3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QtCore.QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    QtCore.QCoreApplication.processEvents()
    return predicate()


class _GatedMedia:
    """Receiver side of one Cast device. Every play_media() is held in
    block_until_active() until the test releases that load, and can be told
    to fail instead."""

    def __init__(self):
        self.loads = []
        self._gates = {}
        self._failures = set()
        self._by_thread = {}
        self.stop_calls = 0
        self.status = SimpleNamespace(player_state="PLAYING", current_time=3.0, duration=180.0)

    def play_media(self, url, content_type, **kwargs):
        index = len(self.loads)
        self.loads.append(url)
        self._gates[index] = threading.Event()
        self._by_thread[threading.get_ident()] = index

    def block_until_active(self, timeout):
        index = self._by_thread[threading.get_ident()]
        self._gates[index].wait(5)
        if index in self._failures:
            raise TimeoutError("receiver did not become active")

    def release(self, index, fail=False):
        if fail:
            self._failures.add(index)
        self._gates[index].set()

    def play(self):
        pass

    def pause(self):
        pass

    def stop(self):
        self.stop_calls += 1


class _Cast:
    uuid = "cast-1"
    name = "Living Room"
    cast_type = "audio"

    def __init__(self):
        self.media_controller = _GatedMedia()
        self.socket_client = SimpleNamespace(ident=None)
        self.cast_info = SimpleNamespace(host="10.0.0.5", port=8009, uuid=self.uuid,
                                         model_name="Chromecast", friendly_name=self.name)

    def wait(self, timeout):
        self.socket_client.ident = 1

    def set_volume(self, value):
        pass

    def disconnect(self, timeout=0):
        pass


class _Player:
    def __init__(self, physical_id):
        self.physical_id = physical_id
        self.playing = False
        self._paused = False
        self.loaded_path = None
        self.stop_calls = 0

    def load(self, path):
        self.loaded_path = path

    def play(self):
        self.playing = self.loaded_path is not None
        self._paused = False

    def pause(self):
        self.playing, self._paused = False, True

    def resume(self):
        self.playing, self._paused = True, False

    def stop(self):
        self.stop_calls += 1
        self.playing, self._paused = False, False

    def is_playing(self):
        return self.playing

    def set_volume(self, value):
        pass

    def seek(self, seconds):
        pass

    def get_pos(self):
        return 0.0

    def get_length(self):
        return 180.0

    def stats(self):
        return {"duration": 180.0, "sample_rate": 44100, "channels": 2}


class _Diagnostics:
    def __init__(self):
        self.events = []
        self.counters = {"tracks_completed": 0}

    def record(self, category, operation, **kw):
        self.events.append((category, operation, kw.get("details") or {}))

    def path_details(self, path):
        return {"path": os.path.basename(str(path))}


class _HarnessWindow(SimpleNamespace):
    """A SimpleNamespace subclass is weak-referenceable, which PyQt requires
    of the owner of a bound-method slot."""


BOUND_METHODS = (
    "_begin_cast_switch", "_on_cast_connected", "_request_cast_load", "_on_cast_loaded",
    "_on_cast_failed", "_cast_play_path", "_return_to_local_output", "stop_playback",
    "_cast_payload_with_fresh_artwork", "_local_output_snapshot", "_pause_local_for_cast",
    "_resume_local_snapshot", "_built_in_players", "_play_path_direct", "_play_simple",
    "_stop_all", "_set_player_topology", "_backend_label", "_use_builtin_player",
    "_use_bass_backend", "_begin_playback_attempt", "_cancel_current_playback_attempt",
    "_advance_playback_attempt_state", "_arm_playback_watchdog", "_cancel_playback_watchdog",
    "_stop_video_for_audio_transition", "_ensure_queue_played_flags", "_mark_queue_row_played",
    "_move_queue_row_to_bottom", "_on_output_selected",
)


def _window(monkeypatch, tmp_path, names=("a.mp3", "b.mp3", "c.mp3")):
    monkeypatch.setattr(window_module, "VLC_AVAILABLE", False)
    paths = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"x")
        paths.append(str(path))
    active, inactive = _Player("bass-A"), _Player("bass-B")
    cast = _Cast()
    controller = CastPlaybackController()
    window = _HarnessWindow(
        queue=list(paths), queue_played=[False] * len(paths), queue_playlist_entries=[None] * len(paths),
        _queue_mutation_epoch=0, _queue_entry_claims={}, _next_queue_entry_token=1,
        _next_queue_selection_id=1, _next_playback_attempt_id=1, _current_playback_attempt=None,
        bass_player=active, bass_inactive_player=inactive, miniaudio_player=None,
        miniaudio_inactive_player=None, simple_player=active, simple_inactive_player=inactive,
        active_player=None, inactive_player=None, _player_topology_epoch=0,
        builtin_backend="bass", use_simple=True, _simple_fallback_active=False,
        _temporary_backend_override=None, auto_playback_recovery=False,
        _playback_recovery_active=False, _playback_recovery_attempts={}, _playback_generation=0,
        _playback_expected=False, _playback_intentionally_paused=False,
        _current_media_type=MediaType.AUDIO, _mixed_transition_state="idle",
        _video_transition_manager=None, _closing=False, cast_active=False,
        current_path=None, current_index=None, track_index_by_path={},
        pending_next=False, master_volume=100, _sleep_timer_gain=1.0,
        _active_normalisation_gain=1.0, diagnostics=_Diagnostics(),
        cast_controller=controller, cast_volume=50,
        cast_media_server=SimpleNamespace(
            revoke_all=lambda: None, register_audio=lambda path: f"http://cast/{os.path.basename(path)}",
            register=lambda path, content_type: "http://cast/art", shutdown=lambda: None,
        ),
        _cast_payload_generation=0, _cast_payload_cache={p: {"title": os.path.basename(p)} for p in paths},
        _cast_artwork_paths={}, _cast_pending_snapshot=None, _cast_pending_device=None,
        _cast_loss_reported=False, _cast_completion_armed=False, _cast_last_state="",
        output_combo=SimpleNamespace(setCurrentIndex=lambda index: None),
        slider_volume=SimpleNamespace(blockSignals=lambda flag: None, setValue=lambda value: None),
        statusBar=lambda: SimpleNamespace(showMessage=lambda *a, **kw: None),
        beat=SimpleNamespace(setPlaying=lambda playing: None),
        btn_pause=SimpleNamespace(setText=lambda t: None, setAccessibleName=lambda t: None),
        _log=lambda message: None, _audio_log=lambda message: None,
        _audio_name=lambda path: os.path.basename(str(path)),
        _cached_gain_for_path=lambda path, target=None: 1.0,
        _set_playing_button_state=lambda: None, _cancel_fade=lambda: None,
        _reset_progress=lambda: None, _resume_deferred_queue_analysis=lambda: None,
        _sync_now_playing_overlay_for_media_type=lambda: None,
        _announce_accessible_status=lambda message: None,
        _remove_queue_row_widget=lambda row, reason=None: None,
        _insert_queue_row_widget=lambda row, reason=None: None,
        _animate_queue_history_move=lambda row: None, _schedule_session_save=lambda: None,
        _on_cast_state=lambda *a: None, pause=lambda: None,
    )
    window._activate_track_ui = lambda path, *, library_index=None, queue_token=None: setattr(window, "current_path", path)
    for name in BOUND_METHODS:
        setattr(window, name, getattr(PlayerWindow, name).__get__(window))
    window._ensure_queue_played_flags()
    # The real signal wiring (_connect_signals' Cast part).
    if hasattr(PlayerWindow, "_connect_cast_controller_signals"):
        PlayerWindow._connect_cast_controller_signals(window)
    else:  # 4d7e850 wiring, verbatim
        controller.state_changed.connect(window._on_cast_state)
        controller.connected.connect(window._on_cast_connected)
        controller.loaded.connect(window._on_cast_loaded)
        controller.failed.connect(window._on_cast_failed)
    window.cast = cast
    window.device = CastDevice("cast-1", "Living Room", cast)
    window.paths = paths
    return window


def _play_locally(window, row):
    token = window._queue_entry_tokens[row]
    assert window._play_path_direct(window.queue[row], queue_entry_token=token) is True
    return window._current_playback_attempt


def _join_worker(window, kind):
    thread = getattr(window.cast_controller, kind)
    assert thread is not None
    thread.join(5)
    assert not thread.is_alive()


def _switch_to_cast_and_hold_load(window):
    """Local track playing -> select the Cast device -> the real connect
    completes and chains into the real Cast load, which the receiver holds."""
    local = _play_locally(window, 0)
    window._begin_cast_switch(window.device)
    _join_worker(window, "connect_thread")
    assert _spin(lambda: window.cast.media_controller.loads, timeout=3)
    return local


def _emit_load_result_but_hold_delivery(window, index, fail=False):
    """The receiver answers and the worker thread emits -- but the queued
    signal is not processed until the test spins the event loop."""
    window.cast.media_controller.release(index, fail=fail)
    _join_worker(window, "load_thread")


def _row_of(window, name):
    # A committed row is relocated to the bottom, so never address by position.
    return [os.path.basename(p) for p in window.queue].index(name)


def _committed(window, name):
    return window.queue_played[_row_of(window, name)]


def _dispatch(window, name):
    """Select a queue entry the way the real queue does: claim it under a
    selection owner, then dispatch, which transfers the claim to the attempt."""
    row = _row_of(window, name)
    token, owner = window_module._claim_queue_selection_for(window, row, reason="test")
    assert window._play_path_direct(
        window.queue[row], queue_entry_token=token, queue_selection_owner=owner,
    ) is True
    return token, window._current_playback_attempt


def _install_output_menu(window):
    """The real Output menu: a QComboBox laid out as _on_cast_devices builds
    it, wired exactly as _connect_signals wires it."""
    combo = QtWidgets.QComboBox()
    combo.addItem("This Computer", None)
    combo.addItem(window.device.name, window.device)
    combo.addItem("Refresh Cast devices…", "__refresh__")
    combo.activated.connect(window._on_output_selected)
    window.output_combo = combo
    return combo


LOCAL_ROW, CAST_ROW, REFRESH_ROW = 0, 1, 2


def _choose_output(window, row):
    """A user's pick in the Output menu: Qt moves the index, then emits activated(row)."""
    window.output_combo.setCurrentIndex(row)
    window.output_combo.activated.emit(row)


def _hold_switch_load(window):
    _join_worker(window, "connect_thread")
    assert _spin(lambda: window.cast.media_controller.loads, timeout=3)


def _record_status_messages(window):
    messages = []
    window.statusBar = lambda: SimpleNamespace(showMessage=lambda message, *a, **kw: messages.append(message))
    return messages


def _record_disconnects(monkeypatch, window):
    calls = []
    monkeypatch.setattr(window.cast_controller, "disconnect", lambda: calls.append(True))
    return calls


def _queue_state(window):
    return list(window.queue), list(window.queue_played), dict(window._queue_entry_claims)


def _cast_now_playing(window):
    """Complete a switch so Cast output is genuinely active."""
    local = _switch_to_cast_and_hold_load(window)
    _emit_load_result_but_hold_delivery(window, 0)
    _spin()
    assert window.cast_active is True
    return local


# -- stale completions have no authority -----------------------------------

def test_late_cast_success_after_stop_does_not_revive_or_take_output(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    _switch_to_cast_and_hold_load(window)
    _emit_load_result_but_hold_delivery(window, 0)

    PlayerWindow.stop_playback(window)
    stops_after_stop = window.simple_player.stop_calls
    attempt_after_stop = window._current_playback_attempt
    _spin()  # the old success is delivered now

    assert window.cast_active is False
    assert getattr(window, "_cast_current_request", None) is None  # no Cast operation keeps authority
    assert window._playback_expected is False  # playback stays stopped
    assert window.simple_player.stop_calls == stops_after_stop  # the stale callback stopped nothing
    assert window._current_playback_attempt is attempt_after_stop
    assert window.queue_played.count(True) == 1  # only the original local play committed


def test_late_cast_failure_after_stop_is_harmless(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    local = _switch_to_cast_and_hold_load(window)
    _emit_load_result_but_hold_delivery(window, 0, fail=True)

    PlayerWindow.stop_playback(window)
    player = window.simple_player
    player_state = (player.playing, player._paused)
    _spin()

    assert window.cast_active is False and window._playback_expected is False
    assert (player.playing, player._paused) == player_state  # no local "resume" after Stop
    assert local.state is PlaybackAttemptState.CANCELLED  # Stop's outcome, not rewritten


def test_late_cast_success_after_return_to_local_keeps_local_output(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    _switch_to_cast_and_hold_load(window)
    _emit_load_result_but_hold_delivery(window, 0)

    window._return_to_local_output()  # the user switches back before the result arrives
    local_attempt = window._current_playback_attempt
    assert window.simple_player.playing
    stops = window.simple_player.stop_calls
    _spin()

    assert window.cast_active is False
    assert getattr(window, "_cast_current_request", None) is None
    assert window.simple_player.playing and window.simple_player.stop_calls == stops
    assert window._current_playback_attempt is local_attempt
    assert local_attempt.state is PlaybackAttemptState.PLAYING


def test_late_cast_failure_after_return_to_local_cannot_disturb_local_playback(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    _switch_to_cast_and_hold_load(window)
    _emit_load_result_but_hold_delivery(window, 0, fail=True)

    window._return_to_local_output()
    local_attempt = window._current_playback_attempt
    _spin()

    assert local_attempt.state is PlaybackAttemptState.PLAYING  # not failed by the old request
    assert window._current_playback_attempt is local_attempt
    assert window.simple_player.playing and window.cast_active is False


def test_old_cast_success_cannot_act_for_a_superseding_selection(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    _switch_to_cast_and_hold_load(window)
    _emit_load_result_but_hold_delivery(window, 0)

    b_attempt = _play_locally(window, 1)  # the user starts B locally first
    b_id, b_state = b_attempt.attempt_id, b_attempt.state
    stops = window.simple_player.stop_calls
    _spin()

    assert window._current_playback_attempt is b_attempt
    assert (b_attempt.attempt_id, b_attempt.state) == (b_id, b_state)
    assert window.simple_player.playing and window.simple_player.stop_calls == stops
    assert window.cast_active is False


def test_old_cast_failure_cannot_fail_a_superseding_selection(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    _switch_to_cast_and_hold_load(window)
    _emit_load_result_but_hold_delivery(window, 0, fail=True)

    b_attempt = _play_locally(window, 1)
    _spin()

    assert b_attempt.state is PlaybackAttemptState.PLAYING
    assert window._current_playback_attempt is b_attempt
    assert window.simple_player.playing


def test_cast_request_1_completing_after_request_2_cannot_act_for_request_2(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    _cast_now_playing(window)

    _b_token, _b_attempt = _dispatch(window, "b.mp3")  # Cast request 1
    assert _spin(lambda: len(window.cast.media_controller.loads) == 2, timeout=3)
    _emit_load_result_but_hold_delivery(window, 1)  # request 1 answered, not yet delivered
    c_token, c_attempt = _dispatch(window, "c.mp3")  # Cast request 2
    assert _spin(lambda: len(window.cast.media_controller.loads) == 3, timeout=3)
    _spin()  # request 1's success is delivered while request 2 is still loading

    assert c_attempt.state is not PlaybackAttemptState.PLAYING
    assert window._queue_entry_claims.get(c_token) == c_attempt.attempt_id  # still reserved, not committed
    assert _committed(window, "c.mp3") is False and _committed(window, "b.mp3") is False

    _emit_load_result_but_hold_delivery(window, 2)  # request 2 answers
    _spin()
    assert c_attempt.state is PlaybackAttemptState.PLAYING
    assert _committed(window, "c.mp3") is True and _committed(window, "b.mp3") is False


# -- the current request keeps its authority ---------------------------------

def test_current_cast_switch_success_takes_output(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    local = _switch_to_cast_and_hold_load(window)
    _emit_load_result_but_hold_delivery(window, 0)
    _spin()

    assert window.cast_active is True
    assert not window.simple_player.playing  # local output stopped for Cast
    assert window._current_playback_attempt is local and local.state is PlaybackAttemptState.PLAYING


def test_current_cast_track_success_commits_its_own_queue_entry(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    _cast_now_playing(window)
    b_token, b_attempt = _dispatch(window, "b.mp3")
    assert _spin(lambda: len(window.cast.media_controller.loads) == 2, timeout=3)
    assert _committed(window, "b.mp3") is False

    _emit_load_result_but_hold_delivery(window, 1)
    _spin()

    assert b_attempt.state is PlaybackAttemptState.PLAYING
    assert _committed(window, "b.mp3") is True
    assert window._queue_entry_claims == {}
    assert window.cast_active is True


def test_current_cast_track_failure_fails_its_attempt_and_returns_output(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    _cast_now_playing(window)
    b_token, b_attempt = _dispatch(window, "b.mp3")
    assert _spin(lambda: len(window.cast.media_controller.loads) == 2, timeout=3)

    _emit_load_result_but_hold_delivery(window, 1, fail=True)
    _spin()

    assert b_attempt.state is PlaybackAttemptState.FAILED
    assert b_token not in window._queue_entry_claims
    assert _committed(window, "b.mp3") is False
    assert window.cast_active is False


# -- Phase 3.1: choosing This Computer while a Cast switch is still pending ----

def test_choosing_this_computer_revokes_a_pending_switch_whose_load_then_succeeds(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    local = _play_locally(window, 0)
    _install_output_menu(window)
    _choose_output(window, CAST_ROW)
    _hold_switch_load(window)
    request = window._cast_current_request
    assert window.cast_active is False and not window.simple_player.playing  # paused for the switch
    _emit_load_result_but_hold_delivery(window, 0)

    _choose_output(window, LOCAL_ROW)  # the user decides local output is authoritative
    stops = window.simple_player.stop_calls
    queue_before = _queue_state(window)
    _spin()  # the switch's success is delivered now

    assert window.cast_active is False
    assert not window_module._cast_request_has_authority_for(window, request, "test")
    assert window.simple_player.playing and window.simple_player.stop_calls == stops
    assert window._current_playback_attempt is local and local.state is PlaybackAttemptState.PLAYING
    assert _queue_state(window) == queue_before
    assert window._cast_pending_device is None


def test_choosing_this_computer_revokes_a_pending_switch_whose_load_then_fails(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    local = _play_locally(window, 0)
    _install_output_menu(window)
    _choose_output(window, CAST_ROW)
    _hold_switch_load(window)
    _emit_load_result_but_hold_delivery(window, 0, fail=True)

    _choose_output(window, LOCAL_ROW)
    messages = _record_status_messages(window)
    disconnects = _record_disconnects(monkeypatch, window)
    _spin()

    assert local.state is PlaybackAttemptState.PLAYING  # not failed by the abandoned switch
    assert window._current_playback_attempt is local
    assert window.simple_player.playing and window.cast_active is False
    assert disconnects == [] and not any("Cast failed" in m for m in messages)


def test_choosing_this_computer_before_the_switch_connects_starts_no_cast_load(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    local = _play_locally(window, 0)
    _install_output_menu(window)
    _choose_output(window, CAST_ROW)
    _join_worker(window, "connect_thread")  # connected, not yet delivered

    _choose_output(window, LOCAL_ROW)
    try:
        started_load = _spin(lambda: window.cast.media_controller.loads, timeout=0.5)
        assert not started_load  # the stale connection did not go on to load on Cast
        assert window.cast_active is False
        assert window.simple_player.playing
        assert window._current_playback_attempt is local and local.state is PlaybackAttemptState.PLAYING
    finally:
        for index in range(len(window.cast.media_controller.loads)):
            window.cast.media_controller.release(index)


# -- Phase 3.1: a switch with no PlaybackAttempt, then newer local playback ----

def _switch_with_no_live_attempt_and_hold_load(window):
    """Stop keeps the current track, so Cast may still be chosen -- but there
    is no live PlaybackAttempt for the switch to be bound to."""
    _play_locally(window, 0)
    PlayerWindow.stop_playback(window)
    _install_output_menu(window)
    _choose_output(window, CAST_ROW)
    request = window._cast_current_request
    assert request.attempt_id is None
    _hold_switch_load(window)
    return request


def test_attemptless_cast_switch_success_cannot_act_after_newer_local_playback(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    request = _switch_with_no_live_attempt_and_hold_load(window)
    _emit_load_result_but_hold_delivery(window, 0)

    _b_token, b_attempt = _dispatch(window, "b.mp3")  # newer user intent: play B here
    b_state = b_attempt.state
    stops = window.simple_player.stop_calls
    queue_before = _queue_state(window)
    _spin()

    assert window.cast_active is False
    assert not window_module._cast_request_has_authority_for(window, request, "test")
    assert window.simple_player.playing and window.simple_player.stop_calls == stops
    assert window._current_playback_attempt is b_attempt and b_attempt.state is b_state
    assert window._playback_expected is True
    assert _queue_state(window) == queue_before


def test_attemptless_cast_switch_failure_cannot_act_after_newer_local_playback(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    _switch_with_no_live_attempt_and_hold_load(window)
    _emit_load_result_but_hold_delivery(window, 0, fail=True)

    _b_token, b_attempt = _dispatch(window, "b.mp3")
    player_state = (window.simple_player.playing, window.simple_player._paused)
    messages = _record_status_messages(window)
    disconnects = _record_disconnects(monkeypatch, window)
    queue_before = _queue_state(window)
    _spin()

    assert b_attempt.state is PlaybackAttemptState.PLAYING
    assert window._current_playback_attempt is b_attempt
    assert disconnects == [] and not any("Cast failed" in m for m in messages)
    assert (window.simple_player.playing, window.simple_player._paused) == player_state
    assert window.cast_active is False
    assert _queue_state(window) == queue_before


# -- Phase 3.1 guards: legitimate switches are not over-invalidated ------------

def test_switch_chosen_from_the_output_menu_takes_output_when_nothing_intervenes(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    local = _play_locally(window, 0)
    _install_output_menu(window)
    _choose_output(window, CAST_ROW)
    _hold_switch_load(window)
    _emit_load_result_but_hold_delivery(window, 0)
    _spin()

    assert window.cast_active is True
    assert not window.simple_player.playing
    assert local.state is PlaybackAttemptState.PLAYING


def test_attemptless_switch_takes_output_when_nothing_intervenes(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    _switch_with_no_live_attempt_and_hold_load(window)
    _emit_load_result_but_hold_delivery(window, 0)
    _spin()

    assert window.cast_active is True


def test_refreshing_cast_devices_does_not_revoke_a_pending_switch(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    refreshes = []
    window.cast_discovery = SimpleNamespace(refresh=lambda: refreshes.append(True))
    _play_locally(window, 0)
    _install_output_menu(window)
    _choose_output(window, CAST_ROW)
    _hold_switch_load(window)
    _choose_output(window, REFRESH_ROW)  # not a choice of output
    _emit_load_result_but_hold_delivery(window, 0)
    _spin()

    assert refreshes == [True]
    assert window.cast_active is True


# -- Phase 3.2: an uncached metadata result belongs to its Cast request --------

class _MetadataGate:
    """Parks the real CastPayloadWorker thread in its tag read until released."""

    def __init__(self, monkeypatch):
        self.released = threading.Event()
        monkeypatch.setattr(workers_module, "read_track_meta", self._read)

    def _read(self, path):
        self.released.wait(5)
        return {"title": os.path.basename(path), "artist": "Artist"}


def _path_of(window, name):
    return next(p for p in window.paths if os.path.basename(p) == name)


def _uncached_metadata(monkeypatch, window, name):
    """No cached Cast payload for `name`, so _request_cast_load goes through
    the real asynchronous CastPayloadWorker."""
    window._cast_payload_cache.pop(_path_of(window, name))
    window._cast_payload_workers = []
    window._worker_registry = WorkerLifetimeRegistry()
    window._cast_artwork_temp = None
    window._ensure_cast_artwork_temp_dir = PlayerWindow._ensure_cast_artwork_temp_dir.__get__(window)
    return _MetadataGate(monkeypatch)


def _record_cast_loads(monkeypatch, window):
    """Every load_async the application initiates, with the request it carries."""
    calls = []
    original = window.cast_controller.load_async

    def load_async(url, *args, request=None, **kwargs):
        calls.append(SimpleNamespace(url=url, request=request, thread=threading.current_thread().name))
        return original(url, *args, request=request, **kwargs)

    monkeypatch.setattr(window.cast_controller, "load_async", load_async)
    return calls


def _hold_metadata_result(window, gate):
    """The switch's connection is delivered and starts the real metadata
    worker; the worker then finishes, so its payload_ready is queued for the
    GUI thread -- but not delivered until the test spins."""
    _join_worker(window, "connect_thread")
    assert _spin(lambda: window._cast_payload_workers, timeout=3)
    worker = window._cast_payload_workers[-1]
    gate.released.set()
    assert worker.wait(5000)
    return worker


def _finish_remote_loads(window):
    media = window.cast.media_controller
    for index in range(len(media.loads)):
        media.release(index)
    thread = window.cast_controller.load_thread
    if thread is not None:
        thread.join(5)
    _spin()


def test_metadata_ready_after_choosing_this_computer_starts_no_cast_load(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    local = _play_locally(window, 0)
    gate = _uncached_metadata(monkeypatch, window, "a.mp3")
    loads = _record_cast_loads(monkeypatch, window)
    _install_output_menu(window)
    _choose_output(window, CAST_ROW)
    worker = _hold_metadata_result(window, gate)
    try:
        _choose_output(window, LOCAL_ROW)
        assert window._cast_current_request is None  # authority already revoked
        assert worker._generation == window._cast_payload_generation  # yet the metadata result is current
        stops = window.simple_player.stop_calls
        queue_before = _queue_state(window)
        _spin()  # the metadata result is delivered now

        assert loads == []  # no stale Cast work initiated
        assert window.cast.media_controller.loads == []
        assert window.cast_active is False
        assert window.simple_player.playing and window.simple_player.stop_calls == stops
        assert window._current_playback_attempt is local and local.state is PlaybackAttemptState.PLAYING
        assert _queue_state(window) == queue_before
    finally:
        _finish_remote_loads(window)


def test_metadata_ready_for_attemptless_switch_after_newer_local_playback_starts_no_cast_load(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    _play_locally(window, 0)
    PlayerWindow.stop_playback(window)
    gate = _uncached_metadata(monkeypatch, window, "a.mp3")
    loads = _record_cast_loads(monkeypatch, window)
    _install_output_menu(window)
    _choose_output(window, CAST_ROW)
    assert window._cast_current_request.attempt_id is None
    worker = _hold_metadata_result(window, gate)
    try:
        _b_token, b_attempt = _dispatch(window, "b.mp3")  # newer user intent: play B here
        assert window._cast_current_request is None
        assert worker._generation == window._cast_payload_generation
        b_state = b_attempt.state
        player = window.simple_player
        player_state = (player.playing, player._paused, player.stop_calls)
        messages = _record_status_messages(window)
        disconnects = _record_disconnects(monkeypatch, window)
        queue_before = _queue_state(window)
        _spin()

        assert loads == []
        assert window.cast.media_controller.loads == []
        assert (player.playing, player._paused, player.stop_calls) == player_state
        assert window._current_playback_attempt is b_attempt and b_attempt.state is b_state
        assert disconnects == [] and not any("Cast" in m for m in messages)
        assert window.cast_active is False and window._cast_current_request is None
        assert _queue_state(window) == queue_before
    finally:
        _finish_remote_loads(window)


def test_current_uncached_metadata_result_starts_exactly_one_load_for_its_request(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    local = _play_locally(window, 0)
    gate = _uncached_metadata(monkeypatch, window, "a.mp3")
    loads = _record_cast_loads(monkeypatch, window)
    _install_output_menu(window)
    _choose_output(window, CAST_ROW)
    request = window._cast_current_request
    _hold_metadata_result(window, gate)
    try:
        assert _spin(lambda: window.cast.media_controller.loads, timeout=3)

        assert [(os.path.basename(c.url), c.request, c.thread) for c in loads] == [("a.mp3", request, "MainThread")]
        _emit_load_result_but_hold_delivery(window, 0)
        _spin()
        assert window.cast_active is True
        assert local.state is PlaybackAttemptState.PLAYING
    finally:
        _finish_remote_loads(window)


def test_superseded_metadata_generation_starts_no_load_while_its_request_is_current(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    _play_locally(window, 0)
    gate = _uncached_metadata(monkeypatch, window, "a.mp3")
    loads = _record_cast_loads(monkeypatch, window)
    _install_output_menu(window)
    _choose_output(window, CAST_ROW)
    request = window._cast_current_request
    _hold_metadata_result(window, gate)
    try:
        # A newer (cached) metadata preparation for the same Cast operation.
        b_path = _path_of(window, "b.mp3")
        window._request_cast_load("http://cast/b.mp3", "audio/mpeg", b_path, 0.0, True)
        _spin()  # the older metadata result for a.mp3 is delivered now

        assert window._cast_current_request is request  # request authority alone would not stop it
        assert [os.path.basename(c.url) for c in loads] == ["b.mp3"]
    finally:
        _finish_remote_loads(window)


def test_metadata_result_of_cast_request_1_cannot_load_after_request_2_replaces_it(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    _play_locally(window, 0)
    gate = _uncached_metadata(monkeypatch, window, "a.mp3")
    loads = _record_cast_loads(monkeypatch, window)
    _install_output_menu(window)
    _choose_output(window, CAST_ROW)
    request_1 = window._cast_current_request
    worker = _hold_metadata_result(window, gate)
    try:
        window.cast.socket_client.ident = None  # the fake reconnects without a rebuilt Chromecast
        _choose_output(window, CAST_ROW)  # the user chooses the device again: request 2
        request_2 = window._cast_current_request
        assert request_2 is not request_1
        assert worker._generation == window._cast_payload_generation
        _join_worker(window, "connect_thread")
        assert _spin(lambda: window.cast.media_controller.loads, timeout=3)
        _spin()

        assert [c.request for c in loads] == [request_2]
        _emit_load_result_but_hold_delivery(window, 0)
        _spin()
        assert window.cast_active is True  # request 2 unaffected
    finally:
        _finish_remote_loads(window)
