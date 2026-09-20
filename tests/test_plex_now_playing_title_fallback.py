"""Stage 3A real-device bug (item 5): the current-track display showed a
raw numeric Plex ratingKey (e.g. "151212") instead of the real title.

Root cause: _load_cached_audio_tags/_load_cached_video_tags look up
display metadata via self._meta_by_path, which is rebuilt from
self._full_meta_list -- a source-aware property that reflects whichever
tab (Local or Plex) is currently being *browsed*, not what's actually
*playing*. Browsing the Local tab while a Plex track is current silently
replaces _meta_by_path with Local-only entries; a later now-playing
refresh for that same still-current Plex track then found nothing there
and fell back to os.path.basename(path) -- for a plex://server/151212.flac
identity, that basename literally is "151212.flac".

Fix: self._plex_meta_by_path, populated once per Plex fetch (never
touched by library-source switching), is consulted as a fallback before
ever reaching a basename."""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import billsmusic.window as window_module
from billsmusic.media_type import MediaType
from billsmusic.now_playing import NowPlayingGeneration
from billsmusic.window import PlayerWindow

PLEX_AUDIO = "plex://server-1/151212.flac"
PLEX_VIDEO = "plex://server-1/107189.mp4"


class TagsHarness:
    _load_cached_audio_tags = PlayerWindow._load_cached_audio_tags
    _load_cached_video_tags = PlayerWindow._load_cached_video_tags
    _cached_queue_analysis = PlayerWindow._cached_queue_analysis
    _display_meta_for_path = PlayerWindow._display_meta_for_path

    def __init__(self):
        self._meta_by_path = {}  # simulates the Local tab having been browsed
        self._plex_meta_by_path = {
            PLEX_AUDIO: {
                "title": "9 PM (Till I Come)", "artist": "ATB",
                "album": "Movin' Melodies", "genre": "Trance",
            },
            PLEX_VIDEO: {
                "title": "Real Video Title", "artist": "Real Video Artist",
                "album": "",
            },
        }
        self.queue_detail_cache = {}
        self.queue_analysis_cache = {}
        self._display_track_tags = lambda info, path: None


def test_plex_audio_title_falls_back_to_plex_meta_not_ratingkey():
    harness = TagsHarness()
    info = harness._load_cached_audio_tags(PLEX_AUDIO)
    assert info.title == "9 PM (Till I Come)"
    assert info.artist == "ATB"
    assert "151212" not in info.title


def test_plex_video_title_falls_back_to_plex_meta_not_ratingkey():
    harness = TagsHarness()
    info = harness._load_cached_video_tags(PLEX_VIDEO)
    assert info.title == "Real Video Title"
    assert "107189" not in info.title


def test_meta_by_path_hit_still_wins_when_present():
    # If _meta_by_path DOES have a live entry (the common, unbroken case
    # -- Plex tab actively browsed), it's used as-is, never overridden by
    # the persistent fallback.
    harness = TagsHarness()
    harness._meta_by_path[PLEX_AUDIO] = {"title": "Fresher Title", "artist": "ATB"}
    info = harness._load_cached_audio_tags(PLEX_AUDIO)
    assert info.title == "Fresher Title"


def test_local_path_never_consults_plex_fallback():
    harness = TagsHarness()
    local_path = "F:/music/track.flac"
    info = harness._load_cached_audio_tags(local_path)
    # No _meta_by_path entry and not a Plex identity -- basename fallback
    # is correct and expected here, exactly as before this fix.
    assert info.title == "track.flac"


# -- Stage 3A-r2 real-device defect: the now-playing LABEL, not just the
# tags lookup that feeds it -----------------------------------------------
#
# The fix above (proven by the tests above it) makes _load_cached_audio_tags
# resolve the correct title. Bill's real acceptance run still showed the
# raw numeric ratingKey in the now-playing area -- because
# _activate_track_ui (the one place that actually calls
# self.now_playing.setText(...)) re-derived the label from
# os.path.basename(path) directly, a few lines *after* already computing
# the correctly-resolved info.title, silently discarding it. This harness
# binds the real, unbound _activate_track_ui itself (not just the tags
# loader it calls) so the actual consumer is what's under test.

