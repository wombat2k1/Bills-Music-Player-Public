"""Plex Stage 1: thin HTTP client, preferences dataclass, and the async
Test Connection worker. No test requires a real Plex server or token --
requests.get is monkeypatched with a deterministic fake transport.
"""
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtWidgets

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP

from billsmusic.plex_preferences import (
    PlexPreferences,
    generate_client_identifier,
    generate_server_config_id,
    normalise_server_address,
)
from billsmusic.plex_client import (
    PlexClient,
    PlexConnectionError,
    PlexLibrarySection,
)


# -- normalise_server_address ------------------------------------------------

def test_normalise_server_address_adds_scheme_when_missing():
    assert normalise_server_address("192.168.1.50:32400") == "http://192.168.1.50:32400"


def test_normalise_server_address_keeps_existing_scheme():
    assert normalise_server_address("https://plex.example.com:32400") == "https://plex.example.com:32400"


def test_normalise_server_address_strips_trailing_slash_and_whitespace():
    assert normalise_server_address("  http://host:32400/  ") == "http://host:32400"


def test_normalise_server_address_empty_stays_empty():
    assert normalise_server_address("") == ""
    assert normalise_server_address("   ") == ""


# -- identifiers --------------------------------------------------------------

def test_generate_client_identifier_is_unique_each_call():
    a = generate_client_identifier()
    b = generate_client_identifier()
    assert a != b
    assert len(a) == 36  # UUID4 string form


def test_generate_server_config_id_is_unique_each_call():
    a = generate_server_config_id()
    b = generate_server_config_id()
    assert a != b


# -- PlexPreferences round-trip -----------------------------------------------

def test_plex_preferences_from_config_defaults_when_empty():
    prefs = PlexPreferences.from_config({})
    assert prefs.enabled is False
    assert prefs.server_address == ""
    assert prefs.token == ""


def test_plex_preferences_round_trips_through_config_dict():
    original = PlexPreferences(
        enabled=True, server_address="http://192.168.1.50:32400",
        token="secret-token-value", client_identifier="client-id-123",
        server_config_id="server-cfg-456", server_name="My Plex",
        server_version="1.40.0", music_library_id="1", music_library_name="Music",
        video_library_id="2", video_library_name="Movies",
        karaoke_library_id="", karaoke_library_name="",
    )
    cfg = {}
    cfg.update(original.to_config_updates())
    restored = PlexPreferences.from_config(cfg)
    assert restored == original


def test_plex_preferences_to_config_updates_only_touches_plex_keys():
    prefs = PlexPreferences(enabled=True, server_address="http://host:32400")
    updates = prefs.to_config_updates()
    assert all(key.startswith("plex_") for key in updates)


def test_plex_preferences_non_string_config_values_do_not_raise():
    # A corrupted/hand-edited config.json with wrong types must not crash
    # startup -- matches the defensive style used elsewhere in this file
    # (e.g. cfg.get(...) with type coercion in window.py's own loader).
    prefs = PlexPreferences.from_config({
        "plex_server_address": 12345, "plex_token": None, "plex_enabled": "yes",
    })
    assert prefs.server_address == ""
    assert prefs.token == ""
    assert prefs.enabled is True  # bool("yes") is True, matches bool(cfg.get(...)) elsewhere


# -- PlexClient headers ---------------------------------------------------

def test_plex_client_headers_include_required_fields_and_no_query_token():
    client = PlexClient("http://host:32400", "my-token", "client-abc")
    headers = client._headers()
    assert headers["Accept"] == "application/json"
    assert headers["X-Plex-Client-Identifier"] == "client-abc"
    assert headers["X-Plex-Product"] == "Bills Music Player"
    assert headers["X-Plex-Version"]
    assert headers["X-Plex-Platform"] == "Windows"
    assert headers["X-Plex-Token"] == "my-token"


def test_plex_client_omits_token_header_when_no_token_configured():
    client = PlexClient("http://host:32400", "", "client-abc")
    headers = client._headers()
    assert "X-Plex-Token" not in headers


def test_plex_client_generates_client_identifier_when_none_given():
    client = PlexClient("http://host:32400", "token", "")
    assert client._headers()["X-Plex-Client-Identifier"]


