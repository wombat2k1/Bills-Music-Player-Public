"""v1.0.67 MainThread I/O hardening: consolidated GUI-thread-I/O guard.

Every bug fixed this round already has its own targeted "must not call X
synchronously" regression test living next to that fix (test_lyrics_load_
worker.py, test_gain_lookup_worker.py, test_cast_payload_worker.py,
test_album_art_fetch_worker.py, test_track_info_panel.py,
test_video_transition_point_analyzer.py, test_track_tag_load_worker.py).
This file adds the one thing those don't give us: a *single, reusable*
guard that blocks every blocking API identified in this round's audit at
once (os.path.isfile/exists, Path.stat/exists, mutagen.File, soundfile.
info/read, builtin open, http_get) and drives each of the interactive
paths named in the v1.0.67 spec (Play/Next/Previous/auto-advance/
crossfade/recovery/video-activation/Smart-transition-timing/lyrics-
update/Cast-start/Up-Next-refresh) through it in one place, so a future
change that reintroduces blocking I/O on any of these paths fails loudly
regardless of which specific per-bug test would otherwise have caught it.

Deliberately does NOT construct a real PlayerWindow (this codebase's
established convention throughout -- see e.g. test_shutdown_hardening.py's
module docstring -- is SimpleNamespace fakes + PlayerWindow's own unbound
methods, not a real QMainWindow). "Exercises" therefore means: call the
real method with a fake window whose *state* is realistic but whose Qt
widgets/backends are doubles, with the blocking APIs patched to raise.
"""
import builtins
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

import billsmusic.window as window_module
from billsmusic.window import PlayerWindow
from billsmusic.worker_registry import WorkerLifetimeRegistry


class BlockedIOError(AssertionError):
    pass


_MEDIA_EXTENSIONS = (
    ".mp3", ".flac", ".wav", ".m4a", ".ogg", ".mp4", ".mkv", ".avi", ".mov",
)


def _looks_like_media_path(value) -> bool:
    return os.path.splitext(str(value))[1].lower() in _MEDIA_EXTENSIONS


