import sys
import threading
import time
from types import SimpleNamespace

from PyQt6 import QtCore, QtWidgets

from billsmusic.cast_service import (
    CastDevice, CastDiscoveryService, CastPlaybackController,
    build_music_metadata,
    is_natural_completion,
)

_APP = None

class FakeBrowser:
    def __init__(self):
        self.stopped = False

    def stop_discovery(self):
        self.stopped = True


class FakeMedia:
    def __init__(self):
        self.calls = []
        self.status = type(
            "Status", (), {
                "player_state": "PLAYING",
                "current_time": 4,
                "adjusted_current_time": 4,
                "duration": 9,
            }
        )()

    def play_media(self, *args, **kwargs):
        self.calls.append(("load", args, kwargs))

    def block_until_active(self, timeout):
        self.calls.append(("active", timeout))

    def play(self):
        self.calls.append(("play",))

    def pause(self):
        self.calls.append(("pause",))

    def stop(self):
        self.calls.append(("stop",))

    def seek(self, value):
        self.calls.append(("seek", value))


class FakeSocketClient:
    """Mimics the .ident behaviour of pychromecast's real threading.Thread
    socket_client: None until the first start(), set forever after."""

    def __init__(self):
        self.ident = None

    def start(self):
        if self.ident is not None:
            raise RuntimeError("threads can only be started once")
        self.ident = 1


class FakeCast:
    uuid = "stable-id"
    name = "Living Room"
    cast_type = "audio"

    def __init__(self, fail=False):
        self.fail = fail
        self.media_controller = FakeMedia()
        self.volumes = []
        self.disconnected = False
        self.socket_client = FakeSocketClient()
        self.cast_info = SimpleNamespace(
            host="10.0.0.5", port=8009, uuid=self.uuid,
            model_name="Chromecast", friendly_name=self.name,
        )

    def wait(self, timeout):
        if self.fail:
            raise TimeoutError("offline")
        self.socket_client.start()

    def set_volume(self, value):
        self.volumes.append(value)

    def disconnect(self, timeout=0):
        self.disconnected = True


def _spin_until(predicate, timeout=2):
    global _APP
    if QtCore.QCoreApplication.instance() is None:
        _APP = QtWidgets.QApplication([])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QtCore.QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_discovery_is_background_and_reports_devices():
    browser = FakeBrowser()
    thread_ids = []

    def backend():
        thread_ids.append(threading.get_ident())
        return [FakeCast()], browser

    service = CastDiscoveryService(backend=backend)
    devices = []
    service.devices_changed.connect(devices.extend)
    caller = threading.get_ident()
    service.refresh()
    assert _spin_until(lambda: devices)
    assert thread_ids[0] != caller
    assert devices[0].uuid == "stable-id"
    service.stop()
    assert browser.stopped


def test_connection_load_controls_volume_snapshot_and_disconnect():
    cast = FakeCast()
    device = CastDevice("stable-id", "Living Room", cast)
    controller = CastPlaybackController()
    connected = []
    controller.connected.connect(connected.append)
    controller.connect_device(device)
    assert _spin_until(lambda: connected)
    metadata = {"metadataType": 3, "title": "Track", "artist": "Artist"}
    controller.load("http://host/token", "audio/mpeg", metadata, 3.5, False)
    controller.play()
    controller.pause()
    controller.seek(7)
    controller.set_volume(0.7)
    assert cast.media_controller.calls[0][0] == "load"
    assert cast.media_controller.calls[0][2]["metadata"] == metadata
    assert controller.snapshot() == {
        "state": "playing", "position": 4.0, "duration": 9.0,
        "idle_reason": "", "raw_position": 4.0,
        "position_source": "adjusted",
    }
    assert cast.volumes == [0.7]
    controller.disconnect()
    assert cast.disconnected


def test_load_uses_buffered_stream_type_not_live():
    # Reported: the Cast device's own screen doesn't show the track name.
    # Root cause: play_media() defaults stream_type to "LIVE" when not
    # given explicitly. The Default Media Receiver renders LIVE content
    # with a stripped-down "live broadcast" card that omits the rich
    # title/artist/artwork display and duration/seek bar it shows for
    # on-demand ("BUFFERED") content -- exactly what our fixed-length
    # audio files served by LocalMediaServer actually are.
    cast = FakeCast()
    device = CastDevice("stable-id", "Living Room", cast)
    controller = CastPlaybackController()
    connected = []
    controller.connected.connect(connected.append)
    controller.connect_device(device)
    assert _spin_until(lambda: connected)
    metadata = {"metadataType": 3, "title": "Track", "artist": "Artist"}
    controller.load("http://host/token", "audio/mpeg", metadata, 3.5, False)
    assert cast.media_controller.calls[0][2]["stream_type"] == "BUFFERED"


