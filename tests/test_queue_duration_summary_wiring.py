"""Wiring tests for the Up Next duration/finish-time summary in
billsmusic/window.py.

Follows the SimpleNamespace "fake window" + real unbound PlayerWindow
method pattern used by tests/test_track_transition_mode.py, plus its
inspect.getsource static-check pattern for behaviour that's easiest to
verify by asserting on the method's source rather than driving the full
(heavily Qt-widget-dependent) live window.
"""
import inspect
import time
from types import SimpleNamespace

from billsmusic.playlist_repair import PlaylistEntry
from billsmusic.queue_duration_estimator import ActiveCrossfadeState
from billsmusic.window import PlayerWindow


class _FakeLabel:
    def __init__(self):
        self.text = None
        self.accessible_description = None
        self.tooltip = None

    def setText(self, value):
        self.text = value

    def setAccessibleDescription(self, value):
        self.accessible_description = value

    def setToolTip(self, value):
        self.tooltip = value


class _FakeTimer:
    def __init__(self):
        self.start_calls = 0

    def start(self, ms):
        self.start_calls += 1


class _RecordingDiagnostics:
    def __init__(self):
        self.events = []

    def record(self, category, operation, **kwargs):
        self.events.append((category, operation, kwargs))


class _FakePlayer:
    def __init__(self, length, pos):
        self._length = length
        self._pos = pos

    def get_length(self):
        return self._length

    def get_pos(self):
        return self._pos


def _base_window(**overrides):
    window = SimpleNamespace(
        queue=[], queue_played=[], queue_playlist_entries=[],
        queue_detail_cache={}, _meta_by_path={},
        current_path=None,
        _last_progress_length_ms=0, _last_progress_current_ms=0,
        track_transition_mode="crossfade", crossfade_seconds=6.0,
        _playback_expected=False, _playback_intentionally_paused=False,
        fade_active=False,
        _use_builtin_player=lambda: True,
        simple_player=None, simple_inactive_player=None,
        active_player=None, inactive_player=None,
        queue_duration_summary_label=_FakeLabel(),
        diagnostics=_RecordingDiagnostics(),
        _closing=False,
        _queue_duration_debounce_timer=_FakeTimer(),
        _queue_duration_pending_reason="structural_change",
        _queue_duration_last_tick_monotonic=0.0,
        _queue_duration_last_unknown_count=None,
        _queue_duration_last_paused=None,
        _queue_duration_last_empty=None,
        _queue_duration_last_finish_minute=None,
        _announce_accessible_status=lambda message, timeout=4000: announced.append(message),
        _format_queue_finish_time=lambda dt: "22:47",
    )
    announced = []
    window._announce_accessible_status = lambda message, timeout=4000: announced.append(message)
    window._announced = announced
    window._queue_entry_duration_seconds = (
        lambda path, entry: PlayerWindow._queue_entry_duration_seconds(window, path, entry)
    )
    window._current_track_duration_state = (
        lambda: PlayerWindow._current_track_duration_state(window)
    )
    window._active_crossfade_state = (
        lambda: PlayerWindow._active_crossfade_state(window)
    )
    window._build_queue_duration_estimate = (
        lambda: PlayerWindow._build_queue_duration_estimate(window)
    )
    for key, value in overrides.items():
        setattr(window, key, value)
    return window


# ---------------------------------------------------------------------------
# _queue_entry_duration_seconds: source priority
# ---------------------------------------------------------------------------

def test_playlist_entry_duration_wins_over_cache_and_meta():
    window = _base_window(
        queue_detail_cache={"a.mp3": {"time": "1:00"}},
        _meta_by_path={"a.mp3": {"duration_seconds": 999.0}},
    )
    entry = PlaylistEntry(path="a.mp3", resolved_path="a.mp3", duration_seconds=42.0)
    result = PlayerWindow._queue_entry_duration_seconds(window, "a.mp3", entry)
    assert result == 42.0


def test_detail_cache_used_when_no_playlist_entry_duration():
    window = _base_window(queue_detail_cache={"a.mp3": {"time": "2:00"}})
    result = PlayerWindow._queue_entry_duration_seconds(window, "a.mp3", None)
    assert result == 120.0


