"""Regression coverage for Phase 2A's dual-video state/orchestration."""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtWidgets

from billsmusic.media_type import MediaType
from billsmusic.video_dual_transition import (
    DualDeckController,
    DualDeckState,
    DualTransitionPreferences,
    DualVideoTransitionEngine,
    SecondaryIdentity,
)


@pytest.fixture(scope="module", autouse=True)
def qapplication():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def _identity(row=3, path="video_b.mp4", epoch=0, media_type=MediaType.VIDEO):
    """Phase D: identity is (queue_token, expected_source).

    The `row=` and `epoch=` keywords are kept only so the existing call
    sites read unchanged -- across this suite `row=` was never a position
    that mattered, it was simply "a different target", which is now
    exactly what a different TOKEN means. `epoch=` is accepted and
    ignored: an unrelated queue mutation no longer invalidates a preload,
    which is the behavioural point of this change.
    """
    return SecondaryIdentity(
        queue_token=row, expected_source=path, media_type=media_type,
    )


def _progress_details(engine, **extra):
    """A secondary_preload_progress details dict carrying the engine's own
    currently-active preload identity (see DualVideoTransitionEngine.
    _preload_envelope_matches_active, 2026-08-24 correctness hardening) --
    on_secondary_preload_progress() now rejects any details lacking a
    matching preload_id/source_hash, so every test simulating a real
    in-flight preload's progress must supply these."""
    return {
        "preload_id": engine.controller.preload_id,
        "source_hash": engine.controller.preload_source_hash,
        **extra,
    }


# -- DualTransitionPreferences.from_config: gpu_effect parsing --------------

def test_from_config_reads_gpu_effect():
    preferences = DualTransitionPreferences.from_config(
        {"video_gpu_transition_effect": "Wipe Right"}
    )
    assert preferences.gpu_effect == "Wipe Right"


def test_from_config_defaults_gpu_effect_to_cross_dissolve():
    preferences = DualTransitionPreferences.from_config({})
    assert preferences.gpu_effect == "Cross Dissolve"


def test_from_config_falls_back_for_unrecognised_gpu_effect():
    # Simulates an old config.json (or one hand-edited/corrupted) naming an
    # effect this build doesn't recognise -- must not raise, and must not
    # silently pass the bogus string through to the shader layer.
    preferences = DualTransitionPreferences.from_config(
        {"video_gpu_transition_effect": "Not A Real Effect"}
    )
    assert preferences.gpu_effect == "Cross Dissolve"


def test_from_config_accepts_random_gpu():
    preferences = DualTransitionPreferences.from_config(
        {"video_gpu_transition_effect": "Random GPU"}
    )
    assert preferences.gpu_effect == "Random GPU"


@pytest.mark.parametrize("style", ["Random GPU Smooth", "Random GPU Energetic"])
def test_from_config_accepts_phase_2c_random_pools(style):
    preferences = DualTransitionPreferences.from_config(
        {"video_gpu_transition_effect": style}
    )
    assert preferences.gpu_effect == style


def test_from_config_accepts_each_phase_2c_named_effect():
    for effect in ("RGB Glitch", "Pixel Dissolve", "Luma Dissolve", "Film Burn", "Zoom Blur", "Diagonal Wipe"):
        preferences = DualTransitionPreferences.from_config(
            {"video_gpu_transition_effect": effect}
        )
        assert preferences.gpu_effect == effect


# -- DualDeckController: pure state machine ----------------------------------

def test_controller_happy_path_preload_ready_commit_promote_complete():
    controller = DualDeckController()
    identity = _identity()
    assert controller.begin_preload(identity) is True
    assert controller.state == DualDeckState.PRELOADING_SECONDARY
    assert controller.mark_secondary_ready() is True
    assert controller.state == DualDeckState.SECONDARY_READY
    assert controller.is_ready_for(identity) is True
    assert controller.begin_commit("automatic") is True
    assert controller.state == DualDeckState.TRANSITIONING
    assert controller.begin_promotion() is True
    assert controller.state == DualDeckState.PROMOTING_SECONDARY
    assert controller.begin_cleanup() is True
    assert controller.state == DualDeckState.CLEANING_PRIMARY
    controller.complete()
    assert controller.state == DualDeckState.IDLE
    assert controller.identity is None


def test_controller_refuses_double_preload():
    controller = DualDeckController()
    assert controller.begin_preload(_identity()) is True
    assert controller.begin_preload(_identity(path="other.mp4")) is False


def test_controller_is_ready_for_requires_matching_identity():
    controller = DualDeckController()
    identity = _identity()
    controller.begin_preload(identity)
    controller.mark_secondary_ready()
    assert controller.is_ready_for(_identity(path="different.mp4")) is False
    assert controller.is_ready_for(None) is False
    assert controller.is_ready_for(identity) is True


def test_controller_is_stale_checks_token_and_source():
    """Phase D: token locates, source validates -- and an unrelated queue
    mutation (which used to bump the epoch) no longer invalidates."""
    controller = DualDeckController()
    controller.begin_preload(_identity(row=7, path="video_b.mp4"))

    # Same token, same content -> still valid.
    assert controller.is_stale(_identity(row=7, path="video_b.mp4")) is False
    # Same token, content replaced in place (missing-track repair) -> stale.
    assert controller.is_stale(_identity(row=7, path="different.mp4")) is True
    # A different queue entry leads now -> stale.
    assert controller.is_stale(_identity(row=8, path="video_b.mp4")) is True
    # Nothing queued at all -> stale.
    assert controller.is_stale(None) is True


def test_controller_cancel_refuses_once_committed():
    controller = DualDeckController()
    controller.begin_preload(_identity())
    controller.mark_secondary_ready()
    controller.begin_commit("automatic")
    assert controller.cancel() is False
    assert controller.state == DualDeckState.TRANSITIONING


def test_controller_cancel_succeeds_before_commitment():
    controller = DualDeckController()
    controller.begin_preload(_identity())
    assert controller.cancel() is True
    assert controller.state == DualDeckState.IDLE


def test_controller_force_abort_works_even_after_commitment():
    controller = DualDeckController()
    controller.begin_preload(_identity())
    controller.mark_secondary_ready()
    controller.begin_commit("automatic")
    controller.begin_promotion()
    assert controller.force_abort() is True
    assert controller.state == DualDeckState.IDLE


def test_controller_reset_for_media_clears_stale_secondary():
    controller = DualDeckController()
    controller.begin_preload(_identity())
    controller.reset_for_media()
    assert controller.state == DualDeckState.IDLE
    assert controller.identity is None


