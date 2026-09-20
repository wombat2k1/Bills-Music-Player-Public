"""Phase 8: shutdown and native resource lifetime.

Closing the application is the one moment when every native resource the
session owns -- BASS/miniaudio streams, the video child process, Cast
sockets and the media server, worker threads, Qt timers -- is released at
once, while playback machinery may still be mid-flight. These tests drive
the real closeEvent()/_request_shutdown()/_finalize_shutdown() split
against the playback states the earlier phases introduced (an in-flight
PlaybackAttempt and its queue reservation, a built-in or VLC crossfade, a
Pause holding deferred work, a mixed Audio<->Video transition), and assert
the ordering and finality invariants that make shutdown safe:

  - playback is silenced in the REQUEST phase, but no native player or
    backend is destroyed until FINALIZE, which is reached only once every
    registered worker is proven finished;
  - the in-flight attempt, its reservation and every specialist token are
    invalidated BEFORE any native teardown, so nothing a worker delivers
    afterwards can act on a resource that is being freed;
  - work held by Pause (Phase 4) is discarded by shutdown, never replayed;
  - shutdown is final: no late callback restarts playback, commits a queue
    entry or re-opens a closed service;
  - teardown is idempotent -- a second close, or a second finalize, closes
    nothing twice.

Harness convention as elsewhere in this suite (see test_shutdown_hardening.py
and test_mixed_media_audio_video_transitions.py): a SimpleNamespace stands in
for PlayerWindow and PlayerWindow own unbound methods are called against it,
so no real QMainWindow, native player or thread is involved.
"""
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets

from billsmusic.window import PlayerWindow
from billsmusic.worker_registry import WorkerLifetimeRegistry

import test_mixed_media_audio_video_transitions as mixed_harness
import billsmusic.window as window_module
import test_pause_freezes_progression as f2
import test_pause_native_transport as p42
import test_playback_transition_authority as p5
import test_queue_playback_state_consistency as p7


class _RecordingVideoBackend(mixed_harness._FakeVideoBackend):
    """The harness video backend plus the two teardown calls shutdown
    makes, recorded into the shared ordered log."""

    def __init__(self, ops):
        super().__init__()
        self._ops = ops

    def stop(self):
        super().stop()
        self._ops.append("video.stop")

    def shutdown(self):
        self._ops.append("video.shutdown")


class _RecordingNativePlayer:
    """Stands in for a BASS/miniaudio player: close() is the destructive
    call _finalize_shutdown makes and nothing else may make."""

    def __init__(self, ops, name):
        self._ops, self._name = ops, name
        self.closed = 0

    def close(self):
        self.closed += 1
        self._ops.append(self._name + ".close")


