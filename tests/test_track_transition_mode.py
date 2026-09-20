"""Tests for the Normal / Crossfade track transition preference.

Follows the SimpleNamespace "fake window" + real unbound PlayerWindow method
pattern used by tests/test_crossfade_player_rotation.py, and the
inspect.getsource static-check pattern used by tests/test_sleep_timer.py for
_load_user_settings/_save_user_settings (both touch too many live Qt widgets
to invoke directly against a fake window).
"""
import inspect
from types import SimpleNamespace

from billsmusic.config import (
    CROSSFADE_SECONDS,
    FADE_QUIET_FRAMES,
    FADE_TRIGGER_DB,
    NORMAL_TRANSITION_EPSILON_SECONDS,
    PREBUFFER_MS,
)
from billsmusic.media_type import MediaType
from billsmusic.window import PlayerWindow


# ---------------------------------------------------------------------------
# Settings persistence (static source checks -- see module docstring)
# ---------------------------------------------------------------------------

def test_load_user_settings_persists_track_transition_mode():
    source = inspect.getsource(PlayerWindow._load_user_settings)
    assert 'cfg.get("track_transition_mode", "crossfade")' in source
    assert "TRACK_TRANSITION_MODES" in source
    assert 'cfg.get("crossfade_seconds", CROSSFADE_SECONDS)' in source


def test_save_user_settings_persists_track_transition_mode():
    source = inspect.getsource(PlayerWindow._save_user_settings)
    assert 'cfg["track_transition_mode"]' in source
    assert 'cfg["crossfade_seconds"]' in source


# ---------------------------------------------------------------------------
# _next_track / prev_track / _play_queue_item: crossfade routing by mode
# ---------------------------------------------------------------------------

class _PlayDirectSpy:
    def __init__(self, result=True):
        self.calls = []
        self.queue_entry_tokens = []
        self.result = result

    def __call__(self, path, crossfade=False, index=None, immediate_crossfade=False,
                 identity_path=None, media_type_override=None, queue_entry_token=None,
                 queue_selection_owner=None):
        # queue_entry_token is accepted but deliberately not recorded:
        # these tests assert the crossfade/immediate shape of each call,
        # and Phase B token threading has its own dedicated coverage.
        self.queue_entry_tokens.append(queue_entry_token)
        self.calls.append(
            {"path": path, "crossfade": crossfade, "index": index,
             "immediate_crossfade": immediate_crossfade}
        )
        return self.result


def _next_track_window(mode, spy):
    window = SimpleNamespace(
        track_transition_mode=mode,
        tracks=["a.flac", "b.flac"],
        current_index=0,
        current_path="a.flac",
        queue=[],
        _playback_context_paths=[],
        _playback_fallback_paths=lambda: ["a.flac", "b.flac"],
        _current_media_type=MediaType.AUDIO,
        fade_active=False,
        prebuffer_active=False,
        sleep_timer=SimpleNamespace(is_stop_after_track=False),
        _next_unplayed_queue_row=lambda: None,
        _audio_log=lambda message: None,
        _audio_name=lambda path: path,
        _play_path_direct=spy,
        _record_track_completion=lambda reason: True,
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None),
        _mixed_transition_state="idle",
    )
    window._crossfade_eligible_for_transition = (
        lambda path: PlayerWindow._crossfade_eligible_for_transition(window, path)
    )
    return window


def test_next_track_normal_mode_cuts_immediately_no_crossfade():
    spy = _PlayDirectSpy()
    PlayerWindow._next_track(_next_track_window("normal", spy), "manual-next")
    assert spy.calls == [
        {"path": "b.flac", "crossfade": False, "index": None, "immediate_crossfade": True}
    ]


def test_next_track_crossfade_mode_crossfades_and_is_immediate():
    for reason in ("manual-next", "near-end", "quiet-end"):
        spy = _PlayDirectSpy()
        PlayerWindow._next_track(_next_track_window("crossfade", spy), reason)
        call = spy.calls[0]
        assert call["crossfade"] is True, reason
        assert call["immediate_crossfade"] is True, reason


