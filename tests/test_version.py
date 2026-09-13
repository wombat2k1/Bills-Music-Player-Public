import re
from pathlib import Path

from billsmusic import __version__


ROOT = Path(__file__).resolve().parents[1]


def _match(path, pattern):
    text = path.read_text(encoding="utf-8")
    match = re.search(pattern, text)
    assert match is not None
    return match.group(1)


def test_release_version_is_consistent_across_app_build_and_installer():
    build_version = _match(
        ROOT / "build_installer.bat",
        r'set "APP_VERSION=([^"]+)"',
    )
    installer_version = _match(
        ROOT / "setup.iss",
        r'#define MyAppVersion "([^"]+)"',
    )
    installer_file_version = _match(
        ROOT / "setup.iss",
        r'#define MyAppVersionInfo "([^"]+)"',
    )
    nuitka_build_version = _match(
        ROOT / "build_nuitka_installer.bat",
        r'set "APP_VERSION=([^"]+)"',
    )
    nuitka_installer_version = _match(
        ROOT / "setup-nuitka.iss",
        r'#define MyAppVersion "([^"]+)"',
    )

    assert (
        __version__
        == build_version
        == installer_version
        == nuitka_build_version
        == nuitka_installer_version
    )
    assert installer_file_version == f"{__version__}.0"