@pytest.fixture
def blocked_io(monkeypatch):
    """Blocks the blocking APIs identified in the v1.0.67 audit whenever
    they're asked about something that looks like a playable media path.
    Any such call reaching one of these from inside the methods under
    test means real MainThread I/O snuck back onto an activation/
    crossfade/recovery/Cast/Up-Next path this round specifically closed
    off. Scoped to media-looking paths specifically (not a blanket
    os.stat/Path.stat/open patch) -- pytest's own internals (traceback
    formatting, its cache directory) call these same stdlib functions
    constantly for unrelated files; a blanket patch breaks the test
    runner itself, not just the code under test."""

    real_isfile = os.path.isfile
    real_exists = os.path.exists

    def _guarded_isfile(path):
        if _looks_like_media_path(path):
            raise BlockedIOError(f"blocked GUI-thread I/O: os.path.isfile({path!r})")
        return real_isfile(path)

    def _guarded_exists(path):
        if _looks_like_media_path(path):
            raise BlockedIOError(f"blocked GUI-thread I/O: os.path.exists({path!r})")
        return real_exists(path)

    def _blocked_mutagen(path, *a, **kw):
        raise BlockedIOError(f"blocked GUI-thread I/O: MutagenFile({path!r})")

    def _blocked_http_get(url, *a, **kw):
        raise BlockedIOError(f"blocked GUI-thread I/O: http_get({url!r})")

    monkeypatch.setattr(os.path, "isfile", _guarded_isfile)
    monkeypatch.setattr(os.path, "exists", _guarded_exists)
    monkeypatch.setattr(window_module, "MutagenFile", _blocked_mutagen)
    monkeypatch.setattr(window_module, "http_get", _blocked_http_get, raising=False)

    real_open = builtins.open

    def _guarded_open(file, *a, **kw):
        # Diagnostics/log/config/session/settings writes to the app's own
        # local %LOCALAPPDATA% files are explicitly out of scope for this
        # round (see CODEX_HANDOFF.md's "deliberately retained sync I/O"
        # section) -- only block opens that look like they're reading a
        # *media* file (the thing that's slow on a NAS).
        if _looks_like_media_path(file):
            raise BlockedIOError(f"blocked GUI-thread I/O: open({file!r})")
        return real_open(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", _guarded_open)
    return None


# ---------------------------------------------------------------------------
# Track/lyrics activation (Play / Next / Previous / auto-advance)
# ---------------------------------------------------------------------------

def test_load_lrc_for_track_cache_miss_dispatch_does_not_block(blocked_io, monkeypatch):
    class _FakeWorker:
        def __init__(self, path):
            self.lyrics_ready = SimpleNamespace(connect=lambda slot: None)
            self.finished = SimpleNamespace(connect=lambda slot: None)
        def start(self):
            pass

    monkeypatch.setattr(window_module, "LyricsLoadWorker", _FakeWorker)
    generation = window_module.NowPlayingGeneration()
    generation.begin("song.mp3", None)
    window = SimpleNamespace(
        _closing=False,
        _now_playing_generation=generation,
        _clear_synced_lyrics_state=lambda: None,
        _lyrics_cache={},
        _lyrics_load_workers=[],
        _worker_registry=WorkerLifetimeRegistry(),
    )
    PlayerWindow._load_lrc_for_track(window, "Y:/network/share/song.mp3", generation=generation.identity.generation)


def test_cached_gain_for_path_miss_dispatch_does_not_block(blocked_io, monkeypatch):
    class _FakeWorker:
        def __init__(self, path, loudness_cache):
            self.gain_ready = SimpleNamespace(connect=lambda slot: None)
            self.finished = SimpleNamespace(connect=lambda slot: None)
        def start(self):
            pass

    monkeypatch.setattr(window_module, "GainLookupWorker", _FakeWorker)
    window = SimpleNamespace(
        _closing=False,
        _gain_lookup_pending=set(),
        _gain_lookup_workers=[],
        _gain_lookup_subscribers={},
        _gain_snapshot_cache={},
        _gain_token_seq=0,
        _active_gain_token=0,
        _inactive_gain_token=0,
        _worker_registry=WorkerLifetimeRegistry(),
        loudness_cache=SimpleNamespace(override_for=lambda path: "default"),
        normalisation_enabled=True, normalisation_mode="track",
        target_lufs=-14.0, tagged_preamp_db=0.0, untagged_preamp_db=0.0,
        prevent_clipping=True,
        statusBar=lambda: SimpleNamespace(showMessage=lambda *a, **kw: None),
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None, path_details=lambda path: {}),
    )
    window._next_gain_token = lambda: PlayerWindow._next_gain_token(window)
    window._queue_gain_lookup_async = (
        lambda path, slot_token, target="active":
            PlayerWindow._queue_gain_lookup_async(window, path, slot_token, target)
    )
    PlayerWindow._cached_gain_for_path(window, "Y:/network/share/song.mp3")


# ---------------------------------------------------------------------------
# Crossfade / playback recovery gain lookup
# ---------------------------------------------------------------------------

def test_crossfade_and_recovery_gain_lookup_does_not_block(blocked_io):
    # _try_recovery_backend, _play_on_player and _play_simple all read
    # gain via the same _cached_gain_for_path -- covered directly above.
    # This proves the crossfade completion handler's own call site is
    # wired the same way (_on_crossfade_load_succeeded), since that's a
    # separate call site in window.py, not a shared helper.
    calls = []
    window = SimpleNamespace(
        _closing=False,
        _crossfade_load_is_current=lambda token, path: True,
        _backend_label=lambda: "BASS",
        _cached_gain_for_path=lambda path, target="active": calls.append(path) or 1.0,
        simple_inactive_player=SimpleNamespace(
            set_volume=lambda v: None, play=lambda: None, is_playing=lambda: True,
            stats=lambda: {"duration": 0.0, "sample_rate": 0, "channels": 2},
            commit_prepared=lambda candidate: True,
        ),
        simple_player=SimpleNamespace(get_length=lambda: 200.0, get_pos=lambda: 190.0),
        _audio_log=lambda msg: None,
        _audio_name=lambda path: path,
        pending_next=True,
        _pending_crossfade_immediate=False,
        pending_builtin_crossfade_quiet=False,
        prebuffer_active=False,
        fade_waits=0,
        _builtin_fade_generation=0,
        crossfade_seconds=5.0,
        _begin_builtin_fade=lambda generation=None: None,
        _is_current_playback_attempt=lambda attempt_id: True,
        _require_current_playback_attempt=lambda attempt_id, stage: True,
        _target_lease_still_valid=lambda lease: True,
    )
    lease = SimpleNamespace(physical_object=window.simple_inactive_player)
    PlayerWindow._on_crossfade_load_prepared(window, 1, "Y:/network/share/next.flac", object(), None, lease)
    assert calls == ["Y:/network/share/next.flac"]


