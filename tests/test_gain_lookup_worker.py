"""v1.0.67 MainThread I/O hardening: ReplayGain/loudness lookups.

_gain_for_path/_gain_details_for_path used to call read_replaygain(path)
(a Mutagen open) and loudness_cache.analysis_for(path) (an os.stat-based
signature check) synchronously on every track activation, crossfade and
playback-recovery attempt. _cached_gain_for_path is the replacement used
on those hot paths: a cache hit returns instantly with zero I/O; a mode
that's already "off" (no override/global-setting combination that could
be changed by reading tags) is computed and cached with zero I/O too;
anything else returns a safe unity-gain default and dispatches
GainLookupWorker (billsmusic/workers.py) to do the real read off-thread,
correcting the now-playing volume in place once it completes (only when
the track is still current -- the same limitation _on_loudness_result
already has for its own late-arriving analysis).

_gain_for_path/_gain_details_for_path themselves are deliberately left
unchanged -- they remain the authoritative synchronous path for the rare,
user-initiated "Show Loudness Information" / normalisation-override menu
actions, which are not on the automatic playback-activation path this
round targets.
"""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import billsmusic.window as window_module
from billsmusic.window import PlayerWindow
from billsmusic.worker_registry import WorkerLifetimeRegistry


def _base_window(**overrides):
    window = SimpleNamespace(
        _closing=False,
        _gain_lookup_pending=set(),
        _gain_lookup_workers=[],
        _gain_lookup_subscribers={},
        _gain_snapshot_cache={},
        _worker_registry=WorkerLifetimeRegistry(),
        loudness_cache=SimpleNamespace(override_for=lambda path: "default"),
        normalisation_enabled=True, normalisation_mode="track",
        target_lufs=-14.0, tagged_preamp_db=0.0, untagged_preamp_db=0.0,
        prevent_clipping=True, auto_loudness_analysis=False, loudness_worker=None,
        current_path=None,
        _active_normalisation_gain=1.0,
        _inactive_normalisation_gain=1.0,
        _gain_token_seq=0,
        _active_gain_token=0,
        _inactive_gain_token=0,
        set_master_volume=lambda v: None,
        master_volume=80,
        statusBar=lambda: SimpleNamespace(showMessage=lambda *a, **kw: None),
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None, path_details=lambda path: {}),
    )
    for key, value in overrides.items():
        setattr(window, key, value)
    window._next_gain_token = lambda: PlayerWindow._next_gain_token(window)
    window._set_slot_gain = (
        lambda target, gain: PlayerWindow._set_slot_gain(window, target, gain)
    )
    window._queue_gain_lookup_async = (
        lambda path, slot_token, target="active":
            PlayerWindow._queue_gain_lookup_async(window, path, slot_token, target)
    )
    return window


def test_cached_gain_for_path_hit_performs_no_io_and_no_worker(monkeypatch):
    def _fail(*a, **kw):
        raise AssertionError("cache hit must not construct a GainLookupWorker")
    monkeypatch.setattr(window_module, "GainLookupWorker", _fail)

    from billsmusic.loudness import calculate_gain
    cached_result = calculate_gain(
        "track", {"track_gain": -3.0, "track_peak": 0.9, "album_gain": None, "album_peak": None},
        None, target_lufs=-14.0, tagged_preamp_db=0.0, untagged_preamp_db=0.0, prevent_clipping=True,
    )
    window = _base_window(_gain_snapshot_cache={"song.mp3": cached_result})

    gain = PlayerWindow._cached_gain_for_path(window, "song.mp3")

    assert gain == cached_result.linear_gain


def test_cached_gain_for_path_off_mode_needs_no_io(monkeypatch):
    def _fail(*a, **kw):
        raise AssertionError("mode='off' must not construct a GainLookupWorker")
    monkeypatch.setattr(window_module, "GainLookupWorker", _fail)

    window = _base_window(
        loudness_cache=SimpleNamespace(override_for=lambda path: "off"),
    )

    gain = PlayerWindow._cached_gain_for_path(window, "song.mp3")

    assert gain == 1.0
    assert window._gain_snapshot_cache["song.mp3"].mode == "off"


