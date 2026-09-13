"""Visibility-aware ACTIVE/SUSPENDED lifecycle for visualiser widgets.

Pure state tracking only: no Qt, no timers, no widget access. Each
visible "consumer" of visualiser rendering (the main window's BeatWidget,
Party Mode's own BeatWidget, the shared audio-analysis feed driving both)
gets its own VisualiserLifecycleController instance. Callers compute
effective visibility from their own window/panel/mini-player state via
should_visualiser_run() and hand it to evaluate(), which reports a
transition only when the state actually changed -- callers never get
duplicate suspend/resume work, or duplicate diagnostics, for an unchanged
state.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class VisualiserRunState(Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


def should_visualiser_run(
    *,
    window_minimized: bool = False,
    window_visible: bool = True,
    panel_visible: bool = True,
    mini_player_active: bool = False,
    closing: bool = False,
) -> bool:
    """Effective-visibility calculation for one visualiser consumer.

    Every input defaults to "nothing is hiding it" so callers only need
    to pass the checks that actually apply to their widget (e.g. Party
    Mode has no "mini player" concept and can simply omit that kwarg).
    """
    if closing:
        return False
    if window_minimized:
        return False
    if not window_visible:
        return False
    if mini_player_active:
        return False
    if not panel_visible:
        return False
    return True


@dataclass(frozen=True)
class VisualiserTransition:
    consumer: str
    previous: VisualiserRunState
    current: VisualiserRunState
    reason: str
    # Only set on a SUSPENDED -> ACTIVE transition.
    suspended_seconds: Optional[float] = None


class VisualiserLifecycleController:
    """Tracks ACTIVE/SUSPENDED for one visualiser consumer and reports a
    transition only when the state actually changes."""

    def __init__(
        self, consumer: str,
        initial_state: VisualiserRunState = VisualiserRunState.SUSPENDED,
    ):
        self.consumer = consumer
        self.state = initial_state
        self._suspended_at: Optional[float] = None

    @property
    def is_active(self) -> bool:
        return self.state is VisualiserRunState.ACTIVE

    def evaluate(
        self, effective_visible: bool, *, reason: str, now: float,
    ) -> Optional[VisualiserTransition]:
        target = (
            VisualiserRunState.ACTIVE if effective_visible
            else VisualiserRunState.SUSPENDED
        )
        if target is self.state:
            return None
        previous = self.state
        self.state = target
        suspended_seconds = None
        if target is VisualiserRunState.ACTIVE and self._suspended_at is not None:
            suspended_seconds = max(0.0, now - self._suspended_at)
            self._suspended_at = None
        elif target is VisualiserRunState.SUSPENDED:
            self._suspended_at = now
        return VisualiserTransition(
            consumer=self.consumer, previous=previous, current=target,
            reason=reason, suspended_seconds=suspended_seconds,
        )
