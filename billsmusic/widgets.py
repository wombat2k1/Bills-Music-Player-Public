"""Custom Qt widgets: marquee label, auto-scroll text, visualiser, shimmer frame."""
import time
import math
import random

from PyQt6 import QtCore, QtGui, QtWidgets
from .performance_diagnostics import get_diagnostics


class MarqueeLabel(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self._offset = 0.0
        self._direction = 1
        self._speed = 0.8
        self._edge_pause_ticks = 0
        self._shimmer_pos = 0.0
        self._shimmer_speed = 1.0
        self._rainbow_shimmer = False
        self._text_color = QtGui.QColor("#e9eef2")
        self._shutting_down = False
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(20)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumHeight(28)

    def shutdown(self):
        """Real-device lifetime bug (2026-08-31 Codex audit): a queue-row
        MarqueeLabel's own 20ms timer could still dispatch a queued
        _tick()/paintEvent() while the row widget wrapping it was in the
        process of being torn down (removeItemWidget()+deleteLater(), or a
        full Up Next rebuild discarding the old widgets outright) --
        observed as AttributeError on an already-initialized instance
        attribute (_rainbow_shimmer, previously _text), not the
        RuntimeError a fully-deleted C++ object would raise. Not a
        constructor-ordering issue -- every affected attribute is already
        set before the timer ever starts above; this is Qt/PyQt object
        lifetime, not initialization order. Callers must invoke this
        *before* handing the row widget (or this label directly) to
        deleteLater()/an implicit Qt-side deferred delete -- stops and
        disconnects the timer so no further tick can ever be queued, and
        sets a guard flag so any tick/paint already queued at the moment
        of the call is a safe no-op instead of touching state on an object
        mid-teardown. Idempotent -- safe to call more than once."""
        if self._shutting_down:
            return
        self._shutting_down = True
        try:
            self._timer.stop()
        except Exception:
            pass
        try:
            self._timer.timeout.disconnect(self._tick)
        except Exception:
            pass

    def setText(self, text: str):
        if self._shutting_down:
            return
        self._text = text
        self._offset = 0.0
        self._direction = 1
        self._edge_pause_ticks = 30
        self.update()

    def setShimmerSpeed(self, speed: float):
        if self._shutting_down:
            return
        self._shimmer_speed = speed

    def setRainbowShimmer(self, enabled: bool):
        if self._shutting_down:
            return
        self._rainbow_shimmer = bool(enabled)
        self.update()

    def setTextColor(self, color: str):
        if self._shutting_down:
            return
        self._text_color = QtGui.QColor(color)
        self.update()

    def _tick(self):
        if self._shutting_down:
            return
        if self._rainbow_shimmer:
            self._shimmer_pos = (self._shimmer_pos + 0.012 * self._shimmer_speed) % 1.0
        if not self._text:
            return
        fm = self.fontMetrics()
        text_width = fm.horizontalAdvance(self._text)
        max_offset = max(0, text_width - self.width())
        if max_offset <= 0:
            self._offset = 0.0
            self._direction = 1
            self.update()
            return
        if self._edge_pause_ticks > 0:
            self._edge_pause_ticks -= 1
            self.update()
            return
        self._offset += self._speed * self._direction
        if self._offset >= max_offset:
            self._offset = float(max_offset)
            self._direction = -1
            self._edge_pause_ticks = 24
        elif self._offset <= 0:
            self._offset = 0.0
            self._direction = 1
            self._edge_pause_ticks = 30
        self.update()

    def paintEvent(self, event):
        if self._shutting_down:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        if not self._text:
            painter.end()
            return
        fm = painter.fontMetrics()
        text_width = fm.horizontalAdvance(self._text)
        y = (rect.height() + fm.ascent() - fm.descent()) // 2
        painter.setClipRect(rect)
        if self._rainbow_shimmer:
            shift = self._shimmer_pos * max(1, rect.width())
            grad = QtGui.QLinearGradient(-shift, 0, max(1, rect.width()) - shift, 0)
            for i, hue in enumerate((315, 275, 205, 175, 55, 25, 315)):
                col = QtGui.QColor()
                col.setHsvF((hue % 360) / 360.0, 0.78, 1.0)
                grad.setColorAt(i / 6.0, col)
            painter.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 70), 3.0))
            painter.drawText(int(-self._offset), y, self._text)
            painter.setPen(QtGui.QPen(QtGui.QBrush(grad), 1.4))
        else:
            painter.setPen(self._text_color)
        painter.drawText(int(-self._offset), y, self._text)
        painter.end()


