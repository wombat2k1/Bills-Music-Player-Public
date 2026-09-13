"""v1.0.72 real-device acceptance correction: a thin animated coloured
strip (ShimmerFrame's decorative rotating-gradient border, meant to
complement the already-colourful visualiser) was visible around the
video display area while a video played -- confirmed via a real,
rendered-and-grabbed screenshot of the exact right_display_stack
construction during investigation. Root cause: _video_output_page was
wrapped in the same ShimmerFrame every other stack page uses, which
insets its child by 1px on every side specifically so its border has
room to paint -- against a plain black video, that border reads as a
stray coloured strip rather than a subtle glow.

Fix: video_output_widget is now used directly as the stack page (no
wrapper), so it fills right_display_stack's rect with zero inset and no
border paints at all. This is a real, real-PlayerWindow, real-event-loop
regression test (not mocks) for the actual layout invariant, since the
bug was a real Qt geometry/paint fact, not something a fake widget graph
could have caught.
"""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets

from billsmusic.widgets import ShimmerFrame
from billsmusic.window import PlayerWindow

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _pump(seconds: float = 0.5):
    app = _app()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.002)


def _build_window(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _app()
    window = PlayerWindow()
    window.resize(1000, 800)
    window.show()
    _pump()
    return window


def test_video_output_page_is_not_shimmer_wrapped(tmp_path, monkeypatch):
    """The specific regression: video_output_widget must be the stack
    page directly, not wrapped in the decorative ShimmerFrame border
    every other page uses."""
    window = _build_window(tmp_path, monkeypatch)
    try:
        assert window._video_output_page is window.video_output_widget
        assert not isinstance(window._video_output_page, ShimmerFrame)
    finally:
        window.close()


def test_other_display_pages_keep_their_shimmer_decoration(tmp_path, monkeypatch):
    """Only the video page changed -- the visualiser (or its
    unavailable-notice fallback) and karaoke keep their existing,
    already-accepted shimmer border untouched."""
    window = _build_window(tmp_path, monkeypatch)
    try:
        assert isinstance(window._normal_display_page, ShimmerFrame)
        assert isinstance(window._karaoke_output_page, ShimmerFrame)
        assert isinstance(window._video_loading_page, ShimmerFrame)
        assert isinstance(window._video_error_page, ShimmerFrame)
    finally:
        window.close()


def test_video_output_widget_fills_the_display_stack_with_no_inset(tmp_path, monkeypatch):
    """Real geometry, real layout pass: once video becomes the current
    page, video_output_widget's rect must exactly match
    right_display_stack's contents rect -- no 1px (or any) gap on any
    edge for a border to show through."""
    window = _build_window(tmp_path, monkeypatch)
    try:
        window._show_video_output_page()
        _pump()

        stack_rect = window.right_display_stack.contentsRect()
        widget_geo = window.video_output_widget.geometry()

        assert widget_geo.width() == stack_rect.width()
        assert widget_geo.height() == stack_rect.height()
        assert widget_geo.x() == 0
        assert widget_geo.y() == 0
        assert window.right_display_stack.currentWidget() is window.video_output_widget
    finally:
        window.close()


def test_switching_back_to_music_restores_the_visualiser_page(tmp_path, monkeypatch):
    """Video -> Music: the stack must return to exactly the page it had
    before (visualiser or its unavailable-notice fallback), with no
    lingering hidden state and no change to the rest of the layout."""
    window = _build_window(tmp_path, monkeypatch)
    try:
        original_page = window.right_display_stack.currentWidget()
        assert original_page is window._normal_display_page

        window._show_video_output_page()
        _pump()
        assert window.right_display_stack.currentWidget() is window.video_output_widget

        window._show_normal_display_page()
        _pump()

        assert window.right_display_stack.currentWidget() is window._normal_display_page
        assert window.right_display_stack.currentWidget() is original_page
    finally:
        window.close()
