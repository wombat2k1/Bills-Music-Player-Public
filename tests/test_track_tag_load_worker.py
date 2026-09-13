"""_activate_track_ui must never call MutagenFile synchronously for audio.

Reported: casting several tracks in a row eventually froze the app (the
GUI thread was blocked long enough to look like a crash), and separately
the Cast progress bar/equaliser "does not work correctly". A captured
stall trace pinned both to the same root cause: _read_tags() (a
mutagen.File() open) was being called inline, on the GUI thread, from
_activate_track_ui() on every single track change --

    _tick -> _next_track -> _play_path_direct -> _cast_play_path
    -> _activate_track_ui -> _load_tags -> _read_tags

-- and _tick() is the exact same timer loop that drives the Cast
progress bar and feeds _analyzer_tick() (the equaliser). A slow/NAS
read there stalls both. The fix mirrors the existing video path
(_load_cached_video_tags): show cached library/queue metadata
immediately (no I/O) and refresh with the authoritative tags via
TrackTagLoadWorker, a background QThread.
"""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import billsmusic.window as window_module
from billsmusic.metadata import Track
from billsmusic.window import PlayerWindow


class _FakeTrackInfo:
    artist = "Some Artist"
    title = "Some Title"


def _audio_activation_window(meta_by_path=None, queue_detail_cache=None):
    displayed = []
    async_requests = []
    window = SimpleNamespace(
        _current_media_type=window_module.MediaType.AUDIO,
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
        _record_recent_played=lambda path: None,
        _update_dj_info=lambda info, path: None,
        _load_lrc_for_track=lambda path: None,
        _start_jukebox_intro=lambda info: None,
        bio_worker=None,
        analyzer=SimpleNamespace(load=lambda path: None),
        analyzer_worker=SimpleNamespace(update_track=SimpleNamespace(emit=lambda path: None)),
        waveform_seekbar=SimpleNamespace(
            set_placeholder=lambda path: None,
            set_video_progress=lambda path: None,
        ),
        waveform_worker=SimpleNamespace(request=lambda path: None),
        now_playing=SimpleNamespace(setText=lambda text: None),
        _sync_party_mode=lambda: None,
        _sync_now_playing_overlay_for_media_type=lambda: None,
        _meta_by_path=meta_by_path if meta_by_path is not None else {},
        queue_detail_cache=queue_detail_cache if queue_detail_cache is not None else {},
        _cached_queue_analysis=lambda path, validate_signature=True: {},
    )
    window._load_cached_audio_tags = lambda path: PlayerWindow._load_cached_audio_tags(window, path)
    window._display_meta_for_path = lambda path: PlayerWindow._display_meta_for_path(window, path)
    window._display_track_tags = lambda info, path: displayed.append((info, path))
    window._queue_track_tags_async = lambda path: async_requests.append(path)
    return window, displayed, async_requests


def test_activate_track_ui_never_calls_mutagen_for_audio(monkeypatch):
    monkeypatch.setattr(
        window_module, "MutagenFile",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError(
                "_activate_track_ui must not read tags synchronously -- "
                "this is what stalled the GUI thread (and, during Cast, "
                "the progress bar/equaliser tick loop) on a NAS-backed track"
            )
        ),
    )
    window, displayed, async_requests = _audio_activation_window()

    PlayerWindow._activate_track_ui(window, None, "Y:/network/share/song.mp3")  # must not raise

    assert async_requests == ["Y:/network/share/song.mp3"]
    assert len(displayed) == 1


def test_cached_audio_tags_uses_library_and_queue_cache_without_io():
    window, _, _ = _audio_activation_window(
        meta_by_path={
            "song.mp3": {"title": "Real Title", "artist": "Real Artist", "album": "Real Album", "genre": "Rock"},
        },
        queue_detail_cache={"song.mp3": {"bitrate": "320k", "time": "3:45"}},
    )

    info = PlayerWindow._load_cached_audio_tags(window, "song.mp3")

    assert info.title == "Real Title"
    assert info.artist == "Real Artist"
    assert info.album == "Real Album"
    assert info.genre == "Rock"
    assert info.bitrate == "320k"
    assert info.duration == "3:45"


def test_cached_audio_tags_falls_back_to_filename_when_uncached():
    window, _, _ = _audio_activation_window()

    info = PlayerWindow._load_cached_audio_tags(window, "Y:/share/Unknown Track.mp3")

    assert info.title == "Unknown Track.mp3"
    assert info.artist == "Unknown"
    assert info.bitrate == "Unknown"


def test_track_tags_ready_is_dropped_for_a_stale_generation():
    displayed = []
    window = SimpleNamespace(
        _playback_generation=2,
        current_path="new_song.mp3",
        _display_track_tags=lambda info, path: displayed.append(path),
    )

    PlayerWindow._on_track_tags_ready(
        window, "old_song.mp3", {"title": "Old"}, generation=1,
    )

    assert displayed == []


