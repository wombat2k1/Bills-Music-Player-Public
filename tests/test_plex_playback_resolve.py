"""Stage 3A: PlexPlaybackResolveWorker -- resolving one plex:// identity
to a Direct Play transport source (or a clear, safe failure) off the GUI
thread. No real Plex server or token -- requests.get is monkeypatched
with a deterministic fake transport, matching this suite's established
convention (see test_plex_client.py).
"""
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _metadata_response(rating_key="42", container="mp3", audio_codec="mp3",
                        video_codec="", part_key="/library/parts/42/1/file.mp3"):
    return {
        "MediaContainer": {
            "Metadata": [{
                "ratingKey": rating_key, "title": "Track",
                "Media": [{
                    "container": container, "audioCodec": audio_codec,
                    "videoCodec": video_codec, "bitrate": 320,
                    "Part": [{"key": part_key}],
                }],
            }],
        },
    }


def _run_resolve_worker(monkeypatch, response_json, media_kind="music",
                         status_code=200, raise_exc=None, generation=1):
    from billsmusic import analysis_warmup
    from billsmusic.workers import PlexPlaybackResolveWorker

    app = _app()
    monkeypatch.setattr(analysis_warmup, "wait_until_ready", lambda: None)

    def fake_get(url, headers=None, timeout=None):
        if raise_exc is not None:
            raise raise_exc
        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = response_json
        return response
    monkeypatch.setattr("billsmusic.plex_client.requests.get", fake_get)

    diagnostics_events = []
    fake_diagnostics = SimpleNamespace(
        record=lambda category, op, **kw: diagnostics_events.append((category, op, kw)),
        path_details=lambda value: {"path_hash": f"hash-of-{value}"} if value else {},
    )
    monkeypatch.setattr("billsmusic.workers.get_diagnostics", lambda: fake_diagnostics)

    worker = PlexPlaybackResolveWorker(
        identity="plex://server-1/42.mp3", server_config_id="server-1",
        rating_key="42", media_kind=media_kind,
        server_address="http://host:32400", token="REAL-SECRET-TOKEN",
        client_identifier="client-1", generation=generation,
    )
    results = []
    worker.finished_result.connect(results.append)
    worker.start()
    deadline = time.monotonic() + 5.0
    while not results and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    worker.wait(2000)
    return results, diagnostics_events


def test_audio_resolves_with_token_as_header_never_in_url(monkeypatch):
    results, events = _run_resolve_worker(monkeypatch, _metadata_response(), media_kind="music")

    assert results and results[0]["success"] is True
    source = results[0]["transport_source"]
    assert source.identity == "plex://server-1/42.mp3"
    assert "REAL-SECRET-TOKEN" not in source.transport_url
    assert source.extra_headers == {"X-Plex-Token": "REAL-SECRET-TOKEN"}
    assert source.transport_url == "http://host:32400/library/parts/42/1/file.mp3"
    assert source.container == "mp3"


def test_video_resolves_with_token_in_query_string_decision1_exception(monkeypatch):
    response = _metadata_response(
        container="mp4", audio_codec="aac", video_codec="h264",
        part_key="/library/parts/42/1/file.mp4",
    )
    results, events = _run_resolve_worker(monkeypatch, response, media_kind="video")

    assert results and results[0]["success"] is True
    source = results[0]["transport_source"]
    assert source.extra_headers is None
    assert "X-Plex-Token=REAL-SECRET-TOKEN" in source.transport_url
    assert source.transport_url == (
        "http://host:32400/library/parts/42/1/file.mp4?X-Plex-Token=REAL-SECRET-TOKEN"
    )


def test_unsupported_audio_container_fails_direct_play_not_transcode(monkeypatch):
    response = _metadata_response(container="wma", audio_codec="wmav2")
    results, events = _run_resolve_worker(monkeypatch, response, media_kind="music")

    assert results and results[0]["success"] is False
    assert results[0]["direct_play_unavailable"] is True
    assert "wma" in results[0]["reason"]
    assert results[0]["container"] == "wma"
    assert results[0]["audio_codec"] == "wmav2"
    # No transcode session request of any kind -- the reason string is
    # purely local capability comparison, never a Plex transcode API call.
    assert "transcode" not in results[0]["reason"].lower()


def test_supported_video_container_from_registry_resolves(monkeypatch):
    # mkv/webm aren't in the small HTTP-proven audio set but ARE in the
    # full Local video extension registry (same FFmpeg decode pipeline
    # regardless of transport) -- proves the video whitelist is reused,
    # not duplicated/narrower.
    response = _metadata_response(
        container="mkv", audio_codec="aac", video_codec="h264",
        part_key="/library/parts/42/1/file.mkv",
    )
    results, events = _run_resolve_worker(monkeypatch, response, media_kind="video")
    assert results and results[0]["success"] is True


def test_connection_failure_reports_safe_reason(monkeypatch):
    import requests

    results, events = _run_resolve_worker(
        monkeypatch, {}, raise_exc=requests.exceptions.Timeout("boom"),
    )

    assert results and results[0]["success"] is False
    assert results[0]["reason"] == "Connection timed out"
    assert "direct_play_unavailable" not in results[0]


def test_diagnostics_never_contain_the_real_token(monkeypatch):
    response = _metadata_response(
        container="mp4", audio_codec="aac", video_codec="h264",
        part_key="/library/parts/42/1/file.mp4",
    )
    results, events = _run_resolve_worker(monkeypatch, response, media_kind="video")

    assert results[0]["success"] is True
    for category, op, kwargs in events:
        assert "REAL-SECRET-TOKEN" not in repr(kwargs)
        assert "REAL-SECRET-TOKEN" not in repr(kwargs.get("details", {}))


def test_started_diagnostic_never_contains_raw_identity_only_hash(monkeypatch):
    results, events = _run_resolve_worker(monkeypatch, _metadata_response())
    started_events = [d for c, op, d in events if op == "plex_playback_resolve_started"]
    assert started_events
    assert started_events[0]["details"]["identity_hash"] == "hash-of-plex://server-1/42.mp3"
    assert "identity" not in started_events[0]["details"]
