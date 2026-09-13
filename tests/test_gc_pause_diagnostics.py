"""Tests for the GC-pause diagnostic probe added to confirm or rule out
Python's own garbage collector as the source of the periodic ~250ms GUI
freezes reported in Party Mode. Pure-Python logic (gc.callbacks + timing),
so these use the "SimpleNamespace fake + real unbound PlayerWindow methods"
convention already established across this suite (see test_party_mode.py)
rather than a real Qt window.
"""
import gc
from types import SimpleNamespace

import pytest

from billsmusic.window import PlayerWindow


@pytest.fixture(autouse=True)
def _isolate_gc_callbacks():
    # gc.callbacks is real global process state -- restore it after every
    # test so a fake harness's probe never leaks into an unrelated test.
    before = list(gc.callbacks)
    yield
    gc.callbacks[:] = before


class _RecordingDiagnostics:
    def __init__(self):
        self.events = []

    def record(self, category, operation, **kwargs):
        self.events.append((category, operation, kwargs))

    def events_for(self, operation):
        return [e for e in self.events if e[1] == operation]


def _make_harness():
    harness = SimpleNamespace(diagnostics=_RecordingDiagnostics(), _gc_pause_started=None)
    harness._install_gc_pause_probe = lambda: PlayerWindow._install_gc_pause_probe(harness)
    harness._uninstall_gc_pause_probe = lambda: PlayerWindow._uninstall_gc_pause_probe(harness)
    harness._gc_pause_probe = lambda phase, info: PlayerWindow._gc_pause_probe(harness, phase, info)
    return harness


def test_install_is_idempotent():
    harness = _make_harness()
    harness._install_gc_pause_probe()
    harness._install_gc_pause_probe()
    assert gc.callbacks.count(harness._gc_pause_probe) == 1


def test_uninstall_without_install_does_not_raise():
    harness = _make_harness()
    harness._uninstall_gc_pause_probe()


def test_uninstall_removes_from_gc_callbacks():
    harness = _make_harness()
    harness._install_gc_pause_probe()
    harness._uninstall_gc_pause_probe()
    assert harness._gc_pause_probe not in gc.callbacks


def test_short_pause_is_not_recorded(monkeypatch):
    harness = _make_harness()
    times = iter([0.0, 0.001])  # 1ms elapsed, below the 5ms floor
    monkeypatch.setattr("billsmusic.window.time.perf_counter", lambda: next(times))
    harness._gc_pause_probe("start", {})
    harness._gc_pause_probe("stop", {"generation": 0, "collected": 10, "uncollectable": 0})
    assert harness.diagnostics.events_for("gc_pause") == []


def test_slow_pause_is_recorded_as_warning(monkeypatch):
    harness = _make_harness()
    times = iter([0.0, 0.06])  # 60ms elapsed
    monkeypatch.setattr("billsmusic.window.time.perf_counter", lambda: next(times))
    harness._gc_pause_probe("start", {})
    harness._gc_pause_probe("stop", {"generation": 2, "collected": 500, "uncollectable": 0})
    events = harness.diagnostics.events_for("gc_pause")
    assert len(events) == 1
    category, operation, kwargs = events[0]
    assert category == "runtime"
    assert kwargs["severity"] == "warning"
    assert kwargs["details"]["generation"] == 2


def test_very_slow_pause_is_recorded_as_critical(monkeypatch):
    harness = _make_harness()
    times = iter([0.0, 0.6])  # 600ms elapsed
    monkeypatch.setattr("billsmusic.window.time.perf_counter", lambda: next(times))
    harness._gc_pause_probe("start", {})
    harness._gc_pause_probe("stop", {"generation": 2, "collected": 1000, "uncollectable": 0})
    events = harness.diagnostics.events_for("gc_pause")
    assert events[0][2]["severity"] == "critical"


def test_stop_without_matching_start_is_ignored():
    harness = _make_harness()
    harness._gc_pause_probe("stop", {"generation": 0, "collected": 0, "uncollectable": 0})
    assert harness.diagnostics.events_for("gc_pause") == []
