"""Stable Plex media identity: parsing/construction only, zero dependencies
(no PyQt, no requests) so this can be imported anywhere, including
session.py and config-adjacent modules that must stay dependency-light.

A Plex queue "path" is never the streaming URL (which is short-lived and
token-bearing) and never derived from the server address (which can
change LAN -> remote without invalidating anything). It's a synthetic,
stable string:

    plex://<server_config_id>/<rating_key><extension>

`server_config_id` is the locally-generated UUID identifying one
configured Plex server connection (plex_preferences.py's
generate_server_config_id() -- NOT Plex's own machineIdentifier, and NOT
derived from the URL/IP). `rating_key` is Plex's own stable per-item
identifier within that server. `extension` (including the leading dot,
e.g. ".mp3"/".mp4") is carried only so existing extension-based code
(classify_path's fallback, display, etc.) has something to work with --
it is never authoritative for AUDIO/VIDEO classification; Plex's own
reported metadata type is (see plex_client.py's media_type field on each
fetched item and window.py's handling of it).

Two different configured servers produce different server_config_id
values, so the same rating_key on two servers never collides.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

PLEX_SCHEME = "plex://"


@dataclass(frozen=True)
class PlexIdentity:
    server_config_id: str
    rating_key: str
    extension: str  # includes leading "." or is ""


def make_plex_identity(server_config_id: str, rating_key: str, extension: str = "") -> str:
    if not server_config_id or not rating_key:
        raise ValueError("server_config_id and rating_key are both required")
    ext = extension if not extension or extension.startswith(".") else f".{extension}"
    return f"{PLEX_SCHEME}{server_config_id}/{rating_key}{ext}"


def is_plex_identity(path: object) -> bool:
    return isinstance(path, str) and path.startswith(PLEX_SCHEME)


def parse_plex_identity(path: str) -> Optional[PlexIdentity]:
    if not is_plex_identity(path):
        return None
    # urlsplit handles the "plex://<server>/<rating_key><ext>" shape
    # cleanly (netloc = server_config_id, path = "/<rating_key><ext>")
    # without hand-rolled string slicing.
    parsed = urlsplit(path)
    server_config_id = parsed.netloc
    remainder = parsed.path.lstrip("/")
    if not server_config_id or not remainder:
        return None
    if "." in remainder:
        rating_key, _, ext = remainder.rpartition(".")
        extension = f".{ext}" if rating_key else ""
        if not rating_key:
            rating_key = remainder
            extension = ""
    else:
        rating_key = remainder
        extension = ""
    return PlexIdentity(
        server_config_id=server_config_id, rating_key=rating_key, extension=extension,
    )
