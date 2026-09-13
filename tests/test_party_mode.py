"""Tests for Party Mode: the full-screen second-monitor presentation view.

Party Mode has no playback engine, queue, or lyric parser of its own -- it
only reads owner (PlayerWindow) state and forwards commands to the owner's
existing QActions/methods. Tests favor pure-function coverage (no Qt needed)
plus a smaller set of real-widget tests against a lightweight fake owner,
following the same "SimpleNamespace fake + real methods" and
"QT_QPA_PLATFORM=offscreen + shared _app()" conventions already used across
this test suite (see test_crossfade_player_rotation.py, test_sleep_timer.py).
"""
import inspect
import os
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

from PyQt6 import QtCore, QtGui, QtWidgets

from billsmusic.config import PARTY_MODE_LAYOUTS, PARTY_MODE_VISUAL_QUALITIES
from billsmusic.party_mode import (
    PartyModeInfoBar,
    PartyModeWindow,
    _blur_radius_for_quality,
    _parse_mmss,
    _visualiser_interval_ms_for_quality,
    format_screen_label,
    resolve_party_mode_screen,
    upcoming_unplayed_paths,
)
from billsmusic.window import PlayerWindow

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _format_duration(seconds):
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}:{secs:02d}"


# ---------------------------------------------------------------------------
# Pure helpers -- no Qt required
# ---------------------------------------------------------------------------

def test_upcoming_unplayed_paths_filters_and_limits():
    queue = ["a", "b", "c", "d"]
    played = [True, False, False, False]
    assert upcoming_unplayed_paths(queue, played, 2) == ["b", "c"]
    assert upcoming_unplayed_paths(queue, played, None) == ["b", "c", "d"]


def test_upcoming_unplayed_paths_empty_queue():
    assert upcoming_unplayed_paths([], [], 3) == []


def test_upcoming_unplayed_paths_all_played():
    assert upcoming_unplayed_paths(["a", "b"], [True, True], 5) == []


def test_blur_radius_for_quality():
    assert _blur_radius_for_quality("low") == 0
    assert _blur_radius_for_quality("medium") > 0
    assert _blur_radius_for_quality("high") > _blur_radius_for_quality("medium")
    assert _blur_radius_for_quality("unknown") == _blur_radius_for_quality("medium")


def test_parse_mmss():
    assert _parse_mmss("3:45") == 225.0
    assert _parse_mmss("1:02:03") == 3723.0
    assert _parse_mmss("garbage") == 0.0
    assert _parse_mmss("") == 0.0


def test_party_progress_bar_mirrors_video_timeline_state():
    source = SimpleNamespace(
        _placeholder_mode="video", _pending_path="clip.mp4", _waveform=None,
    )
    target = SimpleNamespace(
        _placeholder_mode="empty",
        _pending_path=None,
        set_video_progress_calls=[],
        set_video_progress=lambda path: target.set_video_progress_calls.append(path),
        set_placeholder=lambda path: None,
        set_waveform=lambda data: None,
    )
    info_bar = SimpleNamespace(progress_bar=target)

    PartyModeInfoBar.sync_progress_visual(info_bar, source, "clip.mp4")

    assert target.set_video_progress_calls == ["clip.mp4"]


def test_party_video_fullscreen_hides_overlays_without_moving_video_widget():
    _app()
    party = QtWidgets.QWidget()
    root = QtWidgets.QVBoxLayout(party)
    party.stack = QtWidgets.QStackedWidget()
    party.video_widget = QtWidgets.QWidget()
    party.stack.addWidget(party.video_widget)
    root.addWidget(party.stack)
    party.info_bar = QtWidgets.QWidget()
    root.addWidget(party.info_bar)
    party.control_bar = QtWidgets.QWidget(party)
    party.control_bar.conceal = MagicMock(
        side_effect=lambda _animate=True: party.control_bar.hide()
    )
    party.control_bar.reveal = MagicMock(
        side_effect=lambda _animate=True: party.control_bar.show()
    )
    party._auto_hide_timer = MagicMock()
    party._position_control_bar = lambda: None
    party.owner = SimpleNamespace(party_mode_auto_hide_ms=3000)
    party._video_active = True
    party._video_fullscreen_presentation = False
    original_parent = party.video_widget.parentWidget()
    party.show()
    QtWidgets.QApplication.processEvents()

    restore_state = PartyModeWindow.enter_video_fullscreen_presentation(party)

    assert party._video_fullscreen_presentation is True
    assert party.video_widget.parentWidget() is original_parent
    assert party.info_bar.isHidden()
    party.control_bar.conceal.assert_called_once_with(False)

    party.control_bar.conceal.reset_mock()
    party.control_bar.reveal.reset_mock()
    PartyModeWindow.exit_video_fullscreen_presentation(party, restore_state)

    assert party._video_fullscreen_presentation is False
    assert party.video_widget.parentWidget() is original_parent
    assert not party.info_bar.isHidden()
    party._auto_hide_timer.start.assert_called_once()
    party.close()


