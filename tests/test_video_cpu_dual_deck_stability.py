"""Phase 9: the CPU dual-deck child must survive ordinary playback.

DualDeckVideoSubprocessController's compositor stored the QVideoFrame its
videoFrameChanged slot was handed. PyQt wraps that `const QVideoFrame &`
without copying, so the stored object was only valid for the duration of
the slot: every later paintEvent read freed memory. The child took
0xC0000005 (or 0xC0000374, when the freed block had already been reused)
within about a second of playback starting -- no transition, no secondary
deck and no shutdown involved. Measured on the pre-fix code with the same
sequences these tests use: 6-8 crashes in every 8 runs of plain playback,
8 in 8 once a transition was added. After the fix (see
_DualDeckCompositorWidget._owned) the same matrix crashed 0 times in 8, and
120 consecutive transition cycles completed cleanly.

These run the real child process, because the failure is a native crash: an
in-process version would take pytest down with it. They talk to the child
over its own stdin/stdout protocol rather than through
QtVideoPlaybackBackend -- the parent backend's preload-identity validation
(preload_id/source_hash/deck_index) is only implemented by the GPU
controller, so no CPU transition can complete through that API at all (see
test_video_dual_transition_fixture_integration.py's skip note).

Note that the application itself never selects CPU dual-deck mode --
window.py only ever calls set_dual_mode("gpu") or None. This compositor is
the record of the Phase 2A investigation, and these tests keep it honest.
"""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

_FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
_FIXTURE_A = os.path.join(_FIXTURES_DIR, "sample.mp4")
_FIXTURE_B = os.path.join(_FIXTURES_DIR, "sample.mkv")
_FIXTURES_PRESENT = os.path.isfile(_FIXTURE_A) and os.path.isfile(_FIXTURE_B)

# Long enough for the pre-fix crash to land (it happened within ~1s of
# playback, and the fixtures are 2s long) without making the suite slow.
_PLAYBACK_WATCH_S = 2.5


class _Child:
    """The real CPU dual-deck child, driven over its own IPC protocol."""

    def __init__(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(
            os.environ, QT_QPA_PLATFORM="offscreen", PYTHONUNBUFFERED="1")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "billsmusic.video_subprocess", "--dual-deck"],
            cwd=repo_root, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self.events = []
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.process.stdout:
            try:
                self.events.append(json.loads(line.decode().strip()))
            except ValueError:
                pass

    def send(self, **command):
        if self.process.poll() is not None:
            return False
        try:
            self.process.stdin.write((json.dumps(command) + "\n").encode())
            self.process.stdin.flush()
            return True
        except OSError:
            return False

    def wait_event(self, name, seconds=10.0, after=0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for event in self.events[after:]:
                if event.get("event") == name:
                    return True
            if self.process.poll() is not None:
                return False
            time.sleep(0.02)
        return False

    def watch(self, seconds):
        """Let it keep decoding and painting. False if it died meanwhile."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                return False
            time.sleep(0.02)
        return True

    def alive(self):
        return self.process.poll() is None

    def finish(self):
        """Ask it to exit and return its exit code."""
        self.send(cmd="shutdown")
        try:
            return self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            return "timeout"

    def kill(self):
        if self.process.poll() is None:
            self.process.kill()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def _crash_message(child, stage):
    return (
        f"the CPU dual-deck child died {stage} with exit code "
        f"{child.process.poll()} (0xC0000005 = access violation, "
        f"0xC0000374 = heap corruption); events seen: "
        f"{[e.get('event') for e in child.events][-8:]}"
    )


@pytest.fixture
def child():
    instance = _Child()
    try:
        yield instance
    finally:
        instance.kill()


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="video fixtures not present")
def test_plain_playback_does_not_crash_the_cpu_dual_deck_child(child):
    """The minimum sequence that used to crash: one deck, playing."""
    assert child.wait_event("ready", 20), "child never announced ready"
    assert child.send(cmd="load", token=1, path=_FIXTURE_A)
    assert child.wait_event("started", 15), "child never started playback"

    assert child.watch(_PLAYBACK_WATCH_S), _crash_message(child, "during plain playback")
    assert child.finish() == 0


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="video fixtures not present")
def test_a_committed_cross_dissolve_completes_without_crashing(child):
    """Both decks decoding, blended, then promoted -- the compositor's
    actual job, and the sequence the frame ownership has to survive."""
    assert child.wait_event("ready", 20)
    assert child.send(cmd="load", token=1, path=_FIXTURE_A)
    assert child.wait_event("started", 15)

    mark = len(child.events)
    assert child.send(cmd="preload_secondary", path=_FIXTURE_B)
    assert child.wait_event("secondary_ready", 15, mark), _crash_message(
        child, "while preloading the secondary deck")

    mark = len(child.events)
    assert child.send(cmd="commit_dual_transition", duration_ms=400)
    assert child.wait_event("dual_transition_complete", 15, mark), _crash_message(
        child, "during the cross-dissolve")

    # The promoted deck keeps painting after the old primary is torn down.
    assert child.watch(1.0), _crash_message(child, "after promotion")
    assert child.finish() == 0


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason="video fixtures not present")
def test_repeated_preload_and_cancel_cycles_do_not_crash_the_child(child):
    """Frames from a deck that is created and destroyed repeatedly, with no
    transition ever committed -- the shape that leaves stale frames behind."""
    assert child.wait_event("ready", 20)
    assert child.send(cmd="load", token=1, path=_FIXTURE_A)
    assert child.wait_event("started", 15)

    for cycle in range(4):
        mark = len(child.events)
        assert child.send(cmd="preload_secondary", path=_FIXTURE_B), _crash_message(
            child, f"before preload cycle {cycle}")
        assert child.wait_event("secondary_ready", 15, mark), _crash_message(
            child, f"in preload cycle {cycle}")
        assert child.send(cmd="cancel_secondary")
        assert child.watch(0.25), _crash_message(child, f"after cancel cycle {cycle}")

    assert child.alive(), _crash_message(child, "across preload/cancel cycles")
    assert child.finish() == 0
