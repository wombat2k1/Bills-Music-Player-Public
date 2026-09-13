"""Tests for billsmusic.workers.BassStreamPrepareWorker and
MiniaudioSourcePrepareWorker -- the Phase C1 (native audio backend
ownership, 2026-09-10) replacements for the old PlexAudioLoadWorker/
PlayerLoadWorker. Both workers hold NO reference to any BassPlayer/
MiniaudioPlayer instance -- only a source/path and a token -- and only
ever produce a private prepared candidate via the corresponding
player class's static prepare_stream()/prepare_source(). Structural
assertions below (test_worker_stores_no_player_reference*) prove this
directly, satisfying the C1 design's "no async worker owns a live
player instance" requirement.

run() is called directly (not via .start()) so these stay fast,
deterministic unit tests with no real OS thread involved -- the
threading machinery is Qt's own, already covered by the real
cross-thread tests in test_bass_stream_prepare_worker_real.py.
"""
from unittest.mock import patch

from billsmusic.workers import BassStreamPrepareWorker, MiniaudioSourcePrepareWorker


class _FakeBassPreparedStream:
    def __init__(self, source):
        self.source = source


def test_bass_worker_emits_prepared_with_candidate_from_prepare_stream():
    candidate = _FakeBassPreparedStream("track.flac")
    with patch("billsmusic.bass_player.BassPlayer.prepare_stream", return_value=candidate) as mock_prepare:
        worker = BassStreamPrepareWorker("track.flac", 42)
        prepared = []
        failed = []
        worker.prepared.connect(lambda token, ident, cand: prepared.append((token, ident, cand)))
        worker.failed.connect(lambda token, ident, err: failed.append((token, ident, err)))

        worker.run()

        mock_prepare.assert_called_once_with("track.flac")
        assert prepared == [(42, "track.flac", candidate)]
        assert failed == []


def test_bass_worker_emits_failed_on_exception_without_raising():
    with patch("billsmusic.bass_player.BassPlayer.prepare_stream", side_effect=OSError("network path unavailable")):
        worker = BassStreamPrepareWorker("track.flac", 7)
        prepared = []
        failed = []
        worker.prepared.connect(lambda token, ident, cand: prepared.append((token, ident, cand)))
        worker.failed.connect(lambda token, ident, err: failed.append((token, ident, err)))

        worker.run()  # must not raise -- the exception is reported via the signal

        assert prepared == []
        assert len(failed) == 1
        token, ident, error = failed[0]
        assert token == 7
        assert ident == "track.flac"
        assert "network path unavailable" in error


def test_bass_worker_uses_plex_identity_for_a_plex_transport_source():
    from billsmusic.plex_transport import PlexTransportSource

    source = PlexTransportSource(identity="plex://server-1/1.mp3", transport_url="http://host/1.mp3")
    candidate = _FakeBassPreparedStream(source)
    with patch("billsmusic.bass_player.BassPlayer.prepare_stream", return_value=candidate):
        worker = BassStreamPrepareWorker(source, 1)
        prepared = []
        worker.prepared.connect(lambda token, ident, cand: prepared.append((token, ident, cand)))

        worker.run()

        assert prepared == [(1, "plex://server-1/1.mp3", candidate)]


def test_bass_worker_claim_candidate_returns_it_once_then_none_forever():
    # Phase C2 (worker lifetime / shutdown ownership, 2026-09-11):
    # _prepared_candidate/claim_candidate() is how the worker's result
    # stays resolvable even if the GUI thread never processes the
    # queued `prepared` signal -- exactly-once, from whichever caller
    # (normal GUI callback or shutdown finalizer) reaches it first.
    candidate = _FakeBassPreparedStream("track.flac")
    with patch("billsmusic.bass_player.BassPlayer.prepare_stream", return_value=candidate):
        worker = BassStreamPrepareWorker("track.flac", 1)
        worker.run()

        assert worker.claim_candidate() is candidate
        assert worker.claim_candidate() is None  # second caller: nothing left
        assert worker.claim_candidate() is None  # any further caller: still nothing


def test_bass_worker_claim_candidate_is_none_when_prepare_failed():
    with patch("billsmusic.bass_player.BassPlayer.prepare_stream", side_effect=OSError("gone")):
        worker = BassStreamPrepareWorker("track.flac", 1)
        worker.run()

        assert worker.claim_candidate() is None


def test_bass_worker_stores_only_source_and_token_no_player_reference():
    worker = BassStreamPrepareWorker("track.flac", 1)
    assert worker.source == "track.flac"
    assert worker.token == 1
    assert not hasattr(worker, "player")
    assert not any(
        type(value).__name__ in ("BassPlayer", "MiniaudioPlayer")
        for value in vars(worker).values()
    )


class _FakeMiniaudioPreparedSource:
    def __init__(self, path):
        self.path = path


def test_miniaudio_worker_emits_prepared_with_candidate_from_prepare_source():
    candidate = _FakeMiniaudioPreparedSource("track.flac")
    with patch("billsmusic.miniaudio_player.MiniaudioPlayer.prepare_source", return_value=candidate) as mock_prepare:
        worker = MiniaudioSourcePrepareWorker("track.flac", 42)
        prepared = []
        failed = []
        worker.prepared.connect(lambda token, path, cand: prepared.append((token, path, cand)))
        worker.failed.connect(lambda token, path, err: failed.append((token, path, err)))

        worker.run()

        mock_prepare.assert_called_once_with("track.flac")
        assert prepared == [(42, "track.flac", candidate)]
        assert failed == []


def test_miniaudio_worker_emits_failed_on_exception_without_raising():
    with patch("billsmusic.miniaudio_player.MiniaudioPlayer.prepare_source", side_effect=OSError("network path unavailable")):
        worker = MiniaudioSourcePrepareWorker("track.flac", 7)
        failed = []
        worker.failed.connect(lambda token, path, err: failed.append((token, path, err)))

        worker.run()

        assert len(failed) == 1
        token, path, error = failed[0]
        assert token == 7
        assert path == "track.flac"
        assert "network path unavailable" in error


def test_miniaudio_worker_claim_candidate_returns_it_once_then_none_forever():
    candidate = _FakeMiniaudioPreparedSource("track.flac")
    with patch("billsmusic.miniaudio_player.MiniaudioPlayer.prepare_source", return_value=candidate):
        worker = MiniaudioSourcePrepareWorker("track.flac", 1)
        worker.run()

        assert worker.claim_candidate() is candidate
        assert worker.claim_candidate() is None
        assert worker.claim_candidate() is None


def test_miniaudio_worker_claim_candidate_is_none_when_prepare_failed():
    with patch("billsmusic.miniaudio_player.MiniaudioPlayer.prepare_source", side_effect=OSError("gone")):
        worker = MiniaudioSourcePrepareWorker("track.flac", 1)
        worker.run()

        assert worker.claim_candidate() is None


def test_miniaudio_worker_stores_only_path_and_token_no_player_reference():
    worker = MiniaudioSourcePrepareWorker("track.flac", 1)
    assert worker.path == "track.flac"
    assert worker.token == 1
    assert not hasattr(worker, "player")
    assert not any(
        type(value).__name__ in ("BassPlayer", "MiniaudioPlayer")
        for value in vars(worker).values()
    )
