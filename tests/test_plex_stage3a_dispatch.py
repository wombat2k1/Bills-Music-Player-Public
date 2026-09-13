"""Stage 3A: the window.py dispatch/orchestration layer wiring
PlexPlaybackResolveWorker/BassStreamPrepareWorker into the existing
playback pipeline (_play_path_direct's Plex branch,
_play_plex_audio_path_direct, _play_plex_video_path_direct, and their
async continuations). A lightweight harness -- matching this suite's
established convention (see test_plex_library_browsing.py) -- binds only
the real, unbound PlayerWindow methods under test plus stub attributes/
no-op collaborators for everything else, rather than constructing the
full heavyweight window.

PlexPlaybackResolveWorker/BassStreamPrepareWorker are monkeypatched to
fakes that record their constructor arguments and never actually start a
QThread or touch the network -- the worker classes themselves already
have their own dedicated, real test coverage (test_plex_playback_resolve.py,
test_bass_url_streaming.py, test_player_load_worker.py). This file is
about the *dispatch* logic around them: backend gating, async staleness,
success/failure routing, Phase C1 candidate commit/discard, and that the
logical plex:// identity (never the transport URL) is what everything
else in the pipeline sees.
"""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets

import billsmusic.window as window_module
from billsmusic.media_type import MediaType
from billsmusic.plex_preferences import PlexPreferences
from billsmusic.plex_transport import PlexTransportSource
from billsmusic.window import PlayerWindow

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


AUDIO_IDENTITY = "plex://server-1/42.mp3"
VIDEO_IDENTITY = "plex://server-1/99.mp4"


class _FakeSignal:
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        for slot in list(self._slots):
            slot(*args)


class _FakeResolveWorker:
    """Records construction args; .start() does nothing (the test drives
    finished_result.emit(...) itself, exactly like the real worker would
    from its own thread, but synchronously and without any network I/O)."""
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.finished_result = _FakeSignal()
        self.finished = _FakeSignal()
        self.started = False
        type(self).instances.append(self)

    def start(self):
        self.started = True


class _FakeAudioLoadWorker:
    """Fakes BassStreamPrepareWorker: records constructor args (`source`,
    `token` only -- Phase C1's whole point is that this worker never
    receives or holds a live player instance); .start() does nothing
    (the test drives .prepared.emit(...)/.failed.emit(...) itself,
    synchronously, exactly like the real worker's signals would arrive
    but without any network I/O or real BASS call).

    Phase C2 (worker lifetime / shutdown ownership, 2026-09-11): also
    fakes the real worker's _prepared_candidate/claim_candidate() seam --
    the dispatch-time connect() lambda now calls w.claim_candidate()
    itself rather than trusting the raw emitted signal argument, so
    .prepared.emit(token, identity, candidate) here also captures
    `candidate` for claim_candidate() to hand back exactly once,
    matching BassStreamPrepareWorker.run()'s own
    "_prepared_candidate = candidate; then emit" ordering."""
    instances = []

    def __init__(self, source, token):
        self.source = source
        self.token = token
        self._prepared_candidate = None
        self.prepared = _FakeSignal()
        _real_emit = self.prepared.emit

        def _emit_and_capture(tok, ident, candidate):
            self._prepared_candidate = candidate
            _real_emit(tok, ident, candidate)

        self.prepared.emit = _emit_and_capture
        self.failed = _FakeSignal()
        self.finished = _FakeSignal()
        self.started = False
        type(self).instances.append(self)

    def start(self):
        self.started = True

    def claim_candidate(self):
        candidate = self._prepared_candidate
        self._prepared_candidate = None
        return candidate


class _FakePreparedCandidate:
    """Stands in for a real PreparedBassStream in dispatch tests.
    harness.bass_player/bass_inactive_player are MagicMocks, so
    commit_prepared(candidate) on them just returns a truthy MagicMock
    regardless of what `candidate` is -- this fake only tracks
    discard() calls, for tests asserting a rejected candidate was
    never committed."""
    def __init__(self, identity=None):
        self.identity = identity
        self.discarded = False

    def discard(self):
        self.discarded = True
        return True


