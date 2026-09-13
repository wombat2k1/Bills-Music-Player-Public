"""State and orchestration for Phase 2A genuine dual-video cross-dissolve.

This is a foundation/proof-of-concept layer that sits *alongside* Phase 1
(``video_transition.py``), not a replacement for it.  ``VideoTransitionManager``
only ever delegates here for a Video->Video pairing when the experimental
preference is on and a secondary deck is genuinely ready; any failure at any
stage falls back to the existing, unmodified Phase 1 overlay path, which
remains the permanent safety net.

Two-layer split, mirroring ``video_transition.py``'s own
``VideoTransitionController``/``VideoTransitionManager`` split:

- :class:`DualDeckController` is pure Python state/policy, deliberately free
  of real Qt Multimedia so it is unit-testable without a video subprocess.
- :class:`DualVideoTransitionEngine` is the thin ``QObject`` orchestration
  layer that talks to the injected video backend and to the *same*
  ``advance_callback``/``diagnostic_callback`` Phase 1's manager already
  uses -- the queue only ever advances through that one seam.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Mapping, Optional

from PyQt6 import QtCore

from .media_type import MediaType

# Phase 2B/2C -- genuine GPU transition effects, layered onto Phase 2A's
# cross-dissolve-only compositor (see video_dual_deck_blend.frag). Names
# match Phase 1's own EFFECTS convention (human-readable, used directly as
# both the UI label and the internal identifier) rather than introducing a
# second snake_case vocabulary -- video_subprocess.py's
# _GPU_EFFECT_TRANSITION_TYPE/_GPU_EFFECT_DIRECTION maps translate these to
# shader uniform values at the very last step, in the child process.
GPU_TRANSITION_EFFECTS: tuple[str, ...] = (
    "Cross Dissolve", "Push Left", "Push Right", "Wipe Left", "Wipe Right", "Zoom",
    # Phase 2C
    "RGB Glitch", "Pixel Dissolve", "Luma Dissolve", "Film Burn", "Zoom Blur",
    "Diagonal Wipe",
)
# Phase 2C -- curated pools for "Random GPU Smooth"/"Random GPU Energetic",
# alongside the original Phase 2B "Random GPU" (which continues to mean
# "any of the above", now including the Phase 2C effects too -- kept as-is
# rather than renamed, so an existing config.json from a Phase 2B install
# keeps meaning exactly what it always meant). Zoom Blur deliberately
# appears in both pools -- it reads as smooth motion blur but also has
# some visual energy, and is genuinely a reasonable fit for either mood.
GPU_SMOOTH_EFFECTS: tuple[str, ...] = (
    "Cross Dissolve", "Push Left", "Push Right", "Wipe Left", "Wipe Right",
    "Zoom", "Diagonal Wipe", "Zoom Blur",
)
GPU_ENERGETIC_EFFECTS: tuple[str, ...] = (
    "RGB Glitch", "Pixel Dissolve", "Luma Dissolve", "Film Burn", "Zoom Blur",
)
GPU_RANDOM_STYLE = "Random GPU"
GPU_RANDOM_SMOOTH_STYLE = "Random GPU Smooth"
GPU_RANDOM_ENERGETIC_STYLE = "Random GPU Energetic"
GPU_RANDOM_STYLES: tuple[str, ...] = (
    GPU_RANDOM_STYLE, GPU_RANDOM_SMOOTH_STYLE, GPU_RANDOM_ENERGETIC_STYLE,
)
GPU_RANDOM_POOLS: Mapping[str, tuple[str, ...]] = {
    GPU_RANDOM_STYLE: GPU_TRANSITION_EFFECTS,
    GPU_RANDOM_SMOOTH_STYLE: GPU_SMOOTH_EFFECTS,
    GPU_RANDOM_ENERGETIC_STYLE: GPU_ENERGETIC_EFFECTS,
}
GPU_TRANSITION_STYLES: tuple[str, ...] = GPU_TRANSITION_EFFECTS + GPU_RANDOM_STYLES


class DualDeckState(str, Enum):
    IDLE = "idle"
    PRIMARY_PLAYING = "primary_playing"
    PRELOADING_SECONDARY = "preloading_secondary"
    SECONDARY_READY = "secondary_ready"
    TRANSITIONING = "transitioning"
    PROMOTING_SECONDARY = "promoting_secondary"
    CLEANING_PRIMARY = "cleaning_primary"
    CANCELLING = "cancelling"
    ERROR_RECOVERY = "error_recovery"
    SHUTTING_DOWN = "shutting_down"


# States in which a secondary deck genuinely exists and must be released
# before anything else may reuse it -- used by callers deciding whether a
# teardown command is needed.
ACTIVE_SECONDARY_STATES = (
    DualDeckState.PRELOADING_SECONDARY,
    DualDeckState.SECONDARY_READY,
    DualDeckState.TRANSITIONING,
    DualDeckState.PROMOTING_SECONDARY,
    DualDeckState.CLEANING_PRIMARY,
)

# Once commitment has occurred, the existing queue/history mechanism has
# already advanced -- cancelling here would mean trying to resurrect a
# primary that the app now considers finished. See the module docstring.
COMMITTED_STATES = (
    DualDeckState.TRANSITIONING,
    DualDeckState.PROMOTING_SECONDARY,
    DualDeckState.CLEANING_PRIMARY,
)


@dataclass(frozen=True)
class SecondaryIdentity:
    """A stable-enough identity for the queue row a preload targets.

    The Up Next queue has no per-row IDs (three parallel index-aligned
    lists) -- rather than add one, ``epoch`` is a counter bumped by every
    queue-structure mutation (see window.py's ``_bump_queue_mutation_epoch``).
    A preload is only trusted to still point at the same logical item if the
    epoch is unchanged *and* the path at the recorded row still matches.
    """

    epoch: int
    row: Optional[int]
    path: str
    media_type: MediaType


@dataclass(frozen=True)
class DualTransitionPreferences:
    enabled: bool = False
    # Up from 6.0 -- a real-device stall (progress seen, then genuinely no
    # further movement for ~3.3s) showed the *effective* preload window is
    # preload_lead_seconds - automatic_lead_seconds (Phase 1's own
    # observe_position() independently starts competing for the same track
    # the instant that threshold is crossed, whether or not the dual
    # engine has a secondary ready yet -- see observe_position() in
    # video_transition.py). At the old default that was only ~5.0s: not
    # enough room for a stalled first attempt (up to ready_timeout_ms) plus
    # the bounded retry _on_ready_timeout() now performs (see below) to
    # both fit before Phase 1 preempts. A real Y:\ benchmark (see CODEX_
    # HANDOFF.md) measured a P90 successful preload of ~1.6s with prefetch
    # disabled, so 10.0 gives an ~9.0s effective window -- comfortably
    # covers a full initial timeout plus a full retry window with margin,
    # while adding no real cost for the common case (the secondary just
    # sits held for longer, which Stage A's proven zero-drift architecture
    # made safe).
    preload_lead_seconds: float = 10.0
    ready_timeout_ms: int = 4000
    # Phase 2B: which GPU effect to use, or GPU_RANDOM_STYLE to pick one
    # per transition (see DualDeckController.select_gpu_effect). Defaults
    # to the known-good Cross Dissolve reference implementation.
    gpu_effect: str = "Cross Dissolve"
    # Deliberately the *same* two values (and the same config keys, via
    # from_config below) Phase 1's own VideoTransitionPreferences uses for
    # its own automatic-trigger timing -- not a second, independently-
    # configurable notion of "how early"/"how long", which would let the
    # two engines silently drift apart. Used by DualVideoTransitionEngine's
    # own deadline timer (see its module docstring) to schedule a commit
    # attempt *before* the outgoing deck's natural end, rather than relying
    # solely on a position tick happening to land inside the lead window.
    automatic_lead_seconds: float = 1.0
    duration_seconds: float = 1.0
    # Smart Video Transition Points (see video_transition_point_analyzer.py).
    # These are the *final, effective* flags -- window.py combines its own
    # "Smart video transition points" parent checkbox with each subordinate
    # checkbox before writing config, the same way it already combines
    # DUAL_VIDEO_TRANSITIONS_AVAILABLE/capability/checkbox for
    # video_dual_transitions_enabled -- so this dataclass only ever needs
    # to know the two outcomes, not the checkbox hierarchy that produced
    # them. Both default off: a brand new, real-device-unverified feature.
    avoid_black_outros: bool = False
    skip_black_intros: bool = False
    # Stage C (synchronized GPU video audio crossfade) -- GPU-transition-
    # only, never Phase 1/classic/karaoke/music. Default off: a brand new,
    # real-device-unverified feature, matching the same forced-off pattern
    # as avoid_black_outros/skip_black_intros above. audio_crossfade_curve
    # is only meaningful when crossfade_video_audio_enabled is True.
    crossfade_video_audio_enabled: bool = False
    audio_crossfade_curve: str = "Equal Power"
    # Stage B (adaptive bounded preload timeout, see
    # DualVideoTransitionEngine.on_secondary_preload_progress): overall hard
    # cap on the whole preload phase (never exceeded regardless of how much
    # genuine progress is observed) and the bounded increment granted per
    # genuine-progress signal. ready_timeout_ms above remains the *initial*
    # timeout before any extension.
    preload_max_wait_ms: int = 12000
    preload_progress_extension_ms: int = 2000

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> "DualTransitionPreferences":
        # Same defensive-defaulting shape as VideoTransitionPreferences.from_config
        # in video_transition.py -- corrupted/unknown values fall back to the
        # safe (disabled) default rather than raising.
        try:
            preload_lead_seconds = max(
                3.0, min(15.0, float(config.get(
                    "video_dual_preload_lead_seconds", 10.0,
                )))
            )
        except (TypeError, ValueError):
            preload_lead_seconds = 10.0

        def bounded_float(key: str, default: float, low: float, high: float) -> float:
            try:
                return max(low, min(high, float(config.get(key, default))))
            except (TypeError, ValueError):
                return default

        def bounded_int(key: str, default: int, low: int, high: int) -> int:
            try:
                return max(low, min(high, int(config.get(key, default))))
            except (TypeError, ValueError):
                return default

        raw_gpu_effect = str(config.get("video_gpu_transition_effect", "Cross Dissolve") or "")
        gpu_effect = raw_gpu_effect if raw_gpu_effect in GPU_TRANSITION_STYLES else "Cross Dissolve"
        raw_crossfade_curve = str(config.get("video_crossfade_audio_curve", "Equal Power") or "")
        crossfade_curve = raw_crossfade_curve if raw_crossfade_curve in ("Equal Power", "Linear") else "Equal Power"
        return cls(
            enabled=bool(config.get("video_dual_transitions_enabled", False)),
            preload_lead_seconds=preload_lead_seconds,
            ready_timeout_ms=bounded_int(
                "video_dual_ready_timeout_ms", 4000, 1500, 8000,
            ),
            gpu_effect=gpu_effect,
            automatic_lead_seconds=bounded_float(
                "video_transition_automatic_lead_seconds", 1.0, 0.3, 3.0,
            ),
            duration_seconds=bounded_float(
                "video_transition_duration_seconds", 1.0, 0.3, 3.0,
            ),
            avoid_black_outros=bool(config.get("video_avoid_black_outros", False)),
            skip_black_intros=bool(config.get("video_skip_black_intros", False)),
            crossfade_video_audio_enabled=bool(config.get("video_crossfade_audio_enabled", False)),
            audio_crossfade_curve=crossfade_curve,
            preload_max_wait_ms=bounded_int(
                "video_dual_preload_max_wait_ms", 12000, 4000, 30000,
            ),
            preload_progress_extension_ms=bounded_int(
                "video_dual_preload_progress_extension_ms", 2000, 500, 5000,
            ),
        )


class DualDeckController:
    """Pure state/policy for the dual-deck lifecycle -- no Qt, no IPC."""

    def __init__(self, rng: Optional[random.Random] = None) -> None:
        self.state = DualDeckState.IDLE
        self.identity: Optional[SecondaryIdentity] = None
        self.trigger: Optional[str] = None
        # Correctness hardening (2026-08-24, Codex design review): the
        # backend-minted identity of the currently active preload attempt
        # and, once committed, of the currently active transition -- set
        # by DualVideoTransitionEngine right after a successful backend
        # call (see observe_position()/_attempt_bounded_preload_retry's
        # preload_id capture and try_commit()'s transition_id capture),
        # reset alongside identity/trigger everywhere those are. Distinct
        # from ``identity`` (SecondaryIdentity, the *queue-row* identity a
        # preload targets) -- this is the *IPC-protocol* identity of the
        # attempt itself, independent of what queue row it happens to be
        # for. A bounded retry of the same queue row still gets a brand
        # new preload_id (see VideoPreloadHandle in video_backend.py) --
        # PRELOADING_SECONDARY state alone is not identity.
        self.preload_id: Optional[int] = None
        self.preload_source_hash: Optional[str] = None
        self.transition_id: Optional[int] = None
        self._last_gpu_effect: Optional[str] = None
        self._rng = rng or random.Random()

    def select_gpu_effect(self, preferences: "DualTransitionPreferences") -> str:
        """Resolves the effect a commit should use -- mirrors
        VideoTransitionController.select_effect's shape (video_transition.py)
        exactly, including the "never immediately repeat when >1 candidate
        is eligible" rule, now generalised to Phase 2C's three random pools
        (GPU_RANDOM_POOLS) the same way Phase 1 picks between its Smooth/
        Energetic/All pools. An unrecognised gpu_effect value (e.g. an old
        config referencing a since-removed effect) falls back to the known-
        good Cross Dissolve reference implementation rather than raising or
        silently doing nothing."""
        style = preferences.gpu_effect
        if style in GPU_TRANSITION_EFFECTS:
            return style
        candidates = list(GPU_RANDOM_POOLS.get(style, ()))
        if not candidates:
            return "Cross Dissolve"
        if len(candidates) > 1 and self._last_gpu_effect in candidates:
            candidates = [e for e in candidates if e != self._last_gpu_effect]
        selected = self._rng.choice(candidates)
        self._last_gpu_effect = selected
        return selected

    def generate_transition_seed(self) -> float:
        """A fresh value in [0.0, 1.0), generated once per commit and
        reused for the whole transition -- see try_commit(), which calls
        this immediately alongside select_gpu_effect() so both draws come
        from the same seeded rng (deterministic and reproducible in
        tests). Effects that don't need randomness (Cross Dissolve, Push,
        Wipe, Zoom, Diagonal Wipe) simply never read the shader's `seed`
        uniform; RGB Glitch/Pixel Dissolve/Luma Dissolve/Film Burn use it
        for their deterministic-per-transition pseudo-randomness (see
        video_dual_deck_blend.frag) -- never re-drawn mid-transition, so
        those effects stay temporally stable rather than shimmering."""
        return self._rng.random()

    # -- lifecycle ------------------------------------------------------
    def reset_for_media(self) -> None:
        """A new primary track started -- any leftover secondary belonged
        to the track that just finished and is stale."""
        if self.state == DualDeckState.SHUTTING_DOWN:
            return
        self.state = DualDeckState.IDLE
        self.identity = None
        self.trigger = None
        self.preload_id = None
        self.preload_source_hash = None
        self.transition_id = None

    def shutdown(self) -> None:
        self.state = DualDeckState.SHUTTING_DOWN
        self.identity = None
        self.trigger = None
        self.preload_id = None
        self.preload_source_hash = None
        self.transition_id = None

    def set_active_preload(self, preload_id: Optional[int], source_hash: Optional[str]) -> None:
        """Called by DualVideoTransitionEngine right after a *successful*
        backend.preload_secondary() call (reading back backend.
        active_preload_id/active_preload_source_hash) -- not part of
        begin_preload() itself, since the backend call (and therefore the
        real preload_id) only happens after that already runs."""
        self.preload_id = preload_id
        self.preload_source_hash = source_hash

    def set_active_transition(self, transition_id: Optional[int]) -> None:
        """Called by DualVideoTransitionEngine right after a *successful*
        backend.commit_dual_transition() call (reading back backend.
        active_transition_id) -- same timing reasoning as set_active_
        preload above."""
        self.transition_id = transition_id

    # -- preload ----------------------------------------------------------
    @staticmethod
    def should_start_preload(
        position_ms: int, duration_ms: int, preload_lead_seconds: float,
    ) -> bool:
        if duration_ms <= 0 or position_ms < 0 or position_ms > duration_ms:
            return False
        remaining_ms = duration_ms - position_ms
        return remaining_ms <= max(0.0, preload_lead_seconds) * 1000.0

    @staticmethod
    def should_cancel_preload_for_seek(
        position_ms: int, duration_ms: int, preload_lead_seconds: float,
    ) -> bool:
        """A preloaded/ready secondary deck keeps decoding (muted) for as
        long as it's held -- harmless for the few seconds it's normally
        held, but wasteful if the user seeks well back into a long video.
        Generous slack over the preload lead avoids thrashing preload/cancel
        right at the boundary."""
        if duration_ms <= 0 or position_ms < 0:
            return False
        remaining_ms = duration_ms - position_ms
        return remaining_ms > (max(0.0, preload_lead_seconds) + 3.0) * 1000.0

    def begin_preload(self, identity: SecondaryIdentity) -> bool:
        if self.state != DualDeckState.IDLE:
            return False
        self.state = DualDeckState.PRELOADING_SECONDARY
        self.identity = identity
        return True

    def mark_secondary_ready(self) -> bool:
        if self.state != DualDeckState.PRELOADING_SECONDARY:
            return False
        self.state = DualDeckState.SECONDARY_READY
        return True

    def mark_secondary_failed(self) -> bool:
        if self.state not in (
            DualDeckState.PRELOADING_SECONDARY, DualDeckState.SECONDARY_READY,
        ):
            return False
        self.state = DualDeckState.ERROR_RECOVERY
        return True

    def acknowledge_error_recovery(self) -> None:
        self.state = DualDeckState.IDLE
        self.identity = None
        self.trigger = None
        self.preload_id = None
        self.preload_source_hash = None
        self.transition_id = None

    def is_ready_for(self, current_identity: Optional[SecondaryIdentity]) -> bool:
        if self.state != DualDeckState.SECONDARY_READY or current_identity is None:
            return False
        return self.identity == current_identity

    def is_stale(self, current_epoch: int, current_path_at_row: Optional[str]) -> bool:
        """True when the queue has structurally changed since preload began
        in a way that invalidates the recorded target (see
        ``SecondaryIdentity``'s docstring)."""
        if self.identity is None:
            return False
        if self.identity.epoch != current_epoch:
            return True
        return current_path_at_row != self.identity.path

    # -- commit / promote / cleanup --------------------------------------
    def begin_commit(self, trigger: str) -> bool:
        if self.state != DualDeckState.SECONDARY_READY:
            return False
        self.state = DualDeckState.TRANSITIONING
        self.trigger = trigger
        return True

    def begin_promotion(self) -> bool:
        if self.state != DualDeckState.TRANSITIONING:
            return False
        self.state = DualDeckState.PROMOTING_SECONDARY
        return True

    def begin_cleanup(self) -> bool:
        if self.state != DualDeckState.PROMOTING_SECONDARY:
            return False
        self.state = DualDeckState.CLEANING_PRIMARY
        return True

    def complete(self) -> None:
        self.state = DualDeckState.IDLE
        self.identity = None
        self.trigger = None
        self.preload_id = None
        self.preload_source_hash = None
        self.transition_id = None

    # -- cancellation -----------------------------------------------------
    def cancel(self) -> bool:
        """Cooperative cancel -- refuses once commitment has occurred (see
        ``COMMITTED_STATES``); callers must let a committed transition
        finish rather than trying to resurrect the old primary."""
        if self.state in (DualDeckState.IDLE, DualDeckState.SHUTTING_DOWN):
            return False
        if self.state in COMMITTED_STATES:
            return False
        self.state = DualDeckState.CANCELLING
        self.complete()
        return True

    def force_abort(self) -> bool:
        """Unconditional teardown for real failures (primary error, process
        crash, shutdown) regardless of commitment phase -- unlike
        :meth:`cancel`, this does not try to be cooperative."""
        if self.state in (DualDeckState.IDLE, DualDeckState.SHUTTING_DOWN):
            return False
        self.state = DualDeckState.CANCELLING
        self.complete()
        return True


class DualVideoTransitionEngine(QtCore.QObject):
    """Qt orchestration around :class:`DualDeckController`.

    Deliberately mirrors ``video_transition.VideoTransitionManager``'s shape:
    injected callbacks rather than reaching into ``window.py`` directly, and
    the identical ``advance_callback`` is reused for the actual queue
    advance so "advances exactly once" is inherited, not re-implemented.
    """

    READY_TIMEOUT_MS = 4000

    def __init__(
        self,
        parent: QtCore.QObject,
        backend,
        advance_callback: Callable[[str], Optional[MediaType]],
        identity_provider: Callable[[], Optional[SecondaryIdentity]],
        diagnostic_callback: Optional[Callable[[str, Mapping[str, object]], None]] = None,
        pre_advance_callback: Optional[Callable[[str], None]] = None,
        rng: Optional[random.Random] = None,
        outro_transition_point_lookup: Optional[Callable[[str], Optional[int]]] = None,
        intro_transition_point_lookup: Optional[Callable[[str], Optional[int]]] = None,
        current_primary_path_provider: Optional[Callable[[], Optional[str]]] = None,
        staleness_identity_provider: Optional[Callable[[], Optional[SecondaryIdentity]]] = None,
    ):
        super().__init__(parent)
        self.controller = DualDeckController(rng)
        self.preferences = DualTransitionPreferences()
        self._backend = backend
        self._advance_callback = advance_callback
        self._identity_provider = identity_provider
        # Used wherever an already-in-flight preload's identity is merely
        # being re-verified (on_secondary_ready()'s staleness check,
        # try_commit()'s pre-commit check) rather than a new preload being
        # requested -- identity_provider itself is free to have side
        # effects (window.py's variant speculatively triggers Smart
        # Transition Points analysis for the Up Next candidate, which is
        # correct exactly once, at the genuine lead-window preload trigger
        # in observe_position() below), but re-running those side effects
        # on every staleness re-check served no purpose. Defaults to
        # identity_provider for callers that don't distinguish the two.
        self._staleness_identity_provider = staleness_identity_provider or identity_provider
        self._diagnostic_callback = diagnostic_callback
        # Smart Video Transition Points (optional, additive -- see
        # video_transition_point_analyzer.py's module docstring).
        # outro_transition_point_lookup(primary_path) -> Optional[last_visible_ms],
        # used by _schedule_deadline() to substitute a detected black-outro
        # endpoint for duration_ms in its existing formula -- same formula,
        # just a different input, no new scheduling mechanism.
        # intro_transition_point_lookup(secondary_path) -> Optional[safe_start_ms],
        # used by observe_position()'s preload branch to seek the secondary
        # deck's start position. Any being None (the default) is
        # byte-identical to today's behaviour.
        self._outro_transition_point_lookup = outro_transition_point_lookup
        self._intro_transition_point_lookup = intro_transition_point_lookup
        self._current_primary_path_provider = current_primary_path_provider
        # Called with the already-live secondary's path immediately before
        # the shared advance_callback runs -- lets window.py recognise that
        # the upcoming _play_video_path_direct(path) call is for content the
        # subprocess is already playing (as Deck B) rather than something to
        # load from scratch. See window.py's _promote_dual_transition_track_ui.
        self._pre_advance_callback = pre_advance_callback
        self._closing = False
        self._commit_duration_ms = 1000
        self._last_position_ms = 0
        self._last_duration_ms = 0
        self._ready_timer = QtCore.QTimer(self)
        self._ready_timer.setSingleShot(True)
        self._ready_timer.timeout.connect(self._on_ready_timeout)
        # Stage B -- adaptive bounded preload timeout. Both None whenever
        # no preload is in flight; set together in observe_position()'s
        # preload branch and cleared together by _stop_ready_timer(). The
        # *hard* deadline is the one genuine ceiling -- on_secondary_preload_
        # progress() may push _ready_deadline_monotonic further out (never
        # past the hard deadline), but _on_ready_timeout() itself is
        # unchanged: still the sole failure path, firing whenever the
        # currently-armed _ready_timer interval elapses with no further
        # extension.
        self._ready_deadline_monotonic: Optional[float] = None
        self._preload_hard_deadline_monotonic: Optional[float] = None
        self._preload_started_monotonic: Optional[float] = None
        # Bounded early retry (see _on_ready_timeout/_attempt_bounded_
        # preload_retry below): _preload_progress_seen distinguishes "the
        # secondary genuinely reached Loading/Loaded/Buffering(ed) at least
        # once, then stalled" from "never got anywhere" -- only the former
        # is worth retrying. _preload_retry_used bounds this to exactly one
        # retry per preload. Both reset by _stop_ready_timer() alongside
        # the deadline fields, and set fresh by observe_position()'s own
        # preload branch each time a genuinely new preload begins.
        self._preload_progress_seen: bool = False
        self._preload_retry_used: bool = False
        # Bounded one-shot deadline timer -- the actual fix for a real bug
        # found via these classes' own diagnostics on real near-end
        # playback: committing only ever happened either from
        # observe_position()'s position-tick-driven automatic check (which
        # can simply miss its window if no tick lands inside it before the
        # outgoing deck's own end-of-media fires) or, as a last resort,
        # from VideoTransitionManager.handle_natural_end() -- but by the
        # time a natural end-of-media signal arrives, the outgoing deck has
        # no live frame left to contribute to the blend, so a transition
        # committed there shows as a solid black gap instead of genuine
        # A+B overlap, even though secondary_ready had fired several
        # seconds earlier. This timer is (re)armed, once secondary becomes
        # ready and a real duration/position is known, to fire at
        # duration_ms - position_ms - automatic_lead_seconds before the
        # end -- an absolute wall-clock deadline rather than a per-tick
        # condition, so it cannot be skipped by a tick simply not landing
        # in the right place. See _schedule_deadline/_on_deadline_timer_fired
        # below, and every other self._deadline_timer.stop() call in this
        # class, for exactly when it is (re)scheduled and cancelled.
        # handle_natural_end() remains the final safety net for the rare
        # case this deadline is itself missed (e.g. GUI-thread stall) --
        # not the primary mechanism anymore.
        self._deadline_timer = QtCore.QTimer(self)
        self._deadline_timer.setSingleShot(True)
        self._deadline_timer.timeout.connect(self._on_deadline_timer_fired)
        if parent is not None:
            parent.destroyed.connect(self.shutdown)

    @property
    def state(self) -> DualDeckState:
        return self.controller.state

    def configure(self, preferences: DualTransitionPreferences) -> None:
        self.preferences = preferences

    def media_changed(self) -> None:
        if self.controller.state in ACTIVE_SECONDARY_STATES:
            self._teardown_secondary()
        self.controller.reset_for_media()

    # -- preload ------------------------------------------------------------
    def observe_position(
        self, position_ms: int, duration_ms: int, current_media_type: MediaType,
    ) -> None:
        if self._closing or not self.preferences.enabled or current_media_type != MediaType.VIDEO:
            return
        self._last_position_ms = position_ms
        self._last_duration_ms = duration_ms
        if self.controller.state in (
            DualDeckState.PRELOADING_SECONDARY, DualDeckState.SECONDARY_READY,
        ):
            if self.controller.should_cancel_preload_for_seek(
                position_ms, duration_ms, self.preferences.preload_lead_seconds,
            ):
                self._record("dual_transition_cancelled", {"reason": "seeked_away_from_end"})
                self._teardown_secondary()
                self.controller.cancel()
                self._deadline_timer.stop()
                return
            if self.controller.state == DualDeckState.SECONDARY_READY:
                # Reschedule from the freshest position/duration on every
                # tick while we wait -- self-correcting, and exactly what
                # lets the deadline still fire at the right wall-clock time
                # even if this was the last tick before ticks stop arriving
                # (the bug this timer exists to close: see __init__).
                self._schedule_deadline(position_ms, duration_ms)
            return
        if self.controller.state != DualDeckState.IDLE:
            return
        if not self.controller.should_start_preload(
            position_ms, duration_ms, self.preferences.preload_lead_seconds,
        ):
            return
        identity = self._safe_identity()
        if identity is None or identity.media_type != MediaType.VIDEO:
            return
        if not self.controller.begin_preload(identity):
            return
        start_position_ms = self._smart_intro_start_ms(identity.path)
        self._record(
            "preload_requested",
            {"row": identity.row, "start_position_ms": start_position_ms or 0},
        )
        if start_position_ms:
            self._record("smart_intro_seek_used", {"start_position_ms": start_position_ms})
        failure_reason = None
        try:
            started = bool(self._backend.preload_secondary(identity.path, start_position_ms=start_position_ms or 0))
        except Exception as ex:
            started = False
            failure_reason = str(ex)
        if not started:
            self._record("compositor_failure", {"stage": "preload", "error": failure_reason or "declined"})
            self.controller.mark_secondary_failed()
            self.controller.acknowledge_error_recovery()
            return
        # Captured immediately after the successful backend call -- the
        # backend has already minted a fresh preload_id/source_hash by the
        # time preload_secondary() returns (see video_backend.py's
        # VideoPreloadHandle). Every later secondary_ready/secondary_failed/
        # secondary_preload_progress callback below re-verifies its own
        # envelope against exactly this before touching any state.
        self.controller.set_active_preload(
            getattr(self._backend, "active_preload_id", None),
            getattr(self._backend, "active_preload_source_hash", None),
        )
        now = time.monotonic()
        initial_timeout_ms = max(500, int(self.preferences.ready_timeout_ms))
        self._preload_started_monotonic = now
        self._ready_deadline_monotonic = now + initial_timeout_ms / 1000.0
        self._preload_hard_deadline_monotonic = now + max(
            initial_timeout_ms, int(self.preferences.preload_max_wait_ms)
        ) / 1000.0
        self._ready_timer.start(initial_timeout_ms)

    def _stop_ready_timer(self) -> None:
        self._ready_timer.stop()
        self._ready_deadline_monotonic = None
        self._preload_hard_deadline_monotonic = None
        self._preload_started_monotonic = None
        self._preload_progress_seen = False
        self._preload_retry_used = False

    def _preload_envelope_matches_active(self, envelope: Mapping[str, object]) -> bool:
        """Correctness hardening (2026-08-24, Codex design review):
        PRELOADING_SECONDARY/SECONDARY_READY state alone is not identity --
        a superseded preload (the attempt before a bounded retry, or an
        abandoned one after a manual Next/queue change) can leave the
        controller in a state that still *looks* like it's waiting for
        exactly this event. Only a real preload_id + source_hash match
        proves the event actually belongs to the attempt this engine is
        currently tracking (self.controller.preload_id/preload_source_
        hash, set by observe_position()/_attempt_bounded_preload_retry
        right after each successful backend call)."""
        if envelope.get("preload_id") != self.controller.preload_id:
            return False
        if envelope.get("source_hash") != self.controller.preload_source_hash:
            return False
        return True

    def on_secondary_ready(
        self, envelope: Optional[Mapping[str, object]] = None,
    ) -> None:
        if self._closing or self.controller.state != DualDeckState.PRELOADING_SECONDARY:
            return
        envelope = envelope or {}
        if envelope and not self._preload_envelope_matches_active(envelope):
            self._record("secondary_ready_stale_preload_ignored", {
                "event_preload_id": envelope.get("preload_id"),
                "active_preload_id": self.controller.preload_id,
            })
            return
        self._stop_ready_timer()
        if self._current_preload_is_stale():
            self._record("secondary_invalidated_by_queue_change", {})
            self._teardown_secondary()
            self.controller.cancel()
            return
        self.controller.mark_secondary_ready()
        self._record("secondary_ready", {})
        # Arm the deadline immediately using the most recently observed
        # position/duration rather than waiting for the next position tick
        # -- both are already known by the time secondary becomes ready
        # (preload itself only starts once should_start_preload() sees us
        # inside the preload lead window), so there is no reason to leave
        # the deadline unscheduled in the meantime.
        self._schedule_deadline(self._last_position_ms, self._last_duration_ms)

    def on_secondary_failed(
        self, reason: str = "secondary_failed",
        envelope: Optional[Mapping[str, object]] = None,
    ) -> None:
        if self._closing:
            return
        envelope = envelope or {}
        if envelope and not self._preload_envelope_matches_active(envelope):
            self._record("secondary_failed_stale_preload_ignored", {
                "event_preload_id": envelope.get("preload_id"),
                "active_preload_id": self.controller.preload_id,
                "reason": reason,
            })
            return
        if not self.controller.mark_secondary_failed():
            return
        self._stop_ready_timer()
        self._deadline_timer.stop()
        self._record("secondary_invalidated_by_queue_change" if reason == "queue_changed" else "compositor_failure", {"reason": reason})
        self.controller.acknowledge_error_recovery()

    # Minimum remaining hard-deadline budget a retry needs to be worth
    # attempting at all -- below this, re-issuing preload_secondary() and
    # waiting for a fresh LoadingMedia/LoadedMedia round-trip has no
    # realistic chance of finishing before the hard cap, so it's better to
    # fail now (letting Phase 1 take over promptly) than to eat into an
    # already-thin budget for no benefit.
    _MIN_RETRY_BUDGET_MS = 1000

    def _on_ready_timeout(self) -> None:
        if self.controller.state != DualDeckState.PRELOADING_SECONDARY:
            return
        if (
            self._preload_progress_seen
            and not self._preload_retry_used
            and self._preload_hard_deadline_monotonic is not None
        ):
            remaining_ms = int(
                (self._preload_hard_deadline_monotonic - time.monotonic()) * 1000.0
            )
            if remaining_ms >= self._MIN_RETRY_BUDGET_MS:
                self._attempt_bounded_preload_retry(remaining_ms)
                return
        self._record("secondary_preload_timeout", {"retried": self._preload_retry_used})
        self._stop_ready_timer()
        self._teardown_secondary()
        self.controller.mark_secondary_failed()
        self.controller.acknowledge_error_recovery()

    def _attempt_bounded_preload_retry(self, remaining_to_hard_cap_ms: int) -> None:
        """One bounded re-attempt when a preload demonstrably reached
        Loading/Loaded/Buffering(ed)Media at least once and then stalled --
        never for a preload that never got anywhere at all (that's still an
        immediate failure, unchanged from before this method existed).
        Never resets _preload_hard_deadline_monotonic/_preload_started_
        monotonic -- the retry spends down the *same* overall budget rather
        than getting a fresh one, and never touches _deadline_timer (the
        commit deadline), which isn't armed until SECONDARY_READY regardless."""
        self._preload_retry_used = True
        self._preload_progress_seen = False
        identity = self.controller.identity
        started_at = self._preload_started_monotonic
        self._record("secondary_preload_retry", {
            "elapsed_ms": (
                int((time.monotonic() - started_at) * 1000.0)
                if started_at is not None else None
            ),
        })
        self._teardown_secondary()
        if identity is None:
            self._record("secondary_preload_timeout", {"retried": True})
            self._stop_ready_timer()
            self.controller.mark_secondary_failed()
            self.controller.acknowledge_error_recovery()
            return
        start_position_ms = self._smart_intro_start_ms(identity.path)
        failure_reason = None
        try:
            started = bool(self._backend.preload_secondary(
                identity.path, start_position_ms=start_position_ms or 0,
            ))
        except Exception as ex:
            started = False
            failure_reason = str(ex)
        if not started:
            self._record("compositor_failure", {
                "stage": "preload_retry", "error": failure_reason or "declined",
            })
            self._stop_ready_timer()
            self.controller.mark_secondary_failed()
            self.controller.acknowledge_error_recovery()
            return
        # A retry is a *new* preload attempt as far as identity is
        # concerned -- the backend minted a fresh preload_id for it (never
        # a continuation of the one _teardown_secondary() just invalidated
        # above), captured the same way the initial attempt's is in
        # observe_position().
        self.controller.set_active_preload(
            getattr(self._backend, "active_preload_id", None),
            getattr(self._backend, "active_preload_source_hash", None),
        )
        retry_timeout_ms = min(
            remaining_to_hard_cap_ms, max(500, int(self.preferences.ready_timeout_ms)),
        )
        self._ready_deadline_monotonic = time.monotonic() + retry_timeout_ms / 1000.0
        self._ready_timer.start(retry_timeout_ms)

    # -- Stage B: adaptive bounded preload timeout ---------------------------
    _PRELOAD_PROGRESS_STATUS_NAMES = frozenset({
        "LoadingMedia", "LoadedMedia", "BufferingMedia", "BufferedMedia",
    })

    def on_secondary_preload_progress(self, details: Mapping[str, object]) -> None:
        """Extends the ready timer by a bounded increment when the
        secondary is demonstrably still making forward progress, up to the
        overall preload_max_wait_ms hard cap -- never indefinitely, and
        never for a status that indicates a stall/failure rather than
        genuine progress (see _PRELOAD_PROGRESS_STATUS_NAMES). Matches
        string status *names* from the subprocess's payload rather than
        the int enum values in video_subprocess.py's own
        _MEDIA_STATUS_NAMES -- deliberately decoupled across the IPC
        boundary, same as every other diagnostic payload this engine
        already reads by name (e.g. gpu_transition_checkpoint's
        media_status_name fields)."""
        if self._closing or self.controller.state != DualDeckState.PRELOADING_SECONDARY:
            return
        if not self._preload_envelope_matches_active(details):
            return
        if self._ready_deadline_monotonic is None or self._preload_hard_deadline_monotonic is None:
            return
        if str(details.get("media_status_name", "")) not in self._PRELOAD_PROGRESS_STATUS_NAMES:
            return
        # Recorded even when it doesn't earn an extension below (e.g. the
        # very first LoadingMedia signal, comfortably inside the initial
        # window) -- this is what lets _on_ready_timeout() later tell "made
        # it somewhere, then genuinely stalled" apart from "never got
        # anywhere," which is the one case worth a bounded retry.
        self._preload_progress_seen = True
        now = time.monotonic()
        if now >= self._preload_hard_deadline_monotonic:
            return
        extension_seconds = max(0, int(self.preferences.preload_progress_extension_ms)) / 1000.0
        candidate = min(now + extension_seconds, self._preload_hard_deadline_monotonic)
        if candidate <= self._ready_deadline_monotonic:
            return
        self._ready_deadline_monotonic = candidate
        remaining_ms = max(1, int((candidate - now) * 1000.0))
        self._ready_timer.start(remaining_ms)
        started = self._preload_started_monotonic
        self._record("preload_timeout_extended", {
            "extended_by_ms": int((candidate - now) * 1000.0),
            "total_elapsed_ms": int((now - started) * 1000.0) if started is not None else None,
        })

    def _schedule_deadline(self, position_ms: int, duration_ms: int) -> None:
        if duration_ms <= 0:
            return
        lead_ms = int(self.preferences.automatic_lead_seconds * 1000.0)
        effective_duration_ms = duration_ms
        smart_end_ms = self._smart_outro_end_ms(duration_ms)
        if smart_end_ms is not None:
            effective_duration_ms = smart_end_ms
            self._record(
                "smart_deadline_used",
                {"duration_ms": duration_ms, "smart_end_ms": smart_end_ms},
            )
        remaining_ms = max(0, (effective_duration_ms - position_ms) - lead_ms)
        self._deadline_timer.start(remaining_ms)
        # Diagnostic-only: lets a real near-end test session be reconstructed
        # exactly -- what position/duration this schedule used, and the
        # delay_ms it computed -- without guessing from other events that
        # aren't actually tied to this calculation.
        self._record(
            "deadline_armed",
            {
                "position_ms": position_ms,
                "duration_ms": duration_ms,
                "effective_duration_ms": effective_duration_ms,
                "automatic_lead_seconds": self.preferences.automatic_lead_seconds,
                "lead_ms": lead_ms,
                "delay_ms": remaining_ms,
            },
        )

    def _smart_outro_end_ms(self, duration_ms: int) -> Optional[int]:
        """Smart Video Transition Points: substitutes a detected black-
        outro endpoint for duration_ms in _schedule_deadline's existing
        formula -- same formula, different input, no new scheduling
        system. Returns None (byte-identical to today's behaviour)
        whenever the feature is off, either provider is missing, no smart
        endpoint is cached for the current primary path, or applying it
        would leave a degenerate/too-short playable span for a short clip
        (a combined intro+outro trim on a short file must not produce a
        near-zero deadline -- fail back to the unmodified duration_ms
        instead)."""
        if not self.preferences.avoid_black_outros:
            return None
        if self._outro_transition_point_lookup is None or self._current_primary_path_provider is None:
            return None
        try:
            path = self._current_primary_path_provider()
        except Exception:
            return None
        if not path:
            return None
        try:
            smart_end_ms = self._outro_transition_point_lookup(path)
        except Exception:
            return None
        if smart_end_ms is None or smart_end_ms <= 0 or smart_end_ms >= duration_ms:
            return None
        min_playable_span_ms = int(
            2.0 * (self.preferences.automatic_lead_seconds + self.preferences.duration_seconds) * 1000.0
        )
        if smart_end_ms < min_playable_span_ms:
            return None
        return int(smart_end_ms)

    def _smart_intro_start_ms(self, secondary_path: str) -> Optional[int]:
        """Returns a safe preload start offset for the secondary deck, or
        None (today's exact behaviour: start at 0) whenever the feature is
        off, the provider is missing, or nothing is confidently cached for
        this path. The provider itself (VideoTransitionPointAnalyzer.
        cached_intro_start_ms) already applies the stricter audio-silence-
        proven gate -- this method just wires it in, no additional policy
        here."""
        if not self.preferences.skip_black_intros:
            return None
        if self._intro_transition_point_lookup is None or not secondary_path:
            return None
        try:
            start_ms = self._intro_transition_point_lookup(secondary_path)
        except Exception:
            return None
        if not start_ms or start_ms <= 0:
            return None
        return int(start_ms)

    def _on_deadline_timer_fired(self) -> None:
        self._record("deadline_fired", {})
        # try_commit() is itself the "revalidate track identity, queue
        # epoch, secondary-ready state and dual availability" step -- it
        # already declines quietly (returning False) for every case where
        # committing here would be wrong, so there is nothing extra to
        # check before calling it.
        self.try_commit("automatic", self.preferences.duration_seconds)

    # -- commit ---------------------------------------------------------
    def try_commit(self, trigger: str, duration_seconds: float = 1.0) -> bool:
        """Returns True if a genuine dual-video GPU transition was started --
        the caller (VideoTransitionManager) must fall back to the Phase 1
        overlay path when this returns False. ``duration_seconds`` comes
        from the caller's own (Phase 1) preferences -- Phase 2A/2B integrate
        with the existing transition-duration setting rather than adding a
        duplicate one. The effect used (Cross Dissolve, Push/Wipe/Zoom, or
        one resolved from Random GPU) is chosen once here, right before
        committing -- see DualDeckController.select_gpu_effect -- and never
        changes mid-transition; effect choice has no bearing on queue
        semantics, which advance through the same seam regardless."""
        if self._closing or not self.preferences.enabled:
            return False
        if self.controller.state != DualDeckState.SECONDARY_READY:
            return False
        # Re-verifying the already-preloaded identity, not requesting a
        # new preload -- same reasoning as _current_preload_is_stale()'s
        # use of this provider below.
        identity = self._safe_staleness_identity()
        stored = self.controller.identity
        if identity is None or stored is None or identity.row != stored.row:
            # Nothing at that row to compare/commit against right now --
            # not necessarily "stale", just not a match; decline quietly.
            return False
        if self.controller.is_stale(identity.epoch, identity.path):
            self._record("secondary_invalidated_by_queue_change", {})
            self._teardown_secondary()
            self.controller.cancel()
            return False
        self._commit_duration_ms = max(200, int(duration_seconds * 1000.0))
        if not self.controller.begin_commit(trigger):
            return False
        effect = self.controller.select_gpu_effect(self.preferences)
        seed = self.controller.generate_transition_seed()
        self._record("dual_transition_requested", {"trigger": trigger, "effect": effect})
        failure_reason = None
        try:
            committed = bool(
                self._backend.commit_dual_transition(
                    self._commit_duration_ms, effect, seed,
                    audio_crossfade_enabled=self.preferences.crossfade_video_audio_enabled,
                    audio_crossfade_curve=self.preferences.audio_crossfade_curve,
                )
            )
        except Exception as ex:
            committed = False
            failure_reason = str(ex)
        if not committed:
            self._record("compositor_failure", {"stage": "commit", "error": failure_reason or "declined"})
            self._teardown_secondary()
            self.controller.force_abort()
            return False
        # Captured immediately after the successful backend call -- see
        # on_dual_transition_complete()/primary_failed() below, both of
        # which independently re-verify their own transition_id argument
        # against exactly this before allowing completion/promotion/
        # cleanup or an abort to proceed.
        self.controller.set_active_transition(
            getattr(self._backend, "active_transition_id", None),
        )
        self._record(
            "gpu_transition_started",
            {"duration_ms": self._commit_duration_ms, "effect": effect, "seed": seed},
        )
        self._advance_queue_once(trigger)
        return True

    def _advance_queue_once(self, trigger: str) -> None:
        # Deliberately stays at TRANSITIONING here -- the queue has advanced
        # (this is the one commitment point), but the visual cross-dissolve
        # keeps running in the subprocess for the full configured duration.
        # Promotion/cleanup only happen once on_dual_transition_complete()
        # reports the animation has actually finished.
        self._record("transition_commitment_point_reached", {})
        identity = self.controller.identity
        if self._pre_advance_callback is not None and identity is not None:
            try:
                self._pre_advance_callback(identity.path)
            except Exception:
                pass
        self._record("queue_advance_requested", {"trigger": trigger})
        try:
            self._advance_callback(trigger)
        except Exception as ex:
            self._record("error", {"stage": "advance", "error": str(ex)})

    def on_dual_transition_complete(self, transition_id: Optional[int] = None) -> None:
        # Correctness hardening (2026-08-24, Codex design review):
        # video_backend.py's _handle_event already refuses to emit this at
        # all for a mismatched transition_id, but this is independently
        # re-verified here too rather than relying solely on state ==
        # TRANSITIONING/PROMOTING_SECONDARY -- a T1 completion arriving
        # while T2 is the committed transition must have absolutely no
        # effect on T2's promotion/cleanup, and state alone cannot
        # distinguish "the real completion for T2" from "a late one for
        # T1" (both would show the same TRANSITIONING/PROMOTING_SECONDARY
        # state -- state is not identity).
        if transition_id is not None and transition_id != self.controller.transition_id:
            self._record("dual_transition_complete_stale_ignored", {
                "event_transition_id": transition_id,
                "active_transition_id": self.controller.transition_id,
            })
            return
        if self._closing or self.controller.state not in (
            DualDeckState.PROMOTING_SECONDARY, DualDeckState.TRANSITIONING,
        ):
            return
        if self.controller.state == DualDeckState.TRANSITIONING:
            self.controller.begin_promotion()
        self.controller.begin_cleanup()
        self._record("secondary_promoted", {})
        self._record("old_primary_released", {})
        self._record("dual_transition_completed", {})
        self.controller.complete()

    # -- pause / seek / cancel -------------------------------------------
    def playback_paused(self, paused: bool) -> None:
        if self._closing:
            return
        if self.controller.state == DualDeckState.TRANSITIONING:
            try:
                if paused:
                    self._backend.pause_dual_transition()
                else:
                    self._backend.resume_dual_transition()
            except Exception:
                pass
            return
        if self.controller.state != DualDeckState.SECONDARY_READY:
            return
        # The deadline is a wall-clock timer derived from position/duration.
        # While paused, position stops advancing, so an unpaused deadline
        # would fire early against a video that isn't actually approaching
        # its end -- stop it, and reschedule fresh from the last known
        # position once playback resumes.
        if paused:
            self._deadline_timer.stop()
        else:
            self._schedule_deadline(self._last_position_ms, self._last_duration_ms)

    def cancel(self, reason: str) -> bool:
        if not self.controller.cancel():
            return False
        self._stop_ready_timer()
        self._deadline_timer.stop()
        self._teardown_secondary()
        self._record("dual_transition_cancelled", {"reason": reason})
        return True

    def playback_stopped(self, reason: str = "playback_stopped") -> None:
        if self.controller.state in COMMITTED_STATES:
            return
        if not self.controller.cancel():
            return
        self._stop_ready_timer()
        self._deadline_timer.stop()
        self._teardown_secondary()
        self._record("dual_transition_cancelled", {"reason": reason})

    def primary_failed(
        self, transition_id: Optional[int] = None, reason: str = "primary_failed",
    ) -> None:
        """Deck A itself errored, or the backend's bounded commit-ack/
        completion watchdog gave up on a committed transition -- see
        video_backend.py's dual_transition_failed(transition_id, reason)
        signal, which this is wired to (window.py). Known limitation
        (documented in the final report): this always aborts to the normal
        single-deck error path rather than attempting to promote a
        healthy, ready Deck B in place -- kept simple and safe for this
        first pass.

        Correctness hardening (2026-08-24): a failure carrying a
        transition_id that does not match this engine's own active one is
        refused outright -- an old, already-superseded transition failing
        (or a stray late failure signal) must never abort whatever *newer*
        transition is actually committed now. ``transition_id=None`` skips
        this check (the general primary-deck-error path, which isn't
        inherently about any specific committed transition -- e.g. Deck A
        erroring before anything was ever committed at all)."""
        if transition_id is not None and transition_id != self.controller.transition_id:
            self._record("primary_failed_stale_transition_ignored", {
                "event_transition_id": transition_id,
                "active_transition_id": self.controller.transition_id,
            })
            return
        if not self.controller.force_abort():
            return
        self._stop_ready_timer()
        self._deadline_timer.stop()
        self._teardown_secondary()
        self._record("compositor_failure", {"stage": "primary", "reason": reason})

    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._stop_ready_timer()
        self._deadline_timer.stop()
        if self.controller.state in ACTIVE_SECONDARY_STATES:
            self._teardown_secondary()
        self.controller.shutdown()

    # -- helpers ----------------------------------------------------------
    def _teardown_secondary(self) -> None:
        self._deadline_timer.stop()
        try:
            self._backend.cancel_secondary()
        except Exception:
            pass

    def _safe_identity(self) -> Optional[SecondaryIdentity]:
        try:
            return self._identity_provider()
        except Exception:
            return None

    def _safe_staleness_identity(self) -> Optional[SecondaryIdentity]:
        # See __init__'s comment on _staleness_identity_provider: used
        # anywhere identity is being re-verified rather than a new preload
        # requested, so this must never trigger analysis side effects --
        # unlike _safe_identity() above.
        try:
            return self._staleness_identity_provider()
        except Exception:
            return None

    def _current_preload_is_stale(self) -> bool:
        identity = self._safe_staleness_identity()
        if identity is None or self.controller.identity is None:
            return True
        if identity.row != self.controller.identity.row:
            return True
        return self.controller.is_stale(identity.epoch, identity.path)

    def _record(self, event: str, details: Mapping[str, object]) -> None:
        callback = self._diagnostic_callback
        if callback is None:
            return
        try:
            callback(event, details)
        except Exception:
            pass
