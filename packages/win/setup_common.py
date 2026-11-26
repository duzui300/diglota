"""The part of installing that has nothing to do with a window.

Kept out of ``diglota_win`` on purpose: the shell imports the engine and
pywebview, while the setup programs must start on a machine that has never run
the app and has no Python of its own. Everything here is standard library.

Three rules run through the file, and they are what make an install safe to run
twice:

* **The reader's ``data/`` is never written.** The payload has no ``data/`` in
  it; the extractor refuses one if it ever appears.
* **Lessons already on the shelf are not overwritten.** A reinstall refreshes
  the program and leaves every ``corpus/*.md`` the reader has touched alone.
* **A payload entry cannot escape the install folder.** Zip names are checked
  against the resolved destination, so ``../`` in an archive is refused rather
  than written somewhere else on disk.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

APP_NAME = "Diglota"
APP_EXE = "Diglota.exe"
UNINSTALL_EXE = "Uninstall.exe"
WINDOW_TITLE = "Diglota"            # the running reader's title, to find it again
VERSION = "1.1.0"
PUBLISHER = "Diglota"
HELP_URL = "https://github.com/duzui300/diglota"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Diglota"

# What an uninstall removes when it is asked to keep the library. Anything else
# in the folder -- data/, corpus/, a notes file of their own -- is left there.
PROGRAM_ENTRIES = (APP_EXE, UNINSTALL_EXE, "_internal", "README.md", "LICENSE", ".env.example")

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class SetupError(RuntimeError):
    """A reason a person can read, shown as-is in the window."""


def attach_console() -> None:
    """A windowed exe has no stdout. When it was started from a terminal, borrow one.

    Without this, ``--silent`` cannot tell a script what happened: the frozen
    build is linked as a GUI program, and ``sys.stdout`` is ``None`` until
    something gives it a console to write to.
    """
    if sys.platform != "win32" or sys.stdout is not None:
        return
    try:
        if not ctypes.windll.kernel32.AttachConsole(ctypes.c_uint32(-1).value):
            return
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    except (OSError, ValueError):
        pass


def say(text: str, *, error: bool = False) -> None:
    """Print when there is somewhere to print to; never raise when there is not."""
    stream = sys.stderr if error else sys.stdout
    if stream is None:
        return
    try:
        print(text, file=stream)
    except (OSError, ValueError):
        pass


# -- where things live ---------------------------------------------------- #

def default_install_dir() -> Path:
    """The folder the setup program is in: it installs beside itself.

    The same rule the app already uses for ``data/``, taken one step earlier.
    Put the setup exe where Diglota should live, run it there, and the program
    and the reader's library end up in one folder that can be moved as a unit.
    When running from a checkout there is no setup exe to ask, so the current
    directory stands in for it.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def bundled_payload() -> Path:
    """The zip the setup exe carries. In a checkout, the one build.py left behind."""
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        base = Path(frozen)
        # PyInstaller 6 puts bundled files under ``_internal`` in a one-file
        # build; earlier layouts put them at the root. Either one is fine.
        for candidate in (base / "payload.zip", base / "_internal" / "payload.zip"):
            if candidate.is_file():
                return candidate
        return base / "payload.zip"
    return Path(__file__).resolve().parent / "build" / "payload.zip"


def saved_theme(install_dir: Path) -> str:
    """The theme the reader last chose, so setup and removal look like the app."""
    try:
        data = json.loads((Path(install_dir) / "data" / "ui.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "paper"
    theme = data.get("theme")
    return theme if theme in ("paper", "sepia", "night") else "paper"


def format_size(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# -- shortcuts ------------------------------------------------------------ #

class _GUID(ctypes.Structure):
    _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16),
                ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]


_FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"
_FOLDERID_PROGRAMS = "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}"


def _guid(text: str) -> _GUID:
    import uuid

    value = uuid.UUID(text)
    guid = _GUID()
    guid.data1, guid.data2, guid.data3 = value.time_low, value.time_mid, value.time_hi_version
    guid.data4[:] = value.bytes[8:]
    return guid


def _known_folder(folder_id: str) -> Path | None:
    """A Windows special folder, resolved properly -- the Desktop may be in OneDrive."""
    if sys.platform != "win32":
        return None
    try:
        guid = _guid(folder_id)
        out = ctypes.c_wchar_p()
        result = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(out))
        if result != 0 or not out.value:
            return None
        path = Path(out.value)
        ctypes.windll.ole32.CoTaskMemFree(ctypes.cast(out, ctypes.c_void_p))
        return path
    except (AttributeError, OSError, ValueError):
        return None


