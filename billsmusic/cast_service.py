"""Optional PyChromecast adapter; all network work stays off the Qt thread."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum

from PyQt6 import QtCore


class CastState(str, Enum):
    DISABLED = "disabled"
    DISCOVERING = "discovering"
    AVAILABLE = "available"
    CONNECTING = "connecting"
    LOADING = "loading"
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"
    DISCONNECTED = "disconnected"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


def is_natural_completion(snapshot, armed=True):
    """Only Cast's explicit FINISHED idle reason may advance the queue."""
    return bool(
        armed
        and snapshot.get("state") == "idle"
        and snapshot.get("idle_reason") == "finished"
    )


def build_music_metadata(meta, artwork_url=""):
    """Build receiver-friendly music metadata.

    Some Cast audio receivers display ``albumArtist`` in preference to the
    track artist.  Omitting generic compilation values lets those receivers
    correctly fall back to the real per-track ``artist`` field.
    """
    artist = str(meta.get("artist", "") or "")
    album_artist = str(meta.get("album_artist", "") or "")
    payload = {
        "metadataType": 3,
        "title": str(meta.get("title", "") or ""),
        "artist": artist,
        "albumName": str(meta.get("album", "") or ""),
        "trackNumber": int(meta.get("track_no", 0) or 0),
        "discNumber": int(meta.get("disc_no", 0) or 0),
    }
    generic_album_artists = {
        "various", "various artist", "various artists", "va", "v/a",
    }
    if (
        album_artist
        and album_artist.casefold().strip() not in generic_album_artists
    ):
        payload["albumArtist"] = album_artist
    if artwork_url:
        payload["images"] = [{"url": artwork_url}]
    return payload


@dataclass(frozen=True)
class CastDevice:
    uuid: str
    name: str
    cast: object


class CastDiscoveryService(QtCore.QObject):
    devices_changed = QtCore.pyqtSignal(object)
    state_changed = QtCore.pyqtSignal(str, str)

    def __init__(self, parent=None, backend=None):
        super().__init__(parent)
        self._backend = backend
        self._browser = None
        self._generation = 0
        # Distinct from _generation (which changes on every normal
        # refresh()/stop() and only means "a newer discovery superseded
        # this one"): _closed is set exactly once, by close(), and means
        # "the application is shutting down -- never emit again,
        # regardless of generation." Checked immediately before every
        # emit in _discover() (v1.0.66 worker-lifetime hardening) since
        # this thread is unreferenced and cannot itself be joined.
        self._closed = False
        self.discovery_thread = None

    def refresh(self):
        if self._closed:
            return
        self.stop()
        self._generation += 1
        generation = self._generation
        self.state_changed.emit("discovering", "Searching for Cast devices…")
        self.discovery_thread = threading.Thread(
            target=self._discover, args=(generation,), name="cast-discovery",
            daemon=True,
        )
        self.discovery_thread.start()

    def _discover(self, generation):
        try:
            if self._backend:
                casts, browser = self._backend()
            else:
                import pychromecast
                casts, browser = pychromecast.get_chromecasts(timeout=5)
            if generation != self._generation or self._closed:
                try:
                    browser.stop_discovery()
                except Exception:
                    pass
                return
            self._browser = browser
            devices = [
                CastDevice(str(c.uuid), c.name, c) for c in casts
                if getattr(c, "cast_type", "audio") in ("audio", "group")
            ]
            if self._closed:
                return
            self.devices_changed.emit(devices)
            self.state_changed.emit(
                "available", "" if devices else "No Cast devices found",
            )
        except ImportError:
            if not self._closed:
                self.state_changed.emit("unavailable", "Cast support is not installed")
        except Exception as exc:
            if not self._closed:
                self.state_changed.emit("error", f"Cast discovery failed: {exc}")

    def stop(self):
        self._generation += 1
        browser, self._browser = self._browser, None
        if browser:
            try:
                browser.stop_discovery()
            except Exception:
                pass

    def close(self):
        """Application shutdown, not a normal refresh/stop -- see _closed
        above. discovery_thread (if any) is left owned/running; get_chromecasts()
        has no cooperative interruption point, so this only guarantees its
        eventual result is dropped, not that it stops early."""
        self._closed = True
        self.stop()


