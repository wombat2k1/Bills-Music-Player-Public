"""Deterministic, one-time warm-up of librosa's and mutagen's lazily-
imported (and, for librosa, lazily-JIT-compiled) machinery.

librosa lazily imports scipy.signal/scipy.stats (~1.2s) the first time
`librosa.load` is actually used -- not at `import librosa` time. Separately,
`librosa.beat`/`librosa.feature` wrap numba-jitted functions that only
compile machine code the first time they're actually CALLED with real typed
arguments -- merely referencing the attribute (e.g. `librosa.beat.tempo`)
imports the module but does not trigger that compilation. Either happening
on a background analysis thread while other native-threaded work runs
concurrently has been observed to access-violate (crash.log, 2026-08-05).

mutagen.File() with its default `options=None` imports several dozen
format submodules (id3, mp3, mp4, flac, every ogg codec, wavpack, aiff,
aac, ac3, ...) as part of its own auto-detection setup -- the FIRST time
it's called anywhere in the process, regardless of which file type is
actually being opened, since it has to be able to score every candidate
format against the file's header. That first import has also been observed
access-violating (crash.log, 2026-08-05: separate runs crashed inside
mutagen.mp4, mutagen.flac, and mutagen.ogg -- three different-looking
crashes that are actually the exact same import cascade, just interrupted
at a different point in it each time).

`import pychromecast` pulls in `requests`, which lazily resolves
`charset_normalizer` (a compiled extension) the first time anything
imports `requests.compat` -- same hazard again. CastDiscoveryService
does this same import lazily, on a background discovery thread, the
first time the user opens the Cast device selector; a captured crash.log
(2026-08-09) showed exactly that: an access violation inside
charset_normalizer, triggered from `cast_service.py`'s discovery thread
importing pychromecast for the first time in the process.

IMPORTANT -- what this evidence does and doesn't establish: the crash
location and the first-use trigger are confirmed (a fresh interpreter,
mutagen.File() never called before, an access violation inside whatever
submodule the cascade happened to be importing). What is NOT established
is *why* a normal, GIL-protected, import-lock-serialised Python import
produces a native access violation, or whether some earlier, unrelated
memory corruption is what actually made this the code that faulted rather
than something else nearby. Warming these caches up before any worker
thread exists removes the specific *timing window* in which that fault has
been observed to happen -- treat it as a defensive mitigation against a
confirmed trigger, not a proven fix for a fully understood root cause,
until it's held up under further real-world runtime validation.

This module runs both -- a real decode via librosa.load on a generated tiny
WAV plus real calls to the beat/feature functions with real arrays, and a
call to mutagen.File() on an empty in-memory file (so the import list is
whatever the *installed* mutagen version's own File() actually imports,
not a manually maintained copy of it that could drift out of sync after a
dependency upgrade) -- once. Normal application startup calls
run_synchronously() after the splash has painted but before PlayerWindow
(and therefore any application worker, and any GUI-thread mutagen.File()
call) is created -- true for both a source checkout and a packaged
Nuitka/PyInstaller build, since both go through this same Main.py entry
point. QueueAnalysisWorker still blocks on wait_until_ready() as a second
line of defence for tests and non-standard launch paths.
"""
from __future__ import annotations

import importlib
import io
import os
import sys
import tempfile
import threading
import time

from .audio import LIBROSA_AVAILABLE, librosa, np, sf

_ready_event = threading.Event()
_start_lock = threading.Lock()
_started = False
_succeeded = False
_tempo_function = None

_mutagen_succeeded = False
_mutagen_error = ""
_librosa_elapsed_ms = 0.0
_mutagen_elapsed_ms = 0.0

_pychromecast_succeeded = False
_pychromecast_error = ""
_pychromecast_elapsed_ms = 0.0

_requests_succeeded = False
_requests_error = ""
_requests_elapsed_ms = 0.0


