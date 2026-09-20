"""Phase B: stable runtime queue-entry tokens.

A queue row's position is not its identity. _mark_queue_row_played
relocates the row it commits, and the user can reorder, shuffle, remove or
undo at any moment while an async attempt is in flight. Duplicate
identical paths are legal, so a path is not identity either. Tokens give
each row an identity that survives every one of those, without the
row-object redesign (that is Phase D).

These tests pin the three properties the commit contract depends on:
alignment across every mutation, tokens travelling with their rows, and
identity never being invented to paper over a mismatch.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtCore, QtWidgets

from billsmusic.window import (
    QUEUE_ENTRY_TOKEN_ROLE,
    PlayerWindow,
    _allocate_queue_entry_tokens_for,
    _bootstrap_queue_entry_tokens,
    _prune_queue_entry_claims_for,
    _release_queue_entry_claim_for,
    _claim_queue_entry_token_for,
)

from test_queue_undo import QueueUndoHarness, _app, _entry


def _equip_for_commit(window):
    """QueueUndoHarness deliberately borrows only the queue-mutation
    methods; _mark_queue_row_played also needs the relocation + widget
    hooks, so bind the real relocation and stub the view side."""
    window._move_queue_row_to_bottom = (
        lambda row: PlayerWindow._move_queue_row_to_bottom(window, row)
    )
    window._remove_queue_row_widget = lambda row, reason=None: None
    window._insert_queue_row_widget = lambda row, reason=None: None
    window._animate_queue_history_move = lambda row: None
    window._schedule_session_save = lambda: None
    return window


def _tokens(window):
    return list(window._queue_entry_tokens)


def _assert_aligned(window):
    n = len(window.queue)
    assert len(window.queue_played) == n
    assert len(window.queue_playlist_entries) == n
    assert len(window._queue_entry_tokens) == n
    assert len(set(window._queue_entry_tokens)) == n, "tokens must be unique"


# -- allocation ------------------------------------------------------------

def test_tokens_are_never_reused_within_the_process():
    owner = type("O", (), {})()
    first = _allocate_queue_entry_tokens_for(owner, 3)
    second = _allocate_queue_entry_tokens_for(owner, 3)
    assert first == [1, 2, 3]
    assert second == [4, 5, 6]
    assert not set(first) & set(second)


def test_bootstrap_allocates_only_when_a_queue_has_never_had_tokens():
    owner = type("O", (), {})()
    owner.queue = ["a", "b"]
    _bootstrap_queue_entry_tokens(owner)
    allocated = list(owner._queue_entry_tokens)
    assert len(allocated) == 2

    # Second call is a no-op -- it must never re-issue identity.
    _bootstrap_queue_entry_tokens(owner)
    assert owner._queue_entry_tokens == allocated


def test_bootstrap_never_repairs_an_existing_misaligned_token_list():
    """Safeguard 1: once a queue has tokens, a length mismatch is identity
    corruption. Silently topping the list up would make the corruption
    look like valid state and let an async completion commit the wrong
    track, so bootstrap deliberately leaves it for the assertion."""
    owner = type("O", (), {})()
    owner.queue = ["a", "b", "c"]
    owner._queue_entry_tokens = [7]
    owner._next_queue_entry_token = 99

    _bootstrap_queue_entry_tokens(owner)

    assert owner._queue_entry_tokens == [7]  # untouched, not "healed"


def test_misalignment_raises_under_pytest_and_drops_every_claim():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"])
    window._ensure_queue_played_flags()
    window._queue_entry_claims = {t: 1 for t in window._queue_entry_tokens}
    window._queue_entry_tokens.pop()  # simulate a mutation that forgot tokens

    with pytest.raises(AssertionError):
        PlayerWindow._assert_queue_entry_tokens_aligned(window, "deliberate_break")

    # Fails closed: no claim survives a queue whose identity is untrusted.
    assert window._queue_entry_claims == {}


# -- alignment across every mutation ---------------------------------------

def test_remove_selected_keeps_lists_aligned_and_drops_only_that_token():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"])
    window._ensure_queue_played_flags()
    before = _tokens(window)
    window.queue_list.setCurrentRow(1)

    window._remove_selected_queue_item()

    _assert_aligned(window)
    assert _tokens(window) == [before[0], before[2]]


def test_move_selected_swaps_tokens_with_their_rows():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"])
    window._ensure_queue_played_flags()
    before = _tokens(window)
    window.queue_list.setCurrentRow(0)

    window._move_selected_queue_item(1)

    _assert_aligned(window)
    assert window.queue == ["b", "a", "c"]
    assert _tokens(window) == [before[1], before[0], before[2]]


def test_move_to_top_carries_its_token():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"])
    window._ensure_queue_played_flags()
    before = _tokens(window)

    window._move_queue_item_to_top(2)

    _assert_aligned(window)
    assert window.queue == ["c", "a", "b"]
    assert _tokens(window)[0] == before[2]


def test_shuffle_keeps_every_token_with_its_path():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c", "d", "e"])
    window._ensure_queue_played_flags()
    pairing = dict(zip(window.queue, _tokens(window)))

    window._shuffle_up_next()

    _assert_aligned(window)
    assert dict(zip(window.queue, _tokens(window))) == pairing


def test_remove_played_filters_tokens_in_parallel(monkeypatch):
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"], played=[False, True, False])
    window._ensure_queue_played_flags()
    before = _tokens(window)
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question",
        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes),
    )

    window._remove_played_queue_tracks()

    _assert_aligned(window)
    assert _tokens(window) == [before[0], before[2]]


def test_clear_queue_clears_tokens_and_claims():
    _app()
    window = QueueUndoHarness(queue=["a", "b"])
    window._ensure_queue_played_flags()
    window._queue_entry_claims = {t: 1 for t in _tokens(window)}

    window._clear_up_next_queue()

    _assert_aligned(window)
    assert window._queue_entry_tokens == []
    assert window._queue_entry_claims == {}


def test_keep_played_at_bottom_reorders_tokens_with_rows():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"], played=[True, False, True])
    window._ensure_queue_played_flags()
    pairing = dict(zip(window.queue, _tokens(window)))

    window._keep_played_tracks_at_bottom()

    _assert_aligned(window)
    assert dict(zip(window.queue, _tokens(window))) == pairing


def test_insert_unplayed_item_allocates_a_fresh_token():
    _app()
    window = QueueUndoHarness(queue=["a", "b"])
    window._ensure_queue_played_flags()
    before = set(_tokens(window))

    row = window._insert_unplayed_queue_item("new.mp3")

    _assert_aligned(window)
    assert _tokens(window)[row] not in before


def test_playlist_load_allocates_fresh_tokens_for_new_rows_only():
    _app()
    window = QueueUndoHarness(queue=["a", "b"], played=[False, True])
    window._ensure_queue_played_flags()
    before = _tokens(window)

    window._playlist_loaded("list.m3u", [_entry("p1"), _entry("p2")],
                            {"total": 2, "missing": 0}, 5.0)

    _assert_aligned(window)
    assert window.queue == ["a", "p1", "p2", "b"]
    # The pre-existing rows keep their identity.
    assert _tokens(window)[0] == before[0]
    assert _tokens(window)[3] == before[1]


def test_undo_restores_tokens_with_their_rows():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"])
    window._ensure_queue_played_flags()
    before = _tokens(window)
    window.queue_list.setCurrentRow(1)
    window._remove_selected_queue_item()

    window._undo_queue_change()

    _assert_aligned(window)
    # Identity is RESTORED, not re-invented: the row that came back is the
    # same logical entry, so an in-flight attempt still resolves to it.
    assert _tokens(window) == before


def test_commit_relocation_carries_the_token_to_the_bottom():
    _app()
    window = _equip_for_commit(QueueUndoHarness(queue=["a", "b", "c"]))
    window._ensure_queue_played_flags()
    token = _tokens(window)[0]

    moved = PlayerWindow._mark_queue_row_played(window, 0)

    _assert_aligned(window)
    assert _tokens(window)[moved] == token


# -- Safeguard 2: identity lives on the widget item ------------------------

def test_gui_reorder_of_two_identical_paths_preserves_each_original_token():
    """The case a path/title/position match cannot possibly get right.

    Two rows with the SAME path and different tokens. Qt physically
    reorders queue_list, then _sync_queue_from_list rebuilds self.queue
    from widget order. The token must come off each item, so each row
    keeps the identity it started with.
    """
    _app()
    window = QueueUndoHarness(queue=["same.mp3", "other.mp3", "same.mp3"])
    window._ensure_queue_played_flags()
    first_token, middle_token, last_token = _tokens(window)

    # Rebuild the view with the tokens attached, as the real row builders do.
    window.queue_list.clear()
    for row, path in enumerate(window.queue):
        item = QtWidgets.QListWidgetItem()
        item.setData(QtCore.Qt.ItemDataRole.UserRole, path)
        item.setData(window._queue_played_role(), window.queue_played[row])
        item.setData(window._queue_playlist_role(), None)
        item.setData(QUEUE_ENTRY_TOKEN_ROLE, window._queue_entry_tokens[row])
        window.queue_list.addItem(item)

    # Drag the LAST "same.mp3" to the top -- indistinguishable from the
    # first by path, title or any displayed value.
    moved = window.queue_list.takeItem(2)
    window.queue_list.insertItem(0, moved)

    window._sync_queue_from_list()

    _assert_aligned(window)
    assert window.queue == ["same.mp3", "same.mp3", "other.mp3"]
    # The row now at the top is the one that WAS at the bottom.
    assert _tokens(window)[0] == last_token
    assert _tokens(window)[1] == first_token
    assert _tokens(window)[2] == middle_token


def test_sync_gives_a_fresh_token_to_an_item_that_has_none():
    _app()
    window = QueueUndoHarness(queue=["a"])
    window._ensure_queue_played_flags()
    existing = _tokens(window)[0]
    window.queue_list.clear()
    for path in ("a", "b"):
        item = QtWidgets.QListWidgetItem()
        item.setData(QtCore.Qt.ItemDataRole.UserRole, path)
        item.setData(window._queue_played_role(), False)
        item.setData(window._queue_playlist_role(), None)
        if path == "a":
            item.setData(QUEUE_ENTRY_TOKEN_ROLE, existing)
        window.queue_list.addItem(item)

    window._sync_queue_from_list()

    _assert_aligned(window)
    # "a" keeps its identity; "b" gets its own rather than borrowing one.
    assert _tokens(window)[0] == existing
    assert _tokens(window)[1] != existing


# -- claims ----------------------------------------------------------------

def test_pruning_drops_claims_only_for_tokens_that_left_the_queue():
    owner = type("O", (), {})()
    owner._queue_entry_tokens = [1, 3]
    owner._queue_entry_claims = {1: 10, 2: 11, 3: 12}

    _prune_queue_entry_claims_for(owner, "test")

    # Only tokens that left the queue are dropped, and surviving claims
    # keep their owners.
    assert owner._queue_entry_claims == {1: 10, 3: 12}


def test_reorder_preserves_claims_for_surviving_tokens():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"])
    window._ensure_queue_played_flags()
    claimed = _tokens(window)[2]
    window._queue_entry_claims = {claimed: 1}

    window._shuffle_up_next()

    assert window._queue_entry_claims == {claimed: 1}


def test_removing_a_claimed_row_drops_its_claim_without_leaking():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"])
    window._ensure_queue_played_flags()
    claimed = _tokens(window)[1]
    window._queue_entry_claims = {claimed: 1}
    window.queue_list.setCurrentRow(1)

    window._remove_selected_queue_item()

    assert claimed not in window._queue_entry_claims


def test_next_unplayed_skips_a_claimed_row():
    _app()
    window = QueueUndoHarness(queue=["a", "b", "c"])
    window._ensure_queue_played_flags()
    window._queue_entry_claims = {_tokens(window)[0]: 1}

    assert PlayerWindow._next_unplayed_queue_row(window) == 1


def test_next_unplayed_returns_none_when_every_row_is_played_or_claimed():
    _app()
    window = QueueUndoHarness(queue=["a", "b"], played=[True, False])
    window._ensure_queue_played_flags()
    window._queue_entry_claims = {_tokens(window)[1]: 1}

    assert PlayerWindow._next_unplayed_queue_row(window) is None


def test_duplicate_identical_paths_get_distinct_tokens():
    _app()
    window = QueueUndoHarness(queue=["same.mp3", "same.mp3", "same.mp3"])
    window._ensure_queue_played_flags()

    _assert_aligned(window)
    assert len(set(_tokens(window))) == 3
