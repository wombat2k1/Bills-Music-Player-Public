"""Offline audio analysis for the visualiser.

Strategy: when a track loads, decode it once and precompute a normalised
log-mel spectrogram for the whole file. ``get_levels(t)`` is then just an
array lookup, so it is fast, perfectly aligned to the audio clock, and
preserves the music's natural dynamics (loud passages look loud, quiet
passages look quiet) instead of auto-gaining every frame to full scale.

Normalisation is computed ONCE over the whole track using per-band
percentiles, which is the key fix for the old "doesn't feel right" feel.
"""
from typing import List, Optional

from .platform_utils import is_frozen_build

try:
    import numpy as np
    import soundfile as sf
    AUDIO_ANALYSIS_AVAILABLE = True
except Exception:
    np = None
    sf = None
    AUDIO_ANALYSIS_AVAILABLE = False

try:
    import librosa
    LIBROSA_AVAILABLE = True
except Exception:
    librosa = None
    LIBROSA_AVAILABLE = False

# `import librosa` itself is cheap (~13ms) and doesn't touch numba -- the
# lazy submodules that do (librosa.core.audio, librosa.beat, ...) only
# import on first real attribute access (librosa.load, librosa.beat.tempo,
# ...), and that's what's gated off here. Confirmed via crash.log
# (2026-08-06): the packaged Nuitka build access-violates on exactly that
# first access -- Nuitka's own build output warns numba isn't yet fully
# working with standalone mode ("Numba JIT is disabled by default in
# standalone mode"), and that's reproduced 2/2 launches, on the main
# thread, before any worker thread or even PlayerWindow exists, so this
# isn't the earlier concurrent-import timing issue -- it's this specific
# combination not working at all yet. BPM/key estimation (workers.py's
# _estimate_bpm/_estimate_key) already has a pure-Python fallback for
# exactly this "librosa unavailable" case, so a frozen build just always
# uses that instead of the numba-accelerated path, rather than crashing on
# startup. Running from source (python Main.py, no numba/Nuitka
# interaction) is unaffected. See platform_utils.is_frozen_build() for why
# this can't just check sys.frozen directly.
LIBROSA_AVAILABLE = LIBROSA_AVAILABLE and not is_frozen_build()

# Kept for backwards-compat with code that imported it; no longer gates anything.
USE_LIBROSA = LIBROSA_AVAILABLE


# --- analysis constants -----------------------------------------------------
_N_FFT = 2048           # good low-frequency resolution (was ~350 samples before)
_HOP = 512              # ~11.6 ms at 44.1 kHz -> smooth frame rate
_FMIN = 30.0
_DB_FLOOR = -70.0       # anything below this maps to 0
_TARGET_SR = 44100      # resample target when decoding via librosa