def start() -> None:
    """Idempotent -- call once, as early as possible, before any analysis
    worker thread exists. Safe to call from any thread."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(
        target=_run, name="AnalysisLibraryWarmup", daemon=True
    ).start()


def run_synchronously() -> bool:
    """Complete warm-up in the calling thread and return whether it worked.

    If another caller already started the asynchronous compatibility path,
    wait for that one instead of running the native initialisation twice.
    The normal launcher uses this before constructing PlayerWindow so Numba,
    LLVM, scipy, librosa and mutagen's format submodules cannot initialise
    alongside Qt/audio workers.
    """
    global _started
    with _start_lock:
        already_started = _started
        if not already_started:
            _started = True
    if already_started:
        _ready_event.wait()
    else:
        _run()
    return _succeeded


def wait_until_ready(timeout: float = None) -> bool:
    """Blocks the calling thread until warm-up has completed or failed.
    If start() was never called, this blocks forever (or until timeout) --
    callers that might race startup should call start() themselves first,
    which is safe and idempotent."""
    start()
    return _ready_event.wait(timeout)


def is_ready() -> bool:
    return _ready_event.is_set()


def succeeded() -> bool:
    return _succeeded


def mutagen_succeeded() -> bool:
    return _mutagen_succeeded


def pychromecast_succeeded() -> bool:
    return _pychromecast_succeeded


def requests_succeeded() -> bool:
    return _requests_succeeded


def fully_warmed() -> bool:
    """True only if every warmed component succeeded. False means the
    process is running in a partially-warmed state -- callers that want to
    know whether *anything* still needs defensive handling downstream
    should check this rather than succeeded() alone."""
    return _succeeded and _mutagen_succeeded


def report() -> dict:
    """Everything needed to log a clear, complete record of what warm-up
    did and didn't accomplish -- librosa/mutagen outcomes, timings, and
    enough environment detail (mutagen version, Python executable and
    version) to tell which interpreter/venv this ran under."""
    try:
        import mutagen
        mutagen_version = getattr(mutagen, "version_string", None) or ".".join(
            str(part) for part in getattr(mutagen, "version", ())
        )
    except Exception:
        mutagen_version = "unknown"
    return {
        "ready": is_ready(),
        "fully_warmed": fully_warmed(),
        "librosa_succeeded": _succeeded,
        "librosa_elapsed_ms": round(_librosa_elapsed_ms, 1),
        "mutagen_succeeded": _mutagen_succeeded,
        "mutagen_elapsed_ms": round(_mutagen_elapsed_ms, 1),
        "mutagen_error": _mutagen_error,
        "mutagen_version": mutagen_version,
        "pychromecast_succeeded": _pychromecast_succeeded,
        "pychromecast_elapsed_ms": round(_pychromecast_elapsed_ms, 1),
        "pychromecast_error": _pychromecast_error,
        "requests_succeeded": _requests_succeeded,
        "requests_elapsed_ms": round(_requests_elapsed_ms, 1),
        "requests_error": _requests_error,
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
    }


def tempo(y, sr: int, aggregate=None):
    """Call the exact BPM function compiled during startup warm-up.

    Queue workers must never resolve ``librosa.beat`` (or any other lazy
    librosa module) themselves. That previously left a first-time Numba
    compile running on the worker thread despite a reported-successful
    warm-up, and Windows terminated the process with an access violation.
    """
    if not _ready_event.is_set() or not _succeeded or _tempo_function is None:
        raise RuntimeError("librosa tempo analysis was not warmed successfully")
    return _tempo_function(y=y, sr=sr, aggregate=aggregate)


def _run() -> None:
    global _succeeded, _librosa_elapsed_ms
    librosa_started = time.perf_counter()
    try:
        _succeeded = _warm_up_librosa()
    except Exception:
        _succeeded = False
    _librosa_elapsed_ms = (time.perf_counter() - librosa_started) * 1000.0

    # Independent of librosa's own success/failure -- mutagen has nothing
    # to do with librosa, it just happens to have the same "expensive
    # first-time import cascade" hazard.
    global _mutagen_succeeded, _mutagen_error, _mutagen_elapsed_ms
    mutagen_started = time.perf_counter()
    try:
        _warm_up_mutagen()
        _mutagen_succeeded = True
    except Exception as ex:
        _mutagen_succeeded = False
        _mutagen_error = f"{type(ex).__name__}: {ex}"
    _mutagen_elapsed_ms = (time.perf_counter() - mutagen_started) * 1000.0

    # Independent of librosa/mutagen -- pychromecast has nothing to do with
    # either, it just happens to have the same hazard via requests'
    # lazily-resolved charset_normalizer dependency.
    global _pychromecast_succeeded, _pychromecast_error, _pychromecast_elapsed_ms
    pychromecast_started = time.perf_counter()
    try:
        _warm_up_pychromecast()
        _pychromecast_succeeded = True
    except Exception as ex:
        _pychromecast_succeeded = False
        _pychromecast_error = f"{type(ex).__name__}: {ex}"
    _pychromecast_elapsed_ms = (time.perf_counter() - pychromecast_started) * 1000.0

    # Independent of pychromecast -- the Plex integration imports `requests`
    # directly and must not rely on Cast being installed/enabled for this
    # same protection to apply. Same hazard class as _warm_up_pychromecast
    # (which already pulls requests in transitively when Cast IS installed,
    # making this a cheap already-loaded no-op in that case); this call
    # exists so the warm-up still happens when Cast's optional dependencies
    # are absent but Plex is used.
    global _requests_succeeded, _requests_error, _requests_elapsed_ms
    requests_started = time.perf_counter()
    try:
        _warm_up_requests()
        _requests_succeeded = True
    except Exception as ex:
        _requests_succeeded = False
        _requests_error = f"{type(ex).__name__}: {ex}"
    _requests_elapsed_ms = (time.perf_counter() - requests_started) * 1000.0

    _ready_event.set()


def _warm_up_librosa() -> bool:
    global _tempo_function
    if not LIBROSA_AVAILABLE or np is None or sf is None:
        return False
    sr = 22050
    y = (np.random.default_rng(0).standard_normal(sr) * 0.05).astype("float32")

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        sf.write(tmp_path, y, sr)
        # Forces librosa.core.audio's lazy import (scipy.signal/scipy.stats)
        # by actually decoding a real file, matching real analysis use.
        librosa.load(tmp_path, sr=sr, mono=True)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    # Real calls with real arrays -- this is what actually triggers numba's
    # first-time JIT compilation, not merely referencing the attribute.
    try:
        rhythm = importlib.import_module("librosa.feature.rhythm")
        _tempo_function = rhythm.tempo
    except ImportError:  # compatibility with older librosa releases
        beat = importlib.import_module("librosa.beat")
        _tempo_function = beat.tempo
    _tempo_function(y=y, sr=sr, aggregate=None)
    librosa.feature.chroma_stft(y=y, sr=sr)
    return True


def _warm_up_mutagen() -> None:
    """Imports exactly what the *installed* mutagen version's own
    mutagen.File() already imports for its default auto-detection --
    calling it (on an empty in-memory file, so no file on disk is needed)
    rather than maintaining a manual module list here, so this can never
    drift out of sync with what a mutagen upgrade adds, removes, or
    renames. Confirmed against the real install this app ships with: this
    imports more submodules (43, including internal ones like
    mutagen.mp4._atom -- literally the module one of the observed crashes
    was inside) than an earlier hand-maintained list did.
    """
    from mutagen import File as MutagenFile
    MutagenFile(io.BytesIO(b""))


def _warm_up_pychromecast() -> None:
    """Imports pychromecast (and its requests/charset_normalizer/zeroconf/
    casttube dependency chain) once, here, on the main thread.

    CastDiscoveryService._discover() does this same "import pychromecast"
    lazily, the first time the user opens the Cast device selector -- but
    that happens on a background discovery thread. A captured crash.log
    (2026-08-09) confirmed a native access violation inside
    charset_normalizer when that first import happened there instead of
    here. Once imported here, Python's module cache makes every later
    "import pychromecast" (including the one in _discover()) a cheap,
    already-loaded no-op that never touches the native import path again.
    Pychromecast is an optional dependency (Cast support degrades
    gracefully without it) so ImportError is expected and not a failure
    of this warm-up in the sense the other two are.
    """
    import pychromecast  # noqa: F401


def _warm_up_requests() -> None:
    """Imports requests (and its lazily-resolved charset_normalizer compiled
    extension) once, here, on the main thread -- independent of
    pychromecast/Cast. The Plex integration (plex_client.py) uses requests
    directly and is not gated on Cast's optional dependencies being
    installed, so it needs this same protection on its own rather than
    relying on _warm_up_pychromecast() having already run it. requests is
    a real, always-installed dependency once Plex is enabled (declared in
    requirements-plex.txt), so unlike pychromecast this is not expected to
    raise ImportError in a normal install."""
    import requests  # noqa: F401
    import charset_normalizer  # noqa: F401
