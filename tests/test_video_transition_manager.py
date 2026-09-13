"""Regression coverage for Phase 1 video transition state/orchestration."""
import os
import random
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtWidgets

from billsmusic.media_type import MediaType
from billsmusic.video_dual_transition import DualDeckState
from billsmusic.video_transition import (
    EFFECTS,
    VideoTransitionController,
    VideoTransitionManager,
    VideoTransitionPreferences,
    TransitionState,
)
from billsmusic.window import PlayerWindow


@pytest.fixture(scope="module", autouse=True)
def qapplication():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


class FakeOverlay:
    def __init__(self, fail_first_outgoing=False):
        self.fail_first_outgoing = fail_first_outgoing
        self.outgoing_callback = None
        self.incoming_callback = None
        self.effects = []
        self.outgoing_durations = []
        self.incoming_durations = []
        self.hosts = []
        self.covered = []
        self.cancel_count = 0
        self.shutdown_count = 0

    def set_host(self, host):
        self.hosts.append(host)

    def animate_outgoing(self, effect, duration_ms, callback):
        self.effects.append(effect)
        self.outgoing_durations.append(duration_ms)
        if self.fail_first_outgoing:
            self.fail_first_outgoing = False
            raise RuntimeError("paint setup failed")
        self.outgoing_callback = callback

    def animate_incoming(self, effect, duration_ms, callback):
        self.effects.append(effect)
        self.incoming_durations.append(duration_ms)
        self.incoming_callback = callback

    def show_covered(self, effect):
        self.covered.append(effect)

    def cancel(self):
        self.cancel_count += 1

    def shutdown(self):
        self.shutdown_count += 1


def _manager(*, target=MediaType.VIDEO, preferences=None, overlay=None):
    advances = []
    events = []
    fake_overlay = overlay or FakeOverlay()

    def advance(trigger):
        advances.append(trigger)
        return target

    manager = VideoTransitionManager(
        None,
        lambda stage, media_type: (stage, media_type),
        advance,
        lambda: target,
        lambda event, details: events.append((event, dict(details))),
        overlay=fake_overlay,
        rng=random.Random(12),
    )
    manager.configure(preferences or VideoTransitionPreferences(style="Fade Black"))
    return manager, fake_overlay, advances, events


def _finish_outgoing(overlay):
    assert overlay.outgoing_callback is not None
    callback, overlay.outgoing_callback = overlay.outgoing_callback, None
    callback()


def _finish_incoming(overlay):
    assert overlay.incoming_callback is not None
    callback, overlay.incoming_callback = overlay.incoming_callback, None
    callback()


def test_transitions_disabled_leave_existing_next_path_authoritative():
    manager, overlay, advances, _events = _manager(
        preferences=VideoTransitionPreferences(enabled=False)
    )
    assert manager.request_manual_next(MediaType.VIDEO) is False
    manager.observe_position(9_500, 10_000, MediaType.VIDEO)
    assert manager.state == TransitionState.IDLE
    assert overlay.effects == []
    assert advances == []


def test_automatic_video_to_video_waits_for_ready_then_reveals():
    manager, overlay, advances, events = _manager(
        target=MediaType.VIDEO,
        preferences=VideoTransitionPreferences(
            style="Fade Black",
            duration_seconds=0.5,
            automatic_lead_seconds=1.0,
        ),
    )
    manager.observe_position(8_500, 10_000, MediaType.VIDEO)
    assert manager.state == TransitionState.IDLE
    manager.observe_position(9_100, 10_000, MediaType.VIDEO)
    assert manager.state == TransitionState.OUTGOING
    assert overlay.outgoing_durations == [900]
    _finish_outgoing(overlay)
    assert advances == ["automatic"]
    assert manager.state == TransitionState.SWITCHING
    assert overlay.incoming_callback is None
    manager.media_ready(MediaType.VIDEO)
    assert manager.state == TransitionState.INCOMING
    assert overlay.incoming_durations == [250]
    _finish_incoming(overlay)
    assert manager.state == TransitionState.IDLE
    assert sum(event == "queue_advance_requested" for event, _ in events) == 1


def test_manual_video_to_video_and_repeated_next_advance_exactly_once():
    manager, overlay, advances, _events = _manager(target=MediaType.VIDEO)
    assert manager.request_manual_next(MediaType.VIDEO) is True
    assert manager.request_manual_next(MediaType.VIDEO) is True
    _finish_outgoing(overlay)
    assert advances == ["manual"]
    assert manager.request_manual_next(MediaType.VIDEO) is True
    assert advances == ["manual"]


