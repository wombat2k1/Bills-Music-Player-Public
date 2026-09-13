"""Unit tests for QtVideoPlaybackBackend using a mocked QProcess -- no real
child process, no real video file or hardware decoding required."""
import json
import os
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtGui, QtWidgets

from billsmusic.video_backend import (
    ERROR_FILE_MISSING,
    ERROR_RESOURCE,
    ERROR_SUBPROCESS,
    QtVideoPlaybackBackend,
)

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _patch_fake_window_embedding(monkeypatch):
    """The offscreen Qt platform used for tests genuinely can't embed a
    foreign window by winId() -- QWindow.fromWinId() correctly returns
    None there (see video_backend.py's _create_embedded_container(), which
    now checks for exactly that and treats it as an embedding failure).
    Tests that only care about the attach/reuse/resize logic above that
    boundary stand in a real (if not actually foreign) QWindow instead, so
    createWindowContainer() has something genuine to wrap."""
    monkeypatch.setattr(
        QtGui.QWindow, "fromWinId", staticmethod(lambda win_id: QtGui.QWindow())
    )


def _make_backend():
    _app()
    process = MagicMock(spec=QtCore.QProcess)
    process.state.return_value = QtCore.QProcess.ProcessState.Running
    process.waitForFinished.return_value = True
    backend = QtVideoPlaybackBackend(process=process, auto_start=False)
    return backend, process


def _sent_commands(process) -> list:
    """Decode every JSON command written to the mocked process's stdin."""
    commands = []
    for call in process.write.call_args_list:
        data = call.args[0]
        commands.append(json.loads(bytes(data).decode("utf-8")))
    return commands


def _deliver_line(backend, process, obj: dict):
    """Simulate one line of JSON arriving on the child's stdout. ``process``
    is threaded through explicitly (see video_backend.py's _on_stdout_ready
    identity check, added 2026-08-24) -- pass the process the event is
    supposed to have come from; a stale/replaced process's line is silently
    dropped exactly as production code would."""
    line = (json.dumps(obj) + "\n").encode("utf-8")
    process.canReadLine.side_effect = [True, False]
    process.readLine.return_value = line
    backend._on_stdout_ready(process)


def test_load_missing_file_reports_file_missing_without_sending_command(tmp_path):
    backend, process = _make_backend()
    errors = []
    backend.error.connect(lambda category, message: errors.append(category))
    missing_path = str(tmp_path / "does_not_exist.mp4")

    result = backend.load(missing_path)

    assert result is False
    assert errors == [ERROR_FILE_MISSING]
    process.write.assert_not_called()


def test_load_existing_file_sends_load_command(tmp_path):
    backend, process = _make_backend()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"x")

    result = backend.load(str(video_path))

    assert result is True
    commands = _sent_commands(process)
    assert len(commands) == 1
    assert commands[0]["cmd"] == "load"
    assert commands[0]["path"] == str(video_path)
    assert commands[0]["token"] == 1
    # identity_hash (see the stale-timing-attribution regression tests
    # below) is computed fresh per load() -- present and non-empty, exact
    # value not asserted here since it's an opaque, session-salted hash.
    assert commands[0]["identity_hash"]


def test_play_pause_resume_stop_seek_send_expected_commands():
    backend, process = _make_backend()
    backend.play()
    backend.pause()
    backend.resume()
    backend.seek(12345)
    backend.stop()
    assert _sent_commands(process) == [
        {"cmd": "play"},
        {"cmd": "pause"},
        {"cmd": "resume"},
        {"cmd": "seek", "position_ms": 12345},
        {"cmd": "stop"},
    ]


def test_volume_and_mute_send_expected_commands():
    backend, process = _make_backend()
    backend.set_volume(50)
    backend.set_volume(150)  # clamps to 100
    backend.set_volume(-10)  # clamps to 0
    backend.set_muted(True)
    assert _sent_commands(process) == [
        {"cmd": "set_volume", "volume": 50.0},
        {"cmd": "set_volume", "volume": 100.0},
        {"cmd": "set_volume", "volume": 0.0},
        {"cmd": "set_muted", "muted": True},
    ]