# ---------------------------------------------------------------------------
# Multi-monitor resolution (needs a QApplication for QGuiApplication.screens())
# ---------------------------------------------------------------------------

def test_resolve_party_mode_screen_prefers_named_screen():
    _app()
    screens = QtGui.QGuiApplication.screens()
    assert screens, "offscreen platform should still expose at least one QScreen"
    target = screens[0]
    resolved = resolve_party_mode_screen(target.name(), owner_window=None)
    assert resolved is target


def test_resolve_party_mode_screen_falls_back_to_primary_when_missing():
    _app()
    resolved = resolve_party_mode_screen("no-such-monitor-name", owner_window=None)
    assert resolved is QtGui.QGuiApplication.primaryScreen()


def test_format_screen_label_includes_resolution():
    _app()
    screen = QtGui.QGuiApplication.primaryScreen()
    label = format_screen_label(screen)
    geo = screen.geometry()
    assert str(geo.width()) in label
    assert str(geo.height()) in label


# ---------------------------------------------------------------------------
# Settings persistence (static source checks -- matches the convention
# established for track_transition_mode/diagnostics_level, since the full
# load/save methods touch far too many live Qt widgets to invoke directly)
# ---------------------------------------------------------------------------

def test_load_user_settings_persists_party_mode_settings():
    source = inspect.getsource(PlayerWindow._load_user_settings)
    for key, default in (
        ("party_mode_screen_name", '""'),
        ("party_mode_default_layout", '"lyrics"'),
        ("party_mode_show_up_next", "True"),
        ("party_mode_up_next_count", "3"),
        ("party_mode_show_clock", "True"),
        ("party_mode_show_remaining_playlist_time", "False"),
        ("party_mode_auto_hide_ms", "3000"),
        ("party_mode_animations_enabled", "True"),
        ("party_mode_visual_quality", '"medium"'),
    ):
        assert f'cfg.get("{key}"' in source, key
    assert "PARTY_MODE_LAYOUTS" in source
    assert "PARTY_MODE_VISUAL_QUALITIES" in source


def test_save_user_settings_persists_party_mode_settings():
    source = inspect.getsource(PlayerWindow._save_user_settings)
    for key in (
        "party_mode_screen_name",
        "party_mode_default_layout",
        "party_mode_show_up_next",
        "party_mode_up_next_count",
        "party_mode_show_clock",
        "party_mode_show_remaining_playlist_time",
        "party_mode_auto_hide_ms",
        "party_mode_animations_enabled",
        "party_mode_visual_quality",
    ):
        assert f'cfg["{key}"]' in source, key


def test_party_mode_layout_and_quality_constants_are_valid_tuples():
    assert PARTY_MODE_LAYOUTS == ("lyrics", "visualiser", "artwork")
    assert PARTY_MODE_VISUAL_QUALITIES == ("low", "medium", "high")


def test_tooltip_and_menu_popups_get_readable_dark_theme_colors():
    # PartyModeWindow is a separate top-level window from the main
    # PlayerWindow, so it never inherited the main window's dark-theme
    # stylesheet -- native QToolTip/QMenu popups (e.g. the waveform seek
    # bar's hover-time tooltip) fell back to unreadable default OS colors.
    _app()
    log = []
    owner = _make_owner(log)
    window = PartyModeWindow(owner)
    try:
        sheet = window.styleSheet()
        assert "QToolTip" in sheet
        assert "QMenu" in sheet
        assert "color:#eaf2ff" in sheet
    finally:
        window.deleteLater()


