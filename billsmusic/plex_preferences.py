"""Plex integration settings + identity helpers that must NOT depend on
``requests``.

Deliberately separate from plex_client.py: window.py needs PlexPreferences
at module-import time (to load/save config.json), but must not be forced
to import ``requests`` just to do that -- Plex, like Cast, is meant to be
an optional feature whose network dependency only loads when the feature
is actually used. plex_client.py (the real HTTP client, which does import
requests) is only ever imported lazily, off the GUI thread, from inside
workers.py's PlexConnectionTestWorker.run().
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Mapping


def generate_client_identifier() -> str:
    """A fresh, random RFC-4122 UUID string. Callers persist this once
    (config.json's plex_client_identifier) and reuse it for the lifetime
    of the install -- Plex uses X-Plex-Client-Identifier to recognise this
    app as a single stable client across requests/sessions/servers."""
    return str(uuid.uuid4())


def generate_server_config_id() -> str:
    """A fresh, random, locally-generated identifier for one configured
    Plex server connection -- deliberately NOT derived from the server
    URL/IP, so a later LAN->remote address change does not invalidate any
    plex://<server-config-id>/<ratingKey> queue identity already persisted
    against it."""
    return str(uuid.uuid4())


def normalise_server_address(address: str) -> str:
    """Strip whitespace/trailing slashes; default to http:// if the user
    only typed host:port. Pure string handling -- no network access."""
    address = (address or "").strip()
    if not address:
        return ""
    if "://" not in address:
        address = f"http://{address}"
    return address.rstrip("/")


@dataclass(frozen=True)
class PlexPreferences:
    """Everything persisted in config.json for the Plex integration.
    Mirrors VideoTransitionPreferences/DualTransitionPreferences's own
    typed-dataclass-with-from_config() convention (video_transition.py)
    rather than scattering cfg.get(...) calls through window.py.

    Library mappings are stored as (id, name) pairs -- the id is the
    stable Plex section key/identity used for actual mapping lookups; the
    name is display-only, kept so a saved-but-currently-unreachable
    section can still be shown to the user by name (see
    library_mapping_display() in window.py) rather than as a bare id.
    An empty id means "not mapped" (always valid for karaoke; the UI
    allows it for music/video too since nothing requires a mapping until
    the user actually browses that Plex category).

    Two authentication routes, `use_manual_server` selects which is
    active: the PRIMARY route is Plex account sign-in (PIN flow, see
    plex_account.py) -- `account_token`/`account_username` are populated
    from that, and the actual server is one of the account's discovered
    resources, identified by `server_client_identifier` (Plex's own
    stable machine id, NOT `server_config_id`, which stays our own
    locally-generated, connection-URL-independent identity). The
    fallback ADVANCED route is the original Stage 1 manual entry
    (`server_address`/`token` typed directly) -- kept for reverse
    proxies, development, or when discovery fails.

    `server_config_id_map` maps a Plex `clientIdentifier` (one physical
    server, stable across LAN/remote/relay connections) to the
    locally-generated `server_config_id` used in this server's
    `plex://<server_config_id>/<ratingKey>` queue identities -- looked up
    once per physical server and reused forever after, so re-selecting
    the same server (even after its address changes) never invalidates
    previously-persisted queue rows, and two different servers never
    collide. `server_config_id` itself always mirrors
    `server_config_id_map[server_client_identifier]` for the currently
    selected server -- kept as its own field for the same "just read
    self.plex_preferences.server_config_id" convenience the rest of the
    codebase already expects from Stage 1.

    Library section keys are only meaningful *within* one Plex server --
    section "1" on server A may be an unrelated library on server B.
    `library_mappings_by_server` therefore keys the real, persisted
    mapping by `server_client_identifier` (one entry per server, shape
    {"music_library_id"/"_name", "video_library_id"/"_name",
    "karaoke_library_id"/"_name"}). The flat `music_library_id` etc.
    fields below remain as a convenience cache of *whichever* server is
    currently selected (mirroring server_config_id's own "flat field
    mirrors the map entry for the active server" pattern) -- for manual
    mode (no per-server concept, only ever one configured server) they
    are the sole source of truth, unchanged from Stage 1."""

    enabled: bool = False
    use_manual_server: bool = False
    server_address: str = ""
    token: str = ""
    account_token: str = ""
    account_username: str = ""
    client_identifier: str = ""
    server_config_id: str = ""
    server_config_id_map: Mapping[str, str] = field(default_factory=dict)
    server_client_identifier: str = ""
    server_machine_identifier: str = ""
    server_name: str = ""
    server_version: str = ""
    music_library_id: str = ""
    music_library_name: str = ""
    video_library_id: str = ""
    video_library_name: str = ""
    karaoke_library_id: str = ""
    karaoke_library_name: str = ""
    library_mappings_by_server: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> "PlexPreferences":
        def text(key: str) -> str:
            value = config.get(key, "")
            return value if isinstance(value, str) else ""

        raw_map = config.get("plex_server_config_id_map", {})
        server_config_id_map = (
            {str(k): str(v) for k, v in raw_map.items() if isinstance(k, str) and isinstance(v, str)}
            if isinstance(raw_map, Mapping) else {}
        )
        raw_lib_map = config.get("plex_library_mappings_by_server", {})
        library_mappings_by_server = {}
        if isinstance(raw_lib_map, Mapping):
            for server_id, mapping in raw_lib_map.items():
                if isinstance(server_id, str) and isinstance(mapping, Mapping):
                    library_mappings_by_server[server_id] = {
                        str(k): str(v) for k, v in mapping.items()
                        if isinstance(k, str) and isinstance(v, str)
                    }

        return cls(
            enabled=bool(config.get("plex_enabled", False)),
            use_manual_server=bool(config.get("plex_use_manual_server", False)),
            server_address=text("plex_server_address"),
            token=text("plex_token"),
            account_token=text("plex_account_token"),
            account_username=text("plex_account_username"),
            client_identifier=text("plex_client_identifier"),
            server_config_id=text("plex_server_config_id"),
            server_config_id_map=server_config_id_map,
            server_client_identifier=text("plex_server_client_identifier"),
            server_machine_identifier=text("plex_server_machine_identifier"),
            server_name=text("plex_server_name"),
            server_version=text("plex_server_version"),
            music_library_id=text("plex_music_library_id"),
            music_library_name=text("plex_music_library_name"),
            video_library_id=text("plex_video_library_id"),
            video_library_name=text("plex_video_library_name"),
            karaoke_library_id=text("plex_karaoke_library_id"),
            karaoke_library_name=text("plex_karaoke_library_name"),
            library_mappings_by_server=library_mappings_by_server,
        )

    def to_config_updates(self) -> dict:
        """Key/value pairs to merge into the existing config dict (never
        replace it wholesale -- see window.py's _save_user_settings, which
        always does cfg = load_config() or {} then sets individual keys,
        so unrelated settings are preserved automatically)."""
        return {
            "plex_enabled": self.enabled,
            "plex_use_manual_server": self.use_manual_server,
            "plex_server_address": self.server_address,
            "plex_token": self.token,
            "plex_account_token": self.account_token,
            "plex_account_username": self.account_username,
            "plex_client_identifier": self.client_identifier,
            "plex_server_config_id": self.server_config_id,
            "plex_server_config_id_map": dict(self.server_config_id_map),
            "plex_server_client_identifier": self.server_client_identifier,
            "plex_server_machine_identifier": self.server_machine_identifier,
            "plex_server_name": self.server_name,
            "plex_server_version": self.server_version,
            "plex_music_library_id": self.music_library_id,
            "plex_music_library_name": self.music_library_name,
            "plex_video_library_id": self.video_library_id,
            "plex_video_library_name": self.video_library_name,
            "plex_karaoke_library_id": self.karaoke_library_id,
            "plex_karaoke_library_name": self.karaoke_library_name,
            "plex_library_mappings_by_server": {
                server_id: dict(mapping)
                for server_id, mapping in self.library_mappings_by_server.items()
            },
        }

    def resolved_library_mapping(self, server_client_identifier: str) -> Mapping[str, str]:
        """The persisted (id, name) pairs for one server's Music/Video/
        Karaoke mapping -- empty dict if that server has never been
        configured (never falls back to another server's mapping)."""
        return self.library_mappings_by_server.get(server_client_identifier, {})

    def resolved_server_config_id(self, plex_client_identifier: str) -> str:
        """Looks up (never generates) the server_config_id for a given
        Plex server. Callers that need to CREATE one for a never-before-
        seen server call generate_server_config_id() and add it to a new
        server_config_id_map themselves (window.py's server-selection
        handler) -- kept out of this read-only accessor so this dataclass
        stays side-effect-free."""
        return self.server_config_id_map.get(plex_client_identifier, "")