class ActivateTrackUiHarness:
    _activate_track_ui = PlayerWindow._activate_track_ui
    _load_cached_audio_tags = PlayerWindow._load_cached_audio_tags
    _load_cached_video_tags = PlayerWindow._load_cached_video_tags
    _cached_queue_analysis = PlayerWindow._cached_queue_analysis
    _display_meta_for_path = PlayerWindow._display_meta_for_path
    _display_track_tags = PlayerWindow._display_track_tags
    _format_tag_html = PlayerWindow._format_tag_html

    def __init__(self):
        self._meta_by_path = {}
        self._plex_meta_by_path = {
            PLEX_AUDIO: {
                "title": "9 PM (Till I Come)", "artist": "ATB",
                "album": "Movin' Melodies", "genre": "Trance",
            },
            PLEX_VIDEO: {
                "title": "Real Video Title", "artist": "Real Video Artist",
                "album": "",
            },
        }
        self.queue_detail_cache = {}
        self.queue_analysis_cache = {}
        # Real, unbound _display_track_tags (class-level above) is what
        # actually sets now_playing.setText(...) now -- give it the tag
        # panel widgets it touches. No stub override here (unlike
        # TagsHarness/its lambda) since this harness is specifically about
        # proving that real consumer.
        self.tag_scroll_pos = 0.0
        self.tag_reset_after_pause = False
        self.tag_box = SimpleNamespace(
            setHtml=lambda html: None,
            verticalScrollBar=lambda: SimpleNamespace(setValue=lambda v: None),
        )

        self._current_media_type = MediaType.AUDIO
        self.current_index = None
        self.current_path = None
        self.viz_logger = SimpleNamespace(
            active=False, _track=None, stop=lambda: None,
        )
        self._sync_now_playing_overlay_for_media_type = lambda: None
        self.diagnostics = SimpleNamespace(
            record=lambda *a, **k: None,
            path_details=lambda value: {},
        )
        self.scrubbing = True
        self._reset_recently_played_tracking = lambda path: None
        self._schedule_session_save = lambda: None
        self._set_playing_button_state = lambda: None
        self.quiet_count = 0
        self._last_quiet_debug_remaining = None
        self._reset_analyzer_clock = lambda: None
        self._select_tree_item = lambda path: None
        self.beat = SimpleNamespace(setPlaying=lambda *_a, **_k: None)
        self._queue_track_tags_async = lambda path: None
        self._record_recent_played = lambda path: None
        self._update_dj_info = lambda info, path: None
        self._load_lrc_for_track = lambda path: None
        self._clear_synced_lyrics_state = lambda: None
        self._start_jukebox_intro = lambda info: None
        self.bio_worker = None
        self.analyzer = None
        self.analyzer_worker = None
        self.waveform_seekbar = None
        self._sync_party_mode = lambda: None

        self.now_playing_texts = []
        self.now_playing = SimpleNamespace(
            setText=lambda text: self.now_playing_texts.append(text)
        )


def test_activate_track_ui_shows_real_plex_title_not_ratingkey():
    harness = ActivateTrackUiHarness()
    harness._activate_track_ui(PLEX_AUDIO, library_index=0)
    assert harness.now_playing_texts[-1] == "9 PM (Till I Come)"
    assert "151212" not in harness.now_playing_texts[-1]


def test_activate_track_ui_shows_real_plex_video_title_not_ratingkey():
    harness = ActivateTrackUiHarness()
    harness._current_media_type = MediaType.VIDEO
    harness._activate_track_ui(PLEX_VIDEO, library_index=0)
    assert harness.now_playing_texts[-1] == "Real Video Title"
    assert "107189" not in harness.now_playing_texts[-1]


class RecentlyPlayedEntryHarness:
    _record_recently_played_entry = PlayerWindow._record_recently_played_entry
    _display_meta_for_path = PlayerWindow._display_meta_for_path

    def __init__(self):
        self._meta_by_path = {}
        self._plex_meta_by_path = {
            PLEX_AUDIO: {
                "title": "9 PM (Till I Come)", "artist": "ATB",
                "album": "Movin' Melodies",
            },
        }
        self.current_path = PLEX_AUDIO
        self.recently_played_tracker = SimpleNamespace(
            path=PLEX_AUDIO, duration_seconds=210.0,
            listened_seconds=30.0, threshold_seconds=30.0, generation=1,
        )
        self.recently_played_entries = []
        self._log = lambda *a, **k: None
        self._refresh_recently_played_table = lambda: None
        self.recently_played_repository = SimpleNamespace(save=lambda entries: None)


def test_recently_played_entry_uses_real_plex_title_not_ratingkey():
    # Real-device evidence: player.log shows "Recently Played qualified"
    # firing for a currently-playing plex:// path -- this entry's title
    # must be the real song title, not the numeric ratingKey, exactly
    # like the now-playing label.
    harness = RecentlyPlayedEntryHarness()
    harness._record_recently_played_entry()
    assert harness.recently_played_entries[-1].title == "9 PM (Till I Come)"
    assert "151212" not in harness.recently_played_entries[-1].title