def test_visualiser_beat_double_click_and_escape_close_party_mode():
    # The embedded BeatWidget's own double-click/Escape are wired (in
    # window.py) to a separate main-window-only fullscreen feature that
    # Party Mode never connects to -- left alone, double-click silently
    # does nothing despite BeatWidget's tooltip claiming it exits full
    # screen, and clicking the equaliser (StrongFocus) swallows Escape
    # before PartyModeWindow's own close-on-Escape handler ever sees it.
    _app()
    log = []
    owner = _make_owner(log)
    window = PartyModeWindow(owner)
    try:
        window.show()
        assert window.isVisible()
        window.visualiser_layout.beat.fullscreen_toggle_requested.emit()
        assert not window.isVisible()

        window.show()
        assert window.isVisible()
        window.visualiser_layout.beat.fullscreen_exit_requested.emit()
        assert not window.isVisible()
    finally:
        window.deleteLater()


def test_visualiser_beat_has_no_artificial_size_cap():
    # This used to be capped at 1600x900 on the mistaken assumption that
    # BeatWidget's own paint cost was the source of Party Mode's lag -- real
    # per-consumer diagnostics proved that wrong (waveform_widget.py was the
    # actual cause, now fixed), so it should fill the whole screen again.
    _app()
    log = []
    owner = _make_owner(log)
    window = PartyModeWindow(owner)
    try:
        beat = window.visualiser_layout.beat
        assert beat.maximumWidth() >= 16777215 - 1
        assert beat.maximumHeight() >= 16777215 - 1
        assert beat.sizePolicy().horizontalPolicy() == QtWidgets.QSizePolicy.Policy.Expanding
        assert beat.sizePolicy().verticalPolicy() == QtWidgets.QSizePolicy.Policy.Expanding
    finally:
        window.deleteLater()


# ---------------------------------------------------------------------------
# Shutdown / crossfade-sync hooks (static source checks, matching
# test_shutdown_regressions.py's convention -- closeEvent touches session
# saving, diagnostics, and thread teardown, too much to construct in a test)
# ---------------------------------------------------------------------------

def test_close_event_closes_party_mode_like_mini_player():
    source = inspect.getsource(PlayerWindow.closeEvent)
    assert "self.party_mode._allow_close = True" in source
    assert "self.party_mode.close()" in source


def test_activate_track_ui_syncs_party_mode_for_crossfade_and_manual_changes():
    # _activate_track_ui is the single choke point both manual track changes
    # and crossfade completions (_begin_builtin_fade) run through, so hooking
    # it here covers both without needing separate crossfade-specific wiring.
    source = inspect.getsource(PlayerWindow._activate_track_ui)
    assert "self._sync_party_mode()" in source


def test_tick_syncs_party_mode_every_cycle():
    source = inspect.getsource(PlayerWindow._tick)
    assert "self._sync_party_mode()" in source


# ---------------------------------------------------------------------------
# Toggle/show/hide trio -- exercises the REAL unbound PlayerWindow methods
# against a SimpleNamespace fake, with billsmusic.window.PartyModeWindow
# monkeypatched so no full PlayerWindow (or real Qt window) is needed.
# ---------------------------------------------------------------------------

class _FakePartyModeInstance:
    def __init__(self, log):
        self._log = log
        self._visible = False

    def isVisible(self):
        return self._visible

    def open_party_mode(self):
        self._visible = True
        self._log.append("open")

    def hide(self):
        self._visible = False
        self._log.append("hide")


def test_toggle_party_mode_shows_when_hidden_and_hides_when_visible(monkeypatch):
    _app()
    log = []
    fake_instance = _FakePartyModeInstance(log)
    monkeypatch.setattr("billsmusic.window.PartyModeWindow", lambda owner: fake_instance)
    harness = SimpleNamespace(party_mode=None, diagnostics=None)
    harness._show_party_mode = lambda: PlayerWindow._show_party_mode(harness)
    harness._hide_party_mode = lambda: PlayerWindow._hide_party_mode(harness)

    PlayerWindow._toggle_party_mode(harness)
    assert log == ["open"]
    assert harness.party_mode is fake_instance

    PlayerWindow._toggle_party_mode(harness)
    assert log == ["open", "hide"]


