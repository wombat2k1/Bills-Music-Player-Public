"""Phase D: library index and queue token are separate, explicit identities.

`current_index` used to be one field written by _activate_track_ui with
whatever its caller passed -- a LIBRARY index from ten call sites, a QUEUE
ROW from the two mixed-media ones -- and read back as a library index
(`self.tracks[self.current_index]`). Two things follow, and both are
pinned here:

  They are not alternatives. A queue-played local-library track has a
  library index AND a queue token at the same time.

  A library index is POSITIONAL and perishable. A rescan rebuilds
  self.tracks, so the number must be recomputed from current_path -- the
  durable library-side identity -- and never carried across the rebuild.

The queue token is the durable queue-side identity: it may outlive its
entry, and _queue_row_for_token() returning None is the authoritative
"gone" signal. It is never rediscovered from path.
"""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from billsmusic.media_type import MediaType
from billsmusic.now_playing import NowPlayingGeneration, NowPlayingIdentity
from billsmusic.window import PlayerWindow, _queue_token_for_row_of


class _Diagnostics:
    def record(self, *a, **kw):
        pass

    def path_details(self, path):
        return {"path": path}


def _window(tracks=None, queue=None):
    tracks = list(tracks or [])
    queue = list(queue or [])
    w = SimpleNamespace(
        tracks=tracks,
        track_index_by_path={p: i for i, p in enumerate(tracks)},
        queue=queue,
        queue_played=[False] * len(queue),
        queue_playlist_entries=[None] * len(queue),
        _queue_mutation_epoch=0,
        _queue_entry_claims={},
        _next_queue_entry_token=1,
        current_path=None,
        current_library_index=None,
        current_queue_token=None,
        scrubbing=False,
        quiet_count=0,
        _last_quiet_debug_remaining=None,
        _current_media_type=MediaType.AUDIO,
        _now_playing_generation=NowPlayingGeneration(),
        viz_logger=SimpleNamespace(active=False, _track=None, stop=lambda: None),
        beat=SimpleNamespace(setPlaying=lambda v: None),
        diagnostics=_Diagnostics(),
    )
    # Everything _activate_track_ui calls downstream of setting identity --
    # stubbed, because these tests are about WHICH identity it records.
    for name in (
        "_sync_now_playing_overlay_for_media_type", "_reset_analyzer_clock",
        "_set_playing_button_state", "_schedule_session_save",
        "_reset_recently_played_tracking", "_select_tree_item",
        "_display_track_tags", "_load_lrc_for_track", "_queue_track_tags_async",
        "_record_recent_played", "_update_dj_info", "_log",
    ):
        setattr(w, name, lambda *a, **kw: None)
    for name in (
        "_clear_synced_lyrics_state", "_start_jukebox_intro", "_sync_party_mode",
        "_normalize_artist_for_bio", "_normalize_track_for_bio",
    ):
        setattr(w, name, lambda *a, **kw: None)
    w._load_cached_audio_tags = lambda path: SimpleNamespace(title=path, artist="")
    w._load_tags = lambda *a, **kw: SimpleNamespace(title="", artist="")
    w._lyrics = []
    w._karaoke_document = None
    w._bio_requested_artist = None
    w._bio_requested_generation = 0
    w._now_playing_lyric_generation = 0
    w.analyzer = None
    w.analyzer_worker = None
    w.bio_worker = None
    w.overlay = None
    w.waveform_seekbar = None
    w.waveform_worker = None
    w._ensure_queue_played_flags = lambda: PlayerWindow._ensure_queue_played_flags(w)
    w._ensure_queue_played_flags()
    return w


def _activate(window, path, **kw):
    return PlayerWindow._activate_track_ui(window, path, **kw)


# -- the two identities are independent ------------------------------------

def test_direct_library_playback_has_an_index_and_no_queue_token():
    w = _window(tracks=["a.mp3", "b.mp3"])
    _activate(w, "b.mp3", library_index=1)
    assert w.current_library_index == 1
    assert w.current_queue_token is None


def test_queue_playback_of_a_library_track_has_BOTH():
    """The case a single overloaded field could never express."""
    w = _window(tracks=["a.mp3", "b.mp3"], queue=["b.mp3"])
    token = _queue_token_for_row_of(w, 0)
    _activate(w, "b.mp3", library_index=1, queue_token=token)
    assert w.current_library_index == 1
    assert w.current_queue_token == token


def test_queue_only_file_has_a_token_and_no_library_index():
    w = _window(tracks=["a.mp3"], queue=["elsewhere.mp3"])
    token = _queue_token_for_row_of(w, 0)
    _activate(w, "elsewhere.mp3", library_index=None, queue_token=token)
    assert w.current_library_index is None
    assert w.current_queue_token == token


def test_plex_queue_item_may_have_no_library_index():
    w = _window(tracks=["a.mp3"], queue=["plex://server/1234"])
    token = _queue_token_for_row_of(w, 0)
    _activate(w, "plex://server/1234", queue_token=token)
    assert w.current_library_index is None
    assert w.current_queue_token == token


def test_a_negative_or_non_integer_library_index_is_normalised_to_none():
    w = _window(tracks=["a.mp3"])
    _activate(w, "a.mp3", library_index=-1)
    assert w.current_library_index is None
    _activate(w, "a.mp3", library_index="0")
    assert w.current_library_index is None


def test_activate_track_ui_rejects_a_positional_index():
    """The old signature took (index, path). Passing an index positionally
    must now be a hard error rather than silently meaning `path`."""
    w = _window(tracks=["a.mp3"])
    try:
        PlayerWindow._activate_track_ui(w, 0, "a.mp3")
    except TypeError:
        pass
    else:
        raise AssertionError("positional index must not be accepted")


