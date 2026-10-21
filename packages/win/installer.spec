# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for ``Diglota-Setup-<version>.exe``.

The app itself is not rebuilt here: ``build.py --installer`` builds the normal
folder first, adds the uninstaller to it, and zips that folder into
``build/Diglota-payload.zip``. This spec puts the zip inside a one-file exe next
to the setup program, so the exe people download is a single file.

The setup window is the only thing that needs tkinter; the reader is not in this
bundle at all, only as the zip it installs.
"""

import sys
from pathlib import Path

HERE = Path(SPECPATH).resolve()          # packages/win
sys.path.insert(0, str(HERE))
from setup_common import VERSION         # noqa: E402

PAYLOAD = HERE / "build" / "payload.zip"
ICON = HERE / "assets" / "diglota.ico"

a = Analysis(
    [str(HERE / "installer.py")],
    pathex=[str(HERE)],
    binaries=[],
    datas=[
        (str(PAYLOAD), "."),             # -> sys._MEIPASS/payload.zip
        (str(ICON), "assets"),
    ],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    # The setup program must not drag in the reader's stack by accident.
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
    name=f"Diglota-Setup-{VERSION}",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ICON),
)
