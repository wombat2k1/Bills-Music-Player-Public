"""Minimal, dependency-free Matroska (.mkv/.webm) tag reader.

mutagen has never supported the Matroska container -- MutagenFile()
simply returns None for a .mkv file, regardless of what tags a
full-featured tool like TagScanner or mkvpropedit wrote into it
(confirmed against a real ~600-file .mkv video collection: every one
showed up as "Unknown Artist" despite having real tags).

Rather than add a third-party MKV-parsing dependency, this reads exactly
the handful of EBML elements read_track_meta() actually needs (title,
artist, album, date, genre) directly from the Matroska structure, using
each element's own declared size to skip everything else (the actual
video/audio Cluster data, SeekHead, Cues, ...) without reading it -- a
tag lookup on a multi-gigabyte file only ever touches a few KB near
wherever its Tags/Info elements happen to sit.

References:
  EBML:      https://www.rfc-editor.org/rfc/rfc8794
  Matroska:  https://www.matroska.org/technical/elements.html
  Tagging:   https://www.matroska.org/technical/tagging.html
"""
from __future__ import annotations

import io
from typing import BinaryIO, Dict, Iterator, List, Optional, Tuple

_ID_SEGMENT = 0x18538067
_ID_INFO = 0x1549A966
_ID_TITLE = 0x7BA9        # inside Info -- the segment/movie title
_ID_TAGS = 0x1254C367
_ID_TAG = 0x7373
_ID_SIMPLETAG = 0x67C8
_ID_TAGNAME = 0x45A3
_ID_TAGSTRING = 0x4487

_TAG_NAME_MAP = {
    "TITLE": "title",
    "ARTIST": "artist",
    "ALBUM": "album",
    "ALBUM_ARTIST": "album_artist",
    "DATE_RELEASED": "date",
    "DATE": "date",
    "GENRE": "genre",
}

# The Segment Info title (unlike a deliberate Tags/SimpleTag TITLE) is
# frequently just whatever the ripping tool stamped on the disc image,
# not a real title -- confirmed against a real ~600-file .mkv collection,
# where the overwhelming majority carry exactly this DVD-authoring
# artifact as their Info title. Worse than no title at all, since
# callers already have a sensible filename-based fallback of their own.
_JUNK_INFO_TITLES = frozenset({"video_ts"})

# A legitimate tag value (artist/title/etc.) is at most a few hundred
# bytes -- caps how much a single string read will ever pull in, so a
# corrupt or hostile size field can't force a huge allocation.
_MAX_STRING_BYTES = 4096
# Bounds total elements visited across one file, so a corrupt/adversarial
# structure (e.g. a size field that keeps landing just short of a
# boundary) can't turn this into an unbounded loop.
_MAX_ELEMENTS_SCANNED = 200_000


def _read_vint(stream: BinaryIO, *, keep_marker: bool) -> Optional[int]:
    """Reads one EBML variable-length integer. Element IDs conventionally
    keep their leading length-descriptor bits as part of the ID value
    (keep_marker=True); element sizes have those bits masked off to
    leave just the numeric value (keep_marker=False). Returns None at
    EOF or on a malformed (leading all-zero) length descriptor."""
    first = stream.read(1)
    if not first:
        return None
    first_byte = first[0]
    if first_byte == 0:
        return None
    length = 1
    mask = 0x80
    while not (first_byte & mask):
        mask >>= 1
        length += 1
        if length > 8:
            return None
    rest = stream.read(length - 1)
    if len(rest) != length - 1:
        return None
    value = first_byte if keep_marker else (first_byte & (mask - 1))
    for byte in rest:
        value = (value << 8) | byte
    return value


def _element_header(stream: BinaryIO) -> Optional[Tuple[int, int]]:
    """Returns (element_id, size) for a definite-size element, or None at
    EOF, on malformed data, or for an EBML "unknown size" element (all
    value bits set to 1 -- valid for a still-being-written Segment, but
    not safely skippable by byte count, so callers stop rather than
    guess how far it extends)."""
    element_id = _read_vint(stream, keep_marker=True)
    if element_id is None:
        return None
    size_start = stream.tell()
    size = _read_vint(stream, keep_marker=False)
    if size is None:
        return None
    size_bytes = stream.tell() - size_start
    if size_bytes <= 0 or size_bytes > 8:
        return None
    all_ones = (1 << (7 * size_bytes)) - 1
    if size == all_ones:
        return None
    return element_id, size