def test_meta_duration_seconds_used_as_fallback():
    window = _base_window(_meta_by_path={"a.mp3": {"duration_seconds": 77.0}})
    result = PlayerWindow._queue_entry_duration_seconds(window, "a.mp3", None)
    assert result == 77.0


def test_meta_duration_string_parsed_as_fallback():
    window = _base_window(_meta_by_path={"a.mp3": {"duration": "1:15"}})
    result = PlayerWindow._queue_entry_duration_seconds(window, "a.mp3", None)
    assert result == 75.0


def test_unknown_when_no_source_has_duration():
    window = _base_window()
    assert PlayerWindow._queue_entry_duration_seconds(window, "a.mp3", None) is None


def test_never_performs_a_synchronous_metadata_read():
    source = inspect.getsource(PlayerWindow._queue_entry_duration_seconds)
    assert "_queue_track_details" not in source
    assert "MutagenFile" not in source
    source = inspect.getsource(PlayerWindow._current_track_duration_state)
    assert "_queue_track_details" not in source
    assert "MutagenFile" not in source
    source = inspect.getsource(PlayerWindow._build_queue_duration_estimate)
    assert "_queue_track_details" not in source
    assert "MutagenFile" not in source


# ---------------------------------------------------------------------------
# _current_track_duration_state
# ---------------------------------------------------------------------------

def test_current_track_state_none_when_nothing_loaded():
    window = _base_window(current_path=None)
    assert PlayerWindow._current_track_duration_state(window) is None


def test_current_track_state_uses_live_progress_when_available():
    window = _base_window(
        current_path="now.mp3",
        _last_progress_length_ms=200000, _last_progress_current_ms=50000,
    )
    state = PlayerWindow._current_track_duration_state(window)
    assert state.duration_seconds == 200.0
    assert state.position_seconds == 50.0


def test_current_track_state_falls_back_to_cache_before_progress_reported():
    window = _base_window(
        current_path="now.mp3", _last_progress_length_ms=0,
        queue_detail_cache={"now.mp3": {"time": "3:00"}},
    )
    state = PlayerWindow._current_track_duration_state(window)
    assert state.duration_seconds == 180.0
    assert state.position_seconds == 0.0


# ---------------------------------------------------------------------------
# _build_queue_duration_estimate: end-to-end via real queue lists
# ---------------------------------------------------------------------------

def test_build_estimate_excludes_played_and_unavailable_rows():
    missing_entry = PlaylistEntry(path="c.mp3", resolved_path="c.mp3", is_missing=True)
    window = _base_window(
        queue=["a.mp3", "b.mp3", "c.mp3"],
        queue_played=[True, False, False],
        queue_playlist_entries=[None, None, missing_entry],
        queue_detail_cache={
            "a.mp3": {"time": "1:00"}, "b.mp3": {"time": "2:00"}, "c.mp3": {"time": "3:00"},
        },
    )
    estimate = PlayerWindow._build_queue_duration_estimate(window)
    assert estimate.known_remaining_seconds == 120.0
    assert estimate.is_stopped is True


def test_build_estimate_stopped_when_playback_not_expected_even_if_current_path_stale():
    window = _base_window(
        current_path="stale.mp3",  # left over from a previous track, never cleared
        _playback_expected=False,
        queue=["a.mp3"], queue_played=[False],
        queue_detail_cache={"a.mp3": {"time": "1:00"}},
    )
    estimate = PlayerWindow._build_queue_duration_estimate(window)
    assert estimate.is_stopped is True
    assert estimate.current_remaining_seconds is None


def test_build_estimate_playing_includes_current_track():
    window = _base_window(
        current_path="now.mp3", _playback_expected=True,
        track_transition_mode="normal",
        _last_progress_length_ms=100000, _last_progress_current_ms=40000,
        queue=["next.mp3"], queue_played=[False],
        queue_detail_cache={"next.mp3": {"time": "1:00"}},
    )
    estimate = PlayerWindow._build_queue_duration_estimate(window)
    assert estimate.current_remaining_seconds == 60.0
    assert estimate.known_remaining_seconds == 120.0
    assert estimate.is_stopped is False


