"""Stage 3A real-device bug (item 3), window.py dispatch layer:
_analyzer_tick must route Plex-audio-via-BASS to the new live
BassPlayer.get_fft_levels() feed, and leave every other case (Local audio,
Plex not yet playing through BASS, video) going through the existing,
unchanged offline AudioAnalyzer.get_levels(t) path. The BASS FFT
mechanism itself is proven for real against the vendored bass.dll in
test_bass_fft_visualiser.py; this file is purely about which path
_analyzer_tick picks."""
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from billsmusic.media_type import MediaType
from billsmusic.window import PlayerWindow

PLEX_AUDIO = "plex://server-1/42.mp3"
LOCAL_AUDIO = "F:/music/track.mp3"


class AnalyzerTickHarness:
    _analyzer_tick = PlayerWindow._analyzer_tick
    _player_clock_s = PlayerWindow._player_clock_s
    _use_builtin_player = PlayerWindow._use_builtin_player
    _use_bass_backend = PlayerWindow._use_bass_backend

    def __init__(self):
        self.cast_active = False
        self._current_media_type = MediaType.AUDIO
        self.current_path = LOCAL_AUDIO
        self._library_apply_started = False
        self._sync_mini_player = lambda: None
        self._sync_party_mode = lambda: None
        self._update_recently_played_tracking = lambda: None
        self._lyrics_tick = lambda: None
        self._sync_karaoke_position = lambda: None
        self._check_playback_health = lambda: None
        self._maybe_tick_queue_duration_refresh = lambda: None

        self.builtin_backend = "bass"
        self.use_simple = True
        self._temporary_backend_override = None
        self._simple_fallback_active = False
        self.fade_active = False
        self.prebuffer_active = False
        self.simple_player = MagicMock(name="simple_player")
        self.simple_player.is_playing.return_value = True
        self.simple_player.get_pos.return_value = 5.0
        self.simple_inactive_player = None
        self.active_player = None

        self.analyzer = SimpleNamespace(
            bars=32, last_rms_db=-20.0,
            get_levels=MagicMock(return_value=[0.1] * 32),
        )
        self.analyzer_worker = None
        self.analyzer_time_offset_ms = 0
        self._clock_anchor = None
        self._last_clock_s = 0.0
        self._last_analysis_t = 0.0

        self.beat = SimpleNamespace(setLevels=MagicMock())
        self.party_mode = None
        self.viz_logger = SimpleNamespace(active=False, log_frame=lambda *a: None)
        self._closing = False


def test_plex_audio_via_bass_uses_live_fft_not_offline_analyzer():
    harness = AnalyzerTickHarness()
    harness.current_path = PLEX_AUDIO
    fft_levels = [0.2] * 32
    harness.simple_player.get_fft_levels.return_value = fft_levels

    harness._analyzer_tick()

    harness.simple_player.get_fft_levels.assert_called_once_with(32)
    harness.analyzer.get_levels.assert_not_called()
    harness.beat.setLevels.assert_called_once_with(fft_levels)


def test_local_audio_still_uses_offline_analyzer_unchanged():
    harness = AnalyzerTickHarness()
    harness.current_path = LOCAL_AUDIO

    harness._analyzer_tick()

    harness.analyzer.get_levels.assert_called_once()
    harness.simple_player.get_fft_levels.assert_not_called()
    harness.beat.setLevels.assert_called_once_with([0.1] * 32)


def test_plex_audio_on_vlc_backend_falls_back_to_offline_path_not_crash():
    # No BASS active -- Plex audio wouldn't even be playing through this
    # path (Decision 2 already blocks that), but _analyzer_tick must
    # still degrade safely rather than assume simple_player has
    # get_fft_levels.
    harness = AnalyzerTickHarness()
    harness.current_path = PLEX_AUDIO
    harness.use_simple = False
    harness.active_player = MagicMock()
    harness.active_player.is_playing.return_value = True
    harness.active_player.get_time.return_value = 5000

    harness._analyzer_tick()

    harness.analyzer.get_levels.assert_called_once()


def test_plex_video_current_never_touches_fft_path():
    harness = AnalyzerTickHarness()
    harness.current_path = PLEX_AUDIO  # queue identity may still be set
    harness._current_media_type = MediaType.VIDEO
    harness._video_backend = MagicMock()
    harness._video_backend.is_playing.return_value = True
    harness._video_backend.position_ms.return_value = 5000

    harness._analyzer_tick()

    harness.simple_player.get_fft_levels.assert_not_called()


