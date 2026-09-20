"""GPU dual-deck equivalent of test_classic_video_audio_device_following.py
(2026-08-31 Codex audit, follow-up round, section 8): the same real-device
evidence that proved the classic child's QAudioOutput never follows a
post-construction default-device change applies identically to each QML
AudioOutput (audio0/audio1) inside GpuDualDeckVideoSubprocessController --
both bind to whatever the Qt default was at scene-load time and never
update on their own.

These tests instantiate the real GPU controller in-process (the QML scene
genuinely loads and its shader/compositor run in the offscreen software
rasterizer, same convention as test_video_backend_gpu_fixture_integration.py)
rather than going through QtVideoPlaybackBackend's real subprocess, since
the device-drift trigger itself needs to be simulated via a monkeypatch of
QMediaDevices.defaultAudioOutput() -- not reachable across a process
boundary. This exercises the real _reconcile_gpu_audio_output_devices()
policy and the real QML AudioOutput "device"/"volume"/"muted" properties,
not a faked compositor.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtCore, QtWidgets
from PyQt6.QtMultimedia import QMediaDevices

from billsmusic.video_subprocess import GpuDualDeckVideoSubprocessController
from video_controller_teardown import release_and_collect

_FIXTURE_A = os.path.join(os.path.dirname(__file__), "fixtures", "sample.mp4")
_FIXTURE_B = os.path.join(os.path.dirname(__file__), "fixtures", "sample.mkv")

_APP = None

# Every real controller built in this process is released deterministically
# in its own test's teardown -- see video_controller_teardown.py for the
# GIL/FFmpeg-thread deadlock a garbage-collected QMediaPlayer otherwise
# causes in whichever later test the collector happens to run in.
_CONTROLLERS = []


@pytest.fixture(autouse=True)
def _release_real_controllers(monkeypatch):
    # Depends on monkeypatch so this teardown runs before its patches
    # (_emit, _StdinReaderThread.start) are undone.
    yield
    release_and_collect(_CONTROLLERS)


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _pump_until(predicate, seconds=8.0):
    import time
    app = _app()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _controller(monkeypatch):
    _app()
    monkeypatch.setattr(
        "billsmusic.video_subprocess._StdinReaderThread.start", lambda self: None,
    )
    events = []
    monkeypatch.setattr(
        "billsmusic.video_subprocess._emit", lambda obj: events.append(obj),
    )
    controller = GpuDualDeckVideoSubprocessController()
    _CONTROLLERS.append(controller)
    return controller, events


def _real_devices():
    _app()
    outputs = QMediaDevices.audioOutputs()
    if len(outputs) < 2:
        return None
    return outputs[0], outputs[1]


_two_real_devices = _real_devices()


def _events_named(events, name):
    return [e for e in events if e.get("event") == name]


def _rebound_for_deck(events, deck):
    return [e for e in _events_named(events, "audio_device_rebound") if e.get("deck") == deck]


def test_scene_loads_and_reconcile_is_a_noop_when_devices_already_match(monkeypatch):
    controller, events = _controller(monkeypatch)
    assert controller._window is not None
    assert len(controller._audios) == 2

    controller._reconcile_gpu_audio_output_devices("test")

    assert _events_named(events, "audio_device_rebound") == []
    controller.shutdown()


@pytest.mark.skipif(_two_real_devices is None, reason="fewer than 2 real audio output devices present")
def test_reconcile_rebinds_both_decks_preserving_volume_and_mute(monkeypatch):
    device_a, device_b = _two_real_devices
    controller, events = _controller(monkeypatch)
    controller._audios[0].setProperty("device", device_a)
    controller._audios[0].setProperty("volume", 0.42)
    controller._audios[0].setProperty("muted", False)
    controller._audios[1].setProperty("device", device_a)
    controller._audios[1].setProperty("volume", 0.0)
    controller._audios[1].setProperty("muted", True)
    monkeypatch.setattr(QMediaDevices, "defaultAudioOutput", staticmethod(lambda: device_b))

    controller._reconcile_gpu_audio_output_devices("test_drift")

    for i in (0, 1):
        assert bytes(controller._audios[i].property("device").id()) == bytes(device_b.id())
    assert controller._audios[0].property("volume") == pytest.approx(0.42, abs=1e-3)
    assert controller._audios[0].property("muted") is False
    assert controller._audios[1].property("volume") == pytest.approx(0.0, abs=1e-3)
    assert controller._audios[1].property("muted") is True
    assert len(_rebound_for_deck(events, 0)) == 1
    assert len(_rebound_for_deck(events, 1)) == 1
    controller.shutdown()


@pytest.mark.skipif(_two_real_devices is None, reason="fewer than 2 real audio output devices present")
def test_reconcile_does_not_touch_a_deck_already_on_the_default(monkeypatch):
    """Only the deck that's genuinely drifted is rebound -- a deck already
    on the current default must not emit a spurious rebound event (would
    otherwise mask a real drift in diagnostics with noise)."""
    device_a, device_b = _two_real_devices
    controller, events = _controller(monkeypatch)
    controller._audios[0].setProperty("device", device_a)  # will drift
    controller._audios[1].setProperty("device", device_b)  # already correct
    monkeypatch.setattr(QMediaDevices, "defaultAudioOutput", staticmethod(lambda: device_b))

    controller._reconcile_gpu_audio_output_devices("test_partial_drift")

    assert len(_rebound_for_deck(events, 0)) == 1
    assert len(_rebound_for_deck(events, 1)) == 0
    controller.shutdown()


@pytest.mark.skipif(not os.path.isfile(_FIXTURE_A), reason="video fixture not present")
@pytest.mark.skipif(_two_real_devices is None, reason="fewer than 2 real audio output devices present")
def test_load_reconciles_primary_deck_before_playback(monkeypatch):
    """Same real-failure shape as the classic-child test: device drifts
    without recreating the subprocess, then load/play happens on the
    primary deck. Post-fix: reconciled before setSource()/play()."""
    device_a, device_b = _two_real_devices
    controller, events = _controller(monkeypatch)
    controller._audios[0].setProperty("device", device_a)
    monkeypatch.setattr(QMediaDevices, "defaultAudioOutput", staticmethod(lambda: device_b))

    controller._dispatch({
        "cmd": "load", "token": 1, "path": _FIXTURE_A, "identity_hash": "hash-a",
    })

    assert bytes(controller._audios[0].property("device").id()) == bytes(device_b.id())
    rebinds = _rebound_for_deck(events, 0)
    assert len(rebinds) == 1
    assert rebinds[0]["reason"] == "pre_load"

    assert _pump_until(
        lambda: any(e.get("event") == "started" for e in events), seconds=8.0,
    ), "primary deck never reported started after device reconciliation"
    controller.shutdown()


@pytest.mark.skipif(not os.path.isfile(_FIXTURE_A), reason="video fixture not present")
@pytest.mark.skipif(not os.path.isfile(_FIXTURE_B), reason="video fixture not present")
@pytest.mark.skipif(_two_real_devices is None, reason="fewer than 2 real audio output devices present")
def test_preload_secondary_reconciles_secondary_deck_before_playback(monkeypatch):
    """A secondary preload is its own source-loading path, independent of
    the primary load -- must be reconciled on its own before setSource().
    Isolated from the primary load's own reconciliation pass (which
    covers every deck, not just the primary -- see
    test_reconcile_rebinds_both_decks_preserving_volume_and_mute) by only
    drifting the default *after* the primary load already ran, so this
    proves specifically that preload_secondary's own reconciliation call
    is what catches it, not a side effect of the earlier load."""
    device_a, device_b = _two_real_devices
    controller, events = _controller(monkeypatch)
    controller._audios[0].setProperty("device", device_a)
    controller._audios[1].setProperty("device", device_a)
    monkeypatch.setattr(QMediaDevices, "defaultAudioOutput", staticmethod(lambda: device_a))

    controller._dispatch({
        "cmd": "load", "token": 1, "path": _FIXTURE_A, "identity_hash": "hash-a",
    })
    assert _events_named(events, "audio_device_rebound") == []  # nothing drifted yet
    events.clear()

    monkeypatch.setattr(QMediaDevices, "defaultAudioOutput", staticmethod(lambda: device_b))
    controller._dispatch({
        "cmd": "preload_secondary", "path": _FIXTURE_B,
        "identity_hash": "hash-b", "preload_id": "preload-1",
    })

    assert bytes(controller._audios[1].property("device").id()) == bytes(device_b.id())
    rebinds = _rebound_for_deck(events, 1)
    assert len(rebinds) == 1
    assert rebinds[0]["reason"] == "pre_load_secondary"
    controller.shutdown()


@pytest.mark.skipif(not os.path.isfile(_FIXTURE_A), reason="video fixture not present")
@pytest.mark.skipif(_two_real_devices is None, reason="fewer than 2 real audio output devices present")
def test_live_device_change_signal_migrates_both_decks_without_disturbing_master_state(monkeypatch):
    """Section 8's explicit requirement: a live device-list change must
    rebind both decks while preserving _master_volume_fraction/
    _master_muted -- driven through the real audioOutputsChanged signal,
    not a direct method call."""
    device_a, device_b = _two_real_devices
    controller, events = _controller(monkeypatch)
    controller._audios[0].setProperty("device", device_a)
    controller._audios[1].setProperty("device", device_a)

    controller._dispatch({"cmd": "set_volume", "volume": 55})
    controller._dispatch({"cmd": "set_muted", "muted": False})
    assert controller._master_volume_fraction == pytest.approx(0.55, abs=1e-3)
    assert controller._master_muted is False

    monkeypatch.setattr(QMediaDevices, "defaultAudioOutput", staticmethod(lambda: device_b))
    controller._media_devices_monitor.audioOutputsChanged.emit()

    for i in (0, 1):
        assert bytes(controller._audios[i].property("device").id()) == bytes(device_b.id())
    # Master state, the sole authority for what volume/mute *should* be,
    # must be completely untouched by a device rebind.
    assert controller._master_volume_fraction == pytest.approx(0.55, abs=1e-3)
    assert controller._master_muted is False
    controller.shutdown()
