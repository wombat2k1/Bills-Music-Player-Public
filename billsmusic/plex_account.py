"""Plex account authentication (PIN flow) and server discovery.

Deliberately separate from plex_client.py (which talks to one already-
resolved Plex Media Server) -- this module talks to plex.tv's account-
level API: creating/polling an auth PIN, and listing the Plex servers
an authenticated account can reach, each with its own candidate
connections (local + remote).

Bills Music Player never collects or stores the user's Plex username/
password: the PIN flow hands authentication entirely to plex.tv's own
hosted sign-in page (opened in the user's default browser via the stdlib
`webbrowser` module -- no embedded/imitated login form), and this module
only ever polls for the resulting account token.

Like plex_client.py, this module imports `requests` at module level and
is only ever imported lazily (inside a worker's run(), off the GUI
thread) -- see workers.py's PlexSignInWorker/PlexServerDiscoveryWorker/
PlexConnectionResolveWorker.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import List, Optional

import requests

from . import __version__ as APP_VERSION
from .plex_client import APP_PLATFORM, APP_PRODUCT_NAME, PlexConnectionError

PLEX_TV_BASE = "https://plex.tv"
PIN_CREATE_TIMEOUT_S = (8.0, 8.0)
PIN_POLL_TIMEOUT_S = (8.0, 8.0)
RESOURCES_TIMEOUT_S = (8.0, 8.0)
CONNECTION_PROBE_TIMEOUT_S = (3.0, 3.0)


@dataclass(frozen=True)
class PlexPin:
    pin_id: int
    code: str
    auth_token: str  # "" until the user completes sign-in
    expires_at: str  # Plex's own ISO timestamp string


@dataclass(frozen=True)
class PlexConnection:
    protocol: str
    address: str
    port: int
    uri: str
    local: bool
    relay: bool


@dataclass(frozen=True)
class PlexResource:
    name: str
    client_identifier: str  # Plex's own stable machine id for this server
    owned: bool
    access_token: str
    connections: List[PlexConnection] = field(default_factory=list)


def _base_headers(client_identifier: str, account_token: str = "") -> dict:
    headers = {
        "Accept": "application/json",
        "X-Plex-Client-Identifier": client_identifier,
        "X-Plex-Product": APP_PRODUCT_NAME,
        "X-Plex-Version": APP_VERSION,
        "X-Plex-Platform": APP_PLATFORM,
    }
    if account_token:
        headers["X-Plex-Token"] = account_token
    return headers


class PlexAccountClient:
    """Talks to plex.tv only -- never a specific Media Server."""

    def __init__(self, client_identifier: str):
        if not client_identifier:
            raise ValueError("client_identifier is required")
        self._client_identifier = client_identifier

    def create_pin(self) -> PlexPin:
        """POST /api/v2/pins -- a fresh, short-lived PIN the user completes
        sign-in against on plex.tv's own hosted page."""
        try:
            response = requests.post(
                f"{PLEX_TV_BASE}/api/v2/pins",
                headers=_base_headers(self._client_identifier),
                data={"strong": "true"},
                timeout=PIN_CREATE_TIMEOUT_S,
            )
        except requests.exceptions.Timeout as ex:
            raise PlexConnectionError("Connection timed out", type(ex).__name__) from ex
        except requests.exceptions.RequestException as ex:
            raise PlexConnectionError("Server unreachable", type(ex).__name__) from ex
        if response.status_code >= 400:
            raise PlexConnectionError("Invalid Plex response", f"HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as ex:
            raise PlexConnectionError("Invalid Plex response", type(ex).__name__) from ex
        pin_id = data.get("id")
        code = data.get("code")
        if not pin_id or not code:
            raise PlexConnectionError("Invalid Plex response", "missing id/code")
        return PlexPin(
            pin_id=int(pin_id), code=str(code),
            auth_token=str(data.get("authToken") or ""),
            expires_at=str(data.get("expiresAt") or ""),
        )

    def check_pin(self, pin_id: int) -> PlexPin:
        """GET /api/v2/pins/<id> -- authToken stays empty until the user
        finishes sign-in on plex.tv; a 404 means the PIN expired."""
        try:
            response = requests.get(
                f"{PLEX_TV_BASE}/api/v2/pins/{pin_id}",
                headers=_base_headers(self._client_identifier),
                timeout=PIN_POLL_TIMEOUT_S,
            )
        except requests.exceptions.Timeout as ex:
            raise PlexConnectionError("Connection timed out", type(ex).__name__) from ex
        except requests.exceptions.RequestException as ex:
            raise PlexConnectionError("Server unreachable", type(ex).__name__) from ex
        if response.status_code == 404:
            raise PlexConnectionError("PIN expired", "HTTP 404")
        if response.status_code >= 400:
            raise PlexConnectionError("Invalid Plex response", f"HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as ex:
            raise PlexConnectionError("Invalid Plex response", type(ex).__name__) from ex
        return PlexPin(
            pin_id=pin_id, code=str(data.get("code") or ""),
            auth_token=str(data.get("authToken") or ""),
            expires_at=str(data.get("expiresAt") or ""),
        )

    @staticmethod
    def auth_url(client_identifier: str, pin_code: str) -> str:
        """Pure string construction, no network -- the plex.tv-hosted page
        the user's browser is opened to. Bills Music Player never renders
        or imitates this page itself."""
        from urllib.parse import urlencode
        params = {
            "clientID": client_identifier,
            "code": pin_code,
            "context[device][product]": APP_PRODUCT_NAME,
        }
        return f"https://app.plex.tv/auth#?{urlencode(params)}"

    def fetch_account_username(self, account_token: str) -> str:
        """GET /api/v2/user -- display name shown as "Signed in as: ..."."""
        try:
            response = requests.get(
                f"{PLEX_TV_BASE}/api/v2/user",
                headers=_base_headers(self._client_identifier, account_token),
                timeout=RESOURCES_TIMEOUT_S,
            )
        except requests.exceptions.Timeout as ex:
            raise PlexConnectionError("Connection timed out", type(ex).__name__) from ex
        except requests.exceptions.RequestException as ex:
            raise PlexConnectionError("Server unreachable", type(ex).__name__) from ex
        if response.status_code == 401:
            raise PlexConnectionError("Authentication failed", f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise PlexConnectionError("Invalid Plex response", f"HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as ex:
            raise PlexConnectionError("Invalid Plex response", type(ex).__name__) from ex
        return str(data.get("username") or data.get("email") or data.get("title") or "")

    def fetch_resources(self, account_token: str) -> List[PlexResource]:
        """GET /api/v2/resources -- every Plex Media Server this account
        can reach (owned or shared), each with its candidate connections.
        includeHttps/includeRelay so a remote-only/relay-only server is
        still usable, not silently dropped."""
        try:
            response = requests.get(
                f"{PLEX_TV_BASE}/api/v2/resources",
                headers=_base_headers(self._client_identifier, account_token),
                params={"includeHttps": "1", "includeRelay": "1"},
                timeout=RESOURCES_TIMEOUT_S,
            )
        except requests.exceptions.Timeout as ex:
            raise PlexConnectionError("Connection timed out", type(ex).__name__) from ex
        except requests.exceptions.RequestException as ex:
            raise PlexConnectionError("Server unreachable", type(ex).__name__) from ex
        if response.status_code == 401:
            raise PlexConnectionError("Authentication failed", f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise PlexConnectionError("Invalid Plex response", f"HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as ex:
            raise PlexConnectionError("Invalid Plex response", type(ex).__name__) from ex
        if not isinstance(data, list):
            raise PlexConnectionError("Invalid Plex response", "expected a list")
        resources: List[PlexResource] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            provides = str(entry.get("provides") or "")
            if "server" not in provides:
                continue  # players/other resource types, not a Media Server
            client_id = str(entry.get("clientIdentifier") or "")
            name = str(entry.get("name") or "")
            if not client_id or not name:
                continue
            connections = []
            for conn in entry.get("connections") or []:
                if not isinstance(conn, dict):
                    continue
                uri = str(conn.get("uri") or "")
                address = str(conn.get("address") or "")
                if not uri or not address:
                    continue
                connections.append(PlexConnection(
                    protocol=str(conn.get("protocol") or "https"),
                    address=address,
                    port=int(conn.get("port") or 32400),
                    uri=uri,
                    local=bool(conn.get("local", False)),
                    relay=bool(conn.get("relay", False)),
                ))
            resources.append(PlexResource(
                name=name, client_identifier=client_id,
                owned=bool(entry.get("owned", False)),
                access_token=str(entry.get("accessToken") or account_token),
                connections=connections,
            ))
        return resources


def select_best_connection(
    connections: List[PlexConnection], access_token: str,
    client_identifier: str, timeout=CONNECTION_PROBE_TIMEOUT_S,
) -> Optional[PlexConnection]:
    """Probes every candidate connection for this server CONCURRENTLY and
    returns a reachable one, preferring local over remote/relay.

    Deliberately NOT the naive sequential "try local1, then local2, then
    remote" loop: measured analytically before this was written, that
    approach is O(len(connections) * timeout) in the worst case -- a
    laptop away from home with 3 stale local candidates plus 1 working
    remote one would wait up to 9s (or the full 12s if nothing at all is
    reachable) before Plex connection resolution finished, which is not
    an acceptable "away from home" experience. Firing every probe at once
    on a small bounded thread pool instead bounds the *whole* resolution
    to roughly one timeout period regardless of how many connections a
    server advertises -- the pool size is len(connections), never
    unbounded, and every probe still has its own real per-request
    timeout, so this cannot hang indefinitely either. A cheap
    GET /identity call (no library data) confirms genuine reachability,
    not just that the socket connects. Only ever called from a background
    worker (PlexConnectionResolveWorker), never the GUI thread."""
    if not connections:
        return None
    headers = _base_headers(client_identifier, access_token)

    def _probe(conn: PlexConnection) -> Optional[PlexConnection]:
        try:
            response = requests.get(
                f"{conn.uri}/identity", headers=headers, timeout=timeout,
            )
            return conn if response.status_code < 400 else None
        except requests.exceptions.RequestException:
            return None

    with ThreadPoolExecutor(max_workers=len(connections)) as pool:
        future_to_conn = {pool.submit(_probe, conn): conn for conn in connections}
        local_result: Optional[PlexConnection] = None
        remote_result: Optional[PlexConnection] = None
        for future in as_completed(future_to_conn):
            reachable = future.result()
            if reachable is None:
                continue
            if reachable.local:
                local_result = reachable
                break  # a reachable local connection always wins immediately
            elif remote_result is None:
                remote_result = reachable  # keep the first remote; local might still arrive
        return local_result or remote_result
