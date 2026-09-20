"""Astra F2: Pause must freeze automatic progression until Resume.

Pause suspended the audible backends, but not the machinery that advances the
session: _tick() still evaluated end/near-end/quiet-end against the paused
position, the fade timer kept driving A-A crossfades and mixed-media fades on
wall-clock time, a preparation result delivered while paused started and
promoted its incoming media, and pause() only paused one side of a mixed
Audio<->Video overlap.

These tests drive the real pause()/Resume toggle, _tick(), _fade_tick(), the
real crossfade and mixed-transition callbacks, _next_track/next_track,
_play_path_direct and the queue claim / PlaybackAttempt machinery, with inert
recording players (the harnesses of test_queue_dispatch_failure.py and
test_mixed_media_audio_video_transitions.py).
"""
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import billsmusic.window as window_module
from billsmusic.config import FADE_QUIET_FRAMES
from billsmusic.media_type import MediaType
from billsmusic.playback_attempt import PlaybackAttemptState
from billsmusic.window import PlayerWindow
from billsmusic.worker_registry import WorkerLifetimeRegistry

import test_mixed_media_audio_video_transitions as mixed_harness
import test_queue_dispatch_failure as queue_harness
from test_mixed_media_audio_video_transitions import (
    _FakeBassEngine, _FakePlayerLoadWorker, _FakePreparedCandidate, _drive_ticks,
    _load_video_to_audio, _patch_clock, _recorded_ops, _video_current_window,
    _with_real_stop_playback,
)


@pytest.fixture(autouse=True)
def _no_real_gain_lookup_threads(monkeypatch):
    monkeypatch.setattr(window_module, "GainLookupWorker", mixed_harness._InertGainLookupWorker)


def _bind(window, *names):
    for name in names:
        setattr(window, name, getattr(PlayerWindow, name).__get__(window))


def _transport_ui(window):
    window.beat = SimpleNamespace(setPlaying=lambda playing: None)
    window.btn_pause = SimpleNamespace(setText=lambda t: None, setAccessibleName=lambda t: None)
    window._announce_accessible_status = lambda message: None


# -- local audio: _tick-driven advancement ------------------------------------

class _PausableQueuePlayer(queue_harness._Player):
    """queue_harness's inert player with the built-in backends' pause/resume
    contract (a paused player is not playing and keeps its position)."""

    _paused = False

    def play(self):
        super().play()
        self._paused = False

    def pause(self):
        if self.playing:
            self.playing, self._paused = False, True

    def resume(self):
        if self._paused:
            self.playing, self._paused = True, False

    def stop(self):
        super().stop()
        self._paused = False

    def commit_prepared(self, candidate):
        self.loaded_path, self.pos = candidate.path, 0.0
        return True

    def slide_volume(self, value, seconds):
        pass


def _audio_window(monkeypatch, tmp_path, mode="normal", names=("b.mp3",)):
    window = queue_harness._window(monkeypatch, tmp_path, list(names), current="a.mp3")
    for player in (window.bass_player, window.bass_inactive_player):
        player.__class__ = _PausableQueuePlayer
    window.track_transition_mode = mode
    _transport_ui(window)
    _bind(window, "pause", "_built_in_players", "next_track", "_fade_tick")
    return window


def _crossfade_window(monkeypatch, tmp_path):
    """Crossfade mode with the real A-A crossfade engine; its off-thread
    prepare worker is the controllable fake the mixed harness uses."""
    monkeypatch.setattr(window_module, "BassStreamPrepareWorker", _FakePlayerLoadWorker)
    monkeypatch.setattr(window_module, "_BassEngine", _FakeBassEngine)
    window = _audio_window(monkeypatch, tmp_path, mode="crossfade")
    for key, value in dict(
        _crossfade_load_token=0, _crossfade_load_worker=None, pending_builtin_crossfade_path=None,
        pending_builtin_crossfade_index=None, pending_builtin_crossfade_quiet=False,
        _pending_crossfade_immediate=False, _worker_registry=WorkerLifetimeRegistry(),
        _maybe_resume_final_shutdown=lambda: None, _builtin_fade_generation=0, fade_waits=0,
        fade_start=0.0, quiet_count=0, _last_quiet_debug_remaining=None,
        _inactive_normalisation_gain=1.0, _gain_token_seq=0, _active_gain_token=0,
        _inactive_gain_token=0, _gain_snapshot_cache={},
    ).items():
        setattr(window, key, value)
    _bind(
        window, "_start_miniaudio_crossfade_to", "_make_target_lease", "_target_lease_still_valid",
        "_on_crossfade_load_prepared", "_on_crossfade_load_failed", "_on_crossfade_load_worker_finished",
        "_crossfade_load_is_current", "_require_current_playback_attempt", "_is_current_playback_attempt",
        "_discard_prepared_candidate",
        "_finalize_unclaimed_prepare_candidate", "_begin_builtin_fade", "_finish_miniaudio_crossfade",
        "_promote_inactive_player", "_promote_inactive_gain_slot", "_next_gain_token",
    )
    return window


