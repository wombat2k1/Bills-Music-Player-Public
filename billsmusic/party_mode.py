"""Party Mode: a full-screen, second-monitor presentation view backed by the
main PlayerWindow.

This module owns no playback engine, no copy of the queue, and no lyric
parser. Every command is forwarded to the owner's existing QActions/methods
(exactly like mini_player.py already does), and every piece of displayed
state is read -- never mutated -- from the owner on each sync.
"""
import os
import time
from typing import List, Optional

from PyQt6 import QtCore, QtGui, QtWidgets
from .cdg import CdgWidget

from .config import album_cover_cache_path
from .overlay import _box_blur_argb
from .waveform_widget import WaveformSeekBar
from .widgets import BeatWidget
from .visualiser_lifecycle import VisualiserLifecycleController, VisualiserRunState, should_visualiser_run


# ---------------------------------------------------------------------------
# Pure helpers (no Qt state, easily testable)
# ---------------------------------------------------------------------------

def upcoming_unplayed_paths(queue, queue_played, limit=None):
    """Read-only preview of the queue's unplayed tracks. Never mutates."""
    paths = [p for p, played in zip(queue, queue_played) if not played]
    return paths if limit is None else paths[:limit]


def resolve_party_mode_screen(preferred_name, owner_window):
    """Preferred monitor by stable name, else the monitor hosting the main
    window, else the primary screen. Mirrors mini_player.py's
    restore_geometry_safely fallback shape -- the only other multi-monitor
    code in this app."""
    if preferred_name:
        for screen in QtGui.QGuiApplication.screens():
            if screen.name() == preferred_name:
                return screen
    handle = owner_window.windowHandle() if owner_window is not None else None
    screen = handle.screen() if handle is not None else None
    return screen or QtGui.QGuiApplication.primaryScreen()


def format_screen_label(screen):
    geo = screen.geometry()
    name = screen.name() or "Display"
    return f"{name} — {geo.width()}×{geo.height()}"


def _blur_radius_for_quality(quality):
    return {"low": 0, "medium": 14, "high": 26}.get(quality, 14)


def _visualiser_interval_ms_for_quality(quality):
    # BeatWidget repaints on its own internal 16ms (60fps) timer regardless
    # of caller -- fine at the main window's modest panel size, but visibly
    # laggy painted at full screen on a large/4K display. Throttling this
    # interval is the only real lever available without changing BeatWidget
    # itself (there's no lighter-weight render path to opt into).
    return {"low": 66, "medium": 33, "high": 16}.get(quality, 33)


def _parse_mmss(text):
    try:
        parts = [int(p) for p in str(text).split(":")]
    except Exception:
        return 0.0
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return float(seconds)


# ---------------------------------------------------------------------------
# Off-GUI-thread background blur (reuses overlay.py's numpy box blur)
# ---------------------------------------------------------------------------

class _BlurSignals(QtCore.QObject):
    finished = QtCore.pyqtSignal(int, object)  # generation, QPixmap


class _BackgroundBlurTask(QtCore.QRunnable):
    def __init__(self, generation, image, target_size, radius):
        super().__init__()
        self.generation = generation
        self.image = image
        self.target_size = target_size
        self.radius = radius
        self.signals = _BlurSignals()

    @QtCore.pyqtSlot()
    def run(self):
        try:
            scaled = self.image.scaled(
                max(1, self.target_size[0]), max(1, self.target_size[1]),
                QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            )
            argb = scaled.convertToFormat(QtGui.QImage.Format.Format_ARGB32_Premultiplied)
            blurred = _box_blur_argb(argb, self.radius) if self.radius > 0 else argb
            painter = QtGui.QPainter(blurred)
            painter.fillRect(blurred.rect(), QtGui.QColor(0, 0, 0, 110))
            painter.end()
            pixmap = QtGui.QPixmap.fromImage(blurred)
        except Exception:
            pixmap = QtGui.QPixmap()
        self.signals.finished.emit(self.generation, pixmap)


# ---------------------------------------------------------------------------
# Shared bottom info bar (present in all three layouts)
# ---------------------------------------------------------------------------

