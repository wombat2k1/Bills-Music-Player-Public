"""Regression coverage for reparent-free video fullscreen and teardown."""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtTest import QTest

from billsmusic.media_type import MediaType
from billsmusic.window import PlayerWindow
from billsmusic.worker_registry import WorkerLifetimeRegistry


_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _main_video_window(source="local", menu_bar_visible=False):
    """Reproduces the REAL _build_ui nested structure (Phase C2.1
    acceptance defect fix) -- not a synthetic vertical stack. The
    original fixture here stacked every widget vertically in one
    QVBoxLayout, so it could never detect the real defect: the real app
    nests a QHBoxLayout ("mid") with the library region on the left
    (stretch 3) and the video/queue region on the right (stretch 4).
    Hiding every widget INSIDE the library region individually (the old
    fix attempt) still left its stretch-allocated ~3/7 of fullscreen
    width empty, because a hidden nested QLayout item is not the same as
    a hidden WIDGET to QHBoxLayout's own space-reclaiming logic -- only a
    widget's hidden state collapses its layout-allocated space. Building
    this fixture with the same nesting (library_column_container as a
    real QWidget, added via mid.addWidget(..., 3)) is what lets a test
    here actually catch that class of regression again.

    ``source`` mirrors library_source_selector's real state ("local" or
    "plex" -- Refresh Plex only ever shows for "plex", matching
    _on_library_source_changed). ``menu_bar_visible`` mirrors a session
    with View > Show Menu Bar enabled (self.top_menu_visible)."""
    app = _app()
    window = QtWidgets.QMainWindow()
    central = QtWidgets.QWidget()
    root = QtWidgets.QVBoxLayout(central)
    root.setContentsMargins(18, 16, 18, 16)
    root.setSpacing(12)
    window.setCentralWidget(central)

    header = QtWidgets.QHBoxLayout()
    window.now_playing = QtWidgets.QLabel("Now playing")
    header.addWidget(window.now_playing)
    window.dj_info = QtWidgets.QLabel("Backend: ready")
    header.addWidget(window.dj_info)
    window.search_box = QtWidgets.QLineEdit()
    header.addWidget(window.search_box)
    root.addLayout(header)

    window.scan_bar = QtWidgets.QProgressBar()
    root.addWidget(window.scan_bar)

    mid = QtWidgets.QHBoxLayout()

    source_row = QtWidgets.QHBoxLayout()
    window.library_source_label = QtWidgets.QLabel("Source:")
    source_row.addWidget(window.library_source_label)
    window.library_source_selector = QtWidgets.QComboBox()
    source_row.addWidget(window.library_source_selector)
    window.library_plex_refresh_button = QtWidgets.QPushButton("Refresh Plex")
    window.library_plex_refresh_button.setVisible(source == "plex")
    source_row.addWidget(window.library_plex_refresh_button)
    window.library_plex_placeholder = QtWidgets.QLabel("No Plex libraries configured yet")
    window.library_plex_placeholder.setVisible(False)
    window.library_tabs = QtWidgets.QTabWidget()
    window.library_tabs.addTab(QtWidgets.QWidget(), "Music")
    window.library_column_container = QtWidgets.QWidget()
    library_column = QtWidgets.QVBoxLayout(window.library_column_container)
    library_column.setContentsMargins(0, 0, 0, 0)
    library_column.addLayout(source_row)
    library_column.addWidget(window.library_plex_placeholder)
    library_column.addWidget(window.library_tabs)
    mid.addWidget(window.library_column_container, 3)
    # Free-floating, never part of any visible layout in the real app
    # either (moved into the library right-click menu) -- always hidden.
    window.btn_add = QtWidgets.QPushButton("Add Folder")
    window.btn_add.setVisible(False)
    window.btn_rescan = QtWidgets.QPushButton("Rescan")
    window.btn_rescan.setVisible(False)

    right = QtWidgets.QVBoxLayout()
    window.right_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
    page = QtWidgets.QWidget()
    page.setLayout(QtWidgets.QVBoxLayout())
    video_output = QtWidgets.QWidget()
    page.layout().addWidget(video_output)
    # The real right_display_stack nesting (see _build_ui): the STACK is
    # right_splitter's child, and the visualiser frame / video pages are
    # PAGES inside it. Reproduced faithfully because the visualiser
    # fullscreen path pulls its page out of this stack, and getting the
    # page back into the right parent on exit is exactly the defect under
    # test (a stale "Loading video..." page otherwise ends up visible).
    window.right_display_stack = QtWidgets.QStackedWidget()
    window.visualiser_frame = QtWidgets.QLabel("VISUALISER")
    window._normal_display_page = window.visualiser_frame
    window._video_loading_page = QtWidgets.QLabel("Loading video...")
    window.right_display_stack.addWidget(window.visualiser_frame)
    window.right_display_stack.addWidget(window._video_loading_page)
    window.right_display_stack.addWidget(page)
    window.right_display_stack.setCurrentWidget(page)
    window.right_tabs = QtWidgets.QTabWidget()
    window.right_tabs.addTab(QtWidgets.QWidget(), "Up Next")
    window.right_splitter.addWidget(window.right_display_stack)
    window.right_splitter.addWidget(window.right_tabs)
    window.right_splitter.setSizes([650, 350])
    right.addWidget(window.right_splitter, 1)
    mid.addLayout(right, 4)

    root.addLayout(mid, 1)

    controls = QtWidgets.QHBoxLayout()
    window.btn_prev = QtWidgets.QPushButton("Back")
    window.btn_play = QtWidgets.QPushButton("Play")
    window.btn_pause = QtWidgets.QPushButton("Pause")
    window.btn_next = QtWidgets.QPushButton("Next")
    for b in (window.btn_prev, window.btn_play, window.btn_pause, window.btn_next):
        controls.addWidget(b)
    window.output_combo = QtWidgets.QComboBox()
    controls.addWidget(window.output_combo)
    window.slider_progress = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
    controls.addWidget(window.slider_progress, 2)
    window.label_remaining = QtWidgets.QLabel("-0:00")
    controls.addWidget(window.label_remaining)
    window.label_volume = QtWidgets.QLabel("Volume")
    controls.addWidget(window.label_volume)
    window.slider_volume = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
    controls.addWidget(window.slider_volume)
    root.addLayout(controls)

    for menu_name in ("&Playback", "&Edit", "&View", "&Playlist", "&Help"):
        window.menuBar().addMenu(menu_name)
    window.menuBar().setVisible(menu_bar_visible)

    window.resize(1400, 900)
    window.show()
    app.processEvents()

    backend = MagicMock()
    window._current_media_type = MediaType.VIDEO
    window._video_fullscreen = False
    window._video_fullscreen_owner_widget = None
    window._video_fullscreen_restore_state = None
    window._video_output_page = page
    window.video_output_widget = video_output
    window._video_backend = backend
    window.party_mode = None
    window.diagnostic_events = []
    window.diagnostics = SimpleNamespace(
        record=lambda category, operation, **kw: window.diagnostic_events.append(
            (category, operation, kw.get("details") or {})
        ),
        path_details=lambda path: {},
    )
    # Fullscreen transition lifecycle state + the real request/coalescing
    # seam, so the hostile-input tests below exercise the genuine article
    # rather than a re-implementation of it.
    window._video_fullscreen_transitioning = False
    window._video_fullscreen_pending_target = None
    window._video_fullscreen_request_scheduled = False
    window._video_fullscreen_sequence = 0
    window._video_fullscreen_transition_target = None
    window._drain_pending_video_fullscreen_request = (
        lambda: PlayerWindow._drain_pending_video_fullscreen_request(window)
    )
    window._video_fullscreen_requests_dead = (
        lambda: PlayerWindow._video_fullscreen_requests_dead(window)
    )
    window._video_fullscreen_effective_intent = (
        lambda: PlayerWindow._video_fullscreen_effective_intent(window)
    )
    window._record_video_fullscreen_event = (
        lambda operation, **details: PlayerWindow._record_video_fullscreen_event(
            window, operation, **details
        )
    )
    window._next_video_fullscreen_sequence = (
        lambda: PlayerWindow._next_video_fullscreen_sequence(window)
    )
    window._request_video_fullscreen_state = (
        lambda target, source="unknown": PlayerWindow._request_video_fullscreen_state(
            window, target, source
        )
    )
    window._apply_pending_video_fullscreen_state = (
        lambda: PlayerWindow._apply_pending_video_fullscreen_state(window)
    )
    window._toggle_video_fullscreen = lambda: PlayerWindow._toggle_video_fullscreen(window)
    window._enter_video_fullscreen = lambda: PlayerWindow._enter_video_fullscreen(window)
    window._exit_video_fullscreen = lambda: PlayerWindow._exit_video_fullscreen(window)
    window._on_child_video_double_clicked = (
        lambda *a: PlayerWindow._on_child_video_double_clicked(window, *a)
    )
    window._on_child_video_escape_pressed = (
        lambda *a: PlayerWindow._on_child_video_escape_pressed(window, *a)
    )
    window._on_video_fullscreen_menu_requested = (
        lambda *a: PlayerWindow._on_video_fullscreen_menu_requested(window, *a)
    )
    window._video_fullscreen_stage_owner = lambda: PlayerWindow._video_fullscreen_stage_owner(window)
    window._main_video_fullscreen_hidden_widgets = (
        lambda: PlayerWindow._main_video_fullscreen_hidden_widgets(window)
    )
    window._enter_main_video_fullscreen_presentation = (
        lambda: PlayerWindow._enter_main_video_fullscreen_presentation(window)
    )
    window._exit_main_video_fullscreen_presentation = (
        lambda state: PlayerWindow._exit_main_video_fullscreen_presentation(window, state)
    )
    # Visualiser fullscreen shares this window/splitter/stack but keeps
    # its own per-session snapshot -- bound with the real, unbound methods
    # so the cross-mode regressions below exercise the genuine article.
    window._visualiser_fullscreen = False
    window._visualiser_fullscreen_snapshot = None
    window._visualiser_splitter_state = None
    window._splitter_save_timer = None
    window.beat = SimpleNamespace(setFocus=lambda *a, **kw: None)
    window._refresh_visualiser_lifecycle = lambda reason: None
    window._show_normal_display_page = (
        lambda: PlayerWindow._show_normal_display_page(window)
    )
    window._reconcile_display_stage_with_current_media = (
        lambda: PlayerWindow._reconcile_display_stage_with_current_media(window)
    )
    window._remember_right_splitter_state = (
        lambda *a: PlayerWindow._remember_right_splitter_state(window, *a)
    )
    window._restore_right_splitter_position = (
        lambda: PlayerWindow._restore_right_splitter_position(window)
    )
    window._enter_visualiser_fullscreen = (
        lambda: PlayerWindow._enter_visualiser_fullscreen(window)
    )
    window._exit_visualiser_fullscreen = (
        lambda: PlayerWindow._exit_visualiser_fullscreen(window)
    )
    return app, window, page, video_output, backend


