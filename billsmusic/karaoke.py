"""Secure MP3+CDG discovery and preparation using stable source identities."""
from __future__ import annotations

import hashlib
import os
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable, Dict, Optional, Tuple

from mutagen import File as MutagenFile

from .config import karaoke_cache_dir

MAX_ZIP_FILES = 512
MAX_ZIP_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_ZIP_MEMBER_BYTES = 768 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 250.0


class KaraokeError(RuntimeError):
    pass


@dataclass(frozen=True)
class KaraokePair:
    source_path: str
    source_type: str
    audio_path: Optional[str]
    cdg_path: Optional[str]
    audio_member: Optional[str] = None
    cdg_member: Optional[str] = None
    title: str = ""
    status: str = "valid"


def _loose_companion(cdg_path: str) -> Optional[str]:
    folder = os.path.dirname(cdg_path) or "."
    wanted = os.path.splitext(os.path.basename(cdg_path))[0].casefold()
    try:
        for name in os.listdir(folder):
            stem, ext = os.path.splitext(name)
            if ext.casefold() == ".mp3" and stem.casefold() == wanted:
                return os.path.join(folder, name)
    except OSError:
        return None
    return None


def resolve_loose_pair(cdg_path: str) -> KaraokePair:
    audio = _loose_companion(cdg_path)
    title = os.path.splitext(os.path.basename(cdg_path))[0]
    if not audio:
        raise KaraokeError("No matching MP3 was found for this CDG file")
    return KaraokePair(cdg_path, "loose", audio, cdg_path, title=title)


def _safe_member(info: zipfile.ZipInfo) -> bool:
    name = info.filename.replace("\\", "/")
    path = PurePosixPath(name)
    if not name or name.startswith(("/", "\\")) or path.is_absolute():
        return False
    if any(part in ("", ".", "..") for part in path.parts):
        return False
    if re.match(r"^[A-Za-z]:", name):
        return False
    mode = (info.external_attr >> 16) & 0xFFFF
    if mode and stat.S_ISLNK(mode):
        return False
    return True


def inspect_karaoke_zip(path: str) -> KaraokePair:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            if len(infos) > MAX_ZIP_FILES:
                raise KaraokeError("Archive contains too many files")
            total = 0
            stems: Dict[Tuple[str, str], Dict[str, str]] = {}
            for info in infos:
                if not _safe_member(info):
                    raise KaraokeError("Archive contains an unsafe path or symbolic link")
                if info.flag_bits & 0x1:
                    raise KaraokeError("Password-protected karaoke archives are not supported")
                if info.file_size > MAX_ZIP_MEMBER_BYTES:
                    raise KaraokeError("Archive member is unreasonably large")
                total += info.file_size
                if total > MAX_ZIP_EXPANDED_BYTES:
                    raise KaraokeError("Archive expands beyond the safe size limit")
                if info.file_size and info.compress_size == 0:
                    raise KaraokeError("Archive has an unsafe compression ratio")
                ratio = info.file_size / max(1, info.compress_size)
                if ratio > MAX_ZIP_COMPRESSION_RATIO:
                    raise KaraokeError("Archive has an unsafe compression ratio")
                member = PurePosixPath(info.filename.replace("\\", "/"))
                ext = member.suffix.casefold()
                if ext not in (".mp3", ".cdg"):
                    continue
                key = (str(member.parent).casefold(), member.stem.casefold())
                stems.setdefault(key, {})[ext] = info.filename
            pairs = [value for value in stems.values() if ".mp3" in value and ".cdg" in value]
            if len(pairs) != 1:
                if not pairs:
                    raise KaraokeError("Archive does not contain a matching MP3+CDG pair")
                raise KaraokeError("Archive contains multiple ambiguous MP3+CDG pairs")
            selected = pairs[0]
            return KaraokePair(
                path, "zip", None, None,
                audio_member=selected[".mp3"], cdg_member=selected[".cdg"],
                title=PurePosixPath(selected[".mp3"]).stem,
            )
    except KaraokeError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as ex:
        raise KaraokeError(f"Invalid karaoke archive: {ex}") from ex


