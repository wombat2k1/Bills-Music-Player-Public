"""Playback stability hardening, Phase C1.1 (2026-09-11): a surgical
correction round after independent review of the fresh Phase C1
source. Two focused areas:

1. PlayerWindow._discard_prepared_candidate() must not silently ignore
   a native discard() failure -- it must record a safe diagnostic
   (never the candidate's own private content) and must never retry
   the native free.
2. PlayerTargetLease.backend_family must actually be validated by
   _target_lease_still_valid(), via the immutable physical_id, not the
   mutable builtin_backend preference.

(The take() exception-safety fix itself is tested in
tests/test_bass_prepared_stream.py, alongside PreparedBassStream's
other ownership-state-machine tests -- not duplicated here.)
"""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from billsmusic.player_lease import PlayerTargetLease
from billsmusic.window import PlayerWindow


# ---------------------------------------------------------------------------
# _discard_prepared_candidate: safe diagnostic on native free failure
# ---------------------------------------------------------------------------

class _FakeFailingCandidate:
    """A candidate whose discard() reports failure -- mirrors a real
    PreparedBassStream.discard() when BASS_StreamFree itself returns
    False or raises. Tracks call count so a test can prove
    _discard_prepared_candidate never retries the native free."""
    def __init__(self):
        self.discard_calls = 0

    def discard(self):
        self.discard_calls += 1
        return False


class _FakeSucceedingCandidate:
    def __init__(self):
        self.discard_calls = 0

    def discard(self):
        self.discard_calls += 1
        return True


def _diagnostics_window():
    events = []
    window = SimpleNamespace(
        diagnostics=SimpleNamespace(
            record=lambda category, op, **kw: events.append((category, op, kw)),
            path_details=lambda p: {},
        ),
    )
    window._discard_prepared_candidate = (
        lambda candidate, stage: PlayerWindow._discard_prepared_candidate(window, candidate, stage)
    )
    return window, events


def test_discard_failure_records_a_safe_diagnostic_and_never_retries():
    window, events = _diagnostics_window()
    candidate = _FakeFailingCandidate()

    window._discard_prepared_candidate(candidate, "plex_audio_load")

    assert candidate.discard_calls == 1  # never retried
    assert len(events) == 1
    category, op, kw = events[0]
    assert category == "playback"
    assert op == "prepared_candidate_discard_failed"
    assert kw["details"]["stage"] == "plex_audio_load"
    assert kw["details"]["candidate_type"] == "_FakeFailingCandidate"


def test_discard_failure_diagnostic_never_carries_private_content():
    window, events = _diagnostics_window()
    candidate = _FakeFailingCandidate()
    # Simulate what a real Plex-sourced candidate might carry internally
    # -- none of this must ever reach the diagnostic.
    candidate.identity = "plex://server-1/42.mp3"
    candidate.transport_url = "http://plex-host:32400/part.mp3?X-Plex-Token=REAL-SECRET"

    window._discard_prepared_candidate(candidate, "plex_audio_load")

    _, _, kw = events[0]
    serialized = repr(kw["details"])
    assert "REAL-SECRET" not in serialized
    assert "plex://server-1/42.mp3" not in serialized
    assert "X-Plex-Token" not in serialized


def test_discard_success_records_no_diagnostic_and_is_called_exactly_once():
    window, events = _diagnostics_window()
    candidate = _FakeSucceedingCandidate()

    window._discard_prepared_candidate(candidate, "crossfade_load")

    assert events == []
    assert candidate.discard_calls == 1


def test_discard_failure_stage_is_recorded_per_call_site():
    window, events = _diagnostics_window()
    for stage in ("plex_audio_load", "crossfade_load", "mixed_transition_video_to_audio"):
        window._discard_prepared_candidate(_FakeFailingCandidate(), stage)
    stages = [kw["details"]["stage"] for _, _, kw in events]
    assert stages == ["plex_audio_load", "crossfade_load", "mixed_transition_video_to_audio"]


# ---------------------------------------------------------------------------
# PlayerTargetLease.backend_family validation
# ---------------------------------------------------------------------------

def _lease_window():
    bass_a = SimpleNamespace(physical_id="bass-A")
    miniaudio_b = SimpleNamespace(physical_id="miniaudio-B")
    window = SimpleNamespace(
        simple_player=bass_a,
        simple_inactive_player=miniaudio_b,
        _player_topology_epoch=0,
    )
    window._target_lease_still_valid = (
        lambda lease: PlayerWindow._target_lease_still_valid(window, lease)
    )
    return window, bass_a, miniaudio_b


def test_lease_invalid_when_backend_family_says_bass_but_target_is_miniaudio():
    window, bass_a, miniaudio_b = _lease_window()
    # Mismatched on purpose: declares "bass" but the captured physical
    # object (and role pointer, and topology epoch) is genuinely the
    # miniaudio-B instance -- only backend_family is wrong.
    lease = PlayerTargetLease(
        backend_family="bass", target_role="inactive",
        physical_player_id="miniaudio-B", physical_object=miniaudio_b,
        topology_epoch=0,
    )
    assert window._target_lease_still_valid(lease) is False


def test_lease_invalid_when_backend_family_says_miniaudio_but_target_is_bass():
    window, bass_a, miniaudio_b = _lease_window()
    lease = PlayerTargetLease(
        backend_family="miniaudio", target_role="active",
        physical_player_id="bass-A", physical_object=bass_a,
        topology_epoch=0,
    )
    assert window._target_lease_still_valid(lease) is False


def test_lease_valid_when_backend_family_matches():
    window, bass_a, miniaudio_b = _lease_window()
    lease = PlayerTargetLease(
        backend_family="bass", target_role="active",
        physical_player_id="bass-A", physical_object=bass_a,
        topology_epoch=0,
    )
    assert window._target_lease_still_valid(lease) is True

    lease2 = PlayerTargetLease(
        backend_family="miniaudio", target_role="inactive",
        physical_player_id="miniaudio-B", physical_object=miniaudio_b,
        topology_epoch=0,
    )
    assert window._target_lease_still_valid(lease2) is True


def test_lease_family_validation_is_independent_of_mutable_builtin_backend():
    # The check must be driven by the immutable physical_id, never the
    # mutable builtin_backend preference -- even if builtin_backend says
    # something that contradicts the lease's own family, a genuinely
    # matching physical target must still validate correctly.
    window, bass_a, miniaudio_b = _lease_window()
    window.builtin_backend = "miniaudio"  # deliberately contradicts the lease's family
    lease = PlayerTargetLease(
        backend_family="bass", target_role="active",
        physical_player_id="bass-A", physical_object=bass_a,
        topology_epoch=0,
    )
    assert window._target_lease_still_valid(lease) is True
