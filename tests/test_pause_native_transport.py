"""Astra F2, Phase 4.2: Pause must also hold the native transports that run
on their own clocks.

1. BASS attribute slides are processed in real time regardless of whether the
   channel is playing or paused. A crossfade's volume slides therefore ran to
   completion during a Pause, so Resume came back at the end volumes (the
   outgoing track already silent, the incoming one already at full level)
   instead of continuing the fade from where it was paused.
2. A VLC crossfade keeps its incoming track playing silently on the inactive
   player. pause() paused only the active player, so the incoming track kept
   advancing through the Pause, and the delayed fade-begin timer still began
   the fade while paused. That timer also carried no identity, so one
   scheduled for an abandoned crossfade could begin a newer one early.

The BASS tests use the real vendored bass.dll and a real fixture file (this
suite's convention for native BASS behaviour). The VLC tests drive the real
pause(), _play_path_direct/_start_crossfade_to, _begin_fade, _fade_tick,
_finish_crossfade, next_track and stop_playback with recording VLC players,
the queue claim / PlaybackAttempt machinery and real Qt timers.
"""
import ctypes
import os
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets

import billsmusic.window as window_module
from billsmusic.config import PREBUFFER_MS
from billsmusic.playback_attempt import PlaybackAttemptState
from billsmusic.window import PlayerWindow

import test_pause_freezes_progression as f2
import test_queue_dispatch_failure as queue_harness

_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

ROOT = os.path.dirname(os.path.dirname(__file__))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "sample.flac")
_DLL = os.path.join(ROOT, "vendor", "bass", "bin", "x64", "bass.dll")
needs_bass = pytest.mark.skipif(not os.path.isfile(_DLL), reason="bass.dll not present in this environment")


def _spin(seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QtCore.QCoreApplication.processEvents()
        time.sleep(0.005)
    QtCore.QCoreApplication.processEvents()


# -- 1. BASS: native volume slides -------------------------------------------------

def _bass():
    from billsmusic.bass_player import _BassEngine
    bass = _BassEngine.ensure()
    bass.BASS_ChannelGetAttribute.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_float)]
    bass.BASS_ChannelGetAttribute.restype = ctypes.c_bool
    return bass


def _channel_volume(player):
    """The volume BASS is actually applying to the channel right now."""
    from billsmusic.bass_player import BASS_ATTRIB_VOL
    value = ctypes.c_float()
    assert _bass().BASS_ChannelGetAttribute(player._stream, BASS_ATTRIB_VOL, ctypes.byref(value))
    return value.value


@pytest.fixture
def bass_player():
    from billsmusic.bass_player import BassPlayer
    _bass()
    players = []

    def make():
        player = BassPlayer()
        player.load(FIXTURE)
        players.append(player)
        return player

    yield make
    for player in players:
        player.close()


@needs_bass
def test_bass_volume_slide_is_frozen_while_the_channel_is_paused(bass_player):
    player = bass_player()
    player.set_volume(0.0)
    player.play()
    player.slide_volume(0.4, 1.0)
    time.sleep(0.2)
    player.pause()
    at_pause = _channel_volume(player)

    time.sleep(1.2)  # longer than the whole slide

    assert _channel_volume(player) == pytest.approx(at_pause, abs=0.03)
    assert at_pause < 0.3  # it had genuinely not finished


@needs_bass
def test_bass_volume_slide_continues_from_the_paused_level_on_resume(bass_player):
    player = bass_player()
    player.set_volume(0.0)
    player.play()
    player.slide_volume(0.4, 1.0)
    time.sleep(0.2)
    player.pause()
    at_pause = _channel_volume(player)
    time.sleep(1.2)

    player.resume()
    assert _channel_volume(player) == pytest.approx(at_pause, abs=0.05)  # no jump to the end volume
    time.sleep(1.1)  # the remaining part of the slide
    assert _channel_volume(player) == pytest.approx(0.4, abs=0.02)
    player.pause()


@needs_bass
def test_bass_stop_or_new_volume_discards_a_slide_held_by_pause(bass_player):
    player = bass_player()
    player.set_volume(0.0)
    player.play()
    player.slide_volume(0.4, 1.0)
    time.sleep(0.2)
    player.pause()
    player.set_volume(0.1)  # an explicit volume replaces the paused slide
    player.resume()
    time.sleep(1.1)
    assert _channel_volume(player) == pytest.approx(0.1, abs=0.01)
    player.pause()

    other = bass_player()
    other.set_volume(0.0)
    other.play()
    other.slide_volume(0.4, 1.0)
    time.sleep(0.2)
    other.pause()
    other.stop()
    other.load(FIXTURE)  # Stop, then a new track on the same player
    other.set_volume(0.05)
    other.play()
    time.sleep(1.1)
    assert _channel_volume(other) == pytest.approx(0.05, abs=0.01)
    other.pause()


@needs_bass
def test_bass_pause_resume_without_a_slide_leaves_volume_alone(bass_player):
    player = bass_player()
    player.set_volume(0.05)
    player.play()
    player.pause()
    time.sleep(0.2)
    player.resume()
    time.sleep(0.2)
    assert _channel_volume(player) == pytest.approx(0.05, abs=0.01)
    player.pause()