def test_video_to_music_reveals_without_waiting_for_video_ready():
    manager, overlay, advances, _events = _manager(target=MediaType.AUDIO)
    manager.request_manual_next(MediaType.VIDEO)
    _finish_outgoing(overlay)
    assert advances == ["manual"]
    assert manager.state == TransitionState.INCOMING
    _finish_incoming(overlay)
    assert manager.state == TransitionState.IDLE


def test_music_to_video_is_incoming_only_and_never_advances_queue():
    manager, overlay, advances, _events = _manager(target=MediaType.VIDEO)
    assert manager.begin_incoming_only(MediaType.AUDIO, MediaType.VIDEO) is True
    assert manager.state == TransitionState.SWITCHING
    assert advances == []
    manager.media_ready(MediaType.VIDEO)
    assert manager.state == TransitionState.INCOMING
    _finish_incoming(overlay)
    assert advances == []


def test_karaoke_and_unsupported_media_are_not_supported():
    controller = VideoTransitionController()
    assert controller.supports(MediaType.KARAOKE, MediaType.VIDEO) is False
    assert controller.supports(MediaType.VIDEO, MediaType.KARAOKE) is False
    assert controller.supports(MediaType.UNSUPPORTED, MediaType.VIDEO) is False
    manager, _overlay, _advances, _events = _manager()
    assert manager.request_manual_next(MediaType.KARAOKE) is False
    assert manager.begin_incoming_only(MediaType.AUDIO, MediaType.KARAOKE) is False
    manager, _overlay, _advances, _events = _manager(target=MediaType.KARAOKE)
    assert manager.request_manual_next(MediaType.VIDEO) is False
    manager.observe_position(9_500, 10_000, MediaType.VIDEO)
    assert manager.state == TransitionState.IDLE


def test_random_modes_select_only_enabled_effects_without_immediate_repeat():
    controller = VideoTransitionController(random.Random(3))
    enabled = {effect: effect in ("Fade Black", "Push Left") for effect in EFFECTS}
    preferences = VideoTransitionPreferences(
        style="Random All", enabled_effects=enabled,
    )
    first = controller.select_effect(preferences)
    second = controller.select_effect(preferences)
    assert {first, second} == {"Fade Black", "Push Left"}


def test_disabled_effect_and_empty_random_pool_fall_back_to_fade_black():
    controller = VideoTransitionController(random.Random(1))
    disabled = {effect: False for effect in EFFECTS}
    assert controller.select_effect(
        VideoTransitionPreferences(style="Flash", enabled_effects=disabled)
    ) == "Fade Black"
    assert controller.select_effect(
        VideoTransitionPreferences(style="Random Energetic", enabled_effects=disabled)
    ) == "Fade Black"


def test_unknown_and_corrupt_preferences_use_bounded_safe_defaults():
    preferences = VideoTransitionPreferences.from_config({
        "video_transition_style": "Teleport",
        "video_transition_duration_seconds": "broken",
        "video_transition_automatic_lead_seconds": 99,
        "video_transition_manual_duration_seconds": -8,
        "video_transition_enabled_effects": "not-a-map",
    })
    assert preferences.style == "Fade Black"
    assert preferences.duration_seconds == 1.0
    assert preferences.automatic_lead_seconds == 3.0
    assert preferences.manual_duration_seconds == 0.3
    assert all(preferences.enabled_effects.values())


def test_cancel_and_playback_stop_remove_cover_without_advancing():
    manager, overlay, advances, _events = _manager()
    manager.request_manual_next(MediaType.VIDEO)
    assert manager.cancel("test") is True
    assert manager.state == TransitionState.IDLE
    assert overlay.cancel_count == 1
    assert advances == []
    manager.request_manual_next(MediaType.VIDEO)
    manager.playback_stopped()
    assert manager.state == TransitionState.IDLE
    assert overlay.cancel_count == 2
    assert advances == []


def test_queue_emptied_during_transition_still_reveals_and_completes():
    manager, overlay, advances, _events = _manager(target=None)
    manager.request_manual_next(MediaType.VIDEO)
    _finish_outgoing(overlay)
    assert advances == ["manual"]
    assert manager.state == TransitionState.INCOMING
    _finish_incoming(overlay)
    assert manager.state == TransitionState.IDLE