class DispatchHarness:
    # Real, unbound PlayerWindow methods under test.
    _plex_resolved_connection_for_identity = PlayerWindow._plex_resolved_connection_for_identity
    _plex_effective_connection = PlayerWindow._plex_effective_connection
    _offer_switch_to_bass_for_plex_audio = PlayerWindow._offer_switch_to_bass_for_plex_audio
    _start_plex_playback_resolve = PlayerWindow._start_plex_playback_resolve
    _play_plex_audio_path_direct = PlayerWindow._play_plex_audio_path_direct
    _play_plex_video_path_direct = PlayerWindow._play_plex_video_path_direct
    _on_plex_playback_resolved = PlayerWindow._on_plex_playback_resolved
    _fail_plex_playback_resolve = PlayerWindow._fail_plex_playback_resolve
    _terminalise_video_transition_for_failed_incoming = PlayerWindow._terminalise_video_transition_for_failed_incoming
    _start_plex_audio_playback = PlayerWindow._start_plex_audio_playback
    _on_plex_audio_load_prepared = PlayerWindow._on_plex_audio_load_prepared
    _on_plex_audio_load_failed = PlayerWindow._on_plex_audio_load_failed
    _start_plex_video_playback = PlayerWindow._start_plex_video_playback
    _use_bass_backend = PlayerWindow._use_bass_backend
    _use_builtin_player = PlayerWindow._use_builtin_player
    _set_builtin_backend = PlayerWindow._set_builtin_backend
    _begin_playback_recovery = PlayerWindow._begin_playback_recovery
    # Playback stability hardening, Phase A -- real, unbound so the actual
    # authority logic is exercised, not just its absence papered over.
    _begin_playback_attempt = PlayerWindow._begin_playback_attempt
    _is_current_playback_attempt = PlayerWindow._is_current_playback_attempt
    _require_current_playback_attempt = PlayerWindow._require_current_playback_attempt
    _cancel_current_playback_attempt = PlayerWindow._cancel_current_playback_attempt
    _advance_playback_attempt_state = PlayerWindow._advance_playback_attempt_state
    # Phase C1 (native audio backend ownership) -- real, unbound so the
    # actual topology/lease logic is exercised, not just its absence
    # papered over.
    _set_player_topology = PlayerWindow._set_player_topology
    _promote_inactive_player = PlayerWindow._promote_inactive_player
    _make_target_lease = PlayerWindow._make_target_lease
    _target_lease_still_valid = PlayerWindow._target_lease_still_valid
    # Phase C1.1 (2026-09-11) -- real, unbound so a real discard()
    # failure is actually routed to a diagnostic, not just assumed.
    _discard_prepared_candidate = PlayerWindow._discard_prepared_candidate
    # Phase C2 (worker lifetime / shutdown ownership, 2026-09-11) -- both
    # dispatch sites under test now register with WorkerLifetimeRegistry
    # and route their finished handlers through these real, unbound
    # methods too.
    _finalize_unclaimed_prepare_candidate = PlayerWindow._finalize_unclaimed_prepare_candidate
    _on_plex_resolve_worker_finished = PlayerWindow._on_plex_resolve_worker_finished
    _on_plex_audio_load_worker_finished = PlayerWindow._on_plex_audio_load_worker_finished

    def __init__(self):
        self.plex_preferences = PlexPreferences(
            enabled=True, use_manual_server=True,
            server_address="http://plex-host:32400", token="REAL-TOKEN",
            client_identifier="client-1", server_config_id="server-1",
        )
        self._plex_active_connection_uri = ""
        self._plex_active_access_token = ""
        self.cast_active = False
        self._playback_generation = 0
        # Real dispatch always goes through _play_path_direct first (which
        # calls _begin_playback_attempt) -- this harness calls
        # _play_plex_audio_path_direct/_play_plex_video_path_direct
        # directly, so tests that care about attempt authority establish
        # one explicitly via harness._begin_playback_attempt(...).
        self._next_playback_attempt_id = 1
        self._current_playback_attempt = None
        self._plex_resolve_worker = None
        self._plex_resolve_pending_kind = None
        self._plex_resolve_pending_index = None
        self._plex_audio_load_worker = None
        self._plex_audio_load_token = 0
        self._closing = False
        from billsmusic.worker_registry import WorkerLifetimeRegistry
        self._worker_registry = WorkerLifetimeRegistry()
        self._maybe_resume_final_shutdown = lambda: None

        self.builtin_backend = "bass"
        self.use_simple = True
        self._temporary_backend_override = None
        self._simple_fallback_active = False
        self.bass_player = MagicMock(name="bass_player")
        self.bass_player.stats.return_value = {"duration": 180.0, "sample_rate": 44100, "channels": 2}
        self.bass_player.commit_prepared.return_value = True
        self.bass_player.physical_id = "bass-A"
        self.bass_inactive_player = MagicMock(name="bass_inactive_player")
        self.bass_inactive_player.commit_prepared.return_value = True
        self.bass_inactive_player.physical_id = "bass-B"
        self.miniaudio_player = MagicMock(name="miniaudio_player")
        self.miniaudio_player.commit_prepared.return_value = True
        self.miniaudio_player.physical_id = "miniaudio-A"
        self.miniaudio_inactive_player = MagicMock(name="miniaudio_inactive_player")
        self.miniaudio_inactive_player.commit_prepared.return_value = True
        self.miniaudio_inactive_player.physical_id = "miniaudio-B"
        # Phase C1: real _set_player_topology/_make_target_lease are bound
        # above, so this must be initialised the same way production is --
        # a plain reset (no prior state to diff against), not a call
        # through the seam.
        self._player_topology_epoch = 0
        self.simple_player = self.bass_player
        self.simple_inactive_player = self.bass_inactive_player
        self.chk_simple = SimpleNamespace(
            blockSignals=lambda *_a, **_k: None, setChecked=lambda *_a, **_k: None,
        )
        self._save_user_settings = lambda: None
        self._audio_log_calls = []
        self._audio_log = lambda msg: self._audio_log_calls.append(msg)

        self.video_playback_enabled = True
        self._current_media_type = MediaType.AUDIO
        self._video_transition_manager = None
        self._video_backend = MagicMock(name="video_backend")
        self._video_backend.is_dual_mode.return_value = False
        self._video_backend.load.return_value = True
        self.master_volume = 80
        self._muted = False
        self._sleep_timer_gain = 1.0
        self._active_normalisation_gain = 1.0
        self.track_index_by_path = {}
        self.beat = SimpleNamespace(setPlaying=lambda *_a, **_k: None)
        self.pending_next = True
        self.current_path = None
        self.auto_playback_recovery = True
        self._playback_recovery_active = False
        self.label_remaining = SimpleNamespace(setText=lambda *_a, **_k: None)
        self._current_video_error = None
        self._on_video_error = lambda category, message: setattr(
            self, "_current_video_error", (category, message),
        )

        self._status_messages = []
        self._diagnostics_events = []
        self.diagnostics = SimpleNamespace(
            record=lambda category, op, **kw: self._diagnostics_events.append((category, op, kw)),
            path_details=lambda value: {"path_hash": f"hash-of-{value}"} if value else {},
        )
        self._cached_gain_for_path = lambda path, target="active": 1.0
        self._audio_name = lambda path: path.rsplit("/", 1)[-1]
        self._set_playing_button_state = lambda: None
        self._activate_track_ui_calls = []
        self._activate_track_ui = lambda index, path: self._activate_track_ui_calls.append((index, path))
        self._reset_progress = lambda: None
        self._arm_playback_watchdog_calls = []
        self._arm_playback_watchdog = lambda pos=0.0: self._arm_playback_watchdog_calls.append(pos)
        self._cancel_fade = lambda: None
        self._stop_all_calls = 0
        self._stop_video_for_audio_transition = lambda: None
        self._cancel_playback_watchdog = lambda: None
        self._show_video_loading_page = lambda: None

    def _stop_all(self):
        self._stop_all_calls += 1

    def statusBar(self):
        harness = self

        class _Bar:
            def showMessage(self, text, *args):
                harness._status_messages.append(text)

        return _Bar()


