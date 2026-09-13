import collections
import inspect
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

from PyQt6 import QtWidgets

from billsmusic.sleep_timer import (
    MAX_CUSTOM_MINUTES,
    MIN_CUSTOM_MINUTES,
    MODE_OFF,
    MODE_STOP_AFTER_TRACK,
    MODE_TIMED,
    FADE_WINDOW_SECONDS,
    SleepTimerController,
    clamp_custom_minutes,
)
from billsmusic.media_type import MediaType
from billsmusic.window import PlayerWindow

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _clock(start=0.0):
    box = [start]

    def now():
        return box[0]

    def advance(seconds):
        box[0] += seconds

    now.advance = advance
    now.box = box
    return now


# ---------------------------------------------------------------------------
# Pure SleepTimerController logic -- no Qt/window involved.
# ---------------------------------------------------------------------------

def test_sleep_timer_defaults_to_off():
    controller = SleepTimerController(clock=_clock())
    assert controller.state.mode == MODE_OFF
    assert not controller.is_active
    assert not controller.is_timed
    assert not controller.is_stop_after_track


def test_fifteen_minute_timer_creates_correct_deadline():
    clock = _clock(1000.0)
    controller = SleepTimerController(clock=clock)
    controller.start_timed(15, fade_enabled=False)
    assert controller.state.deadline == 1000.0 + 15 * 60.0
    assert controller.state.duration_minutes == 15
    assert controller.is_timed


def test_custom_timer_validates_its_range():
    assert clamp_custom_minutes(0) == MIN_CUSTOM_MINUTES
    assert clamp_custom_minutes(-5) == MIN_CUSTOM_MINUTES
    assert clamp_custom_minutes(10_000) == MAX_CUSTOM_MINUTES
    assert clamp_custom_minutes(45) == 45

    clock = _clock(0.0)
    controller = SleepTimerController(clock=clock)
    controller.start_timed(99999, fade_enabled=False)
    assert controller.state.duration_minutes == MAX_CUSTOM_MINUTES


def test_remaining_time_uses_the_monotonic_deadline():
    clock = _clock(0.0)
    controller = SleepTimerController(clock=clock)
    controller.start_timed(1, fade_enabled=False)
    assert controller.remaining_seconds() == 60.0
    clock.advance(25.0)
    assert controller.remaining_seconds() == 35.0
    clock.advance(1000.0)
    assert controller.remaining_seconds() == 0.0
    assert controller.is_expired()


def test_pausing_does_not_accidentally_reset_the_timer():
    # The controller has no notion of "paused" -- elapsed time keeps moving
    # against the monotonic clock regardless of playback state, so simply
    # letting the clock advance (as it would while paused) must not reset
    # or otherwise perturb the deadline.
    clock = _clock(500.0)
    controller = SleepTimerController(clock=clock)
    controller.start_timed(10, fade_enabled=False)
    deadline_before = controller.state.deadline
    clock.advance(120.0)  # time passes while "paused"
    assert controller.state.deadline == deadline_before
    assert controller.remaining_seconds() == 480.0


def test_cancelling_clears_the_deadline():
    controller = SleepTimerController(clock=_clock())
    controller.start_timed(30, fade_enabled=True)
    controller.cancel()
    assert controller.state.mode == MODE_OFF
    assert controller.state.deadline is None
    assert controller.state.duration_minutes is None
    assert not controller.is_active


def test_fade_preference_survives_cancel():
    controller = SleepTimerController(clock=_clock())
    controller.start_timed(15, fade_enabled=True)
    controller.cancel()
    # cancel() intentionally carries the fade preference forward so a
    # restored "Fade Before Stopping" preference is not lost by an Off click.
    assert controller.state.fade_enabled is True


def test_stop_after_track_arms_without_a_deadline():
    controller = SleepTimerController(clock=_clock())
    controller.arm_stop_after_track(fade_enabled=False)
    assert controller.is_stop_after_track
    assert controller.is_active
    assert controller.state.deadline is None


def test_should_be_fading_only_within_final_window_when_enabled():
    clock = _clock(0.0)
    controller = SleepTimerController(clock=clock)
    controller.start_timed(1, fade_enabled=True)
    assert not controller.should_be_fading()
    clock.advance(60.0 - FADE_WINDOW_SECONDS)
    assert controller.should_be_fading()
    assert 0.0 <= controller.fade_gain() <= 1.0


