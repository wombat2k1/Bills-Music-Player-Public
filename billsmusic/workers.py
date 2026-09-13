"""Background QThread workers: analyzer, artist-bio fetch, search, library scan."""
import os
import re
import json
import time
import urllib.parse
import urllib.request
from collections import Counter
from typing import List, Optional, Dict, Tuple, Any, TYPE_CHECKING

from PyQt6 import QtCore, QtGui, QtMultimedia
from mutagen import File as MutagenFile

from .net import http_get
from .config import load_config, SUPPORTED_EXTENSIONS, album_cover_cache_path
from .media_capabilities import is_bpm_key_eligible, is_library_scannable
from .native_analysis_guard import native_analysis_slot
from .metadata import read_track_meta, read_cover_bytes, meta_needs_backfill, read_full_tag_display
from .cast_service import build_music_metadata
from .audio import AudioAnalyzer, np, sf, librosa, LIBROSA_AVAILABLE
from .library_search import filter_search_index, group_library_albums
from .performance_diagnostics import get_diagnostics
from .library_cache import (
    audio_fingerprint_matches,
    lrc_fingerprint_matches,
    metadata_with_lrc_fingerprint,
)
from .loudness import read_replaygain
from .lyrics import load_lyrics_for_track

if TYPE_CHECKING:
    from .window import PlayerWindow


class AnalyzerWorker(QtCore.QThread):
    result_ready = QtCore.pyqtSignal(str, list)
    # emitted True while a track is being analysed, False when ready
    analysis_busy = QtCore.pyqtSignal(str, bool)
    # these signals are used to communicate from the GUI thread to the worker thread
    update_time = QtCore.pyqtSignal(float)
    update_track = QtCore.pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._lock = QtCore.QMutex()
        self._cond = QtCore.QWaitCondition()
        self._time = None
        self._pending_path = None      # a new track waiting to be decoded
        self._current_path = ""
        self._analyzer = AudioAnalyzer()
        self._running = True

        # connect signals to slots so that updates happen in this thread
        self.update_time.connect(self._on_update_time)
        self.update_track.connect(self._on_update_track)

    @QtCore.pyqtSlot(float)
    def _on_update_time(self, time_sec: float):
        with QtCore.QMutexLocker(self._lock):
            self._time = time_sec
            self._cond.wakeAll()

    @QtCore.pyqtSlot(str)
    def _on_update_track(self, path: str):
        with QtCore.QMutexLocker(self._lock):
            self._pending_path = path
            self._time = None
            self._cond.wakeAll()

    def run(self):
        while self._running:
            # 1) New track to decode? Grab it, release the lock, then do the
            #    heavy work so it never blocks the GUI thread's signals.
            with QtCore.QMutexLocker(self._lock):
                path = self._pending_path
                self._pending_path = None

            if path is not None:
                self._current_path = path
                self.analysis_busy.emit(path, True)
                with native_analysis_slot(lambda: self._running) as acquired:
                    if not acquired:
                        self.analysis_busy.emit(path, False)
                        break
                    self._analyzer.load(path)
                    self._analyzer.build_mel_cache_chunked(chunk_sec=5.0)
                self.analysis_busy.emit(path, False)
                if not self._running:
                    break
                continue  # loop back and pick up the latest time

            # 2) Otherwise wait for a time tick and do a cheap lookup.
            with QtCore.QMutexLocker(self._lock):
                if self._time is None and self._pending_path is None:
                    self._cond.wait(self._lock, 50)
                    continue
                t = self._time
                self._time = None

            if t is not None:
                levels = self._analyzer.get_levels(t)
                # v1.0.66 worker-lifetime hardening: re-checked immediately
                # before emit (not just at the top of the loop) -- stop()
                # landing while get_levels() was running must not still let
                # this one stale result through, matching the pattern
                # already used by BioWorker/QueueAnalysisWorker/
                # LoudnessAnalysisWorker/WaveformWorker.
                if levels is not None and self._running:
                    self.result_ready.emit(self._current_path, levels)

    @property
    def last_rms_db(self):
        return self._analyzer.last_rms_db


    def stop(self):
        self._running = False
        with QtCore.QMutexLocker(self._lock):
            self._cond.wakeAll()


