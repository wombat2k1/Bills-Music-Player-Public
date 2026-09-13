"""ReplayGain parsing, safe gain calculation, and the player loudness cache."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import os
import re
import tempfile
from typing import Any, Dict, Mapping, Optional

try:
    from mutagen import File as MutagenFile
except Exception:
    MutagenFile = None

VALID_MODES = {"default", "track", "album", "off"}
_GAIN_RE = re.compile(r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(?:db)?\s*$", re.I)
_PEAK_RE = re.compile(r"^\s*(\d+(?:\.\d*)?|\.\d+)\s*$")


def _first(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return getattr(value, "text", value)


def parse_gain(value: Any) -> Optional[float]:
    value = _first(value)
    match = _GAIN_RE.match(str(value or ""))
    if not match:
        return None
    number = float(match.group(1))
    return number if math.isfinite(number) and -60.0 <= number <= 60.0 else None


def parse_peak(value: Any) -> Optional[float]:
    value = _first(value)
    match = _PEAK_RE.match(str(value or ""))
    if not match:
        return None
    number = float(match.group(1))
    return number if math.isfinite(number) and 0.0 < number <= 16.0 else None


def _tag_map(tags: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    if not tags:
        return result
    try:
        items = tags.items()
    except Exception:
        return result
    for key, value in items:
        normalized = str(key).upper()
        result[normalized] = value
        # ID3 TXXX keys are commonly exposed as TXXX:REPLAYGAIN_TRACK_GAIN.
        if ":" in normalized:
            result.setdefault(normalized.rsplit(":", 1)[-1], value)
    return result


def read_replaygain(path: str) -> Dict[str, Optional[float]]:
    empty = {"track_gain": None, "track_peak": None, "album_gain": None, "album_peak": None}
    try:
        if MutagenFile is None:
            return empty
        audio = MutagenFile(path, easy=False)
        tags = _tag_map(getattr(audio, "tags", None))
        return {
            "track_gain": parse_gain(tags.get("REPLAYGAIN_TRACK_GAIN")),
            "track_peak": parse_peak(tags.get("REPLAYGAIN_TRACK_PEAK")),
            "album_gain": parse_gain(tags.get("REPLAYGAIN_ALBUM_GAIN")),
            "album_peak": parse_peak(tags.get("REPLAYGAIN_ALBUM_PEAK")),
        }
    except Exception:
        return empty


@dataclass(frozen=True)
class GainResult:
    mode: str
    source: str
    requested_db: float
    applied_db: float
    linear_gain: float
    clipping_reduced: bool
    pending_analysis: bool = False


def effective_mode(enabled: bool, global_mode: str, override: str = "default") -> str:
    override = override if override in VALID_MODES else "default"
    if override == "off":
        return "off"
    if override in ("track", "album"):
        return override
    if not enabled:
        return "off"
    return global_mode if global_mode in ("track", "album") else "track"


def calculate_gain(
    mode: str,
    replaygain: Mapping[str, Any],
    measured: Optional[Mapping[str, Any]] = None,
    *,
    target_lufs: float = -14.0,
    tagged_preamp_db: float = 0.0,
    untagged_preamp_db: float = 0.0,
    prevent_clipping: bool = True,
) -> GainResult:
    if mode == "off":
        return GainResult(mode, "fallback", 0.0, 0.0, 1.0, False)
    measured = measured or {}
    gain = peak = None
    source = "fallback"
    if mode == "album":
        gain, peak = replaygain.get("album_gain"), replaygain.get("album_peak")
    if gain is None:
        gain, peak = replaygain.get("track_gain"), replaygain.get("track_peak")
    if gain is not None:
        requested = float(gain) + float(tagged_preamp_db)
        source = "embedded tag"
    elif measured.get("lufs") is not None:
        requested = float(target_lufs) - float(measured["lufs"]) + float(untagged_preamp_db)
        peak = measured.get("true_peak")
        source = "player analysis cache"
    else:
        return GainResult(mode, source, 0.0, 0.0, 1.0, False, True)
    applied = requested
    if prevent_clipping and peak is not None and float(peak) > 0:
        max_db = -20.0 * math.log10(float(peak))
        applied = min(applied, max_db)
    applied = max(-60.0, min(24.0, applied))
    return GainResult(mode, source, requested, applied, 10.0 ** (applied / 20.0), applied < requested - 1e-9)


def combine_volume(user_volume: float, normalisation_gain: float, fade: float = 1.0) -> float:
    return max(0.0, min(1.0, float(user_volume) * float(normalisation_gain) * float(fade)))


class LoudnessCache:
    """Small JSON cache keyed by normalised absolute path; never touches audio files."""
    def __init__(self, path: str):
        self.path = path
        self.overrides: Dict[str, str] = {}
        self.analysis: Dict[str, Dict[str, Any]] = {}
        self.load()

    @staticmethod
    def key(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self.overrides = dict(data.get("overrides") or {})
            self.analysis = dict(data.get("analysis") or {})
        except Exception:
            self.overrides, self.analysis = {}, {}

    def save(self):
        folder = os.path.dirname(self.path) or "."
        os.makedirs(folder, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="loudness-", suffix=".json", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "overrides": self.overrides, "analysis": self.analysis}, handle, indent=2)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def override_for(self, path: str) -> str:
        return self.overrides.get(self.key(path), "default")

    def set_override(self, path: str, mode: str):
        key = self.key(path)
        if mode == "default":
            self.overrides.pop(key, None)
        elif mode in VALID_MODES:
            self.overrides[key] = mode
        self.save()

    def analysis_for(self, path: str) -> Optional[Dict[str, Any]]:
        item = self.analysis.get(self.key(path))
        if not item or not os.path.isfile(path):
            return None
        try:
            stat = os.stat(path)
            if item.get("size") != stat.st_size or item.get("mtime_ns") != stat.st_mtime_ns:
                return None
        except OSError:
            return None
        return item

    def store_analysis(self, path: str, lufs: float, true_peak: float):
        stat = os.stat(path)
        self.analysis[self.key(path)] = {
            "lufs": float(lufs), "true_peak": float(true_peak),
            "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
        }
        self.save()

    def remove_analysis(self, path: str):
        self.analysis.pop(self.key(path), None)
        self.save()
