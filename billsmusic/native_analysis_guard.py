"""Serialise crash-prone native audio analysis performed by background workers.

Playback does not use this gate. It only prevents SoundFile/libsndfile and
NumPy/SciPy FFT analysis jobs from entering their native sections at the same
time in different Python threads.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Callable, Iterator


_NATIVE_ANALYSIS_LOCK = threading.Lock()


@contextmanager
def native_analysis_slot(
    should_continue: Callable[[], bool],
) -> Iterator[bool]:
    acquired = False
    while should_continue():
        if _NATIVE_ANALYSIS_LOCK.acquire(timeout=0.05):
            acquired = True
            break
    try:
        yield acquired
    finally:
        if acquired:
            _NATIVE_ANALYSIS_LOCK.release()