class BioWorker(QtCore.QThread):
    bio_ready = QtCore.pyqtSignal(str, str, int)

    def __init__(self):
        super().__init__()
        self._lock = QtCore.QMutex()
        self._cond = QtCore.QWaitCondition()
        self._artist = ""
        self._track = ""
        self._generation = 0
        self._running = True
        self._artist_bio_name_cache = {}
        self._diagnostics = get_diagnostics()
        self._diagnostic_token = None

    def set_artist_track(self, artist: str, track: str, generation: int = 0):
        new_token = self._diagnostics.submit_worker(
            "biography",
            "fetch_artist_biography",
            details={"artist_hash": self._diagnostics.path_details(
                artist or "unknown"
            )["path_hash"]},
        )
        with QtCore.QMutexLocker(self._lock):
            if self._diagnostic_token is not None:
                stats = self._diagnostics.worker_stats[
                    self._diagnostic_token.operation
                ]
                stats["queued"] = max(0, stats["queued"] - 1)
                stats["cancelled"] += 1
            self._diagnostic_token = new_token
            self._artist = artist or ""
            self._track = track or ""
            self._generation = int(generation)
            self._cond.wakeAll()

    def run(self):
        last_artist = None
        last_track = None
        while self._running:
            with QtCore.QMutexLocker(self._lock):
                if not self._artist:
                    self._cond.wait(self._lock, 200)
                    continue
                artist = self._artist
                track = self._track
                generation = self._generation
                token = self._diagnostic_token
                self._diagnostic_token = None
            if artist == last_artist and track == last_track:
                if token is not None:
                    stats = self._diagnostics.worker_stats[token.operation]
                    stats["queued"] = max(0, stats["queued"] - 1)
                    stats["cancelled"] += 1
                time.sleep(0.2)
                continue
            last_artist = artist
            last_track = track
            if token is not None:
                with self._diagnostics.run_worker(token):
                    text = self._fetch_artist_bio(artist)
            else:
                text = self._fetch_artist_bio(artist)
            # Network calls cannot be interrupted safely.  If a newer track
            # arrived meanwhile, retain any worker-local provider cache but
            # never emit the obsolete result to the GUI.
            with QtCore.QMutexLocker(self._lock):
                current_generation = self._generation
            if text and self._running and generation == current_generation:
                self.bio_ready.emit(artist, text, generation)

    def _artist_bio_candidates(self, artist: str):
        """Return bio lookup names from most specific to most likely primary artist."""
        artist = (artist or "").strip()
        if not artist:
            return []
        cached = self._artist_bio_name_cache.get(artist)
        candidates = []
        if cached:
            candidates.append(cached)
        candidates.append(artist)

        # Some tags list a group followed by member credits, e.g.
        # "Eurythmics, Annie Lennox, Dave Stewart". If that misses,
        # try the first comma-separated name as the likely bio subject.
        first_comma = re.split(r"\s*,\s*", artist, maxsplit=1)[0].strip()
        if first_comma and first_comma != artist:
            candidates.append(first_comma)

        first_joined = re.split(
            r"\s+(?:with|and|&|/|x|vs\.?|feat\.?|ft\.?|featuring)\s+",
            artist,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()
        if first_joined and first_joined != artist:
            candidates.append(first_joined)

        seen = set()
        unique = []
        for candidate in candidates:
            key = candidate.casefold()
            if key not in seen:
                seen.add(key)
                unique.append(candidate)
        return unique

    def _bio_debug(self, message: str):
        try:
            log_dir = os.path.join(os.environ.get("LOCALAPPDATA", os.getcwd()), "Bills Music Player")
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, "player.log"), "a", encoding="utf-8") as f:
                f.write("BIO: " + message + "\n")
        except Exception:
            pass

    def _fetch_artist_bio(self, artist: str) -> str:
        candidates = self._artist_bio_candidates(artist)
        self._bio_debug(f"lookup artist={artist!r}; candidates={candidates!r}")
        for candidate in candidates:
            text = self._fetch_artist_bio_for_name(candidate)
            if text and text != "No bio found.":
                self._artist_bio_name_cache[artist] = candidate
                if candidate != artist:
                    self._bio_debug(f"using fallback candidate={candidate!r} for artist={artist!r}")
                return text
        self._bio_debug(f"no bio found for artist={artist!r}")
        return "No bio found."

    def _fetch_artist_bio_for_name(self, artist: str) -> str:
        try:
            # Try Last.fm first for richer bios, then enrich with tags/top tracks.
            lastfm_text = self._lastfm_bio(artist)
            if lastfm_text and lastfm_text != "No bio found.":
                self._bio_debug(f"provider=Last.fm artist bio; artist={artist!r}; chars={len(lastfm_text)}")
                return self._enrich_bio_text(artist, lastfm_text)
            # Try Last.fm track info if artist bio is missing
            track_text = self._lastfm_track_bio(artist, self._track)
            if track_text and track_text != "No bio found.":
                self._bio_debug(f"provider=Last.fm track wiki; artist={artist!r}; track={self._track!r}; chars={len(track_text)}")
                return self._enrich_bio_text(artist, track_text)
            # MusicBrainz artist search
            query = f'artist:"{artist}"'
            params = {"query": query, "fmt": "json", "limit": 1}
            url = "https://musicbrainz.org/ws/2/artist/?" + urllib.parse.urlencode(params)
            data = json.loads(http_get(url).decode("utf-8", errors="ignore"))
            artists = data.get("artists") or []
            if not artists:
                return "No bio found."
            mbid = artists[0].get("id")
            if not mbid:
                return "No bio found."
            self._bio_debug(f"provider=MusicBrainz search; artist={artist!r}; mbid={mbid}")
            tadb_text = self._theaudiodb_artist_bio(artist, mbid)
            tadb_fallback = ""
            if tadb_text and tadb_text != "No bio found.":
                story_chars = self._bio_story_chars(tadb_text)
                self._bio_debug(
                    f"provider=TheAudioDB artist bio; artist={artist!r}; mbid={mbid}; "
                    f"chars={len(tadb_text)}; story_chars={story_chars}"
                )
                # TheAudioDB sometimes returns only a tiny biography plus useful
                # facts/tags. Keep it as a fallback, but continue to Wikipedia
                # so the overlay has a proper story when one is available.
                if story_chars >= 180:
                    return self._enrich_bio_text(artist, tadb_text)
                tadb_fallback = tadb_text
                self._bio_debug(f"provider=TheAudioDB artist bio too short; artist={artist!r}; trying Wikipedia fallback")
            time.sleep(1.1)  # MusicBrainz rate limit
            url = f"https://musicbrainz.org/ws/2/artist/{mbid}?inc=url-rels&fmt=json"
            data = json.loads(http_get(url).decode("utf-8", errors="ignore"))
            wiki_url = ""
            wikidata_id = ""
            for rel in data.get("relations", []):
                if rel.get("type") == "wikipedia":
                    wiki_url = rel.get("url", {}).get("resource", "")
                    break
                if rel.get("type") == "wikidata":
                    wikidata_id = rel.get("url", {}).get("resource", "").rstrip("/").split("/")[-1]
            if not wiki_url:
                if wikidata_id:
                    wd_api = f"https://www.wikidata.org/wiki/Special:EntityData/{wikidata_id}.json"
                    wd = json.loads(http_get(wd_api).decode("utf-8", errors="ignore"))
                    entities = wd.get("entities", {})
                    ent = entities.get(wikidata_id, {})
                    sitelinks = ent.get("sitelinks", {})
                    enwiki = sitelinks.get("enwiki", {})
                    title = enwiki.get("title", "")
                    if title:
                        wiki_url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title)
            if not wiki_url:
                direct_text = self._wikipedia_summary(artist, artist)
                if direct_text:
                    return self._with_bio_extras(artist, direct_text, tadb_fallback)
                return tadb_fallback if tadb_fallback else "No bio found."
            title = urllib.parse.unquote(wiki_url.rstrip("/").split("/")[-1])
            text = self._wikipedia_summary(artist, title)
            if text:
                return self._with_bio_extras(artist, text, tadb_fallback)
            return tadb_fallback if tadb_fallback else "No bio found."
        except Exception:
            return "No bio found."

    def _wikipedia_summary(self, artist: str, title: str) -> str:
        try:
            wiki_api = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title)
            summary = json.loads(http_get(wiki_api).decode("utf-8", errors="ignore"))
            if summary.get("type") == "disambiguation":
                self._bio_debug(f"provider=Wikipedia summary skipped disambiguation; artist={artist!r}; page={title!r}")
                return ""
            text = summary.get("extract", "") or ""
            if text:
                self._bio_debug(f"provider=Wikipedia summary; artist={artist!r}; page={title!r}; chars={len(text)}")
            return text.strip()
        except Exception:
            return ""

    def _with_bio_extras(self, artist: str, text: str, extra_source: str) -> str:
        extras = self._bio_extra_lines(extra_source)
        if extras:
            text = text.strip() + "\n" + "\n".join(extras)
        return self._enrich_bio_text(artist, text)


    def _bio_story_chars(self, text: str) -> int:
        story_lines = [
            line.strip()
            for line in (text or "").splitlines()
            if line.strip() and not re.match(r"^(Artist facts|Style tags|Known for):", line.strip(), re.IGNORECASE)
        ]
        return len(" ".join(story_lines))

    def _bio_extra_lines(self, text: str) -> List[str]:
        extras = []
        for line in (text or "").splitlines():
            line = line.strip()
            if line and re.match(r"^(Artist facts|Style tags):", line, re.IGNORECASE):
                extras.append(line)
        return extras

    def _lastfm_bio(self, artist: str) -> str:
        try:
            cfg = load_config()
            api_key = cfg.get("lastfm_api_key", "").strip()
            if not api_key:
                return "No bio found."
            params = {
                "method": "artist.getinfo",
                "artist": artist,
                "api_key": api_key,
                "format": "json",
                "autocorrect": 1,
            }
            url = "https://ws.audioscrobbler.com/2.0/?" + urllib.parse.urlencode(params)
            data = json.loads(http_get(url).decode("utf-8", errors="ignore"))
            artist_data = data.get("artist") or {}
            bio = artist_data.get("bio") or {}
            text = bio.get("summary", "") or bio.get("content", "")
            if not text:
                return "No bio found."
            # Strip HTML tags in a simple way
            return re.sub(r"<[^>]+>", "", text).strip()
        except Exception:
            return "No bio found."

    def _lastfm_track_bio(self, artist: str, track: str) -> str:
        try:
            if not track:
                return "No bio found."
            cfg = load_config()
            api_key = cfg.get("lastfm_api_key", "").strip()
            if not api_key:
                return "No bio found."
            params = {
                "method": "track.getInfo",
                "artist": artist,
                "track": track,
                "api_key": api_key,
                "format": "json",
                "autocorrect": 1,
            }
            url = "https://ws.audioscrobbler.com/2.0/?" + urllib.parse.urlencode(params)
            data = json.loads(http_get(url).decode("utf-8", errors="ignore"))
            track_data = data.get("track") or {}
            wiki = track_data.get("wiki") or {}
            text = wiki.get("summary", "") or wiki.get("content", "")
            if not text:
                return "No bio found."
            return re.sub(r"<[^>]+>", "", text).strip()
        except Exception:
            return "No bio found."

    def _theaudiodb_api_key(self) -> str:
        try:
            return (load_config().get("theaudiodb_api_key", "") or "2").strip() or "2"
        except Exception:
            return "2"

    def _theaudiodb_artist_bio(self, artist: str, mbid: str) -> str:
        try:
            api_key = self._theaudiodb_api_key()
            url = f"https://www.theaudiodb.com/api/v1/json/{urllib.parse.quote(api_key)}/artist-mb.php?i=" + urllib.parse.quote(mbid)
            data = json.loads(http_get(url).decode("utf-8", errors="ignore"))
            artists = data.get("artists") or []
            if not artists:
                return "No bio found."
            item = artists[0] or {}
            bio = re.sub(r"<[^>]+>", "", item.get("strBiographyEN") or "").strip()
            facts = []
            country = (item.get("strCountry") or "").strip()
            formed = str(item.get("intFormedYear") or "").strip()
            genre = (item.get("strGenre") or "").strip()
            style = (item.get("strStyle") or "").strip()
            if country:
                facts.append(f"from {country}")
            if formed and formed != "0":
                facts.append(f"formed in {formed}")
            if facts:
                self._bio_debug(f"provider=TheAudioDB extras; artist={artist!r}; facts={facts!r}")
                bio += "\nArtist facts: " + ", ".join(facts) + "."
            tags = ", ".join([part for part in (genre, style) if part])
            if tags:
                self._bio_debug(f"provider=TheAudioDB extras; artist={artist!r}; style={tags!r}")
                bio += "\nStyle tags: " + tags + "."
            return bio if bio.strip() else "No bio found."
        except Exception:
            return "No bio found."

    def _lastfm_api_key(self) -> str:
        try:
            return (load_config().get("lastfm_api_key", "") or "").strip()
        except Exception:
            return ""

    def _lastfm_artist_tags(self, artist: str) -> List[str]:
        try:
            api_key = self._lastfm_api_key()
            if not api_key:
                return []
            params = {
                "method": "artist.getTopTags",
                "artist": artist,
                "api_key": api_key,
                "format": "json",
                "autocorrect": 1,
            }
            url = "https://ws.audioscrobbler.com/2.0/?" + urllib.parse.urlencode(params)
            data = json.loads(http_get(url).decode("utf-8", errors="ignore"))
            tags = (data.get("toptags") or {}).get("tag") or []
            names = []
            for tag in tags:
                name = str(tag.get("name", "")).strip()
                if name and name.lower() not in ("seen live", "albums i own"):
                    names.append(name)
                if len(names) >= 5:
                    break
            return names
        except Exception:
            return []

    def _lastfm_top_tracks(self, artist: str) -> List[str]:
        try:
            api_key = self._lastfm_api_key()
            if not api_key:
                return []
            params = {
                "method": "artist.getTopTracks",
                "artist": artist,
                "api_key": api_key,
                "format": "json",
                "autocorrect": 1,
                "limit": 5,
            }
            url = "https://ws.audioscrobbler.com/2.0/?" + urllib.parse.urlencode(params)
            data = json.loads(http_get(url).decode("utf-8", errors="ignore"))
            tracks = (data.get("toptracks") or {}).get("track") or []
            return [str(track.get("name", "")).strip() for track in tracks if str(track.get("name", "")).strip()][:5]
        except Exception:
            return []

    def _enrich_bio_text(self, artist: str, text: str) -> str:
        extras = []
        tags = self._lastfm_artist_tags(artist)
        if tags:
            self._bio_debug(f"provider=Last.fm extras; artist={artist!r}; tags={tags!r}")
            extras.append("Style tags: " + ", ".join(tags) + ".")
        tracks = self._lastfm_top_tracks(artist)
        if tracks:
            self._bio_debug(f"provider=Last.fm extras; artist={artist!r}; top_tracks={tracks!r}")
            extras.append("Known for: " + ", ".join(tracks) + ".")
        if extras:
            return text.strip() + "\n" + "\n".join(extras)
        return text.strip()

    def stop(self):
        self._running = False
        with QtCore.QMutexLocker(self._lock):
            self._cond.wakeAll()


class SearchWorker(QtCore.QThread):
    results_ready = QtCore.pyqtSignal(int, str, list, list, float)

    def __init__(self):
        super().__init__()
        self._lock = QtCore.QMutex()
        self._cond = QtCore.QWaitCondition()
        self._query = ""
        self._index = []
        self._generation = 0
        self._running = True
        self._dirty = False

    def set_query(self, generation: int, query: str, index):
        with QtCore.QMutexLocker(self._lock):
            self._generation = generation
            self._query = query
            self._index = index
            self._dirty = True
            self._cond.wakeAll()

    def _is_cancelled(self, generation: int) -> bool:
        with QtCore.QMutexLocker(self._lock):
            return not self._running or generation != self._generation

    def run(self):
        while self._running:
            with QtCore.QMutexLocker(self._lock):
                if not self._dirty:
                    self._cond.wait(self._lock, 500)
                if not self._dirty:
                    continue
                generation = self._generation
                query = self._query
                index = self._index
                self._dirty = False
            started = time.perf_counter()
            results = filter_search_index(
                index, query, lambda: self._is_cancelled(generation)
            )
            if self._is_cancelled(generation):
                continue
            album_entries = group_library_albums(results)
            if self._is_cancelled(generation):
                continue
            duration_ms = (time.perf_counter() - started) * 1000.0
            self.results_ready.emit(
                generation, query, results, album_entries, duration_ms
            )


    def stop(self):
        self._running = False
        with QtCore.QMutexLocker(self._lock):
            self._cond.wakeAll()


class KaraokePrepareWorker(QtCore.QThread):
    prepared = QtCore.pyqtSignal(int, str, object, object)
    failed = QtCore.pyqtSignal(int, str, str)

    def __init__(self, generation: int, source_path: str):
        super().__init__()
        self.generation = generation
        self.source_path = source_path

    def cancel(self):
        self.requestInterruption()

    def run(self):
        try:
            from .karaoke import prepare_karaoke_source
            from .cdg import CdgDocument
            pair = prepare_karaoke_source(
                self.source_path, should_cancel=self.isInterruptionRequested,
            )
            if self.isInterruptionRequested():
                return
            document = CdgDocument.from_file(pair.cdg_path)
            if self.isInterruptionRequested():
                return
            self.prepared.emit(self.generation, self.source_path, pair, document)
        except Exception as ex:
            self.failed.emit(self.generation, self.source_path, str(ex))