def _shutdown_window(ops, **overrides):
    """A window carrying exactly what closeEvent/_request_shutdown/
    _finalize_shutdown touch, with every destructive native call recorded
    into `ops` in the order it happens."""
    registry = overrides.pop("_worker_registry", None) or WorkerLifetimeRegistry()
    window = SimpleNamespace(
        _closing=False,
        _shutdown_requested=False, _shutdown_pending=False,
        _shutdown_finalizing=False, _shutdown_complete=False,
        _shutdown_grace_timer=None,
        _worker_registry=registry,
        _video_fullscreen=False, _video_fullscreen_pending_target=None,
        mini_player=None, party_mode=None,
        recently_played_repository=SimpleNamespace(
            save=lambda entries: ops.append("recently_played.save")),
        recently_played_entries=[],
        _playback_generation=1, _plex_audio_load_token=5,
        _crossfade_load_token=7, _karaoke_generation=2,
        _library_search_generation=3, _playback_recovery_active=True,
        _mixed_transition_state="idle",
        _video_backend=_RecordingVideoBackend(ops),
        _video_transition_manager=None,
        bass_player=_RecordingNativePlayer(ops, "bass"),
        bass_inactive_player=_RecordingNativePlayer(ops, "bass_inactive"),
        miniaudio_player=None, miniaudio_inactive_player=None,
        viz_logger=SimpleNamespace(stop=lambda: ops.append("viz_logger.stop")),
        artwork_manager=SimpleNamespace(
            shutdown=lambda: ops.append("artwork_manager.shutdown")),
        cast_discovery=SimpleNamespace(
            close=lambda: ops.append("cast_discovery.close"), discovery_thread=None),
        cast_controller=SimpleNamespace(
            close=lambda: ops.append("cast_controller.close"),
            disconnect=lambda: ops.append("cast_controller.disconnect"),
            connect_thread=None, load_thread=None),
        cast_media_server=SimpleNamespace(
            close=lambda: ops.append("cast_media_server.close")),
        _cast_artwork_temp=None,
        _diagnostic_export_worker=None,
        diagnostics=SimpleNamespace(
            record=lambda *a, **kw: None,
            shutdown=lambda: ops.append("diagnostics.shutdown")),
        _cancel_current_playback_attempt=lambda reason: ops.append(
            "attempt.cancel:" + reason),
        _cancel_playback_watchdog=lambda: None,
        _cancel_pending_library_apply=lambda: None,
        _cancel_fade=lambda: ops.append("fade.cancel"),
        _stop_all=lambda: ops.append("stop_all"),
        _cancel_metadata_backfill=lambda: None,
        _save_session=lambda: ops.append("session.save"),
        _save_queue_analysis_cache=lambda: None,
        _audio_log=lambda message: None,
        hide=lambda: None,
        close=lambda: None,
    )
    window.__dict__.update(overrides)
    window._request_shutdown = lambda: PlayerWindow._request_shutdown(window)
    window._finalize_shutdown = lambda: PlayerWindow._finalize_shutdown(window)
    window._arm_shutdown_grace_timer = lambda: setattr(
        window, "_shutdown_grace_timer", object())
    window._cancel_shutdown_grace_timer = lambda: setattr(
        window, "_shutdown_grace_timer", None)
    return window


@pytest.fixture(autouse=True)
def _application_seam(monkeypatch):
    """closeEvent removes the window own application-wide event filter.
    A SimpleNamespace is not a QObject, so the real QApplication refuses
    it; this substitutes an application stand-in that records the removal
    instead, keeping that step observable rather than skipped."""
    removed = []
    stub = SimpleNamespace(removeEventFilter=lambda obj: removed.append(obj))
    monkeypatch.setattr(
        QtWidgets.QApplication, "instance", staticmethod(lambda: stub))
    return removed


def _close_event():
    accepted, ignored = [], []
    event = SimpleNamespace(
        accept=lambda: accepted.append(True), ignore=lambda: ignored.append(True))
    return event, accepted, ignored


def _close(window, event):
    """closeEvent ends with super().closeEvent(event) -- QMainWindow own
    handler, which a SimpleNamespace cannot satisfy. Everything this file
    asserts has already happened by that final line, so the TypeError it
    raises against the fake window is swallowed here (and only here),
    exactly as the accept/ignore branches themselves are observed.
    """
    try:
        PlayerWindow.closeEvent(window, event)
    except TypeError as ex:
        if "must be an instance or subtype" not in str(ex):
            raise


_DESTRUCTIVE = {
    "bass.close", "bass_inactive.close", "video.shutdown",
    "cast_media_server.close", "cast_discovery.close", "cast_controller.close",
    "artwork_manager.shutdown", "diagnostics.shutdown",
}


# ---------------------------------------------------------------------------
# Invariant: silence first, destroy last -- and only once every worker that
# could still be inside a native call is proven finished.
# ---------------------------------------------------------------------------

def test_request_shutdown_silences_playback_without_destroying_any_backend():
    ops = []
    window = _shutdown_window(ops)
    PlayerWindow._request_shutdown(window)
    assert "stop_all" in ops and "video.stop" in ops
    assert not (_DESTRUCTIVE & set(ops)), ops