class PartyModeInfoBar(QtWidgets.QWidget):
    seek_requested = QtCore.pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PartyModeInfoBar")
        self._build_ui()

    def _build_ui(self):
        self.setStyleSheet(
            "#PartyModeInfoBar { background: rgba(10,7,22,205); }"
            "QLabel { color:#eaf2ff; }"
        )
        outer = QtWidgets.QHBoxLayout(self)
        outer.setContentsMargins(32, 12, 32, 16)
        outer.setSpacing(28)

        self.artwork_label = QtWidgets.QLabel("♪")
        self.artwork_label.setFixedSize(96, 96)
        self.artwork_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.artwork_label.setStyleSheet(
            "background:#120a22;border-radius:12px;border:1px solid #6a3fc9;"
            "color:#21e6ff;font-size:28pt;"
        )
        outer.addWidget(self.artwork_label)

        text_col = QtWidgets.QVBoxLayout()
        text_col.setSpacing(2)
        self.title_label = QtWidgets.QLabel("Ready")
        title_font = QtGui.QFont()
        title_font.setPointSize(20)
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        self.title_label.setStyleSheet("color:#fff8c8;")
        self.artist_album_label = QtWidgets.QLabel("")
        artist_font = QtGui.QFont()
        artist_font.setPointSize(13)
        self.artist_album_label.setFont(artist_font)
        self.artist_album_label.setStyleSheet("color:#21e6ff;")
        self.paused_label = QtWidgets.QLabel("PAUSED")
        self.paused_label.setStyleSheet("color:#ff3cac;font-weight:700;font-size:11pt;")
        self.paused_label.setVisible(False)
        text_col.addWidget(self.title_label)
        text_col.addWidget(self.artist_album_label)
        text_col.addWidget(self.paused_label)
        outer.addLayout(text_col, 1)

        progress_col = QtWidgets.QVBoxLayout()
        progress_row = QtWidgets.QHBoxLayout()
        self.elapsed_label = QtWidgets.QLabel("0:00")
        self.elapsed_label.setStyleSheet("color:#cbb8ff;font-size:12pt;")
        self.remaining_label = QtWidgets.QLabel("-0:00")
        self.remaining_label.setStyleSheet("color:#cbb8ff;font-size:12pt;")
        self.progress_bar = WaveformSeekBar()
        progress_row.addWidget(self.elapsed_label)
        progress_row.addWidget(self.progress_bar, 1)
        progress_row.addWidget(self.remaining_label)
        progress_col.addLayout(progress_row)
        outer.addLayout(progress_col, 2)

        right_col = QtWidgets.QVBoxLayout()
        right_col.setSpacing(2)
        self.clock_label = QtWidgets.QLabel("")
        self.clock_label.setStyleSheet("color:#eaf2ff;font-size:16pt;font-weight:600;")
        self.clock_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        self.up_next_heading = QtWidgets.QLabel("Up Next")
        self.up_next_heading.setStyleSheet("color:#a996c7;font-size:10pt;font-weight:600;")
        self.up_next_heading.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        self.up_next_label = QtWidgets.QLabel("")
        self.up_next_label.setStyleSheet("color:#cbb8ff;font-size:11pt;")
        self.up_next_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        self.remaining_playlist_label = QtWidgets.QLabel("")
        self.remaining_playlist_label.setStyleSheet("color:#8fd7c0;font-size:9pt;")
        self.remaining_playlist_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self.clock_label)
        right_col.addWidget(self.up_next_heading)
        right_col.addWidget(self.up_next_label)
        right_col.addWidget(self.remaining_playlist_label)
        outer.addLayout(right_col)

        self.progress_bar.seek_requested.connect(self.seek_requested)

    # -- setters, all called from PartyModeWindow.sync_from_owner ---------

    def set_track(self, title, artist, album):
        self.title_label.setText(title or "Ready")
        details = " · ".join(part for part in (artist, album) if part)
        self.artist_album_label.setText(details)

    def set_paused(self, paused):
        self.paused_label.setVisible(bool(paused))

    def set_artwork(self, pixmap):
        if pixmap is None or pixmap.isNull():
            self.artwork_label.setPixmap(QtGui.QPixmap())
            self.artwork_label.setText("♪")
            return
        self.artwork_label.setText("")
        self.artwork_label.setPixmap(
            pixmap.scaled(
                self.artwork_label.size(),
                QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            )
        )

    def sync_progress(self, value, maximum, elapsed_text, remaining_text):
        self.progress_bar.blockSignals(True)
        self.progress_bar.setRange(0, max(1, maximum))
        self.progress_bar.setValue(value)
        self.progress_bar.blockSignals(False)
        self.elapsed_label.setText(elapsed_text)
        self.remaining_label.setText(remaining_text)

    def set_waveform(self, data):
        self.progress_bar.set_waveform(data)

    def sync_progress_visual(self, source_bar, path):
        """Mirror the owner's waveform/video/loading state, not just data."""
        mode = getattr(source_bar, "_placeholder_mode", "empty")
        current_mode = getattr(self.progress_bar, "_placeholder_mode", "empty")
        current_path = getattr(self.progress_bar, "_pending_path", None)
        if mode == "video":
            if current_mode != "video" or current_path != path:
                self.progress_bar.set_video_progress(path)
        elif mode == "loading":
            if current_mode != "loading" or current_path != path:
                self.progress_bar.set_placeholder(path)
        else:
            self.progress_bar.set_waveform(getattr(source_bar, "_waveform", None))

    def set_clock_visible(self, visible):
        self.clock_label.setVisible(bool(visible))
        if not visible:
            self.clock_label.setText("")

    def set_clock_text(self, text):
        self.clock_label.setText(text)

    def set_up_next_visible(self, visible):
        self.up_next_heading.setVisible(bool(visible))
        self.up_next_label.setVisible(bool(visible))

    def set_up_next_lines(self, lines):
        self.up_next_label.setText("\n".join(lines) if lines else "Queue is empty")

    def set_remaining_playlist_visible(self, visible):
        self.remaining_playlist_label.setVisible(bool(visible))

    def set_remaining_playlist_text(self, text):
        self.remaining_playlist_label.setText(text or "")


# ---------------------------------------------------------------------------
# Fading control bar (transient, mouse-move revealed)
# ---------------------------------------------------------------------------

