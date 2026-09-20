"""Phase 7 -- queue and playback state consistency.

Up Next, the current track, the playback attempt and the reservations must
agree with what is actually playing, whatever the source.

Defect: Plex video committed its Up Next entry at dispatch. _start_plex_video_
playback advanced its attempt straight to PLAYING before the video subprocess
had loaded anything, so a Plex video that never played was marked played (and
relocated to the bottom of Up Next), and its reservation was released as a
success. Local video commits from _on_video_started -- when the subprocess
reports it is genuinely playing -- and Plex video now does the same.

The rest of these tests are guards for sequences audited and found already
consistent: crossfaded advancement, Previous, re-selecting a played row,
Up Next edits while a row is reserved, and a reserved row removed mid-flight.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

import billsmusic.window as window_module
from billsmusic.media_type import MediaType
from billsmusic.window import PlayerWindow
from billsmusic.playback_attempt import PlaybackAttemptState
from billsmusic.plex_transport import PlexTransportSource

import test_pause_freezes_progression as f2
import test_pause_authority_inflight as p41
import test_playback_transition_authority as p5
import test_plex_stage3a_dispatch as plex_harness


def _state(window):
    return dict(
        current=os.path.basename(str(window.current_path)),
        played={os.path.basename(p): v for p, v in zip(window.queue, window.queue_played)},
        order=[os.path.basename(p) for p in window.queue],
        claims=dict(window._queue_entry_claims),
        commits=list(window.commits),
    )


# -- video: local and Plex must commit on the same event ----------------------------

class _FakeVideoBackend:
    def __init__(self):
        self.loaded = []
        self.stopped = False

    def load(self, path, **kwargs):
        self.loaded.append(path)
        return True

    def set_volume(self, value):
        pass

    def set_muted(self, muted):
        pass

    def is_dual_mode(self):
        return False

    def set_dual_mode(self, mode):
        pass

    def stop(self):
        self.stopped = True

    def position_ms(self):
        return 0

    def duration_ms(self):
        return 0


def _video_window(monkeypatch, tmp_path, names=("v.mp4", "c.mp3")):
    window = p5._crossfade_queue_window(monkeypatch, tmp_path, names=names)
    window.track_transition_mode = "normal"
    window._video_backend = _FakeVideoBackend()
    for key, value in dict(
        video_playback_enabled=True, _video_fullscreen=False, video_start_fullscreen=False,
        _video_transition_manager=None, _video_progress_started_at=0.0,
        _video_progress_warning_reported=False, _video_timing_available_reported=False,
        _dual_transition_promoted_path=None, _muted=False, master_volume=50,
        label_remaining=SimpleNamespace(setText=lambda text: None),
        _show_video_loading_page=lambda: None, _show_video_output_page=lambda: None,
        _attach_video_to_party_mode=lambda: None, _detach_video_from_party_mode=lambda: None,
        _show_normal_display_page=lambda: None, _exit_video_fullscreen=lambda: None,
        _sync_now_playing_overlay_for_media_type=lambda: None,
        _resume_deferred_queue_analysis=lambda: None, _set_playing_button_state=lambda: None,
        _dual_transition_committed_state_value=lambda: None,
        _VIDEO_ERROR_MESSAGES=PlayerWindow._VIDEO_ERROR_MESSAGES,
    ).items():
        setattr(window, key, value)
    f2._bind(
        window, "_play_video_path_direct", "_on_video_started", "_on_video_error",
        "_stop_video_for_audio_transition", "_mixed_transition_owns_video_boundary",
    )
    return window


def _select(window, name, **kw):
    row = [os.path.basename(str(p)) for p in window.queue].index(name)
    token, owner = window_module._claim_queue_selection_for(window, row, reason="test")
    started = window._play_path_direct(window.queue[row], queue_entry_token=token,
                                       queue_selection_owner=owner, **kw)
    return started, token


def test_a_local_video_commits_only_once_it_starts(monkeypatch, tmp_path):
    window = _video_window(monkeypatch, tmp_path)

    started, token = _select(window, "v.mp4")

    assert started is True
    assert window.commits == []  # loading, not yet playing
    assert token in window._queue_entry_claims

    window._on_video_started()

    assert window.commits == ["v.mp4"]
    assert window._current_playback_attempt.state is PlaybackAttemptState.PLAYING
    assert token not in window._queue_entry_claims


def test_a_local_video_that_fails_is_not_committed(monkeypatch, tmp_path):
    window = _video_window(monkeypatch, tmp_path)
    _started, token = _select(window, "v.mp4")

    window._on_video_error("video_decode_error", "could not decode")

    assert window.commits == []
    assert window.queue_played[[os.path.basename(str(p)) for p in window.queue].index("v.mp4")] is False
    assert window._playback_expected is False


PLEX_VIDEO = plex_harness.VIDEO_IDENTITY


def _plex_video_window(monkeypatch, tmp_path):
    window = _video_window(monkeypatch, tmp_path, names=("v.mp4", "c.mp3"))
    monkeypatch.setattr(window_module, "PlexPlaybackResolveWorker", plex_harness._FakeResolveWorker)
    plex_harness._FakeResolveWorker.instances = []
    template = plex_harness.DispatchHarness()
    for key in ("plex_preferences", "_plex_active_connection_uri", "_plex_active_access_token",
                "_plex_resolve_worker", "_plex_resolve_pending_kind", "_plex_resolve_pending_index"):
        setattr(window, key, getattr(template, key))
    window.queue[0] = PLEX_VIDEO
    f2._bind(
        window, "_plex_resolved_connection_for_identity", "_plex_effective_connection",
        "_start_plex_playback_resolve", "_play_plex_video_path_direct", "_on_plex_playback_resolved",
        "_fail_plex_playback_resolve", "_terminalise_video_transition_for_failed_incoming",
        "_start_plex_video_playback", "_on_plex_resolve_worker_finished",
    )
    return window


def _resolve_plex_video(window):
    worker = plex_harness._FakeResolveWorker.instances[-1]
    source = PlexTransportSource(identity=PLEX_VIDEO, transport_url="http://plex-host:32400/part.mp4",
                                 extra_headers={"X-Plex-Token": "REAL-TOKEN"})
    worker.finished_result.emit({"success": True, "identity": PLEX_VIDEO,
                                 "generation": worker.kwargs["generation"], "transport_source": source})


def test_a_plex_video_that_never_starts_is_not_committed(monkeypatch, tmp_path):
    window = _plex_video_window(monkeypatch, tmp_path)
    row = window.queue.index(PLEX_VIDEO)
    token, owner = window_module._claim_queue_selection_for(window, row, reason="test")
    assert window._play_path_direct(window.queue[row], queue_entry_token=token, queue_selection_owner=owner)
    _resolve_plex_video(window)  # resolved: the subprocess is asked to load it
    assert window._video_backend.loaded == [PLEX_VIDEO]
    assert window.commits == []  # not playing yet

    window._on_video_error("video_resource_error", "stream unavailable")

    assert window.commits == []
    assert window.queue_played[window.queue.index(PLEX_VIDEO)] is False
    assert window._playback_expected is False


def test_a_plex_video_commits_once_it_starts(monkeypatch, tmp_path):
    window = _plex_video_window(monkeypatch, tmp_path)
    row = window.queue.index(PLEX_VIDEO)
    token, owner = window_module._claim_queue_selection_for(window, row, reason="test")
    assert window._play_path_direct(window.queue[row], queue_entry_token=token, queue_selection_owner=owner)
    _resolve_plex_video(window)

    window._on_video_started()

    assert window.commits == [os.path.basename(PLEX_VIDEO)]
    assert window._current_playback_attempt.state is PlaybackAttemptState.PLAYING
    assert token not in window._queue_entry_claims
    window._on_video_started()  # a repeated report commits nothing more
    assert window.commits == [os.path.basename(PLEX_VIDEO)]


# -- guards: sequences audited and already consistent ---------------------------------

def test_crossfaded_advancement_commits_each_entry_once_and_leaves_no_reservation(monkeypatch, tmp_path):
    clock = f2._patch_clock(monkeypatch)
    window = p5._crossfade_queue_window(monkeypatch, tmp_path, names=("b.mp3", "c.mp3", "d.mp3"))

    p5._crossfade_to_next(window, clock)  # a -> b
    p5._crossfade_to_next(window, clock)  # b -> c

    state = _state(window)
    assert state["current"] == "c.mp3"
    assert state["played"] == {"b.mp3": True, "c.mp3": True, "d.mp3": False}
    assert state["commits"] == ["b.mp3", "c.mp3"] and state["claims"] == {}


def test_previous_replays_without_duplicating_or_resurrecting_queue_entries(monkeypatch, tmp_path):
    clock = f2._patch_clock(monkeypatch)
    window = p5._crossfade_queue_window(monkeypatch, tmp_path, names=("b.mp3", "c.mp3", "d.mp3"))
    f2._bind(window, "prev_track", "_previous_played_queue_row")
    window._announce_accessible_status = lambda message: None
    p5._crossfade_to_next(window, clock)
    p5._crossfade_to_next(window, clock)
    before = _state(window)

    window.prev_track()  # crossfades back to the previously played entry
    p5._deliver_prepared(window)
    f2._drive_ticks(window, clock, 300, step=0.05)

    after = _state(window)
    assert after["current"] == "b.mp3"
    assert after["played"] == before["played"] and after["order"] == before["order"]
    assert after["commits"] == before["commits"] and after["claims"] == {}


def test_reselecting_a_played_entry_does_not_commit_it_twice(monkeypatch, tmp_path):
    clock = f2._patch_clock(monkeypatch)
    window = p5._crossfade_queue_window(monkeypatch, tmp_path, names=("b.mp3", "c.mp3"))
    p5._crossfade_to_next(window, clock)  # b played and relocated

    started, _token = _select(window, "b.mp3")

    assert started is True
    assert window.commits == ["b.mp3"]
    assert os.path.basename(str(window.current_path)) == "b.mp3"
    assert window._queue_entry_claims == {}


def test_an_up_next_reorder_while_a_row_is_reserved_commits_that_same_row(monkeypatch, tmp_path):
    clock = f2._patch_clock(monkeypatch)
    window = p5._crossfade_queue_window(monkeypatch, tmp_path, names=("b.mp3", "c.mp3", "d.mp3"))
    p5._reach_crossfade_window(window)  # b is reserved while its crossfade prepares
    reserved_token = window._current_playback_attempt.queue_entry_id

    row = [os.path.basename(p) for p in window.queue].index("b.mp3")  # the user drags it down
    window.queue.append(window.queue.pop(row))
    window.queue_played.append(window.queue_played.pop(row))
    window.queue_playlist_entries.append(window.queue_playlist_entries.pop(row))
    window._queue_entry_tokens.append(window._queue_entry_tokens.pop(row))
    p5._deliver_prepared(window)
    f2._drive_ticks(window, clock, 300, step=0.05)

    assert window.commits == ["b.mp3"]  # resolved by token, not by the old row
    assert _state(window)["played"]["b.mp3"] is True
    assert window._queue_entry_claims == {}


def test_a_reserved_entry_removed_from_up_next_is_never_committed(monkeypatch, tmp_path):
    clock = f2._patch_clock(monkeypatch)
    window = p5._crossfade_queue_window(monkeypatch, tmp_path, names=("b.mp3", "c.mp3"))
    p5._reach_crossfade_window(window)
    row = [os.path.basename(p) for p in window.queue].index("b.mp3")
    for sequence in (window.queue, window.queue_played, window.queue_playlist_entries, window._queue_entry_tokens):
        sequence.pop(row)

    p5._deliver_prepared(window)
    f2._drive_ticks(window, clock, 300, step=0.05)

    assert window.commits == []  # it is no longer an Up Next entry
    assert window._queue_entry_claims == {}
    assert os.path.basename(str(window.current_path)) == "b.mp3"  # but it is what is playing
    assert _state(window)["played"] == {"c.mp3": False}


def test_stop_leaves_no_entry_reserved_or_falsely_played(monkeypatch, tmp_path):
    clock = f2._patch_clock(monkeypatch)
    window = p5._crossfade_queue_window(monkeypatch, tmp_path, names=("b.mp3", "c.mp3"))
    p5._crossfade_to_next(window, clock)  # b is playing and committed
    p5._reach_crossfade_window(window)  # c reserved while its crossfade prepares
    reserved = window._current_playback_attempt
    reserved_token = reserved.queue_entry_id

    window.stop_playback()

    assert window._queue_entry_claims == {}  # the reservation is released
    assert reserved_token is not None
    assert _state(window)["played"] == {"b.mp3": True, "c.mp3": False}
    assert window.commits == ["b.mp3"]  # nothing was committed by stopping
    assert reserved.state is PlaybackAttemptState.CANCELLED
    assert window._current_playback_attempt is None  # nothing is authoritative after Stop
    assert window._playback_expected is False
    assert window.simple_player.playing is False and window.simple_inactive_player.playing is False


def test_a_restored_session_has_no_reservations_or_in_progress_attempt(monkeypatch, tmp_path):
    clock = f2._patch_clock(monkeypatch)
    window = p5._crossfade_queue_window(monkeypatch, tmp_path, names=("b.mp3", "c.mp3"))
    session_path = str(tmp_path / "session.json")
    monkeypatch.setattr(window_module, "session_file_path", lambda: session_path)
    f2._bind(window, "_save_session", "_load_session", "_initialise_queue_entry_tokens",
             "_allocate_queue_entry_tokens")
    window._refresh_queue_list = lambda **kw: None
    p5._crossfade_to_next(window, clock)  # b played; c still queued

    window._save_session()
    restored = p5._crossfade_queue_window(monkeypatch, tmp_path, names=("b.mp3", "c.mp3"))
    f2._bind(restored, "_load_session", "_initialise_queue_entry_tokens", "_allocate_queue_entry_tokens")
    restored._refresh_queue_list = lambda **kw: None
    restored._queue_entry_claims = {}
    restored._load_session()

    assert [os.path.basename(p) for p in restored.queue] == [os.path.basename(p) for p in window.queue]
    assert restored.queue_played == window.queue_played  # what was played stays played
    assert restored._queue_entry_claims == {}  # no reservation survives a restart
    assert restored._current_playback_attempt is None  # and no in-progress attempt
    assert len(restored._queue_entry_tokens) == len(restored.queue)
    assert len(set(restored._queue_entry_tokens)) == len(restored._queue_entry_tokens)
    assert os.path.basename(str(restored.current_path)) == "b.mp3"  # the resume point
    assert restored.dispatches == [] and restored.commits == []  # restore starts nothing
