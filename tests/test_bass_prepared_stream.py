"""Playback stability hardening, Phase C1 (native audio backend
ownership, 2026-09-10): exactly-once resource-ownership and exception-
safety tests for PreparedBassStream and BassPlayer.prepare_stream()/
commit_prepared().

Mocked at the _BassEngine.ensure() boundary -- matching this suite's
existing convention of never touching a real bass.dll for pure
ownership-logic tests (the real DLL is exercised separately, in
test_bass_fft_visualiser.py/test_bass_url_streaming.py, and in the
cross-thread integration tests in test_bass_stream_prepare_worker_real.py).

The core invariant under test: CANDIDATE -> exactly one of TRANSFERRED
(via take(), never a native free) or DISCARDED (via discard(), exactly
one BASS_StreamFree) -- no state may ever let a candidate's handle be
freed twice, or let both a candidate and a BassPlayer believe they own
the same handle.
"""
from unittest.mock import MagicMock, patch

import pytest

from billsmusic.bass_player import (
    BassLoadError, BassPlayer, PreparedBassStream, PreparedBassStreamPayload,
    _StreamOwnership,
)


def _fake_bass(stream_handle=111, free_ok=True):
    bass = MagicMock()
    bass.BASS_StreamCreateFile.return_value = stream_handle
    bass.BASS_StreamCreateURL.return_value = stream_handle
    bass.BASS_ChannelGetLength.return_value = 1000
    bass.BASS_ChannelBytes2Seconds.return_value = 12.5
    bass.BASS_StreamFree.return_value = free_ok
    bass.BASS_ChannelStop.return_value = True
    bass.BASS_ChannelSetAttribute.return_value = True
    return bass


# ---------------------------------------------------------------------------
# PreparedBassStream: exactly-once ownership state machine
# ---------------------------------------------------------------------------

def test_take_transitions_candidate_to_transferred_and_returns_payload():
    candidate = PreparedBassStream(111, "track.flac", 12.5, url_buffer=b"buf")
    payload = candidate.take()
    assert isinstance(payload, PreparedBassStreamPayload)
    assert payload.handle == 111
    assert payload.identity == "track.flac"
    assert payload.length == 12.5
    assert payload.url_buffer == b"buf"
    assert candidate.ownership is _StreamOwnership.TRANSFERRED


def test_take_clears_the_candidates_own_handle_so_it_cannot_act_on_it_again():
    candidate = PreparedBassStream(111, "track.flac", 12.5, url_buffer=b"buf")
    candidate.take()
    assert candidate._handle == 0
    assert candidate._url_buffer is None


def test_double_take_raises_and_never_returns_a_second_payload():
    candidate = PreparedBassStream(111, "track.flac", 12.5)
    candidate.take()
    with pytest.raises(RuntimeError):
        candidate.take()


def test_take_leaves_the_candidate_still_owning_the_handle_if_payload_construction_raises(monkeypatch):
    # 2026-09-11 correction: if PreparedBassStreamPayload(...) itself
    # raises, take() must not have already flipped ownership to
    # TRANSFERRED -- otherwise the handle is stranded (nobody received
    # it, and discard() afterward believes there's nothing left to free).
    def _raising_payload(*a, **kw):
        raise RuntimeError("payload construction failed")
    monkeypatch.setattr("billsmusic.bass_player.PreparedBassStreamPayload", _raising_payload)

    candidate = PreparedBassStream(123, "track.flac", 12.5)
    with pytest.raises(RuntimeError):
        candidate.take()

    assert candidate.ownership is _StreamOwnership.CANDIDATE
    assert candidate._handle == 123

    # discard() must still work correctly afterward -- freeing the
    # handle exactly once, not a no-op because ownership was wrongly
    # already TRANSFERRED.
    bass = _fake_bass()
    with patch("billsmusic.bass_player._BassEngine.ensure", return_value=bass):
        assert candidate.discard() is True
    bass.BASS_StreamFree.assert_called_once_with(123)


def test_discard_after_take_is_a_safe_noop_and_never_frees_natively():
    with patch("billsmusic.bass_player._BassEngine.ensure", return_value=_fake_bass()) as ensure:
        candidate = PreparedBassStream(111, "track.flac", 12.5)
        candidate.take()
        result = candidate.discard()
        assert result is True
        ensure.assert_not_called()  # discard() after take() never touches BASS at all
    assert candidate.ownership is _StreamOwnership.TRANSFERRED  # not overwritten to DISCARDED


