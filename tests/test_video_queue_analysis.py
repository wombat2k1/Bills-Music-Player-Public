"""Video's audio track now reaches genuine BPM/key analysis (via
QAudioDecoder -- see workers.py's _decode_video_audio_preview); only
KARAOKE (.cdg/.zip, no independent audio stream) stays permanently
excluded. Music (or video) queued while a *different* video is current
gets metadata-only treatment with real analysis deferred until it stops
(see window.py's _request_queue_analysis and
_resume_deferred_queue_analysis, and workers.py's QueueAnalysisWorker.
request())."""
import os
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import billsmusic.workers as workers
import billsmusic.window as window_module
from billsmusic.media_type import MediaType
from billsmusic.waveform_worker import WaveformWorker
from billsmusic.window import PlayerWindow


def test_queue_analysis_worker_request_now_full_analyses_a_video_path():
    worker = workers.QueueAnalysisWorker()
    enqueued = []
    worker._enqueue = lambda path, metadata_only, fields=frozenset(): enqueued.append(
        (path, metadata_only, fields)
    )

    worker.request("clip.mp4")

    assert enqueued == [("clip.mp4", False, frozenset({"bpm", "key"}))]


def test_queue_analysis_worker_request_never_full_analyses_a_karaoke_path():
    worker = workers.QueueAnalysisWorker()
    enqueued = []
    worker._enqueue = lambda path, metadata_only, fields=frozenset(): enqueued.append(
        (path, metadata_only, fields)
    )

    worker.request("song.cdg")

    assert enqueued == [("song.cdg", True, frozenset())]


def test_queue_analysis_worker_request_still_full_analyses_audio():
    worker = workers.QueueAnalysisWorker()
    enqueued = []
    worker._enqueue = lambda path, metadata_only, fields=frozenset(): enqueued.append(
        (path, metadata_only, fields)
    )

    worker.request("song.flac")

    assert enqueued == [("song.flac", False, frozenset({"bpm", "key"}))]


def _analysis_window(current_media_type, current_path=None, queue=None):
    worker = MagicMock()
    queue = list(queue) if queue else []
    window = SimpleNamespace(
        queue_analysis_worker=worker,
        queue_analysis_pending=set(),
        queue_bpm_key_pending_fields={},
        queue_detail_cache={},
        _deferred_bpm_key_paths=set(),
        _queue_priority_deferred_paths=set(),
        _visualiser_analysis_busy=False,
        _current_media_type=current_media_type,
        current_path=current_path,
        queue=queue,
        queue_played=[False] * len(queue),
        diagnostics=MagicMock(path_details=lambda path: {"path_hash": "x"}),
        queue_spinner_timer=SimpleNamespace(
            isActive=lambda: False, start=lambda: None,
        ),
    )
    window._ensure_queue_played_flags = lambda: PlayerWindow._ensure_queue_played_flags(window)
    window._queue_priority_paths = lambda: PlayerWindow._queue_priority_paths(window)
    return window, worker


def test_karaoke_path_queued_always_gets_metadata_only_regardless_of_playback_state():
    window, worker = _analysis_window(MediaType.AUDIO)
    details = {"time": "--", "bitrate": "--", "key": "--", "bpm": "--"}

    PlayerWindow._request_queue_analysis(window, "song.cdg", details)

    worker.request_metadata.assert_called_once_with("song.cdg")
    worker.request.assert_not_called()
    assert "song.cdg" not in window._deferred_bpm_key_paths
    assert "song.cdg" not in window.queue_bpm_key_pending_fields


def test_video_path_queued_gets_real_bpm_key_analysis_when_not_busy():
    window, worker = _analysis_window(MediaType.AUDIO)
    details = {"time": "--", "bitrate": "--", "key": "--", "bpm": "--"}

    PlayerWindow._request_queue_analysis(window, "clip.mp4", details)

    worker.request.assert_called_once_with("clip.mp4", fields=frozenset({"bpm", "key"}))
    worker.request_metadata.assert_not_called()
    assert "clip.mp4" not in window._deferred_bpm_key_paths
    assert window.queue_bpm_key_pending_fields["clip.mp4"] == {"bpm", "key"}


