"""Plex Stage 1: Preferences dialog persistence, token masking, library
mapping resilience, restart round-trip, and the Local/Plex source
selector. Real PlayerWindow, real Qt event loop, no mocks for the widget
layer -- only _show_normalisation_preferences's own dialog.exec() is
replaced with an immediate-accept/-reject shim so these tests don't block
on a real modal loop.

Follows this suite's two established conventions side by side:
inspect.getsource static checks for the parts of _load_user_settings/
_save_user_settings that are impractical to exercise live (see
test_video_preferences.py's own docstring), and real end-to-end
PlayerWindow round-trips for everything that is a genuine behavioural
claim (masking, restart persistence, graceful missing-library handling).

Real-PlayerWindow tests are deliberately consolidated into few, larger
test functions rather than one real window per assertion: this codebase's
own established pattern (see CODEX_HANDOFF.md's repeated "pre-existing
native-exit flakiness" notes, and test_karaoke_diagnostics.py needing
isolation) is that many real PlayerWindow instances accumulated in one
pytest process increase the odds of a Windows access-violation from
lingering background worker threads racing native Qt teardown -- proven
empirically here too (this file crashed reliably once split across ~13
single-purpose windows; 5 consolidated windows is reliable). Each real
window still gets its own dedicated test for restart specifically, since
that one genuinely needs two windows.
"""
import inspect
import json
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets

from billsmusic import analysis_warmup
from billsmusic.config import config_file_path
from billsmusic.plex_preferences import PlexPreferences
from billsmusic.window import PlayerWindow

# Mirrors Main.py's own startup ordering: run the one-time librosa/scipy/
# mutagen/pychromecast import warm-up on this (the only) thread *before*
# any PlayerWindow -- and therefore any of its worker threads -- exists.
# analysis_warmup.py's own module docstring documents a confirmed class of
# Windows access violations when one of those first-ever lazy imports
# happens on a background thread concurrently with other native-threaded
# work; window.py:1232-1243 separately documents that a test constructing
# PlayerWindow() directly (as this file's _build_window and the two bare
# `PlayerWindow()` calls below do) never goes through Main.py's own
# run_synchronously() call and is therefore exposed to exactly that race.
# run_synchronously() is idempotent (billsmusic/analysis_warmup.py), so
# this single module-level call is enough to cover every window built in
# this file.
analysis_warmup.run_synchronously()

_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _pump(seconds=0.3):
    app = _app()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


def _build_window(tmp_path, monkeypatch, tag=""):
    localappdata = tmp_path / f"appdata{tag}"
    localappdata.mkdir(exist_ok=True)
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    _app()
    window = PlayerWindow()
    window.resize(1000, 800)
    window.show()
    _pump(0.2)
    return window


def _wait_for_music_tab_settled(window, timeout=2.0):
    """Pre-existing (unrelated to Plex), startup-time library tab
    population chain: _apply_meta_list_to_library_tabs populates Music ->
    Video -> Karaoke sequentially, each step transiently activating that
    tab's state, before finally restoring whichever tab was actually
    selected (Music, by default, since no tab was clicked) via its own
    _restore_visual_selection on_finished callback. Confirmed empirically
    (via a standalone repro) that this settles in roughly a second but is
    not instantaneous -- a short pump can observe it mid-flight
    (transiently Video or Karaoke). Only the one test that asserts on
    _active_library_tab needs to wait for this; _build_window's default
    pump stays short since most tests don't care."""
    app = _app()
    deadline = time.monotonic() + timeout
    while (
        getattr(window, "_active_library_tab", None) is not getattr(window, "_music_tab", object())
        and time.monotonic() < deadline
    ):
        app.processEvents()
        time.sleep(0.01)


def _close_window(window):
    """Plain close() + a settle pump. This suite's real-PlayerWindow tests
    are subject to this project's already-documented pre-existing native-
    exit flakiness class (real background worker threads racing native Qt
    teardown -- see CODEX_HANDOFF.md's repeated notes on this, and
    test_karaoke_diagnostics.py needing the same isolation). Explicitly
    calling _worker_registry.shutdown_all() first was tried and measured
    worse (it blocks the calling thread on bounded thread joins without
    pumping the event loop those same threads may need to finish
    cleanly) -- removed after the experiment, not carried forward."""
    window.close()
    _pump(0.3)


