import os
import time

from billsmusic.library_search import (
    LIBRARY_APPLY_MAX_ALBUMS,
    LIBRARY_APPLY_TIME_BUDGET_SECONDS,
    build_search_index,
    filter_search_index,
    format_track_display_label,
    group_library_albums,
    group_search_results,
    library_apply_chunk_complete,
    parse_scoped_query,
)


def library():
    return [
        {
            "path": os.path.join("Music", "Fleetwood Mac", "Dreams.flac"),
            "title": "Dreams",
            "artist": "Fleetwood Mac",
            "album_artist": "Fleetwood Mac",
            "album": "Rumours",
            "genre": "Rock",
            "year": "1977",
        },
        {
            "path": os.path.join("Music", "Kate Bush", "Running Up That Hill.mp3"),
            "title": "Running Up That Hill",
            "artist": "Kate Bush",
            "album": "Hounds of Love",
        },
        {
            "path": os.path.join("Music", "Prince", "1999.wav"),
            "title": "1999",
            "artist": "Prince",
            "album": "1999",
        },
    ]


def paths(results):
    return [item["path"] for item in results]


def test_matching_is_case_insensitive_and_covers_core_fields():
    index = build_search_index(library())
    assert paths(filter_search_index(index, "DREAMS")) == [library()[0]["path"]]
    assert paths(filter_search_index(index, "fleetwood")) == [library()[0]["path"]]
    assert paths(filter_search_index(index, "rumours")) == [library()[0]["path"]]
    assert paths(filter_search_index(index, "running up that hill.mp3")) == [library()[1]["path"]]


def test_all_terms_can_match_across_different_fields():
    index = build_search_index(library())
    assert paths(filter_search_index(index, "fleetwood rumours")) == [library()[0]["path"]]
    assert filter_search_index(index, "fleetwood hounds") == []


def test_empty_query_returns_the_cached_library_in_stable_order():
    records = library()
    assert filter_search_index(build_search_index(records), "") == records


def test_filter_does_not_access_filesystem_or_tags(monkeypatch):
    index = build_search_index(library())

    def fail(*args, **kwargs):
        raise AssertionError("search attempted filesystem access")

    monkeypatch.setattr(os.path, "isfile", fail)
    assert len(filter_search_index(index, "rock 1977")) == 1


def test_index_replacement_removes_deleted_tracks():
    records = library()
    old_index = build_search_index(records)
    new_index = build_search_index(records[1:])
    assert len(filter_search_index(old_index, "dreams")) == 1
    assert filter_search_index(new_index, "dreams") == []


def test_artist_and_album_groups_come_from_matches_and_are_stable():
    matches = list(reversed(library()))
    artists, albums = group_search_results(matches)
    assert artists == ["Fleetwood Mac", "Kate Bush", "Prince"]
    assert albums == [
        ("Fleetwood Mac", "Rumours"),
        ("Kate Bush", "Hounds of Love"),
        ("Prince", "1999"),
    ]


def test_cancellation_is_checked_during_large_search():
    records = [
        {"path": f"C:/Music/{number}.flac", "title": f"Track {number}"}
        for number in range(1000)
    ]
    checks = 0

    def cancelled():
        nonlocal checks
        checks += 1
        return checks > 1

    assert filter_search_index(build_search_index(records), "track", cancelled) == []


def test_full_library_grouping_builds_direct_album_rows():
    rows = group_library_albums(library())
    assert len(rows) == 3
    assert all(len(row) == 4 for row in rows)
    assert {row[1] for row in rows} == {"Rumours", "Hounds of Love", "1999"}


def test_large_restore_requires_multiple_bounded_chunks():
    records = [
        {
            "path": f"C:/Music/Track {number}.flac",
            "title": f"Track {number}",
            "artist": f"Artist {number}",
            "album": f"Album {number}",
        }
        for number in range(1000)
    ]
    rows = group_library_albums(records)
    limit = LIBRARY_APPLY_MAX_ALBUMS
    chunks = [rows[offset:offset + limit] for offset in range(0, len(rows), limit)]
    assert len(chunks) == 5
    assert all(len(chunk) <= limit for chunk in chunks)


def test_smooth_restore_defaults_and_stop_conditions():
    assert LIBRARY_APPLY_MAX_ALBUMS == 200
    assert LIBRARY_APPLY_TIME_BUDGET_SECONDS == 0.010
    assert not library_apply_chunk_complete(199, 0.009)
    assert library_apply_chunk_complete(200, 0.001)
    assert library_apply_chunk_complete(10, 0.010)


