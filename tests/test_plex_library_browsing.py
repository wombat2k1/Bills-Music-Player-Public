"""Plex Stage 2: window-layer queue/UI integration for browsed Plex
content -- the seams that reuse existing local-library machinery but must
never let a plex:// synthetic identity reach Mutagen/os.stat/local-cache
mutation. Lightweight harnesses (bound real PlayerWindow methods on a
minimal stand-in object), matching this suite's established
QueueCacheHarness/QueueHarness pattern in test_artwork_and_queue_updates.py
-- no real PlayerWindow needed for these, since the methods under test
don't touch startup/worker-thread lifecycle. One consolidated real-
PlayerWindow test (test_plex_tab_loading_empty_error_offline_and_source_switch_races)
covers the tab-status-state-machine end to end, per this suite's own
established "few large real-window tests, not one window per assertion"
convention (see test_plex_preferences_ui.py's docstring).
"""
import dataclasses
import json
import os
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets

import billsmusic.window as window_module
import billsmusic.artwork as artwork_module
from billsmusic.artwork import ArtworkManager
from billsmusic.library_search import build_search_index, filter_search_index, group_library_albums, group_row_label
from billsmusic.plex_identity import make_plex_identity
from billsmusic.plex_metadata import (
    plex_meta_list_to_queue_detail_cache,
    plex_track_to_meta_dict,
    plex_video_to_meta_dict,
)
from billsmusic.plex_preferences import PlexPreferences
from billsmusic.window import PlayerWindow


_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _pump(seconds=0.3):
    app = _app()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


def _build_real_window(tmp_path, monkeypatch, tag=""):
    localappdata = tmp_path / f"appdata{tag}"
    localappdata.mkdir(exist_ok=True)
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    _app()
    window = PlayerWindow()
    window.resize(1000, 800)
    window.show()
    _pump(0.3)
    return window


def _close_real_window(window):
    window.close()
    _pump(0.2)


def _seed_local_library_cache(localappdata_dir, meta):
    folder = os.path.join(localappdata_dir, "Bills Music Player")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "library_cache.json"), "w", encoding="utf-8") as f:
        json.dump({"schema_version": 3, "folders": [], "meta": meta}, f)


def _seed_config(localappdata_dir, **overrides):
    folder = os.path.join(localappdata_dir, "Bills Music Player")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "config.json"), "w", encoding="utf-8") as f:
        json.dump(overrides, f)


def _fake_local_meta(count=50, albums=5):
    return [
        {
            "path": f"X:/music/artist{i % albums}/album{i % albums}/track{i}.flac",
            "title": f"Track {i}", "artist": f"Artist {i % albums}",
            "album": f"Album {i % albums}", "album_artist": f"Artist {i % albums}",
            "genre": "Rock", "year": "2001", "track_number": i, "disc_number": 1,
            "media_type": "audio",
        }
        for i in range(count)
    ]


def _tree_status_text(tree):
    """The single non-selectable status row's text, or None if the tree
    holds anything else (multiple rows, a real selectable item, nothing)."""
    if tree.topLevelItemCount() != 1:
        return None
    item = tree.topLevelItem(0)
    if item.flags() & QtCore.Qt.ItemFlag.ItemIsSelectable:
        return None
    return item.text(0)


def _wait_until(predicate, timeout=2.0):
    import time as _time
    deadline = _time.monotonic() + timeout
    while not predicate() and _time.monotonic() < deadline:
        _app().processEvents()
        _time.sleep(0.005)
    assert predicate()


PLEX_PATH = make_plex_identity("server-abc", "4242", ".mp3")


class _Diagnostics:
    def record(self, *args, **kwargs):
        pass


class PlexQueueDetailsHarness:
    _cached_queue_analysis = PlayerWindow._cached_queue_analysis
    _queue_track_details = PlayerWindow._queue_track_details
    _file_signature = PlayerWindow._file_signature
    _request_queue_analysis = PlayerWindow._request_queue_analysis

    def __init__(self):
        self.queue_analysis_cache = {}
        self.queue_detail_cache = {}
        self._plex_item_updated_at = {}
        self.queue_analysis_pending = set()
        self.queue_bpm_key_pending_fields = {}
        self.queue_analysis_worker = None
        self.diagnostics = _Diagnostics()


def test_uncached_plex_track_details_never_touch_mutagen_or_stat(monkeypatch):
    # Both MutagenFile and os.stat are called inside this method's own
    # broad `except Exception: pass` -- a raising poison-pill would be
    # silently swallowed and prove nothing, so this records calls instead.
    harness = PlexQueueDetailsHarness()
    mutagen_calls = []
    stat_calls = []
    monkeypatch.setattr(
        window_module, "MutagenFile",
        lambda *a, **k: mutagen_calls.append((a, k)),
    )
    monkeypatch.setattr(
        window_module.os, "stat",
        lambda *a, **k: stat_calls.append((a, k)) or os.stat_result((0,) * 10),
    )

    details = harness._queue_track_details(PLEX_PATH)

    assert details == {"bitrate": "--", "time": "--", "key": "--", "bpm": "--"}
    assert mutagen_calls == []
    assert stat_calls == []
    # A real local file would have been enqueued for background BPM/Key
    # analysis by this point; a Plex identity must not be.
    assert harness.queue_analysis_pending == set()


def test_uncached_plex_track_details_fall_back_to_persisted_analysis_cache():
    harness = PlexQueueDetailsHarness()
    harness._plex_item_updated_at[PLEX_PATH] = 555
    harness.queue_analysis_cache[PLEX_PATH] = {
        "mtime": 555, "size": 0,
        "result": {"time": "3:21", "bitrate": "--", "key": "Am", "bpm": "120"},
    }

    details = harness._queue_track_details(PLEX_PATH)

    assert details == {"time": "3:21", "bitrate": "--", "key": "Am", "bpm": "120"}


def test_cached_plex_track_details_short_circuit_on_first_line():
    # The ordinary path: an already-fetched Plex library populated
    # queue_detail_cache directly (see plex_meta_list_to_queue_detail_cache
    # in workers.py's PlexLibraryFetchWorker) -- _queue_track_details must
    # return it verbatim without re-deriving anything.
    harness = PlexQueueDetailsHarness()
    harness.queue_detail_cache[PLEX_PATH] = {
        "time": "4:00", "bitrate": "320k", "key": "--", "bpm": "--",
    }

    details = harness._queue_track_details(PLEX_PATH)

    assert details == {"time": "4:00", "bitrate": "320k", "key": "--", "bpm": "--"}


class PlexMetadataOnlyHarness:
    _request_metadata_only_queue_analysis = (
        PlayerWindow._request_metadata_only_queue_analysis
    )

    def __init__(self):
        self._queue_priority_deferred_paths = set()

        class _Worker:
            def __init__(self):
                self.requested = []

            def request_metadata(self, path):
                self.requested.append(path)

        self.queue_analysis_worker = _Worker()


def test_metadata_only_analysis_skips_plex_identities():
    harness = PlexMetadataOnlyHarness()
    details = {"time": "--", "bitrate": "--", "key": "--", "bpm": "--"}

    harness._request_metadata_only_queue_analysis(PLEX_PATH, details)

    assert harness.queue_analysis_worker.requested == []
    assert harness._queue_priority_deferred_paths == set()