def test_ready_timeout_extends_bounded_then_holds_cover_until_media_ready():
    """Regression for a real-device screen recording that showed the app's
    own background exposed for ~334ms during Phase 1 fallback: the ready
    timer must never reveal the incoming side optimistically. It gets a
    bounded number of re-arms while still waiting, then holds the cover
    indefinitely -- only media_ready() (genuine confirmation) is allowed
    to reveal."""
    manager, overlay, _advances, events = _manager(target=MediaType.VIDEO)
    manager.request_manual_next(MediaType.VIDEO)
    _finish_outgoing(overlay)
    assert manager.state == TransitionState.SWITCHING
    for _ in range(manager.MAX_INCOMING_WAIT_EXTENSIONS):
        manager._on_ready_timeout()
        assert manager.state == TransitionState.SWITCHING
        assert overlay.cancel_count == 0
        assert overlay.incoming_callback is None
    assert sum(1 for event, _ in events if event == "incoming_wait_extended") == (
        manager.MAX_INCOMING_WAIT_EXTENSIONS
    )
    # One more timeout past the bound: exhausted, still no reveal.
    manager._on_ready_timeout()
    assert manager.state == TransitionState.SWITCHING
    assert overlay.incoming_callback is None
    assert any(event == "incoming_wait_exhausted" for event, _ in events)
    # Further timeouts after exhaustion are likewise inert.
    manager._on_ready_timeout()
    assert manager.state == TransitionState.SWITCHING

    # Real confirmation finally arrives, late -- now it reveals.
    manager.media_ready(MediaType.VIDEO)
    assert manager.state == TransitionState.INCOMING
    _finish_incoming(overlay)
    assert manager.state == TransitionState.IDLE


def test_incoming_confirmation_stops_further_wait_extensions():
    manager, overlay, _advances, events = _manager(target=MediaType.VIDEO)
    manager.request_manual_next(MediaType.VIDEO)
    _finish_outgoing(overlay)
    manager._on_ready_timeout()
    assert any(event == "incoming_wait_extended" for event, _ in events)
    manager.media_ready(MediaType.VIDEO)
    assert manager.state == TransitionState.INCOMING
    extensions_before = sum(1 for event, _ in events if event == "incoming_wait_extended")
    # State is no longer SWITCHING, so a stray late timer fire is a no-op.
    manager._on_ready_timeout()
    assert sum(1 for event, _ in events if event == "incoming_wait_extended") == extensions_before
    _finish_incoming(overlay)


def test_incoming_animation_failure_after_confirmation_completes_via_immediate_hide():
    """Once media_ready() has genuinely confirmed the incoming video, a
    paint-layer failure in animate_incoming() is a legitimate snap-hide-
    and-complete fallback -- unlike before this fix, this except branch is
    now only ever reachable post-confirmation, never as a premature
    reveal."""
    class _FailingIncomingOverlay(FakeOverlay):
        def animate_incoming(self, effect, duration_ms, callback):
            raise RuntimeError("paint failed")

    overlay = _FailingIncomingOverlay()
    manager, overlay, _advances, events = _manager(target=MediaType.VIDEO, overlay=overlay)
    manager.request_manual_next(MediaType.VIDEO)
    _finish_outgoing(overlay)
    manager.media_ready(MediaType.VIDEO)
    assert overlay.cancel_count == 1
    assert manager.state == TransitionState.IDLE
    assert any(event == "error" and details.get("stage") == "incoming" for event, details in events)


def test_incoming_only_setup_failure_never_reveals_before_confirmation():
    """begin_incoming_only()'s exception path (_visual_failure with
    switch_needed=False) used to call _start_incoming() unconditionally --
    a third premature-reveal site closed by the same guard."""
    class _FailingCoverOverlay(FakeOverlay):
        def show_covered(self, effect):
            raise RuntimeError("cover paint failed")

    overlay = _FailingCoverOverlay()
    manager, overlay, advances, events = _manager(target=MediaType.VIDEO, overlay=overlay)
    assert manager.begin_incoming_only(MediaType.AUDIO, MediaType.VIDEO) is True
    assert overlay.incoming_callback is None
    assert manager.state == TransitionState.SWITCHING
    assert advances == []
    assert any(event == "error" and details.get("stage") == "visual" for event, details in events)


def test_cover_remains_present_between_switch_point_and_incoming_media_ready():
    """Directly operationalizes the user-facing requirement: the cover
    must be visibly up for the entire window between switch_point_reached
    and incoming_media_ready, with no reveal in between."""
    manager, overlay, _advances, events = _manager(target=MediaType.VIDEO)
    manager.request_manual_next(MediaType.VIDEO)
    _finish_outgoing(overlay)
    assert any(event == "switch_point_reached" for event, _ in events)
    assert overlay.covered  # show_covered() was called
    assert overlay.cancel_count == 0
    assert manager.state == TransitionState.SWITCHING
    manager._on_ready_timeout()
    manager._on_ready_timeout()
    # Still covered, still nothing revealed, even after repeated timeouts.
    assert overlay.cancel_count == 0
    assert overlay.incoming_callback is None
    assert manager.state == TransitionState.SWITCHING
    manager.media_ready(MediaType.VIDEO)
    switch_index = next(i for i, (e, _) in enumerate(events) if e == "switch_point_reached")
    ready_index = next(i for i, (e, _) in enumerate(events) if e == "incoming_media_ready")
    assert ready_index > switch_index
    assert overlay.incoming_callback is not None
    _finish_incoming(overlay)


