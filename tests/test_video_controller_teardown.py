"""Regression for the full suite's intermittent xdist hang at ~94-98%:
a real VideoSubprocessController built in-process, left to the garbage
collector after playing media, deadlocked the whole pytest worker when it
was eventually collected (~QMediaPlayer waiting on the FFmpeg plugin thread,
which was waiting on the GIL). See tests/video_controller_teardown.py.

Runs in a subprocess because the failure mode is a permanent hang.
"""
import os
import subprocess
import sys
import textwrap

import pytest

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample.mp4")

_SCRIPT = textwrap.dedent(
    r"""
    import os, sys, time
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6 import QtWidgets
    import billsmusic.video_subprocess as vs
    from video_controller_teardown import controller_media_players, release_and_collect

    vs._StdinReaderThread.start = lambda self: None
    vs._emit = lambda obj: None
    app = QtWidgets.QApplication([])

    def pump(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.005)

    fixture = sys.argv[1]
    for i in range(8):
        controller = vs.VideoSubprocessController()
        controller._dispatch({"cmd": "load", "token": 1, "path": fixture})
        pump(0.4 + (i % 4) * 0.1)
        assert controller_media_players(controller), "no players found to release"
        controller.shutdown()  # what the device-following tests end with
        tracked = [controller]
        del controller
        release_and_collect(tracked)  # the collect used to hang right here
        print("released", i, flush=True)
    print("DONE", flush=True)
    """
)


@pytest.mark.skipif(not os.path.isfile(_FIXTURE), reason="video fixture not present")
def test_released_controller_that_played_media_can_be_collected_without_deadlock(tmp_path):
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(tests_dir)
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join([repo_root, tests_dir, os.environ.get("PYTHONPATH", "")]),
        LOCALAPPDATA=str(tmp_path),
        QT_QPA_PLATFORM="offscreen",
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", _SCRIPT, _FIXTURE],
            cwd=repo_root, env=env, capture_output=True, text=True, timeout=40,
        )
    except subprocess.TimeoutExpired as ex:
        pytest.fail(
            "collecting a released controller deadlocked (no exit within 40s); "
            f"progress: {ex.stdout!r}"
        )
    assert result.returncode == 0, f"{result.stdout[-2000:]}\n{result.stderr[-4000:]}"
    assert "DONE" in result.stdout
