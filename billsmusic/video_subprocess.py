"""Standalone child process for video playback, run via `python -m
billsmusic.video_subprocess`. Owns its own QApplication/QMediaPlayer,
isolated in a separate OS process (and therefore a separate GIL and
separate Qt Multimedia decoder threads) from the main application --
whatever native threading issue in Qt Multimedia was corrupting memory
when it ran alongside the main app's other background threads (Cast/
zeroconf discovery, library scanning, artwork/analysis workers, etc.)
no longer shares a process with any of that.

Protocol: line-delimited JSON over stdin (commands in) / stdout (events
out), flushed immediately. This process never touches the parent's Qt
objects directly -- the parent embeds this process's video widget by
window ID (see attach_output in video_backend.py).
"""
from __future__ import annotations

import hashlib
import json
import math
import struct
import sys
import time

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtMultimedia import (
    QAudioBufferOutput, QAudioFormat, QAudioOutput, QMediaDevices,
    QMediaPlayer, QVideoFrame, QVideoSink,
)
from PyQt6.QtMultimediaWidgets import QVideoWidget

_QT_ERROR_TO_CATEGORY = {
    QMediaPlayer.Error.ResourceError: "video_resource_error",
    QMediaPlayer.Error.FormatError: "video_format_unsupported",
    QMediaPlayer.Error.NetworkError: "video_resource_error",
    QMediaPlayer.Error.AccessDeniedError: "video_resource_error",
}

# Same mapping as _QT_ERROR_TO_CATEGORY above, keyed by the plain int values
# QMediaPlayer.Error's members carry -- needed because GpuDualDeckVideoSubprocessController
# receives these as plain ints via its QML bridge, not as the enum objects
# themselves (see that class's module-level docstring comment on why).
_QT_ERROR_INT_TO_CATEGORY = {
    1: "video_resource_error",   # ResourceError
    2: "video_format_unsupported",  # FormatError
    3: "video_resource_error",   # NetworkError
    4: "video_resource_error",   # AccessDeniedError
}
_MEDIA_STATUS_BUFFERED = 5
_MEDIA_STATUS_END_OF_MEDIA = 6
_PLAYBACK_STATE_STOPPED = 0
_PLAYBACK_STATE_PLAYING = 1
_PLAYBACK_STATE_PAUSED = 2

# Human-readable names for the bounded deck-state diagnostics added to
# GpuDualDeckVideoSubprocessController (see that class's __init__ comment on
# why these come from tracked ints, not a live property read). Full
# QMediaPlayer.MediaStatus/PlaybackState enum, not just the two values this
# file already had names for above.
_MEDIA_STATUS_NAMES = {
    0: "NoMedia", 1: "LoadingMedia", 2: "LoadedMedia", 3: "StalledMedia",
    4: "BufferingMedia", 5: "BufferedMedia", 6: "EndOfMedia", 7: "InvalidMedia",
}
_PLAYBACK_STATE_NAMES = {0: "StoppedState", 1: "PlayingState", 2: "PausedState"}

# Phase 2B -- maps a GPU transition effect name (as chosen in Preferences /
# video_dual_transition.py's GPU_TRANSITION_EFFECTS, human-readable
# throughout the Python layer, matching Phase 1's own EFFECTS convention)
# to the plain-float uniform values video_dual_deck_blend.frag's
# transitionType/direction switch on. An effect missing from either map
# falls back to Cross Dissolve / no direction, so an unrecognised value
# (e.g. a future effect requested by a newer config against an older
# build) degrades to the safe default rather than raising.
_GPU_EFFECT_TRANSITION_TYPE = {
    "Cross Dissolve": 0.0,
    "Push Left": 1.0,
    "Push Right": 1.0,
    "Wipe Left": 2.0,
    "Wipe Right": 2.0,
    "Zoom": 3.0,
    "RGB Glitch": 4.0,
    "Pixel Dissolve": 5.0,
    "Luma Dissolve": 6.0,
    "Film Burn": 7.0,
    "Zoom Blur": 8.0,
    "Diagonal Wipe": 9.0,
}
_GPU_EFFECT_DIRECTION = {
    "Push Left": 1.0,
    "Push Right": -1.0,
    "Wipe Left": 1.0,
    "Wipe Right": -1.0,
    # Top-left -> bottom-right -- the only direction currently exposed in
    # Preferences, though the shader supports the reverse too (see
    # video_dual_deck_blend.frag's diagonalWipe()) since it came for free
    # from the same direction uniform push/wipe already use.
    "Diagonal Wipe": 1.0,
}


def _emit(obj: dict):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _hash_label(label) -> str:
    """Anonymises a device description/id for diagnostics -- callers never
    log the raw string (see the classic-audio-silence investigation,
    2026-08-31 Codex audit, section 4's "do not log raw device IDs or
    paths"). No session salt (unlike performance_diagnostics.py's own
    path_details(), which this child process has no access to -- it never
    owns a diagnostics session, only ever emits events for the parent to
    record, see this module's own docstring) -- just enough to let two
    hashes be compared for equality across parent/child without exposing
    the underlying device name."""
    if not label:
        return ""
    if isinstance(label, (bytes, bytearray, QtCore.QByteArray)):
        raw = bytes(label)
    else:
        raw = str(label).encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:16]


def _extract_audio_buffer_evidence(buf) -> "dict | None":
    """Bounded, one-shot-per-load decoded-audio evidence from a real
    QAudioBuffer (classic-audio-silence investigation, section 6) -- proves
    "buffers received, non-zero sample count, bounded peak/RMS", which is
    strictly more than hasAudio()/volume()/muted() can (all of those can
    look perfectly healthy while nothing audible is actually decoding).
    Mirrors the exact, already-proven sample-format handling
    video_transition_point_probe_subprocess.py's _measure_silence() uses
    (struct.unpack per QAudioFormat.SampleFormat -- no numpy dependency
    needed for this). Returns None for an empty/unreadable buffer rather
    than raising -- caller treats that as "no evidence yet", not an error."""
    try:
        size = buf.byteCount()
        if size <= 0:
            return None
        data_ptr = buf.constData()
        data_ptr.setsize(size)
        raw = bytes(data_ptr.asstring(size))
        if not raw:
            return None
        fmt = buf.format()
        sf = fmt.sampleFormat()
        sample_format_names = QAudioFormat.SampleFormat
        if sf == sample_format_names.Float:
            count = len(raw) // 4
            if count == 0:
                return None
            values = struct.unpack(f"<{count}f", raw[:count * 4])
            format_name = "Float"
        elif sf == sample_format_names.Int16:
            count = len(raw) // 2
            if count == 0:
                return None
            values = [s / 32768.0 for s in struct.unpack(f"<{count}h", raw[:count * 2])]
            format_name = "Int16"
        elif sf == sample_format_names.Int32:
            count = len(raw) // 4
            if count == 0:
                return None
            values = [s / 2147483648.0 for s in struct.unpack(f"<{count}i", raw[:count * 4])]
            format_name = "Int32"
        elif sf == sample_format_names.UInt8:
            values = [(b - 128) / 128.0 for b in raw]
            format_name = "UInt8"
        else:
            return {
                "sample_count": buf.sampleCount(), "format": "Unknown",
                "peak": None, "rms": None, "non_zero": None,
            }
        if not values:
            return None
        peak = max(abs(v) for v in values)
        rms = math.sqrt(sum(v * v for v in values) / len(values))
        return {
            "sample_count": len(values), "format": format_name,
            "peak": round(peak, 6), "rms": round(rms, 6),
            "non_zero": bool(peak > 1e-6),
        }
    except Exception:
        return None


class _StdinReaderThread(QtCore.QThread):
    """QSocketNotifier only works with sockets on Windows, not pipes/stdin,
    so stdin is read here via a plain blocking-read background thread
    instead; each line is delivered to the GUI thread via a queued
    signal connection (thread-safe, no direct Qt-object access here)."""
    line_received = QtCore.pyqtSignal(str)
    closed = QtCore.pyqtSignal()

    def run(self):
        while True:
            line = sys.stdin.readline()
            if not line:
                self.closed.emit()
                return
            line = line.strip()
            if line:
                self.line_received.emit(line)


