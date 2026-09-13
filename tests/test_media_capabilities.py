import os
import time
from pathlib import Path

import pytest

from billsmusic.media_capabilities import (
    AUDIO_FILE_FILTER,
    MEDIA_CAPABILITIES,
    VIDEO_FILE_FILTER,
    MediaType,
    PlaybackBackend,
    audio_extensions,
    backend_supported_extensions,
    get_media_capability,
    is_audio,
    is_bpm_key_eligible,
    is_library_scannable,
    is_playlist_supported,
    is_replaygain_eligible,
    is_supported_media,
    is_video,
    supported_extensions,
    validate_registry,
    video_extensions,
)
from billsmusic.media_server import AUDIO_TYPES


AUDIO = (
    ".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".wma",
    ".aiff", ".aif", ".opus", ".alac", ".mka", ".m4b", ".amr",
)
VIDEO = (
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".m4v",
    ".mpeg", ".mpg",
)
KARAOKE = (".cdg", ".zip")
AUDIO_BACKENDS = frozenset({
    PlaybackBackend.BASS, PlaybackBackend.MINIAUDIO, PlaybackBackend.VLC,
})


def _audio_backends(extension):
    return (
        AUDIO_BACKENDS | frozenset({PlaybackBackend.CAST})
        if extension in (".mp3", ".flac") else AUDIO_BACKENDS
    )


@pytest.mark.parametrize(
    "extension,kind,bpm_key,replaygain,backends",
    [
        *((ext, MediaType.AUDIO, True, True, _audio_backends(ext)) for ext in AUDIO),
        *((ext, MediaType.VIDEO, True, False, frozenset({PlaybackBackend.QT_VIDEO})) for ext in VIDEO),
        *((ext, MediaType.KARAOKE, False, False, frozenset({PlaybackBackend.KARAOKE})) for ext in KARAOKE),
    ],
)
def test_complete_current_format_capability_contract(
    extension, kind, bpm_key, replaygain, backends,
):
    capability = get_media_capability(extension)
    assert capability is not None
    assert capability.extension == extension
    assert capability.kind == kind
    assert capability.library_scan is True
    assert capability.playlist_import is True
    assert capability.bpm_key_analysis is bpm_key
    assert capability.replaygain is replaygain
    assert capability.playback_backends == backends


def test_registry_is_canonical_valid_and_immutable():
    assert validate_registry() == ()
    assert len(MEDIA_CAPABILITIES) == len(AUDIO) + len(VIDEO) + len(KARAOKE)
    assert all(ext.startswith(".") and ext == ext.lower() for ext in MEDIA_CAPABILITIES)
    with pytest.raises(TypeError):
        MEDIA_CAPABILITIES[".new"] = get_media_capability(".mp3")


@pytest.mark.parametrize(
    "value,expected",
    [
        ("MP3", ".mp3"),
        (".MP3", ".mp3"),
        ("Track.MP3", ".mp3"),
        (Path("C:/Music/Track.FLAC"), ".flac"),
    ],
)
def test_lookup_normalises_extensions_filenames_and_paths(value, expected):
    assert get_media_capability(value).extension == expected


@pytest.mark.parametrize("value", [None, "", "README", "file.unknown", "folder/"])
def test_unknown_and_malformed_inputs_are_safely_unsupported(value):
    assert get_media_capability(value) is None
    assert not is_supported_media(value)


def test_audio_video_sets_and_helpers_are_consistent():
    assert audio_extensions() == frozenset(AUDIO)
    assert video_extensions() == frozenset(VIDEO)
    assert audio_extensions().isdisjoint(video_extensions())
    assert supported_extensions() == frozenset((*AUDIO, *VIDEO, *KARAOKE))
    assert is_audio("song.opus") and not is_video("song.opus")
    assert is_video("clip.webm") and not is_audio("clip.webm")


def test_capability_helpers_preserve_current_routing():
    assert is_library_scannable("song.m4b")
    assert is_playlist_supported("clip.mkv")
    assert is_bpm_key_eligible("song.amr")
    assert is_replaygain_eligible("song.flac")
    # Video's audio track is genuinely decodable for BPM/Key (via
    # QAudioDecoder -- see workers.py's _decode_video_audio_preview);
    # only KARAOKE (.cdg/.zip, no independent audio stream of its own)
    # stays permanently excluded.
    assert is_bpm_key_eligible("clip.mp4")
    assert not is_bpm_key_eligible("song.cdg")
    assert not is_replaygain_eligible("show.cdg")
    assert backend_supported_extensions("bass") == frozenset(AUDIO)
    assert backend_supported_extensions("cast") == frozenset({".mp3", ".flac"})
    assert frozenset(AUDIO_TYPES) == backend_supported_extensions("cast")
    assert backend_supported_extensions(PlaybackBackend.QT_VIDEO) == frozenset(VIDEO)
    assert backend_supported_extensions("not-a-backend") == frozenset()


def test_generated_dialog_filters_have_each_wildcard_once():
    audio_wildcards = AUDIO_FILE_FILTER.removeprefix("Audio files (").removesuffix(")").split()
    video_wildcards = VIDEO_FILE_FILTER.removeprefix("Video files (").removesuffix(")").split()
    for extension in AUDIO:
        assert audio_wildcards.count(f"*{extension}") == 1
    for extension in VIDEO:
        assert video_wildcards.count(f"*{extension}") == 1


def test_twenty_thousand_registry_lookups_are_a_trivial_pure_hot_path(monkeypatch):
    monkeypatch.setattr(os, "stat", lambda *_args, **_kwargs: pytest.fail("no filesystem access"))
    started = time.perf_counter()
    for index in range(20_000):
        capability = get_media_capability(f"C:/Library/Track {index}.MP3")
        assert capability is not None
    assert time.perf_counter() - started < 2.0
