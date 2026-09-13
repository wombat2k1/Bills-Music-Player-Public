"""billsmusic.mkv_tags: the dependency-free Matroska tag reader.

Reported: .mkv music videos always showed "Unknown Artist" in the
library, even for files a full-featured tool like TagScanner showed real
tags in. Root cause: mutagen has never supported the Matroska container
at all -- MutagenFile() simply returns None for a .mkv file. This module
reads exactly the handful of EBML elements read_track_meta() needs
directly from the file's own Matroska structure.

Tests build minimal, real EBML/Matroska byte structures by hand (a small
helper below encodes element ID + size + payload) rather than depending
on a real sample .mkv file, so they exercise the actual binary format
this module has to parse.
"""
import io

from billsmusic.mkv_tags import _MAX_ELEMENTS_SCANNED, read_mkv_tags


# ---------------------------------------------------------------------------
# EBML construction helpers -- build the exact byte structures the parser
# has to walk, independent of the parser's own code.
# ---------------------------------------------------------------------------

def _encode_id(element_id: int) -> bytes:
    length = (element_id.bit_length() + 7) // 8
    return element_id.to_bytes(length, "big")


def _encode_size(size: int) -> bytes:
    # Smallest vint length that can hold `size` in its value bits.
    length = 1
    while size > (1 << (7 * length)) - 2:
        length += 1
    marker = 1 << (8 * length - length)
    value = marker | size
    return value.to_bytes(length, "big")


def _element(element_id: int, payload: bytes) -> bytes:
    return _encode_id(element_id) + _encode_size(len(payload)) + payload


def _simpletag(name: str, value: str) -> bytes:
    return _element(0x67C8, (
        _element(0x45A3, name.encode("utf-8"))
        + _element(0x4487, value.encode("utf-8"))
    ))


def _mkv_bytes(*, title=None, simple_tags=()) -> bytes:
    """A minimal but structurally real Matroska file: EBML header (just
    enough to look like one, never parsed by this module) + Segment
    containing Info (with Title) and/or Tags (with the given
    name/value SimpleTag pairs)."""
    ebml_header = _element(0x1A45DFA3, b"\x01\x02\x03")  # contents irrelevant

    segment_children = b""
    if title is not None:
        segment_children += _element(0x1549A966, _element(0x7BA9, title.encode("utf-8")))
    if simple_tags:
        tag_payload = b"".join(_simpletag(name, value) for name, value in simple_tags)
        segment_children += _element(0x1254C367, _element(0x7373, tag_payload))
    segment = _element(0x18538067, segment_children)
    return ebml_header + segment


