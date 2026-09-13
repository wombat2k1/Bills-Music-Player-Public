"""Audio metadata: the Track dataclass and tag/cover readers."""
import os
from dataclasses import dataclass
from typing import List, Optional, Dict, Any, Tuple

from mutagen import File as MutagenFile

from .media_type import MediaType, classify_path
from .mkv_tags import read_mkv_tags
from .textfix import clean_text

_MATROSKA_EXTENSIONS = (".mkv", ".webm")

# Folder names that are containers, not artists -- a video sitting
# directly inside one of these (rather than a proper Sorted\{Artist}\...
# subfolder) must stay "Unknown Artist" rather than getting a nonsense
# "artist" value like "Music Videos" itself.
_GENERIC_VIDEO_FOLDER_NAMES = frozenset({"music videos"})


def _artist_from_video_folder(path: str) -> str:
    """The immediate parent folder name, if it looks like a real artist
    folder rather than a generic container -- see read_track_meta()'s
    video branch for why this is trusted over an embedded tag."""
    folder_name = os.path.basename(os.path.dirname(path))
    if not folder_name or folder_name.casefold() in _GENERIC_VIDEO_FOLDER_NAMES:
        return ""
    return folder_name


def parse_track_number(value: str) -> int:
    if not value or value == "Unknown":
        return 0
    try:
        part = str(value).split("/")[0].strip()
        return int(part)
    except Exception:
        return 0


# Fields added to read_track_meta() after tracks may already have been
# cached without them -- a cached record missing any of these hasn't been
# backfilled yet. Used to make the one-time metadata backfill resumable:
# a record that already has these fields doesn't need re-reading again,
# even across an app restart that interrupted a previous backfill pass.
BACKFILLED_META_FIELDS = ("genre", "year", "bpm", "key")


def meta_needs_backfill(meta: Dict[str, Any]) -> bool:
    return any(field not in meta for field in BACKFILLED_META_FIELDS)


def read_track_meta(path: str) -> Dict[str, Any]:
    media_type = classify_path(path)
    if media_type == MediaType.KARAOKE:
        from .karaoke import karaoke_metadata
        return karaoke_metadata(path)
    if media_type == MediaType.VIDEO:
        # No embedded video metadata is required -- these are the concise,
        # explicitly-documented fallbacks for video records specifically.
        album = "Music Videos"
        title = os.path.splitext(os.path.basename(path))[0]
    else:
        album = "Unknown Album"
        title = os.path.basename(path)
    artist = "Unknown Artist"
    album_artist = "Unknown Artist"
    disc_no = 1
    track_no = 0
    has_embedded_synced_lyrics = False
    genre = "Unknown"
    year = "Unknown"
    bpm = "Unknown"
    key = "Unknown"
    try:
        audio = MutagenFile(path, easy=True)
    except Exception:
        audio = None
    if audio:
        album_val = tag_or(audio, "album")
        title_val = tag_or(audio, "title")
        artist_val = tag_or(audio, "artist")
        album_artist_val = tag_or(audio, "albumartist")
        disc_val = tag_or(audio, "discnumber")
        track_val = tag_or(audio, "tracknumber")
        if album_val and album_val != "Unknown":
            album = album_val
        if title_val and title_val != "Unknown":
            title = title_val
        if artist_val and artist_val != "Unknown":
            artist = artist_val
        if album_artist_val and album_artist_val != "Unknown":
            album_artist = album_artist_val
        disc_no = parse_track_number(disc_val) or 1
        track_no = parse_track_number(track_val)
        synced_keys = {
            "syncedlyrics", "synced lyrics", "lyrics-sync", "lyrics_sync",
            "sylt", "?lyr", "----:com.apple.itunes:syncedlyrics",
        }
        try:
            for key_name in audio.keys():
                if str(key_name).casefold() in synced_keys and audio.get(key_name):
                    has_embedded_synced_lyrics = True
                    break
        except Exception:
            pass
        genre_val = tag_list_or(audio, "genre")
        if genre_val and genre_val != "Unknown":
            genre = genre_val
        year_val = tag_or(audio, "date")
        if year_val and year_val != "Unknown":
            year = year_val
        bpm_val = tag_or(audio, "bpm")
        if not bpm_val or bpm_val == "Unknown":
            bpm_val = tag_or(audio, "tempo")
        if bpm_val and bpm_val != "Unknown":
            bpm = bpm_val.split(".")[0]
        key_val = tag_or(audio, "initialkey")
        if not key_val or key_val == "Unknown":
            key_val = tag_or(audio, "key")
        if not key_val or key_val == "Unknown":
            key_val = tag_or(audio, "musicalkey")
        if key_val and key_val != "Unknown":
            key = key_val
    elif os.path.splitext(path)[1].lower() in _MATROSKA_EXTENSIONS:
        # mutagen has no Matroska support at all -- MutagenFile() returns
        # None for every .mkv/.webm file regardless of what tags a
        # full-featured external tool wrote into them (confirmed against
        # a real video library: every .mkv showed "Unknown Artist"). Read
        # the same handful of fields directly from the file's own EBML
        # Tags/Info structure instead.
        mkv_tags = read_mkv_tags(path)
        if mkv_tags.get("album"):
            album = mkv_tags["album"]
        if mkv_tags.get("title"):
            title = mkv_tags["title"]
        if mkv_tags.get("artist"):
            artist = mkv_tags["artist"]
        if mkv_tags.get("album_artist"):
            album_artist = mkv_tags["album_artist"]
        if mkv_tags.get("genre"):
            genre = mkv_tags["genre"]
        if mkv_tags.get("date"):
            year = mkv_tags["date"]
    if media_type == MediaType.VIDEO:
        folder_artist = _artist_from_video_folder(path)
        if folder_artist:
            # Always wins for videos, deliberately overriding whatever
            # tag-based artist (if any) was found above: this library's
            # video files are organised as Sorted\{Artist}\{Artist} -
            # {Title}.ext, and embedded artist tags are either absent
            # (confirmed: 615 of 616 real .mkv files in this collection
            # have none at all) or, when present, already agree with the
            # folder they're filed under -- so there's nothing to lose by
            # trusting the folder and a real, common case (untagged
            # files) it fixes outright.
            artist = folder_artist
            prefix = f"{folder_artist} - "
            if title.casefold().startswith(prefix.casefold()):
                title = title[len(prefix):].strip() or title
    if bpm == "Unknown" or key == "Unknown":
        bpm, key = _read_raw_bpm_key_fallback(path, bpm, key)
    base, _ = os.path.splitext(path)
    has_lrc_sidecar = os.path.isfile(base + ".lrc")
    return {
        "path": path,
        "album": album,
        "title": title,
        "artist": artist,
        "album_artist": album_artist,
        "disc_no": disc_no,
        "track_no": track_no,
        "has_lrc_sidecar": has_lrc_sidecar,
        "has_embedded_synced_lyrics": has_embedded_synced_lyrics,
        "has_synced_lyrics": (
            has_lrc_sidecar or has_embedded_synced_lyrics
        ),
        "genre": genre,
        "year": year,
        "bpm": bpm,
        "key": key,
        "media_type": media_type.value,
    }