def test_playing_snapshot_uses_adjusted_time_when_raw_cast_status_is_stale():
    """PyChromecast's current_time is only its last receiver report.

    adjusted_current_time extrapolates that report using last_updated and
    playback_rate. Both the progress bar and equaliser need the adjusted
    value or they advance for at most one short interpolation window and
    then freeze until another receiver status message happens to arrive.
    """
    controller = CastPlaybackController()
    status = SimpleNamespace(
        player_state="PLAYING",
        current_time=12.0,
        adjusted_current_time=17.25,
        duration=180.0,
        idle_reason=None,
    )
    controller.cast = SimpleNamespace(
        media_controller=SimpleNamespace(status=status)
    )

    snapshot = controller.snapshot()

    assert snapshot["position"] == 17.25
    assert snapshot["raw_position"] == 12.0
    assert snapshot["position_source"] == "adjusted"


def test_paused_snapshot_does_not_extrapolate_past_reported_position():
    controller = CastPlaybackController()
    status = SimpleNamespace(
        player_state="PAUSED",
        current_time=42.0,
        adjusted_current_time=99.0,
        duration=180.0,
        idle_reason=None,
    )
    controller.cast = SimpleNamespace(
        media_controller=SimpleNamespace(status=status)
    )

    snapshot = controller.snapshot()

    assert snapshot["position"] == 42.0
    assert snapshot["position_source"] == "raw"


def test_playing_snapshot_falls_back_to_raw_if_adjusted_clock_is_unavailable():
    class StatusWithBrokenAdjustedClock:
        player_state = "PLAYING"
        current_time = 23.0
        duration = 180.0
        idle_reason = None

        @property
        def adjusted_current_time(self):
            raise RuntimeError("receiver clock unavailable")

    controller = CastPlaybackController()
    controller.cast = SimpleNamespace(
        media_controller=SimpleNamespace(status=StatusWithBrokenAdjustedClock())
    )

    snapshot = controller.snapshot()

    assert snapshot["state"] == "playing"
    assert snapshot["position"] == 23.0
    assert snapshot["position_source"] == "raw"


def test_playing_snapshot_falls_back_to_raw_if_adjusted_clock_is_unavailable():
    class StatusWithBrokenAdjustedClock:
        player_state = "PLAYING"
        current_time = 23.0
        duration = 180.0
        idle_reason = None

        @property
        def adjusted_current_time(self):
            raise RuntimeError("receiver clock unavailable")

    controller = CastPlaybackController()
    controller.cast = SimpleNamespace(
        media_controller=SimpleNamespace(status=StatusWithBrokenAdjustedClock())
    )

    snapshot = controller.snapshot()

    assert snapshot["state"] == "playing"
    assert snapshot["position"] == 23.0
    assert snapshot["position_source"] == "raw"


def test_failed_connection_is_nonfatal():
    controller = CastPlaybackController()
    failures = []
    controller.failed.connect(failures.append)
    controller.connect_device(CastDevice("id", "Offline", FakeCast(fail=True)))
    assert _spin_until(lambda: failures)
    assert controller.cast is None


