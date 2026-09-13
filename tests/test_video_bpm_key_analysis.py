"""Regression coverage for video-audio BPM/Key analysis (via QtMultimedia's
QAudioDecoder -- see workers.py's _decode_video_audio_preview) and the
QueueAnalysisWorker fixes it shipped alongside: field-specific requests
(only recompute what's actually missing), and replaying a cached result
for a duplicate request instead of silently dropping it (the proven
stuck-spinner root cause -- see window.py's queue_bpm_key_pending_fields).

Real-decode feasibility (librosa/audioread/soundfile/miniaudio all fail to
open an MP4 container; QAudioDecoder succeeds) was proven empirically
against real files during planning -- see CODEX_HANDOFF.md. These tests
never touch real audio decode, matching this subsystem's established
convention (monkeypatched decode/estimate functions, or a small fake
QAudioDecoder-like QObject exposing the same signals)."""
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtCore, QtWidgets

from billsmusic import analysis_warmup, workers as workers_module
from billsmusic.workers import QueueAnalysisWorker

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _pump_until(predicate, seconds=5.0):
    app = _app()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# -- _analyse_track: field-specific dispatch, video routing, diagnostics ----

def test_video_path_uses_video_decode_and_computes_only_missing_fields(monkeypatch):
    _app()
    worker = QueueAnalysisWorker()
    worker._analyse_metadata = lambda _path: {}
    calls = []
    worker._decode_video_audio_preview = lambda path: (
        calls.append(("decode", path)) or (workers_module.np.ones(200000, dtype="float32"), 22050)
    )
    worker._decode_preview = lambda path: (_ for _ in ()).throw(
        AssertionError("audio decode path used for a video file")
    )
    worker._estimate_bpm = lambda y, sr: calls.append("bpm") or 128.0
    worker._estimate_key = lambda y, sr: calls.append("key") or "Am"
    monkeypatch.setattr(analysis_warmup, "wait_until_ready", lambda: None)
    monkeypatch.setattr(analysis_warmup, "succeeded", lambda: True)

    result = worker._analyse_track("clip.mp4", frozenset({"bpm"}))

    assert calls == [("decode", "clip.mp4"), "bpm"]  # key estimator never called
    assert result["bpm"] == "128"
    assert "key" not in result


def test_audio_path_still_uses_audio_decode(monkeypatch):
    _app()
    worker = QueueAnalysisWorker()
    worker._analyse_metadata = lambda _path: {}
    calls = []
    worker._decode_preview = lambda path: (
        calls.append(("decode", path)) or (workers_module.np.ones(200000, dtype="float32"), 22050)
    )
    worker._decode_video_audio_preview = lambda path: (_ for _ in ()).throw(
        AssertionError("video decode path used for an audio file")
    )
    worker._estimate_bpm = lambda y, sr: 128.0
    worker._estimate_key = lambda y, sr: "Am"
    monkeypatch.setattr(analysis_warmup, "wait_until_ready", lambda: None)
    monkeypatch.setattr(analysis_warmup, "succeeded", lambda: True)
    # This test targets the librosa decode branch specifically (asserting
    # _decode_video_audio_preview is never reached for audio) -- force it
    # regardless of whether librosa is actually importable in this
    # environment, matching the LIBROSA_AVAILABLE gate _analyse_track
    # itself branches on.
    monkeypatch.setattr(workers_module, "LIBROSA_AVAILABLE", True)

    worker._analyse_track("song.flac", frozenset({"bpm", "key"}))

    assert calls == [("decode", "song.flac")]


def test_no_fields_requested_skips_decode_entirely(monkeypatch):
    _app()
    worker = QueueAnalysisWorker()
    worker._analyse_metadata = lambda _path: {"time": "3:00"}
    worker._decode_preview = lambda path: (_ for _ in ()).throw(
        AssertionError("decode should never run when nothing is missing")
    )

    result = worker._analyse_track("song.flac", frozenset())

    assert result == {"time": "3:00"}


def test_failed_decode_returns_metadata_only_result(monkeypatch):
    _app()
    worker = QueueAnalysisWorker()
    worker._analyse_metadata = lambda _path: {"time": "3:00"}
    worker._decode_video_audio_preview = lambda path: (None, 0)
    monkeypatch.setattr(analysis_warmup, "wait_until_ready", lambda: None)
    monkeypatch.setattr(analysis_warmup, "succeeded", lambda: True)

    result = worker._analyse_track("broken.mp4", frozenset({"bpm", "key"}))

    assert result == {"time": "3:00"}
    assert "bpm" not in result and "key" not in result


