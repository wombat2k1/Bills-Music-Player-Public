from types import SimpleNamespace

from billsmusic.window import PlayerWindow


class FakePlayer:
    def __init__(self, position):
        self.position = position
        self.stopped = False
        self.volume = None

    def stop(self):
        self.stopped = True

    def set_volume(self, value):
        self.volume = value

    def get_pos(self):
        return self.position


def test_bass_player_pair_stays_rotated_after_consecutive_crossfades():
    first = FakePlayer(0.0)
    second = FakePlayer(1.5)
    window = SimpleNamespace(
        simple_player=first,
        simple_inactive_player=second,
        bass_player=first,
        bass_inactive_player=second,
        miniaudio_player=None,
        miniaudio_inactive_player=None,
        builtin_backend="bass",
        _active_normalisation_gain=1.0,
        _inactive_normalisation_gain=0.8,
        _active_gain_token=1,
        _inactive_gain_token=2,
        _gain_token_seq=2,
        _gain_snapshot_cache={},
        fade_active=True,
        prebuffer_active=True,
        pending_next=True,
        pending_builtin_crossfade_index=1,
        pending_builtin_crossfade_path="next.flac",
        pending_builtin_crossfade_quiet=True,
        master_volume=100,
        _sleep_timer_gain=1.0,
        _reset_progress=lambda: None,
        _arm_playback_watchdog=lambda position: None,
        _audio_log=lambda message: None,
        _backend_label=lambda: "BASS",
        # Phase C1 (native audio backend ownership): real
        # _set_player_topology/_promote_inactive_player are bound below,
        # so this must start initialised the same way production is.
        _player_topology_epoch=0,
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None, path_details=lambda p: {}),
    )
    window._next_gain_token = lambda: PlayerWindow._next_gain_token(window)
    window._promote_inactive_gain_slot = (
        lambda path: PlayerWindow._promote_inactive_gain_slot(window, path)
    )
    window._set_player_topology = (
        lambda active, inactive, reason: PlayerWindow._set_player_topology(window, active, inactive, reason=reason)
    )
    window._promote_inactive_player = (
        lambda reason: PlayerWindow._promote_inactive_player(window, reason=reason)
    )

    PlayerWindow._finish_miniaudio_crossfade(window)

    assert window.simple_player is second
    assert window.bass_player is second
    assert window.simple_inactive_player is first
    assert window.bass_inactive_player is first

    # v1.0.70: the gain identity token travels with the value on promotion
    # (was the inactive slot's token, 2) and the now-idle inactive slot
    # gets a freshly minted one (3) so a stale request can't match it.
    assert window._active_gain_token == 2
    assert window._inactive_gain_token == 3

    # The next call to _play_path_direct restores this canonical pair, so its
    # active player remains the playing post-crossfade stream.
    window.simple_player = window.bass_player
    window.simple_inactive_player = window.bass_inactive_player
    assert window.simple_player is second
