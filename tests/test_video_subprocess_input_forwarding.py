"""Confirms that a real double-click, right-click, and Escape key press
delivered to the child process's own video widget (video_subprocess.py)
actually get forwarded over the IPC protocol -- this is the piece that
can't be exercised from the parent side, since the whole reason
double-click/context-menu/Escape need forwarding at all is that these
input events land on the CHILD's window, never on the parent's.

Uses QTest to synthesize real Qt mouse/keyboard events on the controller's
actual widget rather than calling its event handlers directly, so this
also confirms the handlers are wired to the events they claim to handle.
Still can't confirm real cross-process window embedding itself (that needs
the user's actual desktop) -- this confirms the input-handling half.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtCore, QtWidgets
from PyQt6.QtTest import QTest

from billsmusic.video_subprocess import VideoSubprocessController
from video_controller_teardown import release_and_collect

_APP = None

# Every real controller built in this process is released deterministically
# in its own test's teardown -- see video_controller_teardown.py for the
# GIL/FFmpeg-thread deadlock a garbage-collected QMediaPlayer otherwise
# causes in whichever later test the collector happens to run in.
_CONTROLLERS = []


@pytest.fixture(autouse=True)
def _release_real_controllers(monkeypatch):
    # Depends on monkeypatch so this teardown runs before its patches
    # (_emit, _StdinReaderThread.start) are undone.
    yield
    release_and_collect(_CONTROLLERS)


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _controller(monkeypatch):
    _app()
    # Avoid a real stdin-reading background thread / real QMediaPlayer
    # decode threads in this test -- only the widget's own input-event
    # wiring is under test here.
    monkeypatch.setattr(
        "billsmusic.video_subprocess._StdinReaderThread.start", lambda self: None,
    )
    events = []
    monkeypatch.setattr(
        "billsmusic.video_subprocess._emit", lambda obj: events.append(obj),
    )
    controller = VideoSubprocessController()
    _CONTROLLERS.append(controller)
    return controller, events


def test_real_double_click_on_the_widget_forwards_double_clicked(monkeypatch):
    controller, events = _controller(monkeypatch)

    QTest.mouseDClick(
        controller.widget, QtCore.Qt.MouseButton.LeftButton,
    )

    assert {"event": "double_clicked"} in events


def test_real_right_click_on_the_widget_forwards_context_menu_requested(monkeypatch):
    controller, events = _controller(monkeypatch)

    QTest.mousePress(
        controller.widget, QtCore.Qt.MouseButton.RightButton,
    )

    assert {"event": "context_menu_requested"} in events


def test_real_escape_key_press_on_the_widget_forwards_escape_pressed(monkeypatch):
    controller, events = _controller(monkeypatch)
    controller.widget.setFocus()

    QTest.keyClick(controller.widget, QtCore.Qt.Key.Key_Escape)

    assert {"event": "escape_pressed"} in events


def test_real_left_click_does_not_forward_context_menu_requested(monkeypatch):
    controller, events = _controller(monkeypatch)

    QTest.mousePress(controller.widget, QtCore.Qt.MouseButton.LeftButton)

    assert {"event": "context_menu_requested"} not in events


def test_real_non_escape_key_press_does_not_forward_escape_pressed(monkeypatch):
    controller, events = _controller(monkeypatch)
    controller.widget.setFocus()

    QTest.keyClick(controller.widget, QtCore.Qt.Key.Key_A)

    assert {"event": "escape_pressed"} not in events


def test_resize_converts_between_parent_and_child_dpi(monkeypatch):
    controller, _events = _controller(monkeypatch)
    resized = []
    monkeypatch.setattr(controller.widget, "devicePixelRatioF", lambda: 1.0)
    monkeypatch.setattr(
        controller.widget, "resize", lambda width, height: resized.append((width, height)),
    )

    controller._on_line_received(json.dumps({
        "cmd": "resize",
        "width": 800,
        "height": 450,
        "device_pixel_ratio": 1.5,
    }))

    assert resized == [(1200, 675)]


def test_resize_without_parent_dpi_remains_backward_compatible(monkeypatch):
    controller, _events = _controller(monkeypatch)
    resized = []
    monkeypatch.setattr(controller.widget, "devicePixelRatioF", lambda: 1.5)
    monkeypatch.setattr(
        controller.widget, "resize", lambda width, height: resized.append((width, height)),
    )

    controller._on_line_received(json.dumps({
        "cmd": "resize", "width": 800, "height": 450,
    }))

    assert resized == [(800, 450)]


def test_position_heartbeat_repeats_duration_for_self_healing_progress(monkeypatch):
    controller, events = _controller(monkeypatch)
    monkeypatch.setattr(controller.player, "duration", lambda: 9000)

    controller._on_position_changed(4200)

    assert {
        "event": "position_changed",
        "token": controller._token,
        "position_ms": 4200,
        "duration_ms": 9000,
    } in events
