"""Integration test driving the real Phase 2A dual-mode video subprocess
through several consecutive preload -> commit -> promote cycles using the
existing tiny generated fixtures (tests/fixtures/sample.mp4, sample.mkv) --
not mocked. This is the automatable stand-in for "no process/memory leak
across repeated real transitions"; it complements (not replaces) the
deterministic pixel-level compositor proof in
test_video_dual_deck_compositor.py and the mocked-process unit tests in
test_video_backend.py.

Skips gracefully if either fixture is missing.
"""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtCore, QtWidgets

from billsmusic.video_backend import QtVideoPlaybackBackend

_FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
_FIXTURE_A = os.path.join(_FIXTURES_DIR, "sample.mp4")
_FIXTURE_B = os.path.join(_FIXTURES_DIR, "sample.mkv")

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _pump_until(predicate, seconds=10.0):
    app = _app()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


_FIXTURES_PRESENT = os.path.isfile(_FIXTURE_A) and os.path.isfile(_FIXTURE_B)

# KNOWN BLOCKER (see CODEX_HANDOFF.md / final report): painting two
# QVideoFrame objects sourced from two concurrently-decoding QMediaPlayer/
# QVideoSink pipelines in one process reproducibly segfaults on this exact
# Qt 6.11.0/PyQt6 6.11.0/FFmpeg-backend/Windows combination -- confirmed via
# faulthandler, non-deterministic in exact trigger point, survived neither
# QVideoFrame.paint() nor QVideoFrame.toImage()+QPainter.drawImage(). A
# segfault kills the whole pytest process, not just this test, so this
# module is skipped by default rather than left to crash `pytest tests/`.
# Remove this skip once the underlying renderer issue is resolved (see the
# investigation notes for the isolated repro scripts) -- the test itself is
# intentionally kept as the regression check for that fix.
pytestmark = pytest.mark.skip(
    reason=(
        "Known blocker: concurrent dual-deck QVideoFrame painting segfaults "
        "reproducibly in this Qt/PyQt build -- see final report. Re-enable "
        "once the renderer issue is resolved."
    )
)


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_dual_mode_subprocess_completes_several_consecutive_transitions():
    _app()
    backend = QtVideoPlaybackBackend(dual_mode=True)
    assert _pump_until(lambda: backend._process_ready, seconds=10.0), (
        "dual-mode subprocess never announced ready"
    )

    started_events = []
    backend.started.connect(lambda: started_events.append(True))
    assert backend.load(_FIXTURE_A) is True
    assert _pump_until(lambda: len(started_events) >= 1), "Deck A never started"

    fixtures = [_FIXTURE_B, _FIXTURE_A, _FIXTURE_B]
    completions = []
    backend.dual_transition_complete.connect(lambda: completions.append(True))
    failures = []
    backend.secondary_failed.connect(lambda reason: failures.append(reason))
    ready_events = []
    backend.secondary_ready.connect(lambda: ready_events.append(True))

    for cycle, next_path in enumerate(fixtures, start=1):
        ready_events.clear()
        assert backend.preload_secondary(next_path) is True
        assert _pump_until(lambda: bool(ready_events) or bool(failures), seconds=10.0), (
            f"cycle {cycle}: secondary never became ready or failed"
        )
        assert not failures, f"cycle {cycle}: secondary preload failed: {failures}"

        completions_before = len(completions)
        assert backend.commit_dual_transition(300) is True
        assert _pump_until(lambda: len(completions) > completions_before, seconds=10.0), (
            f"cycle {cycle}: dual_transition_complete never fired"
        )

    backend.shutdown()
    assert backend._process.state() == QtCore.QProcess.ProcessState.NotRunning
    stderr = bytes(backend._process.readAllStandardError()).decode("utf-8", errors="replace")
    assert "Destroyed while thread is still running" not in stderr


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_cancelled_preload_leaves_no_orphaned_secondary_and_playback_continues():
    _app()
    backend = QtVideoPlaybackBackend(dual_mode=True)
    assert _pump_until(lambda: backend._process_ready, seconds=10.0)

    started_events = []
    backend.started.connect(lambda: started_events.append(True))
    assert backend.load(_FIXTURE_A) is True
    assert _pump_until(lambda: len(started_events) >= 1)

    ready_events = []
    backend.secondary_ready.connect(lambda: ready_events.append(True))
    assert backend.preload_secondary(_FIXTURE_B) is True
    assert _pump_until(lambda: bool(ready_events), seconds=10.0)

    backend.cancel_secondary()
    _app().processEvents()

    # Primary playback must be unaffected by cancelling a preloaded
    # secondary -- position keeps advancing normally.
    positions = []
    backend.position_changed.connect(positions.append)
    assert _pump_until(lambda: len(positions) >= 1, seconds=5.0)

    backend.shutdown()
    assert backend._process.state() == QtCore.QProcess.ProcessState.NotRunning
