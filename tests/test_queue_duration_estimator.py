"""Tests for the pure crossfade-aware Up Next duration/finish-time
calculation in billsmusic/queue_duration_estimator.py.

No Qt, no fake window -- these exercise estimate_queue_duration() and the
formatting helpers directly with plain dataclasses and a fixed `now`.
"""
from datetime import datetime, timedelta

from billsmusic.queue_duration_estimator import (
    ActiveCrossfadeState,
    CurrentTrackState,
    QueueTrackInfo,
    estimate_queue_duration,
    format_queue_duration_summary,
    format_queue_duration_tooltip,
    format_remaining_duration,
    parse_time_field_seconds,
)

NOW = datetime(2026, 8, 3, 20, 29, 0)


def track(duration=None, played=False, unavailable=False, crossfade_eligible=True):
    return QueueTrackInfo(
        duration_seconds=duration, played=played, unavailable=unavailable,
        crossfade_eligible=crossfade_eligible,
    )


# ---------------------------------------------------------------------------
# 1. Empty queue
# ---------------------------------------------------------------------------

def test_empty_queue_no_current_track():
    estimate = estimate_queue_duration(upcoming=[], current=None, now=NOW)
    assert estimate.queue_empty is True
    assert estimate.known_remaining_seconds == 0.0
    assert estimate.finish_datetime is None
    assert format_queue_duration_summary(estimate) == "Up Next: Empty"


def test_empty_queue_all_entries_played_or_unavailable():
    upcoming = [track(120, played=True), track(90, unavailable=True)]
    estimate = estimate_queue_duration(upcoming=upcoming, current=None, now=NOW)
    assert estimate.queue_empty is True


# ---------------------------------------------------------------------------
# 2. Stopped queue: total duration
# ---------------------------------------------------------------------------

def test_stopped_queue_shows_total_duration():
    upcoming = [track(120), track(180)]
    estimate = estimate_queue_duration(upcoming=upcoming, current=None, now=NOW)
    assert estimate.is_stopped is True
    assert estimate.known_remaining_seconds == 300.0
    text = format_queue_duration_summary(estimate)
    assert text == "Up Next: 5 min total"


# ---------------------------------------------------------------------------
# 3. Playing queue subtracts current-track position
# ---------------------------------------------------------------------------

def test_playing_subtracts_current_track_position():
    current = CurrentTrackState(duration_seconds=200.0, position_seconds=50.0)
    estimate = estimate_queue_duration(
        upcoming=[track(100)], current=current, now=NOW,
    )
    assert estimate.current_remaining_seconds == 150.0
    assert estimate.known_remaining_seconds == 250.0
    assert estimate.is_stopped is False


# ---------------------------------------------------------------------------
# 4 & 5. Paused freezes the finish time text; resume restores it
# ---------------------------------------------------------------------------

def test_paused_queue_marks_is_paused_and_summary_shows_paused():
    current = CurrentTrackState(duration_seconds=200.0, position_seconds=50.0)
    estimate = estimate_queue_duration(
        upcoming=[], current=current, is_paused=True, now=NOW,
    )
    assert estimate.is_paused is True
    text = format_queue_duration_summary(estimate, finish_time_text="22:47")
    assert "Paused" in text
    assert "Finishes at" not in text


def test_resume_restores_finish_time_display():
    current = CurrentTrackState(duration_seconds=200.0, position_seconds=50.0)
    estimate = estimate_queue_duration(
        upcoming=[], current=current, is_paused=False, now=NOW,
    )
    text = format_queue_duration_summary(estimate, finish_time_text="22:47")
    assert "Finishes at 22:47" in text
    assert "Paused" not in text


# ---------------------------------------------------------------------------
# 6. Played entries excluded
# ---------------------------------------------------------------------------

def test_played_entries_are_excluded():
    upcoming = [track(60, played=True), track(120)]
    estimate = estimate_queue_duration(upcoming=upcoming, current=None, now=NOW)
    assert estimate.known_remaining_seconds == 120.0


# ---------------------------------------------------------------------------
# 7. Duplicate entries counted separately
# ---------------------------------------------------------------------------

def test_duplicate_entries_counted_separately():
    upcoming = [track(60), track(60), track(60)]
    estimate = estimate_queue_duration(upcoming=upcoming, current=None, now=NOW)
    assert estimate.known_remaining_seconds == 180.0


# ---------------------------------------------------------------------------
# 8 & 9. Unknown durations -> "at least" + unknown count
# ---------------------------------------------------------------------------

def test_unknown_duration_produces_at_least_and_count():
    upcoming = [track(60), track(None), track(90), track(None), track(None)]
    estimate = estimate_queue_duration(upcoming=upcoming, current=None, now=NOW)
    assert estimate.unknown_track_count == 3
    assert estimate.known_remaining_seconds == 150.0
    text = format_queue_duration_summary(estimate, finish_time_text="22:47")
    assert text.startswith("Up Next: at least ")
    assert "3 unknown tracks" in text
    assert "Finishes at" not in text


