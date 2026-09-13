"""Video visibility must suspend the main-window visualiser (and resume it
when video ends), reusing the existing VisualiserLifecycleController rather
than any new suspend mechanism -- QStackedWidget already makes the
visualiser page's own isVisible() go False for free when a different page
is current; this only confirms the lifecycle controller is actually asked
to re-evaluate at the right moments."""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from billsmusic.window import PlayerWindow


def _display_stack_window():
    refresh_calls = []
    window = SimpleNamespace(
        right_display_stack=MagicMock(),
        _video_loading_page=object(),
        _video_output_page=object(),
        _video_error_page=object(),
        _normal_display_page=object(),
        _video_error_label=SimpleNamespace(setText=lambda text: None),
        _refresh_visualiser_lifecycle=lambda reason: refresh_calls.append(reason),
    )
    return window, refresh_calls


def test_showing_video_pages_refreshes_visualiser_lifecycle():
    window, refresh_calls = _display_stack_window()
    PlayerWindow._show_video_loading_page(window)
    PlayerWindow._show_video_output_page(window)
    PlayerWindow._show_video_error_page(window, "oops")
    assert refresh_calls == ["video_shown", "video_shown", "video_shown"]


def test_returning_to_normal_display_refreshes_visualiser_lifecycle():
    window, refresh_calls = _display_stack_window()
    PlayerWindow._show_normal_display_page(window)
    assert refresh_calls == ["video_hidden"]
