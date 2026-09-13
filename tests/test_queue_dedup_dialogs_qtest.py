"""Drives the real duplicate-confirmation QDialogs with actual QTest mouse
clicks and key presses (not monkeypatched stand-ins), so the button wiring,
default-button/Escape behavior and dialog text are exercised for real."""
import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets
from PyQt6.QtTest import QTest

from billsmusic.window import PlayerWindow
from test_queue_add_dedup_guard import DedupGuardHarness as _BaseHarness, _app


def DedupGuardHarness(*args, **kwargs):
    """The shared harness stubs `_confirm_*` to raise-if-called (so other
    tests fail loudly on an unexpected dialog). These tests are specifically
    about the real dialogs, so rebind the actual PlayerWindow methods."""
    window = _BaseHarness(*args, **kwargs)
    window._confirm_batch_duplicate_add = types.MethodType(
        PlayerWindow._confirm_batch_duplicate_add, window
    )
    window._confirm_single_duplicate_add = types.MethodType(
        PlayerWindow._confirm_single_duplicate_add, window
    )
    return window


def _click_dialog_button(text):
    dlg = QtWidgets.QApplication.activeModalWidget()
    assert dlg is not None, "no modal dialog was open to click"
    button = next(
        (b for b in dlg.findChildren(QtWidgets.QPushButton) if b.text() == text),
        None,
    )
    assert button is not None, f"no button labelled {text!r} found in dialog"
    QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)


def _press_escape_on_dialog():
    dlg = QtWidgets.QApplication.activeModalWidget()
    assert dlg is not None, "no modal dialog was open to press Escape on"
    QTest.keyClick(dlg, QtCore.Qt.Key.Key_Escape)


# -- batch (multi-track) duplicate dialog -----------------------------------

def test_batch_dialog_click_add_new_only_returns_new_only():
    _app()
    window = DedupGuardHarness()
    QtCore.QTimer.singleShot(0, lambda: _click_dialog_button("Add New Only"))
    assert window._confirm_batch_duplicate_add(2, 3, 5) == "new_only"


def test_batch_dialog_click_add_all_returns_all():
    _app()
    window = DedupGuardHarness()
    QtCore.QTimer.singleShot(0, lambda: _click_dialog_button("Add All"))
    assert window._confirm_batch_duplicate_add(2, 3, 5) == "all"


def test_batch_dialog_click_cancel_returns_cancel():
    _app()
    window = DedupGuardHarness()
    QtCore.QTimer.singleShot(0, lambda: _click_dialog_button("Cancel"))
    assert window._confirm_batch_duplicate_add(2, 3, 5) == "cancel"


def test_batch_dialog_escape_key_returns_cancel():
    _app()
    window = DedupGuardHarness()
    QtCore.QTimer.singleShot(0, _press_escape_on_dialog)
    assert window._confirm_batch_duplicate_add(2, 3, 5) == "cancel"


def test_batch_dialog_add_new_only_is_the_default_button():
    _app()
    window = DedupGuardHarness()

    def _check_default_then_close():
        dlg = QtWidgets.QApplication.activeModalWidget()
        default_buttons = [
            b for b in dlg.findChildren(QtWidgets.QPushButton) if b.isDefault()
        ]
        assert len(default_buttons) == 1
        assert default_buttons[0].text() == "Add New Only"
        _click_dialog_button("Cancel")

    QtCore.QTimer.singleShot(0, _check_default_then_close)
    window._confirm_batch_duplicate_add(2, 3, 5)


def test_batch_dialog_text_is_singular_for_one_duplicate():
    _app()
    window = DedupGuardHarness()
    captured = {}

    def _capture_then_close():
        dlg = QtWidgets.QApplication.activeModalWidget()
        label = dlg.findChild(QtWidgets.QLabel)
        captured["text"] = label.text()
        _click_dialog_button("Cancel")

    QtCore.QTimer.singleShot(0, _capture_then_close)
    window._confirm_batch_duplicate_add(1, 4, 5)
    assert "1 track is already in Up Next" in captured["text"]
    assert "4 new tracks" in captured["text"]


def test_batch_dialog_is_discoverable_by_title_and_button_text():
    _app()
    window = DedupGuardHarness()
    captured = {}

    def _capture_then_close():
        dlg = QtWidgets.QApplication.activeModalWidget()
        captured["title"] = dlg.windowTitle()
        captured["buttons"] = sorted(
            b.text() for b in dlg.findChildren(QtWidgets.QPushButton)
        )
        _click_dialog_button("Cancel")

    QtCore.QTimer.singleShot(0, _capture_then_close)
    window._confirm_batch_duplicate_add(2, 3, 5)
    assert captured["title"] == "Duplicate Tracks in Up Next"
    assert captured["buttons"] == ["Add All", "Add New Only", "Cancel"]


# -- single-track duplicate dialog -------------------------------------------

def test_single_dialog_click_add_again_returns_add_again():
    _app()
    window = DedupGuardHarness()
    QtCore.QTimer.singleShot(0, lambda: _click_dialog_button("Add Again"))
    assert window._confirm_single_duplicate_add() == "add_again"


def test_single_dialog_click_cancel_returns_cancel():
    _app()
    window = DedupGuardHarness()
    QtCore.QTimer.singleShot(0, lambda: _click_dialog_button("Cancel"))
    assert window._confirm_single_duplicate_add() == "cancel"


def test_single_dialog_escape_key_returns_cancel():
    _app()
    window = DedupGuardHarness()
    QtCore.QTimer.singleShot(0, _press_escape_on_dialog)
    assert window._confirm_single_duplicate_add() == "cancel"


def test_single_dialog_add_again_is_the_default_button():
    _app()
    window = DedupGuardHarness()

    def _check_default_then_close():
        dlg = QtWidgets.QApplication.activeModalWidget()
        default_buttons = [
            b for b in dlg.findChildren(QtWidgets.QPushButton) if b.isDefault()
        ]
        assert len(default_buttons) == 1
        assert default_buttons[0].text() == "Add Again"
        _click_dialog_button("Cancel")

    QtCore.QTimer.singleShot(0, _check_default_then_close)
    window._confirm_single_duplicate_add()


def test_single_dialog_text_and_discoverability():
    _app()
    window = DedupGuardHarness()
    captured = {}

    def _capture_then_close():
        dlg = QtWidgets.QApplication.activeModalWidget()
        captured["title"] = dlg.windowTitle()
        label = dlg.findChild(QtWidgets.QLabel)
        captured["text"] = label.text()
        _click_dialog_button("Cancel")

    QtCore.QTimer.singleShot(0, _capture_then_close)
    window._confirm_single_duplicate_add()
    assert captured["title"] == "Duplicate Track in Up Next"
    assert captured["text"] == "This track is already in Up Next."


# -- end-to-end through the shared guard, real dialog, real click -----------

def test_guard_end_to_end_with_real_dialog_click_add_new_only():
    """No monkeypatching of the confirm methods at all -- the real dialog
    opens, QTest clicks the real button, and the guard applies the result."""
    _app()
    window = DedupGuardHarness(queue=["dup1.mp3", "dup2.mp3"])
    QtCore.QTimer.singleShot(0, lambda: _click_dialog_button("Add New Only"))
    outcome = window._add_to_queue_with_dedup_guard(
        ["new1.mp3", "dup1.mp3", "new2.mp3", "dup2.mp3"]
    )
    assert window.queue == ["dup1.mp3", "dup2.mp3", "new1.mp3", "new2.mp3"]
    assert outcome.added_count == 2
    assert outcome.duplicate_count == 2
    assert window.accessible_messages == ["2 tracks added; 2 duplicates skipped."]
