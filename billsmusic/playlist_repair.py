"""M3U parsing, conservative missing-track matching, and safe saving."""
from __future__ import annotations

import ntpath
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Optional

from .media_capabilities import is_playlist_supported


@dataclass(frozen=True)
class PlaylistEntry:
    path: str
    resolved_path: Optional[str] = None
    display_title: Optional[str] = None
    artist: Optional[str] = None
    duration_seconds: Optional[float] = None
    is_missing: bool = False
    original_path: Optional[str] = None

    @property
    def playable_path(self) -> Optional[str]:
        return None if self.is_missing else (self.resolved_path or self.path)

    @property
    def is_supported_media(self) -> bool:
        """Registry classification only; unsupported M3U rows remain preserved."""
        return is_playlist_supported(self.resolved_path or self.path)

    def with_replacement(self, path: str) -> "PlaylistEntry":
        if not os.path.isfile(path):
            raise ValueError("Replacement must be an existing file")
        return replace(self, resolved_path=os.path.abspath(path), is_missing=False)


@dataclass(frozen=True)
class MatchSuggestion:
    path: Optional[str]
    confidence: str
    reason: str
    preselected: bool = False


def _normalise_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").casefold()))


def _windows_absolute(path: str) -> bool:
    drive, _ = ntpath.splitdrive(path)
    return bool(drive) or path.startswith(("\\\\", "//"))


def _resolve_path(saved_path: str, playlist_dir: str) -> tuple[str, bool]:
    cleaned = saved_path.strip().replace("/", os.sep).replace("\\", os.sep)
    if os.path.isabs(cleaned) or _windows_absolute(saved_path):
        return os.path.normpath(cleaned), False
    return os.path.abspath(os.path.join(playlist_dir, cleaned)), True


def _parse_extinf(line: str):
    payload = line.partition(":")[2]
    duration_text, _, label = payload.partition(",")
    try:
        duration = float(duration_text)
    except (TypeError, ValueError):
        duration = None
    label = label.strip()
    artist = None
    title = label or None
    if " - " in label:
        artist, title = (part.strip() or None for part in label.split(" - ", 1))
    return duration, artist, title


def load_m3u(filename: str, *, exists=os.path.isfile):
    """Return entries and counters while preserving order, duplicates and metadata."""
    started_dir = os.path.dirname(os.path.abspath(filename))
    entries = []
    pending = (None, None, None)
    existence_cache = {}
    relative_count = 0
    with open(filename, "r", encoding="utf-8-sig", errors="replace") as handle:
        for raw in handle:
            line = raw.rstrip("\r\n")
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.upper().startswith("#EXTINF:"):
                pending = _parse_extinf(stripped)
                continue
            if stripped.startswith("#"):
                continue
            resolved, was_relative = _resolve_path(stripped, started_dir)
            relative_count += int(was_relative)
            key = os.path.normcase(resolved)
            present = existence_cache.get(key)
            if present is None:
                present = bool(exists(resolved))
                existence_cache[key] = present
            duration, artist, title = pending
            pending = (None, None, None)
            entries.append(
                PlaylistEntry(
                    path=resolved,
                    resolved_path=resolved if present else None,
                    display_title=title,
                    artist=artist,
                    duration_seconds=duration,
                    is_missing=not present,
                    original_path=stripped,
                )
            )
    return entries, {
        "valid": sum(not entry.is_missing for entry in entries),
        "missing": sum(entry.is_missing for entry in entries),
        "relative_resolved": relative_count,
        "total": len(entries),
    }


def _library_record(record):
    path = str(record.get("path") or "")
    title = str(record.get("title") or Path(path).stem)
    artist = str(record.get("artist") or record.get("album_artist") or "")
    album = str(record.get("album") or "")
    return path, title, artist, album


def suggest_replacement(
    entry: PlaylistEntry, library_records: Iterable[dict]
) -> MatchSuggestion:
    records = [_library_record(record) for record in library_records]
    records = [record for record in records if record[0]]
    original = entry.original_path or entry.path
    filename = ntpath.basename(original).casefold()
    stem = _normalise_text(Path(ntpath.basename(original)).stem)
    title = _normalise_text(entry.display_title or Path(original).stem)
    artist = _normalise_text(entry.artist or "")

    def unique(matches, confidence, reason, preselected=True):
        paths = list(dict.fromkeys(record[0] for record in matches))
        if len(paths) == 1:
            return MatchSuggestion(paths[0], confidence, reason, preselected)
        if len(paths) > 1:
            return MatchSuggestion(None, "Possible", f"Ambiguous {reason}", False)
        return None

    result = unique(
        [record for record in records if ntpath.basename(record[0]).casefold() == filename],
        "Exact", "Exact filename",
    )
    if result:
        return result
    if artist and title:
        result = unique(
            [
                record for record in records
                if _normalise_text(record[1]) == title
                and _normalise_text(record[2]) == artist
            ],
            "Exact", "Exact artist and title",
        )
        if result:
            return result
    result = unique(
        [
            record for record in records
            if _normalise_text(Path(record[0]).stem) == stem
        ],
        "Strong", "Same filename with different extension",
    )
    if result:
        return result
    title_matches = [
        record for record in records if _normalise_text(record[1]) == title
    ]
    result = unique(
        title_matches, "Possible", "Exact title; artist differs", False
    )
    return result or MatchSuggestion(None, "No match", "No conservative match", False)


def apply_replacements(entries, replacements):
    """Apply index->path replacements without changing ordering or duplicates."""
    updated = list(entries)
    for index, path in replacements.items():
        if 0 <= index < len(updated) and path:
            updated[index] = updated[index].with_replacement(path)
    return updated


def save_m3u(filename: str, entries: Iterable[PlaylistEntry]):
    """Atomically save entries after making one bounded backup."""
    filename = os.path.abspath(filename)
    backup = filename + ".bak"
    if os.path.exists(filename):
        try:
            shutil.copy2(filename, backup)
        except Exception as ex:
            raise OSError(f"Could not create playlist backup: {ex}") from ex
    directory = os.path.dirname(filename)
    fd, temporary = tempfile.mkstemp(
        prefix=os.path.basename(filename) + ".", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("#EXTM3U\n")
            for entry in entries:
                if entry.display_title or entry.artist or entry.duration_seconds is not None:
                    duration = (
                        int(entry.duration_seconds)
                        if entry.duration_seconds is not None else -1
                    )
                    label = " - ".join(
                        part for part in (entry.artist, entry.display_title) if part
                    )
                    handle.write(f"#EXTINF:{duration},{label}\n")
                output = (
                    entry.resolved_path
                    if not entry.is_missing and entry.resolved_path
                    else entry.original_path or entry.path
                )
                handle.write(str(output) + "\n")
        os.replace(temporary, filename)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
