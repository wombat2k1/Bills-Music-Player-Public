"""Aggregate karaoke diagnostics: library.karaoke_pair_detected,
library.karaoke_zip_validated, library.karaoke_incomplete (one call per
library apply, not per file) and playback.cdg_parse_warning (one call per
prepared document, not per packet)."""
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets

from billsmusic.performance_diagnostics import PerformanceDiagnostics
from billsmusic.window import PlayerWindow

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _wait_until(predicate, seconds: float = 5.0):
    app = _app()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


def _seed_cache(localappdata_dir, meta):
    folder = os.path.join(localappdata_dir, "Bills Music Player")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "library_cache.json"), "w", encoding="utf-8") as f:
        json.dump({"schema_version": 3, "folders": [], "meta": meta}, f)


def _recorded_calls(monkeypatch):
    calls = []
    original = PerformanceDiagnostics.record

    def _spy(self, category, operation, **kwargs):
        calls.append((category, operation, kwargs))
        return original(self, category, operation, **kwargs)

    monkeypatch.setattr(PerformanceDiagnostics, "record", _spy)
    return calls


def test_library_apply_records_aggregate_karaoke_pair_and_zip_and_incomplete_counts(tmp_path, monkeypatch):
    calls = _recorded_calls(monkeypatch)
    meta = [
        {
            "path": "C:/K/loose1.cdg", "title": "Loose1", "artist": "A", "album": "Karaoke",
            "album_artist": "A", "disc_no": 1, "track_no": 0, "media_type": "karaoke",
            "genre": "Karaoke", "year": "Unknown", "karaoke_source_type": "loose",
            "karaoke_validation_state": "valid", "audio_companion_path": "C:/K/loose1.mp3",
        },
        {
            "path": "C:/K/loose2.cdg", "title": "Loose2", "artist": "A", "album": "Karaoke",
            "album_artist": "A", "disc_no": 1, "track_no": 0, "media_type": "karaoke",
            "genre": "Karaoke", "year": "Unknown", "karaoke_source_type": "loose",
            "karaoke_validation_state": "valid", "audio_companion_path": "C:/K/loose2.mp3",
        },
        {
            "path": "C:/K/show.zip", "title": "Show", "artist": "A", "album": "Karaoke",
            "album_artist": "A", "disc_no": 1, "track_no": 0, "media_type": "karaoke",
            "genre": "Karaoke", "year": "Unknown", "karaoke_source_type": "zip",
            "karaoke_validation_state": "valid", "audio_companion_path": None,
        },
        {
            "path": "C:/K/broken.cdg", "title": "broken", "artist": "Unknown Artist",
            "album": "Karaoke", "album_artist": "Unknown Artist", "disc_no": 1, "track_no": 0,
            "media_type": "karaoke", "genre": "Karaoke", "year": "Unknown",
            "karaoke_source_type": "loose", "karaoke_validation_state": "incomplete",
        },
    ]
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _seed_cache(str(tmp_path), meta)
    _app()
    window = PlayerWindow()
    try:
        _wait_until(lambda: any(op == "media_classified" for _, op, _ in calls))

        pair_calls = [kw for cat, op, kw in calls if op == "karaoke_pair_detected"]
        zip_calls = [kw for cat, op, kw in calls if op == "karaoke_zip_validated"]
        incomplete_calls = [kw for cat, op, kw in calls if op == "karaoke_incomplete"]

        assert pair_calls and pair_calls[0]["details"]["count"] == 2
        assert zip_calls and zip_calls[0]["details"]["count"] == 1
        assert incomplete_calls and incomplete_calls[0]["details"]["count"] == 1
    finally:
        window.close()


def test_cdg_parse_warning_fires_once_per_prepared_document_not_per_packet(tmp_path, monkeypatch):
    calls = _recorded_calls(monkeypatch)
    document = SimpleNamespace(unrecognized_packet_count=3, packets=[b"x"] * 100)
    diagnostics = PerformanceDiagnostics(
        start_writer=False, directory=str(tmp_path / "diagnostics"),
    )
    window = SimpleNamespace(
        _karaoke_generation=1,
        current_path="song.cdg",
        _karaoke_audio_path=None,
        _karaoke_document=None,
        karaoke_widget=MagicMock(),
        party_mode=None,
        diagnostics=diagnostics,
        track_index_by_path={},
        waveform_seekbar=None,
        _current_playback_attempt=SimpleNamespace(attempt_id=1, is_terminal=lambda: False),
    )
    window._play_path_direct = lambda *a, **kw: True
    window._is_current_playback_attempt = (
        lambda attempt_id: PlayerWindow._is_current_playback_attempt(window, attempt_id)
    )
    window._require_current_playback_attempt = (
        lambda attempt_id, stage: PlayerWindow._require_current_playback_attempt(window, attempt_id, stage)
    )
    pair = SimpleNamespace(audio_path="song.mp3")

    PlayerWindow._on_karaoke_prepared(window, 1, "song.cdg", pair, document, attempt_id=1)

    warnings = [kw for cat, op, kw in calls if op == "cdg_parse_warning"]
    assert len(warnings) == 1
    assert warnings[0]["details"]["unrecognized_packets"] == 3
    assert warnings[0]["details"]["total_packets"] == 100
