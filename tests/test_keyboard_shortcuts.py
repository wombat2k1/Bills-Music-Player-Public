import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtGui, QtWidgets

from billsmusic.window import PlayerWindow, SHORTCUTS


_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


class ActionHarness(QtWidgets.QMainWindow):
    _make_window_action = PlayerWindow._make_window_action
    _build_accessible_actions = PlayerWindow._build_accessible_actions
    _set_top_menu_visible = PlayerWindow._set_top_menu_visible
    _add_window_menu_options = PlayerWindow._add_window_menu_options
    _repair_missing_playlist_tracks = lambda self, *args, **kwargs: None
    _build_sleep_timer_menu = PlayerWindow._build_sleep_timer_menu
    _on_sleep_timer_menu_action = PlayerWindow._on_sleep_timer_menu_action
    _on_sleep_timer_fade_toggled = PlayerWindow._on_sleep_timer_fade_toggled

    def __init__(self):
        super().__init__()
        self.setCentralWidget(QtWidgets.QWidget())
        self.lyrics_enabled = True
        self.visualiser_frame = QtWidgets.QWidget()

    def _toggle_play_pause(self):
        pass

    def stop_playback(self):
        pass

    def prev_track(self):
        pass

    def next_track(self):
        pass

    def _adjust_volume(self, delta):
        pass

    def toggle_mute(self):
        pass

    def _focus_library_search(self):
        pass

    def _focus_library(self):
        pass

    def _focus_up_next(self):
        pass

    def _toggle_lyrics_accessibly(self):
        pass

    def _toggle_visualiser_accessibly(self):
        pass

    def _toggle_mini_player(self):
        pass

    def _toggle_party_mode(self):
        pass

    def _show_recently_played(self):
        pass

    def _undo_queue_change(self):
        pass

    def _export_performance_diagnostics(self):
        pass

    def _open_diagnostics_folder(self):
        pass

    def _copy_performance_summary(self):
        pass

    def _show_normalisation_preferences(self):
        pass

    def _save_user_settings(self):
        pass

    def _toggle_video_fullscreen(self):
        pass


class _FakeDiagnostics:
    def record(self, *args, **kwargs):
        pass


class QueueHarness(QtWidgets.QMainWindow):
    _ensure_queue_played_flags = PlayerWindow._ensure_queue_played_flags
    _remove_selected_queue_item = PlayerWindow._remove_selected_queue_item
    _move_selected_queue_item = PlayerWindow._move_selected_queue_item
    _update_undo_action_state = PlayerWindow._update_undo_action_state

    def __init__(self):
        super().__init__()
        self.queue = ["one", "two", "three"]
        self.queue_played = [False, True, False]
        self.queue_list = QtWidgets.QListWidget()
        self.queue_list.addItems(self.queue)
        self.messages = []
        self.diagnostics = _FakeDiagnostics()
        self.current_path = None
        self._queue_undo_snapshot = None

    def _refresh_queue_list(self, keep_played_bottom=True):
        self.queue_list.clear()
        self.queue_list.addItems(self.queue)

    def _remove_queue_row_widget(self, row, reason="track_removed"):
        self.queue_list.takeItem(row)
        return True

    def _move_queue_row_widget(self, source, target, reason="track_moved"):
        item = self.queue_list.takeItem(source)
        self.queue_list.insertItem(target, item)
        return True

    def _schedule_session_save(self):
        pass

    def _announce_accessible_status(self, message):
        self.messages.append(message)


def test_central_actions_have_expected_shortcuts():
    _app()
    window = ActionHarness()
    window._build_accessible_actions()
    assert window.action_play_pause.shortcut() == QtGui.QKeySequence(SHORTCUTS["play_pause"])
    assert window.action_stop.shortcut() == QtGui.QKeySequence("Ctrl+S")
    assert window.action_previous.shortcut() == QtGui.QKeySequence("Ctrl+Left")
    assert window.action_next.shortcut() == QtGui.QKeySequence("Ctrl+Right")
    assert window.action_recently_played.shortcut() == QtGui.QKeySequence("Ctrl+Shift+R")
    assert window.action_video_fullscreen.shortcut() == QtGui.QKeySequence("F11")
    assert window.action_play_pause.shortcutContext() == QtCore.Qt.ShortcutContext.WidgetShortcut
    assert window.action_play_pause_alternate.shortcutContext() == QtCore.Qt.ShortcutContext.WindowShortcut


def test_top_menu_is_hidden_by_default_and_available_from_context_menus():
    _app()
    window = ActionHarness()
    window._build_accessible_actions()
    assert window.menuBar().isHidden()
    assert not window.action_show_top_menu.isChecked()

    context_menu = QtWidgets.QMenu(window)
    window._add_window_menu_options(context_menu)
    assert window.action_show_top_menu in context_menu.actions()

    window.action_show_top_menu.setChecked(True)
    assert not window.menuBar().isHidden()


def test_preferences_and_diagnostic_help_actions_are_available():
    _app()
    window = ActionHarness()
    window._build_accessible_actions()
    menus = {
        action.text().replace("&", ""): action.menu()
        for action in window.menuBar().actions()
    }
    assert "Preferences..." in [
        action.text() for action in menus["View"].actions()
    ]
    help_actions = [action.text() for action in menus["Help"].actions()]
    assert "Export Performance Diagnostic Bundle..." in help_actions
    assert "Open Diagnostics Folder" in help_actions
    assert "Copy Latest Performance Summary" in help_actions


def test_remove_queue_item_keeps_played_flags_aligned_and_selects_neighbour():
    _app()
    window = QueueHarness()
    window.queue_list.setCurrentRow(1)
    window._remove_selected_queue_item()
    assert window.queue == ["one", "three"]
    assert window.queue_played == [False, False]
    assert window.queue_list.currentRow() == 1


def test_move_queue_item_preserves_played_state_and_selection():
    _app()
    window = QueueHarness()
    window.queue_list.setCurrentRow(1)
    window._move_selected_queue_item(-1)
    assert window.queue == ["two", "one", "three"]
    assert window.queue_played == [True, False, False]
    assert window.queue_list.currentRow() == 0
    window._move_selected_queue_item(1)
    assert window.queue == ["one", "two", "three"]
    assert window.queue_played == [False, True, False]
    assert window.queue_list.currentRow() == 1


def test_move_queue_item_does_nothing_at_boundary():
    _app()
    window = QueueHarness()
    window.queue_list.setCurrentRow(0)
    window._move_selected_queue_item(-1)
    assert window.queue == ["one", "two", "three"]
    assert window.queue_played == [False, True, False]
