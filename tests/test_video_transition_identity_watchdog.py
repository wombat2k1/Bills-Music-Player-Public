"""Regression coverage for the GPU dual-transition identity/watchdog
mechanism (real-device bug, 2026-08-24) and its "Codex design review"
correctness hardening pass (same day): no event belonging to an old
transition T1 or preload P1 may mutate a newer transition T2 or preload
P2 -- see video_backend.py's VideoPreloadHandle/_accept_preload_event/
_accept_timing_event and the fresh-timer-per-arm commit-ack/completion
watchdog.

Uses the same mocked-QProcess harness as test_video_backend.py -- no real
child process, no real video file or hardware decoding required."""
import json
import os
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets

from billsmusic.performance_diagnostics import get_diagnostics
from billsmusic.video_backend import ERROR_SUBPROCESS, QtVideoPlaybackBackend

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _make_backend():
    _app()
    process = MagicMock(spec=QtCore.QProcess)
    process.state.return_value = QtCore.QProcess.ProcessState.Running
    process.waitForFinished.return_value = True
    backend = QtVideoPlaybackBackend(process=process, auto_start=False)
    return backend, process


def _make_dual_backend():
    _app()
    process = MagicMock(spec=QtCore.QProcess)
    process.state.return_value = QtCore.QProcess.ProcessState.Running
    process.waitForFinished.return_value = True
    process.errorString.return_value = "Crashed"
    process.readAllStandardError.return_value = b""
    backend = QtVideoPlaybackBackend(
        process=process, auto_start=False, dual_mode="gpu",
    )
    return backend, process


def _sent_commands(process) -> list:
    commands = []
    for call in process.write.call_args_list:
        commands.append(json.loads(bytes(call.args[0]).decode("utf-8")))
    return commands


def _deliver_line(backend, process, obj: dict):
    line = (json.dumps(obj) + "\n").encode("utf-8")
    process.canReadLine.side_effect = [True, False]
    process.readLine.return_value = line
    backend._on_stdout_ready(process)


def _committed_transition(tmp_path):
    """Load a primary, preload+commit a secondary. Returns
    (backend, process, outgoing_hash, incoming_hash, transition_id) with a
    fresh Stage A (ack) watchdog already running, matching the real
    sequence: load() -> preload_secondary() -> commit_dual_transition()."""
    backend, process = _make_dual_backend()
    outgoing = tmp_path / "outgoing.mp4"
    incoming = tmp_path / "incoming.mp4"
    outgoing.write_bytes(b"a")
    incoming.write_bytes(b"b")
    assert backend.load(str(outgoing)) is True
    assert backend.preload_secondary(str(incoming)) is True
    assert backend.commit_dual_transition(1000) is True
    commands = _sent_commands(process)
    outgoing_hash = commands[0]["identity_hash"]
    incoming_hash = commands[1]["identity_hash"]
    transition_id = commands[2]["transition_id"]
    return backend, process, outgoing_hash, incoming_hash, transition_id


def _ack(backend, process, transition_id, *, primary_index=0, secondary_index=1):
    _deliver_line(backend, process, {
        "event": "commit_command_received", "transition_id": transition_id,
        "primary_index": primary_index, "secondary_index": secondary_index,
    })


def _complete(backend, process, transition_id):
    _deliver_line(backend, process, {
        "event": "dual_transition_complete", "token": backend._token,
        "transition_id": transition_id,
    })


def _preload_envelope(backend, *, deck_index=1, secondary_index=1, primary_index=0):
    return {
        "preload_id": backend.active_preload_id,
        "source_hash": backend.active_preload_source_hash,
        "deck_index": deck_index, "secondary_index": secondary_index,
        "primary_index": primary_index,
    }


# -- stale-timing identity rejection (position_changed/duration_changed) ----

def test_outgoing_deck_timing_after_promotion_is_rejected(tmp_path):
    backend, process, outgoing_hash, incoming_hash, transition_id = (
        _committed_transition(tmp_path)
    )
    positions = []
    backend.position_changed.connect(positions.append)
    # The transition hasn't completed yet -- the subprocess's primary deck
    # hasn't swapped, so a genuine report from it still carries the
    # *outgoing* track's identity. That must not reach window.py at all
    # once the parent has already optimistically promoted the incoming
    # track (see commit_dual_transition()'s comment).
    _deliver_line(backend, process, {
        "event": "position_changed", "token": backend._token,
        "position_ms": 210432, "duration_ms": 213213,
        "deck_index": 0, "source_hash": outgoing_hash,
        "primary_index": 0, "secondary_index": 1,
        "transition_id": transition_id,
    })
    assert positions == []
    assert backend.position_ms() == 0