def test_track_label_uses_only_cached_lyrics_flag(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("track label attempted filesystem access")

    monkeypatch.setattr(os.path, "isfile", fail)
    assert format_track_display_label(1, "Dreams", "Fleetwood Mac", True).endswith(
        "\U0001f3a4"
    )
    assert "\U0001f3a4" not in format_track_display_label(
        1, "Dreams", "Fleetwood Mac", False
    )
    assert "\U0001f3a4" not in format_track_display_label(
        1, "Dreams", "Fleetwood Mac"
    )


def test_search_results_retain_cached_lyrics_fields():
    record = {
        "path": "C:/Music/Dreams.flac",
        "title": "Dreams",
        "artist": "Fleetwood Mac",
        "has_lrc_sidecar": True,
        "has_embedded_synced_lyrics": False,
        "has_synced_lyrics": True,
    }
    result = filter_search_index(build_search_index([record]), "dreams")
    assert result == [record]
    assert result[0]["has_synced_lyrics"] is True


def test_20k_record_development_benchmark(capsys):
    records = [
        {
            "path": f"C:/Music/Artist {number % 500}/Track {number}.flac",
            "title": f"Track {number}",
            "artist": f"Artist {number % 500}",
            "album": f"Album {number % 1000}",
        }
        for number in range(20_000)
    ]
    started = time.perf_counter()
    index = build_search_index(records)
    indexed_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    one_word = filter_search_index(index, "artist 42")
    searched_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    groups = group_search_results(one_word)
    grouped_ms = (time.perf_counter() - started) * 1000
    print(
        f"20k index={indexed_ms:.1f}ms search={searched_ms:.1f}ms "
        f"group={grouped_ms:.1f}ms artists={len(groups[0])}"
    )
    assert len(index) == 20_000


# -- scoped bracket-tag search ------------------------------------------

def scoped_library():
    return [
        {
            "path": os.path.join("Music", "Compilations", "Dreams.flac"),
            "title": "Dreams",
            "artist": "Fleetwood Mac",
            "album_artist": "Various Artists",
            "album": "Rumours",
            "genre": "Rock",
            "year": "1977-02-04",
            "bpm": "120",
            "key": "Fm",
        },
        {
            "path": os.path.join("Music", "Prince", "1999.wav"),
            "title": "1999",
            "artist": "Prince",
            "album_artist": "Prince",
            "album": "1999",
            "genre": "Pop",
            "year": "1982",
            "bpm": "120",
            "key": "Fm",
        },
    ]


def test_parse_scoped_query_maps_every_known_tag():
    assert parse_scoped_query("[track] billie jean") == (("title",), "billie jean")
    assert parse_scoped_query("[artist] queen") == (("artist", "album_artist"), "queen")
    assert parse_scoped_query("[album] thriller") == (("album",), "thriller")
    assert parse_scoped_query("[genre] rock") == (("genre",), "rock")
    assert parse_scoped_query("[year] 1985") == (("year",), "1985")
    assert parse_scoped_query("[bpm] 128") == (("bpm",), "128")
    assert parse_scoped_query("[key] am") == (("key",), "am")


def test_parse_scoped_query_is_case_insensitive_and_whitespace_tolerant():
    assert parse_scoped_query("[TRACK] Dreams") == (("title",), "dreams")
    assert parse_scoped_query("  [ Artist ]   fleetwood  ") == (("artist", "album_artist"), "fleetwood")


def test_parse_scoped_query_falls_back_to_general_for_unknown_tag():
    fields, remainder = parse_scoped_query("[foo] bar")
    assert fields is None
    assert remainder == "[foo] bar"


def test_parse_scoped_query_with_no_bracket_is_general():
    fields, remainder = parse_scoped_query("just a query")
    assert fields is None
    assert remainder == "just a query"


def test_track_scope_matches_title_only():
    index = build_search_index(scoped_library())
    assert paths(filter_search_index(index, "[track] dreams")) == [scoped_library()[0]["path"]]
    # "rumours" only appears in album, not title -- must not match scoped.
    assert filter_search_index(index, "[track] rumours") == []


def test_artist_scope_matches_artist_and_album_artist():
    index = build_search_index(scoped_library())
    # "Fleetwood Mac" is the artist field; "Various Artists" is album_artist.
    assert paths(filter_search_index(index, "[artist] fleetwood")) == [scoped_library()[0]["path"]]
    assert paths(filter_search_index(index, "[artist] various")) == [scoped_library()[0]["path"]]


def test_album_scope_does_not_match_artist_text():
    index = build_search_index(scoped_library())
    assert filter_search_index(index, "[album] fleetwood") == []
    assert paths(filter_search_index(index, "[album] rumours")) == [scoped_library()[0]["path"]]


def test_genre_year_bpm_key_scopes():
    index = build_search_index(scoped_library())
    assert paths(filter_search_index(index, "[genre] rock")) == [scoped_library()[0]["path"]]
    assert paths(filter_search_index(index, "[year] 1977")) == [scoped_library()[0]["path"]]
    assert paths(filter_search_index(index, "[year] 1982")) == [scoped_library()[1]["path"]]
    # both fixture records share bpm/key -- scoped search should return both.
    assert len(filter_search_index(index, "[bpm] 120")) == 2
    assert len(filter_search_index(index, "[key] fm")) == 2


def test_scope_with_empty_remainder_returns_full_library():
    index = build_search_index(scoped_library())
    assert filter_search_index(index, "[track]") == scoped_library()


def test_scoped_search_does_not_touch_filesystem(monkeypatch):
    index = build_search_index(scoped_library())

    def fail(*args, **kwargs):
        raise AssertionError("scoped search attempted filesystem access")

    monkeypatch.setattr(os.path, "isfile", fail)
    assert len(filter_search_index(index, "[artist] prince")) == 1