def test_current_video_track_always_gets_real_analysis_despite_its_own_media_type():
    # The current track's own media type being VIDEO must never defer its
    # own analysis against itself via the video-busy gate.
    window, worker = _analysis_window(MediaType.VIDEO, current_path="clip.mp4")
    details = {"time": "--", "bitrate": "--", "key": "--", "bpm": "--"}

    PlayerWindow._request_queue_analysis(window, "clip.mp4", details)

    worker.request.assert_called_once_with("clip.mp4", fields=frozenset({"bpm", "key"}))
    worker.request_metadata.assert_not_called()
    assert "clip.mp4" not in window._deferred_bpm_key_paths


def test_audio_path_queued_while_video_plays_is_deferred():
    window, worker = _analysis_window(MediaType.VIDEO)
    details = {"time": "--", "bitrate": "--", "key": "--", "bpm": "--"}

    PlayerWindow._request_queue_analysis(window, "song.flac", details)

    worker.request_metadata.assert_called_once_with("song.flac")
    worker.request.assert_not_called()
    assert "song.flac" in window._deferred_bpm_key_paths
    assert "song.flac" not in window.queue_bpm_key_pending_fields


def test_video_path_queued_while_different_video_plays_is_deferred():
    window, worker = _analysis_window(MediaType.VIDEO, current_path="currently_playing.mp4")
    details = {"time": "--", "bitrate": "--", "key": "--", "bpm": "--"}

    PlayerWindow._request_queue_analysis(window, "upcoming.mp4", details)

    worker.request_metadata.assert_called_once_with("upcoming.mp4")
    worker.request.assert_not_called()
    assert "upcoming.mp4" in window._deferred_bpm_key_paths


def test_audio_path_queued_while_audio_plays_gets_full_analysis_immediately():
    window, worker = _analysis_window(MediaType.AUDIO)
    details = {"time": "--", "bitrate": "--", "key": "--", "bpm": "--"}

    PlayerWindow._request_queue_analysis(window, "song.flac", details)

    worker.request.assert_called_once_with("song.flac", fields=frozenset({"bpm", "key"}))
    worker.request_metadata.assert_not_called()
    assert "song.flac" not in window._deferred_bpm_key_paths


def test_only_missing_fields_are_requested_when_key_is_already_known():
    # A real queue row observed with Key known and BPM missing: BPM must
    # keep spinning/being computed, Key must never be re-requested.
    window, worker = _analysis_window(MediaType.AUDIO)
    details = {"time": "3:45", "bitrate": "320k", "key": "--", "bpm": "129"}

    PlayerWindow._request_queue_analysis(window, "song.flac", details)

    worker.request.assert_called_once_with("song.flac", fields=frozenset({"key"}))
    assert window.queue_bpm_key_pending_fields["song.flac"] == {"key"}


def test_only_missing_fields_are_requested_when_bpm_is_already_known():
    # The mirror case: a real queue row observed with BPM = 129 known and
    # Key missing -- BPM must never be recomputed, only Key requested.
    window, worker = _analysis_window(MediaType.AUDIO)
    details = {"time": "3:45", "bitrate": "320k", "key": "Am", "bpm": "--"}

    PlayerWindow._request_queue_analysis(window, "song.flac", details)

    worker.request.assert_called_once_with("song.flac", fields=frozenset({"bpm"}))
    assert window.queue_bpm_key_pending_fields["song.flac"] == {"bpm"}


