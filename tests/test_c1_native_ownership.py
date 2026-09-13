"""Playback stability hardening, Phase C1 (native audio backend
ownership, 2026-09-10): the required hostile-ownership test matrix
(design review sections 9/12).

Scope note: this file covers the scenarios that need a DEDICATED test
because no existing file frames them in native-ownership terms
(commit_prepared/discard call counts on the physical player, not just
feature-level state). The remaining scenarios from the matrix already
have real, passing coverage elsewhere and are not duplicated here:

    E (Video->Audio candidate becomes stale, no inactive-player
       mutation) -- tests/test_mixed_media_audio_video_transitions.py::
       test_manual_next_during_video_to_audio_preparation_abandons_stale_load
       (strengthened this round with an explicit
       commit_prepared_calls/discarded assertion).
    F (FFT reads race a candidate completing) and
    L (real QThread BassStreamPrepareWorker, candidate survives queued
       signal delivery, GUI commits, FFT/seek/volume keep working) --
       tests/test_bass_stream_prepare_worker_real.py (real bass.dll).
    H (no async C1 worker owns a live player instance) --
       tests/test_player_load_worker.py's structural tests, plus
       tests/test_mixed_media_audio_video_transitions.py's real-QThread
       section (`assert not hasattr(worker, "player")`).

This file's scenarios (A, B, C, D, G) reuse the DispatchHarness/
_crossfade_window fixtures from test_plex_stage3a_dispatch.py and
test_track_transition_mode.py -- the same real, unbound PlayerWindow
methods, no isolated re-implementation of the logic under test.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from billsmusic.media_type import MediaType
from billsmusic.plex_transport import PlexTransportSource
from billsmusic.window import PlayerWindow

from test_plex_stage3a_dispatch import (
    AUDIO_IDENTITY,
    _FakeAudioLoadWorker,
    _FakePreparedCandidate,
    _FakeResolveWorker,
    _harness,
)
from test_track_transition_mode import (
    _FakeBassEngine as _CrossfadeFakeBassEngine,
    _FakeCrossfadePlayer,
    _FakeLoadWorker as _FakeCrossfadeLoadWorker,
    _FakePreparedCandidate as _FakeCrossfadeCandidate,
    _crossfade_window,
)


# ---------------------------------------------------------------------------
# A: Plex candidate A prepares; Local B becomes authoritative; A completes;
#    A's discard cannot stop/replace B.
# ---------------------------------------------------------------------------

def test_a_plex_candidate_discard_cannot_touch_a_locally_authoritative_player(monkeypatch):
    harness = _harness(monkeypatch)
    harness._begin_playback_attempt(AUDIO_IDENTITY, MediaType.AUDIO, "attempt_a_plex")
    harness._play_plex_audio_path_direct(AUDIO_IDENTITY)
    resolve_worker = _FakeResolveWorker.instances[0]
    source = PlexTransportSource(identity=AUDIO_IDENTITY, transport_url="http://x/y.mp3")
    resolve_worker.finished_result.emit({
        "success": True, "identity": AUDIO_IDENTITY,
        "generation": resolve_worker.kwargs["generation"], "transport_source": source,
    })
    load_worker = _FakeAudioLoadWorker.instances[0]

    # Local track B becomes authoritative WHILE A's BASS candidate is
    # still in flight -- the exact mechanism _play_path_direct uses for
    # every real selection.
    attempt_b = harness._begin_playback_attempt(
        "Y:/Music/Local.flac", MediaType.AUDIO, "attempt_b_local",
    )
    # B's own hypothetical commit already happened on its own physical
    # slot -- simulate that bass_player is now "playing B" by recording
    # a baseline call count before A's stale candidate arrives.
    commits_before = harness.bass_player.commit_prepared.call_count

    candidate_a = _FakePreparedCandidate(AUDIO_IDENTITY)
    load_worker.prepared.emit(load_worker.token, AUDIO_IDENTITY, candidate_a)

    assert candidate_a.discarded is True
    assert harness.bass_player.commit_prepared.call_count == commits_before  # A never committed
    harness.bass_player.stop.assert_not_called()  # A's discard never touches bass_player at all
    harness.bass_player.play.assert_not_called()
    assert harness._current_playback_attempt is attempt_b


# ---------------------------------------------------------------------------
# B: Plex A creating; Stop; A completes; A only frees its own candidate.
# ---------------------------------------------------------------------------

def test_b_stop_during_plex_prepare_then_late_completion_only_frees_its_own_candidate(monkeypatch):
    harness = _harness(monkeypatch)
    harness._begin_playback_attempt(AUDIO_IDENTITY, MediaType.AUDIO, "attempt_a")
    harness._play_plex_audio_path_direct(AUDIO_IDENTITY)
    resolve_worker = _FakeResolveWorker.instances[0]
    source = PlexTransportSource(identity=AUDIO_IDENTITY, transport_url="http://x/y.mp3")
    resolve_worker.finished_result.emit({
        "success": True, "identity": AUDIO_IDENTITY,
        "generation": resolve_worker.kwargs["generation"], "transport_source": source,
    })
    load_worker = _FakeAudioLoadWorker.instances[0]

    harness._cancel_current_playback_attempt("stop_playback")
    assert harness._current_playback_attempt is None

    candidate = _FakePreparedCandidate(AUDIO_IDENTITY)
    load_worker.prepared.emit(load_worker.token, AUDIO_IDENTITY, candidate)

    # No stream starts. The candidate frees only itself.
    assert candidate.discarded is True
    harness.bass_player.commit_prepared.assert_not_called()
    harness.bass_player.play.assert_not_called()
    harness.bass_player.stop.assert_not_called()


# ---------------------------------------------------------------------------
# C: Crossfade candidate targets physical inactive player P2; the topology
#    changes (a promotion happens) before the candidate completes; the
#    candidate cannot commit into P2's new role.
# ---------------------------------------------------------------------------

def test_c_crossfade_candidate_cannot_commit_after_its_target_slot_is_promoted(monkeypatch):
    outgoing = _FakeCrossfadePlayer(length=200.0, pos=193.0, physical_id="bass-A")
    incoming = _FakeCrossfadePlayer(physical_id="bass-B")  # P2 -- the inactive target
    window = _crossfade_window(outgoing, incoming)
    monkeypatch.setattr("billsmusic.window.BassStreamPrepareWorker", _FakeCrossfadeLoadWorker)
    monkeypatch.setattr("billsmusic.window._BassEngine", _CrossfadeFakeBassEngine)
    _FakeCrossfadeLoadWorker.created = []

    PlayerWindow._start_miniaudio_crossfade_to(window, "next.flac", immediate=False)
    worker = _FakeCrossfadeLoadWorker.created[0]

    # P2 (incoming/bass-B) gets promoted to active before the candidate
    # completes -- e.g. a different, already-in-flight crossfade finished
    # first. _promote_inactive_player is the ONLY real seam that can do
    # this; calling it directly here simulates that real event.
    window._promote_inactive_player(reason="test_unrelated_promotion")
    assert window.simple_player is incoming  # P2 is now playing live audio

    candidate = _FakeCrossfadeCandidate("next.flac")
    worker.prepared.emit(worker.token, "next.flac", candidate)

    # The candidate (targeting P2 AS INACTIVE) must not commit now that
    # P2 occupies the active role -- discarded instead.
    assert candidate.discarded is True
    assert incoming.commit_prepared_calls == 0
    assert incoming.played is False


# ---------------------------------------------------------------------------
# D: An outstanding BASS candidate is in flight while the backend switches
#    to miniaudio -- it must never commit into the newly-selected backend's
#    slot.
# ---------------------------------------------------------------------------

def test_d_outstanding_bass_candidate_cannot_commit_after_backend_switches_to_miniaudio(monkeypatch):
    outgoing = _FakeCrossfadePlayer(length=200.0, pos=193.0, physical_id="bass-A")
    incoming = _FakeCrossfadePlayer(physical_id="bass-B")
    window = _crossfade_window(outgoing, incoming)
    monkeypatch.setattr("billsmusic.window.BassStreamPrepareWorker", _FakeCrossfadeLoadWorker)
    monkeypatch.setattr("billsmusic.window._BassEngine", _CrossfadeFakeBassEngine)
    _FakeCrossfadeLoadWorker.created = []

    PlayerWindow._start_miniaudio_crossfade_to(window, "next.flac", immediate=False)
    worker = _FakeCrossfadeLoadWorker.created[0]

    # Backend switches to miniaudio while the BASS candidate is still
    # preparing -- e.g. a recovery fallback, or an explicit preference
    # change. Re-pin via the real seam.
    miniaudio_active = _FakeCrossfadePlayer(physical_id="miniaudio-A")
    miniaudio_inactive = _FakeCrossfadePlayer(physical_id="miniaudio-B")
    window.miniaudio_player = miniaudio_active
    window.miniaudio_inactive_player = miniaudio_inactive
    window.builtin_backend = "miniaudio"
    window._set_player_topology(miniaudio_active, miniaudio_inactive, reason="test_backend_switch")

    candidate = _FakeCrossfadeCandidate("next.flac")
    worker.prepared.emit(worker.token, "next.flac", candidate)

    assert candidate.discarded is True
    assert incoming.commit_prepared_calls == 0  # the old BASS inactive slot
    assert miniaudio_inactive.commit_prepared_calls == 0  # nor the new miniaudio one


# ---------------------------------------------------------------------------
# G: Rapid prepare A / prepare B / Stop / prepare C, completing out of
#    order (B, A, C) -- only C may ever commit.
# ---------------------------------------------------------------------------

def test_g_rapid_prepare_a_b_stop_c_completing_out_of_order_only_c_commits(monkeypatch):
    harness = _harness(monkeypatch)

    def _dispatch_and_resolve(reason):
        harness._begin_playback_attempt(AUDIO_IDENTITY, MediaType.AUDIO, reason)
        harness._play_plex_audio_path_direct(AUDIO_IDENTITY)
        resolve_worker = _FakeResolveWorker.instances[-1]
        source = PlexTransportSource(identity=AUDIO_IDENTITY, transport_url="http://x/y.mp3")
        resolve_worker.finished_result.emit({
            "success": True, "identity": AUDIO_IDENTITY,
            "generation": resolve_worker.kwargs["generation"], "transport_source": source,
        })
        return _FakeAudioLoadWorker.instances[-1]

    load_a = _dispatch_and_resolve("attempt_a")
    load_b = _dispatch_and_resolve("attempt_b")  # supersedes A
    harness._cancel_current_playback_attempt("stop_playback")  # Stop
    load_c = _dispatch_and_resolve("attempt_c")

    candidate_b = _FakePreparedCandidate(AUDIO_IDENTITY)
    candidate_a = _FakePreparedCandidate(AUDIO_IDENTITY)
    candidate_c = _FakePreparedCandidate(AUDIO_IDENTITY)

    # Deliberately out of order: B, then A, then C.
    load_b.prepared.emit(load_b.token, AUDIO_IDENTITY, candidate_b)
    load_a.prepared.emit(load_a.token, AUDIO_IDENTITY, candidate_a)
    load_c.prepared.emit(load_c.token, AUDIO_IDENTITY, candidate_c)

    assert candidate_a.discarded is True
    assert candidate_b.discarded is True
    assert candidate_c.discarded is False  # committed, not discarded
    harness.bass_player.commit_prepared.assert_called_once_with(candidate_c)
    harness.bass_player.play.assert_called_once()