def test_metadata_only_analysis_still_dispatches_for_local_paths():
    harness = PlexMetadataOnlyHarness()
    details = {"time": "--", "bitrate": "--", "key": "--", "bpm": "--"}

    harness._request_metadata_only_queue_analysis("X:/music/track.flac", details)

    assert harness.queue_analysis_worker.requested == ["X:/music/track.flac"]
    assert harness._queue_priority_deferred_paths == {"X:/music/track.flac"}


class _AssertNotCalled:
    def __init__(self, name):
        self.name = name

    def __call__(self, *args, **kwargs):
        raise AssertionError(f"{self.name} must not be called for Plex-sourced items")


class TreeMenuHarness(QtWidgets.QWidget):
    _show_tree_menu = PlayerWindow._show_tree_menu
    _handle_album_group_queue_action = PlayerWindow._handle_album_group_queue_action
    _album_paths_from_data = PlayerWindow._album_paths_from_data
    _artist_paths_from_data = PlayerWindow._artist_paths_from_data

    def __init__(self):
        super().__init__()
        self.tree_tracks = QtWidgets.QTreeWidget()
        self._meta_list = []
        self.calls = []

        def record(name):
            def _fn(*args, **kwargs):
                self.calls.append((name, args, kwargs))
            return _fn

        self._add_window_menu_options = lambda menu: None
        self._queue_play_next = record("_queue_play_next")
        self._add_to_queue_with_dedup_guard = record("_add_to_queue_with_dedup_guard")
        self.play_path = record("play_path")
        self._insert_unplayed_queue_item = record("_insert_unplayed_queue_item")
        self._refresh_queue_list = record("_refresh_queue_list")
        self._request_queue_analysis_for_paths = record(
            "_request_queue_analysis_for_paths"
        )
        self._schedule_session_save = record("_schedule_session_save")

        # Local-only actions that a Plex-sourced menu must never reach.
        self._play_and_log = _AssertNotCalled("_play_and_log")
        self._add_normalisation_menu = _AssertNotCalled("_add_normalisation_menu")
        self._handle_normalisation_action = _AssertNotCalled(
            "_handle_normalisation_action"
        )
        self._remove_library_paths = _AssertNotCalled("_remove_library_paths")
        self._add_library_admin_menu = _AssertNotCalled("_add_library_admin_menu")
        self._handle_library_admin = _AssertNotCalled("_handle_library_admin")
        self._refresh_album_tags = _AssertNotCalled("_refresh_album_tags")
        self._show_album_cover = _AssertNotCalled("_show_album_cover")
        self._show_all_album_covers = _AssertNotCalled("_show_all_album_covers")
        self._hide_all_album_covers = _AssertNotCalled("_hide_all_album_covers")
        self._fetch_album_art_online = _AssertNotCalled("_fetch_album_art_online")


def _patch_menu_exec(monkeypatch, select_text):
    """Replaces QMenu.exec for the duration of one call: records the
    offered action texts and returns the action matching select_text (or
    None, simulating a dismissed menu, if select_text is None)."""
    offered = []
    orig_exec = QtWidgets.QMenu.exec

    def _fake_exec(self, *args, **kwargs):
        offered.append([action.text() for action in self.actions() if not action.isSeparator()])
        if select_text is None:
            return None
        for action in self.actions():
            if action.text() == select_text:
                return action
        return None

    monkeypatch.setattr(QtWidgets.QMenu, "exec", _fake_exec)
    return offered


def test_plex_track_context_menu_offers_only_queue_actions(monkeypatch):
    _app()
    harness = TreeMenuHarness()
    item = QtWidgets.QTreeWidgetItem(["Track"])
    item.setData(0, QtCore.Qt.ItemDataRole.UserRole, PLEX_PATH)
    harness.tree_tracks.addTopLevelItem(item)
    harness.tree_tracks.setCurrentItem(item)
    offered = _patch_menu_exec(monkeypatch, "Add to Queue")

    harness._show_tree_menu(harness.tree_tracks.visualItemRect(item).center())

    assert offered == [["Play Next", "Add to Queue"]]
    assert harness.calls == [
        ("_add_to_queue_with_dedup_guard", ([PLEX_PATH],), {})
    ]


def test_plex_track_context_menu_dismissed_does_nothing(monkeypatch):
    # Regression: with local-only actions built as unconditional
    # QAction/None sentinels elsewhere in this menu, a dismissed menu
    # (action is None) must not spuriously match anything.
    _app()
    harness = TreeMenuHarness()
    item = QtWidgets.QTreeWidgetItem(["Track"])
    item.setData(0, QtCore.Qt.ItemDataRole.UserRole, PLEX_PATH)
    harness.tree_tracks.addTopLevelItem(item)
    harness.tree_tracks.setCurrentItem(item)
    _patch_menu_exec(monkeypatch, None)

    harness._show_tree_menu(harness.tree_tracks.visualItemRect(item).center())

    assert harness.calls == []


def test_plex_album_context_menu_offers_only_queue_actions(monkeypatch):
    _app()
    harness = TreeMenuHarness()
    album_item = QtWidgets.QTreeWidgetItem(["Album"])
    album_data = {
        "album": "Greatest Hits",
        "artist": "Some Artist",
        "items": [
            (None, None, None, None, None, PLEX_PATH),
        ],
    }
    album_item.setData(0, QtCore.Qt.ItemDataRole.UserRole, album_data)
    harness.tree_tracks.addTopLevelItem(album_item)
    harness.tree_tracks.setCurrentItem(album_item)
    offered = _patch_menu_exec(monkeypatch, "Add Album to Up Next")

    harness._show_tree_menu(harness.tree_tracks.visualItemRect(album_item).center())

    assert offered == [[
        "Play Album", "Play Album Next", "Add Album to Up Next",
        "Play Artist Next", "Add Artist's Tracks to Up Next",
    ]]
    assert harness.calls == [
        ("_add_to_queue_with_dedup_guard", ([PLEX_PATH],), {})
    ]


def test_plex_album_context_menu_dismissed_does_nothing(monkeypatch):
    _app()
    harness = TreeMenuHarness()
    album_item = QtWidgets.QTreeWidgetItem(["Album"])
    album_data = {
        "album": "Greatest Hits",
        "artist": "Some Artist",
        "items": [
            (None, None, None, None, None, PLEX_PATH),
        ],
    }
    album_item.setData(0, QtCore.Qt.ItemDataRole.UserRole, album_data)
    harness.tree_tracks.addTopLevelItem(album_item)
    harness.tree_tracks.setCurrentItem(album_item)
    _patch_menu_exec(monkeypatch, None)

    harness._show_tree_menu(harness.tree_tracks.visualItemRect(album_item).center())

    assert harness.calls == []