class _FakeBassEngine:
    """Phase C1: _start_plex_audio_playback primes _BassEngine.ensure()
    on the GUI thread before dispatching the first BASS prepare worker
    -- faked here so dispatch-logic tests never touch the real
    bass.dll/BASS_Init."""
    @staticmethod
    def ensure():
        return None


def _harness(monkeypatch):
    monkeypatch.setattr(window_module, "PlexPlaybackResolveWorker", _FakeResolveWorker)
    monkeypatch.setattr(window_module, "BassStreamPrepareWorker", _FakeAudioLoadWorker)
    monkeypatch.setattr(window_module, "_BassEngine", _FakeBassEngine)
    _FakeResolveWorker.instances = []
    _FakeAudioLoadWorker.instances = []
    return DispatchHarness()


# -- connection resolution ---------------------------------------------------

def test_connection_lookup_returns_none_for_mismatched_server(monkeypatch):
    harness = _harness(monkeypatch)
    result = harness._plex_resolved_connection_for_identity("plex://some-other-server/1.mp3")
    assert result is None


def test_connection_lookup_returns_none_when_not_connected(monkeypatch):
    import dataclasses

    harness = _harness(monkeypatch)
    harness.plex_preferences = dataclasses.replace(harness.plex_preferences, server_address="")
    result = harness._plex_resolved_connection_for_identity(AUDIO_IDENTITY)
    assert result is None


