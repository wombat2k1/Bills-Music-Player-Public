"""Small central registry tracking background-worker shutdown ownership.

Not a rewrite of any worker class -- QThread subclasses, raw
threading.Thread wrappers, and QRunnable-driven tasks all register the
same way: a category label, an optional cancellation callback, and an
optional object exposing wait(ms) (a QThread) for a bounded join.
Registration/unregistration are cheap dict operations, safe to call from
any GUI-thread code path; nothing here touches another thread's state
directly -- cancellation is delegated entirely to whatever callback the
caller supplies, since each worker already knows how to stop itself
cooperatively (or, for a handful of legacy cases, does not, in which case
the callback is simply omitted and shutdown_all() logs that it could not
request cancellation).

Phase C2 (worker lifetime / shutdown ownership, 2026-09-11) migrated
every worker category onto this registry -- each registers itself at its
own construction/dispatch site (rather than via ad-hoc inline
cancel/wait/null blocks in a shutdown method), reached via one
shutdown_all() call near the end of PlayerWindow._request_shutdown() (the
former single _shutdown_threads() split into _request_shutdown()/
_finalize_shutdown()) that acts as the single shutdown-ownership
mechanism for all of them.
"""
from __future__ import annotations

import itertools
import time
from typing import Callable, Optional


class _WorkerHandle:
    __slots__ = ("token", "category", "cancel", "thread", "wait_ms", "finalize_after_join")

    def __init__(self, token, category, cancel, thread, wait_ms, finalize_after_join):
        self.token = token
        self.category = category
        self.cancel = cancel
        self.thread = thread
        self.wait_ms = wait_ms
        self.finalize_after_join = finalize_after_join


class WorkerLifetimeRegistry:
    def __init__(self, diagnostics=None):
        self._diagnostics = diagnostics
        self._next_token = itertools.count(1)
        self._workers: dict = {}

    def register(
        self, category: str, *,
        cancel: Optional[Callable[[], None]] = None,
        thread=None, wait_ms: int = 2000,
        finalize_after_join: Optional[Callable[[], None]] = None,
    ) -> int:
        """Records one in-flight worker. ``thread`` is any object exposing
        a QThread-style ``wait(ms) -> bool``; omit it for a worker with no
        interruptible join (e.g. a bare daemon thread), in which case
        shutdown_all() can still request cancellation but can never
        implicitly consider it finished -- see the thread-is-None handling
        in shutdown_all() below; such an entry is only ever removed by its
        own explicit unregister() call. Phase C2 (worker lifetime/shutdown
        ownership, 2026-09-11) registers no thread=None entries -- every
        worker registered this round is a real QThread with a working
        wait(ms); this remains a documented safety property for any future
        caller, not something currently exercised.

        ``finalize_after_join``, if given, runs exactly once, and only
        after ``thread.wait(wait_ms)`` has positively returned True (i.e.
        only once the worker's run() has actually returned) -- never on a
        timeout, never on a wait() exception. This is how a Phase C1
        prepare worker's still-unclaimed PreparedBassStream/
        PreparedMiniaudioSource gets resolved deterministically during
        shutdown even though the GUI thread never returned to the event
        loop to process the worker's queued `prepared` signal (see
        PlayerWindow._finalize_unclaimed_prepare_candidate). A raising
        finalizer is recorded (worker_finalize_error) and does not itself
        change whether the worker is considered finished -- wait() already
        established that positively before the finalizer ever runs.
        Returns a token to pass to unregister()."""
        token = next(self._next_token)
        self._workers[token] = _WorkerHandle(token, category, cancel, thread, wait_ms, finalize_after_join)
        self._record("worker_registered", category, token)
        return token

    def unregister(self, token: Optional[int]) -> None:
        """Call from a worker's own completion path once it has finished
        normally -- keeps the registry from waiting on something already
        done. Safe to call more than once or with None."""
        if token is None:
            return
        handle = self._workers.pop(token, None)
        if handle is not None:
            self._record("worker_finished", handle.category, token)

    def active_count(self) -> int:
        return len(self._workers)

    def shutdown_all(self) -> None:
        """Requests cancellation of every still-registered worker, then
        bound-waits each one that exposes a wait(ms) join. Never raises --
        a worker's own cancel()/wait()/finalize_after_join misbehaving
        must not abort the rest of shutdown.

        UNKNOWN != FINISHED (Phase C2, 2026-09-11): a worker is only ever
        released from this registry when its completion has been
        POSITIVELY established -- thread.wait(wait_ms) returning True.
        wait() returning False (timeout), wait() raising (unknown), and
        cancel() raising are all recorded distinctly but every one of them
        KEEPS the worker registered rather than discarding the final
        strong reference to a possibly-still-live QThread -- see
        CODEX_HANDOFF.md's v1.0.66 entry for why terminating a live
        QThread is deliberately avoided here too."""
        if not self._workers:
            return
        self._record("shutdown_workers_begin", "*", None, count=len(self._workers))
        pending = list(self._workers.values())
        for handle in pending:
            if handle.cancel is not None:
                try:
                    handle.cancel()
                except Exception:
                    self._record("worker_cancel_error", handle.category, handle.token)
                else:
                    self._record("worker_cancel_requested", handle.category, handle.token)
        for handle in pending:
            if handle.thread is None:
                # No wait-capable object registered -- shutdown_all()
                # cannot positively prove this one finished, so it is
                # never implicitly released here. It can only be removed
                # by its own explicit unregister() call elsewhere (its
                # own completion path). See register()'s docstring.
                continue
            started = time.monotonic()
            try:
                finished = bool(handle.thread.wait(handle.wait_ms))
            except Exception:
                elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
                self._record(
                    "worker_wait_error", handle.category, handle.token,
                    elapsed_ms=elapsed_ms,
                )
                continue
            if not finished:
                elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
                self._record(
                    "worker_join_timeout", handle.category, handle.token,
                    elapsed_ms=elapsed_ms,
                )
                continue
            if handle.finalize_after_join is not None:
                try:
                    handle.finalize_after_join()
                except Exception:
                    self._record("worker_finalize_error", handle.category, handle.token)
            self._workers.pop(handle.token, None)
        self._record(
            "shutdown_workers_complete", "*", None,
            remaining=len(self._workers),
        )

    def _record(self, operation, category, token, **extra):
        if self._diagnostics is None:
            return
        details = {"category": category}
        if token is not None:
            details["worker_id"] = token
        details.update(extra)
        try:
            self._diagnostics.record(
                "worker_lifetime", operation, details=details, minimum_level="detailed",
            )
        except Exception:
            pass
