"""Per-tab library state for the Music/Videos split.

PlayerWindow keeps exactly one live copy of each of these fields as a plain
instance attribute (``self.tree_tracks``, ``self.tree_item_by_path``, ...)
because dozens of existing methods already read/write them directly. Rather
than rewriting every one of those call sites to go through a tab object,
each ``LibraryTabState`` is a parked snapshot of that same set of fields for
one tab; switching the active tab means copying the live attributes out into
the tab that's losing focus, then copying the newly-active tab's stored
values back onto the live attributes. See ``PlayerWindow._activate_library_tab``
in window.py.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .media_type import MediaType

# Window attribute names that are swapped in lock-step when the active
# library tab changes. Order doesn't matter; every name here must exist as
# a plain PlayerWindow instance attribute before the first tab swap.
LIBRARY_TAB_ALIASED_ATTRS = (
    "tree_tracks",
    "tree_item_by_path",
    "album_item_by_key",
    "album_key_by_path",
    "tracks",
    "track_index_by_path",
    "_build_queue",
    "_populate_queue",
    "_lazy_albums_populated",
    "_lazy_track_rows_created",
    "_library_apply_generation",
    "_library_apply_token",
    "_library_apply_reason",
    "_library_apply_correlation_id",
    "_pending_selection_path",
    "_pending_selection_album_key",
    "_library_search_generation",
    "_library_search_index",
)


@dataclass
class LibraryTabState:
    """One tab's persisted slice of the library-tree/search state."""

    media_type: MediaType
    tree_tracks: Any = None
    tree_item_by_path: Dict[str, Any] = field(default_factory=dict)
    album_item_by_key: Dict[str, Any] = field(default_factory=dict)
    album_key_by_path: Dict[str, str] = field(default_factory=dict)
    tracks: List[str] = field(default_factory=list)
    track_index_by_path: Dict[str, int] = field(default_factory=dict)
    _build_queue: "deque" = field(default_factory=deque)
    _populate_queue: List[Any] = field(default_factory=list)
    _lazy_albums_populated: int = 0
    _lazy_track_rows_created: int = 0
    _library_apply_generation: Optional[int] = None
    _library_apply_token: int = 0
    _library_apply_reason: Optional[str] = None
    _library_apply_correlation_id: Optional[str] = None
    _pending_selection_path: Optional[str] = None
    _pending_selection_album_key: Optional[str] = None
    _library_search_generation: int = 0
    _library_search_index: List[Any] = field(default_factory=list)