def test_shutdown_during_transition_never_advances_or_runs_late_callback():
    manager, overlay, advances, _events = _manager()
    manager.request_manual_next(MediaType.VIDEO)
    pending = overlay.outgoing_callback
    manager.shutdown()
    assert overlay.shutdown_count == 1
    pending()
    assert advances == []


def test_effect_setup_failure_uses_fade_black_and_preserves_advance():
    overlay = FakeOverlay(fail_first_outgoing=True)
    manager, overlay, advances, events = _manager(
        target=MediaType.AUDIO,
        preferences=VideoTransitionPreferences(style="Flash"),
        overlay=overlay,
    )
    manager.request_manual_next(MediaType.VIDEO)
    assert overlay.effects == ["Flash", "Fade Black"]
    assert any(event == "fallback_used" for event, _ in events)
    _finish_outgoing(overlay)
    assert advances == ["manual"]


def test_automatic_trigger_fires_once_per_media_and_seek_back_cancels_safely():
    manager, overlay, advances, _events = _manager()
    manager.observe_position(9_100, 10_000, MediaType.VIDEO)
    manager.observe_position(9_200, 10_000, MediaType.VIDEO)
    assert overlay.effects == ["Fade Black"]
    manager.observe_position(5_000, 10_000, MediaType.VIDEO)
    assert manager.state == TransitionState.IDLE
    assert advances == []
    manager.observe_position(9_300, 10_000, MediaType.VIDEO)
    assert len(overlay.effects) == 2


def test_natural_end_during_outgoing_forces_the_existing_switch_once():
    manager, overlay, advances, _events = _manager(target=MediaType.AUDIO)
    manager.observe_position(9_100, 10_000, MediaType.VIDEO)
    assert manager.handle_natural_end(MediaType.VIDEO) is True
    assert advances == ["automatic"]
    assert manager.handle_natural_end(MediaType.VIDEO) is True
    assert advances == ["automatic"]
    assert manager.state == TransitionState.INCOMING


def test_window_manual_next_delegates_to_transition_manager_when_consumed():
    calls = []
    window = SimpleNamespace(
        _current_media_type=MediaType.VIDEO,
        _video_transition_manager=SimpleNamespace(
            request_manual_next=lambda media_type: calls.append(media_type) or True
        ),
        _next_track=lambda reason: pytest.fail("existing Next must wait for switch point"),
        _announce_accessible_status=lambda message: None,
        _mixed_transition_state="idle",
    )
    PlayerWindow.next_track(window)
    assert calls == [MediaType.VIDEO]


def test_window_manual_next_uses_existing_path_when_not_consumed():
    calls = []
    window = SimpleNamespace(
        _current_media_type=MediaType.AUDIO,
        _video_transition_manager=SimpleNamespace(
            request_manual_next=lambda media_type: False
        ),
        _next_track=lambda reason: calls.append(reason),
        _announce_accessible_status=lambda message: None,
        _mixed_transition_state="idle",
    )
    PlayerWindow.next_track(window)
    assert calls == ["manual-next"]


# -- Phase 2A dispatch: VideoTransitionManager trying the dual engine first --

class FakeDualEngine:
    def __init__(self, *, commit_result=False, controller_state=DualDeckState.SECONDARY_READY):
        self.commit_result = commit_result
        self.commit_calls = []
        self.observed_positions = []
        self.media_changed_count = 0
        self.cancel_calls = []
        self.playback_stopped_calls = []
        self.playback_paused_calls = []
        self.shutdown_count = 0
        # A real DualDeckController's .state, mirrored here so
        # VideoTransitionManager's "is a dual transition already
        # committed" guards (handle_natural_end/request_manual_next) can be
        # exercised without a real controller. Defaults to SECONDARY_READY
        # so existing try_commit()-focused tests are unaffected.
        self.controller = SimpleNamespace(state=controller_state)

    def try_commit(self, trigger, duration_seconds):
        self.commit_calls.append((trigger, duration_seconds))
        if self.commit_result:
            # Mirror DualDeckController.begin_commit()'s real state
            # transition, so tests exercising a second call after a
            # successful commit see the same "already committed" state a
            # real controller would report.
            self.controller.state = DualDeckState.TRANSITIONING
        return self.commit_result

    def observe_position(self, position_ms, duration_ms, media_type):
        self.observed_positions.append((position_ms, duration_ms, media_type))

    def media_changed(self):
        self.media_changed_count += 1

    def cancel(self, reason):
        self.cancel_calls.append(reason)
        return True

    def playback_stopped(self, reason):
        self.playback_stopped_calls.append(reason)

    def playback_paused(self, paused):
        self.playback_paused_calls.append(paused)

    def shutdown(self):
        self.shutdown_count += 1