def test_plex_track_metadata_is_searchable_via_the_shared_library_search_index():
    # Stage 2 item: Plex search must work by reusing build_search_index/
    # filter_search_index exactly as the Local library already does --
    # no parallel Plex-specific search implementation. Proven directly
    # against plex_track_to_meta_dict's real output shape.
    raw_item = {
        "ratingKey": "4242",
        "title": "Dancing Queen",
        "grandparentTitle": "ABBA",
        "parentTitle": "Arrival",
        "duration": 230000,
    }
    meta = plex_track_to_meta_dict(raw_item, "server-abc")
    index = build_search_index([meta])

    assert filter_search_index(index, "dancing queen") == [meta]
    assert filter_search_index(index, "abba") == [meta]
    assert filter_search_index(index, "arrival") == [meta]
    assert filter_search_index(index, "no such track") == []


def test_local_track_context_menu_is_unchanged_by_plex_awareness(monkeypatch):
    # Hard regression boundary: a local (non-Plex) track's menu must still
    # offer every action it always has, unfiltered.
    _app()
    harness = TreeMenuHarness()
    harness._play_and_log = lambda *a, **k: harness.calls.append(("_play_and_log", a, k))
    harness._add_normalisation_menu = lambda menu, paths: {}
    harness._handle_normalisation_action = lambda action, actions, paths: False
    harness._add_library_admin_menu = lambda menu, track_path=None: ()
    harness._handle_library_admin = lambda action, refs, track_path=None: None
    item = QtWidgets.QTreeWidgetItem(["Track"])
    item.setData(0, QtCore.Qt.ItemDataRole.UserRole, "X:/music/local.flac")
    harness.tree_tracks.addTopLevelItem(item)
    harness.tree_tracks.setCurrentItem(item)
    offered = _patch_menu_exec(monkeypatch, "Add to Queue")

    harness._show_tree_menu(harness.tree_tracks.visualItemRect(item).center())

    assert offered[0][:2] == ["Play Next", "Add to Queue"]
    assert "Log visualiser data" in offered[0]
    assert "Remove This Track From Library" in offered[0]
    assert harness.calls == [
        ("_add_to_queue_with_dedup_guard", (["X:/music/local.flac"],), {})
    ]


class PlexThumbSourceHarness:
    _plex_effective_connection = PlayerWindow._plex_effective_connection
    _plex_thumb_http_source = PlayerWindow._plex_thumb_http_source

    def __init__(self, prefs=None, connection_uri="", access_token=""):
        self.plex_preferences = prefs or PlexPreferences()
        self._plex_active_connection_uri = connection_uri
        self._plex_active_access_token = access_token


def test_plex_thumb_http_source_builds_url_with_header_token_never_query_string():
    harness = PlexThumbSourceHarness(
        connection_uri="http://192.168.1.50:32400", access_token="secret-server-token",
    )

    result = harness._plex_thumb_http_source("/library/metadata/9/thumb/1")

    assert result == (
        "http://192.168.1.50:32400/library/metadata/9/thumb/1",
        {"X-Plex-Token": "secret-server-token"},
    )
    # The token must appear only in the header dict, never in the URL.
    assert "secret-server-token" not in result[0]


def test_plex_thumb_http_source_returns_none_when_not_connected():
    harness = PlexThumbSourceHarness()  # no connection resolved yet

    assert harness._plex_thumb_http_source("/library/metadata/9/thumb/1") is None


def test_plex_thumb_http_source_returns_none_for_empty_thumb():
    harness = PlexThumbSourceHarness(
        connection_uri="http://192.168.1.50:32400", access_token="secret",
    )

    assert harness._plex_thumb_http_source("") is None


def test_plex_thumb_http_source_prefers_manual_server_when_advanced_mode_is_on():
    prefs = PlexPreferences(
        use_manual_server=True, server_address="192.168.1.99:32400", token="manual-token",
    )
    harness = PlexThumbSourceHarness(
        prefs=prefs, connection_uri="http://ignored:32400", access_token="ignored",
    )

    result = harness._plex_thumb_http_source("/library/metadata/9/thumb/1")

    assert result[0] == "http://192.168.1.99:32400/library/metadata/9/thumb/1"
    assert result[1] == {"X-Plex-Token": "manual-token"}


class PlexAlbumArtworkHarness(QtWidgets.QWidget):
    _plex_effective_connection = PlayerWindow._plex_effective_connection
    _plex_thumb_http_source = PlayerWindow._plex_thumb_http_source
    _request_album_artwork = PlayerWindow._request_album_artwork

    def __init__(self, connected=True):
        super().__init__()
        self.plex_preferences = PlexPreferences()
        self._plex_active_connection_uri = "http://192.168.1.50:32400" if connected else ""
        self._plex_active_access_token = "secret-token" if connected else ""
        self._library_search_generation = 1
        self.album_item_by_key = {}
        self._meta_by_path = {}
        self.artwork_manager = ArtworkManager()
        self.diagnostics = _Diagnostics()

    def shutdown(self):
        self.artwork_manager.shutdown()


def test_plex_album_artwork_requests_via_http_never_local_read(monkeypatch):
    _app()
    harness = PlexAlbumArtworkHarness()
    plex_path = make_plex_identity("server-xyz", "77", ".mp3")
    harness._meta_by_path[plex_path] = {"thumb": "/library/metadata/77/thumb/9"}
    album_item = QtWidgets.QTreeWidgetItem(["Greatest Hits"])
    harness.album_item_by_key["k"] = album_item

    read_cover_calls = []
    monkeypatch.setattr(
        artwork_module, "read_cover_bytes",
        lambda path: read_cover_calls.append(path) or None,
    )
    fetch_calls = []

    def fake_get(url, headers=None, timeout=None):
        fetch_calls.append((url, headers))
        class _Resp:
            status_code = 200
            content = _png_bytes()
        return _Resp()

    monkeypatch.setattr("requests.get", fake_get)

    ok = harness._request_album_artwork(
        album_item, "Greatest Hits", "ABBA",
        [(None, None, None, None, None, plex_path)], "k",
    )
    assert ok
    _wait_until(lambda: bool(fetch_calls))

    assert read_cover_calls == []
    assert fetch_calls == [
        (
            "http://192.168.1.50:32400/library/metadata/77/thumb/9",
            {"X-Plex-Token": "secret-token"},
        )
    ]
    harness.shutdown()


def test_plex_album_artwork_skips_request_when_plex_not_connected():
    _app()
    harness = PlexAlbumArtworkHarness(connected=False)
    plex_path = make_plex_identity("server-xyz", "77", ".mp3")
    harness._meta_by_path[plex_path] = {"thumb": "/library/metadata/77/thumb/9"}
    album_item = QtWidgets.QTreeWidgetItem(["Greatest Hits"])
    harness.album_item_by_key["k"] = album_item

    ok = harness._request_album_artwork(
        album_item, "Greatest Hits", "ABBA",
        [(None, None, None, None, None, plex_path)], "k",
    )

    assert ok is False
    assert harness.artwork_manager.pending_count == 0
    harness.shutdown()


def _png_bytes():
    from PyQt6 import QtGui
    image = QtGui.QImage(4, 4, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor("blue"))
    data = QtCore.QByteArray()
    buffer = QtCore.QBuffer(data)
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(data)