def test_activate_track_ui_still_falls_back_to_basename_when_truly_untagged():
    # Control: an identity with no metadata anywhere (real-world equivalent
    # of a brand new, never-fetched file) must still show *something*
    # rather than an empty label -- the basename fallback this replaces
    # must survive for that genuinely-untagged case.
    harness = ActivateTrackUiHarness()
    local_path = "F:/music/mystery_track.flac"
    harness._activate_track_ui(local_path, library_index=0)
    # info.title's own last-resort fallback (_load_cached_audio_tags'
    # cached(meta.get("title"), os.path.basename(path))) keeps the
    # extension -- matches test_local_path_never_consults_plex_fallback's
    # existing "track.flac" expectation for the identical untagged case.
    assert harness.now_playing_texts[-1] == "mystery_track.flac"


# -- Stage 3A-r3 real-device defect: title correct at activation, then
# overwritten to "Unknown" moments later ------------------------------------
#
# Bill's real r3 acceptance run: toast, library row, and Up Next all
# showed "Take On Me" / "A-Ha" correctly, but the top current-track
# header showed "Unknown". Root cause: _activate_track_ui's audio branch
# calls _queue_track_tags_async(path) right after showing the correct
# cached title -- that spawns a TrackTagLoadWorker which opens `path`
# with MutagenFile. For a plex:// identity that always fails, but
# read_full_tag_display's failure path returns a *successful* all-Unknown
# dict rather than raising, so the async result still arrives and
# _on_track_tags_ready overwrites the correct title with "Unknown". This
# harness binds the REAL _queue_track_tags_async/_on_track_tags_ready
# (previous tests in this file stub _queue_track_tags_async as a no-op,
# which is exactly why they could not have caught this) and drives the
# actual sequence: activate -> (real async tag machinery would run) ->
# simulated subsequent UI/timer updates.

class RealAsyncActivateTrackUiHarness(ActivateTrackUiHarness):
    _queue_track_tags_async = PlayerWindow._queue_track_tags_async
    _on_track_tags_ready = PlayerWindow._on_track_tags_ready

    def __init__(self):
        super().__init__()
        del self._queue_track_tags_async  # uncover the real, class-level method
        self._closing = False
        self._playback_generation = 1
        self._track_tag_load_workers = []
        self._worker_registry = SimpleNamespace(
            register=lambda *a, **kw: "token", unregister=lambda token: None,
        )


def test_real_sequence_plex_title_survives_subsequent_async_tag_machinery(monkeypatch):
    # No TrackTagLoadWorker may even be constructed for the Plex identity
    # -- if the fix regresses, this fails loudly instead of masking the
    # bug behind a mock that happily returns fake "real" data.
    def _exploding_worker(*a, **kw):
        raise AssertionError(
            "TrackTagLoadWorker must never be constructed for a plex:// "
            "identity during real track activation"
        )
    monkeypatch.setattr(window_module, "TrackTagLoadWorker", _exploding_worker)

    harness = RealAsyncActivateTrackUiHarness()
    harness._plex_meta_by_path[PLEX_AUDIO] = {
        "title": "Take On Me", "artist": "A-Ha", "album": "Hunting High and Low",
    }

    harness._activate_track_ui(PLEX_AUDIO, library_index=0)  # must not raise

    assert harness.now_playing_texts[-1] == "Take On Me"
    assert harness._track_tag_load_workers == []

    # "Run normal subsequent UI/timer updates" -- a few more ticks of
    # whatever periodic machinery exists must not disturb it either, since
    # nothing was ever queued that could.
    for _ in range(3):
        pass  # no periodic hook under test here re-reads title; nothing to tick

    assert harness.now_playing_texts[-1] == "Take On Me"


def test_real_sequence_local_track_title_still_gets_the_async_refresh(monkeypatch):
    # Control: proves the harness's real _queue_track_tags_async wiring
    # genuinely starts a worker for Local (unlike the Plex case above),
    # so the assertion above is meaningful and not vacuous.
    started = []

    class _FakeWorker:
        def __init__(self, path):
            self.path = path
            self.tags_ready = SimpleNamespace(connect=lambda slot: None)
            self.finished = SimpleNamespace(connect=lambda slot: None)

        def start(self):
            started.append(self.path)

    monkeypatch.setattr(window_module, "TrackTagLoadWorker", _FakeWorker)
    harness = RealAsyncActivateTrackUiHarness()
    local_path = "F:/music/Take On Me.flac"

    harness._activate_track_ui(local_path, library_index=0)

    assert started == [local_path]