@needs_bass
def test_pausing_a_bass_crossfade_holds_both_native_slides_until_resume(bass_player, monkeypatch, tmp_path):
    """Through the real window: the crossfade fade begin arms both slides and
    Pause lands before the first manual-ramp tick (whose set_volume would
    otherwise already have stopped them) -- e.g. a Resume that begins a held
    fade, immediately paused again. pause() pauses both players; neither
    channel's volume may move, and Resume continues from those levels."""
    window = f2._crossfade_window(monkeypatch, tmp_path)
    outgoing, incoming = bass_player(), bass_player()
    window.master_volume = 30
    for name, player in (("bass_player", outgoing), ("bass_inactive_player", incoming)):
        setattr(window, name, player)
    window.simple_player, window.simple_inactive_player = outgoing, incoming
    outgoing.set_volume(0.3)
    incoming.set_volume(0.0)
    outgoing.play()
    incoming.play()
    window.prebuffer_active = True
    window.pending_builtin_crossfade_path = window.queue[0]
    window.crossfade_seconds = 1.0
    window._begin_builtin_fade()  # arms the BASS slides
    assert window.fade_active is True
    time.sleep(0.05)

    window.pause()
    levels = (_channel_volume(outgoing), _channel_volume(incoming))
    time.sleep(1.2)

    assert _channel_volume(outgoing) == pytest.approx(levels[0], abs=0.03)
    assert _channel_volume(incoming) == pytest.approx(levels[1], abs=0.03)
    assert levels[0] > 0.05 and levels[1] < 0.25  # genuinely mid-fade

    window.pause()  # Resume
    assert _channel_volume(outgoing) == pytest.approx(levels[0], abs=0.05)
    assert _channel_volume(incoming) == pytest.approx(levels[1], abs=0.05)
    window._fade_tick()  # the manual ramp agrees: it too resumes where it paused
    assert window.fade_active is True
    assert _channel_volume(outgoing) == pytest.approx(levels[0], abs=0.08)
    assert _channel_volume(incoming) == pytest.approx(levels[1], abs=0.08)
    outgoing.pause()
    incoming.pause()


# -- 2. VLC: the incoming crossfade player --------------------------------------------

class _FakeVlcPlayer:
    """Recording libvlc media player: position advances exactly while it
    reports playing."""

    def __init__(self, name):
        self.name = name
        self.path = None
        self.playing = False
        self.paused = False
        self.volume = None
        self.time_ms = 0
        self.length_ms = 180_000

    def load(self, path):
        self.path = path

    def play(self):
        self.playing, self.paused = True, False
        return 0

    def pause(self):  # libvlc_media_player_pause toggles
        if self.playing:
            self.playing, self.paused = False, True
        elif self.paused:
            self.playing, self.paused = True, False

    def set_pause(self, do_pause):
        if do_pause and self.playing:
            self.playing, self.paused = False, True
        elif not do_pause and self.paused:
            self.playing, self.paused = True, False

    def stop(self):
        self.playing = self.paused = False

    def is_playing(self):
        return self.playing

    def audio_set_volume(self, value):
        self.volume = value

    def get_time(self):
        return self.time_ms

    def get_length(self):
        return self.length_ms


def _real_timers_for_plain_objects(monkeypatch):
    """PyQt needs a weak-referenceable owner for a bound-method timer target;
    the namespace harness window is not one. Keep the real timer and event
    loop, only wrapping the target in a plain function."""
    real_single_shot = QtCore.QTimer.singleShot

    def single_shot(msec, *args):
        target = args[-1]
        real_single_shot(msec, *args[:-1], lambda: target())

    monkeypatch.setattr(window_module.QtCore.QTimer, "singleShot", single_shot)


def _vlc_window(monkeypatch, tmp_path, names=("b.mp3", "c.mp3")):
    _real_timers_for_plain_objects(monkeypatch)
    window = queue_harness._window(monkeypatch, tmp_path, list(names), current="a.mp3")
    active, inactive = _FakeVlcPlayer("vlc-A"), _FakeVlcPlayer("vlc-B")
    for key, value in dict(
        use_simple=False, simple_player=None, simple_inactive_player=None,
        bass_player=None, bass_inactive_player=None, miniaudio_player=None,
        miniaudio_inactive_player=None, active_player=active, inactive_player=inactive,
        track_transition_mode="crossfade", crossfade_seconds=6.0, fade_from=None, fade_waits=0,
        fade_start=0.0, _inactive_normalisation_gain=1.0, _gain_token_seq=0, _active_gain_token=0,
        _inactive_gain_token=0, _gain_snapshot_cache={}, _ensure_vlc=lambda: None,
        _begin_playback_recovery=lambda *a, **kw: False,
        _sync_now_playing_overlay_for_media_type=lambda: None,
    ).items():
        setattr(window, key, value)

    def _play_on_player(player, path, volume_scale):
        player.load(path)
        player.play()
        window._set_volume(player, volume_scale)
        return True

    window._play_on_player = _play_on_player
    f2._transport_ui(window)
    f2._bind(
        window, "pause", "next_track", "stop_playback", "_start_crossfade_to", "_begin_fade",
        "_fade_tick", "_finish_crossfade", "_cancel_fade", "_set_volume", "_promote_inactive_gain_slot",
        "_next_gain_token", "_reapply_master_volume",
    )
    active.load(window.current_path)
    active.play()
    return window, active, inactive