def test_build_estimate_paused_flag_only_true_while_expected():
    window = _base_window(
        current_path="now.mp3", _playback_expected=True,
        _playback_intentionally_paused=True,
        _last_progress_length_ms=100000, _last_progress_current_ms=40000,
    )
    estimate = PlayerWindow._build_queue_duration_estimate(window)
    assert estimate.is_paused is True


# ---------------------------------------------------------------------------
# _active_crossfade_state
# ---------------------------------------------------------------------------

def test_active_crossfade_state_none_when_not_fading():
    window = _base_window(fade_active=False)
    assert PlayerWindow._active_crossfade_state(window) is None


def test_active_crossfade_state_builtin_backend():
    window = _base_window(
        fade_active=True,
        simple_player=_FakePlayer(length=200.0, pos=196.0),
        simple_inactive_player=_FakePlayer(length=0.0, pos=2.0),
    )
    state = PlayerWindow._active_crossfade_state(window)
    assert isinstance(state, ActiveCrossfadeState)
    assert state.outgoing_remaining_seconds == 4.0
    assert state.incoming_elapsed_seconds == 2.0


# ---------------------------------------------------------------------------
# _schedule_queue_duration_refresh: single reused debounce timer
# ---------------------------------------------------------------------------

def test_schedule_refresh_restarts_the_one_debounce_timer():
    window = _base_window()
    for _ in range(50):
        PlayerWindow._schedule_queue_duration_refresh(window, "tracks_added")
    # 50 rapid structural changes (e.g. a bulk playlist import) all funnel
    # through the same timer being restarted, not 50 separate timers.
    assert window._queue_duration_debounce_timer.start_calls == 50
    assert window._queue_duration_pending_reason == "tracks_added"


def test_schedule_refresh_does_nothing_while_closing():
    timer = _FakeTimer()
    window = _base_window(_closing=True, _queue_duration_debounce_timer=timer)
    PlayerWindow._schedule_queue_duration_refresh(window, "tracks_added")
    assert timer.start_calls == 0


# ---------------------------------------------------------------------------
# _refresh_queue_duration_summary_now
# ---------------------------------------------------------------------------

def test_refresh_now_updates_label_text_and_tooltip():
    window = _base_window(
        queue=["a.mp3"], queue_played=[False],
        queue_detail_cache={"a.mp3": {"time": "2:00"}},
    )
    PlayerWindow._refresh_queue_duration_summary_now(window, "structural_change")
    label = window.queue_duration_summary_label
    assert label.text == "Up Next: 2 min total"
    assert label.accessible_description == label.text
    assert "Queued tracks total" in label.tooltip


def test_refresh_now_never_rebuilds_up_next_widget():
    source = inspect.getsource(PlayerWindow._refresh_queue_duration_summary_now)
    assert "_refresh_queue_list" not in source
    assert "queue_list.clear" not in source


def test_refresh_now_records_diagnostics_on_structural_reason():
    window = _base_window(queue=["a.mp3"], queue_played=[False])
    PlayerWindow._refresh_queue_duration_summary_now(window, "tracks_added")
    operations = [event[1] for event in window.diagnostics.events]
    assert "duration_estimate_updated" in operations


def test_repeated_position_ticks_with_unchanged_unknown_count_skip_diagnostics():
    window = _base_window(
        current_path="now.mp3", _playback_expected=True,
        _last_progress_length_ms=100000, _last_progress_current_ms=1000,
    )
    PlayerWindow._refresh_queue_duration_summary_now(window, "position_tick")
    window.diagnostics.events.clear()
    PlayerWindow._refresh_queue_duration_summary_now(window, "position_tick")
    operations = [event[1] for event in window.diagnostics.events]
    assert "duration_estimate_updated" not in operations