class CastPlaybackController(QtCore.QObject):
    state_changed = QtCore.pyqtSignal(str, str)
    connected = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)
    loaded = QtCore.pyqtSignal()
    # The same events, each carrying the opaque `request` the caller passed
    # to connect_device()/load_async(). The generation check below only runs
    # on the worker thread before emitting, so a result already queued when a
    # newer request, Stop or a return to local output happens still reaches
    # the GUI; the request lets the receiver decide whether it still has any
    # authority (Astra F11). Emitted alongside the untagged signals above.
    request_connected = QtCore.pyqtSignal(object, object)  # device, request
    request_loaded = QtCore.pyqtSignal(object)  # request
    request_failed = QtCore.pyqtSignal(object, str)  # request, message

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cast = None
        self.volume = 0.5
        # See CastDiscoveryService's _closed/_generation comment above --
        # same reasoning, applied to connect_device()/load_async()'s
        # unreferenced background threads (v1.0.66 worker-lifetime
        # hardening). _generation is bumped on every connect/load
        # request (a newer one supersedes an older in-flight one); _closed
        # is set once, by close(), for real application shutdown.
        self._generation = 0
        self._closed = False
        self.connect_thread = None
        self.load_thread = None

    def connect_device(self, device, request=None):
        if self._closed:
            return
        self._generation += 1
        generation = self._generation
        self.state_changed.emit("connecting", f"Connecting to {device.name}…")
        self.connect_thread = threading.Thread(
            target=self._connect, args=(device, generation), kwargs={"request": request},
            name="cast-connect", daemon=True,
        )
        self.connect_thread.start()

    def _is_stale(self, generation) -> bool:
        return self._closed or generation != self._generation

    def _connect(self, device, generation, request=None):
        try:
            cast = device.cast
            if cast.socket_client.ident is not None:
                # cast.socket_client is a plain threading.Thread -- Python
                # threads are single-use, so reconnecting to the very same
                # Chromecast object after disconnect() (e.g. switching back
                # to "This Computer" and then selecting this device again
                # without an intervening device refresh) raises "threads
                # can only be started once" from wait()'s socket_client
                # .start() call. .ident is None until a thread's first
                # start() and stays set forever after, so it reliably
                # marks "already used once" without touching is_alive()
                # (which is also False before the first start). Rebuild a
                # fresh, never-started Chromecast from the same
                # already-resolved host/port instead -- no new mDNS scan
                # needed.
                import pychromecast
                info = cast.cast_info
                cast = pychromecast.get_chromecast_from_host(
                    (info.host, info.port, info.uuid, info.model_name, info.friendly_name)
                )
            cast.wait(timeout=10)
            if self._is_stale(generation):
                return
            self.cast = cast
            self.connected.emit(device)
            self.request_connected.emit(device, request)
            self.state_changed.emit("stopped", f"Casting to {device.name}")
        except Exception as exc:
            if self._is_stale(generation):
                return
            self.cast = None
            self.failed.emit(str(exc))
            self.request_failed.emit(request, str(exc))
            self.state_changed.emit("error", f"Could not connect: {exc}")

    def load(self, url, content_type, metadata, position=0.0, autoplay=True):
        if not self.cast:
            raise RuntimeError("No Cast device connected")
        self.state_changed.emit("loading", "Loading track…")
        controller = self.cast.media_controller
        controller.play_media(
            url, content_type, metadata=metadata,
            current_time=max(0.0, float(position)), autoplay=bool(autoplay),
            # pychromecast's play_media() defaults stream_type to LIVE.
            # Tracks served from LocalMediaServer are fixed-length files,
            # not live streams -- LIVE tells the receiver's Default Media
            # Receiver UI to render the "live broadcast" card instead of
            # the rich Now Playing card, which drops the title/artist/art
            # display and the seekable duration bar on the cast device's
            # own screen even though this app's own UI looks correct.
            stream_type="BUFFERED",
        )
        controller.block_until_active(timeout=10)

    def load_async(self, url, content_type, metadata, position=0.0, autoplay=True, request=None):
        if self._closed:
            return
        self._generation += 1
        generation = self._generation
        self.load_thread = threading.Thread(
            target=self._load_worker,
            args=(generation, url, content_type, metadata, position, autoplay),
            kwargs={"request": request},
            name="cast-load",
            daemon=True,
        )
        self.load_thread.start()

    def _load_worker(self, generation, *args, request=None):
        if self._is_stale(generation):
            return
        try:
            self.load(*args)
            if self._is_stale(generation):
                return
            self.loaded.emit()
            self.request_loaded.emit(request)
            self.state_changed.emit(
                "playing" if args[-1] else "paused",
                "Cast track loaded",
            )
        except Exception as exc:
            if self._is_stale(generation):
                return
            self.failed.emit(str(exc))
            self.request_failed.emit(request, str(exc))
            self.state_changed.emit("error", f"Cast load failed: {exc}")

    def play(self):
        if self.cast:
            self.cast.media_controller.play()

    def pause(self):
        if self.cast:
            self.cast.media_controller.pause()

    def stop(self):
        if self.cast:
            self.cast.media_controller.stop()

    def seek(self, seconds):
        if self.cast:
            self.cast.media_controller.seek(max(0.0, float(seconds)))

    def set_volume(self, value):
        self.volume = max(0.0, min(1.0, float(value)))
        if self.cast:
            self.cast.set_volume(self.volume)

    def snapshot(self):
        if not self.cast:
            return {
                "state": "disconnected", "position": 0.0,
                "duration": 0.0, "raw_position": 0.0,
                "position_source": "raw",
            }
        try:
            status = self.cast.media_controller.status
            state = str(getattr(status, "player_state", "")).casefold()
            raw_position = max(
                0.0, float(getattr(status, "current_time", 0.0) or 0.0)
            )
            duration = max(
                0.0, float(getattr(status, "duration", 0.0) or 0.0)
            )
            position = raw_position
            position_source = "raw"
            if state == "playing":
                # current_time is only the position contained in the most
                # recent MEDIA_STATUS message. A receiver may not send
                # another status for a long time, so treating it as a live
                # clock freezes both the progress bar and the equaliser.
                # PyChromecast's adjusted_current_time extrapolates from
                # current_time, last_updated and playback_rate specifically
                # for this purpose. Paused/stopped states deliberately keep
                # the raw position so their clock cannot drift.
                try:
                    adjusted = getattr(status, "adjusted_current_time", None)
                except Exception:
                    # Timing enrichment must never turn a healthy Cast
                    # connection into a false disconnect. Older or unusual
                    # controller implementations can safely fall back to
                    # their last raw receiver position.
                    adjusted = None
                if adjusted is not None:
                    try:
                        position = max(0.0, float(adjusted))
                        position_source = "adjusted"
                    except (TypeError, ValueError, OverflowError):
                        position = raw_position
            if duration > 0.0:
                position = min(position, duration)
            return {
                "state": state,
                "position": position,
                "duration": duration,
                "idle_reason": str(
                    getattr(status, "idle_reason", "") or ""
                ).casefold(),
                "raw_position": raw_position,
                "position_source": position_source,
            }
        except Exception:
            return {
                "state": "disconnected", "position": 0.0,
                "duration": 0.0, "idle_reason": "",
                "raw_position": 0.0, "position_source": "raw",
            }

    def disconnect(self):
        cast, self.cast = self.cast, None
        if cast:
            try:
                cast.disconnect(timeout=5)
            except TypeError:
                cast.disconnect()
        self.state_changed.emit("disconnected", "")

    def close(self):
        """Application shutdown, not a normal disconnect -- see _closed
        above. connect_thread/load_thread (if any) are left owned/running;
        cast.wait()/play_media() have no cooperative interruption point,
        so this only guarantees their eventual result is dropped via
        _is_stale(), not that they stop early."""
        self._closed = True
        self._generation += 1