def test_next_track_records_completion_and_honors_stop_after_track_for_normal_end():
    spy = _PlayDirectSpy()
    window = _next_track_window("normal", spy)
    window.sleep_timer = SimpleNamespace(is_stop_after_track=True)
    completed = []
    window._record_track_completion = lambda reason: completed.append(reason)
    stopped = []
    window._complete_stop_after_track = lambda reason: stopped.append(reason)
    PlayerWindow._next_track(window, "normal-end")
    assert completed == ["normal-end"]
    assert stopped == ["normal-end"]
    assert spy.calls == []  # returned early, no playback started


def _prev_track_window(mode, spy):
    window = SimpleNamespace(
        track_transition_mode=mode,
        tracks=["a.flac", "b.flac"],
        current_index=1,
        current_path="b.flac",
        queue=[],
        _playback_context_paths=[],
        _playback_fallback_paths=lambda: ["a.flac", "b.flac"],
        _current_media_type=MediaType.AUDIO,
        fade_active=False,
        prebuffer_active=False,
        _previous_played_queue_row=lambda: None,
        _audio_log=lambda message: None,
        _audio_name=lambda path: path,
        _play_path_direct=spy,
        _announce_accessible_status=lambda message: None,
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None),
        _mixed_transition_state="idle",
    )
    window._crossfade_eligible_for_transition = (
        lambda path: PlayerWindow._crossfade_eligible_for_transition(window, path)
    )
    return window


def test_prev_track_normal_mode_cuts_immediately_no_crossfade():
    spy = _PlayDirectSpy()
    PlayerWindow.prev_track(_prev_track_window("normal", spy))
    assert spy.calls == [
        {"path": "a.flac", "crossfade": False, "index": None, "immediate_crossfade": True}
    ]


def test_prev_track_crossfade_mode_crossfades():
    spy = _PlayDirectSpy()
    PlayerWindow.prev_track(_prev_track_window("crossfade", spy))
    call = spy.calls[0]
    assert call["crossfade"] is True
    assert call["immediate_crossfade"] is True


def _queue_item_window(mode, spy, row=0):
    item = object()
    window = SimpleNamespace(
        track_transition_mode=mode,
        queue_list=SimpleNamespace(row=lambda i: row),
        queue=["a.flac", "b.flac"],
        queue_played=[False, False],
        _current_media_type=MediaType.AUDIO,
        fade_active=False,
        prebuffer_active=False,
        _ensure_queue_played_flags=lambda: None,
        _audio_log=lambda message: None,
        _audio_name=lambda path: path,
        _play_path_direct=spy,
        _mark_queue_row_played=lambda row: None,
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None),
    )
    window._crossfade_eligible_for_transition = (
        lambda path: PlayerWindow._crossfade_eligible_for_transition(window, path)
    )
    return window, item


def test_play_queue_item_normal_mode_cuts_immediately_no_crossfade():
    spy = _PlayDirectSpy()
    window, item = _queue_item_window("normal", spy)
    PlayerWindow._play_queue_item(window, item)
    assert spy.calls == [
        {"path": "a.flac", "crossfade": False, "index": None, "immediate_crossfade": True}
    ]


def test_play_queue_item_crossfade_mode_crossfades():
    spy = _PlayDirectSpy()
    window, item = _queue_item_window("crossfade", spy)
    PlayerWindow._play_queue_item(window, item)
    assert spy.calls[0]["crossfade"] is True


# ---------------------------------------------------------------------------
# _tick: mode-gated trigger logic (built-in backend branch)
# ---------------------------------------------------------------------------

class _FakeTickPlayer:
    def __init__(self, length, pos):
        self._length = length
        self._pos = pos

    def get_length(self):
        return self._length

    def get_pos(self):
        return self._pos


def _tick_window(mode, remaining, rms_db, next_track_calls, quiet_count=0):
    length = 100.0
    pos = length - remaining
    return SimpleNamespace(
        track_transition_mode=mode,
        _current_media_type=MediaType.AUDIO,
        _sync_mini_player=lambda: None,
        _sync_party_mode=lambda: None,
        _update_recently_played_tracking=lambda: None,
        _lyrics_tick=lambda: None,
        _sync_karaoke_position=lambda: None,
        _check_playback_health=lambda: None,
        _use_builtin_player=lambda: True,
        _use_bass_backend=lambda: False,
        simple_player=_FakeTickPlayer(length, pos),
        _current_rms_db=lambda: rms_db,
        _last_quiet_debug_remaining=None,
        quiet_count=quiet_count,
        fade_active=False,
        prebuffer_active=False,
        pending_next=False,
        pending_builtin_crossfade_quiet=False,
        crossfade_seconds=CROSSFADE_SECONDS,
        _backend_label=lambda: "BASS",
        _audio_log=lambda message: None,
        _next_track=lambda reason: next_track_calls.append(reason),
        scrubbing=False,
        _update_progress=lambda a, b: None,
        _mixed_transition_state="idle",
        _maybe_prepare_mixed_transition_from_video=lambda: None,
    )


