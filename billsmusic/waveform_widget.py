"""Custom-painted waveform seek bar.

A single QWidget (QAbstractSlider subclass) that paints the whole track's
peak envelope in one paintEvent -- no per-bar child widgets -- with a muted
unplayed fill and the app's existing cyan/magenta/purple theme gradient for
the played portion. Subclassing QAbstractSlider (rather than a plain
QWidget) gives keyboard seeking (arrow/Home/End/PageUp/PageDown) and
accessible slider semantics for free, and keeps it API-compatible with the
QSlider it replaces (value()/setValue()/maximum()/blockSignals()), so
existing call sites in window.py and mini_player.py need no changes.
"""
from __future__ import annotations

import time
from typing import Optional

from PyQt6 import QtCore, QtGui, QtWidgets

from .performance_diagnostics import get_diagnostics
from .waveform import WaveformData

_PLAYED_STOPS = (
    (0.0, "#ff3cac"),
    (0.5, "#b14cff"),
    (1.0, "#21e6ff"),
)
_UNPLAYED_COLOR = "#3a2a55"
_GROOVE_COLOR = "#1d1336"
_HOVER_COLOR = "#fff8c8"


def _played_gradient(x0: float, x1: float) -> QtGui.QLinearGradient:
    gradient = QtGui.QLinearGradient(x0, 0, x1, 0)
    for stop, color in _PLAYED_STOPS:
        gradient.setColorAt(stop, QtGui.QColor(color))
    return gradient


