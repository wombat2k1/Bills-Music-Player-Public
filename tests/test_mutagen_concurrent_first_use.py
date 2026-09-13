"""Regression test for the concurrent first-use import scenario that
preceded three access violations in crash.log (2026-08-05), inside
mutagen.mp4, mutagen.flac, and mutagen.ogg on separate runs.

What this test can and can't establish: an access violation from a native
memory-corruption race is not reliably forceable on demand -- if it were,
it would have shown up in previous test runs of this exact code long
before it showed up in real, hours-long usage. This test starts several
threads at once, all calling mutagen.File() on real representative files
for the first time in the process, immediately after simulating a cold
start (no mutagen format submodules imported yet) -- the same shape as
the real trigger. If run against the code *before* the analysis_warmup fix
(warm-up disabled below), this reproduces genuine concurrent first-import
activity across threads; it is not expected to reliably reproduce the
native fault itself on every machine or every run, and a clean pass here
must not be read as proof the fault can no longer occur -- only that the
warm-up measurably removes the specific concurrent-first-import window
that was present at every observed crash.
"""
import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

import billsmusic.analysis_warmup as analysis_warmup

_FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
_FIXTURE_FILES = [
    os.path.join(_FIXTURES_DIR, name)
    for name in ("sample.mp3", "sample.flac", "sample.mp4")  # mp4 stands in for m4a -- same container/parser
]


def _existing_fixtures():
    return [path for path in _FIXTURE_FILES if os.path.isfile(path)]


def _clear_mutagen_submodules():
    import sys
    for name in list(sys.modules):
        if name == "mutagen" or name.startswith("mutagen."):
            if name != "mutagen":
                del sys.modules[name]


def _hammer_mutagen_from_threads(thread_count: int, rounds: int):
    """Starts `thread_count` threads, releases them all at once via a
    barrier (maximising actual concurrent overlap rather than a staggered
    start), and has each repeatedly call mutagen.File() on every fixture.
    Returns (exceptions, crashed) -- `crashed` can only ever be seen as a
    non-zero exit code by a supervising process, since a real access
    violation kills the interpreter outright; a Python-level exception
    here is a *different*, survivable failure mode, not the one this is
    ultimately chasing."""
    from mutagen import File as MutagenFile

    fixtures = _existing_fixtures()
    barrier = threading.Barrier(thread_count)
    exceptions = []
    lock = threading.Lock()

    def _worker():
        barrier.wait()
        try:
            for _ in range(rounds):
                for path in fixtures:
                    MutagenFile(path)
        except Exception as ex:  # pragma: no cover - only on genuine failure
            with lock:
                exceptions.append(ex)

    threads = [threading.Thread(target=_worker) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return exceptions


@pytest.mark.skipif(not _existing_fixtures(), reason="audio fixtures not present")
def test_concurrent_first_use_without_warmup_is_the_scenario_that_crashed(monkeypatch):
    """Cold start (no mutagen submodules imported), many threads hitting
    mutagen.File() on real files at the same instant, with NO warm-up run
    first -- this is the exact shape of the trigger in crash.log. A clean
    pass here demonstrates this specific harness doesn't force the fault
    on this machine on demand (expected -- native races are famously
    non-deterministic); it does not demonstrate the fault cannot occur.
    """
    _clear_mutagen_submodules()

    exceptions = _hammer_mutagen_from_threads(thread_count=12, rounds=15)

    # A Python-level exception surviving to here (rather than a hard
    # process crash, which this harness cannot catch or convert into a
    # test failure) would itself be a real, reportable bug distinct from
    # the access violation -- worth asserting against even though it's not
    # the primary thing under investigation.
    assert exceptions == []


@pytest.mark.skipif(not _existing_fixtures(), reason="audio fixtures not present")
def test_concurrent_first_use_after_warmup_has_nothing_left_to_import(monkeypatch):
    """Same concurrent hammering, but run analysis_warmup's mutagen step
    first, synchronously, the way Main.py does before PlayerWindow exists.
    By the time the threads start, every submodule mutagen.File() could
    possibly need is already in sys.modules -- there is no import work
    left for any thread to race on, which is the concrete, verifiable
    thing the warm-up guarantees (as opposed to the unverifiable claim
    that it prevents a native fault that isn't reliably reproducible in
    the first place).
    """
    _clear_mutagen_submodules()
    analysis_warmup._warm_up_mutagen()

    import sys
    before = {n for n in sys.modules if n.startswith("mutagen.")}

    exceptions = _hammer_mutagen_from_threads(thread_count=12, rounds=15)

    after = {n for n in sys.modules if n.startswith("mutagen.")}
    assert exceptions == []
    assert after == before  # nothing new imported -- no first-use race window existed
