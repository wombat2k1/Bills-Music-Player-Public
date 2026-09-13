import ast
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtGui, QtWidgets

from billsmusic.splash import (
    ARTWORK_HEIGHT,
    ARTWORK_WIDTH,
    PROGRESS_RECT,
    STATUS_RECT,
    VERSION_RECT,
    StartupCoordinator,
    StartupSplash,
    remaining_minimum_ms,
    resource_path,
)
from billsmusic.window import PlayerWindow


ROOT = Path(__file__).resolve().parents[1]
_APP = None


def _app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def test_supplied_splash_artwork_resolves_from_source():
    path = resource_path(
        "billsmusic", "assets", "bills_music_splash.png"
    )
    image = QtGui.QImage(path)
    assert Path(path).is_absolute()
    assert not image.isNull()
    assert image.width() == ARTWORK_WIDTH == 1672
    assert image.height() == ARTWORK_HEIGHT == 941


def test_missing_artwork_uses_fallback_and_logs_warning():
    warnings = []
    splash = StartupSplash(
        _app(),
        image_path=str(ROOT / "missing-splash.png"),
        warning_logger=warnings.append,
    )
    assert splash.using_fallback
    assert warnings
    assert "using fallback" in warnings[0]


def test_real_progress_widget_starts_empty_and_is_proportional():
    splash = StartupSplash(_app())
    assert isinstance(splash.progress, QtWidgets.QProgressBar)
    assert splash.progress.value() == 0
    original_width = splash.progress.width()
    expected_ratio = PROGRESS_RECT[2] / ARTWORK_WIDTH
    assert abs(original_width / splash.width() - expected_ratio) < 0.01

    splash.resize(splash.width() // 2, splash.height() // 2)
    splash._layout_overlays()
    resized = splash.progress.geometry()
    assert abs(resized.width() / original_width - 0.5) < 0.05
    assert abs(resized.x() / splash.width() - PROGRESS_RECT[0] / ARTWORK_WIDTH) < 0.01


def test_status_message_sits_below_artwork_without_clipping():
    splash = StartupSplash(_app())
    status = splash.status.geometry()
    assert STATUS_RECT[1] > PROGRESS_RECT[1] + PROGRESS_RECT[3]
    assert STATUS_RECT[1] >= 880
    assert STATUS_RECT[1] + STATUS_RECT[3] <= ARTWORK_HEIGHT - 10
    assert status.height() >= splash.status.sizeHint().height()


def test_splash_shows_small_version_label_in_top_left():
    splash = StartupSplash(_app())

    assert splash.version.text().startswith("v1.")
    assert splash.version.geometry() == splash._scaled_rect(VERSION_RECT)
    assert VERSION_RECT[0] < ARTWORK_WIDTH // 4
    assert VERSION_RECT[1] < ARTWORK_HEIGHT // 4


def test_progress_is_clamped_and_never_moves_backwards():
    splash = StartupSplash(_app())
    splash.set_stage("First", -10)
    assert splash.progress.value() == 0
    splash.set_stage("Second", 70)
    splash.set_stage("Third", 20)
    assert splash.progress.value() == 70
    assert splash.status.text() == "Third"
    splash.set_stage("Ready", 500)
    assert splash.progress.value() == 100


def test_stage_messages_and_accessible_status_update():
    splash = StartupSplash(_app())
    logs = []
    clock_values = iter([10.0, 10.1, 10.2])
    coordinator = StartupCoordinator(
        splash, logs.append, started_at=10.0, clock=lambda: next(clock_values)
    )
    coordinator.report("preferences", "Loading preferences...", 15, 4.2)
    assert splash.status.text() == "Loading preferences..."
    assert splash.progress.value() == 15
    assert splash.accessibleDescription() == "Loading preferences..."
    assert "stage=preferences" in logs[-1]


def test_minimum_duration_only_waits_for_remaining_portion():
    assert remaining_minimum_ms(5.0, now=5.1, minimum_ms=400) == 300
    assert remaining_minimum_ms(5.0, now=5.5, minimum_ms=400) == 0
    assert remaining_minimum_ms(5.0, now=7.0, minimum_ms=400) == 0


def test_finish_closes_splash_after_target_is_visible():
    app = _app()
    splash = StartupSplash(app)
    target = QtWidgets.QWidget()
    target.show()
    splash.show()
    app.processEvents()
    assert target.isVisible() and splash.isVisible()
    splash.finish(target)
    assert not splash.isVisible()


def test_failure_status_is_exposed_and_does_not_increase_progress():
    splash = StartupSplash(_app())
    splash.set_stage("Loading", 55)
    splash.set_failed()
    assert splash.status.text() == "Bills Music Player could not start"
    assert splash.progress.value() == 55


def test_coordinator_failure_updates_splash_logs_and_emits():
    splash = StartupSplash(_app())
    splash.show()
    _app().processEvents()
    logs = []
    failures = []
    coordinator = StartupCoordinator(
        splash, logs.append, started_at=1.0, clock=lambda: 1.1
    )
    coordinator.startup_failed.connect(failures.append)
    coordinator.fail("boom")
    assert splash.status.text() == "Bills Music Player could not start"
    assert failures == ["boom"]
    assert logs[-1] == "Startup failed: boom"
    coordinator.finish(None)
    assert not splash.isVisible()


def test_startup_stage_callbacks_yield_to_event_loop():
    app = _app()
    calls = []

    class Harness:
        _startup_ready_emitted = False
        _startup_failed_emitted = False
        _schedule_startup_stage = PlayerWindow._schedule_startup_stage
        _run_startup_stage = PlayerWindow._run_startup_stage

        def _log(self, _message):
            pass

        def _emit_startup_failed(self, message):
            raise AssertionError(message)

    harness = Harness()
    harness._schedule_startup_stage(lambda: calls.append("paintable stage"))
    assert calls == []
    app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents)
    assert calls == ["paintable stage"]


