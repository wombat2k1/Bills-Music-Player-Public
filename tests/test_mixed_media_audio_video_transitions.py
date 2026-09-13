"""v1.0.71: seamless mixed-media Audio<->Video crossfade transitions.

Real-world bug (Codex-audited, discovered during v1.0.70's manual
installed acceptance): Audio->Video and Video->Audio never crossfaded --
_crossfade_eligible_for_transition() returns False whenever either side
of a transition isn't audio (logging crossfade_skipped_for_video), so
_next_track() fell through to a hard cut: the outgoing medium was
stopped immediately, then the incoming one started cold. For Audio->
Video specifically, this combined badly with the existing quiet-end
trigger (_tick()'s RMS-based "the track has gone audibly quiet" early
trigger, intended to skip dead air before a *crossfade* starts) -- quiet-
end fired with ~17 seconds of the track still technically remaining, and
because the next track was video, that early trigger became an
immediate hard stop instead of a graceful overlap.

The fix adds a self-contained preparation/overlap/commit state machine
(_begin_mixed_media_transition and friends in window.py) that sits
alongside -- and never touches -- the existing A-A crossfade engine and
the video-video VideoTransitionManager/GPU dual-deck engine. These tests
drive the real production PlayerWindow methods (bound via __get__ onto a
SimpleNamespace "fake window", the same pattern test_gain_crossfade_
identity_race.py established for v1.0.70), not isolated unit calls.
"""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import billsmusic.window as window_module
from billsmusic.window import PlayerWindow
from billsmusic.media_type import MediaType
from billsmusic.visualiser_lifecycle import VisualiserLifecycleController, VisualiserRunState
from billsmusic.video_dual_transition import DualDeckState
from billsmusic.worker_registry import WorkerLifetimeRegistry

REPLAYGAIN_TAGS = {
    "track_gain": -6.0, "track_peak": 0.9, "album_gain": None, "album_peak": None,
}


class _FakeVideoBackend:
    def __init__(self, load_result=True):
        self.load_calls = []
        self.load_result = load_result
        self.loaded_path = None
        self.muted = None
        self.mute_calls = []
        self.volume = None
        self.volume_calls = []
        self.stopped = False
        self._position_ms = 0
        self._duration_ms = 0
        self.audio_state_queries = []

    def query_audio_state(self, checkpoint, transition_id):
        self.audio_state_queries.append((checkpoint, transition_id))
        return True

    def load(self, path):
        self.loaded_path = path
        self.load_calls.append(path)
        return self.load_result

    def set_muted(self, muted):
        self.muted = muted
        self.mute_calls.append(muted)

    def set_volume(self, v):
        self.volume = v
        self.volume_calls.append(v)

    def stop(self):
        self.stopped = True

    def position_ms(self):
        return self._position_ms

    def duration_ms(self):
        return self._duration_ms


class _FakeAudioPlayer:
    def __init__(self, name="", physical_id=""):
        self.name = name
        self.physical_id = physical_id or name
        self.volume = None
        self.volume_calls = []
        self.playing = False
        self.stopped = False
        self.loaded_path = None
        self._pos = 0.0
        self.commit_prepared_calls = 0

    def load(self, path):
        self.loaded_path = path

    def commit_prepared(self, candidate):
        # Phase C1: mirrors BassPlayer.commit_prepared/
        # MiniaudioPlayer.commit_prepared's public contract closely enough
        # for these tests -- adopts the candidate's path (the fakes above
        # only ever carry a path/token, no real prepared-source object)
        # and reports success, matching every dispatch site's
        # `if use_bass_backend: BassStreamPrepareWorker(path, token)`.
        self.commit_prepared_calls += 1
        self.loaded_path = getattr(candidate, "path", self.loaded_path)
        return True

    def set_volume(self, v):
        self.volume = v
        self.volume_calls.append(v)

    def play(self):
        self.playing = True

    def stop(self):
        self.stopped = True
        self.playing = False

    def get_pos(self):
        return self._pos


class _FakeSignal:
    def __init__(self):
        self.slot = None

    def connect(self, slot):
        self.slot = slot

    def emit(self, *args):
        assert self.slot is not None, "signal fired with no connected slot"
        self.slot(*args)


class _FakePlayerLoadWorker:
    """Stand-in for workers.BassStreamPrepareWorker: constructed like the
    real one (source, token only -- Phase C1's whole point is that this
    worker never receives or holds a live player instance), but
    .start() does nothing -- the test fires .prepared/.failed itself to
    control exactly when the (now off-thread) Video->Audio audio load
    completes relative to other events."""

    def __init__(self, source, token):
        self.source = source
        self.token = token
        self._prepared_candidate = None
        self.prepared = _FakeSignal()
        _real_emit = self.prepared.emit

        def _emit_and_capture(tok, p, candidate):
            self._prepared_candidate = candidate
            _real_emit(tok, p, candidate)

        self.prepared.emit = _emit_and_capture
        self.failed = _FakeSignal()
        self.finished = _FakeSignal()
        self.started = False

    def start(self):
        self.started = True

    def claim_candidate(self):
        candidate = self._prepared_candidate
        self._prepared_candidate = None
        return candidate


class _FakeBassEngine:
    """Phase C1: _prepare_mixed_transition_video_to_audio primes
    _BassEngine.ensure() on the GUI thread before dispatching the first
    BASS prepare worker -- faked here so these dispatch-logic tests
    never touch the real bass.dll/BASS_Init."""
    @staticmethod
    def ensure():
        return None


class _FakePreparedCandidate:
    """Stands in for a real PreparedBassStream/PreparedMiniaudioSource --
    carries just enough (a path) for _FakeAudioPlayer.commit_prepared to
    adopt, and tracks discard() calls for tests asserting a rejected
    candidate was never committed."""
    def __init__(self, path=None):
        self.path = path
        self.discarded = False

    def discard(self):
        self.discarded = True
        return True


BOUND_METHODS = (
    "_next_mixed_transition_id", "_reset_mixed_media_transition_state",
    "_mixed_media_transition_eligible", "_begin_mixed_media_transition",
    "_prepare_mixed_transition_audio_to_video", "_prepare_mixed_transition_video_to_audio",
    "_on_mixed_transition_video_ready", "_on_mixed_transition_video_failed",
    "_on_mixed_transition_audio_load_prepared", "_on_mixed_transition_audio_load_failed",
    # Phase C1 (native audio backend ownership) -- real, unbound so the
    # actual topology/lease logic is exercised, not just its absence
    # papered over.
    "_set_player_topology", "_promote_inactive_player",
    "_make_target_lease", "_target_lease_still_valid",
    "_discard_prepared_candidate",
    "_activate_mixed_media_transition", "_mixed_transition_tick",
    "_finish_mixed_transition_audio_to_video", "_finish_mixed_transition_video_to_audio",
    "_abandon_mixed_media_transition_and_fallback", "_cancel_mixed_media_transition",
    "_maybe_prepare_mixed_transition_from_video",
    "_next_gain_token", "_set_slot_gain", "_cached_gain_for_path",
    "_queue_gain_lookup_async", "_promote_inactive_gain_slot",
    "_next_track", "next_track", "_crossfade_eligible_for_transition",
    "_on_video_started", "_on_video_error", "_on_video_end_of_media",
    "_on_video_position_changed", "_on_video_duration_changed",
    "_mixed_transition_owns_video_boundary",
    "_tick", "_fade_tick",
    "_play_path_direct", "_play_video_path_direct",
    "_stop_video_for_audio_transition", "_dual_transition_committed_state_value",
    "_probe_mixed_video_audio_state", "_probe_post_completion_video_audio_state",
    "_on_video_audio_state_reported", "_record_mixed_transition_gap_checkpoint",
    "_on_mixed_transition_load_worker_finished",
    "_finalize_unclaimed_prepare_candidate",
)


def _window(**overrides):
    outgoing_audio = _FakeAudioPlayer("outgoing", physical_id="bass-A")
    incoming_audio = _FakeAudioPlayer("incoming", physical_id="bass-B")
    video_backend = _FakeVideoBackend()
    diagnostics_calls = []
    window = SimpleNamespace(
        _closing=False,
        track_transition_mode="crossfade",
        crossfade_seconds=6.0,
        current_path="outgoing.mp3",
        current_index=0,
        queue=["outgoing.mp3", "incoming.mp4"],
        queue_played=[True, False],
        queue_playlist_entries=[None, None],
        track_index_by_path={"outgoing.mp3": 0, "incoming.mp4": 1},
        _playback_context_paths=[],
        _playback_fallback_paths=lambda: [],
        _current_media_type=MediaType.AUDIO,
        _video_backend=video_backend,
        simple_player=outgoing_audio,
        simple_inactive_player=incoming_audio,
        bass_player=outgoing_audio,
        bass_inactive_player=incoming_audio,
        miniaudio_player=None,
        miniaudio_inactive_player=None,
        # Phase C1: real _set_player_topology/_make_target_lease are bound
        # via BOUND_METHODS, so this must start initialised the same way
        # production is -- a plain reset (no prior state to diff
        # against), not a call through the seam.
        _player_topology_epoch=0,
        builtin_backend="bass",
        master_volume=100,
        _muted=False,
        _sleep_timer_gain=1.0,
        fade_active=False,
        prebuffer_active=False,
        pending_next=False,
        sleep_timer=SimpleNamespace(is_stop_after_track=False),
        _playback_generation=1,
        _last_completed_playback_generation=None,
        _video_fullscreen=False,
        _video_transition_manager=None,
        _mixed_transition_id=0,
        _mixed_transition_state="idle",
        _mixed_transition_direction=None,
        _mixed_transition_outgoing_path=None,
        _mixed_transition_incoming_path=None,
        _mixed_transition_incoming_row=None,
        _mixed_transition_reason=None,
        _mixed_transition_start=None,
        _mixed_transition_video_audio_scale=0.0,
        _mixed_transition_gain_token=None,
        _mixed_transition_load_worker=None,
        _active_normalisation_gain=1.0,
        _inactive_normalisation_gain=1.0,
        _gain_token_seq=0,
        _active_gain_token=0,
        _inactive_gain_token=0,
        _gain_snapshot_cache={},
        _gain_lookup_pending=set(),
        _gain_lookup_workers=[],
        _gain_lookup_subscribers={},
        _worker_registry=WorkerLifetimeRegistry(),
        _maybe_resume_final_shutdown=lambda: None,
        loudness_cache=SimpleNamespace(override_for=lambda path: "default"),
        normalisation_enabled=True, normalisation_mode="track",
        target_lufs=-14.0, tagged_preamp_db=0.0, untagged_preamp_db=0.0,
        prevent_clipping=True, auto_loudness_analysis=False, loudness_worker=None,
        set_master_volume=lambda v: None,
        statusBar=lambda: SimpleNamespace(showMessage=lambda *a, **kw: None),
        diagnostics=SimpleNamespace(
            record=lambda *a, **kw: diagnostics_calls.append((a, kw)),
            path_details=lambda path: {},
        ),
        _audio_log=lambda message: None,
        _audio_name=lambda path: path,
        _activate_track_ui=lambda index, path: None,
        _begin_playback_recovery=lambda *a, **k: None,
        _reset_progress=lambda: None,
        _arm_playback_watchdog=lambda position: None,
        _cancel_playback_watchdog=lambda: None,
        _stop_all=lambda: (
            setattr(outgoing_audio, "stopped", True),
            setattr(incoming_audio, "stopped", True),
        ),
        _cancel_fade=lambda: None,
        _backend_label=lambda: "BASS",
        _current_backend_name=lambda: "bass",
        _use_bass_backend=lambda: True,
        _use_builtin_player=lambda: True,
        _sync_mini_player=lambda: None,
        _maybe_tick_queue_duration_refresh=lambda: None,
        _sync_party_mode=lambda: None,
        _update_recently_played_tracking=lambda: None,
        _lyrics_tick=lambda: None,
        _sync_karaoke_position=lambda: None,
        _check_playback_health=lambda: None,
        _update_progress=lambda a, b: None,
        _record_video_timing_available=lambda *a, **kw: None,
        scrubbing=False,
        _show_video_output_page=lambda: None,
        _show_normal_display_page=lambda: None,
        _detach_video_from_party_mode=lambda: None,
        _resume_deferred_queue_analysis=lambda: None,
        _exit_video_fullscreen=lambda: None,
        _next_unplayed_queue_row=lambda: next(
            (i for i, played in enumerate(window.queue_played) if not played), None,
        ),
        _peek_next_media_type_for_transition=lambda: (
            None if window._next_unplayed_queue_row() is None
            else _classify(window.queue[window._next_unplayed_queue_row()])
        ),
        _mark_queue_row_played=lambda row: window.queue_played.__setitem__(row, True),
        _announce_accessible_status=lambda message: None,
        _record_track_completion=lambda reason, generation=None: True,
        _queue_entry_is_missing=lambda row: False,
        _refresh_queue_list=lambda **kw: None,
        # Only needed by _play_video_path_direct -- exercised solely by the
        # pre-fix (git stash) proof for the central quiet-end regression
        # test below, since post-fix code never reaches it for a mixed-
        # eligible transition.
        _dual_transition_promoted_path=None,
        video_playback_enabled=True,
        cast_active=False,
        _playback_recovery_active=False,
        _playback_recovery_attempts={},
        _playback_expected=False,
        _playback_intentionally_paused=False,
        _video_progress_started_at=0.0,
        _video_progress_warning_reported=False,
        _video_timing_available_reported=False,
        _show_video_loading_page=lambda: None,
        label_remaining=SimpleNamespace(setText=lambda t: None),
        _dual_transition_committed_state_value=lambda: None,
    )
    for key, value in overrides.items():
        setattr(window, key, value)
    window.diagnostics_calls = diagnostics_calls
    for name in BOUND_METHODS:
        if name in overrides:
            continue  # an explicit override always wins over the real method
        impl = getattr(PlayerWindow, name, None)
        if impl is not None:
            setattr(window, name, impl.__get__(window))
    return window


def _classify(path):
    from billsmusic.media_type import classify_path
    return classify_path(path)


class _FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _patch_clock(monkeypatch, start=1_000_000.0):
    clock = _FakeClock(start)
    monkeypatch.setattr(window_module.time, "time", clock.time)
    return clock


def _drive_ticks(window, clock, n=1, step=0.03):
    for _ in range(n):
        clock.advance(step)
        window._fade_tick()


# ---------------------------------------------------------------------------
# AUDIO -> VIDEO
# ---------------------------------------------------------------------------

def test_audio_to_video_incoming_video_ready_before_fade_window():
    window = _window()
    window._next_track("quiet-end")
    assert window._mixed_transition_state == "preparing"
    assert window._video_backend.load_calls == ["incoming.mp4"]
    assert window._video_backend.muted is True  # muted while preparing

    window._on_video_started()
    assert window._mixed_transition_state == "active"
    assert window._current_media_type == MediaType.VIDEO


def _recorded_ops(window):
    return [args[1] for args, _kwargs in window.diagnostics_calls]


def _recorded_details(window, operation):
    for args, kwargs in window.diagnostics_calls:
        if args[1] == operation:
            return kwargs.get("details", {})
    return None


def test_audio_to_video_gap_checkpoints_carry_increasing_elapsed_ms(monkeypatch):
    """Long audio->video gap investigation: the new timestamped
    checkpoints must actually appear, in order, each with a numeric
    elapsed_ms relative to mixed_transition_requested -- exactly what the
    next real reproduction needs to tell load/buffering/first-frame/
    visibility/fade-start/commit apart without guessing."""
    clock = _patch_clock(monkeypatch)
    window = _window()
    window._next_track("quiet-end")
    ops = _recorded_ops(window)
    assert "incoming_video_load_requested" in ops
    assert "video_play_requested" in ops
    assert _recorded_details(window, "incoming_video_load_requested")["elapsed_ms"] >= 0

    window._on_video_started()
    ops = _recorded_ops(window)
    assert "video_playing_state_received" in ops
    assert "incoming_video_visible" in ops
    assert "audio_fade_out_started" in ops
    # Ordering matches the real call sequence: load/play requested at
    # preparation, playing-state on readiness, visible/fade-start at
    # activation -- each stage's elapsed_ms must not decrease relative to
    # the previous one (the clock only ever advances).
    load_ms = _recorded_details(window, "incoming_video_load_requested")["elapsed_ms"]
    playing_ms = _recorded_details(window, "video_playing_state_received")["elapsed_ms"]
    visible_ms = _recorded_details(window, "incoming_video_visible")["elapsed_ms"]
    fade_ms = _recorded_details(window, "audio_fade_out_started")["elapsed_ms"]
    assert load_ms <= playing_ms <= visible_ms == fade_ms

    _drive_ticks(window, clock, 500)  # complete the fade
    ops = _recorded_ops(window)
    assert "transition_committed" in ops
    assert "transition_completed" in ops
    assert _recorded_details(window, "transition_completed")["elapsed_ms"] >= fade_ms


def test_audio_to_video_gap_checkpoints_never_recorded_for_video_to_audio(monkeypatch):
    """Scoped to Audio->Video only, per instruction -- Video->Audio must
    not gain any of these new events (it already has its own, separate
    diagnostics; this is purely additive for the direction with the
    reported gap)."""
    window = _video_current_window()
    worker = _load_video_to_audio(window, monkeypatch)
    worker.prepared.emit(worker.token, "incoming.mp3", _FakePreparedCandidate())
    ops = set(_recorded_ops(window))
    assert not (ops & {
        "incoming_video_load_requested", "video_play_requested",
        "video_playing_state_received", "incoming_video_visible",
        "audio_fade_out_started", "transition_committed", "transition_completed",
    })


def test_audio_to_video_outgoing_audio_remains_playing_during_fade(monkeypatch):
    clock = _patch_clock(monkeypatch)
    window = _window()
    window._next_track("quiet-end")
    window._on_video_started()
    assert window.simple_player.stopped is False

    _drive_ticks(window, clock, 3)  # partway through the overlap
    assert window.simple_player.stopped is False
    assert window._mixed_transition_state == "active"


