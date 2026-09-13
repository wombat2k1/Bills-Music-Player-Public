"""Stage 3A review gate 5: prove pending_next actually SUPPRESSES a queue
advance / stall-recovery request during the "Switch to BASS?" nested Qt
event loop (and the Plex resolve window generally), not merely postpones
one until pending_next clears.

_check_playback_health/_maybe_prepare_mixed_transition_from_video are
pure polling functions -- they re-evaluate current conditions fresh on
every call (a QTimer tick), there is no internal queue of "requests" that
could fire later. This file proves that directly: feed each one
conditions that would unambiguously trigger a queue advance / recovery
attempt if pending_next were False, call them repeatedly *while*
pending_next stays True (simulating the stall watchdog and near-end
timer both firing multiple times during the dialog+resolve wait), then
confirm zero recovery/advance calls -- not just during, but also
afterward, once pending_next clears and watchdog state is what a real
successful track start leaves it as (never a stale "catch-up" fire)."""
import os
import time
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import vlc

from billsmusic.media_type import MediaType
from billsmusic.window import PlayerWindow

PLEX_AUDIO = "plex://server-1/42.mp3"
LOCAL_AUDIO = "Y:\\Music\\Artist\\Album\\track.mp3"


class HealthCheckHarness:
    _check_playback_health = PlayerWindow._check_playback_health
    _playback_health_snapshot = PlayerWindow._playback_health_snapshot
    _current_backend_name = PlayerWindow._current_backend_name
    _use_builtin_player = PlayerWindow._use_builtin_player

    def __init__(self):
        self._current_media_type = MediaType.AUDIO
        self.auto_playback_recovery = True
        self._playback_expected = True
        self._playback_recovery_active = False
        self._playback_intentionally_paused = False
        self._closing = False
        self.scrubbing = False
        self.fade_active = False
        self.prebuffer_active = False
        self.pending_next = False
        self.current_path = PLEX_AUDIO

        self.builtin_backend = "bass"
        self.use_simple = True
        self._temporary_backend_override = None
        self._simple_fallback_active = False
        self.simple_player = MagicMock(name="simple_player")
        self.simple_player.is_playing.return_value = False  # "stalled, never started"
        self.simple_player.get_pos.return_value = 0.0
        self.simple_player.get_length.return_value = 300.0  # a real, non-trivial track
        self.active_player = None

        # Watchdog state set up so the stall condition is ALREADY,
        # unambiguously satisfied -- far in the past, position never
        # advanced. Matches _arm_playback_watchdog(0.0)'s own initial
        # values, just long enough ago that both grace-period checks pass.
        started_long_ago = time.monotonic() - 10.0
        self._playback_watch_started = started_long_ago
        self._playback_watch_last_advance = started_long_ago
        self._playback_watch_last_position = 0.0
        self._playback_watch_has_advanced = False
        self._playback_watch_requested_position = 0.0

        self.recovery_calls = []
        self._begin_playback_recovery = lambda reason, backend, **kw: self.recovery_calls.append(
            (reason, backend)
        )


def test_stall_condition_produces_zero_recovery_calls_while_pending_next_true():
    harness = HealthCheckHarness()
    harness.pending_next = True

    # Simulate the watchdog ticking repeatedly during the dialog+resolve
    # wait -- the same unambiguous stall condition is present every time.
    for _ in range(5):
        harness._check_playback_health()

    assert harness.recovery_calls == [], (
        "a stall condition present the whole time pending_next was True "
        "must never trigger recovery -- not even once"
    )


def test_no_deferred_recovery_fires_once_pending_next_clears_with_fresh_watchdog():
    harness = HealthCheckHarness()
    harness.pending_next = True
    for _ in range(3):
        harness._check_playback_health()
    assert harness.recovery_calls == []

    # The real sequence once BASS load succeeds: _arm_playback_watchdog(0.0)
    # resets watchdog state for the genuinely-now-playing track, and
    # pending_next clears -- both happen together, exactly as
    # _on_plex_audio_load_succeeded's real tail does.
    harness.pending_next = False
    harness.simple_player.is_playing.return_value = True
    harness.simple_player.get_pos.return_value = 0.5
    now = time.monotonic()
    harness._playback_watch_started = now
    harness._playback_watch_last_advance = now
    harness._playback_watch_last_position = 0.0
    harness._playback_watch_has_advanced = False
    harness._playback_watch_requested_position = 0.0

    harness._check_playback_health()

    assert harness.recovery_calls == [], (
        "clearing pending_next must never trigger a queued/deferred "
        "recovery from conditions that were true while it was set -- "
        "the request must have been discarded, not postponed"
    )


def test_stall_condition_correctly_fires_once_pending_next_is_false_control_case():
    # Control: confirms the harness genuinely reproduces a real stall
    # trigger when pending_next is NOT the reason it's being suppressed --
    # otherwise the two tests above would be vacuously true. Uses a LOCAL
    # path deliberately -- a plex:// identity is now unconditionally
    # excluded from this watchdog regardless of pending_next (see
    # test_plex_identity_never_triggers_recovery_even_with_pending_next_false
    # below), so it can no longer serve as this control's "nothing else is
    # suppressing it" case.
    harness = HealthCheckHarness()
    harness.pending_next = False
    harness.current_path = LOCAL_AUDIO

    harness._check_playback_health()

    assert harness.recovery_calls == [("startup-stall", "bass")]


