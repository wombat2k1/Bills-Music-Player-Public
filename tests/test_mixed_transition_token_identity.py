"""Phase D: mixed transitions carry a queue token, not a retained row.

_mixed_transition_incoming_row used to be kept for the whole transition
and re-read after the commit had relocated its row. It is gone: the
transition stores the token and the expected incoming source, and a row is
resolved only at the moment a position is genuinely needed.

Two properties, and they pull in opposite directions -- which is why both
are needed:

  A reorder or insertion moves the row but not the CONTENT, so the
  transition must survive it. Identity is never located by path.

  Missing-track repair deliberately preserves an entry's token while
  replacing its path. A surviving token therefore does NOT prove the
  prepared media still belongs to that entry, so content is validated
  before activation. Prepared A must not activate for an entry now
  holding B.
"""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from billsmusic.window import (
    PlayerWindow,
    _queue_entry_still_holds_source,
    _queue_row_for_token_of,
    _queue_token_for_row_of,
)


def _window(paths):
    w = SimpleNamespace(
        queue=list(paths),
        queue_played=[False] * len(paths),
        queue_playlist_entries=[None] * len(paths),
        _queue_mutation_epoch=0,
        _queue_entry_claims={},
        _next_queue_entry_token=1,
    )
    w._ensure_queue_played_flags = lambda: PlayerWindow._ensure_queue_played_flags(w)
    w._ensure_queue_played_flags()
    return w


# -- content validity ------------------------------------------------------

def test_unchanged_entry_still_holds_its_source():
    w = _window(["a.mp3", "incoming.mp4"])
    token = _queue_token_for_row_of(w, 1)
    assert _queue_entry_still_holds_source(w, token, "incoming.mp4")


def test_insertion_before_the_target_does_not_invalidate_it():
    """An unrelated queue edit moves the row but not the content."""
    w = _window(["a.mp3", "incoming.mp4"])
    token = _queue_token_for_row_of(w, 1)

    for lst in (w.queue, w.queue_played, w.queue_playlist_entries, w._queue_entry_tokens):
        lst.insert(0, "x" if lst is w.queue else (False if lst is w.queue_played else None))
    w._queue_entry_tokens[0] = 999

    assert _queue_row_for_token_of(w, token) == 2
    assert _queue_entry_still_holds_source(w, token, "incoming.mp4")


def test_reorder_does_not_invalidate_it():
    w = _window(["a.mp3", "incoming.mp4", "c.mp3"])
    token = _queue_token_for_row_of(w, 1)

    for lst in (w.queue, w.queue_played, w.queue_playlist_entries, w._queue_entry_tokens):
        lst.insert(0, lst.pop(1))

    assert _queue_row_for_token_of(w, token) == 0
    assert _queue_entry_still_holds_source(w, token, "incoming.mp4")


def test_in_place_path_change_under_the_same_token_INVALIDATES_it():
    """The hostile case: missing-track repair keeps token T and swaps its
    path from A to B while A is being prepared. Prepared A must not
    activate for an entry that now holds B."""
    w = _window(["a.mp3", "A.mp4"])
    token = _queue_token_for_row_of(w, 1)

    w.queue[1] = "B.mp4"  # repaired in place; token deliberately unchanged

    assert _queue_row_for_token_of(w, token) == 1   # token survives
    assert not _queue_entry_still_holds_source(w, token, "A.mp4")  # content does not
    assert _queue_entry_still_holds_source(w, token, "B.mp4")


def test_departed_token_is_not_valid():
    w = _window(["a.mp3", "incoming.mp4"])
    token = _queue_token_for_row_of(w, 1)
    for lst in (w.queue, w.queue_played, w.queue_playlist_entries, w._queue_entry_tokens):
        lst.pop(1)

    assert _queue_row_for_token_of(w, token) is None
    assert not _queue_entry_still_holds_source(w, token, "incoming.mp4")


def test_duplicate_identical_paths_validate_per_token_not_per_path():
    """Both rows hold the same path; each token validates against its own
    row, and a path lookup could not distinguish them."""
    w = _window(["same.mp4", "other.mp3", "same.mp4"])
    first, _, last = list(w._queue_entry_tokens)

    assert _queue_entry_still_holds_source(w, first, "same.mp4")
    assert _queue_entry_still_holds_source(w, last, "same.mp4")

    w.queue[2] = "changed.mp4"  # only the LAST row is repaired

    assert _queue_entry_still_holds_source(w, first, "same.mp4")
    assert not _queue_entry_still_holds_source(w, last, "same.mp4")


def test_comparison_uses_the_projects_canonical_normalisation():
    """Reuses queue_dedup.normalize_path_for_comparison -- slash direction
    normalised, and case-insensitive on Windows -- rather than any ad-hoc
    rule of its own."""
    w = _window(["a.mp3", os.path.join("dir", "sub", "clip.mp4")])
    token = _queue_token_for_row_of(w, 1)

    assert _queue_entry_still_holds_source(w, token, "dir/sub/clip.mp4")
    assert _queue_entry_still_holds_source(w, token, "dir//sub//clip.mp4")
    assert not _queue_entry_still_holds_source(w, token, "dir/sub/other.mp4")


def test_empty_or_missing_source_is_never_valid():
    w = _window(["a.mp3"])
    token = _queue_token_for_row_of(w, 0)
    assert not _queue_entry_still_holds_source(w, token, None)
    assert not _queue_entry_still_holds_source(w, token, "")
    assert not _queue_entry_still_holds_source(w, None, "a.mp3")


def test_no_retained_row_field_remains_on_the_transition():
    import billsmusic.window as window_module
    source = __import__("inspect").getsource(window_module)
    assert "_mixed_transition_incoming_row" not in source
