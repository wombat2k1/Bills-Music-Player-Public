"""Regression coverage for karaoke fullscreen: it must share the exact
reparent-free _video_fullscreen mechanism video already uses (expand the
existing stage, hide chrome) rather than the old approach of reparenting
karaoke_widget into its own top-level window and back.

Reparenting a widget while changing its window flags on a still-visible
widget is a known Qt/Windows hazard where the widget's native surface can
come back blank instead of repainting -- this was reported as "esc from
party mode full screen [leaves] the video [does not reappear] in the main
gui" (a black karaoke display after exiting fullscreen). Video fullscreen
already avoids this (see test_video_fullscreen.py); karaoke previously had
its own, older, reparenting-based _toggle_karaoke_fullscreen/
_exit_karaoke_fullscreen that predated that fix.
"""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets
from PyQt6.QtTest import QTest

from billsmusic.media_type import MediaType
from billsmusic.window import PlayerWindow

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _main_karaoke_window():
    app = _app()
    window = QtWidgets.QMainWindow()
    central = QtWidgets.QWidget()
    root = QtWidgets.QVBoxLayout(central)
    root.setContentsMargins(18, 16, 18, 16)
    root.setSpacing(12)
    window.setCentralWidget(central)

    window.now_playing = QtWidgets.QLabel("Now playing")
    root.addWidget(window.now_playing)
    window.library_tabs = QtWidgets.QTabWidget()
    # Phase C2.1 acceptance defect fix: library_tabs (and the rest of the
    # library region) is wrapped in ONE real container widget in the real
    # app now -- see test_video_fullscreen.py's _main_video_window for the
    # full explanation of why a bare nested QLayout item can't be
    # collapsed by hiding its children individually.
    window.library_column_container = QtWidgets.QWidget()
    library_column = QtWidgets.QVBoxLayout(window.library_column_container)
    library_column.setContentsMargins(0, 0, 0, 0)
    library_column.addWidget(window.library_tabs)
    root.addWidget(window.library_column_container)
    window.right_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
    page = QtWidgets.QWidget()
    page.setLayout(QtWidgets.QVBoxLayout())
    from billsmusic.cdg import CdgWidget
    karaoke_widget = CdgWidget()
    page.layout().addWidget(karaoke_widget)
    window.right_tabs = QtWidgets.QTabWidget()
    window.right_splitter.addWidget(page)
    window.right_splitter.addWidget(window.right_tabs)
    root.addWidget(window.right_splitter, 1)
    window.slider_progress = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
    root.addWidget(window.slider_progress)
    window.resize(800, 600)
    window.show()
    app.processEvents()

    backend = MagicMock()
    window._current_media_type = MediaType.KARAOKE
    window._video_fullscreen = False
    window._video_fullscreen_owner_widget = None
    window._video_fullscreen_restore_state = None
    window._karaoke_output_page = page
    window.karaoke_widget = karaoke_widget
    window._video_backend = backend
    window.party_mode = None
    window.diagnostics = SimpleNamespace(record=lambda *a, **kw: None)
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
    window._toggle_video_fullscreen = lambda: PlayerWindow._toggle_video_fullscreen(window)
    window._enter_video_fullscreen = lambda: PlayerWindow._enter_video_fullscreen(window)
    window._exit_video_fullscreen = lambda: PlayerWindow._exit_video_fullscreen(window)
    window._toggle_karaoke_fullscreen = lambda: PlayerWindow._toggle_karaoke_fullscreen(window)
    window._exit_karaoke_fullscreen = lambda: PlayerWindow._exit_karaoke_fullscreen(window)
    # Fullscreen exit reconciles the display stage against current media
    # (presentation-state only) -- karaoke shares the video mechanism.
    window.right_display_stack = None
    window._reconcile_display_stage_with_current_media = (
        lambda: PlayerWindow._reconcile_display_stage_with_current_media(window)
    )
    # Fullscreen transition lifecycle state + the real request seam
    # (karaoke input shares the one video fullscreen mechanism).
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
    window._on_karaoke_double_clicked = (
        lambda *a: PlayerWindow._on_karaoke_double_clicked(window, *a)
    )
    window._on_karaoke_escape_pressed = (
        lambda *a: PlayerWindow._on_karaoke_escape_pressed(window, *a)
    )
    # Mirrors the real connection made in PlayerWindow.__init__ so this test
    # exercises the actual signal path a real Escape key press uses.
    karaoke_widget.escape_pressed.connect(window._exit_karaoke_fullscreen)
    return app, window, page, karaoke_widget, backend


