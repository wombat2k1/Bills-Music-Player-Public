"""Optional BASS audio backend.

This is intentionally a small facade that matches the methods window.py already
uses for the built-in player path. BASS DLLs live under vendor/bass and are only
loaded when the user enables the BASS backend.
"""
import ctypes
import enum
import hashlib
import math
import os
import platform
import threading
from typing import Optional


BASS_UNICODE = 0x80000000
BASS_STREAM_PRESCAN = 0x20000
BASS_POS_BYTE = 0
BASS_ATTRIB_VOL = 2
BASS_ACTIVE_STOPPED = 0
BASS_ACTIVE_PLAYING = 1
BASS_ACTIVE_STALLED = 2
BASS_ACTIVE_PAUSED = 3
_BASS_NO_DEVICE = 0xFFFFFFFF  # BASS_GetDevice()'s DWORD -1 ("no device"/error)
# BASS_DATA_FFTxxxx flags for BASS_ChannelGetData: high bit marks an FFT
# request, low bits select FFT size (2048 -> 1024 returned magnitude
# floats, a reasonable size/detail tradeoff for a 32-bar visualiser).
BASS_DATA_FFT2048 = 0x80000000 | 3
_FFT2048_BIN_COUNT = 1024
# Matches audio.py's AudioAnalyzer._DB_FLOOR exactly -- kept as its own
# constant (not a cross-import) so this module stays dependency-light,
# not pulling in audio.py's numpy/soundfile/librosa imports just for one
# shared number.
_DB_FLOOR = -70.0


class _BassDeviceInfo(ctypes.Structure):
    """Mirrors BASS's own BASS_DEVICEINFO struct exactly (name, driver,
    flags) -- used only by current_device_description() below, for the
    classic-video-silence investigation's Qt-vs-BASS output-device
    comparison (2026-08-31 Codex audit, section 7). Never used by any
    playback logic."""
    _fields_ = [
        ("name", ctypes.c_char_p),
        ("driver", ctypes.c_char_p),
        ("flags", ctypes.c_uint),
    ]


class BassLoadError(RuntimeError):
    pass


