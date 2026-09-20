"""Coverage for _sync_now_playing_overlay_for_media_type: the Now Playing
overlay (title/artist/equaliser badge/panel + bio cards, all painted by the
single JukeboxOverlay widget) must hide the instant video or karaoke
playback starts and come back exactly as it was once audio resumes -- never
forcing it visible if it was already manually hidden, and never flickering
on a redundant call while already in the target state.

Uses the SimpleNamespace "fake window" + real unbound PlayerWindow method
pattern established throughout this test suite.
"""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from billsmusic.media_type import MediaType
from billsmusic.window import PlayerWindow


class _FakeOverlay:
    """Mimics JukeboxOverlay's visibility surface (isVisible/hide/setVisible)."""

    def __init__(self, visible=True):
        self._visible = visible
        self.hide_calls = 0
        self.set_visible_calls = []

    def isVisible(self):
        return self._visible

    def hide(self):
        self.hide_calls += 1
        self._visible = False

    def setVisible(self, value):
        self.set_visible_calls.append(value)
        self._visible = value


class _FakeTrackInfo:
    artist = "Some Artist"
    title = "Some Title"


def _base_window(media_type, overlay_visible=True):
    """A window double wired for _activate_track_ui, with a real overlay
    double so overlay visibility can actually be observed after the call."""
    analyzer_worker = SimpleNamespace(update_track=MagicMock())
    window = SimpleNamespace(
        _current_media_type=media_type,
        overlay=_FakeOverlay(visible=overlay_visible),
        _now_playing_overlay_suppressed=False,
        _now_playing_overlay_was_visible=True,
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
        _load_lrc_for_track=lambda path: None,
        _clear_synced_lyrics_state=lambda: None,
        _start_jukebox_intro=MagicMock(),
        bio_worker=None,
        analyzer=MagicMock(),
        analyzer_worker=analyzer_worker,
        waveform_seekbar=MagicMock(),
        waveform_worker=MagicMock(),
        now_playing=SimpleNamespace(setText=lambda text: None),
        _sync_party_mode=lambda: None,
    )
    window._sync_now_playing_overlay_for_media_type = (
        lambda: PlayerWindow._sync_now_playing_overlay_for_media_type(window)
    )
    return window


# -- music -> video -----------------------------------------------------

def test_music_to_video_hides_overlay():
    window = _base_window(MediaType.AUDIO, overlay_visible=True)
    window._current_media_type = MediaType.VIDEO
    PlayerWindow._activate_track_ui(window, "clip.mp4")
    assert window.overlay.isVisible() is False
    assert window._now_playing_overlay_suppressed is True


# -- video -> music -------------------------------------------------------

def test_video_to_music_restores_overlay():
    window = _base_window(MediaType.VIDEO, overlay_visible=True)
    # Simulate the overlay already having been hidden by an earlier video.
    window.overlay.hide()
    window._now_playing_overlay_suppressed = True
    window._now_playing_overlay_was_visible = True

    window._current_media_type = MediaType.AUDIO
    PlayerWindow._activate_track_ui(window, "song.mp3")

    assert window.overlay.isVisible() is True
    assert window._now_playing_overlay_suppressed is False


# -- music -> karaoke -------------------------------------------------------

def test_music_to_karaoke_hides_overlay():
    window = _base_window(MediaType.AUDIO, overlay_visible=True)
    window._current_media_type = MediaType.KARAOKE
    PlayerWindow._activate_track_ui(window, "song.cdg")
    assert window.overlay.isVisible() is False
    assert window._now_playing_overlay_suppressed is True


# -- karaoke -> music -------------------------------------------------------

def test_karaoke_to_music_restores_overlay():
    window = _base_window(MediaType.KARAOKE, overlay_visible=True)
    window.overlay.hide()
    window._now_playing_overlay_suppressed = True
    window._now_playing_overlay_was_visible = True

    window._current_media_type = MediaType.AUDIO
    PlayerWindow._activate_track_ui(window, "song.mp3")

    assert window.overlay.isVisible() is True
    assert window._now_playing_overlay_suppressed is False


# -- video -> video (idempotent, no redundant toggling/flicker) -----------

