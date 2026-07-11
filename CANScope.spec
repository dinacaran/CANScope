# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import os

from PyInstaller.utils.hooks import collect_submodules

# PyInstaller executes spec files without defining __file__.
# Resolve the project root from the current working directory, which is
# expected to be the project root when build.bat runs pyinstaller.
project_root = Path(os.getcwd()).resolve()
icon_file = project_root / "resources" / "app_icon.ico"

datas = []
if icon_file.exists():
    datas.append((str(icon_file), "resources"))

# Always bundle the splash screen image
splash_file = project_root / "resources" / "splashscreen.png"
if splash_file.exists():
    datas.append((str(splash_file), "resources"))

# Bundle app icon PNG for runtime window icon
icon_png = project_root / "resources" / "CANScope_ICON.png"
if icon_png.exists():
    datas.append((str(icon_png), "resources"))

# asammdf asks canmatrix to load database format handlers by module name at
# runtime. PyInstaller cannot discover those dynamic imports automatically;
# without them the packaged app abandons native one-pass MF4 extraction and
# falls back to the slow frame-by-frame reader.
canmatrix_format_imports = collect_submodules("canmatrix.formats")

hiddenimports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "pyqtgraph",
    "can.io.blf",
    "can.io.asc",
    "asammdf",
    "canmatrix",
    "cantools.database",
    "cantools.database.can.formats.arxml",
    "lxml",
] + canmatrix_format_imports

block_cipher = None


a = Analysis(
    [str(project_root / "app.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # diskcache is listed as a cantools dependency and is imported unconditionally
    # by cantools at startup. It must be bundled even though CAN Scope never
    # activates the cache (cache_dir is never passed to load_file()).
    # The Dependabot pickle-deserialization alert (CVE diskcache <=5.6.3) does
    # not apply here: no cache directory is ever created or read by this app.
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
    name="CANScope",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(project_root / 'resources' / 'app_icon.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="CANScope",
)
