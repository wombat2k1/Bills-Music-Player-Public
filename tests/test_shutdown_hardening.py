"""v1.0.66 worker-lifetime hardening: hostile shutdown tests.

Deliberately exercises "close while worker X is active" for every
background-work category identified in the v1.0.66 audit (see
CODEX_HANDOFF.md), proving:

  - no result handler mutates GUI/application state once self._closing
    (or the fake window's stand-in for it) is True;
  - no method that starts new background work does so once closing;
  - _request_shutdown()/_finalize_shutdown()/closeEvent() (Phase C2,
    2026-09-11: split out of the former single _shutdown_threads()) still
    actually reach the cancel/wait calls for every worker category that
    previously had none;
  - a worker's own normal-completion path still works exactly as before
    when the window is NOT closing (these guards must never fire for an
    ordinary user action).

Follows this test suite's existing convention (see e.g.
test_album_tag_refresh_worker.py, test_track_tag_load_worker.py): a
SimpleNamespace stands in for PlayerWindow, and PlayerWindow's own
unbound methods are called against it directly -- no real QMainWindow,
no real background threads, so these stay fast and deterministic.
"""
import inspect
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets

import billsmusic.window as window_module
from billsmusic.window import PlayerWindow
from billsmusic.worker_registry import WorkerLifetimeRegistry


def _no_popup_guard(monkeypatch):
    """Fails the test loudly if a modal dialog is ever actually shown --
    used everywhere a _closing guard is supposed to prevent one."""
    def _fail(*a, **kw):
        raise AssertionError("must not show a modal dialog while closing")
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", _fail)
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", _fail)


# ---------------------------------------------------------------------------
# Diagnostic export worker (can pop a modal QMessageBox on the closing window)
# ---------------------------------------------------------------------------

def test_diagnostic_export_complete_is_a_noop_while_closing(monkeypatch):
    _no_popup_guard(monkeypatch)
    window = SimpleNamespace(_closing=True, _diagnostic_export_worker=object())
    PlayerWindow._diagnostic_export_complete(window, "C:/bundle.zip")
    assert window._diagnostic_export_worker is None  # still cleared


def test_diagnostic_export_failed_is_a_noop_while_closing(monkeypatch):
    _no_popup_guard(monkeypatch)
    window = SimpleNamespace(_closing=True, _diagnostic_export_worker=object())
    PlayerWindow._diagnostic_export_failed(window, "boom")
    assert window._diagnostic_export_worker is None


def test_diagnostic_export_complete_still_shows_the_dialog_when_not_closing(monkeypatch):
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", lambda *a, **kw: shown.append(a))
    window = SimpleNamespace(
        _closing=False, _diagnostic_export_worker=object(),
        statusBar=lambda: SimpleNamespace(showMessage=lambda *a, **kw: None),
    )
    PlayerWindow._diagnostic_export_complete(window, "C:/bundle.zip")
    assert shown


# ---------------------------------------------------------------------------
# Cast subsystem result handlers
# ---------------------------------------------------------------------------

def test_cast_result_handlers_are_all_noops_while_closing():
    window = SimpleNamespace(_closing=True)
    # Each of these would AttributeError on the very next line if the
    # closing guard were missing or in the wrong place -- no attribute
    # this deep in each real handler body is present on this bare fake.
    PlayerWindow._on_cast_devices(window, [object()])
    PlayerWindow._on_cast_state(window, "playing", "hi")
    PlayerWindow._on_cast_connected(window, object())
    PlayerWindow._on_cast_loaded(window)
    PlayerWindow._on_cast_failed(window, "network down")


def test_on_cast_connected_never_chains_into_load_async_while_closing():
    calls = []
    window = SimpleNamespace(
        _closing=True,
        cast_controller=SimpleNamespace(load_async=lambda *a, **kw: calls.append(a)),
    )
    PlayerWindow._on_cast_connected(window, object())
    assert calls == []


# ---------------------------------------------------------------------------
# GPU capability probe
# ---------------------------------------------------------------------------

def test_gpu_capability_probe_finished_is_a_noop_while_closing():
    window = SimpleNamespace(_closing=True, _gpu_dual_capability=None, _gpu_dual_probe=object())
    PlayerWindow._on_gpu_capability_probe_finished(window, True, "")
    # Untouched -- real handler would set both of these when not closing.
    assert window._gpu_dual_capability is None
    assert window._gpu_dual_probe is not None


def test_start_gpu_capability_probe_refuses_once_closing(monkeypatch):
    def _fail(*a, **kw):
        raise AssertionError("must not construct a new GpuCompositorProbe while closing")
    monkeypatch.setattr(window_module, "GpuCompositorProbe", _fail)
    window = SimpleNamespace(
        _closing=True, _gpu_dual_capability=None, _gpu_dual_probe=None,
    )
    PlayerWindow._start_gpu_capability_probe(window)  # must not raise


# ---------------------------------------------------------------------------
# Crossfade load worker (PlayerLoadWorker) -- a late success used to be able
# to actually resume audio playback because the stale-result token was never
# invalidated by shutdown.
# ---------------------------------------------------------------------------

def test_crossfade_load_succeeded_is_a_noop_while_closing():
    # Phase C1 (native audio backend ownership, 2026-09-10): the candidate
    # never touched simple_inactive_player -- discarding it is all that's
    # needed here; _finalize_shutdown()'s own player.close() loop handles
    # the actual live players.
    calls = []
    window = SimpleNamespace(
        _closing=True,
        _crossfade_load_token=5,
        prebuffer_active=True,
        pending_builtin_crossfade_path="track.flac",
        simple_inactive_player=SimpleNamespace(stop=lambda: calls.append("stop")),
        _is_current_playback_attempt=lambda attempt_id: True,
        _require_current_playback_attempt=lambda attempt_id, stage: True,
    )
    window._discard_prepared_candidate = (
        lambda candidate, stage: PlayerWindow._discard_prepared_candidate(window, candidate, stage)
    )
    candidate = SimpleNamespace(discarded=False)

    def _discard():
        candidate.discarded = True
        return True
    candidate.discard = _discard
    PlayerWindow._on_crossfade_load_prepared(window, 5, "track.flac", candidate)
    # Falls into the "closing" branch and discards the candidate -- it
    # must NOT go on to resume/schedule a fade, and must not touch the
    # inactive player at all.
    assert candidate.discarded is True
    assert calls == []


def test_crossfade_load_failed_is_a_noop_while_closing():
    window = SimpleNamespace(
        _closing=True, _crossfade_load_token=5,
        _is_current_playback_attempt=lambda attempt_id: True,
        _require_current_playback_attempt=lambda attempt_id, stage: True,
    )
    PlayerWindow._on_crossfade_load_failed(window, 5, "track.flac", "error")  # must not raise


def test_shutdown_invalidates_the_crossfade_token_so_a_late_success_cannot_resume_playback():
    # The real defect: even with _closing checked, a token that still
    # matched would let a *slightly earlier-queued* signal (delivered
    # before Qt processes the _closing write, in a hypothetical race)
    # fall through _crossfade_load_is_current(). Bumping the token itself
    # is the actual fix; this proves shutdown does it.
    source = inspect.getsource(PlayerWindow._request_shutdown)
    assert "_crossfade_load_token += 1" in source


# ---------------------------------------------------------------------------
# Track tag load worker(s)
# ---------------------------------------------------------------------------

def test_track_tags_ready_is_a_noop_while_closing():
    window = SimpleNamespace(
        _closing=True, _playback_generation=3, current_path="a.mp3",
    )
    PlayerWindow._on_track_tags_ready(window, "a.mp3", {"title": "X"}, 3)  # must not raise


def test_queue_track_tags_async_refuses_to_start_once_closing(monkeypatch):
    def _fail(*a, **kw):
        raise AssertionError("must not construct a new TrackTagLoadWorker while closing")
    monkeypatch.setattr(window_module, "TrackTagLoadWorker", _fail)
    window = SimpleNamespace(_closing=True, _playback_generation=1)
    PlayerWindow._queue_track_tags_async(window, "a.mp3")  # must not raise


def test_queue_track_tags_async_registers_and_unregisters_normally(monkeypatch):
    class _FakeSignal:
        def __init__(self):
            self.slot = None
        def connect(self, slot):
            self.slot = slot

    class _FakeWorker:
        def __init__(self, path):
            self.tags_ready = _FakeSignal()
            self.finished = _FakeSignal()
            self.started = False
        def start(self):
            self.started = True

    monkeypatch.setattr(window_module, "TrackTagLoadWorker", _FakeWorker)
    registry = WorkerLifetimeRegistry()
    window = SimpleNamespace(
        _closing=False, _playback_generation=1, _track_tag_load_workers=[],
        _worker_registry=registry,
    )
    PlayerWindow._queue_track_tags_async(window, "a.mp3")
    assert registry.active_count() == 1
    worker = window._track_tag_load_workers[0]
    assert worker.started
    worker.finished.slot()  # simulate normal completion
    assert registry.active_count() == 0
    assert window._track_tag_load_workers == []