def _select(window, name, crossfade=True):
    row = [os.path.basename(p) for p in window.queue].index(name)
    token, owner = window_module._claim_queue_selection_for(window, row, reason="test")
    assert window._play_path_direct(
        window.queue[row], crossfade=crossfade, queue_entry_token=token, queue_selection_owner=owner,
    )
    return window.queue[[os.path.basename(p) for p in window.queue].index(name)]


def test_vlc_incoming_crossfade_track_is_paused_with_playback_and_its_fade_held(monkeypatch, tmp_path):
    clock = f2._patch_clock(monkeypatch)
    window, active, inactive = _vlc_window(monkeypatch, tmp_path)
    b_path = _select(window, "b.mp3")  # crossfade: B starts silently on the inactive player
    assert inactive.playing is True and window.prebuffer_active is True

    window.pause()
    assert active.playing is False
    assert inactive.playing is False  # the incoming track does not run on through the Pause
    _spin(PREBUFFER_MS / 1000.0 + 0.3)  # the fade-begin timer fires while paused
    assert window.fade_active is False
    assert window._playback_intentionally_paused is True

    clock.advance(60.0)
    window.pause()  # Resume
    assert active.playing is True and inactive.playing is True
    assert window.fade_active is True  # the held fade begins once, now
    f2._drive_ticks(window, clock, 10, step=0.1)  # 1 s into a 6 s fade
    assert window.fade_active is True and window.active_player is active
    f2._drive_ticks(window, clock, 60, step=0.1)
    assert window.fade_active is False and window.active_player is inactive
    assert inactive.path == b_path


def test_vlc_pause_mid_fade_freezes_the_incoming_track_and_resumes_the_fade_in_place(monkeypatch, tmp_path):
    clock = f2._patch_clock(monkeypatch)
    window, active, inactive = _vlc_window(monkeypatch, tmp_path)
    _select(window, "b.mp3")
    _spin(PREBUFFER_MS / 1000.0 + 0.3)
    assert window.fade_active is True
    f2._drive_ticks(window, clock, 20, step=0.1)  # 2 s into the fade
    volumes = (active.volume, inactive.volume)

    window.pause()
    assert inactive.playing is False
    f2._drive_ticks(window, clock, 300, step=0.1)  # 30 s of paused wall clock
    assert (active.volume, inactive.volume) == volumes and window.fade_active is True

    window.pause()  # Resume
    assert inactive.playing is True
    f2._drive_ticks(window, clock, 1, step=0.03)
    assert window.fade_active is True  # not completed by the paused time
    assert inactive.volume == pytest.approx(volumes[1], abs=5)


def test_stale_vlc_fade_timer_cannot_begin_a_newer_crossfade_early(monkeypatch, tmp_path):
    window, active, inactive = _vlc_window(monkeypatch, tmp_path)
    _select(window, "b.mp3")  # crossfade to B; its fade timer is pending
    window._cancel_fade()  # abandoned (the incoming player is left as it was)
    inactive.stop()
    _spin(0.25)
    _select(window, "c.mp3")  # a newer crossfade to C, its own timer 500 ms from now
    assert window.prebuffer_active is True

    _spin(0.35)  # B's stale timer fires; C's has not
    assert window.fade_active is False

    _spin(0.4)  # C's own timer
    assert window.fade_active is True


def test_explicit_selection_while_a_vlc_crossfade_is_paused_plays_it_and_nothing_stale_resumes(monkeypatch, tmp_path):
    window, active, inactive = _vlc_window(monkeypatch, tmp_path)
    _select(window, "b.mp3")
    window.pause()

    window.next_track()  # existing semantics: Next is ignored while a crossfade is under way
    assert window.prebuffer_active is True and window._playback_intentionally_paused is True

    _select(window, "c.mp3")  # an explicit selection: nothing is playing, so a direct cut to C
    _spin(PREBUFFER_MS / 1000.0 + 0.3)  # B's fade timer fires

    assert active.playing is True and os.path.basename(active.path) == "c.mp3"
    assert inactive.playing is False
    assert window.fade_active is False and window.prebuffer_active is False
    assert window._playback_intentionally_paused is False
    assert window._current_playback_attempt.state is PlaybackAttemptState.PLAYING


def test_stop_while_a_vlc_crossfade_is_paused_discards_it(monkeypatch, tmp_path):
    window, active, inactive = _vlc_window(monkeypatch, tmp_path)
    _select(window, "b.mp3")
    window.pause()

    window.stop_playback()
    _spin(PREBUFFER_MS / 1000.0 + 0.3)

    assert active.playing is False and inactive.playing is False
    assert window.fade_active is False and window.prebuffer_active is False
    assert window._playback_intentionally_paused is False
