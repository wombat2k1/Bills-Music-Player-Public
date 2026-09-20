"""Astra F2, Phase 4.1: automatic work already in flight when Pause happens
stays automatic, and must respect Pause until it reaches a terminal point.

Phase 4 gated where automatic progression STARTS. These cover continuations
of work that had already started:
1. a video->video overlay transition whose animation completes while paused;
2. GPU dual-video secondary readiness that arrives while paused;
3. an automatic advance to a Plex track whose resolve completes while paused;
4. an Audio->Video preparation failure whose hard-cut fallback runs paused;
5. a delayed crossfade-begin callback that binds to a newer crossfade load;
6. delayed Cast receiver status overwriting the user's intentional Pause.

They drive the real engines, timers and callbacks (VideoTransitionManager with
the real overlay animation, DualVideoTransitionEngine, the Plex resolve/load
callbacks, the mixed-transition failure path, the A-A crossfade scheduler,
Cast _tick) together with the real pause()/Resume toggle, _next_track and the
queue claim / PlaybackAttempt machinery.
"""
import gc
import inspect
import os
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets, sip

import billsmusic.window as window_module
from billsmusic.media_type import MediaType
from billsmusic.playback_attempt import PlaybackAttemptState
from billsmusic.plex_transport import PlexTransportSource
from billsmusic.video_dual_transition import (
    DualDeckState, DualTransitionPreferences, DualVideoTransitionEngine,
)
from billsmusic.video_transition import (
    TransitionState, VideoTransitionManager, VideoTransitionPreferences,
)
from billsmusic.video_transition_overlay import VideoTransitionOverlay
from billsmusic.window import PlayerWindow

import test_mixed_media_audio_video_transitions as mixed_harness
import test_pause_freezes_progression as f2
import test_plex_stage3a_dispatch as plex_harness
from test_mixed_media_audio_video_transitions import _recorded_ops, _with_real_stop_playback
from test_video_dual_transition import FakeDualBackend
from test_video_transition_manager import FakeOverlay

_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def _no_real_gain_lookup_threads(monkeypatch):
    monkeypatch.setattr(window_module, "GainLookupWorker", mixed_harness._InertGainLookupWorker)


@pytest.fixture
def dispose_qt():
    """Tear the parentless Qt objects a test creates (engines with live
    timers, a top-level overlay, its host) down on the GUI thread, now --
    left to garbage collection they can be destroyed later on another
    thread, which crashes the worker."""
    owned = []
    yield owned.append
    for obj in reversed(owned):
        try:
            shutdown = getattr(obj, "shutdown", None)
            if shutdown is not None:
                shutdown()
        except Exception:
            pass
    for obj in reversed(owned):
        try:
            if not sip.isdeleted(obj):
                sip.delete(obj)
        except Exception:
            pass
    gc.collect()