def test_analyse_track_emits_bpm_key_lifecycle_diagnostics(monkeypatch):
    _app()
    worker = QueueAnalysisWorker()
    worker._analyse_metadata = lambda _path: {}
    worker._decode_video_audio_preview = lambda path: (
        workers_module.np.ones(200000, dtype="float32"), 22050,
    )
    worker._estimate_bpm = lambda y, sr: 128.0
    worker._estimate_key = lambda y, sr: "Am"
    monkeypatch.setattr(analysis_warmup, "wait_until_ready", lambda: None)
    monkeypatch.setattr(analysis_warmup, "succeeded", lambda: True)
    events = []
    monkeypatch.setattr(
        worker._diagnostics, "record",
        lambda category, operation, **kw: events.append((operation, kw.get("details", {}))),
    )

    worker._analyse_track("clip.mp4", frozenset({"bpm", "key"}))

    names = [e for e, _ in events]
    assert names == [
        "bpm_key_analysis_started", "bpm_key_analysis_completed",
    ]
    started_details = events[0][1]
    assert started_details["requested_fields"] == ["bpm", "key"]
    assert "path" not in started_details  # no full paths, per convention
    completed_details = events[1][1]
    assert completed_details["bpm_present"] is True
    assert completed_details["key_present"] is True
    assert "elapsed_ms" in completed_details


def test_analyse_track_emits_failed_diagnostic_on_decode_failure(monkeypatch):
    _app()
    worker = QueueAnalysisWorker()
    worker._analyse_metadata = lambda _path: {}
    worker._decode_video_audio_preview = lambda path: (None, 0)
    monkeypatch.setattr(analysis_warmup, "wait_until_ready", lambda: None)
    monkeypatch.setattr(analysis_warmup, "succeeded", lambda: True)
    events = []
    monkeypatch.setattr(
        worker._diagnostics, "record",
        lambda category, operation, **kw: events.append((operation, kw.get("details", {}))),
    )

    worker._analyse_track("broken.mp4", frozenset({"bpm", "key"}))

    names = [e for e, _ in events]
    assert names == ["bpm_key_analysis_started", "bpm_key_analysis_failed"]
    assert events[1][1]["reason"] == "decode_failed"


# -- v1.0.68/v1.0.72: frozen-build (LIBROSA_AVAILABLE=False) warmup routing -
#
# Frozen/Nuitka builds deliberately set LIBROSA_AVAILABLE=False (see
# audio.py -- numba JIT doesn't work in Nuitka standalone mode, so librosa
# is never even attempted there). analysis_warmup.succeeded() reflects
# only librosa's own warmup outcome, so it can never succeed when
# LIBROSA_AVAILABLE is False -- _analyse_track used to unconditionally
# require it before reaching ANY decode path, so every video BPM/key
# request failed with reason="warmup_failed" in a frozen build even
# though _decode_video_audio_preview (QAudioDecoder) and _estimate_bpm/
# _estimate_key's NumPy fallback branches never touch librosa at all.
# v1.0.72 closed the matching gap for ordinary AUDIO (see
# tests/test_frozen_audio_bpm_key_analysis.py for its full, dedicated
# coverage) -- the two tests below are the audio-side half of this same
# gate, kept here alongside the video tests it was originally written
# next to.

def test_frozen_build_video_path_skips_warmup_gate_and_reaches_qaudiodecoder(monkeypatch):
    _app()
    worker = QueueAnalysisWorker()
    worker._analyse_metadata = lambda _path: {}
    calls = []
    worker._decode_video_audio_preview = lambda path: (
        calls.append(("decode", path)) or (workers_module.np.ones(200000, dtype="float32"), 22050)
    )
    worker._estimate_bpm = lambda y, sr: calls.append("bpm") or 128.0
    worker._estimate_key = lambda y, sr: calls.append("key") or "Am"
    monkeypatch.setattr(workers_module, "LIBROSA_AVAILABLE", False)

    def _fail_warmup_wait():
        raise AssertionError(
            "frozen-build video analysis must not wait on librosa warmup "
            "at all -- it never touches librosa"
        )
    monkeypatch.setattr(analysis_warmup, "wait_until_ready", _fail_warmup_wait)
    monkeypatch.setattr(analysis_warmup, "succeeded", lambda: False)

    result = worker._analyse_track("clip.mp4", frozenset({"bpm", "key"}))

    assert calls == [("decode", "clip.mp4"), "bpm", "key"]
    assert result["bpm"] == "128"
    assert result["key"] == "Am"


