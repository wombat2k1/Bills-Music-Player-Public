"""Cross-file isolation regressions: failures that only ever appeared when
two particular test files happened to share one pytest process (one xdist
worker), so they are reproduced here by running exactly that ordered pair
in a child pytest process.
"""
import os
import subprocess
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_TESTS_DIR)


def _run_pytest_in_child(node_ids, tmp_path, timeout_s=120):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", LOCALAPPDATA=str(tmp_path))
    try:
        return subprocess.run(
            [
                sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                "-p", "no:xdist", "-o", f"faulthandler_timeout={timeout_s // 2}",
                *node_ids,
            ],
            cwd=_REPO_ROOT, env=env, capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as ex:
        output = (ex.stdout or b"")
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        pytest.fail(f"child pytest run hung (> {timeout_s}s):\n{output[-4000:]}")


def test_mini_player_tests_do_not_leave_a_window_that_vetoes_later_app_quit(tmp_path):
    """test_mini_player.py used to leave a visible MiniPlayerWindow behind;
    its closeEvent ignores closes it wasn't allowed, so Qt 6 then refused
    every later QApplication.quit() and the analyzer test's app.exec() never
    returned (observed as a full-suite hang at ~97%)."""
    result = _run_pytest_in_child(
        [
            "tests/test_mini_player.py::test_opening_reuses_one_window_and_hides_main_without_playback_command",
            "tests/test_video_transition_point_analyzer.py::test_probe_failure_result_stores_no_trim_and_does_not_raise",
        ],
        tmp_path,
    )
    assert result.returncode == 0, f"{result.stdout[-4000:]}\n{result.stderr[-2000:]}"
    assert "2 passed" in result.stdout