def test_artwork_manager_falls_back_to_http_source_after_local_misses(monkeypatch):
    # artwork.py-layer proof, independent of window.py: disk_path/paths
    # both miss, http_source is tried third, token travels as a header.
    _app()
    monkeypatch.setattr(artwork_module, "read_cover_bytes", lambda path: None)
    fetch_calls = []

    def fake_get(url, headers=None, timeout=None):
        fetch_calls.append((url, headers))
        class _Resp:
            status_code = 200
            content = _png_bytes()
        return _Resp()

    monkeypatch.setattr("requests.get", fake_get)
    manager = ArtworkManager(max_pending=4, max_cache=4)
    results = []
    manager.request(
        "plex::server-xyz::/library/metadata/77/thumb/9",
        [], None, (40, 40), 1, results.append,
        http_source=("http://host:32400/library/metadata/77/thumb/9", {"X-Plex-Token": "tok"}),
    )
    _wait_until(lambda: len(results) == 1)

    assert fetch_calls == [
        ("http://host:32400/library/metadata/77/thumb/9", {"X-Plex-Token": "tok"})
    ]
    assert results[0].source == "plex-thumb"
    assert not results[0].image.isNull()
    manager.shutdown()


def test_artwork_manager_never_calls_http_source_when_local_disk_hits(monkeypatch, tmp_path):
    _app()
    cover_path = tmp_path / "cover.png"
    cover_path.write_bytes(_png_bytes())
    fetch_calls = []
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: fetch_calls.append((a, k)) or (_ for _ in ()).throw(
            AssertionError("must not fetch over HTTP when disk_path already hit")
        ),
    )
    manager = ArtworkManager(max_pending=4, max_cache=4)
    results = []
    manager.request(
        "local::album", [], str(cover_path), (40, 40), 1, results.append,
        http_source=("http://host/should-not-be-used", {"X-Plex-Token": "tok"}),
    )
    _wait_until(lambda: len(results) == 1)

    assert fetch_calls == []
    assert results[0].source == "disk-thumbnail"
    manager.shutdown()


class _FakeStatusBar:
    def __init__(self):
        self.messages = []

    def showMessage(self, text, timeout=0):
        self.messages.append(text)


class PlexFetchResultHarness:
    _on_plex_library_fetch_result = PlayerWindow._on_plex_library_fetch_result
    _plex_tree_for_kind = PlayerWindow._plex_tree_for_kind
    _set_plex_tab_status_row = PlayerWindow._set_plex_tab_status_row
    _PLEX_CATEGORY_LABEL = PlayerWindow._PLEX_CATEGORY_LABEL
    _PLEX_LOADED_NOUN = PlayerWindow._PLEX_LOADED_NOUN

    def _record_library_source_backing_state(self, context):
        pass

    def __init__(self, generation=2, library_source="plex"):
        _app()
        self._closing = False
        self._plex_refresh_in_flight = {"music"}
        self._plex_fetch_generation = generation
        self._plex_meta_by_kind = {"music": [], "video": [], "karaoke": []}
        self._plex_meta_by_path = {}
        self._plex_item_updated_at = {}
        self._plex_fetched_kinds = set()
        self._plex_fetch_error = {}
        self.queue_detail_cache = {}
        self.library_source = library_source
        self.diagnostics = _Diagnostics()
        self.show_calls = 0
        self.tree_tracks = QtWidgets.QTreeWidget()
        self.tree_tracks_video = QtWidgets.QTreeWidget()
        self.tree_tracks_karaoke = QtWidgets.QTreeWidget()
        self._status_bar = _FakeStatusBar()

    def statusBar(self):
        return self._status_bar

    def _show_plex_library_view(self):
        self.show_calls += 1

    def _save_plex_library_cache(self):
        self.save_cache_calls = getattr(self, "save_cache_calls", 0) + 1


def _fake_music_result(generation, path="plex://server-x/1.mp3"):
    return {
        "success": True, "media_kind": "music", "generation": generation,
        "meta_list": [{"path": path, "title": "Track", "updated_at": 99}],
    }


def test_stale_generation_fetch_result_is_discarded():
    # Item 15: source/server/mapping can change while a fetch is still in
    # flight (e.g. the user hit Refresh again, or switched servers) --
    # a result carrying an old generation must never overwrite newer state.
    harness = PlexFetchResultHarness(generation=2)

    harness._on_plex_library_fetch_result(_fake_music_result(generation=1))

    assert harness._plex_meta_by_kind["music"] == []
    assert harness.queue_detail_cache == {}
    assert harness.show_calls == 0
    # The in-flight marker is still cleared even for a discarded result --
    # otherwise _start_plex_library_fetch's coalescing guard would wedge.
    assert "music" not in harness._plex_refresh_in_flight


def test_current_generation_fetch_result_is_applied():
    harness = PlexFetchResultHarness(generation=2)

    harness._on_plex_library_fetch_result(_fake_music_result(generation=2))

    assert len(harness._plex_meta_by_kind["music"]) == 1
    assert "plex://server-x/1.mp3" in harness.queue_detail_cache
    assert harness._plex_item_updated_at["plex://server-x/1.mp3"] == 99
    assert harness.show_calls == 1


def test_failed_refresh_never_destroys_existing_good_plex_data():
    # Real-device follow-up item 10: a failed/timed-out refresh must
    # retain whatever Plex data was already cached, show a short safe
    # error, and never replace a good list with empty data.
    harness = PlexFetchResultHarness(generation=2)
    existing = [{"path": "plex://server-x/1.mp3", "title": "Existing Track"}]
    harness._plex_meta_by_kind["music"] = existing
    harness._plex_fetched_kinds.add("music")

    harness._on_plex_library_fetch_result({
        "success": False, "reason": "Connection timed out",
        "media_kind": "music", "generation": 2,
    })

    assert harness._plex_meta_by_kind["music"] == existing  # untouched
    assert harness._plex_fetch_error["music"] == "Connection timed out"
    # The error goes to the status bar (existing data stays visible),
    # never replacing the tree with an error row.
    assert harness._status_bar.messages == [
        "Unable to refresh Plex Music: Connection timed out",
    ]
    assert _tree_status_text(harness.tree_tracks) is None


def test_failed_first_load_with_no_prior_data_shows_an_in_tree_error():
    harness = PlexFetchResultHarness(generation=2)

    harness._on_plex_library_fetch_result({
        "success": False, "reason": "Server unreachable",
        "media_kind": "music", "generation": 2,
    })

    assert harness._plex_meta_by_kind["music"] == []
    assert _tree_status_text(harness.tree_tracks) == (
        "Unable to load Plex Music. Server unreachable"
    )
    assert harness._status_bar.messages == []


def test_fetch_result_while_viewing_local_does_not_refresh_the_visible_tree():
    # Stage 2 item: a Plex background refresh finishing while the user has
    # already switched to the Local tab must update the cache (so it's
    # ready next time Plex is selected) without touching the currently
    # visible Local tree.
    harness = PlexFetchResultHarness(generation=2, library_source="local")

    harness._on_plex_library_fetch_result(_fake_music_result(generation=2))

    assert len(harness._plex_meta_by_kind["music"]) == 1
    assert harness.show_calls == 0


def test_fetch_result_is_ignored_during_window_shutdown():
    harness = PlexFetchResultHarness(generation=2)
    harness._closing = True

    harness._on_plex_library_fetch_result(_fake_music_result(generation=2))

    assert harness._plex_meta_by_kind["music"] == []
    assert harness.show_calls == 0