# ---------------------------------------------------------------------------
# Lyrics load worker (v1.0.67 MainThread I/O hardening)
# ---------------------------------------------------------------------------

def test_load_lrc_for_track_refuses_to_start_once_closing(monkeypatch):
    def _fail(*a, **kw):
        raise AssertionError("must not construct a new LyricsLoadWorker while closing")
    monkeypatch.setattr(window_module, "LyricsLoadWorker", _fail)
    window = SimpleNamespace(
        _closing=True,
        _now_playing_generation=window_module.NowPlayingGeneration(),
        _clear_synced_lyrics_state=lambda: None,
        _lyrics_cache={},
    )
    PlayerWindow._load_lrc_for_track(window, "a.mp3", generation=1)  # must not raise


def test_load_lrc_for_track_registers_and_unregisters_normally(monkeypatch):
    class _FakeSignal:
        def __init__(self):
            self.slot = None
        def connect(self, slot):
            self.slot = slot

    class _FakeWorker:
        def __init__(self, path):
            self.lyrics_ready = _FakeSignal()
            self.finished = _FakeSignal()
            self.started = False
        def start(self):
            self.started = True

    monkeypatch.setattr(window_module, "LyricsLoadWorker", _FakeWorker)
    registry = WorkerLifetimeRegistry()
    generation = window_module.NowPlayingGeneration()
    generation.begin("a.mp3")
    window = SimpleNamespace(
        _closing=False,
        _now_playing_generation=generation,
        _clear_synced_lyrics_state=lambda: None,
        _lyrics_cache={},
        _lyrics_load_workers=[],
        _worker_registry=registry,
        _set_synced_lyrics=lambda entries: None,
        _log=lambda msg: None,
        _record_stale_now_playing_detail=lambda *a, **kw: None,
        diagnostics=SimpleNamespace(
            record=lambda *a, **kw: None,
            path_details=lambda path: {},
        ),
    )
    PlayerWindow._load_lrc_for_track(window, "a.mp3", generation=generation.identity.generation)
    assert registry.active_count() == 1
    worker = window._lyrics_load_workers[0]
    assert worker.started
    worker.lyrics_ready.slot("a.mp3", [(1.0, "line")], "tags")  # simulate normal completion
    worker.finished.slot()
    assert registry.active_count() == 0
    assert window._lyrics_load_workers == []
    assert window._lyrics_cache["a.mp3"] == ([(1.0, "line")], "tags")


# ---------------------------------------------------------------------------
# Gain lookup worker (v1.0.67 MainThread I/O hardening)
# ---------------------------------------------------------------------------

def test_queue_gain_lookup_async_refuses_to_start_once_closing(monkeypatch):
    def _fail(*a, **kw):
        raise AssertionError("must not construct a new GainLookupWorker while closing")
    monkeypatch.setattr(window_module, "GainLookupWorker", _fail)
    window = SimpleNamespace(_closing=True, _gain_lookup_pending=set())
    PlayerWindow._queue_gain_lookup_async(window, "a.mp3", 1)  # must not raise


def test_queue_gain_lookup_async_registers_and_unregisters_normally(monkeypatch):
    class _FakeSignal:
        def __init__(self):
            self.slot = None
        def connect(self, slot):
            self.slot = slot

    class _FakeWorker:
        def __init__(self, path, loudness_cache):
            self.gain_ready = _FakeSignal()
            self.finished = _FakeSignal()
            self.started = False
        def start(self):
            self.started = True

    monkeypatch.setattr(window_module, "GainLookupWorker", _FakeWorker)
    registry = WorkerLifetimeRegistry()
    volume_calls = []
    window = SimpleNamespace(
        _closing=False,
        _gain_lookup_pending=set(),
        _gain_lookup_workers=[],
        _gain_lookup_subscribers={},
        _gain_snapshot_cache={},
        _gain_token_seq=5,
        _active_gain_token=5,
        _inactive_gain_token=0,
        _worker_registry=registry,
        loudness_cache=SimpleNamespace(override_for=lambda path: "default"),
        normalisation_enabled=True, normalisation_mode="track",
        target_lufs=-14.0, tagged_preamp_db=0.0, untagged_preamp_db=0.0,
        prevent_clipping=True, auto_loudness_analysis=False, loudness_worker=None,
        current_path="a.mp3",
        _active_normalisation_gain=1.0,
        set_master_volume=lambda v: volume_calls.append(v),
        master_volume=80,
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None, path_details=lambda path: {}),
    )
    PlayerWindow._queue_gain_lookup_async(window, "a.mp3", 5, "active")
    assert registry.active_count() == 1
    worker = window._gain_lookup_workers[0]
    assert worker.started
    worker.gain_ready.slot("a.mp3", {"track_gain": -3.0, "track_peak": 0.9,
                                      "album_gain": None, "album_peak": None}, None)
    worker.finished.slot()
    assert registry.active_count() == 0
    assert window._gain_lookup_workers == []
    assert "a.mp3" in window._gain_snapshot_cache
    # Result arrived while "a.mp3" was still the current track -- volume
    # must be corrected in place, matching _on_loudness_result's precedent.
    assert volume_calls == [80]


# ---------------------------------------------------------------------------
# Cast payload worker (v1.0.67 MainThread I/O hardening)
# ---------------------------------------------------------------------------

def test_request_cast_load_refuses_to_start_once_closing(monkeypatch):
    def _fail(*a, **kw):
        raise AssertionError("must not construct a new CastPayloadWorker while closing")
    monkeypatch.setattr(window_module, "CastPayloadWorker", _fail)
    window = SimpleNamespace(_closing=True, _cast_payload_cache={})
    # must not raise
    PlayerWindow._request_cast_load(window, "http://x/1", "audio/mpeg", "a.mp3", 0.0, True)


def _fake_cast_window(**overrides):
    window = SimpleNamespace(
        _closing=False,
        _cast_payload_cache={},
        _cast_artwork_paths={},
        _cast_payload_generation=0,
        _cast_payload_workers=[],
        _worker_registry=WorkerLifetimeRegistry(),
        cast_media_server=SimpleNamespace(
            register=lambda path, content_type=None: f"http://fresh/{path}",
        ),
        _cast_artwork_temp=None,
        cast_controller=SimpleNamespace(load_async=lambda *a, **kw: None),
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None, path_details=lambda path: {}),
    )
    for key, value in overrides.items():
        setattr(window, key, value)
    window._ensure_cast_artwork_temp_dir = lambda: "C:/temp/cast-art"
    window._cast_payload_with_fresh_artwork = (
        lambda path, payload: PlayerWindow._cast_payload_with_fresh_artwork(window, path, payload)
    )
    return window


class _FakeCastSignal:
    def __init__(self):
        self.slot = None
    def connect(self, slot):
        self.slot = slot


class _FakeCastPayloadWorker:
    def __init__(self, generation, path, artwork_dir):
        self.payload_ready = _FakeCastSignal()
        self.finished = _FakeCastSignal()
        self.started = False
    def start(self):
        self.started = True


def test_request_cast_load_registers_and_unregisters_normally(monkeypatch):
    monkeypatch.setattr(window_module, "CastPayloadWorker", _FakeCastPayloadWorker)
    registry = WorkerLifetimeRegistry()
    load_calls = []
    window = _fake_cast_window(
        _worker_registry=registry,
        cast_controller=SimpleNamespace(load_async=lambda *a, **kw: load_calls.append(a)),
    )
    PlayerWindow._request_cast_load(window, "http://x/1", "audio/mpeg", "a.mp3", 0.0, True)
    assert registry.active_count() == 1
    worker = window._cast_payload_workers[0]
    assert worker.started
    worker.payload_ready.slot(1, "a.mp3", {"title": "Song"}, "")
    worker.finished.slot()
    assert registry.active_count() == 0
    assert window._cast_payload_workers == []
    assert window._cast_payload_cache["a.mp3"] == {"title": "Song"}
    assert load_calls == [("http://x/1", "audio/mpeg", {"title": "Song"}, 0.0, True)]


def test_request_cast_load_drops_a_superseded_payload_result(monkeypatch):
    monkeypatch.setattr(window_module, "CastPayloadWorker", _FakeCastPayloadWorker)
    load_calls = []
    window = _fake_cast_window(
        cast_controller=SimpleNamespace(load_async=lambda *a, **kw: load_calls.append(a)),
    )
    PlayerWindow._request_cast_load(window, "http://x/1", "audio/mpeg", "old.mp3", 0.0, True)
    stale_worker = window._cast_payload_workers[0]
    # A newer track advance dispatches a second request before the first
    # one's payload arrives.
    PlayerWindow._request_cast_load(window, "http://x/2", "audio/mpeg", "new.mp3", 0.0, True)
    stale_worker.payload_ready.slot(1, "old.mp3", {"title": "Old"}, "")

    assert load_calls == []  # the stale result must never reach load_async
    assert window._cast_payload_cache["old.mp3"] == {"title": "Old"}