def test_controller_should_start_preload_timing():
    assert DualDeckController.should_start_preload(4000, 10000, 6.0) is True
    assert DualDeckController.should_start_preload(3000, 10000, 6.0) is False
    assert DualDeckController.should_start_preload(1000, 0, 6.0) is False


def test_controller_should_cancel_preload_for_seek_has_slack():
    # 8s remaining, lead=6s -> within (6+3)s slack, do not cancel
    assert DualDeckController.should_cancel_preload_for_seek(2000, 10000, 6.0) is False
    # seek way back: 19s remaining, well past the slack
    assert DualDeckController.should_cancel_preload_for_seek(1000, 20000, 6.0) is True


# -- DualVideoTransitionEngine: Qt orchestration around a fake backend -------

class FakeDualBackend:
    def __init__(self, *, preload_succeeds=True, commit_succeeds=True):
        self.preload_succeeds = preload_succeeds
        self.commit_succeeds = commit_succeeds
        self.preloaded_paths = []
        self.preloaded_start_positions_ms = []
        self.cancelled_count = 0
        self.committed_durations = []
        self.committed_effects = []
        self.committed_seeds = []
        self.committed_audio_crossfade_enabled = []
        self.committed_audio_crossfade_curves = []
        self.paused_count = 0
        self.resumed_count = 0
        # Mirrors video_backend.py's real VideoPreloadHandle/transition_id
        # minting (2026-08-24 correctness hardening) -- a fresh id every
        # successful call, never reused, so engine-side tests can exercise
        # real identity propagation/mismatch scenarios rather than only
        # ever seeing None.
        self._next_preload_id = 0
        self._next_transition_id = 0
        self.active_preload_id = None
        self.active_preload_source_hash = None
        self.active_transition_id = None

    def preload_secondary(self, path, *, start_position_ms=0):
        self.preloaded_paths.append(path)
        self.preloaded_start_positions_ms.append(start_position_ms)
        if self.preload_succeeds:
            self._next_preload_id += 1
            self.active_preload_id = self._next_preload_id
            self.active_preload_source_hash = f"hash:{path}"
        return self.preload_succeeds

    def cancel_secondary(self):
        self.cancelled_count += 1
        self.active_preload_id = None
        self.active_preload_source_hash = None

    def commit_dual_transition(
        self, duration_ms, transition_type="Cross Dissolve", seed=0.0, *,
        audio_crossfade_enabled=False, audio_crossfade_curve="Equal Power",
    ):
        self.committed_durations.append(duration_ms)
        self.committed_effects.append(transition_type)
        self.committed_seeds.append(seed)
        self.committed_audio_crossfade_enabled.append(audio_crossfade_enabled)
        self.committed_audio_crossfade_curves.append(audio_crossfade_curve)
        if self.commit_succeeds:
            self._next_transition_id += 1
            self.active_transition_id = self._next_transition_id
        return self.commit_succeeds

    def pause_dual_transition(self):
        self.paused_count += 1

    def resume_dual_transition(self):
        self.resumed_count += 1


def _engine(
    *, backend=None, preferences=None, identity=None,
    advance_target=MediaType.VIDEO,
    outro_transition_point_lookup=None, intro_transition_point_lookup=None,
    current_primary_path=None,
):
    backend = backend or FakeDualBackend()
    advances = []
    events = []
    pre_advances = []
    identity_holder = {"value": identity if identity is not None else _identity()}
    primary_path_holder = {"value": current_primary_path}

    def advance(trigger):
        advances.append(trigger)
        return advance_target

    def identity_provider():
        return identity_holder["value"]

    def diagnostic(event, details):
        events.append((event, dict(details)))

    def pre_advance(path):
        pre_advances.append(path)

    def primary_path_provider():
        return primary_path_holder["value"]

    engine = DualVideoTransitionEngine(
        None, backend, advance, identity_provider, diagnostic, pre_advance,
        outro_transition_point_lookup=outro_transition_point_lookup,
        intro_transition_point_lookup=intro_transition_point_lookup,
        current_primary_path_provider=(
            primary_path_provider if current_primary_path is not None else None
        ),
    )
    engine.configure(preferences or DualTransitionPreferences(
        enabled=True, preload_lead_seconds=6.0,
    ))
    return engine, backend, identity_holder, advances, events, pre_advances


def test_disabled_preference_never_preloads():
    engine, backend, _identity_holder, _advances, _events, _pre = _engine(
        preferences=DualTransitionPreferences(enabled=False),
    )
    engine.observe_position(9000, 10000, MediaType.VIDEO)
    assert backend.preloaded_paths == []
    assert engine.state == DualDeckState.IDLE


def test_preload_starts_when_lead_crossed_and_does_not_repeat():
    engine, backend, _identity_holder, _advances, _events, _pre = _engine()
    engine.observe_position(3000, 10000, MediaType.VIDEO)  # 7s remaining
    assert backend.preloaded_paths == []
    engine.observe_position(4500, 10000, MediaType.VIDEO)  # 5.5s remaining
    assert backend.preloaded_paths == ["video_b.mp4"]
    assert engine.state == DualDeckState.PRELOADING_SECONDARY
    engine.observe_position(4600, 10000, MediaType.VIDEO)
    assert backend.preloaded_paths == ["video_b.mp4"]


def test_preload_failure_returns_to_idle():
    backend = FakeDualBackend(preload_succeeds=False)
    engine, backend, _identity_holder, _advances, events, _pre = _engine(backend=backend)
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    assert engine.state == DualDeckState.IDLE
    assert any(event == "compositor_failure" for event, _ in events)


def test_secondary_ready_signal_moves_to_ready_when_identity_matches():
    engine, backend, _identity_holder, _advances, events, _pre = _engine()
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_ready()
    assert engine.state == DualDeckState.SECONDARY_READY
    assert any(event == "secondary_ready" for event, _ in events)


def test_secondary_ready_invalidated_by_identity_change():
    engine, backend, identity_holder, _advances, events, _pre = _engine()
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    # Phase D: invalidation now means the ENTRY changed, not that some
    # unrelated queue edit bumped a global counter. A different token
    # leading is a genuine identity change.
    identity_holder["value"] = _identity(row=99, path="video_b.mp4")
    engine.on_secondary_ready()
    assert engine.state == DualDeckState.IDLE
    assert backend.cancelled_count == 1
    assert any(event == "secondary_invalidated_by_queue_change" for event, _ in events)