class PartyModeControlBar(QtWidgets.QWidget):
    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        self.setObjectName("PartyModeControlBar")
        self._build_ui()
        self._opacity_effect = QtWidgets.QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity_effect)
        self._fade_anim = QtCore.QPropertyAnimation(self._opacity_effect, b"opacity", self)
        self._fade_anim.setDuration(180)
        self.setVisible(False)

    def _build_ui(self):
        # Plain text labels, not symbol/emoji glyphs -- glyphs like the media
        # transport symbols aren't guaranteed to be in every system font and
        # can render as blank "tofu" boxes (this is what mini_player.py's
        # buttons already avoid by using plain text for the same reason).
        self.setStyleSheet(
            "#PartyModeControlBar { background: rgba(10,7,22,220); border-radius: 14px; }"
            "QToolButton {"
            "  background:#21113f; border:1px solid #5a3aa0; border-radius:8px;"
            "  color:#f3e9ff; font-size:14pt; font-weight:600; padding:10px 20px;"
            "}"
            "QToolButton:hover { border:1px solid #21e6ff; color:#21e6ff; }"
        )
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(18, 10, 18, 10)
        layout.setSpacing(10)
        self.btn_previous = QtWidgets.QToolButton()
        self.btn_previous.setText("Prev")
        self.btn_play_pause = QtWidgets.QToolButton()
        self.btn_play_pause.setText("Play/Pause")
        self.btn_next = QtWidgets.QToolButton()
        self.btn_next.setText("Next")
        self.btn_layout = QtWidgets.QToolButton()
        self.btn_layout.setText("Layout")
        self.btn_previous.clicked.connect(self.owner.action_previous.trigger)
        self.btn_play_pause.clicked.connect(self.owner.action_play_pause_alternate.trigger)
        self.btn_next.clicked.connect(self.owner.action_next.trigger)
        # No setToolTip() here: native tooltips rendered unreadable (black
        # box, invisible text) against Party Mode's dark full-screen theme.
        # The button labels are already self-explanatory, so tooltips add
        # nothing -- accessibleName still covers screen readers.
        for button, tip in (
            (self.btn_previous, "Previous track"),
            (self.btn_play_pause, "Play or pause"),
            (self.btn_next, "Next track"),
            (self.btn_layout, "Switch layout"),
        ):
            button.setAccessibleName(tip)
            button.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            layout.addWidget(button)

    def reveal(self, animate=True):
        self.setVisible(True)
        self._fade_anim.stop()
        if animate:
            self._fade_anim.setStartValue(self._opacity_effect.opacity())
            self._fade_anim.setEndValue(1.0)
            self._fade_anim.start()
        else:
            self._opacity_effect.setOpacity(1.0)

    def conceal(self, animate=True):
        self._fade_anim.stop()
        if animate:
            self._fade_anim.setStartValue(self._opacity_effect.opacity())
            self._fade_anim.setEndValue(0.0)
            try:
                self._fade_anim.finished.disconnect(self._hide_if_transparent)
            except Exception:
                pass
            self._fade_anim.finished.connect(self._hide_if_transparent)
            self._fade_anim.start()
        else:
            self._opacity_effect.setOpacity(0.0)
            self.setVisible(False)

    def _hide_if_transparent(self):
        try:
            self._fade_anim.finished.disconnect(self._hide_if_transparent)
        except Exception:
            pass
        if self._opacity_effect.opacity() <= 0.01:
            self.setVisible(False)


# ---------------------------------------------------------------------------
# Layout: Lyrics
# ---------------------------------------------------------------------------

class LyricsLayoutWidget(QtWidgets.QWidget):
    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        self._animations_enabled = True
        self._build_ui()

    def _build_ui(self):
        self.setStyleSheet("background: transparent;")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(60, 40, 60, 20)
        layout.addStretch(1)

        dim_font = QtGui.QFont()
        dim_font.setPointSize(26)
        dim_font.setBold(True)

        self.previous_label = QtWidgets.QLabel("")
        self.previous_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.previous_label.setWordWrap(True)
        self.previous_label.setFont(dim_font)
        self.previous_label.setStyleSheet("color: rgba(234,242,255,110);")

        current_font = QtGui.QFont()
        current_font.setPointSize(52)
        current_font.setBold(True)
        self.current_label = QtWidgets.QLabel("")
        self.current_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.current_label.setWordWrap(True)
        self.current_label.setFont(current_font)
        self.current_label.setStyleSheet("color: #fff8c8;")
        self._current_opacity_effect = QtWidgets.QGraphicsOpacityEffect(self.current_label)
        self._current_opacity_effect.setOpacity(1.0)
        self.current_label.setGraphicsEffect(self._current_opacity_effect)

        self.next_label = QtWidgets.QLabel("")
        self.next_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.next_label.setWordWrap(True)
        self.next_label.setFont(dim_font)
        self.next_label.setStyleSheet("color: rgba(234,242,255,110);")

        self.empty_label = QtWidgets.QLabel("No lyrics available for this track")
        self.empty_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        empty_font = QtGui.QFont()
        empty_font.setPointSize(22)
        self.empty_label.setFont(empty_font)
        self.empty_label.setStyleSheet("color: #a996c7;")
        self.empty_label.setVisible(False)

        layout.addWidget(self.previous_label)
        layout.addSpacing(18)
        layout.addWidget(self.current_label)
        layout.addSpacing(18)
        layout.addWidget(self.next_label)
        layout.addWidget(self.empty_label)
        layout.addStretch(1)

        self._fade_anim = QtCore.QPropertyAnimation(self._current_opacity_effect, b"opacity", self)
        self._fade_anim.setDuration(250)
        self._fade_anim.setStartValue(0.25)
        self._fade_anim.setEndValue(1.0)

    def set_animations_enabled(self, enabled):
        self._animations_enabled = bool(enabled)

    def set_track(self, title, artist):
        pass  # Lyrics layout relies on the shared info bar for title/artist

    def show_lines(self, previous_text, current_text, next_text, animate=True):
        self.empty_label.setVisible(False)
        for widget in (self.previous_label, self.current_label, self.next_label):
            widget.setVisible(True)
        self.previous_label.setText(previous_text or "")
        self.current_label.setText(current_text or "")
        self.next_label.setText(next_text or "")
        if animate and self._animations_enabled:
            self._fade_anim.stop()
            self._fade_anim.start()
        else:
            self._current_opacity_effect.setOpacity(1.0)

    def show_no_lyrics(self):
        for widget in (self.previous_label, self.current_label, self.next_label):
            widget.setVisible(False)
            widget.setText("")
        self.empty_label.setVisible(True)


# ---------------------------------------------------------------------------
# Layout: Artwork
# ---------------------------------------------------------------------------

