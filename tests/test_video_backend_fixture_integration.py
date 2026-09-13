"""Integration test driving QtVideoPlaybackBackend against a real, tiny
generated MP4 fixture (tests/fixtures/sample.mp4) through Qt's actual
QMediaPlayer -- not mocked. Confirms the backend genuinely opens, plays,
reports duration/position, and reaches EndOfMedia through real Qt Multimedia
decoding, complementing the mocked unit tests in test_video_backend.py.

Skips gracefully if the fixture is missing (e.g. a checkout without
vendor/ffmpeg's generated fixtures) rather than failing the whole suite.
"""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtCore, QtWidgets

from billsmusic.video_backend import QtVideoPlaybackBackend

_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "sample.mp4"
)

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _pump_until(predicate, seconds=8.0):
    app = _app()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.mark.skipif(not os.path.isfile(_FIXTURE), reason="video fixture not present")
def test_real_mp4_fixture_plays_and_reports_duration():
    _app()
    backend = QtVideoPlaybackBackend()
    durations = []
    backend.duration_changed.connect(durations.append)
    started = []
    backend.started.connect(lambda: started.append(True))

    result = backend.load(_FIXTURE)
    assert result is True

    assert _pump_until(lambda: bool(started)), "video never reported started"
    assert _pump_until(lambda: backend.duration_ms() > 0), "duration never became known"
    assert backend.duration_ms() <= 5000  # the fixture is ~2s

    backend.shutdown()


@pytest.mark.skipif(not os.path.isfile(_FIXTURE), reason="video fixture not present")
def test_real_mp4_fixture_reaches_end_of_media():
    _app()
    backend = QtVideoPlaybackBackend()
    ended = []
    backend.end_of_media.connect(lambda: ended.append(True))

    backend.load(_FIXTURE)
    assert _pump_until(lambda: bool(ended), seconds=10.0), "EndOfMedia never fired"

    backend.shutdown()


def test_shutdown_leaves_no_running_child_process_and_reader_thread_joins():
    # If video_subprocess.py's stdin reader thread didn't cleanly join
    # before the process exits, Qt prints "QThread: Destroyed while thread
    # is still running" to stderr (and can crash on some platforms) -- a
    # clean exit with empty/unremarkable stderr is the observable proof
    # the reader thread actually finished, not just that the process died.
    _app()
    backend = QtVideoPlaybackBackend()
    _pump_until(lambda: backend._process is not None and backend._process_ready, seconds=8.0)
    assert backend._process_ready is True

    backend.shutdown()

    assert backend._process.state() == QtCore.QProcess.ProcessState.NotRunning
    stderr = bytes(backend._process.readAllStandardError()).decode("utf-8", errors="replace")
    assert "Destroyed while thread is still running" not in stderr


