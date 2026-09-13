"""Playback stability hardening, Phase C1 (native audio backend
ownership, 2026-09-10): PlayerTargetLease proves that the specific
physical BassPlayer/MiniaudioPlayer instance an async preparation
worker was dispatched for is still the intended target by the time its
result is ready to commit.

Why this exists, concretely: PlayerWindow keeps exactly 4 physical
player objects for its whole lifetime (2 BassPlayer, 2 MiniaudioPlayer),
but neither the `simple_player`/`simple_inactive_player` role pointers
NOR the `bass_player`/`bass_inactive_player`/`miniaudio_player`/
`miniaudio_inactive_player` labels are stable physical identities --
all of them get reassigned across the object pool as playback
progresses (backend switches, new track dispatch, crossfade/mixed-
transition promotion swaps which physical object plays which role).
A candidate prepared for "whatever `simple_inactive_player` is right
now" can therefore no longer be safely committed by re-reading
`simple_inactive_player` again later -- it might mean a different
physical object by then. PlayerTargetLease captures the physical
object directly, plus enough to prove nothing about its role has
changed since dispatch.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class PlayerTargetLease:
    backend_family: str      # "bass" | "miniaudio"
    target_role: str         # "active" | "inactive"
    physical_player_id: str  # e.g. "bass-A" -- the object's own immutable
                              # `physical_id`, captured at dispatch time
    physical_object: object  # direct reference to the targeted BassPlayer/
                              # MiniaudioPlayer instance, captured at dispatch
    topology_epoch: int      # PlayerWindow._player_topology_epoch at dispatch