class VideoSubprocessController(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.widget = QVideoWidget()
        self.widget.setStyleSheet("background:#000000;")
        self.widget.setAspectRatioMode(QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        self.widget.resize(640, 360)
        # Mouse/keyboard input over the embedded picture is delivered to
        # this process (it's the actual native window under the cursor,
        # regardless of which process owns it) -- the parent's own
        # double-click/Escape handlers on its container widget never see
        # these, so double-click-to-fullscreen has to be caught here and
        # forwarded over the IPC protocol instead.
        self.widget.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

        def _handle_double_click(event, _default=self.widget.mouseDoubleClickEvent):
            _emit({"event": "double_clicked"})

        def _handle_key_press(event, _default=self.widget.keyPressEvent):
            if event.key() == QtCore.Qt.Key.Key_Escape:
                _emit({"event": "escape_pressed"})
            else:
                _default(event)

        def _handle_mouse_press(event, _default=self.widget.mousePressEvent):
            if event.button() == QtCore.Qt.MouseButton.RightButton:
                _emit({"event": "context_menu_requested"})
            else:
                _default(event)

        self.widget.mouseDoubleClickEvent = _handle_double_click
        self.widget.keyPressEvent = _handle_key_press
        self.widget.mousePressEvent = _handle_mouse_press
        # QVideoWidget's compositing surface (D3D/GPU-backed on Windows)
        # isn't fully realized by winId() alone -- it needs an actual
        # show/expose cycle, or the parent's createWindowContainer() ends
        # up embedding a window that never paints anything (a known Qt
        # cross-process embedding gotcha: "black frame"/blank container
        # when the source window was never shown before being embedded).
        # Moved off-screen first so nothing flashes on this process's own
        # desktop before the parent reparents it into its own UI.
        self.widget.move(-32000, -32000)
        self.widget.show()

        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.widget)
        # Real-device CONFIRMED root cause (2026-08-31 Codex audit,
        # follow-up round): self.audio_output above binds to whatever Qt's
        # default output device is at *this exact construction moment*
        # (this process's own startup) and never updates again on its own
        # -- Qt Multimedia does not follow endpoint changes automatically.
        # Real r4 acceptance diagnostics proved this exactly: device_hash
        # matched default_output_device_hash at child_startup, then
        # diverged by the time video actually played, with has_audio=true/
        # actual_volume=0.7/actual_muted=false all still reporting
        # "healthy" throughout -- nothing about the AudioOutput's own
        # state signals this at all, only comparing its device() against a
        # *fresh* QMediaDevices.defaultAudioOutput() call does. Empirically
        # confirmed safe in this Qt 6.11 build: QAudioOutput.setDevice()
        # while genuinely PlayingState keeps playing (position continues
        # advancing, no error, volume/muted untouched by the switch) -- no
        # pause/reload/restart needed. See _reconcile_audio_output_device()
        # (checked before every load) and the audioOutputsChanged
        # subscription below (checked live during playback too).
        self._media_devices_monitor = QMediaDevices()
        self._media_devices_monitor.audioOutputsChanged.connect(
            lambda: self._reconcile_audio_output_device("device_list_changed")
        )
        # Classic-audio-silence investigation (2026-08-31 Codex audit,
        # section 6): a QAudioBufferOutput coexists with the real
        # QAudioOutput above -- Qt Multimedia routes decoded PCM to both
        # independently, so this is pure observation, never a second
        # playback path (see _extract_audio_buffer_evidence's own
        # docstring and video_transition_point_probe_subprocess.py's
        # _measure_silence for the proven precedent this mirrors). Bounded
        # to one evidence emission per load -- never per-buffer/per-frame.
        self._audio_buffer_output = QAudioBufferOutput()
        self.player.setAudioBufferOutput(self._audio_buffer_output)
        self._audio_buffer_output.audioBufferReceived.connect(
            self._on_audio_buffer_received
        )
        self._decoded_audio_evidence_token = None
        self._latest_audio_buffer_evidence = None

        self.player.mediaStatusChanged.connect(self._on_media_status_changed)
        self.player.errorOccurred.connect(self._on_error_occurred)
        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.playbackStateChanged.connect(self._on_playback_state_changed)

        self._token = None
        # One-shot "steady playback" checkpoint (section 4), a couple of
        # seconds into confirmed PlayingState -- re-armed per load, and
        # never fires for a load this process has since moved on from
        # (guarded by token, exactly like every other post-load event).
        self._steady_state_timer = QtCore.QTimer(self)
        self._steady_state_timer.setSingleShot(True)
        self._steady_state_timer.timeout.connect(self._emit_steady_state_snapshot)

        self._reader = _StdinReaderThread()
        self._reader.line_received.connect(self._on_line_received)
        self._reader.closed.connect(lambda: QtWidgets.QApplication.instance().quit())
        self._reader.start()

    def announce_ready(self):
        # winId() forces native window creation; the parent embeds this by
        # ID via QWindow.fromWinId() + createWindowContainer(). Widget is
        # never shown by this process itself -- the parent controls that
        # by reparenting the embedded container into its own layout.
        win_id = int(self.widget.winId())
        _emit({"event": "ready", "win_id": win_id})
        self._emit_audio_state("child_startup")

    # -- classic-audio-silence investigation diagnostics (2026-08-31 Codex
    # audit, sections 4-6) --------------------------------------------------
    def _audio_state_snapshot(self, checkpoint: str) -> dict:
        """Everything requested for the classic-audio-silence investigation
        that this Qt 6.11/PyQt6 build's real, verified API surface can
        answer (see this module's own version check, not invented):
        hasAudio, mediaStatus, playbackState, error, whether the attached
        QAudioOutput is genuinely the one object this process constructed,
        its actual volume()/isMuted(), the actual QAudioDevice it's using
        (isNull/isDefault, plus an anonymised hash), and a hash of the
        system's current default output device for comparison -- answers
        section 7's "does Qt use a different endpoint than BASS" question
        from the Qt side; BASS's own selected device is reported
        separately (see bass_player.py)."""
        try:
            has_audio = bool(self.player.hasAudio())
        except Exception:
            has_audio = None
        try:
            media_status = self.player.mediaStatus()
            media_status_name = media_status.name
        except Exception:
            media_status_name = None
        try:
            playback_state = self.player.playbackState()
            playback_state_name = playback_state.name
        except Exception:
            playback_state_name = None
        try:
            error = self.player.error()
            error_name = error.name
        except Exception:
            error_name = None
        try:
            audio_output_attached_correctly = (
                self.player.audioOutput() is self.audio_output
            )
        except Exception:
            audio_output_attached_correctly = None
        try:
            actual_volume = round(float(self.audio_output.volume()), 4)
        except Exception:
            actual_volume = None
        try:
            actual_muted = bool(self.audio_output.isMuted())
        except Exception:
            actual_muted = None
        device_is_null = None
        device_is_default = None
        device_hash = None
        try:
            device = self.audio_output.device()
            device_is_null = bool(device.isNull())
            device_is_default = bool(device.isDefault())
            device_hash = _hash_label(device.id()) or _hash_label(device.description())
        except Exception:
            pass
        default_output_device_hash = None
        try:
            default_device = QMediaDevices.defaultAudioOutput()
            default_output_device_hash = (
                _hash_label(default_device.id()) or _hash_label(default_device.description())
            )
        except Exception:
            pass
        return {
            "checkpoint": checkpoint,
            "token": self._token,
            "has_audio": has_audio,
            "media_status": media_status_name,
            "playback_state": playback_state_name,
            "error": error_name,
            "audio_output_attached_correctly": audio_output_attached_correctly,
            "actual_volume": actual_volume,
            "actual_muted": actual_muted,
            "device_is_null": device_is_null,
            "device_is_default": device_is_default,
            "device_hash": device_hash,
            "default_output_device_hash": default_output_device_hash,
            "device_matches_default": (
                device_hash == default_output_device_hash
                if device_hash and default_output_device_hash else None
            ),
        }

    def _reconcile_audio_output_device(self, reason: str) -> None:
        """Real-device CONFIRMED fix (2026-08-31 Codex audit follow-up):
        rebind self.audio_output to Qt's *current* default output device
        if it has drifted from whatever this process happened to default
        to at some earlier moment -- see __init__'s comment for the full
        real-diagnostics evidence this responds to. Authority is the
        underlying QAudioDevice id() (a stable QByteArray), never an old
        device object's own isDefault flag (proven stale in the real
        incident) and never a description-string comparison. Safe to call
        unconditionally and often: a no-op whenever nothing has actually
        changed. Called before every load (reason="pre_load") and live,
        whenever Qt reports the audio device list itself changed
        (reason="device_list_changed", via audioOutputsChanged) --
        confirmed empirically safe to call while genuinely playing in this
        Qt 6.11 build (position keeps advancing, no error raised)."""
        try:
            current_default = QMediaDevices.defaultAudioOutput()
            if current_default.isNull():
                return
            existing = self.audio_output.device()
            if not existing.isNull() and bytes(existing.id()) == bytes(current_default.id()):
                return  # already correct -- the common case, every call
            volume = self.audio_output.volume()
            muted = self.audio_output.isMuted()
            old_hash = _hash_label(existing.id()) or _hash_label(existing.description())
            self.audio_output.setDevice(current_default)
            # Empirically confirmed setDevice() does not itself reset
            # volume/muted (see the 2026-08-31 investigation notes in
            # CODEX_HANDOFF.md) -- re-applied explicitly anyway, matching
            # the instruction to preserve both, rather than relying on
            # that undocumented-in-code behaviour continuing to hold.
            self.audio_output.setVolume(volume)
            self.audio_output.setMuted(muted)
            new_hash = _hash_label(current_default.id()) or _hash_label(current_default.description())
            _emit({
                "event": "audio_device_rebound", "token": self._token,
                "reason": reason,
                "old_device_hash": old_hash, "new_device_hash": new_hash,
                "preserved_volume": round(float(volume), 4),
                "preserved_muted": bool(muted),
            })
        except Exception:
            pass

    def _emit_audio_state(self, checkpoint: str) -> None:
        try:
            _emit({"event": "classic_audio_state", **self._audio_state_snapshot(checkpoint)})
        except Exception:
            pass

    def _emit_command_applied(self, operation: str) -> None:
        """Request/ack confirmation for a classic audio command (section
        5) -- emitted once, right after the command has actually been
        applied to the real Qt objects, carrying the resulting real state
        (not an echo of what was requested). Reuses the current load's
        token as the request/source identity -- there is no separate
        per-command request-ID protocol to invent for this single-deck
        controller (only one thing can ever be "the current load")."""
        try:
            snapshot = self._audio_state_snapshot(f"command_applied:{operation}")
            _emit({
                "event": "audio_command_applied", "operation": operation,
                **{k: v for k, v in snapshot.items() if k != "checkpoint"},
            })
        except Exception:
            pass

    def _emit_steady_state_snapshot(self) -> None:
        if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            return
        self._emit_audio_state("steady_playback")
        # Real-device follow-up (2026-08-31 Codex audit): the *early*
        # decoded-audio-evidence sample below can genuinely land while the
        # caller (window.py's mixed-transition fade, or simply the very
        # first buffer after a fresh load) still has volume ramping up
        # from zero -- a real peak=0/rms=0/non_zero=false there proved
        # nothing about whether the file itself has silent audio, and was
        # previously the *only* sample taken. This second, steady-state
        # sample (same ~2s checkpoint as the audio-state snapshot above,
        # well past any startup fade) is what actually answers "is this
        # file's audio genuinely decoding to non-zero samples" -- reuses
        # whatever buffer most recently arrived rather than waiting for a
        # fresh one at this exact instant, since buffers stream in several
        # times a second during ordinary playback.
        if self._latest_audio_buffer_evidence is not None:
            try:
                _emit({
                    "event": "decoded_audio_evidence", "token": self._token,
                    "checkpoint": "steady_playback",
                    **self._latest_audio_buffer_evidence,
                })
            except Exception:
                pass

    def _on_audio_buffer_received(self, buf) -> None:
        evidence = _extract_audio_buffer_evidence(buf)
        if evidence is None:
            return
        self._latest_audio_buffer_evidence = evidence
        if self._decoded_audio_evidence_token == self._token:
            return  # already have the early sample for this load
        self._decoded_audio_evidence_token = self._token
        try:
            _emit({
                "event": "decoded_audio_evidence", "token": self._token,
                "checkpoint": "early", **evidence,
            })
        except Exception:
            pass

    # -- command handling -----------------------------------------------------
    def _on_line_received(self, line: str):
        try:
            command = json.loads(line)
        except Exception:
            return
        self._dispatch(command)

    def _dispatch(self, command: dict):
        name = command.get("cmd")
        token = command.get("token")
        if name == "load":
            self._token = token
            path = command.get("path", "")
            transport_url = command.get("transport_url", "")
            import os
            if not transport_url and (not path or not os.path.isfile(path)):
                _emit({"event": "error", "token": token, "category": "video_file_missing", "message": f"Video file not found: {path}"})
                return
            self._decoded_audio_evidence_token = None
            self._latest_audio_buffer_evidence = None
            self._steady_state_timer.stop()
            self._reconcile_audio_output_device("pre_load")
            _emit({"event": "load_requested", "token": token})
            try:
                if transport_url:
                    # Stage 3A: the only place a Plex transport URL (which
                    # may carry ?X-Plex-Token=...) is ever actually opened
                    # -- path/token/identity_hash above all stay the
                    # stable plex:// logical identity, never this URL.
                    self.player.setSource(QtCore.QUrl(transport_url))
                else:
                    self.player.setSource(QtCore.QUrl.fromLocalFile(path))
            except Exception as ex:
                from .plex_transport import sanitize_plex_text
                _emit({"event": "error", "token": token, "category": "video_unknown_error", "message": sanitize_plex_text(str(ex))})
                return
            self.player.play()
            self._emit_command_applied("load_play")
        elif name == "play":
            self.player.play()
        elif name == "pause":
            self.player.pause()
        elif name == "resume":
            self.player.play()
        elif name == "stop":
            self._token = None
            self.player.stop()
        elif name == "seek":
            self.player.setPosition(int(command.get("position_ms", 0)))
        elif name == "resize":
            # Explicit, rather than relying on the parent's window manager
            # to propagate a native resize across the process boundary --
            # that turned out not to happen reliably, leaving this widget
            # at its initial 640x360 and the embedded container showing
            # whatever's behind/around it (see video_backend.py's
            # _send_resize()).
            width = max(1, int(command.get("width", self.widget.width())))
            height = max(1, int(command.get("height", self.widget.height())))
            # The parent and child can run with different effective DPI
            # contexts even though they use the same Qt build (notably after
            # a cross-process native-window reparent on Windows).  Convert the
            # parent's logical size through physical pixels into this
            # process's logical coordinate system.
            try:
                child_dpr = max(0.1, float(self.widget.devicePixelRatioF()))
            except Exception:
                child_dpr = 1.0
            try:
                parent_dpr = max(
                    0.1,
                    float(command.get("device_pixel_ratio", child_dpr)),
                )
            except (TypeError, ValueError):
                parent_dpr = child_dpr
            width = max(1, round(width * parent_dpr / child_dpr))
            height = max(1, round(height * parent_dpr / child_dpr))
            self.widget.resize(width, height)
        elif name == "set_volume":
            volume = max(0.0, min(100.0, float(command.get("volume", 100))))
            self.audio_output.setVolume(volume / 100.0)
            self._emit_command_applied("set_volume")
        elif name == "set_muted":
            self.audio_output.setMuted(bool(command.get("muted", False)))
            self._emit_command_applied("set_muted")
        elif name == "shutdown":
            self.shutdown()

    def shutdown(self):
        self.player.stop()
        QtWidgets.QApplication.instance().quit()

    # -- Qt signal handlers -> stdout events -----------------------------------
    def _on_media_status_changed(self, status):
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            self._emit_audio_state("source_loaded")
        elif status == QMediaPlayer.MediaStatus.BufferedMedia:
            self._emit_audio_state("media_ready")
        elif status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._steady_state_timer.stop()
            self._emit_audio_state("end_of_media")
            _emit({"event": "end_of_media", "token": self._token})

    def _on_error_occurred(self, error, error_string: str):
        if error == QMediaPlayer.Error.NoError:
            return
        self._steady_state_timer.stop()
        self._emit_audio_state("error")
        category = _QT_ERROR_TO_CATEGORY.get(error, "video_decode_error")
        # Qt/FFmpeg error text can embed the failing URL verbatim -- for a
        # Plex load that URL may carry ?X-Plex-Token=... (Decision 1's
        # approved video exception), so this is sanitised before it ever
        # reaches the IPC pipe/parent log/diagnostics.
        from .plex_transport import sanitize_plex_text
        message = sanitize_plex_text(error_string) or category
        _emit({"event": "error", "token": self._token, "category": category, "message": message})

    def _on_position_changed(self, position: int):
        # Include duration on every position heartbeat. Some Windows/FFmpeg
        # combinations deliver durationChanged before the parent finishes
        # wiring/reparenting the native window; repeating it here makes the
        # progress display self-healing instead of depending on one event.
        _emit({
            "event": "position_changed",
            "token": self._token,
            "position_ms": int(position),
            "duration_ms": int(self.player.duration()),
        })

    def _on_duration_changed(self, duration: int):
        _emit({"event": "duration_changed", "token": self._token, "duration_ms": int(duration)})

    def _on_playback_state_changed(self, state):
        if state == QMediaPlayer.PlaybackState.PlayingState:
            _emit({"event": "started", "token": self._token})
            self._emit_audio_state("playback_started")
            self._steady_state_timer.start(2000)
        elif state == QMediaPlayer.PlaybackState.PausedState:
            _emit({"event": "paused", "token": self._token})
            self._steady_state_timer.stop()
        elif state == QMediaPlayer.PlaybackState.StoppedState:
            _emit({"event": "stopped", "token": self._token})
            self._steady_state_timer.stop()
            self._emit_audio_state("stop")


class _DualDeckCompositorWidget(QtWidgets.QWidget):
    """Phase 2A's proof-of-concept dual-video compositor.

    Paints a genuine two-frame cross-dissolve via ``QVideoFrame.paint()``:
    frame A drawn opaque, frame B drawn on top at ``opacity=progress`` --
    standard "source over" alpha compositing, which is exactly
    ``output = A*(1-progress) + B*progress``.

    This is deliberately NOT a rewrite of ``VideoSubprocessController``'s
    proven ``QVideoWidget`` path above (untouched by this class) -- it only
    exists in the child process when the experimental dual-transition
    preference is explicitly on (see ``main()``'s mode selection). Qt's own
    documentation notes ``QVideoFrame.paint()`` typically renders without
    hardware acceleration, unlike ``QVideoWidget``'s normal path -- an
    accepted, measured cost for a ~1 second transition behind an
    off-by-default flag; ``compositor_paint_timing`` diagnostics below make
    that cost visible rather than assumed.
    """

    def __init__(self):
        super().__init__()
        self.setStyleSheet("background:#000000;")
        self.resize(640, 360)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

        def _handle_double_click(event, _default=self.mouseDoubleClickEvent):
            _emit({"event": "double_clicked"})

        def _handle_key_press(event, _default=self.keyPressEvent):
            if event.key() == QtCore.Qt.Key.Key_Escape:
                _emit({"event": "escape_pressed"})
            else:
                _default(event)

        def _handle_mouse_press(event, _default=self.mousePressEvent):
            if event.button() == QtCore.Qt.MouseButton.RightButton:
                _emit({"event": "context_menu_requested"})
            else:
                _default(event)

        self.mouseDoubleClickEvent = _handle_double_click
        self.keyPressEvent = _handle_key_press
        self.mousePressEvent = _handle_mouse_press

        self._frame_a: Optional[QVideoFrame] = None
        self._frame_b: Optional[QVideoFrame] = None
        self._progress = 0.0
        self._paint_count = 0
        self._paint_time_total_ms = 0.0
        self._last_diag_emit = 0.0

        # Same "show before winId()" ordering as VideoSubprocessController --
        # see its __init__ docstring comment for why an unshown source
        # window embeds as a permanently blank container.
        self.move(-32000, -32000)
        self.show()

    def set_frame_a(self, frame: Optional[QVideoFrame]) -> None:
        self._frame_a = frame
        self.update()

    def set_frame_b(self, frame: Optional[QVideoFrame]) -> None:
        self._frame_b = frame
        self.update()

    def clear_frame_b(self) -> None:
        self._frame_b = None

    def frame_b(self) -> Optional[QVideoFrame]:
        return self._frame_b

    def set_progress(self, value: float) -> None:
        self._progress = max(0.0, min(1.0, float(value)))
        self.update()

    def paintEvent(self, event) -> None:
        started = time.perf_counter()
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor(0, 0, 0))
        rect = QtCore.QRectF(self.rect())
        options = QVideoFrame.PaintOptions()
        options.aspectRatioMode = QtCore.Qt.AspectRatioMode.KeepAspectRatio
        if self._frame_a is not None and self._frame_a.isValid():
            painter.setOpacity(1.0)
            self._frame_a.paint(painter, rect, options)
        if self._progress > 0.0 and self._frame_b is not None and self._frame_b.isValid():
            painter.setOpacity(self._progress)
            self._frame_b.paint(painter, rect, options)
        painter.end()
        self._record_paint_timing((time.perf_counter() - started) * 1000.0)

    def _record_paint_timing(self, elapsed_ms: float) -> None:
        # Rate-limited (~every 2s), matching the Cast clock-snapshot
        # diagnostics precedent in cast_service.py -- never per-frame.
        self._paint_count += 1
        self._paint_time_total_ms += elapsed_ms
        now = time.monotonic()
        if self._paint_count and now - self._last_diag_emit >= 2.0:
            _emit({
                "event": "compositor_paint_timing",
                "avg_paint_ms": round(self._paint_time_total_ms / self._paint_count, 3),
                "frame_count": self._paint_count,
            })
            self._last_diag_emit = now
            self._paint_count = 0
            self._paint_time_total_ms = 0.0


