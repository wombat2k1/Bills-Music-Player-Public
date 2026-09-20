"""StallTracebackWatchdog (billsmusic/stall_watchdog.py): the GUI-stall
traceback dumper behind Diagnostics\\stall_traceback.log.

The last test is the regression for the full suite's intermittent Windows
access violations ("node down: Not properly terminated"): every real
PlayerWindow armed faulthandler.dump_traceback_later(0.15s) on each 100ms
event-loop probe, and CPython's faulthandler thread walks other threads'
frame stacks without the GIL -- so any >150ms busy stretch of Python on the
GUI thread (routine under `pytest -n auto` load) could crash the process.
It runs in a subprocess because the failure mode is a native crash.
"""
import io
import os
import subprocess
import sys
import textwrap
import threading
import time

from billsmusic.stall_watchdog import StallTracebackWatchdog, format_all_thread_stacks

THRESHOLD_S = 0.1


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _a_distinctively_named_stalled_function(release):
    release.wait(5.0)


def test_format_all_thread_stacks_matches_faulthandler_layout_and_sees_other_threads():
    release = threading.Event()
    worker = threading.Thread(target=_a_distinctively_named_stalled_function, args=(release,))
    worker.start()
    try:
        assert _wait_until(lambda: worker.ident in sys._current_frames())
        text = format_all_thread_stacks()
    finally:
        release.set()
        worker.join()
    assert f"Thread 0x{worker.ident:08x} (most recent call first):" in text
    assert f"Thread 0x{threading.get_ident():08x} (most recent call first):" in text
    assert "in _a_distinctively_named_stalled_function" in text
    assert f'  File "{__file__}", line ' in text


def test_format_all_thread_stacks_can_exclude_a_thread():
    text = format_all_thread_stacks(exclude_ident=threading.get_ident())
    assert f"Thread 0x{threading.get_ident():08x}" not in text


def test_dumps_once_per_stall_and_rearms_on_heartbeat(tmp_path):
    path = tmp_path / "stall_traceback.log"
    with open(path, "a", encoding="utf-8") as handle:
        watchdog = StallTracebackWatchdog(THRESHOLD_S, handle)
        try:
            # A stall: no heartbeat past the threshold -> exactly one dump,
            # showing the stalled (this) thread's stack.
            assert _wait_until(lambda: watchdog.dump_count == 1)
            time.sleep(THRESHOLD_S * 4)
            assert watchdog.dump_count == 1  # not repeated for the same stall

            # A heartbeat re-arms it: the next stall dumps again.
            watchdog.heartbeat()
            assert _wait_until(lambda: watchdog.dump_count == 2)
        finally:
            watchdog.stop()
    assert not watchdog.is_alive()
    text = path.read_text(encoding="utf-8")
    assert text.count("Timeout (0:00:00.100000)!") == 2
    assert f"Thread 0x{threading.get_ident():08x} (most recent call first):" in text
    assert "in test_dumps_once_per_stall_and_rearms_on_heartbeat" in text
    # The watchdog never reports its own sampling thread.
    assert "in format_all_thread_stacks" not in text


def test_regular_heartbeats_never_dump(tmp_path):
    with open(tmp_path / "stall_traceback.log", "a", encoding="utf-8") as handle:
        watchdog = StallTracebackWatchdog(THRESHOLD_S, handle)
        try:
            deadline = time.monotonic() + THRESHOLD_S * 8
            while time.monotonic() < deadline:
                watchdog.heartbeat()
                time.sleep(THRESHOLD_S / 5)
            assert watchdog.dump_count == 0
        finally:
            watchdog.stop()
    assert (tmp_path / "stall_traceback.log").read_text(encoding="utf-8") == ""


def test_stop_joins_promptly_and_writes_nothing_afterwards(tmp_path):
    with open(tmp_path / "stall_traceback.log", "a", encoding="utf-8") as handle:
        watchdog = StallTracebackWatchdog(30.0, handle)
        started = time.monotonic()
        watchdog.stop()
        assert time.monotonic() - started < 1.0  # woken, not left sleeping 30s
        assert not watchdog.is_alive()
        assert watchdog.dump_count == 0


# ---------------------------------------------------------------------------
# Shutdown is never subordinate to the diagnostic (blocked or failing log I/O)
# ---------------------------------------------------------------------------

class _RecordingLog:
    """File-like log. ``block_writes`` makes write() park until released;
    ``fail_writes`` makes it raise. Records closes and any use after close."""

    def __init__(self, block_writes=False, fail_writes=False):
        self.block_writes = block_writes
        self.fail_writes = fail_writes
        self.write_entered = threading.Event()
        self.release = threading.Event()
        self.write_attempts = 0
        self.close_calls = 0
        self.used_after_close = 0
        self.closed = False
        self.text = ""

    def write(self, text):
        if self.closed:
            self.used_after_close += 1
        self.write_attempts += 1
        self.write_entered.set()
        if self.fail_writes:
            raise OSError("disk full")
        if self.block_writes:
            self.release.wait()
        if self.closed:
            self.used_after_close += 1
        self.text += text
        return len(text)

    def flush(self):
        if self.closed:
            self.used_after_close += 1

    def close(self):
        self.close_calls += 1
        self.closed = True