def test_staleness_identity_provider_used_only_for_reverification():
    """Regression for a real GUI-thread stall traced to on_secondary_ready()'s
    staleness check incidentally re-triggering window.py's (since-removed
    prefetch and still-present) Smart Transition Points analysis side
    effects (see CODEX_HANDOFF.md).
    identity_provider (which may carry side effects in window.py's real
    wiring) must be used only where a *new* preload is genuinely being
    requested -- observe_position()'s lead-window trigger; the ready-time
    and commit-time re-verifications must go through the side-effect-free
    staleness_identity_provider instead, with no change to the happy
    path's outcome."""
    backend = FakeDualBackend()
    identity_calls = []
    staleness_calls = []
    identity_value = _identity()

    def identity_provider():
        identity_calls.append(1)
        return identity_value

    def staleness_identity_provider():
        staleness_calls.append(1)
        return identity_value

    advances = []
    events = []
    pre_advances = []

    def advance(trigger):
        advances.append(trigger)
        return MediaType.VIDEO

    def diagnostic(event, details):
        events.append((event, dict(details)))

    def pre_advance(path):
        pre_advances.append(path)

    engine = DualVideoTransitionEngine(
        None, backend, advance, identity_provider, diagnostic, pre_advance,
        staleness_identity_provider=staleness_identity_provider,
    )
    engine.configure(DualTransitionPreferences(enabled=True, preload_lead_seconds=6.0))

    # The one legitimate site identity_provider's real-world side effects
    # are meant to run at: the lead-window preload trigger.
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    assert backend.preloaded_paths == ["video_b.mp4"]
    assert len(identity_calls) == 1
    assert len(staleness_calls) == 0

    # Ready-time staleness re-check must use the side-effect-free provider.
    engine.on_secondary_ready()
    assert engine.state == DualDeckState.SECONDARY_READY
    assert len(identity_calls) == 1
    assert len(staleness_calls) == 1

    # Commit-time staleness re-check must also use it -- and the happy
    # path must still work identically to the single-provider case.
    assert engine.try_commit("manual", 1.0) is True
    assert pre_advances == ["video_b.mp4"]
    assert advances == ["manual"]
    assert len(identity_calls) == 1
    assert len(staleness_calls) == 2


def test_staleness_identity_provider_defaults_to_identity_provider():
    """Backward-compatibility contract: callers that don't pass
    staleness_identity_provider (every existing caller/test in this file)
    get the pre-fix behaviour of reusing identity_provider everywhere."""
    engine, backend, _identity_holder, _advances, _events, _pre = _engine()
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_ready()
    assert engine.state == DualDeckState.SECONDARY_READY
    assert engine.try_commit("manual", 1.0) is True


def test_secondary_failed_signal_resets_to_idle():
    engine, backend, _identity_holder, _advances, events, _pre = _engine()
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_failed("video_decode_error")
    assert engine.state == DualDeckState.IDLE


def test_preload_ready_timeout_tears_down_and_falls_back():
    engine, backend, _identity_holder, _advances, events, _pre = _engine(
        preferences=DualTransitionPreferences(
            enabled=True, preload_lead_seconds=6.0, ready_timeout_ms=500,
        ),
    )
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    assert engine.state == DualDeckState.PRELOADING_SECONDARY
    engine._on_ready_timeout()
    assert engine.state == DualDeckState.IDLE
    assert backend.cancelled_count == 1
    assert any(event == "secondary_preload_timeout" for event, _ in events)
    timeout_details = next(d for e, d in events if e == "secondary_preload_timeout")
    assert timeout_details["retried"] is False


# -- Bounded early retry (stalled-after-progress preloads) ------------------

def test_bounded_retry_fires_once_after_progress_then_stall():
    engine, backend, _identity_holder, _advances, events, _pre = _engine(
        preferences=DualTransitionPreferences(
            enabled=True, preload_lead_seconds=6.0, ready_timeout_ms=500,
        ),
    )
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    assert backend.preloaded_paths == ["video_b.mp4"]
    engine.on_secondary_preload_progress(_progress_details(engine, media_status_name="BufferingMedia"))
    engine._on_ready_timeout()
    # Retried, not given up: still preloading, backend re-issued preload,
    # the commit deadline timer was never touched (unarmed either way,
    # since we never reached SECONDARY_READY).
    assert engine.state == DualDeckState.PRELOADING_SECONDARY
    assert backend.preloaded_paths == ["video_b.mp4", "video_b.mp4"]
    assert backend.cancelled_count == 1  # the stalled attempt was torn down first
    assert not engine._deadline_timer.isActive()
    assert any(event == "secondary_preload_retry" for event, _ in events)

    # A second stall (after the retry) must give up for real -- exactly one
    # retry per preload, never indefinite.
    engine._on_ready_timeout()
    assert engine.state == DualDeckState.IDLE
    assert backend.preloaded_paths == ["video_b.mp4", "video_b.mp4"]  # no third attempt
    timeout_details = [d for e, d in events if e == "secondary_preload_timeout"][-1]
    assert timeout_details["retried"] is True


def test_retry_skipped_when_no_progress_was_ever_seen():
    """Unchanged from before the retry existed: a preload that never even
    reported LoadingMedia/LoadedMedia/BufferingMedia isn't worth retrying --
    it fails immediately, same as test_preload_ready_timeout_tears_down_
    and_falls_back above."""
    engine, backend, _identity_holder, _advances, events, _pre = _engine(
        preferences=DualTransitionPreferences(
            enabled=True, preload_lead_seconds=6.0, ready_timeout_ms=500,
        ),
    )
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine._on_ready_timeout()
    assert engine.state == DualDeckState.IDLE
    assert backend.preloaded_paths == ["video_b.mp4"]  # no retry attempt


def test_retry_skipped_when_insufficient_hard_deadline_budget_remains():
    engine, backend, _identity_holder, _advances, events, _pre = _engine(
        preferences=DualTransitionPreferences(
            enabled=True, preload_lead_seconds=6.0, ready_timeout_ms=500,
        ),
    )
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_preload_progress(_progress_details(engine, media_status_name="BufferingMedia"))
    # Simulate having almost no hard-deadline budget left (e.g. adaptive
    # extensions already pushed the ready deadline out to nearly the cap).
    engine._preload_hard_deadline_monotonic = time.monotonic() + 0.05
    engine._on_ready_timeout()
    assert engine.state == DualDeckState.IDLE
    assert backend.preloaded_paths == ["video_b.mp4"]  # no retry attempt
    timeout_details = next(d for e, d in events if e == "secondary_preload_timeout")
    assert timeout_details["retried"] is False


