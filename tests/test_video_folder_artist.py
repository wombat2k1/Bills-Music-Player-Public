"""Video artist inference from the immediate parent folder name.

This library's video collection is organised as
Sorted\\{Artist}\\{Artist} - {Title}.ext, and a stress-test against the
real ~616-file .mkv collection found only 1 file with an embedded ARTIST
tag -- the artist lives in the folder structure, not the files. Per an
explicit decision to trust that folder structure, read_track_meta() now
derives a video's artist from its parent folder name and lets it win
over whatever (if anything) tag-reading found, rather than the reverse.
"""
from types import SimpleNamespace

from billsmusic import metadata


class _FakeEasyAudio:
    def __init__(self, data=None):
        self._data = data or {}

    def get(self, key):
        return self._data.get(key)

    def keys(self):
        return self._data.keys()


class _FakeFullAudio:
    def __init__(self, tags=None):
        self.tags = tags


def _no_lrc(monkeypatch):
    monkeypatch.setattr(metadata.os.path, "isfile", lambda path: False)


def _no_tags(monkeypatch):
    monkeypatch.setattr(metadata, "MutagenFile", lambda *a, **kw: None)
    monkeypatch.setattr(metadata, "read_mkv_tags", lambda path: {})


def test_artist_comes_from_the_immediate_parent_folder(monkeypatch):
    _no_tags(monkeypatch)
    _no_lrc(monkeypatch)

    result = metadata.read_track_meta(r"Y:\Nas Music Vids back up\Sorted\10cc\10cc - Dreadlock Holiday.mkv")

    assert result["artist"] == "10cc"


def test_folder_artist_wins_even_when_mutagen_found_a_different_one(monkeypatch):
    def fake_mutagen(path, easy=False, **kwargs):
        if easy:
            return _FakeEasyAudio({"artist": ["Tagged Artist"]})
        return _FakeFullAudio()

    monkeypatch.setattr(metadata, "MutagenFile", fake_mutagen)
    _no_lrc(monkeypatch)

    result = metadata.read_track_meta(r"Y:\Sorted\Folder Artist\clip.mp4")

    assert result["artist"] == "Folder Artist"


def test_redundant_artist_prefix_is_stripped_from_the_title(monkeypatch):
    _no_tags(monkeypatch)
    _no_lrc(monkeypatch)

    result = metadata.read_track_meta(r"Y:\Sorted\10cc\10cc - Dreadlock Holiday.mkv")

    assert result["title"] == "Dreadlock Holiday"


def test_title_left_alone_when_it_does_not_start_with_the_artist(monkeypatch):
    _no_tags(monkeypatch)
    _no_lrc(monkeypatch)

    result = metadata.read_track_meta(r"Y:\Sorted\10cc\Dreadlock Holiday (Official Video).mkv")

    assert result["title"] == "Dreadlock Holiday (Official Video)"
    assert result["artist"] == "10cc"


def test_generic_music_videos_folder_is_not_used_as_artist(monkeypatch):
    _no_tags(monkeypatch)
    _no_lrc(monkeypatch)

    result = metadata.read_track_meta(r"Y:\Sorted\Music Videos\10cc - Dreadlock Holiday.mkv")

    assert result["artist"] == "Unknown Artist"


def test_generic_folder_name_match_is_case_insensitive(monkeypatch):
    _no_tags(monkeypatch)
    _no_lrc(monkeypatch)

    result = metadata.read_track_meta(r"Y:\Sorted\MUSIC VIDEOS\clip.mkv")

    assert result["artist"] == "Unknown Artist"


def test_file_directly_at_drive_root_has_no_usable_parent_folder(monkeypatch):
    _no_tags(monkeypatch)
    _no_lrc(monkeypatch)

    result = metadata.read_track_meta(r"Y:\clip.mkv")

    assert result["artist"] == "Unknown Artist"


def test_audio_files_are_never_affected_by_video_folder_inference(monkeypatch):
    # A .flac file sitting inside a folder that happens to be named after
    # its own artist must not go through the video-only override path at
    # all -- it's already correct via the ordinary tag-reading logic, and
    # this proves the new code is properly gated on media_type == VIDEO.
    def fake_mutagen(path, easy=False, **kwargs):
        if easy:
            return _FakeEasyAudio({})  # no artist tag
        return _FakeFullAudio()

    monkeypatch.setattr(metadata, "MutagenFile", fake_mutagen)
    _no_lrc(monkeypatch)

    result = metadata.read_track_meta(r"Y:\Music\10cc\10cc - Dreadlock Holiday.flac")

    assert result["artist"] == "Unknown Artist"
    assert result["media_type"] == "audio"


def test_karaoke_files_are_never_affected_by_video_folder_inference(monkeypatch):
    # classify_path() must route .cdg/.zip to karaoke_metadata() before
    # read_track_meta() ever reaches the video-specific code at all.
    called = SimpleNamespace(count=0)

    def fake_karaoke_metadata(path):
        called.count += 1
        return {"path": path, "artist": "Unknown Artist", "media_type": "karaoke"}

    monkeypatch.setattr("billsmusic.karaoke.karaoke_metadata", fake_karaoke_metadata)

    result = metadata.read_track_meta(r"Y:\Sorted\10cc\10cc - Dreadlock Holiday.cdg")

    assert called.count == 1
    assert result["media_type"] == "karaoke"