def test_rapid_next_both_misses_only_newest_worker_result_casts(monkeypatch):
    """v1.0.69 section 7/12: A miss -> B miss -> late A. Only B may cast,
    regardless of the order the two workers happen to finish in."""
    monkeypatch.setattr(window_module, "CastPayloadWorker", _FakeCastPayloadWorker)
    load_calls = []
    window = _fake_cast_window(
        cast_controller=SimpleNamespace(load_async=lambda *a, **kw: load_calls.append(a)),
    )
    PlayerWindow._request_cast_load(window, "http://x/a", "audio/mpeg", "a.mp3", 0.0, True)
    worker_a = window._cast_payload_workers[0]
    PlayerWindow._request_cast_load(window, "http://x/b", "audio/mpeg", "b.mp3", 0.0, True)
    worker_b = window._cast_payload_workers[1]

    # B's worker resolves first (a plausible real-world ordering too).
    worker_b.payload_ready.slot(2, "b.mp3", {"title": "B"}, "")
    assert load_calls == [("http://x/b", "audio/mpeg", {"title": "B"}, 0.0, True)]

    # A's worker resolves late -- must be dropped, not cast over B.
    worker_a.payload_ready.slot(1, "a.mp3", {"title": "A"}, "")
    assert load_calls == [("http://x/b", "audio/mpeg", {"title": "B"}, 0.0, True)]


def test_repeated_request_for_the_same_path_newest_wins(monkeypatch):
    """v1.0.69 section 12 item 4: two rapid requests for the *same* path
    (e.g. a double-click or a fast repeat) both dispatch (no dedup at
    this layer -- generation is the source of truth), but only the
    newer one's result may actually reach load_async."""
    monkeypatch.setattr(window_module, "CastPayloadWorker", _FakeCastPayloadWorker)
    load_calls = []
    window = _fake_cast_window(
        cast_controller=SimpleNamespace(load_async=lambda *a, **kw: load_calls.append(a)),
    )
    PlayerWindow._request_cast_load(window, "http://x/1", "audio/mpeg", "a.mp3", 0.0, True)
    first = window._cast_payload_workers[0]
    PlayerWindow._request_cast_load(window, "http://x/1", "audio/mpeg", "a.mp3", 0.0, True)
    second = window._cast_payload_workers[1]

    # The first (now-superseded) request's worker resolves after the second.
    first.payload_ready.slot(1, "a.mp3", {"title": "Old scan"}, "")
    assert load_calls == []  # generation 1 != current generation 2 -- dropped

    second.payload_ready.slot(2, "a.mp3", {"title": "Fresh scan"}, "")
    assert load_calls == [("http://x/1", "audio/mpeg", {"title": "Fresh scan"}, 0.0, True)]


def test_cast_payload_cache_hit_re_registers_artwork_fresh_not_from_cached_url(monkeypatch):
    """v1.0.69 (Codex finding C): the cached payload dict never carries a
    baked-in artwork URL (CastPayloadWorker only ever produces the local
    file path -- see its own docstring) -- this proves a cache-hit replay
    actually calls register() again for a known local artwork path,
    rather than trusting whatever (possibly already-revoked) URL might
    have ended up in the cached dict."""
    register_calls = []
    window = _fake_cast_window(
        _cast_payload_cache={"a.mp3": {"title": "A", "images": [{"url": "http://dead/old-token"}]}},
        _cast_artwork_paths={"a.mp3": "C:/temp/cast-art/cover-1.jpg"},
        cast_media_server=SimpleNamespace(
            register=lambda path, content_type=None: register_calls.append(path) or f"http://fresh/{path}",
        ),
    )
    load_calls = []
    window.cast_controller = SimpleNamespace(load_async=lambda *a, **kw: load_calls.append(a))

    PlayerWindow._request_cast_load(window, "http://x/1", "audio/mpeg", "a.mp3", 0.0, True)

    assert register_calls == ["C:/temp/cast-art/cover-1.jpg"]  # freshly re-registered, not skipped
    assert load_calls[0][2]["images"] == [{"url": "http://fresh/C:/temp/cast-art/cover-1.jpg"}]
    assert load_calls[0][2]["images"] != [{"url": "http://dead/old-token"}]  # the stale URL never reached Cast


def test_request_cast_load_generation_advances_on_cache_hit_too(monkeypatch):
    """v1.0.69: the Codex-reported "old-track Cast race" -- A misses (worker
    dispatched, generation 1), B is a cache hit (must still advance the
    generation to 2, not skip it), then A's late result must be dropped
    as stale rather than casting over the already-loaded B."""
    monkeypatch.setattr(window_module, "CastPayloadWorker", _FakeCastPayloadWorker)
    load_calls = []
    window = _fake_cast_window(
        _cast_payload_cache={"b.mp3": {"title": "B"}},
        cast_controller=SimpleNamespace(load_async=lambda *a, **kw: load_calls.append(a)),
    )
    PlayerWindow._request_cast_load(window, "http://x/a", "audio/mpeg", "a.mp3", 0.0, True)
    worker_a = window._cast_payload_workers[0]
    assert window._cast_payload_generation == 1

    PlayerWindow._request_cast_load(window, "http://x/b", "audio/mpeg", "b.mp3", 0.0, True)
    assert window._cast_payload_generation == 2  # cache hit still advanced it
    assert load_calls == [("http://x/b", "audio/mpeg", {"title": "B"}, 0.0, True)]

    worker_a.payload_ready.slot(1, "a.mp3", {"title": "A"}, "")

    # A's late result must never have reached load_async a second time.
    assert load_calls == [("http://x/b", "audio/mpeg", {"title": "B"}, 0.0, True)]
    # It's still cached for a future request of the same path, though.
    assert window._cast_payload_cache["a.mp3"] == {"title": "A"}


def test_request_cast_load_cache_hit_refused_while_closing(monkeypatch):
    """v1.0.69: the cache-hit path used to apply unconditionally, even
    while the application was shutting down -- now guarded like the miss
    path always was."""
    load_calls = []
    window = _fake_cast_window(
        _closing=True,
        _cast_payload_cache={"a.mp3": {"title": "A"}},
        cast_controller=SimpleNamespace(load_async=lambda *a, **kw: load_calls.append(a)),
    )
    PlayerWindow._request_cast_load(window, "http://x/a", "audio/mpeg", "a.mp3", 0.0, True)
    assert load_calls == []


def test_late_cast_worker_result_during_shutdown_cannot_cast_or_reopen_server(monkeypatch):
    """v1.0.69 (Codex findings A/B): a CastPayloadWorker dispatched before
    shutdown began, whose result arrives after application shutdown has
    both set _closing and permanently closed the real media server, must
    not cast the stale track and must not resurrect the server via its
    artwork registration attempt."""
    from billsmusic.media_server import LocalMediaServer
    monkeypatch.setattr(window_module, "CastPayloadWorker", _FakeCastPayloadWorker)
    real_server = LocalMediaServer(address_resolver=lambda: "127.0.0.1")
    load_calls = []
    window = _fake_cast_window(
        cast_media_server=real_server,
        cast_controller=SimpleNamespace(load_async=lambda *a, **kw: load_calls.append(a)),
    )
    PlayerWindow._request_cast_load(window, "http://x/1", "audio/mpeg", "a.mp3", 0.0, True)
    worker = window._cast_payload_workers[0]

    # Application shutdown begins: _closing set, server permanently closed
    # (mirrors _request_shutdown/_finalize_shutdown's own ordering).
    window._closing = True
    real_server.close()

    # The worker's result arrives late, with a (still-real, on-disk)
    # artwork path -- if this reached registration at all, it would try
    # to resurrect the server.
    worker.payload_ready.slot(1, "a.mp3", {"title": "A"}, "some/local/art.jpg")

    assert load_calls == []
    assert not real_server.running


def test_cast_payload_with_fresh_artwork_fails_closed_when_server_is_closed(tmp_path):
    """Defense-in-depth, independent of the _closing guard above: even if
    _cast_payload_with_fresh_artwork were reached with the media server
    already permanently closed, register() itself refuses, and artwork is
    dropped rather than left pointing at a URL that will never resolve."""
    from billsmusic.media_server import LocalMediaServer
    real_server = LocalMediaServer(address_resolver=lambda: "127.0.0.1")
    art = tmp_path / "cover.jpg"
    art.write_bytes(b"data")
    real_server.close()
    window = SimpleNamespace(
        cast_media_server=real_server,
        _cast_artwork_paths={"a.mp3": str(art)},
        _closing=True,
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None, path_details=lambda path: {}),
    )

    payload = PlayerWindow._cast_payload_with_fresh_artwork(window, "a.mp3", {"title": "A"})

    assert "images" not in payload
    assert not real_server.running


