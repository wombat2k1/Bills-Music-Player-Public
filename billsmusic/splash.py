"""Lightweight startup splash and truthful stage coordinator."""
import os
import sys
import time

from PyQt6 import QtCore, QtGui, QtWidgets
from . import __version__


ARTWORK_WIDTH = 1672
ARTWORK_HEIGHT = 941
MINIMUM_SPLASH_DURATION_MS = 400
PROGRESS_RECT = (505, 808, 630, 14)
STATUS_RECT = (430, 886, 812, 42)
VERSION_RECT = (22, 18, 190, 30)


def resource_path(*parts):
    """Resolve a bundled or source resource without depending on cwd."""
    if hasattr(sys, "_MEIPASS"):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def remaining_minimum_ms(
    shown_at, now=None, minimum_ms=MINIMUM_SPLASH_DURATION_MS
):
    now = time.perf_counter() if now is None else now
    elapsed_ms = max(0.0, (now - shown_at) * 1000.0)
    return max(0, int(round(float(minimum_ms) - elapsed_ms)))


class StartupSplash(QtWidgets.QWidget):
    """Frameless artwork splash with a live, proportionally placed bar."""

    def __init__(self, app, image_path=None, warning_logger=None):
        super().__init__(
            None,
            QtCore.Qt.WindowType.SplashScreen
            | QtCore.Qt.WindowType.FramelessWindowHint,
        )
        self._app = app
        self._warning_logger = warning_logger
        self._progress_value = 0
        self._fallback = False
        self.setAccessibleName("Bills Music Player loading")
        self.setAccessibleDescription("Bills Music Player is starting")

        self.artwork = QtWidgets.QLabel(self)
        self.artwork.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.artwork.setScaledContents(False)

        path = image_path or resource_path(
            "billsmusic", "assets", "bills_music_splash.png"
        )
        self._source_pixmap = QtGui.QPixmap(path)
        if self._source_pixmap.isNull():
            self._fallback = True
            self._source_pixmap = QtGui.QPixmap(
                ARTWORK_WIDTH, ARTWORK_HEIGHT
            )
            self._source_pixmap.fill(QtGui.QColor("#070412"))
            self.artwork.setText("Bills Music Player\n\nLoading...")
            self.artwork.setStyleSheet(
                "color:#eaf2ff;background:#070412;"
                "font:600 34pt 'Segoe UI';"
            )
            if warning_logger is not None:
                warning_logger(
                    f"Startup splash artwork unavailable: {path}; using fallback"
                )

        self.progress = QtWidgets.QProgressBar(self)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setAccessibleName("Startup progress")
        self.progress.setStyleSheet(
            "QProgressBar {"
            "border:0;background:#070513;border-radius:5px;padding:0;"
            "}"
            "QProgressBar::chunk {"
            "border:0;border-radius:5px;"
            "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #16e6e9,stop:0.34 #168fff,"
            "stop:0.68 #6c35ff,stop:1 #ed24dc);"
            "}"
        )

        self.status = QtWidgets.QLabel(
            "Starting Bills Music Player...", self
        )
        self.status.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.status.setStyleSheet(
            "color:#bfefff;background:rgba(5,3,15,185);"
            "border-radius:5px;padding:2px 10px;"
            "font:600 10pt 'Segoe UI';"
        )
        self.status.setAccessibleName("Startup status")
        self.version = QtWidgets.QLabel(f"v{__version__}", self)
        self.version.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft
            | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        self.version.setStyleSheet(
            "color:rgba(220,235,255,205);background:rgba(5,3,15,120);"
            "border-radius:4px;padding:1px 6px;"
            "font:500 8pt 'Segoe UI';"
        )
        self.version.setAccessibleName(
            f"Bills Music Player version {__version__}"
        )

        screen = (
            QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos())
            or app.primaryScreen()
        )
        available = screen.availableGeometry()
        target_width = min(
            900,
            ARTWORK_WIDTH,
            max(320, int(available.width() * 0.90)),
        )
        target_height = int(round(target_width * ARTWORK_HEIGHT / ARTWORK_WIDTH))
        if target_height > int(available.height() * 0.90):
            target_height = max(180, int(available.height() * 0.90))
            target_width = int(
                round(target_height * ARTWORK_WIDTH / ARTWORK_HEIGHT)
            )
        self.resize(target_width, target_height)
        self.move(available.center() - self.rect().center())
        self._layout_overlays()

    @property
    def using_fallback(self):
        return self._fallback

    def _scaled_rect(self, original_rect):
        x, y, width, height = original_rect
        scale_x = self.width() / ARTWORK_WIDTH
        scale_y = self.height() / ARTWORK_HEIGHT
        return QtCore.QRect(
            int(round(x * scale_x)),
            int(round(y * scale_y)),
            max(1, int(round(width * scale_x))),
            max(1, int(round(height * scale_y))),
        )

    def _layout_overlays(self):
        self.artwork.setGeometry(self.rect())
        if not self._fallback:
            self.artwork.setPixmap(
                self._source_pixmap.scaled(
                    self.size(),
                    QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                    QtCore.Qt.TransformationMode.SmoothTransformation,
                )
            )
        self.progress.setGeometry(self._scaled_rect(PROGRESS_RECT))
        self.status.setGeometry(self._scaled_rect(STATUS_RECT))
        self.version.setGeometry(self._scaled_rect(VERSION_RECT))

    def resizeEvent(self, event):
        self._layout_overlays()
        super().resizeEvent(event)

    def set_stage(self, text, progress):
        value = max(0, min(100, int(progress)))
        value = max(self._progress_value, value)
        self._progress_value = value
        self.progress.setValue(value)
        if text:
            self.status.setText(str(text))
            self.status.setAccessibleDescription(str(text))
            self.setAccessibleDescription(str(text))

    def set_failed(self):
        text = "Bills Music Player could not start"
        self.status.setText(text)
        self.status.setAccessibleDescription(text)
        self.setAccessibleDescription(text)

    def finish(self, window):
        self.hide()
        self.close()
        if window is not None:
            window.raise_()
            window.activateWindow()


