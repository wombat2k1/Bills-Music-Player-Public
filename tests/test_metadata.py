from billsmusic import metadata


class _FakeEasyAudio:
    def __init__(self, data=None):
        self._data = data or {}

    def get(self, key):
        return self._data.get(key)

    def keys(self):
        return self._data.keys()


class _FakeTags:
    def __init__(self, data):
        self._data = data

    def get(self, key):
        return self._data.get(key)

    def items(self):
        return self._data.items()


class _FakeFullAudio:
    def __init__(self, tags=None):
        self.tags = tags


def _no_lrc(monkeypatch):
    monkeypatch.setattr(metadata.os.path, "isfile", lambda path: False)


def test_reads_genre_and_year_from_easy_tags(monkeypatch):
    def fake_mutagen(path, easy=False, **kwargs):
        if easy:
            return _FakeEasyAudio({"genre": ["Rock"], "date": ["1985-03-02"]})
        return _FakeFullAudio()

    monkeypatch.setattr(metadata, "MutagenFile", fake_mutagen)
    _no_lrc(monkeypatch)
    result = metadata.read_track_meta("C:/Music/Song.flac")
    assert result["genre"] == "Rock"
    assert result["year"] == "1985-03-02"


def test_genre_joins_multiple_values(monkeypatch):
    def fake_mutagen(path, easy=False, **kwargs):
        if easy:
            return _FakeEasyAudio({"genre": ["Rock", "Pop"]})
        return _FakeFullAudio()

    monkeypatch.setattr(metadata, "MutagenFile", fake_mutagen)
    _no_lrc(monkeypatch)
    result = metadata.read_track_meta("C:/Music/Song.flac")
    assert result["genre"] == "Rock, Pop"


def test_bpm_key_from_easy_tags_skips_raw_fallback(monkeypatch):
    def fake_mutagen(path, easy=False, **kwargs):
        if easy:
            return _FakeEasyAudio({"bpm": ["120.0"], "initialkey": ["Am"]})
        raise AssertionError(
            "raw (non-easy) MutagenFile parse should not run when easy "
            "tags already satisfy bpm/key"
        )

    monkeypatch.setattr(metadata, "MutagenFile", fake_mutagen)
    _no_lrc(monkeypatch)
    result = metadata.read_track_meta("C:/Music/Song.mp3")
    assert result["bpm"] == "120"
    assert result["key"] == "Am"


def test_bpm_key_falls_back_to_raw_frames_when_easy_tags_absent(monkeypatch):
    def fake_mutagen(path, easy=False, **kwargs):
        if easy:
            return _FakeEasyAudio({})
        return _FakeFullAudio(tags=_FakeTags({"TBPM": ["128.5"], "TKEY": ["Gm"]}))

    monkeypatch.setattr(metadata, "MutagenFile", fake_mutagen)
    _no_lrc(monkeypatch)
    result = metadata.read_track_meta("C:/Music/Song.mp3")
    assert result["bpm"] == "128"
    assert result["key"] == "Gm"


def test_missing_genre_year_bpm_key_default_to_unknown(monkeypatch):
    monkeypatch.setattr(metadata, "MutagenFile", lambda *args, **kwargs: None)
    _no_lrc(monkeypatch)
    result = metadata.read_track_meta("C:/Music/Song.mp3")
    assert result["genre"] == "Unknown"
    assert result["year"] == "Unknown"
    assert result["bpm"] == "Unknown"
    assert result["key"] == "Unknown"


def test_partial_easy_tags_still_fall_back_only_for_missing_one(monkeypatch):
    def fake_mutagen(path, easy=False, **kwargs):
        if easy:
            return _FakeEasyAudio({"bpm": ["95"]})  # key still missing
        return _FakeFullAudio(tags=_FakeTags({"TKEY": ["Cm"]}))

    monkeypatch.setattr(metadata, "MutagenFile", fake_mutagen)
    _no_lrc(monkeypatch)
    result = metadata.read_track_meta("C:/Music/Song.mp3")
    assert result["bpm"] == "95"
    assert result["key"] == "Cm"


def test_external_folder_jpg_lookup_is_case_insensitive(monkeypatch, tmp_path):
    track = tmp_path / "song.mp3"
    track.write_bytes(b"audio")
    (tmp_path / "Folder.JPG").write_bytes(b"folder-cover")
    monkeypatch.setattr(metadata, "MutagenFile", lambda *args, **kwargs: None)
    assert metadata.read_cover_bytes(str(track)) == b"folder-cover"


