"""_update_dj_info must never block the GUI thread on file I/O.

Captured in the same GUI-stall trace as the album-tag-refresh freeze
(tests/test_album_tag_refresh_worker.py): the main thread stuck inside a
synchronous mutagen.File() call reached via
_play_video_path_direct -> _activate_track_ui -> _update_dj_info ->
_queue_track_details, while starting a video whose tags weren't cached
yet. _queue_track_details already has a cached_details_only=True mode
that skips the blocking mutagen call (documented at its other three call
sites as "must never touch slow/network paths") -- _update_dj_info was
the one caller that didn't use it.
"""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import billsmusic.window as window_module
from billsmusic.window import PlayerWindow


def _dj_info_window(queue_detail_cache=None, cached_analysis=None):
    window = SimpleNamespace(
        queue_detail_cache=queue_detail_cache if queue_detail_cache is not None else {},
        _backend_label=lambda: "BASS",
        _cached_queue_analysis=lambda path, validate_signature=False: cached_analysis,
        _backfill_scan_percent=None,
        dj_info=SimpleNamespace(setText=lambda text: None),
        _meta_by_path={},
    )
    window._queue_track_details = lambda path, cached_details_only=False: (
        PlayerWindow._queue_track_details(window, path, cached_details_only=cached_details_only)
    )
    window._set_dj_info = lambda text: PlayerWindow._set_dj_info(window, text)
    window._refresh_dj_info_display = lambda: PlayerWindow._refresh_dj_info_display(window)
    return window


def test_update_dj_info_does_not_call_mutagen_for_an_uncached_track(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError(
            "_update_dj_info must use cached_details_only=True -- calling "
            "MutagenFile() directly here is what froze the GUI"
        )

    monkeypatch.setattr(window_module, "MutagenFile", _boom)
    window = _dj_info_window()
    info = SimpleNamespace(duration="3:45", bitrate="192k")

    PlayerWindow._update_dj_info(window, info, "Y:/network/share/video.mp4")  # must not raise


def test_update_dj_info_shows_key_bpm_when_already_cached(monkeypatch):
    monkeypatch.setattr(
        window_module, "MutagenFile",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not touch mutagen at all")),
    )
    shown = {}
    window = _dj_info_window(
        queue_detail_cache={"song.mp3": {"bitrate": "320k", "time": "3:30", "key": "Am", "bpm": "120"}},
    )
    window.dj_info = SimpleNamespace(setText=lambda text: shown.setdefault("text", text))

    PlayerWindow._update_dj_info(window, SimpleNamespace(duration="Unknown", bitrate="Unknown"), "song.mp3")

    assert "Time: 3:30" in shown["text"]
    assert "Rate: 320k" in shown["text"]
    assert "Key: Am" in shown["text"]
    assert "BPM: 120" in shown["text"]


def test_update_dj_info_omits_key_bpm_when_not_yet_analysed():
    shown = {}
    window = _dj_info_window()  # empty cache, no cached analysis
    window.dj_info = SimpleNamespace(setText=lambda text: shown.setdefault("text", text))

    PlayerWindow._update_dj_info(window, SimpleNamespace(duration="4:00", bitrate="256k"), "new_track.mp4")

    assert "Key:" not in shown["text"]
    assert "BPM:" not in shown["text"]
    assert "Time: 4:00" in shown["text"]