def test_single_unknown_track_uses_singular_noun():
    upcoming = [track(60), track(None)]
    estimate = estimate_queue_duration(upcoming=upcoming, current=None, now=NOW)
    text = format_queue_duration_summary(estimate)
    assert "1 unknown track" in text
    assert "1 unknown tracks" not in text


# ---------------------------------------------------------------------------
# 11-13, 15. Crossfade overlap calculation
# ---------------------------------------------------------------------------

def test_crossfade_disabled_uses_full_durations():
    upcoming = [track(100), track(100)]
    estimate = estimate_queue_duration(
        upcoming=upcoming, current=None,
        crossfade_enabled=False, crossfade_seconds=10.0, now=NOW,
    )
    assert estimate.known_remaining_seconds == 200.0
    assert estimate.expected_crossfade_seconds == 0.0


def test_crossfade_enabled_deducts_eligible_overlaps():
    upcoming = [track(100), track(100), track(100)]
    estimate = estimate_queue_duration(
        upcoming=upcoming, current=None,
        crossfade_enabled=True, crossfade_seconds=10.0, now=NOW,
    )
    # Two transitions, 10s overlap each.
    assert estimate.expected_crossfade_seconds == 20.0
    assert estimate.known_remaining_seconds == 280.0


def test_crossfade_overlap_capped_by_short_track_duration():
    upcoming = [track(100), track(3)]
    estimate = estimate_queue_duration(
        upcoming=upcoming, current=None,
        crossfade_enabled=True, crossfade_seconds=10.0, now=NOW,
    )
    assert estimate.expected_crossfade_seconds == 3.0
    assert estimate.known_remaining_seconds == 100.0


def test_ineligible_transition_does_not_deduct_overlap():
    upcoming = [track(100), track(100, crossfade_eligible=False)]
    estimate = estimate_queue_duration(
        upcoming=upcoming, current=None,
        crossfade_enabled=True, crossfade_seconds=10.0, now=NOW,
    )
    assert estimate.expected_crossfade_seconds == 0.0
    assert estimate.known_remaining_seconds == 200.0


# ---------------------------------------------------------------------------
# 14. Unknown-duration transitions do not deduct speculative overlap
# ---------------------------------------------------------------------------

def test_unknown_duration_transition_no_speculative_overlap():
    upcoming = [track(100), track(None), track(100)]
    estimate = estimate_queue_duration(
        upcoming=upcoming, current=None,
        crossfade_enabled=True, crossfade_seconds=10.0, now=NOW,
    )
    # Neither transition touching the unknown-duration track can be scored.
    assert estimate.expected_crossfade_seconds == 0.0
    assert estimate.known_remaining_seconds == 200.0
    assert estimate.unknown_track_count == 1


# ---------------------------------------------------------------------------
# 16. Active crossfade is not counted twice
# ---------------------------------------------------------------------------

def test_active_crossfade_not_double_counted():
    current = CurrentTrackState(duration_seconds=200.0, position_seconds=196.0)
    active = ActiveCrossfadeState(
        outgoing_remaining_seconds=4.0, incoming_elapsed_seconds=2.0,
    )
    upcoming = [track(120)]
    without_active = estimate_queue_duration(
        upcoming=upcoming, current=current,
        crossfade_enabled=True, crossfade_seconds=6.0, now=NOW,
    )
    with_active = estimate_queue_duration(
        upcoming=upcoming, current=current, active_crossfade=active,
        crossfade_enabled=True, crossfade_seconds=6.0, now=NOW,
    )
    # Authoritative state: 4s left outgoing + (120-2)=118s left incoming.
    assert with_active.known_remaining_seconds == 122.0
    # No formula-based overlap subtracted for the actively-crossfading pair.
    assert with_active.expected_crossfade_seconds == 0.0
    assert with_active.known_remaining_seconds != without_active.known_remaining_seconds


# ---------------------------------------------------------------------------
# 17. Failed/unavailable entries excluded
# ---------------------------------------------------------------------------

def test_unavailable_entries_are_excluded():
    upcoming = [track(60, unavailable=True), track(90)]
    estimate = estimate_queue_duration(upcoming=upcoming, current=None, now=NOW)
    assert estimate.known_remaining_seconds == 90.0


# ---------------------------------------------------------------------------
# 22. Current position beyond duration clamps to zero
# ---------------------------------------------------------------------------

def test_current_position_beyond_duration_clamps_to_zero():
    current = CurrentTrackState(duration_seconds=100.0, position_seconds=250.0)
    estimate = estimate_queue_duration(upcoming=[], current=current, now=NOW)
    assert estimate.current_remaining_seconds == 0.0
    assert estimate.known_remaining_seconds == 0.0


# ---------------------------------------------------------------------------
# 23. Crossfade deduction cannot make the total negative
# ---------------------------------------------------------------------------

def test_crossfade_deduction_cannot_go_negative():
    upcoming = [track(5), track(5), track(5), track(5)]
    estimate = estimate_queue_duration(
        upcoming=upcoming, current=None,
        crossfade_enabled=True, crossfade_seconds=100.0, now=NOW,
    )
    assert estimate.known_remaining_seconds >= 0.0


