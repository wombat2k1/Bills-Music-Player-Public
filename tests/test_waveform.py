import os
import tempfile
import threading
import unittest
from unittest import mock

import numpy as np
import soundfile as sf

import billsmusic.waveform as waveform_module
from billsmusic.waveform import (
    WaveformData, cache_key, decode_peaks, load_cached_waveform, save_waveform,
)
from billsmusic.waveform_worker import WaveformWorker


def _write_tone(path: str, seconds: float = 1.0, samplerate: int = 8000, freq: float = 440.0):
    n = int(seconds * samplerate)
    t = np.linspace(0, seconds, n, endpoint=False)
    data = (0.5 * np.sin(2 * np.pi * freq * t)).astype("float32")
    sf.write(path, data, samplerate)


class DecodePeaksTests(unittest.TestCase):
    def test_decode_produces_target_point_count(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "tone.wav")
            _write_tone(path, seconds=2.0)
            result = decode_peaks(path, target_points=200)
        self.assertIsNotNone(result)
        self.assertEqual(len(result.mins), 200)
        self.assertEqual(len(result.maxs), 200)
        self.assertTrue(all(-1.0 <= v <= 1.0 for v in result.mins))
        self.assertTrue(all(-1.0 <= v <= 1.0 for v in result.maxs))
        self.assertGreater(result.duration_s, 0.0)

    def test_decode_reads_bounded_blocks_not_the_whole_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "tone.wav")
            _write_tone(path, seconds=2.0, samplerate=8000)
            original_block = waveform_module.DECODE_BLOCK_FRAMES
            waveform_module.DECODE_BLOCK_FRAMES = 500
            calls = []
            real_read = waveform_module.sf.SoundFile.read

            def counting_read(self_handle, *args, **kwargs):
                frames = args[0] if args else kwargs.get("frames")
                calls.append(frames)
                return real_read(self_handle, *args, **kwargs)

            try:
                with mock.patch.object(waveform_module.sf.SoundFile, "read", counting_read):
                    result = decode_peaks(path, target_points=50)
            finally:
                waveform_module.DECODE_BLOCK_FRAMES = original_block
        self.assertIsNotNone(result)
        # More than one block was read (incremental consumption)...
        self.assertGreater(len(calls), 1)
        # ...and no single call asked for more than the bounded block size.
        self.assertTrue(all(frames is None or frames <= 500 for frames in calls))

    def test_corrupt_file_returns_none_without_raising(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "garbage.wav")
            with open(path, "wb") as handle:
                handle.write(os.urandom(64))
            result = decode_peaks(path)
        self.assertIsNone(result)

    def test_missing_file_returns_none(self):
        self.assertIsNone(decode_peaks("Z:/definitely/does/not/exist.mp3"))

    def test_cancellation_returns_none_promptly(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "tone.wav")
            _write_tone(path, seconds=1.0)
            result = decode_peaks(path, should_cancel=lambda: True)
        self.assertIsNone(result)

    def test_audioread_backend_matches_soundfile_shape(self):
        """Backend independence: the fallback decode path produces the same
        shaped output as the soundfile path, and this module never touches
        the live playback backends (VLC/miniaudio/BASS) at all."""
        samplerate = 8000
        channels = 1
        duration_s = 1.0
        n = int(samplerate * duration_s)
        t = np.linspace(0, duration_s, n, endpoint=False)
        samples = (0.5 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2")
        block_size = 400
        blocks = [samples[i:i + block_size].tobytes() for i in range(0, n, block_size)]

        class _FakeAudioFile:
            def __init__(self):
                self.channels = channels
                self.samplerate = samplerate
                self.duration = duration_s

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __iter__(self):
                return iter(blocks)

        with mock.patch.object(waveform_module.audioread, "audio_open", return_value=_FakeAudioFile()):
            result = waveform_module._decode_peaks_audioread(
                "fake.m4a", target_points=50, should_cancel=lambda: False,
            )
        self.assertIsNotNone(result)
        self.assertEqual(len(result.mins), 50)
        self.assertEqual(len(result.maxs), 50)


class WaveformCacheTests(unittest.TestCase):
    def test_cache_key_is_stable_for_same_fingerprint(self):
        first = cache_key("C:/music/song.mp3", 1234, 5678)
        second = cache_key("C:/music/song.mp3", 1234, 5678)
        self.assertEqual(first, second)
        self.assertNotEqual(first, cache_key("C:/music/song.mp3", 1234, 5679))

    def test_cache_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            cache_dir = os.path.join(folder, "waveforms")
            audio_path = os.path.join(folder, "tone.wav")
            _write_tone(audio_path, seconds=1.0)
            data = decode_peaks(audio_path, target_points=100)
            self.assertIsNotNone(data)
            with mock.patch("billsmusic.config.waveform_cache_dir", return_value=cache_dir):
                self.assertTrue(save_waveform(audio_path, data))
                loaded = load_cached_waveform(audio_path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.mins, data.mins)
        self.assertEqual(loaded.maxs, data.maxs)
        self.assertAlmostEqual(loaded.duration_s, data.duration_s)

    def test_cache_invalidated_on_size_change(self):
        with tempfile.TemporaryDirectory() as folder:
            cache_dir = os.path.join(folder, "waveforms")
            audio_path = os.path.join(folder, "tone.wav")
            _write_tone(audio_path, seconds=1.0)
            data = decode_peaks(audio_path, target_points=100)
            with mock.patch("billsmusic.config.waveform_cache_dir", return_value=cache_dir):
                save_waveform(audio_path, data)
                with open(audio_path, "ab") as handle:
                    handle.write(b"\x00" * 1000)
                self.assertIsNone(load_cached_waveform(audio_path))

    def test_cache_invalidated_on_mtime_change(self):
        with tempfile.TemporaryDirectory() as folder:
            cache_dir = os.path.join(folder, "waveforms")
            audio_path = os.path.join(folder, "tone.wav")
            _write_tone(audio_path, seconds=1.0)
            data = decode_peaks(audio_path, target_points=100)
            with mock.patch("billsmusic.config.waveform_cache_dir", return_value=cache_dir):
                save_waveform(audio_path, data)
                stat = os.stat(audio_path)
                new_ns = stat.st_mtime_ns + 5_000_000_000
                os.utime(audio_path, ns=(new_ns, new_ns))
                self.assertIsNone(load_cached_waveform(audio_path))

    def test_cache_miss_on_untouched_file_with_no_prior_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            cache_dir = os.path.join(folder, "waveforms")
            audio_path = os.path.join(folder, "tone.wav")
            _write_tone(audio_path, seconds=1.0)
            with mock.patch("billsmusic.config.waveform_cache_dir", return_value=cache_dir):
                self.assertIsNone(load_cached_waveform(audio_path))


class WaveformWorkerTests(unittest.TestCase):
    def test_request_is_latest_wins(self):
        worker = WaveformWorker()
        worker.request("a.mp3")
        worker.request("b.mp3")
        self.assertEqual(worker._pending_path, "b.mp3")

    def test_is_stale_detects_superseded_path(self):
        worker = WaveformWorker()
        worker.request("a.mp3")
        self.assertFalse(worker._is_stale("a.mp3"))
        worker.request("b.mp3")
        self.assertTrue(worker._is_stale("a.mp3"))
        self.assertFalse(worker._is_stale("b.mp3"))

    def test_decode_never_runs_on_gui_thread(self):
        main_thread_id = threading.main_thread().ident
        captured = {}
        done = threading.Event()

        def fake_decode(path, *, should_cancel=lambda: False):
            captured["thread_id"] = threading.get_ident()
            done.set()
            return None

        worker = WaveformWorker()
        with mock.patch("billsmusic.waveform_worker.decode_peaks", side_effect=fake_decode):
            worker.start()
            try:
                worker.request("Z:/no/such/track.mp3")
                self.assertTrue(done.wait(timeout=5.0), "decode was never invoked")
            finally:
                worker.stop()
                worker.wait(3000)
        self.assertIn("thread_id", captured)
        self.assertNotEqual(captured["thread_id"], main_thread_id)


if __name__ == "__main__":
    unittest.main()
