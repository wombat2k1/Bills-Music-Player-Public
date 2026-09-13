"""Compatibility facade for the authoritative media capability registry."""
from __future__ import annotations

from .media_capabilities import (
    MediaType,
    audio_extensions,
    extensions_for_kind,
    get_media_capability,
    supported_extensions,
    video_extensions,
)

AUDIO_EXTENSIONS = audio_extensions()
VIDEO_EXTENSIONS = video_extensions()
KARAOKE_EXTENSIONS = extensions_for_kind(MediaType.KARAOKE)
SUPPORTED_MEDIA_EXTENSIONS = supported_extensions()


def classify_extension(ext: str) -> MediaType:
    capability = get_media_capability(ext)
    return capability.kind if capability else MediaType.UNSUPPORTED


def classify_path(path: str) -> MediaType:
    capability = get_media_capability(path)
    return capability.kind if capability else MediaType.UNSUPPORTED
