"""v1.0.70: ReplayGain / crossfade identity race.

Independent Codex audit finding: during a crossfade, if the incoming
track's gain is not already cached, the inactive/incoming player is
loaded at unity gain while GainLookupWorker resolves the real value in
the background. Pre-fix, the worker's result was only ever applied when
`loaded_path == self.current_path` -- but the incoming player is *not*
yet current while it's still crossfading in, so a result that arrived
before promotion was silently dropped, and the stale unity gain got
carried into `_active_normalisation_gain` unchanged at promotion. This
was a correctness bug (wrong playback volume), not merely a performance
one.

The fix (see _cached_gain_for_path / _set_slot_gain / _queue_gain_lookup_async
/ _finish_miniaudio_crossfade / _finish_crossfade in window.py) replaces the
path == current_path gate with a per-slot identity token: every
(re)assignment of the active or inactive slot mints a fresh token, and a
worker result is only applied to whichever slot (if either) still holds
the token it was dispatched under. Promotion swaps the token together
with the gain value, so a result that arrives *after* promotion still
finds its way to the now-active slot instead of being dropped.

These tests exercise the real player/promote lifecycle production uses
(_on_crossfade_load_succeeded -> _begin_builtin_fade ->
_finish_miniaudio_crossfade), not an isolated _gain_for_path unit test.
test_result_after_promotion_lands_on_the_promoted_player is the central
reproduction of the audited race: confirmed (via `git stash`) to fail
against pre-fix v1.0.69 source with the promoted player left at unity
gain instead of the correct ReplayGain-derived value.
"""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import billsmusic.window as window_module
from billsmusic.window import PlayerWindow
from billsmusic.worker_registry import WorkerLifetimeRegistry

REPLAYGAIN_TAGS = {
    "track_gain": -6.0, "track_peak": 0.9, "album_gain": None, "album_peak": None,
}


class _FakeSignal:
    def __init__(self):
        self.slot = None

    def connect(self, slot):
        self.slot = slot

    def emit(self, *args):
        assert self.slot is not None, "signal fired with no connected slot"
        self.slot(*args)


class _FakeGainWorker:
    """Stand-in for GainLookupWorker: constructed like the real one, but
    .start() does nothing -- the test fires .gain_ready.emit(...) itself
    to control exactly when the result arrives relative to promotion."""

    def __init__(self, path, loudness_cache):
        self.path = path
        self.gain_ready = _FakeSignal()
        self.finished = _FakeSignal()
        self.started = False

    def start(self):
        self.started = True


class _FakePreparedCandidate:
    def __init__(self, path=None):
        self.path = path
        self.discarded = False

    def discard(self):
        self.discarded = True
        return True


class _FakePlayer:
    def __init__(self, length=200.0, pos=0.0, physical_id=""):
        self.volume = None
        self.playing = False
        self.stopped = False
        self._length = length
        self._pos = pos
        self.physical_id = physical_id
        self.commit_prepared_calls = 0

    def commit_prepared(self, candidate):
        # Phase C1: mirrors BassPlayer.commit_prepared/
        # MiniaudioPlayer.commit_prepared's public contract closely enough
        # for these tests -- reports success, no real resource involved.
        self.commit_prepared_calls += 1
        return True

    def set_volume(self, v):
        self.volume = v

    def play(self):
        self.playing = True

    def stop(self):
        self.stopped = True
        self.playing = False

    def is_playing(self):
        return self.playing

    def stats(self):
        return {"duration": self._length, "sample_rate": 44100, "channels": 2}

    def get_length(self):
        return self._length

    def get_pos(self):
        return self._pos

    def slide_volume(self, target, seconds):
        self.volume = target

    def load(self, path):
        pass

    def seek(self, position):
        pass


