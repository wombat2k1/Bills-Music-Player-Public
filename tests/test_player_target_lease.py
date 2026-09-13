"""Playback stability hardening, Phase C1 (native audio backend
ownership, 2026-09-10): direct unit coverage for PlayerTargetLease and
PlayerWindow._make_target_lease/_target_lease_still_valid, in isolation
from any specific dispatch path (Plex/crossfade/mixed-transition already
exercise these incidentally -- this file is the focused unit-level
proof of the three independent validity conditions themselves).
"""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from billsmusic.player_lease import PlayerTargetLease
from billsmusic.window import PlayerWindow


def _window(**overrides):
    bass_a = SimpleNamespace(physical_id="bass-A")
    bass_b = SimpleNamespace(physical_id="bass-B")
    window = SimpleNamespace(
        simple_player=bass_a,
        simple_inactive_player=bass_b,
        bass_player=bass_a,
        bass_inactive_player=bass_b,
        miniaudio_player=None,
        miniaudio_inactive_player=None,
        builtin_backend="bass",
        _player_topology_epoch=0,
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None, path_details=lambda p: {}),
    )
    for key, value in overrides.items():
        setattr(window, key, value)
    window._set_player_topology = (
        lambda active, inactive, reason: PlayerWindow._set_player_topology(window, active, inactive, reason=reason)
    )
    window._promote_inactive_player = (
        lambda reason: PlayerWindow._promote_inactive_player(window, reason=reason)
    )
    window._make_target_lease = (
        lambda backend_family, target_role: PlayerWindow._make_target_lease(window, backend_family, target_role)
    )
    window._target_lease_still_valid = (
        lambda lease: PlayerWindow._target_lease_still_valid(window, lease)
    )
    return window


def test_make_target_lease_captures_the_active_role():
    window = _window()
    lease = window._make_target_lease("bass", "active")
    assert lease.backend_family == "bass"
    assert lease.target_role == "active"
    assert lease.physical_object is window.simple_player
    assert lease.physical_player_id == "bass-A"
    assert lease.topology_epoch == 0


def test_make_target_lease_captures_the_inactive_role():
    window = _window()
    lease = window._make_target_lease("bass", "inactive")
    assert lease.physical_object is window.simple_inactive_player
    assert lease.physical_player_id == "bass-B"


def test_lease_is_valid_immediately_after_capture():
    window = _window()
    lease = window._make_target_lease("bass", "inactive")
    assert window._target_lease_still_valid(lease) is True


def test_lease_invalid_after_topology_epoch_changes():
    window = _window()
    lease = window._make_target_lease("bass", "inactive")
    window._set_player_topology(window.miniaudio_player, window.miniaudio_inactive_player, reason="test")
    assert lease.topology_epoch != window._player_topology_epoch
    assert window._target_lease_still_valid(lease) is False


def test_lease_valid_when_topology_is_re_pinned_to_the_same_pair():
    # _set_player_topology only bumps the epoch when the physical pair
    # actually changes -- re-pinning to the SAME pair must not invalidate
    # an in-flight lease.
    window = _window()
    lease = window._make_target_lease("bass", "inactive")
    window._set_player_topology(window.bass_player, window.bass_inactive_player, reason="test_repin_same_pair")
    assert lease.topology_epoch == window._player_topology_epoch
    assert window._target_lease_still_valid(lease) is True


def test_lease_invalid_after_promotion_swaps_active_and_inactive():
    window = _window()
    lease = window._make_target_lease("bass", "inactive")  # targets bass-B
    window._promote_inactive_player(reason="test_promotion")
    # bass-B is now the ACTIVE role -- a lease captured for "inactive"
    # must not validate against it anymore.
    assert window.simple_player is lease.physical_object  # confirms the promotion really happened
    assert window._target_lease_still_valid(lease) is False


def test_lease_invalid_when_physical_id_has_been_tampered_with():
    # Defence-in-depth: even if topology_epoch and the role pointer both
    # (implausibly) still matched, a mismatched physical_id must still
    # fail closed.
    window = _window()
    lease = window._make_target_lease("bass", "inactive")
    tampered = PlayerTargetLease(
        backend_family=lease.backend_family, target_role=lease.target_role,
        physical_player_id="not-the-real-id", physical_object=lease.physical_object,
        topology_epoch=lease.topology_epoch,
    )
    assert window._target_lease_still_valid(tampered) is False


def test_lease_invalid_when_the_role_pointer_moved_without_an_epoch_bump():
    # Defence-in-depth: simulates a hypothetical future bug where some
    # call site reassigns simple_inactive_player directly instead of
    # going through _set_player_topology (bypassing the epoch bump) --
    # the role-pointer-identity check must still catch it independently.
    window = _window()
    lease = window._make_target_lease("bass", "inactive")
    window.simple_inactive_player = SimpleNamespace(physical_id="bass-B")  # bypasses the seam
    assert window._target_lease_still_valid(lease) is False