def test_connection_lookup_resolves_matching_server(monkeypatch):
    harness = _harness(monkeypatch)
    result = harness._plex_resolved_connection_for_identity(AUDIO_IDENTITY)
    assert result == ("http://plex-host:32400", "REAL-TOKEN", "42")


# -- backend gating (Decision 2) ---------------------------------------------

def test_plex_audio_dispatches_resolve_when_bass_already_active(monkeypatch):
    harness = _harness(monkeypatch)
    harness._begin_playback_attempt(AUDIO_IDENTITY, MediaType.AUDIO, "test_dispatch")
    result = harness._play_plex_audio_path_direct(AUDIO_IDENTITY, index=3)
    assert result is True
    assert len(_FakeResolveWorker.instances) == 1
    worker = _FakeResolveWorker.instances[0]
    assert worker.started is True
    assert worker.kwargs["identity"] == AUDIO_IDENTITY
    assert worker.kwargs["media_kind"] == "music"
    assert worker.kwargs["rating_key"] == "42"
    assert worker.kwargs["token"] == "REAL-TOKEN"
    # The real secret token must never appear in the status bar text.
    assert all("REAL-TOKEN" not in m for m in harness._status_messages)


def test_plex_audio_on_miniaudio_prompts_and_declines_leaves_miniaudio(monkeypatch):
    harness = _harness(monkeypatch)
    harness.builtin_backend = "miniaudio"
    harness.simple_player = harness.miniaudio_player
    harness.pending_next = False
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question",
        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No,
    )

    harness._begin_playback_attempt(AUDIO_IDENTITY, MediaType.AUDIO, "test_dispatch")
    result = harness._play_plex_audio_path_direct(AUDIO_IDENTITY)

    assert result is False
    assert harness.builtin_backend == "miniaudio"  # never auto-switched
    assert _FakeResolveWorker.instances == []
    assert any("BASS audio backend" in m for m in harness._status_messages)
    # Declining leaves the queue exactly as it was -- no advance, nothing
    # left "in flight" that could later suppress a real stall/near-end
    # trigger for whatever plays next.
    assert harness.pending_next is False


def test_plex_audio_on_miniaudio_prompts_and_accepts_switches_and_resolves(monkeypatch):
    harness = _harness(monkeypatch)
    harness.builtin_backend = "miniaudio"
    harness.simple_player = harness.miniaudio_player
    harness.pending_next = False
    pending_next_during_dialog = []

    def _question(*a, **k):
        # The real QMessageBox.question() runs Qt's own nested event loop
        # -- the stall watchdog/near-end triggers (both gated on
        # pending_next) keep ticking for however long the user takes to
        # answer. Captured here to prove the guard is already up *before*
        # the click, not just after.
        pending_next_during_dialog.append(harness.pending_next)
        return QtWidgets.QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QtWidgets.QMessageBox, "question", _question)

    harness._begin_playback_attempt(AUDIO_IDENTITY, MediaType.AUDIO, "test_dispatch")
    result = harness._play_plex_audio_path_direct(AUDIO_IDENTITY)

    assert result is True
    assert harness.builtin_backend == "bass"  # switched only after explicit Yes
    assert pending_next_during_dialog == [True]
    # Real-device bug: the backend switch used to be immediately followed
    # by the queue silently advancing to the next track instead of
    # resolving/playing the same Plex track that was just confirmed. The
    # resolve is dispatched (started) but not yet resolved -- pending_next
    # must still be True, and the SAME track (not the next queue row) is
    # what's in flight.
    assert harness.pending_next is True
    assert len(_FakeResolveWorker.instances) == 1
    assert _FakeResolveWorker.instances[0].kwargs["identity"] == AUDIO_IDENTITY

    # Completing the resolve+load starts exactly that same track -- the
    # queue head is untouched, no other track was ever dispatched.
    resolve_worker = _FakeResolveWorker.instances[0]
    source = PlexTransportSource(identity=AUDIO_IDENTITY, transport_url="http://plex-host/part.mp3")
    resolve_worker.finished_result.emit({
        "success": True, "identity": AUDIO_IDENTITY,
        "generation": resolve_worker.kwargs["generation"], "transport_source": source,
    })
    load_worker = _FakeAudioLoadWorker.instances[0]
    load_worker.prepared.emit(load_worker.token, AUDIO_IDENTITY, _FakePreparedCandidate(AUDIO_IDENTITY))

    assert harness._activate_track_ui_calls == [(None, AUDIO_IDENTITY)]
    assert harness.pending_next is False
    harness.bass_player.play.assert_called_once()
    assert harness.simple_player is harness.bass_player
    assert len(_FakeResolveWorker.instances) == 1