def test_tick_quiet_end_only_triggers_in_crossfade_mode():
    for mode, expect_trigger in (("crossfade", True), ("normal", False)):
        calls = []
        # remaining kept well above the near-end/normal-end thresholds so
        # only the quiet-end path could plausibly fire here.
        window = _tick_window(
            mode, remaining=20.0, rms_db=FADE_TRIGGER_DB - 1.0,
            next_track_calls=calls, quiet_count=FADE_QUIET_FRAMES - 1,
        )
        PlayerWindow._tick(window)
        if expect_trigger:
            assert calls == ["quiet-end"], mode
        else:
            assert calls == [], mode


def test_tick_never_reads_stale_audio_position_while_video_is_current():
    # Regression: a stopped-but-not-reloaded simple_player can still report
    # the *previous* track's near-the-end position/length, which used to
    # make _tick() fire _next_track() within the first tick or two of video
    # playback (video plays for under a second, then goes blank).
    calls = []
    window = _tick_window(
        "crossfade", remaining=0.05, rms_db=FADE_TRIGGER_DB - 1.0,
        next_track_calls=calls, quiet_count=FADE_QUIET_FRAMES,
    )
    window._current_media_type = MediaType.VIDEO
    PlayerWindow._tick(window)
    assert calls == []


def test_tick_near_end_triggers_only_in_crossfade_mode():
    prebuffer_sec = PREBUFFER_MS / 1000.0
    remaining = CROSSFADE_SECONDS + prebuffer_sec - 0.1  # just inside the trigger window
    for mode, expect_trigger in (("crossfade", True), ("normal", False)):
        calls = []
        window = _tick_window(mode, remaining=remaining, rms_db=None, next_track_calls=calls)
        PlayerWindow._tick(window)
        if expect_trigger:
            assert calls == ["near-end"], mode
        else:
            assert calls == [], mode


def test_tick_normal_end_triggers_only_in_normal_mode():
    remaining = NORMAL_TRANSITION_EPSILON_SECONDS - 0.05
    calls = []
    window = _tick_window("normal", remaining=remaining, rms_db=None, next_track_calls=calls)
    PlayerWindow._tick(window)
    assert calls == ["normal-end"]

    # Crossfade at the same tiny remaining still advances, but via its own
    # near-end path, not the normal-mode epsilon check.
    calls = []
    window = _tick_window("crossfade", remaining=remaining, rms_db=None, next_track_calls=calls)
    PlayerWindow._tick(window)
    assert calls == ["near-end"]


# ---------------------------------------------------------------------------
# Stop cancels an in-flight crossfade regardless of where it is
# ---------------------------------------------------------------------------