def test_position_and_duration_update_from_events():
    backend, process = _make_backend()
    backend.load.__self__._token = 1  # no-op, just documents current token
    backend._token = 1
    _deliver_line(backend, process, {
        "event": "position_changed", "token": 1,
        "position_ms": 4200, "duration_ms": 9000,
    })
    assert backend.position_ms() == 4200
    assert backend.duration_ms() == 9000


def test_attach_output_embeds_once_ready_and_reuses_container(monkeypatch):
    _patch_fake_window_embedding(monkeypatch)
    backend, process = _make_backend()
    host_a = QtWidgets.QWidget()
    host_a.setLayout(QtWidgets.QVBoxLayout())
    host_b = QtWidgets.QWidget()
    host_b.setLayout(QtWidgets.QVBoxLayout())

    # Before "ready", the request just queues -- no container yet.
    backend.attach_output(host_a)
    assert backend._embedded_container is None

    _deliver_line(backend, process, {"event": "ready", "win_id": int(host_a.winId())})
    assert backend._embedded_container is not None
    container = backend._embedded_container
    assert host_a.layout().indexOf(container) != -1

    # Moving to a different host reuses the same container -- never a
    # second embedded window/child process.
    backend.attach_output(host_b)
    assert backend._embedded_container is container
    assert host_b.layout().indexOf(container) != -1
    assert host_a.layout().indexOf(container) == -1
    backend.shutdown()
    QtWidgets.QApplication.processEvents()


def test_double_click_and_escape_events_forwarded_regardless_of_token():
    backend, process = _make_backend()
    backend._token = 5  # simulate a load() having already happened
    double_clicks = []
    escapes = []
    backend.double_clicked.connect(lambda: double_clicks.append(1))
    backend.escape_pressed.connect(lambda: escapes.append(1))
    # These are raw UI input events forwarded from the embedded child
    # window (see video_subprocess.py) -- untagged, unlike playback events.
    _deliver_line(backend, process, {"event": "double_clicked"})
    _deliver_line(backend, process, {"event": "escape_pressed"})
    assert double_clicks == [1]
    assert escapes == [1]


def test_container_resize_sends_resize_command_to_child(monkeypatch):
    _patch_fake_window_embedding(monkeypatch)
    backend, process = _make_backend()
    host = QtWidgets.QWidget()
    host.setLayout(QtWidgets.QVBoxLayout())
    backend.attach_output(host)
    _deliver_line(backend, process, {"event": "ready", "win_id": int(host.winId())})
    process.reset_mock()

    container = backend._embedded_container
    # Deliver a real Qt resize event.  The old implementation's test called a
    # dynamically replaced resizeEvent method directly, which did not prove
    # that native Qt events would ever reach it in production.
    container.resize(800, 450)
    QtWidgets.QApplication.processEvents()

    commands = _sent_commands(process)
    resize_commands = [c for c in commands if c.get("cmd") == "resize"]
    assert resize_commands, f"expected a resize command, got {commands}"
    last = resize_commands[-1]
    assert last["width"] == 800
    assert last["height"] == 450
    assert last["device_pixel_ratio"] == container.devicePixelRatioF()
    backend.shutdown()
    QtWidgets.QApplication.processEvents()


def test_end_of_media_event_emits_signal():
    backend, process = _make_backend()
    backend._token = 1
    events = []
    backend.end_of_media.connect(lambda: events.append("end"))
    _deliver_line(backend, process, {"event": "end_of_media", "token": 1})
    assert events == ["end"]


