"""Phase 5 -- playback transition authority.

Only the playback action that currently owns the transition may change what
is playing or what Up Next treats as current.

Defect: a built-in (BASS/miniaudio) crossfade never committed its queue entry.
_play_path_direct returned straight from its crossfade branch, and nothing
later in the crossfade lifecycle advanced the PlaybackAttempt to PLAYING --
the one place a queue entry is committed. The crossfaded track became the
audible, current track while its attempt stayed REQUESTED and its row stayed
claimed and unplayed; when the next track started, that attempt was
superseded, its claim released, and the row came back as unplayed -- so every
crossfaded Up Next track was played again later.

These tests drive the real _tick near-end trigger, _next_track,
_play_path_direct, the crossfade engine (_start_miniaudio_crossfade_to,
_on_crossfade_load_prepared, _begin_builtin_fade, _fade_tick,
_finish_miniaudio_crossfade), stop_playback, pause() and the queue claim /
PlaybackAttempt machinery, with inert recording players.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import billsmusic.window as window_module
from billsmusic.playback_attempt import PlaybackAttemptState
from billsmusic.window import PlayerWindow

import test_pause_freezes_progression as f2
import test_queue_dispatch_failure as queue_harness


def _crossfade_queue_window(monkeypatch, tmp_path, names=("b.mp3", "c.mp3")):
    """f2's real crossfade window, with an Up Next of `names` after a.mp3."""
    window = f2._crossfade_window(monkeypatch, tmp_path)
    paths = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"x")
        paths.append(str(path))
    window.queue = paths
    window.queue_played = [False] * len(paths)
    window.queue_playlist_entries = [None] * len(paths)
    window._queue_entry_tokens = []
    window._next_queue_entry_token = 1
    window._ensure_queue_played_flags()
    f2._bind(window, "stop_playback", "_cancel_fade", "_cancel_current_playback_attempt")
    window._sync_now_playing_overlay_for_media_type = lambda: None
    window.commits = []
    real_record = window.diagnostics.record

    def record(category, operation, **kw):
        if operation == "entry_committed":
            window.commits.append(os.path.basename(_path_for_token(window, kw["details"]["token"])))
        return real_record(category, operation, **kw)

    window.diagnostics.record = record
    window.dispatches = []
    real_play = window._play_path_direct

    def play(path, **kw):
        window.dispatches.append(os.path.basename(str(path)))
        return real_play(path, **kw)

    window._play_path_direct = play
    return window


def _path_for_token(window, token):
    return window.queue[window._queue_entry_tokens.index(token)]


def _played(window):
    return {os.path.basename(p): played for p, played in zip(window.queue, window.queue_played)}


def _reach_crossfade_window(window):
    player = window.simple_player
    player.pos = player.length - 7.0
    window._tick()  # the real near-end trigger


def _deliver_prepared(window):
    worker = window._crossfade_load_worker
    path = window.pending_builtin_crossfade_path
    worker.prepared.emit(worker.token, path, f2._FakePreparedCandidate(path))


def _crossfade_to_next(window, clock):
    """Near-end -> the crossfade to the next Up Next entry runs to completion."""
    _reach_crossfade_window(window)
    attempt = window._current_playback_attempt
    _deliver_prepared(window)
    f2._drive_ticks(window, clock, 300, step=0.05)
    assert window.fade_active is False and window.prebuffer_active is False
    return attempt


def test_a_crossfaded_queue_track_is_committed_exactly_once(monkeypatch, tmp_path):
    clock = f2._patch_clock(monkeypatch)
    window = _crossfade_queue_window(monkeypatch, tmp_path)
    _reach_crossfade_window(window)
    attempt = window._current_playback_attempt
    token = attempt.queue_entry_id
    assert _played(window)["b.mp3"] is False  # not committed while it is only preparing

    _deliver_prepared(window)  # the fade begins: B is now the audible, current track

    assert attempt.state is PlaybackAttemptState.PLAYING
    assert _played(window)["b.mp3"] is True
    assert token not in window._queue_entry_claims
    f2._drive_ticks(window, clock, 300, step=0.05)  # the crossfade completes
    assert os.path.basename(window.current_path) == "b.mp3"
    assert window.commits == ["b.mp3"]  # once, not again at completion


