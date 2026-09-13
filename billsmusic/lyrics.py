"""Synced-lyrics extraction (embedded tags + sidecar .lrc), pulled out of
PlayerWindow as plain functions so LyricsLoadWorker (workers.py) can call
them off the GUI thread without importing window.py (which already
imports workers.py -- importing back would be circular).

v1.0.67 MainThread I/O hardening: _load_lrc_for_track used to run this
exact logic -- MutagenFile(path) plus, on a miss, a sidecar file read --
synchronously on every AUDIO track activation. The video/karaoke branch
right next to it had already been fixed for the identical class of bug
(a real ~124ms NAS stall); this closes the same gap for lyrics.
"""
from __future__ import annotations

import os
import re
from typing import List, Tuple

from mutagen import File as MutagenFile

from .textfix import clean_text

_TIMESTAMP_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")

_SYNCED_LYRICS_TAG_KEYS = (
    "syncedlyrics", "synced lyrics", "lyrics-sync", "lyrics_sync",
    "SYNCEDLYRICS", "SYLT", "?lyr", "----:com.apple.iTunes:SYNCEDLYRICS",
)


def coerce_tag_text_values(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, bytes):
        return [value.decode("utf-8", errors="ignore")]
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            result.extend(coerce_tag_text_values(item))
        return result
    text_attr = getattr(value, "text", None)
    if text_attr is not None and text_attr is not value:
        return coerce_tag_text_values(text_attr)
    return [str(value)]


def tag_values_for_keys(tags, keys: Tuple[str, ...]) -> List[str]:
    if not tags:
        return []
    wanted = {str(key).casefold() for key in keys}
    values = []
    try:
        for key in keys:
            if hasattr(tags, "getall"):
                for value in tags.getall(key) or []:
                    values.extend(coerce_tag_text_values(value))
            try:
                value = tags.get(key)
            except Exception:
                value = None
            values.extend(coerce_tag_text_values(value))
        try:
            iterator = tags.items()
        except Exception:
            iterator = []
        for tag_key, value in iterator:
            if str(tag_key).casefold() in wanted:
                values.extend(coerce_tag_text_values(value))
    except Exception:
        pass
    clean_values = []
    for value in values:
        text = clean_text(str(value)).strip()
        if text and text != "Unknown":
            clean_values.append(text)
    return clean_values


def parse_lrc_text(text: str) -> List[Tuple[float, str]]:
    entries = []
    normalised = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    for raw in normalised.split("\n"):
        line = raw.strip()
        matches = list(_TIMESTAMP_RE.finditer(line))
        if not matches:
            continue
        lyric = clean_text(_TIMESTAMP_RE.sub("", line).strip())
        for m in matches:
            minutes = int(m.group(1))
            seconds = int(m.group(2))
            frac = m.group(3) or "0"
            frac_s = int(frac) / (10 ** len(frac))
            entries.append((minutes * 60 + seconds + frac_s, lyric))
    return entries


def extract_synced_lyrics_from_tags(path: str) -> List[Tuple[float, str]]:
    try:
        audio = MutagenFile(path)
        tags = getattr(audio, "tags", None) if audio else None
    except Exception:
        tags = None
    if not tags:
        return []

    # ID3 SYLT frames store true synced lyrics as text/time pairs.
    try:
        frames = tags.getall("SYLT") if hasattr(tags, "getall") else []
        for frame in frames:
            entries = []
            for item in getattr(frame, "text", []) or []:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    lyric, stamp = item[0], item[1]
                else:
                    continue
                try:
                    seconds = float(stamp) / 1000.0
                except Exception:
                    continue
                entries.append((seconds, str(lyric)))
            if entries:
                return entries
    except Exception:
        pass

    # Many FLAC/MP4/ID3 files use LRC text in tags such as syncedlyrics.
    for value in tag_values_for_keys(tags, _SYNCED_LYRICS_TAG_KEYS):
        entries = parse_lrc_text(value)
        if entries:
            return entries
    return []


def load_lyrics_for_track(path: str) -> Tuple[List[Tuple[float, str]], str]:
    """Full load: embedded tags first, sidecar .lrc file on a miss. Returns
    (entries, source) where source is "tags"/"sidecar"/"none" -- callers
    use it only for diagnostics, matching what _load_lrc_for_track already
    recorded before this was pulled out to run off-thread."""
    entries = extract_synced_lyrics_from_tags(path)
    if entries:
        return entries, "tags"

    lrc_path = os.path.splitext(path)[0] + ".lrc"
    if not os.path.isfile(lrc_path):
        return [], "none"

    try:
        with open(lrc_path, "r", encoding="utf-8-sig") as f:
            entries = parse_lrc_text(f.read())
    except Exception:
        return [], "none"

    return entries, ("sidecar" if entries else "none")