class DualDeckVideoSubprocessController(QtCore.QObject):
    """Experimental dual-mode child controller (Phase 2A).

    Structurally a sibling of ``VideoSubprocessController``, not a subclass
    of it -- selected once at process startup (see ``main()``) so classic
    mode's code path above is never touched or shared with this one. Deck A
    ("primary") is always the thing the parent's ordinary ``load``/``play``/
    ``pause``/``resume``/``stop``/``seek``/``resize``/``set_volume``/
    ``set_muted`` commands act on, exactly like classic mode -- the parent
    does not need to know which mode the child is running in for ordinary
    playback. Deck B ("secondary") only exists between a ``preload_secondary``
    command and either ``cancel_secondary`` or a completed
    ``commit_dual_transition``.
    """

    _SECONDARY_READY_CATEGORY = "video_dual_secondary_error"

    def __init__(self):
        super().__init__()
        self.compositor = _DualDeckCompositorWidget()

        self.player_a = QMediaPlayer()
        self.audio_output_a = QAudioOutput()
        self.sink_a = QVideoSink()
        self.player_a.setAudioOutput(self.audio_output_a)
        self.player_a.setVideoOutput(self.sink_a)
        self.sink_a.videoFrameChanged.connect(self._on_frame_a_changed)
        self.player_a.mediaStatusChanged.connect(self._on_media_status_changed)
        self.player_a.errorOccurred.connect(self._on_error_occurred)
        self.player_a.positionChanged.connect(self._on_position_changed)
        self.player_a.durationChanged.connect(self._on_duration_changed)
        self.player_a.playbackStateChanged.connect(self._on_playback_state_changed)

        self.player_b: Optional[QMediaPlayer] = None
        self.audio_output_b: Optional[QAudioOutput] = None
        self.sink_b: Optional[QVideoSink] = None
        self._secondary_first_frame_seen = False
        self._dual_animation: Optional[QtCore.QVariantAnimation] = None

        self._token = None

        self._reader = _StdinReaderThread()
        self._reader.line_received.connect(self._on_line_received)
        self._reader.closed.connect(lambda: QtWidgets.QApplication.instance().quit())
        self._reader.start()

    def announce_ready(self):
        win_id = int(self.compositor.winId())
        _emit({"event": "ready", "win_id": win_id})

    # -- command handling (primary deck -- same protocol as classic mode) --
    def _on_line_received(self, line: str):
        try:
            command = json.loads(line)
        except Exception:
            return
        self._dispatch(command)

    def _dispatch(self, command: dict):
        name = command.get("cmd")
        token = command.get("token")
        if name == "load":
            self._token = token
            path = command.get("path", "")
            import os
            if not path or not os.path.isfile(path):
                _emit({"event": "error", "token": token, "category": "video_file_missing", "message": f"Video file not found: {path}"})
                return
            try:
                self.player_a.setSource(QtCore.QUrl.fromLocalFile(path))
            except Exception as ex:
                _emit({"event": "error", "token": token, "category": "video_unknown_error", "message": str(ex)})
                return
            self.player_a.play()
        elif name == "play":
            self.player_a.play()
        elif name == "pause":
            self.player_a.pause()
        elif name == "resume":
            self.player_a.play()
        elif name == "stop":
            self._token = None
            self.player_a.stop()
        elif name == "seek":
            self.player_a.setPosition(int(command.get("position_ms", 0)))
        elif name == "resize":
            width = max(1, int(command.get("width", self.compositor.width())))
            height = max(1, int(command.get("height", self.compositor.height())))
            try:
                child_dpr = max(0.1, float(self.compositor.devicePixelRatioF()))
            except Exception:
                child_dpr = 1.0
            try:
                parent_dpr = max(0.1, float(command.get("device_pixel_ratio", child_dpr)))
            except (TypeError, ValueError):
                parent_dpr = child_dpr
            width = max(1, round(width * parent_dpr / child_dpr))
            height = max(1, round(height * parent_dpr / child_dpr))
            self.compositor.resize(width, height)
        elif name == "set_volume":
            volume = max(0.0, min(100.0, float(command.get("volume", 100))))
            self.audio_output_a.setVolume(volume / 100.0)
        elif name == "set_muted":
            self.audio_output_a.setMuted(bool(command.get("muted", False)))
        elif name == "preload_secondary":
            self._start_preload_secondary(command.get("path", ""))
        elif name == "cancel_secondary":
            self._cancel_secondary()
        elif name == "commit_dual_transition":
            self._commit_dual_transition(int(command.get("duration_ms", 1000)))
        elif name == "pause_dual_transition":
            self._pause_dual_transition()
        elif name == "resume_dual_transition":
            self._resume_dual_transition()
        elif name == "shutdown":
            self.shutdown()

    def shutdown(self):
        self._cancel_secondary()
        self.player_a.stop()
        QtWidgets.QApplication.instance().quit()

    # -- primary deck signal handlers -> stdout events -----------------------
    def _on_frame_a_changed(self, frame):
        self.compositor.set_frame_a(frame)

    def _on_media_status_changed(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            _emit({"event": "end_of_media", "token": self._token})

    def _on_error_occurred(self, error, error_string: str):
        if error == QMediaPlayer.Error.NoError:
            return
        category = _QT_ERROR_TO_CATEGORY.get(error, "video_decode_error")
        _emit({"event": "error", "token": self._token, "category": category, "message": error_string or category})

    def _on_position_changed(self, position: int):
        _emit({
            "event": "position_changed",
            "token": self._token,
            "position_ms": int(position),
            "duration_ms": int(self.player_a.duration()),
        })

    def _on_duration_changed(self, duration: int):
        _emit({"event": "duration_changed", "token": self._token, "duration_ms": int(duration)})

    def _on_playback_state_changed(self, state):
        if state == QMediaPlayer.PlaybackState.PlayingState:
            _emit({"event": "started", "token": self._token})
        elif state == QMediaPlayer.PlaybackState.PausedState:
            _emit({"event": "paused", "token": self._token})
        elif state == QMediaPlayer.PlaybackState.StoppedState:
            _emit({"event": "stopped", "token": self._token})

    # -- secondary deck (preload) --------------------------------------------
    def _start_preload_secondary(self, path: str):
        import os
        self._cancel_secondary()  # at most one secondary deck at a time
        if not path or not os.path.isfile(path):
            _emit({"event": "secondary_failed", "reason": "video_file_missing"})
            return
        _emit({"event": "secondary_loading_started"})
        self.player_b = QMediaPlayer()
        self.audio_output_b = QAudioOutput()
        self.audio_output_b.setMuted(True)  # never audible while preloading
        self.sink_b = QVideoSink()
        self.player_b.setAudioOutput(self.audio_output_b)
        self.player_b.setVideoOutput(self.sink_b)
        self._secondary_first_frame_seen = False
        self.sink_b.videoFrameChanged.connect(self._on_frame_b_changed)
        self.player_b.errorOccurred.connect(self._on_secondary_error_occurred)
        try:
            self.player_b.setSource(QtCore.QUrl.fromLocalFile(path))
        except Exception as ex:
            _emit({"event": "secondary_failed", "reason": str(ex)})
            self._cancel_secondary()
            return
        self.player_b.play()

    def _on_frame_b_changed(self, frame):
        self.compositor.set_frame_b(frame)
        if not self._secondary_first_frame_seen and frame is not None and frame.isValid():
            self._secondary_first_frame_seen = True
            _emit({"event": "secondary_first_frame"})
            _emit({"event": "secondary_ready"})

    def _on_secondary_error_occurred(self, error, error_string: str):
        if error == QMediaPlayer.Error.NoError:
            return
        _emit({"event": "secondary_failed", "reason": error_string or "video_decode_error"})
        self._cancel_secondary()

    def _cancel_secondary(self):
        self.compositor.clear_frame_b()
        self.compositor.set_progress(0.0)
        self._secondary_first_frame_seen = False
        if self.player_b is not None:
            try:
                self.player_b.stop()
            except Exception:
                pass
            try:
                self.player_b.deleteLater()
            except Exception:
                pass
        self.player_b = None
        self.audio_output_b = None
        self.sink_b = None

    # -- committed cross-dissolve --------------------------------------------
    def _commit_dual_transition(self, duration_ms: int):
        if self.player_b is None or not self._secondary_first_frame_seen:
            _emit({"event": "secondary_failed", "reason": "not_ready_at_commit"})
            return
        _emit({"event": "dual_transition_committed"})
        # Simplest safe audio handover (deliberately not a crossfade -- see
        # video_dual_transition.py's module docstring): audio ownership
        # switches the instant the visual dissolve begins, never overlapping
        # at full volume.
        self.audio_output_a.setMuted(True)
        self.audio_output_b.setMuted(False)
        animation = QtCore.QVariantAnimation(self)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setDuration(max(1, int(duration_ms)))
        animation.setEasingCurve(QtCore.QEasingCurve.Type.InOutQuad)
        animation.valueChanged.connect(lambda v: self.compositor.set_progress(float(v)))
        animation.finished.connect(self._on_dual_transition_finished)
        self._dual_animation = animation
        animation.start()

    def _pause_dual_transition(self):
        if self._dual_animation is not None:
            self._dual_animation.pause()
        self.player_a.pause()
        if self.player_b is not None:
            self.player_b.pause()

    def _resume_dual_transition(self):
        if self._dual_animation is not None:
            self._dual_animation.resume()
        self.player_a.play()
        if self.player_b is not None:
            self.player_b.play()

    def _on_dual_transition_finished(self):
        animation, self._dual_animation = self._dual_animation, None
        if animation is not None:
            try:
                animation.deleteLater()
            except Exception:
                pass
        old_player_a, old_audio_a, old_sink_a = (
            self.player_a, self.audio_output_a, self.sink_a,
        )
        # Hand the compositor a frame that stays valid once the old primary
        # is torn down below, *before* touching that old primary at all.
        # A QVideoFrame's backing buffer belongs to the decoder that
        # produced it -- repainting self._frame_a after its owning player
        # has been stopped/deleted is a genuine access violation (reproduced
        # and root-caused via faulthandler during development, not
        # hypothetical), because Qt/Windows can schedule a repaint the
        # instant set_progress()/clear_frame_b() below call update().
        self.compositor.set_frame_a(self.compositor.frame_b())
        self.compositor.clear_frame_b()
        self.compositor.set_progress(0.0)
        # Promote: the secondary deck becomes the new primary. The parent
        # was never told a new load() happened for this path (window.py
        # recognises it as already-live and skips re-issuing load -- see
        # _promote_dual_transition_track_ui), so events below keep using the
        # same self._token the parent already has cached for "the current
        # load."
        self.player_a = self.player_b
        self.audio_output_a = self.audio_output_b
        self.sink_a = self.sink_b
        self.player_b = None
        self.audio_output_b = None
        self.sink_b = None
        self._secondary_first_frame_seen = False
        try:
            self.sink_a.videoFrameChanged.disconnect(self._on_frame_b_changed)
        except Exception:
            pass
        self.sink_a.videoFrameChanged.connect(self._on_frame_a_changed)
        self.player_a.mediaStatusChanged.connect(self._on_media_status_changed)
        self.player_a.errorOccurred.connect(self._on_error_occurred)
        self.player_a.positionChanged.connect(self._on_position_changed)
        self.player_a.durationChanged.connect(self._on_duration_changed)
        self.player_a.playbackStateChanged.connect(self._on_playback_state_changed)
        # Disconnect the old primary's signals (including its sink's stale
        # frame delivery) before stopping/deleting it -- a late queued
        # signal from a stopped-but-not-yet-deleted player must not run a
        # handler that reads self.player_a (already reassigned above) or
        # overwrite the compositor's freshly-promoted frame with a dying
        # decoder's stale one.
        for signal, handler in (
            (old_sink_a.videoFrameChanged, self._on_frame_a_changed),
            (old_player_a.mediaStatusChanged, self._on_media_status_changed),
            (old_player_a.errorOccurred, self._on_error_occurred),
            (old_player_a.positionChanged, self._on_position_changed),
            (old_player_a.durationChanged, self._on_duration_changed),
            (old_player_a.playbackStateChanged, self._on_playback_state_changed),
        ):
            try:
                signal.disconnect(handler)
            except Exception:
                pass
        try:
            old_player_a.stop()
        except Exception:
            pass
        try:
            old_player_a.deleteLater()
        except Exception:
            pass
        _emit({"event": "dual_transition_complete", "token": self._token})


def _gpu_qml_path() -> str:
    import os
    from .platform_utils import resource_path
    return resource_path(os.path.join("assets", "video_dual_deck.qml"))


class _GpuBridge(QtCore.QObject):
    """Receives QML-forwarded MediaPlayer signals as plain ints/strs.

    QQuickMediaPlayer's enum-typed signals (mediaStatusChanged,
    errorOccurred, playbackStateChanged) do not marshal to Python cleanly
    through PyQt6's QML introspection in this build -- connecting a Python
    slot to them directly, or reading them back via QObject.property(),
    both raise "unable to convert a C++ ... instance to a Python object"
    (confirmed empirically, not assumed). QML JavaScript treats C++ enum
    values as plain numbers, so video_dual_deck.qml's `Connections` blocks
    forward each signal through a call into this object's plain
    int/str-typed pyqtSignals instead, which marshal without issue -- the
    fix lives in QML, this class is just the landing point.
    """

    mediaStatusChanged = QtCore.pyqtSignal(int, int)
    errorOccurred = QtCore.pyqtSignal(int, int, str)
    playbackStateChanged = QtCore.pyqtSignal(int, int)
    doubleClicked = QtCore.pyqtSignal()
    escapePressed = QtCore.pyqtSignal()
    contextMenuRequested = QtCore.pyqtSignal()


def _run_gpu_capability_probe() -> int:
    """`--dual-deck-gpu --probe-only`: loads the real GPU dual-deck QML
    scene (same file the real controller uses -- proves the ShaderEffect
    and its compiled .qsb shader genuinely initialise, not just that some
    generic Qt Quick window can pick a GPU backend), checks the scene
    graph's actual RHI backend, reports one event, and exits. No video is
    ever loaded and no win_id is ever announced -- this never behaves like
    a real playback process."""
    # Test hook, not a feature: forces this probe to report unavailable
    # without touching the real Qt Quick/RHI check at all, so the Phase 1
    # fallback path (checkbox stays disabled, classic video keeps working)
    # can be proven deterministically -- including in a packaged build --
    # without needing to actually break a real GPU driver/install. Inert
    # unless a developer explicitly sets this specific env var; never set
    # by the application itself.
    import os
    if os.environ.get("BILLSMUSIC_FORCE_GPU_UNAVAILABLE"):
        _emit({"event": "gpu_compositor_unavailable", "reason": "forced_unavailable_for_testing"})
        return 0

    from PyQt6 import QtQml
    from PyQt6.QtQuick import QSGRendererInterface

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    engine = QtQml.QQmlApplicationEngine()
    bridge = _GpuBridge()
    engine.rootContext().setContextProperty("bridge", bridge)
    qml_warnings = []
    engine.warnings.connect(lambda ws: qml_warnings.extend(ws))
    engine.load(QtCore.QUrl.fromLocalFile(_gpu_qml_path()))

    def check():
        reason = None
        if not engine.rootObjects() or qml_warnings:
            reason = "qml_load_failed" if not engine.rootObjects() else "qml_warnings"
        else:
            try:
                root = engine.rootObjects()[0]
                api = root.rendererInterface().graphicsApi()
                # Real hardware-backed RHI graphics APIs -- deliberately
                # excludes Software/NullRhi (Qt Quick's own CPU fallback,
                # used when no real GPU/driver is available) and
                # OpenVG/Unknown. Confirmed member names empirically against
                # this PyQt6 build rather than assumed (Direct3D12, not
                # Direct3D12Rhi; OpenGL, not OpenGLRhi).
                gpu_apis = {
                    QSGRendererInterface.GraphicsApi.Direct3D11Rhi,
                    QSGRendererInterface.GraphicsApi.Direct3D12,
                    QSGRendererInterface.GraphicsApi.OpenGL,
                    QSGRendererInterface.GraphicsApi.VulkanRhi,
                    QSGRendererInterface.GraphicsApi.MetalRhi,
                }
                if api not in gpu_apis:
                    reason = f"non_gpu_backend:{api}"
            except Exception as ex:
                reason = f"capability_check_failed:{ex}"
        if reason is None:
            _emit({"event": "gpu_compositor_available"})
        else:
            _emit({"event": "gpu_compositor_unavailable", "reason": reason})
        QtCore.QTimer.singleShot(0, app.quit)

    # Deferred so the scene graph has actually initialised (same "show
    # before winId()"-class timing concern as classic mode's QVideoWidget,
    # and confirmed necessary during development -- graphicsApi() checked
    # immediately after load() is unreliable).
    QtCore.QTimer.singleShot(500, check)
    return app.exec()


class GpuDualDeckVideoSubprocessController(QtCore.QObject):
    """Experimental GPU dual-mode child controller (Phase 2A, GPU variant).

    Structurally a third sibling of VideoSubprocessController and
    DualDeckVideoSubprocessController (the earlier, confirmed-crashing CPU
    compositor -- kept in this file unmodified as the record of that
    investigation, never selected by any code path here) -- selected once
    at process startup (see main()). Deck 0/1 are two QML MediaPlayer/
    AudioOutput/VideoOutput triples in video_dual_deck.qml, composited by a
    ShaderEffect running a compiled GLSL fragment shader
    (assets/video_dual_deck_blend.frag[.qsb]) entirely on the GPU via Qt
    Quick's RHI scene graph -- no QVideoFrame object is ever visible to
    this Python code, unlike the CPU compositor.

    Deck roles are tracked as plain Python indices (self._primary_index /
    self._secondary_index) rather than swapped in QML at promotion time --
    every signal handler below checks which index is currently primary
    rather than being reconnected, so promotion needs no signal
    disconnect/reconnect dance (the CPU controller's harder, riskier
    approach). IPC command/event shapes are byte-identical to the CPU
    controller's, by design -- video_backend.py, video_dual_transition.py
    and window.py do not need to know or care which compositor produced a
    given event.
    """

    def __init__(self):
        super().__init__()
        from PyQt6 import QtQml

        # GPU equivalent of VideoSubprocessController's device-following
        # fix (2026-08-31 Codex audit, follow-up round) -- constructed
        # before the QML scene load below so it exists even if that load
        # fails (see the no-window early-return further down);
        # _reconcile_gpu_audio_output_devices() itself no-ops until
        # self._audios is populated.
        self._media_devices_monitor = QMediaDevices()
        self._media_devices_monitor.audioOutputsChanged.connect(
            lambda: self._reconcile_gpu_audio_output_devices("device_list_changed")
        )
        self._engine = QtQml.QQmlApplicationEngine()
        self._bridge = _GpuBridge()
        self._engine.rootContext().setContextProperty("bridge", self._bridge)
        self._qml_warnings = []
        self._engine.warnings.connect(lambda ws: self._qml_warnings.extend(ws))
        self._engine.load(QtCore.QUrl.fromLocalFile(_gpu_qml_path()))
        if not self._engine.rootObjects():
            # No window, no players -- nothing more this controller can do.
            # main() still calls announce_ready() unconditionally; guard
            # every method below against self._window being None instead
            # of special-casing startup failure everywhere it's used.
            self._window = None
            self._players = []
            self._audios = []
            self._transition_anim = None
            self._blend = None
            self._reader = _StdinReaderThread()
            self._reader.line_received.connect(self._on_line_received)
            self._reader.closed.connect(lambda: QtWidgets.QApplication.instance().quit())
            self._reader.start()
            return

        root = self._engine.rootObjects()[0]
        self._window = root
        self._players = [
            root.findChild(QtCore.QObject, "player0"),
            root.findChild(QtCore.QObject, "player1"),
        ]
        self._audios = [
            root.findChild(QtCore.QObject, "audio0"),
            root.findChild(QtCore.QObject, "audio1"),
        ]
        self._transition_anim = root.findChild(QtCore.QObject, "transitionAnim")
        self._blend = root.findChild(QtCore.QObject, "blend")

        self._primary_index = 0
        self._secondary_index = 1
        self._token = None
        # Immutable-per-load identity for whatever's currently on each
        # physical deck, echoed back on every position_changed/
        # duration_changed event (see _on_position_changed/_on_duration_
        # changed) so the parent can tell a stale report from the outgoing
        # deck apart from a genuine one from the deck it currently
        # considers authoritative -- see video_backend.py's
        # _authoritative_source_hash. Set from the "identity_hash" field
        # the parent already includes on "load"/"preload_secondary"
        # commands (computed there via get_diagnostics().path_details(),
        # never derived here). Real-device bug (2026-08-24): a promoted
        # track's position/duration continued being reported from the
        # still-primary outgoing deck (the swap only happens in
        # _on_transition_finished, once the transition genuinely
        # completes) and got misattributed by the parent to the
        # already-promoted current_path.
        self._deck_identity = [None, None]
        # Set from "commit_dual_transition"'s "transition_id" field (see
        # _dispatch), cleared once _on_transition_finished() genuinely
        # completes the swap. Echoed on the commit milestone events and on
        # every checkpoint/completion event emitted while a transition is
        # in flight, so the parent's bounded watchdog (video_backend.py)
        # can tell a late/stale message apart from the transition it's
        # currently actually waiting on.
        self._active_transition_id = None
        # Real-device correctness hardening (2026-08-24, Codex design
        # review): the immutable identity of whichever preload attempt is
        # currently installed on the secondary deck -- set from
        # "preload_secondary"'s "preload_id" field the parent mints fresh
        # per attempt (including every bounded retry), installed *before*
        # setSource() is ever called (see _start_preload_secondary) so
        # every event this deck can possibly emit afterward, synchronous or
        # not, already has the right identity to echo. Cleared by
        # _cancel_secondary alongside the deck's own source/identity.
        self._active_preload_id = None
        self._secondary_ready = False
        # Smart Video Transition Points' optional intro-skip offset (see
        # video_dual_transition.py's _smart_intro_start_ms) -- 0 means
        # today's exact behaviour (secondary starts at its own position 0,
        # marked ready on its first BUFFERED status). See
        # _start_preload_secondary/_on_media_status_changed/_on_position_changed.
        self._secondary_pending_start_ms = 0
        self._secondary_seek_issued = False
        self._secondary_seek_attempts = 0
        self._secondary_seek_confirmable = False
        # A seek issued right at the deck's first BUFFERED status can be
        # silently dropped by the underlying Qt Multimedia/FFmpeg backend
        # (confirmed empirically this session -- no prior seek call site
        # in this codebase issues one this early in a player's lifecycle,
        # and the first real attempt measurably did not land). This bounded
        # one-shot retry re-issues the same seek once more if position
        # confirmation (_on_position_changed) hasn't landed shortly after
        # the first attempt -- see _on_secondary_seek_retry.
        self._secondary_seek_retry_timer = QtCore.QTimer(self)
        self._secondary_seek_retry_timer.setSingleShot(True)
        self._secondary_seek_retry_timer.timeout.connect(self._on_secondary_seek_retry)
        # Bounded count of secondary_seek_position_report diagnostics emitted
        # for the *current* seek sequence -- reset per _start_preload_secondary
        # (see below). Pure defensive cap: the sequence already self-terminates
        # within ~330ms in practice (80ms confirm delay + one 250ms retry), so
        # this should never be reached, but nothing here may log every frame.
        self._secondary_seek_position_reports = 0
        # Stage A -- True from the moment the secondary is paused at
        # ready-confirmation until it's resumed at commit (see
        # _hold_and_emit_secondary_ready/_commit_dual_transition). Never
        # left True across a cancelled/replaced/failed secondary -- reset
        # in _cancel_secondary alongside the other seek-tracking flags.
        self._secondary_held = False
        # Test-only: artificial delay (ms) inserted between BUFFERED status
        # and the corrective seek/confirm sequence actually starting, so a
        # test can deterministically simulate a slow-to-buffer secondary
        # that has time to drift substantially (real playback continues the
        # whole time, since nothing pauses the deck until it's held) before
        # becoming ready -- see debug_set_seek_delay_ms. Always 0 (no
        # delay, no behaviour change) in production; never set outside a
        # test.
        self._debug_seek_delay_ms = 0
        # Stage C -- authoritative master volume/mute, tracked
        # unconditionally from every set_volume/set_muted dispatch
        # (regardless of whether a crossfade is active) -- see
        # _apply_crossfade_audio_tick's module comment for why this must
        # never be inferred from a live AudioOutput.volume/muted read on
        # either deck. Defaults match AudioOutput's own QML defaults
        # (volume 1.0, unmuted) so a crossfade committed before the first
        # real set_volume/set_muted call (should never happen in practice --
        # window.py always pushes both on every video load -- but nothing
        # here should assume that) still has a sane starting point.
        self._master_volume_fraction = 1.0
        self._master_muted = False
        self._crossfade_active = False
        self._audio_crossfade_curve = "Equal Power"
        # Stage B -- rate-limits the bufferProgress-based half of
        # secondary_preload_progress (see _on_position_changed): only a
        # meaningful increase, at most once per ~750ms, ever emits -- this
        # is the one that matters for a file stuck mid-buffer on a slow
        # network share, which otherwise fires exactly one mediaStatus
        # transition (->BufferingMedia) and then nothing further,
        # indistinguishable from a genuine stall without this.
        self._preload_progress_last_buffer = None
        self._preload_progress_last_emit_monotonic = 0.0

        # Bounded deck-state diagnostics (added to investigate a real-device
        # black-interval report where every existing diagnostic showed the
        # smart-seek/deadline logic firing correctly, yet the recording still
        # showed an extended black interval -- see CODEX_HANDOFF.md). These
        # arrays are the *only* place this process knows a deck's current
        # mediaStatus/playbackState: the QML MediaPlayer's own mediaStatus/
        # playbackState *properties* cannot be read directly from Python
        # (same "unable to convert a C++ ... instance" issue video_dual_deck.qml's
        # module comment documents for the enum-typed *signals* -- it applies
        # to reading the properties directly too), so these are populated
        # exclusively from the bridge's already-marshalled plain-int signals,
        # for both decks, not just the primary.
        self._deck_media_status = [0, 0]
        self._deck_playback_state = [_PLAYBACK_STATE_STOPPED, _PLAYBACK_STATE_STOPPED]
        # True only from secondary_ready until dual_transition_complete (see
        # _on_transition_finished) -- deck-state changes outside a committed
        # transition are not what's being investigated and would not be
        # bounded (playback runs indefinitely), so this window is the only
        # place _on_media_status_changed/_on_playback_state_changed emit their
        # extra "deck_state_changed" diagnostic.
        self._deck_state_monitoring_active = False
        # Bounded 5-point GPU transition progress-checkpoint sampling (see
        # _commit_dual_transition/_on_checkpoint_timer_tick/_capture_transition_checkpoint).
        # Polls blend.progress via a QTimer rather than connecting to the
        # QML ShaderEffect's auto-generated progressChanged signal directly --
        # progress is a plain, already-proven-safe-to-read float property
        # (like position/duration below), so this reuses that exact pattern
        # instead of a new, unverified dynamic QML signal connection.
        self._progress_checkpoints_pending: list = []
        self._current_transition_type_label = "Cross Dissolve"
        self._checkpoint_timer = QtCore.QTimer(self)
        self._checkpoint_timer.setInterval(20)
        self._checkpoint_timer.timeout.connect(self._on_checkpoint_timer_tick)

        for index, player in enumerate(self._players):
            player.positionChanged.connect(
                lambda position, i=index: self._on_position_changed(i, position)
            )
            player.durationChanged.connect(
                lambda duration, i=index: self._on_duration_changed(i, duration)
            )
        self._bridge.mediaStatusChanged.connect(self._on_media_status_changed)
        self._bridge.errorOccurred.connect(self._on_error_occurred)
        self._bridge.playbackStateChanged.connect(self._on_playback_state_changed)
        self._bridge.doubleClicked.connect(lambda: _emit({"event": "double_clicked"}))
        self._bridge.escapePressed.connect(lambda: _emit({"event": "escape_pressed"}))
        self._bridge.contextMenuRequested.connect(
            lambda: _emit({"event": "context_menu_requested"})
        )
        self._transition_anim.finished.connect(self._on_transition_finished)

        self._reader = _StdinReaderThread()
        self._reader.line_received.connect(self._on_line_received)
        self._reader.closed.connect(lambda: QtWidgets.QApplication.instance().quit())
        self._reader.start()

    def _reconcile_gpu_audio_output_devices(self, reason: str) -> None:
        """GPU equivalent of VideoSubprocessController._reconcile_audio_
        output_device -- each QML AudioOutput (audio0/audio1) binds to
        whatever the Qt default output device was at scene-load time and
        never follows a later default change on its own, exactly like the
        classic child's QAudioOutput did (same real-device evidence, same
        root cause, same fix). Both decks are reconciled independently,
        never only whichever is currently primary -- either can be
        genuinely playing or about to: the secondary during a preload,
        the outgoing deck during a still-in-flight crossfade.

        Confirmed empirically against this exact QML AudioOutput exposure
        (findChild returns a real QAudioOutput-typed object here, not a
        generic QObject): reading/writing its "device" property via
        property()/setProperty() round-trips a genuine QAudioDevice with
        working id()/description()/isNull(), the same as the classic
        controller's direct .device()/.setDevice() calls. Per-deck
        volume/mute is only *preserved* here (read before, reapplied
        after) -- _master_volume_fraction/_master_muted stay the sole
        authoritative source for what those should actually be, applied
        via the existing set_volume/set_muted/_apply_crossfade_audio_tick
        call sites, never duplicated in this method.
        """
        if not self._audios:
            return
        try:
            current_default = QMediaDevices.defaultAudioOutput()
            if current_default.isNull():
                return
            for index, audio in enumerate(self._audios):
                existing = audio.property("device")
                if not existing.isNull() and bytes(existing.id()) == bytes(current_default.id()):
                    continue  # already correct -- the common case, every call
                volume = audio.property("volume")
                muted = audio.property("muted")
                old_hash = _hash_label(existing.id()) or _hash_label(existing.description())
                audio.setProperty("device", current_default)
                audio.setProperty("volume", volume)
                audio.setProperty("muted", muted)
                new_hash = _hash_label(current_default.id()) or _hash_label(current_default.description())
                _emit({
                    "event": "audio_device_rebound",
                    "token": self._token,
                    "deck": index,
                    "reason": reason,
                    "old_device_hash": old_hash,
                    "new_device_hash": new_hash,
                    "preserved_volume": round(float(volume), 4),
                    "preserved_muted": bool(muted),
                })
        except Exception:
            pass

    def announce_ready(self):
        if self._window is None:
            _emit({
                "event": "error", "category": "video_subprocess_error",
                "message": "GPU compositor scene failed to load",
            })
            return
        win_id = int(self._window.winId())
        _emit({"event": "ready", "win_id": win_id})

    # -- command handling ---------------------------------------------------
    def _on_line_received(self, line: str):
        try:
            command = json.loads(line)
        except Exception:
            return
        self._dispatch(command)

    def _dispatch(self, command: dict):
        import os
        if self._window is None:
            if command.get("cmd") == "shutdown":
                QtWidgets.QApplication.instance().quit()
            return
        name = command.get("cmd")
        token = command.get("token")
        primary = self._players[self._primary_index]
        primary_audio = self._audios[self._primary_index]
        if name == "load":
            # Correctness hardening (2026-08-24): a new primary load fully
            # supersedes any dual-transition/preload state this process was
            # tracking -- a still-armed GPU animation/checkpoint timer or a
            # secondary held from before this load could otherwise later
            # swap physical decks or emit an event that outlives what this
            # load() call is about. Torn down *before* installing the new
            # source/identity, mirroring the identity-before-setSource
            # ordering used below.
            self._reset_dual_transition_state(reason="new_primary_load")
            self._cancel_secondary(reason="new_primary_load")
            self._token = token
            path = command.get("path", "")
            if not path or not os.path.isfile(path):
                _emit({"event": "error", "token": token, "category": "video_file_missing", "message": f"Video file not found: {path}"})
                return
            self._reconcile_gpu_audio_output_devices("pre_load")
            # Identity installed before setSource() (see _start_preload_
            # secondary's matching ordering for the secondary deck): if
            # setSource() synchronously produces a status/error signal, the
            # handler already sees the correct new identity, never the
            # previous track's.
            self._deck_identity[self._primary_index] = command.get("identity_hash")
            try:
                primary.setProperty("source", QtCore.QUrl.fromLocalFile(path))
            except Exception as ex:
                self._deck_identity[self._primary_index] = None
                _emit({"event": "error", "token": token, "category": "video_unknown_error", "message": str(ex)})
                return
            primary.play()
        elif name == "play" or name == "resume":
            primary.play()
        elif name == "pause":
            primary.pause()
        elif name == "stop":
            self._reset_dual_transition_state(reason="stop")
            self._cancel_secondary(reason="stop")
            self._token = None
            self._deck_identity[self._primary_index] = None
            primary.stop()
        elif name == "seek":
            primary.setProperty("position", int(command.get("position_ms", 0)))
        elif name == "resize":
            self._handle_resize(command)
        elif name == "set_volume":
            volume = max(0.0, min(100.0, float(command.get("volume", 100))))
            # Stage C -- self._master_volume_fraction is tracked
            # unconditionally (not only during a crossfade) so it's always
            # accurate the instant a crossfade-enabled transition commits;
            # see _apply_crossfade_audio_tick for why this must be the
            # single authoritative source, never a live AudioOutput.volume
            # read on either deck.
            self._master_volume_fraction = volume / 100.0
            if self._crossfade_active:
                self._apply_crossfade_audio_tick()
            else:
                primary_audio.setProperty("volume", self._master_volume_fraction)
        elif name == "set_muted":
            self._master_muted = bool(command.get("muted", False))
            if self._crossfade_active:
                self._apply_crossfade_audio_tick()
            else:
                primary_audio.setProperty("muted", self._master_muted)
        elif name == "preload_secondary":
            self._start_preload_secondary(
                command.get("path", ""),
                int(command.get("start_position_ms", 0) or 0),
                command.get("identity_hash"),
                command.get("preload_id"),
            )
        elif name == "cancel_secondary":
            self._cancel_secondary(reason="cancelled")
        elif name == "commit_dual_transition":
            # Two distinct milestones, emitted before _commit_dual_transition
            # is even entered, so a real-device stall can be placed
            # precisely: "received" proves this process's event loop is
            # still alive and read the command at all; "execution_started"
            # proves _dispatch made it all the way to calling the method
            # (nothing between the two blocks below can silently swallow a
            # command). The existing synchronous checkpoint(0.0) inside
            # _commit_dual_transition remains the next milestone after
            # that -- see video_backend.py's bounded ack/completion
            # watchdog for how the parent uses these.
            transition_id = command.get("transition_id")
            self._active_transition_id = transition_id
            _emit({
                "event": "commit_command_received",
                "transition_id": transition_id,
                "primary_index": self._primary_index,
                "secondary_index": self._secondary_index,
                "primary_source_hash": self._deck_identity[self._primary_index],
                "secondary_source_hash": self._deck_identity[self._secondary_index],
            })
            _emit({"event": "commit_execution_started", "transition_id": transition_id})
            self._commit_dual_transition(
                int(command.get("duration_ms", 1000)),
                str(command.get("transition_type", "Cross Dissolve")),
                float(command.get("seed", 0.0)),
                audio_crossfade_enabled=bool(command.get("audio_crossfade_enabled", False)),
                audio_crossfade_curve=str(command.get("audio_crossfade_curve", "Equal Power")),
            )
        elif name == "pause_dual_transition":
            self._transition_anim.pause()
            primary.pause()
            self._players[self._secondary_index].pause()
        elif name == "resume_dual_transition":
            self._transition_anim.resume()
            primary.play()
            self._players[self._secondary_index].play()
        elif name == "shutdown":
            self.shutdown()
        elif name == "debug_set_seek_delay_ms":
            self._debug_seek_delay_ms = max(0, int(command.get("delay_ms", 0)))
        elif name == "query_audio_state":
            # v1.0.71 correction: diagnostics-only echo of what's actually
            # on the primary deck right now, queried only at specific
            # window.py checkpoints (never polled/continuous) -- see
            # video_backend.py's query_audio_state()/audio_state_reported.
            # checkpoint/transition_id are opaque here, echoed back verbatim
            # so the parent can label this without a correlation scheme.
            # playback_state comes from self._deck_playback_state, not a
            # direct primary.property("playbackState") read -- the QML
            # MediaPlayer's playbackState/mediaStatus properties cannot be
            # read directly from Python in this Qt binding (see the
            # _deck_playback_state field's own module comment above);
            # volume/muted are plain float/bool QML properties, which
            # *can* be read directly, unlike the enum-typed ones.
            _emit({
                "event": "audio_state_report",
                "checkpoint": command.get("checkpoint"),
                "transition_id": command.get("transition_id"),
                "primary_index": self._primary_index,
                "secondary_index": self._secondary_index,
                "deck_source_hash": self._deck_identity[self._primary_index],
                "volume": primary_audio.property("volume"),
                "muted": primary_audio.property("muted"),
                "playback_state": self._deck_playback_state[self._primary_index],
                "crossfade_active": self._crossfade_active,
            })

    def _handle_resize(self, command: dict):
        width = max(1, int(command.get("width", self._window.width())))
        height = max(1, int(command.get("height", self._window.height())))
        try:
            child_dpr = max(0.1, float(self._window.devicePixelRatio()))
        except Exception:
            child_dpr = 1.0
        try:
            parent_dpr = max(0.1, float(command.get("device_pixel_ratio", child_dpr)))
        except (TypeError, ValueError):
            parent_dpr = child_dpr
        width = max(1, round(width * parent_dpr / child_dpr))
        height = max(1, round(height * parent_dpr / child_dpr))
        self._window.resize(width, height)

    def shutdown(self):
        self._reset_dual_transition_state(reason="shutdown")
        self._cancel_secondary(reason="shutdown")
        self._players[self._primary_index].stop()
        QtWidgets.QApplication.instance().quit()

    # -- secondary deck (preload) --------------------------------------------
    def _secondary_event_envelope(self) -> dict:
        """One consistent identity subset for every event this deck can
        emit about the *currently installed* preload attempt -- built fresh
        at emission time (never cached), so it always reflects whatever
        _start_preload_secondary/_cancel_secondary last installed. Used via
        ``{**self._secondary_event_envelope(), "event": ..., ...}`` at every
        secondary-related _emit() call site rather than each one assembling
        its own subset of these same five fields by hand."""
        return {
            "preload_id": self._active_preload_id,
            "source_hash": self._deck_identity[self._secondary_index],
            "deck_index": self._secondary_index,
            "primary_index": self._primary_index,
            "secondary_index": self._secondary_index,
        }

    def _start_preload_secondary(
        self, path: str, start_position_ms: int = 0, identity_hash=None,
        preload_id=None,
    ):
        import os
        self._cancel_secondary(reason="new_preload")  # step 1: invalidate previous
        # Steps 2-3: install the new preload/deck identity *before* ever
        # calling setSource() below, so any signal setSource() triggers
        # synchronously (a status change, or an error) is already
        # attributable to this new preload, never the one just cancelled.
        self._active_preload_id = preload_id
        self._deck_identity[self._secondary_index] = identity_hash
        if not path or not os.path.isfile(path):
            envelope = self._secondary_event_envelope()
            self._active_preload_id = None
            self._deck_identity[self._secondary_index] = None
            _emit({**envelope, "event": "secondary_failed", "reason": "video_file_missing"})
            return
        self._reconcile_gpu_audio_output_devices("pre_load_secondary")
        _emit({**self._secondary_event_envelope(), "event": "secondary_loading_started"})
        secondary = self._players[self._secondary_index]
        secondary_audio = self._audios[self._secondary_index]
        secondary_audio.setProperty("muted", True)
        self._secondary_pending_start_ms = max(0, int(start_position_ms))
        self._secondary_seek_issued = False
        self._secondary_seek_attempts = 0
        self._secondary_seek_confirmable = False
        self._secondary_seek_position_reports = 0
        try:
            secondary.setProperty("source", QtCore.QUrl.fromLocalFile(path))  # step 4
        except Exception as ex:
            # Step 6: setSource() itself raised -- capture the envelope
            # (identity already installed above), clear that preload/deck
            # identity, stop/clear the deck safely, emit the failure
            # carrying the captured identity, and never fall through to
            # play() below.
            envelope = self._secondary_event_envelope()
            self._active_preload_id = None
            self._deck_identity[self._secondary_index] = None
            try:
                secondary.stop()
                secondary.setProperty("source", QtCore.QUrl())
            except Exception:
                pass
            _emit({**envelope, "event": "secondary_failed", "reason": str(ex)})
            return
        # Step 5: verify the same preload is still active -- setSource()
        # can synchronously deliver a mediaStatusChanged/errorOccurred
        # signal before returning (e.g. _on_error_occurred already calling
        # _cancel_secondary() inline), which would have cleared
        # self._active_preload_id above. Only proceed to play() if nothing
        # in that synchronous window already invalidated this exact
        # preload attempt.
        if self._active_preload_id != preload_id:
            return
        secondary.play()

    def _cancel_secondary(self, *, reason: str):
        secondary = self._players[self._secondary_index]
        secondary.stop()
        secondary.setProperty("source", QtCore.QUrl())
        self._deck_identity[self._secondary_index] = None
        self._active_preload_id = None
        self._secondary_ready = False
        self._secondary_pending_start_ms = 0
        self._secondary_seek_issued = False
        self._secondary_seek_attempts = 0
        self._secondary_seek_confirmable = False
        self._secondary_seek_position_reports = 0
        self._secondary_seek_retry_timer.stop()
        self._secondary_held = False
        self._preload_progress_last_buffer = None
        self._preload_progress_last_emit_monotonic = 0.0

    def _reset_dual_transition_state(self, *, reason: str) -> None:
        """Correctness hardening (2026-08-24): a new primary load()/stop()
        must not leave a stale committed-transition animation/checkpoint
        timer armed, or a stale transition_id that a late, already-in-
        flight event could still reference. Parent-side identity
        invalidation (video_backend.py's load()/stop()) alone is not
        sufficient -- if this process's own GPU animation were left
        running, _on_transition_finished() could still swap physical decks
        later, entirely independent of whatever the parent now believes is
        current. Idempotent and cheap to call unconditionally; does not
        touch the secondary deck itself (see _cancel_secondary, called
        alongside this at both call sites)."""
        if self._transition_anim is not None:
            try:
                self._transition_anim.stop()
            except Exception:
                pass
        self._checkpoint_timer.stop()
        self._progress_checkpoints_pending = []
        self._active_transition_id = None
        self._crossfade_active = False
        self._deck_state_monitoring_active = False

    # -- committed transition (Phase 2B/2C GPU effects) ----------------------
    def _commit_dual_transition(
        self, duration_ms: int, transition_type: str = "Cross Dissolve",
        seed: float = 0.0, *,
        audio_crossfade_enabled: bool = False,
        audio_crossfade_curve: str = "Equal Power",
    ):
        if not self._secondary_ready:
            _emit({
                **self._secondary_event_envelope(),
                "event": "secondary_failed", "reason": "not_ready_at_commit",
                "transition_id": self._active_transition_id,
            })
            self._active_transition_id = None
            return
        _emit({"event": "dual_transition_committed"})
        # Stage A -- resume the held secondary as the very first action,
        # before any blend/audio property writes: never a re-seek here
        # (setProperty("position", ...)) -- resuming a genuinely paused
        # player is cheaper than re-seeking, since the decoder's own
        # buffers are retained across a pause but a seek must decode
        # forward from the nearest keyframe. See
        # _hold_and_emit_secondary_ready for where the pause was issued.
        if self._secondary_held:
            self._players[self._secondary_index].play()
            self._secondary_held = False
        if audio_crossfade_enabled:
            # Stage C -- silencing is expressed purely through volume for
            # the duration of the transition, never through muted (both
            # forced False here) -- this is what lets a live mid-transition
            # set_volume/set_muted apply symmetrically to both decks
            # (_apply_crossfade_audio_tick), instead of only ever reaching
            # whichever deck happens to be "primary_audio" at dispatch time.
            self._crossfade_active = True
            self._audio_crossfade_curve = audio_crossfade_curve
            self._audios[self._primary_index].setProperty("muted", False)
            self._audios[self._secondary_index].setProperty("muted", False)
            self._apply_crossfade_audio_tick()
        else:
            # Simplest safe audio handover (deliberately not a crossfade --
            # see video_dual_transition.py's module docstring): audio
            # ownership switches the instant the visual transition begins,
            # never overlapping at full volume. Identical policy to the CPU
            # controller, and unaffected by which visual effect is chosen.
            #
            # Real-device bug (2026-08-31 Codex audit): this used to just
            # flip "muted" (True on the outgoing deck, unconditionally
            # False on the incoming one) and stop there -- unlike the
            # crossfade branch above, it never touched "volume" at all, and
            # never consulted _master_muted before deciding to unmute. The
            # incoming deck's own AudioOutput.volume property keeps
            # whatever it last held (QML's own default of 1.0 if this deck
            # has never been primary before, or a stale fractional value
            # left over from an *earlier* crossfade-enabled transition this
            # same physical deck object took part in) -- so a promoted
            # video could play at the wrong volume, or audibly despite the
            # user having the app globally muted. _on_transition_finished()
            # only re-applies the authoritative master volume/mute for the
            # crossfade_active case (its own "belt and braces" comment) --
            # nothing did it for this branch. Fixed the same way the
            # crossfade branch already keeps _master_volume_fraction/
            # _master_muted authoritative: apply both to the incoming deck
            # right here, before the visual handover it's about to become
            # visible.
            self._audios[self._primary_index].setProperty("muted", True)
            self._audios[self._secondary_index].setProperty(
                "volume", self._master_volume_fraction,
            )
            self._audios[self._secondary_index].setProperty(
                "muted", self._master_muted,
            )
        # blend.progress always animates a fixed 0.0->1.0 (see
        # video_dual_deck.qml's transitionAnim) regardless of which deck
        # is primary -- primaryIsFirst is what tells the shader which
        # physical texture is the outgoing one this time, so a directional
        # effect (push/wipe/zoom) has an unambiguous "A"/"B" without
        # needing the animation itself to run backwards, unlike Phase 2A's
        # original cross-dissolve-only from/to flip.
        self._blend.setProperty(
            "transitionType",
            _GPU_EFFECT_TRANSITION_TYPE.get(transition_type, 0.0),
        )
        self._blend.setProperty(
            "direction", _GPU_EFFECT_DIRECTION.get(transition_type, 1.0),
        )
        self._blend.setProperty(
            "primaryIsFirst", 1.0 if self._primary_index == 0 else 0.0,
        )
        # Phase 2C: fixed for the whole transition (see this method's
        # caller, DualDeckController.generate_transition_seed in
        # video_dual_transition.py, for why it's generated once per
        # commit rather than per frame). Harmless no-op for effects that
        # don't use it.
        self._blend.setProperty("seed", float(seed))
        self._transition_anim.setProperty("duration", max(1, int(duration_ms)))
        self._current_transition_type_label = transition_type
        # Bounded 5-point checkpoint sampling: progress is exactly 0.0 right
        # here (NumberAnimation's explicit from: 0.0 forces this the instant
        # running becomes true), so this one is captured synchronously rather
        # than waiting for the first timer tick -- the other four are polled
        # by _on_checkpoint_timer_tick, with _on_transition_finished as a
        # fallback guaranteeing exactly 5 emissions even if the timer's ~20ms
        # granularity never samples a value >= 1.0 before the animation's own
        # finished signal fires.
        self._progress_checkpoints_pending = [0.25, 0.5, 0.75, 1.0]
        self._transition_anim.setProperty("running", True)
        self._capture_transition_checkpoint(0.0)
        self._checkpoint_timer.start()

    # -- bounded deck-state diagnostics helpers -------------------------------
    @staticmethod
    def _safe_property(obj, name: str):
        """Read a plain (non-enum) QML property defensively. Only int/float/
        bool results are trusted -- anything else (including a failed read,
        or a genuinely enum-typed property someone adds here by mistake in
        future) comes back as None rather than risking the same "unable to
        convert a C++ ... instance to a Python object" failure this file's
        module docstring on _GpuBridge already documents for enum-typed
        values. position/duration/hasVideo/bufferProgress are all plain
        types and already proven readable this way elsewhere in this class
        (see _on_position_changed's existing "duration" read)."""
        try:
            value = obj.property(name)
        except Exception:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value
        return None

    @staticmethod
    def _source_is_empty(player) -> Optional[bool]:
        try:
            url = player.property("source")
            return bool(url.isEmpty())
        except Exception:
            return None

    @staticmethod
    def _crossfade_gains(progress: float, curve: str) -> tuple:
        """A_gain/B_gain for the given curve, where A is always the
        *outgoing* deck (self._primary_index) and B the *incoming* one
        (self._secondary_index) -- never the reverse, regardless of which
        physical deck index that currently is. Equal power
        (cos/sin quarter-wave) keeps A_gain^2 + B_gain^2 == 1 throughout,
        avoiding the perceived volume dip a straight linear crossfade
        produces through the middle of the transition."""
        progress = max(0.0, min(1.0, progress))
        if curve == "Linear":
            return 1.0 - progress, progress
        return math.cos(progress * math.pi / 2.0), math.sin(progress * math.pi / 2.0)

    def _apply_crossfade_audio_tick(self) -> None:
        """Stage C -- the *only* place either deck's AudioOutput.volume is
        written while a crossfade-enabled transition is active. Silencing
        (global mute) is expressed purely as effective volume 0.0, never
        by touching the muted property (which _commit_dual_transition
        already forced False on both decks for the crossfade's duration) --
        this is what makes a live set_volume/set_muted call during the
        transition (routed here from _dispatch, see __init__'s comment on
        self._master_volume_fraction/self._master_muted) apply symmetrically
        and immediately to both decks, not just whichever one the old
        primary-only dispatch would have reached."""
        if not self._crossfade_active or self._blend is None:
            return
        progress = self._safe_property(self._blend, "progress")
        if progress is None:
            return
        a_gain, b_gain = self._crossfade_gains(progress, self._audio_crossfade_curve)
        if self._master_muted:
            a_effective = b_effective = 0.0
        else:
            a_effective = self._master_volume_fraction * a_gain
            b_effective = self._master_volume_fraction * b_gain
        self._audios[self._primary_index].setProperty("volume", a_effective)
        self._audios[self._secondary_index].setProperty("volume", b_effective)

    def _capture_transition_checkpoint(self, progress_target: float) -> None:
        a = self._primary_index
        b = self._secondary_index
        player_a = self._players[a]
        player_b = self._players[b]
        progress_actual = self._safe_property(self._blend, "progress")
        checkpoint = {
            "event": "gpu_transition_checkpoint",
            "transition_id": self._active_transition_id,
            "progress_target": progress_target,
            "progress_actual": progress_actual,
            "primary_index": a,
            "secondary_index": b,
            "a_position_ms": self._safe_property(player_a, "position"),
            "b_position_ms": self._safe_property(player_b, "position"),
            "a_media_status": self._deck_media_status[a],
            "a_media_status_name": _MEDIA_STATUS_NAMES.get(self._deck_media_status[a], "?"),
            "b_media_status": self._deck_media_status[b],
            "b_media_status_name": _MEDIA_STATUS_NAMES.get(self._deck_media_status[b], "?"),
            "a_playback_state": self._deck_playback_state[a],
            "a_playback_state_name": _PLAYBACK_STATE_NAMES.get(self._deck_playback_state[a], "?"),
            "b_playback_state": self._deck_playback_state[b],
            "b_playback_state_name": _PLAYBACK_STATE_NAMES.get(self._deck_playback_state[b], "?"),
            "a_has_video": self._safe_property(player_a, "hasVideo"),
            "b_has_video": self._safe_property(player_b, "hasVideo"),
            "a_source_empty": self._source_is_empty(player_a),
            "b_source_empty": self._source_is_empty(player_b),
            "transition_type": self._current_transition_type_label,
        }
        if self._crossfade_active and progress_actual is not None:
            # Stage C -- extends the existing 5-point checkpoint payload
            # rather than adding a new per-tick event: still exactly 5
            # emissions per transition regardless of whether crossfade is
            # enabled, no new logging volume.
            a_gain, b_gain = self._crossfade_gains(progress_actual, self._audio_crossfade_curve)
            checkpoint.update({
                "audio_crossfade_curve": self._audio_crossfade_curve,
                "global_muted": self._master_muted,
                "a_base_gain": self._master_volume_fraction,
                "b_base_gain": self._master_volume_fraction,
                "a_transition_gain": a_gain,
                "b_transition_gain": b_gain,
                "a_effective_volume": 0.0 if self._master_muted else self._master_volume_fraction * a_gain,
                "b_effective_volume": 0.0 if self._master_muted else self._master_volume_fraction * b_gain,
            })
        _emit(checkpoint)

    def _on_checkpoint_timer_tick(self) -> None:
        if self._crossfade_active:
            self._apply_crossfade_audio_tick()
        if not self._progress_checkpoints_pending:
            self._checkpoint_timer.stop()
            return
        progress = self._safe_property(self._blend, "progress")
        if progress is None:
            return
        while self._progress_checkpoints_pending and progress >= self._progress_checkpoints_pending[0] - 1e-6:
            target = self._progress_checkpoints_pending.pop(0)
            self._capture_transition_checkpoint(target)
        if not self._progress_checkpoints_pending:
            self._checkpoint_timer.stop()

    def _on_transition_finished(self):
        self._checkpoint_timer.stop()
        if self._progress_checkpoints_pending:
            # Guarantee exactly 5 checkpoints total even if the ~20ms poll
            # granularity never sampled a live value >= 1.0 before the
            # animation's own finished signal fired.
            for target in list(self._progress_checkpoints_pending):
                self._capture_transition_checkpoint(target)
            self._progress_checkpoints_pending = []
        # Promote: the secondary deck becomes the new primary. No QML-side
        # reconnection needed -- every handler below checks
        # self._primary_index/self._secondary_index dynamically, unlike the
        # CPU controller's C++ signal reconnect dance (and the stale-frame
        # bug that came with it).
        self._primary_index, self._secondary_index = self._secondary_index, self._primary_index
        if self._crossfade_active:
            # Stage C -- gain has already reached exactly 1.0/0.0 at
            # progress 1.0 (cos/sin at pi/2, or 1.0/0.0 for linear), but
            # explicitly re-applying the clean master volume/mute state to
            # the now-promoted primary here is belt-and-braces against
            # float rounding, and restores it to being governed by
            # ordinary future set_volume/set_muted calls exactly as before
            # a crossfade ever started (the demoted old primary is torn
            # down two lines below regardless, so its audio state doesn't
            # matter after this point).
            self._crossfade_active = False
            self._audios[self._primary_index].setProperty("volume", self._master_volume_fraction)
            self._audios[self._primary_index].setProperty("muted", self._master_muted)
        old_primary = self._players[self._secondary_index]
        old_primary.stop()
        old_primary.setProperty("source", QtCore.QUrl())
        self._deck_identity[self._secondary_index] = None
        self._secondary_ready = False
        _emit({
            "event": "dual_transition_complete", "token": self._token,
            "transition_id": self._active_transition_id,
        })
        self._active_transition_id = None
        # From here on, deck-state changes belong to whatever comes next
        # (new preload, idle primary playback) rather than this just-finished
        # transition -- see __init__'s comment on _deck_state_monitoring_active.
        self._deck_state_monitoring_active = False

    # -- QML bridge signal handlers -> stdout events -------------------------
    def _on_media_status_changed(self, deck: int, status: int):
        previous_status = self._deck_media_status[deck]
        self._deck_media_status[deck] = status
        if self._deck_state_monitoring_active and status != previous_status:
            _emit({
                "event": "deck_state_changed",
                "deck": deck,
                "role": "primary" if deck == self._primary_index else "secondary",
                "kind": "media_status",
                "previous": previous_status,
                "previous_name": _MEDIA_STATUS_NAMES.get(previous_status, "?"),
                "current": status,
                "current_name": _MEDIA_STATUS_NAMES.get(status, "?"),
            })
        # Stage B -- bounded preload-progress signal: mediaStatus has ~8
        # values total, so this is a handful of emissions per preload, never
        # per-frame. Covers the "made it through the normal Loading->
        # Loaded->Buffering->Buffered sequence" half of adaptive-timeout
        # detection; _on_position_changed's bufferProgress-based emission
        # below covers the other half (a file stuck mid-buffer on a slow
        # network share fires exactly one status transition and then
        # nothing further, which status changes alone can't distinguish
        # from a genuine stall).
        if deck == self._secondary_index and not self._secondary_ready and status != previous_status:
            secondary_for_progress = self._players[deck]
            _emit({
                **self._secondary_event_envelope(),
                "event": "secondary_preload_progress",
                "reason": "media_status_changed",
                "media_status_name": _MEDIA_STATUS_NAMES.get(status, "?"),
                "position_ms": self._safe_property(secondary_for_progress, "position"),
                "buffer_progress": self._safe_property(secondary_for_progress, "bufferProgress"),
                "has_video": self._safe_property(secondary_for_progress, "hasVideo"),
            })
        if deck == self._secondary_index and status == _MEDIA_STATUS_BUFFERED and not self._secondary_ready:
            # Seek/confirm is issued unconditionally, even when the target
            # is the natural start (0) -- no more "0 means skip the confirm
            # dance" special case. Only once the deck has actually buffered
            # (no precedent anywhere in this codebase for seeking before
            # that point, and Qt Multimedia backends are known to sometimes
            # drop a too-early seek silently) -- ready/first_frame are held
            # back until _on_position_changed below confirms the seek
            # actually landed, not just that *a* frame arrived.
            #
            # A real-device regression proved the old "0 = skip confirm,
            # just hold wherever it happens to be" behaviour was wrong: a
            # plain preload (start_position_ms=0) is started with .play()
            # and left running while it buffers (see
            # _start_preload_secondary) -- for a slow load, real sessions
            # showed requested 0ms held at 1241ms and 3320ms, silently
            # skipping that much of the incoming video. Treating 0 as just
            # another seek target through the exact same confirm pipeline
            # Smart Video Transition Points' intro-skip already uses closes
            # that gap uniformly, with no special-casing left to get wrong.
            if not self._secondary_seek_issued:
                self._secondary_seek_issued = True
                if self._debug_seek_delay_ms > 0:
                    # Test-only: see debug_set_seek_delay_ms.
                    QtCore.QTimer.singleShot(self._debug_seek_delay_ms, self._issue_secondary_seek)
                else:
                    self._issue_secondary_seek()
            return
        if deck == self._primary_index and status == _MEDIA_STATUS_END_OF_MEDIA:
            _emit({"event": "end_of_media", "token": self._token})

    def _hold_and_emit_secondary_ready(self, deck: int, *, via: str) -> None:
        """Stage A -- "ready" must mean "ready and held," never just "a
        frame arrived": pauses the secondary deck *before* announcing
        readiness, in the same synchronous call, so there is no window
        where DualVideoTransitionEngine could observe secondary_ready and
        commit before the pause has actually been dispatched to the QML
        MediaPlayer. Without this, Smart Video Transition Points' intro
        seek would land the secondary at the right position only for it to
        keep silently playing (muted) for however long the deadline timer
        takes to fire -- exactly the real-device bug v1.0.57's diagnostics
        proved (a 280ms seek target measured at 3200-4200ms by the time the
        GPU transition actually started). Resume happens once, later, in
        _commit_dual_transition -- never a re-seek, see that method."""
        secondary = self._players[deck]
        secondary.pause()
        self._secondary_held = True
        _emit({
            **self._secondary_event_envelope(),
            "event": "secondary_held",
            "position_ms": self._safe_property(secondary, "position"),
            "media_status_name": _MEDIA_STATUS_NAMES.get(self._deck_media_status[deck], "?"),
            "playback_state_name": _PLAYBACK_STATE_NAMES.get(self._deck_playback_state[deck], "?"),
            "has_video": self._safe_property(secondary, "hasVideo"),
            "source_empty": self._source_is_empty(secondary),
        })
        self._secondary_ready = True
        self._deck_state_monitoring_active = True
        _emit({**self._secondary_event_envelope(), "event": "secondary_first_frame"})
        _emit({
            **self._secondary_event_envelope(),
            "event": "secondary_ready",
            "via": via,
            "confirmed_position_ms": self._safe_property(secondary, "position"),
            "media_status_name": _MEDIA_STATUS_NAMES.get(self._deck_media_status[deck], "?"),
            "playback_state_name": _PLAYBACK_STATE_NAMES.get(self._deck_playback_state[deck], "?"),
            "buffer_progress": self._safe_property(secondary, "bufferProgress"),
            "has_video": self._safe_property(secondary, "hasVideo"),
            "source_empty": self._source_is_empty(secondary),
        })

    def _issue_secondary_seek(self) -> None:
        self._secondary_seek_attempts += 1
        self._secondary_seek_confirmable = False
        secondary = self._players[self._secondary_index]
        _emit({
            **self._secondary_event_envelope(),
            "event": "secondary_seek_issued",
            "attempt": self._secondary_seek_attempts,
            "requested_start_position_ms": self._secondary_pending_start_ms,
            "position_before_seek_ms": self._safe_property(secondary, "position"),
            "media_status_name": _MEDIA_STATUS_NAMES.get(self._deck_media_status[self._secondary_index], "?"),
            "playback_state_name": _PLAYBACK_STATE_NAMES.get(self._deck_playback_state[self._secondary_index], "?"),
            "buffer_progress": self._safe_property(secondary, "bufferProgress"),
            "has_video": self._safe_property(secondary, "hasVideo"),
            "source_empty": self._source_is_empty(secondary),
        })
        secondary.setProperty(
            "position", self._secondary_pending_start_ms,
        )
        # Confirmed empirically this session: setting the "position"
        # property synchronously echoes a positionChanged signal carrying
        # the *requested* value immediately, well before the underlying
        # decoder has actually completed the real seek -- trusting that
        # first echo as proof a real frame exists there is wrong (it fired
        # before any real seek could possibly have landed). This short
        # delay is not a fixed wait for the seek itself; it just excludes
        # that one synchronous echo so _on_position_changed only starts
        # trusting reports that came from a later, genuine event-loop
        # iteration.
        QtCore.QTimer.singleShot(80, self._on_secondary_seek_confirmable)
        # Bounded single retry, not a poll loop: a seek issued right at
        # first-BUFFERED can be silently dropped by the underlying decoder
        # (see __init__'s comment on _secondary_seek_retry_timer) -- if
        # position confirmation (_on_position_changed) hasn't landed
        # shortly after this attempt, try exactly once more, then accept
        # whatever position is eventually reported rather than retrying
        # indefinitely.
        if self._secondary_seek_attempts < 2:
            self._secondary_seek_retry_timer.start(250)

    def _on_secondary_seek_confirmable(self) -> None:
        self._secondary_seek_confirmable = True

    def _on_secondary_seek_retry(self) -> None:
        # No longer gated on _secondary_pending_start_ms > 0 -- target 0 is
        # just another seek target now (see _on_media_status_changed) and
        # deserves the same bounded retry as any Smart Intro offset.
        if self._secondary_ready:
            return
        self._issue_secondary_seek()

    def _on_error_occurred(self, deck: int, error: int, message: str):
        if error == 0:
            return
        category = _QT_ERROR_INT_TO_CATEGORY.get(error, "video_decode_error")
        if deck == self._primary_index:
            _emit({"event": "error", "token": self._token, "category": category, "message": message or category})
        elif deck == self._secondary_index:
            envelope = self._secondary_event_envelope()
            self._cancel_secondary(reason="error")
            _emit({**envelope, "event": "secondary_failed", "reason": message or category})

    def _on_playback_state_changed(self, deck: int, state: int):
        previous_state = self._deck_playback_state[deck]
        self._deck_playback_state[deck] = state
        if self._deck_state_monitoring_active and state != previous_state:
            _emit({
                "event": "deck_state_changed",
                "deck": deck,
                "role": "primary" if deck == self._primary_index else "secondary",
                "kind": "playback_state",
                "previous": previous_state,
                "previous_name": _PLAYBACK_STATE_NAMES.get(previous_state, "?"),
                "current": state,
                "current_name": _PLAYBACK_STATE_NAMES.get(state, "?"),
            })
        if deck != self._primary_index:
            return
        if state == _PLAYBACK_STATE_PLAYING:
            _emit({"event": "started", "token": self._token})
        elif state == _PLAYBACK_STATE_PAUSED:
            _emit({"event": "paused", "token": self._token})
        elif state == _PLAYBACK_STATE_STOPPED:
            _emit({"event": "stopped", "token": self._token})

    def _on_position_changed(self, deck: int, position: int):
        if deck == self._secondary_index and not self._secondary_ready:
            # Stage B -- the other half of secondary_preload_progress: a
            # file stuck mid-buffer on a slow network share fires exactly
            # one mediaStatus transition (->BufferingMedia) and then
            # nothing further until it either completes or genuinely
            # stalls, so mediaStatus changes alone (see
            # _on_media_status_changed) can't distinguish "still loading"
            # from "stalled" in that specific case -- bufferProgress can,
            # but only if rate-limited: without both the >=0.02 delta and
            # the >=750ms spacing, this would fire on every positionChanged
            # tick, which is explicitly what "bounded, never per-frame"
            # rules out.
            buffer_progress = self._safe_property(self._players[deck], "bufferProgress")
            if buffer_progress is not None:
                last = self._preload_progress_last_buffer
                now = time.monotonic()
                if (
                    (last is None or buffer_progress - last >= 0.02)
                    and now - self._preload_progress_last_emit_monotonic >= 0.75
                ):
                    self._preload_progress_last_buffer = buffer_progress
                    self._preload_progress_last_emit_monotonic = now
                    _emit({
                        **self._secondary_event_envelope(),
                        "event": "secondary_preload_progress",
                        "reason": "buffer_progress_increased",
                        "media_status_name": _MEDIA_STATUS_NAMES.get(self._deck_media_status[deck], "?"),
                        "position_ms": self._safe_property(self._players[deck], "position"),
                        "buffer_progress": buffer_progress,
                        "has_video": self._safe_property(self._players[deck], "hasVideo"),
                    })
        if (
            deck == self._secondary_index
            and self._secondary_seek_issued
            and not self._secondary_ready
        ):
            secondary = self._players[deck]
            # For target 0 this reads as "position >= -100", trivially true
            # for the first real (confirmable) report -- deliberately so.
            # An earlier version of this fix added an explicit upper bound
            # for the zero-target case (reject anything past ~100ms) to try
            # to guarantee an even tighter hold, but that check is not
            # "sticky": nothing pauses the deck between seek and
            # confirmation, so position keeps climbing during continued
            # playback, and once a report arrives past the upper bound the
            # condition could never become true again without another
            # re-seek -- with only the existing bounded 2-attempt retry,
            # that reliably hung secondary_ready forever (caught by the
            # regression test below). The single lower-bound check the
            # non-zero path already uses is sufficient on its own: it
            # confirms as soon as the *first* real post-seek report
            # arrives, which -- because the corrective seek actually reset
            # the deck to 0 moments earlier -- lands within the same
            # ~60-100ms confirm-window drift already proven acceptable for
            # non-zero Smart Intro targets, not the 1200-3300ms of
            # accumulated pre-seek buffering drift this fix exists to close.
            satisfies_tolerance = bool(
                self._secondary_seek_confirmable
                and position >= self._secondary_pending_start_ms - 100
            )
            self._secondary_seek_position_reports += 1
            if self._secondary_seek_position_reports <= 20:
                # Bounded: this window self-terminates the instant
                # secondary_ready fires below, and in practice spans well
                # under the 80ms confirm delay + one 250ms retry -- the cap
                # is a pure defensive belt, not an expected limit.
                _emit({
                    **self._secondary_event_envelope(),
                    "event": "secondary_seek_position_report",
                    "attempt": self._secondary_seek_attempts,
                    "position_ms": int(position),
                    "confirmable": self._secondary_seek_confirmable,
                    "satisfies_tolerance": satisfies_tolerance,
                    "media_status_name": _MEDIA_STATUS_NAMES.get(self._deck_media_status[deck], "?"),
                    "playback_state_name": _PLAYBACK_STATE_NAMES.get(self._deck_playback_state[deck], "?"),
                    "buffer_progress": self._safe_property(secondary, "bufferProgress"),
                    "has_video": self._safe_property(secondary, "hasVideo"),
                    "source_empty": self._source_is_empty(secondary),
                })
            # Second confirmation for Smart Video Transition Points' intro
            # seek (see _on_media_status_changed): only now, once the deck
            # has actually reached at/near the requested position and
            # therefore has a real frame to show there, is the secondary
            # considered ready -- not merely "a frame arrived" (which,
            # right after a seek, could still be a stale pre-seek frame,
            # or even the synchronous requested-value echo the property
            # write itself produces -- see _secondary_seek_confirmable).
            if satisfies_tolerance:
                self._secondary_seek_retry_timer.stop()
                self._hold_and_emit_secondary_ready(deck, via="seek_confirmed")
            return
        if deck != self._primary_index:
            return
        _emit({
            "event": "position_changed",
            "token": self._token,
            "position_ms": int(position),
            "duration_ms": int(self._players[self._primary_index].property("duration") or 0),
            "deck_index": deck,
            "source_hash": self._deck_identity[deck],
            "primary_index": self._primary_index,
            "secondary_index": self._secondary_index,
            "transition_id": self._active_transition_id,
        })

    def _on_duration_changed(self, deck: int, duration: int):
        if deck != self._primary_index:
            return
        _emit({
            "event": "duration_changed",
            "token": self._token,
            "duration_ms": int(duration),
            "deck_index": deck,
            "source_hash": self._deck_identity[deck],
            "primary_index": self._primary_index,
            "secondary_index": self._secondary_index,
            "transition_id": self._active_transition_id,
        })


def _install_child_crash_logging():
    """Mirrors Main.py's _install_crash_logging() for this child process.
    The parent's own faulthandler/crash.log setup (Main.py's
    _install_crash_logging()) only ever runs for the main process --
    Main.py dispatches straight into this module's main() for
    "--video-subprocess" before that setup executes, so a native crash in
    this child (e.g. during GPU/RHI initialisation) previously left no
    trace anywhere: the parent's QProcess only reports the OS-level shape
    of the failure ("Crashed"), never *why*. Appends to the same crash.log
    the parent uses, tagged with this process's own pid and launch mode so
    entries from different processes stay distinguishable when
    interleaved."""
    import faulthandler
    import os
    base = os.environ.get(
        "LOCALAPPDATA", os.path.dirname(os.path.abspath(__file__))
    )
    folder = os.path.join(base, "Bills Music Player")
    try:
        os.makedirs(folder, exist_ok=True)
        handle = open(os.path.join(folder, "crash.log"), "a", encoding="utf-8")
        mode = (
            "gpu" if "--dual-deck-gpu" in sys.argv else
            "cpu" if "--dual-deck" in sys.argv else
            "classic"
        )
        handle.write(
            f"\n--- video subprocess crash capture started "
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} "
            f"mode={mode} ---\n"
        )
        handle.flush()
        faulthandler.enable(file=handle)
    except Exception:
        pass


