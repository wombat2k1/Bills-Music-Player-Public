"""Party Mode video takeover: reuses the single shared video backend/output
widget, never creates a second QMediaPlayer, and restores the previously
selected layout when video ends or Party Mode hides."""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets

from billsmusic.media_type import MediaType
from billsmusic.window import PlayerWindow

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _window_with_party_mode(party_mode_visible):
    _app()
    video_backend = MagicMock()
    main_output = object()
    party_mode = SimpleNamespace(
        video_widget=object(),
        _video_active=False,
        isVisible=lambda: party_mode_visible,
        show_video_calls=0,
        return_to_normal_calls=0,
    )
    party_mode.show_video = lambda: (
        setattr(party_mode, "_video_active", True),
        setattr(party_mode, "show_video_calls", party_mode.show_video_calls + 1),
    )
    party_mode.return_to_normal_layout = lambda: (
        setattr(party_mode, "_video_active", False),
        setattr(party_mode, "return_to_normal_calls", party_mode.return_to_normal_calls + 1),
    )
    window = SimpleNamespace(
        _current_media_type=MediaType.VIDEO,
        _video_fullscreen=False,
        _video_fullscreen_owner_widget=None,
        _video_backend=video_backend,
        video_output_widget=main_output,
        party_mode=party_mode,
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None),
    )
    window._route_video_output = lambda target: PlayerWindow._route_video_output(window, target)
    window._attach_video_to_party_mode = lambda: PlayerWindow._attach_video_to_party_mode(window)
    window._finish_party_video_attach = (
        lambda party: PlayerWindow._finish_party_video_attach(window, party)
    )
    window._detach_video_from_party_mode = lambda: PlayerWindow._detach_video_from_party_mode(window)
    return window, video_backend, party_mode, main_output


def test_video_started_while_party_mode_visible_moves_output_there():
    window, video_backend, party_mode, _main = _window_with_party_mode(party_mode_visible=True)

    window._attach_video_to_party_mode()

    video_backend.attach_output.assert_called_once_with(party_mode.video_widget)
    assert party_mode.show_video_calls == 1
    assert party_mode._video_active is True


def test_video_started_while_party_mode_hidden_does_not_touch_it():
    window, video_backend, party_mode, _main = _window_with_party_mode(party_mode_visible=False)

    window._attach_video_to_party_mode()

    video_backend.attach_output.assert_not_called()
    assert party_mode.show_video_calls == 0


def test_opening_party_mode_from_video_fullscreen_defers_output_move(monkeypatch):
    window, video_backend, party_mode, _main = _window_with_party_mode(party_mode_visible=True)
    callbacks = []
    events = []
    window._video_fullscreen = True
    window._exit_video_fullscreen = lambda: (
        events.append("exit_fullscreen"),
        setattr(window, "_video_fullscreen", False),
    )
    monkeypatch.setattr(
        "billsmusic.window.QtCore.QTimer.singleShot",
        lambda delay, callback: callbacks.append(callback),
    )

    window._attach_video_to_party_mode()

    assert events == ["exit_fullscreen"]
    assert party_mode._video_active is True
    assert party_mode.show_video_calls == 1
    video_backend.attach_output.assert_not_called()
    assert len(callbacks) == 1

    callbacks[0]()

    video_backend.attach_output.assert_called_once_with(party_mode.video_widget)
    assert party_mode.show_video_calls == 1


def test_next_video_keeps_existing_party_mode_fullscreen():
    window, video_backend, party_mode, _main = _window_with_party_mode(party_mode_visible=True)
    window._video_fullscreen = True
    window._video_fullscreen_owner_widget = party_mode
    party_mode._video_active = True
    window._exit_video_fullscreen = MagicMock()

    window._attach_video_to_party_mode()

    window._exit_video_fullscreen.assert_not_called()
    video_backend.attach_output.assert_not_called()
    assert window._video_fullscreen is True
    assert party_mode.show_video_calls == 0


def test_deferred_party_mode_attach_is_cancelled_if_video_ends(monkeypatch):
    window, video_backend, party_mode, _main = _window_with_party_mode(party_mode_visible=True)
    callbacks = []
    window._video_fullscreen = True
    window._exit_video_fullscreen = lambda: setattr(window, "_video_fullscreen", False)
    monkeypatch.setattr(
        "billsmusic.window.QtCore.QTimer.singleShot",
        lambda delay, callback: callbacks.append(callback),
    )

    window._attach_video_to_party_mode()
    window._current_media_type = MediaType.AUDIO
    callbacks[0]()

    video_backend.attach_output.assert_not_called()


def test_detach_reattaches_to_main_window_and_restores_layout():
    window, video_backend, party_mode, main_output = _window_with_party_mode(party_mode_visible=True)
    window._attach_video_to_party_mode()

    window._detach_video_from_party_mode()

    video_backend.attach_output.assert_called_with(main_output)
    assert party_mode.return_to_normal_calls == 1
    assert party_mode._video_active is False


def test_detach_is_a_no_op_on_layout_restore_when_party_mode_never_showed_video():
    window, video_backend, party_mode, main_output = _window_with_party_mode(party_mode_visible=False)

    window._detach_video_from_party_mode()

    # Output is still safely pointed at the main window...
    video_backend.attach_output.assert_called_once_with(main_output)
    # ...but Party Mode's layout is untouched since it never took over.
    assert party_mode.return_to_normal_calls == 0


def test_hiding_party_mode_exits_presentation_before_reattaching_to_main():
    window, video_backend, party_mode, _main = _window_with_party_mode(party_mode_visible=True)
    events = []
    window._video_fullscreen = True
    window._exit_video_fullscreen = lambda: (
        events.append("exit_fullscreen"),
        setattr(window, "_video_fullscreen", False),
    )
    party_mode._video_active = True

    window._detach_video_from_party_mode()

    assert events == ["exit_fullscreen"]
    video_backend.attach_output.assert_called_once_with(window.video_output_widget)
    assert party_mode.return_to_normal_calls == 1


def test_no_second_video_subprocess_is_ever_launched_for_party_mode(monkeypatch):
    # Video playback lives in a separate child process now (see
    # video_backend.py's module docstring for why); Party Mode must reuse
    # the owner's single backend/subprocess via attach_output() rather than
    # constructing its own QtVideoPlaybackBackend, which would launch a
    # second child process.
    _app()
    from PyQt6 import QtCore
    from billsmusic.video_backend import QtVideoPlaybackBackend

    starts = []
    monkeypatch.setattr(QtCore.QProcess, "start", lambda self, *a, **kw: starts.append(self))

    backend = QtVideoPlaybackBackend()
    assert len(starts) == 1

    backend.shutdown()
