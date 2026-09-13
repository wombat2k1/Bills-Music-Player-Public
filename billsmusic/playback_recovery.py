"""Pure policy helpers for bounded playback recovery."""
from __future__ import annotations

from typing import Dict, Iterable, List

PLAYBACK_START_GRACE_SECONDS = 2.0
STALL_POSITION_TOLERANCE_SECONDS = 0.10
STALL_CONFIRMATION_SECONDS = 3.0
RECOVERY_REWIND_SECONDS = 0.5
BACKEND_FAILURE_WINDOW_SECONDS = 60.0
BACKEND_QUARANTINE_SECONDS = 300.0
BACKEND_FAILURES_BEFORE_QUARANTINE = 2


def recovery_resume_position(position: float) -> float:
    return max(0.0, float(position or 0.0) - RECOVERY_REWIND_SECONDS)


def ordered_recovery_backends(
    active: str,
    available: Iterable[str],
    quarantined: Iterable[str] = (),
) -> List[str]:
    available_set = set(available)
    quarantined_set = set(quarantined)
    preferred_order = [active, "vlc", "miniaudio", "bass"]
    result: List[str] = []
    for backend in preferred_order:
        if (
            backend in available_set
            and backend not in quarantined_set
            and backend not in result
        ):
            result.append(backend)
    return result


def record_backend_failure(
    failure_times: Dict[str, List[float]],
    quarantined_until: Dict[str, float],
    backend: str,
    now: float,
) -> bool:
    recent = [
        value for value in failure_times.get(backend, [])
        if now - value <= BACKEND_FAILURE_WINDOW_SECONDS
    ]
    recent.append(now)
    failure_times[backend] = recent
    if len(recent) >= BACKEND_FAILURES_BEFORE_QUARANTINE:
        quarantined_until[backend] = now + BACKEND_QUARANTINE_SECONDS
        return True
    return False


def is_backend_quarantined(
    quarantined_until: Dict[str, float], backend: str, now: float,
) -> bool:
    until = float(quarantined_until.get(backend, 0.0) or 0.0)
    if until <= now:
        quarantined_until.pop(backend, None)
        return False
    return True