def test_large_plex_library_conversion_and_indexing_is_bounded():
    # Performance smoke test (item L): a 3000-track Plex music library --
    # the raw JSON -> meta_dict conversion (plex_track_to_meta_dict),
    # queue_detail_cache bulk-build (plex_meta_list_to_queue_detail_cache),
    # and search-index build (build_search_index) are the genuinely
    # Plex-specific costs on the fetch path; the shared tree-widget build
    # itself is unchanged Local-library machinery, already covered by this
    # project's own existing Local-scan performance work. 3000 tracks
    # comfortably covers "hundreds of tracks" from the Stage 2 spec.
    import time as _time

    raw_items = [
        {
            "ratingKey": str(index),
            "title": f"Track {index}",
            "grandparentTitle": f"Artist {index % 200}",
            "parentTitle": f"Album {index % 400}",
            "parentYear": 1990 + (index % 30),
            "index": (index % 20) + 1,
            "duration": 180000 + (index % 60) * 1000,
            "thumb": f"/library/metadata/{index}/thumb/1",
            "updatedAt": 1000 + index,
            "Media": [{
                "container": "mp3", "bitrate": 320,
                "Part": [{"key": f"/library/parts/{index}/file.mp3"}],
            }],
        }
        for index in range(3000)
    ]

    started = _time.perf_counter()
    meta_list = [
        plex_track_to_meta_dict(item, "server-perf") for item in raw_items
    ]
    convert_ms = (_time.perf_counter() - started) * 1000.0

    started = _time.perf_counter()
    detail_cache = plex_meta_list_to_queue_detail_cache(meta_list)
    detail_ms = (_time.perf_counter() - started) * 1000.0

    started = _time.perf_counter()
    index = build_search_index(meta_list)
    index_ms = (_time.perf_counter() - started) * 1000.0

    started = _time.perf_counter()
    results = filter_search_index(index, "artist 5")
    search_ms = (_time.perf_counter() - started) * 1000.0

    assert len(meta_list) == 3000
    assert len(detail_cache) == 3000
    assert len(index) == 3000
    assert len(results) > 0

    total_ms = convert_ms + detail_ms + index_ms
    print(
        f"\nPlex 3000-track perf: convert={convert_ms:.1f}ms "
        f"detail_cache={detail_ms:.1f}ms index={index_ms:.1f}ms "
        f"search={search_ms:.1f}ms total={total_ms:.1f}ms"
    )
    # Generous bound (this is a smoke test, not a tight perf gate) --
    # a real regression (e.g. an accidental O(n^2)) would blow past this
    # by an order of magnitude on 3000 items.
    assert total_ms < 2000.0
    assert search_ms < 200.0


# -- Video hierarchy (real-device follow-up item 7) --------------------------
# The real Plex Video library rendered as one large flat list. Root cause,
# traced directly: plex_video_to_meta_dict defaulted both artist and album
# to "" for items with no grandparentTitle/parentTitle, and
# group_library_albums groups purely on (album, artist) -- every such video
# landed in the SAME ("", "") bucket. Fixed at the metadata layer (no new
# grouping code): artist defaults to "Unknown Artist" (Local's own
# sentinel) and an empty album is left "" (not "Unknown Album"), which
# group_row_label now renders as just the artist -- "Artist -> Video" with
# no fake album layer, falling back to "Artist -> Album -> Video" whenever
# Plex genuinely supplies a parentTitle.

def test_group_row_label_omits_trailing_dash_when_album_is_empty():
    assert group_row_label("ABBA", "") == "ABBA"
    assert group_row_label("ABBA", "Arrival") == "ABBA - Arrival"
    assert group_row_label("", "Arrival") == "Arrival"
    assert group_row_label("", "") == ""


def test_plex_video_with_grandparent_title_gets_that_artist_and_no_fake_album():
    item = {"ratingKey": "9", "title": "Dancing Queen", "grandparentTitle": "ABBA"}
    meta = plex_video_to_meta_dict(item, "server-x")
    assert meta["artist"] == "ABBA"
    assert meta["album"] == ""


def test_plex_video_with_no_artist_metadata_falls_back_to_alphabetical_group():
    # Real-device follow-up: "Unknown Artist" was itself found to be an
    # insufficient fix -- an entire library with no artist metadata at
    # all just produced one giant "Unknown Artist" bucket (1,984 videos,
    # 1 visible row). Groups by the title's own first letter instead.
    item = {"ratingKey": "9", "title": "Some Clip"}
    meta = plex_video_to_meta_dict(item, "server-x")
    assert meta["artist"] == "S"
    assert meta["album"] == ""


def test_plex_video_alphabetical_fallback_handles_non_letter_titles():
    item = {"ratingKey": "9", "title": "1999"}
    meta = plex_video_to_meta_dict(item, "server-x")
    assert meta["artist"] == "#"


def test_plex_video_never_uses_originaltitle_as_artist():
    # originalTitle is the video's OWN alternate title, not an artist --
    # using it as an artist fallback would mislabel/miscategorise the
    # video (the exact bug the old expression risked). Falls back to the
    # alphabetical group (from title), not originalTitle.
    item = {"ratingKey": "9", "title": "Dancing Queen", "originalTitle": "Zzz Alt Cut"}
    meta = plex_video_to_meta_dict(item, "server-x")
    assert meta["artist"] == "D"


def test_plex_video_uses_artist_tag_list_when_no_grandparent_title():
    item = {"ratingKey": "9", "title": "Some Clip", "Artist": [{"tag": "Queen"}]}
    meta = plex_video_to_meta_dict(item, "server-x")
    assert meta["artist"] == "Queen"


def test_plex_video_keeps_real_album_when_plex_supplies_one():
    item = {
        "ratingKey": "9", "title": "Bohemian Rhapsody",
        "grandparentTitle": "Queen", "parentTitle": "Greatest Video Hits",
    }
    meta = plex_video_to_meta_dict(item, "server-x")
    assert meta["artist"] == "Queen"
    assert meta["album"] == "Greatest Video Hits"


def test_flat_plex_video_library_groups_by_artist_not_into_one_blob():
    # End-to-end proof against the real grouping function: many videos by
    # several different artists, none with a real Plex album, must NOT
    # all collapse into one undifferentiated top-level row.
    meta_list = [
        plex_video_to_meta_dict(
            {"ratingKey": str(i), "title": f"Video {i}", "grandparentTitle": artist},
            "server-x",
        )
        for i, artist in enumerate(
            ["ABBA"] * 5 + ["Queen"] * 3 + ["Prince"] * 2, start=1
        )
    ]

    entries = group_library_albums(meta_list)

    labels = {group_row_label(display_artist, album) for _, album, display_artist, _ in entries}
    assert labels == {"ABBA", "Queen", "Prince"}
    counts = {
        group_row_label(display_artist, album): len(items)
        for _, album, display_artist, items in entries
    }
    assert counts == {"ABBA": 5, "Queen": 3, "Prince": 2}