def test_external_png_and_jpeg_names_are_supported(monkeypatch, tmp_path):
    monkeypatch.setattr(metadata, "MutagenFile", lambda *args, **kwargs: None)
    for name in ("cover.jpeg", "albumart.png"):
        folder = tmp_path / name.replace(".", "-")
        folder.mkdir()
        track = folder / "song.flac"
        track.write_bytes(b"audio")
        (folder / name).write_bytes(name.encode())
        assert metadata.read_cover_bytes(str(track)) == name.encode()


# ---------------------------------------------------------------------------
# .mkv/.webm: mutagen has no Matroska support at all (MutagenFile() always
# returns None for these), so read_track_meta() falls back to
# mkv_tags.read_mkv_tags() -- reported live as every .mkv video showing
# "Unknown Artist" despite having real tags a full-featured tool could see.
# ---------------------------------------------------------------------------

def test_mkv_falls_back_to_mkv_tags_when_mutagen_returns_none(monkeypatch):
    monkeypatch.setattr(metadata, "MutagenFile", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        metadata, "read_mkv_tags",
        lambda path: {"artist": "10cc", "title": "Dreadlock Holiday", "album": "Music Videos"},
    )
    _no_lrc(monkeypatch)

    # Under the generic "Music Videos" catch-all folder (blocklisted from
    # the folder-artist override below) so this test stays focused on the
    # mkv_tags fallback alone -- see test_video_folder_artist.py-style
    # tests further down for the folder-inference behaviour itself.
    result = metadata.read_track_meta("Y:/Videos/Music Videos/10cc - Dreadlock Holiday.mkv")

    assert result["artist"] == "10cc"
    assert result["title"] == "Dreadlock Holiday"
    assert result["album"] == "Music Videos"
    assert result["media_type"] == "video"


def test_mkv_with_no_tags_at_all_stays_unknown_artist(monkeypatch):
    # The real case found live: a .mkv with only auto-generated
    # ENCODER/DURATION tags and no ARTIST -- must not invent one.
    monkeypatch.setattr(metadata, "MutagenFile", lambda *args, **kwargs: None)
    monkeypatch.setattr(metadata, "read_mkv_tags", lambda path: {})
    _no_lrc(monkeypatch)

    result = metadata.read_track_meta("Y:/Videos/Music Videos/10cc - Dreadlock Holiday.mkv")

    assert result["artist"] == "Unknown Artist"
    assert result["album"] == "Music Videos"
    # Falls back to the filename stem, matching the existing video default.
    assert result["title"] == "10cc - Dreadlock Holiday"


def test_webm_also_uses_the_mkv_fallback(monkeypatch):
    # WebM is a Matroska profile -- same container format, same fallback.
    monkeypatch.setattr(metadata, "MutagenFile", lambda *args, **kwargs: None)
    monkeypatch.setattr(metadata, "read_mkv_tags", lambda path: {"artist": "Band"})
    _no_lrc(monkeypatch)

    result = metadata.read_track_meta("Y:/Videos/Music Videos/clip.webm")

    assert result["artist"] == "Band"


def test_non_matroska_video_does_not_attempt_mkv_tags(monkeypatch):
    # An .mp4 that mutagen fails to read for some other reason must not
    # go anywhere near the MKV-specific EBML parser.
    monkeypatch.setattr(metadata, "MutagenFile", lambda *args, **kwargs: None)

    def _boom(path):
        raise AssertionError("must not attempt MKV parsing for a non-Matroska file")

    monkeypatch.setattr(metadata, "read_mkv_tags", _boom)
    _no_lrc(monkeypatch)

    result = metadata.read_track_meta("Y:/Videos/Music Videos/clip.mp4")  # must not raise
    assert result["artist"] == "Unknown Artist"


def test_mkv_tags_still_overridden_by_the_video_folder_artist(monkeypatch):
    # A tag-based artist (from mutagen or mkv_tags) is still subject to
    # the video folder-artist override below -- confirmed here for the
    # mkv_tags path specifically, since it's the one most likely to
    # actually produce a value for a real .mkv today.
    monkeypatch.setattr(metadata, "MutagenFile", lambda *args, **kwargs: None)
    monkeypatch.setattr(metadata, "read_mkv_tags", lambda path: {"artist": "Tag Artist"})
    _no_lrc(monkeypatch)

    result = metadata.read_track_meta(r"Y:\Sorted\Folder Artist\clip.mkv")

    assert result["artist"] == "Folder Artist"