class AudioAnalyzer:
    def __init__(self, bars: int = 32, window_ms: int = 8):
        # window_ms kept in the signature for API compatibility; unused now.
        self.bars = bars
        self.window_ms = window_ms
        self.samplerate = 0
        self.last_rms_db: Optional[float] = None
        self._path: Optional[str] = None
        self._duration_sec = 0.0

        # precomputed spectrogram state
        self._spec = None           # np.ndarray [bars, n_frames], 0..1
        self._rms_db = None         # np.ndarray [n_frames]
        self._frame_rate = 0.0      # frames per second
        self._ready = False

        # mel filter cache (rebuilt when samplerate changes)
        self._mel_fb = None
        self._mel_fb_sr = None

        # legacy attributes some callers may still poke
        self.file = None

    # -- lifecycle -----------------------------------------------------------
    def load(self, path: str):
        """Light load: record the path/duration, clear any old spectrogram.

        The heavy decode happens in build_mel_cache_chunked(), which the
        analyzer worker calls off the GUI thread so playback stays instant.
        """
        self.close()
        self._path = path
        try:
            info = sf.info(path)
            self.samplerate = info.samplerate
            if self.samplerate > 0:
                self._duration_sec = info.frames / float(self.samplerate)
        except Exception:
            # soundfile can't read some formats (e.g. mp3 on older libsndfile);
            # we'll fall back to librosa during the heavy decode.
            self.samplerate = _TARGET_SR
            self._duration_sec = 0.0

    def close(self):
        self._path = None
        self.samplerate = 0
        self.last_rms_db = None
        self._duration_sec = 0.0
        self._spec = None
        self._rms_db = None
        self._frame_rate = 0.0
        self._ready = False
        if self.file:
            try:
                self.file.close()
            except Exception:
                pass
            self.file = None

    # -- heavy decode (called from worker thread) ----------------------------
    def build_mel_cache_chunked(self, chunk_sec: float = 5.0):
        """Decode the whole track and precompute the normalised spectrogram.

        Name/signature kept for compatibility with the existing worker.
        """
        if not self._path:
            return
        try:
            y, sr = self._decode_mono(self._path)
            if y is None or y.size < _N_FFT:
                return
            self.samplerate = sr
            self._duration_sec = y.size / float(sr)

            mag = self._stft_mag(y)                  # [freq_bins, frames]
            mel_fb = self._mel_filterbank(sr, mag.shape[0])
            mel = mel_fb @ mag                       # [bars, frames]

            # power -> dB, with a fixed floor (NOT per-frame!)
            power = mel ** 2
            db = 10.0 * np.log10(power + 1e-10)
            db = np.maximum(db, _DB_FLOOR)

            # Per-band normalisation using percentiles over the whole track.
            # 95th percentile -> ~1.0 keeps occasional peaks from flattening
            # everything; this is what preserves the dynamics/feel.
            lo = np.percentile(db, 20.0, axis=1, keepdims=True)
            hi = np.percentile(db, 97.0, axis=1, keepdims=True)
            span = np.maximum(hi - lo, 6.0)          # avoid divide-by-tiny
            norm = (db - lo) / span
            norm = np.clip(norm, 0.0, 1.0)

            # Perceptual shaping: a mild gamma makes motion feel punchier.
            norm = norm ** 1.35

            self._spec = norm.astype("float32")
            self._frame_rate = sr / float(_HOP)

            # whole-track RMS in dB per frame (for the fade trigger)
            frame_rms = np.sqrt(np.mean(self._frame_view(y) ** 2, axis=1) + 1e-12)
            self._rms_db = (20.0 * np.log10(frame_rms + 1e-12)).astype("float32")

            self._ready = True
        except Exception:
            self._spec = None
            self._ready = False

    # alias kept for compatibility
    def build_mel_cache(self):
        self.build_mel_cache_chunked()

    # -- per-frame lookup (called from GUI/worker, very cheap) ---------------
    def get_levels(self, time_sec: float):
        if not self._ready or self._spec is None:
            # Not analysed yet: report nothing so the widget shows its idle pulse.
            self.last_rms_db = None
            return None
        frame = int(round(time_sec * self._frame_rate))
        n = self._spec.shape[1]
        if n == 0:
            return None
        frame = max(0, min(frame, n - 1))
        if self._rms_db is not None and frame < self._rms_db.shape[0]:
            self.last_rms_db = float(self._rms_db[frame])
        return [float(x) for x in self._spec[:, frame]]

    # -- internals -----------------------------------------------------------
    def _decode_mono(self, path: str):
        """Return (mono float32 array, samplerate). Tries soundfile, then librosa."""
        try:
            data, sr = sf.read(path, dtype="float32", always_2d=True)
            mono = data.mean(axis=1)
            return mono, int(sr)
        except Exception:
            pass
        if LIBROSA_AVAILABLE:
            try:
                y, sr = librosa.load(path, sr=_TARGET_SR, mono=True)
                return y.astype("float32"), int(sr)
            except Exception:
                pass
        return None, 0

    def _frame_view(self, y):
        """Frame the signal into [frames, _HOP] rows (for RMS), zero-padded."""
        n_frames = 1 + max(0, (y.size - 1) // _HOP)
        pad = n_frames * _HOP - y.size
        if pad > 0:
            y = np.concatenate([y, np.zeros(pad, dtype=y.dtype)])
        return y.reshape(n_frames, _HOP)

    def _stft_mag(self, y):
        """Magnitude STFT via numpy (librosa path collapses to the same shape)."""
        win = np.hanning(_N_FFT).astype("float32")
        n_frames = 1 + max(0, (y.size - _N_FFT) // _HOP)
        if n_frames < 1:
            n_frames = 1
        # build framed matrix
        idx = np.arange(_N_FFT)[None, :] + _HOP * np.arange(n_frames)[:, None]
        idx = np.clip(idx, 0, y.size - 1)
        frames = y[idx] * win[None, :]
        spec = np.fft.rfft(frames, axis=1)
        return np.abs(spec).T.astype("float32")   # [freq_bins, frames]

    def _mel_filterbank(self, sr: int, n_bins: int):
        if self._mel_fb is not None and self._mel_fb_sr == sr and self._mel_fb.shape == (self.bars, n_bins):
            return self._mel_fb
        nyq = sr / 2.0
        fmax = nyq
        # mel-spaced band edges, slightly mid-weighted
        def hz_to_mel(h): return 2595.0 * np.log10(1.0 + h / 700.0)
        def mel_to_hz(m): return 700.0 * (10.0 ** (m / 2595.0) - 1.0)
        edges_mel = np.linspace(hz_to_mel(_FMIN), hz_to_mel(fmax), self.bars + 2)
        edges_hz = mel_to_hz(edges_mel)
        bin_freqs = np.linspace(0.0, nyq, n_bins)
        fb = np.zeros((self.bars, n_bins), dtype="float32")
        for i in range(self.bars):
            lo, ctr, hi = edges_hz[i], edges_hz[i + 1], edges_hz[i + 2]
            for j, f in enumerate(bin_freqs):
                if lo <= f <= ctr and ctr > lo:
                    fb[i, j] = (f - lo) / (ctr - lo)
                elif ctr < f <= hi and hi > ctr:
                    fb[i, j] = (hi - f) / (hi - ctr)
        self._mel_fb = fb
        self._mel_fb_sr = sr
        return fb
