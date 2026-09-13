"""Regression coverage for the video child process launch command.

Reported: the packaged (Nuitka) build showed "video player stopped
unexpectedly" when starting a video. Two separate, stacked root causes:

1. _subprocess_launch_command() gated on `sys.frozen` alone, which Nuitka
   does not set for arbitrary application code (confirmed via Nuitka's own
   package config -- see platform_utils.is_frozen_build()'s docstring). In
   a packaged build this silently launched a second full copy of the app
   ("-m billsmusic.video_subprocess" as argv, which Main.py's
   "--video-subprocess" check never matches) instead of the actual video
   subprocess entry point.

2. Fixing (1) alone still didn't fix the live report: `sys.executable`
   itself is unreliable in this Nuitka build. It reports Nuitka's internal
   default name ("...\\Main.dist\\python.exe") rather than the renamed
   output binary configured via `--output-filename="Bills Music
   Player.exe"` -- a file that does not exist under that name (confirmed
   via player.log's unredacted python_executable= field, and by checking
   the dist folder directly). Passing that nonexistent path to
   QProcess.setProgram() fails with Windows error 2 ("The system cannot
   find the file specified") before the child process even starts --
   which looks identical to a crash from the caller's side.

Either failure surfaces identically to the user as "video player stopped
unexpectedly", and to _subprocess_launch_command()'s two callers (a fresh
launch and every crash-triggered restart).
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from billsmusic import platform_utils
from billsmusic.video_backend import _subprocess_launch_command


def test_launch_command_uses_module_invocation_when_not_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    program, args = _subprocess_launch_command()
    # Not frozen -- current_executable_path() must not touch
    # GetModuleFileNameW at all here, just trust sys.executable.
    assert program == sys.executable
    assert args == ["-m", "billsmusic.video_subprocess"]


def test_launch_command_uses_flag_when_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    try:
        program, args = _subprocess_launch_command()
        # Must be a real, existing file -- this is the actual guarantee
        # that matters (root cause 2 above). It is deliberately NOT
        # asserted to equal sys.executable: forcing the frozen branch
        # while genuinely running under a dev venv makes
        # current_executable_path() consult GetModuleFileNameW for real,
        # which can legitimately differ from sys.executable for a venv's
        # own python.exe launcher/stub -- that's a correct, expected
        # difference in this artificial test setup, not the bug.
        assert os.path.isfile(program)
        # Must match exactly what Main.py's own argv check looks for --
        # "-m billsmusic.video_subprocess" would silently relaunch the
        # whole app instead (see module docstring above).
        assert args == ["--video-subprocess"]
    finally:
        monkeypatch.setattr(sys, "frozen", False, raising=False)


def test_current_executable_path_trusts_sys_executable_when_not_frozen():
    assert platform_utils.current_executable_path() == sys.executable


def test_current_executable_path_always_returns_an_existing_file_when_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    try:
        path = platform_utils.current_executable_path()
        # The concrete guarantee this function exists for: whatever it
        # returns is a real file that can actually be launched -- unlike
        # sys.executable in this Nuitka build, which pointed at a filename
        # ("python.exe") that plainly does not exist on disk.
        assert os.path.isfile(path)
    finally:
        monkeypatch.setattr(sys, "frozen", False, raising=False)


def test_is_frozen_build_true_via_sys_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    try:
        assert platform_utils.is_frozen_build() is True
    finally:
        monkeypatch.setattr(sys, "frozen", False, raising=False)


def test_is_frozen_build_true_via_compiled_global(monkeypatch):
    # Simulates what Nuitka actually injects (a per-module __compiled__
    # global) -- sys.frozen is deliberately left unset here, matching a
    # real Nuitka standalone build where it is never set for app code.
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setitem(platform_utils.__dict__, "__compiled__", object())
    assert platform_utils.is_frozen_build() is True


def test_is_frozen_build_false_from_source():
    assert platform_utils.is_frozen_build() is False