class ArtworkLayoutWidget(QtWidgets.QWidget):
    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        self._background_pixmap = None
        self._build_ui()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addStretch(1)

        self.art_label = QtWidgets.QLabel("♪")
        self.art_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.art_label.setFixedSize(560, 560)
        art_font = QtGui.QFont()
        art_font.setPointSize(72)
        self.art_label.setFont(art_font)
        self.art_label.setStyleSheet(
            "background:#120a22;border-radius:16px;border:2px solid #6a3fc9;color:#21e6ff;"
        )
        art_row = QtWidgets.QHBoxLayout()
        art_row.addStretch(1)
        art_row.addWidget(self.art_label)
        art_row.addStretch(1)
        layout.addLayout(art_row)

        layout.addSpacing(28)
        self.title_label = QtWidgets.QLabel("")
        self.title_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        title_font = QtGui.QFont()
        title_font.setPointSize(30)
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        self.title_label.setStyleSheet("color:#fff8c8;")
        self.artist_label = QtWidgets.QLabel("")
        self.artist_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        artist_font = QtGui.QFont()
        artist_font.setPointSize(18)
        self.artist_label.setFont(artist_font)
        self.artist_label.setStyleSheet("color:#21e6ff;")
        layout.addWidget(self.title_label)
        layout.addWidget(self.artist_label)
        layout.addStretch(1)

    def set_track(self, title, artist):
        self.title_label.setText(title or "")
        self.artist_label.setText(artist or "")

    def set_artwork(self, pixmap):
        if pixmap is None or pixmap.isNull():
            self.art_label.setPixmap(QtGui.QPixmap())
            self.art_label.setText("♪")
            return
        self.art_label.setText("")
        self.art_label.setPixmap(
            pixmap.scaled(
                self.art_label.size(),
                QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            )
        )

    def set_background(self, pixmap):
        self._background_pixmap = pixmap
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        if self._background_pixmap is not None and not self._background_pixmap.isNull():
            painter.drawPixmap(self.rect(), self._background_pixmap)
        else:
            painter.fillRect(self.rect(), QtGui.QColor(10, 7, 22))
        painter.end()
        super().paintEvent(event)


# ---------------------------------------------------------------------------
# Layout: Visualiser
# ---------------------------------------------------------------------------

class VisualiserLayoutWidget(QtWidgets.QWidget):
    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 10)
        layout.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel("")
        title_font = QtGui.QFont()
        title_font.setPointSize(20)
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        self.title_label.setStyleSheet("color:#fff8c8;")
        self.artist_label = QtWidgets.QLabel("")
        artist_font = QtGui.QFont()
        artist_font.setPointSize(14)
        self.artist_label.setFont(artist_font)
        self.artist_label.setStyleSheet("color:#21e6ff;")
        header.addWidget(self.title_label)
        header.addSpacing(16)
        header.addWidget(self.artist_label)
        header.addStretch(1)
        layout.addLayout(header)

        # BeatWidget only sets a minimum HEIGHT (120px) and has the default
        # QWidget size policy (Preferred, not Expanding), so nesting it in
        # stretch-padded layouts to "center" it collapsed it down to its
        # near-zero natural width instead of filling the available space --
        # nothing visible was actually a widget rendered a few pixels wide.
        # Passing an alignment to addWidget() doesn't fix this either: Qt
        # only "fills the cell" when NO alignment is given -- specifying one
        # (even AlignCenter) switches it to size-to-hint-then-position,
        # which collapsed it right back down to its minimum. An explicit
        # Expanding policy with NO alignment argument is what actually lets
        # it grow to fill the cell.
        #
        # This used to also cap the widget at 1600x900 to bound per-frame
        # paint cost on the (mistaken) assumption that BeatWidget's own
        # rendering was the source of Party Mode's lag -- real per-consumer
        # diagnostics later proved that wrong (the actual cause was
        # waveform_widget.py re-rendering its bars on every sync tick, now
        # fixed), so it no longer needs to sacrifice being edge-to-edge.
        self.beat = BeatWidget(diagnostic_consumer="party_mode")
        self.beat.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self.beat.setMinimumSize(320, 180)
        layout.addWidget(self.beat, 1)

    def set_track(self, title, artist):
        self.title_label.setText(title or "")
        self.artist_label.setText(artist or "")

    def set_visual_mode(self, mode):
        try:
            self.beat.set_visual_mode(mode)
        except Exception:
            pass

    def set_quality(self, quality):
        interval = _visualiser_interval_ms_for_quality(quality)
        try:
            self.beat._timer.setInterval(interval)
        except Exception:
            pass

    def push_levels(self, levels):
        self.beat.setLevels(levels)

    def set_playing(self, playing):
        self.beat.setPlaying(bool(playing))


# ---------------------------------------------------------------------------
# Top-level window
# ---------------------------------------------------------------------------

