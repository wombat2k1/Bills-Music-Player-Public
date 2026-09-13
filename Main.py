"""Fast staged launcher that paints the splash before full-player imports."""
import time

_PROCESS_STARTED = time.perf_counter()

import faulthandler
import os
import sys

from PyQt6 import QtCore, QtGui, QtWidgets


_CRASH_FILE = None


def _log_directory():
    base = os.environ.get(
        "LOCALAPPDATA", os.path.dirname(os.path.abspath(__file__))
    )
    folder = os.path.join(base, "Bills Music Player")
    os.makedirs(folder, exist_ok=True)
    return folder


def _append_player_log(message):
    try:
        with open(
            os.path.join(_log_directory(), "player.log"),
            "a",
            encoding="utf-8",
        ) as handle:
            handle.write(str(message).rstrip() + "\n")
    except Exception:
        pass


def _install_crash_logging():
    def log_exception(ex):
        import traceback

        detail = "".join(
            traceback.format_exception(type(ex), ex, ex.__traceback__)
        )
        try:
            with open(
                os.path.join(_log_directory(), "error.log"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write(detail)
        except Exception:
            pass
        _append_player_log("Unhandled application exception:\n" + detail)
        try:
            from billsmusic.performance_diagnostics import get_diagnostics
            get_diagnostics().record(
                "exception", "unhandled_exception",
                status="failure", severity="fatal",
                details={
                    "exception_type": type(ex).__name__,
                    "exception": str(ex),
                    "traceback": detail,
                },
                minimum_level="off",
            )
        except Exception:
            pass

    def excepthook(exctype, value, tb):
        log_exception(value)
        sys.__excepthook__(exctype, value, tb)

    sys.excepthook = excepthook
    global _CRASH_FILE
    try:
        _CRASH_FILE = open(
            os.path.join(_log_directory(), "crash.log"),
            "a",
            encoding="utf-8",
        )
        _CRASH_FILE.write(
            f"\n--- crash capture started {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"pid={os.getpid()} ---\n"
        )
        _CRASH_FILE.flush()
        faulthandler.enable(file=_CRASH_FILE)
    except Exception:
        pass
    return log_exception


def main() -> int:
    qapplication_started = time.perf_counter()
    app = QtWidgets.QApplication(sys.argv)
    from billsmusic import __version__
    app.setApplicationName("Bills Music Player")
    app.setApplicationVersion(__version__)
    qapplication_ms = (time.perf_counter() - qapplication_started) * 1000.0

    # This module is deliberately lightweight and imported only after the one
    # QApplication exists. Full player imports remain deferred until later.
    from billsmusic.splash import (
        StartupCoordinator,
        StartupSplash,
        remaining_minimum_ms,
    )

    splash = StartupSplash(app, warning_logger=_append_player_log)
    coordinator = StartupCoordinator(
        splash,
        _append_player_log,
        started_at=_PROCESS_STARTED,
    )
    coordinator.report(
        "qapplication",
        "Starting Bills Music Player...",
        2,
        qapplication_ms,
    )
    splash.show()
    app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents)
    coordinator.mark_splash_shown()
    coordinator.report(
        "splash-first-shown",
        "Loading application modules...",
        5,
    )

    log_exception = _install_crash_logging()
    state = {"window": None, "show_started": None, "diagnostics": None}

    def fail_startup(ex):
        error = ex if isinstance(ex, BaseException) else RuntimeError(str(ex))
        log_exception(error)
        coordinator.fail(str(error))
        window = state.get("window")
        if window is not None:
            try:
                window.close()
            except Exception:
                pass
        coordinator.finish(None)
        QtWidgets.QMessageBox.critical(
            None,
            "Bills Music Player",
            "Bills Music Player could not start.\n\n"
            "Details were written to player.log.",
        )
        app.exit(1)

    def finish_splash():
        window = state["window"]
        coordinator.finish(window)
        diagnostics = state.get("diagnostics")
        if diagnostics is not None:
            diagnostics.record(
                "startup", "splash_finish",
                duration_ms=(
                    (coordinator.splash_finished_at - coordinator.splash_shown_at)
                    * 1000.0
                    if coordinator.splash_finished_at
                    and coordinator.splash_shown_at else 0.0
                ),
                details={
                    "process_total_ms": (
                        time.perf_counter() - _PROCESS_STARTED
                    ) * 1000.0
                },
                minimum_level="basic",
            )

    def first_window_paint():
        window = state["window"]
        if window is None:
            return
        paint_started = state.get("show_started") or time.perf_counter()
        coordinator.metrics["first-main-window-paint"] = (
            time.perf_counter() - paint_started
        ) * 1000.0
        coordinator.complete(window)
        diagnostics = state.get("diagnostics")
        if diagnostics is not None:
            diagnostics.record(
                "startup", "main_window_first_paint",
                duration_ms=coordinator.metrics.get(
                    "first-main-window-paint", 0.0
                ),
                details={
                    "process_elapsed_ms": (
                        time.perf_counter() - _PROCESS_STARTED
                    ) * 1000.0
                },
                minimum_level="basic",
            )
        window.show_startup_warning()
        delay = remaining_minimum_ms(coordinator.splash_shown_at)
        if delay:
            QtCore.QTimer.singleShot(delay, finish_splash)
        else:
            finish_splash()

    def show_ready_window():
        window = state["window"]
        if window is None:
            return
        coordinator.report("ready", "Almost ready...", 96)
        window.first_paint.connect(
            first_window_paint, QtCore.Qt.ConnectionType.SingleShotConnection
        )
        state["show_started"] = time.perf_counter()
        window.show()

    def construct_window(PlayerWindow):
        try:
            started = time.perf_counter()
            window = PlayerWindow(startup_reporter=coordinator.report)
            duration_ms = (time.perf_counter() - started) * 1000.0
            state["window"] = window
            diagnostics = state.get("diagnostics")
            if diagnostics is not None:
                diagnostics.record(
                    "startup", "main_window_shell",
                    duration_ms=duration_ms,
                    minimum_level="basic",
                )
            coordinator.report(
                "main-window-shell",
                "Starting player services...",
                15,
                duration_ms,
            )
            window.startup_ready.connect(
                show_ready_window,
                QtCore.Qt.ConnectionType.SingleShotConnection,
            )
            window.startup_failed.connect(
                fail_startup,
                QtCore.Qt.ConnectionType.SingleShotConnection,
            )
        except Exception as ex:
            fail_startup(ex)

    def import_application():
        try:
            started = time.perf_counter()
            from billsmusic.platform_utils import configure_vlc_env

            configure_vlc_env()
            from billsmusic.window import PlayerWindow
            from billsmusic.config import load_config
            from billsmusic.performance_diagnostics import get_diagnostics

            diagnostics = get_diagnostics()
            try:
                diagnostics_config = load_config() or {}
            except Exception:
                diagnostics_config = {}
            diagnostics.configure(
                str(os.environ.get(
                    "BILLSMUSIC_DIAGNOSTICS_LEVEL",
                    diagnostics_config.get(
                        "diagnostics_level", "basic"
                    ),
                )).lower(),
                bool(diagnostics_config.get(
                    "diagnostics_include_full_paths", False
                )),
            )
            state["diagnostics"] = diagnostics

            coordinator.report(
                "heavy-imports",
                "Preparing the interface...",
                12,
                (time.perf_counter() - started) * 1000.0,
            )
            diagnostics.record(
                "startup", "heavy_imports",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                minimum_level="basic",
            )
            QtCore.QTimer.singleShot(
                0, lambda: construct_window(PlayerWindow)
            )
        except Exception as ex:
            fail_startup(ex)

    def run_analysis_warmup():
        _append_player_log("Analysis/mutagen warm-up starting (synchronous, before PlayerWindow)...")
        try:
            from billsmusic import analysis_warmup

            started = time.perf_counter()
            succeeded = analysis_warmup.run_synchronously()
            duration_ms = (time.perf_counter() - started) * 1000.0
            info = analysis_warmup.report()
            _append_player_log(
                "Analysis/mutagen warm-up finished: "
                f"status={'success' if succeeded else 'unavailable'}; "
                f"duration_ms={duration_ms:.1f}; "
                f"librosa_succeeded={info['librosa_succeeded']}; "
                f"librosa_elapsed_ms={info['librosa_elapsed_ms']}; "
                f"mutagen_succeeded={info['mutagen_succeeded']}; "
                f"mutagen_elapsed_ms={info['mutagen_elapsed_ms']}; "
                f"mutagen_version={info['mutagen_version']}; "
                f"pychromecast_succeeded={info['pychromecast_succeeded']}; "
                f"pychromecast_elapsed_ms={info['pychromecast_elapsed_ms']}; "
                f"python_executable={info['python_executable']}; "
                f"python_version={info['python_version']}"
            )
            if not info["mutagen_succeeded"]:
                # Never let this be silent: mutagen.File() will still work
                # without the warm-up (it just imports on first real use
                # instead), but that first real use is exactly the timing
                # window the warm-up exists to remove, so the app is now
                # running in a known partially-warmed state.
                _append_player_log(
                    "WARNING: mutagen warm-up did not complete -- "
                    f"error={info['mutagen_error']!r}. The app will still "
                    "run, but the first real tag read will do this import "
                    "work instead of it having been done here."
                )
            coordinator.report(
                "analysis-warmup",
                "Loading application modules...",
                9,
                duration_ms,
            )
            QtCore.QTimer.singleShot(0, import_application)
        except Exception as ex:
            # Python exceptions mean analysis is unavailable, not that the
            # player itself cannot run. Native access violations are handled
            # by faulthandler and cannot be recovered in-process.
            _append_player_log(f"WARNING: Analysis/mutagen warm-up failed outright: {ex}")
            QtCore.QTimer.singleShot(0, import_application)

    def announce_analysis_warmup():
        # Return to the event loop once after updating the text so the splash
        # visibly explains the one synchronous/native-initialisation pause.
        coordinator.report(
            "analysis-warmup-start",
            "Preparing audio analysis...",
            7,
        )
        QtCore.QTimer.singleShot(0, run_analysis_warmup)

    QtCore.QTimer.singleShot(0, announce_analysis_warmup)
    return app.exec()


if __name__ == "__main__":
    # Video plays in a separate child process (isolating Qt Multimedia's
    # native decode threads from the rest of the app's threading -- see
    # billsmusic/video_backend.py). In a packaged (Nuitka/PyInstaller)
    # build there is no standalone python.exe to launch with `-m`, so the
    # child re-invokes this same executable with this flag instead; this
    # must be the very first thing checked, before any normal startup work.
    if "--video-subprocess" in sys.argv:
        from billsmusic.video_subprocess import main as video_subprocess_main
        raise SystemExit(video_subprocess_main() or 0)
    # Smart Video Transition Points' bounded black-frame/silence probe --
    # a wholly separate, disposable child process sharing no code with the
    # video playback subprocess above (see video_transition_point_probe_
    # subprocess.py's own docstring for why it stays isolated the same way).
    if "--video-transition-point-probe" in sys.argv:
        from billsmusic.video_transition_point_probe_subprocess import main as transition_point_probe_main
        raise SystemExit(transition_point_probe_main() or 0)
    raise SystemExit(main())
