"""Compact presentation-only window backed by the main PlayerWindow."""
import os

from PyQt6 import QtCore, QtGui, QtWidgets

from .metadata import read_cover_bytes
from .widgets import MarqueeLabel


class _ArtworkLoadSignals(QtCore.QObject):
    loaded = QtCore.pyqtSignal(str, str, object)


class _ArtworkLoadTask(QtCore.QRunnable):
    """Read, decode and scale current-track artwork outside the GUI thread."""

    def __init__(self, path, cache_key, size=(126, 126)):
        super().__init__()
        self.path = path
        self.cache_key = cache_key
        self.size = size
        self.signals = _ArtworkLoadSignals()

    @QtCore.pyqtSlot()
    def run(self):
        try:
            data = read_cover_bytes(self.path)
            image = QtGui.QImage.fromData(data) if data else QtGui.QImage()
            if not image.isNull():
                image = image.scaled(
                    self.size[0],
                    self.size[1],
                    QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                    QtCore.Qt.TransformationMode.SmoothTransformation,
                )
        except Exception:
            image = QtGui.QImage()
        self.signals.loaded.emit(self.path, self.cache_key, image)


class ElidedLabel(QtWidgets.QLabel):
    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full_text = text
        self.setMinimumWidth(0)

    def setText(self, text):
        self._full_text = str(text or "")
        super().setText(self._full_text)
        self.setToolTip(self._full_text)
        self._apply_elision()

    def resizeEvent(self, event):
        self._apply_elision()
        super().resizeEvent(event)

    def _apply_elision(self):
        metrics = self.fontMetrics()
        elided = metrics.elidedText(
            self._full_text, QtCore.Qt.TextElideMode.ElideRight,
            max(20, self.contentsRect().width()),
        )
        QtWidgets.QLabel.setText(self, elided)