class LibraryScanThread(QtCore.QThread):
    finished_scan = QtCore.pyqtSignal(list, list)
    canceled_scan = QtCore.pyqtSignal()
    progress = QtCore.pyqtSignal(str, str, int, int)

    def __init__(
        self,
        window: "PlayerWindow",
        add_folder: Optional[str] = None,
        force_full_reread: bool = False,
    ):
        super().__init__()
        self.window = window
        self.add_folder = add_folder
        self.force_full_reread = force_full_reread
        self._cancel_requested = False
        self.scan_stats = {
            "enumerated": 0,
            "metadata_reused": 0,
            "metadata_read": 0,
            "lyrics_only_updated": 0,
            "added": 0,
            "modified": 0,
            "removed": 0,
            "unavailable_folders": 0,
            "fingerprints_baselined": 0,
        }

    def cancel(self):
        self._cancel_requested = True

    def is_cancelled(self) -> bool:
        return self._cancel_requested

    def _checkpoint_backfill_progress(
        self,
        cached_folder_by_path,
        cached_meta_by_path,
        folder_entries_so_far,
        meta_list_so_far,
        current_folder_entry=None,
        current_folder_processed_paths=None,
    ):
        """Persist a full, self-consistent snapshot of the whole library --
        folders/tracks fully processed so far, plus every untouched folder
        reused as-is from the previous cache -- so an interrupted backfill
        resumes near where it left off on the next launch instead of
        restarting from scratch. Only used for the one-time metadata
        backfill (force_full_reread); normal scans don't need this."""
        snapshot_folders = list(folder_entries_so_far)
        snapshot_meta = list(meta_list_so_far)
        accounted_for = {f.get("path") for f in snapshot_folders if isinstance(f, dict)}
        if current_folder_entry is not None:
            snapshot_folders.append(current_folder_entry)
            accounted_for.add(current_folder_entry.get("path"))
            processed = current_folder_processed_paths or set()
            for path in current_folder_entry.get("tracks", []):
                if path in processed:
                    continue
                cached_meta = cached_meta_by_path.get(path)
                if cached_meta:
                    snapshot_meta.append(cached_meta)
        for folder_path, cached_folder in cached_folder_by_path.items():
            if folder_path in accounted_for:
                continue
            snapshot_folders.append(cached_folder)
            for path in cached_folder.get("tracks", []):
                cached_meta = cached_meta_by_path.get(path)
                if cached_meta:
                    snapshot_meta.append(cached_meta)
        try:
            self.window._save_cache(snapshot_folders, snapshot_meta)
        except Exception:
            pass

    def run(self):
        cache = self.window._load_cache() or {}
        cached_folders = cache.get("folders", []) if isinstance(cache, dict) else []
        cached_meta = cache.get("meta", []) if isinstance(cache, dict) else []
        cached_meta_by_path = {
            meta.get("path"): meta
            for meta in cached_meta
            if isinstance(meta, dict) and meta.get("path")
        }
        cached_folder_by_path = {
            folder.get("path"): folder
            for folder in cached_folders
            if isinstance(folder, dict) and folder.get("path")
        }

        folders = [folder.get("path") for folder in cached_folders if folder.get("path")]
        if self.add_folder:
            if self.add_folder not in folders:
                folders.append(self.add_folder)
        folder_entries = []
        meta_list: List[Dict[str, Any]] = []
        incremental = bool(self.add_folder)
        if incremental:
            if self.is_cancelled():
                self.canceled_scan.emit()
                return
            self.progress.emit("Preparing", "Reusing cached folders", 0, 0)
            folder_entries = [
                folder
                for folder in cached_folders
                if isinstance(folder, dict) and folder.get("path")
            ]
            meta_list = [
                meta
                for meta in cached_meta
                if isinstance(meta, dict) and meta.get("path")
            ]
            if self.add_folder and self.add_folder not in cached_folder_by_path and os.path.isdir(self.add_folder):
                self.progress.emit("Scanning new folder", self.add_folder, 0, 0)
                sig, tracks, fingerprints = self.window._scan_folder(
                    self.add_folder,
                    lambda root, count: self.progress.emit("Scanning new folder", root, count, 0),
                    should_cancel=self.is_cancelled,
                )
                if self.is_cancelled():
                    self.canceled_scan.emit()
                    return
                new_meta = []
                total = len(tracks)
                for i, path in enumerate(tracks, 1):
                    if self.is_cancelled():
                        self.canceled_scan.emit()
                        return
                    self.progress.emit("Reading tags", path, i, total)
                    new_meta.append(read_track_meta(path))
                    self.scan_stats["metadata_read"] += 1
                    self.scan_stats["added"] += 1
                self.scan_stats["enumerated"] += len(tracks)
                folder_entries.append({
                    "path": self.add_folder,
                    "signature": sig,
                    "tracks": tracks,
                    "file_fingerprints": fingerprints,
                })
                meta_list.extend(new_meta)
            self.finished_scan.emit(meta_list, folder_entries)
            return

        for folder in folders:
            if self.is_cancelled():
                self.canceled_scan.emit()
                return
            if not folder:
                continue
            if not os.path.isdir(folder):
                cached_folder = cached_folder_by_path.get(folder)
                if cached_folder:
                    folder_entries.append(cached_folder)
                    for path in cached_folder.get("tracks", []):
                        cached_meta = cached_meta_by_path.get(path)
                        if cached_meta:
                            meta_list.append(cached_meta)
                            self.scan_stats["metadata_reused"] += 1
                    self.scan_stats["unavailable_folders"] += 1
                continue
            self.progress.emit("Scanning folder", folder, 0, 0)
            sig, tracks, fingerprints = self.window._scan_folder(
                folder,
                lambda root, count: self.progress.emit("Scanning folder", root, count, 0),
                should_cancel=self.is_cancelled,
            )
            if self.is_cancelled():
                self.canceled_scan.emit()
                return
            cached_folder = cached_folder_by_path.get(folder)
            cached_tracks = cached_folder.get("tracks", []) if cached_folder else []
            cached_fingerprints = (
                cached_folder.get("file_fingerprints", {})
                if isinstance(cached_folder, dict)
                else {}
            )
            if not isinstance(cached_fingerprints, dict):
                cached_fingerprints = {}
            self.scan_stats["enumerated"] += len(tracks)
            self.scan_stats["removed"] += len(set(cached_tracks) - set(tracks))
            total = len(tracks)
            for i, path in enumerate(tracks, 1):
                if self.is_cancelled():
                    self.canceled_scan.emit()
                    return
                cached_meta = cached_meta_by_path.get(path)
                previous_fingerprint = cached_fingerprints.get(path)
                current_fingerprint = fingerprints.get(path, {})
                if (
                    cached_meta
                    and isinstance(previous_fingerprint, dict)
                    and audio_fingerprint_matches(
                        previous_fingerprint, current_fingerprint
                    )
                    and (not self.force_full_reread or not meta_needs_backfill(cached_meta))
                ):
                    if lrc_fingerprint_matches(
                        previous_fingerprint, current_fingerprint
                    ):
                        meta_list.append(cached_meta)
                        self.scan_stats["metadata_reused"] += 1
                    else:
                        meta_list.append(
                            metadata_with_lrc_fingerprint(
                                cached_meta, current_fingerprint
                            )
                        )
                        self.scan_stats["metadata_reused"] += 1
                        self.scan_stats["lyrics_only_updated"] += 1
                    if i % 250 == 0 or i == total:
                        self.progress.emit(
                            "Checking unchanged tracks", path, i, total
                        )
                    continue
                self.progress.emit("Reading changed tags", path, i, total)
                meta_list.append(read_track_meta(path))
                self.scan_stats["metadata_read"] += 1
                if cached_meta:
                    if isinstance(previous_fingerprint, dict):
                        self.scan_stats["modified"] += 1
                    else:
                        self.scan_stats["fingerprints_baselined"] += 1
                else:
                    self.scan_stats["added"] += 1
                if self.force_full_reread and i % 300 == 0:
                    self._checkpoint_backfill_progress(
                        cached_folder_by_path,
                        cached_meta_by_path,
                        folder_entries,
                        meta_list,
                        current_folder_entry={
                            "path": folder,
                            "signature": sig,
                            "tracks": tracks,
                            "file_fingerprints": fingerprints,
                        },
                        current_folder_processed_paths=set(tracks[:i]),
                    )
            folder_entries.append({
                "path": folder,
                "signature": sig,
                "tracks": tracks,
                "file_fingerprints": fingerprints,
            })
            if self.force_full_reread:
                self._checkpoint_backfill_progress(
                    cached_folder_by_path,
                    cached_meta_by_path,
                    folder_entries,
                    meta_list,
                )
        self.finished_scan.emit(meta_list, folder_entries)


class AlbumTagRefreshWorker(QtCore.QThread):
    """Re-reads tags for one album group and rescans its folder(s) for new
    files, off the GUI thread.

    _show_tree_menu's "Refresh Tags For This Album" action used to do this
    inline on the GUI thread -- read_track_meta() per track plus an
    os.walk() per folder, both real file I/O. For an album backed by a
    slow network share (confirmed via a captured GUI-stall trace: the main
    thread stuck inside mutagen's _openfile()/read_track_meta() while
    handling this exact menu action) this froze the whole window for as
    long as the I/O took. Scoped to just the one album's meta/folders
    (unlike LibraryScanThread, which always covers the whole library) so
    it stays a fast, targeted refresh rather than a full rescan."""
    finished_refresh = QtCore.pyqtSignal(list)

    def __init__(self, album: str, full_meta_list: List[Dict[str, Any]]):
        super().__init__()
        self._album = album
        # Snapshot: the GUI thread must not mutate this list while we read
        # it here, and we return a whole new list rather than editing it
        # in place.
        self._full_meta_list = list(full_meta_list)
        # v1.0.66 worker-lifetime hardening: this had no cancellation
        # mechanism at all before -- checked at each per-track/per-file
        # read (the same bounded-interval granularity LibraryScanThread's
        # own cancel() already uses) so app shutdown can interrupt a
        # refresh mid-album on a slow/network share instead of only being
        # able to wait for it. finished_refresh is deliberately still
        # emitted on cancellation (with whatever was refreshed so far) --
        # the caller (WorkerLifetimeRegistry, via its "album_tag_refresh"
        # registration) never connects a slot expecting a "cancelled"
        # signal shape, and the window.py result handler's own _closing
        # check is what actually decides whether to apply it.
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        album_dirs = set()
        existing_paths = set()
        refreshed: List[Dict[str, Any]] = []
        for meta in self._full_meta_list:
            if self._cancelled:
                break
            path = meta.get("path")
            if path:
                existing_paths.add(path)
            if meta.get("album") == self._album:
                if path:
                    album_dirs.add(os.path.dirname(path))
                    refreshed.append(read_track_meta(path))
            else:
                refreshed.append(meta)
        if not self._cancelled:
            for folder in sorted(album_dirs):
                if self._cancelled:
                    break
                if not folder or not os.path.isdir(folder):
                    continue
                for root, _, files in os.walk(folder):
                    if self._cancelled:
                        break
                    for name in files:
                        if self._cancelled:
                            break
                        if not is_library_scannable(name):
                            continue
                        path = os.path.join(root, name)
                        if path in existing_paths:
                            continue
                        refreshed.append(read_track_meta(path))
                        existing_paths.add(path)
        self.finished_refresh.emit(refreshed)


class TrackTagLoadWorker(QtCore.QThread):
    """Reads the full "Tags" panel fields for one track off the GUI thread.

    _activate_track_ui() runs on every track change and used to call
    mutagen directly inline -- confirmed via a captured stall trace to
    freeze the GUI thread (including Cast's progress bar/equaliser tick)
    for as long as a slow/NAS read took. This does the same read
    (read_full_tag_display) on a worker thread instead; the caller shows
    cached data immediately and swaps in this result once it arrives.
    """
    tags_ready = QtCore.pyqtSignal(str, dict)

    def __init__(self, path: str):
        super().__init__()
        self._path = path

    def run(self):
        try:
            fields = read_full_tag_display(self._path)
        except Exception:
            return
        self.tags_ready.emit(self._path, fields)


class LyricsLoadWorker(QtCore.QThread):
    """Reads synced lyrics (embedded tags, falling back to a sidecar .lrc
    file) for one track off the GUI thread.

    v1.0.67 MainThread I/O hardening: _load_lrc_for_track used to run
    lyrics.load_lyrics_for_track's exact logic -- a MutagenFile(path) open,
    and on a miss, a sidecar file read -- synchronously on every AUDIO
    track activation. Mirrors TrackTagLoadWorker's shape exactly: the
    caller shows/keeps whatever lyrics state it already has immediately
    and swaps in this result once it arrives, generation/path-guarded.
    """
    lyrics_ready = QtCore.pyqtSignal(str, list, str)  # path, entries, source

    def __init__(self, path: str):
        super().__init__()
        self._path = path

    def run(self):
        try:
            entries, source = load_lyrics_for_track(self._path)
        except Exception:
            entries, source = [], "none"
        self.lyrics_ready.emit(self._path, entries, source)


