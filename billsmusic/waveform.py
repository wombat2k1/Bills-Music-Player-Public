"""Bounded-memory waveform peak generation and on-disk peak cache.

Decodes audio in fixed-size blocks and folds each block into a small,
fixed-size array of min/max peak pairs -- the full decoded track is never
held in memory. Two decode paths are tried in order:

  1. ``soundfile`` (libsndfile) -- handles wav/flac/mp3/ogg/aiff.
  2. ``audioread`` -- fallback for formats libsndfile can't open
     (aac/m4a/wma/opus/alac/mka/mp4/m4b/webm/amr).

Both paths are dependency-guarded and fail closed: missing libraries,
corrupt files, or a cancellation request all simply yield ``None`` rather
than raising, so a caller (the waveform worker) never needs to guard
against this module crashing a background thread.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from typing import Callable, List, Optional

from .library_cache import audio_fingerprint_matches

try:
    import numpy as np
except Exception:
    np = None

try:
    import soundfile as sf
except Exception:
    sf = None

try:
    import audioread
except Exception:
    audioread = None

WAVEFORM_SCHEMA_VERSION = 1
DEFAULT_PEAK_COUNT = 1500          # within the requested 1000-2000 range
DECODE_BLOCK_FRAMES = 65536        # ~1.5s @ 44.1kHz per soundfile block read


@dataclass
class WaveformData:
    schema_version: int
    peak_count: int
    duration_s: float
    channels: int
    mins: List[float]     # len == peak_count, values in [-1.0, 1.0]
    maxs: List[float]     # len == peak_count, values in [-1.0, 1.0]


# -- cache key / paths -------------------------------------------------------

def cache_key(path: str, size: int, mtime_ns: int) -> str:
    normalized = os.path.normcase(os.path.abspath(path))
    raw = f"{normalized}::{size}::{mtime_ns}::v{WAVEFORM_SCHEMA_VERSION}".encode("utf-8", "ignore")
    return hashlib.sha1(raw).hexdigest()


def cache_file_path(path: str, size: int, mtime_ns: int) -> str:
    from .config import waveform_cache_dir
    return os.path.join(waveform_cache_dir(), f"{cache_key(path, size, mtime_ns)}.json")


# -- disk cache read/write ---------------------------------------------------

def load_cached_waveform(path: str) -> Optional[WaveformData]:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    cache_path = cache_file_path(path, stat.st_size, stat.st_mtime_ns)
    try:
        with open(cache_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return None
    if raw.get("schema_version") != WAVEFORM_SCHEMA_VERSION:
        return None
    current = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    stored = {"size": raw.get("size"), "mtime_ns": raw.get("mtime_ns")}
    if not audio_fingerprint_matches(stored, current):
        return None
    mins, maxs = raw.get("mins"), raw.get("maxs")
    if not isinstance(mins, list) or not isinstance(maxs, list):
        return None
    if len(mins) != len(maxs) or not mins:
        return None
    try:
        return WaveformData(
            schema_version=WAVEFORM_SCHEMA_VERSION,
            peak_count=len(mins),
            duration_s=float(raw.get("duration_s", 0.0)),
            channels=int(raw.get("channels", 1)),
            mins=[float(v) for v in mins],
            maxs=[float(v) for v in maxs],
        )
    except (TypeError, ValueError):
        return None


def save_waveform(path: str, data: WaveformData) -> bool:
    try:
        stat = os.stat(path)
    except OSError:
        return False
    cache_path = cache_file_path(path, stat.st_size, stat.st_mtime_ns)
    folder = os.path.dirname(cache_path)
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:
        return False
    payload = {
        "schema_version": WAVEFORM_SCHEMA_VERSION,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "duration_s": data.duration_s,
        "channels": data.channels,
        "mins": data.mins,
        "maxs": data.maxs,
    }
    try:
        fd, temporary = tempfile.mkstemp(prefix="waveform-", suffix=".json", dir=folder)
    except Exception:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        os.replace(temporary, cache_path)
        return True
    except Exception:
        return False
    finally:
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except Exception:
            pass


# -- bounded-block decode -----------------------------------------------------

class _BucketAggregator:
    """Folds an unbounded stream of mono samples into a fixed-size min/max array."""

    def __init__(self, target_points: int, frames_per_bucket: int):
        self.target_points = target_points
        self.frames_per_bucket = max(1, frames_per_bucket)
        self.mins = [1.0] * target_points
        self.maxs = [-1.0] * target_points
        self._bucket_idx = 0
        self._frames_in_bucket = 0
        self._bucket_min = 1.0
        self._bucket_max = -1.0
        self.frames_seen = 0

    def add(self, mono_block) -> None:
        offset = 0
        n = mono_block.shape[0]
        while offset < n:
            if self._bucket_idx >= self.target_points:
                # Track ran longer than expected; fold any remainder into the
                # last bucket rather than growing the arrays.
                chunk = mono_block[offset:]
                if chunk.size:
                    idx = self.target_points - 1
                    self.mins[idx] = min(self.mins[idx], float(chunk.min()))
                    self.maxs[idx] = max(self.maxs[idx], float(chunk.max()))
                self.frames_seen += n - offset
                return
            remaining_in_bucket = self.frames_per_bucket - self._frames_in_bucket
            take = min(remaining_in_bucket, n - offset)
            chunk = mono_block[offset:offset + take]
            if chunk.size:
                self._bucket_min = min(self._bucket_min, float(chunk.min()))
                self._bucket_max = max(self._bucket_max, float(chunk.max()))
            self._frames_in_bucket += take
            offset += take
            self.frames_seen += take
            if self._frames_in_bucket >= self.frames_per_bucket:
                self._flush_bucket()

    def _flush_bucket(self) -> None:
        self.mins[self._bucket_idx] = self._bucket_min
        self.maxs[self._bucket_idx] = self._bucket_max
        self._bucket_idx += 1
        self._frames_in_bucket = 0
        self._bucket_min = 1.0
        self._bucket_max = -1.0

    def finish(self) -> None:
        if self._frames_in_bucket > 0 and self._bucket_idx < self.target_points:
            self._flush_bucket()
        # Any buckets past the last real sample stay at the neutral fill
        # (min=1.0, max=-1.0 means "empty"); normalize them to 0 so a short
        # track doesn't paint stray full-height bars at the tail.
        for i in range(self._bucket_idx, self.target_points):
            self.mins[i] = 0.0
            self.maxs[i] = 0.0


def _decode_peaks_soundfile(
    path: str, target_points: int, should_cancel: Callable[[], bool],
) -> Optional[WaveformData]:
    if sf is None or np is None:
        return None
    try:
        with sf.SoundFile(path) as handle:
            total_frames = len(handle)
            channels = handle.channels
            samplerate = handle.samplerate
            if total_frames <= 0 or samplerate <= 0:
                return None
            duration_s = total_frames / float(samplerate)
            frames_per_bucket = max(1, total_frames // target_points)
            aggregator = _BucketAggregator(target_points, frames_per_bucket)
            while True:
                if should_cancel():
                    return None
                block = handle.read(DECODE_BLOCK_FRAMES, dtype="float32", always_2d=True)
                if block.shape[0] == 0:
                    break
                mono_block = block.mean(axis=1) if block.shape[1] > 1 else block[:, 0]
                aggregator.add(mono_block)
            if should_cancel():
                return None
            aggregator.finish()
            return WaveformData(
                schema_version=WAVEFORM_SCHEMA_VERSION,
                peak_count=target_points,
                duration_s=duration_s,
                channels=channels,
                mins=aggregator.mins,
                maxs=aggregator.maxs,
            )
    except Exception:
        return None


def _decode_peaks_audioread(
    path: str, target_points: int, should_cancel: Callable[[], bool],
) -> Optional[WaveformData]:
    if audioread is None or np is None:
        return None
    try:
        with audioread.audio_open(path) as input_file:
            channels = input_file.channels or 1
            samplerate = input_file.samplerate or 44100
            duration_s = float(getattr(input_file, "duration", 0.0) or 0.0)
            total_frames = int(duration_s * samplerate) if duration_s > 0 else 0
            frames_per_bucket = max(1, total_frames // target_points) if total_frames else DECODE_BLOCK_FRAMES
            aggregator = _BucketAggregator(target_points, frames_per_bucket)
            for raw_block in input_file:
                if should_cancel():
                    return None
                if not raw_block:
                    continue
                samples = np.frombuffer(raw_block, dtype="<i2")
                if samples.size == 0:
                    continue
                if channels > 1:
                    usable = (samples.size // channels) * channels
                    if usable == 0:
                        continue
                    samples = samples[:usable].reshape(-1, channels).mean(axis=1)
                mono_block = samples.astype("float32") / 32768.0
                aggregator.add(mono_block)
            if should_cancel():
                return None
            aggregator.finish()
            if duration_s <= 0:
                duration_s = aggregator.frames_seen / float(samplerate)
            return WaveformData(
                schema_version=WAVEFORM_SCHEMA_VERSION,
                peak_count=target_points,
                duration_s=duration_s,
                channels=channels,
                mins=aggregator.mins,
                maxs=aggregator.maxs,
            )
    except Exception:
        return None


def decode_peaks(
    path: str,
    *,
    target_points: int = DEFAULT_PEAK_COUNT,
    should_cancel: Callable[[], bool] = lambda: False,
) -> Optional[WaveformData]:
    """Decode ``path`` into a bounded-size peak envelope.

    Returns ``None`` for missing/corrupt/unsupported/cancelled input -- never
    raises. Reads audio in bounded blocks; the full decoded track is never
    held in memory.
    """
    if not path or not os.path.isfile(path):
        return None
    if target_points <= 0:
        return None
    if should_cancel():
        return None
    result = _decode_peaks_soundfile(path, target_points, should_cancel)
    if result is None and not should_cancel():
        result = _decode_peaks_audioread(path, target_points, should_cancel)
    return result