def _window_state_spy(window):
    """Records every window-state-changing call the production code makes
    (self.showNormal()/showMaximized()/setWindowState() all resolve to
    these instance attributes first), and the resulting state after each,
    so a test can prove no windowed intermediate was ever passed
    through."""
    calls = []
    real_show_normal = window.showNormal
    real_show_maximized = window.showMaximized
    real_set_state = window.setWindowState

    def _show_normal():
        calls.append(("showNormal", None))
        real_show_normal()

    def _show_maximized():
        calls.append(("showMaximized", None))
        real_show_maximized()

    def _set_window_state(state):
        calls.append(("setWindowState", state))
        real_set_state(state)

    window.showNormal = _show_normal
    window.showMaximized = _show_maximized
    window.setWindowState = _set_window_state
    return calls


def test_main_fullscreen_expands_existing_stage_without_reparenting_video():
    app, window, page, video_output, backend = _main_video_window()
    original_parent = video_output.parentWidget()
    original_margins = window.centralWidget().layout().getContentsMargins()

    PlayerWindow._enter_video_fullscreen(window)
    QTest.qWait(125)

    assert window._video_fullscreen is True
    assert window.isFullScreen()
    assert video_output.parentWidget() is original_parent
    backend.attach_output.assert_not_called()
    assert window.now_playing.isHidden()
    assert window.right_tabs.isHidden()
    # Phase C2.1 acceptance defect (real root cause): the whole library
    # region -- source row + tabs -- is now ONE container widget, hidden
    # as a single unit (see test_main_fullscreen_collapses_the_library_
    # column_and_uses_full_width below for the geometric proof that this
    # actually reclaims the space, not just isHidden() on a child).
    assert window.library_column_container.isHidden()
    assert not window.library_tabs.isVisible()  # effectively invisible via its hidden ancestor
    assert not window.library_source_label.isVisible()
    assert not window.library_source_selector.isVisible()
    assert window.btn_add.isHidden()
    assert window.btn_rescan.isHidden()
    assert window.menuBar().isHidden()  # was already hidden (default session) -- stays hidden
    assert window.centralWidget().layout().getContentsMargins() == (0, 0, 0, 0)
    backend.schedule_output_geometry_sync.assert_called_once()

    PlayerWindow._exit_video_fullscreen(window)
    app.processEvents()
    assert window._video_fullscreen is False
    assert not window.isFullScreen()
    assert video_output.parentWidget() is original_parent
    backend.attach_output.assert_not_called()
    assert not window.now_playing.isHidden()
    assert not window.right_tabs.isHidden()
    assert not window.library_column_container.isHidden()
    assert window.library_tabs.isVisible()
    assert window.library_source_label.isVisible()
    assert window.library_source_selector.isVisible()
    # btn_add/btn_rescan are never part of any visible layout in the real
    # app either (moved to the library right-click menu) -- always hidden,
    # fullscreen entry/exit must not be what shows them.
    assert window.btn_add.isHidden()
    assert window.btn_rescan.isHidden()
    assert window.menuBar().isHidden()  # never revealed -- it was hidden before fullscreen too
    assert window.centralWidget().layout().getContentsMargins() == original_margins
    assert backend.schedule_output_geometry_sync.call_count == 2
    window.close()


def test_main_fullscreen_collapses_the_library_column_and_uses_full_width():
    # The actual acceptance defect, proven geometrically: hiding every
    # WIDGET inside the library region individually (the previous, still-
    # broken fix attempt) is not enough, because library_column was added
    # to `mid` (QHBoxLayout) as a bare nested QLayout item -- only a
    # hidden WIDGET's stretch-allocated space is reclaimed by QHBoxLayout;
    # a QLayout item has no "hidden" concept at all, so it kept its
    # stretch-3-of-7 share of fullscreen width regardless. Confirmed
    # against Bill's real screenshot: a large blank area on the left,
    # video confined to the right-hand ~4/7 portion. Do NOT reduce this to
    # `library_tabs.isHidden()` -- that is exactly the assertion the
    # original (pre-fix) version of this test used, and it could not see
    # this defect at all.
    app, window, page, video_output, backend = _main_video_window()

    PlayerWindow._enter_video_fullscreen(window)
    QTest.qWait(150)
    app.processEvents()

    central_width = window.centralWidget().width()
    assert central_width > 0
    # A hidden widget's own .width() is not reliably reset to 0 by Qt (a
    # layout simply stops consulting a hidden item's sizeHint/stretch when
    # distributing space -- it does not necessarily zero the item's own
    # stale geometry) -- the real proof this defect is fixed is that the
    # SIBLING (right_splitter) actually grew to consume the reclaimed
    # width, checked just below via its x position and width.
    assert window.library_column_container.isHidden()
    # The right/video region starts essentially at the usable area's left
    # edge (root margins are zeroed for fullscreen) and consumes
    # essentially all of it -- not ~4/7 with ~3/7 still reserved and empty.
    assert window.right_splitter.x() <= 2
    assert window.right_splitter.width() >= central_width * 0.9
    # Video page itself fills the region the splitter gives it.
    assert page.x() <= 2
    assert page.width() >= central_width * 0.9
    assert window.right_tabs.isHidden()  # queue hidden

    PlayerWindow._exit_video_fullscreen(window)
    app.processEvents()

    # Exact pre-fullscreen horizontal proportions restored (library
    # region visible and back to occupying real width again).
    assert not window.library_column_container.isHidden()
    assert window.library_column_container.width() > 0
    lib_width = window.library_column_container.width()
    right_width = window.right_splitter.width()
    # 3:4 stretch -- allow a tolerant band rather than an exact pixel
    # ratio (Qt layout rounding), but it must be nowhere near collapsed.
    ratio = lib_width / float(lib_width + right_width)
    assert 0.30 <= ratio <= 0.50
    window.close()


def test_main_fullscreen_hides_a_visible_menu_bar_and_restores_it():
    # Bill's real acceptance screenshot showed Playback/Edit/View/
    # Playlist/Help still visible in fullscreen -- a session with View >
    # Show Menu Bar enabled (self.top_menu_visible = True) keeps
    # self.menuBar() visible at all times outside fullscreen; fullscreen
    # must hide it like everything else and restore it on exit.
    app, window, page, video_output, backend = _main_video_window(menu_bar_visible=True)
    assert not window.menuBar().isHidden()

    PlayerWindow._enter_video_fullscreen(window)
    QTest.qWait(50)
    assert window.menuBar().isHidden()

    PlayerWindow._exit_video_fullscreen(window)
    app.processEvents()
    assert not window.menuBar().isHidden()
    window.close()


def test_main_fullscreen_menu_bar_already_hidden_stays_hidden():
    # Control, mirroring the existing library_plex_refresh_button test:
    # the default (top menu bar disabled) session must not have fullscreen
    # entry/exit *reveal* a menu bar that was correctly hidden all along.
    app, window, page, video_output, backend = _main_video_window(menu_bar_visible=False)
    assert window.menuBar().isHidden()

    PlayerWindow._enter_video_fullscreen(window)
    QTest.qWait(50)
    assert window.menuBar().isHidden()

    PlayerWindow._exit_video_fullscreen(window)
    app.processEvents()
    assert window.menuBar().isHidden()
    window.close()