def test_no_fft_data_yet_leaves_levels_unset_without_error():
    harness = AnalyzerTickHarness()
    harness.current_path = PLEX_AUDIO
    harness.simple_player.get_fft_levels.return_value = None

    harness._analyzer_tick()  # must not raise

    harness.beat.setLevels.assert_not_called()


# -- review gate 6: mid-session source switching -----------------------------

def test_mid_session_switch_from_plex_to_local_returns_to_offline_analyzer():
    # One continuous harness/session -- _analyzer_tick is purely stateless
    # per call (reads current_path/media_type fresh every time), so a
    # track change mid-session must immediately pick the other path with
    # no leftover state from the previous track.
    harness = AnalyzerTickHarness()
    harness.current_path = PLEX_AUDIO
    harness.simple_player.get_fft_levels.return_value = [0.3] * 32

    harness._analyzer_tick()
    harness.simple_player.get_fft_levels.assert_called_once_with(32)
    harness.analyzer.get_levels.assert_not_called()

    # Track changes to a Local file (the queue advanced, or the user
    # picked something else) -- nothing about this call site needs to
    # "reset" anything; the branch condition itself already changes.
    harness.current_path = LOCAL_AUDIO
    harness._analyzer_tick()

    harness.analyzer.get_levels.assert_called_once()  # now used, for the first time
    assert harness.simple_player.get_fft_levels.call_count == 1  # not called again


def test_mid_session_switch_from_local_to_plex_selects_live_fft():
    harness = AnalyzerTickHarness()
    harness.current_path = LOCAL_AUDIO

    harness._analyzer_tick()
    harness.analyzer.get_levels.assert_called_once()
    harness.simple_player.get_fft_levels.assert_not_called()

    harness.current_path = PLEX_AUDIO
    harness.simple_player.get_fft_levels.return_value = [0.4] * 32
    harness._analyzer_tick()

    harness.simple_player.get_fft_levels.assert_called_once_with(32)
    assert harness.analyzer.get_levels.call_count == 1  # not called again for the Plex tick


def test_cast_active_never_reads_local_bass_fft_even_for_a_plex_track():
    # Real-device bug found via the full regression suite: _analyzer_tick
    # has no top-level cast_active early return (that's _tick(), a
    # different function) -- it calls _player_clock_s() (which has its
    # own separate Cast-snapshot branch) and falls through to the same
    # "pick a levels source" logic regardless. Audio during Cast plays on
    # the Cast device, never through simple_player locally, so a Plex
    # track that happens to be current while casting must still use the
    # ordinary offline analyzer branch (matching every other non-BASS-
    # local case), never simple_player.get_fft_levels().
    harness = AnalyzerTickHarness()
    harness.current_path = PLEX_AUDIO
    harness.cast_active = True
    harness.cast_controller = SimpleNamespace(
        snapshot=lambda: {"state": "playing", "position": 12.0},
    )

    harness._analyzer_tick()  # must not raise even if current_path/backend look BASS-like

    harness.simple_player.get_fft_levels.assert_not_called()
    harness.analyzer.get_levels.assert_called_once()


# -- Stage 3A-r2 real-device defect: genuine integration, not isolated -------
#
# Bill's real acceptance run showed the live FFT visualiser going dead
# during real Plex playback despite every test above (and the isolated
# get_fft_levels() unit tests in test_bass_fft_visualiser.py) passing --
# because every test above replaces _check_playback_health with a no-op
# stub, so none of them can see what the REAL stall watchdog does to
# shared backend-selection state (_temporary_backend_override) before
# _analyzer_tick picks its FFT source. _check_playback_health actually
# runs from _tick (self.ui_timer, 200ms), a separate QTimer from
# self.analyzer_timer (16ms) that drives _analyzer_tick -- but they both
# read/write the *same* self._temporary_backend_override, so a 200ms
# _tick firing that corrupts it poisons every _analyzer_tick call until
# the next real track change. This harness binds the real, unbound
# _check_playback_health alongside the real _analyzer_tick and calls them
# in that same sequence -- one _tick-equivalent stall check, then several
# _analyzer_tick-equivalent frames -- so a regression in their interaction
# (not just either function in isolation) would actually be caught here.