def test_reconnecting_to_the_same_device_rebuilds_a_fresh_chromecast(monkeypatch):
    # Reported: after casting, switching back to "This Computer", then
    # selecting the same Cast device again shows "Could not connect:
    # threads can only be started once". Root cause: device.cast (a
    # pychromecast.Chromecast wrapping a plain threading.Thread
    # socket_client) is reused across the whole app session -- once
    # disconnect() has stopped that thread, wait()'s internal
    # socket_client.start() can never be called again (Python threads
    # are single-use). The fix rebuilds a fresh Chromecast from the same
    # resolved host/port via pychromecast.get_chromecast_from_host()
    # whenever socket_client.ident shows the old one was already used.
    stale_cast = FakeCast()
    device = CastDevice("stable-id", "Living Room", stale_cast)
    controller = CastPlaybackController()
    connected = []
    controller.connected.connect(connected.append)

    # First connection: a brand new socket_client, connects normally.
    controller.connect_device(device)
    assert _spin_until(lambda: connected)
    assert controller.cast is stale_cast

    # Simulate switching back to "This Computer": disconnect() stops the
    # real thread permanently (here: nothing to reset -- ident stays set,
    # matching a real already-started-and-joined threading.Thread).
    controller.disconnect()

    # Reconnecting to the very same CastDevice (as the dropdown does when
    # re-selecting a device without an intervening discovery refresh)
    # must not touch the stale socket_client's start() again.
    fresh_cast = FakeCast()
    build_calls = []

    def fake_get_chromecast_from_host(host):
        build_calls.append(host)
        return fresh_cast

    fake_pychromecast = SimpleNamespace(
        get_chromecast_from_host=fake_get_chromecast_from_host
    )
    monkeypatch.setitem(sys.modules, "pychromecast", fake_pychromecast)

    connected.clear()
    failures = []
    controller.failed.connect(failures.append)
    controller.connect_device(device)
    assert _spin_until(lambda: connected or failures)

    assert failures == []
    assert build_calls == [("10.0.0.5", 8009, "stable-id", "Chromecast", "Living Room")]
    assert controller.cast is fresh_cast


def test_only_explicit_finished_idle_advances_queue():
    assert is_natural_completion(
        {"state": "idle", "idle_reason": "finished"}, armed=True
    )
    for snapshot in (
        {"state": "idle", "idle_reason": "interrupted"},
        {"state": "idle", "idle_reason": "cancelled"},
        {"state": "idle", "idle_reason": ""},
        {"state": "playing", "idle_reason": "finished"},
    ):
        assert not is_natural_completion(snapshot, armed=True)
    assert not is_natural_completion(
        {"state": "idle", "idle_reason": "finished"}, armed=False
    )


def test_compilation_metadata_prefers_track_artist_on_receiver():
    payload = build_music_metadata({
        "title": "Song", "artist": "Real Singer",
        "album": "Compilation", "album_artist": "Various Artists",
        "track_no": 4, "disc_no": 1,
    })
    assert payload["artist"] == "Real Singer"
    assert "albumArtist" not in payload


def test_specific_album_artist_is_preserved():
    payload = build_music_metadata({
        "title": "Song", "artist": "Featured Singer",
        "album": "Album", "album_artist": "Main Band",
    }, "http://host/art")
    assert payload["artist"] == "Featured Singer"
    assert payload["albumArtist"] == "Main Band"
    assert payload["images"] == [{"url": "http://host/art"}]


# ---------------------------------------------------------------------------
# v1.0.66 worker-lifetime hardening: connect_device()/load_async()/refresh()
# spawn unreferenced background threads with no prior cancellation/shutdown
# path at all -- close()/_generation/_closed close that gap by dropping any
# in-flight result rather than emitting into a closing/closed application.
# ---------------------------------------------------------------------------

class _BlockingCast(FakeCast):
    """wait() blocks on an Event the test controls, so close()/a newer
    connect_device() call can be issued while a connect is genuinely
    still in flight, deterministically -- not a sleep-based race."""

    def __init__(self, release_event):
        super().__init__()
        self._release_event = release_event

    def wait(self, timeout):
        self._release_event.wait(timeout=5)
        super().wait(timeout)


def test_connect_device_drops_result_after_close():
    release = threading.Event()
    device = CastDevice("id", "Living Room", _BlockingCast(release))
    controller = CastPlaybackController()
    connected = []
    controller.connected.connect(connected.append)
    controller.connect_device(device)
    controller.close()
    release.set()
    thread = controller.connect_thread
    assert thread is not None
    thread.join(timeout=5)
    assert not connected
    assert controller.cast is None


def test_connect_device_drops_result_after_being_superseded_by_a_newer_request():
    release = threading.Event()
    stale_device = CastDevice("id-1", "Stale", _BlockingCast(release))
    fresh_cast = FakeCast()
    fresh_device = CastDevice("id-2", "Fresh", fresh_cast)
    controller = CastPlaybackController()
    connected = []
    controller.connected.connect(connected.append)
    controller.connect_device(stale_device)
    controller.connect_device(fresh_device)  # supersedes the still-blocked one
    release.set()
    controller.connect_thread and controller.connect_thread.join(timeout=5)
    assert _spin_until(lambda: connected)
    assert len(connected) == 1
    assert connected[0].uuid == "id-2"
    assert controller.cast is fresh_cast


