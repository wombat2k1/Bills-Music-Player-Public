"""Small miniaudio playback backend used by the built-in player option."""
import array
import time
from typing import Optional

import miniaudio


class PreparedMiniaudioSource:
    """Phase C1 (native audio backend ownership, 2026-09-10): the
    miniaudio counterpart to bass_player.PreparedBassStream, but
    deliberately much lighter -- per design, preparation for miniaudio
    gathers only immutable source metadata (miniaudio.get_file_info(),
    no native device/stream construction) off the GUI thread; the real
    stream/device object is still built synchronously on commit, under
    MiniaudioPlayer's existing GUI-owned lifecycle, unchanged. There is
    therefore no native resource for this class to own or free -- its
    only job is the same exactly-once-resolution discipline
    PreparedBassStream has, so every call site can treat both prepared
    types the same way (always exactly one of commit/discard)."""
    __slots__ = ("path", "sample_rate", "channels", "duration", "_resolved")

    def __init__(self, path: str, sample_rate: int, channels: int, duration: float):
        self.path = path
        self.sample_rate = sample_rate
        self.channels = channels
        self.duration = duration
        self._resolved = False

    @property
    def resolved(self) -> bool:
        return self._resolved

    def take(self) -> None:
        """Marks this source resolved, exactly once. Unlike
        PreparedBassStream.take(), there is no handle/resource to
        return -- the caller (MiniaudioPlayer.commit_prepared) reads
        path/sample_rate/channels/duration directly off this object
        afterward. Raises RuntimeError if already resolved."""
        if self._resolved:
            raise RuntimeError("PreparedMiniaudioSource.take() called on an already-resolved source")
        self._resolved = True

    def discard(self) -> bool:
        # No native resource was ever created off-thread -- nothing to
        # free. Kept as a real method (not a no-op call site) so window.py
        # never has to special-case "which prepared type is this" in its
        # commit/discard branches.
        self._resolved = True
        return True


class MiniaudioPlayer:
    """A tiny player facade matching the methods window.py already expects."""

    def __init__(self):
        self._path = ""
        self._device = None
        self._stream = None
        self._sample_rate = 44100
        self._channels = 2
        self._length = 0.0
        self._pos = 0.0
        self._started_at: Optional[float] = None
        self._paused = False
        self._volume = 1.0
        self._ended = False

    def load(self, path: str):
        self.stop()
        info = miniaudio.get_file_info(path)
        self._path = path
        self._sample_rate = int(info.sample_rate or 44100)
        self._channels = int(info.nchannels or 2)
        frames = int(getattr(info, "num_frames", 0) or 0)
        self._length = (frames / float(self._sample_rate)) if frames > 0 else 0.0
        self._pos = 0.0
        self._paused = False
        self._ended = False

    @staticmethod
    def prepare_source(path: str) -> PreparedMiniaudioSource:
        """Phase C1: off-GUI-thread-safe counterpart to load(). Gathers
        only immutable metadata via miniaudio.get_file_info(path) --
        no MiniaudioPlayer instance is touched, no device/stream is
        constructed. Safe to call from a background candidate-
        preparation worker; the caller commits the result to a specific
        MiniaudioPlayer via commit_prepared() below, or discards it."""
        info = miniaudio.get_file_info(path)
        sample_rate = int(info.sample_rate or 44100)
        frames = int(getattr(info, "num_frames", 0) or 0)
        duration = (frames / float(sample_rate)) if frames > 0 else 0.0
        return PreparedMiniaudioSource(path, sample_rate, int(info.nchannels or 2), duration)

    def commit_prepared(self, candidate: PreparedMiniaudioSource) -> bool:
        """GUI-thread only. Adopts a still-valid candidate's metadata.
        Does not itself start playback -- callers call .play() afterward
        exactly as they do today after load(). Returns False (untouched)
        if the candidate was already resolved by someone else."""
        if candidate.resolved:
            return False
        self.stop()
        candidate.take()
        self._path = candidate.path
        self._sample_rate = candidate.sample_rate
        self._channels = candidate.channels
        self._length = candidate.duration
        self._pos = 0.0
        self._paused = False
        self._ended = False
        return True

    def play(self):
        if not self._path:
            return
        self._start_from(self._pos)

    def pause(self):
        if self._paused:
            return
        self._pos = self.get_pos() or self._pos
        self._stop_device()
        self._paused = True

    def resume(self):
        if not self._paused:
            return
        self._paused = False
        self._start_from(self._pos)

    def stop(self):
        self._stop_device()
        self._pos = 0.0
        self._paused = False

    def close(self):
        self.stop()
        self._path = ""


    def seek(self, seconds: float):
        self._pos = max(0.0, min(float(seconds or 0.0), self._length or float("inf")))
        was_playing = self.is_playing()
        self._stop_device()
        if was_playing and not self._paused:
            self._start_from(self._pos)

    def set_volume(self, value: float):
        self._volume = max(0.0, min(1.0, float(value)))
        # The stream wrapper reads this value for every audio buffer.

    def get_pos(self):
        if self._started_at is None:
            return self._pos
        pos = self._pos + (time.monotonic() - self._started_at)
        return min(pos, self._length) if self._length > 0 else pos

    def get_length(self):
        return self._length

    def stats(self):
        return {
            "path": self._path,
            "duration": self._length,
            "sample_rate": self._sample_rate,
            "channels": self._channels,
            "position": self.get_pos(),
            "ended": self._ended,
        }

    def is_playing(self):
        return self._device is not None and self._started_at is not None and not self._paused

    def _start_from(self, seconds: float):
        self._stop_device()
        self._ended = False
        seek_frame = max(0, int(seconds * self._sample_rate))
        source = miniaudio.stream_file(
            self._path,
            output_format=miniaudio.SampleFormat.FLOAT32,
            nchannels=self._channels,
            sample_rate=self._sample_rate,
            seek_frame=seek_frame,
            frames_to_read=512,
        )
        self._stream = self._volume_stream(source)
        next(self._stream)
        self._device = miniaudio.PlaybackDevice(
            output_format=miniaudio.SampleFormat.FLOAT32,
            nchannels=self._channels,
            sample_rate=self._sample_rate,
            buffersize_msec=40,
        )
        self._device.start(self._stream)
        self._pos = max(0.0, float(seconds))
        self._started_at = time.monotonic()
        self._paused = False

    def _volume_stream(self, source):
        requested = yield b""
        silence = array.array("f")
        while True:
            try:
                chunk = source.send(requested)
            except StopIteration:
                self._ended = True
                frames = int(requested or 0)
                silence = array.array("f", [0.0] * max(0, frames * self._channels))
                requested = yield silence
                continue
            volume = self._volume
            if volume < 0.999:
                chunk = array.array(chunk.typecode, (sample * volume for sample in chunk))
            requested = yield chunk

    def _stop_device(self):
        device = self._device
        stream = self._stream
        self._device = None
        self._stream = None
        self._started_at = None
        if device is not None:
            try:
                device.stop()
            except Exception:
                pass
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