class GainLookupWorker(QtCore.QThread):
    """Reads ReplayGain tags and validates the cached loudness-analysis
    signature for one track off the GUI thread.

    v1.0.67 MainThread I/O hardening: _gain_details_for_path used to call
    read_replaygain(path) (a Mutagen open) and loudness_cache.analysis_for(path)
    (an os.stat-based signature check) synchronously on every track
    activation/crossfade/recovery -- the same class of NAS-stall bug fixed
    elsewhere this round for lyrics and tags. loudness_cache is only ever
    read here (override_for/analysis_for), never written -- writes
    (store_analysis/set_override/remove_analysis) stay on the GUI thread.
    """
    gain_ready = QtCore.pyqtSignal(str, dict, object)  # path, replaygain tags, measured-or-None

    def __init__(self, path: str, loudness_cache):
        super().__init__()
        self._path = path
        self._loudness_cache = loudness_cache

    def run(self):
        try:
            tags = read_replaygain(self._path)
        except Exception:
            tags = {"track_gain": None, "track_peak": None, "album_gain": None, "album_peak": None}
        try:
            measured = self._loudness_cache.analysis_for(self._path)
        except Exception:
            measured = None
        self.gain_ready.emit(self._path, tags, measured)


class CastPayloadWorker(QtCore.QThread):
    """Builds one track's Cast metadata (tag read + artwork extract/scale/
    encode) off the GUI thread.

    v1.0.67 MainThread I/O hardening: this used to run inline (_cast_payload
    /_cast_artwork_url in window.py) from _on_cast_connected and, on every
    single Cast track advance, _cast_play_path -- a Mutagen tag read plus,
    for tracks with embedded art, a full decode/scale/JPEG-encode/disk-write.
    artwork_dir is a plain path -- the lazily-created temp directory is
    still created once on the GUI thread, since it's always a local OS
    temp path, never a NAS path. QImage decode/scale off the GUI thread
    already has precedent in this codebase (artwork.py's QRunnable
    workers do the same).

    v1.0.69: no longer takes/touches cast_media_server or calls
    register() -- a Codex-reported defect ("Cast server resurrection
    during shutdown") found that a late-completing worker's own
    register() call could restart the HTTP server after application
    shutdown had already torn it down. This worker now only ever
    produces *data* -- the encoded artwork's local file path, not a
    server-scoped URL/token -- and the caller (window.py's
    _request_cast_load/_cast_payload_with_fresh_artwork) does the one
    canonical register() call on the GUI thread, only once the result's
    request generation is confirmed current and the server is confirmed
    open (media_server.py's own closed-state guard is the backstop
    either way).
    """
    payload_ready = QtCore.pyqtSignal(int, str, dict, str)  # generation, path, metadata, artwork_path

    def __init__(self, generation: int, path: str, artwork_dir: str):
        super().__init__()
        self._generation = generation
        self._path = path
        self._artwork_dir = artwork_dir

    def run(self):
        artwork_path = ""
        try:
            meta = read_track_meta(self._path)
            artwork_path = self._prepare_artwork()
            payload = build_music_metadata(meta, "")
        except Exception:
            payload = {}
        self.payload_ready.emit(self._generation, self._path, payload, artwork_path)

    def _prepare_artwork(self) -> str:
        """Returns the local encoded-artwork file path, or "" if the
        track has no usable embedded art -- never a registered URL (see
        class docstring)."""
        try:
            data = read_cover_bytes(self._path)
            if not data:
                return ""
            image = QtGui.QImage.fromData(data)
            if image.isNull():
                return ""
            image = image.scaled(
                512, 512,
                QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            )
            target = os.path.join(self._artwork_dir, f"cover-{self._generation}.jpg")
            if not image.save(target, "JPG", 88):
                return ""
            return target
        except Exception:
            return ""


def musicbrainz_release_id(artist: str, album: str):
    """Pure lookup, no I/O beyond the one HTTP call -- pulled out of
    PlayerWindow._musicbrainz_release_id (window.py) unchanged so
    AlbumArtFetchWorker can call it off the GUI thread; it never touched
    `self` in the first place."""
    query_parts = [f'release:"{album}"']
    if artist:
        query_parts.append(f'artist:"{artist}"')
    query = " AND ".join(query_parts)
    params = {"query": query, "fmt": "json", "limit": 1}
    url = "https://musicbrainz.org/ws/2/release/?" + urllib.parse.urlencode(params)
    data = json.loads(http_get(url).decode("utf-8", errors="ignore"))
    releases = data.get("releases") or []
    if not releases:
        return None
    return releases[0].get("id")


class AlbumArtFetchWorker(QtCore.QThread):
    """Fetches album art from MusicBrainz + Cover Art Archive off the GUI
    thread.

    v1.0.67 MainThread I/O hardening: _fetch_album_art_online used to make
    two synchronous HTTP requests (MusicBrainz release lookup, then a Cover
    Art Archive image fetch, each up to a 10s timeout with internal
    retries -- http_get in net.py) directly on the GUI thread, from the
    "Fetch Album Art Online" menu action. A slow/unreachable network could
    freeze the whole window for tens of seconds, worse than any NAS stall
    fixed elsewhere this round.
    """
    art_ready = QtCore.pyqtSignal(str, bytes, str)  # album_key, cover_bytes, error

    def __init__(self, album_key: str, artist: str, album: str):
        super().__init__()
        self._album_key = album_key
        self._artist = artist
        self._album = album

    def run(self):
        try:
            mbid = musicbrainz_release_id(self._artist, self._album)
            if not mbid:
                self.art_ready.emit(self._album_key, b"", "No match found on MusicBrainz.")
                return
            art_url = f"https://coverartarchive.org/release/{mbid}/front-250"
            cover_bytes = http_get(art_url)
            if self._artist:
                path = album_cover_cache_path(self._artist, self._album)
                with open(path, "wb") as f:
                    f.write(cover_bytes)
            self.art_ready.emit(self._album_key, cover_bytes, "")
        except Exception as ex:
            self.art_ready.emit(self._album_key, b"", str(ex))


class PlexConnectionTestWorker(QtCore.QThread):
    """Runs one Plex "Test Connection" attempt off the GUI thread: server
    identity + library-section discovery. Mirrors AlbumArtFetchWorker's
    shape (one QThread per attempt, one result signal, registered with
    _worker_registry by the caller before start()).

    Bounded: PlexClient's own request timeouts (see plex_client.py) already
    bound how long a single HTTP call can take; this worker does not add
    its own additional timeout layer on top.

    Never includes the token or a token-bearing URL in the emitted result
    or in any diagnostics recorded here -- the token is only ever sent as
    an HTTP header (see PlexClient._headers()), never placed in a URL by
    this module, so there is nothing token-shaped to accidentally log."""

    finished_result = QtCore.pyqtSignal(dict)

    def __init__(self, server_address: str, token: str, client_identifier: str):
        super().__init__()
        self._server_address = server_address
        self._token = token
        self._client_identifier = client_identifier
        self._diagnostics = get_diagnostics()

    def run(self):
        # Same hazard class analysis_warmup.py exists for (a first-time
        # import of a package that lazily resolves a compiled extension,
        # observed to access-violate when it happens on a background
        # thread) -- requests pulls in idna/charset_normalizer the first
        # time it's imported anywhere in the process. _warm_up_requests()
        # already does this proactively on the main thread during normal
        # startup, but this worker can be the very first thing to import
        # plex_client.py/requests at all (e.g. a test constructing
        # PlayerWindow directly, bypassing Main.py's launcher) -- blocking
        # here until warm-up has genuinely finished, exactly like
        # QueueAnalysisWorker._analyse_track already does for librosa,
        # closes that window instead of racing it.
        from . import analysis_warmup
        analysis_warmup.wait_until_ready()

        from .plex_client import PlexClient, PlexConnectionError

        started = time.perf_counter()
        client = PlexClient(self._server_address, self._token, self._client_identifier)
        try:
            info = client.fetch_server_info()
        except PlexConnectionError as ex:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self._diagnostics.record(
                "plex", "plex_connection_test",
                details={
                    "success": False, "reason": ex.reason,
                    "elapsed_ms": round(elapsed_ms, 1),
                    "has_token": bool(self._token),
                },
                minimum_level="basic",
            )
            self.finished_result.emit({"success": False, "reason": ex.reason, "detail": ex.detail})
            return
        except Exception as ex:  # pragma: no cover - defensive, unexpected shape
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self._diagnostics.record(
                "plex", "plex_connection_test",
                details={
                    "success": False, "reason": "Invalid Plex response",
                    "elapsed_ms": round(elapsed_ms, 1),
                    "has_token": bool(self._token),
                },
                severity="warning",
                minimum_level="basic",
            )
            self.finished_result.emit({
                "success": False, "reason": "Invalid Plex response",
                "detail": type(ex).__name__,
            })
            return
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        libraries = [
            {"key": section.key, "title": section.title, "type": section.type}
            for section in info.libraries
        ]
        self._diagnostics.record(
            "plex", "plex_connection_test",
            details={
                "success": True,
                "elapsed_ms": round(elapsed_ms, 1),
                "has_token": bool(self._token),
                "library_count": len(libraries),
            },
            minimum_level="basic",
        )
        self._diagnostics.record(
            "plex", "plex_library_discovery",
            details={"library_count": len(libraries)},
            minimum_level="detailed",
        )
        self.finished_result.emit({
            "success": True,
            "friendly_name": info.friendly_name,
            "version": info.version,
            "machine_identifier": info.machine_identifier,
            "libraries": libraries,
        })


