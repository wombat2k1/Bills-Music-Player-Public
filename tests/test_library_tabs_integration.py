"""End-to-end Music/Videos tab tests against a real PlayerWindow instance.

Unlike most of this suite (which extracts individual unbound methods onto a
lightweight harness), these tests construct the actual PlayerWindow and pump
the Qt event loop, because the behaviour under test -- two tabs sharing one
chunked, timer-driven library-build pipeline without racing each other --
only exists as an emergent property of that real, staged startup sequence.
"""
import json
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets

from billsmusic.window import PlayerWindow

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _pump(seconds: float = 3.0):
    app = _app()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.002)


def _wait_until(predicate, seconds: float = 5.0):
    app = _app()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


def _seed_cache(localappdata_dir, meta):
    folder = os.path.join(localappdata_dir, "Bills Music Player")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "library_cache.json"), "w", encoding="utf-8") as f:
        json.dump({"schema_version": 3, "folders": [], "meta": meta}, f)


_MIXED_META = [
    {
        "path": "C:/Music/A/song1.mp3", "title": "Song1", "artist": "ArtistA",
        "album": "AlbumA", "album_artist": "ArtistA", "disc_no": 1, "track_no": 1,
        "media_type": "audio", "genre": "Unknown", "year": "Unknown",
    },
    {
        "path": "C:/Music/A/song2.mp3", "title": "Song2", "artist": "ArtistA",
        "album": "AlbumA", "album_artist": "ArtistA", "disc_no": 1, "track_no": 2,
        "media_type": "audio", "genre": "Unknown", "year": "Unknown",
    },
    {
        "path": "C:/Videos/V/clip1.mp4", "title": "Clip1", "artist": "Unknown Artist",
        "album": "Music Videos", "album_artist": "Unknown Artist", "disc_no": 1,
        "track_no": 0, "media_type": "video", "genre": "Unknown", "year": "Unknown",
    },
    {
        "path": "C:/Karaoke/K/song.cdg", "title": "Song", "artist": "Singer",
        "album": "Karaoke", "album_artist": "Singer", "disc_no": 1,
        "track_no": 0, "media_type": "karaoke", "genre": "Karaoke", "year": "Unknown",
        "karaoke_source_type": "loose", "karaoke_validation_state": "valid",
        "audio_companion_path": "C:/Karaoke/K/song.mp3",
    },
]


def _build_window(tmp_path, monkeypatch, meta=_MIXED_META):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _seed_cache(str(tmp_path), meta)
    _app()
    window = PlayerWindow()
    _wait_until(lambda: getattr(window, "_karaoke_tab", None) is not None
                and window._karaoke_tab.tracks)
    return window


def test_startup_restore_splits_all_three_media_types_into_separate_tabs(tmp_path, monkeypatch):
    window = _build_window(tmp_path, monkeypatch)
    try:
        assert window._music_tab.tracks == [
            "C:/Music/A/song1.mp3", "C:/Music/A/song2.mp3",
        ]
        assert window._video_tab.tracks == ["C:/Videos/V/clip1.mp4"]
        assert window._karaoke_tab.tracks == ["C:/Karaoke/K/song.cdg"]
        # One shared scan/cache restore, not two -- both tabs came from the
        # single seeded cache file, never a second filesystem walk.
        assert window._active_library_tab is window._music_tab
    finally:
        window.close()


def test_unsupported_extension_appears_in_neither_tab(tmp_path, monkeypatch):
    meta = _MIXED_META + [{
        "path": "C:/Random/notes.txt", "title": "notes", "artist": "Unknown Artist",
        "album": "Unknown Album", "album_artist": "Unknown Artist", "disc_no": 1,
        "track_no": 0, "media_type": "unsupported", "genre": "Unknown", "year": "Unknown",
    }]
    window = _build_window(tmp_path, monkeypatch, meta=meta)
    try:
        all_tracks = (
            set(window._music_tab.tracks)
            | set(window._video_tab.tracks)
            | set(window._karaoke_tab.tracks)
        )
        assert "C:/Random/notes.txt" not in all_tracks
    finally:
        window.close()


def test_switching_tabs_does_not_rescan_or_rebuild(tmp_path, monkeypatch):
    window = _build_window(tmp_path, monkeypatch)
    try:
        music_tracks_before = list(window._music_tab.tracks)
        video_tracks_before = list(window._video_tab.tracks)
        scan_started = []
        window._start_scan = lambda *a, **kw: scan_started.append(True)

        window.library_tabs.setCurrentIndex(1)
        _pump(0.2)
        assert window._active_library_tab is window._video_tab
        assert window.tree_tracks is window.tree_tracks_video
        assert window._video_tab.tracks == video_tracks_before

        window.library_tabs.setCurrentIndex(0)
        _pump(0.2)
        assert window._active_library_tab is window._music_tab
        assert window._music_tab.tracks == music_tracks_before
        assert not scan_started
    finally:
        window.close()


def test_switching_tabs_does_not_change_playback_context(tmp_path, monkeypatch):
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.play_index(0)
        frozen_context = list(window._playback_context_paths)
        assert frozen_context == window._music_tab.tracks
        current_path = window.current_path

        window.library_tabs.setCurrentIndex(1)
        _pump(0.2)
        assert window._playback_context_paths == frozen_context
        assert window.current_path == current_path
        # Fallback sequence still resolves from the frozen Music snapshot
        # even though the Videos tab is now the one on screen.
        assert window._playback_fallback_paths() == frozen_context

        window.library_tabs.setCurrentIndex(0)
        _pump(0.2)
    finally:
        window.close()


def test_search_generation_carries_tab_identity(tmp_path, monkeypatch):
    window = _build_window(tmp_path, monkeypatch)
    try:
        music_generation = window._music_tab._library_search_generation
        video_generation = window._video_tab._library_search_generation
        assert music_generation == window._library_search_generation

        window.library_tabs.setCurrentIndex(1)
        _pump(0.05)
        assert window._library_search_generation == video_generation
        assert window._library_search_index == window._video_tab._library_search_index
        for meta, _ in window._library_search_index:
            assert meta.get("media_type") == "video"

        window.library_tabs.setCurrentIndex(0)
        _pump(0.05)
        assert window._library_search_index == window._music_tab._library_search_index
        for meta, _ in window._library_search_index:
            assert meta.get("media_type") != "video"
    finally:
        window.close()
