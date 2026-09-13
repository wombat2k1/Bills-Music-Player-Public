"""Small, GUI-thread-owned identity guard for Now Playing detail work."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NowPlayingIdentity:
    generation: int
    path: str
    queue_index: int | None


class NowPlayingGeneration:
    """Monotonic track identity; no I/O and no widget access."""

    def __init__(self) -> None:
        self._generation = 0
        self._identity = NowPlayingIdentity(0, "", None)

    @property
    def identity(self) -> NowPlayingIdentity:
        return self._identity

    def begin(self, path: str, queue_index: int | None) -> NowPlayingIdentity:
        self._generation += 1
        self._identity = NowPlayingIdentity(self._generation, path or "", queue_index)
        return self._identity

    def is_current(self, generation: int, path: str) -> bool:
        current = self._identity
        return current.generation == generation and current.path == (path or "")