def test_stale_token_events_are_ignored():
    backend, process = _make_backend()
    positions = []
    backend.position_changed.connect(positions.append)
    backend._token = 1
    _deliver_line(backend, process, {"event": "position_changed", "token": 1, "position_ms": 1000})
    assert positions == [1000]
    # A second load() bumps the token -- a late event tagged with the old
    # token must not be treated as belonging to the new load.
    backend._token = 2
    _deliver_line(backend, process, {"event": "position_changed", "token": 1, "position_ms": 2000})
    assert positions == [1000]
    _deliver_line(backend, process, {"event": "position_changed", "token": 2, "position_ms": 3000})
    assert positions == [1000, 3000]


def test_error_event_emits_error_signal():
    backend, process = _make_backend()
    backend._token = 1
    errors = []
    backend.error.connect(lambda category, message: errors.append(category))
    _deliver_line(backend, process, {"event": "error", "token": 1, "category": ERROR_RESOURCE, "message": "boom"})
    assert errors == [ERROR_RESOURCE]


def test_playback_state_events_emit_started_paused_stopped():
    backend, process = _make_backend()
    backend._token = 1
    events = []
    backend.started.connect(lambda: events.append("started"))
    backend.paused.connect(lambda: events.append("paused"))
    backend.stopped.connect(lambda: events.append("stopped"))
    _deliver_line(backend, process, {"event": "started", "token": 1})
    _deliver_line(backend, process, {"event": "paused", "token": 1})
    _deliver_line(backend, process, {"event": "stopped", "token": 1})
    assert events == ["started", "paused", "stopped"]
    assert backend.is_playing() is False  # last event was "stopped"


def test_process_start_failure_emits_subprocess_error_and_schedules_restart():
    backend, process = _make_backend()
    errors = []
    backend.error.connect(lambda category, message: errors.append(category))
    process.errorString.return_value = "No such file or directory"

    backend._on_process_error(QtCore.QProcess.ProcessError.FailedToStart)

    assert errors == [ERROR_SUBPROCESS]
    assert backend._restart_scheduled is True
    assert backend._restart_attempts == 1


def test_errorOccurred_and_finished_for_same_crash_report_only_once():
    # Qt commonly fires both signals for the same crash -- only the first
    # may turn into a user-visible error.
    backend, process = _make_backend()
    errors = []
    backend.error.connect(lambda category, message: errors.append(category))
    process.errorString.return_value = "Crashed"

    backend._on_process_error(QtCore.QProcess.ProcessError.Crashed)
    backend._on_process_finished(1, QtCore.QProcess.ExitStatus.CrashExit)

    assert errors == [ERROR_SUBPROCESS]


def test_process_crash_restarts_via_factory_and_reattaches_pending_target(monkeypatch):
    _patch_fake_window_embedding(monkeypatch)
    process1 = MagicMock(spec=QtCore.QProcess)
    process1.state.return_value = QtCore.QProcess.ProcessState.Running
    process1.errorString.return_value = "Crashed"
    process2 = MagicMock(spec=QtCore.QProcess)
    process2.state.return_value = QtCore.QProcess.ProcessState.Running

    factory_calls = []

    def factory():
        factory_calls.append(1)
        return process2

    _app()
    backend = QtVideoPlaybackBackend(
        process=process1, auto_start=False, process_factory=factory,
    )
    host = QtWidgets.QWidget()
    host.setLayout(QtWidgets.QVBoxLayout())
    backend.attach_output(host)
    _deliver_line(backend, process1, {"event": "ready", "win_id": int(host.winId())})
    assert backend._embedded_container is not None
    old_container = backend._embedded_container

    # The child dies -- the dead container is discarded, a restart is
    # scheduled (deferred via QTimer in the real app; called directly here
    # for a deterministic test), and the new process is wired up the same
    # way, remembering where to reattach once it's ready again.
    backend._on_process_finished(1, QtCore.QProcess.ExitStatus.CrashExit)
    assert backend._embedded_container is None
    assert factory_calls == []  # not yet -- restart is scheduled, not immediate
    backend._perform_restart()

    assert factory_calls == [1]
    assert backend._process is process2

    _deliver_line(backend, process2, {"event": "ready", "win_id": 999})
    assert backend._embedded_container is not None
    assert backend._embedded_container is not old_container
    assert host.layout().indexOf(backend._embedded_container) != -1
    backend.shutdown()
    QtWidgets.QApplication.processEvents()


