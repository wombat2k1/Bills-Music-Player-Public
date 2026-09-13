"""Playback stability hardening, Phase C1 (native audio backend
ownership, 2026-09-10): real-bass.dll, real-thread integration tests for
the candidate prepare/commit/discard design.

Proven against the real vendored bass.dll (this suite's established
convention -- see test_bass_url_streaming.py/test_bass_fft_visualiser.py),
not mocked. Two levels, per the Phase C1 design review:

    K. plain threading.Thread calling BassPlayer.prepare_stream()
       directly, main thread commits -- proves the core cross-thread
       hazard the whole design exists to close is actually closed under
       real BASS semantics (not just Python-level bookkeeping), and
       that BASS's own device auto-selection for a thread with none
       selected works as the design assumed.
    L. the real production BassStreamPrepareWorker (a QThread), a real
       QApplication event loop, and the real `prepared` signal -- proves
       the candidate survives real queued signal delivery, the worker
       never held a player reference, and GUI-thread commit/play/FFT/
       seek/volume/stop all keep working afterward.
"""
import os
import threading
import time

import pytest

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
_DLL_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "vendor", "bass", "bin", "x64", "bass.dll"),
]

pytestmark = pytest.mark.skipif(
    not any(os.path.isfile(p) for p in _DLL_CANDIDATES),
    reason="bass.dll not present in this environment",
)


def _wait(predicate, timeout=5.0, tick=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(tick)
    return predicate()


def _wait_for_fft(player, timeout=3.0):
    # get_fft_levels() legitimately returns None until BASS has a full
    # FFT window decoded -- polled here rather than asserted on the very
    # first read, matching test_bass_fft_visualiser.py's own convention.
    deadline = time.monotonic() + timeout
    levels = player.get_fft_levels(32)
    while levels is None and time.monotonic() < deadline:
        time.sleep(0.05)
        levels = player.get_fft_levels(32)
    return levels


# ---------------------------------------------------------------------------
# K: plain threading.Thread
# ---------------------------------------------------------------------------

def test_plain_thread_prepare_then_main_thread_commit_and_play():
    from billsmusic.bass_player import BassPlayer, _BassEngine

    _BassEngine.ensure()  # simulates the real GUI-thread priming (Phase C1 design, section H)
    local_path = os.path.join(FIXTURES_DIR, "sample.mp3")

    box = {}

    def _worker():
        box["candidate"] = BassPlayer.prepare_stream(local_path)

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join(timeout=5.0)
    assert "candidate" in box, "background thread never produced a candidate"

    player = BassPlayer()
    try:
        assert player.commit_prepared(box["candidate"]) is True
        player.play()
        assert _wait(player.is_playing), "player never started playing after a cross-thread prepare"
        time.sleep(0.2)

        levels = _wait_for_fft(player)
        assert levels is not None, "FFT never became available for a cross-thread-prepared stream"
        assert len(levels) == 32

        # Ownership boundary (F): stop/seek/volume/FFT all keep working
        # normally on the committed player -- there is no longer any
        # worker thread that could race them.
        player.seek(0.05)
        player.set_volume(0.5)
        assert player.is_playing()
    finally:
        player.stop()


# ---------------------------------------------------------------------------
# L: the real production BassStreamPrepareWorker (QThread) + prepared signal
# ---------------------------------------------------------------------------

_REAL_APP = None


def _real_app():
    global _REAL_APP
    from PyQt6 import QtWidgets
    _REAL_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _REAL_APP


def _pump_real(predicate, seconds=5.0):
    app = _real_app()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_real_bass_stream_prepare_worker_end_to_end():
    from billsmusic.bass_player import BassPlayer, _BassEngine
    from billsmusic.workers import BassStreamPrepareWorker

    _real_app()
    _BassEngine.ensure()
    local_path = os.path.join(FIXTURES_DIR, "sample.mp3")

    worker = BassStreamPrepareWorker(local_path, 1)
    # Structural requirement (H): no player reference, ever.
    assert not hasattr(worker, "player")

    prepared = []
    failed = []
    worker.prepared.connect(lambda token, path, candidate: prepared.append((token, path, candidate)))
    worker.failed.connect(lambda token, path, err: failed.append((token, path, err)))

    worker.start()
    assert _pump_real(lambda: bool(prepared) or bool(failed)), "worker never reported back"
    assert not failed, f"real prepare_stream failed: {failed}"
    assert _pump_real(lambda: worker.isFinished()), "worker never finished"
    # The worker's own thread is done -- clean shutdown/cleanup of the
    # QThread itself, nothing left dangling.
    assert not worker.isRunning()

    token, path, candidate = prepared[0]
    assert token == 1
    assert path == local_path

    player = BassPlayer()
    try:
        assert player.commit_prepared(candidate) is True
        player.play()
        assert _pump_real(player.is_playing), "player never started playing"
        time.sleep(0.2)

        levels = _wait_for_fft(player)
        assert levels is not None, "FFT never became available after a real QThread prepare"
        assert len(levels) == 32

        player.seek(0.05)
        player.set_volume(0.5)
        assert player.is_playing()
    finally:
        player.stop()


def test_real_bass_stream_prepare_worker_reports_failure_for_a_missing_file():
    from billsmusic.bass_player import _BassEngine
    from billsmusic.workers import BassStreamPrepareWorker

    _real_app()
    _BassEngine.ensure()
    missing_path = os.path.join(FIXTURES_DIR, "does_not_exist.mp3")

    worker = BassStreamPrepareWorker(missing_path, 1)
    prepared = []
    failed = []
    worker.prepared.connect(lambda token, path, candidate: prepared.append((token, path, candidate)))
    worker.failed.connect(lambda token, path, err: failed.append((token, path, err)))

    worker.start()
    assert _pump_real(lambda: bool(prepared) or bool(failed)), "worker never reported back"
    assert prepared == []
    assert len(failed) == 1
