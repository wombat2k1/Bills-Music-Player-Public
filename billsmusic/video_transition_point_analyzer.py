"""Smart Video Transition Points: detects genuine black/near-black intro
and outro regions in music video files, so the existing GPU dual-video
cross-dissolve (video_dual_transition.py, unmodified by this module) can
start its transition relative to where a video's picture actually ends/
begins rather than blindly relative to the file's raw duration/position 0.

This exists because real near-end testing this session proved the
transition *scheduling* correct (a real 990ms-early commit, measured via
diagnostics) while still showing black gaps on screen -- and direct frame
inspection of the real files involved (ABBA - Super Trouper, ABBA -
Knowing Me, Knowing You) found the outgoing/incoming videos themselves
contain genuine black leader/trailer frames. A technically perfect
transition between "A already faded to black" and "B still on black" still
looks like a black pause, so this is a content-detection problem, not a
compositor or timing bug -- and is layered strictly on top of the existing,
unmodified engine rather than changing it.

Architecture, in three layers:

  - Pure, dependency-free classification (``_classify_visible_boundary``,
    ``detect_content_rect``, ``frame_metrics_within_rect``): decision logic
    only, operating on plain sample tuples/luminance grids. No Qt, no
    subprocess, no I/O -- this is what lets the large majority of this
    feature's required test scenarios run as fast synthetic-data unit
    tests with zero video decode.
  - ``VideoTransitionPointCache``: on-disk cache, deliberately mirroring
    loudness.py's ``LoudnessCache`` shape byte-for-byte (same key scheme,
    same atomic-write pattern, same size/mtime_ns invalidation) rather than
    inventing a new persistence pattern.
  - ``VideoTransitionPointAnalyzer`` (QObject) + ``_VideoTransitionPointProbeProcess``:
    orchestration. The actual frame/audio sampling never runs in this
    process or any of its threads -- it happens in a wholly separate,
    disposable child process (video_transition_point_probe_subprocess.py),
    launched and supervised the same way video_backend.py's
    ``GpuCompositorProbe`` launches its own throwaway GPU-capability-check
    child: a QProcess, line-delimited JSON on stdout, a bounded timeout,
    exactly one result, then killed and discarded. See that class's own
    docstring for why every Qt Multimedia decode in this app stays
    strictly out of the main process's threads.

Conservative by construction: any decode failure, timeout, low confidence,
or disabled preference simply means "no smart trim" -- normal duration-
based/position-0 transition behaviour, unchanged from before this feature
existed. This module is an enhancement, never a playback dependency.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from PyQt6 import QtCore

from .platform_utils import current_executable_path, is_frozen_build

# -- tunable constants (not exposed in Preferences -- see module docstring) -

# A sample only qualifies as "near-black" when BOTH its average luminance
# is below this (0-255 scale) AND the fraction of its sampled pixels below
# the per-pixel dark cutoff exceeds the fraction threshold below -- never a
# single metric alone (real-file calibration: Super Trouper's genuine black
# tail measured avg_luminance == 0.00; Money, Money, Money's dim-but-visible
# opening measured 24-29, comfortably above this threshold and correctly
# excluded).
BLACK_LUMA_THRESHOLD = 12.0
BLACK_DARK_PIXEL_FRACTION_THRESHOLD = 0.90
DARK_PIXEL_PER_PIXEL_CUTOFF = 20.0

# A qualifying run shorter than this is not worth trimming at all.
MIN_TRIM_DURATION_MS = 250
# A qualifying run longer than this is treated as suspicious (more likely
# a genuinely dark scene/legitimate content than removable padding) and
# discarded -- fail safe rather than over-trim.
MAX_TRIM_DURATION_MS = 3000

# Letterbox/pillarbox border-scan: a border row/column qualifies as "bar"
# only when its own average is below this per-line threshold, and only a
# border at least this many samples thick is excluded from analysis.
BORDER_SCAN_LUMA_THRESHOLD = 10.0
BORDER_SCAN_MIN_THICKNESS = 2

ANALYSIS_VERSION = 1

# How many samples in a run, and how deep below threshold, factor into the
# reported confidence -- heuristic and versioned (ANALYSIS_VERSION), not a
# guarantee; callers (especially intro-skip) can apply their own additional
# floor on top of this.
_MIN_CONFIDENT_RUN_SAMPLES = 3


@dataclass(frozen=True)
class BoundarySample:
    offset_ms: int
    avg_luminance: float
    dark_pixel_fraction: float


@dataclass(frozen=True)
class BoundaryResult:
    """Result of classifying one bounded intro or outro sample window.

    ``boundary_ms`` is the raw position where the qualifying black run
    starts (outro) or ends (intro) -- see ``_classify_visible_boundary``.
    ``trim_ms`` is 0 whenever no confident trim was found (the safe
    default). ``confidence`` is in [0.0, 1.0]."""
    boundary_ms: int
    trim_ms: int
    confidence: float


def _classify_visible_boundary(
    samples: Sequence[BoundarySample],
    *,
    from_start: bool,
    edge_ms: int,
    min_trim_ms: int = MIN_TRIM_DURATION_MS,
    max_trim_ms: int = MAX_TRIM_DURATION_MS,
) -> BoundaryResult:
    """Pure decision logic, no Qt/IO. ``samples`` must be sorted ascending
    by ``offset_ms``. ``from_start=True`` classifies an intro (does the
    file *begin* with a sustained black run?); ``from_start=False``
    classifies an outro (does it *end* with one?). ``edge_ms`` is the true
    boundary the run must reach -- 0 for intro, the file's own duration_ms
    for outro -- matching the spec's "must persist to EOF/start, or very
    close to it" requirement literally: a run that stops short of the real
    edge is not anchored to anything and is ignored, no matter how black.

    A single black frame, or a black region in the middle of otherwise
    visible content, can never trigger a trim here: only a *contiguous*
    run anchored at the real edge is ever considered, which structurally
    rules out isolated cuts/flashes without needing a separate special
    case for them."""
    no_trim = BoundaryResult(boundary_ms=edge_ms, trim_ms=0, confidence=0.0)
    if not samples:
        return no_trim
    ordered = sorted(samples, key=lambda s: s.offset_ms)

    def qualifies(sample: BoundarySample) -> bool:
        return (
            sample.avg_luminance < BLACK_LUMA_THRESHOLD
            and sample.dark_pixel_fraction > BLACK_DARK_PIXEL_FRACTION_THRESHOLD
        )

    if from_start:
        run = []
        for sample in ordered:
            if not qualifies(sample):
                break
            run.append(sample)
        if not run:
            return no_trim
        # Anchored at the real start (offset 0) by construction, since we
        # only walked forward from the first sample -- but require the
        # first sample to be at (or essentially at) the true edge, not
        # already partway into the file.
        if run[0].offset_ms > min_trim_ms:
            return no_trim
        boundary_ms = run[-1].offset_ms
        run_duration_ms = boundary_ms - edge_ms
        reached_edge_bonus = 1.0
    else:
        run = []
        for sample in reversed(ordered):
            if not qualifies(sample):
                break
            run.append(sample)
        if not run:
            return no_trim
        run.reverse()
        # Anchored at the real end (duration_ms) by construction -- require
        # the last sample to be at/near the true edge.
        if edge_ms - run[-1].offset_ms > min_trim_ms:
            return no_trim
        boundary_ms = run[0].offset_ms
        run_duration_ms = edge_ms - boundary_ms
        reached_edge_bonus = 1.0

    if run_duration_ms < min_trim_ms:
        return no_trim
    if run_duration_ms > max_trim_ms:
        # Suspiciously long -- more likely a real dark scene than
        # removable padding. Fail safe: no trim.
        return no_trim

    depth = max(0.0, BLACK_LUMA_THRESHOLD - max(s.avg_luminance for s in run)) / max(BLACK_LUMA_THRESHOLD, 1.0)
    sample_confidence = min(1.0, len(run) / float(_MIN_CONFIDENT_RUN_SAMPLES))
    confidence = min(1.0, 0.4 * reached_edge_bonus + 0.3 * sample_confidence + 0.3 * depth)
    trim_ms = int(run_duration_ms)
    return BoundaryResult(boundary_ms=int(boundary_ms), trim_ms=trim_ms, confidence=confidence)


def detect_content_rect(
    grid: Sequence[Sequence[float]],
    *,
    luma_threshold: float = BORDER_SCAN_LUMA_THRESHOLD,
    min_thickness: int = BORDER_SCAN_MIN_THICKNESS,
) -> Tuple[int, int, int, int]:
    """Best-effort letterbox/pillarbox exclusion (v1, heuristic -- see
    module docstring). ``grid`` is a plain rows-of-luminance-values 2D
    sequence (no QImage dependency, so this is directly unit-testable with
    synthetic data). Returns ``(row_start, row_end, col_start, col_end)``
    (end-exclusive) identifying the sub-rectangle to treat as real content;
    defaults to the whole grid when no qualifying border is found. This is
    not a guarantee against a permanently-letterboxed file being
    miscalibrated -- the sustained-run/min/max-duration/confidence gates in
    ``_classify_visible_boundary`` are the actual safety net for that,
    not this scan alone."""
    if not grid or not grid[0]:
        return (0, 0, 0, 0)
    rows = len(grid)
    cols = len(grid[0])

    def row_is_bar(r: int) -> bool:
        row = grid[r]
        return (sum(row) / len(row)) < luma_threshold

    def col_is_bar(c: int) -> bool:
        return (sum(grid[r][c] for r in range(rows)) / rows) < luma_threshold

    row_start = 0
    while row_start < rows and row_is_bar(row_start):
        row_start += 1
    row_end = rows
    while row_end > row_start and row_is_bar(row_end - 1):
        row_end -= 1
    col_start = 0
    while col_start < cols and col_is_bar(col_start):
        col_start += 1
    col_end = cols
    while col_end > col_start and col_is_bar(col_end - 1):
        col_end -= 1

    if row_start < min_thickness:
        row_start = 0
    if rows - row_end < min_thickness:
        row_end = rows
    if col_start < min_thickness:
        col_start = 0
    if cols - col_end < min_thickness:
        col_end = cols
    if row_end <= row_start or col_end <= col_start:
        return (0, rows, 0, cols)
    return (row_start, row_end, col_start, col_end)


def frame_metrics_within_rect(
    grid: Sequence[Sequence[float]],
    rect: Tuple[int, int, int, int],
    *,
    dark_pixel_cutoff: float = DARK_PIXEL_PER_PIXEL_CUTOFF,
) -> Tuple[float, float]:
    """Average luminance + dark-pixel-fraction computed only within
    ``rect`` (as returned by ``detect_content_rect``) -- pure, no Qt."""
    row_start, row_end, col_start, col_end = rect
    total = 0.0
    dark = 0
    count = 0
    for r in range(row_start, row_end):
        for c in range(col_start, col_end):
            value = grid[r][c]
            total += value
            if value < dark_pixel_cutoff:
                dark += 1
            count += 1
    if count == 0:
        return (255.0, 0.0)
    return (total / count, dark / count)


@dataclass
class VideoTransitionPointResult:
    """One file's cached analysis. Either half (outro/intro) may be
    unpopulated (``None`` fields) if that half hasn't been analysed yet --
    the two are analysed independently and merged into one cache entry."""
    duration_ms: int = 0
    analysis_version: int = ANALYSIS_VERSION
    last_visible_ms: Optional[int] = None
    outro_black_duration_ms: int = 0
    outro_confidence: float = 0.0
    first_visible_ms: Optional[int] = None
    intro_black_duration_ms: int = 0
    intro_confidence: float = 0.0
    intro_silent: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "duration_ms": self.duration_ms,
            "analysis_version": self.analysis_version,
            "last_visible_ms": self.last_visible_ms,
            "outro_black_duration_ms": self.outro_black_duration_ms,
            "outro_confidence": self.outro_confidence,
            "first_visible_ms": self.first_visible_ms,
            "intro_black_duration_ms": self.intro_black_duration_ms,
            "intro_confidence": self.intro_confidence,
            "intro_silent": self.intro_silent,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VideoTransitionPointResult":
        return cls(
            duration_ms=int(data.get("duration_ms", 0) or 0),
            analysis_version=int(data.get("analysis_version", ANALYSIS_VERSION) or ANALYSIS_VERSION),
            last_visible_ms=data.get("last_visible_ms"),
            outro_black_duration_ms=int(data.get("outro_black_duration_ms", 0) or 0),
            outro_confidence=float(data.get("outro_confidence", 0.0) or 0.0),
            first_visible_ms=data.get("first_visible_ms"),
            intro_black_duration_ms=int(data.get("intro_black_duration_ms", 0) or 0),
            intro_confidence=float(data.get("intro_confidence", 0.0) or 0.0),
            intro_silent=data.get("intro_silent"),
        )


class VideoTransitionPointCache:
    """Mirrors loudness.py's LoudnessCache shape exactly: same key scheme,
    same atomic tempfile+os.replace write, same size/mtime_ns invalidation
    -- deliberately not a new persistence pattern."""

    def __init__(self, path: str):
        self.path = path
        self._entries: Dict[str, Dict[str, Any]] = {}
        self.load()

    @staticmethod
    def key(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self._entries = dict(data.get("entries") or {})
        except Exception:
            self._entries = {}

    def save(self) -> None:
        folder = os.path.dirname(self.path) or "."
        os.makedirs(folder, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="video-transition-point-", suffix=".json", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {"version": ANALYSIS_VERSION, "entries": self._entries},
                    handle, indent=2,
                )
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def get(self, path: str) -> Optional[VideoTransitionPointResult]:
        item = self._entries.get(self.key(path))
        if not item or not os.path.isfile(path):
            return None
        try:
            stat = os.stat(path)
            if item.get("size") != stat.st_size or item.get("mtime_ns") != stat.st_mtime_ns:
                return None
        except OSError:
            return None
        try:
            return VideoTransitionPointResult.from_dict(item)
        except Exception:
            return None

    def _store(self, path: str, mutate: Callable[[VideoTransitionPointResult], None]) -> None:
        stat = os.stat(path)
        key = self.key(path)
        existing_raw = self._entries.get(key)
        if (
            existing_raw
            and existing_raw.get("size") == stat.st_size
            and existing_raw.get("mtime_ns") == stat.st_mtime_ns
        ):
            try:
                result = VideoTransitionPointResult.from_dict(existing_raw)
            except Exception:
                result = VideoTransitionPointResult()
        else:
            result = VideoTransitionPointResult()
        mutate(result)
        entry = result.to_dict()
        entry["size"] = stat.st_size
        entry["mtime_ns"] = stat.st_mtime_ns
        self._entries[key] = entry
        self.save()

    def store_outro(
        self, path: str, *, duration_ms: int, last_visible_ms: Optional[int],
        black_duration_ms: int, confidence: float,
    ) -> None:
        def mutate(result: VideoTransitionPointResult) -> None:
            result.duration_ms = duration_ms
            result.last_visible_ms = last_visible_ms
            result.outro_black_duration_ms = black_duration_ms
            result.outro_confidence = confidence
        self._store(path, mutate)

    def store_intro(
        self, path: str, *, duration_ms: int, first_visible_ms: Optional[int],
        black_duration_ms: int, confidence: float, intro_silent: Optional[bool],
    ) -> None:
        def mutate(result: VideoTransitionPointResult) -> None:
            result.duration_ms = duration_ms
            result.first_visible_ms = first_visible_ms
            result.intro_black_duration_ms = black_duration_ms
            result.intro_confidence = confidence
            result.intro_silent = intro_silent
        self._store(path, mutate)


def _probe_subprocess_launch_command(path: str, kind: str) -> Tuple[str, List[str]]:
    """Mirrors video_backend.py's ``_subprocess_launch_command`` exactly --
    same packaged-vs-source dispatch, same executable-path helper -- for a
    wholly separate, disposable probe process that shares no code with the
    video playback child process."""
    program = current_executable_path()
    extra = ["--kind", kind, "--path", path]
    if is_frozen_build():
        return program, ["--video-transition-point-probe"] + extra
    return program, ["-m", "billsmusic.video_transition_point_probe_subprocess"] + extra


class _VideoTransitionPointProbeProcess(QtCore.QObject):
    """One-shot bounded analysis of one (path, kind) pair. Mirrors
    video_backend.py's GpuCompositorProbe shape verbatim: throwaway
    QProcess, line-delimited JSON on stdout, bounded timeout, exactly one
    result via a signal, then killed and discarded."""

    finished = QtCore.pyqtSignal(dict)  # always emits, even on failure

    _TIMEOUT_MS = 8000

    def __init__(
        self, path: str, kind: str, parent: Optional[QtCore.QObject] = None, *,
        process_factory: Optional[Callable[[], QtCore.QProcess]] = None,
    ):
        super().__init__(parent)
        self.path = path
        self.kind = kind
        self._done = False
        self._process_factory = process_factory or self._default_process_factory
        self._process: Optional[QtCore.QProcess] = None
        self._timeout_timer = QtCore.QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.timeout.connect(lambda: self._complete({"result": "no_trim", "confidence": 0.0, "reason": "probe_timeout"}))

    def _default_process_factory(self) -> QtCore.QProcess:
        process = QtCore.QProcess(self)
        program, args = _probe_subprocess_launch_command(self.path, self.kind)
        process.setProgram(program)
        process.setArguments(args)
        return process

    def start(self) -> None:
        if self._done:
            return
        self._process = self._process_factory()
        self._process.readyReadStandardOutput.connect(self._on_stdout_ready)
        self._process.finished.connect(self._on_process_finished)
        self._process.errorOccurred.connect(self._on_process_error)
        self._process.start()
        self._timeout_timer.start(self._TIMEOUT_MS)

    def _on_stdout_ready(self) -> None:
        while self._process is not None and self._process.canReadLine():
            raw = bytes(self._process.readLine()).decode("utf-8", errors="replace").strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            if isinstance(payload, dict) and "result" in payload:
                self._complete(payload)
                return

    def _on_process_finished(self, *_args) -> None:
        if not self._done:
            self._complete({"result": "no_trim", "confidence": 0.0, "reason": "process_exited_without_result"})

    def _on_process_error(self, *_args) -> None:
        if not self._done:
            self._complete({"result": "no_trim", "confidence": 0.0, "reason": "process_error"})

    def _complete(self, payload: dict) -> None:
        if self._done:
            return
        self._done = True
        self._timeout_timer.stop()
        process, self._process = self._process, None
        if process is not None:
            try:
                if process.state() != QtCore.QProcess.ProcessState.NotRunning:
                    process.kill()
            except Exception:
                pass
            try:
                process.deleteLater()
            except Exception:
                pass
        self.finished.emit(payload)

    def cancel(self) -> None:
        """Application shutdown, not a normal completion (v1.0.66
        worker-lifetime hardening) -- kills the probe subprocess and marks
        this probe done without emitting ``finished``. Destroying/
        deleteLater()-ing a QProcess object while its external process is
        still running does not terminate that process (Qt's own documented
        behaviour); VideoTransitionPointAnalyzer.shutdown() used to
        deleteLater() the active probe without calling this first, orphaning
        the child instead of actually killing it."""
        if self._done:
            return
        self._done = True
        self._timeout_timer.stop()
        process, self._process = self._process, None
        if process is not None:
            try:
                if process.state() != QtCore.QProcess.ProcessState.NotRunning:
                    process.kill()
            except Exception:
                pass
            try:
                process.deleteLater()
            except Exception:
                pass


class VideoTransitionPointAnalyzer(QtCore.QObject):
    """Public orchestrator the rest of the app talks to. Cache-first,
    never blocks, at most one in-flight probe at a time (analysis is
    explicitly low priority), analyzer-level (path, kind) dedup so a
    caller re-asking "is this cached yet?" before the first probe resolves
    can never launch a second one for the same file. The pending queue is
    priority-ordered: outro requests (current track) are serviced before
    intro requests (Up Next) whenever both are pending."""

    def __init__(
        self, parent: Optional[QtCore.QObject], cache: VideoTransitionPointCache,
        *,
        diagnostic_callback: Optional[Callable[[str, Mapping[str, object]], None]] = None,
        probe_factory: Optional[Callable[[str, str], _VideoTransitionPointProbeProcess]] = None,
        path_hasher: Optional[Callable[[str], str]] = None,
    ):
        super().__init__(parent)
        self.cache = cache
        self._diagnostic_callback = diagnostic_callback
        self._probe_factory = probe_factory or (
            lambda path, kind: _VideoTransitionPointProbeProcess(path, kind, self)
        )
        self._path_hasher = path_hasher or (lambda path: os.path.basename(path))
        self._pending: List[Tuple[str, str]] = []
        self._queued: set = set()
        self._active: Optional[_VideoTransitionPointProbeProcess] = None
        self._closing = False
        # v1.0.67 MainThread I/O hardening: cached_outro_end_ms/
        # cached_intro_start_ms are called from _schedule_deadline on
        # every position tick while SECONDARY_READY (video_dual_transition.py)
        # -- self.cache.get() does a real os.path.isfile+os.stat every
        # single call, a stat-storm on a NAS path for as long as the
        # player sits in that state. This memoizes the *resolved* (already
        # stat-validated) result per path so the hot tick path never
        # touches the filesystem again after the first lookup; invalidated
        # in _apply_result whenever a fresh probe result is stored.
        self._resolved_cache: Dict[str, Optional[VideoTransitionPointResult]] = {}

    # -- public API -----------------------------------------------------
    def analyze_outro(self, path: str) -> None:
        self._request(path, "outro")

    def analyze_intro(self, path: str) -> None:
        self._request(path, "intro")

    def _resolved(self, path: str) -> Optional[VideoTransitionPointResult]:
        if path not in self._resolved_cache:
            self._resolved_cache[path] = self.cache.get(path)
        return self._resolved_cache[path]

    def cached_outro_end_ms(self, path: str) -> Optional[int]:
        result = self._resolved(path)
        if result is None or result.last_visible_ms is None:
            return None
        if result.outro_confidence <= 0.0:
            return None
        return int(result.last_visible_ms)

    def cached_intro_start_ms(self, path: str) -> Optional[int]:
        """Returns a safe seek offset only when BOTH the visual detection
        and the audio-silence check are confidently proven -- an intro
        false positive skips real content the viewer never sees, so this
        is gated more strictly than the outro path (see module/plan
        rationale: outro-trim false positives are nearly harmless, intro
        ones are not)."""
        result = self._resolved(path)
        if result is None or result.first_visible_ms is None:
            return None
        if result.intro_silent is not True:
            return None
        if result.intro_confidence < 0.6:
            return None
        return max(0, int(result.first_visible_ms))

    def shutdown(self) -> None:
        self._closing = True
        self._pending.clear()
        self._queued.clear()
        if self._active is not None:
            # cancel() kills the child process first -- deleteLater() alone
            # (the previous behaviour) only tears down this Qt wrapper and
            # orphans the still-running probe subprocess (v1.0.66
            # worker-lifetime hardening; see _VideoTransitionPointProbe
            # Process.cancel()'s docstring).
            self._active.cancel()
            self._active.deleteLater()
            self._active = None

    # -- internals --------------------------------------------------------
    def _request(self, path: str, kind: str) -> None:
        if self._closing:
            return
        # v1.0.67: analyze_intro() in particular is polled repeatedly
        # (see _maybe_analyze_video_intro's docstring) -- _resolved()
        # keeps repeat polls of an already-resolved path off the
        # filesystem too, same as the cached_*_ms hot path above.
        cached = self._resolved(path)
        if cached is not None:
            cached_relevant = (
                (kind == "outro" and cached.last_visible_ms is not None)
                or (kind == "intro" and cached.first_visible_ms is not None)
            )
            if cached_relevant:
                self._record("cache_hit", {"kind": kind, "path_hash": self._path_hasher(path)})
                return
        token = (path, kind)
        if token in self._queued or (self._active is not None and self._active.path == path and self._active.kind == kind):
            return
        self._queued.add(token)
        if kind == "outro":
            self._pending.insert(0, token)
        else:
            self._pending.append(token)
        self._pump()

    def _pump(self) -> None:
        if self._closing or self._active is not None or not self._pending:
            return
        path, kind = self._pending.pop(0)
        self._queued.discard((path, kind))
        probe = self._probe_factory(path, kind)
        self._active = probe
        probe.finished.connect(lambda payload: self._on_probe_finished(path, kind, payload))
        self._record("analysis_started", {"kind": kind, "path_hash": self._path_hasher(path)})
        probe.start()

    def _on_probe_finished(self, path: str, kind: str, payload: dict) -> None:
        self._active = None
        try:
            self._apply_result(path, kind, payload)
        finally:
            self._pump()

    def _apply_result(self, path: str, kind: str, payload: dict) -> None:
        if not os.path.isfile(path):
            self._record("analysis_failed", {"kind": kind, "reason": "file_missing"})
            return
        # A fresh probe result is about to be written to self.cache below --
        # drop any memoized "nothing here yet" answer so the next
        # cached_outro_end_ms/cached_intro_start_ms call re-resolves it
        # (a single stat, not on the tick hot path) instead of returning
        # stale None forever.
        self._resolved_cache.pop(path, None)
        result = payload.get("result")
        confidence = float(payload.get("confidence", 0.0) or 0.0)
        duration_ms = int(payload.get("duration_ms", 0) or 0)
        details = {"kind": kind, "path_hash": self._path_hasher(path), "duration_ms": duration_ms}
        if result != "trim" or duration_ms <= 0:
            self._record("analysis_complete", {**details, "trim_ms": 0, "confidence": confidence, "fallback_used": True})
            if kind == "outro":
                self.cache.store_outro(
                    path, duration_ms=duration_ms, last_visible_ms=None,
                    black_duration_ms=0, confidence=0.0,
                )
            else:
                self.cache.store_intro(
                    path, duration_ms=duration_ms, first_visible_ms=None,
                    black_duration_ms=0, confidence=0.0, intro_silent=payload.get("intro_silent"),
                )
            return
        boundary_ms = int(payload.get("boundary_ms", 0) or 0)
        trim_ms = int(payload.get("trim_ms", 0) or 0)
        self._record(
            "analysis_complete",
            {**details, "trim_ms": trim_ms, "confidence": confidence, "fallback_used": False},
        )
        if kind == "outro":
            self.cache.store_outro(
                path, duration_ms=duration_ms, last_visible_ms=boundary_ms,
                black_duration_ms=trim_ms, confidence=confidence,
            )
            self._record("smart_outro_detected", {**details, "last_visible_ms": boundary_ms})
        else:
            intro_silent = payload.get("intro_silent")
            self.cache.store_intro(
                path, duration_ms=duration_ms, first_visible_ms=boundary_ms,
                black_duration_ms=trim_ms, confidence=confidence,
                intro_silent=intro_silent if isinstance(intro_silent, bool) else None,
            )
            self._record("smart_intro_detected", {**details, "first_visible_ms": boundary_ms, "intro_silent": intro_silent})

    def _record(self, event: str, details: Mapping[str, object]) -> None:
        callback = self._diagnostic_callback
        if callback is None:
            return
        try:
            callback(event, details)
        except Exception:
            pass