def test_take_after_discard_raises():
    with patch("billsmusic.bass_player._BassEngine.ensure", return_value=_fake_bass()):
        candidate = PreparedBassStream(111, "track.flac", 12.5)
        candidate.discard()
        with pytest.raises(RuntimeError):
            candidate.take()


def test_double_discard_frees_the_native_handle_exactly_once():
    bass = _fake_bass()
    with patch("billsmusic.bass_player._BassEngine.ensure", return_value=bass):
        candidate = PreparedBassStream(111, "track.flac", 12.5)
        assert candidate.discard() is True
        assert candidate.discard() is True  # safe no-op, not a second free
    bass.BASS_StreamFree.assert_called_once_with(111)
    assert candidate.ownership is _StreamOwnership.DISCARDED


def test_discard_reports_failure_when_bass_reports_failure_but_still_resolves():
    with patch("billsmusic.bass_player._BassEngine.ensure", return_value=_fake_bass(free_ok=False)):
        candidate = PreparedBassStream(111, "track.flac", 12.5)
        assert candidate.discard() is False
    # Still fully resolved -- a failed native free must not leave the
    # candidate looking like it's still CANDIDATE (which would invite a
    # second, possibly also-failing, free attempt from elsewhere).
    assert candidate.ownership is _StreamOwnership.DISCARDED


def test_discard_swallows_a_raising_native_call_and_reports_failure():
    bass = _fake_bass()
    bass.BASS_StreamFree.side_effect = OSError("native failure")
    with patch("billsmusic.bass_player._BassEngine.ensure", return_value=bass):
        candidate = PreparedBassStream(111, "track.flac", 12.5)
        assert candidate.discard() is False  # never raises out of discard()
    assert candidate.ownership is _StreamOwnership.DISCARDED


def test_discard_with_no_handle_is_a_cheap_noop():
    with patch("billsmusic.bass_player._BassEngine.ensure") as ensure:
        candidate = PreparedBassStream(0, "track.flac", 0.0)
        assert candidate.discard() is True
        ensure.assert_not_called()  # nothing to free -- never even touches BASS


# ---------------------------------------------------------------------------
# BassPlayer.prepare_stream(): exception safety
# ---------------------------------------------------------------------------

def test_prepare_stream_returns_a_candidate_owning_the_new_handle():
    bass = _fake_bass(stream_handle=222)
    with patch("billsmusic.bass_player._BassEngine.ensure", return_value=bass):
        candidate = BassPlayer.prepare_stream("track.flac")
    assert candidate.ownership is _StreamOwnership.CANDIDATE
    payload = candidate.take()
    assert payload.handle == 222
    assert payload.identity == "track.flac"
    assert payload.length == 12.5


def test_prepare_stream_raises_and_frees_nothing_when_stream_creation_itself_fails():
    bass = _fake_bass()
    bass.BASS_StreamCreateFile.return_value = 0  # BASS's own "failed" signal
    with patch("billsmusic.bass_player._BassEngine.ensure", return_value=bass):
        with pytest.raises(BassLoadError):
            BassPlayer.prepare_stream("track.flac")
    bass.BASS_StreamFree.assert_not_called()  # nothing was ever created to free


def test_prepare_stream_frees_the_orphaned_handle_exactly_once_if_a_later_step_raises():
    # BASS_StreamCreateFile succeeds, but BASS_ChannelGetLength (called
    # right after, still inside prepare_stream, before a PreparedBassStream
    # exists to own the handle) raises -- prepare_stream's own finally
    # block must free the orphaned handle exactly once, since no
    # PreparedBassStream was ever constructed to be responsible for it.
    bass = _fake_bass(stream_handle=333)
    bass.BASS_ChannelGetLength.side_effect = OSError("native failure")
    with patch("billsmusic.bass_player._BassEngine.ensure", return_value=bass):
        with pytest.raises(OSError):
            BassPlayer.prepare_stream("track.flac")
    bass.BASS_StreamFree.assert_called_once_with(333)


