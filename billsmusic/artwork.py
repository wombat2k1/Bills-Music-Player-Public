"""Bounded background artwork decoding for library and player thumbnails."""
from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from PyQt6 import QtCore, QtGui

from .metadata import read_cover_bytes


@dataclass(frozen=True)
class ArtworkResult:
    key: str
    generation: int
    size: tuple[int, int]
    image: Optional[QtGui.QImage]
    source: str
    timings: dict
    thread_name: str


class _TaskSignals(QtCore.QObject):
    finished = QtCore.pyqtSignal(object)


class _ArtworkTask(QtCore.QRunnable):
    def __init__(self, key, paths, disk_path, size, generation, http_source=None):
        super().__init__()
        self.key = key
        self.paths = tuple(paths)
        self.disk_path = disk_path
        self.size = size
        self.generation = generation
        # (url, headers) for a Plex-hosted thumbnail -- tried only when
        # disk_path/paths (both local-file strategies) found nothing. The
        # token travels as a header, never a query parameter, and is never
        # placed on self/ArtworkResult/diagnostics -- it lives only in this
        # local variable for the duration of one GET.
        self.http_source = http_source
        self.signals = _TaskSignals()

    def run(self):
        started = time.perf_counter()
        read_ms = extract_ms = fetch_ms = decode_ms = scale_ms = 0.0
        data = None
        source = "missing"
        try:
            if self.disk_path:
                disk_started = time.perf_counter()
                try:
                    with open(self.disk_path, "rb") as handle:
                        data = handle.read()
                    source = "disk-thumbnail"
                except (FileNotFoundError, OSError):
                    pass
                read_ms += (time.perf_counter() - disk_started) * 1000.0
            if not data:
                for path in self.paths:
                    extract_started = time.perf_counter()
                    data = read_cover_bytes(path)
                    extract_ms += (
                        time.perf_counter() - extract_started
                    ) * 1000.0
                    if data:
                        source = "embedded-or-folder"
                        break
            if not data and self.http_source:
                fetch_started = time.perf_counter()
                data = self._fetch_http_source()
                fetch_ms = (time.perf_counter() - fetch_started) * 1000.0
                if data:
                    source = "plex-thumb"
            image = None
            if data:
                decode_started = time.perf_counter()
                decoded = QtGui.QImage.fromData(data)
                decode_ms = (time.perf_counter() - decode_started) * 1000.0
                if not decoded.isNull():
                    scale_started = time.perf_counter()
                    image = decoded.scaled(
                        self.size[0],
                        self.size[1],
                        QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                        QtCore.Qt.TransformationMode.SmoothTransformation,
                    )
                    scale_ms = (
                        time.perf_counter() - scale_started
                    ) * 1000.0
            result = ArtworkResult(
                self.key,
                self.generation,
                self.size,
                image,
                source,
                {
                    "source_read_ms": read_ms,
                    "embedded_extraction_ms": extract_ms,
                    "http_fetch_ms": fetch_ms,
                    "decode_ms": decode_ms,
                    "scale_ms": scale_ms,
                    "worker_execution_ms": (
                        time.perf_counter() - started
                    ) * 1000.0,
                },
                threading.current_thread().name,
            )
        except Exception:
            result = ArtworkResult(
                self.key,
                self.generation,
                self.size,
                None,
                "failed",
                {"worker_execution_ms": (time.perf_counter() - started) * 1000.0},
                threading.current_thread().name,
            )
        self.signals.finished.emit(result)

    def _fetch_http_source(self) -> Optional[bytes]:
        """GET a Plex thumbnail. Lazy `requests` import (mirrors
        plex_client.py's own convention): this module is imported at
        window.py's module level, so requests must not become a hard
        dependency just because artwork.py exists -- it's only ever
        actually imported here, on a background QThreadPool worker, and
        only when a caller genuinely passed an http_source (Plex
        enabled and a thumb resolved)."""
        try:
            import requests
            from .plex_client import CONNECT_TIMEOUT_S, READ_TIMEOUT_S
        except Exception:
            return None
        url, headers = self.http_source
        try:
            response = requests.get(
                url, headers=headers, timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
            )
        except requests.exceptions.RequestException:
            return None
        if response.status_code != 200:
            return None
        return response.content


