"""Versioned, atomic persistence for the Up Next session."""
from datetime import datetime, timezone
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from .plex_identity import is_plex_identity

SESSION_VERSION = 1


class SessionError(ValueError):
    """Raised when saved session data cannot be safely restored."""


def session_document(
    queue: List[str], queue_played: List[bool], current_path: Optional[str]
) -> Dict[str, Any]:
    entries = []
    for index, path in enumerate(queue):
        if not isinstance(path, str):
            continue
        played = queue_played[index] if index < len(queue_played) else False
        entries.append({"path": path, "played": played if isinstance(played, bool) else False})
    return {
        "version": SESSION_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "queue": entries,
        "current_path": current_path if isinstance(current_path, str) else None,
    }


def save_session_file(
    filename: str, queue: List[str], queue_played: List[bool], current_path: Optional[str]
) -> None:
    """Write a complete session beside its destination, then atomically replace it."""
    folder = os.path.dirname(os.path.abspath(filename))
    os.makedirs(folder, exist_ok=True)
    temporary = filename + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(
                session_document(queue, queue_played, current_path),
                stream,
                ensure_ascii=False,
                indent=2,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, filename)
    finally:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass


def load_session_file(
    filename: str, validate_paths: bool = True
) -> Optional[Tuple[List[str], List[bool], Optional[str]]]:
    """Load a session, optionally checking track paths for non-startup callers."""
    if not os.path.exists(filename):
        return None
    try:
        with open(filename, "r", encoding="utf-8") as stream:
            document = json.load(stream)
    except Exception as ex:
        raise SessionError(f"could not read session: {ex}") from ex
    if not isinstance(document, dict):
        raise SessionError("session root is not an object")
    if document.get("version") != SESSION_VERSION:
        raise SessionError(f"unsupported session version: {document.get('version')!r}")

    records = document.get("queue")
    if not isinstance(records, list):
        records = []
    queue: List[str] = []
    played_flags: List[bool] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        path = record.get("path")
        # A plex:// identity is never validated with os.path.isfile (it
        # isn't a local file) and is never dropped just because Plex
        # wasn't reachable at startup -- see item 18: the row survives,
        # marked unavailable, not silently removed. Only a genuinely
        # missing LOCAL file is ever excluded here.
        if (
            not isinstance(path, str)
            or (validate_paths and not is_plex_identity(path) and not os.path.isfile(path))
        ):
            continue
        played = record.get("played")
        queue.append(path)
        played_flags.append(played if isinstance(played, bool) else False)

    current_path = document.get("current_path")
    if (
        not isinstance(current_path, str)
        or (
            validate_paths and not is_plex_identity(current_path)
            and not os.path.isfile(current_path)
        )
    ):
        current_path = None
    return queue, played_flags, current_path


def clear_session_file(filename: str) -> None:
    try:
        os.remove(filename)
    except FileNotFoundError:
        pass