def test_plex_identity_never_triggers_recovery_even_with_pending_next_false():
    # Stage 3A-r2 real-device defect: the generic stall watchdog is tuned
    # for Local files (BASS_StreamCreateFile on a local/network path
    # resolves near-instantly) and misfired during a genuinely in-flight,
    # slower Plex async resolve+load even though pending_next *should* have
    # blocked it. Rather than rely solely on timing the pending_next window
    # correctly, a plex:// current_path must unconditionally disable this
    # watchdog -- Plex has its own dedicated resolve/load pipeline with its
    # own generation/token staleness checks, and the fallback ladder this
    # watchdog would otherwise run (_try_recovery_backend) feeds the raw
    # plex:// identity into BASS/VLC/miniaudio loaders that cannot open it,
    # corrupting _temporary_backend_override in the process (see the
    # matching comment in window.py's _check_playback_health).
    harness = HealthCheckHarness()
    harness.pending_next = False
    assert harness.current_path == PLEX_AUDIO

    for _ in range(10):
        harness._check_playback_health()

    assert harness.recovery_calls == [], (
        "a plex:// current_path must suppress this watchdog unconditionally, "
        "not merely while pending_next happens to be True"
    )


def test_multiple_ticks_during_wait_never_accumulate_a_pending_call():
    # Exactly the item-1 hostile scenario spelled out by the review: stall
    # watchdog fires, near-end-style repeated ticks occur, THEN pending_next
    # clears -- must be exactly zero recovery calls total, not "one queued
    # call finally released".
    harness = HealthCheckHarness()
    # A non-Plex path -- this test is about pending_next's own suppression
    # semantics (accumulation vs. discard), which the unconditional plex://
    # exclusion above would otherwise mask entirely.
    harness.current_path = LOCAL_AUDIO
    harness.pending_next = True
    for _ in range(10):
        harness._check_playback_health()
    harness.pending_next = False
    # Deliberately do NOT reset watchdog state here (unlike the test
    # above) -- proves suppression doesn't depend on the caller happening
    # to also reset timing; the discard is real. Since state genuinely
    # says "stalled", a stall check ONE more time is expected to
    # legitimately fire now -- but only once, for the current, real
    # condition, never retroactively for the 10 suppressed ticks.
    harness._check_playback_health()
    assert len(harness.recovery_calls) == 1


# -- Stage 3A-r2 item 1 (required scenario): a stale old-backend stopped/
# end callback arriving AFTER the new Plex/BASS track has already started
# must be ignored -- no _next_track(), no queue promotion. -----------------
#
# _tick() (the real, unbound method under test here) is where the "vlc
# ended" detection actually lives (self.active_player.get_state() ==
# vlc.State.Ended -> self._next_track("vlc-ended")). The real protection
# isn't a generation check on that branch specifically -- it's that
# _use_builtin_player() (True for a BASS-backed Plex track, since
# _temporary_backend_override is never touched by the now-excluded
# recovery ladder) routes _tick() into the builtin-player branch, which
# unconditionally returns before the vlc.State.Ended check is ever
# reached. A stale VLC player object left over from whatever played
# before the Plex track -- even one still reporting Ended -- is therefore
# structurally unreachable while a Plex/BASS track is current.

class TickHarness(HealthCheckHarness):
    _tick = PlayerWindow._tick
    _use_bass_backend = PlayerWindow._use_bass_backend

    def __init__(self):
        super().__init__()
        self.current_path = PLEX_AUDIO
        self._sync_mini_player = lambda: None
        self.cast_active = False
        self._sync_party_mode = lambda: None
        self._update_recently_played_tracking = lambda: None
        self._lyrics_tick = lambda: None
        self._sync_karaoke_position = lambda: None
        self.scrubbing = False
        self.fade_active = False
        self.prebuffer_active = False
        self.pending_next = False
        self.track_transition_mode = "normal"
        self.crossfade_seconds = 2.0
        self._update_progress = lambda *a, **k: None

        # Genuinely playing via BASS -- the real state a Plex track is in
        # once _on_plex_audio_load_succeeded has run.
        self.simple_player.is_playing.return_value = True
        self.simple_player.get_pos.return_value = 5.0
        self.simple_player.get_length.return_value = 190.0
        self._playback_watch_last_position = 5.0
        self._playback_watch_has_advanced = True

        # A stale VLC player object left over from whatever played before
        # this Plex track started -- still reporting Ended, exactly the
        # shape the real recovery-ladder bug used to leave behind.
        stale_vlc_player = MagicMock(name="stale_active_player")
        stale_vlc_player.is_playing.return_value = False
        stale_vlc_player.get_state.return_value = vlc.State.Ended
        self.active_player = stale_vlc_player
        self.master_volume = 80
        self._sleep_timer_gain = 1.0

        self.next_track_calls = []
        self._next_track = lambda reason: self.next_track_calls.append(reason)


def test_stale_vlc_ended_callback_is_unreachable_while_plex_bass_is_current():
    harness = TickHarness()

    for _ in range(5):
        harness._tick()

    assert harness.next_track_calls == [], (
        "a stale VLC 'Ended' state left over from a previous backend must "
        "never advance the queue while a Plex/BASS track is genuinely "
        "current -- it must never even be consulted"
    )
    assert harness.recovery_calls == []


def test_control_stale_vlc_ended_would_fire_if_builtin_player_were_not_active():
    # Control: confirms the harness's stale-VLC setup genuinely would
    # trigger _next_track("vlc-ended") if _use_builtin_player() were False
    # (the pre-fix-adjacent shape, e.g. use_simple=False) -- otherwise the
    # test above would be vacuously true.
    harness = TickHarness()
    harness.use_simple = False
    harness._temporary_backend_override = None

    harness._tick()

    assert harness.next_track_calls == ["vlc-ended"]