def test_native_teardown_happens_only_after_playback_is_already_silenced():
    ops = []
    window = _shutdown_window(ops)
    event, accepted, _ignored = _close_event()
    _close(window, event)
    assert accepted == [True]
    for destructive in ("bass.close", "video.shutdown", "cast_media_server.close"):
        assert ops.index("stop_all") < ops.index(destructive), ops
        assert ops.index("video.stop") < ops.index(destructive), ops


def test_the_in_flight_attempt_is_invalidated_before_any_native_teardown():
    ops = []
    window = _shutdown_window(ops)
    event, _accepted, _ignored = _close_event()
    _close(window, event)
    assert "attempt.cancel:shutdown" in ops
    for destructive in sorted(_DESTRUCTIVE & set(ops)):
        assert ops.index("attempt.cancel:shutdown") < ops.index(destructive), ops


def test_every_specialist_token_is_invalidated_by_the_request_phase():
    ops = []
    window = _shutdown_window(ops)
    before = (
        window._playback_generation, window._plex_audio_load_token,
        window._crossfade_load_token, window._karaoke_generation,
        window._library_search_generation,
    )
    PlayerWindow._request_shutdown(window)
    after = (
        window._playback_generation, window._plex_audio_load_token,
        window._crossfade_load_token, window._karaoke_generation,
        window._library_search_generation,
    )
    assert all(b != a for b, a in zip(before, after)), (before, after)
    # A recovery that was running is no longer authoritative either.
    assert window._playback_recovery_active is False


def test_an_unproven_worker_keeps_every_native_resource_open():
    ops = []
    registry = WorkerLifetimeRegistry()
    # No thread= handle: a worker with no interruptible join can never be
    # implicitly considered finished, which is exactly the "unproven"
    # state this invariant is about.
    registry.register("album_art_fetch")
    window = _shutdown_window(ops, _worker_registry=registry)
    event, accepted, ignored = _close_event()
    _close(window, event)
    assert ignored == [True] and accepted == []
    assert window._shutdown_pending is True
    # Silenced, but nothing that worker might still be inside is freed.
    assert "stop_all" in ops
    assert not (_DESTRUCTIVE & set(ops)), ops
    assert window._shutdown_complete is False


# ---------------------------------------------------------------------------
# Invariant: cleanup is idempotent -- nothing is closed twice, and the
# one-time session teardown runs once however many times close is requested.
# ---------------------------------------------------------------------------

def test_a_second_finalize_closes_nothing_a_second_time():
    ops = []
    window = _shutdown_window(ops)
    PlayerWindow._finalize_shutdown(window)
    first = list(ops)
    PlayerWindow._finalize_shutdown(window)
    assert ops == first
    assert window.bass_player.closed == 1


def test_closing_twice_saves_the_session_once_and_closes_natives_once():
    ops = []
    window = _shutdown_window(ops)
    event, accepted, _ignored = _close_event()
    _close(window, event)
    _close(window, event)
    assert ops.count("session.save") == 1
    assert ops.count("bass.close") == 1
    assert ops.count("video.shutdown") == 1
    assert ops.count("cast_media_server.close") == 1
    assert accepted == [True, True]


# ---------------------------------------------------------------------------
# Closing while playback is mid-flight. These use the real crossfade engine,
# queue claim and PlaybackAttempt machinery (test_playback_transition_
# authority.py's window), so what shutdown invalidates is the genuine state,
# not a stand-in for it.
# ---------------------------------------------------------------------------

