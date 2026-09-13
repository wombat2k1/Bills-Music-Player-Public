import urllib.error
import urllib.request

import pytest

from billsmusic.media_server import LocalMediaServer, UnsupportedMediaError


@pytest.fixture
def server():
    value = LocalMediaServer(address_resolver=lambda: "127.0.0.1")
    yield value
    value.shutdown()


def _request(url, method="GET", headers=None):
    request = urllib.request.Request(url, method=method, headers=headers or {})
    return urllib.request.urlopen(request, timeout=2)


def test_authorised_get_head_and_mime(server, tmp_path):
    track = tmp_path / "song.mp3"
    track.write_bytes(b"0123456789")
    url = server.register_audio(track)
    with _request(url) as response:
        assert response.status == 200
        assert response.headers["Content-Type"] == "audio/mpeg"
        assert response.headers["Content-Length"] == "10"
        assert response.read() == b"0123456789"
    with _request(url, "HEAD") as response:
        assert response.status == 200
        assert response.headers["Content-Length"] == "10"
        assert response.read() == b""


def test_flac_mime_and_valid_range(server, tmp_path):
    track = tmp_path / "song.FLAC"
    track.write_bytes(b"0123456789")
    url = server.register_audio(track)
    with _request(url, headers={"Range": "bytes=2-5"}) as response:
        assert response.status == 206
        assert response.headers["Content-Type"] == "audio/flac"
        assert response.headers["Content-Range"] == "bytes 2-5/10"
        assert response.headers["Accept-Ranges"] == "bytes"
        assert response.read() == b"2345"


def test_invalid_range_token_traversal_and_directory_rejected(server, tmp_path):
    track = tmp_path / "song.mp3"
    track.write_bytes(b"abc")
    url = server.register_audio(track)
    with pytest.raises(urllib.error.HTTPError) as invalid:
        _request(url, headers={"Range": "bytes=99-100"})
    assert invalid.value.code == 416
    base = url.rsplit("/", 1)[0]
    for path in ("/invalid-token", "/../song.mp3", "/"):
        with pytest.raises(urllib.error.HTTPError) as rejected:
            _request(base + path)
        assert rejected.value.code == 404


def test_unsupported_format_and_clean_shutdown(server, tmp_path):
    track = tmp_path / "song.wav"
    track.write_bytes(b"wave")
    with pytest.raises(UnsupportedMediaError):
        server.register_audio(track)
    supported = tmp_path / "song.mp3"
    supported.write_bytes(b"audio")
    server.register_audio(supported)
    assert server.running
    server.shutdown()
    assert not server.running


# -- v1.0.69: closed vs. temporarily-stopped state --------------------------
#
# shutdown() (called from _on_cast_failed/_return_to_local_output/
# _force_local_output_for_video -- all "might Cast again this session")
# must keep letting register() bring the server back. close() (called
# once, only from _finalize_shutdown -- real application shutdown) must
# permanently refuse register()/register_audio()/start() from that point
# on, so a straggling CastPayloadWorker result (or anything else) cannot
# resurrect the HTTP server after shutdown has already begun.

def test_shutdown_then_register_still_works_normally(server, tmp_path):
    """Ordinary Cast reconnect during a live session must remain possible
    -- shutdown() alone is not a permanent close."""
    track = tmp_path / "song.mp3"
    track.write_bytes(b"first")
    server.register_audio(track)
    assert server.running
    server.shutdown()
    assert not server.running

    url = server.register_audio(track)
    assert server.running
    with _request(url) as response:
        assert response.status == 200
        assert response.read() == b"first"


def test_close_prevents_the_server_from_being_resurrected(server, tmp_path):
    track = tmp_path / "song.mp3"
    track.write_bytes(b"data")
    server.register_audio(track)
    assert server.running

    server.close()
    assert not server.running

    with pytest.raises(RuntimeError):
        server.register_audio(track)
    assert not server.running  # register_audio's start() must not have run

    server.start()  # a no-op once closed, consistent with "already running" -- never raises
    assert not server.running


def test_close_is_idempotent(server, tmp_path):
    track = tmp_path / "song.mp3"
    track.write_bytes(b"data")
    server.register_audio(track)
    server.close()
    server.close()  # must not raise a second time
    assert not server.running
    with pytest.raises(RuntimeError):
        server.register_audio(track)


def test_a_fresh_server_instance_after_close_works_normally(tmp_path):
    """close() is per-instance state, not global/module-level -- a brand
    new LocalMediaServer (matching a fresh application session) must be
    completely unaffected by an earlier instance having been closed."""
    old_server = LocalMediaServer(address_resolver=lambda: "127.0.0.1")
    old_server.close()
    assert old_server._closed

    new_server = LocalMediaServer(address_resolver=lambda: "127.0.0.1")
    try:
        track = tmp_path / "song.mp3"
        track.write_bytes(b"fresh")
        url = new_server.register_audio(track)
        assert new_server.running
        with _request(url) as response:
            assert response.status == 200
            assert response.read() == b"fresh"
    finally:
        new_server.shutdown()


def test_revoked_artwork_token_gets_a_fresh_valid_registration_on_replay(server, tmp_path):
    """v1.0.69 (Codex finding C): a cached Cast payload must never be
    replayed with a dead artwork URL after revoke_all() -- the fix is to
    re-register the *local file* fresh at replay time rather than cache
    the URL/token itself. This proves the mechanism at the MediaServer
    level: the same local file, registered again after a revoke, gets a
    genuinely different, independently resolvable token/URL, not a reuse
    of the dead one."""
    art = tmp_path / "cover.jpg"
    art.write_bytes(b"\xff\xd8\xff\xe0jpeg-bytes")
    first_url = server.register(art, "image/jpeg")
    with _request(first_url) as response:
        assert response.status == 200
        assert response.read() == b"\xff\xd8\xff\xe0jpeg-bytes"

    server.revoke_all()
    with pytest.raises(urllib.error.HTTPError) as revoked:
        _request(first_url)
    assert revoked.value.code == 404

    second_url = server.register(art, "image/jpeg")
    assert second_url != first_url  # a genuinely fresh token, not the dead one
    with _request(second_url) as response:
        assert response.status == 200
        assert response.read() == b"\xff\xd8\xff\xe0jpeg-bytes"