class PlexSignInWorker(QtCore.QThread):
    """Drives the Plex account PIN sign-in flow off the GUI thread: create
    a PIN, open the user's default browser to plex.tv's own hosted sign-in
    page (never an embedded/imitated login form -- Bills Music Player
    never sees the username/password), then poll until the PIN carries an
    authToken, expires, or is cancelled.

    Cooperative cancellation: request_cancel() sets a flag the polling
    loop checks between polls (bounded by the poll interval, matching the
    cancel callback contract _worker_registry expects).
    """

    finished_result = QtCore.pyqtSignal(dict)
    pin_ready = QtCore.pyqtSignal(str)  # auth_url, so the caller can open it immediately

    POLL_INTERVAL_S = 1.5
    MAX_WAIT_S = 15 * 60  # Plex PINs are typically valid for ~15 minutes

    def __init__(self, client_identifier: str):
        super().__init__()
        self._client_identifier = client_identifier
        self._diagnostics = get_diagnostics()
        self._cancelled = False

    def request_cancel(self):
        self._cancelled = True

    def run(self):
        from . import analysis_warmup
        analysis_warmup.wait_until_ready()

        from .plex_account import PlexAccountClient
        from .plex_client import PlexConnectionError

        started = time.perf_counter()
        client = PlexAccountClient(self._client_identifier)
        try:
            pin = client.create_pin()
        except PlexConnectionError as ex:
            self._diagnostics.record(
                "plex", "plex_signin_failed",
                details={"reason": ex.reason, "stage": "create_pin"},
                minimum_level="basic",
            )
            self.finished_result.emit({"success": False, "reason": ex.reason})
            return

        auth_url = PlexAccountClient.auth_url(self._client_identifier, pin.code)
        self.pin_ready.emit(auth_url)
        self._diagnostics.record(
            "plex", "plex_signin_pin_created",
            details={"elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1)},
            minimum_level="detailed",
        )

        deadline = time.monotonic() + self.MAX_WAIT_S
        while time.monotonic() < deadline:
            if self._cancelled:
                self._diagnostics.record(
                    "plex", "plex_signin_failed",
                    details={"reason": "Cancelled", "stage": "poll"},
                    minimum_level="basic",
                )
                self.finished_result.emit({"success": False, "reason": "Cancelled"})
                return
            self.msleep(int(self.POLL_INTERVAL_S * 1000))
            if self._cancelled:
                continue  # loop back to the cancellation check above
            try:
                pin = client.check_pin(pin.pin_id)
            except PlexConnectionError as ex:
                if ex.reason == "PIN expired":
                    self._diagnostics.record(
                        "plex", "plex_signin_failed",
                        details={"reason": "PIN expired", "stage": "poll"},
                        minimum_level="basic",
                    )
                    self.finished_result.emit({"success": False, "reason": "PIN expired"})
                    return
                continue  # transient network hiccup -- keep polling until deadline
            if pin.auth_token:
                try:
                    username = client.fetch_account_username(pin.auth_token)
                except PlexConnectionError:
                    username = ""
                self._diagnostics.record(
                    "plex", "plex_signin_succeeded",
                    details={"elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1)},
                    minimum_level="basic",
                )
                self.finished_result.emit({
                    "success": True, "account_token": pin.auth_token, "username": username,
                })
                return

        self._diagnostics.record(
            "plex", "plex_signin_failed",
            details={"reason": "PIN expired", "stage": "timeout"},
            minimum_level="basic",
        )
        self.finished_result.emit({"success": False, "reason": "PIN expired"})


class PlexServerDiscoveryWorker(QtCore.QThread):
    """Lists the Plex servers an authenticated account can reach, off the
    GUI thread. Does not resolve/probe connections itself (see
    PlexConnectionResolveWorker) -- this is the cheap "what servers exist"
    call, kept separate so selecting a different server doesn't need to
    re-fetch the resource list."""

    finished_result = QtCore.pyqtSignal(dict)

    def __init__(self, client_identifier: str, account_token: str):
        super().__init__()
        self._client_identifier = client_identifier
        self._account_token = account_token
        self._diagnostics = get_diagnostics()

    def run(self):
        from . import analysis_warmup
        analysis_warmup.wait_until_ready()

        from .plex_account import PlexAccountClient
        from .plex_client import PlexConnectionError

        started = time.perf_counter()
        client = PlexAccountClient(self._client_identifier)
        try:
            resources = client.fetch_resources(self._account_token)
        except PlexConnectionError as ex:
            self._diagnostics.record(
                "plex", "plex_server_discovery_failed",
                details={"reason": ex.reason},
                minimum_level="basic",
            )
            self.finished_result.emit({"success": False, "reason": ex.reason})
            return
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self._diagnostics.record(
            "plex", "plex_server_discovery_completed",
            details={"elapsed_ms": round(elapsed_ms, 1), "server_count": len(resources)},
            minimum_level="basic",
        )
        self.finished_result.emit({
            "success": True,
            "servers": [
                {
                    "name": r.name, "client_identifier": r.client_identifier,
                    "owned": r.owned, "access_token": r.access_token,
                    "connections": [
                        {
                            "protocol": c.protocol, "address": c.address, "port": c.port,
                            "uri": c.uri, "local": c.local, "relay": c.relay,
                        }
                        for c in r.connections
                    ],
                }
                for r in resources
            ],
        })


class PlexConnectionResolveWorker(QtCore.QThread):
    """Probes a chosen server's candidate connections and resolves the
    best reachable one (local preferred, remote/relay fallback), off the
    GUI thread. Separate from PlexServerDiscoveryWorker so re-resolving
    (e.g. on startup, or after a network change) doesn't need another
    account-level resources fetch."""

    finished_result = QtCore.pyqtSignal(dict)

    def __init__(self, client_identifier: str, server_client_identifier: str,
                 access_token: str, connections: list):
        super().__init__()
        self._client_identifier = client_identifier
        self._server_client_identifier = server_client_identifier
        self._access_token = access_token
        self._connections = connections
        self._diagnostics = get_diagnostics()

    def run(self):
        from . import analysis_warmup
        analysis_warmup.wait_until_ready()

        from .plex_account import PlexConnection, select_best_connection

        started = time.perf_counter()
        connections = [
            PlexConnection(
                protocol=c["protocol"], address=c["address"], port=c["port"],
                uri=c["uri"], local=c["local"], relay=c["relay"],
            )
            for c in self._connections
        ]
        best = select_best_connection(
            connections, self._access_token, self._client_identifier,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self._diagnostics.record(
            "plex", "plex_connection_resolved" if best else "plex_connection_resolve_failed",
            details={
                "elapsed_ms": round(elapsed_ms, 1),
                "candidate_count": len(connections),
                "local": bool(best and best.local),
                "relay": bool(best and best.relay),
            },
            minimum_level="basic",
        )
        if best is None:
            self.finished_result.emit({"success": False, "reason": "Server unreachable"})
            return
        self.finished_result.emit({
            "success": True, "uri": best.uri, "local": best.local, "relay": best.relay,
            "access_token": self._access_token,
        })


class PlexLibraryFetchWorker(QtCore.QThread):
    """Fetches every item in ONE Plex library section in ONE bulk API
    call (never one request per track/video -- see PlexClient.
    fetch_section_items's own docstring), off the GUI thread, and
    converts the raw Plex JSON into meta_list-shaped dicts
    (plex_metadata.py) ready for the existing local-library tree/search
    infrastructure. Carries a `generation` token through untouched so the
    caller can discard a stale result if the source/server/mapping
    changed while this was in flight (see window.py's
    _plex_fetch_generation)."""

    finished_result = QtCore.pyqtSignal(dict)
    # (media_kind, message) -- indeterminate stage text only, never a
    # fake percentage (see window.py's _on_plex_library_fetch_progress).
    progress = QtCore.pyqtSignal(str, str)

    # 500 items/page: for a 43,000-track library that's ~86 requests --
    # few enough not to be chatty, small enough that each page's JSON
    # payload (with embedded Media/Part/Genre per item) comfortably
    # fetches inside the existing 8s connect/read timeout regardless of
    # total library size. This bounds each REQUEST, which is what
    # actually fixes a large-library timeout -- a single unbounded
    # request for tens of thousands of tracks can legitimately exceed
    # any one-shot timeout budget; bumping that budget wouldn't fix a
    # library twice as large again, bounding the request size does.
    PAGE_SIZE = 500
    # Safety bound only (500 * 2000 = 1,000,000 items) -- guards against
    # a pathological loop if a server's own totalSize/pagination
    # behaviour is ever inconsistent; no real personal library approaches
    # this.
    MAX_PAGES = 2000

    def __init__(self, server_address: str, token: str, client_identifier: str,
                 section_key: str, item_type, media_kind: str,
                 server_config_id: str, generation: int):
        super().__init__()
        self._server_address = server_address
        self._token = token
        self._client_identifier = client_identifier
        self._section_key = section_key
        self._item_type = item_type
        self._media_kind = media_kind  # "music" | "video" | "karaoke"
        self._server_config_id = server_config_id
        self._generation = generation
        self._diagnostics = get_diagnostics()
        self._running = True

    def stop(self):
        # Cooperative cancellation checked between pages -- a fetch mid-
        # flight on one page still completes that single bounded request
        # (never left half-read), just never starts the next one.
        self._running = False

    def run(self):
        from . import analysis_warmup
        analysis_warmup.wait_until_ready()

        from .plex_client import PlexClient, PlexConnectionError
        from .plex_metadata import (
            plex_karaoke_to_meta_dict, plex_track_to_meta_dict, plex_video_to_meta_dict,
        )

        convert = {
            "music": plex_track_to_meta_dict,
            "video": plex_video_to_meta_dict,
            "karaoke": plex_karaoke_to_meta_dict,
        }[self._media_kind]
        label = {"music": "Music", "video": "Video", "karaoke": "Karaoke"}[self._media_kind]

        self._diagnostics.record(
            "plex", "plex_library_fetch_started",
            details={
                "category": self._media_kind, "section_key": self._section_key,
                "server_hash": self._diagnostics.path_details(
                    self._server_config_id
                ).get("path_hash", ""),
            },
            minimum_level="basic",
        )
        self.progress.emit(self._media_kind, f"Retrieving {label} library…")
        started = time.perf_counter()
        client = PlexClient(self._server_address, self._token, self._client_identifier)
        raw_items: list = []
        total_size = None
        page_count = 0
        try:
            start = 0
            while page_count < self.MAX_PAGES:
                if not self._running:
                    self.finished_result.emit({
                        "success": False, "reason": "Cancelled",
                        "media_kind": self._media_kind, "generation": self._generation,
                    })
                    return
                page_items, page_meta = client.fetch_section_items_page(
                    self._section_key, self._item_type, start, self.PAGE_SIZE,
                )
                page_count += 1
                if total_size is None:
                    # Plex's own reported total from the FIRST page --
                    # every subsequent progress message and the stop
                    # condition below are driven by this real number,
                    # never a guess. A section with no totalSize (some
                    # older/unusual servers) just means "one page was
                    # everything" -- falls back to this page's own count.
                    total_size = page_meta.get("total_size") or len(page_items)
                raw_items.extend(page_items)
                start += len(page_items)
                if total_size:
                    self.progress.emit(
                        self._media_kind,
                        f"Retrieving {label} library… {min(start, total_size):,} / {total_size:,}",
                    )
                if not page_items or start >= total_size:
                    break
        except PlexConnectionError as ex:
            self._diagnostics.record(
                "plex", "plex_library_refresh_failed",
                details={
                    "reason": ex.reason, "media_kind": self._media_kind,
                    "items_fetched_before_failure": len(raw_items),
                    "page_count": page_count,
                },
                minimum_level="basic",
            )
            self.finished_result.emit({
                "success": False, "reason": ex.reason,
                "media_kind": self._media_kind, "generation": self._generation,
            })
            return

        fetch_elapsed_ms = (time.perf_counter() - started) * 1000.0
        item_types = Counter(raw.get("type") for raw in raw_items)
        self._diagnostics.record(
            "plex", "plex_library_fetch_response",
            details={
                "category": self._media_kind,
                "http_status": 200,  # any non-2xx already raised PlexConnectionError above
                "container_total_size": total_size,
                "raw_item_count": len(raw_items),
                "page_count": page_count, "page_size": self.PAGE_SIZE,
                "item_types_encountered": dict(item_types),
                "elapsed_ms": round(fetch_elapsed_ms, 1),
            },
            minimum_level="basic",
        )
        self.progress.emit(
            self._media_kind, f"Processing {len(raw_items):,} {self._media_kind}…",
        )

        parse_started = time.perf_counter()
        meta_list = []
        rejected_reasons: Counter = Counter()
        for raw in raw_items:
            try:
                meta = convert(raw, self._server_config_id)
            except Exception as ex:
                # One malformed item must never silently kill the whole
                # fetch (an uncaught exception in a QThread.run() just
                # ends the thread -- finished_result never fires, and the
                # tab is left looking identical to "still loading"
                # forever). Skip it, count why, keep going.
                rejected_reasons[f"exception:{type(ex).__name__}"] += 1
                continue
            if meta is None:
                rejected_reasons["no_rating_key"] += 1
                continue
            meta_list.append(meta)
        parse_elapsed_ms = (time.perf_counter() - parse_started) * 1000.0

        self._diagnostics.record(
            "plex", "plex_library_conversion_completed",
            details={
                "category": self._media_kind,
                "input_count": len(raw_items), "output_count": len(meta_list),
                "artists": len({m.get("artist") for m in meta_list if m.get("artist")}),
                "albums": len({m.get("album") for m in meta_list if m.get("album")}),
                "tracks": len(meta_list),
                "rejected_count": sum(rejected_reasons.values()),
                "rejection_reasons": dict(rejected_reasons),
            },
            minimum_level="basic",
        )
        if self._media_kind == "video" and raw_items:
            # item 9: counts only, never actual titles/filenames -- shows
            # exactly which Plex metadata is actually populated for this
            # library, so a one-bucket-per-alphabet-letter fallback (see
            # plex_metadata.py's _plex_video_alphabetical_group) can be
            # explained/audited rather than silently accepted as "the fix".
            field_presence = {
                field: sum(1 for raw in raw_items if raw.get(field))
                for field in ("grandparentTitle", "parentTitle", "originalTitle", "title", "studio")
            }
            field_presence["Artist_tag"] = sum(
                1 for raw in raw_items
                if isinstance(raw.get("Artist"), list) and raw.get("Artist")
            )
            field_presence["Media_Part"] = sum(
                1 for raw in raw_items
                if isinstance(raw.get("Media"), list) and raw.get("Media")
                and isinstance(raw["Media"][0], dict)
                and raw["Media"][0].get("Part")
            )
            self._diagnostics.record(
                "plex", "plex_video_field_presence",
                details={"item_count": len(raw_items), "field_presence": field_presence},
                minimum_level="basic",
            )
        self.progress.emit(self._media_kind, "Building library view…")

        self._diagnostics.record(
            "plex", "plex_library_refresh_completed",
            details={
                "media_kind": self._media_kind, "item_count": len(meta_list),
                "fetch_elapsed_ms": round(fetch_elapsed_ms, 1),
                "parse_elapsed_ms": round(parse_elapsed_ms, 1),
            },
            minimum_level="basic",
        )
        self.finished_result.emit({
            "success": True, "media_kind": self._media_kind,
            "meta_list": meta_list, "generation": self._generation,
        })


class QueueAnalysisWorker(QtCore.QThread):
    """Background BPM/key estimator for tracks shown in Up Next."""
    result_ready = QtCore.pyqtSignal(str, dict)
    metadata_ready = QtCore.pyqtSignal(str, dict)

    # Class-level (not local) so tests can monkeypatch them down for fast,
    # deterministic timeout/lifecycle coverage without waiting out a real
    # 20s hard timeout.
    VIDEO_DECODE_HARD_TIMEOUT_MS = 20000
    VIDEO_DECODE_POLL_INTERVAL_MS = 200

    def __init__(self):
        super().__init__()
        self._lock = QtCore.QMutex()
        self._cond = QtCore.QWaitCondition()
        self._queue: List[Tuple[str, bool, frozenset]] = []
        self._queued = set()
        self._done = set()
        self._done_results: Dict[Tuple[str, bool], Dict[str, str]] = {}
        self._running = True
        self._diagnostics = get_diagnostics()
        self._diagnostic_tokens = {}

    def request(self, path: str, fields: frozenset = frozenset({"bpm", "key"})):
        # Defense in depth: ineligible paths must never reach BPM/key/librosa
        # analysis. Registry lookup is O(1), extension-only, and performs no I/O.
        if not is_bpm_key_eligible(path):
            self.request_metadata(path)
            return
        self._enqueue(path, metadata_only=False, fields=fields)

    def request_metadata(self, path: str):
        self._enqueue(path, metadata_only=True, fields=frozenset())

    def _enqueue(self, path: str, metadata_only: bool, fields: frozenset = frozenset()):
        if not path:
            return
        request_key = (path, metadata_only)
        replay_result = None
        with QtCore.QMutexLocker(self._lock):
            if request_key in self._queued:
                return
            if request_key in self._done:
                replay_result = self._done_results.get(request_key, {})
            else:
                self._queue.append((path, metadata_only, fields))
                self._queued.add(request_key)
                self._diagnostic_tokens[request_key] = self._diagnostics.submit_worker(
                    "worker",
                    "queue_track_metadata" if metadata_only
                    else "queue_track_analysis",
                    details=self._diagnostics.path_details(path),
                )
                self._cond.wakeAll()
        if replay_result is not None:
            # A duplicate request for an already-completed job used to be
            # silently dropped here (no signal ever emitted) since self._done
            # is never cleared for the worker's lifetime -- that left the
            # caller's "pending"/spinner bookkeeping stuck forever for
            # duplicate Up Next rows, requeues, or "Play Next" on an
            # already-analysed path. Replay the last known result instead.
            # request()/request_metadata() are only ever called from the GUI
            # thread, so this resolves synchronously within the same call.
            self._diagnostics.record(
                "worker", "bpm_key_analysis_queued",
                details={
                    **self._diagnostics.path_details(path),
                    "replay": True, "metadata_only": metadata_only,
                },
                minimum_level="detailed",
            )
            if metadata_only:
                self.metadata_ready.emit(path, replay_result)
            else:
                self.result_ready.emit(path, replay_result)

    def run(self):
        while self._running:
            with QtCore.QMutexLocker(self._lock):
                while self._running and not self._queue:
                    self._cond.wait(self._lock, 500)
                if not self._running:
                    break
                path, metadata_only, fields = self._queue.pop(0)
                request_key = (path, metadata_only)
                self._queued.discard(request_key)
            token = self._diagnostic_tokens.pop(request_key, None)
            if token is not None:
                with self._diagnostics.run_worker(token):
                    result = (
                        self._analyse_metadata(path)
                        if metadata_only else self._analyse_track(path, fields)
                    )
            else:
                result = (
                    self._analyse_metadata(path)
                    if metadata_only else self._analyse_track(path, fields)
                )
            with QtCore.QMutexLocker(self._lock):
                self._done.add(request_key)
                self._done_results[request_key] = result
            if self._running:
                if metadata_only:
                    self.metadata_ready.emit(path, result)
                elif result:
                    self.result_ready.emit(path, result)
            self.msleep(250 if metadata_only else 80)

    def stop(self):
        self._running = False
        with QtCore.QMutexLocker(self._lock):
            self._cond.wakeAll()

    def _analyse_track(
        self, path: str, fields: frozenset = frozenset({"bpm", "key"}),
    ) -> Dict[str, str]:
        started = time.monotonic()
        path_details = self._diagnostics.path_details(path)
        from .media_type import MediaType, classify_path
        is_video = classify_path(path) == MediaType.VIDEO
        # v1.0.68: "qaudiodecoder" (video, librosa never touched) vs
        # "native" (audio, librosa when available/warmed, soundfile
        # fallback otherwise) -- so bpm_key_analysis_failed's reason is
        # traceable to the actual route taken instead of being ambiguous
        # between the two.
        #
        # v1.0.72: a frozen build sets LIBROSA_AVAILABLE=False, which
        # previously starved *audio* of any working decode route at all
        # (see the warmup-gate comment below) even though video's own
        # QAudioDecoder path already proved Qt's decoder handles
        # compressed containers fine without librosa. "native_qaudiodecoder"
        # names this third, audio-specific case distinctly from video's
        # "qaudiodecoder" (a real decode failure/timeout/cancellation is
        # otherwise indistinguishable between the two decoders sharing
        # this diagnostic) and from ordinary "native" (still librosa/
        # soundfile, unchanged whenever librosa is actually available).
        fallback_reason = None
        if is_video:
            decoder = "qaudiodecoder"
        elif not LIBROSA_AVAILABLE:
            decoder = "native_qaudiodecoder"
            fallback_reason = "librosa_unavailable"
        else:
            decoder = "native"
        self._diagnostics.record(
            "worker", "bpm_key_analysis_started",
            details={
                **path_details, "requested_fields": sorted(fields),
                "decoder": decoder, "librosa_available": LIBROSA_AVAILABLE,
                "fallback_reason": fallback_reason,
            },
            minimum_level="detailed",
        )

        def failed(reason: str) -> Dict[str, str]:
            self._diagnostics.record(
                "worker", "bpm_key_analysis_failed",
                details={
                    **path_details, "reason": reason, "decoder": decoder,
                    "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
                },
                minimum_level="detailed",
            )
            return result

        def completed(res: Dict[str, str]) -> Dict[str, str]:
            self._diagnostics.record(
                "worker", "bpm_key_analysis_completed",
                details={
                    **path_details,
                    "bpm_present": bool(res.get("bpm")),
                    "key_present": bool(res.get("key")),
                    "decoder": decoder,
                    "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
                },
                minimum_level="detailed",
            )
            return res

        result = self._analyse_metadata(path)
        if not fields:
            return completed(result)
        if np is None:
            return failed("no_numpy")
        # librosa's first real use does expensive, previously crash-prone
        # lazy import/JIT work (see analysis_warmup.py) -- block here,
        # deterministically, until that has completed or failed, rather
        # than racing it. If it failed, degrade to metadata-only instead
        # of touching librosa at all.
        #
        # v1.0.68: that gate must only apply when this analysis could
        # actually *reach* librosa. Video's decode path
        # (_decode_video_audio_preview, QAudioDecoder-based) never
        # references librosa at all, and _estimate_bpm/_estimate_key each
        # independently gate their own librosa use behind the same
        # LIBROSA_AVAILABLE constant checked here, with a complete
        # NumPy-only fallback either way. In a frozen build where librosa
        # is intentionally unavailable (LIBROSA_AVAILABLE=False), librosa
        # warmup can never succeed -- analysis_warmup.succeeded() is
        # always False -- so every video BPM/key request was previously
        # failing with reason="warmup_failed" before ever reaching the
        # QAudioDecoder path that was fully capable of serving it.
        #
        # v1.0.72: the same reasoning applies just as directly to AUDIO --
        # _decode_native_audio_preview (below) reuses that identical
        # QAudioDecoder machinery and never touches librosa either, so
        # ordinary MP3/FLAC/etc. BPM/Key requests were failing with the
        # same warmup_failed misdiagnosis in a frozen build, even though a
        # fully-capable non-librosa decode route existed. The gate is now
        # simply "skip only when librosa is unavailable" -- when it *is*
        # available, both video and audio keep waiting on/requiring a
        # successful warmup exactly as before (video's own decode never
        # needed that wait either way, but this preserves that pre-existing
        # behaviour rather than optimising it away here).
        if LIBROSA_AVAILABLE:
            from . import analysis_warmup
            analysis_warmup.wait_until_ready()
            if not analysis_warmup.succeeded():
                return failed("warmup_failed")
        with native_analysis_slot(lambda: self._running) as acquired:
            if not acquired:
                return failed("lock_unavailable")
            try:
                if is_video:
                    y, sr = self._decode_video_audio_preview(path)
                elif not LIBROSA_AVAILABLE:
                    y, sr = self._decode_native_audio_preview(path)
                else:
                    y, sr = self._decode_preview(path)
                if y is None or sr <= 0:
                    if not self._running:
                        return failed("cancelled")
                    return failed("decode_failed")
                if y.size < sr * 8:
                    return failed("insufficient_audio")
                y = np.asarray(y, dtype="float32")
                y = y - float(np.mean(y))
                peak = float(np.max(np.abs(y))) if y.size else 0.0
                if peak > 0:
                    y = y / peak
                # Only estimate fields actually missing -- a shared decode
                # (the expensive part) still happens once regardless, but a
                # value that's already known (cached/tagged) is never
                # recomputed, per the "reuse known values" requirement.
                if "bpm" in fields:
                    bpm = self._estimate_bpm(y, sr)
                    if bpm:
                        result["bpm"] = str(int(round(bpm)))
                if "key" in fields:
                    key = self._estimate_key(y, sr)
                    if key:
                        result["key"] = key
                return completed(result)
            except Exception:
                return failed("exception")

    def _analyse_metadata(self, path: str) -> Dict[str, str]:
        result: Dict[str, str] = {}
        from .media_type import MediaType, classify_path
        if classify_path(path) == MediaType.KARAOKE:
            try:
                file_stat = os.stat(path)
                result["_mtime"] = int(file_stat.st_mtime)
                result["_size"] = int(file_stat.st_size)
            except Exception:
                pass
            return result
        try:
            audio = MutagenFile(path, easy=True)
            info = getattr(audio, "info", None) if audio else None
            bitrate = getattr(info, "bitrate", 0) or 0
            length = getattr(info, "length", 0) or 0
            if bitrate:
                result["bitrate"] = f"{int(bitrate / 1000)}k"
            if length:
                minutes, seconds = divmod(int(round(length)), 60)
                result["time"] = f"{minutes}:{seconds:02d}"
        except Exception:
            pass
        try:
            stat = os.stat(path)
            result["_mtime"] = int(stat.st_mtime)
            result["_size"] = int(stat.st_size)
        except Exception:
            pass
        return result

    def _decode_preview(self, path: str):
        # Analyse only a preview slice so queued tracks don't compete with playback.
        target_sr = 22050
        duration = 90.0
        offset = 20.0
        if LIBROSA_AVAILABLE:
            try:
                y, sr = librosa.load(path, sr=target_sr, mono=True, offset=offset, duration=duration)
                return y, int(sr)
            except Exception:
                pass
        try:
            info = sf.info(path)
            start = min(int(offset * info.samplerate), max(0, info.frames - 1))
            frames = min(int(duration * info.samplerate), max(0, info.frames - start))
            data, sr = sf.read(path, start=start, frames=frames, dtype="float32", always_2d=True)
            mono = data.mean(axis=1)
            if sr and sr != target_sr:
                idx = np.linspace(0, mono.size - 1, int(mono.size * target_sr / sr))
                mono = np.interp(idx, np.arange(mono.size), mono).astype("float32")
                sr = target_sr
            return mono, int(sr)
        except Exception:
            return None, 0

    def _decode_video_audio_preview(self, path: str):
        """Decode a bounded preview slice of a video's audio track via
        QtMultimedia's QAudioDecoder (Qt6's own FFmpeg-backed decoder --
        the same stack already used for real video playback elsewhere in
        this app), producing a mono float32 array at 22050 Hz matching
        _decode_preview's (y, sr) contract so _estimate_bpm/_estimate_key
        are completely unchanged. librosa.load()/soundfile/miniaudio
        cannot open an MP4 container on this stack at all (confirmed
        directly: NoBackendError / "unsupported file format") --
        QAudioDecoder is the only proven-working decode path for video
        audio.

        Runs entirely on this worker's own QThread via a locally-created
        QEventLoop -- QAudioDecoder is signal-driven and needs *some*
        running event loop to make progress, but that loop never needs to
        be (and here never is) the GUI thread's. The decoder is
        constructed with no parent, entirely inside this method, so its
        thread affinity is this worker thread by construction; it is
        never moved to or touched from MainThread anywhere in this
        design.

        QAudioDecoder has no setPosition()/seek in this Qt version
        (confirmed via hasattr), so this over-decodes to roughly
        offset+duration worth of samples from position 0, then slices the
        [offset:offset+duration] window out afterwards -- the same
        20s-skip-intro/90s-window convention _decode_preview uses, just
        reached by slicing instead of seeking.
        """
        target_sr = 22050
        duration = 90.0
        offset = 20.0
        needed_seconds = offset + duration + 2.0  # small margin
        hard_timeout_ms = self.VIDEO_DECODE_HARD_TIMEOUT_MS
        poll_interval_ms = self.VIDEO_DECODE_POLL_INTERVAL_MS

        decoder = QtMultimedia.QAudioDecoder()
        loop = QtCore.QEventLoop()
        state = {"chunks": [], "native_sr": 0, "total_samples": 0}
        SampleFormat = QtMultimedia.QAudioFormat.SampleFormat

        def finish():
            # Every exit path (early-stop once enough samples are in,
            # natural finished/error signals, the hard timeout, and
            # cancellation via self._running) funnels through here:
            # stop the decoder first, then quit the loop -- never the
            # reverse, and never a bare loop.quit(). stop() is safe/
            # idempotent to call more than once or on an
            # already-stopped/errored decoder.
            decoder.stop()
            loop.quit()

        def on_buffer_ready():
            buf = decoder.read()
            if not buf.isValid():
                return
            fmt = buf.format()
            size = buf.byteCount()
            if size <= 0:
                return
            ptr = buf.constData()
            ptr.setsize(size)
            raw = bytes(ptr.asstring(size))
            sf_kind = fmt.sampleFormat()
            if sf_kind == SampleFormat.Float:
                arr = np.frombuffer(raw, dtype="<f4")
            elif sf_kind == SampleFormat.Int16:
                arr = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
            elif sf_kind == SampleFormat.Int32:
                arr = np.frombuffer(raw, dtype="<i4").astype("float32") / 2147483648.0
            elif sf_kind == SampleFormat.UInt8:
                arr = (np.frombuffer(raw, dtype="u1").astype("float32") - 128.0) / 128.0
            else:
                return
            channels = max(1, fmt.channelCount())
            if channels > 1:
                usable = (arr.size // channels) * channels
                arr = arr[:usable].reshape(-1, channels).mean(axis=1)
            state["chunks"].append(arr)
            state["native_sr"] = fmt.sampleRate()
            state["total_samples"] += arr.size
            if (
                state["native_sr"]
                and state["total_samples"] >= needed_seconds * state["native_sr"]
            ):
                finish()

        def on_finished():
            finish()

        def on_error(*_args):
            finish()

        decoder.bufferReady.connect(on_buffer_ready)
        decoder.finished.connect(on_finished)
        decoder.error.connect(on_error)

        cancel_check = QtCore.QTimer()
        cancel_check.setInterval(poll_interval_ms)

        def on_cancel_check():
            if not self._running:
                finish()
        cancel_check.timeout.connect(on_cancel_check)

        try:
            decoder.setSource(QtCore.QUrl.fromLocalFile(path))
            # decoder.start() can emit `error` (e.g. a container FFmpeg
            # can't open at all -- confirmed to happen for real files)
            # quickly enough that finish()'s loop.quit() lands *before*
            # loop.exec() below has actually started running -- a classic
            # Qt race where a quit() issued before exec() begins is
            # silently lost, since there's no running loop yet to mark
            # for exit. Deferring start() via singleShot(0, ...) guarantees
            # it only runs once loop.exec() is already processing events,
            # so any resulting error/finished signal's quit() always lands
            # on a genuinely running loop. Confirmed via direct
            # reproduction: without this, a real "Could not open file"
            # failure hung for the full hard_timeout_ms instead of
            # returning within milliseconds.
            QtCore.QTimer.singleShot(0, decoder.start)
            cancel_check.start()
            QtCore.QTimer.singleShot(hard_timeout_ms, finish)
            loop.exec()
        finally:
            cancel_check.stop()
            # Disconnect before touching local state further -- a signal
            # that manages to fire in the brief window between stop() and
            # the loop actually unwinding must never act on state we're
            # about to discard/return. No deleteLater(): run() uses a
            # custom mutex/condition loop, not QThread::exec(), so a
            # deferred deletion would never actually be processed on this
            # thread. `decoder` is a plain local variable with no
            # remaining external references once disconnected, so normal
            # reference-counted teardown (safe here -- only ever touched
            # from this one worker thread) is the correct and sufficient
            # mechanism, not left implicit.
            try:
                decoder.bufferReady.disconnect(on_buffer_ready)
            except Exception:
                pass
            try:
                decoder.finished.disconnect(on_finished)
            except Exception:
                pass
            try:
                decoder.error.disconnect(on_error)
            except Exception:
                pass

        if not state["chunks"] or not state["native_sr"]:
            return None, 0
        try:
            mono = np.concatenate(state["chunks"]).astype("float32")
            native_sr = int(state["native_sr"])
            start = int(offset * native_sr)
            if start >= mono.size:
                start = 0
            end = min(mono.size, start + int(duration * native_sr))
            window = mono[start:end] if end > start else mono
            if native_sr != target_sr and window.size:
                idx = np.linspace(0, window.size - 1, int(window.size * target_sr / native_sr))
                window = np.interp(idx, np.arange(window.size), window).astype("float32")
            return window, target_sr
        except Exception:
            return None, 0

    def _decode_native_audio_preview(self, path: str):
        """Frozen-build AUDIO fallback (v1.0.72) for when LIBROSA_AVAILABLE
        is False -- _decode_preview's own librosa/soundfile route is
        bypassed by _analyse_track's decoder-selection above in that case,
        so ordinary MP3/FLAC/etc. files need an alternative that, like
        video's, never touches librosa.

        Reuses _decode_video_audio_preview's QAudioDecoder machinery
        completely unchanged: nothing about that method is actually
        video-specific -- it decodes whatever local file path it is given
        via Qt's own FFmpeg-backed decoder, the same one confirmed (real
        tests/fixtures/sample.mp3 and sample.flac, see
        tests/test_frozen_audio_bpm_key_analysis.py) to decode ordinary
        compressed audio containers directly, with no librosa/soundfile
        involvement. Kept as its own, explicitly-named call site (see
        _analyse_track's decoder-selection block) rather than calling
        _decode_video_audio_preview directly from the audio branch, so
        v1.0.71's already-accepted video behaviour, diagnostics, and
        existing tests that monkeypatch _decode_video_audio_preview by
        name are completely undisturbed.
        """
        return self._decode_video_audio_preview(path)

    def _estimate_bpm(self, y, sr: int) -> Optional[float]:
        if LIBROSA_AVAILABLE:
            try:
                # Use the exact callable imported and compiled synchronously
                # by analysis_warmup. Resolving librosa.beat here would lazy-
                # import and JIT a different module on this worker thread.
                from . import analysis_warmup
                tempo = analysis_warmup.tempo(y=y, sr=sr, aggregate=None)
                if tempo is not None and len(tempo):
                    bpm = float(np.median(tempo))
                    while bpm < 80:
                        bpm *= 2.0
                    while bpm > 180:
                        bpm /= 2.0
                    return bpm
            except Exception:
                pass
        try:
            frame = 1024
            hop = 512
            if y.size < frame * 4:
                return None
            n = 1 + (y.size - frame) // hop
            energy = np.empty(n, dtype="float32")
            for i in range(n):
                chunk = y[i * hop:i * hop + frame]
                energy[i] = float(np.sqrt(np.mean(chunk * chunk)))
            novelty = np.maximum(0.0, np.diff(energy, prepend=energy[0]))
            novelty -= float(np.mean(novelty))
            if float(np.max(np.abs(novelty))) <= 1e-6:
                return None
            corr = np.correlate(novelty, novelty, mode="full")[len(novelty)-1:]
            rate = sr / float(hop)
            min_lag = max(1, int(rate * 60.0 / 180.0))
            max_lag = min(len(corr) - 1, int(rate * 60.0 / 80.0))
            if max_lag <= min_lag:
                return None
            lag = min_lag + int(np.argmax(corr[min_lag:max_lag + 1]))
            return 60.0 * rate / float(lag)
        except Exception:
            return None

    def _estimate_key(self, y, sr: int) -> str:
        try:
            if LIBROSA_AVAILABLE:
                chroma = librosa.feature.chroma_stft(y=y, sr=sr)
                profile = np.mean(chroma, axis=1)
            else:
                n_fft = 4096
                hop = 2048
                if y.size < n_fft:
                    return ""
                bins = np.fft.rfftfreq(n_fft, 1.0 / sr)
                pitch_classes = np.full(bins.shape, -1, dtype=int)
                valid = bins > 40.0
                pitch_classes[valid] = np.round(12 * np.log2(bins[valid] / 440.0) + 69).astype(int) % 12
                profile = np.zeros(12, dtype="float32")
                win = np.hanning(n_fft).astype("float32")
                for start in range(0, max(1, y.size - n_fft), hop):
                    mag = np.abs(np.fft.rfft(y[start:start+n_fft] * win))
                    for pc in range(12):
                        profile[pc] += float(np.sum(mag[pitch_classes == pc]))
            if float(np.max(profile)) <= 0:
                return ""
            profile = profile / float(np.sum(profile))
            major = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88], dtype="float32")
            minor = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17], dtype="float32")
            names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
            best_score = -1e9
            best_name = ""
            for root in range(12):
                maj_score = float(np.corrcoef(profile, np.roll(major, root))[0, 1])
                min_score = float(np.corrcoef(profile, np.roll(minor, root))[0, 1])
                if maj_score > best_score:
                    best_score = maj_score
                    best_name = names[root]
                if min_score > best_score:
                    best_score = min_score
                    best_name = names[root] + "m"
            return best_name
        except Exception:
            return ""


