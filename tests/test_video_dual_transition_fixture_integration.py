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

# FORMER KNOWN BLOCKER, resolved 2026-09-20 (Phase 9): this module was
# skipped because the CPU compositor segfaulted, which was read at the time
# as "painting two concurrently-decoding QVideoFrames is unsafe on this
# Qt/PyQt build". It was not a renderer limitation: the compositor stored
# the QVideoFrame handed to its videoFrameChanged slot, and PyQt wraps that
# `const QVideoFrame &` without copying, so the stored object dangled the
# moment the slot returned and every later paintEvent read freed memory
# (which is also why replacing paint() with toImage() in paintEvent did not
# help -- the frame was already gone by then). The compositor now takes an
# owned copy in the slot; see _DualDeckCompositorWidget._owned. With that,
# the crash rate across the scenario matrix went from 4-8 in 8 runs to 0.
#
# The subprocess crash itself is now pinned by
# test_video_cpu_dual_deck_stability.py, which drives the CPU child over its
# own protocol and fails on the old code.
#
# This module nevertheless stays skipped, for a different and unrelated
# reason found while trying to enable it: the CPU child predates the
# parent's preload-identity protocol. QtVideoPlaybackBackend._accept_preload
# _event() requires every secondary-deck event to carry preload_id,
# source_hash and deck_index/secondary_index, which only the GPU controller
# emits -- the CPU child still emits a bare {"event": "secondary_ready"},
# so the parent rejects it as an unidentified preload and no transition can
# ever complete through this API. Two further staleness: the test passes
# dual_mode=True, but that parameter became a string ("cpu"/"gpu"/None) when
# the GPU compositor was added, so as written it silently launched the
# CLASSIC child, which ignores preload_secondary entirely.
#
# Enabling this module therefore needs the CPU controller ported onto the
# preload-identity protocol, which is a protocol change, not the frame-
# ownership fix Phase 9 was scoped to -- and the product never selects CPU
# mode at all (window.py only ever calls set_dual_mode("gpu")/None). Kept,
# unchanged apart from this note, as the end-to-end check to re-enable if
# that port is ever done.
pytestmark = pytest.mark.skip(
    reason=(
        "CPU dual-deck child predates the parent's preload-identity protocol "
        "(no preload_id/source_hash/deck_index), so secondary events are "
        "rejected before any transition can complete. The segfault this "
        "module was originally skipped for is fixed and covered by "
        "test_video_cpu_dual_deck_stability.py."
    )
)

_DUAL_MODE = "cpu"


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="dual-deck video fixtures not present")
def test_real_dual_mode_subprocess_completes_several_consecutive_transitions():
    _app()
    backend = QtVideoPlaybackBackend(dual_mode=_DUAL_MODE)
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
    backend = QtVideoPlaybackBackend(dual_mode=_DUAL_MODE)
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