def test_retry_falls_back_cleanly_when_backend_declines_the_reissue():
    engine, backend, _identity_holder, _advances, events, _pre = _engine(
        preferences=DualTransitionPreferences(
            enabled=True, preload_lead_seconds=6.0, ready_timeout_ms=500,
        ),
    )
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_preload_progress(_progress_details(engine, media_status_name="BufferingMedia"))
    backend.preload_succeeds = False
    engine._on_ready_timeout()
    assert engine.state == DualDeckState.IDLE
    assert any(
        event == "compositor_failure" and details.get("stage") == "preload_retry"
        for event, details in events
    )


def test_retry_reschedules_ready_timer_bounded_by_remaining_hard_cap():
    engine, backend, _identity_holder, _advances, events, _pre = _engine(
        preferences=DualTransitionPreferences(
            enabled=True, preload_lead_seconds=6.0, ready_timeout_ms=500,
            preload_max_wait_ms=4000,
        ),
    )
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_preload_progress(_progress_details(engine, media_status_name="BufferingMedia"))
    hard_deadline_before = engine._preload_hard_deadline_monotonic
    started_before = engine._preload_started_monotonic
    engine._on_ready_timeout()
    assert engine.state == DualDeckState.PRELOADING_SECONDARY
    # The hard cap and original start time are untouched by the retry --
    # it spends down the same overall budget, never gets a fresh one.
    assert engine._preload_hard_deadline_monotonic == hard_deadline_before
    assert engine._preload_started_monotonic == started_before
    assert engine._ready_timer.isActive()


# -- Stage B: adaptive bounded preload timeout ------------------------------

def test_secondary_preload_progress_extends_ready_timer_on_genuine_progress():
    engine, _backend, _identity_holder, _advances, events, _pre = _engine(
        preferences=DualTransitionPreferences(
            enabled=True, preload_lead_seconds=6.0, ready_timeout_ms=500,
            preload_max_wait_ms=10000, preload_progress_extension_ms=2000,
        ),
    )
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    assert engine.state == DualDeckState.PRELOADING_SECONDARY
    deadline_before = engine._ready_deadline_monotonic
    engine.on_secondary_preload_progress(_progress_details(engine, media_status_name="BufferingMedia"))
    assert engine._ready_deadline_monotonic > deadline_before
    assert engine._ready_timer.isActive()
    assert any(event == "preload_timeout_extended" for event, _ in events)


def test_secondary_preload_progress_ignores_non_progress_status():
    engine, _backend, _identity_holder, _advances, events, _pre = _engine(
        preferences=DualTransitionPreferences(
            enabled=True, preload_lead_seconds=6.0, ready_timeout_ms=500,
            preload_max_wait_ms=10000, preload_progress_extension_ms=2000,
        ),
    )
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    deadline_before = engine._ready_deadline_monotonic
    for status in ("StalledMedia", "InvalidMedia", "EndOfMedia", "NoMedia"):
        engine.on_secondary_preload_progress(_progress_details(engine, media_status_name=status))
    assert engine._ready_deadline_monotonic == deadline_before
    assert not any(event == "preload_timeout_extended" for event, _ in events)


def test_secondary_preload_progress_never_extends_past_hard_cap():
    engine, _backend, _identity_holder, _advances, _events, _pre = _engine(
        preferences=DualTransitionPreferences(
            enabled=True, preload_lead_seconds=6.0, ready_timeout_ms=500,
            preload_max_wait_ms=1500, preload_progress_extension_ms=5000,
        ),
    )
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    hard_deadline = engine._preload_hard_deadline_monotonic
    for _ in range(5):
        engine.on_secondary_preload_progress(_progress_details(engine, media_status_name="BufferingMedia"))
    assert engine._ready_deadline_monotonic <= hard_deadline
    assert engine._preload_hard_deadline_monotonic == hard_deadline


def test_secondary_preload_progress_noop_outside_preloading_state():
    engine, _backend, _identity_holder, _advances, events, _pre = _engine()
    # Never entered PRELOADING_SECONDARY at all -- must not raise, must not
    # arm anything.
    engine.on_secondary_preload_progress(_progress_details(engine, media_status_name="BufferingMedia"))
    assert engine._ready_deadline_monotonic is None
    assert not any(event == "preload_timeout_extended" for event, _ in events)


def test_secondary_preload_progress_noop_once_ready():
    engine, _backend, _identity_holder, _advances, events, _pre = _engine()
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_ready()
    assert engine._ready_deadline_monotonic is None
    assert not engine._ready_timer.isActive()
    engine.on_secondary_preload_progress(_progress_details(engine, media_status_name="BufferingMedia"))
    assert engine._ready_deadline_monotonic is None
    assert not engine._ready_timer.isActive()
    assert not any(event == "preload_timeout_extended" for event, _ in events)


def test_automatic_commit_succeeds_when_ready_and_advances_queue_once():
    engine, backend, _identity_holder, advances, events, pre_advances = _engine()
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_ready()
    assert engine.try_commit("automatic", 1.0) is True
    assert backend.committed_durations == [1000]
    assert pre_advances == ["video_b.mp4"]
    assert advances == ["automatic"]
    # Still TRANSITIONING -- the queue has advanced (the one commitment
    # point) but the visual dissolve is still running in the subprocess;
    # promotion only happens once on_dual_transition_complete() fires.
    assert engine.state == DualDeckState.TRANSITIONING
    assert any(event == "gpu_transition_started" for event, _ in events)
    assert any(event == "transition_commitment_point_reached" for event, _ in events)
    engine.on_dual_transition_complete()
    assert engine.state == DualDeckState.IDLE


def test_try_commit_declines_when_not_ready():
    engine, backend, _identity_holder, advances, _events, _pre = _engine()
    assert engine.try_commit("manual", 0.5) is False
    assert backend.committed_durations == []
    assert advances == []


def test_manual_commit_with_ready_secondary_succeeds():
    engine, backend, _identity_holder, advances, _events, _pre = _engine()
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_ready()
    assert engine.try_commit("manual", 0.5) is True
    assert backend.committed_durations == [500]
    assert advances == ["manual"]


