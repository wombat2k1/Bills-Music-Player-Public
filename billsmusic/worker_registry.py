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


#: Overall wall-clock budget shutdown_all() may spend joining workers.
#: Individual wait_ms budgets are retained but clamped to whatever of
#: this remains, so N slow workers cost this once rather than N times.
DEFAULT_SHUTDOWN_DEADLINE_MS = 8000


class WorkerLifetimeRegistry:
    """GUI-THREAD-ONLY. register(), unregister(), active_count() and
    shutdown_all() all mutate/read a plain dict with no lock, and are
    only ever correct when called from the GUI thread.

    This holds today because every unregister() call site is a lambda
    connected to a worker's ``finished`` signal, and PyQt creates the
    connection proxy in the thread that called connect() (always the GUI
    thread here), so delivery is queued onto the GUI thread even though
    the signal is emitted as a worker's run() returns. A single direct
    unregister() call from inside a worker's own run() would silently
    break that invariant -- register from the dispatch site and release
    from a ``finished`` handler, never from worker-thread code.
    """

    def __init__(self, diagnostics=None, monotonic=None):
        self._diagnostics = diagnostics
        self._next_token = itertools.count(1)
        self._workers: dict = {}
        # Injectable purely so shutdown_all()'s deadline arithmetic can be
        # driven deterministically by tests; production always uses the
        # real clock.
        self._monotonic = monotonic if monotonic is not None else time.monotonic

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

    def active_workers(self) -> list:
        """[(token, category), ...] for everything still registered, in
        registration order. Used by the shutdown grace-expiry path to name
        exactly which workers were never proven finished; carries no
        worker-supplied content, only the category label this registry was
        constructed with and its own token."""
        return [(h.token, h.category) for h in self._workers.values()]

    def shutdown_all(self, *, deadline_ms: Optional[int] = DEFAULT_SHUTDOWN_DEADLINE_MS) -> None:
        """Requests cancellation of every still-registered worker, then
        bound-waits each one that exposes a wait(ms) join. Never raises --
        a worker's own cancel()/wait()/finalize_after_join misbehaving
        must not abort the rest of shutdown.

        OVERALL DEADLINE (Phase C2 fix, blocker 2): cancellation is always
        requested for EVERY registered worker first, before any waiting
        begins -- a slow join must never delay another worker being told
        to stop. Each worker then keeps its own configured wait_ms budget,
        clamped to whatever remains of ``deadline_ms`` measured across the
        whole call; once that overall budget is exhausted the remaining
        workers are not waited on at all (recorded as
        worker_deadline_exhausted, one per skipped worker). Without this,
        the joins are sequential and additive: several registered
        categories at 1500-15000ms each can freeze the GUI thread for
        25s+ on a close. Pass deadline_ms=None to restore the old
        unbounded-in-aggregate behaviour.

        A worker skipped because the overall deadline was exhausted is in
        exactly the same position as one that timed out individually: NOT
        proven finished, so it stays registered and its
        finalize_after_join never runs (see below).

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
        deadline = None if deadline_ms is None else self._monotonic() + (deadline_ms / 1000.0)
        for handle in pending:
            if handle.thread is None:
                # No wait-capable object registered -- shutdown_all()
                # cannot positively prove this one finished, so it is
                # never implicitly released here. It can only be removed
                # by its own explicit unregister() call elsewhere (its
                # own completion path). See register()'s docstring.
                continue
            wait_ms = handle.wait_ms
            if deadline is not None:
                remaining_ms = (deadline - self._monotonic()) * 1000.0
                if remaining_ms <= 0:
                    # Overall budget gone -- do not wait on this or any
                    # later worker. Distinct from an individual timeout:
                    # this one was never given a chance to be proven.
                    self._record(
                        "worker_deadline_exhausted", handle.category, handle.token,
                    )
                    continue
                wait_ms = min(wait_ms, int(remaining_ms))
            started = self._monotonic()
            try:
                finished = bool(handle.thread.wait(wait_ms))
            except Exception:
                elapsed_ms = round((self._monotonic() - started) * 1000.0, 1)
                self._record(
                    "worker_wait_error", handle.category, handle.token,
                    elapsed_ms=elapsed_ms,
                )
                continue
            if not finished:
                elapsed_ms = round((self._monotonic() - started) * 1000.0, 1)
                self._record(
                    "worker_join_timeout", handle.category, handle.token,
                    elapsed_ms=elapsed_ms, waited_ms=wait_ms,
                )
                continue
            self._record(
                "worker_joined", handle.category, handle.token,
                elapsed_ms=round((self._monotonic() - started) * 1000.0, 1),
            )
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
