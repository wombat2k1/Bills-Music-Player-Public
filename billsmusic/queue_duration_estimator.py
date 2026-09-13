"""Crossfade-aware Up Next duration and finish-time estimation.

Pure calculation only: no Qt, no file I/O, no playback-backend access.
Callers (PlayerWindow) resolve queue rows, current-track state and
crossfade settings from already-cached data and hand them in here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Sequence


@dataclass(frozen=True)
class QueueTrackInfo:
    """One Up Next row's duration-relevant state."""
    duration_seconds: Optional[float] = None
    played: bool = False
    unavailable: bool = False
    crossfade_eligible: bool = True


@dataclass(frozen=True)
class CurrentTrackState:
    """The actively loaded track's duration and playback position."""
    duration_seconds: Optional[float]
    position_seconds: float = 0.0


@dataclass(frozen=True)
class ActiveCrossfadeState:
    """Authoritative outgoing/incoming player state while a crossfade
    between the current track and the next Up Next row is already in
    progress.

    When supplied, this overrides the formula-based overlap estimate for
    that one transition, so real (already-overlapping) remaining time
    isn't also deducted a second time via the static min() formula.
    """
    outgoing_remaining_seconds: float
    incoming_elapsed_seconds: float


@dataclass(frozen=True)
class QueueDurationEstimate:
    known_remaining_seconds: float
    unknown_track_count: int
    expected_crossfade_seconds: float
    is_paused: bool
    finish_datetime: Optional[datetime]
    is_stopped: bool = False
    queue_empty: bool = False
    current_remaining_seconds: Optional[float] = None
    queued_known_seconds: float = 0.0
    calculated_at: Optional[datetime] = None


def estimate_queue_duration(
    *,
    upcoming: Sequence[QueueTrackInfo] = (),
    current: Optional[CurrentTrackState] = None,
    is_paused: bool = False,
    crossfade_enabled: bool = False,
    crossfade_seconds: float = 0.0,
    active_crossfade: Optional[ActiveCrossfadeState] = None,
    now: Optional[datetime] = None,
) -> QueueDurationEstimate:
    now = now or datetime.now()
    is_stopped = current is None

    eligible = [
        track for track in upcoming if not track.played and not track.unavailable
    ]

    if current is None and not eligible:
        return QueueDurationEstimate(
            known_remaining_seconds=0.0,
            unknown_track_count=0,
            expected_crossfade_seconds=0.0,
            is_paused=is_paused,
            finish_datetime=None,
            is_stopped=is_stopped,
            queue_empty=True,
            calculated_at=now,
        )

    segment_known: List[Optional[float]] = []
    segment_full: List[Optional[float]] = []
    segment_crossfade_in: List[bool] = []

    if current is not None:
        if current.duration_seconds is None:
            current_remaining: Optional[float] = None
        else:
            current_remaining = max(
                0.0,
                float(current.duration_seconds) - max(0.0, float(current.position_seconds)),
            )
        segment_known.append(current_remaining)
        segment_full.append(current.duration_seconds)
        segment_crossfade_in.append(False)  # no "into" transition for the first segment

    for track in eligible:
        segment_known.append(track.duration_seconds)
        segment_full.append(track.duration_seconds)
        segment_crossfade_in.append(track.crossfade_eligible)

    total_overlap = 0.0
    for index in range(1, len(segment_known)):
        if not crossfade_enabled or not segment_crossfade_in[index]:
            continue
        if index == 1 and current is not None and active_crossfade is not None:
            continue  # authoritative state below replaces this transition's estimate
        outgoing_full = segment_full[index - 1]
        incoming_full = segment_full[index]
        if outgoing_full is None or incoming_full is None:
            continue
        overlap = min(max(0.0, crossfade_seconds), outgoing_full, incoming_full)
        total_overlap += max(0.0, overlap)

    if active_crossfade is not None and current is not None and len(segment_known) >= 2:
        segment_known[0] = max(0.0, active_crossfade.outgoing_remaining_seconds)
        if segment_full[1] is not None:
            segment_known[1] = max(
                0.0,
                float(segment_full[1]) - max(0.0, active_crossfade.incoming_elapsed_seconds),
            )

    unknown_count = sum(1 for value in segment_known if value is None)
    known_sum = sum(value for value in segment_known if value is not None)
    known_remaining = max(0.0, known_sum - total_overlap)

    current_remaining_component = segment_known[0] if current is not None else None
    queued_values = segment_known[1:] if current is not None else segment_known
    queued_known = sum(value for value in queued_values if value is not None)

    return QueueDurationEstimate(
        known_remaining_seconds=known_remaining,
        unknown_track_count=unknown_count,
        expected_crossfade_seconds=total_overlap,
        is_paused=is_paused,
        finish_datetime=now + timedelta(seconds=known_remaining),
        is_stopped=is_stopped,
        queue_empty=False,
        current_remaining_seconds=current_remaining_component,
        queued_known_seconds=queued_known,
        calculated_at=now,
    )