def test_debug_sample_held_secondary_diagnostic_has_been_removed():
    # Hardening (2026-08-25): BILLSMUSIC_DEBUG_SAMPLE_HELD_SECONDARY used to
    # make try_commit() call a debug_sample_deck() IPC command via
    # _maybe_debug_sample_held_secondary() before every real commit.
    # Real-device testing proved firing that call during a live commit
    # could crash the GPU subprocess (its async grabToImage() restore
    # could land after the real commit's own progress animation had
    # started driving the same blend properties), and the tool had never
    # once produced a usable result in a real attempt -- so rather than
    # gating it further, it has been removed entirely, method and IPC
    # command both. This is a permanent regression pin: neither the
    # wrapper method nor the underlying primitive may be reintroduced
    # onto the engine or a production backend.
    from billsmusic.video_backend import QtVideoPlaybackBackend
    from billsmusic.video_subprocess import GpuDualDeckVideoSubprocessController

    engine, _backend, _identity_holder, _advances, _events, _pre = _engine()
    assert not hasattr(engine, "_maybe_debug_sample_held_secondary")
    assert not hasattr(QtVideoPlaybackBackend, "debug_sample_deck")
    assert not hasattr(GpuDualDeckVideoSubprocessController, "_debug_sample_deck")


def test_try_commit_ignores_the_old_debug_sample_env_var_regardless_of_value(monkeypatch):
    # Regression pin: even a stale BILLSMUSIC_DEBUG_SAMPLE_HELD_SECONDARY=1
    # left over on a real machine from before this fix (exactly what
    # happened on the dev machine that found the crash) must have zero
    # effect on the normal commit path -- nothing reads this env var any
    # more, so setting it changes nothing about the commit outcome.
    monkeypatch.setenv("BILLSMUSIC_DEBUG_SAMPLE_HELD_SECONDARY", "1")
    engine, backend, _identity_holder, advances, _events, _pre = _engine()
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_ready()
    assert engine.try_commit("automatic", 1.0) is True
    assert backend.committed_durations == [1000]
    assert advances == ["automatic"]


def test_repeated_commit_attempts_do_not_double_advance():
    engine, backend, _identity_holder, advances, _events, _pre = _engine()
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_ready()
    assert engine.try_commit("automatic", 1.0) is True
    # A second attempt while already TRANSITIONING must decline, not
    # double-advance the queue or re-commit.
    assert engine.try_commit("automatic", 1.0) is False
    assert advances == ["automatic"]
    assert backend.committed_durations == [1000]


def test_commit_time_staleness_invalidates_and_falls_back():
    engine, backend, identity_holder, advances, events, _pre = _engine()
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_ready()
    # Same token, content replaced in place (the missing-track-repair
    # case) -- the prepared media no longer belongs to that entry.
    identity_holder["value"] = _identity(path="repaired.mp4")
    assert engine.try_commit("automatic", 1.0) is False
    assert advances == []
    assert backend.cancelled_count == 1
    assert engine.state == DualDeckState.IDLE


def test_commit_failure_at_backend_force_aborts():
    backend = FakeDualBackend(commit_succeeds=False)
    engine, backend, _identity_holder, advances, events, _pre = _engine(backend=backend)
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_ready()
    assert engine.try_commit("automatic", 1.0) is False
    assert advances == []
    assert engine.state == DualDeckState.IDLE
    assert backend.cancelled_count == 1
    assert any(event == "compositor_failure" for event, _ in events)


def test_primary_failed_unsticks_an_already_committed_transition():
    # Real-device bug (2026-08-24): a video subprocess crash/watchdog
    # failure during a *committed* transition (TRANSITIONING) previously
    # left this engine stuck there for the rest of the session --
    # playback_stopped()/cancel() deliberately refuse once committed (see
    # COMMITTED_STATES), and nothing ever called primary_failed() (window.py
    # now does, from backend.dual_transition_failed -- see
    # test_video_fullscreen.py). force_abort() must work unconditionally,
    # regardless of commitment phase, unlike the cooperative cancel() path.
    engine, backend, _identity_holder, advances, events, _pre = _engine()
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_ready()
    assert engine.try_commit("automatic", 1.0) is True
    assert engine.state == DualDeckState.TRANSITIONING
    transition_id = engine.controller.transition_id
    assert transition_id is not None

    engine.primary_failed(transition_id, "video_subprocess_error")

    assert engine.state == DualDeckState.IDLE
    assert any(event == "compositor_failure" for event, _ in events)
    # The queue already advanced exactly once at commit time -- primary_
    # failed() must not touch that again.
    assert advances == ["automatic"]


def test_primary_failed_refuses_a_stale_transition_id():
    # Correctness hardening (2026-08-24): a failure report for an already-
    # superseded transition must never abort whatever *newer* transition
    # is actually committed now.
    engine, backend, _identity_holder, advances, events, _pre = _engine()
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_ready()
    assert engine.try_commit("automatic", 1.0) is True
    real_transition_id = engine.controller.transition_id

    engine.primary_failed((real_transition_id or 0) + 999, "video_subprocess_error")

    assert engine.state == DualDeckState.TRANSITIONING
    assert not any(event == "compositor_failure" for event, _ in events)


def test_primary_failed_is_a_safe_no_op_when_already_idle():
    engine, backend, _identity_holder, advances, _events, _pre = _engine()
    assert engine.state == DualDeckState.IDLE

    engine.primary_failed(None, "video_subprocess_error")

    assert engine.state == DualDeckState.IDLE
    assert advances == []


# -- deadline timer: v1.0.53 fix for EOF-only commits -----------------------
#
# v1.0.52 diagnostics from real near-end playback showed gpu_transition_started
# and track_completed sharing the exact same timestamp in every transition of
# one session -- proving every commit happened via
# VideoTransitionManager.handle_natural_end()'s end-of-media fallback, never
# from observe_position()'s position-tick-driven automatic check. By the time
# a natural end-of-media signal fires, the outgoing deck has no live frame
# left to contribute, so what should be a genuine A+B cross-dissolve showed as
# a solid black gap instead. Root cause: the automatic check only ever ran
# from whatever position tick happened to land, and if ticks stopped arriving
# before one landed inside automatic_lead_seconds of the end (exactly what
# happens near a real video's own end), try_commit() was simply never
# attempted until end-of-media. The deadline timer below is scheduled the
# moment secondary becomes ready (and re-scheduled on every subsequent tick,
# self-correcting for drift) to an absolute wall-clock deadline, so a missing
# final tick can no longer skip the commit.