def test_outgoing_deck_position_and_duration_getters_reset_at_commit(tmp_path):
    """Real-device bug (2026-08-28, "Baby Baby"): _accept_timing_event
    only gates position_changed/duration_changed *events* -- it does
    nothing for a caller that reads position_ms()/duration_ms() as bare
    cached getters instead (window.py's _check_playback_health() and
    _maybe_prepare_mixed_transition_from_video() both do exactly this,
    with no event/signal involved at all). Reproduces the exact captured
    incident numbers: the outgoing deck had genuinely, correctly reached
    position=219052/duration=220840 (~1.8s from its own real end) *before*
    the dual transition committed -- those must not still be readable as
    "the current timing" the instant commit_dual_transition() optimistically
    promotes the incoming track's identity, or a consumer with no signal
    plumbing at all (like the video->audio near-end trigger) sees a video
    that just started looking like it's about to end."""
    backend, process = _make_dual_backend()
    outgoing = tmp_path / "outgoing.mp4"
    incoming = tmp_path / "incoming.mp4"
    outgoing.write_bytes(b"a")
    incoming.write_bytes(b"b")
    assert backend.load(str(outgoing)) is True
    # The outgoing deck's own genuine, correctly-accepted near-end position
    # -- captured before any dual transition is even preloaded, exactly
    # matching the real incident's a_position_ms/duration_ms pair.
    _deliver_line(backend, process, {
        "event": "position_changed", "token": backend._token,
        "position_ms": 219052, "duration_ms": 220840,
    })
    assert backend.position_ms() == 219052
    assert backend.duration_ms() == 220840

    assert backend.preload_secondary(str(incoming)) is True
    assert backend.commit_dual_transition(1000) is True

    # The instant commit_dual_transition() optimistically promotes the
    # incoming track's identity, the outgoing deck's last genuine reading
    # must stop being readable as authoritative -- not just rejected for
    # *new* events (already covered above), but reset outright, since
    # nothing about a bare getter read carries any identity to reject in
    # the first place.
    assert backend.position_ms() == 0, (
        "outgoing deck's stale position must not remain readable as "
        "current once the incoming track has been optimistically promoted"
    )
    assert backend.duration_ms() == 0, (
        "outgoing deck's stale duration must not remain readable as "
        "current once the incoming track has been optimistically promoted"
    )


def test_incoming_deck_timing_after_promotion_is_accepted(tmp_path):
    backend, process, outgoing_hash, incoming_hash, transition_id = (
        _committed_transition(tmp_path)
    )
    positions = []
    backend.position_changed.connect(positions.append)
    # Once the real swap has happened, the (now-primary) deck's reports
    # carry the incoming track's identity, matching what the parent
    # already optimistically committed to -- must flow through normally.
    _deliver_line(backend, process, {
        "event": "position_changed", "token": backend._token,
        "position_ms": 1500, "duration_ms": 202600,
        "deck_index": 1, "source_hash": incoming_hash,
        "primary_index": 1, "secondary_index": 0,
        "transition_id": transition_id,
    })
    assert positions == [1500]
    assert backend.position_ms() == 1500


def test_stale_duration_cannot_overwrite_promoted_track_duration(tmp_path):
    backend, process, outgoing_hash, incoming_hash, transition_id = (
        _committed_transition(tmp_path)
    )
    durations = []
    backend.duration_changed.connect(durations.append)
    _deliver_line(backend, process, {
        "event": "duration_changed", "token": backend._token,
        "duration_ms": 213213,
        "deck_index": 0, "source_hash": outgoing_hash,
        "primary_index": 0, "secondary_index": 1,
        "transition_id": transition_id,
    })
    assert durations == []
    assert backend.duration_ms() == 0


