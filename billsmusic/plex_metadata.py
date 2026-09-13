"""Converts raw Plex `/library/sections/<key>/all` JSON items into the
exact shapes the EXISTING local-library infrastructure already expects --
the meta_list dict shape consumed by _apply_meta_list_to_library_tabs/
_set_tracks_from_meta/build_search_index, and the queue_detail_cache
{"time","bitrate","key","bpm"} shape -- so that machinery can be reused
for Plex-sourced content with no parallel tree/display pipeline. This is
the "natural, already-decoupled seam" identified during the Stage 1
architecture investigation.

No `requests` dependency -- pure data transformation, safe to import
anywhere (including window.py at module level).

Plex's own reported metadata type is authoritative for AUDIO/VIDEO
classification here -- media_type is set directly from which conversion
function is called (plex_track_to_meta_dict -> AUDIO,
plex_video_to_meta_dict -> VIDEO), never inferred from the synthetic
plex:// identity's extension.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .media_capabilities import MediaType
from .plex_identity import make_plex_identity


def format_plex_duration(duration_ms: Any) -> str:
    try:
        total_seconds = int(duration_ms) // 1000
    except (TypeError, ValueError):
        return "--"
    if total_seconds <= 0:
        return "--"
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _plex_int(value: Any, default: int = 0) -> int:
    """Plex's index/parentIndex are already JSON integers in practice,
    but this stays defensive against a missing/non-numeric value (an
    unnumbered single/loose track) rather than letting a track/disc
    number crash the whole conversion -- graceful fallback, matching
    _parse_track_number's own local-scan behaviour."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def extract_media_info(item: Dict[str, Any]) -> Dict[str, Any]:
    """First Media/Part entry's container/codec/bitrate/part key. Used
    for Stage 2 display metadata (bitrate) and, as of Stage 3A, for
    Direct Play resolution (see resolve_direct_play_capability and
    workers.py's PlexPlaybackResolveWorker) -- fetching this never
    requires a second request (Plex's own bulk /all listing, and a
    single-item /library/metadata/<ratingKey> fetch, both already embed
    Media/Part inline per item)."""
    media_list = item.get("Media")
    if not isinstance(media_list, list) or not media_list:
        return {"container": "", "video_codec": "", "audio_codec": "", "bitrate": 0, "part_key": ""}
    media = media_list[0] if isinstance(media_list[0], dict) else {}
    parts = media.get("Part")
    part = parts[0] if isinstance(parts, list) and parts and isinstance(parts[0], dict) else {}
    return {
        "container": media.get("container", "") or "",
        "video_codec": media.get("videoCodec", "") or "",
        "audio_codec": media.get("audioCodec", "") or "",
        "bitrate": media.get("bitrate", 0) or 0,
        "part_key": part.get("key", "") or "",
    }


def _guess_extension(item: Dict[str, Any], default: str) -> str:
    media_list = item.get("Media")
    if isinstance(media_list, list) and media_list and isinstance(media_list[0], dict):
        container = media_list[0].get("container")
        if container:
            return f".{container}"
    return default


def plex_track_to_meta_dict(item: Dict[str, Any], server_config_id: str) -> Optional[Dict[str, Any]]:
    """One Plex "track" item (type=10, from a music library's bulk
    /all?type=10 listing -- already includes parentTitle/grandparentTitle
    inline, no per-track album/artist request needed) -> a meta_list-
    shaped dict, matching what _apply_meta_list_to_library_tabs/
    build_search_index already expect from a local scan record."""
    rating_key = str(item.get("ratingKey") or "")
    if not rating_key:
        return None
    path = make_plex_identity(server_config_id, rating_key, _guess_extension(item, ".mp3"))
    media_info = extract_media_info(item)
    return {
        "path": path,
        "media_type": MediaType.AUDIO.value,
        "title": item.get("title") or "Unknown Title",
        # Plex sets a track's own originalTitle as the track-artist
        # override specifically for compilation/various-artists albums
        # (the album's own grandparentTitle stays the shared album
        # artist) -- preferring it here is what stops every track on a
        # "Various Artists" compilation from displaying as "Various
        # Artists" when Plex actually knows the real performer.
        "artist": item.get("originalTitle") or item.get("grandparentTitle") or "Unknown Artist",
        "album": item.get("parentTitle") or "Unknown Album",
        "album_artist": item.get("grandparentTitle") or "",
        "genre": (item.get("Genre") or [{}])[0].get("tag", "") if item.get("Genre") else "",
        "year": item.get("parentYear") or item.get("year") or "",
        # disc_no/track_no (not track_number/disc_number) -- these exact
        # keys are what group_library_albums/_track_display_label already
        # read for every Local track (see window.py's _read_album_title_
        # track); a mismatched key here silently defaults every Plex
        # track to disc_no=1/track_no=0 downstream, indistinguishable
        # from "no numbering", which sorted every album alphabetically by
        # title instead of by disc/track order.
        "track_no": _plex_int(item.get("index")),
        "disc_no": _plex_int(item.get("parentIndex"), default=1),
        "duration_ms": item.get("duration", 0) or 0,
        "rating_key": rating_key,
        "parent_rating_key": str(item.get("parentRatingKey") or ""),
        "grandparent_rating_key": str(item.get("grandparentRatingKey") or ""),
        "thumb": item.get("thumb") or item.get("parentThumb") or "",
        "updated_at": item.get("updatedAt", 0) or 0,
        "plex_key": item.get("plex_key", ""),  # populated only if a future custom field maps one
        "plex_bpm": item.get("plex_bpm", ""),
        **media_info,
    }