class PartyModeWindow(QtWidgets.QWidget):
    """A lightweight, full-screen view that delegates all commands to its
    owner -- no independent playback engine, queue, or lyric parser."""

    def __init__(self, owner):
        super().__init__(None, QtCore.Qt.WindowType.Window)
        self.owner = owner
        self._allow_close = False
        # v1.0.66 worker-lifetime hardening: _BackgroundBlurTask runs on
        # the global QThreadPool (uncancellable once started), and this
        # window is never deleteLater()'d on close (see closeEvent below),
        # so it stays a live receiver for a late finished() callback
        # unless that callback checks this flag itself.
        self._closing = False
        self.active_layout = "lyrics"
        self._synced_path = None
        self._synced_lyric_idx = None
        self._synced_up_next_key = None
        self._artwork_generation = 0
        self._blur_tasks = set()
        self._visualiser_lifecycle = VisualiserLifecycleController("party_mode")

        self.setWindowTitle("Bills Music Player — Party Mode")
        self.setObjectName("PartyModeWindow")
        self.setStyleSheet(
            "QWidget#PartyModeWindow { background:#05030c; }"
            # This is a separate top-level window from the main PlayerWindow,
            # so it doesn't inherit its dark-theme stylesheet -- native
            # QToolTip/QMenu popups (e.g. the waveform seek bar's hover-time
            # tooltip) fell back to unreadable default OS colors here.
            "QToolTip {"
            "  background:#140c28; color:#eaf2ff;"
            "  border:1px solid #3a2a55; padding:4px 8px;"
            "}"
            "QMenu {"
            "  background:#140c28; color:#eaf2ff;"
            "  border:1px solid #3a2a55; border-radius:8px;"
            "}"
            "QMenu::item:selected { background:rgba(255,60,172,0.30); }"
        )
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

        self._build_ui()

        self._auto_hide_timer = QtCore.QTimer(self)
        self._auto_hide_timer.setSingleShot(True)
        self._auto_hide_timer.timeout.connect(self._on_auto_hide_timeout)

        app = QtGui.QGuiApplication.instance()
        if app is not None:
            app.screenRemoved.connect(self._on_screen_removed)

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.stack = QtWidgets.QStackedWidget()
        self.lyrics_layout = LyricsLayoutWidget(self.owner)
        self.artwork_layout = ArtworkLayoutWidget(self.owner)
        self.visualiser_layout = VisualiserLayoutWidget(self.owner)
        self.stack.addWidget(self.lyrics_layout)
        self.stack.addWidget(self.artwork_layout)
        self.stack.addWidget(self.visualiser_layout)
        # A reusable video page -- created once, never destroyed. Showing
        # video here only ever reattaches the owner's single shared video
        # backend's output (see attach_output() in video_backend.py); it
        # never creates a second decoder/child process. This is a plain
        # container -- the actual video window gets embedded into it by
        # native window ID, it doesn't render video itself.
        self.video_widget = QtWidgets.QWidget()
        self.video_widget.setStyleSheet("background:#000000;")
        video_widget_layout = QtWidgets.QVBoxLayout(self.video_widget)
        video_widget_layout.setContentsMargins(0, 0, 0, 0)
        self.stack.addWidget(self.video_widget)
        self.karaoke_widget = CdgWidget()
        self.stack.addWidget(self.karaoke_widget)
        self._video_active = False
        self._video_active_widget = None
        self._pre_video_layout = None
        self._video_fullscreen_presentation = False
        root.addWidget(self.stack, 1)

        # The embedded BeatWidget's double-click/Escape are wired to a
        # separate main-window-only feature (toggling its OWN fullscreen
        # visualiser panel) that Party Mode never connects -- left alone,
        # double-click here is a silent no-op despite the widget's own
        # tooltip claiming it exits full screen, and clicking the equaliser
        # (which grabs keyboard focus) swallows Escape before it ever
        # reaches this window's own close-on-Escape handler below. Route
        # both to the same close behavior as Escape/hide already use.
        self.visualiser_layout.beat.fullscreen_toggle_requested.connect(self.hide)
        self.visualiser_layout.beat.fullscreen_exit_requested.connect(self.hide)

        self.info_bar = PartyModeInfoBar()
        self.info_bar.seek_requested.connect(self._on_seek_requested)
        root.addWidget(self.info_bar, 0)

        self.control_bar = PartyModeControlBar(self.owner, parent=self)
        self.control_bar.btn_layout.clicked.connect(self._cycle_layout)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_control_bar()

    def _position_control_bar(self):
        bar = self.control_bar
        bar.adjustSize()
        x = (self.width() - bar.width()) // 2
        y = self.height() - self.info_bar.height() - bar.height() - 24
        bar.move(max(0, x), max(0, y))

    # -- open/close lifecycle ------------------------------------------------

    def open_party_mode(self):
        self._apply_layout(getattr(self.owner, "party_mode_default_layout", "lyrics"), persist=False)
        self._reposition_on_screen()
        self.showFullScreen()
        handle = self.windowHandle()
        target = resolve_party_mode_screen(
            getattr(self.owner, "party_mode_screen_name", ""), self.owner,
        )
        if handle is not None and handle.screen() is not target:
            handle.setScreen(target)
            self.setGeometry(target.geometry())
        self._position_control_bar()
        self.sync_from_owner(force=True)
        self.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)

    def _reposition_on_screen(self):
        screen = resolve_party_mode_screen(
            getattr(self.owner, "party_mode_screen_name", ""), self.owner,
        )
        self.setGeometry(screen.geometry())

    def showEvent(self, event):
        super().showEvent(event)
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self.control_bar.reveal(False)
        delay_ms = int(getattr(self.owner, "party_mode_auto_hide_ms", 3000))
        self._auto_hide_timer.start(max(500, delay_ms))
        attach_video = getattr(self.owner, "_attach_video_to_party_mode", None)
        if attach_video is not None:
            attach_video()
        self._refresh_visualiser_lifecycle("party_mode_shown")

    def hideEvent(self, event):
        if (
            getattr(self.owner, "_video_fullscreen", False)
            and getattr(self.owner, "_video_fullscreen_owner_widget", None) is self
        ):
            self.owner._exit_video_fullscreen()
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self._auto_hide_timer.stop()
        if self._video_active:
            detach_video = getattr(self.owner, "_detach_video_from_party_mode", None)
            if detach_video is not None:
                detach_video()
        super().hideEvent(event)
        self._refresh_visualiser_lifecycle("party_mode_hidden")

    # -- visualiser suspend/resume lifecycle --------------------------------
    # Own BeatWidget instance, own render timer -- suspended whenever Party
    # Mode isn't visible or "visualiser" isn't the selected layout, resumed
    # when both are true. Reuses the same controller class the main window
    # uses, but with its own independent state (each window is its own
    # visible/hidden consumer of the shared analysis feed).

    def _effective_visualiser_visibility(self) -> bool:
        return should_visualiser_run(
            window_visible=self.isVisible(),
            panel_visible=self.active_layout == "visualiser" and not self._video_active,
            closing=getattr(self.owner, "_closing", False),
        )

    # -- video takeover --------------------------------------------------------
    # Video/karaoke temporarily overrides whichever lyrics/artwork/visualiser
    # layout was selected; the pre-video selection (self._pre_video_layout)
    # is remembered separately so returning to audio restores exactly what
    # the user had picked. While active, "video" becomes a fourth stop in
    # the L-key cycle (see _cycle_layout) so a user who scrolls away from
    # the picture to check lyrics/artwork/visualiser can scroll back to it
    # without needing to stop and restart playback.

    def show_video(self):
        if not self._video_active:
            self._pre_video_layout = self.active_layout
            self._video_active = True
        self._video_active_widget = self.video_widget
        self.active_layout = "video"
        self.stack.setCurrentWidget(self.video_widget)
        self._refresh_visualiser_lifecycle("party_mode_video_shown")

    def show_karaoke(self, document):
        if not self._video_active:
            self._pre_video_layout = self.active_layout
            self._video_active = True
        self.karaoke_widget.set_document(document)
        self._video_active_widget = self.karaoke_widget
        self.active_layout = "video"
        self.stack.setCurrentWidget(self.karaoke_widget)
        self._refresh_visualiser_lifecycle("party_mode_karaoke_shown")

    def set_karaoke_position(self, position_ms):
        if self.stack.currentWidget() is self.karaoke_widget:
            self.karaoke_widget.set_position(position_ms)

    def return_to_normal_layout(self):
        if not self._video_active:
            return
        self._video_active = False
        self._video_active_widget = None
        self.karaoke_widget.clear()
        # Reuses the existing layout-switch method (not just a raw stack
        # index) so returning to "visualiser" re-primes its playing/quality
        # state exactly like a normal layout switch would.
        self._apply_layout(self._pre_video_layout or self.active_layout, persist=False)
        self._pre_video_layout = None
        self._refresh_visualiser_lifecycle("party_mode_video_hidden")

    def enter_video_fullscreen_presentation(self):
        """Expand the existing Party video page without reparenting it."""
        if not self._video_active or self._video_fullscreen_presentation:
            return None
        restore_state = {
            "info_bar_visible": not self.info_bar.isHidden(),
            "control_bar_visible": not self.control_bar.isHidden(),
        }
        self._video_fullscreen_presentation = True
        self._auto_hide_timer.stop()
        self.info_bar.hide()
        self.control_bar.conceal(False)
        self._position_control_bar()
        self.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
        return restore_state

    def exit_video_fullscreen_presentation(self, restore_state):
        if not self._video_fullscreen_presentation:
            return
        self._video_fullscreen_presentation = False
        restore_state = restore_state if isinstance(restore_state, dict) else {}
        self.info_bar.setVisible(bool(restore_state.get("info_bar_visible", True)))
        if restore_state.get("control_bar_visible", False):
            self.control_bar.reveal(False)
        else:
            self.control_bar.conceal(False)
        self._position_control_bar()
        delay_ms = int(getattr(self.owner, "party_mode_auto_hide_ms", 3000))
        self._auto_hide_timer.start(max(500, delay_ms))

    def _refresh_visualiser_lifecycle(self, reason: str):
        transition = self._visualiser_lifecycle.evaluate(
            self._effective_visualiser_visibility(),
            reason=reason, now=time.perf_counter(),
        )
        if transition is not None:
            beat = getattr(self.visualiser_layout, "beat", None)
            if beat is not None:
                try:
                    if transition.current is VisualiserRunState.ACTIVE:
                        beat.resume()
                    else:
                        beat.suspend()
                except Exception:
                    pass
        owner = getattr(self, "owner", None)
        refresh_owner = getattr(owner, "_refresh_visualiser_lifecycle", None)
        if refresh_owner is not None:
            refresh_owner("party_mode_changed")

    def closeEvent(self, event):
        if self._allow_close:
            self._closing = True
            event.accept()
            return
        event.ignore()
        self.hide()

    def _on_screen_removed(self, removed_screen):
        if not self.isVisible():
            return
        handle = self.windowHandle()
        current = handle.screen() if handle is not None else None
        if current is not removed_screen:
            return
        self._reposition_on_screen()
        self.showFullScreen()

    # -- layout switching ------------------------------------------------------

    _LAYOUT_ORDER = ("lyrics", "artwork", "visualiser")

    def _cycle_layout(self):
        # "video" is only a valid stop in the cycle while video/karaoke is
        # actually showing -- without this, scrolling away from the picture
        # (to check lyrics/artwork/visualiser) had no way back to it short
        # of stopping and restarting playback.
        order = self._LAYOUT_ORDER + ("video",) if self._video_active else self._LAYOUT_ORDER
        current = self.active_layout if self.active_layout in order else "lyrics"
        next_layout = order[(order.index(current) + 1) % len(order)]
        self.set_layout(next_layout)

    def set_layout(self, name):
        self._apply_layout(name, persist=True)

    def _apply_layout(self, name, persist):
        if name == "video":
            if not self._video_active or self._video_active_widget is None:
                name = "lyrics"
            else:
                self.active_layout = "video"
                self.stack.setCurrentWidget(self._video_active_widget)
                self._refresh_visualiser_lifecycle("party_mode_layout_changed")
                return
        if name not in self._LAYOUT_ORDER:
            name = "lyrics"
        self.active_layout = name
        widget = {
            "lyrics": self.lyrics_layout,
            "artwork": self.artwork_layout,
            "visualiser": self.visualiser_layout,
        }[name]
        self.stack.setCurrentWidget(widget)
        if persist:
            self.owner.party_mode_default_layout = name
            self.owner._save_user_settings()
        if name == "visualiser":
            playing = bool(
                getattr(self.owner, "_playback_expected", False)
                and not getattr(self.owner, "_playback_intentionally_paused", False)
            )
            self.visualiser_layout.set_playing(playing)
            self.visualiser_layout.set_visual_mode(getattr(self.owner.beat, "visual_mode", "neon"))
            self.visualiser_layout.set_quality(self._visual_quality())
        self._refresh_visualiser_lifecycle("party_mode_layout_changed")

    # -- preferences-driven visuals ---------------------------------------------

    def _animations_enabled(self):
        return bool(getattr(self.owner, "party_mode_animations_enabled", True))

    def _visual_quality(self):
        return getattr(self.owner, "party_mode_visual_quality", "medium")

    def apply_preferences(self):
        """Call after Preferences are saved so an already-open Party Mode
        reflects the new settings immediately, without restarting playback."""
        self.lyrics_layout.set_animations_enabled(self._animations_enabled())
        self.info_bar.set_clock_visible(getattr(self.owner, "party_mode_show_clock", True))
        self.info_bar.set_up_next_visible(getattr(self.owner, "party_mode_show_up_next", True))
        self.info_bar.set_remaining_playlist_visible(
            getattr(self.owner, "party_mode_show_remaining_playlist_time", False)
        )
        self.visualiser_layout.set_quality(self._visual_quality())
        self.sync_from_owner(force=True)

    # -- artwork -----------------------------------------------------------------

    def _request_artwork(self, path):
        self._artwork_generation += 1
        generation = self._artwork_generation
        if not path:
            self._apply_artwork(None, generation)
            return
        meta = self.owner._meta_by_path.get(path) or {}
        artist = str(meta.get("album_artist") or meta.get("artist") or "")
        album = str(meta.get("album") or "")
        key = f"{artist}::{album}"
        disk_path = album_cover_cache_path(artist, album) if artist and album else None
        manager = getattr(self.owner, "artwork_manager", None)
        if manager is None:
            self._apply_artwork(None, generation)
            return
        size = (self._artwork_target_size(), self._artwork_target_size())

        def callback(result, gen=generation):
            if gen != self._artwork_generation:
                return  # superseded by a newer track change; drop it
            self._apply_artwork(result.image, gen)

        if not manager.request(key, [path], disk_path, size, generation, callback):
            self._apply_artwork(None, generation)

    def _artwork_target_size(self):
        return max(400, min(1200, min(self.width() or 900, self.height() or 900)))

    def _apply_artwork(self, image, generation):
        if generation != self._artwork_generation:
            return
        if image is None or image.isNull():
            self.info_bar.set_artwork(None)
            self.artwork_layout.set_artwork(None)
            self.artwork_layout.set_background(None)
            return
        pixmap = QtGui.QPixmap.fromImage(image)
        self.info_bar.set_artwork(pixmap)
        self.artwork_layout.set_artwork(pixmap)
        self._request_background_blur(image, generation)

    def _request_background_blur(self, image, generation):
        if self._closing:
            return
        radius = _blur_radius_for_quality(self._visual_quality())
        size = (max(1, self.width()), max(1, self.height()))
        task = _BackgroundBlurTask(generation, image, size, radius)
        self._blur_tasks.add(task)

        def finished(gen, pixmap, t=task):
            self._blur_tasks.discard(t)
            if self._closing or gen != self._artwork_generation:
                return
            self.artwork_layout.set_background(pixmap)

        task.signals.finished.connect(finished)
        QtCore.QThreadPool.globalInstance().start(task)

    # -- sync from owner (the only source of truth) ------------------------------

    def sync_from_owner(self, force=False):
        # Diagnostic instrumentation: this is the one Party-Mode-specific
        # method that runs on every tick (~200ms, via window.py's
        # _sync_party_mode), so it's the prime suspect for the periodic
        # GUI-thread stalls that only show up while Party Mode is open.
        # Only records when slow (>=30ms), so a normal-speed session
        # doesn't get a log line 5x/second -- same "only log what's
        # actionable" pattern used elsewhere (e.g. _refresh_queue_list).
        diagnostics = getattr(self.owner, "diagnostics", None)
        if diagnostics is None:
            return self._sync_from_owner_impl(force)
        started = time.perf_counter()
        result = self._sync_from_owner_impl(force)
        duration_ms = (time.perf_counter() - started) * 1000.0
        if duration_ms >= 30.0:
            diagnostics.record(
                "party_mode", "sync_from_owner",
                duration_ms=duration_ms,
                severity="warning" if duration_ms >= 100.0 else "info",
                details={"active_layout": self.active_layout, "force": bool(force)},
                minimum_level="basic",
            )
        return result

    def _sync_from_owner_impl(self, force=False):
        owner = self.owner
        path = owner.current_path or ""
        meta = owner._meta_by_path.get(path) or {}
        title = str(meta.get("title") or (os.path.splitext(os.path.basename(path))[0] if path else "Ready"))
        artist = str(meta.get("artist") or "")
        album = str(meta.get("album") or "")

        if force or path != self._synced_path:
            self._synced_path = path
            self.info_bar.set_track(title, artist, album)
            self.artwork_layout.set_track(title, artist)
            self.visualiser_layout.set_track(title, artist)
            self._request_artwork(path or None)

        paused = bool(owner._playback_intentionally_paused) or not bool(owner._playback_expected)
        self.info_bar.set_paused(paused and bool(path))
        if self.active_layout == "visualiser":
            self.visualiser_layout.set_playing(
                bool(owner._playback_expected and not owner._playback_intentionally_paused)
            )

        maximum = max(1, owner.slider_progress.maximum())
        value = owner.slider_progress.value()
        length_ms = getattr(owner, "_last_progress_length_ms", 0) or 0
        elapsed_s = (value / float(maximum)) * (length_ms / 1000.0) if length_ms else 0.0
        elapsed_text = owner._format_duration(int(max(0.0, elapsed_s)))
        remaining_text = owner.label_remaining.text() or "-0:00"
        self.info_bar.sync_progress(value, maximum, elapsed_text, remaining_text)

        waveform_bar = getattr(owner, "waveform_seekbar", None)
        if waveform_bar is not None:
            self.info_bar.sync_progress_visual(waveform_bar, path)

        if getattr(owner, "party_mode_show_clock", True):
            self.info_bar.set_clock_text(QtCore.QTime.currentTime().toString("h:mm ap"))

        owner._ensure_queue_played_flags()
        count = int(getattr(owner, "party_mode_up_next_count", 3))
        upcoming = upcoming_unplayed_paths(owner.queue, owner.queue_played, count)
        up_next_key = tuple(upcoming)
        if force or up_next_key != self._synced_up_next_key:
            self._synced_up_next_key = up_next_key
            lines = []
            for up_path in upcoming:
                up_meta = owner._meta_by_path.get(up_path) or {}
                up_title = up_meta.get("title") or os.path.splitext(os.path.basename(up_path))[0]
                up_artist = up_meta.get("artist") or ""
                lines.append(f"{up_artist} — {up_title}" if up_artist else str(up_title))
            self.info_bar.set_up_next_lines(lines)
            if getattr(owner, "party_mode_show_remaining_playlist_time", False):
                self.info_bar.set_remaining_playlist_text(self._remaining_playlist_text(owner))

        lyrics = getattr(owner, "_lyrics", None) or []
        idx = getattr(owner, "_lyric_idx", None)
        if not lyrics:
            if force or self._synced_lyric_idx is not None:
                self._synced_lyric_idx = None
                self.lyrics_layout.show_no_lyrics()
        elif force or idx != self._synced_lyric_idx:
            self._synced_lyric_idx = idx
            previous_text = lyrics[idx - 1][1] if idx is not None and idx - 1 >= 0 else ""
            current_text = lyrics[idx][1] if idx is not None and 0 <= idx < len(lyrics) else ""
            next_text = lyrics[idx + 1][1] if idx is not None and idx + 1 < len(lyrics) else ""
            self.lyrics_layout.show_lines(previous_text, current_text, next_text, animate=not force)

    def _remaining_playlist_text(self, owner):
        upcoming = upcoming_unplayed_paths(owner.queue, owner.queue_played, None)
        total = 0.0
        details_fn = getattr(owner, "_queue_track_details", None)
        for path in upcoming:
            meta = owner._meta_by_path.get(path) or {}
            seconds = meta.get("duration_seconds")
            if seconds:
                total += float(seconds)
                continue
            if details_fn is not None:
                try:
                    details = details_fn(path, cached_details_only=True)
                except Exception:
                    details = None
                text = (details or {}).get("time") if details else None
                if text:
                    total += _parse_mmss(text)
        if total <= 0:
            return ""
        return f"{owner._format_duration(int(total))} remaining in queue"

    def push_levels(self, levels):
        if self.active_layout == "visualiser" and self.isVisible():
            self.visualiser_layout.push_levels(levels)

    # -- controls / keyboard ---------------------------------------------------

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.Type.MouseMove:
            # This filter is installed app-wide (QApplication.installEventFilter),
            # so obj can be a bare QWindow (native window-manager events, other
            # top-level windows) as well as a QWidget -- isAncestorOf() only
            # accepts a QWidget and raises TypeError on anything else.
            if (
                not self._video_fullscreen_presentation
                and (
                    obj is self
                    or (isinstance(obj, QtWidgets.QWidget) and self.isAncestorOf(obj))
                )
            ):
                self.control_bar.reveal(self._animations_enabled())
                delay_ms = int(getattr(self.owner, "party_mode_auto_hide_ms", 3000))
                self._auto_hide_timer.start(max(500, delay_ms))
        return False

    def _on_auto_hide_timeout(self):
        self.control_bar.conceal(self._animations_enabled())

    def keyPressEvent(self, event):
        key = event.key()
        modifiers = event.modifiers()
        owner = self.owner
        if key == QtCore.Qt.Key.Key_Escape:
            if (
                getattr(owner, "_video_fullscreen", False)
                and getattr(owner, "_video_fullscreen_owner_widget", None) is self
            ):
                owner._exit_video_fullscreen()
            else:
                self.hide()
            event.accept()
            return
        if key == QtCore.Qt.Key.Key_Space:
            owner.action_play_pause_alternate.trigger()
            event.accept()
            return
        if key == QtCore.Qt.Key.Key_L and modifiers == QtCore.Qt.KeyboardModifier.NoModifier:
            self._cycle_layout()
            event.accept()
            return
        if key == QtCore.Qt.Key.Key_Left:
            if modifiers & QtCore.Qt.KeyboardModifier.ControlModifier:
                owner.action_previous.trigger()
            else:
                self._seek_relative(-10.0)
            event.accept()
            return
        if key == QtCore.Qt.Key.Key_Right:
            if modifiers & QtCore.Qt.KeyboardModifier.ControlModifier:
                owner.action_next.trigger()
            else:
                self._seek_relative(10.0)
            event.accept()
            return
        if key == QtCore.Qt.Key.Key_Up:
            owner.action_volume_up.trigger()
            event.accept()
            return
        if key == QtCore.Qt.Key.Key_Down:
            owner.action_volume_down.trigger()
            event.accept()
            return
        super().keyPressEvent(event)

    def _seek_relative(self, delta_seconds):
        owner = self.owner
        maximum = max(1, owner.slider_progress.maximum())
        length_ms = getattr(owner, "_last_progress_length_ms", 0) or 0
        if length_ms <= 0:
            return
        length_s = length_ms / 1000.0
        current_s = (owner.slider_progress.value() / float(maximum)) * length_s
        target_s = max(0.0, min(length_s, current_s + delta_seconds))
        ratio = target_s / length_s if length_s else 0.0
        owner.slider_progress.setValue(int(round(ratio * maximum)))
        owner._progress_release()

    def _on_seek_requested(self, ratio):
        owner = self.owner
        maximum = max(1, owner.slider_progress.maximum())
        owner.slider_progress.setValue(int(round(ratio * maximum)))
        owner._progress_release()