def test_main_fullscreen_source_plex_refresh_button_visible_before_and_after():
    # Source=Plex: "Refresh Plex" is genuinely visible before fullscreen
    # (see _on_library_source_changed) -- it lives inside
    # library_column_container now, so hiding the container hides it too
    # without any Plex-specific code in the fullscreen path, and exit
    # must bring it back visible (it was visible before), not leave it
    # hidden the way a naive "always hide, never distinguish" fix would.
    app, window, page, video_output, backend = _main_video_window(source="plex")
    assert window.library_plex_refresh_button.isVisible()

    PlayerWindow._enter_video_fullscreen(window)
    QTest.qWait(50)
    assert not window.library_plex_refresh_button.isVisible()  # hidden via its container

    PlayerWindow._exit_video_fullscreen(window)
    app.processEvents()
    assert window.library_plex_refresh_button.isVisible()
    window.close()


def test_fullscreen_source_controls_stay_correctly_hidden_when_already_hidden():
    # Control: library_plex_refresh_button is only ever visible for
    # source=="plex" in real use -- fullscreen entry/exit must not
    # accidentally *reveal* it for a Local session that had it correctly
    # hidden all along.
    app, window, page, video_output, backend = _main_video_window()
    window.library_plex_refresh_button.hide()
    app.processEvents()

    PlayerWindow._enter_video_fullscreen(window)
    QTest.qWait(50)
    assert window.library_plex_refresh_button.isHidden()

    PlayerWindow._exit_video_fullscreen(window)
    app.processEvents()
    assert window.library_plex_refresh_button.isHidden()
    window.close()


def test_fullscreen_exit_restores_splitter_proportions_explicitly():
    # Stage 3A-r3 real-device defect: hiding right_tabs during fullscreen
    # lets QSplitter redistribute its space entirely to the video stage --
    # merely calling setVisible(True) again on exit is not guaranteed to
    # reproduce the original pixel split (this is what "queue is pushed
    # down, video/container geometry is wrong" after exiting fullscreen
    # traced back to). Proven here by deliberately perturbing the
    # splitter's sizes *while* right_tabs is hidden (simulating whatever
    # real-world layout churn happens during a real fullscreen session)
    # and confirming exit still lands on the pre-fullscreen proportions.
    app, window, page, video_output, backend = _main_video_window()
    # Sized to fit this environment's (800x800) offscreen virtual screen:
    # the fixture's default 1400x900 is deliberately wider than the screen
    # for the width-collapse test above, and a window larger than the
    # screen gets clamped by the platform across a real window-state
    # transition, which would drift the absolute pixel totals for reasons
    # that have nothing to do with the restore ordering under test.
    window.resize(600, 500)
    app.processEvents()
    QTest.qWait(60)
    original_sizes = list(window.right_splitter.sizes())
    assert sum(original_sizes) > 0

    PlayerWindow._enter_video_fullscreen(window)
    QTest.qWait(50)
    # Perturb sizes while right_tabs is hidden -- exactly the kind of
    # drift Qt's own show/hide bookkeeping does not promise to undo.
    window.right_splitter.setSizes([sum(original_sizes), 0])
    app.processEvents()

    PlayerWindow._exit_video_fullscreen(window)
    app.processEvents()
    QTest.qWait(50)

    # Exact pixel equality: the captured sizes are now applied AFTER the
    # single window-state transition (see
    # _exit_main_video_fullscreen_presentation), so QSplitter fits them to
    # the restored window size rather than to the fullscreen size and
    # then re-proportioning them on the way back down -- which is what
    # used to make the absolute totals drift.
    assert list(window.right_splitter.sizes()) == original_sizes
    window.close()


# ---------------------------------------------------------------------------
# Fullscreen EXIT restoration (real screen recording, ~14.0-14.5s):
# exiting fullscreen from a maximised window visibly passed through a
# half-restored fullscreen-size layout, then a small windowed frame, then
# a maximise -- three visible states for one logical transition.
# ---------------------------------------------------------------------------

def test_maximised_video_fullscreen_exit_never_passes_through_windowed_state():
    app, window, page, video_output, backend = _main_video_window()
    window.showMaximized()
    app.processEvents()
    QTest.qWait(60)
    assert window.isMaximized()
    geometry_before = window.geometry()
    splitter_before = list(window.right_splitter.sizes())

    PlayerWindow._enter_video_fullscreen(window)
    QTest.qWait(120)
    app.processEvents()
    assert window.isFullScreen()

    calls = _window_state_spy(window)
    PlayerWindow._exit_video_fullscreen(window)
    app.processEvents()
    QTest.qWait(120)
    app.processEvents()

    # The actual defect: showNormal() forced a windowed state the window
    # was never in, purely so setWindowState() could undo it a moment
    # later -- visible as a small desktop window that then maximises.
    assert not any(name == "showNormal" for name, _arg in calls), (
        f"exit must not pass through a windowed state; calls were {calls}"
    )
    # Exactly one window-state transition, straight to the captured state.
    state_calls = [arg for name, arg in calls if name == "setWindowState"]
    assert len(state_calls) == 1
    assert state_calls[0] & QtCore.Qt.WindowState.WindowMaximized
    assert not (state_calls[0] & QtCore.Qt.WindowState.WindowFullScreen)

    assert window.isMaximized()
    assert not window.isFullScreen()
    assert window.geometry() == geometry_before
    assert list(window.right_splitter.sizes()) == splitter_before
    assert not window.library_column_container.isHidden()
    assert not window.right_tabs.isHidden()
    assert window.menuBar().isHidden()  # was hidden before -- stays hidden
    assert window.updatesEnabled()  # repaint suppression always released
    window.close()


def test_windowed_video_fullscreen_exit_restores_the_same_windowed_state():
    # Control for the above: exit must NOT hard-code "always maximise" --
    # a genuinely windowed session comes back windowed, at its own size.
    app, window, page, video_output, backend = _main_video_window()
    window.resize(600, 500)
    app.processEvents()
    QTest.qWait(60)
    assert not window.isMaximized()
    size_before = window.size()
    splitter_before = list(window.right_splitter.sizes())

    PlayerWindow._enter_video_fullscreen(window)
    QTest.qWait(120)
    app.processEvents()
    assert window.isFullScreen()

    calls = _window_state_spy(window)
    PlayerWindow._exit_video_fullscreen(window)
    app.processEvents()
    QTest.qWait(120)
    app.processEvents()

    assert not window.isFullScreen()
    assert not window.isMaximized()
    state_calls = [arg for name, arg in calls if name == "setWindowState"]
    assert len(state_calls) == 1
    assert not (state_calls[0] & QtCore.Qt.WindowState.WindowMaximized)
    assert not (state_calls[0] & QtCore.Qt.WindowState.WindowFullScreen)
    assert window.size() == size_before
    assert list(window.right_splitter.sizes()) == splitter_before
    assert window.updatesEnabled()
    window.close()


def test_video_fullscreen_exit_suppresses_repaints_across_the_restore():
    # The layout/widget/splitter restoration must not be painted at
    # fullscreen size ("video shrinks into the right side of a mostly
    # black screen" was that half-restored frame). Proven by observing
    # updatesEnabled() from inside the restore itself: the widget-
    # visibility restore runs while repaints are suppressed.
    app, window, page, video_output, backend = _main_video_window()
    observed = []

    class _Probe(QtWidgets.QWidget):
        def setVisible(self, visible):
            observed.append((visible, window.updatesEnabled()))
            super().setVisible(visible)

    probe = _Probe()
    probe.setVisible(True)
    observed.clear()
    window.now_playing = probe  # picked up by _main_video_fullscreen_hidden_widgets

    PlayerWindow._enter_video_fullscreen(window)
    QTest.qWait(60)
    observed.clear()
    PlayerWindow._exit_video_fullscreen(window)
    app.processEvents()

    restore_observations = [enabled for _visible, enabled in observed]
    assert restore_observations, "widget visibility restore never ran"
    assert all(enabled is False for enabled in restore_observations), (
        f"restore must run with repaints suppressed; saw {observed}"
    )
    assert window.updatesEnabled()  # released afterwards
    window.close()


def _switch_to_audio(window, app):
    """Presentation-level equivalent of a Plex AUDIO track taking over
    (what _stop_video_for_audio_transition does): media type becomes
    AUDIO and the display stage returns to the visualiser page. No
    transport/playback involved."""
    window._current_media_type = MediaType.AUDIO
    window._show_normal_display_page()
    app.processEvents()


def test_visualiser_fullscreen_keeps_page_in_stack_and_fullscreens_main_window():
    # Real-device regression (2026-09-12, ~29.8-30.4s): the old visualiser
    # fullscreen path made visualiser_frame itself a top-level window and
    # reparented it back on exit. On Windows the top-level geometry/native
    # state survived that hand-off, producing a brief stand-alone window and
    # then a narrow/clipped visualiser inside the restored application.
    # Fullscreen presentation must therefore keep the visualiser page in its
    # QStackedWidget for the entire cycle and fullscreen the main window
    # around the existing stage, just like video fullscreen does.
    app, window, page, video_output, backend = _main_video_window()
    _switch_to_audio(window, app)

    frame = window.visualiser_frame
    stack = window.right_display_stack
    original_parent = frame.parentWidget()
    original_index = stack.indexOf(frame)
    original_count = stack.count()

    PlayerWindow._enter_visualiser_fullscreen(window)
    app.processEvents()
    QTest.qWait(60)

    assert window._visualiser_fullscreen is True
    assert window.isFullScreen()
    assert frame.parentWidget() is original_parent is stack
    assert stack.indexOf(frame) == original_index
    assert stack.count() == original_count
    assert not frame.isWindow()

    PlayerWindow._exit_visualiser_fullscreen(window)
    app.processEvents()
    QTest.qWait(60)

    assert window._visualiser_fullscreen is False
    assert window._visualiser_fullscreen_snapshot is None
    assert not window.isFullScreen()
    assert frame.parentWidget() is stack
    assert stack.indexOf(frame) == original_index
    assert stack.count() == original_count
    assert window.right_splitter.count() == 2
    assert window.right_display_stack.currentWidget() is frame
    assert not frame.isWindow()
    window.close()