def test_audio_to_video_audio_volume_decreases_across_the_fade(monkeypatch):
    clock = _patch_clock(monkeypatch)
    window = _window()
    window._next_track("quiet-end")
    window._on_video_started()
    _drive_ticks(window, clock, 1)
    first = window.simple_player.volume
    _drive_ticks(window, clock, 5)
    second = window.simple_player.volume
    assert second < first


def test_audio_to_video_video_audio_increases_across_the_fade(monkeypatch):
    clock = _patch_clock(monkeypatch)
    window = _window()
    window._next_track("quiet-end")
    window._on_video_started()
    assert window._video_backend.volume == 0
    _drive_ticks(window, clock, 3)
    mid = window._video_backend.volume
    assert mid > 0
    _drive_ticks(window, clock, 200)  # run well past crossfade_seconds
    assert window._video_backend.volume == window.master_volume


def test_audio_to_video_reveal_happens_at_overlap_start():
    reveal_calls = []
    window = _window(_show_video_output_page=lambda: reveal_calls.append(True))
    window._next_track("quiet-end")
    assert reveal_calls == []  # not revealed while merely preparing
    window._on_video_started()
    assert reveal_calls == [True]  # revealed the instant the overlap begins


def test_audio_to_video_outgoing_audio_stops_only_after_fade_completes(monkeypatch):
    clock = _patch_clock(monkeypatch)
    window = _window()
    window._next_track("quiet-end")
    window._on_video_started()
    _drive_ticks(window, clock, 5)  # not yet complete (crossfade_seconds=6.0, 30ms ticks)
    assert window.simple_player.stopped is False
    _drive_ticks(window, clock, 500)  # run well past completion
    assert window.simple_player.stopped is True
    assert window._mixed_transition_state == "idle"


def test_audio_to_video_exactly_one_queue_advance(monkeypatch):
    clock = _patch_clock(monkeypatch)
    window = _window()
    window._next_track("quiet-end")
    assert window.queue_played == [True, True]  # marked at dispatch, matching A-A
    window._on_video_started()
    _drive_ticks(window, clock, 500)
    assert window.queue_played == [True, True]
    # Re-driving further ticks after completion must not mark it again or
    # raise (state is back to idle -- _mixed_transition_tick is a no-op).
    _drive_ticks(window, clock, 5)
    assert window.queue_played == [True, True]


# ---------------------------------------------------------------------------
# VIDEO -> AUDIO
# ---------------------------------------------------------------------------

def _video_current_window(**overrides):
    return _window(
        current_path="outgoing.mp4",
        queue=["outgoing.mp4", "incoming.mp3"],
        queue_played=[True, False],
        track_index_by_path={"outgoing.mp4": 0, "incoming.mp3": 1},
        _current_media_type=MediaType.VIDEO,
        **overrides,
    )


def _load_video_to_audio(window, monkeypatch, near_end=True):
    """Drive the real Video->Audio near-end trigger (_tick() ->
    _maybe_prepare_mixed_transition_from_video -> _begin_mixed_media_
    transition -> _prepare_mixed_transition_video_to_audio), which now
    dispatches the incoming audio load via the same off-thread
    PlayerLoadWorker mechanism the A-A crossfade's own incoming-player
    load already uses (see the v1.0.71 correction comment on
    _prepare_mixed_transition_video_to_audio). Returns the fake worker so
    the test controls exactly when the load completes."""
    monkeypatch.setattr(window_module, "BassStreamPrepareWorker", _FakePlayerLoadWorker)
    monkeypatch.setattr(window_module, "_BassEngine", _FakeBassEngine)
    if near_end:
        window._video_backend._duration_ms = 100_000
        window._video_backend._position_ms = 100_000 - 6500
    window._tick()
    worker = window._mixed_transition_load_worker
    assert worker is not None, "expected a PlayerLoadWorker to have been dispatched"
    return worker


def test_video_to_audio_incoming_audio_loaded_before_video_end(monkeypatch):
    window = _video_current_window()
    worker = _load_video_to_audio(window, monkeypatch)

    assert window._mixed_transition_state == "preparing"  # load dispatched, not yet complete
    # Phase C1 structural requirement: the worker holds no player
    # reference at all -- only source/token.
    assert not hasattr(worker, "player")
    assert worker.source == "incoming.mp3"

    worker.prepared.emit(worker.token, "incoming.mp3", _FakePreparedCandidate())

    assert window._mixed_transition_state == "active"
    assert window._current_media_type == MediaType.VIDEO  # video still current/visible


def test_video_to_audio_incoming_audio_starts_muted(monkeypatch):
    window = _video_current_window()
    worker = _load_video_to_audio(window, monkeypatch)
    worker.prepared.emit(worker.token, "incoming.mp3", _FakePreparedCandidate())
    assert window.simple_inactive_player.volume == 0.0


def test_video_to_audio_video_audio_decreases_across_the_fade(monkeypatch):
    clock = _patch_clock(monkeypatch)
    window = _video_current_window()
    worker = _load_video_to_audio(window, monkeypatch)
    worker.prepared.emit(worker.token, "incoming.mp3", _FakePreparedCandidate())
    _drive_ticks(window, clock, 1)
    first = window._video_backend.volume
    assert first > window.master_volume * 0.9  # near-full at the very start (out_scale~=1)
    _drive_ticks(window, clock, 5)
    assert window._video_backend.volume < first


def test_video_to_audio_incoming_bass_audio_increases_across_the_fade(monkeypatch):
    clock = _patch_clock(monkeypatch)
    window = _video_current_window()
    worker = _load_video_to_audio(window, monkeypatch)
    worker.prepared.emit(worker.token, "incoming.mp3", _FakePreparedCandidate())
    _drive_ticks(window, clock, 1)
    first = window.simple_inactive_player.volume
    _drive_ticks(window, clock, 5)
    second = window.simple_inactive_player.volume
    assert second > first


def test_video_to_audio_outgoing_video_not_hidden_too_early(monkeypatch):
    clock = _patch_clock(monkeypatch)
    hide_calls = []
    window = _video_current_window(
        _show_normal_display_page=lambda: hide_calls.append(True),
    )
    worker = _load_video_to_audio(window, monkeypatch)
    worker.prepared.emit(worker.token, "incoming.mp3", _FakePreparedCandidate())
    _drive_ticks(window, clock, 5)  # partway through the overlap
    assert hide_calls == []
    assert window._video_backend.stopped is False
    _drive_ticks(window, clock, 500)  # completion
    assert hide_calls == [True]
    assert window._video_backend.stopped is True


def test_video_to_audio_exactly_one_queue_advance(monkeypatch):
    clock = _patch_clock(monkeypatch)
    window = _video_current_window()
    worker = _load_video_to_audio(window, monkeypatch)
    assert window.queue_played == [True, False]  # not yet -- load still in flight
    worker.prepared.emit(worker.token, "incoming.mp3", _FakePreparedCandidate())
    assert window.queue_played == [True, True]  # marked once preparation succeeded
    _drive_ticks(window, clock, 500)
    assert window.queue_played == [True, True]
    assert window._current_media_type == MediaType.AUDIO
    assert window.simple_player is not None


# ---------------------------------------------------------------------------
# HOSTILE ORDERING
# ---------------------------------------------------------------------------

def test_audio_to_video_preparation_slow_audio_keeps_playing(monkeypatch):
    clock = _patch_clock(monkeypatch)
    window = _window()
    window._next_track("quiet-end")
    assert window._mixed_transition_state == "preparing"
    # Time passes with no video_started signal at all -- nothing about the
    # outgoing audio changes; fade_timer's tick is a no-op while preparing.
    _drive_ticks(window, clock, 50)
    assert window.simple_player.stopped is False
    assert window._mixed_transition_state == "preparing"


def test_video_to_audio_preparation_slow_video_keeps_playing(monkeypatch):
    window = _video_current_window()
    worker = _load_video_to_audio(window, monkeypatch)
    assert window._mixed_transition_state == "preparing"
    assert worker.started is True
    # Time passes with the audio load worker still not having reported
    # back -- the video keeps playing untouched; nothing about the outgoing
    # video changes just because the incoming audio load is slow (e.g. a
    # real NAS-backed file), matching why this was moved off the GUI
    # thread in the first place (see _prepare_mixed_transition_video_to_
    # audio's v1.0.71 correction comment).
    assert window._video_backend.stopped is False
    assert window._current_media_type == MediaType.VIDEO

    worker.prepared.emit(worker.token, "incoming.mp3", _FakePreparedCandidate())
    assert window._mixed_transition_state == "active"


def test_video_preparation_failure_falls_back_without_touching_audio():
    played = []
    window = _window(
        _video_backend=_FakeVideoBackend(load_result=False),
        _play_path_direct=lambda path, crossfade=False, index=None, immediate_crossfade=False: played.append(path) or True,
    )
    window._next_track("quiet-end")
    assert window._mixed_transition_state == "idle"
    assert window.simple_player.stopped is False  # never touched
    assert played == ["incoming.mp4"]  # fell back to the direct hard-cut path
    assert window.queue_played == [True, True]


