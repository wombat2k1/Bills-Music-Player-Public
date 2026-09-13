from billsmusic.media_type import (
    AUDIO_EXTENSIONS,
    KARAOKE_EXTENSIONS,
    MediaType,
    SUPPORTED_MEDIA_EXTENSIONS,
    VIDEO_EXTENSIONS,
    classify_extension,
    classify_path,
)


def test_all_documented_audio_extensions_classify_as_audio():
    audio_exts = [
        ".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".wma", ".aiff",
        ".aif", ".opus", ".alac", ".mka", ".m4b", ".amr",
    ]
    for ext in audio_exts:
        assert classify_extension(ext) == MediaType.AUDIO, ext
        assert ext in AUDIO_EXTENSIONS


def test_all_documented_video_extensions_classify_as_video():
    video_exts = [
        ".mp4", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".m4v", ".mpeg",
        ".mpg",
    ]
    for ext in video_exts:
        assert classify_extension(ext) == MediaType.VIDEO, ext
        assert ext in VIDEO_EXTENSIONS


def test_mp4_and_webm_moved_from_audio_to_video():
    # These used to be treated as ordinary audio candidates; the whole
    # point of this migration is that they must now classify as video.
    assert classify_extension(".mp4") == MediaType.VIDEO
    assert classify_extension(".webm") == MediaType.VIDEO


def test_m4b_and_mka_remain_audio():
    assert classify_extension(".m4b") == MediaType.AUDIO
    assert classify_extension(".mka") == MediaType.AUDIO


def test_unsupported_extension_classifies_as_unsupported():
    assert classify_extension(".txt") == MediaType.UNSUPPORTED
    assert classify_extension(".exe") == MediaType.UNSUPPORTED


def test_classify_extension_is_case_insensitive_and_tolerates_missing_dot():
    assert classify_extension(".MP4") == MediaType.VIDEO
    assert classify_extension("MP3") == MediaType.AUDIO


def test_classify_path_uses_extension_only():
    assert classify_path("C:/Movies/My Video.MP4") == MediaType.VIDEO
    assert classify_path("C:/Music/Song.flac") == MediaType.AUDIO
    assert classify_path("C:/Random/file.docx") == MediaType.UNSUPPORTED
    assert classify_path("") == MediaType.UNSUPPORTED
    assert classify_path(None) == MediaType.UNSUPPORTED


def test_supported_media_extensions_is_the_union():
    assert SUPPORTED_MEDIA_EXTENSIONS == (
        AUDIO_EXTENSIONS | VIDEO_EXTENSIONS | KARAOKE_EXTENSIONS
    )
    assert AUDIO_EXTENSIONS.isdisjoint(VIDEO_EXTENSIONS)
