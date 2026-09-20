"""v1.0.67 MainThread I/O hardening: synced-lyrics loading.

_load_lrc_for_track used to open the media file with Mutagen (and, on a
miss, read a sidecar .lrc file) synchronously on every AUDIO track
activation -- the same class of NAS-stall bug the video/karaoke branch
right next to it had already been fixed for (a real ~124ms stall). The
fix moves the actual read onto LyricsLoadWorker (billsmusic/workers.py),
backed by pure functions in billsmusic/lyrics.py; _load_lrc_for_track
itself now only ever consumes an in-memory cache immediately or kicks off
that worker.

Covers: the extracted lyrics.py logic still behaves exactly as the old
inline PlayerWindow methods did, the cache-hit path performs zero I/O and
zero worker construction, and a worker result arriving for a track the
user has already skipped past is dropped (generation/path guard), mirroring
the established _now_playing_generation pattern used throughout this file.
"""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import billsmusic.window as window_module
from billsmusic import lyrics as lyrics_module
from billsmusic.window import PlayerWindow


# ---------------------------------------------------------------------------
# billsmusic/lyrics.py -- pure extraction/parsing logic
# ---------------------------------------------------------------------------

def test_parse_lrc_text_extracts_timestamped_lines():
    text = "[00:01.50]First line\n[00:03.00]Second line\n"
    entries = lyrics_module.parse_lrc_text(text)
    assert entries == [(1.5, "First line"), (3.0, "Second line")]


def test_parse_lrc_text_ignores_lines_without_a_timestamp():
    entries = lyrics_module.parse_lrc_text("[ar:Some Artist]\nno timestamp here\n")
    assert entries == []


def test_coerce_tag_text_values_flattens_nested_and_bytes():
    assert lyrics_module.coerce_tag_text_values(b"hello") == ["hello"]
    assert lyrics_module.coerce_tag_text_values(["a", ["b", "c"]]) == ["a", "b", "c"]
    assert lyrics_module.coerce_tag_text_values(None) == []


def test_extract_synced_lyrics_from_tags_prefers_sylt_frames(monkeypatch):
    class _FakeFrame:
        text = [("Line one", 1000), ("Line two", 2500)]

    class _FakeTags:
        def getall(self, key):
            return [_FakeFrame()] if key == "SYLT" else []
        def get(self, key):
            return None
        def items(self):
            return []

    class _FakeAudio:
        tags = _FakeTags()

    monkeypatch.setattr(lyrics_module, "MutagenFile", lambda path: _FakeAudio())
    entries = lyrics_module.extract_synced_lyrics_from_tags("song.mp3")
    assert entries == [(1.0, "Line one"), (2.5, "Line two")]


def test_extract_synced_lyrics_from_tags_returns_empty_when_no_tags(monkeypatch):
    monkeypatch.setattr(lyrics_module, "MutagenFile", lambda path: None)
    assert lyrics_module.extract_synced_lyrics_from_tags("song.mp3") == []


def test_load_lyrics_for_track_falls_back_to_sidecar_lrc(tmp_path, monkeypatch):
    monkeypatch.setattr(lyrics_module, "MutagenFile", lambda path: None)
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"")
    lrc_path = tmp_path / "song.lrc"
    lrc_path.write_text("[00:02.00]Sidecar line\n", encoding="utf-8")

    entries, source = lyrics_module.load_lyrics_for_track(str(audio_path))

    assert entries == [(2.0, "Sidecar line")]
    assert source == "sidecar"


def test_load_lyrics_for_track_reports_none_when_nothing_found(tmp_path, monkeypatch):
    monkeypatch.setattr(lyrics_module, "MutagenFile", lambda path: None)
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"")

    entries, source = lyrics_module.load_lyrics_for_track(str(audio_path))

    assert entries == []
    assert source == "none"


# ---------------------------------------------------------------------------
# PlayerWindow._load_lrc_for_track -- cache + generation-guard dispatch
# ---------------------------------------------------------------------------

def _window_with_cache(cache, generation=None, set_calls=None, stale_calls=None):
    if generation is None:
        generation = window_module.NowPlayingGeneration()
        generation.begin("song.mp3")
    set_calls = set_calls if set_calls is not None else []
    stale_calls = stale_calls if stale_calls is not None else []
    return SimpleNamespace(
        _closing=False,
        _now_playing_generation=generation,
        _clear_synced_lyrics_state=lambda: None,
        _lyrics_cache=cache,
        _set_synced_lyrics=lambda entries: set_calls.append(entries),
        _record_stale_now_playing_detail=lambda *a, **kw: stale_calls.append(a),
        _log=lambda msg: None,
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None, path_details=lambda path: {}),
    )


def test_load_lrc_for_track_cache_hit_performs_no_io_and_no_worker(monkeypatch):
    def _fail(*a, **kw):
        raise AssertionError("cache hit must not construct a LyricsLoadWorker")
    monkeypatch.setattr(window_module, "LyricsLoadWorker", _fail)

    generation = window_module.NowPlayingGeneration()
    generation.begin("song.mp3")
    set_calls = []
    window = _window_with_cache(
        {"song.mp3": ([(1.0, "cached line")], "tags")},
        generation=generation, set_calls=set_calls,
    )

    PlayerWindow._load_lrc_for_track(window, "song.mp3", generation=generation.identity.generation)

    assert set_calls == [[(1.0, "cached line")]]


def test_load_lrc_for_track_drops_stale_worker_result(monkeypatch):
    class _FakeSignal:
        def __init__(self):
            self.slot = None
        def connect(self, slot):
            self.slot = slot

    class _FakeWorker:
        def __init__(self, path):
            self.lyrics_ready = _FakeSignal()
            self.finished = _FakeSignal()
        def start(self):
            pass

    monkeypatch.setattr(window_module, "LyricsLoadWorker", _FakeWorker)

    generation = window_module.NowPlayingGeneration()
    identity = generation.begin("song.mp3")
    requested_generation = identity.generation

    set_calls = []
    stale_calls = []
    window = _window_with_cache({}, generation=generation, set_calls=set_calls, stale_calls=stale_calls)
    window._lyrics_load_workers = []
    from billsmusic.worker_registry import WorkerLifetimeRegistry
    window._worker_registry = WorkerLifetimeRegistry()

    PlayerWindow._load_lrc_for_track(window, "song.mp3", generation=requested_generation)

    # User skips to a different track before the background read finishes.
    generation.begin("other.mp3")

    worker = window._lyrics_load_workers[0]
    worker.lyrics_ready.slot("song.mp3", [(1.0, "late line")], "tags")

    assert set_calls == []
    assert len(stale_calls) == 1
    # The result is still cached for a future activation of the same track.
    assert window._lyrics_cache["song.mp3"] == ([(1.0, "late line")], "tags")