def test_fade_disabled_never_reports_should_be_fading():
    clock = _clock(0.0)
    controller = SleepTimerController(clock=clock)
    controller.start_timed(1, fade_enabled=False)
    clock.advance(59.0)
    assert not controller.should_be_fading()
    assert controller.fade_gain() == 1.0


def test_mark_fade_started_fires_once():
    controller = SleepTimerController(clock=_clock(0.0))
    controller.start_timed(1, fade_enabled=True)
    assert controller.mark_fade_started() is True
    assert controller.mark_fade_started() is False


def test_format_remaining():
    clock = _clock(0.0)
    controller = SleepTimerController(clock=clock)
    controller.start_timed(1, fade_enabled=False)
    clock.advance(31.0)
    assert controller.format_remaining() == "0:29"


# ---------------------------------------------------------------------------
# Fake playback backends
# ---------------------------------------------------------------------------

class _FakeBuiltinPlayer:
    """Stands in for MiniaudioPlayer/BassPlayer."""

    def __init__(self, name="builtin"):
        self.name = name
        self.stopped = False
        self.volume = None
        self.playing = True

    def stop(self):
        self.stopped = True
        self.playing = False

    def set_volume(self, value):
        self.volume = value

    def is_playing(self):
        return self.playing and not self.stopped


class _FakeVlcPlayer:
    """Stands in for a vlc.MediaPlayer instance."""

    def __init__(self, name="vlc"):
        self.name = name
        self.stopped = False
        self.volume_pct = None
        self.playing = True

    def stop(self):
        self.stopped = True
        self.playing = False

    def audio_set_volume(self, value):
        self.volume_pct = value

    def is_playing(self):
        return self.playing and not self.stopped

    def get_time(self):
        return 0


class _RecordingDiagnostics:
    def __init__(self):
        self.events = []
        self.counters = collections.Counter()

    def record(self, category, operation, **kwargs):
        self.events.append((category, operation, kwargs))
        return None

    def events_for(self, operation):
        return [e for e in self.events if e[1] == operation]


