"""Astra F6: leaving a temporary recovery backend must not orphan the
physical player that was playing on it.

Failure sequence on the pre-fix code: configured BASS fails to open, the
real recovery ladder switches the logical roles to miniaudio and starts
the track there, then the user picks another local track.
_play_path_direct repoints simple_player/simple_inactive_player back to
BASS *before* its _stop_all(), and _stop_all() only reaches whatever the
roles currently reference -- so the miniaudio stream kept playing,
unreachable, underneath the new BASS track (and Stop could no longer
reach it either).

Drives the real PlayerWindow methods (_play_path_direct, _play_simple,
_begin_playback_recovery, _try_recovery_backend, _set_player_topology,
_promote_inactive_player, _stop_all, stop_playback) on a SimpleNamespace
fake window with inert physical players that record their own state.
"""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import billsmusic.window as window_module
from billsmusic.media_type import MediaType
from billsmusic.window import PlayerWindow


class _FakePhysicalPlayer:
    def __init__(self, physical_id, fail_loads=0):
        self.physical_id = physical_id
        self.fail_loads = fail_loads
        self.playing = False
        self.loaded_path = None
        self.stop_calls = 0

    def load(self, path):
        if self.fail_loads > 0:
            self.fail_loads -= 1
            raise RuntimeError(f"{self.physical_id} open failed")
        self.loaded_path = path

    def play(self):
        if self.loaded_path is not None:
            self.playing = True

    def stop(self):
        self.stop_calls += 1
        self.playing = False

    def is_playing(self):
        return self.playing

    def set_volume(self, value):
        pass

    def seek(self, seconds):
        pass

    def stats(self):
        return {"duration": 180.0, "sample_rate": 44100, "channels": 2}

    def commit_prepared(self, candidate):
        self.loaded_path = candidate.path
        return True

    def get_pos(self):
        return 0.0


BOUND_METHODS = (
    "_play_path_direct", "_play_simple", "_begin_playback_recovery",
    "_try_recovery_backend", "_set_player_topology", "_promote_inactive_player",
    "_stop_all", "stop_playback", "_backend_label", "_use_builtin_player",
    "_use_bass_backend", "_configured_preferred_backend",
    "_available_recovery_backends", "_record_playback_backend_failure",
    "_begin_playback_attempt", "_cancel_current_playback_attempt",
    "_advance_playback_attempt_state", "_arm_playback_watchdog",
    "_cancel_playback_watchdog", "_stop_video_for_audio_transition",
)


def _window(monkeypatch, tmp_path):
    monkeypatch.setattr(window_module, "VLC_AVAILABLE", False)
    first = tmp_path / "first.mp3"
    second = tmp_path / "second.mp3"
    for path in (first, second):
        path.write_bytes(b"x")
    # BASS fails the initial open AND the same-backend recovery retry, so
    # the real recovery ladder moves on to miniaudio.
    bass_a = _FakePhysicalPlayer("bass-A", fail_loads=2)
    bass_b = _FakePhysicalPlayer("bass-B")
    mini_a = _FakePhysicalPlayer("miniaudio-A")
    mini_b = _FakePhysicalPlayer("miniaudio-B")
    window = SimpleNamespace(
        bass_player=bass_a, bass_inactive_player=bass_b,
        miniaudio_player=mini_a, miniaudio_inactive_player=mini_b,
        simple_player=None, simple_inactive_player=None,
        active_player=None, inactive_player=None,
        _player_topology_epoch=0,
        builtin_backend="bass", use_simple=True, _simple_fallback_active=False,
        _temporary_backend_override=None,
        auto_playback_recovery=True, allow_backend_fallback=True,
        _playback_recovery_active=False, _playback_recovery_attempts={},
        _backend_failure_times={}, _backend_quarantined_until={},
        _playback_generation=0, _playback_expected=False,
        _playback_intentionally_paused=False,
        _current_playback_attempt=None, _next_playback_attempt_id=1,
        _current_media_type=MediaType.AUDIO, _mixed_transition_state="idle",
        _video_transition_manager=None, _closing=False, cast_active=False,
        current_path=None, current_index=None, track_index_by_path={},
        pending_next=False, master_volume=100, _sleep_timer_gain=1.0,
        _active_normalisation_gain=1.0, _lyric_idx=None,
        diagnostics=SimpleNamespace(
            record=lambda *a, **kw: None, path_details=lambda path: {},
        ),
        statusBar=lambda: SimpleNamespace(showMessage=lambda *a, **kw: None),
        beat=SimpleNamespace(setPlaying=lambda playing: None),
        btn_pause=SimpleNamespace(setText=lambda t: None, setAccessibleName=lambda t: None),
        _audio_log=lambda message: None,
        _audio_name=lambda path: os.path.basename(path),
        _cached_gain_for_path=lambda path, target=None: 1.0,
        _set_playing_button_state=lambda: None,
        _cancel_fade=lambda: None,
        _reset_progress=lambda: None,
        _resume_deferred_queue_analysis=lambda: None,
        _sync_now_playing_overlay_for_media_type=lambda: None,
        _announce_accessible_status=lambda message: None,
    )
    window._activate_track_ui = (
        lambda path, *, library_index=None, queue_token=None:
        setattr(window, "current_path", path)
    )
    for name in BOUND_METHODS:
        setattr(window, name, getattr(PlayerWindow, name).__get__(window))
    # Startup pins the configured BASS pair, as the real window does.
    window._set_player_topology(bass_a, bass_b, reason="startup")
    return window, str(first), str(second)


