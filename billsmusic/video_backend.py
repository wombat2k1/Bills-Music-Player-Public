"""Built-in video playback via a dedicated child process running Qt
Multimedia -- never VLC, never an external player, and never sharing a
process with the rest of the app's threading (Cast/zeroconf discovery,
library scanning, artwork/analysis workers, ...). QMediaPlayer's internal
decode threads corrupting memory when they ran alongside that other
background activity was the root cause of a hard-to-pin-down crash; running
the player in its own OS process (its own GIL, its own decode threads,
nothing else in it) removes that interaction entirely.

The child process (video_subprocess.py) owns the actual QMediaPlayer/
QAudioOutput/QVideoWidget. This module talks to it over line-delimited JSON
on stdin/stdout via QProcess, and embeds its video widget into this
process's UI by native window ID (QWindow.fromWinId + createWindowContainer)
-- the picture is still genuinely part of this window, not a separate
window floating on top.

The child process can still crash on its own (it's still real Qt
Multimedia/FFmpeg code) -- this module watches QProcess's started/finished/
errorOccurred signals, reports a crash exactly once, and safely relaunches
a fresh child before the next load() rather than leaving playback
permanently dead for the rest of the session.

Public API (signals and methods) is unchanged from the single-process
version so window.py/party_mode.py did not need to change how they call it.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

from PyQt6 import QtCore, QtGui, QtWidgets

from .performance_diagnostics import get_diagnostics
from .platform_utils import current_executable_path, is_frozen_build

# Stable failure categories -- see PLAYBACK ERRORS in the project spec.
ERROR_FILE_MISSING = "video_file_missing"
ERROR_FORMAT_UNSUPPORTED = "video_format_unsupported"
ERROR_RESOURCE = "video_resource_error"
ERROR_DECODE = "video_decode_error"
ERROR_AUDIO_OUTPUT = "video_audio_output_error"
ERROR_UNKNOWN = "video_unknown_error"
ERROR_SUBPROCESS = "video_subprocess_error"

_MAX_RESTART_ATTEMPTS = 3
# Bounded two-stage watchdog for a committed GPU dual-transition (real-
# device bug, 2026-08-24: a 1000ms transition stayed marked "transitioning"
# for 6+ seconds before the subprocess crash was even detected). Stage A
# bounds how long a healthy child should take to acknowledge a commit at
# all (local IPC over an already-open pipe -- if this fires, the child was
# almost certainly already unresponsive before the commit was even sent,
# not because of it). Stage B bounds the whole remaining transition
# lifecycle once acknowledged, so a transition can never again sit
# "transitioning" indefinitely. Named constants, not magic numbers
# scattered at call sites -- see commit_dual_transition()/_handle_event().
_COMMIT_ACK_TIMEOUT_MS = 900
_TRANSITION_COMPLETION_GRACE_MS = 1500
# Lower-level preload sub-events (no public signal, no state mutation of
# their own) -- still gated through _accept_preload_event() before being
# logged, unlike the pure transition-phase diagnostics further down in
# _handle_event(), so a stale preload's chatter is never recorded as if it
# belonged to the currently active one. See video_subprocess.py's
# _secondary_event_envelope for what every one of these now carries.
_PRELOAD_DIAGNOSTIC_EVENTS = frozenset({
    "secondary_loading_started", "secondary_first_frame",
    "secondary_seek_issued", "secondary_seek_position_report",
    "secondary_held",
})


@dataclass(frozen=True)
class VideoPreloadHandle:
    """Immutable identity for one secondary-deck preload attempt (Codex
    design review, 2026-08-24 correctness hardening). A fresh handle --
    with a fresh ``preload_id`` -- is minted on every call to
    preload_secondary(), including every bounded retry of the same
    logical target (see video_dual_transition.py's
    _attempt_bounded_preload_retry): a retry is a *new* preload as far as
    identity is concerned, never a continuation of the old one. Compared
    against the identity every child event about the secondary deck now
    carries (see video_subprocess.py's _secondary_event_envelope) before
    any such event is allowed to mutate state or reach a public signal --
    see _accept_preload_event()."""
    preload_id: int
    source_hash: Optional[str]


def _subprocess_launch_command(dual_mode: Optional[str] = None, *, probe_only: bool = False):
    """Returns (program, args) to launch the video child process. A
    packaged (Nuitka/PyInstaller) build has no standalone python.exe --
    the app re-invokes its own executable with a flag Main.py checks for
    before anything else; running from source uses a normal `python -m`
    invocation instead.

    Getting the *flag* choice wrong in a packaged build silently launches
    a second full copy of the app instead of the video subprocess (Main.py's
    `--video-subprocess` argv check never matches `-m
    billsmusic.video_subprocess`, so it falls through to its normal
    startup path) -- that duplicate instance never sends the "ready"
    handshake this module waits for. Getting the *program path* wrong
    (see current_executable_path()'s docstring -- sys.executable reports
    the wrong filename in this Nuitka build) fails even earlier, before
    any process starts at all. Either failure surfaces identically to the
    user as "video player stopped unexpectedly".

    ``dual_mode`` is ``None`` (classic, default), ``"cpu"`` (the confirmed-
    crashing Phase 2A CPU compositor, permanently unavailable -- see
    window.py's DUAL_VIDEO_TRANSITIONS_AVAILABLE) or ``"gpu"`` (the Qt
    Quick/RHI compositor) -- appends ``--dual-deck``/``--dual-deck-gpu``
    respectively, selecting the corresponding controller in
    video_subprocess.py's main(). ``probe_only`` additionally appends
    ``--probe-only``, which only makes sense paired with ``dual_mode="gpu"``
    (see GpuCompositorProbe) -- the child checks GPU/RHI capability and
    exits without ever announcing "ready" or loading any video."""
    program = current_executable_path()
    extra = []
    if dual_mode == "cpu":
        extra.append("--dual-deck")
    elif dual_mode == "gpu":
        extra.append("--dual-deck-gpu")
    if probe_only:
        extra.append("--probe-only")
    if is_frozen_build():
        return program, ["--video-subprocess"] + extra
    return program, ["-m", "billsmusic.video_subprocess"] + extra


class QtVideoPlaybackBackend(QtCore.QObject):
    started = QtCore.pyqtSignal()
    paused = QtCore.pyqtSignal()
    stopped = QtCore.pyqtSignal()
    position_changed = QtCore.pyqtSignal(int)
    duration_changed = QtCore.pyqtSignal(int)
    end_of_media = QtCore.pyqtSignal()
    error = QtCore.pyqtSignal(str, str)
    double_clicked = QtCore.pyqtSignal()
    escape_pressed = QtCore.pyqtSignal()
    context_menu_requested = QtCore.pyqtSignal()
    # Phase 2A (experimental dual-video cross-dissolve) -- only ever emitted
    # when the child process was launched in dual mode (see set_dual_mode()).
    # secondary_ready/secondary_failed carry the *validated* event envelope
    # (preload_id, source_hash, deck_index, primary_index, secondary_index,
    # plus whatever else the child included) rather than nothing/a bare
    # reason string -- callers that need to independently re-verify
    # identity (DualVideoTransitionEngine.on_secondary_ready/on_secondary_
    # failed, see video_dual_transition.py) have it available without a
    # second round-trip. Both are only ever emitted after
    # _accept_preload_event() has already confirmed the event belongs to
    # the currently active preload -- see _handle_event().
    secondary_ready = QtCore.pyqtSignal(dict)
    secondary_failed = QtCore.pyqtSignal(dict)
    # Stage B -- unlike the diagnostics-only tuple below, DualVideoTransitionEngine
    # must actually react to this one (adaptive preload timeout extension),
    # so it needs a real signal, not just a recorded diagnostic.
    secondary_preload_progress = QtCore.pyqtSignal(dict)
    # Carries the transition_id it belongs to -- only ever emitted for the
    # transition this backend currently considers active (see
    # _handle_event()'s dual_transition_complete branch); a late/stale
    # completion for an already-superseded transition never reaches this
    # signal at all, not even for a caller to filter itself.
    dual_transition_complete = QtCore.pyqtSignal(int)
    # Correctness hardening (2026-08-24): a typed failure specifically for
    # a *committed* GPU transition (watchdog timeout or a process crash
    # while one was active) -- carries the exact transition_id so a
    # listener (DualVideoTransitionEngine.primary_failed) can refuse to
    # act on a failure that belongs to an already-superseded transition.
    # Deliberately separate from the generic ``error`` signal, which
    # continues to drive window.py's display/error cleanup only -- it must
    # never independently abort whichever transition happens to be active.
    dual_transition_failed = QtCore.pyqtSignal(int, str)
    # v1.0.71 correction: on-demand echo of the *actual* primary deck audio
    # state, queried only at the specific checkpoints window.py's mixed-
    # media Audio<->Video transition cares about (see query_audio_state()) --
    # never emitted continuously. Exists because set_volume()/set_muted()
    # are fire-and-forget IPC commands: nothing before this told the parent
    # whether a requested value actually landed on the deck currently
    # rendering, as opposed to a stale/wrong one -- see CODEX_HANDOFF.md's
    # "mixed_video_audio_state" diagnostic for why real acceptance testing
    # needed this distinction.
    audio_state_reported = QtCore.pyqtSignal(dict)

    def __init__(
        self, parent: Optional[QtCore.QObject] = None, *,
        process: Optional[QtCore.QProcess] = None,
        auto_start: bool = True,
        process_factory: Optional[Callable[[], QtCore.QProcess]] = None,
        dual_mode: Optional[str] = None,
    ):
        super().__init__(parent)
        self._shutdown = False
        self._dual_mode = dual_mode
        self._token = 0
        self._current_position_ms = 0
        self._current_duration_ms = 0
        self._is_playing = False
        self._win_id: Optional[int] = None
        self._embedded_container: Optional[QtWidgets.QWidget] = None
        self._pending_attach_target: Optional[QtWidgets.QWidget] = None
        self._last_attach_target: Optional[QtWidgets.QWidget] = None
        self._process_ready = False
        self._crash_reported = False
        self._restart_scheduled = False
        self._restart_attempts = 0
        self._geometry_sync_timer = QtCore.QTimer(self)
        self._geometry_sync_timer.setSingleShot(True)
        self._geometry_sync_timer.timeout.connect(self.sync_output_geometry)
        self._geometry_sync_late_timer = QtCore.QTimer(self)
        self._geometry_sync_late_timer.setSingleShot(True)
        self._geometry_sync_late_timer.timeout.connect(self.sync_output_geometry)

        # -- GPU dual-transition identity/watchdog state (real-device bug,
        # 2026-08-24 -- see module-level constants and _handle_event()) --
        # ``_authoritative_source_hash`` is the parent's own belief about
        # which track's timing is currently real, updated on load() and on
        # a *confirmed* dual_transition_complete -- deliberately never
        # inferred from whatever window.py's self.current_path happens to
        # already say, since that flips to the incoming track immediately
        # at commit (for UI responsiveness), well before the subprocess's
        # own deck swap actually happens.
        self._authoritative_source_hash: Optional[str] = None
        self._pending_secondary_hash: Optional[str] = None
        self._last_rejected_stale_hash: Optional[object] = object()  # never equals a real hash/None
        self._next_transition_id = 0
        self._active_transition_id: Optional[int] = None
        self._transition_commit_monotonic: Optional[float] = None
        self._transition_completion_deadline_ms: Optional[int] = None
        self._last_child_event_name: Optional[str] = None
        self._last_child_event_monotonic: Optional[float] = None
        # Correctness hardening (2026-08-24, Codex design review): each
        # watchdog stage gets a *fresh* QTimer per arm rather than one
        # shared, repeatedly restarted instance -- stopping a QTimer does
        # not retroactively cancel a timeout already queued for delivery on
        # the Qt event loop (the same class of race _is_stale_process_signal
        # already guards against for QProcess signals), so a queued T1
        # timeout must be independently rejected by identity even if it
        # still manages to fire after T2 has started. See _arm_commit_ack_
        # timer/_arm_completion_timer/_discard_watchdog_timer and the
        # timeout handlers' own dual (transition_id, timer-instance) check.
        self._active_commit_ack_timer: Optional[QtCore.QTimer] = None
        self._active_completion_timer: Optional[QtCore.QTimer] = None
        # Immutable per-attempt preload identity (see VideoPreloadHandle).
        self._next_preload_id = 0
        self._active_preload_handle: Optional[VideoPreloadHandle] = None
        self._last_rejected_stale_preload_key: Optional[object] = None

        # Injectable for tests: a factory used both for the very first
        # process and for every restart after a crash. The real app always
        # uses the default, launching video_subprocess.py.
        self._process_factory = process_factory or self._default_process_factory
        self._process: Optional[QtCore.QProcess] = None
        if process is not None:
            self._process = process
            self._wire_process(process)
            if auto_start:
                process.start()
        else:
            self._launch_new_process(auto_start=auto_start)

    def _default_process_factory(self) -> QtCore.QProcess:
        process = QtCore.QProcess(self)
        program, args = _subprocess_launch_command(dual_mode=self._dual_mode)
        process.setProgram(program)
        process.setArguments(args)
        try:
            get_diagnostics().record(
                "playback", "video_subprocess_launch_command",
                details={
                    "program": program, "args": args,
                    "is_frozen_build": is_frozen_build(),
                    "sys_frozen": bool(getattr(sys, "frozen", False)),
                    "sys_executable": sys.executable,
                },
                minimum_level="basic",
            )
        except Exception:
            pass
        return process

    def _wire_process(self, process: QtCore.QProcess):
        # Correctness hardening (2026-08-24): the exact process object is
        # captured by this specific connection at wiring time, not read
        # from self._process when the slot eventually runs -- a
        # readyReadStandardOutput already queued from an old process at
        # the moment it's replaced must not be read as belonging to
        # whatever self._process happens to point at by the time it's
        # delivered (mirrors _is_stale_process_signal()'s existing
        # rationale for the error/finished signals below, which already
        # use QObject.sender() for the same reason -- stdout has no
        # equivalent "sender" concept available inside the slot, so the
        # process is threaded through explicitly instead). See
        # _on_stdout_ready's own identity check.
        process.readyReadStandardOutput.connect(
            lambda p=process: self._on_stdout_ready(p)
        )
        process.errorOccurred.connect(self._on_process_error)
        process.started.connect(self._on_process_started)
        process.finished.connect(self._on_process_finished)

    def _unwire_process(self, process: QtCore.QProcess):
        """Disconnects a process's signals before it is intentionally
        replaced while still alive (set_dual_mode()) or torn down after an
        already-handled crash (_perform_restart()). Without this, forcibly
        closing/killing a still-running process fires its own
        errorOccurred(Crashed)/finished(CrashExit) into the same
        _on_process_error/_on_process_finished handlers a genuine crash
        uses -- since those only check self._shutdown (True solely during
        full backend shutdown(), not a mode-switch restart), the
        intentional termination of the OLD process gets misread as a fresh
        crash of whatever self._process currently points at, scheduling a
        spurious restart that kills the just-launched replacement before it
        reaches "ready" and repeats until the restart budget is exhausted.
        Confirmed via diagnostics: a single set_dual_mode("gpu") call
        produced 4 subprocess launches and burned all 3 restart attempts in
        ~1.1s before this fix."""
        try:
            # readyReadStandardOutput was connected to a per-process lambda
            # (see _wire_process), not a plain bound-method slot -- there is
            # only ever one connection on this specific process's signal, so
            # disconnecting all of them is equivalent and avoids having to
            # keep the exact lambda object around just to name it here.
            process.readyReadStandardOutput.disconnect()
        except Exception:
            pass
        for signal, slot in (
            (process.errorOccurred, self._on_process_error),
            (process.started, self._on_process_started),
            (process.finished, self._on_process_finished),
        ):
            try:
                signal.disconnect(slot)
            except Exception:
                pass

    def _launch_new_process(self, auto_start: bool = True):
        self._process_ready = False
        process = self._process_factory()
        self._wire_process(process)
        self._process = process
        if auto_start:
            process.start()

    # -- child process lifecycle -------------------------------------------------
    def _on_process_started(self):
        # The OS process exists now, but it isn't actually usable until it
        # completes the "ready" handshake below (QApplication constructed,
        # video widget realized, stdin reader running).
        pass

    def _is_stale_process_signal(self) -> bool:
        """True if the signal currently being handled came from a process
        that is no longer self._process. _unwire_process() disconnecting
        the old process's signals before it's intentionally torn down is
        the primary defence, but disconnect() is not a substitute for this
        check: Qt can still deliver a signal that was already in flight
        (e.g. emitted synchronously from inside the very kill()/
        waitForFinished() call tearing that process down) before the
        disconnect takes effect, and this identity check is what actually
        stops it from being misread as a crash of whatever process
        self._process currently points at. self.sender() is only valid
        while directly inside a slot invoked by a real signal emission,
        which is exactly the context every caller of this uses it in."""
        sender = self.sender()
        return sender is not None and sender is not self._process

    def _on_process_finished(self, exit_code=None, exit_status=None):
        if self._is_stale_process_signal():
            return
        self._process_ready = False
        if self._shutdown:
            return
        self._handle_process_crash(exit_code=exit_code, exit_status=exit_status)

    def _on_process_error(self, process_error=None):
        if self._is_stale_process_signal():
            return
        if self._shutdown:
            return
        # errorOccurred fires for cases finished() doesn't always follow
        # (e.g. FailedToStart never gets a finished signal at all) -- both
        # paths funnel into the same crash handling so neither is missed.
        self._handle_process_crash(process_error=process_error)

    def _handle_process_crash(
        self, exit_code=None, exit_status=None, process_error=None, reason=None,
    ):
        self._process_ready = False
        # 1. capture active T (may be None -- a crash with no committed
        #    transition in flight, the common case, needs no typed
        #    failure). 2. invalidate T and its watchdogs.
        failed_transition_id = self._active_transition_id
        self._cancel_transition_watchdogs()
        self._active_preload_handle = None
        if failed_transition_id is not None:
            # 3. emit typed failure for T -- covers *both* paths that reach
            #    this method: a watchdog timeout (_fail_active_transition,
            #    reason already set to "commit_ack_timeout"/"transition_
            #    completion_timeout") and a genuinely OS-detected crash
            #    arriving directly via _on_process_error/_on_process_
            #    finished while a transition happened to be committed
            #    (reason=None here -> "process_signal", matching
            #    _report_crash_once's own default below).
            self.dual_transition_failed.emit(
                failed_transition_id, reason or "process_signal",
            )
        # 4-5. existing user-facing error reporting + safe subprocess
        #    recovery/relaunch -- unchanged, proven path.
        self._report_crash_once(exit_code, exit_status, process_error, reason=reason)
        # The dead process's embedded window is gone with it -- discard the
        # container and remember where it was attached so the replacement
        # lands in the same place automatically once it's ready.
        if self._embedded_container is not None:
            try:
                self._embedded_container.setParent(None)
                self._embedded_container.deleteLater()
            except Exception:
                pass
            self._embedded_container = None
        self._win_id = None
        if self._last_attach_target is not None:
            self._pending_attach_target = self._last_attach_target
        self._schedule_restart()

    def _report_crash_once(
        self, exit_code=None, exit_status=None, process_error=None, reason=None,
    ):
        # "Report it once": errorOccurred and finished commonly both fire
        # for the same crash -- only the first turns into a user-visible
        # error signal. ``reason`` distinguishes a genuinely OS-detected
        # crash (default, "process_signal") from one this backend's own
        # bounded transition watchdog decided to treat the same way after
        # getting no acknowledgement/completion in time -- see
        # _on_commit_ack_timeout/_on_transition_completion_timeout. Both
        # funnel into this exact same, already-proven recovery path
        # (report once, tear down, relaunch) rather than a second one.
        if self._crash_reported:
            return
        self._crash_reported = True
        message = "unknown error"
        state = None
        stderr_tail = ""
        if self._process is not None:
            try:
                message = self._process.errorString() or message
                state = self._process.state()
            except Exception:
                pass
            # The child's own stderr is the only place a Python traceback or
            # a fatal Qt/QML message would show up -- process_error/exit_code
            # alone only tell us the OS-level shape of the failure (e.g.
            # "Crashed"), never *why*. Capped to stay well under any
            # diagnostics payload limit. This is the process's *entire*
            # accumulated stderr up to now -- e.g. a codec/format warning
            # logged when an earlier, unrelated file was opened can still
            # be sitting in this tail; do not treat its mere presence as
            # evidence it caused this particular failure.
            try:
                stderr_tail = bytes(self._process.readAllStandardError()).decode(
                    "utf-8", errors="replace"
                )[-4000:]
            except Exception:
                pass
        try:
            get_diagnostics().record(
                "playback", "video_subprocess_crash_detail", severity="warning",
                details={
                    "error_string": message,
                    "process_error": str(process_error),
                    "exit_code": exit_code,
                    "exit_status": str(exit_status),
                    "process_state": str(state),
                    "dual_mode": self._dual_mode,
                    "stderr_tail": stderr_tail,
                    "reason": reason or "process_signal",
                },
                minimum_level="basic",
            )
        except Exception:
            pass
        self.error.emit(
            ERROR_SUBPROCESS, f"Video playback process failed: {message}",
        )

    # -- GPU dual-transition ack/completion watchdog -----------------------
    def _discard_watchdog_timer(self, attr_name: str) -> None:
        """Stops and schedules deletion of whatever QTimer currently lives
        at ``attr_name`` (if any), then clears the attribute. Used both to
        retire a superseded timer when a fresh one is armed and to fully
        stand both watchdogs down -- see _arm_commit_ack_timer/_arm_
        completion_timer/_cancel_transition_watchdogs. deleteLater() (not a
        bare del/reference drop) because a timeout already queued for this
        timer on the Qt event loop must not fire into a half-torn-down
        Python object; Qt's own deferred deletion only runs once that
        queued delivery has already been processed (and, since the timeout
        handlers below independently check "is this still the
        authoritative timer", a delivery that does still slip through
        before deleteLater() runs is harmless regardless)."""
        timer = getattr(self, attr_name, None)
        if timer is not None:
            try:
                timer.stop()
                timer.deleteLater()
            except Exception:
                pass
        setattr(self, attr_name, None)

    def _arm_commit_ack_timer(self, transition_id: int) -> None:
        self._discard_watchdog_timer("_active_commit_ack_timer")
        timer = QtCore.QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(
            lambda: self._on_commit_ack_timeout(transition_id, timer)
        )
        self._active_commit_ack_timer = timer
        timer.start(_COMMIT_ACK_TIMEOUT_MS)

    def _arm_completion_timer(self, transition_id: int, deadline_ms: int) -> None:
        self._discard_watchdog_timer("_active_completion_timer")
        timer = QtCore.QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(
            lambda: self._on_transition_completion_timeout(transition_id, timer)
        )
        self._active_completion_timer = timer
        timer.start(deadline_ms)

    def _cancel_transition_watchdogs(self):
        self._discard_watchdog_timer("_active_commit_ack_timer")
        self._discard_watchdog_timer("_active_completion_timer")
        self._active_transition_id = None
        self._transition_commit_monotonic = None
        self._transition_completion_deadline_ms = None

    def _on_commit_ack_timeout(self, transition_id: int, timer: QtCore.QTimer) -> None:
        # Correctness hardening (2026-08-24): requires *both* the captured
        # transition_id to still be the active one *and* the captured timer
        # to still be the currently-authoritative watchdog instance for
        # this stage -- a queued T1 timeout delivered after T2 has already
        # armed its own fresh timer must fail neither of these, even in the
        # pathological case where transition_id alone happened to be
        # ambiguous (it never is in practice -- ids are minted once and
        # never reused -- but the timer-instance check makes that
        # independent of that assumption ever continuing to hold).
        if transition_id != self._active_transition_id:
            return
        if timer is not self._active_commit_ack_timer:
            return
        # A healthy child acknowledges over an already-open local pipe
        # almost instantly -- not receiving even that within the bounded
        # window means it was most likely already unresponsive before this
        # commit was ever sent, not because of it (see module docstring's
        # real-device timeline: the last confirmed-healthy child event
        # preceded the commit by several seconds both times this was
        # observed). Treated exactly like a detected crash: report once,
        # tear the child down, relaunch.
        self._record_transition_watchdog_event(
            "video_transition_commit_ack_timeout", transition_id,
        )
        self._fail_active_transition(transition_id, reason="commit_ack_timeout")

    def _on_transition_completion_timeout(
        self, transition_id: int, timer: QtCore.QTimer,
    ) -> None:
        if transition_id != self._active_transition_id:
            return
        if timer is not self._active_completion_timer:
            return
        self._record_transition_watchdog_event(
            "video_transition_completion_timeout", transition_id,
        )
        self._fail_active_transition(
            transition_id, reason="transition_completion_timeout",
        )

    def _record_transition_watchdog_event(self, operation: str, transition_id: int):
        now = time.monotonic()
        ms_since_commit = None
        if self._transition_commit_monotonic is not None:
            ms_since_commit = int((now - self._transition_commit_monotonic) * 1000)
        ms_since_last_child_event = None
        if self._last_child_event_monotonic is not None:
            ms_since_last_child_event = int(
                (now - self._last_child_event_monotonic) * 1000
            )
        try:
            get_diagnostics().record(
                "playback", operation, severity="warning",
                details={
                    "transition_id": transition_id,
                    "primary_source_hash": self._authoritative_source_hash,
                    "secondary_source_hash": self._pending_secondary_hash,
                    "process_state": (
                        str(self._process.state())
                        if self._process is not None else "None"
                    ),
                    "ms_since_commit_sent": ms_since_commit,
                    "ms_since_last_child_event": ms_since_last_child_event,
                    "last_child_event_name": self._last_child_event_name,
                },
                minimum_level="basic",
            )
        except Exception:
            pass

    def _fail_active_transition(self, transition_id: int, *, reason: str):
        # Guards against a race where the transition already resolved
        # (completed/was superseded) between the timer firing and this
        # handler actually running -- QTimer.stop() is not instantaneous
        # protection against a timeout already queued on the event loop.
        if self._active_transition_id != transition_id:
            return
        # _handle_process_crash() itself now performs all five steps
        # (capture T, invalidate T + watchdogs, emit the typed dual_
        # transition_failed(T, reason) failure, existing user-facing error
        # reporting, existing safe subprocess recovery/relaunch) -- shared
        # with the genuinely-OS-detected-crash path so a transition failure
        # is reported identically regardless of which one caught it.
        self._handle_process_crash(reason=reason)

    def _schedule_restart(self):
        if self._shutdown or self._restart_scheduled:
            return
        if self._restart_attempts >= _MAX_RESTART_ATTEMPTS:
            return
        self._restart_attempts += 1
        self._restart_scheduled = True
        QtCore.QTimer.singleShot(0, self._perform_restart)

    def _ensure_process_stopped(self, process):
        """Used only by _perform_restart(), where ``process`` has just
        crashed/errored -- unlike _stop_process_gracefully() (used by
        shutdown() and set_dual_mode(), where the process is known-healthy
        and worth asking nicely), writing an IPC command to a process that
        is mid-crash is itself questionable, and empirically correlated
        with an unbounded restart loop (each replacement child exiting
        almost immediately with a clean exit_code=0/NormalExit, as if it
        had itself received a stray "shutdown" command) rather than the
        bounded, genuine-crash-driven restarts _MAX_RESTART_ATTEMPTS
        expects. This only confirms the process is actually gone, killing
        it if not, without trying to talk to it."""
        try:
            still_running = (
                process.state() != QtCore.QProcess.ProcessState.NotRunning
            )
        except Exception:
            still_running = True
        if still_running:
            try:
                process.kill()
                process.waitForFinished(1000)
            except Exception:
                pass

    def _perform_restart(self):
        self._restart_scheduled = False
        if self._shutdown:
            return
        old_process = self._process
        if old_process is not None:
            # errorOccurred(Crashed) firing does not guarantee the OS has
            # actually finished tearing the process down yet -- only that
            # Qt has detected the error condition. deleteLater() alone
            # (the old behaviour) does not wait for that, so a crashed-but-
            # not-yet-fully-exited process could still overlap with its own
            # replacement for a moment. That overlap is exactly the
            # two-concurrent-children-of-this-exe condition set_dual_mode()
            # was fixed to avoid (see its docstring: confirmed via a real
            # WER crash dump to fault natively inside Qt6Core.dll), and
            # matters here too since this same path re-runs on every
            # restart attempt within a crash-restart cascade.
            self._unwire_process(old_process)
            # See set_dual_mode()'s matching comment: cleared before (not
            # after) tearing old_process down so _is_stale_process_signal()
            # rejects a signal already in flight from it, independent of
            # whether disconnect() itself took effect in time.
            self._process = None
            self._ensure_process_stopped(old_process)
        self._launch_new_process(auto_start=True)
        if old_process is not None:
            try:
                old_process.deleteLater()
            except Exception:
                pass

    # -- child process I/O -----------------------------------------------------
    def _on_stdout_ready(self, process: QtCore.QProcess):
        # Reject before reading/parsing anything at all -- see
        # _wire_process's comment. A stale process's buffered stdout is
        # simply never read; if a replacement process is later attached
        # via _launch_new_process, it gets its own fresh connection and
        # its own readyReadStandardOutput deliveries.
        if process is not self._process:
            return
        while self._process is not None and self._process.canReadLine():
            raw = bytes(self._process.readLine()).decode("utf-8", errors="replace").strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except Exception:
                continue
            self._handle_event(event)

    def _send(self, command: dict) -> bool:
        """Returns whether the command was actually handed to a process
        QProcess believes is alive -- callers that promise the caller
        something happened (load()) must not claim success when this is
        False."""
        if self._shutdown or self._process is None:
            return False
        if self._process.state() == QtCore.QProcess.ProcessState.NotRunning:
            return False
        try:
            self._process.write((json.dumps(command) + "\n").encode("utf-8"))
            return True
        except Exception:
            return False

    def _handle_event(self, event: dict):
        name = event.get("event")
        if name:
            # Unconditional, for every event regardless of type -- purely
            # diagnostic (see _record_transition_watchdog_event's
            # "ms_since_last_child_event"/"last_child_event_name"), not
            # part of any accept/reject decision below.
            self._last_child_event_name = name
            self._last_child_event_monotonic = time.monotonic()
        if name == "ready":
            # Proof the (possibly just-restarted) child is genuinely
            # functional again -- a future crash gets its own fresh report
            # and restart budget.
            self._process_ready = True
            self._crash_reported = False
            self._restart_attempts = 0
            self._win_id = event.get("win_id")
            self._create_embedded_container()
            return
        if name == "audio_state_report":
            self.audio_state_reported.emit({k: v for k, v in event.items() if k != "event"})
            return
        # Input events forwarded from the embedded window (see
        # video_subprocess.py) -- not tied to a particular load()/token,
        # since they're raw UI interaction, not media playback state.
        if name == "double_clicked":
            self.double_clicked.emit()
            return
        if name == "escape_pressed":
            self.escape_pressed.emit()
            return
        if name == "context_menu_requested":
            self.context_menu_requested.emit()
            return
        # Phase 2A dual-deck events -- not tied to the primary load()'s
        # token, since a secondary deck is a separate, parallel preload
        # (see video_dual_transition.py). dual_transition_complete is the
        # one exception: by the time it fires, the promoted deck IS the
        # primary the parent's token already refers to (see
        # video_subprocess.py's _on_dual_transition_finished).
        if name == "secondary_ready":
            if not self._accept_preload_event(event):
                return
            envelope = {k: v for k, v in event.items() if k != "event"}
            # Carries bounded seek-confirmation diagnostics when Smart Video
            # Transition Points' intro seek produced it (see
            # video_subprocess.py's GpuDualDeckVideoSubprocessController) --
            # recorded before emitting the signal so a diagnostics failure
            # can never suppress the real event.
            try:
                get_diagnostics().record(
                    "playback", "video_secondary_ready",
                    details=envelope, minimum_level="detailed",
                )
            except Exception:
                pass
            self.secondary_ready.emit(envelope)
            return
        if name == "secondary_failed":
            # A synchronous decline at commit time (e.g. "not_ready_at_
            # commit") carries the transition_id it belongs to -- if it
            # matches what this backend is currently watching, the commit
            # attempt is definitively over already, so both watchdog
            # stages must stand down rather than firing a spurious timeout
            # for something already resolved. This is independent of
            # preload-identity acceptance below: the transition itself
            # really did fail regardless of whose preload it was about.
            transition_id = event.get("transition_id")
            if transition_id is not None and transition_id == self._active_transition_id:
                self._cancel_transition_watchdogs()
            if not self._accept_preload_event(event):
                return
            self.secondary_failed.emit({k: v for k, v in event.items() if k != "event"})
            return
        if name == "commit_command_received":
            transition_id = event.get("transition_id")
            if transition_id == self._active_transition_id:
                self._discard_watchdog_timer("_active_commit_ack_timer")
                deadline_ms = self._transition_completion_deadline_ms
                if deadline_ms:
                    self._arm_completion_timer(transition_id, deadline_ms)
            try:
                get_diagnostics().record(
                    "playback", "video_transition_commit_command_received",
                    details={k: v for k, v in event.items() if k != "event"},
                    minimum_level="detailed",
                )
            except Exception:
                pass
            return
        if name == "commit_execution_started":
            # Diagnostic milestone only -- see module docstring's four-
            # milestone ordering. Deliberately unconditional (no transition_
            # id gate): it mutates no state and drives no public signal, so
            # a stale T1 arriving here has nothing to corrupt (matches
            # gpu_transition_checkpoint/deck_state_changed's own treatment
            # below for the identical reason).
            try:
                get_diagnostics().record(
                    "playback", "video_transition_commit_execution_started",
                    details={k: v for k, v in event.items() if k != "event"},
                    minimum_level="detailed",
                )
            except Exception:
                pass
            return
        if name == "secondary_preload_progress":
            if not self._accept_preload_event(event):
                return
            details = {k: v for k, v in event.items() if k != "event"}
            try:
                get_diagnostics().record(
                    "playback", "video_secondary_preload_progress",
                    details=details, minimum_level="detailed",
                )
            except Exception:
                pass
            self.secondary_preload_progress.emit(details)
            return
        if name in _PRELOAD_DIAGNOSTIC_EVENTS:
            # Lower-level preload sub-events with no public signal and no
            # state mutation of their own -- still identity-gated (rather
            # than logged unconditionally like the pure transition
            # diagnostics below) so a stale P1's loading/seek/held chatter
            # arriving during P2 is never recorded as if it were about the
            # currently active preload.
            if not self._accept_preload_event(event):
                return
            try:
                get_diagnostics().record(
                    "playback", f"video_{name}",
                    details={k: v for k, v in event.items() if k != "event"},
                    minimum_level="detailed",
                )
            except Exception:
                pass
            return
        if name in (
            "compositor_paint_timing", "dual_transition_committed",
            "gpu_transition_checkpoint", "deck_state_changed",
        ):
            # Pure transition-phase diagnostics -- no public signal, no
            # state mutation regardless of which transition_id they carry,
            # so a stale T1 checkpoint/deck-state-change arriving during T2
            # is harmless to log as-is (it already carries its own
            # transition_id/primary_index/secondary_index for later
            # analysis -- see video_subprocess.py's _capture_transition_
            # checkpoint).
            try:
                get_diagnostics().record(
                    "playback", f"video_{name}",
                    details={k: v for k, v in event.items() if k != "event"},
                    minimum_level="detailed",
                )
            except Exception:
                pass
            return
        if name in (
            "classic_audio_state", "audio_command_applied",
            "decoded_audio_evidence", "load_requested",
            "audio_device_rebound",
        ):
            # Classic-audio-silence investigation (2026-08-31 Codex audit,
            # sections 4-6) -- pure diagnostics, no public signal, no state
            # mutation, so a "stale" one (from a load this backend has
            # since moved on from) is still genuinely useful postmortem
            # data rather than noise to drop -- each event already carries
            # its own "token" for later correlation, same reasoning as the
            # GPU transition-phase diagnostics above. minimum_level=basic
            # per instruction (this round's acceptance diagnostics must be
            # visible without raising the app's configured diagnostics
            # level), not detailed like everything else in this method.
            try:
                get_diagnostics().record(
                    "playback", f"video_{name}",
                    details={k: v for k, v in event.items() if k != "event"},
                    minimum_level="basic",
                )
            except Exception:
                pass
            return
        # Every other event is tagged with the token of the load() it
        # belongs to -- a stale event from a load() this backend has since
        # moved on from (superseded by a newer load()/stop()) is dropped
        # rather than acted on.
        if event.get("token") != self._token:
            return
        if name == "dual_transition_complete":
            # Correctness hardening (2026-08-24): reject a missing or
            # mismatched transition_id *before* cancelling watchdogs,
            # changing active-transition state, logging it as the current
            # transition, or emitting the public completion signal at all
            # -- a T1 completion arriving while T2 is the active transition
            # must have absolutely no effect on T2, not even a signal
            # DualVideoTransitionEngine has to filter out itself (though it
            # independently re-validates too -- see its own transition_id
            # check in on_dual_transition_complete()).
            transition_id = event.get("transition_id")
            if transition_id is None or transition_id != self._active_transition_id:
                self._maybe_record_stale_transition_event(event, "dual_transition_complete")
                return
            # _authoritative_source_hash was already updated optimistically
            # in commit_dual_transition() (see its comment) -- this is just
            # standing the watchdog down now that real completion has
            # genuinely arrived.
            self._cancel_transition_watchdogs()
            self.dual_transition_complete.emit(transition_id)
            return
        if name == "started":
            self._is_playing = True
            self.started.emit()
        elif name == "paused":
            self._is_playing = False
            self.paused.emit()
        elif name == "stopped":
            self._is_playing = False
            self.stopped.emit()
        elif name == "position_changed":
            if not self._accept_timing_event(event):
                return
            self._current_position_ms = int(event.get("position_ms", 0))
            duration_ms = int(event.get("duration_ms", 0) or 0)
            if duration_ms > 0:
                self._current_duration_ms = duration_ms
            self.position_changed.emit(self._current_position_ms)
        elif name == "duration_changed":
            if not self._accept_timing_event(event):
                return
            self._current_duration_ms = int(event.get("duration_ms", 0))
            self.duration_changed.emit(self._current_duration_ms)
        elif name == "end_of_media":
            self.end_of_media.emit()
        elif name == "error":
            self.error.emit(
                event.get("category", ERROR_UNKNOWN), event.get("message", ""),
            )

    def _accept_timing_event(self, event: dict) -> bool:
        """True if a position_changed/duration_changed event's own carried
        source identity actually matches what this backend currently
        believes is authoritative -- False means it's a stale report from
        a deck that isn't (or isn't yet) the real current track, and must
        be dropped outright: never applied to _current_position_ms/
        _current_duration_ms, never re-emitted as position_changed/
        duration_changed (so it can't move the seekbar or feed deadline
        logic), just silently ignored (plus a bounded diagnostic).

        Real-device bug (2026-08-24): window.py's self.current_path flips
        to the incoming track immediately at GPU commit time, well before
        the subprocess's own deck swap actually happens in
        _on_transition_finished(). ``_authoritative_source_hash`` is
        updated to match that exact same optimistic moment (see
        commit_dual_transition()'s comment) precisely so this check can
        catch the failure mode that produced the bug: a still-not-yet-
        swapped primary deck's genuine position/duration report, arriving
        during the transition's brief animation window, no longer matches
        what the parent (and the UI) now consider current, so it's
        rejected here instead of sailing through and getting mislabelled
        with the promoted track's identity downstream in window.py.

        The classic (non-dual) single-deck controller's events never carry
        "source_hash" at all -- ``None`` here always means "no identity
        information to check," so this is a pure no-op for that backend,
        preserving its existing behaviour exactly."""
        source_hash = event.get("source_hash")
        if source_hash is None:
            return True
        if source_hash == self._authoritative_source_hash:
            return True
        # Bounded: only the first rejection for a given stale source is
        # recorded, not one per position tick (these fire several times a
        # second) -- reset whenever _authoritative_source_hash itself
        # changes (load()/a confirmed dual_transition_complete), so a
        # renewed mismatch after that point is reported again.
        if source_hash != self._last_rejected_stale_hash:
            self._last_rejected_stale_hash = source_hash
            try:
                get_diagnostics().record(
                    "playback", "video_timing_rejected_stale", severity="notice",
                    details={
                        "deck_index": event.get("deck_index"),
                        "primary_index": event.get("primary_index"),
                        "secondary_index": event.get("secondary_index"),
                        "transition_id": event.get("transition_id"),
                    },
                    minimum_level="detailed",
                )
            except Exception:
                pass
        return False

    def _accept_preload_event(self, event: dict) -> bool:
        """True if a secondary-deck event's own carried identity actually
        matches the preload attempt this backend currently believes is
        active -- False means it belongs to a superseded preload (the
        original attempt before a bounded retry, or an entirely different
        candidate after a manual Next/queue change) and must be ignored
        outright: never applied to any state, never re-emitted as a public
        signal. Checks, in order: a preload is currently active at all;
        the event's preload_id matches it exactly (ids are minted fresh
        per attempt in preload_secondary(), including every retry -- never
        reused, so this alone is already unambiguous); its source_hash
        matches too (belt-and-braces -- catches a hypothetical id-
        generation bug independently of the id check); and its deck_index
        is genuinely the physical secondary deck (a stray event tagged
        with the primary deck's index, which should never happen given
        video_subprocess.py's own dispatch, is rejected rather than
        trusted)."""
        handle = self._active_preload_handle
        if handle is None:
            self._maybe_record_stale_preload_event(event, "no_active_preload")
            return False
        if event.get("preload_id") != handle.preload_id:
            self._maybe_record_stale_preload_event(event, "preload_id_mismatch")
            return False
        if event.get("source_hash") != handle.source_hash:
            self._maybe_record_stale_preload_event(event, "source_hash_mismatch")
            return False
        deck_index = event.get("deck_index")
        secondary_index = event.get("secondary_index")
        if deck_index is None or secondary_index is None or deck_index != secondary_index:
            self._maybe_record_stale_preload_event(event, "deck_index_mismatch")
            return False
        return True

    def _maybe_record_stale_preload_event(self, event: dict, reason: str) -> None:
        # Bounded the same way _accept_timing_event's stale-hash diagnostic
        # is: one record per distinct (event type, id, reason) combination,
        # not one per occurrence -- a superseded preload can otherwise keep
        # emitting several rejected events in a row (loading/seek/held...).
        key = (event.get("event"), event.get("preload_id"), reason)
        if key == self._last_rejected_stale_preload_key:
            return
        self._last_rejected_stale_preload_key = key
        try:
            get_diagnostics().record(
                "playback", "video_preload_event_rejected_stale", severity="notice",
                details={
                    "child_event": event.get("event"),
                    "reason": reason,
                    "event_preload_id": event.get("preload_id"),
                    "active_preload_id": (
                        self._active_preload_handle.preload_id
                        if self._active_preload_handle is not None else None
                    ),
                    "deck_index": event.get("deck_index"),
                },
                minimum_level="detailed",
            )
        except Exception:
            pass

    def _maybe_record_stale_transition_event(self, event: dict, child_event_name: str) -> None:
        try:
            get_diagnostics().record(
                "playback", "video_transition_event_rejected_stale", severity="notice",
                details={
                    "child_event": child_event_name,
                    "event_transition_id": event.get("transition_id"),
                    "active_transition_id": self._active_transition_id,
                },
                minimum_level="detailed",
            )
        except Exception:
            pass

    # -- native window embedding ------------------------------------------------
    def _create_embedded_container(self):
        if self._win_id is None or self._embedded_container is not None or self._shutdown:
            return
        try:
            foreign_window = QtGui.QWindow.fromWinId(self._win_id)
        except Exception as ex:
            self.error.emit(
                ERROR_SUBPROCESS, f"Could not embed video window: fromWinId raised: {ex}",
            )
            return
        if foreign_window is None:
            self.error.emit(
                ERROR_SUBPROCESS,
                "Could not embed video window: fromWinId() returned no window",
            )
            return
        try:
            container = QtWidgets.QWidget.createWindowContainer(foreign_window)
        except Exception as ex:
            self.error.emit(
                ERROR_SUBPROCESS,
                f"Could not embed video window: createWindowContainer raised: {ex}",
            )
            return
        if container is None:
            self.error.emit(
                ERROR_SUBPROCESS,
                "Could not embed video window: createWindowContainer() returned no widget",
            )
            return

        # Replacing ``container.resizeEvent`` on an already-created Qt
        # widget instance is not a reliable virtual-method override.  In
        # particular, native resize events delivered by Qt/Windows can bypass
        # that Python instance attribute entirely.  An event filter is a real
        # part of Qt's event path, so every layout/fullscreen resize reaches us.
        container.installEventFilter(self)
        container.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self._embedded_container = container
        if self._pending_attach_target is not None:
            target, self._pending_attach_target = self._pending_attach_target, None
            self._do_attach(target)

    def eventFilter(self, watched, event):
        if (
            watched is self._embedded_container
            and event.type() in (
                QtCore.QEvent.Type.Resize,
                QtCore.QEvent.Type.Show,
                QtCore.QEvent.Type.PolishRequest,
            )
        ):
            self.sync_output_geometry()
        return super().eventFilter(watched, event)

    def _send_resize(self, size: QtCore.QSize, device_pixel_ratio: float = 1.0):
        # The parent's window manager doesn't reliably propagate a resize
        # of the container across the process boundary to the child's own
        # widget, which otherwise stays at its initial 640x360 -- explicit
        # beats implicit here.
        width, height = size.width(), size.height()
        if width > 0 and height > 0:
            self._send({
                "cmd": "resize",
                "width": width,
                "height": height,
                # QWidget sizes are device-independent pixels.  A window
                # owned by another process can have a different DPI context,
                # so the child needs the parent's ratio to preserve the same
                # physical client size on Windows display scaling.
                "device_pixel_ratio": max(0.1, float(device_pixel_ratio)),
            })

    def sync_output_geometry(self):
        """Push the embedded container's current client size to the child.

        Public so the main window can request another synchronization after a
        fullscreen/reparent layout has reached its final geometry.
        """
        container = self._embedded_container
        if container is None or self._shutdown:
            return
        try:
            dpr = container.devicePixelRatioF()
            size = container.contentsRect().size()
        except RuntimeError:
            # The host can destroy the native container while a deferred
            # geometry sync is pending (rapid close/restart/test teardown).
            # Treat that exactly like a detached output instead of letting an
            # exception escape a Qt timer slot, which makes PyQt abort.
            if self._embedded_container is container:
                self._embedded_container = None
            return
        except Exception:
            dpr = 1.0
            try:
                size = container.size()
            except Exception:
                return
        self._send_resize(size, dpr)

    def schedule_output_geometry_sync(self):
        # The first call covers an already-laid-out host; the deferred calls
        # cover Qt/Windows completing a reparent or fullscreen transition.
        self.sync_output_geometry()
        self._geometry_sync_timer.start(0)
        self._geometry_sync_late_timer.start(75)

    def attach_output(self, host_widget: Optional[QtWidgets.QWidget]):
        """host_widget must be a plain QWidget with a layout already set
        (see window.py's video_output_widget / party_mode.py's video_widget)
        -- the single persistent embedded video container is (re)parented
        into it. Safe to call before the child process is ready; the
        request is queued and applied once the container exists. Also
        remembered so a container recreated after a subprocess restart
        gets reattached to the same place automatically."""
        if self._shutdown:
            return
        self._last_attach_target = host_widget
        if self._embedded_container is None:
            self._pending_attach_target = host_widget
            return
        self._do_attach(host_widget)

    def _do_attach(self, host_widget: Optional[QtWidgets.QWidget]):
        container = self._embedded_container
        if container is None:
            return
        current_parent = container.parentWidget()
        if current_parent is not None:
            current_layout = current_parent.layout()
            if current_layout is not None:
                current_layout.removeWidget(container)
        container.setParent(None)
        if host_widget is not None and host_widget.layout() is not None:
            host_widget.layout().addWidget(container)
            host_widget.layout().activate()
            # Layout activation delivers a real Resize event through the
            # filter above; also synchronize once immediately for a host
            # whose geometry was already final before attachment.
            self.sync_output_geometry()

    # -- public API -------------------------------------------------------------
    def load(self, path: str, transport_url: Optional[str] = None) -> bool:
        """Load and start playing `path`. Returns False on an immediate,
        pre-flight failure (missing file), or if the command could not
        actually be delivered to a live child process (e.g. it just
        crashed) -- a restart is attempted for next time, but this specific
        call never claims a success that didn't happen.

        `transport_url`, Stage 3A: for a Plex video identity, the
        ephemeral (possibly token-bearing) HTTP(S) URL the child process
        should actually open -- `path` stays the stable plex://... logical
        identity used here for hashing/diagnostics/the IPC token, exactly
        as everywhere else in the app; it is never itself opened as a
        file. Local playback (transport_url=None) is completely
        unchanged: the existing os.path.isfile pre-flight and
        QUrl.fromLocalFile route in the child both still apply."""
        if self._shutdown:
            return False
        self._token += 1
        token = self._token
        self._current_position_ms = 0
        self._current_duration_ms = 0
        if transport_url:
            pass  # pre-flight existence is meaningless for a remote URL
        elif not path or not os.path.isfile(path):
            self.error.emit(ERROR_FILE_MISSING, f"Video file not found: {path}")
            return False
        identity_hash = get_diagnostics().path_details(path).get("path_hash")
        # A fresh classic load is immediately authoritative -- there is no
        # deck-swap race in single-deck mode, and in dual mode this is
        # exactly what a real _promote_dual_transition_track_ui-driven
        # activation should confirm too. Any transition this backend was
        # still tracking is necessarily stale the moment a new load lands.
        self._authoritative_source_hash = identity_hash
        self._last_rejected_stale_hash = object()
        self._cancel_transition_watchdogs()
        # A new primary supersedes any preload this backend was tracking
        # too -- whatever it was for belonged to the track that just
        # finished (or was abandoned via a manual Next while a preload was
        # still pending), and is stale the instant this new load lands.
        self._active_preload_handle = None
        payload = {
            "cmd": "load", "path": path, "token": token,
            "identity_hash": identity_hash,
        }
        if transport_url:
            # Private stdin/stdout IPC only, never argv -- see this
            # module's own docstring on the parent/child transport.
            payload["transport_url"] = transport_url
        return self._send(payload)

    def play(self):
        if not self._shutdown:
            self._send({"cmd": "play"})

    def pause(self):
        if not self._shutdown:
            self._send({"cmd": "pause"})

    def resume(self):
        if not self._shutdown:
            self._send({"cmd": "resume"})

    def stop(self):
        if not self._shutdown:
            self._token += 1
            self._is_playing = False
            self._cancel_transition_watchdogs()
            self._active_preload_handle = None
            self._send({"cmd": "stop"})

    def seek(self, position_ms: int):
        if not self._shutdown:
            self._send({"cmd": "seek", "position_ms": int(position_ms)})

    def position_ms(self) -> int:
        return 0 if self._shutdown else self._current_position_ms

    def duration_ms(self) -> int:
        return 0 if self._shutdown else self._current_duration_ms

    def is_playing(self) -> bool:
        return (not self._shutdown) and self._is_playing

    def set_volume(self, volume_0_100: float):
        if not self._shutdown:
            clamped = max(0.0, min(100.0, float(volume_0_100)))
            self._send({"cmd": "set_volume", "volume": clamped})

    def set_muted(self, muted: bool):
        if not self._shutdown:
            self._send({"cmd": "set_muted", "muted": bool(muted)})

    def query_audio_state(self, checkpoint: str, transition_id: Optional[int] = None) -> bool:
        """Fire-and-forget request for the child's *actual* primary-deck
        audio state (see audio_state_reported). `checkpoint`/`transition_id`
        are opaque to the child -- it only ever echoes them straight back
        on the "audio_state_report" event, so the caller can label the
        eventual (asynchronous) report without needing real request/
        response correlation for what is purely a diagnostic snapshot."""
        if self._shutdown:
            return False
        return self._send({
            "cmd": "query_audio_state", "checkpoint": checkpoint,
            "transition_id": transition_id,
        })

    # -- Phase 2A: experimental dual-video cross-dissolve ------------------
    def is_dual_mode(self) -> bool:
        """True for either dual compositor (currently only "gpu" is ever
        requested by window.py -- "cpu" remains permanently unavailable,
        see window.py's DUAL_VIDEO_TRANSITIONS_AVAILABLE). Use
        dual_compositor_mode() if the caller needs to know which one."""
        return self._dual_mode is not None

    def dual_compositor_mode(self) -> Optional[str]:
        return self._dual_mode

    def set_dual_mode(self, mode: Optional[str]):
        """Restarts the child process with the other mode, but only when
        the mode actually changed -- reuses the same crash-restart
        machinery as _perform_restart() rather than a bespoke path. Classic
        mode (mode=None) is the default and is unaffected unless the
        experimental preference was actually turned on at some point.
        ``mode`` is ``None`` (classic), ``"cpu"`` (permanently unavailable
        -- see window.py) or ``"gpu"``."""
        if mode is not None and mode not in ("cpu", "gpu"):
            raise ValueError(f"unknown dual compositor mode: {mode!r}")
        if self._shutdown or mode == self._dual_mode:
            return
        # Correctness hardening (2026-08-24): cancel/invalidate any
        # committed-transition watchdog and preload tracking *before*
        # replacing the child -- the process about to be torn down is the
        # only one that could ever have completed/failed either, so both
        # are unconditionally stale the instant a mode switch is requested.
        self._cancel_transition_watchdogs()
        self._active_preload_handle = None
        self._dual_mode = mode
        self._restart_attempts = 0
        old_process = self._process
        if old_process is not None:
            # The old process must be fully gone -- not just disconnected
            # -- before the replacement starts. Launching the new process
            # first and closing the old one after (the original design,
            # meant to avoid a blank-video gap during the switch) leaves a
            # window where two child instances of this same Qt-based exe
            # run concurrently under this parent -- confirmed via a real
            # Windows Error Reporting crash dump to fault natively inside
            # Qt6Core.dll 6.11.1.0 with STATUS_STACK_BUFFER_OVERRUN
            # (0xC0000409) at a consistent offset, reproduced even with
            # two plain classic-mode children and no compositor/QML/RHI
            # code involved at all -- so this is a Qt-level hazard, not
            # something specific to the dual-video feature. Accepting a
            # brief gap in the (rare, once-per-session) mode-switch case is
            # the safe tradeoff.
            self._unwire_process(old_process)
            # Cleared before tearing the old process down (not after) so
            # _is_stale_process_signal() correctly rejects a signal that
            # was already in flight from old_process at the moment it gets
            # killed below, even if it arrives before -- or despite --
            # _unwire_process()'s disconnect() actually taking effect.
            self._process = None
            self._stop_process_gracefully(old_process)
            try:
                old_process.deleteLater()
            except Exception:
                pass
        # The dead process's embedded window is gone with it -- without
        # this, _create_embedded_container() silently no-ops on the new
        # process's "ready" event because it still sees a (now stale)
        # non-None container (see _handle_process_crash(), which clears
        # the same state for the crash-restart case).
        if self._embedded_container is not None:
            try:
                self._embedded_container.setParent(None)
                self._embedded_container.deleteLater()
            except Exception:
                pass
            self._embedded_container = None
        self._win_id = None
        if self._last_attach_target is not None:
            self._pending_attach_target = self._last_attach_target
        self._launch_new_process(auto_start=True)

    def preload_secondary(self, path: str, *, start_position_ms: int = 0) -> bool:
        """No-op (returns False) unless running in dual mode -- callers
        (DualVideoTransitionEngine) treat False as "dual unavailable" and
        fall back to Phase 1. ``start_position_ms`` is Smart Video
        Transition Points' optional intro-skip offset (see
        video_dual_transition.py's _smart_intro_start_ms) -- omitted from
        the IPC command entirely when 0, so an unmodified child process/
        default call site behaves exactly as before this parameter
        existed."""
        if not self._dual_mode or self._shutdown:
            return False
        if not path or not os.path.isfile(path):
            return False
        # A fresh, immutable preload_id every call -- including a bounded
        # retry of the exact same logical target (see video_dual_
        # transition.py's _attempt_bounded_preload_retry): a retry is a new
        # preload attempt as far as identity is concerned, never a
        # continuation of the old one, so any straggler event from the
        # attempt it's replacing is unambiguously stale from this point on.
        self._next_preload_id += 1
        preload_id = self._next_preload_id
        identity_hash = get_diagnostics().path_details(path).get("path_hash")
        self._active_preload_handle = VideoPreloadHandle(
            preload_id=preload_id, source_hash=identity_hash,
        )
        self._pending_secondary_hash = identity_hash
        command = {
            "cmd": "preload_secondary", "path": path,
            "identity_hash": identity_hash, "preload_id": preload_id,
        }
        if start_position_ms:
            command["start_position_ms"] = int(start_position_ms)
        sent = self._send(command)
        if not sent:
            self._active_preload_handle = None
        return sent

    @property
    def active_preload_id(self) -> Optional[int]:
        """Read by DualVideoTransitionEngine immediately after a successful
        preload_secondary() call to capture the id it just minted (see
        video_dual_transition.py's DualDeckController.preload_id) --
        preload_secondary() itself keeps its existing bool return so
        nothing else calling it needs to change."""
        handle = self._active_preload_handle
        return handle.preload_id if handle is not None else None

    @property
    def active_preload_source_hash(self) -> Optional[str]:
        handle = self._active_preload_handle
        return handle.source_hash if handle is not None else None

    @property
    def active_transition_id(self) -> Optional[int]:
        """Read by DualVideoTransitionEngine immediately after a successful
        commit_dual_transition() call to capture the id it just minted
        (see video_dual_transition.py's DualDeckController.transition_id)
        -- commit_dual_transition() itself keeps its existing bool return."""
        return self._active_transition_id

    def cancel_secondary(self):
        if self._dual_mode and not self._shutdown:
            self._active_preload_handle = None
            self._send({"cmd": "cancel_secondary"})

    def debug_set_seek_delay_ms(self, delay_ms: int) -> bool:
        """Test-only: triggers GpuDualDeckVideoSubprocessController's
        debug_set_seek_delay_ms IPC command -- used solely by the
        zero-start-drift regression test to deterministically simulate a
        secondary that takes a while to reach BUFFERED and has real time
        to drift before the corrective seek/confirm sequence starts. No
        production code path calls this."""
        if not self._dual_mode or self._shutdown:
            return False
        return self._send({"cmd": "debug_set_seek_delay_ms", "delay_ms": int(delay_ms)})

    def commit_dual_transition(
        self, duration_ms: int, transition_type: str = "Cross Dissolve",
        seed: float = 0.0, *,
        audio_crossfade_enabled: bool = False,
        audio_crossfade_curve: str = "Equal Power",
    ) -> bool:
        if not self._dual_mode or self._shutdown:
            return False
        if self._active_transition_id is not None:
            # Defense-in-depth: DualVideoTransitionEngine.try_commit()
            # already refuses a second commit while the controller is past
            # SECONDARY_READY (see video_dual_transition.py's
            # test_repeated_commit_attempts_do_not_double_advance), so this
            # should never actually be reached in practice -- but this
            # backend must not itself send a second "commit_dual_
            # transition" IPC command while one is still outstanding
            # regardless of what any caller's own state machine believes.
            return False
        self._next_transition_id += 1
        transition_id = self._next_transition_id
        sent = self._send({
            "cmd": "commit_dual_transition", "transition_id": transition_id,
            "duration_ms": int(duration_ms),
            "transition_type": str(transition_type), "seed": float(seed),
            "audio_crossfade_enabled": bool(audio_crossfade_enabled),
            "audio_crossfade_curve": str(audio_crossfade_curve),
        })
        if not sent:
            return False
        outgoing_hash = self._authoritative_source_hash
        # Stage A starts now (see module-level constants); stopped once
        # commit_command_received arrives for this same transition_id, at
        # which point Stage B starts. Both cancelled the instant
        # dual_transition_complete confirms real completion, or by any of
        # load()/stop()/shutdown() invalidating the whole thing outright.
        self._active_transition_id = transition_id
        self._transition_commit_monotonic = time.monotonic()
        self._transition_completion_deadline_ms = (
            int(duration_ms) + _TRANSITION_COMPLETION_GRACE_MS
        )
        # Optimistic, deliberately -- matches window.py's own
        # _promote_dual_transition_track_ui, which flips self.current_path
        # to the incoming track immediately at this same moment, well
        # before the subprocess's real deck swap happens in
        # _on_transition_finished(). If this stayed pointed at the
        # outgoing track until confirmed completion instead, every
        # position/duration report the still-not-yet-swapped primary deck
        # sends during the transition's ~1s animation would keep matching
        # it and sail straight through to window.py -- exactly reproducing
        # the real-device bug (ABC's duration/position reported under
        # Goody's already-promoted path) this whole mechanism exists to
        # prevent. Updating it here means any such report is correctly
        # rejected as stale for the rest of the transition, and accepted
        # again the instant the real swap actually happens and the
        # primary deck's own reports start carrying this same hash.
        self._authoritative_source_hash = self._pending_secondary_hash
        self._last_rejected_stale_hash = object()
        # Real-device bug (2026-08-28, "Baby Baby"): _accept_timing_event
        # above only gates *events* -- it does nothing for a caller that
        # reads position_ms()/duration_ms() as bare cached getters instead
        # (window.py's _check_playback_health() and _maybe_prepare_mixed_
        # transition_from_video() both do exactly this, with no signal
        # involved at all). Left unset, those getters kept returning the
        # *outgoing* deck's last genuinely-accepted values -- e.g. a video
        # 1s from its own real end -- for the entire optimistic window
        # above, now silently mislabelled as belonging to the just-
        # promoted incoming track. That is exactly how a video which had
        # only just started got treated as almost over: near-end fired,
        # and a video->audio mixed transition was requested a few seconds
        # into playback. `load()` already resets these two fields for
        # this exact reason (a fresh load has no valid timing yet either)
        # -- this mirrors that, for the same reason, at the other moment
        # _authoritative_source_hash changes. Every caller of position_ms()/
        # duration_ms() already treats duration<=0 as "not yet available"
        # (the established sentinel throughout this codebase), so this
        # alone makes the ambiguous window safe for all of them without
        # changing any of their own logic.
        self._current_position_ms = 0
        self._current_duration_ms = 0
        try:
            get_diagnostics().record(
                "playback", "video_transition_commit_sent",
                details={
                    "transition_id": transition_id,
                    "primary_source_hash": outgoing_hash,
                    "secondary_source_hash": self._pending_secondary_hash,
                    "duration_ms": int(duration_ms),
                },
                minimum_level="detailed",
            )
        except Exception:
            pass
        self._arm_commit_ack_timer(transition_id)
        return True

    def pause_dual_transition(self):
        if self._dual_mode and not self._shutdown:
            self._send({"cmd": "pause_dual_transition"})

    def resume_dual_transition(self):
        if self._dual_mode and not self._shutdown:
            self._send({"cmd": "resume_dual_transition"})

    def _stop_process_gracefully(self, process):
        """Asks a child process to exit cleanly, closes our end of its
        stdin so its reader thread sees EOF even if the shutdown command
        itself is somehow lost, then forces it if it doesn't exit on its
        own within a bounded wait. Shared by shutdown() (full backend
        teardown) and set_dual_mode() (replacing the child with one
        running the other mode) -- both need the process fully gone,
        not just asked nicely, before moving on."""
        import time
        started = time.monotonic()
        try:
            process.write((json.dumps({"cmd": "shutdown"}) + "\n").encode("utf-8"))
            process.waitForBytesWritten(500)
        except Exception:
            pass
        try:
            process.closeWriteChannel()
        except Exception:
            pass
        try:
            still_running = (
                process.state() != QtCore.QProcess.ProcessState.NotRunning
            )
        except Exception:
            still_running = True
        needed_kill = False
        if still_running and not process.waitForFinished(2000):
            needed_kill = True
            try:
                process.kill()
                process.waitForFinished(1000)
            except Exception:
                pass
        # Diagnostic-only (see set_dual_mode()'s investigation): confirms
        # whether the old process actually exits on its own within the
        # graceful window, or whether the kill() fallback is what's really
        # ending it -- and how long that takes, since a live mode switch
        # blocks the GUI thread for however long this call runs.
        try:
            get_diagnostics().record(
                "playback", "video_subprocess_stop_gracefully_result",
                details={
                    "needed_kill": needed_kill,
                    "duration_ms": (time.monotonic() - started) * 1000.0,
                },
                minimum_level="basic",
            )
        except Exception:
            pass

    def shutdown(self):
        """Invalidate the generation token so no late event acts on
        anything, stop the child process (see _stop_process_gracefully()),
        and detach/release the embedded window. Safe to call more than
        once."""
        if self._shutdown:
            return
        self._shutdown = True
        self._token += 1
        self._geometry_sync_timer.stop()
        self._geometry_sync_late_timer.stop()
        self._cancel_transition_watchdogs()
        self._active_preload_handle = None
        if self._process is not None:
            self._stop_process_gracefully(self._process)
        if self._embedded_container is not None:
            try:
                self._embedded_container.setParent(None)
                self._embedded_container.deleteLater()
            except Exception:
                pass
            self._embedded_container = None


class GpuCompositorProbe(QtCore.QObject):
    """One-shot check of whether the GPU dual-video compositor
    (video_subprocess.py's GpuDualDeckVideoSubprocessController) can
    actually work on this machine, without touching real playback.

    Launches a throwaway child process (`--dual-deck-gpu --probe-only`)
    that loads the real GPU compositor QML scene, checks the Qt Quick scene
    graph's actual RHI backend, reports exactly one
    ``gpu_compositor_available``/``gpu_compositor_unavailable`` event, and
    exits -- never announces "ready", never embeds a window, never loads a
    video. Meant to be created once per application session and discarded;
    window.py caches the result and does not create a second probe (see
    that module's ``_start_gpu_capability_probe``) -- launching one of
    these mid-playback is not this class's job to prevent, but nothing in
    the app does that.
    """

    finished = QtCore.pyqtSignal(bool, str)  # available, reason ("" if available)

    _TIMEOUT_MS = 8000

    def __init__(
        self, parent: Optional[QtCore.QObject] = None, *,
        process_factory: Optional[Callable[[], QtCore.QProcess]] = None,
    ):
        super().__init__(parent)
        self._done = False
        self._process_factory = process_factory or self._default_process_factory
        self._process: Optional[QtCore.QProcess] = None
        self._timeout_timer = QtCore.QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.timeout.connect(lambda: self._complete(False, "probe_timeout"))

    def _default_process_factory(self) -> QtCore.QProcess:
        process = QtCore.QProcess(self)
        program, args = _subprocess_launch_command(dual_mode="gpu", probe_only=True)
        process.setProgram(program)
        process.setArguments(args)
        return process

    def start(self):
        if self._done:
            return
        self._process = self._process_factory()
        self._process.readyReadStandardOutput.connect(self._on_stdout_ready)
        self._process.finished.connect(self._on_process_finished)
        self._process.errorOccurred.connect(self._on_process_error)
        self._process.start()
        self._timeout_timer.start(self._TIMEOUT_MS)

    def _on_stdout_ready(self):
        while self._process is not None and self._process.canReadLine():
            raw = bytes(self._process.readLine()).decode("utf-8", errors="replace").strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except Exception:
                continue
            name = event.get("event")
            if name == "gpu_compositor_available":
                self._complete(True, "")
            elif name == "gpu_compositor_unavailable":
                self._complete(False, str(event.get("reason", "")))

    def _on_process_finished(self, *_args):
        if not self._done:
            self._complete(False, "process_exited_without_result")

    def _on_process_error(self, *_args):
        if not self._done:
            self._complete(False, "process_error")

    def _complete(self, available: bool, reason: str):
        if self._done:
            return
        self._done = True
        self._timeout_timer.stop()
        process, self._process = self._process, None
        if process is not None:
            try:
                if process.state() != QtCore.QProcess.ProcessState.NotRunning:
                    process.kill()
            except Exception:
                pass
            try:
                process.deleteLater()
            except Exception:
                pass
        self.finished.emit(available, reason)

    def cancel(self):
        """Application shutdown, not a normal completion (v1.0.66
        worker-lifetime hardening) -- kills the probe subprocess and marks
        this probe done without emitting ``finished``, since nothing should
        still be listening for a capability result once the window is
        closing. Safe to call after the probe has already completed on its
        own (the ``_done`` guard makes it a no-op)."""
        if self._done:
            return
        self._done = True
        self._timeout_timer.stop()
        process, self._process = self._process, None
        if process is not None:
            try:
                if process.state() != QtCore.QProcess.ProcessState.NotRunning:
                    process.kill()
            except Exception:
                pass
            try:
                process.deleteLater()
            except Exception:
                pass
