"""Phase B: queue entries commit only on authoritative playback.

The contract, in one sentence: selecting a queue row CLAIMS its stable
token; only the attempt that owns that claim may commit it, and only when
it genuinely becomes authoritative playback.

Two properties are load-bearing and get most of the coverage here:

  Ownership. Claims map token -> owner, never a bare set, so a stale
  attempt terminating after a recovery/fallback handoff cannot release a
  claim its replacement now holds.

  Selection-site claiming. The claim is taken where the row is SELECTED,
  not inside _play_path_direct, so the contract holds even when dispatch
  is stubbed -- and a dispatch that fails before any attempt exists still
  has an owner to release it.
"""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from billsmusic.media_type import MediaType
from billsmusic.playback_attempt import PlaybackAttemptState
from billsmusic.window import (
    PlayerWindow,
    _claim_queue_entry_token_for,
    _claim_queue_selection_for,
    _commit_queue_entry_token_for,
    _queue_entry_claim_owner_of,
    _queue_token_for_row_of,
    _release_queue_entry_claim_for,
    _selection_claim_owner,
    _transfer_queue_entry_claim_for,
)


class _Diagnostics:
    def __init__(self):
        self.events = []

    def record(self, category, operation, **kw):
        self.events.append((category, operation, kw.get("details") or {}))

    def path_details(self, path):
        return {"path": path}


def _window(queue=None, played=None):
    """Namespace carrying real queue lists plus the commit hooks, with the
    view side stubbed. Relocation is REAL -- committing genuinely moves the
    row, which is the behaviour identity has to survive."""
    paths = list(queue if queue is not None else ["a.mp3", "b.mp3", "c.mp3"])
    w = SimpleNamespace(
        queue=paths,
        queue_played=list(played if played is not None else [False] * len(paths)),
        queue_playlist_entries=[None] * len(paths),
        _queue_mutation_epoch=0,
        _queue_entry_claims={},
        _next_queue_entry_token=1,
        _next_queue_selection_id=1,
        _next_playback_attempt_id=1,
        _current_playback_attempt=None,
        _current_media_type=MediaType.AUDIO,
        diagnostics=_Diagnostics(),
    )
    w._ensure_queue_played_flags = lambda: PlayerWindow._ensure_queue_played_flags(w)
    w._move_queue_row_to_bottom = lambda row: PlayerWindow._move_queue_row_to_bottom(w, row)
    w._remove_queue_row_widget = lambda row, reason=None: None
    w._insert_queue_row_widget = lambda row, reason=None: None
    w._animate_queue_history_move = lambda row: None
    w._schedule_session_save = lambda: None
    w._mark_queue_row_played = lambda row: PlayerWindow._mark_queue_row_played(w, row)
    w._ensure_queue_played_flags()
    return w


def _begin(window, path, *, token=None, reason="test"):
    return PlayerWindow._begin_playback_attempt(
        window, path, MediaType.AUDIO, reason, queue_entry_id=token,
    )


def _advance(window, attempt_id, state):
    PlayerWindow._advance_playback_attempt_state(window, attempt_id, state)


def _claims(window):
    return dict(window._queue_entry_claims)


# -- selection claims, independent of dispatch ----------------------------

def test_selection_claims_the_row_even_when_dispatch_is_stubbed():
    """The whole point of claiming at the selection site: a stubbed
    _play_path_direct must not be able to make the contract silently
    disappear."""
    w = _window()
    token, owner = _claim_queue_selection_for(w, 1, reason="next")

    assert token == w._queue_entry_tokens[1]
    assert _queue_entry_claim_owner_of(w, token) == owner
    # Claimed is not played, and the queue is untouched.
    assert w.queue_played == [False, False, False]
    assert w.queue == ["a.mp3", "b.mp3", "c.mp3"]


def test_selection_owner_is_unique_per_selection():
    w = _window()
    _, first = _claim_queue_selection_for(w, 0, reason="next")
    _, second = _claim_queue_selection_for(w, 1, reason="next")
    assert first != second


def test_selection_of_a_row_without_a_token_claims_nothing():
    w = _window()
    token, owner = _claim_queue_selection_for(w, 99, reason="next")
    assert (token, owner) == (None, None)
    assert _claims(w) == {}


def test_dispatch_failure_releases_the_selection_claim():
    """Nothing took ownership, so the selection must release or the row
    would be skipped forever."""
    w = _window()
    token, owner = _claim_queue_selection_for(w, 1, reason="next")

    _release_queue_entry_claim_for(w, token, attempt_id=owner, reason="dispatch_failed")

    assert _claims(w) == {}
    assert w.queue_played == [False, False, False]


# -- ownership transfer ----------------------------------------------------