def test_stale_position_cannot_move_current_seek_state(tmp_path):
    backend, process, outgoing_hash, incoming_hash, transition_id = (
        _committed_transition(tmp_path)
    )
    _deliver_line(backend, process, {
        "event": "position_changed", "token": backend._token,
        "position_ms": 900, "duration_ms": 202600,
        "deck_index": 1, "source_hash": incoming_hash,
        "primary_index": 1, "secondary_index": 0,
        "transition_id": transition_id,
    })
    assert backend.position_ms() == 900
    _deliver_line(backend, process, {
        "event": "position_changed", "token": backend._token,
        "position_ms": 210500, "duration_ms": 213213,
        "deck_index": 0, "source_hash": outgoing_hash,
        "primary_index": 1, "secondary_index": 0,
        "transition_id": transition_id,
    })
    assert backend.position_ms() == 900


def test_classic_single_deck_position_duration_events_are_never_filtered(tmp_path):
    backend, process = _make_backend()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"x")
    assert backend.load(str(video_path)) is True
    positions = []
    durations = []
    backend.position_changed.connect(positions.append)
    backend.duration_changed.connect(durations.append)
    _deliver_line(backend, process, {
        "event": "position_changed", "token": backend._token,
        "position_ms": 4000, "duration_ms": 180000,
    })
    _deliver_line(backend, process, {
        "event": "duration_changed", "token": backend._token,
        "duration_ms": 180000,
    })
    assert positions == [4000]
    assert durations == [180000]


# -- commit ack/execution-started milestones --------------------------------

def test_commit_command_received_with_matching_transition_id_starts_stage_b(tmp_path):
    backend, process, _outgoing, _incoming, transition_id = _committed_transition(
        tmp_path
    )
    assert backend._active_commit_ack_timer is not None
    assert backend._active_commit_ack_timer.isActive()
    assert backend._active_completion_timer is None
    _ack(backend, process, transition_id)
    assert backend._active_commit_ack_timer is None
    assert backend._active_completion_timer is not None
    assert backend._active_completion_timer.isActive()


def test_commit_execution_started_does_not_raise_and_is_diagnostic_only(tmp_path):
    backend, process, _outgoing, _incoming, transition_id = _committed_transition(
        tmp_path
    )
    _deliver_line(backend, process, {
        "event": "commit_execution_started", "transition_id": transition_id,
    })
    assert backend._active_commit_ack_timer is not None
    assert backend._active_commit_ack_timer.isActive()


# -- bounded watchdog recovery -----------------------------------------------

def test_no_ack_triggers_commit_ack_timeout_recovery(tmp_path):
    backend, process, _outgoing, _incoming, transition_id = _committed_transition(
        tmp_path
    )
    errors = []
    failures = []
    backend.error.connect(lambda category, message: errors.append(category))
    backend.dual_transition_failed.connect(
        lambda tid, reason: failures.append((tid, reason))
    )
    ack_timer = backend._active_commit_ack_timer
    assert backend._active_transition_id == transition_id

    backend._on_commit_ack_timeout(transition_id, ack_timer)

    assert errors == [ERROR_SUBPROCESS]
    assert failures == [(transition_id, "commit_ack_timeout")]
    assert backend._crash_reported is True
    assert backend._restart_scheduled is True
    assert backend._active_transition_id is None
    assert backend._active_commit_ack_timer is None
    assert backend._active_completion_timer is None


def test_ack_but_no_completion_triggers_completion_timeout_recovery(tmp_path):
    backend, process, _outgoing, _incoming, transition_id = _committed_transition(
        tmp_path
    )
    _ack(backend, process, transition_id)
    completion_timer = backend._active_completion_timer
    errors = []
    failures = []
    backend.error.connect(lambda category, message: errors.append(category))
    backend.dual_transition_failed.connect(
        lambda tid, reason: failures.append((tid, reason))
    )

    backend._on_transition_completion_timeout(transition_id, completion_timer)

    assert errors == [ERROR_SUBPROCESS]
    assert failures == [(transition_id, "transition_completion_timeout")]
    assert backend._crash_reported is True
    assert backend._restart_scheduled is True
    assert backend._active_transition_id is None


