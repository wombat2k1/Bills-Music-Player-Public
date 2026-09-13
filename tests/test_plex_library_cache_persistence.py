"""Stage 3A real-device bug (item 2): the Plex Music/Video/Karaoke
libraries were fetched fresh from the Plex server on every single launch
-- unnecessarily expensive startup/network use, and a tab showed nothing
useful until that fetch completed, no matter how good the previous
session's data still was.

Fix: _save_plex_library_cache() persists the same Stage-2 meta dicts
already held in memory after every successful fetch (a plex_library_cache.json
file, separate from the Local library's own cache) along with
last_successful_refresh_utc and the library_ids_by_kind the cache was
fetched from. _restore_plex_library_cache_at_startup() loads it back
before any Plex network activity, so a tab has real data immediately.

Freshness policy (review gate 3): a cache younger than
PLAYERWINDOW.PLEX_LIBRARY_CACHE_FRESHNESS_SECONDS (24h default) is used
with NO automatic background refetch at all -- the real fix for "the
whole library downloads again every single launch". A stale cache is
still shown immediately, with exactly one background refresh kicked off
once Plex connection resolution actually succeeds
(_maybe_start_stale_plex_startup_refresh, called from both the account-
mode and manual-mode paths of _start_plex_startup_validation). "Refresh
Plex" (_refresh_plex_library) is completely unconditional and untouched.
A server change or a changed library mapping discards that cache/kind
entirely rather than risk showing or refreshing the wrong library."""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets

from billsmusic.plex_preferences import PlexPreferences
from billsmusic.window import PlayerWindow

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


class _Diagnostics:
    def __init__(self):
        self.events = []

    def record(self, category, op, **kw):
        self.events.append((category, op, kw))


class _VisibilityTrackingWidget:
    def __init__(self):
        self.visible = None  # None means never explicitly set

    def setVisible(self, value):
        self.visible = bool(value)


class CacheHarness:
    _save_plex_library_cache = PlayerWindow._save_plex_library_cache
    _load_plex_library_cache = PlayerWindow._load_plex_library_cache
    _restore_plex_library_cache_at_startup = PlayerWindow._restore_plex_library_cache_at_startup
    _maybe_start_stale_plex_startup_refresh = PlayerWindow._maybe_start_stale_plex_startup_refresh
    _maybe_rehydrate_plex_queue_after_cache_restore = (
        PlayerWindow._maybe_rehydrate_plex_queue_after_cache_restore
    )
    _PLEX_LIBRARY_CACHE_SCHEMA_VERSION = PlayerWindow._PLEX_LIBRARY_CACHE_SCHEMA_VERSION
    PLEX_LIBRARY_CACHE_FRESHNESS_SECONDS = PlayerWindow.PLEX_LIBRARY_CACHE_FRESHNESS_SECONDS

    def __init__(self, server_config_id="server-1", library_source="local", music_library_id="2"):
        _app()
        self.plex_preferences = PlexPreferences(
            server_config_id=server_config_id, music_library_id=music_library_id,
        )
        self._plex_meta_by_kind = {"music": [], "video": [], "karaoke": []}
        self._plex_meta_by_path = {}
        self._plex_item_updated_at = {}
        self._plex_fetched_kinds = set()
        self.library_source = library_source
        self.diagnostics = _Diagnostics()
        self.apply_calls = []
        self._closing = False
        self.prioritized_fetch_calls = []
        self.queue = []
        self.refresh_queue_list_calls = []
        self.library_tabs = _VisibilityTrackingWidget()
        self.library_plex_placeholder = _VisibilityTrackingWidget()

    def _apply_meta_list_to_library_tabs(self, meta_list, reason, trigger_source):
        self.apply_calls.append((list(meta_list), reason, trigger_source))

    def _refresh_queue_list(self, cached_details_only=False, reason="structural_change"):
        self.refresh_queue_list_calls.append((cached_details_only, reason))

    def _start_plex_library_fetches_prioritized(self, kinds):
        self.prioritized_fetch_calls.append(list(kinds))


_MUSIC_ITEM = {
    "path": "plex://server-1/42.flac", "title": "9 PM (Till I Come)",
    "artist": "ATB", "album": "Movin' Melodies", "disc_no": 1, "track_no": 3,
    "duration_ms": 210000, "rating_key": "42", "updated_at": 555, "thumb": "/thumb/42",
}