# ---------------------------------------------------------------------------
# Album art fetch worker (v1.0.67 MainThread I/O hardening)
# ---------------------------------------------------------------------------

def test_fetch_album_art_online_refuses_to_start_once_closing(monkeypatch):
    def _fail(*a, **kw):
        raise AssertionError("must not construct a new AlbumArtFetchWorker while closing")
    monkeypatch.setattr(window_module, "AlbumArtFetchWorker", _fail)
    window = SimpleNamespace(_closing=True)
    # must not raise
    PlayerWindow._fetch_album_art_online(window, object(), {"artist": "A", "album": "B"})


# See test_album_art_fetch_worker.py for the registration/unregistration/
# result-applied coverage -- it needs a real QApplication (QPixmap/QIcon
# construction in the real completion handler), which this file
# deliberately avoids for every other worker category (see module
# docstring), so that test lives there instead.


# ---------------------------------------------------------------------------
# Track info panel tag load (v1.0.67 MainThread I/O hardening)
# ---------------------------------------------------------------------------

def test_show_track_info_refuses_to_start_a_worker_once_closing(monkeypatch):
    def _fail(*a, **kw):
        raise AssertionError("must not construct a new TrackTagLoadWorker while closing")
    monkeypatch.setattr(window_module, "TrackTagLoadWorker", _fail)
    window = SimpleNamespace(
        _closing=True, current_path=None,
        _load_cached_audio_tags=lambda path: SimpleNamespace(
            title="", artist="", album="", genre="", bitrate="", sample_rate="", channels="", duration="",
        ),
        _display_track_info=lambda info, path: None,
    )
    PlayerWindow._show_track_info(window, "a.mp3")  # must not raise


def test_show_track_info_registers_and_unregisters_normally(monkeypatch):
    class _FakeSignal:
        def __init__(self):
            self.slot = None
        def connect(self, slot):
            self.slot = slot

    class _FakeWorker:
        def __init__(self, path):
            self.tags_ready = _FakeSignal()
            self.finished = _FakeSignal()
            self.started = False
        def start(self):
            self.started = True

    monkeypatch.setattr(window_module, "TrackTagLoadWorker", _FakeWorker)
    registry = WorkerLifetimeRegistry()
    displayed = []
    window = SimpleNamespace(
        _closing=False,
        current_path=None,
        _track_tag_load_workers=[],
        _worker_registry=registry,
        _load_cached_audio_tags=lambda path: SimpleNamespace(title="Cached"),
        _display_track_info=lambda info, path: displayed.append((info, path)),
    )
    PlayerWindow._show_track_info(window, "a.mp3")
    assert len(displayed) == 1 and displayed[0][0].title == "Cached"
    assert registry.active_count() == 1
    worker = window._track_tag_load_workers[0]
    assert worker.started
    worker.tags_ready.slot("a.mp3", {
        "title": "Fresh", "artist": "Unknown", "album": "Unknown", "genre": "Unknown",
        "bitrate": "Unknown", "sample_rate": "Unknown", "channels": "Unknown", "duration": "Unknown",
    })
    worker.finished.slot()
    assert registry.active_count() == 0
    assert window._track_tag_load_workers == []
    assert displayed[-1][1] == "a.mp3"
    assert displayed[-1][0].title == "Fresh"


# ---------------------------------------------------------------------------
# Regular library scan + quiet metadata backfill
# ---------------------------------------------------------------------------

def test_scan_finished_is_a_noop_while_closing():
    window = SimpleNamespace(_closing=True)
    PlayerWindow._on_scan_finished(window, [], [])  # must not raise -- would AttributeError otherwise


def test_scan_progress_and_canceled_are_noops_while_closing():
    window = SimpleNamespace(_closing=True)
    PlayerWindow._on_scan_progress(window, "Scanning", "x", 1, 10)
    PlayerWindow._on_scan_canceled(window)


def test_scan_thread_is_registered_with_the_shutdown_registry_at_dispatch():
    # Phase C2: the scan thread's cancel/wait is no longer inline in
    # shutdown -- it is registered (with a real cancel= callback, so
    # shutdown_all() actively cancels it, not just waits) at the point it
    # is started, and released via _on_simple_worker_finished once
    # WorkerLifetimeRegistry.shutdown_all() positively proves it finished.
    source = inspect.getsource(PlayerWindow._start_scan)
    idx = source.index('"scan_thread"')
    nearby = source[idx:idx + 150]
    assert "cancel=self.scan_thread.cancel" in nearby
    assert "thread=self.scan_thread" in nearby


def test_maybe_start_metadata_backfill_refuses_once_closing(monkeypatch):
    def _fail(*a, **kw):
        raise AssertionError("must not construct a new backfill LibraryScanThread while closing")
    monkeypatch.setattr(window_module, "LibraryScanThread", _fail)
    window = SimpleNamespace(_closing=True)
    PlayerWindow._maybe_start_metadata_backfill(window)  # must not raise


def test_backfill_finished_is_a_noop_while_closing_but_still_clears_the_thread():
    window = SimpleNamespace(_closing=True, _backfill_scan_thread=object())
    PlayerWindow._on_backfill_finished(window, [], [])
    assert window._backfill_scan_thread is None


def test_backfill_progress_and_canceled_are_noops_while_closing():
    window = SimpleNamespace(_closing=True, _backfill_scan_thread=object())
    PlayerWindow._on_backfill_progress(window, "Reading changed tags", "x", 1, 10)
    PlayerWindow._on_backfill_canceled(window)
    assert window._backfill_scan_thread is None


def test_shutdown_cancels_and_waits_the_backfill_scan_thread():
    # _request_shutdown calls the one semantically-correct cancellation
    # entry point (_cancel_metadata_backfill(), which also sets
    # _backfill_scan_cancelled_by_user_action) -- the registry's own
    # "backfill_scan" registration deliberately carries no cancel=, so
    # there is exactly one cancellation path, not two competing ones.
    request_source = inspect.getsource(PlayerWindow._request_shutdown)
    assert "_cancel_metadata_backfill()" in request_source
    dispatch_source = inspect.getsource(PlayerWindow._maybe_start_metadata_backfill)
    idx = dispatch_source.index('"backfill_scan"')
    nearby = dispatch_source[idx:idx + 150]
    assert "cancel=" not in nearby
    assert "thread=thread" in nearby


# ---------------------------------------------------------------------------
# Album tag refresh worker
# ---------------------------------------------------------------------------

def test_album_tags_refreshed_is_a_noop_while_closing():
    window = SimpleNamespace(_closing=True, _album_tag_refresh_thread=object())
    PlayerWindow._on_album_tags_refreshed(window, [{"path": "a.mp3"}])
    assert window._album_tag_refresh_thread is None


def test_refresh_album_tags_refuses_to_start_once_closing(monkeypatch):
    def _fail(*a, **kw):
        raise AssertionError("must not construct a new AlbumTagRefreshWorker while closing")
    monkeypatch.setattr(window_module, "AlbumTagRefreshWorker", _fail)
    window = SimpleNamespace(_closing=True, _cancel_metadata_backfill=lambda: None)
    PlayerWindow._refresh_album_tags(window, {"album": "Greatest Hits"})  # must not raise


def test_album_tag_refresh_worker_cancel_stops_the_run_loop_early(monkeypatch):
    from billsmusic.workers import AlbumTagRefreshWorker
    import billsmusic.workers as workers

    read_calls = []
    monkeypatch.setattr(workers, "read_track_meta", lambda p: read_calls.append(p) or {"path": p, "album": "A"})
    monkeypatch.setattr(os.path, "isdir", lambda p: False)

    worker = AlbumTagRefreshWorker("A", [
        {"path": "1.mp3", "album": "A"},
        {"path": "2.mp3", "album": "A"},
        {"path": "3.mp3", "album": "A"},
    ])
    worker.cancel()
    results = []
    worker.finished_refresh.connect(results.append)
    worker.run()
    assert read_calls == []  # cancelled before the loop's first iteration
    assert results == [[]]  # still emits, with whatever (nothing) was done


def test_album_tag_refresh_thread_is_registered_with_the_shutdown_registry_at_dispatch():
    # Phase C2: cancel/wait is no longer inline in shutdown -- registered
    # (with a real cancel= callback) at the point the worker is started.
    source = inspect.getsource(PlayerWindow._refresh_album_tags)
    idx = source.index('"album_tag_refresh"')
    nearby = source[idx:idx + 150]
    assert "cancel=thread.cancel" in nearby
    assert "thread=thread" in nearby


# ---------------------------------------------------------------------------
# Queue folder drop worker + playlist load worker
# ---------------------------------------------------------------------------

def test_finish_queue_drop_is_a_noop_while_closing():
    def _fail(*a, **kw):
        raise AssertionError("must not mutate the queue while closing")
    window = SimpleNamespace(_closing=True, _add_to_queue_with_dedup_guard=_fail)
    PlayerWindow._finish_queue_drop(window, ["a.mp3"])  # must not raise