def test_both_bpm_and_key_already_known_requests_nothing():
    window, worker = _analysis_window(MediaType.AUDIO)
    details = {"time": "3:45", "bitrate": "320k", "key": "Am", "bpm": "129"}

    PlayerWindow._request_queue_analysis(window, "song.flac", details)

    worker.request.assert_not_called()
    worker.request_metadata.assert_not_called()
    assert "song.flac" not in window.queue_bpm_key_pending_fields
    assert "song.flac" not in window.queue_analysis_pending


def test_audio_analysis_is_deferred_while_visualiser_prepares_track():
    window, worker = _analysis_window(MediaType.AUDIO)
    window._visualiser_analysis_busy = True
    details = {"time": "--", "bitrate": "--", "key": "--", "bpm": "--"}

    PlayerWindow._request_queue_analysis(window, "song.flac", details)

    worker.request_metadata.assert_called_once_with("song.flac")
    worker.request.assert_not_called()
    assert window._deferred_bpm_key_paths == {"song.flac"}


def test_deferred_analysis_waits_until_visualiser_is_idle():
    window, worker = _analysis_window(MediaType.AUDIO)
    window._visualiser_analysis_busy = True
    window._deferred_bpm_key_paths = {"song.flac"}
    window.queue_detail_cache["song.flac"] = {
        "time": "--", "bitrate": "--", "key": "--", "bpm": "--",
    }

    PlayerWindow._resume_deferred_queue_analysis(window)

    worker.request.assert_not_called()
    assert window._deferred_bpm_key_paths == {"song.flac"}

    window._visualiser_analysis_busy = False
    PlayerWindow._resume_deferred_queue_analysis(window)

    worker.request.assert_called_once_with("song.flac", fields=frozenset({"bpm", "key"}))
    assert window._deferred_bpm_key_paths == set()


def test_visualiser_idle_signal_resumes_deferred_analysis(monkeypatch):
    resumed = []
    beat = MagicMock()
    window = SimpleNamespace(
        current_path="song.flac",
        beat=beat,
        _visualiser_analysis_busy=True,
        _deferred_bpm_key_paths={"song.flac"},
        _resume_deferred_queue_analysis=lambda: resumed.append(True),
    )
    monkeypatch.setattr(
        workers.QtCore.QTimer, "singleShot", lambda _delay, callback: callback()
    )

    PlayerWindow._on_analysis_busy(window, "song.flac", False)

    assert window._visualiser_analysis_busy is False
    assert resumed == [True]
    beat.set_analyzing.assert_called_once_with(False)


def test_visualiser_bpm_and_waveform_workers_share_native_analysis_gate(monkeypatch):
    from billsmusic import analysis_warmup

    visualiser_entered = threading.Event()
    release_visualiser = threading.Event()
    bpm_entered = threading.Event()
    waveform_entered = threading.Event()

    analyzer_worker = workers.AnalyzerWorker()
    analyzer_worker._pending_path = "playing.flac"

    class FakeAnalyzer:
        last_rms_db = None

        def load(self, _path):
            pass

        def build_mel_cache_chunked(self, chunk_sec):
            assert chunk_sec == 5.0
            visualiser_entered.set()
            release_visualiser.wait(2.0)
            analyzer_worker._running = False

    analyzer_worker._analyzer = FakeAnalyzer()
    analyzer_thread = threading.Thread(target=analyzer_worker.run)

    queue_worker = workers.QueueAnalysisWorker()
    queue_worker._analyse_metadata = lambda _path: {}
    queue_worker._decode_preview = lambda _path: (
        bpm_entered.set() or workers.np.ones(80, dtype="float32"),
        10,
    )
    queue_worker._estimate_bpm = lambda _samples, _rate: 120.0
    queue_worker._estimate_key = lambda _samples, _rate: "Am"
    monkeypatch.setattr(analysis_warmup, "wait_until_ready", lambda: None)
    monkeypatch.setattr(analysis_warmup, "succeeded", lambda: True)
    # Force the librosa decode branch (_decode_preview, stubbed above) so
    # this test exercises the intended path regardless of whether librosa
    # is actually importable in this environment -- otherwise
    # _analyse_track takes the _decode_native_audio_preview fallback
    # instead and the stub above is never reached.
    monkeypatch.setattr(workers, "LIBROSA_AVAILABLE", True)
    queue_thread = threading.Thread(
        target=lambda: queue_worker._analyse_track("queued.flac")
    )

    waveform_worker = WaveformWorker()
    waveform_worker._decode = lambda _path: waveform_entered.set() or None
    waveform_thread = threading.Thread(
        target=lambda: waveform_worker._process("waveform.flac")
    )

    try:
        analyzer_thread.start()
        assert visualiser_entered.wait(1.0)
        queue_thread.start()
        waveform_thread.start()
        assert not bpm_entered.wait(0.15)
        assert not waveform_entered.wait(0.15)
    finally:
        release_visualiser.set()
        analyzer_thread.join(2.0)
        queue_thread.join(2.0)
        waveform_thread.join(2.0)

    assert not analyzer_thread.is_alive()
    assert not queue_thread.is_alive()
    assert not waveform_thread.is_alive()
    assert bpm_entered.is_set()
    assert waveform_entered.is_set()