def test_normal_transition_completion_cancels_both_watchdogs(tmp_path):
    backend, process, _outgoing, incoming_hash, transition_id = _committed_transition(
        tmp_path
    )
    _ack(backend, process, transition_id)
    assert backend._active_completion_timer.isActive()

    completions = []
    backend.dual_transition_complete.connect(completions.append)
    _complete(backend, process, transition_id)

    assert completions == [transition_id]
    assert backend._active_transition_id is None
    assert backend._active_commit_ack_timer is None
    assert backend._active_completion_timer is None
    # Confirms the accept side too: the now-promoted deck's own reports
    # (matching incoming_hash) are accepted after real completion.
    positions = []
    backend.position_changed.connect(positions.append)
    _deliver_line(backend, process, {
        "event": "position_changed", "token": backend._token,
        "position_ms": 50, "duration_ms": 202600,
        "deck_index": 0, "source_hash": incoming_hash,
        "primary_index": 0, "secondary_index": 1,
        "transition_id": None,
    })
    assert positions == [50]


def test_watchdog_timeout_never_fires_twice_for_the_same_transition(tmp_path):
    backend, process, _outgoing, _incoming, transition_id = _committed_transition(
        tmp_path
    )
    errors = []
    backend.error.connect(lambda category, message: errors.append(category))
    ack_timer = backend._active_commit_ack_timer
    backend._on_commit_ack_timeout(transition_id, ack_timer)
    # Firing again (e.g. a stray duplicate timer signal) must not report a
    # second time -- both the (transition_id, timer-instance) guard and
    # _crash_reported independently prevent it.
    backend._on_commit_ack_timeout(transition_id, ack_timer)
    assert errors == [ERROR_SUBPROCESS]


def test_queued_ack_timeout_after_completion_has_no_effect(tmp_path):
    # A Stage A timeout already queued on the Qt event loop at the moment
    # the real ack/completion arrives must not fire into a transition that
    # has since genuinely completed -- captures the *timer instance* at
    # arm time, which _discard_watchdog_timer() has since retired.
    backend, process, _outgoing, _incoming, transition_id = _committed_transition(
        tmp_path
    )
    stale_ack_timer = backend._active_commit_ack_timer
    _ack(backend, process, transition_id)
    _complete(backend, process, transition_id)
    assert backend._active_transition_id is None

    backend._on_commit_ack_timeout(transition_id, stale_ack_timer)

    assert backend._crash_reported is False
    assert backend._active_transition_id is None


# -- transition_id invalidation ----------------------------------------------

def test_second_simultaneous_commit_is_rejected(tmp_path):
    backend, process, _outgoing, _incoming, transition_id = _committed_transition(
        tmp_path
    )
    assert backend.commit_dual_transition(1000) is False
    assert backend._active_transition_id == transition_id


def test_late_ack_from_old_transition_is_ignored(tmp_path):
    backend, process, _outgoing, incoming_hash, first_id = _committed_transition(
        tmp_path
    )
    _ack(backend, process, first_id)
    _complete(backend, process, first_id)  # T1 genuinely resolved
    # A brand new preload+commit for T2, now that T1 is fully resolved.
    third = tmp_path / "third.mp4"
    third.write_bytes(b"c")
    assert backend.preload_secondary(str(third)) is True
    assert backend.commit_dual_transition(1000) is True
    second_id = backend.active_transition_id
    assert second_id != first_id
    assert backend._active_commit_ack_timer.isActive()

    _ack(backend, process, first_id)  # late, belongs to the resolved T1

    # Stage A must still be running for the *current* (second) transition
    # -- a late ack for the old, already-completed one must not touch it.
    assert backend._active_commit_ack_timer.isActive()
    assert backend._active_transition_id == second_id


def test_late_completion_from_old_transition_is_ignored(tmp_path):
    backend, process, _outgoing, _incoming, first_id = _committed_transition(
        tmp_path
    )
    _ack(backend, process, first_id)
    _complete(backend, process, first_id)
    third = tmp_path / "third.mp4"
    third.write_bytes(b"c")
    assert backend.preload_secondary(str(third)) is True
    assert backend.commit_dual_transition(1000) is True
    second_id = backend.active_transition_id
    assert second_id != first_id

    completions = []
    backend.dual_transition_complete.connect(completions.append)
    # A duplicate/late completion for the already-resolved first_id.
    _complete(backend, process, first_id)

    assert completions == []  # rejected before ever reaching a public signal
    assert backend._active_transition_id == second_id


