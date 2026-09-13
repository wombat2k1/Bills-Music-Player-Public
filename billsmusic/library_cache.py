"""Stable per-file fingerprints used by incremental library rescans."""
from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, List, Mapping

from .media_type import classify_path

LIBRARY_CACHE_SCHEMA_VERSION = 3


def audio_fingerprint_matches(
    previous: Mapping[str, Any], current: Mapping[str, Any],
) -> bool:
    return (
        previous.get("size") == current.get("size")
        and previous.get("mtime_ns") == current.get("mtime_ns")
        and previous.get("companion_size") == current.get("companion_size")
        and previous.get("companion_mtime_ns") == current.get("companion_mtime_ns")
    )


def lrc_fingerprint_matches(
    previous: Mapping[str, Any], current: Mapping[str, Any],
) -> bool:
    return (
        previous.get("lrc_size") == current.get("lrc_size")
        and previous.get("lrc_mtime_ns") == current.get("lrc_mtime_ns")
    )


def stable_folder_signature(
    folder: str, fingerprints: Mapping[str, Mapping[str, Any]],
) -> str:
    digest = hashlib.blake2b(digest_size=20)
    folder_key = str(folder).replace("\\", "/").casefold().rstrip("/")
    for path in sorted(fingerprints, key=lambda value: value.casefold()):
        fingerprint = fingerprints[path]
        path_key = str(path).replace("\\", "/").casefold()
        if path_key.startswith(folder_key + "/"):
            path_key = path_key[len(folder_key) + 1:]
        values = (
            path_key,
            fingerprint.get("size"),
            fingerprint.get("mtime_ns"),
            fingerprint.get("lrc_size"),
            fingerprint.get("lrc_mtime_ns"),
            fingerprint.get("companion_size"),
            fingerprint.get("companion_mtime_ns"),
        )
        digest.update(("\0".join(str(value) for value in values) + "\n").encode("utf-8"))
    return digest.hexdigest()


def dedupe_meta_list_by_path(meta_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop duplicate track records that refer to the same file.

    Overlapping library folders (the same folder added twice, or one added
    folder nested inside another already-scanned one) can cause a track to
    be scanned into more than one metadata record. Nothing upstream checks
    for this, so without a final pass here the duplicate gets persisted to
    the cache and reproduces itself on every later scan. Keeps the first
    record per normalized path.
    """
    seen = set()
    deduped: List[Dict[str, Any]] = []
    for meta in meta_list:
        path = meta.get("path") if isinstance(meta, dict) else None
        if not path:
            deduped.append(meta)
            continue
        key = os.path.normcase(os.path.normpath(str(path)))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(meta)
    return deduped


def migrate_cache_record(meta: Mapping[str, Any]) -> Dict[str, Any]:
    """Return `meta` with `media_type` filled in when a v2 cache lacks it.

    Existing records already carry a `media_type` once written by a current
    scan; this only backfills older (schema v2) records by inferring the
    type from the path, without touching anything else in the record and
    without forcing a rescan. The caller persists the backfilled value on
    the next natural cache save.
    """
    if isinstance(meta, dict) and "media_type" in meta:
        return meta
    updated = dict(meta)
    updated["media_type"] = classify_path(str(updated.get("path", ""))).value
    return updated


def migrate_cache_meta_list(meta_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [migrate_cache_record(meta) for meta in meta_list]


def metadata_with_lrc_fingerprint(
    metadata: Mapping[str, Any], fingerprint: Mapping[str, Any],
) -> Dict[str, Any]:
    updated = dict(metadata)
    has_lrc = fingerprint.get("lrc_mtime_ns") is not None
    updated["has_lrc_sidecar"] = has_lrc
    updated["has_embedded_synced_lyrics"] = bool(
        updated.get("has_embedded_synced_lyrics")
    )
    updated["has_synced_lyrics"] = bool(
        has_lrc or updated["has_embedded_synced_lyrics"]
    )
    return updated
