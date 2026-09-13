"""Integration test driving the real Phase 2A GPU (Qt Quick/RHI) dual-mode
video subprocess through the actual QML scene and compiled shader in
assets/, using the repo's own tiny fixtures (tests/fixtures/sample.mp4,
sample.mkv) -- not mocked. Complements the mocked-process unit tests in
test_video_backend.py and the pure state-machine tests in
test_video_dual_transition.py; this is the one that proves the real IPC
round-trip through video_subprocess.py's GpuDualDeckVideoSubprocessController
actually works end to end.

Runs under whatever QT_QPA_PLATFORM this test session uses (offscreen by
convention -- see os.environ.setdefault below) -- Qt Quick's software
rasterizer fallback still processes the scene correctly under offscreen, it
just isn't GPU-accelerated, so this test exercises the real state machine
and IPC protocol regardless of whether real GPU hardware is available in
the environment it runs in. The GPU *capability probe* itself
(GpuCompositorProbe / _run_gpu_capability_probe) is a separate concern
tested elsewhere; a probe run under offscreen is expected to report
unavailable (Software backend) and that's correct, not a bug.

Skips gracefully if the fixtures are missing.
"""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtCore, QtWidgets

from billsmusic.video_backend import QtVideoPlaybackBackend
from billsmusic.video_dual_transition import GPU_TRANSITION_EFFECTS

_FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
_FIXTURE_A = os.path.join(_FIXTURES_DIR, "sample.mp4")
_FIXTURE_B = os.path.join(_FIXTURES_DIR, "sample.mkv")
_FIXTURES_PRESENT = os.path.isfile(_FIXTURE_A) and os.path.isfile(_FIXTURE_B)

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _pump_until(predicate, seconds=10.0):
    app = _app()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_gpu_dual_mode_subprocess_loads_and_plays_primary():
    _app()
    backend = QtVideoPlaybackBackend(dual_mode="gpu")
    assert _pump_until(lambda: backend._process_ready, seconds=10.0), (
        "GPU dual-mode subprocess never announced ready"
    )

    started_events = []
    backend.started.connect(lambda: started_events.append(True))
    durations = []
    backend.duration_changed.connect(durations.append)
    assert backend.load(_FIXTURE_A) is True
    assert _pump_until(lambda: len(started_events) >= 1), "primary deck never started"
    assert _pump_until(lambda: bool(durations)), "duration never reported"

    backend.shutdown()
    assert backend._process.state() == QtCore.QProcess.ProcessState.NotRunning


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_gpu_dual_mode_completes_several_consecutive_transitions():
    _app()
    backend = QtVideoPlaybackBackend(dual_mode="gpu")
    assert _pump_until(lambda: backend._process_ready, seconds=10.0)

    started_events = []
    backend.started.connect(lambda: started_events.append(True))
    assert backend.load(_FIXTURE_A) is True
    assert _pump_until(lambda: len(started_events) >= 1), "Deck A never started"

    fixtures = [_FIXTURE_B, _FIXTURE_A, _FIXTURE_B]
    completions = []
    backend.dual_transition_complete.connect(lambda transition_id: completions.append(True))
    failures = []
    backend.secondary_failed.connect(lambda envelope: failures.append(envelope.get("reason")))
    ready_events = []
    backend.secondary_ready.connect(lambda envelope: ready_events.append(True))

    for cycle, next_path in enumerate(fixtures, start=1):
        ready_events.clear()
        assert backend.preload_secondary(next_path) is True
        assert _pump_until(lambda: bool(ready_events) or bool(failures), seconds=10.0), (
            f"cycle {cycle}: secondary never became ready or failed"
        )
        assert not failures, f"cycle {cycle}: secondary preload failed: {failures}"

        completions_before = len(completions)
        assert backend.commit_dual_transition(300) is True
        assert _pump_until(lambda: len(completions) > completions_before, seconds=10.0), (
            f"cycle {cycle}: dual_transition_complete never fired"
        )

    backend.shutdown()
    assert backend._process.state() == QtCore.QProcess.ProcessState.NotRunning
    stderr = bytes(backend._process.readAllStandardError()).decode("utf-8", errors="replace")
    assert "Destroyed while thread is still running" not in stderr


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_gpu_dual_mode_cancel_leaves_primary_playback_unaffected():
    _app()
    backend = QtVideoPlaybackBackend(dual_mode="gpu")
    assert _pump_until(lambda: backend._process_ready, seconds=10.0)

    started_events = []
    backend.started.connect(lambda: started_events.append(True))
    assert backend.load(_FIXTURE_A) is True
    assert _pump_until(lambda: len(started_events) >= 1)

    ready_events = []
    backend.secondary_ready.connect(lambda envelope: ready_events.append(True))
    assert backend.preload_secondary(_FIXTURE_B) is True
    assert _pump_until(lambda: bool(ready_events), seconds=10.0)

    # By the time secondary_ready fires, the secondary is already held
    # (Stage A pauses it before announcing readiness) -- wait a little
    # longer here so this genuinely cancels a secondary that has been
    # sitting paused for a real stretch of time, not one cancelled the
    # instant it became ready.
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        _app().processEvents()
        time.sleep(0.02)

    backend.cancel_secondary()
    _app().processEvents()

    positions = []
    backend.position_changed.connect(positions.append)
    assert _pump_until(lambda: len(positions) >= 1, seconds=5.0)

    backend.shutdown()
    assert backend._process.state() == QtCore.QProcess.ProcessState.NotRunning


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_gpu_dual_mode_completes_a_b_a_with_every_gpu_effect():
    """Drives the real production QML scene and compiled shader (not
    mocked) through an A->B->A cycle for every GPU transition effect --
    Cross Dissolve, the Phase 2B Push/Wipe/Zoom effects, and the Phase 2C
    RGB Glitch/Pixel Dissolve/Luma Dissolve/Film Burn/Zoom Blur/Diagonal
    Wipe effects -- proving the compositor is genuinely extensible to many
    transition types, in one continuous child-process session (several
    consecutive transitions, not one isolated transition per process),
    without destabilising the working Cross Dissolve/Push/Wipe/Zoom paths.
    Each cycle: preload, commit with that effect and a distinct non-zero
    seed (so the seed-consuming Phase 2C effects are genuinely exercised,
    not left at their default), wait for dual_transition_complete, confirm
    no secondary_failed and the process is still alive and responsive
    afterward (proves no stuck transition and correct promotion -- a
    stuck/incorrect promotion would leave the next cycle's preload or
    commit unable to complete within the timeout). Scales automatically to
    however many effects GPU_TRANSITION_EFFECTS currently lists."""
    _app()
    backend = QtVideoPlaybackBackend(dual_mode="gpu")
    assert _pump_until(lambda: backend._process_ready, seconds=10.0)

    started_events = []
    backend.started.connect(lambda: started_events.append(True))
    assert backend.load(_FIXTURE_A) is True
    assert _pump_until(lambda: len(started_events) >= 1), "Deck A never started"

    completions = []
    backend.dual_transition_complete.connect(lambda transition_id: completions.append(True))
    failures = []
    backend.secondary_failed.connect(lambda envelope: failures.append(envelope.get("reason")))
    ready_events = []
    backend.secondary_ready.connect(lambda envelope: ready_events.append(True))

    fixtures = [_FIXTURE_B, _FIXTURE_A]
    clips = (fixtures * ((len(GPU_TRANSITION_EFFECTS) // 2) + 1))[:len(GPU_TRANSITION_EFFECTS)]
    for cycle, (effect, next_path) in enumerate(
        zip(GPU_TRANSITION_EFFECTS, clips), start=1,
    ):
        ready_events.clear()
        assert backend.preload_secondary(next_path) is True
        assert _pump_until(
            lambda: bool(ready_events) or bool(failures), seconds=10.0,
        ), f"cycle {cycle} ({effect}): secondary never became ready or failed"
        assert not failures, f"cycle {cycle} ({effect}): secondary preload failed: {failures}"

        completions_before = len(completions)
        seed = ((cycle * 37) % 100) / 100.0
        assert backend.commit_dual_transition(300, effect, seed) is True, (
            f"cycle {cycle} ({effect}): commit declined"
        )
        assert _pump_until(
            lambda: len(completions) > completions_before, seconds=10.0,
        ), f"cycle {cycle} ({effect}): dual_transition_complete never fired"
        # Still alive and responsive -- a stuck transition or a crash
        # during this specific effect would show up here as either the
        # process dying or the next cycle's preload/commit never
        # completing within its own timeout above.
        assert backend._process.state() == QtCore.QProcess.ProcessState.Running

    assert len(completions) == len(GPU_TRANSITION_EFFECTS)
    backend.shutdown()
    assert backend._process.state() == QtCore.QProcess.ProcessState.NotRunning
    stderr = bytes(backend._process.readAllStandardError()).decode("utf-8", errors="replace")
    assert "Destroyed while thread is still running" not in stderr
    stderr = bytes(backend._process.readAllStandardError()).decode("utf-8", errors="replace")
    assert "Destroyed while thread is still running" not in stderr


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_gpu_dual_mode_intro_seek_lands_at_requested_position():
    """Smart Video Transition Points' intro-skip offset
    (preload_secondary(path, start_position_ms=...)) drives the real
    subprocess's reactive seek-after-BUFFERED-then-confirm path in
    video_subprocess.py's GpuDualDeckVideoSubprocessController -- this is
    the one thing in that feature with no prior precedent anywhere in this
    codebase (every other seek call site seeks well after a load/buffered
    confirmation, never combined with a fresh preload), so it gets its own
    real, non-mocked verification against the actual IPC round-trip before
    anything else in the intro path is trusted to build on it.

    There's no direct "secondary position" signal exposed to the parent
    (position_changed is primary-deck-only by design -- see
    video_backend.py), so this verifies indirectly: preload with a modest
    nonzero start_position_ms, wait for secondary_ready, commit the
    transition, and confirm the *promoted* deck's first reported position
    (once it becomes primary) reflects having started from the requested
    offset rather than from 0. The position_changed listener is connected
    only *after* dual_transition_complete fires (i.e. after promotion) --
    connecting it earlier would also capture Deck A's own, unrelated,
    already-in-flight position reports from before the transition, making
    "first position" ambiguous about which deck it came from."""
    _app()
    backend = QtVideoPlaybackBackend(dual_mode="gpu")
    assert _pump_until(lambda: backend._process_ready, seconds=10.0)

    started_events = []
    backend.started.connect(lambda: started_events.append(True))
    assert backend.load(_FIXTURE_A) is True
    assert _pump_until(lambda: len(started_events) >= 1), "Deck A never started"

    ready_events = []
    backend.secondary_ready.connect(lambda envelope: ready_events.append(True))
    failures = []
    backend.secondary_failed.connect(lambda envelope: failures.append(envelope.get("reason")))
    requested_start_ms = 300
    assert backend.preload_secondary(_FIXTURE_B, start_position_ms=requested_start_ms) is True
    assert _pump_until(lambda: bool(ready_events) or bool(failures), seconds=10.0), (
        "secondary never became ready or failed"
    )
    assert not failures, f"secondary preload with start_position_ms failed: {failures}"

    completions = []
    backend.dual_transition_complete.connect(lambda transition_id: completions.append(True))
    assert backend.commit_dual_transition(300) is True
    assert _pump_until(lambda: bool(completions), seconds=10.0), "dual_transition_complete never fired"

    positions_after_promotion = []
    backend.position_changed.connect(positions_after_promotion.append)
    assert _pump_until(lambda: len(positions_after_promotion) >= 1, seconds=5.0), (
        "promoted deck never reported a position"
    )
    # Generous tolerance -- the point is "did not start from 0", not
    # frame-exact precision (real playback keeps advancing between commit
    # and the first observed position report).
    assert positions_after_promotion[0] >= requested_start_ms - 150, (
        f"promoted deck's first position ({positions_after_promotion[0]}ms) suggests "
        f"the intro seek to {requested_start_ms}ms was not honoured"
    )

    backend.shutdown()
    assert backend._process.state() == QtCore.QProcess.ProcessState.NotRunning


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_gpu_dual_mode_secondary_held_reports_valid_frame_metadata(tmp_path):
    """Stage A's foundational proof, automated-suite-safe half: pixel-level
    verification via a grabToImage()/grabWindow() diagnostic was attempted
    and found to reliably crash the GPU subprocess in this sandboxed test
    environment (no Python traceback, not caught by faulthandler -- a
    genuine native crash in the RHI grab/readback path, consistent with
    this environment having no real GPU/display surface for Qt Quick's
    scene graph to grab from, only its offscreen/software fallback for
    ordinary rendering). That diagnostic (formerly debug_sample_deck())
    was later found to also be able to crash a *real* GPU subprocess when
    it raced a live commit, and has been removed entirely (2026-08-25) --
    see CODEX_HANDOFF.md's "v1.0.65" entry. What *is* verified here,
    automatically and reliably: Qt's own
    authoritative hasVideo/mediaStatus signals -- not inferred, not
    assumed -- confirm the held secondary has a genuine decoded video
    stream ready to display at the moment it's paused, and position
    stability across a real wait (proven in
    test_real_gpu_dual_mode_smart_intro_seek_held_not_advanced below) rules
    out the failure mode v1.0.57 actually diagnosed (silent continued
    playback)."""
    from billsmusic.performance_diagnostics import get_diagnostics, reset_diagnostics_for_tests

    reset_diagnostics_for_tests()
    diagnostics = get_diagnostics(directory=str(tmp_path), level="developer", start_writer=False)
    try:
        _app()
        backend = QtVideoPlaybackBackend(dual_mode="gpu")
        assert _pump_until(lambda: backend._process_ready, seconds=10.0)

        started_events = []
        backend.started.connect(lambda: started_events.append(True))
        assert backend.load(_FIXTURE_A) is True
        assert _pump_until(lambda: len(started_events) >= 1), "Deck A never started"

        ready_events = []
        backend.secondary_ready.connect(lambda envelope: ready_events.append(True))
        assert backend.preload_secondary(_FIXTURE_B) is True
        assert _pump_until(lambda: bool(ready_events), seconds=10.0), "secondary never became ready"

        held_events = [e for e in diagnostics.recent_events if e["operation"] == "video_secondary_held"]
        assert held_events, "no secondary_held diagnostic recorded"
        held = held_events[-1]["details"]
        assert held["has_video"] is True
        assert held["source_empty"] is False
        assert held["playback_state_name"] == "PausedState"

        backend.shutdown()
    finally:
        reset_diagnostics_for_tests()


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_gpu_dual_mode_smart_intro_seek_held_not_advanced(tmp_path):
    """Extends test_real_gpu_dual_mode_intro_seek_lands_at_requested_position:
    that test only proved the seek *landed*; this proves the secondary then
    *stays* there (held) rather than continuing to play for the several
    seconds a real deadline wait can take -- the exact real-device bug
    v1.0.57 diagnosed (280ms requested, ~321ms confirmed, ~3200-4200ms by
    the time gpu_transition_started actually fired)."""
    from billsmusic.performance_diagnostics import get_diagnostics, reset_diagnostics_for_tests

    reset_diagnostics_for_tests()
    diagnostics = get_diagnostics(directory=str(tmp_path), level="developer", start_writer=False)
    try:
        _app()
        backend = QtVideoPlaybackBackend(dual_mode="gpu")
        assert _pump_until(lambda: backend._process_ready, seconds=10.0)

        started_events = []
        backend.started.connect(lambda: started_events.append(True))
        assert backend.load(_FIXTURE_A) is True
        assert _pump_until(lambda: len(started_events) >= 1)

        ready_events = []
        backend.secondary_ready.connect(lambda envelope: ready_events.append(True))
        requested_start_ms = 300
        assert backend.preload_secondary(_FIXTURE_B, start_position_ms=requested_start_ms) is True
        assert _pump_until(lambda: bool(ready_events), seconds=10.0)

        held_events = [e for e in diagnostics.recent_events if e["operation"] == "video_secondary_held"]
        assert held_events, "no secondary_held diagnostic recorded"
        held_position = held_events[-1]["details"]["position_ms"]
        assert held_position >= requested_start_ms - 150, (
            f"held position ({held_position}ms) does not reflect the requested "
            f"Smart Intro seek to {requested_start_ms}ms"
        )

        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline:
            _app().processEvents()
            time.sleep(0.02)

        completions = []
        backend.dual_transition_complete.connect(lambda transition_id: completions.append(True))
        assert backend.commit_dual_transition(300) is True
        assert _pump_until(lambda: bool(completions), seconds=10.0)

        checkpoints = [e for e in diagnostics.recent_events if e["operation"] == "video_gpu_transition_checkpoint"]
        checkpoint0 = next(e["details"] for e in checkpoints if e["details"]["progress_target"] == 0.0)
        assert abs(checkpoint0["b_position_ms"] - held_position) < 150, (
            f"secondary advanced past its Smart Intro seek position while held: "
            f"held={held_position}ms, at commit={checkpoint0['b_position_ms']}ms"
        )

        backend.shutdown()
    finally:
        reset_diagnostics_for_tests()


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_gpu_dual_mode_resume_at_commit_sends_no_reseek(tmp_path):
    """Stage A3's explicit requirement: resuming the held secondary at
    commit must never re-seek it (a seek decodes forward from the nearest
    keyframe; resuming a genuine pause does not). Verified directly on the
    real diagnostic stream: no secondary_seek_issued event may occur after
    secondary_held -- the seek machinery (_issue_secondary_seek) must never
    fire again once the secondary is confirmed ready and held, all the way
    through commit and promotion."""
    from billsmusic.performance_diagnostics import get_diagnostics, reset_diagnostics_for_tests

    reset_diagnostics_for_tests()
    diagnostics = get_diagnostics(directory=str(tmp_path), level="developer", start_writer=False)
    try:
        _app()
        backend = QtVideoPlaybackBackend(dual_mode="gpu")
        assert _pump_until(lambda: backend._process_ready, seconds=10.0)

        started_events = []
        backend.started.connect(lambda: started_events.append(True))
        assert backend.load(_FIXTURE_A) is True
        assert _pump_until(lambda: len(started_events) >= 1)

        ready_events = []
        backend.secondary_ready.connect(lambda envelope: ready_events.append(True))
        assert backend.preload_secondary(_FIXTURE_B, start_position_ms=300) is True
        assert _pump_until(lambda: bool(ready_events), seconds=10.0)

        events_before_commit = list(diagnostics.recent_events)
        held_index = next(
            i for i, e in enumerate(events_before_commit)
            if e["operation"] == "video_secondary_held"
        )
        assert not any(
            e["operation"] == "video_secondary_seek_issued"
            for e in events_before_commit[held_index + 1:]
        ), "a seek was issued again after the secondary was already held"

        completions = []
        backend.dual_transition_complete.connect(lambda transition_id: completions.append(True))
        assert backend.commit_dual_transition(300) is True
        assert _pump_until(lambda: bool(completions), seconds=10.0)

        all_events = list(diagnostics.recent_events)
        assert not any(
            e["operation"] == "video_secondary_seek_issued" for e in all_events[held_index + 1:]
        ), "a seek was issued during or after the transition commit"

        backend.shutdown()
    finally:
        reset_diagnostics_for_tests()


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_gpu_dual_mode_zero_start_held_near_zero_despite_slow_buffering(tmp_path):
    """Regression test for a real-device bug found testing v1.0.58: a
    plain preload (start_position_ms=0, i.e. no Smart Intro seek) used to
    take a shortcut straight to "hold wherever the deck happens to be" the
    instant BUFFERED status fired -- but the deck is playing the whole time
    it's buffering (see _start_preload_secondary's secondary.play()), so a
    slow load left it holding well past position 0. Real sessions showed
    requested 0ms held at 1241ms and 3320ms, silently skipping that much
    of the incoming video. Fixed by routing target 0 through the exact
    same seek/confirm pipeline Smart Intro's non-zero offsets already use
    (see _on_media_status_changed) -- no more special-casing "0 means skip
    the confirm dance."

    Both real fixtures are only ~2s long and load almost instantly, so
    they'd never naturally take long enough to reach BUFFERED for this
    race to manifest in an automated test. debug_set_seek_delay_ms
    (test-only) deterministically simulates that: it delays the corrective
    seek from starting for 1.5s after BUFFERED fires, while the deck
    keeps playing the whole time -- reproducing "the decoder had time to
    drift substantially before becoming ready," the same shape as the real
    1241ms/3320ms drift, just scaled to fit inside these fixtures' own
    ~2s duration instead of the user's own longer real-world example."""
    from billsmusic.performance_diagnostics import get_diagnostics, reset_diagnostics_for_tests

    reset_diagnostics_for_tests()
    diagnostics = get_diagnostics(directory=str(tmp_path), level="developer", start_writer=False)
    try:
        _app()
        backend = QtVideoPlaybackBackend(dual_mode="gpu")
        assert _pump_until(lambda: backend._process_ready, seconds=10.0)

        started_events = []
        backend.started.connect(lambda: started_events.append(True))
        assert backend.load(_FIXTURE_A) is True
        assert _pump_until(lambda: len(started_events) >= 1)

        assert backend.debug_set_seek_delay_ms(1500) is True

        ready_events = []
        backend.secondary_ready.connect(lambda envelope: ready_events.append(True))
        assert backend.preload_secondary(_FIXTURE_B, start_position_ms=0) is True
        assert _pump_until(lambda: bool(ready_events), seconds=10.0), (
            "secondary never became ready -- see if the seek/confirm pipeline "
            "hung for the zero-target case"
        )

        held_events = [e for e in diagnostics.recent_events if e["operation"] == "video_secondary_held"]
        assert held_events, "no secondary_held diagnostic recorded"
        held_position = held_events[-1]["details"]["position_ms"]
        assert held_position <= 250, (
            f"held position ({held_position}ms) should be near 0 despite the "
            f"decoder having ~1.5s to drift while buffering -- the zero-start "
            f"drift bug is back if this is large"
        )

        completions = []
        backend.dual_transition_complete.connect(lambda transition_id: completions.append(True))
        assert backend.commit_dual_transition(300) is True
        assert _pump_until(lambda: bool(completions), seconds=10.0)

        checkpoints = [e for e in diagnostics.recent_events if e["operation"] == "video_gpu_transition_checkpoint"]
        checkpoint0 = next(e["details"] for e in checkpoints if e["details"]["progress_target"] == 0.0)
        assert abs(checkpoint0["b_position_ms"] - held_position) < 150, (
            f"secondary advanced while held: held={held_position}ms, "
            f"at commit={checkpoint0['b_position_ms']}ms"
        )

        backend.shutdown()
    finally:
        reset_diagnostics_for_tests()


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_gpu_dual_mode_secondary_preload_progress_reported(tmp_path):
    """Stage B's real, non-mocked proof that secondary_preload_progress
    actually fires during a genuine preload -- the mediaStatus-based half
    (video_subprocess.py's _on_media_status_changed) should fire at least
    once even for these tiny, near-instantly-loading fixtures, since it's
    a real Qt status transition, not something that depends on file size
    or load duration. The signal is a real pyqtSignal (not diagnostics-
    only), so this also confirms it reaches the backend's Qt signal path,
    not just the diagnostics log."""
    from billsmusic.performance_diagnostics import get_diagnostics, reset_diagnostics_for_tests

    reset_diagnostics_for_tests()
    diagnostics = get_diagnostics(directory=str(tmp_path), level="developer", start_writer=False)
    try:
        _app()
        backend = QtVideoPlaybackBackend(dual_mode="gpu")
        assert _pump_until(lambda: backend._process_ready, seconds=10.0)

        started_events = []
        backend.started.connect(lambda: started_events.append(True))
        assert backend.load(_FIXTURE_A) is True
        assert _pump_until(lambda: len(started_events) >= 1)

        progress_signal_events = []
        backend.secondary_preload_progress.connect(progress_signal_events.append)

        ready_events = []
        backend.secondary_ready.connect(lambda envelope: ready_events.append(True))
        assert backend.preload_secondary(_FIXTURE_B) is True
        assert _pump_until(lambda: bool(ready_events), seconds=10.0)

        assert progress_signal_events, "secondary_preload_progress signal never fired"
        assert any(
            e["reason"] == "media_status_changed" for e in progress_signal_events
        )

        progress_diagnostics = [
            e for e in diagnostics.recent_events
            if e["operation"] == "video_secondary_preload_progress"
        ]
        assert progress_diagnostics, "no video_secondary_preload_progress diagnostic recorded"

        backend.shutdown()
    finally:
        reset_diagnostics_for_tests()


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_gpu_dual_mode_checkpoint_and_seek_diagnostics_recorded(tmp_path):
    """Bounded deck-state diagnostics added to investigate a real-device
    black-interval report (see CODEX_HANDOFF.md's "Deck-state diagnostics"
    section): secondary-seek-confirmation events and the 5-point GPU
    transition progress checkpoint. Verified against the real subprocess/QML
    scene, not mocked -- matches this file's own stated purpose and this
    feature's established practice of not trusting a new Qt/QML interaction
    (here: polling blend.progress via QTimer, reading position/hasVideo/
    bufferProgress properties on both decks) without a real, non-mocked
    round-trip proving it actually behaves as designed."""
    from billsmusic.performance_diagnostics import get_diagnostics, reset_diagnostics_for_tests

    reset_diagnostics_for_tests()
    diagnostics = get_diagnostics(
        directory=str(tmp_path), level="developer", start_writer=False,
    )
    try:
        _app()
        backend = QtVideoPlaybackBackend(dual_mode="gpu")
        assert _pump_until(lambda: backend._process_ready, seconds=10.0)

        started_events = []
        backend.started.connect(lambda: started_events.append(True))
        assert backend.load(_FIXTURE_A) is True
        assert _pump_until(lambda: len(started_events) >= 1), "Deck A never started"

        ready_events = []
        backend.secondary_ready.connect(lambda envelope: ready_events.append(True))
        failures = []
        backend.secondary_failed.connect(lambda envelope: failures.append(envelope.get("reason")))
        requested_start_ms = 300
        assert backend.preload_secondary(_FIXTURE_B, start_position_ms=requested_start_ms) is True
        assert _pump_until(lambda: bool(ready_events) or bool(failures), seconds=10.0), (
            "secondary never became ready or failed"
        )
        assert not failures, f"secondary preload with start_position_ms failed: {failures}"

        completions = []
        backend.dual_transition_complete.connect(lambda transition_id: completions.append(True))
        assert backend.commit_dual_transition(300) is True
        assert _pump_until(lambda: bool(completions), seconds=10.0), "dual_transition_complete never fired"
        # Checkpoint 1.0 is captured synchronously inside _on_transition_finished
        # before dual_transition_complete is emitted, but a couple of
        # processEvents() calls ensure any queued diagnostics writes land.
        _app().processEvents()

        events = list(diagnostics.recent_events)

        seek_issued = [e for e in events if e["operation"] == "video_secondary_seek_issued"]
        assert seek_issued, "no video_secondary_seek_issued event recorded"
        assert seek_issued[0]["details"]["requested_start_position_ms"] == requested_start_ms
        assert seek_issued[0]["details"]["position_before_seek_ms"] is not None

        ready = [e for e in events if e["operation"] == "video_secondary_ready"]
        assert ready, "no video_secondary_ready event recorded"
        last_ready = ready[-1]["details"]
        assert last_ready.get("via") == "seek_confirmed", last_ready
        assert last_ready.get("confirmed_position_ms") is not None
        assert last_ready.get("has_video") is not None

        checkpoints = [e for e in events if e["operation"] == "video_gpu_transition_checkpoint"]
        targets = [e["details"]["progress_target"] for e in checkpoints]
        assert targets == [0.0, 0.25, 0.5, 0.75, 1.0], (
            f"expected exactly the 5 fixed checkpoints in order, got {targets}"
        )
        for checkpoint in checkpoints:
            details = checkpoint["details"]
            assert details["a_position_ms"] is not None
            assert details["b_position_ms"] is not None
            assert details["a_has_video"] is not None
            assert details["b_has_video"] is not None
            assert details["a_source_empty"] is False, (
                "outgoing deck's source appears cleared during the transition window"
            )

        backend.shutdown()
    finally:
        reset_diagnostics_for_tests()


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_gpu_dual_mode_seek_and_pause_resume_do_not_crash():
    _app()
    backend = QtVideoPlaybackBackend(dual_mode="gpu")
    assert _pump_until(lambda: backend._process_ready, seconds=10.0)

    started_events = []
    backend.started.connect(lambda: started_events.append(True))
    durations = []
    backend.duration_changed.connect(durations.append)
    assert backend.load(_FIXTURE_A) is True
    assert _pump_until(lambda: len(started_events) >= 1 and bool(durations))

    backend.seek(min(500, durations[-1] // 2 if durations[-1] else 0))
    _app().processEvents()
    backend.pause()
    _app().processEvents()
    backend.resume()
    _app().processEvents()

    positions = []
    backend.position_changed.connect(positions.append)
    assert _pump_until(lambda: len(positions) >= 1, seconds=5.0)

    backend.shutdown()
    assert backend._process.state() == QtCore.QProcess.ProcessState.NotRunning


# -- Stage C: synchronized GPU video audio crossfade ------------------------

def _drive_dual_transition_with_crossfade(
    tmp_path, *, crossfade_enabled, duration_ms=300, mid_transition_action=None,
):
    """Shared setup for the Stage C real-fixture tests below: loads A,
    preloads/holds B, commits with the given crossfade preference, pumps
    to completion, and returns the diagnostics instance so the caller can
    inspect gpu_transition_checkpoint payloads."""
    from billsmusic.performance_diagnostics import get_diagnostics, reset_diagnostics_for_tests

    reset_diagnostics_for_tests()
    diagnostics = get_diagnostics(directory=str(tmp_path), level="developer", start_writer=False)
    _app()
    backend = QtVideoPlaybackBackend(dual_mode="gpu")
    assert _pump_until(lambda: backend._process_ready, seconds=10.0)

    started_events = []
    backend.started.connect(lambda: started_events.append(True))
    assert backend.load(_FIXTURE_A) is True
    assert _pump_until(lambda: len(started_events) >= 1)

    ready_events = []
    backend.secondary_ready.connect(lambda envelope: ready_events.append(True))
    assert backend.preload_secondary(_FIXTURE_B) is True
    assert _pump_until(lambda: bool(ready_events), seconds=10.0)

    completions = []
    backend.dual_transition_complete.connect(lambda transition_id: completions.append(True))
    assert backend.commit_dual_transition(
        duration_ms, audio_crossfade_enabled=crossfade_enabled,
    ) is True
    if mid_transition_action is not None:
        mid_transition_action(backend)
    assert _pump_until(lambda: bool(completions), seconds=10.0)

    return backend, diagnostics


def _checkpoints(diagnostics):
    events = [e for e in diagnostics.recent_events if e["operation"] == "video_gpu_transition_checkpoint"]
    return sorted(events, key=lambda e: e["details"]["progress_target"])


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_gpu_dual_mode_crossfade_disabled_preserves_existing_checkpoint_shape(tmp_path):
    """Default (disabled) preference: today's exact checkpoint payload
    shape, byte-for-byte -- no new audio fields leak in when the feature
    is off."""
    from billsmusic.performance_diagnostics import reset_diagnostics_for_tests
    try:
        backend, diagnostics = _drive_dual_transition_with_crossfade(
            tmp_path, crossfade_enabled=False,
        )
        checkpoints = _checkpoints(diagnostics)
        assert len(checkpoints) == 5
        for checkpoint in checkpoints:
            details = checkpoint["details"]
            for field in (
                "audio_crossfade_curve", "global_muted", "a_base_gain", "b_base_gain",
                "a_transition_gain", "b_transition_gain", "a_effective_volume", "b_effective_volume",
            ):
                assert field not in details, f"{field} present despite crossfade disabled"
        backend.shutdown()
    finally:
        reset_diagnostics_for_tests()


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_gpu_dual_mode_crossfade_enabled_gains_follow_progress(tmp_path):
    """Enabled: the checkpoint payload carries the new audio fields, A's
    transition gain starts at 1.0 and ends at 0.0, B's does the reverse,
    tracking the same blend.progress the visual transition uses -- no
    second, independent progress source."""
    from billsmusic.performance_diagnostics import reset_diagnostics_for_tests
    try:
        backend, diagnostics = _drive_dual_transition_with_crossfade(
            tmp_path, crossfade_enabled=True,
        )
        checkpoints = _checkpoints(diagnostics)
        assert len(checkpoints) == 5
        first, last = checkpoints[0]["details"], checkpoints[-1]["details"]
        assert first["audio_crossfade_curve"] == "Equal Power"
        assert first["global_muted"] is False
        assert abs(first["a_transition_gain"] - 1.0) < 1e-6
        assert abs(first["b_transition_gain"] - 0.0) < 1e-6
        assert abs(last["a_transition_gain"] - 0.0) < 1e-6
        assert abs(last["b_transition_gain"] - 1.0) < 1e-6
        # Monotonic in the expected directions across all 5 points.
        a_gains = [c["details"]["a_transition_gain"] for c in checkpoints]
        b_gains = [c["details"]["b_transition_gain"] for c in checkpoints]
        assert a_gains == sorted(a_gains, reverse=True)
        assert b_gains == sorted(b_gains)
        # Full master volume (default 1.0, unmuted) -> base gain 1.0 for
        # both decks, never one copied from the other's live property.
        for checkpoint in checkpoints:
            assert checkpoint["details"]["a_base_gain"] == 1.0
            assert checkpoint["details"]["b_base_gain"] == 1.0
        backend.shutdown()
    finally:
        reset_diagnostics_for_tests()


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_gpu_dual_mode_crossfade_master_volume_change_reaches_both_decks_live(tmp_path):
    """Master volume changed mid-transition must be reflected in later
    checkpoints without needing the transition to finish first -- proves
    the base gain is live-tracked, not snapshotted once at commit."""
    from billsmusic.performance_diagnostics import reset_diagnostics_for_tests

    def _change_volume(backend):
        backend.set_volume(40)
        _app().processEvents()

    try:
        backend, diagnostics = _drive_dual_transition_with_crossfade(
            tmp_path, crossfade_enabled=True, duration_ms=400,
            mid_transition_action=_change_volume,
        )
        checkpoints = _checkpoints(diagnostics)
        assert len(checkpoints) == 5
        # progress=0.0 was captured synchronously inside commit_dual_transition,
        # before the test's mid_transition_action ever ran -- must still show
        # the original 1.0 base gain.
        assert checkpoints[0]["details"]["a_base_gain"] == 1.0
        # The final checkpoint, well after the volume change, must reflect it.
        assert abs(checkpoints[-1]["details"]["a_base_gain"] - 0.4) < 1e-6
        assert abs(checkpoints[-1]["details"]["b_base_gain"] - 0.4) < 1e-6
        backend.shutdown()
    finally:
        reset_diagnostics_for_tests()


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_gpu_dual_mode_crossfade_mute_mid_transition_silences_both_decks(tmp_path):
    """Global mute pressed mid-transition must silence both decks
    immediately (effective volume 0.0 for both), regardless of where the
    visual transition currently is -- mute stays authoritative throughout,
    never defeated by the crossfade."""
    from billsmusic.performance_diagnostics import reset_diagnostics_for_tests

    def _mute(backend):
        backend.set_muted(True)
        _app().processEvents()

    try:
        backend, diagnostics = _drive_dual_transition_with_crossfade(
            tmp_path, crossfade_enabled=True, duration_ms=400,
            mid_transition_action=_mute,
        )
        checkpoints = _checkpoints(diagnostics)
        assert len(checkpoints) == 5
        assert checkpoints[0]["details"]["global_muted"] is False
        final = checkpoints[-1]["details"]
        assert final["global_muted"] is True
        assert final["a_effective_volume"] == 0.0
        assert final["b_effective_volume"] == 0.0
        backend.shutdown()
    finally:
        reset_diagnostics_for_tests()


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_gpu_dual_mode_crossfade_no_second_timer_introduced(tmp_path):
    """The audio tick must ride the existing checkpoint timer, not a new
    one -- still exactly 5 gpu_transition_checkpoint emissions with
    crossfade enabled, matching the disabled-preference count exactly."""
    from billsmusic.performance_diagnostics import reset_diagnostics_for_tests
    try:
        backend, diagnostics = _drive_dual_transition_with_crossfade(
            tmp_path, crossfade_enabled=True,
        )
        checkpoints = _checkpoints(diagnostics)
        assert len(checkpoints) == 5
        backend.shutdown()
    finally:
        reset_diagnostics_for_tests()


def _query_and_wait(backend, checkpoint, transition_id=1, seconds=5.0):
    """Drives one real query_audio_state()/audio_state_reported round trip
    and returns the report payload -- the "production volume/mute command
    path, not a boolean-only fake" verification v1.0.71's correction round
    needed: the mixed-media Audio->Video envelope (window.py) only ever
    sends fire-and-forget set_volume/set_muted IPC commands, so nothing
    before this could distinguish "the right command was sent" from "the
    right command actually landed on the deck currently rendering"."""
    reports = []
    conn = backend.audio_state_reported.connect(reports.append)
    try:
        assert backend.query_audio_state(checkpoint, transition_id) is True
        assert _pump_until(lambda: bool(reports), seconds=seconds), (
            f"audio_state_report for checkpoint={checkpoint!r} never arrived"
        )
    finally:
        try:
            backend.audio_state_reported.disconnect(conn)
        except Exception:
            pass
    return reports[-1]


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_gpu_single_load_mixed_audio_to_video_envelope_reaches_full_volume():
    """Replays window.py's exact Audio->Video mixed-transition command
    sequence (_prepare_mixed_transition_audio_to_video's set_muted(True)
    before load(); _activate_mixed_media_transition's set_muted(False)+
    set_volume(0) once ready; _mixed_transition_tick's progressive
    set_volume ramp; _finish_mixed_transition_audio_to_video's final
    set_volume(master)) against the real subprocess and asserts what the
    deck *actually* has via query_audio_state at each stage -- not what
    window.py merely intended to send."""
    _app()
    backend = QtVideoPlaybackBackend(dual_mode="gpu")
    assert _pump_until(lambda: backend._process_ready, seconds=10.0)

    # -- prepare: muted before the load even starts, matching
    # _prepare_mixed_transition_audio_to_video's ordering exactly.
    backend.set_muted(True)
    started_events = []
    backend.started.connect(lambda: started_events.append(True))
    assert backend.load(_FIXTURE_A) is True
    assert _pump_until(lambda: len(started_events) >= 1), "primary deck never started"

    report = _query_and_wait(backend, "prepare")
    assert report["muted"] is True, "video must still be muted immediately after load"

    # -- transition start: unmute, volume forced to 0 (start of the fade-in).
    backend.set_muted(False)
    backend.set_volume(0)
    report = _query_and_wait(backend, "transition_start")
    assert report["muted"] is False, "unmute must actually reach the rendering deck"
    assert report["volume"] == pytest.approx(0.0, abs=1e-6)
    assert report["playback_state"] == 1, "deck must still be PlayingState, not paused/stopped"

    # -- ramp: master_volume analogue of 80, in_scale rising through the
    # same 25/50/75/100% checkpoints window.py's own tick uses.
    master_volume = 80.0
    previous_volume = -1.0
    for pct in (25, 50, 75, 100):
        in_scale = pct / 100.0
        backend.set_volume(master_volume * in_scale)
        report = _query_and_wait(backend, f"{pct}pct")
        assert report["muted"] is False
        assert report["volume"] > previous_volume, (
            f"volume did not increase at {pct}%: {report['volume']} <= {previous_volume}"
        )
        previous_volume = report["volume"]

    # -- commit/completion: final volume must equal the real steady-state
    # value (master_volume/100), not merely "nonzero".
    report = _query_and_wait(backend, "completion")
    assert report["muted"] is False
    assert report["volume"] == pytest.approx(master_volume / 100.0, abs=1e-6), (
        "video is not left at the correct steady-state volume after the "
        "mixed transition completes"
    )
    assert report["playback_state"] == 1

    backend.shutdown()
    assert backend._process.state() == QtCore.QProcess.ProcessState.NotRunning


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_gpu_second_consecutive_audio_to_video_envelope_also_unmutes():
    """The real acceptance-failure pattern was "silent, then works, then
    silent again" -- a single successful cycle does not rule out a bug
    that only manifests on a second Audio->Video in the same subprocess
    session. Repeats the full mute->load->unmute->ramp->full-volume
    envelope twice in a row (two separate single-deck loads, standing in
    for two separate mixed transitions later in the same queue) and
    requires *both* to end unmuted at full steady-state volume."""
    _app()
    backend = QtVideoPlaybackBackend(dual_mode="gpu")
    assert _pump_until(lambda: backend._process_ready, seconds=10.0)

    master_volume = 80.0
    for cycle, fixture in enumerate((_FIXTURE_A, _FIXTURE_B), start=1):
        started_events = []
        conn = backend.started.connect(lambda: started_events.append(True))
        backend.set_muted(True)
        assert backend.load(fixture) is True, f"cycle {cycle}: load rejected"
        assert _pump_until(lambda: len(started_events) >= 1), (
            f"cycle {cycle}: primary deck never started"
        )
        try:
            backend.started.disconnect(conn)
        except Exception:
            pass

        backend.set_muted(False)
        backend.set_volume(0)
        backend.set_volume(master_volume)
        report = _query_and_wait(backend, f"cycle_{cycle}_completion", transition_id=cycle)
        assert report["muted"] is False, f"cycle {cycle}: video left muted"
        assert report["volume"] == pytest.approx(master_volume / 100.0, abs=1e-6), (
            f"cycle {cycle}: video not at correct steady-state volume "
            f"(got {report['volume']!r})"
        )

    backend.shutdown()
    assert backend._process.state() == QtCore.QProcess.ProcessState.NotRunning


# -- GPU non-crossfade promotion audio (2026-08-31 Codex audit) -------------
# commit_dual_transition()'s non-crossfade branch used to only flip "muted"
# (True on the outgoing deck, unconditionally False on the incoming one) and
# never touch "volume" or consult the authoritative master-mute state at
# all -- unlike the crossfade branch, which already keeps both properties
# governed by _master_volume_fraction/_master_muted throughout. A promoted
# video could therefore play at the wrong volume (whatever that physical
# AudioOutput object last happened to hold) or audibly despite the user
# having the app globally muted. These tests drive the real GPU subprocess,
# not a fake, and read the promoted deck's actual property values back via
# query_audio_state -- the same mechanism the v1.0.71 correction round built
# for exactly this class of "state looks right but is it really?" proof.

@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_gpu_non_crossfade_promotion_respects_master_volume():
    _app()
    backend = QtVideoPlaybackBackend(dual_mode="gpu")
    assert _pump_until(lambda: backend._process_ready, seconds=10.0)

    started_events = []
    backend.started.connect(lambda: started_events.append(True))
    assert backend.load(_FIXTURE_A) is True
    assert _pump_until(lambda: len(started_events) >= 1)
    backend.set_volume(70)
    backend.set_muted(False)

    ready_events = []
    backend.secondary_ready.connect(lambda envelope: ready_events.append(True))
    assert backend.preload_secondary(_FIXTURE_B) is True
    assert _pump_until(lambda: bool(ready_events), seconds=10.0)

    completions = []
    backend.dual_transition_complete.connect(lambda transition_id: completions.append(True))
    # audio_crossfade_enabled defaults to False -- the buggy branch.
    assert backend.commit_dual_transition(300) is True
    assert _pump_until(lambda: bool(completions), seconds=10.0)

    report = _query_and_wait(backend, "non_crossfade_promotion")
    assert report["volume"] == pytest.approx(0.70, abs=1e-6), (
        f"promoted deck must play at the authoritative master volume (70%), "
        f"not whatever it last held (got {report['volume']!r})"
    )
    assert report["muted"] is False

    backend.shutdown()
    assert backend._process.state() == QtCore.QProcess.ProcessState.NotRunning


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_gpu_non_crossfade_promotion_respects_global_mute_then_unmute():
    _app()
    backend = QtVideoPlaybackBackend(dual_mode="gpu")
    assert _pump_until(lambda: backend._process_ready, seconds=10.0)

    started_events = []
    backend.started.connect(lambda: started_events.append(True))
    assert backend.load(_FIXTURE_A) is True
    assert _pump_until(lambda: len(started_events) >= 1)
    backend.set_volume(80)
    backend.set_muted(True)  # global mute is on *before* the transition

    ready_events = []
    backend.secondary_ready.connect(lambda envelope: ready_events.append(True))
    assert backend.preload_secondary(_FIXTURE_B) is True
    assert _pump_until(lambda: bool(ready_events), seconds=10.0)

    completions = []
    backend.dual_transition_complete.connect(lambda transition_id: completions.append(True))
    assert backend.commit_dual_transition(300) is True
    assert _pump_until(lambda: bool(completions), seconds=10.0)

    report = _query_and_wait(backend, "muted_promotion")
    assert report["muted"] is True, (
        "a promoted video must remain muted if the app was globally muted "
        "before the transition committed -- unconditionally unmuting it "
        "was the confirmed bug"
    )

    # Unmuting afterwards must make the promoted (now primary) deck audible
    # at the correct volume -- ordinary post-promotion set_muted/set_volume
    # commands already route to whichever deck is currently primary.
    backend.set_muted(False)
    report = _query_and_wait(backend, "unmuted_after_promotion")
    assert report["muted"] is False
    assert report["volume"] == pytest.approx(0.80, abs=1e-6)

    backend.shutdown()
    assert backend._process.state() == QtCore.QProcess.ProcessState.NotRunning


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_gpu_several_consecutive_non_crossfade_promotions_no_stale_deck():
    """A -> B -> A -> B, alternating master volume each time -- no earlier,
    now-secondary deck may retain or receive a later master-volume/mute
    command meant for whichever deck is genuinely primary at the time."""
    _app()
    backend = QtVideoPlaybackBackend(dual_mode="gpu")
    assert _pump_until(lambda: backend._process_ready, seconds=10.0)

    started_events = []
    backend.started.connect(lambda: started_events.append(True))
    assert backend.load(_FIXTURE_A) is True
    assert _pump_until(lambda: len(started_events) >= 1)

    fixtures = [_FIXTURE_B, _FIXTURE_A, _FIXTURE_B]
    volumes = [70, 40, 90]
    for cycle, (next_path, volume) in enumerate(zip(fixtures, volumes), start=1):
        backend.set_volume(volume)
        backend.set_muted(False)

        ready_events = []
        conn = backend.secondary_ready.connect(lambda envelope: ready_events.append(True))
        assert backend.preload_secondary(next_path) is True
        assert _pump_until(lambda: bool(ready_events), seconds=10.0), (
            f"cycle {cycle}: secondary never became ready"
        )
        try:
            backend.secondary_ready.disconnect(conn)
        except Exception:
            pass

        completions = []
        conn2 = backend.dual_transition_complete.connect(lambda tid: completions.append(True))
        assert backend.commit_dual_transition(300) is True
        assert _pump_until(lambda: bool(completions), seconds=10.0), (
            f"cycle {cycle}: dual_transition_complete never fired"
        )
        try:
            backend.dual_transition_complete.disconnect(conn2)
        except Exception:
            pass

        report = _query_and_wait(backend, f"cycle_{cycle}", transition_id=cycle)
        assert report["volume"] == pytest.approx(volume / 100.0, abs=1e-6), (
            f"cycle {cycle}: promoted deck volume {report['volume']!r} does "
            f"not match the master volume set just before this transition "
            f"({volume}%) -- a stale deck may have received/retained it"
        )
        assert report["muted"] is False

    backend.shutdown()
    assert backend._process.state() == QtCore.QProcess.ProcessState.NotRunning
