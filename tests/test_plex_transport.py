"""Stage 3A: the central Plex transport-URL sanitiser and
PlexTransportSource -- the token must not survive anywhere observable.
Hostile tests per the explicit spec: token in URL query, exception text,
a simulated failed Qt media error string, and a simulated parent/child
IPC diagnostic payload.
"""
from billsmusic.plex_transport import PlexTransportSource, sanitize_plex_text

REAL_SECRET = "sk-real-plex-token-do-not-leak-9f8e7d6c5b4a"


def test_sanitizes_token_in_url_query_string():
    url = f"https://1.2.3.4:32400/library/parts/5?X-Plex-Token={REAL_SECRET}&download=1"
    sanitized = sanitize_plex_text(url)
    assert REAL_SECRET not in sanitized
    assert "X-Plex-Token=<redacted>" in sanitized
    assert "download=1" in sanitized  # rest of the URL preserved


def test_sanitizes_token_in_query_string_lowercase_key():
    url = f"https://1.2.3.4:32400/library/parts/5?x-plex-token={REAL_SECRET}"
    sanitized = sanitize_plex_text(url)
    assert REAL_SECRET not in sanitized


def test_sanitizes_token_as_the_only_query_param():
    url = f"https://1.2.3.4:32400/library/parts/5?X-Plex-Token={REAL_SECRET}"
    sanitized = sanitize_plex_text(url)
    assert REAL_SECRET not in sanitized


def test_sanitizes_token_embedded_as_bass_crlf_header():
    url_and_header = f"https://1.2.3.4:32400/library/parts/5\r\nX-Plex-Token: {REAL_SECRET}\r\n"
    sanitized = sanitize_plex_text(url_and_header)
    assert REAL_SECRET not in sanitized


def test_sanitizes_token_inside_arbitrary_exception_text():
    # A native/Qt error message that happens to echo the failing URL back
    # verbatim -- the token must not survive being wrapped in prose.
    exc_text = (
        f"ConnectionError: failed to fetch "
        f"https://plex.example.com/library/parts/9?X-Plex-Token={REAL_SECRET} "
        f"after 3 retries"
    )
    sanitized = sanitize_plex_text(exc_text)
    assert REAL_SECRET not in sanitized


def test_sanitizes_token_inside_simulated_qt_media_error():
    # Real QMediaPlayer errorString()-shaped text.
    qt_error = (
        f'FFmpeg: Failed to open "https://plex.example.com/library/parts/9'
        f'?X-Plex-Token={REAL_SECRET}": Connection timed out'
    )
    sanitized = sanitize_plex_text(qt_error)
    assert REAL_SECRET not in sanitized


def test_sanitizes_token_inside_simulated_parent_child_ipc_diagnostic():
    # A diagnostic dict the video subprocess might report back to the
    # parent (e.g. a load-failure envelope carrying the source it tried).
    ipc_payload_text = str({
        "cmd": "load_failed",
        "source": f"https://plex.example.com/library/parts/9?X-Plex-Token={REAL_SECRET}",
        "reason": "timeout",
    })
    sanitized = sanitize_plex_text(ipc_payload_text)
    assert REAL_SECRET not in sanitized


def test_sanitize_is_a_noop_when_no_token_present():
    text = "https://plex.example.com/library/parts/9?download=1"
    assert sanitize_plex_text(text) == text


def test_sanitize_handles_none_and_non_string_input():
    assert sanitize_plex_text(None) == ""
    assert sanitize_plex_text(404) == "404"


def test_sanitize_handles_multiple_tokens_in_one_string():
    text = (
        f"first=https://a/p?X-Plex-Token={REAL_SECRET} "
        f"second=https://b/p?X-Plex-Token={REAL_SECRET}2"
    )
    sanitized = sanitize_plex_text(text)
    assert REAL_SECRET not in sanitized
    assert sanitized.count("<redacted>") == 2


# -- PlexTransportSource -----------------------------------------------

def test_transport_source_repr_never_contains_the_real_token():
    source = PlexTransportSource(
        identity="plex://server-1/42.mp3",
        transport_url=f"https://h/p?X-Plex-Token={REAL_SECRET}",
        extra_headers={"X-Plex-Token": REAL_SECRET},
    )
    assert REAL_SECRET not in repr(source)
    assert REAL_SECRET not in str(source)
    # extra_headers itself is never shown at all, redacted wholesale --
    # not just token-scrubbed -- since headers may carry other Plex
    # request metadata not worth exposing either.
    assert "<redacted>" in repr(source)


def test_transport_source_sanitized_transport_url_helper():
    source = PlexTransportSource(
        identity="plex://server-1/42.mp3",
        transport_url=f"https://h/p?X-Plex-Token={REAL_SECRET}&other=1",
    )
    sanitized = source.sanitized_transport_url()
    assert REAL_SECRET not in sanitized
    assert "other=1" in sanitized


def test_transport_source_identity_is_never_touched_by_sanitizer():
    # The logical identity has no token in it by construction -- prove
    # sanitizing doesn't mangle it if ever (incorrectly) run through the
    # same function.
    identity = "plex://server-1/42.mp3"
    assert sanitize_plex_text(identity) == identity


def test_transport_source_with_no_extra_headers_reprs_as_none():
    source = PlexTransportSource(
        identity="plex://server-1/42.mp3", transport_url="https://h/p",
    )
    assert "extra_headers=None" in repr(source)
