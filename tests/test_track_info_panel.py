"""v1.0.67 MainThread I/O hardening: the "Show Track Info" overlay.

_show_track_info used to call _read_tags(path) (a Mutagen open)
synchronously on the GUI thread -- a one-off, user-triggered menu action,
but still capable of freezing the window for as long as a slow/NAS read
took. It now shows _load_cached_audio_tags(path) immediately (no I/O)
and refreshes via TrackTagLoadWorker (billsmusic/workers.py, already
used for the same purpose on the automatic playback-activation path)
once the authoritative read completes, dropping a result that arrives
for a path the user has since moved away from.
"""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import billsmusic.window as window_module
from billsmusic.window import PlayerWindow
from billsmusic.worker_registry import WorkerLifetimeRegistry


def _info_window(**overrides):
    displayed = []
    diagnostics_events = []
    window = SimpleNamespace(
        _closing=False,
        current_path=None,
        _track_tag_load_workers=[],
        _worker_registry=WorkerLifetimeRegistry(),
        _load_cached_audio_tags=lambda path: SimpleNamespace(title="Cached", path=path),
        _display_track_info=lambda info, path: displayed.append((info, path)),
        diagnostics=SimpleNamespace(
            record=lambda category, op, **kw: diagnostics_events.append((category, op, kw)),
            path_details=lambda value: {"path_hash": f"hash-of-{value}"},
        ),
    )
    for key, value in overrides.items():
        setattr(window, key, value)
    return window, displayed, diagnostics_events


def test_show_track_info_never_reads_tags_synchronously(monkeypatch):
    def _fail(*a, **kw):
        raise AssertionError(
            "_show_track_info must not call _read_tags/Mutagen "
            "synchronously -- this used to freeze the window on a "
            "NAS-backed track for a one-off menu action"
        )
    monkeypatch.setattr(window_module, "MutagenFile", _fail)
    window, displayed, _diag = _info_window()

    class _FakeWorker:
        def __init__(self, path):
            self.tags_ready = SimpleNamespace(connect=lambda slot: None)
            self.finished = SimpleNamespace(connect=lambda slot: None)
        def start(self):
            pass

    monkeypatch.setattr(window_module, "TrackTagLoadWorker", _FakeWorker)

    PlayerWindow._show_track_info(window, "Y:/network/share/song.mp3")

    assert displayed == [(window._load_cached_audio_tags("Y:/network/share/song.mp3"), "Y:/network/share/song.mp3")]
    assert len(window._track_tag_load_workers) == 1


def test_show_track_info_defaults_to_current_path(monkeypatch):
    class _FakeWorker:
        def __init__(self, path):
            self.tags_ready = SimpleNamespace(connect=lambda slot: None)
            self.finished = SimpleNamespace(connect=lambda slot: None)
        def start(self):
            pass

    monkeypatch.setattr(window_module, "TrackTagLoadWorker", _FakeWorker)
    window, displayed, _diag = _info_window(current_path="now_playing.mp3")

    PlayerWindow._show_track_info(window, None)

    assert displayed[0][1] == "now_playing.mp3"


def test_show_track_info_does_nothing_without_a_path():
    window, displayed, _diag = _info_window(current_path=None)
    PlayerWindow._show_track_info(window, None)
    assert displayed == []


def test_show_track_info_applies_a_matching_result():
    class _FakeSignal:
        def __init__(self):
            self.slot = None
        def connect(self, slot):
            self.slot = slot

    class _FakeWorker:
        def __init__(self, path):
            self.tags_ready = _FakeSignal()
            self.finished = _FakeSignal()
        def start(self):
            pass

    import billsmusic.window as wm
    original = wm.TrackTagLoadWorker
    wm.TrackTagLoadWorker = _FakeWorker
    try:
        window, displayed, _diag = _info_window()
        PlayerWindow._show_track_info(window, "song.mp3")
        worker = window._track_tag_load_workers[0]
        assert len(displayed) == 1  # the cached-immediate display

        worker.tags_ready.slot("song.mp3", {
            "title": "Fresh", "artist": "Unknown", "album": "Unknown", "genre": "Unknown",
            "bitrate": "Unknown", "sample_rate": "Unknown", "channels": "Unknown", "duration": "Unknown",
        })
        assert len(displayed) == 2
        assert displayed[-1][0].title == "Fresh"
    finally:
        wm.TrackTagLoadWorker = original


def test_show_track_info_drops_a_result_for_a_path_the_user_moved_away_from(monkeypatch):
    # Two requests in flight (song A, then song B before A's background
    # read completed) -- A's late result must not clobber B's already-
    # displayed info. There's no "current track" concept for this overlay
    # (unlike the Tags panel), so this is guarded by
    # window._track_info_panel_path instead.
    class _FakeSignal:
        def __init__(self):
            self.slot = None
        def connect(self, slot):
            self.slot = slot

    workers = {}

    class _FakeWorker:
        def __init__(self, path):
            self.path = path
            self.tags_ready = _FakeSignal()
            self.finished = _FakeSignal()
            workers[path] = self
        def start(self):
            pass

    monkeypatch.setattr(window_module, "TrackTagLoadWorker", _FakeWorker)
    window, displayed, _diag = _info_window()

    PlayerWindow._show_track_info(window, "song_a.mp3")
    PlayerWindow._show_track_info(window, "song_b.mp3")
    assert len(displayed) == 2  # both cached-immediate displays

    fields = {
        "artist": "Unknown", "album": "Unknown", "genre": "Unknown",
        "bitrate": "Unknown", "sample_rate": "Unknown", "channels": "Unknown", "duration": "Unknown",
    }
    workers["song_a.mp3"].tags_ready.slot("song_a.mp3", {"title": "Late A", **fields})
    assert len(displayed) == 2  # dropped -- panel has already moved on to song_b

    workers["song_b.mp3"].tags_ready.slot("song_b.mp3", {"title": "Fresh B", **fields})
    assert len(displayed) == 3
    assert displayed[-1][0].title == "Fresh B"


def test_show_track_info_never_starts_a_worker_for_a_plex_identity(monkeypatch):
    # Stage 3A-r3 real-device defect: same root cause as
    # test_track_tag_load_worker.py's matching test -- Mutagen can never
    # read a synthetic plex:// identity, and read_full_tag_display's
    # failure path returns a *successful* all-Unknown result rather than
    # raising, so a worker started here would silently overwrite the
    # already-correct cached Plex title.
    def _exploding_worker(*a, **kw):
        raise AssertionError(
            "TrackTagLoadWorker must never be constructed for a plex:// "
            "identity from the Show Track Info panel either"
        )
    monkeypatch.setattr(window_module, "TrackTagLoadWorker", _exploding_worker)
    window, displayed, diag = _info_window()

    PlayerWindow._show_track_info(window, "plex://server-1/151212.flac")

    assert len(displayed) == 1  # the cached-immediate display only
    assert window._track_tag_load_workers == []
    skipped = [e for e in diag if e[1] == "track_tags_async_skipped_for_plex"]
    assert len(skipped) == 1
