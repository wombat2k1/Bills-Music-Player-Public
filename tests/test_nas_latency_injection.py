"""v1.0.67 spec section 18: NAS-latency-injected tests.

Proves the GUI-thread-facing dispatch methods stay responsive even when
the underlying I/O they hand off to a background QThread is genuinely
slow (250-500ms, simulating a NAS/network share) -- without needing a
real NAS. Uses the real worker classes from workers.py (not fakes, unlike
every other test file this round), with the actual I/O function each one
calls monkeypatched to sleep before returning, so this measures real
thread dispatch overhead, not just mocked-out logic.

Each test proves two things: (1) the dispatching call itself (the method
called directly on the GUI thread) returns almost immediately regardless
of the injected latency, and (2) the injected latency was real and did
elapse on the background thread (via QThread.wait(), a lower-level,
more reliable synchronisation primitive than pumping the event loop and
hoping a signal connected to a plain Python closure gets delivered
cross-thread in a headless test environment -- that delivery path is
already covered by each bug's own dedicated test file, e.g.
test_shutdown_hardening.py's "registers and unregisters normally" tests,
which use a controllable fake signal instead of a real one for exactly
this reason).
"""
import os
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtWidgets

import billsmusic.window as window_module
import billsmusic.workers as workers_module
from billsmusic.window import PlayerWindow
from billsmusic.worker_registry import WorkerLifetimeRegistry

NAS_LATENCY_S = 0.35  # within the spec's 250-500ms simulated-blocking range
RESPONSIVE_BUDGET_S = 0.05  # the dispatching call itself must return well under this


@pytest.fixture(scope="module", autouse=True)
def qapplication():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def test_lyrics_dispatch_stays_responsive_under_simulated_nas_latency(monkeypatch):
    def _slow_load(path):
        time.sleep(NAS_LATENCY_S)
        return [(1.0, "line")], "tags"

    monkeypatch.setattr(workers_module, "load_lyrics_for_track", _slow_load)
    generation = window_module.NowPlayingGeneration()
    generation.begin("song.mp3", None)
    window = SimpleNamespace(
        _closing=False,
        _now_playing_generation=generation,
        _clear_synced_lyrics_state=lambda: None,
        _lyrics_cache={},
        _lyrics_load_workers=[],
        _worker_registry=WorkerLifetimeRegistry(),
        _set_synced_lyrics=lambda entries: None,
        _log=lambda msg: None,
        _record_stale_now_playing_detail=lambda *a, **kw: None,
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None, path_details=lambda path: {}),
    )

    started = time.perf_counter()
    PlayerWindow._load_lrc_for_track(window, "Y:/network/share/song.mp3", generation=generation.identity.generation)
    dispatch_elapsed = time.perf_counter() - started

    assert dispatch_elapsed < RESPONSIVE_BUDGET_S, (
        f"_load_lrc_for_track blocked the caller for {dispatch_elapsed:.3f}s "
        f"-- the {NAS_LATENCY_S}s simulated read must happen off-thread"
    )

    worker = window._lyrics_load_workers[0]
    background_started = time.perf_counter()
    assert worker.wait(2000), "background LyricsLoadWorker never finished"
    background_elapsed = time.perf_counter() - background_started
    assert background_elapsed >= NAS_LATENCY_S * 0.8, (
        "the simulated latency didn't actually elapse -- this test would "
        "pass trivially if the worker weren't doing real (slow) work"
    )


def test_gain_lookup_dispatch_stays_responsive_under_simulated_nas_latency(monkeypatch):
    def _slow_replaygain(path):
        time.sleep(NAS_LATENCY_S)
        return {"track_gain": -3.0, "track_peak": 0.9, "album_gain": None, "album_peak": None}

    monkeypatch.setattr(workers_module, "read_replaygain", _slow_replaygain)
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
        loudness_cache=SimpleNamespace(override_for=lambda path: "default", analysis_for=lambda path: None),
        normalisation_enabled=True, normalisation_mode="track",
        target_lufs=-14.0, tagged_preamp_db=0.0, untagged_preamp_db=0.0,
        prevent_clipping=True, auto_loudness_analysis=False, loudness_worker=None,
        current_path="Y:/network/share/song.mp3",
        _active_normalisation_gain=1.0,
        set_master_volume=lambda v: None,
        master_volume=80,
        statusBar=lambda: SimpleNamespace(showMessage=lambda *a, **kw: None),
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None, path_details=lambda path: {}),
    )

    window._next_gain_token = lambda: PlayerWindow._next_gain_token(window)
    window._queue_gain_lookup_async = (
        lambda p, slot_token, target="active":
            PlayerWindow._queue_gain_lookup_async(window, p, slot_token, target)
    )
    started = time.perf_counter()
    gain = PlayerWindow._cached_gain_for_path(window, "Y:/network/share/song.mp3")
    dispatch_elapsed = time.perf_counter() - started

    assert dispatch_elapsed < RESPONSIVE_BUDGET_S, (
        f"_cached_gain_for_path blocked the caller for {dispatch_elapsed:.3f}s"
    )
    assert gain == 1.0  # safe default returned immediately

    worker = window._gain_lookup_workers[0]
    background_started = time.perf_counter()
    assert worker.wait(2000), "background GainLookupWorker never finished"
    background_elapsed = time.perf_counter() - background_started
    assert background_elapsed >= NAS_LATENCY_S * 0.8


def test_cast_payload_dispatch_stays_responsive_under_simulated_nas_latency(monkeypatch, tmp_path):
    def _slow_meta(path):
        time.sleep(NAS_LATENCY_S)
        return {"title": "Song", "artist": "Artist", "album": "Album"}

    monkeypatch.setattr(workers_module, "read_track_meta", _slow_meta)
    monkeypatch.setattr(workers_module, "read_cover_bytes", lambda path: None)  # no artwork branch
    window = SimpleNamespace(
        _closing=False,
        _cast_payload_cache={},
        _cast_artwork_paths={},
        _cast_payload_generation=0,
        _cast_payload_workers=[],
        _worker_registry=WorkerLifetimeRegistry(),
        cast_media_server=SimpleNamespace(
            register=lambda path, content_type=None: f"http://cast/art/{path}",
        ),
        _cast_artwork_temp=None,
        cast_controller=SimpleNamespace(load_async=lambda *a, **kw: None),
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None, path_details=lambda path: {}),
    )
    window._ensure_cast_artwork_temp_dir = lambda: str(tmp_path)
    window._cast_payload_with_fresh_artwork = (
        lambda path, payload: PlayerWindow._cast_payload_with_fresh_artwork(window, path, payload)
    )

    started = time.perf_counter()
    PlayerWindow._request_cast_load(window, "http://cast/1", "audio/mpeg", "Y:/network/share/song.mp3", 0.0, True)
    dispatch_elapsed = time.perf_counter() - started

    assert dispatch_elapsed < RESPONSIVE_BUDGET_S, (
        f"_request_cast_load blocked the caller for {dispatch_elapsed:.3f}s"
    )

    worker = window._cast_payload_workers[0]
    background_started = time.perf_counter()
    assert worker.wait(2000), "background CastPayloadWorker never finished"
    background_elapsed = time.perf_counter() - background_started
    assert background_elapsed >= NAS_LATENCY_S * 0.8