class _FakeStoppablePlayer:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def test_stop_playback_cancels_an_in_flight_crossfade_prebuffer():
    simple_player = _FakeStoppablePlayer()
    simple_inactive_player = _FakeStoppablePlayer()
    window = SimpleNamespace(
        track_transition_mode="crossfade",
        _current_media_type=MediaType.AUDIO,
        fade_active=True,
        prebuffer_active=True,
        pending_next=True,
        fade_waits=3,
        pending_builtin_crossfade_index=1,
        pending_builtin_crossfade_path="next.flac",
        pending_builtin_crossfade_quiet=False,
        simple_player=simple_player,
        simple_inactive_player=simple_inactive_player,
        active_player=None,
        inactive_player=None,
        _cancel_playback_watchdog=lambda: None,
        _playback_expected=True,
        _playback_intentionally_paused=False,
        beat=SimpleNamespace(setPlaying=lambda playing: None),
        btn_pause=SimpleNamespace(setText=lambda text: None, setAccessibleName=lambda name: None),
        _reset_progress=lambda: None,
        _announce_accessible_status=lambda message: None,
    )
    # Use the real (unbound) cancel/stop methods so this exercises the same
    # cancellation path stop_playback relies on in the live app.
    window._cancel_fade = lambda: PlayerWindow._cancel_fade(window)
    window._stop_all = lambda: PlayerWindow._stop_all(window)
    window._stop_video_for_audio_transition = (
        lambda: PlayerWindow._stop_video_for_audio_transition(window)
    )
    window._resume_deferred_queue_analysis = lambda: None
    window._deferred_bpm_key_paths = set()
    window._sync_now_playing_overlay_for_media_type = (
        lambda: PlayerWindow._sync_now_playing_overlay_for_media_type(window)
    )
    window._current_playback_attempt = None
    window._cancel_current_playback_attempt = (
        lambda reason: PlayerWindow._cancel_current_playback_attempt(window, reason)
    )

    PlayerWindow.stop_playback(window)

    assert window.fade_active is False
    assert window.prebuffer_active is False
    assert window.pending_next is False
    assert simple_player.stopped is True
    assert simple_inactive_player.stopped is True


# ---------------------------------------------------------------------------
# ReplayGain/normalisation untouched: gain combination lines are unmodified
# ---------------------------------------------------------------------------

def test_begin_builtin_fade_gain_combination_unchanged():
    source = inspect.getsource(PlayerWindow._begin_builtin_fade)
    assert "self._inactive_normalisation_gain" in source
    assert "self.master_volume / 100.0" in source
    assert "self._sleep_timer_gain" in source
    assert "self.crossfade_seconds" in source


# ---------------------------------------------------------------------------
# The incoming player must start flowing immediately (not deferred until the
# calculated swap moment), so device/buffer startup latency doesn't land
# right at the crossfade boundary.
# ---------------------------------------------------------------------------

class _FakeCrossfadePlayer:
    def __init__(self, length=0.0, pos=0.0, physical_id=""):
        self._length = length
        self._pos = pos
        self.played = False
        self.loaded_path = None
        self.physical_id = physical_id
        self.commit_prepared_calls = 0

    def stop(self):
        pass

    def load(self, path):
        self.loaded_path = path

    def commit_prepared(self, candidate):
        # Phase C1: mirrors BassPlayer.commit_prepared/
        # MiniaudioPlayer.commit_prepared's public contract closely enough
        # for these tests -- adopts the candidate's path and reports
        # success.
        self.commit_prepared_calls += 1
        self.loaded_path = getattr(candidate, "path", self.loaded_path)
        return True

    def set_volume(self, value):
        pass

    def play(self):
        self.played = True

    def is_playing(self):
        return self.played

    def get_length(self):
        return self._length

    def get_pos(self):
        return self._pos

    def stats(self):
        return {"duration": self._length, "sample_rate": 44100, "channels": 2}


class _FakeSignal:
    def __init__(self):
        self.callback = None

    def connect(self, callback):
        self.callback = callback

    def emit(self, *args):
        if self.callback:
            self.callback(*args)


class _FakeLoadWorker:
    """Stand-in for billsmusic.workers.BassStreamPrepareWorker -- lets
    tests control exactly when the "background" file load completes
    instead of depending on real OS thread scheduling. Phase C1: takes
    only source/token -- no player instance, matching the real worker's
    contract."""
    created = []

    def __init__(self, source, token):
        self.source = source
        self.token = token
        self._prepared_candidate = None
        self.prepared = _FakeSignal()
        _real_emit = self.prepared.emit

        def _emit_and_capture(tok, path, candidate):
            self._prepared_candidate = candidate
            _real_emit(tok, path, candidate)

        self.prepared.emit = _emit_and_capture
        self.failed = _FakeSignal()
        self.finished = _FakeSignal()
        self.started = False
        _FakeLoadWorker.created.append(self)

    def start(self):
        self.started = True

    def claim_candidate(self):
        candidate = self._prepared_candidate
        self._prepared_candidate = None
        return candidate


class _FakePreparedCandidate:
    def __init__(self, path=None):
        self.path = path
        self.discarded = False

    def discard(self):
        self.discarded = True
        return True


class _FakeBassEngine:
    @staticmethod
    def ensure():
        return None


