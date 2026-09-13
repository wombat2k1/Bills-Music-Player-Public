"""QueueAnalysisWorker must not begin BPM/key analysis until the librosa
warm-up (billsmusic/analysis_warmup.py) has completed or failed -- these
tests exercise that synchronization directly rather than timing-racing the
real, expensive warm-up (real librosa.load + real numba compilation)."""
import threading

import billsmusic.analysis_warmup as analysis_warmup
import billsmusic.workers as workers


def _reset_module_state(monkeypatch):
    monkeypatch.setattr(analysis_warmup, "_started", False)
    monkeypatch.setattr(analysis_warmup, "_ready_event", threading.Event())
    monkeypatch.setattr(analysis_warmup, "_succeeded", False)
    monkeypatch.setattr(analysis_warmup, "_tempo_function", None)
    monkeypatch.setattr(analysis_warmup, "_mutagen_succeeded", False)
    monkeypatch.setattr(analysis_warmup, "_mutagen_error", "")
    monkeypatch.setattr(analysis_warmup, "_librosa_elapsed_ms", 0.0)
    monkeypatch.setattr(analysis_warmup, "_mutagen_elapsed_ms", 0.0)
    monkeypatch.setattr(analysis_warmup, "_pychromecast_succeeded", False)
    monkeypatch.setattr(analysis_warmup, "_pychromecast_error", "")
    monkeypatch.setattr(analysis_warmup, "_pychromecast_elapsed_ms", 0.0)


