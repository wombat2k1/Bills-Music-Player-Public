"""Classic-audio-silence investigation (2026-08-31 Codex audit, section 7):
bass_device_hash() anonymises BASS's actual selected output device for
comparison against the video child's own device_hash. These tests cover
the pure hashing behaviour without depending on BASS actually being
loadable in every test environment.
"""
import hashlib

from billsmusic.bass_player import _BassEngine, bass_device_hash


def test_bass_device_hash_empty_when_not_initialized(monkeypatch):
    monkeypatch.setattr(_BassEngine, "bass", None)
    monkeypatch.setattr(_BassEngine, "initialized", False)

    assert bass_device_hash() == ""


def test_bass_device_hash_is_deterministic_sha256_of_description(monkeypatch):
    monkeypatch.setattr(
        _BassEngine, "current_device_description", classmethod(lambda cls: "Speakers (Realtek(R) Audio)")
    )

    result = bass_device_hash()

    expected = hashlib.sha256(
        "Speakers (Realtek(R) Audio)".encode("utf-8", "replace")
    ).hexdigest()[:16]
    assert result == expected
    assert len(result) == 16


def test_bass_device_hash_never_raises_on_query_failure(monkeypatch):
    def _boom(cls):
        raise RuntimeError("simulated BASS query failure")

    monkeypatch.setattr(_BassEngine, "current_device_description", classmethod(_boom))

    # bass_device_hash() is documented to never raise, even if the
    # underlying query somehow does (belt-and-braces on top of
    # current_device_description()'s own internal try/except) -- this must
    # not propagate, just report "no evidence yet".
    assert bass_device_hash() == ""


def test_current_device_description_returns_empty_before_bass_init(monkeypatch):
    monkeypatch.setattr(_BassEngine, "bass", None)
    monkeypatch.setattr(_BassEngine, "initialized", False)

    assert _BassEngine.current_device_description() == ""