def test_plex_video_library_with_real_albums_groups_artist_dash_album():
    meta_list = [
        plex_video_to_meta_dict(
            {"ratingKey": "1", "title": "Song A", "grandparentTitle": "Queen", "parentTitle": "Greatest Hits"},
            "server-x",
        ),
        plex_video_to_meta_dict(
            {"ratingKey": "2", "title": "Song B", "grandparentTitle": "Queen", "parentTitle": "Greatest Hits"},
            "server-x",
        ),
    ]

    entries = group_library_albums(meta_list)

    assert len(entries) == 1
    _, album, display_artist, items = entries[0]
    assert group_row_label(display_artist, album) == "Queen - Greatest Hits"
    assert len(items) == 2


# -- MP3/FLAC never filtered out (real-device follow-up item 3) --------------

def test_plex_mp3_track_becomes_valid_audio_item_with_mp3_extension():
    item = {
        "ratingKey": "1", "type": 10, "title": "Track", "grandparentTitle": "Artist",
        "parentTitle": "Album", "duration": 200000,
        "Media": [{"container": "mp3", "audioCodec": "mp3", "bitrate": 320,
                   "Part": [{"key": "/library/parts/1/file.mp3"}]}],
    }
    meta = plex_track_to_meta_dict(item, "server-x")
    assert meta["media_type"] == window_module.MediaType.AUDIO.value
    assert meta["path"].endswith(".mp3")
    assert meta["container"] == "mp3"


def test_plex_flac_track_becomes_valid_audio_item_with_flac_extension():
    item = {
        "ratingKey": "2", "type": 10, "title": "Track", "grandparentTitle": "Artist",
        "parentTitle": "Album", "duration": 200000,
        "Media": [{"container": "flac", "audioCodec": "flac", "bitrate": 900,
                   "Part": [{"key": "/library/parts/2/file.flac"}]}],
    }
    meta = plex_track_to_meta_dict(item, "server-x")
    assert meta["media_type"] == window_module.MediaType.AUDIO.value
    assert meta["path"].endswith(".flac")
    assert meta["container"] == "flac"


def test_mp3_and_flac_both_survive_the_music_video_karaoke_split():
    # _apply_meta_list_to_library_tabs's classification is authoritative
    # media_type only -- never re-derived from the plex:// identity's
    # extension or from any Local capability/extension registry.
    mp3_item = {
        "ratingKey": "1", "type": 10, "title": "MP3 Track", "grandparentTitle": "Artist",
        "Media": [{"container": "mp3", "Part": [{"key": "/p/1"}]}],
    }
    flac_item = {
        "ratingKey": "2", "type": 10, "title": "FLAC Track", "grandparentTitle": "Artist",
        "Media": [{"container": "flac", "Part": [{"key": "/p/2"}]}],
    }
    meta_list = [
        plex_track_to_meta_dict(mp3_item, "server-x"),
        plex_track_to_meta_dict(flac_item, "server-x"),
    ]

    music_meta = [
        m for m in meta_list if m.get("media_type") == window_module.MediaType.AUDIO.value
    ]
    video_meta = [
        m for m in meta_list if m.get("media_type") == window_module.MediaType.VIDEO.value
    ]

    assert len(music_meta) == 2
    assert video_meta == []


# -- Real-window: loading/empty/error/offline states + source-switch races --
# (real-device follow-up items 4/5/9). One consolidated PlayerWindow, per
# this suite's established convention -- PlexLibraryFetchWorker itself is
# replaced with a no-op-start stand-in (still a real QThread subclass, so
# _worker_registry.register's expectations are satisfied) so this drives
# the window-layer state machine deterministically without any real
# network or threading; results are delivered by calling
# _on_plex_library_fetch_result directly, exactly as the real worker's
# finished_result signal would.

class _StubPlexLibraryFetchWorker(QtCore.QThread):
    finished_result = QtCore.pyqtSignal(dict)
    progress = QtCore.pyqtSignal(str, str)

    def __init__(self, *args, **kwargs):
        super().__init__()

    def start(self, *args, **kwargs):
        pass  # the test drives finished_result/progress itself

    def stop(self):
        pass


def test_plex_tab_loading_empty_error_offline_and_source_switch_races(tmp_path, monkeypatch):
    monkeypatch.setattr(window_module, "PlexLibraryFetchWorker", _StubPlexLibraryFetchWorker)
    window = _build_real_window(tmp_path, monkeypatch, tag="_plexstates")
    try:
        window.plex_preferences = PlexPreferences(
            enabled=True, use_manual_server=True,
            server_address="http://host:32400", token="tok",
            server_config_id="cfg-1",
            music_library_id="1", music_library_name="Music",
            video_library_id="2", video_library_name="Videos",
            # karaoke deliberately left unmapped
        )
        window.library_source_selector.setCurrentIndex(
            window.library_source_selector.findData("plex")
        )
        _pump(0.4)

        # 1. NOT CONFIGURED: karaoke was never mapped.
        assert _tree_status_text(window.tree_tracks_karaoke) == (
            "No Plex Karaoke library selected. Configure it in Preferences > Plex."
        )

        # 2. LOADING: the mapped Music category shows this immediately,
        # before any result has landed (item 5's minimum bar).
        assert _tree_status_text(window.tree_tracks) == "Loading Plex Music…"
        assert "music" in window._plex_refresh_in_flight

        # 3. TRUE EMPTY: the fetch "succeeds" with zero items.
        window._on_plex_library_fetch_result({
            "success": True, "media_kind": "music", "meta_list": [],
            "generation": window._plex_fetch_generation,
        })
        _pump(0.3)
        assert _tree_status_text(window.tree_tracks) == "No items found in this Plex library."

        # 4. ERROR: a refresh this time fails outright.
        window._refresh_plex_library()
        assert _tree_status_text(window.tree_tracks) == "Loading Plex Music…"
        window._on_plex_library_fetch_result({
            "success": False, "reason": "Server unreachable", "media_kind": "music",
            "generation": window._plex_fetch_generation,
        })
        _pump(0.3)
        assert _tree_status_text(window.tree_tracks) == (
            "Unable to load Plex Music. Server unreachable"
        )

        # 5. SUCCESS with real data + the transient "N loaded" status
        # message (never a permanent label -- it must be gone by the
        # time a later, unrelated message would show).
        window._refresh_plex_library()
        real_track = plex_track_to_meta_dict(
            {
                "ratingKey": "1", "type": 10, "title": "Dancing Queen",
                "grandparentTitle": "ABBA", "parentTitle": "Arrival",
                "Media": [{"container": "mp3", "Part": [{"key": "/p/1"}]}],
            },
            "cfg-1",
        )
        window._on_plex_library_fetch_result({
            "success": True, "media_kind": "music", "meta_list": [real_track],
            "generation": window._plex_fetch_generation,
        })
        _pump(0.5)
        assert window.statusBar().currentMessage() == "1 tracks loaded"
        assert window.tree_tracks.topLevelItemCount() >= 1
        assert _tree_status_text(window.tree_tracks) is None  # a real row now, not a status row

        # Give Videos its own real data too, so phase 8 below has a
        # populated Videos tree to prove is left untouched.
        real_video = plex_video_to_meta_dict(
            {"ratingKey": "9", "type": 1, "title": "Some Clip", "grandparentTitle": "Queen"},
            "cfg-1",
        )
        window._on_plex_library_fetch_result({
            "success": True, "media_kind": "video", "meta_list": [real_video],
            "generation": window._plex_fetch_generation,
        })
        _pump(0.5)
        assert window.tree_tracks_video.topLevelItemCount() >= 1

        # 6. OFFLINE: the connection is gone (e.g. laptop went out of
        # range) -- distinct from "still loading".
        window.plex_preferences = dataclasses.replace(
            window.plex_preferences, server_address="",
        )
        window._plex_meta_by_kind["music"] = []
        window._plex_fetched_kinds.discard("music")
        window._start_plex_library_fetch("music")
        assert _tree_status_text(window.tree_tracks) == "Plex server is unavailable."
        window.plex_preferences = dataclasses.replace(
            window.plex_preferences, server_address="http://host:32400",
        )

        # 7. Source-switch race #1 (item 9): Music genuinely loading,
        # user switches to Local before it resolves. The late result
        # must still update the cache but must NOT touch the Local tree.
        window._plex_fetched_kinds.discard("music")
        window._start_plex_library_fetch("music")
        assert "music" in window._plex_refresh_in_flight
        window.library_source_selector.setCurrentIndex(
            window.library_source_selector.findData("local")
        )
        _pump(0.3)
        local_top_level_before = window.tree_tracks.topLevelItemCount()
        local_first_item_before = (
            window.tree_tracks.topLevelItem(0).text(0) if local_top_level_before else None
        )
        window._on_plex_library_fetch_result({
            "success": True, "media_kind": "music", "meta_list": [real_track],
            "generation": window._plex_fetch_generation,
        })
        _pump(0.3)
        assert window.tree_tracks.topLevelItemCount() == local_top_level_before
        if local_top_level_before:
            assert window.tree_tracks.topLevelItem(0).text(0) == local_first_item_before
        assert window._plex_meta_by_kind["music"] == [real_track]  # cache still updated

        # 8. Source-switch race #2 (item 9): back to Plex; a late Music
        # result must never overwrite the already-populated Videos tab.
        window.library_source_selector.setCurrentIndex(
            window.library_source_selector.findData("plex")
        )
        _pump(0.3)
        video_top_level_before = window.tree_tracks_video.topLevelItemCount()
        window._plex_meta_by_kind["music"] = []
        window._plex_fetched_kinds.discard("music")
        window._on_plex_library_fetch_result({
            "success": True, "media_kind": "music", "meta_list": [real_track],
            "generation": window._plex_fetch_generation,
        })
        _pump(0.3)
        assert window.tree_tracks_video.topLevelItemCount() == video_top_level_before
    finally:
        _close_real_window(window)