def _closeable(window, ops):
    """Adds what _request_shutdown/_finalize_shutdown touch to one of the
    playback harness windows, recording native teardown into `ops`. The
    playback side (players, queue, claims, attempts, _stop_all, _cancel_fade,
    _cancel_current_playback_attempt) stays exactly as that harness built
    it -- this only supplies the services a real window would own."""
    window._shutdown_requested = False
    window._shutdown_pending = False
    window._shutdown_finalizing = False
    window._shutdown_complete = False
    window._shutdown_grace_timer = None
    window._video_fullscreen = False
    window._video_fullscreen_pending_target = None
    window.mini_player = None
    window.party_mode = None
    window.recently_played_repository = SimpleNamespace(save=lambda entries: None)
    window.recently_played_entries = []
    for name in (
        "_plex_audio_load_token", "_karaoke_generation", "_library_search_generation",
        "_crossfade_load_token",
    ):
        setattr(window, name, getattr(window, name, 0))
    # Keep whichever video backend the playback harness built (the tests
    # assert against its own recording of what was loaded); only its two
    # teardown calls are wrapped into the shared ordered log.
    backend = getattr(window, "_video_backend", None)
    if backend is None:
        window._video_backend = _RecordingVideoBackend(ops)
    else:
        backend_stop = backend.stop

        def _stop(_backend_stop=backend_stop):
            ops.append("video.stop")
            _backend_stop()

        backend.stop = _stop
        backend.shutdown = lambda: ops.append("video.shutdown")
    window.viz_logger = SimpleNamespace(stop=lambda: None)
    window.artwork_manager = SimpleNamespace(shutdown=lambda: None)
    window.cast_discovery = SimpleNamespace(close=lambda: None, discovery_thread=None)
    window.cast_controller = SimpleNamespace(
        close=lambda: None, disconnect=lambda: None,
        connect_thread=None, load_thread=None)
    window.cast_media_server = SimpleNamespace(close=lambda: ops.append("cast_media_server.close"))
    window._cast_artwork_temp = None
    window._diagnostic_export_worker = None
    window._held_crossfade_failure = getattr(window, "_held_crossfade_failure", None)
    window._cancel_pending_library_apply = lambda: None
    window._cancel_metadata_backfill = lambda: None
    window._save_session = lambda: None
    window._save_queue_analysis_cache = lambda: None
    window.diagnostics.shutdown = lambda: None
    window.hide = lambda: None
    window.close = lambda: None
    window._request_shutdown = lambda: PlayerWindow._request_shutdown(window)
    window._finalize_shutdown = lambda: PlayerWindow._finalize_shutdown(window)
    window._arm_shutdown_grace_timer = lambda: setattr(window, "_shutdown_grace_timer", object())
    window._cancel_shutdown_grace_timer = lambda: setattr(window, "_shutdown_grace_timer", None)
    return ops


def test_closing_during_a_builtin_crossfade_discards_the_prepared_load(monkeypatch, tmp_path):
    f2._patch_clock(monkeypatch)
    window = p5._crossfade_queue_window(monkeypatch, tmp_path)
    ops = _closeable(window, [])
    p5._reach_crossfade_window(window)  # the real near-end crossfade to b.mp3
    assert window._crossfade_load_worker is not None
    assert window.dispatches == ["b.mp3"]

    PlayerWindow._request_shutdown(window)
    p5._deliver_prepared(window)  # the off-thread load lands after shutdown

    assert window.commits == []  # nothing marked played by a shutdown crossfade
    assert window.queue_played == [False, False]
    assert window.fade_active is False
    assert window.bass_inactive_player.playing is False
    assert window._queue_entry_claims == {}  # the reservation is released
    assert window._current_playback_attempt is None


def test_a_tick_after_shutdown_never_advances_the_queue(monkeypatch, tmp_path):
    f2._patch_clock(monkeypatch)
    window = p5._crossfade_queue_window(monkeypatch, tmp_path)
    _closeable(window, [])
    PlayerWindow._request_shutdown(window)

    player = window.bass_player
    player.pos = player.length  # at the normal-end threshold
    for _ in range(20):
        window._tick()

    assert window.dispatches == []
    assert window.commits == []
    assert window.queue_played == [False, False]