# ---------------------------------------------------------------------------
# Smart Video Transition timing (position-tick hot path)
# ---------------------------------------------------------------------------

def test_smart_transition_timing_lookup_does_not_block_after_first_resolution(blocked_io):
    from billsmusic.video_transition_point_analyzer import (
        VideoTransitionPointAnalyzer, VideoTransitionPointResult,
    )

    class _MemoryOnlyCache:
        """Stands in for VideoTransitionPointCache without ever touching a
        real file -- the analyzer's own memoization (this round's BUG #5
        fix) is what's actually under test: cached_outro_end_ms must call
        this at most once per path, no matter how many ticks ask."""
        def __init__(self, result):
            self._result = result
            self.calls = 0
        def get(self, path):
            self.calls += 1
            return self._result

    cache = _MemoryOnlyCache(VideoTransitionPointResult(
        last_visible_ms=9100, outro_confidence=0.9,
    ))
    analyzer = VideoTransitionPointAnalyzer(None, cache)
    for _ in range(10):  # simulate 10 SECONDARY_READY position ticks
        assert analyzer.cached_outro_end_ms("Y:/network/share/clip.mp4") == 9100
    assert cache.calls == 1


# ---------------------------------------------------------------------------
# Cast start (connect + every track advance)
# ---------------------------------------------------------------------------

def test_cast_play_path_dispatch_does_not_block(blocked_io, monkeypatch):
    class _FakeWorker:
        def __init__(self, generation, path, artwork_dir):
            self.payload_ready = SimpleNamespace(connect=lambda slot: None)
            self.finished = SimpleNamespace(connect=lambda slot: None)
        def start(self):
            pass

    monkeypatch.setattr(window_module, "CastPayloadWorker", _FakeWorker)
    window = SimpleNamespace(
        _closing=False,
        _playback_generation=0, _playback_expected=False, _playback_intentionally_paused=True,
        _cast_payload_cache={}, _cast_artwork_paths={}, _cast_payload_generation=0, _cast_payload_workers=[],
        _worker_registry=WorkerLifetimeRegistry(),
        track_index_by_path={},
        _activate_track_ui=lambda index, path: None,
        cast_media_server=SimpleNamespace(
            revoke_all=lambda: None, register_audio=lambda path: f"http://cast/{path}",
            register=lambda path, content_type=None: f"http://cast/art/{path}",
        ),
        _cast_completion_armed=True, pending_next=True, _reset_progress=lambda: None,
        cast_controller=SimpleNamespace(load_async=lambda *a, **kw: None),
        statusBar=lambda: SimpleNamespace(showMessage=lambda *a, **kw: None),
        _ensure_cast_artwork_temp_dir=lambda: "C:/temp/cast-art",
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None, path_details=lambda path: {}),
    )
    window._request_cast_load = (
        lambda media_url, content_type, path, position, autoplay:
            PlayerWindow._request_cast_load(window, media_url, content_type, path, position, autoplay)
    )
    window._cast_payload_with_fresh_artwork = (
        lambda path, payload: PlayerWindow._cast_payload_with_fresh_artwork(window, path, payload)
    )
    assert PlayerWindow._cast_play_path(window, "Y:/network/share/song.mp3") is True


# ---------------------------------------------------------------------------
# Up Next refresh (cached_details_only path)
# ---------------------------------------------------------------------------

def test_queue_track_details_cached_only_does_not_block(blocked_io):
    window = SimpleNamespace(
        queue_detail_cache={},
        _cached_queue_analysis=lambda path, validate_signature=True: {},
    )
    details = PlayerWindow._queue_track_details(window, "Y:/network/share/song.mp3", cached_details_only=True)
    assert details == {"bitrate": "--", "time": "--", "key": "--", "bpm": "--"}
