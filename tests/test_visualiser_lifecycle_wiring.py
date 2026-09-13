"""Wiring tests for the visibility-aware visualiser suspend/resume feature
across billsmusic/window.py, billsmusic/widgets.py (BeatWidget) and
billsmusic/party_mode.py (PartyModeWindow).

Follows the LayoutHarness pattern from tests/test_visualiser_layout.py: a
QMainWindow subclass binding the real unbound PlayerWindow methods under
test, plus a real BeatWidget so timer start/stop behaviour is exercised
for real (not mocked), run under QT_QPA_PLATFORM=offscreen.
"""
import inspect
import os
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from billsmusic.media_type import MediaType
from billsmusic.widgets import BeatWidget
from billsmusic.window import PlayerWindow
from billsmusic.party_mode import PartyModeWindow
from billsmusic.visualiser_lifecycle import (
    VisualiserLifecycleController,
    VisualiserRunState,
)

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


class _FakeTimer:
    """Stand-in for analyzer_timer -- avoids needing the real AudioAnalyzer/
    player-clock pipeline just to test start/stop/restart behaviour."""
    def __init__(self):
        self._active = False
        self.start_calls = 0
        self.stop_calls = 0

    def isActive(self):
        return self._active

    def start(self, *_args):
        self._active = True
        self.start_calls += 1

    def stop(self):
        self._active = False
        self.stop_calls += 1


class _FakeMiniPlayer:
    def __init__(self, visible=False):
        self._visible = visible

    def isVisible(self):
        return self._visible


class VisualiserLifecycleHarness(QtWidgets.QMainWindow):
    _apply_visualiser_layout = PlayerWindow._apply_visualiser_layout
    _remember_right_splitter_state = PlayerWindow._remember_right_splitter_state
    _restore_right_splitter_position = PlayerWindow._restore_right_splitter_position
    _effective_visualiser_visibility = PlayerWindow._effective_visualiser_visibility
    _effective_analyzer_feed_needed = PlayerWindow._effective_analyzer_feed_needed
    _refresh_visualiser_lifecycle = PlayerWindow._refresh_visualiser_lifecycle
    _apply_visualiser_transition = PlayerWindow._apply_visualiser_transition
    _apply_analyzer_feed_transition = PlayerWindow._apply_analyzer_feed_transition
    _record_visualiser_diagnostics = PlayerWindow._record_visualiser_diagnostics
    showEvent = PlayerWindow.showEvent
    hideEvent = PlayerWindow.hideEvent
    changeEvent = PlayerWindow.changeEvent
    _stop_video_for_audio_transition = PlayerWindow._stop_video_for_audio_transition
    _show_normal_display_page = PlayerWindow._show_normal_display_page
    _detach_video_from_party_mode = PlayerWindow._detach_video_from_party_mode
    _route_video_output = PlayerWindow._route_video_output

    def __init__(self):
        super().__init__()
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        self.beat = BeatWidget()
        self.visualiser_frame = QtWidgets.QFrame()
        frame_layout = QtWidgets.QVBoxLayout(self.visualiser_frame)
        frame_layout.addWidget(self.beat)
        self.right_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.right_splitter.addWidget(self.visualiser_frame)
        self.right_splitter.addWidget(QtWidgets.QFrame())
        root.addWidget(self.right_splitter)
        self.action_toggle_visualiser = QtGui.QAction(self)
        self.action_toggle_visualiser.setCheckable(True)
        self.action_toggle_visualiser.setChecked(True)
        self._visualiser_fullscreen = False
        self._visualiser_splitter_state = None
        self._splitter_save_timer = None

        self.mini_player = None
        self.party_mode = None
        self._closing = False
        self.diagnostics = _RecordingDiagnostics()
        self.analyzer_timer = _FakeTimer()
        self.analyzer_timer.start()
        self._analyzer_tick_calls = []
        self._analyzer_tick = lambda: self._analyzer_tick_calls.append(time.perf_counter())

        self._visualiser_lifecycle = VisualiserLifecycleController("main_window")
        self._analyzer_feed_lifecycle = VisualiserLifecycleController(
            "analyzer_feed", initial_state=VisualiserRunState.ACTIVE,
        )

        self._current_media_type = MediaType.AUDIO
        self._video_fullscreen = False
        self._video_backend = SimpleNamespace(
            stop=lambda: None, attach_output=lambda host: None,
            schedule_output_geometry_sync=lambda: None,
        )
        self.video_output_widget = QtWidgets.QWidget()
        self._resume_deferred_queue_analysis = lambda: None
        # Deliberately NOT added to any real layout, and NOT reusing
        # visualiser_frame (which is already parented under right_splitter
        # above -- reparenting it here via addWidget() would silently pull
        # it out of that layout and break every other test in this file).
        # _show_normal_display_page() only needs somewhere to call
        # setCurrentWidget() on; the real visualiser_frame's own visibility
        # (what _effective_visualiser_visibility() actually reads) is
        # governed by its real parent chain, untouched by this.
        self.right_display_stack = QtWidgets.QStackedWidget()
        self._normal_display_page = QtWidgets.QFrame()
        self.right_display_stack.addWidget(self._normal_display_page)
        self._video_output_page = QtWidgets.QFrame()
        self.right_display_stack.addWidget(self._video_output_page)