# -- a library index is positional and perishable --------------------------

def _scan_finished_window(tracks, current_path):
    w = _window(tracks=tracks)
    w.current_path = current_path
    labels = []
    w.now_playing = SimpleNamespace(setText=lambda text: labels.append(text))
    w.labels = labels
    w._display_meta_for_path = lambda path: {"title": f"TITLE:{path}"}
    return w


def _rebuild_library(window, new_tracks):
    """The part of _on_scan_finished Phase D governs: the library list has
    just been rebuilt, so recompute from current_path and never reuse the
    stored number."""
    window.tracks = list(new_tracks)
    window.track_index_by_path = {p: i for i, p in enumerate(new_tracks)}
    if window.current_path:
        window.current_library_index = window.track_index_by_path.get(window.current_path)
    else:
        window.current_library_index = None
    if not window.current_path:
        window.now_playing.setText("Ready")
    else:
        meta = window._display_meta_for_path(window.current_path)
        window.now_playing.setText(
            meta.get("title") or os.path.splitext(os.path.basename(window.current_path))[0]
        )


def test_rescan_reordering_never_reuses_the_old_numeric_index():
    """The currently playing track was at library index 2. The rescan
    reorders the library so index 2 is now a DIFFERENT file. Reusing the
    stored number would display that other file."""
    w = _scan_finished_window(["a.mp3", "b.mp3", "target.mp3"], "target.mp3")
    w.current_library_index = 2
    assert w.tracks[2] == "target.mp3"

    _rebuild_library(w, ["target.mp3", "a.mp3", "b.mp3", "new.mp3"])

    # Recomputed from current_path, not carried across.
    assert w.current_library_index == 0
    assert w.tracks[2] == "b.mp3"          # the old number now means another file
    assert w.labels[-1] == "TITLE:target.mp3"


def test_rescan_clears_the_index_when_the_current_track_left_the_library():
    w = _scan_finished_window(["a.mp3", "gone.mp3"], "gone.mp3")
    w.current_library_index = 1

    _rebuild_library(w, ["a.mp3", "c.mp3", "d.mp3"])

    assert w.current_library_index is None
    # Still names the track that is actually playing -- never an unrelated
    # library row picked by a stale number.
    assert w.labels[-1] == "TITLE:gone.mp3"


def test_rescan_with_nothing_playing_shows_ready():
    w = _scan_finished_window(["a.mp3"], None)
    _rebuild_library(w, ["a.mp3", "b.mp3"])
    assert w.current_library_index is None
    assert w.labels[-1] == "Ready"


# -- queue token lifecycle -------------------------------------------------

def test_current_queue_token_survives_its_entry_departing_and_resolves_to_none():
    """Contract: the token is NOT cleared at removal time. Because tokens
    are never reused, a departed token can only ever resolve to None --
    never to a different row -- so tolerating None is the whole contract."""
    w = _window(tracks=[], queue=["a.mp3", "b.mp3"])
    token = _queue_token_for_row_of(w, 1)
    _activate(w, "b.mp3", queue_token=token)

    for lst in (w.queue, w.queue_played, w.queue_playlist_entries, w._queue_entry_tokens):
        lst.pop(1)

    assert w.current_queue_token == token          # still stored
    assert PlayerWindow._queue_row_for_token(w, token) is None  # authoritative "gone"


def test_a_departed_token_never_resolves_to_a_reused_row():
    w = _window(tracks=[], queue=["a.mp3", "b.mp3"])
    departed = _queue_token_for_row_of(w, 0)
    for lst in (w.queue, w.queue_played, w.queue_playlist_entries, w._queue_entry_tokens):
        lst.pop(0)
    w.queue.append("c.mp3")
    w.queue_played.append(False)
    w.queue_playlist_entries.append(None)
    w._queue_entry_tokens.append(99)

    assert PlayerWindow._queue_row_for_token(w, departed) is None


def test_duplicate_identical_paths_cannot_confuse_current_queue_identity():
    """Two rows, same path, different tokens. Whichever is activated, the
    stored identity is that exact row -- and a path lookup could not tell
    them apart."""
    w = _window(tracks=[], queue=["same.mp3", "other.mp3", "same.mp3"])
    first, _, last = list(w._queue_entry_tokens)

    _activate(w, "same.mp3", queue_token=last)

    assert w.current_queue_token == last
    assert w.current_queue_token != first
    assert PlayerWindow._queue_row_for_token(w, last) == 2
    assert PlayerWindow._queue_row_for_token(w, first) == 0


def test_queue_identity_follows_its_row_through_a_reorder():
    w = _window(tracks=[], queue=["a.mp3", "b.mp3", "c.mp3"])
    token = _queue_token_for_row_of(w, 2)
    _activate(w, "c.mp3", queue_token=token)

    for lst in (w.queue, w.queue_played, w.queue_playlist_entries, w._queue_entry_tokens):
        lst.insert(0, lst.pop(2))

    assert PlayerWindow._queue_row_for_token(w, w.current_queue_token) == 0
    assert w.queue[0] == "c.mp3"


# -- NowPlayingGeneration carries no positional index ----------------------

def test_now_playing_identity_has_no_index_field():
    assert set(NowPlayingIdentity.__dataclass_fields__) == {"generation", "path"}


def test_now_playing_generation_still_keys_on_generation_and_path():
    guard = NowPlayingGeneration()
    first = guard.begin("A.mp3")
    second = guard.begin("B.mp3")
    assert second.generation == first.generation + 1
    assert guard.is_current(second.generation, "B.mp3")
    assert not guard.is_current(first.generation, "A.mp3")