def test_a_crossfaded_queue_track_is_not_played_again_later(monkeypatch, tmp_path):
    clock = f2._patch_clock(monkeypatch)
    window = _crossfade_queue_window(monkeypatch, tmp_path)

    _crossfade_to_next(window, clock)  # a -> b
    _crossfade_to_next(window, clock)  # b -> c
    assert os.path.basename(window.current_path) == "c.mp3"

    window.simple_player.pos = window.simple_player.length - 7.0
    window._tick()  # c nears its end: Up Next is exhausted

    assert window.dispatches == ["b.mp3", "c.mp3"]  # b was not queued up again
    assert _played(window) == {"b.mp3": True, "c.mp3": True}
    assert window.commits == ["b.mp3", "c.mp3"]


def test_a_crossfade_superseded_before_its_fade_begins_never_commits(monkeypatch, tmp_path):
    clock = f2._patch_clock(monkeypatch)
    window = _crossfade_queue_window(monkeypatch, tmp_path)
    _reach_crossfade_window(window)
    b_worker = window._crossfade_load_worker
    b_path = window.pending_builtin_crossfade_path
    b_attempt = window._current_playback_attempt

    row = [os.path.basename(p) for p in window.queue].index("c.mp3")
    token, owner = window_module._claim_queue_selection_for(window, row, reason="test")
    assert window._play_path_direct(window.queue[row], queue_entry_token=token, queue_selection_owner=owner)
    b_worker.prepared.emit(b_worker.token, b_path, f2._FakePreparedCandidate(b_path))  # B's late result
    f2._drive_ticks(window, clock, 300, step=0.05)

    assert b_attempt.state is PlaybackAttemptState.CANCELLED
    assert _played(window) == {"b.mp3": False, "c.mp3": True}  # B stays selectable
    assert window.commits == ["c.mp3"]
    assert b_attempt.queue_entry_id not in window._queue_entry_claims
    assert os.path.basename(window.current_path) == "c.mp3"


def test_stop_before_the_fade_begins_leaves_the_track_uncommitted(monkeypatch, tmp_path):
    clock = f2._patch_clock(monkeypatch)
    window = _crossfade_queue_window(monkeypatch, tmp_path)
    _reach_crossfade_window(window)
    worker = window._crossfade_load_worker
    path = window.pending_builtin_crossfade_path

    window.stop_playback()
    worker.prepared.emit(worker.token, path, f2._FakePreparedCandidate(path))
    f2._drive_ticks(window, clock, 300, step=0.05)

    assert window.commits == [] and _played(window)["b.mp3"] is False
    assert window._queue_entry_claims == {}
    assert window.simple_inactive_player.playing is False and window.fade_active is False


def test_a_crossfade_prepared_while_paused_commits_only_when_its_fade_begins(monkeypatch, tmp_path):
    clock = f2._patch_clock(monkeypatch)
    window = _crossfade_queue_window(monkeypatch, tmp_path)
    _reach_crossfade_window(window)
    window.pause()
    _deliver_prepared(window)
    f2._drive_ticks(window, clock, 100, step=0.05)
    assert window.commits == []  # Pause holds the transition, and so its commit

    window.pause()  # Resume: the held fade begins
    assert window.commits == ["b.mp3"]
    f2._drive_ticks(window, clock, 300, step=0.05)
    assert window.commits == ["b.mp3"]


def test_stop_after_the_fade_began_keeps_the_single_commit_and_restarts_nothing(monkeypatch, tmp_path):
    clock = f2._patch_clock(monkeypatch)
    window = _crossfade_queue_window(monkeypatch, tmp_path)
    _reach_crossfade_window(window)
    _deliver_prepared(window)  # fade begun: B committed as the current track
    f2._drive_ticks(window, clock, 10, step=0.05)

    window.stop_playback()
    f2._drive_ticks(window, clock, 300, step=0.05)
    for _ in range(5):
        window._tick()

    assert window.commits == ["b.mp3"]
    assert window.fade_active is False and window.prebuffer_active is False
    assert window.simple_player.playing is False and window.simple_inactive_player.playing is False
    assert window._playback_expected is False
    assert window.dispatches == ["b.mp3"]


