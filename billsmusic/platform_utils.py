"""Platform / packaging helpers (PyInstaller resource paths, VLC env)."""
import os
import sys


_DLL_DIR_HANDLES = []


def is_frozen_build() -> bool:
    """True when running as a packaged build (Nuitka standalone or
    PyInstaller), false when running from source.

    `sys.frozen` alone is NOT reliable for this under Nuitka: it's a
    PyInstaller convention that Nuitka only patches into a short, fixed
    list of specific known stdlib/third-party source lines (see Nuitka's
    own standard.nuitka-package.config.yml / stdlib3.nuitka-package.
    config.yml "workaround for 'sys.frozen' not being set" entries) -- it
    does NOT apply to arbitrary application code that checks it directly,
    confirmed by a rebuilt package still misbehaving with that check alone
    (see billsmusic/audio.py's crash-log-documented history and
    video_backend.py's subprocess-launch-command bug, both of which used
    to gate on `sys.frozen` by itself). `__compiled__` is what Nuitka
    actually injects as a real per-module global into every module it
    compiles, and is the general-purpose check Nuitka's own config
    recommends OR'ing in for exactly this situation; PyInstaller keeps
    setting `sys.frozen` correctly on its own.
    """
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def current_executable_path() -> str:
    """The actual path to the running executable -- reliable even when
    `sys.executable` is not.

    Confirmed via player.log (2026-08-06): in this Nuitka standalone build
    (custom `--output-filename="Bills Music Player.exe"`), `sys.executable`
    reports Nuitka's internal default name ("...\\Main.dist\\python.exe")
    instead of the renamed output binary -- a file that does not exist on
    disk under that name. Anything that spawns a copy of the running
    executable (video_backend.py re-launching itself as the video
    subprocess) using `sys.executable` as the program path fails with
    Windows error 2 ("The system cannot find the file specified"), which
    is what actually surfaced to users as "video player stopped
    unexpectedly": the failure happens before the child process even
    starts, so it looks identical to a crash from the outside.

    `GetModuleFileNameW(NULL, ...)` is the Windows-native, always-correct
    answer to "what file am I actually running from", independent of
    whatever `sys.executable` claims -- used here (frozen + Windows only;
    `sys.executable` is trustworthy everywhere else, including this same
    build's own non-Windows CI/dev use, which doesn't exist yet but costs
    nothing to keep correct).
    """
    if is_frozen_build() and sys.platform == "win32":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(32768)
            ctypes.windll.kernel32.GetModuleFileNameW(None, buf, len(buf))
            if buf.value:
                return buf.value
        except Exception:
            pass
    return sys.executable


def resource_path(relative_path: str) -> str:
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(project_root, relative_path)


def _vlc_candidate_dirs():
    candidates = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(sys._MEIPASS)
    exe_dir = os.path.dirname(sys.executable)
    if exe_dir:
        candidates.append(exe_dir)
    for env_name in ("VLC_DIR", "VIDEOLAN", "ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(env_name)
        if root:
            candidates.append(root if env_name in ("VLC_DIR", "VIDEOLAN") else os.path.join(root, "VideoLAN", "VLC"))
    return [path for path in candidates if path and os.path.isdir(path)]


def configure_vlc_env():
    """Make libVLC discoverable for both bundled and system-installed VLC.

    Fresh Windows installs often have VLC in Program Files but not on PATH, so
    python-vlc can import while libvlc.dll/plugin discovery still fails later.
    """
    for vlc_dir in _vlc_candidate_dirs():
        plugin_path = os.path.join(vlc_dir, "plugins")
        if os.path.isdir(plugin_path):
            os.environ.setdefault("VLC_PLUGIN_PATH", plugin_path)
        path_parts = os.environ.get("PATH", "").split(os.pathsep)
        if vlc_dir not in path_parts:
            os.environ["PATH"] = vlc_dir + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            try:
                _DLL_DIR_HANDLES.append(os.add_dll_directory(vlc_dir))
            except Exception:
                pass
        if os.path.exists(os.path.join(vlc_dir, "libvlc.dll")):
            return