@pytest.mark.skipif(not os.path.isfile(_FIXTURE), reason="video fixture not present")
def test_real_mp4_fixture_seek_and_position_reporting():
    _app()
    backend = QtVideoPlaybackBackend()
    positions = []
    backend.position_changed.connect(positions.append)
    backend.duration_changed.connect(lambda d: None)

    backend.load(_FIXTURE)
    assert _pump_until(lambda: backend.duration_ms() > 0)
    duration = backend.duration_ms()
    backend.seek(min(500, duration // 2))
    assert _pump_until(lambda: backend.position_ms() > 0, seconds=5.0)

    backend.shutdown()


# -- classic-audio-silence investigation (2026-08-31 Codex audit, section 9) -
# The real classic (non-dual) child, spawned as an actual OS subprocess
# exactly like production does, playing the real sample.mp4 fixture --
# confirmed separately (see this module's own manual verification during
# development) to carry a genuine, non-silent AAC mono audio track. These
# tests are the "not only a fake backend" real integration coverage the
# investigation requires: they can't prove audible sound reaches Windows'
# actual speakers (no CI machine can), but they do prove every layer this
# process's own new diagnostics claim to observe -- hasAudio, the correct
# QAudioOutput attachment, command acknowledgement, and genuinely non-zero
# decoded PCM -- is real, not merely "looks right on paper".

def _diagnostics_events(diagnostics, operation):
    return [e for e in diagnostics.recent_events if e["operation"] == operation]


@pytest.mark.skipif(not os.path.isfile(_FIXTURE), reason="video fixture not present")
def test_real_classic_child_reports_has_audio_and_correct_output_attachment(tmp_path):
    from billsmusic.performance_diagnostics import get_diagnostics, reset_diagnostics_for_tests

    reset_diagnostics_for_tests()
    diagnostics = get_diagnostics(directory=str(tmp_path), level="developer", start_writer=False)
    _app()
    backend = QtVideoPlaybackBackend()
    started = []
    backend.started.connect(lambda: started.append(True))

    assert backend.load(_FIXTURE) is True
    assert _pump_until(lambda: bool(started)), "video never reported started"
    # The steady-state checkpoint fires 2s into confirmed PlayingState.
    assert _pump_until(
        lambda: bool(_diagnostics_events(diagnostics, "video_classic_audio_state")),
        seconds=3.0,
    )

    snapshots = _diagnostics_events(diagnostics, "video_classic_audio_state")
    checkpoints = {e["details"]["checkpoint"] for e in snapshots}
    assert "playback_started" in checkpoints or "media_ready" in checkpoints or "source_loaded" in checkpoints

    playing_snapshot = next(
        e["details"] for e in snapshots if e["details"]["checkpoint"] == "playback_started"
    )
    assert playing_snapshot["has_audio"] is True, "real fixture has a genuine audio track"
    assert playing_snapshot["audio_output_attached_correctly"] is True, (
        "the player's audioOutput() must be the exact QAudioOutput this "
        "process constructed and is reading volume/mute from"
    )
    assert playing_snapshot["playback_state"] == "PlayingState"
    assert playing_snapshot["error"] == "NoError"

    backend.shutdown()


@pytest.mark.skipif(not os.path.isfile(_FIXTURE), reason="video fixture not present")
def test_real_classic_child_acknowledges_volume_and_mute_commands(tmp_path):
    from billsmusic.performance_diagnostics import get_diagnostics, reset_diagnostics_for_tests

    reset_diagnostics_for_tests()
    diagnostics = get_diagnostics(directory=str(tmp_path), level="developer", start_writer=False)
    _app()
    backend = QtVideoPlaybackBackend()
    started = []
    backend.started.connect(lambda: started.append(True))
    assert backend.load(_FIXTURE) is True
    assert _pump_until(lambda: bool(started))

    backend.set_volume(55)
    assert _pump_until(
        lambda: any(
            e["details"]["operation"] == "set_volume"
            for e in _diagnostics_events(diagnostics, "video_audio_command_applied")
        )
    )
    ack = next(
        e["details"] for e in _diagnostics_events(diagnostics, "video_audio_command_applied")
        if e["details"]["operation"] == "set_volume"
    )
    assert ack["actual_volume"] == pytest.approx(0.55, abs=1e-3)
    assert ack["audio_output_attached_correctly"] is True

    backend.set_muted(True)
    assert _pump_until(
        lambda: any(
            e["details"]["operation"] == "set_muted"
            for e in _diagnostics_events(diagnostics, "video_audio_command_applied")
        )
    )
    mute_acks = [
        e["details"] for e in _diagnostics_events(diagnostics, "video_audio_command_applied")
        if e["details"]["operation"] == "set_muted"
    ]
    assert mute_acks[-1]["actual_muted"] is True

    backend.shutdown()


@pytest.mark.skipif(not os.path.isfile(_FIXTURE), reason="video fixture not present")
def test_real_classic_child_reports_non_zero_decoded_audio_evidence(tmp_path):
    """The strongest available proof this real fixture's audio is genuinely
    decoding: not hasAudio (a stream *exists*) but actual non-zero PCM
    samples observed via QAudioBufferOutput, without rerouting playback
    through Python at all (see _extract_audio_buffer_evidence)."""
    from billsmusic.performance_diagnostics import get_diagnostics, reset_diagnostics_for_tests

    reset_diagnostics_for_tests()
    diagnostics = get_diagnostics(directory=str(tmp_path), level="developer", start_writer=False)
    _app()
    backend = QtVideoPlaybackBackend()
    started = []
    backend.started.connect(lambda: started.append(True))
    assert backend.load(_FIXTURE) is True
    assert _pump_until(lambda: bool(started))

    assert _pump_until(
        lambda: bool(_diagnostics_events(diagnostics, "video_decoded_audio_evidence")),
        seconds=5.0,
    ), "no decoded-audio evidence was ever reported for a fixture with a real audio track"
    evidence = _diagnostics_events(diagnostics, "video_decoded_audio_evidence")[0]["details"]
    assert evidence["sample_count"] > 0
    assert evidence["non_zero"] is True, (
        "the real fixture's audio track is not silent -- decoded samples "
        "must show genuine signal, not just an empty/zeroed buffer"
    )
    assert evidence["format"] in ("Float", "Int16", "Int32", "UInt8")

    backend.shutdown()