def test_frozen_build_video_path_never_returns_warmup_failed(monkeypatch):
    _app()
    worker = QueueAnalysisWorker()
    worker._analyse_metadata = lambda _path: {}
    worker._decode_video_audio_preview = lambda path: (
        workers_module.np.ones(200000, dtype="float32"), 22050,
    )
    worker._estimate_bpm = lambda y, sr: 128.0
    worker._estimate_key = lambda y, sr: "Am"
    monkeypatch.setattr(workers_module, "LIBROSA_AVAILABLE", False)
    monkeypatch.setattr(analysis_warmup, "succeeded", lambda: False)
    events = []
    monkeypatch.setattr(
        worker._diagnostics, "record",
        lambda category, operation, **kw: events.append((operation, kw.get("details", {}))),
    )

    result = worker._analyse_track("clip.mkv", frozenset({"bpm", "key"}))

    names = [e for e, _ in events]
    assert "bpm_key_analysis_failed" not in names
    assert names == ["bpm_key_analysis_started", "bpm_key_analysis_completed"]
    assert events[0][1]["decoder"] == "qaudiodecoder"
    assert events[0][1]["librosa_available"] is False
    assert result["bpm"] == "128"
    assert result["key"] == "Am"


def test_frozen_build_video_decode_failure_is_a_clean_decode_failed_not_warmup_failed(monkeypatch):
    _app()
    worker = QueueAnalysisWorker()
    worker._analyse_metadata = lambda _path: {"time": "3:00"}
    worker._decode_video_audio_preview = lambda path: (None, 0)
    monkeypatch.setattr(workers_module, "LIBROSA_AVAILABLE", False)
    monkeypatch.setattr(analysis_warmup, "succeeded", lambda: False)
    events = []
    monkeypatch.setattr(
        worker._diagnostics, "record",
        lambda category, operation, **kw: events.append((operation, kw.get("details", {}))),
    )

    result = worker._analyse_track("broken.mp4", frozenset({"bpm", "key"}))

    assert result == {"time": "3:00"}
    assert events[-1][0] == "bpm_key_analysis_failed"
    assert events[-1][1]["reason"] == "decode_failed"


def test_frozen_build_audio_path_now_also_skips_warmup_and_uses_native_decoder(monkeypatch):
    """v1.0.72 superseded the v1.0.68-era assumption that audio must
    always wait on librosa warmup: _decode_native_audio_preview (see
    tests/test_frozen_audio_bpm_key_analysis.py for its full coverage)
    reuses the same QAudioDecoder machinery video already relied on, so
    an ordinary audio file in a frozen build must skip the warmup gate
    and reach that native decoder exactly like video does -- never
    warmup_failed, and never the librosa/soundfile _decode_preview path."""
    _app()
    worker = QueueAnalysisWorker()
    worker._analyse_metadata = lambda _path: {}
    calls = []
    worker._decode_native_audio_preview = lambda path: (
        calls.append(("decode", path)) or (workers_module.np.ones(200000, dtype="float32"), 22050)
    )
    worker._decode_preview = lambda path: (_ for _ in ()).throw(
        AssertionError("frozen-build audio must use the native decoder, not librosa/soundfile")
    )
    worker._estimate_bpm = lambda y, sr: 123.0
    worker._estimate_key = lambda y, sr: "Am"
    monkeypatch.setattr(workers_module, "LIBROSA_AVAILABLE", False)

    def _fail_warmup_wait():
        raise AssertionError(
            "frozen-build audio analysis must not wait on librosa warmup "
            "at all -- its native decode route never touches librosa"
        )
    monkeypatch.setattr(analysis_warmup, "wait_until_ready", _fail_warmup_wait)
    monkeypatch.setattr(analysis_warmup, "succeeded", lambda: False)

    result = worker._analyse_track("song.flac", frozenset({"bpm", "key"}))

    assert calls == [("decode", "song.flac")]
    assert result["bpm"] == "123"
    assert result["key"] == "Am"


def test_audio_path_with_librosa_available_still_requires_warmup(monkeypatch):
    """The other half: when LIBROSA_AVAILABLE is True, an ordinary audio
    file keeps the original gate and the original librosa/soundfile
    decode route -- the v1.0.72 native fallback is librosa-unavailable-only,
    never a universal replacement for _decode_preview."""
    _app()
    worker = QueueAnalysisWorker()
    worker._analyse_metadata = lambda _path: {}
    worker._decode_preview = lambda path: (_ for _ in ()).throw(
        AssertionError("must not decode when warmup is required and failed")
    )
    worker._decode_native_audio_preview = lambda path: (_ for _ in ()).throw(
        AssertionError("native fallback must not be used when librosa is available")
    )
    monkeypatch.setattr(workers_module, "LIBROSA_AVAILABLE", True)
    monkeypatch.setattr(analysis_warmup, "wait_until_ready", lambda: None)
    monkeypatch.setattr(analysis_warmup, "succeeded", lambda: False)

    result = worker._analyse_track("song.flac", frozenset({"bpm", "key"}))

    assert "bpm" not in result and "key" not in result


