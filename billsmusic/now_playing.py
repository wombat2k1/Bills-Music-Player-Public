"""Small, GUI-thread-owned identity guard for Now Playing detail work."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NowPlayingIdentity:
    generation: int
    path: str


class NowPlayingGeneration:
    """Monotonic track identity; no I/O and no widget access."""

    def __init__(self) -> None:
        self._generation = 0
        self._identity = NowPlayingIdentity(0, "")

    @property
    def identity(self) -> NowPlayingIdentity:
        return self._identity

    def begin(self, path: str) -> NowPlayingIdentity:
        """Phase D: deliberately carries no positional index.

        This used to take a `queue_index` that was never read anywhere --
        is_current() compares generation and path only -- and that
        _activate_track_ui fed from self.current_index, which for most
        callers was a LIBRARY index, not a queue one. Keeping a
        mis-labelled, unread positional field would just relocate the
        ambiguity into this class. If a queue identity is ever genuinely
        needed here it should be the Phase B queue token, added when
        something actually consumes it."""
        self._generation += 1
        self._identity = NowPlayingIdentity(self._generation, path or "")
        return self._identity

    def is_current(self, generation: int, path: str) -> bool:
        current = self._identity
        return current.generation == generation and current.path == (path or "")