def test_audio_preparation_failure_leaves_video_playing(monkeypatch):
    window = _video_current_window()
    worker = _load_video_to_audio(window, monkeypatch)

    # The off-thread load itself failed (mirrors the real PlayerLoadWorker
    # catching player.load()'s own exception and emitting load_failed
    # rather than raising it back into window.py).
    worker.failed.emit(worker.token, "incoming.mp3", "disk error")

    assert window._mixed_transition_state == "idle"
    assert window._video_backend.stopped is False  # video was never touched
    assert window.queue_played == [True, False]  # not advanced -- video keeps playing to retry later


def test_manual_next_during_audio_to_video_preparation_abandons_it():
    # queue_played[1] ("incoming.mp4") is marked played optimistically the
    # instant preparation is dispatched (matching A-A's own
    # _start_miniaudio_crossfade_to precedent -- see
    # _begin_mixed_media_transition), so a fresh Manual Next during
    # preparation naturally moves on to row 2, not a retry of row 1.
    window = _window(
        queue=["outgoing.mp3", "incoming.mp4", "third.mp4"],
        queue_played=[True, False, False],
        track_index_by_path={"outgoing.mp3": 0, "incoming.mp4": 1, "third.mp4": 2},
    )
    window._next_track("quiet-end")
    assert window._mixed_transition_state == "preparing"
    assert window.queue_played == [True, True, False]
    first_video_load = window._video_backend.load_calls[0]

    window.next_track()

    assert first_video_load == "incoming.mp4"
    assert window._video_backend.stopped is True  # the abandoned video load was stopped
    # A fresh decision was made for the (now next-in-line) mixed transition.
    assert window._mixed_transition_incoming_path == "third.mp4"
    assert window.queue_played == [True, True, True]


def test_manual_next_during_audio_to_video_active_fade_no_two_videos():
    window = _window(
        queue=["outgoing.mp3", "incoming.mp4", "third.mp4"],
        queue_played=[True, False, False],
        track_index_by_path={"outgoing.mp3": 0, "incoming.mp4": 1, "third.mp4": 2},
    )
    window._next_track("quiet-end")
    window._on_video_started()
    assert window._mixed_transition_state == "active"
    first_backend = window._video_backend

    window.next_track()

    assert first_backend.stopped is True
    assert first_backend is window._video_backend  # one persistent backend, matching reality
    # The abandoned video's load, then a fresh one for the new target --
    # never two videos loaded without the first being stopped in between.
    assert window._video_backend.load_calls == ["incoming.mp4", "third.mp4"]


def test_manual_next_during_video_to_audio_preparation_abandons_stale_load(monkeypatch):
    # v1.0.71 correction: preparing the inactive audio player now loads
    # off-thread (PlayerLoadWorker), so there is a genuine "preparing"
    # window before the load reports back -- Manual Next during that
    # window must cleanly abandon it. Since the row is only marked played
    # once the load succeeds (see _activate_mixed_media_transition),
    # "incoming.mp3" is still the next-unplayed row afterward, so Manual
    # Next's fresh decision correctly re-dispatches a brand new load for
    # the same target rather than leaving the transition idle.
    window = _video_current_window()
    stale_worker = _load_video_to_audio(window, monkeypatch)
    assert window._mixed_transition_state == "preparing"
    stale_id = window._mixed_transition_id
    stale_player = window.simple_inactive_player

    window.next_track()

    assert stale_player.stopped is True
    assert window._mixed_transition_state == "preparing"
    assert window._mixed_transition_id != stale_id  # a fresh transition, not the old one
    fresh_worker = window._mixed_transition_load_worker
    assert fresh_worker is not None and fresh_worker is not stale_worker

    # Phase C1 hostile-ownership scenario E: the now-stale load result
    # arriving later must be dropped -- discarded, never applied to the
    # (different) fresh transition now in flight, and never mutating
    # whichever physical player now occupies the inactive slot.
    commits_before = stale_player.commit_prepared_calls
    stale_candidate = _FakePreparedCandidate("incoming.mp3")
    stale_worker.prepared.emit(stale_worker.token, "incoming.mp3", stale_candidate)
    assert window._mixed_transition_state == "preparing"
    assert window._mixed_transition_id != stale_id
    assert stale_candidate.discarded is True
    assert stale_player.commit_prepared_calls == commits_before  # never mutated


def test_stale_worker_finished_after_fresh_worker_started_does_not_erase_ownership(monkeypatch):
    """Real-device correctness bug (2026-08-31 Codex audit): worker A's own
    `finished` signal used to unconditionally null
    `_mixed_transition_load_worker`, regardless of whether a fresh worker B
    had *already* been assigned there in the meantime (exactly the Manual-
    Next-during-preparation scenario the sibling test above sets up). A's
    result callbacks were already correctly guarded by transition_id/token,
    but its *cleanup* callback was not -- A finishing after B started would
    silently erase the reference to B while B was still genuinely running.
    Reproduces that exact ordering and asserts B's ownership survives it."""
    window = _video_current_window()
    stale_worker = _load_video_to_audio(window, monkeypatch)
    window.next_track()
    fresh_worker = window._mixed_transition_load_worker
    assert fresh_worker is not None and fresh_worker is not stale_worker

    # A's finished signal arrives *after* B has already taken ownership of
    # the slot -- this must not touch B's reference at all.
    stale_worker.finished.emit()

    assert window._mixed_transition_load_worker is fresh_worker, (
        "a superseded worker's own finished cleanup erased ownership of "
        "the fresh worker that replaced it"
    )

    # B finishing afterwards must still correctly clear its own ownership.
    fresh_worker.finished.emit()
    assert window._mixed_transition_load_worker is None


def test_manual_next_during_video_to_audio_active_fade_no_double_advance_no_stale_promotion(monkeypatch):
    clock = _patch_clock(monkeypatch)
    window = _video_current_window()
    worker = _load_video_to_audio(window, monkeypatch)
    worker.prepared.emit(worker.token, "incoming.mp3", _FakePreparedCandidate())
    _drive_ticks(window, clock, 3)  # partway through the overlap
    stale_player = window.simple_inactive_player
    original_active = window.simple_player

    window.next_track()

    assert stale_player.stopped is True
    assert window.simple_player is original_active  # never promoted
    # The row was already marked played once preparation succeeded (see
    # _activate_mixed_media_transition) -- Manual Next abandoning the
    # overlap is a user-initiated skip, exactly like Next during an
    # ordinary A-A crossfade, not a failure that should un-advance it.
    assert window.queue_played == [True, True]
    assert window._mixed_transition_state == "idle"


def test_manual_next_during_preparation_stops_the_abandoned_video_backend():
    # The window.py-level guard (_on_mixed_transition_video_ready only
    # acts while state == "preparing" and direction == "audio_to_video")
    # is a coarse, cheap secondary check -- it cannot by itself tell which
    # *specific* video a stray ready signal belongs to, since the signal
    # carries no payload. The real protection against a stale ready signal
    # from an abandoned load is QtVideoPlaybackBackend's own per-load
    # _token invalidation (bumped on every load()/stop()), which a fake
    # test backend doesn't reproduce -- see CODEX_HANDOFF.md's v1.0.71
    # section for the full reasoning. What IS directly verifiable here:
    # abandoning a preparing transition must stop the old video backend
    # (so it can never itself go on to emit a real ready signal), and a
    # fresh, correctly-scoped transition must be the only thing prepared
    # afterward.
    window = _window(
        queue=["outgoing.mp3", "incoming.mp4", "third.mp4"],
        queue_played=[True, False, False],
        track_index_by_path={"outgoing.mp3": 0, "incoming.mp4": 1, "third.mp4": 2},
    )
    window._next_track("quiet-end")
    abandoned_backend = window._video_backend
    window.next_track()  # abandons "incoming.mp4", starts preparing "third.mp4"

    assert abandoned_backend.stopped is True
    assert window._mixed_transition_incoming_path == "third.mp4"
    assert window._mixed_transition_state == "preparing"


def test_stale_video_ready_after_full_cancel_is_dropped():
    window = _window()
    window._next_track("quiet-end")
    window._cancel_mixed_media_transition("test_abandon")
    assert window._mixed_transition_state == "idle"

    window._on_mixed_transition_video_ready()  # must not resurrect anything

    assert window._mixed_transition_state == "idle"
    assert window._current_media_type == MediaType.AUDIO


def test_outgoing_audio_eof_during_active_audio_to_video_does_not_double_fire():
    calls = []
    window = _window(_next_track=lambda reason: calls.append(reason))
    real_next_track = PlayerWindow._next_track.__get__(window)
    real_next_track("quiet-end")
    window._on_video_started()
    assert window._current_media_type == MediaType.VIDEO
    # _tick()'s own audio near-end/quiet-end polling never runs once
    # _current_media_type flipped to VIDEO -- confirmed by the early
    # return for MediaType.VIDEO in _tick() itself (existing behaviour,
    # untouched by v1.0.71).
    window._tick()
    assert calls == []


def test_outgoing_video_ended_during_active_video_to_audio_cancels_cleanly(monkeypatch):
    window = _video_current_window()
    worker = _load_video_to_audio(window, monkeypatch)
    worker.prepared.emit(worker.token, "incoming.mp3", _FakePreparedCandidate())
    assert window._mixed_transition_state == "active"

    # The video's own natural-end handler fires mid-overlap (e.g. a very
    # short clip) -- _next_track's own top-of-function cancel-guard must
    # cleanly abandon the in-flight transition rather than double-advance.
    window._next_track("video-ended")

    assert window.queue_played.count(True) == 2  # exactly one advance total