def test_repeated_play_next_during_video_defers_every_track_and_all_resume_on_stop():
    window, worker = _analysis_window(MediaType.VIDEO)
    tracks = ["one.flac", "two.mp3", "three.wav"]
    for path in tracks:
        details = {"time": "--", "bitrate": "--", "key": "--", "bpm": "--"}
        window.queue_detail_cache[path] = details
        PlayerWindow._request_queue_analysis(window, path, details)

    assert worker.request.call_count == 0
    assert worker.request_metadata.call_count == 3
    assert window._deferred_bpm_key_paths == set(tracks)

    # Video stops -- every deferred track gets a real analysis request now.
    PlayerWindow._resume_deferred_queue_analysis(window)

    assert window._deferred_bpm_key_paths == set()
    requested = {call.args[0] for call in worker.request.call_args_list}
    assert requested == set(tracks)
    for call in worker.request.call_args_list:
        assert call.kwargs["fields"] == frozenset({"bpm", "key"})


def test_resume_deferred_queue_analysis_is_a_no_op_when_nothing_deferred():
    window, worker = _analysis_window(MediaType.AUDIO)

    PlayerWindow._resume_deferred_queue_analysis(window)

    worker.request.assert_not_called()


def test_video_to_audio_transition_stops_backend_and_resumes_deferred_analysis():
    video_backend = MagicMock()
    resumed = []
    window = SimpleNamespace(
        _current_media_type=MediaType.VIDEO,
        _video_backend=video_backend,
        _detach_video_from_party_mode=lambda: None,
        _show_normal_display_page=lambda: None,
        _resume_deferred_queue_analysis=lambda: resumed.append(1),
    )

    PlayerWindow._stop_video_for_audio_transition(window)

    video_backend.stop.assert_called_once()
    assert window._current_media_type == MediaType.AUDIO
    assert resumed == [1]


# -- Bounded prioritisation: current track + next 3 Up Next -----------------
# (widened from next-2 to next-3 on 2026-09-06: real-device video BPM/Key
# analysis can take ~7-16s via QAudioDecoder, longer than a viewer
# typically spends on the second Up Next row before it becomes current.)