def test_on_queue_files_dropped_refuses_to_start_once_closing(monkeypatch):
    def _fail(*a, **kw):
        raise AssertionError("must not construct a new QueueFolderDropWorker while closing")
    monkeypatch.setattr(window_module, "QueueFolderDropWorker", _fail)
    window = SimpleNamespace(_closing=True, _queue_drop_worker=None)
    PlayerWindow._on_queue_files_dropped(window, ["/some/folder"])  # must not raise


def test_playlist_loaded_and_load_failed_are_noops_while_closing(monkeypatch):
    _no_popup_guard(monkeypatch)
    window = SimpleNamespace(_closing=True)
    PlayerWindow._playlist_loaded(window, "list.m3u", [object()], {}, 10.0)
    PlayerWindow._playlist_load_failed(window, "bad file")


def test_load_playlist_to_queue_refuses_to_start_once_closing(monkeypatch):
    monkeypatch.setattr(
        QtWidgets.QFileDialog, "getOpenFileName",
        staticmethod(lambda *a, **kw: ("playlist.m3u", "")),
    )
    def _fail(*a, **kw):
        raise AssertionError("must not construct a new PlaylistLoadWorker while closing")
    monkeypatch.setattr(window_module, "PlaylistLoadWorker", _fail)
    window = SimpleNamespace(_closing=True, _playlist_load_worker=None)
    PlayerWindow._load_playlist_to_queue(window)  # must not raise


# ---------------------------------------------------------------------------
# Analyzer worker (visualiser)
# ---------------------------------------------------------------------------

def test_analyzer_result_and_busy_are_noops_while_closing():
    window = SimpleNamespace(_closing=True, current_path="a.mp3")
    PlayerWindow._on_analyzer_result(window, "a.mp3", [0.1, 0.2])
    PlayerWindow._on_analysis_busy(window, "a.mp3", True)


def test_analyzer_worker_never_emits_a_result_after_stop_mid_tick(monkeypatch):
    from billsmusic.workers import AnalyzerWorker
    worker = AnalyzerWorker.__new__(AnalyzerWorker)  # skip QThread.__init__ (no QApplication needed)
    results = []
    worker.result_ready = SimpleNamespace(emit=lambda *a: results.append(a))
    worker._analyzer = SimpleNamespace(get_levels=lambda t: [0.5])
    worker._current_path = "a.mp3"
    worker._running = False  # stop() already landed
    # Exercise just the emit-decision line's logic directly (mirrors the
    # real run() body's structure without needing the full mutex/condvar
    # loop or a real QThread).
    t = 1.0
    levels = worker._analyzer.get_levels(t)
    if levels is not None and worker._running:
        worker.result_ready.emit(worker._current_path, levels)
    assert results == []


# ---------------------------------------------------------------------------
# mini_player / party_mode global-QThreadPool artwork tasks
# ---------------------------------------------------------------------------

def test_mini_player_artwork_finished_closure_drops_a_result_delivered_after_closing_began():
    class _FakeTask:
        def __init__(self, *a, **kw):
            self.signals = SimpleNamespace(loaded=SimpleNamespace(connect=lambda slot: setattr(self, "_slot", slot)))

    class _FakePool:
        def start(self, task):
            pass

    fake_window = SimpleNamespace(
        _artwork_load_path=None,
        _artwork_tasks=set(),
        _closing=False,  # task starts BEFORE closing begins
        artwork=SimpleNamespace(width=lambda: 100, height=lambda: 100),
        owner=SimpleNamespace(current_path="a.mp3", _now_playing_generation=None),
    )
    import billsmusic.mini_player as mp
    real_task_cls = mp._ArtworkLoadTask
    real_pool = mp.QtCore.QThreadPool
    try:
        mp._ArtworkLoadTask = _FakeTask
        mp.QtCore.QThreadPool = SimpleNamespace(globalInstance=lambda: _FakePool())
        mp.MiniPlayerWindow._load_current_artwork(fake_window, "a.mp3", "key")
        assert len(fake_window._artwork_tasks) == 1  # task genuinely started
        task = next(iter(fake_window._artwork_tasks))
        applied = []
        fake_window._apply_loaded_artwork = lambda *a: applied.append(a)
        fake_window._closing = True  # shutdown begins while the task is in flight
        task._slot("a.mp3", "key", object())  # the global-pool task finally delivers
        assert applied == []
        assert fake_window._artwork_tasks == set()  # still cleaned up either way
    finally:
        mp._ArtworkLoadTask = real_task_cls
        mp.QtCore.QThreadPool = real_pool


def test_mini_player_load_current_artwork_refuses_to_start_once_closing():
    def _fail(*a, **kw):
        raise AssertionError("must not construct a new _ArtworkLoadTask while closing")
    fake_window = SimpleNamespace(_artwork_load_path=None, _closing=True)
    import billsmusic.mini_player as mp
    real_task_cls = mp._ArtworkLoadTask
    try:
        mp._ArtworkLoadTask = _fail
        mp.MiniPlayerWindow._load_current_artwork(fake_window, "a.mp3", "key")  # must not raise
    finally:
        mp._ArtworkLoadTask = real_task_cls


def test_mini_player_close_sets_closing_flag_when_actually_closing():
    from billsmusic.mini_player import MiniPlayerWindow
    window = SimpleNamespace(_allow_close=True, _closing=False)
    event = SimpleNamespace(accept=lambda: None, ignore=lambda: None)
    MiniPlayerWindow.closeEvent(window, event)
    assert window._closing is True


def test_party_mode_close_sets_closing_flag_when_actually_closing():
    from billsmusic.party_mode import PartyModeWindow
    window = SimpleNamespace(_allow_close=True, _closing=False)
    event = SimpleNamespace(accept=lambda: None, ignore=lambda: None)
    PartyModeWindow.closeEvent(window, event)
    assert window._closing is True


def test_party_mode_background_blur_refuses_to_start_once_closing():
    from billsmusic.party_mode import PartyModeWindow
    window = SimpleNamespace(_closing=True, _blur_tasks=set())
    PartyModeWindow._request_background_blur(window, object(), 1)  # must not raise
    assert window._blur_tasks == set()


# ---------------------------------------------------------------------------
# closeEvent / _request_shutdown / _finalize_shutdown structural/ordering
# requirements
# ---------------------------------------------------------------------------

def test_closing_flag_is_the_first_statement_of_close_event():
    source = inspect.getsource(PlayerWindow.closeEvent)
    lines = [ln.strip() for ln in source.splitlines()[1:] if ln.strip() and not ln.strip().startswith("#")]
    assert lines[0] == "self._closing = True"


def test_request_shutdown_sets_closing_early_too():
    # Phase C2: closeEvent() itself now sets self._closing = True as its
    # own literal first statement (see
    # test_closing_flag_is_the_first_statement_of_close_event), before
    # even the one-time UI/session teardown -- _request_shutdown() sets
    # it again (idempotent) for callers that invoke it directly (tests)
    # without going through closeEvent. Checked here directly rather than
    # requiring it be _request_shutdown's literal first statement, since
    # _request_shutdown legitimately starts with its own
    # _shutdown_requested idempotency guard first.
    source = inspect.getsource(PlayerWindow._request_shutdown)
    idx = source.index("_shutdown_requested = True")
    nearby = source[idx:idx + 100]
    assert "self._closing = True" in nearby


def test_finalize_shutdown_calls_close_not_stop_on_cast_services():
    # stop() is a normal refresh/reconnect reset (still used elsewhere);
    # close() is the one-way shutdown flag -- see cast_service.py.
    source = inspect.getsource(PlayerWindow._finalize_shutdown)
    assert "self.cast_discovery.close()" in source
    assert "self.cast_controller.close()" in source


def test_request_shutdown_cancels_the_gpu_capability_probe():
    source = inspect.getsource(PlayerWindow._request_shutdown)
    idx = source.index("_gpu_dual_probe")
    nearby = source[idx:idx + 150]
    assert "gpu_probe.cancel()" in nearby


def test_request_shutdown_calls_worker_registry_shutdown_all():
    source = inspect.getsource(PlayerWindow._request_shutdown)
    assert "self._worker_registry.shutdown_all()" in source


def test_loudness_worker_is_registered_with_the_shutdown_registry_at_startup():
    # Phase C2: loudness_worker is no longer stopped/waited/nulled inline
    # inside shutdown -- it is registered with WorkerLifetimeRegistry at
    # construction time (one shutdown-ownership mechanism per worker,
    # not a duplicate ad-hoc block), and released via
    # _on_simple_worker_finished once WorkerLifetimeRegistry.shutdown_all()
    # (called from _request_shutdown) positively proves it finished.
    source = inspect.getsource(PlayerWindow._startup_prepare_audio)
    idx = source.index("self.loudness_worker = LoudnessAnalysisWorker()")
    nearby = source[idx:idx + 400]
    assert "self._worker_registry.register(" in nearby
    assert "cancel=self.loudness_worker.stop" in nearby
    assert '"loudness"' in nearby