def _manager_with_dual(
    *, commit_result, target=MediaType.VIDEO,
    controller_state=DualDeckState.SECONDARY_READY,
):
    dual_engine = FakeDualEngine(commit_result=commit_result, controller_state=controller_state)
    manager, overlay, advances, events = _manager(target=target)
    manager._dual_engine = dual_engine
    return manager, dual_engine, overlay, advances, events


def test_automatic_transition_tries_dual_first_and_skips_overlay_when_it_succeeds():
    manager, dual_engine, overlay, advances, _events = _manager_with_dual(commit_result=True)
    manager.observe_position(9_100, 10_000, MediaType.VIDEO)
    assert dual_engine.commit_calls == [("automatic", 1.0)]
    assert overlay.effects == []  # no overlay animation -- dual is handling it
    assert manager.controller.automatic_started is True
    assert manager.state == TransitionState.IDLE  # Phase 1's own state untouched


def test_automatic_transition_falls_back_to_overlay_when_dual_declines():
    manager, dual_engine, overlay, advances, _events = _manager_with_dual(commit_result=False)
    manager.observe_position(9_100, 10_000, MediaType.VIDEO)
    assert dual_engine.commit_calls == [("automatic", 1.0)]
    assert manager.state == TransitionState.OUTGOING
    assert overlay.effects == ["Fade Black"]


# -- regression: real near-end playback with the v1.0.53 deadline timer
# found a THIRD call site missing the v1.0.51 "already committed" guard.
# handle_natural_end()/request_manual_next() both check
# self._dual_engine.controller.state in COMMITTED_STATES before starting a
# competing Phase 1 overlay -- observe_position()'s own automatic-commit
# branch did not, because its only defence (self.controller.automatic_started)
# is set solely by *this* method's own successful try_commit() call a few
# lines below. The deadline timer commits through an independent QTimer
# callback instead, so that flag never gets set -- the very next position
# tick (Qt keeps ticking for roughly the whole blend duration, since the
# outgoing deck is still playing) sees this controller still IDLE with
# automatic_started still False, and falls through to _start_outgoing(),
# starting a second, competing transition on top of the already-running GPU
# blend. Confirmed via v1.0.54 diagnostics on real playback: in one case it
# cascaded into handle_natural_end()'s own switch-point branch performing a
# *second* queue advance, silently skipping the next track entirely.
def test_observe_position_does_not_start_a_second_transition_when_dual_already_committed():
    manager, dual_engine, overlay, advances, _events = _manager_with_dual(
        commit_result=True, controller_state=DualDeckState.TRANSITIONING,
    )
    # Simulates the deadline timer having already committed via its own
    # independent QTimer callback -- automatic_started was never set by
    # this specific call, unlike the "tries dual first" test above.
    manager.observe_position(9_100, 10_000, MediaType.VIDEO)
    # try_commit() must not even be retried -- it would correctly decline
    # anyway (state != SECONDARY_READY), but the guard should short-circuit
    # before that, exactly like handle_natural_end()/request_manual_next().
    assert dual_engine.commit_calls == []
    assert overlay.effects == []
    assert manager.state == TransitionState.IDLE
    assert advances == []


def test_manual_next_tries_dual_first_and_skips_overlay_when_it_succeeds():
    manager, dual_engine, overlay, _advances, _events = _manager_with_dual(commit_result=True)
    assert manager.request_manual_next(MediaType.VIDEO) is True
    assert dual_engine.commit_calls == [("manual", 0.5)]
    assert overlay.effects == []


def test_manual_next_falls_back_to_overlay_when_dual_declines():
    manager, dual_engine, overlay, _advances, _events = _manager_with_dual(commit_result=False)
    assert manager.request_manual_next(MediaType.VIDEO) is True
    assert dual_engine.commit_calls == [("manual", 0.5)]
    assert overlay.effects == ["Fade Black"]


