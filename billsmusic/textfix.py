"""Small text cleanup helpers for music tags and lyric files."""
from typing import Any, Dict


_MOJIBAKE_MARKERS = (
    "Ã", "Â", "â€", "â€™", "â€œ", "â€�", "â€“", "â€”", "ðŸ", "ï¿½", "�",
)


def clean_text(value: Any) -> str:
    """Repair common UTF-8 text that was accidentally decoded as Windows text."""
    if value is None:
        return ""
    text = str(value)
    if not text or not any(marker in text for marker in _MOJIBAKE_MARKERS):
        return text
    try:
        repaired = text.encode("cp1252").decode("utf-8")
    except Exception:
        return text
    return repaired if repaired else text


def clean_meta_dict(meta: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = dict(meta)
    for key in ("album", "title", "artist", "album_artist", "genre", "key"):
        if key in cleaned:
            cleaned[key] = clean_text(cleaned[key])
    return cleaned