class IntegratedTickHarness(AnalyzerTickHarness):
    _check_playback_health = PlayerWindow._check_playback_health
    _playback_health_snapshot = PlayerWindow._playback_health_snapshot
    _current_backend_name = PlayerWindow._current_backend_name

    def __init__(self):
        super().__init__()
        # AnalyzerTickHarness.__init__ sets an *instance* no-op stub for
        # _check_playback_health (to isolate _analyzer_tick's own dispatch
        # in the tests above) -- that instance attribute would otherwise
        # shadow this class's real, unbound method. Remove it so
        # self._check_playback_health() resolves to the real method.
        del self._check_playback_health
        self.auto_playback_recovery = True
        self._playback_recovery_active = False
        self._playback_intentionally_paused = False
        self.scrubbing = False
        self.pending_next = False
        self._playback_expected = True

        # Genuinely stall-shaped watchdog state -- old arm time, no
        # advance recorded -- the same shape real Plex network latency
        # produces while PlexPlaybackResolveWorker/PlexAudioLoadWorker are
        # still in flight. Proves the fix isn't "the watchdog never fires
        # for Plex because nothing looks stalled" but "the watchdog
        # refuses to act on Plex regardless of how stall-shaped things
        # look".
        started_long_ago = time.monotonic() - 10.0
        self._playback_watch_started = started_long_ago
        self._playback_watch_last_advance = started_long_ago
        self._playback_watch_last_position = 0.0
        self._playback_watch_has_advanced = False
        self._playback_watch_requested_position = 0.0

        self.recovery_calls = []

        def _fake_begin_playback_recovery(reason, backend, **kw):
            # Mocks only _begin_playback_recovery's own internals (its
            # BASS/VLC/miniaudio fallback ladder has its own dedicated
            # coverage in test_plex_stage3a_dispatch.py) while reproducing
            # the one real side effect this test is actually about: a
            # "successful" VLC fallback against a Plex identity that can't
            # really be opened leaves _temporary_backend_override stuck on
            # "vlc" -- see the matching comment in window.py's
            # _check_playback_health for why.
            self.recovery_calls.append((reason, backend))
            self._temporary_backend_override = "vlc"

        self._begin_playback_recovery = _fake_begin_playback_recovery


def test_integrated_tick_plex_bass_playback_survives_a_stall_shaped_watchdog_check():
    harness = IntegratedTickHarness()
    harness.current_path = PLEX_AUDIO
    # Genuinely playing (is_playing=True, real position) -- matching the
    # real bug exactly: BASS was audibly producing sound the whole time.
    # The watchdog's OWN bookkeeping (_playback_watch_last_position/
    # _last_advance) is what's stale here -- seeded to already match the
    # current position (so the "position just advanced" branch doesn't
    # reset it) with a last-advance timestamp far in the past, the same
    # shape real Plex network/position-reporting jitter produces. Before
    # the fix this alone was enough for _begin_playback_recovery to run
    # for a Plex identity.
    harness.simple_player.is_playing.return_value = True
    harness.simple_player.get_pos.return_value = 5.0
    harness.simple_player.get_length.return_value = 190.0
    harness._playback_watch_last_position = 5.0
    harness._playback_watch_has_advanced = True
    fft_levels = [0.35] * 32
    harness.simple_player.get_fft_levels.return_value = fft_levels

    # One _tick()-equivalent stall check (200ms QTimer, real method) that
    # finds the stall-shaped-but-genuinely-playing state above, followed
    # by several _analyzer_tick()-equivalent frames (separate 16ms
    # QTimer) -- the exact production sequencing: a single _tick misfire
    # would otherwise poison every _analyzer_tick call that follows it
    # until the next real track change.
    harness._check_playback_health()
    for _ in range(5):
        harness._analyzer_tick()

    # The stall-shaped watchdog check must never have run a recovery --
    # confirming _check_playback_health's own Plex guard, exercised for
    # real here rather than mocked out.
    assert harness.recovery_calls == []
    # And critically, nothing corrupted _temporary_backend_override in the
    # process -- the FFT dispatch keeps choosing the live BASS feed on
    # every single tick, never silently falling back to the offline
    # analyzer partway through.
    assert harness._temporary_backend_override is None
    assert harness.simple_player.get_fft_levels.call_count == 5
    harness.analyzer.get_levels.assert_not_called()
    assert harness.beat.setLevels.call_args_list[-1].args[0] == fft_levels