# -- regression: a GPU transition already committed must never be layered
# under a second, competing Phase 1 overlay transition. The dual engine's
# own controller.state stays in a COMMITTED_STATES value for the whole
# time its cross-dissolve plays out in the subprocess (try_commit's
# commitment point already advanced the queue once), while this Phase 1
# controller's own .state never leaves IDLE for a dual-handled transition
# -- so nothing else here would otherwise stop a second _start_outgoing()
# from firing and stomping on the still-running GPU blend (this is what
# surfaced as the app's background flashing through between videos in
# real manual testing).
@pytest.mark.parametrize(
    "committed_state",
    [
        DualDeckState.TRANSITIONING,
        DualDeckState.PROMOTING_SECONDARY,
        DualDeckState.CLEANING_PRIMARY,
    ],
)
def test_natural_end_is_ignored_while_dual_transition_already_committed(committed_state):
    manager, dual_engine, overlay, advances, _events = _manager_with_dual(
        commit_result=True, controller_state=committed_state,
    )
    assert manager.handle_natural_end(MediaType.VIDEO) is True
    # No second queue advance, no competing overlay transition started.
    assert advances == []
    assert overlay.effects == []
    assert manager.state == TransitionState.IDLE


@pytest.mark.parametrize(
    "committed_state",
    [
        DualDeckState.TRANSITIONING,
        DualDeckState.PROMOTING_SECONDARY,
        DualDeckState.CLEANING_PRIMARY,
    ],
)
def test_manual_next_is_ignored_while_dual_transition_already_committed(committed_state):
    manager, dual_engine, overlay, advances, _events = _manager_with_dual(
        commit_result=True, controller_state=committed_state,
    )
    assert manager.request_manual_next(MediaType.VIDEO) is True
    # try_commit() must not even be retried -- it would correctly decline
    # anyway (state != SECONDARY_READY), but the guard should short-circuit
    # before that, and definitely before ever falling to the overlay.
    assert dual_engine.commit_calls == []
    assert overlay.effects == []
    assert manager.state == TransitionState.IDLE


def test_natural_end_still_falls_back_to_overlay_when_dual_engine_declines():
    # A dual engine that's ready to be asked (state=SECONDARY_READY, the
    # default) but declines the commit itself (e.g. a genuinely stale
    # identity) must still fall through to the ordinary Phase 1 overlay --
    # handle_natural_end() tries the dual engine first (see the regression
    # test below) but must not treat a decline as fatal.
    manager, dual_engine, overlay, advances, _events = _manager_with_dual(commit_result=False)
    assert manager.handle_natural_end(MediaType.VIDEO) is True
    assert dual_engine.commit_calls == [("automatic", 1.0)]
    assert overlay.effects == ["Fade Black"]


# -- regression: real near-end natural playback (seek to a few seconds
# before a video's own end, let it advance naturally) found that two of
# four transitions in one session fell to the Phase 1 overlay -- classic
# load() plus a ~200-250ms video-host-hidden window, exactly the reported
# app-background-exposure symptom -- *despite* the secondary deck having
# been SECONDARY_READY for several seconds. Root cause: try_commit() is
# normally reached from observe_position()'s position-tick-driven
# automatic check, timed automatic_lead_seconds before the end; if no tick
# happens to land inside that window before the outgoing deck's own
# end-of-media fires, try_commit() is never attempted at all.
# handle_natural_end() only ever checked whether a transition was
# *already* committed (the v1.0.51 fix above) -- it never gave the
# already-ready dual engine a final chance to commit itself, unlike
# request_manual_next(), which already tried the dual engine first.
#
# v1.0.53 note: DualVideoTransitionEngine now owns a bounded deadline timer
# (see tests/test_video_dual_transition.py) that normally commits well
# before natural end is ever reached, so this handle_natural_end() path is
# now a fallback of last resort -- exercised here as "the deadline was
# somehow missed (e.g. a GUI-thread stall) -> natural end still arrives ->
# handle_natural_end() must still recover safely, with no double advance."
def test_natural_end_commits_the_dual_engine_when_secondary_is_ready():
    manager, dual_engine, overlay, advances, _events = _manager_with_dual(commit_result=True)
    # No observe_position() call at all -- simulates the deadline timer
    # never having fired (e.g. missed) before natural end arrives.
    assert manager.handle_natural_end(MediaType.VIDEO) is True
    assert dual_engine.commit_calls == [("automatic", 1.0)]
    # No competing Phase 1 overlay transition, no second queue advance.
    assert overlay.effects == []
    assert advances == []
    assert manager.state == TransitionState.IDLE
    assert manager.controller.automatic_started is True
    # A second natural-end call (e.g. a stray duplicate signal) must not
    # try to commit again now that automatic_started is set.
    assert manager.handle_natural_end(MediaType.VIDEO) is True
    assert dual_engine.commit_calls == [("automatic", 1.0)]


