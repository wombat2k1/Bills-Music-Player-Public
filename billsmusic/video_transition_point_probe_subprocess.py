"""Isolated, disposable child process for Smart Video Transition Points
(see video_transition_point_analyzer.py's module docstring for the full
rationale). Launched fresh per (path, kind) analysis via
``_probe_subprocess_launch_command``, mirroring video_backend.py's
``GpuCompositorProbe``/video_subprocess.py's isolation policy: this is a
new, never-before-exercised use of Python-side ``QVideoFrame`` pixel
access in this codebase (the production GPU compositor deliberately never
does this -- see GpuDualDeckVideoSubprocessController's own docstring), so
it stays fenced off in its own throwaway process rather than running in
the main app's threads, exactly like every other Qt Multimedia decode in
this app.

Prints exactly one JSON line to stdout and exits. Any decode failure,
timeout, unsupported media, or unexpected exception anywhere in this
module results in a ``{"result": "no_trim", ...}`` line -- never a
non-zero exit or an unhandled traceback reaching the parent. This is the
single most important behavioural rule here: analysis is an enhancement,
never a playback dependency.
"""
from __future__ import annotations

import json
import math
import os
import struct
import sys
import traceback
from typing import List, Optional, Tuple

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtGui, QtMultimedia, QtWidgets

from .video_transition_point_analyzer import (
    BoundarySample,
    MAX_TRIM_DURATION_MS,
    MIN_TRIM_DURATION_MS,
    _classify_visible_boundary,
    detect_content_rect,
    frame_metrics_within_rect,
)

# Bounded analysis windows -- see module docstring: "do not analyse full
# videos." Chosen to comfortably cover the max trim ceiling
# (MAX_TRIM_DURATION_MS, 3000ms) with margin either side.
INTRO_WINDOW_MS = 4000
OUTRO_WINDOW_MS = 4000
SAMPLE_INTERVAL_MS = 200

# Frames are downsampled to roughly this many samples per the longer side
# before any luminance/dark-pixel computation -- only enough detail to
# distinguish "black" from "meaningfully visible," not full-resolution
# pixel-by-pixel analysis (explicit requirement).
DOWNSAMPLE_TARGET_PX = 32

# Calibrated this session against real files (ABBA - Super Trouper: a
# genuine black+near-silent intro measured RMS 0.0 through ~1s; ABBA -
# Money, Money, Money: a *visually* dim-but-not-black intro, correctly
# excluded by the luminance gate before audio is even considered, whose
# audio also happens to open near-silent -- confirming the audio check
# alone cannot safely discriminate content and must only ever run as a
# secondary gate on top of an already-confirmed visual black run, over
# that same bounded window, not as a standalone signal).
SILENCE_RMS_THRESHOLD = 0.01

_SAMPLING_SAFETY_MARGIN_MS = 3000
_DURATION_TIMEOUT_MS = 6000
_OVERALL_WATCHDOG_MS = 20000


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _fail_safe(reason: str) -> dict:
    return {"result": "no_trim", "confidence": 0.0, "duration_ms": 0, "reason": reason}


def _image_to_luminance_grid(image: QtGui.QImage) -> List[List[float]]:
    if image.isNull():
        return []
    img = image.convertToFormat(QtGui.QImage.Format.Format_RGB32)
    w, h = img.width(), img.height()
    if w <= 0 or h <= 0:
        return []
    if w >= h:
        new_w = DOWNSAMPLE_TARGET_PX
        new_h = max(1, round(h * DOWNSAMPLE_TARGET_PX / w))
    else:
        new_h = DOWNSAMPLE_TARGET_PX
        new_w = max(1, round(w * DOWNSAMPLE_TARGET_PX / h))
    small = img.scaled(
        new_w, new_h,
        QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
        QtCore.Qt.TransformationMode.FastTransformation,
    )
    grid: List[List[float]] = []
    for y in range(small.height()):
        row = []
        for x in range(small.width()):
            px = small.pixelColor(x, y)
            row.append(0.299 * px.red() + 0.587 * px.green() + 0.114 * px.blue())
        grid.append(row)
    return grid