def test_save_then_load_round_trips_the_same_meta(tmp_path, monkeypatch):
    cache_file = tmp_path / "plex_library_cache.json"
    monkeypatch.setattr("billsmusic.window.plex_library_cache_path", lambda: str(cache_file))

    harness = CacheHarness()
    harness._plex_meta_by_kind["music"] = [_MUSIC_ITEM]
    harness._save_plex_library_cache()

    assert cache_file.exists()
    loaded = harness._load_plex_library_cache()
    assert loaded["server_config_id"] == "server-1"
    assert loaded["meta_by_kind"]["music"] == [_MUSIC_ITEM]


def test_persisted_cache_never_contains_a_transport_url_or_token(tmp_path, monkeypatch):
    cache_file = tmp_path / "plex_library_cache.json"
    monkeypatch.setattr("billsmusic.window.plex_library_cache_path", lambda: str(cache_file))

    harness = CacheHarness()
    harness._plex_meta_by_kind["music"] = [_MUSIC_ITEM]
    harness._save_plex_library_cache()

    raw = cache_file.read_text(encoding="utf-8")
    assert "X-Plex-Token" not in raw
    assert "transport_url" not in raw
    assert "http://" not in raw and "https://" not in raw


def test_restore_at_startup_populates_in_memory_caches_without_network(tmp_path, monkeypatch):
    cache_file = tmp_path / "plex_library_cache.json"
    monkeypatch.setattr("billsmusic.window.plex_library_cache_path", lambda: str(cache_file))
    writer = CacheHarness()
    writer._plex_meta_by_kind["music"] = [_MUSIC_ITEM]
    writer._save_plex_library_cache()

    reader = CacheHarness(library_source="local")  # browsing Local, not Plex
    reader._restore_plex_library_cache_at_startup()

    assert reader._plex_meta_by_kind["music"] == [_MUSIC_ITEM]
    assert reader._plex_meta_by_path["plex://server-1/42.flac"] == _MUSIC_ITEM
    assert reader._plex_item_updated_at["plex://server-1/42.flac"] == 555
    assert "music" in reader._plex_fetched_kinds
    # Browsing Local at startup -- the cache is available in memory (so a
    # later tab switch to Plex shows it immediately) but nothing was
    # applied to the tree that isn't even visible right now.
    assert reader.apply_calls == []


def test_restore_applies_to_tabs_immediately_when_plex_was_last_viewed(tmp_path, monkeypatch):
    cache_file = tmp_path / "plex_library_cache.json"
    monkeypatch.setattr("billsmusic.window.plex_library_cache_path", lambda: str(cache_file))
    writer = CacheHarness()
    writer._plex_meta_by_kind["music"] = [_MUSIC_ITEM]
    writer._save_plex_library_cache()

    reader = CacheHarness(library_source="plex")
    reader._restore_plex_library_cache_at_startup()

    assert len(reader.apply_calls) == 1
    meta_list, reason, trigger_source = reader.apply_calls[0]
    assert meta_list == [_MUSIC_ITEM]
    assert trigger_source == "plex_fetch"


def test_cache_from_a_different_server_is_discarded_not_applied(tmp_path, monkeypatch):
    cache_file = tmp_path / "plex_library_cache.json"
    monkeypatch.setattr("billsmusic.window.plex_library_cache_path", lambda: str(cache_file))
    writer = CacheHarness(server_config_id="old-server")
    writer._plex_meta_by_kind["music"] = [_MUSIC_ITEM]
    writer._save_plex_library_cache()

    reader = CacheHarness(server_config_id="new-server", library_source="plex")
    reader._restore_plex_library_cache_at_startup()

    assert reader._plex_meta_by_kind["music"] == []
    assert reader.apply_calls == []


def test_missing_cache_file_is_a_safe_noop(tmp_path, monkeypatch):
    cache_file = tmp_path / "does-not-exist.json"
    monkeypatch.setattr("billsmusic.window.plex_library_cache_path", lambda: str(cache_file))

    harness = CacheHarness(library_source="plex")
    harness._restore_plex_library_cache_at_startup()  # must not raise

    assert harness._plex_meta_by_kind["music"] == []
    assert harness.apply_calls == []


def test_corrupt_cache_file_is_a_safe_noop(tmp_path, monkeypatch):
    cache_file = tmp_path / "plex_library_cache.json"
    cache_file.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr("billsmusic.window.plex_library_cache_path", lambda: str(cache_file))

    harness = CacheHarness(library_source="plex")
    harness._restore_plex_library_cache_at_startup()  # must not raise

    assert harness._plex_meta_by_kind["music"] == []


# -- freshness policy (review gate 3) ----------------------------------------