# -- Stage 3A-r4 real-device defect: analyzer_worker is a persistent
# QThread ------------------------------------------------------------------
#
# Every test above (including IntegratedTickHarness's real _check_
# playback_health integration) leaves harness.analyzer_worker = None,
# which makes the Plex live-FFT branch trivially reachable regardless of
# dispatch ordering -- it does NOT model the real application, where
# AnalyzerWorker is constructed and .start()ed once during startup (see
# _startup_restore_library) and stays a running QThread for virtually the
# entire session. The old production ordering checked
# analyzer_worker.isRunning() BEFORE the Plex branch, so in the real app
# that check was almost always True and the Plex branch was never
# reached -- exactly why the real Plex visualiser stayed dead despite
# every isolated test (this file's own, and test_bass_fft_visualiser.py's)
# passing.

def test_plex_audio_with_a_persistently_running_analyzer_worker_still_uses_live_fft():
    harness = AnalyzerTickHarness()
    harness.current_path = PLEX_AUDIO
    harness.analyzer_worker = MagicMock(name="analyzer_worker")
    harness.analyzer_worker.isRunning.return_value = True
    fft_levels = [0.6] * 32
    harness.simple_player.get_fft_levels.return_value = fft_levels

    harness._analyzer_tick()

    harness.simple_player.get_fft_levels.assert_called_once_with(32)
    harness.analyzer_worker.update_time.emit.assert_not_called()
    harness.analyzer.get_levels.assert_not_called()
    harness.beat.setLevels.assert_called_once_with(fft_levels)

    # Switch to Local audio in the SAME harness/session -- the Plex live
    # FFT path must stop and the existing (still-running, never stopped
    # or restarted) analyzer_worker path must resume.
    harness.current_path = LOCAL_AUDIO
    harness._analyzer_tick()

    harness.simple_player.get_fft_levels.assert_called_once()  # not called again
    harness.analyzer_worker.update_time.emit.assert_called_once()
    harness.analyzer.get_levels.assert_not_called()
    harness.analyzer_worker.isRunning.assert_called()  # never stopped/restarted -- always the same instance


def test_integrated_tick_plex_reverts_to_offline_analyzer_after_switch_to_local():
    # Companion control: the same real _check_playback_health/_analyzer_tick
    # pairing must still hand a Local track straight back to the existing,
    # unchanged offline analyzer once the Plex track ends and a Local one
    # becomes current -- proves the integration fix doesn't leak Plex-only
    # behaviour into Local playback.
    harness = IntegratedTickHarness()
    harness.current_path = PLEX_AUDIO
    harness.simple_player.get_fft_levels.return_value = [0.5] * 32
    harness._analyzer_tick()
    harness.simple_player.get_fft_levels.assert_called_once()

    harness.current_path = LOCAL_AUDIO
    harness.simple_player.is_playing.return_value = True
    harness.simple_player.get_pos.return_value = 5.0
    harness._analyzer_tick()

    harness.analyzer.get_levels.assert_called_once()
    assert harness.simple_player.get_fft_levels.call_count == 1  # not called again
    assert harness.recovery_calls == []


# -- Item 7: full Plex -> Local -> Plex integration, combining
# _activate_track_ui (waveform state, offline-analysis suppression) with
# _analyzer_tick (live-FFT-vs-offline dispatch) in one continuous session,
# proving neither a stale analyzer source nor a stale waveform request
# leaks across a track change in either direction. -------------------------

class _FakeTrackInfo:
    artist = "Some Artist"
    title = "Some Title"


