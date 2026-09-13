from billsmusic import metadata


def test_metadata_scan_records_existing_lrc(monkeypatch):
    monkeypatch.setattr(metadata, "MutagenFile", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        metadata.os.path,
        "isfile",
        lambda path: path == "C:/Music/Song.lrc",
    )
    result = metadata.read_track_meta("C:/Music/Song.mp3")
    assert result["has_lrc_sidecar"] is True
    assert result["has_embedded_synced_lyrics"] is False
    assert result["has_synced_lyrics"] is True


def test_metadata_scan_records_missing_lrc(monkeypatch):
    monkeypatch.setattr(metadata, "MutagenFile", lambda *args, **kwargs: None)
    monkeypatch.setattr(metadata.os.path, "isfile", lambda path: False)
    result = metadata.read_track_meta("C:/Music/Song.flac")
    assert result["has_lrc_sidecar"] is False
    assert result["has_synced_lyrics"] is False