def test_video_fullscreen_then_audio_visualiser_fullscreen_leaves_no_video_pane():
    # Real screen recording, ~35s: with Plex AUDIO playing (visualiser +
    # Up Next, no video pane anywhere), entering and leaving visualiser
    # fullscreen brought back an extra panel reading "Loading video...".
    # Root cause: visualiser_frame is a PAGE inside right_display_stack,
    # so pulling it out for fullscreen made QStackedWidget auto-advance to
    # its next page (the video loading placeholder), and exit re-inserted
    # the frame as a THIRD right_splitter child instead of back into the
    # stack -- leaving the orphaned stack visible underneath it.
    app, window, page, video_output, backend = _main_video_window()

    # -- Plex video: fullscreen in and out --
    window._current_media_type = MediaType.VIDEO
    window.right_display_stack.setCurrentWidget(page)
    app.processEvents()
    PlayerWindow._enter_video_fullscreen(window)
    QTest.qWait(80)
    PlayerWindow._exit_video_fullscreen(window)
    app.processEvents()
    QTest.qWait(80)
    assert window._video_fullscreen_restore_state is None  # consumed and cleared

    # -- switch to Plex audio: video pane must be gone, visualiser up --
    _switch_to_audio(window, app)
    assert window.right_display_stack.currentWidget() is window.visualiser_frame
    assert window.visualiser_frame.isVisible()
    assert not page.isVisible()
    assert not window._video_loading_page.isVisible()

    splitter_before = list(window.right_splitter.sizes())
    window_state_before = window.windowState()
    library_visible_before = not window.library_column_container.isHidden()
    queue_visible_before = not window.right_tabs.isHidden()

    # -- visualiser fullscreen in and out --
    PlayerWindow._enter_visualiser_fullscreen(window)
    app.processEvents()
    QTest.qWait(80)
    assert window._visualiser_fullscreen is True

    PlayerWindow._exit_visualiser_fullscreen(window)
    app.processEvents()
    QTest.qWait(80)

    assert window._visualiser_fullscreen is False
    assert window._current_media_type == MediaType.AUDIO
    # The visualiser page is back INSIDE the stack, not bolted onto the
    # splitter as an extra pane.
    assert window.visualiser_frame.parentWidget() is window.right_display_stack
    assert window.right_splitter.count() == 2
    assert window.right_display_stack.currentWidget() is window.visualiser_frame
    assert window.visualiser_frame.isVisible()
    # ...and no video pane of any kind came back with it.
    assert not window._video_loading_page.isVisible()
    assert not page.isVisible()
    # Surrounding chrome and geometry untouched by the visualiser cycle.
    assert (not window.right_tabs.isHidden()) == queue_visible_before
    assert (not window.library_column_container.isHidden()) == library_visible_before
    assert list(window.right_splitter.sizes()) == splitter_before
    assert window.windowState() == window_state_before
    window.close()


def test_repeated_fullscreen_cycles_never_carry_state_between_them():
    app, window, page, video_output, backend = _main_video_window()
    # Sized to fit this environment's offscreen virtual screen so the
    # splitter comparison across cycles measures state leakage rather
    # than the platform clamping an over-large window (see
    # test_fullscreen_exit_restores_splitter_proportions_explicitly).
    window.resize(600, 500)
    app.processEvents()
    QTest.qWait(60)

    def video_cycle():
        window._current_media_type = MediaType.VIDEO
        window.right_display_stack.setCurrentWidget(page)
        app.processEvents()
        PlayerWindow._enter_video_fullscreen(window)
        QTest.qWait(60)
        PlayerWindow._exit_video_fullscreen(window)
        app.processEvents()
        QTest.qWait(60)
        # Snapshot consumed and cleared -- nothing survives the exit.
        assert window._video_fullscreen is False
        assert window._video_fullscreen_restore_state is None
        assert window._video_fullscreen_owner_widget is None
        assert not window.isFullScreen()
        assert window.updatesEnabled()

    def visualiser_cycle():
        _switch_to_audio(window, app)
        PlayerWindow._enter_visualiser_fullscreen(window)
        app.processEvents()
        QTest.qWait(60)
        PlayerWindow._exit_visualiser_fullscreen(window)
        app.processEvents()
        QTest.qWait(60)
        assert window._visualiser_fullscreen is False
        assert window._visualiser_fullscreen_snapshot is None
        # Structure identical to before the cycle, every time.
        assert window.right_splitter.count() == 2
        assert window.right_display_stack.count() == 3
        assert window.visualiser_frame.parentWidget() is window.right_display_stack
        assert window.right_display_stack.currentWidget() is window.visualiser_frame
        assert not window._video_loading_page.isVisible()

    baseline_splitter = list(window.right_splitter.sizes())
    video_cycle()
    visualiser_cycle()
    visualiser_cycle()
    video_cycle()
    visualiser_cycle()

    assert window._current_media_type == MediaType.AUDIO
    assert window.right_splitter.count() == 2
    assert window.right_display_stack.count() == 3
    assert not window._video_loading_page.isVisible()
    assert not window.library_column_container.isHidden()
    assert not window.right_tabs.isHidden()
    assert list(window.right_splitter.sizes()) == baseline_splitter
    window.close()


def test_video_fullscreen_exit_reconciles_stage_when_track_became_audio():
    # Presentation-state correction (never playback): if the track changed
    # to audio while video fullscreen was active, the restored stage must
    # not be left showing a video page.
    app, window, page, video_output, backend = _main_video_window()
    window._current_media_type = MediaType.VIDEO
    window.right_display_stack.setCurrentWidget(page)
    app.processEvents()

    PlayerWindow._enter_video_fullscreen(window)
    QTest.qWait(60)
    # The queue advanced to an audio track while fullscreen was up.
    window._current_media_type = MediaType.AUDIO
    PlayerWindow._exit_video_fullscreen(window)
    app.processEvents()
    QTest.qWait(60)

    assert window.right_display_stack.currentWidget() is window.visualiser_frame
    assert not page.isVisible()
    assert not window._video_loading_page.isVisible()
    window.close()


def test_video_fullscreen_exit_keeps_the_video_stage_for_a_video_track():
    # Control: the reconciliation must not steal the stage from video
    # that is genuinely still playing.
    app, window, page, video_output, backend = _main_video_window()
    window._current_media_type = MediaType.VIDEO
    window.right_display_stack.setCurrentWidget(page)
    app.processEvents()

    PlayerWindow._enter_video_fullscreen(window)
    QTest.qWait(60)
    PlayerWindow._exit_video_fullscreen(window)
    app.processEvents()
    QTest.qWait(60)

    assert window.right_display_stack.currentWidget() is page
    assert not window.visualiser_frame.isVisible()
    window.close()


# ---------------------------------------------------------------------------
# Fullscreen transition lifecycle / re-entrancy (real-device crash,
# session 38cddc05): the stall trace caught MainThread inside
# _exit_main_video_fullscreen_presentation's widget.setVisible() loop with
# BOTH input routes present in the same trace -- the child process's
# forwarded double-click (delivered inside a QProcess stdout callback) and
# the main-window eventFilter Escape. Both were independent synchronous
# callers of the same native restore path, with no guard against one
# starting while the other was still physically restoring.
# ---------------------------------------------------------------------------

def _fullscreen_transitions(window, target=None):
    """Physical transitions actually executed, from the bounded
    transition diagnostics."""
    return [
        details for _cat, op, details in window.diagnostic_events
        if op == "video_fullscreen_transition_begin"
        and (target is None or details.get("target") is target)
    ]


def _enter_fullscreen_now(window, app, page):
    window._current_media_type = MediaType.VIDEO
    window.right_display_stack.setCurrentWidget(page)
    app.processEvents()
    PlayerWindow._enter_video_fullscreen(window)
    QTest.qWait(80)
    app.processEvents()
    assert window._video_fullscreen is True
    window.diagnostic_events.clear()


def test_child_double_click_and_escape_in_one_turn_produce_exactly_one_exit():
    # Case A: the child's forwarded double-click requests exit, then the
    # main-window Escape requests exit too, before the queued exit has
    # run. Exactly ONE physical exit may happen.
    app, window, page, video_output, backend = _main_video_window()
    _enter_fullscreen_now(window, app, page)

    PlayerWindow._on_child_video_double_clicked(window)
    PlayerWindow._request_video_fullscreen_state(window, False, "event_filter_escape")
    # Nothing may have happened yet -- both are requests only.
    assert window._video_fullscreen is True
    assert _fullscreen_transitions(window) == []

    QTest.qWait(80)
    app.processEvents()

    assert window._video_fullscreen is False
    assert len(_fullscreen_transitions(window, target=False)) == 1
    assert _fullscreen_transitions(window, target=True) == []
    coalesced = [
        d for _c, op, d in window.diagnostic_events
        if op == "video_fullscreen_request_coalesced"
    ]
    assert coalesced, "the second request must be recorded as coalesced"
    window.close()


