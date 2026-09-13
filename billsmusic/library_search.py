"""Pure, in-memory indexing and matching for the music library."""
from __future__ import annotations

import os
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SearchRecord = Tuple[Dict[str, Any], str]
LIBRARY_APPLY_MAX_ALBUMS = 200
LIBRARY_APPLY_TIME_BUDGET_SECONDS = 0.010

_SCOPE_FIELD_ALIASES: Dict[str, Tuple[str, ...]] = {
    "track": ("title",),
    "artist": ("artist", "album_artist"),
    "album": ("album",),
    "genre": ("genre",),
    "year": ("year",),
    "bpm": ("bpm",),
    "key": ("key",),
}
_SCOPE_TAG_PATTERN = re.compile(r"^\[\s*([a-z]+)\s*\]\s*(.*)$")


def library_apply_chunk_complete(
    rows_added: int,
    elapsed_seconds: float,
    row_limit: int = LIBRARY_APPLY_MAX_ALBUMS,
    time_budget: float = LIBRARY_APPLY_TIME_BUDGET_SECONDS,
) -> bool:
    return rows_added >= row_limit or elapsed_seconds >= time_budget


def format_track_display_label(
    track_no: int,
    title: str,
    artist: str,
    has_synced_lyrics: bool = False,
) -> str:
    title_text = f"{title} - {artist}" if artist else title
    label = f"{track_no:02d}. {title_text}" if track_no > 0 else title_text
    return label + " \U0001f3a4" if has_synced_lyrics else label


def normalise_search_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(text.split())


def parse_scoped_query(query: str) -> Tuple[Optional[Tuple[str, ...]], str]:
    """Split a search query into (field-names-to-match, remaining-text).

    Returns (None, normalised_query) when there is no recognised leading
    bracket tag -- including when the text merely looks like a tag but
    names an unrecognised scope (e.g. "[foo] bar" is treated as ordinary
    search text, unchanged).
    """
    normalised = normalise_search_text(query)
    match = _SCOPE_TAG_PATTERN.match(normalised)
    if not match:
        return None, normalised
    fields = _SCOPE_FIELD_ALIASES.get(match.group(1))
    if fields is None:
        return None, normalised
    return fields, match.group(2).strip()


def build_search_index(meta_list: Iterable[Dict[str, Any]]) -> List[SearchRecord]:
    records: List[SearchRecord] = []
    for meta in meta_list:
        path = str(meta.get("path") or "")
        fields = (
            meta.get("title"),
            meta.get("artist"),
            meta.get("album_artist"),
            meta.get("album"),
            meta.get("genre"),
            meta.get("year"),
            os.path.basename(path),
            path,
        )
        records.append((meta, normalise_search_text(" ".join(str(v or "") for v in fields))))
    return records


def query_terms(query: str) -> Tuple[str, ...]:
    return tuple(normalise_search_text(query).split())


def filter_search_index(
    index: Sequence[SearchRecord],
    query: str,
    cancelled=None,
) -> List[Dict[str, Any]]:
    fields, remainder = parse_scoped_query(query)
    terms = tuple(remainder.split()) if fields is not None else query_terms(query)
    if not terms:
        return [meta for meta, _ in index]
    matches: List[Dict[str, Any]] = []
    for position, (meta, search_text) in enumerate(index):
        if cancelled is not None and position % 256 == 0 and cancelled():
            return []
        if fields is None:
            haystack = search_text
        else:
            haystack = normalise_search_text(
                " ".join(str(meta.get(field) or "") for field in fields)
            )
        if all(term in haystack for term in terms):
            matches.append(meta)
    return matches


def group_search_results(
    matches: Sequence[Dict[str, Any]],
) -> Tuple[List[str], List[Tuple[str, str]]]:
    artists = {
        str(meta.get("artist") or "")
        for meta in matches
        if meta.get("artist")
    }
    albums = {
        (
            str(meta.get("album_artist") or meta.get("artist") or ""),
            str(meta.get("album") or ""),
        )
        for meta in matches
        if meta.get("album")
    }
    return (
        sorted(artists, key=str.casefold),
        sorted(albums, key=lambda value: (value[0].casefold(), value[1].casefold())),
    )


def group_library_albums(meta_list: Sequence[Dict[str, Any]]) -> List[tuple]:
    """Build the plain album rows consumed by the Qt tree insertion phase."""
    album_map: Dict[Tuple[str, str], list] = {}
    for meta in meta_list:
        album = meta.get("album", "Unknown Album")
        title = meta.get("title") or os.path.basename(meta.get("path", ""))
        artist = meta.get("artist", "Unknown Artist")
        album_artist = meta.get("album_artist", "Unknown Artist")
        path = meta.get("path")
        if not path:
            continue
        group_artist = (
            album_artist
            if album_artist and album_artist != "Unknown Artist"
            else artist
        )
        album_map.setdefault((album, group_artist), []).append(
            (
                meta.get("disc_no", 1),
                meta.get("track_no", 0),
                title,
                artist,
                album_artist,
                path,
            )
        )

    entries = []
    for (album, group_artist), items in album_map.items():
        artists = {
            artist.strip()
            for _, _, _, artist, _, _ in items
            if artist and artist not in ("Unknown Artist", "Various Artists")
        }
        album_artists = {
            artist.strip()
            for _, _, _, _, artist, _ in items
            if artist and artist not in ("Unknown Artist", "Various Artists")
        }
        display_artist = ""
        if len(album_artists) == 1:
            display_artist = next(iter(album_artists))
        elif len(artists) == 1:
            display_artist = next(iter(artists))
        label = group_row_label(display_artist, album)
        entries.append((label.casefold(), album, display_artist, items))
    return entries


def group_row_label(display_artist: str, album: str) -> str:
    """The top-level tree row label for one (artist, album) group.
    "Artist - Album" when both are known; when there's no album (e.g. a
    Plex video with no genuine parentTitle -- see plex_metadata.py's
    plex_video_to_meta_dict), just the artist, never a trailing " - "
    with nothing after it; when there's no artist either, just the
    album. This is what turns an empty album into "group by artist
    alone" (Artist -> Video) rather than a stray "Artist - " label."""
    if display_artist and album:
        return f"{display_artist} - {album}"
    return display_artist or album
