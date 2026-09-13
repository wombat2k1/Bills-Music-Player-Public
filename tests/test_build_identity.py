"""Immutable build identity (2026-08-31 Codex audit, section 16):
_record_build_identity() must report the actual git commit/tree/build-UUID
a frozen build was compiled from (via billsmusic/_build_info.py, generated
at build time -- see generate_build_info.py), and must fall back to a
bounded, best-effort live `git rev-parse HEAD` only for a source run that
has never had the build script run against it -- never for a frozen build.
"""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from billsmusic.window import PlayerWindow


def _window(**overrides):
    diagnostics_calls = []
    window = SimpleNamespace(
        diagnostics=SimpleNamespace(
            record=lambda *a, **kw: diagnostics_calls.append((a, kw)),
        ),
    )
    window.diagnostics_calls = diagnostics_calls
    window._record_build_identity = PlayerWindow._record_build_identity.__get__(window)
    # _live_source_git_commit is a @staticmethod (takes no self) -- assign
    # the plain function directly, not via __get__(window), which would
    # incorrectly bind window as its first (nonexistent) parameter.
    window._live_source_git_commit = PlayerWindow._live_source_git_commit
    for key, value in overrides.items():
        setattr(window, key, value)
    return window


def _recorded_details(window):
    for args, kwargs in window.diagnostics_calls:
        if args[1] == "build_identity":
            return kwargs["details"]
    raise AssertionError("build_identity was never recorded")


def test_frozen_build_reports_build_info_fields_verbatim(monkeypatch):
    import billsmusic.window as window_module

    fake_build_info = SimpleNamespace(
        BUILD_COMMIT="abc123", BUILD_COMMIT_DIRTY=False,
        BUILD_TREE_HASH="tree456", BUILD_UUID="uuid-789",
        BUILD_TIMESTAMP_UTC="2026-08-31T00:00:00+00:00",
    )
    monkeypatch.setattr(window_module, "_build_info", fake_build_info)
    monkeypatch.setattr(window_module, "is_frozen_build", lambda: True)
    monkeypatch.setattr(window_module, "current_executable_path", lambda: __file__)

    window = _window()
    live_calls = []
    window._live_source_git_commit = lambda: live_calls.append(True) or "should-never-be-called"

    window._record_build_identity()

    details = _recorded_details(window)
    assert details["build_commit"] == "abc123"
    assert details["build_commit_dirty"] is False
    assert details["build_tree_hash"] == "tree456"
    assert details["build_uuid"] == "uuid-789"
    assert details["build_timestamp_utc"] == "2026-08-31T00:00:00+00:00"
    assert details["frozen"] is True
    # The whole point of this fix: a frozen build must never fall back to
    # a live git query, regardless of whether _build_info looks populated.
    assert "source_run_git_commit" not in details
    assert live_calls == []


def test_source_run_with_populated_build_info_does_not_query_git_live(monkeypatch):
    import billsmusic.window as window_module

    fake_build_info = SimpleNamespace(
        BUILD_COMMIT="abc123", BUILD_COMMIT_DIRTY=False,
        BUILD_TREE_HASH="tree456", BUILD_UUID="uuid-789",
        BUILD_TIMESTAMP_UTC="2026-08-31T00:00:00+00:00",
    )
    monkeypatch.setattr(window_module, "_build_info", fake_build_info)
    monkeypatch.setattr(window_module, "is_frozen_build", lambda: False)
    monkeypatch.setattr(window_module, "current_executable_path", lambda: __file__)

    window = _window()
    live_calls = []
    window._live_source_git_commit = lambda: live_calls.append(True) or "unused"

    window._record_build_identity()

    details = _recorded_details(window)
    assert details["build_commit"] == "abc123"
    assert "source_run_git_commit" not in details
    assert live_calls == []


def test_source_run_with_unpopulated_build_info_falls_back_to_live_git(monkeypatch):
    import billsmusic.window as window_module

    unpopulated = SimpleNamespace(
        BUILD_COMMIT=None, BUILD_COMMIT_DIRTY=None,
        BUILD_TREE_HASH=None, BUILD_UUID=None, BUILD_TIMESTAMP_UTC=None,
    )
    monkeypatch.setattr(window_module, "_build_info", unpopulated)
    monkeypatch.setattr(window_module, "is_frozen_build", lambda: False)
    monkeypatch.setattr(window_module, "current_executable_path", lambda: __file__)

    window = _window()
    window._live_source_git_commit = lambda: "live-commit-hash"

    window._record_build_identity()

    details = _recorded_details(window)
    assert details["build_commit"] is None
    assert details["source_run_git_commit"] == "live-commit-hash"


def test_live_source_git_commit_never_raises_when_git_unavailable(monkeypatch):
    import subprocess

    def _boom(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(subprocess, "run", _boom)
    window = _window()

    assert window._live_source_git_commit() is None


def test_live_source_git_commit_returns_none_on_nonzero_exit(monkeypatch):
    import subprocess

    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: SimpleNamespace(returncode=128, stdout=""),
    )
    window = _window()

    assert window._live_source_git_commit() is None


def test_live_source_git_commit_strips_and_returns_real_output(monkeypatch):
    import subprocess

    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="deadbeef1234\n"),
    )
    window = _window()

    assert window._live_source_git_commit() == "deadbeef1234"