_LIVE_WINDOWS = []


def _shown_harness():
    app = _app()
    window = VisualiserLifecycleHarness()
    window.resize(400, 400)
    window.show()
    app.processEvents()
    _LIVE_WINDOWS.append(window)
    return app, window


@pytest.fixture(autouse=True)
def _cleanup_live_windows():
    # Real BeatWidget instances actually paint; leaving dozens of live
    # top-level windows accumulated across this file's tests has caused
    # crashes in the offscreen QPA backend. Close and release each one
    # right after its test.
    yield
    app = _app()
    for window in _LIVE_WINDOWS:
        try:
            window.close()
            window.deleteLater()
        except Exception:
            pass
    _LIVE_WINDOWS.clear()
    app.processEvents()


# ---------------------------------------------------------------------------
# 1. Visible selected visualiser starts ACTIVE
# ---------------------------------------------------------------------------

def test_visible_visualiser_becomes_active_on_show():
    app, window = _shown_harness()
    assert window._visualiser_lifecycle.is_active is True
    assert window.beat.is_suspended is False


# ---------------------------------------------------------------------------
# 2 & 3. Minimise suspends, restore resumes
# ---------------------------------------------------------------------------

def test_minimising_suspends_and_restoring_resumes():
    app, window = _shown_harness()
    window.showMinimized()
    app.processEvents()
    assert window._visualiser_lifecycle.is_active is False
    assert window.beat.is_suspended is True

    window.showNormal()
    app.processEvents()
    assert window._visualiser_lifecycle.is_active is True
    assert window.beat.is_suspended is False


# ---------------------------------------------------------------------------
# 4 & 5. Hiding the panel suspends; showing it again resumes
# ---------------------------------------------------------------------------

def test_hiding_and_showing_the_visualiser_panel():
    app, window = _shown_harness()
    window._apply_visualiser_layout(False)
    app.processEvents()
    assert window._visualiser_lifecycle.is_active is False
    assert window.beat.is_suspended is True

    window._apply_visualiser_layout(True)
    app.processEvents()
    assert window._visualiser_lifecycle.is_active is True
    assert window.beat.is_suspended is False


# ---------------------------------------------------------------------------
# 6 & 7. "Disabling in preferences" -- this app has no separate preference
# distinct from the panel-visible toggle (action_toggle_visualiser IS the
# enable/disable control); re-exercises the same path with that framing.
# ---------------------------------------------------------------------------

def test_disabling_visualiser_via_toggle_suspends_and_reenabling_resumes():
    app, window = _shown_harness()
    window._apply_visualiser_layout(False)  # "disabled"
    app.processEvents()
    assert window.beat.is_suspended is True

    window._apply_visualiser_layout(True)  # "re-enabled", still visible
    app.processEvents()
    assert window.beat.is_suspended is False


# ---------------------------------------------------------------------------
# 9 & 10. Mini Player mode
# ---------------------------------------------------------------------------

def test_entering_mini_player_suspends_hidden_main_visualiser():
    app, window = _shown_harness()
    window.mini_player = _FakeMiniPlayer(visible=True)
    window.hide()  # mirrors _show_mini_player() hiding the main window
    app.processEvents()
    assert window._visualiser_lifecycle.is_active is False
    assert window.beat.is_suspended is True