def test_quiet_end_during_active_audio_to_video_does_not_refire():
    calls = []
    window = _window()
    window._next_track("quiet-end")
    assert window.pending_next is True
    window._on_video_started()

    # A second, redundant quiet-end style call (as could happen if some
    # other code path called _next_track directly) must cleanly cancel the
    # active transition (restoring audio display) rather than corrupt
    # state or double-mark the queue -- with no further unplayed rows in
    # this fixture's 2-entry queue, nothing new starts afterward.
    window._next_track("quiet-end")

    assert window.queue_played == [True, True]
    assert window._current_media_type == MediaType.AUDIO
    assert window._mixed_transition_state == "idle"


def test_no_double_queue_advancement_across_full_audio_to_video_lifecycle(monkeypatch):
    clock = _patch_clock(monkeypatch)
    window = _window()
    window._next_track("quiet-end")
    window._on_video_started()
    _drive_ticks(window, clock, 500)
    assert window.queue_played == [True, True]
    _drive_ticks(window, clock, 500)  # idle now; must stay exactly as-is
    assert window.queue_played == [True, True]


def test_shutdown_during_mixed_transition_cancels_without_touching_ui():
    ui_calls = []
    window = _window(
        _show_normal_display_page=lambda: ui_calls.append("normal"),
        _detach_video_from_party_mode=lambda: ui_calls.append("detach"),
    )
    window._next_track("quiet-end")
    window._on_video_started()
    assert window._mixed_transition_state == "active"

    window._closing = True
    window._cancel_mixed_media_transition("shutdown")

    assert window._mixed_transition_state == "idle"
    assert ui_calls == []  # no UI touched during a closing-time cancel
    assert window._video_backend.stopped is True


def test_shutdown_during_active_video_to_audio_remains_clean(monkeypatch):
    window = _video_current_window()
    worker = _load_video_to_audio(window, monkeypatch)
    worker.prepared.emit(worker.token, "incoming.mp3", _FakePreparedCandidate())
    assert window._mixed_transition_state == "active"

    window._closing = True
    window._cancel_mixed_media_transition("shutdown")

    assert window._mixed_transition_state == "idle"
    assert window.simple_inactive_player.stopped is True
    assert window._current_media_type == MediaType.VIDEO  # cancel doesn't touch it for V->A


def test_replaygain_identity_still_correct_during_video_to_audio_incoming_fade(monkeypatch):
    class _FakeSignal:
        def __init__(self):
            self.slot = None
        def connect(self, slot):
            self.slot = slot
        def emit(self, *args):
            self.slot(*args)

    class _FakeGainWorker:
        def __init__(self, path, loudness_cache):
            self.path = path
            self.gain_ready = _FakeSignal()
            self.finished = _FakeSignal()
        def start(self):
            pass

    monkeypatch.setattr(window_module, "GainLookupWorker", _FakeGainWorker)
    clock = _patch_clock(monkeypatch)
    window = _video_current_window()
    load_worker = _load_video_to_audio(window, monkeypatch)
    load_worker.prepared.emit(load_worker.token, "incoming.mp3", _FakePreparedCandidate())  # dispatches the gain lookup (cache miss)

    assert window._inactive_normalisation_gain == 1.0  # safe default while pending
    worker = window._gain_lookup_workers[-1]

    _drive_ticks(window, clock, 3)  # partway through the overlap, gain still pending
    worker.gain_ready.emit("incoming.mp3", REPLAYGAIN_TAGS, None)

    expected = window._gain_snapshot_cache["incoming.mp3"].linear_gain
    assert expected != 1.0
    assert window._inactive_normalisation_gain == expected
    _drive_ticks(window, clock, 1)
    assert window.simple_inactive_player.volume_calls[-1] != 0.0


# ---------------------------------------------------------------------------
# Central quiet-end regression (section 26): the exact real-world scenario
# ---------------------------------------------------------------------------

def test_quiet_end_with_video_next_no_longer_hard_cuts_the_audio(monkeypatch):
    """Reproduces the real installed-build log: BASS audio playing, quiet-
    end fires with ~16.9s of the track still remaining, next queue entry is
    a video. Pre-fix (git stash -- billsmusic/window.py), this called
    _play_path_direct(next_path, crossfade=False, ...) directly from
    _next_track, which for the builtin backend calls _play_video_path_direct
    -> _stop_all() BEFORE the video ever loads -- i.e. the outgoing audio
    player's .stop() was called synchronously, immediately, as part of
    that single _next_track("quiet-end") call. Post-fix, that same call
    must enter the mixed-transition lifecycle instead: the outgoing
    player must NOT be stopped by the trigger itself.

    os.path.isfile is patched to True since these are fake in-memory
    paths, not real files -- without this, the pre-fix code path returns
    early at _play_path_direct's missing-file guard before ever reaching
    the hard-cut logic under test, which would make the "proof" trivially
    (and wrongly) pass for the wrong reason."""
    monkeypatch.setattr(window_module.os.path, "isfile", lambda path: True)
    window = _window()
    window.crossfade_seconds = 6.0
    # 16.90s remaining, matching the real log line: "quiet-end trigger;
    # remaining=16.90s". Nothing about the trigger itself depends on the
    # exact remaining value once _next_track has been reached -- this
    # documents the real scenario for traceability.

    window._next_track("quiet-end")

    assert window.simple_player.stopped is False, (
        "quiet-end must not hard-stop the audio just because next media is video"
    )
    assert window._mixed_transition_state == "preparing"
    assert window._video_backend.load_calls == ["incoming.mp4"]


# ---------------------------------------------------------------------------
# Acceptance-failure corrections (real installed-build acceptance run,
# session e23fe1f87ca449b19d4272faf1793104): equaliser freeze, promoted
# audio never actually starting, and mixed/video-video ownership collision.
# ---------------------------------------------------------------------------

class _FakeBeat:
    def __init__(self):
        self.calls = []
        self.is_suspended = True

    def resume(self):
        self.calls.append("resume")
        self.is_suspended = False

    def suspend(self):
        self.calls.append("suspend")
        self.is_suspended = True


class _FakeAnalyzerTimer:
    def __init__(self, active=False):
        self._active = active
        self.start_calls = 0
        self.stop_calls = 0

    def isActive(self):
        return self._active

    def start(self):
        self._active = True
        self.start_calls += 1

    def stop(self):
        self._active = False
        self.stop_calls += 1


def _video_current_window_with_visualiser(**overrides):
    """Extends _video_current_window with the real, pure-Python
    VisualiserLifecycleController machinery (billsmusic/visualiser_
    lifecycle.py) plus lightweight Qt-widget stand-ins, so these tests
    drive the *real* _refresh_visualiser_lifecycle/_apply_visualiser_
    transition/_apply_analyzer_feed_transition/_effective_visualiser_
    visibility/_effective_analyzer_feed_needed methods end to end -- not
    a re-implementation of the equaliser-freeze fix's own logic."""
    analyzer_tick_calls = []
    window = _video_current_window(
        _visualiser_lifecycle=VisualiserLifecycleController(
            "main", initial_state=VisualiserRunState.SUSPENDED,
        ),
        _analyzer_feed_lifecycle=VisualiserLifecycleController(
            "analyzer_feed", initial_state=VisualiserRunState.SUSPENDED,
        ),
        beat=_FakeBeat(),
        analyzer_timer=_FakeAnalyzerTimer(active=False),
        visualiser_frame=SimpleNamespace(isVisible=lambda: True),
        mini_player=None,
        party_mode=None,
        isMinimized=lambda: False,
        isVisible=lambda: True,
        _analyzer_tick=lambda: analyzer_tick_calls.append(True),
        **overrides,
    )
    window.analyzer_tick_calls = analyzer_tick_calls
    for name in (
        "_refresh_visualiser_lifecycle", "_effective_visualiser_visibility",
        "_effective_analyzer_feed_needed", "_apply_visualiser_transition",
        "_apply_analyzer_feed_transition", "_record_visualiser_diagnostics",
    ):
        impl = getattr(PlayerWindow, name, None)
        if impl is not None:
            setattr(window, name, impl.__get__(window))
    # _show_normal_display_page for these tests should be the REAL one
    # (it's what calls _refresh_visualiser_lifecycle("video_hidden")) --
    # the shared _window() default is a no-op stub, override it back.
    window._show_normal_display_page = PlayerWindow._show_normal_display_page.__get__(window)
    window.right_display_stack = SimpleNamespace(setCurrentWidget=lambda w: None)
    window._normal_display_page = object()
    window._record_video_host_visibility_change = lambda page: None
    return window


def test_video_to_audio_completion_resumes_main_visualiser(monkeypatch):
    # Clock must be patched BEFORE the transition activates -- _mixed_
    # transition_start is stamped with time.time() at that moment, and a
    # clock patched afterward would leave it holding a real (huge) epoch
    # value the fake clock's much smaller values could never catch up to.
    clock = _patch_clock(monkeypatch, start=1_000_000.0)
    window = _video_current_window_with_visualiser()
    worker = _load_video_to_audio(window, monkeypatch)
    worker.prepared.emit(worker.token, "incoming.mp3", _FakePreparedCandidate())
    assert window._mixed_transition_state == "active"
    assert window.beat.is_suspended is True  # still suspended while video shows

    _drive_ticks(window, clock, 500)  # run the fade to completion

    assert window._current_media_type == MediaType.AUDIO
    assert "resume" in window.beat.calls, (
        "the main visualiser must resume once Video->Audio actually completes"
    )
    assert window.beat.is_suspended is False


