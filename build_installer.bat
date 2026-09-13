@echo off
setlocal

cd /d "%~dp0"

set "APP_VERSION=1.1.0"
set "PYTHON_EXE=.venv\Scripts\python.exe"
if defined BILLSMUSIC_PYTHON set "PYTHON_EXE=%BILLSMUSIC_PYTHON%"

"%PYTHON_EXE%" -c "import sys" >nul 2>&1
if errorlevel 1 set "PYTHON_EXE=python"

"%PYTHON_EXE%" -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo.
    echo A working Python installation could not be found.
    echo Repair .venv or set BILLSMUSIC_PYTHON to a working Python executable.
    exit /b 1
)

findstr /C:"#define MyAppVersion \"%APP_VERSION%\"" "setup.iss" >nul
if errorlevel 1 (
    echo.
    echo Version mismatch: build_installer.bat expects %APP_VERSION%,
    echo but setup.iss does not contain the same MyAppVersion.
    echo Update both version values before building.
    exit /b 1
)

echo Building Bills Music Player with PyInstaller...
"%PYTHON_EXE%" -m PyInstaller --clean --noconfirm "Bills Music Player.spec"
if errorlevel 1 (
    echo.
    echo PyInstaller build failed.
    echo Python used: %PYTHON_EXE%
    exit /b 1
)

if not exist "dist\Bills Music Player\Bills Music Player.exe" (
    echo.
    echo Build finished, but dist\Bills Music Player\Bills Music Player.exe was not found.
    exit /b 1
)

if not exist "dist\Bills Music Player\_internal\vendor\bass\bin\x64\bass.dll" (
    echo.
    echo Build finished, but the packaged BASS DLL was not found.
    echo Expected:
    echo   dist\Bills Music Player\_internal\vendor\bass\bin\x64\bass.dll
    exit /b 1
)

echo.
echo Build complete:
echo   dist\Bills Music Player\Bills Music Player.exe
echo   dist\Bills Music Player\_internal\vendor\bass\bin\x64\bass.dll
echo.

set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"

if not defined ISCC (
    echo Inno Setup compiler was not found automatically.
    echo The app build is fresh, but the installer was NOT rebuilt.
    echo Install Inno Setup 6 or open setup.iss in Inno Setup and compile it manually.
    exit /b 1
)

echo Building installer with Inno Setup...
"%ISCC%" "setup.iss"
if errorlevel 1 (
    echo.
    echo Inno Setup build failed.
    exit /b 1
)

echo.
echo Installer complete:
echo   installer\BillsMusicPlayerSetup-%APP_VERSION%.exe
