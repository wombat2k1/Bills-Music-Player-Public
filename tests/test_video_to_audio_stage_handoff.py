"""Stage 3A real-device defect: Plex VIDEO playing -> Plex MP3 from Up Next
takes over at natural end -> audio plays correctly, but the right-hand
display stays completely black.

Real diagnostics from that session (e622155c, 12:03:31-12:03:38) proved
the cause. The display page DID switch back to the visualiser, and the
native embedded video container WAS hidden (video_host_visibility_changed
page=normal, embedded_container_visible=false; separately confirmed on the
real Windows platform with Win32 IsWindowVisible and screen pixels). What
stayed up was the Phase 1 transition COVER: the natural-end transition's
switch point sampled the incoming media type straight after the queue
advance, but Plex audio is dispatched asynchronously, so the type was
still VIDEO. The cover waited for a video media_ready() that an audio
track never sends, exhausted its bounded wait, and by design stayed up
indefinitely over the stage.

These tests drive the real pieces end to end: PlayerWindow's own
_on_video_end_of_media, a real VideoTransitionManager and
VideoTransitionOverlay over a real QStackedWidget stage, the real
_advance_video_transition / _stop_video_for_audio_transition / page
helpers / _detach_video_from_party_mode / _route_video_output, and the
real asynchronous Plex audio dispatch (_play_plex_audio_path_direct ->
_start_plex_playback_resolve -> _on_plex_playback_resolved ->
_start_plex_audio_playback) from test_plex_stage3a_dispatch's harness.
Local audio is modelled exactly as _play_path_direct performs it:
synchronously, stopping video before returning.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtWidgets
from PyQt6.QtTest import QTest

from billsmusic.media_type import MediaType, classify_path
from billsmusic.playback_attempt import PlaybackAttemptState
from billsmusic.plex_identity import is_plex_identity
from billsmusic.plex_transport import PlexTransportSource
from billsmusic.video_transition import (
    TransitionState,
    VideoTransitionManager,
    VideoTransitionPreferences,
)
from billsmusic.window import PlayerWindow

from test_plex_stage3a_dispatch import (
    _FakeAudioLoadWorker,
    _FakePreparedCandidate,
    _FakeResolveWorker,
    _harness,
)

PLEX_VIDEO = "plex://server-1/99.mp4"
PLEX_AUDIO = "plex://server-1/42.mp3"
LOCAL_VIDEO = "F:/videos/clip.mp4"
LOCAL_AUDIO = "F:/music/track.mp3"

REAL_METHODS = (
    "_stop_video_for_audio_transition", "_show_normal_display_page",
    "_show_video_output_page", "_show_video_loading_page",
    "_record_video_host_visibility_change", "_detach_video_from_party_mode",
    "_route_video_output", "_video_transition_host",
    "_advance_video_transition", "_on_video_end_of_media",
    "_record_video_transition_event", "_finish_video_end_without_next",
    "next_track",
)


class _StageBackend:
    """QtVideoPlaybackBackend at the window boundary: ONE persistent
    embedded container (a real QWidget), reparented by attach_output() the
    same way the real _do_attach() does, counting every container ever
    created so a test can prove none are added."""

    def __init__(self):
        self._embedded_container = QtWidgets.QWidget()
        self._embedded_container.setStyleSheet("background:#000000;")
        self.containers_created = 1
        self.stop_calls = 0

    def attach_output(self, host):
        container = self._embedded_container
        parent = container.parentWidget()
        if parent is not None and parent.layout() is not None:
            parent.layout().removeWidget(container)
        container.setParent(None)
        if host is not None and host.layout() is not None:
            host.layout().addWidget(container)
            host.layout().activate()

    def schedule_output_geometry_sync(self):
        pass

    def stop(self):
        self.stop_calls += 1

    def is_dual_mode(self):
        return False


class _PartyMode(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        layout = QtWidgets.QVBoxLayout(self)
        self.stack = QtWidgets.QStackedWidget()
        self.video_widget = QtWidgets.QWidget()
        QtWidgets.QVBoxLayout(self.video_widget).setContentsMargins(0, 0, 0, 0)
        self.stack.addWidget(QtWidgets.QLabel("party visualiser"))
        self.stack.addWidget(self.video_widget)
        layout.addWidget(self.stack)
        self._video_active = False
        self.returned_to_normal = 0

    def return_to_normal_layout(self):
        self.returned_to_normal += 1
        self._video_active = False


def _pump(ms=60, rounds=4):
    app = QtWidgets.QApplication.instance()
    for _ in range(rounds):
        QTest.qWait(ms)
        app.processEvents()


def _pump_until(predicate, ms=2500):
    app = QtWidgets.QApplication.instance()
    waited = 0
    while waited < ms:
        if predicate():
            return True
        QTest.qWait(20)
        app.processEvents()
        waited += 20
    return predicate()


_APP = None


def _app():
    # Held at module level: a QApplication created without a lasting Python
    # reference is garbage-collected at once, and the next widget operation
    # then aborts the whole process natively.
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _stage(monkeypatch, *, party_mode=None):
    _app()
    h = _harness(monkeypatch)
    root = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(root)
    stack = QtWidgets.QStackedWidget()
    visualiser = QtWidgets.QLabel("VISUALISER")
    loading = QtWidgets.QLabel("Loading video...")
    video_output = QtWidgets.QWidget()
    QtWidgets.QVBoxLayout(video_output).setContentsMargins(0, 0, 0, 0)
    for page in (visualiser, loading, video_output):
        stack.addWidget(page)
    layout.addWidget(stack)
    root.resize(800, 500)
    root.show()
    h._root = root
    h.right_display_stack = stack
    h.visualiser_frame = visualiser
    h._normal_display_page = visualiser
    h._video_loading_page = loading
    h.video_output_widget = video_output
    h._video_output_page = video_output
    h.party_mode = party_mode
    h._video_backend = _StageBackend()
    h._video_backend.attach_output(video_output)
    h._video_fullscreen = False
    h._mixed_transition_state = "idle"
    h.lifecycle_reasons = []
    h._refresh_visualiser_lifecycle = lambda reason: h.lifecycle_reasons.append(reason)
    h._dual_transition_committed_state_value = lambda: None
    h._resume_deferred_queue_analysis = lambda: None
    h._mixed_transition_owns_video_boundary = lambda: False
    h._activate_track_ui = lambda index, path: setattr(h, "current_path", path)
    h._exit_video_fullscreen = lambda: None
    h._sync_now_playing_overlay_for_media_type = lambda: None
    h._announce_accessible_status = lambda _text: None
    for name in REAL_METHODS:
        setattr(h, name, getattr(PlayerWindow, name).__get__(h))

    h.next_path = None
    h.next_track_reasons = []

    def _next_track(reason):
        h.next_track_reasons.append(reason)
        path = h.next_path
        media = classify_path(path)
        h._begin_playback_attempt(path, media, "play_path_direct")
        if is_plex_identity(path):
            # The real, asynchronous Plex audio dispatch: returns with the
            # resolve still pending and the media type still VIDEO.
            h._play_plex_audio_path_direct(path)
        else:
            # Exactly what _play_path_direct does for local audio: stop
            # video synchronously, then start the new playback generation.
            h._stop_video_for_audio_transition()
            h._playback_generation += 1
            h.current_path = path

    h._next_track = _next_track
    manager = VideoTransitionManager(
        None,
        h._video_transition_host,
        h._advance_video_transition,
        lambda: MediaType.AUDIO,
        h._record_video_transition_event,
    )
    manager.configure(VideoTransitionPreferences(style="Fade Black", duration_seconds=0.3))
    h._video_transition_manager = manager
    return h


def _teardown(h):
    h._video_transition_manager.shutdown()
    if h.party_mode is not None:
        h.party_mode.close()
    h._root.close()


def _video_playing(h, path):
    h._current_media_type = MediaType.VIDEO
    h.current_path = path
    host = h.video_output_widget
    if h.party_mode is not None and h.party_mode.isVisible():
        host = h.party_mode.video_widget
        h.party_mode._video_active = True
        h.party_mode.stack.setCurrentWidget(host)
    if h._video_backend._embedded_container.parentWidget() is not host:
        h._video_backend.attach_output(host)
    h._show_video_output_page()
    _pump(rounds=2)
    assert h._video_backend._embedded_container.isVisible(), "video surface not showing"


def _natural_end_to(h, path):
    h.next_path = path
    h._on_video_end_of_media()
    assert _pump_until(
        lambda: h._video_transition_manager.state
        in (TransitionState.SWITCHING, TransitionState.INCOMING, TransitionState.IDLE)
        and h._video_transition_manager.state != TransitionState.OUTGOING
    ), "outgoing transition never reached its switch point"


def _complete_plex_audio(h):
    resolve = _FakeResolveWorker.instances[-1]
    source = PlexTransportSource(identity=h.next_path, transport_url="http://plex-host:32400/x.mp3")
    resolve.finished_result.emit({
        "success": True, "identity": h.next_path,
        "generation": resolve.kwargs["generation"], "transport_source": source,
    })
    load = _FakeAudioLoadWorker.instances[-1]
    load.prepared.emit(load.token, h.next_path, _FakePreparedCandidate(h.next_path))


def _ops(h, operation):
    return [kw.get("details") or {} for _c, op, kw in h._diagnostics_events if op == operation]


def _assert_audio_owns_the_stage(h, path):
    container = h._video_backend._embedded_container
    manager = h._video_transition_manager
    assert _pump_until(lambda: manager.state == TransitionState.IDLE), (
        f"transition never finished (state={manager.state.value})"
    )
    _pump(rounds=2)
    # The defect itself: the painted cover left standing over the stage.
    assert not manager._overlay.isVisible(), "transition cover left covering the stage"
    assert h._current_media_type == MediaType.AUDIO
    assert h.current_path == path
    assert h.right_display_stack.currentWidget() is h._normal_display_page
    assert not h.video_output_widget.isVisible()
    assert not container.isVisible()
    assert h.visualiser_frame.isVisible()
    assert not h._video_loading_page.isVisible()
    assert h.lifecycle_reasons and h.lifecycle_reasons[-1] == "video_hidden"
    visibility = _ops(h, "video_host_visibility_changed")
    assert visibility and visibility[-1]["page"] == "normal"
    assert visibility[-1]["embedded_container_visible"] is False
    if is_plex_identity(path):
        # _analyzer_tick's live_plex_bass predicate holds for the new track.
        assert not h.cast_active and h._use_bass_backend()


# ---------------------------------------------------------------------------

def test_plex_video_to_plex_mp3_natural_end_lifts_the_cover(monkeypatch):
    h = _stage(monkeypatch)
    try:
        _video_playing(h, PLEX_VIDEO)
        _natural_end_to(h, PLEX_AUDIO)
        manager = h._video_transition_manager
        # Resolve still pending: the cover is up and audio hasn't taken over.
        assert manager.state == TransitionState.SWITCHING
        assert manager._overlay.isVisible()
        assert h._current_media_type == MediaType.VIDEO

        _complete_plex_audio(h)

        _assert_audio_owns_the_stage(h, PLEX_AUDIO)
        events = [op for _c, op, _kw in h._diagnostics_events]
        assert "video_transition_incoming_media_superseded" in events
        assert "video_transition_complete" in events
    finally:
        _teardown(h)


def test_plex_video_to_plex_mp3_after_the_cover_wait_is_exhausted(monkeypatch):
    # Bill's exact log shape: timeouts, extensions, then exhausted -- after
    # which the cover stays up by design until something lifts it.
    monkeypatch.setattr(VideoTransitionManager, "READY_TIMEOUT_MS", 30)
    h = _stage(monkeypatch)
    try:
        _video_playing(h, PLEX_VIDEO)
        _natural_end_to(h, PLEX_AUDIO)
        assert _pump_until(lambda: bool(_ops(h, "video_transition_incoming_wait_exhausted")))
        assert h._video_transition_manager.state == TransitionState.SWITCHING

        _complete_plex_audio(h)

        _assert_audio_owns_the_stage(h, PLEX_AUDIO)
    finally:
        _teardown(h)


def test_plex_video_to_local_audio(monkeypatch):
    h = _stage(monkeypatch)
    try:
        _video_playing(h, PLEX_VIDEO)
        _natural_end_to(h, LOCAL_AUDIO)
        _assert_audio_owns_the_stage(h, LOCAL_AUDIO)
        # Synchronous path: nothing was stranded, so nothing to supersede.
        assert _ops(h, "video_transition_incoming_media_superseded") == []
    finally:
        _teardown(h)


def test_local_video_to_plex_audio(monkeypatch):
    h = _stage(monkeypatch)
    try:
        _video_playing(h, LOCAL_VIDEO)
        _natural_end_to(h, PLEX_AUDIO)
        assert h._video_transition_manager._overlay.isVisible()
        _complete_plex_audio(h)
        _assert_audio_owns_the_stage(h, PLEX_AUDIO)
    finally:
        _teardown(h)


def test_local_video_to_local_audio(monkeypatch):
    h = _stage(monkeypatch)
    try:
        _video_playing(h, LOCAL_VIDEO)
        _natural_end_to(h, LOCAL_AUDIO)
        _assert_audio_owns_the_stage(h, LOCAL_AUDIO)
        assert _ops(h, "video_transition_incoming_media_superseded") == []
    finally:
        _teardown(h)


def test_party_mode_plex_video_to_plex_mp3_returns_the_surface_and_lifts_the_cover(monkeypatch):
    _app()
    party = _PartyMode()
    party.show()
    h = _stage(monkeypatch, party_mode=party)
    try:
        container = h._video_backend._embedded_container
        _video_playing(h, PLEX_VIDEO)
        assert container.parentWidget() is party.video_widget

        _natural_end_to(h, PLEX_AUDIO)
        manager = h._video_transition_manager
        assert manager._overlay.isVisible()
        _complete_plex_audio(h)
        assert _pump_until(lambda: manager.state == TransitionState.IDLE)
        _pump(rounds=2)

        assert not manager._overlay.isVisible()
        assert h._current_media_type == MediaType.AUDIO
        # Existing Party Mode behaviour preserved: the one surface goes back
        # to the main host, and Party Mode returns to its normal layout.
        assert container.parentWidget() is h.video_output_widget
        assert h._video_backend._embedded_container is container
        assert party.returned_to_normal >= 1
        assert not container.isVisible()
        assert h.right_display_stack.currentWidget() is h._normal_display_page
    finally:
        _teardown(h)


def test_repeated_plex_video_audio_handoffs_never_strand_the_cover(monkeypatch):
    h = _stage(monkeypatch)
    try:
        container = h._video_backend._embedded_container
        for cycle in range(10):
            video = f"plex://server-1/{900 + cycle}.mp4"
            audio = f"plex://server-1/{100 + cycle}.mp3"
            _video_playing(h, video)
            assert container.isVisible(), f"surface missing on video, cycle {cycle}"
            _natural_end_to(h, audio)
            _complete_plex_audio(h)
            _assert_audio_owns_the_stage(h, audio)
            # Same single surface, same host, no extra containers.
            assert h._video_backend._embedded_container is container
            assert h._video_backend.containers_created == 1
            assert container.parentWidget() is h.video_output_widget
        assert len(_ops(h, "video_transition_incoming_media_superseded")) == 10
        assert len(_ops(h, "video_transition_complete")) == 10
    finally:
        _teardown(h)


# ---------------------------------------------------------------------------
# Async Plex resolve FAILURE while the cover is up (Stage 3A terminal path).
#
# Every test below reaches the covered SWITCHING state through the real
# asynchronous dispatch -- the resolve worker has started and
# _playback_generation has already been bumped -- and only THEN has the
# worker return success=False. The synchronous early failures in
# _start_plex_playback_resolve (not connected / casting) return before the
# generation changes and never reach this branch.

def _fail_plex_resolve(h, reason="server unreachable"):
    resolve = _FakeResolveWorker.instances[-1]
    resolve.finished_result.emit({
        "success": False, "identity": h.next_path, "reason": reason,
        "generation": resolve.kwargs["generation"],
    })


def _assert_covered_awaiting_async_resolve(h, generation_before):
    manager = h._video_transition_manager
    assert manager.state == TransitionState.SWITCHING
    assert manager._overlay.isVisible()
    assert h._current_media_type == MediaType.VIDEO
    assert len(_FakeResolveWorker.instances) == 1
    assert h._playback_generation == generation_before + 1
    assert h.pending_next is True


def _assert_failure_diagnostics(h, trigger):
    failed = _ops(h, "video_transition_incoming_media_failed")
    assert len(failed) == 1
    assert failed[0]["trigger"] == trigger
    assert failed[0]["reason"] == "plex_resolve_failed"
    assert len(_ops(h, "video_transition_cancelled")) == 1
    assert _ops(h, "video_transition_complete") == []
    assert _ops(h, "video_transition_incoming_media_superseded") == []
    assert _ops(h, "video_transition_incoming_media_ready") == []


def _assert_natural_end_failure_restores_stage(h):
    manager = h._video_transition_manager
    container = h._video_backend._embedded_container
    assert manager.state == TransitionState.IDLE
    _pump(rounds=2)
    assert not manager._overlay.isVisible(), "transition cover stranded after resolve failure"
    assert h.right_display_stack.currentWidget() is h._normal_display_page
    assert h.visualiser_frame.isVisible()
    assert not h.video_output_widget.isVisible()
    assert not container.isVisible()
    assert h._video_backend.stop_calls == 1
    assert h._current_media_type == MediaType.AUDIO
    assert h.pending_next is False
    assert h._current_playback_attempt.state == PlaybackAttemptState.FAILED
    assert h.next_track_reasons == ["video-ended"], "queue advanced a second time"
    assert len(_FakeResolveWorker.instances) == 1
    assert _FakeAudioLoadWorker.instances == []
    assert any("Plex playback failed" in m for m in h._status_messages)
    _assert_failure_diagnostics(h, "automatic")


def test_plex_video_natural_end_to_plex_audio_async_resolve_failure(monkeypatch):
    h = _stage(monkeypatch)
    try:
        _video_playing(h, PLEX_VIDEO)
        generation_before = h._playback_generation
        _natural_end_to(h, PLEX_AUDIO)
        _assert_covered_awaiting_async_resolve(h, generation_before)

        _fail_plex_resolve(h)

        _assert_natural_end_failure_restores_stage(h)
        # Stays terminal: nothing later re-covers or re-advances.
        _pump(ms=100, rounds=3)
        assert h._video_transition_manager.state == TransitionState.IDLE
        assert not h._video_transition_manager._overlay.isVisible()
        assert h.next_track_reasons == ["video-ended"]
    finally:
        _teardown(h)


def test_local_video_natural_end_to_plex_audio_async_resolve_failure(monkeypatch):
    h = _stage(monkeypatch)
    try:
        _video_playing(h, LOCAL_VIDEO)
        generation_before = h._playback_generation
        _natural_end_to(h, PLEX_AUDIO)
        _assert_covered_awaiting_async_resolve(h, generation_before)

        _fail_plex_resolve(h)

        _assert_natural_end_failure_restores_stage(h)
    finally:
        _teardown(h)


def test_manual_next_from_video_to_plex_audio_async_resolve_failure(monkeypatch):
    h = _stage(monkeypatch)
    try:
        container = h._video_backend._embedded_container
        _video_playing(h, PLEX_VIDEO)
        generation_before = h._playback_generation
        h.next_path = PLEX_AUDIO
        h.next_track()
        manager = h._video_transition_manager
        assert _pump_until(lambda: manager.state == TransitionState.SWITCHING)
        _assert_covered_awaiting_async_resolve(h, generation_before)
        assert h.next_track_reasons == ["manual-next"]

        _fail_plex_resolve(h)

        assert manager.state == TransitionState.IDLE
        _pump(rounds=2)
        assert not manager._overlay.isVisible(), "cover stranded after manual-next failure"
        # The outgoing video was never stopped: it is still the presented
        # playback underneath, exactly as before the Next press.
        assert h._video_backend.stop_calls == 0
        assert h._current_media_type == MediaType.VIDEO
        assert h.current_path == PLEX_VIDEO
        assert h.right_display_stack.currentWidget() is h._video_output_page
        assert container.isVisible()
        assert h.pending_next is False
        assert h._current_playback_attempt.state == PlaybackAttemptState.FAILED
        assert h.next_track_reasons == ["manual-next"], "nested/second Next"
        assert len(_FakeResolveWorker.instances) == 1
        assert any("Plex playback failed" in m for m in h._status_messages)
        _assert_failure_diagnostics(h, "manual")
    finally:
        _teardown(h)


def test_plex_resolve_failure_after_the_cover_wait_is_exhausted(monkeypatch):
    monkeypatch.setattr(VideoTransitionManager, "READY_TIMEOUT_MS", 30)
    h = _stage(monkeypatch)
    try:
        _video_playing(h, PLEX_VIDEO)
        generation_before = h._playback_generation
        _natural_end_to(h, PLEX_AUDIO)
        assert _pump_until(lambda: bool(_ops(h, "video_transition_incoming_wait_exhausted")))
        assert len(_ops(h, "video_transition_incoming_wait_extended")) == 2
        _assert_covered_awaiting_async_resolve(h, generation_before)

        _fail_plex_resolve(h)

        _assert_natural_end_failure_restores_stage(h)
    finally:
        _teardown(h)


def test_plex_resolve_failure_with_no_transition_pending_is_a_no_op(monkeypatch):
    h = _stage(monkeypatch)
    try:
        # Transitions disabled: the natural end advances straight through
        # _next_track with the manager IDLE throughout.
        h._video_transition_manager.configure(VideoTransitionPreferences(enabled=False))
        _video_playing(h, PLEX_VIDEO)
        _natural_end_to(h, PLEX_AUDIO)
        manager = h._video_transition_manager
        assert manager.state == TransitionState.IDLE
        assert manager.incoming_media_failed("probe") is None

        _fail_plex_resolve(h)

        assert manager.state == TransitionState.IDLE
        assert _ops(h, "video_transition_incoming_media_failed") == []
        assert _ops(h, "video_transition_cancelled") == []
        # The helper itself mutated no presentation.
        assert h._video_backend.stop_calls == 0
        assert h.pending_next is False
    finally:
        _teardown(h)


def test_plex_resolve_failure_after_transition_already_cancelled_is_harmless(monkeypatch):
    h = _stage(monkeypatch)
    try:
        _video_playing(h, PLEX_VIDEO)
        _natural_end_to(h, PLEX_AUDIO)
        manager = h._video_transition_manager
        assert manager.state == TransitionState.SWITCHING
        manager.cancel("test_external_cancel")
        assert manager.state == TransitionState.IDLE
        pages_before = len(_ops(h, "video_host_visibility_changed"))

        _fail_plex_resolve(h)

        assert manager.state == TransitionState.IDLE
        assert not manager._overlay.isVisible()
        assert _ops(h, "video_transition_incoming_media_failed") == []
        assert len(_ops(h, "video_transition_cancelled")) == 1
        assert h._video_backend.stop_calls == 0
        assert len(_ops(h, "video_host_visibility_changed")) == pages_before
        assert h.next_track_reasons == ["video-ended"]
    finally:
        _teardown(h)


def test_plex_resolve_failure_after_transition_already_completed_is_harmless(monkeypatch):
    h = _stage(monkeypatch)
    try:
        _video_playing(h, PLEX_VIDEO)
        _natural_end_to(h, LOCAL_AUDIO)
        _assert_audio_owns_the_stage(h, LOCAL_AUDIO)
        manager = h._video_transition_manager
        stops_before = h._video_backend.stop_calls
        pages_before = len(_ops(h, "video_host_visibility_changed"))

        assert manager.incoming_media_failed("plex_resolve_failed") is None
        h._terminalise_video_transition_for_failed_incoming("plex_resolve_failed")

        assert manager.state == TransitionState.IDLE
        assert _ops(h, "video_transition_incoming_media_failed") == []
        assert _ops(h, "video_transition_cancelled") == []
        assert h._video_backend.stop_calls == stops_before
        assert len(_ops(h, "video_host_visibility_changed")) == pages_before
        assert h.current_path == LOCAL_AUDIO
    finally:
        _teardown(h)


def test_party_mode_plex_video_natural_end_async_resolve_failure(monkeypatch):
    _app()
    party = _PartyMode()
    party.show()
    h = _stage(monkeypatch, party_mode=party)
    try:
        container = h._video_backend._embedded_container
        _video_playing(h, PLEX_VIDEO)
        assert container.parentWidget() is party.video_widget
        _natural_end_to(h, PLEX_AUDIO)
        manager = h._video_transition_manager
        assert manager.state == TransitionState.SWITCHING
        assert manager._overlay.isVisible()

        _fail_plex_resolve(h)

        assert manager.state == TransitionState.IDLE
        _pump(rounds=2)
        assert not manager._overlay.isVisible()
        assert container.parentWidget() is h.video_output_widget
        assert party.returned_to_normal >= 1
        assert not container.isVisible()
        assert h.right_display_stack.currentWidget() is h._normal_display_page
        assert h.next_track_reasons == ["video-ended"]
    finally:
        _teardown(h)