def test_restart_attempts_are_capped_to_avoid_a_crash_loop():
    process_count = {"n": 0}

    def factory():
        process_count["n"] += 1
        p = MagicMock(spec=QtCore.QProcess)
        p.state.return_value = QtCore.QProcess.ProcessState.Running
        p.errorString.return_value = "Crashed"
        return p

    _app()
    first = factory()
    backend = QtVideoPlaybackBackend(
        process=first, auto_start=False, process_factory=factory,
    )
    for _ in range(6):
        backend._on_process_finished(1, QtCore.QProcess.ExitStatus.CrashExit)
        if backend._restart_scheduled:
            backend._perform_restart()

    # `first` (1 call) + at most 3 restarts via the factory = 4, never an
    # unbounded crash loop.
    assert process_count["n"] <= 4
    assert backend._restart_attempts <= 3


def test_ready_after_restart_resets_crash_reporting_and_attempts(monkeypatch):
    _patch_fake_window_embedding(monkeypatch)
    backend, process = _make_backend()
    process.errorString.return_value = "Crashed"
    backend._on_process_error(QtCore.QProcess.ProcessError.Crashed)
    assert backend._crash_reported is True
    assert backend._restart_attempts == 1

    _deliver_line(backend, process, {"event": "ready", "win_id": 1})

    assert backend._crash_reported is False
    assert backend._restart_attempts == 0
    backend.shutdown()
    QtWidgets.QApplication.processEvents()


def test_load_returns_false_when_process_not_running(tmp_path):
    backend, process = _make_backend()
    process.state.return_value = QtCore.QProcess.ProcessState.NotRunning
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"x")

    result = backend.load(str(video_path))

    assert result is False


def test_embedding_failure_when_fromwinid_returns_none_reports_error():
    # Under the offscreen platform used for tests, QWindow.fromWinId()
    # genuinely returns None (no real platform windows exist) -- this is
    # exactly the condition _create_embedded_container() must detect and
    # report rather than silently proceeding with no window.
    backend, process = _make_backend()
    errors = []
    backend.error.connect(lambda category, message: errors.append(category))

    _deliver_line(backend, process, {"event": "ready", "win_id": 12345})

    assert errors == [ERROR_SUBPROCESS]
    assert backend._embedded_container is None


def test_context_menu_requested_event_forwarded():
    backend, process = _make_backend()
    events = []
    backend.context_menu_requested.connect(lambda: events.append(1))
    _deliver_line(backend, process, {"event": "context_menu_requested"})
    assert events == [1]


def test_shutdown_sends_shutdown_command_and_is_idempotent():
    backend, process = _make_backend()
    backend.shutdown()
    commands = _sent_commands(process)
    assert {"cmd": "shutdown"} in commands
    # Closing our end of stdin is what lets the child's reader thread see
    # EOF and exit cleanly even if the shutdown command itself is lost.
    process.closeWriteChannel.assert_called_once()
    process.waitForFinished.assert_called()
    # Second call must not raise or double-act.
    write_calls_before = process.write.call_count
    backend.shutdown()
    assert process.write.call_count == write_calls_before


def test_operations_after_shutdown_are_safe_no_ops(tmp_path):
    backend, process = _make_backend()
    backend.shutdown()
    process.reset_mock()
    process.state.return_value = QtCore.QProcess.ProcessState.Running
    assert backend.load(str(tmp_path / "clip.mp4")) is False
    backend.play()
    backend.pause()
    backend.resume()
    backend.seek(1000)
    backend.set_volume(50)
    backend.set_muted(True)
    process.write.assert_not_called()