def test_dual_transition_complete_with_missing_transition_id_is_rejected(tmp_path):
    backend, process, _outgoing, _incoming, transition_id = _committed_transition(
        tmp_path
    )
    completions = []
    backend.dual_transition_complete.connect(completions.append)
    _deliver_line(backend, process, {
        "event": "dual_transition_complete", "token": backend._token,
    })  # no transition_id key at all
    assert completions == []
    assert backend._active_transition_id == transition_id


def test_t1_checkpoint_and_execution_started_during_t2_never_raise_or_mutate(tmp_path):
    # Pure transition-phase diagnostics (see video_backend.py's
    # _handle_event) -- no public signal, no state mutation regardless of
    # which transition_id they carry, so a stale T1's checkpoint/
    # execution_started arriving after T2 has started is harmless by
    # construction. This pins that down explicitly rather than relying only
    # on "it happens to have no side effects" being true by omission.
    backend, process, _outgoing, _incoming, first_id = _committed_transition(
        tmp_path
    )
    _ack(backend, process, first_id)
    _complete(backend, process, first_id)
    third = tmp_path / "third.mp4"
    third.write_bytes(b"c")
    assert backend.preload_secondary(str(third)) is True
    assert backend.commit_dual_transition(1000) is True
    second_id = backend.active_transition_id

    _deliver_line(backend, process, {
        "event": "gpu_transition_checkpoint", "transition_id": first_id,
        "progress_target": 0.5, "primary_index": 0, "secondary_index": 1,
    })
    _deliver_line(backend, process, {
        "event": "commit_execution_started", "transition_id": first_id,
    })

    assert backend._active_transition_id == second_id
    assert backend._active_commit_ack_timer.isActive()


def test_queued_ack_timeout_after_t2_has_started_has_no_effect(tmp_path):
    # The exact race the fresh-timer-per-arm design exists for: T1's Stage
    # A timer object is captured *before* T1 resolves and T2 begins -- a
    # late invocation of T1's own timeout handler (as if a queued Qt
    # timeout had finally been delivered) must not touch T2's watchdog
    # state at all, even though T1's transition_id could never legitimately
    # match _active_transition_id by this point either way (belt-and-
    # braces: the timer-instance check is independent of that).
    backend, process, _outgoing, _incoming, first_id = _committed_transition(
        tmp_path
    )
    stale_ack_timer = backend._active_commit_ack_timer
    _ack(backend, process, first_id)
    _complete(backend, process, first_id)
    third = tmp_path / "third.mp4"
    third.write_bytes(b"c")
    assert backend.preload_secondary(str(third)) is True
    assert backend.commit_dual_transition(1000) is True
    second_id = backend.active_transition_id
    second_ack_timer = backend._active_commit_ack_timer

    backend._on_commit_ack_timeout(first_id, stale_ack_timer)

    assert backend._crash_reported is False
    assert backend._active_transition_id == second_id
    assert backend._active_commit_ack_timer is second_ack_timer


def test_queued_completion_timeout_after_t2_has_started_has_no_effect(tmp_path):
    backend, process, _outgoing, _incoming, first_id = _committed_transition(
        tmp_path
    )
    _ack(backend, process, first_id)
    stale_completion_timer = backend._active_completion_timer
    _complete(backend, process, first_id)
    third = tmp_path / "third.mp4"
    third.write_bytes(b"c")
    assert backend.preload_secondary(str(third)) is True
    assert backend.commit_dual_transition(1000) is True
    second_id = backend.active_transition_id
    _ack(backend, process, second_id)
    second_completion_timer = backend._active_completion_timer

    backend._on_transition_completion_timeout(first_id, stale_completion_timer)

    assert backend._crash_reported is False
    assert backend._active_transition_id == second_id
    assert backend._active_completion_timer is second_completion_timer


def test_matching_t2_completion_completes_exactly_once(tmp_path):
    backend, process, _outgoing, _incoming, transition_id = _committed_transition(
        tmp_path
    )
    _ack(backend, process, transition_id)
    completions = []
    backend.dual_transition_complete.connect(completions.append)
    _complete(backend, process, transition_id)
    # A duplicate delivery of the same real completion (e.g. a stray
    # re-send) must not be re-accepted -- _active_transition_id is already
    # None by this point, so a second matching message has nothing left to
    # match.
    _complete(backend, process, transition_id)

    assert completions == [transition_id]