def _name(path):
    return os.path.basename(str(path)) if path else None


def test_paused_track_reaching_its_end_does_not_advance_until_resumed(monkeypatch, tmp_path):
    window = _audio_window(monkeypatch, tmp_path)
    player = window.simple_player
    window.pause()
    assert window._playback_intentionally_paused is True and player._paused

    player.pos = player.length  # at (or past) the normal-end threshold
    for _ in range(20):
        window._tick()

    assert _name(window.current_path) == "a.mp3" and _name(player.loaded_path) == "a.mp3"
    assert window.activated == []  # B never became the current track
    assert window._current_playback_attempt is None  # no attempt for B
    assert window._queue_entry_claims == {}  # no reservation for B
    assert window.queue_played == [False]  # nothing committed
    assert window.pending_next is False

    window.pause()  # Resume
    window._tick()

    assert _name(window.current_path) == "b.mp3"
    attempt = window._current_playback_attempt
    assert attempt is not None and attempt.state is PlaybackAttemptState.PLAYING
    assert window.queue_played == [True]


def test_paused_inside_the_crossfade_window_does_not_start_next_until_resumed(monkeypatch, tmp_path):
    window = _crossfade_window(monkeypatch, tmp_path)
    player = window.simple_player
    window.pause()
    player.pos = player.length - 7.0  # inside crossfade_seconds + prebuffer

    for _ in range(20):
        window._tick()

    assert _name(window.current_path) == "a.mp3" and _name(player.loaded_path) == "a.mp3"
    assert window.activated == []
    assert window._current_playback_attempt is None and window._queue_entry_claims == {}
    assert window.prebuffer_active is False and window._crossfade_load_worker is None
    assert window.pending_next is False

    window.pause()  # Resume
    window._tick()

    assert window.prebuffer_active is True  # the near-end crossfade to B begins normally
    assert _name(window._crossfade_load_worker.source) == "b.mp3"
    attempt = window._current_playback_attempt
    assert attempt is not None and not attempt.is_terminal()
    assert window._queue_entry_claims == {window._queue_entry_tokens[0]: attempt.attempt_id}


def test_quiet_end_evidence_neither_accumulates_nor_fires_while_paused(monkeypatch, tmp_path):
    window = _crossfade_window(monkeypatch, tmp_path)
    window._current_rms_db = lambda: -80.0  # a paused analyzer reads silence
    player = window.simple_player
    player.pos = player.length - 15.0  # inside the quiet-end window, before near-end
    for _ in range(FADE_QUIET_FRAMES - 1):
        window._tick()
    assert window.quiet_count == FADE_QUIET_FRAMES - 1  # one frame short of the trigger

    window.pause()
    for _ in range(20):
        window._tick()

    assert window.quiet_count == FADE_QUIET_FRAMES - 1
    assert _name(window.current_path) == "a.mp3" and _name(player.loaded_path) == "a.mp3"
    assert window._current_playback_attempt is None and window._queue_entry_claims == {}
    assert window.pending_next is False and window.prebuffer_active is False

    window.pause()  # Resume: the same evidence completes the trigger
    window._tick()
    assert window.prebuffer_active is True
    assert _name(window._crossfade_load_worker.source) == "b.mp3"