def _priority_window(queue, current_path=None, current_media_type=MediaType.AUDIO):
    worker = MagicMock()
    details_by_path = {
        path: {"time": "--", "bitrate": "--", "key": "--", "bpm": "--"}
        for path in queue
    }
    window = SimpleNamespace(
        queue=list(queue),
        queue_played=[False] * len(queue),
        current_path=current_path,
        queue_analysis_worker=worker,
        queue_analysis_pending=set(),
        queue_bpm_key_pending_fields={},
        queue_detail_cache=dict(details_by_path),
        _deferred_bpm_key_paths=set(),
        _queue_priority_deferred_paths=set(),
        _visualiser_analysis_busy=False,
        _current_media_type=current_media_type,
        diagnostics=MagicMock(path_details=lambda path: {"path_hash": "x"}),
        queue_spinner_timer=SimpleNamespace(isActive=lambda: False, start=lambda: None),
    )
    window._ensure_queue_played_flags = lambda: PlayerWindow._ensure_queue_played_flags(window)
    window._queue_priority_paths = lambda: PlayerWindow._queue_priority_paths(window)
    window._queue_track_details = lambda path, cached_details_only=False: window.queue_detail_cache[path]
    window._request_queue_analysis = lambda path, details: PlayerWindow._request_queue_analysis(window, path, details)
    window._request_metadata_only_queue_analysis = (
        lambda path, details: PlayerWindow._request_metadata_only_queue_analysis(window, path, details)
    )
    window._request_queue_analysis_for_paths = (
        lambda paths: PlayerWindow._request_queue_analysis_for_paths(window, paths)
    )
    window._promote_priority_queue_analysis = (
        lambda: PlayerWindow._promote_priority_queue_analysis(window)
    )
    return window, worker


def test_priority_paths_are_current_track_plus_next_three_unplayed():
    window, _worker = _priority_window(
        ["a.mp3", "b.mp3", "c.mp3", "d.mp3", "e.mp3"], current_path="a.mp3",
    )
    assert window._queue_priority_paths() == ["a.mp3", "b.mp3", "c.mp3", "d.mp3"]


def test_priority_paths_skip_already_played_rows():
    window, _worker = _priority_window(
        ["a.mp3", "b.mp3", "c.mp3", "d.mp3", "e.mp3"], current_path="a.mp3",
    )
    window.queue_played = [True, True, False, False, False]
    assert window._queue_priority_paths() == ["a.mp3", "c.mp3", "d.mp3", "e.mp3"]


def test_batch_add_only_requests_real_analysis_for_the_priority_window():
    # "Play Next" on an entire album/artist -- an 11-track batch -- must
    # not analyse the whole queue simultaneously.
    queue = [f"track{i}.mp3" for i in range(11)]
    window, worker = _priority_window(queue, current_path="track0.mp3")

    window._request_queue_analysis_for_paths(queue)

    real_requested = {call.args[0] for call in worker.request.call_args_list}
    assert real_requested == {"track0.mp3", "track1.mp3", "track2.mp3", "track3.mp3"}
    metadata_requested = {call.args[0] for call in worker.request_metadata.call_args_list}
    assert metadata_requested == set(queue[4:])
    assert window._queue_priority_deferred_paths == set(queue[4:])


def test_promotion_requests_real_analysis_once_priority_window_shifts():
    queue = [f"track{i}.mp3" for i in range(8)]
    window, worker = _priority_window(queue, current_path="track0.mp3")
    window._request_queue_analysis_for_paths(queue)
    worker.reset_mock()
    assert window._queue_priority_deferred_paths == {
        "track4.mp3", "track5.mp3", "track6.mp3", "track7.mp3",
    }

    # The current track advances to track2 -- the earlier rows are now
    # played (standard Up Next queue semantics), so track4/track5 enter
    # the next-3 window; track6/track7 remain outside it.
    window.current_path = "track2.mp3"
    window.queue_played = [True, True, False, False, False, False, False, False]
    window._promote_priority_queue_analysis()

    # Window is now [track2 (current), track3, track4, track5] -- track3
    # already had real analysis from the initial batch; track4/track5 are
    # newly promoted; track6/track7 are still one row further out.
    real_requested = {call.args[0] for call in worker.request.call_args_list}
    assert real_requested == {"track4.mp3", "track5.mp3"}
    assert "track6.mp3" not in real_requested
    assert "track7.mp3" not in real_requested
    assert window._queue_priority_deferred_paths == {"track6.mp3", "track7.mp3"}