def _write_cache_with_age(tmp_path, monkeypatch, age_seconds, music_library_id="2"):
    import time as time_module
    cache_file = tmp_path / "plex_library_cache.json"
    monkeypatch.setattr("billsmusic.window.plex_library_cache_path", lambda: str(cache_file))
    cache_file.write_text(json.dumps({
        "schema_version": 1, "server_config_id": "server-1",
        "last_successful_refresh_utc": time_module.time() - age_seconds,
        "library_ids_by_kind": {"music": music_library_id, "video": "", "karaoke": ""},
        "meta_by_kind": {"music": [_MUSIC_ITEM], "video": [], "karaoke": []},
    }), encoding="utf-8")
    return cache_file


def test_fresh_cache_marks_kind_fetched_and_schedules_no_background_refresh(tmp_path, monkeypatch):
    _write_cache_with_age(tmp_path, monkeypatch, age_seconds=60)  # 1 minute old

    harness = CacheHarness(library_source="plex")
    harness._restore_plex_library_cache_at_startup()
    harness._maybe_start_stale_plex_startup_refresh()

    assert harness._plex_meta_by_kind["music"] == [_MUSIC_ITEM]
    assert "music" in harness._plex_fetched_kinds
    # This is the actual fix for "the whole library refetches every
    # launch": a fresh cache schedules zero HTTP-issuing fetches.
    assert harness.prioritized_fetch_calls == []


def test_stale_cache_displays_immediately_then_schedules_one_background_refresh(tmp_path, monkeypatch):
    _write_cache_with_age(
        tmp_path, monkeypatch,
        age_seconds=CacheHarness.PLEX_LIBRARY_CACHE_FRESHNESS_SECONDS + 3600,
    )

    harness = CacheHarness(library_source="plex")
    harness._restore_plex_library_cache_at_startup()
    # Cached data is already displayed immediately -- before any network
    # activity, i.e. before connection resolution would ever call the
    # refresh trigger below.
    assert harness._plex_meta_by_kind["music"] == [_MUSIC_ITEM]
    assert len(harness.apply_calls) == 1

    # Only once connection resolution actually succeeds (simulated here)
    # does the one background refresh for the stale kind get scheduled.
    harness._maybe_start_stale_plex_startup_refresh()
    assert harness.prioritized_fetch_calls == [["music"]]

    # A second call (e.g. a redundant connection-result signal) must not
    # schedule a second refresh for the same startup.
    harness._maybe_start_stale_plex_startup_refresh()
    assert harness.prioritized_fetch_calls == [["music"]]


def test_offline_startup_leaves_cached_data_in_place_no_refresh_attempted(tmp_path, monkeypatch):
    _write_cache_with_age(
        tmp_path, monkeypatch,
        age_seconds=CacheHarness.PLEX_LIBRARY_CACHE_FRESHNESS_SECONDS + 3600,
    )

    harness = CacheHarness(library_source="plex")
    harness._restore_plex_library_cache_at_startup()

    # Plex never came online this session -- _maybe_start_stale_plex_
    # startup_refresh is only ever called from a successful connection
    # result, so it's simply never invoked. Cached data must still be
    # exactly what's shown/held.
    assert harness._plex_meta_by_kind["music"] == [_MUSIC_ITEM]
    assert harness.prioritized_fetch_calls == []


def test_changed_library_mapping_discards_that_kind_forcing_immediate_refetch(tmp_path, monkeypatch):
    # Cached under music_library_id="2"; the user has since remapped
    # Music to a different Plex library section ("9").
    _write_cache_with_age(tmp_path, monkeypatch, age_seconds=60, music_library_id="2")

    harness = CacheHarness(library_source="plex", music_library_id="9")
    harness._restore_plex_library_cache_at_startup()

    # Treated as never-fetched, not as fresh-and-suppressed -- the
    # ordinary _show_plex_library_view "still pending" path (kind not in
    # _plex_fetched_kinds) will fetch it as soon as it's viewed, exactly
    # like a first-ever fetch.
    assert harness._plex_meta_by_kind["music"] == []
    assert "music" not in harness._plex_fetched_kinds
    assert harness.apply_calls == []


def test_explicit_refresh_ignores_freshness_entirely():
    # _refresh_plex_library (the real "Refresh Plex" handler) calls
    # _start_plex_library_fetch directly for every mapped kind,
    # completely independent of _plex_fetched_kinds/staleness -- confirmed
    # by reading its own source rather than re-implementing it here (it
    # has its own dedicated coverage in test_plex_library_browsing.py).
    # This test documents the contract this freshness feature must not
    # break: _refresh_plex_library never consults _plex_startup_stale_
    # kinds or any freshness timestamp at all.
    import inspect
    source = inspect.getsource(PlayerWindow._refresh_plex_library)
    assert "_plex_startup_stale_kinds" not in source
    assert "freshness" not in source.lower()
    assert "_start_plex_library_fetch(kind)" in source


