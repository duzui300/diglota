# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for ``Uninstall.exe``, which ships inside the app folder.

Small on purpose: no engine, no window of the reader's, just the removal window
and the standard library. The installer copies this exe into the app folder and
points the Apps list at it.
"""

from pathlib import Path

HERE = Path(SPECPATH).resolve()          # packages/win
ICON = HERE / "assets" / "diglota.ico"

a = Analysis(
    [str(HERE / "uninstaller.py")],
    pathex=[str(HERE)],
    binaries=[],
    datas=[(str(ICON), "assets")],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=["app", "webview", "uvicorn", "fastapi", "starlette", "pytest",
              "PIL", "numpy", "typesafe_sdk"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Uninstall",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ICON),
)