def test_show_party_mode_creates_instance_only_once(monkeypatch):
    _app()
    created = []

    def factory(owner):
        created.append(owner)
        return _FakePartyModeInstance([])

    monkeypatch.setattr("billsmusic.window.PartyModeWindow", factory)
    harness = SimpleNamespace(party_mode=None, diagnostics=None)

    PlayerWindow._show_party_mode(harness)
    PlayerWindow._show_party_mode(harness)

    assert len(created) == 1  # second call reused the existing instance


# ---------------------------------------------------------------------------
# PartyModeWindow against a fake owner (real Qt widgets, offscreen)
# ---------------------------------------------------------------------------

def _make_action(recorder, name):
    action = QtGui.QAction()
    action.triggered.connect(lambda: recorder.append(name))
    return action


def _make_owner(app_actions_log):
    slider = QtWidgets.QSlider()
    slider.setRange(0, 1000)
    slider.setValue(400)
    label_remaining = QtWidgets.QLabel("-2:00")
    owner = SimpleNamespace(
        current_path="",
        _meta_by_path={},
        _playback_intentionally_paused=False,
        _playback_expected=False,
        slider_progress=slider,
        label_remaining=label_remaining,
        _last_progress_length_ms=200000,
        waveform_seekbar=None,
        queue=[],
        queue_played=[],
        _ensure_queue_played_flags=lambda: None,
        _lyrics=[],
        _lyric_idx=None,
        beat=SimpleNamespace(visual_mode="neon"),
        artwork_manager=None,
        _format_duration=_format_duration,
        _progress_release=lambda: app_actions_log.append("seek"),
        _save_user_settings=lambda: app_actions_log.append("save_settings"),
        action_previous=_make_action(app_actions_log, "previous"),
        action_play_pause_alternate=_make_action(app_actions_log, "play_pause"),
        action_next=_make_action(app_actions_log, "next"),
        action_volume_up=_make_action(app_actions_log, "volume_up"),
        action_volume_down=_make_action(app_actions_log, "volume_down"),
        party_mode_default_layout="lyrics",
        party_mode_show_up_next=True,
        party_mode_up_next_count=3,
        party_mode_show_clock=True,
        party_mode_show_remaining_playlist_time=False,
        party_mode_auto_hide_ms=3000,
        party_mode_animations_enabled=True,
        party_mode_visual_quality="medium",
        party_mode_screen_name="",
    )
    return owner


def test_sync_from_owner_updates_track_only_when_path_changes():
    _app()
    log = []
    owner = _make_owner(log)
    window = PartyModeWindow(owner)
    try:
        owner.current_path = "song_a.flac"
        owner._meta_by_path = {"song_a.flac": {"title": "Song A", "artist": "Artist A"}}
        window.sync_from_owner()
        assert window.info_bar.title_label.text() == "Song A"

        # Same path again: sync should be a no-op for the title (still correct either way).
        window.sync_from_owner()
        assert window.info_bar.title_label.text() == "Song A"

        owner.current_path = "song_b.flac"
        owner._meta_by_path = {"song_b.flac": {"title": "Song B", "artist": "Artist B"}}
        window.sync_from_owner()
        assert window.info_bar.title_label.text() == "Song B"
    finally:
        window.deleteLater()


def test_sync_from_owner_picks_up_lyric_index_changes_only():
    _app()
    log = []
    owner = _make_owner(log)
    owner._lyrics = [(0.0, "First line"), (5.0, "Second line"), (10.0, "Third line")]
    owner._lyric_idx = 0
    window = PartyModeWindow(owner)
    try:
        window.sync_from_owner()
        assert window.lyrics_layout.current_label.text() == "First line"
        assert window.lyrics_layout.next_label.text() == "Second line"

        owner._lyric_idx = 1
        window.sync_from_owner()
        assert window.lyrics_layout.current_label.text() == "Second line"
        assert window.lyrics_layout.previous_label.text() == "First line"
        assert window.lyrics_layout.next_label.text() == "Third line"
    finally:
        window.deleteLater()


