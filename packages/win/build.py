"""Freeze the desktop app into a folder, and optionally into a Windows installer.

    python packages/win/build.py               # packages/win/dist/Diglota/
    python packages/win/build.py --installer   # ... and Diglota-Setup-1.0.1.exe

The folder is portable: copy it anywhere, double-click ``Diglota.exe``. The
reader's own ``data/`` is created beside the executable on first run -- which is
also why the build never writes into it and why installing over a folder does
not touch it.

``--installer`` also builds ``Uninstall.exe`` (small, no engine), copies it into
the app folder, zips that folder into a payload, and wraps the payload in a
single setup exe. ``--installer-only`` repeats just that last part, which is
what you want while working on the setup window.

The icon is regenerated first if it is missing, so a clean checkout builds
without an extra step.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
ICON = HERE / "assets" / "diglota.ico"
DIST = HERE / "dist" / "Diglota"
SETUP_WORK = HERE / "build"
PAYLOAD = SETUP_WORK / "payload.zip"

sys.path.insert(0, str(HERE))
from setup_common import UNINSTALL_EXE, VERSION          # noqa: E402


def run(*args: str) -> None:
    print("+", " ".join(args))
    subprocess.run(args, cwd=HERE, check=True)


def build_app() -> None:
    if not ICON.is_file():
        run(sys.executable, str(HERE / "make_icon.py"))

    run(sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
        str(HERE / "diglota.spec"))

    # Things a person needs next to the executable, not inside the bundle: their
    # own corpus shelf (the sample passage), the licence, the README, and the
    # example .env. `data/` is deliberately absent -- it is theirs from first run.
    for name, source in (
        ("corpus", ROOT / "corpus"),
        ("README.md", ROOT / "README.md"),
        ("LICENSE", ROOT / "LICENSE"),
        (".env.example", ROOT / ".env.example"),
    ):
        destination = DIST / name
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        elif source.is_file():
            shutil.copy2(source, destination)

    exe = DIST / "Diglota.exe"
    if not exe.is_file():
        raise SystemExit("build finished without an executable -- inspect the PyInstaller output above")
    size = folder_size(DIST)
    print(f"\nbuilt {exe}")
    print(f"      {size / 1e6:.1f} MB across {file_count(DIST)} files")
    print("      double-click it, or copy the whole folder somewhere else first")


def build_uninstaller() -> Path:
    run(sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
        "--distpath", str(SETUP_WORK / "uninstaller-dist"),
        str(HERE / "uninstaller.spec"))
    produced = SETUP_WORK / "uninstaller-dist" / "Uninstall.exe"
    if not produced.is_file():
        raise SystemExit("the uninstaller build produced no exe")
    shutil.copy2(produced, DIST / UNINSTALL_EXE)
    return produced


def make_payload() -> Path:
    SETUP_WORK.mkdir(parents=True, exist_ok=True)
    PAYLOAD.unlink(missing_ok=True)
    with zipfile.ZipFile(PAYLOAD, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(DIST.rglob("*")):
            relative = path.relative_to(DIST)
            # A build folder that has been run once contains the reader's own
            # library. It is never part of what ships.
            if not path.is_file() or relative.parts[0] == "data":
                continue
            archive.write(path, relative.as_posix())
    return PAYLOAD


def build_installer() -> None:
    if not (DIST / "Diglota.exe").is_file():
        raise SystemExit("no app folder to install -- run without --installer-only first")

    build_uninstaller()
    payload = make_payload()
    print(f"\npayload {payload} ({payload.stat().st_size / 1e6:.1f} MB)")

    run(sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
        "--distpath", str(HERE / "dist"), str(HERE / "installer.spec"))
    setup = HERE / "dist" / f"Diglota-Setup-{VERSION}.exe"
    if not setup.is_file():
        raise SystemExit("the installer build produced no exe")
    print(f"\nbuilt {setup}")
    print(f"      {setup.stat().st_size / 1e6:.1f} MB, one file")
    print("      run it from the folder Diglota should live in")


def folder_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def file_count(path: Path) -> int:
    return sum(1 for item in path.rglob("*") if item.is_file())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Windows desktop app.")
    parser.add_argument("--installer", action="store_true",
                        help="also produce Diglota-Setup-<version>.exe")
    parser.add_argument("--installer-only", action="store_true",
                        help="rebuild only the setup exe from the existing app folder")
    args = parser.parse_args(argv)

    if not args.installer_only:
        build_app()
    if args.installer or args.installer_only:
        build_installer()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