class SleepTimerHarness(QtWidgets.QMainWindow):
    _next_track = PlayerWindow._next_track
    _playback_fallback_paths = PlayerWindow._playback_fallback_paths
    _crossfade_eligible_for_transition = PlayerWindow._crossfade_eligible_for_transition
    _record_track_completion = PlayerWindow._record_track_completion
    stop_playback = PlayerWindow.stop_playback
    _sync_now_playing_overlay_for_media_type = PlayerWindow._sync_now_playing_overlay_for_media_type
    _stop_video_for_audio_transition = PlayerWindow._stop_video_for_audio_transition
    _resume_deferred_queue_analysis = PlayerWindow._resume_deferred_queue_analysis
    _cancel_fade = PlayerWindow._cancel_fade
    _cancel_playback_watchdog = PlayerWindow._cancel_playback_watchdog
    _use_builtin_player = PlayerWindow._use_builtin_player
    _current_backend_name = PlayerWindow._current_backend_name
    _stop_all = PlayerWindow._stop_all
    _set_volume = PlayerWindow._set_volume
    # Playback stability hardening, Phase A.
    _begin_playback_attempt = PlayerWindow._begin_playback_attempt
    _is_current_playback_attempt = PlayerWindow._is_current_playback_attempt
    _cancel_current_playback_attempt = PlayerWindow._cancel_current_playback_attempt
    _advance_playback_attempt_state = PlayerWindow._advance_playback_attempt_state

    _build_sleep_timer_menu = PlayerWindow._build_sleep_timer_menu
    _on_sleep_timer_menu_action = PlayerWindow._on_sleep_timer_menu_action
    _checked_sleep_timer_action = PlayerWindow._checked_sleep_timer_action
    _on_sleep_timer_fade_toggled = PlayerWindow._on_sleep_timer_fade_toggled
    _sleep_timer_playback_state_label = PlayerWindow._sleep_timer_playback_state_label
    _start_sleep_timer = PlayerWindow._start_sleep_timer
    _arm_stop_after_track = PlayerWindow._arm_stop_after_track
    _cancel_sleep_timer = PlayerWindow._cancel_sleep_timer
    _complete_stop_after_track = PlayerWindow._complete_stop_after_track
    _expire_sleep_timer = PlayerWindow._expire_sleep_timer
    _teardown_sleep_timer = PlayerWindow._teardown_sleep_timer
    _restore_sleep_timer_volume = PlayerWindow._restore_sleep_timer_volume
    _reapply_sleep_timer_gain = PlayerWindow._reapply_sleep_timer_gain
    _apply_sleep_timer_gain = PlayerWindow._apply_sleep_timer_gain
    _ensure_sleep_timer_ticker = PlayerWindow._ensure_sleep_timer_ticker
    _stop_sleep_timer_ticker = PlayerWindow._stop_sleep_timer_ticker
    _on_sleep_timer_tick = PlayerWindow._on_sleep_timer_tick
    _sleep_timer_status_text = PlayerWindow._sleep_timer_status_text
    _update_sleep_timer_status = PlayerWindow._update_sleep_timer_status
    _sync_sleep_timer_menu_checks = PlayerWindow._sync_sleep_timer_menu_checks

    def __init__(self, clock):
        super().__init__()
        self.setCentralWidget(QtWidgets.QWidget())
        self.diagnostics = _RecordingDiagnostics()
        self._next_playback_attempt_id = 1
        self._current_playback_attempt = None
        self.sleep_timer = SleepTimerController(clock=clock)
        self.sleep_timer_fade_pref = False
        self._sleep_timer_gain = 1.0
        self._sleep_timer_timer = None
        self._sleep_timer_custom_minutes = 30

        self.tracks = ["a.flac", "b.flac"]
        self.current_index = 0
        self.current_path = "a.flac"
        self._playback_context_paths = []
        self._playback_context_index = None
        self._current_media_type = MediaType.AUDIO
        self._deferred_bpm_key_paths = set()
        self.queue_analysis_worker = None
        self.queue = []
        self.queue_played = []
        self.track_transition_mode = "crossfade"
        self.crossfade_seconds = 6.0
        self._playback_generation = 1
        self._last_completed_playback_generation = None
        self.fade_active = False
        self.prebuffer_active = False
        self.pending_next = False
        self.pending_builtin_crossfade_index = None
        self.pending_builtin_crossfade_path = None
        self.pending_builtin_crossfade_quiet = False
        self.fade_waits = 0
        self._playback_expected = True
        self._playback_intentionally_paused = False
        self._playback_watch_started = 0.0
        self._playback_watch_last_advance = 0.0
        self._playback_watch_has_advanced = False

        self.use_simple = True
        self.builtin_backend = "miniaudio"
        self.simple_player = _FakeBuiltinPlayer("simple")
        self.simple_inactive_player = _FakeBuiltinPlayer("simple_inactive")
        self.active_player = None
        self.inactive_player = None
        self._simple_fallback_active = False
        self._temporary_backend_override = None
        self._active_normalisation_gain = 1.0
        self._inactive_normalisation_gain = 1.0
        self.master_volume = 70
        self._mixed_transition_state = "idle"

        self.beat = SimpleNamespace(setPlaying=lambda v: None)
        self.btn_pause = SimpleNamespace(setText=lambda t: None, setAccessibleName=lambda t: None)

        self._play_path_direct_calls = []
        self._mark_queue_row_played_calls = []

        self.action_sleep_timer_fade = None
        # Keep the menu alive for the harness's lifetime -- Qt owns the
        # QActions via the QMenu, and an unparented, unreferenced QMenu can
        # be garbage-collected out from under them.
        self._sleep_timer_menu_root = QtWidgets.QMenu(self)
        self._build_sleep_timer_menu(self._sleep_timer_menu_root)

    # -- stand-ins for widget-heavy / unrelated real methods ---------------

    def statusBar(self):
        if not hasattr(self, "_status_bar"):
            self._status_bar = QtWidgets.QStatusBar()
        return self._status_bar

    def _sync_mini_player(self):
        pass

    def _reset_progress(self):
        pass

    def _announce_accessible_status(self, message, timeout=4000):
        pass

    def _audio_log(self, message):
        pass

    def _audio_name(self, path):
        return path

    def _play_path_direct(self, path, crossfade=False, index=None, immediate_crossfade=False):
        self._play_path_direct_calls.append((path, crossfade, immediate_crossfade))
        return True

    def _next_unplayed_queue_row(self):
        return None

    def _mark_queue_row_played(self, row):
        self._mark_queue_row_played_calls.append(row)

    def _queue_entry_is_missing(self, row):
        return False

    def _refresh_queue_list(self, **kwargs):
        pass

    def _save_user_settings(self):
        self.save_user_settings_calls = getattr(self, "save_user_settings_calls", 0) + 1