class PlexLocalPlexHarness:
    _activate_track_ui = PlayerWindow._activate_track_ui
    _analyzer_tick = PlayerWindow._analyzer_tick
    _player_clock_s = PlayerWindow._player_clock_s
    _use_builtin_player = PlayerWindow._use_builtin_player
    _use_bass_backend = PlayerWindow._use_bass_backend

    def __init__(self):
        # -- _analyzer_tick dependencies (mirrors AnalyzerTickHarness, but
        # with a real, persistently-running analyzer_worker -- the real
        # application's actual shape, see the r4 tests above) --
        self.cast_active = False
        self._current_media_type = MediaType.AUDIO
        self.current_path = None
        self._library_apply_started = False
        self.builtin_backend = "bass"
        self.use_simple = True
        self._temporary_backend_override = None
        self._simple_fallback_active = False
        self.fade_active = False
        self.prebuffer_active = False
        self.simple_player = MagicMock(name="simple_player")
        self.simple_player.is_playing.return_value = True
        self.simple_player.get_pos.return_value = 5.0
        self.simple_inactive_player = None
        self.active_player = None
        self.analyzer = SimpleNamespace(
            bars=32, last_rms_db=-20.0,
            get_levels=MagicMock(return_value=[0.1] * 32),
            load=MagicMock(),
        )
        self.analyzer_worker = MagicMock(name="analyzer_worker")
        self.analyzer_worker.isRunning.return_value = True
        self.analyzer_time_offset_ms = 0
        self._clock_anchor = None
        self._last_clock_s = 0.0
        self._last_analysis_t = 0.0
        self.party_mode = None
        self.viz_logger = SimpleNamespace(
            active=False, log_frame=lambda *a: None, _track=None, stop=lambda: None,
        )
        self._closing = False

        # -- _activate_track_ui dependencies (mirrors
        # test_video_waveform_analysis_exclusion.py's _activate_ui_window) --
        self.current_index = None
        self._reset_recently_played_tracking = lambda path: None
        self._schedule_session_save = lambda: None
        self._set_playing_button_state = lambda: None
        self.quiet_count = 0
        self._last_quiet_debug_remaining = None
        self._reset_analyzer_clock = lambda: None
        self._select_tree_item = lambda path: None
        self.beat = SimpleNamespace(setPlaying=lambda v: None, setLevels=MagicMock())
        self._load_tags = lambda path: _FakeTrackInfo()
        self._load_cached_audio_tags = lambda path: _FakeTrackInfo()
        self._display_track_tags = lambda info, path: None
        self._queue_track_tags_async = lambda path: None
        self._record_recent_played = lambda path: None
        self._update_dj_info = lambda info, path: None
        self._load_lrc_for_track = MagicMock()
        self._clear_synced_lyrics_state = MagicMock()
        self._start_jukebox_intro = MagicMock()
        self.bio_worker = None
        self.waveform_seekbar = MagicMock()
        self.waveform_worker = MagicMock()
        self.now_playing = SimpleNamespace(setText=lambda text: None)
        self._sync_party_mode = lambda: None
        self._sync_now_playing_overlay_for_media_type = lambda: None


def test_plex_to_local_to_plex_no_stale_analyzer_or_waveform_source_leaks():
    harness = PlexLocalPlexHarness()

    # -- 1. Plex becomes current --
    harness._activate_track_ui(PLEX_AUDIO)
    harness.analyzer.load.assert_not_called()
    harness.analyzer_worker.update_track.emit.assert_not_called()
    harness.waveform_worker.request.assert_not_called()
    harness.waveform_seekbar.set_placeholder.assert_not_called()
    harness.waveform_seekbar.set_waveform.assert_called_once_with(None)  # unavailable immediately

    harness.simple_player.get_fft_levels.return_value = [0.2] * 32
    harness._analyzer_tick()
    harness.simple_player.get_fft_levels.assert_called_once_with(32)
    harness.analyzer.get_levels.assert_not_called()
    harness.analyzer_worker.update_time.emit.assert_not_called()

    # -- 2. switch to Local -- existing waveform/analyzer preparation must
    # resume completely unchanged, with no leftover Plex-only state.
    harness._activate_track_ui(LOCAL_AUDIO)
    harness.analyzer.load.assert_called_once_with(LOCAL_AUDIO)
    harness.analyzer_worker.update_track.emit.assert_called_once_with(LOCAL_AUDIO)
    harness.waveform_worker.request.assert_called_once_with(LOCAL_AUDIO)
    harness.waveform_seekbar.set_placeholder.assert_called_once_with(LOCAL_AUDIO)

    harness._analyzer_tick()
    harness.simple_player.get_fft_levels.assert_called_once()  # not called again
    harness.analyzer_worker.update_time.emit.assert_called_once()
    harness.analyzer.get_levels.assert_not_called()

    # -- 3. back to Plex -- must behave exactly like step 1 again, not
    # skip anything because it already ran once this session.
    harness._activate_track_ui(PLEX_AUDIO)
    assert harness.waveform_seekbar.set_waveform.call_count == 2  # unavailable again, immediately
    assert harness.analyzer.load.call_count == 1  # not called again
    assert harness.analyzer_worker.update_track.emit.call_count == 1  # not called again
    assert harness.waveform_worker.request.call_count == 1  # not called again

    harness.simple_player.get_fft_levels.return_value = [0.3] * 32
    harness._analyzer_tick()
    assert harness.simple_player.get_fft_levels.call_count == 2
    harness.analyzer.get_levels.assert_not_called()
    assert harness.analyzer_worker.update_time.emit.call_count == 1  # not called again
    assert harness.analyzer_worker.isRunning.call_count >= 1  # same instance throughout -- never stopped/restarted
