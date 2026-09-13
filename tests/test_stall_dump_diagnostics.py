"""Tests for the faulthandler-based stall-dump watchdog added to pin down
the periodic ~250-300ms Party Mode GUI freezes -- sync_from_owner timing
and a GC-pause probe have both already ruled themselves out as the cause,
so this arms faulthandler.dump_traceback_later() on every event-loop probe
tick to capture every thread's live stack if the GUI thread ever misses two
consecutive ticks. Uses the "SimpleNamespace fake + real unbound
PlayerWindow methods" convention (see test_party_mode.py) with
faulthandler's own arm/cancel calls monkeypatched out, since the real
mechanism needs a genuine file descriptor and wall-clock stall to observe
(verified manually, not worth the flakiness in a unit test).
"""
from types import SimpleNamespace

from billsmusic import window as window_module
from billsmusic.window import PlayerWindow


def _make_harness(tmp_path, diagnostics_directory="present"):
    diagnostics = SimpleNamespace(directory=str(tmp_path)) if diagnostics_directory else SimpleNamespace()
    harness = SimpleNamespace(diagnostics=diagnostics, _fault_dump_file=None)
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
    monkeypatch.setattr(window_module.faulthandler, "cancel_dump_traceback_later", lambda: calls.append("cancel"))
    monkeypatch.setattr(
        window_module.faulthandler, "dump_traceback_later",
        lambda timeout, exit, file: calls.append(("arm", timeout, exit)),
    )

    harness._arm_stall_dump()
    first_file = harness._fault_dump_file
    assert first_file is not None
    assert calls == ["cancel", ("arm", window_module.STALL_DUMP_THRESHOLD_S, False)]

    calls.clear()
    harness._arm_stall_dump()
    assert harness._fault_dump_file is first_file  # reused, not reopened
    assert calls == ["cancel", ("arm", window_module.STALL_DUMP_THRESHOLD_S, False)]

    harness._cancel_stall_dump()


def test_arm_stall_dump_is_a_no_op_without_a_diagnostics_directory(monkeypatch):
    harness = SimpleNamespace(diagnostics=SimpleNamespace(), _fault_dump_file=None)
    harness._fault_dump_path = lambda: PlayerWindow._fault_dump_path(harness)
    harness._arm_stall_dump = lambda: PlayerWindow._arm_stall_dump(harness)
    calls = []
    monkeypatch.setattr(window_module.faulthandler, "cancel_dump_traceback_later", lambda: calls.append("cancel"))
    monkeypatch.setattr(
        window_module.faulthandler, "dump_traceback_later",
        lambda timeout, exit, file: calls.append("arm"),
    )
    harness._arm_stall_dump()
    assert calls == []
    assert harness._fault_dump_file is None


def test_cancel_stall_dump_cancels_and_closes_the_file(tmp_path, monkeypatch):
    harness = _make_harness(tmp_path)
    monkeypatch.setattr(window_module.faulthandler, "cancel_dump_traceback_later", lambda: None)
    monkeypatch.setattr(window_module.faulthandler, "dump_traceback_later", lambda timeout, exit, file: None)
    harness._arm_stall_dump()
    opened_file = harness._fault_dump_file
    assert opened_file is not None

    cancel_calls = []
    monkeypatch.setattr(window_module.faulthandler, "cancel_dump_traceback_later", lambda: cancel_calls.append(1))
    harness._cancel_stall_dump()
    assert cancel_calls == [1]
    assert harness._fault_dump_file is None
    assert opened_file.closed


def test_cancel_stall_dump_without_arming_does_not_raise(monkeypatch):
    harness = SimpleNamespace(diagnostics=SimpleNamespace(), _fault_dump_file=None)
    harness._cancel_stall_dump = lambda: PlayerWindow._cancel_stall_dump(harness)
    monkeypatch.setattr(window_module.faulthandler, "cancel_dump_traceback_later", lambda: None)
    harness._cancel_stall_dump()  # should not raise
