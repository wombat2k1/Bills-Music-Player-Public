from unittest.mock import MagicMock

from billsmusic.loudness_worker import LoudnessAnalysisWorker
from billsmusic.workers import QueueAnalysisWorker


def test_queue_worker_uses_registry_to_route_bpm_and_metadata_requests():
    # Video's audio track is genuinely decodable for BPM/Key (via
    # QAudioDecoder -- see workers.py's _decode_video_audio_preview), so
    # .mp4 now routes to real analysis alongside .flac; only a genuinely
    # unsupported extension falls back to metadata-only.
    worker = QueueAnalysisWorker()
    worker._enqueue = MagicMock()
    worker.request_metadata = MagicMock()

    worker.request("song.FLAC")
    worker.request("clip.MP4")
    worker.request("unknown.bin")

    assert worker._enqueue.call_args_list == [
        (("song.FLAC",), {"metadata_only": False, "fields": frozenset({"bpm", "key"})}),
        (("clip.MP4",), {"metadata_only": False, "fields": frozenset({"bpm", "key"})}),
    ]
    worker.request_metadata.assert_called_once_with("unknown.bin")


def test_loudness_worker_rejects_registry_ineligible_media():
    worker = LoudnessAnalysisWorker()

    worker.request("song.mp3")
    worker.request("clip.mp4")
    worker.request("unknown.bin")

    assert worker._queue == ["song.mp3"]
    assert worker._queued == {"song.mp3"}