def _crossfade_window(outgoing, incoming, **overrides):
    outgoing.physical_id = outgoing.physical_id or "bass-A"
    incoming.physical_id = incoming.physical_id or "bass-B"
    window = SimpleNamespace(
        track_transition_mode="crossfade",
        crossfade_seconds=CROSSFADE_SECONDS,
        simple_player=outgoing,
        simple_inactive_player=incoming,
        bass_player=outgoing,
        bass_inactive_player=incoming,
        miniaudio_player=None,
        miniaudio_inactive_player=None,
        builtin_backend="bass",
        prebuffer_active=False,
        fade_waits=0,
        pending_next=True,
        pending_builtin_crossfade_index=None,
        pending_builtin_crossfade_path=None,
        pending_builtin_crossfade_quiet=False,
        _builtin_fade_generation=0,
        _crossfade_load_worker=None,
        _crossfade_load_token=0,
        _pending_crossfade_immediate=False,
        track_index_by_path={},
        _backend_label=lambda: "BASS",
        _current_backend_name=lambda: "bass",
        _use_bass_backend=lambda: True,
        _use_builtin_player=lambda: True,
        _gain_for_path=lambda path: 1.0,
        _cached_gain_for_path=lambda path, target="active": 1.0,
        _audio_log=lambda message: None,
        _audio_name=lambda path: path,
        _activate_track_ui=lambda path, *, library_index=None, queue_token=None: None,
        _begin_playback_recovery=lambda *a, **k: None,
        _current_playback_attempt=None,
        _is_current_playback_attempt=lambda attempt_id: True,
        _require_current_playback_attempt=lambda attempt_id, stage: True,
        _advance_playback_attempt_state=lambda attempt_id, state: None,
        # Phase C1: real _player_topology_epoch, so this must start
        # initialised the same way production is.
        _player_topology_epoch=0,
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None, path_details=lambda p: {}),
        _closing=False,
        _maybe_resume_final_shutdown=lambda: None,
    )
    from billsmusic.worker_registry import WorkerLifetimeRegistry
    window._worker_registry = WorkerLifetimeRegistry()
    for key, value in overrides.items():
        setattr(window, key, value)
    window._crossfade_load_is_current = (
        lambda token, path: PlayerWindow._crossfade_load_is_current(window, token, path)
    )
    window._fail_pending_crossfade = (
        lambda backend, path, error: PlayerWindow._fail_pending_crossfade(window, backend, path, error)
    )
    window._on_crossfade_load_prepared = (
        lambda token, path, candidate, attempt_id=None, lease=None: PlayerWindow._on_crossfade_load_prepared(
            window, token, path, candidate, attempt_id, lease,
        )
    )
    window._on_crossfade_load_failed = (
        lambda token, path, error, attempt_id=None: PlayerWindow._on_crossfade_load_failed(
            window, token, path, error, attempt_id,
        )
    )
    # Phase C1: real, unbound so the actual topology/lease logic is
    # exercised, not just its absence papered over.
    window._set_player_topology = (
        lambda active, inactive, reason: PlayerWindow._set_player_topology(window, active, inactive, reason=reason)
    )
    window._promote_inactive_player = (
        lambda reason: PlayerWindow._promote_inactive_player(window, reason=reason)
    )
    window._make_target_lease = (
        lambda backend_family, target_role: PlayerWindow._make_target_lease(window, backend_family, target_role)
    )
    window._target_lease_still_valid = (
        lambda lease: PlayerWindow._target_lease_still_valid(window, lease)
    )
    window._discard_prepared_candidate = (
        lambda candidate, stage: PlayerWindow._discard_prepared_candidate(window, candidate, stage)
    )
    # Phase C2 (worker lifetime / shutdown ownership, 2026-09-11):
    # _start_miniaudio_crossfade_to now registers with
    # WorkerLifetimeRegistry and routes its finished handler through
    # these real, unbound methods too.
    window._finalize_unclaimed_prepare_candidate = (
        lambda w, stage: PlayerWindow._finalize_unclaimed_prepare_candidate(window, w, stage)
    )
    window._on_crossfade_load_worker_finished = (
        lambda w, t: PlayerWindow._on_crossfade_load_worker_finished(window, w, t)
    )
    return window