# ---------------------------------------------------------------------------
# Stop After Current Track
# ---------------------------------------------------------------------------

def test_stop_after_track_prevents_the_next_track_starting():
    _app()
    window = SleepTimerHarness(_clock(0.0))
    window._arm_stop_after_track()
    window._next_track("near-end")
    assert window._play_path_direct_calls == []
    assert window.simple_player.stopped is True
    assert not window.sleep_timer.is_active


def test_stop_after_track_suppresses_crossfade_preparation():
    _app()
    window = SleepTimerHarness(_clock(0.0))
    window._arm_stop_after_track()

    def _tripwire(*a, **k):
        raise AssertionError("crossfade preparation must be suppressed")

    window._start_miniaudio_crossfade_to = _tripwire
    window._start_crossfade_to = _tripwire
    window._next_track("near-end")
    assert window._play_path_direct_calls == []


def test_stop_after_track_works_when_armed_before_playback():
    _app()
    window = SleepTimerHarness(_clock(0.0))
    window._playback_expected = False
    window._arm_stop_after_track()
    assert window.sleep_timer.is_stop_after_track
    # Track starts and later finishes naturally -- still honoured.
    window._playback_expected = True
    window._next_track("quiet-end")
    assert window._play_path_direct_calls == []
    assert window.simple_player.stopped is True


def test_stop_after_track_works_with_video_ended_reason():
    _app()
    window = SleepTimerHarness(_clock(0.0))
    window._playback_expected = False
    window._arm_stop_after_track()
    assert window.sleep_timer.is_stop_after_track
    window._playback_expected = True
    window._next_track("video-ended")
    assert window._play_path_direct_calls == []
    assert window.simple_player.stopped is True


def test_stop_after_track_leaves_up_next_and_queue_flags_untouched():
    _app()
    window = SleepTimerHarness(_clock(0.0))
    window.queue = ["x.mp3", "y.mp3"]
    window.queue_played = [False, False]
    window._arm_stop_after_track()
    window._next_track("near-end")
    assert window.queue == ["x.mp3", "y.mp3"]
    assert window.queue_played == [False, False]
    assert window._mark_queue_row_played_calls == []


def test_stop_after_track_records_diagnostics():
    _app()
    window = SleepTimerHarness(_clock(0.0))
    window._arm_stop_after_track()
    assert window.diagnostics.events_for("stop_after_track_armed")
    window._next_track("vlc-ended")
    assert window.diagnostics.events_for("stop_after_track_completed")


def test_manual_next_is_not_blocked_by_stop_after_track():
    _app()
    window = SleepTimerHarness(_clock(0.0))
    window._arm_stop_after_track()
    window._next_track("manual-next")
    # Manual navigation is a deliberate user override, not a natural
    # completion -- the gate only intercepts natural end-of-track reasons.
    assert window._play_path_direct_calls != []
    assert window.simple_player.stopped is False


# ---------------------------------------------------------------------------
# Timed expiry
# ---------------------------------------------------------------------------

def test_timer_expiry_stops_playback():
    _app()
    clock = _clock(0.0)
    window = SleepTimerHarness(clock)
    window._start_sleep_timer(15)
    clock.advance(15 * 60.0 + 1.0)
    window._on_sleep_timer_tick()
    assert window.simple_player.stopped is True
    assert window.diagnostics.events_for("sleep_timer_expired")
    assert window.diagnostics.events_for("sleep_timer_stopped_playback")
    assert window.sleep_timer.state.mode == MODE_OFF


def test_timer_expiry_does_not_remove_queue_entries():
    _app()
    clock = _clock(0.0)
    window = SleepTimerHarness(clock)
    window.queue = ["a.mp3", "b.mp3"]
    window.queue_played = [False, False]
    window._start_sleep_timer(15)
    clock.advance(15 * 60.0 + 1.0)
    window._on_sleep_timer_tick()
    assert window.queue == ["a.mp3", "b.mp3"]
    assert window.queue_played == [False, False]


def test_timer_expiry_does_not_mark_the_next_track_played():
    _app()
    clock = _clock(0.0)
    window = SleepTimerHarness(clock)
    window._start_sleep_timer(15)
    clock.advance(15 * 60.0 + 1.0)
    window._on_sleep_timer_tick()
    assert window._mark_queue_row_played_calls == []