def _physical(window):
    return (
        window.bass_player, window.bass_inactive_player,
        window.miniaudio_player, window.miniaudio_inactive_player,
    )


def test_returning_from_recovery_backend_stops_the_orphaned_recovery_player(monkeypatch, tmp_path):
    window, first, second = _window(monkeypatch, tmp_path)
    bass_a, bass_b, mini_a, mini_b = _physical(window)

    # Configured BASS fails; the real recovery path switches to miniaudio.
    assert window._play_path_direct(first) is True
    assert window._temporary_backend_override == "miniaudio"
    assert window.simple_player is mini_a
    assert mini_a.playing is True and mini_a.loaded_path == first
    assert not bass_a.playing

    # A normal local selection returns playback to the configured backend.
    assert window._play_path_direct(second) is True

    assert window._temporary_backend_override is None
    assert window.simple_player is bass_a
    assert window.simple_inactive_player is bass_b
    assert bass_a.playing is True and bass_a.loaded_path == second
    # The retiring recovery stream must not be left audible and unreachable.
    assert mini_a.playing is False
    assert [p for p in _physical(window) if p.playing] == [bass_a]

    window.stop_playback()
    assert not any(p.playing for p in _physical(window))


def test_entering_recovery_keeps_the_recovery_player_it_just_started(monkeypatch, tmp_path):
    """configured -> recovery: the retirement stop in _set_player_topology
    runs before the recovery backend loads, so it never touches the
    newly-active player."""
    window, first, _second = _window(monkeypatch, tmp_path)
    bass_a, bass_b, mini_a, mini_b = _physical(window)

    window._play_path_direct(first)

    assert window.simple_player is mini_a and window.simple_inactive_player is mini_b
    assert [p for p in _physical(window) if p.playing] == [mini_a]
    window.stop_playback()
    assert not any(p.playing for p in _physical(window))


def test_crossfade_promotion_keeps_the_incoming_player_alive(monkeypatch, tmp_path):
    window, _first, _second = _window(monkeypatch, tmp_path)
    outgoing, incoming = window.bass_player, window.bass_inactive_player
    outgoing.loaded_path, incoming.loaded_path = "out.mp3", "in.mp3"
    outgoing.play()
    incoming.play()
    epoch_before = window._player_topology_epoch

    window._promote_inactive_player(reason="crossfade_complete")

    assert window.simple_player is incoming and window.simple_inactive_player is outgoing
    assert window.bass_player is incoming and window.bass_inactive_player is outgoing
    assert window._player_topology_epoch == epoch_before + 1
    assert incoming.playing is True and incoming.stop_calls == 0
    assert outgoing.stop_calls == 0  # retiring the outgoing side stays the fade's job


def test_repinning_the_same_pair_stops_nothing(monkeypatch, tmp_path):
    window, _first, _second = _window(monkeypatch, tmp_path)
    active = window.simple_player
    active.loaded_path = "x.mp3"
    active.play()

    window._set_player_topology(window.bass_player, window.bass_inactive_player, reason="play_path_direct")

    assert active.playing is True and active.stop_calls == 0