def test_video_to_audio_completion_resumes_analyzer_feed_and_timer(monkeypatch):
    clock = _patch_clock(monkeypatch, start=2_000_000.0)
    window = _video_current_window_with_visualiser()
    worker = _load_video_to_audio(window, monkeypatch)
    worker.prepared.emit(worker.token, "incoming.mp3", _FakePreparedCandidate())
    _drive_ticks(window, clock, 500)

    assert window.analyzer_timer.isActive() is True, (
        "analyzer_timer must be running again once Video->Audio completes -- "
        "this is the confirmed root cause of the equaliser freeze: "
        "_current_media_type used to still read VIDEO at the instant "
        "_show_normal_display_page() re-evaluated the lifecycle"
    )


def test_video_to_audio_completion_analyzer_feed_produces_fresh_data(monkeypatch):
    # Proxy for "spectrum data changes": _apply_analyzer_feed_transition
    # calls _analyzer_tick() itself the instant the feed resumes (see
    # window.py), specifically so the very first post-video frame already
    # reflects live data instead of waiting for the next timer tick.
    clock = _patch_clock(monkeypatch, start=3_000_000.0)
    window = _video_current_window_with_visualiser()
    worker = _load_video_to_audio(window, monkeypatch)
    worker.prepared.emit(worker.token, "incoming.mp3", _FakePreparedCandidate())
    _drive_ticks(window, clock, 500)

    assert window.analyzer_tick_calls, (
        "the resumed analyzer feed must pull a fresh tick immediately, "
        "not leave the equaliser showing stale/frozen bar values until "
        "the next natural timer interval"
    )


def test_video_to_audio_incoming_bass_player_actually_starts(monkeypatch):
    # Section 2 of the acceptance failure: "load succeeded" != "playback
    # started". Confirmed root cause of the startup-stall recovery firing
    # ~2s after a mixed transition completed in the real session.
    window = _video_current_window()
    worker = _load_video_to_audio(window, monkeypatch)
    assert window.simple_inactive_player.playing is False  # not yet, load still in flight

    worker.prepared.emit(worker.token, "incoming.mp3", _FakePreparedCandidate())

    assert window.simple_inactive_player.playing is True, (
        "the incoming player must actually be started (not just loaded) "
        "during preparation, or its position never advances and the "
        "playback watchdog concludes it stalled"
    )


def test_video_to_audio_incoming_position_advances_before_and_after_promotion(monkeypatch):
    clock = _patch_clock(monkeypatch)
    window = _video_current_window()
    worker = _load_video_to_audio(window, monkeypatch)
    worker.prepared.emit(worker.token, "incoming.mp3", _FakePreparedCandidate())
    incoming = window.simple_inactive_player
    assert incoming.playing is True

    # Simulate real playback advancing the position while the transition
    # is still fading in (a real BASS/miniaudio player's get_pos() reflects
    # genuine decode/playback progress once .play() has been called).
    incoming._pos = 1.5
    assert incoming.get_pos() > 0.0, "position must be advancing before promotion"

    _drive_ticks(window, clock, 500)  # run the fade to completion/promotion

    assert window.simple_player is incoming  # promotion preserves the already-running stream
    incoming._pos = 3.2  # playback continues advancing
    assert window.simple_player.get_pos() > 1.5, "position must keep advancing after promotion"
    assert window.simple_player.stopped is False  # the promoted (former incoming) stream itself was never stopped


class _RecordingTransitionManager:
    def __init__(self, committed=False):
        self.observe_position_calls = []
        self.cancel_calls = []
        self._committed = committed
        self.dual_engine = SimpleNamespace(
            controller=SimpleNamespace(
                state=DualDeckState.TRANSITIONING if committed else None,
            ),
        )

    def observe_position(self, position_ms, duration_ms, media_type):
        self.observe_position_calls.append((position_ms, duration_ms, media_type))

    def cancel(self, reason, *, allow_automatic_retry=False):
        self.cancel_calls.append(reason)
        return True


def test_active_video_to_audio_suppresses_video_video_observe_position(monkeypatch):
    manager = _RecordingTransitionManager()
    window = _video_current_window(_video_transition_manager=manager)
    worker = _load_video_to_audio(window, monkeypatch)
    worker.prepared.emit(worker.token, "incoming.mp3", _FakePreparedCandidate())
    assert window._mixed_transition_state == "active"
    manager.observe_position_calls.clear()  # ignore whatever happened before "active"

    window._video_backend._position_ms = 95_000
    window._on_video_position_changed(95_000)
    window._on_video_duration_changed(100_000)

    assert manager.observe_position_calls == [], (
        "the video-video preload/deadline engine must not be fed further "
        "position data while a Video->Audio mixed transition owns this "
        "boundary -- confirmed real-world bug: it independently preloaded "
        "and later committed a transition to a *different* video while "
        "this exact overlap was already active, silently skipping the "
        "incoming audio track"
    )


def test_video_to_audio_preparing_also_suppresses_observe_position(monkeypatch):
    manager = _RecordingTransitionManager()
    window = _video_current_window(_video_transition_manager=manager)
    _load_video_to_audio(window, monkeypatch)
    assert window._mixed_transition_state == "preparing"
    manager.observe_position_calls.clear()

    window._on_video_position_changed(90_000)

    assert manager.observe_position_calls == []


def test_begin_video_to_audio_cancels_existing_video_video_preload(monkeypatch):
    manager = _RecordingTransitionManager()
    window = _video_current_window(_video_transition_manager=manager)

    _load_video_to_audio(window, monkeypatch)

    assert "mixed_transition_claimed_boundary" in manager.cancel_calls, (
        "claiming the Video->Audio boundary must tear down any video-video "
        "preload/deadline-timer state that started before position-update "
        "suppression could take effect"
    )


def test_video_to_audio_declines_when_dual_transition_already_committed(monkeypatch):
    manager = _RecordingTransitionManager(committed=True)
    window = _video_current_window(_video_transition_manager=manager)

    # _begin_mixed_media_transition must decline outright -- a genuine GPU
    # cross-dissolve already owns this boundary.
    monkeypatch.setattr(window_module, "BassStreamPrepareWorker", _FakePlayerLoadWorker)
    monkeypatch.setattr(window_module, "_BassEngine", _FakeBassEngine)
    window._video_backend._duration_ms = 100_000
    window._video_backend._position_ms = 100_000 - 6500
    window._tick()

    assert window._mixed_transition_state == "idle"
    assert window._mixed_transition_load_worker is None
    assert manager.cancel_calls == []  # never even tried to contest it


def test_video_ended_during_active_mixed_transition_never_calls_handle_natural_end(monkeypatch):
    handle_natural_end_calls = []
    manager = SimpleNamespace(
        handle_natural_end=lambda media_type: handle_natural_end_calls.append(media_type) or True,
        observe_position=lambda *a: None,
        cancel=lambda *a, **kw: True,
    )
    window = _video_current_window(_video_transition_manager=manager)
    worker = _load_video_to_audio(window, monkeypatch)
    worker.prepared.emit(worker.token, "incoming.mp3", _FakePreparedCandidate())
    assert window._mixed_transition_state == "active"

    window._on_video_end_of_media()

    assert handle_natural_end_calls == [], (
        "a committed/active Video->Audio mixed transition already owns "
        "this boundary -- the outgoing video's own natural end-of-media "
        "must not be handed to the video-video engine at all while it "
        "does"
    )