def read_full_tag_display(path: str) -> Dict[str, str]:
    """Full now-playing tag/format fields for the "Tags" info panel.

    Opens the file via mutagen -- real, sometimes slow (NAS-backed) file
    I/O -- so this must only ever be called off the GUI thread (see
    workers.TrackTagLoadWorker). PlayerWindow._activate_track_ui() used
    to call this inline via _read_tags() on every track change; confirmed
    via a captured stall trace to freeze the whole window, including the
    same _tick() loop that drives the Cast progress bar/equaliser, for as
    long as the read took.
    """
    try:
        audio = MutagenFile(path, easy=True)
    except Exception:
        audio = None

    result = {
        "title": "Unknown", "artist": "Unknown", "album": "Unknown",
        "genre": "Unknown", "bitrate": "Unknown", "sample_rate": "Unknown",
        "channels": "Unknown", "duration": "Unknown",
    }
    if not audio:
        return result

    result["title"] = tag_or(audio, "title")
    result["artist"] = tag_or(audio, "artist")
    result["album"] = tag_or(audio, "album")
    result["genre"] = tag_list_or(audio, "genre")
    info = audio.info
    if info:
        bitrate = getattr(info, "bitrate", 0) or 0
        if bitrate:
            result["bitrate"] = f"{int(bitrate / 1000)} kbps"
        sample_rate = getattr(info, "sample_rate", 0) or 0
        if sample_rate:
            result["sample_rate"] = f"{sample_rate} Hz"
        channels = getattr(info, "channels", None)
        if channels:
            result["channels"] = str(channels)
        length = getattr(info, "length", 0) or 0
        if length:
            total = int(length)
            minutes, seconds = divmod(total, 60)
            hours, minutes = divmod(minutes, 60)
            result["duration"] = (
                f"{hours}:{minutes:02d}:{seconds:02d}" if hours
                else f"{minutes}:{seconds:02d}"
            )
    return result


def tag_or(audio, key: str) -> str:
    val = audio.get(key)
    if not val:
        return "Unknown"
    if isinstance(val, list):
        return clean_text(val[0]) if val else "Unknown"
    return clean_text(str(val))