def test_attempt_creation_transfers_ownership_exactly_once():
    w = _window()
    token, selection_owner = _claim_queue_selection_for(w, 1, reason="next")

    attempt = _begin(w, "b.mp3", token=token)

    assert _queue_entry_claim_owner_of(w, token) == attempt.attempt_id
    # A second transfer from the now-stale selection owner is refused.
    assert not _transfer_queue_entry_claim_for(
        w, token, from_owner=selection_owner, to_owner=999, reason="again",
    )
    assert _queue_entry_claim_owner_of(w, token) == attempt.attempt_id


def test_stale_selection_cleanup_cannot_release_an_attempt_owned_claim():
    w = _window()
    token, selection_owner = _claim_queue_selection_for(w, 1, reason="next")
    attempt = _begin(w, "b.mp3", token=token)

    # The selection site's own late "dispatch failed" cleanup must be inert.
    released = _release_queue_entry_claim_for(
        w, token, attempt_id=selection_owner, reason="late_dispatch_failed",
    )

    assert released is False
    assert _queue_entry_claim_owner_of(w, token) == attempt.attempt_id


# -- commit on authoritative playback -------------------------------------

def test_playing_commits_the_claim_exactly_once():
    w = _window()
    token, _ = _claim_queue_selection_for(w, 0, reason="next")
    attempt = _begin(w, "a.mp3", token=token)

    _advance(w, attempt.attempt_id, PlaybackAttemptState.PLAYING)

    assert w.queue_played[w._queue_entry_tokens.index(token)] is True
    assert _claims(w) == {}
    # Idempotent: a second PLAYING advance commits nothing further.
    before = list(w.queue)
    _advance(w, attempt.attempt_id, PlaybackAttemptState.PLAYING)
    assert w.queue == before


def test_preparing_and_starting_never_commit():
    w = _window()
    token, _ = _claim_queue_selection_for(w, 0, reason="next")
    attempt = _begin(w, "a.mp3", token=token)

    _advance(w, attempt.attempt_id, PlaybackAttemptState.PREPARING)
    _advance(w, attempt.attempt_id, PlaybackAttemptState.STARTING)

    assert w.queue_played == [False, False, False]
    assert _queue_entry_claim_owner_of(w, token) == attempt.attempt_id


def test_failed_attempt_releases_and_leaves_the_row_unplayed_and_in_place():
    w = _window()
    token, _ = _claim_queue_selection_for(w, 1, reason="next")
    attempt = _begin(w, "b.mp3", token=token)

    _advance(w, attempt.attempt_id, PlaybackAttemptState.FAILED)

    assert _claims(w) == {}
    assert w.queue_played == [False, False, False]
    assert w.queue == ["a.mp3", "b.mp3", "c.mp3"]


def test_stop_during_preparation_commits_nothing():
    w = _window()
    token, _ = _claim_queue_selection_for(w, 1, reason="next")
    attempt = _begin(w, "b.mp3", token=token)
    _advance(w, attempt.attempt_id, PlaybackAttemptState.PREPARING)

    PlayerWindow._cancel_current_playback_attempt(w, "stop_playback")

    assert _claims(w) == {}
    assert w.queue_played == [False, False, False]


def test_commit_is_refused_for_an_attempt_that_no_longer_owns_the_claim():
    w = _window()
    token, _ = _claim_queue_selection_for(w, 0, reason="next")
    first = _begin(w, "a.mp3", token=token)
    _transfer_queue_entry_claim_for(
        w, token, from_owner=first.attempt_id, to_owner=4242, reason="handoff",
    )

    assert _commit_queue_entry_token_for(
        w, token, attempt_id=first.attempt_id, reason="stale",
    ) is None
    assert w.queue_played == [False, False, False]


def test_commit_skips_an_entry_that_left_the_queue():
    w = _window()
    token, owner = _claim_queue_selection_for(w, 1, reason="next")
    w.queue.pop(1)
    w.queue_played.pop(1)
    w.queue_playlist_entries.pop(1)
    w._queue_entry_tokens.pop(1)

    assert _commit_queue_entry_token_for(w, token, attempt_id=owner, reason="gone") is None


# -- hostile ordering ------------------------------------------------------

def test_rapid_next_a_b_c_only_the_final_selection_commits():
    w = _window(queue=["a.mp3", "b.mp3", "c.mp3"])
    tokens = list(w._queue_entry_tokens)

    a_token, _ = _claim_queue_selection_for(w, 0, reason="next")
    attempt_a = _begin(w, "a.mp3", token=a_token)
    b_token, _ = _claim_queue_selection_for(w, 1, reason="next")
    attempt_b = _begin(w, "b.mp3", token=b_token)
    c_token, _ = _claim_queue_selection_for(w, 2, reason="next")
    attempt_c = _begin(w, "c.mp3", token=c_token)

    _advance(w, attempt_c.attempt_id, PlaybackAttemptState.PLAYING)

    # Only C committed; A and B were superseded and released.
    assert w.queue_played[w._queue_entry_tokens.index(tokens[2])] is True
    assert w.queue_played[w._queue_entry_tokens.index(tokens[0])] is False
    assert w.queue_played[w._queue_entry_tokens.index(tokens[1])] is False
    assert _claims(w) == {}
    assert attempt_a.state is PlaybackAttemptState.CANCELLED
    assert attempt_b.state is PlaybackAttemptState.CANCELLED


