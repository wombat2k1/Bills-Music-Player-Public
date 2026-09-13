"""Plex Stage 3A: the ephemeral, resolved-at-play-time STREAMING source
for one Plex media item -- distinct from the stable plex://<server_config_id>/
<rating_key> logical identity (plex_identity.py) everywhere else in this
app (current_path, queue, Recently Played, session persistence, caches).

Per the approved Decision 1 exception: for Plex VIDEO specifically, Qt
Multimedia's QMediaPlayer/QML MediaPlayer have no API to attach a custom
HTTP header to a plain QUrl source, so the X-Plex-Token must travel in
that transport URL's own query string. For Plex AUDIO, BASS_StreamCreateURL
accepts headers appended to its own url buffer (CRLF-separated), so the
token there travels as a real HTTP header, never in the URL text at all.

Either way, this module is the ONE place that both:
  (a) builds a PlexTransportSource, and
  (b) sanitises any text that might contain one, before it can reach a
      log line, diagnostic record, exception message, tooltip, or any
      other observable surface.

Dependency-free (no PyQt, no requests) so it can be imported anywhere,
including video_subprocess.py (a separate process) and bass_player.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional

# Matches "X-Plex-Token" (any case) followed by the usual ways a token
# value can be attached to it in the shapes this module actually
# produces or that a native error message might echo back:
#   query string:      ?X-Plex-Token=<secret>   or   &X-Plex-Token=<secret>
#   CRLF-embedded header (BASS's own convention): X-Plex-Token: <secret>
#   a bare "key=value"/"key: value" fragment inside arbitrary text
# The value pattern stops at the next query-string/header delimiter
# (&, whitespace, \r, \n, quote) so the rest of the surrounding text
# (other query params, the following header line, etc.) is preserved --
# sanitising must stay USEFUL for debugging, not just safe.
_PLEX_TOKEN_RE = re.compile(
    r"(?i)(X-Plex-Token)\s*[:=]\s*[^&\s\"'\r\n]+"
)


def sanitize_plex_text(text) -> str:
    """Redacts every X-Plex-Token occurrence (case-insensitive, query-
    string or header-line shaped) in `text`, leaving everything else
    untouched. Safe to call on ANY string -- a full transport URL, a raw
    exception message, a native Qt media-error string, an IPC payload
    about to be logged -- and safe/idempotent when no token is present.
    Never raises; a non-string input is coerced via str() first so a
    caller can pass an exception object directly."""
    if text is None:
        return ""
    return _PLEX_TOKEN_RE.sub(r"\1=<redacted>", str(text))


@dataclass(frozen=True)
class PlexTransportSource:
    """The resolved, ephemeral streaming source for one Plex media item.

    `identity` is the stable plex://... string -- this is what must be
    used everywhere else (current_path, queue, Recently Played, session
    persistence, diagnostics `path`/identity fields, artwork/cache keys).

    `transport_url` is what actually gets handed to the playback backend:
    - AUDIO (BASS): the plain https://.../library/parts/<key> URL, with
      NO token in it -- BASS_StreamCreateURL's headers argument
      (`extra_headers`, CRLF-joined) carries X-Plex-Token instead.
    - VIDEO (QMediaPlayer): the https://.../library/parts/<key> URL WITH
      ?X-Plex-Token=<token> appended -- the approved Decision 1 exception,
      since QMediaPlayer/QML MediaPlayer have no custom-header API for a
      QUrl source.

    Neither field is ever the right thing to log unsanitised -- see
    sanitize_plex_text -- and `transport_url`/`extra_headers` must never
    be: current_path/queue identity, persisted, Recently Played identity,
    written to config/session data, written to diagnostics, written to
    normal logs, shown in tooltips, used as an artwork/cache key, or
    included in subprocess argv (an IPC message body is fine; a command-
    line argument is not, since argv is visible to other processes on
    the same machine)."""

    identity: str
    transport_url: str
    extra_headers: Optional[Dict[str, str]] = None
    container: str = ""
    video_codec: str = ""
    audio_codec: str = ""

    def sanitized_transport_url(self) -> str:
        """The transport URL with any token redacted -- the ONLY form of
        this URL that may ever reach a log line, diagnostic, tooltip, or
        exception message."""
        return sanitize_plex_text(self.transport_url)

    def __repr__(self) -> str:
        # Defense in depth: even an accidental repr()/str() of this
        # object (a raw print(), an unguarded f-string in a future edit,
        # a debugger watch expression) must never leak the token.
        return (
            f"PlexTransportSource(identity={self.identity!r}, "
            f"transport_url={self.sanitized_transport_url()!r}, "
            f"extra_headers={'<redacted>' if self.extra_headers else None}, "
            f"container={self.container!r})"
        )

    __str__ = __repr__