class QueueFolderDropWorker(QtCore.QThread):
    """Walks folders dropped onto Up Next off the UI thread and returns a
    flat, deterministically-ordered list of audio file paths."""
    completed = QtCore.pyqtSignal(list)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, folders: List[str], loose_files: List[str]):
        super().__init__()
        self.folders = folders
        self.loose_files = loose_files

    def run(self):
        try:
            result = list(self.loose_files)
            for folder in self.folders:
                for root, dirnames, filenames in os.walk(folder):
                    dirnames.sort(key=str.casefold)
                    for name in sorted(filenames, key=str.casefold):
                        if is_library_scannable(name):
                            result.append(os.path.join(root, name))
            self.completed.emit(result)
        except Exception as ex:
            self.failed.emit(str(ex))


class PlexPlaybackResolveWorker(QtCore.QThread):
    """Stage 3A: resolves ONE plex:// identity to a Direct Play transport
    source, entirely off the GUI thread -- server resolution, DNS, the
    Plex metadata fetch, and the Media/Part refresh must never freeze the
    window (pressing Play should read as "Resolving Plex media..." then
    either play or a clear, safe failure message, never a stall).

    Fetches the item's CURRENT Metadata fresh (never reuses whatever was
    cached during library browsing -- the file/transcode-eligibility may
    have changed since), decides Direct Play capability (Stage 3A is
    Direct Play only -- never starts a transcode session), and if
    playable, builds the transport URL/headers per Decision 1's approved
    split: AUDIO gets the token as a real HTTP header (BASS
    BASS_StreamCreateURL's own header mechanism); VIDEO gets the token
    in the transport URL's query string ONLY (the narrow, approved
    exception -- QMediaPlayer/QML MediaPlayer have no custom-header API
    for a QUrl source). Carries `generation` through untouched, same
    staleness-discard convention as every other Plex worker."""

    finished_result = QtCore.pyqtSignal(dict)

    def __init__(self, identity: str, server_config_id: str, rating_key: str,
                 media_kind: str, server_address: str, token: str,
                 client_identifier: str, generation: int):
        super().__init__()
        self._identity = identity
        self._server_config_id = server_config_id
        self._rating_key = rating_key
        self._media_kind = media_kind  # "music" | "video"
        self._server_address = server_address
        self._token = token
        self._client_identifier = client_identifier
        self._generation = generation
        self._diagnostics = get_diagnostics()

    def run(self):
        from . import analysis_warmup
        analysis_warmup.wait_until_ready()

        from .plex_client import PlexClient, PlexConnectionError
        from .plex_metadata import extract_media_info, resolve_direct_play_capability
        from .plex_transport import PlexTransportSource

        identity_hash = self._diagnostics.path_details(self._identity).get("path_hash", "")
        self._diagnostics.record(
            "plex", "plex_playback_resolve_started",
            details={"category": self._media_kind, "identity_hash": identity_hash},
            minimum_level="basic",
        )
        started = time.perf_counter()
        client = PlexClient(self._server_address, self._token, self._client_identifier)
        try:
            item = client.fetch_item_metadata(self._rating_key)
        except PlexConnectionError as ex:
            self._diagnostics.record(
                "plex", "plex_playback_resolve_failed",
                details={
                    "category": self._media_kind, "identity_hash": identity_hash,
                    "reason": ex.reason,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
                },
                minimum_level="basic",
            )
            self.finished_result.emit({
                "success": False, "reason": ex.reason, "identity": self._identity,
                "generation": self._generation,
            })
            return

        media_info = extract_media_info(item)
        capability = resolve_direct_play_capability(media_info, self._media_kind)
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
        if not capability["can_direct_play"]:
            self._diagnostics.record(
                "plex", "plex_playback_resolve_failed",
                details={
                    "category": self._media_kind, "identity_hash": identity_hash,
                    "reason": capability["reason"], "container": media_info.get("container", ""),
                    "video_codec": media_info.get("video_codec", ""),
                    "audio_codec": media_info.get("audio_codec", ""),
                    "elapsed_ms": elapsed_ms,
                },
                minimum_level="basic",
            )
            self.finished_result.emit({
                "success": False, "reason": capability["reason"], "identity": self._identity,
                "generation": self._generation, "direct_play_unavailable": True,
                "container": media_info.get("container", ""),
                "video_codec": media_info.get("video_codec", ""),
                "audio_codec": media_info.get("audio_codec", ""),
            })
            return

        base_url = client.part_stream_url(media_info["part_key"])
        if self._media_kind == "video":
            # Decision 1's approved exception: no custom-header API for a
            # QUrl source, so the token travels in the transport URL's
            # own query string -- and ONLY here, ONLY in this ephemeral,
            # never-logged-unsanitised value (see plex_transport.py).
            separator = "&" if "?" in base_url else "?"
            transport_url = f"{base_url}{separator}X-Plex-Token={self._token}"
            extra_headers = None
        else:
            # AUDIO (BASS): the token is a real HTTP header, never part
            # of the URL text at all.
            transport_url = base_url
            extra_headers = {"X-Plex-Token": self._token}

        source = PlexTransportSource(
            identity=self._identity, transport_url=transport_url, extra_headers=extra_headers,
            container=media_info.get("container", ""),
            video_codec=media_info.get("video_codec", ""),
            audio_codec=media_info.get("audio_codec", ""),
        )
        self._diagnostics.record(
            "plex", "plex_playback_resolve_completed",
            details={
                "category": self._media_kind, "identity_hash": identity_hash,
                "container": source.container, "elapsed_ms": elapsed_ms,
            },
            minimum_level="basic",
        )
        self.finished_result.emit({
            "success": True, "identity": self._identity, "generation": self._generation,
            "transport_source": source,
        })


