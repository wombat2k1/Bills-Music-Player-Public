import json
import os
import tempfile
import unittest
from unittest import mock

from billsmusic.loudness import (
    LoudnessCache, calculate_gain, combine_volume, effective_mode,
    parse_gain, parse_peak, read_replaygain,
)


class _Audio:
    def __init__(self, tags):
        self.tags = tags


class LoudnessTests(unittest.TestCase):
    def test_parses_mp3_replaygain_track_tags(self):
        tags = {"TXXX:REPLAYGAIN_TRACK_GAIN": ["-4.20 dB"], "TXXX:REPLAYGAIN_TRACK_PEAK": ["0.91"]}
        with mock.patch("billsmusic.loudness.MutagenFile", return_value=_Audio(tags)):
            result = read_replaygain("song.mp3")
        self.assertEqual(result["track_gain"], -4.2)
        self.assertEqual(result["track_peak"], 0.91)

    def test_parses_flac_track_album_gain_and_peak_case_insensitively(self):
        tags = {
            "replaygain_track_gain": ["+2.0 dB"], "replaygain_track_peak": ["0.8"],
            "replaygain_album_gain": ["-1.5 dB"], "replaygain_album_peak": ["0.95"],
        }
        with mock.patch("billsmusic.loudness.MutagenFile", return_value=_Audio(tags)):
            result = read_replaygain("song.flac")
        self.assertEqual(result, {"track_gain": 2.0, "track_peak": 0.8, "album_gain": -1.5, "album_peak": 0.95})

    def test_rejects_malformed_and_unreasonable_values(self):
        for value in ("loud", "2 dB extra", "nan", "61 dB", None):
            self.assertIsNone(parse_gain(value))
        for value in ("-1", "zero", "17", None):
            self.assertIsNone(parse_peak(value))

    def test_track_mode_calculation(self):
        result = calculate_gain("track", {"track_gain": -4.0, "track_peak": 0.5})
        self.assertAlmostEqual(result.applied_db, -4.0)
        self.assertEqual(result.source, "embedded tag")

    def test_album_mode_and_track_fallback(self):
        album = calculate_gain("album", {"album_gain": -2.0, "album_peak": 0.9, "track_gain": 4.0})
        fallback = calculate_gain("album", {"album_gain": None, "track_gain": -3.0, "track_peak": 0.8})
        self.assertEqual(album.applied_db, -2.0)
        self.assertEqual(fallback.applied_db, -3.0)

    def test_global_and_override_mode_selection(self):
        self.assertEqual(effective_mode(True, "track", "default"), "track")
        self.assertEqual(effective_mode(True, "track", "album"), "album")
        self.assertEqual(effective_mode(True, "album", "off"), "off")
        self.assertEqual(effective_mode(False, "track", "default"), "off")
        self.assertEqual(effective_mode(False, "track", "track"), "track")

    def test_clipping_protection(self):
        result = calculate_gain("track", {"track_gain": 6.0, "track_peak": 0.8})
        self.assertTrue(result.clipping_reduced)
        self.assertAlmostEqual(result.applied_db, -20.0 * __import__("math").log10(0.8))

    def test_user_volume_normalisation_and_fade_are_combined(self):
        self.assertAlmostEqual(combine_volume(0.8, 0.5), 0.4)
        self.assertAlmostEqual(combine_volume(0.8, 0.5, 0.25), 0.1)

    def test_cache_round_trip_override_removal_and_missing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            cache_path = os.path.join(folder, "cache.json")
            audio_path = os.path.join(folder, "track.flac")
            with open(audio_path, "wb") as handle:
                handle.write(b"audio")
            cache = LoudnessCache(cache_path)
            cache.set_override(audio_path, "album")
            cache.store_analysis(audio_path, -17.0, 0.7)
            loaded = LoudnessCache(cache_path)
            self.assertEqual(loaded.override_for(audio_path), "album")
            self.assertEqual(loaded.analysis_for(audio_path)["lufs"], -17.0)
            loaded.set_override(audio_path, "default")
            self.assertEqual(loaded.override_for(audio_path), "default")
            os.unlink(audio_path)
            self.assertIsNone(loaded.analysis_for(audio_path))
            with open(cache_path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["version"], 1)

    def test_invalid_tags_never_raise_or_prevent_fallback(self):
        with mock.patch("billsmusic.loudness.MutagenFile", side_effect=ValueError("bad tags")):
            tags = read_replaygain("bad.mp3")
        result = calculate_gain("track", tags)
        self.assertEqual(result.linear_gain, 1.0)
        self.assertTrue(result.pending_analysis)


if __name__ == "__main__":
    unittest.main()
