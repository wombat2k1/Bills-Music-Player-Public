"""Thin, purpose-built Plex Media Server HTTP client.

Deliberately NOT built on plexapi (see CODEX_HANDOFF.md's Plex Stage 1
section for the justification): a hand-rolled client extending this app's
existing net.py/http_get conventions needs no new Nuitka packaging beyond
declaring requests itself (requirements-plex.txt), avoids plexapi's own
opinionated session/URL handling fighting this app's established
worker-registry/diagnostics-redaction conventions, and requests is already
a real, installed, transitively-bundled dependency via pychromecast.

Every method here performs real network I/O and must only ever be called
off the GUI thread (see workers.py's PlexConnectionTestWorker, which
imports this module lazily, inside run(), so a plain top-level `import
billsmusic.workers` never pulls in requests). Settings (PlexPreferences)
and identity helpers that must NOT depend on requests live in
plex_preferences.py instead, since window.py needs those at module-import
time and must not be forced to import requests just to load/save
config.json -- Plex, like Cast, is meant to be an optional feature whose
network dependency only loads when the feature is actually used.

Token handling: the token is sent as the X-Plex-Token HTTP header for
every ordinary API request in this module -- never as a query-string
parameter, and never logged. Callers must not construct or log a URL that
embeds the token (media/art URLs that require the token as a query
parameter, if ever needed, are Stage 2/3 concerns -- not built here).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import requests

from . import __version__ as APP_VERSION
from .plex_preferences import generate_client_identifier, normalise_server_address

APP_PRODUCT_NAME = "Bills Music Player"
APP_PLATFORM = "Windows"
CONNECT_TIMEOUT_S = 8.0
READ_TIMEOUT_S = 8.0


@dataclass(frozen=True)
class PlexLibrarySection:
    key: str
    title: str
    type: str  # Plex's own section type: "artist" (music), "movie", "show", etc.


@dataclass(frozen=True)
class PlexServerInfo:
    friendly_name: str
    version: str
    machine_identifier: str
    libraries: List[PlexLibrarySection] = field(default_factory=list)


class PlexConnectionError(Exception):
    """Raised by every PlexClient network method. ``reason`` is always one
    of a small fixed set of user-facing strings (never the token, never a
    token-bearing URL); ``detail`` may carry a low-level exception message
    for diagnostics and must itself never be logged verbatim without
    redaction -- see performance_diagnostics.py's SECRET_KEY_RE/
    TOKEN_VALUE_RE, which already redact anything shaped like a token."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class PlexClient:
    """Minimal synchronous Plex Media Server HTTP client, one server per
    instance. Stage 1 scope: identity + library-section discovery only --
    no browsing, no media URL resolution (Stage 2/3)."""

    def __init__(self, server_address: str, token: str, client_identifier: str):
        self.base_url = normalise_server_address(server_address)
        self._token = token or ""
        self._client_identifier = client_identifier or generate_client_identifier()
        # Populated by fetch_section_items with the *last* call's
        # MediaContainer size/totalSize (if Plex supplied them) -- purely
        # diagnostic, safe to log (no path/token content), read by
        # workers.py's PlexLibraryFetchWorker right after the call returns.
        self.last_container_meta: dict = {}

    def _headers(self) -> dict:
        headers = {
            "Accept": "application/json",
            "X-Plex-Client-Identifier": self._client_identifier,
            "X-Plex-Product": APP_PRODUCT_NAME,
            "X-Plex-Version": APP_VERSION,
            "X-Plex-Platform": APP_PLATFORM,
        }
        if self._token:
            headers["X-Plex-Token"] = self._token
        return headers

    def _get(self, path: str) -> dict:
        if not self.base_url:
            raise PlexConnectionError("Server unreachable", "no server address configured")
        url = f"{self.base_url}{path}"
        try:
            response = requests.get(
                url, headers=self._headers(),
                timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
            )
        except requests.exceptions.Timeout as ex:
            raise PlexConnectionError("Connection timed out", type(ex).__name__) from ex
        except requests.exceptions.RequestException as ex:
            # Deliberately type(ex).__name__ only, not str(ex) -- requests
            # exception messages can include the full request URL, which
            # for other call sites might carry a token; this module's own
            # URLs never do (token is header-only), but keeping this
            # uniform avoids the hazard being reintroduced by a future
            # edit that adds a token-bearing path.
            raise PlexConnectionError("Server unreachable", type(ex).__name__) from ex
        if response.status_code == 401:
            raise PlexConnectionError("Authentication failed", f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise PlexConnectionError("Invalid Plex response", f"HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as ex:
            raise PlexConnectionError("Invalid Plex response", type(ex).__name__) from ex

    def fetch_server_info(self) -> PlexServerInfo:
        """GET / -- server identity (friendlyName/version/machineIdentifier)
        -- then library sections. Raises PlexConnectionError on any
        failure; never returns a partially-valid result."""
        root = self._get("/")
        container = root.get("MediaContainer")
        if not isinstance(container, dict):
            raise PlexConnectionError("Invalid Plex response", "missing MediaContainer")
        friendly_name = str(container.get("friendlyName") or "")
        version = str(container.get("version") or "")
        machine_identifier = str(container.get("machineIdentifier") or "")
        if not machine_identifier:
            raise PlexConnectionError("Invalid Plex response", "missing machineIdentifier")
        libraries = self.fetch_library_sections()
        return PlexServerInfo(
            friendly_name=friendly_name, version=version,
            machine_identifier=machine_identifier, libraries=libraries,
        )

    def fetch_library_sections(self) -> List[PlexLibrarySection]:
        """GET /library/sections -- every accessible library section,
        regardless of type; callers decide which sections map to
        Music/Videos/Karaoke (Plex library names/types are never assumed,
        per product requirement)."""
        data = self._get("/library/sections")
        container = data.get("MediaContainer")
        if not isinstance(container, dict):
            raise PlexConnectionError("Invalid Plex response", "missing MediaContainer")
        directories = container.get("Directory") or []
        sections: List[PlexLibrarySection] = []
        for entry in directories:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("key", "")).strip()
            title = str(entry.get("title", "")).strip()
            kind = str(entry.get("type", "")).strip()
            if key and title:
                sections.append(PlexLibrarySection(key=key, title=title, type=kind))
        return sections

    def fetch_section_items(self, section_key: str, item_type: int = None) -> List[dict]:
        """GET /library/sections/<key>/all[?type=N] -- Plex's own bulk,
        flat listing of every item in one section in ONE request (a music
        library's type=10 "track" listing already embeds parentTitle/
        grandparentTitle/Media/Part inline per track, so this never needs
        a follow-up request per visible row -- see plex_metadata.py's
        conversion functions, which consume exactly this raw shape).
        Returns the raw Metadata list unconverted; callers (workers.py's
        PlexLibraryFetchWorker) convert via plex_metadata.py."""
        path = f"/library/sections/{section_key}/all"
        if item_type is not None:
            path += f"?type={int(item_type)}"
        data = self._get(path)
        container = data.get("MediaContainer")
        if not isinstance(container, dict):
            raise PlexConnectionError("Invalid Plex response", "missing MediaContainer")
        self.last_container_meta = {
            "size": container.get("size"),
            "total_size": container.get("totalSize"),
        }
        items = container.get("Metadata") or []
        return [item for item in items if isinstance(item, dict)]

    def fetch_section_items_page(
        self, section_key: str, item_type, start: int, size: int,
    ):
        """One bounded page of GET /library/sections/<key>/all, using
        Plex's own supported pagination (X-Plex-Container-Start/-Size as
        query parameters -- the standard client approach). Bounding each
        request's payload this way is what actually fixes a large-library
        timeout (a single unbounded request for tens of thousands of
        tracks can legitimately take longer than any one-shot connect/
        read timeout budget); it is not a timeout increase.

        Returns (items, container_meta) where container_meta is
        {"size": <count in this page>, "total_size": <Plex's own reported
        total>} -- callers (PlexLibraryFetchWorker) use total_size from
        the FIRST page to decide how many more pages to fetch and to
        report genuine "N / total" progress, never a fabricated
        percentage."""
        path = f"/library/sections/{section_key}/all?X-Plex-Container-Start={int(start)}&X-Plex-Container-Size={int(size)}"
        if item_type is not None:
            path += f"&type={int(item_type)}"
        data = self._get(path)
        container = data.get("MediaContainer")
        if not isinstance(container, dict):
            raise PlexConnectionError("Invalid Plex response", "missing MediaContainer")
        container_meta = {
            "size": container.get("size"),
            "total_size": container.get("totalSize"),
        }
        self.last_container_meta = container_meta
        items = container.get("Metadata") or []
        return [item for item in items if isinstance(item, dict)], container_meta

    def fetch_item_metadata(self, rating_key: str) -> dict:
        """GET /library/metadata/<ratingKey> -- one item's own current
        Metadata entry (including its Media/Part list), fetched fresh at
        play time rather than reused from whatever was cached during
        library browsing (Stage 3A's own requirement: resolve fresh, in
        case the file/transcode-eligibility changed since the last
        browse). Raises PlexConnectionError if the item no longer
        exists or the fetch otherwise fails; never returns a partial/
        guessed result."""
        data = self._get(f"/library/metadata/{rating_key}")
        container = data.get("MediaContainer")
        if not isinstance(container, dict):
            raise PlexConnectionError("Invalid Plex response", "missing MediaContainer")
        items = container.get("Metadata") or []
        for item in items:
            if isinstance(item, dict) and str(item.get("ratingKey") or "") == str(rating_key):
                return item
        if items and isinstance(items[0], dict):
            return items[0]
        raise PlexConnectionError("Invalid Plex response", "item not found")

    def part_stream_url(self, part_key: str) -> str:
        """The Direct Play transport URL for one Media/Part's `key`
        (e.g. "/library/parts/123/456/file.mp3") -- just base_url + the
        part's own path, no token embedded. Callers attach the token
        themselves, exactly once, in whichever form their backend needs
        (a header for BASS audio, a query parameter for Qt video) -- see
        plex_transport.py's PlexTransportSource docstring for why those
        two paths differ."""
        return f"{self.base_url}{part_key}"
