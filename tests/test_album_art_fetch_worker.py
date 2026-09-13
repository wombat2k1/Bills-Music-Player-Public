"""v1.0.67 MainThread I/O hardening: online album art fetch.

_fetch_album_art_online used to make two synchronous HTTP requests
(MusicBrainz release lookup, then a Cover Art Archive image fetch --
each up to a 10s timeout with internal retries, see net.py's http_get)
plus a disk write, all directly on the GUI thread, from the "Fetch Album
Art Online" menu action. A slow/unreachable network could freeze the
whole window for tens of seconds. AlbumArtFetchWorker (workers.py) now
does the network/disk work off-thread; _fetch_album_art_online only
dispatches and applies the result.

Needs a real QApplication (unlike test_shutdown_hardening.py's other
worker-category tests) because the real completion handler constructs a
QPixmap/QIcon -- see test_shutdown_hardening.py's note on this file.
"""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtWidgets

import billsmusic.window as window_module
from billsmusic.window import PlayerWindow
from billsmusic.worker_registry import WorkerLifetimeRegistry
from billsmusic.workers import musicbrainz_release_id


@pytest.fixture(scope="module", autouse=True)
def qapplication():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


class _FakeSignal:
    def __init__(self):
        self.slot = None
    def connect(self, slot):
        self.slot = slot


class _FakeWorker:
    def __init__(self, album_key, artist, album):
        self.art_ready = _FakeSignal()
        self.finished = _FakeSignal()
        self.started = False
    def start(self):
        self.started = True


def _dispatch_window(**overrides):
    window = SimpleNamespace(
        _closing=False,
        _album_art_fetch_workers=[],
        _worker_registry=WorkerLifetimeRegistry(),
        album_cover_allowlist=set(),
        album_cover_cache={},
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None, path_details=lambda path: {}),
    )
    for key, value in overrides.items():
        setattr(window, key, value)
    return window


def test_musicbrainz_release_id_returns_first_release(monkeypatch):
    monkeypatch.setattr(
        "billsmusic.workers.http_get",
        lambda url, timeout=10: b'{"releases": [{"id": "abc-123"}]}',
    )
    assert musicbrainz_release_id("Some Artist", "Some Album") == "abc-123"


def test_musicbrainz_release_id_returns_none_when_no_releases(monkeypatch):
    monkeypatch.setattr(
        "billsmusic.workers.http_get",
        lambda url, timeout=10: b'{"releases": []}',
    )
    assert musicbrainz_release_id("Some Artist", "Some Album") is None


def test_fetch_album_art_online_never_calls_http_get_synchronously(monkeypatch):
    def _fail(*a, **kw):
        raise AssertionError(
            "_fetch_album_art_online must not perform HTTP requests "
            "synchronously -- this used to freeze the whole window for up "
            "to 10s (with retries, longer) on a slow/unreachable network"
        )
    monkeypatch.setattr(window_module, "http_get", _fail, raising=False)
    monkeypatch.setattr(window_module, "AlbumArtFetchWorker", _FakeWorker)
    window = _dispatch_window()

    PlayerWindow._fetch_album_art_online(window, object(), {"artist": "A", "album": "B"})

    assert len(window._album_art_fetch_workers) == 1


def test_fetch_album_art_online_applies_a_successful_result(monkeypatch):
    monkeypatch.setattr(window_module, "AlbumArtFetchWorker", _FakeWorker)
    registry = WorkerLifetimeRegistry()
    window = _dispatch_window(_worker_registry=registry)
    item = QtWidgets.QTreeWidgetItem(["Some Album"])

    PlayerWindow._fetch_album_art_online(window, item, {"artist": "A", "album": "B"})
    assert registry.active_count() == 1
    worker = window._album_art_fetch_workers[0]
    assert worker.started

    worker.art_ready.slot("A::B", b"not-really-a-jpeg", "")
    worker.finished.slot()

    assert registry.active_count() == 0
    assert window._album_art_fetch_workers == []
    assert "A::B" in window.album_cover_allowlist
    assert window.album_cover_cache["A::B"] == b"not-really-a-jpeg"


def test_fetch_album_art_online_tolerates_a_deleted_tree_item(monkeypatch):
    # The library tree can be rebuilt (rescan, filter change, ...) while a
    # fetch is in flight -- applying a result to a since-deleted
    # QTreeWidgetItem must not crash the app.
    class _DeletedItem:
        def setIcon(self, *a, **kw):
            raise RuntimeError("wrapped C/C++ object of type QTreeWidgetItem has been deleted")

    monkeypatch.setattr(window_module, "AlbumArtFetchWorker", _FakeWorker)
    window = _dispatch_window()
    PlayerWindow._fetch_album_art_online(window, _DeletedItem(), {"artist": "A", "album": "B"})
    worker = window._album_art_fetch_workers[0]

    # A real (tiny) GIF, so loadFromData succeeds and setIcon is actually reached.
    tiny_gif = (
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00"
        b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01"
        b"\x00\x00\x02\x02D\x01\x00;"
    )
    worker.art_ready.slot("A::B", tiny_gif, "")  # must not raise
    assert window.album_cover_cache["A::B"] == tiny_gif


def test_fetch_album_art_online_shows_no_match_message_without_crashing(monkeypatch):
    messages = []
    monkeypatch.setattr(
        window_module.QtWidgets.QMessageBox, "information",
        staticmethod(lambda *a, **kw: messages.append(a)),
    )
    monkeypatch.setattr(window_module, "AlbumArtFetchWorker", _FakeWorker)
    window = _dispatch_window()

    PlayerWindow._fetch_album_art_online(window, object(), {"artist": "A", "album": "B"})
    worker = window._album_art_fetch_workers[0]
    worker.art_ready.slot("A::B", b"", "No match found on MusicBrainz.")  # must not raise

    assert window.album_cover_cache == {}
    assert len(messages) == 1