# -- Real-window, large-library end-to-end performance (real-device follow-
# up item 6). The earlier 3000-track benchmark in this file proved
# conversion/detail-cache/search-index costs in isolation; this proves the
# same data through a REAL PlayerWindow's actual tree construction --
# whether the existing chunked/lazy _tree_build_tick mechanism (already
# used for Local, unmodified by Plex Stage 2) keeps the GUI responsive for
# a large Plex-sourced meta_list too. The HTTP layer itself is not real
# (no live Plex server available here) -- fetch/JSON-decode timing must be
# measured against the user's own real server; this isolates and measures
# everything downstream of that: conversion, cache population, and real
# Qt tree construction/GUI-apply time.

def test_large_plex_library_builds_a_real_tree_without_blocking_the_gui(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(window_module, "PlexLibraryFetchWorker", _StubPlexLibraryFetchWorker)
    window = _build_real_window(tmp_path, monkeypatch, tag="_plexperf")
    try:
        window.plex_preferences = PlexPreferences(
            enabled=True, use_manual_server=True,
            server_address="http://host:32400", token="tok",
            server_config_id="cfg-perf", music_library_id="1", music_library_name="Music",
        )
        window.library_source_selector.setCurrentIndex(
            window.library_source_selector.findData("plex")
        )
        _pump(0.3)

        raw_items = [
            {
                "ratingKey": str(i), "type": 10, "title": f"Track {i}",
                "grandparentTitle": f"Artist {i % 300}", "parentTitle": f"Album {i % 600}",
                "parentYear": 1990 + (i % 30), "index": (i % 20) + 1,
                "duration": 200000, "thumb": f"/library/metadata/{i}/thumb/1",
                "updatedAt": 1000 + i,
                "Media": [{"container": "mp3" if i % 4 else "flac", "bitrate": 320,
                           "Part": [{"key": f"/library/parts/{i}/file"}]}],
            }
            for i in range(3000)
        ]
        convert_started = time.perf_counter()
        meta_list = [plex_track_to_meta_dict(item, "cfg-perf") for item in raw_items]
        convert_ms = (time.perf_counter() - convert_started) * 1000.0

        apply_started = time.perf_counter()
        window._on_plex_library_fetch_result({
            "success": True, "media_kind": "music", "meta_list": meta_list,
            "generation": window._plex_fetch_generation,
        })
        cache_populated_ms = (time.perf_counter() - apply_started) * 1000.0
        assert len(window.queue_detail_cache) == 3000  # cache population, not deferred

        deadline = time.monotonic() + 15.0
        app = _app()
        while (
            window.tree_tracks.topLevelItemCount() < 600  # 600 unique albums expected
            and time.monotonic() < deadline
        ):
            app.processEvents()
            time.sleep(0.005)
        tree_build_ms = (time.perf_counter() - apply_started) * 1000.0

        print(
            f"\nPlex 3000-track real-window perf: convert={convert_ms:.1f}ms "
            f"cache_population={cache_populated_ms:.1f}ms "
            f"tree_build_total={tree_build_ms:.1f}ms "
            f"longest_chunk={window._library_apply_longest_chunk_ms:.1f}ms "
            f"chunks={window._library_apply_chunks}"
        )

        assert window.tree_tracks.topLevelItemCount() == 600
        # The existing chunked-build budget (10ms/chunk target, matching
        # Local's own LIBRARY_APPLY_TIME_BUDGET_SECONDS) is unmodified by
        # Plex Stage 2 -- a generous bound here catches a real regression
        # (e.g. an accidental per-row synchronous decode) without being a
        # tight timing assertion.
        assert window._library_apply_longest_chunk_ms < 100.0
        assert tree_build_ms < 10000.0
    finally:
        _close_real_window(window)


# -- Local library regression: Local -> Plex -> Local loses everything ------
# Real-device diagnostics: startup_cache_restore genuinely built 1284
# albums/43230 rows (proving the Local disk cache itself is intact), but a
# later source_switched_to_local apply built rows_created=0. Root-caused by
# reading the actual startup order: _load_user_settings (which sets
# self.library_source from persisted config) runs BEFORE the startup Local
# disk-cache restore's own _apply_meta_list_to_library_tabs call. If the
# user's PREVIOUS session ended with Plex selected, library_source is
# already "plex" at that point -- the old ambient _full_meta_list SETTER
# then filed the real Local records under the PLEX backing store instead
# (the tree still displayed correctly at that moment, since tree-building
# reads the meta_list parameter directly, never this property -- so
# nothing looked wrong yet). The bug surfaces later: Local's own backing
# store was never actually populated, so switching back to Local finds it
# empty. Fixed by _store_full_meta_list, which uses the call's own
# trigger_source (unambiguous: "startup" vs "plex_fetch") instead of the
# possibly-stale ambient self.library_source.

def test_local_library_survives_when_previous_session_persisted_plex_source(
    tmp_path, monkeypatch,
):
    localappdata = tmp_path / "appdata_localregress"
    localappdata.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    fake_meta = _fake_local_meta(count=50, albums=5)
    _seed_local_library_cache(str(localappdata), fake_meta)
    # Simulates "last session ended with Plex selected" -- library_source
    # is already "plex" before the startup Local cache-restore ever runs.
    _seed_config(str(localappdata), library_source="plex")

    _app()
    window = PlayerWindow()
    window.resize(1000, 800)
    window.show()
    _pump(0.6)
    try:
        assert window.library_source == "plex"
        assert len(window._local_full_meta_list_backing) == 50
        assert window._plex_full_meta_list_backing == []

        window.library_source_selector.setCurrentIndex(
            window.library_source_selector.findData("local")
        )
        _pump(0.4)

        assert window.library_source == "local"
        assert window.tree_tracks.topLevelItemCount() == 5  # 5 albums
        assert len(window.tracks) == 50
    finally:
        _close_real_window(window)


def test_repeated_local_plex_local_switching_retains_local_every_time(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(window_module, "PlexLibraryFetchWorker", _StubPlexLibraryFetchWorker)
    localappdata = tmp_path / "appdata_localregress2"
    localappdata.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    fake_meta = _fake_local_meta(count=30, albums=3)
    _seed_local_library_cache(str(localappdata), fake_meta)
    _seed_config(str(localappdata), library_source="local")

    _app()
    window = PlayerWindow()
    window.resize(1000, 800)
    window.show()
    _pump(0.6)
    try:
        assert window.tree_tracks.topLevelItemCount() == 3
        local_count_before = len(window._local_full_meta_list_backing)
        assert local_count_before == 30

        window.plex_preferences = PlexPreferences(
            enabled=True, use_manual_server=True,
            server_address="http://host:32400", token="tok",
            server_config_id="cfg-switch",
        )  # no libraries mapped -- Plex tab shows the "not configured" placeholder

        selector = window.library_source_selector
        for _ in range(2):
            selector.setCurrentIndex(selector.findData("plex"))
            _pump(0.2)
            assert window._local_full_meta_list_backing is not None
            assert len(window._local_full_meta_list_backing) == 30  # untouched by Plex

            selector.setCurrentIndex(selector.findData("local"))
            _pump(0.3)
            assert window.tree_tracks.topLevelItemCount() == 3
            assert len(window._local_full_meta_list_backing) == 30
            assert len(window.tracks) == 30
    finally:
        _close_real_window(window)


def test_local_admin_destructive_actions_disabled_while_viewing_plex(
    tmp_path, monkeypatch,
):
    # Belt-and-suspenders: the empty-space right-click menu's "Library"
    # submenu (Add/Remove Folder, Clean Cache, Delete Library, Rescan) all
    # read/write the Local on-disk cache and backing store directly --
    # disabled (not omitted, to avoid a dismissed-menu "action is None"
    # spuriously matching an omitted action) while Plex is the active
    # source, so they can never be triggered against the wrong data.
    localappdata = tmp_path / "appdata_adminmenu"
    localappdata.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    _app()
    window = PlayerWindow()
    window.resize(1000, 800)
    window.show()
    _pump(0.3)
    try:
        window.library_source = "plex"
        menu = QtWidgets.QMenu(window)
        refs = window._add_library_admin_menu(menu, track_path=None)
        (
            act_info, font_actions, act_add, act_remove_folder, act_clean_cache,
            act_delete_library, act_rescan, act_normalisation_preferences,
            *_rest,
        ) = refs
        for action in (act_add, act_remove_folder, act_clean_cache, act_delete_library, act_rescan):
            assert action.isEnabled() is False
        # Generic, source-agnostic settings remain available.
        assert act_normalisation_preferences.isEnabled() is True

        window.library_source = "local"
        menu2 = QtWidgets.QMenu(window)
        refs2 = window._add_library_admin_menu(menu2, track_path=None)
        (
            _info2, _font2, act_add2, act_remove_folder2, act_clean_cache2,
            act_delete_library2, act_rescan2, *_rest2,
        ) = refs2
        for action in (act_add2, act_remove_folder2, act_clean_cache2, act_delete_library2, act_rescan2):
            assert action.isEnabled() is True
    finally:
        _close_real_window(window)


# -- Prioritised fetch order (real-device follow-up item 7) ------------------
# The real log showed Music and Video requests starting essentially
# simultaneously. Whichever category tab is actually visible should start
# first; the rest are staggered so they don't compete with it.

class PlexPriorityFetchHarness:
    _start_plex_library_fetches_prioritized = (
        PlayerWindow._start_plex_library_fetches_prioritized
    )
    _PLEX_TAB_INDEX_TO_KIND = PlayerWindow._PLEX_TAB_INDEX_TO_KIND

    def __init__(self, current_tab_index=0):
        self.library_tabs = SimpleNamespace(currentIndex=lambda: current_tab_index)
        self.library_source = "plex"
        self._closing = False
        self.started = []

    def _start_plex_library_fetch(self, kind):
        self.started.append(kind)


def test_visible_music_tab_gets_priority_over_video_and_karaoke():
    harness = PlexPriorityFetchHarness(current_tab_index=0)  # Music tab visible

    harness._start_plex_library_fetches_prioritized(["video", "karaoke", "music"])

    assert harness.started == ["music"]  # started immediately; rest deferred


def test_visible_video_tab_gets_priority_when_that_tab_is_selected():
    harness = PlexPriorityFetchHarness(current_tab_index=1)  # Videos tab visible

    harness._start_plex_library_fetches_prioritized(["music", "video", "karaoke"])

    assert harness.started == ["video"]


def test_deferred_categories_start_after_the_visible_one_via_timers(monkeypatch):
    _app()
    harness = PlexPriorityFetchHarness(current_tab_index=0)
    scheduled = []
    monkeypatch.setattr(
        window_module.QtCore.QTimer, "singleShot",
        staticmethod(lambda ms, fn: scheduled.append((ms, fn))),
    )

    harness._start_plex_library_fetches_prioritized(["video", "karaoke", "music"])

    assert harness.started == ["music"]
    assert [ms for ms, _ in scheduled] == [600, 1200]
    # Firing the deferred callbacks actually starts the deferred categories.
    for _, fn in scheduled:
        fn()
    assert harness.started == ["music", "video", "karaoke"]


def test_deferred_fetch_skipped_if_source_switched_away_before_it_fires(monkeypatch):
    _app()
    harness = PlexPriorityFetchHarness(current_tab_index=0)
    scheduled = []
    monkeypatch.setattr(
        window_module.QtCore.QTimer, "singleShot",
        staticmethod(lambda ms, fn: scheduled.append((ms, fn))),
    )

    harness._start_plex_library_fetches_prioritized(["video", "music"])
    harness.library_source = "local"  # user switched away before the timer fires
    for _, fn in scheduled:
        fn()

    assert harness.started == ["music"]  # the deferred "video" never started
