"""Astra F3: a queue entry whose local file is unavailable must not stall
automatic advancement or leave its queue token reserved.

Pre-fix path: the real near-end/normal-end trigger in _tick() sets
pending_next and calls _next_track(); _next_track() claims the next row
under a SELECTION owner; _play_path_direct() creates a PlaybackAttempt and
transfers the claim to it; os.path.isfile() fails and _play_path_direct()
returns False -- but the attempt was never terminated, so it kept the claim,
pending_next stayed set (no later trigger ever fires), and _next_track's
"dispatch failed" release of the selection claim was refused because the
selection no longer owned it.

Everything below drives those real methods -- _tick, _next_track,
_play_path_direct, the claim helpers, the PlaybackAttempt lifecycle, local
_play_simple playback and the queue commit -- with inert recording players.
"""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import billsmusic.window as window_module
from billsmusic.media_type import MediaType
from billsmusic.playback_attempt import PlaybackAttemptState
from billsmusic.window import PlayerWindow


class _Player:
    def __init__(self, physical_id):
        self.physical_id = physical_id
        self.playing = False
        self.loaded_path = None
        self.length = 180.0
        self.pos = 0.0

    def load(self, path):
        self.loaded_path = path
        self.pos = 0.0

    def play(self):
        if self.loaded_path is not None:
            self.playing = True

    def stop(self):
        self.playing = False

    def is_playing(self):
        return self.playing

    def set_volume(self, value):
        pass

    def seek(self, seconds):
        pass

    def stats(self):
        return {"duration": self.length, "sample_rate": 44100, "channels": 2}

    def get_length(self):
        return self.length

    def get_pos(self):
        return self.pos


class _Diagnostics:
    def __init__(self):
        self.events = []
        self.counters = {"tracks_completed": 0}

    def record(self, category, operation, **kw):
        self.events.append((category, operation, kw.get("details") or {}))

    def path_details(self, path):
        return {"path": os.path.basename(str(path))}


BOUND_METHODS = (
    "_tick", "_next_track", "_play_path_direct", "_play_simple", "_stop_all",
    "_set_player_topology", "_backend_label", "_use_builtin_player",
    "_use_bass_backend", "_begin_playback_attempt", "_cancel_current_playback_attempt",
    "_advance_playback_attempt_state", "_arm_playback_watchdog", "_cancel_playback_watchdog",
    "_stop_video_for_audio_transition", "_next_unplayed_queue_row", "_queue_entry_is_missing",
    "_ensure_queue_played_flags", "_mark_queue_row_played", "_move_queue_row_to_bottom",
    "_mixed_media_transition_eligible", "_crossfade_eligible_for_transition",
)