def test_stale_completion_after_a_newer_selection_commits_nothing():
    w = _window()
    a_token, _ = _claim_queue_selection_for(w, 0, reason="next")
    attempt_a = _begin(w, "a.mp3", token=a_token)
    b_token, _ = _claim_queue_selection_for(w, 1, reason="next")
    _begin(w, "b.mp3", token=b_token)

    # A's late completion arrives now.
    _advance(w, attempt_a.attempt_id, PlaybackAttemptState.PLAYING)

    assert w.queue_played == [False, False, False]


def test_reorder_while_preparation_is_pending_commits_the_right_row():
    """The queue moves under an in-flight attempt; the token still
    resolves to the entry that was actually selected."""
    w = _window(queue=["a.mp3", "b.mp3", "c.mp3"])
    token, _ = _claim_queue_selection_for(w, 1, reason="next")  # b.mp3
    attempt = _begin(w, "b.mp3", token=token)

    # User drags b.mp3 to the top while it is still preparing.
    for lst in (w.queue, w.queue_played, w.queue_playlist_entries, w._queue_entry_tokens):
        lst.insert(0, lst.pop(1))

    _advance(w, attempt.attempt_id, PlaybackAttemptState.PLAYING)

    committed_row = w._queue_entry_tokens.index(token)
    assert w.queue[committed_row] == "b.mp3"
    assert w.queue_played[committed_row] is True


def test_duplicate_identical_paths_commit_only_the_selected_token():
    w = _window(queue=["same.mp3", "other.mp3", "same.mp3"])
    first_same, _, last_same = list(w._queue_entry_tokens)

    token, _ = _claim_queue_selection_for(w, 2, reason="next")  # the LAST copy
    assert token == last_same
    attempt = _begin(w, "same.mp3", token=token)
    _advance(w, attempt.attempt_id, PlaybackAttemptState.PLAYING)

    assert w.queue_played[w._queue_entry_tokens.index(last_same)] is True
    assert w.queue_played[w._queue_entry_tokens.index(first_same)] is False


# -- recovery / fallback handoff ------------------------------------------

def test_recovery_receives_the_same_token_and_owns_the_claim():
    w = _window()
    token, _ = _claim_queue_selection_for(w, 0, reason="next")
    original = _begin(w, "a.mp3", token=token)

    recovery = _begin(w, "a.mp3", token=token, reason="recovery_open_failed")

    assert recovery.queue_entry_id == token
    assert _queue_entry_claim_owner_of(w, token) == recovery.attempt_id
    assert original.state is PlaybackAttemptState.CANCELLED


def test_original_failing_after_handoff_cannot_release_recoverys_claim():
    w = _window()
    token, _ = _claim_queue_selection_for(w, 0, reason="next")
    original = _begin(w, "a.mp3", token=token)
    recovery = _begin(w, "a.mp3", token=token, reason="recovery")

    # The original's own terminal cleanup arrives late.
    _advance(w, original.attempt_id, PlaybackAttemptState.FAILED)

    assert _queue_entry_claim_owner_of(w, token) == recovery.attempt_id


def test_recovery_success_commits_the_inherited_token_once():
    w = _window()
    token, _ = _claim_queue_selection_for(w, 0, reason="next")
    _begin(w, "a.mp3", token=token)
    recovery = _begin(w, "a.mp3", token=token, reason="recovery")

    _advance(w, recovery.attempt_id, PlaybackAttemptState.PLAYING)

    assert w.queue_played[w._queue_entry_tokens.index(token)] is True
    assert _claims(w) == {}


def test_recovery_failure_releases_the_token_and_leaves_the_row_in_place():
    w = _window()
    token, _ = _claim_queue_selection_for(w, 1, reason="next")
    _begin(w, "b.mp3", token=token)
    recovery = _begin(w, "b.mp3", token=token, reason="recovery")

    _advance(w, recovery.attempt_id, PlaybackAttemptState.FAILED)

    assert _claims(w) == {}
    assert w.queue_played == [False, False, False]
    assert w.queue == ["a.mp3", "b.mp3", "c.mp3"]


def test_recovery_that_cannot_start_leaves_the_original_to_release():
    """No replacement owner is created, so the original failure releases
    the claim normally."""
    w = _window()
    token, _ = _claim_queue_selection_for(w, 1, reason="next")
    original = _begin(w, "b.mp3", token=token)

    _advance(w, original.attempt_id, PlaybackAttemptState.FAILED)

    assert _claims(w) == {}
    assert w.queue_played == [False, False, False]