def test_track_tags_ready_updates_display_for_the_current_track():
    displayed = []
    window = SimpleNamespace(
        _playback_generation=1,
        current_path="song.mp3",
        _display_track_tags=lambda info, path: displayed.append((info.title, path)),
    )

    PlayerWindow._on_track_tags_ready(
        window, "song.mp3", {
            "title": "Fresh Title", "artist": "Fresh Artist", "album": "Fresh Album",
            "genre": "Jazz", "bitrate": "256 kbps", "sample_rate": "44100 Hz",
            "channels": "2", "duration": "3:21",
        },
        generation=1,
    )

    assert displayed == [("Fresh Title", "song.mp3")]


# -- Stage 3A-r3 real-device defect: this worker "Unknown"-ifies the
# already-correct Plex title -------------------------------------------
#
# read_full_tag_display(path) opens `path` with MutagenFile(path,
# easy=True). For a synthetic plex://.../<ratingKey>.<ext> identity that
# open always fails -- but the function's own failure path does not
# raise, it returns {"title": "Unknown", "artist": "Unknown", ...} as a
# *successful* result. TrackTagLoadWorker.run() then emits tags_ready
# with that dict, and _on_track_tags_ready's generation/path staleness
# guard does not catch it (it really is the current track -- Mutagen
# just has nothing to read), so it silently overwrites the title
# _load_cached_audio_tags/_display_meta_for_path had already resolved
# correctly moments earlier. Confirmed against Bill's real acceptance
# run: toast/library row/Up Next all showed "Take On Me" / "A-Ha"
# correctly, but the top current-track header showed "Unknown".

PLEX_AUDIO = "plex://server-1/151212.flac"


def test_queue_track_tags_async_never_starts_a_worker_for_a_plex_identity(monkeypatch):
    class _ExplodingWorker:
        def __init__(self, *a, **kw):
            raise AssertionError(
                "TrackTagLoadWorker must never be constructed for a plex:// "
                "identity -- Mutagen can never read one, and its own "
                "failure path returns a *successful* all-Unknown result "
                "that would overwrite the already-correct Plex title"
            )

    monkeypatch.setattr(window_module, "TrackTagLoadWorker", _ExplodingWorker)
    diagnostics_events = []
    window = SimpleNamespace(
        _closing=False,
        _playback_generation=3,
        _track_tag_load_workers=[],
        diagnostics=SimpleNamespace(
            record=lambda category, op, **kw: diagnostics_events.append((category, op, kw)),
            path_details=lambda value: {"path_hash": f"hash-of-{value}"},
        ),
    )

    PlayerWindow._queue_track_tags_async(window, PLEX_AUDIO)  # must not raise

    assert window._track_tag_load_workers == []
    skipped_events = [
        e for e in diagnostics_events if e[1] == "track_tags_async_skipped_for_plex"
    ]
    assert len(skipped_events) == 1


def test_queue_track_tags_async_still_starts_a_worker_for_a_local_path():
    # Control: proves the guard above is specific to Plex identities, not
    # a blanket "never refresh tags" regression -- Local's existing
    # background-refresh behaviour must keep working exactly as before.
    started = []

    class _FakeWorker:
        def __init__(self, path):
            self.path = path
            self.tags_ready = SimpleNamespace(connect=lambda slot: None)
            self.finished = SimpleNamespace(connect=lambda slot: None)

        def start(self):
            started.append(self.path)

    import billsmusic.window as wm
    original = wm.TrackTagLoadWorker
    wm.TrackTagLoadWorker = _FakeWorker
    try:
        window = SimpleNamespace(
            _closing=False,
            _playback_generation=1,
            _track_tag_load_workers=[],
            _worker_registry=SimpleNamespace(
                register=lambda *a, **kw: "token", unregister=lambda token: None,
            ),
        )
        PlayerWindow._queue_track_tags_async(window, "Y:/network/share/song.mp3")
    finally:
        wm.TrackTagLoadWorker = original

    assert started == ["Y:/network/share/song.mp3"]


def test_on_track_tags_ready_never_overwrites_plex_title_with_unknown():
    displayed = []
    window = SimpleNamespace(
        _closing=False,
        _playback_generation=1,
        current_path=PLEX_AUDIO,
        _display_track_tags=lambda info, path: displayed.append((info.title, path)),
    )

    # The exact shape read_full_tag_display returns for a path Mutagen
    # cannot open -- a "successful" all-Unknown result, not an exception.
    PlayerWindow._on_track_tags_ready(
        window, PLEX_AUDIO, {
            "title": "Unknown", "artist": "Unknown", "album": "Unknown",
            "genre": "Unknown", "bitrate": "Unknown", "sample_rate": "Unknown",
            "channels": "Unknown", "duration": "Unknown",
        },
        generation=1,
    )

    assert displayed == [], (
        "a Mutagen-sourced result for a plex:// identity must never reach "
        "the display -- it can only ever be read_full_tag_display's "
        "no-such-file placeholder, never real data"
    )