def _window(monkeypatch, tmp_path, names, current="current.mp3"):
    """`names` are queue entries; any name starting with "missing" has no file."""
    monkeypatch.setattr(window_module, "VLC_AVAILABLE", False)
    paths = []
    for name in [current] + list(names):
        if name.startswith("plex://"):
            paths.append(name)  # a Plex identity, never a local file
            continue
        path = tmp_path / name
        if not name.startswith("missing"):
            path.write_bytes(b"x")
        paths.append(str(path))
    current_path, queue = paths[0], paths[1:]
    active, inactive = _Player("bass-A"), _Player("bass-B")
    diagnostics = _Diagnostics()
    window = SimpleNamespace(
        queue=queue, queue_played=[False] * len(queue),
        queue_playlist_entries=[None] * len(queue),
        _queue_mutation_epoch=0, _queue_entry_claims={}, _next_queue_entry_token=1,
        _next_queue_selection_id=1, _next_playback_attempt_id=1, _current_playback_attempt=None,
        bass_player=active, bass_inactive_player=inactive,
        miniaudio_player=None, miniaudio_inactive_player=None,
        simple_player=active, simple_inactive_player=inactive,
        active_player=None, inactive_player=None, _player_topology_epoch=0,
        builtin_backend="bass", use_simple=True, _simple_fallback_active=False,
        _temporary_backend_override=None, auto_playback_recovery=False,
        _playback_recovery_active=False, _playback_recovery_attempts={},
        _playback_generation=1, _playback_expected=True, _playback_intentionally_paused=False,
        _current_media_type=MediaType.AUDIO, _mixed_transition_state="idle",
        _video_transition_manager=None, _closing=False, cast_active=False,
        current_path=current_path, current_index=None, track_index_by_path={},
        track_transition_mode="normal", crossfade_seconds=6.0,
        pending_next=False, fade_active=False, prebuffer_active=False, scrubbing=False,
        sleep_timer=SimpleNamespace(is_stop_after_track=False),
        master_volume=100, _sleep_timer_gain=1.0, _active_normalisation_gain=1.0,
        _last_completed_playback_generation=None,
        diagnostics=diagnostics,
        statusBar=lambda: SimpleNamespace(showMessage=lambda *a, **kw: None),
        beat=SimpleNamespace(setPlaying=lambda playing: None),
        _audio_log=lambda message: None,
        _audio_name=lambda path: os.path.basename(str(path)),
        _cached_gain_for_path=lambda path, target=None: 1.0,
        _set_playing_button_state=lambda: None, _cancel_fade=lambda: None,
        _reset_progress=lambda: None, _resume_deferred_queue_analysis=lambda: None,
        _playback_fallback_paths=lambda: [], _refresh_queue_list=lambda **kw: None,
        _record_track_completion=lambda reason, generation=None: True,
        _sync_mini_player=lambda: None, _sync_party_mode=lambda: None,
        _update_recently_played_tracking=lambda: None, _lyrics_tick=lambda: None,
        _sync_karaoke_position=lambda: None, _check_playback_health=lambda: None,
        _update_progress=lambda position, length: None, _current_rms_db=lambda: None,
        _remove_queue_row_widget=lambda row, reason=None: None,
        _insert_queue_row_widget=lambda row, reason=None: None,
        _animate_queue_history_move=lambda row: None, _schedule_session_save=lambda: None,
    )
    window.activated = []
    window._activate_track_ui = lambda path, *, library_index=None, queue_token=None: (
        window.activated.append(path), setattr(window, "current_path", path),
    )
    for name in BOUND_METHODS:
        setattr(window, name, getattr(PlayerWindow, name).__get__(window))
    # The current track is genuinely playing, as a real local dispatch left it.
    active.load(current_path)
    active.play()
    window._ensure_queue_played_flags()
    return window


def _record_dispatches(window):
    """Wrap the real _play_path_direct to record what was dispatched, with the
    PlaybackAttempt each dispatch created."""
    dispatched = []
    real_play = window._play_path_direct

    def _play(path, **kw):
        epoch_at_dispatch = window._queue_mutation_epoch
        result = real_play(path, **kw)
        attempt = window._current_playback_attempt
        dispatched.append({
            "name": os.path.basename(str(path)), "token": kw.get("queue_entry_token"),
            "attempt_id": getattr(attempt, "attempt_id", None),
            "terminal_reason": getattr(attempt, "terminal_reason", None),
            "result": result, "epoch_at_dispatch": epoch_at_dispatch,
        })
        return result

    window._play_path_direct = _play
    return dispatched


def _reach_end_of_current_track(window):
    """The real automatic-advancement producer: _tick() sees the playing
    track's remaining time drop under the normal-transition epsilon."""
    player = window.simple_player
    player.pos = player.length
    window._tick()


def _token(window, name):
    for row, path in enumerate(window.queue):
        if os.path.basename(path) == name:
            return window._queue_entry_tokens[row]
    raise AssertionError(name)


def _row(window, name):
    return [os.path.basename(p) for p in window.queue].index(name)