def _zip_cache_identity(path: str) -> str:
    source = os.path.abspath(path)
    info = os.stat(source)
    raw = f"{source.casefold()}\0{info.st_mtime_ns}\0{info.st_size}".encode("utf-8", "ignore")
    return hashlib.sha256(raw).hexdigest()


def prepare_karaoke_source(
    path: str, should_cancel: Optional[Callable[[], bool]] = None,
) -> KaraokePair:
    ext = os.path.splitext(path)[1].casefold()
    if ext == ".cdg":
        return resolve_loose_pair(path)
    if ext != ".zip":
        raise KaraokeError("Unsupported karaoke source")
    pair = inspect_karaoke_zip(path)
    if should_cancel and should_cancel():
        raise KaraokeError("Karaoke preparation was cancelled")
    destination = os.path.join(karaoke_cache_dir(), _zip_cache_identity(path))
    audio_path = os.path.join(destination, "audio.mp3")
    cdg_path = os.path.join(destination, "graphics.cdg")
    if os.path.isfile(audio_path) and os.path.isfile(cdg_path):
        return KaraokePair(path, "zip", audio_path, cdg_path, pair.audio_member, pair.cdg_member, pair.title)
    os.makedirs(destination, exist_ok=True)
    try:
        with zipfile.ZipFile(path) as archive:
            for member, target in ((pair.audio_member, audio_path), (pair.cdg_member, cdg_path)):
                assert member is not None
                info = archive.getinfo(member)
                with archive.open(info) as source, open(target + ".part", "wb") as output:
                    while True:
                        if should_cancel and should_cancel():
                            raise KaraokeError("Karaoke preparation was cancelled")
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                if os.path.getsize(target + ".part") != info.file_size:
                    raise KaraokeError("Archive extraction was incomplete")
                os.replace(target + ".part", target)
    except Exception as ex:
        for candidate in (audio_path, cdg_path, audio_path + ".part", cdg_path + ".part"):
            try:
                os.remove(candidate)
            except OSError:
                pass
        if isinstance(ex, KaraokeError):
            raise
        raise KaraokeError(f"Could not prepare karaoke archive: {ex}") from ex
    return KaraokePair(path, "zip", audio_path, cdg_path, pair.audio_member, pair.cdg_member, pair.title)


def karaoke_metadata(path: str) -> dict:
    try:
        pair = resolve_loose_pair(path) if path.casefold().endswith(".cdg") else inspect_karaoke_zip(path)
    except KaraokeError as ex:
        return {
            "path": path, "title": os.path.splitext(os.path.basename(path))[0],
            "artist": "Unknown Artist", "album": "Karaoke", "genre": "Karaoke",
            "media_type": "karaoke", "karaoke_source_type": "zip" if path.casefold().endswith(".zip") else "loose",
            "karaoke_validation_state": "incomplete", "karaoke_error": str(ex),
        }
    audio = pair.audio_path
    title, artist, album, duration = pair.title, "Unknown Artist", "Karaoke", None
    if audio:
        try:
            parsed = MutagenFile(audio, easy=True)
            if parsed:
                title = (parsed.get("title") or [title])[0]
                artist = (parsed.get("artist") or [artist])[0]
                album = (parsed.get("album") or [album])[0]
                duration = float(getattr(getattr(parsed, "info", None), "length", 0) or 0)
        except Exception:
            pass
    return {
        "path": path, "title": title, "artist": artist, "album": album,
        "album_artist": artist, "genre": "Karaoke", "disc_no": 1, "track_no": 0,
        "media_type": "karaoke", "karaoke_source_type": pair.source_type,
        "audio_companion_path": pair.audio_path if pair.source_type == "loose" else None,
        "karaoke_validation_state": "valid", "duration_seconds": duration,
        "has_lrc_sidecar": False, "has_embedded_synced_lyrics": False,
        "has_synced_lyrics": False, "year": "Unknown", "bpm": "Unknown", "key": "Unknown",
    }
