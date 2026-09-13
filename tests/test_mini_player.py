import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtGui, QtWidgets

from billsmusic.mini_player import MiniPlayerWindow
from billsmusic.window import PlayerWindow


_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


class OwnerHarness(QtWidgets.QMainWindow):
    _show_mini_player = PlayerWindow._show_mini_player
    _restore_full_player = PlayerWindow._restore_full_player
    _save_mini_player_geometry = PlayerWindow._save_mini_player_geometry
    _sync_mini_player = PlayerWindow._sync_mini_player
    _ensure_queue_played_flags = PlayerWindow._ensure_queue_played_flags

    def __init__(self):
        super().__init__()
        self.setCentralWidget(QtWidgets.QWidget())
        self.calls = []
        self.action_previous = self._action("previous")
        self.action_play_pause_alternate = self._action("play_pause")
        self.action_stop = self._action("stop")
        self.action_next = self._action("next")
        self.action_mute = self._action("mute")
        self.action_volume_up = self._action("volume_up")
        self.action_volume_down = self._action("volume_down")
        self.slider_volume = QtWidgets.QSlider()
        self.slider_volume.setRange(0, 100)
        self.slider_volume.setValue(70)
        self.slider_progress = QtWidgets.QSlider()
        self.slider_progress.setRange(0, 1000)
        self.label_remaining = QtWidgets.QLabel("-2:00")
        self.scrubbing = False
        self.current_path = "C:/Music/track.flac"
        self._meta_by_path = {
            self.current_path: {
                "title": "Current Track", "artist": "Current Artist",
                "album": "Current Album", "album_artist": "Current Artist",
            },
            "C:/Music/next.flac": {
                "title": "Next Track", "artist": "Next Artist",
            },
        }
        self.album_cover_cache = {}
        self.queue = ["C:/Music/played.flac", "C:/Music/next.flac"]
        self.queue_played = [True, False]
        self.master_volume = 70
        self._muted = False
        self._playback_expected = True
        self._playback_intentionally_paused = False
        self._last_progress_length_ms = 180000
        self.mini_player = None
        self.mini_player_geometry = {"x": 80, "y": 80, "width": 420, "height": 190}
        self.mini_player_always_on_top = False
        self.saved = 0
        self.seeked = 0

    def _action(self, name):
        action = QtGui.QAction(self)
        action.triggered.connect(lambda checked=False, n=name: self.calls.append(n))
        return action

    def _save_user_settings(self):
        self.saved += 1

    def _progress_release(self):
        self.scrubbing = False
        self.seeked += 1


def test_opening_reuses_one_window_and_hides_main_without_playback_command():
    app = _app()
    owner = OwnerHarness()
    owner.show()
    owner._show_mini_player()
    first = owner.mini_player
    app.processEvents()
    assert first.isVisible()
    assert not owner.isVisible()
    assert owner.calls == []

    owner._show_mini_player()
    assert owner.mini_player is first


def test_return_to_full_player_preserves_state():
    app = _app()
    owner = OwnerHarness()
    owner._show_mini_player()
    current_path = owner.current_path
    queue_object = owner.queue
    owner._restore_full_player()
    app.processEvents()
    assert owner.isVisible()
    assert not owner.mini_player.isVisible()
    assert owner.current_path == current_path
    assert owner.queue is queue_object


def test_controls_delegate_to_existing_actions():
    _app()
    owner = OwnerHarness()
    mini = MiniPlayerWindow(owner)
    mini.btn_previous.click()
    mini.btn_play_pause.click()
    mini.btn_stop.click()
    mini.btn_next.click()
    mini.btn_mute.click()
    assert owner.calls == ["previous", "play_pause", "stop", "next", "mute"]


def test_state_progress_volume_and_queue_preview_sync():
    _app()
    owner = OwnerHarness()
    mini = MiniPlayerWindow(owner)
    owner.slider_progress.setValue(375)
    owner.master_volume = 55
    mini.sync_from_owner(force=True)
    assert mini.title_label._full_text == "Current Track"
    assert mini.artist_label._full_text == "Current Artist"
    assert mini.album_label._full_text == "Current Album"
    assert mini.progress.value() == 375
    assert mini.volume.value() == 55
    assert mini.next_label._full_text == "Next: Next Artist – Next Track"

    mini.progress.setValue(500)
    mini._finish_seek()
    assert owner.slider_progress.value() == 500
    assert owner.seeked == 1


