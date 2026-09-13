"""Stage 3A real-device bug: the crossfade mixed-media transition system
(video<->audio overlap) predates Plex and only ever passes a plain path
string through to local-file APIs (BASS_StreamCreateFile, QUrl.fromLocalFile)
-- never a resolved PlexTransportSource. _maybe_prepare_mixed_transition_
from_video is a per-tick trigger with no "already failed, stop retrying"
gate, so a Plex target there produced a real-device retry storm: dozens of
failed BASS_StreamCreateFile attempts per second for the rest of the video
(confirmed via the user's own crash.log/performance_diagnostics.jsonl
incident capture -- transition_id incrementing by 1 roughly every 200ms,
identical failure every time). _mixed_media_transition_eligible (used by
_next_track's own one-shot attempt) shared the same blind spot.

Fix: both now return/no-op when either side of the transition is a Plex
identity, falling through to the ordinary _play_path_direct call (which
does resolve Plex correctly) instead of the local-only crossfade prep --
the same treatment Karaoke already gets on either side."""
import os
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from billsmusic.media_type import MediaType
from billsmusic.window import PlayerWindow

PLEX_AUDIO = "plex://server-1/42.flac"
PLEX_VIDEO = "plex://server-1/99.mp4"
LOCAL_AUDIO = "F:/music/track.flac"
LOCAL_VIDEO = "F:/video/clip.mp4"


class EligibilityHarness:
    _mixed_media_transition_eligible = PlayerWindow._mixed_media_transition_eligible

    def __init__(self):
        self.track_transition_mode = "crossfade"
        self._current_media_type = MediaType.VIDEO
        self.current_path = LOCAL_VIDEO


def test_eligible_for_ordinary_local_video_to_audio():
    harness = EligibilityHarness()
    assert harness._mixed_media_transition_eligible(LOCAL_AUDIO) == "video_to_audio"


def test_not_eligible_when_next_is_plex_audio():
    harness = EligibilityHarness()
    assert harness._mixed_media_transition_eligible(PLEX_AUDIO) is None


def test_not_eligible_when_current_video_is_plex():
    harness = EligibilityHarness()
    harness.current_path = PLEX_VIDEO
    assert harness._mixed_media_transition_eligible(LOCAL_AUDIO) is None


def test_not_eligible_audio_to_video_when_next_is_plex_video():
    harness = EligibilityHarness()
    harness._current_media_type = MediaType.AUDIO
    harness.current_path = LOCAL_AUDIO
    assert harness._mixed_media_transition_eligible(PLEX_VIDEO) is None


class NearEndTriggerHarness:
    _maybe_prepare_mixed_transition_from_video = PlayerWindow._maybe_prepare_mixed_transition_from_video
    _peek_next_media_type_for_transition = PlayerWindow._peek_next_media_type_for_transition
    _next_unplayed_queue_row = PlayerWindow._next_unplayed_queue_row
    _ensure_queue_played_flags = PlayerWindow._ensure_queue_played_flags
    _playback_fallback_paths = PlayerWindow._playback_fallback_paths

    def __init__(self, next_path):
        self.track_transition_mode = "crossfade"
        self._mixed_transition_state = "idle"
        self.pending_next = False
        self.fade_active = False
        self.prebuffer_active = False
        self.queue = [next_path]
        self.queue_played = [False]
        self.current_path = PLEX_VIDEO if is_plex(next_path) else LOCAL_VIDEO
        self._playback_context_paths = []
        self.tracks = []
        self.crossfade_seconds = 6.0
        self._video_backend = MagicMock()
        self._video_backend.position_ms.return_value = 118000
        self._video_backend.duration_ms.return_value = 120000  # 2s remaining < lead
        self.begin_calls = []
        self._begin_mixed_media_transition = lambda *a: (
            self.begin_calls.append(a) or True
        )


def is_plex(path):
    return path.startswith("plex://")


def test_near_end_trigger_skips_plex_next_target_no_retry_storm():
    harness = NearEndTriggerHarness(PLEX_AUDIO)
    harness.current_path = LOCAL_VIDEO  # ordinary local video, Plex is next

    # Simulate several ticks in a row (as the real per-tick timer would) --
    # none should ever attempt a mixed transition for the Plex target.
    for _ in range(10):
        harness._maybe_prepare_mixed_transition_from_video()

    assert harness.begin_calls == []
    assert harness.pending_next is False  # never left stuck either


def test_near_end_trigger_skips_when_current_video_is_plex():
    harness = NearEndTriggerHarness(LOCAL_AUDIO)
    harness.current_path = PLEX_VIDEO

    harness._maybe_prepare_mixed_transition_from_video()

    assert harness.begin_calls == []


def test_near_end_trigger_still_fires_for_ordinary_local_pair():
    harness = NearEndTriggerHarness(LOCAL_AUDIO)
    harness.current_path = LOCAL_VIDEO

    harness._maybe_prepare_mixed_transition_from_video()

    assert len(harness.begin_calls) == 1
    assert harness.begin_calls[0][0] == "video_to_audio"
