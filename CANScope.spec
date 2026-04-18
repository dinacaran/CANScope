# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import os

# PyInstaller executes spec files without defining __file__.
# Resolve the project root from the current working directory, which is
# expected to be the project root when build.bat runs pyinstaller.
project_root = Path(os.getcwd()).resolve()
icon_file = project_root / "resources" / "app_icon.ico"

datas = []
if icon_file.exists():
    datas.append((str(icon_file), "resources"))

hiddenimports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "pyqtgraph",
    "can.io.blf",
    "can.io.asc",
    "asammdf",
    "cantools.database",
]

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
    icon=str(icon_file) if icon_file.exists() else None,
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
