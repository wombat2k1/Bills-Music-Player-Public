"""Classic-video silent-audio investigation, CONFIRMED root cause
(2026-08-31 Codex audit follow-up): the real r4 acceptance installer's own
diagnostics proved VideoSubprocessController's QAudioOutput binds to
whatever Qt's default output device is at *process construction time* and
never updates again on its own -- device_hash matched
default_output_device_hash at child_startup, then diverged by the time
video actually played (has_audio/actual_volume/actual_muted all still
reported "healthy" throughout, since none of those signal a wrong-device
condition at all).

These tests drive the real VideoSubprocessController and, wherever
possible, the two genuinely different real audio output devices this
machine has (see the module-level skip guard below) -- not a fully faked
video backend. Where the "system default changed" trigger itself can't be
automated without actually changing this machine's live Windows audio
configuration (out of scope for an automated test), only that one signal
is simulated via a monkeypatch of QMediaDevices.defaultAudioOutput(), per
the explicit instruction to factor and test the device-reconciliation
*policy* rather than fake the whole backend.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtCore, QtWidgets
from PyQt6.QtMultimedia import QMediaDevices

from billsmusic.video_subprocess import VideoSubprocessController

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample.mp4")

_APP = None


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
    controller = VideoSubprocessController()
    return controller, events


def _real_devices():
    """The two genuinely different real audio output devices this
    machine has (confirmed present during this investigation) -- None if
    fewer than two are available, in which case the real-device-switch
    tests below are skipped rather than faking a QAudioDevice identity
    (Qt does not support constructing one directly; only real system
    enumeration or a monkeypatched return value produces a valid one)."""
    _app()
    outputs = QMediaDevices.audioOutputs()
    if len(outputs) < 2:
        return None
    return outputs[0], outputs[1]


_two_real_devices = _real_devices()


def _events_named(events, name):
    return [e for e in events if e.get("event") == name]


def test_reconcile_is_a_noop_when_device_already_matches_default(monkeypatch):
    controller, events = _controller(monkeypatch)

    controller._reconcile_audio_output_device("test")

    assert _events_named(events, "audio_device_rebound") == []


@pytest.mark.skipif(_two_real_devices is None, reason="fewer than 2 real audio output devices present")
def test_reconcile_rebinds_when_default_has_drifted_preserving_volume_and_mute(monkeypatch):
    device_a, device_b = _two_real_devices
    controller, events = _controller(monkeypatch)
    controller.audio_output.setDevice(device_a)
    controller.audio_output.setVolume(0.42)
    controller.audio_output.setMuted(True)
    # Simulate "the system default changed since this process started" --
    # the one piece that can't be triggered by actually changing this
    # machine's live Windows audio configuration in an automated test.
    monkeypatch.setattr(QMediaDevices, "defaultAudioOutput", staticmethod(lambda: device_b))

    controller._reconcile_audio_output_device("test_drift")

    assert bytes(controller.audio_output.device().id()) == bytes(device_b.id())
    assert controller.audio_output.volume() == pytest.approx(0.42, abs=1e-3)
    assert controller.audio_output.isMuted() is True
    rebinds = _events_named(events, "audio_device_rebound")
    assert len(rebinds) == 1
    assert rebinds[0]["reason"] == "test_drift"
    assert rebinds[0]["preserved_volume"] == pytest.approx(0.42, abs=1e-3)
    assert rebinds[0]["preserved_muted"] is True
    assert rebinds[0]["old_device_hash"] != rebinds[0]["new_device_hash"]
    # Anonymised -- never the raw device description/id in the diagnostic.
    assert device_a.description() not in str(rebinds[0])
    assert device_b.description() not in str(rebinds[0])


@pytest.mark.skipif(_two_real_devices is None, reason="fewer than 2 real audio output devices present")
def test_calling_reconcile_twice_only_rebinds_once(monkeypatch):
    device_a, device_b = _two_real_devices
    controller, events = _controller(monkeypatch)
    controller.audio_output.setDevice(device_a)
    monkeypatch.setattr(QMediaDevices, "defaultAudioOutput", staticmethod(lambda: device_b))

    controller._reconcile_audio_output_device("first")
    controller._reconcile_audio_output_device("second")

    assert len(_events_named(events, "audio_device_rebound")) == 1


@pytest.mark.skipif(not os.path.isfile(_FIXTURE), reason="video fixture not present")
@pytest.mark.skipif(_two_real_devices is None, reason="fewer than 2 real audio output devices present")
def test_load_reconciles_device_before_playback_no_duplicate_player(monkeypatch):
    """The exact real failure, reproduced against the real dispatch path:
    device drifts from what the process bound to at construction, WITHOUT
    recreating the subprocess, then a video is loaded/played. Post-fix:
    the deck is rebound *before* playback starts; source still loads,
    hasAudio remains true, exactly one QMediaPlayer/QAudioOutput pair is
    ever used (no duplicate player), volume/mute are preserved."""
    device_a, device_b = _two_real_devices
    controller, events = _controller(monkeypatch)
    controller.audio_output.setDevice(device_a)
    controller.audio_output.setVolume(0.65)
    controller.audio_output.setMuted(False)
    monkeypatch.setattr(QMediaDevices, "defaultAudioOutput", staticmethod(lambda: device_b))
    player_before = controller.player
    audio_output_before = controller.audio_output

    controller._dispatch({"cmd": "load", "token": 1, "path": _FIXTURE})

    # Rebind happened synchronously, before setSource()/play() -- proven
    # by the rebind event appearing before load_requested in emit order.
    names = [e.get("event") for e in events]
    assert "audio_device_rebound" in names
    assert names.index("audio_device_rebound") < names.index("load_requested")
    assert bytes(controller.audio_output.device().id()) == bytes(device_b.id())
    assert controller.audio_output.volume() == pytest.approx(0.65, abs=1e-3)
    assert controller.audio_output.isMuted() is False
    # No duplicate player/output -- still the exact same objects, just
    # the existing QAudioOutput's device property changed.
    assert controller.player is player_before
    assert controller.audio_output is audio_output_before

    assert _pump_until(
        lambda: any(e.get("event") == "started" for e in events), seconds=8.0,
    ), "video never started after device reconciliation"
    assert _pump_until(
        lambda: any(
            e.get("event") == "classic_audio_state" and e.get("has_audio") is True
            for e in events
        ),
        seconds=8.0,
    ), "has_audio never confirmed true after device reconciliation"

    controller.shutdown()


@pytest.mark.skipif(not os.path.isfile(_FIXTURE), reason="video fixture not present")
@pytest.mark.skipif(_two_real_devices is None, reason="fewer than 2 real audio output devices present")
def test_live_device_change_signal_migrates_while_genuinely_playing(monkeypatch):
    """Section 7's live-migration test: video already playing on device A,
    then the (simulated) system default changes to B while playback is
    active -- confirmed empirically safe in this Qt 6.11 build
    (QAudioOutput.setDevice() while PlayingState: no error, position keeps
    advancing). Drives the real audioOutputsChanged subscription, not a
    direct method call, so this also proves the signal wiring itself
    works end to end."""
    device_a, device_b = _two_real_devices
    controller, events = _controller(monkeypatch)
    controller.audio_output.setDevice(device_a)
    controller.player.setSource(QtCore.QUrl.fromLocalFile(_FIXTURE))
    controller.player.play()
    assert _pump_until(lambda: bool(_events_named(events, "started")), seconds=8.0)

    position_before = controller.player.position()
    monkeypatch.setattr(QMediaDevices, "defaultAudioOutput", staticmethod(lambda: device_b))
    controller._media_devices_monitor.audioOutputsChanged.emit()

    assert bytes(controller.audio_output.device().id()) == bytes(device_b.id())
    assert controller.player.error() == controller.player.error().NoError
    assert _pump_until(
        lambda: controller.player.position() > position_before, seconds=5.0,
    ), "playback did not continue advancing after the live device migration"
    assert controller.player.playbackState() == controller.player.playbackState().PlayingState

    controller.shutdown()
