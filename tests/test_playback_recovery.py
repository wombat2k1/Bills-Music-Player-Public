from billsmusic.playback_recovery import (
    BACKEND_QUARANTINE_SECONDS,
    is_backend_quarantined,
    ordered_recovery_backends,
    record_backend_failure,
    recovery_resume_position,
)
from billsmusic.media_type import MediaType
from billsmusic.window import PlayerWindow


class WatchdogHarness:
    _arm_playback_watchdog = PlayerWindow._arm_playback_watchdog
    _check_playback_health = PlayerWindow._check_playback_health

    def __init__(self):
        self._current_media_type = MediaType.AUDIO
        self.auto_playback_recovery = True
        self._playback_expected = False
        self._playback_recovery_active = False
        self._playback_intentionally_paused = False
        self._closing = False
        self.scrubbing = False
        self.fade_active = False
        self.prebuffer_active = False
        self.pending_next = False
        self.current_path = "track.flac"
        self.snapshot = (True, 0.0, 180.0, None)
        self.recoveries = []

    def _playback_health_snapshot(self):
        return self.snapshot

    def _current_backend_name(self):
        return "bass"

    def _begin_playback_recovery(self, reason, backend, error=None):
        self.recoveries.append((reason, backend))


def test_resume_position_rewinds_without_going_below_zero():
    assert recovery_resume_position(12.0) == 11.5
    assert recovery_resume_position(0.2) == 0.0
    assert recovery_resume_position(0.0) == 0.0


def test_recovery_order_starts_with_active_and_skips_unavailable():
    assert ordered_recovery_backends(
        "miniaudio", ["vlc", "miniaudio"]
    ) == ["miniaudio", "vlc"]
    assert ordered_recovery_backends("bass", ["vlc"]) == ["vlc"]


def test_quarantined_backends_are_skipped():
    assert ordered_recovery_backends(
        "vlc", ["vlc", "miniaudio", "bass"], ["vlc", "bass"]
    ) == ["miniaudio"]


def test_two_recent_failures_temporarily_quarantine_backend():
    failures = {}
    quarantined = {}
    assert not record_backend_failure(failures, quarantined, "vlc", 100.0)
    assert record_backend_failure(failures, quarantined, "vlc", 120.0)
    assert is_backend_quarantined(quarantined, "vlc", 121.0)
    assert quarantined["vlc"] == 120.0 + BACKEND_QUARANTINE_SECONDS
    assert not is_backend_quarantined(
        quarantined, "vlc", 121.0 + BACKEND_QUARANTINE_SECONDS
    )


def test_old_failures_do_not_trigger_quarantine():
    failures = {"bass": [1.0]}
    quarantined = {}
    assert not record_backend_failure(failures, quarantined, "bass", 100.0)
    assert not is_backend_quarantined(quarantined, "bass", 100.0)


def test_playing_backend_with_clock_still_at_start_is_not_restarted(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("billsmusic.window.time.monotonic", lambda: clock[0])
    harness = WatchdogHarness()
    harness._arm_playback_watchdog(0.0)
    clock[0] = 120.0
    harness._check_playback_health()
    assert harness.recoveries == []
    assert not harness._playback_watch_has_advanced


def test_midtrack_stall_requires_prior_clock_advancement(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("billsmusic.window.time.monotonic", lambda: clock[0])
    harness = WatchdogHarness()
    harness._arm_playback_watchdog(0.0)
    clock[0] = 101.0
    harness.snapshot = (True, 0.2, 180.0, None)
    harness._check_playback_health()
    assert harness._playback_watch_has_advanced
    clock[0] = 105.0
    harness._check_playback_health()
    assert harness.recoveries == [("midtrack-stall", "bass")]


def test_video_never_enters_audio_playback_recovery(monkeypatch):
    # Same stalled-clock scenario as test_midtrack_stall_requires_prior_
    # clock_advancement above -- for audio this triggers recovery; for
    # video, the audio watchdog must never even be consulted.
    clock = [100.0]
    monkeypatch.setattr("billsmusic.window.time.monotonic", lambda: clock[0])
    harness = WatchdogHarness()
    harness._arm_playback_watchdog(0.0)
    harness._current_media_type = MediaType.VIDEO
    clock[0] = 101.0
    harness.snapshot = (True, 0.2, 180.0, None)
    harness._check_playback_health()
    clock[0] = 105.0
    harness._check_playback_health()
    assert harness.recoveries == []
