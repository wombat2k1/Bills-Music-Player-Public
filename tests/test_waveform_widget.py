import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest

from PyQt6 import QtCore, QtTest, QtWidgets

from billsmusic.waveform import WaveformData
from billsmusic.waveform_widget import WaveformSeekBar

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


class _CountingSeekBar(WaveformSeekBar):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.update_calls = 0

    def update(self):
        self.update_calls += 1
        super().update()


def _fake_waveform(n=100, amplitude=0.8, duration_s=120.0):
    return WaveformData(
        schema_version=1, peak_count=n, duration_s=duration_s, channels=1,
        mins=[-amplitude] * n, maxs=[amplitude] * n,
    )


class WaveformSeekBarTests(unittest.TestCase):
    def setUp(self):
        _app()

    def test_set_position_seconds_clips_ratio_to_0_1(self):
        widget = WaveformSeekBar()
        widget.set_position_seconds(current_s=-5, length_s=100)
        self.assertEqual(widget.value(), widget.minimum())
        widget.set_position_seconds(current_s=999, length_s=100)
        self.assertEqual(widget.value(), widget.maximum())

    def test_set_position_seconds_ignores_non_positive_length(self):
        widget = WaveformSeekBar()
        widget.setValue(500)
        widget.set_position_seconds(current_s=10, length_s=0)
        self.assertEqual(widget.value(), 500)

    def test_set_position_seconds_skips_repaint_when_pixel_unchanged(self):
        widget = _CountingSeekBar()
        widget.resize(500, 48)
        widget.update_calls = 0
        widget.set_position_seconds(current_s=10.0, length_s=100.0)
        self.assertEqual(widget.update_calls, 1)
        # Small enough change in position that the rounded pixel is identical.
        widget.set_position_seconds(current_s=10.05, length_s=100.0)
        self.assertEqual(widget.update_calls, 1)
        # A large enough jump must move the pixel and trigger a repaint.
        widget.set_position_seconds(current_s=50.0, length_s=100.0)
        self.assertEqual(widget.update_calls, 2)

    def test_set_waveform_skips_repaint_when_data_object_is_unchanged(self):
        # Party Mode's sync_from_owner re-sends the owner's cached
        # WaveformData reference every ~200ms tick, not just when a new
        # track's decode actually finishes -- set_waveform must not treat
        # that as new data, or _ensure_layers rebuilds its full bar
        # geometry on the very next paint for no reason, every tick.
        widget = _CountingSeekBar()
        widget.resize(500, 48)
        data = _fake_waveform()

        widget.set_waveform(data)
        self.assertEqual(widget.update_calls, 1)
        widget.set_waveform(data)
        self.assertEqual(widget.update_calls, 1)

        other_data = _fake_waveform()
        widget.set_waveform(other_data)
        self.assertEqual(widget.update_calls, 2)

    def test_seek_to_pixel_matches_expected_ratio(self):
        widget = WaveformSeekBar()
        widget.resize(1000, 48)
        widget._seek_to_pixel(250)
        self.assertEqual(widget.value(), 250)
        widget._seek_to_pixel(0)
        self.assertEqual(widget.value(), 0)
        widget._seek_to_pixel(1000)
        self.assertEqual(widget.value(), widget.maximum())

    def test_seek_to_pixel_clips_past_edges(self):
        widget = WaveformSeekBar()
        widget.resize(1000, 48)
        widget._seek_to_pixel(-500)
        self.assertEqual(widget.value(), 0)
        widget._seek_to_pixel(5000)
        self.assertEqual(widget.value(), widget.maximum())

    def test_mouse_press_drag_release_emits_seek_requested(self):
        widget = WaveformSeekBar()
        widget.resize(1000, 48)
        emitted = []
        widget.seek_requested.connect(emitted.append)

        QtTest.QTest.mousePress(
            widget, QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(100, 24),
        )
        self.assertTrue(widget.isSliderDown())
        QtTest.QTest.mouseMove(widget, QtCore.QPoint(300, 24))
        QtTest.QTest.mouseRelease(
            widget, QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(300, 24),
        )
        self.assertFalse(widget.isSliderDown())
        self.assertEqual(len(emitted), 1)
        self.assertAlmostEqual(emitted[0], 0.3, places=2)

    def test_keyboard_seek_left_right_home_end(self):
        widget = WaveformSeekBar()
        widget.resize(400, 48)
        widget.setValue(500)
        widget.show()
        widget.setFocus()

        QtTest.QTest.keyClick(widget, QtCore.Qt.Key.Key_Right)
        self.assertEqual(widget.value(), 500 + widget.singleStep())

        QtTest.QTest.keyClick(widget, QtCore.Qt.Key.Key_Left)
        self.assertEqual(widget.value(), 500)

        QtTest.QTest.keyClick(widget, QtCore.Qt.Key.Key_Home)
        self.assertEqual(widget.value(), widget.minimum())

        QtTest.QTest.keyClick(widget, QtCore.Qt.Key.Key_End)
        self.assertEqual(widget.value(), widget.maximum())
        widget.hide()

    def test_accessible_slider_semantics(self):
        widget = WaveformSeekBar()
        self.assertIsInstance(widget, QtWidgets.QAbstractSlider)
        self.assertEqual(widget.accessibleName(), "Playback position")
        self.assertEqual(widget.accessibleDescription(), "Seek within the current track")
        self.assertEqual(widget.focusPolicy(), QtCore.Qt.FocusPolicy.StrongFocus)

    def test_placeholder_mode_paints_without_exception(self):
        widget = WaveformSeekBar()
        widget.resize(300, 48)
        widget.set_placeholder("C:/Music/track.flac")
        pixmap = widget.grab()
        self.assertFalse(pixmap.isNull())

    def test_video_progress_clears_old_waveform_and_identifies_timeline(self):
        widget = WaveformSeekBar()
        widget.set_waveform(_fake_waveform())

        widget.set_video_progress("C:/Videos/clip.mp4")

        self.assertIsNone(widget._waveform)
        self.assertEqual(widget._placeholder_mode, "video")
        self.assertEqual(widget.toolTip(), "Video playback position")

    def test_failed_waveform_has_an_explicit_unavailable_state(self):
        widget = WaveformSeekBar()
        widget.set_placeholder("C:/Music/broken.flac")

        widget.set_waveform(None)

        self.assertEqual(widget._placeholder_mode, "unavailable")
        self.assertEqual(widget.toolTip(), "Waveform unavailable")

    def test_waveform_mode_paints_without_exception(self):
        widget = WaveformSeekBar()
        widget.resize(300, 48)
        widget.set_waveform(_fake_waveform())
        widget.set_position_seconds(60.0, 120.0)
        pixmap = widget.grab()
        self.assertFalse(pixmap.isNull())
        self.assertEqual(widget._duration_s, 120.0)


if __name__ == "__main__":
    unittest.main()
