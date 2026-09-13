"""State and orchestration for Phase 1 music-video visual transitions.

The existing player remains authoritative for media playback and queue
history.  This module only coordinates a painted cover over the current
content and calls the supplied ``advance_callback`` once at the visual switch
point.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, Optional, Sequence

from PyQt6 import QtCore, QtWidgets

from .media_type import MediaType
from .video_dual_transition import COMMITTED_STATES, DualVideoTransitionEngine
from .video_transition_overlay import VideoTransitionOverlay


EFFECTS: tuple[str, ...] = (
    "Fade Black",
    "Flash",
    "Push Left",
    "Push Right",
    "Zoom Blur",
    "RGB Glitch",
    "Film Burn",
    "Pixel Dissolve",
)
SMOOTH_EFFECTS: tuple[str, ...] = (
    "Fade Black", "Zoom Blur", "Push Left", "Push Right",
)
ENERGETIC_EFFECTS: tuple[str, ...] = (
    "Flash", "RGB Glitch", "Film Burn", "Pixel Dissolve",
)
RANDOM_STYLES: tuple[str, ...] = (
    "Random Smooth", "Random Energetic", "Random All",
)
TRANSITION_STYLES: tuple[str, ...] = EFFECTS + RANDOM_STYLES


class TransitionState(str, Enum):
    IDLE = "idle"
    OUTGOING = "outgoing"
    SWITCHING = "switching"
    INCOMING = "incoming"
    CANCELLING = "cancelling"


@dataclass(frozen=True)
class VideoTransitionPreferences:
    enabled: bool = True
    style: str = "Random Smooth"
    duration_seconds: float = 1.0
    automatic_lead_seconds: float = 1.0
    manual_duration_seconds: float = 0.5
    enabled_effects: Mapping[str, bool] = field(
        default_factory=lambda: {effect: True for effect in EFFECTS}
    )

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> "VideoTransitionPreferences":
        def bounded_float(key: str, default: float, low: float, high: float) -> float:
            try:
                return max(low, min(high, float(config.get(key, default))))
            except (TypeError, ValueError):
                return default

        raw_style = str(config.get("video_transition_style", "Random Smooth") or "")
        style = raw_style if raw_style in TRANSITION_STYLES else "Fade Black"
        raw_effects = config.get("video_transition_enabled_effects", {})
        if not isinstance(raw_effects, Mapping):
            raw_effects = {}
        enabled_effects = {
            effect: bool(raw_effects.get(effect, True)) for effect in EFFECTS
        }
        return cls(
            enabled=bool(config.get("video_transitions_enabled", True)),
            style=style,
            duration_seconds=bounded_float(
                "video_transition_duration_seconds", 1.0, 0.3, 3.0,
            ),
            automatic_lead_seconds=bounded_float(
                "video_transition_automatic_lead_seconds", 1.0, 0.3, 3.0,
            ),
            manual_duration_seconds=bounded_float(
                "video_transition_manual_duration_seconds", 0.5, 0.3, 3.0,
            ),
            enabled_effects=enabled_effects,
        )


class VideoTransitionController:
    """Pure transition policy/state, deliberately testable without Qt video."""

    def __init__(self, rng: Optional[random.Random] = None):
        self.state = TransitionState.IDLE
        self.trigger: Optional[str] = None
        self.effect = "Fade Black"
        self.switch_requested = False
        self.automatic_started = False
        self._last_random_effect: Optional[str] = None
        self._rng = rng or random.Random()

    def reset_for_media(self) -> None:
        self.automatic_started = False

    @staticmethod
    def supports(source: MediaType, target: Optional[MediaType] = None) -> bool:
        if source in (MediaType.KARAOKE, MediaType.UNSUPPORTED):
            return False
        if target in (MediaType.KARAOKE, MediaType.UNSUPPORTED):
            return False
        return source == MediaType.VIDEO or target == MediaType.VIDEO

    def select_effect(self, preferences: VideoTransitionPreferences) -> str:
        style = preferences.style
        if style in EFFECTS:
            return style if preferences.enabled_effects.get(style, True) else "Fade Black"
        pools: dict[str, Sequence[str]] = {
            "Random Smooth": SMOOTH_EFFECTS,
            "Random Energetic": ENERGETIC_EFFECTS,
            "Random All": EFFECTS,
        }
        candidates = [
            effect for effect in pools.get(style, ())
            if preferences.enabled_effects.get(effect, True)
        ]
        if not candidates:
            return "Fade Black"
        if len(candidates) > 1 and self._last_random_effect in candidates:
            candidates = [e for e in candidates if e != self._last_random_effect]
        selected = self._rng.choice(candidates)
        self._last_random_effect = selected
        return selected

    def begin_outgoing(self, trigger: str, effect: str) -> bool:
        if self.state != TransitionState.IDLE:
            return False
        self.state = TransitionState.OUTGOING
        self.trigger = trigger
        self.effect = effect if effect in EFFECTS else "Fade Black"
        self.switch_requested = False
        if trigger == "automatic":
            self.automatic_started = True
        return True

    def begin_incoming_only(self, effect: str) -> bool:
        if self.state != TransitionState.IDLE:
            return False
        self.state = TransitionState.SWITCHING
        self.trigger = "incoming-only"
        self.effect = effect if effect in EFFECTS else "Fade Black"
        self.switch_requested = True
        return True

    def mark_switching(self) -> bool:
        if self.state != TransitionState.OUTGOING or self.switch_requested:
            return False
        self.switch_requested = True
        self.state = TransitionState.SWITCHING
        return True

    def mark_incoming(self) -> bool:
        if self.state != TransitionState.SWITCHING:
            return False
        self.state = TransitionState.INCOMING
        return True

    def complete(self) -> None:
        self.state = TransitionState.IDLE
        self.trigger = None
        self.switch_requested = False

    def cancel(self, *, allow_automatic_retry: bool = False) -> bool:
        if self.state == TransitionState.IDLE:
            return False
        self.state = TransitionState.CANCELLING
        if allow_automatic_retry:
            self.automatic_started = False
        self.complete()
        return True

    def should_start_automatic(
        self, position_ms: int, duration_ms: int, lead_seconds: float,
    ) -> bool:
        if self.state != TransitionState.IDLE or self.automatic_started:
            return False
        if duration_ms <= 0 or position_ms < 0 or position_ms > duration_ms:
            return False
        remaining_ms = duration_ms - position_ms
        return remaining_ms <= max(0.0, lead_seconds) * 1000.0

    def should_cancel_for_seek(
        self, position_ms: int, duration_ms: int, lead_seconds: float,
    ) -> bool:
        if (
            self.state != TransitionState.OUTGOING
            or self.trigger != "automatic"
            or self.switch_requested
            or duration_ms <= 0
        ):
            return False
        remaining_ms = duration_ms - max(0, position_ms)
        return remaining_ms > (max(0.0, lead_seconds) + 0.75) * 1000.0


class VideoTransitionManager(QtCore.QObject):
    """Qt orchestration around :class:`VideoTransitionController`."""

    READY_TIMEOUT_MS = 1800

    def __init__(
        self,
        parent: QtCore.QObject,
        host_provider: Callable[[str, Optional[MediaType]], Optional[QtWidgets.QWidget]],
        advance_callback: Callable[[str], Optional[MediaType]],
        target_hint_callback: Optional[Callable[[], Optional[MediaType]]] = None,
        diagnostic_callback: Optional[Callable[[str, Mapping[str, object]], None]] = None,
        overlay: Optional[VideoTransitionOverlay] = None,
        rng: Optional[random.Random] = None,
        dual_engine: Optional[DualVideoTransitionEngine] = None,
    ):
        super().__init__(parent)
        self.controller = VideoTransitionController(rng)
        self.preferences = VideoTransitionPreferences()
        self._host_provider = host_provider
        self._advance_callback = advance_callback
        self._target_hint_callback = target_hint_callback
        self._diagnostic_callback = diagnostic_callback
        self._overlay = overlay or VideoTransitionOverlay()
        # Phase 2A: an optional genuine dual-video cross-dissolve engine.
        # Tried first for Video->Video automatic/manual triggers; any
        # decline (disabled, not ready, or a failure at any stage) falls
        # through to the overlay logic below unchanged -- this class remains
        # the permanent, always-available fallback, not a second thing that
        # has to be kept in sync with the dual engine's behaviour.
        self._dual_engine = dual_engine
        self._closing = False
        self._target_media_type: Optional[MediaType] = None
        self._outgoing_duration_ms = 500
        self._incoming_duration_ms = 500
        # Single guarded reveal entry point (see _start_incoming): the
        # cover must never lift for a video target until media_ready() has
        # genuinely confirmed the incoming video, closing three separate
        # premature-reveal paths at once -- the optimistic 1.8s
        # _on_ready_timeout() reveal, _start_incoming()'s own except branch
        # (previously reachable before any confirmation), and begin_
        # incoming_only()'s exception path. A real-device recording showed
        # the app's own background exposed for ~334ms during exactly this
        # kind of gap. _incoming_wait_extensions bounds how long
        # _on_ready_timeout() keeps re-arming rather than giving up
        # silently (still never reveals either way -- see MAX_INCOMING_
        # WAIT_EXTENSIONS below).
        self._incoming_confirmed: bool = False
        self._incoming_wait_extensions: int = 0
        self._ready_timer = QtCore.QTimer(self)
        self._ready_timer.setSingleShot(True)
        self._ready_timer.timeout.connect(self._on_ready_timeout)
        if parent is not None:
            parent.destroyed.connect(self.shutdown)

    @property
    def state(self) -> TransitionState:
        return self.controller.state

    @property
    def dual_engine(self) -> Optional[DualVideoTransitionEngine]:
        return self._dual_engine

    def configure(self, preferences: VideoTransitionPreferences) -> None:
        self.preferences = preferences

    def configure_dual(self, preferences) -> None:
        if self._dual_engine is not None:
            self._dual_engine.configure(preferences)

    def media_changed(self) -> None:
        self.controller.reset_for_media()
        if self._dual_engine is not None:
            self._dual_engine.media_changed()

    def request_manual_next(self, current_media_type: MediaType) -> bool:
        if not self.preferences.enabled or current_media_type != MediaType.VIDEO:
            return False
        if not self.controller.supports(current_media_type, self._target_hint()):
            return False
        if self.state != TransitionState.IDLE:
            self._record("next_ignored", {"state": self.state.value})
            return True
        if self._dual_engine is not None and self._dual_engine.controller.state in COMMITTED_STATES:
            # A GPU cross-dissolve already committed and is still running
            # (this Phase 1 controller's own state stays IDLE for its
            # whole lifetime -- see handle_natural_end's matching comment).
            # A second manual-next press landing here must not start a
            # competing Phase 1 overlay transition on top of it.
            self._record("next_ignored", {"state": "dual_committed"})
            return True
        if self._dual_engine is not None and self._dual_engine.try_commit(
            "manual", self.preferences.manual_duration_seconds,
        ):
            return True
        self._start_outgoing("manual", self.preferences.manual_duration_seconds)
        return True

    def observe_position(
        self, position_ms: int, duration_ms: int, current_media_type: MediaType,
    ) -> None:
        if self._closing or current_media_type != MediaType.VIDEO:
            return
        if self.controller.should_cancel_for_seek(
            position_ms, duration_ms, self.preferences.automatic_lead_seconds,
        ):
            self.cancel("seeked_away_from_end", allow_automatic_retry=True)
            return
        if self._dual_engine is not None:
            self._dual_engine.observe_position(
                position_ms, duration_ms, current_media_type,
            )
            if self._dual_engine.controller.state in COMMITTED_STATES:
                # A genuine GPU cross-dissolve already committed -- most
                # often now via DualVideoTransitionEngine's own bounded
                # deadline timer (v1.0.53), which commits through an
                # independent QTimer callback rather than this method's own
                # try_commit() branch below, so it never sets this
                # controller's automatic_started. Without this check, the
                # very next position tick (Qt keeps ticking for roughly the
                # whole blend duration, since the outgoing deck is still
                # playing) would see this controller still IDLE with
                # automatic_started still False, decide the lead window is
                # still open, and fall through to _start_outgoing() below --
                # a second, competing Phase 1 overlay transition layered on
                # top of the already-running GPU blend (confirmed via
                # v1.0.54 diagnostics on real playback: video_transition_
                # requested/outgoing_started firing at the same millisecond
                # as video_dual_transition_committed in every failing
                # transition, and in at least one case cascading into a
                # second queue advance via handle_natural_end()'s own
                # switch-point branch once this controller ended up
                # OUTGOING -- silently skipping the next track). Mirrors the
                # identical guard already in handle_natural_end()/
                # request_manual_next() (v1.0.51) -- this was the one call
                # site that fix didn't cover.
                return
        if (
            self.preferences.enabled
            and self.controller.supports(current_media_type, self._target_hint())
            and self.controller.should_start_automatic(
                position_ms, duration_ms,
                self.preferences.automatic_lead_seconds,
            )
        ):
            if self._dual_engine is not None and self._dual_engine.try_commit(
                "automatic", self.preferences.duration_seconds,
            ):
                # A genuine cross-dissolve is now running inside the video
                # subprocess -- mark automatic_started exactly as
                # begin_outgoing() would so should_start_automatic() does
                # not fire again on the next position tick and layer the
                # overlay on top of it.
                self.controller.automatic_started = True
                self._record("dual_transition_requested", {"trigger": "automatic"})
                return
            # For an automatic transition, let the outgoing paint develop
            # over the actual ending frames.  Using half the configured total
            # duration here made a 0.5 s transition appear for only 0.25 s,
            # often too late to notice.  Finishing at the reported media end
            # also avoids cutting away from the current video early.
            remaining_ms = max(1, duration_ms - position_ms)
            self._start_outgoing(
                "automatic",
                self.preferences.duration_seconds,
                outgoing_duration_ms=remaining_ms,
            )

    def begin_incoming_only(
        self, source: MediaType, target: MediaType,
    ) -> bool:
        if (
            self._closing
            or not self.preferences.enabled
            or not self.controller.supports(source, target)
            or source != MediaType.AUDIO
            or target != MediaType.VIDEO
        ):
            return False
        effect = self.controller.select_effect(self.preferences)
        if not self.controller.begin_incoming_only(effect):
            return False
        self._target_media_type = target
        self._incoming_confirmed = False
        self._incoming_wait_extensions = 0
        self._incoming_duration_ms = max(
            1, int(self.preferences.duration_seconds * 1000.0) // 2,
        )
        self._record("requested", {"trigger": "incoming-only", "effect": effect})
        try:
            self._overlay.set_host(self._host_provider("covered", target))
            self._overlay.show_covered(effect)
            self._ready_timer.start(self.READY_TIMEOUT_MS)
        except Exception as ex:
            self._visual_failure(ex, switch_needed=False)
        return True

    def handle_natural_end(self, current_media_type: MediaType) -> bool:
        if not self.preferences.enabled or current_media_type != MediaType.VIDEO:
            return False
        if not self.controller.supports(current_media_type, self._target_hint()):
            return False
        if self.state == TransitionState.OUTGOING:
            self._reach_switch_point()
            return True
        if self.state in (TransitionState.SWITCHING, TransitionState.INCOMING):
            return True
        if self._dual_engine is not None and self._dual_engine.controller.state in COMMITTED_STATES:
            # A genuine GPU cross-dissolve already committed (try_commit's
            # own commitment point already advanced the queue once) and is
            # still running in the subprocess. This Phase 1 controller's
            # own state stays IDLE for the whole dual-engine lifetime --
            # the two are mutually exclusive alternatives chosen once at
            # try_commit()/_start_outgoing(), not a shared state machine --
            # so without this check, the outgoing deck's own natural
            # end-of-media (reached because the GPU transition started
            # near, but not exactly at, that deck's own last frame) would
            # fall through to _start_outgoing() below and begin a *second*,
            # redundant Phase 1 overlay transition on top of the
            # still-running GPU one: a second queue advance, plus classic
            # single-video load()/stop()/host-hide teardown stomping on the
            # compositor window the GPU transition still owns. That is
            # exactly what surfaced as the app's own background flashing
            # through between videos. Simply recognising the dual engine
            # already has this covered is enough.
            return True
        if self._dual_engine is not None and self._dual_engine.try_commit(
            "automatic", self.preferences.duration_seconds,
        ):
            # Final safety net, not the primary path. try_commit() is meant
            # to be reached well before this: DualVideoTransitionEngine now
            # owns a bounded deadline timer of its own (armed the moment
            # secondary becomes ready, from a known position/duration, and
            # rescheduled on every position tick) that calls try_commit()
            # on a wall-clock deadline rather than waiting for a
            # position-tick to happen to land inside the lead window.
            # Natural end-of-media is inherently too late to *start* a
            # genuine dual-video transition -- by then the outgoing deck
            # has no live frame left to contribute, so a commit that only
            # ever happens here shows as a black gap rather than real A+B
            # overlap (found via v1.0.52 diagnostics on real near-end
            # playback: gpu_transition_started and track_completed shared
            # the exact same timestamp in every transition of that
            # session). This branch stays only for the rare case the
            # deadline timer itself is missed (e.g. a GUI-thread stall) --
            # still better than falling to the Phase 1 overlay when the
            # secondary has, in fact, been sitting ready.
            self.controller.automatic_started = True
            self._record("dual_transition_requested", {"trigger": "automatic"})
            return True
        self._start_outgoing("automatic", self.preferences.duration_seconds)
        return True

    def playback_paused(self, paused: bool) -> None:
        """Forwarded from window.py's pause()/resume() handling. A no-op
        unless a genuine dual cross-dissolve is actively committed -- Phase
        1's overlay has no playing timeline of its own to pause."""
        if self._dual_engine is not None:
            self._dual_engine.playback_paused(paused)

    def media_ready(self, media_type: MediaType) -> None:
        if self._closing or self.state != TransitionState.SWITCHING:
            return
        if self._target_media_type == MediaType.VIDEO and media_type != MediaType.VIDEO:
            return
        self._incoming_confirmed = True
        self._record("incoming_media_ready", {"media_type": media_type.value})
        self._start_incoming()

    def incoming_media_superseded(self, media_type: MediaType) -> bool:
        """Non-video media has just become authoritative while the cover is
        still up waiting for an incoming VIDEO to confirm.

        Real-device defect (Plex video -> Plex MP3 at natural end): the
        switch point samples the incoming media type straight after the
        queue advance, but Plex audio is dispatched asynchronously -- the
        media type is still VIDEO at that instant, so the cover waited for a
        video media_ready() that an audio track never sends, exhausted its
        bounded wait, and by design stayed up indefinitely over the stage.
        No video readiness can arrive now, so waiting any longer only
        strands the cover: re-target and reveal what now owns the stage.
        A no-op unless covered and waiting on a VIDEO target -- a transition
        already revealing, or targeting non-video media, is untouched."""
        if (
            self._closing
            or media_type == MediaType.VIDEO
            or self.state != TransitionState.SWITCHING
            or self._target_media_type != MediaType.VIDEO
        ):
            return False
        self._record("incoming_media_superseded", {"media_type": media_type.value})
        self._target_media_type = media_type
        self._incoming_confirmed = True
        self._start_incoming()
        return True

    def incoming_media_failed(self, reason: str) -> Optional[str]:
        """The media this covered transition was waiting on has failed
        before it ever became authoritative -- e.g. an asynchronous Plex
        resolve that returned success=False after the switch point. No
        replacement media owns the stage, so this is neither media_ready()
        nor incoming_media_superseded(): it is a distinct terminal outcome,
        routed through the canonical cancel() reset.

        Returns the terminated transition's trigger ("automatic",
        "manual", "incoming-only") so the caller can decide what the stage
        should show next, or None when nothing was pending (idle, already
        revealing/complete/cancelled, or closing) -- a no-op then."""
        if self._closing or self.state != TransitionState.SWITCHING:
            return None
        trigger = self.controller.trigger or ""
        self._record("incoming_media_failed", {"reason": reason, "trigger": trigger})
        self.cancel(f"incoming_media_failed:{reason}")
        return trigger

    def playback_stopped(self, reason: str = "playback_stopped") -> None:
        self.cancel(reason)
        if self._dual_engine is not None:
            self._dual_engine.playback_stopped(reason)

    def cancel(self, reason: str, *, allow_automatic_retry: bool = False) -> bool:
        # Cancelling a pending dual preload is always safe to attempt
        # alongside the overlay cancel below -- DualVideoTransitionEngine
        # refuses on its own once a dual transition has actually committed
        # (see its module docstring), so this never disturbs a genuine
        # cross-dissolve already in flight.
        if self._dual_engine is not None:
            self._dual_engine.cancel(reason)
        if not self.controller.cancel(allow_automatic_retry=allow_automatic_retry):
            return False
        self._ready_timer.stop()
        try:
            self._overlay.cancel()
        except Exception:
            pass
        self._target_media_type = None
        self._incoming_confirmed = False
        self._incoming_wait_extensions = 0
        self._record("cancelled", {"reason": reason})
        return True

    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._ready_timer.stop()
        self.controller.cancel()
        if self._dual_engine is not None:
            self._dual_engine.shutdown()
        try:
            self._overlay.shutdown()
        except Exception:
            pass

    def _start_outgoing(
        self,
        trigger: str,
        duration_seconds: float,
        *,
        outgoing_duration_ms: Optional[int] = None,
    ) -> None:
        effect = self.controller.select_effect(self.preferences)
        if not self.controller.begin_outgoing(trigger, effect):
            return
        self._target_media_type = None
        self._incoming_confirmed = False
        self._incoming_wait_extensions = 0
        total_duration_ms = max(300, int(duration_seconds * 1000.0))
        self._outgoing_duration_ms = max(
            1,
            int(outgoing_duration_ms)
            if outgoing_duration_ms is not None
            else total_duration_ms // 2,
        )
        self._incoming_duration_ms = max(1, total_duration_ms // 2)
        self._record(
            "requested",
            {
                "trigger": trigger,
                "effect": effect,
                "outgoing_duration_ms": self._outgoing_duration_ms,
            },
        )
        self._record("outgoing_started", {"trigger": trigger, "effect": effect})
        self._start_overlay_outgoing(effect)

    def _start_overlay_outgoing(self, effect: str) -> None:
        try:
            self._overlay.set_host(self._host_provider("outgoing", MediaType.VIDEO))
            self._overlay.animate_outgoing(
                effect, self._outgoing_duration_ms,
                self._reach_switch_point,
            )
        except Exception as ex:
            if effect != "Fade Black":
                self.controller.effect = "Fade Black"
                self._record("fallback_used", {"from": effect, "error": str(ex)})
                try:
                    self._overlay.cancel()
                    self._overlay.set_host(
                        self._host_provider("outgoing", MediaType.VIDEO)
                    )
                    self._overlay.animate_outgoing(
                        "Fade Black", self._outgoing_duration_ms,
                        self._reach_switch_point,
                    )
                    return
                except Exception as fallback_ex:
                    ex = fallback_ex
            self._visual_failure(ex, switch_needed=True)

    def _reach_switch_point(self) -> None:
        if self._closing or not self.controller.mark_switching():
            return
        self._record("switch_point_reached", {"trigger": self.controller.trigger or ""})
        try:
            self._overlay.set_host(self._host_provider("covered", None))
            self._overlay.show_covered(self.controller.effect)
        except Exception as ex:
            self._record("error", {"stage": "covered", "error": str(ex)})
        trigger = self.controller.trigger or "automatic"
        try:
            self._record("queue_advance_requested", {"trigger": trigger})
            self._target_media_type = self._advance_callback(trigger)
        except Exception as ex:
            self._target_media_type = None
            self._record("error", {"stage": "advance", "error": str(ex)})
        if self._target_media_type == MediaType.VIDEO:
            self._ready_timer.start(self.READY_TIMEOUT_MS)
        else:
            self._start_incoming()

    def _start_incoming(self) -> None:
        if self._closing:
            return
        if self._target_media_type == MediaType.VIDEO and not self._incoming_confirmed:
            # Never reveal the incoming side of a video transition until
            # media_ready() has genuinely confirmed it -- the cover stays
            # up (state stays SWITCHING) instead of advancing here. See
            # _on_ready_timeout below for what happens while waiting.
            return
        if not self.controller.mark_incoming():
            return
        self._ready_timer.stop()
        self._record(
            "incoming_reveal_started",
            {"media_type": self._target_media_type.value if self._target_media_type else "none"},
        )
        try:
            self._overlay.set_host(
                self._host_provider("incoming", self._target_media_type)
            )
            self._overlay.animate_incoming(
                self.controller.effect,
                self._incoming_duration_ms,
                self._complete,
            )
        except Exception as ex:
            self._record("error", {"stage": "incoming", "error": str(ex)})
            try:
                self._overlay.cancel()
            except Exception:
                pass
            self._complete()

    def _complete(self) -> None:
        if self._closing:
            return
        self._ready_timer.stop()
        self.controller.complete()
        self._target_media_type = None
        self._incoming_confirmed = False
        self._incoming_wait_extensions = 0
        self._record("complete", {})

    # Bounded re-arms of the ready timer while waiting for media_ready() --
    # never gives up by revealing early. Total bounded wait before
    # "exhausted": READY_TIMEOUT_MS * (1 + MAX_INCOMING_WAIT_EXTENSIONS).
    # Once exhausted, the cover stays up indefinitely; the video backend's
    # own error path (playback_stopped -> cancel()) remains the real
    # safety net for a genuinely hung/failed load, exactly as it already
    # is for _on_video_error today.
    MAX_INCOMING_WAIT_EXTENSIONS = 2

    def _on_ready_timeout(self) -> None:
        if self.state != TransitionState.SWITCHING:
            return
        self._record("media_ready_timeout", {"timeout_ms": self.READY_TIMEOUT_MS})
        if self._incoming_wait_extensions < self.MAX_INCOMING_WAIT_EXTENSIONS:
            self._incoming_wait_extensions += 1
            self._record("incoming_wait_extended", {
                "extension": self._incoming_wait_extensions,
                "timeout_ms": self.READY_TIMEOUT_MS,
            })
            self._ready_timer.start(self.READY_TIMEOUT_MS)
            return
        self._record("incoming_wait_exhausted", {
            "total_wait_ms": self.READY_TIMEOUT_MS * (1 + self._incoming_wait_extensions),
        })
        # Deliberately does not call _start_incoming(): it would no-op
        # against the guard above anyway (still unconfirmed). The cover
        # stays covering until media_ready() or a genuine failure path
        # resolves it.

    def _visual_failure(self, ex: Exception, *, switch_needed: bool) -> None:
        self._record("error", {"stage": "visual", "error": str(ex)})
        if switch_needed:
            QtCore.QTimer.singleShot(0, self._reach_switch_point)
        else:
            self._start_incoming()

    def _record(self, event: str, details: Mapping[str, object]) -> None:
        callback = self._diagnostic_callback
        if callback is None:
            return
        try:
            callback(event, details)
        except Exception:
            pass

    def _target_hint(self) -> Optional[MediaType]:
        callback = self._target_hint_callback
        if callback is None:
            return None
        try:
            return callback()
        except Exception:
            return None