def test_karaoke_double_click_expands_stage_without_reparenting_widget():
    app, window, page, karaoke_widget, backend = _main_karaoke_window()
    original_parent = karaoke_widget.parentWidget()
    original_margins = window.centralWidget().layout().getContentsMargins()

    PlayerWindow._toggle_karaoke_fullscreen(window)
    QTest.qWait(125)

    assert window._video_fullscreen is True
    assert window.isFullScreen()
    assert karaoke_widget.parentWidget() is original_parent
    assert karaoke_widget.windowFlags() & QtCore.Qt.WindowType.Window == 0
    assert window.now_playing.isHidden()
    assert window.library_column_container.isHidden()
    assert window.centralWidget().layout().getContentsMargins() == (0, 0, 0, 0)

    PlayerWindow._exit_karaoke_fullscreen(window)
    app.processEvents()

    assert window._video_fullscreen is False
    assert not window.isFullScreen()
    assert karaoke_widget.parentWidget() is original_parent
    assert karaoke_widget.windowFlags() & QtCore.Qt.WindowType.Window == 0
    assert not window.now_playing.isHidden()
    assert not window.library_column_container.isHidden()
    assert window.centralWidget().layout().getContentsMargins() == original_margins
    window.close()


def test_escape_delegates_to_shared_exit_video_fullscreen():
    app, window, page, karaoke_widget, backend = _main_karaoke_window()
    PlayerWindow._toggle_karaoke_fullscreen(window)
    QTest.qWait(125)
    assert window._video_fullscreen is True

    karaoke_widget.escape_pressed.emit()
    app.processEvents()

    assert window._video_fullscreen is False
    assert karaoke_widget.parentWidget() is page
    window.close()


def test_toggle_is_a_no_op_for_audio():
    app, window, page, karaoke_widget, backend = _main_karaoke_window()
    window._current_media_type = MediaType.AUDIO

    PlayerWindow._toggle_karaoke_fullscreen(window)

    assert window._video_fullscreen is False
    window.close()


def test_party_mode_hosted_karaoke_fullscreen_uses_party_presentation_without_reparenting():
    _app()
    window = QtWidgets.QMainWindow()
    from billsmusic.cdg import CdgWidget
    party_mode = QtWidgets.QWidget()
    party_mode._video_active = True
    party_mode.enter_video_fullscreen_presentation = MagicMock(return_value={"party": True})
    party_mode.exit_video_fullscreen_presentation = MagicMock()
    party_mode.show()
    QtWidgets.QApplication.processEvents()
    backend = MagicMock()
    window._current_media_type = MediaType.KARAOKE
    window._video_fullscreen = False
    window._video_fullscreen_owner_widget = None
    window._video_fullscreen_restore_state = None
    window.party_mode = party_mode
    window._video_backend = backend
    window.diagnostics = SimpleNamespace(record=lambda *a, **kw: None)
    window._video_fullscreen_stage_owner = lambda: PlayerWindow._video_fullscreen_stage_owner(window)
    window._enter_main_video_fullscreen_presentation = MagicMock()
    window._toggle_video_fullscreen = lambda: PlayerWindow._toggle_video_fullscreen(window)
    window._enter_video_fullscreen = lambda: PlayerWindow._enter_video_fullscreen(window)
    window._exit_video_fullscreen = lambda: PlayerWindow._exit_video_fullscreen(window)
    window.right_display_stack = None  # Party Mode owns its own stage
    window._reconcile_display_stage_with_current_media = (
        lambda: PlayerWindow._reconcile_display_stage_with_current_media(window)
    )
    # Fullscreen transition lifecycle state + the real request seam
    # (karaoke input shares the one video fullscreen mechanism).
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
    window._on_karaoke_double_clicked = (
        lambda *a: PlayerWindow._on_karaoke_double_clicked(window, *a)
    )
    window._on_karaoke_escape_pressed = (
        lambda *a: PlayerWindow._on_karaoke_escape_pressed(window, *a)
    )

    PlayerWindow._toggle_karaoke_fullscreen(window)
    # The toggle is a REQUEST now (see _request_video_fullscreen_state) --
    # the physical transition runs on the next queued GUI turn, once the
    # originating input callback stack has unwound.
    assert window._video_fullscreen is False  # not yet -- still queued
    QTest.qWait(20)
    QtWidgets.QApplication.processEvents()

    assert window._video_fullscreen is True
    assert window._video_fullscreen_owner_widget is party_mode
    party_mode.enter_video_fullscreen_presentation.assert_called_once_with()
    window._enter_main_video_fullscreen_presentation.assert_not_called()

    PlayerWindow._exit_karaoke_fullscreen(window)

    party_mode.exit_video_fullscreen_presentation.assert_called_once_with({"party": True})
    party_mode.close()
    window.close()