# -- Real-device bug (2026-09-06): video-busy deferral must not override --
# -- the bounded priority window ------------------------------------------

def test_video_within_priority_window_gets_full_analysis_even_while_a_different_video_plays():
    # CURRENT VIDEO + several Up Next videos: the next-three videos must
    # be promoted to real BPM/Key analysis by the bounded priority window
    # regardless of a *different* video currently playing. Real-device
    # evidence: Up Next videos inside this window never got a
    # bpm_key_analysis_requested/started/completed at all -- only
    # queue_track_metadata -- because _request_queue_analysis's own
    # video-busy deferral fired for any non-current path whenever
    # _current_media_type == VIDEO, with no exception for priority-window
    # membership.
    queue = ["current.mp4", "next1.mp4", "next2.mp4", "next3.mp4", "far.mp4"]
    window, worker = _priority_window(
        queue, current_path="current.mp4", current_media_type=MediaType.VIDEO,
    )

    for path in queue[1:]:
        window._request_queue_analysis(path, window.queue_detail_cache[path])

    real_requested = {call.args[0] for call in worker.request.call_args_list}
    assert real_requested == {"next1.mp4", "next2.mp4", "next3.mp4"}
    metadata_requested = {call.args[0] for call in worker.request_metadata.call_args_list}
    assert metadata_requested == {"far.mp4"}
    assert window._deferred_bpm_key_paths == {"far.mp4"}
    assert "next1.mp4" not in window._deferred_bpm_key_paths
    assert "next2.mp4" not in window._deferred_bpm_key_paths
    assert "next3.mp4" not in window._deferred_bpm_key_paths


def test_video_busy_deferred_path_is_promoted_once_it_enters_the_priority_window():
    # A path correctly deferred while genuinely outside the window (video
    # busy AND beyond current+next-3) must still be promoted once the
    # window shifts to include it, the same as a batch-add deferral is.
    queue = ["current.mp4", "next1.mp4", "next2.mp4", "next3.mp4", "far.mp4"]
    window, worker = _priority_window(
        queue, current_path="current.mp4", current_media_type=MediaType.VIDEO,
    )
    for path in queue[1:]:
        window._request_queue_analysis(path, window.queue_detail_cache[path])
    assert window._deferred_bpm_key_paths == {"far.mp4"}
    worker.reset_mock()

    # Playback advances past current.mp4 and next1.mp4 -- far.mp4 now
    # enters the current+next-3 window.
    window.current_path = "next3.mp4"
    window.queue_played = [True, True, True, False, False]
    window._promote_priority_queue_analysis()

    assert window._deferred_bpm_key_paths == set()
    real_requested = {call.args[0] for call in worker.request.call_args_list}
    assert real_requested == {"far.mp4"}


def test_current_video_still_gets_real_analysis_when_batch_added_via_priority_helper():
    # The same real-device scenario, but reached the way an actual
    # multi-file Up Next add does: _request_queue_analysis_for_paths ->
    # _request_queue_analysis for priority-window members.
    queue = ["current.mp4", "next1.mp4", "next2.mp4", "next3.mp4", "far.mp4"]
    window, worker = _priority_window(
        queue, current_path="current.mp4", current_media_type=MediaType.VIDEO,
    )

    window._request_queue_analysis_for_paths(queue)

    real_requested = {call.args[0] for call in worker.request.call_args_list}
    assert real_requested == {"current.mp4", "next1.mp4", "next2.mp4", "next3.mp4"}
    metadata_requested = {call.args[0] for call in worker.request_metadata.call_args_list}
    assert metadata_requested == {"far.mp4"}


