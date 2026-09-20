"""Phase D: missing-track repair locates its target by queue token.

_repair_missing_playlist_tracks captures the rows to repair, then opens a
modal dialog. dialog.exec() spins a NESTED event loop, which delivers
queued worker completions -- _playlist_loaded, _finish_queue_drop,
_on_scan_finished, the metadata backfill -- several of which insert or
reorder queue rows. A row captured before the dialog can therefore mean a
different entry by the time it returns, and the old code wrote the
replacement straight into that row.

The contract these tests pin:

  the TOKEN locates the logical queue entry -- never a path lookup,
  because duplicate identical paths are legal;

  the CAPTURED ENTRY OBJECT proves the located row still holds the
  content this repair was chosen for. Same token with different content
  means the entry was replaced while the dialog was open, and the newer
  state wins.
"""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from billsmusic.playlist_repair import PlaylistEntry
from billsmusic.window import (
    PlayerWindow,
    _queue_row_for_token_of,
    _queue_token_for_row_of,
)


class _Diagnostics:
    def __init__(self):
        self.events = []

    def record(self, category, operation, **kw):
        self.events.append((category, operation, kw.get("details") or {}))


def _missing(path):
    return PlaylistEntry(path=path, display_title=os.path.basename(path), is_missing=True)


def _window(paths, missing_rows=()):
    entries = [
        _missing(p) if row in missing_rows else None
        for row, p in enumerate(paths)
    ]
    w = SimpleNamespace(
        queue=list(paths),
        queue_played=[False] * len(paths),
        queue_playlist_entries=entries,
        _queue_mutation_epoch=0,
        _queue_entry_claims={},
        _next_queue_entry_token=1,
        diagnostics=_Diagnostics(),
    )
    w._ensure_queue_played_flags = lambda: PlayerWindow._ensure_queue_played_flags(w)
    w._ensure_queue_played_flags()
    return w


def _apply(window, replacements):
    """The post-dialog write-back, exactly as _repair_missing_playlist_tracks
    performs it: locate by token, validate by captured entry."""
    repaired_rows = []
    for token, (captured_entry, replacement) in replacements.items():
        queue_row = _queue_row_for_token_of(window, token)
        if queue_row is None:
            continue
        if not (0 <= queue_row < len(window.queue_playlist_entries)):
            continue
        entry = window.queue_playlist_entries[queue_row]
        if entry is None or not entry.is_missing:
            continue
        if entry is not captured_entry:
            window.diagnostics.record(
                "playlist", "repair_skipped_stale_entry", details={"token": token},
            )
            continue
        repaired = entry.with_replacement(replacement)
        window.queue_playlist_entries[queue_row] = repaired
        window.queue[queue_row] = repaired.resolved_path
        repaired_rows.append(queue_row)
    return repaired_rows


def _insert_row(window, at, path):
    window.queue.insert(at, path)
    window.queue_played.insert(at, False)
    window.queue_playlist_entries.insert(at, None)
    window._queue_entry_tokens.insert(at, window._next_queue_entry_token)
    window._next_queue_entry_token += 1


def _remove_row(window, row):
    for lst in (window.queue, window.queue_played,
                window.queue_playlist_entries, window._queue_entry_tokens):
        lst.pop(row)


def test_unrelated_insertion_before_the_target_still_repairs_the_right_entry(tmp_path):
    replacement = tmp_path / "found.mp3"
    replacement.write_bytes(b"x")
    w = _window(["a.mp3", "missing.mp3"], missing_rows={1})
    token = _queue_token_for_row_of(w, 1)
    captured = w.queue_playlist_entries[1]
    replacements = {token: (captured, str(replacement))}

    # A playlist load lands during the dialog's nested event loop.
    _insert_row(w, 0, "inserted.mp3")
    assert w.queue.index("missing.mp3") == 2  # the captured row 1 is now wrong

    rows = _apply(w, replacements)

    assert rows == [2]
    assert w.queue[2] == str(replacement)
    assert w.queue[0] == "inserted.mp3"  # untouched