def test_plex_client_never_puts_token_in_the_request_url(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "MediaContainer": {
                "friendlyName": "Test Server", "version": "1.40.0",
                "machineIdentifier": "abc123",
            }
        }
        return response

    monkeypatch.setattr("billsmusic.plex_client.requests.get", fake_get)
    client = PlexClient("http://host:32400", "super-secret-token", "client-id")
    client.fetch_library_sections()  # separate call, checked below too
    assert "super-secret-token" not in captured["url"]
    assert captured["headers"]["X-Plex-Token"] == "super-secret-token"


# -- PlexClient error mapping --------------------------------------------

def _client_with_fake_get(monkeypatch, fake_get):
    monkeypatch.setattr("billsmusic.plex_client.requests.get", fake_get)
    return PlexClient("http://host:32400", "token", "client-id")


def test_plex_client_maps_401_to_authentication_failed(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        response = MagicMock()
        response.status_code = 401
        return response
    client = _client_with_fake_get(monkeypatch, fake_get)
    with pytest.raises(PlexConnectionError) as exc:
        client.fetch_server_info()
    assert exc.value.reason == "Authentication failed"


def test_plex_client_maps_timeout_exception(monkeypatch):
    import requests

    def fake_get(url, headers=None, timeout=None):
        raise requests.exceptions.Timeout("timed out")
    client = _client_with_fake_get(monkeypatch, fake_get)
    with pytest.raises(PlexConnectionError) as exc:
        client.fetch_server_info()
    assert exc.value.reason == "Connection timed out"


def test_plex_client_maps_connection_error_to_server_unreachable(monkeypatch):
    import requests

    def fake_get(url, headers=None, timeout=None):
        raise requests.exceptions.ConnectionError("refused")
    client = _client_with_fake_get(monkeypatch, fake_get)
    with pytest.raises(PlexConnectionError) as exc:
        client.fetch_server_info()
    assert exc.value.reason == "Server unreachable"


def test_plex_client_maps_bad_json_to_invalid_response(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        response = MagicMock()
        response.status_code = 200
        response.json.side_effect = ValueError("not json")
        return response
    client = _client_with_fake_get(monkeypatch, fake_get)
    with pytest.raises(PlexConnectionError) as exc:
        client.fetch_server_info()
    assert exc.value.reason == "Invalid Plex response"


def test_plex_client_maps_missing_media_container_to_invalid_response(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"unexpected": "shape"}
        return response
    client = _client_with_fake_get(monkeypatch, fake_get)
    with pytest.raises(PlexConnectionError) as exc:
        client.fetch_server_info()
    assert exc.value.reason == "Invalid Plex response"


def test_plex_client_empty_server_address_is_server_unreachable():
    client = PlexClient("", "token", "client-id")
    with pytest.raises(PlexConnectionError) as exc:
        client.fetch_server_info()
    assert exc.value.reason == "Server unreachable"


def test_plex_client_exception_detail_never_contains_the_token(monkeypatch):
    import requests

    def fake_get(url, headers=None, timeout=None):
        # A real requests exception can carry the full URL/request in its
        # message -- confirm our mapping never surfaces that raw message.
        raise requests.exceptions.ConnectionError(
            "Failed to establish a new connection: super-secret-token-xyz"
        )
    client = _client_with_fake_get(monkeypatch, fake_get)
    with pytest.raises(PlexConnectionError) as exc:
        client.fetch_server_info()
    assert "super-secret-token-xyz" not in exc.value.detail
    assert "super-secret-token-xyz" not in str(exc.value)


# -- PlexClient success path ------------------------------------------------

def test_plex_client_fetch_server_info_success(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        response = MagicMock()
        response.status_code = 200
        if url.endswith("/library/sections"):
            response.json.return_value = {
                "MediaContainer": {
                    "Directory": [
                        {"key": "1", "title": "Music", "type": "artist"},
                        {"key": "2", "title": "Movies", "type": "movie"},
                        {"key": "3", "title": "TV Shows", "type": "show"},
                    ]
                }
            }
        else:
            response.json.return_value = {
                "MediaContainer": {
                    "friendlyName": "My Plex Server", "version": "1.40.0.1234",
                    "machineIdentifier": "abcdef0123456789",
                }
            }
        return response

    monkeypatch.setattr("billsmusic.plex_client.requests.get", fake_get)
    client = PlexClient("192.168.1.50:32400", "token", "client-id")
    info = client.fetch_server_info()

    assert info.friendly_name == "My Plex Server"
    assert info.version == "1.40.0.1234"
    assert info.machine_identifier == "abcdef0123456789"
    assert len(info.libraries) == 3
    assert info.libraries[0] == PlexLibrarySection(key="1", title="Music", type="artist")
    assert len(calls) == 2  # / then /library/sections


def test_plex_client_library_sections_skip_malformed_entries(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "MediaContainer": {
                "Directory": [
                    {"key": "1", "title": "Music", "type": "artist"},
                    {"title": "Missing key"},
                    {"key": "3"},  # missing title
                    "not-even-a-dict",
                ]
            }
        }
        return response
    monkeypatch.setattr("billsmusic.plex_client.requests.get", fake_get)
    client = PlexClient("http://host:32400", "token", "client-id")
    sections = client.fetch_library_sections()
    assert len(sections) == 1
    assert sections[0].key == "1"


# -- PlexConnectionTestWorker -------------------------------------------------

def test_connection_test_worker_success_emits_result_and_diagnostics(monkeypatch):
    app = _app()
    from billsmusic.workers import PlexConnectionTestWorker

    def fake_get(url, headers=None, timeout=None):
        response = MagicMock()
        response.status_code = 200
        if url.endswith("/library/sections"):
            response.json.return_value = {
                "MediaContainer": {"Directory": [
                    {"key": "1", "title": "Music", "type": "artist"},
                ]}
            }
        else:
            response.json.return_value = {
                "MediaContainer": {
                    "friendlyName": "Test Server", "version": "1.0",
                    "machineIdentifier": "xyz",
                }
            }
        return response
    monkeypatch.setattr("billsmusic.plex_client.requests.get", fake_get)

    diagnostics_events = []
    fake_diagnostics = SimpleNamespace(
        record=lambda category, op, **kw: diagnostics_events.append((category, op, kw))
    )
    monkeypatch.setattr("billsmusic.workers.get_diagnostics", lambda: fake_diagnostics)

    worker = PlexConnectionTestWorker("http://host:32400", "super-secret-token", "client-id")
    results = []
    worker.finished_result.connect(results.append)
    worker.start()
    deadline = time.monotonic() + 5.0
    while not results and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    worker.wait(2000)

    assert results and results[0]["success"] is True
    assert results[0]["friendly_name"] == "Test Server"
    assert len(results[0]["libraries"]) == 1

    # No diagnostics event anywhere contains the raw token.
    for category, op, kw in diagnostics_events:
        assert "super-secret-token" not in repr(kw)
        assert "super-secret-token" not in repr(kw.get("details", {}))


def test_connection_test_worker_failure_never_logs_token(monkeypatch):
    app = _app()
    from billsmusic.workers import PlexConnectionTestWorker
    import requests

    def fake_get(url, headers=None, timeout=None):
        raise requests.exceptions.ConnectionError("refused: token=super-secret-token")
    monkeypatch.setattr("billsmusic.plex_client.requests.get", fake_get)

    diagnostics_events = []
    fake_diagnostics = SimpleNamespace(
        record=lambda category, op, **kw: diagnostics_events.append((category, op, kw))
    )
    monkeypatch.setattr("billsmusic.workers.get_diagnostics", lambda: fake_diagnostics)

    worker = PlexConnectionTestWorker("http://host:32400", "super-secret-token", "client-id")
    results = []
    worker.finished_result.connect(results.append)
    worker.start()
    deadline = time.monotonic() + 5.0
    while not results and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    worker.wait(2000)

    assert results and results[0]["success"] is False
    assert results[0]["reason"] == "Server unreachable"
    for category, op, kw in diagnostics_events:
        assert "super-secret-token" not in repr(kw)


# -- PlexLibraryFetchWorker ---------------------------------------------------
# Stage 2 real-device follow-up: the user's real Plex server showed a blank
# Music panel with no indication of why (still loading? truly empty? wrong
# endpoint? conversion rejecting everything?). These tests drive the worker
# with a monkeypatched requests.get (one bulk call, matching what a real
# GET /library/sections/<key>/all?type=N returns) and assert the new
# diagnostics/hardening added to answer exactly that question next time.

def _music_track_item(rating_key="1", container="mp3", item_type=10, **overrides):
    item = {
        "ratingKey": rating_key, "type": item_type, "title": f"Track {rating_key}",
        "grandparentTitle": "ABBA", "parentTitle": "Arrival", "parentYear": 1976,
        "index": 1, "duration": 210000, "updatedAt": 100,
        "Media": [{"container": container, "audioCodec": container, "bitrate": 320,
                   "Part": [{"key": f"/library/parts/{rating_key}/file.{container}"}]}],
    }
    item.update(overrides)
    return item


def _run_plex_library_fetch_worker(monkeypatch, raw_items, media_kind="music", item_type=10):
    from billsmusic import analysis_warmup
    from billsmusic.workers import PlexLibraryFetchWorker

    app = _app()
    monkeypatch.setattr(analysis_warmup, "wait_until_ready", lambda: None)
    fetch_calls = []

    def fake_get(url, headers=None, timeout=None):
        fetch_calls.append(url)
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "MediaContainer": {
                "size": len(raw_items), "totalSize": len(raw_items),
                "Metadata": raw_items,
            }
        }
        return response
    monkeypatch.setattr("billsmusic.plex_client.requests.get", fake_get)

    diagnostics_events = []
    fake_diagnostics = SimpleNamespace(
        record=lambda category, op, **kw: diagnostics_events.append((category, op, kw)),
        path_details=lambda value: {"path_hash": f"hash-of-{value}"} if value else {},
    )
    monkeypatch.setattr("billsmusic.workers.get_diagnostics", lambda: fake_diagnostics)

    worker = PlexLibraryFetchWorker(
        "http://host:32400", "secret-token", "client-id", "1", item_type,
        media_kind, "server-cfg-id", 1,
    )
    results = []
    progress_events = []
    worker.finished_result.connect(results.append)
    worker.progress.connect(lambda kind, message: progress_events.append((kind, message)))
    worker.start()
    deadline = time.monotonic() + 5.0
    while not results and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    worker.wait(2000)
    return results, diagnostics_events, progress_events, fetch_calls


def test_fetch_worker_emits_the_full_diagnostic_sequence(monkeypatch):
    raw_items = [_music_track_item(rating_key=str(i)) for i in range(1, 4)]
    results, events, progress_events, fetch_calls = _run_plex_library_fetch_worker(
        monkeypatch, raw_items,
    )

    assert results and results[0]["success"] is True
    assert len(results[0]["meta_list"]) == 3
    # Exactly one bulk HTTP call -- never one request per track (item 2).
    assert len(fetch_calls) == 1

    ops = [op for _, op, _ in events]
    assert ops == [
        "plex_library_fetch_started", "plex_library_fetch_response",
        "plex_library_conversion_completed", "plex_library_refresh_completed",
    ]

    started_details = events[0][2]["details"]
    assert started_details["category"] == "music"
    assert started_details["section_key"] == "1"
    assert started_details["server_hash"] == "hash-of-server-cfg-id"

    response_details = events[1][2]["details"]
    assert response_details["http_status"] == 200
    assert response_details["container_total_size"] == 3
    assert response_details["raw_item_count"] == 3
    assert response_details["page_count"] == 1  # 3 items fit in one page
    assert response_details["page_size"] == 500
    assert response_details["item_types_encountered"] == {10: 3}

    conversion_details = events[2][2]["details"]
    assert conversion_details["input_count"] == 3
    assert conversion_details["output_count"] == 3
    assert conversion_details["tracks"] == 3
    assert conversion_details["artists"] == 1
    assert conversion_details["albums"] == 1
    assert conversion_details["rejected_count"] == 0
    assert conversion_details["rejection_reasons"] == {}

    assert progress_events == [
        ("music", "Retrieving Music library…"),
        ("music", "Retrieving Music library… 3 / 3"),
        ("music", "Processing 3 music…"),
        ("music", "Building library view…"),
    ]


def test_fetch_worker_reports_wrong_item_type_in_diagnostics(monkeypatch):
    # If the wrong Plex type were ever queried for a section (item 2's
    # exact concern -- e.g. an "artist" library returning type=8 rows
    # instead of type=10 tracks), this is the field that reveals it
    # instantly rather than a silent empty/wrong result.
    raw_items = [
        _music_track_item(rating_key="1", item_type=10),
        {"ratingKey": "2", "type": 8, "title": "ABBA"},  # an artist row, not a track
    ]
    results, events, _, _ = _run_plex_library_fetch_worker(monkeypatch, raw_items)

    response_details = next(d for _, op, d in events if op == "plex_library_fetch_response")["details"]
    assert response_details["item_types_encountered"] == {10: 1, 8: 1}


def test_fetch_worker_rejects_one_bad_item_without_losing_the_batch(monkeypatch):
    # Reproduces the exact hazard traced in plex_track_to_meta_dict:
    # make_plex_identity raises ValueError for a falsy server_config_id/
    # rating_key. Before this round's hardening, ANY conversion exception
    # for ANY single item propagated out of the (unwrapped) loop, killing
    # the whole QThread.run() silently -- finished_result never fired,
    # and the tab was left indistinguishable from "still loading" forever.
    import billsmusic.plex_metadata as plex_metadata_module

    raw_items = [
        _music_track_item(rating_key="1"),
        _music_track_item(rating_key="2"),
        _music_track_item(rating_key="3"),
    ]
    real_convert = plex_metadata_module.plex_track_to_meta_dict

    def flaky_convert(item, server_config_id):
        if item.get("ratingKey") == "2":
            raise ValueError("simulated malformed item")
        return real_convert(item, server_config_id)

    monkeypatch.setattr(plex_metadata_module, "plex_track_to_meta_dict", flaky_convert)
    # workers.py imports plex_track_to_meta_dict inside run() via `from
    # .plex_metadata import ...`, which re-binds the name fresh from the
    # module each call -- patching the module attribute above is enough.

    results, events, _, _ = _run_plex_library_fetch_worker(monkeypatch, raw_items)

    assert results and results[0]["success"] is True
    assert len(results[0]["meta_list"]) == 2  # items 1 and 3 survived
    conversion_details = next(
        d for _, op, d in events if op == "plex_library_conversion_completed"
    )["details"]
    assert conversion_details["input_count"] == 3
    assert conversion_details["output_count"] == 2
    assert conversion_details["rejected_count"] == 1
    assert conversion_details["rejection_reasons"] == {"exception:ValueError": 1}


def test_fetch_worker_video_progress_message_uses_video_kind(monkeypatch):
    raw_items = [{"ratingKey": "1", "type": 1, "title": "Some Video"}]
    _, _, progress_events, _ = _run_plex_library_fetch_worker(
        monkeypatch, raw_items, media_kind="video", item_type=1,
    )
    assert progress_events[0] == ("video", "Retrieving Video library…")


# -- Pagination (real-device follow-up items 4/5/6) --------------------------
# The real Music fetch never completed a single 8s request for a large
# library; Video (1984 items) worked fine unpaginated. Rather than raise the
# timeout, each request is now bounded to PAGE_SIZE items regardless of
# library size -- these tests use a mock that genuinely respects
# X-Plex-Container-Start/-Size (slicing a real underlying dataset), unlike
# _run_plex_library_fetch_worker's simpler single-page mock above.

def _paginated_music_dataset(count):
    return [_music_track_item(rating_key=str(i)) for i in range(count)]


def _run_paginated_worker(
    monkeypatch, dataset, media_kind="music", item_type=10,
    fail_on_page=None, cancel_after_page=None, page_delay=0.0,
):
    import urllib.parse
    from billsmusic import analysis_warmup
    from billsmusic.workers import PlexLibraryFetchWorker
    import requests

    app = _app()
    monkeypatch.setattr(analysis_warmup, "wait_until_ready", lambda: None)
    fetch_calls = []

    def fake_get(url, headers=None, timeout=None):
        if page_delay:
            time.sleep(page_delay)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        start = int(query["X-Plex-Container-Start"][0])
        size = int(query["X-Plex-Container-Size"][0])
        page_number = len(fetch_calls) + 1
        fetch_calls.append(url)
        if fail_on_page and page_number == fail_on_page:
            raise requests.exceptions.Timeout("simulated page timeout")
        page = dataset[start:start + size]
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "MediaContainer": {
                "size": len(page), "totalSize": len(dataset), "Metadata": page,
            }
        }
        return response
    monkeypatch.setattr("billsmusic.plex_client.requests.get", fake_get)

    diagnostics_events = []
    fake_diagnostics = SimpleNamespace(
        record=lambda category, op, **kw: diagnostics_events.append((category, op, kw)),
        path_details=lambda value: {"path_hash": f"hash-of-{value}"} if value else {},
    )
    monkeypatch.setattr("billsmusic.workers.get_diagnostics", lambda: fake_diagnostics)

    worker = PlexLibraryFetchWorker(
        "http://host:32400", "secret-token", "client-id", "1", item_type,
        media_kind, "server-cfg-id", 1,
    )
    results = []
    progress_events = []
    worker.finished_result.connect(results.append)
    worker.progress.connect(lambda kind, message: progress_events.append((kind, message)))
    worker.start()
    if cancel_after_page is not None:
        deadline = time.monotonic() + 5.0
        while len(fetch_calls) < cancel_after_page and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        worker.stop()
    deadline = time.monotonic() + 5.0
    while not results and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    worker.wait(2000)
    return results, diagnostics_events, progress_events, fetch_calls


def test_large_music_library_is_fetched_across_multiple_bounded_pages(monkeypatch):
    dataset = _paginated_music_dataset(1250)  # 3 pages at PAGE_SIZE=500
    results, events, progress_events, fetch_calls = _run_paginated_worker(monkeypatch, dataset)

    assert results and results[0]["success"] is True
    # G: every item across every page accumulated, none dropped/duplicated.
    assert len(results[0]["meta_list"]) == 1250
    assert {m["rating_key"] for m in results[0]["meta_list"]} == {str(i) for i in range(1250)}
    # F: genuinely paginated, not one giant request.
    assert len(fetch_calls) == 3

    response_details = next(d for _, op, d in events if op == "plex_library_fetch_response")["details"]
    assert response_details["page_count"] == 3
    assert response_details["container_total_size"] == 1250
    assert response_details["raw_item_count"] == 1250

    # J: progress counts are the real accumulated total, never fabricated.
    retrieving_progress = [
        message for kind, message in progress_events if message.startswith("Retrieving")
    ]
    assert retrieving_progress[-2:] == [
        "Retrieving Music library… 1,000 / 1,250",
        "Retrieving Music library… 1,250 / 1,250",
    ]


def test_pagination_stops_promptly_when_cancelled_between_pages(monkeypatch):
    dataset = _paginated_music_dataset(1250)
    results, events, _, fetch_calls = _run_paginated_worker(
        monkeypatch, dataset, cancel_after_page=1, page_delay=0.1,
    )

    assert results and results[0]["success"] is False
    assert results[0]["reason"] == "Cancelled"
    # H: cancellation is checked BETWEEN pages -- the page already in
    # flight when stop() was called still completes (never left half-
    # read), but no further page is started after it.
    assert len(fetch_calls) <= 2


def test_page_failure_reports_a_useful_error_without_losing_progress_info(monkeypatch):
    dataset = _paginated_music_dataset(1250)
    results, events, _, fetch_calls = _run_paginated_worker(
        monkeypatch, dataset, fail_on_page=2,
    )

    assert results and results[0]["success"] is False
    # K: a real, short, safe reason -- matches the actual real-device
    # symptom (PlexConnectionError("Connection timed out")).
    assert results[0]["reason"] == "Connection timed out"
    failed_details = next(d for _, op, d in events if op == "plex_library_refresh_failed")["details"]
    assert failed_details["items_fetched_before_failure"] == 500  # page 1 succeeded
    assert failed_details["page_count"] == 1