def test_observe_position_forwards_to_dual_engine_for_preload():
    manager, dual_engine, _overlay, _advances, _events = _manager_with_dual(commit_result=False)
    manager.observe_position(2_000, 10_000, MediaType.VIDEO)
    assert dual_engine.observed_positions == [(2_000, 10_000, MediaType.VIDEO)]


def test_media_changed_cascades_to_dual_engine():
    manager, dual_engine, _overlay, _advances, _events = _manager_with_dual(commit_result=False)
    manager.media_changed()
    assert dual_engine.media_changed_count == 1


def test_cancel_cascades_to_dual_engine_alongside_overlay_cancel():
    manager, dual_engine, overlay, _advances, _events = _manager_with_dual(commit_result=False)
    manager.request_manual_next(MediaType.VIDEO)  # starts the overlay (dual declined)
    manager.cancel("external_media_request")
    assert dual_engine.cancel_calls == ["external_media_request"]
    assert overlay.cancel_count == 1


def test_cancel_cascades_to_dual_engine_even_when_overlay_has_nothing_to_cancel():
    manager, dual_engine, _overlay, _advances, _events = _manager_with_dual(commit_result=False)
    assert manager.cancel("some_reason") is False  # overlay had nothing to cancel
    assert dual_engine.cancel_calls == ["some_reason"]  # dual cancel still attempted


def test_playback_stopped_cascades_to_dual_engine():
    manager, dual_engine, _overlay, _advances, _events = _manager_with_dual(commit_result=False)
    manager.playback_stopped("stop")
    assert dual_engine.playback_stopped_calls == ["stop"]


def test_playback_paused_forwards_to_dual_engine():
    manager, dual_engine, _overlay, _advances, _events = _manager_with_dual(commit_result=False)
    manager.playback_paused(True)
    manager.playback_paused(False)
    assert dual_engine.playback_paused_calls == [True, False]


def test_shutdown_cascades_to_dual_engine():
    manager, dual_engine, _overlay, _advances, _events = _manager_with_dual(commit_result=False)
    manager.shutdown()
    assert dual_engine.shutdown_count == 1


# ---------------------------------------------------------------------------
# Real-device defect (Plex video -> Plex MP3 at natural end, session
# e622155c): the switch point sampled the incoming media type while the
# asynchronous Plex audio dispatch was still pending, so it was still VIDEO.
# The cover waited for a video media_ready() that an audio track never
# sends, exhausted its bounded wait, and stayed up over the stage for good.
# incoming_media_superseded() is how an audio takeover lifts it.
# ---------------------------------------------------------------------------

def _covered_waiting_for_video(manager, overlay):
    assert manager.handle_natural_end(MediaType.VIDEO) is True
    _finish_outgoing(overlay)
    assert manager.state == TransitionState.SWITCHING
    assert overlay.covered, "the cover must be up at the switch point"


def test_audio_takeover_lifts_a_cover_still_waiting_for_video():
    manager, overlay, _advances, events = _manager(target=MediaType.VIDEO)
    _covered_waiting_for_video(manager, overlay)

    assert manager.incoming_media_superseded(MediaType.AUDIO) is True
    assert manager.state == TransitionState.INCOMING
    assert overlay.incoming_callback is not None  # reveal started
    _finish_incoming(overlay)
    assert manager.state == TransitionState.IDLE
    names = [name for name, _details in events]
    assert "incoming_media_superseded" in names
    assert names.index("incoming_media_superseded") < names.index("complete")


def test_audio_takeover_lifts_a_cover_even_after_its_wait_was_exhausted():
    # Bill's log: three media_ready timeouts, then incoming_wait_exhausted,
    # after which the cover stays up by design. A later audio takeover must
    # still be able to lift it.
    manager, overlay, _advances, events = _manager(target=MediaType.VIDEO)
    _covered_waiting_for_video(manager, overlay)
    for _ in range(manager.MAX_INCOMING_WAIT_EXTENSIONS + 1):
        manager._on_ready_timeout()
    assert "incoming_wait_exhausted" in [name for name, _d in events]
    assert manager.state == TransitionState.SWITCHING

    assert manager.incoming_media_superseded(MediaType.AUDIO) is True
    _finish_incoming(overlay)
    assert manager.state == TransitionState.IDLE


def test_superseded_is_a_no_op_for_a_non_video_target():
    # Synchronous local audio: the switch point already learned AUDIO and
    # revealed -- nothing to supersede, and nothing must be disturbed.
    manager, overlay, _advances, _events = _manager(target=MediaType.AUDIO)
    assert manager.handle_natural_end(MediaType.VIDEO) is True
    _finish_outgoing(overlay)
    assert manager.state == TransitionState.INCOMING
    reveal = overlay.incoming_callback
    assert manager.incoming_media_superseded(MediaType.AUDIO) is False
    assert overlay.incoming_callback is reveal  # untouched
    _finish_incoming(overlay)
    assert manager.state == TransitionState.IDLE


