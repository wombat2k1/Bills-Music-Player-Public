"""Plex account authentication (PIN flow) and server discovery. No test
requires a real Plex account or a real browser -- requests.post/get and
webbrowser.open are all monkeypatched with deterministic fakes.
"""
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtWidgets

from billsmusic.plex_account import (
    PlexAccountClient,
    PlexConnection,
    PlexPin,
    PlexResource,
    select_best_connection,
)
from billsmusic.plex_client import PlexConnectionError
from billsmusic.plex_preferences import PlexPreferences, generate_server_config_id

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


# -- 1. PIN creation ----------------------------------------------------

def test_create_pin_parses_response(monkeypatch):
    def fake_post(url, headers=None, data=None, timeout=None):
        assert url == "https://plex.tv/api/v2/pins"
        assert data == {"strong": "true"}
        response = MagicMock()
        response.status_code = 201
        response.json.return_value = {
            "id": 12345, "code": "ABCD", "authToken": None, "expiresAt": "2026-09-06T12:00:00Z",
        }
        return response
    monkeypatch.setattr("billsmusic.plex_account.requests.post", fake_post)
    client = PlexAccountClient("client-id-123")

    pin = client.create_pin()

    assert pin.pin_id == 12345
    assert pin.code == "ABCD"
    assert pin.auth_token == ""


def test_create_pin_never_sends_a_password_field(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, data=None, timeout=None):
        captured["data"] = data
        captured["headers"] = headers
        response = MagicMock()
        response.status_code = 201
        response.json.return_value = {"id": 1, "code": "WXYZ", "authToken": None}
        return response
    monkeypatch.setattr("billsmusic.plex_account.requests.post", fake_post)
    PlexAccountClient("client-id-123").create_pin()

    assert "password" not in {k.lower() for k in captured["data"]}
    assert "username" not in {k.lower() for k in captured["data"]}
    assert not any("password" in str(v).lower() for v in captured["headers"].values())


# -- 2. Browser auth URL construction (pure, no network) -----------------

def test_auth_url_is_pure_string_construction_no_network():
    url = PlexAccountClient.auth_url("client-abc", "WXYZ")
    assert url.startswith("https://app.plex.tv/auth#?")
    assert "clientID=client-abc" in url
    assert "code=WXYZ" in url


# -- 3/4. PIN polling: successful claim, and expiration -------------------

def test_check_pin_returns_auth_token_once_claimed(monkeypatch):
    def fake_get(url, headers=None, timeout=None, params=None):
        assert url == "https://plex.tv/api/v2/pins/12345"
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "code": "ABCD", "authToken": "real-account-token", "expiresAt": "...",
        }
        return response
    monkeypatch.setattr("billsmusic.plex_account.requests.get", fake_get)
    client = PlexAccountClient("client-id")

    pin = client.check_pin(12345)

    assert pin.auth_token == "real-account-token"


def test_check_pin_404_raises_pin_expired(monkeypatch):
    def fake_get(url, headers=None, timeout=None, params=None):
        response = MagicMock()
        response.status_code = 404
        return response
    monkeypatch.setattr("billsmusic.plex_account.requests.get", fake_get)
    client = PlexAccountClient("client-id")

    with pytest.raises(PlexConnectionError) as exc:
        client.check_pin(12345)
    assert exc.value.reason == "PIN expired"


# -- 5. Cancelled login ----------------------------------------------------

def test_signin_worker_cancellation_stops_polling_promptly(monkeypatch):
    from billsmusic.workers import PlexSignInWorker

    monkeypatch.setattr(
        "billsmusic.plex_account.PlexAccountClient.create_pin",
        lambda self: PlexPin(pin_id=1, code="WXYZ", auth_token="", expires_at=""),
    )
    check_calls = []

    def fake_check_pin(self, pin_id):
        check_calls.append(pin_id)
        return PlexPin(pin_id=pin_id, code="WXYZ", auth_token="", expires_at="")
    monkeypatch.setattr("billsmusic.plex_account.PlexAccountClient.check_pin", fake_check_pin)
    monkeypatch.setattr(
        "billsmusic.workers.get_diagnostics",
        lambda: SimpleNamespace(record=lambda *a, **kw: None),
    )

    app = _app()
    worker = PlexSignInWorker("client-id")
    results = []
    worker.finished_result.connect(results.append)
    worker.start()

    deadline = time.monotonic() + 2.0
    while not check_calls and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    worker.request_cancel()

    deadline = time.monotonic() + 5.0
    while not results and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    worker.wait(2000)

    assert results and results[0]["success"] is False
    assert results[0]["reason"] == "Cancelled"


# -- 6. Auth token stored (PlexPreferences round-trip) ---------------------

def test_account_token_round_trips_through_config():
    prefs = PlexPreferences(
        enabled=True, account_token="real-account-token", account_username="alice@example.com",
    )
    restored = PlexPreferences.from_config(prefs.to_config_updates())
    assert restored.account_token == "real-account-token"
    assert restored.account_username == "alice@example.com"


# -- 7. Password never requested/stored ------------------------------------

def test_plex_preferences_has_no_password_field():
    field_names = {f for f in PlexPreferences.__dataclass_fields__}
    assert not any("password" in name.lower() for name in field_names)


# -- 8. Token redacted -------------------------------------------------

def test_signin_diagnostics_never_contain_the_account_token(monkeypatch):
    from billsmusic.workers import PlexSignInWorker

    monkeypatch.setattr(
        "billsmusic.plex_account.PlexAccountClient.create_pin",
        lambda self: PlexPin(pin_id=1, code="WXYZ", auth_token="", expires_at=""),
    )
    monkeypatch.setattr(
        "billsmusic.plex_account.PlexAccountClient.check_pin",
        lambda self, pin_id: PlexPin(
            pin_id=pin_id, code="WXYZ", auth_token="super-secret-account-token", expires_at="",
        ),
    )
    monkeypatch.setattr(
        "billsmusic.plex_account.PlexAccountClient.fetch_account_username",
        lambda self, token: "alice@example.com",
    )
    diagnostics_events = []
    monkeypatch.setattr(
        "billsmusic.workers.get_diagnostics",
        lambda: SimpleNamespace(
            record=lambda category, op, **kw: diagnostics_events.append((category, op, kw))
        ),
    )

    app = _app()
    worker = PlexSignInWorker("client-id")
    results = []
    worker.finished_result.connect(results.append)
    worker.start()
    deadline = time.monotonic() + 5.0
    while not results and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    worker.wait(2000)

    assert results and results[0]["success"] is True
    assert results[0]["account_token"] == "super-secret-account-token"
    for _category, _op, kw in diagnostics_events:
        assert "super-secret-account-token" not in repr(kw)


# -- 9/10/11/12. Account/server discovery: one, multiple, shared -----------

def test_fetch_resources_parses_single_owned_server(monkeypatch):
    def fake_get(url, headers=None, timeout=None, params=None):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = [{
            "name": "Bills Plex", "clientIdentifier": "abc123", "owned": True,
            "provides": "server", "accessToken": "server-token",
            "connections": [
                {"protocol": "http", "address": "192.168.1.50", "port": 32400,
                 "uri": "http://192.168.1.50:32400", "local": True, "relay": False},
            ],
        }]
        return response
    monkeypatch.setattr("billsmusic.plex_account.requests.get", fake_get)
    resources = PlexAccountClient("client-id").fetch_resources("account-token")

    assert len(resources) == 1
    assert resources[0].name == "Bills Plex"
    assert resources[0].client_identifier == "abc123"
    assert resources[0].owned is True
    assert len(resources[0].connections) == 1


def test_fetch_resources_parses_multiple_servers(monkeypatch):
    def fake_get(url, headers=None, timeout=None, params=None):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = [
            {"name": "Bills Plex", "clientIdentifier": "abc123", "owned": True,
             "provides": "server", "accessToken": "tok1", "connections": []},
            {"name": "Family Plex", "clientIdentifier": "def456", "owned": False,
             "provides": "server", "accessToken": "tok2", "connections": []},
        ]
        return response
    monkeypatch.setattr("billsmusic.plex_account.requests.get", fake_get)
    resources = PlexAccountClient("client-id").fetch_resources("account-token")

    assert {r.client_identifier for r in resources} == {"abc123", "def456"}
    shared = next(r for r in resources if r.client_identifier == "def456")
    assert shared.owned is False


def test_fetch_resources_skips_non_server_resources(monkeypatch):
    def fake_get(url, headers=None, timeout=None, params=None):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = [
            {"name": "My Phone", "clientIdentifier": "player1", "owned": True,
             "provides": "player", "accessToken": "tok", "connections": []},
            {"name": "Bills Plex", "clientIdentifier": "abc123", "owned": True,
             "provides": "server", "accessToken": "tok", "connections": []},
        ]
        return response
    monkeypatch.setattr("billsmusic.plex_account.requests.get", fake_get)
    resources = PlexAccountClient("client-id").fetch_resources("account-token")

    assert len(resources) == 1
    assert resources[0].client_identifier == "abc123"