def test_start_miniaudio_crossfade_to_dispatches_load_without_blocking(monkeypatch):
    # player.load() reads/scans the file header -- fast on a local disk but
    # sometimes many seconds on a network share, which used to freeze the
    # whole window since it ran directly on the GUI thread here. It must
    # now be dispatched to a worker instead of called inline.
    scheduled = []
    monkeypatch.setattr(
        "billsmusic.window.QtCore.QTimer.singleShot",
        lambda ms, cb: scheduled.append((ms, cb)),
    )
    _FakeLoadWorker.created = []
    monkeypatch.setattr("billsmusic.window.BassStreamPrepareWorker", _FakeLoadWorker)
    monkeypatch.setattr("billsmusic.window._BassEngine", _FakeBassEngine)
    outgoing = _FakeCrossfadePlayer(length=200.0, pos=193.0)  # remaining = 7s
    incoming = _FakeCrossfadePlayer()
    window = _crossfade_window(outgoing, incoming)

    result = PlayerWindow._start_miniaudio_crossfade_to(window, "next.flac", immediate=False)

    assert result is True
    assert window.prebuffer_active is True
    assert window.pending_builtin_crossfade_path == "next.flac"
    # Not playing yet -- load() hasn't actually run (no real thread here).
    assert incoming.played is False
    assert incoming.loaded_path is None
    assert len(_FakeLoadWorker.created) == 1
    worker = _FakeLoadWorker.created[0]
    assert worker.started is True
    assert worker.source == "next.flac"
    assert window._crossfade_load_worker is worker


def test_incoming_player_starts_once_the_load_completes(monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        "billsmusic.window.QtCore.QTimer.singleShot",
        lambda ms, cb: scheduled.append((ms, cb)),
    )
    _FakeLoadWorker.created = []
    monkeypatch.setattr("billsmusic.window.BassStreamPrepareWorker", _FakeLoadWorker)
    monkeypatch.setattr("billsmusic.window._BassEngine", _FakeBassEngine)
    outgoing = _FakeCrossfadePlayer(length=200.0, pos=193.0)  # remaining = 7s
    incoming = _FakeCrossfadePlayer()
    window = _crossfade_window(outgoing, incoming)

    PlayerWindow._start_miniaudio_crossfade_to(window, "next.flac", immediate=False)
    worker = _FakeLoadWorker.created[0]
    worker.prepared.emit(worker.token, "next.flac", _FakePreparedCandidate())

    assert incoming.played is True
    # The ramp start is still deferred via a timer (7s lead has plenty of
    # slack for a fixed-duration crossfade) -- unchanged from before.
    assert len(scheduled) == 1


def test_immediate_crossfade_begins_fade_as_soon_as_load_completes(monkeypatch):
    monkeypatch.setattr(
        "billsmusic.window.QtCore.QTimer.singleShot",
        lambda ms, cb: (_ for _ in ()).throw(AssertionError("should not defer when immediate")),
    )
    _FakeLoadWorker.created = []
    monkeypatch.setattr("billsmusic.window.BassStreamPrepareWorker", _FakeLoadWorker)
    monkeypatch.setattr("billsmusic.window._BassEngine", _FakeBassEngine)
    fade_calls = []
    outgoing = _FakeCrossfadePlayer(length=200.0, pos=0.0)
    incoming = _FakeCrossfadePlayer()
    window = _crossfade_window(
        outgoing, incoming,
        _begin_builtin_fade=lambda generation=None: fade_calls.append(generation),
    )

    PlayerWindow._start_miniaudio_crossfade_to(window, "next.flac", immediate=True)
    worker = _FakeLoadWorker.created[0]
    worker.prepared.emit(worker.token, "next.flac", _FakePreparedCandidate())

    assert incoming.played is True
    assert fade_calls == [1]


def test_stale_load_result_is_discarded_without_playing():
    # The user stopped playback (or requested another track) while this
    # load was still in flight -- _cancel_fade()-equivalent state reset
    # already happened, so the eventual result must not resurrect it.
    outgoing = _FakeCrossfadePlayer(length=200.0, pos=193.0)
    incoming = _FakeCrossfadePlayer()
    window = _crossfade_window(
        outgoing, incoming,
        _crossfade_load_token=5, prebuffer_active=False,
        pending_builtin_crossfade_path=None,
    )
    candidate = _FakePreparedCandidate("next.flac")
    PlayerWindow._on_crossfade_load_prepared(window, 5, "next.flac", candidate)
    assert incoming.played is False
    assert candidate.discarded is True


