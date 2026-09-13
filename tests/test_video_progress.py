"""Video progress remains live even if a one-shot Qt timing signal is lost."""
import time
from types import SimpleNamespace

from billsmusic.media_type import MediaType
from billsmusic.window import PlayerWindow


def _window(duration_ms=9000, position_ms=4200):
    progress = []
    records = []
    backend = SimpleNamespace(
        duration_ms=lambda: duration_ms,
        position_ms=lambda: position_ms,
    )
    window = SimpleNamespace(
        _current_media_type=MediaType.VIDEO,
        _video_backend=backend,
        scrubbing=False,
        _playback_expected=True,
        _video_progress_warning_reported=False,
        _video_progress_started_at=time.monotonic(),
        _video_timing_available_reported=False,
        current_path="clip.mp4",
        diagnostics=SimpleNamespace(
            record=lambda *args, **kwargs: records.append((args, kwargs)),
            path_details=lambda path: {"path": path},
        ),
        _update_progress=lambda position, duration: progress.append((position, duration)),
    )
    window._record_video_timing_available = (
        lambda position, duration: PlayerWindow._record_video_timing_available(
            window, position, duration,
        )
    )
    return window, progress, records


def test_health_tick_polls_video_timing_and_updates_progress():
    window, progress, records = _window()

    PlayerWindow._check_playback_health(window)

    assert progress == [(4200, 9000)]
    timing_records = [r for r in records if r[0][1] == "video_timing_available"]
    assert len(timing_records) == 1


def test_missing_video_duration_is_reported_once_after_grace_period():
    window, progress, records = _window(duration_ms=0, position_ms=0)
    window._video_progress_started_at = time.monotonic() - 4.0

    PlayerWindow._check_playback_health(window)
    PlayerWindow._check_playback_health(window)

    assert progress == []
    warning_records = [r for r in records if r[0][1] == "video_timing_unavailable"]
    assert len(warning_records) == 1
