"""Stage 3A real-device bugs found during acceptance testing, all in
plex_track_to_meta_dict's conversion of one Plex music-track JSON item:

1. track_no/disc_no (item 6): the conversion wrote "track_number"/
   "disc_number" -- keys nothing downstream reads. group_library_albums
   and _track_display_label (window.py) both read "disc_no"/"track_no"
   exactly (the same keys the Local scanner's own _read_album_title_track
   already uses) -- the mismatch silently defaulted every Plex track to
   disc_no=1/track_no=0, indistinguishable from "unnumbered", so
   items.sort(key=lambda t: (t[0], t[1], t[2].lower())) fell through to
   alphabetical-by-title for every Plex album.

2. track artist vs album artist (item 7): artist preferred grandparentTitle
   (the album artist) over originalTitle (Plex's own track-artist override
   for compilation albums), so every track on a "Various Artists" album
   displayed as "Various Artists" even when Plex knew the real performer.

   Verified against the real Plex server (not assumed): a live, read-only
   scan of 8000 real tracks from the actual Music library found 362 with
   a non-empty originalTitle, every single one a genuine track-artist-
   override case -- "feat." collaborations (grandparentTitle="Sting",
   originalTitle="Sting feat. Vicente Amigo") and true various-artists
   remix compilations (grandparentTitle="Paul Oakenfold" -- the compiler
   -- originalTitle="The Cure"/"Justin Timberlake"/etc., the track's
   real performer). originalTitle was empty on every one of the other
   ~7638 (ordinary, non-collaboration) tracks scanned. The fixtures below
   mirror that real shape rather than an invented one.
"""
from billsmusic.media_capabilities import MediaType
from billsmusic.plex_metadata import plex_track_to_meta_dict


def _track_item(**overrides):
    item = {
        "ratingKey": "42", "title": "Song Title",
        "grandparentTitle": "The Artist", "parentTitle": "The Album",
        "parentYear": 2001, "index": 7, "parentIndex": 2,
        "duration": 210000,
        "Media": [{"container": "flac", "audioCodec": "flac", "bitrate": 900,
                    "Part": [{"key": "/library/parts/42/1/file.flac"}]}],
    }
    item.update(overrides)
    return item


def test_track_and_disc_number_use_the_keys_downstream_sorting_reads():
    meta = plex_track_to_meta_dict(_track_item(index=7, parentIndex=2), "server-1")
    assert meta["track_no"] == 7
    assert meta["disc_no"] == 2
    assert "track_number" not in meta
    assert "disc_number" not in meta


def test_track_and_disc_number_are_real_ints_not_strings():
    meta = plex_track_to_meta_dict(_track_item(index=7, parentIndex=2), "server-1")
    assert isinstance(meta["track_no"], int)
    assert isinstance(meta["disc_no"], int)


def test_missing_track_number_falls_back_to_zero_disc_to_one():
    item = _track_item()
    del item["index"]
    del item["parentIndex"]
    meta = plex_track_to_meta_dict(item, "server-1")
    assert meta["track_no"] == 0
    assert meta["disc_no"] == 1


def test_album_sort_order_uses_real_disc_and_track_numbers_not_alphabetical():
    # Reproduces the actual downstream consumer -- group_library_albums'
    # own sort key -- against three Plex-converted tracks whose titles
    # are deliberately in the OPPOSITE order from their real track
    # numbers, proving the fix isn't just about key presence but about
    # producing the correct real-world ordering.
    items = [
        plex_track_to_meta_dict(_track_item(ratingKey=str(n), title=t, index=n, parentIndex=1), "server-1")
        for n, t in [(3, "Zebra"), (1, "Apple"), (2, "Mango")]
    ]
    tuples = [(m["disc_no"], m["track_no"], m["title"]) for m in items]
    tuples.sort(key=lambda t: (t[0], t[1], t[2].lower()))
    assert [t[2] for t in tuples] == ["Apple", "Mango", "Zebra"]


def test_track_artist_preferred_over_album_artist_for_compilations():
    # Real shape, proven against the actual Plex server -- see this
    # file's own module docstring: a remix-compilation album whose
    # grandparentTitle is the compiler, not the track's real performer.
    meta = plex_track_to_meta_dict(
        _track_item(
            title="Close to Me (Closer mix)",
            grandparentTitle="Paul Oakenfold", parentTitle="Greatest Hits & Remixes",
            originalTitle="The Cure",
        ),
        "server-1",
    )
    assert meta["artist"] == "The Cure"
    assert meta["album_artist"] == "Paul Oakenfold"  # unchanged, still the true album artist


def test_track_artist_preferred_for_featuring_collaborations():
    # Also real-proven: a "feat." credit is likewise carried in
    # originalTitle, not grandparentTitle.
    meta = plex_track_to_meta_dict(
        _track_item(
            title="Send Your Love", grandparentTitle="Sting", parentTitle="Greatest Hits",
            originalTitle="Sting feat. Vicente Amigo",
        ),
        "server-1",
    )
    assert meta["artist"] == "Sting feat. Vicente Amigo"
    assert meta["album_artist"] == "Sting"


def test_ordinary_album_without_original_title_keeps_album_artist_as_track_artist():
    meta = plex_track_to_meta_dict(_track_item(grandparentTitle="The Artist"), "server-1")
    assert "originalTitle" not in _track_item()  # sanity: not present by default
    assert meta["artist"] == "The Artist"
    assert meta["album_artist"] == "The Artist"


def test_media_type_is_audio():
    meta = plex_track_to_meta_dict(_track_item(), "server-1")
    assert meta["media_type"] == MediaType.AUDIO.value
