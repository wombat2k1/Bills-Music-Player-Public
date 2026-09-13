"""Allow-list-only HTTP media serving for Google Cast."""
from __future__ import annotations

import mimetypes
import os
import secrets
import socket
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .media_capabilities import PlaybackBackend, backend_supported_extensions


_CAST_CONTENT_TYPES = {".mp3": "audio/mpeg", ".flac": "audio/flac"}
AUDIO_TYPES = {
    extension: _CAST_CONTENT_TYPES[extension]
    for extension in backend_supported_extensions(PlaybackBackend.CAST)
}


class UnsupportedMediaError(ValueError):
    pass


@dataclass(frozen=True)
class Resource:
    path: str
    content_type: str


def active_lan_address() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
        if address.startswith("127."):
            raise OSError("loopback address selected")
        return address
    finally:
        probe.close()


def parse_range(value: str, size: int):
    if not value:
        return None
    if not value.startswith("bytes=") or "," in value:
        raise ValueError
    first, separator, last = value[6:].strip().partition("-")
    if not separator:
        raise ValueError
    if not first:
        count = int(last)
        if count <= 0:
            raise ValueError
        return max(0, size - count), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start < 0 or start >= size or end < start:
        raise ValueError
    return start, min(end, size - 1)


class LocalMediaServer:
    def __init__(self, address_resolver=active_lan_address, logger=None):
        self._address_resolver = address_resolver
        self._logger = logger or (lambda _message: None)
        self._resources = {}
        self._lock = threading.RLock()
        self._server = None
        self._thread = None
        self._host = ""
        # v1.0.69: distinct from shutdown() (which only tears down the
        # *current* server -- start()/register() are expected to bring it
        # back for the next Cast session, and legitimately do, from
        # _on_cast_failed/_return_to_local_output/_force_local_output_for_video).
        # _closed is set exactly once, by close(), for real application
        # shutdown -- mirrors CastDiscoveryService/CastPlaybackController's
        # own _closed. Without this, a CastPayloadWorker's artwork
        # registration (or any other late caller) arriving after
        # application shutdown had already torn the server down could
        # resurrect a fresh HTTP server + thread via the ordinary
        # start()-on-register() path.
        self._closed = False

    @property
    def running(self):
        return self._server is not None

    def start(self):
        if self._closed or self.running:
            return
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "BillsMusicCast/1"

            def do_GET(self):
                self._respond(False)

            def do_HEAD(self):
                self._respond(True)

            def do_POST(self):
                self.send_error(405)

            def log_message(self, _format, *_args):
                pass

            def _respond(self, head_only):
                token = urlsplit(self.path).path.removeprefix("/")
                if not token or "/" in token or token in {".", ".."}:
                    self.send_error(404)
                    return
                with owner._lock:
                    resource = owner._resources.get(token)
                if resource is None:
                    self.send_error(404)
                    return
                try:
                    size = os.path.getsize(resource.path)
                    selected = parse_range(self.headers.get("Range", ""), size)
                except ValueError:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                except OSError:
                    self.send_error(404)
                    return
                start, end = selected or (0, size - 1)
                length = max(0, end - start + 1)
                self.send_response(206 if selected else 200)
                self.send_header("Content-Type", resource.content_type)
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                if selected:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if head_only:
                    return
                try:
                    with open(resource.path, "rb") as source:
                        source.seek(start)
                        remaining = length
                        while remaining:
                            data = source.read(min(131072, remaining))
                            if not data:
                                break
                            self.wfile.write(data)
                            remaining -= len(data)
                    owner._logger("Cast HTTP response completed")
                except (OSError, ConnectionError):
                    owner._logger("Cast HTTP response interrupted")

        self._host = self._address_resolver()
        self._server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="cast-media-server",
            daemon=True,
        )
        self._thread.start()

    def register(self, path, content_type=None):
        if self._closed:
            raise RuntimeError("Cast media server is closed")
        path = os.path.abspath(os.fspath(path))
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        content_type = content_type or mimetypes.guess_type(path)[0]
        if not content_type:
            raise UnsupportedMediaError("Unsupported media format")
        self.start()
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._resources[token] = Resource(path, content_type)
        port = self._server.server_address[1]
        return f"http://{self._host}:{port}/{token}"

    def register_audio(self, path):
        content_type = AUDIO_TYPES.get(os.path.splitext(os.fspath(path))[1].casefold())
        if not content_type:
            raise UnsupportedMediaError("Cast currently supports MP3 and FLAC")
        return self.register(path, content_type)

    def revoke_all(self):
        with self._lock:
            self._resources.clear()

    def shutdown(self):
        server, thread = self._server, self._thread
        self._server = self._thread = None
        self.revoke_all()
        if server:
            server.shutdown()
            server.server_close()
        if thread and thread is not threading.current_thread():
            thread.join(2)

    def close(self):
        """Application shutdown, not a normal Cast disconnect -- see
        _closed above. Permanently refuses start()/register() from this
        point on, so a late caller (a CastPayloadWorker whose result
        arrives after shutdown began, or any other straggler) cannot
        resurrect the HTTP server. A fresh LocalMediaServer instance in a
        new application session is unaffected -- this is per-instance
        state, not global."""
        self._closed = True
        self.shutdown()