def test_video_path_with_librosa_available_still_requires_warmup(monkeypatch):
    """The other half of the same proof: when LIBROSA_AVAILABLE is True
    (a normal source run, or a hypothetical future frozen build with
    librosa re-enabled) a video path keeps the original gate too -- the
    skip only applies to the specific librosa-unavailable case."""
    _app()
    worker = QueueAnalysisWorker()
    worker._analyse_metadata = lambda _path: {}
    worker._decode_video_audio_preview = lambda path: (_ for _ in ()).throw(
        AssertionError("must not decode when warmup is required and failed")
    )
    monkeypatch.setattr(workers_module, "LIBROSA_AVAILABLE", True)
    monkeypatch.setattr(analysis_warmup, "wait_until_ready", lambda: None)
    monkeypatch.setattr(analysis_warmup, "succeeded", lambda: False)

    result = worker._analyse_track("clip.mp4", frozenset({"bpm", "key"}))

    assert "bpm" not in result and "key" not in result


# -- Worker-level: duplicate-after-_done replay (the stuck-spinner fix) -----

def test_duplicate_request_before_completion_is_deduped_not_requeued():
    worker = QueueAnalysisWorker()
    worker.request("song.flac")
    worker.request("song.flac")
    assert len(worker._queue) == 1


def test_duplicate_request_after_completion_replays_synchronously():
    """The core stuck-spinner fix: previously, _enqueue silently dropped a
    repeat request once its (path, metadata_only) key reached _done (which
    is never cleared for the worker's lifetime) -- no signal was ever
    emitted, so the caller's pending/spinner bookkeeping never resolved.
    request()/request_metadata() only run on the GUI thread, so a replay
    must resolve synchronously within the same call."""
    worker = QueueAnalysisWorker()
    cached_result = {"bpm": "128", "key": "Am"}
    worker._done.add(("song.flac", False))
    worker._done_results[("song.flac", False)] = cached_result

    received = []
    worker.result_ready.connect(lambda path, result: received.append((path, result)))
    worker.request("song.flac")

    assert received == [("song.flac", cached_result)]
    assert ("song.flac", False) not in worker._queued
    assert len(worker._queue) == 0


def test_duplicate_metadata_request_after_completion_replays_via_metadata_ready():
    worker = QueueAnalysisWorker()
    cached_result = {"time": "3:00"}
    worker._done.add(("clip.mp4", True))
    worker._done_results[("clip.mp4", True)] = cached_result

    received = []
    worker.metadata_ready.connect(lambda path, result: received.append((path, result)))
    worker.request_metadata("clip.mp4")

    assert received == [("clip.mp4", cached_result)]


# -- QAudioDecoder lifecycle: explicit stop-before-quit, cancellation, ------
# -- error, and normal completion, via a small fake decoder QObject --------

class _FakeAudioFormat:
    def __init__(self, sample_format, channels=1, rate=22050):
        self._sample_format = sample_format
        self._channels = channels
        self._rate = rate

    def sampleFormat(self):
        return self._sample_format

    def channelCount(self):
        return self._channels

    def sampleRate(self):
        return self._rate


class _FakePtr:
    def __init__(self, data: bytes):
        self._data = data

    def setsize(self, size):
        pass

    def asstring(self, size):
        return self._data[:size]


class _FakeAudioBuffer:
    def __init__(self, data: bytes, fmt):
        self._data = data
        self._fmt = fmt

    def isValid(self):
        return True

    def format(self):
        return self._fmt

    def byteCount(self):
        return len(self._data)

    def constData(self):
        return _FakePtr(self._data)


class _FakeQAudioDecoder(QtCore.QObject):
    """Exposes the same signals/methods _decode_video_audio_preview uses,
    without touching Qt Multimedia or a real file."""
    bufferReady = QtCore.pyqtSignal()
    finished = QtCore.pyqtSignal()
    error = QtCore.pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self.stop_calls = 0
        self.start_calls = 0
        self._pending_buffer = None

    def setSource(self, url):
        pass

    def start(self):
        self.start_calls += 1

    def stop(self):
        self.stop_calls += 1

    def read(self):
        buf = self._pending_buffer
        self._pending_buffer = None
        return buf

    def push_buffer(self, buf):
        self._pending_buffer = buf
        self.bufferReady.emit()