# -- Cast lifecycle --------------------------------------------------------

def test_cast_dispatch_claims_but_does_not_commit():
    w = _window()
    token, _ = _claim_queue_selection_for(w, 0, reason="cast")
    attempt = _begin(w, "a.mp3", token=token)

    assert _queue_entry_claim_owner_of(w, token) == attempt.attempt_id
    assert w.queue_played == [False, False, False]


def test_cast_receiver_confirmation_commits_exactly_once():
    w = _window()
    token, _ = _claim_queue_selection_for(w, 0, reason="cast")
    attempt = _begin(w, "a.mp3", token=token)

    _advance(w, attempt.attempt_id, PlaybackAttemptState.PLAYING)  # `loaded`

    assert w.queue_played[w._queue_entry_tokens.index(token)] is True
    assert _claims(w) == {}


def test_cast_receiver_error_releases_the_claim():
    w = _window()
    token, _ = _claim_queue_selection_for(w, 0, reason="cast")
    attempt = _begin(w, "a.mp3", token=token)

    _advance(w, attempt.attempt_id, PlaybackAttemptState.FAILED)  # `failed`

    assert _claims(w) == {}
    assert w.queue_played == [False, False, False]


def test_cast_timeout_releases_the_claim_through_the_same_failure_path():
    """No second timer exists: CastController.load() ends in
    pychromecast's bounded block_until_active(timeout=10), and
    _load_worker turns that timeout into the same `failed` signal an
    explicit receiver error produces."""
    w = _window()
    token, _ = _claim_queue_selection_for(w, 0, reason="cast")
    attempt = _begin(w, "a.mp3", token=token)

    _advance(w, attempt.attempt_id, PlaybackAttemptState.FAILED)

    assert _claims(w) == {}
    assert w.queue_played == [False, False, False]


def test_late_cast_confirmation_after_supersession_does_nothing():
    w = _window()
    token, _ = _claim_queue_selection_for(w, 0, reason="cast")
    cast_attempt = _begin(w, "a.mp3", token=token)
    other_token, _ = _claim_queue_selection_for(w, 1, reason="next")
    _begin(w, "b.mp3", token=other_token)

    _advance(w, cast_attempt.attempt_id, PlaybackAttemptState.PLAYING)

    assert w.queue_played == [False, False, False]


def test_cast_failure_to_local_fallback_preserves_the_token():
    w = _window()
    token, _ = _claim_queue_selection_for(w, 0, reason="cast")
    cast_attempt = _begin(w, "a.mp3", token=token)

    fallback = _begin(w, "a.mp3", token=token, reason="return_to_local")
    assert _queue_entry_claim_owner_of(w, token) == fallback.attempt_id

    # The Cast attempt's own late failure cannot disturb the fallback.
    _advance(w, cast_attempt.attempt_id, PlaybackAttemptState.FAILED)
    assert _queue_entry_claim_owner_of(w, token) == fallback.attempt_id

    _advance(w, fallback.attempt_id, PlaybackAttemptState.PLAYING)
    assert w.queue_played[w._queue_entry_tokens.index(token)] is True


def test_stop_during_pending_cast_releases_the_claim():
    w = _window()
    token, _ = _claim_queue_selection_for(w, 0, reason="cast")
    _begin(w, "a.mp3", token=token)

    PlayerWindow._cancel_current_playback_attempt(w, "stop_playback")

    assert _claims(w) == {}
    assert w.queue_played == [False, False, False]


# -- next-unplayed interaction --------------------------------------------

def test_next_unplayed_skips_a_claimed_row_so_manual_next_moves_past_it():
    w = _window()
    _claim_queue_selection_for(w, 0, reason="next")
    assert PlayerWindow._next_unplayed_queue_row(w) == 1


def test_releasing_a_claim_makes_the_row_selectable_again():
    w = _window()
    token, owner = _claim_queue_selection_for(w, 0, reason="next")
    assert PlayerWindow._next_unplayed_queue_row(w) == 1

    _release_queue_entry_claim_for(w, token, attempt_id=owner, reason="abandoned")

    assert PlayerWindow._next_unplayed_queue_row(w) == 0


def test_a_committed_row_is_played_and_relocated_to_the_bottom():
    w = _window()
    token, _ = _claim_queue_selection_for(w, 0, reason="next")
    attempt = _begin(w, "a.mp3", token=token)

    _advance(w, attempt.attempt_id, PlaybackAttemptState.PLAYING)

    assert w.queue == ["b.mp3", "c.mp3", "a.mp3"]
    assert w.queue_played == [False, False, True]
    assert w._queue_entry_tokens[-1] == token