class ArtworkManager(QtCore.QObject):
    """Two-worker, deduplicated artwork queue with bounded positive/negative caches."""

    diagnostic = QtCore.pyqtSignal(dict)

    def __init__(self, parent=None, *, max_pending=64, max_cache=128):
        super().__init__(parent)
        self.max_pending = max_pending
        self.max_cache = max_cache
        self.pool = QtCore.QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self._pending = {}
        self._cache = OrderedDict()
        self._failed = OrderedDict()
        self._closed = False

    @property
    def pending_count(self):
        return len(self._pending)

    def request(
        self,
        key: str,
        paths: Iterable[str],
        disk_path: Optional[str],
        size: tuple[int, int],
        generation: int,
        callback: Callable[[ArtworkResult], None],
        http_source: Optional[tuple] = None,
    ) -> bool:
        if self._closed:
            return False
        cache_key = (key, size)
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._cache.move_to_end(cache_key)

            def _deliver_cached():
                # v1.0.66 worker-lifetime hardening: re-checked here, not
                # just at request()'s own entry -- shutdown() can happen
                # between this being scheduled and this callback actually
                # firing on the next event-loop tick.
                if self._closed:
                    return
                callback(
                    ArtworkResult(
                        key, generation, size, cached, "memory-cache",
                        {"memory_cache_lookup_ms": 0.0},
                        threading.current_thread().name,
                    )
                )

            QtCore.QTimer.singleShot(0, _deliver_cached)
            self.diagnostic.emit({"event": "cache_hit", "key": key})
            return True
        if cache_key in self._failed:
            self.diagnostic.emit({
                "event": "negative_cache_hit", "status": "not_found",
                "key": key,
            })
            return False
        pending = self._pending.get(cache_key)
        if pending is not None:
            pending["callbacks"].append((generation, callback))
            self.diagnostic.emit({"event": "deduplicated", "key": key})
            return True
        if len(self._pending) >= self.max_pending:
            self.diagnostic.emit({"event": "cancelled_bounded", "key": key})
            return False
        task = _ArtworkTask(key, paths, disk_path, size, generation, http_source)
        self._pending[cache_key] = {
            "callbacks": [(generation, callback)],
            "queued": time.perf_counter(),
            "task": task,
        }
        task.signals.finished.connect(
            lambda result, ck=cache_key: self._finished(ck, result)
        )
        self.pool.start(task)
        return True

    def _finished(self, cache_key, result):
        pending = self._pending.pop(cache_key, None)
        if pending is None or self._closed:
            return
        wait_ms = max(
            0.0,
            (time.perf_counter() - pending["queued"]) * 1000.0
            - float(result.timings.get("worker_execution_ms", 0.0)),
        )
        timings = dict(result.timings)
        timings["queue_wait_ms"] = wait_ms
        result = ArtworkResult(
            result.key, result.generation, result.size, result.image,
            result.source, timings, result.thread_name,
        )
        if result.image is not None and not result.image.isNull():
            self._cache[cache_key] = result.image
            self._cache.move_to_end(cache_key)
            while len(self._cache) > self.max_cache:
                self._cache.popitem(last=False)
        else:
            self._failed[cache_key] = time.monotonic()
            while len(self._failed) > self.max_cache:
                self._failed.popitem(last=False)
        for generation, callback in pending["callbacks"]:
            callback(
                ArtworkResult(
                    result.key, generation, result.size, result.image,
                    result.source, result.timings, result.thread_name,
                )
            )
        found = result.image is not None and not result.image.isNull()
        self.diagnostic.emit(
            {
                "event": "completed" if found else "negative_cache_store",
                "status": "success" if found else "not_found",
                "key": result.key,
                "pending": len(self._pending),
                "thread": result.thread_name,
                "representative_files_checked": min(3, len(pending["task"].paths)),
                **result.timings,
            }
        )

    def invalidate_negative_cache(self):
        count = len(self._failed)
        self._failed.clear()
        self.diagnostic.emit({
            "event": "negative_cache_invalidated", "count": count,
            "status": "success",
        })

    def shutdown(self):
        self._closed = True
        self._pending.clear()
        self.pool.clear()
        self.pool.waitForDone(2000)
