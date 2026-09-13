"""Stage 3A: QtVideoPlaybackBackend.load(identity, transport_url=...) driven
against a real local HTTP server serving the real sample.mp4 fixture, through
the actual video_subprocess.py child process -- not mocked. Proves (not
assumes) that a plex:// logical identity paired with a remote transport_url
genuinely plays through Qt's real FFmpeg-backed pipeline, that a token
embedded in the transport URL's query string is delivered to the server (the
Decision 1 exception) and never appears in any diagnostic this process
records, and that local (transport_url=None) playback is completely
unaffected by this change.

Skips gracefully if the video fixture is missing, matching
test_video_backend_fixture_integration.py's own convention.
"""
import http.server
import os
import socketserver
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtWidgets

from billsmusic.video_backend import QtVideoPlaybackBackend

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample.mp4")

pytestmark = pytest.mark.skipif(not os.path.isfile(_FIXTURE), reason="video fixture not present")

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _pump_until(predicate, seconds=10.0):
    app = _app()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    received_paths = []
    _lock = threading.Lock()

    def do_GET(self):
        with self._lock:
            type(self).received_paths.append(self.path)
        filepath = _FIXTURE
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


@pytest.fixture
def fixture_server():
    _RecordingHandler.received_paths = []
    server = _ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/sample.mp4"
    server.shutdown()
    server.server_close()


def test_plex_video_transport_url_plays_over_real_http(fixture_server):
    _app()
    backend = QtVideoPlaybackBackend()
    started = []
    backend.started.connect(lambda: started.append(True))

    identity = "plex://server-1/999.mp4"
    result = backend.load(identity, transport_url=fixture_server)
    assert result is True

    assert _pump_until(lambda: bool(started)), "Plex transport video never reported started"
    assert _pump_until(lambda: backend.duration_ms() > 0), "duration never became known"

    backend.shutdown()


def test_plex_video_token_in_query_reaches_server_never_in_diagnostics(fixture_server, tmp_path):
    from billsmusic.performance_diagnostics import get_diagnostics, reset_diagnostics_for_tests

    reset_diagnostics_for_tests()
    diagnostics = get_diagnostics(directory=str(tmp_path), level="developer", start_writer=False)
    _app()
    backend = QtVideoPlaybackBackend()
    started = []
    backend.started.connect(lambda: started.append(True))

    identity = "plex://server-1/999.mp4"
    transport_url = f"{fixture_server}?X-Plex-Token=REAL-SECRET-TOKEN-XYZ"
    assert backend.load(identity, transport_url=transport_url) is True
    assert _pump_until(lambda: bool(started))

    assert _pump_until(lambda: bool(_RecordingHandler.received_paths))
    assert any("REAL-SECRET-TOKEN-XYZ" in p for p in _RecordingHandler.received_paths), (
        "the token must actually reach the server as part of the request"
    )

    for event in diagnostics.recent_events:
        assert "REAL-SECRET-TOKEN-XYZ" not in repr(event)

    backend.shutdown()


def test_local_file_playback_unaffected_by_transport_url_parameter():
    _app()
    backend = QtVideoPlaybackBackend()
    started = []
    backend.started.connect(lambda: started.append(True))

    result = backend.load(_FIXTURE)
    assert result is True
    assert _pump_until(lambda: bool(started)), "local fixture never reported started"
    assert _pump_until(lambda: backend.duration_ms() > 0)

    backend.shutdown()