# -- Cast exclusion (Decision 5) ---------------------------------------------

def test_plex_audio_rejected_while_casting(monkeypatch):
    harness = _harness(monkeypatch)
    harness.cast_active = True
    harness._begin_playback_attempt(AUDIO_IDENTITY, MediaType.AUDIO, "test_dispatch")
    result = harness._play_plex_audio_path_direct(AUDIO_IDENTITY)
    assert result is False
    assert _FakeResolveWorker.instances == []
    assert any("Cast" in m for m in harness._status_messages)


# -- async staleness (items 30/31/32) ----------------------------------------

def test_stale_resolve_result_is_silently_discarded(monkeypatch):
    harness = _harness(monkeypatch)
    harness._begin_playback_attempt(AUDIO_IDENTITY, MediaType.AUDIO, "test_dispatch")
    harness._play_plex_audio_path_direct(AUDIO_IDENTITY)
    worker = _FakeResolveWorker.instances[0]
    stale_generation = worker.kwargs["generation"]
    # A later, unrelated play action (source switch while resolving)
    # bumps the shared generation counter before this worker reports back.
    harness._playback_generation += 1

    worker.finished_result.emit({
        "success": True, "identity": AUDIO_IDENTITY,
        "generation": stale_generation,
        "transport_source": PlexTransportSource(identity=AUDIO_IDENTITY, transport_url="http://x/y.mp3"),
    })

    assert _FakeAudioLoadWorker.instances == []  # never actually started playback
    assert harness._activate_track_ui_calls == []
    # Stage 3A-r2 item 9: this rejection must leave a diagnostic trail --
    # not a new suppression mechanism, just visibility into the existing
    # generation-ownership check actually firing.
    superseded_events = [
        e for e in harness._diagnostics_events
        if e[1] == "plex_playback_attempt_superseded"
    ]
    assert len(superseded_events) == 1
    assert superseded_events[0][2]["details"]["stage"] == "resolve"
    assert superseded_events[0][2]["details"]["stale_generation"] == stale_generation


def test_shutdown_during_resolution_is_a_safe_noop(monkeypatch):
    harness = _harness(monkeypatch)
    harness._begin_playback_attempt(AUDIO_IDENTITY, MediaType.AUDIO, "test_dispatch")
    harness._play_plex_audio_path_direct(AUDIO_IDENTITY)
    worker = _FakeResolveWorker.instances[0]
    harness._closing = True

    worker.finished_result.emit({
        "success": True, "identity": AUDIO_IDENTITY,
        "generation": worker.kwargs["generation"],
        "transport_source": PlexTransportSource(identity=AUDIO_IDENTITY, transport_url="http://x/y.mp3"),
    })

    assert _FakeAudioLoadWorker.instances == []


# -- successful audio resolve -> load -> play tail ---------------------------

