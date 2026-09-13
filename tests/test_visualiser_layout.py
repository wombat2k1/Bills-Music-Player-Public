import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtGui, QtWidgets

from billsmusic.window import PlayerWindow


_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


class LayoutHarness(QtWidgets.QMainWindow):
    _apply_visualiser_layout = PlayerWindow._apply_visualiser_layout
    _remember_right_splitter_state = PlayerWindow._remember_right_splitter_state
    _restore_right_splitter_position = PlayerWindow._restore_right_splitter_position

    def __init__(self):
        super().__init__()
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        self.search_box = QtWidgets.QLineEdit()
        root.addWidget(self.search_box)
        self.visualiser_layout = QtWidgets.QVBoxLayout()
        root.addLayout(self.visualiser_layout, 1)
        self.right_splitter = QtWidgets.QSplitter(
            QtCore.Qt.Orientation.Vertical
        )
        self.right_splitter.setChildrenCollapsible(False)
        self.visualiser_layout.addWidget(self.right_splitter)
        self.visualiser_frame = QtWidgets.QFrame()
        self.visualiser_frame.setMinimumHeight(190)
        self.right_splitter.addWidget(self.visualiser_frame)
        self.queue_panel = QtWidgets.QWidget()
        queue_layout = QtWidgets.QVBoxLayout(self.queue_panel)
        self.queue_list = QtWidgets.QListWidget()
        self.queue_list.addItems([f"Track {index}" for index in range(30)])
        queue_layout.addWidget(self.queue_list)
        self.right_tabs = QtWidgets.QTabWidget()
        self.right_tabs.setMinimumHeight(190)
        self.right_tabs.addTab(self.queue_panel, "Up Next")
        self.right_splitter.addWidget(self.right_tabs)
        self.right_splitter.setSizes([650, 350])
        self._visualiser_splitter_state = None
        self._splitter_save_timer = None
        self.right_splitter.splitterMoved.connect(
            self._remember_right_splitter_state
        )
        self.controls = QtWidgets.QWidget()
        self.controls.setFixedHeight(40)
        root.addWidget(self.controls)
        self.action_toggle_visualiser = QtGui.QAction(self)
        self.action_toggle_visualiser.setCheckable(True)
        self.action_toggle_visualiser.setChecked(True)
        self._visualiser_fullscreen = False


def test_splitter_is_draggable_and_neither_panel_can_collapse():
    app = _app()
    window = LayoutHarness()
    window.resize(900, 900)
    window.show()
    app.processEvents()

    window.right_splitter.setSizes([300, 500])
    app.processEvents()

    visualiser_height, lower_height = window.right_splitter.sizes()
    assert visualiser_height >= 190
    assert lower_height >= 190
    assert lower_height > visualiser_height
    assert not window.right_splitter.childrenCollapsible()


def test_hidden_visualiser_removes_handle_and_restores_previous_split():
    app = _app()
    window = LayoutHarness()
    window.resize(900, 900)
    window.show()
    app.processEvents()
    window.right_splitter.setSizes([360, 420])
    app.processEvents()
    expected = window.right_splitter.sizes()

    window._apply_visualiser_layout(False)
    app.processEvents()

    assert not window.visualiser_frame.isVisible()
    assert not window.right_splitter.handle(1).isVisible()
    assert window.right_tabs.height() > expected[1]

    window._apply_visualiser_layout(True)
    app.processEvents()

    assert window.visualiser_frame.isVisible()
    assert window.right_splitter.handle(1).isVisible()
    restored = window.right_splitter.sizes()
    assert abs(restored[0] - expected[0]) <= 2
    assert abs(restored[1] - expected[1]) <= 2
    assert window.action_toggle_visualiser.isChecked()
