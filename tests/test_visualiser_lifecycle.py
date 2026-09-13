"""Tests for the pure ACTIVE/SUSPENDED visualiser lifecycle logic in
billsmusic/visualiser_lifecycle.py.

No Qt, no widgets -- these exercise should_visualiser_run() and
VisualiserLifecycleController directly.
"""
from billsmusic.visualiser_lifecycle import (
    VisualiserLifecycleController,
    VisualiserRunState,
    should_visualiser_run,
)


# ---------------------------------------------------------------------------
# should_visualiser_run: effective-visibility calculation
# ---------------------------------------------------------------------------

def test_fully_visible_runs():
    assert should_visualiser_run(
        window_minimized=False, window_visible=True, panel_visible=True,
        mini_player_active=False, closing=False,
    ) is True


def test_minimized_window_does_not_run():
    assert should_visualiser_run(window_minimized=True) is False


def test_hidden_window_does_not_run():
    assert should_visualiser_run(window_visible=False) is False


def test_mini_player_active_does_not_run():
    assert should_visualiser_run(mini_player_active=True) is False


def test_panel_not_visible_does_not_run():
    assert should_visualiser_run(panel_visible=False) is False


def test_closing_never_runs_even_if_otherwise_visible():
    assert should_visualiser_run(
        window_minimized=False, window_visible=True, panel_visible=True,
        mini_player_active=False, closing=True,
    ) is False


def test_defaults_assume_nothing_is_hiding_it():
    # Party Mode has no "mini player" concept and can omit that kwarg.
    assert should_visualiser_run() is True


# ---------------------------------------------------------------------------
# VisualiserLifecycleController: state tracking + dedup
# ---------------------------------------------------------------------------

def test_starts_suspended_by_default():
    controller = VisualiserLifecycleController("main_window")
    assert controller.state is VisualiserRunState.SUSPENDED
    assert controller.is_active is False


def test_visible_and_selected_transitions_to_active():
    controller = VisualiserLifecycleController("main_window")
    transition = controller.evaluate(True, reason="startup", now=100.0)
    assert transition is not None
    assert transition.previous is VisualiserRunState.SUSPENDED
    assert transition.current is VisualiserRunState.ACTIVE
    assert controller.is_active is True


def test_minimizing_suspends():
    controller = VisualiserLifecycleController(
        "main_window", initial_state=VisualiserRunState.ACTIVE,
    )
    transition = controller.evaluate(False, reason="window_minimized", now=100.0)
    assert transition.current is VisualiserRunState.SUSPENDED
    assert controller.is_active is False


def test_restoring_resumes():
    controller = VisualiserLifecycleController(
        "main_window", initial_state=VisualiserRunState.SUSPENDED,
    )
    transition = controller.evaluate(True, reason="window_restored", now=100.0)
    assert transition.current is VisualiserRunState.ACTIVE


def test_switching_tabs_suspends_and_returning_resumes():
    controller = VisualiserLifecycleController(
        "main_window", initial_state=VisualiserRunState.ACTIVE,
    )
    away = controller.evaluate(False, reason="tab_changed", now=10.0)
    assert away.current is VisualiserRunState.SUSPENDED
    back = controller.evaluate(True, reason="tab_changed", now=11.0)
    assert back.current is VisualiserRunState.ACTIVE


def test_hiding_panel_suspends():
    controller = VisualiserLifecycleController(
        "main_window", initial_state=VisualiserRunState.ACTIVE,
    )
    transition = controller.evaluate(False, reason="panel_hidden", now=1.0)
    assert transition.current is VisualiserRunState.SUSPENDED


def test_disabling_in_preferences_suspends_and_reenabling_resumes():
    controller = VisualiserLifecycleController(
        "main_window", initial_state=VisualiserRunState.ACTIVE,
    )
    disabled = controller.evaluate(False, reason="preferences_disabled", now=1.0)
    assert disabled.current is VisualiserRunState.SUSPENDED
    enabled = controller.evaluate(True, reason="preferences_enabled", now=2.0)
    assert enabled.current is VisualiserRunState.ACTIVE


def test_mini_player_entry_suspends_and_exit_resumes():
    controller = VisualiserLifecycleController(
        "main_window", initial_state=VisualiserRunState.ACTIVE,
    )
    entered = controller.evaluate(False, reason="mini_player_shown", now=1.0)
    assert entered.current is VisualiserRunState.SUSPENDED
    left = controller.evaluate(True, reason="mini_player_hidden", now=2.0)
    assert left.current is VisualiserRunState.ACTIVE


# ---------------------------------------------------------------------------
# Dedup: repeated identical visibility must not produce repeated transitions
# ---------------------------------------------------------------------------

def test_repeated_identical_state_produces_no_transition():
    controller = VisualiserLifecycleController(
        "main_window", initial_state=VisualiserRunState.ACTIVE,
    )
    assert controller.evaluate(True, reason="tick", now=1.0) is None
    assert controller.evaluate(True, reason="tick", now=2.0) is None
    assert controller.state is VisualiserRunState.ACTIVE


def test_rapid_repeated_visibility_flapping_still_only_transitions_on_change():
    controller = VisualiserLifecycleController(
        "main_window", initial_state=VisualiserRunState.SUSPENDED,
    )
    transitions = [
        controller.evaluate(visible, reason="rapid", now=float(i))
        for i, visible in enumerate([False, False, True, True, True, False])
    ]
    changed = [t for t in transitions if t is not None]
    assert [t.current for t in changed] == [
        VisualiserRunState.ACTIVE, VisualiserRunState.SUSPENDED,
    ]


# ---------------------------------------------------------------------------
# suspended_seconds bookkeeping (used for diagnostics/perf measurement)
# ---------------------------------------------------------------------------

def test_suspended_seconds_recorded_on_resume():
    controller = VisualiserLifecycleController(
        "main_window", initial_state=VisualiserRunState.ACTIVE,
    )
    controller.evaluate(False, reason="hidden", now=100.0)
    resumed = controller.evaluate(True, reason="shown", now=142.5)
    assert resumed.suspended_seconds == 42.5


def test_suspended_seconds_is_none_on_a_suspend_transition():
    controller = VisualiserLifecycleController(
        "main_window", initial_state=VisualiserRunState.ACTIVE,
    )
    transition = controller.evaluate(False, reason="hidden", now=1.0)
    assert transition.suspended_seconds is None


def test_suspended_seconds_never_negative_even_with_clock_oddities():
    controller = VisualiserLifecycleController(
        "main_window", initial_state=VisualiserRunState.ACTIVE,
    )
    controller.evaluate(False, reason="hidden", now=100.0)
    resumed = controller.evaluate(True, reason="shown", now=99.0)  # clock went backwards
    assert resumed.suspended_seconds == 0.0


# ---------------------------------------------------------------------------
# Multiple independent consumers (main window vs. Party Mode vs. shared
# analyzer feed) never share or corrupt each other's state.
# ---------------------------------------------------------------------------

def test_independent_controllers_do_not_share_state():
    main = VisualiserLifecycleController("main_window")
    party = VisualiserLifecycleController("party_mode")
    main.evaluate(True, reason="startup", now=1.0)
    assert main.is_active is True
    assert party.is_active is False
    assert main.consumer == "main_window"
    assert party.consumer == "party_mode"