def _plex_video_artist(item: Dict[str, Any]) -> Optional[str]:
    """grandparentTitle (the "Show"-equivalent parent for a music-video-
    shaped library, exactly how Plex's TV-like agents represent an artist
    above album/video) first, then an explicit Artist tag list if the
    library's agent supplies one instead. Deliberately does NOT fall back
    to originalTitle (that's the video's own alternate title, not an
    artist -- using it here would mislabel/miscategorise the video) and
    does NOT invent anything from the filename. Returns None (not a
    sentinel string) when nothing usable is found -- see
    _plex_video_alphabetical_group, the caller's fallback for that case."""
    artist = item.get("grandparentTitle")
    if artist:
        return artist
    tags = item.get("Artist")
    if isinstance(tags, list) and tags and isinstance(tags[0], dict):
        tag = tags[0].get("tag")
        if tag:
            return tag
    return None


def _plex_video_alphabetical_group(title: str) -> str:
    """Fallback grouping key for a video with NO usable Plex artist
    metadata at all -- a real, observed case: an entire "Other Videos"
    library where every item lacks grandparentTitle/Artist tags, which
    previously meant every single video landed in one shared "Unknown
    Artist" bucket (1,984 videos, 1 visible row). Groups by the title's
    own first letter (A-Z, or "#" for anything else) instead, giving
    "A -> Video", "B -> Video", etc. Deliberately does NOT parse the
    filename/path or attempt any other artist inference -- title is
    already a legitimate Plex-supplied field, not something invented,
    and any further filename-based extraction is explicitly deferred
    pending review, not built here."""
    first = (title or "").strip()[:1].upper()
    return first if first.isalpha() else "#"


def plex_video_to_meta_dict(item: Dict[str, Any], server_config_id: str) -> Optional[Dict[str, Any]]:
    """One Plex video item (music-video-shaped library -- title/artist/
    year/duration/thumb, no assumption of movie-style Director/Studio
    metadata) -> a meta_list-shaped dict.

    `album` is left "" (not "Unknown Album") when Plex genuinely has no
    parentTitle for this item -- group_library_albums/_tree_build_tick
    treat an empty album as "no album level", grouping the video directly
    under its artist ("Artist -> Video") rather than fabricating a fake
    shared "Unknown Album" bucket that would misleadingly imply every
    unattributed video belongs to the same one album. When Plex *does*
    supply a real parentTitle (e.g. a genuinely album-organised music-
    video library), that value is used as-is and naturally produces
    "Artist -> Album -> Video" through the same existing grouping."""
    rating_key = str(item.get("ratingKey") or "")
    if not rating_key:
        return None
    path = make_plex_identity(server_config_id, rating_key, _guess_extension(item, ".mp4"))
    media_info = extract_media_info(item)
    title = item.get("title") or "Unknown Title"
    artist = _plex_video_artist(item)
    if artist is None:
        artist = _plex_video_alphabetical_group(title)
    return {
        "path": path,
        "media_type": MediaType.VIDEO.value,
        "title": title,
        "artist": artist,
        "album": item.get("parentTitle") or "",
        "album_artist": "",
        "genre": "",
        "year": item.get("year", ""),
        "duration_ms": item.get("duration", 0) or 0,
        "rating_key": rating_key,
        "parent_rating_key": str(item.get("parentRatingKey") or ""),
        "grandparent_rating_key": str(item.get("grandparentRatingKey") or ""),
        "thumb": item.get("thumb") or "",
        "updated_at": item.get("updatedAt", 0) or 0,
        "plex_key": "",
        "plex_bpm": "",
        **media_info,
    }