# -- Pre-build safety check: a promoted real request must not have its --
# -- pending/spinner state clobbered by a stale metadata-only companion --
# -- job's completion arriving afterward. The QueueAnalysisWorker thread --
# -- is single-threaded and FIFO (see workers.py's run()), so the two --
# -- jobs never execute concurrently -- but the metadata-only job, having --
# -- been queued first (at the original out-of-window deferral), will --
# -- always *complete* before the promoted real job does. ------------------

def _completion_window(current_path="song.mp4"):
    window = SimpleNamespace(
        queue_analysis_pending=set(),
        queue_bpm_key_pending_fields={},
        queue_detail_cache={},
        current_path=current_path,
        queue=[],
        diagnostics=MagicMock(),
        _audio_log=lambda *a, **k: None,
        _audio_name=lambda path: path,
        _store_queue_analysis=lambda *a, **k: None,
        _queue_row_widgets={},
        queue_spinner_timer=SimpleNamespace(stop=lambda: None, isActive=lambda: False, start=lambda: None),
        _queue_metadata_pending=None,
        _request_next_legacy_queue_metadata=lambda: None,
    )
    window._on_queue_analysis_ready = (
        lambda path, result, metadata_only=False:
            PlayerWindow._on_queue_analysis_ready(window, path, result, metadata_only=metadata_only)
    )
    window._on_queue_metadata_ready = (
        lambda path, result: PlayerWindow._on_queue_metadata_ready(window, path, result)
    )
    return window


def test_stale_metadata_completion_does_not_clear_a_promoted_real_requests_pending_state(monkeypatch):
    monkeypatch.setattr(
        window_module.QtCore.QTimer, "singleShot", lambda _delay, _callback: None
    )
    window = _completion_window(current_path=None)
    path = "upcoming.mp4"

    # Simulate the exact promotion race: a metadata-only job was queued
    # first (original out-of-window deferral), then the window shifted to
    # include this path and a real request was dispatched for it too
    # (_promote_priority_queue_analysis) -- both are now genuinely
    # in-flight in the worker's FIFO queue.
    window.queue_analysis_pending.add(path)
    window.queue_bpm_key_pending_fields[path] = {"bpm", "key"}

    # The metadata-only job, queued earlier, completes first.
    window._on_queue_metadata_ready(path, {"time": "3:45", "bitrate": "320k"})

    # The real request's own pending/spinner tracking must survive this
    # stale completion untouched -- it is still genuinely running.
    assert path in window.queue_bpm_key_pending_fields
    assert window.queue_bpm_key_pending_fields[path] == {"bpm", "key"}
    # Time/Bitrate (fields the metadata job actually reads) are still
    # applied normally.
    assert window.queue_detail_cache[path]["time"] == "3:45"
    assert window.queue_detail_cache[path]["bitrate"] == "320k"

    # The real job then genuinely completes -- its result must land and
    # correctly clear the pending state.
    window._on_queue_analysis_ready(path, {"key": "Am", "bpm": "128"})

    assert path not in window.queue_bpm_key_pending_fields
    assert path not in window.queue_analysis_pending
    assert window.queue_detail_cache[path]["key"] == "Am"
    assert window.queue_detail_cache[path]["bpm"] == "128"


def test_metadata_completion_with_no_pending_real_request_behaves_as_before(monkeypatch):
    # The overwhelmingly common case -- a plain metadata-only request with
    # no real request ever dispatched for the same path -- must be
    # unaffected: pending_fields was never set, so there is nothing to
    # preserve, and the path's pending flag still clears normally.
    monkeypatch.setattr(
        window_module.QtCore.QTimer, "singleShot", lambda _delay, _callback: None
    )
    window = _completion_window(current_path=None)
    path = "plain.mp4"
    window.queue_analysis_pending.add(path)

    window._on_queue_metadata_ready(path, {"time": "2:10", "bitrate": "256k"})

    assert path not in window.queue_analysis_pending
    assert path not in window.queue_bpm_key_pending_fields
    assert window.queue_detail_cache[path]["time"] == "2:10"