def test_shutdown_discards_a_crossfade_failure_held_by_a_pause(monkeypatch, tmp_path):
    f2._patch_clock(monkeypatch)
    window = p5._crossfade_queue_window(monkeypatch, tmp_path)
    _closeable(window, [])
    p5._reach_crossfade_window(window)
    worker = window._crossfade_load_worker
    window.pause()
    assert window._playback_intentionally_paused is True

    # The load fails while paused: Phase 6 holds it for Resume. Emitted
    # through the worker's own signal, so the attempt identity the real
    # connection carries is the one the callback validates.
    worker.failed.emit(
        worker.token, window.pending_builtin_crossfade_path,
        "unsupported or corrupt audio",
    )
    assert window._held_crossfade_failure is not None

    PlayerWindow._request_shutdown(window)
    # Whatever happens next, the held failure cannot act: shutdown advanced
    # the crossfade token, so the replay is rejected by identity.
    held = window._held_crossfade_failure
    if held is not None:
        window._on_crossfade_load_failed(*held)

    assert window.commits == []
    assert window.dispatches == ["b.mp3"]  # the pre-shutdown one only
    assert window.queue_played == [False, False]


def test_stop_then_close_commits_nothing_and_closes_each_native_once(monkeypatch, tmp_path):
    f2._patch_clock(monkeypatch)
    window = p5._crossfade_queue_window(monkeypatch, tmp_path)
    ops = _closeable(window, [])
    window._tick()  # playing, with a crossfade to b.mp3 preparing
    window.stop_playback()
    assert window._queue_entry_claims == {}

    PlayerWindow._request_shutdown(window)
    PlayerWindow._finalize_shutdown(window)
    PlayerWindow._finalize_shutdown(window)

    assert ops.count("video.shutdown") == 1
    assert ops.count("cast_media_server.close") == 1
    assert window.commits == []
    assert window._current_playback_attempt is None


def test_closing_during_a_mixed_transition_cancels_it_and_releases_the_claim(monkeypatch, tmp_path):
    f2._patch_clock(monkeypatch)
    window = p5._crossfade_queue_window(monkeypatch, tmp_path)
    _closeable(window, [])
    cancels = []
    window._cancel_mixed_media_transition = lambda reason, *, release_incoming_claim=False: (
        cancels.append((reason, release_incoming_claim)),
        setattr(window, "_mixed_transition_state", "idle"),
    )
    window._mixed_transition_state = "fading"

    PlayerWindow._request_shutdown(window)

    assert cancels == [("shutdown", True)]
    assert window._mixed_transition_state == "idle"


# ---------------------------------------------------------------------------
# Closing while video is the current media. The video child process is the
# one native resource that outlives a crash of its parent, so the order
# "stop the stream, then shut the process down" matters, as does a late
# started/ended event never committing anything afterwards.
# ---------------------------------------------------------------------------

def test_closing_while_a_local_video_plays_stops_it_before_shutting_it_down(monkeypatch, tmp_path):
    window = p7._video_window(monkeypatch, tmp_path)
    ops = _closeable(window, [])
    started, _token = p7._select(window, "v.mp4")
    assert started is True
    window._on_video_started()
    assert window.commits == ["v.mp4"]

    event, _accepted, _ignored = _close_event()
    _close(window, event)

    assert ops.index("video.stop") < ops.index("video.shutdown")
    assert ops.count("video.shutdown") == 1


def test_a_video_started_event_after_shutdown_commits_nothing(monkeypatch, tmp_path):
    window = p7._video_window(monkeypatch, tmp_path)
    _closeable(window, [])
    started, token = p7._select(window, "v.mp4")
    assert started is True and window.commits == []

    PlayerWindow._request_shutdown(window)
    window._on_video_started()  # the child reports playback after we asked to close

    assert window.commits == []
    assert window.queue_played == [False, False]
    assert token not in window._queue_entry_claims  # the reservation was released
    assert window._current_playback_attempt is None


