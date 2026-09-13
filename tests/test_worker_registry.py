"""WorkerLifetimeRegistry: the small central shutdown-ownership tracker
used by PlayerWindow._request_shutdown() (v1.0.66 worker-lifetime
hardening; split from the former single _shutdown_threads() into
_request_shutdown()/_finalize_shutdown() by Phase C2, 2026-09-11). Pure
Python, no Qt/QApplication needed -- workers are stood in with plain
fakes."""
from billsmusic.worker_registry import WorkerLifetimeRegistry


class _FakeThread:
    def __init__(self, finishes=True):
        self.finishes = finishes
        self.wait_calls = []

    def wait(self, ms):
        self.wait_calls.append(ms)
        return self.finishes


class _FakeDiagnostics:
    def __init__(self):
        self.events = []

    def record(self, category, operation, *, details=None, minimum_level="detailed"):
        self.events.append((category, operation, details or {}))


def test_register_returns_a_unique_token_each_time():
    registry = WorkerLifetimeRegistry()
    a = registry.register("bio")
    b = registry.register("bio")
    assert a != b
    assert registry.active_count() == 2


def test_unregister_removes_the_worker():
    registry = WorkerLifetimeRegistry()
    token = registry.register("waveform")
    assert registry.active_count() == 1
    registry.unregister(token)
    assert registry.active_count() == 0


def test_unregister_is_safe_for_unknown_or_none_token():
    registry = WorkerLifetimeRegistry()
    registry.unregister(None)  # must not raise
    registry.unregister(99999)  # must not raise


def test_shutdown_all_requests_cancellation_of_every_registered_worker():
    registry = WorkerLifetimeRegistry()
    cancelled = []
    registry.register("album_tag_refresh", cancel=lambda: cancelled.append("a"))
    registry.register("backfill_scan", cancel=lambda: cancelled.append("b"))
    registry.shutdown_all()
    assert sorted(cancelled) == ["a", "b"]


def test_shutdown_all_waits_each_thread_and_clears_finished_ones():
    registry = WorkerLifetimeRegistry()
    thread = _FakeThread(finishes=True)
    registry.register("track_tags", thread=thread, wait_ms=1234)
    registry.shutdown_all()
    assert thread.wait_calls == [1234]
    assert registry.active_count() == 0


def test_shutdown_all_leaves_a_timed_out_worker_registered_not_destroyed():
    registry = WorkerLifetimeRegistry()
    thread = _FakeThread(finishes=False)
    token = registry.register("cast_connect", thread=thread, wait_ms=500)
    registry.shutdown_all()
    # Deliberately NOT force-terminated or dropped -- still owned/tracked.
    assert registry.active_count() == 1
    assert token in registry._workers


def test_shutdown_all_is_a_noop_with_nothing_registered():
    registry = WorkerLifetimeRegistry()
    registry.shutdown_all()  # must not raise


def test_shutdown_all_never_raises_when_cancel_callback_raises():
    # Phase C2 correction: this registration has no thread=, so
    # shutdown_all() can never positively prove it finished regardless of
    # what cancel() does -- it stays registered (see the thread=None
    # semantics above), released only via its own explicit unregister().
    registry = WorkerLifetimeRegistry()
    def _boom():
        raise RuntimeError("boom")
    token = registry.register("playlist_load", cancel=_boom)
    registry.shutdown_all()  # must not raise
    assert registry.active_count() == 1
    registry.unregister(token)
    assert registry.active_count() == 0


def test_shutdown_all_never_raises_when_thread_wait_raises():
    # Phase C2 correction (2026-09-11): UNKNOWN != FINISHED. wait()
    # raising means completion is genuinely unknown, not proven -- the
    # worker must stay registered/owned, not be silently dropped. This
    # test used to assert the opposite (active_count() == 0); that
    # codified exactly the unsafe pattern C2 fixes.
    registry = WorkerLifetimeRegistry()
    class _RaisingThread:
        def wait(self, ms):
            raise RuntimeError("boom")
    token = registry.register("queue_drop", thread=_RaisingThread())
    registry.shutdown_all()  # must not raise
    assert registry.active_count() == 1
    assert token in registry._workers


def test_shutdown_all_never_implicitly_finishes_a_thread_none_registration():
    # Phase C2 correction: a registration with no wait-capable thread
    # object cannot be positively proven finished by shutdown_all() at
    # all -- it must stay registered until its own explicit unregister()
    # call. This test used to assert the opposite (silently cleared);
    # that codified exactly the unsafe pattern C2 fixes.
    registry = WorkerLifetimeRegistry()
    token = registry.register("diagnostic_export")
    registry.shutdown_all()
    assert registry.active_count() == 1
    assert token in registry._workers
    # Only an explicit unregister() (its own completion path) releases it.
    registry.unregister(token)
    assert registry.active_count() == 0


