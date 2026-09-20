"""Queue row identity across a commit that RELOCATES the row.

_mark_queue_row_played does not merely set a flag -- it calls
_move_queue_row_to_bottom, which rewrites self.queue, self.queue_played
and self.queue_playlist_entries and bumps _queue_mutation_epoch. Every row
number captured before that call is therefore stale afterwards: it now
refers to whatever track shuffled up into that position.

Both mixed-media transition directions captured a row at *request* time
and then re-read it after the commit had relocated it --
_finish_mixed_transition_video_to_audio passed it to _activate_track_ui as
current_index, so the Now Playing index pointed at the wrong track. These
tests pin the relocation contract and the re-pinning that fixes it, with
fakes that genuinely relocate rather than only setting the flag (the
existing mixed-media suite's fake deliberately does not, which is why it
could never observe this).
"""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from billsmusic.window import PlayerWindow


def _queue_window(paths, played=None):
    """Fake carrying real queue lists and the real relocation machinery."""
    window = SimpleNamespace(
        queue=list(paths),
        queue_played=list(played if played is not None else [False] * len(paths)),
        queue_playlist_entries=[None] * len(paths),
        _queue_mutation_epoch=0,
        removed=[],
        inserted=[],
        saves=[],
    )
    window._ensure_queue_played_flags = lambda: None
    window._remove_queue_row_widget = lambda row, reason=None: window.removed.append(row)
    window._insert_queue_row_widget = lambda row, reason=None: window.inserted.append(row)
    window._animate_queue_history_move = lambda row: None
    window._schedule_session_save = lambda: window.saves.append(True)
    window._move_queue_row_to_bottom = lambda row: PlayerWindow._move_queue_row_to_bottom(window, row)
    return window


def test_mark_queue_row_played_returns_the_post_relocation_row():
    window = _queue_window(["A.mp3", "B.mp4", "C.flac"])

    moved = PlayerWindow._mark_queue_row_played(window, 0)

    # A moved to the bottom; the returned index must be where it landed,
    # not where it was asked for.
    assert moved == 2
    assert window.queue == ["B.mp4", "C.flac", "A.mp3"]
    assert window.queue_played == [False, False, True]


def test_the_row_number_passed_in_is_stale_immediately_afterwards():
    """The whole reason the return value exists: row 0 no longer means
    the track the caller marked."""
    window = _queue_window(["A.mp3", "B.mp4", "C.flac"])

    requested_row = 0
    requested_path = window.queue[requested_row]
    moved = PlayerWindow._mark_queue_row_played(window, requested_row)

    assert window.queue[requested_row] != requested_path  # stale -- now B.mp4
    assert window.queue[moved] == requested_path          # live


def test_mark_queue_row_played_returns_none_and_changes_nothing_when_out_of_range():
    # A no-op mark must never be mistaken for a relocation -- callers
    # re-pin only on a non-None result, so returning None here is what
    # stops an out-of-range mark clobbering a still-valid captured row.
    window = _queue_window(["A.mp3", "B.mp4"])

    assert PlayerWindow._mark_queue_row_played(window, 7) is None
    assert window.queue == ["A.mp3", "B.mp4"]
    assert window.queue_played == [False, False]
    assert window.saves == []


def test_relocation_bumps_the_queue_mutation_epoch():
    window = _queue_window(["A.mp3", "B.mp4"])
    before = window._queue_mutation_epoch

    PlayerWindow._mark_queue_row_played(window, 0)

    assert window._queue_mutation_epoch == before + 1


def test_playlist_entry_object_travels_with_its_row():
    """_move_queue_row_to_bottom pops and re-appends the SAME entry
    object -- the property Phase B's token contract relies on."""
    window = _queue_window(["A.mp3", "B.mp4", "C.flac"])
    entry_a = object()
    window.queue_playlist_entries[0] = entry_a

    moved = PlayerWindow._mark_queue_row_played(window, 0)

    assert window.queue_playlist_entries[moved] is entry_a
    assert window.queue_playlist_entries[0] is None


def test_all_three_queue_lists_stay_length_aligned_through_relocation():
    window = _queue_window(["A.mp3", "B.mp4", "C.flac", "D.mp4"])

    for row in (0, 1, 0):
        PlayerWindow._mark_queue_row_played(window, row)
        assert len(window.queue) == 4
        assert len(window.queue_played) == 4
        assert len(window.queue_playlist_entries) == 4