def test_manual_next_during_active_video_to_audio_never_reaches_video_transition_manager(monkeypatch):
    """Real-device confirmed bug (2026-09-05, user report): "press Next
    while a video is crossfading into a music track" did nothing further
    -- the video just kept playing. Real diagnostics showed repeated
    mixed_transition_cancelled(direction=video_to_audio, reason=
    manual_next) events with nothing else ever following.

    Root cause: next_track() cancels the active mixed transition (which
    already, by design, marks the incoming track played and abandons it
    -- see test_manual_next_during_video_to_audio_active_fade_no_double_
    advance_no_stale_promotion), then unconditionally asks
    VideoTransitionManager.request_manual_next(self._current_media_type)
    -- which stays MediaType.VIDEO throughout a Video->Audio overlap by
    design. VideoTransitionController.supports() returns True for *any*
    VIDEO-sourced request regardless of target, so the video-video Phase
    1 manager (built for video<->video fades, not mixed-media
    video<->audio, which already fully owns this boundary) can claim the
    request too and never hand back to _next_track(), which is the one
    piece that would actually notice the already-skipped row and move on
    to whatever queued item follows it.
    """
    request_manual_next_calls = []
    manager = SimpleNamespace(
        request_manual_next=lambda media_type: request_manual_next_calls.append(media_type) or True,
        observe_position=lambda *a: None,
        cancel=lambda *a, **kw: True,
    )
    window = _window(
        current_path="outgoing.mp4",
        queue=["outgoing.mp4", "incoming.mp3", "third.mp3"],
        queue_played=[True, False, False],
        track_index_by_path={"outgoing.mp4": 0, "incoming.mp3": 1, "third.mp3": 2},
        _current_media_type=MediaType.VIDEO,
        _video_transition_manager=manager,
    )
    worker = _load_video_to_audio(window, monkeypatch)
    worker.prepared.emit(worker.token, "incoming.mp3", _FakePreparedCandidate())
    assert window._mixed_transition_state == "active"

    window.next_track()

    assert request_manual_next_calls == [], (
        "a Video->Audio mixed transition already owns this boundary and "
        "was just cancelled by Manual Next -- handing the request to "
        "the video-video Phase 1 manager too risks it silently "
        "swallowing the request without _next_track() ever running"
    )
    # incoming.mp3 was correctly skipped (already-played, by the mixed
    # system's own intentional design -- Manual Next during an active
    # overlap is a user-initiated skip, exactly like Next during an
    # ordinary A-A crossfade), and _next_track() genuinely ran and moved
    # on to whatever queued item follows it (still eligible for another
    # Video->Audio mixed transition, since _current_media_type is still
    # VIDEO and "third.mp3" is audio) -- proving this is a real
    # continuation, not just "nothing crashed".
    assert window.queue_played == [True, True, False]
    assert window._mixed_transition_state == "preparing"
    assert window._mixed_transition_incoming_path == "third.mp3"


# ---------------------------------------------------------------------------
# Queue identity across a full mixed 6-track queue (acceptance section 8:
# "the visibly selected next track must be the track that starts")
# ---------------------------------------------------------------------------

def test_manual_queue_identity_preserved_across_full_mixed_six_track_queue(monkeypatch):
    """AUDIO A -> VIDEO B -> AUDIO C -> VIDEO D -> [VIDEO E, handled entirely
    by the separate video-video GPU engine, out of scope here and already
    covered by the real IPC-level dual-deck tests] -> AUDIO F. At every hop
    this drives, snapshots which row _next_unplayed_queue_row() reports as
    "visibly next" *before* requesting the transition, then asserts the
    transition that actually gets prepared targets that exact row/path --
    never a different one, which is exactly the class of bug real
    acceptance testing found (a video-video preload silently stealing the
    row a Video->Audio mixed transition already owned)."""
    monkeypatch.setattr(window_module, "BassStreamPrepareWorker", _FakePlayerLoadWorker)
    monkeypatch.setattr(window_module, "_BassEngine", _FakeBassEngine)
    clock = _patch_clock(monkeypatch)
    queue = ["A.mp3", "B.mp4", "C.flac", "D.mp4", "E.mp4", "F.flac"]
    window = _window(
        current_path="A.mp3",
        current_index=0,
        queue=list(queue),
        queue_played=[True, False, False, False, False, False],
        track_index_by_path={path: i for i, path in enumerate(queue)},
        _current_media_type=MediaType.AUDIO,
    )

    def _visible_next_row():
        return window._next_unplayed_queue_row()

    # -- A (audio, current) -> B (video): Audio->Video.
    expected_row = _visible_next_row()
    assert expected_row == 1 and queue[expected_row] == "B.mp4"
    window._next_track("quiet-end")
    assert window._mixed_transition_direction == "audio_to_video"
    assert window._mixed_transition_incoming_row == expected_row
    assert window._mixed_transition_incoming_path == queue[expected_row]
    window._on_video_started()
    _drive_ticks(window, clock, 500)
    assert window._current_media_type == MediaType.VIDEO
    assert window._mixed_transition_state == "idle"
    assert window.queue_played == [True, True, False, False, False, False]

    # -- B (video, current) -> C (audio): Video->Audio.
    expected_row = _visible_next_row()
    assert expected_row == 2 and queue[expected_row] == "C.flac"
    window._video_backend._duration_ms = 100_000
    window._video_backend._position_ms = 100_000 - 6500
    window._tick()
    worker = window._mixed_transition_load_worker
    assert worker is not None
    assert window._mixed_transition_incoming_row == expected_row
    assert window._mixed_transition_incoming_path == queue[expected_row]
    worker.prepared.emit(worker.token, queue[expected_row], _FakePreparedCandidate())
    _drive_ticks(window, clock, 500)
    assert window._current_media_type == MediaType.AUDIO
    assert window._mixed_transition_state == "idle"
    assert window.queue_played == [True, True, True, False, False, False]

    # -- C (audio, current) -> D (video): Audio->Video, via Manual Next
    # this time (not quiet-end) -- the "visibly next" guarantee must hold
    # for a user-initiated Next exactly as it does for an automatic one.
    expected_row = _visible_next_row()
    assert expected_row == 3 and queue[expected_row] == "D.mp4"
    window.next_track()
    assert window._mixed_transition_direction == "audio_to_video"
    assert window._mixed_transition_incoming_row == expected_row
    assert window._mixed_transition_incoming_path == queue[expected_row]
    window._on_video_started()
    _drive_ticks(window, clock, 500)
    assert window._current_media_type == MediaType.VIDEO
    assert window._mixed_transition_state == "idle"
    assert window.queue_played == [True, True, True, True, False, False]

    # -- D -> E is a plain video-video GPU dual-deck transition, owned
    # entirely by VideoTransitionManager/the dual-deck engine (never
    # _next_track's mixed-media branch) -- out of scope for this fixture,
    # already covered end-to-end against the real subprocess by
    # test_video_backend_gpu_fixture_integration.py. Fast-forward past it
    # exactly as that engine would have (mark played, advance current).
    window.queue_played[4] = True
    window.current_path = "E.mp4"
    window.current_index = 4
    assert window._current_media_type == MediaType.VIDEO  # unchanged, still video

    # -- E (video, current) -> F (audio): Video->Audio, completing the
    # full A/B/C/D/E/F pattern's every mixed-relevant hop.
    expected_row = _visible_next_row()
    assert expected_row == 5 and queue[expected_row] == "F.flac"
    window._video_backend._duration_ms = 100_000
    window._video_backend._position_ms = 100_000 - 6500
    window._tick()
    worker = window._mixed_transition_load_worker
    assert worker is not None
    assert window._mixed_transition_incoming_row == expected_row
    assert window._mixed_transition_incoming_path == queue[expected_row]
    worker.prepared.emit(worker.token, queue[expected_row], _FakePreparedCandidate())
    _drive_ticks(window, clock, 500)
    assert window._current_media_type == MediaType.AUDIO
    assert window._mixed_transition_state == "idle"
    assert window.queue_played == [True, True, True, True, True, True]


# ---------------------------------------------------------------------------
# Real-device regression (2026-08-28): video-video dual-transition timing
# identity race ("Baby Baby") -- a video promoted as logically current by a
# GPU dual-deck transition inherited the OUTGOING deck's stale near-end
# position/duration, which _maybe_prepare_mixed_transition_from_video()
# read as "this video is about to end", triggering an immediate,
# unwanted Video->Audio mixed transition a few seconds into playback.
# Root cause and fix live in video_backend.py's commit_dual_transition()
# (see tests/test_video_transition_identity_watchdog.py's
# test_outgoing_deck_position_and_duration_getters_reset_at_commit for the
# direct proof against the real backend); this test covers the window.py
# consumer side -- _maybe_prepare_mixed_transition_from_video() must
# correctly treat "not yet available" (duration<=0, what the fixed
# backend now reports during the ambiguous promotion window) as "wait",
# not "the video is over", and must resume normal near-end behaviour once
# genuine incoming timing arrives.
# ---------------------------------------------------------------------------

def test_baby_baby_stale_outgoing_timing_does_not_trigger_premature_near_end(monkeypatch):
    monkeypatch.setattr(window_module, "BassStreamPrepareWorker", _FakePlayerLoadWorker)
    monkeypatch.setattr(window_module, "_BassEngine", _FakeBassEngine)
    window = _window(
        current_path="Baby Baby.mp4",
        queue=["Baby Baby.mp4", "Ed Sheeran - Galway Girl.flac"],
        queue_played=[True, False],
        track_index_by_path={
            "Baby Baby.mp4": 0, "Ed Sheeran - Galway Girl.flac": 1,
        },
        _current_media_type=MediaType.VIDEO,
    )

    # State 1: Baby Baby has just been logically promoted (current_path/
    # _current_media_type already reflect it), but its own deck's timing
    # has not yet arrived -- the fixed video_backend.py reports this as
    # duration_ms=0 (see commit_dual_transition()), never as the outgoing
    # deck's real near-end values (position=219052, duration=220840 in the
    # captured incident). Ticking repeatedly during this window must never
    # trigger anything.
    window._video_backend._duration_ms = 0
    window._video_backend._position_ms = 0
    for _ in range(5):
        window._tick()
    assert window._mixed_transition_state == "idle", (
        "no timing yet available must never be read as near-end"
    )
    assert window.queue_played == [True, False], "queue must not advance"

    # State 2: genuine incoming timing arrives -- Baby Baby's own real
    # position/duration (position ~1369ms, duration 232896ms in the
    # captured incident, matching video_transition_analysis_complete's
    # later report). Far from its own near-end; must still not trigger.
    window._video_backend._duration_ms = 232896
    window._video_backend._position_ms = 1369
    window._tick()
    assert window._mixed_transition_state == "idle"
    assert window.queue_played == [True, False]
    assert window.current_path == "Baby Baby.mp4"  # remains current, plays normally

    # State 3: Baby Baby now genuinely approaches its own real end (well
    # within crossfade_seconds + PREBUFFER_MS of its *actual* 232896ms
    # duration) -- only now may the normal Video->Audio near-end trigger
    # fire, and it must target the genuinely-next queued track.
    window._video_backend._position_ms = 232896 - 4000
    window._tick()
    assert window._mixed_transition_state == "preparing"
    assert window._mixed_transition_direction == "video_to_audio"
    assert window._mixed_transition_incoming_path == "Ed Sheeran - Galway Girl.flac"