class WaveformSeekBar(QtWidgets.QAbstractSlider):
    seek_requested = QtCore.pyqtSignal(float)   # ratio in [0.0, 1.0], emitted on committed seek

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOrientation(QtCore.Qt.Orientation.Horizontal)
        self.setRange(0, 1000)
        self.setSingleStep(10)
        self.setPageStep(50)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(48)
        self.setMouseTracking(True)
        self.setAccessibleName("Playback position")
        self.setAccessibleDescription("Seek within the current track")
        self.setToolTip("Click or drag to seek")

        self._performance_diagnostics = get_diagnostics()

        self._waveform: Optional[WaveformData] = None
        self._pending_path: Optional[str] = None
        self._placeholder_mode = "empty"
        self._dragging = False
        self._hover_x: Optional[int] = None
        self._last_painted_playhead_px: Optional[int] = None
        self._duration_s = 0.0

        # The bar *shapes* only depend on (widget size, which WaveformData)
        # -- never on the played-position pixel, which advances on nearly
        # every ~200ms playback tick. Rendering the unplayed and played bars
        # as two full-width layers ONCE (on data/size change) and, on every
        # tick, just compositing them with a clip rect turns the per-tick
        # repaint into two cheap drawPixmap() blits instead of rebuilding
        # ~1500 bars' worth of QPainterPath geometry every single tick.
        self._unplayed_layer: Optional[QtGui.QPixmap] = None
        self._played_layer: Optional[QtGui.QPixmap] = None
        self._layers_key = None

    # -- public API ------------------------------------------------------

    def set_placeholder(self, path: str):
        """Called when a new track becomes current, before any decode result exists."""
        self._pending_path = path
        self._waveform = None
        self._placeholder_mode = "loading"
        self._invalidate_layers()
        self._last_painted_playhead_px = None
        self.setToolTip("Preparing waveform")
        self.update()

    def set_video_progress(self, path: str):
        """Show a deliberately simple progress strip for video playback."""
        self._pending_path = path
        self._waveform = None
        self._placeholder_mode = "video"
        self._invalidate_layers()
        self._last_painted_playhead_px = None
        self.setToolTip("Video playback position")
        self.update()

    def set_waveform(self, data: Optional[WaveformData]):
        if data is self._waveform and (
            data is not None or self._placeholder_mode == "unavailable"
        ):
            # Same object as last time (e.g. a caller re-syncing every tick
            # from an owner's cached reference, not a new decode result) --
            # skip the invalidate/repaint or _ensure_layers rebuilds its
            # ~1500 bars of QPainterPath geometry on the next paint for no
            # reason, every single call.
            return
        self._waveform = data
        if data is not None:
            self._placeholder_mode = "waveform"
            self._duration_s = data.duration_s
            self.setToolTip("Track playback position")
        else:
            self._placeholder_mode = "unavailable"
            self.setToolTip("Waveform unavailable")
        self._invalidate_layers()
        self._last_painted_playhead_px = None
        self.update()

    def _invalidate_layers(self):
        self._unplayed_layer = None
        self._played_layer = None
        self._layers_key = None

    def resizeEvent(self, event):
        self._invalidate_layers()
        super().resizeEvent(event)

    def set_duration_s(self, seconds: float):
        if seconds and seconds > 0:
            self._duration_s = float(seconds)

    def set_position_seconds(self, current_s: float, length_s: float):
        if length_s <= 0:
            return
        self._duration_s = length_s
        ratio = max(0.0, min(1.0, current_s / length_s))
        maximum = max(1, self.maximum())
        new_value = int(round(ratio * maximum))
        if new_value != self.value():
            self.blockSignals(True)
            self.setValue(new_value)
            self.blockSignals(False)
        playhead_px = int(round(ratio * max(1, self.width())))
        if playhead_px != self._last_painted_playhead_px:
            self._last_painted_playhead_px = playhead_px
            self.update()

    # -- mouse interaction (click-and-drag seeking) -----------------------

    def mousePressEvent(self, event: QtGui.QMouseEvent):
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._dragging = True
        self.setSliderDown(True)
        self._seek_to_pixel(event.position().x())
        event.accept()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent):
        x = int(event.position().x())
        if self._dragging:
            self._seek_to_pixel(x)
        moved = x != self._hover_x
        self._hover_x = x
        self._update_hover_label(x)
        if moved:
            self.update()
        event.accept()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent):
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self._seek_to_pixel(event.position().x())
            self.setSliderDown(False)
            self.seek_requested.emit(self.value() / float(max(1, self.maximum())))
        event.accept()

    def leaveEvent(self, event):
        self._hover_x = None
        QtWidgets.QToolTip.hideText()
        self.update()
        super().leaveEvent(event)

    def _seek_to_pixel(self, x: float):
        width = max(1.0, float(self.width()))
        ratio = max(0.0, min(1.0, x / width))
        self.setValue(int(round(ratio * self.maximum())))

    # -- keyboard seeking --------------------------------------------------

    def keyPressEvent(self, event: QtGui.QKeyEvent):
        old_value = self.value()
        super().keyPressEvent(event)
        if event.isAccepted() and self.value() != old_value:
            # QAbstractSlider's default handling updates value() directly for
            # arrow/Home/End/PageUp/PageDown but never presses/releases the
            # slider -- press+release here reuses the existing seek wiring
            # (window.py's _progress_press/_progress_release) unmodified.
            self.setSliderDown(True)
            self.setSliderDown(False)

    # -- hover / drag target-time label -------------------------------------

    def _update_hover_label(self, x: Optional[int]):
        if x is None or self._duration_s <= 0:
            QtWidgets.QToolTip.hideText()
            return
        width = max(1.0, float(self.width()))
        ratio = max(0.0, min(1.0, x / width))
        target_s = ratio * self._duration_s
        QtWidgets.QToolTip.showText(
            self.mapToGlobal(QtCore.QPoint(x, 0)), _format_mm_ss(target_s), self,
        )

    # -- painting ------------------------------------------------------------

    def paintEvent(self, event):
        started = time.perf_counter()
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        self._paint_track(painter, self.rect())
        painter.end()
        self._record_paint_performance(started)

    def _paint_track(self, painter: QtGui.QPainter, rect: QtCore.QRect):
        maximum = max(1, self.maximum())
        played_ratio = self.value() / float(maximum)
        played_x = int(round(rect.width() * played_ratio))

        if self._waveform is None:
            self._paint_placeholder(painter, rect, played_x)
        else:
            self._ensure_layers(rect)
            painter.drawPixmap(rect.topLeft(), self._unplayed_layer)
            if played_x > 0:
                source = QtCore.QRect(
                    0, 0, min(played_x, self._played_layer.width()), self._played_layer.height(),
                )
                painter.drawPixmap(rect.topLeft(), self._played_layer, source)

        if self._hover_x is not None:
            self._paint_hover_marker(painter, rect, self._hover_x)

    def _paint_placeholder(self, painter: QtGui.QPainter, rect: QtCore.QRect, played_x: int):
        groove_y = rect.height() // 2 - 3
        groove = QtCore.QRect(rect.x(), rect.y() + groove_y, rect.width(), 6)
        painter.fillRect(groove, QtGui.QColor(_GROOVE_COLOR))
        if played_x > 0:
            played_rect = QtCore.QRect(groove.x(), groove.y(), played_x, groove.height())
            painter.fillRect(played_rect, _played_gradient(rect.left(), rect.right()))

        labels = {
            "loading": "Preparing waveform…",
            "unavailable": "Waveform unavailable",
            "video": "Video timeline",
        }
        label = labels.get(self._placeholder_mode)
        if label and self.height() >= 34:
            painter.save()
            painter.setPen(QtGui.QColor("#8f82a8"))
            font = painter.font()
            font.setPointSize(max(7, font.pointSize() - 1))
            painter.setFont(font)
            painter.drawText(
                self.rect().adjusted(4, 2, -4, -2),
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop,
                label,
            )
            painter.restore()

    def _ensure_layers(self, rect: QtCore.QRect):
        """Render the unplayed/played bar layers once per (size, data) --
        NOT per paint. Playback position advances on nearly every tick, so
        this must stay decoupled from played_x, which _paint_track applies
        afterwards via a cheap clipped drawPixmap() rather than by rebuilding
        bar geometry every ~200ms."""
        key = (rect.width(), rect.height(), id(self._waveform))
        if self._layers_key == key and self._unplayed_layer is not None:
            return
        width = max(1, rect.width())
        height = max(1, rect.height())
        mins, maxs = self._waveform.mins, self._waveform.maxs
        n = len(mins)
        if n == 0:
            unplayed = QtGui.QPixmap(width, height)
            unplayed.fill(QtCore.Qt.GlobalColor.transparent)
            painter = QtGui.QPainter(unplayed)
            self._paint_placeholder(painter, QtCore.QRect(0, 0, width, height), 0)
            painter.end()
            played = QtGui.QPixmap(unplayed)
        else:
            unplayed = self._render_bar_layer(width, height, n, mins, maxs, QtGui.QColor(_UNPLAYED_COLOR))
            played = self._render_bar_layer(width, height, n, mins, maxs, _played_gradient(0, width))
        self._unplayed_layer = unplayed
        self._played_layer = played
        self._layers_key = key

    @staticmethod
    def _render_bar_layer(width: int, height: int, n: int, mins, maxs, brush) -> QtGui.QPixmap:
        pixmap = QtGui.QPixmap(width, height)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(brush)
        mid_y = height / 2.0
        bar_w = width / float(n)
        path = QtGui.QPainterPath()
        for i in range(n):
            x = i * bar_w
            top = mid_y - max(1.0, maxs[i] * mid_y)
            bottom = mid_y + max(1.0, -mins[i] * mid_y)
            path.addRect(QtCore.QRectF(x, top, max(1.0, bar_w - 0.5), bottom - top))
        painter.drawPath(path)
        painter.end()
        return pixmap

    def _paint_hover_marker(self, painter: QtGui.QPainter, rect: QtCore.QRect, x: int):
        x = max(rect.left(), min(rect.right(), x))
        pen = QtGui.QPen(QtGui.QColor(_HOVER_COLOR))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawLine(x, rect.top(), x, rect.bottom())

    def _record_paint_performance(self, started: float):
        # Recorded directly rather than via observe_visual_frame(): that
        # helper's "frame_stutter" check is gap-based, tuned for a
        # continuously-animating widget (BeatWidget, ~60fps). This widget
        # repaints on demand -- long, deliberate gaps between paints are the
        # intended behaviour (see set_position_seconds' repaint dedup), not
        # a stutter, so gap-based detection would misreport it as one.
        paint_ms = (time.perf_counter() - started) * 1000.0
        if not self.isVisible():
            return
        self._performance_diagnostics.record(
            "waveform", "paint",
            duration_ms=paint_ms,
            severity="warning" if paint_ms >= 50.0 else "info",
            minimum_level="detailed",
        )


def _format_mm_ss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"