def test_missing_file_does_not_stall_advancement_or_keep_its_reservation(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path, ["missing.mp3", "valid.mp3"])
    missing_token = _token(window, "missing.mp3")
    valid_token = _token(window, "valid.mp3")

    _reach_end_of_current_track(window)

    # The missing entry's own dispatch attempt terminated and its claim is gone.
    failed = [
        details for cat, op, details in window.diagnostics.events
        if op == "entry_claim_released" and details.get("token") == missing_token
    ]
    assert failed, "the missing entry's reservation was never released"
    assert missing_token not in window._queue_entry_claims
    # Automatic progression reached the valid successor in the same cycle ...
    assert os.path.basename(window.current_path) == "valid.mp3"
    assert window.simple_player.loaded_path.endswith("valid.mp3") and window.simple_player.playing
    assert window._current_playback_attempt.queue_entry_id == valid_token
    assert window._current_playback_attempt.state is PlaybackAttemptState.PLAYING
    assert valid_token not in window._queue_entry_claims  # committed, not left reserved
    assert window.queue_played[_row(window, "valid.mp3")] is True
    # ... the missing entry stays in the queue, unplayed and selectable ...
    assert window.queue_played[_row(window, "missing.mp3")] is False
    assert window._queue_entry_tokens[_row(window, "missing.mp3")] == missing_token
    # ... and advancement is not left pending.
    assert window.pending_next is False


def test_missing_file_attempt_reaches_a_terminal_failed_state(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path, ["missing.mp3"])
    missing_token = _token(window, "missing.mp3")
    attempts = []
    real_begin = window._begin_playback_attempt
    window._begin_playback_attempt = lambda *a, **kw: attempts.append(real_begin(*a, **kw)) or attempts[-1]

    _reach_end_of_current_track(window)

    assert len(attempts) == 1
    assert attempts[0].queue_entry_id == missing_token
    assert attempts[0].state is PlaybackAttemptState.FAILED
    assert window._queue_entry_claims == {}
    refused = [op for _c, op, _d in window.diagnostics.events if op == "entry_claim_release_refused"]
    assert refused == []  # no caller tried to release a claim it no longer owned


def test_all_entries_unavailable_tries_each_once_then_stops_without_a_retry_loop(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path, ["missing-1.mp3", "missing-2.mp3", "missing-3.mp3"])
    dispatched = []
    real_play = window._play_path_direct
    window._play_path_direct = lambda path, **kw: dispatched.append(os.path.basename(path)) or real_play(path, **kw)

    _reach_end_of_current_track(window)

    assert dispatched == ["missing-1.mp3", "missing-2.mp3", "missing-3.mp3"]  # each once, in order
    assert window._queue_entry_claims == {}
    assert window.queue_played == [False, False, False]
    assert window.pending_next is False

    dispatched.clear()
    for _ in range(3):
        window._tick()  # the same finished track on later ticks: no redispatch at all
    assert dispatched == []


class _VideoBackend:
    def __init__(self):
        self.loads = []

    def load(self, path):
        self.loads.append(os.path.basename(path))
        return os.path.isfile(path)  # the real backend's missing-file pre-flight

    def set_muted(self, muted):
        pass

    def set_volume(self, volume):
        pass

    def stop(self):
        pass

    def query_audio_state(self, checkpoint, transition_id):
        return True


MIXED_METHODS = (
    "_begin_mixed_media_transition", "_next_mixed_transition_id",
    "_prepare_mixed_transition_audio_to_video", "_abandon_mixed_media_transition_and_fallback",
    "_reset_mixed_media_transition_state", "_record_mixed_transition_gap_checkpoint",
    "_probe_mixed_video_audio_state",
)