def test_child_double_click_during_an_exit_never_re_enters_fullscreen():
    # Case B: Escape requests exit; while the exit is physically running,
    # the child's double-click arrives. It must not re-enter fullscreen.
    app, window, page, video_output, backend = _main_video_window()
    arrived = []
    armed = []

    class _Probe(QtWidgets.QWidget):
        def setVisible(self, visible):
            # Fires from inside _exit_main_video_fullscreen_presentation's
            # widget-visibility restore -- exactly where the real-device
            # trace caught MainThread. `armed` keeps the test's own setup
            # call out of it, and keeps this to the EXIT restore only.
            if armed and not arrived:
                arrived.append(True)
                PlayerWindow._on_child_video_double_clicked(window)
            super().setVisible(visible)

    probe = _Probe()
    probe.setVisible(True)
    # Installed BEFORE entering: the widget list restored on exit is
    # snapshotted by _main_video_fullscreen_hidden_widgets on entry.
    window.now_playing = probe
    _enter_fullscreen_now(window, app, page)
    armed.append(True)

    PlayerWindow._request_video_fullscreen_state(window, False, "event_filter_escape")
    QTest.qWait(80)
    app.processEvents()
    QTest.qWait(80)
    app.processEvents()

    assert arrived, "the mid-restore input was never delivered"
    assert window._video_fullscreen is False
    assert not window.isFullScreen()
    # One exit, and crucially no enter at all.
    assert len(_fullscreen_transitions(window, target=False)) == 1
    assert _fullscreen_transitions(window, target=True) == []
    window.close()


def test_two_double_clicks_in_one_turn_coalesce_without_enter_exit_recursion():
    # Case C: two double-clicks in the same event-loop turn must coalesce
    # to one deterministic target, never enter->exit->enter.
    app, window, page, video_output, backend = _main_video_window()
    window._current_media_type = MediaType.VIDEO
    window.right_display_stack.setCurrentWidget(page)
    app.processEvents()
    window.diagnostic_events.clear()

    PlayerWindow._on_child_video_double_clicked(window)
    PlayerWindow._on_child_video_double_clicked(window)
    assert _fullscreen_transitions(window) == []  # still nothing executed

    QTest.qWait(80)
    app.processEvents()
    QTest.qWait(80)
    app.processEvents()

    # Both asked for the same thing (enter, since it started windowed):
    # exactly one enter, no exit chasing it.
    assert window._video_fullscreen is True
    assert len(_fullscreen_transitions(window, target=True)) == 1
    assert _fullscreen_transitions(window, target=False) == []

    PlayerWindow._exit_video_fullscreen(window)
    app.processEvents()
    window.close()


def test_events_raised_while_restoring_cannot_start_a_nested_transition():
    # Case D: the exit presentation itself must be re-entrancy proof even
    # against a DIRECT call (the app's own internal lifecycle paths still
    # call the physical methods synchronously), not merely against the
    # queued request seam.
    app, window, page, video_output, backend = _main_video_window()
    observed = []
    armed = []

    class _Probe(QtWidgets.QWidget):
        def setVisible(self, visible):
            if armed and not observed:
                observed.append(window._video_fullscreen_transitioning)
                # Directly re-enter both physical paths mid-restore.
                PlayerWindow._exit_video_fullscreen(window)
                PlayerWindow._enter_video_fullscreen(window)
            super().setVisible(visible)

    probe = _Probe()
    probe.setVisible(True)
    # Installed BEFORE entering: _main_video_fullscreen_hidden_widgets is
    # snapshotted on entry, so only widgets present then are restored.
    window.now_playing = probe
    _enter_fullscreen_now(window, app, page)
    armed.append(True)

    PlayerWindow._exit_video_fullscreen(window)
    app.processEvents()
    QTest.qWait(80)

    assert observed == [True], "the restore must run with the transition guard set"
    blocked = [
        d for _c, op, d in window.diagnostic_events
        if op == "video_fullscreen_transition_reentry_blocked"
    ]
    assert len(blocked) == 2, f"both nested attempts must be refused; saw {blocked}"
    # Exactly one physical exit, no enter, and a clean final state.
    assert len(_fullscreen_transitions(window, target=False)) == 1
    assert _fullscreen_transitions(window, target=True) == []
    assert window._video_fullscreen is False
    assert not window.isFullScreen()
    assert window._video_fullscreen_transitioning is False
    assert window.updatesEnabled()
    window.close()


def test_fifty_fullscreen_cycles_leave_no_state_or_hierarchy_corruption():
    # Case E: 50+ enter/exit cycles through the real request seam.
    app, window, page, video_output, backend = _main_video_window()
    window.resize(600, 500)
    app.processEvents()
    QTest.qWait(60)
    window._current_media_type = MediaType.VIDEO
    window.right_display_stack.setCurrentWidget(page)
    app.processEvents()

    splitter_before = list(window.right_splitter.sizes())
    stack_pages_before = window.right_display_stack.count()
    splitter_children_before = window.right_splitter.count()
    video_parent_before = video_output.parentWidget()

    for cycle in range(50):
        PlayerWindow._request_video_fullscreen_state(window, True, "stress")
        QTest.qWait(15)
        app.processEvents()
        assert window._video_fullscreen is True, f"enter failed on cycle {cycle}"
        assert window._video_fullscreen_transitioning is False
        PlayerWindow._request_video_fullscreen_state(window, False, "stress")
        QTest.qWait(15)
        app.processEvents()
        assert window._video_fullscreen is False, f"exit failed on cycle {cycle}"
        # Snapshot cleared every single cycle.
        assert window._video_fullscreen_restore_state is None
        assert window._video_fullscreen_owner_widget is None
        assert window._video_fullscreen_transitioning is False
        assert window._video_fullscreen_pending_target is None
        assert window.updatesEnabled()

    # Exactly 50 of each -- no duplicates, no skipped transitions.
    assert len(_fullscreen_transitions(window, target=True)) == 50
    assert len(_fullscreen_transitions(window, target=False)) == 50
    # No hierarchy corruption.
    assert window.right_splitter.count() == splitter_children_before
    assert window.right_display_stack.count() == stack_pages_before
    assert video_output.parentWidget() is video_parent_before
    backend.attach_output.assert_not_called()
    assert not window.isFullScreen()
    assert not window.library_column_container.isHidden()
    assert not window.right_tabs.isHidden()
    assert list(window.right_splitter.sizes()) == splitter_before
    window.close()


def test_request_that_is_already_satisfied_does_nothing():
    app, window, page, video_output, backend = _main_video_window()
    window._current_media_type = MediaType.VIDEO
    app.processEvents()
    window.diagnostic_events.clear()

    PlayerWindow._request_video_fullscreen_state(window, False, "redundant")
    QTest.qWait(60)
    app.processEvents()

    assert window._video_fullscreen is False
    assert _fullscreen_transitions(window) == []
    assert window._video_fullscreen_request_scheduled is False
    window.close()


def test_child_input_returns_immediately_without_transitioning():
    # Requirement 1: the request must return to the QProcess/input
    # callback before any window transition happens.
    app, window, page, video_output, backend = _main_video_window()
    window._current_media_type = MediaType.VIDEO
    window.right_display_stack.setCurrentWidget(page)
    app.processEvents()
    window.diagnostic_events.clear()

    PlayerWindow._on_child_video_double_clicked(window)

    # Returned already, with nothing physically done yet.
    assert window._video_fullscreen is False
    assert window.isFullScreen() is False
    assert window._video_fullscreen_pending_target is True
    assert window._video_fullscreen_request_scheduled is True
    assert _fullscreen_transitions(window) == []
    requests = [
        d for _c, op, d in window.diagnostic_events
        if op == "video_fullscreen_request"
    ]
    assert requests and requests[0]["request_source"] == "child_double_click"
    assert requests[0]["requested_target"] is True
    assert requests[0]["transitioning"] is False

    QTest.qWait(80)
    app.processEvents()
    assert window._video_fullscreen is True
    PlayerWindow._exit_video_fullscreen(window)
    app.processEvents()
    window.close()


# ---------------------------------------------------------------------------
# Fullscreen lifecycle follow-up: pending requests must drain after EVERY
# physical transition (including direct internal ones), requests are dead
# during shutdown, Escape acts on effective intent, and failure
# diagnostics are truthful.
# ---------------------------------------------------------------------------

def _ops(window, operation):
    return [d for _c, op, d in window.diagnostic_events if op == operation]


class _EventFilterDelegate(QtCore.QObject):
    """Installed on the real QApplication exactly as PlayerWindow installs
    itself, forwarding key presses to the REAL PlayerWindow.eventFilter --
    so a test's QKeyEvent travels Qt's own dispatch and filter route rather
    than a direct call to the request seam."""

    def __init__(self, window):
        super().__init__()
        self._window = window
        self.errors = []

    def eventFilter(self, obj, event):
        if event.type() != QtCore.QEvent.Type.KeyPress:
            return False  # the real method only acts on KeyPress too
        try:
            return bool(PlayerWindow.eventFilter(self._window, obj, event))
        except Exception as exc:
            # An exception escaping a Qt virtual aborts the process under
            # PyQt6, which would turn a real assertion failure into an
            # opaque crash. Captured instead; every test asserts it's empty.
            self.errors.append(repr(exc))
            return False