def test_crossfade_prepared_while_paused_neither_starts_nor_progresses_until_resumed(monkeypatch, tmp_path):
    clock = _patch_clock(monkeypatch)
    window = _crossfade_window(monkeypatch, tmp_path)
    outgoing, incoming = window.simple_player, window.simple_inactive_player
    outgoing.pos = outgoing.length - 7.0
    window._tick()  # near-end: the crossfade to B is dispatched while playing
    worker = window._crossfade_load_worker
    b_path = window.queue[0]
    assert window.prebuffer_active is True

    window.pause()
    worker.prepared.emit(worker.token, b_path, _FakePreparedCandidate(b_path))  # result arrives paused
    _drive_ticks(window, clock, 400, step=0.05)  # 20 s of wall clock

    assert incoming.playing is False  # the prepared incoming track is held, not started
    assert window.fade_active is False
    assert window.simple_player is outgoing and _name(window.current_path) == "a.mp3"
    assert window.activated == []

    window.pause()  # Resume
    assert incoming.playing is True and window.fade_active is True
    assert _name(window.current_path) == "b.mp3"
    _drive_ticks(window, clock, 1, step=0.05)
    assert window.fade_active is True  # continues from the start, not already complete
    _drive_ticks(window, clock, 400, step=0.05)
    assert window.simple_player is incoming and window.fade_active is False
    assert window.activated == [b_path]  # promoted exactly once


def test_manual_next_while_paused_still_plays_the_next_track(monkeypatch, tmp_path):
    window = _audio_window(monkeypatch, tmp_path)
    window.pause()

    window.next_track()

    assert _name(window.current_path) == "b.mp3"
    assert window.simple_player.playing is True
    assert window._playback_intentionally_paused is False
    attempt = window._current_playback_attempt
    assert attempt is not None and attempt.state is PlaybackAttemptState.PLAYING
    assert window.queue_played == [True]


# -- mixed-media transitions ---------------------------------------------------

class _PausableMixedAudio(mixed_harness._FakeAudioPlayer):
    _paused = False

    def play(self):
        self.playing, self._paused = True, False

    def pause(self):
        if self.playing:
            self.playing, self._paused = False, True

    def resume(self):
        if self._paused:
            self.playing, self._paused = True, False

    def stop(self):
        super().stop()
        self._paused = False

    def is_playing(self):
        return self.playing


class _PausableVideo(mixed_harness._FakeVideoBackend):
    paused = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False


def _pausable(window):
    for player in (window.simple_player, window.simple_inactive_player):
        player.__class__ = _PausableMixedAudio
    window._video_backend.__class__ = _PausableVideo
    _transport_ui(window)
    _bind(window, "pause", "_built_in_players")
    return window


def _completions(window):
    return _recorded_ops(window).count("mixed_transition_completed")


def _audio_to_video_active(monkeypatch):
    clock = _patch_clock(monkeypatch)
    window = _pausable(mixed_harness._window())
    window.simple_player.playing = True  # the outgoing track is audibly playing
    window._next_track("quiet-end")
    window._on_video_started()
    _drive_ticks(window, clock, 30)  # ~15% into the 6 s overlap
    assert window._mixed_transition_state == "active"
    return clock, window


def test_active_audio_to_video_fade_freezes_while_paused_and_completes_once_after(monkeypatch):
    clock, window = _audio_to_video_active(monkeypatch)
    outgoing = window.simple_player
    state = (window._mixed_transition_state, window._current_media_type, list(window.queue_played),
             dict(window._queue_entry_claims))
    volumes = (outgoing.volume, window._video_backend.volume)

    window.pause()
    assert window._video_backend.paused is True
    assert outgoing._paused is True  # both sides of the overlap are paused
    _drive_ticks(window, clock, 1000, step=0.03)  # 30 s of wall clock

    assert (window._mixed_transition_state, window._current_media_type, list(window.queue_played),
            dict(window._queue_entry_claims)) == state
    assert (outgoing.volume, window._video_backend.volume) == volumes  # the fade did not progress
    assert outgoing.stopped is False and _completions(window) == 0

    window.pause()  # Resume
    assert window._video_backend.paused is False and outgoing.playing is True
    _drive_ticks(window, clock, 400, step=0.03)
    assert window._mixed_transition_state == "idle" and outgoing.stopped is True
    assert _completions(window) == 1
    assert window.queue_played == [True, True]