def test_cache_restore_failure_returns_empty_cache_and_warning():
    logs = []

    class Harness:
        _load_initial_library_cache = PlayerWindow._load_initial_library_cache
        _startup_warning = ""

        def _load_cache(self):
            raise ValueError("bad cache")

        def _log(self, message):
            logs.append(message)

    harness = Harness()
    cache, failed = harness._load_initial_library_cache()
    assert cache is None and failed
    assert "bad cache" in logs[-1]
    assert "Traceback" in logs[-1]
    assert "Rescan Library" in harness._startup_warning


def test_library_tree_failure_completes_initial_startup():
    app = _app()

    class Beat:
        def setPaused(self, _paused):
            pass

    class Harness:
        _tree_build_tick = PlayerWindow._tree_build_tick

        def __init__(self):
            self._library_apply_generation = None
            self._library_search_generation = 0
            self._build_queue = [("malformed",)]
            self._build_timer = QtCore.QTimer()
            self._build_timer.start(1000)
            self.tree_tracks = QtWidgets.QTreeWidget()
            self.beat = Beat()
            self._startup_ready_emitted = False
            self._startup_warning = ""
            self.logs = []
            self.ready_count = 0

        def _cancel_pending_library_apply(self):
            self._build_timer.stop()
            self._build_queue = []

        def _log(self, message):
            self.logs.append(message)

        def _emit_startup_ready(self):
            if not self._startup_ready_emitted:
                self._startup_ready_emitted = True
                self.ready_count += 1

    harness = Harness()
    harness._tree_build_tick()
    app.processEvents()
    assert not harness._build_timer.isActive()
    assert harness.ready_count == 1
    assert "Traceback" in harness.logs[-1]
    assert "Rescan Library" in harness._startup_warning


def test_ready_or_failed_outcome_is_emitted_only_once():
    splash = StartupSplash(_app())
    clock = type("Clock", (), {"now": 1.0, "__call__": lambda self: self.now})()
    coordinator = StartupCoordinator(
        splash, lambda _message: None, started_at=1.0, clock=clock
    )
    ready = []
    failed = []
    coordinator.startup_complete.connect(ready.append)
    coordinator.startup_failed.connect(failed.append)
    clock.now = 1.5
    target = QtWidgets.QWidget()
    coordinator.complete(target)
    coordinator.fail("too late")
    assert ready == [target]
    assert failed == []
    assert coordinator.outcome == "ready"


def test_actual_splash_duration_is_logged_when_finished():
    logs = []
    splash = StartupSplash(_app())
    clock = type("Clock", (), {"now": 10.0, "__call__": lambda self: self.now})()
    coordinator = StartupCoordinator(
        splash, logs.append, started_at=10.0, clock=clock
    )
    clock.now = 10.1
    coordinator.mark_splash_shown()
    clock.now = 10.5
    coordinator.complete(QtWidgets.QWidget())
    clock.now = 10.8
    coordinator.finish(None)
    assert coordinator.main_window_first_paint_at == 10.5
    assert coordinator.splash_finished_at == 10.8
    assert "splash_visible_ms=700.0" in logs[-1]
    assert "process_to_first_paint_ms=500.0" in logs[-1]


def test_progress_reaches_100_only_when_window_is_ready():
    splash = StartupSplash(_app())
    coordinator = StartupCoordinator(
        splash, lambda _message: None, started_at=1.0, clock=lambda: 1.1
    )
    coordinator.report("workers", "Starting services", 99)
    assert splash.progress.value() == 99
    coordinator.complete(QtWidgets.QWidget())
    assert splash.progress.value() == 100


def test_entrypoint_has_one_qapplication_and_defers_full_imports():
    tree = ast.parse((ROOT / "Main.py").read_text(encoding="utf-8"))
    module_imports = [
        node
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert not any(
        isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith("billsmusic")
        for node in module_imports
    )

    qapplication_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "QApplication"
    ]
    assert len(qapplication_calls) == 1

    source = (ROOT / "Main.py").read_text(encoding="utf-8")
    assert source.index("_PROCESS_STARTED = time.perf_counter()") < source.index(
        "from PyQt6 import"
    )
    assert source.index("splash.show()") < source.index(
        "from billsmusic.window import PlayerWindow"
    )
    assert source.index("app.processEvents(") < source.index(
        "from billsmusic.window import PlayerWindow"
    )
    assert source.index("def first_window_paint") < source.index(
        "coordinator.complete(window)"
    )


def test_compatibility_entrypoint_does_not_import_window_at_module_level():
    tree = ast.parse(
        (ROOT / "billsmusic" / "app.py").read_text(encoding="utf-8-sig")
    )
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module == "billsmusic.window"
        for node in tree.body
    )


def test_pyinstaller_spec_bundles_only_the_required_splash_path():
    spec = (ROOT / "Bills Music Player.spec").read_text(encoding="utf-8")
    assert '"billsmusic/assets/bills_music_splash.png"' in spec
    assert '"billsmusic/assets", "billsmusic/assets"' not in spec


def test_packaged_resource_lookup_uses_bundle_root(monkeypatch):
    bundle_root = str(ROOT / "packaged-root")
    monkeypatch.setattr(sys, "_MEIPASS", bundle_root, raising=False)
    path = resource_path(
        "billsmusic", "assets", "bills_music_splash.png"
    )
    assert path == os.path.join(
        bundle_root,
        "billsmusic",
        "assets",
        "bills_music_splash.png",
    )