def parse_time_field_seconds(text: Optional[str]) -> Optional[float]:
    """Parse an 'm:ss' / 'h:mm:ss' display string into seconds.

    Returns None (not 0) for missing, placeholder ("--") or unparsable
    text, so callers can tell "unknown" apart from "zero-length".
    """
    if not text:
        return None
    text = text.strip()
    if not text or text in ("--", "Unknown"):
        return None
    try:
        total = 0
        for part in text.split(":"):
            total = total * 60 + int(part)
    except (TypeError, ValueError):
        return None
    return float(total)


def format_remaining_duration(seconds: float) -> str:
    """'H hr M min' style text; seconds only shown under one minute."""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        whole = int(round(seconds))
        return f"{whole} sec"
    total_minutes = int(seconds // 60)
    hours, minutes = divmod(total_minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hr")
    parts.append(f"{minutes} min")
    return " ".join(parts)


def format_queue_duration_summary(
    estimate: QueueDurationEstimate,
    *,
    finish_time_text: Optional[str] = None,
) -> str:
    if estimate.queue_empty:
        return "Up Next: Empty"
    quantity = format_remaining_duration(estimate.known_remaining_seconds)
    if estimate.unknown_track_count > 0:
        quantity = f"at least {quantity}"
    quantity += " total" if estimate.is_stopped else " remaining"
    parts = [quantity]
    if estimate.is_paused:
        parts.append("Paused")
    elif (
        not estimate.is_stopped
        and estimate.unknown_track_count == 0
        and finish_time_text
    ):
        parts.append(f"Finishes at {finish_time_text}")
    if estimate.unknown_track_count > 0:
        noun = "track" if estimate.unknown_track_count == 1 else "tracks"
        parts.append(f"{estimate.unknown_track_count} unknown {noun}")
    return "Up Next: " + " · ".join(parts)


def format_queue_duration_tooltip(estimate: QueueDurationEstimate) -> str:
    lines = []
    if estimate.current_remaining_seconds is not None:
        lines.append(
            "Current track remaining: "
            + format_remaining_duration(estimate.current_remaining_seconds)
        )
    lines.append(
        "Queued tracks total: "
        + format_remaining_duration(estimate.queued_known_seconds)
    )
    if estimate.expected_crossfade_seconds:
        lines.append(
            "Expected crossfade overlap: "
            + format_remaining_duration(estimate.expected_crossfade_seconds)
        )
    if estimate.unknown_track_count:
        noun = "track" if estimate.unknown_track_count == 1 else "tracks"
        lines.append(f"Unknown duration: {estimate.unknown_track_count} {noun}")
    if estimate.calculated_at is not None:
        lines.append(f"Calculated at {estimate.calculated_at.strftime('%H:%M:%S')}")
    return "\n".join(lines)