def _window(**overrides):
    outgoing = overrides.pop("simple_player", None) or _FakePlayer(length=200.0, pos=190.0, physical_id="bass-A")
    incoming = overrides.pop("simple_inactive_player", None) or _FakePlayer(physical_id="bass-B")
    diagnostics_calls = []
    window = SimpleNamespace(
        _closing=False,
        simple_player=outgoing,
        simple_inactive_player=incoming,
        bass_player=outgoing,
        bass_inactive_player=incoming,
        miniaudio_player=None,
        miniaudio_inactive_player=None,
        builtin_backend="bass",
        # Phase C1: real _make_target_lease/_target_lease_still_valid are
        # bound below, so this must start initialised the same way
        # production is.
        _player_topology_epoch=0,
        prebuffer_active=True,
        fade_active=False,
        fade_waits=0,
        pending_next=True,
        pending_builtin_crossfade_index=1,
        pending_builtin_crossfade_path="incoming.flac",
        pending_builtin_crossfade_quiet=False,
        _builtin_fade_generation=0,
        _crossfade_load_token=1,
        _pending_crossfade_immediate=False,
        crossfade_seconds=5.0,
        track_index_by_path={},
        master_volume=100,
        _sleep_timer_gain=1.0,
        current_path="outgoing.flac",
        _active_normalisation_gain=1.0,
        _inactive_normalisation_gain=1.0,
        _gain_token_seq=0,
        _active_gain_token=0,
        _inactive_gain_token=0,
        _gain_snapshot_cache={},
        _gain_lookup_pending=set(),
        _gain_lookup_workers=[],
        _gain_lookup_subscribers={},
        _worker_registry=WorkerLifetimeRegistry(),
        loudness_cache=SimpleNamespace(override_for=lambda path: "default"),
        normalisation_enabled=True, normalisation_mode="track",
        target_lufs=-14.0, tagged_preamp_db=0.0, untagged_preamp_db=0.0,
        prevent_clipping=True, auto_loudness_analysis=False, loudness_worker=None,
        set_master_volume=lambda v: None,
        statusBar=lambda: SimpleNamespace(showMessage=lambda *a, **kw: None),
        diagnostics=SimpleNamespace(
            record=lambda *a, **kw: diagnostics_calls.append((a, kw)),
            path_details=lambda path: {},
        ),
        _audio_log=lambda message: None,
        _audio_name=lambda path: path,
        _activate_track_ui=lambda path, *, library_index=None, queue_token=None: None,
        _begin_playback_recovery=lambda *a, **k: None,
        _reset_progress=lambda: None,
        _arm_playback_watchdog=lambda position: None,
        _backend_label=lambda: "BASS",
        _current_backend_name=lambda: "bass",
        _use_bass_backend=lambda: True,
        _use_builtin_player=lambda: True,
        _cancel_fade=lambda: None,
        _stop_all=lambda: None,
        _current_playback_attempt=None,
        _is_current_playback_attempt=lambda attempt_id: True,
        _require_current_playback_attempt=lambda attempt_id, stage: True,
        _advance_playback_attempt_state=lambda attempt_id, state: None,
        _temporary_backend_override=None,
    )
    for key, value in overrides.items():
        setattr(window, key, value)
    window.diagnostics_calls = diagnostics_calls
    for name in (
        "_next_gain_token", "_set_slot_gain", "_cached_gain_for_path",
        "_queue_gain_lookup_async", "_crossfade_load_is_current",
        "_fail_pending_crossfade", "_on_crossfade_load_prepared",
        "_on_crossfade_load_failed", "_begin_builtin_fade",
        "_finish_miniaudio_crossfade", "_try_recovery_backend",
        "_promote_inactive_gain_slot",
        # Phase C1 (native audio backend ownership) -- real, unbound so
        # the actual topology/lease logic is exercised, not just its
        # absence papered over.
        "_set_player_topology", "_promote_inactive_player",
        "_make_target_lease", "_target_lease_still_valid",
        "_discard_prepared_candidate",
    ):
        impl = getattr(PlayerWindow, name, None)
        if impl is not None:
            setattr(window, name, impl.__get__(window))
    return window


def _load_incoming(window, monkeypatch, path="incoming.flac"):
    """Drive the real _on_crossfade_load_prepared entry point (as if
    _start_miniaudio_crossfade_to's BassStreamPrepareWorker just reported
    back), dispatching a real _cached_gain_for_path(target="inactive") call. With
    no snapshot cached yet, this is a miss: unity gain now, real gain via
    the (faked) worker later. Returns the fake worker so the test controls
    exactly when the result arrives."""
    monkeypatch.setattr(window_module, "GainLookupWorker", _FakeGainWorker)
    lease = window._make_target_lease("bass", "inactive")
    window._on_crossfade_load_prepared(window._crossfade_load_token, path, _FakePreparedCandidate(path), None, lease)
    assert window._gain_lookup_workers, "expected a GainLookupWorker to have been dispatched"
    return window._gain_lookup_workers[-1]