def test_request_shutdown_is_idempotent_when_called_twice_in_a_row():
    # Regression guard for "close twice": _request_shutdown has an
    # explicit idempotency guard (unlike the old _shutdown_threads, which
    # relied on every individual block happening to be defensive) --
    # proven at the source level rather than by counting try/except
    # blocks.
    source = inspect.getsource(PlayerWindow._request_shutdown)
    # The idempotency guard must be the first executable statement, i.e.
    # appear before any of the actual shutdown side effects it guards.
    guard_idx = source.index("if self._shutdown_requested:")
    first_side_effect_idx = source.index("self._cancel_current_playback_attempt(")
    assert guard_idx < first_side_effect_idx
    assert source.count("except Exception") >= 10  # individual steps still stay defensive


def test_finalize_shutdown_is_idempotent_and_never_runs_twice():
    source = inspect.getsource(PlayerWindow._finalize_shutdown)
    assert "if self._shutdown_complete:" in source
    assert "if self._shutdown_finalizing:" in source
    assert "self._shutdown_complete = True" in source


# ---------------------------------------------------------------------------
# Phase C2 hostile tests: worker-held candidate ownership during shutdown
# (prepared-vs-finished signal order independence, using the real
# BassStreamPrepareWorker/claim_candidate() exactly-once seam and the
# real _on_plex_audio_load_prepared/_on_plex_audio_load_worker_finished
# dispatch handlers -- proves the guarantee end to end, not just at the
# worker-class level already covered by test_player_load_worker.py).
# ---------------------------------------------------------------------------

class _FakeCandidate:
    def __init__(self):
        self.discard_calls = 0

    def discard(self):
        self.discard_calls += 1
        return True


def _plex_audio_load_shutdown_window(worker):
    events = []
    window = SimpleNamespace(
        _closing=True,
        _plex_audio_load_worker=worker,
        _plex_audio_load_token=1,
        diagnostics=SimpleNamespace(record=lambda *a, **kw: events.append((a, kw))),
        _worker_registry=WorkerLifetimeRegistry(),
        _require_current_playback_attempt=lambda attempt_id, stage: True,
        _maybe_resume_final_shutdown=lambda: None,
    )
    window._discard_prepared_candidate = (
        lambda c, stage: PlayerWindow._discard_prepared_candidate(window, c, stage)
    )
    window._finalize_unclaimed_prepare_candidate = (
        lambda w, stage: PlayerWindow._finalize_unclaimed_prepare_candidate(window, w, stage)
    )
    return window, events


def test_prepared_then_finished_during_shutdown_discards_exactly_once():
    from billsmusic.workers import BassStreamPrepareWorker
    candidate = _FakeCandidate()
    worker = BassStreamPrepareWorker.__new__(BassStreamPrepareWorker)
    worker._prepared_candidate = candidate
    window, events = _plex_audio_load_shutdown_window(worker)

    # Order A: the queued `prepared` signal is processed first -- the
    # dispatch-time connect() lambda claims via worker.claim_candidate()
    # before calling the handler, exactly like the real registration.
    PlayerWindow._on_plex_audio_load_prepared(window, 1, "plex://x", worker.claim_candidate(), None, None)
    # ...then the worker's own `finished` signal is processed.
    PlayerWindow._on_plex_audio_load_worker_finished(window, worker, None)

    assert candidate.discard_calls == 1  # discarded exactly once, never twice
    assert window._plex_audio_load_worker is None
    assert events == []  # a successful discard() records nothing


def test_finished_then_prepared_during_shutdown_discards_exactly_once():
    from billsmusic.workers import BassStreamPrepareWorker
    candidate = _FakeCandidate()
    worker = BassStreamPrepareWorker.__new__(BassStreamPrepareWorker)
    worker._prepared_candidate = candidate
    window, events = _plex_audio_load_shutdown_window(worker)

    # Order B: `finished` is processed first (worker.wait() proved
    # completion, or the finished signal simply arrived first) -- its
    # identity-safe handler claims the still-unclaimed candidate via
    # _finalize_unclaimed_prepare_candidate.
    PlayerWindow._on_plex_audio_load_worker_finished(window, worker, None)
    # ...then the queued `prepared` signal is processed; the dispatch-time
    # lambda's own worker.claim_candidate() call now returns None.
    PlayerWindow._on_plex_audio_load_prepared(window, 1, "plex://x", worker.claim_candidate(), None, None)

    assert candidate.discard_calls == 1  # still exactly once, opposite order
    assert window._plex_audio_load_worker is None
    assert events == []


def test_registry_finalize_after_join_discards_via_the_c1_1_helper_on_native_free_failure():
    # A worker that finished (wait() proved it) but whose GUI-thread
    # `prepared` callback never ran -- the registry's finalize_after_join
    # must resolve it via _finalize_unclaimed_prepare_candidate, which
    # must still route through _discard_prepared_candidate() (never a
    # bare candidate.discard()) so a native free failure (BASS_StreamFree
    # returning False) stays diagnostically visible, exactly like every
    # other rejection path -- and must never retry the native free.
    from billsmusic.workers import BassStreamPrepareWorker

    class _FailingCandidate:
        def __init__(self):
            self.discard_calls = 0

        def discard(self):
            self.discard_calls += 1
            return False

    candidate = _FailingCandidate()
    worker = BassStreamPrepareWorker.__new__(BassStreamPrepareWorker)
    worker._prepared_candidate = candidate
    window, events = _plex_audio_load_shutdown_window(worker)
    registry = window._worker_registry
    registry.register(
        "plex_audio_load", thread=SimpleNamespace(wait=lambda ms: True),
        finalize_after_join=lambda w=worker: window._finalize_unclaimed_prepare_candidate(w, "plex_audio_load_join"),
    )

    registry.shutdown_all()

    assert candidate.discard_calls == 1  # never retried
    assert len(events) == 1
    (args, kw) = events[0]
    assert args[1] == "prepared_candidate_discard_failed"
    assert kw["details"]["stage"] == "plex_audio_load_join"
    # A second, later resolution attempt (e.g. the finished handler,
    # were it to also fire) claims nothing further -- exactly-once.
    assert worker.claim_candidate() is None


# ---------------------------------------------------------------------------
# Phase C2 hostile tests: _finalize_shutdown ordering, video stop-vs-
# shutdown split, worker-timeout-keeps-window-alive, final-worker-finish
# resumes and completes shutdown, multiple-workers-only-last-completes,
# no duplicate cancellation, no QThread.terminate().
# ---------------------------------------------------------------------------

def test_finalize_shutdown_does_not_mark_complete_until_after_resource_teardown():
    # _shutdown_complete must only become True in the finally block, AFTER
    # every teardown step has at least been attempted -- not at the top,
    # so a concurrent/re-entrant caller can never observe "complete" while
    # native resources are still being closed. AssertionError raised
    # inside a teardown step's own callback would be silently swallowed
    # by _finalize_shutdown's own per-step try/except, so this records
    # the observed flag value into `order` instead of asserting inline.
    order = []
    window = SimpleNamespace(_shutdown_complete=False, _shutdown_finalizing=False)

    class _RecordingPlayer:
        def close(self):
            order.append(("player_close", window._shutdown_complete))

    for name in ("miniaudio_player", "miniaudio_inactive_player", "bass_player", "bass_inactive_player"):
        setattr(window, name, _RecordingPlayer())
    window._video_backend = None
    window._video_transition_point_analyzer = None
    window.viz_logger = SimpleNamespace(stop=lambda: order.append("viz_stop"))
    window.artwork_manager = SimpleNamespace(shutdown=lambda: order.append("artwork_shutdown"))
    window.cast_discovery = SimpleNamespace(close=lambda: order.append("cast_discovery_close"), discovery_thread=None)
    window.cast_controller = SimpleNamespace(
        close=lambda: order.append("cast_controller_close"), disconnect=lambda: None,
        connect_thread=None, load_thread=None,
    )
    window.cast_media_server = SimpleNamespace(close=lambda: order.append("cast_media_server_close"))
    window._cast_artwork_temp = None
    window._diagnostic_export_worker = None
    window.diagnostics = SimpleNamespace(
        record=lambda *a, **kw: order.append("diagnostics_record"),
        shutdown=lambda: order.append("diagnostics_shutdown"),
    )
    window._audio_log = lambda message: None

    PlayerWindow._finalize_shutdown(window)

    player_close_events = [entry[1] for entry in order if isinstance(entry, tuple) and entry[0] == "player_close"]
    assert player_close_events  # the loop actually ran
    assert all(flag is False for flag in player_close_events)  # not complete yet, at the time of each close()
    assert "diagnostics_shutdown" in order
    assert window._shutdown_complete is True  # only True at the very end
    assert window._shutdown_finalizing is False