def test_previous_during_a_crossfade_is_ignored_and_commits_nothing(monkeypatch, tmp_path):
    clock = f2._patch_clock(monkeypatch)
    window = _crossfade_queue_window(monkeypatch, tmp_path)
    f2._bind(window, "prev_track", "_previous_played_queue_row")
    window._announce_accessible_status = lambda message: None
    _reach_crossfade_window(window)

    window.prev_track()  # while B prepares
    assert window.dispatches == ["b.mp3"] and window.commits == []
    _deliver_prepared(window)
    window.prev_track()  # while B fades in
    f2._drive_ticks(window, clock, 300, step=0.05)

    assert window.dispatches == ["b.mp3"]
    assert window.commits == ["b.mp3"]
    assert os.path.basename(window.current_path) == "b.mp3"


# -- Stop is final: _tick's end triggers after Stop ------------------------------

class _StoppablePlayer(f2._PausableQueuePlayer):
    """The built-in backends' stop() contract (BassPlayer frees its stream,
    MiniaudioPlayer rewinds): the position returns to 0 while the loaded
    length is still reported."""

    def stop(self):
        super().stop()
        self.pos = 0.0


def _short_track_window(monkeypatch, tmp_path, length):
    window = _crossfade_queue_window(monkeypatch, tmp_path)
    for player in (window.bass_player, window.bass_inactive_player):
        player.__class__ = _StoppablePlayer
        player.length = length
    return window


def test_stop_is_final_for_a_short_track_inside_the_crossfade_window(monkeypatch, tmp_path):
    clock = f2._patch_clock(monkeypatch)
    window = _short_track_window(monkeypatch, tmp_path, length=5.0)  # shorter than fade + prebuffer
    window._tick()  # playing: the near-end crossfade to B begins preparing
    assert window.dispatches == ["b.mp3"]

    window.stop_playback()
    for _ in range(20):
        window._tick()
    f2._drive_ticks(window, clock, 100, step=0.05)

    assert window.dispatches == ["b.mp3"]  # nothing started after Stop
    assert window.simple_player.playing is False and window.simple_inactive_player.playing is False
    assert window.commits == [] and window._queue_entry_claims == {}
    assert window.prebuffer_active is False and window.pending_next is False


def test_stop_is_final_for_a_short_track_whose_stopped_analyzer_reads_silence(monkeypatch, tmp_path):
    window = _short_track_window(monkeypatch, tmp_path, length=20.0)  # inside the quiet-end window
    window._current_rms_db = lambda: -80.0

    window.stop_playback()
    for _ in range(20):
        window._tick()

    assert window.dispatches == []
    assert window.quiet_count == 0


def test_a_stopped_short_track_starts_again_only_when_played(monkeypatch, tmp_path):
    window = _short_track_window(monkeypatch, tmp_path, length=5.0)
    window.stop_playback()
    window._tick()
    assert window.dispatches == []

    window.next_track()  # an explicit action is not blocked
    assert window.dispatches == ["b.mp3"]
    assert os.path.basename(window.current_path) == "b.mp3"


_DLL = os.path.join(os.path.dirname(os.path.dirname(__file__)), "vendor", "bass", "bin", "x64", "bass.dll")


@pytest.mark.skipif(not os.path.isfile(_DLL), reason="bass.dll not present in this environment")
def test_a_real_stopped_bass_player_rewinds_but_still_reports_its_length():
    """The backend contract _StoppablePlayer models: a stopped track keeps
    reporting its length while its position reads 0 -- so for a short track
    _tick's remaining-time arithmetic alone cannot tell "stopped" from
    "about to end"."""
    from billsmusic.bass_player import BassPlayer
    player = BassPlayer()
    try:
        player.load(os.path.join(os.path.dirname(__file__), "fixtures", "sample.flac"))
        player.set_volume(0.0)
        player.play()
        player.stop()
        assert player.get_pos() == 0.0
        assert player.get_length() > 0.0
        assert player.is_playing() is False
    finally:
        player.close()
