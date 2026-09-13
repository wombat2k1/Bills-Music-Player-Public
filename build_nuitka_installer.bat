@echo off
setlocal
cd /d "%~dp0"

set "APP_VERSION=1.1.0"
set "NUITKA_CACHE_DIR=%CD%\.codex-build\nuitka-cache"
set "PYTHON_EXE=.venv\Scripts\python.exe"
if defined BILLSMUSIC_PYTHON set "PYTHON_EXE=%BILLSMUSIC_PYTHON%"
rem Nuitka's pyqt6 plugin does not auto-include the multimedia plugin
rem subdirectory (only iconengines/imageformats/platforms/styles/tls), so
rem QMediaPlayer has no decoder backend at runtime unless this is included
rem explicitly. Override PYQT6_PLUGIN_DIR if not using the project .venv.
set "PYQT6_PLUGIN_DIR=.venv\Lib\site-packages\PyQt6\Qt6\plugins"
if defined BILLSMUSIC_PYQT6_PLUGIN_DIR set "PYQT6_PLUGIN_DIR=%BILLSMUSIC_PYQT6_PLUGIN_DIR%"
rem ffmpegmediaplugin.dll itself gets included above, but it loads these 5
rem FFmpeg shared libraries dynamically at runtime (not via its PE import
rem table), so Nuitka's static dependency walker never follows them --
rem without them present, Qt Multimedia silently falls back to
rem windowsmediaplugin.dll, which has weaker codec support and fails on
rem otherwise very ordinary H.264/AAC .mp4 files with "Unsupported media, a
rem codec is missing" (confirmed 2026-08-06: identical files that play fine
rem via QMediaPlayer from source failed this way in the packaged build; the
rem venv's own Qt6\bin has these 5 DLLs, the Nuitka dist had none of them).
rem Placed at the dist root, matching where Nuitka already puts every other
rem Qt6*.dll -- the main exe's own directory is always on the Windows DLL
rem search path, unlike plugins\multimedia\ or PATH.
set "PYQT6_BIN_DIR=.venv\Lib\site-packages\PyQt6\Qt6\bin"
if defined BILLSMUSIC_PYQT6_BIN_DIR set "PYQT6_BIN_DIR=%BILLSMUSIC_PYQT6_BIN_DIR%"
rem Phase 2A GPU dual-video compositor (video_subprocess.py's
rem GpuDualDeckVideoSubprocessController / --dual-deck-gpu / --probe-only)
rem loads assets\video_dual_deck.qml at runtime via a QQmlApplicationEngine.
rem Like the multimedia plugin above, none of this is discovered by
rem Nuitka's static dependency walker just because --enable-plugin=pyqt6 and
rem --include-package=PyQt6.QtQml/QtQuick are given below: the walker only
rem follows a compiled extension's own PE import table, and every DLL below
rem is loaded by *Qt's own* runtime plugin/QML-import resolution instead
rem (qtquick2plugin.dll, quickwindowplugin.dll and quickmultimediaplugin.dll
rem are never referenced from any Python-visible import at all -- they are
rem found by the QML engine at "import QtQuick"/"import QtQuick.Window"/
rem "import QtMultimedia" resolution time, the exact same class of gap
rem ffmpegmediaplugin.dll already has above). Verified against the actual
rem standalone dist, not assumed correct from this reasoning alone -- see
rem CODEX_HANDOFF.md's "Phase 2A" section for the verification record and
rem what (if anything) needed adjusting on the first attempt.
set "PYQT6_QML_DIR=.venv\Lib\site-packages\PyQt6\Qt6\qml"
if defined BILLSMUSIC_PYQT6_QML_DIR set "PYQT6_QML_DIR=%BILLSMUSIC_PYQT6_QML_DIR%"

"%PYTHON_EXE%" -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo The project Python environment is unavailable.
    exit /b 1
)