class _BlockingCloseLog(_RecordingLog):
    """close() parks until released (e.g. a final flush of buffered output)."""

    def __init__(self):
        super().__init__()
        self.close_entered = threading.Event()
        self.release_close = threading.Event()
        self.closed_on = None

    def close(self):
        self.close_calls += 1
        self.closed_on = threading.current_thread().name
        self.close_entered.set()
        self.release_close.wait()
        self.closed = True


def _private_closer(name="TestDiagnosticFileCloser"):
    """A closer of the test's own, so a close deliberately held blocked here
    can never back up the process-wide service other tests use."""
    from billsmusic.stall_watchdog import DiagnosticFileCloser

    return DiagnosticFileCloser(name=name)


class _NoThreadStarts:
    """Patches threading.Thread.start to record and refuse every start except
    the named test caller threads -- "can't start new thread" -- and restores
    it on exit."""

    def __init__(self, allowed_names=("shutdown-caller",)):
        self.allowed_names = set(allowed_names)
        self.refused = []
        self._real_start = threading.Thread.start

    def __enter__(self):
        real_start, patch = self._real_start, self

        def _start(thread):
            if thread.name in patch.allowed_names:
                return real_start(thread)
            patch.refused.append(thread.name)
            raise RuntimeError("can't start new thread")

        threading.Thread.start = _start
        return self

    def __exit__(self, *exc):
        threading.Thread.start = self._real_start
        return False


def _stop_in_background(call):
    """Run a stop call on another thread so a regression reports as a failed
    bound instead of hanging the test run."""
    done = threading.Event()
    result = {}

    def _runner():
        started = time.monotonic()
        result["value"] = call()
        result["elapsed"] = time.monotonic() - started
        done.set()

    threading.Thread(target=_runner, name="shutdown-caller", daemon=True).start()
    return done, result


def test_stop_is_bounded_while_a_log_write_is_blocked():
    """Astra regression: stop() used to take _lock before its timed join
    while the watchdog thread held that same lock across write()/flush(), so
    a blocked diagnostic write blocked shutdown for as long as the write did."""
    log = _RecordingLog(block_writes=True)
    watchdog = StallTracebackWatchdog(0.05, log)
    try:
        assert log.write_entered.wait(5.0), "watchdog never started a dump"
        done, result = _stop_in_background(lambda: watchdog.stop(timeout=0.05))
        assert done.wait(1.0), "stop(timeout=0.05) blocked behind a stuck log write"
        assert result["elapsed"] < 1.0
        assert result["value"] is False  # returned; the thread is still in write()
        assert watchdog.is_alive()
        assert log.close_calls == 0
    finally:
        log.release.set()
    assert _wait_until(lambda: not watchdog.is_alive())
    assert log.used_after_close == 0


def test_blocked_writer_keeps_file_ownership_until_its_write_finishes():
    log = _RecordingLog(block_writes=True)
    watchdog = StallTracebackWatchdog(0.05, log)
    try:
        assert log.write_entered.wait(5.0)
        done, result = _stop_in_background(lambda: watchdog.stop(timeout=0.05, close_file=True))
        assert done.wait(1.0)
        assert result["value"] is False
        # Control is back with the caller, but the file is not closed under
        # the write that is still in progress.
        assert log.close_calls == 0 and not log.closed
    finally:
        log.release.set()
    assert _wait_until(lambda: not watchdog.is_alive())
    assert _wait_until(lambda: log.close_calls == 1)  # closed by the exiting thread
    assert log.used_after_close == 0
    assert "Timeout (" in log.text  # the in-flight dump completed intact
    assert watchdog.stop(timeout=0.05, close_file=True) is True
    assert log.close_calls == 1  # never twice


def test_cancel_stall_dump_does_not_hold_shutdown_behind_a_blocked_log_write():
    """Shutdown integration: PlayerWindow._cancel_stall_dump (called from
    _request_shutdown before playback teardown) returns within the watchdog's
    bounded stop even when the diagnostic write never finishes, and leaves
    the file for the watchdog to close afterwards."""
    from types import SimpleNamespace
    from billsmusic.window import PlayerWindow

    log = _RecordingLog(block_writes=True)
    harness = SimpleNamespace(
        _fault_dump_file=log, _stall_watchdog=StallTracebackWatchdog(0.05, log),
    )
    watchdog = harness._stall_watchdog
    try:
        assert log.write_entered.wait(5.0)
        done, result = _stop_in_background(lambda: PlayerWindow._cancel_stall_dump(harness))
        assert done.wait(3.0), "_cancel_stall_dump blocked behind a stuck log write"
        assert result["elapsed"] < 3.0
        assert harness._stall_watchdog is None and harness._fault_dump_file is None
        assert log.close_calls == 0
    finally:
        log.release.set()
    assert _wait_until(lambda: not watchdog.is_alive())
    assert _wait_until(lambda: log.close_calls == 1)
    assert log.used_after_close == 0