def _install_real_event_filter(window, app, monkeypatch):
    monkeypatch.setattr(
        QtWidgets.QApplication, "activeWindow", staticmethod(lambda: window),
    )
    delegate = _EventFilterDelegate(window)
    app.installEventFilter(delegate)
    return delegate


def _send_real_escape(window):
    event = QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_Escape,
        QtCore.Qt.KeyboardModifier.NoModifier,
    )
    return QtWidgets.QApplication.sendEvent(window, event)


def _pump(app, rounds=3, ms=60):
    for _ in range(rounds):
        QTest.qWait(ms)
        app.processEvents()


def test_request_during_a_direct_internal_enter_is_drained_not_stranded():
    # C: _enter_video_fullscreen() called DIRECTLY (as video_start_fullscreen
    # does), with an exit request arriving mid-presentation. Previously the
    # pending target was only drained by the queued applier, so it was left
    # stranded with no timer armed and the window stayed fullscreen.
    app, window, page, video_output, backend = _main_video_window()
    armed, fired = [], []

    class _Probe(QtWidgets.QWidget):
        def setVisible(self, visible):
            if armed and not fired:
                fired.append(
                    (window._video_fullscreen_transitioning, window._video_fullscreen)
                )
                PlayerWindow._request_video_fullscreen_state(window, False, "test_escape")
            super().setVisible(visible)

    probe = _Probe()
    probe.setVisible(True)
    window.now_playing = probe
    window._current_media_type = MediaType.VIDEO
    window.right_display_stack.setCurrentWidget(page)
    app.processEvents()
    window.diagnostic_events.clear()
    armed.append(True)

    PlayerWindow._enter_video_fullscreen(window)  # direct, not via the applier
    assert fired == [(True, False)], "request must land mid-enter, before commit"
    _pump(app)

    assert window._video_fullscreen is False
    assert not window.isFullScreen()
    assert window._video_fullscreen_pending_target is None
    assert window._video_fullscreen_request_scheduled is False
    assert len(_fullscreen_transitions(window, target=True)) == 1
    assert len(_fullscreen_transitions(window, target=False)) == 1
    assert _ops(window, "video_fullscreen_transition_reentry_blocked") == []
    window.close()


def test_request_during_a_direct_internal_exit_is_cleared_even_when_it_matches():
    # D: a request arriving during a DIRECT internal exit that matches the
    # state the exit ends up committing must still be cleared -- never left
    # stale in _video_fullscreen_pending_target.
    app, window, page, video_output, backend = _main_video_window()
    armed, fired = [], []

    class _Probe(QtWidgets.QWidget):
        def setVisible(self, visible):
            if armed and not fired:
                fired.append(window._video_fullscreen_transitioning)
                PlayerWindow._request_video_fullscreen_state(window, False, "test_escape")
            super().setVisible(visible)

    probe = _Probe()
    probe.setVisible(True)
    window.now_playing = probe
    _enter_fullscreen_now(window, app, page)
    armed.append(True)

    PlayerWindow._exit_video_fullscreen(window)  # direct, not via the applier
    assert fired == [True], "request must land mid-exit"
    # Cleared synchronously by the exit's own drain -- no timer needed.
    assert window._video_fullscreen_pending_target is None
    assert window._video_fullscreen_request_scheduled is False
    _pump(app)

    assert window._video_fullscreen is False
    assert window._video_fullscreen_pending_target is None
    assert window._video_fullscreen_request_scheduled is False
    assert len(_fullscreen_transitions(window, target=False)) == 1
    assert _fullscreen_transitions(window, target=True) == []
    window.close()


def _make_shutdown_capable(window):
    """Everything closeEvent/_request_shutdown touch directly, with one
    fake unresolved worker so shutdown stays PENDING and the normal Qt
    event loop keeps running (the C2 design)."""
    registry = WorkerLifetimeRegistry()
    registry.register(
        "stuck_worker", thread=SimpleNamespace(wait=lambda ms: False), wait_ms=1,
    )
    window._worker_registry = registry
    window._closing = False
    window._shutdown_requested = False
    window._shutdown_pending = False
    window._shutdown_complete = False
    window._shutdown_finalizing = False
    # Shutdown grace timer (added by the stuck-worker shutdown hardening):
    # substituted exactly as test_shutdown_hardening.py does, so the real
    # QTimer -> _force_process_exit (os._exit) can never fire in the runner.
    window._shutdown_grace_timer = None
    window._arm_shutdown_grace_timer = lambda: setattr(window, "_shutdown_grace_timer", object())
    window._cancel_shutdown_grace_timer = lambda: setattr(window, "_shutdown_grace_timer", None)
    window._playback_generation = 0
    window._plex_audio_load_token = 0
    window._crossfade_load_token = 0
    window._karaoke_generation = 0
    window._library_search_generation = 0
    window._playback_recovery_active = False
    window._mixed_transition_state = "idle"
    window.mini_player = None
    window.recently_played_repository = SimpleNamespace(save=lambda entries: None)
    window.recently_played_entries = []
    window._save_session = lambda: None
    window._save_queue_analysis_cache = lambda: None
    window._log = lambda message: None
    window._audio_log = lambda message: None
    window._cancel_current_playback_attempt = lambda reason: None
    window._cancel_playback_watchdog = lambda: None
    window._cancel_pending_library_apply = lambda: None
    window._cancel_fade = lambda: None
    window._stop_all = lambda: None
    window._cancel_metadata_backfill = lambda: None
    window._request_shutdown = lambda: PlayerWindow._request_shutdown(window)
    return registry


def test_queued_fullscreen_enter_is_dead_once_shutdown_begins():
    # E: an ENTER request is queued, then the real close/shutdown path runs
    # before the timer fires, with a worker keeping shutdown pending (so
    # app.exec()'s normal loop keeps delivering timers). The queued enter
    # must not execute and the window must not reappear.
    app, window, page, video_output, backend = _main_video_window()
    registry = _make_shutdown_capable(window)
    window._current_media_type = MediaType.VIDEO
    window.right_display_stack.setCurrentWidget(page)
    app.processEvents()
    window.diagnostic_events.clear()

    PlayerWindow._request_video_fullscreen_state(window, True, "test_enter")
    assert window._video_fullscreen_request_scheduled is True

    close_event = QtGui.QCloseEvent()
    PlayerWindow.closeEvent(window, close_event)  # the real close path
    assert window._closing is True
    assert window._shutdown_requested is True
    assert window._shutdown_pending is True  # the stuck worker keeps it pending
    assert not close_event.isAccepted()
    assert window._video_fullscreen_pending_target is None  # invalidated on first close
    backend.stop.assert_called()

    _pump(app)  # the already-posted singleShot fires now

    assert _fullscreen_transitions(window) == []  # no enter executed
    assert window._video_fullscreen is False
    assert not window.isFullScreen()
    assert not window.isVisible()  # did not reappear
    assert window._video_fullscreen_pending_target is None
    assert window._video_fullscreen_request_scheduled is False
    backend.schedule_output_geometry_sync.assert_not_called()
    backend.load.assert_not_called()
    backend.play.assert_not_called()
    # Shutdown stays authoritative, and later input stays dead.
    assert window._shutdown_pending is True
    assert window._shutdown_complete is False
    assert registry.active_count() == 1
    PlayerWindow._on_child_video_double_clicked(window)
    _pump(app, rounds=1)
    assert _fullscreen_transitions(window) == []
    assert window._video_fullscreen_request_scheduled is False
    assert _ops(window, "video_fullscreen_request_rejected")
    window.close()


def test_already_posted_enter_timer_is_discarded_by_the_applier_guard_alone():
    # The applier guard on its own: shutdown begins via _request_shutdown()
    # WITHOUT closeEvent's explicit pending invalidation, after an enter
    # timer was already posted with its target still pending. A posted
    # singleShot can't be cancelled, so only the applier's own guard can
    # stop it.
    app, window, page, video_output, backend = _main_video_window()
    _make_shutdown_capable(window)
    window._current_media_type = MediaType.VIDEO
    window.right_display_stack.setCurrentWidget(page)
    app.processEvents()
    window.diagnostic_events.clear()

    PlayerWindow._request_video_fullscreen_state(window, True, "test_enter")
    assert window._video_fullscreen_pending_target is True
    PlayerWindow._request_shutdown(window)  # sets _closing/_shutdown_requested only
    assert window._video_fullscreen_pending_target is True  # NOT invalidated here

    _pump(app)

    assert _fullscreen_transitions(window) == []
    assert window._video_fullscreen is False
    assert not window.isFullScreen()
    assert window._video_fullscreen_pending_target is None
    assert window._video_fullscreen_request_scheduled is False
    window.close()