def start_menu_dir() -> Path:
    return _known_folder(_FOLDERID_PROGRAMS) or (
        Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        / "Microsoft" / "Windows" / "Start Menu" / "Programs")


def desktop_dir() -> Path:
    return _known_folder(_FOLDERID_DESKTOP) or (Path.home() / "Desktop")


def start_menu_shortcut() -> Path:
    return start_menu_dir() / f"{APP_NAME}.lnk"


def desktop_shortcut() -> Path:
    return desktop_dir() / f"{APP_NAME}.lnk"


def _powershell_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def create_shortcut(link: Path, target: Path, *, description: str = APP_NAME) -> bool:
    """Create a .lnk through the shell's own COM object.

    PowerShell, not pythoncom: the setup programs deliberately have no
    third-party dependencies. A shortcut is a convenience, never a reason to
    fail an install, so this reports success rather than raising.
    """
    if sys.platform != "win32":
        return False
    link.parent.mkdir(parents=True, exist_ok=True)
    command = (
        "$ErrorActionPreference = 'Stop'; "
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_powershell_quote(link)}); "
        f"$s.TargetPath = {_powershell_quote(target)}; "
        f"$s.WorkingDirectory = {_powershell_quote(Path(target).parent)}; "
        f"$s.IconLocation = {_powershell_quote(str(target) + ',0')}; "
        f"$s.Description = {_powershell_quote(description)}; "
        "$s.Save()"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and link.is_file()


def remove_shortcuts() -> None:
    for link in (start_menu_shortcut(), desktop_shortcut()):
        try:
            link.unlink(missing_ok=True)
        except OSError:
            pass


# -- Apps & features ------------------------------------------------------ #

def uninstall_values(install_dir: Path, size_bytes: int = 0) -> dict[str, object]:
    install_dir = Path(install_dir)
    exe = install_dir / APP_EXE
    entry: dict[str, object] = {
        "DisplayName": APP_NAME,
        "DisplayVersion": VERSION,
        "Publisher": PUBLISHER,
        "InstallLocation": str(install_dir),
        "DisplayIcon": str(exe),
        "UninstallString": f'"{install_dir / UNINSTALL_EXE}"',
        "QuietUninstallString": f'"{install_dir / UNINSTALL_EXE}" --silent',
        "HelpLink": HELP_URL,
        "NoModify": 1,
        "NoRepair": 1,
    }
    if size_bytes:
        entry["EstimatedSize"] = max(1, int(size_bytes / 1024))
    return entry


def write_uninstall_entry(install_dir: Path, size_bytes: int = 0) -> None:
    if sys.platform != "win32":
        return
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        for name, value in uninstall_values(install_dir, size_bytes).items():
            kind = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
            winreg.SetValueEx(key, name, 0, kind, value)