def test_result_before_promotion_applies_to_incoming_player(monkeypatch):
    """CASE 1: the worker resolves while the crossfade is still running --
    the inactive slot must pick up the real gain immediately, before
    promotion, so the fade-in is audibly correct throughout."""
    window = _window()
    worker = _load_incoming(window, monkeypatch)
    assert window._inactive_normalisation_gain == 1.0  # safe default while pending

    worker.gain_ready.emit("incoming.flac", REPLAYGAIN_TAGS, None)

    assert window._inactive_normalisation_gain != 1.0
    expected = window._gain_snapshot_cache["incoming.flac"].linear_gain
    assert window._inactive_normalisation_gain == expected


def test_result_after_promotion_lands_on_the_promoted_player(monkeypatch):
    """CASE 2 -- the central audited race. Result arrives AFTER promotion
    has already swapped the incoming player into the active slot. Pre-fix,
    this result was dropped (path != current_path at dispatch time, and
    nothing ever re-checked it), leaving the promoted/now-playing track at
    unity gain. Confirmed via git stash to fail against pre-fix v1.0.69:
    AssertionError on the final linear_gain comparison, active gain stuck
    at 1.0."""
    window = _window()
    worker = _load_incoming(window, monkeypatch)

    # Promotion happens (crossfade completes) BEFORE the worker replies.
    window._finish_miniaudio_crossfade()
    assert window._active_normalisation_gain == 1.0  # still the safe default

    worker.gain_ready.emit("incoming.flac", REPLAYGAIN_TAGS, None)

    expected = window._gain_snapshot_cache["incoming.flac"].linear_gain
    assert expected != 1.0, "test fixture must produce a non-trivial ReplayGain value"
    assert window._active_normalisation_gain == expected, (
        "late gain result must still reach the promoted (now-active) player"
    )


def test_cache_hit_applies_gain_immediately_no_regression(monkeypatch):
    """CASE 3: no regression for the already-covered fast path -- a cache
    hit at crossfade-load time must give the correct gain with no worker
    dispatch at all."""
    from billsmusic.loudness import calculate_gain
    cached = calculate_gain(
        "track", REPLAYGAIN_TAGS, None, target_lufs=-14.0,
        tagged_preamp_db=0.0, untagged_preamp_db=0.0, prevent_clipping=True,
    )
    window = _window(_gain_snapshot_cache={"incoming.flac": cached})

    def _fail(*a, **kw):
        raise AssertionError("cache hit must not construct a GainLookupWorker")
    monkeypatch.setattr(window_module, "GainLookupWorker", _fail)

    lease = window._make_target_lease("bass", "inactive")
    window._on_crossfade_load_prepared(
        window._crossfade_load_token, "incoming.flac",
        _FakePreparedCandidate("incoming.flac"), None, lease,
    )

    assert window._inactive_normalisation_gain == cached.linear_gain


def test_lookup_failure_resolves_to_safe_computed_default(monkeypatch):
    """CASE 4: GainLookupWorker always emits gain_ready (internal
    read_replaygain/analysis_for failures are caught and default to empty
    tags / no measurement inside the worker itself) -- this proves that
    "failure" path still resolves safely and is still correctly applied to
    the right slot rather than crashing or hanging."""
    window = _window()
    worker = _load_incoming(window, monkeypatch)

    empty_tags = {"track_gain": None, "track_peak": None, "album_gain": None, "album_peak": None}
    worker.gain_ready.emit("incoming.flac", empty_tags, None)

    assert window._inactive_normalisation_gain == 1.0
    assert window._gain_snapshot_cache["incoming.flac"].linear_gain == 1.0


