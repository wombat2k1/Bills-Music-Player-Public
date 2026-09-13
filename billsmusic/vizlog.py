"""Diagnostic logger for the visualiser.

When enabled for a track, writes one CSV row per analysis frame capturing the
clocks and band levels, so the numbers can be checked against the actual audio
offline. Also writes a self-check header: the app re-analyses the file
independently and records where energy *should* be at several timestamps, so
the log validates itself.
"""
import os
import csv
import time
from typing import Optional, List


def _log_dir() -> str:
    base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    folder = os.path.join(base, "Bills Music Player", "vizlogs")
    os.makedirs(folder, exist_ok=True)
    return folder


class VizLogger:
    def __init__(self, bars: int = 32):
        self.bars = bars
        self._fh = None
        self._writer = None
        self.active = False
        self.path: Optional[str] = None
        self._track: Optional[str] = None
        self._t0 = 0.0

    def start(self, track_path: str, backend: str, analyzer=None,
              latency_ms: int = 0):
        """Open a new CSV log for a track. backend is 'vlc' or 'builtin'."""
        self.stop()
        try:
            base = os.path.splitext(os.path.basename(track_path))[0]
            stamp = time.strftime("%Y%m%d_%H%M%S")
            safe = "".join(c for c in base if c.isalnum() or c in " _-")[:60].strip()
            self.path = os.path.join(_log_dir(), f"{safe}_{backend}_{stamp}.csv")
            self._fh = open(self.path, "w", newline="", encoding="utf-8")
            self._writer = csv.writer(self._fh)
            self._track = track_path
            self._t0 = time.time()
            self.active = True

            # --- metadata + self-check header (as # comment lines) ---
            self._fh.write(f"# track,{track_path}\n")
            self._fh.write(f"# backend,{backend}\n")
            self._fh.write(f"# latency_offset_ms,{latency_ms}\n")
            self._fh.write(f"# bars,{self.bars}\n")
            self._write_selfcheck(track_path, analyzer)

            # --- column header ---
            cols = (["wall_s", "player_clock_s", "analysis_t_s", "rms_db",
                     "peak_band"]
                    + [f"band{i:02d}" for i in range(self.bars)])
            self._writer.writerow(cols)
            self._fh.flush()
        except Exception:
            self.active = False
            self._fh = None
            self._writer = None

    def _write_selfcheck(self, track_path: str, analyzer):
        """Independently re-analyse the file and note expected peak bands."""
        try:
            from .audio import AudioAnalyzer
            ref = AudioAnalyzer(bars=self.bars)
            ref.load(track_path)
            ref.build_mel_cache_chunked()
            if not getattr(ref, "_ready", False):
                self._fh.write("# selfcheck,unavailable\n")
                return
            dur = ref._duration_sec
            self._fh.write(f"# duration_s,{dur:.2f}\n")
            self._fh.write("# selfcheck_format,time_s:peak_band:max_level:rms_db\n")
            n = 8
            for k in range(1, n):
                tt = dur * k / float(n)
                lv = ref.get_levels(tt)
                if not lv:
                    continue
                pb = max(range(len(lv)), key=lambda i: lv[i])
                self._fh.write(
                    f"# selfcheck,{tt:.2f}:{pb}:{max(lv):.3f}:"
                    f"{(ref.last_rms_db if ref.last_rms_db is not None else 0):.1f}\n"
                )
            ref.close()
        except Exception:
            try:
                self._fh.write("# selfcheck,error\n")
            except Exception:
                pass

    def log_frame(self, player_clock_s: float, analysis_t_s: float,
                  rms_db: Optional[float], levels: List[float]):
        if not self.active or self._writer is None or not levels:
            return
        try:
            peak_band = max(range(len(levels)), key=lambda i: levels[i])
            row = ([f"{time.time() - self._t0:.3f}",
                    f"{player_clock_s:.3f}",
                    f"{analysis_t_s:.3f}",
                    f"{rms_db:.1f}" if rms_db is not None else "",
                    peak_band]
                   + [f"{x:.4f}" for x in levels])
            self._writer.writerow(row)
        except Exception:
            pass

    def stop(self):
        self.active = False
        if self._fh:
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:
                pass
        self._fh = None
        self._writer = None
        self._track = None
