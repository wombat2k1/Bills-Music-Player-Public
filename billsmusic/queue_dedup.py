"""Filesystem-free path normalization and duplicate partitioning for batch
additions to the Up Next queue."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Generic, Iterable, List, Sequence, TypeVar

T = TypeVar("T")


def normalize_path_for_comparison(path: str) -> str:
    """Stable, filesystem-free path identity: case-insensitive on Windows,
    slash-direction normalized. No os.path.exists/stat/Path.resolve/tag reads."""
    return os.path.normcase(os.path.normpath(path))


def build_normalized_path_set(paths: Iterable[str]) -> set:
    return {normalize_path_for_comparison(p) for p in paths if p}


@dataclass
class DedupResult(Generic[T]):
    new_items: List[T] = field(default_factory=list)
    all_items: List[T] = field(default_factory=list)
    new_count: int = 0
    total_count: int = 0
    duplicate_count: int = 0
    existing_duplicate_count: int = 0
    intra_batch_duplicate_count: int = 0
    has_duplicates: bool = False


def _identity(item: T) -> str:
    return item  # type: ignore[return-value]


def partition_incoming_batch(
    incoming: Sequence[T],
    existing_queue_paths: Iterable[str],
    *,
    path_of: Callable[[T], str] = _identity,
) -> DedupResult[T]:
    """Partition an incoming batch against the current queue, preserving
    order and keeping the first occurrence of any duplicate.

    Builds the existing-queue normalized-path set exactly once. A single
    linear pass over `incoming` then classifies each item as: already in
    the existing queue, already seen earlier in this same batch, or new.
    """
    existing_normalized = build_normalized_path_set(existing_queue_paths)
    seen_in_batch: set = set()
    new_items: List[T] = []
    existing_dupes = 0
    intra_dupes = 0
    for item in incoming:
        raw_path = path_of(item)
        key = normalize_path_for_comparison(raw_path) if raw_path else None
        if key is not None and key in existing_normalized:
            existing_dupes += 1
        elif key is not None and key in seen_in_batch:
            intra_dupes += 1
        else:
            new_items.append(item)
        if key is not None:
            seen_in_batch.add(key)
    duplicate_count = existing_dupes + intra_dupes
    return DedupResult(
        new_items=new_items,
        all_items=list(incoming),
        new_count=len(new_items),
        total_count=len(incoming),
        duplicate_count=duplicate_count,
        existing_duplicate_count=existing_dupes,
        intra_batch_duplicate_count=intra_dupes,
        has_duplicates=duplicate_count > 0,
    )


@dataclass
class QueueAddOutcome:
    """Result of a duplicate-guarded batch add, for callers that want to
    build their own composite status message (e.g. playlist load)."""
    added_count: int = 0
    duplicate_count: int = 0
    cancelled: bool = False