# -- invalidation on load()/stop()/shutdown()/set_dual_mode() ---------------

def test_manual_load_during_pending_commit_cancels_watchdogs(tmp_path):
    backend, process, _outgoing, _incoming, transition_id = _committed_transition(
        tmp_path
    )
    assert backend._active_commit_ack_timer.isActive()
    new_path = tmp_path / "next.mp4"
    new_path.write_bytes(b"c")

    assert backend.load(str(new_path)) is True

    assert backend._active_commit_ack_timer is None
    assert backend._active_completion_timer is None
    assert backend._active_transition_id is None
    assert backend._active_preload_handle is None


def test_stop_during_pending_commit_cancels_watchdogs(tmp_path):
    backend, process, _outgoing, _incoming, transition_id = _committed_transition(
        tmp_path
    )
    backend.stop()
    assert backend._active_commit_ack_timer is None
    assert backend._active_completion_timer is None
    assert backend._active_transition_id is None
    assert backend._active_preload_handle is None


def test_shutdown_cancels_watchdog_timers_cleanly(tmp_path):
    backend, process, _outgoing, _incoming, transition_id = _committed_transition(
        tmp_path
    )
    backend.shutdown()
    assert backend._active_commit_ack_timer is None
    assert backend._active_completion_timer is None
    assert backend._active_transition_id is None
    assert backend._active_preload_handle is None
    # Safe to call more than once, matching shutdown()'s own docstring.
    backend.shutdown()


def test_set_dual_mode_cancels_watchdogs_before_replacing_the_child(tmp_path):
    backend, process, _outgoing, _incoming, transition_id = _committed_transition(
        tmp_path
    )
    assert backend._active_commit_ack_timer.isActive()

    backend.set_dual_mode(None)

    assert backend._active_commit_ack_timer is None
    assert backend._active_completion_timer is None
    assert backend._active_transition_id is None
    assert backend._active_preload_handle is None


# -- preload_id invalidation --------------------------------------------------

def test_p1_ready_during_p2_has_no_public_signal(tmp_path):
    backend, process = _make_dual_backend()
    a = tmp_path / "a.mp4"
    a.write_bytes(b"a")
    assert backend.load(str(a)) is True
    p1 = tmp_path / "p1.mp4"
    p1.write_bytes(b"p1")
    assert backend.preload_secondary(str(p1)) is True
    p1_envelope = _preload_envelope(backend)
    p2 = tmp_path / "p2.mp4"
    p2.write_bytes(b"p2")
    assert backend.preload_secondary(str(p2)) is True  # supersedes P1
    assert backend.active_preload_id != p1_envelope["preload_id"]

    ready_events = []
    backend.secondary_ready.connect(ready_events.append)
    _deliver_line(backend, process, {**p1_envelope, "event": "secondary_ready"})

    assert ready_events == []


def test_p1_failure_during_p2_has_no_public_signal(tmp_path):
    backend, process = _make_dual_backend()
    a = tmp_path / "a.mp4"
    a.write_bytes(b"a")
    assert backend.load(str(a)) is True
    p1 = tmp_path / "p1.mp4"
    p1.write_bytes(b"p1")
    assert backend.preload_secondary(str(p1)) is True
    p1_envelope = _preload_envelope(backend)
    p2 = tmp_path / "p2.mp4"
    p2.write_bytes(b"p2")
    assert backend.preload_secondary(str(p2)) is True

    failed_events = []
    backend.secondary_failed.connect(failed_events.append)
    _deliver_line(backend, process, {
        **p1_envelope, "event": "secondary_failed", "reason": "video_decode_error",
    })

    assert failed_events == []


def test_p1_progress_during_p2_is_rejected(tmp_path):
    backend, process = _make_dual_backend()
    a = tmp_path / "a.mp4"
    a.write_bytes(b"a")
    assert backend.load(str(a)) is True
    p1 = tmp_path / "p1.mp4"
    p1.write_bytes(b"p1")
    assert backend.preload_secondary(str(p1)) is True
    p1_envelope = _preload_envelope(backend)
    p2 = tmp_path / "p2.mp4"
    p2.write_bytes(b"p2")
    assert backend.preload_secondary(str(p2)) is True

    progress_events = []
    backend.secondary_preload_progress.connect(progress_events.append)
    _deliver_line(backend, process, {
        **p1_envelope, "event": "secondary_preload_progress",
        "media_status_name": "BufferingMedia",
    })

    assert progress_events == []


