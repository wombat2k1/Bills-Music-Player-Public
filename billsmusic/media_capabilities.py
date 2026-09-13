"""Authoritative intended media routing for Bills Music Player.

Runtime decoder support is still verified by the selected playback backend.
To add a format, add one registry entry and explicitly choose its media kind,
scanner/playlist eligibility, analysis/ReplayGain eligibility, and backends,
then update the capability parity tests.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping


class MediaType(Enum):
    AUDIO = "audio"
    VIDEO = "video"
    KARAOKE = "karaoke"
    UNSUPPORTED = "unsupported"


class PlaybackBackend(Enum):
    BASS = "bass"
    MINIAUDIO = "miniaudio"
    VLC = "vlc"
    CAST = "cast"
    QT_VIDEO = "qt_video"
    KARAOKE = "karaoke"


@dataclass(frozen=True)
class MediaCapability:
    extension: str
    kind: MediaType
    library_scan: bool
    playlist_import: bool
    bpm_key_analysis: bool
    replaygain: bool
    playback_backends: frozenset[PlaybackBackend]


_AUDIO_BACKENDS = frozenset({
    PlaybackBackend.BASS, PlaybackBackend.MINIAUDIO, PlaybackBackend.VLC,
})


def _cap(
    extension: str,
    kind: MediaType,
    *,
    library_scan: bool,
    playlist_import: bool,
    bpm_key: bool = False,
    replaygain: bool = False,
    backends: frozenset[PlaybackBackend],
) -> MediaCapability:
    return MediaCapability(
        extension=extension,
        kind=kind,
        library_scan=library_scan,
        playlist_import=playlist_import,
        bpm_key_analysis=bpm_key,
        replaygain=replaygain,
        playback_backends=backends,
    )


_CAPABILITIES = (
    *(
        _cap(
            ext,
            MediaType.AUDIO,
            library_scan=True,
            playlist_import=True,
            bpm_key=True,
            replaygain=True,
            backends=(
                _AUDIO_BACKENDS | frozenset({PlaybackBackend.CAST})
                if ext in (".mp3", ".flac") else _AUDIO_BACKENDS
            ),
        )
        for ext in (
            ".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".wma",
            ".aiff", ".aif", ".opus", ".alac", ".mka", ".m4b", ".amr",
        )
    ),
    *(
        _cap(
            ext,
            MediaType.VIDEO,
            library_scan=True,
            playlist_import=True,
            # Video's audio track is genuinely decodable for BPM/Key
            # (see billsmusic/workers.py's QAudioDecoder-based
            # _decode_video_audio_preview) -- all video extensions share
            # the one QT_VIDEO backend, so eligibility is uniform rather
            # than hand-split per extension.
            bpm_key=True,
            backends=frozenset({PlaybackBackend.QT_VIDEO}),
        )
        for ext in (
            ".mp4", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".m4v",
            ".mpeg", ".mpg",
        )
    ),
    *(
        _cap(
            ext,
            MediaType.KARAOKE,
            library_scan=True,
            playlist_import=True,
            backends=frozenset({PlaybackBackend.KARAOKE}),
        )
        for ext in (".cdg", ".zip")
    ),
)

MEDIA_CAPABILITIES: Mapping[str, MediaCapability] = MappingProxyType({
    capability.extension: capability for capability in _CAPABILITIES
})
_SUPPORTED_EXTENSIONS = frozenset(MEDIA_CAPABILITIES)
_EXTENSIONS_BY_KIND = MappingProxyType({
    kind: frozenset(
        extension for extension, capability in MEDIA_CAPABILITIES.items()
        if capability.kind == kind
    )
    for kind in (MediaType.AUDIO, MediaType.VIDEO, MediaType.KARAOKE)
})
_EXTENSIONS_BY_BACKEND = MappingProxyType({
    backend: frozenset(
        extension for extension, capability in MEDIA_CAPABILITIES.items()
        if backend in capability.playback_backends
    )
    for backend in PlaybackBackend
})


def _normalise_extension(path_or_extension) -> str:
    if path_or_extension is None:
        return ""
    try:
        text = os.fspath(path_or_extension)
    except (TypeError, AttributeError):
        return ""
    if isinstance(text, bytes):
        try:
            text = os.fsdecode(text)
        except (TypeError, UnicodeError):
            return ""
    text = text.strip()
    if not text or text.endswith(("/", "\\")):
        return ""
    name = text.replace("\\", "/").rsplit("/", 1)[-1]
    if name.startswith(".") and name.count(".") == 1:
        extension = name
    else:
        extension = os.path.splitext(name)[1]
        if not extension and "." not in name:
            extension = "." + name
    return extension.casefold()


def get_media_capability(path_or_extension) -> MediaCapability | None:
    return MEDIA_CAPABILITIES.get(_normalise_extension(path_or_extension))


def is_supported_media(path_or_extension) -> bool:
    return get_media_capability(path_or_extension) is not None


def is_audio(path_or_extension) -> bool:
    capability = get_media_capability(path_or_extension)
    return capability is not None and capability.kind == MediaType.AUDIO


def is_video(path_or_extension) -> bool:
    capability = get_media_capability(path_or_extension)
    return capability is not None and capability.kind == MediaType.VIDEO


def is_library_scannable(path_or_extension) -> bool:
    capability = get_media_capability(path_or_extension)
    return bool(capability and capability.library_scan)


def is_playlist_supported(path_or_extension) -> bool:
    capability = get_media_capability(path_or_extension)
    return bool(capability and capability.playlist_import)


def is_bpm_key_eligible(path_or_extension) -> bool:
    capability = get_media_capability(path_or_extension)
    return bool(capability and capability.bpm_key_analysis)


def is_replaygain_eligible(path_or_extension) -> bool:
    capability = get_media_capability(path_or_extension)
    return bool(capability and capability.replaygain)


def supported_extensions() -> frozenset[str]:
    return _SUPPORTED_EXTENSIONS


def extensions_for_kind(kind: MediaType) -> frozenset[str]:
    return _EXTENSIONS_BY_KIND.get(kind, frozenset())


def audio_extensions() -> frozenset[str]:
    return extensions_for_kind(MediaType.AUDIO)


def video_extensions() -> frozenset[str]:
    return extensions_for_kind(MediaType.VIDEO)


def backend_supported_extensions(backend: PlaybackBackend | str) -> frozenset[str]:
    if not isinstance(backend, PlaybackBackend):
        try:
            backend = PlaybackBackend(str(backend).casefold())
        except ValueError:
            return frozenset()
    return _EXTENSIONS_BY_BACKEND[backend]


def _wildcards(extensions: frozenset[str]) -> str:
    return " ".join(f"*{extension}" for extension in sorted(extensions))


AUDIO_FILE_FILTER = f"Audio files ({_wildcards(audio_extensions())})"
VIDEO_FILE_FILTER = f"Video files ({_wildcards(video_extensions())})"
MEDIA_FILE_FILTER = f"Media files ({_wildcards(supported_extensions())})"


def validate_registry() -> tuple[str, ...]:
    errors = []
    if len(MEDIA_CAPABILITIES) != len(_CAPABILITIES):
        errors.append("duplicate extensions")
    for extension, capability in MEDIA_CAPABILITIES.items():
        if not extension.startswith("."):
            errors.append(f"missing dot: {extension}")
        if extension != extension.lower():
            errors.append(f"not lowercase: {extension}")
        if capability.extension != extension:
            errors.append(f"key mismatch: {extension}")
        if capability.kind == MediaType.UNSUPPORTED:
            errors.append(f"unsupported registry entry: {extension}")
        if capability.replaygain and capability.kind != MediaType.AUDIO:
            errors.append(f"non-audio ReplayGain enabled: {extension}")
        if any(not isinstance(backend, PlaybackBackend) for backend in capability.playback_backends):
            errors.append(f"invalid backend: {extension}")
        if not capability.playback_backends:
            errors.append(f"no backend: {extension}")
    return tuple(errors)