class BassStreamPrepareWorker(QtCore.QThread):
    """Phase C1 (native audio backend ownership, 2026-09-10): prepares a
    candidate BASS stream off the GUI thread WITHOUT ever touching a
    live BassPlayer instance -- replaces the old PlexAudioLoadWorker
    (Plex active-slot load) and the BASS branch of the old
    PlayerLoadWorker (Local crossfade / Video->Audio preload onto the
    inactive slot). `source` is either a local path (str) or a Stage 3A
    PlexTransportSource, exactly like BassPlayer.prepare_stream()'s own
    argument.

    This worker holds NO reference to any BassPlayer instance -- only
    `source`/`token` and the PreparedBassStream it produces -- so a stale
    or superseded worker's completed preparation has nothing shared to
    mutate. The GUI thread alone (via PlayerWindow's attempt/token/
    transition + target-lease checks) decides whether to commit or
    discard the candidate this produces; a rejected candidate's discard()
    frees only its own HSTREAM, never touching any BassPlayer. See
    bass_player.PreparedBassStream for the full ownership design.

    Phase C2 (worker lifetime / shutdown ownership, 2026-09-11): the
    worker itself retains a strong reference to the candidate it created
    (_prepared_candidate) until claim_candidate() is called -- exactly
    once, by whichever caller gets there first: the GUI thread's normal
    `prepared`-signal callback, or PlayerWindow's shutdown-time finalizer
    (see _finalize_unclaimed_prepare_candidate / worker_registry.py's
    finalize_after_join). This makes the candidate's fate deterministic
    even if the GUI thread never returns to the event loop to process the
    queued `prepared` signal at all (e.g. genuinely blocked inside a
    synchronous shutdown wait) -- shutdown can synchronously reach into
    THIS object, once its run() has positively finished, and resolve
    whatever it's still holding. After assigning _prepared_candidate,
    run() never touches it again."""
    prepared = QtCore.pyqtSignal(int, str, object)
    failed = QtCore.pyqtSignal(int, str, str)

    def __init__(self, source, token: int):
        super().__init__()
        self.source = source
        self.token = token
        self._prepared_candidate = None

    def run(self):
        from .bass_player import BassPlayer
        from .plex_transport import PlexTransportSource, sanitize_plex_text
        is_plex = isinstance(self.source, PlexTransportSource)
        identity = self.source.identity if is_plex else self.source
        try:
            candidate = BassPlayer.prepare_stream(self.source)
            self._prepared_candidate = candidate
            self.prepared.emit(self.token, identity, candidate)
        except Exception as ex:
            message = sanitize_plex_text(str(ex)) if is_plex else str(ex)
            self.failed.emit(self.token, identity, message)

    def claim_candidate(self):
        """Exactly-once: returns the PreparedBassStream on the first call
        (from whichever caller -- normal GUI callback or shutdown
        finalizer -- gets here first), None on every call after. Safe to
        call even if run() never produced a candidate at all (failed
        path, or never started) -- simply returns None."""
        candidate = self._prepared_candidate
        self._prepared_candidate = None
        return candidate