def test_active_video_to_audio_fade_freezes_while_paused_and_completes_once_after(monkeypatch):
    clock = _patch_clock(monkeypatch)
    window = _pausable(_video_current_window())
    worker = _load_video_to_audio(window, monkeypatch)
    worker.prepared.emit(worker.token, "incoming.mp3", _FakePreparedCandidate("incoming.mp3"))
    _drive_ticks(window, clock, 30)
    assert window._mixed_transition_state == "active"
    outgoing, incoming = window.simple_player, window.simple_inactive_player
    state = (window._mixed_transition_state, window._current_media_type, list(window.queue_played))
    volumes = (incoming.volume, window._video_backend.volume)

    window.pause()
    assert window._video_backend.paused is True and incoming._paused is True
    _drive_ticks(window, clock, 1000, step=0.03)

    assert (window._mixed_transition_state, window._current_media_type, list(window.queue_played)) == state
    assert (incoming.volume, window._video_backend.volume) == volumes
    assert window.simple_player is outgoing and _completions(window) == 0

    window.pause()  # Resume
    assert window._video_backend.paused is False and incoming.playing is True
    _drive_ticks(window, clock, 400, step=0.03)
    assert window._mixed_transition_state == "idle"
    assert window.simple_player is incoming and window._current_media_type == MediaType.AUDIO
    assert _completions(window) == 1
    assert window.queue_played == [True, True]


def test_long_pause_resumes_the_mixed_fade_where_it_was_paused(monkeypatch):
    clock, window = _audio_to_video_active(monkeypatch)
    window.pause()
    paused_video_volume = window._video_backend.volume
    clock.advance(600.0)  # ten minutes paused

    window.pause()  # Resume
    _drive_ticks(window, clock, 1, step=0.03)

    assert window._mixed_transition_state == "active"
    assert window._video_backend.volume < window.master_volume
    assert window._video_backend.volume == pytest.approx(paused_video_volume, abs=5.0)


def test_video_to_audio_preparation_result_while_paused_is_held_until_resumed(monkeypatch):
    clock = _patch_clock(monkeypatch)
    window = _pausable(_video_current_window())
    worker = _load_video_to_audio(window, monkeypatch)
    assert window._mixed_transition_state == "preparing"
    incoming = window.simple_inactive_player

    window.pause()
    worker.prepared.emit(worker.token, "incoming.mp3", _FakePreparedCandidate("incoming.mp3"))
    _drive_ticks(window, clock, 1000, step=0.03)

    assert window._mixed_transition_state == "preparing"  # not promoted to the overlap
    assert incoming.commit_prepared_calls == 1  # the prepared result is retained
    assert incoming.playing is False  # but the incoming audio has not started
    assert window.queue_played == [True, False]  # nothing committed
    assert window._current_media_type == MediaType.VIDEO

    window.pause()  # Resume
    assert window._mixed_transition_state == "active" and incoming.playing is True
    assert window.queue_played == [True, True]
    _drive_ticks(window, clock, 400, step=0.03)
    assert window._mixed_transition_state == "idle" and window.simple_player is incoming
    assert incoming.commit_prepared_calls == 1 and _completions(window) == 1


def test_audio_to_video_readiness_while_paused_is_held_until_resumed(monkeypatch):
    clock = _patch_clock(monkeypatch)
    window = _pausable(mixed_harness._window())
    outgoing = window.simple_player
    outgoing.playing = True
    window._next_track("quiet-end")
    assert window._mixed_transition_state == "preparing"
    token = window._queue_entry_tokens[1]

    window.pause()
    assert outgoing._paused is True and window._video_backend.paused is True
    window._on_video_started()  # the incoming video reports ready while paused
    _drive_ticks(window, clock, 1000, step=0.03)

    assert window._mixed_transition_state == "preparing"
    assert window._current_media_type == MediaType.AUDIO
    assert window.queue_played == [True, False]
    assert token in window._queue_entry_claims  # still reserved, not committed
    assert window._video_backend.load_calls == ["incoming.mp4"]

    window.pause()  # Resume
    assert window._mixed_transition_state == "active"
    assert window._current_media_type == MediaType.VIDEO
    assert outgoing.playing is True and window._video_backend.paused is False
    _drive_ticks(window, clock, 400, step=0.03)
    assert window._mixed_transition_state == "idle" and _completions(window) == 1
    assert window.queue_played == [True, True] and window._queue_entry_claims == {}
    assert window._video_backend.load_calls == ["incoming.mp4"]  # never prepared twice


