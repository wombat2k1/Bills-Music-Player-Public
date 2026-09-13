"""Single-widget Qt painter for Phase 1 video transition effects."""
from __future__ import annotations

import math
import random
from typing import Callable, Optional

from PyQt6 import QtCore, QtGui, QtWidgets


class VideoTransitionOverlay(QtWidgets.QWidget):
    """An input-transparent top-level paint layer, hidden while idle.

    The video picture belongs to a different process and is embedded as a
    native child window.  Making this overlay another *child* native widget
    changes Qt/Windows' native ancestry and can displace that foreign window
    or leave an invisible input surface behind.  A transient top-level tool
    window covers the same screen rectangle without ever parenting itself to
    the video host.
    """

    def __init__(self):
        flags = (
            QtCore.Qt.WindowType.Tool
            | QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.NoDropShadowWindowHint
            | QtCore.Qt.WindowType.WindowDoesNotAcceptFocus
            | QtCore.Qt.WindowType.WindowTransparentForInput
        )
        super().__init__(None, flags)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self._host: Optional[QtWidgets.QWidget] = None
        self._host_window: Optional[QtWidgets.QWidget] = None
        self._animation: Optional[QtCore.QVariantAnimation] = None
        self._effect = "Fade Black"
        self._phase = "hold"
        self._progress = 1.0
        self._finished_callback: Optional[Callable[[], None]] = None
        self._pixel_order = list(range(16 * 9))
        self._seed = 0
        self._shutting_down = False
        self.hide()

    def set_host(self, host: Optional[QtWidgets.QWidget]) -> None:
        if host is None:
            raise RuntimeError("No transition overlay host is available")
        if host is self._host:
            self._sync_geometry()
            self._sync_transient_owner()
            self.raise_()
            return
        self._remove_host_event_filters()
        self._host = host
        self._host_window = host.window()
        host.installEventFilter(self)
        if self._host_window is not host:
            self._host_window.installEventFilter(self)
        self._sync_geometry()
        self._sync_transient_owner()
        self.raise_()

    def animate_outgoing(
        self, effect: str, duration_ms: int, finished: Callable[[], None],
    ) -> None:
        self._begin(effect, "outgoing", 0.0, 1.0, duration_ms, finished)

    def animate_incoming(
        self, effect: str, duration_ms: int, finished: Callable[[], None],
    ) -> None:
        self._begin(effect, "incoming", 1.0, 0.0, duration_ms, finished)

    def show_covered(self, effect: str) -> None:
        self._stop_animation()
        self._effect = effect
        self._phase = "hold"
        self._progress = 1.0
        self.show()
        self.raise_()
        self.update()

    def cancel(self) -> None:
        self._stop_animation()
        self.hide()
        self._progress = 0.0
        self._finished_callback = None

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self.cancel()
        self._remove_host_event_filters()
        self._host = None
        self._host_window = None
        self.deleteLater()

    def eventFilter(self, watched, event):
        if watched in (self._host, self._host_window) and event.type() in (
            QtCore.QEvent.Type.Resize,
            QtCore.QEvent.Type.Show,
            QtCore.QEvent.Type.Move,
            QtCore.QEvent.Type.LayoutRequest,
            QtCore.QEvent.Type.WindowStateChange,
        ):
            self._sync_geometry()
            if self.isVisible():
                self.raise_()
        return super().eventFilter(watched, event)

    def _sync_geometry(self) -> None:
        if self._host is not None:
            try:
                global_top_left = self._host.mapToGlobal(QtCore.QPoint(0, 0))
                self.setGeometry(QtCore.QRect(global_top_left, self._host.size()))
            except RuntimeError:
                pass

    def _sync_transient_owner(self) -> None:
        """Keep the tool above its app window without changing host ancestry."""
        if self._host_window is None:
            return
        try:
            self.winId()
            overlay_handle = self.windowHandle()
            owner_handle = self._host_window.windowHandle()
            if overlay_handle is not None and owner_handle is not None:
                overlay_handle.setTransientParent(owner_handle)
        except RuntimeError:
            pass

    def _remove_host_event_filters(self) -> None:
        for watched in (self._host, self._host_window):
            if watched is None:
                continue
            try:
                watched.removeEventFilter(self)
            except RuntimeError:
                pass

    def _begin(
        self,
        effect: str,
        phase: str,
        start: float,
        end: float,
        duration_ms: int,
        finished: Callable[[], None],
    ) -> None:
        if self._host is None:
            raise RuntimeError("Transition overlay has no host")
        self._stop_animation()
        self._effect = effect
        self._phase = phase
        self._progress = start
        self._finished_callback = finished
        self._seed = random.randrange(1 << 30)
        rng = random.Random(self._seed)
        self._pixel_order = list(range(16 * 9))
        rng.shuffle(self._pixel_order)
        animation = QtCore.QVariantAnimation(self)
        animation.setStartValue(start)
        animation.setEndValue(end)
        animation.setDuration(max(1, int(duration_ms)))
        easing = QtCore.QEasingCurve.Type.InOutQuad
        if effect.startswith("Push"):
            easing = (
                QtCore.QEasingCurve.Type.InCubic
                if phase == "outgoing"
                else QtCore.QEasingCurve.Type.OutCubic
            )
        elif effect in ("Flash", "RGB Glitch"):
            easing = QtCore.QEasingCurve.Type.OutQuad
        animation.setEasingCurve(easing)
        animation.valueChanged.connect(self._set_progress)
        animation.finished.connect(self._animation_finished)
        self._animation = animation
        self.show()
        self.raise_()
        self.update()
        animation.start()

    def _stop_animation(self) -> None:
        animation, self._animation = self._animation, None
        if animation is not None:
            try:
                animation.stop()
                animation.deleteLater()
            except RuntimeError:
                pass

    def _set_progress(self, value) -> None:
        self._progress = max(0.0, min(1.0, float(value)))
        self.update()

    def _animation_finished(self) -> None:
        animation, self._animation = self._animation, None
        if animation is not None:
            animation.deleteLater()
        callback, self._finished_callback = self._finished_callback, None
        if self._phase == "incoming":
            self.hide()
        if callback is not None:
            callback()

    def paintEvent(self, event) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        coverage = max(0.0, min(1.0, self._progress))
        if self._phase == "hold":
            painter.fillRect(self.rect(), QtGui.QColor(0, 0, 0))
        elif self._effect == "Flash":
            self._paint_flash(painter, coverage)
        elif self._effect == "Push Left":
            self._paint_push(painter, coverage, left=True)
        elif self._effect == "Push Right":
            self._paint_push(painter, coverage, left=False)
        elif self._effect == "Zoom Blur":
            self._paint_zoom(painter, coverage)
        elif self._effect == "RGB Glitch":
            self._paint_glitch(painter, coverage)
        elif self._effect == "Film Burn":
            self._paint_film_burn(painter, coverage)
        elif self._effect == "Pixel Dissolve":
            self._paint_pixels(painter, coverage)
        else:
            painter.fillRect(
                self.rect(), QtGui.QColor(0, 0, 0, int(255 * coverage))
            )
        painter.end()

    def _paint_flash(self, painter: QtGui.QPainter, coverage: float) -> None:
        # Brightness rises late and clears quickly; the readiness hold itself
        # is neutral black so a slow decoder never leaves a white screen up.
        alpha = int(255 * min(1.0, coverage ** 1.8))
        painter.fillRect(self.rect(), QtGui.QColor(245, 250, 255, alpha))

    def _paint_push(
        self, painter: QtGui.QPainter, coverage: float, *, left: bool,
    ) -> None:
        width = self.width()
        painter.fillRect(self.rect(), QtGui.QColor(0, 0, 0, int(120 * coverage)))
        covered = int(width * coverage)
        x = width - covered if left else 0
        rect = QtCore.QRect(x, 0, covered, self.height())
        gradient = QtGui.QLinearGradient(
            QtCore.QPointF(rect.topLeft()), QtCore.QPointF(rect.topRight())
        )
        if left:
            gradient.setColorAt(0.0, QtGui.QColor(0, 0, 0, 220))
            gradient.setColorAt(1.0, QtGui.QColor(0, 0, 0, 255))
        else:
            gradient.setColorAt(0.0, QtGui.QColor(0, 0, 0, 255))
            gradient.setColorAt(1.0, QtGui.QColor(0, 0, 0, 220))
        painter.fillRect(rect, gradient)
        edge_x = x if left else x + covered
        painter.fillRect(edge_x - 5, 0, 10, self.height(), QtGui.QColor(80, 210, 255, 90))

    def _paint_zoom(self, painter: QtGui.QPainter, coverage: float) -> None:
        painter.fillRect(self.rect(), QtGui.QColor(0, 0, 0, int(210 * coverage)))
        centre = QtCore.QPointF(self.rect().center())
        radius = max(self.width(), self.height()) * (0.15 + coverage)
        gradient = QtGui.QRadialGradient(centre, radius)
        gradient.setColorAt(0.0, QtGui.QColor(180, 225, 255, int(55 * coverage)))
        gradient.setColorAt(max(0.05, 0.7 - 0.4 * coverage), QtGui.QColor(20, 30, 55, int(130 * coverage)))
        gradient.setColorAt(1.0, QtGui.QColor(0, 0, 0, int(255 * coverage)))
        painter.fillRect(self.rect(), gradient)
        rays = 10
        painter.setPen(QtGui.QPen(QtGui.QColor(120, 210, 255, int(45 * coverage)), 3))
        for i in range(rays):
            angle = 2.0 * math.pi * i / rays
            end = QtCore.QPointF(
                centre.x() + math.cos(angle) * radius,
                centre.y() + math.sin(angle) * radius,
            )
            painter.drawLine(centre, end)

    def _paint_glitch(self, painter: QtGui.QPainter, coverage: float) -> None:
        painter.fillRect(self.rect(), QtGui.QColor(0, 0, 0, int(235 * coverage)))
        rng = random.Random(self._seed + int(coverage * 40))
        colours = (
            QtGui.QColor(255, 25, 80, 150),
            QtGui.QColor(0, 230, 255, 150),
            QtGui.QColor(80, 255, 80, 110),
        )
        for i in range(18):
            h = rng.randint(2, max(3, self.height() // 24))
            y = rng.randrange(max(1, self.height()))
            x = rng.randint(-20, max(0, self.width() - 20))
            w = rng.randint(max(10, self.width() // 10), max(11, self.width() // 2))
            offset = rng.randint(-18, 18)
            painter.fillRect(x + offset, y, w, h, colours[i % len(colours)])
        painter.setPen(QtGui.QPen(QtGui.QColor(120, 220, 255, int(80 * coverage)), 1))
        for y in range(0, self.height(), 6):
            painter.drawLine(0, y, self.width(), y)

    def _paint_film_burn(self, painter: QtGui.QPainter, coverage: float) -> None:
        painter.fillRect(self.rect(), QtGui.QColor(30, 5, 0, int(180 * coverage)))
        centre = QtCore.QPointF(
            self.width() * (0.15 + 0.7 * coverage),
            self.height() * (0.75 - 0.45 * coverage),
        )
        radius = max(self.width(), self.height()) * (0.25 + 0.9 * coverage)
        gradient = QtGui.QRadialGradient(centre, radius)
        gradient.setColorAt(0.0, QtGui.QColor(255, 250, 190, int(255 * coverage)))
        gradient.setColorAt(0.28, QtGui.QColor(255, 155, 35, int(245 * coverage)))
        gradient.setColorAt(0.65, QtGui.QColor(220, 35, 5, int(185 * coverage)))
        gradient.setColorAt(1.0, QtGui.QColor(15, 0, 0, int(250 * coverage)))
        painter.fillRect(self.rect(), gradient)

    def _paint_pixels(self, painter: QtGui.QPainter, coverage: float) -> None:
        cols, rows = 16, 9
        cell_w = max(1, math.ceil(self.width() / cols))
        cell_h = max(1, math.ceil(self.height() / rows))
        count = min(len(self._pixel_order), math.ceil(coverage * len(self._pixel_order)))
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        for order_index in range(count):
            cell = self._pixel_order[order_index]
            x = (cell % cols) * cell_w
            y = (cell // cols) * cell_h
            shade = 5 + (cell * 17) % 24
            painter.fillRect(x, y, cell_w + 1, cell_h + 1, QtGui.QColor(shade, shade, shade))