# ---------------------------------------------------------------------------
# sync_from_owner diagnostics -- confirms the per-tick instrumentation only
# records when the call is actually slow (>=30ms), never on every tick,
# and stays a no-op when the owner has no diagnostics sink at all.
# ---------------------------------------------------------------------------

class _RecordingDiagnostics:
    def __init__(self):
        self.events = []

    def record(self, category, operation, **kwargs):
        self.events.append((category, operation, kwargs))

    def events_for(self, operation):
        return [e for e in self.events if e[1] == operation]


def test_sync_from_owner_does_not_record_when_fast():
    _app()
    log = []
    owner = _make_owner(log)
    owner.diagnostics = _RecordingDiagnostics()
    window = PartyModeWindow(owner)
    try:
        window.sync_from_owner()
        assert owner.diagnostics.events_for("sync_from_owner") == []
    finally:
        window.deleteLater()


def test_sync_from_owner_records_info_when_slow(monkeypatch):
    _app()
    log = []
    owner = _make_owner(log)
    owner.diagnostics = _RecordingDiagnostics()
    window = PartyModeWindow(owner)
    try:
        times = iter([0.0, 0.05])  # 50ms elapsed
        monkeypatch.setattr("billsmusic.party_mode.time.perf_counter", lambda: next(times))
        window.sync_from_owner(force=True)
        events = owner.diagnostics.events_for("sync_from_owner")
        assert len(events) == 1
        category, operation, kwargs = events[0]
        assert category == "party_mode"
        assert kwargs["severity"] == "info"
        assert kwargs["duration_ms"] == 50.0
        assert kwargs["details"]["force"] is True
    finally:
        window.deleteLater()


def test_sync_from_owner_records_warning_when_very_slow(monkeypatch):
    _app()
    log = []
    owner = _make_owner(log)
    owner.diagnostics = _RecordingDiagnostics()
    window = PartyModeWindow(owner)
    try:
        times = iter([0.0, 0.15])  # 150ms elapsed
        monkeypatch.setattr("billsmusic.party_mode.time.perf_counter", lambda: next(times))
        window.sync_from_owner()
        events = owner.diagnostics.events_for("sync_from_owner")
        assert len(events) == 1
        assert events[0][2]["severity"] == "warning"
    finally:
        window.deleteLater()


def test_sync_from_owner_is_a_no_op_without_diagnostics():
    # _make_owner doesn't set a diagnostics attribute at all, matching the
    # rest of this file's owner fixture -- sync_from_owner must not blow up.
    _app()
    log = []
    owner = _make_owner(log)
    window = PartyModeWindow(owner)
    try:
        window.sync_from_owner()  # should not raise
    finally:
        window.deleteLater()


def test_missing_lyrics_shows_fallback_message():
    _app()
    log = []
    owner = _make_owner(log)
    owner._lyrics = []
    owner._lyric_idx = None
    window = PartyModeWindow(owner)
    try:
        window.sync_from_owner(force=True)
        # The window is never shown() in this test, so isVisible() would be
        # False for every widget regardless of state; isHidden() reflects
        # the widget's own explicit setVisible() call instead.
        assert not window.lyrics_layout.empty_label.isHidden()
        assert window.lyrics_layout.current_label.isHidden()
    finally:
        window.deleteLater()


def test_empty_queue_shows_sensible_message():
    _app()
    log = []
    owner = _make_owner(log)
    owner.queue = []
    owner.queue_played = []
    window = PartyModeWindow(owner)
    try:
        window.sync_from_owner(force=True)
        assert window.info_bar.up_next_label.text() == "Queue is empty"
    finally:
        window.deleteLater()


