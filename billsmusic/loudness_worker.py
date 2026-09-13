"""Low-priority, cancellable loudness analysis worker."""
from __future__ import annotations

from PyQt6 import QtCore

from .media_capabilities import is_replaygain_eligible
from .native_analysis_guard import native_analysis_slot

try:
    import numpy as np
    import soundfile as sf
except Exception:
    np = sf = None


class LoudnessAnalysisWorker(QtCore.QThread):
    result_ready = QtCore.pyqtSignal(str, dict)

    def __init__(self):
        super().__init__()
        self._queue = []
        self._queued = set()
        self._running = True
        self._lock = QtCore.QMutex()
        self._condition = QtCore.QWaitCondition()

    def request(self, path: str, force: bool = False):
        if not is_replaygain_eligible(path):
            return
        with QtCore.QMutexLocker(self._lock):
            if path in self._queued:
                return
            if force:
                self._queue.insert(0, path)
            else:
                self._queue.append(path)
            self._queued.add(path)
            self._condition.wakeAll()

    def stop(self):
        self._running = False
        with QtCore.QMutexLocker(self._lock):
            self._condition.wakeAll()

    def run(self):
        while self._running:
            with QtCore.QMutexLocker(self._lock):
                while self._running and not self._queue:
                    self._condition.wait(self._lock, 500)
                if not self._running:
                    return
                path = self._queue.pop(0)
                self._queued.discard(path)
            with native_analysis_slot(lambda: self._running) as acquired:
                result = self.analyse(path) if acquired else {}
            if result and self._running:
                self.result_ready.emit(path, result)
            self.msleep(100)

    @staticmethod
    def analyse(path: str):
        if np is None or sf is None:
            return {}
        try:
            data, _ = sf.read(path, dtype="float32", always_2d=True)
            if data.size == 0:
                return {}
            mono = data.mean(axis=1, dtype="float64")
            # Conservative integrated estimate. This is intentionally dependency-free;
            # embedded ReplayGain remains authoritative when available.
            rms = float(np.sqrt(np.mean(np.square(mono), dtype="float64")))
            peak = float(np.max(np.abs(data)))
            if rms <= 0 or peak <= 0:
                return {}
            lufs = -0.691 + 20.0 * float(np.log10(rms))
            return {"lufs": max(-70.0, min(0.0, lufs)), "true_peak": peak}
        except Exception:
            return {}