def _spin(seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QtCore.QCoreApplication.processEvents()
        time.sleep(0.005)
    QtCore.QCoreApplication.processEvents()


def _pause_provider_kwargs(cls, window):
    """The production wiring hands the engines the application's Pause
    authority; on a build without that seam the engines run unguarded."""
    if "automatic_progress_suspended" in inspect.signature(cls.__init__).parameters:
        return {"automatic_progress_suspended": lambda: window_module._automatic_progression_suspended_for(window)}
    return {}


# -- 1 + 2: video -> video ----------------------------------------------------

def _video_window(tmp_path):
    paths = []
    for name in ("a.mp4", "b.mp4"):
        path = tmp_path / name
        path.write_bytes(b"x")
        paths.append(str(path))
    window = f2._pausable(mixed_harness._window(
        track_transition_mode="normal", current_path=paths[0], queue=list(paths),
        queue_played=[True, False], track_index_by_path={paths[0]: 0, paths[1]: 1},
        _current_media_type=MediaType.VIDEO,
    ))
    window._video_backend._duration_ms = 10_000
    window._current_playback_attempt = None
    window._next_playback_attempt_id = 1
    f2._bind(
        window, "_advance_video_transition", "_peek_next_queue_identity_for_dual_transition_pure",
        "_begin_playback_attempt", "_advance_playback_attempt_state", "_cancel_current_playback_attempt",
    )
    window.paths = paths
    return window


def _b_dispatched(window):
    return window._video_backend.load_calls


def test_video_overlay_transition_freezes_while_paused_and_completes_once_after(tmp_path, dispose_qt):
    window = _video_window(tmp_path)
    host = QtWidgets.QWidget()
    dispose_qt(host)
    host.resize(320, 180)
    overlay = VideoTransitionOverlay()
    dispose_qt(overlay)
    manager = VideoTransitionManager(
        None, lambda stage, media_type: host, window._advance_video_transition,
        window._peek_next_media_type_for_transition, lambda event, details: None,
        overlay=overlay, **_pause_provider_kwargs(VideoTransitionManager, window),
    )
    dispose_qt(manager)
    manager.configure(VideoTransitionPreferences(
        style="Fade Black", duration_seconds=1.0, automatic_lead_seconds=1.0,
    ))
    window._video_transition_manager = manager
    window._on_video_position_changed(9_400)  # the automatic overlay begins (600 ms outgoing)
    assert manager.state == TransitionState.OUTGOING
    _spin(0.2)

    window.pause()
    _spin(1.5)  # far longer than the remaining animation

    assert manager.state == TransitionState.OUTGOING
    assert _b_dispatched(window) == []  # B never loaded
    assert window.current_path == window.paths[0]
    assert window._current_playback_attempt is None  # no attempt for B
    assert window.queue_played == [True, False] and not getattr(window, "_queue_entry_claims", {})
    assert window._playback_intentionally_paused is True

    window.pause()  # Resume
    _spin(0.1)
    assert _b_dispatched(window) == []  # continues where it paused, not done at once
    _spin(1.0)
    assert _b_dispatched(window) == [window.paths[1]]  # completes exactly once
    assert window._current_playback_attempt.source_identity == window.paths[1]


def _gpu_window(tmp_path, dispose_qt):
    window = _video_window(tmp_path)
    backend = FakeDualBackend()
    engine = DualVideoTransitionEngine(
        None, backend, window._advance_video_transition,
        window._peek_next_queue_identity_for_dual_transition_pure,
        lambda event, details: None,
        staleness_identity_provider=window._peek_next_queue_identity_for_dual_transition_pure,
        **_pause_provider_kwargs(DualVideoTransitionEngine, window),
    )
    dispose_qt(engine)
    engine.configure(DualTransitionPreferences(
        enabled=True, preload_lead_seconds=6.0, automatic_lead_seconds=1.0, duration_seconds=1.0,
    ))
    manager = VideoTransitionManager(
        None, lambda stage, media_type: None, window._advance_video_transition,
        window._peek_next_media_type_for_transition, lambda event, details: None,
        overlay=FakeOverlay(), dual_engine=engine,
        **_pause_provider_kwargs(VideoTransitionManager, window),
    )
    dispose_qt(manager)
    manager.configure(VideoTransitionPreferences(
        style="Fade Black", duration_seconds=1.0, automatic_lead_seconds=1.0,
    ))
    window._video_transition_manager = manager
    return window, engine, backend


def test_gpu_secondary_ready_while_paused_is_held_until_resumed(tmp_path, dispose_qt):
    window, engine, backend = _gpu_window(tmp_path, dispose_qt)
    window._on_video_position_changed(8_900)  # 1.1 s left: the secondary preloads
    assert engine.state == DualDeckState.PRELOADING_SECONDARY

    window.pause()
    engine.on_secondary_ready()  # readiness arrives while paused
    _spin(0.4)

    assert engine.state == DualDeckState.SECONDARY_READY  # retained
    assert engine._deadline_timer.isActive() is False  # no automatic timing armed
    assert backend.committed_durations == []  # no GPU transition
    assert _b_dispatched(window) == [] and window._current_playback_attempt is None
    assert window._playback_intentionally_paused is True

    window.pause()  # Resume
    _spin(0.4)
    assert backend.committed_durations == [1000]  # begins exactly once
    assert backend.preloaded_paths == [window.paths[1]]  # not prepared again
    assert window._current_playback_attempt.source_identity == window.paths[1]


def test_gpu_readiness_held_through_a_long_pause_does_not_commit_at_once(tmp_path, dispose_qt):
    window, engine, backend = _gpu_window(tmp_path, dispose_qt)
    window._on_video_position_changed(4_500)  # 5.5 s left
    window.pause()
    engine.on_secondary_ready()
    assert engine._deadline_timer.isActive() is False

    window.pause()  # Resume after however long: timing restarts from the paused position
    assert engine._deadline_timer.interval() == 4_500
    _spin(0.1)
    assert backend.committed_durations == []


def test_stop_while_gpu_readiness_is_held_cannot_be_revived(tmp_path, dispose_qt):
    window, engine, backend = _gpu_window(tmp_path, dispose_qt)
    _with_real_stop_playback(window)
    window._on_video_position_changed(8_900)
    window.pause()
    engine.on_secondary_ready()

    window.stop_playback()
    engine.on_secondary_ready()  # a late duplicate readiness
    window.pause()
    window.pause()
    _spin(0.4)

    assert engine.state == DualDeckState.IDLE
    assert backend.committed_durations == [] and backend.cancelled_count >= 1
    assert _b_dispatched(window) == []


# -- 3: automatic Plex advancement ---------------------------------------------

PLEX_AUDIO = plex_harness.AUDIO_IDENTITY


def _plex_window(monkeypatch, tmp_path):
    """A local track near its end (still playing), a Plex track queued next,
    and the real Plex resolve/load dispatch with controllable workers."""
    window = f2._crossfade_window(monkeypatch, tmp_path)
    monkeypatch.setattr(window_module, "PlexPlaybackResolveWorker", plex_harness._FakeResolveWorker)
    monkeypatch.setattr(window_module, "BassStreamPrepareWorker", plex_harness._FakeAudioLoadWorker)
    monkeypatch.setattr(window_module, "_BassEngine", plex_harness._FakeBassEngine)
    plex_harness._FakeResolveWorker.instances = []
    plex_harness._FakeAudioLoadWorker.instances = []
    window.queue[0] = PLEX_AUDIO
    template = plex_harness.DispatchHarness()
    for key in (
        "plex_preferences", "_plex_active_connection_uri", "_plex_active_access_token",
        "_plex_resolve_worker", "_plex_resolve_pending_kind", "_plex_resolve_pending_index",
        "_plex_audio_load_worker", "_plex_audio_load_token", "video_playback_enabled",
        "_video_backend", "_muted", "label_remaining",
    ):
        setattr(window, key, getattr(template, key))
    f2._bind(
        window, "_plex_resolved_connection_for_identity", "_plex_effective_connection",
        "_start_plex_playback_resolve", "_play_plex_audio_path_direct", "_on_plex_playback_resolved",
        "_fail_plex_playback_resolve", "_terminalise_video_transition_for_failed_incoming",
        "_start_plex_audio_playback", "_on_plex_audio_load_prepared", "_on_plex_audio_load_failed",
        "_on_plex_resolve_worker_finished", "_on_plex_audio_load_worker_finished",
    )
    window._sync_now_playing_overlay_for_media_type = lambda: None
    return window


def _resolve_result(worker):
    source = PlexTransportSource(
        identity=PLEX_AUDIO, transport_url="http://plex-host:32400/part.mp3",
        extra_headers={"X-Plex-Token": "REAL-TOKEN"},
    )
    return {"success": True, "identity": PLEX_AUDIO, "generation": worker.kwargs["generation"],
            "transport_source": source}


def _deliver_all_plex_results(window):
    """Every outstanding resolve and load result, including any a delivered
    result dispatches in turn."""
    delivered_any = True
    while delivered_any:
        delivered_any = False
        for worker in list(plex_harness._FakeResolveWorker.instances):
            if not getattr(worker, "delivered", False):
                worker.delivered = delivered_any = True
                worker.finished_result.emit(_resolve_result(worker))
        for worker in list(plex_harness._FakeAudioLoadWorker.instances):
            if not getattr(worker, "delivered", False):
                worker.delivered = delivered_any = True
                candidate = plex_harness._FakePreparedCandidate(PLEX_AUDIO)
                candidate.path = PLEX_AUDIO
                worker.prepared.emit(worker.token, PLEX_AUDIO, candidate)


def test_automatic_plex_advance_resolved_while_paused_is_held_until_resumed(monkeypatch, tmp_path):
    window = _plex_window(monkeypatch, tmp_path)
    local = window.simple_player
    local.pos = local.length - 7.0
    window._tick()  # near-end: automatic advance to the queued Plex track starts resolving
    attempt = window._current_playback_attempt
    assert attempt.source_identity == PLEX_AUDIO and attempt.state is PlaybackAttemptState.PREPARING

    window.pause()
    _deliver_all_plex_results(window)

    assert window._playback_intentionally_paused is True
    assert plex_harness._FakeAudioLoadWorker.instances == []  # nothing loaded or started
    assert local._paused is True and window.current_path.endswith("a.mp3")
    assert attempt.state is PlaybackAttemptState.PREPARING  # not PLAYING
    assert window.queue_played == [False]  # not committed

    window.pause()  # Resume
    _deliver_all_plex_results(window)

    assert attempt.state is PlaybackAttemptState.PLAYING
    assert window.queue_played == [True]
    assert len(plex_harness._FakeResolveWorker.instances) == 1  # not resolved again
    assert len(plex_harness._FakeAudioLoadWorker.instances) == 1  # started exactly once
    assert window.current_path == PLEX_AUDIO


def test_explicit_plex_selection_while_paused_plays(monkeypatch, tmp_path):
    window = _plex_window(monkeypatch, tmp_path)
    window.pause()

    row = window.queue.index(PLEX_AUDIO)
    token, owner = window_module._claim_queue_selection_for(window, row, reason="test")
    assert window._play_path_direct(PLEX_AUDIO, queue_entry_token=token, queue_selection_owner=owner)
    attempt = window._current_playback_attempt
    _deliver_all_plex_results(window)

    assert attempt.state is PlaybackAttemptState.PLAYING
    assert window._playback_intentionally_paused is False
    assert window.queue_played == [True]


def test_stop_discards_a_held_automatic_plex_result(monkeypatch, tmp_path):
    window = _plex_window(monkeypatch, tmp_path)
    f2._bind(window, "stop_playback", "_cancel_current_playback_attempt", "_cancel_fade")
    for key, value in dict(
        btn_pause=SimpleNamespace(setText=lambda t: None, setAccessibleName=lambda t: None),
        _sync_now_playing_overlay_for_media_type=lambda: None,
    ).items():
        setattr(window, key, value)
    local = window.simple_player
    local.pos = local.length - 7.0
    window._tick()
    attempt = window._current_playback_attempt
    window.pause()
    _deliver_all_plex_results(window)

    window.stop_playback()
    # Later: play again and pause/resume -- the held result must not start.
    local.play()
    window.pause()
    window.pause()
    _deliver_all_plex_results(window)

    assert plex_harness._FakeAudioLoadWorker.instances == []
    assert attempt.state is PlaybackAttemptState.CANCELLED
    assert window.queue_played == [False] and window._queue_entry_claims == {}


# -- 4: Audio->Video preparation failure ----------------------------------------

def _failing_audio_to_video(monkeypatch):
    clock = f2._patch_clock(monkeypatch)
    hard_cuts = []
    window = f2._pausable(mixed_harness._window(
        _play_path_direct=lambda path, **kw: hard_cuts.append((path, kw.get("queue_entry_token"))) or True,
    ))
    window.simple_player.playing = True
    window._next_track("quiet-end")  # automatic Audio->Video preparation
    assert window._mixed_transition_state == "preparing"
    return clock, window, hard_cuts


def test_audio_to_video_failure_while_paused_does_not_hard_cut_until_resumed(monkeypatch):
    clock, window, hard_cuts = _failing_audio_to_video(monkeypatch)
    token = window._queue_entry_tokens[1]
    window.pause()

    window._on_video_error("video_decode_error", "could not decode")
    f2._drive_ticks(window, clock, 200)

    assert hard_cuts == []  # no hard-cut while paused
    assert window._playback_intentionally_paused is True
    assert window._current_media_type == MediaType.AUDIO and window.simple_player._paused
    assert window.queue_played == [True, False]
    assert token in window._queue_entry_claims  # still reserved for the held fallback

    window.pause()  # Resume
    window.pause()
    window.pause()  # further toggles do not repeat it
    assert hard_cuts == [("incoming.mp4", token)]


def test_stop_discards_a_held_audio_to_video_fallback(monkeypatch):
    clock, window, hard_cuts = _failing_audio_to_video(monkeypatch)
    _with_real_stop_playback(window)
    window.pause()
    window._on_video_error("video_decode_error", "could not decode")

    window.stop_playback()
    f2._drive_ticks(window, clock, 200)

    assert hard_cuts == []
    assert window._mixed_transition_state == "idle"
    assert not window._queue_entry_claims and window.queue_played == [True, False]


def test_held_audio_to_video_fallback_never_replays_an_invalidated_target(monkeypatch):
    clock, window, hard_cuts = _failing_audio_to_video(monkeypatch)
    token = window._queue_entry_tokens[1]
    window.pause()
    window._on_video_error("video_decode_error", "could not decode")

    window.queue[1] = "replacement.mp4"  # missing-track repair: same token, new source

    window.pause()  # Resume
    assert hard_cuts == []  # the stale target is never played (Astra F5)
    assert token not in window._queue_entry_claims
    assert window._mixed_transition_state == "idle"


# -- 5: delayed crossfade begin -------------------------------------------------

def _delayed_crossfade(monkeypatch, tmp_path):
    window = f2._crossfade_window(monkeypatch, tmp_path)
    f2._bind(window, "_cancel_fade")
    c_path = tmp_path / "c.mp3"
    c_path.write_bytes(b"x")
    window.c_path = str(c_path)
    return window


def _prepare_b_with_delayed_fade(window):
    """B is selected with ~0.15 s of A left before its fade window, so once
    prepared its fade begin is scheduled on a real timer, not run at once."""
    outgoing = window.simple_player
    outgoing.pos = outgoing.length - (window.crossfade_seconds + 0.15)
    b_path = window.queue[0]
    assert window._play_path_direct(b_path, crossfade=True)  # an explicit crossfaded selection
    worker = window._crossfade_load_worker
    worker.prepared.emit(worker.token, b_path, f2._FakePreparedCandidate(b_path))
    assert window.simple_inactive_player.playing is True and window.fade_active is False
    return b_path


def test_stale_delayed_crossfade_begin_cannot_bind_to_a_newer_load(monkeypatch, tmp_path):
    window = _delayed_crossfade(monkeypatch, tmp_path)
    incoming = window.simple_inactive_player
    _prepare_b_with_delayed_fade(window)

    window._cancel_fade()  # B's crossfade is abandoned (its timer is still pending)
    c_path = window.c_path
    assert window._play_path_direct(c_path, crossfade=True)  # a new selection: C is preparing
    assert incoming.playing is False  # the C load stopped the inactive player
    activated_before = list(window.activated)

    _spin(0.4)  # B's old timer fires

    assert incoming.playing is False  # B was not restarted
    assert window.fade_active is False
    assert window.prebuffer_active is True and window.pending_builtin_crossfade_path == c_path
    assert window.activated == activated_before


def test_delayed_crossfade_begin_for_the_current_load_still_begins(monkeypatch, tmp_path):
    window = _delayed_crossfade(monkeypatch, tmp_path)
    b_path = _prepare_b_with_delayed_fade(window)

    _spin(0.4)

    assert window.fade_active is True
    assert window.activated[-1] == b_path


def test_delayed_crossfade_begin_held_by_pause_then_superseded_is_harmless(monkeypatch, tmp_path):
    window = _delayed_crossfade(monkeypatch, tmp_path)
    incoming = window.simple_inactive_player
    _prepare_b_with_delayed_fade(window)
    window.pause()
    _spin(0.4)  # B's timer fires while paused
    assert window.fade_active is False

    window._cancel_fade()
    window.simple_player.resume()
    window._playback_intentionally_paused = False
    c_path = window.c_path
    assert window._play_path_direct(c_path, crossfade=True)
    window.pause()
    window.pause()  # Resume

    assert window.fade_active is False and incoming.playing is False
    assert window.pending_builtin_crossfade_path == c_path


# -- 6: Cast receiver status vs intentional Pause --------------------------------

def _cast_window():
    receiver = {"snapshot": {"state": "playing", "idle_reason": "", "position": 170.0, "duration": 180.0}}
    controller = SimpleNamespace(
        snapshot=lambda: dict(receiver["snapshot"]), play=lambda: None, pause=lambda: None,
    )
    window = f2._pausable(mixed_harness._window(
        cast_active=True, cast_controller=controller, _cast_completion_armed=True,
        _cast_loss_reported=False, _cast_last_state="playing",
        _record_cast_clock_snapshot=lambda snapshot: None,
    ))
    requests = f2._record_next_track(window)
    return window, receiver, requests


PLAYING = {"state": "playing", "idle_reason": "", "position": 171.0, "duration": 180.0}
FINISHED = {"state": "idle", "idle_reason": "finished", "position": 180.0, "duration": 180.0}


def test_stale_receiver_playing_status_cannot_undo_pause_or_advance():
    window, receiver, requests = _cast_window()
    window.pause()

    receiver["snapshot"] = PLAYING  # delayed status from before the pause
    window._tick()
    receiver["snapshot"] = FINISHED
    window._tick()

    assert window._playback_intentionally_paused is True
    assert requests == []


def test_receiver_finished_after_explicit_resume_advances():
    window, receiver, requests = _cast_window()
    window.pause()
    receiver["snapshot"] = PLAYING
    window._tick()

    window.pause()  # Resume
    receiver["snapshot"] = FINISHED
    window._tick()

    assert requests == ["cast-ended"]


def test_receiver_status_without_pause_still_drives_completion():
    window, receiver, requests = _cast_window()
    window._cast_completion_armed = False
    receiver["snapshot"] = PLAYING
    window._tick()
    assert window._cast_completion_armed is True
    receiver["snapshot"] = FINISHED
    window._tick()
    assert requests == ["cast-ended"]