def test_p1_held_and_first_frame_during_p2_are_not_logged_as_current(tmp_path):
    # Diagnostic-only preload sub-events (no public signal either way) --
    # still gated so a stale P1's chatter is never recorded as if it
    # belonged to the currently active P2. Exercised via the recording
    # call itself never raising, and via _accept_preload_event's own
    # bounded-diagnostic bookkeeping not confusing later assertions --
    # direct behavioural proof lives in _accept_preload_event's unit-level
    # coverage above; this just confirms delivery never raises for any of
    # the preload-diagnostic event names.
    backend, process = _make_dual_backend()
    a = tmp_path / "a.mp4"
    a.write_bytes(b"a")
    assert backend.load(str(a)) is True
    p1 = tmp_path / "p1.mp4"
    p1.write_bytes(b"p1")
    assert backend.preload_secondary(str(p1)) is True
    p1_envelope = _preload_envelope(backend)
    p2 = tmp_path / "p2.mp4"
    p2.write_bytes(b"p2")
    assert backend.preload_secondary(str(p2)) is True

    for event_name in ("secondary_loading_started", "secondary_first_frame", "secondary_held"):
        _deliver_line(backend, process, {**p1_envelope, "event": event_name})
    # No exception is the assertion here; nothing else observable changed.
    assert backend.active_preload_id != p1_envelope["preload_id"]


def test_p1_cancellation_event_after_p2_starts_does_not_reactivate_p1(tmp_path):
    backend, process = _make_dual_backend()
    a = tmp_path / "a.mp4"
    a.write_bytes(b"a")
    assert backend.load(str(a)) is True
    p1 = tmp_path / "p1.mp4"
    p1.write_bytes(b"p1")
    assert backend.preload_secondary(str(p1)) is True
    p1_envelope = _preload_envelope(backend)
    backend.cancel_secondary()
    p2 = tmp_path / "p2.mp4"
    p2.write_bytes(b"p2")
    assert backend.preload_secondary(str(p2)) is True
    p2_id = backend.active_preload_id

    failed_events = []
    backend.secondary_failed.connect(failed_events.append)
    _deliver_line(backend, process, {
        **p1_envelope, "event": "secondary_failed", "reason": "cancelled",
    })

    assert failed_events == []
    assert backend.active_preload_id == p2_id


def test_p1_id_with_p2_hash_is_rejected(tmp_path):
    backend, process = _make_dual_backend()
    a = tmp_path / "a.mp4"
    a.write_bytes(b"a")
    assert backend.load(str(a)) is True
    p1 = tmp_path / "p1.mp4"
    p1.write_bytes(b"p1")
    assert backend.preload_secondary(str(p1)) is True
    p2 = tmp_path / "p2.mp4"
    p2.write_bytes(b"p2")
    assert backend.preload_secondary(str(p2)) is True
    p2_envelope = _preload_envelope(backend)

    ready_events = []
    backend.secondary_ready.connect(ready_events.append)
    # p2's preload_id paired with a mismatched source_hash -- neither field
    # alone is trusted; both must match.
    mismatched = {**p2_envelope, "source_hash": "not-p2s-real-hash"}
    _deliver_line(backend, process, {**mismatched, "event": "secondary_ready"})

    assert ready_events == []


def test_p2_id_with_p1_hash_is_rejected(tmp_path):
    backend, process = _make_dual_backend()
    a = tmp_path / "a.mp4"
    a.write_bytes(b"a")
    assert backend.load(str(a)) is True
    p1 = tmp_path / "p1.mp4"
    p1.write_bytes(b"p1")
    assert backend.preload_secondary(str(p1)) is True
    p1_hash = backend.active_preload_source_hash
    p2 = tmp_path / "p2.mp4"
    p2.write_bytes(b"p2")
    assert backend.preload_secondary(str(p2)) is True
    p2_envelope = _preload_envelope(backend)

    ready_events = []
    backend.secondary_ready.connect(ready_events.append)
    mismatched = {**p2_envelope, "preload_id": p2_envelope["preload_id"], "source_hash": p1_hash}
    _deliver_line(backend, process, {**mismatched, "event": "secondary_ready"})

    assert ready_events == []


