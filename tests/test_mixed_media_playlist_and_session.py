"""Mixed audio/video M3U playlists and session-1 restore: neither
playlist_repair.py nor session.py need any media-type awareness of their
own -- both already carry bare path strings, and classification happens
at use time via classify_path(). These tests demonstrate that a playlist
or session containing a mix of audio and video paths round-trips exactly
like an audio-only one, and that each restored path classifies correctly
on demand."""
import tempfile
from pathlib import Path

from billsmusic.media_type import MediaType, classify_path
from billsmusic.playlist_repair import load_m3u, save_m3u, PlaylistEntry
from billsmusic.session import SESSION_VERSION, load_session_file, save_session_file


def test_mixed_m3u_playlist_preserves_order_and_duplicates(tmp_path):
    song = tmp_path / "song.mp3"
    clip = tmp_path / "clip.mp4"
    song.touch()
    clip.touch()
    playlist_path = tmp_path / "mixed.m3u8"
    playlist_path.write_text(
        "#EXTM3U\n"
        f"#EXTINF:180,Artist - Song\n{song}\n"
        f"#EXTINF:30,Unknown Artist - Clip\n{clip}\n"
        f"{song}\n"  # duplicate audio entry
        f"{clip}\n"  # duplicate video entry
        , encoding="utf-8",
    )

    entries, counts = load_m3u(str(playlist_path))

    assert [entry.path for entry in entries] == [str(song), str(clip), str(song), str(clip)]
    assert counts["valid"] == 4
    assert [classify_path(entry.path) for entry in entries] == [
        MediaType.AUDIO, MediaType.VIDEO, MediaType.AUDIO, MediaType.VIDEO,
    ]


def test_mixed_m3u_playlist_round_trips_through_save(tmp_path):
    song = tmp_path / "song.mp3"
    clip = tmp_path / "clip.mp4"
    song.touch()
    clip.touch()
    entries = [
        PlaylistEntry(
            path=str(song), resolved_path=str(song), display_title="Song",
            artist="Artist", duration_seconds=180, is_missing=False, original_path=str(song),
        ),
        PlaylistEntry(
            path=str(clip), resolved_path=str(clip), display_title="Clip",
            artist="Unknown Artist", duration_seconds=None, is_missing=False, original_path=str(clip),
        ),
    ]
    out_path = tmp_path / "saved.m3u8"

    save_m3u(str(out_path), entries)
    reloaded, counts = load_m3u(str(out_path))

    assert [e.path for e in reloaded] == [str(song), str(clip)]
    assert counts["valid"] == 2


def test_session_version_1_restores_mixed_audio_video_and_karaoke_paths(tmp_path):
    song = tmp_path / "song.flac"
    clip = tmp_path / "clip.mkv"
    song.touch()
    clip.touch()
    karaoke = tmp_path / "karaoke.zip"
    karaoke.touch()
    session_path = tmp_path / "session.json"
    queue = [str(song), str(karaoke), str(clip), str(karaoke)]

    save_session_file(str(session_path), queue, [False, False, True, False], str(clip))
    restored_queue, played, current_path = load_session_file(str(session_path))

    assert SESSION_VERSION == 1  # no schema bump was needed for this feature
    assert restored_queue == queue
    assert played == [False, False, True, False]
    assert current_path == str(clip)
    # Media type is inferred from the restored path, not stored -- confirm
    # each restored path still classifies correctly on its own.
    assert classify_path(restored_queue[0]) == MediaType.AUDIO
    assert classify_path(restored_queue[1]) == MediaType.KARAOKE
    assert classify_path(restored_queue[2]) == MediaType.VIDEO


def test_m3u_preserves_stable_karaoke_sources_and_duplicates(tmp_path):
    loose = tmp_path / "Duet.cdg"
    archive = tmp_path / "Show.zip"
    loose.touch()
    archive.touch()
    playlist_path = tmp_path / "karaoke.m3u8"
    playlist_path.write_text(
        f"#EXTM3U\n{loose.name}\n{archive.name}\n{loose.name}\n",
        encoding="utf-8",
    )

    entries, counts = load_m3u(str(playlist_path))

    assert [entry.path for entry in entries] == [str(loose), str(archive), str(loose)]
    assert counts["valid"] == 3
    assert all(classify_path(entry.path) == MediaType.KARAOKE for entry in entries)