@pytest.fixture
def fake_decoder(monkeypatch):
    instances = []

    def factory():
        instance = _FakeQAudioDecoder()
        instances.append(instance)
        return instance

    monkeypatch.setattr(workers_module.QtMultimedia, "QAudioDecoder", factory)
    yield instances


def test_decode_normal_completion_returns_expected_array(fake_decoder, monkeypatch):
    _app()
    worker = QueueAnalysisWorker()
    SampleFormat = workers_module.QtMultimedia.QAudioFormat.SampleFormat
    # 22050 samples/sec * (20 + 90 + 2)s worth, mono float32 -- one big
    # buffer is enough to satisfy the early-stop threshold in one shot.
    sample_count = int(112 * 22050)
    data = (workers_module.np.linspace(-1.0, 1.0, sample_count, dtype="float32")).tobytes()
    fmt = _FakeAudioFormat(SampleFormat.Float, channels=1, rate=22050)

    def on_decoder_created():
        pass

    QtCore.QTimer.singleShot(0, lambda: _feed(fake_decoder, data, fmt))

    def _feed(instances, data, fmt):
        if instances:
            instances[-1].push_buffer(_FakeAudioBuffer(data, fmt))

    y, sr = worker._decode_video_audio_preview("clip.mp4")

    assert sr == 22050
    assert y is not None
    assert y.size == int(90 * 22050)  # exactly the duration window
    decoder = fake_decoder[-1]
    assert decoder.stop_calls >= 1


def test_decode_hard_timeout_stops_decoder_and_returns_promptly(fake_decoder):
    _app()
    worker = QueueAnalysisWorker()
    worker.VIDEO_DECODE_HARD_TIMEOUT_MS = 150
    worker.VIDEO_DECODE_POLL_INTERVAL_MS = 50000  # disable the cancel-poll path

    started = time.monotonic()
    y, sr = worker._decode_video_audio_preview("stuck.mp4")
    elapsed = time.monotonic() - started

    assert (y, sr) == (None, 0)
    assert elapsed < 2.0  # bounded by the (shortened) hard timeout, not hung
    decoder = fake_decoder[-1]
    assert decoder.stop_calls >= 1  # stop() called before the loop unwound


def test_decode_cancellation_via_running_flag_stops_decoder_promptly(fake_decoder):
    _app()
    worker = QueueAnalysisWorker()
    worker.VIDEO_DECODE_HARD_TIMEOUT_MS = 20000
    worker.VIDEO_DECODE_POLL_INTERVAL_MS = 20

    def cancel_soon():
        worker._running = False

    QtCore.QTimer.singleShot(60, cancel_soon)
    started = time.monotonic()
    y, sr = worker._decode_video_audio_preview("cancel-me.mp4")
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    decoder = fake_decoder[-1]
    assert decoder.stop_calls >= 1


def test_decode_error_signal_stops_decoder_and_returns_safely(fake_decoder):
    _app()
    worker = QueueAnalysisWorker()

    def emit_error():
        fake_decoder[-1].error.emit(2)  # QAudioDecoder.Error.FormatError

    QtCore.QTimer.singleShot(0, emit_error)
    started = time.monotonic()
    y, sr = worker._decode_video_audio_preview("broken.mp4")
    elapsed = time.monotonic() - started

    assert (y, sr) == (None, 0)
    assert elapsed < 2.0
    decoder = fake_decoder[-1]
    assert decoder.stop_calls >= 1


# -- Application shutdown: QueueAnalysisWorker.stop() during a real job -----

def test_worker_stop_interrupts_a_running_video_decode_promptly():
    """QueueAnalysisWorker.stop() (app shutdown) must interrupt a video
    decode that's still in flight, not leave the thread hung -- the
    decode function's own self._running poll (see the cancellation test
    above) is what makes this possible."""
    _app()
    worker = QueueAnalysisWorker()
    decode_entered = threading.Event()
    release_decode = threading.Event()
    worker._analyse_metadata = lambda _path: {}

    def blocking_decode(_path):
        decode_entered.set()
        release_decode.wait(2.0)
        return None, 0

    worker._decode_video_audio_preview = blocking_decode
    import unittest.mock as _mock
    with _mock.patch.object(analysis_warmup, "wait_until_ready", lambda: None), \
         _mock.patch.object(analysis_warmup, "succeeded", lambda: True):
        worker.request("clip.mp4")
        thread = threading.Thread(target=worker.run)
        thread.start()
        try:
            assert decode_entered.wait(2.0)
            worker.stop()
            release_decode.set()
            thread.join(3.0)
            assert not thread.is_alive()
        finally:
            release_decode.set()
            worker._running = False
            thread.join(2.0)