def test_cached_gain_for_path_miss_returns_safe_default_and_dispatches(monkeypatch):
    class _FakeSignal:
        def connect(self, slot):
            pass

    class _FakeWorker:
        def __init__(self, path, loudness_cache):
            self.gain_ready = _FakeSignal()
            self.finished = _FakeSignal()
        def start(self):
            pass

    monkeypatch.setattr(window_module, "GainLookupWorker", _FakeWorker)
    window = _base_window()

    gain = PlayerWindow._cached_gain_for_path(window, "song.mp3")

    assert gain == 1.0
    assert "song.mp3" in window._gain_lookup_pending


def test_cached_gain_for_path_does_not_dispatch_duplicate_lookups(monkeypatch):
    calls = []

    class _FakeSignal:
        def connect(self, slot):
            pass

    class _FakeWorker:
        def __init__(self, path, loudness_cache):
            calls.append(path)
            self.gain_ready = _FakeSignal()
            self.finished = _FakeSignal()
        def start(self):
            pass

    monkeypatch.setattr(window_module, "GainLookupWorker", _FakeWorker)
    window = _base_window()

    PlayerWindow._cached_gain_for_path(window, "song.mp3")
    PlayerWindow._cached_gain_for_path(window, "song.mp3")

    assert calls == ["song.mp3"]


def test_gain_lookup_result_only_corrects_volume_for_the_still_valid_slot(monkeypatch):
    """v1.0.70: application is gated on the slot's identity token, not on
    path == current_path. A result whose token no longer matches either
    slot (the slot was reassigned to something else in the meantime) is
    dropped; the underlying gain snapshot is still cached either way."""
    class _FakeSignal:
        def __init__(self):
            self.slot = None
        def connect(self, slot):
            self.slot = slot

    class _FakeWorker:
        def __init__(self, path, loudness_cache):
            self.gain_ready = _FakeSignal()
            self.finished = _FakeSignal()
        def start(self):
            pass

    monkeypatch.setattr(window_module, "GainLookupWorker", _FakeWorker)
    volume_calls = []
    window = _base_window(set_master_volume=lambda v: volume_calls.append(v))

    # Request dispatched for the active slot under a freshly minted token ...
    original_token = PlayerWindow._next_gain_token(window)
    PlayerWindow._queue_gain_lookup_async(window, "song.mp3", original_token, "active")
    # ... but before the worker replies, the active slot gets reassigned to
    # a different track (rapid Next), minting a new (different) token.
    window._active_gain_token = PlayerWindow._next_gain_token(window)
    assert window._active_gain_token != original_token

    worker = window._gain_lookup_workers[0]
    worker.gain_ready.slot("song.mp3", {"track_gain": -3.0, "track_peak": 0.9,
                                         "album_gain": None, "album_peak": None}, None)

    assert volume_calls == []
    assert "song.mp3" in window._gain_snapshot_cache
    assert window._active_normalisation_gain == 1.0


def test_gain_lookup_result_applies_when_slot_token_still_matches(monkeypatch):
    class _FakeSignal:
        def __init__(self):
            self.slot = None
        def connect(self, slot):
            self.slot = slot

    class _FakeWorker:
        def __init__(self, path, loudness_cache):
            self.gain_ready = _FakeSignal()
            self.finished = _FakeSignal()
        def start(self):
            pass

    monkeypatch.setattr(window_module, "GainLookupWorker", _FakeWorker)
    volume_calls = []
    window = _base_window(set_master_volume=lambda v: volume_calls.append(v))

    gain = PlayerWindow._cached_gain_for_path(window, "song.mp3", target="active")
    assert gain == 1.0  # safe default while the worker is in flight

    worker = window._gain_lookup_workers[0]
    worker.gain_ready.slot("song.mp3", {"track_gain": -3.0, "track_peak": 0.9,
                                         "album_gain": None, "album_peak": None}, None)

    assert volume_calls == [80]
    assert window._active_normalisation_gain == window._gain_snapshot_cache["song.mp3"].linear_gain
    assert window._active_normalisation_gain != 1.0