class MiniMarqueeLabel(MarqueeLabel):
    """Full-player marquee behaviour with Mini Player label semantics."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setText(text)

    def setText(self, text):
        self._full_text = str(text or "")
        self.setToolTip(self._full_text)
        super().setText(self._full_text)


class MiniPlayerWindow(QtWidgets.QWidget):
    """A lightweight view that delegates all commands to its owner."""

    return_to_full_requested = QtCore.pyqtSignal()
    settings_changed = QtCore.pyqtSignal()

    def __init__(self, owner):
        super().__init__(None, QtCore.Qt.WindowType.Window)
        self.owner = owner
        self._allow_close = False
        # v1.0.66 worker-lifetime hardening: _ArtworkLoadTask runs on the
        # global QThreadPool, which cannot cancel a task once started --
        # this window is also never deleteLater()'d on close (see
        # closeEvent below), so it stays a live receiver for a late
        # finished() callback unless that callback checks this flag itself.
        self._closing = False
        self._synced_path = None
        self._artwork_path = None
        self._artwork_load_path = None
        self._artwork_tasks = set()
        self._seeking = False
        self.setWindowTitle("Bills Music Player — Mini Player")
        self.setMinimumSize(380, 180)
        self.setMaximumSize(720, 320)
        self.resize(420, 190)
        self._build_ui()
        self._configure_shortcuts()
        self.sync_from_owner(force=True)

    def _build_ui(self):
        self.setStyleSheet("""
            QWidget { background:#0a0716; color:#eaf2ff; font-family:'Segoe UI'; font-size:10pt; }
            QPushButton, QToolButton {
                background:#21113f; border:1px solid #5a3aa0; border-radius:7px;
                padding:5px 9px; color:#f3e9ff; font-weight:600;
            }
            QPushButton:hover, QToolButton:hover { border:1px solid #21e6ff; }
            QPushButton:focus, QToolButton:focus { border:2px solid #fff8c8; }
            QSlider::groove:horizontal { height:5px; border-radius:2px; background:#1d1336; }
            QSlider::sub-page:horizontal { background:#ff3cac; border-radius:2px; }
            QSlider::handle:horizontal {
                width:14px; margin:-5px 0; border-radius:7px;
                background:#ffffff; border:2px solid #21e6ff;
            }
            QCheckBox { color:#cbb8ff; }
        """)
        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(12)

        self.artwork = QtWidgets.QLabel("♪")
        self.artwork.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.artwork.setFixedSize(126, 126)
        self.artwork.setStyleSheet(
            "background:#120a22;border:1px solid #b14cff;border-radius:9px;"
            "color:#21e6ff;font-size:36pt;"
        )
        root.addWidget(self.artwork, 0, QtCore.Qt.AlignmentFlag.AlignTop)

        content = QtWidgets.QVBoxLayout()
        content.setSpacing(4)
        title_row = QtWidgets.QHBoxLayout()
        self.title_label = MiniMarqueeLabel("Ready")
        title_font = self.title_label.font()
        title_font.setPointSize(12)
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        self.title_label.setTextColor("#fff8c8")
        title_row.addWidget(self.title_label, 1)
        self.btn_top = QtWidgets.QToolButton()
        self.btn_top.setText("Pin")
        self.btn_top.setCheckable(True)
        self.btn_top.toggled.connect(self._set_always_on_top)
        title_row.addWidget(self.btn_top)
        self.btn_full = QtWidgets.QToolButton()
        self.btn_full.setText("Full")
        self.btn_full.clicked.connect(self.return_to_full_requested)
        title_row.addWidget(self.btn_full)
        self.btn_close = QtWidgets.QToolButton()
        self.btn_close.setText("×")
        self.btn_close.clicked.connect(self.return_to_full_requested)
        title_row.addWidget(self.btn_close)
        content.addLayout(title_row)

        self.artist_label = MiniMarqueeLabel("")
        self.artist_label.setTextColor("#21e6ff")
        content.addWidget(self.artist_label)
        self.album_label = ElidedLabel("")
        self.album_label.setStyleSheet("color:#a996c7;font-size:9pt;")
        content.addWidget(self.album_label)

        progress_row = QtWidgets.QHBoxLayout()
        self.progress = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.progress.setRange(0, 1000)
        self.progress.sliderPressed.connect(self._begin_seek)
        self.progress.sliderReleased.connect(self._finish_seek)
        progress_row.addWidget(self.progress, 1)
        self.time_label = QtWidgets.QLabel("-0:00")
        self.time_label.setMinimumWidth(48)
        self.time_label.setStyleSheet("color:#21e6ff;")
        progress_row.addWidget(self.time_label)
        content.addLayout(progress_row)

        controls = QtWidgets.QHBoxLayout()
        self.btn_previous = QtWidgets.QPushButton("Back")
        self.btn_play_pause = QtWidgets.QPushButton("Play")
        self.btn_stop = QtWidgets.QPushButton("Stop")
        self.btn_next = QtWidgets.QPushButton("Next")
        self.btn_previous.clicked.connect(self.owner.action_previous.trigger)
        self.btn_play_pause.clicked.connect(self.owner.action_play_pause_alternate.trigger)
        self.btn_stop.clicked.connect(self.owner.action_stop.trigger)
        self.btn_next.clicked.connect(self.owner.action_next.trigger)
        for button in (self.btn_previous, self.btn_play_pause, self.btn_stop, self.btn_next):
            controls.addWidget(button)
        content.addLayout(controls)

        volume_row = QtWidgets.QHBoxLayout()
        volume_label = QtWidgets.QLabel("Volume")
        volume_label.setStyleSheet("color:#a996c7;font-size:9pt;")
        volume_row.addWidget(volume_label)
        self.volume = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.valueChanged.connect(self._volume_changed)
        volume_row.addWidget(self.volume, 1)
        self.btn_mute = QtWidgets.QPushButton("Mute")
        self.btn_mute.clicked.connect(self.owner.action_mute.trigger)
        volume_row.addWidget(self.btn_mute)
        content.addLayout(volume_row)

        self.next_label = ElidedLabel("Next: Library playback")
        self.next_label.setStyleSheet("color:#cbb8ff;font-size:9pt;")
        content.addWidget(self.next_label)
        self.sleep_timer_label = ElidedLabel("")
        self.sleep_timer_label.setStyleSheet("color:#8fd7c0;font-size:9pt;")
        self.sleep_timer_label.setVisible(False)
        content.addWidget(self.sleep_timer_label)
        root.addLayout(content, 1)

        accessible = (
            (self.artwork, "Album artwork", "Artwork for the current track"),
            (self.title_label, "Track title", "Current track title"),
            (self.artist_label, "Artist", "Current track artist"),
            (self.progress, "Playback position", "Seek within the current track"),
            (self.btn_previous, "Previous track", "Play the previous track"),
            (self.btn_play_pause, "Play or Pause", "Toggle playback"),
            (self.btn_stop, "Stop", "Stop playback"),
            (self.btn_next, "Next track", "Play the next track"),
            (self.volume, "Volume", "Playback volume from 0 to 100 percent"),
            (self.btn_mute, "Mute", "Mute or restore playback volume"),
            (self.btn_top, "Always on Top", "Keep the Mini Player above other windows"),
            (self.btn_full, "Return to Full Player", "Show the complete music library window"),
        )
        for widget, name, description in accessible:
            widget.setAccessibleName(name)
            widget.setAccessibleDescription(description)
        QtWidgets.QWidget.setTabOrder(self.progress, self.btn_previous)
        QtWidgets.QWidget.setTabOrder(self.btn_previous, self.btn_play_pause)
        QtWidgets.QWidget.setTabOrder(self.btn_play_pause, self.btn_stop)
        QtWidgets.QWidget.setTabOrder(self.btn_stop, self.btn_next)
        QtWidgets.QWidget.setTabOrder(self.btn_next, self.volume)
        QtWidgets.QWidget.setTabOrder(self.volume, self.btn_mute)
        QtWidgets.QWidget.setTabOrder(self.btn_mute, self.btn_top)
        QtWidgets.QWidget.setTabOrder(self.btn_top, self.btn_full)

    def _configure_shortcuts(self):
        shortcuts = (
            ("Ctrl+Space", self.owner.action_play_pause_alternate.trigger),
            ("Ctrl+Left", self.owner.action_previous.trigger),
            ("Ctrl+Right", self.owner.action_next.trigger),
            ("Ctrl+Up", self.owner.action_volume_up.trigger),
            ("Ctrl+Down", self.owner.action_volume_down.trigger),
            ("Ctrl+M", self.owner.action_mute.trigger),
            ("Ctrl+Shift+M", self.return_to_full_requested.emit),
            ("Escape", self.return_to_full_requested.emit),
        )
        self._shortcut_actions = []
        for sequence, callback in shortcuts:
            action = QtGui.QAction(self)
            action.setShortcut(QtGui.QKeySequence(sequence))
            action.setShortcutContext(QtCore.Qt.ShortcutContext.WindowShortcut)
            action.triggered.connect(callback)
            self.addAction(action)
            self._shortcut_actions.append(action)

    def _volume_changed(self, value):
        if not self.volume.signalsBlocked():
            self.owner.slider_volume.setValue(value)

    def _begin_seek(self):
        self._seeking = True
        self.owner.scrubbing = True

    def _finish_seek(self):
        selected_value = self.progress.value()
        try:
            self.owner.slider_progress.setValue(selected_value)
            self.owner._progress_release()
        finally:
            self._seeking = False

    def _set_always_on_top(self, enabled):
        self.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, bool(enabled))
        self.show()
        self.raise_()
        self.activateWindow()
        self.settings_changed.emit()

    def set_always_on_top(self, enabled):
        self.btn_top.blockSignals(True)
        self.btn_top.setChecked(bool(enabled))
        self.btn_top.blockSignals(False)
        self.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, bool(enabled))

    def restore_geometry_safely(self, geometry):
        width = max(self.minimumWidth(), min(self.maximumWidth(), int(geometry.get("width", 420))))
        height = max(self.minimumHeight(), min(self.maximumHeight(), int(geometry.get("height", 190))))
        x = int(geometry.get("x", 80))
        y = int(geometry.get("y", 80))
        target = QtCore.QRect(x, y, width, height)
        screens = QtGui.QGuiApplication.screens()
        if not any(screen.availableGeometry().intersects(target) for screen in screens):
            available = QtGui.QGuiApplication.primaryScreen().availableGeometry()
            target.moveCenter(available.center())
        self.setGeometry(target)

    def geometry_settings(self):
        rect = self.normalGeometry() if self.isMaximized() else self.geometry()
        return {"x": rect.x(), "y": rect.y(), "width": rect.width(), "height": rect.height()}

    def _cached_track_meta(self, path):
        meta = self.owner._meta_by_path.get(path) or {}
        title = str(meta.get("title") or os.path.splitext(os.path.basename(path or ""))[0] or "Ready")
        artist = str(meta.get("artist") or "")
        album = str(meta.get("album") or "")
        return meta, title, artist, album

    def _sync_artwork(self, path, meta):
        if (
            path == self._artwork_path
            and self.artwork.pixmap() is not None
            and not self.artwork.pixmap().isNull()
        ):
            return
        self._artwork_path = path
        provider = getattr(self.owner, "_cached_artwork_pixmap", None)
        if provider is not None:
            pixmap = provider(path, self.artwork.size())
            if (
                pixmap is not None
                and not pixmap.isNull()
                and path == self.owner.current_path
            ):
                self.artwork.setPixmap(
                    pixmap.scaled(
                        self.artwork.size(),
                        QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                        QtCore.Qt.TransformationMode.SmoothTransformation,
                    )
                )
                return
        artist = str(meta.get("album_artist") or meta.get("artist") or "")
        album = str(meta.get("album") or "")
        self.artwork.setPixmap(QtGui.QPixmap())
        self.artwork.setText("♪")
        if provider is not None and path and path == self.owner.current_path:
            self._load_current_artwork(path, f"{artist}::{album}")

    def _set_artwork_pixmap(self, pixmap):
        self.artwork.setText("")
        self.artwork.setPixmap(
            pixmap.scaled(
                self.artwork.size(),
                QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _load_current_artwork(self, path, cache_key):
        if self._artwork_load_path == path or self._closing:
            return
        self._artwork_load_path = path
        task = _ArtworkLoadTask(
            path,
            cache_key,
            (self.artwork.width(), self.artwork.height()),
        )
        self._artwork_tasks.add(task)

        generation = getattr(
            getattr(self.owner, "_now_playing_generation", None), "identity", None
        )
        generation = getattr(generation, "generation", None)

        def finished(loaded_path, loaded_key, data, artwork_task=task):
            self._artwork_tasks.discard(artwork_task)
            if self._artwork_load_path == loaded_path:
                self._artwork_load_path = None
            if self._closing:
                return
            current = getattr(getattr(self.owner, "_now_playing_generation", None), "identity", None)
            if generation is not None and getattr(current, "generation", None) != generation:
                return
            self._apply_loaded_artwork(loaded_path, loaded_key, data)

        task.signals.loaded.connect(finished)
        QtCore.QThreadPool.globalInstance().start(task)

    def _apply_loaded_artwork(self, path, cache_key, data):
        if path != self.owner.current_path:
            return
        if isinstance(data, QtGui.QImage):
            if data.isNull():
                return
            pixmap = QtGui.QPixmap.fromImage(data)
        else:
            # Compatibility for callers/tests; production workers return QImage.
            pixmap = QtGui.QPixmap()
            if not data or not pixmap.loadFromData(data):
                return
            self.owner.album_cover_cache[cache_key] = data
        if not pixmap.isNull():
            self._artwork_path = path
            self._set_artwork_pixmap(pixmap)

    def _next_preview(self):
        self.owner._ensure_queue_played_flags()
        for path, played in zip(self.owner.queue, self.owner.queue_played):
            if not played:
                _, title, artist, _ = self._cached_track_meta(path)
                return f"Next: {artist} – {title}" if artist else f"Next: {title}"
        return "Next: Library playback"

    def sync_from_owner(self, force=False):
        path = self.owner.current_path or ""
        meta, title, artist, album = self._cached_track_meta(path)
        if force or path != self._synced_path:
            self._synced_path = path
            self.title_label.setText(title)
            self.artist_label.setText(artist or "Unknown Artist")
            self.album_label.setText(album)
            self._sync_artwork(path, meta)
        if not self._seeking:
            self.progress.blockSignals(True)
            self.progress.setValue(self.owner.slider_progress.value())
            self.progress.setEnabled(bool(getattr(self.owner, "_last_progress_length_ms", 0)))
            self.progress.blockSignals(False)
        self.time_label.setText(self.owner.label_remaining.text())
        self.volume.blockSignals(True)
        self.volume.setValue(self.owner.master_volume)
        self.volume.blockSignals(False)
        paused = bool(self.owner._playback_intentionally_paused)
        active = bool(self.owner._playback_expected)
        self.btn_play_pause.setText("Play" if paused or not active else "Pause")
        self.btn_play_pause.setAccessibleName(self.btn_play_pause.text())
        muted = bool(self.owner._muted or self.owner.master_volume == 0)
        self.btn_mute.setText("Unmute" if muted else "Mute")
        self.btn_mute.setAccessibleName(self.btn_mute.text())
        self.next_label.setText(self._next_preview())
        sleep_text = ""
        status_text_fn = getattr(self.owner, "_sleep_timer_status_text", None)
        if status_text_fn is not None:
            sleep_text = status_text_fn()
        self.sleep_timer_label.setText(sleep_text)
        self.sleep_timer_label.setVisible(bool(sleep_text))

    def keyPressEvent(self, event):
        if (
            event.key() == QtCore.Qt.Key.Key_Space
            and event.modifiers() == QtCore.Qt.KeyboardModifier.NoModifier
        ):
            focused = QtWidgets.QApplication.focusWidget()
            if isinstance(
                focused,
                (QtWidgets.QAbstractButton, QtWidgets.QSlider, QtWidgets.QLineEdit),
            ):
                super().keyPressEvent(event)
                return
            self.owner.action_play_pause_alternate.trigger()
            event.accept()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event):
        self.album_label.setVisible(self.width() >= 420 and self.height() >= 190)
        super().resizeEvent(event)

    def closeEvent(self, event):
        if self._allow_close:
            self._closing = True
            event.accept()
            return
        event.ignore()
        self.return_to_full_requested.emit()