def test_reorder_during_the_dialog_still_repairs_the_right_entry(tmp_path):
    replacement = tmp_path / "found.mp3"
    replacement.write_bytes(b"x")
    w = _window(["missing.mp3", "b.mp3", "c.mp3"], missing_rows={0})
    token = _queue_token_for_row_of(w, 0)
    captured = w.queue_playlist_entries[0]
    replacements = {token: (captured, str(replacement))}

    # Drag it to the bottom while the dialog is open.
    for lst in (w.queue, w.queue_played, w.queue_playlist_entries, w._queue_entry_tokens):
        lst.append(lst.pop(0))

    rows = _apply(w, replacements)

    assert rows == [2]
    assert w.queue[2] == str(replacement)
    assert w.queue[0] == "b.mp3"


def test_target_removed_during_the_dialog_is_skipped(tmp_path):
    replacement = tmp_path / "found.mp3"
    replacement.write_bytes(b"x")
    w = _window(["a.mp3", "missing.mp3"], missing_rows={1})
    token = _queue_token_for_row_of(w, 1)
    captured = w.queue_playlist_entries[1]
    replacements = {token: (captured, str(replacement))}

    _remove_row(w, 1)

    assert _apply(w, replacements) == []
    assert w.queue == ["a.mp3"]
    assert _queue_row_for_token_of(w, token) is None


def test_entry_already_replaced_during_the_dialog_preserves_the_newer_state(tmp_path):
    """Same token, different content: something else repaired or replaced
    this entry while the dialog was open. The stale repair must not
    overwrite it."""
    stale = tmp_path / "stale.mp3"
    stale.write_bytes(b"x")
    newer = tmp_path / "newer.mp3"
    newer.write_bytes(b"x")
    w = _window(["a.mp3", "missing.mp3"], missing_rows={1})
    token = _queue_token_for_row_of(w, 1)
    captured = w.queue_playlist_entries[1]
    replacements = {token: (captured, str(stale))}

    # The row keeps its token (repair is an in-place edit of the same
    # logical entry) but now holds a DIFFERENT entry object.
    w.queue_playlist_entries[1] = _missing("replaced.mp3")
    w.queue[1] = "replaced.mp3"

    assert _apply(w, replacements) == []
    assert w.queue[1] == "replaced.mp3"  # newer state preserved
    assert any(op == "repair_skipped_stale_entry" for _, op, _ in w.diagnostics.events)


def test_duplicate_identical_paths_repair_only_the_captured_token(tmp_path):
    """Both rows are missing and hold the SAME path. A path lookup could
    not tell them apart; the token can."""
    replacement = tmp_path / "found.mp3"
    replacement.write_bytes(b"x")
    w = _window(["same.mp3", "other.mp3", "same.mp3"], missing_rows={0, 2})
    target_token = _queue_token_for_row_of(w, 2)
    captured = w.queue_playlist_entries[2]
    replacements = {target_token: (captured, str(replacement))}

    rows = _apply(w, replacements)

    assert rows == [2]
    assert w.queue[2] == str(replacement)
    assert w.queue[0] == "same.mp3"  # the other identical row untouched
    assert w.queue_playlist_entries[0].is_missing is True


def test_duplicate_paths_survive_a_reorder_and_still_repair_only_the_captured_one(tmp_path):
    replacement = tmp_path / "found.mp3"
    replacement.write_bytes(b"x")
    w = _window(["same.mp3", "same.mp3"], missing_rows={0, 1})
    target_token = _queue_token_for_row_of(w, 1)
    captured = w.queue_playlist_entries[1]
    other_entry = w.queue_playlist_entries[0]
    replacements = {target_token: (captured, str(replacement))}

    for lst in (w.queue, w.queue_played, w.queue_playlist_entries, w._queue_entry_tokens):
        lst.insert(0, lst.pop(1))

    rows = _apply(w, replacements)

    assert rows == [0]
    assert w.queue_playlist_entries[1] is other_entry
    assert w.queue_playlist_entries[1].is_missing is True
