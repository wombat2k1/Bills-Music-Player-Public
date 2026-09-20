"""GUI-stall traceback watchdog for Diagnostics\\stall_traceback.log.

Replaces the earlier ``faulthandler.dump_traceback_later()`` watchdog, which
was not safe to arm in a running application: CPython's faulthandler
watchdog thread walks every thread's interpreter frame stack *without*
holding the GIL, so whenever the GUI thread was merely busy running Python
(or re-entering Python from Qt, e.g. ``eventFilter``) past the threshold, the
walk raced frame push/pop on that thread and could read freed frame-stack
memory -- a native access violation in the faulthandler thread itself.
Reproduced standalone (no Qt, no pytest): a busy main thread plus Python
worker threads with the watchdog re-armed on a 100ms tick crashed 6/6 runs;
the identical workload without the watchdog survived 6/6.

This watchdog is an ordinary Python thread, so it only ever samples stacks
via ``sys._current_frames()`` while holding the GIL -- every frame it sees is
consistent. The trade-off: a GUI thread stuck inside a native call that
never releases the GIL can't be sampled until that call returns (the sample
then shows the Python line right after it). A process-global faulthandler
timer also can't be shared, so the old approach silently cancelled any other
``dump_traceback_later`` user (e.g. pytest's ``faulthandler_timeout``).
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from datetime import timedelta
from typing import Callable, List, Optional, TextIO

MAX_FRAMES_PER_THREAD = 100


def format_all_thread_stacks(exclude_ident: Optional[int] = None) -> str:
    """faulthandler-compatible text for every thread's current Python stack
    (``Thread 0x... (most recent call first):`` / ``  File "...", line N in
    name``), so existing stall_traceback.log reading habits keep working."""
    lines = []
    for ident, frame in sys._current_frames().items():
        if ident == exclude_ident:
            continue
        lines.append(f"Thread 0x{ident:08x} (most recent call first):")
        depth = 0
        while frame is not None:
            if depth >= MAX_FRAMES_PER_THREAD:
                lines.append("  ...")
                break
            code = frame.f_code
            lines.append(f'  File "{code.co_filename}", line {frame.f_lineno} in {code.co_name}')
            frame = frame.f_back
            depth += 1
        lines.append("")
    return "\n".join(lines) + "\n"


class DiagnosticFileCloser:
    """One long-lived daemon thread that closes diagnostic log files handed
    to it, so no shutdown path ever closes (or drops the last reference to)
    one itself.

    Closing a buffered text file flushes whatever is still buffered (e.g. a
    dump whose earlier flush failed), and so does destroying one, so either
    can block for as long as the underlying output does. This service is
    created during normal diagnostic start-up -- never during shutdown,
    which only calls ``submit()``:

    - ``submit(file)`` is a non-blocking put on an unbounded queue; the queue
      entry is a strong reference, held until that file's close() returns,
      so the file can never be finalised on the submitting thread;
    - a close() exception is contained and the service carries on;
    - a close() that blocks delays only later closes; those files stay
      safely referenced in the queue, and callers are never affected;
    - the thread is a daemon and cannot keep the process alive.

    The constructor raises if its thread cannot be started; callers must
    then not own any diagnostic file (see ``ensure_diagnostic_closer``)."""

    def __init__(self, name: str = "DiagnosticFileCloser"):
        self._queue: "queue.SimpleQueue" = queue.SimpleQueue()
        self.closed_count = 0
        self.failed_count = 0
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    @property
    def thread(self) -> threading.Thread:
        return self._thread

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def submit(self, file) -> None:
        self._queue.put(file)

    def _run(self) -> None:
        while True:
            file = self._queue.get()
            try:
                file.close()
                self.closed_count += 1
            except Exception:
                self.failed_count += 1
            file = None  # drop the reference only once its close has run


_default_closer: Optional[DiagnosticFileCloser] = None
_default_closer_lock = threading.Lock()
# Last resort for a file that must leave its owner while no closer exists:
# kept referenced so it is never finalised (and flushed) on the caller's
# thread. Not reachable from PlayerWindow, which never opens a log without
# a closer; see ensure_diagnostic_closer.
_retained_without_closer: List[object] = []


def ensure_diagnostic_closer() -> Optional[DiagnosticFileCloser]:
    """The process-wide closer, starting it on first use. Call only during
    normal diagnostic start-up, before opening a log. Returns None if its
    thread cannot be started, in which case no diagnostic log should be
    opened for this run."""
    global _default_closer
    with _default_closer_lock:
        if _default_closer is None:
            try:
                _default_closer = DiagnosticFileCloser()
            except Exception:
                return None
        return _default_closer


def hand_off_diagnostic_file(file, closer: Optional[DiagnosticFileCloser]) -> None:
    """Give up ownership of ``file`` without closing it here and without
    starting any thread: queue it on an existing closer, or keep it
    referenced if there is none."""
    if closer is not None:
        closer.submit(file)
    else:
        _retained_without_closer.append(file)


class StallTracebackWatchdog:
    """Dumps every thread's stack to ``file`` once per stall, where a stall
    is ``threshold_s`` elapsing without a ``heartbeat()``. Mirrors the old
    ``dump_traceback_later(threshold, repeat=False)`` re-armed on every
    heartbeat: one dump per missed deadline, re-armed by the next heartbeat.

    Shutdown is never subordinate to the diagnostic: ``_lock`` only guards
    small state changes and is never held while sampling, writing or
    flushing, so ``stop(timeout)`` is bounded by ``timeout`` even if a write
    to the log blocks indefinitely. Nor does stop() ever run the file's final
    close() on the caller's thread, or start a thread to do so: the closer it
    hands a file to already exists. See ``stop()`` for who closes the file."""

    def __init__(
        self,
        threshold_s: float,
        file: TextIO,
        clock: Callable[[], float] = time.monotonic,
        closer: Optional[DiagnosticFileCloser] = None,
    ):
        self.threshold_s = float(threshold_s)
        self._file = file
        self._clock = clock
        # Captured now, during normal start-up, so stop() never has to
        # create any execution resource.
        self._closer = closer if closer is not None else ensure_diagnostic_closer()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopped = False
        self._close_file_on_exit = False
        self._file_closed = False
        self.handed_to_closer = False
        self._last_heartbeat = clock()
        self._dumped_for_heartbeat: Optional[float] = None
        self.dump_count = 0
        self._thread = threading.Thread(
            target=self._run, name="StallTracebackWatchdog", daemon=True,
        )
        self._thread.start()

    def heartbeat(self) -> None:
        self._last_heartbeat = self._clock()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def stop(self, timeout: float = 1.0, close_file: bool = False) -> bool:
        """Stop the watchdog, waiting at most ``timeout`` for its thread.

        Returns True if the thread has terminated. The thread is a daemon, so
        a watchdog still stuck in a blocked log write never holds up process
        exit either.

        ``close_file=True`` hands ownership of closing the log file to the
        watchdog, so the caller must not close it itself. The file is closed
        exactly once, never while the thread might still write to it, and
        never by the caller of stop() -- a close can block on flushing
        buffered output, which would defeat ``timeout``. If the thread is
        still running, it closes the file itself as it exits, after its last
        write (a close blocking there only ever delays that daemon thread,
        and join() above is bounded). If it has already terminated, the file
        is queued once on the pre-existing DiagnosticFileCloser -- a
        non-blocking put, no thread start. Idempotent: repeated or concurrent
        calls never queue it twice."""
        with self._lock:
            self._stopped = True
            if close_file:
                self._close_file_on_exit = True
        self._wake.set()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout)
        terminated = not self._thread.is_alive()
        if terminated:
            self._close_file_if_owned(in_background=True)
        return terminated

    def _close_file_if_owned(self, *, in_background: bool) -> None:
        with self._lock:
            if not self._close_file_on_exit or self._file_closed:
                return
            self._file_closed = True  # claimed: exactly one closer
        if in_background:
            hand_off_diagnostic_file(self._file, self._closer)
            self.handed_to_closer = True
            return
        try:
            self._file.close()
        except Exception:
            pass

    def _run(self) -> None:
        try:
            self._sample_until_stopped()
        finally:
            # Already on this daemon thread, after its last write.
            self._close_file_if_owned(in_background=False)

    def _sample_until_stopped(self) -> None:
        own_ident = threading.get_ident()
        while True:
            last = self._last_heartbeat
            if last == self._dumped_for_heartbeat:
                wait_s = self.threshold_s
            else:
                wait_s = max(0.005, last + self.threshold_s - self._clock())
            self._wake.wait(wait_s)
            # State only under the lock -- sampling and log I/O happen after
            # it is released, so stop() can never be made to wait on them.
            with self._lock:
                if self._stopped:
                    return
                last = self._last_heartbeat
                if last == self._dumped_for_heartbeat:
                    continue
                if self._clock() - last < self.threshold_s:
                    continue
                self._dumped_for_heartbeat = last
            try:
                self._file.write(
                    f"Timeout ({timedelta(seconds=self.threshold_s)})!\n"
                    + format_all_thread_stacks(exclude_ident=own_ident)
                )
                self._file.flush()
                self.dump_count += 1
            except Exception:
                pass