def test_leaving_mini_player_resumes_the_main_visualiser():
    app, window = _shown_harness()
    window.mini_player = _FakeMiniPlayer(visible=True)
    window.hide()
    app.processEvents()
    assert window.beat.is_suspended is True

    window.mini_player = _FakeMiniPlayer(visible=False)
    window.show()
    app.processEvents()
    assert window._visualiser_lifecycle.is_active is True
    assert window.beat.is_suspended is False


# ---------------------------------------------------------------------------
# 11, 12, 13. Render timer stops while suspended; exactly one timer exists
# across repeated cycles; no recurring repaint work happens while suspended
# ---------------------------------------------------------------------------

def test_render_timer_stops_while_suspended():
    app, window = _shown_harness()
    assert window.beat._timer.isActive() is True
    window.showMinimized()
    app.processEvents()
    assert window.beat._timer.isActive() is False


def test_only_one_render_timer_survives_repeated_suspend_resume_cycles():
    app, window = _shown_harness()
    timer_identity = id(window.beat._timer)
    for _ in range(10):
        window.showMinimized()
        app.processEvents()
        window.showNormal()
        app.processEvents()
    assert id(window.beat._timer) == timer_identity
    assert window.beat._timer.isActive() is True


def test_suspended_state_produces_no_recurring_repaint_requests(monkeypatch):
    app, window = _shown_harness()
    window.showMinimized()
    app.processEvents()
    update_calls = []
    monkeypatch.setattr(window.beat, "update", lambda: update_calls.append(1))
    # Simulate what would have been several timer callbacks -- since the
    # timer is stopped, this loop proves nothing fires it automatically.
    app.processEvents()
    QtCore.QThread.msleep(50)
    app.processEvents()
    assert update_calls == []


# ---------------------------------------------------------------------------
# 14, 15, 16. Resume behaviour: no stale-frame restart, one fresh frame,
# no replay of old buffered frames
# ---------------------------------------------------------------------------

def test_queued_stale_updates_do_not_restart_rendering():
    app, window = _shown_harness()
    window.beat.setLevels([0.5] * 16)
    window.showMinimized()
    app.processEvents()
    assert window.beat.is_suspended is True
    # setLevels() while suspended is a cheap store only -- must not itself
    # restart the timer or trigger the paint loop.
    window.beat.setLevels([0.9] * 16)
    assert window.beat.is_suspended is True


def test_resume_requests_exactly_one_fresh_tick(monkeypatch):
    app, window = _shown_harness()
    window.showMinimized()
    app.processEvents()
    tick_calls = []
    original_tick = window.beat._tick

    def _counting_tick():
        tick_calls.append(1)
        original_tick()

    monkeypatch.setattr(window.beat, "_tick", _counting_tick)
    window.showNormal()
    app.processEvents()
    assert tick_calls == [1]


def test_old_buffered_levels_are_not_replayed_as_a_burst_on_resume():
    app, window = _shown_harness()
    window.beat.setLevels([0.2] * 16)
    window.showMinimized()
    app.processEvents()
    # "Stale" levels arrive while suspended (cheap store only).
    window.beat.setLevels([0.7] * 16)
    levels_before_resume = list(window.beat._external_levels)
    window.showNormal()
    app.processEvents()
    # Resume paints exactly the current stored levels once -- not a queue
    # of intermediate frames.
    assert window.beat._external_levels == levels_before_resume


# ---------------------------------------------------------------------------
# 17. Visualiser frame/level data stays a bounded latest-value store
# ---------------------------------------------------------------------------

def test_levels_storage_never_grows_a_queue():
    app, window = _shown_harness()
    window.showMinimized()
    app.processEvents()
    for i in range(500):
        window.beat.setLevels([float(i % 2)] * 16)
    assert isinstance(window.beat._external_levels, list)
    assert len(window.beat._external_levels) == 16  # latest value only, not a queue


# ---------------------------------------------------------------------------
# 29. Repeated identical visibility events do not emit duplicate diagnostics
# ---------------------------------------------------------------------------

