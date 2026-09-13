"""Video tracks must never reach the audio waveform/BPM/key analysis
pipeline -- _activate_track_ui gates the analyzer/analyzer_worker/waveform
calls on the current media type."""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from billsmusic.media_type import MediaType
from billsmusic.window import PlayerWindow


class _FakeTrackInfo:
    artist = "Some Artist"
    title = "Some Title"


def _activate_ui_window(media_type):
    analyzer = MagicMock()
    analyzer_worker = SimpleNamespace(update_track=MagicMock())
    waveform_worker = MagicMock()
    waveform_seekbar = MagicMock()
    window = SimpleNamespace(
        _current_media_type=media_type,
        viz_logger=SimpleNamespace(active=False, _track=None),
        current_index=None,
        current_path=None,
        _reset_recently_played_tracking=lambda path: None,
        _schedule_session_save=lambda: None,
        _set_playing_button_state=lambda: None,
        quiet_count=0,
        _last_quiet_debug_remaining=None,
        _reset_analyzer_clock=lambda: None,
        _select_tree_item=lambda path: None,
        beat=SimpleNamespace(setPlaying=lambda v: None),
        _load_tags=lambda path: _FakeTrackInfo(),
        _load_cached_audio_tags=lambda path: _FakeTrackInfo(),
        _display_track_tags=lambda info, path: None,
        _queue_track_tags_async=lambda path: None,
        _record_recent_played=lambda path: None,
        _update_dj_info=lambda info, path: None,
        _load_lrc_for_track=MagicMock(),
        _clear_synced_lyrics_state=MagicMock(),
        _start_jukebox_intro=MagicMock(),
        bio_worker=None,
        analyzer=analyzer,
        analyzer_worker=analyzer_worker,
        waveform_seekbar=waveform_seekbar,
        waveform_worker=waveform_worker,
        now_playing=SimpleNamespace(setText=lambda text: None),
        _sync_party_mode=lambda: None,
        _sync_now_playing_overlay_for_media_type=lambda: None,
    )
    return window, analyzer, analyzer_worker, waveform_worker, waveform_seekbar


def test_video_track_skips_analyzer_and_waveform():
    window, analyzer, analyzer_worker, waveform_worker, waveform_seekbar = _activate_ui_window(
        MediaType.VIDEO
    )
    PlayerWindow._activate_track_ui(window, None, "clip.mp4")
    analyzer.load.assert_not_called()
    analyzer_worker.update_track.emit.assert_not_called()
    waveform_worker.request.assert_not_called()
    waveform_seekbar.set_placeholder.assert_not_called()
    waveform_seekbar.set_video_progress.assert_called_once_with("clip.mp4")


def test_video_track_skips_jukebox_intro_overlay():
    # The intro card is a full-window overlay (see _start_jukebox_intro)
    # that visually conflicts with QVideoWidget's native compositing --
    # reported as the title card rendering partly behind the black video
    # surface. Video's own picture is already the visual, so skip it.
    window, *_ = _activate_ui_window(MediaType.VIDEO)
    PlayerWindow._activate_track_ui(window, None, "clip.mp4")
    window._start_jukebox_intro.assert_not_called()


def test_audio_track_still_reaches_analyzer_and_waveform():
    window, analyzer, analyzer_worker, waveform_worker, waveform_seekbar = _activate_ui_window(
        MediaType.AUDIO
    )
    PlayerWindow._activate_track_ui(window, None, "song.mp3")
    analyzer.load.assert_called_once_with("song.mp3")
    analyzer_worker.update_track.emit.assert_called_once_with("song.mp3")
    waveform_worker.request.assert_called_once_with("song.mp3")
    waveform_seekbar.set_placeholder.assert_called_once_with("song.mp3")
    window._start_jukebox_intro.assert_called_once()


# -- Plex audio: never send a remote plex:// identity to the offline
# analyzer/waveform decode pipeline (Phase C2.1 acceptance defect). The
# live visualiser for Plex audio comes from BASS FFT (_analyzer_tick's
# live_plex_bass branch, tests/test_plex_visualiser_dispatch.py) instead
# -- decoding the same remote stream a second time here would be wasted
# work at best and a download-then-decode of the whole file at worst. ---

PLEX_AUDIO_PATH = "plex://server-1/42.mp3"


def test_plex_audio_track_skips_offline_analyzer_and_waveform_decode():
    window, analyzer, analyzer_worker, waveform_worker, waveform_seekbar = _activate_ui_window(
        MediaType.AUDIO
    )
    PlayerWindow._activate_track_ui(window, None, PLEX_AUDIO_PATH)
    analyzer.load.assert_not_called()
    analyzer_worker.update_track.emit.assert_not_called()
    waveform_worker.request.assert_not_called()
    waveform_seekbar.set_placeholder.assert_not_called()
    # Waveform state set directly to "unavailable" -- never a decode
    # attempt, which for a remote identity would mean downloading the
    # file first.
    waveform_seekbar.set_waveform.assert_called_once_with(None)
    # The live-visualiser/current-track-analysis scheduling is unrelated
    # to the offline decode pipeline and must still run normally.
    window._start_jukebox_intro.assert_called_once()


def test_plex_audio_track_still_loads_lyrics_and_jukebox_intro():
    # Only the offline analyzer/waveform-decode calls are Plex-excluded --
    # everything else _activate_track_ui does for AUDIO is unaffected.
    window, *_ = _activate_ui_window(MediaType.AUDIO)
    PlayerWindow._activate_track_ui(window, None, PLEX_AUDIO_PATH)
    window._load_lrc_for_track.assert_called_once_with(PLEX_AUDIO_PATH)
    window._clear_synced_lyrics_state.assert_not_called()


# -- synced lyrics: video/karaoke never trigger the Mutagen/file-based
# lookup (neither media type ever displays them -- video shows its own
# picture instead of self.overlay, and karaoke drives its own separate CDG
# lyric mechanism entirely), matching a real-device performance finding:
# the unconditional lookup measured ~124ms on the GUI thread for an MP4
# during a transition, coinciding with a ~171ms GUI lag. ---------------------

def test_video_track_skips_lyrics_lookup():
    window, *_ = _activate_ui_window(MediaType.VIDEO)
    PlayerWindow._activate_track_ui(window, None, "clip.mp4")
    window._load_lrc_for_track.assert_not_called()
    window._clear_synced_lyrics_state.assert_called_once()


def test_karaoke_track_skips_lyrics_lookup():
    window, *_ = _activate_ui_window(MediaType.KARAOKE)
    PlayerWindow._activate_track_ui(window, None, "song.cdg")
    window._load_lrc_for_track.assert_not_called()
    window._clear_synced_lyrics_state.assert_called_once()


def test_audio_track_still_loads_lyrics():
    window, *_ = _activate_ui_window(MediaType.AUDIO)
    PlayerWindow._activate_track_ui(window, None, "song.mp3")
    window._load_lrc_for_track.assert_called_once_with("song.mp3")
    window._clear_synced_lyrics_state.assert_not_called()