def test_superseded_is_a_no_op_when_idle_or_already_revealing_or_for_video():
    manager, overlay, _advances, _events = _manager(target=MediaType.VIDEO)
    assert manager.incoming_media_superseded(MediaType.AUDIO) is False  # idle
    _covered_waiting_for_video(manager, overlay)
    # A genuinely incoming video must still be waited for.
    assert manager.incoming_media_superseded(MediaType.VIDEO) is False
    assert manager.state == TransitionState.SWITCHING
    manager.media_ready(MediaType.VIDEO)
    assert manager.state == TransitionState.INCOMING
    assert manager.incoming_media_superseded(MediaType.AUDIO) is False  # revealing
    _finish_incoming(overlay)
    assert manager.state == TransitionState.IDLE


def test_superseded_is_a_no_op_after_shutdown():
    manager, overlay, _advances, _events = _manager(target=MediaType.VIDEO)
    _covered_waiting_for_video(manager, overlay)
    manager.shutdown()
    assert manager.incoming_media_superseded(MediaType.AUDIO) is False


# ---------------------------------------------------------------------------
# incoming_media_failed(): the awaited incoming media failed before ever
# becoming authoritative (async Plex resolve success=False). A distinct
# terminal outcome -- never a fake media_ready/superseded/complete.
# ---------------------------------------------------------------------------

def test_incoming_failure_cancels_a_covered_transition():
    manager, overlay, advances, events = _manager(target=MediaType.VIDEO)
    _covered_waiting_for_video(manager, overlay)
    cancels_before = overlay.cancel_count

    assert manager.incoming_media_failed("plex_resolve_failed") == "automatic"

    assert manager.state == TransitionState.IDLE
    assert overlay.cancel_count == cancels_before + 1
    assert not manager._ready_timer.isActive()
    assert manager._target_media_type is None
    assert manager._incoming_confirmed is False
    assert manager._incoming_wait_extensions == 0
    assert advances == ["automatic"]
    names = [name for name, _details in events]
    assert names[-2:] == ["incoming_media_failed", "cancelled"]
    assert dict(events[-2][1]) == {"reason": "plex_resolve_failed", "trigger": "automatic"}
    assert "complete" not in names
    assert "incoming_media_superseded" not in names
    assert "incoming_media_ready" not in names
    assert "incoming_reveal_started" not in names


def test_incoming_failure_reports_manual_trigger():
    manager, overlay, _advances, _events = _manager(target=MediaType.VIDEO)
    assert manager.request_manual_next(MediaType.VIDEO) is True
    _finish_outgoing(overlay)
    assert manager.state == TransitionState.SWITCHING
    assert manager.incoming_media_failed("plex_resolve_failed") == "manual"
    assert manager.state == TransitionState.IDLE


def test_incoming_failure_after_wait_exhausted_still_cancels():
    manager, overlay, _advances, events = _manager(target=MediaType.VIDEO)
    _covered_waiting_for_video(manager, overlay)
    for _ in range(manager.MAX_INCOMING_WAIT_EXTENSIONS + 1):
        manager._on_ready_timeout()
    assert "incoming_wait_exhausted" in [name for name, _d in events]
    assert manager.incoming_media_failed("plex_resolve_failed") == "automatic"
    assert manager.state == TransitionState.IDLE


def test_incoming_failure_is_a_no_op_when_not_switching():
    manager, overlay, _advances, events = _manager(target=MediaType.VIDEO)
    assert manager.incoming_media_failed("x") is None  # idle
    _covered_waiting_for_video(manager, overlay)
    manager.media_ready(MediaType.VIDEO)
    assert manager.state == TransitionState.INCOMING
    reveal = overlay.incoming_callback
    assert manager.incoming_media_failed("x") is None  # revealing
    assert overlay.incoming_callback is reveal
    _finish_incoming(overlay)
    assert manager.state == TransitionState.IDLE
    assert manager.incoming_media_failed("x") is None  # completed
    assert "incoming_media_failed" not in [name for name, _d in events]
    assert "cancelled" not in [name for name, _d in events]


def test_incoming_failure_is_a_no_op_after_shutdown():
    manager, overlay, _advances, events = _manager(target=MediaType.VIDEO)
    _covered_waiting_for_video(manager, overlay)
    manager.shutdown()
    assert manager.incoming_media_failed("x") is None
    assert "incoming_media_failed" not in [name for name, _d in events]