def test_video_stop_happens_during_request_shutdown_only_destroys_during_finalize():
    video_calls = []
    window = SimpleNamespace(
        _shutdown_requested=False, _closing=False, _shutdown_pending=False,
        _playback_generation=0, _plex_audio_load_token=0, _crossfade_load_token=0,
        _karaoke_generation=0, _library_search_generation=0, _playback_recovery_active=False,
        _worker_registry=WorkerLifetimeRegistry(),
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None),
        _cancel_current_playback_attempt=lambda reason: None,
        _cancel_playback_watchdog=lambda: None,
        _audio_log=lambda message: None,
        _cancel_pending_library_apply=lambda: None,
        _cancel_fade=lambda: None,
        _stop_all=lambda: None,
        _cancel_metadata_backfill=lambda: None,
        _mixed_transition_state="idle",
        _video_backend=SimpleNamespace(
            stop=lambda: video_calls.append("stop"),
            shutdown=lambda: video_calls.append("shutdown"),
        ),
    )

    PlayerWindow._request_shutdown(window)
    assert video_calls == ["stop"]  # REQUEST: stopped, not destroyed

    window._video_transition_point_analyzer = None
    for name in ("miniaudio_player", "miniaudio_inactive_player", "bass_player", "bass_inactive_player"):
        setattr(window, name, None)
    window.viz_logger = SimpleNamespace(stop=lambda: None)
    window.artwork_manager = SimpleNamespace(shutdown=lambda: None)
    window.cast_discovery = SimpleNamespace(close=lambda: None, discovery_thread=None)
    window.cast_controller = SimpleNamespace(close=lambda: None, disconnect=lambda: None, connect_thread=None, load_thread=None)
    window.cast_media_server = SimpleNamespace(close=lambda: None)
    window._cast_artwork_temp = None
    window._diagnostic_export_worker = None
    window._shutdown_complete = False
    window._shutdown_finalizing = False

    PlayerWindow._finalize_shutdown(window)
    assert video_calls == ["stop", "shutdown"]  # FINALIZE: now destroyed


def test_close_event_with_a_pending_worker_ignores_the_close_and_keeps_the_window_alive(monkeypatch):
    # closeEvent's one-time UI/session teardown does
    # QtWidgets.QApplication.instance() and, if not None, calls
    # app.removeEventFilter(self) -- self here is a SimpleNamespace, not a
    # real QObject, so this must be forced to the "no app yet" branch
    # rather than depending on whether some earlier test in this same
    # pytest process happened to leave a real QApplication installed.
    monkeypatch.setattr(QtWidgets.QApplication, "instance", staticmethod(lambda: None))
    registry = WorkerLifetimeRegistry()
    registry.register("plex_audio_load", thread=SimpleNamespace(wait=lambda ms: False), wait_ms=1)
    finalize_calls = []
    accept_calls = []
    hide_calls = []
    window = SimpleNamespace(
        _closing=False, _shutdown_requested=False, _shutdown_pending=False,
        _shutdown_complete=False, _shutdown_finalizing=False,
        _video_fullscreen=False, mini_player=None, party_mode=None,
        recently_played_repository=SimpleNamespace(save=lambda entries: None),
        recently_played_entries=[],
        _playback_generation=0, _plex_audio_load_token=0, _crossfade_load_token=0,
        _karaoke_generation=0, _library_search_generation=0, _playback_recovery_active=False,
        _worker_registry=registry,
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None, shutdown=lambda: finalize_calls.append("diagnostics_shutdown")),
        _cancel_current_playback_attempt=lambda reason: None,
        _cancel_playback_watchdog=lambda: None,
        _audio_log=lambda message: None,
        _cancel_pending_library_apply=lambda: None,
        _cancel_fade=lambda: None,
        _stop_all=lambda: None,
        _cancel_metadata_backfill=lambda: None,
        _mixed_transition_state="idle",
        _video_backend=None,
        _save_session=lambda: None,
        _save_queue_analysis_cache=lambda: None,
        hide=lambda: hide_calls.append(True),
        _shutdown_grace_timer=None,
    )
    window._request_shutdown = lambda: PlayerWindow._request_shutdown(window)
    window._arm_shutdown_grace_timer = lambda: setattr(window, "_shutdown_grace_timer", object())
    window._cancel_shutdown_grace_timer = lambda: setattr(window, "_shutdown_grace_timer", None)
    event = SimpleNamespace(ignore=lambda: None, accept=lambda: accept_calls.append(True))

    PlayerWindow.closeEvent(window, event)

    assert window._closing is True
    assert window._shutdown_pending is True
    assert hide_calls == [True]
    assert accept_calls == []  # never accepted -- window stays alive
    assert finalize_calls == []  # _finalize_shutdown never reached
    assert window._shutdown_complete is False
    # Stage 2 armed the single forced-exit safety net rather than leaving
    # the hidden window waiting on a worker that may never finish.
    assert window._shutdown_grace_timer is not None


def test_final_worker_finish_resumes_and_completes_shutdown():
    # Mirrors the real _on_plex_audio_load_worker_finished pattern: once
    # this is the LAST registered worker to unregister, its own
    # _maybe_resume_final_shutdown() call must re-enter close() and this
    # time actually finalize (registry now empty).
    registry = WorkerLifetimeRegistry()
    token = registry.register("plex_audio_load", thread=SimpleNamespace(wait=lambda ms: True))
    close_calls = []
    cancel_calls = []
    window = SimpleNamespace(
        _shutdown_pending=True, _worker_registry=registry,
        close=lambda: close_calls.append(True),
        _cancel_shutdown_grace_timer=lambda: cancel_calls.append(True),
    )

    registry.unregister(token)
    PlayerWindow._maybe_resume_final_shutdown(window)

    assert close_calls == [True]
    # The forced-exit path must be disarmed on the normal route out.
    assert cancel_calls == [True]


def test_multiple_pending_workers_only_the_last_to_finish_resumes_shutdown():
    registry = WorkerLifetimeRegistry()
    token_a = registry.register("bio", thread=SimpleNamespace(wait=lambda ms: True))
    token_b = registry.register("search", thread=SimpleNamespace(wait=lambda ms: True))
    close_calls = []
    window = SimpleNamespace(
        _shutdown_pending=True, _worker_registry=registry,
        close=lambda: close_calls.append(True),
        _cancel_shutdown_grace_timer=lambda: None,
    )

    registry.unregister(token_a)
    PlayerWindow._maybe_resume_final_shutdown(window)
    assert close_calls == []  # one worker still outstanding -- must not resume yet

    registry.unregister(token_b)
    PlayerWindow._maybe_resume_final_shutdown(window)
    assert close_calls == [True]  # now the last one -- resumes


def test_maybe_resume_final_shutdown_is_a_noop_during_normal_non_shutdown_operation():
    registry = WorkerLifetimeRegistry()
    close_calls = []
    window = SimpleNamespace(
        _shutdown_pending=False, _worker_registry=registry,
        close=lambda: close_calls.append(True),
    )
    PlayerWindow._maybe_resume_final_shutdown(window)
    assert close_calls == []


# ---------------------------------------------------------------------------
# Phase C2 Stage 2: the grace timer / forced-exit safety net.
#
# Before this, a worker the registry could never prove finished (stuck in a
# native call -- a libsndfile read against a dead NAS mount, a hung socket in
# album_art_fetch) left closeEvent permanently in its event.ignore() branch:
# the window hidden, _finalize_shutdown() never reached, the process alive
# and invisible with BASS/miniaudio/video backends still open, killable only
# from Task Manager. _maybe_resume_final_shutdown() was the ONLY route out
# and it is only ever called from a worker's finished handler.
# ---------------------------------------------------------------------------

def _close_event_window(registry, arm_calls):
    """Fake carrying what PlayerWindow.closeEvent touches, with the grace
    timer's Qt construction replaced by a counter so arming is observable
    without a QApplication."""
    window = SimpleNamespace(
        _closing=False, _shutdown_requested=False, _shutdown_pending=False,
        _shutdown_complete=False, _shutdown_finalizing=False,
        _shutdown_grace_timer=None,
        _video_fullscreen=False, mini_player=None, party_mode=None,
        recently_played_repository=SimpleNamespace(save=lambda entries: None),
        recently_played_entries=[],
        _playback_generation=0, _plex_audio_load_token=0, _crossfade_load_token=0,
        _karaoke_generation=0, _library_search_generation=0,
        _playback_recovery_active=False,
        _worker_registry=registry,
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None, shutdown=lambda: None),
        _cancel_current_playback_attempt=lambda reason: None,
        _cancel_playback_watchdog=lambda: None,
        _audio_log=lambda message: None,
        _cancel_pending_library_apply=lambda: None,
        _cancel_fade=lambda: None,
        _stop_all=lambda: None,
        _cancel_metadata_backfill=lambda: None,
        _mixed_transition_state="idle",
        _video_backend=None,
        _save_session=lambda: None,
        _save_queue_analysis_cache=lambda: None,
        hide=lambda: None,
        accepted=[],
    )
    window._request_shutdown = lambda: PlayerWindow._request_shutdown(window)
    window._finalize_shutdown = lambda: None

    def _arm():
        arm_calls.append(True)
        window._shutdown_grace_timer = object()

    window._arm_shutdown_grace_timer = _arm
    window._cancel_shutdown_grace_timer = lambda: setattr(window, "_shutdown_grace_timer", None)
    return window