def test_missing_video_in_crossfade_mode_is_passed_over_not_reselected_every_tick(monkeypatch, tmp_path):
    """Crossfade mode routes an audio -> video boundary through the mixed
    transition: the missing video's load is rejected and its hard-cut
    fallback dispatch fails on the missing file. Releasing that entry's
    reservation (F3) must not make every later near-end tick select the
    same missing video again."""
    window = _window(monkeypatch, tmp_path, ["missing.mp4", "valid.mp3"])
    window.track_transition_mode = "crossfade"
    window._video_backend = _VideoBackend()
    window._dual_transition_committed_state_value = lambda: None
    window._start_miniaudio_crossfade_to = lambda *a, **kw: False  # direct start, no worker
    for name, value in dict(
        _mixed_transition_id=0, _mixed_transition_direction=None,
        _mixed_transition_outgoing_path=None, _mixed_transition_incoming_path=None,
        _mixed_transition_incoming_token=None, _mixed_transition_reason=None,
        _mixed_transition_start=None, _mixed_transition_video_audio_scale=0.0,
        _mixed_transition_gain_token=None, _mixed_transition_requested_monotonic=None,
        _mixed_transition_audio_probe_done=set(), _muted=False,
    ).items():
        setattr(window, name, value)
    for name in MIXED_METHODS:
        setattr(window, name, getattr(PlayerWindow, name).__get__(window))
    missing_token = _token(window, "missing.mp4")

    player = window.simple_player
    player.pos = player.length - 1.0  # inside the crossfade near-end window
    window._tick()  # near-end: mixed transition -> load rejected -> fallback fails
    assert window._video_backend.loads == ["missing.mp4"]
    assert missing_token not in window._queue_entry_claims
    assert window.pending_next is False

    for _ in range(5):  # the same outgoing track keeps ticking near its end
        window._tick()

    assert window._video_backend.loads == ["missing.mp4"]  # never re-selected
    assert os.path.basename(window.current_path) == "valid.mp3"
    assert window.queue_played[_row(window, "missing.mp4")] is False
    assert window._queue_entry_claims == {}


def test_valid_successor_is_not_blocked_by_stale_ownership_on_the_next_advance(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path, ["missing.mp3", "first.mp3", "second.mp3"])
    _reach_end_of_current_track(window)
    assert os.path.basename(window.current_path) == "first.mp3"

    _reach_end_of_current_track(window)  # "first.mp3" finishes

    # The unavailable entry is retried once in this new cycle, then passed.
    assert os.path.basename(window.current_path) == "second.mp3"
    assert window._queue_entry_claims == {}
    assert window.queue_played[_row(window, "missing.mp3")] is False
    assert window.pending_next is False


# ---------------------------------------------------------------------------
# Phase 2.1: automatic retry suppression vs explicit user retry
#
# Rule: a token whose dispatch failed synchronously is not dispatched again by
# timer-driven advancement in the same advancement context, but a later
# explicit user command (Next, direct selection) may always retry it.
# ---------------------------------------------------------------------------

PLEX_AUDIO = "plex://server-1/4711.flac"


def _with_explicit_next(window):
    window.next_track = PlayerWindow.next_track.__get__(window)
    window._announce_accessible_status = lambda message: None
    return window


def _with_plex_audio_offer(window, accept=False):
    """Configured backend is miniaudio, so Plex audio needs the BASS-switch
    offer. The dialog is the only stub; the dispatch path is real."""
    window.builtin_backend = "miniaudio"
    window._play_plex_audio_path_direct = PlayerWindow._play_plex_audio_path_direct.__get__(window)
    window.offers = []
    window._offer_switch_to_bass_for_plex_audio = lambda: window.offers.append("offer") or accept
    return window


def _live_attempt(window):
    attempt = window._current_playback_attempt
    return attempt if attempt is not None and not attempt.is_terminal() else None


