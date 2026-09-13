# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules


block_cipher = None

hiddenimports = ["vlc"]
hiddenimports += collect_submodules("soundfile")
hiddenimports += collect_submodules("miniaudio")
# Built-in video playback (Qt Multimedia) -- PyInstaller's PyQt6 hook
# normally picks these up automatically once imported, but naming them
# explicitly guards against the hook missing the multimedia submodules and
# their Qt FFmpeg backend plugin on a version where auto-detection changes.
hiddenimports += ["PyQt6.QtMultimedia", "PyQt6.QtMultimediaWidgets"]

a = Analysis(
    ["Main.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("assets", "assets"),
        (
            "billsmusic/assets/bills_music_splash.png",
            "billsmusic/assets",
        ),
        ("vendor/bass", "vendor/bass"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Bills Music Player",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/app_icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Bills Music Player",
)

