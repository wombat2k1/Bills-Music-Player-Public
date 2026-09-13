"""Bounded, privacy-aware performance diagnostics for Bills Music Player."""
from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import platform
import queue
import re
import shutil
import sys
import threading
import time
import traceback
import uuid
import zipfile
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional


SCHEMA_VERSION = 1
RECENT_EVENT_LIMIT = 2000
MAX_PENDING_DIAGNOSTIC_EVENTS = 5000
PERFORMANCE_LOG_BYTES = 10 * 1024 * 1024
PERFORMANCE_LOG_BACKUPS = 5
PLAYER_LOG_BYTES = 5 * 1024 * 1024
PLAYER_LOG_BACKUPS = 5
INCIDENT_LIMIT = 15
SESSION_SUMMARY_LIMIT = 20
MINIMUM_FREE_DISK_BYTES = 100 * 1024 * 1024
EVENT_LOOP_PROBE_INTERVAL_MS = 100
EVENT_LOOP_THRESHOLDS_MS = (25, 50, 150, 500)
GUI_OPERATION_WARNING_MS = 30
GUI_OPERATION_SEVERE_MS = 100
WORKER_QUEUE_WARNING_MS = 500
WORKER_EXECUTION_WARNING_MS = 2000

LEVELS = {"off": 0, "basic": 1, "detailed": 2, "developer": 3}
SEVERITY_RANK = {
    "debug": 0, "info": 1, "notice": 2, "warning": 3,
    "severe": 4, "critical": 5, "fatal": 6,
}
SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|authorization|auth[_-]?header)",
    re.IGNORECASE,
)
TOKEN_VALUE_RE = re.compile(
    r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+|"
    r"\b(api[_-]?key|token|secret|password)\s*[:=]\s*[^,\s;]+"
)
WINDOWS_PATH_RE = re.compile(
    r"(?i)(?:[A-Z]:\\|\\\\)[^ \t\r\n\"'<>|]+"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _safe_json(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "<depth-limited>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return TOKEN_VALUE_RE.sub(lambda m: f"{m.group(1) or m.group(2)}=<redacted>", value)[:4000]
    if isinstance(value, dict):
        output = {}
        for key, item in list(value.items())[:200]:
            name = str(key)
            output[name] = "<redacted>" if SECRET_KEY_RE.search(name) else _safe_json(item, depth + 1)
        return output
    if isinstance(value, (list, tuple, set, deque)):
        return [_safe_json(item, depth + 1) for item in list(value)[:200]]
    try:
        return _safe_json(vars(value), depth + 1)
    except Exception:
        return repr(value)[:1000]


def redact_configuration(config: Dict[str, Any]) -> Dict[str, Any]:
    safe_keys = {
        "audio_backend", "use_simple_player", "visual_mode", "lyrics_enabled",
        "normalisation_enabled", "normalisation_mode", "auto_playback_recovery",
        "allow_backend_fallback", "diagnostics_level",
        "diagnostics_include_full_paths", "waveform_seekbar_enabled",
    }
    return {key: _safe_json(config[key]) for key in safe_keys if key in config}


@dataclass
class WorkerToken:
    event_id: str
    category: str
    operation: str
    submitted_at: float
    correlation_id: Optional[str] = None
    generation: Any = None
    details: Optional[Dict[str, Any]] = None


class _Measurement:
    def __init__(self, diagnostics: "PerformanceDiagnostics", **kwargs):
        self.diagnostics = diagnostics
        self.category = kwargs.pop("category")
        self.operation = kwargs.pop("operation")
        self.correlation_id = kwargs.pop("correlation_id", None)
        self.generation = kwargs.pop("generation", None)
        self.details = kwargs.pop("details", None) or {}
        self.threshold_ms = kwargs.pop("threshold_ms", None)
        self.minimum_level = kwargs.pop("minimum_level", "detailed")
        self.feature = kwargs.pop("feature", None)
        self.event_id = uuid.uuid4().hex
        self.started_at = 0.0
        self.cancelled = False

    def __enter__(self):
        self.started_at = time.perf_counter()
        self.diagnostics._push_operation(self)
        if self.diagnostics.level_at_least("detailed"):
            self.diagnostics.record(
                self.category, self.operation, phase="start", status="running",
                event_id=self.event_id, correlation_id=self.correlation_id,
                generation=self.generation, details=self.details,
                minimum_level=self.minimum_level,
            )
        return self

    def cancel(self, reason: str = "cancelled"):
        self.cancelled = True
        self.details["cancellation_reason"] = reason

    def __exit__(self, exc_type, exc, tb):
        duration_ms = (time.perf_counter() - self.started_at) * 1000.0
        self.diagnostics._pop_operation(self)
        status = "cancelled" if self.cancelled else "failure" if exc else "success"
        severity = "severe" if exc else (
            "severe" if duration_ms >= GUI_OPERATION_SEVERE_MS
            else "warning" if duration_ms >= GUI_OPERATION_WARNING_MS
            else "info"
        )
        details = dict(self.details)
        if exc is not None:
            details.update({
                "exception_type": exc_type.__name__ if exc_type else "Exception",
                "exception": str(exc),
                "traceback": "".join(traceback.format_exception(exc_type, exc, tb))[-8000:],
            })
        threshold = self.threshold_ms
        should_record = (
            exc is not None
            or self.cancelled
            or threshold is None
            or duration_ms >= threshold
            or self.diagnostics.level_at_least(self.minimum_level)
        )
        if should_record:
            self.diagnostics.record(
                self.category, self.operation, phase="complete", status=status,
                severity=severity, event_id=self.event_id,
                correlation_id=self.correlation_id, generation=self.generation,
                duration_ms=duration_ms, details=details,
                minimum_level="basic" if severity in ("severe", "critical") else self.minimum_level,
            )
        self.diagnostics.observe_duration(self.category, self.operation, duration_ms, status)
        return False


class PerformanceDiagnostics:
    def __init__(
        self,
        directory: Optional[str] = None,
        level: str = "basic",
        include_full_paths: bool = False,
        gui_thread_id: Optional[int] = None,
        start_writer: bool = True,
    ):
        self.directory = Path(directory or self.default_directory())
        self.level = level if level in LEVELS else "basic"
        self.include_full_paths = bool(include_full_paths)
        self.gui_thread_id = gui_thread_id or threading.main_thread().ident
        self.session_id = uuid.uuid4().hex
        self.started_monotonic = time.perf_counter()
        self.started_utc = _utc_now()
        self.recent_events = deque(maxlen=RECENT_EVENT_LIMIT)
        self.pending: queue.Queue = queue.Queue(MAX_PENDING_DIAGNOSTIC_EVENTS)
        self.dropped_events = Counter()
        self.counters = Counter()
        self.duration_stats = defaultdict(lambda: {"count": 0, "total_ms": 0.0, "max_ms": 0.0})
        self.completed_operation_max = {}
        self.completed_gui_operation_max = {}
        self.worker_stats = defaultdict(lambda: {
            "submitted": 0, "completed": 0, "cancelled": 0, "failed": 0,
            "queued": 0, "running": 0, "queue_wait_total_ms": 0.0,
            "queue_wait_max_ms": 0.0, "execution_total_ms": 0.0,
            "execution_max_ms": 0.0,
        })
        self.lag_samples = deque(maxlen=6000)
        self.lag_counts = Counter()
        self.longest_lag_ms = 0.0
        self.visual_stats = Counter()
        self.visual_max = Counter()
        self.visual_stats_by_consumer = defaultdict(Counter)
        self.visual_max_by_consumer = defaultdict(Counter)
        self.memory_samples = deque(maxlen=240)
        self.peak_memory_bytes = 0
        self.memory_sampling_error = None
        self._thread_local = threading.local()
        self._rate_limit = {}
        self._state_provider: Optional[Callable[[], Dict[str, Any]]] = None
        self._stop = threading.Event()
        self._writer = None
        self._file_output = True
        self._write_lock = threading.Lock()
        self._overhead_ns = 0
        self._events_built = 0
        self._aggregation_ns = 0
        self._aggregation_samples = 0
        self._last_summary_path = None
        self._shutdown_complete = False
        self._disk_space_low = False
        self._last_disk_check = 0.0
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except Exception:
            self._file_output = False
        if start_writer:
            self._writer = threading.Thread(
                target=self._writer_loop,
                name="PerformanceDiagnosticsWriter",
                daemon=True,
            )
            self._writer.start()
        self.record(
            "startup", "diagnostics_session", phase="start", status="running",
            details={"level": self.level}, minimum_level="basic",
        )
        self.sample_memory({"source": "session_start"})

    @staticmethod
    def default_directory() -> str:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Bills Music Player", "Diagnostics")

    @property
    def log_path(self) -> Path:
        return self.directory / "performance_diagnostics.jsonl"

    def configure(self, level: str, include_full_paths: bool = False):
        self.level = level if level in LEVELS else "basic"
        self.include_full_paths = bool(include_full_paths)
        self.record(
            "configuration", "diagnostics_level_change",
            details={"level": self.level, "full_paths": self.include_full_paths},
            minimum_level="basic",
        )

    def level_at_least(self, minimum: str) -> bool:
        return LEVELS.get(self.level, 1) >= LEVELS.get(minimum, 1)

    def set_state_provider(self, provider: Callable[[], Dict[str, Any]]):
        self._state_provider = provider

    def current_operation(self) -> Optional[Dict[str, str]]:
        stack = getattr(self._thread_local, "operations", [])
        if not stack:
            return None
        current = stack[-1]
        return {
            "category": current.category,
            "operation": current.operation,
            "event_id": current.event_id,
        }

    def _push_operation(self, measurement):
        stack = getattr(self._thread_local, "operations", None)
        if stack is None:
            stack = []
            self._thread_local.operations = stack
        stack.append(measurement)

    def _pop_operation(self, measurement):
        stack = getattr(self._thread_local, "operations", [])
        if stack and stack[-1] is measurement:
            stack.pop()
        elif measurement in stack:
            stack.remove(measurement)

    def measure(self, category: str, operation: str, **kwargs):
        return _Measurement(self, category=category, operation=operation, **kwargs)

    def timed(self, category: str, operation: str, **kwargs):
        def decorator(function):
            def wrapped(*args, **call_kwargs):
                with self.measure(category, operation, **kwargs):
                    return function(*args, **call_kwargs)
            return wrapped
        return decorator

    def path_details(self, path: Optional[str]) -> Dict[str, Any]:
        if not path:
            return {}
        path = str(path)
        extension = os.path.splitext(path)[1].lower()
        digest = hashlib.sha256(
            (self.session_id + "|" + os.path.normcase(path)).encode("utf-8", "replace")
        ).hexdigest()[:16]
        result = {"path_hash": digest, "extension": extension}
        if self.include_full_paths:
            result["path"] = path
        return result

    def redact_text(self, text: Any) -> str:
        value = str(text)
        value = TOKEN_VALUE_RE.sub(
            lambda match: f"{match.group(1) or match.group(2)}=<redacted>",
            value,
        )
        if not self.include_full_paths:
            def replace_path(match):
                raw = match.group(0)
                digest = hashlib.sha256(
                    (self.session_id + "|" + raw).encode("utf-8", "replace")
                ).hexdigest()[:12]
                extension = os.path.splitext(raw)[1].lower()
                return f"<path:{digest}{extension}>"
            value = WINDOWS_PATH_RE.sub(replace_path, value)
        return value

    def _privacy_safe(self, value: Any, depth: int = 0) -> Any:
        if depth > 6:
            return "<depth-limited>"
        if isinstance(value, str):
            return self.redact_text(value)[:4000]
        if isinstance(value, dict):
            result = {}
            for key, item in list(value.items())[:200]:
                name = str(key)
                result[name] = (
                    "<redacted>" if SECRET_KEY_RE.search(name)
                    else self._privacy_safe(item, depth + 1)
                )
            return result
        if isinstance(value, (list, tuple, set, deque)):
            return [
                self._privacy_safe(item, depth + 1)
                for item in list(value)[:200]
            ]
        return _safe_json(value, depth)

    def _allowed(self, minimum_level: str, severity: str) -> bool:
        if severity == "fatal":
            return True
        return self.level_at_least(minimum_level)

    def record(
        self, category: str, operation: str, *, phase: str = "instant",
        status: str = "success", severity: str = "info",
        event_id: Optional[str] = None, parent_event_id: Optional[str] = None,
        correlation_id: Optional[str] = None, generation: Any = None,
        duration_ms: Optional[float] = None, queue_wait_ms: Optional[float] = None,
        details: Optional[Dict[str, Any]] = None, minimum_level: str = "detailed",
        rate_limit_seconds: float = 0.0,
    ) -> Optional[Dict[str, Any]]:
        if self._shutdown_complete:
            # Phase C2 (worker lifetime / shutdown ownership, 2026-09-11):
            # the writer thread is already gone by the time this can be
            # reached -- pending.put_nowait() below has nothing left to
            # drain it, so silently dropping instead of queuing forever
            # is the correct behavior, not a bug being masked. shutdown()
            # itself calls record() for its own final event BEFORE
            # setting this flag, so that one still goes through.
            return None
        started_ns = time.perf_counter_ns()
        if not self._allowed(minimum_level, severity):
            return None
        now = time.perf_counter()
        signature = (category, operation, status, severity)
        if rate_limit_seconds:
            previous = self._rate_limit.get(signature)
            if previous and now - previous[0] < rate_limit_seconds:
                self._rate_limit[signature] = (previous[0], previous[1] + 1)
                self.counters["rate_limited"] += 1
                return None
            repeated = previous[1] if previous else 0
            self._rate_limit[signature] = (now, 0)
        else:
            repeated = 0
        thread = threading.current_thread()
        current = self.current_operation()
        event = {
            "schema_version": SCHEMA_VERSION,
            "timestamp_utc": _utc_now(),
            "monotonic_seconds": round(now, 6),
            "session_id": self.session_id,
            "event_id": event_id or uuid.uuid4().hex,
            "parent_event_id": parent_event_id or (current or {}).get("event_id"),
            "correlation_id": correlation_id,
            "category": str(category),
            "operation": str(operation),
            "phase": str(phase),
            "status": str(status),
            "severity": str(severity),
            "thread_name": thread.name,
            "thread_id": thread.ident,
            "gui_thread": thread.ident == self.gui_thread_id,
            "generation": generation,
            "details": self._privacy_safe(details or {}),
        }
        if duration_ms is not None:
            event["duration_ms"] = round(float(duration_ms), 3)
        if queue_wait_ms is not None:
            event["queue_wait_ms"] = round(float(queue_wait_ms), 3)
        if repeated:
            event["repeated_since_previous"] = repeated
        self.recent_events.append(event)
        if (
            duration_ms is not None
            and float(duration_ms) > 0
            and phase != "start"
            and status not in ("running", "cancelled", "stale")
        ):
            completed = {
                "category": str(category),
                "operation": str(operation),
                "duration_ms": float(duration_ms),
                "status": str(status),
            }
            key = (str(category), str(operation))
            if float(duration_ms) > self.completed_operation_max.get(
                key, {}
            ).get("duration_ms", 0):
                self.completed_operation_max[key] = completed
            if event["gui_thread"] and float(duration_ms) > (
                self.completed_gui_operation_max.get(key, {}).get(
                    "duration_ms", 0
                )
            ):
                self.completed_gui_operation_max[key] = completed
        self.counters[f"severity_{severity}"] += 1
        priority = SEVERITY_RANK.get(severity, 1)
        try:
            self.pending.put_nowait((priority, event))
        except queue.Full:
            if priority >= SEVERITY_RANK["severe"]:
                self._drop_one_low_priority()
                try:
                    self.pending.put_nowait((priority, event))
                except queue.Full:
                    self.dropped_events["severe"] += 1
            else:
                self.dropped_events[severity] += 1
        if priority >= SEVERITY_RANK["severe"]:
            self._queue_incident(event)
        self._events_built += 1
        self._overhead_ns += time.perf_counter_ns() - started_ns
        return event

    def _drop_one_low_priority(self):
        retained = []
        dropped = False
        while True:
            try:
                item = self.pending.get_nowait()
            except queue.Empty:
                break
            if not dropped and item[0] < SEVERITY_RANK["severe"]:
                self.dropped_events["low_priority"] += 1
                dropped = True
                continue
            retained.append(item)
        for item in retained:
            try:
                self.pending.put_nowait(item)
            except queue.Full:
                break

    def _queue_incident(self, anomaly):
        if not self._file_output:
            return
        snapshot = {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "anomaly": anomaly,
            "recent_events": list(self.recent_events)[-200:],
            "summary": self.summary_dict(),
        }
        try:
            self.pending.put_nowait((SEVERITY_RANK["critical"], {"_incident": snapshot}))
        except queue.Full:
            self.dropped_events["incident"] += 1

    def observe_duration(self, category, operation, duration_ms, status="success"):
        stats = self.duration_stats[(category, operation)]
        previous_average = (
            stats["total_ms"] / stats["count"] if stats["count"] else 0.0
        )
        stats["count"] += 1
        stats["total_ms"] += duration_ms
        stats["max_ms"] = max(stats["max_ms"], duration_ms)
        self.counters[f"operation_{status}"] += 1
        if (
            stats["count"] >= 6
            and duration_ms >= max(100.0, previous_average * 3.0)
        ):
            self.record(
                category, f"{operation}_baseline_anomaly",
                status="warning", severity="warning",
                duration_ms=duration_ms,
                details={
                    "recent_average_ms": previous_average,
                    "baseline_multiple": (
                        duration_ms / previous_average
                        if previous_average else None
                    ),
                },
                minimum_level="detailed",
                rate_limit_seconds=30.0,
            )

    def submit_worker(self, category, operation, **kwargs) -> WorkerToken:
        token = WorkerToken(
            uuid.uuid4().hex, category, operation, time.perf_counter(),
            kwargs.get("correlation_id"), kwargs.get("generation"),
            kwargs.get("details") or {},
        )
        stats = self.worker_stats[operation]
        stats["submitted"] += 1
        stats["queued"] += 1
        if self.level_at_least("developer"):
            self.record(
                category, operation, phase="submitted", status="queued",
                event_id=token.event_id, correlation_id=token.correlation_id,
                generation=token.generation, details=token.details,
                minimum_level="developer",
            )
        return token

    @contextlib.contextmanager
    def run_worker(self, token: WorkerToken):
        started = time.perf_counter()
        wait_ms = (started - token.submitted_at) * 1000.0
        stats = self.worker_stats[token.operation]
        stats["queued"] = max(0, stats["queued"] - 1)
        stats["running"] += 1
        stats["queue_wait_total_ms"] += wait_ms
        stats["queue_wait_max_ms"] = max(stats["queue_wait_max_ms"], wait_ms)
        status = "success"
        try:
            yield token
        except BaseException:
            status = "cancelled" if sys.exc_info()[0] in (KeyboardInterrupt, SystemExit) else "failure"
            raise
        finally:
            duration_ms = (time.perf_counter() - started) * 1000.0
            stats["running"] = max(0, stats["running"] - 1)
            stats["completed"] += status == "success"
            stats["failed"] += status == "failure"
            stats["cancelled"] += status == "cancelled"
            stats["execution_total_ms"] += duration_ms
            stats["execution_max_ms"] = max(stats["execution_max_ms"], duration_ms)
            severity = "warning" if (
                wait_ms >= WORKER_QUEUE_WARNING_MS
                or duration_ms >= WORKER_EXECUTION_WARNING_MS
            ) else "info"
            self.record(
                token.category, token.operation, phase="complete", status=status,
                severity=severity, event_id=token.event_id,
                correlation_id=token.correlation_id, generation=token.generation,
                duration_ms=duration_ms, queue_wait_ms=wait_ms,
                details=token.details,
                minimum_level="basic" if severity == "warning" else "detailed",
            )

    def observe_event_loop(self, lag_ms: float, state: Optional[Dict[str, Any]] = None):
        overhead_started = time.perf_counter_ns()
        lag_ms = max(0.0, float(lag_ms))
        self.lag_samples.append(lag_ms)
        self.longest_lag_ms = max(self.longest_lag_ms, lag_ms)
        for threshold in EVENT_LOOP_THRESHOLDS_MS:
            if lag_ms >= threshold:
                self.lag_counts[str(threshold)] += 1
        if lag_ms < EVENT_LOOP_THRESHOLDS_MS[0]:
            self._aggregation_ns += time.perf_counter_ns() - overhead_started
            self._aggregation_samples += 1
            return
        severity = (
            "critical" if lag_ms >= 500 else
            "severe" if lag_ms >= 150 else
            "warning" if lag_ms >= 50 else "notice"
        )
        snapshot = state or (self._state_provider() if self._state_provider else {})
        self.record(
            "event_loop", "gui_lag", status="warning", severity=severity,
            duration_ms=lag_ms,
            details={"lag_ms": lag_ms, "application_state": snapshot},
            minimum_level="basic" if lag_ms >= 50 else "detailed",
            rate_limit_seconds=2.0,
        )
        self._aggregation_ns += time.perf_counter_ns() - overhead_started
        self._aggregation_samples += 1

    def observe_visual_frame(
        self, paint_ms: float, gap_ms: float, visible: bool = True,
        target_interval_ms: float = 16.7, consumer: str = "main",
    ):
        overhead_started = time.perf_counter_ns()
        if not visible:
            self.visual_stats["hidden_samples"] += 1
            self._aggregation_ns += time.perf_counter_ns() - overhead_started
            self._aggregation_samples += 1
            return
        self.visual_stats["frames_painted"] += 1
        self.visual_stats["paint_total_us"] += int(max(0.0, paint_ms) * 1000)
        self.visual_stats["gap_total_us"] += int(max(0.0, gap_ms) * 1000)
        self.visual_max["paint_ms"] = max(self.visual_max["paint_ms"], paint_ms)
        self.visual_max["gap_ms"] = max(self.visual_max["gap_ms"], gap_ms)
        # Separate per-consumer breakdown (e.g. "main" window vs "party_mode")
        # -- the aggregate stats above mix every BeatWidget instance together,
        # which hides a slowdown that's specific to just one of them.
        consumer_stats = self.visual_stats_by_consumer[consumer]
        consumer_stats["frames_painted"] += 1
        consumer_stats["paint_total_us"] += int(max(0.0, paint_ms) * 1000)
        consumer_stats["gap_total_us"] += int(max(0.0, gap_ms) * 1000)
        consumer_max = self.visual_max_by_consumer[consumer]
        consumer_max["paint_ms"] = max(consumer_max["paint_ms"], paint_ms)
        consumer_max["gap_ms"] = max(consumer_max["gap_ms"], gap_ms)
        if paint_ms > 16 or gap_ms > max(150, target_interval_ms * 5):
            self.record(
                "visualiser", "frame_stutter", status="warning",
                severity="warning", duration_ms=max(paint_ms, gap_ms),
                details={"paint_ms": paint_ms, "gap_ms": gap_ms, "consumer": consumer},
                minimum_level="detailed", rate_limit_seconds=5.0,
            )
        self._aggregation_ns += time.perf_counter_ns() - overhead_started
        self._aggregation_samples += 1

    def sample_memory(self, details: Optional[Dict[str, Any]] = None):
        working_set = self._working_set_bytes()
        if working_set is None:
            self.memory_sampling_error = (
                self.memory_sampling_error or "working set unavailable"
            )
            self.record(
                "memory", "working_set_sample",
                status="unavailable",
                details={
                    "reason": self.memory_sampling_error,
                    **(details or {}),
                },
                minimum_level="detailed",
                rate_limit_seconds=60.0,
            )
            return
        self.memory_samples.append(working_set)
        self.peak_memory_bytes = max(self.peak_memory_bytes, working_set)
        self.record(
            "memory", "resource_sample",
            details={"working_set_bytes": working_set, **(details or {})},
            minimum_level="detailed",
        )
        if len(self.memory_samples) >= 6:
            baseline = min(list(self.memory_samples)[:3])
            growth = working_set - baseline
            if growth >= 100 * 1024 * 1024:
                self.record(
                    "memory", "suspicious_working_set_growth",
                    status="warning", severity="warning",
                    details={
                        "growth_bytes": growth,
                        "working_set_bytes": working_set,
                    },
                    minimum_level="basic",
                    rate_limit_seconds=300.0,
                )

    @staticmethod
    def _working_set_bytes() -> Optional[int]:
        if os.name == "nt":
            class Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            try:
                from ctypes import wintypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                psapi = ctypes.WinDLL("psapi", use_last_error=True)
                get_process = kernel32.GetCurrentProcess
                get_process.restype = wintypes.HANDLE
                handle = get_process()
                function = getattr(
                    kernel32, "K32GetProcessMemoryInfo", None
                )
                if function is None:
                    function = psapi.GetProcessMemoryInfo
                function.argtypes = [
                    wintypes.HANDLE,
                    ctypes.POINTER(Counters),
                    wintypes.DWORD,
                ]
                function.restype = wintypes.BOOL
                if function(handle, ctypes.byref(counters), counters.cb):
                    return int(counters.WorkingSetSize)
            except Exception:
                return None
        return None

    def _writer_loop(self):
        while not self._stop.is_set() or not self.pending.empty():
            try:
                _priority, event = self.pending.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if "_incident" in event:
                    self._write_incident(event["_incident"])
                else:
                    self._write_event(event)
            except Exception:
                self._file_output = False

    def _write_event(self, event):
        if not self._file_output:
            return
        now = time.monotonic()
        if now - self._last_disk_check >= 30.0:
            self._last_disk_check = now
            try:
                self._disk_space_low = (
                    shutil.disk_usage(self.directory).free
                    < MINIMUM_FREE_DISK_BYTES
                )
            except Exception:
                pass
        if (
            self._disk_space_low
            and SEVERITY_RANK.get(event.get("severity", "info"), 1)
            < SEVERITY_RANK["severe"]
        ):
            self.dropped_events["low_disk_space"] += 1
            return
        started = time.perf_counter()
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            self._rotate(self.log_path, PERFORMANCE_LOG_BYTES, PERFORMANCE_LOG_BACKUPS)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        self.counters["diagnostic_writes"] += 1
        self.counters["diagnostic_write_us"] += int(
            (time.perf_counter() - started) * 1_000_000
        )

    @staticmethod
    def _rotate(path: Path, maximum_bytes: int, backups: int):
        if not path.exists() or path.stat().st_size < maximum_bytes:
            return
        oldest = Path(str(path) + f".{backups}")
        if oldest.exists():
            oldest.unlink()
        for index in range(backups - 1, 0, -1):
            source = Path(str(path) + f".{index}")
            if source.exists():
                source.replace(Path(str(path) + f".{index + 1}"))
        path.replace(Path(str(path) + ".1"))

    def _write_incident(self, snapshot):
        incident_dir = self.directory / "incidents"
        incident_dir.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime("incident-%Y%m%d-%H%M%S-%f.json")
        (incident_dir / name).write_text(
            json.dumps(_safe_json(snapshot), indent=2), encoding="utf-8"
        )
        self._retain(incident_dir.glob("incident-*.json"), INCIDENT_LIMIT)

    @staticmethod
    def _retain(paths, limit):
        items = sorted(paths, key=lambda path: path.stat().st_mtime, reverse=True)
        for path in items[limit:]:
            try:
                path.unlink()
            except Exception:
                pass

    def summary_dict(self) -> Dict[str, Any]:
        samples = sorted(self.lag_samples)
        percentile = lambda p: samples[min(len(samples) - 1, int(len(samples) * p))] if samples else 0.0
        def slowest_from(values):
            if not values:
                return None
            value = max(values.values(), key=lambda item: item["duration_ms"])
            return {
                **value,
                "duration_ms": round(value["duration_ms"], 1),
            }
        slowest = slowest_from(self.completed_operation_max)
        slowest_gui = slowest_from(self.completed_gui_operation_max)
        return {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "started_utc": self.started_utc,
            "session_duration_seconds": round(time.perf_counter() - self.started_monotonic, 3),
            "diagnostics_level": self.level,
            "event_loop": {
                "longest_lag_ms": round(self.longest_lag_ms, 1),
                "average_lag_ms": round(sum(samples) / len(samples), 1) if samples else 0.0,
                "p95_lag_ms": round(percentile(.95), 1),
                "p99_lag_ms": round(percentile(.99), 1),
                "counts": dict(self.lag_counts),
            },
            "slowest_operation": slowest,
            "slowest_gui_operation": slowest_gui,
            "workers": _safe_json(dict(self.worker_stats)),
            "visualiser": {
                **dict(self.visual_stats),
                "longest_paint_ms": round(float(self.visual_max["paint_ms"]), 1),
                "longest_gap_ms": round(float(self.visual_max["gap_ms"]), 1),
                "by_consumer": {
                    name: {
                        "frames_painted": stats.get("frames_painted", 0),
                        "average_paint_ms": round(
                            stats.get("paint_total_us", 0)
                            / max(1, stats.get("frames_painted", 0)) / 1000,
                            3,
                        ),
                        "average_gap_ms": round(
                            stats.get("gap_total_us", 0)
                            / max(1, stats.get("frames_painted", 0)) / 1000,
                            3,
                        ),
                        "longest_paint_ms": round(
                            float(self.visual_max_by_consumer[name]["paint_ms"]), 1
                        ),
                        "longest_gap_ms": round(
                            float(self.visual_max_by_consumer[name]["gap_ms"]), 1
                        ),
                    }
                    for name, stats in self.visual_stats_by_consumer.items()
                },
            },
            "peak_memory_bytes": self.peak_memory_bytes or None,
            "memory_sampling": {
                "available": bool(self.peak_memory_bytes),
                "error": self.memory_sampling_error,
            },
            "playback": {
                "tracks_started": self.counters["tracks_started"],
                "tracks_completed": self.counters["tracks_completed"],
                "backend_usage": {
                    key.removeprefix("backend_"): value
                    for key, value in self.counters.items()
                    if key.startswith("backend_")
                },
                "recovery_attempts": self.counters["recovery_attempts"],
                "recovery_successes": self.counters["recovery_successes"],
                "crossfades_attempted": self.counters["crossfades_attempted"],
                "crossfades_completed": self.counters["crossfades_completed"],
            },
            "searches": {
                "performed": self.counters["searches_performed"],
                "maximum_ms": round(
                    self.duration_stats[("search", "filter_library")]["max_ms"],
                    1,
                ),
            },
            "events_dropped": dict(self.dropped_events),
            "disk_space_low": self._disk_space_low,
            "warnings": self.counters["severity_warning"],
            "severe_anomalies": (
                self.counters["severity_severe"]
                + self.counters["severity_critical"]
                + self.counters["severity_fatal"]
            ),
            "diagnostics_overhead": {
                "events_built": self._events_built,
                "event_build_total_ms": round(self._overhead_ns / 1_000_000, 3),
                "average_event_build_us": round(
                    self._overhead_ns / max(1, self._events_built) / 1000, 3
                ),
                "writes": self.counters["diagnostic_writes"],
                "write_total_ms": round(self.counters["diagnostic_write_us"] / 1000, 3),
                "aggregation_total_ms": round(
                    self._aggregation_ns / 1_000_000, 3
                ),
                "average_aggregation_us": round(
                    self._aggregation_ns
                    / max(1, self._aggregation_samples)
                    / 1000,
                    3,
                ),
                "pending": self.pending.qsize(),
            },
            "log_file": str(self.log_path),
        }

    def human_summary(self) -> str:
        summary = self.summary_dict()
        duration = int(summary["session_duration_seconds"])
        slow = summary["slowest_operation"] or {"operation": "None", "duration_ms": 0}
        event_loop = summary["event_loop"]
        peak_memory = summary["peak_memory_bytes"]
        peak_memory_text = (
            f"{peak_memory / (1024 * 1024):.0f}MB"
            if peak_memory is not None else "unavailable"
        )
        return "\n".join([
            f"Session duration: {duration // 60}m {duration % 60}s",
            f"Longest UI freeze: {event_loop['longest_lag_ms']:.0f}ms",
            f"UI freezes at or above 50ms: {event_loop['counts'].get('50', 0)}",
            f"Slowest operation: {slow['operation']}, {slow['duration_ms']:.0f}ms",
            f"Visualiser longest frame gap: {summary['visualiser']['longest_gap_ms']:.0f}ms",
            f"Peak memory: {peak_memory_text}",
            f"Severe anomalies: {summary['severe_anomalies']}",
            f"Diagnostics dropped events: {sum(summary['events_dropped'].values())}",
        ])

    def write_session_summary(self) -> Optional[Path]:
        if not self._file_output:
            return None
        try:
            summary_dir = self.directory / "session_summaries"
            summary_dir.mkdir(parents=True, exist_ok=True)
            path = summary_dir / datetime.now().strftime("session-%Y%m%d-%H%M%S.json")
            payload = self.summary_dict()
            payload["finished_utc"] = _utc_now()
            payload["human_summary"] = self.human_summary()
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            self._retain(summary_dir.glob("session-*.json"), SESSION_SUMMARY_LIMIT)
            self._last_summary_path = path
            return path
        except Exception:
            return None

    def export_bundle(
        self, destination: str, *, player_log_directory: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        application_details: Optional[Dict[str, Any]] = None,
    ) -> str:
        started = time.perf_counter()
        self.flush(2.0)
        destination = str(destination)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "created_utc": _utc_now(),
            "application": _safe_json(application_details or {}),
            "system": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "architecture": platform.machine(),
            },
            "configuration": redact_configuration(config or {}),
            "summary": self.summary_dict(),
        }
        readme = (
            "Bills Music Player Performance Diagnostic Bundle\n\n"
            "This bundle contains bounded performance logs, incident snapshots, "
            "session summaries and redacted system/configuration details. It does "
            "not contain music, artwork, lyrics, playlists, passwords or tokens.\n\n"
            + self.human_summary() + "\n"
        )
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("README.txt", readme)
            archive.writestr("diagnostic_manifest.json", json.dumps(manifest, indent=2))
            for path in [self.log_path] + [
                Path(str(self.log_path) + f".{index}")
                for index in range(1, PERFORMANCE_LOG_BACKUPS + 1)
            ]:
                if path.exists():
                    archive.write(path, f"performance/{path.name}")
            for folder_name, pattern in (
                ("incidents", "incident-*.json"),
                ("session_summaries", "session-*.json"),
            ):
                folder = self.directory / folder_name
                if folder.exists():
                    for path in sorted(folder.glob(pattern))[-20:]:
                        archive.write(path, f"{folder_name}/{path.name}")
            if player_log_directory:
                log_dir = Path(player_log_directory)
                for path in sorted(log_dir.glob("player.log*"))[:6]:
                    if path.is_file() and path.stat().st_size <= PLAYER_LOG_BYTES * 2:
                        text = path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                        archive.writestr(
                            f"player_logs/{path.name}",
                            self.redact_text(text),
                        )
        self.record(
            "file_io", "export_diagnostic_bundle",
            duration_ms=(time.perf_counter() - started) * 1000.0,
            details={"bytes": os.path.getsize(destination)},
            minimum_level="basic",
        )
        return destination

    def flush(self, timeout: float = 2.0):
        deadline = time.perf_counter() + timeout
        while not self.pending.empty() and time.perf_counter() < deadline:
            time.sleep(0.01)

    def shutdown(self):
        if self._shutdown_complete:
            return
        # Record the final event BEFORE setting _shutdown_complete --
        # record() itself now guards on that flag (Phase C2, 2026-09-11),
        # so setting it first would silently drop this one event too.
        self.record(
            "shutdown", "diagnostics_session", phase="complete",
            duration_ms=(time.perf_counter() - self.started_monotonic) * 1000,
            minimum_level="basic",
        )
        self._shutdown_complete = True
        self.flush(2.0)
        self.write_session_summary()
        self._stop.set()
        if self._writer and self._writer is not threading.current_thread():
            self._writer.join(2.0)


_SINGLETON = None
_SINGLETON_LOCK = threading.Lock()


def get_diagnostics(**kwargs) -> PerformanceDiagnostics:
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = PerformanceDiagnostics(**kwargs)
        return _SINGLETON


def reset_diagnostics_for_tests():
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is not None:
            _SINGLETON.shutdown()
        _SINGLETON = None