def test_ordinary_stop_of_a_sleeping_watchdog_is_prompt_and_closes_the_file():
    log = _RecordingLog()
    watchdog = StallTracebackWatchdog(30.0, log)
    started = time.monotonic()
    assert watchdog.stop(close_file=True) is True
    assert time.monotonic() - started < 1.0
    assert not watchdog.is_alive()
    assert log.close_calls == 1 and log.write_attempts == 0


def test_repeated_stop_is_safe_and_idempotent():
    closer = _private_closer()
    submitted = []
    real_submit = closer.submit
    closer.submit = lambda file: (submitted.append(file), real_submit(file))
    log = _RecordingLog()
    watchdog = StallTracebackWatchdog(30.0, log, closer=closer)
    assert watchdog.stop() is True
    assert log.close_calls == 0  # ownership not handed over: caller's file
    assert watchdog.stop(close_file=True) is True  # thread already gone: queued off-thread
    assert watchdog.stop(close_file=True) is True
    assert watchdog.stop(timeout=0.0) is True
    assert submitted == [log]  # queued exactly once
    assert _wait_until(lambda: log.close_calls == 1)
    assert log.close_calls == 1
    assert not watchdog.is_alive()


def test_rearm_after_cancel_starts_a_fresh_watchdog_and_file(tmp_path):
    from types import SimpleNamespace
    from billsmusic.window import PlayerWindow

    harness = SimpleNamespace(
        diagnostics=SimpleNamespace(directory=str(tmp_path)),
        _fault_dump_file=None, _stall_watchdog=None,
    )
    harness._fault_dump_path = lambda: PlayerWindow._fault_dump_path(harness)
    PlayerWindow._arm_stall_dump(harness)
    first_watchdog, first_file = harness._stall_watchdog, harness._fault_dump_file
    PlayerWindow._cancel_stall_dump(harness)
    assert not first_watchdog.is_alive() and first_file.closed

    PlayerWindow._arm_stall_dump(harness)  # e.g. _restart_performance_probes
    try:
        second_watchdog, second_file = harness._stall_watchdog, harness._fault_dump_file
        assert second_watchdog is not first_watchdog and second_watchdog.is_alive()
        assert second_file is not first_file and not second_file.closed
        second_watchdog.threshold_s = THRESHOLD_S  # stall quickly, no more heartbeats
        assert _wait_until(lambda: second_watchdog.dump_count >= 1)
    finally:
        PlayerWindow._cancel_stall_dump(harness)
    assert not second_watchdog.is_alive() and second_file.closed
    assert "Timeout (" in (tmp_path / "stall_traceback.log").read_text(encoding="utf-8")


def test_failing_log_write_is_contained_and_stop_stays_bounded():
    log = _RecordingLog(fail_writes=True)
    watchdog = StallTracebackWatchdog(THRESHOLD_S, log)
    assert _wait_until(lambda: log.write_attempts >= 1)
    watchdog.heartbeat()  # a later stall is attempted again
    assert _wait_until(lambda: log.write_attempts >= 2)
    assert watchdog.is_alive() and watchdog.dump_count == 0
    started = time.monotonic()
    assert watchdog.stop(timeout=0.5, close_file=True) is True
    assert time.monotonic() - started < 1.0
    assert log.close_calls == 1 and log.used_after_close == 0


def test_failing_stack_sampling_is_contained_and_stop_stays_bounded(monkeypatch):
    import billsmusic.stall_watchdog as stall_watchdog_module

    attempts = []

    def _explode(exclude_ident=None):
        attempts.append(exclude_ident)
        raise RuntimeError("sampling failed")

    monkeypatch.setattr(stall_watchdog_module, "format_all_thread_stacks", _explode)
    log = _RecordingLog()
    watchdog = StallTracebackWatchdog(THRESHOLD_S, log)
    assert _wait_until(lambda: len(attempts) >= 1)
    assert watchdog.is_alive() and watchdog.dump_count == 0 and log.write_attempts == 0
    started = time.monotonic()
    assert watchdog.stop(timeout=0.5, close_file=True) is True
    assert time.monotonic() - started < 1.0
    assert log.close_calls == 1


# ---------------------------------------------------------------------------
# Phase 1.3: the final close() is never run by the caller of stop()
# ---------------------------------------------------------------------------