class _BassEngine:
    bass = None
    dll_dir = ""
    initialized = False
    plugins_loaded = False
    # Phase C1 (native audio backend ownership, 2026-09-10): guards ONLY
    # the one-time DLL-load/BASS_Init/plugin-load section below -- a real
    # race is possible now that BASS stream *creation* (BASS_StreamCreateURL/
    # File, in BassPlayer.prepare_stream) can run from a background
    # candidate-preparation worker instead of only ever from the GUI
    # thread. Never held around BASS_StreamCreateURL/File themselves, or
    # any other potentially-blocking network/file call -- those run
    # entirely outside this lock, matching this codebase's existing
    # "don't block the GUI/other workers behind a network wait" rule.
    _init_lock = threading.Lock()

    @classmethod
    def ensure(cls):
        if cls.bass is not None and cls.initialized and cls.plugins_loaded:
            return cls.bass  # already warmed -- lock-free fast path
        with cls._init_lock:
            if cls.bass is None:
                cls._load()
            if not cls.initialized:
                if not cls.bass.BASS_Init(-1, 44100, 0, None, None):
                    raise BassLoadError(f"BASS_Init failed: {cls.error_code()}")
                cls.initialized = True
            if not cls.plugins_loaded:
                cls._load_plugins()
                cls.plugins_loaded = True
        return cls.bass

    @classmethod
    def error_code(cls) -> int:
        try:
            return int(cls.bass.BASS_ErrorGetCode())
        except Exception:
            return 0

    @classmethod
    def current_device_description(cls) -> str:
        """The real output device BASS_Init(-1, ...) actually resolved to
        for the calling thread (BASS's device selection is per-thread) --
        added solely for the classic-video-silence investigation's section
        7 ("does Qt's classic video child use a different Windows audio
        endpoint than BASS"). Returns "" if BASS isn't initialised yet or
        the query fails for any reason; never raises, and never used by
        any real playback code path -- diagnostics only. The caller is
        responsible for hashing this before logging it (see window.py) --
        this returns the raw device name."""
        if cls.bass is None or not cls.initialized:
            return ""
        try:
            device_index = cls.bass.BASS_GetDevice()
            if device_index == _BASS_NO_DEVICE:
                return ""
            info = _BassDeviceInfo()
            if not cls.bass.BASS_GetDeviceInfo(device_index, ctypes.byref(info)):
                return ""
            return info.name.decode("utf-8", "replace") if info.name else ""
        except Exception:
            return ""

    @classmethod
    def _load(cls):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
        arch_dir = "x64" if platform.architecture()[0] == "64bit" else ""
        candidates = []
        if arch_dir:
            candidates.append(os.path.join(root, "vendor", "bass", "bin", arch_dir, "bass.dll"))
            candidates.append(os.path.join(root, "vendor", "bass", "bass24", arch_dir, "bass.dll"))
        candidates.append(os.path.join(root, "vendor", "bass", "bin", "bass.dll"))
        candidates.append(os.path.join(root, "vendor", "bass", "bass24", "bass.dll"))
        dll_path = next((path for path in candidates if os.path.isfile(path)), "")
        if not dll_path:
            raise BassLoadError("bass.dll was not found under vendor/bass")
        cls.dll_dir = os.path.dirname(dll_path)
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(cls.dll_dir)
        cls.bass = ctypes.WinDLL(dll_path)
        cls._bind_functions()

    @classmethod
    def _bind_functions(cls):
        bass = cls.bass
        bass.BASS_Init.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        bass.BASS_Init.restype = ctypes.c_bool
        bass.BASS_Free.argtypes = []
        bass.BASS_Free.restype = ctypes.c_bool
        bass.BASS_ErrorGetCode.argtypes = []
        bass.BASS_ErrorGetCode.restype = ctypes.c_int
        bass.BASS_StreamCreateFile.argtypes = [
            ctypes.c_bool, ctypes.c_wchar_p, ctypes.c_ulonglong, ctypes.c_ulonglong, ctypes.c_uint
        ]
        bass.BASS_StreamCreateFile.restype = ctypes.c_uint
        # Stage 3A (Plex Direct Play audio). Proven empirically (see
        # tests/test_bass_url_streaming.py and the Stage 3A format-audit
        # report), not assumed: the `url` argument is ALWAYS `const
        # char*` in BASS's C ABI, never `const wchar_t*` -- combining it
        # with BASS_UNICODE (which tells BASS to reinterpret this same
        # pointer as UTF-16) is an illegal-parameter combination and
        # every call fails with BASS_ERROR_ILLPARAM. Custom HTTP request
        # headers (the Plex token, among others) are appended directly
        # to this same buffer as CRLF-separated "Header: value" lines
        # after the URL -- there is no separate headers parameter.
        bass.BASS_StreamCreateURL.argtypes = [
            ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
        ]
        bass.BASS_StreamCreateURL.restype = ctypes.c_uint
        bass.BASS_StreamFree.argtypes = [ctypes.c_uint]
        bass.BASS_StreamFree.restype = ctypes.c_bool
        bass.BASS_ChannelPlay.argtypes = [ctypes.c_uint, ctypes.c_bool]
        bass.BASS_ChannelPlay.restype = ctypes.c_bool
        bass.BASS_ChannelPause.argtypes = [ctypes.c_uint]
        bass.BASS_ChannelPause.restype = ctypes.c_bool
        bass.BASS_ChannelStop.argtypes = [ctypes.c_uint]
        bass.BASS_ChannelStop.restype = ctypes.c_bool
        bass.BASS_ChannelIsActive.argtypes = [ctypes.c_uint]
        bass.BASS_ChannelIsActive.restype = ctypes.c_uint
        bass.BASS_ChannelSetAttribute.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_float]
        bass.BASS_ChannelSetAttribute.restype = ctypes.c_bool
        bass.BASS_ChannelSlideAttribute.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_float, ctypes.c_uint]
        bass.BASS_ChannelSlideAttribute.restype = ctypes.c_bool
        bass.BASS_ChannelGetLength.argtypes = [ctypes.c_uint, ctypes.c_uint]
        bass.BASS_ChannelGetLength.restype = ctypes.c_ulonglong
        bass.BASS_ChannelGetPosition.argtypes = [ctypes.c_uint, ctypes.c_uint]
        bass.BASS_ChannelGetPosition.restype = ctypes.c_ulonglong
        bass.BASS_ChannelBytes2Seconds.argtypes = [ctypes.c_uint, ctypes.c_ulonglong]
        bass.BASS_ChannelBytes2Seconds.restype = ctypes.c_double
        bass.BASS_ChannelSeconds2Bytes.argtypes = [ctypes.c_uint, ctypes.c_double]
        bass.BASS_ChannelSeconds2Bytes.restype = ctypes.c_ulonglong
        bass.BASS_ChannelSetPosition.argtypes = [ctypes.c_uint, ctypes.c_ulonglong, ctypes.c_uint]
        bass.BASS_ChannelSetPosition.restype = ctypes.c_bool
        bass.BASS_PluginLoad.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
        bass.BASS_PluginLoad.restype = ctypes.c_uint
        bass.BASS_GetDevice.argtypes = []
        bass.BASS_GetDevice.restype = ctypes.c_uint
        # Stage 3A visualiser fix: reads straight from whatever this
        # channel already has decoded/buffered internally -- BASS_Channel-
        # GetData never touches the network itself, whether the channel
        # is a local BASS_StreamCreateFile stream or a remote
        # BASS_StreamCreateURL one. See get_fft_levels() below.
        bass.BASS_ChannelGetData.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        bass.BASS_ChannelGetData.restype = ctypes.c_int
        bass.BASS_GetDeviceInfo.argtypes = [ctypes.c_uint, ctypes.POINTER(_BassDeviceInfo)]
        bass.BASS_GetDeviceInfo.restype = ctypes.c_bool

    @classmethod
    def _load_plugins(cls):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
        arch_dir = "x64" if platform.architecture()[0] == "64bit" else ""
        plugin_candidates = []
        for name, folder in (("bassflac.dll", "bassflac24"), ("bassmix.dll", "bassmix24")):
            if arch_dir:
                plugin_candidates.append(os.path.join(root, "vendor", "bass", "bin", arch_dir, name))
                plugin_candidates.append(os.path.join(root, "vendor", "bass", folder, arch_dir, name))
            plugin_candidates.append(os.path.join(root, "vendor", "bass", "bin", name))
            plugin_candidates.append(os.path.join(root, "vendor", "bass", folder, name))
        for path in plugin_candidates:
            if os.path.isfile(path):
                if hasattr(os, "add_dll_directory"):
                    os.add_dll_directory(os.path.dirname(path))
                cls.bass.BASS_PluginLoad(path, BASS_UNICODE)