def test_repeated_video_start_and_stop_then_close_leaves_nothing_reserved(monkeypatch, tmp_path):
    window = p7._video_window(monkeypatch, tmp_path)
    ops = _closeable(window, [])
    for _ in range(5):
        p7._select(window, "v.mp4")
        window._on_video_started()
        window.stop_playback()
        window.queue_played = [False, False]  # replay the same entry next time round
        window.commits.clear()

    assert window._queue_entry_claims == {}
    assert window._current_playback_attempt is None

    event, _accepted, _ignored = _close_event()
    _close(window, event)

    # Five start/stop cycles still tear down exactly one of each resource.
    assert ops.count("video.shutdown") == 1
    assert ops.count("cast_media_server.close") == 1


def test_closing_immediately_after_resume_starts_nothing_new(monkeypatch, tmp_path):
    f2._patch_clock(monkeypatch)
    window = p5._crossfade_queue_window(monkeypatch, tmp_path)
    _closeable(window, [])
    window._tick()
    dispatched = list(window.dispatches)
    window.pause()
    window.pause()  # Resume
    assert window._playback_intentionally_paused is False

    PlayerWindow._request_shutdown(window)
    player = window.bass_player
    player.pos = player.length
    for _ in range(10):
        window._tick()

    assert window.dispatches == dispatched  # nothing new dispatched after Resume+close
    assert window.commits == []


# ---------------------------------------------------------------------------
# Closing during the remaining in-flight shapes: a VLC crossfade, a Plex
# resolve that is still outstanding, and a playback recovery.
# ---------------------------------------------------------------------------

def test_closing_during_a_vlc_crossfade_stops_both_players_and_commits_nothing(monkeypatch, tmp_path):
    window, active, inactive = p42._vlc_window(monkeypatch, tmp_path)
    _closeable(window, [])
    row = [os.path.basename(p) for p in window.queue].index("b.mp3")
    token, owner = window_module._claim_queue_selection_for(window, row, reason="test")
    assert window._play_path_direct(
        window.queue[row], crossfade=True,
        queue_entry_token=token, queue_selection_owner=owner) is True
    assert window.prebuffer_active is True  # the incoming player is pre-rolling

    PlayerWindow._request_shutdown(window)

    assert window.fade_active is False and window.prebuffer_active is False
    assert active.playing is False and inactive.playing is False
    assert window.queue_played[row] is False
    assert window._queue_entry_claims == {}


def test_a_plex_resolve_that_lands_after_shutdown_loads_nothing(monkeypatch, tmp_path):
    window = p7._plex_video_window(monkeypatch, tmp_path)
    _closeable(window, [])
    row = window.queue.index(p7.PLEX_VIDEO)
    token, owner = window_module._claim_queue_selection_for(window, row, reason="test")
    assert window._play_path_direct(
        window.queue[row], queue_entry_token=token, queue_selection_owner=owner)
    assert window._video_backend.loaded == []  # still resolving

    PlayerWindow._request_shutdown(window)
    p7._resolve_plex_video(window)  # the resolve completes after we asked to close

    assert window._video_backend.loaded == []  # nothing handed to a closing backend
    assert window.commits == []
    assert window.queue_played[row] is False
    assert window._queue_entry_claims == {}


def test_shutdown_ends_a_playback_recovery_rather_than_continuing_it(monkeypatch, tmp_path):
    f2._patch_clock(monkeypatch)
    window = p5._crossfade_queue_window(monkeypatch, tmp_path)
    _closeable(window, [])
    window._playback_recovery_active = True

    PlayerWindow._request_shutdown(window)

    # No recovery remains authoritative, so nothing it was waiting on can
    # restart playback against a backend that is about to be closed.
    assert window._playback_recovery_active is False
    assert window.commits == []
