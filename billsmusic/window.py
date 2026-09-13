"""The main application window."""
import bisect
import dataclasses
import faulthandler
import gc
import os
import re
import json
import random
import tempfile
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict, Tuple, Any, cast

from PyQt6 import QtCore, QtGui, QtWidgets

pyqt_property = cast(Any, getattr(QtCore, "pyqtProperty"))

# VLC Python bindings may not be installed on all systems; guard usage
try:
    import vlc
    VLC_AVAILABLE = True
    VLC_IMPORT_ERROR = None
except Exception as ex:
    vlc = None
    VLC_AVAILABLE = False
    VLC_IMPORT_ERROR = ex

from mutagen import File as MutagenFile

# built-in audio players. VLC remains the fallback.
try:
    from .miniaudio_player import MiniaudioPlayer
except Exception as ex:
    MiniaudioPlayer = None
    MINIAUDIO_IMPORT_ERROR = ex
else:
    MINIAUDIO_IMPORT_ERROR = None

try:
    from .bass_player import BassPlayer, BassLoadError, bass_device_hash, _BassEngine
except Exception as ex:
    BassPlayer = None
    BassLoadError = RuntimeError
    bass_device_hash = None
    _BassEngine = None
    BASS_IMPORT_ERROR = ex
else:
    BASS_IMPORT_ERROR = None

from .config import (
    APP_TITLE,
    CROSSFADE_SECONDS, CROSSFADE_SECONDS_MIN, CROSSFADE_SECONDS_MAX,
    NORMAL_TRANSITION_EPSILON_SECONDS, TRACK_TRANSITION_MODES,
    PARTY_MODE_LAYOUTS, PARTY_MODE_VISUAL_QUALITIES,
    PARTY_MODE_UP_NEXT_COUNT_MIN, PARTY_MODE_UP_NEXT_COUNT_MAX,
    PARTY_MODE_AUTO_HIDE_SECONDS_MIN, PARTY_MODE_AUTO_HIDE_SECONDS_MAX,
    FADE_INTERVAL_MS, PREBUFFER_MS,
    VLC_LOCAL_FILE_CACHING_MS, VLC_NETWORK_CACHING_MS, VLC_NETWORK_SHARE_CACHING_MS,
    FADE_TRIGGER_DB, FADE_TRIGGER_WINDOW_SECONDS, FADE_QUIET_FRAMES,
    BIO_PAUSE_AFTER_LEAVE_SEC, BIO_PAUSE_AT_END_SEC,
    TAG_PAUSE_AFTER_LEAVE_SEC, TAG_PAUSE_AT_END_SEC,
    cache_file_path, config_file_path, session_file_path, recently_played_file_path, queue_analysis_cache_path, loudness_cache_path, load_config, album_cover_cache_path,
    video_transition_point_cache_path, plex_library_cache_path,
)
from .session import SessionError, clear_session_file, load_session_file, save_session_file
from .platform_utils import resource_path, is_frozen_build, current_executable_path
from .metadata import Track, read_cover_bytes, read_full_tag_display
from .cast_service import (
    CastDiscoveryService, CastPlaybackController, is_natural_completion,
)
from .media_server import LocalMediaServer, UnsupportedMediaError, AUDIO_TYPES
from .textfix import clean_meta_dict, clean_text
from .audio import AudioAnalyzer, AUDIO_ANALYSIS_AVAILABLE
from .widgets import MarqueeLabel, AutoScrollText, BeatWidget, ShimmerFrame
from .workers import AnalyzerWorker, BioWorker, SearchWorker, LibraryScanThread, QueueAnalysisWorker, QueueFolderDropWorker, BassStreamPrepareWorker, MiniaudioSourcePrepareWorker, KaraokePrepareWorker, AlbumTagRefreshWorker, TrackTagLoadWorker, LyricsLoadWorker, GainLookupWorker, CastPayloadWorker, AlbumArtFetchWorker, PlexConnectionTestWorker, PlexSignInWorker, PlexServerDiscoveryWorker, PlexConnectionResolveWorker, PlexLibraryFetchWorker, PlexPlaybackResolveWorker
from . import lyrics as lyrics_module
from . import __version__ as APP_VERSION, BUILD_IDENTITY_TAG
from . import _build_info
from .cdg import CdgWidget
from .vizlog import VizLogger
from .overlay import JukeboxOverlay, LYRIC_STYLES
from .loudness import LoudnessCache, calculate_gain, combine_volume, effective_mode, read_replaygain
from .loudness_worker import LoudnessAnalysisWorker
from .waveform_worker import WaveformWorker
from .waveform_widget import WaveformSeekBar
from .queue_undo import (
    UNDO_ACTION_LABELS, UNDO_STATUS_MESSAGES, GENERIC_UNDO_LABEL,
    capture_queue_undo, snapshot_queue_state, record_undo_snapshot_diagnostics,
)
from .queue_dedup import QueueAddOutcome, partition_incoming_batch
from .library_search import (
    LIBRARY_APPLY_MAX_ALBUMS,
    LIBRARY_APPLY_TIME_BUDGET_SECONDS,
    build_search_index,
    format_track_display_label,
    group_library_albums,
    group_row_label,
    library_apply_chunk_complete,
    normalise_search_text,
)
from .library_cache import (
    LIBRARY_CACHE_SCHEMA_VERSION,
    dedupe_meta_list_by_path,
    migrate_cache_meta_list,
    stable_folder_signature,
)
from .media_type import MediaType, classify_path
from .media_capabilities import (
    AUDIO_FILE_FILTER,
    is_audio,
    is_library_scannable,
    is_supported_media,
)
from .library_tab_state import LIBRARY_TAB_ALIASED_ATTRS, LibraryTabState
from .video_backend import GpuCompositorProbe, QtVideoPlaybackBackend
from .plex_preferences import (
    PlexPreferences,
    generate_client_identifier,
    generate_server_config_id,
    normalise_server_address,
)
from .plex_identity import is_plex_identity, parse_plex_identity
from .plex_metadata import plex_meta_list_to_queue_detail_cache
from .plex_transport import PlexTransportSource, sanitize_plex_text
from .playback_attempt import PlaybackAttempt, PlaybackAttemptState
from .player_lease import PlayerTargetLease
from .video_transition import (
    EFFECTS as VIDEO_TRANSITION_EFFECTS,
    TRANSITION_STYLES as VIDEO_TRANSITION_STYLES,
    VideoTransitionManager,
    VideoTransitionPreferences,
)
from .video_dual_transition import (
    DualTransitionPreferences,
    DualVideoTransitionEngine,
    GPU_TRANSITION_STYLES,
    SecondaryIdentity,
)
from .video_transition_point_analyzer import (
    VideoTransitionPointAnalyzer,
    VideoTransitionPointCache,
)
from .playback_recovery import (
    PLAYBACK_START_GRACE_SECONDS,
    STALL_CONFIRMATION_SECONDS,
    STALL_POSITION_TOLERANCE_SECONDS,
    is_backend_quarantined,
    ordered_recovery_backends,
    record_backend_failure,
    recovery_resume_position,
)
from .mini_player import MiniPlayerWindow
from .party_mode import PartyModeWindow, format_screen_label
from .recently_played import (
    ListenTracker,
    RecentlyPlayedRepository,
    add_entry,
    make_entry,
)
from .performance_diagnostics import (
    EVENT_LOOP_PROBE_INTERVAL_MS,
    PLAYER_LOG_BACKUPS,
    PLAYER_LOG_BYTES,
    get_diagnostics,
)
from .artwork import ArtworkManager
from .worker_registry import WorkerLifetimeRegistry
from .now_playing import NowPlayingGeneration
from .playlist_repair import (
    PlaylistEntry,
    load_m3u,
    save_m3u,
    suggest_replacement,
)
from .queue_duration_estimator import (
    ActiveCrossfadeState,
    CurrentTrackState,
    QueueDurationEstimate,
    QueueTrackInfo,
    estimate_queue_duration,
    format_queue_duration_summary,
    format_queue_duration_tooltip,
    parse_time_field_seconds,
)
from .sleep_timer import (
    MAX_CUSTOM_MINUTES,
    MIN_CUSTOM_MINUTES,
    PRESET_MINUTES,
    SleepTimerController,
)
from .visualiser_lifecycle import (
    VisualiserLifecycleController,
    VisualiserRunState,
    VisualiserTransition,
    should_visualiser_run,
)

SHORTCUTS = {
    "play_pause": "Space",
    "play_pause_alternate": "Ctrl+Space",
    "stop": "Ctrl+S",
    "previous": "Ctrl+Left",
    "next": "Ctrl+Right",
    "volume_up": "Ctrl+Up",
    "volume_down": "Ctrl+Down",
    "mute": "Ctrl+M",
    "focus_search": "Ctrl+F",
    "focus_library": "Ctrl+L",
    "focus_up_next": "Ctrl+U",
    "toggle_lyrics": "Ctrl+Shift+L",
    "toggle_visualiser": "Ctrl+Shift+V",
    "mini_player": "Ctrl+Shift+M",
    "party_mode": "Ctrl+Shift+P",
    "recently_played": "Ctrl+Shift+R",
    "undo_queue_change": "Ctrl+Z",
    "video_fullscreen": "F11",
}

SLEEP_TIMER_TICK_MS = 200

# Diagnostic only: if the GUI thread doesn't return to the event-loop probe
# (which fires every EVENT_LOOP_PROBE_INTERVAL_MS = 100ms) within this
# window, faulthandler dumps every thread's live Python stack -- pinpoints
# exactly what's running during the periodic ~250-300ms freezes reported in
# Party Mode, which sync_from_owner timing and a GC-pause probe have both
# already ruled out as their own cause.
STALL_DUMP_THRESHOLD_S = 0.15

# Bump this to trigger exactly one more automatic, quiet, full library
# rescan on next launch (e.g. when a new per-track metadata field is
# added that needs backfilling into already-cached tracks).
LIBRARY_METADATA_BACKFILL_VERSION = 1

# Version 1 changes the application-wide default from VLC/miniaudio to BASS.
# Existing installations have no way to distinguish an old inherited value
# from an explicit choice, so migrate once; subsequent user selections are
# preserved because _save_user_settings writes this version marker.
AUDIO_BACKEND_PREFERENCE_VERSION = 1

# Phase 2A (experimental dual-video cross-dissolve). Two compositors have
# been built; only one is ever reachable from this module:
#   - The CPU (QVideoFrame.paint()/QPainter) compositor reproducibly caused
#     a native access violation (confirmed with faulthandler, isolated
#     with standalone repro scripts) and is permanently unavailable. Its
#     code is kept in video_subprocess.py as the record of that
#     investigation, but no code path here ever requests "cpu" dual mode.
#   - The GPU (Qt Quick/RHI ShaderEffect) compositor passed extensive
#     standalone validation (15+ short runs, a 900s/240-transition soak
#     test with real music-video content, zero crashes -- see
#     CODEX_HANDOFF.md's "Phase 2A" sections) and is what this constant now
#     gates.
#
# This is the *compile-time* ceiling only -- "has a human reviewed the GPU
# path as safe to ever offer". It is not sufficient on its own to enable
# the feature: DUAL_VIDEO_TRANSITIONS_AVAILABLE and a *runtime* capability
# check (see _start_gpu_capability_probe/self._gpu_dual_capability) must
# both be true before the Preferences checkbox is enabled -- a machine
# with no real GPU, or a broken/ancient driver forcing Qt Quick's software
# rasterizer fallback, must fail closed to Phase 1 rather than expose a
# control that would just fail every time it's used.
DUAL_VIDEO_TRANSITIONS_AVAILABLE = True


def _dual_transition_unavailable_reason(gpu_capability: Optional[bool]) -> str:
    if not DUAL_VIDEO_TRANSITIONS_AVAILABLE:
        return (
            "(Experimental) Seamless dual-video cross-dissolve is not "
            "enabled in this build."
        )
    if gpu_capability is None:
        return (
            "(Experimental) Seamless dual-video cross-dissolve: checking "
            "GPU compositor availability -- reopen Preferences shortly."
        )
    return (
        "(Experimental) Seamless dual-video cross-dissolve is unavailable: "
        "the GPU compositor capability check did not succeed on this "
        "machine (no suitable GPU/driver, or Qt Quick fell back to its "
        "software renderer). See CODEX_HANDOFF.md's \"Phase 2A\" section."
    )


def resolve_audio_backend_preference(cfg: Dict[str, Any]) -> Tuple[str, bool]:
    try:
        preference_version = int(cfg.get("audio_backend_preference_version", 0))
    except (TypeError, ValueError):
        preference_version = 0
    if preference_version < AUDIO_BACKEND_PREFERENCE_VERSION:
        return "bass", True
    backend = str(cfg.get("audio_backend", "bass") or "bass").lower()
    if backend not in ("bass", "miniaudio"):
        backend = "bass"
    return backend, bool(cfg.get("use_simple_player", True))


def _is_network_file_path(path: str) -> bool:
    if not path:
        return False
    if path.startswith("\\\\"):
        return True
    drive, _ = os.path.splitdrive(path)
    if not drive:
        return False
    try:
        import ctypes
        drive_root = drive + "\\"
        return ctypes.windll.kernel32.GetDriveTypeW(drive_root) == 4  # DRIVE_REMOTE
    except Exception:
        return False


class SpinningToolButton(QtWidgets.QToolButton):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rotation = 0.0

    @pyqt_property(float)
    def rotation(self) -> float:
        return self._rotation

    @rotation.setter
    def rotation(self, value: float):
        self._rotation = value % 360.0
        self.update()

    def paintEvent(self, event):
        option = QtWidgets.QStyleOptionToolButton()
        self.initStyleOption(option)
        text = option.text
        option.text = ""

        painter = QtWidgets.QStylePainter(self)
        painter.drawComplexControl(QtWidgets.QStyle.ComplexControl.CC_ToolButton, option)
        painter.end()

        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(self.palette().buttonText().color())
        painter.setFont(self.font())
        center = self.rect().center()
        painter.translate(center)
        painter.rotate(self._rotation)
        painter.translate(-center)
        painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, text)
        painter.end()


class DiagnosticExportWorker(QtCore.QThread):
    completed = QtCore.pyqtSignal(str)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, diagnostics, destination, player_log_directory, config, details):
        super().__init__()
        self.diagnostics = diagnostics
        self.destination = destination
        self.player_log_directory = player_log_directory
        self.config = config
        self.details = details

    def run(self):
        try:
            result = self.diagnostics.export_bundle(
                self.destination,
                player_log_directory=self.player_log_directory,
                config=self.config,
                application_details=self.details,
            )
            self.completed.emit(result)
        except Exception as ex:
            self.failed.emit(str(ex))


class PlaylistLoadWorker(QtCore.QThread):
    completed = QtCore.pyqtSignal(str, object, object, float)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, filename):
        super().__init__()
        self.filename = filename

    def run(self):
        started = time.perf_counter()
        try:
            entries, counts = load_m3u(self.filename)
            self.completed.emit(
                self.filename,
                entries,
                counts,
                (time.perf_counter() - started) * 1000.0,
            )
        except Exception as ex:
            self.failed.emit(str(ex))


class QueueListWidget(QtWidgets.QListWidget):
    """A QListWidget that additionally accepts external file/folder drops
    (from Explorer, etc.) without disturbing its existing InternalMove
    drag-to-reorder behavior."""

    filesDropped = QtCore.pyqtSignal(list)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and event.source() is not self:
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls() and event.source() is not self:
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls() and event.source() is not self:
            paths = [
                url.toLocalFile()
                for url in event.mimeData().urls()
                if url.isLocalFile()
            ]
            event.acceptProposedAction()
            if paths:
                self.filesDropped.emit(paths)
            return
        super().dropEvent(event)


class PlayerWindow(QtWidgets.QMainWindow):
    startup_ready = QtCore.pyqtSignal()
    startup_failed = QtCore.pyqtSignal(str)
    first_paint = QtCore.pyqtSignal()

    def __init__(self, startup_reporter=None):
        super().__init__()
        self._startup_reporter = startup_reporter
        self.diagnostics = get_diagnostics()
        self.diagnostics.gui_thread_id = threading.get_ident()
        self.diagnostics.set_state_provider(self._diagnostic_state)
        self._record_build_identity()
        self._diagnostic_export_worker = None
        self._diagnostic_recovery_id = None
        self._last_completed_playback_generation = None
        self._diagnostic_crossfade_id = None
        self._diagnostic_artwork_batches = 0
        self._diagnostic_artwork_decoded = 0
        self._diagnostic_artwork_total_ms = 0.0
        self._diagnostic_artwork_max_ms = 0.0
        self._event_loop_expected = None
        self._event_loop_probe_timer = None
        self._memory_probe_timer = None
        self._gc_pause_started = None
        self._fault_dump_file = None
        self._first_paint_emitted = False
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(QtGui.QIcon(resource_path(os.path.join("assets", "app_icon.ico"))))
        self.resize(980, 640)

        self.instance = None
        self.player_a = None
        self.player_b = None
        self.active_player = None
        self.inactive_player = None
        # debug steps (logged after UI built)

        self.tracks: List[str] = []
        self.current_index: Optional[int] = None
        self.current_path: Optional[str] = None
        # Detail workers capture this identity.  It advances only when the
        # application makes a different item authoritative/current.
        self._now_playing_generation = NowPlayingGeneration()
        self.track_index_by_path: Dict[str, int] = {}
        self.tree_item_by_path: Dict[str, QtWidgets.QTreeWidgetItem] = {}
        self.album_item_by_key: Dict[str, QtWidgets.QTreeWidgetItem] = {}
        self.album_key_by_path: Dict[str, str] = {}
        self.album_cover_cache: Dict[str, Optional[bytes]] = {}
        self.artwork_manager = ArtworkManager(self)
        self.artwork_manager.diagnostic.connect(self._on_artwork_diagnostic)
        self.album_covers_enabled = False
        self.album_cover_allowlist = set()
        self.queue: List[str] = []
        self.queue_played: List[bool] = []
        self.queue_playlist_entries: List[Optional[PlaylistEntry]] = []
        self._queue_undo_snapshot = None
        self._loaded_playlist_entries: List[PlaylistEntry] = []
        self._loaded_playlist_filename = None
        self._playlist_load_worker = None
        self._queue_drop_worker = None
        self.queue_move_anims = []
        self.queue_detail_cache: Dict[str, Dict[str, str]] = {}
        self._queue_duration_debounce_timer = QtCore.QTimer(self)
        self._queue_duration_debounce_timer.setSingleShot(True)
        self._queue_duration_debounce_timer.timeout.connect(
            self._refresh_queue_duration_summary_now
        )
        self._queue_duration_pending_reason = "structural_change"
        self._queue_duration_last_tick_monotonic = 0.0
        self._queue_duration_last_unknown_count: Optional[int] = None
        self._queue_duration_last_paused: Optional[bool] = None
        self._queue_duration_last_empty: Optional[bool] = None
        self._queue_duration_last_finish_minute: Optional[int] = None
        self.queue_analysis_cache: Dict[str, Dict[str, Any]] = {}
        self.queue_analysis_pending = set()
        # Which specific BPM/Key cells a spinner applies to, per path --
        # independent of queue_analysis_pending (which just tracks "some
        # worker job is in flight," metadata-only or real). Keeps a known
        # BPM from ever being replaced by a spinner just because Key (or
        # vice versa) is still being computed for the same row. Populated
        # only when a genuine BPM/Key job is dispatched (never for
        # metadata-only requests), with exactly the fields that job was
        # asked to compute; popped entirely once that job's result arrives.
        self.queue_bpm_key_pending_fields: Dict[str, set] = {}
        # Paths that got metadata-only treatment because video playback or
        # visualiser preparation made BPM/key analysis temporarily ineligible.
        # Re-requested once that competing work stops.
        self._deferred_bpm_key_paths: set = set()
        # Paths outside the current bounded priority window (current track
        # + next 3 Up Next) that got metadata-only treatment purely to
        # avoid analysing an entire freshly-queued batch at once. Promoted
        # to real analysis by _promote_priority_queue_analysis() once the
        # priority window shifts to include them.
        self._queue_priority_deferred_paths: set = set()
        self._legacy_queue_metadata_backlog = []
        self._queue_metadata_pending = None
        self.queue_analysis_worker = None
        self.queue_spinner_index = 0
        self._queue_row_widgets: Dict[str, List[Dict[str, Any]]] = {}
        self._meta_list: List[Dict[str, Any]] = []
        # _full_meta_list itself is a source-aware property (see its
        # definition near _apply_meta_list_to_library_tabs) -- this
        # assignment goes through its setter, initialising the "local"
        # backing store (library_source defaults to "local" until
        # _load_user_settings runs later in startup).
        self._full_meta_list: List[Dict[str, Any]] = []
        self._showing_full = False
        self._library_search_generation = 0
        self._library_search_index = []
        self._library_apply_on_finished = None
        # Frozen snapshot of the list a track was chosen from, used as the
        # Next/Previous fallback sequence -- deliberately independent of
        # self.tracks (the *currently displayed* tab's list) so switching
        # Music/Videos tabs mid-playback never changes what plays next.
        self._playback_context_paths: List[str] = []
        self._playback_context_index: Optional[int] = None
        self._meta_by_path: Dict[str, Dict[str, Any]] = {}
        self._search_results_active = False
        self._tree_updates_suspended = False
        self._tree_signals_were_blocked = False
        self._pending_selection_path = None
        self._pending_selection_album_key = None
        self._library_apply_generation = None
        self._library_apply_reason = None
        self._library_apply_trigger = None
        self._library_apply_correlation_id = None
        self._library_request_serial = 0
        self._last_library_request_signature = None
        self._retired_library_items = deque()
        self._retired_library_cleanup_timer = QtCore.QTimer(self)
        self._retired_library_cleanup_timer.timeout.connect(
            self._cleanup_retired_library_items
        )
        self._retired_library_cleanup_timer.setInterval(0)
        self._library_apply_chunk_size = LIBRARY_APPLY_MAX_ALBUMS
        self._library_apply_time_budget = LIBRARY_APPLY_TIME_BUDGET_SECONDS
        self._library_apply_started = 0.0
        self._library_apply_chunks = 0
        self._library_apply_longest_chunk_ms = 0.0
        self._library_apply_item_ms = 0.0
        self._library_apply_finalization_ms = 0.0
        self._library_apply_timer_ticks = 0
        self._library_apply_token = 0
        self._library_finalize_scheduled = False
        self._library_apply_album_population_ms = 0.0
        self._library_apply_initial_albums_populated = 0
        self._library_apply_initial_track_rows = 0
        self._lazy_albums_populated = 0
        self._lazy_track_rows_created = 0
        self._library_cached_artwork_only = False
        self._startup_ready_emitted = False
        self._startup_failed_emitted = False
        self._startup_warning = ""
        self._startup_warning_box = None
        self.recent_played: List[str] = []
        self.bio_detail_mode = "detailed"
        self._build_queue = []
        self._build_playlist = []
        self._cover_queue = []
        self._build_timer = QtCore.QTimer(self)
        self._build_timer.timeout.connect(self._tree_build_tick)
        self._build_timer.setInterval(0)
        self._cover_timer = QtCore.QTimer(self)
        self._cover_timer.timeout.connect(self._cover_build_tick)
        self._populate_queue = []
        self._populate_timer = QtCore.QTimer(self)
        self._populate_timer.timeout.connect(self._populate_album_tick)
        self._populate_timer.setInterval(0)
        self.master_volume = 70
        self._volume_before_mute = self.master_volume
        self._muted = False
        self._last_accessible_announcement = ""
        self._last_search_announcement = ""
        self._last_progress_length_ms = 0
        self._last_progress_current_ms = 0
        self.mini_player = None
        self.mini_player_geometry = {"x": 80, "y": 80, "width": 420, "height": 190}
        self.mini_player_always_on_top = False
        self.party_mode = None
        self.party_mode_screen_name = ""
        self.party_mode_default_layout = "lyrics"
        self.party_mode_show_up_next = True
        self.party_mode_up_next_count = 3
        self.party_mode_show_clock = True
        self.party_mode_show_remaining_playlist_time = False
        self.party_mode_auto_hide_ms = 3000
        self.party_mode_animations_enabled = True
        self.party_mode_visual_quality = "medium"
        startup_started = time.perf_counter()
        self.recently_played_repository = RecentlyPlayedRepository(
            recently_played_file_path(), self._log
        )
        self.recently_played_entries = []
        self.recently_played_tracker = ListenTracker()
        self.fade_active = False
        self.fade_start = 0.0
        self.fade_direction = 1
        self.fade_from: Tuple[float, float] = (1.0, 0.0)
        self.pending_next = False
        self.prebuffer_active = False
        self.pending_builtin_crossfade_index = None
        self.pending_builtin_crossfade_path = None
        self.pending_builtin_crossfade_quiet = False
        self._builtin_fade_generation = 0
        self._crossfade_load_worker = None
        self._crossfade_load_token = 0
        self._pending_crossfade_immediate = False
        self.scrubbing = False
        self.fade_waits = 0
        self.quiet_count = 0
        self._last_quiet_debug_remaining = None
        self.analyzer = AudioAnalyzer() if AUDIO_ANALYSIS_AVAILABLE else None
        self.analyzer_time_offset_ms = 0
        self.viz_logger = VizLogger(bars=32)
        self._log_next_path = None   # set when user picks "Log visualiser data"
        self._lyrics = []
        self._lyric_times = []
        self._lyric_idx = None
        self.lyric_time_offset_ms = 0
        self.lyrics_enabled = True
        self.lyric_visual_style = "neon"
        self.scan_dialog = None
        self.scan_phase_label = None
        self.scan_detail_label = None
        self.scan_progress_bar = None
        self._last_scan_ui_update = 0.0
        self._closing_scan_dialog = False
        # BASS is the preferred audio player. VLC remains a fallback for
        # unsupported files or recovery after a backend failure.
        self.use_simple = True
        self.builtin_backend = "bass"
        self.miniaudio_player = None
        self.miniaudio_inactive_player = None
        self.bass_player = None
        self.bass_inactive_player = None
        self.simple_player = None
        self.simple_inactive_player = None
        # Phase C1 (native audio backend ownership): bumped by
        # _set_player_topology()/_promote_inactive_player() every time the
        # physical objects behind simple_player/simple_inactive_player
        # actually change. A PlayerTargetLease captured at async-dispatch
        # time is only valid at commit time if this still matches.
        self._player_topology_epoch = 0
        self._simple_fallback_active = False
        self.auto_playback_recovery = True
        self.allow_backend_fallback = True
        self.warn_before_adding_duplicate_queue_tracks = True
        self._playback_generation = 0
        # Playback stability hardening, Phase A (BMP-002): the umbrella
        # attempt-authority layer above _playback_generation and every
        # other specialist token below -- see playback_attempt.py's own
        # module docstring for the full invariant. _next_playback_attempt_id
        # is a plain monotonically-increasing counter (never reused,
        # never reset except at construction) -- attempt_id 0 is
        # reserved/never issued so a bare `if attempt_id:` reads
        # naturally in callers that only care "is this real".
        self._next_playback_attempt_id = 1
        self._current_playback_attempt: Optional[PlaybackAttempt] = None
        # Stage 3A: Plex Direct Play resolution/load are both async
        # (server metadata fetch, DNS, and the BASS/Qt stream-open call
        # must never block the GUI thread -- see _play_plex_audio_path_direct/
        # _play_plex_video_path_direct). generation reuses the same
        # _playback_generation staleness token every other async playback
        # path in this class already relies on (_begin_playback_recovery,
        # _crossfade_load_is_current, ...) -- any later play action, Plex
        # or local, bumps it and makes an in-flight resolve/load result a
        # no-op when it eventually arrives.
        self._plex_resolve_worker = None
        self._plex_resolve_pending_generation = None
        self._plex_resolve_pending_kind = None
        self._plex_resolve_pending_index = None
        self._plex_audio_load_worker = None
        self._plex_audio_load_token = 0
        self._playback_recovery_active = False
        self._playback_expected = False
        self._playback_intentionally_paused = False
        self._playback_watch_started = 0.0
        self._playback_watch_last_advance = 0.0
        self._playback_watch_last_position = 0.0
        self._playback_watch_requested_position = 0.0
        self._playback_watch_has_advanced = False
        self._playback_recovery_attempts: Dict[str, int] = {}
        self._backend_failure_times: Dict[str, List[float]] = {}
        self._backend_quarantined_until: Dict[str, float] = {}
        self._temporary_backend_override: Optional[str] = None
        self._closing = False
        # Phase C2 (worker lifetime / shutdown ownership, 2026-09-11):
        # four small, single-purpose shutdown-state flags -- see
        # closeEvent/_request_shutdown/_finalize_shutdown.
        #   _shutdown_requested: the first close request has been received
        #     and REQUEST-shutdown has run (or is running) -- guards the
        #     one-time UI/session teardown + _request_shutdown() from ever
        #     running a second time.
        #   _shutdown_pending: REQUEST-shutdown completed but one or more
        #     registered workers remain unresolved -- the close event is
        #     currently being ignored; the window stays alive and the
        #     normal Qt event loop keeps running until the last one
        #     resolves.
        #   _shutdown_finalizing: FINALIZE-shutdown (native player close,
        #     service teardown, diagnostics shutdown) is CURRENTLY
        #     executing -- guards _finalize_shutdown() against re-entrant
        #     execution.
        #   _shutdown_complete: FINALIZE-shutdown has actually run to
        #     completion -- only once this is True may closeEvent accept
        #     the close for real.
        self._shutdown_requested = False
        self._shutdown_pending = False
        self._shutdown_finalizing = False
        self._shutdown_complete = False
        self._worker_registry = WorkerLifetimeRegistry(diagnostics=self.diagnostics)
        self._visualiser_lifecycle = VisualiserLifecycleController("main_window")
        self._analyzer_feed_lifecycle = VisualiserLifecycleController("analyzer_feed")
        self.cast_active = False
        self.cast_volume = 50
        self._cast_devices = []
        self._cast_pending_snapshot = None
        self._cast_pending_device = None
        self._cast_completion_armed = False
        self._cast_last_state = "stopped"
        self._cast_loss_reported = False
        self._cast_artwork_temp = None
        self._cast_payload_cache: Dict[str, dict] = {}
        self._cast_artwork_paths: Dict[str, str] = {}
        self._cast_payload_generation = 0
        self._cast_payload_workers: list = []
        self._cast_clock_diagnostic_at = 0.0
        self.cast_discovery = CastDiscoveryService(self)
        self.cast_controller = CastPlaybackController(self)
        self.cast_media_server = LocalMediaServer(logger=self._log)
        self._visualiser_fullscreen = False
        # Per-session visualiser-fullscreen snapshot: exists only while a
        # visualiser fullscreen session is active, consumed and cleared on
        # exit (see _enter/_exit_visualiser_fullscreen). Deliberately
        # separate from the video side's own _video_fullscreen_restore_state
        # so neither mode's restore data can ever be applied to the other.
        self._visualiser_fullscreen_snapshot = None
        # Video playback state -- deliberately separate from
        # _visualiser_fullscreen. The one persistent native video surface is
        # routed only between the main and Party Mode hosts. Fullscreen is an
        # in-place presentation state of whichever host already owns it; the
        # cross-process native window is never reparented for fullscreen.
        self._current_media_type = MediaType.AUDIO
        self._karaoke_generation = 0
        self._karaoke_prepare_worker = None
        self._karaoke_audio_path = None
        self._karaoke_document = None
        # Whether _sync_now_playing_overlay_for_media_type() is the one
        # currently holding the overlay hidden for video/karaoke, and what
        # its visibility was immediately before that -- so returning to
        # audio restores exactly what was there before, rather than always
        # forcing it back on (see _sync_now_playing_overlay_for_media_type).
        self._now_playing_overlay_suppressed = False
        self._now_playing_overlay_was_visible = True
        self._video_fullscreen = False
        self._video_fullscreen_owner_widget = None
        self._video_fullscreen_restore_state = None
        # Fullscreen transition lifecycle (real-device crash, session
        # 38cddc05, 18:13:54.661): every input route -- the child process's
        # forwarded double-click/Escape (delivered inside a QProcess
        # readyReadStandardOutput callback), the main-window eventFilter
        # Escape, the container's own double-click, the context menu and
        # the keyboard action -- used to run the whole top-level
        # QMainWindow presentation change synchronously, inline in the
        # originating callback, with no guard against a second one
        # starting while the first was still physically restoring.
        # _video_fullscreen_transitioning marks a physical transition in
        # progress (nothing may nest inside it);
        # _video_fullscreen_pending_target coalesces whatever the latest
        # requested target is; the request itself is always applied from a
        # queued GUI turn so the input/IPC callback stack unwinds first.
        self._video_fullscreen_transitioning = False
        # The target of the physical transition currently executing (None
        # when idle) -- lets input like Escape act on the effective
        # fullscreen INTENT during an enter that hasn't committed yet,
        # without ever committing _video_fullscreen early.
        self._video_fullscreen_transition_target = None
        self._video_fullscreen_pending_target = None
        self._video_fullscreen_request_scheduled = False
        self._video_fullscreen_sequence = 0
        self._video_progress_started_at = 0.0
        self._video_progress_warning_reported = False
        self._video_timing_available_reported = False
        self.video_playback_enabled = True
        self.video_start_fullscreen = False
        self.video_return_to_normal_display_on_end = True
        transition_preferences = VideoTransitionPreferences()
        self.video_transitions_enabled = transition_preferences.enabled
        self.video_transition_style = transition_preferences.style
        self.video_transition_duration_seconds = transition_preferences.duration_seconds
        self.video_transition_automatic_lead_seconds = (
            transition_preferences.automatic_lead_seconds
        )
        self.video_transition_manual_duration_seconds = (
            transition_preferences.manual_duration_seconds
        )
        self.video_transition_enabled_effects = dict(
            transition_preferences.enabled_effects
        )
        # Phase 2A -- experimental genuine dual-video cross-dissolve (GPU
        # compositor). Forced off regardless of the preferences dataclass
        # default until DUAL_VIDEO_TRANSITIONS_AVAILABLE and a runtime
        # capability probe both agree -- see that constant's docstring
        # comment and _start_gpu_capability_probe below.
        dual_transition_preferences = DualTransitionPreferences()
        self.video_dual_transitions_enabled = (
            DUAL_VIDEO_TRANSITIONS_AVAILABLE and dual_transition_preferences.enabled
        )
        # Phase 2B -- which genuine GPU transition effect to use (or
        # GPU_RANDOM_STYLE to pick one per transition). Only takes effect
        # when video_dual_transitions_enabled is actually on; see
        # _apply_gpu_dual_mode_state.
        self.video_gpu_transition_effect = dual_transition_preferences.gpu_effect
        # Smart Video Transition Points -- brand new, real-device-
        # unverified feature; all three default off regardless of what a
        # future preferences default might become, matching the same
        # forced-off-until-proven pattern above. Only ever has an effect
        # when video_dual_transitions_enabled is also on -- see
        # _apply_gpu_dual_mode_state.
        self.video_smart_transition_points_enabled = False
        self.video_avoid_black_outros = dual_transition_preferences.avoid_black_outros
        self.video_skip_black_intros = dual_transition_preferences.skip_black_intros
        self.video_crossfade_audio_enabled = dual_transition_preferences.crossfade_video_audio_enabled
        self.video_crossfade_audio_curve = dual_transition_preferences.audio_crossfade_curve
        self.video_dual_preload_lead_seconds = dual_transition_preferences.preload_lead_seconds
        self.video_dual_ready_timeout_ms = dual_transition_preferences.ready_timeout_ms
        self.video_dual_preload_max_wait_ms = dual_transition_preferences.preload_max_wait_ms
        self.video_dual_preload_progress_extension_ms = (
            dual_transition_preferences.preload_progress_extension_ms
        )
        # None = not yet probed this session; True/False = cached probe
        # result. Never re-probed once set (see _start_gpu_capability_probe).
        self._gpu_dual_capability: Optional[bool] = None
        self._gpu_dual_probe = None
        self._dual_transition_promoted_path = None
        # Bumped by every queue-structure mutation (see queue_undo.py's
        # capture_queue_undo and _insert_unplayed_queue_item/
        # _move_queue_row_to_bottom below) -- lets a dual-transition preload
        # notice its target row no longer means what it did when preload
        # began, without needing real per-row queue IDs (the queue has none;
        # see CODEX_HANDOFF.md on why that's deliberately not being added
        # opportunistically here).
        self._queue_mutation_epoch = 0
        self._video_transition_manager = None
        self._video_backend = QtVideoPlaybackBackend(self)
        self._video_backend.started.connect(self._on_video_started)
        self._video_backend.paused.connect(self._on_video_paused)
        self._video_backend.end_of_media.connect(self._on_video_end_of_media)
        self._video_backend.error.connect(self._on_video_error)
        self._video_backend.position_changed.connect(self._on_video_position_changed)
        self._video_backend.duration_changed.connect(self._on_video_duration_changed)
        self._video_backend.secondary_ready.connect(self._on_video_secondary_ready)
        self._video_backend.secondary_failed.connect(self._on_video_secondary_failed)
        self._video_backend.secondary_preload_progress.connect(
            self._on_video_secondary_preload_progress
        )
        self._video_backend.dual_transition_complete.connect(
            self._on_video_dual_transition_complete
        )
        self._video_backend.audio_state_reported.connect(self._on_video_audio_state_reported)
        self._video_backend.dual_transition_failed.connect(
            self._on_video_dual_transition_failed
        )
        # Mouse/keyboard input over the video picture lands on the embedded
        # child process's own window, not on video_output_widget -- these
        # arrive forwarded over the backend's IPC protocol instead of via
        # the widget's own mouseDoubleClickEvent/keyPressEvent.
        # Requests only -- these are emitted from inside the backend's
        # QProcess readyReadStandardOutput read loop (see
        # video_backend._on_stdout_ready/_handle_event), so the actual
        # window transition must not run here; it is applied from a queued
        # GUI turn by _request_video_fullscreen_state.
        self._video_backend.double_clicked.connect(self._on_child_video_double_clicked)
        self._video_backend.escape_pressed.connect(self._on_child_video_escape_pressed)
        self._video_backend.context_menu_requested.connect(
            self._show_video_context_menu_at_cursor
        )
        self.library_font_family = "Segoe UI"
        self._vlc_error_shown = False
        self.normalisation_enabled = False
        self.normalisation_mode = "track"
        self.prevent_clipping = True
        self.auto_loudness_analysis = False
        self.target_lufs = -14.0
        self.tagged_preamp_db = 0.0
        self.untagged_preamp_db = 0.0
        self._active_normalisation_gain = 1.0
        self._inactive_normalisation_gain = 1.0
        # v1.0.70 crossfade/ReplayGain identity race fix: each slot's token
        # is bumped every time that slot is (re)assigned a track -- a
        # GainLookupWorker result is only ever applied to a slot if the
        # token it was dispatched under still matches that slot's *current*
        # token when the result arrives. See _cached_gain_for_path,
        # _set_slot_gain and _queue_gain_lookup_async.
        self._gain_token_seq = 0
        self._active_gain_token = 0
        self._inactive_gain_token = 0
        self.loudness_cache = LoudnessCache(loudness_cache_path())
        self.loudness_worker = None
        self._gain_snapshot_cache: Dict[str, Any] = {}
        self._gain_lookup_pending: set = set()
        self._gain_lookup_workers: list = []
        # path -> list of slot tokens awaiting that path's GainLookupWorker
        # result (fan-out: e.g. the active and inactive slots both loading
        # the same path get their own token, both resolved by one worker).
        self._gain_lookup_subscribers: Dict[str, list] = {}
        # v1.0.71 mixed-media (Audio<->Video) transition state. Deliberately
        # plain attributes (not a QObject/manager class) -- this sits
        # alongside the existing A-A crossfade fields and the video-video
        # VideoTransitionManager, never replacing either. See
        # _begin_mixed_media_transition and friends.
        self._mixed_transition_id = 0
        self._mixed_transition_state = "idle"  # "idle" | "preparing" | "active"
        self._mixed_transition_direction = None  # "audio_to_video" | "video_to_audio"
        self._mixed_transition_outgoing_path = None
        self._mixed_transition_incoming_path = None
        self._mixed_transition_incoming_row = None
        self._mixed_transition_reason = None
        self._mixed_transition_start = None
        self._mixed_transition_video_audio_scale = 0.0
        self._mixed_transition_gain_token = None
        self._mixed_transition_load_worker = None
        # v1.0.71 correction: which of the 25/50/75% audio-state diagnostic
        # checkpoints (see _probe_mixed_video_audio_state) have already
        # fired for the *current* Audio->Video transition -- reset per
        # transition in _begin_mixed_media_transition so a slow tick
        # crossing two thresholds between ticks doesn't skip one, and a
        # fast one doesn't re-fire the same threshold repeatedly.
        self._mixed_transition_audio_probe_done = set()
        # Long audio->video gap investigation (2026-08-28): request-time
        # anchor for _record_mixed_transition_gap_checkpoint's elapsed_ms.
        self._mixed_transition_requested_monotonic = None
        self.waveform_seekbar_enabled = True
        self.waveform_seekbar = None
        self.waveform_worker = None
        self.sleep_timer = SleepTimerController(clock=time.monotonic)
        self.sleep_timer_fade_pref = False
        self._sleep_timer_gain = 1.0
        self._sleep_timer_timer = None
        self._sleep_timer_custom_minutes = 30

        # Expensive startup work is split across event-loop turns so the
        # already-visible splash can paint each truthful milestone.
        self._schedule_startup_stage(self._startup_load_recently_played)

    def _record_build_identity(self) -> None:
        """v1.0.71 correction: real-device acceptance testing surfaced a
        case where the diagnostics from an "installed 1.0.71" build could
        not be trusted to actually contain a given round's source changes
        -- nothing on disk or in the logs said which exact build produced
        a given session. Low-volume, once per session, so the diagnostics
        from any future report can be matched against a specific build
        without guessing from filenames/version numbers alone (which are
        deliberately not bumped for an interim acceptance-retest build
        like this one -- see BUILD_IDENTITY_TAG's own docstring).

        Hardened further (2026-08-31 Codex audit, section 16): the fields
        above proved dist EXE == installed EXE (both hashable and
        comparable after the fact), but never that current source HEAD ->
        built EXE -- none of app_version/build_tag/frozen/basename/size/
        mtime are tied to an actual git commit. build_commit/tree_hash/
        build_uuid/build_timestamp_utc come from billsmusic/_build_info.py,
        generated once immediately before compilation by
        generate_build_info.py (see build_nuitka_installer.bat) and frozen
        into the build like any other source file -- never a live `git`
        call at frozen startup (explicitly required: it would need a
        working tree and add unpredictable startup latency/failure modes
        a packaged install has no business depending on). A *source* run
        has a real git checkout right there and nothing performance-
        sensitive about startup in the same way, so it additionally
        queries git directly, bounded and best-effort, purely to catch the
        common case of running from source without ever having run the
        build script (_build_info.py would otherwise still show the
        checked-in None placeholders)."""
        exe_path = current_executable_path()
        frozen = is_frozen_build()
        details: Dict[str, Any] = {
            "app_version": APP_VERSION,
            "build_tag": BUILD_IDENTITY_TAG,
            "frozen": frozen,
            "executable_basename": os.path.basename(exe_path) if exe_path else None,
            "build_commit": _build_info.BUILD_COMMIT,
            "build_commit_dirty": _build_info.BUILD_COMMIT_DIRTY,
            "build_tree_hash": _build_info.BUILD_TREE_HASH,
            "build_uuid": _build_info.BUILD_UUID,
            "build_timestamp_utc": _build_info.BUILD_TIMESTAMP_UTC,
        }
        if not frozen and _build_info.BUILD_COMMIT is None:
            details["source_run_git_commit"] = self._live_source_git_commit()
        try:
            stat = os.stat(exe_path)
            details["executable_size_bytes"] = stat.st_size
            details["executable_mtime_utc"] = (
                datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            )
        except OSError:
            details["executable_size_bytes"] = None
            details["executable_mtime_utc"] = None
        self.diagnostics.record(
            "startup", "build_identity", details=details, minimum_level="basic",
        )

    @staticmethod
    def _live_source_git_commit() -> Optional[str]:
        """Source-run-only fallback for build_identity -- see
        _record_build_identity's docstring for why this never runs for a
        frozen build. Bounded (2s timeout) and best-effort: any failure
        (git not on PATH, not a checkout, etc.) is reported as None, never
        raised into the startup path."""
        try:
            import subprocess
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                capture_output=True, text=True, timeout=2.0,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def _schedule_startup_stage(self, callback):
        if self._startup_ready_emitted or self._startup_failed_emitted:
            return
        QtCore.QTimer.singleShot(0, lambda: self._run_startup_stage(callback))

    def _run_startup_stage(self, callback):
        if self._startup_ready_emitted or self._startup_failed_emitted:
            return
        try:
            callback()
        except Exception as ex:
            detail = traceback.format_exc()
            self._log(f"Startup stage failed: {ex}\n{detail}")
            self._emit_startup_failed(str(ex))

    def _startup_load_recently_played(self):
        started = time.perf_counter()
        self.recently_played_entries = self.recently_played_repository.load()
        self._report_startup(
            "recently-played",
            "Loading Recently Played...",
            20,
            started,
        )
        self._schedule_startup_stage(self._startup_prepare_audio)

    def _startup_prepare_audio(self):
        started = time.perf_counter()
        self.miniaudio_player = MiniaudioPlayer() if MiniaudioPlayer else None
        self.miniaudio_inactive_player = MiniaudioPlayer() if MiniaudioPlayer else None
        self.bass_player = BassPlayer() if BassPlayer else None
        self.bass_inactive_player = BassPlayer() if BassPlayer else None
        # Phase C1: immutable physical identity, stamped once and never
        # reassigned -- unlike bass_player/bass_inactive_player/
        # simple_player/simple_inactive_player, which are all mutable role
        # labels that get repointed at these same 4 objects over the app's
        # lifetime (see _set_player_topology/_promote_inactive_player).
        for _player, _physical_id in (
            (self.bass_player, "bass-A"), (self.bass_inactive_player, "bass-B"),
            (self.miniaudio_player, "miniaudio-A"), (self.miniaudio_inactive_player, "miniaudio-B"),
        ):
            if _player is not None:
                _player.physical_id = _physical_id
        self._set_player_topology(self.miniaudio_player, self.miniaudio_inactive_player, reason="startup")
        self.loudness_worker = LoudnessAnalysisWorker()
        self.loudness_worker.result_ready.connect(self._on_loudness_result)
        _loudness_token = self._worker_registry.register(
            "loudness", cancel=self.loudness_worker.stop, thread=self.loudness_worker, wait_ms=3000,
        )
        self.loudness_worker.finished.connect(
            lambda w=self.loudness_worker, t=_loudness_token: self._on_simple_worker_finished("loudness_worker", w, t)
        )
        self.loudness_worker.start()
        try:
            cfg = load_config() or {}
        except Exception:
            cfg = {}
        self.waveform_seekbar_enabled = bool(cfg.get("waveform_seekbar_enabled", True))
        if self.waveform_seekbar_enabled:
            self.waveform_worker = WaveformWorker()
            self.waveform_worker.waveform_ready.connect(self._on_waveform_ready)
            self.waveform_worker.waveform_unavailable.connect(self._on_waveform_unavailable)
            _waveform_token = self._worker_registry.register(
                "waveform", cancel=self.waveform_worker.stop, thread=self.waveform_worker, wait_ms=3000,
            )
            self.waveform_worker.finished.connect(
                lambda w=self.waveform_worker, t=_waveform_token: self._on_simple_worker_finished("waveform_worker", w, t)
            )
            self.waveform_worker.start(QtCore.QThread.Priority.LowPriority)
        self._report_startup(
            "audio-backends",
            "Preparing audio backends...",
            32,
            started,
        )
        self._schedule_startup_stage(self._startup_build_interface)

    def _startup_build_interface(self):
        self._log("PlayerWindow startup: building UI")
        startup_started = time.perf_counter()
        self._build_ui()
        # Smart Video Transition Points: constructed before the engine that
        # consumes its lookups (see video_transition_point_analyzer.py's
        # module docstring). Mirrors self.loudness_cache's own
        # LoudnessCache(loudness_cache_path()) construction shape exactly.
        self._video_transition_point_cache = VideoTransitionPointCache(
            video_transition_point_cache_path()
        )
        self._video_transition_point_analyzer = VideoTransitionPointAnalyzer(
            self, self._video_transition_point_cache,
            diagnostic_callback=self._record_video_transition_event,
            path_hasher=lambda path: self.diagnostics.path_details(path).get("path_hash", ""),
        )
        dual_transition_engine = DualVideoTransitionEngine(
            self,
            self._video_backend,
            self._advance_video_transition,
            self._peek_next_queue_identity_for_dual_transition,
            self._record_video_transition_event,
            self._prepare_dual_transition_promotion,
            outro_transition_point_lookup=self._video_transition_point_analyzer.cached_outro_end_ms,
            intro_transition_point_lookup=self._video_transition_point_analyzer.cached_intro_start_ms,
            current_primary_path_provider=lambda: getattr(self, "current_path", None),
            staleness_identity_provider=self._peek_next_queue_identity_for_dual_transition_pure,
        )
        self._video_transition_manager = VideoTransitionManager(
            self,
            self._video_transition_host,
            self._advance_video_transition,
            self._peek_next_media_type_for_transition,
            self._record_video_transition_event,
            dual_engine=dual_transition_engine,
        )
        self._build_accessible_actions()
        self._configure_accessibility()
        self._log("PlayerWindow startup: connecting signals")
        self._connect_signals()
        self._report_startup(
            "interface",
            "Preparing the interface...",
            45,
            startup_started,
        )
        # Deliberately independent of the sequential startup-stage chain
        # above (never awaited, never blocks "ready") -- a throwaway child
        # process launch that only decides whether the Preferences
        # checkbox can ever be enabled. See DUAL_VIDEO_TRANSITIONS_AVAILABLE.
        QtCore.QTimer.singleShot(0, self._start_gpu_capability_probe)
        self._schedule_startup_stage(self._startup_load_preferences)

    def _startup_load_preferences(self):
        self._log("PlayerWindow startup: loading user settings")
        self._load_queue_analysis_cache()
        startup_started = time.perf_counter()
        self._load_user_settings()
        self._report_startup(
            "preferences",
            "Loading preferences...",
            55,
            startup_started,
        )
        self.session_save_timer = QtCore.QTimer(self)
        self.session_save_timer.setSingleShot(True)
        self.session_save_timer.setInterval(750)
        self.session_save_timer.timeout.connect(self._save_session)
        # v1.0.67: _store_queue_analysis used to call _save_queue_analysis_cache()
        # (a full JSON serialise+write of the whole, up-to-2000-entry cache)
        # unconditionally on every single completed background BPM/key
        # result -- debounced the same way session saves already are.
        self._queue_analysis_save_timer = QtCore.QTimer(self)
        self._queue_analysis_save_timer.setSingleShot(True)
        self._queue_analysis_save_timer.setInterval(750)
        self._queue_analysis_save_timer.timeout.connect(self._save_queue_analysis_cache)
        self._schedule_startup_stage(self._startup_restore_session)

    def _startup_restore_session(self):
        startup_started = time.perf_counter()
        self._load_session()
        self._report_startup(
            "session",
            "Restoring Up Next...",
            68,
            startup_started,
        )
        self._schedule_startup_stage(self._startup_restore_library)

    def _startup_restore_library(self):
        self.ui_timer = QtCore.QTimer(self)
        self.ui_timer.timeout.connect(self._tick)
        self.ui_timer.start(200)

        self.fade_timer = QtCore.QTimer(self)
        self.fade_timer.timeout.connect(self._fade_tick)
        self.fade_timer.start(FADE_INTERVAL_MS)

        if AUDIO_ANALYSIS_AVAILABLE:
            self.analyzer_timer = QtCore.QTimer(self)
            self.analyzer_timer.timeout.connect(self._analyzer_tick)
            self.analyzer_timer.start(16)
        else:
            self.analyzer_timer = None

        self.scan_thread = None
        self._album_tag_refresh_thread = None
        self._track_tag_load_workers = []
        self._lyrics_load_workers = []
        self._lyrics_cache: Dict[str, tuple] = {}
        self._album_art_fetch_workers = []
        self._track_info_panel_path = None
        self._backfill_scan_thread = None
        self._backfill_scan_total = 0
        self._backfill_scan_seen = 0
        self._backfill_scan_percent = None
        self._backfill_scan_cancelled_by_user_action = False
        self._backfill_last_ui_update = 0.0
        self._dj_info_base_text = ""
        startup_started = time.perf_counter()
        cache, cache_restore_failed = self._load_initial_library_cache()
        self._report_startup(
            "library-cache",
            "Restoring music library...",
            76,
            startup_started,
        )
        restoring_cached_library = bool(
            cache and isinstance(cache.get("meta"), list) and cache["meta"]
        )
        if restoring_cached_library:
            self._apply_meta_list_to_library_tabs(
                dedupe_meta_list_by_path(cache["meta"]),
                reason="startup_cache_restore",
                trigger_source="startup",
            )
            self._record_library_source_backing_state("after_startup_restore")
        elif not cache_restore_failed:
            self._start_scan()
            # _apply_meta_list_to_library_tabs already rebuilds the search
            # index itself when restoring from cache; only needed here for
            # the empty-library-so-far state while the scan is still running.
            self._rebuild_library_search_index()
        self._restore_plex_library_cache_at_startup()
        self._maybe_rehydrate_plex_queue_after_cache_restore()

        if AUDIO_ANALYSIS_AVAILABLE:
            self._visualiser_analysis_busy = False
            self.analyzer_worker = AnalyzerWorker()
            self.analyzer_worker.result_ready.connect(self._on_analyzer_result)
            self.analyzer_worker.analysis_busy.connect(self._on_analysis_busy)
            _analyzer_token = self._worker_registry.register(
                "analyzer", cancel=self.analyzer_worker.stop, thread=self.analyzer_worker, wait_ms=2000,
            )
            self.analyzer_worker.finished.connect(
                lambda w=self.analyzer_worker, t=_analyzer_token: self._on_simple_worker_finished("analyzer_worker", w, t)
            )
            self.analyzer_worker.start()
        else:
            self.analyzer_worker = None
        self._set_analyzer_mode("")

        self.bio_worker = BioWorker()
        self.bio_worker.bio_ready.connect(self._on_bio_ready)
        _bio_token = self._worker_registry.register(
            "bio", cancel=self.bio_worker.stop, thread=self.bio_worker, wait_ms=2000,
        )
        self.bio_worker.finished.connect(
            lambda w=self.bio_worker, t=_bio_token: self._on_simple_worker_finished("bio_worker", w, t)
        )
        self.bio_worker.start()
        # timers and workers that require UI widgets created in _build_ui
        self.search_timer = QtCore.QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(250)
        self.search_timer.timeout.connect(self._apply_search_pending)
        self._search_pending_text = ""
        self.search_worker = SearchWorker()
        self.search_worker.results_ready.connect(self._on_search_results)
        _search_token = self._worker_registry.register(
            "search", cancel=self.search_worker.stop, thread=self.search_worker, wait_ms=2000,
        )
        self.search_worker.finished.connect(
            lambda w=self.search_worker, t=_search_token: self._on_simple_worker_finished("search_worker", w, t)
        )
        self.search_worker.start()
        self.queue_analysis_worker = QueueAnalysisWorker()
        self.queue_analysis_worker.result_ready.connect(self._on_queue_analysis_ready)
        self.queue_analysis_worker.metadata_ready.connect(
            self._on_queue_metadata_ready
        )
        _queue_analysis_token = self._worker_registry.register(
            "queue_analysis", cancel=self.queue_analysis_worker.stop,
            thread=self.queue_analysis_worker, wait_ms=2000,
        )
        self.queue_analysis_worker.finished.connect(
            lambda w=self.queue_analysis_worker, t=_queue_analysis_token: self._on_simple_worker_finished("queue_analysis_worker", w, t)
        )
        self.queue_analysis_worker.start()
        self.startup_ready.connect(
            lambda: QtCore.QTimer.singleShot(
                1500, self._start_legacy_queue_enrichment
            )
        )
        self.startup_ready.connect(
            lambda: QtCore.QTimer.singleShot(
                3000, self._maybe_start_metadata_backfill
            )
        )
        # Deliberately deferred until well after the whole staged startup
        # sequence (and every other startup-time worker thread) has
        # settled, not fired immediately during _startup_load_preferences.
        # analysis_warmup.wait_until_ready() (called inside
        # PlexServerDiscoveryWorker.run()) is safe by construction once
        # the real app's own launcher has already completed
        # analysis_warmup.run_synchronously() before PlayerWindow ever
        # existed (see analysis_warmup.py's module docstring) -- but a
        # test constructing PlayerWindow() directly never does that, so
        # firing this too early made a *background* thread's first-ever
        # analysis_warmup.start() call race the same concurrent-native-
        # import hazard analysis_warmup.py already documents (confirmed
        # via a real access violation with a librosa import mid-flight
        # while several other startup-time worker threads were also
        # starting). Nothing about Plex startup validation needs this
        # early; 1500ms costs nothing for a background reconnect check.
        self.startup_ready.connect(
            lambda: QtCore.QTimer.singleShot(
                1500, self._start_plex_startup_validation
            )
        )
        self.queue_spinner_timer = QtCore.QTimer(self)
        self.queue_spinner_timer.timeout.connect(self._queue_spinner_tick)
        self.queue_spinner_timer.setInterval(350)
        self.bio_anim_timer = QtCore.QTimer(self)
        self.bio_anim_timer.timeout.connect(self._bio_anim_tick)
        self.bio_animating = False
        self.bio_anim_start = 0.0
        self.bio_pending_text = ""
        self._report_startup(
            "workers",
            "Starting background services...",
            88,
        )
        self._start_performance_probes()
        if not restoring_cached_library:
            QtCore.QTimer.singleShot(0, self._emit_startup_ready)

    def _load_initial_library_cache(self):
        try:
            return self._load_cache(), False
        except Exception as ex:
            detail = traceback.format_exc()
            self._log(
                f"Initial library cache restore failed: {ex}\n{detail}"
            )
            self._startup_warning = (
                "The saved music library could not be restored. "
                "Bills Music Player opened with an empty library; "
                "use Rescan Library to rebuild it."
            )
            return None, True

    def _diagnostic_state(self):
        return {
            "window_visible": self.isVisible(),
            "window_minimised": self.isMinimized(),
            "playback_expected": bool(getattr(self, "_playback_expected", False)),
            "visualiser_visible": bool(
                getattr(self, "visualiser_frame", None)
                and self.visualiser_frame.isVisible()
            ),
            "search_active": bool(getattr(self, "_search_results_active", False)),
            "library_restoring": bool(getattr(self, "_library_apply_started", 0)),
            "queue_size": len(getattr(self, "queue", [])),
            "queued_search": int(bool(
                getattr(getattr(self, "search_worker", None), "_dirty", False)
            )),
            "queued_analysis": len(
                getattr(getattr(self, "queue_analysis_worker", None), "_queue", [])
            ),
            "backend": getattr(self, "builtin_backend", "vlc"),
            "crossfade_active": bool(getattr(self, "fade_active", False)),
            "recovery_active": bool(
                getattr(self, "_playback_recovery_active", False)
            ),
        }

    def _start_performance_probes(self):
        if not self.diagnostics.level_at_least("basic"):
            return
        self._event_loop_expected = time.perf_counter() + (
            EVENT_LOOP_PROBE_INTERVAL_MS / 1000.0
        )
        self._event_loop_probe_timer = QtCore.QTimer(self)
        self._event_loop_probe_timer.setInterval(EVENT_LOOP_PROBE_INTERVAL_MS)
        self._event_loop_probe_timer.timeout.connect(self._event_loop_probe)
        self._event_loop_probe_timer.start()
        self._memory_probe_timer = QtCore.QTimer(self)
        self._memory_probe_timer.setInterval(45000)
        self._memory_probe_timer.timeout.connect(self._sample_resource_health)
        self._memory_probe_timer.start()
        self._install_gc_pause_probe()
        self._arm_stall_dump()

    def _restart_performance_probes(self):
        for timer in (self._event_loop_probe_timer, self._memory_probe_timer):
            if timer is not None:
                timer.stop()
        self._event_loop_probe_timer = None
        self._memory_probe_timer = None
        self._uninstall_gc_pause_probe()
        self._cancel_stall_dump()
        self._start_performance_probes()

    def _fault_dump_path(self):
        directory = getattr(self.diagnostics, "directory", None)
        if directory is None:
            return None
        return Path(directory) / "stall_traceback.log"

    def _arm_stall_dump(self):
        # See STALL_DUMP_THRESHOLD_S above: if the GUI thread misses two
        # consecutive event-loop probes, dump every thread's live stack to
        # stall_traceback.log so the next freeze report captures exactly
        # what the interpreter was doing, not just that it happened.
        if self._fault_dump_file is None:
            path = self._fault_dump_path()
            if path is None:
                return
            try:
                self._fault_dump_file = open(path, "a", encoding="utf-8")
            except OSError:
                return
        try:
            faulthandler.cancel_dump_traceback_later()
            faulthandler.dump_traceback_later(
                STALL_DUMP_THRESHOLD_S, exit=False, file=self._fault_dump_file,
            )
        except Exception:
            pass

    def _cancel_stall_dump(self):
        try:
            faulthandler.cancel_dump_traceback_later()
        except Exception:
            pass
        if self._fault_dump_file is not None:
            try:
                self._fault_dump_file.close()
            except Exception:
                pass
            self._fault_dump_file = None

    def _install_gc_pause_probe(self):
        # Diagnostic only: confirms/rules out Python's own garbage collector
        # as the source of the periodic ~250ms GUI freezes reported in Party
        # Mode. Those freezes recur on a suspiciously regular ~2s cadence,
        # untied to any instrumented operation (queue updates, biography
        # fetches, waveform decodes all landed at unrelated times) -- which
        # is consistent with a generational GC pass over the audio
        # analyzer's constant small-object churn (60Hz level updates).
        if self._gc_pause_probe in gc.callbacks:
            return
        gc.callbacks.append(self._gc_pause_probe)

    def _uninstall_gc_pause_probe(self):
        try:
            gc.callbacks.remove(self._gc_pause_probe)
        except ValueError:
            pass
        self._gc_pause_started = None

    def _gc_pause_probe(self, phase, info):
        if phase == "start":
            self._gc_pause_started = time.perf_counter()
            return
        started = self._gc_pause_started
        self._gc_pause_started = None
        if started is None:
            return
        duration_ms = (time.perf_counter() - started) * 1000.0
        if duration_ms < 5.0:
            return
        severity = (
            "critical" if duration_ms >= 500 else
            "severe" if duration_ms >= 150 else
            "warning" if duration_ms >= 50 else "notice"
        )
        self.diagnostics.record(
            "runtime", "gc_pause",
            duration_ms=duration_ms,
            severity=severity,
            details={
                "generation": info.get("generation"),
                "collected": info.get("collected"),
                "uncollectable": info.get("uncollectable"),
            },
            minimum_level="basic",
            rate_limit_seconds=1.0,
        )

    def _event_loop_probe(self):
        now = time.perf_counter()
        expected = self._event_loop_expected or now
        lag_ms = max(0.0, (now - expected) * 1000.0)
        self._event_loop_expected = now + EVENT_LOOP_PROBE_INTERVAL_MS / 1000.0
        self.diagnostics.observe_event_loop(lag_ms)
        self._arm_stall_dump()

    def _sample_resource_health(self):
        self.diagnostics.sample_memory({
            "library_rows": (
                self.tree_tracks.topLevelItemCount()
                if hasattr(self, "tree_tracks") else 0
            ),
            "queue_size": len(self.queue),
            "recently_played_count": len(self.recently_played_entries),
            "artwork_cache_count": len(self.album_cover_cache),
            "mini_player_exists": self.mini_player is not None,
        })

    def _report_startup(
        self, key, text, progress, started_at=None, duration_ms=None
    ):
        reporter = getattr(self, "_startup_reporter", None)
        if reporter is None:
            return
        if duration_ms is None and started_at is not None:
            duration_ms = (time.perf_counter() - started_at) * 1000.0
        try:
            reporter(key, text, progress, duration_ms)
        except Exception:
            pass

    def _emit_startup_ready(self):
        if self._startup_ready_emitted or self._startup_failed_emitted:
            return
        self._startup_ready_emitted = True
        self.startup_ready.emit()

    def _emit_startup_failed(self, message):
        if self._startup_ready_emitted or self._startup_failed_emitted:
            return
        self._startup_failed_emitted = True
        self.startup_failed.emit(str(message))

    def show_startup_warning(self):
        if not self._startup_warning or self._startup_warning_box is not None:
            return
        self.statusBar().showMessage(self._startup_warning, 15000)
        box = QtWidgets.QMessageBox(
            QtWidgets.QMessageBox.Icon.Warning,
            "Music Library",
            self._startup_warning,
            QtWidgets.QMessageBox.StandardButton.Ok,
            self,
        )
        box.setModal(False)
        box.finished.connect(lambda _result: setattr(
            self, "_startup_warning_box", None
        ))
        self._startup_warning_box = box
        box.open()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._first_paint_emitted:
            self._first_paint_emitted = True
            self.first_paint.emit()

    def _log(self, msg: str):
        """Append a simple message to a rolling log file.
        Used for debug info such as player backend toggles.
        """
        try:
            log_dir = os.path.join(os.environ.get("LOCALAPPDATA", os.getcwd()), "Bills Music Player")
            os.makedirs(log_dir, exist_ok=True)
            log_path = Path(log_dir) / "player.log"
            self.diagnostics._rotate(
                log_path, PLAYER_LOG_BYTES, PLAYER_LOG_BACKUPS
            )
            timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] {msg}\n")
        except Exception:
            pass

    def _schedule_session_save(self):
        timer = getattr(self, "session_save_timer", None)
        if timer is not None:
            timer.start()

    def _schedule_queue_analysis_cache_save(self):
        timer = getattr(self, "_queue_analysis_save_timer", None)
        if timer is not None:
            timer.start()

    def _save_session(self):
        started = time.perf_counter()
        try:
            self._ensure_queue_played_flags()
            save_session_file(
                session_file_path(),
                self.queue,
                self.queue_played,
                self.current_path,
            )
            self.diagnostics.record(
                "file_io", "save_session",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                details={"queue_size": len(self.queue)},
                minimum_level="detailed",
            )
        except Exception as ex:
            self._log(f"Session save failed: {ex}")
            self.diagnostics.record(
                "file_io", "save_session", status="failure",
                severity="warning",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                details={"exception": str(ex)},
                minimum_level="basic",
            )

    def _load_session(self):
        started = time.perf_counter()
        try:
            # Network-path validation and audio-tag reads made startup block
            # for many seconds. Playback still validates files when used.
            restored = load_session_file(
                session_file_path(), validate_paths=False
            )
        except SessionError as ex:
            self._log(f"Session restore skipped: {ex}")
            self.diagnostics.record(
                "file_io", "load_session", status="failure",
                severity="warning",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                details={"exception": str(ex)},
                minimum_level="basic",
            )
            return
        except Exception as ex:
            self._log(f"Session restore skipped: {ex}")
            self.diagnostics.record(
                "file_io", "load_session", status="failure",
                severity="warning",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                details={"exception": str(ex)},
                minimum_level="basic",
            )
            return
        if restored is None:
            self.diagnostics.record(
                "file_io", "load_session",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                details={"restored": False},
                minimum_level="detailed",
            )
            return
        self.queue, self.queue_played, self.current_path = restored
        self.queue_playlist_entries = [None] * len(self.queue)
        self._ensure_queue_played_flags()
        # This refresh occurs before queue-analysis workers exist and never starts playback.
        self._refresh_queue_list(
            cached_details_only=True, reason="startup_restore"
        )
        self.diagnostics.record(
            "file_io", "load_session",
            duration_ms=(time.perf_counter() - started) * 1000.0,
            details={"restored": True, "queue_size": len(self.queue)},
            minimum_level="detailed",
        )

    def _clear_saved_session(self):
        try:
            clear_session_file(session_file_path())
            timer = getattr(self, "session_save_timer", None)
            if timer is not None:
                timer.stop()
        except Exception as ex:
            self._log(f"Could not clear saved session: {ex}")
        
        

    def _wrap_shimmer(self, widget: QtWidgets.QWidget) -> QtWidgets.QWidget:
        frame = ShimmerFrame(widget)
        frame.setSizePolicy(widget.sizePolicy())
        return frame

    def _request_shutdown(self) -> None:
        """Phase C2 (worker lifetime / shutdown ownership, 2026-09-11):
        the REQUEST half of shutdown -- cancellation/invalidation and
        bounded waits only. Never closes a native audio/video backend and
        never shuts down diagnostics -- see _finalize_shutdown() for that,
        reached only once every registered worker's completion has been
        positively established (WorkerLifetimeRegistry.active_count() ==
        0), whether that happens synchronously here (every worker resolved
        within its own bounded wait) or later, while shutdown is pending
        (see closeEvent/_maybe_resume_final_shutdown). Idempotent: a
        second call (there should never be one, but see _shutdown_requested)
        is a no-op."""
        if self._shutdown_requested:
            return
        self._shutdown_requested = True
        self._closing = True
        # Playback stability hardening: make the current PlaybackAttempt
        # (Phase A) non-authoritative FIRST, alongside every existing
        # specialist token -- none of those are replaced by PlaybackAttempt,
        # they stay in place as additional defence, invalidated together.
        self._cancel_current_playback_attempt("shutdown")
        self._playback_generation += 1
        self._plex_audio_load_token += 1
        self._crossfade_load_token += 1
        self._karaoke_generation += 1
        self._library_search_generation += 1
        self._playback_recovery_active = False
        self._cancel_playback_watchdog()
        self._audio_log("shutdown requested")
        try:
            getattr(self, "_refresh_visualiser_lifecycle", lambda *_: None)("shutting_down")
            beat = getattr(self, "beat", None)
            if beat is not None:
                beat.suspend()
        except Exception:
            pass
        for timer_name in (
            "ui_timer", "fade_timer", "analyzer_timer", "search_timer",
            "_build_timer", "_cover_timer", "_populate_timer", "queue_spinner_timer",
            "bio_anim_timer", "_splitter_save_timer",
            "_event_loop_probe_timer", "_memory_probe_timer",
            "_sleep_timer_timer", "_queue_duration_debounce_timer",
        ):
            try:
                timer = getattr(self, timer_name, None)
                if timer:
                    timer.stop()
            except Exception:
                pass
        try:
            self._uninstall_gc_pause_probe()
        except Exception:
            pass
        try:
            self._cancel_stall_dump()
        except Exception:
            pass
        self._cancel_pending_library_apply()
        try:
            if getattr(self, "overlay", None) is not None and getattr(self.overlay, "_timer", None):
                self.overlay._timer.stop()
        except Exception:
            pass
        self._cancel_fade()
        try:
            if self._mixed_transition_state != "idle":
                self._cancel_mixed_media_transition("shutdown")
        except Exception:
            pass
        try:
            transition_manager = getattr(self, "_video_transition_manager", None)
            if transition_manager is not None:
                transition_manager.shutdown()
        except Exception:
            pass
        # Stop audible/visible playback immediately -- safe even while
        # shutdown may still be pending afterward, since Phase C1's
        # prepare workers never touch a live player and the actual
        # native .close()/destroy calls are deferred to
        # _finalize_shutdown(), reached only once worker ownership is
        # fully resolved. video_backend.stop() (a "stop the stream,
        # don't destroy the backend" command, distinct from .shutdown())
        # mirrors _stop_all() on the audio side -- a hidden, pending
        # window must not leave video/audio still audibly/visibly
        # playing.
        self._stop_all()
        try:
            video_backend = getattr(self, "_video_backend", None)
            if video_backend is not None:
                video_backend.stop()
        except Exception:
            pass
        try:
            # The one existing, semantically-correct cancellation entry
            # point for the backfill scan (also sets
            # _backfill_scan_cancelled_by_user_action) -- the registry's
            # own "backfill_scan" registration carries no cancel=
            # callback specifically so this remains the only place that
            # requests its cancellation (see its registration comment).
            self._cancel_metadata_backfill()
        except Exception:
            pass
        try:
            gpu_probe = getattr(self, "_gpu_dual_probe", None)
            if gpu_probe is not None:
                gpu_probe.cancel()
                self._gpu_dual_probe = None
        except Exception:
            pass
        # One shutdown-ownership mechanism for every migrated worker:
        # requests cancellation (for every category that registered a
        # cancel= callback) then bound-waits each one, keeping (not
        # dropping) anything whose completion isn't positively proven --
        # see worker_registry.py's own docstring for the full semantics.
        try:
            self._worker_registry.shutdown_all()
        except Exception:
            pass
        self._audio_log("shutdown request phase complete")

    def _finalize_shutdown(self) -> None:
        """Phase C2: the FINALIZE half of shutdown -- native audio/video
        backend closure and remaining service teardown. Reached only once
        WorkerLifetimeRegistry.active_count() == 0 (see closeEvent), so no
        thread still capable of operating on a native player/backend
        object can possibly be running when this closes it. Guarded
        against re-entrancy by _shutdown_finalizing (in case something
        somehow calls this twice concurrently) and against ever running a
        second time by _shutdown_complete. Every individual teardown step
        stays exception-isolated (its own try/except) so one failing close
        does not prevent the rest from being attempted -- no single step
        here is identified as needing to block _shutdown_complete from
        becoming True: every one of them is best-effort teardown of a
        resource the application is abandoning regardless, and blocking
        final closure on any of them would trade a clean exit for a
        stuck/hung application, which is strictly worse."""
        if self._shutdown_complete:
            return
        if self._shutdown_finalizing:
            return
        self._shutdown_finalizing = True
        finalize_started = time.perf_counter()
        try:
            try:
                video_backend = getattr(self, "_video_backend", None)
                if video_backend is not None:
                    self.diagnostics.record(
                        "playback", "video_backend_shutdown", minimum_level="detailed",
                    )
                    video_backend.shutdown()
            except Exception:
                pass
            try:
                transition_point_analyzer = getattr(self, "_video_transition_point_analyzer", None)
                if transition_point_analyzer is not None:
                    transition_point_analyzer.shutdown()
            except Exception:
                pass
            for player_name in ("miniaudio_player", "miniaudio_inactive_player", "bass_player", "bass_inactive_player"):
                try:
                    player = getattr(self, player_name, None)
                    if player and hasattr(player, "close"):
                        player.close()
                except Exception:
                    pass
            try:
                self.viz_logger.stop()
            except Exception:
                pass
            try:
                self.artwork_manager.shutdown()
            except Exception:
                pass
            try:
                self.cast_discovery.close()
                self.cast_controller.close()
                self.cast_controller.disconnect()
                # Best-effort only -- none of these three threads has a
                # cooperative interruption point (blocking mDNS/socket calls),
                # so close()'s _closed/_generation guards are the actual
                # correctness fix (a late result is dropped whether or not
                # this join succeeds). A short bound, not the usual 2-3s: these
                # are daemon threads that don't block process exit either way,
                # this is purely "give an already-finishing one a moment"
                # rather than something shutdown correctness depends on.
                for thread in (
                    self.cast_discovery.discovery_thread,
                    self.cast_controller.connect_thread,
                    self.cast_controller.load_thread,
                ):
                    if thread is not None and thread.is_alive():
                        thread.join(timeout=0.3)
                # v1.0.69: close(), not shutdown() -- this is real application
                # shutdown, not a normal Cast disconnect/reconnect. Permanently
                # refuses any late register()/register_audio() (e.g. from a
                # CastPayloadWorker still finishing after this point), so the
                # HTTP server cannot be resurrected post-shutdown. The other 3
                # call sites (_on_cast_failed, _return_to_local_output,
                # _force_local_output_for_video) keep calling shutdown() --
                # those are legitimate "might Cast again this session" points.
                self.cast_media_server.close()
                self.diagnostics.record(
                    "cast", "cast_server_closed", minimum_level="detailed",
                )
                if self._cast_artwork_temp is not None:
                    self._cast_artwork_temp.cleanup()
                    self._cast_artwork_temp = None
            except Exception as ex:
                self._log(f"Cast shutdown cleanup failed: {ex}")
            try:
                export_worker = getattr(self, "_diagnostic_export_worker", None)
                if export_worker is not None and export_worker.isRunning():
                    export_worker.wait(3000)
            except Exception:
                pass
            try:
                self.diagnostics.record(
                    "shutdown", "main_window_close",
                    duration_ms=(time.perf_counter() - finalize_started) * 1000.0,
                    minimum_level="basic",
                )
                self.diagnostics.shutdown()
            except Exception:
                pass
            self._audio_log("shutdown complete")
        finally:
            self._shutdown_finalizing = False
            self._shutdown_complete = True

    def _maybe_resume_final_shutdown(self) -> None:
        """Phase C2: called from every registry-owned worker's identity-
        safe finished handler. If shutdown is pending (the first close
        request found unresolved workers and is currently waiting, via
        event.ignore(), for the normal Qt event loop to keep running) and
        this was the last unresolved worker, re-enter close() -- this
        time the registry is empty, so closeEvent finalizes for real. A
        no-op during normal (non-shutdown) operation."""
        if self._shutdown_pending and self._worker_registry.active_count() == 0:
            self.close()

    def closeEvent(self, event):
        # The one authoritative "the application is closing" flag (v1.0.66
        # worker-lifetime hardening) -- set here, first, before anything
        # else in shutdown (including the one-time UI/session teardown
        # below), so every code path reachable from this method already
        # sees it. _request_shutdown() also sets it, kept as a harmless,
        # idempotent second write for callers that invoke it directly
        # (e.g. tests) without going through closeEvent.
        self._closing = True
        if self._shutdown_complete:
            event.accept()
            return
        if not self._shutdown_requested:
            # ---- one-time UI/session teardown -- first close request only ----
            # Invalidate any outstanding fullscreen request: with _closing
            # already set, the request seam refuses new ones and the queued
            # applier discards a timer that was posted before this point.
            self._video_fullscreen_pending_target = None
            # Restore the normal presentation state before closing so hidden
            # panels and Party Mode overlays are not left mid-transition while
            # the backend and its embedded child surface shut down. This
            # DIRECT exit is deliberately not gated by the shutdown guard.
            if getattr(self, "_video_fullscreen", False):
                self._exit_video_fullscreen()
            try:
                self.recently_played_repository.save(self.recently_played_entries)
            except Exception as ex:
                self._log(f"Recently Played shutdown save failed: {ex}")
            if self.mini_player is not None:
                self._save_mini_player_geometry()
                self.mini_player._allow_close = True
                self.mini_player.close()
            if self.party_mode is not None:
                self.party_mode._allow_close = True
                self.party_mode.close()
            app = QtWidgets.QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
            if hasattr(self, "session_save_timer"):
                self.session_save_timer.stop()
            self._save_session()
            if hasattr(self, "_queue_analysis_save_timer"):
                self._queue_analysis_save_timer.stop()
            self._save_queue_analysis_cache()
            self._request_shutdown()

        if self._worker_registry.active_count() == 0:
            self._finalize_shutdown()
            event.accept()
            super().closeEvent(event)
        else:
            self._shutdown_pending = True
            try:
                self.hide()
            except Exception:
                pass
            event.ignore()

    def _build_library_tree_widget(self) -> QtWidgets.QTreeWidget:
        """Build one library QTreeWidget, configured identically for the
        Music and Videos tabs (shared factory so the setup isn't duplicated)."""
        tree = QtWidgets.QTreeWidget()
        tree.verticalScrollBar().valueChanged.connect(
            self._queue_visible_album_covers
        )
        tree.setHeaderHidden(True)
        tree.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        tree.setIconSize(QtCore.QSize(40, 40))
        tree.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        tree.setAlternatingRowColors(True)
        tree.setStyleSheet(
            "QTreeWidget { background:#0c0820; alternate-background-color:rgba(33,230,255,0.035); border:0; padding:5px; }"
            "QTreeWidget::item { padding:5px 4px; border-radius:7px; color:#eaf2ff; }"
            "QTreeWidget::item:selected { background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 rgba(255,60,172,0.38),stop:0.65 rgba(177,76,255,0.28),stop:1 rgba(33,230,255,0.22)); color:#ffffff; }"
            "QTreeWidget::item:hover { background:rgba(33,230,255,0.12); color:#fff8c8; }"
        )
        tree.itemDoubleClicked.connect(self._play_tree_item)
        tree.itemExpanded.connect(self._on_album_expanded)
        tree.customContextMenuRequested.connect(self._show_tree_menu)
        return tree

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        header = QtWidgets.QHBoxLayout()
        self.now_playing = MarqueeLabel()
        self.now_playing.setText("Ready")
        self.now_playing.setRainbowShimmer(True)
        self.now_playing.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.now_playing.customContextMenuRequested.connect(self._show_now_playing_menu)
        self.now_playing.setShimmerSpeed(0.85)
        self.now_playing.setStyleSheet(
            "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #2a1245, stop:1 #14203a);"
            "border:1px solid #ff3cac;border-radius:10px;padding:8px 14px;"
        )
        self.dj_info = QtWidgets.QLabel("Backend: ready")
        self.dj_info.setStyleSheet("color:#21e6ff; font-size:8pt; font-weight:700; padding:0 8px;")
        now_stack = QtWidgets.QVBoxLayout()
        now_stack.setSpacing(2)
        now_stack.addWidget(self.now_playing)
        now_stack.addWidget(self.dj_info)
        header.addLayout(now_stack, 3)

        self.search_box = QtWidgets.QLineEdit()
        self.search_box.setPlaceholderText("Search, or [tag] to narrow (artist/album/track/genre/year/bpm/key)")
        header.addWidget(self.search_box, 1)

        # Admin controls kept alive but moved off the header into the library
        # right-click menu (see _show_tree_menu). They stay as objects so the
        # scan enable/disable logic keeps working.
        self.btn_add = QtWidgets.QPushButton("Add Folder")
        self.btn_rescan = QtWidgets.QPushButton("Rescan")
        self.chk_simple = QtWidgets.QCheckBox("Use miniaudio player")
        self.btn_add.setVisible(False)
        self.btn_rescan.setVisible(False)
        self.chk_simple.setVisible(False)
        root.addLayout(header)

        self.scan_bar = QtWidgets.QProgressBar()
        self.scan_bar.setRange(0, 100)
        self.scan_bar.setValue(0)
        self.scan_bar.setVisible(False)
        self.scan_bar.setStyleSheet(
            "QProgressBar { background:#120a22; border:1px solid #3a2a55; border-radius:7px; height:12px; }"
            "QProgressBar::chunk { border-radius:7px; "
            "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #ff3cac,stop:0.5 #b14cff,stop:1 #21e6ff); }"
        )
        root.addWidget(self.scan_bar)

        mid = QtWidgets.QHBoxLayout()
        mid.setSpacing(12)
        self.tree_tracks = self._build_library_tree_widget()
        self.tree_tracks_video = self._build_library_tree_widget()
        self.tree_tracks_karaoke = self._build_library_tree_widget()
        self._tree_tracks_base_stylesheet = self.tree_tracks.styleSheet()
        self.library_frame = self._wrap_shimmer(self.tree_tracks)
        self.library_frame_video = self._wrap_shimmer(self.tree_tracks_video)
        self.library_frame_karaoke = self._wrap_shimmer(self.tree_tracks_karaoke)
        for frame in (self.library_frame, self.library_frame_video, self.library_frame_karaoke):
            glow = QtWidgets.QGraphicsDropShadowEffect(frame)
            glow.setBlurRadius(18)
            glow.setColor(QtGui.QColor(177, 76, 255, 90))
            glow.setOffset(0, 0)
            frame.setGraphicsEffect(glow)
        self.library_tabs = QtWidgets.QTabWidget()
        self.library_tabs.setObjectName("libraryTabs")
        self.library_tabs.addTab(self.library_frame, "Music")
        self.library_tabs.addTab(self.library_frame_video, "Videos")
        self.library_tabs.addTab(self.library_frame_karaoke, "Karaoke")

        # Source selector (Local/Plex) -- Stage 1: exists and persists, but
        # selecting Plex does not yet browse real Plex content (Stage 2/3).
        # Local remains the default and its behaviour is completely
        # unaffected by this selector's presence.
        source_row = QtWidgets.QHBoxLayout()
        source_row.setContentsMargins(0, 0, 0, 4)
        self.library_source_label = QtWidgets.QLabel("Source:")
        source_row.addWidget(self.library_source_label)
        self.library_source_selector = QtWidgets.QComboBox()
        self.library_source_selector.setObjectName("librarySourceSelector")
        self.library_source_selector.addItem("Local", "local")
        self.library_source_selector.addItem("Plex", "plex")
        source_row.addWidget(self.library_source_selector)
        self.library_plex_refresh_button = QtWidgets.QPushButton("Refresh Plex")
        self.library_plex_refresh_button.setObjectName("libraryPlexRefreshButton")
        self.library_plex_refresh_button.setVisible(False)
        self.library_plex_refresh_button.clicked.connect(self._refresh_plex_library)
        source_row.addWidget(self.library_plex_refresh_button)
        source_row.addStretch(1)
        self.library_plex_placeholder = QtWidgets.QLabel(
            "No Plex libraries configured yet -- configure Plex in "
            "Preferences > Plex."
        )
        self.library_plex_placeholder.setWordWrap(True)
        self.library_plex_placeholder.setVisible(False)
        # Wrapped in a real container widget (not added as a bare layout
        # item) so fullscreen can collapse the whole library region by
        # hiding ONE widget -- QHBoxLayout only reclaims a hidden child
        # WIDGET's stretch-allocated space, never a nested QLayout item's,
        # so a bare `mid.addLayout(library_column, 3)` left ~3/7 of
        # fullscreen width permanently reserved for an empty column even
        # with every widget inside it hidden individually (see
        # _main_video_fullscreen_hidden_widgets).
        self.library_column_container = QtWidgets.QWidget()
        library_column = QtWidgets.QVBoxLayout(self.library_column_container)
        library_column.setContentsMargins(0, 0, 0, 0)
        library_column.addLayout(source_row)
        library_column.addWidget(self.library_plex_placeholder)
        library_column.addWidget(self.library_tabs)
        mid.addWidget(self.library_column_container, 3)
        self.library_source_selector.currentIndexChanged.connect(
            self._on_library_source_changed
        )

        self._music_tab = LibraryTabState(
            media_type=MediaType.AUDIO, tree_tracks=self.tree_tracks,
        )
        self._video_tab = LibraryTabState(
            media_type=MediaType.VIDEO, tree_tracks=self.tree_tracks_video,
        )
        self._karaoke_tab = LibraryTabState(
            media_type=MediaType.KARAOKE, tree_tracks=self.tree_tracks_karaoke,
        )
        self._active_library_tab = self._music_tab
        self.library_tabs.currentChanged.connect(self._on_library_tab_changed)

        # Right side: a user-resizable visualiser above the queue/history tabs.
        right = QtWidgets.QVBoxLayout()
        self.visualiser_layout = right
        right.setSpacing(0)
        self.right_splitter = QtWidgets.QSplitter(
            QtCore.Qt.Orientation.Vertical
        )
        self.right_splitter.setObjectName("rightSplitter")
        self.right_splitter.setChildrenCollapsible(False)
        self.right_splitter.setHandleWidth(8)
        self._visualiser_splitter_state = None
        right.addWidget(self.right_splitter, 1)
        self.beat = BeatWidget()
        # Display stack: the normal visualiser/artwork page shares this slot
        # with video playback pages, so video works whether or not the
        # analyser/visualiser itself is available, and reverts cleanly to
        # whichever normal page already existed once video stops.
        self.right_display_stack = QtWidgets.QStackedWidget()
        self.right_display_stack.setMinimumHeight(190)
        if AUDIO_ANALYSIS_AVAILABLE:
            self.visualiser_frame = self._wrap_shimmer(self.beat)
            self.visualiser_frame.setMinimumHeight(190)
            self._normal_display_page = self.visualiser_frame
        else:
            self.visualiser_frame = None
            self.beat.setVisible(False)
            notice = QtWidgets.QLabel("Audio analysis unavailable\n(install numpy & soundfile for visualizer)")
            notice.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            notice.setStyleSheet("color:#888; font-style:italic;")
            self.visualiser_unavailable_frame = self._wrap_shimmer(notice)
            self.visualiser_unavailable_frame.setMinimumHeight(190)
            self._normal_display_page = self.visualiser_unavailable_frame
        self.right_display_stack.addWidget(self._normal_display_page)

        video_loading_label = QtWidgets.QLabel("Loading video…")
        video_loading_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        video_loading_label.setStyleSheet("color:#aeeaff; font-style:italic;")
        self._video_loading_page = self._wrap_shimmer(video_loading_label)
        self.right_display_stack.addWidget(self._video_loading_page)

        # Plain container the video backend embeds its child process's
        # video window into by native window ID (see attach_output() in
        # video_backend.py) -- this widget never renders video itself.
        self.video_output_widget = QtWidgets.QWidget()
        self.video_output_widget.setStyleSheet("background:#000000;")
        video_output_layout = QtWidgets.QVBoxLayout(self.video_output_widget)
        video_output_layout.setContentsMargins(0, 0, 0, 0)
        self.video_output_widget.mouseDoubleClickEvent = self._on_video_widget_double_click
        self.video_output_widget.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.video_output_widget.customContextMenuRequested.connect(
            self._show_video_context_menu
        )
        # v1.0.72 correction (real-device acceptance): every other
        # right_display_stack page is wrapped in ShimmerFrame for its
        # decorative animated border, but that border reads as a stray
        # coloured strip flickering around a plain video frame -- unlike
        # the already-colourful visualiser it was designed to complement.
        # ShimmerFrame insets its child by 1px on every side specifically
        # so that border has room to paint (confirmed directly: a real
        # render of the wrapped page shows a visible 1-2px animated
        # gradient outline around the content on all four edges, sweeping
        # to whichever edge the animation's rotating gradient currently
        # favours -- matching a real report of it appearing "along the
        # bottom edge" at one particular moment). The video page skips
        # the wrapper entirely instead of disabling/hiding the border by
        # a flag, so video_output_widget becomes the stack page directly
        # and genuinely fills 100% of right_display_stack's rect with no
        # inset at all -- not just a border that no longer paints.
        self._video_output_page = self.video_output_widget
        self.right_display_stack.addWidget(self._video_output_page)

        self.karaoke_widget = CdgWidget()
        # Requests only, same as the video backend's forwarded input above
        # -- karaoke shares the one video fullscreen mechanism, so it
        # shares its single request seam too rather than being a second
        # independent synchronous caller of the native restore path.
        self.karaoke_widget.double_clicked.connect(self._on_karaoke_double_clicked)
        self.karaoke_widget.escape_pressed.connect(self._on_karaoke_escape_pressed)
        self._karaoke_output_page = self._wrap_shimmer(self.karaoke_widget)
        self.right_display_stack.addWidget(self._karaoke_output_page)

        self._video_error_label = QtWidgets.QLabel("")
        self._video_error_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._video_error_label.setWordWrap(True)
        self._video_error_label.setStyleSheet(
            "color:#ff8080; font-style:italic; padding:12px;"
        )
        self._video_error_page = self._wrap_shimmer(self._video_error_label)
        self.right_display_stack.addWidget(self._video_error_page)

        self.right_display_stack.setCurrentWidget(self._normal_display_page)
        self.right_splitter.addWidget(self.right_display_stack)
        self._video_backend.attach_output(self.video_output_widget)

        # --- Hidden-but-alive widgets ---------------------------------------
        # The bio box machinery and the file-info (tag) box still exist so all
        # their methods keep working, but they're no longer shown permanently.
        # Bio now flows to the jukebox cards; file info is shown on demand via
        # the "Show track info" panel (right-click the library).
        self.tag_box = AutoScrollText()
        self.tag_box.setReadOnly(True)
        self.tag_box.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tag_box.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tag_box.setStyleSheet("background:#0c0820;color:#eaf2ff;border:1px solid #281a44;border-radius:10px;padding:6px 8px;")
        self.tag_box.setVisible(False)

        self.bio_box = AutoScrollText()
        self.bio_box.setReadOnly(True)
        self.bio_box.setVisible(False)

        # Queue/history: compact tabs under the visualiser.
        self.right_tabs = QtWidgets.QTabWidget()
        self.right_tabs.setAccessibleName("Queue and Recently Played")
        self.up_next_page = QtWidgets.QWidget()
        up_next_layout = QtWidgets.QVBoxLayout(self.up_next_page)
        up_next_layout.setContentsMargins(0, 2, 0, 0)
        up_next_layout.setSpacing(3)
        self.queue_label = QtWidgets.QLabel("UP NEXT")
        self.queue_label.setStyleSheet("color:#b14cff; font-size:9pt; font-weight:600; padding:0 4px;")
        up_next_layout.addWidget(self.queue_label)
        self.queue_duration_summary_label = QtWidgets.QLabel("Up Next: Empty")
        self.queue_duration_summary_label.setStyleSheet(
            "color:#9d94b8; font-size:7pt; padding:0 4px 2px 4px;"
        )
        self.queue_duration_summary_label.setWordWrap(True)
        self.queue_duration_summary_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.NoTextInteraction
        )
        up_next_layout.addWidget(self.queue_duration_summary_label)
        self.queue_header = QtWidgets.QWidget()
        queue_header_layout = QtWidgets.QHBoxLayout(self.queue_header)
        queue_header_layout.setContentsMargins(8, 0, 44, 0)
        queue_header_layout.setSpacing(8)
        for text, stretch in (("TRACK", 6), ("TIME", 1), ("RATE", 2), ("KEY", 1), ("BPM", 1)):
            label = QtWidgets.QLabel(text)
            label.setStyleSheet("color:#21e6ff; font-size:7pt; font-weight:800; letter-spacing:1px;")
            label.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
            queue_header_layout.addWidget(label, stretch)
        up_next_layout.addWidget(self.queue_header)
        self.queue_list = QueueListWidget()
        self.queue_list.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.queue_list.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.queue_list.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.queue_list.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.InternalMove)
        self.queue_list.setAcceptDrops(True)
        self.queue_list.filesDropped.connect(self._on_queue_files_dropped)
        self.queue_list.setSpacing(3)
        self.queue_list.setStyleSheet(
            "QListWidget { background:#0c0820; border:1px solid #281a44; border-radius:10px; padding:4px; color:#f7f2ff; }"
            "QListWidget::item { border:0; padding:0; margin:0; }"
            "QListWidget::item:selected { background:rgba(33,230,255,0.18); border-radius:7px; }"
        )
        self.queue_panel = QtWidgets.QWidget()
        queue_panel_layout = QtWidgets.QGridLayout(self.queue_panel)
        queue_panel_layout.setContentsMargins(0, 0, 0, 0)
        queue_panel_layout.setSpacing(0)
        queue_panel_layout.addWidget(self._wrap_shimmer(self.queue_list), 0, 0)
        self.btn_save_queue = SpinningToolButton()
        self.btn_save_queue.setText("\u266a+")
        self.btn_save_queue.setToolTip("Save Up Next as playlist")
        self.btn_save_queue.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_save_queue.setFixedSize(38, 34)
        self.btn_save_queue.setStyleSheet(
            "QToolButton {"
            "background:qradialgradient(cx:0.35,cy:0.30,radius:0.9,stop:0 #21e6ff,stop:0.55 #b14cff,stop:1 #ff3cac);"
            "border:1px solid #ffd6fb;border-radius:17px;color:#ffffff;font-size:15pt;font-weight:800;"
            "padding-bottom:2px;"
            "}"
            "QToolButton:hover { border:2px solid #ffffff; color:#fff8c8; }"
            "QToolButton:pressed { background:#150b2a; border:1px solid #21e6ff; }"
        )
        self.btn_save_queue.setGraphicsEffect(QtWidgets.QGraphicsOpacityEffect(self.btn_save_queue))
        self.queue_save_anim = QtCore.QPropertyAnimation(self.btn_save_queue.graphicsEffect(), b"opacity", self)
        self.queue_save_anim.setStartValue(0.68)
        self.queue_save_anim.setEndValue(1.0)
        self.queue_save_anim.setDuration(850)
        self.queue_save_anim.setEasingCurve(QtCore.QEasingCurve.Type.InOutSine)
        self.queue_save_anim.setLoopCount(-1)
        self.queue_save_anim.start()
        self.queue_spin_anim = QtCore.QPropertyAnimation(self.btn_save_queue, b"rotation", self)
        self.queue_spin_anim.setDuration(1600)
        self.queue_spin_anim.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
        self.queue_spin_timer = QtCore.QTimer(self)
        self.queue_spin_timer.timeout.connect(self._play_queue_save_spin)
        self.queue_spin_timer.start(45000)
        QtCore.QTimer.singleShot(1200, self._play_queue_save_spin)
        queue_panel_layout.addWidget(
            self.btn_save_queue,
            0,
            0,
            QtCore.Qt.AlignmentFlag.AlignBottom | QtCore.Qt.AlignmentFlag.AlignRight,
        )
        up_next_layout.addWidget(self.queue_panel, 1)
        self.right_tabs.addTab(self.up_next_page, "Up Next")

        self.recent_page = QtWidgets.QWidget()
        recent_layout = QtWidgets.QVBoxLayout(self.recent_page)
        recent_layout.setContentsMargins(0, 2, 0, 0)
        self.recent_table = QtWidgets.QTableWidget(0, 4)
        self.recent_table.setObjectName("recentlyPlayedTable")
        self.recent_table.setHorizontalHeaderLabels(
            ["Title", "Artist", "Album", "Played"]
        )
        self.recent_table.setAlternatingRowColors(True)
        self.recent_table.setShowGrid(False)
        self.recent_table.setWordWrap(False)
        self.recent_table.verticalHeader().setVisible(False)
        self.recent_table.verticalHeader().setDefaultSectionSize(34)
        self.recent_table.setVerticalScrollMode(
            QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.recent_table.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.recent_table.setStyleSheet(
            "QTableWidget#recentlyPlayedTable {"
            "background:#0c0820; alternate-background-color:#110d29;"
            "border:1px solid #281a44; border-radius:10px; padding:4px;"
            "color:#f7f2ff; gridline-color:transparent;"
            "selection-background-color:rgba(33,230,255,0.18);"
            "selection-color:#ffffff;"
            "}"
            "QTableWidget#recentlyPlayedTable::item {"
            "border:0; padding:5px 7px;"
            "}"
            "QTableWidget#recentlyPlayedTable::item:hover {"
            "background:rgba(177,76,255,0.12); border-radius:7px;"
            "}"
            "QTableWidget#recentlyPlayedTable::item:selected {"
            "background:rgba(33,230,255,0.18); border-radius:7px;"
            "}"
            "QTableWidget#recentlyPlayedTable QHeaderView::section {"
            "background:#0c0820; color:#21e6ff; border:0;"
            "border-bottom:1px solid #281a44; padding:6px 7px;"
            "font-size:7pt; font-weight:800;"
            "}"
            "QTableWidget#recentlyPlayedTable QTableCornerButton::section {"
            "background:#0c0820; border:0;"
            "}"
        )
        self.recent_table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        for column in (1, 2, 3):
            self.recent_table.horizontalHeader().setSectionResizeMode(
                column, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
            )
        self.recent_table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.recent_table.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.recent_table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.recent_table.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.recent_table.setAccessibleName("Recently Played")
        self.recent_table.setAccessibleDescription(
            "Tracks previously listened to, newest first"
        )
        self.recent_table.horizontalHeaderItem(3).setData(
            QtCore.Qt.ItemDataRole.AccessibleTextRole, "Played time"
        )
        self.recent_frame = self._wrap_shimmer(self.recent_table)
        recent_layout.addWidget(self.recent_frame)
        self.right_tabs.addTab(self.recent_page, "Recently Played")
        self.right_tabs.setTabToolTip(1, "Tracks previously listened to")
        self.right_tabs.setMinimumHeight(190)
        self.right_splitter.addWidget(self.right_tabs)
        self.right_splitter.setStretchFactor(0, 65)
        self.right_splitter.setStretchFactor(1, 35)
        self.right_splitter.setSizes([650, 350])
        self._splitter_save_timer = QtCore.QTimer(self)
        self._splitter_save_timer.setSingleShot(True)
        self._splitter_save_timer.setInterval(500)
        self._splitter_save_timer.timeout.connect(self._save_user_settings)
        self.right_splitter.splitterMoved.connect(
            self._remember_right_splitter_state
        )
        self._refresh_recently_played_table()

        mid.addLayout(right, 4)
        root.addLayout(mid, 1)

        controls = QtWidgets.QHBoxLayout()
        self.btn_prev = QtWidgets.QPushButton("Back")
        self.btn_play = QtWidgets.QPushButton("Play")
        self.btn_pause = QtWidgets.QPushButton("Pause")
        self.btn_next = QtWidgets.QPushButton("Next")
        controls.addWidget(self.btn_prev)
        controls.addWidget(self.btn_play)
        controls.addWidget(self.btn_pause)
        controls.addWidget(self.btn_next)
        self.output_combo = QtWidgets.QComboBox()
        self.output_combo.setMinimumWidth(150)
        self.output_combo.addItem("This Computer", None)
        self.output_combo.addItem("Refresh Cast devices…", "__refresh__")
        self.output_combo.setToolTip(
            "Choose this computer or a discovered Google Cast speaker"
        )
        controls.addWidget(self.output_combo)

        if self.waveform_seekbar_enabled:
            self.waveform_seekbar = WaveformSeekBar()
            self.slider_progress = self.waveform_seekbar
        else:
            self.waveform_seekbar = None
            self.slider_progress = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
            self.slider_progress.setRange(0, 1000)
            self.slider_progress.setValue(0)
        controls.addWidget(self.slider_progress, 2)

        self.label_remaining = QtWidgets.QLabel("-0:00")
        self.label_remaining.setStyleSheet("color:#21e6ff; font-weight:600; min-width:70px;")
        controls.addWidget(self.label_remaining)

        self.label_volume = QtWidgets.QLabel("Volume")
        self.label_volume.setStyleSheet("color:#cbb8ff; min-width:55px;")
        controls.addWidget(self.label_volume)

        self.slider_volume = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider_volume.setRange(0, 100)
        self.slider_volume.setValue(self.master_volume)
        self.slider_volume.setMaximumWidth(160)
        controls.addWidget(self.slider_volume)

        # volume controls removed to increase visual space
        root.addLayout(controls)

        self.setStyleSheet("""
            QWidget {
                background:#0a0716;
                color:#eaf2ff;
                font-family:'Segoe UI', sans-serif;
                font-size:12pt;
            }
            QMainWindow, QWidget#central { background:#0a0716; }

            QPushButton {
                background:qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 #2a1750, stop:1 #1a0f33);
                border:1px solid #5a3aa0;
                padding:8px 16px;
                border-radius:9px;
                color:#f3e9ff;
                font-weight:600;
            }
            QPushButton:hover {
                border:1px solid #ff3cac;
                background:qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 #3a1f68, stop:1 #241345);
                color:#ffffff;
            }
            QPushButton:pressed {
                background:#150b2a;
                border:1px solid #21e6ff;
            }
            QPushButton:focus, QToolButton:focus, QComboBox:focus {
                border:2px solid #fff8c8;
            }

            QLineEdit {
                background:#140c28;
                border:1px solid #3a2a55;
                border-radius:9px;
                padding:7px 12px;
                selection-background-color:#ff3cac;
            }
            QLineEdit:focus { border:2px solid #fff8c8; }

            QCheckBox { color:#cbb8ff; spacing:7px; }
            QCheckBox::indicator {
                width:16px; height:16px; border-radius:4px;
                border:1px solid #5a3aa0; background:#140c28;
            }
            QCheckBox::indicator:checked {
                background:qlineargradient(x1:0,y1:0,x2:1,y2:1,
                    stop:0 #ff3cac, stop:1 #21e6ff);
                border:1px solid #ff3cac;
            }

            QTreeWidget, QListWidget, QTableWidget {
                background:#0c0820;
                border:1px solid #281a44;
                border-radius:10px;
                outline:0;
            }
            QTreeWidget:focus, QListWidget:focus, QTableWidget:focus {
                border:2px solid #fff8c8;
            }
            QTreeWidget::item, QListWidget::item { padding:4px 2px; border-radius:6px; }
            QTreeWidget::item:selected, QListWidget::item:selected {
                background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 rgba(255,60,172,0.35), stop:1 rgba(33,230,255,0.20));
                color:#ffffff;
            }
            QTreeWidget::item:hover, QListWidget::item:hover {
                background:rgba(120,80,200,0.18);
            }

            QSlider::groove:horizontal {
                height:6px; border-radius:3px;
                background:#1d1336;
            }
            QSlider::sub-page:horizontal {
                height:6px; border-radius:3px;
                background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 #ff3cac, stop:0.5 #b14cff, stop:1 #21e6ff);
            }
            QSlider::handle:horizontal {
                width:16px; margin:-6px 0; border-radius:8px;
                background:#ffffff;
                border:2px solid #ff3cac;
            }
            QSlider::handle:horizontal:hover { border:2px solid #21e6ff; }
            QScrollBar:vertical { background:transparent; width:9px; margin:0; }
            QScrollBar::handle:vertical {
                background:#3a2a55; border-radius:4px; min-height:24px;
            }
            QScrollBar::handle:vertical:hover { background:#b14cff; }
            QScrollBar::add-line, QScrollBar::sub-line { height:0; }

            QMenu {
                background:#140c28; color:#eaf2ff;
                border:1px solid #3a2a55; border-radius:8px;
                min-width:300px;
            }
            QMenu::item { padding:7px 42px 7px 14px; }
            QMenu::separator {
                height:1px; background:#3a2a55; margin:4px 10px;
            }
            QMenu::item:selected { background:rgba(255,60,172,0.30); }

            QTabWidget::pane { border:1px solid #281a44; border-radius:8px; }
            QTabBar::tab {
                background:#140c28; color:#cbb8ff; padding:5px 12px;
                border:1px solid #3a2a55;
            }
            QTabBar::tab:selected { color:#fff8c8; border-color:#21e6ff; }
            QHeaderView::section {
                background:#140c28; color:#21e6ff; border:0;
                padding:4px; font-weight:700;
            }

            QSplitter#rightSplitter::handle:vertical {
                height:8px;
                margin:1px 22px;
                border-radius:3px;
                background:#3a2a55;
            }
            QSplitter#rightSplitter::handle:vertical:hover {
                background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 #ff3cac, stop:1 #21e6ff);
            }

            QLabel { color:#cbb8ff; }
        """)

        # Jukebox intro overlay -- sits on top of everything, click-through.
        self.overlay = JukeboxOverlay(central)
        self.overlay.setGeometry(central.rect())
        self.overlay.raise_()

    def _make_window_action(self, name, text, shortcut, callback):
        action = QtGui.QAction(text, self)
        action.setObjectName(f"action_{name}")
        action.setShortcut(QtGui.QKeySequence(shortcut))
        action.setShortcutContext(QtCore.Qt.ShortcutContext.WindowShortcut)
        action.triggered.connect(callback)
        self.addAction(action)
        setattr(self, f"action_{name}", action)
        return action

    def _build_accessible_actions(self):
        self.action_play_pause = QtGui.QAction("Play or Pause", self.centralWidget())
        self.action_play_pause.setObjectName("action_play_pause")
        self.action_play_pause.setShortcut(QtGui.QKeySequence(SHORTCUTS["play_pause"]))
        self.action_play_pause.setShortcutContext(QtCore.Qt.ShortcutContext.WidgetShortcut)
        self.action_play_pause.triggered.connect(self._toggle_play_pause)
        self.centralWidget().addAction(self.action_play_pause)
        self._make_window_action(
            "play_pause_alternate", "Play or Pause", SHORTCUTS["play_pause_alternate"],
            self._toggle_play_pause,
        )
        self._make_window_action("stop", "Stop", SHORTCUTS["stop"], self.stop_playback)
        self._make_window_action("previous", "Previous Track", SHORTCUTS["previous"], self.prev_track)
        self._make_window_action("next", "Next Track", SHORTCUTS["next"], self.next_track)
        self._make_window_action(
            "volume_up", "Volume Up", SHORTCUTS["volume_up"],
            lambda: self._adjust_volume(5),
        )
        self._make_window_action(
            "volume_down", "Volume Down", SHORTCUTS["volume_down"],
            lambda: self._adjust_volume(-5),
        )
        self._make_window_action("mute", "Mute", SHORTCUTS["mute"], self.toggle_mute)
        self._make_window_action(
            "focus_search", "Focus Library Search", SHORTCUTS["focus_search"],
            self._focus_library_search,
        )
        self._make_window_action(
            "focus_library", "Focus Music Library", SHORTCUTS["focus_library"],
            self._focus_library,
        )
        self._make_window_action(
            "focus_up_next", "Focus Up Next", SHORTCUTS["focus_up_next"],
            self._focus_up_next,
        )
        self._make_window_action(
            "toggle_lyrics", "Show Lyrics", SHORTCUTS["toggle_lyrics"],
            self._toggle_lyrics_accessibly,
        )
        self.action_toggle_lyrics.setCheckable(True)
        self.action_toggle_lyrics.setChecked(bool(self.lyrics_enabled))
        self._make_window_action(
            "toggle_visualiser", "Show Visualiser", SHORTCUTS["toggle_visualiser"],
            self._toggle_visualiser_accessibly,
        )
        self.action_toggle_visualiser.setCheckable(True)
        self.action_toggle_visualiser.setChecked(bool(self.visualiser_frame))
        self._make_window_action(
            "mini_player", "Mini Player", SHORTCUTS["mini_player"],
            self._toggle_mini_player,
        )
        self._make_window_action(
            "party_mode", "Party Mode", SHORTCUTS["party_mode"],
            self._toggle_party_mode,
        )
        self._make_window_action(
            "recently_played", "Recently Played", SHORTCUTS["recently_played"],
            self._show_recently_played,
        )
        self._make_window_action(
            "undo_queue_change", GENERIC_UNDO_LABEL, SHORTCUTS["undo_queue_change"],
            self._undo_queue_change,
        )
        self.action_undo_queue_change.setEnabled(False)
        self._make_window_action(
            "video_fullscreen", "Toggle Video Full Screen", SHORTCUTS["video_fullscreen"],
            self._toggle_video_fullscreen,
        )
        self.action_show_top_menu = QtGui.QAction("Show Top Menu", self)
        self.action_show_top_menu.setCheckable(True)
        self.action_show_top_menu.setChecked(False)
        self.action_show_top_menu.setToolTip(
            "Show or hide the Playback and View menu bar"
        )
        self.action_show_top_menu.toggled.connect(
            self._set_top_menu_visible
        )

        playback_menu = self.menuBar().addMenu("&Playback")
        for action in (
            self.action_play_pause, self.action_play_pause_alternate, self.action_stop,
            self.action_previous, self.action_next, self.action_volume_up,
            self.action_volume_down, self.action_mute,
        ):
            playback_menu.addAction(action)
        self._build_sleep_timer_menu(playback_menu)
        edit_menu = self.menuBar().addMenu("&Edit")
        edit_menu.addAction(self.action_undo_queue_change)
        view_menu = self.menuBar().addMenu("&View")
        for action in (
            self.action_focus_search, self.action_focus_library,
            self.action_focus_up_next, self.action_toggle_lyrics,
            self.action_toggle_visualiser, self.action_mini_player,
            self.action_party_mode, self.action_recently_played,
        ):
            view_menu.addAction(action)
        view_menu.addSeparator()
        action_preferences = view_menu.addAction("Preferences...")
        action_preferences.triggered.connect(
            self._show_normalisation_preferences
        )
        view_menu.addSeparator()
        view_menu.addAction(self.action_show_top_menu)
        playlist_menu = self.menuBar().addMenu("&Playlist")
        action_repair_playlist = playlist_menu.addAction(
            "Repair Missing Tracks..."
        )
        action_repair_playlist.triggered.connect(
            self._repair_missing_playlist_tracks
        )
        help_menu = self.menuBar().addMenu("&Help")
        action_export_diagnostics = help_menu.addAction(
            "Export Performance Diagnostic Bundle..."
        )
        action_export_diagnostics.triggered.connect(
            self._export_performance_diagnostics
        )
        action_open_diagnostics = help_menu.addAction(
            "Open Diagnostics Folder"
        )
        action_open_diagnostics.triggered.connect(
            self._open_diagnostics_folder
        )
        action_copy_diagnostics = help_menu.addAction(
            "Copy Latest Performance Summary"
        )
        action_copy_diagnostics.triggered.connect(
            self._copy_performance_summary
        )
        self.menuBar().hide()

    def _set_top_menu_visible(self, visible):
        visible = bool(visible)
        self.top_menu_visible = visible
        self.menuBar().setVisible(visible)
        self._save_user_settings()

    def _add_window_menu_options(self, menu):
        menu.addSeparator()
        menu.addAction(self.action_show_top_menu)

    def _export_performance_diagnostics(self):
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        default_name = os.path.join(
            os.path.expanduser("~/Desktop"),
            f"Bills-Music-Player-Diagnostics-{timestamp}.zip",
        )
        destination, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export Performance Diagnostic Bundle",
            default_name,
            "ZIP archives (*.zip)",
        )
        if not destination:
            return
        if not destination.lower().endswith(".zip"):
            destination += ".zip"
        try:
            cfg = load_config() or {}
        except Exception:
            cfg = {}
        self.statusBar().showMessage("Exporting performance diagnostics...")
        worker = DiagnosticExportWorker(
            self.diagnostics,
            destination,
            os.path.join(
                os.environ.get("LOCALAPPDATA", os.getcwd()),
                "Bills Music Player",
            ),
            cfg,
            {
                "title": APP_TITLE,
                "version": QtWidgets.QApplication.applicationVersion(),
                "backend": self._backend_label(),
                "vlc_available": VLC_AVAILABLE,
                "miniaudio_available": self.miniaudio_player is not None,
                "bass_available": self.bass_player is not None,
            },
        )
        worker.completed.connect(self._diagnostic_export_complete)
        worker.failed.connect(self._diagnostic_export_failed)
        worker.finished.connect(worker.deleteLater)
        self._diagnostic_export_worker = worker
        worker.start()

    def _diagnostic_export_complete(self, path):
        self._diagnostic_export_worker = None
        if getattr(self, "_closing", False):
            return
        self.statusBar().showMessage(
            f"Performance diagnostic bundle saved: {path}", 15000
        )
        QtWidgets.QMessageBox.information(
            self, "Performance Diagnostics",
            f"Diagnostic bundle saved to:\n\n{path}",
        )

    def _diagnostic_export_failed(self, message):
        self._diagnostic_export_worker = None
        if getattr(self, "_closing", False):
            return
        self.statusBar().showMessage("Diagnostic export failed", 10000)
        QtWidgets.QMessageBox.warning(
            self, "Performance Diagnostics",
            f"The diagnostic bundle could not be created.\n\n{message}",
        )

    def _open_diagnostics_folder(self):
        path = str(self.diagnostics.directory)
        try:
            os.makedirs(path, exist_ok=True)
            os.startfile(path)
        except Exception as ex:
            QtWidgets.QMessageBox.warning(
                self, "Performance Diagnostics",
                f"The diagnostics folder could not be opened.\n\n{ex}",
            )

    def _copy_performance_summary(self):
        text = self.diagnostics.human_summary()
        QtWidgets.QApplication.clipboard().setText(text)
        self.statusBar().showMessage(
            "Latest performance summary copied", 8000
        )

    def _configure_accessibility(self):
        controls = (
            (self.btn_play, "Play", "Start the selected track"),
            (self.btn_pause, "Pause", "Pause or resume playback"),
            (self.btn_prev, "Previous track", "Play the previous track"),
            (self.btn_next, "Next track", "Play the next track"),
            (self.slider_volume, "Volume", "Playback volume from 0 to 100 percent"),
            (self.search_box, "Library search", "Search tracks, albums and artists. Prefix with a bracket tag such as [artist] to search one field only."),
            (self.tree_tracks, "Music library", "Browse tracks grouped by album"),
            (self.queue_list, "Up Next", "Tracks queued to play before normal library playback"),
            (
                self.queue_duration_summary_label, "Up Next duration",
                "Remaining playback time and estimated finish time for Up Next",
            ),
            (self.recent_table, "Recently Played", "Tracks previously listened to, newest first"),
            (self.beat, "Visualiser", "Animated audio visualiser and visualiser mode selector"),
            (self.slider_progress, "Playback position", "Current position in the playing track"),
            (self.now_playing, "Current track title", "Title of the current or selected track"),
            (self.dj_info, "Current artist and playback information", "Artist and audio details"),
            (self.overlay, "Album artwork and lyrics", "Current album artwork and synchronised lyrics"),
        )
        for widget, name, description in controls:
            widget.setAccessibleName(name)
            widget.setAccessibleDescription(description)
        self.statusBar().setAccessibleName("Player status")
        self.statusBar().setAccessibleDescription("Announcements about keyboard commands and player state")
        for widget in (
            self.label_remaining, self.label_volume, self.queue_header,
            self.queue_duration_summary_label,
        ):
            widget.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        QtWidgets.QWidget.setTabOrder(self.search_box, self.tree_tracks)
        QtWidgets.QWidget.setTabOrder(self.tree_tracks, self.queue_list)
        QtWidgets.QWidget.setTabOrder(self.queue_list, self.recent_table)
        QtWidgets.QWidget.setTabOrder(self.recent_table, self.btn_prev)
        QtWidgets.QWidget.setTabOrder(self.btn_prev, self.btn_play)
        QtWidgets.QWidget.setTabOrder(self.btn_play, self.btn_pause)
        QtWidgets.QWidget.setTabOrder(self.btn_pause, self.btn_next)
        QtWidgets.QWidget.setTabOrder(self.btn_next, self.slider_progress)
        QtWidgets.QWidget.setTabOrder(self.slider_progress, self.slider_volume)
        self.btn_play.setToolTip(f"Play ({SHORTCUTS['play_pause']})")
        self.btn_pause.setToolTip(f"Pause or resume ({SHORTCUTS['play_pause']})")
        self.btn_prev.setToolTip(f"Previous track ({SHORTCUTS['previous']})")
        self.btn_next.setToolTip(f"Next track ({SHORTCUTS['next']})")
        self.slider_volume.setToolTip(
            f"Volume ({SHORTCUTS['volume_up']} / {SHORTCUTS['volume_down']})"
        )
        self.search_box.setToolTip(
            f"Search the music library ({SHORTCUTS['focus_search']})\n"
            "Prefix with a bracket tag to search one field only: "
            "[track], [artist], [album], [genre], [year], [bpm], [key]"
        )
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def _announce_accessible_status(self, message, timeout=4000):
        if not message:
            return
        self._last_accessible_announcement = str(message)
        status = self.statusBar()
        status.setAccessibleDescription(str(message))
        status.showMessage(str(message), timeout)

    def _toggle_play_pause(self):
        if self._playback_expected or self._playback_intentionally_paused:
            self.pause()
        else:
            self.play_selected()

    def stop_playback(self):
        # Playback stability hardening, Phase A: Stop is absolute --
        # current attempt -> CANCELLED, authoritative -> NONE, right here,
        # synchronously, before any of the rest of this function's own
        # teardown runs. Every async operation already in flight for the
        # cancelled attempt (Plex resolve, Plex audio load, ...) may still
        # finish on its own schedule, but _is_current_playback_attempt
        # will reject it from this point on.
        self._cancel_current_playback_attempt("stop_playback")
        transition_manager = getattr(self, "_video_transition_manager", None)
        if transition_manager is not None:
            transition_manager.playback_stopped()
        if self._current_media_type == MediaType.KARAOKE:
            self._karaoke_generation += 1
            self._exit_karaoke_fullscreen()
            self.karaoke_widget.clear()
            self._karaoke_audio_path = None
            self._karaoke_document = None
            if self.party_mode is not None:
                self.party_mode.return_to_normal_layout()
            self._show_normal_display_page()
            self._current_media_type = MediaType.AUDIO
        self._stop_video_for_audio_transition()
        if getattr(self, "cast_active", False):
            self.cast_controller.stop()
            self._cast_completion_armed = False
        self._cancel_fade()
        self._stop_all()
        self._cancel_playback_watchdog()
        self._playback_expected = False
        self._playback_intentionally_paused = False
        self.beat.setPlaying(False)
        self.btn_pause.setText("Pause")
        self.btn_pause.setAccessibleName("Pause")
        self._reset_progress()
        # No new track is being activated on a manual Stop, so
        # _activate_track_ui's own call to this won't run -- _current_media_type
        # is already back to AUDIO by this point either way (karaoke branch
        # above or _stop_video_for_audio_transition), so this restores it.
        self._sync_now_playing_overlay_for_media_type()
        self._announce_accessible_status("Playback stopped")
        getattr(self, "_schedule_queue_duration_refresh", lambda *_: None)("playback_stopped")

    # --- Sleep Timer -----------------------------------------------------

    def _build_sleep_timer_menu(self, playback_menu):
        submenu = playback_menu.addMenu("Sleep Timer")
        self._sleep_timer_actions = {}
        group = QtGui.QActionGroup(submenu)
        group.setExclusive(True)

        action_off = submenu.addAction("Off")
        action_off.setCheckable(True)
        action_off.setChecked(True)
        group.addAction(action_off)
        self._sleep_timer_actions[action_off] = ("off", None)

        action_stop_after = submenu.addAction("Stop After Current Track")
        action_stop_after.setCheckable(True)
        group.addAction(action_stop_after)
        self._sleep_timer_actions[action_stop_after] = ("stop_after_track", None)

        submenu.addSeparator()
        for minutes in PRESET_MINUTES:
            action = submenu.addAction(f"{minutes} Minutes")
            action.setCheckable(True)
            group.addAction(action)
            self._sleep_timer_actions[action] = ("timed", minutes)

        action_custom = submenu.addAction("Custom…")
        action_custom.setCheckable(True)
        group.addAction(action_custom)
        self._sleep_timer_actions[action_custom] = ("custom", None)
        self.action_sleep_timer_off = action_off
        self.action_sleep_timer_custom = action_custom
        self._sleep_timer_action_group = group

        submenu.addSeparator()
        self.action_sleep_timer_fade = submenu.addAction("Fade Before Stopping")
        self.action_sleep_timer_fade.setCheckable(True)
        self.action_sleep_timer_fade.toggled.connect(
            self._on_sleep_timer_fade_toggled
        )

        group.triggered.connect(self._on_sleep_timer_menu_action)

    def _on_sleep_timer_menu_action(self, action):
        kind, minutes = self._sleep_timer_actions.get(action, (None, None))
        if kind == "off":
            self._cancel_sleep_timer("user_off")
        elif kind == "stop_after_track":
            self._arm_stop_after_track()
        elif kind == "timed":
            self._start_sleep_timer(minutes)
        elif kind == "custom":
            self._prompt_custom_sleep_timer()

    def _prompt_custom_sleep_timer(self):
        previous_action = self._checked_sleep_timer_action()
        minutes, ok = QtWidgets.QInputDialog.getInt(
            self,
            "Custom Sleep Timer",
            "Stop after how many minutes?",
            self._sleep_timer_custom_minutes,
            MIN_CUSTOM_MINUTES,
            MAX_CUSTOM_MINUTES,
        )
        if not ok:
            if previous_action is not None:
                previous_action.setChecked(True)
            else:
                self.action_sleep_timer_off.setChecked(True)
            return
        self._sleep_timer_custom_minutes = minutes
        self._start_sleep_timer(minutes)

    def _checked_sleep_timer_action(self):
        for action in self._sleep_timer_actions:
            if action.isChecked():
                return action
        return None

    def _on_sleep_timer_fade_toggled(self, checked):
        self.sleep_timer_fade_pref = bool(checked)
        self.sleep_timer.set_fade_enabled(bool(checked))
        self._save_user_settings()

    def _sleep_timer_playback_state_label(self):
        if self._playback_intentionally_paused:
            return "paused"
        if self._playback_expected:
            return "playing"
        return "stopped"

    def _start_sleep_timer(self, minutes: int):
        self._sleep_timer_gain = 1.0
        self.sleep_timer.start_timed(
            minutes, fade_enabled=self.action_sleep_timer_fade.isChecked()
        )
        self.diagnostics.record(
            "sleep_timer", "sleep_timer_started",
            details={
                "mode": "timed",
                "requested_minutes": minutes,
                "backend": self._current_backend_name(),
                "playback_state": self._sleep_timer_playback_state_label(),
                "crossfade_active": bool(self.fade_active or self.prebuffer_active),
                "fade_enabled": bool(self.sleep_timer.state.fade_enabled),
            },
            minimum_level="basic",
        )
        self._ensure_sleep_timer_ticker()
        self._sync_sleep_timer_menu_checks()

    def _arm_stop_after_track(self):
        self._sleep_timer_gain = 1.0
        self.sleep_timer.arm_stop_after_track(
            fade_enabled=self.action_sleep_timer_fade.isChecked()
        )
        self.diagnostics.record(
            "sleep_timer", "stop_after_track_armed",
            details={
                "mode": "stop_after_track",
                "backend": self._current_backend_name(),
                "playback_state": self._sleep_timer_playback_state_label(),
                "crossfade_active": bool(self.fade_active or self.prebuffer_active),
            },
            minimum_level="basic",
        )
        self._update_sleep_timer_status()
        self._sync_sleep_timer_menu_checks()

    def _cancel_sleep_timer(self, reason: str = "user_off"):
        was_active = self.sleep_timer.is_active
        mode = self.sleep_timer.state.mode
        elapsed = self.sleep_timer.elapsed_seconds()
        self._teardown_sleep_timer()
        if was_active:
            self.diagnostics.record(
                "sleep_timer", "sleep_timer_cancelled",
                details={
                    "mode": mode,
                    "elapsed_seconds": elapsed,
                    "cancellation_reason": reason,
                },
                minimum_level="basic",
            )

    def _complete_stop_after_track(self, reason: str):
        self.diagnostics.record(
            "sleep_timer", "stop_after_track_completed",
            details={
                "reason": reason,
                "backend": self._current_backend_name(),
                "playback_state": self._sleep_timer_playback_state_label(),
                "crossfade_active": bool(self.fade_active or self.prebuffer_active),
            },
            minimum_level="basic",
        )
        self.stop_playback()
        self._teardown_sleep_timer()

    def _expire_sleep_timer(self):
        requested_minutes = self.sleep_timer.state.duration_minutes
        elapsed = self.sleep_timer.elapsed_seconds()
        crossfade_was_active = bool(self.fade_active or self.prebuffer_active)
        fade_enabled = bool(self.sleep_timer.state.fade_enabled)
        backend = self._current_backend_name()
        playback_state = self._sleep_timer_playback_state_label()
        self.diagnostics.record(
            "sleep_timer", "sleep_timer_expired",
            details={
                "mode": "timed",
                "requested_minutes": requested_minutes,
                "elapsed_seconds": elapsed,
                "backend": backend,
                "playback_state": playback_state,
                "crossfade_active": crossfade_was_active,
                "fade_enabled": fade_enabled,
            },
            minimum_level="basic",
        )
        stop_started = time.perf_counter()
        self.stop_playback()
        stop_duration_ms = (time.perf_counter() - stop_started) * 1000.0
        self.diagnostics.record(
            "sleep_timer", "sleep_timer_stopped_playback",
            details={
                "mode": "timed",
                "backend": backend,
                "playback_state": playback_state,
                "crossfade_active": crossfade_was_active,
                "fade_enabled": fade_enabled,
                "stop_duration_ms": stop_duration_ms,
            },
            minimum_level="basic",
        )
        self._teardown_sleep_timer()

    def _teardown_sleep_timer(self):
        self._stop_sleep_timer_ticker()
        self.sleep_timer.cancel()
        self._restore_sleep_timer_volume()
        self._sync_sleep_timer_menu_checks()
        if hasattr(self, "statusBar"):
            self.statusBar().clearMessage()
        self._sync_mini_player()

    def _restore_sleep_timer_volume(self):
        if self._sleep_timer_gain == 1.0:
            return
        self._sleep_timer_gain = 1.0
        self._reapply_sleep_timer_gain()

    def _reapply_sleep_timer_gain(self):
        if self.fade_active:
            return
        try:
            if self._use_builtin_player():
                if self.simple_player:
                    self.simple_player.set_volume(
                        combine_volume(
                            self.master_volume / 100.0,
                            self._active_normalisation_gain,
                            self._sleep_timer_gain,
                        )
                    )
                if self.simple_inactive_player:
                    self.simple_inactive_player.set_volume(
                        combine_volume(
                            self.master_volume / 100.0,
                            self._inactive_normalisation_gain,
                            self._sleep_timer_gain,
                        )
                    )
            elif self.active_player:
                self._set_volume(self.active_player, self._sleep_timer_gain)
        except Exception:
            pass

    def _apply_sleep_timer_gain(self, gain: float):
        gain = max(0.0, min(1.0, float(gain)))
        if gain == self._sleep_timer_gain:
            return
        self._sleep_timer_gain = gain
        self._reapply_sleep_timer_gain()

    def _ensure_sleep_timer_ticker(self):
        if self._sleep_timer_timer is None:
            self._sleep_timer_timer = QtCore.QTimer(self)
            self._sleep_timer_timer.timeout.connect(self._on_sleep_timer_tick)
        if not self._sleep_timer_timer.isActive():
            self._sleep_timer_timer.start(SLEEP_TIMER_TICK_MS)
        self._on_sleep_timer_tick()

    def _stop_sleep_timer_ticker(self):
        if self._sleep_timer_timer is not None:
            self._sleep_timer_timer.stop()

    def _on_sleep_timer_tick(self):
        if not self.sleep_timer.is_timed:
            return
        if self.sleep_timer.should_be_fading():
            if self.sleep_timer.mark_fade_started():
                self.diagnostics.record(
                    "sleep_timer", "sleep_timer_fade_started",
                    details={
                        "mode": "timed",
                        "backend": self._current_backend_name(),
                        "crossfade_active": bool(
                            self.fade_active or self.prebuffer_active
                        ),
                    },
                    minimum_level="basic",
                )
            self._apply_sleep_timer_gain(self.sleep_timer.fade_gain())
        if self.sleep_timer.is_expired():
            self._expire_sleep_timer()
            return
        self._update_sleep_timer_status()

    def _sleep_timer_status_text(self) -> str:
        if self.sleep_timer.is_timed:
            return f"Sleep timer: {self.sleep_timer.format_remaining()} remaining"
        if self.sleep_timer.is_stop_after_track:
            return "Sleep timer: stop after current track"
        return ""

    def _update_sleep_timer_status(self):
        message = self._sleep_timer_status_text()
        if hasattr(self, "statusBar"):
            if message:
                self.statusBar().showMessage(message)
            else:
                self.statusBar().clearMessage()
        self._sync_mini_player()

    def _sync_sleep_timer_menu_checks(self):
        actions = getattr(self, "_sleep_timer_actions", None)
        if not actions:
            return
        state = self.sleep_timer.state
        target = None
        if state.mode == "stop_after_track" and state.stop_after_track_armed:
            for action, (kind, _minutes) in actions.items():
                if kind == "stop_after_track":
                    target = action
                    break
        elif state.mode == "timed" and state.deadline is not None:
            for action, (kind, minutes) in actions.items():
                if kind == "timed" and minutes == state.duration_minutes:
                    target = action
                    break
            if target is None:
                target = self.action_sleep_timer_custom
        else:
            target = self.action_sleep_timer_off
        if target is not None:
            with QtCore.QSignalBlocker(target):
                target.setChecked(True)

    def _adjust_volume(self, delta):
        value = max(self.slider_volume.minimum(), min(self.slider_volume.maximum(), self.master_volume + delta))
        self.slider_volume.setValue(value)
        self._muted = value == 0
        self.action_mute.setText("Unmute" if self._muted else "Mute")
        self._announce_accessible_status(f"Volume: {value}%")

    def toggle_mute(self):
        if self._muted or self.master_volume == 0:
            value = max(1, int(self._volume_before_mute or 70))
            self.slider_volume.setValue(value)
            self._muted = False
            message = "Unmuted"
        else:
            self._volume_before_mute = self.master_volume
            self.slider_volume.setValue(0)
            self._muted = True
            message = "Muted"
        self.action_mute.setText("Unmute" if self._muted else "Mute")
        self._announce_accessible_status(message)

    def _focus_library_search(self):
        self.search_box.setFocus(QtCore.Qt.FocusReason.ShortcutFocusReason)
        self.search_box.selectAll()

    def _focus_library(self):
        self.tree_tracks.setFocus(QtCore.Qt.FocusReason.ShortcutFocusReason)

    def _focus_up_next(self):
        self.right_tabs.setCurrentWidget(self.up_next_page)
        self.queue_list.setFocus(QtCore.Qt.FocusReason.ShortcutFocusReason)
        if self.queue_list.currentRow() < 0 and self.queue_list.count():
            self.queue_list.setCurrentRow(0)
        if not self.queue_list.count():
            self._announce_accessible_status("Up Next is empty")

    def _show_recently_played(self):
        if self.mini_player is not None and self.mini_player.isVisible():
            self._restore_full_player()
        self.right_tabs.setCurrentWidget(self.recent_page)
        self.recent_table.setFocus(QtCore.Qt.FocusReason.ShortcutFocusReason)

    def _history_duration_seconds(self, meta):
        value = meta.get("duration_seconds")
        if value is not None:
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                pass
        text = str(meta.get("duration") or "")
        if not text or text == "Unknown":
            return 0.0
        try:
            total = 0
            for part in text.split(":"):
                total = total * 60 + int(part)
            return float(total)
        except (TypeError, ValueError):
            return 0.0

    def _reset_recently_played_tracking(self, path):
        meta = self._meta_by_path.get(path) or {}
        self.recently_played_tracker.reset(
            path, self._playback_generation,
            self._history_duration_seconds(meta),
        )

    def _update_recently_played_tracking(self):
        tracker = self.recently_played_tracker
        if not tracker.path or tracker.path != self.current_path:
            return
        position = self._player_clock_s()
        qualified = tracker.update(
            time.monotonic(), position,
            active=bool(self._playback_expected),
            paused=bool(self._playback_intentionally_paused),
            seeking=bool(self.scrubbing),
            closing=bool(getattr(self, "_closing", False)),
        )
        if qualified:
            self._record_recently_played_entry()

    def _record_recently_played_entry(self):
        tracker = self.recently_played_tracker
        path = tracker.path
        if not path or path != self.current_path:
            return
        meta = self._display_meta_for_path(path)
        entry = make_entry(
            path,
            str(meta.get("title") or os.path.splitext(os.path.basename(path))[0]),
            str(meta.get("artist") or ""),
            str(meta.get("album") or ""),
            tracker.duration_seconds,
        )
        self.recently_played_entries = add_entry(
            self.recently_played_entries, entry
        )
        self._log(
            f"Recently Played qualified: path={path!r}; "
            f"listened_seconds={tracker.listened_seconds:.1f}; "
            f"threshold_seconds={tracker.threshold_seconds:.1f}; "
            f"generation={tracker.generation}"
        )
        self._refresh_recently_played_table()
        try:
            self.recently_played_repository.save(self.recently_played_entries)
        except Exception as ex:
            self._log(f"Recently Played save failed: {ex}")

    def _recent_entry_key(self, entry):
        return (entry.path, entry.played_at)

    def _selected_recent_entries(self):
        rows = sorted({index.row() for index in self.recent_table.selectionModel().selectedRows()})
        return [
            self.recently_played_entries[row]
            for row in rows if 0 <= row < len(self.recently_played_entries)
        ]

    def _refresh_recently_played_table(self):
        if not hasattr(self, "recent_table"):
            return
        diagnostic_started = time.perf_counter()
        selected = {
            self._recent_entry_key(entry)
            for entry in self._selected_recent_entries()
        }
        table = self.recent_table
        table.setUpdatesEnabled(False)
        table.setRowCount(len(self.recently_played_entries))
        library_paths = set(self._meta_by_path) if hasattr(self, "_meta_by_path") else set()
        for row, entry in enumerate(self.recently_played_entries):
            played = entry.played_at.replace("T", " ").replace("Z", "")[:16]
            for column, text in enumerate(
                (entry.title, entry.artist, entry.album, played)
            ):
                item = QtWidgets.QTableWidgetItem(text)
                item.setToolTip(text)
                item.setData(
                    QtCore.Qt.ItemDataRole.UserRole,
                    {"path": entry.path, "played_at": entry.played_at},
                )
                if library_paths and entry.path not in library_paths:
                    item.setForeground(QtGui.QColor("#756b87"))
                    item.setToolTip(f"{text}\nUnavailable in the current library")
                table.setItem(row, column, item)
            if self._recent_entry_key(entry) in selected:
                table.selectRow(row)
        table.setUpdatesEnabled(True)
        self.diagnostics.record(
            "gui", "refresh_recently_played",
            duration_ms=(time.perf_counter() - diagnostic_started) * 1000.0,
            details={"rows": len(self.recently_played_entries)},
            minimum_level="detailed",
        )

    def _recent_path_available(self, path):
        if path in self._meta_by_path or path in self.track_index_by_path:
            return True
        return os.path.isfile(path)

    def _play_recently_played_selected(self):
        entries = self._selected_recent_entries()
        if len(entries) != 1:
            return
        entry = entries[0]
        if not self._recent_path_available(entry.path):
            self._announce_accessible_status(
                "This track is no longer available at its saved location."
            )
            return
        self.play_path(entry.path, crossfade=False)

    def _remove_recent_entries(self, entries):
        keys = {self._recent_entry_key(entry) for entry in entries}
        self.recently_played_entries = [
            entry for entry in self.recently_played_entries
            if self._recent_entry_key(entry) not in keys
        ]
        self._refresh_recently_played_table()
        self.recently_played_repository.save(self.recently_played_entries)
        self._announce_accessible_status("History entry removed")

    def _locate_recent_in_library(self, entry, select_track):
        meta = self._meta_by_path.get(entry.path)
        if not meta:
            self._announce_accessible_status("Track is unavailable")
            return
        self.search_box.clear()

        def locate(attempt=0):
            album_key = self.album_key_by_path.get(entry.path)
            album_item = self.album_item_by_key.get(album_key) if album_key else None
            if album_item is None and attempt < 10:
                QtCore.QTimer.singleShot(100, lambda: locate(attempt + 1))
                return
            if album_item is None:
                self._announce_accessible_status("Album is not currently visible")
                return
            if select_track:
                self._populate_album_item(album_item, immediate=True, reason="history")
                track_item = self.tree_item_by_path.get(entry.path)
                if track_item is not None:
                    self.tree_tracks.setCurrentItem(track_item)
                    self.tree_tracks.scrollToItem(track_item)
            else:
                self.tree_tracks.setCurrentItem(album_item)
                self.tree_tracks.scrollToItem(album_item)
            self._focus_library()

        locate()

    def _show_recently_played_menu(self, pos):
        entries = self._selected_recent_entries()
        menu = QtWidgets.QMenu(self)
        action_play = menu.addAction("Play Now")
        action_play.setEnabled(len(entries) == 1)
        action_next = menu.addAction("Play Next")
        action_add = menu.addAction("Add to Up Next")
        action_next.setEnabled(bool(entries))
        action_add.setEnabled(bool(entries))
        menu.addSeparator()
        action_album = menu.addAction("Show Album in Library")
        action_track = menu.addAction("Show Track in Library")
        action_album.setEnabled(len(entries) == 1)
        action_track.setEnabled(len(entries) == 1)
        menu.addSeparator()
        action_remove = menu.addAction("Remove from Recently Played")
        action_remove.setEnabled(bool(entries))
        action_clear = menu.addAction("Clear Recently Played History")
        self._add_window_menu_options(menu)
        action = menu.exec(self.recent_table.viewport().mapToGlobal(pos))
        if action == action_play:
            self._play_recently_played_selected()
        elif action == action_next:
            for entry in reversed(entries):
                self._queue_play_next(entry.path)
            self._announce_accessible_status("Track added to Up Next")
        elif action == action_add:
            self._add_to_queue_with_dedup_guard([entry.path for entry in entries])
        elif action == action_album:
            self._locate_recent_in_library(entries[0], False)
        elif action == action_track:
            self._locate_recent_in_library(entries[0], True)
        elif action == action_remove:
            self._remove_recent_entries(entries)
        elif action == action_clear:
            answer = QtWidgets.QMessageBox.question(
                self, "Clear Recently Played",
                "Clear the complete Recently Played history?",
            )
            if answer == QtWidgets.QMessageBox.StandardButton.Yes:
                self.recently_played_entries = []
                self._refresh_recently_played_table()
                self.recently_played_repository.save([])
                self._announce_accessible_status(
                    "Recently Played history cleared"
                )

    def _toggle_lyrics_accessibly(self):
        shown = not bool(self.lyrics_enabled)
        self._set_lyrics_enabled(shown)
        self.action_toggle_lyrics.setChecked(shown)
        self._announce_accessible_status("Lyrics shown" if shown else "Lyrics hidden")

    def _toggle_visualiser_accessibly(self):
        if self.visualiser_frame is None:
            self.action_toggle_visualiser.setChecked(False)
            self._announce_accessible_status("Visualiser unavailable with the current installation")
            return
        shown = self.action_toggle_visualiser.isChecked()
        self._apply_visualiser_layout(shown)
        self._announce_accessible_status("Visualiser shown" if shown else "Visualiser hidden")

    def _apply_visualiser_layout(self, shown: bool):
        """Collapse the visualiser and let Up Next reclaim its layout space."""
        frame = self.visualiser_frame
        if frame is None:
            return
        shown = bool(shown)
        if not shown and self._visualiser_fullscreen:
            self._exit_visualiser_fullscreen()
        central = self.centralWidget()
        central.setUpdatesEnabled(False)
        try:
            if shown:
                frame.show()
                handle = self.right_splitter.handle(1)
                if handle is not None:
                    handle.show()
                self._restore_right_splitter_position()
            else:
                self._remember_right_splitter_state()
                frame.hide()
                handle = self.right_splitter.handle(1)
                if handle is not None:
                    handle.hide()
            self.action_toggle_visualiser.setChecked(shown)
        finally:
            central.setUpdatesEnabled(True)
            central.update()
        getattr(self, "_refresh_visualiser_lifecycle", lambda *_: None)("visualiser_panel_toggled")

    # -- visualiser suspend/resume lifecycle --------------------------------
    # Stops the main visualiser's own render timer (BeatWidget.suspend/
    # resume) whenever it isn't actually visible to the user, and the
    # shared analyzer_timer feed (mel-spectrogram lookups) whenever neither
    # this window's visualiser nor Party Mode's needs it. Does not touch
    # playback, crossfade, BPM/key analysis or library scanning -- those
    # run on entirely separate timers/workers.

    def _effective_visualiser_visibility(self) -> bool:
        return should_visualiser_run(
            window_minimized=self.isMinimized(),
            window_visible=self.isVisible(),
            panel_visible=bool(
                getattr(self, "visualiser_frame", None)
                and self.visualiser_frame.isVisible()
                and getattr(self, "_current_media_type", MediaType.AUDIO)
                == MediaType.AUDIO
            ),
            mini_player_active=bool(
                getattr(self, "mini_player", None) and self.mini_player.isVisible()
            ),
            closing=getattr(self, "_closing", False),
        )

    def _effective_analyzer_feed_needed(self) -> bool:
        if self._visualiser_lifecycle.is_active:
            return True
        party_mode = getattr(self, "party_mode", None)
        if (
            party_mode is not None
            and getattr(party_mode, "active_layout", None) == "visualiser"
            and party_mode.isVisible()
            and not getattr(party_mode, "_video_active", False)
        ):
            return True
        return False

    def _refresh_visualiser_lifecycle(self, reason: str):
        now = time.perf_counter()
        transition = self._visualiser_lifecycle.evaluate(
            self._effective_visualiser_visibility(), reason=reason, now=now,
        )
        if transition is not None:
            self._apply_visualiser_transition(transition)
        feed_transition = self._analyzer_feed_lifecycle.evaluate(
            self._effective_analyzer_feed_needed(), reason=reason, now=now,
        )
        if feed_transition is not None:
            self._apply_analyzer_feed_transition(feed_transition)

    def _apply_visualiser_transition(self, transition: "VisualiserTransition"):
        started = time.perf_counter()
        operation = (
            "resumed" if transition.current is VisualiserRunState.ACTIVE
            else "suspended"
        )
        try:
            beat = getattr(self, "beat", None)
            if beat is None:
                return
            if transition.current is VisualiserRunState.ACTIVE:
                beat.resume()
            else:
                beat.suspend()
        except Exception as ex:
            self._record_visualiser_diagnostics(
                "resume_failed" if operation == "resumed" else "suspend_failed",
                transition, error=str(ex),
            )
            return
        self._record_visualiser_diagnostics(
            operation, transition,
            resume_latency_ms=(
                (time.perf_counter() - started) * 1000.0
                if operation == "resumed" else None
            ),
        )

    def _apply_analyzer_feed_transition(self, transition: "VisualiserTransition"):
        timer = getattr(self, "analyzer_timer", None)
        if timer is None:
            return
        started = time.perf_counter()
        operation = (
            "resumed" if transition.current is VisualiserRunState.ACTIVE
            else "suspended"
        )
        try:
            if transition.current is VisualiserRunState.ACTIVE:
                if not timer.isActive():
                    timer.start()
                    self._analyzer_tick()
            else:
                timer.stop()
        except Exception as ex:
            self._record_visualiser_diagnostics(
                "resume_failed" if operation == "resumed" else "suspend_failed",
                transition, error=str(ex),
            )
            return
        self._record_visualiser_diagnostics(
            operation, transition,
            resume_latency_ms=(
                (time.perf_counter() - started) * 1000.0
                if operation == "resumed" else None
            ),
        )

    def _record_visualiser_diagnostics(
        self, operation, transition: "VisualiserTransition", *,
        resume_latency_ms=None, error=None,
    ):
        diagnostics = getattr(self, "diagnostics", None)
        if diagnostics is None:
            return
        beat = getattr(self, "beat", None)
        details = {
            "consumer": transition.consumer,
            "reason": transition.reason,
            "visual_mode": getattr(beat, "visual_mode", None),
            "window_minimised": self.isMinimized(),
            "window_visible": self.isVisible(),
            "visualiser_visible": bool(
                getattr(self, "visualiser_frame", None)
                and self.visualiser_frame.isVisible()
            ),
            "mini_player_active": bool(
                getattr(self, "mini_player", None) and self.mini_player.isVisible()
            ),
            "render_timer_active": not getattr(beat, "is_suspended", True),
            "analyzer_timer_active": bool(
                getattr(self, "analyzer_timer", None)
                and self.analyzer_timer.isActive()
            ),
        }
        if transition.suspended_seconds is not None:
            details["suspended_seconds"] = round(transition.suspended_seconds, 3)
        if resume_latency_ms is not None:
            details["resume_latency_ms"] = round(resume_latency_ms, 3)
        if error is not None:
            details["error"] = error
        diagnostics.record(
            "visualiser", operation,
            severity="warning" if error is not None else "info",
            details=details,
            minimum_level="basic" if error is not None else "detailed",
        )

    def _remember_right_splitter_state(self, *args):
        splitter = getattr(self, "right_splitter", None)
        frame = getattr(self, "visualiser_frame", None)
        if (
            splitter is not None
            and frame is not None
            and frame.isVisible()
            and len(splitter.sizes()) == 2
            and min(splitter.sizes()) > 0
        ):
            self._visualiser_splitter_state = splitter.saveState()
            timer = getattr(self, "_splitter_save_timer", None)
            if timer is not None:
                timer.start()

    def _restore_right_splitter_position(self):
        started = time.perf_counter()
        splitter = getattr(self, "right_splitter", None)
        if splitter is None:
            return
        restored = False
        state = getattr(self, "_visualiser_splitter_state", None)
        if state:
            restored = splitter.restoreState(state)
        if not restored:
            splitter.setSizes([650, 350])
        diagnostics = getattr(self, "diagnostics", None)
        if diagnostics is not None:
            diagnostics.record(
                "gui", "restore_visualiser_splitter",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                details={"saved_state_restored": bool(restored)},
                minimum_level="detailed",
            )

    def _toggle_mini_player(self):
        if self.mini_player is not None and self.mini_player.isVisible():
            self._restore_full_player()
        else:
            self._show_mini_player()

    def _show_mini_player(self):
        started = time.perf_counter()
        created = self.mini_player is None
        if self.mini_player is None:
            self.mini_player = MiniPlayerWindow(self)
            self.mini_player.return_to_full_requested.connect(self._restore_full_player)
            self.mini_player.settings_changed.connect(self._save_mini_player_geometry)
        self.mini_player.restore_geometry_safely(self.mini_player_geometry)
        self.mini_player.set_always_on_top(self.mini_player_always_on_top)
        self.mini_player.sync_from_owner(force=True)
        self.mini_player.show()
        self.mini_player.raise_()
        self.mini_player.activateWindow()
        self.hide()
        getattr(self, "_refresh_visualiser_lifecycle", lambda *_: None)("mini_player_shown")
        diagnostics = getattr(self, "diagnostics", None)
        if diagnostics is not None:
            diagnostics.record(
                "gui", "show_mini_player",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                details={"created": created},
                minimum_level="detailed",
            )

    def _restore_full_player(self):
        if self.mini_player is not None:
            self._save_mini_player_geometry()
            self.mini_player.hide()
        self.show()
        self.raise_()
        self.activateWindow()
        getattr(self, "_refresh_visualiser_lifecycle", lambda *_: None)("mini_player_hidden")

    def _save_mini_player_geometry(self):
        if self.mini_player is None:
            return
        self.mini_player_geometry = self.mini_player.geometry_settings()
        self.mini_player_always_on_top = self.mini_player.btn_top.isChecked()
        self._save_user_settings()

    def _toggle_party_mode(self):
        if self.party_mode is not None and self.party_mode.isVisible():
            self._hide_party_mode()
        else:
            self._show_party_mode()

    def _show_party_mode(self):
        started = time.perf_counter()
        created = self.party_mode is None
        if self.party_mode is None:
            self.party_mode = PartyModeWindow(self)
        self.party_mode.open_party_mode()
        diagnostics = getattr(self, "diagnostics", None)
        if diagnostics is not None:
            diagnostics.record(
                "gui", "show_party_mode",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                details={"created": created},
                minimum_level="detailed",
            )

    def _hide_party_mode(self):
        if self.party_mode is not None:
            self.party_mode.hide()

    def _sync_mini_player(self):
        if self.mini_player is not None:
            self.mini_player.sync_from_owner()

    def _sync_party_mode(self):
        if self.party_mode is not None and self.party_mode.isVisible():
            self.party_mode.sync_from_owner()

    def _sync_karaoke_position(self):
        """Drives the CDG graphics from the backing MP3's own reported
        position -- there is no independent graphics clock to drift, so
        pausing (the backend's position stops advancing) freezes the
        picture for free, and a seek is picked up the next time this runs
        after the backend reports the new position."""
        if self._current_media_type != MediaType.KARAOKE or self._karaoke_document is None:
            return
        try:
            if self._use_builtin_player():
                karaoke_position_ms = int(self.simple_player.get_pos() * 1000)
            elif self.active_player:
                karaoke_position_ms = max(0, int(self.active_player.get_time()))
            else:
                karaoke_position_ms = 0
            self.karaoke_widget.set_position(karaoke_position_ms)
            if self.party_mode is not None and self.party_mode.isVisible():
                self.party_mode.set_karaoke_position(karaoke_position_ms)
        except Exception:
            pass

    def resizeEvent(self, event):
        try:
            if getattr(self, "overlay", None) is not None:
                self.overlay.setGeometry(self.centralWidget().rect())
                self.overlay.raise_()
        except Exception:
            pass
        super().resizeEvent(event)

    def showEvent(self, event):
        # Explicit base-class call (not super()) so this stays safe if ever
        # bound as an unbound method onto an unrelated test harness class,
        # the pattern already used throughout tests/ -- a zero-arg super()
        # resolves against the class it was *defined* in, not the instance's
        # actual class, which crashes PyQt6's event dispatch outright rather
        # than raising a catchable Python exception.
        QtWidgets.QMainWindow.showEvent(self, event)
        self._refresh_visualiser_lifecycle("window_shown")

    def hideEvent(self, event):
        QtWidgets.QMainWindow.hideEvent(self, event)
        self._refresh_visualiser_lifecycle("window_hidden")

    def changeEvent(self, event):
        QtWidgets.QMainWindow.changeEvent(self, event)
        if event.type() == QtCore.QEvent.Type.WindowStateChange:
            self._refresh_visualiser_lifecycle("window_state_changed")

    def _play_queue_save_spin(self):
        if not hasattr(self, "queue_spin_anim") or self.queue_spin_anim.state() == QtCore.QAbstractAnimation.State.Running:
            return
        # Every so often the playlist button does a quick DJ-style spin, then eases back down.
        start = float(getattr(self.btn_save_queue, "rotation", 0.0))
        self.queue_spin_anim.setStartValue(start)
        self.queue_spin_anim.setEndValue(start + 720.0)
        self.queue_spin_anim.start()
    def _ensure_vlc(self):
        if not VLC_AVAILABLE:
            self._show_vlc_error(f"VLC support could not be loaded: {VLC_IMPORT_ERROR}")
            return
        if self.instance is not None:
            return
        try:
            self.instance = vlc.Instance(
                "--quiet",
                "--no-video",
                f"--file-caching={VLC_LOCAL_FILE_CACHING_MS}",
                f"--network-caching={VLC_NETWORK_CACHING_MS}",
                "--aout=directsound"
            )
            self.player_a = self.instance.media_player_new()
            self.player_b = self.instance.media_player_new()
            # Volume is connected once in _connect_signals so VLC and miniaudio share the same control.
            self.active_player = self.player_a
            self.inactive_player = self.player_b
        except Exception as ex:
            self.instance = None
            self.active_player = None
            self.inactive_player = None
            self._show_vlc_error(f"VLC could not start: {ex}")

    def _show_vlc_error(self, message: str):
        self._log(message)
        if self._vlc_error_shown:
            return
        self._vlc_error_shown = True
        QtWidgets.QMessageBox.warning(
            self,
            "VLC Playback",
            message + "\n\nPlease install 64-bit VLC, then restart Bills Music Player.",
        )

    # --- Optional Google Cast output ------------------------------------

    def _on_output_selected(self, row):
        choice = self.output_combo.itemData(int(row))
        if choice == "__refresh__":
            self.cast_discovery.refresh()
            self.output_combo.setCurrentIndex(0 if not self.cast_active else row)
            return
        if choice is None:
            if self.cast_active:
                self._return_to_local_output()
            return
        if self.cast_active and self._cast_pending_device == choice:
            return
        self._begin_cast_switch(choice)

    def _on_cast_devices(self, devices):
        if getattr(self, "_closing", False):
            return
        self._cast_devices = list(devices)
        selected_uuid = (
            getattr(self._cast_pending_device, "uuid", None)
            if self._cast_pending_device else None
        )
        self.output_combo.blockSignals(True)
        self.output_combo.clear()
        self.output_combo.addItem("This Computer", None)
        selected_row = 0
        for device in self._cast_devices:
            self.output_combo.addItem(device.name, device)
            if device.uuid == selected_uuid:
                selected_row = self.output_combo.count() - 1
        self.output_combo.addItem("Refresh Cast devices…", "__refresh__")
        self.output_combo.setCurrentIndex(selected_row)
        self.output_combo.blockSignals(False)

    def _on_cast_state(self, state, message):
        if getattr(self, "_closing", False):
            return
        self.diagnostics.record(
            "cast", "state_changed",
            details={"state": state},
            minimum_level="basic",
        )
        if message:
            self.statusBar().showMessage(message, 5000)

    def _record_cast_clock_snapshot(self, snapshot):
        """Rate-limited receiver timing evidence for real-device diagnosis."""
        now = time.monotonic()
        previous = float(
            getattr(self, "_cast_clock_diagnostic_at", 0.0) or 0.0
        )
        if previous > 0.0 and now - previous < 10.0:
            return
        self._cast_clock_diagnostic_at = now
        self.diagnostics.record(
            "cast", "clock_snapshot",
            details={
                "state": str(snapshot.get("state", "")),
                "raw_position_seconds": round(
                    float(snapshot.get("raw_position", 0.0) or 0.0), 3
                ),
                "position_seconds": round(
                    float(snapshot.get("position", 0.0) or 0.0), 3
                ),
                "position_source": str(
                    snapshot.get("position_source", "unknown")
                ),
                "duration_seconds": round(
                    float(snapshot.get("duration", 0.0) or 0.0), 3
                ),
            },
            minimum_level="basic",
        )

    def _local_output_snapshot(self):
        position = 0.0
        try:
            if self._use_builtin_player() and self.simple_player:
                position = float(self.simple_player.get_pos() or 0.0)
            elif self.active_player:
                position = max(0.0, float(self.active_player.get_time() or 0) / 1000)
        except Exception:
            pass
        return {
            "path": self.current_path,
            "position": position,
            "playing": bool(
                self._playback_expected and not self._playback_intentionally_paused
            ),
            "paused": bool(self._playback_intentionally_paused),
        }

    def _pause_local_for_cast(self):
        try:
            if self._use_builtin_player():
                for player in self._built_in_players():
                    if player and player.is_playing():
                        player.pause()
            elif self.active_player and self.active_player.is_playing():
                self.active_player.pause()
        except Exception as ex:
            self._log(f"Could not pause local output for Cast: {ex}")

    def _resume_local_snapshot(self, snapshot):
        if not snapshot or not snapshot.get("playing"):
            return
        try:
            if self._use_builtin_player():
                for player in self._built_in_players():
                    if bool(getattr(player, "_paused", False)):
                        player.resume()
            elif self.active_player:
                self.active_player.play()
        except Exception as ex:
            self._log(f"Could not restore local playback after Cast failure: {ex}")

    def _begin_cast_switch(self, device):
        if not self.current_path:
            self.statusBar().showMessage("Play a track before selecting Cast", 5000)
            self.output_combo.setCurrentIndex(0)
            return
        snapshot = self._local_output_snapshot()
        self._pause_local_for_cast()
        self._cast_pending_snapshot = snapshot
        self._cast_pending_device = device
        self.cast_controller.connect_device(device)

    def _ensure_cast_artwork_temp_dir(self) -> str:
        if self._cast_artwork_temp is None:
            self._cast_artwork_temp = tempfile.TemporaryDirectory(
                prefix="billsmusic-cast-art-"
            )
        return self._cast_artwork_temp.name

    def _cast_payload_with_fresh_artwork(self, path: str, payload: dict) -> dict:
        """v1.0.69: cached/completed payloads never carry a baked-in
        artwork URL -- CastPayloadWorker only ever produces the local
        artwork file path (self._cast_artwork_paths), never a server
        token/URL (see its own docstring for why). This registers a
        *fresh* token right before every actual Cast load, whether the
        payload came from cache or just finished, so a revoke_all() that
        happened at any point before this call (every Cast connect/track
        advance starts with one) can never leave a reusable cached
        payload pointing at a dead URL. Artwork is dropped, not left
        broken, if registration fails for any reason (including the
        server being permanently closed -- see media_server.py's
        close())."""
        artwork_path = self._cast_artwork_paths.get(path)
        if not artwork_path:
            return payload
        payload = dict(payload)
        try:
            payload["images"] = [{"url": self.cast_media_server.register(artwork_path, "image/jpeg")}]
        except Exception:
            payload.pop("images", None)
            if getattr(self, "_closing", False):
                self.diagnostics.record(
                    "cast", "cast_registration_refused_closing",
                    details=self.diagnostics.path_details(path),
                    minimum_level="detailed",
                )
        return payload

    def _request_cast_load(self, media_url, content_type, path, position, autoplay):
        """v1.0.67 MainThread I/O hardening: _cast_payload's tag read and
        artwork extract/scale/encode/write used to run synchronously here,
        on both Cast connect and every single Cast track advance. A cache
        hit (already built this session -- e.g. looping the same track)
        applies immediately with no I/O; a miss dispatches CastPayloadWorker
        and calls cast_controller.load_async (itself already dispatched to
        its own background thread, see cast_service.py) once the payload
        is ready -- generation-guarded so a superseded request (another
        track change before this one finishes) is dropped.

        v1.0.69: the generation now advances for *every* logical request,
        cache hit or miss, checked before the cache lookup -- previously
        only a cache miss advanced it, so a rapid Next that landed on a
        cache hit left an earlier still-in-flight miss looking current
        when it finally returned, and it could load after the newer
        track (Codex-reported "old-track Cast race")."""
        if getattr(self, "_closing", False):
            return
        self._cast_payload_generation += 1
        generation = self._cast_payload_generation
        self.diagnostics.record(
            "cast", "cast_request_started",
            details={"request_id": generation, **self.diagnostics.path_details(path)},
            minimum_level="detailed",
        )
        cached = self._cast_payload_cache.get(path)
        if cached is not None:
            self.diagnostics.record(
                "cast", "cast_payload_cache_hit",
                details={"request_id": generation, **self.diagnostics.path_details(path)},
                minimum_level="detailed",
            )
            payload = self._cast_payload_with_fresh_artwork(path, cached)
            self.cast_controller.load_async(media_url, content_type, payload, position, autoplay)
            return
        artwork_dir = self._ensure_cast_artwork_temp_dir()
        worker = CastPayloadWorker(generation, path, artwork_dir)
        self._cast_payload_workers.append(worker)
        token = self._worker_registry.register("cast_payload", thread=worker, wait_ms=1500)
        self.diagnostics.record(
            "cast", "cast_payload_worker_started",
            details={"request_id": generation, **self.diagnostics.path_details(path)},
            minimum_level="detailed",
        )

        def _on_ready(ready_generation, ready_path, payload, artwork_path):
            if payload:
                self._cast_payload_cache[ready_path] = payload
            self._cast_artwork_paths[ready_path] = artwork_path
            stale = ready_generation != self._cast_payload_generation
            if getattr(self, "_closing", False) or stale:
                if stale:
                    self.diagnostics.record(
                        "cast", "cast_payload_stale_dropped",
                        details={
                            "request_id": ready_generation,
                            **self.diagnostics.path_details(ready_path),
                        },
                        minimum_level="detailed",
                    )
                return
            final_payload = self._cast_payload_with_fresh_artwork(ready_path, payload)
            self.cast_controller.load_async(media_url, content_type, final_payload, position, autoplay)

        def _on_finished(worker=worker, token=token):
            if worker in self._cast_payload_workers:
                self._cast_payload_workers.remove(worker)
            self._worker_registry.unregister(token)

        worker.payload_ready.connect(_on_ready)
        worker.finished.connect(_on_finished)
        worker.start()

    def _on_cast_connected(self, device):
        if getattr(self, "_closing", False):
            return
        snapshot = self._cast_pending_snapshot or {}
        path = snapshot.get("path")
        try:
            self.cast_media_server.revoke_all()
            media_url = self.cast_media_server.register_audio(path)
            content_type = AUDIO_TYPES[os.path.splitext(path)[1].casefold()]
            self._request_cast_load(
                media_url, content_type, path,
                snapshot.get("position", 0.0), snapshot.get("playing", False),
            )
        except Exception as ex:
            self._on_cast_failed(str(ex))

    def _on_cast_loaded(self):
        if getattr(self, "_closing", False):
            return
        snapshot = self._cast_pending_snapshot or {}
        self._stop_all()
        self.cast_active = True
        self._cast_loss_reported = False
        self._playback_expected = bool(snapshot.get("playing") or snapshot.get("paused"))
        self._playback_intentionally_paused = not bool(snapshot.get("playing"))
        self._cast_completion_armed = bool(snapshot.get("playing"))
        self._cast_last_state = "playing" if snapshot.get("playing") else "paused"
        self.slider_volume.blockSignals(True)
        self.slider_volume.setValue(self.cast_volume)
        self.slider_volume.blockSignals(False)
        self.statusBar().showMessage(
            f"Casting to {getattr(self._cast_pending_device, 'name', 'Cast device')}",
            5000,
        )
        self.beat.setPlaying(False)

    def _on_cast_failed(self, message):
        if getattr(self, "_closing", False):
            return
        snapshot = self._cast_pending_snapshot
        self.cast_controller.disconnect()
        self.cast_media_server.shutdown()
        self.cast_active = False
        self._resume_local_snapshot(snapshot)
        self._cast_pending_snapshot = None
        self._cast_pending_device = None
        self.output_combo.setCurrentIndex(0)
        self.statusBar().showMessage(f"Cast failed: {message}", 7000)

    def _cast_play_path(self, path, index=None):
        try:
            self._playback_generation += 1
            self._playback_expected = True
            self._playback_intentionally_paused = False
            self._activate_track_ui(
                index if index is not None else self.track_index_by_path.get(path),
                path,
            )
            self.cast_media_server.revoke_all()
            media_url = self.cast_media_server.register_audio(path)
            content_type = AUDIO_TYPES[os.path.splitext(path)[1].casefold()]
            self._request_cast_load(media_url, content_type, path, 0.0, True)
            self._cast_completion_armed = False
            self.pending_next = False
            self._reset_progress()
            return True
        except (OSError, UnsupportedMediaError, RuntimeError) as ex:
            self.statusBar().showMessage(f"Could not cast track: {ex}", 7000)
            return False

    def _return_to_local_output(self):
        snapshot = self.cast_controller.snapshot()
        path = self.current_path
        was_playing = snapshot.get("state") == "playing"
        was_paused = snapshot.get("state") == "paused"
        self._cast_completion_armed = False
        self.cast_controller.stop()
        self.cast_controller.disconnect()
        self.cast_media_server.shutdown()
        self.cast_active = False
        self._cast_pending_device = None
        self.slider_volume.blockSignals(True)
        self.slider_volume.setValue(self.master_volume)
        self.slider_volume.blockSignals(False)
        if path and self._play_path_direct(path, crossfade=False):
            position = float(snapshot.get("position", 0.0) or 0.0)
            try:
                if self._use_builtin_player():
                    self.simple_player.seek(position)
                elif self.active_player:
                    self.active_player.set_time(int(position * 1000))
            except Exception:
                pass
            if was_paused or not was_playing:
                self.pause()
        self.statusBar().showMessage("Playing on this computer", 4000)

    def _connect_signals(self):
        self.btn_add.clicked.connect(self.add_folder)
        self.btn_rescan.clicked.connect(self.rescan_library)
        self.btn_play.clicked.connect(self.play_selected)
        self.btn_pause.clicked.connect(self.pause)
        self.btn_next.clicked.connect(self.next_track)
        self.btn_prev.clicked.connect(self.prev_track)
        self.slider_volume.valueChanged.connect(self.set_master_volume)
        self.output_combo.activated.connect(self._on_output_selected)
        self.cast_discovery.devices_changed.connect(self._on_cast_devices)
        self.cast_discovery.state_changed.connect(self._on_cast_state)
        self.cast_controller.state_changed.connect(self._on_cast_state)
        self.cast_controller.connected.connect(self._on_cast_connected)
        self.cast_controller.loaded.connect(self._on_cast_loaded)
        self.cast_controller.failed.connect(self._on_cast_failed)
        # Music/Videos tree signal wiring (itemDoubleClicked/itemExpanded/
        # customContextMenuRequested) now happens once per tree inside
        # _build_library_tree_widget(), covering both tabs.
        self.queue_list.itemDoubleClicked.connect(self._play_queue_item)
        self.queue_list.customContextMenuRequested.connect(self._show_queue_menu)
        self.queue_list.model().rowsMoved.connect(self._sync_queue_from_list)
        self.recent_table.itemDoubleClicked.connect(
            lambda item: self._play_recently_played_selected()
        )
        self.recent_table.customContextMenuRequested.connect(
            self._show_recently_played_menu
        )
        self.btn_save_queue.clicked.connect(self._save_queue_as_playlist)
        self.search_box.textChanged.connect(self._queue_search)
        self.slider_progress.sliderPressed.connect(self._progress_press)
        self.slider_progress.sliderReleased.connect(self._progress_release)
        self.slider_progress.valueChanged.connect(self._progress_change)
        self.chk_simple.stateChanged.connect(self._on_simple_toggled)
        # connect visualiser signals
        try:
            self.beat.visual_mode_changed.connect(self._on_visual_mode_changed)
            self.beat.latency_changed.connect(self._on_visual_latency_changed)
            self.beat.fullscreen_toggle_requested.connect(self._toggle_visualiser_fullscreen)
            self.beat.fullscreen_exit_requested.connect(self._exit_visualiser_fullscreen)
        except Exception:
            pass

    def _toggle_visualiser_fullscreen(self):
        if self._visualiser_fullscreen:
            self._exit_visualiser_fullscreen()
        else:
            self._enter_visualiser_fullscreen()

    def _enter_visualiser_fullscreen(self):
        frame = self.visualiser_frame
        if frame is None or self._visualiser_fullscreen:
            return
        # Real-device fullscreen-exit fix (2026-09-12): do NOT turn the
        # visualiser page itself into a temporary top-level window. The old
        # setParent(None) -> Window flag -> showFullScreen() path left the
        # same QWidget carrying top-level geometry/native-window state when
        # it was inserted back into right_display_stack. On Windows that
        # produced a stand-alone visualiser during exit and then a permanently
        # narrow/clipped visualiser in the normal layout. Video fullscreen
        # already has the safer model: keep the stage hierarchy fixed and
        # fullscreen the main window around it. Reuse that presentation seam.
        stack = getattr(self, "right_display_stack", None)
        if stack is not None and stack.indexOf(frame) >= 0:
            stack.setCurrentWidget(frame)
        self._remember_right_splitter_state()
        restore_state = self._enter_main_video_fullscreen_presentation()
        if restore_state is None:
            return
        self._visualiser_fullscreen_snapshot = {
            "presentation": restore_state,
        }
        self._visualiser_fullscreen = True
        self.beat.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)

    def _exit_visualiser_fullscreen(self):
        frame = self.visualiser_frame
        if frame is None or not self._visualiser_fullscreen:
            return
        snapshot = getattr(self, "_visualiser_fullscreen_snapshot", None) or {}
        self._visualiser_fullscreen = False
        # Consume the session snapshot before restoring so a re-entrant or
        # repeated exit can never reuse stale fullscreen state.
        self._visualiser_fullscreen_snapshot = None
        restore_state = snapshot.get("presentation")
        if restore_state is not None:
            self._exit_main_video_fullscreen_presentation(restore_state)
        elif self.isFullScreen():
            # Defensive fallback for a partially-restored/legacy session.
            # Normal sessions always have the presentation snapshot above.
            self.showNormal()
        # The visualiser never left right_display_stack, so there is no
        # reparenting, Window-flag flip, stand-alone showNormal(), or geometry
        # hand-off to repair here. Reconcile only the display page in case
        # the media type changed while fullscreen was active.
        self._reconcile_display_stage_with_current_media()

    def _reconcile_display_stage_with_current_media(self) -> None:
        """Presentation-state correction after any fullscreen restoration.

        Never starts or stops playback -- it only makes the visible
        display stage agree with what is ACTUALLY playing now, so a video
        page (including the "Loading video..." placeholder) can never be
        left showing for an audio track just because some earlier session
        had video up. Video/karaoke keep whatever page their own existing
        rules put there."""
        stack = getattr(self, "right_display_stack", None)
        if stack is None:
            return
        if getattr(self, "_current_media_type", MediaType.AUDIO) != MediaType.AUDIO:
            return
        normal_page = getattr(self, "_normal_display_page", None)
        if normal_page is None or stack.currentWidget() is normal_page:
            return
        try:
            self._show_normal_display_page()
        except Exception:
            pass

    def _load_user_settings(self):
        try:
            cfg = load_config()
        except Exception:
            cfg = {}
        mode = cfg.get("visual_mode", "blocky")
        try:
            self.beat.set_visual_mode(mode)
        except Exception:
            pass
        try:
            self.analyzer_time_offset_ms = int(cfg.get("analyzer_time_offset_ms", 0))
        except Exception:
            self.analyzer_time_offset_ms = 0
        try:
            self.lyric_time_offset_ms = int(cfg.get("lyric_time_offset_ms", 0))
        except Exception:
            self.lyric_time_offset_ms = 0
        self.lyrics_enabled = bool(cfg.get("lyrics_enabled", True))
        self.action_toggle_lyrics.setChecked(self.lyrics_enabled)
        self.lyric_visual_style = cfg.get("lyric_visual_style", "neon")
        self.bio_detail_mode = cfg.get("bio_detail_mode", "detailed") if cfg.get("bio_detail_mode", "detailed") in ("concise", "detailed", "facts") else "detailed"
        self.recent_played = [p for p in cfg.get("recent_played", []) if isinstance(p, str)][:30]
        self.library_font_family = str(cfg.get("library_font_family", "Segoe UI") or "Segoe UI")
        self._apply_library_font(self.library_font_family)
        self.normalisation_enabled = bool(cfg.get("normalisation_enabled", False))
        self.normalisation_mode = cfg.get("normalisation_mode", "track")
        if self.normalisation_mode not in ("track", "album"):
            self.normalisation_mode = "track"
        self.prevent_clipping = bool(cfg.get("normalisation_prevent_clipping", True))
        self.auto_loudness_analysis = bool(cfg.get("normalisation_auto_analyse", False))
        self.target_lufs = max(-23.0, min(-9.0, float(cfg.get("normalisation_target_lufs", -14.0))))
        self.tagged_preamp_db = max(-12.0, min(12.0, float(cfg.get("normalisation_tagged_preamp_db", 0.0))))
        self.untagged_preamp_db = max(-12.0, min(12.0, float(cfg.get("normalisation_untagged_preamp_db", 0.0))))
        self.auto_playback_recovery = bool(
            cfg.get("auto_playback_recovery", True)
        )
        self.allow_backend_fallback = bool(
            cfg.get("allow_backend_fallback", True)
        )
        self.warn_before_adding_duplicate_queue_tracks = bool(
            cfg.get("warn_before_adding_duplicate_queue_tracks", True)
        )
        self.track_transition_mode = cfg.get("track_transition_mode", "crossfade")
        if self.track_transition_mode not in TRACK_TRANSITION_MODES:
            self.track_transition_mode = "crossfade"
        try:
            self.crossfade_seconds = max(
                CROSSFADE_SECONDS_MIN,
                min(CROSSFADE_SECONDS_MAX, float(cfg.get("crossfade_seconds", CROSSFADE_SECONDS))),
            )
        except Exception:
            self.crossfade_seconds = CROSSFADE_SECONDS
        self.video_playback_enabled = bool(cfg.get("video_playback_enabled", True))
        self.video_start_fullscreen = bool(cfg.get("video_start_fullscreen", False))
        self.video_return_to_normal_display_on_end = bool(
            cfg.get("video_return_to_normal_display_on_end", True)
        )
        transition_preferences = VideoTransitionPreferences.from_config(cfg)
        self.video_transitions_enabled = transition_preferences.enabled
        self.video_transition_style = transition_preferences.style
        self.video_transition_duration_seconds = (
            transition_preferences.duration_seconds
        )
        self.video_transition_automatic_lead_seconds = (
            transition_preferences.automatic_lead_seconds
        )
        self.video_transition_manual_duration_seconds = (
            transition_preferences.manual_duration_seconds
        )
        self.video_transition_enabled_effects = dict(
            transition_preferences.enabled_effects
        )
        transition_manager = getattr(self, "_video_transition_manager", None)
        if transition_manager is not None:
            transition_manager.configure(transition_preferences)
        dual_transition_preferences = DualTransitionPreferences.from_config(cfg)
        if not DUAL_VIDEO_TRANSITIONS_AVAILABLE:
            # Force-disabled regardless of what an old/edited config.json
            # has stored -- see DUAL_VIDEO_TRANSITIONS_AVAILABLE above.
            dual_transition_preferences = DualTransitionPreferences(enabled=False)
        self.video_dual_transitions_enabled = dual_transition_preferences.enabled
        self.video_gpu_transition_effect = dual_transition_preferences.gpu_effect
        self.video_smart_transition_points_enabled = bool(
            cfg.get("video_smart_transition_points_enabled", False)
        )
        self.video_avoid_black_outros = dual_transition_preferences.avoid_black_outros
        self.video_skip_black_intros = dual_transition_preferences.skip_black_intros
        self.video_crossfade_audio_enabled = dual_transition_preferences.crossfade_video_audio_enabled
        self.video_crossfade_audio_curve = dual_transition_preferences.audio_crossfade_curve
        self.video_dual_preload_lead_seconds = dual_transition_preferences.preload_lead_seconds
        self.video_dual_ready_timeout_ms = dual_transition_preferences.ready_timeout_ms
        self.video_dual_preload_max_wait_ms = dual_transition_preferences.preload_max_wait_ms
        self.video_dual_preload_progress_extension_ms = (
            dual_transition_preferences.preload_progress_extension_ms
        )
        # The GPU capability probe (see _start_gpu_capability_probe) has
        # not necessarily resolved yet at this point in startup --
        # _apply_gpu_dual_mode_state() correctly resolves to classic mode
        # until self._gpu_dual_capability is actually True, then gets
        # called again once the probe completes.
        self._apply_gpu_dual_mode_state()
        geometry = cfg.get("mini_player_geometry")
        if isinstance(geometry, dict):
            self.mini_player_geometry = geometry
        self.mini_player_always_on_top = bool(
            cfg.get("mini_player_always_on_top", False)
        )
        self.party_mode_screen_name = str(cfg.get("party_mode_screen_name", "") or "")
        self.party_mode_default_layout = cfg.get("party_mode_default_layout", "lyrics")
        if self.party_mode_default_layout not in PARTY_MODE_LAYOUTS:
            self.party_mode_default_layout = "lyrics"
        self.party_mode_show_up_next = bool(cfg.get("party_mode_show_up_next", True))
        try:
            self.party_mode_up_next_count = max(
                PARTY_MODE_UP_NEXT_COUNT_MIN,
                min(PARTY_MODE_UP_NEXT_COUNT_MAX, int(cfg.get("party_mode_up_next_count", 3))),
            )
        except Exception:
            self.party_mode_up_next_count = 3
        self.party_mode_show_clock = bool(cfg.get("party_mode_show_clock", True))
        self.party_mode_show_remaining_playlist_time = bool(
            cfg.get("party_mode_show_remaining_playlist_time", False)
        )
        try:
            self.party_mode_auto_hide_ms = max(
                PARTY_MODE_AUTO_HIDE_SECONDS_MIN * 1000,
                min(
                    PARTY_MODE_AUTO_HIDE_SECONDS_MAX * 1000,
                    int(cfg.get("party_mode_auto_hide_ms", 3000)),
                ),
            )
        except Exception:
            self.party_mode_auto_hide_ms = 3000
        self.party_mode_animations_enabled = bool(
            cfg.get("party_mode_animations_enabled", True)
        )
        self.party_mode_visual_quality = cfg.get("party_mode_visual_quality", "medium")
        if self.party_mode_visual_quality not in PARTY_MODE_VISUAL_QUALITIES:
            self.party_mode_visual_quality = "medium"
        self.diagnostics_level = str(
            os.environ.get(
                "BILLSMUSIC_DIAGNOSTICS_LEVEL",
                cfg.get("diagnostics_level", "basic"),
            )
        ).lower()
        if self.diagnostics_level not in ("off", "basic", "detailed", "developer"):
            self.diagnostics_level = "basic"
        self.diagnostics_include_full_paths = bool(
            cfg.get("diagnostics_include_full_paths", False)
        )
        self.diagnostics.configure(
            self.diagnostics_level,
            self.diagnostics_include_full_paths,
        )
        self.top_menu_visible = bool(cfg.get("top_menu_visible", False))
        with QtCore.QSignalBlocker(self.action_show_top_menu):
            self.action_show_top_menu.setChecked(self.top_menu_visible)
        self.menuBar().setVisible(self.top_menu_visible)
        self.sleep_timer_fade_pref = bool(
            cfg.get("sleep_timer_fade_before_stopping", False)
        )
        if hasattr(self, "action_sleep_timer_fade"):
            with QtCore.QSignalBlocker(self.action_sleep_timer_fade):
                self.action_sleep_timer_fade.setChecked(self.sleep_timer_fade_pref)
        splitter_state = cfg.get("right_splitter_state")
        if isinstance(splitter_state, str) and splitter_state:
            try:
                state = QtCore.QByteArray.fromBase64(
                    splitter_state.encode("ascii")
                )
                if not state.isEmpty():
                    self._visualiser_splitter_state = state
                    self._restore_right_splitter_position()
                    QtCore.QTimer.singleShot(
                        0, self._restore_right_splitter_position
                    )
            except Exception:
                self._visualiser_splitter_state = None
        if self.lyric_visual_style not in LYRIC_STYLES:
            self.lyric_visual_style = "neon"
        try:
            self.overlay.set_lyric_style(self.lyric_visual_style)
        except Exception:
            pass
        # built-in audio backend (BASS is the default; falls back to miniaudio
        # if the BASS DLL isn't available on this machine)
        backend, use_builtin = resolve_audio_backend_preference(cfg)
        if not self._set_builtin_backend(backend):
            if not self._set_builtin_backend("bass"):
                self._set_builtin_backend("miniaudio")
        self.use_simple = bool(use_builtin) and self.simple_player is not None
        try:
            self.chk_simple.setChecked(self.use_simple and self.builtin_backend == "miniaudio")
        except Exception:
            pass
        self.plex_preferences = PlexPreferences.from_config(cfg)
        if not self.plex_preferences.client_identifier:
            # Generated once, ever, per install -- persisted immediately
            # (a narrow read-modify-write, mirroring
            # _mark_backfill_version_complete's pattern) rather than
            # waiting for the user to ever open Preferences, so it's
            # stable from the very first Plex request onward.
            self.plex_preferences = dataclasses.replace(
                self.plex_preferences,
                client_identifier=generate_client_identifier(),
            )
            try:
                fresh_cfg = load_config() or {}
                fresh_cfg["plex_client_identifier"] = self.plex_preferences.client_identifier
                with open(config_file_path(), "w", encoding="utf-8") as f:
                    json.dump(fresh_cfg, f, indent=2)
            except Exception:
                pass
        self.library_source = cfg.get("library_source", "local")
        if self.library_source not in ("local", "plex"):
            self.library_source = "local"
        # Runtime-only (never persisted) Plex connection state -- re-
        # resolved every session by _start_plex_startup_validation();
        # persisting a resolved URI/token would violate "never make the
        # connection URI itself the permanent identity".
        self._plex_status = "unknown"  # "unknown" | "online" | "offline"
        self._plex_active_connection_uri = ""
        self._plex_active_access_token = ""
        # Stage 2 browsing state -- also runtime-only. _plex_fetch_generation
        # guards against a stale PlexLibraryFetchWorker result (source/
        # server/mapping changed while a fetch was in flight) overwriting
        # newer state -- see _on_plex_library_fetch_result.
        self._plex_fetch_generation = 0
        self._plex_refresh_in_flight = set()  # media_kinds currently fetching
        self._plex_meta_by_kind = {"music": [], "video": [], "karaoke": []}
        # Real-device bug: _meta_by_path (built from _full_meta_list, which
        # is source-aware -- see the Local-regression fix) gets silently
        # replaced by Local library data the moment the user browses the
        # Local tab while a Plex track keeps playing, so a later now-
        # playing refresh for that same still-current Plex track found no
        # title/artist there and fell back to the numeric ratingKey
        # (os.path.basename of a plex:// identity). This is deliberately
        # a *separate*, fetch-driven cache -- populated once per Plex
        # fetch (see _on_plex_library_fetch_result), never touched by
        # library-source/tab switching -- consulted as a fallback by
        # _load_cached_audio_tags/_load_cached_video_tags before ever
        # reaching a basename.
        self._plex_meta_by_path = {}
        self._plex_item_updated_at = {}  # path -> Plex updatedAt, for artwork refetch avoidance
        # media_kinds that have received at least one successful result
        # since their last (re)fetch began -- distinguishes "genuinely
        # empty" from "not fetched yet" for _finish_plex_tab_apply's
        # loading/empty/error tab-status decision.
        self._plex_fetched_kinds = set()
        self._plex_fetch_error = {}  # media_kind -> last short, safe failure reason
        self._plex_active_workers_by_kind = {}  # media_kind -> in-flight PlexLibraryFetchWorker
        if hasattr(self, "library_source_selector"):
            with QtCore.QSignalBlocker(self.library_source_selector):
                idx = self.library_source_selector.findData(self.library_source)
                self.library_source_selector.setCurrentIndex(idx if idx >= 0 else 0)
            self.library_tabs.setVisible(self.library_source != "plex")
            self.library_plex_placeholder.setVisible(self.library_source == "plex")

    def _save_user_settings(self):
        diagnostic_started = time.perf_counter()
        try:
            cfg = load_config() or {}
            cfg["visual_mode"] = getattr(self.beat, "visual_mode", "bars")
            cfg["analyzer_time_offset_ms"] = int(getattr(self, "analyzer_time_offset_ms", 0))
            cfg["lyric_time_offset_ms"] = int(getattr(self, "lyric_time_offset_ms", 0))
            cfg["lyrics_enabled"] = bool(getattr(self, "lyrics_enabled", True))
            cfg["lyric_visual_style"] = getattr(self, "lyric_visual_style", "neon")
            cfg["bio_detail_mode"] = getattr(self, "bio_detail_mode", "detailed")
            cfg["recent_played"] = list(getattr(self, "recent_played", []))[:30]
            cfg["library_font_family"] = getattr(self, "library_font_family", "Segoe UI")
            cfg["use_simple_player"] = bool(self.use_simple)
            cfg["audio_backend"] = getattr(self, "builtin_backend", "bass")
            cfg["audio_backend_preference_version"] = AUDIO_BACKEND_PREFERENCE_VERSION
            cfg["normalisation_enabled"] = bool(self.normalisation_enabled)
            cfg["normalisation_mode"] = self.normalisation_mode
            cfg["normalisation_prevent_clipping"] = bool(self.prevent_clipping)
            cfg["normalisation_auto_analyse"] = bool(self.auto_loudness_analysis)
            cfg["normalisation_target_lufs"] = float(self.target_lufs)
            cfg["normalisation_tagged_preamp_db"] = float(self.tagged_preamp_db)
            cfg["normalisation_untagged_preamp_db"] = float(self.untagged_preamp_db)
            cfg["auto_playback_recovery"] = bool(
                self.auto_playback_recovery
            )
            cfg["allow_backend_fallback"] = bool(
                self.allow_backend_fallback
            )
            cfg["warn_before_adding_duplicate_queue_tracks"] = bool(
                getattr(self, "warn_before_adding_duplicate_queue_tracks", True)
            )
            cfg["track_transition_mode"] = getattr(
                self, "track_transition_mode", "crossfade"
            )
            cfg["crossfade_seconds"] = float(
                getattr(self, "crossfade_seconds", CROSSFADE_SECONDS)
            )
            cfg["video_playback_enabled"] = bool(
                getattr(self, "video_playback_enabled", True)
            )
            cfg["video_start_fullscreen"] = bool(
                getattr(self, "video_start_fullscreen", False)
            )
            cfg["video_return_to_normal_display_on_end"] = bool(
                getattr(self, "video_return_to_normal_display_on_end", True)
            )
            cfg["video_transitions_enabled"] = bool(
                getattr(self, "video_transitions_enabled", True)
            )
            cfg["video_transition_style"] = getattr(
                self, "video_transition_style", "Random Smooth"
            )
            cfg["video_transition_duration_seconds"] = float(
                getattr(self, "video_transition_duration_seconds", 1.0)
            )
            cfg["video_transition_automatic_lead_seconds"] = float(
                getattr(self, "video_transition_automatic_lead_seconds", 1.0)
            )
            cfg["video_transition_manual_duration_seconds"] = float(
                getattr(self, "video_transition_manual_duration_seconds", 0.5)
            )
            cfg["video_transition_enabled_effects"] = dict(
                getattr(
                    self, "video_transition_enabled_effects",
                    {effect: True for effect in VIDEO_TRANSITION_EFFECTS},
                )
            )
            cfg["video_dual_transitions_enabled"] = bool(
                getattr(self, "video_dual_transitions_enabled", False)
            )
            cfg["video_gpu_transition_effect"] = getattr(
                self, "video_gpu_transition_effect", "Cross Dissolve"
            )
            cfg["video_smart_transition_points_enabled"] = bool(
                getattr(self, "video_smart_transition_points_enabled", False)
            )
            cfg["video_avoid_black_outros"] = bool(
                getattr(self, "video_avoid_black_outros", False)
            )
            cfg["video_skip_black_intros"] = bool(
                getattr(self, "video_skip_black_intros", False)
            )
            cfg["video_crossfade_audio_enabled"] = bool(
                getattr(self, "video_crossfade_audio_enabled", False)
            )
            cfg["video_crossfade_audio_curve"] = getattr(
                self, "video_crossfade_audio_curve", "Equal Power"
            )
            cfg["waveform_seekbar_enabled"] = bool(
                self.waveform_seekbar_enabled
            )
            cfg["mini_player_geometry"] = dict(self.mini_player_geometry)
            cfg["mini_player_always_on_top"] = bool(
                self.mini_player_always_on_top
            )
            cfg["party_mode_screen_name"] = getattr(self, "party_mode_screen_name", "")
            cfg["party_mode_default_layout"] = getattr(
                self, "party_mode_default_layout", "lyrics"
            )
            cfg["party_mode_show_up_next"] = bool(
                getattr(self, "party_mode_show_up_next", True)
            )
            cfg["party_mode_up_next_count"] = int(
                getattr(self, "party_mode_up_next_count", 3)
            )
            cfg["party_mode_show_clock"] = bool(
                getattr(self, "party_mode_show_clock", True)
            )
            cfg["party_mode_show_remaining_playlist_time"] = bool(
                getattr(self, "party_mode_show_remaining_playlist_time", False)
            )
            cfg["party_mode_auto_hide_ms"] = int(
                getattr(self, "party_mode_auto_hide_ms", 3000)
            )
            cfg["party_mode_animations_enabled"] = bool(
                getattr(self, "party_mode_animations_enabled", True)
            )
            cfg["party_mode_visual_quality"] = getattr(
                self, "party_mode_visual_quality", "medium"
            )
            cfg["top_menu_visible"] = bool(
                self.action_show_top_menu.isChecked()
            )
            cfg["sleep_timer_fade_before_stopping"] = bool(
                getattr(self, "sleep_timer_fade_pref", False)
            )
            cfg["diagnostics_level"] = getattr(
                self, "diagnostics_level", "basic"
            )
            cfg["diagnostics_include_full_paths"] = bool(
                getattr(self, "diagnostics_include_full_paths", False)
            )
            splitter_state = getattr(
                self, "_visualiser_splitter_state", None
            )
            if (
                not splitter_state
                and getattr(self, "right_splitter", None) is not None
            ):
                splitter_state = self.right_splitter.saveState()
            if splitter_state:
                cfg["right_splitter_state"] = bytes(
                    splitter_state.toBase64()
                ).decode("ascii")
            cfg.update(getattr(
                self, "plex_preferences", PlexPreferences(),
            ).to_config_updates())
            cfg["library_source"] = getattr(self, "library_source", "local")
            with open(config_file_path(), "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            duration_ms = (
                time.perf_counter() - diagnostic_started
            ) * 1000.0
            self.diagnostics.record(
                "configuration", "save_preferences",
                duration_ms=duration_ms,
                severity="warning" if duration_ms >= 30 else "info",
                minimum_level="basic" if duration_ms >= 30 else "detailed",
            )
        except Exception as ex:
            self.diagnostics.record(
                "configuration", "save_preferences", status="failure",
                severity="warning",
                duration_ms=(
                    time.perf_counter() - diagnostic_started
                ) * 1000.0,
                details={"exception": str(ex)},
                minimum_level="basic",
            )

    def _on_visual_mode_changed(self, mode: str):
        try:
            # ensure widget updated and save
            self.beat.visual_mode = mode
            self.beat.update()
        except Exception:
            pass
        self._save_user_settings()

    def _on_simple_toggled(self, state: int):
        checked_value = int(getattr(QtCore.Qt.CheckState.Checked, "value", QtCore.Qt.CheckState.Checked))
        self._set_builtin_backend("miniaudio")
        self.use_simple = int(state) == checked_value
        self._simple_fallback_active = False
        self._audio_log(f"backend={self._backend_label().lower()} enabled={self.use_simple}")
        self._update_dj_info(path=self.current_path)
        self._save_user_settings()
        # If currently playing, restart the same track on the newly selected backend.
        if self.current_path:
            self.play_path(self.current_path, crossfade=False)
            self.beat.setPlaying(True)
            self.btn_pause.setText("Pause")

    def _on_visual_latency_changed(self, ms: int):
        try:
            self.analyzer_time_offset_ms = int(ms)
        except Exception:
            self.analyzer_time_offset_ms = 0

    def _file_signature(self, path: str) -> Dict[str, Any]:
        if is_plex_identity(path):
            # Plex freshness signature: the item's own server-provided
            # updatedAt, not a filesystem stat this identity has none of
            # -- a Plex item whose metadata genuinely changed (mtime
            # bumped by Plex itself) invalidates only its own cached
            # entry; an unchanged item's cache is reused across restarts
            # without re-fetching. See item 10 -- deliberately NOT
            # redesigning the local os.stat-based signature system this
            # shares a return shape with, just substituting the source of
            # "mtime" for this one identity class.
            updated_at = getattr(self, "_plex_item_updated_at", {}).get(path, 0)
            return {"mtime": int(updated_at or 0), "size": 0}
        try:
            stat = os.stat(path)
            return {"mtime": int(stat.st_mtime), "size": int(stat.st_size)}
        except Exception:
            return {"mtime": 0, "size": 0}

    def _load_queue_analysis_cache(self):
        try:
            with open(queue_analysis_cache_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            self.queue_analysis_cache = data if isinstance(data, dict) else {}
        except Exception:
            self.queue_analysis_cache = {}

    def _save_queue_analysis_cache(self):
        try:
            with open(queue_analysis_cache_path(), "w", encoding="utf-8") as f:
                json.dump(self.queue_analysis_cache, f, indent=2)
        except Exception:
            pass

    def _cached_queue_analysis(
        self, path: str, validate_signature: bool = True
    ) -> Dict[str, str]:
        entry = self.queue_analysis_cache.get(path)
        if not isinstance(entry, dict):
            return {}
        if validate_signature:
            sig = self._file_signature(path)
            if (
                entry.get("mtime") != sig.get("mtime")
                or entry.get("size") != sig.get("size")
            ):
                return {}
        result = entry.get("result") or {}
        return result if isinstance(result, dict) else {}

    def _store_queue_analysis(
        self, path: str, result: Dict[str, str],
        signature: Optional[Dict[str, int]] = None,
    ):
        if not path or not result:
            return
        sig = signature if signature is not None else self._file_signature(path)
        self.queue_analysis_cache[path] = {"mtime": sig.get("mtime", 0), "size": sig.get("size", 0), "result": result}
        if len(self.queue_analysis_cache) > 2000:
            for key in list(self.queue_analysis_cache.keys())[:250]:
                self.queue_analysis_cache.pop(key, None)
        self._schedule_queue_analysis_cache_save()

    def _set_builtin_backend(self, backend: str) -> bool:
        backend = backend if backend in ("miniaudio", "bass") else "miniaudio"
        if backend == "bass":
            if not self.bass_player or not self.bass_inactive_player:
                return False
            self._set_player_topology(self.bass_player, self.bass_inactive_player, reason="backend_switch")
        else:
            if not self.miniaudio_player or not self.miniaudio_inactive_player:
                return False
            self._set_player_topology(self.miniaudio_player, self.miniaudio_inactive_player, reason="backend_switch")
        self.builtin_backend = backend
        return True

    def _backend_label(self) -> str:
        if self._temporary_backend_override:
            return self._temporary_backend_override.upper() if self._temporary_backend_override == "vlc" else self._temporary_backend_override
        if not self._use_builtin_player():
            return "VLC"
        return "BASS" if getattr(self, "builtin_backend", "miniaudio") == "bass" else "miniaudio"

    def _use_bass_backend(self) -> bool:
        backend = self._temporary_backend_override or getattr(
            self, "builtin_backend", "miniaudio"
        )
        return self._use_builtin_player() and backend == "bass"

    def _set_dj_info(self, text: str):
        self._dj_info_base_text = text
        self._refresh_dj_info_display()

    def _refresh_dj_info_display(self):
        text = self._dj_info_base_text
        percent = self._backfill_scan_percent
        if percent is not None:
            text = f"{text}  |  Scanning {percent}%" if text else f"Scanning {percent}%"
        try:
            self.dj_info.setText(text)
        except Exception:
            pass

    def _update_dj_info(self, info=None, path: Optional[str] = None):
        backend = self._backend_label()
        if info is None and path:
            try:
                info = self._read_tags(path)
            except Exception:
                info = None
        parts = [f"Backend: {backend}"]
        details = {}
        if path:
            # Cached-only detail lookup keeps the activation path free of
            # Mutagen/network I/O while still allowing the header to show
            # bitrate/time that completed queue enrichment already knows.
            details = self.queue_detail_cache.get(path) or self._queue_track_details(
                path, cached_details_only=True
            )
        if info:
            if getattr(info, "duration", "Unknown") not in ("", "Unknown"):
                parts.append(f"Time: {info.duration}")
            elif details.get("time") and details["time"] != "--":
                parts.append(f"Time: {details['time']}")
            if getattr(info, "bitrate", "Unknown") not in ("", "Unknown"):
                parts.append(f"Rate: {info.bitrate}")
            elif details.get("bitrate") and details["bitrate"] != "--":
                parts.append(f"Rate: {details['bitrate']}")
        if path:
            # cached_details_only=True: this runs on every track/video
            # activation (_activate_track_ui), on the GUI thread. A brand
            # new, not-yet-analysed track on a slow network share would
            # otherwise block here on a synchronous MutagenFile() call --
            # confirmed via a captured GUI-stall trace showing exactly
            # that (_play_video_path_direct -> _activate_track_ui ->
            # _update_dj_info -> _queue_track_details -> mutagen.File()).
            # Key/BPM just stay "--" until the background analysis
            # pipeline fills queue_detail_cache in, matching every other
            # call site of _queue_track_details already documented as
            # "must never touch slow/network paths".
            if details.get("key") and details.get("key") != "--":
                parts.append(f"Key: {details['key']}")
            if details.get("bpm") and details.get("bpm") != "--":
                parts.append(f"BPM: {details['bpm']}")
        self._set_dj_info("  |  ".join(parts))

    def _record_recent_played(self, path: str):
        if not path:
            return
        self.recent_played = [p for p in self.recent_played if p != path]
        self.recent_played.insert(0, path)
        self.recent_played = self.recent_played[:30]
        self._save_user_settings()

    def _load_cache(self):
        started = time.perf_counter()
        cache_path = cache_file_path()
        if not os.path.exists(cache_path):
            self.diagnostics.record(
                "file_io", "load_library_cache",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                details={"cache_present": False},
                minimum_level="detailed",
            )
            return None
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                result = json.load(f)
            if isinstance(result, dict) and isinstance(result.get("meta"), list):
                result["meta"] = migrate_cache_meta_list(result["meta"])
            self.diagnostics.record(
                "file_io", "load_library_cache",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                details={
                    "cache_present": True,
                    "records": len(result.get("meta", []))
                    if isinstance(result, dict) else 0,
                },
                minimum_level="detailed",
            )
            return result
        except Exception as ex:
            self.diagnostics.record(
                "file_io", "load_library_cache", status="failure",
                severity="warning",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                details={"exception": str(ex)},
                minimum_level="basic",
            )
            return None

    _PLEX_LIBRARY_CACHE_SCHEMA_VERSION = 1

    def _load_plex_library_cache(self) -> Optional[Dict[str, Any]]:
        """Stage 3A: {"server_config_id", "meta_by_kind": {"music": [...],
        "video": [...], "karaoke": [...]}} persisted by
        _save_plex_library_cache, or None if absent/unreadable/for a
        different server (a stale cache from a since-changed Plex server
        must never be applied as if it were current -- see
        _startup_restore_library's caller)."""
        path = plex_library_cache_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("meta_by_kind"), dict):
            return None
        return data

    # Default freshness window: a cache younger than this is used as-is,
    # with no automatic background refetch at all -- only "Refresh Plex"
    # (always unconditional) or a genuinely stale cache triggers one.
    # 24h: a personal media library's *contents* rarely change more than
    # once a day, and this is what actually eliminates the real-device
    # complaint (a full library refetch, every single launch, all day) --
    # a shorter interval would still refetch on most same-day relaunches.
    PLEX_LIBRARY_CACHE_FRESHNESS_SECONDS = 24 * 60 * 60

    def _restore_plex_library_cache_at_startup(self):
        """Stage 3A: real-device bug -- the Plex Music/Video/Karaoke
        libraries were fetched fresh from the Plex server on every single
        launch, even though Stage 2 already established stable Plex
        identities and metadata caching; a tab showed "Loading..." (or
        nothing) until that fetch completed, no matter how good the
        previous session's data still was. Restoring the persisted cache
        here (before any Plex network activity -- _start_plex_startup_
        validation only fires 1500ms after startup_ready) makes cached
        data available immediately.

        Freshness policy: a cache younger than
        PLEX_LIBRARY_CACHE_FRESHNESS_SECONDS is used with NO automatic
        background refetch -- self._plex_fetched_kinds already gates
        _show_plex_library_view's own "still need to fetch this kind"
        check, so simply marking a fresh kind fetched is what suppresses
        it. A STALE kind is still shown immediately from cache, but is
        also recorded in self._plex_startup_stale_kinds so
        _on_plex_startup_connection_result can kick off exactly one
        background refresh for it once actually online (never here --
        no connection has been resolved yet at this point in startup).
        "Refresh Plex" (_refresh_plex_library) is completely unconditional
        and untouched by any of this.

        Never applied across a server change (server_config_id mismatch)
        or a changed library mapping (library_ids_by_kind mismatch, per
        kind) -- either discards that kind's cached data entirely rather
        than risk showing/refreshing the wrong library's content; a
        changed kind is left exactly as "never fetched" so the normal
        _show_plex_library_view path fetches it immediately once viewed,
        same as decision 3's "if the user has changed server/library
        mappings, refresh immediately" requirement."""
        self._plex_startup_stale_kinds = set()
        cache = self._load_plex_library_cache()
        if not cache:
            return
        if cache.get("server_config_id") != self.plex_preferences.server_config_id:
            return  # cache from a since-changed server -- discard entirely
        meta_by_kind = cache.get("meta_by_kind") or {}
        cached_library_ids = cache.get("library_ids_by_kind") or {}
        current_library_ids = {
            "music": self.plex_preferences.music_library_id,
            "video": self.plex_preferences.video_library_id,
            "karaoke": self.plex_preferences.karaoke_library_id,
        }
        last_refresh = cache.get("last_successful_refresh_utc")
        try:
            cache_age_seconds = time.time() - float(last_refresh) if last_refresh else None
        except (TypeError, ValueError):
            cache_age_seconds = None
        is_stale = (
            cache_age_seconds is None
            or cache_age_seconds >= self.PLEX_LIBRARY_CACHE_FRESHNESS_SECONDS
        )
        for kind in ("music", "video", "karaoke"):
            if cached_library_ids.get(kind) != current_library_ids.get(kind):
                continue  # mapping changed since this was fetched -- discard this kind
            meta_list = meta_by_kind.get(kind)
            if not (isinstance(meta_list, list) and meta_list):
                continue
            self._plex_meta_by_kind[kind] = meta_list
            self._plex_fetched_kinds.add(kind)
            if is_stale:
                self._plex_startup_stale_kinds.add(kind)
        for meta in (
            list(self._plex_meta_by_kind.get("music", []))
            + list(self._plex_meta_by_kind.get("video", []))
            + list(self._plex_meta_by_kind.get("karaoke", []))
        ):
            path = meta.get("path")
            if not path:
                continue
            self._plex_item_updated_at[path] = meta.get("updated_at", 0)
            self._plex_meta_by_path[path] = meta
        self.diagnostics.record(
            "plex", "plex_library_cache_restored",
            details={
                "music_records": len(self._plex_meta_by_kind.get("music", [])),
                "video_records": len(self._plex_meta_by_kind.get("video", [])),
                "karaoke_records": len(self._plex_meta_by_kind.get("karaoke", [])),
                "cache_age_seconds": round(cache_age_seconds) if cache_age_seconds is not None else None,
                "stale_kinds": sorted(self._plex_startup_stale_kinds),
            },
            minimum_level="basic",
        )
        if self.library_source == "plex" and self._plex_fetched_kinds:
            combined = (
                list(self._plex_meta_by_kind.get("music", []))
                + list(self._plex_meta_by_kind.get("video", []))
                + list(self._plex_meta_by_kind.get("karaoke", []))
            )
            self._apply_meta_list_to_library_tabs(
                combined, reason="startup_plex_cache_restore", trigger_source="plex_fetch",
            )
            # Real-device bug (Stage 3A-r2): _load_user_settings's own
            # combo-sync (self.library_source_selector's initial index)
            # runs much earlier, before any Plex data exists yet, and
            # blindly hides library_tabs / shows library_plex_placeholder
            # for library_source == "plex" regardless of whether a cache
            # was about to be restored -- neither
            # _apply_meta_list_to_library_tabs above nor anything else in
            # this method ever re-asserts visibility once real data
            # actually arrives, so the tree stayed built but invisible
            # behind the placeholder until the user manually touched the
            # source selector (which routes through _show_plex_library_view,
            # the only other place that gets this right). Now that a real
            # cache has just been restored and applied above, correct it
            # here -- mirroring _show_plex_library_view's own success path.
            if hasattr(self, "library_tabs"):
                self.library_tabs.setVisible(True)
                self.library_plex_placeholder.setVisible(False)

    def _maybe_rehydrate_plex_queue_after_cache_restore(self):
        """Real-device bug (Stage 3A-r2): the Up Next list has already
        been rebuilt once by this point, via _load_session (an earlier
        startup stage, _startup_restore_session) calling its own
        _refresh_queue_list(cached_details_only=True, reason=
        "startup_restore") -- but that ran BEFORE
        _restore_plex_library_cache_at_startup (just above) populated
        self._plex_meta_by_path, so any restored Plex queue row was built
        with no metadata available at all and fell back to the raw
        numeric ratingKey. session.json only ever persists {path, played}
        (see save_session_file) -- there was never anything richer to
        restore from directly. Now that _plex_meta_by_path is populated,
        rebuild those rows once more so they pick up the real
        titles/artists without requiring the user to touch anything."""
        if any(is_plex_identity(p) for p in self.queue):
            self._refresh_queue_list(
                cached_details_only=True, reason="plex_cache_restored",
            )

    def _maybe_start_stale_plex_startup_refresh(self):
        """Called once Plex connection resolution actually succeeds
        (_on_plex_startup_connection_result) -- refreshing a stale cache
        needs a real, working connection, which isn't resolved yet at
        _restore_plex_library_cache_at_startup's point in the startup
        sequence. A no-op if nothing was marked stale (the common case
        once a cache exists and is still fresh) or if the user has since
        navigated away in a way that invalidates this (checked the same
        way _start_if_still_relevant already does for the ordinary
        prioritized-fetch path)."""
        stale = getattr(self, "_plex_startup_stale_kinds", None)
        if not stale or getattr(self, "_closing", False):
            return
        self._plex_startup_stale_kinds = set()
        self._start_plex_library_fetches_prioritized(sorted(stale))

    def _save_plex_library_cache(self):
        """Persists exactly the same Stage-2 meta dicts already held in
        memory (self._plex_meta_by_kind) -- these never contain a
        transport URL or token (see plex_metadata.py's conversion
        functions: only the stable plex:// identity, rating_key, thumb
        artwork identity, and display/media-part fields). Called after
        every successful Plex fetch (_on_plex_library_fetch_result) so
        the next startup has real data to restore immediately, instead of
        showing an empty/loading library until a fresh fetch completes.

        last_successful_refresh_utc and library_ids_by_kind are what
        _restore_plex_library_cache_at_startup uses to decide freshness
        and to detect a changed library mapping -- deliberately NOT the
        cache file's own mtime, which would be indistinguishable from
        "someone touched the file" and says nothing about which Plex
        library it was actually fetched from."""
        prefs = self.plex_preferences
        try:
            data = {
                "schema_version": self._PLEX_LIBRARY_CACHE_SCHEMA_VERSION,
                "server_config_id": prefs.server_config_id,
                "last_successful_refresh_utc": time.time(),
                "library_ids_by_kind": {
                    "music": prefs.music_library_id,
                    "video": prefs.video_library_id,
                    "karaoke": prefs.karaoke_library_id,
                },
                "meta_by_kind": self._plex_meta_by_kind,
            }
            with open(plex_library_cache_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as ex:
            self.diagnostics.record(
                "file_io", "save_plex_library_cache", status="failure",
                severity="warning", details={"exception": str(ex)},
                minimum_level="basic",
            )

    def _save_cache(self, folders: List[Dict], meta_list: List[Dict[str, Any]]):
        started = time.perf_counter()
        data = {
            "schema_version": LIBRARY_CACHE_SCHEMA_VERSION,
            "folders": folders,
            "meta": meta_list,
        }
        with open(cache_file_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        self.diagnostics.record(
            "file_io", "save_library_cache",
            duration_ms=(time.perf_counter() - started) * 1000.0,
            details={"folders": len(folders), "records": len(meta_list)},
            minimum_level="detailed",
        )

    def _scan_folder(self, folder: str, progress=None, should_cancel=None):
        fingerprints: Dict[str, Dict[str, Optional[int]]] = {}
        tracks = []
        seen = 0
        for root, _, files in os.walk(folder):
            if should_cancel and should_cancel():
                break
            seen += 1
            if progress and seen % 10 == 1:
                try:
                    progress(root, len(tracks))
                except Exception:
                    pass
            names_by_casefold = {name.casefold(): name for name in files}
            karaoke_stems = {
                os.path.splitext(name)[0].casefold()
                for name in files
                if os.path.splitext(name)[1].casefold() == ".cdg"
            }
            for name in files:
                if should_cancel and should_cancel():
                    break
                path = os.path.join(root, name)
                ext = os.path.splitext(name)[1].lower()
                if not is_library_scannable(name):
                    continue
                stem, _ = os.path.splitext(name)
                # A backing MP3 and its matching CDG are one logical item;
                # retain the stable CDG identity and suppress the Music row.
                if ext == ".mp3" and stem.casefold() in karaoke_stems:
                    continue
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                lrc_name = names_by_casefold.get((stem + ".lrc").casefold())
                lrc_size = None
                lrc_mtime_ns = None
                if lrc_name:
                    try:
                        lrc_stat = os.stat(os.path.join(root, lrc_name))
                        lrc_size = int(lrc_stat.st_size)
                        lrc_mtime_ns = int(lrc_stat.st_mtime_ns)
                    except OSError:
                        pass
                fingerprints[path] = {
                    "size": int(stat.st_size),
                    "mtime_ns": int(stat.st_mtime_ns),
                    "lrc_size": lrc_size,
                    "lrc_mtime_ns": lrc_mtime_ns,
                }
                if ext == ".cdg":
                    companion_name = names_by_casefold.get((stem + ".mp3").casefold())
                    if companion_name:
                        try:
                            companion_stat = os.stat(os.path.join(root, companion_name))
                            fingerprints[path]["companion_size"] = int(companion_stat.st_size)
                            fingerprints[path]["companion_mtime_ns"] = int(companion_stat.st_mtime_ns)
                        except OSError:
                            pass
                tracks.append(path)
                if progress and len(tracks) % 25 == 0:
                    try:
                        progress(root, len(tracks))
                    except Exception:
                        pass
        signature = stable_folder_signature(folder, fingerprints)
        return signature, tracks, fingerprints

    def add_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Music Folder")
        if not folder:
            return
        self._start_scan(add_folder=folder)

    def rescan_library(self):
        self._start_scan()

    def _load_folder_cache(self) -> List[str]:
        data = self._load_cache()
        if not data:
            return []
        return [f.get("path") for f in data.get("folders", []) if f.get("path")]

    def _merge_folder(self, folder: str):
        sig, tracks, fingerprints = self._scan_folder(folder)
        existing_folders = self._load_folder_cache()
        folders = []
        for path in existing_folders:
            s, t, fps = self._scan_folder(path)
            folders.append({
                "path": path, "signature": s, "tracks": t,
                "file_fingerprints": fps,
            })
        folders.append({
            "path": folder, "signature": sig, "tracks": tracks,
            "file_fingerprints": fingerprints,
        })
        all_tracks = []
        for f in folders:
            all_tracks.extend(f["tracks"])
        self._set_tracks(sorted(set(all_tracks)))
        self._save_cache(folders, self._meta_list)

    def _save_active_library_tab_state(self):
        """Copy the live self.<attr> values back onto the tab that owns them."""
        tab = getattr(self, "_active_library_tab", None)
        if tab is None:
            return
        for attr in LIBRARY_TAB_ALIASED_ATTRS:
            setattr(tab, attr, getattr(self, attr))

    def _activate_library_tab_state(self, tab: LibraryTabState):
        """Swap the live self.<attr> aliases to point at `tab`'s own state.

        This never touches playback (_playback_context_paths/current_path)
        and never starts a rebuild -- it only changes which already-built
        (or still-building, via the shared timers) tab's containers the
        existing library methods read and write.
        """
        self._save_active_library_tab_state()
        self._active_library_tab = tab
        for attr in LIBRARY_TAB_ALIASED_ATTRS:
            setattr(self, attr, getattr(tab, attr))

    def _start_plex_startup_validation(self):
        """Fire-and-forget: if signed into a Plex account (not manual-URL
        mode) and Plex is enabled, rediscover the currently-selected
        server's reachable connection off the GUI thread. Never blocks
        Local startup -- called via QTimer.singleShot(0, ...) from
        _startup_load_preferences, same pattern as the GPU capability
        probe. Local mode is fully usable regardless of the outcome."""
        if getattr(self, "_closing", False):
            return
        prefs = self.plex_preferences
        if prefs.enabled and prefs.use_manual_server:
            # Manual mode already has everything it needs (a typed
            # address + token, no discovery/resolve round-trip) --
            # _plex_effective_connection() is ready to use this instant,
            # so a stale startup cache can refresh immediately rather
            # than waiting on a resolution step this mode never performs.
            if prefs.server_address and prefs.token:
                self._maybe_start_stale_plex_startup_refresh()
            return
        if not prefs.enabled or not prefs.account_token:
            return
        if not prefs.server_client_identifier:
            return
        client_id = prefs.client_identifier or generate_client_identifier()
        worker = PlexServerDiscoveryWorker(client_id, prefs.account_token)
        self._plex_startup_workers = getattr(self, "_plex_startup_workers", [])
        self._plex_startup_workers.append(worker)
        reg_token = self._worker_registry.register(
            "plex_startup_validation", thread=worker, wait_ms=10000,
        )

        def _on_finished(worker=worker, reg_token=reg_token):
            self._worker_registry.unregister(reg_token)
            if worker in self._plex_startup_workers:
                self._plex_startup_workers.remove(worker)
        worker.finished.connect(_on_finished)
        worker.finished_result.connect(self._on_plex_startup_discovery_result)
        worker.start()

    def _on_plex_startup_discovery_result(self, result: dict):
        if getattr(self, "_closing", False):
            return
        if not result.get("success"):
            self._plex_status = "offline"
            return
        prefs = self.plex_preferences
        server_entry = next(
            (s for s in result.get("servers", [])
             if s["client_identifier"] == prefs.server_client_identifier),
            None,
        )
        if server_entry is None or not server_entry["connections"]:
            self._plex_status = "offline"
            return
        client_id = prefs.client_identifier or generate_client_identifier()
        worker = PlexConnectionResolveWorker(
            client_id, server_entry["client_identifier"],
            server_entry["access_token"], server_entry["connections"],
        )
        self._plex_startup_workers.append(worker)
        reg_token = self._worker_registry.register(
            "plex_startup_validation", thread=worker, wait_ms=10000,
        )

        def _on_finished(worker=worker, reg_token=reg_token):
            self._worker_registry.unregister(reg_token)
            if worker in self._plex_startup_workers:
                self._plex_startup_workers.remove(worker)
        worker.finished.connect(_on_finished)
        worker.finished_result.connect(self._on_plex_startup_connection_result)
        worker.start()

    def _on_plex_startup_connection_result(self, result: dict):
        if getattr(self, "_closing", False):
            return
        if not result.get("success"):
            self._plex_status = "offline"
            return
        self._plex_status = "online"
        self._plex_active_connection_uri = result["uri"]
        self._plex_active_access_token = result["access_token"]
        self._maybe_start_stale_plex_startup_refresh()

    def _record_library_source_backing_state(self, context: str):
        """Low-volume integrity diagnostic (item 11): proves whether
        either backing store has been damaged, without ever touching
        paths -- just counts. Emitted at the 4 moments a corruption bug
        would actually show up: startup restore, and immediately before/
        after a source switch or a Plex result landing."""
        local_meta = getattr(self, "_local_full_meta_list_backing", [])
        plex_meta = getattr(self, "_plex_full_meta_list_backing", [])
        self.diagnostics.record(
            "library", "library_source_backing_state",
            details={
                "context": context, "source": getattr(self, "library_source", "local"),
                "local_meta_count": len(local_meta), "plex_meta_count": len(plex_meta),
                "local_audio_count": sum(
                    1 for m in local_meta if m.get("media_type") == MediaType.AUDIO.value
                ),
                "local_video_count": sum(
                    1 for m in local_meta if m.get("media_type") == MediaType.VIDEO.value
                ),
                "local_karaoke_count": sum(
                    1 for m in local_meta if m.get("media_type") == MediaType.KARAOKE.value
                ),
            },
            minimum_level="detailed",
        )

    def _on_library_source_changed(self, index: int):
        self._record_library_source_backing_state("before_source_switch")
        source = self.library_source_selector.itemData(index) or "local"
        self.library_source = source
        # Local behaviour is completely unaffected -- _full_meta_list is a
        # source-aware property (see its definition near
        # _apply_meta_list_to_library_tabs), so Local's own scanned list
        # is a genuinely separate backing store from Plex's.
        self.library_plex_refresh_button.setVisible(source == "plex")
        if source == "plex":
            self._show_plex_library_view()
        else:
            self.library_tabs.setVisible(True)
            self.library_plex_placeholder.setVisible(False)
            self._apply_meta_list_to_library_tabs(
                list(self._full_meta_list), reason="source_switched_to_local",
            )
        self._save_user_settings()
        self.diagnostics.record(
            "library", "library_source_changed",
            details={"source": source},
            minimum_level="basic",
        )
        self._record_library_source_backing_state("after_source_switch")

    _PLEX_CATEGORY_LABEL = {"music": "Music", "video": "Video", "karaoke": "Karaoke"}

    def _show_plex_library_view(self):
        """Populates the Music/Videos/Karaoke tabs from cached Plex data
        (fetching first if nothing has ever been fetched this session),
        or shows the blanket "not configured" placeholder if the account/
        server isn't even set up yet. An individual category's own state
        (not configured / loading / empty / error / real data) is decided
        per-tab, precisely when that tab's own build actually finishes --
        see _finish_plex_tab_apply, hooked into _apply_meta_list_to_library_tabs
        via trigger_source="plex_fetch"."""
        prefs = self.plex_preferences
        any_mapped = bool(
            prefs.music_library_id or prefs.video_library_id or prefs.karaoke_library_id
        )
        if not prefs.enabled or not any_mapped:
            self.library_tabs.setVisible(False)
            self.library_plex_placeholder.setVisible(True)
            self.library_plex_placeholder.setText(
                "No Plex libraries configured yet -- configure Plex in "
                "Preferences > Plex."
            )
            return
        self.library_tabs.setVisible(True)
        self.library_plex_placeholder.setVisible(False)
        combined = list(self._plex_meta_by_kind.get("music", []))
        combined += list(self._plex_meta_by_kind.get("video", []))
        combined += list(self._plex_meta_by_kind.get("karaoke", []))
        self._apply_meta_list_to_library_tabs(
            combined, reason="plex_source_selected", trigger_source="plex_fetch",
        )
        # kind not in _plex_fetched_kinds -- NOT "not
        # self._plex_meta_by_kind.get(kind)" -- a category that was
        # genuinely fetched and came back with zero real items would
        # otherwise look identical to "never fetched" (both are an
        # empty list) and get re-fetched forever, permanently stuck
        # showing "Loading..." instead of "No items found".
        pending = [
            kind for kind, library_id in (
                ("music", prefs.music_library_id),
                ("video", prefs.video_library_id),
                ("karaoke", prefs.karaoke_library_id),
            )
            if library_id and kind not in self._plex_fetched_kinds
        ]
        self._start_plex_library_fetches_prioritized(pending)

    _PLEX_TAB_INDEX_TO_KIND = {0: "music", 1: "video", 2: "karaoke"}

    def _start_plex_library_fetches_prioritized(self, kinds: List[str]):
        """Starts a fetch for each of `kinds`, but not all at once (item
        7): whichever category tab is actually visible right now starts
        immediately, the rest are staggered a beat apart so they don't
        compete with it for the same connection/bandwidth -- a genuinely
        large Music fetch shouldn't be slowed down by a simultaneous
        Video/Karaoke request the user isn't even looking at yet."""
        if not kinds:
            return
        visible_kind = self._PLEX_TAB_INDEX_TO_KIND.get(
            self.library_tabs.currentIndex() if hasattr(self, "library_tabs") else 0,
            "music",
        )
        ordered = sorted(kinds, key=lambda kind: kind != visible_kind)

        def _start_if_still_relevant(kind):
            # The user may have switched away from Plex (or even closed
            # the window) in the 600ms+ this deferred category waited --
            # tree_tracks_video/tree_tracks_karaoke are the SAME shared
            # widgets Local's own tabs use, so starting this fetch
            # unconditionally could clobber whatever Local is now
            # showing in them.
            if getattr(self, "_closing", False) or self.library_source != "plex":
                return
            self._start_plex_library_fetch(kind)

        for index, kind in enumerate(ordered):
            if index == 0:
                self._start_plex_library_fetch(kind)
            else:
                QtCore.QTimer.singleShot(
                    600 * index, lambda kind=kind: _start_if_still_relevant(kind)
                )

    def _set_plex_tab_status_row(self, tree, message: str):
        """Clears `tree` and shows exactly one non-selectable status row --
        the single mechanism behind every Plex tab state that isn't a real
        populated tree (not configured / loading / empty / offline /
        error), so the user can always tell those apart from each other
        and from a genuinely slow-but-working fetch (see item 4/5 of the
        Plex Stage 2 real-device follow-up)."""
        try:
            tree.clear()
            item = QtWidgets.QTreeWidgetItem([message])
            item.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
            tree.addTopLevelItem(item)
        except RuntimeError:
            pass  # tree already torn down (window closing)

    def _finish_plex_tab_apply(
        self, category: str, meta_list, tree, started_at: float,
    ) -> Optional[str]:
        """Called exactly once, right as one Plex-sourced tab's own
        (possibly chunked/async) tree build finishes -- see the
        is_plex_fetch-gated calls inside _apply_meta_list_to_library_tabs.
        Doing this HERE, not synchronously in _start_plex_library_fetch,
        matters: the Music/Videos/Karaoke tabs build sequentially via a
        chained on_finished callback, so a status row set any earlier for
        the video/karaoke tabs would just get wiped out when their own
        (already-scheduled, from-an-earlier-snapshot) build finally runs.

        Returns a "N loaded" status-bar message for the caller to show
        once the WHOLE chain (not just this one tab) has settled -- never
        shown here directly, since the very next tab's own
        _cancel_pending_library_apply()/_set_tracks_from_meta call would
        just clear it a moment later (see _apply_meta_list_to_library_tabs's
        plex_loaded_messages)."""
        self.diagnostics.record(
            "plex", "plex_library_ui_apply_completed",
            details={
                "source": "plex", "category": category, "meta_count": len(meta_list),
                "visible_top_level_rows": tree.topLevelItemCount(),
                "elapsed_ms": round((time.perf_counter() - started_at) * 1000.0, 1),
            },
            minimum_level="basic",
        )
        if meta_list:
            return f"{len(meta_list)} {self._PLEX_LOADED_NOUN[category]} loaded"
        prefs = self.plex_preferences
        library_id = {
            "music": prefs.music_library_id, "video": prefs.video_library_id,
            "karaoke": prefs.karaoke_library_id,
        }[category]
        label = self._PLEX_CATEGORY_LABEL[category]
        if not library_id:
            self._set_plex_tab_status_row(
                tree, f"No Plex {label} library selected. Configure it in Preferences > Plex.",
            )
        elif category in self._plex_refresh_in_flight:
            self._set_plex_tab_status_row(tree, f"Loading Plex {label}…")
        elif category in self._plex_fetch_error:
            self._set_plex_tab_status_row(
                tree, f"Unable to load Plex {label}. {self._plex_fetch_error[category]}",
            )
        elif category in self._plex_fetched_kinds:
            self._set_plex_tab_status_row(tree, "No items found in this Plex library.")
        # else: mapped, not yet started and not in-flight -- nothing to
        # show here yet; _start_plex_library_fetch (called right after
        # this apply, from _show_plex_library_view's own loop) shows its
        # own "Loading..." row once the fetch actually begins.
        return None

    def _plex_effective_connection(self):
        """(server_address, token) to use for a Plex API request right
        now -- manual/Advanced fields if that mode is active, otherwise
        the runtime-only resolved connection from
        _start_plex_startup_validation()/the Preferences dialog's own
        server selection (never persisted -- see plex_preferences.py's
        own docstring on why). Returns ("", "") if nothing is resolved
        yet, which callers must treat as "not ready", not as an error."""
        prefs = self.plex_preferences
        if prefs.use_manual_server:
            return prefs.server_address, prefs.token
        return self._plex_active_connection_uri, self._plex_active_access_token

    _PLEX_ITEM_TYPE = {"music": 10, "video": 1, "karaoke": None}

    def _plex_tree_for_kind(self, kind: str):
        return {
            "music": self.tree_tracks, "video": self.tree_tracks_video,
            "karaoke": self.tree_tracks_karaoke,
        }[kind]

    def _start_plex_library_fetch(self, kind: str):
        if kind in self._plex_refresh_in_flight:
            return  # coalesce -- a fetch for this category is already running
        prefs = self.plex_preferences
        library_id = {
            "music": prefs.music_library_id,
            "video": prefs.video_library_id,
            "karaoke": prefs.karaoke_library_id,
        }[kind]
        if not library_id:
            return
        tree = self._plex_tree_for_kind(kind)
        label = self._PLEX_CATEGORY_LABEL[kind]
        # A previous error no longer describes what's about to happen;
        # existing GOOD data, however, is deliberately left alone here
        # (see below) rather than reset -- a refresh that fails must not
        # have already destroyed the last known-good list.
        self._plex_fetch_error.pop(kind, None)
        server_address, token = self._plex_effective_connection()
        has_existing_data = bool(self._plex_meta_by_kind.get(kind))
        if not server_address:
            # OFFLINE: distinct from "still loading" -- _start_plex_startup_validation
            # will retry resolving a connection on its own schedule; this
            # tab just needs to say so rather than sit blank. If there's
            # already good data showing, leave it alone and say so via
            # the status bar instead of replacing it.
            if has_existing_data:
                self.statusBar().showMessage(
                    f"Plex server is unavailable -- showing cached Plex {label}.", 6000,
                )
            else:
                self._set_plex_tab_status_row(tree, "Plex server is unavailable.")
            return
        if has_existing_data:
            # item 10: never blank out an already-populated tab just
            # because a refresh started -- the tree stays exactly as it
            # is unless/until this fetch actually succeeds.
            self.statusBar().showMessage(f"Refreshing Plex {label}…")
        else:
            self._plex_fetched_kinds.discard(kind)
            self._set_plex_tab_status_row(tree, f"Loading Plex {label}…")
        self._plex_refresh_in_flight.add(kind)
        client_id = prefs.client_identifier or generate_client_identifier()
        worker = PlexLibraryFetchWorker(
            server_address, token, client_id, library_id, self._PLEX_ITEM_TYPE[kind],
            kind, prefs.server_config_id, self._plex_fetch_generation,
        )
        # Tracked by kind so a superseding refresh/generation bump can
        # cooperatively cancel an in-flight paginated fetch between pages
        # (see PlexLibraryFetchWorker.stop()) instead of letting it burn
        # bandwidth on a result that's about to be discarded anyway.
        self._plex_active_workers_by_kind[kind] = worker
        reg_token = self._worker_registry.register(
            "plex_library_fetch", thread=worker, wait_ms=15000,
        )

        def _on_finished(worker=worker, reg_token=reg_token, kind=kind):
            self._worker_registry.unregister(reg_token)
            if self._plex_active_workers_by_kind.get(kind) is worker:
                del self._plex_active_workers_by_kind[kind]
        worker.finished.connect(_on_finished)
        worker.finished_result.connect(self._on_plex_library_fetch_result)
        worker.progress.connect(self._on_plex_library_fetch_progress)
        worker.setParent(self)
        self.diagnostics.record(
            "plex", "plex_library_refresh_started",
            details={"media_kind": kind}, minimum_level="basic",
        )
        worker.start()

    def _on_plex_library_fetch_progress(self, kind: str, message: str):
        if getattr(self, "_closing", False):
            return
        if self.library_source != "plex" or kind not in self._plex_refresh_in_flight:
            return
        if self._plex_meta_by_kind.get(kind):
            # Refreshing existing data -- progress goes to the status
            # bar (see _start_plex_library_fetch), never replacing the
            # still-visible tree.
            self.statusBar().showMessage(message)
        else:
            self._set_plex_tab_status_row(self._plex_tree_for_kind(kind), message)

    _PLEX_LOADED_NOUN = {"music": "tracks", "video": "videos", "karaoke": "karaoke items"}

    def _on_plex_library_fetch_result(self, result: dict):
        if getattr(self, "_closing", False):
            return
        kind = result.get("media_kind", "")
        self._plex_refresh_in_flight.discard(kind)
        # Stale-result guard (item 15): the source/server/mapping may have
        # changed while this fetch was in flight -- a late result must
        # never overwrite newer state.
        if result.get("generation") != self._plex_fetch_generation:
            return
        if not result.get("success"):
            reason = result.get("reason") or "Unknown error"
            self._plex_fetch_error[kind] = reason
            label = self._PLEX_CATEGORY_LABEL[kind]
            if self.library_source == "plex":
                if self._plex_meta_by_kind.get(kind):
                    # item 10: a failed refresh must never destroy the
                    # last known-good list -- the tree was left
                    # untouched when this fetch started (see
                    # _start_plex_library_fetch's has_existing_data
                    # branch), so it's still showing that good data;
                    # just say the refresh itself didn't work.
                    self.statusBar().showMessage(
                        f"Unable to refresh Plex {label}: {reason}", 6000,
                    )
                else:
                    self._set_plex_tab_status_row(
                        self._plex_tree_for_kind(kind),
                        f"Unable to load Plex {label}. {reason}",
                    )
            return
        self._plex_fetch_error.pop(kind, None)
        self._plex_fetched_kinds.add(kind)
        meta_list = result.get("meta_list", [])
        self._plex_meta_by_kind[kind] = meta_list
        details = plex_meta_list_to_queue_detail_cache(meta_list)
        self.queue_detail_cache.update(details)
        for meta in meta_list:
            self._plex_item_updated_at[meta["path"]] = meta.get("updated_at", 0)
            self._plex_meta_by_path[meta["path"]] = meta
        self._save_plex_library_cache()
        if self.library_source == "plex":
            # The "N loaded" status message itself is shown later, once
            # the whole (possibly multi-tab) apply chain this triggers has
            # actually settled -- see _finish_plex_tab_apply's return
            # value and _apply_meta_list_to_library_tabs's
            # plex_loaded_messages, since anything shown here would just
            # get cleared by that chain's own _cancel_pending_library_apply.
            self._show_plex_library_view()
        self.diagnostics.record(
            "plex", "plex_library_refresh_completed",
            details={"media_kind": kind, "item_count": len(meta_list)},
            minimum_level="detailed",
        )
        self._record_library_source_backing_state("after_plex_result_apply")

    def _refresh_plex_library(self):
        """"Refresh Plex" action: re-fetches every mapped category.
        Bumps the generation first so any late result from a PREVIOUS
        (now-superseded) fetch is discarded rather than applied, and
        cooperatively cancels any already-in-flight fetch (between
        pages -- see PlexLibraryFetchWorker.stop()) rather than either
        launching a competing overlapping request or silently coalescing
        into a no-op while the stale fetch finishes on its own."""
        if self.library_source != "plex":
            return
        self._plex_fetch_generation += 1
        for kind, worker in list(self._plex_active_workers_by_kind.items()):
            worker.stop()
            self._plex_refresh_in_flight.discard(kind)
        prefs = self.plex_preferences
        for kind, library_id in (
            ("music", prefs.music_library_id),
            ("video", prefs.video_library_id),
            ("karaoke", prefs.karaoke_library_id),
        ):
            if library_id:
                self._start_plex_library_fetch(kind)

    def _on_library_tab_changed(self, index: int):
        target = (
            self._video_tab if index == 1
            else self._karaoke_tab if index == 2
            else self._music_tab
        )
        if target is getattr(self, "_active_library_tab", None):
            return
        self._activate_library_tab_state(target)
        self.diagnostics.record(
            "library", "video_tab_populated"
            if target.media_type == MediaType.VIDEO
            else "karaoke_tab_populated"
            if target.media_type == MediaType.KARAOKE
            else "music_tab_activated",
            details={"albums": self.tree_tracks.topLevelItemCount()},
            minimum_level="detailed",
        )

    @property
    def _full_meta_list(self) -> List[Dict[str, Any]]:
        """Source-aware READ: transparently returns the "local" or "plex"
        backing list depending on self.library_source, so every existing
        piece of local-library code that *reads* self._full_meta_list
        (_apply_meta_list_to_library_tabs, _rebuild_library_search_index,
        etc.) needs ZERO changes.

        The setter below is intentionally ambient too, but only safe for
        a read-modify-write of the CURRENTLY ACTIVE source's own list
        (e.g. _rebuild_library_search_index's `self._full_meta_list =
        [clean(m) for m in self._full_meta_list]`) -- the read and write
        share one ambient resolution, atomically, so there's no way for
        them to disagree.

        It is NOT safe for a call site that is populating a *specific*,
        already-known source's data (a Local disk-cache restore, a Plex
        fetch result, a Local "remove from library" edit) -- self.library_source
        can be stale relative to what's actually being written (proven
        real-world: at startup, a *persisted* library_source of "plex"
        from a previous session is already set before the Local disk-
        cache restore runs, so writing through this ambient setter filed
        the real ~47k-record Local list under the Plex backing store
        instead -- Local looked fine at startup, since the tree itself
        is built straight from the meta_list parameter, not this
        property, but appeared to lose everything the next time Local
        was actually selected). Those call sites use
        _store_full_meta_list (Local-vs-Plex is explicit, from
        trigger_source) or write self._local_full_meta_list_backing
        directly (the Local-only admin/remove/refresh/rescan paths,
        which are never plausibly Plex data)."""
        if getattr(self, "library_source", "local") == "plex":
            return getattr(self, "_plex_full_meta_list_backing", [])
        return getattr(self, "_local_full_meta_list_backing", [])

    @_full_meta_list.setter
    def _full_meta_list(self, value: List[Dict[str, Any]]) -> None:
        if getattr(self, "library_source", "local") == "plex":
            self._plex_full_meta_list_backing = value
        else:
            self._local_full_meta_list_backing = value

    def _store_full_meta_list(self, meta_list, trigger_source: str) -> None:
        """Writes to the LOCAL or PLEX backing store based on what THIS
        call is explicitly known to be populating (trigger_source), never
        the ambient self.library_source -- see the _full_meta_list
        property's own docstring for why the ambient setter is unsafe
        here specifically."""
        if trigger_source == "plex_fetch":
            self._plex_full_meta_list_backing = list(meta_list)
        else:
            self._local_full_meta_list_backing = list(meta_list)

    def _full_meta_list_for_trigger_source(self, trigger_source: str) -> List[Dict[str, Any]]:
        """The backing store trigger_source explicitly targets -- NOT the
        ambient self._full_meta_list getter, which reads whatever
        self.library_source currently is and can disagree with
        trigger_source (see _store_full_meta_list's docstring)."""
        if trigger_source == "plex_fetch":
            return getattr(self, "_plex_full_meta_list_backing", [])
        return getattr(self, "_local_full_meta_list_backing", [])

    def _apply_meta_list_to_library_tabs(
        self, meta_list: List[Dict[str, Any]],
        reason: str = "manual_refresh",
        trigger_source: str = "main_window",
    ):
        """Split one shared-scan meta_list by media type and (re)build both
        the Music and Videos tabs from it -- one physical library scan, two
        independent tab trees, built sequentially so they never contend for
        the single shared build timer/queue that _set_tracks_from_meta uses."""
        self._cancel_pending_library_apply()
        # Explicit, not the ambient _full_meta_list setter -- trigger_source
        # unambiguously says which backing store this meta_list belongs
        # to (startup/Local-rescan callers vs. "plex_fetch"), whereas
        # self.library_source can be stale relative to it (e.g. a
        # persisted library_source="plex" from a previous session is
        # already set before this very call restores the Local disk
        # cache at startup). See _store_full_meta_list's docstring.
        # Explicit, not the ambient _full_meta_list setter -- trigger_source
        # unambiguously says which backing store this meta_list belongs
        # to (startup/Local-rescan callers vs. "plex_fetch"), whereas
        # self.library_source can be stale relative to it (e.g. a
        # persisted library_source="plex" from a previous session is
        # already set before this very call restores the Local disk
        # cache at startup). See _store_full_meta_list's docstring.
        self._store_full_meta_list(meta_list, trigger_source)
        # Rebuild both tabs' search indices/generations for *this* cycle
        # before starting either build -- _tree_build_tick's staleness guard
        # compares a build's own apply_generation against its tab's current
        # _library_search_generation, so those must be settled first.
        self._rebuild_library_search_index()
        music_meta = [
            m for m in meta_list if m.get("media_type") == MediaType.AUDIO.value
        ]
        video_meta = [
            m for m in meta_list if m.get("media_type") == MediaType.VIDEO.value
        ]
        karaoke_meta = [
            m for m in meta_list if m.get("media_type") == MediaType.KARAOKE.value
        ]
        self.diagnostics.record(
            "library", "media_classified",
            details={
                "audio": len(music_meta), "video": len(video_meta),
                "karaoke": len(karaoke_meta),
                "unsupported": len(meta_list) - len(music_meta) - len(video_meta) - len(karaoke_meta),
            },
            minimum_level="detailed",
        )
        if karaoke_meta:
            # Aggregate counts, not one call per file -- matches the
            # existing media_classified pattern above.
            loose_valid = sum(
                1 for m in karaoke_meta
                if m.get("karaoke_source_type") == "loose"
                and m.get("karaoke_validation_state") == "valid"
            )
            zip_valid = sum(
                1 for m in karaoke_meta
                if m.get("karaoke_source_type") == "zip"
                and m.get("karaoke_validation_state") == "valid"
            )
            incomplete = sum(
                1 for m in karaoke_meta
                if m.get("karaoke_validation_state") != "valid"
            )
            if loose_valid:
                self.diagnostics.record(
                    "library", "karaoke_pair_detected",
                    details={"count": loose_valid}, minimum_level="detailed",
                )
            if zip_valid:
                self.diagnostics.record(
                    "library", "karaoke_zip_validated",
                    details={"count": zip_valid}, minimum_level="detailed",
                )
            if incomplete:
                self.diagnostics.record(
                    "library", "karaoke_incomplete", severity="warning",
                    details={"count": incomplete}, minimum_level="basic",
                )
        visually_selected = (
            self._video_tab if getattr(self, "library_tabs", None) is not None
            and self.library_tabs.currentIndex() == 1
            else self._karaoke_tab if getattr(self, "library_tabs", None) is not None
            and self.library_tabs.currentIndex() == 2
            else self._music_tab
        )
        is_plex_fetch = trigger_source == "plex_fetch"
        music_started = time.perf_counter()
        video_started = None
        karaoke_started = None
        # Any tab's "N loaded" message would just get wiped by the NEXT
        # tab's own _cancel_pending_library_apply()/_set_tracks_from_meta
        # call a moment later (they share the same status bar) -- collect
        # them here and show them together only once the whole chain
        # (all 3 tabs) has genuinely finished, in _restore_visual_selection,
        # after which nothing else in this cycle touches the status bar.
        plex_loaded_messages: List[str] = []

        def _restore_visual_selection():
            if is_plex_fetch:
                message = self._finish_plex_tab_apply(
                    "karaoke", karaoke_meta, self.tree_tracks_karaoke, karaoke_started,
                )
                if message:
                    plex_loaded_messages.append(message)
                if plex_loaded_messages:
                    self.statusBar().showMessage("; ".join(plex_loaded_messages), 4000)
            self._activate_library_tab_state(visually_selected)

        def _build_karaoke_tab():
            nonlocal karaoke_started
            if is_plex_fetch:
                message = self._finish_plex_tab_apply(
                    "video", video_meta, self.tree_tracks_video, video_started,
                )
                if message:
                    plex_loaded_messages.append(message)
            karaoke_started = time.perf_counter()
            self._activate_library_tab_state(self._karaoke_tab)
            self._set_tracks_from_meta(
                karaoke_meta, apply_generation=self._library_search_generation,
                reason=reason, trigger_source=trigger_source,
                on_finished=_restore_visual_selection,
            )

        def _build_video_tab():
            nonlocal video_started
            if is_plex_fetch:
                message = self._finish_plex_tab_apply(
                    "music", music_meta, self.tree_tracks, music_started,
                )
                if message:
                    plex_loaded_messages.append(message)
            video_started = time.perf_counter()
            self._activate_library_tab_state(self._video_tab)
            # Pass this tab's own search generation explicitly -- the
            # _tree_build_tick staleness guard compares _library_apply_generation
            # against _library_search_generation, and each tab now keeps an
            # independent search-generation counter, so the two must be tied
            # together explicitly rather than relying on the single shared
            # counter they used to both derive from.
            self._set_tracks_from_meta(
                video_meta, apply_generation=self._library_search_generation,
                reason=reason, trigger_source=trigger_source,
                on_finished=_build_karaoke_tab,
            )

        self._activate_library_tab_state(self._music_tab)
        self._set_tracks_from_meta(
            music_meta, apply_generation=self._library_search_generation,
            reason=reason, trigger_source=trigger_source,
            on_finished=_build_video_tab,
        )

    def _set_tracks_from_meta(
        self, meta_list: List[Dict[str, Any]],
        apply_generation: Optional[int] = None,
        cached_artwork_only: bool = False,
        prepared_album_entries: Optional[List[tuple]] = None,
        reason: str = "manual_refresh",
        trigger_source: str = "main_window",
        on_finished: Optional[Any] = None,
    ):
        apply_started = time.perf_counter()
        self._library_request_serial += 1
        if apply_generation is None:
            apply_generation = self._library_request_serial
        correlation_id = uuid.uuid4().hex
        signature = (
            reason,
            apply_generation,
            len(meta_list),
            str(meta_list[0].get("path") if meta_list else ""),
            str(meta_list[-1].get("path") if meta_list else ""),
        )
        if (
            self._library_apply_started
            and signature == self._last_library_request_signature
        ):
            self.diagnostics.record(
                "library", "library_rebuild_coalesced",
                generation=apply_generation,
                correlation_id=correlation_id,
                status="cancelled",
                details={"reason": reason, "trigger_source": trigger_source},
                minimum_level="detailed",
            )
            if on_finished is not None:
                try:
                    on_finished()
                except Exception:
                    pass
            return
        self._last_library_request_signature = signature
        self.diagnostics.record(
            "library", "apply_library_results",
            phase="start", status="running",
            generation=apply_generation,
            correlation_id=correlation_id,
            details={
                "reason": reason,
                "trigger_source": trigger_source,
                "existing_top_level_rows": self.tree_tracks.topLevelItemCount(),
                "search_active": self._search_results_active,
            },
            minimum_level="detailed",
        )
        previous_path = self._get_selected_path()
        previous_album_key = None
        current_item = self.tree_tracks.currentItem()
        if current_item is not None:
            current_data = current_item.data(0, QtCore.Qt.ItemDataRole.UserRole)
            if isinstance(current_data, dict):
                previous_album_key = current_data.get("key")
        if previous_path is None:
            previous_path = self._pending_selection_path
        if previous_album_key is None:
            previous_album_key = self._pending_selection_album_key
        self._cancel_pending_library_apply()
        clean_started = time.perf_counter()
        meta_list = list(meta_list)
        cached_ms = (time.perf_counter() - clean_started) * 1000.0
        self._meta_list = meta_list
        # Checks the backing store trigger_source actually targets, not
        # the ambient _full_meta_list getter -- this call runs 3 times
        # per _apply_meta_list_to_library_tabs cycle (music/video/karaoke,
        # each with a media-type-filtered SUBSET of the full list, all
        # sharing the same trigger_source). Checking the ambient property
        # meant that whenever self.library_source didn't happen to match
        # trigger_source (e.g. the Local startup restore running while a
        # *previous* session's persisted library_source was still "plex"),
        # every one of those 3 calls saw an empty "currently active"
        # store and re-fired this fallback -- so the smaller video/
        # karaoke subsets (often empty) clobbered the real, already-
        # correctly-populated Local backing store moments after
        # _apply_meta_list_to_library_tabs's own write. Checking the
        # correct target store instead means this only ever fires when
        # THAT store is genuinely still unpopulated.
        # Checks the backing store trigger_source actually targets, not
        # the ambient _full_meta_list getter -- this call runs 3 times
        # per _apply_meta_list_to_library_tabs cycle (music/video/karaoke,
        # each with a media-type-filtered SUBSET of the full list, all
        # sharing the same trigger_source). Checking the ambient property
        # meant that whenever self.library_source didn't happen to match
        # trigger_source (e.g. the Local startup restore running while a
        # *previous* session's persisted library_source was still "plex"),
        # every one of those 3 calls saw an empty "currently active"
        # store and re-fired this fallback -- so the smaller video/
        # karaoke subsets (often empty) clobbered the real, already-
        # correctly-populated Local backing store moments after
        # _apply_meta_list_to_library_tabs's own write. Checking the
        # correct target store instead means this only ever fires when
        # THAT store is genuinely still unpopulated.
        if not self._full_meta_list_for_trigger_source(trigger_source):
            self._store_full_meta_list(meta_list, trigger_source)
        self._pending_selection_path = previous_path
        self._pending_selection_album_key = previous_album_key

        clear_started = time.perf_counter()
        signals_were_blocked = self.tree_tracks.blockSignals(True)
        self.tree_tracks.setUpdatesEnabled(False)
        sorting_enabled = self.tree_tracks.isSortingEnabled()
        self.tree_tracks.setSortingEnabled(False)
        try:
            while self.tree_tracks.topLevelItemCount():
                old_item = self.tree_tracks.takeTopLevelItem(0)
                if old_item is not None:
                    self._retired_library_items.append(old_item)
        finally:
            self.tree_tracks.setSortingEnabled(sorting_enabled)
            self.tree_tracks.setUpdatesEnabled(True)
            self.tree_tracks.blockSignals(signals_were_blocked)
        if self._retired_library_items:
            self._retired_library_cleanup_timer.start()
        clear_ms = (time.perf_counter() - clear_started) * 1000.0
        self._library_tree_detach_ms = clear_ms
        self.tree_item_by_path = {}
        self.album_item_by_key = {}
        self.album_key_by_path = {}
        self._cover_queue = []
        self._cover_timer.stop()
        self._populate_queue = []
        self._populate_timer.stop()
        try:
            self.beat.setPaused(False)
        except Exception:
            pass
        grouping_started = time.perf_counter()
        album_entries = (
            prepared_album_entries
            if prepared_album_entries is not None
            else group_library_albums(meta_list)
        )
        grouping_ms = (time.perf_counter() - grouping_started) * 1000.0
        sorting_started = time.perf_counter()
        self._build_queue = deque(sorted(album_entries, key=lambda t: t[0]))
        sorting_ms = (time.perf_counter() - sorting_started) * 1000.0
        self._build_playlist = []
        self.track_index_by_path = {}
        self._library_apply_generation = apply_generation
        self._library_apply_reason = reason
        self._library_apply_trigger = trigger_source
        self._library_apply_correlation_id = correlation_id
        self._library_cached_artwork_only = cached_artwork_only
        self._library_apply_started = apply_started
        self._library_apply_chunks = 0
        self._library_apply_longest_chunk_ms = 0.0
        self._library_apply_item_ms = 0.0
        self._library_apply_finalization_ms = 0.0
        self._library_apply_timer_ticks = 0
        self._library_apply_album_population_ms = 0.0
        self._library_apply_initial_albums_populated = 0
        self._library_apply_initial_track_rows = 0
        self._lazy_albums_populated = 0
        self._lazy_track_rows_created = 0
        self._library_apply_token += 1
        self._library_finalize_scheduled = False
        self._log(
            f"Library apply prepared: generation={apply_generation}; records={len(meta_list)}; "
            f"albums={len(self._build_queue)}; cached_results_ms={cached_ms:.1f}; "
            f"grouping_ms={grouping_ms:.1f}; sorting_ms={sorting_ms:.1f}; "
            f"tree_clear_ms={clear_ms:.1f}; row_limit={self._library_apply_chunk_size}; "
            f"reason={reason}; correlation_id={correlation_id}; "
            f"time_budget_ms={self._library_apply_time_budget * 1000.0:.1f}"
        )
        if trigger_source != "plex_fetch":
            # A Plex-triggered apply communicates its own state through
            # each tab's in-tree status row plus the "N loaded" transient
            # message _on_plex_library_fetch_result shows once a
            # category's real data actually lands -- this generic message
            # would otherwise overwrite that (the Music/Videos/Karaoke
            # tabs build sequentially, each calling this) with a stale
            # "Updating library..." that never gets cleared.
            self.statusBar().showMessage("Restoring library\u2026" if cached_artwork_only else "Updating library\u2026")
        # Set after the internal _cancel_pending_library_apply() call above
        # (which clears any *previous* build's callback) so this build's own
        # on_finished survives to fire when its chunked build completes.
        self._library_apply_on_finished = on_finished
        self._build_timer.start()

    def _cleanup_retired_library_items(self):
        started = time.perf_counter()
        removed = 0
        while (
            self._retired_library_items
            and removed < 2
            and time.perf_counter() - started < 0.003
        ):
            self._retired_library_items.popleft()
            removed += 1
        if not self._retired_library_items:
            self._retired_library_cleanup_timer.stop()
            gc_started = time.perf_counter()
            gc.collect(0)
            self.diagnostics.record(
                "library", "deferred_old_item_cleanup",
                duration_ms=(time.perf_counter() - gc_started) * 1000.0,
                correlation_id=self._library_apply_correlation_id,
                generation=self._library_apply_generation,
                details={"items_removed": removed, "bounded": True},
                minimum_level="detailed",
            )

    def _library_apply_callback_is_current(self, token: int, generation) -> bool:
        return bool(
            self._library_apply_started
            and token == self._library_apply_token
            and not getattr(self, "_closing", False)
            and (
                self._library_apply_reason
                not in ("search_results", "clear_search")
                or generation == self._library_search_generation
            )
        )

    def _schedule_library_apply_finish(self):
        if self._library_finalize_scheduled or not self._library_apply_started:
            return
        self._library_finalize_scheduled = True
        token = self._library_apply_token
        generation = self._library_apply_generation
        QtCore.QTimer.singleShot(
            0, lambda: self._finish_library_result_apply(token, generation)
        )

    def _finish_library_result_apply(self, token: int, generation):
        if not self._library_apply_callback_is_current(token, generation):
            return
        finalization_started = time.perf_counter()
        self.tracks = self._build_playlist
        self.track_index_by_path = {p: i for i, p in enumerate(self.tracks)}
        selection_started = time.perf_counter()
        signals_were_blocked = self.tree_tracks.blockSignals(True)
        try:
            selected_path = self._pending_selection_path
            if selected_path:
                self._select_tree_item(selected_path)
            elif self._pending_selection_album_key:
                album_item = self.album_item_by_key.get(self._pending_selection_album_key)
                if album_item is not None:
                    self.tree_tracks.setCurrentItem(album_item)
        finally:
            self.tree_tracks.blockSignals(signals_were_blocked)
            self.tree_tracks.viewport().update()
        selection_ms = (time.perf_counter() - selection_started) * 1000.0
        self._library_apply_finalization_ms = (
            time.perf_counter() - finalization_started
        ) * 1000.0
        QtCore.QTimer.singleShot(
            0,
            lambda: self._finish_library_deferred_work(
                token, generation, selection_ms
            ),
        )

    def _finish_library_deferred_work(
        self, token: int, generation, selection_ms: float,
    ):
        if not self._library_apply_callback_is_current(token, generation):
            return
        artwork_started = time.perf_counter()
        expansion_restore_ms = 0.0
        self._queue_visible_album_covers()
        artwork_queue_ms = (time.perf_counter() - artwork_started) * 1000.0
        total_ms = (time.perf_counter() - self._library_apply_started) * 1000.0
        average_chunk_ms = (
            self._library_apply_item_ms / self._library_apply_chunks
            if self._library_apply_chunks
            else 0.0
        )
        self._log(
            f"Library apply complete: generation={self._library_apply_generation}; "
            f"albums={self.tree_tracks.topLevelItemCount()}; chunks={self._library_apply_chunks}; "
            f"row_limit={self._library_apply_chunk_size}; "
            f"time_budget_ms={self._library_apply_time_budget * 1000.0:.1f}; "
            f"item_insertion_ms={self._library_apply_item_ms:.1f}; "
            f"longest_chunk_ms={self._library_apply_longest_chunk_ms:.1f}; "
            f"average_chunk_ms={average_chunk_ms:.1f}; "
            f"selection_restore_ms={selection_ms:.1f}; "
            f"finalization_ms={self._library_apply_finalization_ms:.1f}; "
            f"expansion_restore_ms={expansion_restore_ms:.1f}; "
            f"album_population_ms={self._library_apply_album_population_ms:.1f}; "
            f"artwork_queue_ms={artwork_queue_ms:.1f}; "
            f"initial_albums_populated={self._library_apply_initial_albums_populated}; "
            f"initial_track_rows={self._library_apply_initial_track_rows}; "
            f"lazy_albums_populated={self._lazy_albums_populated}; "
            f"lazy_track_rows={self._lazy_track_rows_created}; "
            f"expansion_states_restored=0; "
            f"visualizer_ticks={self._library_apply_timer_ticks}; total_ms={total_ms:.1f}"
        )
        self.diagnostics.record(
            "library", "apply_library_results",
            generation=self._library_apply_generation,
            correlation_id=self._library_apply_correlation_id,
            duration_ms=total_ms,
            severity="warning" if total_ms >= 100 else "info",
            details={
                "albums": self.tree_tracks.topLevelItemCount(),
                "chunks": self._library_apply_chunks,
                "longest_chunk_ms": self._library_apply_longest_chunk_ms,
                "average_chunk_ms": average_chunk_ms,
                "selection_restore_ms": selection_ms,
                "finalization_ms": self._library_apply_finalization_ms,
                "rows_created": len(self._build_playlist),
                "filesystem_checks": 0,
                "reason": self._library_apply_reason,
                "trigger_source": self._library_apply_trigger,
                "tree_detach_ms": getattr(
                    self, "_library_tree_detach_ms", None
                ),
                "deferred_old_items": len(self._retired_library_items),
            },
            minimum_level="basic" if total_ms >= 100 else "detailed",
        )
        if self._library_apply_trigger != "plex_fetch":
            # Mirrors _set_tracks_from_meta's own matching suppression --
            # a Plex-triggered apply's status messaging (in-tree status
            # rows, the "N loaded" transient message) has its own
            # lifecycle and must not be scrubbed by this generic
            # set-then-clear pairing meant for the plain "Updating
            # library..." message.
            self.statusBar().clearMessage()
        self._library_apply_started = 0.0
        self._library_apply_generation = None
        self._library_apply_reason = None
        self._library_apply_trigger = None
        self._library_apply_correlation_id = None
        self._library_finalize_scheduled = False
        self._library_cached_artwork_only = False
        self._pending_selection_path = None
        self._pending_selection_album_key = None
        self._report_startup(
            "library-restore",
            "Restoring music library...",
            94,
            duration_ms=total_ms,
        )
        self._emit_startup_ready()
        callback = self._library_apply_on_finished
        self._library_apply_on_finished = None
        if callback is not None:
            try:
                callback()
            except Exception:
                pass

    def _cancel_pending_library_apply(self):
        self._build_timer.stop()
        self._library_apply_token += 1
        self._library_finalize_scheduled = False
        if self._library_apply_started:
            self._log(
                f"Library apply cancelled: generation={self._library_apply_generation}; "
                f"chunks={self._library_apply_chunks}"
            )
        self._build_queue = deque()
        self._library_apply_started = 0.0
        self._library_apply_generation = None
        self._library_cached_artwork_only = False
        self._library_apply_on_finished = None
        self.statusBar().clearMessage()
        self.tree_tracks.setUpdatesEnabled(True)

    def _rebuild_library_search_index(self):
        started = time.perf_counter()
        self._full_meta_list = [
            clean_meta_dict(meta) for meta in self._full_meta_list
        ]
        # Each tab searches only its own media type -- separate indices
        # built from one shared meta list, not a second scan.
        music_meta = [
            m for m in self._full_meta_list
            if m.get("media_type") == MediaType.AUDIO.value
        ]
        video_meta = [
            m for m in self._full_meta_list
            if m.get("media_type") == MediaType.VIDEO.value
        ]
        karaoke_meta = [
            m for m in self._full_meta_list
            if m.get("media_type") == MediaType.KARAOKE.value
        ]
        self._music_tab._library_search_index = build_search_index(music_meta)
        self._video_tab._library_search_index = build_search_index(video_meta)
        self._karaoke_tab._library_search_index = build_search_index(karaoke_meta)
        self._music_tab._library_search_generation += 1
        self._video_tab._library_search_generation += 1
        self._karaoke_tab._library_search_generation += 1
        # Keep the live aliases in sync with whichever tab is currently
        # active so a rebuild while a tab is displayed doesn't get clobbered
        # by the next tab-switch's save-then-restore.
        active_tab = getattr(self, "_active_library_tab", None)
        if active_tab is self._music_tab:
            self._library_search_index = self._music_tab._library_search_index
            self._library_search_generation = self._music_tab._library_search_generation
        elif active_tab is self._video_tab:
            self._library_search_index = self._video_tab._library_search_index
            self._library_search_generation = self._video_tab._library_search_generation
        elif active_tab is self._karaoke_tab:
            self._library_search_index = self._karaoke_tab._library_search_index
            self._library_search_generation = self._karaoke_tab._library_search_generation
        self._meta_by_path = {
            meta.get("path"): meta
            for meta in self._full_meta_list
            if meta.get("path")
        }
        self._refresh_recently_played_table()
        self._log(
            f"Library search index: generation={self._library_search_generation}; "
            f"music_records={len(self._music_tab._library_search_index)}; "
            f"video_records={len(self._video_tab._library_search_index)}; "
            f"karaoke_records={len(self._karaoke_tab._library_search_index)}; "
            f"duration_ms={(time.perf_counter() - started) * 1000.0:.1f}"
        )

    def _refresh_library_view_after_change(self):
        query = self.search_box.text() if hasattr(self, "search_box") else ""
        if (query or "").strip() and getattr(self, "search_worker", None):
            self._search_pending_text = query
            self._apply_search_pending()
        else:
            self._search_results_active = False
            self._apply_meta_list_to_library_tabs(
                self._full_meta_list,
                reason="metadata_refresh",
                trigger_source="library_change",
            )

    def _track_display_label(
        self, track_no: int, title: str, artist: str,
        has_synced_lyrics: bool = False,
    ) -> str:
        return format_track_display_label(
            track_no, title, artist, has_synced_lyrics
        )

    def _set_cached_lyrics_availability(
        self, path: str, has_lrc_sidecar: bool,
        has_embedded_synced_lyrics: Optional[bool] = None,
    ):
        meta = self._meta_by_path.get(path)
        if meta is None:
            return
        meta["has_lrc_sidecar"] = bool(has_lrc_sidecar)
        if has_embedded_synced_lyrics is not None:
            meta["has_embedded_synced_lyrics"] = bool(
                has_embedded_synced_lyrics
            )
        meta["has_synced_lyrics"] = bool(
            meta.get("has_lrc_sidecar")
            or meta.get("has_embedded_synced_lyrics")
        )
        item = self.tree_item_by_path.get(path)
        if item is not None and item.data(
            0, QtCore.Qt.ItemDataRole.UserRole
        ) == path:
            item.setText(
                0,
                self._track_display_label(
                    int(meta.get("track_no") or 0),
                    str(meta.get("title") or os.path.basename(path)),
                    str(meta.get("artist") or ""),
                    bool(meta.get("has_synced_lyrics")),
                ),
            )

    def _get_selected_path(self) -> Optional[str]:
        item = self.tree_tracks.currentItem()
        if not item:
            return None
        path = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        return path if isinstance(path, str) else None

    def _use_builtin_player(self) -> bool:
        if self._temporary_backend_override:
            return self._temporary_backend_override in ("miniaudio", "bass")
        return bool(self.use_simple and self.simple_player and not self._simple_fallback_active)

    def play_selected(self):
        path = self._get_selected_path()
        if path is None:
            if self.tracks:
                self.play_index(0, crossfade=False)
            return
        # ensure VLC only if not using the simple backend
        if not self.use_simple:
            self._ensure_vlc()
        self.play_path(path, crossfade=False)

    def _play_tree_item(self, item, column=0):
        data = item.data(0, QtCore.Qt.ItemDataRole.UserRole) if item else None
        if isinstance(data, dict):
            if data.get("placeholder"):
                return
            if "album" in data:
                paths = self._album_paths_from_data(data)
                if paths:
                    self.play_path(paths[0], crossfade=False)
                return
        self.play_selected()

    def _set_playing_button_state(self):
        """A newly-started track should always show Pause, even if the previous track was paused."""
        try:
            self.btn_pause.setText("Pause")
            self.btn_pause.setAccessibleName("Pause")
        except Exception:
            pass

    def _sync_now_playing_overlay_for_media_type(self):
        """The Now Playing overlay -- title, artist, the animated
        equaliser badge, and its rounded panel background, plus the
        drifting bio-fact cards, all painted by the single self.overlay
        widget (JukeboxOverlay) -- visually conflicts with the video
        picture or karaoke CDG graphics the same way the jukebox intro
        already does (see the video/karaoke guard a few lines below this
        method). Unlike the intro-skip guard, which only stops a *new*
        intro from starting, this stops an already-settled badge/cards
        left over from the previous audio track from continuing to paint
        on top of the video/karaoke surface for the whole rest of that
        playback, and brings it back exactly as it was once audio
        playback resumes.

        Idempotent and driven purely by the authoritative
        self._current_media_type (never the selected library tab), so
        it's safe to call from every place that can change or clear that
        state -- _activate_track_ui (new track starts), stop_playback,
        _on_video_error, _on_karaoke_prepare_failed, and the "video ended
        with nothing next" branch of _on_video_end_of_media -- without
        redundant calls ever causing visible flicker: a call while
        already in the right state is a no-op.
        """
        overlay = getattr(self, "overlay", None)
        if overlay is None:
            return
        is_visual_playback = self._current_media_type in (
            MediaType.VIDEO, MediaType.KARAOKE,
        )
        if is_visual_playback:
            if not self._now_playing_overlay_suppressed:
                # Remember exactly what it was -- if it was already
                # hidden for some other reason, restoring later must
                # leave it hidden, not force it back on.
                self._now_playing_overlay_was_visible = overlay.isVisible()
                overlay.hide()
                self._now_playing_overlay_suppressed = True
        elif self._now_playing_overlay_suppressed:
            overlay.setVisible(self._now_playing_overlay_was_visible)
            self._now_playing_overlay_suppressed = False

    def _activate_track_ui(self, index: Optional[int], path: str):
        """Make a track the visible/current song once it is actually taking over."""
        # As early as possible -- the caller has already set
        # _current_media_type for this track before calling here, so this
        # reflects the transition immediately rather than after everything
        # else below runs.
        self._sync_now_playing_overlay_for_media_type()
        # If logging is active for a different track, close it out.
        if self.viz_logger.active and self.viz_logger._track not in (None, path):
            self.viz_logger.stop()
            self._log("Visualiser logging stopped (track changed)")
        self.current_index = index if isinstance(index, int) and index >= 0 else None
        self.current_path = path
        # Focused view/controller harnesses used by tests do not run the
        # heavyweight window constructor.  Keep the identity guard equally
        # safe for those legitimate partial instances.
        if not hasattr(self, "_now_playing_generation"):
            self._now_playing_generation = NowPlayingGeneration()
        now_playing_identity = self._now_playing_generation.begin(
            path, self.current_index
        )
        diagnostics = getattr(self, "diagnostics", None)
        if diagnostics is not None:
            diagnostics.record(
                "now_playing", "detail_request_started",
                generation=now_playing_identity.generation,
                details={"detail_type": "track", **diagnostics.path_details(path)},
                minimum_level="detailed",
            )
        # A release event can be lost if a seek is interrupted by a track,
        # Party Mode or fullscreen transition. Never carry that stale
        # scrubbing state into the next item or progress updates stay muted.
        self.scrubbing = False
        self._reset_recently_played_tracking(path)
        self._schedule_session_save()
        self._set_playing_button_state()
        self.quiet_count = 0
        self._last_quiet_debug_remaining = None
        self._reset_analyzer_clock()
        self._select_tree_item(path)
        try:
            self.beat.setPlaying(False)
            self.beat.setPlaying(True)
        except Exception:
            pass
        if getattr(self, "_current_media_type", MediaType.AUDIO) in (MediaType.VIDEO, MediaType.KARAOKE):
            # Video files can live on a NAS and Mutagen may take seconds to
            # open them. Playback already has library/queue metadata, so use
            # that cached data here instead of blocking the GUI (and delaying
            # the subprocess load command) on every video-to-video change.
            video_tag_loader = getattr(self, "_load_cached_video_tags", None)
            info = (
                video_tag_loader(path)
                if callable(video_tag_loader)
                else self._load_tags(path)
            )
        else:
            # Audio can live on a NAS/network share too (confirmed via a
            # captured stall trace during Cast auto-advance, where this
            # blocked the same _tick() loop that drives the progress bar
            # and equaliser). Show cached data immediately and refresh
            # with the authoritative tags once a background read completes.
            info = self._load_cached_audio_tags(path)
            self._display_track_tags(info, path)
            self._queue_track_tags_async(path)
        self._record_recent_played(path)
        self._update_dj_info(info, path)
        # Keep the established single-argument loader seam; test and plugin
        # callers may replace it.  The real loader reads this captured value.
        self._now_playing_lyric_generation = now_playing_identity.generation
        if getattr(self, "_current_media_type", MediaType.AUDIO) == MediaType.AUDIO:
            self._load_lrc_for_track(path)
        else:
            # Video/karaoke never display these synced lyrics -- video shows
            # its own picture instead of self.overlay (see the AUDIO-only
            # jukebox-intro gate right below), and karaoke drives its own
            # separate CDG lyric mechanism entirely (see
            # _sync_karaoke_position, self._karaoke_document/karaoke_widget --
            # a completely different system, not self._lyrics). Loading here
            # anyway was pure wasted work: _load_lrc_for_track's tag lookup
            # opens the file with Mutagen synchronously on the GUI thread,
            # measured taking ~124ms for an MP4 during a real transition and
            # coinciding with a ~171ms GUI lag -- exactly the class of "NAS
            # video files can take seconds to open with Mutagen" problem the
            # video/karaoke tag-loading branch just above this already works
            # around, just missed for lyrics specifically. Still clears any
            # stale lyrics left over from a previous *audio* track so
            # nothing lingers if the media type changes.
            self._clear_synced_lyrics_state()
        if getattr(self, "_current_media_type", MediaType.AUDIO) == MediaType.AUDIO:
            # The jukebox intro card is a full-window overlay
            # (self.overlay covers the whole central widget, see
            # _start_jukebox_intro) that visually conflicts with the
            # video output's native window surface -- its own picture is
            # already the visual for a video, so skip the card entirely
            # rather than have it render partly behind the video surface.
            try:
                self._start_jukebox_intro(info)
            except Exception:
                pass
            scheduler = getattr(self, "_schedule_current_track_analysis_after_intro", None)
            if callable(scheduler):
                scheduler(path, now_playing_identity.generation)
        else:
            request_current = getattr(self, "_request_current_track_analysis", None)
            if callable(request_current):
                request_current(path)
        # The bounded priority window (current track + next 3 Up Next)
        # just shifted -- promote any path that was metadata-only-deferred
        # while it was further out in the queue to real BPM/Key analysis
        # now that it's in the window.
        promote = getattr(self, "_promote_priority_queue_analysis", None)
        if callable(promote):
            promote()
        if self.bio_worker:
            self._bio_requested_artist = self._normalize_artist_for_bio(
                info.artist
            )
            self._bio_requested_generation = now_playing_identity.generation
            self.bio_worker.set_artist_track(
                self._bio_requested_artist,
                self._normalize_track_for_bio(info.title),
                now_playing_identity.generation,
            )
        # Video never feeds the audio waveform-seekbar analysis pipeline --
        # that's a separate feature from BPM/Key (requested just above via
        # _request_current_track_analysis/_promote_priority_queue_analysis,
        # which now do reach video's audio track) that this phase
        # deliberately doesn't add (see WAVEFORM, ANALYSIS AND AUDIO FEATURES).
        if getattr(self, "_current_media_type", MediaType.AUDIO) == MediaType.AUDIO:
            if is_plex_identity(path):
                # Phase C2.1 acceptance defect: a plex:// identity is a
                # remote HTTP stream, not a local file -- AudioAnalyzer's
                # offline whole-file decode, AnalyzerWorker's librosa
                # pipeline, and WaveformWorker all expect a real path on
                # disk. The live visualiser for Plex audio comes from
                # BASS FFT instead (_analyzer_tick's live_plex_bass
                # branch, which already has this channel's audio decoded
                # -- sending it here too would mean decoding the same
                # remote stream a second time, or a downstream decode
                # failure). Waveform display is simply unavailable for
                # Plex audio (Stage 3A policy) -- set that UI state
                # directly rather than requesting a decode that would
                # have to download the file to ever succeed.
                if self.waveform_seekbar is not None:
                    try:
                        self.waveform_seekbar.set_waveform(None)
                    except Exception:
                        pass
            else:
                if self.analyzer:
                    self.analyzer.load(path)
                if self.analyzer_worker:
                    try:
                        self.analyzer_worker.update_track.emit(path)
                    except Exception:
                        pass
                if self.waveform_seekbar is not None:
                    try:
                        self.waveform_seekbar.set_placeholder(path)
                        if self.waveform_worker:
                            self.waveform_worker.request(path)
                    except Exception:
                        pass
        elif self.waveform_seekbar is not None:
            # Video intentionally has no audio waveform analysis, but it must
            # clear the previous song's waveform and retain the same seekable
            # progress control as a clean, simple video timeline.
            self.waveform_seekbar.set_video_progress(path)
        # now_playing's title is already set correctly above via
        # _display_track_tags(info, path) (both the video and audio
        # branches call it, directly or via _load_cached_video_tags) --
        # see its own docstring for the real-device fix. No separate
        # setText needed here.
        self._sync_party_mode()
        getattr(self, "_schedule_queue_duration_refresh", lambda *_: None)("track_changed")

    def _request_current_track_analysis(self, path: str):
        """Queue missing current-track details without synchronous file I/O."""
        request_analysis = getattr(self, "_request_queue_analysis", None)
        if not callable(request_analysis):
            return
        details = getattr(self, "queue_detail_cache", {}).get(path)
        if details is None:
            details = self._queue_track_details(path, cached_details_only=True)
        request_analysis(path, details)

    def _schedule_current_track_analysis_after_intro(self, path: str, generation: int):
        """Leave the visual title sequence GPU/CPU headroom before BPM work."""
        overlay = getattr(self, "overlay", None)
        delay_ms = int((float(getattr(overlay, "_intro_dur", 0.0)) + 0.15) * 1000)

        def start_if_current():
            guard = getattr(self, "_now_playing_generation", None)
            if guard is None or not guard.is_current(generation, path):
                return
            self._request_current_track_analysis(path)

        QtCore.QTimer.singleShot(max(0, delay_ms), start_if_current)

    def play_index(self, index: int, crossfade: bool = False):
        if index < 0 or index >= len(self.tracks):
            return False
        path = self.tracks[index]
        # Freeze the Next/Previous fallback sequence to whichever tab's list
        # this track came from -- switching Music/Videos tabs afterwards
        # must not change what plays next.
        self._playback_context_paths = list(self.tracks)
        self._playback_context_index = index
        return self._play_path_direct(path, crossfade=crossfade, index=index)

    def _playback_fallback_paths(self) -> List[str]:
        """The Next/Previous library-order fallback sequence: the frozen
        snapshot from the last tree-driven play, or (before any track has
        been chosen this session) the active tab's current list."""
        return self._playback_context_paths or self.tracks

    # -- Playback stability hardening, Phase A: PlaybackAttempt authority ----
    # See playback_attempt.py's module docstring for the full invariant.
    # These four methods are the *only* thing this phase adds -- every
    # existing token (pending_next, _playback_generation,
    # _plex_audio_load_token, crossfade tokens, karaoke generations, video
    # transition IDs) stays exactly as it was, unmodified, as additional
    # defence. This is a second, independent gate layered above them.

    def _begin_playback_attempt(
        self, source_identity: str, media_type: Optional[MediaType],
        reason: str, queue_entry_id: Optional[object] = None,
    ) -> PlaybackAttempt:
        """Start a new authoritative playback attempt, superseding
        whatever attempt (if any) was previously authoritative -- this is
        the "new selection supersedes the old one" invariant (section 3's
        Plex-Track-A-preparing-then-user-selects-Local-Track-B example):
        the old attempt is marked CANCELLED here, synchronously, the
        instant a new one begins, never waiting for its own async work to
        notice on its own."""
        previous = self._current_playback_attempt
        if previous is not None and not previous.is_terminal():
            previous.state = PlaybackAttemptState.CANCELLED
            self.diagnostics.record(
                "playback", "playback_attempt_superseded",
                details={
                    "cancelled_attempt_id": previous.attempt_id,
                    "cancelled_reason": previous.reason,
                    "new_reason": reason,
                },
                minimum_level="detailed",
            )
        attempt = PlaybackAttempt(
            attempt_id=self._next_playback_attempt_id,
            source_identity=source_identity,
            media_type=media_type,
            reason=reason,
            queue_entry_id=queue_entry_id,
        )
        self._next_playback_attempt_id += 1
        self._current_playback_attempt = attempt
        self.diagnostics.record(
            "playback", "playback_attempt_started",
            details={
                "attempt_id": attempt.attempt_id,
                "media_type": media_type.value if media_type else None,
                "reason": reason,
                **self.diagnostics.path_details(source_identity),
            },
            minimum_level="detailed",
        )
        return attempt

    def _is_current_playback_attempt(self, attempt_id: Optional[int]) -> bool:
        """The core authority check every guarded async callback must
        pass before it's allowed to do anything authoritative (start
        playback, change current_path, change backend override, mark
        queue history, change current-track UI, trigger Next, change
        playback_expected/pending progression state). attempt_id=None
        means "no attempt was captured for this call" -- always allowed,
        so existing direct callers/tests that don't pass one are
        unaffected; this only ever narrows behaviour for callers that do."""
        if attempt_id is None:
            return True
        current = self._current_playback_attempt
        return (
            current is not None
            and current.attempt_id == attempt_id
            and not current.is_terminal()
        )

    def _require_current_playback_attempt(self, attempt_id: Optional[int], stage: str) -> bool:
        """The strict authority gate for every MIGRATED async callback
        (Plex resolve, Plex audio load, karaoke prepare, crossfade
        preload) -- unlike _is_current_playback_attempt's own
        attempt_id=None-always-passes default (kept for genuinely
        synchronous legacy call sites that haven't been migrated yet),
        a migrated callback reaching here with attempt_id=None is a
        programming/invariant violation, not a legitimate "no attempt
        was captured" case: every migrated dispatch site captures one
        before starting its worker. Reject and record it distinctly from
        an ordinary superseded-attempt rejection, so the two are never
        conflated in diagnostics, but never crash the release app over
        it.

        NOTE (2026-09-10 Phase C1): passing this check proves the CALLBACK
        is not allowed to act -- historically that did NOT prove the
        underlying native backend object (BassPlayer/miniaudio) was never
        mutated by the worker thread that ran before this callback fired
        (the old PlexAudioLoadWorker/PlayerLoadWorker called player.load(...)
        directly, off the GUI thread, with no attempt-authority check at
        all). Phase C1 closed that gap structurally: BassStreamPrepareWorker/
        MiniaudioSourcePrepareWorker never receive or mutate a live
        BassPlayer/MiniaudioPlayer -- they only ever produce a private
        PreparedBassStream/PreparedMiniaudioSource, which this callback's
        caller commits (via BassPlayer.commit_prepared/
        MiniaudioPlayer.commit_prepared) or discards after this check
        (plus the relevant token and PlayerTargetLease checks) passes.
        See bass_player.PreparedBassStream and player_lease.PlayerTargetLease."""
        if attempt_id is None:
            self.diagnostics.record(
                "playback", "playback_attempt_id_missing",
                details={"stage": stage},
                severity="warning",
                minimum_level="basic",
            )
            return False
        if not self._is_current_playback_attempt(attempt_id):
            self.diagnostics.record(
                "playback", "playback_attempt_stale_callback_rejected",
                details={"attempt_id": attempt_id, "stage": stage},
                minimum_level="basic",
            )
            return False
        return True

    def _cancel_current_playback_attempt(self, reason: str) -> None:
        """STOP is absolute (section 3): the current attempt becomes
        CANCELLED and authoritative becomes NONE, synchronously, right
        here -- not whenever whatever async work it was waiting on
        happens to notice. Every in-flight resolve/load/prepare callback
        for it will still run to completion (harmlessly) once
        _is_current_playback_attempt starts returning False for it."""
        current = self._current_playback_attempt
        if current is None or current.is_terminal():
            return
        current.state = PlaybackAttemptState.CANCELLED
        self._current_playback_attempt = None
        self.diagnostics.record(
            "playback", "playback_attempt_cancelled",
            details={"attempt_id": current.attempt_id, "reason": reason},
            minimum_level="detailed",
        )

    def _advance_playback_attempt_state(
        self, attempt_id: Optional[int], state: PlaybackAttemptState,
    ) -> None:
        """Advance the named attempt's own state (PREPARING/STARTING/
        PLAYING/FAILED) -- a no-op if it's already superseded/cancelled,
        or if no attempt_id was captured for this call site yet."""
        if attempt_id is None:
            return
        current = self._current_playback_attempt
        if current is None or current.attempt_id != attempt_id or current.is_terminal():
            return
        current.state = state

    def _set_player_topology(self, active, inactive, *, reason: str) -> None:
        """Phase C1 (native audio backend ownership): the ONLY place
        simple_player/simple_inactive_player may be reassigned. Bumps
        _player_topology_epoch exactly when the physical active/inactive
        objects actually change (identity comparison, not equality) --
        re-pinning to the SAME pair that's already current (e.g.
        _play_path_direct dispatching another track on an unchanged
        backend) costs nothing extra and invalidates no in-flight
        PlayerTargetLease. See player_lease.py for why the epoch exists
        at all: neither simple_player/simple_inactive_player nor
        bass_player/bass_inactive_player/miniaudio_player/
        miniaudio_inactive_player are stable physical identities -- all
        of them get reassigned as playback progresses."""
        changed = (active is not self.simple_player) or (inactive is not self.simple_inactive_player)
        self.simple_player = active
        self.simple_inactive_player = inactive
        if changed:
            self._player_topology_epoch += 1
            self.diagnostics.record(
                "playback", "player_topology_changed",
                details={"reason": reason, "epoch": self._player_topology_epoch},
                minimum_level="detailed",
            )

    def _promote_inactive_player(self, *, reason: str) -> None:
        """Phase C1: the ONLY place a crossfade/mixed-transition
        promotion may swap active<->inactive. Reuses
        _set_player_topology for the epoch bump, then re-labels whichever
        backend-specific canonical pair (bass_player/bass_inactive_player
        or miniaudio_player/miniaudio_inactive_player) is currently live,
        so both stay in sync with simple_player/simple_inactive_player
        exactly as before -- now from one implementation instead of the
        two duplicated copies this replaces."""
        old_active, old_inactive = self.simple_player, self.simple_inactive_player
        self._set_player_topology(old_inactive, old_active, reason=reason)
        if self.builtin_backend == "bass":
            self.bass_player, self.bass_inactive_player = self.simple_player, self.simple_inactive_player
        else:
            self.miniaudio_player, self.miniaudio_inactive_player = self.simple_player, self.simple_inactive_player

    def _make_target_lease(self, backend_family: str, target_role: str) -> PlayerTargetLease:
        """Phase C1: captures, at async-dispatch time, everything needed
        to prove at commit time that the physical player a preparation
        worker was targeting is still the intended target -- see
        player_lease.py."""
        obj = self.simple_player if target_role == "active" else self.simple_inactive_player
        return PlayerTargetLease(
            backend_family=backend_family, target_role=target_role,
            physical_player_id=obj.physical_id, physical_object=obj,
            topology_epoch=self._player_topology_epoch,
        )

    def _target_lease_still_valid(self, lease: PlayerTargetLease) -> bool:
        """Four independent conditions, deliberately redundant with each
        other (see the revised Phase C1 design, 2026-09-10): the epoch
        check is the fast path, but role-pointer identity and physical-id
        equality are checked separately too, as genuine defence against a
        future dispatch/promotion site that forgets to route through
        _set_player_topology/_promote_inactive_player -- exactly the kind
        of coverage gap _try_recovery_backend turned out to be before this
        design was corrected.

        2026-09-11 addition: backend_family consistency. Checked via the
        immutable physical_id (a "bass-*"/"miniaudio-*" prefix), never
        the mutable builtin_backend preference -- a lease's declared
        family must match the physical player it actually targets.
        Structurally impossible to violate today (every dispatch site's
        backend_family argument and target player come from the same
        _use_bass_backend() check at capture time), but this is cheap,
        independent insurance against a future caller passing "bass"
        while the target role actually points at a miniaudio physical
        object (or vice versa) -- exactly the kind of drift the other
        three checks don't, by themselves, catch."""
        if lease.topology_epoch != self._player_topology_epoch:
            return False
        current = self.simple_player if lease.target_role == "active" else self.simple_inactive_player
        if current is not lease.physical_object:
            return False
        if lease.physical_object.physical_id != lease.physical_player_id:
            return False
        physical_family = lease.physical_player_id.split("-", 1)[0]
        return physical_family == lease.backend_family

    def _discard_prepared_candidate(self, candidate, stage: str) -> None:
        """Phase C1.1 (2026-09-11): every normal candidate-rejection
        branch (Plex BASS load, crossfade preload, mixed Video->Audio
        preload) routes through here instead of calling
        candidate.discard() bare, so a native free failure is never
        silently swallowed. candidate.discard() already returns False
        (never raises) when BASS_StreamFree itself reported failure or
        raised -- PreparedMiniaudioSource.discard() has no native
        resource and always returns True, so this stays a cheap no-op
        diagnostic-wise for the miniaudio path. Never logs anything
        about the candidate's own content (Plex URL/token/HTTP headers/
        raw identity) -- only the rejection stage and the candidate's
        class name, neither of which carries any private data."""
        if not candidate.discard():
            self.diagnostics.record(
                "playback", "prepared_candidate_discard_failed",
                details={"stage": stage, "candidate_type": type(candidate).__name__},
                severity="warning",
                minimum_level="basic",
            )

    def _finalize_unclaimed_prepare_candidate(self, worker, stage: str) -> None:
        """Phase C2 (worker lifetime / shutdown ownership, 2026-09-11):
        the ONE seam that resolves a BassStreamPrepareWorker/
        MiniaudioSourcePrepareWorker's still-unclaimed candidate,
        whichever of two independent callers reaches it first:

        - WorkerLifetimeRegistry's shutdown_all(), as a
          finalize_after_join callback, called synchronously once
          worker.wait(...) has positively proven the worker's run() has
          returned -- this can resolve the candidate even if the GUI
          thread never returns to the event loop to process the queued
          `prepared` signal at all;
        - this same worker's own identity-safe `finished` handler, if it
          fires (via the normal queued signal) while self._closing is
          True -- covering the case where `finished` happens to be
          delivered before `prepared` is ever processed (Qt's per-worker
          prepared-then-finished emit order is NOT relied upon here).

        worker.claim_candidate() is the single exactly-once gate both of
        those, and the normal (non-shutdown) prepared-callback path, all
        go through -- so no matter which of the (up to) three callers
        gets here first, every other one sees None and does nothing.
        Routes through _discard_prepared_candidate() (never a bare
        candidate.discard()) so a native free failure stays
        diagnostically visible here exactly as it is on every other
        rejection path."""
        candidate = worker.claim_candidate()
        if candidate is not None:
            self._discard_prepared_candidate(candidate, stage)

    def _on_simple_worker_finished(self, attribute_name: str, worker, registry_token) -> None:
        """Phase C2: shared identity-safe finished handler for the
        persistent, single-instance analysis/utility workers
        (analyzer_worker, bio_worker, search_worker, queue_analysis_worker,
        loudness_worker, waveform_worker) plus the one-shot scan/backfill/
        playlist-load/queue-drop/album-tag-refresh workers -- none of
        these carry a Phase C1 native candidate, so there is nothing to
        finalize beyond the identity-safe attribute clear + registry
        release + shutdown-resume check every registered worker needs."""
        if getattr(self, attribute_name, None) is worker:
            setattr(self, attribute_name, None)
        self._worker_registry.unregister(registry_token)
        self._maybe_resume_final_shutdown()

    def _play_path_direct(self, path: str, crossfade: bool = False, index: Optional[int] = None, immediate_crossfade: bool = False, identity_path: Optional[str] = None, media_type_override: Optional[MediaType] = None) -> bool:
        """Play a real file path, including Up Next entries that are not in the library index."""
        if not path:
            return False
        # Playback stability hardening, Phase A: every direct play
        # request -- Local or Plex, audio, video, or karaoke -- funnels
        # through this one function, making it the single correct place
        # to establish a new authoritative PlaybackAttempt. This alone is
        # what makes "Local audio supersedes an in-flight Plex load"
        # true: the mere act of calling this again, for anything,
        # immediately cancels whatever attempt was previously
        # authoritative (see _begin_playback_attempt).
        self._begin_playback_attempt(
            path, media_type_override or classify_path(path), "play_path_direct",
        )
        transition_manager = getattr(self, "_video_transition_manager", None)
        if (
            transition_manager is not None
            and transition_manager.state.value in ("outgoing", "incoming")
        ):
            # A direct library/queue selection supersedes the pending visual
            # transition.  The manager's own switch callback runs in the
            # SWITCHING state, so it deliberately does not enter this branch.
            transition_manager.cancel("external_media_request")
        if self._mixed_transition_state != "idle":
            # Same for a v1.0.71 mixed-media transition -- a direct
            # library/queue selection (double-click, etc.) supersedes it.
            self._cancel_mixed_media_transition("external_media_request")
        if is_plex_identity(path):
            # Stage 3A: Direct Play only, dispatched BEFORE os.path.isfile/
            # Mutagen/BASS-local-file/video local-file loading ever see
            # this synthetic identity (none of those understand a plex://
            # string and must never be given the chance to fail on it
            # mysteriously). classify_path's extension-suffix fallback is
            # explicitly sanctioned for this by plex_identity.py's own
            # docstring -- it works unchanged on a plex://.../<key>.<ext>
            # string since it only ever inspects the suffix.
            plex_kind = classify_path(path)
            if plex_kind == MediaType.VIDEO and media_type_override is None:
                return self._play_plex_video_path_direct(path, index=index)
            if plex_kind == MediaType.AUDIO:
                return self._play_plex_audio_path_direct(path, index=index)
            # Karaoke (or anything else Plex-flavoured) is not part of the
            # Stage 3A scope -- same safe "not yet" messaging Stage 2 used
            # for all of Plex playback, now narrowed to just this case.
            if hasattr(self, "statusBar"):
                self.statusBar().showMessage(
                    "This Plex media type isn't supported yet.", 5000,
                )
            self.diagnostics.record(
                "plex", "plex_playback_not_yet_available",
                details={"media_type": plex_kind.value if plex_kind else "unknown"},
                minimum_level="basic",
            )
            return False
        if not os.path.isfile(path):
            self._audio_log(
                f"missing playlist track skipped; file={self._audio_name(path)!r}"
            )
            self.diagnostics.record(
                "playlist", "missing_entry_skipped",
                severity="warning",
                details=self.diagnostics.path_details(path),
                minimum_level="basic",
                rate_limit_seconds=2.0,
            )
            if hasattr(self, "statusBar"):
                self.statusBar().showMessage(
                    "Missing playlist track skipped", 5000
                )
            return False
        source_type = classify_path(path)
        if source_type == MediaType.KARAOKE and media_type_override is None:
            return self._play_karaoke_path_direct(path, index=index)
        if (
            self._current_media_type == MediaType.KARAOKE
            and media_type_override != MediaType.KARAOKE
        ):
            self._stop_karaoke_for_transition()
        if source_type == MediaType.VIDEO and media_type_override is None:
            return self._play_video_path_direct(path, index=index)
        # Reaching here means an audio path is about to play -- stop and
        # invalidate any current video before anything else, regardless of
        # which audio route (Cast or local) ends up handling this path.
        self._stop_video_for_audio_transition()
        if media_type_override is not None:
            self._current_media_type = media_type_override
        if getattr(self, "cast_active", False):
            return self._cast_play_path(path, index=index)
        self._playback_generation += 1
        transition_manager = getattr(self, "_video_transition_manager", None)
        if transition_manager is not None:
            transition_manager.media_changed()
        self._playback_recovery_active = False
        self._playback_recovery_attempts = {}
        self._playback_expected = True
        self._playback_intentionally_paused = False
        self._temporary_backend_override = None
        if self.builtin_backend == "bass" and self.bass_player:
            self._set_player_topology(self.bass_player, self.bass_inactive_player, reason="play_path_direct")
        elif self.miniaudio_player:
            self._set_player_topology(self.miniaudio_player, self.miniaudio_inactive_player, reason="play_path_direct")
        display_path = identity_path or path
        if index is None:
            index = self.track_index_by_path.get(display_path)
        if crossfade and self.use_simple and self.simple_player and self.simple_player.is_playing():
            if self._start_miniaudio_crossfade_to(path, immediate=immediate_crossfade, index=index):
                self.pending_next = False
                return True
            crossfade = False

        self._activate_track_ui(index, display_path)
        if self.use_simple and self.simple_player:
            # Built-in backend: either miniaudio or BASS, selected from the Library menu.
            self._simple_fallback_active = False
            if crossfade and self.simple_player and self.simple_player.is_playing():
                if not self._start_miniaudio_crossfade_to(path):
                    self._audio_log(f"backend={self._backend_label().lower()} fallback_to_vlc; file={self._audio_name(path)!r}")
                    self._simple_fallback_active = True
                    self._stop_all()
                    self._ensure_vlc()
                    if self.active_player:
                        if not self._play_on_player(self.active_player, path, volume_scale=1.0):
                            return self._begin_playback_recovery(
                                "open-failed", "vlc", path=path, position=0.0
                            )
                    else:
                        return False
            else:
                self._cancel_fade()
                self._stop_all()
                if not self._play_simple(path):
                    if not self.auto_playback_recovery:
                        self._simple_fallback_active = True
                        self._ensure_vlc()
                        if not self.active_player or not self._play_on_player(
                            self.active_player, path, volume_scale=1.0
                        ):
                            return False
                    else:
                        return self._begin_playback_recovery(
                            "open-failed",
                            self._backend_label().lower(),
                            path=path,
                            position=0.0,
                        )
            self.beat.setPlaying(True)
        else:
            self._ensure_vlc()
            if crossfade and self.active_player and self.active_player.is_playing():
                self._start_crossfade_to(path)
            else:
                self._cancel_fade()
                # Backend switches can leave the other engine playing, so silence both before VLC starts.
                self._stop_all()
                if not self._play_on_player(self.active_player, path, volume_scale=1.0):
                    return self._begin_playback_recovery(
                        "open-failed", "vlc", path=path, position=0.0
                    )
                self.beat.setPlaying(True)
        self.pending_next = False
        self._reset_progress()
        self._arm_playback_watchdog(0.0)
        return True

    # -- Stage 3A: Plex Direct Play (audio via BASS, video classic-only) -----
    def _plex_resolved_connection_for_identity(self, path: str):
        """(server_address, token, rating_key) for a plex:// identity, or
        None if it belongs to a server other than the one currently
        configured (a stale queue entry surviving a server change) or no
        connection is resolved yet -- callers must treat None as a clean
        "not ready" failure, never raise on it."""
        identity = parse_plex_identity(path)
        if identity is None:
            return None
        if identity.server_config_id != self.plex_preferences.server_config_id:
            return None
        server_address, token = self._plex_effective_connection()
        if not server_address or not token:
            return None
        return server_address, token, identity.rating_key

    def _offer_switch_to_bass_for_plex_audio(self) -> bool:
        """Plex audio Direct Play is BASS-only for Stage 3A: BASS's
        BASS_StreamCreateURL supports native HTTP(S) streaming with
        custom headers, miniaudio's Python binding has no URL-streaming
        capability at all, and downloading the whole remote file first
        isn't real streaming. Never switches the user's backend on its
        own -- only after an explicit Yes here, mirroring the existing
        menu-driven backend switch (_set_builtin_backend + use_simple +
        _save_user_settings)."""
        if not self.bass_player:
            self.statusBar().showMessage(
                "Plex audio currently requires the BASS audio backend.", 6000,
            )
            return False
        reply = QtWidgets.QMessageBox.question(
            self, "Plex Audio",
            "Plex audio currently requires the BASS audio backend.\n\n"
            "Switch to BASS now?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            self.statusBar().showMessage(
                "Plex audio currently requires the BASS audio backend.", 6000,
            )
            return False
        self._stop_all()
        if not self._set_builtin_backend("bass"):
            QtWidgets.QMessageBox.warning(self, "Audio Backend", "BASS is not available.")
            return False
        self.use_simple = True
        try:
            self.chk_simple.blockSignals(True)
            self.chk_simple.setChecked(self.use_simple and self.builtin_backend == "miniaudio")
        except Exception:
            pass
        finally:
            try:
                self.chk_simple.blockSignals(False)
            except Exception:
                pass
        self._audio_log("backend=bass enabled=True; source=plex_audio_switch_prompt")
        self._save_user_settings()
        return True

    def _start_plex_playback_resolve(self, path: str, media_kind: str, index: Optional[int]) -> bool:
        """Kicks off async Plex Direct Play resolution (server metadata
        fetch, DNS, capability check -- see PlexPlaybackResolveWorker) for
        one plex:// identity. Never blocks the GUI thread. Reuses
        _playback_generation as the staleness token: any later play
        action, Plex or local, bumps it and makes this resolve's eventual
        result a silent no-op (_on_plex_playback_resolved)."""
        # Real-device bug: the stall watchdog (_check_playback_health) and
        # the near-end/quiet-end crossfade triggers all read whatever the
        # PREVIOUS track's player/current_path state still is throughout
        # this entire async resolve -- a real network round-trip, and (for
        # audio via _play_plex_audio_path_direct) potentially a much
        # longer wait on a modal confirmation dialog before this is even
        # reached. Both respect pending_next as their own "an operation is
        # already in flight, don't second-guess it" guard; without this,
        # either could conclude the previous track stalled/quietly ended
        # and silently advance the queue out from under the Plex request
        # the user is still waiting on. Cleared on every terminal outcome
        # below (this function's own early failures, and eventually
        # _on_plex_playback_resolved's success/failure handlers).
        self.pending_next = True
        # Playback stability hardening, Phase A: captured now, before any
        # of this function's own async work begins, and threaded through
        # to _on_plex_playback_resolved below via the connect() lambda --
        # see playback_attempt.py. Additional defence alongside
        # _playback_generation, not a replacement for it.
        attempt = self._current_playback_attempt
        attempt_id = attempt.attempt_id if attempt is not None else None
        if getattr(self, "cast_active", False):
            # Stage 3C, not yet -- see Decision 5. Never route Plex
            # through LocalMediaServer as a workaround here.
            self.statusBar().showMessage(
                "Plex playback isn't available while Casting yet.", 6000,
            )
            self.pending_next = False
            return False
        connection = self._plex_resolved_connection_for_identity(path)
        if connection is None:
            self.statusBar().showMessage("Plex is not connected.", 6000)
            self.pending_next = False
            return False
        server_address, token, rating_key = connection
        self._playback_generation += 1
        generation = self._playback_generation
        self._plex_resolve_pending_kind = media_kind
        self._plex_resolve_pending_index = index
        self._advance_playback_attempt_state(attempt_id, PlaybackAttemptState.PREPARING)
        self.statusBar().showMessage("Resolving Plex media…")
        worker = PlexPlaybackResolveWorker(
            identity=path,
            server_config_id=self.plex_preferences.server_config_id,
            rating_key=rating_key,
            media_kind=media_kind,
            server_address=server_address,
            token=token,
            client_identifier=self.plex_preferences.client_identifier,
            generation=generation,
        )
        self._plex_resolve_worker = worker
        registry_token = self._worker_registry.register(
            "plex_resolve", thread=worker, wait_ms=2000,
        )
        worker.finished_result.connect(
            lambda result: self._on_plex_playback_resolved(result, attempt_id)
        )
        worker.finished.connect(
            lambda w=worker, t=registry_token: self._on_plex_resolve_worker_finished(w, t)
        )
        worker.start()
        return True

    def _on_plex_resolve_worker_finished(self, worker, registry_token) -> None:
        if self._plex_resolve_worker is worker:
            self._plex_resolve_worker = None
        self._worker_registry.unregister(registry_token)
        self._maybe_resume_final_shutdown()

    def _play_plex_audio_path_direct(self, path: str, index: Optional[int] = None) -> bool:
        if not self._use_bass_backend():
            # The confirmation dialog below runs Qt's own nested event
            # loop -- the stall watchdog/near-end triggers keep ticking
            # while it's up, still reading whatever track was current
            # before this request. Guard the wait itself, not just the
            # resolve that follows a Yes (see the matching comment in
            # _start_plex_playback_resolve) -- real-device bug: without
            # this, clicking Yes could find the queue had already silently
            # advanced past the very track being confirmed.
            self.pending_next = True
            if not self._offer_switch_to_bass_for_plex_audio():
                self.pending_next = False
                return False
        return self._start_plex_playback_resolve(path, "music", index)

    def _play_plex_video_path_direct(self, path: str, index: Optional[int] = None) -> bool:
        if not getattr(self, "video_playback_enabled", True):
            self.statusBar().showMessage(
                "Video playback is disabled in Preferences.", 5000
            )
            return False
        return self._start_plex_playback_resolve(path, "video", index)

    def _on_plex_playback_resolved(self, result: dict, attempt_id: Optional[int] = None):
        generation = result.get("generation")
        media_kind = self._plex_resolve_pending_kind
        index = self._plex_resolve_pending_index
        if not self._require_current_playback_attempt(attempt_id, "plex_resolve"):
            # Playback stability hardening, Phase A: second, independent
            # gate above the generation check right below -- a resolve
            # completing after Stop, or after a newer selection replaced
            # it, must never be allowed to start playback, change
            # current_path, or touch anything else authoritative,
            # regardless of what _playback_generation says. This gate
            # alone does not prove nothing native was mutated already --
            # see _require_current_playback_attempt's own docstring
            # (Phase C1 closes that gap, not this one).
            return
        if (
            getattr(self, "_closing", False)
            or generation is None
            or generation != self._playback_generation
        ):
            # Superseded by a later play action -- discard silently. This
            # is the same generation-ownership check that already exists
            # for _on_plex_audio_load_succeeded/_failed
            # (_plex_audio_load_token) -- the requirement (Stage 3A-r2,
            # item 9) is diagnostic visibility into it actually rejecting
            # something, not a new mechanism: without a real event trail,
            # a genuine stale-callback race like the recovery-watchdog one
            # (see _check_playback_health's Plex guard) would be invisible
            # until it silently misbehaved. Never used to suppress a
            # genuine, current-attempt EndOfMedia/stop -- only fires when
            # this callback's own generation has already been superseded.
            if not getattr(self, "_closing", False) and generation is not None:
                self.diagnostics.record(
                    "plex", "plex_playback_attempt_superseded",
                    details={
                        "stale_generation": generation,
                        "current_generation": self._playback_generation,
                        "stage": "resolve",
                    },
                    minimum_level="basic",
                )
            return
        if not result.get("success"):
            self._fail_plex_playback_resolve(result, attempt_id)
            return
        source = result.get("transport_source")
        self._advance_playback_attempt_state(attempt_id, PlaybackAttemptState.STARTING)
        if media_kind == "video":
            self._start_plex_video_playback(source, index, attempt_id=attempt_id)
        else:
            self._start_plex_audio_playback(source, index, attempt_id=attempt_id)

    def _fail_plex_playback_resolve(self, result: dict, attempt_id: Optional[int] = None):
        reason = sanitize_plex_text(result.get("reason") or "Plex playback failed")
        if result.get("direct_play_unavailable"):
            message = f"This Plex item cannot currently be Direct Played ({reason})."
        else:
            message = f"Plex playback failed: {reason}"
        self.statusBar().showMessage(message, 8000)
        self._audio_log(f"plex playback resolve failed; reason={reason}")
        # Nothing was left locked (no player swapped out, no watchdog
        # armed) -- the queue/Up Next remain exactly as usable as before
        # this attempt; a manual Next still works normally.
        self.pending_next = False
        self._advance_playback_attempt_state(attempt_id, PlaybackAttemptState.FAILED)
        self._terminalise_video_transition_for_failed_incoming("plex_resolve_failed")

    def _terminalise_video_transition_for_failed_incoming(self, reason: str) -> None:
        """A Phase 1 transition that reached its switch point while this
        (asynchronous) incoming media was still resolving is left covered in
        SWITCHING, waiting on a media_ready() that can now never arrive --
        the same stranded-cover class incoming_media_superseded() closes for
        a successful takeover. Terminalise it here instead.

        The incoming media never started, so nothing was swapped out: the
        video backend was not stopped and _current_media_type is still the
        outgoing VIDEO. What the stage shows next depends on why the
        transition ran:
          automatic -- the outgoing video genuinely reached its end, so
            restore the ordinary display exactly as a video ending with no
            successor does (_finish_video_end_without_next);
          manual / incoming-only -- the outgoing media is still viable
            underneath the cover, so only the cover is lifted.
        Never advances the queue again. A no-op when no covered transition
        is pending (see incoming_media_failed)."""
        transition_manager = getattr(self, "_video_transition_manager", None)
        if transition_manager is None:
            return
        try:
            trigger = transition_manager.incoming_media_failed(reason)
        except Exception:
            return
        if trigger == "automatic" and self._current_media_type == MediaType.VIDEO:
            PlayerWindow._finish_video_end_without_next(self)

    def _start_plex_audio_playback(
        self, source: PlexTransportSource, index: Optional[int],
        attempt_id: Optional[int] = None,
    ):
        self._stop_video_for_audio_transition()
        transition_manager = getattr(self, "_video_transition_manager", None)
        if transition_manager is not None:
            transition_manager.media_changed()
        self._playback_recovery_active = False
        self._playback_recovery_attempts = {}
        self._playback_expected = True
        self._playback_intentionally_paused = False
        self._temporary_backend_override = None
        # Plex audio is BASS-only regardless of the user's actual
        # builtin_backend preference (miniaudio, or bass reached only via
        # a recovery-driven temporary override) -- point the active slot
        # at BASS for this playback without ever writing builtin_backend
        # itself here. That preference is only ever changed with explicit
        # confirmation, in _offer_switch_to_bass_for_plex_audio.
        self._set_player_topology(self.bass_player, self.bass_inactive_player, reason="plex_audio_playback")
        if index is None:
            index = self.track_index_by_path.get(source.identity)
        self._cancel_fade()
        self._stop_all()
        self._activate_track_ui(index, source.identity)
        self._simple_fallback_active = False
        self._plex_audio_load_token += 1
        token = self._plex_audio_load_token
        # Phase C1: prime BASS's one-time init on the GUI thread before the
        # first BASS candidate-preparation worker could possibly race it --
        # a no-op once already warmed (see _BassEngine.ensure()). Only done
        # on BASS preparation paths, never unconditionally, so a
        # miniaudio-only user never pays for it.
        if _BassEngine is not None:
            try:
                _BassEngine.ensure()
            except Exception:
                pass  # surfaced instead via the worker's own `failed` signal below
        lease = self._make_target_lease("bass", "active")
        worker = BassStreamPrepareWorker(source, token)
        self._plex_audio_load_worker = worker
        registry_token = self._worker_registry.register(
            "plex_audio_load", thread=worker, wait_ms=2000,
            finalize_after_join=lambda w=worker: self._finalize_unclaimed_prepare_candidate(w, "plex_audio_load_join"),
        )
        worker.prepared.connect(
            lambda tok, ident, _candidate, w=worker: self._on_plex_audio_load_prepared(
                tok, ident, w.claim_candidate(), attempt_id, lease,
            )
        )
        worker.failed.connect(
            lambda tok, ident, err: self._on_plex_audio_load_failed(tok, ident, err, attempt_id)
        )
        worker.finished.connect(
            lambda w=worker, t=registry_token: self._on_plex_audio_load_worker_finished(w, t)
        )
        worker.start()

    def _on_plex_audio_load_worker_finished(self, worker, registry_token) -> None:
        if self._closing:
            self._finalize_unclaimed_prepare_candidate(worker, "shutdown_worker_finished")
        if self._plex_audio_load_worker is worker:
            self._plex_audio_load_worker = None
        self._worker_registry.unregister(registry_token)
        self._maybe_resume_final_shutdown()

    def _on_plex_audio_load_prepared(
        self, token: int, identity: str, candidate, attempt_id: Optional[int] = None,
        lease: Optional[PlayerTargetLease] = None,
    ):
        """Phase C1: replaces the old _on_plex_audio_load_succeeded, which
        assumed the worker had already mutated a live BassPlayer by the
        time this callback ran. `candidate` is a private PreparedBassStream
        that has not touched any BassPlayer -- every rejection branch below
        discards it (freeing only its own HSTREAM); only the final,
        fully-validated branch commits it to the specific physical player
        `lease` was captured for.

        Phase C2: `candidate` arrives via worker.claim_candidate() at the
        dispatch-time connect() lambda, not the raw signal argument --
        None here means the shutdown finalizer (or this worker's own
        `finished` handler, if it fired first) already claimed and
        resolved it. Nothing left to do."""
        if candidate is None:
            return
        if not self._require_current_playback_attempt(attempt_id, "plex_audio_load"):
            # Phase A: a load completing after Stop, or after a newer
            # selection replaced this attempt, must never start playback
            # or make this identity current -- regardless of the token
            # check right below.
            #
            # Phase C1 (2026-09-10): `candidate` never touched any
            # BassPlayer -- discarding it here frees only its own
            # HSTREAM. There is no shared-object risk left to reason
            # about, unlike the old design this replaces (see
            # bass_player.PreparedBassStream).
            self._discard_prepared_candidate(candidate, "plex_audio_load")
            return
        if getattr(self, "_closing", False):
            # Real shutdown: the candidate was never committed to
            # anything, so discarding it is all that's needed --
            # _finalize_shutdown()'s own player.close() loop handles the
            # actual live players.
            self._discard_prepared_candidate(candidate, "plex_audio_load")
            return
        if token != self._plex_audio_load_token:
            # Superseded (stopped, or another track change) while this
            # load was in flight. See the matching diagnostic comment in
            # _on_plex_playback_resolved -- same ownership-token pattern,
            # one stage later.
            self.diagnostics.record(
                "plex", "plex_playback_attempt_superseded",
                details={
                    "stale_token": token,
                    "current_token": self._plex_audio_load_token,
                    "stage": "audio_load_succeeded",
                },
                minimum_level="basic",
            )
            self._discard_prepared_candidate(candidate, "plex_audio_load")
            return
        if lease is None or not self._target_lease_still_valid(lease):
            self.diagnostics.record(
                "playback", "player_target_lease_invalid",
                details={"stage": "plex_audio_load"},
                minimum_level="basic",
            )
            self._discard_prepared_candidate(candidate, "plex_audio_load")
            return
        player = lease.physical_object
        try:
            if not player.commit_prepared(candidate):
                # Already resolved by someone else -- shouldn't happen
                # given every check above (single GUI thread resolves a
                # given candidate), but not assumed.
                return
            self._active_normalisation_gain = self._cached_gain_for_path(identity)
            player.set_volume(
                combine_volume(
                    self.master_volume / 100.0,
                    self._active_normalisation_gain,
                    self._sleep_timer_gain,
                )
            )
            player.play()
            self._set_playing_button_state()
            stats = player.stats()
            self._audio_log(
                f"backend=bass load ok; file={self._audio_name(identity)!r}; "
                f"duration={stats['duration']:.2f}s; rate={stats['sample_rate']}; "
                f"channels={stats['channels']}; source=plex"
            )
            self._audio_log(f"backend=bass play start; file={self._audio_name(identity)!r}; source=plex")
        except Exception as ex:
            self._audio_log(f"backend=bass play failed after load; file={self._audio_name(identity)!r}; error={ex}; source=plex")
            try:
                player.stop()
            except Exception:
                pass
            self.statusBar().showMessage("Plex audio failed to play.", 6000)
            self.pending_next = False
            return
        self.beat.setPlaying(True)
        self.pending_next = False
        self._reset_progress()
        self._arm_playback_watchdog(0.0)
        self._advance_playback_attempt_state(attempt_id, PlaybackAttemptState.PLAYING)

    def _on_plex_audio_load_failed(
        self, token: int, identity: str, error: str, attempt_id: Optional[int] = None,
    ):
        if not self._require_current_playback_attempt(attempt_id, "plex_audio_load_failed"):
            return
        if getattr(self, "_closing", False) or token != self._plex_audio_load_token:
            if not getattr(self, "_closing", False):
                self.diagnostics.record(
                    "plex", "plex_playback_attempt_superseded",
                    details={
                        "stale_token": token,
                        "current_token": self._plex_audio_load_token,
                        "stage": "audio_load_failed",
                    },
                    minimum_level="basic",
                )
            return
        self._audio_log(f"backend=bass load failed; file={self._audio_name(identity)!r}; error={error}; source=plex")
        self.statusBar().showMessage("Plex audio failed to load.", 6000)
        self.pending_next = False
        self._advance_playback_attempt_state(attempt_id, PlaybackAttemptState.FAILED)

    def _start_plex_video_playback(
        self, source: PlexTransportSource, index: Optional[int],
        attempt_id: Optional[int] = None,
    ):
        # Playback stability hardening, Phase A: the attempt is accepted
        # and advanced here (the subprocess IPC load call itself is
        # fire-and-forget, with its own generation/token handled by
        # VideoBackend -- wiring attempt authority into that subprocess
        # boundary is Phase C/D territory, not this round's scope).
        self._advance_playback_attempt_state(attempt_id, PlaybackAttemptState.PLAYING)
        record_bass_device_state = getattr(self, "_record_bass_device_state_once", None)
        if record_bass_device_state is not None:
            record_bass_device_state()
        previous_media_type = getattr(self, "_current_media_type", MediaType.AUDIO)
        self._cancel_fade()
        self._stop_all()
        try:
            self._cancel_playback_watchdog()
        except Exception:
            pass
        transition_manager = getattr(self, "_video_transition_manager", None)
        if transition_manager is not None:
            transition_manager.begin_incoming_only(previous_media_type, MediaType.VIDEO)
        self._current_media_type = MediaType.VIDEO
        if transition_manager is not None:
            transition_manager.media_changed()
        self._playback_recovery_active = False
        self._playback_recovery_attempts = {}
        self._playback_expected = True
        self._playback_intentionally_paused = False
        self._video_progress_started_at = time.monotonic()
        self._video_progress_warning_reported = False
        self._video_timing_available_reported = False
        if index is None:
            index = self.track_index_by_path.get(source.identity)
        self._activate_track_ui(index, source.identity)
        self._show_video_loading_page()
        self.diagnostics.record(
            "playback", "video_load_started",
            details=self.diagnostics.path_details(source.identity),
            minimum_level="detailed",
        )
        self._video_backend.set_volume(self.master_volume)
        self._video_backend.set_muted(self._muted)
        if self._video_backend.is_dual_mode():
            # Stage 3A: Plex video is classic-only -- never let a plex://
            # identity or its tokenised transport URL reach the GPU
            # dual-deck preload system (that's Stage 3B). This is a real
            # subprocess-mode switch (see VideoBackend.set_dual_mode's own
            # docstring on the restart it performs), so it's only done
            # for the duration of Plex video playback; the next LOCAL
            # video activation re-evaluates and restores GPU mode via
            # _apply_gpu_dual_mode_state().
            self._video_backend.set_dual_mode(None)
            self.diagnostics.record(
                "playback", "plex_video_gpu_deferred",
                details={"reason": "stage3a_classic_only"},
                minimum_level="basic",
            )
        started = self._video_backend.load(source.identity, transport_url=source.transport_url)
        self.pending_next = False
        if started:
            self._reset_progress()
            self.label_remaining.setText("Loading…")
        elif self._current_media_type == MediaType.VIDEO:
            self._on_video_error(
                "video_subprocess_error",
                "The video process was not ready to accept playback.",
            )

    def _force_local_output_for_video(self):
        """Stop Cast safely and switch to This Computer -- called once when
        a video becomes current while Cast output is active. Unlike
        _return_to_local_output, this never resumes playback locally: a
        video is about to start immediately after this returns."""
        self._cast_completion_armed = False
        try:
            self.cast_controller.stop()
            self.cast_controller.disconnect()
            self.cast_media_server.shutdown()
        except Exception:
            pass
        self.cast_active = False
        self._cast_pending_device = None
        try:
            for i in range(self.output_combo.count()):
                if self.output_combo.itemData(i) is None:
                    self.output_combo.setCurrentIndex(i)
                    break
        except Exception:
            pass
        self.statusBar().showMessage("Videos play on This Computer", 4000)
        self.diagnostics.record(
            "playback", "video_forced_local_output", minimum_level="detailed",
        )

    def _record_bass_device_state_once(self) -> None:
        """Classic-audio-silence investigation (2026-08-31 Codex audit,
        section 7): "does Qt's classic video child use a different Windows
        audio endpoint than BASS" -- logs BASS's actual selected device
        hash exactly once per session (bounded; BASS's own device
        selection never changes after BASS_Init), for direct comparison
        against video_classic_audio_state's own device_hash/
        default_output_device_hash fields in the same session's
        diagnostics. A no-op if BASS was never loaded/initialised (nothing
        to report yet -- not an error)."""
        if getattr(self, "_bass_device_state_recorded", False):
            return
        if bass_device_hash is None:
            return
        device_hash = bass_device_hash()
        if not device_hash:
            return  # BASS not initialised yet -- try again next call
        self._bass_device_state_recorded = True
        self.diagnostics.record(
            "playback", "bass_device_state",
            details={"device_hash": device_hash},
            minimum_level="basic",
        )

    def _play_video_path_direct(self, path: str, index: Optional[int] = None) -> bool:
        """Route a video path to the Qt Multimedia backend. Never starts
        VLC/miniaudio/BASS and never arms the audio recovery watchdog."""
        analyze_outro = getattr(self, "_maybe_analyze_video_outro", None)
        if analyze_outro is not None:
            analyze_outro(path)
        already_current_video = (
            bool(path)
            and path == getattr(self, "current_path", None)
            and getattr(self, "_current_media_type", MediaType.AUDIO) == MediaType.VIDEO
        )
        if path and (
            path == getattr(self, "_dual_transition_promoted_path", None)
            or already_current_video
        ):
            # Real-device bug: a redundant re-activation of the exact
            # video already playing (e.g. re-clicking/re-activating it in
            # the library or queue while it's current) reached the normal
            # reload path below, ~18s after a GPU dual-transition had
            # promoted it -- the one-shot _dual_transition_promoted_path
            # guard only protects the *first* activation post-promotion,
            # not later redundant ones. That issued a fresh classic-
            # backend load() for content the GPU compositor was already
            # actively rendering, exactly the failure
            # _promote_dual_transition_track_ui's own docstring warns
            # about ("would interrupt the cross-dissolve already running
            # on screen") -- confirmed via diagnostics: video_started
            # never fired for that reload, leaving the video output page
            # stuck on "Loading video..." (audio unaffected, a separate
            # pipeline) until the next natural transition. Any redundant
            # reactivation of the currently-playing video -- promoted or
            # not -- gets the same safe treatment: refresh the UI/
            # bookkeeping only, never touch the video backend.
            self._dual_transition_promoted_path = None
            return self._promote_dual_transition_track_ui(
                path, index,
                reason="redundant_reactivation" if already_current_video else "dual_transition_promotion",
            )
        if not getattr(self, "video_playback_enabled", True):
            self.statusBar().showMessage(
                "Video playback is disabled in Preferences.", 5000
            )
            return False
        # A prior Plex video (Stage 3A is classic-only) may have forced
        # GPU dual mode off for its own duration -- a genuine new local
        # video load is exactly the point this should be re-evaluated and
        # restored if the user's actual preference still wants it. A
        # no-op (same restart-only-on-change guard as every other caller
        # of this) when nothing needs to change.
        apply_gpu_state = getattr(self, "_apply_gpu_dual_mode_state", None)
        if apply_gpu_state is not None:
            apply_gpu_state()
        record_bass_device_state = getattr(self, "_record_bass_device_state_once", None)
        if record_bass_device_state is not None:
            record_bass_device_state()
        if getattr(self, "cast_active", False):
            self._force_local_output_for_video()
        previous_media_type = getattr(self, "_current_media_type", MediaType.AUDIO)
        # Cancel any pending audio crossfade/prebuffer and stop the current
        # audio backend safely before handing off to the video backend.
        self._cancel_fade()
        self._stop_all()
        try:
            self._cancel_playback_watchdog()
        except Exception:
            pass
        transition_manager = getattr(self, "_video_transition_manager", None)
        if transition_manager is not None:
            transition_manager.begin_incoming_only(
                previous_media_type, MediaType.VIDEO,
            )
        self._current_media_type = MediaType.VIDEO
        self._playback_generation += 1
        if transition_manager is not None:
            transition_manager.media_changed()
        self._playback_recovery_active = False
        self._playback_recovery_attempts = {}
        self._playback_expected = True
        self._playback_intentionally_paused = False
        self._video_progress_started_at = time.monotonic()
        self._video_progress_warning_reported = False
        self._video_timing_available_reported = False
        if index is None:
            index = self.track_index_by_path.get(path)
        self._activate_track_ui(index, path)
        self._show_video_loading_page()
        self.diagnostics.record(
            "playback", "video_load_started",
            details=self.diagnostics.path_details(path),
            minimum_level="detailed",
        )
        # Every other backend (_play_simple's BASS/miniaudio, the VLC path)
        # explicitly syncs volume/mute on every track load rather than
        # relying on whatever the destination backend happened to default
        # to -- the video backend's own QAudioOutput was the one exception,
        # silently staying at its own construction-time defaults (or a
        # stale value from before a subprocess restart) instead of the
        # user's actual current volume/mute state, reported live as no
        # audio on videos.
        self._video_backend.set_volume(self.master_volume)
        self._video_backend.set_muted(self._muted)
        _dual_state_check = getattr(self, "_dual_transition_committed_state_value", None)
        dual_state = _dual_state_check() if _dual_state_check is not None else None
        if dual_state is not None:
            # Canary for the exact bug class fixed in video_transition.py's
            # handle_natural_end()/request_manual_next(): a classic load()
            # call reaching here while a GPU cross-dissolve is already
            # committed would tear down the presentation surface the GPU
            # transition still owns. Should never happen post-fix -- this
            # is a permanent regression detector, not a per-frame log.
            self.diagnostics.record(
                "playback", "video_load_during_committed_dual_transition",
                severity="warning",
                details={
                    "dual_transition_committed_state": dual_state,
                    **self.diagnostics.path_details(path),
                },
                minimum_level="basic",
            )
        started = self._video_backend.load(path)
        self.pending_next = False
        if started:
            self._reset_progress()
            self.label_remaining.setText("Loading…")
        elif self._current_media_type == MediaType.VIDEO:
            # Missing files emit their own synchronous error; this covers a
            # dead/not-ready subprocess that simply could not accept load().
            self._on_video_error(
                "video_subprocess_error",
                "The video process was not ready to accept playback.",
            )
        return started

    # -- Party Mode video takeover --------------------------------------------
    def _route_video_output(self, target: str) -> bool:
        """Attach the one subprocess-owned video surface to a named stage."""
        if target == "main_window":
            host = self.video_output_widget
        elif target == "party_mode":
            party_mode = getattr(self, "party_mode", None)
            host = getattr(party_mode, "video_widget", None)
        else:
            host = None
        if host is None:
            return False
        self._video_backend.attach_output(host)
        self._video_backend.schedule_output_geometry_sync()
        self.diagnostics.record(
            "playback", "video_output_moved",
            details={"target": target}, minimum_level="basic",
        )
        return True

    def _play_karaoke_path_direct(self, source_path: str, index: Optional[int] = None) -> bool:
        if self._current_media_type == MediaType.VIDEO:
            self._stop_video_for_audio_transition()
        self._karaoke_generation += 1
        generation = self._karaoke_generation
        self._cancel_fade()
        self._stop_all()
        try:
            previous_worker = getattr(self, "_karaoke_prepare_worker", None)
            if previous_worker is not None:
                previous_worker.cancel()
        except Exception:
            pass
        if getattr(self, "cast_active", False):
            self._force_local_output_for_video()
            self.statusBar().showMessage(
                "Karaoke plays on This Computer to keep lyrics synchronised.", 6000
            )
            self.diagnostics.record("playback", "karaoke_forced_local_output")
        self._current_media_type = MediaType.KARAOKE
        self._karaoke_audio_path = None
        self._karaoke_document = None
        self.karaoke_widget.clear()
        self.right_display_stack.setCurrentWidget(self._karaoke_output_page)
        self._refresh_visualiser_lifecycle("karaoke_shown")
        self._activate_track_ui(index, source_path)
        self.diagnostics.record(
            "playback", "karaoke_prepare_started",
            details=self.diagnostics.path_details(source_path),
        )
        # Playback stability hardening, Phase A: captured now, threaded
        # through to both callbacks below via the connect() lambdas --
        # additional defence alongside the existing _karaoke_generation
        # check, same pattern as the Plex resolve/load wiring above.
        attempt = self._current_playback_attempt
        attempt_id = attempt.attempt_id if attempt is not None else None
        self._advance_playback_attempt_state(attempt_id, PlaybackAttemptState.PREPARING)
        worker = KaraokePrepareWorker(generation, source_path)
        self._karaoke_prepare_worker = worker
        registry_token = self._worker_registry.register(
            "karaoke_prepare", cancel=worker.cancel, thread=worker, wait_ms=5000,
        )
        worker.prepared.connect(
            lambda gen, path, pair, doc: self._on_karaoke_prepared(gen, path, pair, doc, attempt_id)
        )
        worker.failed.connect(
            lambda gen, path, message: self._on_karaoke_prepare_failed(gen, path, message, attempt_id)
        )
        worker.finished.connect(
            lambda w=worker, t=registry_token: self._release_karaoke_worker(w, t)
        )
        worker.start()
        return True

    def _stop_karaoke_for_transition(self):
        if self._current_media_type != MediaType.KARAOKE:
            return
        self._karaoke_generation += 1
        self._exit_karaoke_fullscreen()
        self.karaoke_widget.clear()
        self._karaoke_audio_path = None
        self._karaoke_document = None
        if self.party_mode is not None and getattr(self.party_mode, "_video_active", False):
            self.party_mode.return_to_normal_layout()
        self._show_normal_display_page()
        self._current_media_type = MediaType.AUDIO

    def _release_karaoke_worker(self, worker, registry_token=None):
        if self._karaoke_prepare_worker is worker:
            self._karaoke_prepare_worker = None
        worker.deleteLater()
        self._worker_registry.unregister(registry_token)
        self._maybe_resume_final_shutdown()

    def _on_karaoke_prepared(self, generation, source_path, pair, document, attempt_id=None):
        if not self._require_current_playback_attempt(attempt_id, "karaoke_prepared"):
            return
        if generation != self._karaoke_generation or self.current_path != source_path:
            return
        self._karaoke_audio_path = pair.audio_path
        self._karaoke_document = document
        self.karaoke_widget.set_document(document)
        if self.party_mode is not None and self.party_mode.isVisible():
            self.party_mode.show_karaoke(document)
        if getattr(document, "unrecognized_packet_count", 0) > 0:
            # One aggregate call per prepared document, never per-packet.
            self.diagnostics.record(
                "playback", "cdg_parse_warning", severity="warning",
                details={
                    "unrecognized_packets": document.unrecognized_packet_count,
                    "total_packets": len(document.packets),
                    **self.diagnostics.path_details(source_path),
                },
                minimum_level="basic",
            )
        started = self._play_path_direct(
            pair.audio_path, crossfade=False,
            index=self.track_index_by_path.get(source_path),
            identity_path=source_path, media_type_override=MediaType.KARAOKE,
        )
        if started:
            if self.waveform_seekbar is not None:
                self.waveform_seekbar.set_placeholder(source_path)
                if self.waveform_worker is not None:
                    self.waveform_worker.request(pair.audio_path)
            self.diagnostics.record(
                "playback", "karaoke_started",
                details=self.diagnostics.path_details(source_path),
            )
        else:
            self._on_karaoke_prepare_failed(
                generation, source_path, "Backing MP3 could not be played", attempt_id,
            )

    def _on_karaoke_prepare_failed(self, generation, source_path, message, attempt_id=None):
        if not self._require_current_playback_attempt(attempt_id, "karaoke_prepare_failed"):
            return
        if generation != self._karaoke_generation:
            return
        self._playback_expected = False
        self._current_media_type = MediaType.AUDIO
        self.karaoke_widget.clear()
        self._show_normal_display_page()
        self._sync_now_playing_overlay_for_media_type()
        self.statusBar().showMessage(f"Karaoke could not start: {message}", 8000)
        self.diagnostics.record(
            "playback", "karaoke_failed", severity="warning",
            details={"message": str(message), **self.diagnostics.path_details(source_path)},
        )
        self._advance_playback_attempt_state(attempt_id, PlaybackAttemptState.FAILED)

    def _toggle_karaoke_fullscreen(self):
        # Karaoke fullscreen shares _video_fullscreen/_enter_video_fullscreen/
        # _exit_video_fullscreen with video (both gate on
        # _current_media_type below) rather than reparenting karaoke_widget
        # into its own top-level window and back: reparenting a widget whose
        # window flags change while it's still visible is a known Qt/Windows
        # hazard (the widget's native surface can come back blank instead of
        # repainting), and this exact "expand the existing stage, never move
        # the widget" approach was already adopted for video fullscreen for
        # the same reason (see _enter_main_video_fullscreen_presentation).
        # Goes through the shared request seam (_toggle_video_fullscreen is
        # a request now), so karaoke input cannot be a second independent
        # synchronous caller of the native restore path.
        self._toggle_video_fullscreen()

    def _exit_karaoke_fullscreen(self):
        # Deliberately synchronous: the remaining callers are INTERNAL
        # lifecycle paths (stop_playback, _stop_karaoke_for_transition)
        # that clear karaoke state immediately afterwards and must not
        # depend on a queued turn. Karaoke's Escape/double-click INPUT
        # goes through the request seam instead (see
        # _on_karaoke_escape_pressed / _on_karaoke_double_clicked); the
        # re-entrancy guard inside _exit_video_fullscreen protects this
        # path too.
        self._exit_video_fullscreen()

    def _attach_video_to_party_mode(self):
        """Move the shared video backend's output onto Party Mode's video
        page, if a video is current and Party Mode is visible. Same player,
        same generation -- only the output widget moves. Called both when a
        video starts while Party Mode is already open, and when Party Mode
        opens while a video is already playing."""
        if self._current_media_type == MediaType.KARAOKE:
            party_mode = getattr(self, "party_mode", None)
            if (
                party_mode is not None and party_mode.isVisible()
                and self._karaoke_document is not None
            ):
                party_mode.show_karaoke(self._karaoke_document)
            return
        if self._current_media_type != MediaType.VIDEO:
            return
        party_mode = getattr(self, "party_mode", None)
        if party_mode is None or not party_mode.isVisible():
            return

        if getattr(self, "_video_fullscreen", False):
            if getattr(self, "_video_fullscreen_owner_widget", None) is party_mode:
                # A new video has started on the Party Mode stage that is
                # already fullscreen. Keep that presentation and, crucially,
                # do not detach/reparent the native child window between two
                # consecutive videos.
                if not getattr(party_mode, "_video_active", False):
                    party_mode.show_video()
                return
            # Moving a cross-process native window while its current host is
            # fullscreen is unreliable on Windows. Restore that host first,
            # put Party Mode on its video page immediately, then let Qt finish
            # the window-state change before reattaching the native surface.
            self._exit_video_fullscreen()
            party_mode.show_video()
            QtCore.QTimer.singleShot(
                0,
                lambda party=party_mode: self._finish_party_video_attach(party),
            )
            return

        self._finish_party_video_attach(party_mode)

    def _finish_party_video_attach(self, party_mode):
        """Complete a Party Mode video hand-off after window-state changes."""
        if self._current_media_type != MediaType.VIDEO:
            return
        if party_mode is not getattr(self, "party_mode", None):
            return
        if not party_mode.isVisible() or getattr(self, "_video_fullscreen", False):
            return
        self._route_video_output("party_mode")
        if not getattr(party_mode, "_video_active", False):
            party_mode.show_video()

    def _detach_video_from_party_mode(self):
        """Reattach the shared video backend's output back to the main
        window's video page and restore Party Mode's previously-selected
        layout. Called when Party Mode hides while showing video, or when
        video itself ends/stops/errors while Party Mode was showing it."""
        party_mode = getattr(self, "party_mode", None)
        if self._current_media_type == MediaType.KARAOKE:
            if party_mode is not None and getattr(party_mode, "_video_active", False):
                party_mode.return_to_normal_layout()
            return
        if getattr(self, "_video_fullscreen", False):
            self._exit_video_fullscreen()
        self._route_video_output("main_window")
        if party_mode is not None and getattr(party_mode, "_video_active", False):
            party_mode.return_to_normal_layout()

    def _stop_video_for_audio_transition(self):
        """Every switch from video to audio playback goes through here
        first, regardless of which audio route (Cast or local) ends up
        handling the new path -- stops the child process's decode/
        playback and invalidates its generation token (via stop()) so no
        late event from the just-abandoned video acts on anything, then
        resumes any BPM/key analysis that was deferred while video was
        current. Safe to call even when nothing was playing.

        _current_media_type flips to AUDIO before any of the teardown
        steps below, not after: _show_normal_display_page() re-evaluates
        the visualiser lifecycle immediately (_refresh_visualiser_
        lifecycle("video_hidden")), and that evaluation's panel_visible
        check requires _current_media_type == AUDIO. Flipping it last (as
        this used to) made that one evaluation see the stale VIDEO type,
        conclude the visualiser should stay suspended, and left it stuck
        that way -- nothing else in the ordinary playback path re-checks
        the lifecycle afterward, so the main visualiser stayed suspended
        for the rest of the session after any video played, even once a
        normal audio track was current again."""
        was_video = self._current_media_type == MediaType.VIDEO
        self._current_media_type = MediaType.AUDIO
        if was_video:
            if getattr(self, "_video_fullscreen", False):
                self._exit_video_fullscreen()
            self._video_backend.stop()
            self._detach_video_from_party_mode()
            self._show_normal_display_page()
        # Audio now owns the stage. If a Phase 1 transition cover is still up
        # waiting for an incoming VIDEO -- the case for an asynchronous audio
        # takeover such as Plex, whose media type was still VIDEO when the
        # switch point sampled it -- lift it now, AFTER the page switch
        # above, so the reveal shows the visualiser rather than the stopped
        # video. Previously nothing did, and the cover stayed up over the
        # stage indefinitely (real-device session e622155c, 12:03:33).
        # A no-op for every other state (see incoming_media_superseded).
        transition_manager = getattr(self, "_video_transition_manager", None)
        if transition_manager is not None:
            try:
                transition_manager.incoming_media_superseded(self._current_media_type)
            except Exception:
                pass
        self._resume_deferred_queue_analysis()

    # -- v1.0.71 mixed-media (Audio<->Video) transitions ---------------------
    # A small, self-contained preparation/overlap/commit lifecycle that sits
    # alongside the existing A-A crossfade engine and the video-video
    # VideoTransitionManager/GPU dual-deck engine, touching neither. See
    # CODEX_HANDOFF.md's v1.0.71 section for the full design rationale.
    #
    # State machine: idle -> preparing -> active -> idle (via a "finish"
    # method) or idle (via _cancel_mixed_media_transition /
    # _abandon_mixed_media_transition_and_fallback at any point).
    #
    # Audio->Video keeps _current_media_type == AUDIO through "preparing"
    # (the incoming video is loaded muted, off the visible display stack --
    # nothing in the existing video-video machinery is touched) and flips to
    # VIDEO the moment "active" begins, matching how a direct video
    # activation already orders things (_play_video_path_direct). This is
    # safe because the outgoing audio's own fade is driven by fade_timer,
    # not by _tick()'s media-type-gated near-end/quiet-end polling.
    #
    # Video->Audio instead keeps _current_media_type == VIDEO through BOTH
    # "preparing" and "active" -- only flipping to AUDIO at the very end,
    # inside _finish_mixed_transition_video_to_audio -- because the video
    # backend's own natural-end/position/error signal handlers all gate on
    # _current_media_type == VIDEO, and must keep working normally for the
    # whole overlap (the video is still visibly playing throughout).

    def _next_mixed_transition_id(self) -> int:
        self._mixed_transition_id += 1
        return self._mixed_transition_id

    def _reset_mixed_media_transition_state(self) -> None:
        self._mixed_transition_state = "idle"
        self._mixed_transition_direction = None
        self._mixed_transition_outgoing_path = None
        self._mixed_transition_incoming_path = None
        self._mixed_transition_incoming_row = None
        self._mixed_transition_reason = None
        self._mixed_transition_start = None
        self._mixed_transition_video_audio_scale = 0.0
        self._mixed_transition_gain_token = None
        self._mixed_transition_requested_monotonic = None

    def _mixed_media_transition_eligible(self, next_path: str) -> Optional[str]:
        """Return "audio_to_video" / "video_to_audio" if the switch from
        the current media to next_path should attempt a mixed-media
        transition, else None. Only ever active for the user's crossfade
        preference (mirrors _crossfade_eligible_for_transition's own
        gate) -- "normal" transition mode keeps today's hard-cut behaviour
        for mixed media, same as it already does for audio-audio.
        Karaoke is excluded on either side, deliberately unchanged in
        v1.0.71 (classify_path never returns KARAOKE for the *current*
        side via _current_media_type -- karaoke tracks play through the
        ordinary audio path with _current_media_type == KARAOKE, which
        this checks for explicitly rather than relying on classify_path).
        """
        if self.track_transition_mode != "crossfade":
            return None
        # Stage 3A: neither side of a mixed-media crossfade understands a
        # Plex identity -- the video-side load call and the audio-side
        # candidate-preparation worker here both only ever pass a plain
        # path/string through to local-file APIs (QUrl.fromLocalFile, BASS_StreamCreateFile),
        # never a resolved PlexTransportSource. A clean hard switch (this
        # returning None falls through to the ordinary _play_path_direct
        # call, which *does* know how to resolve Plex) is the explicitly
        # accepted Stage 3A boundary -- same treatment Karaoke already
        # gets on either side.
        if is_plex_identity(next_path) or is_plex_identity(self.current_path):
            return None
        current = self._current_media_type
        next_type = classify_path(next_path)
        if current == MediaType.AUDIO and next_type == MediaType.VIDEO:
            return "audio_to_video"
        if current == MediaType.VIDEO and next_type == MediaType.AUDIO:
            return "video_to_audio"
        return None

    def _probe_post_completion_video_audio_state(self, transition_id: int) -> None:
        """The ninth, delayed checkpoint from _finish_mixed_transition_
        audio_to_video: a real "did it stay right" check a moment after
        settling, not just immediately at commit. Skipped once anything
        has moved on (a newer transition, or video no longer current) --
        an unconditional query here would attach a misleading label to
        whatever the video subprocess happens to be doing by then, which
        is worse than no data point at all."""
        if self._current_media_type != MediaType.VIDEO:
            return
        if self._mixed_transition_id != transition_id or self._mixed_transition_state != "idle":
            return
        try:
            self._video_backend.query_audio_state("post_completion_steady_state", transition_id)
        except Exception:
            pass

    def _record_mixed_transition_gap_checkpoint(self, checkpoint: str) -> None:
        """Long audio->video gap investigation (2026-08-28): the captured
        incident window didn't contain enough of the beginning of an
        Audio->Video transition to prove where a noticeable gap came from
        -- load, buffering, first-frame readiness, visibility/embed
        switching, waiting for playback to start, the outgoing audio's own
        fade, or transition commit. Bounded, checkpoint-only (never per-
        frame): one record per named milestone, each carrying elapsed_ms
        relative to the original mixed_transition_requested moment (see
        _begin_mixed_media_transition), so the next real reproduction can
        answer that directly instead of guessing. Audio->Video only, since
        that's the direction with the reported gap; a mid-fade timing hole
        here (e.g. after a Manual Next cancelled the transition) skips
        silently rather than recording a meaningless/negative elapsed_ms."""
        if self._mixed_transition_direction != "audio_to_video":
            return
        started_at = self._mixed_transition_requested_monotonic
        if started_at is None:
            return
        self.diagnostics.record(
            "playback", checkpoint,
            details={
                "transition_id": self._mixed_transition_id,
                "elapsed_ms": round((time.monotonic() - started_at) * 1000.0, 1),
            },
            minimum_level="detailed",
        )

    def _probe_mixed_video_audio_state(self, checkpoint: str) -> None:
        """v1.0.71 correction: request the on-demand mixed_video_audio_state
        diagnostic (see _on_video_audio_state_reported) at one of the fixed
        checkpoints real acceptance testing needs to tell "never sent",
        "sent but muted/zero", "sent to the wrong deck", and "sent
        correctly and audible" apart -- something no existing diagnostic
        could distinguish. Fire-and-forget: the report (if any) arrives
        asynchronously via the video subprocess's IPC channel."""
        try:
            self._video_backend.query_audio_state(checkpoint, self._mixed_transition_id)
        except Exception:
            pass

    def _on_video_audio_state_reported(self, payload: Dict[str, Any]) -> None:
        self.diagnostics.record(
            "playback", "mixed_video_audio_state",
            details={
                "checkpoint": payload.get("checkpoint"),
                "transition_id": payload.get("transition_id"),
                "primary_index": payload.get("primary_index"),
                "secondary_index": payload.get("secondary_index"),
                "deck_source_hash": payload.get("deck_source_hash"),
                "backend_volume": payload.get("volume"),
                "backend_muted": payload.get("muted"),
                "playback_state": payload.get("playback_state"),
                "crossfade_active": payload.get("crossfade_active"),
                "requested_video_audio_scale": self._mixed_transition_video_audio_scale,
                "master_volume": self.master_volume,
                "window_muted": self._muted,
            },
            minimum_level="detailed",
        )

    def _begin_mixed_media_transition(
        self, direction: str, next_row: int, next_path: str, reason: str,
    ) -> bool:
        if self._mixed_transition_state != "idle":
            return False
        self._mixed_transition_audio_probe_done = set()
        if direction == "video_to_audio" and self._dual_transition_committed_state_value() is not None:
            # v1.0.71 correction: a genuine GPU cross-dissolve has already
            # committed for the current video -- that engine already owns
            # this boundary (its own commit point already advanced the
            # queue once, per DualVideoTransitionEngine.try_commit). Back
            # off entirely rather than contest it; ordinary video-video
            # completion runs its course untouched.
            return False
        transition_id = self._next_mixed_transition_id()
        self._mixed_transition_state = "preparing"
        self._mixed_transition_direction = direction
        self._mixed_transition_outgoing_path = self.current_path
        self._mixed_transition_incoming_path = next_path
        self._mixed_transition_incoming_row = next_row
        self._mixed_transition_reason = reason
        # Long audio->video gap investigation: anchor for the new
        # incoming_video_*/audio_fade_out_started/video_play_requested/
        # video_playing_state_received checkpoints below (see
        # _record_mixed_transition_gap_checkpoint) -- elapsed_ms is always
        # relative to this exact moment, never to _mixed_transition_start
        # (which isn't set until _activate_mixed_media_transition, itself
        # one of the things this is trying to time).
        self._mixed_transition_requested_monotonic = time.monotonic()
        self.pending_next = True
        if direction == "video_to_audio":
            # Claim exclusive ownership of this outgoing-video boundary:
            # tear down any video-video preload/deadline-timer state that
            # may have independently started for the current video's own
            # "what's next" candidate before _mixed_transition_owns_video_
            # boundary's suppression of further position updates could
            # take effect. VideoTransitionManager.cancel() safely refuses
            # once a transition has actually committed (already checked
            # above) and does not touch transition_id/preload_id
            # validation itself -- it is the same cancel mechanism
            # _play_path_direct's own direct-selection guard already
            # relies on elsewhere. Confirmed real-world bug without this:
            # the dual engine independently preloaded and later committed
            # a transition to a *different* video while this mixed
            # transition to the correct incoming audio was already active,
            # and the queue silently skipped the audio track.
            transition_manager = getattr(self, "_video_transition_manager", None)
            if transition_manager is not None:
                transition_manager.cancel("mixed_transition_claimed_boundary")
        if direction == "audio_to_video":
            # Mark the queue row played optimistically, the instant the
            # transition is committed to (mirrors the A-A crossfade's own
            # _start_miniaudio_crossfade_to, which marks played the moment
            # the load worker is dispatched, not once the crossfade
            # finishes) -- this is what lets Manual Next skip PAST a track
            # that is still preparing/fading in rather than re-attempting
            # the same one. Safe here specifically because an Audio->Video
            # preparation failure still plays this same path via the
            # direct hard-cut fallback (_abandon_mixed_media_transition_
            # and_fallback), so the row genuinely does get played either
            # way. Video->Audio is the mirror image -- see
            # _activate_mixed_media_transition for why it marks later.
            self._mark_queue_row_played(next_row)
        self.diagnostics.record(
            "playback", "mixed_transition_requested",
            details={
                "transition_id": transition_id, "direction": direction,
                "trigger": reason, "configured_overlap_s": self.crossfade_seconds,
                **self.diagnostics.path_details(next_path),
            },
            minimum_level="detailed",
        )
        if direction == "audio_to_video":
            return self._prepare_mixed_transition_audio_to_video(transition_id, next_path)
        return self._prepare_mixed_transition_video_to_audio(transition_id, next_path)

    def _prepare_mixed_transition_audio_to_video(self, transition_id: int, path: str) -> bool:
        self.diagnostics.record(
            "playback", "mixed_transition_preparing",
            details={
                "transition_id": transition_id, "direction": "audio_to_video",
                **self.diagnostics.path_details(path),
            },
            minimum_level="detailed",
        )
        self._video_backend.set_muted(True)
        self._mixed_transition_video_audio_scale = 0.0
        self._probe_mixed_video_audio_state("prepare")
        self._record_mixed_transition_gap_checkpoint("incoming_video_load_requested")
        try:
            started = self._video_backend.load(path)
            # A single load() call already both sets the source and issues
            # play() on the subprocess side (see video_subprocess.py's
            # "load" command handler) -- there is no separate "now start
            # playing" step to time independently for this direction, so
            # this checkpoint necessarily lands at the same instant as the
            # one above. Recorded anyway (matching the requested event
            # list exactly) so a future design change that does separate
            # them is automatically caught by the gap between the two.
            if started:
                self._record_mixed_transition_gap_checkpoint("video_play_requested")
        except Exception as ex:
            self._audio_log(f"mixed-transition audio->video prepare failed; error={ex}")
            started = False
        if not started:
            self._abandon_mixed_media_transition_and_fallback("video_load_rejected")
            return False
        return True

    def _prepare_mixed_transition_video_to_audio(self, transition_id: int, path: str) -> bool:
        self.diagnostics.record(
            "playback", "mixed_transition_preparing",
            details={
                "transition_id": transition_id, "direction": "video_to_audio",
                **self.diagnostics.path_details(path),
            },
            minimum_level="detailed",
        )
        if not self.simple_inactive_player:
            self._abandon_mixed_media_transition_and_fallback("no_inactive_audio_player")
            return False
        # v1.0.71 correction: this used to call simple_inactive_player.
        # load(path) directly on the GUI thread -- measured contributing
        # 100-170ms of real GUI lag on the same real session that exposed
        # the other acceptance-failure bugs (a mixed_transition_started
        # landed within 1ms of a logged gui_lag event of comparable
        # duration). player.load() reads/scans the file header, fast on a
        # local disk but not guaranteed on a network share. Reuses the
        # same off-thread mechanism the A-A crossfade's own incoming
        # player load already uses (_start_miniaudio_crossfade_to) rather
        # than inventing a new one -- the transition_id doubles as the
        # load token, matching that existing pattern exactly. Phase C1:
        # the worker never receives a live player -- it only prepares a
        # private candidate; commit/discard is decided in the callback
        # below, using mixed_transition_id + a physical target lease
        # (deliberately not migrated to PlaybackAttempt -- see the Phase
        # C1 design, section 7).
        use_bass = self._use_bass_backend()
        lease = self._make_target_lease("bass" if use_bass else "miniaudio", "inactive")
        if use_bass:
            if _BassEngine is not None:
                try:
                    _BassEngine.ensure()
                except Exception:
                    pass  # surfaced instead via the worker's own `failed` signal below
            worker = BassStreamPrepareWorker(path, transition_id)
        else:
            worker = MiniaudioSourcePrepareWorker(path, transition_id)
        self._mixed_transition_load_worker = worker
        registry_token = self._worker_registry.register(
            "mixed_transition_load", thread=worker, wait_ms=1500,
            finalize_after_join=lambda w=worker: self._finalize_unclaimed_prepare_candidate(w, "mixed_transition_load_join"),
        )
        worker.prepared.connect(
            lambda tok, p, _candidate, w=worker: self._on_mixed_transition_audio_load_prepared(
                tok, p, w.claim_candidate(), lease,
            )
        )
        worker.failed.connect(self._on_mixed_transition_audio_load_failed)
        # Real-device correctness bug (2026-08-31 Codex audit): this used
        # to unconditionally null the reference on *any* worker's finished
        # signal -- worker A (superseded, e.g. by a Manual Next that
        # dispatched a fresh worker B for the same still-unplayed target;
        # see _load_video_to_audio's stale-worker test) finishing *after*
        # B has already been assigned here would erase ownership of B
        # while B is still genuinely running, even though B's own result
        # callbacks are correctly guarded by transition_id. Only clear the
        # reference if it still points at *this exact* worker; A's own
        # finished cleanup then simply does nothing to B's slot. The
        # registry token is captured per-worker (not read from self),
        # so it always unregisters the right entry regardless of ownership.
        worker.finished.connect(
            lambda w=worker, t=registry_token: self._on_mixed_transition_load_worker_finished(w, t)
        )
        worker.start()
        return True

    def _on_mixed_transition_load_worker_finished(self, worker, registry_token) -> None:
        if self._closing:
            self._finalize_unclaimed_prepare_candidate(worker, "shutdown_worker_finished")
        if self._mixed_transition_load_worker is worker:
            self._mixed_transition_load_worker = None
        self._worker_registry.unregister(registry_token)
        self._maybe_resume_final_shutdown()

    def _on_mixed_transition_audio_load_prepared(
        self, token: int, path: str, candidate, lease: Optional[PlayerTargetLease] = None,
    ) -> None:
        """Phase C1: replaces the old _on_mixed_transition_audio_load_succeeded,
        which assumed the worker had already mutated a live inactive-slot
        player. `candidate` has not touched any player -- every rejection
        branch below discards it; only the final, fully-validated branch
        commits it. Deliberately keyed on mixed_transition_id + this
        target lease, NOT PlaybackAttempt (see Phase C1 design, section 7).

        Phase C2: `candidate` arrives via worker.claim_candidate() --
        None means someone else already claimed and resolved it."""
        if candidate is None:
            return
        if (
            self._mixed_transition_state != "preparing"
            or self._mixed_transition_direction != "video_to_audio"
            or token != self._mixed_transition_id
        ):
            self._discard_prepared_candidate(candidate, "mixed_transition_video_to_audio")
            return  # cancelled/superseded while the load was in flight
        if lease is None or not self._target_lease_still_valid(lease):
            self.diagnostics.record(
                "playback", "player_target_lease_invalid",
                details={"stage": "mixed_transition_video_to_audio"},
                minimum_level="basic",
            )
            self._discard_prepared_candidate(candidate, "mixed_transition_video_to_audio")
            return
        player = lease.physical_object
        # Reuses the v1.0.70 gain-slot-token machinery exactly as a normal
        # crossfade's incoming player does -- a late GainLookupWorker result
        # will correctly self-correct _inactive_normalisation_gain whether
        # it lands during "preparing", "active", or after promotion.
        gain = self._cached_gain_for_path(path, target="inactive")
        self._inactive_normalisation_gain = gain
        self._mixed_transition_gain_token = self._inactive_gain_token
        try:
            if not player.commit_prepared(candidate):
                return
            player.set_volume(0.0)
            # Start the incoming stream silently right away -- exactly the
            # same "load succeeded" != "playback started" distinction
            # _on_crossfade_load_prepared's own comment documents for the
            # A-A crossfade. Without this, the promoted player after
            # _finish_mixed_transition_video_to_audio has never actually
            # been playing: its position never advances, and the playback
            # watchdog armed at promotion correctly (and confusingly)
            # concludes playback stalled.
            player.play()
        except Exception as ex:
            self._audio_log(f"mixed-transition video->audio prepare failed; error={ex}")
            self._abandon_mixed_media_transition_and_fallback("audio_play_failed")
            return
        self._activate_mixed_media_transition()

    def _on_mixed_transition_audio_load_failed(self, token: int, path: str, error: str) -> None:
        if (
            self._mixed_transition_state != "preparing"
            or self._mixed_transition_direction != "video_to_audio"
            or token != self._mixed_transition_id
        ):
            return
        self._audio_log(f"mixed-transition video->audio prepare failed; error={error}")
        self._abandon_mixed_media_transition_and_fallback("audio_load_failed")

    def _on_mixed_transition_video_ready(self) -> None:
        if self._mixed_transition_state != "preparing" or self._mixed_transition_direction != "audio_to_video":
            return
        self.diagnostics.record(
            "playback", "mixed_transition_ready",
            details={"transition_id": self._mixed_transition_id, "direction": "audio_to_video"},
            minimum_level="detailed",
        )
        self._probe_mixed_video_audio_state("video_ready")
        self._record_mixed_transition_gap_checkpoint("video_playing_state_received")
        self._activate_mixed_media_transition()

    def _on_mixed_transition_video_failed(self, category: str, message: str) -> None:
        if self._mixed_transition_state != "preparing" or self._mixed_transition_direction != "audio_to_video":
            return
        self._abandon_mixed_media_transition_and_fallback(category)

    def _activate_mixed_media_transition(self) -> None:
        if self._mixed_transition_state != "preparing":
            return
        self._mixed_transition_state = "active"
        self._mixed_transition_start = time.time()
        direction = self._mixed_transition_direction
        if direction == "audio_to_video":
            incoming_path = self._mixed_transition_incoming_path
            incoming_row = self._mixed_transition_incoming_row
            self._current_media_type = MediaType.VIDEO
            self._playback_generation += 1
            self._video_backend.set_muted(self._muted)
            self._video_backend.set_volume(0)
            self._activate_track_ui(incoming_row, incoming_path)
            self._show_video_output_page()
            self._record_mixed_transition_gap_checkpoint("incoming_video_visible")
            transition_manager = getattr(self, "_video_transition_manager", None)
            if transition_manager is not None:
                transition_manager.media_changed()
            self._probe_mixed_video_audio_state("transition_start")
            # The outgoing audio's fade-out is driven by the same fade
            # timer armed just below (_mixed_transition_start) -- its very
            # first tick already starts reducing simple_player's volume
            # (see _mixed_transition_tick), so "fade started" and
            # "transition became active" are the same instant for this
            # direction; recorded under its own name anyway, matching the
            # requested event list, and to make it obvious from the
            # diagnostics alone that this is *not* delayed relative to the
            # video becoming visible above.
            self._record_mixed_transition_gap_checkpoint("audio_fade_out_started")
        else:
            # Video->Audio: only mark the row played once preparation has
            # genuinely succeeded (we're here because it did) -- unlike
            # Audio->Video, a Video->Audio *failure* deliberately leaves
            # the video playing untouched with nothing else about to play
            # this path, so marking it played optimistically at dispatch
            # time would silently drop the track from the queue if
            # preparation had failed instead of reaching here.
            row = self._mixed_transition_incoming_row
            if row is not None:
                self._mark_queue_row_played(row)
        self.diagnostics.record(
            "playback", "mixed_transition_started",
            details={
                "transition_id": self._mixed_transition_id, "direction": direction,
                "overlap_s": self.crossfade_seconds,
            },
            minimum_level="detailed",
        )

    def _mixed_transition_tick(self) -> None:
        if self._mixed_transition_state != "active" or self._mixed_transition_start is None:
            return
        elapsed = time.time() - self._mixed_transition_start
        duration = self.crossfade_seconds
        t = 1.0 if duration <= 0 else min(1.0, elapsed / duration)
        # Same curve as the existing A-A crossfade's _fade_tick.
        in_scale = t ** 0.5
        out_scale = 1.0 - in_scale
        if self._mixed_transition_direction == "audio_to_video":
            if self.simple_player:
                try:
                    self.simple_player.set_volume(combine_volume(self.master_volume / 100.0, self._active_normalisation_gain, out_scale * self._sleep_timer_gain))
                except Exception:
                    pass
            self._mixed_transition_video_audio_scale = in_scale
            self._video_backend.set_volume(self.master_volume * in_scale)
            for threshold in (0.25, 0.5, 0.75):
                if t >= threshold and threshold not in self._mixed_transition_audio_probe_done:
                    self._mixed_transition_audio_probe_done.add(threshold)
                    self._probe_mixed_video_audio_state(f"{int(threshold * 100)}pct")
            if t >= 1.0:
                self._finish_mixed_transition_audio_to_video()
        else:
            self._mixed_transition_video_audio_scale = out_scale
            self._video_backend.set_volume(self.master_volume * out_scale)
            if self.simple_inactive_player:
                try:
                    self.simple_inactive_player.set_volume(combine_volume(self.master_volume / 100.0, self._inactive_normalisation_gain, in_scale * self._sleep_timer_gain))
                except Exception:
                    pass
            if t >= 1.0:
                self._finish_mixed_transition_video_to_audio()

    def _finish_mixed_transition_audio_to_video(self) -> None:
        # Note: the queue row was already marked played optimistically in
        # _begin_mixed_media_transition -- nothing to do here for that.
        transition_id = self._mixed_transition_id
        self._stop_all()
        try:
            self._cancel_playback_watchdog()
        except Exception:
            pass
        self._video_backend.set_volume(self.master_volume)
        self.diagnostics.record(
            "playback", "mixed_transition_committed",
            details={"transition_id": transition_id, "direction": "audio_to_video"},
            minimum_level="detailed",
        )
        self._probe_mixed_video_audio_state("commit")
        # Both recorded before the state reset below (which clears
        # _mixed_transition_direction, the gap-checkpoint helper's own
        # guard) -- transition_completed is captured here rather than
        # after, unlike the pre-existing mixed_transition_completed event
        # a few lines down, since this is a separate, purely additive gap
        # diagnostic with no other code depending on its exact ordering.
        self._record_mixed_transition_gap_checkpoint("transition_committed")
        self._record_mixed_transition_gap_checkpoint("transition_completed")
        self._reset_mixed_media_transition_state()
        self.pending_next = False
        self.diagnostics.record(
            "playback", "mixed_transition_completed",
            details={"transition_id": transition_id, "direction": "audio_to_video"},
            minimum_level="detailed",
        )
        self._probe_mixed_video_audio_state("completion")
        QtCore.QTimer.singleShot(
            1000,
            lambda: self._probe_post_completion_video_audio_state(transition_id),
        )

    def _finish_mixed_transition_video_to_audio(self) -> None:
        transition_id = self._mixed_transition_id
        row = self._mixed_transition_incoming_row
        promoted_path = self._mixed_transition_incoming_path
        self._promote_inactive_player(reason="mixed_transition_video_to_audio")
        self._promote_inactive_gain_slot(promoted_path)
        if self.simple_player:
            try:
                self.simple_player.set_volume(combine_volume(self.master_volume / 100.0, self._active_normalisation_gain, self._sleep_timer_gain))
            except Exception:
                pass
        if self.simple_inactive_player:
            try:
                self.simple_inactive_player.set_volume(0.0)
            except Exception:
                pass
        self.diagnostics.record(
            "playback", "mixed_transition_committed",
            details={"transition_id": transition_id, "direction": "video_to_audio"},
            minimum_level="detailed",
        )
        # v1.0.71 correction: this used to duplicate _stop_video_for_audio_
        # transition's body manually, with _current_media_type flipped to
        # AUDIO *after* _show_normal_display_page() instead of before --
        # exactly the ordering bug that method's own docstring documents
        # fixing once already (_show_normal_display_page ->
        # _refresh_visualiser_lifecycle("video_hidden")'s panel_visible
        # check requires _current_media_type == AUDIO; seeing it still
        # VIDEO at that instant left the main visualiser/analyzer_feed
        # concluding they should stay suspended, with nothing else in the
        # ordinary playback path ever re-evaluating the lifecycle
        # afterward -- the equaliser stayed frozen). Calling the canonical
        # method directly, instead of duplicating its logic, both fixes
        # this and guarantees the two can never drift apart again.
        self._stop_video_for_audio_transition()
        self._playback_generation += 1
        self._activate_track_ui(row, promoted_path)
        self._reset_progress()
        try:
            promoted_position = float(self.simple_player.get_pos() or 0.0)
        except Exception:
            promoted_position = 0.0
        self._arm_playback_watchdog(promoted_position)
        self._reset_mixed_media_transition_state()
        self.pending_next = False
        self.diagnostics.record(
            "playback", "mixed_transition_completed",
            details={"transition_id": transition_id, "direction": "video_to_audio"},
            minimum_level="detailed",
        )

    def _abandon_mixed_media_transition_and_fallback(self, reason: str) -> None:
        """Preparation failed before any overlap began (video rejected the
        load, or the inactive audio player couldn't load the next track).
        The outgoing medium was never touched, so falling back to today's
        direct hard-cut for the same incoming path is a safe, bounded
        degradation -- not a new failure mode. The queue row was already
        marked played in _begin_mixed_media_transition, so no further
        queue bookkeeping is needed here either way."""
        transition_id = self._mixed_transition_id
        direction = self._mixed_transition_direction
        next_path = self._mixed_transition_incoming_path
        self.diagnostics.record(
            "playback", "mixed_transition_failed",
            details={"transition_id": transition_id, "direction": direction, "fallback_reason": reason},
            minimum_level="basic",
        )
        self._reset_mixed_media_transition_state()
        self.pending_next = False
        if direction == "audio_to_video":
            try:
                self._video_backend.stop()
            except Exception:
                pass
            self._play_path_direct(next_path, crossfade=False, immediate_crossfade=True)
        # video_to_audio: nothing else to do -- the video was never
        # touched, so it keeps playing normally and its own natural-end/
        # manual-next path will retry (or fall back to today's hard cut).

    def _cancel_mixed_media_transition(self, reason: str) -> None:
        if self._mixed_transition_state == "idle":
            return
        transition_id = self._mixed_transition_id
        direction = self._mixed_transition_direction
        was_active = self._mixed_transition_state == "active"
        self.diagnostics.record(
            "playback", "mixed_transition_cancelled",
            details={
                "transition_id": transition_id, "direction": direction,
                "reason": reason, "was_active": was_active,
            },
            minimum_level="basic",
        )
        closing = getattr(self, "_closing", False)
        if direction == "audio_to_video":
            try:
                self._video_backend.stop()
            except Exception:
                pass
            if was_active and not closing:
                try:
                    self._detach_video_from_party_mode()
                    self._show_normal_display_page()
                except Exception:
                    pass
                self._current_media_type = MediaType.AUDIO
        else:
            if self.simple_inactive_player:
                try:
                    self.simple_inactive_player.stop()
                except Exception:
                    pass
            self._inactive_normalisation_gain = 1.0
            self._inactive_gain_token = self._next_gain_token()
        self._reset_mixed_media_transition_state()
        self.pending_next = False

    def _maybe_prepare_mixed_transition_from_video(self) -> None:
        """The Video->Audio equivalent of _tick()'s audio-side near-end
        trigger. _tick() itself never reaches the ordinary near-end/
        quiet-end audio polling while a video is current (see the early
        return in _tick()), so Video->Audio preparation needs its own,
        much simpler trigger: video position/duration are read directly
        (no signal plumbing needed -- QtVideoPlaybackBackend already
        exposes plain getters), and the next queue path is peeked without
        committing to it (_peek_next_media_type_for_transition), exactly
        as the video-video dual engine already does before it decides
        whether to preload a secondary deck."""
        if self.track_transition_mode != "crossfade":
            return
        if self._mixed_transition_state != "idle":
            return
        if self.pending_next or self.fade_active or self.prebuffer_active:
            return
        next_type = self._peek_next_media_type_for_transition()
        if next_type != MediaType.AUDIO:
            return
        try:
            position_ms = self._video_backend.position_ms()
            duration_ms = self._video_backend.duration_ms()
        except Exception:
            return
        if duration_ms <= 0:
            return
        remaining_s = max(0.0, (duration_ms - position_ms) / 1000.0)
        lead_s = self.crossfade_seconds + (PREBUFFER_MS / 1000.0)
        if remaining_s > lead_s:
            return
        next_queue_row = self._next_unplayed_queue_row()
        if next_queue_row is None:
            return
        next_path = self.queue[next_queue_row]
        if classify_path(next_path) != MediaType.AUDIO:
            return
        if is_plex_identity(next_path) or is_plex_identity(self.current_path):
            # Stage 3A: this trigger fires every tick while the video
            # nears its end -- unlike _next_track's own one-shot mixed-
            # transition attempt, retrying a Plex target here every tick
            # (it would fail deterministically every time, see
            # _mixed_media_transition_eligible's matching guard) produced
            # a real-device retry storm: dozens of failed BASS_StreamCreateFile
            # attempts per second for the remainder of the video. Just
            # don't attempt it -- the video's own natural end still falls
            # through to _next_track -> _play_path_direct, which resolves
            # Plex identities correctly.
            return
        self.pending_next = True
        if not self._begin_mixed_media_transition("video_to_audio", next_queue_row, next_path, "video-near-end"):
            # Declined (e.g. a video-video transition already committed
            # for the current video, see _begin_mixed_media_transition) --
            # nothing was dispatched, so don't leave the audio-side near-
            # end/quiet-end polling gate stuck once audio is current again.
            self.pending_next = False

    # -- video display stack -----------------------------------------------
    def _dual_transition_committed_state_value(self) -> Optional[str]:
        """The dual-deck controller's own .state.value if a GPU cross-
        dissolve is currently committed (TRANSITIONING/PROMOTING_SECONDARY/
        CLEANING_PRIMARY -- see video_dual_transition.COMMITTED_STATES),
        else None. Cheap, safe to call from anywhere; used only for
        diagnostics below, never for control flow."""
        transition_manager = getattr(self, "_video_transition_manager", None)
        dual_engine = getattr(transition_manager, "dual_engine", None) if transition_manager else None
        controller = getattr(dual_engine, "controller", None)
        state = getattr(controller, "state", None)
        if state is None:
            return None
        from .video_dual_transition import COMMITTED_STATES
        return state.value if state in COMMITTED_STATES else None

    def _record_video_host_visibility_change(self, page: str) -> None:
        """Permanent, non-per-frame diagnostic marker for every video
        presentation-area page switch (Loading/Output/Error/Normal), added
        after real manual testing found the app's own background flashing
        through between Video->Video GPU transitions: once a GPU
        cross-dissolve has committed, the presentation host/container must
        stay visible and unchanged until the incoming deck is promoted --
        any page switch recorded here with a non-null
        dual_transition_committed_state is exactly that invariant being
        violated and should be investigated as a regression of the same
        class of bug fixed in video_transition.py's
        handle_natural_end()/request_manual_next()."""
        container = getattr(self._video_backend, "_embedded_container", None)
        self.diagnostics.record(
            "playback", "video_host_visibility_changed",
            details={
                "page": page,
                "dual_transition_committed_state": self._dual_transition_committed_state_value(),
                "video_output_widget_visible": bool(
                    getattr(self, "video_output_widget", None) is not None
                    and self.video_output_widget.isVisible()
                ),
                "embedded_container_exists": container is not None,
                "embedded_container_visible": bool(container is not None and container.isVisible()),
            },
            minimum_level="detailed",
        )

    def _show_video_loading_page(self):
        self.right_display_stack.setCurrentWidget(self._video_loading_page)
        # QStackedWidget hides the non-current page automatically (so
        # visualiser_frame.isVisible() already goes False for free), but the
        # lifecycle controller only re-evaluates on an explicit trigger.
        self._refresh_visualiser_lifecycle("video_shown")
        _record_page = getattr(self, "_record_video_host_visibility_change", None)
        if _record_page is not None:
            _record_page("loading")

    def _show_video_output_page(self):
        self.right_display_stack.setCurrentWidget(self._video_output_page)
        self._refresh_visualiser_lifecycle("video_shown")
        _record_page = getattr(self, "_record_video_host_visibility_change", None)
        if _record_page is not None:
            _record_page("output")

    def _show_video_error_page(self, message: str):
        self._video_error_label.setText(message)
        self.right_display_stack.setCurrentWidget(self._video_error_page)
        self._refresh_visualiser_lifecycle("video_shown")
        _record_page = getattr(self, "_record_video_host_visibility_change", None)
        if _record_page is not None:
            _record_page("error")

    def _show_normal_display_page(self):
        self.right_display_stack.setCurrentWidget(self._normal_display_page)
        self._refresh_visualiser_lifecycle("video_hidden")
        _record_page = getattr(self, "_record_video_host_visibility_change", None)
        if _record_page is not None:
            _record_page("normal")

    # -- Phase 1 video visual transitions ---------------------------------
    def _video_transition_host(
        self, stage: str, target_media_type: Optional[MediaType],
    ) -> Optional[QtWidgets.QWidget]:
        """Return the visible stage the painted transition must cover.

        The one video decoder/native window remains untouched.  The overlay
        follows whichever existing host currently owns that surface and moves
        to the containing stack while media pages change behind full cover.
        """
        party_mode = getattr(self, "party_mode", None)
        party_visible = bool(party_mode is not None and party_mode.isVisible())
        if party_visible:
            if (
                stage in ("outgoing", "incoming")
                and target_media_type == MediaType.VIDEO
            ):
                return getattr(party_mode, "video_widget", None)
            return getattr(party_mode, "stack", party_mode)
        if (
            stage in ("outgoing", "incoming")
            and target_media_type == MediaType.VIDEO
        ):
            return getattr(self, "video_output_widget", None)
        return getattr(self, "right_display_stack", None)

    def _record_video_transition_event(self, event: str, details) -> None:
        self.diagnostics.record(
            "playback",
            f"video_transition_{event}",
            details=dict(details),
            minimum_level="detailed",
        )

    def _advance_video_transition(self, trigger: str) -> Optional[MediaType]:
        """Call the existing authoritative Next path once at full coverage."""
        generation_before = self._playback_generation
        reason = "manual-next" if trigger == "manual" else "video-ended"
        self._next_track(reason)
        if self._playback_generation == generation_before:
            if trigger == "automatic":
                PlayerWindow._finish_video_end_without_next(self)
            return None
        return self._current_media_type

    def _peek_next_media_type_for_transition(self) -> Optional[MediaType]:
        """Classify the likely successor without changing queue/history state."""
        next_queue_row = self._next_unplayed_queue_row()
        if next_queue_row is not None and 0 <= next_queue_row < len(self.queue):
            return classify_path(self.queue[next_queue_row])
        fallback = self._playback_fallback_paths()
        if not fallback:
            return None
        if self.current_path in fallback:
            next_index = (fallback.index(self.current_path) + 1) % len(fallback)
        else:
            next_index = 0
        return classify_path(fallback[next_index])

    # -- Phase 2A dual-video cross-dissolve (experimental) -------------------
    def _peek_next_queue_identity_for_dual_transition_pure(self) -> Optional[SecondaryIdentity]:
        """Side-effect-free identity lookup -- no Smart Transition Points
        analysis, nothing that can touch the filesystem or a network share.
        Used as DualVideoTransitionEngine's staleness_identity_provider:
        anywhere an already-in-flight preload's identity is merely being
        re-verified (on_secondary_ready, try_commit), never where a *new*
        preload is being requested. A real-device GUI-thread stall traced a
        200ms freeze to this exact staleness-check path incidentally
        re-triggering a (since-removed) prefetch call (see CODEX_HANDOFF.md)
        -- this split keeps that path pure by construction rather than by
        convention."""
        next_queue_row = self._next_unplayed_queue_row()
        if next_queue_row is None or not (0 <= next_queue_row < len(self.queue)):
            return None
        path = self.queue[next_queue_row]
        return SecondaryIdentity(
            epoch=self._queue_mutation_epoch,
            row=next_queue_row,
            path=path,
            media_type=classify_path(path),
        )

    def _peek_next_queue_identity_for_dual_transition(self) -> Optional[SecondaryIdentity]:
        """Like _peek_next_media_type_for_transition, but also captures the
        stable-enough identity (epoch + row + path) a Phase 2A preload needs
        to detect a stale target later. Deliberately queue-only -- the
        library fallback wraparound path (used once Up Next is empty) has
        no row concept the epoch counter tracks, so dual preload/commit
        simply never triggers in that case and Phase 1 remains available.

        This is DualVideoTransitionEngine's primary identity_provider, used
        only at the genuine lead-window preload trigger in
        observe_position() -- see _peek_next_queue_identity_for_dual_
        transition_pure above for the side-effect-free variant used
        everywhere else."""
        identity = self._peek_next_queue_identity_for_dual_transition_pure()
        if identity is None:
            return None
        analyze_intro = getattr(self, "_maybe_analyze_video_intro", None)
        if analyze_intro is not None:
            analyze_intro(identity.path)
        return identity

    def _maybe_analyze_video_outro(self, path: str) -> None:
        """Smart Video Transition Points: called whenever a video becomes
        current (see _play_video_path_direct) -- analysis itself is cache-
        first/dedup-protected/never-blocking inside the analyzer (see
        video_transition_point_analyzer.py), so it's safe to call this on
        every video load without extra bookkeeping here. Only actually
        requests analysis when the feature is genuinely reachable (GPU dual
        transitions available and the "Smart video transition points"
        checkbox on) -- no point launching a probe subprocess whose result
        nothing will ever consult otherwise."""
        analyzer = getattr(self, "_video_transition_point_analyzer", None)
        if analyzer is None or not path or classify_path(path) != MediaType.VIDEO:
            return
        if not (
            DUAL_VIDEO_TRANSITIONS_AVAILABLE
            and getattr(self, "video_smart_transition_points_enabled", False)
        ):
            return
        analyzer.analyze_outro(path)

    def _maybe_analyze_video_intro(self, path: str) -> None:
        """Smart Video Transition Points: called whenever a video becomes
        the dual engine's Up Next preload candidate (see
        _peek_next_queue_identity_for_dual_transition, polled repeatedly --
        analyzer-level dedup makes repeat calls for the same path free)."""
        analyzer = getattr(self, "_video_transition_point_analyzer", None)
        if analyzer is None or not path or classify_path(path) != MediaType.VIDEO:
            return
        if not (
            DUAL_VIDEO_TRANSITIONS_AVAILABLE
            and getattr(self, "video_smart_transition_points_enabled", False)
        ):
            return
        analyzer.analyze_intro(path)

    def _prepare_dual_transition_promotion(self, path: str) -> None:
        # Runs at the exact GPU transition commitment point, immediately
        # before the shared _next_track()/_play_path_direct() queue-advance
        # seam -- the one moment both the outgoing (A, still self.current_path
        # here) and incoming (B, `path`) identities and the presentation
        # host's current visibility are simultaneously available. Permanent,
        # not per-frame; fires once per committed transition.
        container = getattr(self._video_backend, "_embedded_container", None)
        self.diagnostics.record(
            "playback", "dual_transition_commitment_point",
            details={
                "dual_transition_committed_state": self._dual_transition_committed_state_value(),
                "media_type_before": self._current_media_type.value,
                "path_a_outgoing": self.diagnostics.path_details(self.current_path or ""),
                "path_b_incoming": self.diagnostics.path_details(path),
                "video_output_widget_visible": bool(
                    getattr(self, "video_output_widget", None) is not None
                    and self.video_output_widget.isVisible()
                ),
                "embedded_container_exists": container is not None,
                "embedded_container_visible": bool(container is not None and container.isVisible()),
            },
            minimum_level="detailed",
        )
        self._dual_transition_promoted_path = path

    def _start_gpu_capability_probe(self) -> None:
        """Launch the one-time, cached GPU compositor capability check --
        idempotent: does nothing if already probed this session or a probe
        is already in flight, so this is safe to call from multiple
        places (startup, and defensively before showing Preferences)
        without ever launching a second probe process."""
        if not DUAL_VIDEO_TRANSITIONS_AVAILABLE or getattr(self, "_closing", False):
            return
        if self._gpu_dual_capability is not None or self._gpu_dual_probe is not None:
            return
        probe = GpuCompositorProbe(self)
        probe.finished.connect(self._on_gpu_capability_probe_finished)
        self._gpu_dual_probe = probe
        probe.start()

    def _on_gpu_capability_probe_finished(self, available: bool, reason: str) -> None:
        if getattr(self, "_closing", False):
            return
        self._gpu_dual_capability = bool(available)
        self._gpu_dual_probe = None
        self.diagnostics.record(
            "playback", "gpu_dual_capability_probe_result",
            details={"available": bool(available), "reason": reason},
            minimum_level="basic",
        )
        self._apply_gpu_dual_mode_state()

    def _apply_gpu_dual_mode_state(self) -> None:
        """Single place deciding whether the video subprocess should
        actually be running in GPU dual mode right now, and applying it.
        Called after settings load, whenever the capability probe
        completes, and when Preferences is saved -- the desired mode can
        change from any of those three, but the decision itself (and the
        restart-only-on-change behaviour in set_dual_mode) lives here once."""
        video_backend = getattr(self, "_video_backend", None)
        if video_backend is None:
            return
        wants_gpu = (
            DUAL_VIDEO_TRANSITIONS_AVAILABLE
            and self._gpu_dual_capability is True
            and bool(getattr(self, "video_dual_transitions_enabled", False))
        )
        video_backend.set_dual_mode("gpu" if wants_gpu else None)
        transition_manager = getattr(self, "_video_transition_manager", None)
        if transition_manager is not None:
            # Smart Video Transition Points only ever has an effect when the
            # GPU dual engine itself is actually running -- it adjusts that
            # engine's own timing/preload, nothing else -- so both
            # subordinate flags are additionally gated on wants_gpu and the
            # parent "Smart video transition points" checkbox here, the same
            # way video_gpu_transition_effect is only meaningful when
            # wants_gpu is True.
            smart_enabled = wants_gpu and bool(
                getattr(self, "video_smart_transition_points_enabled", False)
            )
            transition_manager.configure_dual(
                DualTransitionPreferences(
                    enabled=wants_gpu,
                    # preload_lead_seconds/ready_timeout_ms/preload_max_wait_ms/
                    # preload_progress_extension_ms were previously omitted
                    # here entirely -- DualTransitionPreferences.from_config()
                    # parsing them from config.json was not sufficient on its
                    # own, since this manual reconstruction (not from_config)
                    # is what actually reaches the live engine. Fixed as part
                    # of Stage B; see CODEX_HANDOFF.md.
                    preload_lead_seconds=getattr(
                        self, "video_dual_preload_lead_seconds", 10.0,
                    ),
                    ready_timeout_ms=getattr(
                        self, "video_dual_ready_timeout_ms", 4000,
                    ),
                    gpu_effect=getattr(
                        self, "video_gpu_transition_effect", "Cross Dissolve",
                    ),
                    automatic_lead_seconds=getattr(
                        self, "video_transition_automatic_lead_seconds", 1.0,
                    ),
                    duration_seconds=getattr(
                        self, "video_transition_duration_seconds", 1.0,
                    ),
                    avoid_black_outros=smart_enabled and bool(
                        getattr(self, "video_avoid_black_outros", False)
                    ),
                    skip_black_intros=smart_enabled and bool(
                        getattr(self, "video_skip_black_intros", False)
                    ),
                    crossfade_video_audio_enabled=wants_gpu and bool(
                        getattr(self, "video_crossfade_audio_enabled", False)
                    ),
                    audio_crossfade_curve=getattr(
                        self, "video_crossfade_audio_curve", "Equal Power",
                    ),
                    preload_max_wait_ms=getattr(
                        self, "video_dual_preload_max_wait_ms", 12000,
                    ),
                    preload_progress_extension_ms=getattr(
                        self, "video_dual_preload_progress_extension_ms", 2000,
                    ),
                )
            )

    def _on_video_secondary_ready(self, envelope: dict) -> None:
        dual_engine = getattr(self._video_transition_manager, "dual_engine", None)
        if dual_engine is not None:
            dual_engine.on_secondary_ready(envelope)

    def _on_video_secondary_failed(self, envelope: dict) -> None:
        dual_engine = getattr(self._video_transition_manager, "dual_engine", None)
        if dual_engine is not None:
            dual_engine.on_secondary_failed(str(envelope.get("reason", "")), envelope)

    def _on_video_secondary_preload_progress(self, details: dict) -> None:
        dual_engine = getattr(self._video_transition_manager, "dual_engine", None)
        if dual_engine is not None:
            dual_engine.on_secondary_preload_progress(details)

    def _on_video_dual_transition_complete(self, transition_id: int) -> None:
        dual_engine = getattr(self._video_transition_manager, "dual_engine", None)
        if dual_engine is not None:
            dual_engine.on_dual_transition_complete(transition_id)

    def _on_video_dual_transition_failed(self, transition_id: int, reason: str) -> None:
        # Correctness hardening (2026-08-24): a typed failure specifically
        # for a *committed* GPU transition (see video_backend.py's
        # dual_transition_failed signal) -- deliberately separate from
        # _on_video_error()'s generic display/error cleanup, which must
        # never independently abort whichever transition happens to be
        # active. primary_failed() itself re-verifies transition_id
        # against whatever this engine currently considers active before
        # doing anything.
        dual_engine = getattr(self._video_transition_manager, "dual_engine", None)
        if dual_engine is not None:
            dual_engine.primary_failed(transition_id, reason)

    def _promote_dual_transition_track_ui(
        self, path: str, index: Optional[int], *,
        reason: str = "dual_transition_promotion",
    ) -> bool:
        """Short-circuit for _play_video_path_direct(): `path` is already
        playing live as the current video -- either just now promoted by
        the video subprocess as the secondary deck (see video_dual_
        transition.py's _advance_queue_once and video_subprocess.py's
        _on_dual_transition_finished), or a later redundant reactivation
        of that same already-current video (e.g. re-clicking/re-selecting
        it while it's still playing -- confirmed via a real-device
        diagnostic trace to otherwise reach the normal reload path and
        break the video, see the caller). Either way: do the normal
        current-track bookkeeping (Recently Played, session, generation
        bump, tag loading, now-playing UI) exactly as usual, but skip
        touching the video backend at all: issuing a fresh load()/stop()
        here would interrupt whatever the video subprocess -- GPU
        compositor or classic single-deck -- already has on screen."""
        self._current_media_type = MediaType.VIDEO
        self._playback_generation += 1
        self._playback_recovery_active = False
        self._playback_recovery_attempts = {}
        self._playback_expected = True
        self._playback_intentionally_paused = False
        self._video_progress_started_at = time.monotonic()
        self._video_progress_warning_reported = False
        self._video_timing_available_reported = False
        if index is None:
            index = self.track_index_by_path.get(path)
        self._activate_track_ui(index, path)
        self.diagnostics.record(
            "playback", "video_dual_transition_promoted",
            details={
                "media_type_after": self._current_media_type.value,
                "video_backend_load_called": False,
                "video_backend_stop_called": False,
                "reason": reason,
                **self.diagnostics.path_details(path),
            },
            minimum_level="detailed",
        )
        return True

    def _finish_video_end_without_next(self) -> None:
        """Restore the ordinary UI when a completed video has no successor."""
        if not getattr(self, "video_return_to_normal_display_on_end", True):
            return
        if getattr(self, "_video_fullscreen", False):
            self._exit_video_fullscreen()
        try:
            self._video_backend.stop()
        except Exception:
            pass
        self._detach_video_from_party_mode()
        self._show_normal_display_page()
        self._current_media_type = MediaType.AUDIO
        self._sync_now_playing_overlay_for_media_type()

    # -- video backend signal handlers --------------------------------------
    def _on_video_started(self):
        mixed_prep = (
            self._mixed_transition_state == "preparing"
            and self._mixed_transition_direction == "audio_to_video"
        )
        if self._current_media_type != MediaType.VIDEO and not mixed_prep:
            return
        if mixed_prep:
            # _current_media_type is deliberately still AUDIO during
            # Audio->Video preparation (see the v1.0.71 block above) --
            # this is the readiness signal for that lifecycle instead of
            # the ordinary direct-video-activation path below.
            self._on_mixed_transition_video_ready()
            return
        self._show_video_output_page()
        self.scrubbing = False
        self._attach_video_to_party_mode()
        if getattr(self, "video_start_fullscreen", False) and not self._video_fullscreen:
            self._enter_video_fullscreen()
        transition_manager = getattr(self, "_video_transition_manager", None)
        if transition_manager is not None:
            transition_manager.media_ready(MediaType.VIDEO)
        self._set_playing_button_state()
        self.diagnostics.record(
            "playback", "video_started",
            details=self.diagnostics.path_details(self.current_path or ""),
        )

    def _on_video_paused(self):
        if self._current_media_type != MediaType.VIDEO:
            return
        self.diagnostics.record("playback", "video_paused", minimum_level="detailed")

    def _on_video_end_of_media(self):
        if self._current_media_type != MediaType.VIDEO:
            return
        if self._mixed_transition_owns_video_boundary():
            # v1.0.71 correction: a committed/in-flight Video->Audio mixed
            # transition already owns this boundary -- the outgoing
            # video reaching its physical end while that overlap is under
            # way is an expected event, not a request to reinterpret what
            # comes next. Letting this fall through to handle_natural_end()
            # (which could hand control to a video-video transition that
            # had independently preloaded a *different* next candidate) or
            # to _next_track() (which would find the incoming audio's
            # queue row already marked played and skip straight past it to
            # whatever video follows) was the confirmed root cause of the
            # queue silently jumping to the wrong track. The mixed
            # transition's own _mixed_transition_tick finishes it on its
            # own schedule regardless of when the video itself reaches
            # end-of-media.
            self.diagnostics.record(
                "playback", "mixed_transition_video_eof_suppressed",
                details={"transition_id": self._mixed_transition_id},
                minimum_level="detailed",
            )
            return
        self.diagnostics.record(
            "playback", "video_completed",
            details=self.diagnostics.path_details(self.current_path or ""),
            minimum_level="detailed",
        )
        transition_manager = getattr(self, "_video_transition_manager", None)
        if (
            transition_manager is not None
            and transition_manager.handle_natural_end(MediaType.VIDEO)
        ):
            return
        generation_before = self._playback_generation
        self._next_track("video-ended")
        if self._playback_generation == generation_before:
            # Nothing new started (queue and library fallback were both
            # empty) -- the video genuinely just ended with nothing next.
            PlayerWindow._finish_video_end_without_next(self)

    _VIDEO_ERROR_MESSAGES = {
        "video_file_missing": "This video file could not be found.",
        "video_format_unsupported": "This video's format isn't supported.",
        "video_resource_error": "This video could not be opened.",
        "video_decode_error": "This video could not be decoded.",
        "video_audio_output_error": "This video's audio could not be started.",
        "video_subprocess_error": "The video player stopped unexpectedly.",
        "video_unknown_error": "This video could not be played.",
    }

    def _on_video_error(self, category: str, message: str):
        mixed_prep = (
            self._mixed_transition_state == "preparing"
            and self._mixed_transition_direction == "audio_to_video"
        )
        if self._current_media_type != MediaType.VIDEO and not mixed_prep:
            return
        if mixed_prep:
            self._on_mixed_transition_video_failed(category, message)
            return
        transition_manager = getattr(self, "_video_transition_manager", None)
        if transition_manager is not None:
            # Cooperative cancel only -- deliberately refuses once a dual
            # transition has actually committed (see COMMITTED_STATES),
            # which is correct here: this generic handler has no
            # transition_id to verify, so it must never independently
            # abort whichever transition happens to be active. A
            # *committed* transition's real failure is reported with its
            # exact transition_id via backend.dual_transition_failed ->
            # _on_video_dual_transition_failed -> dual_engine.
            # primary_failed(transition_id, reason), which is the only
            # thing allowed to unstick a committed state (see that
            # method's own correctness-hardening docstring).
            transition_manager.playback_stopped("video_error")
        self.diagnostics.record(
            "playback", "video_failed", severity="warning",
            details={
                "category": category,
                "message": str(message),
                **self.diagnostics.path_details(self.current_path or ""),
            },
            minimum_level="basic",
        )
        # Stop playback, clear video state, and restore the normal display
        # -- matching the same terminal behaviour audio playback already
        # has once its own backend recovery attempts are exhausted (stop,
        # show a status message, no silent auto-advance): see
        # _begin_playback_recovery's final fallback.
        if getattr(self, "_video_fullscreen", False):
            self._exit_video_fullscreen()
        self._video_backend.stop()
        self._detach_video_from_party_mode()
        self._show_normal_display_page()
        self._current_media_type = MediaType.AUDIO
        self._sync_now_playing_overlay_for_media_type()
        self._playback_expected = False
        self._resume_deferred_queue_analysis()
        self.statusBar().showMessage(
            self._VIDEO_ERROR_MESSAGES.get(category, self._VIDEO_ERROR_MESSAGES["video_unknown_error"]),
            5000,
        )

    def _mixed_transition_owns_video_boundary(self) -> bool:
        """v1.0.71 correction: True while a Video->Audio mixed transition
        has claimed the current video's outgoing boundary (preparing or
        active). The video-video preload/deadline engine must not be fed
        further position data during that window -- otherwise it can
        independently preload and later commit a transition to whatever
        video follows, racing the mixed transition's own ownership of
        "what comes next" (confirmed real-world bug: the dual engine
        committed to a different video while a mixed transition to the
        correct incoming audio track was already active, and the queue
        silently skipped the audio track)."""
        return (
            self._mixed_transition_direction == "video_to_audio"
            and self._mixed_transition_state in ("preparing", "active")
        )

    def _on_video_position_changed(self, position_ms: int):
        if self._current_media_type != MediaType.VIDEO or self.scrubbing:
            return
        duration_ms = self._video_backend.duration_ms()
        if duration_ms > 0:
            # Reuses the exact same slider/waveform-placeholder/mini-player
            # update path audio already uses -- video just supplies position
            # and duration from Qt Multimedia instead of the audio backend.
            self._update_progress(position_ms, duration_ms)
            self._record_video_timing_available(position_ms, duration_ms)
            transition_manager = getattr(self, "_video_transition_manager", None)
            if transition_manager is not None and not self._mixed_transition_owns_video_boundary():
                transition_manager.observe_position(
                    position_ms, duration_ms, MediaType.VIDEO,
                )

    def _on_video_duration_changed(self, duration_ms: int):
        if self._current_media_type != MediaType.VIDEO:
            return
        if duration_ms > 0:
            position_ms = self._video_backend.position_ms()
            self._update_progress(position_ms, duration_ms)
            self._record_video_timing_available(
                position_ms, duration_ms,
            )
            transition_manager = getattr(self, "_video_transition_manager", None)
            if transition_manager is not None and not self._mixed_transition_owns_video_boundary():
                transition_manager.observe_position(
                    position_ms, duration_ms, MediaType.VIDEO,
                )

    def _record_video_timing_available(self, position_ms: int, duration_ms: int):
        if self._video_timing_available_reported:
            return
        self._video_timing_available_reported = True
        self.diagnostics.record(
            "playback", "video_timing_available",
            details={
                "position_ms": int(position_ms),
                "duration_ms": int(duration_ms),
                **self.diagnostics.path_details(self.current_path or ""),
            },
            minimum_level="basic",
        )

    # -- video fullscreen and context menu -----------------------------------
    def _on_video_widget_double_click(self, event):
        # Request, not a direct transition: this is a Qt mouse-event
        # handler and the transition must not run inside event delivery.
        self._request_video_fullscreen_state(
            not self._video_fullscreen, "container_double_click",
        )

    def _show_video_context_menu(self, pos):
        # Reached only for a right-click that lands on video_output_widget
        # itself outside the embedded picture (e.g. a thin margin) --
        # clicks on the picture are forwarded from the child process
        # instead, see _show_video_context_menu_at_cursor.
        self._show_video_context_menu_global(self.video_output_widget.mapToGlobal(pos))

    def _show_video_context_menu_at_cursor(self):
        # The video picture is the embedded child process's own window, so
        # a right-click there is delivered straight to that process by
        # Windows -- it forwards a context_menu_requested event over the
        # IPC protocol instead of video_output_widget's
        # customContextMenuRequested ever firing.
        self._show_video_context_menu_global(QtGui.QCursor.pos())

    def _show_video_context_menu_global(self, global_pos):
        menu = QtWidgets.QMenu(self.video_output_widget)
        label = "Exit Full Screen" if self._video_fullscreen else "Enter Full Screen"
        # Request, not a direct transition: this runs inside QMenu.exec()'s
        # own nested event loop.
        menu.addAction(label, self._on_video_fullscreen_menu_requested)
        menu.exec(global_pos)

    # -- fullscreen request seam -------------------------------------------
    #
    # Every INPUT route funnels through _request_video_fullscreen_state.
    # The physical transition (_enter/_exit_video_fullscreen) is only ever
    # executed from a queued GUI turn, never inline in the originating
    # callback -- the child process's forwarded input arrives inside a
    # QProcess readyReadStandardOutput handler whose read loop is still
    # iterating, and the Escape route arrives inside the application's own
    # eventFilter. The app's INTERNAL lifecycle paths (shutdown, media-type
    # changes, Party Mode routing, video end/error) deliberately keep
    # calling the physical methods directly: those are ordering-critical
    # and several run while the application is closing, where a queued
    # turn may never come. They are protected from re-entrancy by the
    # transition guard inside those methods instead.

    def _record_video_fullscreen_event(self, operation: str, **details) -> None:
        """Bounded fullscreen-transition diagnostics -- booleans, the
        target, a sequence id, a short source label and elapsed_ms only.
        Never a path or a token. Recorded at "basic" (the application's
        default level, and the level video_fullscreen_changed already
        uses): these exist so the next real-device failure can prove
        whether duplicate requests were involved, which they cannot do if
        a default session drops them. One event per input or transition,
        never per frame."""
        diagnostics = getattr(self, "diagnostics", None)
        if diagnostics is None:
            return
        try:
            diagnostics.record(
                "playback", operation, details=details, minimum_level="basic",
            )
        except Exception:
            pass

    def _request_video_fullscreen_state(self, target: bool, source: str = "unknown") -> None:
        """The one entry point for every fullscreen input route.

        Returns immediately to the caller: the transition itself is
        applied from a queued GUI turn once this callback stack has fully
        unwound. Duplicate requests coalesce onto a single pending target,
        so two inputs in the same event-loop turn (e.g. the child's
        forwarded double-click plus a main-window Escape) produce exactly
        one physical transition."""
        target = bool(target)
        if self._video_fullscreen_requests_dead():
            # Shutdown is authoritative. C2 keeps app.exec() running while
            # shutdown is pending, so a request accepted now could still be
            # applied by the normal event loop mid-teardown -- refuse it.
            self._video_fullscreen_pending_target = None
            self._record_video_fullscreen_event(
                "video_fullscreen_request_rejected",
                requested_target=target,
                request_source=str(source),
                reason="shutdown",
            )
            return
        self._record_video_fullscreen_event(
            "video_fullscreen_request",
            requested_target=target,
            current_committed_state=bool(self._video_fullscreen),
            transitioning=bool(self._video_fullscreen_transitioning),
            request_source=str(source),
        )
        if self._video_fullscreen_transitioning:
            # A physical transition is mid-flight. Never nest: record the
            # latest target and let the applier pick it up on a fresh
            # queued turn once the current transition has finished.
            self._video_fullscreen_pending_target = target
            self._record_video_fullscreen_event(
                "video_fullscreen_request_coalesced",
                target=target,
                sequence_id=int(self._video_fullscreen_sequence),
            )
            return
        if (
            self._video_fullscreen_pending_target is None
            and target == bool(self._video_fullscreen)
        ):
            return  # already satisfied, nothing to do
        already_scheduled = bool(self._video_fullscreen_request_scheduled)
        self._video_fullscreen_pending_target = target
        if already_scheduled:
            self._record_video_fullscreen_event(
                "video_fullscreen_request_coalesced",
                target=target,
                sequence_id=int(self._video_fullscreen_sequence),
            )
            return
        self._video_fullscreen_request_scheduled = True
        QtCore.QTimer.singleShot(0, self._apply_pending_video_fullscreen_state)

    def _apply_pending_video_fullscreen_state(self) -> None:
        """Queued applier for _request_video_fullscreen_state."""
        self._video_fullscreen_request_scheduled = False
        if self._video_fullscreen_requests_dead():
            # An already-posted singleShot cannot be cancelled, so this
            # guard is what actually stops a request queued just before
            # closeEvent from running during shutdown.
            self._video_fullscreen_pending_target = None
            return
        if self._video_fullscreen_transitioning:
            # A DIRECT internal transition is mid-flight (this turn was
            # delivered from inside it). Leave the pending target in place:
            # that transition drains it when it finishes.
            return
        target = self._video_fullscreen_pending_target
        self._video_fullscreen_pending_target = None
        if target is not None and bool(target) != bool(self._video_fullscreen):
            if target:
                self._enter_video_fullscreen()
            else:
                self._exit_video_fullscreen()
        # The physical methods drain after themselves; this covers the
        # no-transition case with the same single helper.
        self._drain_pending_video_fullscreen_request()

    def _video_fullscreen_requests_dead(self) -> bool:
        """True once the application is closing -- fullscreen REQUESTS are
        refused and any pending one discarded from then on. The physical
        methods themselves are deliberately NOT gated by this: closeEvent
        still performs its own direct synchronous exit to restore the
        presentation before teardown."""
        return bool(
            getattr(self, "_closing", False)
            or getattr(self, "_shutdown_requested", False)
        )

    def _drain_pending_video_fullscreen_request(self) -> None:
        """Called after EVERY physical transition finishes -- whether it
        came from the queued request seam or from a direct internal
        lifecycle path -- so a request that arrived mid-transition is never
        stranded in _video_fullscreen_pending_target with no timer armed.
        Idempotent; never double-schedules."""
        if self._video_fullscreen_transitioning:
            return
        target = self._video_fullscreen_pending_target
        if target is None:
            return
        if self._video_fullscreen_requests_dead():
            self._video_fullscreen_pending_target = None
            return
        if bool(target) == bool(self._video_fullscreen):
            self._video_fullscreen_pending_target = None
            return
        if not self._video_fullscreen_request_scheduled:
            self._video_fullscreen_request_scheduled = True
            QtCore.QTimer.singleShot(0, self._apply_pending_video_fullscreen_state)

    def _video_fullscreen_effective_intent(self) -> bool:
        """What fullscreen state the application is heading towards right
        now: the latest pending request if any, else the target of a
        physical transition in progress, else the committed state. Used
        for input that must act on intent (Escape during an enter that has
        been requested or is physically running but has not committed)."""
        pending = self._video_fullscreen_pending_target
        if pending is not None:
            return bool(pending)
        if self._video_fullscreen_transitioning:
            transition_target = getattr(self, "_video_fullscreen_transition_target", None)
            if transition_target is not None:
                return bool(transition_target)
        return bool(self._video_fullscreen)

    def _next_video_fullscreen_sequence(self) -> int:
        self._video_fullscreen_sequence = int(self._video_fullscreen_sequence) + 1
        return self._video_fullscreen_sequence

    def _on_child_video_double_clicked(self, *_args) -> None:
        """Child process forwarded a double-click (arrives inside the
        QProcess stdout read loop -- request only, never a transition)."""
        self._request_video_fullscreen_state(
            not self._video_fullscreen, "child_double_click",
        )

    def _on_child_video_escape_pressed(self, *_args) -> None:
        self._request_video_fullscreen_state(False, "child_escape")

    def _on_karaoke_double_clicked(self, *_args) -> None:
        self._request_video_fullscreen_state(
            not self._video_fullscreen, "karaoke_double_click",
        )

    def _on_karaoke_escape_pressed(self, *_args) -> None:
        self._request_video_fullscreen_state(False, "karaoke_escape")

    def _on_video_fullscreen_menu_requested(self, *_args) -> None:
        self._request_video_fullscreen_state(
            not self._video_fullscreen, "context_menu",
        )

    def _toggle_video_fullscreen(self):
        if self._current_media_type not in (MediaType.VIDEO, MediaType.KARAOKE):
            return
        self._request_video_fullscreen_state(not self._video_fullscreen, "toggle")

    def _enter_video_fullscreen(self):
        if self._video_fullscreen_transitioning:
            # Re-entrancy refused: a physical transition is already running.
            self._record_video_fullscreen_event(
                "video_fullscreen_transition_reentry_blocked",
                target=True,
                sequence_id=int(self._video_fullscreen_sequence),
            )
            return
        if self._video_fullscreen or self._current_media_type not in (
            MediaType.VIDEO, MediaType.KARAOKE,
        ):
            return
        sequence_id = self._next_video_fullscreen_sequence()
        started = time.perf_counter()
        self._record_video_fullscreen_event(
            "video_fullscreen_transition_begin",
            target=True, sequence_id=sequence_id,
        )
        stage_owner = None
        restore_state = None
        failure_reason = None
        exception_type = None
        self._video_fullscreen_transitioning = True
        self._video_fullscreen_transition_target = True
        try:
            try:
                stage_owner = self._video_fullscreen_stage_owner()
                if stage_owner is self:
                    restore_state = self._enter_main_video_fullscreen_presentation()
                else:
                    restore_state = stage_owner.enter_video_fullscreen_presentation()
            except Exception as exc:
                # Recorded below; committed state left untouched. Ordinary
                # Exception only -- never BaseException. Not re-raised: this
                # usually runs from a queued GUI slot, where an uncaught
                # exception is far worse than a recorded failed transition.
                restore_state = None
                failure_reason = "exception"
                exception_type = type(exc).__name__
            if restore_state is not None:
                # Committed only once the presentation change has actually
                # succeeded -- never before it runs.
                self._video_fullscreen = True
                self._video_fullscreen_owner_widget = stage_owner
                self._video_fullscreen_restore_state = restore_state
            elif failure_reason is None:
                failure_reason = "presentation_declined"
        finally:
            self._video_fullscreen_transitioning = False
            self._video_fullscreen_transition_target = None
        try:
            if restore_state is None:
                # Deterministic: the committed state is untouched and
                # nothing is retried or toggled recursively.
                self._record_video_fullscreen_event(
                    "video_fullscreen_transition_failed",
                    target=True, sequence_id=sequence_id,
                    elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
                    reason=failure_reason,
                    exception_type=exception_type,
                )
                return
            self._video_backend.schedule_output_geometry_sync()
            self.diagnostics.record(
                "playback", "video_fullscreen_changed",
                details={
                    "fullscreen": True,
                    "owner": "party_mode" if stage_owner is not self else "main_window",
                    "native_window_reparented": False,
                },
                minimum_level="basic",
            )
            self._record_video_fullscreen_event(
                "video_fullscreen_transition_complete",
                target=True, sequence_id=sequence_id,
                elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
            )
        finally:
            # After EVERY physical transition, success or failure, direct
            # internal caller or queued seam alike.
            self._drain_pending_video_fullscreen_request()

    def _exit_video_fullscreen(self):
        if self._video_fullscreen_transitioning:
            # Re-entrancy refused. This is the case the real-device trace
            # implicates: the restore below hides/shows dozens of widgets
            # and performs a native window-state change, during which Qt
            # can deliver further input -- previously each such delivery
            # went straight back into this same native restore path.
            self._record_video_fullscreen_event(
                "video_fullscreen_transition_reentry_blocked",
                target=False,
                sequence_id=int(self._video_fullscreen_sequence),
            )
            return
        if not self._video_fullscreen:
            return
        sequence_id = self._next_video_fullscreen_sequence()
        started = time.perf_counter()
        self._record_video_fullscreen_event(
            "video_fullscreen_transition_begin",
            target=False, sequence_id=sequence_id,
        )
        stage_owner = self._video_fullscreen_owner_widget
        restore_state = self._video_fullscreen_restore_state
        physical_ok = False
        exception_type = None
        self._video_fullscreen_transitioning = True
        self._video_fullscreen_transition_target = False
        try:
            try:
                if stage_owner is self:
                    self._exit_main_video_fullscreen_presentation(restore_state)
                elif stage_owner is not None:
                    stage_owner.exit_video_fullscreen_presentation(restore_state)
                else:
                    # Defensive recovery for sessions restored from an
                    # older state.
                    self.showNormal()
                physical_ok = True
            except Exception as exc:
                # Deterministic on failure: the snapshot has been consumed
                # and cannot be replayed, so the logical state is still
                # reconciled below rather than left claiming fullscreen.
                # Ordinary Exception only -- never BaseException.
                exception_type = type(exc).__name__
            # Committed only now, with the physical restore finished. This
            # used to be set before the restore ran, so a re-entrant
            # toggle arriving mid-restore saw _video_fullscreen == False
            # and tried to ENTER fullscreen while the window was still
            # physically restoring out of it.
            self._video_fullscreen = False
            self._video_fullscreen_owner_widget = None
            self._video_fullscreen_restore_state = None
        finally:
            self._video_fullscreen_transitioning = False
            self._video_fullscreen_transition_target = None
        try:
            # Presentation-state correction only (never touches playback):
            # if the track changed to audio while fullscreen was active,
            # the restored stage must not be left on a video page.
            self._reconcile_display_stage_with_current_media()
            self._video_backend.schedule_output_geometry_sync()
            if physical_ok:
                self.diagnostics.record(
                    "playback", "video_fullscreen_changed",
                    details={"fullscreen": False, "native_window_reparented": False},
                    minimum_level="basic",
                )
                self._record_video_fullscreen_event(
                    "video_fullscreen_transition_complete",
                    target=False, sequence_id=sequence_id,
                    elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
                )
            else:
                # One failed physical transition is reported ONLY as
                # failed -- never also as changed/complete. The logical
                # reconciliation it still performed is stated explicitly.
                self._record_video_fullscreen_event(
                    "video_fullscreen_transition_failed",
                    target=False, sequence_id=sequence_id,
                    elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
                    reason="exception",
                    exception_type=exception_type,
                    logical_state_reconciled=True,
                )
        finally:
            # After EVERY physical transition, success or failure -- this
            # includes closeEvent's own direct exit during shutdown, where
            # the drain discards any pending request instead.
            self._drain_pending_video_fullscreen_request()

    def _video_fullscreen_stage_owner(self):
        party_mode = getattr(self, "party_mode", None)
        if (
            party_mode is not None
            and party_mode.isVisible()
            and getattr(party_mode, "_video_active", False)
        ):
            return party_mode
        return self

    def _main_video_fullscreen_hidden_widgets(self):
        names = (
            "now_playing", "dj_info", "search_box", "scan_bar",
            # Stage 3A-r3 real-device defect: the source-selector row
            # (Local/Plex, "Refresh Plex") was never covered here at all --
            # a gap that predates Plex entirely (nothing here is
            # Plex-specific; Local video fullscreen has always left it
            # showing). Now covered by library_column_container below,
            # along with library_tabs itself -- hiding the ONE container
            # widget hides everything inside it (source row + placeholder
            # + tabs) without needing a per-child entry, and also lets
            # QHBoxLayout reclaim its stretch-allocated width (see
            # _build_ui's comment on why this must be a widget, not a
            # bare layout item).
            "library_column_container", "right_tabs", "btn_prev", "btn_play",
            "btn_pause", "btn_next", "output_combo", "slider_progress",
            "label_remaining", "label_volume", "slider_volume",
            "btn_add", "btn_rescan",
        )
        widgets = [getattr(self, name, None) for name in names]
        try:
            widgets.append(self.statusBar())
        except Exception:
            pass
        try:
            # Acceptance defect: a session with the top menu bar enabled
            # (View > Show Menu Bar) kept it visible throughout fullscreen.
            # Folded into the existing widget_visibility capture/restore
            # below rather than special-cased, so a session where it was
            # already hidden stays hidden on exit too.
            widgets.append(self.menuBar())
        except Exception:
            pass
        return tuple(widget for widget in widgets if widget is not None)

    def _enter_main_video_fullscreen_presentation(self):
        """Fullscreen the main stage without moving the embedded QWindow."""
        root_layout = self.centralWidget().layout()
        widgets = self._main_video_fullscreen_hidden_widgets()
        splitter_handle = self.right_splitter.handle(1)
        restore_state = {
            "window_state": self.windowState(),
            "window_geometry": self.saveGeometry(),
            "root_margins": root_layout.getContentsMargins(),
            "root_spacing": root_layout.spacing(),
            "widget_visibility": tuple(
                (widget, not widget.isHidden()) for widget in widgets
            ),
            "splitter_handle_visible": not splitter_handle.isHidden(),
            # Stage 3A-r3 real-device defect: hiding right_tabs above lets
            # QSplitter redistribute its space entirely to right_display_
            # stack -- QSplitter.setVisible(True) on the child later does
            # not reliably reproduce the original proportions on its own
            # (it's a size-restore heuristic, not a guarantee), especially
            # combined with the root layout's own margins/spacing changing
            # at the same time. Capture the real pixel split explicitly
            # and restore it explicitly on exit instead of hoping Qt's
            # show/hide bookkeeping gets it back exactly right -- this is
            # what "queue is pushed down, video/container geometry is
            # wrong" after exiting fullscreen traces back to.
            "splitter_sizes": list(self.right_splitter.sizes()),
        }
        for widget, _visible in restore_state["widget_visibility"]:
            widget.hide()
        splitter_handle.hide()
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.showFullScreen()
        self.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
        return restore_state

    def _exit_main_video_fullscreen_presentation(self, restore_state):
        """Leave fullscreen as ONE visible transition.

        Real-device defect (screen recording, ~14.0-14.5s): exiting
        fullscreen from a maximised window visibly passed through three
        separate states -- the half-restored layout painted at fullscreen
        size ("video shrinks into the right side of a mostly black
        screen"), then a small windowed frame (showNormal() +
        restoreGeometry()), then a maximise animation
        (setWindowState(saved)). The ordering itself was wrong, not just
        unbuffered: showNormal() forced a windowed state the window was
        never in before fullscreen, purely so setWindowState() could undo
        it a moment later.

        Now: every layout/widget/splitter change happens with this
        window's repaints suppressed, and the window state goes DIRECTLY
        from fullscreen to whatever was captured on entry -- no windowed
        intermediate at all when the window was maximised. The
        updates-disabled window is there to keep a half-restored layout
        off screen, not to hide an incorrect state machine: the state
        transition below is a single step by construction."""
        if not isinstance(restore_state, dict):
            self.showNormal()
            return
        saved_window_state = restore_state.get(
            "window_state", QtCore.Qt.WindowState.WindowNoState,
        )
        # The captured state is from before showFullScreen(), so it should
        # not carry the fullscreen bit -- masked anyway so a restore can
        # never put the window back INTO fullscreen.
        target_state = saved_window_state & ~QtCore.Qt.WindowState.WindowFullScreen
        was_maximized = bool(target_state & QtCore.Qt.WindowState.WindowMaximized)
        self.setUpdatesEnabled(False)
        try:
            root_layout = self.centralWidget().layout()
            margins = restore_state.get("root_margins", (18, 16, 18, 16))
            root_layout.setContentsMargins(*margins)
            root_layout.setSpacing(int(restore_state.get("root_spacing", 12)))
            for widget, visible in restore_state.get("widget_visibility", ()):
                try:
                    widget.setVisible(bool(visible))
                except RuntimeError:
                    pass
            try:
                self.right_splitter.handle(1).setVisible(
                    bool(restore_state.get("splitter_handle_visible", True))
                )
            except Exception:
                pass
            geometry = restore_state.get("window_geometry")
            if geometry is not None and not was_maximized:
                # A genuinely windowed session needs its exact
                # pre-fullscreen geometry back, so this is applied while
                # still fullscreen (where it only records the normal
                # geometry) and the single state change below lands on
                # it. Deliberately NOT applied for a maximised session:
                # forcing a windowed geometry first is exactly what made
                # the maximise visible.
                self.restoreGeometry(geometry)
            self.setWindowState(target_state)
            splitter_sizes = restore_state.get("splitter_sizes")
            if splitter_sizes:
                # After the state change, not before: these are pixel
                # sizes captured at the pre-fullscreen window size, so
                # applying them while the window is still fullscreen-sized
                # makes QSplitter scale them to the wrong height and then
                # re-proportion them again on the way back down.
                try:
                    self.right_splitter.setSizes([int(size) for size in splitter_sizes])
                except Exception:
                    pass
        finally:
            self.setUpdatesEnabled(True)
        self.update()

    def play_path(self, path: str, crossfade: bool = False):
        index = self.track_index_by_path.get(path)
        if index is None:
            return self._play_path_direct(path, crossfade=crossfade, index=None)
        return self.play_index(index, crossfade=crossfade)

    def _clear_synced_lyrics_state(self) -> None:
        self._lyrics = []
        self._lyric_times = []
        self._lyric_idx = None
        try:
            if getattr(self, "overlay", None) is not None:
                self.overlay.clear_lyric()
        except Exception:
            pass

    def _load_lrc_for_track(self, path: str, generation: Optional[int] = None):
        """v1.0.67 MainThread I/O hardening: this used to open the media
        file with Mutagen (and, on a miss, read a sidecar .lrc file)
        synchronously right here -- the same class of NAS-stall bug the
        video/karaoke branch next to this call site was already fixed for
        (see _activate_track_ui's comment on that fix). The actual read now
        happens on LyricsLoadWorker; this only ever consumes an in-memory
        cache immediately, or kicks off that worker and returns."""
        generation = (
            getattr(
                self, "_now_playing_lyric_generation",
                self._now_playing_generation.identity.generation,
            )
            if generation is None else generation
        )
        self._clear_synced_lyrics_state()
        if getattr(self, "_closing", False):
            return
        cached = self._lyrics_cache.get(path)
        if cached is not None:
            entries, source = cached
            if entries:
                self._set_synced_lyrics(entries)
            self.diagnostics.record(
                "lyrics", "load_synced_lyrics",
                details={
                    "source": source, "lines": len(entries), "cache_hit": True,
                    **self.diagnostics.path_details(path),
                },
                minimum_level="detailed",
            )
            return
        diagnostic_started = time.perf_counter()
        worker = LyricsLoadWorker(path)
        self._lyrics_load_workers.append(worker)
        token = self._worker_registry.register("lyrics_load", thread=worker, wait_ms=1500)

        def _on_ready(loaded_path, entries, source, started=diagnostic_started):
            self._lyrics_cache[loaded_path] = (entries, source)
            if getattr(self, "_closing", False):
                return
            if not self._now_playing_generation.is_current(generation, loaded_path):
                self._record_stale_now_playing_detail("lyrics", generation, loaded_path)
                return
            if entries:
                self._set_synced_lyrics(entries)
                self._log(
                    f"Synced lyrics loaded ({source}): "
                    f"{os.path.basename(loaded_path)} ({len(entries)} lines)"
                )
            self.diagnostics.record(
                "lyrics", "load_synced_lyrics",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                details={
                    "source": source, "lines": len(entries),
                    **self.diagnostics.path_details(loaded_path),
                },
                minimum_level="detailed",
            )

        def _on_finished(worker=worker, token=token):
            if worker in self._lyrics_load_workers:
                self._lyrics_load_workers.remove(worker)
            self._worker_registry.unregister(token)

        worker.lyrics_ready.connect(_on_ready)
        worker.finished.connect(_on_finished)
        worker.start()

    def _set_synced_lyrics(self, entries):
        entries = [(float(t), clean_text(str(text)).strip()) for t, text in entries if str(text).strip()]
        entries.sort(key=lambda item: item[0])
        self._lyrics = entries
        self._lyric_times = [t for t, _ in entries]

    def _parse_lrc_text(self, text: str):
        # v1.0.67: delegates to lyrics.py so LyricsLoadWorker (off-thread)
        # and this class share one implementation.
        return lyrics_module.parse_lrc_text(text)

    def _load_synced_lyrics_from_tags(self, path: str):
        return lyrics_module.extract_synced_lyrics_from_tags(path)

    def _tag_values_for_keys(self, tags, keys: Tuple[str, ...]) -> List[str]:
        return lyrics_module.tag_values_for_keys(tags, keys)

    def _coerce_tag_text_values(self, value) -> List[str]:
        return lyrics_module.coerce_tag_text_values(value)

    def _lyrics_tick(self):
        if not getattr(self, "lyrics_enabled", True):
            if self._lyric_idx is not None:
                self._lyric_idx = None
                try:
                    self.overlay.clear_lyric()
                except Exception:
                    pass
            return
        if not self._lyrics:
            return
        t = self._player_clock_s()
        if t is None:
            return
        t += self.lyric_time_offset_ms / 1000.0
        idx = bisect.bisect_right(self._lyric_times, t + 0.05) - 1
        if idx < 0:
            if self._lyric_idx is not None:
                self._lyric_idx = None
                try:
                    self.overlay.clear_lyric()
                except Exception:
                    pass
            return
        idx = min(idx, len(self._lyrics) - 1)
        if idx == self._lyric_idx:
            return
        self._lyric_idx = idx
        try:
            current = self._lyrics[idx][1]
            if not current:
                self.overlay.clear_lyric()
                return
            self.overlay.set_lyric(current)
        except Exception:
            pass

    def _audio_log(self, message: str):
        self._log("AUDIO: " + message)
        lowered = message.lower()
        if " play start" in lowered or lowered.startswith("play start"):
            self.diagnostics.counters["tracks_started"] += 1
            self.diagnostics.counters[
                f"backend_{self._current_backend_name()}"
            ] += 1
        if "playback recovery started" in lowered:
            self.diagnostics.counters["recovery_attempts"] += 1
        if "playback recovery complete" in lowered:
            self.diagnostics.counters["recovery_successes"] += 1
        if "crossfade start" in lowered:
            self.diagnostics.counters["crossfades_attempted"] += 1
        if "crossfade complete" in lowered:
            self.diagnostics.counters["crossfades_completed"] += 1
            self._record_track_completion(
                "crossfade",
                generation=getattr(
                    self, "_crossfade_outgoing_generation", None
                ),
            )
        severity = (
            "severe" if any(word in lowered for word in (
                "failed", "error", "stalled", "quarantine", "device"
            ))
            else "warning" if any(word in lowered for word in (
                "recovery", "fallback", "retry", "underrun"
            ))
            else "info"
        )
        operation = (
            "crossfade" if "crossfade" in lowered
            else "playback_recovery" if "recovery" in lowered
            else "backend_playback"
        )
        if operation == "crossfade" and self._diagnostic_crossfade_id is None:
            self._diagnostic_crossfade_id = uuid.uuid4().hex
        if operation == "playback_recovery" and self._diagnostic_recovery_id is None:
            self._diagnostic_recovery_id = uuid.uuid4().hex
        correlation_id = (
            self._diagnostic_crossfade_id
            if operation == "crossfade"
            else self._diagnostic_recovery_id
            if operation == "playback_recovery"
            else None
        )
        self.diagnostics.record(
            "crossfade" if operation == "crossfade" else "audio",
            operation,
            correlation_id=correlation_id,
            status="failure" if severity == "severe" else "success",
            severity=severity,
            details={"message": message},
            minimum_level="basic" if severity != "info" else "detailed",
            rate_limit_seconds=0.25 if severity != "info" else 0,
        )
        if operation == "crossfade" and any(
            word in lowered for word in ("complete", "failed", "cancel")
        ):
            self._diagnostic_crossfade_id = None
        if operation == "playback_recovery" and any(
            word in lowered for word in ("complete", "failed", "cancel")
        ):
            self._diagnostic_recovery_id = None

    def _audio_name(self, path: str) -> str:
        try:
            return os.path.basename(path)
        except Exception:
            return str(path)

    def _current_backend_name(self) -> str:
        if self._temporary_backend_override:
            return self._temporary_backend_override
        if self._use_builtin_player():
            return str(self.builtin_backend or "miniaudio").lower()
        return "vlc"

    def _available_recovery_backends(self) -> List[str]:
        available = []
        if VLC_AVAILABLE:
            available.append("vlc")
        if self.miniaudio_player is not None:
            available.append("miniaudio")
        if self.bass_player is not None:
            available.append("bass")
        return available

    def _record_playback_backend_failure(self, backend: str):
        now = time.monotonic()
        quarantined = record_backend_failure(
            self._backend_failure_times,
            self._backend_quarantined_until,
            backend,
            now,
        )
        if quarantined:
            self._audio_log(
                f"playback recovery quarantine; backend={backend}; seconds=300"
            )

    def _arm_playback_watchdog(self, requested_position: float = 0.0):
        now = time.monotonic()
        self._playback_watch_started = now
        self._playback_watch_last_advance = now
        self._playback_watch_last_position = max(
            0.0, float(requested_position or 0.0)
        )
        self._playback_watch_requested_position = self._playback_watch_last_position
        self._playback_watch_has_advanced = False
        self._playback_expected = True

    def _cancel_playback_watchdog(self):
        self._playback_expected = False
        self._playback_watch_started = 0.0
        self._playback_watch_last_advance = 0.0
        self._playback_watch_has_advanced = False

    def _playback_health_snapshot(self):
        backend = self._current_backend_name()
        if backend in ("miniaudio", "bass"):
            player = self.simple_player
            try:
                return (
                    bool(player and player.is_playing()),
                    float(player.get_pos() or 0.0) if player else 0.0,
                    float(player.get_length() or 0.0) if player else 0.0,
                    None,
                )
            except Exception as ex:
                return False, 0.0, 0.0, ex
        player = self.active_player
        if not player:
            return False, 0.0, 0.0, None
        try:
            state = player.get_state()
            return (
                bool(player.is_playing()),
                max(0.0, float(player.get_time() or 0) / 1000.0),
                max(0.0, float(player.get_length() or 0) / 1000.0),
                state,
            )
        except Exception as ex:
            return False, 0.0, 0.0, ex

    def _check_playback_health(self):
        if self._current_media_type == MediaType.VIDEO:
            # The audio recovery watchdog is never armed for video (see
            # _play_video_path_direct) -- this guard additionally stops it
            # from misfiring off stale state left behind by whatever audio
            # track played before the video started.
            video_backend = getattr(self, "_video_backend", None)
            if video_backend is None:
                return
            duration_ms = video_backend.duration_ms()
            position_ms = video_backend.position_ms()
            if duration_ms > 0:
                if not self.scrubbing:
                    self._update_progress(position_ms, duration_ms)
                self._record_video_timing_available(position_ms, duration_ms)
            elif (
                self._playback_expected
                and not self._video_progress_warning_reported
                and self._video_progress_started_at > 0
                and time.monotonic() - self._video_progress_started_at >= 3.0
            ):
                self._video_progress_warning_reported = True
                self.diagnostics.record(
                    "playback", "video_timing_unavailable",
                    status="warning", severity="warning",
                    details=self.diagnostics.path_details(self.current_path or ""),
                    minimum_level="basic",
                )
            return
        if is_plex_identity(self.current_path):
            # Real-device bug (Stage 3A-r2): this watchdog's stall
            # detection/grace timing was tuned for Local files, where
            # BASS_StreamCreateFile on a local/network-share path resolves
            # near-instantly. A Plex track's genuine startup is an async
            # network round-trip (PlexPlaybackResolveWorker, then
            # BassStreamPrepareWorker's BASS_StreamCreateURL) that can easily
            # exceed PLAYBACK_START_GRACE_SECONDS even when nothing is
            # actually wrong -- and if this fires, _begin_playback_recovery's
            # fallback ladder (_try_recovery_backend) feeds self.current_path
            # (a synthetic plex://.../<ratingKey>.<ext> identity, never a
            # real file or HTTP URL) directly into BASS_StreamCreateFile /
            # VLC's media_new / miniaudio's loader -- none of which
            # understand it. BASS fails immediately and gets quarantined;
            # VLC "opens" the bogus URI and instantly reports Ended, which
            # _begin_playback_recovery treats as a successful fallback and
            # leaves _temporary_backend_override stuck on "vlc" -- even
            # though the real, concurrently-resolving Plex BASS load (which
            # has no way to know any of this happened) goes on to genuinely
            # start playing moments later. That stuck override then fools
            # _use_builtin_player()/_use_bass_backend() on the very next
            # _analyzer_tick into treating the errant VLC player's Ended
            # state as this track's real state (an immediate, spurious
            # _next_track("vlc-ended") -- the observed double "Switch to
            # BASS" prompt plus premature auto-advance) and into skipping
            # the live BASS FFT visualiser dispatch (which also gates on
            # _use_bass_backend()). Plex has its own dedicated async
            # resolve/load pipeline with its own generation/token staleness
            # checks (_playback_generation, _plex_audio_load_token) --
            # this Local-file-oriented watchdog must never run for it.
            return
        if (
            not self.auto_playback_recovery
            or not self._playback_expected
            or self._playback_recovery_active
            or self._playback_intentionally_paused
            or getattr(self, "_closing", False)
            or self.scrubbing
            or self.fade_active
            or self.prebuffer_active
            or self.pending_next
            or not self.current_path
        ):
            return
        playing, position, length, state = self._playback_health_snapshot()
        if length > 0 and (length < 4.0 or position >= max(0.0, length - 1.5)):
            return
        backend = self._current_backend_name()
        if backend == "vlc" and VLC_AVAILABLE:
            if state == vlc.State.Error:
                self._begin_playback_recovery(
                    "backend-error", backend, error=state
                )
                return
            if state in (vlc.State.Opening, vlc.State.Buffering):
                self._playback_watch_last_advance = time.monotonic()
                return
        now = time.monotonic()
        if position >= (
            self._playback_watch_last_position
            + STALL_POSITION_TOLERANCE_SECONDS
        ):
            self._playback_watch_last_position = position
            self._playback_watch_last_advance = now
            self._playback_watch_has_advanced = True
            return
        elapsed = now - self._playback_watch_started
        stalled_for = now - self._playback_watch_last_advance
        if elapsed < PLAYBACK_START_GRACE_SECONDS:
            return
        if not playing and stalled_for >= PLAYBACK_START_GRACE_SECONDS:
            reason = (
                "startup-stall"
                if position <= self._playback_watch_requested_position + 0.25
                else "backend-error"
            )
            self._begin_playback_recovery(reason, backend)
        elif (
            playing
            and self._playback_watch_has_advanced
            and stalled_for >= STALL_CONFIRMATION_SECONDS
        ):
            self._begin_playback_recovery("midtrack-stall", backend)

    def _begin_playback_recovery(
        self, reason: str, backend: str, error=None,
        path: Optional[str] = None, position: Optional[float] = None,
    ) -> bool:
        if not self.auto_playback_recovery or self._playback_recovery_active:
            return False
        identity_path = self.current_path
        if path is None and self._current_media_type == MediaType.KARAOKE:
            path = getattr(self, "_karaoke_audio_path", None)
        path = path or identity_path
        if not path or not identity_path or getattr(self, "_closing", False):
            return False
        if is_plex_identity(identity_path) or is_plex_identity(path):
            # Defense in depth alongside _check_playback_health's own
            # guard: no caller of this function may run the Local-file
            # fallback ladder (_try_recovery_backend -- raw
            # BASS_StreamCreateFile / VLC media_new / miniaudio.load
            # against a synthetic plex:// identity) for Plex audio. See
            # the matching comment in _check_playback_health for the full
            # failure chain this prevents.
            return False
        generation = self._playback_generation
        if position is None:
            _, position, _, _ = self._playback_health_snapshot()
        position = max(0.0, float(position or 0.0))
        resume_position = recovery_resume_position(position)
        self._playback_recovery_active = True
        recovery_started = time.perf_counter()
        self._record_playback_backend_failure(backend)
        self._audio_log(
            f"playback recovery started; reason={reason}; backend={backend}; "
            f"position={position:.2f}; generation={generation}; error={error!r}"
        )
        self.statusBar().showMessage(
            f"Playback stalled \u2014 retrying {backend.upper()}\u2026"
        )
        now = time.monotonic()
        quarantined = [
            name for name in self._available_recovery_backends()
            if is_backend_quarantined(
                self._backend_quarantined_until, name, now
            )
        ]
        order = ordered_recovery_backends(
            backend, self._available_recovery_backends(), quarantined
        )
        if not self.allow_backend_fallback:
            order = [name for name in order if name == backend]
        for attempt_backend in order:
            if generation != self._playback_generation or identity_path != self.current_path:
                self._playback_recovery_active = False
                return False
            if self._playback_recovery_attempts.get(attempt_backend, 0) >= 1:
                continue
            self._playback_recovery_attempts[attempt_backend] = 1
            if attempt_backend != backend:
                self.statusBar().showMessage(
                    f"{backend.upper()} failed \u2014 trying {attempt_backend.upper()}\u2026"
                )
            attempt_started = time.perf_counter()
            try:
                success, seeked = self._try_recovery_backend(
                    attempt_backend, path, resume_position, generation
                )
                attempt_error = None
            except Exception as ex:
                success, seeked, attempt_error = False, False, ex
            self._audio_log(
                f"playback recovery attempt; backend={attempt_backend}; "
                f"attempt={len(self._playback_recovery_attempts)}; "
                f"open_ms={(time.perf_counter() - attempt_started) * 1000.0:.1f}; "
                f"seek={seeked}; result={'success' if success else 'failed'}; "
                f"error={attempt_error!r}"
            )
            if success:
                self._temporary_backend_override = (
                    attempt_backend
                    if attempt_backend != self._configured_preferred_backend()
                    else None
                )
                self._playback_recovery_active = False
                self._playback_intentionally_paused = False
                self._arm_playback_watchdog(resume_position)
                self._lyric_idx = None
                self.beat.setPlaying(True)
                self.btn_pause.setText("Pause")
                self.statusBar().showMessage(
                    f"Playback recovered using {attempt_backend.upper()}", 5000
                )
                self._audio_log(
                    f"playback recovery complete; from_backend={backend}; "
                    f"to_backend={attempt_backend}; position={resume_position:.2f}; "
                    f"duration_ms={(time.perf_counter() - recovery_started) * 1000.0:.1f}"
                )
                return True
            self._record_playback_backend_failure(attempt_backend)
        self._playback_recovery_active = False
        self._cancel_playback_watchdog()
        self._stop_all()
        self.beat.setPlaying(False)
        self.statusBar().showMessage(
            "Unable to play this track with the available audio backends",
            10000,
        )
        self._audio_log(
            f"playback recovery failed; reason={reason}; generation={generation}; "
            f"backends={order!r}; duration_ms={(time.perf_counter() - recovery_started) * 1000.0:.1f}"
        )
        return False

    def _configured_preferred_backend(self) -> str:
        return (
            str(self.builtin_backend or "miniaudio").lower()
            if self.use_simple
            else "vlc"
        )

    def _try_recovery_backend(
        self, backend: str, path: str, position: float, generation: int,
    ):
        self._cancel_fade()
        self._stop_all()
        if backend in ("miniaudio", "bass"):
            if backend == "bass":
                player, inactive = self.bass_player, self.bass_inactive_player
            else:
                player, inactive = (
                    self.miniaudio_player, self.miniaudio_inactive_player
                )
            if not player:
                return False, False
            self._set_player_topology(player, inactive, reason="playback_recovery")
            self._temporary_backend_override = backend
            player.load(path)
            seeked = False
            if position > 0:
                player.seek(position)
                seeked = True
            self._active_normalisation_gain = self._cached_gain_for_path(path)
            player.set_volume(
                combine_volume(
                    self.master_volume / 100.0,
                    self._active_normalisation_gain,
                    self._sleep_timer_gain,
                )
            )
            player.play()
            return bool(player.is_playing()), seeked
        if backend == "vlc" and VLC_AVAILABLE:
            self._temporary_backend_override = "vlc"
            self._ensure_vlc()
            if not self.active_player:
                return False, False
            if not self._play_on_player(self.active_player, path, 1.0):
                return False, False
            seeked = False
            if position > 0:
                self.active_player.set_time(int(position * 1000.0))
                seeked = True
            return True, seeked
        return False, False

    def _current_rms_db(self):
        """Latest analyzer loudness, preferring the worker that feeds the visualiser."""
        try:
            if self.analyzer_worker and self.analyzer_worker.isRunning():
                rms = self.analyzer_worker.last_rms_db
                if rms is not None:
                    return float(rms)
        except Exception:
            pass
        try:
            if self.analyzer and self.analyzer.last_rms_db is not None:
                return float(self.analyzer.last_rms_db)
        except Exception:
            pass
        return None

    def _play_on_player(self, player, path: str, volume_scale: float):
        if not player:
            return False
        media = self.instance.media_new(path)
        try:
            lower_path = path.lower()
            if lower_path.startswith(("http://", "https://", "ftp://", "smb://")):
                media.add_option(f":network-caching={VLC_NETWORK_CACHING_MS}")
            elif _is_network_file_path(path):
                media.add_option(f":file-caching={VLC_NETWORK_SHARE_CACHING_MS}")
        except Exception:
            pass
        player.set_media(media)
        result = player.play()
        if result == -1:
            self._audio_log(
                f"backend=vlc play failed; file={self._audio_name(path)!r}"
            )
            return False
        self._set_playing_button_state()
        self._audio_log(f"backend=vlc play start; file={self._audio_name(path)!r}")
        try:
            player.audio_set_volume(int(self.master_volume))
        except Exception:
            pass
        target = "inactive" if player is self.inactive_player else "active"
        gain = self._cached_gain_for_path(path, target=target)
        if target == "inactive":
            self._inactive_normalisation_gain = gain
        else:
            self._active_normalisation_gain = gain
        self._set_volume(player, volume_scale)
        return True

    def _play_simple(self, path: str) -> bool:
        backend = self._backend_label().lower()
        if not self.simple_player:
            import_error = BASS_IMPORT_ERROR if backend == "bass" else MINIAUDIO_IMPORT_ERROR
            if import_error is not None:
                self._audio_log(f"backend={backend} unavailable; error={import_error}")
            return False
        load_t0 = time.perf_counter()
        try:
            self.simple_player.load(path)
            load_ms = int((time.perf_counter() - load_t0) * 1000)
            self._active_normalisation_gain = self._cached_gain_for_path(path)
            self.simple_player.set_volume(combine_volume(self.master_volume / 100.0, self._active_normalisation_gain, self._sleep_timer_gain))
            self.simple_player.play()
            self._set_playing_button_state()
            stats = self.simple_player.stats()
            self._audio_log(
                f"backend={backend} load ok; file={self._audio_name(path)!r}; "
                f"duration={stats['duration']:.2f}s; rate={stats['sample_rate']}; "
                f"channels={stats['channels']}; load_ms={load_ms}"
            )
            self._audio_log(f"backend={backend} play start; file={self._audio_name(path)!r}")
            return True
        except Exception as ex:
            load_ms = int((time.perf_counter() - load_t0) * 1000)
            self._audio_log(f"backend={backend} load/play failed; file={self._audio_name(path)!r}; load_ms={load_ms}; error={ex}")
            try:
                self.simple_player.stop()
            except Exception:
                pass
            return False

    def _stop_all(self):
        if self.simple_player:
            try:
                self.simple_player.stop()
            except Exception:
                pass
        if self.simple_inactive_player:
            try:
                self.simple_inactive_player.stop()
            except Exception:
                pass
        if self.active_player:
            try:
                self.active_player.stop()
            except Exception:
                pass
        if self.inactive_player:
            try:
                self.inactive_player.stop()
            except Exception:
                pass

    def _start_miniaudio_crossfade_to(
        self, path: str, immediate: bool = False, index: Optional[int] = None,
    ) -> bool:
        backend = self._backend_label().lower()
        if not self.simple_player or not self.simple_inactive_player:
            return False
        if self.prebuffer_active:
            return True
        try:
            self.simple_inactive_player.stop()
        except Exception as ex:
            self._audio_log(f"backend={backend} crossfade failed; file={self._audio_name(path)!r}; error={ex}")
            return False
        # Preparation reads/scans the file header -- fast locally but
        # sometimes many seconds on a network share -- so it runs on a
        # worker thread and the rest of this sequence (silent play, gain,
        # fade scheduling) continues from _on_crossfade_load_prepared once
        # it reports back, instead of blocking the GUI thread here. Phase
        # C1: the worker never receives a live player -- it only prepares
        # a private candidate; the GUI thread decides commit vs. discard.
        self.prebuffer_active = True
        self.pending_builtin_crossfade_path = path
        self.pending_builtin_crossfade_index = (
            index if index is not None
            else getattr(self, "track_index_by_path", {}).get(path)
        )
        self._pending_crossfade_immediate = bool(immediate)
        self._crossfade_load_token += 1
        token = self._crossfade_load_token
        # Playback stability hardening, Phase A: captured now (this runs
        # inside the same _play_path_direct call that already established
        # the attempt), threaded through via the connect() lambdas --
        # additional defence alongside the existing crossfade token check.
        attempt = self._current_playback_attempt
        attempt_id = attempt.attempt_id if attempt is not None else None
        use_bass = self._use_bass_backend()
        lease = self._make_target_lease("bass" if use_bass else "miniaudio", "inactive")
        if use_bass:
            if _BassEngine is not None:
                try:
                    _BassEngine.ensure()
                except Exception:
                    pass  # surfaced instead via the worker's own `failed` signal below
            worker = BassStreamPrepareWorker(path, token)
        else:
            worker = MiniaudioSourcePrepareWorker(path, token)
        self._crossfade_load_worker = worker
        registry_token = self._worker_registry.register(
            "crossfade_load", thread=worker, wait_ms=2000,
            finalize_after_join=lambda w=worker: self._finalize_unclaimed_prepare_candidate(w, "crossfade_load_join"),
        )
        worker.prepared.connect(
            lambda tok, p, _candidate, w=worker: self._on_crossfade_load_prepared(
                tok, p, w.claim_candidate(), attempt_id, lease,
            )
        )
        worker.failed.connect(
            lambda tok, p, err: self._on_crossfade_load_failed(tok, p, err, attempt_id)
        )
        worker.finished.connect(
            lambda w=worker, t=registry_token: self._on_crossfade_load_worker_finished(w, t)
        )
        worker.start()
        return True

    def _on_crossfade_load_worker_finished(self, worker, registry_token) -> None:
        if self._closing:
            self._finalize_unclaimed_prepare_candidate(worker, "shutdown_worker_finished")
        if self._crossfade_load_worker is worker:
            self._crossfade_load_worker = None
        self._worker_registry.unregister(registry_token)
        self._maybe_resume_final_shutdown()

    def _crossfade_load_is_current(self, token: int, path: str) -> bool:
        return (
            token == self._crossfade_load_token
            and self.prebuffer_active
            and self.pending_builtin_crossfade_path == path
        )

    def _fail_pending_crossfade(self, backend: str, path: str, error) -> None:
        self._audio_log(f"backend={backend} crossfade failed; file={self._audio_name(path)!r}; error={error}")
        try:
            self.simple_inactive_player.stop()
        except Exception:
            pass
        self.prebuffer_active = False
        failed_path = self.pending_builtin_crossfade_path
        failed_index = self.pending_builtin_crossfade_index
        self.pending_builtin_crossfade_index = None
        self.pending_builtin_crossfade_path = None
        self.pending_builtin_crossfade_quiet = False
        if failed_path:
            self._activate_track_ui(failed_index, failed_path)
            self._begin_playback_recovery(
                "crossfade-failed", self._current_backend_name(),
                error=error, path=failed_path, position=0.0,
            )

    def _on_crossfade_load_failed(
        self, token: int, path: str, error: str, attempt_id: Optional[int] = None,
    ):
        if not self._require_current_playback_attempt(attempt_id, "crossfade_load_failed"):
            return
        if getattr(self, "_closing", False) or token != self._crossfade_load_token:
            return  # superseded by a later request; nothing to undo here
        self._fail_pending_crossfade(self._backend_label().lower(), path, error)
        self._advance_playback_attempt_state(attempt_id, PlaybackAttemptState.FAILED)

    def _on_crossfade_load_prepared(
        self, token: int, path: str, candidate, attempt_id: Optional[int] = None,
        lease: Optional[PlayerTargetLease] = None,
    ):
        """Phase C1: replaces the old _on_crossfade_load_succeeded, which
        assumed the worker had already mutated a live inactive-slot
        player. `candidate` (a PreparedBassStream or
        PreparedMiniaudioSource) has not touched any player -- every
        rejection branch below discards it; only the final, fully-
        validated branch commits it to the specific physical player
        `lease` was captured for.

        Phase C2: `candidate` arrives via worker.claim_candidate() --
        None means someone else (shutdown finalizer, or this worker's
        own `finished` handler) already claimed and resolved it."""
        if candidate is None:
            return
        if not self._require_current_playback_attempt(attempt_id, "crossfade_load_succeeded"):
            self._discard_prepared_candidate(candidate, "crossfade_load")
            return
        if getattr(self, "_closing", False):
            # Real shutdown: the candidate was never committed to
            # anything, so discarding it is all that's needed.
            self._discard_prepared_candidate(candidate, "crossfade_load")
            return
        if not self._crossfade_load_is_current(token, path):
            # Cancelled or superseded while loading (another track
            # change, ...). Discarding frees only this candidate's own
            # resource -- it never touched the (possibly since-promoted)
            # inactive-slot player.
            self._discard_prepared_candidate(candidate, "crossfade_load")
            return
        if lease is None or not self._target_lease_still_valid(lease):
            self.diagnostics.record(
                "playback", "player_target_lease_invalid",
                details={"stage": "crossfade_load"},
                minimum_level="basic",
            )
            self._discard_prepared_candidate(candidate, "crossfade_load")
            return
        player = lease.physical_object
        backend = self._backend_label().lower()
        immediate = self._pending_crossfade_immediate
        try:
            if not player.commit_prepared(candidate):
                return
            self._inactive_normalisation_gain = self._cached_gain_for_path(path, target="inactive")
            player.set_volume(0.0)
            # Start the incoming stream silently right away, the same way the
            # VLC path already does via _play_on_player(volume_scale=0.0),
            # so device/buffer startup latency doesn't sit right at the
            # crossfade boundary.
            player.play()
            stats = player.stats()
            self._audio_log(
                f"backend={backend} crossfade start; file={self._audio_name(path)!r}; "
                f"duration={stats['duration']:.2f}s; rate={stats['sample_rate']}; channels={stats['channels']}"
            )
        except Exception as ex:
            self._fail_pending_crossfade(backend, path, ex)
            return
        self.fade_waits = 0
        self.pending_next = False
        self._builtin_fade_generation += 1
        fade_generation = self._builtin_fade_generation
        crossfade_seconds = self.crossfade_seconds
        try:
            remaining = max(0.0, self.simple_player.get_length() - self.simple_player.get_pos())
        except Exception:
            remaining = crossfade_seconds
        quiet_triggered = bool(self.pending_builtin_crossfade_quiet) or bool(immediate)
        fade_delay_s = 0.0 if quiet_triggered else max(0.0, remaining - crossfade_seconds)
        if fade_delay_s > 0.05:
            self._audio_log(f"backend={backend} prebuffer ready; fade_starts_in={fade_delay_s:.2f}s")
            QtCore.QTimer.singleShot(int(fade_delay_s * 1000), lambda gen=fade_generation: self._begin_builtin_fade(gen))
        else:
            if immediate:
                self._audio_log(f"backend={backend} prebuffer ready; manual fade begins now")
            elif quiet_triggered:
                self._audio_log(f"backend={backend} prebuffer ready; quiet fade begins now")
            self._begin_builtin_fade(fade_generation)

    def _begin_builtin_fade(self, generation=None):
        if generation is not None and generation != self._builtin_fade_generation:
            return
        if not self.prebuffer_active or self.fade_active:
            return
        # Normally already playing (started silently in
        # _start_miniaudio_crossfade_to); this is just a safety net.
        if self.simple_inactive_player and not self.simple_inactive_player.is_playing():
            try:
                self.simple_inactive_player.play()
            except Exception as ex:
                self._audio_log(f"backend={self._backend_label().lower()} crossfade begin failed; error={ex}")
                failed_path = self.pending_builtin_crossfade_path
                failed_index = self.pending_builtin_crossfade_index
                self.prebuffer_active = False
                self.pending_builtin_crossfade_index = None
                self.pending_builtin_crossfade_path = None
                self.pending_builtin_crossfade_quiet = False
                if failed_path:
                    self._activate_track_ui(failed_index, failed_path)
                    self._begin_playback_recovery(
                        "crossfade-failed",
                        self._current_backend_name(),
                        error=ex,
                        path=failed_path,
                        position=0.0,
                    )
                return
        pending_index = self.pending_builtin_crossfade_index
        pending_path = self.pending_builtin_crossfade_path
        if pending_path:
            self._activate_track_ui(pending_index, pending_path)
        self.fade_active = True
        self.fade_start = time.time()
        self.pending_builtin_crossfade_quiet = False
        self._audio_log(f"backend={self._backend_label().lower()} crossfade playback begin")
        if self._use_bass_backend():
            # BASS can ramp channel volume internally, while _fade_tick also
            # applies a manual guard ramp for packaged builds where slides fail.
            ramp_seconds = self.crossfade_seconds
            try:
                self.simple_player.slide_volume(0.0, ramp_seconds)
                self.simple_inactive_player.slide_volume(
                    combine_volume(self.master_volume / 100.0, self._inactive_normalisation_gain, self._sleep_timer_gain),
                    ramp_seconds,
                )
                self._audio_log(f"backend=bass crossfade volume slides armed; seconds={ramp_seconds:.2f}")
            except Exception as ex:
                self._audio_log(f"backend=bass crossfade slide unavailable; using manual ramp; error={ex}")

    def _promote_inactive_gain_slot(self, promoted_path: Optional[str]) -> None:
        """Swap the active/inactive gain values AND their identity tokens
        together (v1.0.70), then apply the memory-only snapshot safety net
        for the promoted path. Shared by every "the inactive slot is
        becoming active" promotion: A-A crossfade completion (both
        backends) and v1.0.71's Video->Audio mixed-transition commit
        (_finish_mixed_transition_video_to_audio), which promotes
        simple_inactive_player the same way a normal crossfade does.
        """
        self._active_normalisation_gain, self._inactive_normalisation_gain = self._inactive_normalisation_gain, 1.0
        self._active_gain_token, self._inactive_gain_token = self._inactive_gain_token, self._next_gain_token()
        if promoted_path:
            # Memory-only defense-in-depth: if a correct gain for the
            # promoted path is already sitting in the snapshot cache (cache
            # hit at load time, or a worker result that landed earlier),
            # use it directly rather than relying solely on the token swap
            # above. Zero I/O -- dict lookup only.
            snapshot = self._gain_snapshot_cache.get(promoted_path)
            if snapshot is not None:
                self._active_normalisation_gain = snapshot.linear_gain

    def _finish_miniaudio_crossfade(self):
        if self.simple_player:
            try:
                self.simple_player.stop()
            except Exception:
                pass
        # Keep the selected backend's canonical pair in the same order.  Without
        # this, _play_path_direct() restores the stopped pre-crossfade player on
        # the next transition, causing BASS crossfades to work only alternately.
        self._promote_inactive_player(reason="crossfade_complete")
        # v1.0.70: capture the promoted path before pending_builtin_crossfade_path
        # is cleared below.
        promoted_path = self.pending_builtin_crossfade_path
        self._promote_inactive_gain_slot(promoted_path)
        self.fade_active = False
        self.prebuffer_active = False
        self.pending_next = False
        self.pending_builtin_crossfade_index = None
        self.pending_builtin_crossfade_path = None
        self.pending_builtin_crossfade_quiet = False
        if self.simple_player:
            self.simple_player.set_volume(combine_volume(self.master_volume / 100.0, self._active_normalisation_gain, self._sleep_timer_gain))
        if self.simple_inactive_player:
            self.simple_inactive_player.set_volume(0.0)
        self._reset_progress()
        try:
            promoted_position = float(self.simple_player.get_pos() or 0.0)
        except Exception:
            promoted_position = 0.0
        self._arm_playback_watchdog(promoted_position)
        self._audio_log(f"backend={self._backend_label().lower()} crossfade complete")

    def _start_crossfade_to(self, path: str):
        if self.prebuffer_active:
            return
        self.prebuffer_active = True
        self.fade_from = (1.0, 0.0)
        self.fade_waits = 0
        # Reset baseline volumes before starting fade
        self._set_volume(self.active_player, 1.0)
        self._set_volume(self.inactive_player, 0.0)
        if not self._play_on_player(
            self.inactive_player, path, volume_scale=0.0
        ):
            self.prebuffer_active = False
            self._begin_playback_recovery(
                "crossfade-failed", "vlc", path=path, position=0.0
            )
            return
        self.pending_next = False
        QtCore.QTimer.singleShot(PREBUFFER_MS, self._begin_fade)

    def _finish_crossfade(self):
        self.active_player.stop()
        self.active_player, self.inactive_player = self.inactive_player, self.active_player
        # v1.0.70: current_path is already the promoted path here --
        # _play_path sets it via _activate_track_ui before
        # _start_crossfade_to is called.
        self._promote_inactive_gain_slot(self.current_path)
        self.fade_active = False
        self.prebuffer_active = False
        self._set_volume(self.active_player, 1.0)
        self._set_volume(self.inactive_player, 0.0)
        self._reset_progress()
        try:
            promoted_position = max(
                0.0, float(self.active_player.get_time() or 0) / 1000.0
            )
        except Exception:
            promoted_position = 0.0
        self._arm_playback_watchdog(promoted_position)
        QtCore.QTimer.singleShot(1500, self._reapply_master_volume)

    def _cancel_fade(self):
        self.fade_active = False
        self.prebuffer_active = False
        self.pending_next = False
        self.fade_waits = 0
        self.pending_builtin_crossfade_index = None
        self.pending_builtin_crossfade_path = None
        self.pending_builtin_crossfade_quiet = False

    def _built_in_players(self):
        players = []
        if self.simple_player:
            players.append(self.simple_player)
        if self.simple_inactive_player and self.simple_inactive_player is not self.simple_player:
            players.append(self.simple_inactive_player)
        return players

    def pause(self):
        if self._current_media_type == MediaType.VIDEO:
            transition_manager = getattr(self, "_video_transition_manager", None)
            if self._playback_intentionally_paused:
                self._video_backend.resume()
                if transition_manager is not None:
                    transition_manager.playback_paused(False)
                self._playback_intentionally_paused = False
                self.btn_pause.setText("Pause")
                self.btn_pause.setAccessibleName("Pause")
                self._announce_accessible_status("Playback started")
            else:
                self._video_backend.pause()
                if transition_manager is not None:
                    transition_manager.playback_paused(True)
                self._playback_intentionally_paused = True
                self.btn_pause.setText("Resume")
                self.btn_pause.setAccessibleName("Resume")
                self._announce_accessible_status("Playback paused")
            getattr(self, "_schedule_queue_duration_refresh", lambda *_: None)("playback_pause_toggled")
            return
        if getattr(self, "cast_active", False):
            if self._playback_intentionally_paused:
                self.cast_controller.play()
                self._playback_intentionally_paused = False
                self.btn_pause.setText("Pause")
                self.btn_pause.setAccessibleName("Pause")
            else:
                self.cast_controller.pause()
                self._playback_intentionally_paused = True
                self.btn_pause.setText("Resume")
                self.btn_pause.setAccessibleName("Resume")
            getattr(self, "_schedule_queue_duration_refresh", lambda *_: None)("playback_pause_toggled")
            return
        # Built-in backends can have two players during a crossfade; pause or
        # resume every active one so audio and the button state stay together.
        if self._use_builtin_player():
            players = self._built_in_players()
            paused = any(bool(getattr(player, "_paused", False)) for player in players)
            try:
                if paused:
                    for player in players:
                        if bool(getattr(player, "_paused", False)):
                            player.resume()
                    self._audio_log(f"backend={self._backend_label().lower()} resume; position={self.simple_player.get_pos():.2f}s")
                    self.beat.setPlaying(True)
                    self.btn_pause.setText("Pause")
                    self.btn_pause.setAccessibleName("Pause")
                    self._playback_intentionally_paused = False
                    self._arm_playback_watchdog(self.simple_player.get_pos())
                    self._announce_accessible_status("Playback started")
                else:
                    paused_any = False
                    for player in players:
                        if player and player.is_playing():
                            player.pause()
                            paused_any = True
                    if paused_any:
                        self._audio_log(f"backend={self._backend_label().lower()} pause; position={self.simple_player.get_pos():.2f}s")
                        self.beat.setPlaying(False)
                        self.btn_pause.setText("Resume")
                        self.btn_pause.setAccessibleName("Resume")
                        self._playback_intentionally_paused = True
                        self._announce_accessible_status("Playback paused")
            except Exception as ex:
                self._audio_log(f"backend={self._backend_label().lower()} pause/resume failed; error={ex}")
            getattr(self, "_schedule_queue_duration_refresh", lambda *_: None)("playback_pause_toggled")
            return

        if not self.active_player:
            return
        if self.active_player.is_playing():
            self.active_player.pause()
            self._audio_log(f"backend=vlc pause; position={self.active_player.get_time() / 1000.0:.2f}s")
            self.beat.setPlaying(False)
            self.btn_pause.setText("Resume")
            self.btn_pause.setAccessibleName("Resume")
            self._playback_intentionally_paused = True
            self._announce_accessible_status("Playback paused")
        else:
            self.active_player.play()
            self._audio_log(f"backend=vlc resume; position={self.active_player.get_time() / 1000.0:.2f}s")
            self.beat.setPlaying(True)
            self.btn_pause.setText("Pause")
            self.btn_pause.setAccessibleName("Pause")
            self._playback_intentionally_paused = False
            self._arm_playback_watchdog(
                self.active_player.get_time() / 1000.0
            )
            self._announce_accessible_status("Playback started")
        getattr(self, "_schedule_queue_duration_refresh", lambda *_: None)("playback_pause_toggled")

    def set_master_volume(self, value: int):
        if getattr(self, "cast_active", False):
            self.cast_volume = int(value)
            self.cast_controller.set_volume(self.cast_volume / 100.0)
            self._muted = value == 0
            return
        self.master_volume = value
        self._muted = value == 0
        if value > 0 and not self._muted:
            self._volume_before_mute = value
        if hasattr(self, "action_mute"):
            self.action_mute.setText("Unmute" if self._muted else "Mute")
        self._video_backend.set_volume(value)
        self._video_backend.set_muted(self._muted)
        if self.fade_active:
            return
        if self._use_builtin_player():
            try:
                self.simple_player.set_volume(combine_volume(self.master_volume / 100.0, self._active_normalisation_gain, self._sleep_timer_gain))
                if self.simple_inactive_player:
                    self.simple_inactive_player.set_volume(combine_volume(self.master_volume / 100.0, self._inactive_normalisation_gain, self._sleep_timer_gain))
            except Exception:
                pass
        elif self.active_player:
            self._set_volume(self.active_player, 1.0)

    def _set_volume(self, player, scale: float):
        if not player:
            return
        gain = self._inactive_normalisation_gain if player is self.inactive_player else self._active_normalisation_gain
        player.audio_set_volume(int(round(100.0 * combine_volume(self.master_volume / 100.0, gain, scale * self._sleep_timer_gain))))

    def _gain_details_for_path(self, path: str):
        override = self.loudness_cache.override_for(path)
        mode = effective_mode(self.normalisation_enabled, self.normalisation_mode, override)
        tags = read_replaygain(path)
        measured = self.loudness_cache.analysis_for(path)
        result = calculate_gain(
            mode, tags, measured, target_lufs=self.target_lufs,
            tagged_preamp_db=self.tagged_preamp_db,
            untagged_preamp_db=self.untagged_preamp_db,
            prevent_clipping=self.prevent_clipping,
        )
        if result.pending_analysis and self.auto_loudness_analysis and os.path.isfile(path):
            self.loudness_worker.request(path)
        return override, tags, measured, result

    def _gain_for_path(self, path: str) -> float:
        _, _, _, result = self._gain_details_for_path(path)
        if result.pending_analysis:
            self.statusBar().showMessage("Normalisation pending analysis", 4000)
        elif result.mode != "off" and result.source != "fallback":
            suffix = ", limited to prevent clipping" if result.clipping_reduced else ""
            self.statusBar().showMessage(f"Normalised: {result.applied_db:+.1f} dB{suffix}", 5000)
        return result.linear_gain

    def _next_gain_token(self) -> int:
        self._gain_token_seq += 1
        return self._gain_token_seq

    def _set_slot_gain(self, target: str, gain: float) -> None:
        """Assign an authoritative, I/O-free gain value to a playback slot
        ("active" or "inactive"), bumping that slot's identity token.

        Used by every *synchronous* recalculation of the current track's
        gain (loudness analysis completing, a normalisation override
        change, Preferences Apply). Bumping the token here, not just in
        _cached_gain_for_path, is what stops a GainLookupWorker result that
        was dispatched under the old override/settings from clobbering a
        just-recalculated authoritative value if it arrives later.
        """
        slot_token = self._next_gain_token()
        if target == "inactive":
            self._inactive_gain_token = slot_token
            self._inactive_normalisation_gain = gain
        else:
            self._active_gain_token = slot_token
            self._active_normalisation_gain = gain

    def _cached_gain_for_path(self, path: str, target: str = "active") -> float:
        """Playback-path gain lookup: consumes only the in-memory snapshot
        (self._gain_snapshot_cache) and never blocks on Mutagen/stat.

        v1.0.67 MainThread I/O hardening: _gain_for_path/_gain_details_for_path
        (unchanged above, still used by one-off user actions like Show
        Loudness Information) read ReplayGain tags and the loudness-analysis
        cache signature synchronously -- real I/O run on every single track
        activation, crossfade and recovery. This is the cached-immediate +
        background-refresh replacement for those hot paths specifically:
        a cache hit returns instantly; a miss returns a safe unity-gain
        default (unless the override/mode already makes "off" the final
        answer with no I/O needed) and kicks off GainLookupWorker, which
        corrects the now-playing volume in place once it completes.

        v1.0.70: `target` identifies which playback slot ("active" or
        "inactive") this lookup is for. A fresh identity token is minted
        for that slot on every call, hit or miss -- this is what lets a
        later-arriving GainLookupWorker result recognise it has been
        superseded (the slot was reassigned to a different track, or
        recalculated some other way) even when the path happens to match.
        See _queue_gain_lookup_async for how the token is used at apply
        time, and _set_slot_gain for the equivalent synchronous path.
        """
        slot_token = self._next_gain_token()
        if target == "inactive":
            self._inactive_gain_token = slot_token
        else:
            self._active_gain_token = slot_token

        cached = self._gain_snapshot_cache.get(path)
        if cached is not None:
            self.diagnostics.record(
                "loudness", "gain_cache_hit",
                details={
                    "target": target, "token": slot_token,
                    **self.diagnostics.path_details(path),
                },
                minimum_level="detailed",
            )
            if cached.pending_analysis:
                self.statusBar().showMessage("Normalisation pending analysis", 4000)
            elif cached.mode != "off" and cached.source != "fallback":
                suffix = ", limited to prevent clipping" if cached.clipping_reduced else ""
                self.statusBar().showMessage(f"Normalised: {cached.applied_db:+.1f} dB{suffix}", 5000)
            return cached.linear_gain

        override = self.loudness_cache.override_for(path)
        mode = effective_mode(self.normalisation_enabled, self.normalisation_mode, override)
        if mode == "off":
            # No embedded/measured data could change this outcome -- this
            # is already the authoritative answer, no I/O required.
            result = calculate_gain(
                mode, {}, None, target_lufs=self.target_lufs,
                tagged_preamp_db=self.tagged_preamp_db,
                untagged_preamp_db=self.untagged_preamp_db,
                prevent_clipping=self.prevent_clipping,
            )
            self._gain_snapshot_cache[path] = result
            return result.linear_gain

        self._queue_gain_lookup_async(path, slot_token, target)
        return 1.0

    def _queue_gain_lookup_async(self, path: str, slot_token: int, target: str = "active"):
        """Dispatch (or join) a background GainLookupWorker for `path`.

        v1.0.70: `slot_token` is the identity token _cached_gain_for_path
        minted for the requesting slot. Multiple slots/requests can await
        the same in-flight path (e.g. the active and inactive slots both
        happen to load the same path) -- self._gain_lookup_subscribers
        fans the one worker's result out to every still-pending token
        instead of a single "current track" callback being authoritative
        for all of them. At apply time each token is matched against
        *both* current slot tokens (not just the slot it was originally
        requested for) -- this is what lets a result requested for the
        inactive slot still land correctly if promotion has since swapped
        that same token into the active slot (see _finish_miniaudio_crossfade
        / _finish_crossfade). A token matching neither slot is stale and
        dropped: the requesting slot was reassigned (reused for another
        track, or recalculated synchronously) since the request was made.
        """
        if getattr(self, "_closing", False):
            return
        self._gain_lookup_subscribers.setdefault(path, []).append(slot_token)
        self.diagnostics.record(
            "loudness", "gain_request_started",
            details={
                "target": target, "token": slot_token,
                **self.diagnostics.path_details(path),
            },
            minimum_level="detailed",
        )
        if path in self._gain_lookup_pending:
            return
        self._gain_lookup_pending.add(path)
        worker = GainLookupWorker(path, self.loudness_cache)
        self._gain_lookup_workers.append(worker)
        registry_token = self._worker_registry.register("gain_lookup", thread=worker, wait_ms=1500)

        def _on_ready(loaded_path, tags, measured):
            self._gain_lookup_pending.discard(loaded_path)
            pending_tokens = self._gain_lookup_subscribers.pop(loaded_path, [])
            if getattr(self, "_closing", False):
                return
            override = self.loudness_cache.override_for(loaded_path)
            mode = effective_mode(self.normalisation_enabled, self.normalisation_mode, override)
            result = calculate_gain(
                mode, tags, measured, target_lufs=self.target_lufs,
                tagged_preamp_db=self.tagged_preamp_db,
                untagged_preamp_db=self.untagged_preamp_db,
                prevent_clipping=self.prevent_clipping,
            )
            self._gain_snapshot_cache[loaded_path] = result
            if result.pending_analysis and self.auto_loudness_analysis and self.loudness_worker:
                self.loudness_worker.request(loaded_path)
            self.diagnostics.record(
                "loudness", "gain_worker_result",
                details={
                    "source": result.source, "mode": result.mode,
                    **self.diagnostics.path_details(loaded_path),
                },
                minimum_level="detailed",
            )
            applied = False
            for pending_token in pending_tokens:
                if pending_token == self._active_gain_token:
                    self._active_normalisation_gain = result.linear_gain
                    matched_target = "active"
                elif pending_token == self._inactive_gain_token:
                    self._inactive_normalisation_gain = result.linear_gain
                    matched_target = "inactive"
                else:
                    matched_target = None
                if matched_target is not None:
                    applied = True
                    self.diagnostics.record(
                        "loudness", "gain_result_applied",
                        details={
                            "target": matched_target, "token": pending_token,
                            **self.diagnostics.path_details(loaded_path),
                        },
                        minimum_level="detailed",
                    )
                else:
                    self.diagnostics.record(
                        "loudness", "gain_result_stale_dropped",
                        details={
                            "token": pending_token,
                            **self.diagnostics.path_details(loaded_path),
                        },
                        minimum_level="detailed",
                    )
            if applied:
                self.set_master_volume(self.master_volume)

        def _on_finished(worker=worker, registry_token=registry_token):
            if worker in self._gain_lookup_workers:
                self._gain_lookup_workers.remove(worker)
            self._worker_registry.unregister(registry_token)

        worker.gain_ready.connect(_on_ready)
        worker.finished.connect(_on_finished)
        worker.start()

    def _on_loudness_result(self, path: str, result: dict):
        try:
            self.loudness_cache.store_analysis(path, result["lufs"], result["true_peak"])
            self._gain_snapshot_cache.pop(path, None)
            self.statusBar().showMessage(f"Loudness analysis complete: {os.path.basename(path)}", 5000)
            if path == self.current_path and self._gain_details_for_path(path)[3].source == "player analysis cache":
                self._set_slot_gain("active", self._gain_for_path(path))
                self.set_master_volume(self.master_volume)
        except Exception:
            pass

    def _on_waveform_ready(self, path: str, data):
        is_current_karaoke_audio = (
            self._current_media_type == MediaType.KARAOKE
            and path == getattr(self, "_karaoke_audio_path", None)
        )
        if (path != self.current_path and not is_current_karaoke_audio) or self.waveform_seekbar is None:
            return
        self.waveform_seekbar.set_waveform(data)

    def _on_waveform_unavailable(self, path: str):
        is_current_karaoke_audio = (
            self._current_media_type == MediaType.KARAOKE
            and path == getattr(self, "_karaoke_audio_path", None)
        )
        if (path != self.current_path and not is_current_karaoke_audio) or self.waveform_seekbar is None:
            return
        self.waveform_seekbar.set_waveform(None)

    def _add_normalisation_menu(self, menu, paths):
        paths = [path for path in paths if path]
        submenu = menu.addMenu("Normalisation")
        actions = {}
        labels = (
            ("default", "Use Player Default"), ("track", "Track Normalisation"),
            ("album", "Album Normalisation"), ("off", "Normalisation Off" if len(paths) > 1 else "Normalisation Off for This Track"),
        )
        current = self.loudness_cache.override_for(paths[0]) if len(paths) == 1 else None
        group = QtGui.QActionGroup(submenu)
        group.setExclusive(True)
        for key, label in labels:
            action = submenu.addAction(label)
            action.setCheckable(True)
            action.setChecked(current == key)
            group.addAction(action)
            actions[action] = ("override", key)
        submenu.addSeparator()
        analyse = submenu.addAction("Analyse Selected Tracks" if len(paths) > 1 else "Analyse Loudness Now")
        reanalyse = submenu.addAction("Re-analyse Selected Tracks" if len(paths) > 1 else "Re-analyse Loudness")
        actions[analyse] = ("analyse", None)
        actions[reanalyse] = ("reanalyse", None)
        if len(paths) == 1:
            info = submenu.addAction("Show Loudness Information")
            actions[info] = ("info", None)
        return actions

    def _handle_normalisation_action(self, action, actions, paths):
        if action not in actions:
            return False
        command, value = actions[action]
        if command == "override":
            for path in paths:
                self.loudness_cache.set_override(path, value)
                self._gain_snapshot_cache.pop(path, None)
            if self.current_path in paths:
                self._set_slot_gain("active", self._gain_for_path(self.current_path))
                self.set_master_volume(self.master_volume)
        elif command in ("analyse", "reanalyse"):
            for path in paths:
                if command == "reanalyse":
                    self.loudness_cache.remove_analysis(path)
                    self._gain_snapshot_cache.pop(path, None)
                self.loudness_worker.request(path, force=True)
            self.statusBar().showMessage(f"Queued loudness analysis for {len(paths)} track(s)", 5000)
        elif command == "info" and paths:
            self._show_loudness_information(paths[0])
        return True

    def _show_loudness_information(self, path):
        override, tags, measured, result = self._gain_details_for_path(path)
        def show(value, suffix=""):
            return "Not available" if value is None else f"{value:+.2f}{suffix}"
        origin = "Preferences" if override == "default" else "track override"
        text = "\n".join((
            f"File name: {os.path.basename(path)}", f"Current mode: {result.mode.title()} ({origin})",
            f"ReplayGain track gain: {show(tags['track_gain'], ' dB')}",
            f"ReplayGain track peak: {show(tags['track_peak'])}",
            f"ReplayGain album gain: {show(tags['album_gain'], ' dB')}",
            f"ReplayGain album peak: {show(tags['album_peak'])}",
            f"Measured loudness: {show(measured.get('lufs') if measured else None, ' LUFS')}",
            f"Measured true peak: {show(measured.get('true_peak') if measured else None)}",
            f"Final gain: {result.applied_db:+.2f} dB",
            f"Clipping protection reduced gain: {'Yes' if result.clipping_reduced else 'No'}",
            f"Source: {result.source.title()}",
        ))
        QtWidgets.QMessageBox.information(self, "Loudness Information", text)

    def _show_normalisation_preferences(self):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Playback and Diagnostics Preferences")
        # This dialog has grown to cover normalisation, track transitions,
        # Up Next, Party Mode and diagnostics -- taller than fits on many
        # screens. A scroll area keeps every row reachable regardless of
        # screen size, with Ok/Cancel pinned outside it so they're always
        # visible without scrolling.
        outer_layout = QtWidgets.QVBoxLayout(dialog)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll_area = QtWidgets.QScrollArea(dialog)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        form_widget = QtWidgets.QWidget()
        layout = QtWidgets.QFormLayout(form_widget)
        scroll_area.setWidget(form_widget)
        outer_layout.addWidget(scroll_area)
        available = QtGui.QGuiApplication.primaryScreen().availableGeometry()
        dialog.resize(600, min(760, max(420, available.height() - 120)))
        enabled = QtWidgets.QCheckBox("Enable volume normalisation")
        enabled.setChecked(self.normalisation_enabled)
        mode = QtWidgets.QComboBox()
        mode.addItems(["Track", "Album"])
        mode.setCurrentText(self.normalisation_mode.title())
        clipping = QtWidgets.QCheckBox("Prevent clipping")
        clipping.setChecked(self.prevent_clipping)
        auto = QtWidgets.QCheckBox("Automatically analyse tracks when ReplayGain information is missing")
        auto.setChecked(self.auto_loudness_analysis)
        target = QtWidgets.QDoubleSpinBox(); target.setRange(-23.0, -9.0); target.setSuffix(" LUFS"); target.setValue(self.target_lufs)
        tagged = QtWidgets.QDoubleSpinBox(); tagged.setRange(-12.0, 12.0); tagged.setSuffix(" dB"); tagged.setValue(self.tagged_preamp_db)
        untagged = QtWidgets.QDoubleSpinBox(); untagged.setRange(-12.0, 12.0); untagged.setSuffix(" dB"); untagged.setValue(self.untagged_preamp_db)
        recover = QtWidgets.QCheckBox(
            "Automatically retry playback after an audio error"
        )
        recover.setChecked(self.auto_playback_recovery)
        recover.setToolTip(
            "Retries the current track once when playback fails or stalls."
        )
        fallback = QtWidgets.QCheckBox(
            "Allow temporary fallback to another audio backend"
        )
        fallback.setChecked(self.allow_backend_fallback)
        fallback.setToolTip(
            "A fallback is temporary and does not change your preferred backend."
        )
        waveform_seekbar = QtWidgets.QCheckBox("Use waveform seek bar")
        waveform_seekbar.setChecked(self.waveform_seekbar_enabled)
        waveform_seekbar.setToolTip(
            "Shows the track's waveform shape for seeking instead of a plain "
            "progress bar. Takes effect after restarting Bills Music Player."
        )
        explanation = QtWidgets.QLabel("Volume normalisation adjusts each track during playback so quieter and louder recordings have a similar perceived volume. It does not permanently modify your music files.\n\nLouder target settings may require clipping protection.")
        explanation.setWordWrap(True)
        for label, widget in (("", enabled), ("Mode", mode), ("", clipping), ("", auto), ("Target loudness", target), ("Tagged-track preamp", tagged), ("Untagged-track preamp", untagged), ("Playback recovery", recover), ("", fallback), ("", waveform_seekbar)):
            layout.addRow(label, widget)
        layout.addRow(explanation)
        transition_heading = QtWidgets.QLabel("<b>Track Transitions</b>")
        transition_mode = QtWidgets.QComboBox()
        transition_mode.addItems(["Normal", "Crossfade"])
        transition_mode.setCurrentText(self.track_transition_mode.title())
        transition_mode.setToolTip(
            "Normal: each track stops and the next one starts immediately.\n"
            "Crossfade: tracks blend into each other over the duration below."
        )
        crossfade_duration = QtWidgets.QDoubleSpinBox()
        crossfade_duration.setRange(CROSSFADE_SECONDS_MIN, CROSSFADE_SECONDS_MAX)
        crossfade_duration.setSuffix(" s")
        crossfade_duration.setSingleStep(0.5)
        crossfade_duration.setValue(self.crossfade_seconds)
        crossfade_duration.setEnabled(self.track_transition_mode == "crossfade")

        def _on_transition_mode_changed(text, spin=crossfade_duration):
            spin.setEnabled(text == "Crossfade")

        transition_mode.currentTextChanged.connect(_on_transition_mode_changed)
        transition_explanation = QtWidgets.QLabel(
            "Controls how playback moves from one track to the next."
        )
        transition_explanation.setWordWrap(True)
        layout.addRow(transition_heading)
        layout.addRow("Mode", transition_mode)
        layout.addRow("Crossfade duration", crossfade_duration)
        layout.addRow(transition_explanation)
        queue_heading = QtWidgets.QLabel("<b>Up Next Queue</b>")
        warn_duplicates = QtWidgets.QCheckBox(
            "Warn before adding duplicate tracks to Up Next"
        )
        warn_duplicates.setChecked(
            bool(getattr(self, "warn_before_adding_duplicate_queue_tracks", True))
        )
        warn_duplicates.setToolTip(
            "Shows a confirmation when a track you're adding is already in "
            "Up Next, so you can add just the new tracks, add everything "
            "anyway, or cancel."
        )
        layout.addRow(queue_heading)
        layout.addRow("", warn_duplicates)
        video_heading = QtWidgets.QLabel("<b>Video Playback</b>")
        video_enabled = QtWidgets.QCheckBox("Enable built-in video playback")
        video_enabled.setChecked(bool(getattr(self, "video_playback_enabled", True)))
        video_enabled.setToolTip(
            "Videos remain visible in the Videos tab either way; when "
            "disabled, playing one shows a message instead of starting it."
        )
        video_fullscreen_start = QtWidgets.QCheckBox("Start videos full screen")
        video_fullscreen_start.setChecked(bool(getattr(self, "video_start_fullscreen", False)))
        video_return_normal = QtWidgets.QCheckBox(
            "Return to normal display when video ends"
        )
        video_return_normal.setChecked(
            bool(getattr(self, "video_return_to_normal_display_on_end", True))
        )
        layout.addRow(video_heading)
        layout.addRow("", video_enabled)
        layout.addRow("", video_fullscreen_start)
        layout.addRow("", video_return_normal)
        video_transition_heading = QtWidgets.QLabel("<b>Video Transitions</b>")
        video_transitions_enabled = QtWidgets.QCheckBox(
            "Enable video transitions"
        )
        video_transitions_enabled.setChecked(
            bool(getattr(self, "video_transitions_enabled", True))
        )
        video_transition_style = QtWidgets.QComboBox()
        video_transition_style.addItems(list(VIDEO_TRANSITION_STYLES))
        video_transition_style.setCurrentText(
            getattr(self, "video_transition_style", "Random Smooth")
        )
        video_transition_duration = QtWidgets.QDoubleSpinBox()
        video_transition_duration.setRange(0.3, 3.0)
        video_transition_duration.setSingleStep(0.1)
        video_transition_duration.setSuffix(" s")
        video_transition_duration.setValue(
            float(getattr(self, "video_transition_duration_seconds", 1.0))
        )
        video_transition_lead = QtWidgets.QDoubleSpinBox()
        video_transition_lead.setRange(0.3, 3.0)
        video_transition_lead.setSingleStep(0.1)
        video_transition_lead.setSuffix(" s")
        video_transition_lead.setValue(
            float(getattr(self, "video_transition_automatic_lead_seconds", 1.0))
        )
        video_transition_manual_duration = QtWidgets.QDoubleSpinBox()
        video_transition_manual_duration.setRange(0.3, 3.0)
        video_transition_manual_duration.setSingleStep(0.1)
        video_transition_manual_duration.setSuffix(" s")
        video_transition_manual_duration.setValue(
            float(getattr(self, "video_transition_manual_duration_seconds", 0.5))
        )
        effect_widget = QtWidgets.QWidget()
        effect_layout = QtWidgets.QGridLayout(effect_widget)
        effect_layout.setContentsMargins(0, 0, 0, 0)
        effect_checks = {}
        configured_effects = getattr(
            self, "video_transition_enabled_effects",
            {effect: True for effect in VIDEO_TRANSITION_EFFECTS},
        )
        for effect_index, effect in enumerate(VIDEO_TRANSITION_EFFECTS):
            checkbox = QtWidgets.QCheckBox(effect)
            checkbox.setChecked(bool(configured_effects.get(effect, True)))
            effect_layout.addWidget(checkbox, effect_index // 2, effect_index % 2)
            effect_checks[effect] = checkbox

        transition_controls = (
            video_transition_style,
            video_transition_duration,
            video_transition_lead,
            video_transition_manual_duration,
            effect_widget,
        )

        def _sync_video_transition_controls(checked):
            for control in transition_controls:
                control.setEnabled(bool(checked))

        video_transitions_enabled.toggled.connect(_sync_video_transition_controls)
        _sync_video_transition_controls(video_transitions_enabled.isChecked())
        video_transition_explanation = QtWidgets.QLabel(
            "Transitions are painted over the existing video surface; they "
            "do not create a second decoder or change music crossfading. "
            "Automatic effects develop over the final frames and reach full "
            "cover when the video ends. "
            "Effect checkboxes control eligibility in Random modes."
        )
        video_transition_explanation.setWordWrap(True)
        layout.addRow(video_transition_heading)
        layout.addRow("", video_transitions_enabled)
        layout.addRow("Transition style", video_transition_style)
        layout.addRow("Transition duration", video_transition_duration)
        layout.addRow("Automatic lead time", video_transition_lead)
        layout.addRow("Manual Next duration", video_transition_manual_duration)
        layout.addRow("Random effects", effect_widget)
        layout.addRow(video_transition_explanation)
        # Defensive, idempotent -- normally already triggered/resolved by
        # startup (see _startup_build_interface); this just covers the
        # edge case of Preferences being opened unusually early.
        self._start_gpu_capability_probe()
        gpu_dual_available = (
            DUAL_VIDEO_TRANSITIONS_AVAILABLE and self._gpu_dual_capability is True
        )
        video_dual_transitions_enabled = QtWidgets.QCheckBox(
            "(Experimental) Enable seamless dual-video cross-dissolve"
        )
        video_dual_transitions_enabled.setChecked(
            gpu_dual_available and bool(getattr(self, "video_dual_transitions_enabled", False))
        )
        video_dual_transitions_enabled.setEnabled(gpu_dual_available)
        if gpu_dual_available:
            video_dual_transition_explanation = QtWidgets.QLabel(
                "Phase 2A GPU compositor: preloads the next queued video and "
                "genuinely cross-dissolves between both on the GPU, instead of "
                "covering the switch with the painted transition above. Off by "
                "default. Restarts the video player process when toggled. Only "
                "applies to Video -> Video; other combinations keep using the "
                "transition above."
            )
        else:
            video_dual_transitions_enabled.setChecked(False)
            video_dual_transition_explanation = QtWidgets.QLabel(
                _dual_transition_unavailable_reason(self._gpu_dual_capability)
            )
        video_dual_transition_explanation.setWordWrap(True)
        # Phase 2B -- a distinct combo box from "Transition style" above:
        # that one picks a Phase 1 painted-overlay effect, this one picks
        # which genuine GPU effect the seamless dual-video path above uses.
        # Only ever enabled alongside the checkbox above it, and only
        # populated/shown as usable when the GPU path itself is available
        # -- an unavailable GPU subsystem must never advertise an effect
        # as usable.
        video_gpu_transition_effect = QtWidgets.QComboBox()
        video_gpu_transition_effect.addItems(list(GPU_TRANSITION_STYLES))
        video_gpu_transition_effect.setCurrentText(
            getattr(self, "video_gpu_transition_effect", "Cross Dissolve")
        )
        video_gpu_transition_effect.setEnabled(
            gpu_dual_available and video_dual_transitions_enabled.isChecked()
        )
        video_dual_transitions_enabled.toggled.connect(
            lambda checked: video_gpu_transition_effect.setEnabled(
                gpu_dual_available and checked
            )
        )
        layout.addRow("", video_dual_transitions_enabled)
        layout.addRow(video_dual_transition_explanation)
        layout.addRow("GPU transition effect", video_gpu_transition_effect)

        # Smart Video Transition Points -- a content-aware refinement on
        # top of the seamless dual-video cross-dissolve above, not a
        # separate transition system. Only ever meaningful (and only ever
        # shown enabled) alongside that checkbox, the same gating
        # video_gpu_transition_effect already uses just above.
        video_smart_transition_heading = QtWidgets.QLabel(
            "<b>Smart video transition points</b>"
        )
        video_smart_transition_points_enabled = QtWidgets.QCheckBox(
            "Enable smart video transition points"
        )
        video_smart_transition_points_enabled.setChecked(
            gpu_dual_available
            and video_dual_transitions_enabled.isChecked()
            and bool(getattr(self, "video_smart_transition_points_enabled", False))
        )
        video_smart_transition_points_enabled.setEnabled(
            gpu_dual_available and video_dual_transitions_enabled.isChecked()
        )
        video_smart_transition_explanation = QtWidgets.QLabel(
            "Some music videos have genuine black or near-black frames at "
            "their very start or end. Detects those (in a small bounded "
            "window, cached per file) and adjusts the cross-dissolve above "
            "to start a little earlier or reveal the next video's first "
            "real picture, instead of blending black into black. New and "
            "off by default; never skips dark scenes, only content proven "
            "to be genuinely black."
        )
        video_smart_transition_explanation.setWordWrap(True)
        video_avoid_black_outros = QtWidgets.QCheckBox("Avoid black video outros")
        video_avoid_black_outros.setChecked(
            bool(getattr(self, "video_avoid_black_outros", False))
        )
        video_skip_black_intros = QtWidgets.QCheckBox("Skip black video intros")
        video_skip_black_intros.setChecked(
            bool(getattr(self, "video_skip_black_intros", False))
        )
        video_skip_black_intros_explanation = QtWidgets.QLabel(
            "More conservative than avoiding outros: only skips an intro "
            "when it's both visually black and the audio there is proven "
            "silent, so a song's actual opening is never cut short."
        )
        video_skip_black_intros_explanation.setWordWrap(True)

        def _sync_smart_transition_controls(_checked=None):
            smart_available = (
                gpu_dual_available and video_dual_transitions_enabled.isChecked()
            )
            video_smart_transition_points_enabled.setEnabled(smart_available)
            smart_on = smart_available and video_smart_transition_points_enabled.isChecked()
            video_avoid_black_outros.setEnabled(smart_on)
            video_skip_black_intros.setEnabled(smart_on)

        video_crossfade_audio_enabled = QtWidgets.QCheckBox("Crossfade video audio")
        video_crossfade_audio_enabled.setChecked(
            gpu_dual_available
            and video_dual_transitions_enabled.isChecked()
            and bool(getattr(self, "video_crossfade_audio_enabled", False))
        )
        video_crossfade_audio_curve = QtWidgets.QComboBox()
        video_crossfade_audio_curve.addItems(["Equal Power", "Linear"])
        video_crossfade_audio_curve.setCurrentText(
            getattr(self, "video_crossfade_audio_curve", "Equal Power")
        )
        video_crossfade_audio_explanation = QtWidgets.QLabel(
            "Fades the outgoing video's audio down and the incoming "
            "video's audio up together with the picture, instead of "
            "switching instantly. Equal power avoids the volume dip a "
            "simple linear fade produces through the middle of the "
            "transition. Only affects genuine GPU Video→Video "
            "transitions -- classic playback, karaoke, and music are "
            "unaffected. New and off by default."
        )
        video_crossfade_audio_explanation.setWordWrap(True)

        def _sync_crossfade_controls(_checked=None):
            dual_available = gpu_dual_available and video_dual_transitions_enabled.isChecked()
            video_crossfade_audio_enabled.setEnabled(dual_available)
            video_crossfade_audio_curve.setEnabled(
                dual_available and video_crossfade_audio_enabled.isChecked()
            )

        video_dual_transitions_enabled.toggled.connect(_sync_crossfade_controls)
        video_crossfade_audio_enabled.toggled.connect(_sync_crossfade_controls)
        _sync_crossfade_controls()

        video_dual_transitions_enabled.toggled.connect(_sync_smart_transition_controls)
        video_smart_transition_points_enabled.toggled.connect(_sync_smart_transition_controls)
        _sync_smart_transition_controls()
        layout.addRow(video_smart_transition_heading)
        layout.addRow("", video_smart_transition_points_enabled)
        layout.addRow(video_smart_transition_explanation)
        layout.addRow("", video_avoid_black_outros)
        layout.addRow("", video_skip_black_intros)
        layout.addRow(video_skip_black_intros_explanation)
        layout.addRow("", video_crossfade_audio_enabled)
        layout.addRow("Audio crossfade curve", video_crossfade_audio_curve)
        layout.addRow(video_crossfade_audio_explanation)
        party_mode_heading = QtWidgets.QLabel("<b>Party Mode</b>")
        party_screen = QtWidgets.QComboBox()
        party_screen.addItem("Same monitor as main window", "")
        for screen in QtGui.QGuiApplication.screens():
            party_screen.addItem(format_screen_label(screen), screen.name())
        screen_index = party_screen.findData(getattr(self, "party_mode_screen_name", ""))
        party_screen.setCurrentIndex(screen_index if screen_index >= 0 else 0)
        party_layout_combo = QtWidgets.QComboBox()
        party_layout_combo.addItems(["Lyrics", "Artwork", "Visualiser"])
        party_layout_combo.setCurrentText(
            getattr(self, "party_mode_default_layout", "lyrics").title()
        )
        party_show_up_next = QtWidgets.QCheckBox("Show Up Next")
        party_show_up_next.setChecked(bool(getattr(self, "party_mode_show_up_next", True)))
        party_up_next_count = QtWidgets.QSpinBox()
        party_up_next_count.setRange(PARTY_MODE_UP_NEXT_COUNT_MIN, PARTY_MODE_UP_NEXT_COUNT_MAX)
        party_up_next_count.setValue(int(getattr(self, "party_mode_up_next_count", 3)))
        party_show_clock = QtWidgets.QCheckBox("Show clock")
        party_show_clock.setChecked(bool(getattr(self, "party_mode_show_clock", True)))
        party_show_remaining_playlist = QtWidgets.QCheckBox(
            "Show estimated remaining playlist time"
        )
        party_show_remaining_playlist.setChecked(
            bool(getattr(self, "party_mode_show_remaining_playlist_time", False))
        )
        party_auto_hide = QtWidgets.QSpinBox()
        party_auto_hide.setRange(PARTY_MODE_AUTO_HIDE_SECONDS_MIN, PARTY_MODE_AUTO_HIDE_SECONDS_MAX)
        party_auto_hide.setSuffix(" s")
        party_auto_hide.setValue(
            int(round(getattr(self, "party_mode_auto_hide_ms", 3000) / 1000.0))
        )
        party_animations = QtWidgets.QCheckBox("Animated transitions")
        party_animations.setChecked(bool(getattr(self, "party_mode_animations_enabled", True)))
        party_quality = QtWidgets.QComboBox()
        party_quality.addItems(["Low", "Medium", "High"])
        party_quality.setCurrentText(getattr(self, "party_mode_visual_quality", "medium").title())
        party_explanation = QtWidgets.QLabel(
            "Party Mode opens a full-screen presentation view on the selected "
            "monitor, showing lyrics, artwork or the visualiser for the "
            "current track. It never interrupts playback -- it's another "
            "view of what's already playing."
        )
        party_explanation.setWordWrap(True)
        layout.addRow(party_mode_heading)
        layout.addRow("Preferred monitor", party_screen)
        layout.addRow("Default layout", party_layout_combo)
        layout.addRow("", party_show_up_next)
        layout.addRow("Upcoming tracks shown", party_up_next_count)
        layout.addRow("", party_show_clock)
        layout.addRow("", party_show_remaining_playlist)
        layout.addRow("Controls auto-hide after", party_auto_hide)
        layout.addRow("", party_animations)
        layout.addRow("Visual quality", party_quality)
        layout.addRow(party_explanation)
        diagnostics_heading = QtWidgets.QLabel("<b>Diagnostics and Performance</b>")
        diagnostics_level = QtWidgets.QComboBox()
        diagnostics_level.addItems(["Off", "Basic", "Detailed", "Developer"])
        diagnostics_level.setCurrentText(
            getattr(self, "diagnostics_level", "basic").title()
        )
        full_paths = QtWidgets.QCheckBox(
            "Include full music file paths in exported diagnostics"
        )
        full_paths.setChecked(
            bool(getattr(self, "diagnostics_include_full_paths", False))
        )
        diagnostics_explanation = QtWidgets.QLabel(
            "Performance diagnostics record timings, failures and application "
            "state to help identify freezes, audio problems and slow operations. "
            "Detailed diagnostics may create larger log files but do not record "
            "your music or audio."
        )
        diagnostics_explanation.setWordWrap(True)
        layout.addRow(diagnostics_heading)
        layout.addRow("Diagnostics level", diagnostics_level)
        layout.addRow("", full_paths)
        layout.addRow(diagnostics_explanation)

        # -- Plex (Stage 1 connection primitives + account sign-in/server --
        # -- discovery added before Stage 2 browsing -- see CODEX_HANDOFF --
        # -- .md's "Plex account authentication" section). Primary route --
        # -- is Plex account sign-in (PIN flow) + automatic server --
        # -- discovery; manual server URL/token entry is now an Advanced --
        # -- fallback. No Plex browsing/playback yet. --------------------
        plex_heading = QtWidgets.QLabel("<b>Plex</b>")
        plex_enabled = QtWidgets.QCheckBox("Enable Plex integration")
        plex_enabled.setObjectName("plexEnabledCheckbox")
        plex_enabled.setChecked(self.plex_preferences.enabled)

        # -- Primary route: account sign-in + discovered server --------
        def _plex_account_status_text(username: str) -> str:
            return f"Signed in as: {username}" if username else "Not signed in"
        plex_account_status_label = QtWidgets.QLabel(
            _plex_account_status_text(self.plex_preferences.account_username)
        )
        plex_account_status_label.setObjectName("plexAccountStatusLabel")
        plex_signin_button = QtWidgets.QPushButton("Sign in to Plex")
        plex_signin_button.setObjectName("plexSignInButton")
        plex_signin_cancel_button = QtWidgets.QPushButton("Cancel")
        plex_signin_cancel_button.setObjectName("plexSignInCancelButton")
        plex_signin_cancel_button.setVisible(False)
        plex_signout_button = QtWidgets.QPushButton("Sign Out")
        plex_signout_button.setObjectName("plexSignOutButton")
        plex_signout_button.setVisible(bool(self.plex_preferences.account_token))
        plex_signin_button.setVisible(not bool(self.plex_preferences.account_token))
        plex_server_combo = QtWidgets.QComboBox()
        plex_server_combo.setObjectName("plexServerCombo")

        def _plex_initial_status_text() -> str:
            prefs = self.plex_preferences
            if prefs.server_name:
                return (
                    f"Last known server: {prefs.server_name}"
                    + (f" (version {prefs.server_version})" if prefs.server_version else "")
                    + " -- not re-tested this session."
                )
            return "Not connected yet."
        plex_status_label = QtWidgets.QLabel(_plex_initial_status_text())
        plex_status_label.setObjectName("plexStatusLabel")
        plex_status_label.setWordWrap(True)

        # -- Advanced/manual fallback -----------------------------------
        plex_use_manual_server = QtWidgets.QCheckBox("Advanced: Use Manual Server URL")
        plex_use_manual_server.setObjectName("plexUseManualServerCheckbox")
        plex_use_manual_server.setChecked(self.plex_preferences.use_manual_server)
        plex_server_address = QtWidgets.QLineEdit(self.plex_preferences.server_address)
        plex_server_address.setObjectName("plexServerAddressField")
        plex_server_address.setPlaceholderText("http://192.168.x.x:32400")
        plex_token = QtWidgets.QLineEdit(self.plex_preferences.token)
        plex_token.setObjectName("plexTokenField")
        plex_token.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        plex_show_token = QtWidgets.QCheckBox("Show token")
        plex_show_token.setObjectName("plexShowTokenCheckbox")

        def _toggle_plex_token_visibility(checked):
            plex_token.setEchoMode(
                QtWidgets.QLineEdit.EchoMode.Normal if checked
                else QtWidgets.QLineEdit.EchoMode.Password
            )
        plex_show_token.toggled.connect(_toggle_plex_token_visibility)
        plex_token_note = QtWidgets.QLabel(
            "Stored unencrypted in this user's local configuration file "
            "(config.json under %LOCALAPPDATA%\\Bills Music Player), the "
            "same way every other Bills Music Player setting is stored -- "
            "there is currently no encrypted credential store for this "
            "token or the Plex account token above. Never included in "
            "logs or diagnostics."
        )
        plex_token_note.setWordWrap(True)
        plex_test_button = QtWidgets.QPushButton("Test Connection")
        plex_test_button.setObjectName("plexTestConnectionButton")

        def _sync_plex_manual_controls(_checked=None):
            manual = plex_use_manual_server.isChecked()
            for widget in (plex_server_address, plex_token, plex_show_token,
                           plex_token_note, plex_test_button):
                widget.setVisible(manual)
            for widget in (plex_signin_button, plex_signout_button,
                           plex_account_status_label, plex_server_combo):
                widget.setEnabled(not manual)
        plex_use_manual_server.toggled.connect(_sync_plex_manual_controls)
        _sync_plex_manual_controls()

        plex_music_library = QtWidgets.QComboBox()
        plex_music_library.setObjectName("plexMusicLibraryCombo")
        plex_video_library = QtWidgets.QComboBox()
        plex_video_library.setObjectName("plexVideoLibraryCombo")
        plex_karaoke_library = QtWidgets.QComboBox()
        plex_karaoke_library.setObjectName("plexKaraokeLibraryCombo")

        def _populate_plex_library_combo(combo, saved_id: str, saved_name: str):
            combo.clear()
            combo.addItem("(None)", "")
            if saved_id:
                combo.addItem(saved_name or saved_id, saved_id)
            combo.setCurrentIndex(combo.count() - 1)

        _populate_plex_library_combo(
            plex_music_library,
            self.plex_preferences.music_library_id,
            self.plex_preferences.music_library_name,
        )
        _populate_plex_library_combo(
            plex_video_library,
            self.plex_preferences.video_library_id,
            self.plex_preferences.video_library_name,
        )
        _populate_plex_library_combo(
            plex_karaoke_library,
            self.plex_preferences.karaoke_library_id,
            self.plex_preferences.karaoke_library_name,
        )

        def _repopulate_plex_library_combo(combo, libraries):
            previous_id = combo.currentData()
            previous_text = combo.currentText()
            combo.clear()
            combo.addItem("(None)", "")
            seen_ids = set()
            for lib in libraries:
                label = f"{lib['title']} ({lib['type']})" if lib.get("type") else lib["title"]
                combo.addItem(label, lib["key"])
                seen_ids.add(lib["key"])
            if previous_id and previous_id not in seen_ids:
                # Saved/previously-selected library no longer discovered --
                # retain it (shown as unavailable) rather than silently
                # dropping the user's existing mapping; they can pick a
                # different one if they choose.
                combo.addItem(f"{previous_text} (unavailable)", previous_id)
                combo.setCurrentIndex(combo.count() - 1)
            elif previous_id:
                idx = combo.findData(previous_id)
                combo.setCurrentIndex(idx if idx >= 0 else 0)
            else:
                combo.setCurrentIndex(0)

        plex_discovery_state = {"libraries": None, "server_name": "", "version": "", "machine_identifier": ""}
        plex_account_state = {
            "account_token": self.plex_preferences.account_token,
            "account_username": self.plex_preferences.account_username,
            "server_client_identifier": self.plex_preferences.server_client_identifier,
            "server_config_id_map": dict(self.plex_preferences.server_config_id_map),
            "servers": [],  # last PlexServerDiscoveryWorker result, raw dicts
            # Per-server library mapping (item 3): section keys are only
            # meaningful within one Plex server, so this is keyed by
            # server_client_identifier, never a flat/global mapping.
            "library_mappings_by_server": {
                server_id: dict(mapping)
                for server_id, mapping in self.plex_preferences.library_mappings_by_server.items()
            },
        }
        plex_dialog_state = {"closed": False}
        plex_active_workers = []  # keeps strong refs so PyQt doesn't GC an in-flight QThread

        def _plex_track_worker(worker):
            plex_active_workers.append(worker)
            reg_token = self._worker_registry.register(
                "plex_stage2_auth", thread=worker, wait_ms=10000,
            )

            def _on_finished(worker=worker, reg_token=reg_token):
                self._worker_registry.unregister(reg_token)
                if worker in plex_active_workers:
                    plex_active_workers.remove(worker)
            worker.finished.connect(_on_finished)
            worker.setParent(dialog)

        def _on_plex_test_result(result):
            if getattr(self, "_closing", False) or plex_dialog_state["closed"]:
                return
            try:
                plex_test_button.setEnabled(True)
                if result.get("success"):
                    libs = result.get("libraries", [])
                    plex_discovery_state["libraries"] = libs
                    plex_discovery_state["server_name"] = result.get("friendly_name", "")
                    plex_discovery_state["version"] = result.get("version", "")
                    plex_discovery_state["machine_identifier"] = result.get("machine_identifier", "")
                    plex_status_label.setText(
                        "Connected to Plex server: {}\nVersion: {}\nLibraries found: {}".format(
                            result.get("friendly_name") or "(unnamed)",
                            result.get("version") or "unknown",
                            len(libs),
                        )
                    )
                    for combo in (plex_music_library, plex_video_library, plex_karaoke_library):
                        _repopulate_plex_library_combo(combo, libs)
                else:
                    plex_status_label.setText(result.get("reason", "Connection failed"))
            except RuntimeError:
                pass  # dialog/widgets already torn down

        def _fetch_libraries_for(server_address: str, token: str):
            plex_status_label.setText("Fetching libraries…")
            client_id = self.plex_preferences.client_identifier or generate_client_identifier()
            worker = PlexConnectionTestWorker(server_address, token, client_id)
            _plex_track_worker(worker)
            worker.finished_result.connect(_on_plex_test_result)
            worker.start()

        def _start_plex_connection_test():
            server_text = plex_server_address.text().strip()
            token_text = plex_token.text()
            plex_test_button.setEnabled(False)
            plex_status_label.setText("Testing connection…")
            _fetch_libraries_for(server_text, token_text)
        plex_test_button.clicked.connect(_start_plex_connection_test)

        def _on_connection_resolved(result, server_entry):
            if getattr(self, "_closing", False) or plex_dialog_state["closed"]:
                return
            try:
                if not result.get("success"):
                    plex_status_label.setText(result.get("reason", "Server unreachable"))
                    return
                plex_status_label.setText(
                    "Connected ({}).".format("local" if result.get("local") else "remote")
                )
                _fetch_libraries_for(result["uri"], result["access_token"])
            except RuntimeError:
                pass

        def _resolve_and_fetch(server_entry: dict):
            client_id = self.plex_preferences.client_identifier or generate_client_identifier()
            worker = PlexConnectionResolveWorker(
                client_id, server_entry["client_identifier"],
                server_entry["access_token"], server_entry["connections"],
            )
            _plex_track_worker(worker)
            worker.finished_result.connect(
                lambda result, server_entry=server_entry: _on_connection_resolved(result, server_entry)
            )
            plex_status_label.setText("Resolving server connection…")
            worker.start()

        def _on_server_combo_changed(index):
            if plex_use_manual_server.isChecked() or index < 0:
                return
            server_id = plex_server_combo.itemData(index)
            if not server_id:
                return
            server_entry = next(
                (s for s in plex_account_state["servers"] if s["client_identifier"] == server_id),
                None,
            )
            if server_entry is None:
                return
            plex_account_state["server_client_identifier"] = server_id
            if server_id not in plex_account_state["server_config_id_map"]:
                plex_account_state["server_config_id_map"][server_id] = generate_server_config_id()
            # Restore this server's own saved mapping immediately (never
            # another server's) -- shows correctly even before any fresh
            # fetch completes; a successful fetch below then repopulates
            # with the live discovered list, still preserving this
            # selection where it still exists (see
            # _repopulate_plex_library_combo).
            saved_mapping = plex_account_state["library_mappings_by_server"].get(server_id, {})
            _populate_plex_library_combo(
                plex_music_library,
                saved_mapping.get("music_library_id", ""), saved_mapping.get("music_library_name", ""),
            )
            _populate_plex_library_combo(
                plex_video_library,
                saved_mapping.get("video_library_id", ""), saved_mapping.get("video_library_name", ""),
            )
            _populate_plex_library_combo(
                plex_karaoke_library,
                saved_mapping.get("karaoke_library_id", ""), saved_mapping.get("karaoke_library_name", ""),
            )
            if not server_entry["connections"]:
                plex_status_label.setText("Server unreachable")
                return
            _resolve_and_fetch(server_entry)
        plex_server_combo.currentIndexChanged.connect(_on_server_combo_changed)

        def _populate_server_combo(servers, select_client_identifier=""):
            plex_account_state["servers"] = servers
            with QtCore.QSignalBlocker(plex_server_combo):
                plex_server_combo.clear()
                for server in servers:
                    label = server["name"] + ("" if server["owned"] else " (shared)")
                    plex_server_combo.addItem(label, server["client_identifier"])
                idx = (
                    plex_server_combo.findData(select_client_identifier)
                    if select_client_identifier else -1
                )
                plex_server_combo.setCurrentIndex(idx if idx >= 0 else (0 if servers else -1))
            if plex_server_combo.currentIndex() >= 0:
                _on_server_combo_changed(plex_server_combo.currentIndex())

        def _on_discovery_result(result):
            if getattr(self, "_closing", False) or plex_dialog_state["closed"]:
                return
            try:
                if not result.get("success"):
                    plex_status_label.setText(result.get("reason", "Server discovery failed"))
                    return
                servers = result.get("servers", [])
                if not servers:
                    plex_status_label.setText("No Plex servers found for this account.")
                    return
                _populate_server_combo(
                    servers, plex_account_state["server_client_identifier"],
                )
            except RuntimeError:
                pass

        def _start_server_discovery():
            client_id = self.plex_preferences.client_identifier or generate_client_identifier()
            worker = PlexServerDiscoveryWorker(client_id, plex_account_state["account_token"])
            _plex_track_worker(worker)
            worker.finished_result.connect(_on_discovery_result)
            plex_status_label.setText("Discovering Plex servers…")
            worker.start()

        def _on_signin_result(result):
            if getattr(self, "_closing", False) or plex_dialog_state["closed"]:
                return
            try:
                plex_signin_cancel_button.setVisible(False)
                plex_signin_button.setEnabled(True)
                if result.get("success"):
                    plex_account_state["account_token"] = result.get("account_token", "")
                    plex_account_state["account_username"] = result.get("username", "")
                    plex_account_status_label.setText(
                        _plex_account_status_text(plex_account_state["account_username"])
                    )
                    plex_signin_button.setVisible(False)
                    plex_signout_button.setVisible(True)
                    _start_server_discovery()
                else:
                    reason = result.get("reason", "Sign-in failed")
                    plex_status_label.setText(
                        "" if reason == "Cancelled" else f"Plex sign-in failed: {reason}"
                    )
            except RuntimeError:
                pass

        def _on_pin_ready(auth_url):
            if getattr(self, "_closing", False) or plex_dialog_state["closed"]:
                return
            import webbrowser
            try:
                webbrowser.open(auth_url)
            except Exception:
                pass
            try:
                plex_status_label.setText("Waiting for Plex sign-in…")
                plex_signin_cancel_button.setVisible(True)
            except RuntimeError:
                pass

        def _start_plex_signin():
            client_id = self.plex_preferences.client_identifier or generate_client_identifier()
            plex_signin_button.setEnabled(False)
            worker = PlexSignInWorker(client_id)
            _plex_track_worker(worker)
            worker.pin_ready.connect(_on_pin_ready)
            worker.finished_result.connect(_on_signin_result)

            def _on_cancel(worker=worker):
                worker.request_cancel()
            plex_signin_cancel_button.clicked.connect(_on_cancel)
            worker.start()
        plex_signin_button.clicked.connect(_start_plex_signin)

        def _start_plex_signout():
            # Clears account auth state and any cached per-server access
            # token/resolved connection (all sensitive/transient) --
            # retains server_config_id_map and library_mappings_by_server
            # (non-sensitive, no tokens, needed to keep existing Plex
            # queue entries' identity/mapping meaningful if the user signs
            # back in later; those queue rows are NOT deleted by signing
            # out -- they just show unavailable, same as any other
            # temporarily-unreachable Plex state). server_client_identifier
            # is also kept so the same server can be pre-selected again on
            # a future sign-in, matching "restore selected server" intent.
            plex_account_state["account_token"] = ""
            plex_account_state["account_username"] = ""
            self._plex_active_connection_uri = ""
            self._plex_active_access_token = ""
            self._plex_status = "unknown"
            plex_account_status_label.setText(_plex_account_status_text(""))
            plex_signin_button.setVisible(True)
            plex_signout_button.setVisible(False)
            plex_server_combo.clear()
            plex_status_label.setText("Not connected yet.")
        plex_signout_button.clicked.connect(_start_plex_signout)

        dialog.finished.connect(lambda *_: plex_dialog_state.update(closed=True))

        # If already signed in from a previous session and not in manual
        # mode, populate the server list on dialog open -- no extra click
        # needed to see/change the current server, matching "restore
        # selected server" without a Local-scan-style rescan-on-open.
        if plex_account_state["account_token"] and not plex_use_manual_server.isChecked():
            _start_server_discovery()

        plex_explanation = QtWidgets.QLabel(
            "Sign in with your Plex account for automatic server discovery "
            "(recommended), or use Advanced manual server entry below. "
            "Plex browsing and playback are not available yet."
        )
        plex_explanation.setWordWrap(True)
        layout.addRow(plex_heading)
        layout.addRow("", plex_enabled)
        layout.addRow(plex_account_status_label)
        signin_row = QtWidgets.QHBoxLayout()
        signin_row.addWidget(plex_signin_button)
        signin_row.addWidget(plex_signin_cancel_button)
        signin_row.addWidget(plex_signout_button)
        layout.addRow("", signin_row)
        layout.addRow("Server", plex_server_combo)
        layout.addRow(plex_status_label)
        layout.addRow("", plex_use_manual_server)
        layout.addRow("Server address", plex_server_address)
        layout.addRow("Plex token", plex_token)
        layout.addRow("", plex_show_token)
        layout.addRow(plex_token_note)
        layout.addRow("", plex_test_button)
        layout.addRow("Music library", plex_music_library)
        layout.addRow("Video library", plex_video_library)
        layout.addRow("Karaoke library", plex_karaoke_library)
        layout.addRow(plex_explanation)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        button_row = QtWidgets.QHBoxLayout()
        button_row.setContentsMargins(12, 8, 12, 12)
        button_row.addWidget(buttons)
        outer_layout.addLayout(button_row)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            previous_waveform_seekbar_enabled = self.waveform_seekbar_enabled
            self.normalisation_enabled = enabled.isChecked()
            self.normalisation_mode = mode.currentText().lower()
            self.prevent_clipping = clipping.isChecked()
            self.auto_loudness_analysis = auto.isChecked()
            self.target_lufs = target.value()
            self.tagged_preamp_db = tagged.value()
            self.untagged_preamp_db = untagged.value()
            self._gain_snapshot_cache.clear()
            self.auto_playback_recovery = recover.isChecked()
            self.allow_backend_fallback = fallback.isChecked()
            self.track_transition_mode = transition_mode.currentText().lower()
            self.crossfade_seconds = crossfade_duration.value()
            self.warn_before_adding_duplicate_queue_tracks = warn_duplicates.isChecked()
            self.video_playback_enabled = video_enabled.isChecked()
            self.video_start_fullscreen = video_fullscreen_start.isChecked()
            self.video_return_to_normal_display_on_end = video_return_normal.isChecked()
            self.video_transitions_enabled = video_transitions_enabled.isChecked()
            self.video_transition_style = video_transition_style.currentText()
            self.video_transition_duration_seconds = video_transition_duration.value()
            self.video_transition_automatic_lead_seconds = video_transition_lead.value()
            self.video_transition_manual_duration_seconds = (
                video_transition_manual_duration.value()
            )
            self.video_transition_enabled_effects = {
                effect: checkbox.isChecked()
                for effect, checkbox in effect_checks.items()
            }
            transition_manager = getattr(self, "_video_transition_manager", None)
            if transition_manager is not None:
                transition_manager.configure(
                    VideoTransitionPreferences(
                        enabled=self.video_transitions_enabled,
                        style=self.video_transition_style,
                        duration_seconds=self.video_transition_duration_seconds,
                        automatic_lead_seconds=(
                            self.video_transition_automatic_lead_seconds
                        ),
                        manual_duration_seconds=(
                            self.video_transition_manual_duration_seconds
                        ),
                        enabled_effects=dict(self.video_transition_enabled_effects),
                    )
                )
            # Forced off regardless of the widget's state unless both the
            # compile-time and runtime-probe gates already agreed to show
            # it enabled -- see DUAL_VIDEO_TRANSITIONS_AVAILABLE above.
            self.video_dual_transitions_enabled = (
                DUAL_VIDEO_TRANSITIONS_AVAILABLE
                and self._gpu_dual_capability is True
                and video_dual_transitions_enabled.isChecked()
            )
            selected_gpu_effect = video_gpu_transition_effect.currentText()
            self.video_gpu_transition_effect = (
                selected_gpu_effect if selected_gpu_effect in GPU_TRANSITION_STYLES
                else "Cross Dissolve"
            )
            # Same forced-off-unless-reachable shape as video_dual_transitions_
            # enabled just above -- Smart Video Transition Points only ever
            # has an effect when the GPU dual engine itself is running.
            self.video_smart_transition_points_enabled = (
                self.video_dual_transitions_enabled
                and video_smart_transition_points_enabled.isChecked()
            )
            self.video_avoid_black_outros = video_avoid_black_outros.isChecked()
            self.video_skip_black_intros = video_skip_black_intros.isChecked()
            self.video_crossfade_audio_enabled = (
                self.video_dual_transitions_enabled
                and video_crossfade_audio_enabled.isChecked()
            )
            self.video_crossfade_audio_curve = video_crossfade_audio_curve.currentText()
            self._apply_gpu_dual_mode_state()
            self.waveform_seekbar_enabled = waveform_seekbar.isChecked()
            self.diagnostics_level = diagnostics_level.currentText().lower()
            self.diagnostics_include_full_paths = full_paths.isChecked()
            self.diagnostics.configure(
                self.diagnostics_level,
                self.diagnostics_include_full_paths,
            )
            self.party_mode_screen_name = party_screen.currentData() or ""
            self.party_mode_default_layout = party_layout_combo.currentText().lower()
            self.party_mode_show_up_next = party_show_up_next.isChecked()
            self.party_mode_up_next_count = party_up_next_count.value()
            self.party_mode_show_clock = party_show_clock.isChecked()
            self.party_mode_show_remaining_playlist_time = (
                party_show_remaining_playlist.isChecked()
            )
            self.party_mode_auto_hide_ms = party_auto_hide.value() * 1000
            self.party_mode_animations_enabled = party_animations.isChecked()
            self.party_mode_visual_quality = party_quality.currentText().lower()
            if self.party_mode is not None:
                self.party_mode.apply_preferences()

            def _plex_selected_library(combo):
                library_id = combo.currentData() or ""
                library_name = combo.currentText() if library_id else ""
                # Strip the "(unavailable)"/"(type)" display decoration
                # before persisting -- only the real Plex title should be
                # stored as the display-name fallback.
                if library_id and library_name.endswith(" (unavailable)"):
                    library_name = library_name[: -len(" (unavailable)")]
                elif library_id and library_name.endswith(")") and " (" in library_name:
                    library_name = library_name.rsplit(" (", 1)[0]
                return library_id, library_name

            music_lib_id, music_lib_name = _plex_selected_library(plex_music_library)
            video_lib_id, video_lib_name = _plex_selected_library(plex_video_library)
            karaoke_lib_id, karaoke_lib_name = _plex_selected_library(plex_karaoke_library)
            manual_mode = plex_use_manual_server.isChecked()
            if manual_mode:
                # Stage 1 behaviour, unchanged: one server_config_id for
                # the single manually-entered server, generated once.
                server_config_id = self.plex_preferences.server_config_id
                if plex_enabled.isChecked() and not server_config_id:
                    server_config_id = generate_server_config_id()
                server_config_id_map = dict(self.plex_preferences.server_config_id_map)
                server_client_identifier = self.plex_preferences.server_client_identifier
            else:
                # Account mode: server_config_id is looked up (never
                # blindly generated here) from the map keyed by Plex's own
                # stable per-server clientIdentifier -- _on_server_combo_
                # changed already created and cached one the moment a
                # server was selected, so a real selection always has an
                # entry here by the time Accept runs.
                server_client_identifier = plex_account_state["server_client_identifier"]
                server_config_id_map = plex_account_state["server_config_id_map"]
                server_config_id = server_config_id_map.get(server_client_identifier, "")
            library_mappings_by_server = dict(plex_account_state["library_mappings_by_server"])
            if not manual_mode and server_client_identifier:
                # Persist under *this* server's own key only -- never
                # overwrites another server's saved mapping.
                library_mappings_by_server[server_client_identifier] = {
                    "music_library_id": music_lib_id, "music_library_name": music_lib_name,
                    "video_library_id": video_lib_id, "video_library_name": video_lib_name,
                    "karaoke_library_id": karaoke_lib_id, "karaoke_library_name": karaoke_lib_name,
                }
            self.plex_preferences = dataclasses.replace(
                self.plex_preferences,
                enabled=plex_enabled.isChecked(),
                use_manual_server=manual_mode,
                server_address=plex_server_address.text().strip(),
                token=plex_token.text(),
                account_token=plex_account_state["account_token"],
                account_username=plex_account_state["account_username"],
                server_config_id=server_config_id,
                server_config_id_map=server_config_id_map,
                server_client_identifier=server_client_identifier,
                server_name=(
                    plex_discovery_state["server_name"] or self.plex_preferences.server_name
                ),
                server_version=(
                    plex_discovery_state["version"] or self.plex_preferences.server_version
                ),
                server_machine_identifier=(
                    plex_discovery_state["machine_identifier"]
                    or self.plex_preferences.server_machine_identifier
                ),
                music_library_id=music_lib_id, video_library_id=video_lib_id,
                music_library_name=music_lib_name, video_library_name=video_lib_name,
                karaoke_library_id=karaoke_lib_id, karaoke_library_name=karaoke_lib_name,
                library_mappings_by_server=library_mappings_by_server,
            )
            self._restart_performance_probes()
            self._save_user_settings()
            getattr(self, "_schedule_queue_duration_refresh", lambda *_: None)("crossfade_settings_changed")
            if self.current_path:
                self._set_slot_gain("active", self._gain_for_path(self.current_path))
                self.set_master_volume(self.master_volume)
            if self.waveform_seekbar_enabled != previous_waveform_seekbar_enabled:
                self.statusBar().showMessage(
                    "Waveform seek bar setting changed; restart Bills Music "
                    "Player for this to take effect.",
                    8000,
                )

    def next_track(self):
        # v1.0.71: a manual Next must cleanly abandon any in-flight mixed-
        # media transition before request_manual_next() looks at
        # _current_media_type -- Video->Audio preparation/overlap keeps
        # _current_media_type == VIDEO throughout, and left uncancelled it
        # could otherwise race with the video-video transition manager
        # deciding to start its own transition on top of it.
        cancelled_video_to_audio = False
        if self._mixed_transition_state != "idle":
            cancelled_video_to_audio = self._mixed_transition_direction == "video_to_audio"
            self._cancel_mixed_media_transition("manual_next")
        # Real-device bug (2026-09-05, user report): "press Next while a
        # video is crossfading into a music track" silently did nothing
        # further -- the video just kept playing. Cancelling a video_to_
        # audio mixed transition (above) deliberately leaves
        # _current_media_type == VIDEO (the mixed system's own comment:
        # "the video was never touched, so it keeps playing normally"),
        # so on its own the check below cannot tell "genuinely still on a
        # video with no handoff in progress" apart from "was just in the
        # middle of the mixed-media system's own video->audio handoff,
        # which already fully owns this boundary and has just abandoned
        # it." VideoTransitionManager.request_manual_next() -- built for
        # video<->video Phase 1 fades, not mixed-media video<->audio --
        # matches that exact situation too (its own supports() check
        # only looks at current_media_type, still VIDEO here) and can
        # claim the request without ever reaching _next_track() below --
        # confirmed via real diagnostics: repeated mixed_transition_
        # cancelled(direction=video_to_audio, reason=manual_next) events
        # with nothing else ever following, i.e. next_track() kept
        # returning early here every time.
        transition_manager = getattr(self, "_video_transition_manager", None)
        if (
            not cancelled_video_to_audio
            and transition_manager is not None
            and transition_manager.request_manual_next(self._current_media_type)
        ):
            self._announce_accessible_status("Next track selected")
            return
        self._next_track("manual-next")
        self._announce_accessible_status("Next track selected")

    def _next_track(self, reason: str):
        if self._mixed_transition_state != "idle":
            self._cancel_mixed_media_transition(f"superseded_by_{reason}")
        if reason in ("quiet-end", "near-end", "vlc-ended", "normal-end", "video-ended"):
            self._record_track_completion(reason)
            if self.sleep_timer.is_stop_after_track:
                self._complete_stop_after_track(reason)
                return
        if not self.queue and not self._playback_fallback_paths():
            return
        if self.fade_active or self.prebuffer_active:
            self._audio_log(f"next ignored; reason={reason}; fade_active={self.fade_active}; prebuffer_active={self.prebuffer_active}")
            return
        skipped_missing = 0
        next_queue_row = self._next_unplayed_queue_row()
        while (
            next_queue_row is not None
            and self._queue_entry_is_missing(next_queue_row)
        ):
            self.queue_played[next_queue_row] = True
            skipped_missing += 1
            self.diagnostics.record(
                "playlist", "missing_entry_skipped",
                severity="warning",
                details={
                    "reason": reason,
                    **self.diagnostics.path_details(
                        self.queue[next_queue_row]
                    ),
                },
                minimum_level="basic",
            )
            next_queue_row = self._next_unplayed_queue_row()
        if skipped_missing:
            self._refresh_queue_list(
                keep_played_bottom=False, cached_details_only=True,
                reason="missing_playlist_entries_skipped",
            )
            self.statusBar().showMessage(
                f"Skipped {skipped_missing} missing playlist "
                f"{'track' if skipped_missing == 1 else 'tracks'}",
                5000,
            )
        if next_queue_row is not None:
            next_path = self.queue[next_queue_row]
            mixed_direction = self._mixed_media_transition_eligible(next_path)
            if mixed_direction is not None:
                self._audio_log(
                    f"mixed-media transition requested; reason={reason}; "
                    f"direction={mixed_direction}; file={self._audio_name(next_path)!r}"
                )
                self._begin_mixed_media_transition(mixed_direction, next_queue_row, next_path, reason)
                return
            crossfade = self._crossfade_eligible_for_transition(next_path)
            self._audio_log(
                f"next requested; reason={reason}; queue_row={next_queue_row}; "
                f"file={self._audio_name(next_path)!r}"
            )
            if self._play_path_direct(next_path, crossfade=crossfade, immediate_crossfade=True):
                self._mark_queue_row_played(next_queue_row)
            return
        fallback = self._playback_fallback_paths()
        if not fallback:
            return
        if self.current_path in fallback:
            next_index = (fallback.index(self.current_path) + 1) % len(fallback)
        else:
            next_index = 0
        next_path = fallback[next_index]
        crossfade = self._crossfade_eligible_for_transition(next_path)
        self._playback_context_index = next_index
        self._audio_log(
            f"next requested; reason={reason}; library_index={next_index}; "
            f"file={self._audio_name(next_path)!r}"
        )
        self._play_path_direct(next_path, crossfade=crossfade, immediate_crossfade=True)

    def _crossfade_eligible_for_transition(self, next_path: str) -> bool:
        """The user's crossfade preference only ever applies audio-to-audio.
        Any transition touching video (audio->video, video->audio,
        video->video) takes the existing normal-transition branch instead --
        no new code path, just routing more cases into it."""
        if self.track_transition_mode == "normal":
            return False
        if self._current_media_type != MediaType.AUDIO or not is_audio(next_path):
            self.diagnostics.record(
                "playback", "crossfade_skipped_for_video",
                details={
                    "current_media_type": self._current_media_type.value,
                    "next_media_type": classify_path(next_path).value,
                },
                minimum_level="detailed",
            )
            return False
        return True

    def _record_track_completion(self, reason: str, generation=None):
        if generation is None:
            generation = getattr(self, "_playback_generation", None)
        if generation is None or generation == self._last_completed_playback_generation:
            return False
        self._last_completed_playback_generation = generation
        self.diagnostics.counters["tracks_completed"] += 1
        self.diagnostics.record(
            "playback", "track_completed",
            details={"reason": reason},
            minimum_level="detailed",
        )
        if self._current_media_type == MediaType.KARAOKE:
            self.diagnostics.record(
                "playback", "karaoke_completed",
                details={"reason": reason, **self.diagnostics.path_details(self.current_path or "")},
            )
        return True

    def prev_track(self):
        if self._mixed_transition_state != "idle":
            self._cancel_mixed_media_transition("prev_track")
        if not self.queue and not self._playback_fallback_paths():
            return
        if self.fade_active or self.prebuffer_active:
            self._audio_log(f"previous ignored; fade_active={self.fade_active}; prebuffer_active={self.prebuffer_active}")
            return
        previous_queue_row = self._previous_played_queue_row()
        if previous_queue_row is not None:
            prev_path = self.queue[previous_queue_row]
            crossfade = self._crossfade_eligible_for_transition(prev_path)
            self._audio_log(
                f"previous requested; queue_row={previous_queue_row}; "
                f"file={self._audio_name(prev_path)!r}"
            )
            self._play_path_direct(prev_path, crossfade=crossfade, immediate_crossfade=True)
            return
        fallback = self._playback_fallback_paths()
        if not fallback:
            return
        if self.current_path in fallback:
            prev_index = (fallback.index(self.current_path) - 1) % len(fallback)
        else:
            prev_index = 0
        prev_path = fallback[prev_index]
        crossfade = self._crossfade_eligible_for_transition(prev_path)
        self._playback_context_index = prev_index
        self._audio_log(
            f"previous requested; library_index={prev_index}; "
            f"file={self._audio_name(prev_path)!r}"
        )
        self._play_path_direct(prev_path, crossfade=crossfade, immediate_crossfade=True)
        self._announce_accessible_status("Previous track selected")

    def _tick(self):
        self._sync_mini_player()
        getattr(self, "_maybe_tick_queue_duration_refresh", lambda: None)()
        if getattr(self, "cast_active", False):
            snapshot = self.cast_controller.snapshot()
            self._record_cast_clock_snapshot(snapshot)
            state = snapshot.get("state", "")
            position = float(snapshot.get("position", 0.0) or 0.0)
            duration = float(snapshot.get("duration", 0.0) or 0.0)
            if duration > 0 and not self.scrubbing:
                self._update_progress(int(position * 1000), int(duration * 1000))
            if state == "playing":
                self._cast_completion_armed = True
                self._playback_intentionally_paused = False
            elif state == "paused":
                self._playback_intentionally_paused = True
            elif is_natural_completion(
                snapshot, self._cast_completion_armed
            ):
                self._cast_completion_armed = False
                self._record_track_completion("cast-ended")
                self._next_track("cast-ended")
            elif state == "disconnected" and not self._cast_loss_reported:
                self._cast_loss_reported = True
                self._cast_completion_armed = False
                self._playback_expected = False
                self.statusBar().showMessage(
                    "Cast connection lost — choose Cast again or return to This Computer",
                    10000,
                )
            self._cast_last_state = state
            return
        self._sync_party_mode()
        self._update_recently_played_tracking()
        self._lyrics_tick()
        self._sync_karaoke_position()
        self._check_playback_health()
        if self._current_media_type == MediaType.VIDEO:
            # None of the near-end/quiet-end/normal-end triggers below are
            # meaningful for video -- they read simple_player/active_player
            # position, and those audio backends are stopped (not reloaded)
            # once video starts, so a player left holding the *previous*
            # track's near-the-end position/length was making this fire
            # _next_track() within the first tick or two of video playback.
            # v1.0.71: the Video->Audio mixed-transition trigger is the one
            # exception -- it needs its own, much simpler position-based
            # check (see _maybe_prepare_mixed_transition_from_video).
            self._maybe_prepare_mixed_transition_from_video()
            return
        # Built-in player crossfade is driven by _fade_tick, with BASS doing the audio-thread ramp itself.
        if self._use_builtin_player():
            try:
                length_s = self.simple_player.get_length()
                current_s = self.simple_player.get_pos()
            except Exception:
                length_s = 0.0
                current_s = 0.0
            if length_s > 0:
                remaining = length_s - current_s
                prebuffer_sec = PREBUFFER_MS / 1000.0
                if self._use_bass_backend():
                    prebuffer_sec = max(prebuffer_sec, 2.0)
                mode = "normal" if self._current_media_type == MediaType.KARAOKE else self.track_transition_mode
                if mode == "crossfade":
                    # Some mixes carry dead air after the musical fade-out. Use the
                    # analyzer's RMS reading near the end to begin the next fade when
                    # the track has audibly gone quiet, not only when the file ends.
                    rms_db = self._current_rms_db()
                    if rms_db is not None:
                        if remaining <= FADE_TRIGGER_WINDOW_SECONDS:
                            if (
                                self._last_quiet_debug_remaining is None
                                or self._last_quiet_debug_remaining - remaining >= 5.0
                            ):
                                self._last_quiet_debug_remaining = remaining
                                self._audio_log(
                                    f"backend={self._backend_label().lower()} quiet-end check; "
                                    f"remaining={remaining:.2f}s; rms={rms_db:.1f}db; "
                                    f"threshold={FADE_TRIGGER_DB:.1f}db"
                                )
                            if rms_db <= FADE_TRIGGER_DB:
                                self.quiet_count += 1
                            else:
                                self.quiet_count = 0
                            if self.quiet_count >= FADE_QUIET_FRAMES and not self.fade_active and not self.prebuffer_active and not self.pending_next:
                                self._audio_log(
                                    f"backend={self._backend_label().lower()} quiet-end trigger; "
                                    f"remaining={remaining:.2f}s; rms={rms_db:.1f}db"
                                )
                                self.pending_next = True
                                self.pending_builtin_crossfade_quiet = True
                                self._next_track("quiet-end")
                        else:
                            self.quiet_count = 0
                    if remaining <= (self.crossfade_seconds + prebuffer_sec):
                        if not self.fade_active and not self.prebuffer_active and not self.pending_next:
                            self.pending_next = True
                            self._next_track("near-end")
                elif mode == "normal":
                    if remaining <= NORMAL_TRANSITION_EPSILON_SECONDS:
                        if not self.fade_active and not self.prebuffer_active and not self.pending_next:
                            self.pending_next = True
                            self._next_track("normal-end")
                if not self.scrubbing:
                    self._update_progress(int(current_s * 1000), int(length_s * 1000))
            return

        if self.active_player and self.active_player.is_playing():
            if not self.fade_active:
                # Enforce steady-state volume to avoid drift after fades
                self.active_player.audio_set_volume(int(self.master_volume * self._sleep_timer_gain))
            length = self.active_player.get_length()
            current = self.active_player.get_time()
            if length > 0:
                remaining = (length - current) / 1000.0
                prebuffer_sec = PREBUFFER_MS / 1000.0
                mode = "normal" if self._current_media_type == MediaType.KARAOKE else self.track_transition_mode
                if mode == "crossfade":
                    rms_db = self._current_rms_db()
                    if rms_db is not None:
                        if remaining <= FADE_TRIGGER_WINDOW_SECONDS:
                            if rms_db <= FADE_TRIGGER_DB:
                                self.quiet_count += 1
                            else:
                                self.quiet_count = 0
                            if self.quiet_count >= FADE_QUIET_FRAMES and not self.fade_active and not self.prebuffer_active and not self.pending_next:
                                self._audio_log(
                                    f"backend=vlc quiet-end trigger; "
                                    f"remaining={remaining:.2f}s; rms={rms_db:.1f}db"
                                )
                                self.pending_next = True
                                self.pending_builtin_crossfade_quiet = True
                                self._next_track("quiet-end")
                        else:
                            self.quiet_count = 0
                    if remaining <= (self.crossfade_seconds + prebuffer_sec) and not self.fade_active and not self.prebuffer_active and not self.pending_next:
                        self.pending_next = True
                        self._next_track("near-end")
                # Normal mode relies on the vlc.State.Ended fallback below
                # rather than an early trigger, so each track finishes fully.
                if not self.scrubbing:
                    self._update_progress(current, length)
        elif self.active_player:
            state = self.active_player.get_state()
            if state == vlc.State.Ended and not self.fade_active:
                self._next_track("vlc-ended")
                self.pending_next = False

    def _load_tags(self, path: str) -> Track:
        info = self._read_tags(path)
        self._display_track_tags(info, path)
        return info

    def _display_meta_for_path(self, path: str) -> dict:
        """Best-effort display metadata (title/artist/album/genre) for
        `path` with no file I/O -- the single, shared source-aware lookup
        every current-track/queue-row/Recently-Played UI consumer should
        use instead of re-deriving a label from the path itself.

        self._meta_by_path is rebuilt from the source-aware
        _full_meta_list every time the search index refreshes -- browsing
        the Local tab while a Plex track is still current (or queued)
        silently replaces it with Local-only entries. self._plex_meta_by_path
        is populated once per Plex fetch and never touched by
        library-source/tab switching, so it's consulted as the persistent
        fallback for a plex:// identity. Returns {} if nothing is known
        anywhere -- every caller still needs its own last-resort basename
        fallback for that genuinely-untagged case."""
        meta = getattr(self, "_meta_by_path", {}).get(path)
        if not meta and is_plex_identity(path):
            meta = getattr(self, "_plex_meta_by_path", {}).get(path)
        return meta or {}

    def _load_cached_video_tags(self, path: str) -> Track:
        """Build the now-playing video details without opening the MP4.

        The library scan and Up Next enrichment have already collected the
        useful display metadata. Reading the file again here used to block
        the GUI/NAS path before the next video could be handed to the child
        process.
        """
        meta = self._display_meta_for_path(path)
        details = getattr(self, "queue_detail_cache", {}).get(path) or {}

        def cached(value, fallback="Unknown"):
            text = str(value or "").strip()
            return fallback if not text or text == "--" else text

        info = Track(
            path=path,
            title=cached(
                meta.get("title"),
                os.path.splitext(os.path.basename(path))[0],
            ),
            artist=cached(meta.get("artist")),
            album=cached(
                meta.get("album"),
                "Karaoke" if classify_path(path) == MediaType.KARAOKE else "Music Videos",
            ),
            genre=cached(meta.get("genre")),
            bitrate=cached(details.get("bitrate")),
            sample_rate="Unknown",
            channels="Unknown",
            duration=cached(details.get("time")),
        )
        self._display_track_tags(info, path)
        return info

    def _load_cached_audio_tags(self, path: str) -> Track:
        """Build the now-playing audio details without opening the file.

        _activate_track_ui() runs on every track change, including a
        Cast track auto-advancing from _tick() on the GUI thread. Reading
        tags synchronously here (the old behaviour) blocked that same
        _tick() loop for as long as the read took -- confirmed via a
        captured stall trace, and the direct cause of the Cast progress
        bar/equaliser freezing mid-session on a NAS-backed library. The
        library scan and Up Next queue enrichment have already collected
        this same display metadata, so use that cached data immediately;
        _queue_track_tags_async() below refreshes with the authoritative
        read once it completes off the GUI thread.
        """
        meta = self._display_meta_for_path(path)
        details = (
            getattr(self, "queue_detail_cache", {}).get(path)
            or self._cached_queue_analysis(path, validate_signature=False)
            or {}
        )

        def cached(value, fallback="Unknown"):
            text = str(value or "").strip()
            return fallback if not text or text == "--" else text

        return Track(
            path=path,
            title=cached(meta.get("title"), os.path.basename(path)),
            artist=cached(meta.get("artist")),
            album=cached(meta.get("album")),
            genre=cached(meta.get("genre")),
            bitrate=cached(details.get("bitrate")),
            sample_rate="Unknown",
            channels="Unknown",
            duration=cached(details.get("time")),
        )

    def _queue_track_tags_async(self, path: str):
        if getattr(self, "_closing", False):
            return
        if is_plex_identity(path):
            # Real-device bug (Stage 3A-r3): this worker's whole purpose
            # is to replace the cached display with the "authoritative"
            # read straight from the file via Mutagen -- read_full_tag_
            # display(path) opens `path` with MutagenFile(path, easy=True)
            # unconditionally. For a synthetic plex://.../<ratingKey>.<ext>
            # identity that open always fails (there is no such file), and
            # read_full_tag_display's own failure path does not raise --
            # it quietly returns {"title": "Unknown", "artist": "Unknown",
            # ...} as a *successful* result. tags_ready then fires with
            # that all-"Unknown" dict, and _on_track_tags_ready's
            # generation/path staleness guard does not catch it (this
            # really is the current, correct generation/path -- Mutagen
            # just has nothing to read), so it overwrites the title
            # _display_meta_for_path had already resolved correctly a
            # moment earlier. _plex_meta_by_path is already the
            # authoritative source for a Plex identity -- there is no
            # "more authoritative" local file read to chase.
            self.diagnostics.record(
                "now_playing", "track_tags_async_skipped_for_plex",
                details={
                    **self.diagnostics.path_details(path),
                    "reason": "plex_identity_has_no_local_file_to_read",
                },
                minimum_level="detailed",
            )
            return
        generation = self._playback_generation
        worker = TrackTagLoadWorker(path)
        worker.tags_ready.connect(
            lambda p, fields: self._on_track_tags_ready(p, fields, generation)
        )
        self._track_tag_load_workers.append(worker)
        token = self._worker_registry.register("track_tag_load", thread=worker, wait_ms=1500)

        def _on_finished(worker=worker, token=token):
            if worker in self._track_tag_load_workers:
                self._track_tag_load_workers.remove(worker)
            self._worker_registry.unregister(token)

        worker.finished.connect(_on_finished)
        worker.start()

    def _on_track_tags_ready(self, path: str, fields: Dict[str, str], generation: int):
        # A slower read for a track the user has already skipped past must
        # not clobber the (already-current) tag panel with stale data.
        if getattr(self, "_closing", False) or generation != self._playback_generation or path != self.current_path:
            return
        if is_plex_identity(path):
            # Defense in depth alongside _queue_track_tags_async's own
            # guard: a Mutagen-sourced result (the only kind this callback
            # ever receives) can never be a legitimate answer for a
            # synthetic plex:// identity -- see the matching comment
            # there. Never let it overwrite the already-correct
            # Plex-sourced title with Mutagen's own "no such file"
            # placeholder text.
            return
        info = Track(path=path, **fields)
        self._display_track_tags(info, path)

    def _display_track_tags(self, info: Track, path: str):
        html = self._format_tag_html(info, path)
        self.tag_box.setHtml(html)
        self.tag_scroll_pos = 0.0
        self.tag_box.verticalScrollBar().setValue(0)
        self.tag_reset_after_pause = False
        # Real-device bug (Stage 3A-r2): this used to re-derive the label
        # from the raw path (os.path.basename) instead of using the
        # already-resolved info.title the caller just built via
        # _load_cached_audio_tags/_load_cached_video_tags/_read_tags --
        # for a Plex identity (plex://.../<ratingKey>.<ext>), basename-of-
        # path is the raw numeric ratingKey. info.title already carries
        # the shared _display_meta_for_path fallback chain those loaders
        # use, so reuse it here instead of a worse, path-only answer.
        self.now_playing.setText(info.title or os.path.splitext(os.path.basename(path))[0])

    def _read_tags(self, path: str) -> Track:
        # Real file I/O (mutagen.File) -- only ever call this off the GUI
        # thread (see TrackTagLoadWorker / _load_cached_audio_tags, which
        # is what _activate_track_ui actually uses on every track change).
        return Track(path=path, **read_full_tag_display(path))

    def _format_tag_html(self, info: Track, path: str) -> str:
        label_color = "#63b3ff"
        value_color = "#e9eef2"
        ext = os.path.splitext(path)[1].upper().lstrip(".") or "Unknown"
        rows = [
            ("Title", info.title),
            ("Artist", info.artist),
            ("Album", info.album),
            ("Genre", info.genre),
            ("File Type", ext),
            ("Bitrate", info.bitrate),
            ("Sample Rate", info.sample_rate),
            ("Channels", info.channels),
            ("Duration", info.duration),
            ("Path", path),
        ]
        lines = []
        for label, value in rows:
            lines.append(
                f"<span style='color:{label_color};font-weight:600'>{label}:</span> "
                f"<span style='color:{value_color}'>{value}</span>"
            )
        return "<br>".join(lines)

    def _find_album_cover(self, album: str, artist: str, items: List[Tuple[int, int, str, str, str, str]]):
        """Legacy synchronous helper retained only for non-interactive compatibility."""
        cache_key = f"{artist}::{album}"
        if cache_key in self.album_cover_cache:
            return self.album_cover_cache[cache_key]
        if artist:
            cached_path = album_cover_cache_path(artist, album)
            if os.path.isfile(cached_path):
                try:
                    with open(cached_path, "rb") as f:
                        data = f.read()
                    self.album_cover_cache[cache_key] = data
                    return data
                except Exception:
                    pass
        cover = None
        for _, _, _, _, _, path in items:
            cover = read_cover_bytes(path)
            if cover:
                break
        self.album_cover_cache[cache_key] = cover
        return cover

    def _cached_artwork_pixmap(self, path: str, size: QtCore.QSize):
        """Return an existing library icon without doing artwork I/O or decoding."""
        meta = self._meta_by_path.get(path) or {}
        artist = str(meta.get("album_artist") or meta.get("artist") or "")
        album = str(meta.get("album") or "")
        cache_key = f"{artist}::{album}"

        album_key = self.album_key_by_path.get(path) or cache_key
        album_item = self.album_item_by_key.get(album_key)
        if album_item is not None:
            icon = album_item.icon(0)
            if not icon.isNull():
                pixmap = icon.pixmap(size)
                if not pixmap.isNull():
                    return pixmap

        return QtGui.QPixmap()

    def _on_artwork_diagnostic(self, details):
        event = details.get("event", "status")
        self.diagnostics.record(
            "artwork",
            f"artwork_{event}",
            status=details.get("status", "success"),
            duration_ms=details.get("worker_execution_ms"),
            severity="warning" if event == "failed" else "info",
            details=details,
            minimum_level="basic" if event == "failed" else "detailed",
            rate_limit_seconds=2.0 if event == "failed" else 0,
        )

    def _plex_thumb_http_source(self, thumb: str):
        """(url, headers) for a Plex-hosted thumbnail path (e.g.
        "/library/metadata/123/thumb/456"), or None if Plex isn't
        currently connected. Token travels as a header, never a query
        parameter -- matches plex_client.py's own convention -- and this
        return value must never be logged/placed in a diagnostic (see
        _request_album_artwork's cache key, which uses server_config_id +
        the thumb path itself, never the token, never the resolved URL)."""
        if not thumb:
            return None
        server_address, token = self._plex_effective_connection()
        if not server_address or not token:
            return None
        base_url = normalise_server_address(server_address)
        return (f"{base_url}{thumb}", {"X-Plex-Token": token})

    def _request_album_artwork(self, album_item, album, artist, items, album_key):
        if album_item is None:
            return False
        generation = self._library_search_generation
        paths = [entry[-1] for entry in items if entry[-1]][:3]
        disk_path = album_cover_cache_path(artist, album) if artist else None
        http_source = None
        cache_key = f"{artist}::{album}"
        if paths and is_plex_identity(paths[0]):
            # Plex-sourced album: never touch the Local on-disk cover
            # cache or attempt a local read on a synthetic path -- fetch
            # the server's own thumbnail instead, over HTTP with the
            # token as a header. Cache key is server-identity + the
            # thumb's own path (never the artist/album text, which could
            # collide with an unrelated Local album of the same name;
            # never the token).
            identity = parse_plex_identity(paths[0])
            thumb = ""
            for path in paths:
                meta = self._meta_by_path.get(path) or {}
                thumb = meta.get("thumb") or meta.get("parent_thumb") or ""
                if thumb:
                    break
            disk_path = None
            paths = []
            if identity and thumb:
                cache_key = f"plex::{identity.server_config_id}::{thumb}"
                http_source = self._plex_thumb_http_source(thumb)
            if http_source is None:
                return False
        requested_at = time.perf_counter()

        def apply_result(result):
            apply_started = time.perf_counter()
            current = self.album_item_by_key.get(album_key)
            stale = (
                result.generation != self._library_search_generation
                or current is not album_item
            )
            if not stale and result.image is not None and not result.image.isNull():
                # QPixmap/QIcon creation and widget assignment intentionally stay here.
                album_item.setIcon(0, QtGui.QIcon(QtGui.QPixmap.fromImage(result.image)))
            self.diagnostics.record(
                "artwork",
                "artwork_gui_application",
                duration_ms=(time.perf_counter() - apply_started) * 1000.0,
                details={
                    "stale": stale,
                    "key": result.key,
                    "source": result.source,
                    "worker_thread": result.thread_name,
                    "request_elapsed_ms": (
                        time.perf_counter() - requested_at
                    ) * 1000.0,
                    **result.timings,
                },
                minimum_level="detailed",
            )

        return self.artwork_manager.request(
            cache_key,
            paths,
            disk_path,
            (40, 40),
            generation,
            apply_result,
            http_source=http_source,
        )

    def _start_scan(self, add_folder: Optional[str] = None):
        if self.scan_thread and self.scan_thread.isRunning():
            return
        self._cancel_metadata_backfill()
        self.btn_add.setEnabled(False)
        self.btn_rescan.setEnabled(False)
        self.now_playing.setText("Scanning library...")
        self.scan_bar.setVisible(True)
        self.scan_bar.setRange(0, 0)
        self._last_scan_ui_update = 0.0
        self._lyrics_index_scan_started = time.perf_counter()
        try:
            self.beat.setPaused(False)
        except Exception:
            pass
        self._show_scan_dialog("Adding folder..." if add_folder else "Scanning library...")
        self.scan_thread = LibraryScanThread(self, add_folder=add_folder)
        self.scan_thread.progress.connect(self._on_scan_progress)
        self.scan_thread.finished_scan.connect(self._on_scan_finished)
        self.scan_thread.canceled_scan.connect(self._on_scan_canceled)
        _scan_token = self._worker_registry.register(
            "scan_thread", cancel=self.scan_thread.cancel, thread=self.scan_thread, wait_ms=2000,
        )
        self.scan_thread.finished.connect(
            lambda w=self.scan_thread, t=_scan_token: self._on_simple_worker_finished("scan_thread", w, t)
        )
        self.scan_thread.start()

    def _on_scan_finished(self, meta_list: List[Dict[str, Any]], folders: List[Dict]):
        if getattr(self, "_closing", False):
            return
        meta_list = dedupe_meta_list_by_path(meta_list)
        self._on_scan_progress("Finishing", "Rebuilding library view", 0, 0)
        self.artwork_manager.invalidate_negative_cache()
        scan_stats = getattr(self.scan_thread, "scan_stats", {}) or {}
        self._log(
            "Incremental library scan: "
            + "; ".join(
                f"{key}={int(scan_stats.get(key, 0))}"
                for key in (
                    "enumerated", "metadata_reused", "metadata_read",
                    "lyrics_only_updated", "added", "modified", "removed",
                    "unavailable_folders",
                    "fingerprints_baselined",
                )
            )
        )
        lyrics_checked = sum(
            1 for meta in meta_list
            if isinstance(meta.get("has_lrc_sidecar"), bool)
        )
        sidecars_found = sum(
            1 for meta in meta_list if meta.get("has_lrc_sidecar") is True
        )
        embedded_found = sum(
            1 for meta in meta_list
            if meta.get("has_embedded_synced_lyrics") is True
        )
        self._log(
            f"Lyrics availability indexed: tracks_checked={lyrics_checked}; "
            f"sidecars_found={sidecars_found}; embedded_found={embedded_found}; "
            f"duration_ms={(time.perf_counter() - getattr(self, '_lyrics_index_scan_started', time.perf_counter())) * 1000.0:.1f}"
        )
        # Local-scan-only data (lyrics-availability indexing never runs
        # against Plex content) -- write the Local backing store
        # directly, not through the ambient _full_meta_list setter, which
        # would misfile this under Plex if the user happens to be
        # viewing the Plex tab while this background indexing finishes.
        self._local_full_meta_list_backing = meta_list
        # The tree/search-index rebuild below reads self._full_meta_list
        # ambiently -- only do it while Local is actually the visible
        # source, so a scan that finishes while viewing Plex updates the
        # Local backing store (above) without touching the Plex tree/
        # search index. Local's own view gets rebuilt from the fresh
        # backing store automatically the next time it's selected (see
        # _on_library_source_changed).
        if self.library_source == "local":
            active_query = self.search_box.text() if hasattr(self, "search_box") else ""
            if active_query:
                self._rebuild_library_search_index()
                self._search_pending_text = active_query
                self._apply_search_pending()
            else:
                # _apply_meta_list_to_library_tabs rebuilds the search index
                # itself, tied to the generations it's about to build with.
                self._apply_meta_list_to_library_tabs(
                    self._full_meta_list,
                    reason="folder_rescan",
                    trigger_source="scan_complete",
                )
        self._save_cache(folders, meta_list)
        self.btn_add.setEnabled(True)
        self.btn_rescan.setEnabled(True)
        self.scan_bar.setRange(0, 100)
        self.scan_bar.setValue(0)
        self.scan_bar.setVisible(False)
        self._close_scan_dialog()
        if self.current_path and self.current_path in self.track_index_by_path:
            self.current_index = self.track_index_by_path[self.current_path]
            self._select_tree_item(self.current_path)
            # A Local rescan can complete while a Plex track is current
            # (nothing prevents starting one mid-playback) -- consult the
            # same shared, source-aware lookup instead of always
            # re-deriving the label from the path alone.
            meta = self._display_meta_for_path(self.current_path)
            self.now_playing.setText(
                meta.get("title") or os.path.splitext(os.path.basename(self.current_path))[0]
            )
        elif self.current_index is None:
            self.now_playing.setText("Ready")
        else:
            if 0 <= self.current_index < len(self.tracks):
                self.now_playing.setText(os.path.splitext(os.path.basename(self.tracks[self.current_index]))[0])

    def _show_scan_dialog(self, title: str):
        if self.scan_dialog is not None:
            return
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Library Scan")
        dlg.setModal(False)
        dlg.setMinimumSize(520, 236)
        layout = QtWidgets.QVBoxLayout(dlg)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        self.scan_phase_label = QtWidgets.QLabel(title)
        self.scan_phase_label.setStyleSheet("font-weight:700; color:#ffffff;")
        layout.addWidget(self.scan_phase_label)
        self.scan_detail_label = QtWidgets.QLabel("Starting...")
        self.scan_detail_label.setWordWrap(True)
        self.scan_detail_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.scan_detail_label)
        self.scan_progress_bar = QtWidgets.QProgressBar()
        self.scan_progress_bar.setRange(0, 0)
        layout.addWidget(self.scan_progress_bar)
        note = QtWidgets.QLabel("You can keep using the player while this runs.")
        note.setStyleSheet("color:#9a8cc8; font-size:9pt;")
        layout.addWidget(note)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self._cancel_scan)
        layout.addWidget(buttons)
        dlg.rejected.connect(self._cancel_scan)
        self.scan_dialog = dlg
        dlg.show()

    def _cancel_scan(self):
        if self._closing_scan_dialog:
            return
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.cancel()
        self._reset_scan_ui("Scan cancelled")

    def _on_scan_canceled(self):
        if getattr(self, "_closing", False):
            return
        self._reset_scan_ui("Scan cancelled")

    def _reset_scan_ui(self, message: str = ""):
        self.btn_add.setEnabled(True)
        self.btn_rescan.setEnabled(True)
        self.scan_bar.setRange(0, 100)
        self.scan_bar.setValue(0)
        self.scan_bar.setVisible(False)
        self._close_scan_dialog()
        if message:
            title = (
                self._display_meta_for_path(self.current_path).get("title")
                or os.path.splitext(os.path.basename(self.current_path))[0]
            ) if self.current_path else message
            self.now_playing.setText(title)

    def _on_scan_progress(self, phase: str, detail: str, current: int, total: int):
        if getattr(self, "_closing", False):
            return
        now = time.time()
        must_update = phase in ("Finishing", "Done") or (total and current >= total)
        if not must_update and now - self._last_scan_ui_update < 0.12:
            return
        self._last_scan_ui_update = now
        if self.scan_phase_label:
            self.scan_phase_label.setText(phase or "Scanning")
        if self.scan_detail_label:
            text = detail or ""
            if current and total:
                text = f"{current}/{total}: {text}"
            elif current:
                text = f"{current} tracks found\n{text}"
            self.scan_detail_label.setText(text)
        if self.scan_progress_bar:
            if total and total > 0:
                self.scan_progress_bar.setRange(0, total)
                self.scan_progress_bar.setValue(max(0, min(current, total)))
            else:
                self.scan_progress_bar.setRange(0, 0)

    def _close_scan_dialog(self):
        if self.scan_dialog:
            self._closing_scan_dialog = True
            self.scan_dialog.close()
            self.scan_dialog.deleteLater()
            self._closing_scan_dialog = False
        self.scan_dialog = None
        self.scan_phase_label = None
        self.scan_detail_label = None
        self.scan_progress_bar = None

    def _maybe_start_metadata_backfill(self):
        """Quietly, once, force a full re-read of every already-cached
        track so new per-track fields (genre/year/bpm/key) get backfilled.
        Runs on its own thread with no dialog, no disabled buttons, and no
        interference with playback -- unlike a manual Rescan Library."""
        if getattr(self, "_closing", False):
            return
        try:
            cfg = load_config()
        except Exception:
            cfg = {}
        if int(cfg.get("library_metadata_backfill_version", 0)) >= LIBRARY_METADATA_BACKFILL_VERSION:
            return
        if self.scan_thread and self.scan_thread.isRunning():
            return
        if self._backfill_scan_thread and self._backfill_scan_thread.isRunning():
            return
        cache = self._load_cache() or {}
        cached_meta = cache.get("meta", []) if isinstance(cache, dict) else []
        if not cached_meta:
            return
        self._backfill_scan_total = len(cached_meta) or 1
        self._backfill_scan_seen = 0
        self._backfill_scan_percent = None
        self._backfill_last_ui_update = 0.0
        self._backfill_scan_cancelled_by_user_action = False
        thread = LibraryScanThread(self, add_folder=None, force_full_reread=True)
        thread.progress.connect(self._on_backfill_progress)
        thread.finished_scan.connect(self._on_backfill_finished)
        thread.canceled_scan.connect(self._on_backfill_canceled)
        self._backfill_scan_thread = thread
        # No cancel= callback here: _cancel_metadata_backfill() is the
        # one existing, semantically-correct cancellation entry point
        # (it also sets _backfill_scan_cancelled_by_user_action, which
        # _on_backfill_canceled/_on_backfill_finished depend on) -- called
        # explicitly by _request_shutdown() before shutdown_all() runs, so
        # the registry only owns the wait/finalize half for this worker,
        # never a second, competing cancel path (see Phase C2 design,
        # section 12: one shutdown ownership mechanism per worker).
        _backfill_token = self._worker_registry.register(
            "backfill_scan", thread=thread, wait_ms=2000,
        )
        thread.finished.connect(
            lambda w=thread, t=_backfill_token: self._on_simple_worker_finished("_backfill_scan_thread", w, t)
        )
        thread.start()

    def _cancel_metadata_backfill(self):
        """Any user-triggered scan or destructive library action must win
        over the quiet background backfill; call this before proceeding."""
        if self._backfill_scan_thread and self._backfill_scan_thread.isRunning():
            self._backfill_scan_cancelled_by_user_action = True
            self._backfill_scan_thread.cancel()

    def _on_backfill_progress(self, phase: str, detail: str, current: int, total: int):
        if getattr(self, "_closing", False):
            return
        if phase in ("Reading changed tags", "Checking unchanged tracks"):
            self._backfill_scan_seen += 1
        percent = min(99, int((self._backfill_scan_seen / self._backfill_scan_total) * 100))
        if percent == self._backfill_scan_percent:
            return
        now = time.time()
        if percent < 99 and now - self._backfill_last_ui_update < 0.25:
            return
        self._backfill_last_ui_update = now
        self._backfill_scan_percent = percent
        self._refresh_dj_info_display()

    def _on_backfill_finished(self, meta_list: List[Dict[str, Any]], folders: List[Dict]):
        self._backfill_scan_thread = None
        if getattr(self, "_closing", False):
            return
        self._backfill_scan_percent = None
        self._refresh_dj_info_display()
        if self._backfill_scan_cancelled_by_user_action:
            self._backfill_scan_cancelled_by_user_action = False
            return
        meta_list = dedupe_meta_list_by_path(meta_list)
        # Local-scan-only data (metadata backfill never runs against Plex
        # content) -- see _on_scan_finished's identical reasoning.
        self._local_full_meta_list_backing = meta_list
        if self.library_source == "local":
            active_query = self.search_box.text() if hasattr(self, "search_box") else ""
            if not active_query:
                # _apply_meta_list_to_library_tabs rebuilds the search index
                # itself, tied to the generations it's about to build with.
                self._apply_meta_list_to_library_tabs(
                    self._full_meta_list,
                    reason="metadata_backfill",
                    trigger_source="quiet_backfill",
                )
            else:
                self._rebuild_library_search_index()
        self._save_cache(folders, meta_list)
        self._mark_backfill_version_complete()

    def _on_backfill_canceled(self):
        self._backfill_scan_cancelled_by_user_action = False
        self._backfill_scan_thread = None
        if getattr(self, "_closing", False):
            return
        self._backfill_scan_percent = None
        self._refresh_dj_info_display()
        # Deliberately no _save_cache here -- a cancelled backfill must not
        # persist partial state, and the version flag stays unmarked so it
        # retries on the next launch.

    def _mark_backfill_version_complete(self):
        try:
            cfg = load_config() or {}
            cfg["library_metadata_backfill_version"] = LIBRARY_METADATA_BACKFILL_VERSION
            with open(config_file_path(), "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass

    def _read_album_title_track(self, path: str) -> Tuple[str, str, str, str, int, int]:
        try:
            audio = MutagenFile(path, easy=True)
        except Exception:
            audio = None
        album = "Unknown Album"
        title = os.path.basename(path)
        artist = "Unknown Artist"
        album_artist = "Unknown Artist"
        disc_no = 1
        track_no = 0
        if audio:
            album_val = self._tag_or(audio, "album")
            title_val = self._tag_or(audio, "title")
            artist_val = self._tag_or(audio, "artist")
            album_artist_val = self._tag_or(audio, "albumartist")
            disc_val = self._tag_or(audio, "discnumber")
            track_val = self._tag_or(audio, "tracknumber")
            if album_val and album_val != "Unknown":
                album = album_val
            if title_val and title_val != "Unknown":
                title = title_val
            if artist_val and artist_val != "Unknown":
                artist = artist_val
            if album_artist_val and album_artist_val != "Unknown":
                album_artist = album_artist_val
            disc_no = self._parse_track_number(disc_val) or 1
            track_no = self._parse_track_number(track_val)
        return album, title, artist, album_artist, disc_no, track_no

    def _parse_track_number(self, value: str) -> int:
        if not value or value == "Unknown":
            return 0
        try:
            part = str(value).split("/")[0].strip()
            return int(part)
        except Exception:
            return 0

    def _tag_or(self, audio, key: str) -> str:
        val = audio.get(key)
        if not val:
            return "Unknown"
        if isinstance(val, list):
            return clean_text(val[0]) if val else "Unknown"
        return clean_text(str(val))

    def _tag_list(self, audio, key: str) -> str:
        val = audio.get(key)
        if not val:
            return "Unknown"
        if isinstance(val, list):
            parts = [clean_text(v).strip() for v in val if clean_text(v).strip()]
            return ", ".join(parts) if parts else "Unknown"
        text = clean_text(val).strip()
        return text if text else "Unknown"

    def _format_duration(self, seconds: float) -> str:
        total = int(seconds)
        m, s = divmod(total, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    def _select_tree_item(self, path: str):
        item = self.tree_item_by_path.get(path)
        if not item:
            album_key = self.album_key_by_path.get(path)
            album_item = self.album_item_by_key.get(album_key)
            if album_item:
                self._populate_album_item(album_item, immediate=True)
                album_item.setExpanded(True)
                item = self.tree_item_by_path.get(path)
        if not item:
            return
        self.tree_tracks.setCurrentItem(item)
        self.tree_tracks.scrollToItem(item, QtWidgets.QAbstractItemView.ScrollHint.PositionAtCenter)

    def _add_library_admin_menu(self, menu, track_path=None):
        """Shared admin section appended to every library context menu."""
        menu.addSeparator()
        if track_path:
            act_info = menu.addAction("Show track info")
        else:
            act_info = None
        font_menu = menu.addMenu("Album List Font")
        font_actions = {}
        font_group = QtGui.QActionGroup(font_menu)
        font_group.setExclusive(True)
        current_font = getattr(self, "library_font_family", "Segoe UI")
        available_fonts = sorted(
            set(QtGui.QFontDatabase.families()),
            key=str.casefold,
        )
        if "Segoe UI" not in available_fonts:
            available_fonts.insert(0, "Segoe UI")
        for family in available_fonts:
            act = font_menu.addAction(family)
            act.setCheckable(True)
            act.setChecked(family == current_font)
            act.setFont(QtGui.QFont(family))
            font_group.addAction(act)
            font_actions[act] = family
        lib = menu.addMenu("Library")
        act_add = lib.addAction("Add Folder...")
        act_remove_folder = lib.addAction("Remove Folder...")
        act_clean_cache = lib.addAction("Clean Non-Audio Entries")
        act_delete_library = lib.addAction("Delete Library")
        act_rescan = lib.addAction("Rescan Library")
        if self.library_source == "plex":
            # These 5 all read/write the Local on-disk scan cache and the
            # Local backing store directly -- disabled (not omitted, to
            # avoid every "action == None" comparison below spuriously
            # matching a dismissed menu) while viewing Plex, so they can
            # never be triggered against the wrong source's data. See
            # the real-device regression this guards against: Local's
            # ~47k records being lost after a source switch.
            for local_only_action in (
                act_add, act_remove_folder, act_clean_cache,
                act_delete_library, act_rescan,
            ):
                local_only_action.setEnabled(False)
                local_only_action.setToolTip("Switch Source to Local first.")
        act_normalisation_preferences = lib.addAction(
            "Playback and Diagnostics Preferences..."
        )
        act_lyrics_enabled = lib.addAction("Show Lyrics")
        act_lyrics_enabled.setCheckable(True)
        act_lyrics_enabled.setChecked(bool(getattr(self, "lyrics_enabled", True)))
        lyrics = lib.addMenu("Lyrics Timing")
        act_lyric_earlier = lyrics.addAction("Show Lyrics Earlier")
        act_lyric_later = lyrics.addAction("Show Lyrics Later")
        act_lyric_reset = lyrics.addAction("Reset Lyrics Timing")
        act_lyric_status = lyrics.addAction(f"Current offset: {self.lyric_time_offset_ms / 1000.0:+.2f}s")
        act_lyric_status.setEnabled(False)
        visualiser = lib.addMenu("Visualiser Timing")
        act_visual_delay = visualiser.addAction("Delay Visualiser")
        act_visual_advance = visualiser.addAction("Advance Visualiser")
        act_visual_reset = visualiser.addAction("Reset Visualiser Timing")
        act_visual_status = visualiser.addAction(f"Current offset: {self.analyzer_time_offset_ms / 1000.0:+.2f}s")
        act_visual_status.setEnabled(False)
        lyric_styles = lib.addMenu("Lyrics Style")
        lyric_style_actions = {}
        for key, label in LYRIC_STYLES.items():
            act = lyric_styles.addAction(label)
            act.setCheckable(True)
            act.setChecked(key == getattr(self, "lyric_visual_style", "neon"))
            lyric_style_actions[act] = key
        bio_modes = lib.addMenu("Bio Detail")
        bio_mode_actions = {}
        for key, label in (("concise", "Concise"), ("detailed", "Detailed"), ("facts", "Music Facts Only")):
            act = bio_modes.addAction(label)
            act.setCheckable(True)
            act.setChecked(key == getattr(self, "bio_detail_mode", "detailed"))
            bio_mode_actions[act] = key
        recent_menu = lib.addMenu("Recently Played")
        recent_actions = {}
        # No os.path.isfile() pre-filter here -- each check is a blocking
        # network round-trip for any entry on a network share, and with a
        # slow/sleeping share this froze the whole GUI thread for several
        # seconds just to open the context menu (stall_traceback.log,
        # 2026-08-05). A stale entry just no-ops gracefully when clicked --
        # play_path()/_play_path_direct() already skip missing files.
        recent_paths = [p for p in getattr(self, "recent_played", []) if isinstance(p, str) and p]
        if recent_paths:
            for path in recent_paths[:10]:
                label = (
                    self._display_meta_for_path(path).get("title")
                    or os.path.splitext(os.path.basename(path))[0]
                )
                act = recent_menu.addAction(label[:70])
                recent_actions[act] = path
        else:
            act_recent_empty = recent_menu.addAction("No recent tracks yet")
            act_recent_empty.setEnabled(False)

        act_simple = lib.addAction("Use miniaudio player")
        act_simple.setCheckable(True)
        act_simple.setChecked(bool(self.use_simple and self.builtin_backend == "miniaudio"))
        act_bass = lib.addAction("Use BASS player")
        act_bass.setCheckable(True)
        act_bass.setChecked(bool(self.use_simple and self.builtin_backend == "bass"))
        if self.bass_player is None:
            act_bass.setEnabled(False)
            act_bass.setToolTip(f"BASS unavailable: {BASS_IMPORT_ERROR}")
        return (
            act_info, font_actions, act_add, act_remove_folder, act_clean_cache, act_delete_library, act_rescan, act_normalisation_preferences,
            act_lyrics_enabled, act_lyric_earlier, act_lyric_later, act_lyric_reset,
            act_visual_delay, act_visual_advance, act_visual_reset,
            lyric_style_actions, bio_mode_actions, recent_actions, act_simple, act_bass,
        )

    def _handle_library_admin(self, action, refs, track_path=None):
        (
            act_info, font_actions, act_add, act_remove_folder, act_clean_cache, act_delete_library, act_rescan, act_normalisation_preferences,
            act_lyrics_enabled, act_lyric_earlier, act_lyric_later, act_lyric_reset,
            act_visual_delay, act_visual_advance, act_visual_reset,
            lyric_style_actions, bio_mode_actions, recent_actions, act_simple, act_bass,
        ) = refs
        if act_info is not None and action == act_info:
            self._show_track_info(track_path)
            return True
        if action in font_actions:
            self._apply_library_font(font_actions[action])
            self._save_user_settings()
            return True
        if action == act_add:
            self.add_folder()
            return True
        if action == act_remove_folder:
            self._remove_library_folder_dialog()
            return True
        if action == act_clean_cache:
            self._clean_non_audio_cache_entries()
            return True
        if action == act_delete_library:
            self._delete_library_cache()
            return True
        if action == act_rescan:
            self.rescan_library()
            return True
        if action == act_normalisation_preferences:
            self._show_normalisation_preferences()
            return True
        if action == act_lyrics_enabled:
            self._set_lyrics_enabled(act_lyrics_enabled.isChecked())
            return True
        if action == act_lyric_earlier:
            self._adjust_lyric_offset(250)
            return True
        if action == act_lyric_later:
            self._adjust_lyric_offset(-250)
            return True
        if action == act_lyric_reset:
            self._set_lyric_offset(0)
            return True
        if action == act_visual_delay:
            self._adjust_visualiser_offset(-100)
            return True
        if action == act_visual_advance:
            self._adjust_visualiser_offset(100)
            return True
        if action == act_visual_reset:
            self._set_visualiser_offset(0)
            return True
        if action in lyric_style_actions:
            self._set_lyric_visual_style(lyric_style_actions[action])
            return True
        if action in bio_mode_actions:
            self._set_bio_detail_mode(bio_mode_actions[action])
            return True
        if action in recent_actions:
            self.play_path(recent_actions[action], crossfade=False)
            return True

        if action == act_simple or action == act_bass:
            requested_backend = "bass" if action == act_bass else "miniaudio"
            checked = action.isChecked()
            self._stop_all()
            if checked:
                if not self._set_builtin_backend(requested_backend):
                    QtWidgets.QMessageBox.warning(self, "Audio Backend", f"{requested_backend} is not available.")
                    return True
                self.use_simple = True
            else:
                self.use_simple = False
            self._simple_fallback_active = False
            try:
                self.chk_simple.blockSignals(True)
                self.chk_simple.setChecked(self.use_simple and self.builtin_backend == "miniaudio")
            except Exception:
                pass
            finally:
                try:
                    self.chk_simple.blockSignals(False)
                except Exception:
                    pass
            self._audio_log(f"backend={self._backend_label().lower()} enabled={self.use_simple}; source=menu")
            self._update_dj_info(path=self.current_path)
            self._save_user_settings()
            # Restart the current track on the selected backend so playback, volume and visualiser clocks stay in sync.
            if self.current_path:
                self.play_path(self.current_path, crossfade=False)
                self.beat.setPlaying(True)
                self.btn_pause.setText("Pause")
            return True
        return False

    def _apply_library_font(self, family: str):
        family = str(family or "Segoe UI")
        self.library_font_family = family
        escaped_family = family.replace("\\", "\\\\").replace('"', '\\"')
        self.tree_tracks.setStyleSheet(
            self._tree_tracks_base_stylesheet
            + f'QTreeWidget {{ font-family:"{escaped_family}"; }}'
        )

    def _set_bio_detail_mode(self, mode: str):
        if mode not in ("concise", "detailed", "facts"):
            mode = "detailed"
        self.bio_detail_mode = mode
        self._save_user_settings()

    def _set_lyric_visual_style(self, style: str):
        if style not in LYRIC_STYLES:
            style = "neon"
        self.lyric_visual_style = style
        try:
            self.overlay.set_lyric_style(style)
        except Exception:
            pass
        self._save_user_settings()

    def _set_lyrics_enabled(self, enabled: bool):
        self.lyrics_enabled = bool(enabled)
        if hasattr(self, "action_toggle_lyrics"):
            self.action_toggle_lyrics.setChecked(self.lyrics_enabled)
        self._lyric_idx = None
        if not self.lyrics_enabled:
            try:
                self.overlay.clear_lyric()
            except Exception:
                pass
        self._save_user_settings()
        if self.lyrics_enabled:
            self._lyrics_tick()

    def _delete_library_cache(self):
        if self.scan_thread and self.scan_thread.isRunning():
            QtWidgets.QMessageBox.information(
                self,
                "Delete Library",
                "Please wait for the current library scan to finish before deleting the library.",
            )
            return
        self._cancel_metadata_backfill()
        confirm = QtWidgets.QMessageBox.question(
            self,
            "Delete Library",
            "Delete the saved Bills Music library?\n\nThis clears the app's library list and cache only. Your music files will not be deleted.",
        )
        if confirm != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            self._build_timer.stop()
            self._cover_timer.stop()
            self._populate_timer.stop()
        except Exception:
            pass
        self._meta_list = []
        # Local-only action (see _show_tree_menu's empty-space branch,
        # gated to only offer this while Local is the active source).
        self._local_full_meta_list_backing = []
        self._rebuild_library_search_index()
        self._showing_full = False
        self.tracks = []
        self.current_index = None
        self.track_index_by_path = {}
        self.tree_item_by_path = {}
        self.album_item_by_key = {}
        self.album_key_by_path = {}
        self.album_cover_cache = {}
        self._build_queue = []
        self._build_playlist = []
        self._cover_queue = []
        self.tree_tracks.clear()
        self.queue = []
        self.queue_played = []
        self.queue_playlist_entries = []
        self._schedule_session_save()
        self.queue_detail_cache.clear()
        self.queue_analysis_pending.clear()
        if hasattr(self, "queue_spinner_timer"):
            self.queue_spinner_timer.stop()
        for entries in getattr(self, "_queue_row_widgets", {}).values():
            for entry in entries:
                self._shutdown_queue_row_marquee(entry)
        self.queue_list.clear()
        self._queue_row_widgets = {}
        try:
            os.remove(cache_file_path())
        except FileNotFoundError:
            pass
        except Exception:
            self._save_cache([], [])
        if not self.current_path:
            self.now_playing.setText("Ready")
        QtWidgets.QMessageBox.information(self, "Delete Library", "Library cache deleted. Add a folder or rescan to rebuild it.")

    def _adjust_lyric_offset(self, delta_ms: int):
        self._set_lyric_offset(self.lyric_time_offset_ms + int(delta_ms))

    def _set_lyric_offset(self, offset_ms: int):
        self.lyric_time_offset_ms = max(-5000, min(5000, int(offset_ms)))
        self._save_user_settings()
        self._lyric_idx = None
        try:
            self.overlay.clear_lyric()
        except Exception:
            pass
        self._lyrics_tick()

    def _adjust_visualiser_offset(self, delta_ms: int):
        self._set_visualiser_offset(self.analyzer_time_offset_ms + int(delta_ms))

    def _set_visualiser_offset(self, offset_ms: int):
        # Negative values delay the visualiser when an output buffer is slightly behind.
        self.analyzer_time_offset_ms = max(-2000, min(2000, int(offset_ms)))
        self._reset_analyzer_clock()
        self._save_user_settings()

    def _remove_library_folder_dialog(self):
        self._cancel_metadata_backfill()
        cache = self._load_cache() or {}
        folders = [f.get("path") for f in cache.get("folders", []) if isinstance(f, dict) and f.get("path")]
        if not folders:
            QtWidgets.QMessageBox.information(self, "Remove Folder", "There are no library folders to remove.")
            return

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Remove Library Folder")
        dlg.setMinimumWidth(640)
        layout = QtWidgets.QVBoxLayout(dlg)
        label = QtWidgets.QLabel("Choose a folder to remove from the library. Files on disk will not be deleted.")
        label.setWordWrap(True)
        layout.addWidget(label)
        lst = QtWidgets.QListWidget()
        for folder in folders:
            item = QtWidgets.QListWidgetItem(folder)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, folder)
            lst.addItem(item)
        if lst.count():
            lst.setCurrentRow(0)
        layout.addWidget(lst)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        remove_btn = buttons.addButton("Remove Folder", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        layout.addWidget(buttons)
        buttons.rejected.connect(dlg.reject)
        remove_btn.clicked.connect(dlg.accept)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        item = lst.currentItem()
        if not item:
            return
        folder = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if not folder:
            return
        confirm = QtWidgets.QMessageBox.question(
            self,
            "Remove Folder",
            f"Remove this folder from the library?\n\n{folder}\n\nNo audio files will be deleted.",
        )
        if confirm != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._remove_library_folder(folder)

    def _remove_library_folder(self, folder: str):
        cache = self._load_cache() or {}
        folders = cache.get("folders", []) if isinstance(cache, dict) else []
        meta = cache.get("meta", []) if isinstance(cache, dict) else []
        folder_norm = os.path.normcase(os.path.normpath(folder))

        def under_folder(path: str) -> bool:
            try:
                p = os.path.normcase(os.path.normpath(path))
                return p == folder_norm or p.startswith(folder_norm + os.sep)
            except Exception:
                return False

        kept_folders = [
            f for f in folders
            if not (isinstance(f, dict) and under_folder(f.get("path", "")))
        ]
        kept_meta = [
            m for m in meta
            if not (isinstance(m, dict) and under_folder(m.get("path", "")))
        ]
        # Local-only (derived from the on-disk Local scan cache) -- see
        # _delete_library_cache's identical reasoning.
        self._local_full_meta_list_backing = kept_meta
        self._rebuild_library_search_index()
        self._refresh_library_view_after_change()
        self._save_cache(kept_folders, kept_meta)
        if self.current_path and under_folder(self.current_path):
            self.current_path = None
            self.current_index = None
            self._stop_all()
            self.now_playing.setText("Ready")
            try:
                self.overlay.clear()
            except Exception:
                pass
        QtWidgets.QMessageBox.information(self, "Remove Folder", "Folder removed from the library.")

    def _clean_non_audio_cache_entries(self):
        cache = self._load_cache() or {}
        folders = cache.get("folders", []) if isinstance(cache, dict) else []
        meta = cache.get("meta", []) if isinstance(cache, dict) else []

        def is_audio_path(path: str) -> bool:
            return is_library_scannable(path)

        cleaned_meta = [m for m in meta if isinstance(m, dict) and is_audio_path(m.get("path", ""))]
        removed = len(meta) - len(cleaned_meta)
        cleaned_folders = []
        for folder in folders:
            if not isinstance(folder, dict):
                continue
            tracks = [p for p in folder.get("tracks", []) if is_audio_path(p)]
            cleaned = dict(folder)
            cleaned["tracks"] = tracks
            fingerprints = cleaned.get("file_fingerprints")
            if isinstance(fingerprints, dict):
                cleaned["file_fingerprints"] = {
                    path: fingerprint
                    for path, fingerprint in fingerprints.items()
                    if is_audio_path(path)
                }
            cleaned_folders.append(cleaned)
        # Local-only (is_library_scannable is a Local capability check).
        self._local_full_meta_list_backing = cleaned_meta
        self._rebuild_library_search_index()
        self._refresh_library_view_after_change()
        self._save_cache(cleaned_folders, cleaned_meta)
        QtWidgets.QMessageBox.information(
            self,
            "Clean Non-Audio Entries",
            f"Removed {removed} cached non-audio entr{'y' if removed == 1 else 'ies'} from the library.",
        )

    def _remove_library_paths(self, paths: List[str], label: str):
        paths = [p for p in paths if p]
        if not paths:
            return
        path_set = {os.path.normcase(os.path.normpath(p)) for p in paths}
        cache = self._load_cache() or {}
        folders = cache.get("folders", []) if isinstance(cache, dict) else []
        meta = cache.get("meta", []) if isinstance(cache, dict) else []

        def remove_path(path: str) -> bool:
            try:
                return os.path.normcase(os.path.normpath(path)) in path_set
            except Exception:
                return False

        kept_meta = [m for m in meta if not (isinstance(m, dict) and remove_path(m.get("path", "")))]
        kept_folders = []
        for folder in folders:
            if not isinstance(folder, dict):
                continue
            item = dict(folder)
            item["tracks"] = [p for p in item.get("tracks", []) if not remove_path(p)]
            fingerprints = item.get("file_fingerprints")
            if isinstance(fingerprints, dict):
                item["file_fingerprints"] = {
                    path: fingerprint
                    for path, fingerprint in fingerprints.items()
                    if not remove_path(path)
                }
            kept_folders.append(item)
        # Local-only (derived from the on-disk Local scan cache; Plex
        # tree items never reach this function -- see _show_tree_menu's
        # Plex-aware track/album branches, which omit "Remove From
        # Library" entirely for Plex-sourced rows).
        self._local_full_meta_list_backing = kept_meta
        self._rebuild_library_search_index()
        self._refresh_library_view_after_change()
        self._save_cache(kept_folders, kept_meta)
        if self.current_path and remove_path(self.current_path):
            self.current_path = None
            self.current_index = None
            self._stop_all()
            self.now_playing.setText("Ready")
        QtWidgets.QMessageBox.information(self, "Remove From Library", f"Removed {label} from the library cache.")

    def _album_paths_from_data(self, data: Dict[str, Any]) -> List[str]:
        cached_items = data.get("items")
        if isinstance(cached_items, list):
            return [
                item[5] for item in cached_items
                if isinstance(item, tuple) and len(item) > 5 and item[5]
            ]
        album = data.get("album", "")
        artist = data.get("artist", "")
        paths = []
        for meta in self._meta_list:
            if meta.get("album") != album:
                continue
            group_artist = meta.get("album_artist") or meta.get("artist", "")
            if artist and group_artist != artist and meta.get("artist") != artist:
                continue
            path = meta.get("path")
            if path:
                paths.append(path)
        return paths

    def _artist_paths_from_data(self, data: Dict[str, Any]) -> List[str]:
        artist = data.get("artist", "")
        paths = []
        for meta in self._meta_list:
            group_artist = meta.get("album_artist") or meta.get("artist", "")
            if group_artist != artist and meta.get("artist") != artist:
                continue
            path = meta.get("path")
            if path:
                paths.append(path)
        return paths

    def _handle_album_group_queue_action(
        self, action, data, album_paths,
        action_play_album, action_play_next, action_add_queue,
        action_play_artist_next, action_add_artist_queue,
    ) -> bool:
        """The 5 queue-management actions shared by both the Plex-sourced
        and Local album/artist context menus -- shared here so the two
        menu branches in _show_tree_menu don't duplicate this logic (and
        can't drift apart). Returns True if `action` was one of these 5,
        so callers know whether to keep checking their own local-only
        actions."""
        if action == action_play_album and album_paths:
            self.play_path(album_paths[0], crossfade=False)
        elif action == action_play_next and album_paths:
            for path in reversed(album_paths):
                self._insert_unplayed_queue_item(path, play_next=True)
            self._refresh_queue_list(cached_details_only=True, reason="tracks_added")
            self._request_queue_analysis_for_paths(album_paths)
            self._schedule_session_save()
        elif action == action_add_queue and album_paths:
            self._add_to_queue_with_dedup_guard(album_paths)
        elif action == action_play_artist_next:
            artist_paths = self._artist_paths_from_data(data)
            for path in reversed(artist_paths):
                self._insert_unplayed_queue_item(path, play_next=True)
            if artist_paths:
                self._refresh_queue_list(cached_details_only=True, reason="tracks_added")
                self._request_queue_analysis_for_paths(artist_paths)
                self._schedule_session_save()
        elif action == action_add_artist_queue:
            self._add_to_queue_with_dedup_guard(self._artist_paths_from_data(data))
        else:
            return False
        return True

    def _show_tree_menu(self, pos):
        item = self.tree_tracks.itemAt(pos)
        gpos = self.tree_tracks.viewport().mapToGlobal(pos)

        # Right-click on empty space -> just the admin menu.
        if not item:
            menu = QtWidgets.QMenu(self)
            refs = self._add_library_admin_menu(menu, track_path=None)
            self._add_window_menu_options(menu)
            action = menu.exec(gpos)
            if action:
                self._handle_library_admin(action, refs)
            return

        data = item.data(0, QtCore.Qt.ItemDataRole.UserRole)

        # Track item
        if not isinstance(data, dict) or "album" not in data:
            path = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
            if isinstance(path, str):
                menu = QtWidgets.QMenu(self)
                selected_paths = []
                for selected in self.tree_tracks.selectedItems():
                    selected_data = selected.data(0, QtCore.Qt.ItemDataRole.UserRole)
                    if isinstance(selected_data, str):
                        selected_paths.append(selected_data)
                if path not in selected_paths:
                    selected_paths = [path]
                action_play_next = menu.addAction("Play Next")
                action_add_queue = menu.addAction("Add to Queue")
                if is_plex_identity(path):
                    # Stage 2: a Plex-sourced row only supports queue
                    # management here. Visualiser logging, normalisation/
                    # loudness analysis, "Remove From Library", and the
                    # admin menu all assume a local file (decode a real
                    # path, edit local tags, mutate the local scan cache)
                    # -- none of that is built for a plex:// identity yet,
                    # so those actions are simply not offered rather than
                    # silently failing or corrupting local/Plex state.
                    self._add_window_menu_options(menu)
                    action = menu.exec(gpos)
                    if action == action_play_next:
                        self._queue_play_next(path)
                    elif action == action_add_queue:
                        self._add_to_queue_with_dedup_guard([path])
                    return
                menu.addSeparator()
                action_log_viz = menu.addAction("Log visualiser data")
                normalisation_actions = self._add_normalisation_menu(menu, selected_paths)
                menu.addSeparator()
                action_remove_track = menu.addAction("Remove This Track From Library")
                refs = self._add_library_admin_menu(menu, track_path=path)
                self._add_window_menu_options(menu)
                action = menu.exec(gpos)
                if action == action_play_next:
                    self._queue_play_next(path)
                elif action == action_add_queue:
                    self._add_to_queue_with_dedup_guard([path])
                elif action == action_log_viz:
                    self._play_and_log(path)
                elif self._handle_normalisation_action(action, normalisation_actions, selected_paths):
                    pass
                elif action == action_remove_track:
                    self._remove_library_paths([path], "1 track")
                else:
                    self._handle_library_admin(action, refs, track_path=path)
            return

        # Album item
        menu = QtWidgets.QMenu(self)
        album_paths = self._album_paths_from_data(data)
        is_plex_album = any(is_plex_identity(p) for p in album_paths)
        action_play_album = menu.addAction("Play Album")
        action_play_next = menu.addAction("Play Album Next")
        action_add_queue = menu.addAction("Add Album to Up Next")
        action_play_artist_next = menu.addAction("Play Artist Next")
        action_add_artist_queue = menu.addAction("Add Artist's Tracks to Up Next")
        if is_plex_album:
            # Stage 2: same reasoning as the Plex track menu above --
            # tag refresh/removal/artwork actions all assume a local
            # file or the local scan cache; none of that exists for a
            # Plex-sourced album group yet, so they're left off the menu
            # instead of silently doing nothing or corrupting state.
            # Handled and returned separately (not folded into the
            # elif chain below) so a dismissed menu (action is None)
            # can never spuriously match an unset local-only action.
            self._add_window_menu_options(menu)
            action = menu.exec(gpos)
            self._handle_album_group_queue_action(
                action, data, album_paths,
                action_play_album, action_play_next, action_add_queue,
                action_play_artist_next, action_add_artist_queue,
            )
            return
        menu.addSeparator()
        action_refresh_album = menu.addAction("Refresh Album Tags")
        action_remove_album = menu.addAction("Remove This Album Group From Library")
        action_show_cover = menu.addAction("Show Album Cover")
        action_show_all = menu.addAction("Show Covers For All Albums")
        action_hide_all = menu.addAction("Hide Album Covers")
        action_fetch = menu.addAction("Fetch Album Art Online")
        refs = self._add_library_admin_menu(menu, track_path=None)
        self._add_window_menu_options(menu)
        action = menu.exec(gpos)
        if self._handle_album_group_queue_action(
            action, data, album_paths,
            action_play_album, action_play_next, action_add_queue,
            action_play_artist_next, action_add_artist_queue,
        ):
            pass
        elif action == action_refresh_album:
            self._refresh_album_tags(data)
        elif action == action_remove_album:
            paths = self._album_paths_from_data(data)
            if paths:
                confirm = QtWidgets.QMessageBox.question(
                    self,
                    "Remove From Library",
                    f"Remove {len(paths)} cached track(s) from this album group?\n\nNo audio files will be deleted.",
                )
                if confirm == QtWidgets.QMessageBox.StandardButton.Yes:
                    self._remove_library_paths(paths, f"{len(paths)} track(s)")
        elif action == action_show_cover:
            self._show_album_cover(item, data)
        elif action == action_show_all:
            self._show_all_album_covers()
        elif action == action_hide_all:
            self._hide_all_album_covers()
        elif action == action_fetch:
            self._fetch_album_art_online(item, data)
        else:
            self._handle_library_admin(action, refs)

    def _show_track_info(self, path=None):
        """Show file/tag info on demand as a floating jukebox-style card.

        v1.0.67 MainThread I/O hardening: this used to call _read_tags(path)
        (a Mutagen open) synchronously on the GUI thread for this one-off
        "Show Track Info" menu action -- same class of NAS-stall bug fixed
        elsewhere this round for the automatic playback path, just for a
        user-triggered action instead. Shows cached data immediately (no
        I/O), then refreshes via TrackTagLoadWorker once the authoritative
        read completes.
        """
        path = path or getattr(self, "current_path", None)
        if not path:
            return
        # Which path the overlay is supposed to be showing right now -- a
        # second "Show Track Info" request for a different track before
        # this one's background read completes must supersede it, not
        # let the earlier (now-stale) result overwrite the newer display.
        self._track_info_panel_path = path
        self._display_track_info(self._load_cached_audio_tags(path), path)
        if getattr(self, "_closing", False):
            return
        if is_plex_identity(path):
            # Same real-device bug/fix as _queue_track_tags_async -- see
            # its comment. TrackTagLoadWorker can never improve on the
            # cached display just shown above for a synthetic identity.
            self.diagnostics.record(
                "now_playing", "track_tags_async_skipped_for_plex",
                details={
                    **self.diagnostics.path_details(path),
                    "reason": "plex_identity_has_no_local_file_to_read",
                },
                minimum_level="detailed",
            )
            return
        worker = TrackTagLoadWorker(path)
        self._track_tag_load_workers.append(worker)
        token = self._worker_registry.register("track_info_tag_load", thread=worker, wait_ms=1500)

        def _on_ready(ready_path, fields):
            if getattr(self, "_closing", False) or ready_path != getattr(self, "_track_info_panel_path", None):
                return
            self._display_track_info(Track(path=ready_path, **fields), ready_path)

        def _on_finished(worker=worker, token=token):
            if worker in self._track_tag_load_workers:
                self._track_tag_load_workers.remove(worker)
            self._worker_registry.unregister(token)

        worker.tags_ready.connect(_on_ready)
        worker.finished.connect(_on_finished)
        worker.start()

    def _display_track_info(self, info: Track, path: str):
        lines = []
        if info.title not in ("Unknown", ""): lines.append(f"Title: {info.title}")
        if info.artist not in ("Unknown", ""): lines.append(f"Artist: {info.artist}")
        if info.album not in ("Unknown", ""): lines.append(f"Album: {info.album}")
        if info.genre not in ("Unknown", ""): lines.append(f"Genre: {info.genre}")
        if info.bitrate not in ("Unknown", ""): lines.append(f"Bitrate: {info.bitrate}")
        if info.sample_rate not in ("Unknown", ""): lines.append(f"Sample rate: {info.sample_rate}")
        if info.channels not in ("Unknown", ""): lines.append(f"Channels: {info.channels}")
        if info.duration not in ("Unknown", ""): lines.append(f"Duration: {info.duration}")
        lines.append(f"File path: {os.path.abspath(path)}")
        text = "\n".join(lines) if lines else "No track information available."
        if getattr(self, "overlay", None) is not None:
            self.overlay.show_info_panel("TRACK INFO", text)

    def _refresh_album_tags(self, data: Dict[str, Any]):
        """Re-reads tags for this album group and rescans its folder(s) for
        new files, on a background thread (AlbumTagRefreshWorker). Used to
        do this inline on the GUI thread -- a captured GUI-stall trace
        showed the whole window frozen inside mutagen's tag-reading code
        while handling this exact menu action, for as long as the (often
        network-share-backed) file I/O took."""
        album = data.get("album", "")
        if not album or getattr(self, "_closing", False):
            return
        self._cancel_metadata_backfill()
        if self._album_tag_refresh_thread and self._album_tag_refresh_thread.isRunning():
            return
        self.statusBar().showMessage(f"Refreshing tags for “{album}”…", 0)
        thread = AlbumTagRefreshWorker(album, self._full_meta_list)
        thread.finished_refresh.connect(self._on_album_tags_refreshed)
        self._album_tag_refresh_thread = thread
        registry_token = self._worker_registry.register(
            "album_tag_refresh", cancel=thread.cancel, thread=thread, wait_ms=2000,
        )
        thread.finished.connect(
            lambda t=registry_token: self._worker_registry.unregister(t)
        )
        thread.finished.connect(self._maybe_resume_final_shutdown)
        thread.start()

    def _on_album_tags_refreshed(self, refreshed: List[Dict[str, Any]]):
        self._album_tag_refresh_thread = None
        if getattr(self, "_closing", False):
            return
        # Local-only (AlbumTagRefreshWorker reads local files via Mutagen;
        # Plex albums never reach _refresh_album_tags -- see
        # _show_tree_menu's Plex-aware album branch, which omits
        # "Refresh Album Tags" entirely for Plex-sourced albums).
        self._local_full_meta_list_backing = refreshed
        self._rebuild_library_search_index()
        self._refresh_library_view_after_change()
        cache = self._load_cache()
        folders = cache.get("folders", []) if cache else []
        self._save_cache(folders, refreshed)
        self.statusBar().showMessage("Tags refreshed", 3000)

    def _show_album_cover(self, item, data: Dict[str, Any]):
        album = data.get("album", "")
        artist = data.get("artist", "")
        items = []
        if not album:
            return
        album_key = data.get("key") or f"{artist}::{album}"
        self.album_cover_allowlist.add(album_key)
        for meta in self._meta_list:
            if meta.get("album") == album:
                items.append(
                    (
                        meta.get("disc_no", 1),
                        meta.get("track_no", 0),
                        meta.get("title", ""),
                        meta.get("artist", ""),
                        meta.get("album_artist", ""),
                        meta.get("path", ""),
                    )
                )
        self._request_album_artwork(item, album, artist, items, album_key)

    def _show_all_album_covers(self):
        self.album_covers_enabled = True
        self._queue_visible_album_covers()

    def _queue_visible_album_covers(self, *_args):
        if not self.album_covers_enabled or not hasattr(self, "tree_tracks"):
            return
        if skipped_missing:
            self.stop_playback()
            return
        viewport = self.tree_tracks.viewport().rect().adjusted(0, -160, 0, 160)
        queued_keys = {entry[4] for entry in self._cover_queue}
        for album_key, item in list(self.album_item_by_key.items()):
            if album_key in queued_keys or not viewport.intersects(
                self.tree_tracks.visualItemRect(item)
            ):
                continue
            data = item.data(0, QtCore.Qt.ItemDataRole.UserRole) or {}
            album = data.get("album", "")
            artist = data.get("artist", "")
            items = []
            for meta in self._meta_list:
                if meta.get("album") == album:
                    items.append(
                        (
                            meta.get("disc_no", 1),
                            meta.get("track_no", 0),
                            meta.get("title", ""),
                            meta.get("artist", ""),
                            meta.get("album_artist", ""),
                            meta.get("path", ""),
                        )
                    )
            self._cover_queue.append((item, album, artist, items, album_key))
        if self._cover_queue:
            self._cover_timer.start(0)

    def _hide_all_album_covers(self):
        self.album_covers_enabled = False
        self.album_cover_allowlist.clear()
        for item in self.album_item_by_key.values():
            item.setIcon(0, QtGui.QIcon())

    def _fetch_album_art_online(self, item, data: Dict[str, Any]):
        """v1.0.67 MainThread I/O hardening: this used to make two
        synchronous HTTP requests (MusicBrainz release lookup, then a
        Cover Art Archive image fetch -- each up to a 10s timeout with
        internal retries, see net.py's http_get) plus a disk write, all
        directly on the GUI thread. A slow/unreachable network could
        freeze the whole window for tens of seconds. AlbumArtFetchWorker
        (workers.py) now does the network/disk work; this only dispatches
        and applies the result, guarded against closing and against the
        library tree having been rebuilt (item deleted) while in flight."""
        album = data.get("album", "")
        artist = data.get("artist", "")
        if not album:
            QtWidgets.QMessageBox.warning(self, "Album Art", "Album name is missing.")
            return
        if getattr(self, "_closing", False):
            return
        album_key = data.get("key") or f"{artist}::{album}"
        worker = AlbumArtFetchWorker(album_key, artist, album)
        self._album_art_fetch_workers.append(worker)
        token = self._worker_registry.register("album_art_fetch", thread=worker, wait_ms=15000)

        def _on_ready(ready_key, cover_bytes, error):
            self.diagnostics.record(
                "album_art", "fetch_completed",
                details={"found": not error},
                minimum_level="detailed",
            )
            if getattr(self, "_closing", False):
                return
            if error:
                if error == "No match found on MusicBrainz.":
                    QtWidgets.QMessageBox.information(self, "Album Art", error)
                else:
                    QtWidgets.QMessageBox.warning(self, "Album Art", f"Failed to fetch art: {error}")
                return
            self.album_cover_allowlist.add(ready_key)
            pix = QtGui.QPixmap()
            if pix.loadFromData(cover_bytes):
                icon = QtGui.QIcon(
                    pix.scaled(40, 40, QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation)
                )
                try:
                    item.setIcon(0, icon)
                except RuntimeError:
                    pass  # library tree was rebuilt while the fetch was in flight
            self.album_cover_cache[ready_key] = cover_bytes

        def _on_finished(worker=worker, token=token):
            if worker in self._album_art_fetch_workers:
                self._album_art_fetch_workers.remove(worker)
            self._worker_registry.unregister(token)

        worker.art_ready.connect(_on_ready)
        worker.finished.connect(_on_finished)
        worker.start()

    def _queue_add(self, path: str):
        row = self._insert_unplayed_queue_item(path, play_next=False)
        self._insert_queue_row_widget(row, reason="track_added")
        self._schedule_session_save()

    def _insert_queue_paths(self, paths):
        for path in paths:
            self._insert_unplayed_queue_item(path, play_next=False)

    def _confirm_batch_duplicate_add(self, duplicate_count: int, new_count: int, total_count: int) -> str:
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Duplicate Tracks in Up Next")
        layout = QtWidgets.QVBoxLayout(dlg)
        track_is_are = "track is" if duplicate_count == 1 else "tracks are"
        new_word = "track" if new_count == 1 else "tracks"
        label = QtWidgets.QLabel(
            f"{duplicate_count} {track_is_are} already in Up Next.\n\n"
            f"Add only the {new_count} new {new_word}, add all {total_count} tracks, or cancel?"
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        add_new_btn = buttons.addButton("Add New Only", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        add_all_btn = buttons.addButton("Add All", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        layout.addWidget(buttons)
        choice = {"value": "cancel"}

        def _choose_new_only():
            choice["value"] = "new_only"
            dlg.accept()

        def _choose_all():
            choice["value"] = "all"
            dlg.accept()

        add_new_btn.clicked.connect(_choose_new_only)
        add_all_btn.clicked.connect(_choose_all)
        buttons.rejected.connect(dlg.reject)
        add_new_btn.setDefault(True)
        add_new_btn.setAutoDefault(True)
        dlg.exec()
        return choice["value"]

    def _confirm_single_duplicate_add(self) -> str:
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Duplicate Track in Up Next")
        layout = QtWidgets.QVBoxLayout(dlg)
        label = QtWidgets.QLabel("This track is already in Up Next.")
        label.setWordWrap(True)
        layout.addWidget(label)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        add_again_btn = buttons.addButton("Add Again", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        layout.addWidget(buttons)
        add_again_btn.clicked.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        add_again_btn.setDefault(True)
        add_again_btn.setAutoDefault(True)
        result = dlg.exec()
        return "add_again" if result == QtWidgets.QDialog.DialogCode.Accepted else "cancel"

    def _queue_add_status_text(self, outcome: "QueueAddOutcome", result, added_via_new_only: bool) -> str:
        if not result.has_duplicates:
            if outcome.added_count == 1:
                return "Added track to Up Next"
            return f"Added {outcome.added_count} tracks to Up Next"
        added_word = "track" if outcome.added_count == 1 else "tracks"
        dupe_word = "duplicate" if result.duplicate_count == 1 else "duplicates"
        if added_via_new_only:
            return f"{outcome.added_count} {added_word} added; {result.duplicate_count} {dupe_word} skipped."
        return f"{outcome.added_count} {added_word} added, including {result.duplicate_count} {dupe_word}."

    def _add_to_queue_with_dedup_guard(
        self,
        incoming,
        *,
        path_of=lambda item: item,
        insert_fn=None,
        undo_action: str = "add",
        announce: bool = True,
    ) -> QueueAddOutcome:
        """Single funnel point for every 'Add to Up Next' batch: builds the
        existing-queue normalized-path set once, warns about duplicates at
        most once per call (never per-track), then inserts, saves and
        announces exactly once."""
        if not incoming:
            return QueueAddOutcome(added_count=0, duplicate_count=0, cancelled=False)
        if insert_fn is None:
            insert_fn = self._insert_queue_paths
        warn_on = getattr(self, "warn_before_adding_duplicate_queue_tracks", True)
        result = partition_incoming_batch(incoming, self.queue, path_of=path_of)

        added_via_new_only = False
        if not warn_on or not result.has_duplicates:
            final_items = result.all_items
        else:
            if result.total_count == 1:
                choice = self._confirm_single_duplicate_add()
            else:
                choice = self._confirm_batch_duplicate_add(
                    result.duplicate_count, result.new_count, result.total_count
                )
            if choice == "cancel":
                return QueueAddOutcome(
                    added_count=0, duplicate_count=result.duplicate_count, cancelled=True
                )
            final_items = result.new_items if choice == "new_only" else result.all_items
            added_via_new_only = choice == "new_only"

        if final_items:
            with capture_queue_undo(self, undo_action):
                insert_fn(final_items)
            self._refresh_queue_list(cached_details_only=True, reason="tracks_added")
            self._request_queue_analysis_for_paths(
                path_of(item) for item in final_items
            )
            self._schedule_session_save()

        outcome = QueueAddOutcome(
            added_count=len(final_items),
            duplicate_count=result.duplicate_count,
            cancelled=False,
        )
        if announce:
            self._announce_accessible_status(
                self._queue_add_status_text(outcome, result, added_via_new_only)
            )
        return outcome

    def _add_selected_library_item_to_queue(self):
        item = self.tree_tracks.currentItem()
        if item is None:
            self._announce_accessible_status("No library item selected")
            return
        data = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if isinstance(data, str):
            self._add_to_queue_with_dedup_guard([data])
            return
        if isinstance(data, dict) and data.get("placeholder"):
            return
        if isinstance(data, dict) and "album" in data:
            paths = self._album_paths_from_data(data)
            self._add_to_queue_with_dedup_guard(paths)
            return
        self._announce_accessible_status("Artist cannot be added to Up Next")

    def _remove_selected_queue_item(self):
        row = self.queue_list.currentRow()
        if not (0 <= row < len(self.queue)):
            return
        self._ensure_queue_played_flags()
        with capture_queue_undo(self, "remove"):
            self.queue.pop(row)
            self.queue_playlist_entries.pop(row)
            if row < len(self.queue_played):
                self.queue_played.pop(row)
            next_row = min(row, len(self.queue) - 1)
            self._remove_queue_row_widget(row, reason="track_removed")
            if next_row >= 0:
                self.queue_list.setCurrentRow(next_row)
        self._schedule_session_save()
        self._announce_accessible_status("Removed track from Up Next")

    def _move_selected_queue_item(self, delta):
        row = self.queue_list.currentRow()
        target = row + int(delta)
        if not (0 <= row < len(self.queue) and 0 <= target < len(self.queue)):
            return
        self._ensure_queue_played_flags()
        with capture_queue_undo(self, "reorder"):
            self.queue[row], self.queue[target] = self.queue[target], self.queue[row]
            self.queue_played[row], self.queue_played[target] = (
                self.queue_played[target], self.queue_played[row]
            )
            self.queue_playlist_entries[row], self.queue_playlist_entries[target] = (
                self.queue_playlist_entries[target], self.queue_playlist_entries[row]
            )
            self._move_queue_row_widget(row, target, reason="track_moved")
            self.queue_list.setCurrentRow(target)
        self._schedule_session_save()
        direction = "up" if delta < 0 else "down"
        self._announce_accessible_status(f"Moved Up Next item {direction}")

    def _shuffle_up_next(self):
        if not self.queue:
            return
        with capture_queue_undo(self, "shuffle"):
            combined = list(zip(
                self.queue, self.queue_played, self.queue_playlist_entries
            ))
            random.shuffle(combined)
            if combined:
                self.queue, self.queue_played, self.queue_playlist_entries = map(
                    list, zip(*combined)
                )
            self._refresh_queue_list(cached_details_only=True, reason="shuffle")
        self._schedule_session_save()

    def _move_queue_item_to_top(self, row: int):
        if not (0 <= row < len(self.queue)):
            return
        with capture_queue_undo(self, "reorder"):
            path = self.queue.pop(row)
            played = self.queue_played.pop(row) if row < len(self.queue_played) else False
            entry = self.queue_playlist_entries.pop(row)
            self.queue.insert(0, path)
            self.queue_played.insert(0, played)
            self.queue_playlist_entries.insert(0, entry)
            self._move_queue_row_widget(row, 0, reason="track_moved")
            self.queue_list.setCurrentRow(0)
        self._schedule_session_save()

    def _remove_played_queue_tracks(self):
        if not any(self.queue_played):
            return
        confirm = QtWidgets.QMessageBox.question(
            self,
            "Remove Played Tracks",
            "Remove played tracks from Up Next?\n\nThis is manual only. Saving the full playlist still keeps played tracks unless you remove them here.",
        )
        if confirm != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        with capture_queue_undo(self, "remove_played"):
            kept = [
                entry for entry in zip(
                    self.queue, self.queue_played,
                    self.queue_playlist_entries
                ) if not entry[1]
            ]
            self.queue = [p for p, _, _ in kept]
            self.queue_played = [played for _, played, _ in kept]
            self.queue_playlist_entries = [
                entry for _, _, entry in kept
            ]
            self._refresh_queue_list(cached_details_only=True, reason="played_tracks_removed")
        self._schedule_session_save()

    def _clear_up_next_queue(self):
        if not self.queue:
            return
        with capture_queue_undo(self, "clear"):
            self.queue.clear()
            self.queue_played.clear()
            self.queue_playlist_entries.clear()
            self._refresh_queue_list(cached_details_only=True, reason="clear_queue")
        self._schedule_session_save()

    def _update_undo_action_state(self):
        action = getattr(self, "action_undo_queue_change", None)
        if action is None:
            return
        snapshot = self._queue_undo_snapshot
        if snapshot is None:
            action.setEnabled(False)
            action.setText(GENERIC_UNDO_LABEL)
        else:
            action.setEnabled(True)
            action.setText(UNDO_ACTION_LABELS.get(snapshot.action, GENERIC_UNDO_LABEL))

    def _restore_queue_selection(self, selected_rows, scroll_position):
        blocked = self.queue_list.blockSignals(True)
        try:
            self.queue_list.clearSelection()
            count = self.queue_list.count()
            first_valid = None
            for row in selected_rows or []:
                if 0 <= row < count:
                    self.queue_list.item(row).setSelected(True)
                    if first_valid is None:
                        first_valid = row
            if first_valid is not None:
                self.queue_list.setCurrentRow(first_valid)
            if scroll_position is not None:
                self.queue_list.verticalScrollBar().setValue(scroll_position)
        finally:
            self.queue_list.blockSignals(blocked)

    def _undo_queue_change(self):
        snapshot = self._queue_undo_snapshot
        if snapshot is None:
            return
        started = time.perf_counter()
        try:
            if not (
                len(snapshot.queue) == len(snapshot.queue_played)
                == len(snapshot.queue_playlist_entries)
            ):
                raise ValueError("undo snapshot lists are misaligned")
            self.queue = list(snapshot.queue)
            self.queue_played = list(snapshot.queue_played)
            self.queue_playlist_entries = list(snapshot.queue_playlist_entries)
            self._ensure_queue_played_flags()
            # A structural mutation that can change what's at any given
            # row -- a preload/candidate selection recorded by (epoch, row,
            # path) before the undo must not be trusted to still describe
            # the same logical item afterward (see video_dual_transition.py's
            # SecondaryIdentity docstring).
            self._queue_mutation_epoch = getattr(self, "_queue_mutation_epoch", 0) + 1
            # A single full reset is acceptable here -- the entire queue
            # state may change, so there is no meaningful "targeted" restore.
            # keep_played_bottom=False: undo must restore the exact previous
            # order, not re-apply the played-tracks-at-bottom policy on top
            # of it (which could reorder a snapshot that had a played track
            # in the middle, e.g. after undoing "Remove Played Tracks").
            self._refresh_queue_list(
                reason="undo_queue_change", keep_played_bottom=False,
                cached_details_only=True,
            )
            self._restore_queue_selection(snapshot.selected_rows, snapshot.scroll_position)
            # Deliberately NOT debounced through _schedule_session_save()
            # like the other queue mutations below -- test_queue_undo.py's
            # test_undo_saves_session_exactly_once asserts this saves
            # immediately (undo is often the last action before closing
            # the app; an earlier v1.0.67 audit had flagged this call site
            # as an oversight, but this existing, deliberate, passing test
            # says otherwise, so it's left unchanged).
            self._save_session()
            duration_ms = (time.perf_counter() - started) * 1000.0
            self.diagnostics.record(
                "queue", "undo_restore",
                duration_ms=duration_ms,
                details={
                    "action": snapshot.action,
                    "queue_size": len(self.queue),
                    "playback_active": bool(self._playback_expected),
                    "current_track_valid": (
                        (self.current_path in self.queue) if self.current_path else None
                    ),
                },
                minimum_level="basic",
            )
            message = UNDO_STATUS_MESSAGES.get(snapshot.action, "Up Next restored")
            self.statusBar().showMessage(message, 4000)
            self._announce_accessible_status(message)
        except Exception as ex:
            self._log(f"Undo queue change failed: {ex}")
            self.diagnostics.record(
                "queue", "undo_restore", status="failure", severity="warning",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                details={"exception": str(ex)}, minimum_level="basic",
            )
            self.statusBar().showMessage("Couldn't undo the last queue change", 4000)
        finally:
            self._queue_undo_snapshot = None
            self._update_undo_action_state()

    def _context_menu_position(self, view):
        item = view.currentItem()
        if item is None:
            return view.viewport().rect().center()
        rect = view.visualItemRect(item)
        return rect.center() if rect.isValid() else view.viewport().rect().center()

    def _open_context_menu_for_focused_widget(self):
        focused = QtWidgets.QApplication.focusWidget()
        if focused is self.tree_tracks or self.tree_tracks.isAncestorOf(focused):
            self._show_tree_menu(self._context_menu_position(self.tree_tracks))
            return True
        if focused is self.queue_list or self.queue_list.isAncestorOf(focused):
            self._show_queue_menu(self._context_menu_position(self.queue_list))
            return True
        if focused is self.recent_table or self.recent_table.isAncestorOf(focused):
            row = self.recent_table.currentRow()
            rect = self.recent_table.visualItemRect(
                self.recent_table.item(row, 0)
            ) if row >= 0 else QtCore.QRect()
            pos = rect.center() if rect.isValid() else self.recent_table.viewport().rect().center()
            self._show_recently_played_menu(pos)
            return True
        return False

    def _queue_play_next(self, path: str):
        row = self._insert_unplayed_queue_item(path, play_next=True)
        self._insert_queue_row_widget(row, reason="track_added")
        self._schedule_session_save()

    def _insert_unplayed_queue_item(self, path: str, play_next: bool = False):
        # New tracks should join the unplayed block, never below played history.
        self._ensure_queue_played_flags()
        insert_at = 0 if play_next else self._first_played_queue_row()
        self.queue.insert(insert_at, path)
        self.queue_played.insert(insert_at, False)
        self.queue_playlist_entries.insert(insert_at, None)
        self._queue_mutation_epoch = getattr(self, "_queue_mutation_epoch", 0) + 1
        return insert_at

    def _first_played_queue_row(self) -> int:
        self._ensure_queue_played_flags()
        for row, played in enumerate(self.queue_played):
            if played:
                return row
        return len(self.queue)

    def _keep_played_tracks_at_bottom(self):
        # Keep Up Next as: all unplayed tracks first, played/saveable history last.
        self._ensure_queue_played_flags()
        combined = list(zip(self.queue, self.queue_played, self.queue_playlist_entries))
        unplayed = [entry for entry in combined if not entry[1]]
        played_items = [entry for entry in combined if entry[1]]
        ordered = unplayed + played_items
        self.queue = [p for p, _, _ in ordered]
        self.queue_played = [played for _, played, _ in ordered]
        self.queue_playlist_entries = [entry for _, _, entry in ordered]

    def _queue_played_role(self):
        return QtCore.Qt.ItemDataRole.UserRole.value + 1

    def _queue_playlist_role(self):
        return QtCore.Qt.ItemDataRole.UserRole.value + 2

    def _ensure_queue_played_flags(self):
        if not hasattr(self, "queue_playlist_entries"):
            self.queue_playlist_entries = []
        if len(self.queue_played) < len(self.queue):
            self.queue_played.extend([False] * (len(self.queue) - len(self.queue_played)))
        elif len(self.queue_played) > len(self.queue):
            self.queue_played = self.queue_played[:len(self.queue)]
        if len(self.queue_playlist_entries) < len(self.queue):
            self.queue_playlist_entries.extend(
                [None] * (len(self.queue) - len(self.queue_playlist_entries))
            )
        elif len(self.queue_playlist_entries) > len(self.queue):
            self.queue_playlist_entries = self.queue_playlist_entries[:len(self.queue)]

    def _next_unplayed_queue_row(self):
        self._ensure_queue_played_flags()
        for row, played in enumerate(self.queue_played):
            if not played:
                return row
        return None

    def _queue_entry_is_missing(self, row: int) -> bool:
        self._ensure_queue_played_flags()
        return bool(
            0 <= row < len(self.queue_playlist_entries)
            and self.queue_playlist_entries[row] is not None
            and self.queue_playlist_entries[row].is_missing
        )

    def _previous_played_queue_row(self):
        """Return the previous played Up Next row so Back follows queue history before library order."""
        self._ensure_queue_played_flags()
        played_rows = [row for row, played in enumerate(self.queue_played) if played]
        if not played_rows:
            return None
        current_rows = [row for row in played_rows if row < len(self.queue) and self.queue[row] == self.current_path]
        if current_rows:
            current_row = current_rows[-1]
            pos = played_rows.index(current_row)
            if len(played_rows) == 1:
                return current_row
            return played_rows[pos - 1]
        return played_rows[-1]

    def _mark_queue_row_played(self, row: int):
        # Played tracks stay saveable, but move to the bottom so the next unplayed song sits at the top.
        self._ensure_queue_played_flags()
        if 0 <= row < len(self.queue_played):
            self.queue_played[row] = True
            self._remove_queue_row_widget(
                row, reason="played_state_changed"
            )
            moved_row = self._move_queue_row_to_bottom(row)
            self._insert_queue_row_widget(
                moved_row, reason="played_state_changed"
            )
            self._animate_queue_history_move(moved_row)
            self._schedule_session_save()

    def _move_queue_row_to_bottom(self, row: int) -> int:
        if not (0 <= row < len(self.queue)):
            return row
        path = self.queue.pop(row)
        entry = self.queue_playlist_entries.pop(row)
        if row < len(self.queue_played):
            self.queue_played.pop(row)
        self.queue.append(path)
        self.queue_played.append(True)
        self.queue_playlist_entries.append(entry)
        self._queue_mutation_epoch = getattr(self, "_queue_mutation_epoch", 0) + 1
        return len(self.queue) - 1

    def _animate_queue_history_move(self, row: int):
        if not hasattr(self, "queue_list") or not (0 <= row < self.queue_list.count()):
            return
        item = self.queue_list.item(row)
        widget = self.queue_list.itemWidget(item)
        if widget is None:
            return
        effect = QtWidgets.QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
        anim = QtCore.QPropertyAnimation(effect, b"opacity", self)
        anim.setStartValue(0.25)
        anim.setKeyValueAt(0.45, 1.0)
        anim.setEndValue(0.72)
        anim.setDuration(650)
        anim.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
        self.queue_move_anims.append(anim)
        anim.finished.connect(lambda a=anim, w=widget: self._finish_queue_history_anim(a, w))
        anim.start()

    def _finish_queue_history_anim(self, anim, widget):
        try:
            widget.setGraphicsEffect(None)
        except Exception:
            pass
        try:
            self.queue_move_anims.remove(anim)
        except ValueError:
            pass

    def _queue_tag_value(self, tags, keys: Tuple[str, ...]) -> str:
        values = self._tag_values_for_keys(tags, keys)
        return values[0] if values else ""

    def _request_queue_analysis(self, path: str, details: Dict[str, str]):
        # BPM/key estimation runs off the GUI thread; rows are updated when the worker replies.
        if not path or not getattr(self, "queue_analysis_worker", None):
            return
        if is_plex_identity(path):
            # Never queued for decode-based analysis, ever -- see item 9:
            # a Plex item's BPM/Key comes only from Plex's own metadata
            # (already applied to queue_detail_cache/queue_analysis_cache
            # at fetch time, see _on_plex_library_fetch_result), or stays
            # "--". This also guards the widened current+next-3 priority
            # window from ever creating Plex network analysis work --
            # nothing downstream of this early return can reach
            # QueueAnalysisWorker for a plex:// identity.
            return
        if all(
            details.get(field) != "--"
            for field in ("time", "bitrate", "key", "bpm")
        ):
            return
        if path in self.queue_analysis_pending:
            return
        self.queue_analysis_pending.add(path)
        missing_fields = {
            field for field in ("bpm", "key") if details.get(field) == "--"
        }
        media_type = classify_path(path)
        # KARAOKE (.cdg/.zip) never gets BPM/key -- no independently
        # decodable audio stream of its own (a CDG's backing MP3 is a
        # separate AUDIO path, already eligible on its own). Music (or
        # video) queued while video playback or the visualiser's native
        # FFT is active is deprioritised: metadata now (Time/Rate fill in
        # immediately), real BPM/key once the competing work stops --
        # except the bounded priority window (current track + next 3 Up
        # Next), which must always get real analysis immediately
        # regardless. Real-device bug (2026-09-06): this used to only
        # exempt the literal current path, so any *other* video queued
        # while a video was current -- including the very next Up Next
        # row -- fell into this branch and was added to
        # _deferred_bpm_key_paths. That set is only drained by
        # _resume_deferred_queue_analysis(), which fires solely on a
        # video->audio media-type change; an all-video Up Next queue
        # never produces one, so those rows stayed "--"/"--" forever with
        # no bpm_key_analysis_requested ever logged for them. Checking
        # priority-window membership instead of just the current path
        # both fixes that and still covers the current track's own media
        # type never deferring its own analysis against itself, since
        # current_path is always the first entry of _queue_priority_paths().
        if media_type == MediaType.KARAOKE:
            self.queue_analysis_worker.request_metadata(path)
        elif path not in self._queue_priority_paths() and (
            self._current_media_type == MediaType.VIDEO
            or getattr(self, "_visualiser_analysis_busy", False)
        ):
            self.queue_analysis_worker.request_metadata(path)
            self._deferred_bpm_key_paths.add(path)
        elif missing_fields:
            self.queue_bpm_key_pending_fields[path] = missing_fields
            self.diagnostics.record(
                "worker", "bpm_key_analysis_requested",
                details={
                    **self.diagnostics.path_details(path),
                    "requested_fields": sorted(missing_fields),
                    "media_type": media_type.value,
                },
                minimum_level="detailed",
            )
            self.queue_analysis_worker.request(path, fields=frozenset(missing_fields))
        else:
            self.queue_analysis_worker.request_metadata(path)
        if hasattr(self, "queue_spinner_timer") and not self.queue_spinner_timer.isActive():
            self.queue_spinner_timer.start()

    def _resume_deferred_queue_analysis(self):
        """Resume BPM/key work deferred by video or visualiser analysis."""
        if (
            getattr(self, "_visualiser_analysis_busy", False)
            or not self._deferred_bpm_key_paths
        ):
            return
        paths = list(self._deferred_bpm_key_paths)
        self._deferred_bpm_key_paths.clear()
        if not getattr(self, "queue_analysis_worker", None):
            return
        for path in paths:
            if not path or classify_path(path) == MediaType.KARAOKE:
                continue
            details = self.queue_detail_cache.get(path) or {}
            missing_fields = {
                field for field in ("bpm", "key") if details.get(field) == "--"
            }
            if not missing_fields:
                continue
            self.queue_analysis_pending.add(path)
            self.queue_bpm_key_pending_fields[path] = missing_fields
            self.diagnostics.record(
                "worker", "bpm_key_analysis_requested",
                details={
                    **self.diagnostics.path_details(path),
                    "requested_fields": sorted(missing_fields),
                    "media_type": classify_path(path).value,
                },
                minimum_level="detailed",
            )
            self.queue_analysis_worker.request(path, fields=frozenset(missing_fields))
        if hasattr(self, "queue_spinner_timer") and not self.queue_spinner_timer.isActive():
            self.queue_spinner_timer.start()

    def _queue_priority_paths(self) -> List[str]:
        """Current track, then the next three upcoming (unplayed) Up Next
        rows -- the bounded window real BPM/Key analysis is dispatched
        for immediately when a batch of paths is queued at once;
        everything else gets metadata-only treatment until it enters this
        window (see _request_queue_analysis_for_paths /
        _promote_priority_queue_analysis). Widened from next-2 to next-3
        (2026-09-06, real-device evidence): video BPM/Key analysis can
        take ~7-16s via QAudioDecoder, longer than the ~2-3s a viewer
        typically spends on the second Up Next row before it becomes
        current -- starting one row earlier gives a real chance of the
        result being ready by the time that row is reached."""
        priority: List[str] = []
        if self.current_path:
            priority.append(self.current_path)
        self._ensure_queue_played_flags()
        for row, path in enumerate(self.queue):
            if path == self.current_path:
                continue
            if row < len(self.queue_played) and self.queue_played[row]:
                continue
            priority.append(path)
            if len(priority) >= 4:
                break
        return priority

    def _request_queue_analysis_for_paths(self, paths):
        # Backfills Time/Rate/Key/BPM off the GUI thread for a batch of
        # newly-added rows that were displayed via a cached-only refresh
        # (see _refresh_queue_list(cached_details_only=True)) -- mirrors
        # what _insert_queue_row_widget already does for a single row.
        # Bounded: only the current-track + next-3 priority window gets
        # real BPM/Key requested immediately for a whole batch at once;
        # everything else gets metadata-only (Time/Rate) now and is
        # promoted to real analysis once the priority window shifts to
        # include it (see _promote_priority_queue_analysis) -- avoids
        # analysing an entire freshly-queued album/artist/multi-file batch
        # simultaneously.
        priority = self._queue_priority_paths()
        for path in paths:
            if not path:
                continue
            details = self.queue_detail_cache.get(path)
            if details is None:
                details = self._queue_track_details(path, cached_details_only=True)
            if path in priority:
                self._request_queue_analysis(path, details)
            else:
                self._request_metadata_only_queue_analysis(path, details)

    def _request_metadata_only_queue_analysis(self, path: str, details: Dict[str, str]):
        # Deliberately does NOT add to queue_analysis_pending -- that set
        # also gates _request_queue_analysis's own dedup, and this path is
        # expected to be promoted to a *real* analysis request shortly
        # (see _promote_priority_queue_analysis) while this metadata-only
        # job may still be draining through the worker's queue. Blocking
        # the promotion on a stale metadata-only "pending" entry would
        # defeat bounded prioritisation. Duplicate metadata-only submissions
        # are already safe: the worker's own _enqueue dedups/replays by
        # (path, metadata_only) regardless of what this window tracks.
        if not path or not getattr(self, "queue_analysis_worker", None):
            return
        if is_plex_identity(path):
            # Same reasoning as _request_queue_analysis's own guard: a
            # plex:// identity's Time/Rate/Key/BPM come only from Plex's
            # own metadata (already applied at fetch time) or stay "--";
            # the worker's metadata-only path calls MutagenFile/os.stat,
            # which would just fail against a synthetic identity.
            return
        if all(
            details.get(field) != "--"
            for field in ("time", "bitrate", "key", "bpm")
        ):
            return
        self.queue_analysis_worker.request_metadata(path)
        self._queue_priority_deferred_paths.add(path)

    def _promote_priority_queue_analysis(self):
        """Whenever the priority window (current track + next 3 Up Next)
        shifts -- most often because the current track just changed --
        promote any metadata-only-deferred path that has now entered the
        window to real BPM/Key analysis. Deliberately separate from
        _resume_deferred_queue_analysis's video/visualiser busy-gate
        (that one drains everything once competing work stops; reusing it
        here would defeat bounded prioritisation).

        Also drains _deferred_bpm_key_paths, not just
        _queue_priority_deferred_paths: a path can land in the former
        (video/visualiser-busy deferral, see _request_queue_analysis)
        while genuinely outside the priority window at request time, then
        legitimately enter the window later as playback advances -- that
        must promote it too, the same as a batch-add deferral would."""
        if not self._queue_priority_deferred_paths and not self._deferred_bpm_key_paths:
            return
        for path in self._queue_priority_paths():
            deferred_here = (
                path in self._queue_priority_deferred_paths
                or path in self._deferred_bpm_key_paths
            )
            if not deferred_here:
                continue
            self._queue_priority_deferred_paths.discard(path)
            self._deferred_bpm_key_paths.discard(path)
            # A metadata-only request for this same path may still be
            # marked pending -- that must not block the real request
            # about to be made here (see _request_queue_analysis's own
            # dedup guard at the top of that method).
            self.queue_analysis_pending.discard(path)
            details = self.queue_detail_cache.get(path)
            if details is None:
                details = self._queue_track_details(path, cached_details_only=True)
            self._request_queue_analysis(path, details)

    def _start_legacy_queue_enrichment(self):
        """Queue legacy Time/Rate gaps after startup, one file at a time."""
        if getattr(self, "_closing", False) or not getattr(
            self, "queue_analysis_worker", None
        ):
            return
        self._legacy_queue_metadata_backlog = [
            path for path in dict.fromkeys(self.queue)
            if (
                (self.queue_detail_cache.get(path) or {}).get("time") == "--"
                or (self.queue_detail_cache.get(path) or {}).get("bitrate") == "--"
            )
        ]
        if (
            self._legacy_queue_metadata_backlog
            and hasattr(self.queue_analysis_worker, "setPriority")
        ):
            self.queue_analysis_worker.setPriority(
                QtCore.QThread.Priority.LowestPriority
            )
        self._request_next_legacy_queue_metadata()

    def _request_next_legacy_queue_metadata(self):
        if (
            getattr(self, "_closing", False)
            or self._queue_metadata_pending is not None
            or not getattr(self, "queue_analysis_worker", None)
        ):
            return
        while self._legacy_queue_metadata_backlog:
            path = self._legacy_queue_metadata_backlog.pop(0)
            details = self.queue_detail_cache.get(path) or {}
            if (
                details.get("time") != "--"
                and details.get("bitrate") != "--"
            ):
                continue
            self._queue_metadata_pending = path
            self.queue_analysis_worker.request_metadata(path)
            return
        if hasattr(self.queue_analysis_worker, "setPriority"):
            self.queue_analysis_worker.setPriority(
                QtCore.QThread.Priority.NormalPriority
            )

    def _on_queue_metadata_ready(self, path: str, result: Dict[str, str]):
        if self._queue_metadata_pending == path:
            self._queue_metadata_pending = None
        self._on_queue_analysis_ready(path, result, metadata_only=True)
        QtCore.QTimer.singleShot(
            750, self._request_next_legacy_queue_metadata
        )

    def _queue_spinner_tick(self):
        if not self.queue_analysis_pending:
            self.queue_spinner_timer.stop()
            for path in list(self._queue_row_widgets):
                self._update_queue_rows(path, reason="analysis_spinner_complete")
            return
        self.queue_spinner_index = (self.queue_spinner_index + 1) % 4
        for path in self.queue_analysis_pending:
            self._update_queue_rows(path, reason="analysis_spinner")

    def _on_queue_analysis_ready(self, path: str, result: Dict[str, str], metadata_only: bool = False):
        self.queue_analysis_pending.discard(path)
        # Popped unconditionally (even if result is empty below) so a
        # genuinely-attempted-but-empty result can never leave a column's
        # spinner stuck -- "completed with no result/failed" must still
        # resolve to "--", never an indefinite spinner. Exception: a
        # metadata-only completion must never pop this -- that dict is
        # only ever populated when a *real* BPM/Key request is dispatched
        # (_request_queue_analysis's own "elif missing_fields" branch), so
        # it can only be non-empty here because a real request for the
        # same path is genuinely still in flight -- e.g. promoted past a
        # still-running metadata-only companion job by
        # _promote_priority_queue_analysis() (see its own comment). Popping
        # it on this stale metadata completion would prematurely stop that
        # row's spinner while the real analysis is still running.
        if not metadata_only:
            self.queue_bpm_key_pending_fields.pop(path, None)
        if not result:
            return
        details = self.queue_detail_cache.get(path, {"bitrate": "--", "time": "--", "key": "--", "bpm": "--"})
        # Calculated values only fill blanks; embedded tag values should always win.
        if details.get("key") == "--" and result.get("key"):
            details["key"] = result["key"]
        if details.get("bpm") == "--" and result.get("bpm"):
            details["bpm"] = result["bpm"]
        if details.get("time") == "--" and result.get("time"):
            details["time"] = result["time"]
        if details.get("bitrate") == "--" and result.get("bitrate"):
            details["bitrate"] = result["bitrate"]
        self.queue_detail_cache[path] = details
        signature = {
            "mtime": int(result.get("_mtime", 0) or 0),
            "size": int(result.get("_size", 0) or 0),
        }
        cached_result = {
            key: details[key]
            for key in ("time", "bitrate", "key", "bpm")
            if details.get(key) and details.get(key) != "--"
        }
        self._store_queue_analysis(
            path, cached_result, signature=signature
        )
        if path == self.current_path:
            # _load_cached_audio_tags builds a Track purely from in-memory
            # caches (no file I/O) -- queue_detail_cache[path] was just
            # updated above with the fresh key/bpm/time/bitrate, so this
            # picks them up immediately. Passing info=None here previously
            # made _update_dj_info fall through to a synchronous
            # MutagenFile() re-read of a path a background worker had
            # already just finished analysing -- a proven real-device
            # GUI-thread stall caught inside MP4 atom reading via exactly
            # this call chain.
            self._update_dj_info(self._load_cached_audio_tags(path), path)
        self._audio_log(f"queue analysis ready; file={self._audio_name(path)!r}; key={details.get('key')}; bpm={details.get('bpm')}")
        if not self.queue_analysis_pending and hasattr(self, "queue_spinner_timer"):
            self.queue_spinner_timer.stop()
        if path in self.queue or path == self.current_path:
            if result.get("time"):
                getattr(self, "_schedule_queue_duration_refresh", lambda *_: None)("duration_available")
        if path in self.queue:
            self._update_queue_rows(path, reason="analysis_result")

    def _update_queue_rows(self, path: str, reason="metadata"):
        started = time.perf_counter()
        rows = self._queue_row_widgets.get(path, ())
        details = self.queue_detail_cache.get(
            path, {"bitrate": "--", "time": "--", "key": "--", "bpm": "--"}
        )
        spinner = ("|", "/", "-", "\\")[self.queue_spinner_index % 4]
        pending_fields = self.queue_bpm_key_pending_fields.get(path, ())
        for row in rows:
            row["time"].setText(details.get("time", "--"))
            row["bitrate"].setText(details.get("bitrate", "--"))
            row["key"].setText(
                spinner if "key" in pending_fields
                else details.get("key", "--")
            )
            row["bpm"].setText(
                spinner if "bpm" in pending_fields
                else details.get("bpm", "--")
            )
        duration_ms = (time.perf_counter() - started) * 1000.0
        self.diagnostics.record(
            "queue", "targeted_up_next_update",
            duration_ms=duration_ms,
            details={
                "reason": reason,
                "rows_updated": len(rows),
                "avoided_full_refresh": bool(rows),
                "duplicate_path_rows": max(0, len(rows) - 1),
            },
            minimum_level="detailed",
        )
        return len(rows)

    def _queue_track_details(
        self, path: str, cached_details_only: bool = False
    ) -> Dict[str, str]:
        cached = self.queue_detail_cache.get(path)
        if cached is not None:
            return cached
        details = {"bitrate": "--", "time": "--", "key": "--", "bpm": "--"}
        if is_plex_identity(path):
            # A plex:// identity is never a local path -- Mutagen/os.stat
            # would just fail. The normal fetch already populated
            # queue_detail_cache for every item in a mapped library (see
            # plex_meta_list_to_queue_detail_cache), so reaching this
            # branch means the row is showing before/without a completed
            # fetch (e.g. a restored offline queue) -- fall back to
            # whatever was persisted in the on-disk analysis cache and
            # otherwise show "--", never block or attempt network/file I/O.
            cached_analysis = self._cached_queue_analysis(path, validate_signature=False)
            if cached_analysis:
                for key in ("time", "bitrate", "key", "bpm"):
                    details[key] = cached_analysis.get(key) or "--"
            self.queue_detail_cache[path] = details
            return details
        if classify_path(path) == MediaType.KARAOKE:
            meta = getattr(self, "_meta_by_path", {}).get(path) or {}
            duration = float(meta.get("duration_seconds") or 0)
            if duration > 0:
                mins, secs = divmod(int(round(duration)), 60)
                details["time"] = f"{mins}:{secs:02d}"
            self.queue_detail_cache[path] = details
            return details
        if cached_details_only:
            # Startup queue rendering must never touch slow/network music
            # paths. The cache signature is validated later when enrichment
            # is explicitly requested off the startup display path.
            cached_analysis = self._cached_queue_analysis(
                path, validate_signature=False
            )
            if cached_analysis:
                for key in ("time", "bitrate", "key", "bpm"):
                    details[key] = cached_analysis.get(key) or "--"
            self.queue_detail_cache[path] = details
            return details
        try:
            audio = MutagenFile(path, easy=True)
            if audio and getattr(audio, "info", None):
                bitrate = getattr(audio.info, "bitrate", 0) or 0
                if bitrate:
                    details["bitrate"] = f"{int(bitrate / 1000)}k"
                length = getattr(audio.info, "length", 0) or 0
                if length:
                    mins, secs = divmod(int(round(length)), 60)
                    details["time"] = f"{mins}:{secs:02d}"
            if audio:
                musical_key = self._queue_tag_value(audio, ("initialkey", "key", "musicalkey"))
                bpm = self._queue_tag_value(audio, ("bpm", "tempo"))
                if musical_key:
                    details["key"] = musical_key
                if bpm:
                    details["bpm"] = bpm.split(".")[0]
            if details["key"] == "--" or details["bpm"] == "--":
                full_audio = MutagenFile(path)
                tags = getattr(full_audio, "tags", None) if full_audio else None
                if details["key"] == "--":
                    musical_key = self._queue_tag_value(tags, ("TKEY", "initialkey", "INITIALKEY", "initial key", "musicalkey", "----:com.apple.iTunes:initialkey"))
                    if musical_key:
                        details["key"] = musical_key
                if details["bpm"] == "--":
                    bpm = self._queue_tag_value(tags, ("TBPM", "bpm", "BPM", "tempo", "tmpo", "----:com.apple.iTunes:BPM"))
                    if bpm:
                        details["bpm"] = bpm.split(".")[0]
            cached_analysis = self._cached_queue_analysis(path)
            if cached_analysis:
                if details["key"] == "--" and cached_analysis.get("key"):
                    details["key"] = cached_analysis["key"]
                if details["bpm"] == "--" and cached_analysis.get("bpm"):
                    details["bpm"] = cached_analysis["bpm"]
        except Exception:
            pass
        self.queue_detail_cache[path] = details
        self._request_queue_analysis(path, details)
        return details

    def _queue_column_label(self, text: str, played: bool, accent: bool = False) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        label.setWordWrap(False)
        label.setMinimumWidth(0)
        font = label.font()
        font.setStrikeOut(played)
        font.setBold(accent)
        label.setFont(font)
        color = "#8f82a8" if played else ("#fff8c8" if accent else "#d9ccff")
        label.setStyleSheet(f"color:{color}; background:transparent;")
        return label

    def _queue_title_marquee(self, text: str, played: bool) -> MarqueeLabel:
        # Long Up Next titles scroll inside their column instead of stretching the row layout.
        label = MarqueeLabel()
        label.setText(text)
        label.setMinimumWidth(0)
        label.setMaximumHeight(24)
        label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
        font = label.font()
        font.setStrikeOut(played)
        font.setBold(True)
        label.setFont(font)
        label.setTextColor("#8f82a8" if played else "#fff8c8")
        label.setToolTip(text)
        return label

    def _insert_queue_row_widget(self, row: int, reason="track_added"):
        if not (0 <= row < len(self.queue)):
            return False
        started = time.perf_counter()
        path = self.queue[row]
        meta = self._display_meta_for_path(path)
        entry = (
            self.queue_playlist_entries[row]
            if row < len(self.queue_playlist_entries) else None
        )
        missing = bool(entry and entry.is_missing)
        if meta:
            title = meta.get("title") or os.path.splitext(
                os.path.basename(path)
            )[0]
            artist = meta.get("artist") or ""
            label = f"{title} - {artist}" if artist else title
        else:
            label = os.path.splitext(os.path.basename(path))[0]
        if entry and entry.display_title:
            label = (
                f"{entry.display_title} - {entry.artist}"
                if entry.artist else entry.display_title
            )
        if missing:
            label = f"[Missing file] {label}"
        played = self.queue_played[row]
        details = self._queue_track_details(path, cached_details_only=True)
        item = QtWidgets.QListWidgetItem()
        item.setData(QtCore.Qt.ItemDataRole.UserRole, path)
        item.setData(self._queue_played_role(), played)
        item.setData(self._queue_playlist_role(), entry)
        item.setToolTip(
            ("Played - " if played else "") + label
            + (
                f"\nOriginal path: {entry.original_path or entry.path}"
                if missing else ""
            )
        )
        item.setSizeHint(QtCore.QSize(0, 34))
        row_widget = QtWidgets.QWidget()
        row_widget.setStyleSheet(
            "QWidget { background:rgba(255,60,172,0.10); "
            "border:1px solid rgba(177,76,255,0.34); border-radius:7px; }"
            if not played else
            "QWidget { background:rgba(90,72,120,0.16); "
            "border:1px solid rgba(120,102,150,0.26); border-radius:7px; }"
        )
        layout = QtWidgets.QHBoxLayout(row_widget)
        layout.setContentsMargins(8, 3, 44, 3)
        layout.setSpacing(8)
        title_label = self._queue_title_marquee(label, played)
        if missing:
            font = title_label.font()
            font.setItalic(True)
            title_label.setFont(font)
            title_label.setAccessibleName(f"Missing file: {label}")
        layout.addWidget(title_label, 6)
        time_label = self._queue_column_label(details.get("time", "--"), played)
        bitrate_label = self._queue_column_label(
            details.get("bitrate", "--"), played
        )
        layout.addWidget(time_label, 1)
        layout.addWidget(bitrate_label, 2)
        spinner = ("|", "/", "-", "\\")[self.queue_spinner_index % 4]
        pending_fields = self.queue_bpm_key_pending_fields.get(path, ())
        key_label = self._queue_column_label(
            spinner if "key" in pending_fields
            else details.get("key", "--"),
            played,
        )
        bpm_label = self._queue_column_label(
            spinner if "bpm" in pending_fields
            else details.get("bpm", "--"),
            played,
        )
        layout.addWidget(key_label, 1)
        layout.addWidget(bpm_label, 1)
        self.queue_list.insertItem(row, item)
        self.queue_list.setItemWidget(item, row_widget)
        self._queue_row_widgets.setdefault(path, []).append(
            {
                "item": item, "widget": row_widget, "title": title_label,
                "time": time_label, "bitrate": bitrate_label,
                "key": key_label, "bpm": bpm_label,
            }
        )
        self._request_queue_analysis(path, details)
        self.diagnostics.record(
            "queue", "incremental_up_next_update",
            duration_ms=(time.perf_counter() - started) * 1000.0,
            details={
                "reason": reason, "update_type": "insert",
                "row": row, "rows_preserved": self.queue_list.count() - 1,
            },
            minimum_level="basic",
        )
        self._sync_mini_player()
        getattr(self, "_schedule_queue_duration_refresh", lambda *_: None)(reason)
        return True

    def _shutdown_queue_row_marquee(self, row_entry: dict) -> None:
        """Real-device lifetime bug (2026-08-31 Codex audit): must be
        called on every tracked row-widget entry's "title" MarqueeLabel
        *before* its wrapper widget is handed to deleteLater() or an
        implicit Qt-side deferred delete (QListWidget.clear()) -- see
        MarqueeLabel.shutdown()'s own docstring for why. Tolerant of a
        missing/already-shut-down label so callers never need their own
        None-check."""
        title_label = row_entry.get("title") if row_entry else None
        if title_label is not None:
            try:
                title_label.shutdown()
            except Exception:
                pass

    def _remove_queue_row_widget(self, row: int, reason="track_removed"):
        if not (0 <= row < self.queue_list.count()):
            return False
        started = time.perf_counter()
        item = self.queue_list.item(row)
        path = item.data(QtCore.Qt.ItemDataRole.UserRole)
        widget = self.queue_list.itemWidget(item)
        self.queue_list.removeItemWidget(item)
        self.queue_list.takeItem(row)
        rows = self._queue_row_widgets.get(path, [])
        removed_entries = [value for value in rows if value.get("item") is item]
        for entry in removed_entries:
            self._shutdown_queue_row_marquee(entry)
        self._queue_row_widgets[path] = [
            value for value in rows if value.get("item") is not item
        ]
        if not self._queue_row_widgets[path]:
            self._queue_row_widgets.pop(path, None)
        if widget is not None:
            widget.deleteLater()
        self.diagnostics.record(
            "queue", "incremental_up_next_update",
            duration_ms=(time.perf_counter() - started) * 1000.0,
            details={
                "reason": reason, "update_type": "remove",
                "row": row, "rows_preserved": self.queue_list.count(),
            },
            minimum_level="basic",
        )
        self._sync_mini_player()
        getattr(self, "_schedule_queue_duration_refresh", lambda *_: None)(reason)
        return True

    def _move_queue_row_widget(self, source: int, target: int, reason="track_moved"):
        if not (
            0 <= source < self.queue_list.count()
            and 0 <= target < self.queue_list.count()
        ):
            return False
        started = time.perf_counter()
        item = self.queue_list.item(source)
        widget = self.queue_list.itemWidget(item)
        self.queue_list.removeItemWidget(item)
        item = self.queue_list.takeItem(source)
        self.queue_list.insertItem(target, item)
        if widget is not None:
            self.queue_list.setItemWidget(item, widget)
        self.diagnostics.record(
            "queue", "incremental_up_next_update",
            duration_ms=(time.perf_counter() - started) * 1000.0,
            details={
                "reason": reason, "update_type": "move",
                "source_row": source, "target_row": target,
                "rows_preserved": max(0, self.queue_list.count() - 1),
            },
            minimum_level="basic",
        )
        self._sync_mini_player()
        getattr(self, "_schedule_queue_duration_refresh", lambda *_: None)(reason)
        return True

    # --- Up Next duration & finish-time summary ---------------------------
    # Reads only already-cached duration data (queue_detail_cache,
    # _meta_by_path, PlaylistEntry.duration_seconds) and the live player's
    # own get_length()/get_pos() -- never opens a file or queries a backend
    # just to answer "how long is left".

    def _queue_entry_duration_seconds(
        self, path: str, playlist_entry: Optional[PlaylistEntry],
    ) -> Optional[float]:
        if playlist_entry is not None and getattr(playlist_entry, "duration_seconds", None):
            try:
                value = float(playlist_entry.duration_seconds)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                pass
        details = self.queue_detail_cache.get(path)
        if details:
            parsed = parse_time_field_seconds(details.get("time"))
            if parsed is not None:
                return parsed
        meta = getattr(self, "_meta_by_path", {}).get(path)
        if meta:
            value = meta.get("duration_seconds")
            if value is not None:
                try:
                    value = float(value)
                    if value >= 0:
                        return value
                except (TypeError, ValueError):
                    pass
            parsed = parse_time_field_seconds(meta.get("duration"))
            if parsed is not None:
                return parsed
        return None

    def _current_track_duration_state(self) -> Optional[CurrentTrackState]:
        path = self.current_path
        if not path:
            return None
        length_ms = getattr(self, "_last_progress_length_ms", 0) or 0
        current_ms = getattr(self, "_last_progress_current_ms", 0) or 0
        if length_ms > 0:
            return CurrentTrackState(
                duration_seconds=length_ms / 1000.0,
                position_seconds=current_ms / 1000.0,
            )
        duration = self._queue_entry_duration_seconds(path, None)
        return CurrentTrackState(duration_seconds=duration, position_seconds=0.0)

    def _active_crossfade_state(self) -> Optional[ActiveCrossfadeState]:
        if not getattr(self, "fade_active", False):
            return None
        try:
            if self._use_builtin_player():
                outgoing = self.simple_player
                incoming = self.simple_inactive_player
                if not outgoing or not incoming:
                    return None
                outgoing_length = outgoing.get_length()
                outgoing_pos = outgoing.get_pos()
                incoming_pos = incoming.get_pos()
            elif self.active_player and self.inactive_player:
                outgoing_length = self.active_player.get_length() / 1000.0
                outgoing_pos = self.active_player.get_time() / 1000.0
                incoming_pos = self.inactive_player.get_time() / 1000.0
            else:
                return None
        except Exception:
            return None
        if outgoing_length <= 0:
            return None
        return ActiveCrossfadeState(
            outgoing_remaining_seconds=max(0.0, outgoing_length - outgoing_pos),
            incoming_elapsed_seconds=max(0.0, incoming_pos),
        )

    def _build_queue_duration_estimate(self) -> QueueDurationEstimate:
        upcoming = []
        for row, path in enumerate(self.queue):
            played = self.queue_played[row] if row < len(self.queue_played) else False
            entry = (
                self.queue_playlist_entries[row]
                if row < len(self.queue_playlist_entries) else None
            )
            unavailable = bool(entry and entry.is_missing)
            duration = self._queue_entry_duration_seconds(path, entry)
            upcoming.append(
                QueueTrackInfo(
                    duration_seconds=duration, played=played, unavailable=unavailable,
                    crossfade_eligible=is_audio(path),
                )
            )
        playback_expected = bool(getattr(self, "_playback_expected", False))
        is_paused = playback_expected and bool(
            getattr(self, "_playback_intentionally_paused", False)
        )
        current = self._current_track_duration_state() if playback_expected else None
        return estimate_queue_duration(
            upcoming=upcoming,
            current=current,
            is_paused=is_paused,
            crossfade_enabled=getattr(self, "track_transition_mode", "crossfade") == "crossfade",
            crossfade_seconds=float(getattr(self, "crossfade_seconds", 0.0) or 0.0),
            active_crossfade=self._active_crossfade_state() if playback_expected else None,
        )

    def _format_queue_finish_time(self, moment: datetime) -> str:
        qtime = QtCore.QTime(moment.hour, moment.minute)
        return QtCore.QLocale.system().toString(
            qtime, QtCore.QLocale.FormatType.ShortFormat
        )

    def _schedule_queue_duration_refresh(self, reason: str = "structural_change"):
        if getattr(self, "_closing", False):
            return
        self._queue_duration_pending_reason = reason
        timer = getattr(self, "_queue_duration_debounce_timer", None)
        if timer is not None:
            timer.start(150)

    def _refresh_queue_duration_summary_now(self, reason: Optional[str] = None):
        reason = reason or getattr(self, "_queue_duration_pending_reason", "structural_change")
        label = getattr(self, "queue_duration_summary_label", None)
        if label is None:
            return
        started = time.perf_counter()
        try:
            estimate = self._build_queue_duration_estimate()
        except Exception as ex:
            self.diagnostics.record(
                "queue", "duration_estimate_failed",
                status="failure", severity="warning",
                details={"reason": reason, "error": str(ex)},
                minimum_level="basic",
            )
            return
        finish_text = None
        if (
            estimate.finish_datetime is not None
            and not estimate.is_stopped
            and not estimate.is_paused
            and estimate.unknown_track_count == 0
        ):
            finish_text = self._format_queue_finish_time(estimate.finish_datetime)
        summary_text = format_queue_duration_summary(estimate, finish_time_text=finish_text)
        label.setText(summary_text)
        label.setAccessibleDescription(summary_text)
        label.setToolTip(format_queue_duration_tooltip(estimate))

        unknown_changed = (
            estimate.unknown_track_count
            != getattr(self, "_queue_duration_last_unknown_count", None)
        )
        if unknown_changed or reason != "position_tick":
            self.diagnostics.record(
                "queue", "duration_estimate_updated",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                details={
                    "queue_entry_count": len(self.queue),
                    "unknown_duration_count": estimate.unknown_track_count,
                    "known_remaining_seconds": round(estimate.known_remaining_seconds, 1),
                    "crossfade_overlap_seconds": round(estimate.expected_crossfade_seconds, 1),
                    "reason": reason,
                },
                minimum_level="detailed",
            )
            if estimate.unknown_track_count > 0:
                self.diagnostics.record(
                    "queue", "duration_estimate_incomplete",
                    details={
                        "unknown_duration_count": estimate.unknown_track_count,
                        "reason": reason,
                    },
                    minimum_level="basic",
                )

        finish_minute = (
            int(estimate.finish_datetime.timestamp() // 60)
            if estimate.finish_datetime is not None else None
        )
        meaningful_change = (
            estimate.is_paused != getattr(self, "_queue_duration_last_paused", None)
            or estimate.queue_empty != getattr(self, "_queue_duration_last_empty", None)
            or unknown_changed
            or (
                not estimate.is_paused and not estimate.is_stopped
                and finish_minute != getattr(self, "_queue_duration_last_finish_minute", None)
            )
        )
        if meaningful_change:
            self._announce_accessible_status(f"Up Next duration changed: {summary_text}")
        self._queue_duration_last_unknown_count = estimate.unknown_track_count
        self._queue_duration_last_paused = estimate.is_paused
        self._queue_duration_last_empty = estimate.queue_empty
        self._queue_duration_last_finish_minute = finish_minute

    def _maybe_tick_queue_duration_refresh(self):
        if not getattr(self, "_playback_expected", False):
            return
        now_monotonic = time.monotonic()
        last = getattr(self, "_queue_duration_last_tick_monotonic", 0.0)
        if now_monotonic - last < 1.0:
            return
        self._queue_duration_last_tick_monotonic = now_monotonic
        self._refresh_queue_duration_summary_now("position_tick")

    def _refresh_queue_list(
        self, keep_played_bottom=True, cached_details_only=False,
        reason="structural_change",
    ):
        diagnostic_started = time.perf_counter()
        selection_started = time.perf_counter()
        selected_path = None
        current = self.queue_list.currentItem()
        if current is not None:
            selected_path = current.data(QtCore.Qt.ItemDataRole.UserRole)
        scroll_value = self.queue_list.verticalScrollBar().value()
        selection_capture_ms = (time.perf_counter() - selection_started) * 1000.0
        self._ensure_queue_played_flags()
        if keep_played_bottom:
            self._keep_played_tracks_at_bottom()
        snapshot_started = time.perf_counter()
        queue_snapshot = list(self.queue)
        queue_played_snapshot = list(self.queue_played)
        playlist_entry_snapshot = list(self.queue_playlist_entries)
        queue_snapshot_ms = (time.perf_counter() - snapshot_started) * 1000.0
        signals_started = time.perf_counter()
        queue_signals_were_blocked = self.queue_list.blockSignals(True)
        sorting_was_enabled = self.queue_list.isSortingEnabled()
        self.queue_list.setSortingEnabled(False)
        self.queue_list.setUpdatesEnabled(False)
        signal_block_ms = (time.perf_counter() - signals_started) * 1000.0
        clear_started = time.perf_counter()
        # Real-device lifetime bug (2026-08-31 Codex audit): shut down every
        # old row's MarqueeLabel *before* QListWidget.clear() implicitly
        # deferred-deletes their wrapper widgets -- discarding
        # _queue_row_widgets below without this left each one's 20ms timer
        # free to keep ticking (and its finished sibling's ready-queued
        # timeout free to fire) during the deferred-delete window. See
        # MarqueeLabel.shutdown()'s own docstring for the full mechanism.
        for entries in self._queue_row_widgets.values():
            for entry in entries:
                self._shutdown_queue_row_marquee(entry)
        self.queue_list.clear()
        clear_ms = (time.perf_counter() - clear_started) * 1000.0
        self._queue_row_widgets = {}
        index_started = time.perf_counter()
        meta_by_path = getattr(self, "_meta_by_path", None)
        if not meta_by_path:
            meta_by_path = {
                m.get("path"): m for m in self._meta_list if m.get("path")
            }
        library_index_build_ms = (time.perf_counter() - index_started) * 1000.0
        metadata_lookup_ms = 0.0
        display_format_ms = 0.0
        detail_lookup_ms = 0.0
        tooltip_ms = 0.0
        set_data_ms = 0.0
        row_insert_ms = 0.0
        row_durations = []
        detail_cache_hits = 0
        detail_cache_misses = 0
        analysis_cache_hits = 0
        row_creation_started = time.perf_counter()
        for row, path in enumerate(queue_snapshot):
            row_started = time.perf_counter()
            stage_started = time.perf_counter()
            meta = meta_by_path.get(path)
            if not meta and is_plex_identity(path):
                # Same persistent fallback as _display_meta_for_path --
                # meta_by_path above is a snapshot of the currently
                # *browsed* tab's metadata, which a Plex queue entry can
                # easily be outside of (e.g. Local tab browsed while a
                # Plex track is queued).
                meta = getattr(self, "_plex_meta_by_path", {}).get(path)
            metadata_lookup_ms += (
                time.perf_counter() - stage_started
            ) * 1000.0
            playlist_entry = (
                playlist_entry_snapshot[row]
                if row < len(playlist_entry_snapshot) else None
            )
            missing = bool(playlist_entry and playlist_entry.is_missing)
            stage_started = time.perf_counter()
            if meta:
                title = meta.get("title") or os.path.splitext(os.path.basename(path))[0]
                artist = meta.get("artist") or ""
                label = f"{title} - {artist}" if artist else title
            else:
                label = os.path.splitext(os.path.basename(path))[0]
            if playlist_entry and playlist_entry.display_title:
                label = (
                    f"{playlist_entry.display_title} - {playlist_entry.artist}"
                    if playlist_entry.artist else playlist_entry.display_title
                )
            if missing:
                label = f"[Missing file] {label}"
            display_format_ms += (
                time.perf_counter() - stage_started
            ) * 1000.0
            played = queue_played_snapshot[row]
            if path in self.queue_detail_cache:
                detail_cache_hits += 1
            else:
                detail_cache_misses += 1
                if isinstance(
                    getattr(self, "queue_analysis_cache", {}).get(path), dict
                ):
                    analysis_cache_hits += 1
            stage_started = time.perf_counter()
            details = self._queue_track_details(
                path, cached_details_only=cached_details_only
            )
            detail_lookup_ms += (
                time.perf_counter() - stage_started
            ) * 1000.0
            item = QtWidgets.QListWidgetItem()
            stage_started = time.perf_counter()
            item.setData(QtCore.Qt.ItemDataRole.UserRole, path)
            item.setData(self._queue_played_role(), played)
            item.setData(self._queue_playlist_role(), playlist_entry)
            set_data_ms += (time.perf_counter() - stage_started) * 1000.0
            stage_started = time.perf_counter()
            item.setToolTip(
                ("Played - " if played else "")
                + label
                + (
                    f"\nOriginal path: {playlist_entry.original_path or playlist_entry.path}"
                    if missing else ""
                )
            )
            tooltip_ms += (time.perf_counter() - stage_started) * 1000.0
            item.setSizeHint(QtCore.QSize(0, 34))

            row_widget = QtWidgets.QWidget()
            row_widget.setStyleSheet(
                "QWidget { background:rgba(255,60,172,0.10); border:1px solid rgba(177,76,255,0.34); border-radius:7px; }"
                if not played else
                "QWidget { background:rgba(90,72,120,0.16); border:1px solid rgba(120,102,150,0.26); border-radius:7px; }"
            )
            layout = QtWidgets.QHBoxLayout(row_widget)
            layout.setContentsMargins(8, 3, 44, 3)
            layout.setSpacing(8)
            title_label = self._queue_title_marquee(label, played)
            if missing:
                font = title_label.font()
                font.setItalic(True)
                title_label.setFont(font)
                title_label.setAccessibleName(f"Missing file: {label}")
            layout.addWidget(title_label, 6)
            time_label = self._queue_column_label(details.get("time", "--"), played)
            bitrate_label = self._queue_column_label(details["bitrate"], played)
            layout.addWidget(time_label, 1)
            layout.addWidget(bitrate_label, 2)
            spinner = ("|", "/", "-", "\\")[self.queue_spinner_index % 4]
            pending_fields = self.queue_bpm_key_pending_fields.get(path, ())
            key_text = spinner if "key" in pending_fields else details["key"]
            bpm_text = spinner if "bpm" in pending_fields else details["bpm"]
            key_label = self._queue_column_label(key_text, played)
            bpm_label = self._queue_column_label(bpm_text, played)
            layout.addWidget(key_label, 1)
            layout.addWidget(bpm_label, 1)
            stage_started = time.perf_counter()
            self.queue_list.addItem(item)
            self.queue_list.setItemWidget(item, row_widget)
            row_insert_ms += (time.perf_counter() - stage_started) * 1000.0
            self._queue_row_widgets.setdefault(path, []).append(
                {
                    "item": item,
                    "widget": row_widget,
                    "title": title_label,
                    "time": time_label,
                    "bitrate": bitrate_label,
                    "key": key_label,
                    "bpm": bpm_label,
                }
            )
            row_durations.append(
                (time.perf_counter() - row_started) * 1000.0
            )
        restore_started = time.perf_counter()
        if selected_path:
            for row in range(self.queue_list.count()):
                item = self.queue_list.item(row)
                if item.data(QtCore.Qt.ItemDataRole.UserRole) == selected_path:
                    self.queue_list.setCurrentRow(row)
                    break
        self.queue_list.verticalScrollBar().setValue(scroll_value)
        selection_restore_ms = (time.perf_counter() - restore_started) * 1000.0
        row_creation_ms = (
            time.perf_counter() - row_creation_started
        ) * 1000.0
        repaint_started = time.perf_counter()
        self.queue_list.setUpdatesEnabled(True)
        self.queue_list.setSortingEnabled(sorting_was_enabled)
        self.queue_list.blockSignals(queue_signals_were_blocked)
        self.queue_list.viewport().update()
        repaint_schedule_ms = (
            time.perf_counter() - repaint_started
        ) * 1000.0
        duration_ms = (time.perf_counter() - diagnostic_started) * 1000.0
        self.diagnostics.record(
            "queue", "refresh_up_next",
            duration_ms=duration_ms,
            severity="warning" if duration_ms >= 30 else "info",
            details={
                "rows": len(self.queue),
                "average_row_ms": round(
                    sum(row_durations) / max(1, len(row_durations)), 3
                ),
                "maximum_row_ms": round(max(row_durations, default=0.0), 3),
                "rows_above_10_ms": sum(value > 10 for value in row_durations),
                "rows_above_50_ms": sum(value > 50 for value in row_durations),
                "rows_above_100_ms": sum(value > 100 for value in row_durations),
                "cached_details_only": cached_details_only,
                "reason": reason,
                "update_type": "full",
                "selection_capture_ms": selection_capture_ms,
                "selection_restore_ms": selection_restore_ms,
                "queue_snapshot_ms": queue_snapshot_ms,
                "library_index_build_ms": library_index_build_ms,
                "library_index_lookups": len(queue_snapshot),
                "linear_library_searches": 0,
                "metadata_cache_lookup_ms": round(metadata_lookup_ms, 3),
                "detail_cache_lookup_ms": round(detail_lookup_ms, 3),
                "display_format_ms": round(display_format_ms, 3),
                "tooltip_ms": round(tooltip_ms, 3),
                "set_data_ms": round(set_data_ms, 3),
                "row_insert_ms": round(row_insert_ms, 3),
                "detail_cache_hits": detail_cache_hits,
                "detail_cache_misses": detail_cache_misses,
                "analysis_cache_hits": analysis_cache_hits,
                "filesystem_calls": 0 if cached_details_only else None,
                "tag_reads": 0 if cached_details_only else None,
                "artwork_requests": 0,
                "rows_inserted": len(self.queue),
                "clear_rows_ms": clear_ms,
                "row_creation_ms": row_creation_ms,
                "signal_block_ms": signal_block_ms,
                "repaint_schedule_ms": repaint_schedule_ms,
            },
            minimum_level="basic" if duration_ms >= 30 else "detailed",
            rate_limit_seconds=1.0 if duration_ms >= 30 else 0,
        )
        event_loop_scheduled_at = time.perf_counter()
        QtCore.QTimer.singleShot(
            0,
            lambda started=event_loop_scheduled_at, refresh_ms=duration_ms,
            row_count=len(queue_snapshot), refresh_reason=reason:
            self._record_queue_event_loop_return(
                started, refresh_ms, row_count, refresh_reason
            ),
        )
        self._sync_mini_player()
        getattr(self, "_schedule_queue_duration_refresh", lambda *_: None)(reason)

    def _record_queue_event_loop_return(
        self, scheduled_at: float, refresh_ms: float, rows: int, reason: str
    ):
        self.diagnostics.record(
            "queue", "refresh_up_next_event_loop_return",
            duration_ms=(time.perf_counter() - scheduled_at) * 1000.0,
            details={
                "refresh_duration_ms": round(refresh_ms, 3),
                "rows": rows,
                "reason": reason,
            },
            minimum_level="detailed",
        )

    def _play_queue_item(self, item):
        row = self.queue_list.row(item)
        if row < 0 or row >= len(self.queue):
            return
        self._ensure_queue_played_flags()
        path = self.queue[row]
        if self.fade_active or self.prebuffer_active:
            self._audio_log(f"queue double-click ignored; row={row}; fade_active={self.fade_active}; prebuffer_active={self.prebuffer_active}; file={self._audio_name(path)!r}")
            return
        if row < len(self.queue_played) and self.queue_played[row]:
            self._audio_log(f"queue double-click ignored; row={row}; played=True; file={self._audio_name(path)!r}")
            return
        self._audio_log(f"queue double-click play; row={row}; file={self._audio_name(path)!r}")
        crossfade = self._crossfade_eligible_for_transition(path)
        if self._play_path_direct(path, crossfade=crossfade, immediate_crossfade=True):
            self._mark_queue_row_played(row)

    def _show_queue_menu(self, pos):
        item = self.queue_list.itemAt(pos)
        menu = QtWidgets.QMenu(self)
        menu.addAction(self.action_undo_queue_change)
        menu.addSeparator()
        action_load = menu.addAction("Load Playlist into Up Next...")
        action_save_all = menu.addAction("Save Full Up Next Playlist...")
        action_save_unplayed = menu.addAction("Save Unplayed Tracks Only...")
        menu.addSeparator()
        action_shuffle = menu.addAction("Shuffle Up Next")
        action_move_selected = menu.addAction("Move Selected Track to Top")
        action_move_selected.setEnabled(item is not None)
        action_remove_played = menu.addAction("Remove Played Tracks from Up Next")
        action_clear = menu.addAction("Clear Queue")
        action_clear_saved = menu.addAction("Clear Saved Session")
        action_remove = None
        action_find_replacement = None
        selected_paths = []
        if item:
            menu.addSeparator()
            row_for_item = self.queue_list.row(item)
            if self._queue_entry_is_missing(row_for_item):
                action_find_replacement = menu.addAction("Find Replacement...")
            action_remove = menu.addAction("Remove Selected Track")
            rows = sorted({self.queue_list.row(selected) for selected in self.queue_list.selectedItems()})
            if not rows:
                rows = [self.queue_list.row(item)]
            selected_paths = [self.queue[row] for row in rows if 0 <= row < len(self.queue)]
            normalisation_actions = self._add_normalisation_menu(menu, selected_paths)
        else:
            normalisation_actions = {}
        self._add_window_menu_options(menu)
        action = menu.exec(self.queue_list.viewport().mapToGlobal(pos))
        if self._handle_normalisation_action(action, normalisation_actions, selected_paths):
            return
        if action_find_replacement is not None and action == action_find_replacement:
            self._repair_missing_playlist_tracks(
                selected_queue_row=self.queue_list.row(item)
            )
            return
        if action == action_load:
            self._load_playlist_to_queue()
            return
        if action == action_save_all:
            self._save_queue_as_playlist(only_unplayed=False)
            return
        if action == action_save_unplayed:
            self._save_queue_as_playlist(only_unplayed=True)
            return
        if action == action_shuffle:
            self._shuffle_up_next()
            return

        if action == action_move_selected:
            idx = self.queue_list.row(item) if item is not None else -1
            self._move_queue_item_to_top(idx)
            return
        if action == action_remove_played:
            self._remove_played_queue_tracks()
            return
        if action == action_clear:
            self._clear_up_next_queue()
            return
        if action == action_clear_saved:
            confirm = QtWidgets.QMessageBox.question(
                self,
                "Clear Saved Session",
                "Delete the saved Up Next session?\n\n"
                "The queue currently shown will not be cleared. It can be saved again "
                "automatically when the application closes.",
            )
            if confirm == QtWidgets.QMessageBox.StandardButton.Yes:
                self._clear_saved_session()
            return
        if action_remove is not None and action == action_remove:
            row = self.queue_list.row(item)
            if 0 <= row < len(self.queue):
                with capture_queue_undo(self, "remove"):
                    self.queue.pop(row)
                    self.queue_playlist_entries.pop(row)
                    if row < len(self.queue_played):
                        self.queue_played.pop(row)
                    self._remove_queue_row_widget(
                        row, reason="track_removed"
                    )
                self._schedule_session_save()

    def _show_now_playing_menu(self, pos):
        if not self.current_path:
            return
        menu = QtWidgets.QMenu(self)
        actions = self._add_normalisation_menu(menu, [self.current_path])
        self._add_window_menu_options(menu)
        action = menu.exec(self.now_playing.mapToGlobal(pos))
        self._handle_normalisation_action(action, actions, [self.current_path])

    def _sync_queue_from_list(self, *args):
        new_queue = []
        new_played = []
        new_entries = []
        for i in range(self.queue_list.count()):
            item = self.queue_list.item(i)
            path = item.data(QtCore.Qt.ItemDataRole.UserRole)
            played = bool(item.data(self._queue_played_role()))
            playlist_entry = item.data(self._queue_playlist_role())
            if isinstance(path, str):
                new_queue.append(path)
                new_played.append(played)
                new_entries.append(
                    playlist_entry
                    if isinstance(playlist_entry, PlaylistEntry) else None
                )
        if len(new_queue) == self.queue_list.count():
            if new_queue != self.queue:
                # A completed drag-and-drop gesture: capture the pre-drop
                # order for undo before overwriting it. Qt has already
                # physically reordered queue_list by the time rowsMoved
                # fires, so self.queue (still the old order here) is the
                # only source left for the "before" state.
                previous_snapshot = self._queue_undo_snapshot
                snapshot_started = time.perf_counter()
                self._queue_undo_snapshot = snapshot_queue_state(self, "reorder")
                self.queue = new_queue
                self.queue_played = new_played
                self.queue_playlist_entries = new_entries
                self._keep_played_tracks_at_bottom()
                # A genuine reorder changes what's at any given row -- a
                # preload/candidate selection recorded by (epoch, row,
                # path) before this drag-and-drop must not be trusted to
                # still describe the same logical item afterward (see
                # video_dual_transition.py's SecondaryIdentity docstring).
                self._queue_mutation_epoch = getattr(self, "_queue_mutation_epoch", 0) + 1
                record_undo_snapshot_diagnostics(
                    self, "reorder", snapshot_started, previous_snapshot
                )
                self._update_undo_action_state()
            else:
                self.queue = new_queue
                self.queue_played = new_played
                self.queue_playlist_entries = new_entries
                self._keep_played_tracks_at_bottom()
            self._schedule_session_save()
            getattr(self, "_schedule_queue_duration_refresh", lambda *_: None)("queue_reordered")

    def _load_playlist_to_queue(self):
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Load playlist into Up Next",
            "",
            "Playlists (*.m3u *.m3u8);;All Files (*)",
        )
        if not filename:
            return
        if self._playlist_load_worker is not None or getattr(self, "_closing", False):
            return
        self.statusBar().showMessage("Loading playlist…")
        worker = PlaylistLoadWorker(filename)
        self._playlist_load_worker = worker
        registry_token = self._worker_registry.register(
            "playlist_load", thread=worker, wait_ms=2000,
        )
        worker.completed.connect(self._playlist_loaded)
        worker.failed.connect(self._playlist_load_failed)
        worker.finished.connect(
            lambda w=worker, t=registry_token: self._on_simple_worker_finished("_playlist_load_worker", w, t)
        )
        worker.start()

    def _playlist_load_failed(self, message):
        if getattr(self, "_closing", False):
            return
        QtWidgets.QMessageBox.warning(
            self, "Load Playlist", f"Could not load playlist:\n{message}"
        )

    def _playlist_loaded(self, filename, entries, counts, duration_ms):
        if getattr(self, "_closing", False):
            return
        if not entries:
            QtWidgets.QMessageBox.information(
                self, "Load Playlist", "The playlist contained no track entries."
            )
            return

        def _playlist_insert_fn(final_entries):
            insert_at = self._first_played_queue_row()
            paths = [entry.resolved_path or entry.path for entry in final_entries]
            self.queue[insert_at:insert_at] = paths
            self.queue_played[insert_at:insert_at] = [False] * len(final_entries)
            self.queue_playlist_entries[insert_at:insert_at] = final_entries
            # Always the full, original playlist load -- never the
            # dedup-filtered subset -- so a later "Save Repaired Playlist"
            # can't silently truncate the user's M3U file.
            self._loaded_playlist_entries = list(entries)
            self._loaded_playlist_filename = filename

        outcome = self._add_to_queue_with_dedup_guard(
            entries,
            path_of=lambda entry: entry.resolved_path or entry.path,
            insert_fn=_playlist_insert_fn,
            undo_action="playlist_load",
            announce=False,
        )
        if outcome.cancelled:
            return
        self.diagnostics.record(
            "playlist", "playlist_loaded",
            duration_ms=duration_ms,
            details={
                **counts,
                **self.diagnostics.path_details(filename),
            },
            minimum_level="basic",
        )
        if outcome.duplicate_count == 0:
            playlist_message = f"Playlist loaded with {outcome.added_count} tracks."
        elif outcome.added_count < len(entries):
            dupe_word = "duplicate" if outcome.duplicate_count == 1 else "duplicates"
            playlist_message = (
                f"{outcome.added_count} tracks added from playlist; "
                f"{outcome.duplicate_count} {dupe_word} skipped."
            )
        else:
            dupe_word = "duplicate" if outcome.duplicate_count == 1 else "duplicates"
            playlist_message = (
                f"Playlist loaded with {outcome.added_count} tracks, "
                f"including {outcome.duplicate_count} {dupe_word}."
            )
        if counts["missing"]:
            playlist_message += f" {counts['missing']} tracks could not be found."
            self._announce_accessible_status(playlist_message, timeout=10000)
        else:
            self._announce_accessible_status(playlist_message, timeout=5000)

    def _on_queue_files_dropped(self, dropped_paths):
        if self._queue_drop_worker is not None or getattr(self, "_closing", False):
            return
        loose_files = [
            p for p in dropped_paths
            if os.path.isfile(p) and is_supported_media(p)
        ]
        folders = [p for p in dropped_paths if os.path.isdir(p)]
        if not folders:
            self._finish_queue_drop(loose_files)
            return
        self.statusBar().showMessage("Scanning dropped folder(s)…")
        worker = QueueFolderDropWorker(folders, loose_files)
        self._queue_drop_worker = worker
        registry_token = self._worker_registry.register(
            "queue_drop", thread=worker, wait_ms=2000,
        )
        worker.completed.connect(self._finish_queue_drop)
        worker.failed.connect(
            lambda msg: None if getattr(self, "_closing", False) else self.statusBar().showMessage(
                f"Could not read dropped folder: {msg}", 6000
            )
        )
        worker.finished.connect(
            lambda w=worker, t=registry_token: self._on_simple_worker_finished("_queue_drop_worker", w, t)
        )
        worker.start()

    def _finish_queue_drop(self, paths):
        if getattr(self, "_closing", False):
            return
        self._add_to_queue_with_dedup_guard(paths)

    def _repair_missing_playlist_tracks(
        self, checked=False, selected_queue_row=None
    ):
        if isinstance(selected_queue_row, bool):
            selected_queue_row = None
        missing = []
        for row, entry in enumerate(self.queue_playlist_entries):
            if entry is None or not entry.is_missing:
                continue
            if selected_queue_row is None or row == selected_queue_row:
                missing.append((row, entry))
        if not missing:
            QtWidgets.QMessageBox.information(
                self,
                "Repair Missing Playlist Tracks",
                "There are no missing playlist tracks to repair.",
            )
            return
        self.diagnostics.record(
            "playlist", "repair_dialog_opened",
            details={"missing": len(missing)},
            minimum_level="basic",
        )
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Repair Missing Playlist Tracks")
        dialog.resize(980, min(650, 180 + len(missing) * 48))
        layout = QtWidgets.QVBoxLayout(dialog)
        heading = QtWidgets.QLabel(
            f"{len(missing)} missing "
            f"{'track' if len(missing) == 1 else 'tracks'}"
        )
        heading.setAccessibleName("Missing playlist track count")
        layout.addWidget(heading)
        table = QtWidgets.QTableWidget(len(missing), 4)
        table.setHorizontalHeaderLabels(
            ["Original track", "Suggested replacement", "Confidence", "Browse"]
        )
        table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        table.setAccessibleName("Missing playlist tracks and replacements")
        table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        replacements = {}
        confidence_counts = {"Exact": 0, "Strong": 0, "Possible": 0}
        for display_row, (queue_row, entry) in enumerate(missing):
            original_label = (
                f"{entry.artist} - {entry.display_title}"
                if entry.artist and entry.display_title
                else entry.display_title
                or os.path.basename(entry.original_path or entry.path)
            )
            original_item = QtWidgets.QTableWidgetItem(original_label)
            original_item.setToolTip(entry.original_path or entry.path)
            table.setItem(display_row, 0, original_item)
            suggestion = suggest_replacement(entry, self._full_meta_list)
            confidence_counts[suggestion.confidence] = (
                confidence_counts.get(suggestion.confidence, 0) + 1
            )
            combo = QtWidgets.QComboBox()
            combo.setAccessibleName(
                f"Replacement for {original_label}"
            )
            combo.addItem("Ignore", None)
            if suggestion.path:
                combo.addItem(
                    f"{os.path.basename(suggestion.path)} — {suggestion.reason}",
                    suggestion.path,
                )
                if suggestion.preselected:
                    combo.setCurrentIndex(1)
                    replacements[queue_row] = suggestion.path
            combo.currentIndexChanged.connect(
                lambda _index, qr=queue_row, widget=combo: (
                    replacements.__setitem__(qr, widget.currentData())
                    if widget.currentData()
                    else replacements.pop(qr, None)
                )
            )
            table.setCellWidget(display_row, 1, combo)
            confidence_item = QtWidgets.QTableWidgetItem(
                f"{suggestion.confidence}: {suggestion.reason}"
            )
            table.setItem(display_row, 2, confidence_item)
            browse = QtWidgets.QPushButton("Browse...")
            browse.setAccessibleName(
                f"Browse for replacement for {original_label}"
            )

            def browse_for_replacement(
                _checked=False, qr=queue_row, ent=entry, widget=combo
            ):
                original_parent = os.path.dirname(ent.original_path or ent.path)
                start = (
                    original_parent
                    if original_parent and os.path.isdir(original_parent)
                    else os.path.dirname(self._loaded_playlist_filename or "")
                )
                selected, _ = QtWidgets.QFileDialog.getOpenFileName(
                    dialog,
                    "Select replacement track",
                    start,
                    f"{AUDIO_FILE_FILTER};;"
                    "All files (*)",
                )
                if not selected:
                    return
                if not os.path.isfile(selected):
                    QtWidgets.QMessageBox.warning(
                        dialog, "Invalid Replacement",
                        "Select an existing music file, not a folder."
                    )
                    return
                widget.addItem(os.path.basename(selected), selected)
                widget.setCurrentIndex(widget.count() - 1)

            browse.clicked.connect(browse_for_replacement)
            table.setCellWidget(display_row, 3, browse)
        layout.addWidget(table)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Apply
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(
            QtWidgets.QDialogButtonBox.StandardButton.Apply
        ).setText("Apply Repairs")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        repairs = 0
        manual = 0
        repaired_paths = []
        for queue_row, replacement in replacements.items():
            if not (0 <= queue_row < len(self.queue_playlist_entries)):
                continue
            entry = self.queue_playlist_entries[queue_row]
            if entry is None or not entry.is_missing:
                continue
            repaired = entry.with_replacement(replacement)
            self.queue_playlist_entries[queue_row] = repaired
            self.queue[queue_row] = repaired.resolved_path
            repairs += 1
            repaired_paths.append(repaired.resolved_path)
            suggestion = suggest_replacement(entry, self._full_meta_list)
            manual += int(replacement != suggestion.path)
            for index, loaded in enumerate(self._loaded_playlist_entries):
                if loaded is entry:
                    self._loaded_playlist_entries[index] = repaired
                    break
        if not repairs:
            return
        # Replaces the path at specific rows -- a preload/candidate
        # selection recorded by (epoch, row, path) before the repair must
        # not be trusted to still describe the same logical item
        # afterward (see video_dual_transition.py's SecondaryIdentity
        # docstring).
        self._queue_mutation_epoch = getattr(self, "_queue_mutation_epoch", 0) + 1
        # v1.0.67: cached_details_only=True -- a repaired row's replacement
        # path may never have had its bitrate/time/key/bpm analysed before,
        # so this must not fall back to a synchronous Mutagen read here.
        # Matches the same cached-render + targeted-background-fill pattern
        # already used for newly added tracks ("tracks_added" above).
        self._refresh_queue_list(
            keep_played_bottom=False, cached_details_only=True,
            reason="playlist_repairs_applied",
        )
        self._request_queue_analysis_for_paths(repaired_paths)
        unresolved = sum(
            bool(entry and entry.is_missing)
            for entry in self.queue_playlist_entries
        )
        self.diagnostics.record(
            "playlist", "playlist_repairs_applied",
            details={
                "repairs_applied": repairs,
                "manual_replacements": manual,
                "entries_left_unresolved": unresolved,
                **confidence_counts,
            },
            minimum_level="basic",
        )
        self._schedule_session_save()
        if self._loaded_playlist_filename:
            answer = QtWidgets.QMessageBox.question(
                self,
                "Save Repaired Playlist",
                "Repairs were applied. Save them to the original playlist?\n\n"
                "A single .bak backup will be created first.",
            )
            if answer == QtWidgets.QMessageBox.StandardButton.Yes:
                try:
                    save_m3u(
                        self._loaded_playlist_filename,
                        self._loaded_playlist_entries,
                    )
                except Exception as ex:
                    self.diagnostics.record(
                        "playlist", "playlist_save_failed",
                        status="failure", severity="warning",
                        details={"exception": str(ex)},
                        minimum_level="basic",
                    )
                    QtWidgets.QMessageBox.warning(
                        self, "Save Playlist",
                        f"Could not save repaired playlist:\n{ex}",
                    )
                else:
                    self.diagnostics.record(
                        "playlist", "playlist_save_succeeded",
                        details={"entries": len(self._loaded_playlist_entries)},
                        minimum_level="basic",
                    )
                    self.statusBar().showMessage(
                        "Repaired playlist saved; backup created", 6000
                    )

    def _save_queue_as_playlist(self, only_unplayed: bool = False):
        # Save the displayed Up Next order. The default saves played and unplayed tracks.
        self._ensure_queue_played_flags()
        entries = [
            (path, played, self.queue_playlist_entries[index])
            for index, (path, played) in enumerate(
                zip(self.queue, self.queue_played)
            )
            if not only_unplayed or not played
        ]
        if not entries:
            message = "There are no unplayed tracks in Up Next to save." if only_unplayed else "There are no tracks in Up Next to save."
            QtWidgets.QMessageBox.information(self, "Save Playlist", message)
            return
        default_name = "Up Next Unplayed.m3u8" if only_unplayed else "Up Next.m3u8"
        filename, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Save Up Next as playlist",
            default_name,
            "M3U8 Playlist (*.m3u8);;M3U Playlist (*.m3u);;All Files (*)",
        )
        if not filename:
            return
        if not filename.lower().endswith((".m3u8", ".m3u")):
            filename += ".m3u8"
        try:
            playlist_entries = [
                entry
                if entry is not None
                else PlaylistEntry(
                    path=path,
                    resolved_path=path,
                    original_path=path,
                    is_missing=False,
                )
                for path, _, entry in entries
            ]
            save_m3u(filename, playlist_entries)
        except Exception as ex:
            self.diagnostics.record(
                "playlist", "playlist_save_failed",
                status="failure", severity="warning",
                details={"exception": str(ex)},
                minimum_level="basic",
            )
            QtWidgets.QMessageBox.warning(self, "Save Playlist", f"Could not save playlist:\n{ex}")
            return
        self.diagnostics.record(
            "playlist", "playlist_save_succeeded",
            details={"entries": len(playlist_entries)},
            minimum_level="basic",
        )
        QtWidgets.QMessageBox.information(self, "Save Playlist", "Up Next playlist saved.")

    def _reset_progress(self):
        # Note: this only resets the displayed position/label. It is called
        # both on a genuine new track (from _play_path_direct, right after
        # _activate_track_ui) and on crossfade completion for the *same*
        # track (_finish_crossfade / the BASS/miniaudio equivalent), so it
        # must NOT clear the waveform -- only _activate_track_ui's
        # set_placeholder() call does that, on an actual track change.
        self._last_progress_length_ms = 0
        self._last_progress_current_ms = 0
        self.slider_progress.blockSignals(True)
        self.slider_progress.setValue(0)
        self.slider_progress.blockSignals(False)
        self.label_remaining.setText("-0:00")

    def _update_progress(self, current_ms: int, length_ms: int):
        if length_ms <= 0:
            return
        self._last_progress_length_ms = int(length_ms)
        self._last_progress_current_ms = int(current_ms)
        if self.waveform_seekbar is not None:
            self.waveform_seekbar.set_position_seconds(current_ms / 1000.0, length_ms / 1000.0)
        else:
            ratio = max(0.0, min(1.0, current_ms / float(length_ms)))
            slider_val = int(ratio * self.slider_progress.maximum())
            self.slider_progress.blockSignals(True)
            self.slider_progress.setValue(slider_val)
            self.slider_progress.blockSignals(False)
        remaining_sec = max(0, int((length_ms - current_ms) / 1000))
        self.label_remaining.setText(f"-{self._format_duration(remaining_sec)}")
        self._sync_mini_player()

    def _progress_press(self):
        self.scrubbing = True

    def _progress_release(self):
        # handle seeking for either backend
        if self._current_media_type == MediaType.VIDEO:
            duration = self._video_backend.duration_ms()
            if duration > 0:
                ratio = self.slider_progress.value() / float(self.slider_progress.maximum())
                self._video_backend.seek(int(ratio * duration))
            self.scrubbing = False
            return
        if getattr(self, "cast_active", False):
            snapshot = self.cast_controller.snapshot()
            length_s = float(snapshot.get("duration", 0.0) or 0.0)
            if length_s > 0:
                ratio = self.slider_progress.value() / float(
                    self.slider_progress.maximum()
                )
                self.cast_controller.seek(ratio * length_s)
            self.scrubbing = False
            return
        if self._use_builtin_player():
            length_s = self.simple_player.get_length()
            if length_s > 0:
                ratio = self.slider_progress.value() / float(self.slider_progress.maximum())
                target_s = ratio * length_s
                self.simple_player.seek(target_s)
                self._audio_log(f"backend={self._backend_label().lower()} seek; target={target_s:.2f}s")
        elif not self.active_player:
            self.scrubbing = False
            return
        else:
            length = self.active_player.get_length()
            if length > 0:
                ratio = self.slider_progress.value() / float(self.slider_progress.maximum())
                target = int(ratio * length)
                self.active_player.set_time(target)
                self._audio_log(f"backend=vlc seek; target={target / 1000.0:.2f}s")
        self.scrubbing = False
        position = self._player_clock_s()
        if self._current_media_type == MediaType.KARAOKE:
            self.diagnostics.record(
                "playback", "karaoke_seek",
                details={"position_seconds": round(float(position or 0.0), 3)},
                minimum_level="detailed",
            )
        self._arm_playback_watchdog(position or 0.0)

    def _progress_change(self, value: int):
        if not self.scrubbing:
            return
        if self._use_builtin_player():
            length_s = self.simple_player.get_length()
            if length_s > 0:
                ratio = value / float(self.slider_progress.maximum())
                target_s = ratio * length_s
                remaining_sec = max(0, int(length_s - target_s))
                self.label_remaining.setText(f"-{self._format_duration(remaining_sec)}")
            return
        if not self.active_player:
            return
        length = self.active_player.get_length()
        if length > 0:
            ratio = value / float(self.slider_progress.maximum())
            target = int(ratio * length)
            remaining_sec = max(0, int((length - target) / 1000))
            self.label_remaining.setText(f"-{self._format_duration(remaining_sec)}")

    def _fade_tick(self):
        if self._mixed_transition_state == "active":
            self._mixed_transition_tick()
            return
        if not self.fade_active:
            return
        elapsed = time.time() - self.fade_start
        duration = self.crossfade_seconds
        t = 1.0 if duration <= 0 else min(1.0, elapsed / duration)
        # Keep the outgoing track in step with the incoming one: as the new
        # track rises, the old one drops by the same amount so it does not hang loud.
        in_scale = t ** 0.5
        out_scale = 1.0 - in_scale
        if self._use_builtin_player() and self.simple_inactive_player:
            if self._use_bass_backend():
                # Keep a UI-timer volume ramp as a fallback/guard. Some packaged
                # BASS setups do not audibly honour ChannelSlideAttribute, so the
                # manual ramp prevents the outgoing track from cutting off.
                if self.simple_player:
                    self.simple_player.set_volume(combine_volume(self.master_volume / 100.0, self._active_normalisation_gain, out_scale * self._sleep_timer_gain))
                self.simple_inactive_player.set_volume(combine_volume(self.master_volume / 100.0, self._inactive_normalisation_gain, in_scale * self._sleep_timer_gain))
                if t >= 1.0:
                    self._finish_miniaudio_crossfade()
                return
            if self.simple_player:
                self.simple_player.set_volume(combine_volume(self.master_volume / 100.0, self._active_normalisation_gain, out_scale * self._sleep_timer_gain))
            self.simple_inactive_player.set_volume(combine_volume(self.master_volume / 100.0, self._inactive_normalisation_gain, in_scale * self._sleep_timer_gain))
            if t >= 1.0:
                self._finish_miniaudio_crossfade()
            return
        self._set_volume(self.active_player, out_scale)
        self._set_volume(self.inactive_player, in_scale)
        if t >= 1.0:
            self._finish_crossfade()

    def _reapply_master_volume(self):
        if self.active_player:
            self._set_volume(self.active_player, 1.0)

    def _begin_fade(self):
        if not self.prebuffer_active:
            return
        if self.inactive_player and not self.inactive_player.is_playing():
            if self.fade_waits < 20:
                self.fade_waits += 1
                QtCore.QTimer.singleShot(100, self._begin_fade)
                return
        self.fade_active = True
        self.fade_start = time.time()

    def _player_clock_s(self):
        """Current playback position in seconds for whichever backend is live.
        Returns None if nothing is playing."""
        if getattr(self, "cast_active", False):
            # Audio plays on the Cast device, not through any local
            # backend below -- none of them are actually running, so
            # falling through to those branches always reported "nothing
            # playing" and left the visualiser idle for the whole cast
            # session, even though _cast_play_path() already runs the
            # track through _activate_track_ui() the same as local
            # playback, so the local analyzer has real precomputed levels
            # ready -- it just never had a valid time to look them up at.
            # Read the clock from the Cast connection's own reported
            # position instead. Video is never cast (_play_video_path_direct
            # forces local output first), so there's no ordering conflict
            # with the video branch below.
            try:
                snapshot = self.cast_controller.snapshot()
            except Exception:
                return None
            if snapshot.get("state") != "playing":
                return None
            position = float(snapshot.get("position", 0.0) or 0.0)
            return position if position > 0 else None
        if self._current_media_type == MediaType.VIDEO:
            if not self._video_backend.is_playing():
                return None
            return self._video_backend.position_ms() / 1000.0
        if self._use_builtin_player():
            try:
                player = self.simple_player
                if self.fade_active and self.simple_inactive_player:
                    player = self.simple_inactive_player
                if not player or not player.is_playing():
                    return None
                pos = player.get_pos()
                if pos is not None:
                    return float(pos)
            except Exception:
                return None
            return None
        if self.active_player:
            try:
                player = self.active_player
                if (self.fade_active or self.prebuffer_active) and self.inactive_player:
                    player = self.inactive_player
                if not player.is_playing():
                    return None
                ms = player.get_time()
                return (ms / 1000.0) if ms and ms > 0 else None
            except Exception:
                return None
        return None

    def _reset_analyzer_clock(self):
        self._clock_anchor = None
        self._last_clock_s = 0.0
        self._last_analysis_t = 0.0

    def _play_and_log(self, path: str):
        """Start logging the next play of this track, then play it."""
        backend = "miniaudio" if self._use_builtin_player() else "vlc"
        try:
            self.viz_logger.start(
                path, backend,
                analyzer=self.analyzer,
                latency_ms=self.analyzer_time_offset_ms,
            )
            self._log(f"Visualiser logging -> {self.viz_logger.path}")
        except Exception:
            pass
        self.play_path(path, crossfade=False)

    def _analyzer_tick(self):
        if getattr(self, "_closing", False):
            return
        if self._library_apply_started:
            self._library_apply_timer_ticks += 1
        if not self.analyzer:
            return
        raw_s = self._player_clock_s()
        if raw_s is None or raw_s <= 0:
            self._clock_anchor = None
            return

        # VLC's get_time() only updates ~3-4x/sec, but we redraw at ~60fps.
        # Without help that means ~16 identical frames then a jump -- the
        # stutter seen in the logs. So we interpolate: when the raw clock
        # hasn't moved, advance our estimate using elapsed wall time; when it
        # jumps, re-anchor (and snap if it drifted too far, e.g. after a seek).
        now = time.time()
        anchor = getattr(self, "_clock_anchor", None)
        if anchor is None or raw_s != anchor[0]:
            # raw clock advanced (or first sample): re-anchor
            self._clock_anchor = (raw_s, now)
            est_s = raw_s
        else:
            # raw clock unchanged: extrapolate from the anchor
            est_s = anchor[0] + (now - anchor[1])
            # don't let the estimate run more than one update-interval ahead
            est_s = min(est_s, raw_s + 0.30)

        t = est_s + (self.analyzer_time_offset_ms / 1000.0)
        self._last_clock_s = est_s
        self._last_analysis_t = t

        # Phase C2.1 acceptance defect: the visualiser SOURCE must be
        # chosen before testing whether the offline AnalyzerWorker happens
        # to be running -- AnalyzerWorker is a persistent QThread started
        # at application startup (see _startup_restore_library), so
        # analyzer_worker.isRunning() is True for virtually the entire
        # real session. Checking it first (as this used to) made the Plex
        # live-BASS-FFT branch below effectively unreachable in
        # production: isolated tests passed (their fixtures never start a
        # real analyzer_worker) while the real Plex visualiser stayed
        # dead. Live-Plex-BASS is now decided first and, if true, is the
        # only branch taken -- AnalyzerWorker is never stopped or
        # restarted to make room for it, and nothing here decodes the
        # Plex stream a second time (see the branch body's own comment).
        live_plex_bass = (
            not getattr(self, "cast_active", False)
            and self._current_media_type == MediaType.AUDIO
            and is_plex_identity(self.current_path)
            and self._use_bass_backend()
        )
        if live_plex_bass:
            # Stage 3A: Plex audio has no local file for AudioAnalyzer's
            # offline whole-file mel-spectrogram decode below to read --
            # it's a remote HTTP stream, and downloading/decoding it
            # again just for the visualiser would be exactly the "decode
            # the Plex stream a second time" this must not do. BASS
            # already has this channel's audio decoded into its own
            # buffer the moment playback is running -- BASS_ChannelGetData
            # reads straight from that, Local or Plex alike, no extra
            # network/decode work. See BassPlayer.get_fft_levels(). Local
            # BASS playback's own visualiser is untouched -- this branch
            # is only ever reached for a Plex identity.
            levels = self.simple_player.get_fft_levels(self.analyzer.bars) if self.simple_player else None
            if levels is not None:
                self.beat.setLevels(levels)
                if self.party_mode is not None:
                    self.party_mode.push_levels(levels)
        elif self.analyzer_worker and self.analyzer_worker.isRunning():
            try:
                self.analyzer_worker.update_time.emit(t)
            except Exception:
                return
        else:
            levels = self.analyzer.get_levels(t)
            if levels is not None:
                self.beat.setLevels(levels)
                if self.party_mode is not None:
                    self.party_mode.push_levels(levels)
                if self.viz_logger.active:
                    self.viz_logger.log_frame(
                        est_s, t, self.analyzer.last_rms_db, levels)

    def _on_analyzer_result(self, path: str, levels: List[float]):
        if getattr(self, "_closing", False) or (path and path != self.current_path):
            return
        self.beat.setLevels(levels)
        if self.party_mode is not None:
            self.party_mode.push_levels(levels)
        self._set_analyzer_mode("")
        if self.viz_logger.active:
            clock = getattr(self, "_last_clock_s", 0.0)
            ana_t = getattr(self, "_last_analysis_t", clock)
            rms = None
            try:
                rms = self.analyzer_worker.last_rms_db
            except Exception:
                rms = None
            self.viz_logger.log_frame(clock, ana_t, rms, levels)

    def _on_analysis_busy(self, path: str, busy: bool):
        # This state also gates BPM/key work. A crash captured on 2026-08-09
        # showed SciPy/Librosa key estimation running concurrently with this
        # worker's NumPy FFT. Resume deferred work shortly after the visualiser
        # worker becomes idle; the small delay also closes the old-track/new-
        # track signal handover window during rapid selection changes.
        if getattr(self, "_closing", False):
            return
        self._visualiser_analysis_busy = bool(busy)
        if not busy and getattr(self, "_deferred_bpm_key_paths", None):
            QtCore.QTimer.singleShot(100, self._resume_deferred_queue_analysis)
        if path and path != self.current_path:
            return
        # While the track is being analysed, let the visualiser show its
        # idle shimmer; flag it so the beat widget can tint differently.
        try:
            self.beat.set_analyzing(busy)
        except Exception:
            pass

    def _set_analyzer_mode(self, mode: str):
        if getattr(self, "_analyzer_mode", None) == mode:
            return
        self._analyzer_mode = mode
        self.setWindowTitle(APP_TITLE)

    def _queue_search(self, text: str):
        self._search_pending_text = text
        self._library_search_generation += 1
        generation = self._library_search_generation
        self._cancel_pending_library_apply()
        self.search_timer.stop()
        if not (text or "").strip():
            QtCore.QTimer.singleShot(
                0,
                lambda: (
                    self._apply_search_pending()
                    if generation == self._library_search_generation and not getattr(self, "_closing", False)
                    else None
                ),
            )
        else:
            self.search_timer.start()

    def _apply_search_pending(self):
        self._apply_search(self._search_pending_text)

    def _tree_build_tick(self):
        if (
            self._library_apply_generation is not None
            and self._library_apply_generation != self._library_search_generation
        ):
            self._cancel_pending_library_apply()
            return
        try:
            if not self._build_queue:
                self._build_timer.stop()
                try:
                    self.beat.setPaused(False)
                except Exception:
                    pass
                self._schedule_library_apply_finish()
                return
            chunk_started = time.perf_counter()
            added = 0
            signals_were_blocked = self.tree_tracks.blockSignals(True)
            self.tree_tracks.setUpdatesEnabled(False)
            try:
                while self._build_queue and added < self._library_apply_chunk_size:
                    _, album, display_artist, items = self._build_queue.popleft()
                    album_label = group_row_label(display_artist, album)
                    album_item = QtWidgets.QTreeWidgetItem([album_label])
                    album_key = f"{display_artist}::{album}"
                    items.sort(key=lambda t: (t[0], t[1], t[2].lower()))
                    album_item.setData(0, QtCore.Qt.ItemDataRole.UserRole, {
                        "album": album, "artist": display_artist, "key": album_key,
                        "items": items, "populated": False,
                    })
                    self.album_item_by_key[album_key] = album_item
                    self.tree_tracks.addTopLevelItem(album_item)
                    for disc_no, track_no, title, artist, _, path in items:
                        self.album_key_by_path[path] = album_key
                        self._build_playlist.append(path)
                    if items:
                        placeholder = QtWidgets.QTreeWidgetItem(["Loading tracks..."])
                        placeholder.setData(0, QtCore.Qt.ItemDataRole.UserRole, {"placeholder": True})
                        album_item.addChild(placeholder)
                    album_item.setExpanded(False)
                    added += 1
                    if library_apply_chunk_complete(
                        added,
                        time.perf_counter() - chunk_started,
                        self._library_apply_chunk_size,
                        self._library_apply_time_budget,
                    ):
                        break
            finally:
                self.tree_tracks.setUpdatesEnabled(True)
                self.tree_tracks.blockSignals(signals_were_blocked)
                self.tree_tracks.viewport().update()
            chunk_ms = (time.perf_counter() - chunk_started) * 1000.0
            self._library_apply_chunks += 1
            self._library_apply_item_ms += chunk_ms
            self._library_apply_longest_chunk_ms = max(
                self._library_apply_longest_chunk_ms, chunk_ms
            )
            if chunk_ms >= 10 or self.diagnostics.level_at_least("developer"):
                self.diagnostics.record(
                    "library", "tree_chunk_insert",
                    generation=self._library_apply_generation,
                    duration_ms=chunk_ms,
                    severity="warning" if chunk_ms >= 30 else "info",
                    details={
                        "albums_added": added,
                        "remaining_albums": len(self._build_queue),
                        "chunk": self._library_apply_chunks,
                    },
                    minimum_level="detailed",
                    rate_limit_seconds=0.25 if chunk_ms >= 30 else 0,
                )
        except Exception as ex:
            detail = traceback.format_exc()
            was_starting = not self._startup_ready_emitted
            self._cancel_pending_library_apply()
            self._log(f"Library result build failed: {ex}\n{detail}")
            if was_starting:
                self._startup_warning = (
                    "The saved music library could not be displayed. "
                    "Bills Music Player opened without it; use Rescan Library "
                    "to rebuild the library."
                )
                self._emit_startup_ready()

    def _on_album_expanded(self, item):
        self._populate_album_item(item, reason="user")

    def _prepare_album_populate_entries(self, album_item, data):
        items = list(data.get("items") or [])
        album_item.takeChildren()
        disc_map: Dict[int, List[Tuple[int, str, str, str, Optional[bool]]]] = {}
        for disc_no, track_no, title, artist, _, path in items:
            meta = self._meta_by_path.get(path) or {}
            cached_lyrics = meta.get("has_synced_lyrics")
            if not isinstance(cached_lyrics, bool):
                cached_lyrics = None
            disc_map.setdefault(disc_no, []).append(
                (track_no, title, artist, path, cached_lyrics)
            )
        entries = []
        for disc_no in sorted(disc_map.keys()):
            disc_item = album_item
            if len(disc_map) > 1:
                disc_label = f"CD {disc_no}" if disc_no > 0 else "CD"
                disc_item = QtWidgets.QTreeWidgetItem([disc_label])
                album_item.addChild(disc_item)
            tracks_in_disc = disc_map[disc_no]
            tracks_in_disc.sort(key=lambda t: (t[0], t[1].lower()))
            for track_no, title, artist, path, cached_lyrics in tracks_in_disc:
                entries.append(
                    (
                        disc_item, track_no, title, artist, path,
                        cached_lyrics,
                    )
                )
        return entries

    def _add_album_track_item(
        self, disc_item, track_no: int, title: str, artist: str,
        path: str, cached_lyrics: Optional[bool],
    ):
        label_started = time.perf_counter()
        label = self._track_display_label(
            track_no, title, artist, bool(cached_lyrics)
        )
        label_ms = (time.perf_counter() - label_started) * 1000.0
        item_started = time.perf_counter()
        track_item = QtWidgets.QTreeWidgetItem([label])
        track_item.setData(0, QtCore.Qt.ItemDataRole.UserRole, path)
        disc_item.addChild(track_item)
        self.tree_item_by_path[path] = track_item
        item_ms = (time.perf_counter() - item_started) * 1000.0
        return label_ms, item_ms, cached_lyrics is not None

    def _populate_album_item(
        self, album_item, immediate: bool = False, reason: str = "user",
    ):
        data = album_item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict) or "album" not in data or data.get("populated"):
            return
        if data.get("populating") and not immediate:
            return
        if immediate:
            self._populate_queue = [job for job in self._populate_queue if job.get("album_item") is not album_item]
            if not self._populate_queue:
                self._populate_timer.stop()
        population_started = time.perf_counter()
        entries = self._prepare_album_populate_entries(album_item, data)
        if immediate:
            label_ms = 0.0
            item_ms = 0.0
            cache_hits = 0
            for (
                disc_item, track_no, title, artist, path, cached_lyrics
            ) in entries:
                label_elapsed, item_elapsed, known = self._add_album_track_item(
                    disc_item, track_no, title, artist, path, cached_lyrics
                )
                label_ms += label_elapsed
                item_ms += item_elapsed
                cache_hits += int(known)
            data.pop("populating", None)
            data["populated"] = True
            album_item.setData(0, QtCore.Qt.ItemDataRole.UserRole, data)
            if self._library_apply_started:
                self._library_apply_initial_albums_populated += 1
                self._library_apply_initial_track_rows += len(entries)
                self._library_apply_album_population_ms += (
                    time.perf_counter() - population_started
                ) * 1000.0
            self._log(
                f"Album population: album_tracks={len(entries)}; "
                f"track_rows_created={len(entries)}; lyrics_cache_hits={cache_hits}; "
                f"lyrics_cache_unknown={len(entries) - cache_hits}; filesystem_checks=0; "
                f"filesystem_check_ms=0.0; label_generation_ms={label_ms:.1f}; "
                f"qt_item_creation_ms={item_ms:.1f}; "
                f"total_ms={(time.perf_counter() - population_started) * 1000.0:.1f}"
            )
            population_ms = (
                time.perf_counter() - population_started
            ) * 1000.0
            self.diagnostics.record(
                "library", "populate_album_rows",
                duration_ms=population_ms,
                severity="warning" if population_ms >= 30 else "info",
                details={
                    "reason": reason,
                    "rows_created": len(entries),
                    "lyrics_cache_hits": cache_hits,
                    "filesystem_checks": 0,
                    "immediate": True,
                },
                minimum_level=(
                    "basic" if population_ms >= 30 else "detailed"
                ),
            )
            return
        # Keep album expansion responsive by adding track rows in small GUI-thread batches.
        data["populating"] = True
        album_item.setData(0, QtCore.Qt.ItemDataRole.UserRole, data)
        self._populate_queue.append({
            "album_item": album_item,
            "data": data,
            "entries": entries,
            "reason": reason,
            "created": 0,
            "cache_hits": 0,
            "label_ms": 0.0,
            "item_ms": 0.0,
            "started": population_started,
        })
        if not self._populate_timer.isActive():
            self._populate_timer.start()

    def _populate_album_tick(self):
        batch = 18
        made = 0
        while self._populate_queue and made < batch:
            job = self._populate_queue[0]
            entries = job.get("entries") or []
            if not entries:
                data = job.get("data") or {}
                data.pop("populating", None)
                data["populated"] = True
                album_item = job.get("album_item")
                if album_item is not None:
                    album_item.setData(0, QtCore.Qt.ItemDataRole.UserRole, data)
                created = int(job.get("created", 0))
                cache_hits = int(job.get("cache_hits", 0))
                self._log(
                    f"Album population: album_tracks={created}; "
                    f"track_rows_created={created}; lyrics_cache_hits={cache_hits}; "
                    f"lyrics_cache_unknown={created - cache_hits}; filesystem_checks=0; "
                    f"filesystem_check_ms=0.0; "
                    f"label_generation_ms={float(job.get('label_ms', 0.0)):.1f}; "
                    f"qt_item_creation_ms={float(job.get('item_ms', 0.0)):.1f}; "
                    f"total_ms={(time.perf_counter() - float(job.get('started', time.perf_counter()))) * 1000.0:.1f}"
                )
                if job.get("reason") == "user":
                    self._lazy_albums_populated += 1
                    self._log(
                        f"Library lazy album population: albums={self._lazy_albums_populated}; "
                        f"track_rows={self._lazy_track_rows_created}"
                    )
                self._populate_queue.pop(0)
                continue
            (
                disc_item, track_no, title, artist, path, cached_lyrics
            ) = entries.pop(0)
            label_ms, item_ms, known = self._add_album_track_item(
                disc_item, track_no, title, artist, path, cached_lyrics
            )
            job["created"] = int(job.get("created", 0)) + 1
            job["cache_hits"] = int(job.get("cache_hits", 0)) + int(known)
            job["label_ms"] = float(job.get("label_ms", 0.0)) + label_ms
            job["item_ms"] = float(job.get("item_ms", 0.0)) + item_ms
            if job.get("reason") == "user":
                self._lazy_track_rows_created += 1
            made += 1
        if not self._populate_queue:
            self._populate_timer.stop()

    def _cover_build_tick(self):
        if not self._cover_queue:
            self._cover_timer.stop()
            return
        diagnostic_started = time.perf_counter()
        requested = 0
        batch = 12
        for _ in range(batch):
            if not self._cover_queue:
                break
            album_item, album, display_artist, items, album_key = self._cover_queue.pop(0)
            if (not self.album_covers_enabled) and (album_key not in self.album_cover_allowlist):
                continue
            if self._request_album_artwork(
                album_item, album, display_artist, items, album_key
            ):
                requested += 1
        duration_ms = (time.perf_counter() - diagnostic_started) * 1000.0
        self._diagnostic_artwork_batches += 1
        self._diagnostic_artwork_decoded += requested
        self._diagnostic_artwork_total_ms += duration_ms
        self._diagnostic_artwork_max_ms = max(
            self._diagnostic_artwork_max_ms, duration_ms
        )
        if duration_ms >= 30:
            self.diagnostics.record(
                "artwork", "slow_album_cover_batch",
                duration_ms=duration_ms,
                severity="warning",
                details={
                    "requested": requested,
                    "remaining": len(self._cover_queue),
                },
                minimum_level="basic",
                rate_limit_seconds=2.0,
            )
        if not self._cover_queue and self._diagnostic_artwork_batches:
            self.diagnostics.record(
                "artwork", "album_cover_restore_summary",
                duration_ms=self._diagnostic_artwork_total_ms,
                details={
                    "batches": self._diagnostic_artwork_batches,
                    "decoded": self._diagnostic_artwork_decoded,
                    "longest_batch_ms": self._diagnostic_artwork_max_ms,
                    "average_batch_ms": (
                        self._diagnostic_artwork_total_ms
                        / self._diagnostic_artwork_batches
                    ),
                },
                minimum_level="detailed",
            )
            self._diagnostic_artwork_batches = 0
            self._diagnostic_artwork_decoded = 0
            self._diagnostic_artwork_total_ms = 0.0
            self._diagnostic_artwork_max_ms = 0.0

    def _apply_search(self, text: str):
        text = normalise_search_text(text)
        generation = self._library_search_generation
        self.diagnostics.counters["searches_performed"] += 1
        self._log(
            f"Library search queued: generation={generation}; query={text!r}; "
            f"records={len(self._library_search_index)}"
        )
        if self.search_worker:
            diagnostic_tokens = getattr(
                self, "_diagnostic_search_tokens", {}
            )
            for old_generation in list(diagnostic_tokens):
                if old_generation == generation:
                    continue
                old_token = diagnostic_tokens.pop(old_generation)
                stats = self.diagnostics.worker_stats[old_token.operation]
                stats["queued"] = max(0, stats["queued"] - 1)
                stats["cancelled"] += 1
                self.diagnostics.record(
                    "search", "filter_library",
                    phase="complete", status="cancelled",
                    event_id=old_token.event_id,
                    correlation_id=old_token.correlation_id,
                    generation=old_generation,
                    duration_ms=0.0,
                    queue_wait_ms=(
                        time.perf_counter() - old_token.submitted_at
                    ) * 1000.0,
                    details={"reason": "superseded_generation"},
                    minimum_level="detailed",
                )
            token = self.diagnostics.submit_worker(
                "search", "filter_library",
                generation=generation,
                correlation_id=f"search-{generation}",
                details={
                    "records": len(self._library_search_index),
                    "query_length": len(text),
                },
            )
            self._diagnostic_search_tokens = diagnostic_tokens
            self._diagnostic_search_tokens[generation] = token
            self.search_worker.set_query(
                generation, text, self._library_search_index
            )
            return

    def _on_search_results(
        self, generation: int, query: str,
        results: List[Dict[str, Any]], album_entries: List[tuple],
        filtering_ms: float,
    ):
        current = normalise_search_text(self.search_box.text())
        stale = (
            generation != self._library_search_generation
            or query != current
            or getattr(self, "_closing", False)
        )
        token = getattr(
            self, "_diagnostic_search_tokens", {}
        ).pop(generation, None)
        if token is not None:
            queue_wait_ms = max(
                0.0,
                (time.perf_counter() - token.submitted_at) * 1000.0
                - filtering_ms,
            )
            stats = self.diagnostics.worker_stats[token.operation]
            stats["queued"] = max(0, stats["queued"] - 1)
            stats["completed"] += int(not stale)
            stats["cancelled"] += int(stale)
            stats["queue_wait_total_ms"] += queue_wait_ms
            stats["queue_wait_max_ms"] = max(
                stats["queue_wait_max_ms"], queue_wait_ms
            )
            stats["execution_total_ms"] += filtering_ms
            stats["execution_max_ms"] = max(
                stats["execution_max_ms"], filtering_ms
            )
            self.diagnostics.record(
                "search", "filter_library", phase="complete",
                status="cancelled" if stale else "success",
                event_id=token.event_id,
                correlation_id=token.correlation_id,
                generation=generation,
                duration_ms=filtering_ms,
                queue_wait_ms=queue_wait_ms,
                details={"matches": len(results), "albums": len(album_entries)},
                minimum_level="detailed",
            )
        if stale:
            self._log(f"Library search stale result discarded: generation={generation}")
            self.diagnostics.record(
                "search", "stale_result_disposal",
                status="cancelled", generation=generation,
                details={"matches": len(results)},
                minimum_level="detailed",
            )
            return
        apply_started = time.perf_counter()
        if not query:
            self._search_results_active = False
            self._set_tracks_from_meta(
                results,
                apply_generation=generation,
                cached_artwork_only=True,
                prepared_album_entries=album_entries,
                reason="clear_search",
                trigger_source="search_worker",
            )
            self._showing_full = True
        else:
            self._search_results_active = True
            self._set_tracks_from_meta(
                results,
                apply_generation=generation,
                cached_artwork_only=True,
                prepared_album_entries=album_entries,
                reason="search_results",
                trigger_source="search_worker",
            )
            self._showing_full = False
        if not query:
            announcement = "Full library restored"
        elif not results:
            announcement = "No matching music found"
        else:
            albums = {
                (
                    str(meta.get("album_artist") or meta.get("artist") or ""),
                    str(meta.get("album") or ""),
                )
                for meta in results
            }
            announcement = f"{len(results)} tracks in {len(albums)} albums"
        if announcement != self._last_search_announcement:
            self._last_search_announcement = announcement
            self._announce_accessible_status(announcement)
        self._log(
            f"Library search complete: generation={generation}; "
            f"matches={len(results)}; filtering_ms={filtering_ms:.1f}; "
            f"apply_dispatch_ms={(time.perf_counter() - apply_started) * 1000.0:.1f}"
        )
        apply_ms = (time.perf_counter() - apply_started) * 1000.0
        self.diagnostics.record(
            "search", "dispatch_search_results",
            generation=generation,
            correlation_id=f"search-{generation}",
            duration_ms=apply_ms,
            details={"matches": len(results), "query_length": len(query)},
            minimum_level="detailed",
        )

    def _parse_duration_seconds(self, dur_str: str) -> float:
        """Turn a 'm:ss' or 'h:mm:ss' string into seconds; default 180."""
        try:
            parts = [int(p) for p in str(dur_str).split(":")]
            secs = 0
            for p in parts:
                secs = secs * 60 + p
            return float(secs) if secs > 0 else 180.0
        except Exception:
            return 180.0

    def _song_facts(self, info) -> List[str]:
        """Fallback 'did you know' cards built from tags when no bio exists."""
        facts = []
        if getattr(info, "album", "") and info.album not in ("Unknown", ""):
            facts.append(f"This track appears on the album \u201c{info.album}\u201d.")
        if getattr(info, "genre", "") and info.genre not in ("Unknown", ""):
            facts.append(f"Genre: {info.genre}.")
        if getattr(info, "bitrate", "") and info.bitrate not in ("Unknown", ""):
            facts.append(f"Encoded at {info.bitrate}"
                         + (f", {info.sample_rate}." if info.sample_rate not in ("Unknown", "") else "."))
        if getattr(info, "artist", "") and info.artist not in ("Unknown", ""):
            facts.append(f"Now playing music by {info.artist}.")
        return facts

    def _start_jukebox_intro(self, info):
        if getattr(self, "overlay", None) is None:
            return
        self._intro_info = info
        track_len = self._parse_duration_seconds(getattr(info, "duration", ""))
        self.overlay.setGeometry(self.centralWidget().rect())
        self.overlay.raise_()
        self.overlay.start_track(
            artist=info.artist if info.artist not in ("Unknown", "") else "Unknown Artist",
            title=info.title if info.title not in ("Unknown", "") else
                  os.path.splitext(os.path.basename(info.path))[0],
            track_len=track_len,
        )
        # Seed fallback facts immediately; if a real bio arrives it replaces them.
        self._intro_used_bio = False
        facts = self._song_facts(info)
        if facts:
            self.overlay.set_cards(facts)

    def _record_stale_now_playing_detail(self, detail_type: str, generation: int, path: str = ""):
        self.diagnostics.record(
            "now_playing", "detail_result_discarded_stale", status="cancelled",
            generation=generation,
            details={"detail_type": detail_type, **self.diagnostics.path_details(path)},
            minimum_level="detailed", rate_limit_seconds=1.0,
        )

    def _on_bio_ready(self, artist: str, text: str, generation: int):
        diagnostic_started = time.perf_counter()
        stale_started = time.perf_counter()
        if (
            artist != getattr(self, "_bio_requested_artist", artist)
            or generation != self._now_playing_generation.identity.generation
        ):
            self._record_stale_now_playing_detail("biography", generation)
            self.diagnostics.record(
                "biography", "apply_artist_biography",
                status="stale",
                duration_ms=(time.perf_counter() - diagnostic_started) * 1000.0,
                details={"stale_validation_ms": (
                    time.perf_counter() - stale_started
                ) * 1000.0},
                minimum_level="detailed",
            )
            return
        cleanup_started = time.perf_counter()
        cleaned = self._clean_bio_text(text)
        cleanup_ms = (time.perf_counter() - cleanup_started) * 1000.0
        formatting_started = time.perf_counter()
        if cleaned:
            bio_card = self._format_bio_card(cleaned)
            formatting_ms = (
                time.perf_counter() - formatting_started
            ) * 1000.0
            apply_started = time.perf_counter()
            self._start_bio_animation(bio_card)
            # Feed the jukebox cards with the same polished facts, not raw paragraphs.
            try:
                if getattr(self, "overlay", None) is not None:
                    chunks = self._bio_card_chunks(bio_card)
                    if chunks:
                        self.overlay.set_cards(chunks)
                        self._intro_used_bio = True
            except Exception:
                pass
        else:
            formatting_ms = (
                time.perf_counter() - formatting_started
            ) * 1000.0
            apply_started = time.perf_counter()
            self.bio_box.setPlainText("No bio found.")
            self.bio_box.verticalScrollBar().setValue(0)
        widget_apply_ms = (time.perf_counter() - apply_started) * 1000.0
        self.diagnostics.record(
            "biography", "apply_artist_biography",
            duration_ms=(
                time.perf_counter() - diagnostic_started
            ) * 1000.0,
            details={
                "text_length": len(text or ""),
                "stale_validation_ms": (
                    time.perf_counter() - stale_started
                ) * 1000.0,
                "text_cleanup_ms": cleanup_ms,
                "formatting_ms": formatting_ms,
                "widget_apply_ms": widget_apply_ms,
            },
            minimum_level="detailed",
        )
        self.diagnostics.record(
            "now_playing", "detail_result_applied", generation=generation,
            details={"detail_type": "biography", "cache_hit": False},
            minimum_level="detailed",
        )

    def _bio_sentences(self, text: str) -> List[str]:
        # Do not split artist names such as "Panic! at the Disco"; only split ! before likely sentence starts.
        pattern = r"(?<=[.?])\s+|(?<=!)\s+(?=[A-Z0-9\"'(\[])"
        return [s.strip() for s in re.split(pattern, text) if s.strip()]

    def _pick_sentence(self, sentences: List[str], patterns: Tuple[str, ...], used: set) -> str:
        for sentence in sentences:
            if sentence in used:
                continue
            lower = sentence.lower()
            if any(pattern in lower for pattern in patterns):
                used.add(sentence)
                return sentence
        for sentence in sentences:
            if sentence not in used:
                used.add(sentence)
                return sentence
        return ""

    def _format_bio_card(self, text: str) -> str:
        """Turn raw source bios into short, readable music-player cards."""
        info = getattr(self, "_intro_info", None)
        artist = getattr(info, "artist", "") or "Artist"
        title = getattr(info, "title", "") or ""
        album = getattr(info, "album", "") or ""
        genre = getattr(info, "genre", "") or ""
        mode = getattr(self, "bio_detail_mode", "detailed")
        raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
        known_for = ""
        style_tags = ""
        artist_facts = ""
        body_lines = []
        for line in raw_lines:
            lower = line.lower()
            if lower.startswith("known for:"):
                known_for = line.split(":", 1)[1].strip().rstrip(".")
            elif lower.startswith("style tags:"):
                style_tags = line.split(":", 1)[1].strip().rstrip(".")
            elif lower.startswith("artist facts:"):
                artist_facts = line.split(":", 1)[1].strip().rstrip(".")
            else:
                body_lines.append(line)
        body_text = " ".join(body_lines)
        sentences = self._bio_sentences(body_text)

        used = set()
        intro = self._pick_sentence(sentences, (" is ", " are ", " was ", " were ", "formed", "duo", "band", "singer"), used)
        context = self._pick_sentence(sentences, ("known", "best", "hit", "success", "popular", "famous", "award", "chart"), used)
        sound = self._pick_sentence(sentences, ("style", "sound", "genre", "music", "pop", "rock", "soul", "dance", "electronic", "synth"), used)
        extra_story = [] if mode != "detailed" else [s for s in sentences if s not in used and len(s) > 45][:2]

        lines = [artist, ""]
        if intro:
            lines.append(intro)
        if context and context != intro and not context.lower().startswith(("style tags:", "known for:", "artist facts:")):
            lines.extend(["", "Why they matter", context])
        if extra_story:
            lines.extend(["", "More about them", " ".join(extra_story)])
        if known_for:
            lines.extend(["", "Known for", known_for])
        if artist_facts:
            lines.extend(["", "Artist facts", artist_facts])
        if style_tags:
            lines.extend(["", "Style", style_tags])
        elif sound and sound not in (intro, context):
            lines.extend(["", "Sound", sound])

        now_playing = []
        if title and title not in ("Unknown", ""):
            now_playing.append(f"Now playing: {title}")
        if album and album not in ("Unknown", "Unknown Album", ""):
            now_playing.append(f"Album: {album}")
        if genre and genre not in ("Unknown", ""):
            now_playing.append(f"Style tags: {genre}")
        if now_playing:
            lines.extend(["", "This track", *now_playing])

        if mode == "facts":
            keep = {artist, "", "Known for", "Artist facts", "Style", "This track"}
            lines = [line for line in lines if line in keep or line.startswith(("Now playing:", "Album:", "Style tags:")) or (known_for and line == known_for) or (artist_facts and line == artist_facts) or (style_tags and line == style_tags)]
        return "\n".join(lines).strip()

    def _bio_card_chunks(self, text: str, max_chunks: int = 8) -> List[str]:
        mode = getattr(self, "bio_detail_mode", "detailed")
        if mode == "concise":
            max_chunks = min(max_chunks, 4)
        elif mode == "facts":
            max_chunks = min(max_chunks, 5)
        chunks = []
        current_title = ""
        current_lines = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line in ("Why they matter", "More about them", "Known for", "Artist facts", "Style", "Sound", "This track"):
                if current_lines:
                    chunks.append((current_title + ": " if current_title else "") + " ".join(current_lines))
                    current_lines = []
                current_title = line
                continue
            if not chunks and not current_title and not current_lines:
                continue
            current_lines.append(line)
        if current_lines:
            chunks.append((current_title + ": " if current_title else "") + " ".join(current_lines))
        return chunks[:max_chunks]

    def _chunk_bio(self, text: str, max_chunks: int = 6) -> List[str]:
        """Split a bio into card-sized chunks (1-2 sentences each)."""
        import re as _re
        pattern = r"(?<=[.?])\s+|(?<=!)\s+(?=[A-Z0-9\"'(\[])"
        sentences = [s.strip() for s in _re.split(pattern, text) if s.strip()]
        chunks = []
        i = 0
        while i < len(sentences) and len(chunks) < max_chunks:
            chunk = sentences[i]
            # pair short sentences together
            if len(chunk) < 60 and i + 1 < len(sentences):
                chunk = chunk + " " + sentences[i + 1]
                i += 2
            else:
                i += 1
            chunks.append(chunk)
        return chunks

    def _scroll_bio(self):
        if not self.bio_box.toPlainText().strip():
            return
        bar = self.bio_box.verticalScrollBar()
        if bar.maximum() == 0:
            return
        now = time.time()
        if self.bio_animating or self.bio_user_hold or now < self.bio_pause_until:
            return
        if self.bio_reset_after_pause:
            self.bio_scroll_pos = 0.0
            bar.setValue(0)
            self.bio_reset_after_pause = False
            return
        self.bio_scroll_pos += 0.6
        value = int(self.bio_scroll_pos)
        if value >= bar.maximum():
            bar.setValue(bar.maximum())
            self.bio_pause_until = now + BIO_PAUSE_AT_END_SEC
            self.bio_reset_after_pause = True
            return
        bar.setValue(value)

    def _scroll_tag(self):
        if not self.tag_box.toPlainText().strip():
            return
        bar = self.tag_box.verticalScrollBar()
        if bar.maximum() == 0:
            return
        now = time.time()
        if self.tag_user_hold or now < self.tag_pause_until:
            return
        if self.tag_reset_after_pause:
            self.tag_scroll_pos = 0.0
            bar.setValue(0)
            self.tag_reset_after_pause = False
            return
        self.tag_scroll_pos += 0.55
        value = int(self.tag_scroll_pos)
        if value >= bar.maximum():
            bar.setValue(bar.maximum())
            self.tag_pause_until = now + TAG_PAUSE_AT_END_SEC
            self.tag_reset_after_pause = True
            return
        bar.setValue(value)

    def eventFilter(self, obj, event):
        if (
            event.type() == QtCore.QEvent.Type.KeyPress
            and QtWidgets.QApplication.activeWindow() is self
        ):
            key = event.key()
            modifiers = event.modifiers()
            focused = QtWidgets.QApplication.focusWidget()
            no_modifiers = modifiers == QtCore.Qt.KeyboardModifier.NoModifier
            if key == QtCore.Qt.Key.Key_Escape and self._video_fullscreen_effective_intent():
                # Request, not a direct transition: this is inside event
                # filtering, and the child process's forwarded Escape can
                # ask for the same thing in the same event-loop turn --
                # both coalesce into exactly one physical exit. Gated on
                # effective INTENT, not only the committed flag, so Escape
                # also cancels an enter that is queued or physically
                # running but has not committed yet.
                self._request_video_fullscreen_state(False, "event_filter_escape")
                return True
            if key == QtCore.Qt.Key.Key_Space and no_modifiers:
                protected = isinstance(
                    focused,
                    (
                        QtWidgets.QLineEdit, QtWidgets.QTextEdit,
                        QtWidgets.QPlainTextEdit, QtWidgets.QAbstractButton,
                        QtWidgets.QAbstractItemView, QtWidgets.QComboBox,
                        QtWidgets.QSlider, QtWidgets.QCheckBox,
                    ),
                )
                if not protected:
                    self._toggle_play_pause()
                    return True
            if key == QtCore.Qt.Key.Key_Escape and focused is self.search_box:
                if self.search_box.text():
                    self.search_box.clear()
                else:
                    self._focus_library()
                return True
            if (
                focused is not None
                and (focused is self.queue_list or self.queue_list.isAncestorOf(focused))
            ):
                if key == QtCore.Qt.Key.Key_Delete and no_modifiers:
                    self._remove_selected_queue_item()
                    return True
                if key == QtCore.Qt.Key.Key_Up and modifiers == QtCore.Qt.KeyboardModifier.AltModifier:
                    self._move_selected_queue_item(-1)
                    return True
                if key == QtCore.Qt.Key.Key_Down and modifiers == QtCore.Qt.KeyboardModifier.AltModifier:
                    self._move_selected_queue_item(1)
                    return True
            if (
                focused is not None
                and (focused is self.recent_table or self.recent_table.isAncestorOf(focused))
            ):
                if key in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
                    if modifiers == QtCore.Qt.KeyboardModifier.ControlModifier:
                        entries = self._selected_recent_entries()
                        self._add_to_queue_with_dedup_guard([entry.path for entry in entries])
                    elif no_modifiers:
                        self._play_recently_played_selected()
                    else:
                        return super().eventFilter(obj, event)
                    return True
                if key == QtCore.Qt.Key.Key_Delete and no_modifiers:
                    entries = self._selected_recent_entries()
                    if entries:
                        self._remove_recent_entries(entries)
                    return True
            if (
                focused is not None
                and (focused is self.tree_tracks or self.tree_tracks.isAncestorOf(focused))
                and key in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter)
                and modifiers == QtCore.Qt.KeyboardModifier.ControlModifier
            ):
                self._add_selected_library_item_to_queue()
                return True
            if (
                (key == QtCore.Qt.Key.Key_F10 and modifiers == QtCore.Qt.KeyboardModifier.ShiftModifier)
                or (key == QtCore.Qt.Key.Key_Menu and no_modifiers)
            ):
                if self._open_context_menu_for_focused_widget():
                    return True
        if obj is self.bio_box.verticalScrollBar():
            if event.type() == QtCore.QEvent.Type.Enter:
                self.bio_user_hold = True
            elif event.type() == QtCore.QEvent.Type.Leave:
                self.bio_user_hold = False
                self.bio_pause_until = time.time() + BIO_PAUSE_AFTER_LEAVE_SEC
        elif obj is self.bio_box.viewport():
            if event.type() == QtCore.QEvent.Type.Enter:
                self.bio_user_hold = True
            elif event.type() == QtCore.QEvent.Type.Leave:
                self.bio_user_hold = False
                self.bio_pause_until = time.time() + BIO_PAUSE_AFTER_LEAVE_SEC
        elif obj is self.tag_box.verticalScrollBar():
            if event.type() == QtCore.QEvent.Type.Enter:
                self.tag_user_hold = True
            elif event.type() == QtCore.QEvent.Type.Leave:
                self.tag_user_hold = False
                self.tag_pause_until = time.time() + TAG_PAUSE_AFTER_LEAVE_SEC
        elif obj is self.tag_box.viewport():
            if event.type() == QtCore.QEvent.Type.Enter:
                self.tag_user_hold = True
            elif event.type() == QtCore.QEvent.Type.Leave:
                self.tag_user_hold = False
                self.tag_pause_until = time.time() + TAG_PAUSE_AFTER_LEAVE_SEC
        return super().eventFilter(obj, event)

    def _clean_bio_text(self, text: str) -> str:
        if not text:
            return ""
        # Preserve source lines such as "Artist facts:" and "Style tags:";
        # the card builder uses them as labelled sections.
        text = re.sub(r"Read more on Last\.fm.*", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
        text = re.sub(r"<[^>]+>", "", text)
        lines = []
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = re.sub(r"\s+", " ", line).strip()
            if line:
                lines.append(clean_text(line))
        return "\n".join(lines).strip()

    def _normalize_artist_for_bio(self, artist: str) -> str:
        if not artist:
            return ""
        # Strip "feat." or "ft." segments
        artist = re.split(r"\s+(feat\.|ft\.|featuring)\s+", artist, flags=re.IGNORECASE)[0]
        # Split common separators and take the first artist
        artist = re.split(r"\s+(vs\.|vs|&|and|/|x)\s+", artist, flags=re.IGNORECASE)[0]
        # Strip trailing " - " extra info
        artist = artist.split(" - ")[0]
        return artist.strip()

    def _normalize_track_for_bio(self, title: str) -> str:
        if not title:
            return ""
        title = re.split(r"\s+\(.*?\)\s*", title)[0]  # remove parenthetical
        title = re.split(r"\s+\[.*?\]\s*", title)[0]  # remove bracketed
        title = re.split(r"\s+-\s+.*$", title)[0]  # remove suffix after " - "
        return title.strip()

    def _start_bio_animation(self, pending_text: str):
        self.bio_pending_text = pending_text
        self.bio_animating = True
        self.bio_anim_start = time.time()
        self.bio_scroll_pos = 0.0
        self.bio_box.document().setPageSize(
            QtCore.QSizeF(self.bio_box.viewport().width(), self.bio_box.viewport().height())
        )
        self.bio_box.setHtml(
            "<table width='100%' height='100%'><tr><td align='center' valign='middle'>"
            "BIO incoming</td></tr></table>"
        )
        self.bio_box.verticalScrollBar().setValue(0)
        self.bio_anim_timer.start(30)

    def _bio_anim_tick(self):
        if not self.bio_animating:
            self.bio_anim_timer.stop()
            return
        t = time.time() - self.bio_anim_start
        duration = 1.2
        if t >= duration:
            self.bio_animating = False
            self.bio_anim_timer.stop()
            if self.bio_pending_text:
                self.bio_scroll_pos = 0.0
                self.bio_box.setPlainText(self.bio_pending_text)
                self.bio_box.verticalScrollBar().setValue(0)
                self.bio_pending_text = ""
            else:
                self.bio_box.setPlainText("No bio found.")
            return
        if t <= 0.6:
            p = t / 0.6
            size = 8 + (20 - 8) * p
            opacity = p
        else:
            p = (t - 0.6) / 0.6
            size = 20
            opacity = 1.0 - p
        hue = (t * 180) % 360
        r, g, b = self._hsv_to_rgb(hue, 0.8, 1.0)
        alpha = int(255 * max(0.0, min(1.0, opacity)))
        self.bio_box.setHtml(
            "<table width='100%' height='100%'><tr><td align='center' valign='middle'>"
            f"<span style='color: rgba({r},{g},{b},{alpha}); font-size:{size:.1f}pt;'>"
            "BIO incoming</span></td></tr></table>"
        )

    def _hsv_to_rgb(self, h: float, s: float, v: float):
        h = h % 360
        c = v * s
        x = c * (1 - abs((h / 60.0) % 2 - 1))
        m = v - c
        if 0 <= h < 60:
            rp, gp, bp = c, x, 0
        elif 60 <= h < 120:
            rp, gp, bp = x, c, 0
        elif 120 <= h < 180:
            rp, gp, bp = 0, c, x
        elif 180 <= h < 240:
            rp, gp, bp = 0, x, c
        elif 240 <= h < 300:
            rp, gp, bp = x, 0, c
        else:
            rp, gp, bp = c, 0, x
        r = int((rp + m) * 255)
        g = int((gp + m) * 255)
        b = int((bp + m) * 255)
        return r, g, b
