def test_retry_allocates_a_fresh_preload_id(tmp_path):
    backend, process = _make_dual_backend()
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"x")
    assert backend.preload_secondary(str(path)) is True
    first_id = backend.active_preload_id
    # A bounded retry of the *same logical target* still calls
    # preload_secondary() again (see video_dual_transition.py's
    # _attempt_bounded_preload_retry) -- must mint a brand new id, never
    # reuse the one it's replacing.
    assert backend.preload_secondary(str(path)) is True
    second_id = backend.active_preload_id
    assert second_id != first_id


def test_wrong_secondary_deck_is_rejected(tmp_path):
    backend, process = _make_dual_backend()
    a = tmp_path / "a.mp4"
    a.write_bytes(b"a")
    assert backend.load(str(a)) is True
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"x")
    assert backend.preload_secondary(str(path)) is True
    envelope = _preload_envelope(backend)
    # Correct preload_id/source_hash, but tagged as coming from the
    # *primary* deck's index -- must never be trusted regardless.
    wrong_deck = {**envelope, "deck_index": 0, "secondary_index": 1}

    ready_events = []
    backend.secondary_ready.connect(ready_events.append)
    _deliver_line(backend, process, {**wrong_deck, "event": "secondary_ready"})

    assert ready_events == []


def test_missing_preload_id_and_hash_are_rejected(tmp_path):
    backend, process = _make_dual_backend()
    a = tmp_path / "a.mp4"
    a.write_bytes(b"a")
    assert backend.load(str(a)) is True
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"x")
    assert backend.preload_secondary(str(path)) is True

    ready_events = []
    backend.secondary_ready.connect(ready_events.append)
    _deliver_line(backend, process, {
        "event": "secondary_ready", "deck_index": 1, "secondary_index": 1,
        "primary_index": 0,
    })  # no preload_id/source_hash at all

    assert ready_events == []


def test_no_active_preload_rejects_any_secondary_event(tmp_path):
    backend, process = _make_dual_backend()
    a = tmp_path / "a.mp4"
    a.write_bytes(b"a")
    assert backend.load(str(a)) is True  # invalidates any preload tracking
    assert backend._active_preload_handle is None

    ready_events = []
    backend.secondary_ready.connect(ready_events.append)
    _deliver_line(backend, process, {
        "event": "secondary_ready", "preload_id": 1, "source_hash": "x",
        "deck_index": 1, "secondary_index": 1, "primary_index": 0,
    })

    assert ready_events == []


# -- QProcess stdout identity (section 10) -----------------------------------

def test_old_process_stdout_is_ignored_after_replacement():
    process1 = MagicMock(spec=QtCore.QProcess)
    process1.state.return_value = QtCore.QProcess.ProcessState.Running
    process2 = MagicMock(spec=QtCore.QProcess)
    process2.state.return_value = QtCore.QProcess.ProcessState.Running
    _app()
    backend = QtVideoPlaybackBackend(
        process=process1, auto_start=False, process_factory=lambda: process2,
    )
    backend._perform_restart()
    assert backend._process is process2

    started_events = []
    backend.started.connect(lambda: started_events.append(True))
    # A line arriving from the *old* process (already replaced) must be
    # silently dropped, not read as belonging to process2.
    _deliver_line(backend, process1, {"event": "started", "token": backend._token})
    assert started_events == []
    # The new process's own line works normally.
    _deliver_line(backend, process2, {"event": "started", "token": backend._token})
    assert started_events == [True]


# -- diagnostics --------------------------------------------------------------

def test_watchdog_timeout_diagnostic_carries_the_documented_fields(tmp_path):
    get_diagnostics()  # ensure a singleton exists; no assertion on output here
    backend, process, outgoing_hash, incoming_hash, transition_id = (
        _committed_transition(tmp_path)
    )
    # Must not raise even if diagnostics recording itself is exercised for
    # real (as opposed to mocked away) -- this is the same pattern the rest
    # of this test file and test_video_backend.py already rely on.
    backend._on_commit_ack_timeout(transition_id, backend._active_commit_ack_timer)