def tag_list_or(audio, key: str) -> str:
    """Like tag_or, but joins multi-valued tags (e.g. multiple genres)
    instead of keeping only the first value."""
    val = audio.get(key)
    if not val:
        return "Unknown"
    if isinstance(val, list):
        parts = [clean_text(v).strip() for v in val if clean_text(v).strip()]
        return ", ".join(parts) if parts else "Unknown"
    text = clean_text(val).strip()
    return text if text else "Unknown"


def _coerce_tag_text_values(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, bytes):
        return [value.decode("utf-8", errors="ignore")]
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            result.extend(_coerce_tag_text_values(item))
        return result
    text_attr = getattr(value, "text", None)
    if text_attr is not None and text_attr is not value:
        return _coerce_tag_text_values(text_attr)
    return [str(value)]


def _tag_values_for_keys(tags, keys: Tuple[str, ...]) -> List[str]:
    """Qt-free port of PlayerWindow._tag_values_for_keys (window.py) --
    duplicated rather than imported so metadata.py (used by the
    background scan worker) stays free of any PlayerWindow/Qt dependency."""
    if not tags:
        return []
    wanted = {str(key).casefold() for key in keys}
    values = []
    try:
        for key_name in keys:
            if hasattr(tags, "getall"):
                for value in tags.getall(key_name) or []:
                    values.extend(_coerce_tag_text_values(value))
            try:
                value = tags.get(key_name)
            except Exception:
                value = None
            values.extend(_coerce_tag_text_values(value))
        try:
            iterator = tags.items()
        except Exception:
            iterator = []
        for tag_key, value in iterator:
            if str(tag_key).casefold() in wanted:
                values.extend(_coerce_tag_text_values(value))
    except Exception:
        pass
    clean_values = []
    for value in values:
        text = clean_text(str(value)).strip()
        if text and text != "Unknown":
            clean_values.append(text)
    return clean_values


def _read_raw_bpm_key_fallback(path: str, bpm: str, key: str) -> Tuple[str, str]:
    """Second, non-easy tag parse for files whose bpm/key isn't exposed
    under mutagen's easy-mode key names -- mirrors PlayerWindow's
    _queue_track_details on-demand fallback (window.py), duplicated here
    for the same Qt-free reason as _tag_values_for_keys above."""
    try:
        full_audio = MutagenFile(path)
    except Exception:
        return bpm, key
    tags = getattr(full_audio, "tags", None) if full_audio else None
    if not tags:
        return bpm, key
    if key == "Unknown":
        raw_key = _tag_values_for_keys(tags, (
            "TKEY", "initialkey", "INITIALKEY", "initial key",
            "musicalkey", "----:com.apple.iTunes:initialkey",
        ))
        if raw_key:
            key = raw_key[0]
    if bpm == "Unknown":
        raw_bpm = _tag_values_for_keys(tags, (
            "TBPM", "bpm", "BPM", "tempo", "tmpo",
            "----:com.apple.iTunes:BPM",
        ))
        if raw_bpm:
            bpm = raw_bpm[0].split(".")[0]
    return bpm, key


def read_cover_bytes(path: str) -> Optional[bytes]:
    cover = None
    try:
        audio_full = MutagenFile(path)
    except Exception:
        audio_full = None
    if audio_full and cover is None:
        try:
            if hasattr(audio_full, "pictures") and audio_full.pictures:
                cover = audio_full.pictures[0].data
            elif audio_full.tags:
                if hasattr(audio_full.tags, "getall"):
                    apics = audio_full.tags.getall("APIC")
                    if apics:
                        cover = apics[0].data
                if cover is None and "covr" in audio_full.tags:
                    covr = audio_full.tags.get("covr")
                    if covr:
                        cover = bytes(covr[0])
        except Exception:
            cover = None
    if cover is None:
        try:
            folder = os.path.dirname(path)
            folders_to_check = [folder, os.path.dirname(folder)]
            preferred = (
                "cover.jpg", "cover.jpeg", "cover.png",
                "folder.jpg", "folder.jpeg", "folder.png",
                "front.jpg", "front.jpeg", "front.png",
                "albumart.jpg", "albumart.jpeg", "albumart.png",
                "back.jpg", "back.png",
            )
            for base in folders_to_check:
                try:
                    names = {name.casefold(): name for name in os.listdir(base)}
                except OSError:
                    names = {}
                for name in preferred:
                    actual = names.get(name.casefold())
                    candidate = os.path.join(base, actual) if actual else ""
                    if candidate and os.path.isfile(candidate):
                        with open(candidate, "rb") as f:
                            cover = f.read()
                        break
                if cover:
                    break
        except Exception:
            cover = None
    return cover


@dataclass
class Track:
    path: str
    title: str
    artist: str
    album: str
    genre: str
    bitrate: str
    sample_rate: str
    channels: str
    duration: str
