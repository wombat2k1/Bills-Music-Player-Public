"""Sleep Timer state and countdown math for Bills Music Player.

Pure logic, no Qt/PyQt dependency, so it can be driven by a fake monotonic
clock in tests without waiting in real time. billsmusic/window.py owns the
Qt timer, menu, status text and playback wiring; this module only tracks
"when should we fade / stop" and answers "how much time is left".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

MODE_OFF = "off"
MODE_STOP_AFTER_TRACK = "stop_after_track"
MODE_TIMED = "timed"

PRESET_MINUTES = (15, 30, 45, 60, 90)
MIN_CUSTOM_MINUTES = 1
MAX_CUSTOM_MINUTES = 720
FADE_WINDOW_SECONDS = 10.0


def clamp_custom_minutes(minutes: int) -> int:
    return max(MIN_CUSTOM_MINUTES, min(MAX_CUSTOM_MINUTES, int(minutes)))


@dataclass
class SleepTimerState:
    mode: str = MODE_OFF
    deadline: Optional[float] = None  # monotonic seconds
    duration_minutes: Optional[int] = None
    fade_enabled: bool = False
    stop_after_track_armed: bool = False
    fade_started: bool = False
    started_at: Optional[float] = None  # monotonic seconds


class SleepTimerController:
    """Tracks the active sleep timer mode and deadline using a monotonic clock."""

    def __init__(self, clock: Callable[[], float]):
        self._clock = clock
        self.state = SleepTimerState()

    @property
    def is_timed(self) -> bool:
        return self.state.mode == MODE_TIMED and self.state.deadline is not None

    @property
    def is_stop_after_track(self) -> bool:
        return (
            self.state.mode == MODE_STOP_AFTER_TRACK
            and self.state.stop_after_track_armed
        )

    @property
    def is_active(self) -> bool:
        return self.is_timed or self.is_stop_after_track

    def start_timed(self, minutes: int, fade_enabled: bool) -> SleepTimerState:
        minutes = clamp_custom_minutes(minutes)
        now = self._clock()
        self.state = SleepTimerState(
            mode=MODE_TIMED,
            deadline=now + (minutes * 60.0),
            duration_minutes=minutes,
            fade_enabled=bool(fade_enabled),
            started_at=now,
        )
        return self.state

    def arm_stop_after_track(self, fade_enabled: bool) -> SleepTimerState:
        self.state = SleepTimerState(
            mode=MODE_STOP_AFTER_TRACK,
            fade_enabled=bool(fade_enabled),
            stop_after_track_armed=True,
            started_at=self._clock(),
        )
        return self.state

    def cancel(self) -> None:
        self.state = SleepTimerState(fade_enabled=self.state.fade_enabled)

    def set_fade_enabled(self, enabled: bool) -> None:
        self.state.fade_enabled = bool(enabled)

    def remaining_seconds(self) -> Optional[float]:
        if not self.is_timed:
            return None
        return max(0.0, self.state.deadline - self._clock())

    def elapsed_seconds(self) -> Optional[float]:
        if self.state.started_at is None:
            return None
        return max(0.0, self._clock() - self.state.started_at)

    def is_expired(self) -> bool:
        remaining = self.remaining_seconds()
        return remaining is not None and remaining <= 0.0

    def should_be_fading(self) -> bool:
        if not self.is_timed or not self.state.fade_enabled:
            return False
        remaining = self.remaining_seconds()
        return remaining is not None and remaining <= FADE_WINDOW_SECONDS

    def fade_gain(self) -> float:
        """Linear output gain multiplier in [0, 1] for the current instant."""
        if not self.should_be_fading():
            return 1.0
        remaining = self.remaining_seconds() or 0.0
        return max(0.0, min(1.0, remaining / FADE_WINDOW_SECONDS))

    def mark_fade_started(self) -> bool:
        """Returns True only the first time this fires the fade has begun."""
        if self.state.fade_started:
            return False
        self.state.fade_started = True
        return True

    def format_remaining(self) -> str:
        remaining = self.remaining_seconds()
        if remaining is None:
            return ""
        total_seconds = int(round(remaining))
        minutes, seconds = divmod(total_seconds, 60)
        return f"{minutes}:{seconds:02d}"