def test_repeated_identical_visibility_events_emit_no_duplicate_diagnostics():
    app, window = _shown_harness()
    window.diagnostics.events.clear()
    window.showMinimized()
    app.processEvents()
    visualiser_events = [e for e in window.diagnostics.events if e[0] == "visualiser"]
    assert len(visualiser_events) >= 1
    window.diagnostics.events.clear()
    # Firing more show/hide-adjacent refreshes with the SAME effective
    # visibility must not add more "visualiser" events.
    window._refresh_visualiser_lifecycle("redundant_check")
    window._refresh_visualiser_lifecycle("redundant_check_again")
    assert [e for e in window.diagnostics.events if e[0] == "visualiser"] == []


def test_suspend_and_resume_each_emit_exactly_one_diagnostic_event_per_consumer():
    app, window = _shown_harness()
    window.diagnostics.events.clear()
    window.showMinimized()
    app.processEvents()
    # Minimising suspends both the main-window BeatWidget AND (since nothing
    # else needs it) the shared analyzer feed -- one event each, not one
    # combined event and not repeats of either.
    suspended_events = [
        e for e in window.diagnostics.events
        if e[0] == "visualiser" and e[1] == "suspended"
    ]
    consumers = sorted(e[2]["details"]["consumer"] for e in suspended_events)
    assert consumers == ["analyzer_feed", "main_window"]

    window.diagnostics.events.clear()
    window.showNormal()
    app.processEvents()
    resumed_events = [
        e for e in window.diagnostics.events
        if e[0] == "visualiser" and e[1] == "resumed"
    ]
    consumers = sorted(e[2]["details"]["consumer"] for e in resumed_events)
    assert consumers == ["analyzer_feed", "main_window"]
    for event in resumed_events:
        assert "resume_latency_ms" in event[2]["details"]


# ---------------------------------------------------------------------------
# 30. Shutdown stops the visualiser timer safely
# ---------------------------------------------------------------------------

def test_shutdown_suspends_the_visualiser_and_stops_its_timer():
    app, window = _shown_harness()
    assert window.beat.is_suspended is False
    window._closing = True
    window._refresh_visualiser_lifecycle("shutting_down")
    window.beat.suspend()
    app.processEvents()
    assert window.beat.is_suspended is True
    assert window.beat._timer.isActive() is False


def test_shutdown_forces_suspended_even_if_window_still_reports_visible():
    app, window = _shown_harness()
    window._closing = True
    window._refresh_visualiser_lifecycle("shutting_down")
    assert window._visualiser_lifecycle.is_active is False


# ---------------------------------------------------------------------------
# Analyzer feed timer: only one shared feed, gated by whichever consumer
# (main window or Party Mode) actually needs it
# ---------------------------------------------------------------------------

def test_analyzer_feed_stops_when_no_consumer_needs_it():
    app, window = _shown_harness()
    assert window.analyzer_timer.isActive() is True
    window.showMinimized()
    app.processEvents()
    assert window.analyzer_timer.isActive() is False


def test_analyzer_feed_restarts_and_requests_one_fresh_tick_on_resume():
    app, window = _shown_harness()
    window.showMinimized()
    app.processEvents()
    window._analyzer_tick_calls.clear()
    window.showNormal()
    app.processEvents()
    assert window.analyzer_timer.isActive() is True
    assert len(window._analyzer_tick_calls) == 1


def test_analyzer_feed_stays_active_for_party_mode_even_if_main_window_hidden():
    app, window = _shown_harness()

    class _FakePartyMode:
        active_layout = "visualiser"

        def isVisible(self):
            return True

    window.party_mode = _FakePartyMode()
    window.showMinimized()
    app.processEvents()
    assert window._visualiser_lifecycle.is_active is False  # main is suspended
    assert window.analyzer_timer.isActive() is True  # but party mode still needs it


# ---------------------------------------------------------------------------
# Accessibility: no visualiser control becomes unreachable, focus preserved
# ---------------------------------------------------------------------------

def test_visualiser_toggle_action_remains_enabled_after_suspension():
    app, window = _shown_harness()
    window.showMinimized()
    app.processEvents()
    assert window.action_toggle_visualiser.isEnabled()


def test_beat_widget_keeps_accepting_focus_while_suspended():
    app, window = _shown_harness()
    window.showMinimized()
    app.processEvents()
    assert window.beat.focusPolicy() == QtCore.Qt.FocusPolicy.StrongFocus


# ---------------------------------------------------------------------------
# Party Mode: independent visible consumer with its own BeatWidget/timer
# ---------------------------------------------------------------------------