def test_stale_event_after_shutdown_is_ignored():
    backend, process = _make_backend()
    backend._token = 1
    events = []
    backend.end_of_media.connect(lambda: events.append("end"))
    backend.shutdown()
    _deliver_line(backend, process, {"event": "end_of_media", "token": 1})
    assert events == []


# -- Phase 2A: experimental dual-video cross-dissolve IPC -------------------

def _make_dual_backend(mode="gpu"):
    _app()
    process = MagicMock(spec=QtCore.QProcess)
    process.state.return_value = QtCore.QProcess.ProcessState.Running
    process.waitForFinished.return_value = True
    backend = QtVideoPlaybackBackend(process=process, auto_start=False, dual_mode=mode)
    return backend, process


def test_subprocess_launch_command_appends_dual_deck_flags():
    from billsmusic.video_subprocess import main as _unused  # module import sanity
    from billsmusic.video_backend import _subprocess_launch_command
    _program, classic_args = _subprocess_launch_command(dual_mode=None)
    _program, cpu_args = _subprocess_launch_command(dual_mode="cpu")
    _program, gpu_args = _subprocess_launch_command(dual_mode="gpu")
    assert "--dual-deck" not in classic_args and "--dual-deck-gpu" not in classic_args
    assert "--dual-deck" in cpu_args and "--dual-deck-gpu" not in cpu_args
    assert "--dual-deck-gpu" in gpu_args and cpu_args != gpu_args


def test_subprocess_launch_command_probe_only_appends_flag():
    from billsmusic.video_backend import _subprocess_launch_command
    _program, args = _subprocess_launch_command(dual_mode="gpu", probe_only=True)
    assert "--dual-deck-gpu" in args
    assert "--probe-only" in args


def test_subprocess_launch_command_rejects_unknown_mode():
    import pytest as _pytest
    from billsmusic.video_backend import QtVideoPlaybackBackend as _Backend
    _app()
    process = MagicMock(spec=QtCore.QProcess)
    process.state.return_value = QtCore.QProcess.ProcessState.Running
    backend = _Backend(process=process, auto_start=False, dual_mode=None)
    with _pytest.raises(ValueError):
        backend.set_dual_mode("not-a-real-mode")


def test_dual_commands_are_no_ops_outside_dual_mode(tmp_path):
    backend, process = _make_backend()  # classic mode
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"x")
    assert backend.preload_secondary(str(video_path)) is False
    assert backend.commit_dual_transition(1000) is False
    backend.cancel_secondary()
    backend.pause_dual_transition()
    backend.resume_dual_transition()
    process.write.assert_not_called()


def test_preload_secondary_sends_command_in_dual_mode(tmp_path):
    backend, process = _make_dual_backend()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"x")
    assert backend.preload_secondary(str(video_path)) is True
    commands = _sent_commands(process)
    assert len(commands) == 1
    assert commands[0]["cmd"] == "preload_secondary"
    assert commands[0]["path"] == str(video_path)
    assert commands[0]["identity_hash"]


def test_preload_secondary_missing_file_declines_without_sending(tmp_path):
    backend, process = _make_dual_backend()
    assert backend.preload_secondary(str(tmp_path / "missing.mp4")) is False
    process.write.assert_not_called()


def test_cancel_commit_pause_resume_dual_commands(tmp_path):
    backend, process = _make_dual_backend()
    backend.cancel_secondary()
    assert backend.commit_dual_transition(750) is True
    backend.pause_dual_transition()
    backend.resume_dual_transition()
    assert _sent_commands(process) == [
        {"cmd": "cancel_secondary"},
        {
            "cmd": "commit_dual_transition", "transition_id": 1, "duration_ms": 750,
            "transition_type": "Cross Dissolve", "seed": 0.0,
            "audio_crossfade_enabled": False, "audio_crossfade_curve": "Equal Power",
        },
        {"cmd": "pause_dual_transition"},
        {"cmd": "resume_dual_transition"},
    ]


