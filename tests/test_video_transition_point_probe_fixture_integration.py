"""Real, non-mocked integration test for Smart Video Transition Points'
isolated analysis subprocess (video_transition_point_probe_subprocess.py),
using purpose-built tiny fixtures with a known black/pattern structure --
not mocked, and not the repo's existing tests/fixtures/sample.mp4/sample.mkv
(which are ordinary colour-bar-style clips with no genuine black content, so
they can only prove a true negative, not real detection).

The fixtures were generated once, programmatically, using PyQt6's own
QMediaCaptureSession/QVideoFrameInput/QMediaRecorder (Qt 6.8+) -- painting
solid-colour QImages and recording them, no external ffmpeg/encoder
dependency needed. See CODEX_HANDOFF.md's Smart Video Transition Points
section for the generation method, in case they ever need regenerating.

This complements test_video_transition_point_analyzer.py's pure
classifier/cache/orchestrator tests (which use a fake, instant probe) and
test_video_backend_gpu_fixture_integration.py's
test_real_gpu_dual_mode_intro_seek_lands_at_requested_position (the one
real-subprocess IPC verification the intro-seek feature needed) -- this is
the one that proves the real frame-sampling/classification pipeline inside
the actual disposable child process works end to end against real decoded
video, not synthetic in-memory sample tuples.

Skips gracefully if the fixtures are missing.
"""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtWidgets

from billsmusic.video_transition_point_analyzer import (
    VideoTransitionPointAnalyzer,
    VideoTransitionPointCache,
)

_FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
_INTRO_BLACK = os.path.join(_FIXTURES_DIR, "smart_transition_intro_black.mp4")
_OUTRO_BLACK = os.path.join(_FIXTURES_DIR, "smart_transition_outro_black.mp4")
_NO_BLACK = os.path.join(_FIXTURES_DIR, "smart_transition_no_black.mp4")
_FIXTURES_PRESENT = all(os.path.isfile(p) for p in (_INTRO_BLACK, _OUTRO_BLACK, _NO_BLACK))

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _pump_until(predicate, seconds=20.0):
    app = _app()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _analyzer(tmp_path):
    cache = VideoTransitionPointCache(str(tmp_path / "cache.json"))
    events = []
    analyzer = VideoTransitionPointAnalyzer(
        None, cache,
        diagnostic_callback=lambda event, details: events.append((event, dict(details))),
        path_hasher=lambda p: "hash",
    )
    return analyzer, cache, events


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="smart transition point fixtures not present")
def test_real_probe_detects_genuine_intro_black_region(tmp_path):
    _app()
    analyzer, cache, events = _analyzer(tmp_path)
    analyzer.analyze_intro(_INTRO_BLACK)
    assert _pump_until(lambda: cache.get(_INTRO_BLACK) is not None, seconds=20.0), (
        "real probe subprocess never completed for the intro-black fixture"
    )
    result = cache.get(_INTRO_BLACK)
    # Fixture is 1.0s black then 1.0s pattern (~2.0s total) -- expect a
    # confidently detected boundary in the neighbourhood of 1000ms, not 0
    # (no detection) and not the full duration (over-detection).
    assert result.first_visible_ms is not None
    assert 400 < result.first_visible_ms < 1600
    assert result.intro_confidence > 0.8
    assert any(event == "smart_intro_detected" for event, _ in events)


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="smart transition point fixtures not present")
def test_real_probe_detects_genuine_outro_black_region(tmp_path):
    _app()
    analyzer, cache, events = _analyzer(tmp_path)
    analyzer.analyze_outro(_OUTRO_BLACK)
    assert _pump_until(lambda: cache.get(_OUTRO_BLACK) is not None, seconds=20.0), (
        "real probe subprocess never completed for the outro-black fixture"
    )
    result = cache.get(_OUTRO_BLACK)
    # Fixture is 1.0s pattern then 1.0s black (~2.0s total) -- expect the
    # detected boundary in the neighbourhood of 1000ms.
    assert result.last_visible_ms is not None
    assert 400 < result.last_visible_ms < 1600
    assert result.outro_confidence > 0.8
    assert any(event == "smart_outro_detected" for event, _ in events)


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="smart transition point fixtures not present")
def test_real_probe_true_negative_on_dark_but_not_black_content(tmp_path):
    """The no-black fixture's middle segment is dim (RGB 70,70,70 -- well
    above BLACK_LUMA_THRESHOLD) rather than genuinely black, sandwiched
    between two normal-brightness segments -- neither end should trigger a
    trim. Real proof (not just a threshold-constant assertion) that the
    conservative "never skip dark scenes" requirement holds against an
    actual decoded, dim-but-visible frame."""
    _app()
    analyzer, cache, events = _analyzer(tmp_path)
    analyzer.analyze_intro(_NO_BLACK)
    analyzer.analyze_outro(_NO_BLACK)

    def both_halves_analyzed():
        return sum(1 for event, _ in events if event == "analysis_complete") >= 2

    assert _pump_until(both_halves_analyzed, seconds=25.0), (
        "both intro and outro analysis should have completed for the fixture"
    )
    result = cache.get(_NO_BLACK)
    assert result is not None
    assert result.first_visible_ms is None
    assert result.last_visible_ms is None


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="smart transition point fixtures not present")
def test_real_probe_result_is_cached_and_not_relaunched(tmp_path):
    _app()
    analyzer, cache, _events = _analyzer(tmp_path)
    analyzer.analyze_outro(_OUTRO_BLACK)
    assert _pump_until(lambda: cache.get(_OUTRO_BLACK) is not None, seconds=20.0)
    first_result = cache.get(_OUTRO_BLACK)

    launched = []
    original_factory = analyzer._probe_factory
    analyzer._probe_factory = lambda path, kind: launched.append((path, kind)) or original_factory(path, kind)
    analyzer.analyze_outro(_OUTRO_BLACK)
    _app().processEvents()
    assert launched == []  # cache hit -- no second subprocess launch
    assert cache.get(_OUTRO_BLACK).last_visible_ms == first_result.last_visible_ms