def test_diagnostics_events_recorded_across_the_full_lifecycle(monkeypatch):
    diagnostics = _FakeDiagnostics()
    registry = WorkerLifetimeRegistry(diagnostics=diagnostics)
    finishing = _FakeThread(finishes=True)
    stuck = _FakeThread(finishes=False)
    registry.register("bio", thread=finishing, cancel=lambda: None)
    registry.register("cast_connect", thread=stuck, cancel=lambda: None)
    registry.shutdown_all()

    ops = [e[1] for e in diagnostics.events]
    assert ops.count("worker_registered") == 2
    assert "shutdown_workers_begin" in ops
    assert ops.count("worker_cancel_requested") == 2
    assert "worker_join_timeout" in ops
    assert "shutdown_workers_complete" in ops
    # No full file paths or anything path-shaped ever recorded.
    for _cat, _op, details in diagnostics.events:
        for value in details.values():
            assert not (isinstance(value, str) and ("\\" in value or "/" in value))


def test_diagnostics_failures_never_propagate():
    class _BrokenDiagnostics:
        def record(self, *a, **kw):
            raise RuntimeError("diagnostics down")
    registry = WorkerLifetimeRegistry(diagnostics=_BrokenDiagnostics())
    token = registry.register("waveform")
    registry.unregister(token)
    registry.shutdown_all()  # must not raise despite every _record() call failing


# ---------------------------------------------------------------------------
# Phase C2 (worker lifetime / shutdown ownership, 2026-09-11):
# finalize_after_join and the worker_cancel_error/worker_wait_error/
# worker_finalize_error diagnostic split.
# ---------------------------------------------------------------------------

def test_finalize_after_join_runs_only_when_wait_returns_true():
    registry = WorkerLifetimeRegistry()
    calls = []
    thread = _FakeThread(finishes=True)
    registry.register(
        "plex_audio_load", thread=thread,
        finalize_after_join=lambda: calls.append("finalized"),
    )
    registry.shutdown_all()
    assert calls == ["finalized"]
    assert registry.active_count() == 0


def test_finalize_after_join_never_runs_on_timeout():
    registry = WorkerLifetimeRegistry()
    calls = []
    thread = _FakeThread(finishes=False)
    token = registry.register(
        "plex_audio_load", thread=thread,
        finalize_after_join=lambda: calls.append("finalized"),
    )
    registry.shutdown_all()
    assert calls == []  # never called -- completion was never proven
    assert registry.active_count() == 1
    assert token in registry._workers


def test_finalize_after_join_never_runs_when_wait_raises():
    registry = WorkerLifetimeRegistry()
    calls = []
    class _RaisingThread:
        def wait(self, ms):
            raise RuntimeError("boom")
    token = registry.register(
        "crossfade_load", thread=_RaisingThread(),
        finalize_after_join=lambda: calls.append("finalized"),
    )
    registry.shutdown_all()
    assert calls == []
    assert registry.active_count() == 1
    assert token in registry._workers


def test_finalize_after_join_raising_is_recorded_and_still_releases_the_worker():
    # The finalizer's own job (e.g. discarding an unclaimed candidate) is
    # already exception-safe on its own (see PlayerWindow.
    # _finalize_unclaimed_prepare_candidate / _discard_prepared_candidate),
    # but shutdown_all() must not depend on that -- a raising finalizer
    # must not abort the rest of shutdown, and since wait() already
    # positively proved this worker finished, it is still released.
    diagnostics = _FakeDiagnostics()
    registry = WorkerLifetimeRegistry(diagnostics=diagnostics)
    thread = _FakeThread(finishes=True)

    def _boom():
        raise RuntimeError("finalizer boom")

    token = registry.register("plex_audio_load", thread=thread, finalize_after_join=_boom)
    registry.shutdown_all()  # must not raise
    assert registry.active_count() == 0
    assert token not in registry._workers
    ops = [e[1] for e in diagnostics.events]
    assert "worker_finalize_error" in ops


def test_cancel_error_is_recorded_distinctly_and_keeps_ownership_unproven():
    diagnostics = _FakeDiagnostics()
    registry = WorkerLifetimeRegistry(diagnostics=diagnostics)
    thread = _FakeThread(finishes=False)  # cancel raising must not fabricate completion

    def _boom():
        raise RuntimeError("cancel boom")

    token = registry.register("scan_thread", thread=thread, cancel=_boom, wait_ms=100)
    registry.shutdown_all()  # must not raise
    ops = [e[1] for e in diagnostics.events]
    assert "worker_cancel_error" in ops
    # cancel() raised, so no worker_cancel_requested was recorded for it --
    # and since its thread also didn't finish within wait_ms, it stays
    # registered (cancel() raising must not itself imply completion).
    assert "worker_cancel_requested" not in ops
    assert registry.active_count() == 1
    assert token in registry._workers


def test_wait_error_recorded_as_a_distinct_event_from_join_timeout():
    diagnostics = _FakeDiagnostics()
    registry = WorkerLifetimeRegistry(diagnostics=diagnostics)
    class _RaisingThread:
        def wait(self, ms):
            raise RuntimeError("boom")
    registry.register("bio", thread=_RaisingThread())
    registry.shutdown_all()
    ops = [e[1] for e in diagnostics.events]
    assert "worker_wait_error" in ops
    assert "worker_join_timeout" not in ops  # distinct causes, distinct events