def test_declined_plex_backend_offer_is_not_reopened_on_every_tick(monkeypatch, tmp_path):
    window = _with_explicit_next(_with_plex_audio_offer(_window(monkeypatch, tmp_path, [PLEX_AUDIO])))
    dispatched = _record_dispatches(window)
    plex_token = window._queue_entry_tokens[0]

    _reach_end_of_current_track(window)  # automatic advancement: offer declined
    for _ in range(5):
        window._tick()  # the finished track keeps ticking

    assert [d["token"] for d in dispatched] == [plex_token]
    assert window.offers == ["offer"]
    assert dispatched[0]["terminal_reason"] == "dispatch_failed"
    assert window.queue_played == [False]
    assert window._queue_entry_claims == {}
    assert _live_attempt(window) is None
    assert window.pending_next is False

    window.next_track()  # an explicit user retry is still allowed

    assert [d["token"] for d in dispatched] == [plex_token, plex_token]
    assert window.offers == ["offer", "offer"]
    assert dispatched[1]["attempt_id"] != dispatched[0]["attempt_id"]
    assert window._queue_entry_claims == {} and _live_attempt(window) is None


def test_disabled_video_rejection_is_tried_once_automatically_but_retryable_explicitly(monkeypatch, tmp_path):
    window = _with_explicit_next(_window(monkeypatch, tmp_path, ["clip.mp4"]))
    window.video_playback_enabled = False
    window._play_video_path_direct = PlayerWindow._play_video_path_direct.__get__(window)
    dispatched = _record_dispatches(window)
    clip_token = window._queue_entry_tokens[0]

    _reach_end_of_current_track(window)
    for _ in range(5):
        window._tick()

    assert [d["token"] for d in dispatched] == [clip_token]
    assert window.queue_played == [False]
    assert window._queue_entry_claims == {} and _live_attempt(window) is None

    window.next_track()

    assert [d["token"] for d in dispatched] == [clip_token, clip_token]
    assert window._queue_entry_claims == {} and _live_attempt(window) is None


def test_manual_next_retries_a_file_restored_after_automatic_suppression(monkeypatch, tmp_path):
    window = _with_explicit_next(_window(monkeypatch, tmp_path, ["missing.mp3"]))
    dispatched = _record_dispatches(window)
    missing_token = window._queue_entry_tokens[0]

    _reach_end_of_current_track(window)  # automatic: unavailable, suppressed
    window._tick()
    assert [d["token"] for d in dispatched] == [missing_token]

    (tmp_path / "missing.mp3").write_bytes(b"x")  # restored -- the queue is untouched
    epoch_before = window._queue_mutation_epoch
    window.next_track()

    assert [d["token"] for d in dispatched] == [missing_token, missing_token]
    assert dispatched[1]["epoch_at_dispatch"] == epoch_before  # no queue edit was needed
    assert dispatched[1]["result"] is True
    assert os.path.basename(window.current_path) == "missing.mp3"
    assert window.simple_player.playing and window.simple_player.loaded_path.endswith("missing.mp3")
    assert window.queue_played == [True]


def test_duplicate_unavailable_paths_are_distinct_tokens_each_tried_once(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path, ["missing.mp3", "missing.mp3"])
    dispatched = _record_dispatches(window)
    first, second = window._queue_entry_tokens

    _reach_end_of_current_track(window)
    for _ in range(3):
        window._tick()

    assert [d["token"] for d in dispatched] == [first, second]
    assert window._queue_entry_claims == {} and window.queue_played == [False, False]


def test_queue_change_lets_automatic_advancement_reconsider_a_suppressed_entry(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path, ["missing.mp3"])
    dispatched = _record_dispatches(window)
    _reach_end_of_current_track(window)
    window._tick()
    assert len(dispatched) == 1

    window._queue_mutation_epoch += 1  # what every wrapped queue edit does
    window._tick()

    assert len(dispatched) == 2


def test_new_playback_generation_expires_old_automatic_suppression(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path, ["missing.mp3", "first.mp3"])
    dispatched = _record_dispatches(window)
    missing_token = window._queue_entry_tokens[0]

    _reach_end_of_current_track(window)  # missing suppressed, first.mp3 starts
    assert [d["name"] for d in dispatched] == ["missing.mp3", "first.mp3"]

    _reach_end_of_current_track(window)  # a new track has played: a new context

    assert [d["token"] for d in dispatched].count(missing_token) == 2