def _sample_video(
    path: str, window_fn,
) -> Tuple[int, List[BoundarySample]]:
    """Returns (duration_ms, samples). Empty samples / duration_ms==0 on
    any failure -- caller treats that as "no trim" uniformly.
    ``window_fn(duration_ms) -> (window_start_ms, window_end_ms)`` lets the
    caller anchor an outro window against the real duration, which is only
    known after this function has already opened the file -- avoids
    opening/decoding the same file twice just to learn its length first.

    Samples by seeking once to the window start and then letting the
    player run continuously through the window, capturing a frame roughly
    every SAMPLE_INTERVAL_MS of *playback position* as frames arrive
    naturally -- not by seeking to each sample point individually. Real
    profiling this session against 4K VP9 content found repeated seeking
    costs roughly 1.3s/seek (each seek requires decoding forward from the
    nearest keyframe), making a ~20-sample window take ~25+ seconds;
    continuous playback covers the same 4-second window in ~4 seconds
    (bounded by the window itself, not by seek/keyframe cost) since no
    frame is ever decoded twice."""
    player = QtMultimedia.QMediaPlayer()
    sink = QtMultimedia.QVideoSink()
    player.setVideoSink(sink)
    player.setSource(QtCore.QUrl.fromLocalFile(path))

    duration_holder = {"ms": 0}
    duration_loop = QtCore.QEventLoop()

    def on_duration(value: int) -> None:
        if value > 0:
            duration_holder["ms"] = value
            duration_loop.quit()

    conn = player.durationChanged.connect(on_duration)
    QtCore.QTimer.singleShot(_DURATION_TIMEOUT_MS, duration_loop.quit)
    duration_loop.exec()
    player.durationChanged.disconnect(conn)
    duration_ms = duration_holder["ms"] or player.duration()
    if duration_ms <= 0:
        player.setSource(QtCore.QUrl())
        return 0, []
    window_start_ms, window_end_ms = window_fn(duration_ms)

    content_rect: Optional[Tuple[int, int, int, int]] = None
    samples: List[BoundarySample] = []
    last_sample_ms = {"value": window_start_ms - SAMPLE_INTERVAL_MS}

    def on_frame(frame) -> None:
        nonlocal content_rect
        if not frame.isValid():
            return
        position_ms = player.position()
        if position_ms - last_sample_ms["value"] < SAMPLE_INTERVAL_MS:
            return
        last_sample_ms["value"] = position_ms
        grid = _image_to_luminance_grid(frame.toImage())
        if not grid:
            return
        if content_rect is None:
            content_rect = detect_content_rect(grid)
        avg_luminance, dark_fraction = frame_metrics_within_rect(grid, content_rect)
        samples.append(BoundarySample(offset_ms=position_ms, avg_luminance=avg_luminance, dark_pixel_fraction=dark_fraction))

    frame_conn = sink.videoFrameChanged.connect(on_frame)
    player.setPosition(max(0, min(duration_ms, window_start_ms)))
    player.play()

    run_loop = QtCore.QEventLoop()
    poll_timer = QtCore.QTimer()

    def check_done() -> None:
        if player.position() >= window_end_ms - 30:
            run_loop.quit()

    poll_timer.timeout.connect(check_done)
    poll_timer.start(25)
    # Generous margin on top of the window's own real-time duration, in
    # case playback runs slower than real-time under decode load -- not a
    # per-sample timeout (continuous playback has no per-sample seeking to
    # time out on).
    safety_timeout_ms = max(0, window_end_ms - window_start_ms) + _SAMPLING_SAFETY_MARGIN_MS
    QtCore.QTimer.singleShot(safety_timeout_ms, run_loop.quit)
    run_loop.exec()
    poll_timer.stop()
    sink.videoFrameChanged.disconnect(frame_conn)

    player.pause()
    player.stop()
    player.setSource(QtCore.QUrl())
    return duration_ms, samples