def test_successful_audio_resolve_starts_load_then_play(monkeypatch):
    harness = _harness(monkeypatch)
    harness._begin_playback_attempt(AUDIO_IDENTITY, MediaType.AUDIO, "test_dispatch")
    harness._play_plex_audio_path_direct(AUDIO_IDENTITY, index=7)
    resolve_worker = _FakeResolveWorker.instances[0]
    source = PlexTransportSource(
        identity=AUDIO_IDENTITY, transport_url="http://plex-host:32400/part.mp3",
        extra_headers={"X-Plex-Token": "REAL-TOKEN"},
    )
    resolve_worker.finished_result.emit({
        "success": True, "identity": AUDIO_IDENTITY,
        "generation": resolve_worker.kwargs["generation"], "transport_source": source,
    })

    assert len(_FakeAudioLoadWorker.instances) == 1
    load_worker = _FakeAudioLoadWorker.instances[0]
    assert load_worker.source is source
    # Phase C1 structural requirement: the worker holds no player
    # reference at all -- only source/token.
    assert not hasattr(load_worker, "player")
    # The queue-row identity used for UI activation is always the stable
    # plex:// identity, never the transport URL.
    assert harness._activate_track_ui_calls == [(7, AUDIO_IDENTITY)]

    load_worker.prepared.emit(load_worker.token, AUDIO_IDENTITY, _FakePreparedCandidate(AUDIO_IDENTITY))

    harness.bass_player.commit_prepared.assert_called_once()
    harness.bass_player.play.assert_called_once()
    assert harness._arm_playback_watchdog_calls == [0.0]
    assert harness.pending_next is False


def test_stale_audio_load_result_never_touches_the_shared_player(monkeypatch):
    # Phase C1 (native audio backend ownership, 2026-09-10): the candidate
    # emitted by a stale/superseded load result never touched
    # bass_player in the first place (it's a private PreparedBassStream)
    # -- so the fix here is structural, not just "don't call .stop()":
    # this callback commits NOTHING to the shared player and discards
    # only the candidate's own resource.
    harness = _harness(monkeypatch)
    harness._begin_playback_attempt(AUDIO_IDENTITY, MediaType.AUDIO, "test_dispatch")
    harness._play_plex_audio_path_direct(AUDIO_IDENTITY)
    resolve_worker = _FakeResolveWorker.instances[0]
    source = PlexTransportSource(identity=AUDIO_IDENTITY, transport_url="http://x/y.mp3")
    resolve_worker.finished_result.emit({
        "success": True, "identity": AUDIO_IDENTITY,
        "generation": resolve_worker.kwargs["generation"], "transport_source": source,
    })
    load_worker = _FakeAudioLoadWorker.instances[0]
    candidate = _FakePreparedCandidate(AUDIO_IDENTITY)

    load_worker.prepared.emit(999, AUDIO_IDENTITY, candidate)  # wrong/old token

    assert candidate.discarded is True
    harness.bass_player.commit_prepared.assert_not_called()
    harness.bass_player.play.assert_not_called()
    harness.bass_player.stop.assert_not_called()
    superseded_events = [
        e for e in harness._diagnostics_events
        if e[1] == "plex_playback_attempt_superseded"
    ]
    assert len(superseded_events) == 1
    assert superseded_events[0][2]["details"]["stage"] == "audio_load_succeeded"
    assert superseded_events[0][2]["details"]["stale_token"] == 999


def test_failed_resolve_leaves_queue_usable(monkeypatch):
    harness = _harness(monkeypatch)
    harness.pending_next = True
    harness._begin_playback_attempt(AUDIO_IDENTITY, MediaType.AUDIO, "test_dispatch")
    harness._play_plex_audio_path_direct(AUDIO_IDENTITY)
    resolve_worker = _FakeResolveWorker.instances[0]

    resolve_worker.finished_result.emit({
        "success": False, "reason": "Connection timed out",
        "identity": AUDIO_IDENTITY, "generation": resolve_worker.kwargs["generation"],
    })

    assert harness.pending_next is False
    assert any("Connection timed out" in m for m in harness._status_messages)
    assert _FakeAudioLoadWorker.instances == []


def test_direct_play_unavailable_message_is_clear(monkeypatch):
    harness = _harness(monkeypatch)
    harness._begin_playback_attempt(AUDIO_IDENTITY, MediaType.AUDIO, "test_dispatch")
    harness._play_plex_audio_path_direct(AUDIO_IDENTITY)
    resolve_worker = _FakeResolveWorker.instances[0]

    resolve_worker.finished_result.emit({
        "success": False, "reason": "Unsupported audio container for Direct Play: wma",
        "identity": AUDIO_IDENTITY, "generation": resolve_worker.kwargs["generation"],
        "direct_play_unavailable": True, "container": "wma", "audio_codec": "wmav2",
    })

    assert any("Direct Played" in m for m in harness._status_messages)