def _iter_children(
    f: BinaryIO, end: int, budget: List[int]
) -> Iterator[Tuple[int, int, int]]:
    """Yields (element_id, size, child_end) for each direct child element
    between the stream's current position and `end`. Always resumes
    exactly at the previous child's declared end before reading the next
    header, regardless of whether the caller actually consumed that
    child's bytes -- so a caller can freely skip a child (most of them)
    or read into it (Info, Tags) without corrupting sibling traversal.
    `budget` is a one-item list used as a shared mutable counter so
    nested scans still count against the same overall element budget."""
    while f.tell() < end:
        if budget[0] <= 0:
            return
        budget[0] -= 1
        header = _element_header(f)
        if header is None:
            return
        element_id, size = header
        child_end = f.tell() + size
        if child_end > end:
            return  # child claims to extend past its parent -- corrupt
        yield element_id, size, child_end
        f.seek(child_end)


def _read_utf8(f: BinaryIO, size: int) -> str:
    if size <= 0:
        return ""
    data = f.read(min(size, _MAX_STRING_BYTES))
    try:
        return data.decode("utf-8", errors="replace").strip("\x00").strip()
    except Exception:
        return ""


def _scan_simpletag(
    f: BinaryIO, end: int, budget: List[int], result: Dict[str, str]
) -> None:
    name = None
    value = None
    for element_id, size, _child_end in _iter_children(f, end, budget):
        if element_id == _ID_TAGNAME:
            name = _read_utf8(f, size)
        elif element_id == _ID_TAGSTRING:
            value = _read_utf8(f, size)
    if name and value:
        key = _TAG_NAME_MAP.get(name.strip().upper())
        if key:
            result.setdefault(key, value)


def _scan_tag(
    f: BinaryIO, end: int, budget: List[int], result: Dict[str, str]
) -> None:
    for element_id, _size, child_end in _iter_children(f, end, budget):
        if element_id == _ID_SIMPLETAG:
            _scan_simpletag(f, child_end, budget, result)


def _scan_tags(
    f: BinaryIO, end: int, budget: List[int], result: Dict[str, str]
) -> None:
    for element_id, _size, child_end in _iter_children(f, end, budget):
        if element_id == _ID_TAG:
            _scan_tag(f, child_end, budget, result)


def _scan_info_title(f: BinaryIO, end: int, budget: List[int]) -> Optional[str]:
    for element_id, size, _child_end in _iter_children(f, end, budget):
        if element_id == _ID_TITLE:
            value = _read_utf8(f, size)
            if value and value.casefold() not in _JUNK_INFO_TITLES:
                return value
    return None


def _scan_segment(
    f: BinaryIO, end: int, budget: List[int], result: Dict[str, str]
) -> None:
    # Info's Title is frequently a generic artifact from whatever ripped
    # the file (e.g. "VIDEO_TS", confirmed against real files) rather
    # than a deliberately-set value, unlike a Tags/SimpleTag TITLE -- so
    # Tags must win regardless of which element the file happens to list
    # first. Collected separately and merged in only after both have been
    # scanned, rather than writing straight into `result` as each is
    # found, to make that priority independent of file byte order.
    have_info = False
    have_tags = False
    info_title = None
    for element_id, _size, child_end in _iter_children(f, end, budget):
        if element_id == _ID_INFO and not have_info:
            info_title = _scan_info_title(f, child_end, budget)
            have_info = True
        elif element_id == _ID_TAGS and not have_tags:
            _scan_tags(f, child_end, budget, result)
            have_tags = True
        if have_info and have_tags:
            break
    if info_title:
        result.setdefault("title", info_title)


def read_mkv_tags(path: str) -> Dict[str, str]:
    """Best-effort read of title/artist/album/album_artist/date/genre
    from a Matroska file's Tags (and Info/Title) elements. Any value this
    can't find is simply absent from the returned dict -- callers already
    treat a missing key the same as "Unknown" (see read_track_meta()).
    Never raises: any parse failure just yields fewer (or no) tags."""
    result: Dict[str, str] = {}
    try:
        with open(path, "rb") as f:
            f.seek(0, io.SEEK_END)
            file_end = f.tell()
            f.seek(0)
            budget = [_MAX_ELEMENTS_SCANNED]
            for element_id, _size, child_end in _iter_children(f, file_end, budget):
                if element_id == _ID_SEGMENT:
                    _scan_segment(f, child_end, budget, result)
                    break
    except Exception:
        return {}
    return result