def test_schedule_deadline_converts_lead_seconds_to_milliseconds():
    # Explicit dimensional regression: duration_ms/position_ms are
    # milliseconds, automatic_lead_seconds is seconds. A units bug that
    # subtracted the raw seconds value from a millisecond quantity (instead
    # of first multiplying by 1000) would produce 4999ms here, not 4000ms --
    # calls the real _schedule_deadline()/reads the real QTimer, not a
    # reimplementation of the formula, so this fails if the *actual* code
    # ever regresses this conversion.
    engine, _backend, _identity_holder, _advances, _events, _pre = _engine(
        preferences=DualTransitionPreferences(
            enabled=True, preload_lead_seconds=6.0, automatic_lead_seconds=1.0,
        ),
    )
    engine._schedule_deadline(position_ms=115000, duration_ms=120000)
    assert engine._deadline_timer.interval() == 4000
    assert engine._deadline_timer.interval() != 4999


def test_deadline_timer_arms_the_moment_secondary_becomes_ready():
    engine, backend, _identity_holder, _advances, _events, _pre = _engine(
        preferences=DualTransitionPreferences(
            enabled=True, preload_lead_seconds=6.0,
            automatic_lead_seconds=1.0, duration_seconds=1.0,
        ),
    )
    engine.observe_position(4500, 10000, MediaType.VIDEO)  # 5.5s remaining
    assert engine._deadline_timer.isActive() is False  # not armed during preload
    engine.on_secondary_ready()
    assert engine.state == DualDeckState.SECONDARY_READY
    # Armed immediately from the last known position/duration, not waiting
    # for a further tick: (10000 - 4500) - 1000ms lead = 4500ms.
    assert engine._deadline_timer.isActive() is True
    assert engine._deadline_timer.interval() == 4500
    assert backend.committed_durations == []


def test_deadline_timer_commits_before_natural_end_when_ticks_stop_outside_lead_window():
    engine, backend, _identity_holder, advances, events, _pre = _engine(
        preferences=DualTransitionPreferences(
            enabled=True, preload_lead_seconds=6.0,
            automatic_lead_seconds=1.0, duration_seconds=1.0,
        ),
    )
    engine.observe_position(4500, 10000, MediaType.VIDEO)  # 5.5s remaining -> preload starts
    engine.on_secondary_ready()
    # One more tick arrives, but still well outside the 1s automatic lead
    # window (4s remaining) -- the position-tick-driven automatic check in
    # VideoTransitionManager.observe_position() would not fire from this
    # tick either, which is exactly the failure mode this timer closes.
    engine.observe_position(6000, 10000, MediaType.VIDEO)  # 4s remaining
    assert backend.committed_durations == []  # not committed yet
    assert engine._deadline_timer.isActive() is True
    assert engine._deadline_timer.interval() == 3000  # (10000-6000) - 1000ms lead
    # No further position update ever arrives, and the outgoing deck's own
    # end-of-media has not fired either -- fire the deadline directly,
    # standing in for real wall-clock time elapsing with no further Qt
    # position signal (the exact scenario reported).
    engine._on_deadline_timer_fired()
    assert backend.committed_durations == [1000]
    assert advances == ["automatic"]
    assert engine.state == DualDeckState.TRANSITIONING
    assert any(event == "gpu_transition_started" for event, _ in events)


def test_deadline_timer_pauses_and_resumes_with_playback():
    # The deadline is a wall-clock timer derived from position/duration --
    # it must stop counting down while paused (position isn't advancing)
    # and resume fresh from the last known position, not fire early mid-pause.
    engine, backend, _identity_holder, _advances, _events, _pre = _engine()
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_ready()
    assert engine._deadline_timer.isActive() is True
    engine.playback_paused(True)
    assert engine._deadline_timer.isActive() is False
    assert backend.committed_durations == []
    engine.playback_paused(False)
    assert engine._deadline_timer.isActive() is True


def test_deadline_timer_stops_on_seek_cancel():
    engine, backend, _identity_holder, _advances, _events, _pre = _engine()
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_ready()
    assert engine._deadline_timer.isActive() is True
    engine.observe_position(500, 10000, MediaType.VIDEO)  # seek back to start
    assert engine.state == DualDeckState.IDLE
    assert engine._deadline_timer.isActive() is False


# -- Smart Video Transition Points: deadline integration --------------------
#
# Additive substitution inside _schedule_deadline's existing formula (see
# that method's own docstring in video_dual_transition.py) -- these tests
# exercise the substitution itself using injected fake lookups, not real
# analysis (see test_video_transition_point_analyzer.py for the analyzer/
# cache/classifier tests, and test_video_backend_gpu_fixture_integration.py
# for the one real-subprocess verification this phase needed).

def test_smart_outro_end_substitutes_for_duration_in_deadline_formula():
    engine, backend, _identity_holder, _advances, _events, _pre = _engine(
        preferences=DualTransitionPreferences(
            enabled=True, preload_lead_seconds=6.0,
            automatic_lead_seconds=1.0, duration_seconds=1.0,
            avoid_black_outros=True,
        ),
        outro_transition_point_lookup=lambda path: 9000,
        current_primary_path="A.mp4",
    )
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_ready()
    # Same formula as the plain duration-based one, with 9000 substituted
    # for duration_ms=10000: (9000 - 4500) - 1000 = 3500.
    assert engine._deadline_timer.interval() == 3500


def test_smart_outro_lookup_unavailable_falls_back_to_normal_deadline():
    engine, backend, _identity_holder, _advances, _events, _pre = _engine(
        preferences=DualTransitionPreferences(
            enabled=True, preload_lead_seconds=6.0,
            automatic_lead_seconds=1.0, duration_seconds=1.0,
            avoid_black_outros=True,
        ),
        outro_transition_point_lookup=lambda path: None,  # uncached / analysis unavailable
        current_primary_path="A.mp4",
    )
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_ready()
    # Unmodified duration_ms=10000: (10000 - 4500) - 1000 = 4500.
    assert engine._deadline_timer.interval() == 4500


def test_smart_outro_ignored_when_preference_is_off_even_with_a_lookup():
    engine, backend, _identity_holder, _advances, _events, _pre = _engine(
        preferences=DualTransitionPreferences(
            enabled=True, preload_lead_seconds=6.0,
            automatic_lead_seconds=1.0, duration_seconds=1.0,
            avoid_black_outros=False,
        ),
        outro_transition_point_lookup=lambda path: 9000,
        current_primary_path="A.mp4",
    )
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_ready()
    assert engine._deadline_timer.interval() == 4500