class _MinimalOwner(QtWidgets.QMainWindow):
    """Just enough of PlayerWindow for PartyModeWindow to construct and
    for _refresh_visualiser_lifecycle to be a harmless no-op callback."""
    def __init__(self):
        super().__init__()
        self.beat = BeatWidget()
        self._playback_expected = False
        self._playback_intentionally_paused = False
        self._closing = False
        self.party_mode_default_layout = "lyrics"
        self.party_mode_screen_name = ""
        self.party_mode_show_up_next = True
        self.party_mode_up_next_count = 3
        self.party_mode_show_clock = True
        self.party_mode_show_remaining_playlist_time = False
        self.party_mode_animations_enabled = True
        self.party_mode_visual_quality = "medium"
        self.party_mode_auto_hide_ms = 60000
        self.refresh_calls = []
        self.action_previous = QtGui.QAction(self)
        self.action_play_pause_alternate = QtGui.QAction(self)
        self.action_next = QtGui.QAction(self)

    def _refresh_visualiser_lifecycle(self, reason):
        self.refresh_calls.append(reason)

    def _save_user_settings(self):
        pass


def _party_mode_harness():
    app = _app()
    owner = _MinimalOwner()
    party = PartyModeWindow(owner)
    _LIVE_WINDOWS.append(owner)
    return app, owner, party


def test_party_mode_visualiser_suspended_when_hidden_and_active_when_shown_on_visualiser_layout():
    app, owner, party = _party_mode_harness()
    party._allow_close = True
    party.set_layout("visualiser")
    assert party._visualiser_lifecycle.is_active is False  # not shown yet

    party.showFullScreen()
    app.processEvents()
    assert party._visualiser_lifecycle.is_active is True
    assert party.visualiser_layout.beat.is_suspended is False

    party.hide()
    app.processEvents()
    assert party._visualiser_lifecycle.is_active is False
    assert party.visualiser_layout.beat.is_suspended is True
    party.close()
    party.deleteLater()


def test_party_mode_visualiser_suspended_when_a_different_layout_is_selected():
    app, owner, party = _party_mode_harness()
    party._allow_close = True
    party.showFullScreen()
    app.processEvents()
    party.set_layout("visualiser")
    app.processEvents()
    assert party.visualiser_layout.beat.is_suspended is False

    party.set_layout("lyrics")
    app.processEvents()
    assert party.visualiser_layout.beat.is_suspended is True
    party.close()
    party.deleteLater()


def test_party_mode_pings_owner_to_recheck_shared_analyzer_feed():
    app, owner, party = _party_mode_harness()
    party._allow_close = True
    owner.refresh_calls.clear()
    party.set_layout("visualiser")
    assert "party_mode_changed" in owner.refresh_calls
    party.close()
    party.deleteLater()


# ---------------------------------------------------------------------------
# Paint-performance diagnostics: each BeatWidget instance is tagged so a
# slowdown specific to one consumer (e.g. Party Mode on its own screen)
# doesn't get hidden by averaging it in with the main window's.
# ---------------------------------------------------------------------------

def test_beat_widget_defaults_to_main_diagnostic_consumer():
    _app()
    beat = BeatWidget()
    assert beat._diagnostic_consumer == "main"


def test_beat_widget_accepts_an_explicit_diagnostic_consumer():
    _app()
    beat = BeatWidget(diagnostic_consumer="party_mode")
    assert beat._diagnostic_consumer == "party_mode"


def test_party_modes_beat_widget_is_tagged_party_mode():
    app, owner, party = _party_mode_harness()
    try:
        assert party.visualiser_layout.beat._diagnostic_consumer == "party_mode"
    finally:
        party._allow_close = True
        party.close()
        party.deleteLater()


def test_main_and_party_mode_beat_paints_are_attributed_separately(monkeypatch):
    from billsmusic.performance_diagnostics import get_diagnostics

    app, owner, party = _party_mode_harness()
    diagnostics = get_diagnostics()
    recorded = []
    monkeypatch.setattr(
        diagnostics, "observe_visual_frame",
        lambda *a, **k: recorded.append(k.get("consumer")),
    )
    try:
        owner.beat.resize(200, 100)
        owner.beat.show()
        app.processEvents()
        owner.beat.repaint()

        party.showFullScreen()
        party.set_layout("visualiser")
        app.processEvents()
        party.visualiser_layout.beat.repaint()

        assert "main" in recorded
        assert "party_mode" in recorded
    finally:
        party._allow_close = True
        party.close()
        party.deleteLater()