def test_stop_is_bounded_when_the_final_close_blocks_after_the_thread_terminated():
    """Astra regression: with the watchdog thread already terminated (by an
    earlier stop(close_file=False)), stop(close_file=True) ran file.close()
    synchronously on the caller's thread, so a blocking close -- e.g. one
    flushing buffered output -- blocked stop() past its timeout."""
    closer = _private_closer()
    log = _BlockingCloseLog()
    watchdog = StallTracebackWatchdog(30.0, log, closer=closer)
    assert watchdog.stop(close_file=False) is True  # terminated, file retained
    writes_before = log.write_attempts
    try:
        with _NoThreadStarts() as starts:  # Phase 1.4: handing it off starts no thread
            done, result = _stop_in_background(lambda: watchdog.stop(timeout=0.01, close_file=True))
            assert done.wait(1.0), "stop(timeout=0.01) blocked on the final file close"
        assert starts.refused == []
        assert result["elapsed"] < 1.0
        assert result["value"] is True
        assert log.close_entered.wait(5.0)  # the close really is in progress, blocked
        assert log.closed_on == closer.thread.name  # on the pre-existing closer
        assert not log.closed
    finally:
        log.release_close.set()
    assert _wait_until(lambda: log.closed)
    assert log.close_calls == 1
    assert log.write_attempts == writes_before and log.used_after_close == 0
    assert closer.is_alive()


class _FlakyRawOutput(io.RawIOBase):
    """Raw output under a real BufferedWriter/TextIOWrapper: 'fail' raises
    (so a flush leaves the data buffered), 'block' parks, 'ok' accepts."""

    def __init__(self):
        super().__init__()
        self.mode = "fail"
        self.failed_writes = 0
        self.block_entered = threading.Event()
        self.release = threading.Event()
        self.data = b""

    def writable(self):
        return True

    def write(self, b):
        if self.mode == "fail":
            self.failed_writes += 1
            raise OSError("device not ready")
        if self.mode == "block":
            self.block_entered.set()
            self.release.wait()
        self.data += bytes(b)
        return len(b)


def test_stop_is_bounded_when_closing_a_buffered_log_must_flush_into_blocked_output():
    raw = _FlakyRawOutput()
    log = io.TextIOWrapper(io.BufferedWriter(raw, buffer_size=1 << 20), encoding="utf-8")
    watchdog = StallTracebackWatchdog(THRESHOLD_S, log, closer=_private_closer())
    assert _wait_until(lambda: raw.failed_writes >= 1)  # the dump's flush failed ...
    assert watchdog.stop(close_file=False) is True
    assert watchdog.dump_count == 0  # ... so the dump is still buffered
    raw.mode = "block"
    try:
        done, result = _stop_in_background(lambda: watchdog.stop(timeout=0.01, close_file=True))
        assert done.wait(1.0), "stop(timeout=0.01) blocked flushing the buffered log on close"
        assert result["value"] is True
        assert raw.block_entered.wait(5.0)  # close() is flushing into the blocked output
        assert not log.closed
    finally:
        raw.release.set()
    assert _wait_until(lambda: log.closed)
    assert b"Timeout (" in raw.data  # the buffered dump was not lost


def test_concurrent_stops_requesting_close_have_one_owner_and_one_close():
    closer = _private_closer()
    real_submit = closer.submit
    for iteration in range(60):
        already_terminated = bool(iteration % 2)
        closers = []
        closer.submit = lambda file, closers=closers: (closers.append(file), real_submit(file))
        log = _RecordingLog()
        watchdog = StallTracebackWatchdog(30.0, log, closer=closer)
        if already_terminated:
            assert watchdog.stop(close_file=False) is True
        barrier = threading.Barrier(8)
        errors = []

        def _racer():
            try:
                barrier.wait(5.0)
                watchdog.stop(timeout=0.05, close_file=True)
            except Exception as ex:  # pragma: no cover - reported below
                errors.append(ex)

        racers = [threading.Thread(target=_racer) for _ in range(8)]
        for racer in racers:
            racer.start()
        for racer in racers:
            racer.join(5.0)
        assert not any(racer.is_alive() for racer in racers), f"iteration {iteration}: deadlock"
        assert not errors, errors
        assert _wait_until(lambda: log.close_calls >= 1)
        assert log.close_calls == 1, f"iteration {iteration}: closed {log.close_calls} times"
        # A thread still running closes the file itself on exit; only an
        # already-terminated one queues it, exactly once, on the closer.
        assert len(closers) == (1 if already_terminated else 0)
        assert watchdog.handed_to_closer is already_terminated
        assert log.used_after_close == 0