def test_video_to_video_does_not_retoggle_already_hidden_overlay():
    window = _base_window(MediaType.VIDEO, overlay_visible=True)
    PlayerWindow._activate_track_ui(window, "first.mp4")
    assert window.overlay.hide_calls == 1
    assert window._now_playing_overlay_suppressed is True

    # Second video track begins while still in VIDEO -- must not call
    # hide() again (that would be a redundant, flicker-risking toggle).
    window._current_media_type = MediaType.VIDEO
    PlayerWindow._activate_track_ui(window, "second.mp4")
    assert window.overlay.hide_calls == 1


# -- panel manually hidden before video starts must stay hidden after -----

def test_overlay_already_hidden_before_video_stays_hidden_after_returning_to_music():
    window = _base_window(MediaType.AUDIO, overlay_visible=False)
    # User had manually hidden the overlay (or some other feature hid it)
    # before video playback ever started.
    window._current_media_type = MediaType.VIDEO
    PlayerWindow._activate_track_ui(window, "clip.mp4")
    assert window.overlay.isVisible() is False
    assert window._now_playing_overlay_was_visible is False

    window._current_media_type = MediaType.AUDIO
    PlayerWindow._activate_track_ui(window, "song.mp3")
    # Restored to its pre-video state -- still hidden, not forced back on.
    assert window.overlay.isVisible() is False


# -- playback failure and restoration --------------------------------------

def test_video_error_restores_overlay_to_previous_state():
    events = []
    overlay = _FakeOverlay(visible=True)
    overlay.hide()
    window = SimpleNamespace(
        _current_media_type=MediaType.VIDEO,
        _video_fullscreen=False,
        _exit_video_fullscreen=lambda: events.append("exit_fullscreen"),
        _video_backend=SimpleNamespace(stop=lambda: events.append("stop_video")),
        _detach_video_from_party_mode=lambda: None,
        _show_normal_display_page=lambda: None,
        _resume_deferred_queue_analysis=lambda: None,
        _playback_expected=True,
        _VIDEO_ERROR_MESSAGES=PlayerWindow._VIDEO_ERROR_MESSAGES,
        current_path="clip.mp4",
        diagnostics=SimpleNamespace(
            record=lambda *a, **kw: None,
            path_details=lambda path: {},
        ),
        statusBar=lambda: SimpleNamespace(showMessage=lambda *a, **kw: None),
        overlay=overlay,
        _now_playing_overlay_suppressed=True,
        _now_playing_overlay_was_visible=True,
        _mixed_transition_state="idle",
    )
    window._sync_now_playing_overlay_for_media_type = (
        lambda: PlayerWindow._sync_now_playing_overlay_for_media_type(window)
    )

    PlayerWindow._on_video_error(window, "video_decode_error", "decode failed")

    assert window._current_media_type == MediaType.AUDIO
    assert overlay.isVisible() is True
    assert window._now_playing_overlay_suppressed is False


def test_karaoke_prepare_failure_restores_overlay_to_previous_state():
    overlay = _FakeOverlay(visible=True)
    overlay.hide()
    window = SimpleNamespace(
        _karaoke_generation=1,
        _current_media_type=MediaType.KARAOKE,
        karaoke_widget=MagicMock(),
        _playback_expected=True,
        statusBar=lambda: SimpleNamespace(showMessage=lambda *a, **kw: None),
        diagnostics=SimpleNamespace(
            record=lambda *a, **kw: None,
            path_details=lambda path: {},
        ),
        _show_normal_display_page=lambda: None,
        overlay=overlay,
        _now_playing_overlay_suppressed=True,
        _now_playing_overlay_was_visible=True,
        _mixed_transition_state="idle",
        _current_playback_attempt=SimpleNamespace(attempt_id=1, is_terminal=lambda: False),
    )
    window._sync_now_playing_overlay_for_media_type = (
        lambda: PlayerWindow._sync_now_playing_overlay_for_media_type(window)
    )
    window._is_current_playback_attempt = (
        lambda attempt_id: PlayerWindow._is_current_playback_attempt(window, attempt_id)
    )
    window._advance_playback_attempt_state = (
        lambda attempt_id, state: PlayerWindow._advance_playback_attempt_state(window, attempt_id, state)
    )
    window._require_current_playback_attempt = (
        lambda attempt_id, stage: PlayerWindow._require_current_playback_attempt(window, attempt_id, stage)
    )

    PlayerWindow._on_karaoke_prepare_failed(window, 1, "song.cdg", "bad zip", attempt_id=1)

    assert window._current_media_type == MediaType.AUDIO
    assert overlay.isVisible() is True
    assert window._now_playing_overlay_suppressed is False