def test_up_next_reflects_unplayed_queue_entries():
    _app()
    log = []
    owner = _make_owner(log)
    owner.queue = ["a.flac", "b.flac", "c.flac"]
    owner.queue_played = [True, False, False]
    owner._meta_by_path = {
        "b.flac": {"title": "Track B", "artist": "Artist B"},
        "c.flac": {"title": "Track C", "artist": ""},
    }
    owner.party_mode_up_next_count = 2
    window = PartyModeWindow(owner)
    try:
        window.sync_from_owner(force=True)
        text = window.info_bar.up_next_label.text()
        assert "Artist B" in text and "Track B" in text
        assert "Track C" in text
        assert "a.flac" not in text  # already played, must not appear
    finally:
        window.deleteLater()


def test_rapid_track_change_drops_stale_artwork_callback():
    _app()
    log = []
    owner = _make_owner(log)
    pending_callbacks = []

    class _FakeArtworkManager:
        def request(self, key, paths, disk_path, size, generation, callback):
            pending_callbacks.append((generation, callback))
            return True

    owner.artwork_manager = _FakeArtworkManager()
    window = PartyModeWindow(owner)
    try:
        owner.current_path = "track1.flac"
        window.sync_from_owner()
        owner.current_path = "track2.flac"
        window.sync_from_owner()
        assert len(pending_callbacks) == 2

        stale_generation, stale_callback = pending_callbacks[0]
        fresh_generation, fresh_callback = pending_callbacks[1]
        assert stale_generation != fresh_generation

        # Invoke the stale (superseded) callback last, as a real slow request
        # might: it must be dropped, not overwrite the current artwork.
        stale_result = SimpleNamespace(image=None, generation=stale_generation)
        window.info_bar.set_artwork = lambda pixmap: log.append(("stale_applied", pixmap))
        stale_callback(stale_result)
        assert not any(entry[0] == "stale_applied" for entry in log)
    finally:
        window.deleteLater()


def test_keyboard_commands_forward_to_owner_actions():
    _app()
    log = []
    owner = _make_owner(log)
    window = PartyModeWindow(owner)
    try:
        owner.action_previous.trigger()
        owner.action_next.trigger()
        owner.action_play_pause_alternate.trigger()
        owner.action_volume_up.trigger()
        owner.action_volume_down.trigger()
        assert log == ["previous", "next", "play_pause", "volume_up", "volume_down"]
    finally:
        window.deleteLater()


def test_event_filter_ignores_qwindow_mouse_move_without_crashing():
    # Regression: eventFilter is installed app-wide (QApplication.installEventFilter),
    # so it receives events for bare QWindow objects too (native window-manager
    # events, other top-level windows), not just QWidget. isAncestorOf() only
    # accepts a QWidget and previously raised TypeError on every single mouse
    # move anywhere in the whole application while Party Mode was open.
    _app()
    log = []
    owner = _make_owner(log)
    window = PartyModeWindow(owner)
    try:
        native_window = QtGui.QWindow()
        move_event = QtCore.QEvent(QtCore.QEvent.Type.MouseMove)
        result = window.eventFilter(native_window, move_event)  # must not raise
        assert result is False
    finally:
        window.deleteLater()


def test_event_filter_reveals_controls_for_widget_mouse_move():
    _app()
    log = []
    owner = _make_owner(log)
    window = PartyModeWindow(owner)
    try:
        move_event = QtCore.QEvent(QtCore.QEvent.Type.MouseMove)
        window.eventFilter(window.control_bar, move_event)
        # window is never shown() in this test, so isVisible() would be False
        # regardless; isHidden() reflects the widget's own setVisible() call.
        assert not window.control_bar.isHidden()
    finally:
        window.deleteLater()


def test_seek_relative_uses_existing_progress_release_command():
    _app()
    log = []
    owner = _make_owner(log)
    window = PartyModeWindow(owner)
    try:
        owner.slider_progress.setValue(500)  # 50% of a 1000-max slider
        window._seek_relative(10.0)
        assert "seek" in log  # went through owner._progress_release(), not a new engine call
    finally:
        window.deleteLater()


def test_set_layout_persists_setting_without_touching_playback():
    _app()
    log = []
    owner = _make_owner(log)
    window = PartyModeWindow(owner)
    try:
        window.set_layout("artwork")
        assert window.active_layout == "artwork"
        assert owner.party_mode_default_layout == "artwork"
        assert "save_settings" in log
        assert "play_pause" not in log and "next" not in log and "previous" not in log
    finally:
        window.deleteLater()


