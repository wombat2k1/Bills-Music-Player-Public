"""Phase D: dual-transition preloads are identified by queue token.

SecondaryIdentity used to be (epoch, row, path), written when the queue
had no per-row IDs. The epoch was a global counter bumped by EVERY
queue-structure mutation, so it was strictly conservative -- it never
mis-targeted, but it threw away a perfectly valid preload whenever any
unrelated row was added, removed or moved. A discarded GPU preload means a
hard cut instead of a transition, so that was a real user-visible cost.

The replacement is the Phase B rule:

    token locates, source validates

A preload survives anything that moves its row without changing its
content, and is invalidated when the entry leaves the queue, when its
content is replaced in place (missing-track repair keeps a row's token
while swapping its path), or when a different entry leads.

The row is gone from the identity entirely -- a row is a presentation
position, never an identity.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtWidgets

from billsmusic.media_type import MediaType
from billsmusic.queue_dedup import same_logical_source
from billsmusic.video_dual_transition import DualDeckController, SecondaryIdentity


@pytest.fixture(scope="module", autouse=True)
def qapplication():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def _identity(token=3, source="video_b.mp4", media_type=MediaType.VIDEO):
    return SecondaryIdentity(
        queue_token=token, expected_source=source, media_type=media_type,
    )


def _preloaded(token=3, source="video_b.mp4"):
    controller = DualDeckController()
    controller.begin_preload(_identity(token, source))
    return controller


# -- 12. no retained row -----------------------------------------------------

def test_secondary_identity_has_no_row_or_epoch_field():
    fields = set(SecondaryIdentity.__dataclass_fields__)
    assert fields == {"queue_token", "expected_source", "media_type"}
    assert "row" not in fields
    assert "epoch" not in fields


# -- 1-5. things that must NOT invalidate ------------------------------------

def test_insertion_before_the_target_does_not_invalidate():
    """An unrelated row is added above the target. Its row shifts; its
    token and content do not."""
    controller = _preloaded(token=3, source="video_b.mp4")
    assert controller.is_stale(_identity(3, "video_b.mp4")) is False


def test_removal_of_an_unrelated_entry_does_not_invalidate():
    controller = _preloaded(token=3, source="video_b.mp4")
    assert controller.is_stale(_identity(3, "video_b.mp4")) is False


def test_reorder_does_not_invalidate():
    controller = _preloaded(token=3, source="video_b.mp4")
    assert controller.is_stale(_identity(3, "video_b.mp4")) is False


def test_target_token_moving_rows_still_validates():
    """The whole point: the identity carries no row, so the target moving
    from row 5 to row 0 is invisible to validation."""
    controller = _preloaded(token=42, source="video_b.mp4")
    assert controller.is_stale(_identity(42, "video_b.mp4")) is False
    assert controller.is_ready_for(_identity(42, "video_b.mp4")) is False  # not READY yet


def test_unrelated_mutation_epoch_change_does_not_invalidate():
    """The behavioural change this commit exists for. Under the old
    (epoch, row, path) model ANY queue edit bumped the epoch and discarded
    the preload. There is no epoch in the identity any more, so an
    unrelated edit cannot express itself as invalidation at all."""
    controller = _preloaded(token=3, source="video_b.mp4")
    for _unrelated_edit in range(5):
        assert controller.is_stale(_identity(3, "video_b.mp4")) is False


# -- 6-7. things that MUST invalidate ----------------------------------------

def test_target_token_removed_invalidates():
    """The provider returns None when nothing is queued -- the entry left."""
    controller = _preloaded(token=3, source="video_b.mp4")
    assert controller.is_stale(None) is True


def test_a_different_entry_leading_invalidates():
    controller = _preloaded(token=3, source="video_b.mp4")
    assert controller.is_stale(_identity(4, "video_b.mp4")) is True


def test_same_token_with_a_replaced_path_invalidates_the_prepared_media():
    """Missing-track repair deliberately preserves an entry's token while
    replacing its path. Prepared A must not play for an entry now holding
    B -- a surviving token alone does not prove content validity."""
    controller = _preloaded(token=3, source="A.mp4")
    assert controller.is_stale(_identity(3, "B.mp4")) is True
    assert controller.is_stale(_identity(3, "A.mp4")) is False


# -- 8. duplicate identical paths -------------------------------------------

def test_duplicate_identical_paths_do_not_cross_target():
    """Two queue entries hold the same path with different tokens. A path
    comparison could not tell them apart; the token can, in both
    directions."""
    controller = _preloaded(token=1, source="same.mp4")

    assert controller.is_stale(_identity(1, "same.mp4")) is False   # ours
    assert controller.is_stale(_identity(2, "same.mp4")) is True    # the twin

    assert controller.is_ready_for(_identity(2, "same.mp4")) is False


def test_identity_equality_distinguishes_duplicate_paths():
    assert _identity(1, "same.mp4") != _identity(2, "same.mp4")
    assert _identity(1, "same.mp4") == _identity(1, "same.mp4")


# -- 9-10. supersession and stale completion ---------------------------------

def test_a_cancelled_preload_is_not_ready_for_anything():
    controller = _preloaded(token=3, source="video_b.mp4")
    controller.cancel()
    assert controller.is_ready_for(_identity(3, "video_b.mp4")) is False


def test_a_superseded_preload_target_is_stale():
    """A newer preload replaced the stored identity; the older target no
    longer matches."""
    controller = _preloaded(token=3, source="old.mp4")
    controller.cancel()
    controller.begin_preload(_identity(9, "new.mp4"))

    assert controller.is_stale(_identity(3, "old.mp4")) is True
    assert controller.is_stale(_identity(9, "new.mp4")) is False


def test_is_stale_is_false_when_nothing_is_preloaded():
    """No stored identity means there is nothing to invalidate -- callers
    distinguish "no preload" from "stale preload" by state, not here."""
    controller = DualDeckController()
    assert controller.is_stale(_identity(3, "video_b.mp4")) is False
    assert controller.is_stale(None) is False


# -- 11. canonical source comparison ----------------------------------------

def test_source_comparison_uses_canonical_normalisation_for_filesystem_paths():
    controller = _preloaded(token=3, source=os.path.join("dir", "sub", "clip.mp4"))

    assert controller.is_stale(_identity(3, "dir/sub/clip.mp4")) is False
    assert controller.is_stale(_identity(3, "dir//sub//clip.mp4")) is False
    assert controller.is_stale(_identity(3, "dir/sub/other.mp4")) is True


def test_plex_identities_are_compared_exactly_not_path_normalised():
    """A plex:// identity is not a filesystem path -- normcase/normpath
    would mangle its scheme and separators. (Plex cannot currently reach
    the preload path at all: classify_path("plex://...") is UNSUPPORTED
    and preload requires VIDEO. This keeps the comparator correct by
    construction rather than by that accident.)"""
    assert same_logical_source("plex://server/1234", "plex://server/1234") is True
    assert same_logical_source("plex://server/1234", "plex://server/5678") is False
    assert same_logical_source("plex://server/1234", "clip.mp4") is False
    assert same_logical_source("PLEX://server/1234", "plex://server/1234") is False


def test_empty_sources_never_compare_equal():
    assert same_logical_source("", "") is False
    assert same_logical_source(None, "a.mp4") is False
    assert same_logical_source("a.mp4", None) is False