# ---------------------------------------------------------------------------
# Phase 1.1: promotion must never relabel one backend's physical players as
# the other backend's canonical pair.
# ---------------------------------------------------------------------------

class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class _HeldPrepareWorker:
    """Stand-in for Bass/MiniaudioSourcePrepareWorker: never runs, the test
    delivers the prepared candidate itself."""
    instances = []

    def __init__(self, source, token):
        self.source, self.token = source, token
        self.prepared, self.failed, self.finished = _Signal(), _Signal(), _Signal()
        self._candidate = None
        _HeldPrepareWorker.instances.append(self)

    def start(self):
        pass

    def deliver(self):
        self._candidate = SimpleNamespace(path=self.source, discard=lambda: True)
        self.prepared.emit(self.token, self.source, self._candidate)

    def claim_candidate(self):
        candidate, self._candidate = self._candidate, None
        return candidate


class _FakeVideoBackend:
    def __init__(self):
        self.stopped = False

    def load(self, path):
        self.stopped = False
        return True

    def stop(self):
        self.stopped = True

    def set_volume(self, value):
        pass

    def set_muted(self, muted):
        pass

    def query_audio_state(self, checkpoint, transition_id):
        return True


MIXED_METHODS = (
    "_play_video_path_direct", "_next_mixed_transition_id",
    "_reset_mixed_media_transition_state", "_begin_mixed_media_transition",
    "_prepare_mixed_transition_video_to_audio", "_on_mixed_transition_audio_load_prepared",
    "_on_mixed_transition_audio_load_failed",
    "_activate_mixed_media_transition", "_mixed_transition_tick",
    "_finish_mixed_transition_video_to_audio", "_make_target_lease",
    "_target_lease_still_valid", "_discard_prepared_candidate",
    "_promote_inactive_gain_slot", "_next_gain_token",
    "_record_mixed_transition_gap_checkpoint", "_probe_mixed_video_audio_state",
    "_on_mixed_transition_load_worker_finished", "_finalize_unclaimed_prepare_candidate",
    "_fade_tick", "_finish_miniaudio_crossfade",
)


def _with_mixed_media(window, monkeypatch, tmp_path):
    from billsmusic.worker_registry import WorkerLifetimeRegistry

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    _HeldPrepareWorker.instances = []
    monkeypatch.setattr(window_module, "MiniaudioSourcePrepareWorker", _HeldPrepareWorker)
    monkeypatch.setattr(window_module, "BassStreamPrepareWorker", _HeldPrepareWorker)
    monkeypatch.setattr(window_module, "_BassEngine", None)
    clock = SimpleNamespace(now=1_000_000.0)
    monkeypatch.setattr(window_module.time, "time", lambda: clock.now)
    for name, value in dict(
        _video_backend=_FakeVideoBackend(), video_playback_enabled=True,
        _dual_transition_promoted_path=None, _muted=False,
        _video_fullscreen=False, _video_progress_started_at=0.0,
        _video_progress_warning_reported=False, _video_timing_available_reported=False,
        label_remaining=SimpleNamespace(setText=lambda text: None),
        _show_video_loading_page=lambda: None, _show_normal_display_page=lambda: None,
        _detach_video_from_party_mode=lambda: None, _exit_video_fullscreen=lambda: None,
        _dual_transition_committed_state_value=lambda: None,
        _mixed_transition_id=0, _mixed_transition_direction=None,
        _mixed_transition_outgoing_path=None, _mixed_transition_incoming_path=None,
        _mixed_transition_incoming_token=None, _mixed_transition_reason=None,
        _mixed_transition_start=None, _mixed_transition_video_audio_scale=0.0,
        _mixed_transition_gain_token=None, _mixed_transition_requested_monotonic=None,
        _mixed_transition_load_worker=None, _mixed_transition_audio_probe_done=set(),
        _worker_registry=WorkerLifetimeRegistry(), _maybe_resume_final_shutdown=lambda: None,
        crossfade_seconds=6.0, fade_active=False, prebuffer_active=False,
        _inactive_normalisation_gain=1.0, _gain_token_seq=0,
        _active_gain_token=0, _inactive_gain_token=0, _gain_snapshot_cache={},
        queue=[], _arm_playback_watchdog=lambda position: None,
        pending_builtin_crossfade_path=None, pending_builtin_crossfade_index=None,
        pending_builtin_crossfade_quiet=False,
    ).items():
        setattr(window, name, value)
    for name in MIXED_METHODS:
        setattr(window, name, getattr(PlayerWindow, name).__get__(window))
    return str(video), clock