# -- cycling layout while video/karaoke is active must be able to return ----
# Reported: scrolling through layouts (the L key / _cycle_layout) while a
# video or karaoke track was showing had no way back to the picture -- only
# lyrics/artwork/visualiser were in the cycle, so _apply_layout would
# unconditionally switch the stack away from the video/karaoke widget with
# no path back short of stopping and restarting playback.

def test_cycle_layout_includes_video_stop_while_video_active():
    _app()
    log = []
    owner = _make_owner(log)
    window = PartyModeWindow(owner)
    try:
        window.show_video()
        assert window.stack.currentWidget() is window.video_widget
        assert window.active_layout == "video"

        window._cycle_layout()
        assert window.active_layout == "lyrics"
        assert window.stack.currentWidget() is window.lyrics_layout

        window._cycle_layout()
        assert window.active_layout == "artwork"
        window._cycle_layout()
        assert window.active_layout == "visualiser"

        # Fourth press must land back on the video picture -- this is the
        # "no way back" the report described.
        window._cycle_layout()
        assert window.active_layout == "video"
        assert window.stack.currentWidget() is window.video_widget
    finally:
        window.deleteLater()


def test_cycle_layout_includes_karaoke_stop_while_karaoke_active():
    _app()
    log = []
    owner = _make_owner(log)
    window = PartyModeWindow(owner)
    try:
        document = SimpleNamespace(image_at=lambda position_ms: QtGui.QImage())
        window.show_karaoke(document)
        assert window.stack.currentWidget() is window.karaoke_widget
        assert window.active_layout == "video"

        for _ in range(3):
            window._cycle_layout()
        assert window.active_layout == "visualiser"

        window._cycle_layout()
        assert window.active_layout == "video"
        assert window.stack.currentWidget() is window.karaoke_widget
    finally:
        window.deleteLater()


def test_cycle_layout_excludes_video_stop_when_video_inactive():
    # Without an active video/karaoke takeover, "video" must never appear
    # in the cycle -- there would be nothing to show.
    _app()
    log = []
    owner = _make_owner(log)
    window = PartyModeWindow(owner)
    try:
        assert window._video_active is False
        seen = set()
        for _ in range(6):
            window._cycle_layout()
            seen.add(window.active_layout)
        assert seen == {"lyrics", "artwork", "visualiser"}
    finally:
        window.deleteLater()


def test_cycling_away_from_video_does_not_end_the_takeover():
    # Scrolling to lyrics/artwork/visualiser must not stop video/karaoke
    # playback or clear _video_active -- it's still playing, just not the
    # currently displayed page.
    _app()
    log = []
    owner = _make_owner(log)
    window = PartyModeWindow(owner)
    try:
        window.show_video()
        window._cycle_layout()
        assert window.active_layout == "lyrics"
        assert window._video_active is True
        assert window._video_active_widget is window.video_widget
    finally:
        window.deleteLater()


def test_cycling_to_video_does_not_persist_as_the_default_layout():
    _app()
    log = []
    owner = _make_owner(log)
    window = PartyModeWindow(owner)
    try:
        owner.party_mode_default_layout = "lyrics"
        window.show_video()
        # lyrics -> artwork -> visualiser -> video: the first 3 hops land on
        # real, persistable layouts (existing behaviour, unaffected by this
        # fix); only the 4th hop, landing on "video", must not overwrite
        # that persisted default -- "video" wouldn't mean anything on next
        # launch without an active video/karaoke track.
        for _ in range(3):
            window._cycle_layout()
        assert owner.party_mode_default_layout == "visualiser"

        window._cycle_layout()
        assert window.active_layout == "video"
        assert owner.party_mode_default_layout == "visualiser"
    finally:
        window.deleteLater()


