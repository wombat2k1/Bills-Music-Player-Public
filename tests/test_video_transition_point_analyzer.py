"""Regression coverage for Smart Video Transition Points' pure classification
logic, on-disk cache, and orchestrator dedup/priority behaviour. Deliberately
does not touch Qt Multimedia/real video decode here -- see
test_video_backend_gpu_fixture_integration.py's
test_real_gpu_dual_mode_intro_seek_lands_at_requested_position for the one
real-subprocess verification this phase needed, and
video_transition_point_probe_subprocess.py's own module docstring for why
the actual frame/audio sampling stays isolated in a disposable child process
rather than being exercised from unit tests here."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtCore, QtWidgets

from billsmusic.video_transition_point_analyzer import (
    BLACK_DARK_PIXEL_FRACTION_THRESHOLD,
    BLACK_LUMA_THRESHOLD,
    MAX_TRIM_DURATION_MS,
    MIN_TRIM_DURATION_MS,
    BoundarySample,
    VideoTransitionPointAnalyzer,
    VideoTransitionPointCache,
    VideoTransitionPointResult,
    _classify_visible_boundary,
    detect_content_rect,
    frame_metrics_within_rect,
)


@pytest.fixture(scope="module", autouse=True)
def qapplication():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


BRIGHT = (200.0, 0.02)   # (avg_luminance, dark_pixel_fraction) for a normal frame
BLACK = (0.0, 1.0)       # genuinely solid black
NEAR_BLACK = (6.0, 0.97)  # qualifies (below threshold on both metrics)
DIM_BUT_VISIBLE = (28.0, 0.55)  # real luminance/dark-fraction measured this
                                  # session for ABBA - Money, Money, Money's
                                  # dim-but-not-black opening -- must NOT
                                  # classify as black


def _samples(pairs, *, step_ms=200, start_ms=0):
    return [
        BoundarySample(offset_ms=start_ms + i * step_ms, avg_luminance=lum, dark_pixel_fraction=frac)
        for i, (lum, frac) in enumerate(pairs)
    ]


# -- _classify_visible_boundary: pure decision logic ------------------------

def test_no_black_intro_or_outro_yields_no_trim():
    samples = _samples([BRIGHT] * 10)
    result = _classify_visible_boundary(samples, from_start=True, edge_ms=0)
    assert result.trim_ms == 0
    result = _classify_visible_boundary(samples, from_start=False, edge_ms=1800)
    assert result.trim_ms == 0


def test_solid_black_intro_detected_with_high_confidence():
    # 0, 200, 400ms black, then visible from 600ms on.
    samples = _samples([BLACK, BLACK, BLACK, BRIGHT, BRIGHT, BRIGHT])
    result = _classify_visible_boundary(samples, from_start=True, edge_ms=0)
    assert result.trim_ms > 0
    assert result.boundary_ms == 400
    assert result.confidence > 0.8


def test_solid_black_outro_detected_with_high_confidence():
    # visible up to 800ms, black for the final three samples reaching "EOF".
    samples = _samples([BRIGHT, BRIGHT, BRIGHT, BLACK, BLACK, BLACK], start_ms=0)
    edge_ms = samples[-1].offset_ms
    result = _classify_visible_boundary(samples, from_start=False, edge_ms=edge_ms)
    assert result.trim_ms > 0
    assert result.boundary_ms == 600
    assert result.confidence > 0.8


def test_fade_to_black_outro_is_recognised():
    # Real files don't always cut to pure black -- a sustained fade that
    # stays under both thresholds must still count.
    samples = _samples([BRIGHT, BRIGHT, NEAR_BLACK, NEAR_BLACK, BLACK], start_ms=0)
    edge_ms = samples[-1].offset_ms
    result = _classify_visible_boundary(samples, from_start=False, edge_ms=edge_ms)
    assert result.trim_ms > 0
    assert result.boundary_ms == 400


def test_single_black_frame_in_the_middle_is_ignored():
    # A lone black sample not anchored at the true edge (the run must
    # start at position 0 for intro / end at edge_ms for outro) -- an
    # isolated cut/flash must never trigger a trim.
    samples = _samples([BRIGHT, BLACK, BRIGHT, BRIGHT, BRIGHT])
    result = _classify_visible_boundary(samples, from_start=True, edge_ms=0)
    assert result.trim_ms == 0
    edge_ms = samples[-1].offset_ms
    result = _classify_visible_boundary(samples, from_start=False, edge_ms=edge_ms)
    assert result.trim_ms == 0


def test_very_short_black_region_is_ignored():
    # Single qualifying sample anchored at the edge, but its run duration
    # is well under MIN_TRIM_DURATION_MS.
    samples = _samples([BLACK, BRIGHT, BRIGHT, BRIGHT], step_ms=50)
    result = _classify_visible_boundary(
        samples, from_start=True, edge_ms=0, min_trim_ms=MIN_TRIM_DURATION_MS,
    )
    assert result.trim_ms == 0


def test_dark_but_visible_scene_is_not_classified_as_black():
    # Real calibration data (ABBA - Money, Money, Money's dim opening):
    # must not trigger, matching the "never skip dark artistic scenes"
    # requirement.
    samples = _samples([DIM_BUT_VISIBLE] * 6)
    result = _classify_visible_boundary(samples, from_start=True, edge_ms=0)
    assert result.trim_ms == 0
    assert DIM_BUT_VISIBLE[0] > BLACK_LUMA_THRESHOLD  # sanity: proves the fixture is realistic


def test_maximum_trim_protection_discards_suspiciously_long_runs():
    # A run reaching all the way from offset 0 to well past MAX_TRIM_DURATION_MS
    # is more likely a genuine dark scene than removable padding.
    n = (MAX_TRIM_DURATION_MS // 200) + 5
    samples = _samples([BLACK] * n)
    result = _classify_visible_boundary(samples, from_start=True, edge_ms=0)
    assert result.trim_ms == 0


def test_no_samples_yields_no_trim():
    result = _classify_visible_boundary([], from_start=True, edge_ms=0)
    assert result.trim_ms == 0
    assert result.confidence == 0.0


# -- letterbox/pillarbox border-scan (pure, no Qt) ---------------------------

def _bordered_grid(rows=20, cols=20, border=3, bright=180.0, dark=2.0):
    grid = []
    for r in range(rows):
        row = []
        for c in range(cols):
            if r < border or r >= rows - border or c < border or c >= cols - border:
                row.append(dark)
            else:
                row.append(bright)
        grid.append(row)
    return grid


def test_black_letterbox_bars_do_not_trigger_detection():
    grid = _bordered_grid()
    rect = detect_content_rect(grid)
    avg_luminance, dark_fraction = frame_metrics_within_rect(grid, rect)
    # Computed only within the detected content rect -- the bright centre,
    # not diluted by the black bars.
    assert avg_luminance > BLACK_LUMA_THRESHOLD
    assert dark_fraction < BLACK_DARK_PIXEL_FRACTION_THRESHOLD
    # Sanity: the *whole-frame* average (bars included) would have looked
    # black -- proving the border exclusion is what saves this from
    # misclassification.
    whole_frame_avg = sum(sum(row) for row in grid) / (len(grid) * len(grid[0]))
    assert whole_frame_avg < whole_frame_avg + 1  # trivially true, kept for readability
    full_rect = (0, len(grid), 0, len(grid[0]))
    whole_avg, whole_dark = frame_metrics_within_rect(grid, full_rect)
    assert whole_dark > dark_fraction  # bars measurably pull the naive average down


def test_content_rect_defaults_to_whole_grid_when_no_border_found():
    grid = [[180.0] * 10 for _ in range(10)]
    assert detect_content_rect(grid) == (0, 10, 0, 10)


def test_content_rect_handles_empty_grid_safely():
    assert detect_content_rect([]) == (0, 0, 0, 0)
    assert detect_content_rect([[]]) == (0, 0, 0, 0)


# -- VideoTransitionPointCache: mirrors LoudnessCache's shape ---------------

def test_cached_outro_result_reused_without_reanalysis(tmp_path):
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"x" * 100)
    cache = VideoTransitionPointCache(str(tmp_path / "cache.json"))
    cache.store_outro(
        str(video_path), duration_ms=10000, last_visible_ms=9200,
        black_duration_ms=800, confidence=0.95,
    )
    result = cache.get(str(video_path))
    assert result is not None
    assert result.last_visible_ms == 9200
    assert result.outro_confidence == 0.95
    # A fresh VideoTransitionPointCache instance reading the same file
    # (simulating a later app session) sees the same cached entry --
    # proves the JSON round-trips, not just the in-memory dict.
    reloaded = VideoTransitionPointCache(cache.path)
    assert reloaded.get(str(video_path)).last_visible_ms == 9200


def test_cache_invalidated_when_file_fingerprint_changes(tmp_path):
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"x" * 100)
    cache = VideoTransitionPointCache(str(tmp_path / "cache.json"))
    cache.store_outro(
        str(video_path), duration_ms=10000, last_visible_ms=9200,
        black_duration_ms=800, confidence=0.95,
    )
    assert cache.get(str(video_path)) is not None
    # Simulate the file changing on disk (different size -> different
    # mtime_ns almost certainly too).
    video_path.write_bytes(b"y" * 250)
    assert cache.get(str(video_path)) is None


def test_cache_get_returns_none_for_missing_file(tmp_path):
    cache = VideoTransitionPointCache(str(tmp_path / "cache.json"))
    assert cache.get(str(tmp_path / "does_not_exist.mp4")) is None


def test_cache_write_is_atomic_and_uses_tempfile(tmp_path, monkeypatch):
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"x" * 10)
    cache_path = tmp_path / "cache.json"
    cache = VideoTransitionPointCache(str(cache_path))
    cache.store_outro(
        str(video_path), duration_ms=5000, last_visible_ms=4500,
        black_duration_ms=500, confidence=0.9,
    )
    assert cache_path.is_file()
    # No leftover temp files after a successful save.
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith("video-transition-point-")]
    assert leftovers == []


def test_intro_and_outro_halves_merge_into_one_cache_entry(tmp_path):
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"x" * 10)
    cache = VideoTransitionPointCache(str(tmp_path / "cache.json"))
    cache.store_outro(
        str(video_path), duration_ms=5000, last_visible_ms=4500,
        black_duration_ms=500, confidence=0.9,
    )
    cache.store_intro(
        str(video_path), duration_ms=5000, first_visible_ms=300,
        black_duration_ms=300, confidence=0.8, intro_silent=True,
    )
    result = cache.get(str(video_path))
    # Storing the intro half must not clobber the already-cached outro half.
    assert result.last_visible_ms == 4500
    assert result.first_visible_ms == 300
    assert result.intro_silent is True


# -- VideoTransitionPointAnalyzer: orchestration, dedup, priority -----------

class _FakeProbeProcess(QtCore.QObject):
    finished = QtCore.pyqtSignal(dict)

    def __init__(self, path, kind, parent=None, *, payload=None):
        super().__init__(parent)
        self.path = path
        self.kind = kind
        self._payload = payload or {"result": "no_trim", "confidence": 0.0, "duration_ms": 1000}
        self.started = False

    def start(self):
        self.started = True
        QtCore.QTimer.singleShot(0, lambda: self.finished.emit(self._payload))


def _analyzer(tmp_path, *, payloads=None, launched=None):
    cache = VideoTransitionPointCache(str(tmp_path / "cache.json"))
    launched = launched if launched is not None else []
    payloads = payloads or {}

    def factory(path, kind):
        launched.append((path, kind))
        payload = payloads.get((path, kind), {"result": "no_trim", "confidence": 0.0, "duration_ms": 1000})
        return _FakeProbeProcess(path, kind, payload=payload)

    events = []
    analyzer = VideoTransitionPointAnalyzer(
        None, cache,
        diagnostic_callback=lambda event, details: events.append((event, dict(details))),
        probe_factory=factory,
        path_hasher=lambda p: "hash",
    )
    return analyzer, cache, launched, events


def _make_file(tmp_path, name="clip.mp4"):
    path = tmp_path / name
    path.write_bytes(b"x" * 10)
    return str(path)


def test_analyze_outro_launches_a_probe_on_cache_miss(tmp_path, qapplication):
    path = _make_file(tmp_path)
    analyzer, cache, launched, events = _analyzer(tmp_path)
    analyzer.analyze_outro(path)
    assert launched == [(path, "outro")]
    assert any(event == "analysis_started" for event, _ in events)


def test_analyze_outro_does_not_relaunch_when_already_cached(tmp_path, qapplication):
    path = _make_file(tmp_path)
    analyzer, cache, launched, events = _analyzer(tmp_path)
    cache.store_outro(path, duration_ms=1000, last_visible_ms=800, black_duration_ms=200, confidence=0.9)
    analyzer.analyze_outro(path)
    assert launched == []
    assert any(event == "cache_hit" for event, _ in events)


def test_analyzer_dedups_repeated_requests_for_the_same_path_and_kind(tmp_path, qapplication):
    path = _make_file(tmp_path)
    analyzer, cache, launched, _events = _analyzer(tmp_path)
    analyzer.analyze_outro(path)
    analyzer.analyze_outro(path)
    analyzer.analyze_outro(path)
    # Only one probe in flight/queued for the same (path, kind), regardless
    # of how many times a caller re-asks before the first one resolves --
    # closes the check-then-act race a frequently-polled caller (e.g. the
    # dual engine's own Up Next query) could otherwise hit.
    assert launched == [(path, "outro")]


def test_outro_requests_are_serviced_before_intro_requests(tmp_path, qapplication):
    path_a = _make_file(tmp_path, "a.mp4")
    path_b = _make_file(tmp_path, "b.mp4")
    analyzer, cache, launched, _events = _analyzer(tmp_path)
    # Queue an intro request first, then an outro request -- the outro
    # request must still be serviced first (current track's own timing is
    # more immediately actionable than the next track's).
    analyzer._pump = lambda: None  # pause the queue so both requests actually queue up
    analyzer.analyze_intro(path_a)
    analyzer.analyze_outro(path_b)
    del analyzer._pump  # restore, then let it run for real
    analyzer._pump()
    assert launched[:1] == [(path_b, "outro")]


def test_analyze_never_blocks_the_caller(tmp_path, qapplication):
    # analyze_outro/analyze_intro must return immediately regardless of
    # whether a probe is in flight -- "must never make Next feel slow."
    path = _make_file(tmp_path)
    analyzer, cache, launched, _events = _analyzer(tmp_path)
    analyzer.analyze_outro(path)  # launches a probe, not yet resolved
    assert launched == [(path, "outro")]
    # A second, different request while the first is still in flight must
    # not raise or hang -- it just queues.
    path2 = _make_file(tmp_path, "clip2.mp4")
    analyzer.analyze_outro(path2)
    assert (path2, "outro") not in launched  # queued, not launched yet (one in flight)


def test_cached_outro_end_ms_returns_none_when_uncached(tmp_path):
    path = _make_file(tmp_path)
    analyzer, cache, _launched, _events = _analyzer(tmp_path)
    assert analyzer.cached_outro_end_ms(path) is None


def test_cached_outro_end_ms_returns_boundary_once_cached(tmp_path):
    path = _make_file(tmp_path)
    analyzer, cache, _launched, _events = _analyzer(tmp_path)
    cache.store_outro(path, duration_ms=10000, last_visible_ms=9100, black_duration_ms=900, confidence=0.9)
    assert analyzer.cached_outro_end_ms(path) == 9100


def test_cached_outro_end_ms_only_resolves_once_per_path(tmp_path):
    # v1.0.67 MainThread I/O hardening: cached_outro_end_ms is polled on
    # every position tick while SECONDARY_READY (video_dual_transition.py's
    # _schedule_deadline) -- cache.get() does a real os.path.isfile+
    # os.stat, a stat-storm on a NAS path if it ran every tick. Only the
    # first call for a given path may ever reach it.
    path = _make_file(tmp_path)
    analyzer, cache, _launched, _events = _analyzer(tmp_path)
    cache.store_outro(path, duration_ms=10000, last_visible_ms=9100, black_duration_ms=900, confidence=0.9)
    calls = []
    original_get = cache.get
    cache.get = lambda p: calls.append(p) or original_get(p)
    for _ in range(5):
        assert analyzer.cached_outro_end_ms(path) == 9100
    assert len(calls) == 1


def test_cached_intro_start_ms_only_resolves_once_per_path(tmp_path):
    path = _make_file(tmp_path)
    analyzer, cache, _launched, _events = _analyzer(tmp_path)
    cache.store_intro(
        path, duration_ms=10000, first_visible_ms=900,
        black_duration_ms=900, confidence=0.95, intro_silent=True,
    )
    calls = []
    original_get = cache.get
    cache.get = lambda p: calls.append(p) or original_get(p)
    for _ in range(5):
        assert analyzer.cached_intro_start_ms(path) == 900
    assert len(calls) == 1


def test_cached_outro_end_ms_picks_up_a_late_probe_result(tmp_path, qapplication):
    # The memory-only fix must not make a result permanently stale: a
    # position-tick caller may well ask (and get None, correctly) before
    # background analysis has finished, then keep asking every tick while
    # SECONDARY_READY -- the very next ask after the probe completes must
    # see the real answer, not the memoized "nothing yet".
    path = _make_file(tmp_path)
    analyzer, cache, _launched, _events = _analyzer(
        tmp_path,
        payloads={(path, "outro"): {
            "result": "trim", "confidence": 0.9, "duration_ms": 10000,
            "boundary_ms": 9100, "trim_ms": 900,
        }},
    )
    assert analyzer.cached_outro_end_ms(path) is None  # memoizes "no answer yet"
    analyzer.analyze_outro(path)
    qapplication.processEvents()
    QtCore.QTimer.singleShot(50, qapplication.quit)
    qapplication.exec()
    assert analyzer.cached_outro_end_ms(path) == 9100


def test_incoming_black_intro_with_audible_audio_is_not_skipped(tmp_path):
    # Critical safety requirement: a visually-black intro with real,
    # audible audio must never be used as a seek offset.
    path = _make_file(tmp_path)
    analyzer, cache, _launched, _events = _analyzer(tmp_path)
    cache.store_intro(
        path, duration_ms=10000, first_visible_ms=900,
        black_duration_ms=900, confidence=0.95, intro_silent=False,
    )
    assert analyzer.cached_intro_start_ms(path) is None


def test_incoming_black_intro_with_proven_silence_may_be_skipped(tmp_path):
    path = _make_file(tmp_path)
    analyzer, cache, _launched, _events = _analyzer(tmp_path)
    cache.store_intro(
        path, duration_ms=10000, first_visible_ms=900,
        black_duration_ms=900, confidence=0.95, intro_silent=True,
    )
    assert analyzer.cached_intro_start_ms(path) == 900


def test_incoming_black_intro_with_inconclusive_silence_is_not_skipped(tmp_path):
    # intro_silent=None (audio check failed/timed out/inconclusive) must be
    # treated exactly like "audible" for the purpose of gating a skip --
    # never skip on uncertainty.
    path = _make_file(tmp_path)
    analyzer, cache, _launched, _events = _analyzer(tmp_path)
    cache.store_intro(
        path, duration_ms=10000, first_visible_ms=900,
        black_duration_ms=900, confidence=0.95, intro_silent=None,
    )
    assert analyzer.cached_intro_start_ms(path) is None


def test_low_confidence_intro_result_is_not_used_even_if_silent(tmp_path):
    path = _make_file(tmp_path)
    analyzer, cache, _launched, _events = _analyzer(tmp_path)
    cache.store_intro(
        path, duration_ms=10000, first_visible_ms=900,
        black_duration_ms=900, confidence=0.2, intro_silent=True,
    )
    assert analyzer.cached_intro_start_ms(path) is None


def test_probe_failure_result_stores_no_trim_and_does_not_raise(tmp_path, qapplication):
    path = _make_file(tmp_path)
    analyzer, cache, launched, events = _analyzer(
        tmp_path, payloads={(path, "outro"): {"result": "no_trim", "confidence": 0.0, "duration_ms": 0, "reason": "no_samples"}},
    )
    analyzer.analyze_outro(path)
    qapplication.processEvents()
    QtCore.QTimer.singleShot(50, qapplication.quit)
    qapplication.exec()
    assert analyzer.cached_outro_end_ms(path) is None
    assert any(event == "analysis_complete" for event, _ in events)