def plex_karaoke_to_meta_dict(item: Dict[str, Any], server_config_id: str) -> Optional[Dict[str, Any]]:
    """Stage 2 scope: browsing/metadata/queue identity only for a mapped
    Karaoke library -- no CDG/ZIP-specific remote playback classification
    attempted yet. If the raw item doesn't carry enough to classify later
    (no Media/Part entry at all), the metadata is still retained (never
    dropped), just with an empty part_key -- Stage 3 will need to decide
    what "usable karaoke" means for a Plex-sourced item; that limitation
    is reported, not silently hidden."""
    rating_key = str(item.get("ratingKey") or "")
    if not rating_key:
        return None
    path = make_plex_identity(server_config_id, rating_key, _guess_extension(item, ".mp4"))
    media_info = extract_media_info(item)
    return {
        "path": path,
        "media_type": MediaType.KARAOKE.value,
        "title": item.get("title") or "Unknown Title",
        "artist": item.get("grandparentTitle") or item.get("originalTitle") or "",
        "album": item.get("parentTitle") or "",
        "genre": "",
        "year": item.get("year", ""),
        "duration_ms": item.get("duration", 0) or 0,
        "rating_key": rating_key,
        "thumb": item.get("thumb") or "",
        "updated_at": item.get("updatedAt", 0) or 0,
        "karaoke_classification_limited": not bool(media_info.get("part_key")),
        **media_info,
    }


def plex_meta_to_queue_details(meta: Dict[str, Any]) -> Dict[str, str]:
    """queue_detail_cache-shaped {"time","bitrate","key","bpm"}. BPM/Key
    are populated ONLY if the Plex item's own metadata genuinely supplied
    them (plex_key/plex_bpm, only ever set by plex_track_to_meta_dict if
    a future custom-field mapping exists -- Plex has no first-class BPM/
    Key field by default, so these are "--" in practice today); this
    function never triggers or implies any decode/analysis."""
    return {
        "time": format_plex_duration(meta.get("duration_ms")),
        "bitrate": f"{int(meta['bitrate'])}k" if meta.get("bitrate") else "--",
        "key": meta.get("plex_key") or "--",
        "bpm": meta.get("plex_bpm") or "--",
    }


def plex_meta_list_to_queue_detail_cache(meta_list: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """Bulk helper: {path: queue_detail_cache-shaped dict} for every item
    in one fetched meta_list, so a caller can update
    self.queue_detail_cache with one dict.update() rather than one entry
    at a time."""
    return {
        meta["path"]: plex_meta_to_queue_details(meta)
        for meta in meta_list if meta.get("path")
    }


# -- Stage 3A: Direct Play capability -----------------------------------
# Deliberately NOT "any container this app's Local extension registry
# claims to support" for AUDIO -- that registry describes LOCAL
# BASS_StreamCreateFile support, which a real-device audit proved does
# NOT automatically transfer to BASS_StreamCreateURL (see
# tests/test_bass_url_streaming.py and the Stage 3A format-audit report).
# Only the containers actually proven over a real HTTP stream are
# claimed here. VIDEO is different: QMediaPlayer/FFmpeg decode the exact
# same way regardless of whether the demuxer's data comes from a local
# file or an HTTP stream -- the transport layer doesn't change codec
# support -- so the existing, already-trusted Local video extension
# registry is reused directly rather than duplicated.
_DIRECT_PLAY_AUDIO_CONTAINERS = frozenset({"mp3", "flac", "wav", "wave", "ogg"})


def _direct_play_video_containers() -> frozenset:
    from .media_capabilities import MediaType as _MediaType, extensions_for_kind
    return frozenset(ext.lstrip(".") for ext in extensions_for_kind(_MediaType.VIDEO))


def resolve_direct_play_capability(
    media_info: Dict[str, Any], media_kind: str,
) -> Dict[str, Any]:
    """Decides whether this item's own Media/Part (already extracted by
    extract_media_info) can be Direct Played by this app's existing
    backends -- Stage 3A is Direct Play only, never a transcode session.
    Returns {"can_direct_play": bool, "reason": str} -- `reason` is only
    meaningful when can_direct_play is False, and is always one of a
    small set of safe, technical strings (container/codec names only,
    never anything server- or path-derived) suitable for direct display
    per the explicit requirement: "This Plex item cannot currently be
    Direct Played" + container/audio codec/video codec."""
    container = (media_info.get("container") or "").strip().lower()
    if not container:
        return {"can_direct_play": False, "reason": "No media information available"}
    if not media_info.get("part_key"):
        return {"can_direct_play": False, "reason": "No playable file for this item"}
    if media_kind == "music":
        if container in _DIRECT_PLAY_AUDIO_CONTAINERS:
            return {"can_direct_play": True, "reason": ""}
        return {
            "can_direct_play": False,
            "reason": f"Unsupported audio container for Direct Play: {container}",
        }
    if media_kind == "video":
        if container in _direct_play_video_containers():
            return {"can_direct_play": True, "reason": ""}
        return {
            "can_direct_play": False,
            "reason": f"Unsupported video container for Direct Play: {container}",
        }
    return {"can_direct_play": False, "reason": f"Direct Play not supported for {media_kind}"}