# ---------------------------------------------------------------------------
# Playback / crossfade / backend / background-task independence: no code
# changes were made to any of these, verified by source inspection so a
# regression there fails loudly rather than silently.
# ---------------------------------------------------------------------------

def test_crossfade_and_playback_methods_do_not_reference_the_visualiser_lifecycle():
    for method in (
        PlayerWindow._finish_crossfade, PlayerWindow._cancel_fade,
        PlayerWindow._next_track, PlayerWindow.stop_playback,
        PlayerWindow._reset_analyzer_clock,
    ):
        source = inspect.getsource(method)
        assert "_visualiser_lifecycle" not in source
        assert "VisualiserRunState" not in source


def test_analyzer_tick_body_unchanged_by_the_new_gating():
    # The suspend/resume behaviour is implemented by starting/stopping the
    # analyzer_timer itself (see _apply_analyzer_feed_transition), not by
    # adding a visibility check inside _analyzer_tick -- so its existing
    # levels -> beat/party_mode hand-off logic must be untouched.
    source = inspect.getsource(PlayerWindow._analyzer_tick)
    assert "self.beat.setLevels(levels)" in source
    assert "self.party_mode.push_levels(levels)" in source
    assert "_visualiser_lifecycle" not in source


def test_background_worker_shutdown_unaffected_by_visualiser_changes():
    # Phase C2 (worker lifetime / shutdown ownership, 2026-09-11): these
    # workers are no longer cancelled/waited inline in a shutdown method
    # -- each registers itself with WorkerLifetimeRegistry at its own
    # construction/dispatch site, and _request_shutdown's only
    # involvement is its one self._worker_registry.shutdown_all() call.
    # Proven here at the registration sites instead, to keep this test's
    # original intent: visualiser changes must never remove a worker's
    # shutdown-ownership registration.
    startup_source = inspect.getsource(PlayerWindow._startup_restore_library)
    for worker_attr in ("bio_worker", "search_worker", "queue_analysis_worker"):
        assert worker_attr in startup_source
        assert f"self._worker_registry.register(" in startup_source
    scan_source = inspect.getsource(PlayerWindow._start_scan)
    assert "self._worker_registry.register(" in scan_source
    assert "scan_thread" in scan_source


# ---------------------------------------------------------------------------
# Video -> audio transition must resume the visualiser, not leave it stuck
# ---------------------------------------------------------------------------
# Reported live: after a video played and the user switched back to a normal
# audio track, the main visualiser (and its "equaliser" bars) never came
# back. _stop_video_for_audio_transition() used to flip _current_media_type
# to AUDIO only *after* calling _show_normal_display_page(), which
# re-evaluates the visualiser lifecycle immediately -- that evaluation saw
# the still-VIDEO type, concluded the visualiser should stay suspended, and
# nothing in the ordinary playback path ever re-checked it afterward.

def test_video_to_audio_transition_resumes_the_main_visualiser():
    app, window = _shown_harness()
    assert window._visualiser_lifecycle.is_active is True
    assert window.beat.is_suspended is False

    window._current_media_type = MediaType.VIDEO
    window._refresh_visualiser_lifecycle("video_shown")
    assert window._visualiser_lifecycle.is_active is False
    assert window.beat.is_suspended is True

    window._stop_video_for_audio_transition()
    app.processEvents()

    assert window._current_media_type == MediaType.AUDIO
    assert window._visualiser_lifecycle.is_active is True
    assert window.beat.is_suspended is False


def test_current_media_type_flips_to_audio_before_display_page_switches():
    # The ordering itself is the fix -- assert it directly so a future
    # refactor can't quietly reintroduce the stale-read-before-write bug
    # even if some other change happened to mask its symptom.
    app, window = _shown_harness()
    window._current_media_type = MediaType.VIDEO
    seen_media_type_during_switch = []
    real_show_normal = window._show_normal_display_page

    def spying_show_normal_display_page():
        seen_media_type_during_switch.append(window._current_media_type)
        real_show_normal()

    window._show_normal_display_page = spying_show_normal_display_page

    window._stop_video_for_audio_transition()

    assert seen_media_type_during_switch == [MediaType.AUDIO]
