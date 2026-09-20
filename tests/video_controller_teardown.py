"""Deterministic teardown for tests that construct a real video_subprocess
controller (VideoSubprocessController / GpuDualDeckVideoSubprocessController)
inside the pytest process instead of in its own child process.

Why this exists: those controllers own Python-owned QMediaPlayer objects.
Left to Python's garbage collector, a player that has had media loaded is
destroyed at an arbitrary later moment -- typically in some unrelated later
test, from the GUI thread with the GIL held. ~QMediaPlayer then blocks in
QThread::wait() on the FFmpeg media plugin's worker thread, while that
thread is itself blocked waiting for the GIL (tearing down an object whose
PyQt virtual-method hook must enter Python): a permanent deadlock. Native
stacks of a hung xdist worker showed exactly that, and a standalone repro
(play a fixture, stop(), drop the controller, gc.collect()) hangs on its
first iteration every time.

The real video subprocess is not affected: main() only drops its controller
after app.exec() has returned, so the event loop has already settled the
stopped player (measured: every mode exits ~0.06s after "shutdown" with
media loaded). Only in-process test construction skips that step.

Clearing each player's source synchronously releases its FFmpeg session
while no Python-owned wrapper is being destroyed, after which dropping the
controller is safe. The collect happens right here, in the owning test's
teardown, so any future teardown problem is attributed to the test that
caused it rather than to whichever test the collector happens to run in.
"""
import gc

from PyQt6 import QtCore


def controller_media_players(controller):
    players = [getattr(controller, name, None) for name in ("player", "player_a", "player_b")]
    players.extend(getattr(controller, "_players", None) or [])
    return [player for player in players if player is not None]


def release_controller_media(controller):
    for player in controller_media_players(controller):
        try:
            player.stop()
        except Exception:
            pass
        try:
            player.setSource(QtCore.QUrl())
        except Exception:
            pass


def release_and_collect(controllers):
    """Release every tracked controller's media, drop the references, and
    collect now. `controllers` is the per-test list the owning test file's
    fixture appends to; it is emptied."""
    for controller in controllers:
        release_controller_media(controller)
    controllers.clear()
    gc.collect()