def test_timed_expiry_during_crossfade_stops_both_players_without_promotion():
    _app()
    clock = _clock(0.0)
    window = SleepTimerHarness(clock)
    outgoing = window.simple_player
    incoming = window.simple_inactive_player
    window.fade_active = True
    window.prebuffer_active = True
    window._start_sleep_timer(15)
    clock.advance(15 * 60.0 + 1.0)
    window._on_sleep_timer_tick()
    assert outgoing.stopped is True
    assert incoming.stopped is True
    # No promotion swap should have occurred.
    assert window.simple_player is outgoing
    assert window.simple_inactive_player is incoming
    assert window.fade_active is False
    assert window.prebuffer_active is False


def test_incoming_crossfade_player_is_released_on_expiry():
    _app()
    clock = _clock(0.0)
    window = SleepTimerHarness(clock)
    window.fade_active = True
    window._start_sleep_timer(15)
    clock.advance(15 * 60.0 + 1.0)
    window._on_sleep_timer_tick()
    assert window.simple_inactive_player.stopped is True


# ---------------------------------------------------------------------------
# Fade Before Stopping
# ---------------------------------------------------------------------------

def test_sleep_fade_starts_only_once():
    _app()
    clock = _clock(0.0)
    window = SleepTimerHarness(clock)
    window.action_sleep_timer_fade.setChecked(True)
    window._start_sleep_timer(1)
    clock.advance(60.0 - FADE_WINDOW_SECONDS + 0.1)
    window._on_sleep_timer_tick()
    window._on_sleep_timer_tick()
    window._on_sleep_timer_tick()
    assert len(window.diagnostics.events_for("sleep_timer_fade_started")) == 1


def test_sleep_fade_does_not_alter_the_saved_user_volume():
    _app()
    clock = _clock(0.0)
    window = SleepTimerHarness(clock)
    window.action_sleep_timer_fade.setChecked(True)
    window._start_sleep_timer(1)
    clock.advance(60.0 - 1.0)  # deep inside the 10s fade window
    window._on_sleep_timer_tick()
    assert window.master_volume == 70
    assert window._sleep_timer_gain < 1.0


def test_replaygain_remains_correct_during_and_after_the_fade():
    _app()
    clock = _clock(0.0)
    window = SleepTimerHarness(clock)
    window._active_normalisation_gain = 0.5
    window._inactive_normalisation_gain = 0.8
    window.action_sleep_timer_fade.setChecked(True)
    window._start_sleep_timer(1)
    clock.advance(60.0 - 1.0)
    window._on_sleep_timer_tick()
    assert window._active_normalisation_gain == 0.5
    assert window._inactive_normalisation_gain == 0.8
    clock.advance(2.0)
    window._on_sleep_timer_tick()
    assert window._active_normalisation_gain == 0.5
    assert window._inactive_normalisation_gain == 0.8


# ---------------------------------------------------------------------------
# Backend stop coverage: VLC, miniaudio, BASS
# ---------------------------------------------------------------------------

def test_vlc_stops_correctly_on_expiry():
    _app()
    clock = _clock(0.0)
    window = SleepTimerHarness(clock)
    window.use_simple = False
    window.simple_player = None
    window.simple_inactive_player = None
    window.active_player = _FakeVlcPlayer("active")
    window.inactive_player = _FakeVlcPlayer("inactive")
    window._start_sleep_timer(15)
    clock.advance(15 * 60.0 + 1.0)
    window._on_sleep_timer_tick()
    assert window.active_player.stopped is True
    assert window.inactive_player.stopped is True


def test_miniaudio_stops_correctly_on_stop_after_track():
    _app()
    window = SleepTimerHarness(_clock(0.0))
    window.builtin_backend = "miniaudio"
    window._arm_stop_after_track()
    window._next_track("near-end")
    assert window.simple_player.stopped is True


def test_bass_stops_correctly_on_stop_after_track():
    _app()
    window = SleepTimerHarness(_clock(0.0))
    window.builtin_backend = "bass"
    window._arm_stop_after_track()
    window._next_track("near-end")
    assert window.simple_player.stopped is True


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

def test_selecting_off_cancels_every_pending_callback():
    _app()
    clock = _clock(0.0)
    window = SleepTimerHarness(clock)
    window._start_sleep_timer(15)
    assert window._sleep_timer_timer.isActive()
    window._cancel_sleep_timer("user_off")
    assert not window._sleep_timer_timer.isActive()
    assert window.sleep_timer.state.mode == MODE_OFF
    assert window.diagnostics.events_for("sleep_timer_cancelled")
    assert window.action_sleep_timer_off.isChecked()