def main() -> int:
    _install_child_crash_logging()
    dual_mode_gpu = "--dual-deck-gpu" in sys.argv
    dual_mode_cpu = "--dual-deck" in sys.argv and not dual_mode_gpu
    probe_only = "--probe-only" in sys.argv
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    if probe_only:
        return _run_gpu_capability_probe()
    controller = (
        GpuDualDeckVideoSubprocessController() if dual_mode_gpu else
        DualDeckVideoSubprocessController() if dual_mode_cpu else
        VideoSubprocessController()
    )
    # Deferred rather than called inline: the widget's show() call above
    # only actually takes effect once the event loop processes its expose
    # event, so announcing "ready" (and handing the win_id off to the
    # parent for embedding) has to happen after that, not before app.exec()
    # even starts.
    QtCore.QTimer.singleShot(0, controller.announce_ready)
    exit_code = app.exec()
    # The parent closes its end of our stdin as part of its own shutdown()
    # (in addition to sending the "shutdown" command), so the reader
    # thread's blocking readline() unblocks with EOF shortly after
    # app.exec() returns either way. Wait for it here so the QThread is
    # never still running when the controller (and this process) goes
    # away -- avoids a "QThread: Destroyed while thread is still running"
    # crash on interpreter exit.
    controller._reader.wait(2000)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