def test_real_escape_before_a_queued_enter_runs_keeps_the_window_windowed(monkeypatch):
    # Real-Escape A: an enter is requested but still queued (committed state
    # False). A real Escape delivered through the application event filter
    # must cancel it -- previously the filter only checked the committed
    # flag and let the enter go ahead.
    app, window, page, video_output, backend = _main_video_window()
    window._current_media_type = MediaType.VIDEO
    window.right_display_stack.setCurrentWidget(page)
    app.processEvents()
    delegate = _install_real_event_filter(window, app, monkeypatch)
    try:
        window.diagnostic_events.clear()
        PlayerWindow._request_video_fullscreen_state(window, True, "test_enter")
        assert window._video_fullscreen is False

        consumed = _send_real_escape(window)

        _pump(app)
        assert _fullscreen_transitions(window) == []  # no physical enter at all
        assert window._video_fullscreen is False
        assert not window.isFullScreen()
        assert consumed is True  # handled by the real eventFilter
        assert delegate.errors == []
        assert window._video_fullscreen_pending_target is None
        assert window._video_fullscreen_request_scheduled is False
        escapes = [
            d for d in _ops(window, "video_fullscreen_request")
            if d.get("request_source") == "event_filter_escape"
        ]
        assert len(escapes) == 1 and escapes[0]["requested_target"] is False
    finally:
        app.removeEventFilter(delegate)
        window.close()


def test_real_escape_during_a_physical_enter_exits_after_it_without_nesting(monkeypatch):
    # Real-Escape B: Escape delivered while the ENTER presentation is
    # physically running (transitioning, not yet committed). Enter
    # completes atomically, then the queued exit runs; nothing nests.
    app, window, page, video_output, backend = _main_video_window()
    armed, fired = [], []

    class _Probe(QtWidgets.QWidget):
        def setVisible(self, visible):
            if armed and not fired:
                fired.append((
                    window._video_fullscreen_transitioning,
                    window._video_fullscreen,
                    _send_real_escape(window),
                ))
            super().setVisible(visible)

    probe = _Probe()
    probe.setVisible(True)
    window.now_playing = probe
    window._current_media_type = MediaType.VIDEO
    window.right_display_stack.setCurrentWidget(page)
    app.processEvents()
    delegate = _install_real_event_filter(window, app, monkeypatch)
    try:
        window.diagnostic_events.clear()
        armed.append(True)
        PlayerWindow._request_video_fullscreen_state(window, True, "test_enter")
        _pump(app, rounds=4)

        assert fired, "Escape was never delivered during the enter presentation"
        assert window._video_fullscreen is False
        assert not window.isFullScreen()
        assert len(_fullscreen_transitions(window, target=True)) == 1
        assert len(_fullscreen_transitions(window, target=False)) == 1
        transitioning, committed, consumed = fired[0]
        assert transitioning is True and committed is False and consumed is True
        assert delegate.errors == []
        assert _ops(window, "video_fullscreen_transition_reentry_blocked") == []
        assert window._video_fullscreen_pending_target is None
        assert window._video_fullscreen_request_scheduled is False
    finally:
        app.removeEventFilter(delegate)
        window.close()


def test_enter_presentation_exception_is_recorded_truthfully():
    # F: a Python exception from the enter presentation must be recorded as
    # a failed transition, leave the committed state unchanged, clear the
    # transition flags, drain the pending request, and not retry.
    app, window, page, video_output, backend = _main_video_window()
    window._current_media_type = MediaType.VIDEO
    app.processEvents()
    window.diagnostic_events.clear()

    def _raising_enter():
        # A request arrives mid-transition, then the presentation fails.
        PlayerWindow._request_video_fullscreen_state(window, False, "test_escape")
        raise RuntimeError("presentation exploded")

    window._enter_main_video_fullscreen_presentation = _raising_enter

    PlayerWindow._enter_video_fullscreen(window)  # must not raise
    _pump(app)

    failed = _ops(window, "video_fullscreen_transition_failed")
    assert len(failed) == 1
    assert failed[0]["target"] is True
    assert failed[0]["reason"] == "exception"
    assert failed[0]["exception_type"] == "RuntimeError"
    assert _ops(window, "video_fullscreen_transition_complete") == []
    assert _ops(window, "video_fullscreen_changed") == []
    assert window._video_fullscreen is False
    assert window._video_fullscreen_owner_widget is None
    assert window._video_fullscreen_restore_state is None
    assert window._video_fullscreen_transitioning is False
    assert window._video_fullscreen_transition_target is None
    assert window._video_fullscreen_pending_target is None  # drained (matched committed)
    assert window._video_fullscreen_request_scheduled is False
    assert len(_fullscreen_transitions(window)) == 1  # no recursive retry
    backend.schedule_output_geometry_sync.assert_not_called()
    window.close()


def test_enter_does_not_swallow_base_exceptions():
    app, window, page, video_output, backend = _main_video_window()
    window._current_media_type = MediaType.VIDEO

    class _Abort(BaseException):
        pass

    def _aborting_enter():
        raise _Abort()

    window._enter_main_video_fullscreen_presentation = _aborting_enter
    with pytest.raises(_Abort):
        PlayerWindow._enter_video_fullscreen(window)
    assert window._video_fullscreen is False
    assert window._video_fullscreen_transitioning is False  # still cleared
    assert window._video_fullscreen_transition_target is None
    window.close()


def test_exit_presentation_exception_is_failed_only_never_also_complete():
    # G: one failed physical exit must not be reported as both failed and
    # complete/changed. The logical reconciliation it still performs is
    # stated explicitly on the failed event.
    app, window, page, video_output, backend = _main_video_window()
    _enter_fullscreen_now(window, app, page)
    backend.schedule_output_geometry_sync.reset_mock()

    def _raising_exit(_state):
        raise RuntimeError("restore exploded")

    window._exit_main_video_fullscreen_presentation = _raising_exit

    PlayerWindow._exit_video_fullscreen(window)  # must not raise
    _pump(app, rounds=1)

    failed = _ops(window, "video_fullscreen_transition_failed")
    assert len(failed) == 1
    # The defect itself: the same failed transition also reported as
    # complete / changed.
    assert _ops(window, "video_fullscreen_transition_complete") == []
    assert [d for d in _ops(window, "video_fullscreen_changed") if d.get("fullscreen") is False] == []
    assert failed[0]["target"] is False
    assert failed[0]["reason"] == "exception"
    assert failed[0]["exception_type"] == "RuntimeError"
    assert failed[0]["logical_state_reconciled"] is True
    assert window._video_fullscreen is False
    assert window._video_fullscreen_restore_state is None
    assert window._video_fullscreen_owner_widget is None
    assert window._video_fullscreen_transitioning is False
    assert window._video_fullscreen_transition_target is None
    assert window._video_fullscreen_pending_target is None
    window.showNormal()
    window.close()


def test_party_mode_fullscreen_uses_party_presentation_without_reparenting():
    _app()
    window = QtWidgets.QMainWindow()
    party_mode = QtWidgets.QWidget()
    party_mode._video_active = True
    party_mode.enter_video_fullscreen_presentation = MagicMock(return_value={"party": True})
    party_mode.exit_video_fullscreen_presentation = MagicMock()
    party_mode.show()
    QtWidgets.QApplication.processEvents()
    backend = MagicMock()
    window._current_media_type = MediaType.VIDEO
    window._video_fullscreen = False
    window._video_fullscreen_owner_widget = None
    window._video_fullscreen_restore_state = None
    window.party_mode = party_mode
    window._video_backend = backend
    window.diagnostics = SimpleNamespace(record=lambda *a, **kw: None)
    window._video_fullscreen_stage_owner = lambda: PlayerWindow._video_fullscreen_stage_owner(window)
    window._enter_main_video_fullscreen_presentation = MagicMock()
    window.right_display_stack = None  # Party Mode owns its own stage
    window._reconcile_display_stage_with_current_media = (
        lambda: PlayerWindow._reconcile_display_stage_with_current_media(window)
    )
    window._video_fullscreen_transitioning = False
    window._video_fullscreen_pending_target = None
    window._video_fullscreen_request_scheduled = False
    window._video_fullscreen_sequence = 0
    window._video_fullscreen_transition_target = None
    window._drain_pending_video_fullscreen_request = (
        lambda: PlayerWindow._drain_pending_video_fullscreen_request(window)
    )
    window._video_fullscreen_requests_dead = (
        lambda: PlayerWindow._video_fullscreen_requests_dead(window)
    )
    window._video_fullscreen_effective_intent = (
        lambda: PlayerWindow._video_fullscreen_effective_intent(window)
    )
    window._record_video_fullscreen_event = (
        lambda operation, **details: PlayerWindow._record_video_fullscreen_event(
            window, operation, **details
        )
    )
    window._next_video_fullscreen_sequence = (
        lambda: PlayerWindow._next_video_fullscreen_sequence(window)
    )

    PlayerWindow._enter_video_fullscreen(window)

    assert window._video_fullscreen is True
    assert window._video_fullscreen_owner_widget is party_mode
    party_mode.enter_video_fullscreen_presentation.assert_called_once_with()
    window._enter_main_video_fullscreen_presentation.assert_not_called()
    backend.attach_output.assert_not_called()

    PlayerWindow._exit_video_fullscreen(window)

    party_mode.exit_video_fullscreen_presentation.assert_called_once_with({"party": True})
    backend.attach_output.assert_not_called()
    party_mode.close()
    window.close()