class StartupCoordinator(QtCore.QObject):
    """Records genuine startup milestones and feeds the live splash."""

    stage_changed = QtCore.pyqtSignal(str)
    progress_changed = QtCore.pyqtSignal(int)
    startup_complete = QtCore.pyqtSignal(object)
    startup_failed = QtCore.pyqtSignal(str)

    def __init__(self, splash, logger, started_at=None, clock=None):
        super().__init__()
        self.splash = splash
        self.logger = logger
        self.clock = clock or time.perf_counter
        self.started_at = self.clock() if started_at is None else started_at
        self.last_stage_at = self.started_at
        self.splash_shown_at = None
        self.main_window_first_paint_at = None
        self.splash_finished_at = None
        self.outcome = None
        self.progress = 0
        self.metrics = {}
        self.stage_changed.connect(self._show_stage)
        self.progress_changed.connect(self._show_progress)

    def _show_stage(self, text):
        self.splash.set_stage(text, self.progress)

    def _show_progress(self, value):
        self.splash.set_stage("", value)

    def mark_splash_shown(self):
        self.splash_shown_at = self.clock()

    def report(self, key, text, progress, duration_ms=None):
        now = self.clock()
        value = max(self.progress, max(0, min(100, int(progress))))
        if duration_ms is None:
            duration_ms = (now - self.last_stage_at) * 1000.0
        elapsed_ms = (now - self.started_at) * 1000.0
        self.progress = value
        self.metrics[str(key)] = float(duration_ms)
        self.stage_changed.emit(str(text))
        self.progress_changed.emit(value)
        self.logger(
            "Startup stage complete: "
            f"stage={key}; duration_ms={duration_ms:.1f}; "
            f"elapsed_ms={elapsed_ms:.1f}"
        )
        self.last_stage_at = now

    def complete(self, window):
        if self.outcome is not None:
            return
        now = self.clock()
        self.outcome = "ready"
        self.main_window_first_paint_at = now
        first_paint_ms = self.metrics.get("first-main-window-paint")
        self.report(
            "first-main-window-paint",
            "Ready",
            100,
            first_paint_ms,
        )
        self.logger(
            "Main window ready: "
            f"process_to_first_paint_ms={(now - self.started_at) * 1000.0:.1f}; "
            f"qapplication_ms={self.metrics.get('qapplication', 0.0):.1f}; "
            f"heavy_import_ms={self.metrics.get('heavy-imports', 0.0):.1f}; "
            f"preferences_ms={self.metrics.get('preferences', 0.0):.1f}; "
            f"audio_backend_ms={self.metrics.get('audio-backends', 0.0):.1f}; "
            f"library_cache_ms={self.metrics.get('library-cache', 0.0):.1f}; "
            f"library_restore_ms={self.metrics.get('library-restore', 0.0):.1f}; "
            f"window_shell_ms={self.metrics.get('main-window-shell', 0.0):.1f}; "
            f"session_restore_ms={self.metrics.get('session', 0.0):.1f}; "
            f"recently_played_ms={self.metrics.get('recently-played', 0.0):.1f}; "
            f"first_paint_ms={self.metrics.get('first-main-window-paint', 0.0):.1f}"
        )
        self.startup_complete.emit(window)

    def fail(self, message):
        if self.outcome is not None:
            return
        self.outcome = "failed"
        self.splash.set_failed()
        self.logger(f"Startup failed: {message}")
        self.startup_failed.emit(str(message))

    def finish(self, window):
        if self.splash_finished_at is not None:
            return
        self.splash.finish(window)
        now = self.clock()
        self.splash_finished_at = now
        shown_at = self.splash_shown_at or self.started_at
        first_paint_at = self.main_window_first_paint_at
        first_paint_ms = (
            (first_paint_at - self.started_at) * 1000.0
            if first_paint_at is not None
            else 0.0
        )
        self.logger(
            "Startup finished: "
            f"outcome={self.outcome or 'failed'}; "
            f"process_total_ms={(now - self.started_at) * 1000.0:.1f}; "
            f"process_to_first_paint_ms={first_paint_ms:.1f}; "
            f"splash_visible_ms={(now - shown_at) * 1000.0:.1f}"
        )