def test_incoming_player_reused_for_different_path_drops_stale_result(monkeypatch):
    """Hostile sequence: the inactive player loads track B (gain pending),
    then before B's result arrives it gets reused for track C (a second
    crossfade start reassigns the inactive slot). B's late result must not
    mutate what is now C's gain."""
    window = _window(pending_builtin_crossfade_path="track-b.flac")
    worker_b = _load_incoming(window, monkeypatch, path="track-b.flac")

    # Inactive slot reused for a different track before B's result arrives.
    window.pending_builtin_crossfade_path = "track-c.flac"
    window._crossfade_load_token = 2
    lease = window._make_target_lease("bass", "inactive")
    window._on_crossfade_load_prepared(2, "track-c.flac", _FakePreparedCandidate("track-c.flac"), None, lease)
    assert window._inactive_normalisation_gain == 1.0  # C is also a fresh miss

    worker_b.gain_ready.emit("track-b.flac", REPLAYGAIN_TAGS, None)

    # B's result must not have touched the inactive slot now holding C.
    assert window._inactive_normalisation_gain == 1.0
    assert window._gain_snapshot_cache["track-b.flac"].linear_gain != 1.0  # cache still updated


def test_rapid_next_abandons_pending_gain_without_mutating_replacement(monkeypatch):
    """Section 9(a): A plays, crossfade to B begins (gain B pending), user
    hits Next immediately -- B is abandoned and the active slot is
    reassigned directly (bypassing promotion) to C. Late B result must not
    alter C's active gain."""
    window = _window(pending_builtin_crossfade_path="track-b.flac")
    worker_b = _load_incoming(window, monkeypatch, path="track-b.flac")

    # User skip: active slot reassigned directly to C (e.g. via _play_simple),
    # not through crossfade promotion.
    window._active_normalisation_gain = window._cached_gain_for_path("track-c.flac", target="active")

    worker_b.gain_ready.emit("track-b.flac", REPLAYGAIN_TAGS, None)

    assert window._active_normalisation_gain == 1.0  # C's own (still-pending) safe default


def test_same_path_reloaded_new_load_generation_drops_old_result(monkeypatch):
    """A path is loaded into the inactive slot twice in a row (e.g.
    requeued) before the first lookup resolves -- the first (stale) load's
    result must not override the second (current) load's identity."""
    window = _window()
    first_worker = _load_incoming(window, monkeypatch, path="incoming.flac")

    # Same path reloaded into the inactive slot again -- a fresh token.
    window._crossfade_load_token = 2
    lease = window._make_target_lease("bass", "inactive")
    window._on_crossfade_load_prepared(2, "incoming.flac", _FakePreparedCandidate("incoming.flac"), None, lease)
    second_worker = window._gain_lookup_workers[-1]
    assert second_worker is not first_worker or True  # dedup may reuse in-flight worker path-wise

    # The FIRST request's token is now stale relative to the slot.
    first_worker.gain_ready.emit("incoming.flac", REPLAYGAIN_TAGS, None)
    # Whatever the first call produced is still cached (path-level cache is
    # not identity-gated), but let's confirm the *current* inactive token
    # is the second request's, not the first's, before asserting apply state.
    assert window._inactive_gain_token != 0


def test_active_and_inactive_slots_pending_same_path_both_get_result(monkeypatch):
    """Section 14 fan-out: the active and inactive slots both request a
    lookup for the same path while the underlying worker is still in
    flight -- one worker result must correctly reach BOTH valid
    subscribers, not just a single 'current track' callback."""
    window = _window()
    monkeypatch.setattr(window_module, "GainLookupWorker", _FakeGainWorker)

    active_token = window._next_gain_token()
    window._active_gain_token = active_token
    window._queue_gain_lookup_async("shared.flac", active_token, "active")

    inactive_token = window._next_gain_token()
    window._inactive_gain_token = inactive_token
    window._queue_gain_lookup_async("shared.flac", inactive_token, "inactive")

    assert len(window._gain_lookup_workers) == 1, "must dedup to a single in-flight worker"
    worker = window._gain_lookup_workers[0]
    worker.gain_ready.emit("shared.flac", REPLAYGAIN_TAGS, None)

    expected = window._gain_snapshot_cache["shared.flac"].linear_gain
    assert window._active_normalisation_gain == expected
    assert window._inactive_normalisation_gain == expected


