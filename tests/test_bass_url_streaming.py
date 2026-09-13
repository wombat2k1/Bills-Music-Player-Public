"""Stage 3A format audit, made permanent and reproducible: proves (not
assumes) that BASS_StreamCreateURL, as bound in bass_player.py, actually
Direct Plays MP3/FLAC/WAV/OGG over a real local HTTP server, delivers a
custom X-Plex-Token as a genuine HTTP header (never in the URL text),
and that ordinary local (BASS_StreamCreateFile) playback is completely
unaffected. Runs against the real vendored bass.dll -- no mocking of
BASS itself, matching this project's own established practice of never
faking native audio verification.

Skips entirely (not a failure) if bass.dll isn't present in this
environment/build.
"""
import http.server
import json
import os
import socketserver
import threading
import time

import pytest

from billsmusic.bass_player import BassLoadError, BassPlayer, _BassEngine
from billsmusic.plex_transport import PlexTransportSource

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
_DLL_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "vendor", "bass", "bin", "x64", "bass.dll"),
]

pytestmark = pytest.mark.skipif(
    not any(os.path.isfile(p) for p in _DLL_CANDIDATES),
    reason="bass.dll not present in this environment",
)


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    received_headers = []
    _lock = threading.Lock()

    def _record(self):
        with self._lock:
            type(self).received_headers.append(
                {"path": self.path, "headers": dict(self.headers.items())}
            )

    def do_GET(self):
        self._record()
        filename = self.path.lstrip("/").split("?")[0]
        filepath = os.path.join(FIXTURES_DIR, filename)
        if not os.path.isfile(filepath):
            self.send_response(404)
            self.end_headers()
            return
        file_size = os.path.getsize(filepath)
        range_header = self.headers.get("Range")
        with open(filepath, "rb") as f:
            if range_header:
                start, _, end = range_header.replace("bytes=", "").partition("-")
                start = int(start) if start else 0
                end = int(end) if end else file_size - 1
                end = min(end, file_size - 1)
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                self.end_headers()
                f.seek(start)
                self.wfile.write(f.read(length))
            else:
                self.send_response(200)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(file_size))
                self.end_headers()
                self.wfile.write(f.read())

    def log_message(self, format, *args):
        pass


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


@pytest.fixture(scope="module")
def fixture_server():
    _RangeHandler.received_headers = []
    server = _ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def _clear_request_log():
    _RangeHandler.received_headers = []
    yield


@pytest.mark.parametrize("filename", ["sample.mp3", "sample.flac", "sample.wav", "sample.ogg"])
def test_direct_play_format_over_http(fixture_server, filename):
    player = BassPlayer()
    source = PlexTransportSource(
        identity=f"plex://server-1/{filename}",
        transport_url=f"{fixture_server}/{filename}",
    )
    try:
        player.load(source)
        player.play()
        deadline = time.monotonic() + 3.0
        while not player.is_playing() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert player.is_playing(), f"{filename} never started playing"
        assert player.get_length() > 0
    finally:
        player.stop()


def test_custom_header_delivered_and_token_never_in_url(fixture_server):
    player = BassPlayer()
    source = PlexTransportSource(
        identity="plex://server-1/1.mp3",
        transport_url=f"{fixture_server}/sample.mp3",
        extra_headers={"X-Plex-Token": "REAL-SECRET-ABC123"},
    )
    try:
        player.load(source)
        player.play()
        time.sleep(0.3)
    finally:
        player.stop()

    matching = [
        entry for entry in _RangeHandler.received_headers
        if "REAL-SECRET-ABC123" not in entry["path"]
        and {k.lower(): v for k, v in entry["headers"].items()}.get("x-plex-token") == "REAL-SECRET-ABC123"
    ]
    assert matching, f"token header never observed by server: {_RangeHandler.received_headers}"
    for entry in _RangeHandler.received_headers:
        assert "REAL-SECRET-ABC123" not in entry["path"]  # never leaked into the URL/query


def test_seek_pause_resume_stop_lifecycle(fixture_server):
    player = BassPlayer()
    source = PlexTransportSource(
        identity="plex://server-1/1.wav", transport_url=f"{fixture_server}/sample.wav",
    )
    player.load(source)
    player.play()
    deadline = time.monotonic() + 3.0
    while not player.is_playing() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert player.is_playing()

    length = player.get_length()
    assert length > 1.0
    player.seek(0.5)
    time.sleep(0.1)
    assert 0.3 <= player.get_pos() <= 1.2

    player.pause()
    assert not player.is_playing()
    player.resume()
    time.sleep(0.1)
    assert player.is_playing()

    player.stop()
    assert not player.is_playing()
    assert player._stream == 0
    assert player._url_buffer is None  # buffer released once the stream is freed


def test_repeated_load_cleans_up_previous_stream(fixture_server):
    player = BassPlayer()
    for filename in ("sample.mp3", "sample.wav", "sample.mp3"):
        source = PlexTransportSource(
            identity=f"plex://server-1/{filename}", transport_url=f"{fixture_server}/{filename}",
        )
        player.load(source)
        player.play()
        time.sleep(0.1)
    player.stop()
    assert player._stream == 0


def test_load_failure_is_a_clean_bassloaderror(fixture_server):
    player = BassPlayer()
    source = PlexTransportSource(
        identity="plex://server-1/missing.mp3",
        transport_url=f"{fixture_server}/does-not-exist.mp3",
    )
    with pytest.raises(BassLoadError):
        player.load(source)
    assert player._stream == 0


def test_logical_identity_used_for_path_never_the_transport_url(fixture_server):
    player = BassPlayer()
    source = PlexTransportSource(
        identity="plex://server-1/99.mp3",
        transport_url=f"{fixture_server}/sample.mp3?X-Plex-Token=SHOULD-NOT-APPEAR",
        extra_headers={"X-Plex-Token": "SHOULD-NOT-APPEAR"},
    )
    try:
        player.load(source)
        stats = player.stats()
        assert stats["path"] == "plex://server-1/99.mp3"
        assert "SHOULD-NOT-APPEAR" not in repr(stats)
    finally:
        player.stop()


def test_local_file_playback_completely_unaffected(fixture_server):
    player = BassPlayer()
    local_path = os.path.join(FIXTURES_DIR, "sample.mp3")
    try:
        player.load(local_path)
        player.play()
        deadline = time.monotonic() + 3.0
        while not player.is_playing() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert player.is_playing()
        assert player._path == local_path
        assert player.get_length() > 0
    finally:
        player.stop()
