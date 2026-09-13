import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from billsmusic.karaoke import (
    KaraokeError,
    inspect_karaoke_zip,
    karaoke_metadata,
    prepare_karaoke_source,
    resolve_loose_pair,
)
from billsmusic.media_type import MediaType, classify_path
from billsmusic.window import PlayerWindow


def _write_zip(path: Path, members):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members:
            archive.writestr(name, data)


def test_loose_pair_matches_case_insensitively(tmp_path):
    audio = tmp_path / "Song Name.MP3"
    cdg = tmp_path / "song name.cdg"
    audio.write_bytes(b"audio")
    cdg.write_bytes(b"graphics")

    pair = resolve_loose_pair(str(cdg))

    assert Path(pair.audio_path) == audio
    assert pair.cdg_path == str(cdg)
    assert pair.source_path == str(cdg)
    assert pair.source_type == "loose"


def test_unmatched_mp3_remains_music_and_unmatched_cdg_fails_safely(tmp_path):
    song = tmp_path / "ordinary.mp3"
    cdg = tmp_path / "incomplete.cdg"
    song.touch()
    cdg.touch()

    assert classify_path(str(song)) == MediaType.AUDIO
    with pytest.raises(KaraokeError, match="matching MP3"):
        resolve_loose_pair(str(cdg))
    assert karaoke_metadata(str(cdg))["karaoke_validation_state"] == "incomplete"


def test_library_scan_emits_one_logical_loose_karaoke_item(tmp_path):
    (tmp_path / "paired.mp3").touch()
    (tmp_path / "paired.cdg").touch()
    (tmp_path / "ordinary.mp3").touch()

    _, tracks, fingerprints = PlayerWindow._scan_folder(SimpleNamespace(), str(tmp_path))

    assert str(tmp_path / "paired.cdg") in tracks
    assert str(tmp_path / "paired.mp3") not in tracks
    assert str(tmp_path / "ordinary.mp3") in tracks
    assert "companion_mtime_ns" in fingerprints[str(tmp_path / "paired.cdg")]


def test_valid_zip_pair_in_nested_folder_is_selected(tmp_path):
    source = tmp_path / "show.zip"
    _write_zip(source, [("set/My Song.MP3", b"audio"), ("set/my song.cdg", b"cdg")])

    pair = inspect_karaoke_zip(str(source))

    assert pair.source_path == str(source)
    assert pair.source_type == "zip"
    assert pair.audio_member == "set/My Song.MP3"
    assert pair.cdg_member == "set/my song.cdg"


def test_zip_rejects_corruption_traversal_and_ambiguous_pairs(tmp_path):
    corrupt = tmp_path / "corrupt.zip"
    corrupt.write_bytes(b"not a zip")
    with pytest.raises(KaraokeError, match="Invalid karaoke archive"):
        inspect_karaoke_zip(str(corrupt))

    traversal = tmp_path / "traversal.zip"
    _write_zip(traversal, [("../song.mp3", b"a"), ("../song.cdg", b"c")])
    with pytest.raises(KaraokeError, match="unsafe path"):
        inspect_karaoke_zip(str(traversal))

    ambiguous = tmp_path / "ambiguous.zip"
    _write_zip(ambiguous, [
        ("one.mp3", b"a"), ("one.cdg", b"c"),
        ("two.mp3", b"a"), ("two.cdg", b"c"),
    ])
    with pytest.raises(KaraokeError, match="multiple ambiguous"):
        inspect_karaoke_zip(str(ambiguous))


def test_zip_rejects_password_flag_and_unreasonable_ratio(tmp_path, monkeypatch):
    source = tmp_path / "protected.zip"
    _write_zip(source, [("song.mp3", b"audio"), ("song.cdg", b"cdg")])
    real_zip_file = zipfile.ZipFile

    class FlaggedArchive:
        def __init__(self, *args, **kwargs):
            self.archive = real_zip_file(*args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.archive.close()

        def infolist(self):
            infos = self.archive.infolist()
            infos[0].flag_bits |= 1
            return infos

    monkeypatch.setattr(zipfile, "ZipFile", FlaggedArchive)
    with pytest.raises(KaraokeError, match="Password-protected"):
        inspect_karaoke_zip(str(source))

    monkeypatch.setattr(zipfile, "ZipFile", real_zip_file)
    bomb = tmp_path / "bomb.zip"
    _write_zip(bomb, [("song.mp3", b"x" * 100_000), ("song.cdg", b"y" * 100_000)])
    monkeypatch.setattr("billsmusic.karaoke.MAX_ZIP_COMPRESSION_RATIO", 2.0)
    with pytest.raises(KaraokeError, match="compression ratio"):
        inspect_karaoke_zip(str(bomb))


def test_zip_preparation_uses_private_cache_but_preserves_source_identity(tmp_path, monkeypatch):
    local_data = tmp_path / "local"
    monkeypatch.setenv("LOCALAPPDATA", str(local_data))
    source = tmp_path / "show.zip"
    _write_zip(source, [("show.mp3", b"audio"), ("show.cdg", b"graphics")])

    pair = prepare_karaoke_source(str(source))

    assert pair.source_path == str(source)
    assert Path(pair.audio_path).name == "audio.mp3"
    assert Path(pair.cdg_path).name == "graphics.cdg"
    assert str(local_data) in pair.audio_path
    assert karaoke_metadata(str(source))["audio_companion_path"] is None


def test_cancelled_zip_preparation_fails_without_publishing_partial_files(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    source = tmp_path / "show.zip"
    _write_zip(source, [("show.mp3", b"audio"), ("show.cdg", b"graphics")])

    with pytest.raises(KaraokeError, match="cancelled"):
        prepare_karaoke_source(str(source), should_cancel=lambda: True)

    assert not list((tmp_path / "local").rglob("*.part"))