def _open_preferences_and_run(window, mutate):
    """Opens the real Preferences dialog, lets `mutate(dialog)` edit
    widgets, then accepts. dialog.exec() is replaced with a version that
    runs `mutate` on a short timer before calling accept(), matching the
    approach already needed to drive a real modal QDialog under a
    headless test without actually blocking."""
    from PyQt6.QtWidgets import QDialog

    orig_exec = QDialog.exec

    def _fake_exec(self):
        def _apply():
            mutate(self)
            self.accept()
        QtCore.QTimer.singleShot(20, _apply)
        return orig_exec(self)

    # Scoped via MonkeyPatch rather than `QDialog.exec = orig_exec` in a
    # finally: reading QDialog.exec off the class yields a plain builtin
    # function, not sip's binding method descriptor, so writing that back
    # permanently broke `dialog.exec()` for every later test in the same
    # process ("exec(self): first argument of unbound method must have type
    # 'QDialog'" -- e.g. all of test_queue_dedup_dialogs_qtest.py whenever it
    # shared an xdist worker with this file). MonkeyPatch restores the
    # original class __dict__ entry itself.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(QDialog, "exec", _fake_exec)
        window._show_normalisation_preferences()
    _pump(0.1)


def _find(dialog, object_name):
    widget = dialog.findChild(QtWidgets.QWidget, object_name)
    assert widget is not None, f"widget {object_name!r} not found in Preferences dialog"
    return widget


def test_open_preferences_helper_leaves_real_dialog_exec_working_afterwards():
    """Regression: this helper's own QDialog.exec patch used to be undone
    with `QDialog.exec = orig_exec`, which silently broke dialog.exec() for
    the rest of the process. Uses a stand-in window whose preferences
    method just runs a plain QDialog, so no real PlayerWindow is needed."""
    from PyQt6.QtWidgets import QDialog

    _app()
    mutated = []

    class _Window:
        def _show_normalisation_preferences(self):
            return QDialog().exec()

    _open_preferences_and_run(_Window(), mutated.append)
    assert len(mutated) == 1

    later_dialog = QDialog()
    QtCore.QTimer.singleShot(20, later_dialog.accept)
    assert later_dialog.exec() == QDialog.DialogCode.Accepted


# -- Static checks: the wiring survives even where live widgets aren't ------
# -- practical to invoke for every combination (mirrors this suite's own ---
# -- existing convention in test_video_preferences.py). ---------------------

def test_load_user_settings_reconstructs_plex_preferences_from_config():
    source = inspect.getsource(PlayerWindow._load_user_settings)
    assert "PlexPreferences.from_config(cfg)" in source
    assert 'cfg.get("library_source", "local")' in source


def test_save_user_settings_persists_plex_preferences_and_library_source():
    source = inspect.getsource(PlayerWindow._save_user_settings)
    assert "to_config_updates()" in source
    assert 'cfg["library_source"]' in source


def test_client_identifier_generated_once_and_persisted_immediately():
    source = inspect.getsource(PlayerWindow._load_user_settings)
    assert "generate_client_identifier()" in source
    assert '"plex_client_identifier"' in source


# -- Real behavioural round-trips (consolidated: one window per group) ------