def _labels(window):
    return {
        "bass": (window.bass_player, window.bass_inactive_player),
        "miniaudio": (window.miniaudio_player, window.miniaudio_inactive_player),
    }


def _assert_labels_keep_their_physical_family(window, bass_objects, mini_objects):
    labels = _labels(window)
    assert set(labels["bass"]) == set(bass_objects), "BASS labels no longer hold the BASS players"
    assert set(labels["miniaudio"]) == set(mini_objects), "miniaudio labels no longer hold the miniaudio players"
    for family, pair in labels.items():
        for player in pair:
            assert player.physical_id.startswith(f"{family}-")


def test_recovery_video_to_audio_promotion_keeps_backend_labels_on_their_own_players(monkeypatch, tmp_path):
    """Real route to a promotion on a recovery topology: BASS fails ->
    miniaudio recovery -> a video is selected (the video path keeps the
    recovery override and roles) -> the Video->Audio mixed transition
    prepares on, and promotes within, the miniaudio pair."""
    window, first, second = _window(monkeypatch, tmp_path)
    bass_a, bass_b, mini_a, mini_b = _physical(window)
    video, clock = _with_mixed_media(window, monkeypatch, tmp_path)

    window._play_path_direct(first)
    assert window._temporary_backend_override == "miniaudio"
    assert window._play_path_direct(video) is True
    assert window._current_media_type == MediaType.VIDEO
    assert window._temporary_backend_override == "miniaudio"
    assert window.simple_player is mini_a and window.simple_inactive_player is mini_b

    assert window._begin_mixed_media_transition("video_to_audio", None, second, "near-end") is True
    worker = _HeldPrepareWorker.instances[-1]
    worker.deliver()
    assert window._mixed_transition_state == "active"
    assert mini_b.playing is True
    clock.now += 60.0
    window._fade_tick()
    assert window._mixed_transition_state == "idle"

    assert window.simple_player is mini_b and window.simple_inactive_player is mini_a
    assert (window.miniaudio_player, window.miniaudio_inactive_player) == (mini_b, mini_a)
    assert (window.bass_player, window.bass_inactive_player) == (bass_a, bass_b)
    _assert_labels_keep_their_physical_family(window, (bass_a, bass_b), (mini_a, mini_b))


def test_recovery_crossfade_promotion_keeps_backend_labels_on_their_own_players(monkeypatch, tmp_path):
    """Same invariant through the ordinary crossfade completion seam while
    the recovery topology is live: the incoming miniaudio player is
    promoted, and neither backend's canonical pair is renamed."""
    window, first, _second = _window(monkeypatch, tmp_path)
    bass_a, bass_b, mini_a, mini_b = _physical(window)
    _with_mixed_media(window, monkeypatch, tmp_path)
    window._play_path_direct(first)
    mini_b.loaded_path = "incoming.mp3"
    mini_b.play()  # incoming side of the fade
    window.fade_active = window.prebuffer_active = True
    window.pending_builtin_crossfade_path = "incoming.mp3"

    window._finish_miniaudio_crossfade()

    assert window.simple_player is mini_b and mini_b.playing is True
    assert (window.miniaudio_player, window.miniaudio_inactive_player) == (mini_b, mini_a)
    assert (window.bass_player, window.bass_inactive_player) == (bass_a, bass_b)
    _assert_labels_keep_their_physical_family(window, (bass_a, bass_b), (mini_a, mini_b))


def test_normal_bass_crossfade_still_rotates_the_bass_pair(monkeypatch, tmp_path):
    window, _first, _second = _window(monkeypatch, tmp_path)
    bass_a, bass_b, mini_a, mini_b = _physical(window)
    _with_mixed_media(window, monkeypatch, tmp_path)
    bass_b.loaded_path = "incoming.mp3"
    bass_b.play()

    window._finish_miniaudio_crossfade()

    assert (window.simple_player, window.simple_inactive_player) == (bass_b, bass_a)
    assert (window.bass_player, window.bass_inactive_player) == (bass_b, bass_a)
    assert (window.miniaudio_player, window.miniaudio_inactive_player) == (mini_a, mini_b)
    assert bass_b.playing is True