def test_unknown_count_change_during_position_tick_still_logs_incomplete():
    window = _base_window(
        current_path="now.mp3", _playback_expected=True,
        _last_progress_length_ms=100000, _last_progress_current_ms=1000,
        queue=["b.mp3"], queue_played=[False],  # unknown-duration row
    )
    PlayerWindow._refresh_queue_duration_summary_now(window, "position_tick")
    operations = [event[1] for event in window.diagnostics.events]
    assert "duration_estimate_updated" in operations
    assert "duration_estimate_incomplete" in operations


def test_screen_reader_announcement_is_rate_limited_on_unchanged_state():
    window = _base_window(queue=["a.mp3"], queue_played=[False])
    PlayerWindow._refresh_queue_duration_summary_now(window, "structural_change")
    assert len(window._announced) == 1
    PlayerWindow._refresh_queue_duration_summary_now(window, "structural_change")
    # Nothing meaningful changed (same paused/empty/unknown/finish-minute) --
    # no repeated announcement.
    assert len(window._announced) == 1


def test_pause_toggle_triggers_a_fresh_announcement():
    window = _base_window(
        current_path="now.mp3", _playback_expected=True,
        _last_progress_length_ms=100000, _last_progress_current_ms=1000,
    )
    PlayerWindow._refresh_queue_duration_summary_now(window, "structural_change")
    window._playback_intentionally_paused = True
    PlayerWindow._refresh_queue_duration_summary_now(window, "playback_pause_toggled")
    assert len(window._announced) == 2


def test_refresh_now_handles_missing_label_gracefully():
    window = _base_window(queue_duration_summary_label=None)
    PlayerWindow._refresh_queue_duration_summary_now(window, "structural_change")  # no crash


def test_refresh_now_records_failure_diagnostic_and_does_not_raise():
    window = _base_window()

    def boom():
        raise RuntimeError("boom")

    window._build_queue_duration_estimate = boom
    PlayerWindow._refresh_queue_duration_summary_now(window, "structural_change")
    operations = [event[1] for event in window.diagnostics.events]
    assert "duration_estimate_failed" in operations


# ---------------------------------------------------------------------------
# _maybe_tick_queue_duration_refresh: throttled to <= 1/sec, only while
# playback is expected
# ---------------------------------------------------------------------------

def test_tick_refresh_skipped_when_not_playing():
    calls = []
    window = _base_window(_playback_expected=False)
    window._refresh_queue_duration_summary_now = lambda reason=None: calls.append(reason)
    PlayerWindow._maybe_tick_queue_duration_refresh(window)
    assert calls == []


def test_tick_refresh_throttled_to_once_per_second(monkeypatch):
    calls = []
    window = _base_window(_playback_expected=True, _queue_duration_last_tick_monotonic=0.0)
    window._refresh_queue_duration_summary_now = lambda reason=None: calls.append(reason)

    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

    PlayerWindow._maybe_tick_queue_duration_refresh(window)
    assert calls == ["position_tick"]

    clock["t"] += 0.2  # a rapid follow-up tick well under a second later
    PlayerWindow._maybe_tick_queue_duration_refresh(window)
    assert calls == ["position_tick"]  # unchanged: throttled

    clock["t"] += 1.0
    PlayerWindow._maybe_tick_queue_duration_refresh(window)
    assert calls == ["position_tick", "position_tick"]


# ---------------------------------------------------------------------------
# Existing playback/crossfade behaviour is unchanged by the new hooks
# ---------------------------------------------------------------------------

def test_pause_core_logic_unchanged_by_the_new_hook():
    source = inspect.getsource(PlayerWindow.pause)
    assert "self._playback_intentionally_paused = True" in source
    assert "self._playback_intentionally_paused = False" in source
    assert "_schedule_queue_duration_refresh" in source  # the new hook is present...
    # ...but only as an addition, not a replacement of existing calls:
    assert source.count("self._announce_accessible_status(") >= 4


def test_stop_playback_core_logic_unchanged_by_the_new_hook():
    source = inspect.getsource(PlayerWindow.stop_playback)
    assert "self._stop_all()" in source
    assert 'self._playback_expected = False' in source
    assert "_schedule_queue_duration_refresh" in source