def test_save_reload_and_reopen_round_trip(tmp_path, monkeypatch):
    window = _build_window(tmp_path, monkeypatch, tag="_roundtrip")
    try:
        # 1) Save via the real dialog.
        def mutate(dialog):
            _find(dialog, "plexEnabledCheckbox").setChecked(True)
            _find(dialog, "plexUseManualServerCheckbox").setChecked(True)
            _find(dialog, "plexServerAddressField").setText("192.168.1.50:32400")
            _find(dialog, "plexTokenField").setText("test-token-value")
        _open_preferences_and_run(window, mutate)

        assert window.plex_preferences.enabled is True
        assert window.plex_preferences.use_manual_server is True
        assert window.plex_preferences.server_address == "192.168.1.50:32400"
        assert window.plex_preferences.token == "test-token-value"
        assert window.plex_preferences.server_config_id  # freshly generated

        with open(config_file_path(), "r", encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["plex_enabled"] is True
        assert saved["plex_server_address"] == "192.168.1.50:32400"
        assert saved["plex_token"] == "test-token-value"
        assert saved["plex_server_config_id"]

        # 2) Reload straight from the config dict (independent of the live
        # window object) reconstructs an equal PlexPreferences.
        restored = PlexPreferences.from_config(saved)
        assert restored.enabled is True
        assert restored.server_address == "192.168.1.50:32400"
        assert restored.token == "test-token-value"
        assert restored.server_config_id == window.plex_preferences.server_config_id

        # 3) Reopening Preferences shows the saved values, and the token
        # stays masked (Password echo mode) until explicitly revealed.
        seen = {}

        def read(dialog):
            seen["enabled"] = _find(dialog, "plexEnabledCheckbox").isChecked()
            seen["address"] = _find(dialog, "plexServerAddressField").text()
            token_field = _find(dialog, "plexTokenField")
            seen["token"] = token_field.text()
            seen["echo_mode"] = token_field.echoMode()
            show_token = _find(dialog, "plexShowTokenCheckbox")
            show_token.setChecked(True)
            seen["echo_mode_after_show"] = token_field.echoMode()
            show_token.setChecked(False)
            seen["echo_mode_after_hide"] = token_field.echoMode()
        _open_preferences_and_run(window, read)

        assert seen["enabled"] is True
        assert seen["address"] == "192.168.1.50:32400"
        assert seen["token"] == "test-token-value"
        assert seen["echo_mode"] == QtWidgets.QLineEdit.EchoMode.Password
        assert seen["echo_mode_after_show"] == QtWidgets.QLineEdit.EchoMode.Normal
        assert seen["echo_mode_after_hide"] == QtWidgets.QLineEdit.EchoMode.Password
    finally:
        _close_window(window)


def test_library_mapping_persistence_and_missing_library_handling(tmp_path, monkeypatch):
    window = _build_window(tmp_path, monkeypatch, tag="_libmap")
    try:
        window.plex_preferences = window.plex_preferences.__class__(
            enabled=True, server_address="http://host:32400", token="tok",
            client_identifier=window.plex_preferences.client_identifier,
            server_config_id="cfg-1",
            music_library_id="7", music_library_name="My Music",
            video_library_id="9", video_library_name="My Movies",
        )
        window._save_user_settings()

        # Library mappings are restored by stable id, display name shown
        # alongside for the user's benefit.
        seen = {}

        def read_mappings(dialog):
            music_combo = _find(dialog, "plexMusicLibraryCombo")
            video_combo = _find(dialog, "plexVideoLibraryCombo")
            seen["music_id"] = music_combo.currentData()
            seen["music_text"] = music_combo.currentText()
            seen["video_id"] = video_combo.currentData()
        _open_preferences_and_run(window, read_mappings)

        assert seen["music_id"] == "7"
        assert "My Music" in seen["music_text"]
        assert seen["video_id"] == "9"

        # A saved-but-not-yet-re-tested library id must still show, never
        # silently dropped just because this dialog session hasn't run a
        # fresh Test Connection.
        window.plex_preferences = window.plex_preferences.__class__(
            enabled=True, server_address="http://host:32400", token="tok",
            client_identifier=window.plex_preferences.client_identifier,
            server_config_id="cfg-1",
            music_library_id="99", music_library_name="Old Removed Library",
        )
        window._save_user_settings()

        seen2 = {}

        def read_unconfirmed(dialog):
            music_combo = _find(dialog, "plexMusicLibraryCombo")
            seen2["music_id"] = music_combo.currentData()
            seen2["music_text"] = music_combo.currentText()
        _open_preferences_and_run(window, read_unconfirmed)

        assert seen2["music_id"] == "99"
        assert "Old Removed Library" in seen2["music_text"]

        # A real Test Connection whose discovered list no longer contains
        # the saved section id must retain it, shown as unavailable.
        def fake_get(url, headers=None, timeout=None):
            from unittest.mock import MagicMock
            response = MagicMock()
            response.status_code = 200
            if url.endswith("/library/sections"):
                response.json.return_value = {
                    "MediaContainer": {"Directory": [
                        {"key": "1", "title": "Music", "type": "artist"},
                        {"key": "2", "title": "Movies", "type": "movie"},
                    ]}
                }
            else:
                response.json.return_value = {
                    "MediaContainer": {
                        "friendlyName": "Test Server", "version": "1.0",
                        "machineIdentifier": "xyz",
                    }
                }
            return response
        monkeypatch.setattr("billsmusic.plex_client.requests.get", fake_get)

        seen3 = {}

        def click_test_and_read(dialog):
            button = _find(dialog, "plexTestConnectionButton")
            music_combo = _find(dialog, "plexMusicLibraryCombo")
            button.click()
            deadline = time.monotonic() + 5.0
            while music_combo.count() < 3 and time.monotonic() < deadline:
                _app().processEvents()
                time.sleep(0.01)
            seen3["music_id"] = music_combo.currentData()
            seen3["music_text"] = music_combo.currentText()
            seen3["count"] = music_combo.count()
        _open_preferences_and_run(window, click_test_and_read)

        # (None), Music, Movies, plus the retained-but-unavailable "99".
        assert seen3["count"] == 4
        assert seen3["music_id"] == "99"
        assert "unavailable" in seen3["music_text"]
    finally:
        _close_window(window)


def test_plex_and_unrelated_settings_do_not_clobber_each_other(tmp_path, monkeypatch):
    window = _build_window(tmp_path, monkeypatch, tag="_noclobber")
    try:
        # Saving Plex preferences must not erase an unrelated setting
        # saved just before.
        window.lyric_visual_style = "visualiser"
        window._save_user_settings()

        def mutate(dialog):
            _find(dialog, "plexEnabledCheckbox").setChecked(True)
            _find(dialog, "plexUseManualServerCheckbox").setChecked(True)
            _find(dialog, "plexServerAddressField").setText("http://host:32400")
            _find(dialog, "plexTokenField").setText("keep-me-token")
        _open_preferences_and_run(window, mutate)

        with open(config_file_path(), "r", encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["lyric_visual_style"] == "visualiser"
        assert saved["plex_enabled"] is True

        # And a second, unrelated Preferences save (nothing Plex-related
        # touched this time) must not wipe the Plex settings just saved.
        def mutate_unrelated(dialog):
            pass
        _open_preferences_and_run(window, mutate_unrelated)

        assert window.plex_preferences.enabled is True
        assert window.plex_preferences.token == "keep-me-token"
        with open(config_file_path(), "r", encoding="utf-8") as f:
            saved2 = json.load(f)
        assert saved2["plex_token"] == "keep-me-token"
        assert saved2["lyric_visual_style"] == "visualiser"
    finally:
        _close_window(window)


def test_restart_restores_plex_configuration(tmp_path, monkeypatch):
    window = _build_window(tmp_path, monkeypatch, tag="_restart")
    try:
        def mutate(dialog):
            _find(dialog, "plexEnabledCheckbox").setChecked(True)
            _find(dialog, "plexUseManualServerCheckbox").setChecked(True)
            _find(dialog, "plexServerAddressField").setText("http://restart-test:32400")
            _find(dialog, "plexTokenField").setText("restart-token")
        _open_preferences_and_run(window, mutate)
        saved_client_id = window.plex_preferences.client_identifier
        saved_server_config_id = window.plex_preferences.server_config_id
    finally:
        _close_window(window)
    _pump(0.3)

    # Same LOCALAPPDATA, fresh PlayerWindow instance -- simulates an app
    # restart reading back the same config.json.
    window2 = PlayerWindow()
    window2.resize(1000, 800)
    window2.show()
    _pump(0.2)
    try:
        assert window2.plex_preferences.enabled is True
        assert window2.plex_preferences.server_address == "http://restart-test:32400"
        assert window2.plex_preferences.token == "restart-token"
        assert window2.plex_preferences.client_identifier == saved_client_id
        assert window2.plex_preferences.server_config_id == saved_server_config_id
    finally:
        _close_window(window2)


def test_account_mode_restart_signout_and_offline_resilience(tmp_path, monkeypatch):
    # Account mode + a saved server_client_identifier means the second
    # window construction below will genuinely trigger
    # _start_plex_startup_validation() -- correct, intended production
    # behaviour (see item 10: rediscover on restart). That real worker
    # must not be allowed to hit the real network in a test: mock the
    # transport so it deterministically (and quickly) reports "offline"
    # instead, matching this test's own explicit offline-resilience check.
    import requests

    def fake_get(url, headers=None, timeout=None, params=None):
        raise requests.exceptions.ConnectionError("network disabled for tests")
    monkeypatch.setattr("billsmusic.plex_account.requests.get", fake_get)

    window = _build_window(tmp_path, monkeypatch, tag="_account")
    try:
        # 18. Restart restores account token/username/selected server
        # identity/server_config_id_map -- set directly (the sign-in/
        # discovery network flow itself is covered in
        # test_plex_account.py) to isolate the persistence mechanism.
        window.plex_preferences = window.plex_preferences.__class__(
            enabled=True,
            client_identifier=window.plex_preferences.client_identifier,
            account_token="account-token-xyz",
            account_username="alice@example.com",
            server_client_identifier="plex-machine-abc123",
            server_config_id_map={"plex-machine-abc123": "server-cfg-uuid-1"},
            server_config_id="server-cfg-uuid-1",
        )
        window._save_user_settings()
    finally:
        _close_window(window)
    _pump(0.3)

    window2 = PlayerWindow()
    window2.resize(1000, 800)
    window2.show()
    _pump(0.2)
    try:
        assert window2.plex_preferences.account_token == "account-token-xyz"
        assert window2.plex_preferences.account_username == "alice@example.com"
        assert window2.plex_preferences.server_client_identifier == "plex-machine-abc123"
        assert window2.plex_preferences.server_config_id_map == {
            "plex-machine-abc123": "server-cfg-uuid-1",
        }
        assert window2.plex_preferences.server_config_id == "server-cfg-uuid-1"

        # 21. Local source remains fully usable when Plex startup
        # discovery/validation fails outright (server unreachable) --
        # _start_plex_startup_validation must never touch library_source
        # or the local tabs, and must leave _plex_status as "offline"
        # rather than raising.
        window2._on_plex_startup_discovery_result({"success": False, "reason": "Server unreachable"})
        assert window2._plex_status == "offline"
        assert window2.library_source == "local"
        assert window2.library_tabs.isVisible() is True

        # 20. Sign Out clears local authentication state in the dialog
        # (the account token itself is only actually persisted-as-cleared
        # once the dialog is Accepted, matching every other Preferences
        # field's existing save-on-Accept convention).
        seen = {}

        def sign_out(dialog):
            status_before = _find(dialog, "plexAccountStatusLabel").text()
            seen["status_before"] = status_before
            signout_button = _find(dialog, "plexSignOutButton")
            assert signout_button.isVisible() is True
            signout_button.click()
            seen["status_after"] = _find(dialog, "plexAccountStatusLabel").text()
            seen["signin_visible_after"] = _find(dialog, "plexSignInButton").isVisible()
        _open_preferences_and_run(window2, sign_out)

        assert "alice@example.com" in seen["status_before"]
        assert seen["status_after"] == "Not signed in"
        assert seen["signin_visible_after"] is True
        assert window2.plex_preferences.account_token == ""
        assert window2.plex_preferences.account_username == ""
    finally:
        _close_window(window2)


def test_library_mapping_is_scoped_per_server_in_the_dialog(tmp_path, monkeypatch):
    # Pre-Stage-2 invariant 3, UI-level regression: switching the server
    # combo must restore *that* server's own saved mapping, never bleed
    # another server's section id/name across, and never silently reuse
    # server A's mapping for a server the user has never configured.
    from unittest.mock import MagicMock

    def fake_get(url, headers=None, timeout=None, params=None):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = [
            {"name": "Server A", "clientIdentifier": "server-A", "owned": True,
             "provides": "server", "accessToken": "tokA", "connections": []},
            {"name": "Server B", "clientIdentifier": "server-B", "owned": True,
             "provides": "server", "accessToken": "tokB", "connections": []},
        ]
        return response
    monkeypatch.setattr("billsmusic.plex_account.requests.get", fake_get)

    window = _build_window(tmp_path, monkeypatch, tag="_libmap_scope")
    try:
        window.plex_preferences = window.plex_preferences.__class__(
            enabled=True,
            client_identifier=window.plex_preferences.client_identifier,
            account_token="account-token-xyz",
            account_username="alice@example.com",
            server_client_identifier="server-A",
            server_config_id_map={"server-A": "cfg-A", "server-B": "cfg-B"},
            library_mappings_by_server={
                "server-A": {"music_library_id": "1", "music_library_name": "Music A"},
                # server-B deliberately has NO saved mapping yet.
            },
        )
        window._save_user_settings()

        seen = {}

        def switch_servers(dialog):
            server_combo = _find(dialog, "plexServerCombo")
            music_combo = _find(dialog, "plexMusicLibraryCombo")
            deadline = time.monotonic() + 5.0
            while server_combo.count() < 2 and time.monotonic() < deadline:
                _app().processEvents()
                time.sleep(0.01)
            seen["count"] = server_combo.count()

            idx_a = server_combo.findData("server-A")
            server_combo.setCurrentIndex(idx_a)
            seen["server_a_music_id"] = music_combo.currentData()
            seen["server_a_music_text"] = music_combo.currentText()

            idx_b = server_combo.findData("server-B")
            server_combo.setCurrentIndex(idx_b)
            seen["server_b_music_id"] = music_combo.currentData()
            seen["server_b_music_count"] = music_combo.count()

            # Switching back to A must restore A's mapping again, not
            # whatever B's (empty) state left behind.
            server_combo.setCurrentIndex(idx_a)
            seen["server_a_music_id_again"] = music_combo.currentData()
        _open_preferences_and_run(window, switch_servers)

        assert seen["count"] == 2
        assert seen["server_a_music_id"] == "1"
        assert "Music A" in seen["server_a_music_text"]
        # Server B has no saved mapping -- must show as unconfigured
        # ("(None)" only), never inherit server A's "1"/"Music A".
        assert seen["server_b_music_id"] == ""
        assert seen["server_b_music_count"] == 1  # just "(None)"
        assert seen["server_a_music_id_again"] == "1"
    finally:
        _close_window(window)


# -- Local/Plex source selector (one window, several assertions) ------------

def test_source_selector_default_switch_and_local_mode_unaffected(tmp_path, monkeypatch):
    window = _build_window(tmp_path, monkeypatch, tag="_source")
    try:
        # Default is Local; Music/Videos/Karaoke tabs exist and are active,
        # completely unaffected by the Plex integration's mere presence.
        assert window.library_source == "local"
        assert window.library_source_selector.currentData() == "local"
        assert window.library_tabs.isVisible() is True
        assert window.library_plex_placeholder.isVisible() is False
        assert window.library_tabs.count() == 3
        assert window.library_tabs.tabText(0) == "Music"
        assert window.library_tabs.tabText(1) == "Videos"
        assert window.library_tabs.tabText(2) == "Karaoke"
        _wait_for_music_tab_settled(window)
        assert window._active_library_tab is window._music_tab
        assert window.tree_tracks is not None
        assert window.tree_tracks_video is not None
        assert window.tree_tracks_karaoke is not None

        # Switching to Plex persists immediately and swaps the tabs for a
        # placeholder (Stage 1: no real Plex browsing yet).
        idx = window.library_source_selector.findData("plex")
        window.library_source_selector.setCurrentIndex(idx)
        _pump(0.1)

        assert window.library_source == "plex"
        assert window.library_tabs.isVisible() is False
        assert window.library_plex_placeholder.isVisible() is True
        with open(config_file_path(), "r", encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["library_source"] == "plex"

        # Switching back to Local restores exactly the original state.
        idx = window.library_source_selector.findData("local")
        window.library_source_selector.setCurrentIndex(idx)
        _pump(0.1)
        assert window.library_source == "local"
        assert window.library_tabs.isVisible() is True
        assert window.library_plex_placeholder.isVisible() is False
    finally:
        _close_window(window)