def test_wait_until_ready_blocks_until_the_warmup_thread_finishes(monkeypatch):
    _reset_module_state(monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(
        analysis_warmup, "_warm_up_librosa", lambda: release.wait(5) or True,
    )

    results = []

    def _waiter():
        results.append(analysis_warmup.wait_until_ready(timeout=5))

    waiter_thread = threading.Thread(target=_waiter)
    waiter_thread.start()
    waiter_thread.join(0.2)
    assert results == []  # still genuinely blocked, not a timing bet

    release.set()
    waiter_thread.join(5)

    assert results == [True]
    assert analysis_warmup.succeeded() is True


def test_wait_until_ready_unblocks_on_warmup_failure_not_just_success(monkeypatch):
    _reset_module_state(monkeypatch)

    def _failing_warm_up():
        raise RuntimeError("boom")

    monkeypatch.setattr(analysis_warmup, "_warm_up_librosa", _failing_warm_up)

    ready = analysis_warmup.wait_until_ready(timeout=5)

    assert ready is True  # "completed or failed" -- failure still unblocks
    assert analysis_warmup.succeeded() is False


def test_run_synchronously_initialises_on_the_calling_thread(monkeypatch):
    _reset_module_state(monkeypatch)
    caller_thread = threading.get_ident()
    warmup_threads = []

    def _warm_up():
        warmup_threads.append(threading.get_ident())
        return True

    monkeypatch.setattr(analysis_warmup, "_warm_up_librosa", _warm_up)

    assert analysis_warmup.run_synchronously() is True
    assert warmup_threads == [caller_thread]
    assert analysis_warmup.is_ready() is True


def test_analyse_track_blocks_on_warmup_before_decoding(monkeypatch):
    monkeypatch.setattr(workers, "np", object())  # non-None to pass the early guard
    # The warmup wait is gated on LIBROSA_AVAILABLE (see _analyse_track) and
    # only the librosa decode branch (_decode_preview, stubbed below) is
    # under test here -- force it regardless of whether librosa is
    # actually importable in this environment, otherwise the warmup wait
    # is skipped entirely and the real _decode_native_audio_preview runs
    # instead of the stub.
    monkeypatch.setattr(workers, "LIBROSA_AVAILABLE", True)
    calls = []
    monkeypatch.setattr(
        analysis_warmup, "wait_until_ready",
        lambda timeout=None: calls.append("waited") or True,
    )
    monkeypatch.setattr(analysis_warmup, "succeeded", lambda: True)
    worker = workers.QueueAnalysisWorker()
    worker._analyse_metadata = lambda path: {}
    worker._decode_preview = lambda path: (None, 0)

    worker._analyse_track("song.flac")

    assert calls == ["waited"]


def test_analyse_track_skips_librosa_entirely_when_warmup_failed(monkeypatch):
    monkeypatch.setattr(workers, "np", object())
    monkeypatch.setattr(analysis_warmup, "wait_until_ready", lambda timeout=None: True)
    monkeypatch.setattr(analysis_warmup, "succeeded", lambda: False)
    worker = workers.QueueAnalysisWorker()
    worker._analyse_metadata = lambda path: {"time": "3:00"}

    def _must_not_be_called(path):
        raise AssertionError("must not decode when warm-up failed")

    worker._decode_preview = _must_not_be_called

    result = worker._analyse_track("song.flac")

    assert result == {"time": "3:00"}


def test_run_warms_up_mutagen_even_when_librosa_warmup_fails(monkeypatch):
    _reset_module_state(monkeypatch)
    monkeypatch.setattr(
        analysis_warmup, "_warm_up_librosa",
        lambda: (_ for _ in ()).throw(RuntimeError("librosa unavailable")),
    )
    mutagen_calls = []
    monkeypatch.setattr(
        analysis_warmup, "_warm_up_mutagen", lambda: mutagen_calls.append(1),
    )

    analysis_warmup._run()

    assert mutagen_calls == [1]
    assert analysis_warmup.succeeded() is False  # librosa's own outcome, unaffected
    assert analysis_warmup.is_ready() is True


def test_run_does_not_fail_the_whole_warmup_if_mutagen_warmup_raises(monkeypatch):
    _reset_module_state(monkeypatch)
    monkeypatch.setattr(analysis_warmup, "_warm_up_librosa", lambda: True)
    monkeypatch.setattr(
        analysis_warmup, "_warm_up_mutagen",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    analysis_warmup._run()

    assert analysis_warmup.succeeded() is True  # librosa's own outcome, unaffected
    assert analysis_warmup.is_ready() is True


def test_warm_up_mutagen_imports_every_format_submodule_file_touches():
    # mutagen.File()'s own default options=None imports this exact set
    # (see mutagen/_file.py) regardless of which single file type is
    # actually being opened -- the crash this closes (crash.log,
    # 2026-08-05) surfaced inside mutagen.mp4, mutagen.flac, and
    # mutagen.ogg on different runs, all from this same cascade.
    import sys
    for name in (
        "mutagen.mp3", "mutagen.mp4", "mutagen.flac", "mutagen.id3",
        "mutagen.oggvorbis", "mutagen.oggflac", "mutagen.oggopus",
        "mutagen.oggspeex", "mutagen.oggtheora", "mutagen.wavpack",
        "mutagen.aac", "mutagen.ac3", "mutagen.aiff", "mutagen.asf",
        "mutagen.apev2", "mutagen.musepack", "mutagen.monkeysaudio",
        "mutagen.optimfrog", "mutagen.wave",
    ):
        sys.modules.pop(name, None)

    analysis_warmup._warm_up_mutagen()

    for name in (
        "mutagen.mp3", "mutagen.mp4", "mutagen.flac", "mutagen.id3",
        "mutagen.oggvorbis", "mutagen.oggflac", "mutagen.oggopus",
        "mutagen.oggspeex", "mutagen.oggtheora", "mutagen.wavpack",
        "mutagen.aac", "mutagen.ac3", "mutagen.aiff", "mutagen.asf",
        "mutagen.apev2", "mutagen.musepack", "mutagen.monkeysaudio",
        "mutagen.optimfrog", "mutagen.wave",
    ):
        assert name in sys.modules, f"{name} was not warmed up"


def test_mutagen_warmup_failure_is_recorded_clearly_not_swallowed_silently(monkeypatch):
    _reset_module_state(monkeypatch)
    monkeypatch.setattr(analysis_warmup, "_warm_up_librosa", lambda: True)
    monkeypatch.setattr(
        analysis_warmup, "_warm_up_mutagen",
        lambda: (_ for _ in ()).throw(RuntimeError("disk full")),
    )

    analysis_warmup._run()

    # The app must be able to tell it's in a partially-warmed state --
    # librosa succeeded, mutagen didn't -- rather than this being an
    # invisible, silently-swallowed failure.
    assert analysis_warmup.mutagen_succeeded() is False
    assert "disk full" in analysis_warmup._mutagen_error
    assert analysis_warmup.fully_warmed() is False
    assert analysis_warmup.succeeded() is True  # librosa's own result is unaffected


def test_fully_warmed_requires_both_components_to_succeed(monkeypatch):
    _reset_module_state(monkeypatch)
    monkeypatch.setattr(analysis_warmup, "_warm_up_librosa", lambda: True)
    monkeypatch.setattr(analysis_warmup, "_warm_up_mutagen", lambda: None)

    analysis_warmup._run()

    assert analysis_warmup.succeeded() is True
    assert analysis_warmup.mutagen_succeeded() is True
    assert analysis_warmup.fully_warmed() is True


def test_report_includes_versions_timings_and_outcomes(monkeypatch):
    _reset_module_state(monkeypatch)
    monkeypatch.setattr(analysis_warmup, "_warm_up_librosa", lambda: True)
    monkeypatch.setattr(analysis_warmup, "_warm_up_mutagen", lambda: None)

    analysis_warmup._run()
    report = analysis_warmup.report()

    assert report["ready"] is True
    assert report["fully_warmed"] is True
    assert report["librosa_succeeded"] is True
    assert report["mutagen_succeeded"] is True
    assert report["mutagen_error"] == ""
    assert isinstance(report["librosa_elapsed_ms"], float)
    assert isinstance(report["mutagen_elapsed_ms"], float)
    assert isinstance(report["pychromecast_elapsed_ms"], float)
    assert report["mutagen_version"] != "unknown"
    assert report["python_executable"]
    assert report["python_version"]


def test_run_warms_up_pychromecast_independently_of_librosa_and_mutagen(monkeypatch):
    # Reported: CastDiscoveryService._discover()'s lazy "import
    # pychromecast" -- the first anywhere in the process -- access
    # violated inside charset_normalizer (a compiled extension requests
    # pulls in transitively) when it happened on that background
    # discovery thread instead of during this early, synchronous warm-up
    # (crash.log, 2026-08-09).
    _reset_module_state(monkeypatch)
    monkeypatch.setattr(
        analysis_warmup, "_warm_up_librosa",
        lambda: (_ for _ in ()).throw(RuntimeError("librosa unavailable")),
    )
    monkeypatch.setattr(
        analysis_warmup, "_warm_up_mutagen",
        lambda: (_ for _ in ()).throw(RuntimeError("mutagen unavailable")),
    )
    pychromecast_calls = []
    monkeypatch.setattr(
        analysis_warmup, "_warm_up_pychromecast",
        lambda: pychromecast_calls.append(1),
    )

    analysis_warmup._run()

    assert pychromecast_calls == [1]
    assert analysis_warmup.pychromecast_succeeded() is True
    assert analysis_warmup.is_ready() is True


def test_pychromecast_warmup_failure_is_recorded_clearly_not_swallowed_silently(monkeypatch):
    _reset_module_state(monkeypatch)
    monkeypatch.setattr(analysis_warmup, "_warm_up_librosa", lambda: True)
    monkeypatch.setattr(analysis_warmup, "_warm_up_mutagen", lambda: None)
    monkeypatch.setattr(
        analysis_warmup, "_warm_up_pychromecast",
        lambda: (_ for _ in ()).throw(RuntimeError("access violation")),
    )

    analysis_warmup._run()

    assert analysis_warmup.pychromecast_succeeded() is False
    assert "access violation" in analysis_warmup._pychromecast_error
    # Unrelated components' own results are unaffected.
    assert analysis_warmup.succeeded() is True
    assert analysis_warmup.mutagen_succeeded() is True


def test_warm_up_pychromecast_actually_imports_the_module():
    import sys
    sys.modules.pop("pychromecast", None)

    analysis_warmup._warm_up_pychromecast()

    assert "pychromecast" in sys.modules


def test_report_surfaces_mutagen_failure_details():
    import billsmusic.analysis_warmup as module

    module._mutagen_succeeded = False
    module._mutagen_error = "RuntimeError: boom"
    try:
        report = module.report()
        assert report["mutagen_succeeded"] is False
        assert report["mutagen_error"] == "RuntimeError: boom"
    finally:
        module._mutagen_succeeded = True
        module._mutagen_error = ""


def test_bpm_estimator_uses_the_warmed_callable_not_librosa_beat(monkeypatch):
    calls = []
    monkeypatch.setattr(workers, "LIBROSA_AVAILABLE", True)
    monkeypatch.setattr(
        analysis_warmup,
        "tempo",
        lambda *, y, sr, aggregate: calls.append((sr, aggregate)) or workers.np.array([121.0]),
    )

    worker = workers.QueueAnalysisWorker()
    bpm = worker._estimate_bpm(workers.np.ones(22050, dtype="float32"), 22050)

    assert bpm == 121.0
    assert calls == [(22050, None)]