def registered_install_dir() -> Path | None:
    """Where the Apps list says this copy lives, if it says anything."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, "InstallLocation")
    except (FileNotFoundError, OSError, ValueError):
        return None
    return Path(value) if value else None


def remove_uninstall_entry() -> None:
    if sys.platform != "win32":
        return
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except FileNotFoundError:
        pass


# -- the running reader --------------------------------------------------- #

def _find_window(title: str) -> int | None:
    """The first top-level window with exactly this title, whatever owns it."""
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    user32.EnumWindows.argtypes = [
        ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    found: list[int] = []

    def visit(handle: int, _param: int) -> bool:
        text = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(ctypes.c_void_p(handle), text, 512)
        if text.value == title:
            found.append(int(handle))
            return False
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(visit), None)
    return found[0] if found else None


def _process_image(handle: int) -> str:
    """The executable behind a window, or an empty string if it cannot be read."""
    if sys.platform != "win32":
        return ""
    user32 = ctypes.windll.user32
    kernel = ctypes.windll.kernel32
    user32.GetWindowThreadProcessId.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD
    kernel.OpenProcess.argtypes = [
        ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = ctypes.wintypes.BOOL
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]

    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(ctypes.c_void_p(handle), ctypes.byref(pid))
    process = kernel.OpenProcess(0x1000, False, pid.value)   # QUERY_LIMITED_INFORMATION
    if not process:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = ctypes.wintypes.DWORD(len(buffer))
        if not kernel.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value
    finally:
        kernel.CloseHandle(process)


def _is_app_executable(path: str) -> bool:
    return Path(path).name.casefold() == APP_EXE.casefold()


def _find_app_window() -> int | None:
    """A window titled Diglota that actually belongs to Diglota.exe.

    A title is not evidence enough. Explorer's TabProxyWindow can carry a
    folder's name, and a browser window can be titled with the page's name.
    Those must not stop an install; only the reader's own process does.
    """
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    found: list[int] = []

    def visit(handle: int, _param: int) -> bool:
        text = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(ctypes.c_void_p(handle), text, 512)
        if text.value == WINDOW_TITLE and _is_app_executable(_process_image(int(handle))):
            found.append(int(handle))
            return False
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(visit), None)
    return found[0] if found else None


def app_is_running() -> bool:
    return _find_app_window() is not None


def close_running_app(timeout: float = 8.0) -> bool:
    """Ask the reader's window to close and wait for it.

    Install and removal both replace files the running exe holds open, so the
    kind thing is to close it gracefully -- the app saves its place on the way
    out -- instead of telling the person to go and do it themselves.
    """
    if sys.platform != "win32":
        return True
    import time

    handle = _find_app_window()
    if not handle:
        return True
    ctypes.windll.user32.PostMessageW(ctypes.c_void_p(handle), 0x0010, 0, 0)   # WM_CLOSE
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not app_is_running():
            return True
        time.sleep(0.15)
    return not app_is_running()


# -- installing ----------------------------------------------------------- #

@dataclass
class InstallReport:
    path: Path
    files: int
    kept: int
    size: int
    existed: bool


def _member_target(root: Path, member: str) -> Path | None:
    name = member.replace("\\", "/").lstrip("/")
    if not name:
        return None
    # A drive letter or absolute path in a zip is never something this payload
    # produces, and resolving it would land outside the folder we promised.
    if ":" in name.split("/", 1)[0]:
        raise SetupError(f"The installer contains an unsafe path: {member}")
    target = (root / name).resolve()
    if not target.is_relative_to(root):
        raise SetupError(f"The installer contains an unsafe path: {member}")
    return target


def install_from_payload(
    payload: Path,
    install_dir: Path,
    progress: Callable[[int, int, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> InstallReport:
    """Unpack the app into ``install_dir`` without disturbing the reader's shelf."""
    payload = Path(payload)
    if not payload.is_file():
        raise SetupError("The installer is incomplete: its payload is missing.")
    install_dir = Path(install_dir)
    existed = (install_dir / APP_EXE).is_file() or (install_dir / "data").is_dir()

    try:
        install_dir.mkdir(parents=True, exist_ok=True)
        root = install_dir.resolve()
        with zipfile.ZipFile(payload) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            total = len(members)
            written = kept = size = 0
            for index, item in enumerate(members, 1):
                if should_stop and should_stop():
                    raise SetupError("The install was cancelled.")
                target = _member_target(root, item.filename)
                if target is None:
                    continue
                parts = target.relative_to(root).parts
                if parts and parts[0] == "data":
                    kept += 1                 # never, by rule
                    continue
                if parts and parts[0] == "corpus" and target.exists():
                    kept += 1                 # their lessons, not ours to replace
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source, open(target, "wb") as destination:
                    shutil.copyfileobj(source, destination)
                written += 1
                size += item.file_size
                if progress:
                    progress(index, total, target.name)
    except zipfile.BadZipFile as exc:
        raise SetupError("The installer's payload is damaged. Download it again.") from exc
    except OSError as exc:
        raise SetupError(f"Could not write to {install_dir}:\n{exc.strerror or exc}") from exc

    if not (install_dir / APP_EXE).is_file():
        raise SetupError("The install finished without Diglota.exe. The payload is incomplete.")
    return InstallReport(path=install_dir, files=written, kept=kept, size=size, existed=existed)


