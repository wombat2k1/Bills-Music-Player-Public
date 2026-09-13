"""Paint/geometry smoke coverage for every Phase 1 transition effect."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets

from billsmusic.video_transition import EFFECTS
from billsmusic.video_transition_overlay import VideoTransitionOverlay


def test_all_effects_paint_resize_and_release_mouse_input():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = QtWidgets.QWidget()
    window.move(40, 30)
    window.resize(1000, 700)
    host = QtWidgets.QWidget(window)
    host.move(120, 90)
    host.resize(640, 360)
    window.show()
    overlay = VideoTransitionOverlay()
    overlay.set_host(host)
    overlay.show()
    app.processEvents()

    # Regression: the old implementation made the overlay a native child of
    # the video host.  That changed the embedded foreign QWindow's native
    # ancestry, displaced the picture, and could leave mouse input blocked.
    assert overlay.parentWidget() is None
    assert overlay.windowFlags() & QtCore.Qt.WindowType.WindowTransparentForInput
    assert overlay.pos() == host.mapToGlobal(QtCore.QPoint(0, 0))

    for effect in EFFECTS:
        overlay._effect = effect
        overlay._phase = "outgoing"
        overlay._progress = 0.62
        overlay.update()
        # An exception from paintEvent aborts PyQt's event callback, so
        # reaching the next iteration proves each painter path is valid.
        app.processEvents()

    host.resize(913, 517)
    app.processEvents()
    assert overlay.size() == host.size()
    assert overlay.pos() == host.mapToGlobal(QtCore.QPoint(0, 0))
    window.move(170, 140)
    app.processEvents()
    assert overlay.pos() == host.mapToGlobal(QtCore.QPoint(0, 0))
    assert overlay.testAttribute(
        QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents
    )
    overlay.cancel()
    assert overlay.isHidden()
    overlay.shutdown()
    window.deleteLater()