# -- Stage 3A-r2 real-device defect: Up Next queue rows lose Plex titles
# after a restart -----------------------------------------------------------
#
# session.json only ever persists {path, played} (save_session_file), so a
# restarted Up Next row can only ever regain its real title/artist by
# hydrating from live metadata, not from anything in the session file
# itself. _load_session (an earlier startup stage) already rebuilds the
# queue list once with whatever's known at that point -- for a Plex
# identity, that's nothing yet, since _restore_plex_library_cache_at_startup
# (which populates _plex_meta_by_path) hasn't run yet. This proves the
# re-hydration trigger added right after it.

# -- Stage 3A-r2 real-device defect: cache restores but stays invisible
# behind the placeholder -------------------------------------------------
#
# _load_user_settings's own combo-sync (self.library_source_selector's
# initial index, window.py ~4460) runs long before any Plex data exists --
# it blindly hides library_tabs / shows library_plex_placeholder for
# library_source == "plex" with no idea a fresh cache is about to be
# restored. Nothing re-asserted visibility once _restore_plex_library_cache_
# at_startup actually populated the tree, so the data existed but stayed
# hidden behind the placeholder until the user manually touched the source
# selector (the only other code path that gets this right, via
# _show_plex_library_view). These tests drive the real, unbound
# _restore_plex_library_cache_at_startup directly against real widget
# stubs to prove visibility is corrected without that round-trip.

def test_cache_restore_makes_library_tabs_visible_without_a_source_toggle(tmp_path, monkeypatch):
    _write_cache_with_age(tmp_path, monkeypatch, age_seconds=60)

    harness = CacheHarness(library_source="plex")
    # Simulates the real startup order: the early combo-sync already ran
    # and (per the real bug) left the placeholder showing.
    harness.library_tabs.setVisible(False)
    harness.library_plex_placeholder.setVisible(True)

    harness._restore_plex_library_cache_at_startup()

    assert harness.library_tabs.visible is True
    assert harness.library_plex_placeholder.visible is False


def test_no_cached_data_leaves_visibility_untouched(tmp_path, monkeypatch):
    # Control: when there's genuinely nothing to restore (no cache file at
    # all), this must not blindly force tabs visible over real "not
    # configured"/"never fetched" state -- only a successful restore
    # should correct the placeholder.
    monkeypatch.setattr(
        "billsmusic.window.plex_library_cache_path",
        lambda: str(tmp_path / "does_not_exist.json"),
    )
    harness = CacheHarness(library_source="plex")
    harness.library_tabs.setVisible(False)
    harness.library_plex_placeholder.setVisible(True)

    harness._restore_plex_library_cache_at_startup()

    assert harness.library_tabs.visible is False
    assert harness.library_plex_placeholder.visible is True


def test_rehydrates_queue_when_a_plex_identity_is_present(tmp_path, monkeypatch):
    _write_cache_with_age(tmp_path, monkeypatch, age_seconds=60)

    harness = CacheHarness(library_source="plex")
    harness.queue = [
        "plex://server-1/107177.mkv",
        "plex://server-1/42.flac",
    ]
    harness._restore_plex_library_cache_at_startup()
    harness._maybe_rehydrate_plex_queue_after_cache_restore()

    assert harness.refresh_queue_list_calls == [(True, "plex_cache_restored")]


def test_no_rehydration_when_queue_has_no_plex_identity(tmp_path, monkeypatch):
    # Control: a Local-only (or empty) queue has nothing to gain from a
    # second rebuild -- this must not fire unconditionally on every
    # startup, only when it can actually fix something.
    _write_cache_with_age(tmp_path, monkeypatch, age_seconds=60)

    harness = CacheHarness(library_source="plex")
    harness.queue = ["Y:\\Music\\Artist\\Album\\track.mp3"]
    harness._restore_plex_library_cache_at_startup()
    harness._maybe_rehydrate_plex_queue_after_cache_restore()

    assert harness.refresh_queue_list_calls == []


def test_no_rehydration_for_an_empty_queue(tmp_path, monkeypatch):
    _write_cache_with_age(tmp_path, monkeypatch, age_seconds=60)

    harness = CacheHarness(library_source="plex")
    harness._restore_plex_library_cache_at_startup()
    harness._maybe_rehydrate_plex_queue_after_cache_restore()

    assert harness.refresh_queue_list_calls == []
