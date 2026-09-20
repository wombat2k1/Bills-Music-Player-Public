"""Tests for the stall-dump watchdog added to pin down the periodic
~250-300ms Party Mode GUI freezes -- sync_from_owner timing and a GC-pause
probe have both already ruled themselves out as the cause, so every
event-loop probe tick heartbeats a StallTracebackWatchdog that captures
every thread's live stack if the GUI thread ever misses two consecutive
ticks. Uses the "SimpleNamespace fake + real unbound PlayerWindow methods"
convention (see test_party_mode.py) with the watchdog class itself replaced
by a recording fake; the real watchdog's own behaviour (including why it is
no longer faulthandler.dump_traceback_later) is covered by
test_stall_watchdog.py.
"""
import faulthandler
from types import SimpleNamespace

from billsmusic import window as window_module
from billsmusic.window import PlayerWindow


class _FakeWatchdog:
    def __init__(self, calls, threshold_s, file):
        self.calls = calls
        self.file = file
        calls.append(("create", threshold_s, file))

    def heartbeat(self):
        self.calls.append("heartbeat")

    def stop(self, timeout=1.0, close_file=False):
        # Mirrors the real contract: close_file hands closing to the watchdog.
        self.calls.append(("stop", self.file.closed, close_file))
        if close_file:
            self.file.close()
        return True


def _install_fake_watchdog(monkeypatch, calls):
    monkeypatch.setattr(
        window_module, "StallTracebackWatchdog",
        lambda threshold_s, file, closer=None: _FakeWatchdog(calls, threshold_s, file),
    )


def _make_harness(tmp_path, diagnostics_directory="present"):
    diagnostics = SimpleNamespace(directory=str(tmp_path)) if diagnostics_directory else SimpleNamespace()
    harness = SimpleNamespace(diagnostics=diagnostics, _fault_dump_file=None, _stall_watchdog=None)
    harness._fault_dump_path = lambda: PlayerWindow._fault_dump_path(harness)
    harness._arm_stall_dump = lambda: PlayerWindow._arm_stall_dump(harness)
    harness._cancel_stall_dump = lambda: PlayerWindow._cancel_stall_dump(harness)
    return harness


def test_fault_dump_path_is_none_without_a_diagnostics_directory():
    harness = SimpleNamespace(diagnostics=SimpleNamespace())
    harness._fault_dump_path = lambda: PlayerWindow._fault_dump_path(harness)
    assert harness._fault_dump_path() is None


def test_fault_dump_path_lives_under_the_diagnostics_directory(tmp_path):
    harness = _make_harness(tmp_path)
    path = harness._fault_dump_path()
    assert path is not None
    assert path.parent == tmp_path
    assert path.name == "stall_traceback.log"


def test_arm_stall_dump_opens_the_file_once_and_rearms_the_watchdog(tmp_path, monkeypatch):
    harness = _make_harness(tmp_path)
    calls = []
    _install_fake_watchdog(monkeypatch, calls)

    harness._arm_stall_dump()
    first_file = harness._fault_dump_file
    first_watchdog = harness._stall_watchdog
    assert first_file is not None
    assert calls == [("create", window_module.STALL_DUMP_THRESHOLD_S, first_file), "heartbeat"]

    calls.clear()
    harness._arm_stall_dump()
    assert harness._fault_dump_file is first_file  # reused, not reopened
    assert harness._stall_watchdog is first_watchdog  # one watchdog, re-armed
    assert calls == ["heartbeat"]

    harness._cancel_stall_dump()


def test_arm_stall_dump_is_a_no_op_without_a_diagnostics_directory(monkeypatch):
    harness = SimpleNamespace(diagnostics=SimpleNamespace(), _fault_dump_file=None, _stall_watchdog=None)
    harness._fault_dump_path = lambda: PlayerWindow._fault_dump_path(harness)
    harness._arm_stall_dump = lambda: PlayerWindow._arm_stall_dump(harness)
    calls = []
    _install_fake_watchdog(monkeypatch, calls)
    harness._arm_stall_dump()
    assert calls == []
    assert harness._fault_dump_file is None
    assert harness._stall_watchdog is None


def test_cancel_stall_dump_stops_the_watchdog_before_closing_the_file(tmp_path, monkeypatch):
    harness = _make_harness(tmp_path)
    calls = []
    _install_fake_watchdog(monkeypatch, calls)
    harness._arm_stall_dump()
    opened_file = harness._fault_dump_file
    assert opened_file is not None

    calls.clear()
    harness._cancel_stall_dump()
    # stop() observed the file still open and was handed ownership of
    # closing it: the window never closes a file the watchdog may be writing.
    assert calls == [("stop", False, True)]
    assert harness._stall_watchdog is None
    assert harness._fault_dump_file is None
    assert opened_file.closed


def test_cancel_stall_dump_without_arming_does_not_raise():
    harness = SimpleNamespace(diagnostics=SimpleNamespace(), _fault_dump_file=None, _stall_watchdog=None)
    harness._cancel_stall_dump = lambda: PlayerWindow._cancel_stall_dump(harness)
    harness._cancel_stall_dump()  # should not raise


def test_stall_dump_never_touches_the_process_global_faulthandler_timer(tmp_path, monkeypatch):
    """Regression: the watchdog used to be faulthandler.dump_traceback_later,
    whose GIL-free stack walk crashed the process (native access violation)
    whenever the GUI thread was busy running Python past the threshold, and
    whose cancel_dump_traceback_later() silently disarmed any other user of
    that single process-wide timer (e.g. pytest's faulthandler_timeout)."""
    harness = _make_harness(tmp_path)
    calls = []
    monkeypatch.setattr(faulthandler, "dump_traceback_later", lambda *a, **k: calls.append("arm"))
    monkeypatch.setattr(faulthandler, "cancel_dump_traceback_later", lambda *a, **k: calls.append("cancel"))
    harness._arm_stall_dump()
    harness._arm_stall_dump()
    watchdog = harness._stall_watchdog
    assert watchdog is not None and watchdog.is_alive()
    harness._cancel_stall_dump()
    assert calls == []
    assert not watchdog.is_alive()