# ---------------------------------------------------------------------------
# Real QThread ownership (2026-08-31 Codex audit, section 13; updated for
# Phase C1, 2026-09-10): the fake worker above is instant/synchronous by
# design -- it cannot exercise genuine QThread start/run/finished timing
# at all. These tests use the real billsmusic.workers.BassStreamPrepareWorker
# and a real QApplication event loop to prove the worker-lifetime-registry
# ownership fix holds under actual OS-thread scheduling, not just a
# hand-driven simulation of it. Phase C1 changed what the worker's run()
# actually calls (BassPlayer.prepare_stream(source), a static method that
# never touches a live player) -- prepare_stream itself is monkeypatched
# to a slow, holdable fake here so these tests keep controlling exactly
# when the real background thread's run() proceeds, without needing a
# real bass.dll.
# ---------------------------------------------------------------------------

import time as _time
from PyQt6 import QtWidgets as _QtWidgets
from billsmusic.bass_player import BassPlayer as _RealBassPlayer
from billsmusic.workers import BassStreamPrepareWorker as _RealPlayerLoadWorker

_REAL_APP = None


def _real_app():
    global _REAL_APP
    _REAL_APP = _QtWidgets.QApplication.instance() or _QtWidgets.QApplication([])
    return _REAL_APP


def _pump_real(predicate, seconds=5.0):
    app = _real_app()
    deadline = _time.monotonic() + seconds
    while _time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        _time.sleep(0.005)
    return predicate()


def _install_slow_prepare_stream(monkeypatch):
    """Monkeypatches BassPlayer.prepare_stream (a @staticmethod, so this
    is visible to whichever real background thread calls it) ONCE, to a
    fake that can be held per-source via register_hold(source, event) --
    each source string gets its own threading.Event to block on, so
    multiple concurrently-running real workers can be independently
    controlled without racing a single shared patch target (re-patching
    prepare_stream mid-test is NOT safe: a real background thread that
    hasn't yet reached the BassPlayer.prepare_stream(...) attribute
    lookup inside run() would pick up whichever version is current at
    that moment, not the one that was live when .start() was called)."""
    calls = []
    holds = {}

    def _prepare(source):
        calls.append(source)
        hold = holds.get(source)
        if hold is not None:
            hold.wait(timeout=5.0)
        return _FakePreparedCandidate(source)

    monkeypatch.setattr(_RealBassPlayer, "prepare_stream", staticmethod(_prepare))

    def register_hold(source, event):
        holds[source] = event

    return calls, register_hold


def test_real_qthread_stale_worker_finishing_after_fresh_one_preserves_ownership(monkeypatch):
    """A starts (held) -> logically cancelled via Manual Next -> B starts
    and completes -> A is released and finishes -> B's ownership of
    _mixed_transition_load_worker must survive A's real, delayed finished
    signal arriving last."""
    import threading

    _real_app()
    window = _video_current_window()
    calls, register_hold = _install_slow_prepare_stream(monkeypatch)
    hold_a = threading.Event()
    register_hold("incoming-a.mp3", hold_a)

    worker_a = _RealPlayerLoadWorker("incoming-a.mp3", 1)
    window._mixed_transition_load_worker = worker_a
    registry_token_a = window._worker_registry.register(
        "mixed_transition_load", thread=worker_a, wait_ms=1500,
    )
    worker_a.finished.connect(
        lambda w=worker_a, t=registry_token_a: window._on_mixed_transition_load_worker_finished(w, t)
    )
    worker_a.start()
    assert _pump_real(lambda: worker_a.isRunning()), "worker A never actually started running"

    # Manual Next logically supersedes A -- a fresh worker B is dispatched
    # for the same still-unplayed target, taking ownership of the slot
    # while A is still genuinely blocked inside prepare_stream(). B uses
    # its own source string (distinct hold event, registered up front --
    # see _install_slow_prepare_stream), so it stays genuinely in flight
    # (not yet finished) while A's finished signal arrives -- the exact
    # ordering the real bug needed, without racing a shared patch target.
    hold_b = threading.Event()
    register_hold("incoming-b.mp3", hold_b)
    worker_b = _RealPlayerLoadWorker("incoming-b.mp3", 2)
    window._mixed_transition_load_worker = worker_b
    registry_token_b = window._worker_registry.register(
        "mixed_transition_load", thread=worker_b, wait_ms=1500,
    )
    worker_b.finished.connect(
        lambda w=worker_b, t=registry_token_b: window._on_mixed_transition_load_worker_finished(w, t)
    )
    worker_b.start()
    assert _pump_real(lambda: worker_b.isRunning()), "worker B never actually started running"
    assert window._mixed_transition_load_worker is worker_b

    # Release A -- its real thread completes and its real finished signal
    # fires while B is still genuinely running and un-finished.
    hold_a.set()
    assert _pump_real(lambda: worker_a.isFinished()), "worker A never finished"
    # Let the queued finished-signal delivery actually reach the slot.
    _pump_real(lambda: False, seconds=0.2)  # give the event loop a beat

    assert window._mixed_transition_load_worker is worker_b, (
        "A's real, delayed finished signal must not erase B's ownership"
    )
    assert not worker_b.isFinished(), "test setup invariant: B must still be in flight here"

    # Now release B -- its own finished signal must correctly clear its
    # own ownership and unregister from the worker-lifetime registry.
    hold_b.set()
    assert _pump_real(lambda: window._mixed_transition_load_worker is None), (
        "B's own finished signal must still correctly clear ownership"
    )
    assert _pump_real(lambda: window._worker_registry.active_count() == 0)
    assert "incoming-a.mp3" in calls
    assert "incoming-b.mp3" in calls


def test_real_qthread_current_worker_finishing_clears_ownership_and_unregisters(monkeypatch):
    _real_app()
    window = _video_current_window()
    _install_slow_prepare_stream(monkeypatch)
    worker = _RealPlayerLoadWorker("incoming.mp3", 1)
    window._mixed_transition_load_worker = worker
    registry_token = window._worker_registry.register(
        "mixed_transition_load", thread=worker, wait_ms=1500,
    )
    worker.finished.connect(
        lambda w=worker, t=registry_token: window._on_mixed_transition_load_worker_finished(w, t)
    )
    prepared = []
    worker.prepared.connect(lambda token, path, candidate: prepared.append((token, path, candidate)))

    worker.start()
    assert _pump_real(lambda: bool(prepared)), "prepared never arrived"
    assert _pump_real(lambda: window._mixed_transition_load_worker is None), (
        "the current (only) worker's own finished signal must clear ownership"
    )
    assert window._worker_registry.active_count() == 0, (
        "finished cleanup must unregister from the worker-lifetime registry too"
    )
    # Phase C1 structural requirement: the real worker never held a
    # player reference at all -- only source/token.
    assert not hasattr(worker, "player")


def test_real_qthread_shutdown_while_worker_active_bound_waits_not_terminates(monkeypatch):
    """Mirrors the v1.0.66 worker-registry convention this fix adopts:
    shutdown_all() cooperatively bound-waits a genuinely running worker
    rather than calling QThread.terminate() on it."""
    import threading

    _real_app()
    window = _video_current_window()
    hold = threading.Event()
    calls, register_hold = _install_slow_prepare_stream(monkeypatch)
    register_hold("incoming.mp3", hold)
    worker = _RealPlayerLoadWorker("incoming.mp3", 1)
    window._mixed_transition_load_worker = worker
    registry_token = window._worker_registry.register(
        "mixed_transition_load", thread=worker, wait_ms=2000,
    )
    worker.finished.connect(
        lambda w=worker, t=registry_token: window._on_mixed_transition_load_worker_finished(w, t)
    )
    worker.start()
    assert _pump_real(lambda: worker.isRunning())

    # Release the hold from a real background thread just after
    # shutdown_all() starts waiting, so the bound wait(ms) genuinely has to
    # block for a moment rather than the worker already being done --
    # proves this is a real join, not a no-op because the thread had
    # already finished. A QTimer can't do this: shutdown_all()'s wait()
    # blocks this thread synchronously, so the Qt event loop a QTimer
    # depends on never gets a chance to run until wait() itself returns.
    threading.Timer(0.15, hold.set).start()
    window._worker_registry.shutdown_all()

    assert not worker.isRunning()
    assert calls == ["incoming.mp3"]
    assert window._worker_registry.active_count() == 0