def folder_size(path: Path) -> int:
    return sum(item.stat().st_size for item in Path(path).rglob("*") if item.is_file())


# -- removing ------------------------------------------------------------- #

@dataclass
class RemovalReport:
    path: Path
    kept_library: bool
    removed: int


def _force_remove(function: Callable[[str], None], path: str, _error: object) -> None:
    """Retry a failed delete once without the read-only bit (Windows sets it)."""
    try:
        os.chmod(path, 0o700)
        function(path)
    except OSError:
        pass


def remove_install(install_dir: Path, keep_library: bool = True) -> RemovalReport:
    install_dir = Path(install_dir)
    if not install_dir.exists():
        return RemovalReport(path=install_dir, kept_library=keep_library, removed=0)
    if not (install_dir / APP_EXE).is_file() and keep_library:
        raise SetupError(f"{install_dir} does not look like a Diglota install.")

    if not keep_library:
        shutil.rmtree(install_dir, onerror=_force_remove)
        return RemovalReport(path=install_dir, kept_library=False, removed=1)

    removed = 0
    for name in PROGRAM_ENTRIES:
        entry = install_dir / name
        try:
            if entry.is_dir():
                shutil.rmtree(entry, onerror=_force_remove)
            elif entry.exists():
                entry.unlink()
            else:
                continue
            removed += 1
        except OSError:
            continue
    # If nothing of theirs was left behind, do not leave an empty folder either.
    try:
        if install_dir.is_dir() and not any(install_dir.iterdir()):
            install_dir.rmdir()
    except OSError:
        pass
    return RemovalReport(path=install_dir, kept_library=True, removed=removed)


def delete_self_later(path: Path, delay_seconds: int = 2,
                      remove_parent_if_empty: bool = False) -> None:
    """Remove a file after the program holding it closes; Windows locks a running exe.

    The delay is the point: the process that owns the file is exiting, and the
    deletion has to happen after it does. ``rmdir`` without ``/s`` only succeeds
    on an empty folder, which is exactly the condition worth acting on.
    """
    if sys.platform != "win32":
        return
    seconds = max(1, int(delay_seconds))
    # PowerShell, not cmd: a path with spaces and an ampersand is a quoting
    # problem in a cmd one-liner, and the setup program already uses PowerShell
    # for shortcuts, so this adds no new dependency.
    script = (f"Start-Sleep -Seconds {seconds}; "
              f"Remove-Item -LiteralPath {_powershell_quote(path)} -Force "
              "-ErrorAction SilentlyContinue")
    if remove_parent_if_empty:
        parent = _powershell_quote(Path(path).parent)
        script += (f"; if ((Test-Path -LiteralPath {parent}) -and "
                   f"(-not (Get-ChildItem -LiteralPath {parent} -Force | Select-Object -First 1))) "
                   f"{{ Remove-Item -LiteralPath {parent} -Force -ErrorAction SilentlyContinue }}")
    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            creationflags=_NO_WINDOW, close_fds=True)
    except OSError:
        pass