def test_prepare_stream_for_a_plex_transport_source_uses_the_logical_identity():
    from billsmusic.plex_transport import PlexTransportSource

    bass = _fake_bass(stream_handle=444)
    source = PlexTransportSource(identity="plex://server-1/1.mp3", transport_url="http://host/1.mp3")
    with patch("billsmusic.bass_player._BassEngine.ensure", return_value=bass):
        candidate = BassPlayer.prepare_stream(source)
    payload = candidate.take()
    assert payload.identity == "plex://server-1/1.mp3"  # never the transport URL
    bass.BASS_StreamCreateURL.assert_called_once()
    assert payload.url_buffer is not None  # kept alive for the stream's lifetime


# ---------------------------------------------------------------------------
# BassPlayer.commit_prepared(): exception safety and ordering
# ---------------------------------------------------------------------------

def test_commit_prepared_adopts_the_handle_and_applies_volume():
    bass = _fake_bass(stream_handle=444)
    with patch("billsmusic.bass_player._BassEngine.ensure", return_value=bass):
        candidate = PreparedBassStream(444, "track.flac", 12.5)
        player = BassPlayer()
        ok = player.commit_prepared(candidate)
    assert ok is True
    assert player._stream == 444
    assert player._path == "track.flac"
    assert player._length == 12.5
    assert candidate.ownership is _StreamOwnership.TRANSFERRED
    bass.BASS_ChannelSetAttribute.assert_called()  # set_volume ran as part of adoption


def test_commit_prepared_rejects_an_already_resolved_candidate():
    candidate = PreparedBassStream(555, "track.flac", 12.5)
    candidate.take()  # already resolved by someone else
    player = BassPlayer()
    assert player.commit_prepared(candidate) is False
    assert player._stream == 0  # untouched


def test_commit_prepared_discards_candidate_if_stopping_the_old_stream_fails():
    # Ordering under test (the revised Phase C1 design): stop() runs
    # BEFORE take(), so a failure there leaves the candidate having never
    # relinquished its own handle -- a plain discard() is correct and
    # sufficient, and the commit must not have happened.
    bass = _fake_bass()
    with patch("billsmusic.bass_player._BassEngine.ensure", return_value=bass):
        player = BassPlayer()
        player._stream = 999  # a pre-existing "old" stream to be stopped
        bass.BASS_ChannelStop.side_effect = OSError("native failure")

        candidate = PreparedBassStream(111, "track.flac", 12.5)
        with pytest.raises(OSError):
            player.commit_prepared(candidate)

    assert candidate.ownership is _StreamOwnership.DISCARDED  # candidate cleaned itself up
    bass.BASS_StreamFree.assert_any_call(111)  # the candidate's own handle, freed by discard()
    assert player._stream != 111  # commit never happened


def test_commit_prepared_cleans_up_its_own_stream_if_post_adoption_setup_fails():
    # Adoption (self._stream = payload.handle) already completed by the
    # time set_volume raises -- take() already ran, so the candidate must
    # never be touched again; THIS PLAYER cleans up its own newly-owned
    # stream instead (never a double-free of the same handle).
    bass = _fake_bass(stream_handle=777)
    with patch("billsmusic.bass_player._BassEngine.ensure", return_value=bass):
        player = BassPlayer()
        candidate = PreparedBassStream(777, "track.flac", 12.5)
        bass.BASS_ChannelSetAttribute.side_effect = OSError("native failure")

        with pytest.raises(OSError):
            player.commit_prepared(candidate)

    assert candidate.ownership is _StreamOwnership.TRANSFERRED  # take() ran once, never re-touched
    bass.BASS_StreamFree.assert_called_once_with(777)  # freed by the player's own cleanup, once
    assert player._stream == 0


def test_commit_prepared_frees_the_old_stream_before_adopting_the_new_one():
    bass = _fake_bass(stream_handle=222)
    with patch("billsmusic.bass_player._BassEngine.ensure", return_value=bass):
        player = BassPlayer()
        player._stream = 111  # the old, currently-authoritative stream
        candidate = PreparedBassStream(222, "track.flac", 12.5)
        player.commit_prepared(candidate)
    bass.BASS_StreamFree.assert_called_once_with(111)  # only the OLD handle freed
    assert player._stream == 222  # the new one is now authoritative