def _measure_silence(path: str, window_ms: int) -> Optional[bool]:
    """Bounded RMS check over [0, window_ms] of the decoded audio stream,
    via QAudioBufferOutput on a fresh QMediaPlayer -- same Qt Multimedia
    decode pipeline already used for video, no second native decode
    library. Returns True (confidently silent), False (audible), or None
    (inconclusive/failed -- caller must treat this exactly like False for
    the purpose of gating a skip, i.e. never skip on an inconclusive
    result)."""
    if window_ms <= 0:
        return None
    try:
        player = QtMultimedia.QMediaPlayer()
        audio_output = QtMultimedia.QAudioOutput()
        audio_output.setMuted(True)
        player.setAudioOutput(audio_output)
        buffer_output = QtMultimedia.QAudioBufferOutput()
        player.setAudioBufferOutput(buffer_output)

        sample_format = QtMultimedia.QAudioFormat.SampleFormat
        stats = {"total_sq": 0.0, "total_n": 0, "buffers": 0}

        def on_buffer(buf) -> None:
            start_us = buf.startTime()
            start_ms = start_us / 1000.0 if start_us >= 0 else 0.0
            if start_ms > window_ms:
                return
            stats["buffers"] += 1
            data_ptr = buf.constData()
            size = buf.byteCount()
            if size <= 0:
                return
            data_ptr.setsize(size)
            raw = bytes(data_ptr.asstring(size))
            if not raw:
                return
            fmt = buf.format()
            sf = fmt.sampleFormat()
            if sf == sample_format.Float:
                count = len(raw) // 4
                if count == 0:
                    return
                values = struct.unpack(f"<{count}f", raw[:count * 4])
            elif sf == sample_format.Int16:
                count = len(raw) // 2
                if count == 0:
                    return
                values = [s / 32768.0 for s in struct.unpack(f"<{count}h", raw[:count * 2])]
            elif sf == sample_format.Int32:
                count = len(raw) // 4
                if count == 0:
                    return
                values = [s / 2147483648.0 for s in struct.unpack(f"<{count}i", raw[:count * 4])]
            elif sf == sample_format.UInt8:
                values = [(b - 128) / 128.0 for b in raw]
            else:
                return
            stats["total_sq"] += sum(v * v for v in values)
            stats["total_n"] += len(values)

        buffer_output.audioBufferReceived.connect(on_buffer)
        player.setSource(QtCore.QUrl.fromLocalFile(path))
        player.setPosition(0)
        player.play()

        loop = QtCore.QEventLoop()
        QtCore.QTimer.singleShot(max(1500, window_ms + 1500), loop.quit)
        loop.exec()
        player.pause()
        player.stop()
        player.setSource(QtCore.QUrl())

        if stats["total_n"] == 0:
            return None
        rms = math.sqrt(stats["total_sq"] / stats["total_n"])
        return rms < SILENCE_RMS_THRESHOLD
    except Exception:
        return None


def _run_outro(path: str) -> dict:
    duration_ms, samples = _sample_video(
        path, lambda duration: (max(0, duration - OUTRO_WINDOW_MS), duration),
    )
    if duration_ms <= 0 or not samples:
        return _fail_safe("no_samples")
    result = _classify_visible_boundary(samples, from_start=False, edge_ms=duration_ms)
    if result.trim_ms <= 0:
        return {"result": "no_trim", "confidence": result.confidence, "duration_ms": duration_ms}
    return {
        "result": "trim", "boundary_ms": result.boundary_ms, "trim_ms": result.trim_ms,
        "confidence": result.confidence, "duration_ms": duration_ms,
    }


def _run_intro(path: str) -> dict:
    duration_ms, samples = _sample_video(
        path, lambda duration: (0, min(INTRO_WINDOW_MS, duration)),
    )
    if duration_ms <= 0 or not samples:
        return _fail_safe("no_samples")
    result = _classify_visible_boundary(samples, from_start=True, edge_ms=0)
    if result.trim_ms <= 0:
        return {"result": "no_trim", "confidence": result.confidence, "duration_ms": duration_ms, "intro_silent": None}
    intro_silent = _measure_silence(path, result.boundary_ms)
    return {
        "result": "trim", "boundary_ms": result.boundary_ms, "trim_ms": result.trim_ms,
        "confidence": result.confidence, "duration_ms": duration_ms, "intro_silent": intro_silent,
    }


def main() -> int:
    try:
        argv = sys.argv
        path = None
        kind = None
        for i, arg in enumerate(argv):
            if arg == "--path" and i + 1 < len(argv):
                path = argv[i + 1]
            elif arg == "--kind" and i + 1 < len(argv):
                kind = argv[i + 1]
        if not path or not os.path.isfile(path) or kind not in ("intro", "outro"):
            _emit(_fail_safe("invalid_arguments"))
            return 0

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        watchdog = QtCore.QTimer()
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(lambda: (_emit(_fail_safe("overall_watchdog")), os._exit(0)))
        watchdog.start(_OVERALL_WATCHDOG_MS)

        payload = _run_outro(path) if kind == "outro" else _run_intro(path)
        _emit(payload)
        return 0
    except Exception as ex:  # noqa: BLE001 -- must never propagate a traceback
        try:
            _emit(_fail_safe(f"exception:{type(ex).__name__}"))
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