class AutoScrollText(QtWidgets.QTextEdit):
    """Text edit that auto-scrolls its content up and down when idle.

    The scrollbar is hidden, and a timer adjusts the vertical position. Scrolling
    pauses when the mouse enters the widget and resumes when it leaves."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll_dir = 1
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(150)
        self._paused = False

    def enterEvent(self, event):
        self._paused = True
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._paused = False
        super().leaveEvent(event)

    def _tick(self):
        if self._paused:
            return
        sb = self.verticalScrollBar()
        if sb.maximum() == 0:
            return
        val = sb.value()
        maxv = sb.maximum()
        if maxv == 0:
            return
        val += self._scroll_dir
        if val <= 0 or val >= maxv:
            self._scroll_dir *= -1
            val = max(0, min(val, maxv))
        sb.setValue(val)

class BeatWidget(QtWidgets.QWidget):
    """Neon synthwave spectrum visualiser with glow, bloom and reflections.

    Public API (unchanged): setLevels, setPlaying, setPaused, set_visual_mode,
    visual_mode, _visual_modes, visual_mode_changed, latency_changed,
    contextMenuEvent, update. New: set_analyzing(bool).
    """
    visual_mode_changed = QtCore.pyqtSignal(str)
    latency_changed = QtCore.pyqtSignal(int)
    fullscreen_toggle_requested = QtCore.pyqtSignal()
    fullscreen_exit_requested = QtCore.pyqtSignal()

    def __init__(self, parent=None, diagnostic_consumer: str = "main"):
        super().__init__(parent)
        self._performance_diagnostics = get_diagnostics()
        self._diagnostic_consumer = diagnostic_consumer
        self._diagnostic_last_paint = None
        self._levels = [0.0] * 32
        self._target = [0.0] * 32
        self._peaks = [0.0] * 32
        self._stereo = False
        self._lr_levels = (0.0, 0.0)
        self._lr_target = (0.0, 0.0)

        # time-constant smoothing (seconds): fast attack, slower release reads
        # as "punchy but not jittery".
        self._lr_attack_tau = 0.030
        self._lr_release_tau = 0.12
        self._attack_tau = 0.025
        self._release_tau = 0.14
        self._peak_release_rate = 1.8   # peak caps fall slowly (per second)

        self._external_levels = None
        self._peak_floor = 0.0
        self._playing = False
        self._paused = False
        self._analyzing = False
        self._last_time = time.time()

        # animated phase used for the background sweep and idle shimmer
        self._phase = 0.0

        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)

        self.setMinimumHeight(120)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setToolTip("Double-click to enter or leave full screen")

        self.visual_mode = "neon"
        self._visual_modes = [
            "neon", "bars", "city", "skyline", "dancefloor", "drive",
            "cassette", "orbit", "ring", "tunnel", "ribbon", "vinyl",
            "waveform", "dots", "mirror",
        ]
        self._visual_mode_labels = {
            "neon": "Neon bars",
            "bars": "Clean bars",
            "city": "Equaliser city",
            "skyline": "Neon Skyline",
            "dancefloor": "Dancefloor Lights",
            "drive": "Neon Drive",
            "cassette": "Cassette meters",
            "orbit": "Disco orbit",
            "ring": "Rainbow Spectrum Ring",
            "tunnel": "Spectrum tunnel",
            "ribbon": "Neon waveform ribbon",
            "vinyl": "Vinyl groove",
            "waveform": "Classic waveform",
            "dots": "Orbit dots",
            "mirror": "Mirror bars",
        }

    def mouseDoubleClickEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.fullscreen_toggle_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Escape:
            self.fullscreen_exit_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    # -- public API ----------------------------------------------------------
    def setPlaying(self, playing: bool):
        self._playing = playing
        if not playing:
            self._external_levels = None
            self._target = [0.0] * len(self._target)
            self._peaks = [0.0] * len(self._peaks)
            self._lr_target = (0.0, 0.0)

    def setPaused(self, paused: bool):
        self._paused = paused

    def set_analyzing(self, analyzing: bool):
        self._analyzing = bool(analyzing)

    def set_visual_mode(self, mode: str):
        if mode in self._visual_modes:
            self.visual_mode = mode
            self.update()
            try:
                self.visual_mode_changed.emit(mode)
            except Exception:
                pass

    # -- visibility-aware suspend/resume --------------------------------------
    # Suspending stops only this widget's own animation/paint timer; the
    # last-rendered frame stays on screen. setLevels() keeps working while
    # suspended (a cheap store, no repaint), so whatever data arrives during
    # suspension is already current the moment rendering resumes.
    @property
    def is_suspended(self) -> bool:
        return not self._timer.isActive()

    def suspend(self):
        if self._timer.isActive():
            self._timer.stop()

    def resume(self):
        if self._timer.isActive():
            return
        # Avoids a single oversized smoothing step from a stale _last_time
        # (the elapsed-time clamp in _tick already bounds this safely, but
        # resetting it means the first resumed frame is a normal-sized step
        # rather than the full clamped catch-up).
        self._last_time = time.time()
        self._timer.start()
        self._tick()

    def setLevels(self, levels):
        if not levels:
            self._external_levels = None
            return
        if len(levels) != len(self._levels):
            self._levels = [0.0] * len(levels)
            self._target = [0.0] * len(levels)
            self._peaks = [0.0] * len(levels)
        self._stereo = len(levels) % 2 == 0 and len(levels) >= 8
        self._external_levels = [max(0.0, min(1.0, float(x))) for x in levels]
        if self._stereo:
            half = len(levels) // 2
            left = sum(self._external_levels[:half]) / float(half)
            right = sum(self._external_levels[half:]) / float(half)
            self._lr_target = (left, right)
        else:
            mono = sum(self._external_levels) / float(len(self._external_levels))
            self._lr_target = (mono, mono)

    # -- context menu (modes + latency) --------------------------------------
    def contextMenuEvent(self, event):
        menu = QtWidgets.QMenu(self)
        group = []
        for m in self._visual_modes:
            action = menu.addAction(self._visual_mode_labels.get(m, m.capitalize()))
            action.setCheckable(True)
            action.setChecked(m == self.visual_mode)
            group.append((action, m))
        menu.addSeparator()
        lat_menu = menu.addMenu("Latency Compensation")
        lat_options = ["-200 ms", "-100 ms", "-50 ms", "0 ms", "+50 ms", "+100 ms"]
        lat_actions = []
        for lo in lat_options:
            a = lat_menu.addAction(lo)
            lat_actions.append((a, lo))
        chosen = menu.exec(event.globalPos())
        if chosen:
            for act, m in group:
                if act == chosen:
                    self.set_visual_mode(m)
                    return
            for act, lo in lat_actions:
                if act == chosen:
                    try:
                        ms = int(lo.replace(" ms", ""))
                    except Exception:
                        ms = 0
                    try:
                        self.latency_changed.emit(ms)
                    except Exception:
                        pass
                    return

    # -- animation tick ------------------------------------------------------
    def _tick(self):
        if self._paused:
            return
        now = time.time()
        dt = max(1e-4, min(0.1, now - self._last_time))
        self._last_time = now
        self._phase += dt

        # LR smoothing
        l_cur, r_cur = self._lr_levels
        l_t, r_t = self._lr_target
        l_alpha = 1.0 - math.exp(-dt / (self._lr_attack_tau if l_t > l_cur else self._lr_release_tau))
        r_alpha = 1.0 - math.exp(-dt / (self._lr_attack_tau if r_t > r_cur else self._lr_release_tau))
        self._lr_levels = (l_cur + (l_t - l_cur) * l_alpha,
                           r_cur + (r_t - r_cur) * r_alpha)

        for i in range(len(self._levels)):
            if self._external_levels is not None and i < len(self._external_levels):
                tgt = self._external_levels[i]
            elif self._playing:
                # idle: gentle travelling sine so the bars "breathe"
                base = 0.10 + 0.06 * math.sin(self._phase * 2.2 + i * 0.5)
                tgt = max(0.0, base)
            else:
                tgt = 0.0
            self._target[i] = tgt
            tau = self._attack_tau if tgt > self._levels[i] else self._release_tau
            alpha = 1.0 - math.exp(-dt / tau)
            self._levels[i] += (tgt - self._levels[i]) * alpha

            if self._levels[i] >= self._peaks[i]:
                self._peaks[i] = self._levels[i]
            else:
                self._peaks[i] = max(0.0, self._peaks[i] - self._peak_release_rate * dt)

        self.update()

    # -- colour --------------------------------------------------------------
    def _band_color(self, frac_x: float, level: float) -> QtGui.QColor:
        """Synthwave sweep: magenta (low) -> violet -> cyan (high), brighter
        with level."""
        # hue 300 (magenta) down to 180 (cyan) across the spectrum
        hue = 300.0 - 120.0 * max(0.0, min(1.0, frac_x))
        # shift slightly with intensity for a hot core
        hue -= 20.0 * level
        sat = 0.85
        val = 0.45 + 0.55 * max(0.0, min(1.0, level))
        c = QtGui.QColor()
        c.setHsvF((hue % 360) / 360.0, sat, val)
        return c

    # -- painting ------------------------------------------------------------
    def paintEvent(self, event):
        diagnostic_started = time.perf_counter()
        previous_paint = self._diagnostic_last_paint
        self._diagnostic_last_paint = diagnostic_started
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        w, h = rect.width(), rect.height()

        self._paint_background(p, rect)

        n = len(self._levels)
        if n == 0:
            p.end()
            self._record_visual_performance(
                diagnostic_started, previous_paint
            )
            return

        pad = 14
        inner_w = max(1, w - pad * 2)
        baseline = h - 26                  # leave room for reflection + labels
        usable_h = max(10, baseline - 14)

        mode = self.visual_mode
        if mode == "waveform":
            self._paint_waveform(p, pad, inner_w, baseline, usable_h, n)
        elif mode == "ribbon":
            self._paint_ribbon(p, pad, inner_w, baseline, usable_h, n)
        elif mode == "vinyl":
            self._paint_vinyl(p, rect, n)
        elif mode == "city":
            self._paint_city(p, pad, inner_w, baseline, usable_h, n)
        elif mode == "skyline":
            self._paint_skyline(p, rect, n)
        elif mode == "dancefloor":
            self._paint_dancefloor(p, rect, n)
        elif mode == "drive":
            self._paint_drive(p, rect, n)
        elif mode == "cassette":
            self._paint_cassette(p, rect)
        elif mode == "orbit":
            self._paint_orbit(p, rect, n)
        elif mode == "ring":
            self._paint_spectrum_ring(p, rect, n)
        elif mode == "tunnel":
            self._paint_tunnel(p, rect, n)
        elif mode == "dots":
            self._paint_dots(p, pad, inner_w, baseline, usable_h, n)
        else:
            # neon / bars / mirror all share the glowing-bar core
            reflect = mode in ("neon", "mirror")
            glow = mode in ("neon", "mirror")
            self._paint_bars(p, pad, inner_w, baseline, usable_h, n, glow, reflect)

        # subtle "analysing…" hint
        if self._analyzing:
            p.setPen(QtGui.QColor(180, 200, 230, 160))
            f = self.font(); f.setPointSizeF(8.5); p.setFont(f)
            p.drawText(rect.adjusted(0, 6, -10, 0),
                       QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignTop,
                       "analysing…")
        p.end()
        self._record_visual_performance(diagnostic_started, previous_paint)

    def _record_visual_performance(self, started, previous):
        paint_ms = (time.perf_counter() - started) * 1000.0
        gap_ms = (
            (started - previous) * 1000.0
            if previous is not None else 0.0
        )
        self._performance_diagnostics.observe_visual_frame(
            paint_ms,
            gap_ms,
            visible=self.isVisible() and not self._paused,
            target_interval_ms=16.0,
            consumer=self._diagnostic_consumer,
        )

    def _paint_background(self, p, rect):
        w, h = rect.width(), rect.height()
        # vertical deep-space gradient
        g = QtGui.QLinearGradient(0, 0, 0, h)
        g.setColorAt(0.0, QtGui.QColor(18, 10, 34))
        g.setColorAt(0.55, QtGui.QColor(12, 8, 24))
        g.setColorAt(1.0, QtGui.QColor(6, 4, 14))
        p.fillRect(rect, g)

        # slow moving horizon glow that pulses with overall loudness
        loud = 0.5 * (self._lr_levels[0] + self._lr_levels[1])
        cx = w * (0.5 + 0.18 * math.sin(self._phase * 0.5))
        cy = h * 0.82
        radius = max(80.0, w * (0.35 + 0.25 * loud))
        rg = QtGui.QRadialGradient(cx, cy, radius)
        a = int(60 + 120 * loud)
        rg.setColorAt(0.0, QtGui.QColor(255, 60, 180, a))
        rg.setColorAt(0.5, QtGui.QColor(120, 40, 200, int(a * 0.5)))
        rg.setColorAt(1.0, QtGui.QColor(0, 0, 0, 0))
        p.fillRect(rect, rg)

        # faint perspective grid lines (jukebox/retro vibe)
        p.setPen(QtGui.QPen(QtGui.QColor(120, 80, 200, 40), 1))
        horizon = h * 0.78
        for i in range(1, 6):
            yy = horizon + (h - horizon) * (i / 6.0) ** 1.6
            p.drawLine(0, int(yy), w, int(yy))

    def _bar_geometry(self, pad, inner_w, n):
        gap = max(2.0, inner_w / n * 0.22)
        bar_w = max(2.0, (inner_w - gap * (n - 1)) / n)
        return bar_w, gap

    def _paint_bars(self, p, pad, inner_w, baseline, usable_h, n, glow, reflect):
        bar_w, gap = self._bar_geometry(pad, inner_w, n)
        x = float(pad)
        for i in range(n):
            lvl = max(0.0, min(1.0, self._levels[i]))
            frac_x = i / float(max(1, n - 1))
            bh = lvl * usable_h
            top = baseline - bh
            col = self._band_color(frac_x, lvl)

            if glow and bh > 2:
                # bloom: a soft wide translucent rect behind the bar
                glow_col = QtGui.QColor(col)
                glow_col.setAlpha(70)
                gw = bar_w * 2.2
                p.fillRect(QtCore.QRectF(x - (gw - bar_w) / 2, top - 6, gw, bh + 12), glow_col)

            # the bar itself: vertical gradient, hot top
            grad = QtGui.QLinearGradient(0, top, 0, baseline)
            hot = QtGui.QColor(col); hot.setHsvF(col.hueF(), 0.35, 1.0)
            grad.setColorAt(0.0, hot)
            grad.setColorAt(0.35, col)
            base_dark = QtGui.QColor(col); base_dark.setHsvF(col.hueF(), 0.95, 0.35)
            grad.setColorAt(1.0, base_dark)
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QBrush(grad))
            p.drawRoundedRect(QtCore.QRectF(x, top, bar_w, bh), bar_w * 0.35, bar_w * 0.35)

            # peak cap
            pk = max(0.0, min(1.0, self._peaks[i]))
            if pk > 0.02:
                py = baseline - pk * usable_h
                cap = QtGui.QColor(255, 255, 255, 200)
                p.fillRect(QtCore.QRectF(x, py - 2.5, bar_w, 2.5), cap)

            # reflection
            if reflect and bh > 2:
                refl = QtGui.QLinearGradient(0, baseline, 0, baseline + bh * 0.55)
                c0 = QtGui.QColor(col); c0.setAlpha(90)
                c1 = QtGui.QColor(col); c1.setAlpha(0)
                refl.setColorAt(0.0, c0)
                refl.setColorAt(1.0, c1)
                p.setBrush(QtGui.QBrush(refl))
                p.drawRoundedRect(QtCore.QRectF(x, baseline + 2, bar_w, bh * 0.55),
                                  bar_w * 0.35, bar_w * 0.35)
            x += bar_w + gap

    def _paint_waveform(self, p, pad, inner_w, baseline, usable_h, n):
        mid = baseline - usable_h / 2
        pts_top, pts_bot = [], []
        for i in range(n):
            lvl = max(0.0, min(1.0, self._levels[i]))
            xx = pad + inner_w * (i / float(max(1, n - 1)))
            dy = lvl * usable_h / 2
            pts_top.append(QtCore.QPointF(xx, mid - dy))
            pts_bot.append(QtCore.QPointF(xx, mid + dy))
        poly = QtGui.QPolygonF(pts_top + list(reversed(pts_bot)))
        grad = QtGui.QLinearGradient(pad, 0, pad + inner_w, 0)
        grad.setColorAt(0.0, QtGui.QColor(255, 60, 180, 180))
        grad.setColorAt(1.0, QtGui.QColor(40, 220, 255, 180))
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QBrush(grad))
        p.drawPolygon(poly)
        p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 160), 1.5))
        p.drawPolyline(QtGui.QPolygonF(pts_top))

    def _paint_ribbon(self, p, pad, inner_w, baseline, usable_h, n):
        mid = baseline - usable_h / 2
        sweep = self._phase * 2.4
        top_path = QtGui.QPainterPath()
        line_path = QtGui.QPainterPath()
        for i in range(n):
            lvl = max(0.0, min(1.0, self._levels[i]))
            frac = i / float(max(1, n - 1))
            xx = pad + inner_w * frac
            flutter = 0.10 * math.sin(sweep + frac * math.tau * 3.0)
            amp = max(0.03, min(1.0, lvl + flutter)) * usable_h * 0.40
            y_top = mid - amp
            y_line = mid - math.sin(sweep + frac * math.tau * 2.0) * amp * 0.28
            if i == 0:
                top_path.moveTo(xx, y_top)
                line_path.moveTo(xx, y_line)
            else:
                top_path.lineTo(xx, y_top)
                line_path.lineTo(xx, y_line)

        ribbon = QtGui.QPainterPath(top_path)
        points = []
        for i in range(n - 1, -1, -1):
            frac = i / float(max(1, n - 1))
            xx = pad + inner_w * frac
            lvl = max(0.0, min(1.0, self._levels[i]))
            flutter = 0.10 * math.sin(sweep + frac * math.tau * 3.0)
            amp = max(0.03, min(1.0, lvl + flutter)) * usable_h * 0.40
            points.append(QtCore.QPointF(xx, mid + amp))
        for pt in points:
            ribbon.lineTo(pt)
        ribbon.closeSubpath()

        grad = QtGui.QLinearGradient(pad, 0, pad + inner_w, 0)
        grad.setColorAt(0.0, QtGui.QColor(255, 45, 190, 120))
        grad.setColorAt(0.45, QtGui.QColor(150, 70, 255, 105))
        grad.setColorAt(1.0, QtGui.QColor(25, 230, 255, 120))
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QBrush(grad))
        p.drawPath(ribbon)

        for width, alpha in ((10.0, 45), (5.0, 90), (2.0, 230)):
            pen = QtGui.QPen(QtGui.QColor(25, 230, 255, alpha), width)
            pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.drawPath(line_path)
        p.setPen(QtGui.QPen(QtGui.QColor(255, 80, 210, 180), 1.3))
        p.drawPath(top_path)

    def _paint_vinyl(self, p, rect, n):
        w, h = rect.width(), rect.height()
        loud = max(0.0, min(1.0, 0.5 * (self._lr_levels[0] + self._lr_levels[1])))
        cx, cy = w * 0.5, h * 0.52
        radius = max(36.0, min(w, h) * 0.34)
        spin = self._phase * 1.8

        glow = QtGui.QRadialGradient(cx, cy, radius * 1.7)
        glow.setColorAt(0.0, QtGui.QColor(255, 50, 190, int(75 + 80 * loud)))
        glow.setColorAt(0.6, QtGui.QColor(20, 220, 255, int(35 + 80 * loud)))
        glow.setColorAt(1.0, QtGui.QColor(0, 0, 0, 0))
        p.fillRect(rect, glow)

        disc = QtCore.QRectF(cx - radius, cy - radius, radius * 2, radius * 2)
        disc_grad = QtGui.QRadialGradient(cx, cy, radius)
        disc_grad.setColorAt(0.0, QtGui.QColor(38, 20, 58))
        disc_grad.setColorAt(0.38, QtGui.QColor(18, 14, 30))
        disc_grad.setColorAt(1.0, QtGui.QColor(5, 4, 12))
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QBrush(disc_grad))
        p.drawEllipse(disc)

        for i in range(7):
            rr = radius * (0.25 + i * 0.105)
            alpha = 50 + i * 12
            p.setPen(QtGui.QPen(QtGui.QColor(120, 80, 180, alpha), 1.0))
            p.drawEllipse(QtCore.QPointF(cx, cy), rr, rr)

        bars = min(n, 48)
        for i in range(bars):
            lvl = max(0.0, min(1.0, self._levels[i % n]))
            frac = i / float(max(1, bars))
            angle = spin + frac * math.tau
            inner = radius * (0.78 + 0.03 * math.sin(self._phase * 3.0 + i))
            outer = radius * (0.93 + 0.34 * lvl)
            x1 = cx + math.cos(angle) * inner
            y1 = cy + math.sin(angle) * inner
            x2 = cx + math.cos(angle) * outer
            y2 = cy + math.sin(angle) * outer
            col = self._band_color(frac, lvl)
            col.setAlpha(120 + int(120 * lvl))
            pen = QtGui.QPen(col, 2.0 + 3.0 * lvl)
            pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawLine(QtCore.QPointF(x1, y1), QtCore.QPointF(x2, y2))

        label_r = radius * 0.24
        label = QtCore.QRectF(cx - label_r, cy - label_r, label_r * 2, label_r * 2)
        label_grad = QtGui.QRadialGradient(cx - label_r * 0.35, cy - label_r * 0.35, label_r * 1.5)
        label_grad.setColorAt(0.0, QtGui.QColor(255, 80, 210))
        label_grad.setColorAt(1.0, QtGui.QColor(20, 220, 255))
        p.setBrush(QtGui.QBrush(label_grad))
        p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 150), 1.2))
        p.drawEllipse(label)
        p.setBrush(QtGui.QColor(5, 4, 12))
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.drawEllipse(QtCore.QPointF(cx, cy), max(3.0, label_r * 0.18), max(3.0, label_r * 0.18))

    def _paint_spectrum_ring(self, p, rect, n):
        w, h = rect.width(), rect.height()
        cx, cy = w * 0.5, h * 0.52
        radius = min(w, h) * 0.25
        outer_room = min(w, h) * 0.18
        loud = max(0.0, min(1.0, 0.5 * (self._lr_levels[0] + self._lr_levels[1])))

        bg = QtGui.QRadialGradient(cx, cy, min(w, h) * 0.62)
        bg.setColorAt(0.0, QtGui.QColor(22, 8, 28, 235))
        bg.setColorAt(0.65, QtGui.QColor(3, 3, 10, 245))
        bg.setColorAt(1.0, QtGui.QColor(0, 0, 0, 255))
        p.fillRect(rect, bg)

        spokes = max(96, min(192, n * 3))
        spin = self._phase * 0.24
        for i in range(spokes):
            frac = i / float(spokes)
            src = int(frac * n) % n
            level = max(0.0, min(1.0, self._levels[src]))
            peak = max(level, self._peaks[src] if src < len(self._peaks) else 0.0)
            angle = frac * math.tau + spin
            pulse = 0.55 + 0.45 * math.sin(self._phase * 4.0 + i * 0.17)
            spike = outer_room * (0.16 + level * 0.88 + peak * 0.34 * pulse)
            inner_r = radius * (0.78 + 0.06 * math.sin(self._phase * 1.6 + i * 0.05))
            outer_r = radius + spike
            col = QtGui.QColor()
            col.setHsvF((frac + self._phase * 0.025) % 1.0, 0.92, 0.65 + 0.35 * level)
            glow = QtGui.QColor(col); glow.setAlpha(45 + int(105 * level))
            hot = QtGui.QColor(col); hot.setAlpha(185 + int(60 * level))
            x1 = cx + math.cos(angle) * inner_r
            y1 = cy + math.sin(angle) * inner_r * 0.48
            x2 = cx + math.cos(angle) * outer_r
            y2 = cy + math.sin(angle) * outer_r * 0.48
            p.setPen(QtGui.QPen(glow, 4.0))
            p.drawLine(QtCore.QPointF(x1, y1), QtCore.QPointF(x2, y2))
            p.setPen(QtGui.QPen(hot, 1.2 + 1.8 * level))
            p.drawLine(QtCore.QPointF(x1, y1), QtCore.QPointF(x2, y2))

        p.setPen(QtGui.QPen(QtGui.QColor(255, 80, 220, 80), 2.0))
        p.setBrush(QtGui.QColor(2, 2, 8, 245))
        p.drawEllipse(QtCore.QPointF(cx, cy), radius * 0.43, radius * 0.21)
        p.setPen(QtGui.QPen(QtGui.QColor(20, 230, 255, 85), 1.4))
        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        p.drawEllipse(QtCore.QPointF(cx, cy), radius * (0.96 + loud * 0.08), radius * (0.46 + loud * 0.04))

    def _paint_city(self, p, pad, inner_w, baseline, usable_h, n):
        bar_w, gap = self._bar_geometry(pad, inner_w, n)
        x = float(pad)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        horizon = baseline + 2
        loud = max(0.0, min(1.0, 0.5 * (self._lr_levels[0] + self._lr_levels[1])))
        glow = QtGui.QLinearGradient(0, horizon - usable_h * 0.45, 0, horizon + 18)
        glow.setColorAt(0.0, QtGui.QColor(255, 40, 185, 0))
        glow.setColorAt(0.8, QtGui.QColor(255, 40, 185, int(35 + 70 * loud)))
        glow.setColorAt(1.0, QtGui.QColor(20, 220, 255, int(30 + 50 * loud)))
        p.fillRect(QtCore.QRectF(0, horizon - usable_h * 0.45, self.width(), usable_h * 0.55), glow)

        for i in range(n):
            lvl = max(0.0, min(1.0, self._levels[i]))
            frac = i / float(max(1, n - 1))
            building_h = max(6.0, lvl * usable_h * 0.94)
            top = baseline - building_h
            col = self._band_color(frac, lvl)
            side = QtGui.QColor(col); side.setAlpha(85)
            core = QtGui.QColor(col); core.setAlpha(210)
            p.setBrush(side)
            p.drawRoundedRect(QtCore.QRectF(x - 1, top - 3, bar_w + 2, building_h + 5), 2.0, 2.0)

            grad = QtGui.QLinearGradient(0, top, 0, baseline)
            hot = QtGui.QColor(col); hot.setHsvF(col.hueF(), 0.45, 1.0)
            base = QtGui.QColor(col); base.setHsvF(col.hueF(), 0.95, 0.28)
            grad.setColorAt(0.0, hot)
            grad.setColorAt(0.55, core)
            grad.setColorAt(1.0, base)
            p.setBrush(QtGui.QBrush(grad))
            p.drawRoundedRect(QtCore.QRectF(x, top, bar_w, building_h), 2.5, 2.5)

            rows = max(1, int(building_h / 12.0))
            window_col = QtGui.QColor(255, 245, 170, 130 + int(80 * lvl))
            p.setBrush(window_col)
            for row in range(rows):
                if (row + i) % 3 == 0:
                    continue
                yy = baseline - 8 - row * 11
                if yy < top + 4:
                    break
                p.drawRect(QtCore.QRectF(x + bar_w * 0.28, yy, max(1.2, bar_w * 0.42), 2.0))
            x += bar_w + gap

        p.setPen(QtGui.QPen(QtGui.QColor(20, 230, 255, 120), 1.2))
        p.drawLine(QtCore.QPointF(pad, horizon), QtCore.QPointF(pad + inner_w, horizon))

    def _paint_skyline(self, p, rect, n):
        w, h = rect.width(), rect.height()
        loud = max(0.0, min(1.0, 0.5 * (self._lr_levels[0] + self._lr_levels[1])))
        horizon = h * 0.62
        floor = h - 4

        bg = QtGui.QLinearGradient(0, 0, 0, h)
        bg.setColorAt(0.0, QtGui.QColor(1, 3, 12))
        bg.setColorAt(0.42, QtGui.QColor(8, 10, 30))
        bg.setColorAt(0.72, QtGui.QColor(16, 7, 20))
        bg.setColorAt(1.0, QtGui.QColor(2, 2, 8))
        p.fillRect(rect, bg)

        pulse = QtGui.QRadialGradient(w * 0.5, horizon, max(w, h) * (0.42 + loud * 0.14))
        pulse.setColorAt(0.0, QtGui.QColor(255, 105, 35, 95 + int(105 * loud)))
        pulse.setColorAt(0.42, QtGui.QColor(255, 35, 95, 55 + int(65 * loud)))
        pulse.setColorAt(1.0, QtGui.QColor(0, 0, 0, 0))
        p.fillRect(rect, pulse)

        for i in range(42):
            seed = math.sin(i * 78.233 + 19.19) * 43758.5453
            frac = seed - math.floor(seed)
            x = (frac * w + self._phase * (18 + (i % 5) * 5)) % w
            y = 8 + ((math.sin(i * 17.17) * 0.5 + 0.5) * horizon * 0.78)
            twinkle = 0.35 + 0.65 * max(0.0, math.sin(self._phase * 2.2 + i))
            alpha = int(40 + 115 * twinkle)
            p.setPen(QtGui.QPen(QtGui.QColor(255, 95, 55, alpha), 1.0))
            p.drawLine(QtCore.QPointF(x, y), QtCore.QPointF(x + 8 + twinkle * 18, y - 2))

        van_x = w * 0.5
        for i in range(18):
            end_frac = i / 17.0
            end_x = end_frac * w
            level = max(0.0, min(1.0, self._levels[int(end_frac * (n - 1))]))
            ray = QtGui.QPainterPath()
            ray.moveTo(van_x, horizon)
            for step in range(1, 22):
                t = step / 21.0
                x = van_x + (end_x - van_x) * t
                y = horizon + (floor - horizon) * (t ** 1.08)
                whip = math.sin(t * math.tau * (1.35 + level * 2.2) + self._phase * 5.0 + i * 0.58)
                detail = math.sin(t * math.tau * 5.5 - self._phase * 3.0 + i) * 0.35
                lift = max(0.0, whip + detail) * (3.0 + 28.0 * t) * (0.25 + level)
                y -= lift
                ray.lineTo(x, y)
            alpha = 55 + int(120 * max(level, loud * 0.55))
            p.setPen(QtGui.QPen(QtGui.QColor(255, 56, 22, max(30, alpha // 2)), 3.4 + 2.2 * level))
            p.drawPath(ray)
            p.setPen(QtGui.QPen(QtGui.QColor(255, 145, 45, alpha), 0.8 + 1.4 * level))
            p.drawPath(ray)

        path = QtGui.QPainterPath()
        for i in range(n):
            x = (i / float(max(1, n - 1))) * w
            level = max(0.0, min(1.0, self._levels[i]))
            y = horizon + 1 - math.sin(i * 0.18 + self._phase * 3.2) * 7 - level * h * 0.13
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        p.setPen(QtGui.QPen(QtGui.QColor(255, 45, 22, 95), 12.0))
        p.drawPath(path)
        p.setPen(QtGui.QPen(QtGui.QColor(255, 125, 36, 180), 6.0))
        p.drawPath(path)
        p.setPen(QtGui.QPen(QtGui.QColor(255, 244, 118, 240), 2.0))
        p.drawPath(path)

        count = min(56, max(32, n + 10))
        for i in range(count):
            frac = i / float(max(1, count - 1))
            level = max(0.0, min(1.0, self._levels[int(frac * (n - 1))]))
            x = frac * w
            bw = max(3.0, w / count * (0.34 + 0.35 * ((i % 5) / 4.0)))
            base_h = h * (0.08 + 0.42 * level)
            skyline_peak = max(0.0, math.sin(frac * math.pi))
            shimmer = h * 0.08 * max(0.0, math.sin(self._phase * 5.0 + i * 1.7))
            bh = base_h + shimmer
            top = horizon - bh * (0.58 + 0.72 * skyline_peak)
            top = max(8.0, min(horizon - 8, top))
            if i % 9 == 0:
                col = QtGui.QColor(255, 152, 92)
            elif frac < 0.42:
                col = QtGui.QColor(42, 100 + int(130 * level), 255)
            elif frac < 0.72:
                col = QtGui.QColor(88, 245, 255)
            else:
                col = QtGui.QColor(210, 80, 245)
            glow = QtGui.QColor(col); glow.setAlpha(32 + int(58 * level))
            shell = QtCore.QRectF(x - bw * 0.62, top - 2, bw * 1.24, horizon - top + 4)
            core = QtCore.QRectF(x - bw * 0.40, top + 3, bw * 0.68, horizon - top - 2)
            side = QtCore.QRectF(core.right(), top + 5, max(1.0, bw * 0.22), horizon - top - 4)
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(glow)
            p.drawRoundedRect(shell.adjusted(-2, -4, 2, 5), 2, 2)

            shell_grad = QtGui.QLinearGradient(shell.left(), shell.top(), shell.right(), shell.bottom())
            shell_grad.setColorAt(0.0, QtGui.QColor(max(0, col.red() // 3), max(0, col.green() // 3), max(0, col.blue() // 3), 225))
            shell_grad.setColorAt(1.0, QtGui.QColor(4, 6, 18, 245))
            p.setBrush(QtGui.QBrush(shell_grad))
            p.drawRoundedRect(shell, 2, 2)

            body = QtGui.QLinearGradient(core.left(), core.top(), core.right(), core.bottom())
            hot = QtGui.QColor(col); hot.setAlpha(230)
            body.setColorAt(0.0, hot)
            body.setColorAt(0.55, QtGui.QColor(col.red(), col.green(), col.blue(), 130))
            body.setColorAt(1.0, QtGui.QColor(7, 8, 24, 230))
            p.setBrush(QtGui.QBrush(body))
            p.drawRoundedRect(core, 2, 2)

            side_col = QtGui.QColor(max(0, col.red() - 35), max(0, col.green() - 45), max(0, col.blue() - 55), 170)
            p.setBrush(side_col)
            p.drawRoundedRect(side, 1, 1)
            p.setPen(QtGui.QPen(QtGui.QColor(8, 12, 30, 190), 1.0))
            p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(shell, 2, 2)

            if shell.height() > 28 and bw > 4.2:
                p.setPen(QtGui.QPen(QtGui.QColor(230, 255, 255, 75 + int(75 * level)), 0.8))
                rows = min(5, max(1, int(shell.height() / 22.0)))
                for row in range(rows):
                    if (row + i) % 3 == 1:
                        continue
                    yy = horizon - 8 - row * 14
                    if yy <= top + 8:
                        break
                    p.drawLine(QtCore.QPointF(core.left() + core.width() * 0.22, yy),
                               QtCore.QPointF(core.right() - core.width() * 0.16, yy))

    def _paint_dancefloor(self, p, rect, n):
        w, h = rect.width(), rect.height()
        loud_l, loud_r = self._lr_levels
        loud = max(0.0, min(1.0, 0.5 * (loud_l + loud_r)))
        horizon = h * 0.48
        floor = h - 4
        cx = w * 0.5

        bg = QtGui.QLinearGradient(0, 0, 0, h)
        bg.setColorAt(0.0, QtGui.QColor(3, 2, 10))
        bg.setColorAt(0.42, QtGui.QColor(12, 5, 24))
        bg.setColorAt(1.0, QtGui.QColor(4, 3, 12))
        p.fillRect(rect, bg)

        haze = QtGui.QRadialGradient(cx, horizon, max(w, h) * (0.42 + 0.18 * loud))
        haze.setColorAt(0.0, QtGui.QColor(255, 60, 190, 42 + int(70 * loud)))
        haze.setColorAt(0.45, QtGui.QColor(25, 210, 255, 24 + int(42 * loud)))
        haze.setColorAt(1.0, QtGui.QColor(0, 0, 0, 0))
        p.fillRect(rect, haze)

        def floor_point(frac_x, depth):
            y = horizon + (floor - horizon) * (depth ** 1.55)
            half = w * (0.08 + 0.58 * (depth ** 1.18))
            return QtCore.QPointF(cx + (frac_x - 0.5) * 2.0 * half, y)

        # Perspective dancefloor tiles pulse with the music but stay cheap to draw.
        rows = 7
        cols = 9
        for row in range(rows):
            d1 = row / float(rows)
            d2 = (row + 1) / float(rows)
            for col_i in range(cols):
                f1 = col_i / float(cols)
                f2 = (col_i + 1) / float(cols)
                src = int(((col_i + row * 2) / float(cols + rows * 2)) * (n - 1))
                level = max(0.0, min(1.0, self._levels[src]))
                twinkle = max(0.0, math.sin(self._phase * 4.5 + row * 0.8 + col_i * 0.6))
                alpha = 18 + int(80 * level + 34 * twinkle)
                tile = QtGui.QPolygonF([
                    floor_point(f1, d1), floor_point(f2, d1),
                    floor_point(f2, d2), floor_point(f1, d2),
                ])
                frac = (col_i / float(max(1, cols - 1)) + row * 0.07 + self._phase * 0.02) % 1.0
                col = self._band_color(frac, max(level, loud * 0.55))
                col.setAlpha(alpha)
                p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 22 + int(35 * d2)), 0.7))
                p.setBrush(col)
                p.drawPolygon(tile)

        p.setPen(QtGui.QPen(QtGui.QColor(255, 80, 220, 95), 1.1))
        for i in range(8):
            f = i / 7.0
            p.drawLine(floor_point(f, 0.0), floor_point(f, 1.0))
        for row in range(rows + 1):
            d = row / float(rows)
            p.drawLine(floor_point(0.0, d), floor_point(1.0, d))

        fixtures = [
            (0.10, QtGui.QColor(255, 55, 185), -1.0),
            (0.28, QtGui.QColor(45, 220, 255), -0.45),
            (0.50, QtGui.QColor(255, 210, 70), 0.0),
            (0.72, QtGui.QColor(120, 80, 255), 0.45),
            (0.90, QtGui.QColor(255, 92, 35), 1.0),
        ]
        for i, (fx, colour, bias) in enumerate(fixtures):
            source = QtCore.QPointF(w * fx, h * 0.12)
            level = max(0.0, min(1.0, self._levels[int((i / float(len(fixtures) - 1)) * (n - 1))]))
            stereo_push = (loud_r - loud_l) * 0.18
            sweep = math.sin(self._phase * (0.85 + i * 0.08) + i * 1.35) * 0.28
            target_frac = max(0.05, min(0.95, fx + bias * 0.10 + sweep + stereo_push))
            target = floor_point(target_frac, 0.86)
            width = w * (0.08 + 0.09 * max(level, loud))
            beam = QtGui.QPolygonF([
                source,
                QtCore.QPointF(target.x() - width, target.y()),
                QtCore.QPointF(target.x() + width, target.y()),
            ])
            glow = QtGui.QColor(colour); glow.setAlpha(30 + int(70 * max(level, loud)))
            core = QtGui.QColor(colour); core.setAlpha(50 + int(95 * level))
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(glow)
            p.drawPolygon(beam)
            narrow = QtGui.QPolygonF([
                source,
                QtCore.QPointF(target.x() - width * 0.34, target.y()),
                QtCore.QPointF(target.x() + width * 0.34, target.y()),
            ])
            p.setBrush(core)
            p.drawPolygon(narrow)
            p.setBrush(QtGui.QColor(16, 14, 28, 235))
            p.setPen(QtGui.QPen(colour, 1.1))
            p.drawRoundedRect(QtCore.QRectF(source.x() - 9, source.y() - 5, 18, 10), 3, 3)

        # Small mirror-ball sparkle field over the haze.
        for i in range(32):
            seed = math.sin(i * 42.31 + 3.17) * 43758.5453
            frac = seed - math.floor(seed)
            x = (frac * w + math.sin(self._phase * 0.45 + i) * 10) % w
            y = h * (0.15 + 0.44 * ((math.sin(i * 13.19) * 0.5) + 0.5))
            sparkle = max(0.0, math.sin(self._phase * 3.2 + i * 1.7))
            if sparkle <= 0.08:
                continue
            p.setPen(QtGui.QPen(QtGui.QColor(230, 250, 255, int(40 + 120 * sparkle)), 1.0))
            p.drawPoint(QtCore.QPointF(x, y))

    def _paint_drive(self, p, rect, n):
        w, h = rect.width(), rect.height()
        loud_l, loud_r = self._lr_levels
        loud = max(0.0, min(1.0, 0.5 * (loud_l + loud_r)))
        # Keep Neon Drive mostly straight, then add short pseudo-random turn bursts.
        turn_period = 7.5
        turn_slot = int(self._phase / turn_period)
        turn_progress = (self._phase / turn_period) - turn_slot
        turn_seed = math.sin((turn_slot + 1) * 12.9898) * 43758.5453
        turn_value = (turn_seed - math.floor(turn_seed)) * 2.0 - 1.0
        turn_window = max(0.0, 1.0 - abs(turn_progress - 0.58) / 0.18)
        turn_ease = turn_window * turn_window * (3.0 - 2.0 * turn_window)
        steer = max(-1.0, min(1.0, (1.0 if turn_value >= 0 else -1.0) * turn_ease))
        horizon_y = h * 0.34
        road_bottom_y = h + 8
        horizon_x = w * (0.5 + 0.18 * steer)
        bottom_center_x = w * (0.5 - 0.20 * steer)

        sky = QtGui.QLinearGradient(0, 0, 0, h)
        sky.setColorAt(0.0, QtGui.QColor(8, 5, 22))
        sky.setColorAt(0.45, QtGui.QColor(20, 9, 36))
        sky.setColorAt(1.0, QtGui.QColor(4, 4, 12))
        p.fillRect(rect, sky)

        pulse = QtGui.QRadialGradient(horizon_x, horizon_y, max(w, h) * (0.35 + 0.18 * loud))
        pulse.setColorAt(0.0, QtGui.QColor(255, 60, 190, int(70 + 110 * loud)))
        pulse.setColorAt(0.45, QtGui.QColor(20, 220, 255, int(35 + 80 * loud)))
        pulse.setColorAt(1.0, QtGui.QColor(0, 0, 0, 0))
        p.fillRect(rect, pulse)

        def road_x(side, depth):
            center = horizon_x + (bottom_center_x - horizon_x) * depth
            half = (w * 0.06) + (w * 0.40) * (depth ** 1.35)
            return center + side * half

        def y_at(depth):
            return horizon_y + (road_bottom_y - horizon_y) * (depth ** 1.35)

        road = QtGui.QPolygonF([
            QtCore.QPointF(road_x(-1, 0.0), y_at(0.0)),
            QtCore.QPointF(road_x(1, 0.0), y_at(0.0)),
            QtCore.QPointF(road_x(1, 1.0), y_at(1.0)),
            QtCore.QPointF(road_x(-1, 1.0), y_at(1.0)),
        ])
        road_grad = QtGui.QLinearGradient(0, horizon_y, 0, road_bottom_y)
        road_grad.setColorAt(0.0, QtGui.QColor(18, 12, 32, 210))
        road_grad.setColorAt(1.0, QtGui.QColor(5, 5, 14, 245))
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QBrush(road_grad))
        p.drawPolygon(road)

        for side, col in ((-1, QtGui.QColor(255, 55, 200)), (1, QtGui.QColor(20, 230, 255))):
            edge_path = QtGui.QPainterPath()
            for step in range(18):
                depth = step / 17.0
                pt = QtCore.QPointF(road_x(side, depth), y_at(depth))
                if step == 0:
                    edge_path.moveTo(pt)
                else:
                    edge_path.lineTo(pt)
            glow = QtGui.QColor(col); glow.setAlpha(75)
            pen = QtGui.QPen(glow, 7.0)
            pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawPath(edge_path)
            col.setAlpha(220)
            p.setPen(QtGui.QPen(col, 2.0))
            p.drawPath(edge_path)

        lane_phase = (self._phase * 1.95) % 1.0
        for line in (-0.33, 0.33):
            for seg in range(8):
                d1 = ((seg + lane_phase) / 8.0)
                d2 = min(1.0, d1 + 0.045 + d1 * 0.07)
                if d1 >= 1.0:
                    continue
                x1 = road_x(line, d1)
                x2 = road_x(line, d2)
                y1 = y_at(d1)
                y2 = y_at(d2)
                alpha = int(60 + 170 * d1)
                p.setPen(QtGui.QPen(QtGui.QColor(255, 240, 135, alpha), 1.0 + 4.0 * d1))
                p.drawLine(QtCore.QPointF(x1, y1), QtCore.QPointF(x2, y2))

        building_count = min(14, max(8, n // 2))
        for side in (-1, 1):
            for i in range(building_count):
                depth = 1.0 - (i / float(building_count)) * 0.92
                depth = (depth + self._phase * 0.48) % 1.0
                depth = max(0.05, depth)
                level_idx = (i * 2 + (0 if side < 0 else n // 3)) % n
                lvl = max(0.0, min(1.0, self._levels[level_idx]))
                y_base = y_at(depth)
                road_edge = road_x(side, depth)
                width = (9.0 + 34.0 * depth) * (1.0 + 0.25 * lvl)
                depth_3d = 7.0 + 34.0 * depth
                setback = (18.0 + 76.0 * depth)
                x_near = road_edge + side * setback
                height = (18.0 + 150.0 * depth) * (0.35 + 0.95 * lvl)
                top = y_base - height
                x_left = x_near if side > 0 else x_near - width
                rect_b = QtCore.QRectF(x_left, top, width, height)
                frac = (i / float(max(1, building_count - 1)) + (0.18 if side > 0 else 0.0)) % 1.0
                col = self._band_color(frac, lvl)
                glow = QtGui.QColor(col); glow.setAlpha(55 + int(85 * lvl))
                depth_dx = side * depth_3d
                depth_dy = -depth_3d * 0.48
                side_poly = QtGui.QPolygonF([
                    QtCore.QPointF(rect_b.right() if side > 0 else rect_b.left(), rect_b.top()),
                    QtCore.QPointF((rect_b.right() if side > 0 else rect_b.left()) + depth_dx, rect_b.top() + depth_dy),
                    QtCore.QPointF((rect_b.right() if side > 0 else rect_b.left()) + depth_dx, rect_b.bottom() + depth_dy),
                    QtCore.QPointF(rect_b.right() if side > 0 else rect_b.left(), rect_b.bottom()),
                ])
                roof_poly = QtGui.QPolygonF([
                    QtCore.QPointF(rect_b.left(), rect_b.top()),
                    QtCore.QPointF(rect_b.right(), rect_b.top()),
                    QtCore.QPointF(rect_b.right() + depth_dx, rect_b.top() + depth_dy),
                    QtCore.QPointF(rect_b.left() + depth_dx, rect_b.top() + depth_dy),
                ])
                p.setPen(QtCore.Qt.PenStyle.NoPen)
                p.setBrush(glow)
                p.drawRect(rect_b.adjusted(-2, -5, 2, 3))
                body = QtGui.QLinearGradient(rect_b.left(), rect_b.top(), rect_b.right(), rect_b.bottom())
                dark = QtGui.QColor(col); dark.setHsvF(col.hueF(), 0.85, 0.16)
                hot = QtGui.QColor(col); hot.setHsvF(col.hueF(), 0.75, 0.85)
                body.setColorAt(0.0, hot)
                body.setColorAt(1.0, dark)
                side_col = QtGui.QColor(col); side_col.setHsvF(col.hueF(), 0.90, 0.26)
                roof_col = QtGui.QColor(col); roof_col.setHsvF(col.hueF(), 0.55, 0.62)
                p.setBrush(side_col)
                p.drawPolygon(side_poly)
                p.setBrush(roof_col)
                p.drawPolygon(roof_poly)
                p.setBrush(QtGui.QBrush(body))
                p.drawRect(rect_b)

                window_col = QtGui.QColor(255, 240, 160, 95 + int(105 * lvl))
                p.setBrush(window_col)
                rows = max(1, int(height / 14.0))
                for row in range(rows):
                    if (row + i + (0 if side < 0 else 1)) % 3 == 0:
                        continue
                    yy = y_base - 7 - row * 12
                    if yy < top + 4:
                        break
                    p.drawRect(QtCore.QRectF(rect_b.left() + width * 0.28, yy, max(2.0, width * 0.42), 2.0))
                edge_col = QtGui.QColor(20, 230, 255, 55 + int(90 * lvl))
                p.setPen(QtGui.QPen(edge_col, 1.0))
                p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                p.drawPolygon(side_poly)
                p.drawPolygon(roof_poly)
                p.drawRect(rect_b)

        p.setPen(QtGui.QPen(QtGui.QColor(255, 70, 210, 120), 1.0))
        p.drawLine(QtCore.QPointF(0, horizon_y), QtCore.QPointF(w, horizon_y))

    def _paint_cassette(self, p, rect):
        w, h = rect.width(), rect.height()
        loud_l, loud_r = self._lr_levels
        loud = max(0.0, min(1.0, 0.5 * (loud_l + loud_r)))
        deck = rect.adjusted(28, 22, -28, -20)
        if deck.width() <= 30 or deck.height() <= 20:
            return

        body_grad = QtGui.QLinearGradient(deck.left(), deck.top(), deck.right(), deck.bottom())
        body_grad.setColorAt(0.0, QtGui.QColor(35, 20, 55, 230))
        body_grad.setColorAt(1.0, QtGui.QColor(8, 8, 20, 230))
        p.setPen(QtGui.QPen(QtGui.QColor(255, 70, 210, 120), 1.5))
        p.setBrush(QtGui.QBrush(body_grad))
        p.drawRoundedRect(deck, 9, 9)

        window = QtCore.QRectF(deck.left() + deck.width() * 0.19, deck.top() + deck.height() * 0.18,
                               deck.width() * 0.62, deck.height() * 0.48)
        p.setPen(QtGui.QPen(QtGui.QColor(20, 230, 255, 120), 1.2))
        p.setBrush(QtGui.QColor(5, 5, 16, 190))
        p.drawRoundedRect(window, 6, 6)

        for idx, level in enumerate((loud_l, loud_r)):
            cx = window.left() + window.width() * (0.28 + idx * 0.44)
            cy = window.center().y()
            reel_r = min(window.width(), window.height()) * (0.18 + 0.05 * level)
            p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 80), 1.0))
            p.setBrush(QtGui.QColor(18, 10, 30))
            p.drawEllipse(QtCore.QPointF(cx, cy), reel_r, reel_r)
            angle = self._phase * (2.2 + idx * 0.4)
            p.setPen(QtGui.QPen(QtGui.QColor(20, 230, 255, 150), 2.0))
            for spoke in range(3):
                a = angle + spoke * math.tau / 3.0
                p.drawLine(QtCore.QPointF(cx, cy), QtCore.QPointF(cx + math.cos(a) * reel_r * 0.8, cy + math.sin(a) * reel_r * 0.8))

        meter_top = deck.top() + deck.height() * 0.72
        meter_w = deck.width() * 0.32
        for idx, level in enumerate((loud_l, loud_r)):
            mx = deck.left() + deck.width() * (0.16 + idx * 0.50)
            meter = QtCore.QRectF(mx, meter_top, meter_w, 8)
            p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 55), 1.0))
            p.setBrush(QtGui.QColor(10, 7, 18, 180))
            p.drawRoundedRect(meter, 4, 4)
            fill = QtCore.QRectF(meter.left(), meter.top(), meter.width() * max(0.04, level), meter.height())
            grad = QtGui.QLinearGradient(fill.left(), 0, fill.right(), 0)
            grad.setColorAt(0.0, QtGui.QColor(20, 230, 255, 220))
            grad.setColorAt(0.65, QtGui.QColor(255, 70, 210, 230))
            grad.setColorAt(1.0, QtGui.QColor(255, 230, 80, 230))
            p.setBrush(QtGui.QBrush(grad))
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.drawRoundedRect(fill, 4, 4)

        p.setPen(QtGui.QPen(QtGui.QColor(255, 80, 210, int(80 + 100 * loud)), 1.0))
        p.drawLine(QtCore.QPointF(window.left() + window.width() * 0.18, window.center().y()),
                   QtCore.QPointF(window.right() - window.width() * 0.18, window.center().y()))

    def _paint_orbit(self, p, rect, n):
        w, h = rect.width(), rect.height()
        cx, cy = w * 0.5, h * 0.52
        loud = max(0.0, min(1.0, 0.5 * (self._lr_levels[0] + self._lr_levels[1])))
        base_r = max(24.0, min(w, h) * (0.18 + 0.08 * loud))
        p.setPen(QtGui.QPen(QtGui.QColor(20, 230, 255, 70), 1.0))
        for ring in range(3):
            rr = base_r * (1.0 + ring * 0.48)
            p.drawEllipse(QtCore.QPointF(cx, cy), rr, rr * (0.72 + ring * 0.05))

        count = min(max(12, n), 36)
        for i in range(count):
            lvl = max(0.0, min(1.0, self._levels[i % n]))
            frac = i / float(max(1, count))
            angle = self._phase * (0.9 + (i % 3) * 0.22) + frac * math.tau
            rx = base_r * (1.0 + 1.3 * frac + 0.50 * lvl)
            ry = rx * 0.72
            x = cx + math.cos(angle) * rx
            y = cy + math.sin(angle) * ry
            size = 2.5 + 10.0 * lvl
            col = self._band_color(frac, lvl)
            glow = QtGui.QColor(col); glow.setAlpha(70)
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(glow)
            p.drawEllipse(QtCore.QPointF(x, y), size * 2.2, size * 2.2)
            col.setAlpha(180 + int(70 * lvl))
            p.setBrush(col)
            p.drawEllipse(QtCore.QPointF(x, y), size, size)

        center = QtGui.QRadialGradient(cx, cy, base_r * 1.15)
        center.setColorAt(0.0, QtGui.QColor(255, 70, 210, int(120 + 80 * loud)))
        center.setColorAt(0.55, QtGui.QColor(20, 230, 255, int(80 + 70 * loud)))
        center.setColorAt(1.0, QtGui.QColor(0, 0, 0, 0))
        p.setBrush(QtGui.QBrush(center))
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.drawEllipse(QtCore.QPointF(cx, cy), base_r * 1.1, base_r * 1.1)

    def _paint_tunnel(self, p, rect, n):
        w, h = rect.width(), rect.height()
        cx, cy = w * 0.5, h * 0.52
        loud = max(0.0, min(1.0, 0.5 * (self._lr_levels[0] + self._lr_levels[1])))
        max_r = min(w, h) * 0.72
        for ring in range(9, 0, -1):
            frac = ring / 9.0
            idx = int(frac * (n - 1))
            lvl = max(0.0, min(1.0, self._levels[idx]))
            pulse = (self._phase * 0.45 + frac) % 1.0
            rr = max_r * (0.14 + 0.82 * frac) * (0.86 + 0.08 * math.sin(self._phase * 3.0 + ring))
            alpha = int(32 + 95 * lvl + 45 * loud)
            col = self._band_color(frac, lvl)
            col.setAlpha(alpha)
            pen = QtGui.QPen(col, 1.2 + 3.0 * lvl)
            pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            sides = 5 + (ring % 3)
            points = []
            spin = self._phase * 0.6 + pulse * math.tau
            for j in range(sides):
                a = spin + j * math.tau / sides
                x = cx + math.cos(a) * rr
                y = cy + math.sin(a) * rr * 0.62
                points.append(QtCore.QPointF(x, y))
            p.drawPolygon(QtGui.QPolygonF(points))

        ray_count = min(18, n)
        for i in range(ray_count):
            lvl = max(0.0, min(1.0, self._levels[i % n]))
            frac = i / float(max(1, ray_count))
            a = self._phase * 0.35 + frac * math.tau
            col = self._band_color(frac, lvl)
            col.setAlpha(45 + int(105 * lvl))
            p.setPen(QtGui.QPen(col, 1.0 + 2.0 * lvl))
            p.drawLine(QtCore.QPointF(cx, cy), QtCore.QPointF(cx + math.cos(a) * max_r, cy + math.sin(a) * max_r * 0.62))

    def _paint_dots(self, p, pad, inner_w, baseline, usable_h, n):
        bar_w, gap = self._bar_geometry(pad, inner_w, n)
        x = float(pad)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        for i in range(n):
            lvl = max(0.0, min(1.0, self._levels[i]))
            frac_x = i / float(max(1, n - 1))
            cy = baseline - lvl * usable_h
            r = max(2.0, bar_w * 0.5)
            col = self._band_color(frac_x, lvl)
            glow = QtGui.QColor(col); glow.setAlpha(80)
            p.setBrush(glow)
            p.drawEllipse(QtCore.QPointF(x + bar_w / 2, cy), r * 2.0, r * 2.0)
            p.setBrush(col)
            p.drawEllipse(QtCore.QPointF(x + bar_w / 2, cy), r, r)
            x += bar_w + gap

class ShimmerFrame(QtWidgets.QFrame):
    def __init__(self, child: QtWidgets.QWidget, parent=None):
        super().__init__(parent)
        self._child = child
        self._phase = 0.0
        self._speed = 2.5
        self._alpha = 90
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(40)
        self.setFrameStyle(QtWidgets.QFrame.Shape.NoFrame)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.addWidget(child)

    def _tick(self):
        self._phase = (self._phase + self._speed) % 360.0
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        r = self.rect().adjusted(0, 0, -1, -1)
        if r.width() <= 0 or r.height() <= 0:
            return
        a = self._alpha
        center = QtCore.QPointF(r.center())
        grad = QtGui.QConicalGradient(center, self._phase)
        grad.setColorAt(0.0, QtGui.QColor(255, 0, 0, a))
        grad.setColorAt(0.17, QtGui.QColor(255, 255, 0, a))
        grad.setColorAt(0.33, QtGui.QColor(0, 255, 0, a))
        grad.setColorAt(0.50, QtGui.QColor(0, 200, 255, a))
        grad.setColorAt(0.67, QtGui.QColor(120, 0, 255, a))
        grad.setColorAt(0.83, QtGui.QColor(255, 0, 255, a))
        grad.setColorAt(1.0, QtGui.QColor(255, 0, 0, a))
        base = QtGui.QPen(QtGui.QColor(90, 110, 130, 80), 2.0)
        base.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        base.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        painter.setPen(base)
        painter.drawRect(r)
        pen = QtGui.QPen(QtGui.QBrush(grad), 2.0)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawRect(r)