def test_load_async_never_starts_work_when_closed_first():
    cast = FakeCast()
    controller = CastPlaybackController()
    controller.cast = cast
    loaded = []
    failed = []
    controller.loaded.connect(lambda: loaded.append(True))
    controller.failed.connect(failed.append)
    controller.close()  # closed BEFORE the request -- must never even start work
    controller.load_async("http://host/token", "audio/mpeg", {}, 0.0, True)
    assert controller.load_thread is None
    assert not loaded
    assert not failed
    assert cast.media_controller.calls == []


def test_load_worker_drops_a_stale_result_without_calling_load():
    # _load_worker is called directly (not through the real thread) so this
    # stays a fast, deterministic unit test of the staleness check itself,
    # matching this suite's existing style for worker run() bodies.
    cast = FakeCast()
    controller = CastPlaybackController()
    controller.cast = cast
    loaded = []
    failed = []
    controller.loaded.connect(lambda: loaded.append(True))
    controller.failed.connect(failed.append)
    controller._generation = 2  # a newer request has since superseded gen 1

    controller._load_worker(1, "http://host/other", "audio/mpeg", {}, 0.0, True)

    assert cast.media_controller.calls == []
    assert not loaded
    assert not failed


def test_load_worker_proceeds_normally_for_the_current_generation():
    cast = FakeCast()
    controller = CastPlaybackController()
    controller.cast = cast
    loaded = []
    controller.loaded.connect(lambda: loaded.append(True))
    controller._generation = 1

    controller._load_worker(1, "http://host/token", "audio/mpeg", {}, 0.0, True)

    assert loaded == [True]
    assert cast.media_controller.calls[0][0] == "load"


def test_load_worker_tags_its_results_with_the_originating_request():
    """Astra F11: the request passed to load_async()/connect_device() comes
    back on the request_* signals, so the GUI can tell whose result it is."""
    cast = FakeCast()
    controller = CastPlaybackController()
    controller.cast = cast
    loaded, failed, connected = [], [], []
    controller.request_loaded.connect(loaded.append)
    controller.request_failed.connect(lambda request, message: failed.append((request, message)))
    controller.request_connected.connect(lambda device, request: connected.append((device.uuid, request)))
    request = object()
    controller._generation = 1

    controller._load_worker(1, "http://host/token", "audio/mpeg", {}, 0.0, True, request=request)
    controller.cast = None  # the next load fails: no device connected
    controller._load_worker(1, "http://host/token", "audio/mpeg", {}, 0.0, True, request=request)
    controller.connect_device(CastDevice("dev-1", "Living Room", FakeCast()), request=request)
    controller.connect_thread.join(timeout=5)

    assert loaded == [request]
    assert failed == [(request, "No Cast device connected")]
    assert _spin_until(lambda: connected)
    assert connected == [("dev-1", request)]


def test_discovery_refresh_drops_result_after_close():
    release = threading.Event()
    browser = FakeBrowser()

    def backend():
        release.wait(timeout=5)
        return [FakeCast()], browser

    service = CastDiscoveryService(backend=backend)
    devices = []
    service.devices_changed.connect(devices.extend)
    service.refresh()
    service.close()
    release.set()
    thread = service.discovery_thread
    assert thread is not None
    thread.join(timeout=5)
    assert not devices
    assert browser.stopped


def test_refresh_refuses_to_start_new_work_once_closed():
    def backend():
        raise AssertionError("must never be called once closed")

    service = CastDiscoveryService(backend=backend)
    errors = []
    service.state_changed.connect(lambda state, msg: errors.append((state, msg)))
    service.close()
    service.refresh()
    assert service.discovery_thread is None
    assert errors == []


def test_connect_device_and_load_async_refuse_to_start_new_work_once_closed():
    controller = CastPlaybackController()
    controller.close()
    controller.connect_device(CastDevice("id", "Living Room", FakeCast()))
    assert controller.connect_thread is None
    controller.load_async("http://host/token", "audio/mpeg", {}, 0.0, True)
    assert controller.load_thread is None


def test_close_is_idempotent_and_safe_with_nothing_in_flight():
    CastDiscoveryService().close()  # must not raise
    CastPlaybackController().close()  # must not raise
