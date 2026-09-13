"""Playback stability hardening, Phase A (BMP-002): PlaybackAttempt is a
single umbrella authority identity layered ABOVE every existing
specialist token (pending_next, _playback_generation,
_plex_audio_load_token, crossfade tokens, karaoke generations, video
transition IDs) -- none of those are removed or replaced here, they stay
in place as additional defence. See billsmusic/playback_attempt.py's
module docstring for the full invariant this file proves:

    Only the current PlaybackAttempt may make media authoritative. A
    superseded attempt's own async work may still finish harmlessly, but
    it must never start playback, change current_path/backend override,
    mark queue history, change current-track UI, trigger Next, or change
    playback_expected/pending progression state.

Reuses DispatchHarness/_FakeResolveWorker/_FakeAudioLoadWorker/_harness
from test_plex_stage3a_dispatch.py -- the same real, unbound
PlayerWindow methods, the same fake workers that record construction
args and never touch the network/a real QThread, just organised around
the attempt-authority scenarios specifically (hostile ordering/delays)
rather than dispatch-logic-in-general.

Scope note: this file covers exactly the tests in the playback-stability
audit's numbered list that are genuinely BMP-002 (attempt authority) --
1, 2, 3, and a scoped version of 12. Tests 4-11 belong to later phases
(queue-commit semantics/terminalisation = Phase B, native backend/worker
ownership = Phase C, queue identity = Phase D, GUI-thread I/O = Phase E)
and are deliberately not attempted here -- see CODEX_HANDOFF.md's
playback-stability roadmap for the full numbered mapping.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from billsmusic.media_type import MediaType
from billsmusic.playback_attempt import PlaybackAttemptState
from billsmusic.plex_transport import PlexTransportSource
from billsmusic.window import PlayerWindow

from test_plex_stage3a_dispatch import (
    AUDIO_IDENTITY,
    DispatchHarness,
    _FakeAudioLoadWorker,
    _FakePreparedCandidate,
    _harness,
)


def _superseded_events(harness):
    return [
        e for e in harness._diagnostics_events
        if e[1] in (
            "playback_attempt_superseded",
            "playback_attempt_stale_callback_rejected",
            "playback_attempt_id_missing",
        )
    ]


# -- 1: Stop during Plex resolve -- late resolver completes, nothing plays --

def test_stop_during_plex_resolve_makes_a_late_resolver_result_harmless(monkeypatch):
    harness = _harness(monkeypatch)
    attempt = harness._begin_playback_attempt(AUDIO_IDENTITY, MediaType.AUDIO, "test_select")
    result = harness._play_plex_audio_path_direct(AUDIO_IDENTITY)
    assert result is True
    from test_plex_stage3a_dispatch import _FakeResolveWorker
    resolve_worker = _FakeResolveWorker.instances[0]

    # STOP: absolute, synchronous -- current attempt -> CANCELLED, current
    # authoritative -> NONE, right here, before the late resolver result
    # ever arrives.
    harness._cancel_current_playback_attempt("stop_playback")
    assert harness._current_playback_attempt is None
    assert attempt.state == PlaybackAttemptState.CANCELLED

    source = PlexTransportSource(identity=AUDIO_IDENTITY, transport_url="http://x/y.mp3")
    resolve_worker.finished_result.emit({
        "success": True, "identity": AUDIO_IDENTITY,
        "generation": resolve_worker.kwargs["generation"], "transport_source": source,
    })

    # Nothing plays: no audio load worker was ever started for the
    # cancelled attempt's resolve result.
    assert _FakeAudioLoadWorker.instances == []
    assert harness._activate_track_ui_calls == []
    assert len(_superseded_events(harness)) == 1
    assert _superseded_events(harness)[0][2]["details"]["stage"] == "plex_resolve"


# -- 2: Stop during Plex audio load -- late load completes, nothing plays --

def test_stop_during_plex_audio_load_makes_a_late_load_result_harmless(monkeypatch):
    harness = _harness(monkeypatch)
    harness._begin_playback_attempt(AUDIO_IDENTITY, MediaType.AUDIO, "test_select")
    harness._play_plex_audio_path_direct(AUDIO_IDENTITY)
    from test_plex_stage3a_dispatch import _FakeResolveWorker
    resolve_worker = _FakeResolveWorker.instances[0]
    source = PlexTransportSource(identity=AUDIO_IDENTITY, transport_url="http://x/y.mp3")
    resolve_worker.finished_result.emit({
        "success": True, "identity": AUDIO_IDENTITY,
        "generation": resolve_worker.kwargs["generation"], "transport_source": source,
    })
    load_worker = _FakeAudioLoadWorker.instances[0]
    assert harness._current_playback_attempt.state == PlaybackAttemptState.STARTING

    # STOP arrives while the audio load is still in flight.
    harness._cancel_current_playback_attempt("stop_playback")
    assert harness._current_playback_attempt is None

    candidate = _FakePreparedCandidate(AUDIO_IDENTITY)
    load_worker.prepared.emit(load_worker.token, AUDIO_IDENTITY, candidate)

    # Nothing plays: bass_player.play()/.commit_prepared() were never
    # reached for the cancelled attempt's late load result -- Phase C1:
    # the candidate never touched bass_player in the first place, so the
    # only thing that happens to it is discard().
    assert candidate.discarded is True
    harness.bass_player.commit_prepared.assert_not_called()
    harness.bass_player.play.assert_not_called()
    harness.bass_player.stop.assert_not_called()
    stale = [e for e in _superseded_events(harness) if e[1] == "playback_attempt_stale_callback_rejected"]
    assert len(stale) == 1
    assert stale[0][2]["details"]["stage"] == "plex_audio_load"


# -- 3: Local audio supersedes an in-flight Plex load ------------------------

def test_local_selection_supersedes_an_in_flight_plex_load(monkeypatch):
    harness = _harness(monkeypatch)
    harness._begin_playback_attempt(AUDIO_IDENTITY, MediaType.AUDIO, "test_select")
    harness._play_plex_audio_path_direct(AUDIO_IDENTITY)
    from test_plex_stage3a_dispatch import _FakeResolveWorker
    resolve_worker = _FakeResolveWorker.instances[0]
    source = PlexTransportSource(identity=AUDIO_IDENTITY, transport_url="http://x/y.mp3")
    resolve_worker.finished_result.emit({
        "success": True, "identity": AUDIO_IDENTITY,
        "generation": resolve_worker.kwargs["generation"], "transport_source": source,
    })
    load_worker = _FakeAudioLoadWorker.instances[0]

    # The user selects a completely different, Local track WHILE the Plex
    # load is still in flight -- this is the exact mechanism
    # _play_path_direct uses for every real selection (Local or Plex):
    # merely beginning a new attempt immediately cancels the old one.
    local_attempt = harness._begin_playback_attempt(
        "Y:/Music/Local Track.flac", MediaType.AUDIO, "test_local_select",
    )
    assert harness._current_playback_attempt is local_attempt

    # The Plex load then completes -- it must be powerless to replace the
    # (hypothetically already-playing) Local track. Phase C1: its
    # candidate never touched bass_player -- only discard() runs.
    candidate = _FakePreparedCandidate(AUDIO_IDENTITY)
    load_worker.prepared.emit(load_worker.token, AUDIO_IDENTITY, candidate)

    assert candidate.discarded is True
    harness.bass_player.commit_prepared.assert_not_called()
    harness.bass_player.play.assert_not_called()
    harness.bass_player.stop.assert_not_called()
    assert harness._current_playback_attempt is local_attempt


# -- 12 (scoped to attempt authority): rapid hostile sequence ----------------

def test_rapid_abuse_sequence_only_the_final_attempt_is_ever_authoritative(monkeypatch):
    # Plex -> Local -> Stop -> Plex (again) -> Local (final), firing every
    # earlier attempt's async callback out of order and after the fact.
    # Only the LAST attempt begun may ever be authoritative at the end,
    # and every earlier attempt must be terminal (CANCELLED).
    harness = _harness(monkeypatch)

    attempt_plex_1 = harness._begin_playback_attempt(AUDIO_IDENTITY, MediaType.AUDIO, "plex_1")
    harness._play_plex_audio_path_direct(AUDIO_IDENTITY)
    from test_plex_stage3a_dispatch import _FakeResolveWorker
    resolve_1 = _FakeResolveWorker.instances[0]

    attempt_local_1 = harness._begin_playback_attempt(
        "Y:/Music/A.flac", MediaType.AUDIO, "local_1",
    )
    assert attempt_plex_1.state == PlaybackAttemptState.CANCELLED

    harness._cancel_current_playback_attempt("stop_playback")
    assert attempt_local_1.state == PlaybackAttemptState.CANCELLED
    assert harness._current_playback_attempt is None

    attempt_plex_2 = harness._begin_playback_attempt(AUDIO_IDENTITY, MediaType.AUDIO, "plex_2")
    harness._play_plex_audio_path_direct(AUDIO_IDENTITY)
    resolve_2 = _FakeResolveWorker.instances[1]

    attempt_local_final = harness._begin_playback_attempt(
        "Y:/Music/B.flac", MediaType.AUDIO, "local_final",
    )
    assert attempt_plex_2.state == PlaybackAttemptState.CANCELLED

    # Every earlier attempt's async work lands now, deliberately out of
    # the order it was started in.
    source = PlexTransportSource(identity=AUDIO_IDENTITY, transport_url="http://x/y.mp3")
    resolve_2.finished_result.emit({
        "success": True, "identity": AUDIO_IDENTITY,
        "generation": resolve_2.kwargs["generation"], "transport_source": source,
    })
    resolve_1.finished_result.emit({
        "success": True, "identity": AUDIO_IDENTITY,
        "generation": resolve_1.kwargs["generation"], "transport_source": source,
    })

    assert harness._current_playback_attempt is attempt_local_final
    assert attempt_local_final.state == PlaybackAttemptState.REQUESTED
    for stale_attempt in (attempt_plex_1, attempt_local_1, attempt_plex_2):
        assert stale_attempt.state == PlaybackAttemptState.CANCELLED
    # Neither stale resolve produced a live audio load worker.
    assert _FakeAudioLoadWorker.instances == []
    assert harness._activate_track_ui_calls == []


# -- 2026-09-10 audit correction: attempt_id=None is an invariant
# violation for a migrated async path, not a free pass -----------------
#
# _is_current_playback_attempt(None) still returns True (kept only for
# genuinely synchronous legacy callers that haven't been migrated yet --
# e.g. every direct-call test in this suite's sibling files that invokes
# a callback without threading an attempt through it deliberately).
# _require_current_playback_attempt, used by every MIGRATED async path
# (Plex resolve, Plex audio load, karaoke prepare, crossfade preload),
# does NOT extend that pass to None -- every real dispatch site captures
# a real attempt_id before starting its worker, so None reaching one of
# these callbacks means something is wired wrong, not that "no attempt
# applies here".

def test_migrated_callback_rejects_a_missing_attempt_id_as_an_invariant_violation(monkeypatch):
    harness = _harness(monkeypatch)
    harness._begin_playback_attempt(AUDIO_IDENTITY, MediaType.AUDIO, "test_select")
    harness._play_plex_audio_path_direct(AUDIO_IDENTITY)
    from test_plex_stage3a_dispatch import _FakeResolveWorker
    resolve_worker = _FakeResolveWorker.instances[0]
    source = PlexTransportSource(identity=AUDIO_IDENTITY, transport_url="http://x/y.mp3")

    # Simulates a hypothetical future call site that forgot to capture
    # and thread attempt_id through -- must be rejected, not silently
    # treated as authoritative just because nothing was captured.
    harness._on_plex_playback_resolved({
        "success": True, "identity": AUDIO_IDENTITY,
        "generation": resolve_worker.kwargs["generation"], "transport_source": source,
    }, None)

    assert harness._activate_track_ui_calls == []
    missing = [e for e in harness._diagnostics_events if e[1] == "playback_attempt_id_missing"]
    assert len(missing) == 1
    assert missing[0][2]["details"]["stage"] == "plex_resolve"


def test_require_current_playback_attempt_accepts_a_real_current_attempt(monkeypatch):
    # Control: the strict helper is not simply "always reject" -- a real,
    # currently-authoritative attempt_id passes it.
    harness = _harness(monkeypatch)
    attempt = harness._begin_playback_attempt(AUDIO_IDENTITY, MediaType.AUDIO, "test_select")
    assert harness._require_current_playback_attempt(attempt.attempt_id, "unit_test") is True
    assert _superseded_events(harness) == []
