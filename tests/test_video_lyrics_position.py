"""Synced lyrics use the authoritative video position via the same
_player_clock_s()/_lyrics_tick() path audio already uses -- no second
lyrics timer is created for video."""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from billsmusic.media_type import MediaType
from billsmusic.window import PlayerWindow


def test_player_clock_reads_video_backend_position_when_playing():
    video_backend = MagicMock()
    video_backend.is_playing.return_value = True
    video_backend.position_ms.return_value = 12345
    window = SimpleNamespace(
        _current_media_type=MediaType.VIDEO,
        _video_backend=video_backend,
    )
    assert PlayerWindow._player_clock_s(window) == 12345 / 1000.0


def test_player_clock_returns_none_when_video_not_playing():
    video_backend = MagicMock()
    video_backend.is_playing.return_value = False
    window = SimpleNamespace(
        _current_media_type=MediaType.VIDEO,
        _video_backend=video_backend,
    )
    assert PlayerWindow._player_clock_s(window) is None


def test_lyrics_tick_advances_from_video_position():
    video_backend = MagicMock()
    video_backend.is_playing.return_value = True
    video_backend.position_ms.return_value = 5000  # 5.0s
    overlay = SimpleNamespace(set_lyric=MagicMock(), clear_lyric=MagicMock())
    window = SimpleNamespace(
        lyrics_enabled=True,
        _lyrics=[(0.0, "line0"), (4.0, "line1"), (8.0, "line2")],
        _lyric_times=[0.0, 4.0, 8.0],
        _lyric_idx=None,
        lyric_time_offset_ms=0,
        _current_media_type=MediaType.VIDEO,
        _video_backend=video_backend,
        overlay=overlay,
    )
    window._player_clock_s = lambda: PlayerWindow._player_clock_s(window)

    PlayerWindow._lyrics_tick(window)

    assert window._lyric_idx == 1  # 5.0s falls in the [4.0, 8.0) window
    overlay.set_lyric.assert_called_once_with("line1")