def test_long_title_and_artist_scroll_back_and_forth():
    _app()
    owner = OwnerHarness()
    mini = MiniPlayerWindow(owner)
    for label in (mini.title_label, mini.artist_label):
        label.resize(70, label.height())
        label.setText("A deliberately long piece of Mini Player text")
        label._edge_pause_ticks = 0
        label._tick()
        assert label._offset > 0
        label._offset = float(
            label.fontMetrics().horizontalAdvance(label._full_text)
            - label.width()
        )
        label._direction = 1
        label._edge_pause_ticks = 0
        label._tick()
        assert label._direction == -1


def test_progress_sync_does_not_fight_mouse_drag():
    _app()
    owner = OwnerHarness()
    mini = MiniPlayerWindow(owner)
    owner.slider_progress.setValue(100)
    mini._begin_seek()
    mini.progress.setValue(700)

    mini.sync_from_owner()

    assert mini.progress.value() == 700
    assert owner.scrubbing
    mini._finish_seek()
    assert owner.slider_progress.value() == 700
    assert owner.seeked == 1
    assert not mini._seeking


def test_unknown_duration_disables_seeking_and_empty_queue_uses_library():
    _app()
    owner = OwnerHarness()
    owner._last_progress_length_ms = 0
    owner.queue = []
    owner.queue_played = []
    mini = MiniPlayerWindow(owner)
    mini.sync_from_owner(force=True)
    assert not mini.progress.isEnabled()
    assert mini.next_label._full_text == "Next: Library playback"


def test_existing_main_player_artwork_provider_is_used():
    _app()
    owner = OwnerHarness()
    pixmap = QtGui.QPixmap(32, 32)
    pixmap.fill(QtGui.QColor("#ff3cac"))
    calls = []

    def cached_artwork(path, size):
        calls.append((path, size))
        return pixmap

    owner._cached_artwork_pixmap = cached_artwork
    mini = MiniPlayerWindow(owner)
    mini.sync_from_owner(force=True)

    assert calls
    assert calls[-1][0] == owner.current_path
    assert mini.artwork.pixmap() is not None
    assert not mini.artwork.pixmap().isNull()
    assert mini.artwork.pixmap().size() == mini.artwork.size()


def test_background_artwork_result_is_independent_of_library_cover_visibility():
    _app()
    owner = OwnerHarness()
    owner.album_covers_enabled = False
    mini = MiniPlayerWindow(owner)
    source = QtGui.QPixmap(24, 24)
    source.fill(QtGui.QColor("#21e6ff"))
    byte_array = QtCore.QByteArray()
    buffer = QtCore.QBuffer(byte_array)
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    source.save(buffer, "PNG")

    mini._apply_loaded_artwork(
        owner.current_path, "Current Artist::Current Album", bytes(byte_array)
    )

    assert owner.album_cover_cache["Current Artist::Current Album"]
    assert mini.artwork.pixmap() is not None
    assert mini.artwork.pixmap().size() == mini.artwork.size()


def test_invalid_geometry_is_moved_to_a_visible_screen_and_accessibility_is_set():
    _app()
    owner = OwnerHarness()
    mini = MiniPlayerWindow(owner)
    mini.restore_geometry_safely(
        {"x": -100000, "y": -100000, "width": 5000, "height": 20}
    )
    assert any(
        screen.availableGeometry().intersects(mini.geometry())
        for screen in QtGui.QGuiApplication.screens()
    )
    assert mini.width() == mini.maximumWidth()
    assert mini.height() == mini.minimumHeight()
    for widget in (
        mini.artwork, mini.title_label, mini.artist_label, mini.progress,
        mini.btn_previous, mini.btn_play_pause, mini.btn_stop, mini.btn_next,
        mini.volume, mini.btn_mute, mini.btn_top, mini.btn_full,
    ):
        assert widget.accessibleName()


def test_close_returns_to_full_player_instead_of_exiting():
    app = _app()
    owner = OwnerHarness()
    owner._show_mini_player()
    owner.mini_player.close()
    app.processEvents()
    assert owner.isVisible()
    assert not owner.mini_player.isVisible()