def test_return_to_normal_layout_after_cycling_restores_pre_video_layout():
    _app()
    log = []
    owner = _make_owner(log)
    window = PartyModeWindow(owner)
    try:
        window.set_layout("artwork")
        window.show_video()
        window._cycle_layout()  # away from video, to lyrics
        assert window._video_active is True

        window.return_to_normal_layout()

        assert window._video_active is False
        assert window._video_active_widget is None
        # Restores what was selected *before* video started, not wherever
        # the user happened to be cycling through at the moment it ended.
        assert window.active_layout == "artwork"
        assert window.stack.currentWidget() is window.artwork_layout
    finally:
        window.deleteLater()


def test_repeated_open_close_reuses_same_layout_widgets():
    # Guards against accumulating timers/widgets: the same PartyModeWindow
    # instance's child widgets must be identical objects across repeated
    # open/close, not rebuilt each time.
    _app()
    log = []
    owner = _make_owner(log)
    window = PartyModeWindow(owner)
    try:
        lyrics_widget_id = id(window.lyrics_layout)
        info_bar_id = id(window.info_bar)
        for _ in range(3):
            window.sync_from_owner(force=True)
            window.hide()
        assert id(window.lyrics_layout) == lyrics_widget_id
        assert id(window.info_bar) == info_bar_id
    finally:
        window.deleteLater()


# ---------------------------------------------------------------------------
# Regression: the visualiser's BeatWidget must actually fill a meaningful
# area of the screen, not collapse to its near-zero natural width. It has no
# minimum width of its own and a Preferred (not Expanding) size policy by
# default, so wrapping it in stretch-padded layouts to "center" it shrank it
# down to a sliver -- rendered, but imperceptible ("no visualiser shown").
# ---------------------------------------------------------------------------

def test_visualiser_beat_widget_fills_available_space():
    _app()
    log = []
    owner = _make_owner(log)
    window = PartyModeWindow(owner)
    try:
        window.resize(1920, 1080)
        window.set_layout("visualiser")
        window.show()
        QtWidgets.QApplication.processEvents()
        assert window.visualiser_layout.beat.width() > 300
        assert window.visualiser_layout.beat.height() > 200
    finally:
        window.hide()
        window.deleteLater()


# ---------------------------------------------------------------------------
# Regression: control bar buttons must use plain text, not symbol/emoji
# glyphs that can render as unreadable blank boxes on systems/fonts that
# don't support them.
# ---------------------------------------------------------------------------

def test_control_bar_buttons_use_plain_text_not_symbol_glyphs():
    _app()
    log = []
    owner = _make_owner(log)
    window = PartyModeWindow(owner)
    try:
        bar = window.control_bar
        for button in (bar.btn_previous, bar.btn_play_pause, bar.btn_next, bar.btn_layout):
            text = button.text()
            assert text, "button must have visible text"
            assert all(ord(ch) < 128 for ch in text), (
                f"button text {text!r} contains a non-ASCII glyph that may not render"
            )
    finally:
        window.deleteLater()


# ---------------------------------------------------------------------------
# Regression: visualiser paint rate must be throttled by the quality
# preference (BeatWidget otherwise always repaints at a fixed 16ms/60fps
# regardless of how large the widget is, which is visibly laggy filling an
# entire large/4K display).
# ---------------------------------------------------------------------------

def test_visualiser_interval_scales_with_quality():
    low = _visualiser_interval_ms_for_quality("low")
    medium = _visualiser_interval_ms_for_quality("medium")
    high = _visualiser_interval_ms_for_quality("high")
    assert low > medium > high
    assert high == 16  # native BeatWidget rate, unthrottled


def test_switching_to_visualiser_layout_applies_quality_throttle():
    _app()
    log = []
    owner = _make_owner(log)
    owner.party_mode_visual_quality = "low"
    window = PartyModeWindow(owner)
    try:
        window.set_layout("visualiser")
        expected = _visualiser_interval_ms_for_quality("low")
        assert window.visualiser_layout.beat._timer.interval() == expected
    finally:
        window.deleteLater()


def test_apply_preferences_updates_visualiser_throttle_while_open():
    _app()
    log = []
    owner = _make_owner(log)
    window = PartyModeWindow(owner)
    try:
        window.set_layout("visualiser")
        owner.party_mode_visual_quality = "high"
        window.apply_preferences()
        assert window.visualiser_layout.beat._timer.interval() == 16
    finally:
        window.deleteLater()