def test_superseded_load_result_is_discarded():
    outgoing = _FakeCrossfadePlayer(length=200.0, pos=193.0)
    incoming = _FakeCrossfadePlayer()
    window = _crossfade_window(
        outgoing, incoming,
        _crossfade_load_token=2,  # a newer dispatch has since happened
        prebuffer_active=True,
        pending_builtin_crossfade_path="next.flac",
    )
    candidate = _FakePreparedCandidate("next.flac")
    PlayerWindow._on_crossfade_load_prepared(window, 1, "next.flac", candidate)  # stale token
    assert incoming.played is False
    assert candidate.discarded is True


def test_load_failure_triggers_playback_recovery_for_the_new_track():
    recovery_calls = []
    outgoing = _FakeCrossfadePlayer(length=200.0, pos=193.0)
    incoming = _FakeCrossfadePlayer()
    activate_calls = []
    window = _crossfade_window(
        outgoing, incoming,
        _crossfade_load_token=1, prebuffer_active=True,
        pending_builtin_crossfade_path="next.flac",
        pending_builtin_crossfade_index=7,
        _activate_track_ui=(
            lambda path, *, library_index=None, queue_token=None:
            activate_calls.append((library_index, path))
        ),
        _begin_playback_recovery=lambda *a, **k: recovery_calls.append((a, k)),
    )
    PlayerWindow._on_crossfade_load_failed(window, 1, "next.flac", "disk error")

    assert window.prebuffer_active is False
    assert window.pending_builtin_crossfade_path is None
    assert incoming.played is False
    assert activate_calls == [(7, "next.flac")]
    assert recovery_calls[0][0][0] == "crossfade-failed"
    assert recovery_calls[0][1]["path"] == "next.flac"


def test_stale_load_failure_is_ignored():
    recovery_calls = []
    outgoing = _FakeCrossfadePlayer(length=200.0, pos=193.0)
    incoming = _FakeCrossfadePlayer()
    window = _crossfade_window(
        outgoing, incoming,
        _crossfade_load_token=9,  # a newer dispatch has since happened
        _begin_playback_recovery=lambda *a, **k: recovery_calls.append((a, k)),
    )
    PlayerWindow._on_crossfade_load_failed(window, 1, "next.flac", "disk error")
    assert recovery_calls == []


def test_second_dispatch_is_ignored_while_a_load_is_already_in_flight(monkeypatch):
    _FakeLoadWorker.created = []
    monkeypatch.setattr("billsmusic.window.BassStreamPrepareWorker", _FakeLoadWorker)
    monkeypatch.setattr("billsmusic.window._BassEngine", _FakeBassEngine)
    outgoing = _FakeCrossfadePlayer(length=200.0, pos=193.0)
    incoming = _FakeCrossfadePlayer()
    window = _crossfade_window(outgoing, incoming)

    first = PlayerWindow._start_miniaudio_crossfade_to(window, "next.flac")
    second = PlayerWindow._start_miniaudio_crossfade_to(window, "another.flac")

    assert first is True
    assert second is True  # "already in progress" is reported the same as "started"
    assert len(_FakeLoadWorker.created) == 1


def test_start_crossfade_to_vlc_schedules_fixed_delay(monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        "billsmusic.window.QtCore.QTimer.singleShot",
        lambda ms, cb: scheduled.append((ms, cb)),
    )
    window = SimpleNamespace(
        track_transition_mode="crossfade",
        prebuffer_active=False,
        fade_from=None,
        fade_waits=0,
        active_player=SimpleNamespace(get_length=lambda: 200000.0, get_time=lambda: 193000.0),
        inactive_player=SimpleNamespace(),
        pending_next=True,
        _set_volume=lambda player, value: None,
        _play_on_player=lambda player, path, volume_scale: True,
        _begin_fade=lambda: None,
    )

    PlayerWindow._start_crossfade_to(window, "next.flac")

    assert len(scheduled) == 1
    delay_ms, _callback = scheduled[0]
    assert delay_ms == PREBUFFER_MS