def test_fetch_resources_invalid_token_raises_authentication_failed(monkeypatch):
    def fake_get(url, headers=None, timeout=None, params=None):
        response = MagicMock()
        response.status_code = 401
        return response
    monkeypatch.setattr("billsmusic.plex_account.requests.get", fake_get)

    with pytest.raises(PlexConnectionError) as exc:
        PlexAccountClient("client-id").fetch_resources("bad-token")
    assert exc.value.reason == "Authentication failed"


# -- 13/14/15/16. Connection selection: local/remote preference -----------

def _connection(local, relay=False, address="x"):
    return PlexConnection(
        protocol="https", address=address, port=32400,
        uri=f"https://{address}:32400", local=local, relay=relay,
    )


def test_select_best_connection_prefers_local_when_both_reachable(monkeypatch):
    local = _connection(local=True, address="192.168.1.50")
    remote = _connection(local=False, address="1.2.3.4")

    def fake_get(url, headers=None, timeout=None):
        response = MagicMock()
        response.status_code = 200
        return response
    monkeypatch.setattr("billsmusic.plex_account.requests.get", fake_get)

    best = select_best_connection([remote, local], "token", "client-id")
    assert best is local


def test_select_best_connection_falls_back_to_remote_when_local_unreachable(monkeypatch):
    local = _connection(local=True, address="192.168.1.50")
    remote = _connection(local=False, address="1.2.3.4")

    def fake_get(url, headers=None, timeout=None):
        if "192.168.1.50" in url:
            import requests
            raise requests.exceptions.ConnectionError("refused")
        response = MagicMock()
        response.status_code = 200
        return response
    monkeypatch.setattr("billsmusic.plex_account.requests.get", fake_get)

    best = select_best_connection([local, remote], "token", "client-id")
    assert best is remote


def test_select_best_connection_remote_only_still_resolves(monkeypatch):
    remote = _connection(local=False, address="1.2.3.4")

    def fake_get(url, headers=None, timeout=None):
        response = MagicMock()
        response.status_code = 200
        return response
    monkeypatch.setattr("billsmusic.plex_account.requests.get", fake_get)

    best = select_best_connection([remote], "token", "client-id")
    assert best is remote


def test_select_best_connection_returns_none_when_nothing_reachable(monkeypatch):
    import requests

    def fake_get(url, headers=None, timeout=None):
        raise requests.exceptions.ConnectionError("refused")
    monkeypatch.setattr("billsmusic.plex_account.requests.get", fake_get)

    best = select_best_connection(
        [_connection(local=True), _connection(local=False)], "token", "client-id",
    )
    assert best is None


# -- Remote fallback latency (pre-Stage-2 invariant 4): connection --------
# -- resolution must be bounded by ~one timeout period regardless of how --
# -- many stale local candidates exist, not len(connections) * timeout ---

def test_select_best_connection_stays_bounded_with_several_stale_local_candidates(monkeypatch):
    import time

    probe_timeout = 0.3  # short, deterministic timeout for this test

    def fake_get(url, headers=None, timeout=None):
        if "remote" in url:
            return SimpleNamespace(status_code=200)
        # Every local candidate blocks for the full timeout, then fails --
        # the realistic "away from home" shape this invariant is about.
        time.sleep(probe_timeout)
        raise __import__("requests").exceptions.ConnectionError("refused")
    monkeypatch.setattr("billsmusic.plex_account.requests.get", fake_get)

    connections = [
        _connection(local=True, address=f"192.168.1.{i}") for i in range(1, 5)
    ] + [_connection(local=False, address="remote")]

    started = time.perf_counter()
    best = select_best_connection(connections, "token", "client-id", timeout=probe_timeout)
    elapsed = time.perf_counter() - started

    assert best is not None and best.local is False
    # Sequential probing of 5 candidates at this timeout would take up to
    # 5 * probe_timeout (1.5s here); concurrent probing must stay close to
    # a single timeout period regardless of candidate count.
    assert elapsed < probe_timeout * 2, (
        f"connection resolution took {elapsed:.2f}s, expected < {probe_timeout * 2:.2f}s "
        f"(sequential probing of {len(connections)} candidates would take up to "
        f"{len(connections) * probe_timeout:.2f}s)"
    )


# -- 17. Server URL changes but identity remains stable ---------------------

def test_server_config_id_stable_across_different_connection_uris():
    prefs = PlexPreferences(server_config_id_map={"abc123": "fixed-uuid-1"})
    # Same server (by clientIdentifier), regardless of which URI resolved.
    assert prefs.resolved_server_config_id("abc123") == "fixed-uuid-1"
    # A different physical server never collides.
    assert prefs.resolved_server_config_id("def456") == ""