def test_rearm_while_the_old_log_close_is_blocked_gives_the_new_watchdog_its_own_file(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from billsmusic.window import PlayerWindow

    import billsmusic.window as window_module

    for old_state in ("old_watchdog_running", "old_watchdog_terminated"):
        # One closer shared by both generations, as the process-wide one is.
        closer = _private_closer()
        monkeypatch.setattr(window_module, "ensure_diagnostic_closer", lambda closer=closer: closer)
        directory = tmp_path / old_state
        directory.mkdir()
        old_log = _BlockingCloseLog()
        old_watchdog = StallTracebackWatchdog(30.0, old_log, closer=closer)
        if old_state == "old_watchdog_terminated":
            assert old_watchdog.stop(close_file=False) is True
        harness = SimpleNamespace(
            diagnostics=SimpleNamespace(directory=str(directory)),
            _fault_dump_file=old_log, _stall_watchdog=old_watchdog,
            _diagnostic_closer=closer,
        )
        harness._fault_dump_path = lambda harness=harness: PlayerWindow._fault_dump_path(harness)
        new_watchdog = new_file = None
        try:
            done, _result = _stop_in_background(lambda harness=harness: PlayerWindow._cancel_stall_dump(harness))
            assert done.wait(3.0), old_state
            assert old_log.close_entered.wait(5.0)  # old finalisation is in progress, blocked
            PlayerWindow._arm_stall_dump(harness)  # e.g. _restart_performance_probes
            new_watchdog, new_file = harness._stall_watchdog, harness._fault_dump_file
            assert new_watchdog is not old_watchdog and new_watchdog.is_alive(), old_state
            assert new_file is not old_log and not new_file.closed
            assert _wait_until(lambda: new_watchdog.dump_count >= 1)  # new one works normally
        finally:
            old_log.release_close.set()
        assert _wait_until(lambda: old_log.closed)
        assert old_log.close_calls == 1
        assert not new_file.closed and new_watchdog.is_alive()  # old cleanup touched only its own file
        PlayerWindow._cancel_stall_dump(harness)
        assert _wait_until(lambda: new_file.closed), old_state
        assert "Timeout (" in (directory / "stall_traceback.log").read_text(encoding="utf-8")


def test_request_shutdown_is_not_gated_on_a_blocked_diagnostic_log_close():
    """Real PlayerWindow._request_shutdown: with the stall log's final close
    blocked, shutdown still reaches audio stop, video stop and worker
    shutdown, and the log is closed exactly once afterwards."""
    from types import SimpleNamespace
    from billsmusic.window import PlayerWindow
    from billsmusic.worker_registry import WorkerLifetimeRegistry

    for variant in ("watchdog_running", "watchdog_already_terminated", "no_watchdog"):
        closer = _private_closer()
        log = _BlockingCloseLog()
        watchdog = None
        if variant != "no_watchdog":
            watchdog = StallTracebackWatchdog(30.0, log, closer=closer)
            if variant == "watchdog_already_terminated":
                assert watchdog.stop(close_file=False) is True
        window, steps = _shutdown_harness(log, watchdog, closer)
        try:
            # Phase 1.4: shutdown must not need to start any thread at all.
            with _NoThreadStarts() as starts:
                done, result = _stop_in_background(lambda window=window: PlayerWindow._request_shutdown(window))
                assert done.wait(3.0), f"{variant}: shutdown gated on the diagnostic log close"
            assert starts.refused == [], variant
            assert steps == ["audio_stop", "video_stop", "worker_shutdown"], variant
            assert log.close_entered.wait(5.0) and not log.closed
            expected_closer = "StallTracebackWatchdog" if variant == "watchdog_running" else closer.thread.name
            assert log.closed_on == expected_closer, variant
        finally:
            log.release_close.set()
        assert _wait_until(lambda: log.closed), variant
        assert log.close_calls == 1, variant


def _shutdown_harness(dump_file, watchdog, closer):
    """Minimal fake window for the real PlayerWindow._request_shutdown and
    _cancel_stall_dump, recording the playback/worker teardown it reaches."""
    from types import SimpleNamespace
    from billsmusic.window import PlayerWindow
    from billsmusic.worker_registry import WorkerLifetimeRegistry

    steps = []
    registry = WorkerLifetimeRegistry()
    registry.shutdown_all = lambda: steps.append("worker_shutdown")
    window = SimpleNamespace(
        _shutdown_requested=False, _closing=False, _shutdown_pending=False,
        _playback_generation=0, _plex_audio_load_token=0, _crossfade_load_token=0,
        _karaoke_generation=0, _library_search_generation=0, _playback_recovery_active=False,
        _worker_registry=registry,
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None),
        _cancel_current_playback_attempt=lambda reason: None,
        _cancel_playback_watchdog=lambda: None,
        _audio_log=lambda message: None,
        _cancel_pending_library_apply=lambda: None,
        _cancel_fade=lambda: None,
        _stop_all=lambda: steps.append("audio_stop"),
        _cancel_metadata_backfill=lambda: None,
        _mixed_transition_state="idle",
        _video_backend=SimpleNamespace(stop=lambda: steps.append("video_stop")),
        _uninstall_gc_pause_probe=lambda: None,
        _fault_dump_file=dump_file,
        _stall_watchdog=watchdog,
        _diagnostic_closer=closer,
    )
    window._cancel_stall_dump = lambda: PlayerWindow._cancel_stall_dump(window)
    return window, steps