def test_normal_miniaudio_crossfade_still_rotates_the_miniaudio_pair(monkeypatch, tmp_path):
    window, _first, _second = _window(monkeypatch, tmp_path)
    bass_a, bass_b, mini_a, mini_b = _physical(window)
    _with_mixed_media(window, monkeypatch, tmp_path)
    window.builtin_backend = "miniaudio"
    window._set_player_topology(mini_a, mini_b, reason="backend_switch")
    mini_b.loaded_path = "incoming.mp3"
    mini_b.play()

    window._finish_miniaudio_crossfade()

    assert (window.simple_player, window.simple_inactive_player) == (mini_b, mini_a)
    assert (window.miniaudio_player, window.miniaudio_inactive_player) == (mini_b, mini_a)
    assert (window.bass_player, window.bass_inactive_player) == (bass_a, bass_b)
    assert mini_b.playing is True


def test_configured_miniaudio_bass_recovery_promotion_keeps_labels_and_returns_to_miniaudio(monkeypatch, tmp_path):
    window, first, second = _window(monkeypatch, tmp_path)
    bass_a, bass_b, mini_a, mini_b = _physical(window)
    _with_mixed_media(window, monkeypatch, tmp_path)
    window.builtin_backend = "miniaudio"
    window._set_player_topology(mini_a, mini_b, reason="backend_switch")
    bass_a.fail_loads = 0
    mini_a.fail_loads = 2  # initial open and same-backend retry fail -> BASS recovery

    window._play_path_direct(first)
    assert window._temporary_backend_override == "bass"
    assert (window.simple_player, window.simple_inactive_player) == (bass_a, bass_b)
    bass_b.loaded_path = "incoming.mp3"
    bass_b.play()
    window._finish_miniaudio_crossfade()

    assert (window.bass_player, window.bass_inactive_player) == (bass_b, bass_a)
    assert (window.miniaudio_player, window.miniaudio_inactive_player) == (mini_a, mini_b)
    _assert_labels_keep_their_physical_family(window, (bass_a, bass_b), (mini_a, mini_b))

    assert window._play_path_direct(second) is True
    assert (window.simple_player, window.simple_inactive_player) == (mini_a, mini_b)
    assert mini_a.playing is True and bass_b.playing is False
    lease = window._make_target_lease("bass" if window._use_bass_backend() else "miniaudio", "inactive")
    assert lease.backend_family == "miniaudio" and window._target_lease_still_valid(lease)
    window.stop_playback()
    assert not any(p.playing for p in (bass_a, bass_b, mini_a, mini_b))


def test_return_to_configured_bass_after_recovery_promotion_uses_real_bass_players(monkeypatch, tmp_path):
    window, first, second = _window(monkeypatch, tmp_path)
    bass_a, bass_b, mini_a, mini_b = _physical(window)
    video, clock = _with_mixed_media(window, monkeypatch, tmp_path)
    window._play_path_direct(first)
    window._play_path_direct(video)
    window._begin_mixed_media_transition("video_to_audio", None, second, "near-end")
    _HeldPrepareWorker.instances[-1].deliver()
    clock.now += 60.0
    window._fade_tick()
    assert mini_b.playing is True  # the promoted recovery-backend track

    third = tmp_path / "third.mp3"
    third.write_bytes(b"x")
    assert window._play_path_direct(str(third)) is True

    assert window._temporary_backend_override is None
    assert (window.simple_player, window.simple_inactive_player) == (bass_a, bass_b)
    assert bass_a.playing is True and bass_a.loaded_path == str(third)
    assert mini_b.playing is False  # retired by the F6 seam
    _assert_labels_keep_their_physical_family(window, (bass_a, bass_b), (mini_a, mini_b))
    # A BASS preparation dispatched now must pass lease/backend validation.
    lease = window._make_target_lease("bass" if window._use_bass_backend() else "miniaudio", "inactive")
    assert lease.backend_family == "bass"
    assert window._target_lease_still_valid(lease) is True

    window.stop_playback()
    assert not any(p.playing for p in (bass_a, bass_b, mini_a, mini_b))
