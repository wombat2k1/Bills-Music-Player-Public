"""Filesystem paths, persisted config, and application-wide constants."""
import os
import json
import hashlib
from typing import Dict, Any

from .media_type import SUPPORTED_MEDIA_EXTENSIONS

APP_TITLE = "Bills Music Player"


def cache_file_path() -> str:
    base = os.environ.get("LOCALAPPDATA", os.path.abspath("."))
    folder = os.path.join(base, "Bills Music Player")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "library_cache.json")


def plex_library_cache_path() -> str:
    """Stage 3A: persisted Stage-2 Plex library metadata (title/artist/
    album/disc/track/duration/ratingKey/updatedAt/artwork identity/media-
    part data) -- never a transport URL or token, see plex_metadata.py's
    conversion functions, which never produce either. Kept in its own
    file, separate from library_cache.json (the Local library's own
    cache), so a user with no Plex configured never pays any extra
    startup cost, and a Plex library refresh never touches/resizes the
    (often much larger) Local cache file."""
    base = os.environ.get("LOCALAPPDATA", os.path.abspath("."))
    folder = os.path.join(base, "Bills Music Player")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "plex_library_cache.json")


def queue_analysis_cache_path() -> str:
    base = os.environ.get("LOCALAPPDATA", os.path.abspath("."))
    folder = os.path.join(base, "Bills Music Player")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "queue_analysis_cache.json")


def loudness_cache_path() -> str:
    base = os.environ.get("LOCALAPPDATA", os.path.abspath("."))
    folder = os.path.join(base, "Bills Music Player")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "loudness_cache.json")


def video_transition_point_cache_path() -> str:
    base = os.environ.get("LOCALAPPDATA", os.path.abspath("."))
    folder = os.path.join(base, "Bills Music Player")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "video_transition_point_cache.json")


def config_file_path() -> str:
    base = os.environ.get("LOCALAPPDATA", os.path.abspath("."))
    folder = os.path.join(base, "Bills Music Player")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "config.json")


def session_file_path() -> str:
    base = os.environ.get("LOCALAPPDATA", os.path.abspath("."))
    folder = os.path.join(base, "Bills Music Player")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "session.json")


def recently_played_file_path() -> str:
    base = os.environ.get("LOCALAPPDATA", os.path.abspath("."))
    folder = os.path.join(base, "Bills Music Player")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "recently_played.json")


def load_config() -> Dict[str, Any]:
    try:
        with open(config_file_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def cover_cache_dir() -> str:
    base = os.environ.get("LOCALAPPDATA", os.path.abspath("."))
    folder = os.path.join(base, "Bills Music Player", "covers")
    os.makedirs(folder, exist_ok=True)
    return folder


def album_cache_key(artist: str, album: str) -> str:
    raw = f"{artist}::{album}".encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()


def album_cover_cache_path(artist: str, album: str) -> str:
    filename = f"{album_cache_key(artist, album)}.jpg"
    return os.path.join(cover_cache_dir(), filename)


def waveform_cache_dir() -> str:
    base = os.environ.get("LOCALAPPDATA", os.path.abspath("."))
    folder = os.path.join(base, "Bills Music Player", "waveforms")
    os.makedirs(folder, exist_ok=True)
    return folder


def karaoke_cache_dir() -> str:
    base = os.environ.get("LOCALAPPDATA", os.path.abspath("."))
    folder = os.path.join(base, "Bills Music Player", "karaoke")
    os.makedirs(folder, exist_ok=True)
    return folder


CROSSFADE_SECONDS = 6.0
CROSSFADE_SECONDS_MIN = 1.0
CROSSFADE_SECONDS_MAX = 15.0
NORMAL_TRANSITION_EPSILON_SECONDS = 0.15
TRACK_TRANSITION_MODES = ("normal", "crossfade")
PARTY_MODE_LAYOUTS = ("lyrics", "visualiser", "artwork")
PARTY_MODE_VISUAL_QUALITIES = ("low", "medium", "high")
PARTY_MODE_UP_NEXT_COUNT_MIN = 1
PARTY_MODE_UP_NEXT_COUNT_MAX = 5
PARTY_MODE_AUTO_HIDE_SECONDS_MIN = 1
PARTY_MODE_AUTO_HIDE_SECONDS_MAX = 15
FADE_INTERVAL_MS = 30
PREBUFFER_MS = 500
VLC_LOCAL_FILE_CACHING_MS = 1000
VLC_NETWORK_CACHING_MS = 8000
VLC_NETWORK_SHARE_CACHING_MS = 8000
FADE_TRIGGER_DB = -18.0
FADE_TRIGGER_WINDOW_SECONDS = 25.0
FADE_QUIET_FRAMES = 2
FADE_IN_EXP = 0.4
FADE_OUT_EXP = 1.8
ALLOW_INSECURE_TLS_FALLBACK = True
BIO_PAUSE_AFTER_LEAVE_SEC = 2.5
BIO_PAUSE_AT_END_SEC = 5.0
TAG_PAUSE_AFTER_LEAVE_SEC = 2.0
TAG_PAUSE_AT_END_SEC = 4.0
# .mp4/.webm moved from audio to video in media_type.py; this stays a
# immutable superset re-export for older import sites.
SUPPORTED_EXTENSIONS = SUPPORTED_MEDIA_EXTENSIONS
