"""Phase 8: a video child process must never outlive its parent.

The parent asks the child to exit by sending {"cmd": "shutdown"} and then
closing its end of the pipe (video_backend._stop_process_gracefully), but
that is not the only way the parent goes away: the Stage 2 shutdown
grace-expiry path calls os._exit(0) deliberately (see window.py's
_force_process_exit -- a worker that could not be proven finished may still
be inside a native call, so nothing is torn down), and a parent crash does
the same thing involuntarily. In both cases the child gets no command at
all: it only sees EOF on stdin, quits its event loop, and drops a
QMediaPlayer that is still playing.

That last step is the dangerous one. A Python-owned QMediaPlayer destroyed
while its FFmpeg session is live can deadlock in ~QMediaPlayer -- it blocks
on the media thread, which is itself blocked acquiring the GIL to destroy a
PyQt-wrapped object (native stacks of both halves confirmed this, and it is
the same mechanism tests/video_controller_teardown.py exists for). A child
that deadlocks there does not die with its parent: it survives as an orphan
still holding the audio endpoint, with no UI left to stop it.

So this pins the guarantee itself, through the real child process: EOF
alone, while video is genuinely playing, must end the process promptly.
"""
import json
import os
import subprocess
import sys
import time

import pytest

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample.mp4")
_EXIT_BUDGET_S = 20.0


def _spawn(mode_args, tmp_path):
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(
        os.environ,
        LOCALAPPDATA=str(tmp_path),
        QT_QPA_PLATFORM="offscreen",
        PYTHONUNBUFFERED="1",
    )
    return subprocess.Popen(
        [sys.executable, "-m", "billsmusic.video_subprocess", *mode_args],
        cwd=repo_root, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )


def _wait_event(child, name, seconds=20.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        line = child.stdout.readline()
        if not line:
            return None
        try:
            event = json.loads(line.decode().strip())
        except ValueError:
            continue
        if event.get("event") == name:
            return event
    return None


def _play_fixture(child, mode_args=()):
    if _wait_event(child, "ready") is None:
        if "--dual-deck-gpu" in mode_args:
            pytest.skip("the GPU dual-deck child cannot start on this machine")
        raise AssertionError("child never announced ready")
    child.stdin.write(
        (json.dumps({"cmd": "load", "token": 1, "path": _FIXTURE}) + "\n").encode())
    child.stdin.flush()
    assert _wait_event(child, "started") is not None, "child never started playback"
    time.sleep(0.5)  # genuinely playing, not merely asked to


@pytest.mark.skipif(not os.path.isfile(_FIXTURE), reason="video fixture not present")
@pytest.mark.parametrize(
    "mode_args", [(), ("--dual-deck-gpu",)], ids=["classic", "gpu_dual_deck"])
def test_a_playing_video_child_exits_when_its_parent_goes_away(mode_args, tmp_path):
    """No shutdown command -- only EOF, exactly what a force-exited or
    crashed parent leaves behind while video is playing.

    The CPU dual-deck mode (--dual-deck) is deliberately not covered: that
    child crashes during ordinary playback on this Qt/PyQt build, with no
    shutdown involved at all (measured 5 crashes in 10 plain play-and-watch
    runs, against 0 for both modes here), which is the renderer blocker
    test_video_dual_transition_fixture_integration.py is already skipped
    for. Covering it here would test that known crash rather than this
    exit guarantee.
    """
    child = _spawn(mode_args, tmp_path)
    try:
        _play_fixture(child, mode_args)
        child.stdin.close()
        started = time.monotonic()
        try:
            code = child.wait(timeout=_EXIT_BUDGET_S)
        except subprocess.TimeoutExpired:
            pytest.fail(
                "a playing video child survived its parent going away for "
                f"{_EXIT_BUDGET_S:.0f}s -- an orphan still holding the audio device"
            )
        assert code == 0, f"child exited with {code}"
        assert time.monotonic() - started < _EXIT_BUDGET_S
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


@pytest.mark.skipif(not os.path.isfile(_FIXTURE), reason="video fixture not present")
def test_the_shutdown_command_stops_playback_before_the_child_exits(tmp_path):
    """The ordinary path the parent uses: the child stops its player first
    (VideoSubprocessController.shutdown) and only then leaves its event
    loop. Kept alongside the EOF case so a change that made one path exit
    cleanly while breaking the other cannot pass unnoticed."""
    child = _spawn((), tmp_path)
    try:
        _play_fixture(child)
        child.stdin.write((json.dumps({"cmd": "shutdown"}) + "\n").encode())
        child.stdin.flush()
        child.stdin.close()
        try:
            code = child.wait(timeout=_EXIT_BUDGET_S)
        except subprocess.TimeoutExpired:
            pytest.fail("the child did not exit after being asked to shut down")
        assert code == 0, f"child exited with {code}"
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