# -- video: GPU deferral (Decision 4) ----------------------------------------

def test_video_resolve_success_loads_via_classic_backend_with_identity_and_transport(monkeypatch):
    harness = _harness(monkeypatch)
    harness._begin_playback_attempt(VIDEO_IDENTITY, MediaType.VIDEO, "test_dispatch")
    harness._play_plex_video_path_direct(VIDEO_IDENTITY, index=2)
    resolve_worker = _FakeResolveWorker.instances[0]
    assert resolve_worker.kwargs["media_kind"] == "video"
    source = PlexTransportSource(
        identity=VIDEO_IDENTITY,
        transport_url="http://plex-host:32400/part.mp4?X-Plex-Token=REAL-TOKEN",
    )

    resolve_worker.finished_result.emit({
        "success": True, "identity": VIDEO_IDENTITY,
        "generation": resolve_worker.kwargs["generation"], "transport_source": source,
    })

    harness._video_backend.load.assert_called_once_with(
        VIDEO_IDENTITY, transport_url=source.transport_url,
    )
    assert harness._current_media_type == MediaType.VIDEO
    assert harness._activate_track_ui_calls == [(2, VIDEO_IDENTITY)]
    harness._video_backend.set_dual_mode.assert_not_called()


def test_plex_video_defers_gpu_dual_mode_to_classic(monkeypatch):
    harness = _harness(monkeypatch)
    harness._video_backend.is_dual_mode.return_value = True
    harness._begin_playback_attempt(VIDEO_IDENTITY, MediaType.VIDEO, "test_dispatch")
    harness._play_plex_video_path_direct(VIDEO_IDENTITY)
    resolve_worker = _FakeResolveWorker.instances[0]
    source = PlexTransportSource(identity=VIDEO_IDENTITY, transport_url="http://plex-host/part.mp4")

    resolve_worker.finished_result.emit({
        "success": True, "identity": VIDEO_IDENTITY,
        "generation": resolve_worker.kwargs["generation"], "transport_source": source,
    })

    harness._video_backend.set_dual_mode.assert_called_once_with(None)
    deferred_events = [
        e for e in harness._diagnostics_events if e[1] == "plex_video_gpu_deferred"
    ]
    assert deferred_events
    assert deferred_events[0][2]["details"]["reason"] == "stage3a_classic_only"


def test_video_playback_disabled_in_preferences_rejects_before_resolving(monkeypatch):
    harness = _harness(monkeypatch)
    harness.video_playback_enabled = False
    harness._begin_playback_attempt(VIDEO_IDENTITY, MediaType.VIDEO, "test_dispatch")
    result = harness._play_plex_video_path_direct(VIDEO_IDENTITY)
    assert result is False
    assert _FakeResolveWorker.instances == []


# -- Stage 3A-r2 real-device defect: recovery ladder vs. Plex identities -----

def test_begin_playback_recovery_refuses_a_plex_current_path(monkeypatch):
    # Defense in depth for the same real-device bug covered at the
    # _check_playback_health level (test_pending_next_hostile_ordering.py):
    # no caller may run _try_recovery_backend's Local-file fallback ladder
    # (raw BASS_StreamCreateFile / VLC media_new / miniaudio.load) against
    # a synthetic plex:// identity -- none of those loaders can open it,
    # and a "successful" VLC open of the bogus URI leaves
    # _temporary_backend_override stuck on "vlc" even after the real Plex
    # BASS load succeeds moments later, which is what produced both the
    # double "Switch to BASS" prompt / premature auto-advance and the dead
    # live FFT visualiser on Bill's real r2 acceptance run.
    harness = _harness(monkeypatch)
    harness.current_path = AUDIO_IDENTITY
    harness._try_recovery_backend = MagicMock(
        name="_try_recovery_backend", side_effect=AssertionError(
            "must never be reached for a Plex identity"
        ),
    )

    result = harness._begin_playback_recovery("startup-stall", "bass")

    assert result is False
    harness._try_recovery_backend.assert_not_called()
    assert harness._temporary_backend_override is None
    assert harness._stop_all_calls == 0
