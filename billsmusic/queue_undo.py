"""Single-level undo for Up Next queue changes.

Deliberately minimal: one snapshot slot, no history stack, no redo. A new
snapshot always replaces whatever was there before -- capturing it is cheap
(a few list copies, no file reads, no widget/backend references) so there is
no reason to bound or age it out beyond "only one at a time".
"""
from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from typing import List, Optional

from .playlist_repair import PlaylistEntry

UNDO_ACTION_LABELS = {
    "remove": "Undo Remove Tracks",
    "remove_played": "Undo Remove Played Tracks",
    "clear": "Undo Clear Up Next",
    "shuffle": "Undo Shuffle",
    "reorder": "Undo Reorder",
    "playlist_load": "Undo Add Playlist",
    "add": "Undo Add to Up Next",
}

UNDO_STATUS_MESSAGES = {
    "remove": "Queue removal undone",
    "remove_played": "Removed tracks restored",
    "clear": "Up Next restored",
    "shuffle": "Shuffle undone",
    "reorder": "Previous queue order restored",
    "playlist_load": "Playlist addition undone",
    "add": "Up Next addition undone",
}

GENERIC_UNDO_LABEL = "Undo Queue Change"


@dataclass
class QueueUndoSnapshot:
    action: str
    queue: List[str]
    queue_played: List[bool]
    queue_playlist_entries: List[Optional[PlaylistEntry]]
    selected_rows: List[int]
    scroll_position: Optional[int]
    current_path: Optional[str]
    # Phase B: the stable runtime token for each row. Undo must RESTORE
    # identity, not invent it -- an in-flight attempt that selected a row
    # before the undone mutation must still resolve to that same entry
    # afterwards, and its claim must survive. Last, with a default, so
    # existing positional construction stays valid.
    queue_entry_tokens: List[int] = field(default_factory=list)


def snapshot_queue_state(window, action: str) -> QueueUndoSnapshot:
    """Pure capture: lightweight list copies plus selection/scroll state.

    No music-file reads, no backend/widget objects retained -- only plain
    lists, ints and the (immutable) PlaylistEntry references already held by
    the queue.
    """
    selected_rows = sorted({
        window.queue_list.row(item) for item in window.queue_list.selectedItems()
    })
    scrollbar = window.queue_list.verticalScrollBar()
    return QueueUndoSnapshot(
        action=action,
        queue=list(window.queue),
        queue_played=list(window.queue_played),
        queue_playlist_entries=list(window.queue_playlist_entries),
        queue_entry_tokens=list(getattr(window, "_queue_entry_tokens", [])),
        selected_rows=selected_rows,
        scroll_position=scrollbar.value() if scrollbar is not None else None,
        current_path=window.current_path,
    )


def record_undo_snapshot_diagnostics(window, action: str, started: float, previous) -> None:
    duration_ms = (time.perf_counter() - started) * 1000.0
    window.diagnostics.record(
        "queue", "undo_snapshot",
        duration_ms=duration_ms,
        details={
            "action": action,
            "queue_size_before": len(previous.queue) if previous is not None else None,
            "queue_size_after": len(window.queue),
        },
        minimum_level="basic",
    )


@contextlib.contextmanager
def capture_queue_undo(window, action: str):
    """Wrap one queue mutation: snapshot before, commit after success, roll
    back to the previous snapshot if the mutation raises -- a failed
    operation must never leave a broken or misleading undo state."""
    previous = window._queue_undo_snapshot
    started = time.perf_counter()
    window._queue_undo_snapshot = snapshot_queue_state(window, action)
    try:
        yield
    except Exception:
        window._queue_undo_snapshot = previous
        raise
    else:
        record_undo_snapshot_diagnostics(window, action, started, previous)
        window._update_undo_action_state()
        # Every action wrapped here restructures the queue (remove/reorder/
        # shuffle/clear) -- bump the epoch a pending Phase 2A dual-transition
        # preload uses to notice its target row no longer means what it did.
        # See window.py's _queue_mutation_epoch docstring comment.
        window._queue_mutation_epoch = getattr(window, "_queue_mutation_epoch", 0) + 1