# ---------------------------------------------------------------------------
# Phase 1.4: shutdown never needs to create an execution resource
# ---------------------------------------------------------------------------

class _BlockingRawOutput(io.RawIOBase):
    """Raw output for a real TextIOWrapper/BufferedWriter whose write() and
    close() park until released, recording which thread entered them."""

    def __init__(self):
        super().__init__()
        self.release = threading.Event()
        self.entered = threading.Event()
        self.entered_on = []

    def writable(self):
        return True

    def _park(self, what):
        self.entered_on.append((what, threading.current_thread().name))
        self.entered.set()
        self.release.wait()

    def write(self, b):
        self._park("write")
        return len(b)

    def close(self):
        if not self.closed:
            self._park("close")
        super().close()


def test_no_thread_can_start_at_shutdown_yet_the_stall_log_is_never_finalised_there(tmp_path, monkeypatch):
    """Astra regression: shutdown used to need a brand-new closer thread. When
    Thread.start() failed, _cancel_stall_dump dropped the last reference to
    the stall log and CPython finalised the buffered TextIOWrapper right
    there -- a flush into blocked output on the shutdown thread, before audio
    stop, video stop and worker shutdown. Diagnostics start normally here;
    only from shutdown on can no thread be started."""
    import gc
    import weakref
    import billsmusic.stall_watchdog as stall_watchdog_module
    import billsmusic.window as window_module
    from types import SimpleNamespace
    from billsmusic.window import PlayerWindow

    for variant in ("watchdog_already_terminated", "no_watchdog"):
        raw = _BlockingRawOutput()
        raw.blocking = False
        real_park = raw._park
        raw._park = lambda what, raw=raw, real_park=real_park: real_park(what) if raw.blocking else None
        log_refs = []

        def _open_buffered_log(path, mode="r", encoding=None, raw=raw, log_refs=log_refs):
            log = io.TextIOWrapper(io.BufferedWriter(raw, buffer_size=1 << 20), encoding="utf-8")
            log_refs.append(weakref.ref(log))
            return log

        monkeypatch.setattr(window_module, "open", _open_buffered_log, raising=False)
        closer = None
        if hasattr(stall_watchdog_module, "DiagnosticFileCloser"):
            closer = _private_closer()
            monkeypatch.setattr(window_module, "ensure_diagnostic_closer", lambda closer=closer: closer)
        window, steps = _shutdown_harness(None, None, None)
        window.diagnostics = SimpleNamespace(record=lambda *a, **kw: None, directory=str(tmp_path))
        window._fault_dump_path = lambda window=window: PlayerWindow._fault_dump_path(window)
        PlayerWindow._arm_stall_dump(window)  # normal start-up
        watchdog = window._stall_watchdog
        assert watchdog is not None and len(log_refs) == 1
        assert watchdog.stop(close_file=False) is True
        if variant == "no_watchdog":
            window._stall_watchdog = None
        del watchdog
        # A dump still sitting in the text/buffered layers, and output that
        # now blocks: any close or finalisation of this log will block.
        window._fault_dump_file.write("Timeout (0:00:00.150000)!\nunflushed stall dump\n")
        raw.blocking = True
        gc.collect()
        try:
            with _NoThreadStarts() as starts:
                done, result = _stop_in_background(lambda window=window: PlayerWindow._request_shutdown(window))
                finished = done.wait(3.0)
                blocked_on = list(raw.entered_on)
            assert finished, (
                f"{variant}: shutdown blocked finalising the stall log; flush/close ran on {blocked_on}"
            )
            assert steps == ["audio_stop", "video_stop", "worker_shutdown"], variant
            assert all(name != "shutdown-caller" for _what, name in raw.entered_on), raw.entered_on
            if closer is not None:
                assert raw.entered.wait(5.0)
                assert raw.entered_on[0][1] == closer.thread.name, raw.entered_on
            assert starts.refused == [], starts.refused
        finally:
            raw.release.set()
        assert _wait_until(lambda: log_refs[0]() is None or log_refs[0]().closed), variant