class _StreamOwnership(enum.Enum):
    """PreparedBassStream's exactly-once resource-ownership state.
    CANDIDATE: the PreparedBassStream exclusively owns the HSTREAM.
    TRANSFERRED: ownership has moved to a BassPlayer (via take()); the
    candidate's own handle is zeroed and it must never act on it again.
    DISCARDED: the candidate itself freed the HSTREAM (via discard());
    same "handle zeroed, never act again" guarantee, kept as a distinct
    value from TRANSFERRED so a bug that confuses "someone else now owns
    this" with "this was freed" is structurally impossible to write."""
    CANDIDATE = "candidate"
    TRANSFERRED = "transferred"
    DISCARDED = "discarded"


class PreparedBassStreamPayload:
    """Plain values returned by PreparedBassStream.take() -- no
    ownership semantics of its own. Once returned, the caller (always
    BassPlayer.commit_prepared) is unconditionally responsible for
    `handle`."""
    __slots__ = ("handle", "identity", "length", "url_buffer")

    def __init__(self, handle, identity, length, url_buffer):
        self.handle = handle
        self.identity = identity
        self.length = length
        self.url_buffer = url_buffer


class PreparedBassStream:
    """A BASS stream handle created off the GUI thread by
    BassPlayer.prepare_stream(), not yet committed to any BassPlayer.

    Phase C1 (native audio backend ownership, 2026-09-10): the
    architectural fix for the confirmed hazard where a stale worker's
    completed load could mutate a shared, possibly-now-authoritative
    BassPlayer before any Python-level attempt/token check ever ran
    (callback rejection alone cannot prevent a native mutation that
    already happened before the callback fires). This class makes that
    structurally impossible: preparation never touches a BassPlayer
    instance at all, so a stale worker's finished candidate has nothing
    to corrupt -- only its own private HSTREAM, discarded harmlessly if
    it turns out to be unwanted.

    Exactly one of take()/discard() may ever resolve a given instance,
    enforced by _ownership rather than by caller convention -- see both
    methods below. Both callers are expected to run on the GUI thread
    only (the single place PlayerWindow's async load paths resolve a
    candidate's fate), so there is no cross-thread contention on
    _ownership itself; the state machine exists to make double-free and
    "nobody owns it"/"two owners" impossible even under a caller bug or
    an exception at an inconvenient point, not to add its own locking.
    """

    def __init__(self, handle, identity, length, url_buffer=None):
        self._handle = handle
        self._identity = identity
        self._length = length
        self._url_buffer = url_buffer
        self._ownership = _StreamOwnership.CANDIDATE

    @property
    def ownership(self) -> _StreamOwnership:
        return self._ownership

    def take(self) -> PreparedBassStreamPayload:
        """CANDIDATE -> TRANSFERRED, exactly once. Returns the plain
        values a BassPlayer needs to adopt; the caller becomes the sole
        owner of the returned handle the instant this returns, and this
        candidate can never be asked to free it (discard() below is a
        no-op once TRANSFERRED). Raises RuntimeError if this candidate
        was already resolved -- a programming-error guard: production
        only ever calls this once, from BassPlayer.commit_prepared,
        after confirming `ownership is CANDIDATE`.

        2026-09-11 exception-safety correction: the payload is
        constructed FIRST, while this candidate unquestionably still
        owns the handle -- only once that construction has succeeded
        does ownership actually transfer (_ownership set to TRANSFERRED,
        _handle/_url_buffer cleared). If PreparedBassStreamPayload(...)
        itself were to raise, an ordering that flipped _ownership first
        would strand the handle: TRANSFERRED with no receiver ever
        having obtained it, and discard() afterward a no-op because it
        no longer believes it owns anything. With this ordering, a
        raise here leaves the candidate exactly as it was -- still
        CANDIDATE, still owning its handle -- so a caller's discard()
        remains correct and sufficient."""
        if self._ownership is not _StreamOwnership.CANDIDATE:
            raise RuntimeError(
                f"PreparedBassStream.take() called while ownership={self._ownership.value}"
            )
        payload = PreparedBassStreamPayload(
            self._handle, self._identity, self._length, self._url_buffer,
        )
        self._ownership = _StreamOwnership.TRANSFERRED
        self._handle = 0
        self._url_buffer = None
        return payload

    def discard(self) -> bool:
        """CANDIDATE -> DISCARDED. Frees ONLY this candidate's own
        HSTREAM -- never touches any BassPlayer instance, never calls
        BassPlayer.stop(). The handle is detached/zeroed FIRST, before
        the native free call, so a re-entrant or duplicate discard()
        (or a discard() after take()) cannot ever double-free; safe to
        call more than once. Returns True if there was nothing to free
        or BASS reported success; False if BASS_StreamFree reported
        failure or raised -- this module stays dependency-light (no
        diagnostics coupling), so recording that as a diagnostic, if
        wanted, is the caller's job (window.py already has a
        diagnostics object and already records one for every other
        candidate-rejection path)."""
        if self._ownership is not _StreamOwnership.CANDIDATE:
            return True
        handle = self._handle
        self._handle = 0
        self._url_buffer = None
        self._ownership = _StreamOwnership.DISCARDED
        if not handle:
            return True
        try:
            ok = _BassEngine.ensure().BASS_StreamFree(handle)
        except Exception:
            return False
        return bool(ok)


