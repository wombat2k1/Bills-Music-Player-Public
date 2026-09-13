"""_player_clock_s() during Cast.

Reported: the visualiser (equaliser) never animates while casting.
Root cause: _player_clock_s() -- the function _analyzer_tick() uses to
know what time to look precomputed audio levels up at -- only knew how
to read a position from the local video/BASS/miniaudio/VLC backends.
None of those are actually running during Cast (audio plays on the Cast
device instead), so it always fell through to "nothing playing" and the
visualiser sat idle for the whole cast session -- even though
_cast_play_path() already runs the track through _activate_track_ui()
the same as local playback, so the local analyzer has real precomputed
levels ready the whole time.
"""
import os
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from billsmusic.media_type import MediaType
from billsmusic.window import PlayerWindow


def _cast_window(snapshot, **extra):
    kwargs = dict(
        cast_active=True,
        cast_controller=SimpleNamespace(snapshot=lambda: snapshot),
        _record_cast_clock_snapshot=lambda value: None,
        _current_media_type=MediaType.AUDIO,
    )
    kwargs.update(extra)
    return SimpleNamespace(**kwargs)


def test_reads_position_from_cast_snapshot_while_playing():
    window = _cast_window({"state": "playing", "position": 42.5, "duration": 200.0})
    assert PlayerWindow._player_clock_s(window) == 42.5


def test_returns_none_when_cast_is_paused():
    # Matches the existing local-backend convention: the visualiser goes
    # idle (not frozen-in-place) whenever playback isn't actively
    # running, not just when nothing is loaded at all.
    window = _cast_window({"state": "paused", "position": 42.5, "duration": 200.0})
    assert PlayerWindow._player_clock_s(window) is None


def test_returns_none_when_cast_is_disconnected():
    window = _cast_window({"state": "disconnected", "position": 0.0, "duration": 0.0})
    assert PlayerWindow._player_clock_s(window) is None


def test_returns_none_when_position_is_zero_or_missing():
    window = _cast_window({"state": "playing", "position": 0.0, "duration": 200.0})
    assert PlayerWindow._player_clock_s(window) is None

    window = _cast_window({"state": "playing", "duration": 200.0})  # no "position" key
    assert PlayerWindow._player_clock_s(window) is None


def test_snapshot_exception_is_handled_without_raising():
    def _boom():
        raise RuntimeError("cast connection dropped mid-read")

    window = SimpleNamespace(
        cast_active=True,
        cast_controller=SimpleNamespace(snapshot=_boom),
        _current_media_type=MediaType.AUDIO,
    )
    assert PlayerWindow._player_clock_s(window) is None


def test_cast_branch_is_checked_before_the_video_branch():
    # Video is never cast in practice (_play_video_path_direct forces
    # local output first), but this proves cast_active alone is enough
    # to take the Cast path regardless of _current_media_type, so there's
    # no ordering dependency on that invariant holding elsewhere.
    window = _cast_window(
        {"state": "playing", "position": 10.0, "duration": 100.0},
        _current_media_type=MediaType.VIDEO,
    )
    window._video_backend = SimpleNamespace(
        is_playing=lambda: (_ for _ in ()).throw(
            AssertionError("must not consult the video backend while casting")
        ),
    )
    assert PlayerWindow._player_clock_s(window) == 10.0


def test_analyzer_tick_gets_a_usable_clock_from_a_cast_snapshot():
    # End-to-end through _analyzer_tick(), proving the fix actually
    # reaches the visualiser's level lookup, not just _player_clock_s()
    # in isolation.
    levels_requested = []
    window = _cast_window(
        {"state": "playing", "position": 30.0, "duration": 200.0},
        _closing=False,
        _library_apply_started=False,
        analyzer=SimpleNamespace(get_levels=lambda t: levels_requested.append(t) or [0.5]),
        analyzer_worker=None,
        analyzer_time_offset_ms=0,
        beat=SimpleNamespace(setLevels=lambda levels: None),
        party_mode=None,
        _clock_anchor=None,
        viz_logger=SimpleNamespace(active=False, _track=None),
    )
    window._player_clock_s = lambda: PlayerWindow._player_clock_s(window)

    PlayerWindow._analyzer_tick(window)

    assert levels_requested == [30.0]


def test_cast_tick_feeds_adjusted_snapshot_position_to_progress_display():
    updates = []
    snapshot = {
        "state": "playing",
        "position": 27.5,
        "raw_position": 20.0,
        "position_source": "adjusted",
        "duration": 200.0,
        "idle_reason": "",
    }
    window = SimpleNamespace(
        _sync_mini_player=lambda: None,
        _maybe_tick_queue_duration_refresh=lambda: None,
        cast_active=True,
        cast_controller=SimpleNamespace(snapshot=lambda: snapshot),
        _record_cast_clock_snapshot=lambda value: None,
        scrubbing=False,
        _update_progress=lambda current, duration: updates.append(
            (current, duration)
        ),
        _cast_completion_armed=False,
        _playback_intentionally_paused=True,
        _cast_last_state="loading",
    )

    PlayerWindow._tick(window)

    assert updates == [(27500, 200000)]
    assert window._cast_completion_armed is True
    assert window._playback_intentionally_paused is False


def test_cast_clock_diagnostics_are_rate_limited_and_include_both_clocks(monkeypatch):
    events = []
    window = SimpleNamespace(
        _cast_clock_diagnostic_at=0.0,
        diagnostics=SimpleNamespace(
            record=lambda category, operation, **kwargs: events.append(
                (category, operation, kwargs)
            )
        ),
    )
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    snapshot = {
        "state": "playing",
        "raw_position": 12.0,
        "position": 17.25,
        "position_source": "adjusted",
        "duration": 180.0,
    }

    PlayerWindow._record_cast_clock_snapshot(window, snapshot)
    now[0] = 105.0
    PlayerWindow._record_cast_clock_snapshot(window, snapshot)
    now[0] = 110.0
    PlayerWindow._record_cast_clock_snapshot(window, snapshot)

    assert len(events) == 2
    assert events[0][0:2] == ("cast", "clock_snapshot")
    assert events[0][2]["details"] == {
        "state": "playing",
        "raw_position_seconds": 12.0,
        "position_seconds": 17.25,
        "position_source": "adjusted",
        "duration_seconds": 180.0,
    }
