# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the desktop app.

Run through packages/win/build.py, which copies corpus/ beside the finished
executable afterwards. Two path choices matter:

* ``packages/ui/static`` is bundled at ``packages/ui/static`` inside the bundle,
  because that is where config.UI_DIR looks for it (via sys._MEIPASS).
* ``sys.executable``'s folder -- not the bundle -- is where the app writes
  ``data/``, so the reader's library survives reinstalling the executable over it.
"""

from pathlib import Path

HERE = Path(SPECPATH).resolve()          # packages/win
ROOT = HERE.parents[1]                   # repository root
CORE = ROOT / "packages" / "core"
UI = ROOT / "packages" / "ui" / "static"

# Uvicorn picks its loop, its HTTP implementation and its lifespan manager by
# import at runtime, so none of them appear in a static import graph.
UVICORN = [
    "uvicorn.logging",
    "uvicorn.loops", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols", "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan", "uvicorn.lifespan.on", "uvicorn.lifespan.off",
]

a = Analysis(
    [str(HERE / "diglota_win" / "__main__.py")],
    pathex=[str(HERE), str(CORE)],
    binaries=[],
    datas=[
        (str(HERE / "diglota_win" / "splash.html"), "diglota_win"),
        (str(HERE / "assets" / "diglota.ico"), "assets"),
        (str(UI), "packages/ui/static"),
    ],
    hiddenimports=UVICORN + ["typesafe_sdk"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "PIL"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Diglota",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                      # no console window: it is a desktop app
    disable_windowed_traceback=False,
    icon=str(HERE / "assets" / "diglota.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Diglota",
)
