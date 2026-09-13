"""v1.0.67 MainThread I/O hardening: Cast metadata/artwork preparation.

_cast_payload (a Mutagen tag read plus, for tracks with embedded art, a
full decode/scale/JPEG-encode/disk-write) used to run synchronously from
_on_cast_connected (once per Cast session) and, far more importantly,
_cast_play_path -- called on every single Cast track advance. Both now
call _request_cast_load, which serves an in-memory cache hit immediately
and otherwise dispatches CastPayloadWorker (billsmusic/workers.py) and
only calls cast_controller.load_async once the payload is ready,
generation-guarded so a superseded request never applies late.
"""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import billsmusic.window as window_module
from billsmusic.window import PlayerWindow
from billsmusic.worker_registry import WorkerLifetimeRegistry


def _cast_play_window(**overrides):
    load_calls = []
    window = SimpleNamespace(
        _closing=False,
        _playback_generation=0,
        _playback_expected=False,
        _playback_intentionally_paused=True,
        _cast_payload_cache={},
        _cast_artwork_paths={},
        _cast_payload_generation=0,
        _cast_payload_workers=[],
        _worker_registry=WorkerLifetimeRegistry(),
        track_index_by_path={},
        _activate_track_ui=lambda index, path: None,
        cast_media_server=SimpleNamespace(
            revoke_all=lambda: None,
            register_audio=lambda path: f"http://cast/{path}",
            register=lambda path, content_type=None: f"http://cast/art/{path}",
        ),
        _cast_completion_armed=True,
        pending_next=True,
        _reset_progress=lambda: None,
        cast_controller=SimpleNamespace(load_async=lambda *a, **kw: load_calls.append(a)),
        statusBar=lambda: SimpleNamespace(showMessage=lambda *a, **kw: None),
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None, path_details=lambda path: {}),
    )
    window._ensure_cast_artwork_temp_dir = lambda: "C:/temp/cast-art"
    for key, value in overrides.items():
        setattr(window, key, value)
    window._request_cast_load = (
        lambda media_url, content_type, path, position, autoplay:
            PlayerWindow._request_cast_load(window, media_url, content_type, path, position, autoplay)
    )
    window._cast_payload_with_fresh_artwork = (
        lambda path, payload: PlayerWindow._cast_payload_with_fresh_artwork(window, path, payload)
    )
    return window, load_calls


def test_cast_play_path_never_reads_tags_or_artwork_synchronously(monkeypatch):
    def _fail(*a, **kw):
        raise AssertionError(
            "_cast_play_path must not build Cast metadata/artwork "
            "synchronously -- this is what stalled the GUI thread on "
            "every Cast track advance for a NAS-backed track"
        )
    # read_track_meta isn't even imported into window.py any more -- the old
    # synchronous _cast_payload/_cast_artwork_url methods that used it were
    # deleted once CastPayloadWorker became the only caller (dead code).
    # read_cover_bytes is still imported for an unrelated purpose (Show
    # Album Cover), so that one's still worth guarding directly.
    assert not hasattr(window_module, "read_track_meta")
    monkeypatch.setattr(window_module, "read_cover_bytes", _fail)

    class _FakeWorker:
        def __init__(self, generation, path, artwork_dir):
            self.payload_ready = SimpleNamespace(connect=lambda slot: None)
            self.finished = SimpleNamespace(connect=lambda slot: None)
        def start(self):
            pass

    monkeypatch.setattr(window_module, "CastPayloadWorker", _FakeWorker)
    window, load_calls = _cast_play_window()

    result = PlayerWindow._cast_play_path(window, "Y:/network/share/song.mp3")

    assert result is True
    assert len(window._cast_payload_workers) == 1
    assert load_calls == []  # deferred until the (still-pending) payload arrives


def test_cast_play_path_cache_hit_calls_load_async_immediately(monkeypatch):
    def _fail(*a, **kw):
        raise AssertionError("cache hit must not construct a CastPayloadWorker")
    monkeypatch.setattr(window_module, "CastPayloadWorker", _fail)

    window, load_calls = _cast_play_window(
        _cast_payload_cache={"song.mp3": {"title": "Cached Song"}},
    )

    result = PlayerWindow._cast_play_path(window, "song.mp3")

    assert result is True
    assert load_calls == [("http://cast/song.mp3", "audio/mpeg", {"title": "Cached Song"}, 0.0, True)]


def test_cast_play_path_missing_file_returns_false_without_touching_worker(monkeypatch):
    class _FakeWorker:
        def __init__(self, *a, **kw):
            raise AssertionError("must not build a payload for a track that fails to register")
    monkeypatch.setattr(window_module, "CastPayloadWorker", _FakeWorker)

    def _register_audio(path):
        raise FileNotFoundError(path)

    window, load_calls = _cast_play_window(
        cast_media_server=SimpleNamespace(revoke_all=lambda: None, register_audio=_register_audio),
    )

    result = PlayerWindow._cast_play_path(window, "missing.mp3")

    assert result is False
    assert load_calls == []