def test_seek_into_detected_black_outro_fires_deadline_immediately():
    engine, backend, _identity_holder, advances, _events, _pre = _engine(
        preferences=DualTransitionPreferences(
            enabled=True, preload_lead_seconds=6.0,
            automatic_lead_seconds=1.0, duration_seconds=1.0,
            avoid_black_outros=True,
        ),
        outro_transition_point_lookup=lambda path: 9000,
        current_primary_path="A.mp4",
    )
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_ready()
    # User seeks forward into the already-detected black region (past the
    # smart endpoint, still well short of should_cancel_preload_for_seek's
    # "seeked backward away from the end" trigger) -- no separate seek
    # handler needed, the same substituted formula naturally clamps to 0.
    engine.observe_position(9200, 10000, MediaType.VIDEO)
    assert engine._deadline_timer.interval() == 0
    engine._on_deadline_timer_fired()
    assert engine.state == DualDeckState.TRANSITIONING
    assert advances == ["automatic"]


def test_short_clip_sanity_check_rejects_degenerate_smart_deadline():
    # automatic_lead_seconds=1.0 + duration_seconds=1.0 -> min playable
    # span floor of 4000ms; a "detected" smart endpoint of 3000ms is below
    # that floor and must be rejected, falling back to the real duration_ms.
    engine, backend, _identity_holder, _advances, _events, _pre = _engine(
        preferences=DualTransitionPreferences(
            enabled=True, preload_lead_seconds=5.0,
            automatic_lead_seconds=1.0, duration_seconds=1.0,
            avoid_black_outros=True,
        ),
        outro_transition_point_lookup=lambda path: 3000,
        current_primary_path="A.mp4",
    )
    engine.observe_position(3500, 8000, MediaType.VIDEO)
    engine.on_secondary_ready()
    # If the degenerate 3000ms value had been used: (3000-3500)-1000 would
    # clamp to 0. Falling back to the real duration_ms=8000 instead gives
    # (8000-3500)-1000=3500 -- the assertion below only passes if the
    # sanity check actually fired.
    assert engine._deadline_timer.interval() == 3500


def test_cancel_before_commitment_tears_down():
    engine, backend, _identity_holder, _advances, events, _pre = _engine()
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    assert engine.cancel("external_media_request") is True
    assert engine.state == DualDeckState.IDLE
    assert backend.cancelled_count == 1
    assert any(event == "dual_transition_cancelled" for event, _ in events)


def test_cancel_after_commitment_is_a_no_op():
    engine, backend, _identity_holder, advances, _events, _pre = _engine()
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_ready()
    engine.try_commit("automatic", 1.0)
    assert engine.cancel("external_media_request") is False
    assert engine.state == DualDeckState.TRANSITIONING


def test_playback_stopped_cancels_preload_but_not_committed_transition():
    engine, backend, _identity_holder, _advances, _events, _pre = _engine()
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.playback_stopped("stop")
    assert engine.state == DualDeckState.IDLE
    assert backend.cancelled_count == 1

    engine2, backend2, _identity_holder2, _advances2, _events2, _pre2 = _engine()
    engine2.observe_position(4500, 10000, MediaType.VIDEO)
    engine2.on_secondary_ready()
    engine2.try_commit("automatic", 1.0)
    engine2.playback_stopped("stop")
    assert engine2.state == DualDeckState.TRANSITIONING


def test_pause_forwarded_only_while_transitioning():
    engine, backend, _identity_holder, _advances, _events, _pre = _engine()
    engine.playback_paused(True)
    assert backend.paused_count == 0
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_ready()
    # Stage A: the secondary is already paused-and-held by the subprocess
    # itself the instant it's ready (see video_subprocess.py's
    # _hold_and_emit_secondary_ready) -- the app-level pause/resume forwarded
    # here is a completely separate concern (whole-transition pause/resume,
    # only meaningful once TRANSITIONING) and must never touch the held
    # secondary while merely SECONDARY_READY.
    engine.playback_paused(True)
    engine.playback_paused(False)
    assert backend.paused_count == 0
    assert backend.resumed_count == 0
    engine.try_commit("automatic", 1.0)
    engine.playback_paused(True)
    engine.playback_paused(False)
    assert backend.paused_count == 1
    assert backend.resumed_count == 1


def test_seek_backward_cancels_pending_preload():
    engine, backend, _identity_holder, _advances, events, _pre = _engine()
    engine.observe_position(4500, 10000, MediaType.VIDEO)  # preload starts
    assert engine.state == DualDeckState.PRELOADING_SECONDARY
    engine.observe_position(500, 10000, MediaType.VIDEO)  # seek back to start
    assert engine.state == DualDeckState.IDLE
    assert backend.cancelled_count == 1
    assert any(event == "dual_transition_cancelled" for event, _ in events)


def test_media_changed_tears_down_active_secondary():
    engine, backend, _identity_holder, _advances, _events, _pre = _engine()
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.media_changed()
    assert engine.state == DualDeckState.IDLE
    assert backend.cancelled_count == 1


def test_shutdown_during_preload_tears_down_and_ignores_later_calls():
    engine, backend, _identity_holder, advances, _events, _pre = _engine()
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.shutdown()
    assert backend.cancelled_count == 1
    # Late callbacks after shutdown must be inert.
    engine.on_secondary_ready()
    assert engine.try_commit("automatic", 1.0) is False
    assert advances == []


def test_shutdown_during_committed_transition_tears_down():
    engine, backend, _identity_holder, _advances, _events, _pre = _engine()
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_ready()
    engine.try_commit("automatic", 1.0)
    engine.shutdown()
    assert backend.cancelled_count == 1


def test_secondary_deck_never_marked_ready_from_wrong_state():
    controller = DualDeckController()
    assert controller.mark_secondary_ready() is False


# -- Phase 2B/2C: GPU effect selection ---------------------------------------

import random

from billsmusic.video_dual_transition import (
    GPU_ENERGETIC_EFFECTS,
    GPU_RANDOM_ENERGETIC_STYLE,
    GPU_RANDOM_SMOOTH_STYLE,
    GPU_RANDOM_STYLE,
    GPU_SMOOTH_EFFECTS,
    GPU_TRANSITION_EFFECTS,
)


@pytest.mark.parametrize("effect", GPU_TRANSITION_EFFECTS)
def test_select_gpu_effect_returns_the_configured_named_effect(effect):
    controller = DualDeckController()
    preferences = DualTransitionPreferences(gpu_effect=effect)
    assert controller.select_gpu_effect(preferences) == effect
    # A named (non-random) effect is stable across repeated calls -- there
    # is nothing to "avoid repeating" when the user picked one explicitly.
    assert controller.select_gpu_effect(preferences) == effect


