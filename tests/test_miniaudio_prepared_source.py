"""Playback stability hardening, Phase C1 (native audio backend
ownership, 2026-09-10): PreparedMiniaudioSource and
MiniaudioPlayer.prepare_source()/commit_prepared().

Miniaudio preparation gathers only immutable metadata (no native
device/stream construction off-thread, per the Phase C1 design) -- so
there is no HSTREAM-equivalent resource to leak or double-free here.
These tests exist for the same exactly-once-resolution discipline and
API symmetry with PreparedBassStream (window.py's commit/discard call
sites treat both prepared types identically), not because miniaudio has
its own resource-ownership hazard.
"""
from unittest.mock import MagicMock, patch

import pytest

from billsmusic.miniaudio_player import MiniaudioPlayer, PreparedMiniaudioSource


def _fake_info(sample_rate=44100, nchannels=2, num_frames=44100 * 10):
    info = MagicMock()
    info.sample_rate = sample_rate
    info.nchannels = nchannels
    info.num_frames = num_frames
    return info


def test_take_marks_resolved_exactly_once():
    source = PreparedMiniaudioSource("track.flac", 44100, 2, 10.0)
    assert source.resolved is False
    source.take()
    assert source.resolved is True


def test_double_take_raises():
    source = PreparedMiniaudioSource("track.flac", 44100, 2, 10.0)
    source.take()
    with pytest.raises(RuntimeError):
        source.take()


def test_take_after_discard_raises():
    source = PreparedMiniaudioSource("track.flac", 44100, 2, 10.0)
    source.discard()
    with pytest.raises(RuntimeError):
        source.take()


def test_discard_after_take_is_a_safe_noop():
    source = PreparedMiniaudioSource("track.flac", 44100, 2, 10.0)
    source.take()
    assert source.discard() is True  # no-op, no exception
    assert source.resolved is True


def test_double_discard_is_safe():
    source = PreparedMiniaudioSource("track.flac", 44100, 2, 10.0)
    assert source.discard() is True
    assert source.discard() is True


def test_prepare_source_reads_immutable_metadata_without_touching_a_player():
    with patch("billsmusic.miniaudio_player.miniaudio.get_file_info", return_value=_fake_info()) as get_info:
        source = MiniaudioPlayer.prepare_source("track.flac")
    get_info.assert_called_once_with("track.flac")
    assert source.path == "track.flac"
    assert source.sample_rate == 44100
    assert source.channels == 2
    assert source.duration == 10.0
    assert source.resolved is False


def test_commit_prepared_adopts_metadata_and_resolves_the_source():
    with patch("billsmusic.miniaudio_player.miniaudio.get_file_info", return_value=_fake_info(sample_rate=48000, nchannels=1, num_frames=48000 * 5)):
        source = MiniaudioPlayer.prepare_source("track.flac")
    player = MiniaudioPlayer()
    ok = player.commit_prepared(source)
    assert ok is True
    assert player._path == "track.flac"
    assert player._sample_rate == 48000
    assert player._channels == 1
    assert player._length == 5.0
    assert source.resolved is True


def test_commit_prepared_rejects_an_already_resolved_source():
    source = PreparedMiniaudioSource("track.flac", 44100, 2, 10.0)
    source.take()  # already resolved by someone else
    player = MiniaudioPlayer()
    assert player.commit_prepared(source) is False
    assert player._path == ""  # untouched
