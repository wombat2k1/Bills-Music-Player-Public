"""Low-priority, cancellable waveform peak generation worker.

Only the current track is ever analysed: a new ``request(path)`` call
overwrites any pending request, and an in-flight decode is checked for
staleness between blocks (see ``billsmusic.waveform.decode_peaks``), so a
rapid track skip aborts the superseded decode within about one block's
worth of work rather than running to completion.
"""
from __future__ import annotations

import time
from typing import Optional

from PyQt6 import QtCore

from .native_analysis_guard import native_analysis_slot
from .performance_diagnostics import WORKER_EXECUTION_WARNING_MS, get_diagnostics
from .waveform import WaveformData, decode_peaks, load_cached_waveform, save_waveform


class WaveformWorker(QtCore.QThread):
    waveform_ready = QtCore.pyqtSignal(str, object)     # (path, WaveformData)
    waveform_unavailable = QtCore.pyqtSignal(str)        # (path) -- decode failed/corrupt/unsupported

    def __init__(self):
        super().__init__()
        self._lock = QtCore.QMutex()
        self._condition = QtCore.QWaitCondition()
        self._pending_path: Optional[str] = None
        self._running = True
        self._diagnostics = get_diagnostics()

    def request(self, path: str):
        with QtCore.QMutexLocker(self._lock):
            self._pending_path = path
            self._condition.wakeAll()

    def stop(self):
        self._running = False
        with QtCore.QMutexLocker(self._lock):
            self._condition.wakeAll()

    def _is_stale(self, path: str) -> bool:
        if not self._running:
            return True
        with QtCore.QMutexLocker(self._lock):
            return self._pending_path not in (None, path)

    def run(self):
        while self._running:
            with QtCore.QMutexLocker(self._lock):
                while self._running and self._pending_path is None:
                    self._condition.wait(self._lock, 500)
                if not self._running:
                    return
                path = self._pending_path
                self._pending_path = None

            self._process(path)
            self.msleep(50)

    def _process(self, path: str):
        cached = load_cached_waveform(path)
        if cached is not None:
            self._diagnostics.record(
                "waveform", "cache_hit",
                details=self._diagnostics.path_details(path),
                minimum_level="basic",
            )
            if not self._is_stale(path):
                self.waveform_ready.emit(path, cached)
            return

        self._diagnostics.record(
            "waveform", "cache_miss",
            details=self._diagnostics.path_details(path),
            minimum_level="basic",
        )

        token = self._diagnostics.submit_worker(
            "waveform", "generate_waveform",
            details=self._diagnostics.path_details(path),
        )
        with self._diagnostics.run_worker(token):
            with native_analysis_slot(
                lambda: self._running and not self._is_stale(path)
            ) as acquired:
                result = self._decode(path) if acquired else None

        if self._is_stale(path):
            self._diagnostics.record(
                "waveform", "generate_cancelled",
                details=self._diagnostics.path_details(path),
                minimum_level="basic",
            )
            return
        if result is None:
            self.waveform_unavailable.emit(path)
            return
        save_waveform(path, result)
        self.waveform_ready.emit(path, result)

    def _decode(self, path: str) -> Optional[WaveformData]:
        # Timed manually rather than via diagnostics.measure(): that helper
        # unconditionally judges duration against the GUI-thread severity
        # thresholds (30ms/100ms), which flagged every real decode -- a
        # background-thread operation legitimately taking hundreds of ms to
        # a few seconds -- as a false "severe" anomaly.
        started = time.perf_counter()
        result = decode_peaks(path, should_cancel=lambda: self._is_stale(path))
        duration_ms = (time.perf_counter() - started) * 1000.0
        cancelled = result is None and self._is_stale(path)
        unavailable = result is None and not cancelled
        self._diagnostics.record(
            "waveform", "decode_and_peak_generation",
            status=(
                "cancelled" if cancelled else
                "unavailable" if unavailable else
                "success"
            ),
            severity=(
                "warning"
                if unavailable or duration_ms >= WORKER_EXECUTION_WARNING_MS
                else "info"
            ),
            duration_ms=duration_ms,
            details=self._diagnostics.path_details(path),
            minimum_level="basic",
        )
        return result