def test_select_gpu_effect_falls_back_to_cross_dissolve_for_unknown_value():
    controller = DualDeckController()
    preferences = DualTransitionPreferences(gpu_effect="Some Future Effect")
    assert controller.select_gpu_effect(preferences) == "Cross Dissolve"


def test_select_gpu_effect_random_gpu_picks_from_the_known_pool():
    controller = DualDeckController(random.Random(7))
    preferences = DualTransitionPreferences(gpu_effect=GPU_RANDOM_STYLE)
    for _ in range(10):
        assert controller.select_gpu_effect(preferences) in GPU_TRANSITION_EFFECTS


def test_select_gpu_effect_random_gpu_never_immediately_repeats():
    controller = DualDeckController(random.Random(42))
    preferences = DualTransitionPreferences(gpu_effect=GPU_RANDOM_STYLE)
    previous = None
    for _ in range(50):
        selected = controller.select_gpu_effect(preferences)
        assert selected != previous
        previous = selected


# -- Phase 2C: curated random pools (Random GPU Smooth/Energetic) -----------

@pytest.mark.parametrize(
    "style,pool", [
        (GPU_RANDOM_SMOOTH_STYLE, GPU_SMOOTH_EFFECTS),
        (GPU_RANDOM_ENERGETIC_STYLE, GPU_ENERGETIC_EFFECTS),
    ],
)
def test_select_gpu_effect_curated_pool_only_picks_from_its_own_pool(style, pool):
    controller = DualDeckController(random.Random(11))
    preferences = DualTransitionPreferences(gpu_effect=style)
    for _ in range(20):
        assert controller.select_gpu_effect(preferences) in pool


@pytest.mark.parametrize("style", [GPU_RANDOM_SMOOTH_STYLE, GPU_RANDOM_ENERGETIC_STYLE])
def test_select_gpu_effect_curated_pool_never_immediately_repeats(style):
    controller = DualDeckController(random.Random(99))
    preferences = DualTransitionPreferences(gpu_effect=style)
    previous = None
    for _ in range(50):
        selected = controller.select_gpu_effect(preferences)
        assert selected != previous
        previous = selected


_PHASE_2B_EFFECTS = frozenset((
    "Cross Dissolve", "Push Left", "Push Right", "Wipe Left", "Wipe Right", "Zoom",
))


def test_select_gpu_effect_random_gpu_all_can_pick_a_phase_2c_effect():
    # Confirms the *original* "Random GPU" style's pool genuinely grew to
    # include the new Phase 2C effects, rather than silently staying
    # frozen at the 6 Phase 2B ones -- run enough draws that, with a
    # 12-effect pool, seeing at least one Phase 2C-only effect is a
    # near-certainty rather than a coincidence.
    controller = DualDeckController(random.Random(5))
    preferences = DualTransitionPreferences(gpu_effect=GPU_RANDOM_STYLE)
    phase_2c_only = set(GPU_TRANSITION_EFFECTS) - _PHASE_2B_EFFECTS
    seen = {controller.select_gpu_effect(preferences) for _ in range(60)}
    assert seen & phase_2c_only


# -- Phase 2C: per-transition seed generation --------------------------------

def test_generate_transition_seed_is_in_unit_range():
    controller = DualDeckController(random.Random(3))
    for _ in range(20):
        seed = controller.generate_transition_seed()
        assert 0.0 <= seed < 1.0


def test_generate_transition_seed_is_deterministic_for_a_given_rng_seed():
    a = DualDeckController(random.Random(123)).generate_transition_seed()
    b = DualDeckController(random.Random(123)).generate_transition_seed()
    assert a == b


def test_generate_transition_seed_varies_across_calls():
    controller = DualDeckController(random.Random(3))
    values = {controller.generate_transition_seed() for _ in range(10)}
    assert len(values) > 1


@pytest.mark.parametrize("effect", GPU_TRANSITION_EFFECTS)
@pytest.mark.parametrize("trigger", ["manual", "automatic"])
def test_try_commit_sends_the_selected_effect_and_advances_once(trigger, effect):
    engine, backend, _identity_holder, advances, events, _pre = _engine(
        preferences=DualTransitionPreferences(
            enabled=True, preload_lead_seconds=6.0, gpu_effect=effect,
        ),
    )
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_ready()
    assert engine.try_commit(trigger, 0.5) is True
    assert backend.committed_effects == [effect]
    assert advances == [trigger]
    assert any(
        event == "gpu_transition_started" and details.get("effect") == effect
        for event, details in events
    )


def test_try_commit_random_gpu_resolves_to_one_concrete_effect_per_commit():
    engine, backend, _identity_holder, _advances, events, _pre = _engine(
        preferences=DualTransitionPreferences(
            enabled=True, preload_lead_seconds=6.0, gpu_effect=GPU_RANDOM_STYLE,
        ),
        # Fresh rng per _engine() call, but DualDeckController is created
        # inside _engine with no rng override -- pass one explicitly here
        # via configure() is not needed since selection happens on the
        # already-constructed controller; a default random.Random() is
        # fine, this test only cares that exactly one concrete effect (not
        # the literal "Random GPU" placeholder) reaches the backend.
    )
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_ready()
    assert engine.try_commit("automatic", 1.0) is True
    assert len(backend.committed_effects) == 1
    resolved = backend.committed_effects[0]
    assert resolved in GPU_TRANSITION_EFFECTS
    assert resolved != GPU_RANDOM_STYLE
    assert any(
        event == "gpu_transition_started" and details.get("effect") == resolved
        for event, details in events
    )


def test_stop_during_a_push_transition_does_not_disturb_it():
    engine, backend, _identity_holder, _advances, _events, _pre = _engine(
        preferences=DualTransitionPreferences(
            enabled=True, preload_lead_seconds=6.0, gpu_effect="Push Left",
        ),
    )
    engine.observe_position(4500, 10000, MediaType.VIDEO)
    engine.on_secondary_ready()
    assert engine.try_commit("automatic", 1.0) is True
    engine.playback_stopped("stop")
    # Committed transitions are never cancelled once started, regardless
    # of which effect is running -- see COMMITTED_STATES.
    assert engine.state == DualDeckState.TRANSITIONING
    assert backend.cancelled_count == 0