def test_cancelling_restores_the_original_effective_volume():
    _app()
    clock = _clock(0.0)
    window = SleepTimerHarness(clock)
    window.action_sleep_timer_fade.setChecked(True)
    window._start_sleep_timer(1)
    clock.advance(60.0 - 1.0)
    window._on_sleep_timer_tick()
    assert window._sleep_timer_gain < 1.0
    window._cancel_sleep_timer("user_off")
    assert window._sleep_timer_gain == 1.0
    from billsmusic.loudness import combine_volume
    expected = combine_volume(window.master_volume / 100.0, window._active_normalisation_gain, 1.0)
    assert window.simple_player.volume == expected


def test_cancelling_clears_status_message():
    _app()
    window = SleepTimerHarness(_clock(0.0))
    window._start_sleep_timer(15)
    window._update_sleep_timer_status()
    assert window.statusBar().currentMessage() != ""
    window._cancel_sleep_timer("user_off")
    assert window.statusBar().currentMessage() == ""


def test_only_one_timer_mode_active_at_a_time():
    _app()
    window = SleepTimerHarness(_clock(0.0))
    window._start_sleep_timer(15)
    assert window.sleep_timer.is_timed
    window._arm_stop_after_track()
    assert window.sleep_timer.is_stop_after_track
    assert not window.sleep_timer.is_timed


def test_menu_checks_follow_active_mode():
    _app()
    window = SleepTimerHarness(_clock(0.0))
    window._start_sleep_timer(30)
    matched = [
        action for action, (kind, minutes) in window._sleep_timer_actions.items()
        if kind == "timed" and minutes == 30
    ][0]
    assert matched.isChecked()

    window.sleep_timer.start_timed(20, fade_enabled=False)
    window._sync_sleep_timer_menu_checks()
    assert window.action_sleep_timer_custom.isChecked()


# ---------------------------------------------------------------------------
# Diagnostics volume / rate limiting
# ---------------------------------------------------------------------------

def test_diagnostics_are_not_written_every_second():
    _app()
    clock = _clock(0.0)
    window = SleepTimerHarness(clock)
    window._start_sleep_timer(15)
    initial_event_count = len(window.diagnostics.events)
    for _ in range(20):
        clock.advance(1.0)
        window._on_sleep_timer_tick()
    # Only status/volume bookkeeping happens on most ticks; no per-tick
    # diagnostics events should have been appended this far from expiry
    # or the fade window.
    assert len(window.diagnostics.events) == initial_event_count


# ---------------------------------------------------------------------------
# Startup / persistence
# ---------------------------------------------------------------------------

def test_active_timer_is_not_restored_after_restart():
    # A freshly constructed controller (as happens on every app startup)
    # always begins Off, with no deadline -- there is no code path that
    # seeds it from disk.
    controller = SleepTimerController(clock=_clock())
    assert controller.state.mode == MODE_OFF
    assert controller.state.deadline is None


def test_fade_preference_may_be_restored_without_restoring_the_timer():
    _app()
    window = SleepTimerHarness(_clock(0.0))
    window.sleep_timer_fade_pref = True
    window.action_sleep_timer_fade.setChecked(True)
    assert window.sleep_timer.state.mode == MODE_OFF
    assert not window.sleep_timer.is_active


def test_save_and_load_user_settings_never_reference_the_live_deadline():
    save_source = inspect.getsource(PlayerWindow._save_user_settings)
    load_source = inspect.getsource(PlayerWindow._load_user_settings)
    for source in (save_source, load_source):
        assert "sleep_timer.state" not in source
        assert "deadline" not in source
    assert "sleep_timer_fade_before_stopping" in save_source
    assert "sleep_timer_fade_before_stopping" in load_source


def test_session_document_has_no_sleep_timer_fields():
    from billsmusic import session

    source = inspect.getsource(session)
    assert "sleep_timer" not in source
    assert "deadline" not in source


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

def test_shutdown_stops_the_sleep_timer_ticker():
    # Phase C2 (2026-09-11): the timer-stop loop lives in
    # _request_shutdown() now (the former single _shutdown_threads() was
    # split into _request_shutdown()/_finalize_shutdown()).
    source = inspect.getsource(PlayerWindow._request_shutdown)
    assert "_sleep_timer_timer" in source