def test_commit_dual_transition_sends_requested_effect(tmp_path):
    backend, process = _make_dual_backend()
    assert backend.commit_dual_transition(400, "Push Left") is True
    assert _sent_commands(process) == [
        {
            "cmd": "commit_dual_transition", "transition_id": 1, "duration_ms": 400,
            "transition_type": "Push Left", "seed": 0.0,
            "audio_crossfade_enabled": False, "audio_crossfade_curve": "Equal Power",
        },
    ]


def test_commit_dual_transition_sends_requested_seed(tmp_path):
    backend, process = _make_dual_backend()
    assert backend.commit_dual_transition(400, "RGB Glitch", 0.6789) is True
    assert _sent_commands(process) == [
        {
            "cmd": "commit_dual_transition", "transition_id": 1, "duration_ms": 400,
            "transition_type": "RGB Glitch", "seed": 0.6789,
            "audio_crossfade_enabled": False, "audio_crossfade_curve": "Equal Power",
        },
    ]


def test_secondary_ready_and_failed_events_emit_signals(tmp_path):
    # Both events now carry the validated envelope (see video_backend.py's
    # _accept_preload_event, added 2026-08-24) -- a real preload_secondary()
    # call is required first so the delivered events' preload_id/source_hash
    # actually match what this backend currently considers active.
    backend, process = _make_dual_backend()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"x")
    assert backend.preload_secondary(str(video_path)) is True
    preload_id = backend.active_preload_id
    source_hash = backend.active_preload_source_hash
    ready_events = []
    failed_reasons = []
    backend.secondary_ready.connect(lambda envelope: ready_events.append(envelope))
    backend.secondary_failed.connect(
        lambda envelope: failed_reasons.append(envelope.get("reason"))
    )
    envelope = {
        "preload_id": preload_id, "source_hash": source_hash,
        "deck_index": 1, "primary_index": 0, "secondary_index": 1,
    }
    _deliver_line(backend, process, {**envelope, "event": "secondary_ready"})
    _deliver_line(backend, process, {
        **envelope, "event": "secondary_failed", "reason": "video_decode_error",
    })
    assert len(ready_events) == 1 and ready_events[0]["preload_id"] == preload_id
    assert failed_reasons == ["video_decode_error"]


def test_dual_transition_complete_only_emits_for_matching_transition_id(tmp_path):
    # transition_id is now the authoritative identity (see video_backend.py's
    # _handle_event dual_transition_complete branch, added 2026-08-24) -- a
    # real commit_dual_transition() call is required first.
    backend, process = _make_dual_backend()
    assert backend.commit_dual_transition(1000) is True
    transition_id = backend.active_transition_id
    backend._token = 5
    completions = []
    backend.dual_transition_complete.connect(completions.append)
    _deliver_line(backend, process, {
        "event": "dual_transition_complete", "token": 5,
        "transition_id": transition_id - 1 if transition_id else 999,
    })
    assert completions == []  # stale transition_id, ignored
    _deliver_line(backend, process, {
        "event": "dual_transition_complete", "token": 5, "transition_id": transition_id,
    })
    assert completions == [transition_id]


def test_diagnostic_only_dual_events_do_not_raise_or_emit_signals():
    backend, process = _make_dual_backend()
    ready_events = []
    backend.secondary_ready.connect(lambda envelope: ready_events.append(envelope))
    # Must not raise and must not be misinterpreted as secondary_ready.
    _deliver_line(backend, process, {"event": "secondary_loading_started"})
    _deliver_line(backend, process, {"event": "secondary_first_frame"})
    _deliver_line(backend, process, {
        "event": "compositor_paint_timing", "avg_paint_ms": 1.2, "frame_count": 30,
    })
    _deliver_line(backend, process, {"event": "dual_transition_committed"})
    assert ready_events == []