# ---------------------------------------------------------------------------
# 24 & 25. Finish time uses local `now`; clock changes don't affect
# the already-known remaining duration.
# ---------------------------------------------------------------------------

def test_finish_time_is_now_plus_known_remaining():
    upcoming = [track(600)]
    estimate = estimate_queue_duration(upcoming=upcoming, current=None, now=NOW)
    assert estimate.finish_datetime == NOW + timedelta(seconds=600)


def test_clock_change_only_affects_finish_time_not_remaining():
    upcoming = [track(600)]
    first = estimate_queue_duration(upcoming=upcoming, current=None, now=NOW)
    later_now = NOW + timedelta(hours=1)  # e.g. system clock jumped forward
    second = estimate_queue_duration(upcoming=upcoming, current=None, now=later_now)
    assert first.known_remaining_seconds == second.known_remaining_seconds
    assert first.finish_datetime != second.finish_datetime


# ---------------------------------------------------------------------------
# 26. Formatting handles durations over 24 hours
# ---------------------------------------------------------------------------

def test_format_remaining_duration_over_24_hours():
    assert format_remaining_duration(30 * 3600 + 5 * 60) == "30 hr 5 min"


def test_format_remaining_duration_under_a_minute_shows_seconds():
    assert format_remaining_duration(45) == "45 sec"


def test_format_remaining_duration_omits_seconds_at_and_above_a_minute():
    assert format_remaining_duration(18 * 60) == "18 min"
    assert format_remaining_duration(90) == "1 min"


def test_format_remaining_duration_never_negative():
    assert format_remaining_duration(-5) == "0 sec"


# ---------------------------------------------------------------------------
# parse_time_field_seconds
# ---------------------------------------------------------------------------

def test_parse_time_field_seconds_mm_ss():
    assert parse_time_field_seconds("3:45") == 225.0


def test_parse_time_field_seconds_h_mm_ss():
    assert parse_time_field_seconds("1:02:03") == 3723.0


def test_parse_time_field_seconds_placeholder_is_unknown():
    assert parse_time_field_seconds("--") is None
    assert parse_time_field_seconds("Unknown") is None
    assert parse_time_field_seconds(None) is None
    assert parse_time_field_seconds("") is None


def test_parse_time_field_seconds_garbage_is_unknown():
    assert parse_time_field_seconds("not-a-time") is None


# ---------------------------------------------------------------------------
# Given examples from the spec, verbatim
# ---------------------------------------------------------------------------

def test_summary_examples_from_spec():
    playing = estimate_queue_duration(
        upcoming=[track(60 * 60 + 18 * 60)], current=None, now=NOW,
    )
    # Fake a "current" so it's a remaining (not total) phrasing:
    current = CurrentTrackState(duration_seconds=1.0, position_seconds=0.0)
    playing = estimate_queue_duration(
        upcoming=[track(2 * 3600 + 18 * 60 - 1)], current=current, now=NOW,
    )
    text = format_queue_duration_summary(playing, finish_time_text="22:47")
    assert text == "Up Next: 2 hr 18 min remaining · Finishes at 22:47"

    paused = estimate_queue_duration(
        upcoming=[track(2 * 3600 + 18 * 60 - 1)], current=current,
        is_paused=True, now=NOW,
    )
    assert (
        format_queue_duration_summary(paused, finish_time_text="22:47")
        == "Up Next: 2 hr 18 min remaining · Paused"
    )

    stopped = estimate_queue_duration(
        upcoming=[track(2 * 3600 + 31 * 60)], current=None, now=NOW,
    )
    assert format_queue_duration_summary(stopped) == "Up Next: 2 hr 31 min total"

    incomplete = estimate_queue_duration(
        upcoming=[
            track(1 * 3600 + 52 * 60 - 1), track(None), track(None), track(None),
        ],
        current=current, now=NOW,
    )
    assert (
        format_queue_duration_summary(incomplete, finish_time_text="22:47")
        == "Up Next: at least 1 hr 52 min remaining · 3 unknown tracks"
    )

    empty = estimate_queue_duration(upcoming=[], current=None, now=NOW)
    assert format_queue_duration_summary(empty) == "Up Next: Empty"


# ---------------------------------------------------------------------------
# Tooltip includes the documented breakdown fields
# ---------------------------------------------------------------------------

def test_tooltip_includes_breakdown_fields():
    current = CurrentTrackState(duration_seconds=200.0, position_seconds=50.0)
    upcoming = [track(100), track(100)]
    estimate = estimate_queue_duration(
        upcoming=upcoming, current=current,
        crossfade_enabled=True, crossfade_seconds=5.0, now=NOW,
    )
    tooltip = format_queue_duration_tooltip(estimate)
    assert "Current track remaining" in tooltip
    assert "Queued tracks total" in tooltip
    assert "Expected crossfade overlap" in tooltip
    assert "Calculated at" in tooltip


def test_tooltip_shows_unknown_count_when_present():
    upcoming = [track(100), track(None)]
    estimate = estimate_queue_duration(upcoming=upcoming, current=None, now=NOW)
    tooltip = format_queue_duration_tooltip(estimate)
    assert "Unknown duration: 1 track" in tooltip