def test_closer_unavailable_at_startup_disables_stall_dumps_so_no_log_is_owned(tmp_path, monkeypatch):
    """Start-up counterpart: if the closer service itself cannot be started,
    stall dumps are disabled for the run and no log is ever opened, so no
    later shutdown can have one to finalise."""
    import weakref
    import billsmusic.stall_watchdog as stall_watchdog_module
    import billsmusic.window as window_module
    from types import SimpleNamespace
    from billsmusic.window import PlayerWindow

    raw = _BlockingRawOutput()
    opened = []

    def _open_buffered_log(path, mode="r", encoding=None):
        log = io.TextIOWrapper(io.BufferedWriter(raw, buffer_size=1 << 20), encoding="utf-8")
        log.write("Timeout (0:00:00.150000)!\nbuffered dump that never reached the output\n")
        opened.append(weakref.ref(log))  # never a strong reference here
        return log

    monkeypatch.setattr(window_module, "open", _open_buffered_log, raising=False)
    monkeypatch.setattr(stall_watchdog_module, "_default_closer", None, raising=False)
    window, steps = _shutdown_harness(None, None, None)
    window.diagnostics = SimpleNamespace(record=lambda *a, **kw: None, directory=str(tmp_path))
    window._fault_dump_path = lambda: PlayerWindow._fault_dump_path(window)
    del window._diagnostic_closer
    try:
        with _NoThreadStarts() as starts:  # the process cannot start threads
            for _ in range(3):  # event-loop probe ticks
                PlayerWindow._arm_stall_dump(window)
            done, result = _stop_in_background(lambda: PlayerWindow._request_shutdown(window))
            assert done.wait(3.0), "shutdown blocked finalising the stall log on the shutdown thread"
        assert steps == ["audio_stop", "video_stop", "worker_shutdown"]
        assert window._fault_dump_file is None and window._stall_watchdog is None
        assert getattr(window, "_stall_dump_disabled", False) is True
        assert opened == []  # no diagnostic log was ever opened
        assert not raw.entered.is_set()  # nothing flushed or closed on any thread
    finally:
        raw.release.set()


def test_request_shutdown_starts_no_thread_once_stall_diagnostics_are_running(tmp_path, monkeypatch):
    """Architecture guard: after normal diagnostic start-up, no path through
    _request_shutdown's watchdog/log teardown calls Thread.start()."""
    import billsmusic.window as window_module
    from types import SimpleNamespace
    from billsmusic.window import PlayerWindow

    import billsmusic.stall_watchdog as stall_watchdog_module

    for variant in ("watchdog_running", "watchdog_already_terminated", "no_watchdog"):
        if hasattr(stall_watchdog_module, "DiagnosticFileCloser"):
            closer = _private_closer()
            monkeypatch.setattr(window_module, "ensure_diagnostic_closer", lambda closer=closer: closer)
        directory = tmp_path / variant
        directory.mkdir()
        window, steps = _shutdown_harness(None, None, None)
        window.diagnostics = SimpleNamespace(record=lambda *a, **kw: None, directory=str(directory))
        window._fault_dump_path = lambda window=window: PlayerWindow._fault_dump_path(window)
        PlayerWindow._arm_stall_dump(window)  # normal start-up: closer, file, watchdog
        dump_file, watchdog = window._fault_dump_file, window._stall_watchdog
        assert dump_file is not None and watchdog is not None and watchdog.is_alive()
        if variant != "watchdog_running":
            assert watchdog.stop(close_file=False) is True
        if variant == "no_watchdog":
            window._stall_watchdog = None
        starts = []
        real_start = threading.Thread.start
        monkeypatch.setattr(
            threading.Thread, "start",
            lambda thread: (starts.append(thread.name), real_start(thread))[1],
        )
        PlayerWindow._request_shutdown(window)
        monkeypatch.setattr(threading.Thread, "start", real_start)
        assert starts == [], f"{variant}: shutdown started {starts}"
        assert steps == ["audio_stop", "video_stop", "worker_shutdown"], variant
        assert _wait_until(lambda: dump_file.closed), variant


def test_watchdog_that_cannot_start_hands_its_opened_log_to_the_closer(tmp_path, monkeypatch):
    import billsmusic.window as window_module
    from types import SimpleNamespace
    from billsmusic.window import PlayerWindow

    closer = _private_closer()
    monkeypatch.setattr(window_module, "ensure_diagnostic_closer", lambda: closer)

    def _cannot_start(*a, **kw):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(window_module, "StallTracebackWatchdog", _cannot_start)
    harness = SimpleNamespace(
        diagnostics=SimpleNamespace(directory=str(tmp_path)),
        _fault_dump_file=None, _stall_watchdog=None,
    )
    harness._fault_dump_path = lambda: PlayerWindow._fault_dump_path(harness)
    PlayerWindow._arm_stall_dump(harness)
    assert harness._fault_dump_file is None and harness._stall_watchdog is None
    assert harness._stall_dump_disabled is True
    assert _wait_until(lambda: closer.closed_count == 1)  # closed by the service, not here
    PlayerWindow._arm_stall_dump(harness)  # disabled: not retried every probe tick
    assert harness._fault_dump_file is None and closer.closed_count == 1