def test_set_dual_mode_restarts_process_only_on_actual_change():
    process1 = MagicMock(spec=QtCore.QProcess)
    process1.state.return_value = QtCore.QProcess.ProcessState.Running
    process2 = MagicMock(spec=QtCore.QProcess)
    process2.state.return_value = QtCore.QProcess.ProcessState.Running

    processes = [process2]

    def factory():
        return processes.pop(0)

    _app()
    backend = QtVideoPlaybackBackend(
        process=process1, auto_start=False, process_factory=factory, dual_mode=None,
    )
    backend.set_dual_mode(None)  # no-op, same mode
    assert backend._process is process1

    backend.set_dual_mode("gpu")
    assert backend.is_dual_mode() is True
    assert backend.dual_compositor_mode() == "gpu"
    assert backend._process is process2
    # The old process is fully stopped (shutdown command, write channel
    # closed, waited for) -- not just closed -- before the new one starts.
    # See _stop_process_gracefully()'s docstring: the two must never run
    # concurrently, which the old close()-after-launch order allowed.
    sent = _sent_commands(process1)
    assert {"cmd": "shutdown"} in sent
    process1.closeWriteChannel.assert_called_once()
    process1.deleteLater.assert_called_once()

    backend.shutdown()
    QtWidgets.QApplication.processEvents()


def test_set_dual_mode_unwires_old_process_before_closing_it():
    # Regression test: set_dual_mode() used to close() the old, still-
    # running process without disconnecting its errorOccurred/finished
    # signals first. Since those signals were still connected to this same
    # backend's crash handlers (and self._shutdown is False during a mode
    # switch, unlike a real shutdown()), Qt's genuine errorOccurred(Crashed)
    # for that *intentional* close was misread as a fresh crash of whatever
    # self._process pointed at by then, scheduling a restart that killed
    # the brand new replacement process before it reached "ready" --
    # cascading through all 3 restart attempts in about a second (confirmed
    # live: one set_dual_mode("gpu") call produced 4 subprocess launches).
    # The fix disconnects the old process's signals before it is closed, so
    # its termination can no longer reach the crash-handling path at all.
    process1 = MagicMock(spec=QtCore.QProcess)
    process1.state.return_value = QtCore.QProcess.ProcessState.Running
    process2 = MagicMock(spec=QtCore.QProcess)
    process2.state.return_value = QtCore.QProcess.ProcessState.Running

    _app()
    backend = QtVideoPlaybackBackend(
        process=process1, auto_start=False,
        process_factory=lambda: process2, dual_mode=None,
    )

    backend.set_dual_mode("gpu")

    assert backend._process is process2
    process1.errorOccurred.disconnect.assert_called_once_with(backend._on_process_error)
    process1.finished.disconnect.assert_called_once_with(backend._on_process_finished)
    # readyReadStandardOutput is now connected via a per-process lambda
    # (see video_backend.py's _wire_process, added 2026-08-24), so
    # _unwire_process disconnects it with a bare disconnect() rather than
    # naming a specific slot -- there is only ever one connection on this
    # specific process's signal.
    process1.readyReadStandardOutput.disconnect.assert_called_once_with()
    process1.started.disconnect.assert_called_once_with(backend._on_process_started)
    # process2 (the new, live process) must remain fully wired.
    process2.errorOccurred.disconnect.assert_not_called()

    backend.shutdown()
    QtWidgets.QApplication.processEvents()


def test_perform_restart_unwires_old_process_before_deleting_it():
    # Same fix, applied to the existing crash-restart path: once a genuine
    # crash has already been handled and a restart performed, the dead old
    # process must not be able to fire a second, stray signal into the
    # handlers now watching the new process.
    process1 = MagicMock(spec=QtCore.QProcess)
    process1.state.return_value = QtCore.QProcess.ProcessState.Running
    process2 = MagicMock(spec=QtCore.QProcess)
    process2.state.return_value = QtCore.QProcess.ProcessState.Running

    _app()
    backend = QtVideoPlaybackBackend(
        process=process1, auto_start=False,
        process_factory=lambda: process2, dual_mode=None,
    )

    backend._on_process_finished(1, QtCore.QProcess.ExitStatus.CrashExit)
    backend._perform_restart()

    assert backend._process is process2
    process1.errorOccurred.disconnect.assert_called_once_with(backend._on_process_error)
    process1.finished.disconnect.assert_called_once_with(backend._on_process_finished)

    backend.shutdown()
    QtWidgets.QApplication.processEvents()


