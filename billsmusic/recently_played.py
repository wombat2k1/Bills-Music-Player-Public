"""Persistent Recently Played history and listened-time qualification policy."""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

HISTORY_VERSION = 1
HISTORY_LIMIT = 500


@dataclass
class RecentlyPlayedEntry:
    path: str
    title: str
    artist: str
    album: str
    duration_seconds: float
    played_at: str

    @classmethod
    def from_dict(cls, value: Dict) -> Optional["RecentlyPlayedEntry"]:
        if not isinstance(value, dict) or not isinstance(value.get("path"), str):
            return None
        try:
            return cls(
                path=value["path"],
                title=str(value.get("title") or ""),
                artist=str(value.get("artist") or ""),
                album=str(value.get("album") or ""),
                duration_seconds=max(0.0, float(value.get("duration_seconds") or 0.0)),
                played_at=str(value.get("played_at") or ""),
            )
        except (TypeError, ValueError):
            return None


def qualification_threshold(duration_seconds: float) -> float:
    duration = max(0.0, float(duration_seconds or 0.0))
    return min(30.0, duration * 0.5) if duration > 0 else 30.0


class ListenTracker:
    """Accumulate genuine listened time for one official playback generation."""

    def __init__(self):
        self.reset("", -1, 0.0)

    def reset(self, path: str, generation: int, duration_seconds: float):
        self.path = str(path or "")
        self.generation = int(generation)
        self.duration_seconds = max(0.0, float(duration_seconds or 0.0))
        self.threshold_seconds = qualification_threshold(self.duration_seconds)
        self.listened_seconds = 0.0
        self.recorded = False
        self._last_time: Optional[float] = None
        self._last_position: Optional[float] = None

    def update(
        self, now: float, position: Optional[float], *,
        active: bool, paused: bool, seeking: bool, closing: bool,
    ) -> bool:
        now = float(now)
        if position is None:
            self._last_time = now
            self._last_position = None
            return False
        position = max(0.0, float(position))
        if self._last_time is None or self._last_position is None:
            self._last_time = now
            self._last_position = position
            return False
        elapsed = max(0.0, now - self._last_time)
        position_delta = position - self._last_position
        self._last_time = now
        self._last_position = position
        if (
            self.recorded or not active or paused or seeking or closing
            or elapsed <= 0.0 or elapsed > 2.0
            or position_delta <= 0.0 or position_delta > 2.0
        ):
            return False
        self.listened_seconds += min(elapsed, position_delta)
        if self.listened_seconds >= self.threshold_seconds:
            self.recorded = True
            return True
        return False


class RecentlyPlayedRepository:
    def __init__(self, path: str, logger: Optional[Callable[[str], None]] = None):
        self.path = path
        self.logger = logger or (lambda message: None)

    def load(self) -> List[RecentlyPlayedEntry]:
        started = time.perf_counter()
        invalid = 0
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if payload.get("version") != HISTORY_VERSION:
                raise ValueError("unsupported history version")
            entries = []
            for raw in payload.get("entries", []):
                entry = RecentlyPlayedEntry.from_dict(raw)
                if entry is None:
                    invalid += 1
                else:
                    entries.append(entry)
            entries = entries[:HISTORY_LIMIT]
        except FileNotFoundError:
            entries = []
        except Exception as ex:
            self.logger(f"Recently Played load skipped: {ex}")
            entries = []
        self.logger(
            f"Recently Played loaded: entries={len(entries)}; invalid_entries={invalid}; "
            f"duration_ms={(time.perf_counter() - started) * 1000.0:.1f}"
        )
        return entries

    def save(self, entries: List[RecentlyPlayedEntry]):
        started = time.perf_counter()
        bounded = list(entries[:HISTORY_LIMIT])
        folder = os.path.dirname(self.path)
        os.makedirs(folder, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix="recently_played-", suffix=".tmp", dir=folder
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": HISTORY_VERSION,
                        "entries": [asdict(entry) for entry in bounded],
                    },
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.logger(
            f"Recently Played saved: entries={len(bounded)}; "
            f"duration_ms={(time.perf_counter() - started) * 1000.0:.1f}"
        )


def make_entry(
    path: str, title: str, artist: str, album: str, duration_seconds: float,
) -> RecentlyPlayedEntry:
    return RecentlyPlayedEntry(
        path=path,
        title=title,
        artist=artist,
        album=album,
        duration_seconds=max(0.0, float(duration_seconds or 0.0)),
        played_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


def add_entry(
    entries: List[RecentlyPlayedEntry], entry: RecentlyPlayedEntry,
) -> List[RecentlyPlayedEntry]:
    result = list(entries)
    if result and result[0].path == entry.path:
        result[0] = entry
    else:
        result.insert(0, entry)
    return result[:HISTORY_LIMIT]
