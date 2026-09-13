"""Deterministic proof that _DualDeckCompositorWidget performs a genuine
two-frame cross-dissolve, not just an overlay swap: feeds synthetic,
distinguishable-colour QVideoFrame inputs and asserts on actually rendered
pixels at progress 0.0 (pure A), 0.5 (a real mixture of both), and 1.0
(pure B). A no-crash smoke test alone would not demonstrate this."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtGui, QtWidgets
from PyQt6.QtMultimedia import QVideoFrame

from billsmusic.video_subprocess import _DualDeckCompositorWidget

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _solid_frame(color: QtGui.QColor, size=(64, 64)) -> QVideoFrame:
    image = QtGui.QImage(size[0], size[1], QtGui.QImage.Format.Format_RGB32)
    image.fill(color)
    return QVideoFrame(image)


def _rendered_center_color(widget) -> QtGui.QColor:
    pixmap = widget.grab()
    image = pixmap.toImage()
    return image.pixelColor(image.width() // 2, image.height() // 2)


def _widget():
    _app()
    widget = _DualDeckCompositorWidget()
    widget.resize(64, 64)
    widget.set_frame_a(_solid_frame(QtGui.QColor(255, 0, 0)))
    widget.set_frame_b(_solid_frame(QtGui.QColor(0, 0, 255)))
    return widget


def test_synthetic_frames_are_valid_and_paintable():
    frame = _solid_frame(QtGui.QColor(255, 0, 0))
    assert frame.isValid()


def test_progress_zero_renders_pure_frame_a():
    widget = _widget()
    widget.set_progress(0.0)
    _app().processEvents()
    color = _rendered_center_color(widget)
    assert color.red() > 200
    assert color.blue() < 40


def test_progress_one_renders_pure_frame_b():
    widget = _widget()
    widget.set_progress(1.0)
    _app().processEvents()
    color = _rendered_center_color(widget)
    assert color.blue() > 200
    assert color.red() < 40


def test_progress_half_renders_a_genuine_mixture_of_both():
    widget = _widget()
    widget.set_progress(0.5)
    _app().processEvents()
    color = _rendered_center_color(widget)
    # Both channels must genuinely be present -- not a hard cut to either
    # source, and not some unrelated third colour.
    assert 60 < color.red() < 200
    assert 60 < color.blue() < 200
    assert color.green() < 40


def test_progress_quarter_and_three_quarter_are_distinct_and_monotonic():
    widget = _widget()
    widget.set_progress(0.25)
    _app().processEvents()
    quarter = _rendered_center_color(widget)
    widget.set_progress(0.75)
    _app().processEvents()
    three_quarter = _rendered_center_color(widget)
    # Blue contribution must increase and red contribution decrease as
    # progress advances -- a real, monotonic dissolve, not a step function.
    assert three_quarter.blue() > quarter.blue()
    assert three_quarter.red() < quarter.red()


def test_no_frame_b_yet_renders_only_frame_a_even_at_nonzero_progress():
    _app()
    widget = _DualDeckCompositorWidget()
    widget.resize(64, 64)
    widget.set_frame_a(_solid_frame(QtGui.QColor(255, 0, 0)))
    widget.set_progress(0.8)  # progress without a B frame must not crash/blend garbage
    _app().processEvents()
    color = _rendered_center_color(widget)
    assert color.red() > 200
    assert color.blue() < 40


def test_clear_frame_b_returns_to_pure_frame_a():
    widget = _widget()
    widget.set_progress(1.0)
    widget.clear_frame_b()
    widget.set_progress(1.0)  # progress stays high, but B is gone
    _app().processEvents()
    color = _rendered_center_color(widget)
    assert color.red() > 200
    assert color.blue() < 40