def test_set_dual_mode_no_op_after_shutdown():
    backend, process = _make_backend()
    backend.shutdown()
    process.reset_mock()
    backend.set_dual_mode("gpu")
    assert backend.is_dual_mode() is False
    assert backend.dual_compositor_mode() is None


# -- GpuCompositorProbe -------------------------------------------------

def _make_probe(factory=None):
    from billsmusic.video_backend import GpuCompositorProbe
    _app()
    return GpuCompositorProbe(process_factory=factory)


def _mock_probe_process():
    process = MagicMock(spec=QtCore.QProcess)
    process.state.return_value = QtCore.QProcess.ProcessState.Running
    return process


def test_probe_launches_via_the_supplied_process_factory():
    process = _mock_probe_process()
    probe = _make_probe(factory=lambda: process)
    probe.start()
    process.start.assert_called_once()


def test_probe_default_factory_uses_gpu_probe_only_launch_command():
    from billsmusic.video_backend import _subprocess_launch_command
    probe = _make_probe()  # default factory, real (unstarted) QProcess
    process = probe._process_factory()
    _program, args = _subprocess_launch_command(dual_mode="gpu", probe_only=True)
    assert process.program() == _program
    assert list(process.arguments()) == args


def test_probe_reports_available_on_success_event():
    process = _mock_probe_process()
    probe = _make_probe(factory=lambda: process)
    results = []
    probe.finished.connect(lambda available, reason: results.append((available, reason)))
    probe.start()
    line = (json.dumps({"event": "gpu_compositor_available"}) + "\n").encode("utf-8")
    process.canReadLine.side_effect = [True, False]
    process.readLine.return_value = line
    probe._on_stdout_ready()
    assert results == [(True, "")]
    # The mock reports itself as still "Running" -- _complete() defensively
    # kills it rather than assuming the real child has already exited on
    # its own (it usually has, but this must not depend on that).
    process.kill.assert_called_once()


def test_probe_reports_unavailable_with_reason():
    process = _mock_probe_process()
    probe = _make_probe(factory=lambda: process)
    results = []
    probe.finished.connect(lambda available, reason: results.append((available, reason)))
    probe.start()
    line = (json.dumps({
        "event": "gpu_compositor_unavailable", "reason": "non_gpu_backend:Software",
    }) + "\n").encode("utf-8")
    process.canReadLine.side_effect = [True, False]
    process.readLine.return_value = line
    probe._on_stdout_ready()
    assert results == [(False, "non_gpu_backend:Software")]


def test_probe_reports_unavailable_if_process_exits_without_a_result():
    process = _mock_probe_process()
    probe = _make_probe(factory=lambda: process)
    results = []
    probe.finished.connect(lambda available, reason: results.append((available, reason)))
    probe.start()
    probe._on_process_finished(0, QtCore.QProcess.ExitStatus.NormalExit)
    assert results == [(False, "process_exited_without_result")]


def test_probe_only_completes_once_even_with_multiple_signals():
    process = _mock_probe_process()
    probe = _make_probe(factory=lambda: process)
    results = []
    probe.finished.connect(lambda available, reason: results.append((available, reason)))
    probe.start()
    line = (json.dumps({"event": "gpu_compositor_available"}) + "\n").encode("utf-8")
    process.canReadLine.side_effect = [True, False]
    process.readLine.return_value = line
    probe._on_stdout_ready()
    probe._on_process_finished(0, QtCore.QProcess.ExitStatus.NormalExit)
    probe._on_process_error()
    assert results == [(True, "")]