def test_stop_during_active_mixed_audio_to_video_overlap_leaves_real_fullscreen():
    """Phase 1.1: real fullscreen presentation (real enter/exit methods on
    the real nested window) entered during an active Audio->Video mixed
    overlap, then the real stop_playback. Stop cancels the transition
    first, which returns the logical media type to AUDIO before the
    ordinary video teardown runs -- fullscreen must still be exited."""
    app, window, page, video_output, backend = _main_video_window()
    window._enter_video_fullscreen()
    app.processEvents()
    assert window._video_fullscreen is True

    # State of an active Audio->Video overlap (see _activate_mixed_media_transition).
    window._closing = False
    window._mixed_transition_id = 7
    window._mixed_transition_state = "active"
    window._mixed_transition_direction = "audio_to_video"
    window._mixed_transition_incoming_token = None
    window.pending_next = True
    window._reset_mixed_media_transition_state = (
        lambda: PlayerWindow._reset_mixed_media_transition_state(window)
    )
    window._cancel_mixed_media_transition = (
        lambda reason, **kw: PlayerWindow._cancel_mixed_media_transition(window, reason, **kw)
    )
    window._stop_video_for_audio_transition = (
        lambda: PlayerWindow._stop_video_for_audio_transition(window)
    )
    window._detach_video_from_party_mode = lambda: None
    window._resume_deferred_queue_analysis = lambda: None
    window._current_playback_attempt = None
    window._cancel_current_playback_attempt = lambda reason: None
    window._video_transition_manager = None
    window.cast_active = False
    window._cancel_fade = lambda: None
    window._stop_all = lambda: None
    window._cancel_playback_watchdog = lambda: None
    window._playback_expected = True
    window._playback_intentionally_paused = False
    window.beat = SimpleNamespace(setFocus=lambda *a, **kw: None, setPlaying=lambda playing: None)
    window._reset_progress = lambda: None
    window._sync_now_playing_overlay_for_media_type = lambda: None
    window._announce_accessible_status = lambda message: None

    PlayerWindow.stop_playback(window)
    app.processEvents()

    assert window._video_fullscreen is False
    assert not window.isFullScreen()
    assert window._mixed_transition_state == "idle"
    assert window._current_media_type == MediaType.AUDIO
    assert window.right_display_stack.currentWidget() is window._normal_display_page
    backend.stop.assert_called()
    window.close()


def test_video_to_audio_transition_exits_fullscreen_before_stopping():
    events = []
    window = SimpleNamespace(
        _current_media_type=MediaType.VIDEO,
        _video_fullscreen=True,
        _exit_video_fullscreen=lambda: events.append("exit_fullscreen"),
        _video_backend=SimpleNamespace(stop=lambda: events.append("stop_video")),
        _detach_video_from_party_mode=lambda: None,
        _show_normal_display_page=lambda: None,
        _resume_deferred_queue_analysis=lambda: None,
    )

    PlayerWindow._stop_video_for_audio_transition(window)

    assert events == ["exit_fullscreen", "stop_video"]
    assert window._current_media_type == MediaType.AUDIO


def test_video_error_exits_fullscreen_and_restores_audio_state():
    events = []
    window = SimpleNamespace(
        _current_media_type=MediaType.VIDEO,
        _video_fullscreen=True,
        _exit_video_fullscreen=lambda: events.append("exit_fullscreen"),
        _video_backend=SimpleNamespace(stop=lambda: events.append("stop_video")),
        _detach_video_from_party_mode=lambda: None,
        _show_normal_display_page=lambda: None,
        _resume_deferred_queue_analysis=lambda: None,
        _playback_expected=True,
        _VIDEO_ERROR_MESSAGES=PlayerWindow._VIDEO_ERROR_MESSAGES,
        current_path="clip.mp4",
        diagnostics=SimpleNamespace(
            record=lambda *a, **kw: None,
            path_details=lambda path: {},
        ),
        statusBar=lambda: SimpleNamespace(showMessage=lambda *a, **kw: None),
        _sync_now_playing_overlay_for_media_type=lambda: None,
        _mixed_transition_state="idle",
    )

    PlayerWindow._on_video_error(window, "video_decode_error", "decode failed")

    assert events == ["exit_fullscreen", "stop_video"]
    assert window._current_media_type == MediaType.AUDIO
    assert window._playback_expected is False


def test_video_error_never_independently_aborts_a_committed_dual_transition():
    # Correctness hardening (2026-08-24, Codex design review): this
    # generic handler has no transition_id to verify, so it must never
    # reach into the dual engine and abort whichever transition happens to
    # be active -- only a typed, transition_id-carrying failure (backend.
    # dual_transition_failed -> _on_video_dual_transition_failed, see
    # below) is allowed to do that. playback_stopped()'s own cooperative
    # cancel() still runs (and still correctly refuses once committed --
    # see video_dual_transition.py's COMMITTED_STATES -- that's expected,
    # not a bug: an *uncommitted* preload/secondary-ready state is still
    # cleaned up here just fine).
    playback_stopped_calls = []
    primary_failed_calls = []
    window = SimpleNamespace(
        _current_media_type=MediaType.VIDEO,
        _video_fullscreen=False,
        _video_backend=SimpleNamespace(stop=lambda: None),
        _detach_video_from_party_mode=lambda: None,
        _show_normal_display_page=lambda: None,
        _resume_deferred_queue_analysis=lambda: None,
        _playback_expected=True,
        _VIDEO_ERROR_MESSAGES=PlayerWindow._VIDEO_ERROR_MESSAGES,
        current_path="clip.mp4",
        diagnostics=SimpleNamespace(
            record=lambda *a, **kw: None, path_details=lambda path: {},
        ),
        statusBar=lambda: SimpleNamespace(showMessage=lambda *a, **kw: None),
        _sync_now_playing_overlay_for_media_type=lambda: None,
        _video_transition_manager=SimpleNamespace(
            playback_stopped=lambda reason: playback_stopped_calls.append(reason),
            dual_engine=SimpleNamespace(
                primary_failed=lambda *a: primary_failed_calls.append(a),
            ),
        ),
        _mixed_transition_state="idle",
    )

    PlayerWindow._on_video_error(window, "video_subprocess_error", "process crashed")

    assert playback_stopped_calls == ["video_error"]
    assert primary_failed_calls == []


def test_video_dual_transition_failed_calls_primary_failed_with_transition_id():
    # The typed path (backend.dual_transition_failed signal, see
    # video_backend.py) -- unlike the generic _on_video_error above, this
    # one *is* allowed to abort the dual engine, since it carries the
    # exact transition_id primary_failed() itself independently
    # re-verifies before doing anything.
    primary_failed_calls = []
    window = SimpleNamespace(
        _video_transition_manager=SimpleNamespace(
            dual_engine=SimpleNamespace(
                primary_failed=lambda transition_id, reason: primary_failed_calls.append(
                    (transition_id, reason)
                ),
            ),
        ),
    )

    PlayerWindow._on_video_dual_transition_failed(window, 7, "commit_ack_timeout")

    assert primary_failed_calls == [(7, "commit_ack_timeout")]


def test_video_dual_transition_failed_with_no_dual_engine_does_not_raise():
    window = SimpleNamespace(_video_transition_manager=None)
    PlayerWindow._on_video_dual_transition_failed(window, 7, "commit_ack_timeout")


def test_video_error_with_no_dual_engine_does_not_raise():
    window = SimpleNamespace(
        _current_media_type=MediaType.VIDEO,
        _video_fullscreen=False,
        _video_backend=SimpleNamespace(stop=lambda: None),
        _detach_video_from_party_mode=lambda: None,
        _show_normal_display_page=lambda: None,
        _resume_deferred_queue_analysis=lambda: None,
        _playback_expected=True,
        _VIDEO_ERROR_MESSAGES=PlayerWindow._VIDEO_ERROR_MESSAGES,
        current_path="clip.mp4",
        diagnostics=SimpleNamespace(
            record=lambda *a, **kw: None, path_details=lambda path: {},
        ),
        statusBar=lambda: SimpleNamespace(showMessage=lambda *a, **kw: None),
        _sync_now_playing_overlay_for_media_type=lambda: None,
        # Phase 1-only manager (dual video transitions disabled/unavailable)
        # has no dual_engine attribute at all.
        _video_transition_manager=SimpleNamespace(playback_stopped=lambda reason: None),
        _mixed_transition_state="idle",
    )

    PlayerWindow._on_video_error(window, "video_decode_error", "decode failed")

    assert window._current_media_type == MediaType.AUDIO


def test_video_end_with_nothing_next_exits_fullscreen():
    events = []
    window = SimpleNamespace(
        _current_media_type=MediaType.VIDEO,
        _video_fullscreen=True,
        _exit_video_fullscreen=lambda: events.append("exit_fullscreen"),
        _playback_generation=4,
        _next_track=lambda reason: None,
        video_return_to_normal_display_on_end=True,
        _detach_video_from_party_mode=lambda: None,
        _show_normal_display_page=lambda: None,
        current_path="clip.mp4",
        diagnostics=SimpleNamespace(
            record=lambda *a, **kw: None,
            path_details=lambda path: {},
        ),
        _sync_now_playing_overlay_for_media_type=lambda: None,
        _mixed_transition_owns_video_boundary=lambda: False,
    )

    PlayerWindow._on_video_end_of_media(window)

    assert events == ["exit_fullscreen"]
    assert window._current_media_type == MediaType.AUDIO