class BassPlayer:
    """Small BASS player facade used by PlayerWindow."""

    def __init__(self):
        self._path = ""
        self._stream = 0
        self._length = 0.0
        self._volume = 1.0
        self._paused = False
        # Stage 3A: kept alive for the full lifetime of a URL-backed
        # stream. Verified empirically that BASS still played correctly
        # after this exact buffer was dereferenced+GC'd in a standalone
        # audit, but that result does not reliably prove BASS copies the
        # buffer internally (Python's allocator does not guarantee freed
        # memory is overwritten immediately) -- per explicit instruction
        # not to guess here, this reference is held regardless, cleared
        # only once the stream itself is freed in stop().
        self._url_buffer = None

    def load(self, path):
        """`path` is either a local filesystem path (str, existing
        behaviour, completely unchanged) or a Stage 3A
        PlexTransportSource (Plex Direct Play audio) -- BASS_StreamCreateURL
        instead of BASS_StreamCreateFile, with the token (if any) carried
        as a real HTTP header, never in the URL text. self._path is
        always the LOGICAL identity in the Plex case (source.identity),
        never the tokenised transport URL -- stats()/diagnostics/anything
        that reads self._path must only ever see the stable identity."""
        self.stop()
        bass = _BassEngine.ensure()
        from .plex_transport import PlexTransportSource
        if isinstance(path, PlexTransportSource):
            url_and_headers = path.transport_url
            if path.extra_headers:
                for name, value in path.extra_headers.items():
                    url_and_headers += f"\r\n{name}: {value}"
                url_and_headers += "\r\n"
            self._url_buffer = ctypes.create_string_buffer(url_and_headers.encode("utf-8"))
            stream = bass.BASS_StreamCreateURL(
                ctypes.cast(self._url_buffer, ctypes.c_char_p), 0, 0, None, None,
            )
            if not stream:
                self._url_buffer = None
                raise BassLoadError(f"BASS_StreamCreateURL failed: {_BassEngine.error_code()}")
            self._path = path.identity
        else:
            flags = BASS_UNICODE | BASS_STREAM_PRESCAN
            stream = bass.BASS_StreamCreateFile(False, os.path.abspath(path), 0, 0, flags)
            if not stream:
                raise BassLoadError(f"BASS_StreamCreateFile failed: {_BassEngine.error_code()}")
            self._path = path
        self._stream = int(stream)
        self._paused = False
        self.set_volume(self._volume)
        length_bytes = bass.BASS_ChannelGetLength(self._stream, BASS_POS_BYTE)
        self._length = float(bass.BASS_ChannelBytes2Seconds(self._stream, length_bytes)) if length_bytes else 0.0

    @staticmethod
    def prepare_stream(source) -> PreparedBassStream:
        """Phase C1: the off-GUI-thread-safe half of what load() used to
        do in one step. Creates a new BASS stream handle WITHOUT
        touching any BassPlayer instance's state -- no self.stop(), no
        self._stream/_path/_length/_url_buffer write. `source` is either
        a local filesystem path (str) or a Stage 3A PlexTransportSource,
        exactly like load()'s own `path` argument. Safe to call from any
        thread (a background candidate-preparation worker, in practice)
        -- the only shared state it touches is _BassEngine's own
        one-time init, itself made safe for concurrent first use by
        _init_lock above. The returned PreparedBassStream exclusively
        owns the new handle until a BassPlayer commits or discards it
        (see PreparedBassStream and BassPlayer.commit_prepared)."""
        bass = _BassEngine.ensure()
        from .plex_transport import PlexTransportSource
        handle = 0
        url_buffer = None
        try:
            if isinstance(source, PlexTransportSource):
                url_and_headers = source.transport_url
                if source.extra_headers:
                    for name, value in source.extra_headers.items():
                        url_and_headers += f"\r\n{name}: {value}"
                    url_and_headers += "\r\n"
                url_buffer = ctypes.create_string_buffer(url_and_headers.encode("utf-8"))
                handle = bass.BASS_StreamCreateURL(
                    ctypes.cast(url_buffer, ctypes.c_char_p), 0, 0, None, None,
                )
                if not handle:
                    raise BassLoadError(f"BASS_StreamCreateURL failed: {_BassEngine.error_code()}")
                identity = source.identity
            else:
                flags = BASS_UNICODE | BASS_STREAM_PRESCAN
                handle = bass.BASS_StreamCreateFile(False, os.path.abspath(source), 0, 0, flags)
                if not handle:
                    raise BassLoadError(f"BASS_StreamCreateFile failed: {_BassEngine.error_code()}")
                identity = source
            length_bytes = bass.BASS_ChannelGetLength(handle, BASS_POS_BYTE)
            length = float(bass.BASS_ChannelBytes2Seconds(handle, length_bytes)) if length_bytes else 0.0
            candidate = PreparedBassStream(handle, identity, length, url_buffer)
            handle = 0  # ownership transferred to `candidate` -- the
                        # finally block below must leave it alone now
            return candidate
        finally:
            if handle:
                # BASS_StreamCreateURL/File succeeded but something
                # after it raised before a PreparedBassStream existed to
                # own the handle (e.g. BASS_ChannelGetLength) -- free it
                # here, exactly once, since nothing else will.
                try:
                    bass.BASS_StreamFree(handle)
                except Exception:
                    pass

    def commit_prepared(self, candidate: PreparedBassStream) -> bool:
        """GUI-thread only. The other half of the Phase C1 split: adopts
        a still-valid candidate's handle, becoming its sole owner.
        Returns False (candidate untouched) if the candidate was already
        resolved by someone else -- should never happen given the
        single-GUI-thread-resolves-a-candidate convention, but checked
        rather than assumed.

        Exception-safety ordering matters here: the OLD stream is
        stopped/freed BEFORE the candidate's take() runs, while the
        candidate still owns its new handle -- so if stop() itself
        raises, the candidate has not yet relinquished anything, and a
        plain discard() is correct and sufficient. Once take() has run,
        self._stream already points at the new handle and THIS PLAYER,
        not the candidate, is responsible for cleaning it up if anything
        after adoption fails. There is no path where nobody owns the new
        handle, and no path where both this player and the candidate
        believe they own it."""
        if candidate.ownership is not _StreamOwnership.CANDIDATE:
            return False
        try:
            self.stop()
        except Exception:
            candidate.discard()
            raise
        payload = candidate.take()
        self._stream = payload.handle
        self._path = payload.identity
        self._length = payload.length
        self._url_buffer = payload.url_buffer
        self._paused = False
        try:
            self.set_volume(self._volume)
        except Exception:
            # Adoption already completed (self._stream is the new
            # handle) -- this player, now the sole owner, cleans up its
            # own stream; the candidate was already resolved by take()
            # above and must never be touched again.
            self.stop()
            raise
        return True

    def play(self):
        if self._stream:
            if not _BassEngine.ensure().BASS_ChannelPlay(self._stream, False):
                raise BassLoadError(f"BASS_ChannelPlay failed: {_BassEngine.error_code()}")
            self._paused = False

    def pause(self):
        if self._stream:
            _BassEngine.ensure().BASS_ChannelPause(self._stream)
            self._paused = True

    def resume(self):
        self.play()

    def stop(self):
        if self._stream:
            bass = _BassEngine.ensure()
            try:
                bass.BASS_ChannelStop(self._stream)
                bass.BASS_StreamFree(self._stream)
            finally:
                self._stream = 0
                self._url_buffer = None
        self._paused = False

    def close(self):
        self.stop()
        self._path = ""

    def seek(self, seconds: float):
        if not self._stream:
            return
        bass = _BassEngine.ensure()
        seconds = max(0.0, min(float(seconds or 0.0), self._length or float("inf")))
        pos = bass.BASS_ChannelSeconds2Bytes(self._stream, seconds)
        bass.BASS_ChannelSetPosition(self._stream, pos, BASS_POS_BYTE)

    def set_volume(self, value: float):
        self._volume = max(0.0, min(1.0, float(value)))
        if self._stream:
            _BassEngine.ensure().BASS_ChannelSetAttribute(
                self._stream, BASS_ATTRIB_VOL, ctypes.c_float(self._volume)
            )

    def slide_volume(self, value: float, seconds: float):
        """Let BASS ramp volume on its audio thread for smooth crossfades."""
        self._volume = max(0.0, min(1.0, float(value)))
        if self._stream:
            millis = max(1, int(float(seconds or 0.0) * 1000))
            ok = _BassEngine.ensure().BASS_ChannelSlideAttribute(
                self._stream, BASS_ATTRIB_VOL, ctypes.c_float(self._volume), millis
            )
            if not ok:
                raise BassLoadError(f"BASS_ChannelSlideAttribute failed: {_BassEngine.error_code()}")
            return True
        return False

    def get_pos(self):
        if not self._stream:
            return 0.0
        bass = _BassEngine.ensure()
        pos = bass.BASS_ChannelGetPosition(self._stream, BASS_POS_BYTE)
        return float(bass.BASS_ChannelBytes2Seconds(self._stream, pos)) if pos else 0.0

    def get_length(self):
        return self._length

    def stats(self):
        return {
            "path": self._path,
            "duration": self._length,
            "sample_rate": 0,
            "channels": 0,
            "position": self.get_pos(),
            "ended": False,
        }

    def is_playing(self):
        if not self._stream:
            return False
        return _BassEngine.ensure().BASS_ChannelIsActive(self._stream) == BASS_ACTIVE_PLAYING

    def buffering_state(self) -> str:
        """"connecting" / "buffering" / "ready" / "paused" / "stopped" --
        derived entirely from BASS's own existing BASS_ChannelIsActive()
        state (BASS_ACTIVE_STALLED already means "still buffering,
        waiting for more network data" -- this is not a new signal, just
        exposing one BASS already reports). No global BASS buffering
        configuration is read or changed here -- see the Stage 3A
        investigation note in CODEX_HANDOFF.md for why that was left
        alone. Never a fabricated percentage; local (non-URL) playback
        never reports "connecting"/"buffering" since a local stream is
        never BASS_ACTIVE_STALLED under normal conditions."""
        if not self._stream:
            return "stopped"
        state = _BassEngine.ensure().BASS_ChannelIsActive(self._stream)
        if state == BASS_ACTIVE_STALLED:
            return "buffering"
        if state == BASS_ACTIVE_PAUSED:
            return "paused"
        if state == BASS_ACTIVE_PLAYING:
            return "ready"
        return "stopped"

    def get_fft_levels(self, num_bars: int = 32) -> Optional[list]:
        """Stage 3A visualiser fix: `num_bars` normalised (0..1) levels
        read live from this channel's own decoded audio, via BASS's own
        BASS_ChannelGetData(..., BASS_DATA_FFT2048) -- works identically
        for a local BASS_StreamCreateFile channel and a remote Plex
        BASS_StreamCreateURL one, because BASS itself doesn't distinguish
        them once the stream is open: this call only ever reads from
        audio BASS has already decoded into its own internal buffer for
        playback, never triggers any decode or network I/O of its own.
        This is deliberately a SEPARATE path from the offline, whole-file
        AudioAnalyzer.load()/get_levels() used for local playback's own
        visualiser -- that system needs a real local file to pre-decode,
        which a remote plex:// identity never has; this one needs nothing
        but an already-open, already-playing BASS channel, Local or Plex
        alike. Returns None when there is no active stream or the read
        genuinely fails (e.g. nothing decoded yet) -- callers should treat
        that exactly like AudioAnalyzer.get_levels() returning None."""
        if not self._stream:
            return None
        bass = _BassEngine.ensure()
        buf = (ctypes.c_float * _FFT2048_BIN_COUNT)()
        result = bass.BASS_ChannelGetData(self._stream, buf, BASS_DATA_FFT2048)
        if result < 0:
            return None
        bins = list(buf)
        levels = []
        for i in range(num_bars):
            # Exponential (log-like) bucket edges from bin 1 to the top
            # bin -- low bars cover a handful of bins (bass frequencies,
            # perceptually spread wide), high bars cover hundreds (treble,
            # perceptually compressed) -- the same shape a mel/log-
            # frequency spacing produces, without needing a mel filterbank
            # for just this.
            lo = max(1, int(round(_FFT2048_BIN_COUNT ** (i / num_bars))))
            hi = max(lo + 1, int(round(_FFT2048_BIN_COUNT ** ((i + 1) / num_bars))))
            hi = min(hi, _FFT2048_BIN_COUNT)
            band = bins[lo:hi] or [0.0]
            magnitude = max(band)
            # BASS's FFT magnitudes are linear amplitude ratios, typically
            # well under 1.0 for any single bin once energy is spread
            # across 1024 of them -- a dB scale with the same floor
            # AudioAnalyzer already uses keeps this visually comparable
            # rather than looking uniformly near-silent.
            db = 20.0 * math.log10(magnitude) if magnitude > 1e-9 else _DB_FLOOR
            db = max(_DB_FLOOR, min(0.0, db))
            levels.append((db - _DB_FLOOR) / -_DB_FLOOR)
        return levels

    @property
    def _paused(self):
        return self.__paused

    @_paused.setter
    def _paused(self, value):
        self.__paused = bool(value)


def bass_device_hash() -> str:
    """Anonymised (sha256, truncated) identity of BASS's actual selected
    output device, for the classic-video-silence investigation's section 7
    Qt-vs-BASS device comparison -- callers compare this against the
    video child's own device_hash (see video_subprocess.py's _hash_label,
    the same hashing convention, so equal device names always produce
    equal hashes). Returns "" (never raises, even if
    current_device_description() itself somehow does) if BASS isn't
    initialised or the query fails for any reason."""
    try:
        description = _BassEngine.current_device_description()
    except Exception:
        return ""
    if not description:
        return ""
    return hashlib.sha256(description.encode("utf-8", "replace")).hexdigest()[:16]
