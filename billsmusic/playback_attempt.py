"""Playback stability hardening, Phase A (BMP-002): a single umbrella
identity establishing which in-flight play request is currently
authoritative, layered ABOVE the existing specialist tokens
(pending_next, _playback_generation, _plex_audio_load_token, crossfade
tokens, karaoke generations, video transition IDs) -- none of those are
replaced or removed by this. They stay in place as additional defence;
this is a second, independent gate any async callback must also clear
before it's allowed to mutate authoritative playback state.

Core invariant: only the current PlaybackAttempt may make media
authoritative. An attempt superseded by a later one (Stop, or a new
selection) may still finish its own async work harmlessly, but that
work must never start playback, change current_path/backend override,
mark queue history, change current-track UI, trigger Next, or change
playback_expected/pending progression state.

Deliberately minimal -- no terminalisation seam, no queue-commit
semantics, no worker-ownership model. Those are later phases (see
CODEX_HANDOFF.md's playback-stability roadmap)."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .media_type import MediaType


class PlaybackAttemptState(Enum):
    REQUESTED = "requested"
    PREPARING = "preparing"
    STARTING = "starting"
    PLAYING = "playing"
    FAILED = "failed"
    CANCELLED = "cancelled"


# An attempt in one of these states can no longer become (or continue
# being) authoritative -- it is done, one way or another.
TERMINAL_STATES = (PlaybackAttemptState.FAILED, PlaybackAttemptState.CANCELLED)


@dataclass
class PlaybackAttempt:
    attempt_id: int
    source_identity: str
    media_type: Optional[MediaType]
    reason: str
    queue_entry_id: Optional[object] = None
    state: PlaybackAttemptState = field(default=PlaybackAttemptState.REQUESTED)
    # Why a dispatch that never started playback failed, set as it becomes
    # terminal (e.g. "source_unavailable" for a missing local file), so the
    # advancement policy can tell an unavailable entry from a backend failure.
    terminal_reason: Optional[str] = None
    # The automatic advancement reason ("near-end", "video-ended", ...) when
    # this attempt was dispatched by automatic advancement rather than an
    # explicit request. It stays with the attempt through asynchronous
    # preparation, so a result arriving while paused is held (Astra F2).
    automatic_reason: Optional[str] = None

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES
