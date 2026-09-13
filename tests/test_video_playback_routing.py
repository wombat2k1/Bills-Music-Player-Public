"""_play_path_direct routing: audio keeps using the existing backends
untouched, video goes to the Qt Multimedia backend, and Cast is forced back
to This Computer before a video starts. Uses the SimpleNamespace "fake
window" + real unbound PlayerWindow method pattern used throughout this
suite (see test_track_transition_mode.py's module docstring)."""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from billsmusic.media_type import MediaType
from billsmusic.window import PlayerWindow


def _video_window(tmp_path, cast_active=False, video_playback_enabled=True):
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"x")
    video_backend = MagicMock()
    video_backend.load.return_value = True
    calls = SimpleNamespace(
        force_local_output=0, cancel_fade=0, stop_all=0,
        cancel_watchdog=0, activate_ui=[], show_loading=0,
    )
    window = SimpleNamespace(
        _video_backend=video_backend,
        cast_active=cast_active,
        video_playback_enabled=video_playback_enabled,
        track_index_by_path={},
        _playback_generation=0,
        _playback_recovery_active=False,
        _playback_recovery_attempts={},
        _playback_expected=False,
        _playback_intentionally_paused=True,
        pending_next=True,
        current_path=None,
        current_index=None,
        diagnostics=SimpleNamespace(
            record=lambda *a, **kw: None,
            path_details=lambda path: {"path_hash": "x"},
        ),
        statusBar=lambda: SimpleNamespace(showMessage=lambda *a, **kw: None),
        label_remaining=SimpleNamespace(setText=lambda text: None),
        master_volume=70,
        _muted=False,
        _current_media_type=MediaType.AUDIO,
        _calls=calls,
        _detach_video_from_party_mode=lambda: None,
        _show_normal_display_page=lambda: None,
        _resume_deferred_queue_analysis=lambda: None,
        _mixed_transition_state="idle",
        _next_playback_attempt_id=1,
        _current_playback_attempt=None,
        _player_topology_epoch=0,
    )
    window._set_player_topology = (
        lambda active, inactive, reason: PlayerWindow._set_player_topology(window, active, inactive, reason=reason)
    )
    window._begin_playback_attempt = (
        lambda *a, **kw: PlayerWindow._begin_playback_attempt(window, *a, **kw)
    )
    window._is_current_playback_attempt = (
        lambda attempt_id: PlayerWindow._is_current_playback_attempt(window, attempt_id)
    )
    window._cancel_current_playback_attempt = (
        lambda reason: PlayerWindow._cancel_current_playback_attempt(window, reason)
    )
    window._advance_playback_attempt_state = (
        lambda attempt_id, state: PlayerWindow._advance_playback_attempt_state(window, attempt_id, state)
    )
    window._stop_video_for_audio_transition = (
        lambda: PlayerWindow._stop_video_for_audio_transition(window)
    )
    window._force_local_output_for_video = lambda: setattr(
        calls, "force_local_output", calls.force_local_output + 1
    )
    window._cancel_fade = lambda: setattr(calls, "cancel_fade", calls.cancel_fade + 1)
    window._stop_all = lambda: setattr(calls, "stop_all", calls.stop_all + 1)
    window._cancel_playback_watchdog = lambda: setattr(
        calls, "cancel_watchdog", calls.cancel_watchdog + 1
    )
    window._activate_track_ui = lambda index, path: calls.activate_ui.append((index, path))
    window._show_video_loading_page = lambda: setattr(
        calls, "show_loading", calls.show_loading + 1
    )
    window._reset_progress = lambda: None
    window._play_video_path_direct = lambda path, index=None: PlayerWindow._play_video_path_direct(
        window, path, index=index
    )
    window._promote_dual_transition_track_ui = (
        lambda path, index, reason="dual_transition_promotion":
            PlayerWindow._promote_dual_transition_track_ui(window, path, index, reason=reason)
    )
    window._dual_transition_promoted_path = None
    return window, str(video_path), video_backend, calls


def test_video_path_routes_to_video_backend_not_vlc(tmp_path):
    window, video_path, video_backend, calls = _video_window(tmp_path)
    # A poisoned _ensure_vlc proves the video path never reaches VLC setup.
    window._ensure_vlc = lambda: (_ for _ in ()).throw(
        AssertionError("VLC must never be constructed for a video path")
    )

    result = PlayerWindow._play_path_direct(window, video_path)

    assert result is True
    video_backend.load.assert_called_once_with(video_path)
    assert window._current_media_type == MediaType.VIDEO
    assert calls.activate_ui == [(None, video_path)]
    assert calls.show_loading == 1
    assert calls.cancel_fade == 1
    assert calls.stop_all == 1
    assert calls.cancel_watchdog == 1


def test_video_load_syncs_current_volume_and_mute_state(tmp_path):
    # Reported live: video played with no audio; restarting the app fixed
    # it. Root cause: unlike every other backend (_play_simple's BASS/
    # miniaudio, the VLC path), which explicitly sets volume on every
    # track load, the video backend's own QAudioOutput was never synced
    # to the app's actual current volume/mute state on load -- only when
    # the user happened to move the volume slider afterward. A stale or
    # freshly-restarted video subprocess had no way to pick up the real
    # state until that unrelated event occurred.
    window, video_path, video_backend, _calls = _video_window(tmp_path)
    window.master_volume = 42
    window._muted = True

    PlayerWindow._play_path_direct(window, video_path)

    video_backend.set_volume.assert_called_once_with(42)
    video_backend.set_muted.assert_called_once_with(True)
    # Must happen before load() actually starts playback, matching
    # _play_simple's set_volume-then-play ordering.
    assert video_backend.method_calls.index(("set_volume", (42,), {})) < (
        video_backend.method_calls.index(("load", (video_path,), {}))
    )


def test_video_load_warns_when_dual_transition_already_committed(tmp_path):
    # Regression canary for the bug fixed in video_transition.py's
    # handle_natural_end()/request_manual_next(): a classic load() call
    # must never reach the video backend while a GPU cross-dissolve has
    # already committed -- that would tear down the presentation surface
    # the still-running GPU transition owns. This pins the permanent
    # diagnostic added to detect exactly that if it ever regresses.
    window, video_path, video_backend, _calls = _video_window(tmp_path)
    recorded = []
    window.diagnostics = SimpleNamespace(
        record=lambda category, event, **kw: recorded.append((category, event, kw)),
        path_details=lambda path: {"path_hash": "x"},
    )
    window._dual_transition_committed_state_value = lambda: "transitioning"

    PlayerWindow._play_path_direct(window, video_path)

    video_backend.load.assert_called_once_with(video_path)
    warnings = [r for r in recorded if r[1] == "video_load_during_committed_dual_transition"]
    assert len(warnings) == 1
    assert warnings[0][2]["details"]["dual_transition_committed_state"] == "transitioning"


def test_video_load_has_no_warning_when_no_dual_transition_is_committed(tmp_path):
    window, video_path, video_backend, _calls = _video_window(tmp_path)
    recorded = []
    window.diagnostics = SimpleNamespace(
        record=lambda category, event, **kw: recorded.append((category, event, kw)),
        path_details=lambda path: {"path_hash": "x"},
    )
    window._dual_transition_committed_state_value = lambda: None

    PlayerWindow._play_path_direct(window, video_path)

    video_backend.load.assert_called_once_with(video_path)
    assert [r for r in recorded if r[1] == "video_load_during_committed_dual_transition"] == []


def test_dual_transition_promoted_path_skips_backend_reload(tmp_path):
    # The video subprocess already promoted this path live as the GPU
    # secondary deck; a matching _play_video_path_direct call (the one-
    # shot post-promotion activation) must do UI bookkeeping only and
    # never issue a fresh classic-backend load() that would interrupt the
    # cross-dissolve already on screen.
    window, video_path, video_backend, calls = _video_window(tmp_path)
    window._dual_transition_promoted_path = video_path
    recorded = []
    window.diagnostics = SimpleNamespace(
        record=lambda category, event, **kw: recorded.append((category, event, kw)),
        path_details=lambda path: {"path_hash": "x"},
    )

    result = PlayerWindow._play_video_path_direct(window, video_path)

    assert result is True
    video_backend.load.assert_not_called()
    assert calls.show_loading == 0
    assert calls.activate_ui == [(None, video_path)]
    assert window._dual_transition_promoted_path is None
    promotions = [r for r in recorded if r[1] == "video_dual_transition_promoted"]
    assert len(promotions) == 1
    assert promotions[0][2]["details"]["reason"] == "dual_transition_promotion"


def test_redundant_reactivation_of_current_video_skips_backend_reload(tmp_path):
    # Real-device bug: re-clicking/re-activating a video that is already
    # the current track (no fresh promotion pending) reached the normal
    # reload path, issuing a classic-backend load() for content the GPU
    # compositor was already actively rendering -- video broke while
    # audio kept playing. A redundant reactivation of the already-current
    # video must be treated the same as a promoted-path reactivation:
    # bookkeeping only, backend untouched.
    window, video_path, video_backend, calls = _video_window(tmp_path)
    window.current_path = video_path
    window._current_media_type = MediaType.VIDEO
    recorded = []
    window.diagnostics = SimpleNamespace(
        record=lambda category, event, **kw: recorded.append((category, event, kw)),
        path_details=lambda path: {"path_hash": "x"},
    )

    result = PlayerWindow._play_video_path_direct(window, video_path)

    assert result is True
    video_backend.load.assert_not_called()
    assert calls.show_loading == 0
    assert calls.activate_ui == [(None, video_path)]
    promotions = [r for r in recorded if r[1] == "video_dual_transition_promoted"]
    assert len(promotions) == 1
    assert promotions[0][2]["details"]["reason"] == "redundant_reactivation"


def test_different_new_video_path_is_unaffected_by_redundant_reactivation_guard(tmp_path):
    # A genuinely different video (not the current path, not a pending
    # promotion) must still take the full reload path unaffected by the
    # new guard.
    window, video_path, video_backend, calls = _video_window(tmp_path)
    other_path = str(tmp_path / "other.mp4")
    window.current_path = other_path
    window._current_media_type = MediaType.VIDEO

    result = PlayerWindow._play_video_path_direct(window, video_path)

    assert result is True
    video_backend.load.assert_called_once_with(video_path)
    assert calls.show_loading == 1


def test_audio_path_never_touches_video_backend(tmp_path):
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"x")
    window, _video_path, video_backend, _calls = _video_window(tmp_path)
    window.use_simple = True
    window.simple_player = MagicMock()
    window.simple_player.is_playing.return_value = False
    window.builtin_backend = "miniaudio"
    window.miniaudio_player = window.simple_player
    window.simple_inactive_player = MagicMock()
    window.miniaudio_inactive_player = window.simple_inactive_player
    window._start_miniaudio_crossfade_to = lambda *a, **kw: False
    window._cancel_fade = lambda: None
    window._stop_all = lambda: None
    window._play_simple = lambda path: True
    window._arm_playback_watchdog = lambda *a, **kw: None
    window.beat = SimpleNamespace(setPlaying=lambda v: None)

    PlayerWindow._play_path_direct(window, str(audio_path))

    video_backend.load.assert_not_called()
    assert window._current_media_type == MediaType.AUDIO


def test_video_while_cast_active_forces_local_output_first(tmp_path):
    window, video_path, video_backend, calls = _video_window(tmp_path, cast_active=True)

    PlayerWindow._play_path_direct(window, video_path)

    assert calls.force_local_output == 1
    video_backend.load.assert_called_once_with(video_path)


def test_video_playback_disabled_shows_message_and_never_loads(tmp_path):
    window, video_path, video_backend, calls = _video_window(
        tmp_path, video_playback_enabled=False,
    )
    messages = []
    window.statusBar = lambda: SimpleNamespace(
        showMessage=lambda text, *a, **kw: messages.append(text)
    )

    result = PlayerWindow._play_path_direct(window, video_path)

    assert result is False
    video_backend.load.assert_not_called()
    assert calls.activate_ui == []
    assert messages


def test_video_now_playing_metadata_uses_cache_without_opening_mp4():
    path = "Z:/Music Videos/Cached Clip.mp4"
    tag_box = MagicMock()
    tag_box.verticalScrollBar.return_value = MagicMock()
    window = SimpleNamespace(
        _meta_by_path={
            path: {
                "title": "Cached Title",
                "artist": "Cached Artist",
                "album": "Music Videos",
                "genre": "Pop",
            }
        },
        queue_detail_cache={path: {"bitrate": "320k", "time": "3:45"}},
        tag_box=tag_box,
        tag_scroll_pos=99.0,
        tag_reset_after_pause=True,
        now_playing=MagicMock(),
        _read_tags=MagicMock(side_effect=AssertionError("MP4 must not be opened")),
    )
    window._format_tag_html = (
        lambda info, track_path: PlayerWindow._format_tag_html(window, info, track_path)
    )
    window._display_track_tags = (
        lambda info, track_path: PlayerWindow._display_track_tags(window, info, track_path)
    )
    window._display_meta_for_path = (
        lambda track_path: PlayerWindow._display_meta_for_path(window, track_path)
    )

    info = PlayerWindow._load_cached_video_tags(window, path)

    window._read_tags.assert_not_called()
    assert info.title == "Cached Title"
    assert info.artist == "Cached Artist"
    assert info.bitrate == "320k"
    assert info.duration == "3:45"
    tag_box.setHtml.assert_called_once()


def test_pause_toggles_video_backend_when_video_is_current():
    video_backend = MagicMock()
    window = SimpleNamespace(
        _current_media_type=MediaType.VIDEO,
        _video_backend=video_backend,
        _playback_intentionally_paused=False,
        btn_pause=SimpleNamespace(setText=lambda t: None, setAccessibleName=lambda t: None),
        _announce_accessible_status=lambda message: None,
    )
    PlayerWindow.pause(window)
    video_backend.pause.assert_called_once()
    assert window._playback_intentionally_paused is True

    PlayerWindow.pause(window)
    video_backend.resume.assert_called_once()
    assert window._playback_intentionally_paused is False


def test_stop_playback_stops_video_backend_and_resets_media_type():
    video_backend = MagicMock()
    window = SimpleNamespace(
        _current_media_type=MediaType.VIDEO,
        _video_backend=video_backend,
        _show_normal_display_page=lambda: None,
        _detach_video_from_party_mode=lambda: None,
        cast_active=False,
        _cancel_fade=lambda: None,
        _stop_all=lambda: None,
        _cancel_playback_watchdog=lambda: None,
        _playback_expected=True,
        _playback_intentionally_paused=True,
        beat=SimpleNamespace(setPlaying=lambda v: None),
        btn_pause=SimpleNamespace(setText=lambda t: None, setAccessibleName=lambda t: None),
        _reset_progress=lambda: None,
        _announce_accessible_status=lambda message: None,
        _resume_deferred_queue_analysis=lambda: None,
        _sync_now_playing_overlay_for_media_type=lambda: None,
        _current_playback_attempt=None,
    )
    window._stop_video_for_audio_transition = (
        lambda: PlayerWindow._stop_video_for_audio_transition(window)
    )
    window._cancel_current_playback_attempt = (
        lambda reason: PlayerWindow._cancel_current_playback_attempt(window, reason)
    )
    PlayerWindow.stop_playback(window)
    video_backend.stop.assert_called_once()
    assert window._current_media_type == MediaType.AUDIO