rem Immutable build identity (2026-08-31 Codex audit, section 16): must run
rem immediately before Nuitka, every build, so billsmusic/_build_info.py
rem always reflects the exact commit/tree this specific compile is from --
rem never at normal frozen application startup (see generate_build_info.py
rem and window.py's _record_build_identity()).
echo Generating build identity...
"%PYTHON_EXE%" generate_build_info.py
if errorlevel 1 exit /b 1

echo Building Bills Music Player with Nuitka...
"%PYTHON_EXE%" -m nuitka ^
  --mode=standalone ^
  --assume-yes-for-downloads ^
  --prefer-source-code ^
  --enable-plugin=pyqt6 ^
  --include-package=PyQt6.QtMultimedia ^
  --include-package=PyQt6.QtMultimediaWidgets ^
  --include-data-files="%PYQT6_PLUGIN_DIR%\multimedia\ffmpegmediaplugin.dll"=PyQt6\Qt6\plugins\multimedia\ffmpegmediaplugin.dll ^
  --include-data-files="%PYQT6_PLUGIN_DIR%\multimedia\windowsmediaplugin.dll"=PyQt6\Qt6\plugins\multimedia\windowsmediaplugin.dll ^
  --include-data-files="%PYQT6_BIN_DIR%\avcodec-61.dll"=avcodec-61.dll ^
  --include-data-files="%PYQT6_BIN_DIR%\avformat-61.dll"=avformat-61.dll ^
  --include-data-files="%PYQT6_BIN_DIR%\avutil-59.dll"=avutil-59.dll ^
  --include-data-files="%PYQT6_BIN_DIR%\swresample-5.dll"=swresample-5.dll ^
  --include-data-files="%PYQT6_BIN_DIR%\swscale-8.dll"=swscale-8.dll ^
  --include-package=PyQt6.QtQml ^
  --include-package=PyQt6.QtQuick ^
  --include-qt-plugins=qml ^
  --include-data-files="%PYQT6_BIN_DIR%\Qt6Qml.dll"=Qt6Qml.dll ^
  --include-data-files="%PYQT6_BIN_DIR%\Qt6QmlMeta.dll"=Qt6QmlMeta.dll ^
  --include-data-files="%PYQT6_BIN_DIR%\Qt6QmlModels.dll"=Qt6QmlModels.dll ^
  --include-data-files="%PYQT6_BIN_DIR%\Qt6QmlWorkerScript.dll"=Qt6QmlWorkerScript.dll ^
  --include-data-files="%PYQT6_BIN_DIR%\Qt6Quick.dll"=Qt6Quick.dll ^
  --include-data-files="%PYQT6_BIN_DIR%\Qt6MultimediaQuick.dll"=Qt6MultimediaQuick.dll ^
  --include-data-dir="%PYQT6_QML_DIR%\QtQml"=PyQt6\Qt6\qml\QtQml ^
  --include-data-dir="%PYQT6_QML_DIR%\QtQuick"=PyQt6\Qt6\qml\QtQuick ^
  --include-data-dir="%PYQT6_QML_DIR%\QtMultimedia"=PyQt6\Qt6\qml\QtMultimedia ^
  --windows-console-mode=disable ^
  --windows-icon-from-ico=assets\app_icon.ico ^
  --output-dir=nuitka-castfix-dist ^
  --output-filename="Bills Music Player.exe" ^
  --include-data-dir=assets=assets ^
  --include-data-dir=vendor\bass=vendor\bass ^
  --include-data-files=vendor\bass\bin\x64\bass.dll=vendor\bass\bin\x64\bass.dll ^
  --include-data-files=vendor\bass\bin\x64\bassflac.dll=vendor\bass\bin\x64\bassflac.dll ^
  --include-data-files=vendor\bass\bin\x64\bassmix.dll=vendor\bass\bin\x64\bassmix.dll ^
  --include-data-file=billsmusic\assets\bills_music_splash.png=billsmusic\assets\bills_music_splash.png ^
  --include-package=pychromecast ^
  --include-package=zeroconf ^
  --include-package=casttube ^
  --include-package-data=certifi ^
  --include-package=requests ^
  --include-package=urllib3 ^
  --include-package=idna ^
  --include-package=charset_normalizer ^
  --nofollow-import-to=pytest ^
  Main.py
if errorlevel 1 exit /b 1

if not exist "nuitka-castfix-dist\Main.dist\Bills Music Player.exe" exit /b 1

rem RecordUpdateListener crosses the Nuitka/Cython boundary, so this one
rem accelerator must use the embedded Python implementation. Other Zeroconf
rem accelerators remain because Nuitka links to them directly.
del /q "nuitka-castfix-dist\Main.dist\zeroconf\_updates.pyd" 2>nul

rem Set to skip the installer step and stop after the standalone dist --
rem useful for verifying the standalone build (e.g. a new Qt module's
rem packaging) before spending the extra time building/signing the
rem installer around a dist that might still need another pass.
if defined BILLSMUSIC_SKIP_INSTALLER exit /b 0

set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC exit /b 1
rem v1.0.71 correction: BILLSMUSIC_INSTALLER_SUFFIX (e.g. "-r2") produces a
rem distinguishably-named installer for an interim acceptance-retest build
rem that intentionally does not bump APP_VERSION, without overwriting the
rem normal-name installer from a prior build -- unset for every ordinary
rem build, so the filename is unchanged by default.
set "ISCC_DEFINES="
if defined BILLSMUSIC_INSTALLER_SUFFIX set "ISCC_DEFINES=/DMyBuildSuffix=%BILLSMUSIC_INSTALLER_SUFFIX%"
"%ISCC%" %ISCC_DEFINES% "setup-nuitka.iss"