class MiniaudioSourcePrepareWorker(QtCore.QThread):
    """Phase C1 counterpart to BassStreamPrepareWorker for the
    miniaudio backend -- replaces the miniaudio branch of the old
    PlayerLoadWorker. Gathers only immutable source metadata off the
    GUI thread via MiniaudioPlayer.prepare_source() (i.e.
    miniaudio.get_file_info() -- no device/stream is constructed here);
    holds no reference to any MiniaudioPlayer instance. The token
    convention matches BassStreamPrepareWorker exactly, so the same
    caller-side staleness/lease-checking pattern applies to both.

    Phase C2: carries the identical _prepared_candidate/claim_candidate()
    seam as BassStreamPrepareWorker, purely for API consistency in
    PlayerWindow's dispatch/finalizer code -- PreparedMiniaudioSource has
    no native resource to leak, so this doesn't close a real hazard here
    the way it does on the BASS side, but it means window.py never has to
    special-case which backend a worker belongs to when claiming/
    finalizing its result."""
    prepared = QtCore.pyqtSignal(int, str, object)
    failed = QtCore.pyqtSignal(int, str, str)

    def __init__(self, path: str, token: int):
        super().__init__()
        self.path = path
        self.token = token
        self._prepared_candidate = None

    def run(self):
        from .miniaudio_player import MiniaudioPlayer
        try:
            candidate = MiniaudioPlayer.prepare_source(self.path)
            self._prepared_candidate = candidate
            self.prepared.emit(self.token, self.path, candidate)
        except Exception as ex:
            self.failed.emit(self.token, self.path, str(ex))

    def claim_candidate(self):
        candidate = self._prepared_candidate
        self._prepared_candidate = None
        return candidate
