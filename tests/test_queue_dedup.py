from types import SimpleNamespace
from unittest import mock

from billsmusic.queue_dedup import (
    DedupResult,
    build_normalized_path_set,
    normalize_path_for_comparison,
    partition_incoming_batch,
)


# -- normalize_path_for_comparison ------------------------------------------

def test_case_insensitive_on_windows():
    assert normalize_path_for_comparison(r"C:\Music\A.mp3") == normalize_path_for_comparison(
        r"c:\music\a.mp3"
    )


def test_mixed_slash_direction_normalised():
    assert normalize_path_for_comparison(r"C:\Music\Album\track.mp3") == normalize_path_for_comparison(
        "C:/Music/Album/track.mp3"
    )


def test_harmless_syntax_normalised():
    assert normalize_path_for_comparison(r"C:\Music\.\Album\track.mp3") == normalize_path_for_comparison(
        r"C:\Music\Album\track.mp3"
    )


def test_normalization_never_touches_the_filesystem():
    with mock.patch("os.path.isfile", side_effect=AssertionError("isfile called")), \
         mock.patch("os.path.exists", side_effect=AssertionError("exists called")), \
         mock.patch("os.stat", side_effect=AssertionError("stat called")):
        normalize_path_for_comparison(r"C:\Music\A.mp3")
        build_normalized_path_set([r"C:\Music\A.mp3", r"D:\Other\B.flac"])


# -- partition_incoming_batch ------------------------------------------------

def test_no_duplicates_pass_through_unchanged():
    result = partition_incoming_batch(["a.mp3", "b.mp3"], ["c.mp3"])
    assert result.new_items == ["a.mp3", "b.mp3"]
    assert result.all_items == ["a.mp3", "b.mp3"]
    assert result.new_count == 2
    assert result.total_count == 2
    assert result.duplicate_count == 0
    assert not result.has_duplicates


def test_existing_queue_duplicate_is_skipped_in_new_only():
    result = partition_incoming_batch(["a.mp3", "b.mp3"], ["a.mp3"])
    assert result.new_items == ["b.mp3"]
    assert result.all_items == ["a.mp3", "b.mp3"]
    assert result.existing_duplicate_count == 1
    assert result.intra_batch_duplicate_count == 0
    assert result.duplicate_count == 1
    assert result.has_duplicates


def test_existing_queue_duplicate_matches_via_normalised_path():
    result = partition_incoming_batch([r"C:\Music\A.mp3"], [r"c:/music/a.mp3"])
    assert result.new_items == []
    assert result.existing_duplicate_count == 1


def test_intra_batch_duplicate_keeps_first_occurrence_and_order():
    result = partition_incoming_batch(["a.mp3", "b.mp3", "a.mp3", "c.mp3"], [])
    assert result.new_items == ["a.mp3", "b.mp3", "c.mp3"]
    assert result.all_items == ["a.mp3", "b.mp3", "a.mp3", "c.mp3"]
    assert result.intra_batch_duplicate_count == 1
    assert result.existing_duplicate_count == 0
    assert result.duplicate_count == 1


def test_mixed_existing_and_intra_batch_duplicates():
    result = partition_incoming_batch(
        ["a.mp3", "b.mp3", "a.mp3", "c.mp3"], ["c.mp3"]
    )
    # first "a.mp3" and "b.mp3" are genuinely new; the second "a.mp3" is an
    # intra-batch duplicate (first occurrence kept), "c.mp3" already exists.
    assert result.new_items == ["a.mp3", "b.mp3"]
    assert result.existing_duplicate_count == 1  # c.mp3
    assert result.intra_batch_duplicate_count == 1  # second a.mp3
    assert result.duplicate_count == 2
    assert result.new_count == 2
    assert result.total_count == 4


def test_empty_incoming_batch():
    result = partition_incoming_batch([], ["a.mp3"])
    assert result == DedupResult(
        new_items=[], all_items=[], new_count=0, total_count=0,
        duplicate_count=0, existing_duplicate_count=0,
        intra_batch_duplicate_count=0, has_duplicates=False,
    )


def test_custom_path_of_callback_for_non_string_items():
    entries = [
        SimpleNamespace(resolved_path=None, path="a.mp3"),
        SimpleNamespace(resolved_path="b_resolved.mp3", path="b.mp3"),
    ]
    result = partition_incoming_batch(
        entries, ["a.mp3"],
        path_of=lambda e: e.resolved_path or e.path,
    )
    assert result.new_items == [entries[1]]
    assert result.existing_duplicate_count == 1


def test_single_existing_set_built_once_per_call():
    # build_normalized_path_set should only need to be invoked one time
    # per partition_incoming_batch call, regardless of batch size.
    calls = []
    real_build = build_normalized_path_set

    def _spy(paths):
        calls.append(1)
        return real_build(paths)

    with mock.patch("billsmusic.queue_dedup.build_normalized_path_set", side_effect=_spy):
        partition_incoming_batch(["a.mp3", "b.mp3", "c.mp3", "d.mp3"], ["x.mp3", "y.mp3"])
    assert len(calls) == 1
