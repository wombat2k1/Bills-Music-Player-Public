"""Phase 6 -- playback failure & recovery authority.

A failure, fallback or recovery must not corrupt Up Next, resurrect or tear
down playback it does not own, or retry forever.

Defects:
1. A built-in crossfade whose incoming load failed made the failed track the
   current track (_fail_pending_crossfade activated it) and relied on
   recovery to deal with the rest. When recovery could not run (disabled by
   the user, or already busy) the outgoing track kept playing under the failed
   track's name, and nothing stopped automatic advancement from choosing the
   same broken entry again on the next near-end tick -- a retry loop.
2. The same failure arriving while paused ran recovery at once: it tore down
   the paused track and switched the current track during the Pause.
3. A VLC crossfade whose incoming player failed to start, with no recovery,
   fell through to _play_path_direct's synchronous commit: the entry that
   never played was marked played.

The tests drive the real _tick near-end trigger, _next_track,
_play_path_direct, the built-in crossfade engine and its failure path, the
VLC crossfade start, _begin_playback_recovery / _try_recovery_backend,
pause(), stop_playback and the queue claim / PlaybackAttempt machinery, with
inert recording players whose load of a "bad" file fails.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import billsmusic.window as window_module
from billsmusic.playback_attempt import PlaybackAttemptState

import test_pause_freezes_progression as f2
import test_pause_native_transport as p42
import test_playback_transition_authority as p5


class _CorruptAwarePlayer(p5._StoppablePlayer):
    """A built-in player that cannot decode any file named bad*."""

    def load(self, path):
        if os.path.basename(str(path)).startswith("bad"):
            raise RuntimeError("unsupported or corrupt audio")
        super().load(path)


def _builtin_window(monkeypatch, tmp_path, *, recovery, names=("bad.mp3", "c.mp3")):
    window = p5._crossfade_queue_window(monkeypatch, tmp_path, names=names)
    for player in (window.bass_player, window.bass_inactive_player):
        player.__class__ = _CorruptAwarePlayer
    window.auto_playback_recovery = recovery
    window.allow_backend_fallback = True
    window._backend_quarantined_until = {}
    window._backend_failure_times = {}
    window._lyric_idx = None
    f2._bind(
        window, "_begin_playback_recovery", "_try_recovery_backend", "_available_recovery_backends",
        "_record_playback_backend_failure", "_configured_preferred_backend", "_playback_health_snapshot",
        "_current_backend_name", "_fail_pending_crossfade", "_arm_playback_watchdog",
        "_cancel_playback_watchdog",
    )
    return window


def _fail_crossfade_load(window):
    """The prepare worker reports the incoming file as undecodable."""
    worker = window._crossfade_load_worker
    worker.failed.emit(worker.token, window.pending_builtin_crossfade_path, "unsupported or corrupt audio")


def _name(path):
    return os.path.basename(str(path)) if path else None


def _recovery_attempts(window):
    return [e for e in window.diagnostics.events if e[1] == "playback_attempt_started"
            and str(e[2].get("reason", "")).startswith("recovery_")]


# -- 1. built-in crossfade failure without recovery ----------------------------------

def test_a_failed_crossfade_without_recovery_keeps_the_outgoing_track_current_and_skips_the_bad_entry(monkeypatch, tmp_path):
    window = _builtin_window(monkeypatch, tmp_path, recovery=False)
    outgoing = window.simple_player
    p5._reach_crossfade_window(window)  # near-end: crossfade to bad.mp3 dispatched
    bad_attempt = window._current_playback_attempt

    _fail_crossfade_load(window)

    assert _name(window.current_path) == "a.mp3"  # the playing track is still the current one
    assert window.activated == []
    assert outgoing.playing is True and window.prebuffer_active is False
    assert bad_attempt.state is PlaybackAttemptState.FAILED
    assert window._queue_entry_claims == {} and window.commits == []

    for _ in range(5):
        window._tick()  # still near the end: automatic advancement passes over bad.mp3

    assert window.dispatches == ["bad.mp3", "c.mp3"]  # one failure, then the next entry
    assert window.pending_builtin_crossfade_path.endswith("c.mp3")


def test_explicit_next_may_still_retry_a_crossfade_entry_that_failed(monkeypatch, tmp_path):
    window = _builtin_window(monkeypatch, tmp_path, recovery=False)
    p5._reach_crossfade_window(window)
    _fail_crossfade_load(window)

    window.next_track()  # an explicit request is not bound by automatic suppression

    assert window.dispatches == ["bad.mp3", "bad.mp3"]


# -- 2. failure while paused ------------------------------------------------------------

def test_a_crossfade_failure_while_paused_is_held_until_resume(monkeypatch, tmp_path):
    window = _builtin_window(monkeypatch, tmp_path, recovery=True)
    outgoing = window.simple_player
    p5._reach_crossfade_window(window)
    window.pause()

    _fail_crossfade_load(window)

    assert _name(window.current_path) == "a.mp3" and window.activated == []
    assert outgoing._paused is True and outgoing.loaded_path.endswith("a.mp3")  # not torn down
    assert _recovery_attempts(window) == []
    assert window._playback_intentionally_paused is True and window.commits == []

    window.pause()  # Resume: the failure is handled now, once

    assert len(_recovery_attempts(window)) == 1
    assert window._current_playback_attempt.state is PlaybackAttemptState.FAILED
    assert window.commits == [] and window._queue_entry_claims == {}
    for _ in range(5):
        window._tick()
    assert len(_recovery_attempts(window)) == 1 and window.dispatches == ["bad.mp3"]


def test_stop_discards_a_crossfade_failure_held_by_pause(monkeypatch, tmp_path):
    window = _builtin_window(monkeypatch, tmp_path, recovery=True)
    p5._reach_crossfade_window(window)
    window.pause()
    _fail_crossfade_load(window)

    window.stop_playback()
    window.simple_player.play()  # later playback on the same player
    window._playback_expected = True
    window.pause()
    window.pause()

    assert _recovery_attempts(window) == []
    assert window.activated == [] and window.commits == [] and window._queue_entry_claims == {}


# -- 3. VLC crossfade start failure --------------------------------------------------------

def test_a_vlc_crossfade_whose_incoming_track_never_starts_is_not_committed(monkeypatch, tmp_path):
    window, active, inactive = p42._vlc_window(monkeypatch, tmp_path, names=("bad.mp3", "c.mp3"))
    real_play_on_player = window._play_on_player

    def play_on_player(player, path, volume_scale):
        if os.path.basename(str(path)).startswith("bad"):
            return False  # libvlc could not open it
        return real_play_on_player(player, path, volume_scale)

    window._play_on_player = play_on_player
    row = [os.path.basename(p) for p in window.queue].index("bad.mp3")
    token, owner = window_module._claim_queue_selection_for(window, row, reason="test")

    started = window._play_path_direct(window.queue[row], crossfade=True, queue_entry_token=token,
                                       queue_selection_owner=owner)

    attempt = window._current_playback_attempt
    assert started is False
    assert window.queue_played[row] is False  # never played, never committed
    assert token not in window._queue_entry_claims
    assert attempt.state is PlaybackAttemptState.FAILED
    assert inactive.playing is False and window.prebuffer_active is False


# -- guards ----------------------------------------------------------------------------------

def test_a_failed_crossfade_with_recovery_has_one_outcome_and_no_retry_loop(monkeypatch, tmp_path):
    window = _builtin_window(monkeypatch, tmp_path, recovery=True)
    p5._reach_crossfade_window(window)

    _fail_crossfade_load(window)
    for _ in range(10):
        window._tick()

    assert len(_recovery_attempts(window)) == 1
    assert window._current_playback_attempt.state is PlaybackAttemptState.FAILED
    assert window.dispatches == ["bad.mp3"]
    assert window.commits == [] and window._queue_entry_claims == {}
    assert window._playback_expected is False


def test_a_stale_crossfade_failure_cannot_disturb_a_newer_selection(monkeypatch, tmp_path):
    window = _builtin_window(monkeypatch, tmp_path, recovery=True)
    p5._reach_crossfade_window(window)
    worker = window._crossfade_load_worker
    bad_path = window.pending_builtin_crossfade_path
    row = [os.path.basename(p) for p in window.queue].index("c.mp3")
    token, owner = window_module._claim_queue_selection_for(window, row, reason="test")
    assert window._play_path_direct(window.queue[row], queue_entry_token=token, queue_selection_owner=owner)
    c_attempt = window._current_playback_attempt

    worker.failed.emit(worker.token, bad_path, "late failure")

    assert window._current_playback_attempt is c_attempt and c_attempt.state is PlaybackAttemptState.PLAYING
    assert _recovery_attempts(window) == []
    assert _name(window.current_path) == "c.mp3" and window.simple_player.playing is True


def test_bad_then_good_direct_plays_fail_once_then_play_and_commit(monkeypatch, tmp_path):
    window = _builtin_window(monkeypatch, tmp_path, recovery=True, names=("bad.mp3", "c.mp3"))
    window.track_transition_mode = "normal"

    def select(name):
        row = [os.path.basename(p) for p in window.queue].index(name)
        token, owner = window_module._claim_queue_selection_for(window, row, reason="test")
        return window._play_path_direct(window.queue[row], queue_entry_token=token, queue_selection_owner=owner)

    assert select("bad.mp3") is False
    assert len(_recovery_attempts(window)) == 1 and window._queue_entry_claims == {}
    for _ in range(5):
        window._tick()
    assert len(_recovery_attempts(window)) == 1  # the failure did not restart itself

    assert select("c.mp3") is True
    assert window.commits == ["c.mp3"] and window.simple_player.playing is True


class _PlayFailsPlayer(_CorruptAwarePlayer):
    """Loads fine, but its output cannot be started."""

    def play(self):
        if self.loaded_path and os.path.basename(str(self.loaded_path)).startswith("stall"):
            raise RuntimeError("device refused to start")
        super().play()


def test_an_incoming_track_that_cannot_start_is_not_current_or_committed(monkeypatch, tmp_path):
    window = _builtin_window(monkeypatch, tmp_path, recovery=False, names=("stall.mp3", "c.mp3"))
    for player in (window.bass_player, window.bass_inactive_player):
        player.__class__ = _PlayFailsPlayer
    outgoing = window.simple_player
    p5._reach_crossfade_window(window)
    attempt = window._current_playback_attempt
    worker = window._crossfade_load_worker
    path = window.pending_builtin_crossfade_path

    worker.prepared.emit(worker.token, path, f2._FakePreparedCandidate(path))  # prepared, but play() fails

    assert _name(window.current_path) == "a.mp3" and window.activated == []
    assert outgoing.playing is True and window.fade_active is False and window.prebuffer_active is False
    assert attempt.state is PlaybackAttemptState.FAILED
    assert window.commits == [] and window._queue_entry_claims == {}
    for _ in range(5):
        window._tick()
    assert window.dispatches == ["stall.mp3", "c.mp3"]


def test_an_incoming_track_that_cannot_start_when_a_held_fade_begins_is_not_current_or_committed(monkeypatch, tmp_path):
    window = _builtin_window(monkeypatch, tmp_path, recovery=False, names=("stall.mp3", "c.mp3"))
    for player in (window.bass_player, window.bass_inactive_player):
        player.__class__ = _PlayFailsPlayer
    outgoing = window.simple_player
    p5._reach_crossfade_window(window)
    attempt = window._current_playback_attempt
    window.pause()
    worker = window._crossfade_load_worker
    path = window.pending_builtin_crossfade_path
    worker.prepared.emit(worker.token, path, f2._FakePreparedCandidate(path))  # held: not started while paused

    window.pause()  # Resume: the held fade begins, and starting the incoming track fails

    assert _name(window.current_path) == "a.mp3" and window.activated == []
    assert outgoing.playing is True and window.fade_active is False and window.prebuffer_active is False
    assert attempt.state is PlaybackAttemptState.FAILED
    assert window.commits == [] and window._queue_entry_claims == {}
    for _ in range(5):
        window._tick()
    assert window.dispatches == ["stall.mp3", "c.mp3"]