def test_diagnostic_closer_lifecycle(tmp_path, monkeypatch):
    """Documented contract of the closer service: started once, as a daemon,
    during diagnostic start-up (never by cancel/shutdown); submit() is a
    non-blocking put that keeps the file referenced until closed; a close()
    exception does not kill the service; a blocked close delays only later
    closes, whose files stay referenced."""
    import gc
    import weakref
    import billsmusic.stall_watchdog as stall_watchdog_module
    from types import SimpleNamespace
    from billsmusic.window import PlayerWindow

    monkeypatch.setattr(stall_watchdog_module, "_default_closer", None)
    harness = SimpleNamespace(
        diagnostics=SimpleNamespace(directory=str(tmp_path)),
        _fault_dump_file=None, _stall_watchdog=None,
    )
    harness._fault_dump_path = lambda: PlayerWindow._fault_dump_path(harness)
    PlayerWindow._arm_stall_dump(harness)
    service = stall_watchdog_module._default_closer
    assert service is not None and harness._diagnostic_closer is service
    assert service.thread.daemon and service.is_alive()
    assert stall_watchdog_module.ensure_diagnostic_closer() is service  # one per process
    PlayerWindow._cancel_stall_dump(harness)
    PlayerWindow._arm_stall_dump(harness)
    assert stall_watchdog_module._default_closer is service  # not recreated by cancel/re-arm
    PlayerWindow._cancel_stall_dump(harness)

    closer = _private_closer()
    blocked = _BlockingCloseLog()

    class _ExplodingLog(_RecordingLog):
        def close(self):
            super().close()
            raise OSError("close failed")

    later_closes = []  # recorded outside the object, which is freed once closed

    class _LaterLog(_RecordingLog):
        def close(self):
            super().close()
            later_closes.append(threading.current_thread().name)

    exploding = _ExplodingLog()
    later = _LaterLog()
    later_ref = weakref.ref(later)
    try:
        started = time.monotonic()
        closer.submit(blocked)
        assert blocked.close_entered.wait(5.0)  # the service is stuck in this close
        closer.submit(exploding)
        closer.submit(later)
        assert time.monotonic() - started < 1.0  # submit never waits on a blocked close
        del later
        gc.collect()
        assert later_ref() is not None  # queued file still strongly referenced
        assert later_closes == []
    finally:
        blocked.release_close.set()
    assert _wait_until(lambda: len(later_closes) == 1)
    assert later_closes == [closer.thread.name]
    assert blocked.close_calls == 1 and exploding.close_calls == 1
    assert closer.failed_count == 1 and closer.closed_count == 2
    assert closer.is_alive()  # an exception in one close does not kill the service
    # ... and the service lets go of a file once its close has run.
    assert _wait_until(lambda: (gc.collect(), later_ref() is None)[1])


_CHURN_SCRIPT = textwrap.dedent(
    r"""
    import sys, threading, time
    from types import SimpleNamespace
    from billsmusic import window as window_module
    from billsmusic.window import PlayerWindow

    diagnostics_dir, duration = sys.argv[1], float(sys.argv[2])
    harness = SimpleNamespace(
        diagnostics=SimpleNamespace(directory=diagnostics_dir),
        _fault_dump_file=None, _stall_watchdog=None,
    )
    harness._fault_dump_path = lambda: PlayerWindow._fault_dump_path(harness)

    stop = False
    def recurse(n):
        # Deep enough to spill across CPython's per-thread frame-stack
        # chunks, so returning frees memory a GIL-free walker may be reading.
        return recurse(n - 1) if n else len([i for i in range(20)])
    def worker():
        while not stop:
            recurse(300)
            time.sleep(0.0005)
    for _ in range(6):
        threading.Thread(target=worker, daemon=True).start()

    busy_s = window_module.STALL_DUMP_THRESHOLD_S + 0.05
    end = time.monotonic() + duration
    stalls = 0
    while time.monotonic() < end:
        PlayerWindow._arm_stall_dump(harness)  # the event-loop probe tick
        busy_end = time.perf_counter() + busy_s  # a GUI thread busy in Python
        while time.perf_counter() < busy_end:
            recurse(300)
        stalls += 1
    PlayerWindow._cancel_stall_dump(harness)
    stop = True
    print("STALLS", stalls)
    """
)


def test_stall_dump_survives_a_busy_gui_thread_with_running_python_workers(tmp_path):
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, PYTHONPATH=repo_root + os.pathsep + os.environ.get("PYTHONPATH", ""))
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    result = subprocess.run(
        [sys.executable, "-c", _CHURN_SCRIPT, str(tmp_path), "6"],
        cwd=repo_root, env=env, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, (
        f"stall watchdog crashed the process (exit {result.returncode}):\n"
        f"{result.stdout[-2000:]}\n{result.stderr[-4000:]}"
    )
    assert "STALLS" in result.stdout
    # The diagnostic still did its job: stalls were captured.
    dump = (tmp_path / "stall_traceback.log").read_text(encoding="utf-8")
    assert "Timeout (" in dump
    assert "in recurse" in dump
