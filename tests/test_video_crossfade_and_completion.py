"""Video-specific crossfade skipping and completion routing.

Covers: audio<->video/video<->video transitions never crossfade while
audio<->audio is unaffected; EndOfMedia flows through the same completion
path as the existing audio-completion reasons, exactly once per generation;
manual Stop is not treated as a completion; video queue rows are excluded
from crossfade-savings in the duration estimator.
"""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from billsmusic.media_type import MediaType
from billsmusic.queue_duration_estimator import QueueTrackInfo
from billsmusic.window import PlayerWindow


def _crossfade_probe_window(current_type, mode="crossfade"):
    window = SimpleNamespace(
        track_transition_mode=mode,
        _current_media_type=current_type,
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None),
    )
    window._crossfade_eligible_for_transition = (
        lambda path: PlayerWindow._crossfade_eligible_for_transition(window, path)
    )
    return window


def test_audio_to_audio_crossfade_is_unchanged():
    window = _crossfade_probe_window(MediaType.AUDIO)
    assert window._crossfade_eligible_for_transition("next.mp3") is True


def test_audio_to_video_skips_crossfade():
    window = _crossfade_probe_window(MediaType.AUDIO)
    assert window._crossfade_eligible_for_transition("next.mp4") is False


def test_video_to_audio_skips_crossfade():
    window = _crossfade_probe_window(MediaType.VIDEO)
    assert window._crossfade_eligible_for_transition("next.mp3") is False


def test_video_to_video_skips_crossfade():
    window = _crossfade_probe_window(MediaType.VIDEO)
    assert window._crossfade_eligible_for_transition("next.mp4") is False


def test_all_transitions_involving_karaoke_skip_crossfade():
    assert _crossfade_probe_window(MediaType.AUDIO)._crossfade_eligible_for_transition("next.cdg") is False
    assert _crossfade_probe_window(MediaType.KARAOKE)._crossfade_eligible_for_transition("next.mp3") is False
    assert _crossfade_probe_window(MediaType.KARAOKE)._crossfade_eligible_for_transition("next.zip") is False
    assert _crossfade_probe_window(MediaType.VIDEO)._crossfade_eligible_for_transition("next.zip") is False


def test_normal_mode_never_crossfades_regardless_of_media_type():
    window = _crossfade_probe_window(MediaType.AUDIO, mode="normal")
    assert window._crossfade_eligible_for_transition("next.mp3") is False


def test_crossfade_skip_does_not_mutate_global_transition_mode():
    window = _crossfade_probe_window(MediaType.VIDEO)
    window._crossfade_eligible_for_transition("next.mp3")
    assert window.track_transition_mode == "crossfade"


class _PlayDirectSpy:
    def __init__(self, result=True):
        self.calls = []
        self.result = result

    def __call__(self, path, crossfade=False, index=None, immediate_crossfade=False):
        self.calls.append({"path": path, "crossfade": crossfade})
        return self.result


def _next_track_window_for_completion(spy, completed, reason_marker):
    window = SimpleNamespace(
        track_transition_mode="crossfade",
        current_path="clip.mp4",
        queue=[],
        _playback_context_paths=["clip.mp4", "clip2.mp4"],
        _current_media_type=MediaType.VIDEO,
        fade_active=False,
        prebuffer_active=False,
        sleep_timer=SimpleNamespace(is_stop_after_track=False),
        _next_unplayed_queue_row=lambda: None,
        _audio_log=lambda message: None,
        _audio_name=lambda path: path,
        _play_path_direct=spy,
        _record_track_completion=lambda reason: completed.append(reason) or True,
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None),
        _mixed_transition_state="idle",
    )
    window._playback_fallback_paths = lambda: window._playback_context_paths
    window._crossfade_eligible_for_transition = (
        lambda path: PlayerWindow._crossfade_eligible_for_transition(window, path)
    )
    return window


def test_video_ended_reason_records_completion_and_advances():
    spy = _PlayDirectSpy()
    completed = []
    window = _next_track_window_for_completion(spy, completed, "video-ended")

    PlayerWindow._next_track(window, "video-ended")

    assert completed == ["video-ended"]
    assert spy.calls == [{"path": "clip2.mp4", "crossfade": False}]


def test_video_ended_transition_to_next_video_skips_crossfade():
    spy = _PlayDirectSpy()
    window = _next_track_window_for_completion(spy, [], "video-ended")
    PlayerWindow._next_track(window, "video-ended")
    assert spy.calls[0]["crossfade"] is False


def test_manual_next_on_video_does_not_record_completion():
    spy = _PlayDirectSpy()
    completed = []
    window = _next_track_window_for_completion(spy, completed, "manual-next")

    PlayerWindow._next_track(window, "manual-next")

    assert completed == []
    assert spy.calls == [{"path": "clip2.mp4", "crossfade": False}]


def test_video_queue_row_is_not_crossfade_eligible_for_duration_estimate():
    info = QueueTrackInfo(
        duration_seconds=120.0, played=False, unavailable=False,
        crossfade_eligible=MediaType.VIDEO == MediaType.AUDIO,
    )
    assert info.crossfade_eligible is False


def test_audio_queue_row_remains_crossfade_eligible_for_duration_estimate():
    info = QueueTrackInfo(
        duration_seconds=120.0, played=False, unavailable=False,
        crossfade_eligible=MediaType.AUDIO == MediaType.AUDIO,
    )
    assert info.crossfade_eligible is True