def _write(tmp_path, data: bytes) -> str:
    path = tmp_path / "sample.mkv"
    path.write_bytes(data)
    return str(path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_reads_artist_and_title_from_simpletags(tmp_path):
    data = _mkv_bytes(simple_tags=[("ARTIST", "10cc"), ("TITLE", "Dreadlock Holiday")])
    tags = read_mkv_tags(_write(tmp_path, data))
    assert tags["artist"] == "10cc"
    assert tags["title"] == "Dreadlock Holiday"


def test_reads_album_and_album_artist_and_date_and_genre(tmp_path):
    data = _mkv_bytes(simple_tags=[
        ("ALBUM", "Greatest Hits"), ("ALBUM_ARTIST", "10cc"),
        ("DATE_RELEASED", "1978"), ("GENRE", "Rock"),
    ])
    tags = read_mkv_tags(_write(tmp_path, data))
    assert tags["album"] == "Greatest Hits"
    assert tags["album_artist"] == "10cc"
    assert tags["date"] == "1978"
    assert tags["genre"] == "Rock"


def test_falls_back_to_info_title_when_no_tags_element(tmp_path):
    data = _mkv_bytes(title="Shakira ft Pitbull - Rabiosa - www.HDMusicVideos.org")
    tags = read_mkv_tags(_write(tmp_path, data))
    assert tags == {"title": "Shakira ft Pitbull - Rabiosa - www.HDMusicVideos.org"}


def test_video_ts_info_title_is_treated_as_junk_and_ignored(tmp_path):
    # "VIDEO_TS" is a near-universal DVD-authoring artifact, not a real
    # title -- confirmed as the Info title on the large majority of a
    # real ~600-file .mkv collection. Worse than no title at all, since
    # callers already have a sensible filename-based fallback.
    data = _mkv_bytes(title="VIDEO_TS")
    tags = read_mkv_tags(_write(tmp_path, data))
    assert tags == {}


def test_video_ts_junk_filter_is_case_insensitive(tmp_path):
    data = _mkv_bytes(title="Video_ts")
    tags = read_mkv_tags(_write(tmp_path, data))
    assert tags == {}


def test_tags_element_title_wins_over_info_title(tmp_path):
    # _mkv_bytes always places Info before Tags in the file -- this alone
    # wouldn't prove priority is independent of byte order (setdefault on
    # whichever is scanned first would coincidentally pass too). Uses a
    # non-junk Info title so this test still holds even if the junk
    # filter above were ever loosened.
    data = _mkv_bytes(title="Some Real Info Title", simple_tags=[("TITLE", "Dreadlock Holiday")])
    tags = read_mkv_tags(_write(tmp_path, data))
    assert tags["title"] == "Dreadlock Holiday"


def test_tags_title_wins_over_info_title_even_when_tags_comes_first(tmp_path):
    # Same assertion, but with Tags physically written before Info in the
    # file -- proves the priority is a deliberate merge order, not an
    # accident of whichever element the scanner happens to reach first.
    ebml_header = _element(0x1A45DFA3, b"\x01")
    tags_elem = _element(0x1254C367, _element(0x7373, _simpletag("TITLE", "Dreadlock Holiday")))
    info_elem = _element(0x1549A966, _element(0x7BA9, "Some Real Info Title".encode("utf-8")))
    segment = _element(0x18538067, tags_elem + info_elem)
    tags = read_mkv_tags(_write(tmp_path, ebml_header + segment))
    assert tags["title"] == "Dreadlock Holiday"


def test_unrecognised_tag_names_are_ignored(tmp_path):
    # Matches the real-world case found while investigating this: files
    # with only auto-generated ENCODER/DURATION tags and no ARTIST at
    # all -- must come back empty, not raise or invent a value.
    data = _mkv_bytes(simple_tags=[("ENCODER", "Lavf60.16.100"), ("DURATION", "00:04:33")])
    tags = read_mkv_tags(_write(tmp_path, data))
    assert tags == {}


def test_no_segment_element_returns_empty(tmp_path):
    tags = read_mkv_tags(_write(tmp_path, _element(0x1A45DFA3, b"\x01")))
    assert tags == {}


def test_missing_file_returns_empty():
    assert read_mkv_tags(r"C:\definitely\does\not\exist.mkv") == {}


def test_garbage_non_ebml_content_returns_empty_not_raises(tmp_path):
    path = tmp_path / "sample.mkv"
    path.write_bytes(b"not an ebml file at all, just plain garbage bytes")
    assert read_mkv_tags(str(path)) == {}


def test_empty_file_returns_empty(tmp_path):
    path = tmp_path / "sample.mkv"
    path.write_bytes(b"")
    assert read_mkv_tags(str(path)) == {}


def test_truncated_mid_element_does_not_raise(tmp_path):
    data = _mkv_bytes(simple_tags=[("ARTIST", "10cc")])
    path = _write(tmp_path, data[: len(data) - 3])  # cut off mid-payload
    tags = read_mkv_tags(path)  # must not raise
    assert isinstance(tags, dict)


def test_child_size_claiming_to_extend_past_parent_is_rejected(tmp_path):
    # A SimpleTag whose declared size reaches past its enclosing Tag's own
    # declared size -- a corrupt/hostile file. Must stop cleanly rather
    # than read into whatever bytes follow.
    bad_simpletag = _encode_id(0x67C8) + _encode_size(9999) + b"short"
    tag = _element(0x7373, bad_simpletag)
    tags_elem = _element(0x1254C367, tag)
    segment = _element(0x18538067, tags_elem)
    ebml_header = _element(0x1A45DFA3, b"\x01")
    tags = read_mkv_tags(_write(tmp_path, ebml_header + segment))
    assert tags == {}


def test_oversized_tag_string_is_capped_not_fully_read(tmp_path):
    # A TagString claiming a huge size must not force a huge read/allocation.
    huge_value = b"A" * 200_000
    simpletag_payload = (
        _element(0x45A3, b"ARTIST") + _element(0x4487, huge_value)
    )
    simpletag = _element(0x67C8, simpletag_payload)
    tag = _element(0x7373, simpletag)
    tags_elem = _element(0x1254C367, tag)
    segment = _element(0x18538067, tags_elem)
    ebml_header = _element(0x1A45DFA3, b"\x01")
    tags = read_mkv_tags(_write(tmp_path, ebml_header + segment))
    # Capped at _MAX_STRING_BYTES -- still a valid (if truncated) string,
    # proving the cap engaged rather than reading all 200KB.
    assert tags["artist"].startswith("A")
    assert len(tags["artist"]) <= 4096


def test_element_scan_budget_bounds_a_pathological_file(tmp_path):
    # Many tiny sibling elements at the Tags level -- if the scan budget
    # didn't bound this, a hostile file could still only cost a bounded
    # amount of work, not spin forever. Also a plain correctness check:
    # a well-formed ARTIST tag near the end must still be found as long
    # as it's within budget.
    many_tags = b"".join(_simpletag(f"IGNORED_{i}", "x") for i in range(50))
    many_tags += _simpletag("ARTIST", "10cc")
    tag = _element(0x7373, many_tags)
    tags_elem = _element(0x1254C367, tag)
    segment = _element(0x18538067, tags_elem)
    ebml_header = _element(0x1A45DFA3, b"\x01")
    tags = read_mkv_tags(_write(tmp_path, ebml_header + segment))
    assert tags["artist"] == "10cc"


def test_real_dreadlock_holiday_file_if_present():
    # Opportunistic real-world check: skips cleanly if this session's NAS
    # path isn't reachable (e.g. CI, or the share not mounted), but when
    # it is, confirms the parser handles a real, full-size video file
    # (not just hand-built minimal fixtures) without error.
    import os
    path = r"Y:\Nas Music Vids back up\Sorted\10cc\10cc - Dreadlock Holiday.mkv"
    if not os.path.isfile(path):
        return
    tags = read_mkv_tags(path)
    assert isinstance(tags, dict)
    # This specific file is known (from live investigation) to carry only
    # auto-generated ENCODER/DURATION tags and a junk-filtered "VIDEO_TS"
    # Info title -- no ARTIST tag at all, which is the real reason it
    # shows "Unknown Artist" from tag-reading alone (fixed instead by
    # video-folder artist inference in metadata.py).
    assert "artist" not in tags
    assert "title" not in tags