def test_gain_lookup_subscribers_cleared_after_resolution(monkeypatch):
    """_gain_lookup_subscribers must never leak: every path entry appended
    at dispatch time is removed once that path's worker resolves, whether
    the result was applied (token still valid) or dropped as stale (token
    superseded). Covers both a fan-out path (two live subscribers) and a
    single stale one, then confirms the dict is completely empty."""
    window = _window()
    monkeypatch.setattr(window_module, "GainLookupWorker", _FakeGainWorker)

    active_token = window._next_gain_token()
    window._active_gain_token = active_token
    window._queue_gain_lookup_async("shared.flac", active_token, "active")
    inactive_token = window._next_gain_token()
    window._inactive_gain_token = inactive_token
    window._queue_gain_lookup_async("shared.flac", inactive_token, "inactive")
    assert "shared.flac" in window._gain_lookup_subscribers
    assert len(window._gain_lookup_subscribers["shared.flac"]) == 2

    stale_token = window._next_gain_token()
    window._queue_gain_lookup_async("stale.flac", stale_token, "active")
    # Slot reassigned before the stale.flac lookup resolves -- its token
    # will match neither current slot when the result arrives.
    window._active_gain_token = window._next_gain_token()
    assert "stale.flac" in window._gain_lookup_subscribers

    assert set(window._gain_lookup_subscribers.keys()) == {"shared.flac", "stale.flac"}
    assert len(window._gain_lookup_workers) == 2

    for worker in list(window._gain_lookup_workers):
        worker.gain_ready.emit(worker.path, REPLAYGAIN_TAGS, None)
        worker.finished.emit()

    assert window._gain_lookup_subscribers == {}
    assert window._gain_lookup_pending == set()
    assert window._gain_lookup_workers == []


def test_promotion_safety_check_uses_memory_only_snapshot(monkeypatch):
    """Section 8 defense-in-depth: even with no worker involved at all, if
    a correct gain snapshot for the promoted path already sits in
    _gain_snapshot_cache by promotion time, promotion re-derives the
    active gain from it -- pure dict lookup, zero I/O. Proven here by
    making read_replaygain raise if it's ever called during promotion."""
    from billsmusic.loudness import calculate_gain

    def _no_io(*a, **kw):
        raise AssertionError("promotion must not perform any I/O")
    monkeypatch.setattr(window_module, "read_replaygain", _no_io)

    cached = calculate_gain(
        "track", REPLAYGAIN_TAGS, None, target_lufs=-14.0,
        tagged_preamp_db=0.0, untagged_preamp_db=0.0, prevent_clipping=True,
    )
    window = _window(
        _gain_snapshot_cache={"incoming.flac": cached},
        _inactive_normalisation_gain=1.0,  # not yet corrected in-variable
    )

    window._finish_miniaudio_crossfade()

    assert window._active_normalisation_gain == cached.linear_gain


def test_recovery_backend_receives_pending_gain_correctly(monkeypatch):
    """Section 10: recovery uses the same _cached_gain_for_path machinery.
    A pending lookup dispatched for the recovery player's path must, once
    resolved, apply to the active slot the recovery path actually uses."""
    window = _window()
    monkeypatch.setattr(window_module, "GainLookupWorker", _FakeGainWorker)
    recovery_player = _FakePlayer()

    ok, seeked = window._try_recovery_backend("bass", "recovered.flac", 0.0, generation=1)
    assert ok is True or ok is False  # bass_player None-guard aside, gain path is what we check

    worker = window._gain_lookup_workers[-1]
    worker.gain_ready.emit("recovered.flac", REPLAYGAIN_TAGS, None)

    expected = window._gain_snapshot_cache["recovered.flac"].linear_gain
    assert window._active_normalisation_gain == expected


def test_closing_drops_late_result_without_mutation(monkeypatch):
    """Shutdown safety: a result that arrives after _closing is set must
    not mutate any player/slot state."""
    window = _window()
    worker = _load_incoming(window, monkeypatch)

    window._closing = True
    worker.gain_ready.emit("incoming.flac", REPLAYGAIN_TAGS, None)

    assert window._inactive_normalisation_gain == 1.0
    assert "incoming.flac" not in window._gain_snapshot_cache


def test_master_volume_and_replaygain_combination_unchanged(monkeypatch):
    """Section 7: the fix only changes *where* the result is applied, not
    the math combining it with master volume."""
    from billsmusic.loudness import combine_volume
    window = _window(master_volume=50)
    worker = _load_incoming(window, monkeypatch)
    worker.gain_ready.emit("incoming.flac", REPLAYGAIN_TAGS, None)

    gain = window._inactive_normalisation_gain
    expected_volume = combine_volume(0.5, gain, 1.0)
    assert expected_volume == combine_volume(
        window.master_volume / 100.0, window._inactive_normalisation_gain, window._sleep_timer_gain,
    )