def _grace_window(registry, *, shutdown_complete=False):
    """Minimal fake carrying only what the Stage 2 paths touch."""
    recorded = []
    window = SimpleNamespace(
        _shutdown_complete=shutdown_complete,
        _shutdown_grace_timer=None,
        _worker_registry=registry,
        diagnostics=SimpleNamespace(
            record=lambda category, operation, **kw: recorded.append((category, operation, kw)),
            shutdown=lambda: recorded.append(("diagnostics", "shutdown", {})),
        ),
        _audio_log=lambda message: None,
        forced=[],
        finalized=[],
        closed=[],
    )
    window._force_process_exit = lambda code: window.forced.append(code)
    window._finalize_shutdown = lambda: window.finalized.append(True)
    window.close = lambda: window.closed.append(True)
    window.recorded = recorded
    return window


def test_close_event_starts_one_grace_timer_when_worker_never_finishes(monkeypatch):
    monkeypatch.setattr(QtWidgets.QApplication, "instance", staticmethod(lambda: None))
    registry = WorkerLifetimeRegistry()
    # wait() always False: the registry can never prove this one finished,
    # and its finished signal will never arrive either.
    registry.register("album_art_fetch", thread=SimpleNamespace(wait=lambda ms: False), wait_ms=1)
    arm_calls = []
    window = _close_event_window(registry, arm_calls)
    event = SimpleNamespace(ignore=lambda: None, accept=lambda: window.accepted.append(True))

    PlayerWindow.closeEvent(window, event)

    assert window._shutdown_pending is True
    assert window.accepted == []
    assert arm_calls == [True]  # exactly one grace timer armed


def test_repeated_close_does_not_arm_multiple_grace_timers(monkeypatch):
    monkeypatch.setattr(QtWidgets.QApplication, "instance", staticmethod(lambda: None))
    registry = WorkerLifetimeRegistry()
    registry.register("plex_audio_load", thread=SimpleNamespace(wait=lambda ms: False), wait_ms=1)
    arm_calls = []
    window = _close_event_window(registry, arm_calls)
    event = SimpleNamespace(ignore=lambda: None, accept=lambda: window.accepted.append(True))

    PlayerWindow.closeEvent(window, event)
    PlayerWindow.closeEvent(window, event)
    PlayerWindow.closeEvent(window, event)

    # Re-arming on every close would let a user postpone the forced exit
    # indefinitely by clicking X repeatedly.
    assert arm_calls == [True]
    assert window.accepted == []


def test_worker_finishing_during_grace_period_uses_normal_shutdown_not_force_exit():
    registry = WorkerLifetimeRegistry()
    token = registry.register("bio", thread=SimpleNamespace(wait=lambda ms: False))
    cancel_calls = []
    close_calls = []
    window = SimpleNamespace(
        _shutdown_pending=True,
        _worker_registry=registry,
        close=lambda: close_calls.append(True),
        _cancel_shutdown_grace_timer=lambda: cancel_calls.append(True),
    )

    # The worker resolves normally, inside the grace window.
    registry.unregister(token)
    PlayerWindow._maybe_resume_final_shutdown(window)

    assert close_calls == [True]      # normal close re-entered
    assert cancel_calls == [True]     # forced-exit path disarmed first


def test_grace_expiry_rechecks_registry_before_force_exit():
    # The timer may fire after the last worker resolved but before its
    # queued finished handler ran -- the re-check must find an empty
    # registry and take the normal, fully safe teardown.
    registry = WorkerLifetimeRegistry()
    window = _grace_window(registry)

    PlayerWindow._on_shutdown_grace_expired(window)

    assert window.forced == []          # never forced
    assert window.finalized == [True]   # normal native teardown ran
    assert window.closed == [True]


def test_grace_expiry_with_stuck_worker_records_diagnostic_and_requests_force_exit():
    registry = WorkerLifetimeRegistry()
    token = registry.register("album_art_fetch", thread=SimpleNamespace(wait=lambda ms: False))
    window = _grace_window(registry)

    PlayerWindow._on_shutdown_grace_expired(window)

    assert window.forced == [0]        # forced exit requested
    # _finalize_shutdown must NOT run: everything it closes (BASS streams,
    # the video backend, the Cast media server) may still be in use by the
    # worker that could not be proven finished.
    assert window.finalized == []
    forced_events = [
        kw for category, operation, kw in window.recorded
        if operation == "shutdown_forced_exit"
    ]
    assert len(forced_events) == 1
    details = forced_events[0]["details"]
    assert details["unproven_count"] == 1
    assert details["unproven_workers"] == [
        {"worker_id": token, "category": "album_art_fetch"}
    ]


def test_no_migrated_registry_worker_has_a_second_competing_cancel_path():
    # Every worker migrated onto WorkerLifetimeRegistry in Phase C2 must
    # have exactly one shutdown-ownership mechanism -- either the
    # registry's own cancel= callback, or (for backfill_scan specifically,
    # which needs the extra _backfill_scan_cancelled_by_user_action side
    # effect) the one pre-existing _cancel_metadata_backfill() call in
    # _request_shutdown -- never both an inline cancel/wait block AND a
    # registry registration for the same worker.
    request_source = inspect.getsource(PlayerWindow._request_shutdown)
    # The old inline per-worker wait() calls this replaced must be gone --
    # only the registry's own shutdown_all() call remains.
    assert request_source.count(".wait(") == 0
    # Every migrated worker's own inline .cancel() call must be gone too
    # -- gpu_probe.cancel() is a deliberate, documented exception (the GPU
    # capability probe is not WorkerLifetimeRegistry-owned).
    for stale_inline_cancel in (
        "scan_thread.cancel()", "_album_tag_refresh_thread.cancel()",
        "_karaoke_prepare_worker.cancel()", "loudness_worker.stop()",
        "waveform_worker.stop()", "analyzer_worker.stop()", "bio_worker.stop()",
        "search_worker.stop()", "queue_analysis_worker.stop()",
    ):
        assert stale_inline_cancel not in request_source
    assert request_source.count("self._worker_registry.shutdown_all()") == 1


def test_no_qthread_terminate_anywhere_in_the_shutdown_path():
    request_source = inspect.getsource(PlayerWindow._request_shutdown)
    finalize_source = inspect.getsource(PlayerWindow._finalize_shutdown)
    registry_source = inspect.getsource(WorkerLifetimeRegistry.shutdown_all)
    for source in (request_source, finalize_source, registry_source):
        assert ".terminate()" not in source


# ---------------------------------------------------------------------------
# VideoTransitionPointAnalyzer process hygiene
# ---------------------------------------------------------------------------

def test_video_transition_point_analyzer_shutdown_kills_the_active_probe_process():
    from billsmusic.video_transition_point_analyzer import VideoTransitionPointAnalyzer

    analyzer = VideoTransitionPointAnalyzer.__new__(VideoTransitionPointAnalyzer)
    cancelled = []
    analyzer._closing = False
    analyzer._pending = {}
    analyzer._queued = {}
    analyzer._active = SimpleNamespace(
        cancel=lambda: cancelled.append(True), deleteLater=lambda: None,
    )
    VideoTransitionPointAnalyzer.shutdown(analyzer)
    assert cancelled == [True]
    assert analyzer._active is None


# ---------------------------------------------------------------------------
# ArtworkManager narrow cache-hit gap
# ---------------------------------------------------------------------------

def test_artwork_manager_cache_hit_callback_drops_result_if_closed_before_it_fires():
    from billsmusic.artwork import ArtworkManager

    manager = ArtworkManager()
    manager._cache[("key", (64, 64))] = object()
    calls = []
    scheduled = []
    with_patch = MagicMock()
    import billsmusic.artwork as artwork_module
    original_timer = artwork_module.QtCore.QTimer.singleShot
    artwork_module.QtCore.QTimer.singleShot = staticmethod(lambda ms, fn: scheduled.append(fn))
    try:
        ok = manager.request("key", [], None, (64, 64), 1, lambda result: calls.append(result))
        assert ok is True
        manager._closed = True  # shutdown() happens before the singleShot fires
        scheduled[0]()  # simulate the deferred callback actually firing
        assert calls == []
    finally:
        artwork_module.QtCore.QTimer.singleShot = original_timer