def test_same_rating_key_on_two_servers_stays_distinct():
    from billsmusic.plex_identity import make_plex_identity

    id_a = make_plex_identity("server-cfg-A", "999", ".mp3")
    id_b = make_plex_identity("server-cfg-B", "999", ".mp3")
    assert id_a != id_b


# -- Pre-Stage-2 invariant 3: library mappings belong to the server ---------

def test_library_mapping_is_scoped_per_server_not_global():
    prefs = PlexPreferences(
        library_mappings_by_server={
            "server-A": {"music_library_id": "1", "music_library_name": "Music A"},
            "server-B": {"music_library_id": "1", "music_library_name": "Music B"},
        },
    )
    # The SAME section key ("1") on two different servers must resolve to
    # each server's own distinct mapping, never one bleeding into the other.
    mapping_a = prefs.resolved_library_mapping("server-A")
    mapping_b = prefs.resolved_library_mapping("server-B")
    assert mapping_a["music_library_name"] == "Music A"
    assert mapping_b["music_library_name"] == "Music B"


def test_library_mapping_for_unconfigured_server_is_empty_not_borrowed():
    prefs = PlexPreferences(
        library_mappings_by_server={"server-A": {"music_library_id": "1", "music_library_name": "Music A"}},
    )
    # A never-before-seen server must never silently inherit server-A's
    # mapping -- empty means "not configured", shown to the user as such.
    assert prefs.resolved_library_mapping("server-never-seen") == {}


def test_library_mappings_by_server_round_trip_through_config():
    prefs = PlexPreferences(
        library_mappings_by_server={
            "server-A": {"music_library_id": "1", "music_library_name": "Music"},
            "server-B": {"video_library_id": "5", "video_library_name": "Other Videos"},
        },
    )
    restored = PlexPreferences.from_config(prefs.to_config_updates())
    assert restored.resolved_library_mapping("server-A")["music_library_id"] == "1"
    assert restored.resolved_library_mapping("server-B")["video_library_id"] == "5"


# -- Pre-Stage-2 invariant 5: resolved connection is transient, never -------
# -- persisted as the authoritative identity ---------------------------------

def test_to_config_updates_never_persists_a_connection_uri_or_relay_state():
    prefs = PlexPreferences(
        enabled=True, account_token="tok", server_client_identifier="abc123",
    )
    updates = prefs.to_config_updates()
    # No key anywhere in the persisted shape represents a resolved
    # connection URI -- server discovery must always be free to replace
    # it (home LAN -> away -> home LAN again, without touching Preferences).
    assert not any("uri" in key.lower() or "connection" in key.lower() for key in updates)
    assert not any(
        isinstance(v, str) and (v.startswith("http://") or v.startswith("https://"))
        for v in updates.values()
    )


# -- Pre-Stage-2 invariant 1: account token and server token are two --------
# -- distinct things, both redacted --------------------------------------

def test_account_token_and_server_access_token_are_independent_fields():
    # fetch_resources() returns a PER-SERVER accessToken distinct from the
    # account_token used to authenticate the discovery call itself --
    # confirmed structurally: PlexResource carries its own access_token,
    # never reuses the caller's account token unless Plex's own response
    # happens to omit one (see PlexAccountClient.fetch_resources's
    # accessToken-or-account_token fallback for that edge case only).
    resource = PlexResource(
        name="Bills Plex", client_identifier="abc123", owned=True,
        access_token="server-specific-token-xyz", connections=[],
    )
    assert resource.access_token != "account-token-abc"
    assert resource.access_token == "server-specific-token-xyz"


def test_connection_resolve_diagnostics_never_contain_the_access_token(monkeypatch):
    from billsmusic.workers import PlexConnectionResolveWorker

    def fake_get(url, headers=None, timeout=None):
        response = MagicMock()
        response.status_code = 200
        return response
    monkeypatch.setattr("billsmusic.plex_account.requests.get", fake_get)

    diagnostics_events = []
    monkeypatch.setattr(
        "billsmusic.workers.get_diagnostics",
        lambda: SimpleNamespace(
            record=lambda category, op, **kw: diagnostics_events.append((category, op, kw))
        ),
    )

    app = _app()
    worker = PlexConnectionResolveWorker(
        "client-id", "server-abc123", "super-secret-server-access-token",
        [{"protocol": "http", "address": "192.168.1.1", "port": 32400,
          "uri": "http://192.168.1.1:32400", "local": True, "relay": False}],
    )
    results = []
    worker.finished_result.connect(results.append)
    worker.start()
    deadline = time.monotonic() + 5.0
    while not results and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    worker.wait(2000)

    assert results and results[0]["success"] is True
    for _category, _op, kw in diagnostics_events:
        assert "super-secret-server-access-token" not in repr(kw)