def test_stop_while_paused_discards_a_held_transition(monkeypatch):
    clock = _patch_clock(monkeypatch)
    window = _with_real_stop_playback(_pausable(_video_current_window()))
    worker = _load_video_to_audio(window, monkeypatch)
    window.pause()
    worker.prepared.emit(worker.token, "incoming.mp3", _FakePreparedCandidate("incoming.mp3"))
    incoming = window.simple_inactive_player

    window.stop_playback()
    _drive_ticks(window, clock, 500)

    assert window._mixed_transition_state == "idle"
    assert window._playback_intentionally_paused is False
    assert incoming.playing is False and window.simple_player is not incoming
    assert window.queue_played == [True, False] and not window._queue_entry_claims
    assert window._video_backend.stopped is True
    assert window.activated_after_stop == [] and _completions(window) == 0


# -- ended callbacks -------------------------------------------------------------

def _record_next_track(window):
    """Stands in for the advancement _next_track performs: records the
    request and starts "the next media" (a new playback generation)."""
    requests = []

    def _next_track(reason):
        requests.append(reason)
        window._playback_generation += 1

    window._next_track = _next_track
    return requests


def test_video_end_delivered_while_paused_advances_only_after_resume(monkeypatch):
    window = _pausable(_video_current_window(track_transition_mode="normal"))
    requests = _record_next_track(window)
    window.pause()

    window._on_video_end_of_media()

    assert requests == []
    window.pause()  # Resume
    assert requests == ["video-ended"]


def test_cast_end_reported_while_paused_advances_only_after_resume(monkeypatch):
    snapshot = {"state": "idle", "idle_reason": "finished", "position": 180.0, "duration": 180.0}
    controller = SimpleNamespace(snapshot=lambda: dict(snapshot), play=lambda: None, pause=lambda: None)
    window = _pausable(mixed_harness._window(
        cast_active=True, cast_controller=controller, _cast_completion_armed=True,
        _cast_loss_reported=False, _cast_last_state="playing",
        _record_cast_clock_snapshot=lambda snapshot: None,
    ))
    requests = _record_next_track(window)
    window.pause()  # the user pauses as the receiver reports its natural end

    for _ in range(5):
        window._tick()

    assert requests == []
    window.pause()  # Resume
    window._tick()
    assert requests == ["cast-ended"]


def test_stop_while_paused_during_an_active_overlap_tears_it_down(monkeypatch):
    clock, window = _audio_to_video_active(monkeypatch)
    _with_real_stop_playback(window)
    window.pause()
    played = list(window.queue_played)

    window.stop_playback()
    _drive_ticks(window, clock, 500)

    assert window._mixed_transition_state == "idle"
    assert window._current_media_type == MediaType.AUDIO
    assert window._video_backend.stopped is True
    assert window._playback_intentionally_paused is False
    assert window.queue_played == played and not window._queue_entry_claims
    assert _completions(window) == 0


def test_manual_next_while_paused_into_a_mixed_transition_is_not_held(monkeypatch):
    clock = _patch_clock(monkeypatch)
    window = _pausable(mixed_harness._window())
    window.simple_player.playing = True
    window.pause()

    window.next_track()  # the next queue item is a video
    window._on_video_started()
    _drive_ticks(window, clock, 400, step=0.03)

    assert window._video_backend.load_calls == ["incoming.mp4"]
    assert window._mixed_transition_state == "idle" and _completions(window) == 1
    assert window._current_media_type == MediaType.VIDEO
    assert window.queue_played == [True, True]


def test_a_held_crossfade_start_cannot_begin_a_later_crossfade(monkeypatch, tmp_path):
    window = _crossfade_window(monkeypatch, tmp_path)
    outgoing = window.simple_player
    outgoing.pos = outgoing.length - 7.0
    window._tick()
    worker = window._crossfade_load_worker
    b_path = window.queue[0]
    window.pause()
    worker.prepared.emit(worker.token, b_path, _FakePreparedCandidate(b_path))  # held
    window._cancel_fade = PlayerWindow._cancel_fade.__get__(window)
    window._cancel_fade()  # the held crossfade is abandoned (as Stop does)
    window._playback_intentionally_paused = False
    outgoing.resume()  # playing again

    # A later crossfade is dispatched, and not yet prepared, when Pause/Resume happens.
    window.prebuffer_active = True
    window._crossfade_load_token += 1
    window.pending_builtin_crossfade_path = b_path
    window.pause()
    assert window._playback_intentionally_paused is True
    window.pause()  # Resume
    assert window._playback_intentionally_paused is False

    assert window.fade_active is False  # the stale held start did not begin it
    assert window.simple_inactive_player.playing is False
