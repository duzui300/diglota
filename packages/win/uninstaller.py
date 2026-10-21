"""The uninstaller that ships inside the install folder as ``Uninstall.exe``.

Windows locks a running executable, so this program does not remove itself in
place: it copies itself to ``%TEMP%``, the copy does the work, and the copy
deletes itself once its window has closed. The same path makes the folder safe
to delete entirely on a "remove everything" uninstall.

The default is to keep the reader's library. Their lessons, review history and
place in every text live in ``data/`` and ``corpus/``; removing the program is
not a reason to throw those away. Deleting them is a separate, explicit choice.
"""

from __future__ import annotations

import argparse
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path

import setup_common as common
import setup_ui as ui

WINDOW_WIDTH = 680
WINDOW_HEIGHT = 470
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class UninstallerApp:
    def __init__(self, root: tk.Tk, target: Path, delete_library: bool = False) -> None:
        self.root = root
        self.target = Path(target)
        self.delete_library = delete_library
        self.kit = ui.Kit(common.saved_theme(self.target))

        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.kept = not delete_library

        root.title("Remove Diglota")
        root.resizable(False, False)
        root.configure(bg=self.kit.colors["page"])
        ui.apply_icon(root)
        ui.center_on_screen(root, self.kit.px(WINDOW_WIDTH), self.kit.px(WINDOW_HEIGHT))
        root.protocol("WM_DELETE_WINDOW", self._close)
        root.bind("<Escape>", lambda _event: self._close())

        self._chrome()
        self.show_confirm()

    # -- shared with the installer, deliberately -------------------------- #

    def _chrome(self) -> None:
        kit, page, ink = self.kit, self.kit.colors["page"], self.kit.colors["ink"]
        tk.Label(self.root, text=common.APP_NAME, font=kit.font("serif", 26, "bold"),
                 bg=page, fg=ink).place(x=kit.px(44), y=kit.px(30), anchor="nw")
        tk.Label(self.root,
                 text=f"Version {common.VERSION}. Free software under the AGPL-3.0.",
                 font=kit.font("sans", 11), bg=page,
                 fg=self.kit.colors["ink3"]).place(
            x=kit.px(44), y=kit.px(WINDOW_HEIGHT - 24), anchor="sw")
        self.body = tk.Frame(self.root, bg=page, bd=0, highlightthickness=0)
        self.body.place(x=kit.px(44), y=kit.px(90),
                        width=kit.px(WINDOW_WIDTH - 88), height=kit.px(280))
        self.actions = tk.Frame(self.root, bg=page, bd=0, highlightthickness=0)
        self.actions.place(x=kit.px(44), y=kit.px(382),
                           width=kit.px(WINDOW_WIDTH - 88), height=kit.px(44))

    def _clear(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        for child in self.actions.winfo_children():
            child.destroy()

    def _label(self, text: str, *, family: str = "sans", size: int = 14,
               weight: str = "normal", fg: str | None = None, wrap: int = 566,
               x: int = 0, y: int = 0) -> tk.Label:
        label = tk.Label(self.body, text=text, font=self.kit.font(family, size, weight),
                         bg=self.kit.colors["page"],
                         fg=fg or self.kit.colors["ink"],
                         justify="left", anchor="nw", wraplength=self.kit.px(wrap))
        label.place(x=self.kit.px(x), y=self.kit.px(y), anchor="nw")
        return label

    def _actions(self, buttons: list[tuple[str, object, str]]) -> None:
        for text, command, kind in buttons:
            button = ui.Button(self.actions, self.kit, text, command, kind=kind)
            button.pack(side="right", padx=(self.kit.px(8), 0))
            if kind in ("primary", "destructive"):
                self.primary_button = button

    # -- states ------------------------------------------------------------ #

    def show_confirm(self) -> None:
        self._clear()
        kit = self.kit
        self._label("Remove Diglota?", family="serif", size=25, weight="bold", y=0)
        self._label(
            "The program will be removed from this computer. Your lessons, reviews "
            "and your place in every text stay in the folder below, so installing "
            "again later picks them up where you left off.",
            size=14, fg=kit.colors["ink2"], wrap=566, y=48)
        self._label(ui.fit_text(kit, str(self.target), kit.font("mono", 12),
                                kit.px(566)),
                    family="mono", size=12, wrap=566, y=124)

        check = ui.Check(self.body, kit, "Also delete my lessons and settings",
                         value=self.delete_library, command=self._set_delete_library)
        check.place(x=0, y=kit.px(160))

        self.warning = self._label(self._warning_text(), size=12,
                                   fg=kit.colors["ink2"], wrap=566, y=192)

        self._actions([("Remove", self.start_removal, "destructive"),
                       ("Cancel", self._close, "ghost")])
        self.primary_button.focus_set()

    def _warning_text(self) -> str:
        if self.delete_library:
            return ("The whole folder will be deleted, including your library. "
                    "This cannot be undone.")
        return "Nothing in that folder is touched."

    def _set_delete_library(self, value: bool) -> None:
        self.delete_library = value
        color = (ui.DESTRUCTIVE if value else self.kit.colors["ink2"])
        self.warning.configure(text=self._warning_text(), fg=color)

    def show_working(self) -> None:
        self._clear()
        self._label("Removing Diglota.", family="serif", size=23, weight="bold", y=0)
        self.detail = self._label("Closing the open reader", size=14,
                                  fg=self.kit.colors["ink2"], wrap=566, y=44)

    def show_done(self, kept: bool, existed: bool) -> None:
        self._clear()
        kit = self.kit
        self._label("Diglota was removed.", family="serif", size=25, weight="bold", y=0)
        if kept:
            self._label("Your lessons and settings are still in", size=14,
                        fg=kit.colors["ink2"], wrap=300, y=48)
            self._label(ui.fit_text(kit, str(self.target), kit.font("mono", 12),
                                    kit.px(566)),
                        family="mono", size=12, wrap=566, y=72)
            self._label("Installing again will find them.", size=14,
                        fg=kit.colors["ink2"], wrap=566, y=116)
        else:
            self._label("The program and everything in its folder were deleted.",
                        size=14, fg=kit.colors["ink2"], wrap=566, y=48)
        if not existed:
            self._label("There was nothing to remove.", size=12,
                        fg=kit.colors["ink3"], y=160)
        self._actions([("Close", self._close, "primary")])
        self.primary_button.focus_set()

    def show_error(self, message: str) -> None:
        self._clear()
        self.busy = False
        kit = self.kit
        self._label("Diglota was not removed.", family="serif", size=23,
                    weight="bold", y=0)
        frame = tk.Frame(self.body, bg=kit.colors["surface"],
                         highlightbackground=kit.colors["line"],
                         highlightthickness=max(1, kit.px(1)))
        frame.place(x=0, y=kit.px(48), width=kit.px(566), height=kit.px(92))
        reason = tk.Text(frame, wrap="word", bd=0, highlightthickness=0,
                         bg=kit.colors["surface"], fg=kit.colors["ink"],
                         font=kit.font("mono", 11), padx=kit.px(12), pady=kit.px(9),
                         height=3)
        reason.insert("1.0", message)
        reason.configure(state="disabled")
        reason.pack(fill="both", expand=True)

        self._label("Close Diglota if it is open, then try again.",
                    size=12, fg=kit.colors["ink3"], wrap=566, y=154)
        self._actions([("Try again", self.show_confirm, "primary"),
                       ("Close", self._close, "ghost")])
        self.primary_button.focus_set()

    # -- work -------------------------------------------------------------- #

    def start_removal(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.kept = not self.delete_library
        self.show_working()
        threading.Thread(target=self._work, name="diglota-uninstall", daemon=True).start()
        self.root.after(80, self._poll)

    def _work(self) -> None:
        try:
            if common.app_is_running():
                self.events.put(("phase", "Closing the open reader"))
                if not common.close_running_app():
                    raise common.SetupError(
                        "Diglota is still open. Close its window, then try again.")
            self.events.put(("phase", "Removing the program files"))
            existed = (self.target / common.APP_EXE).is_file()
            common.remove_install(self.target, keep_library=self.kept)
            self.events.put(("phase", "Removing shortcuts"))
            common.remove_shortcuts()
            common.remove_uninstall_entry()
            self.events.put(("done", self.kept, existed))
        except common.SetupError as exc:
            self.events.put(("error", str(exc)))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("error", f"{type(exc).__name__}: {exc}"))

    def _poll(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "phase":
                    self.detail.configure(text=event[1])
                elif event[0] == "done":
                    self.busy = False
                    _finish_installed_copy(self.target, event[1])
                    self.show_done(event[1], event[2])
                elif event[0] == "error":
                    self.show_error(event[1])
        except queue.Empty:
            pass
        if self.busy:
            self.root.after(80, self._poll)

    def _close(self) -> None:
        if self.busy:
            return
        self.root.destroy()


# -- where it was installed ----------------------------------------------- #

def _guess_target() -> Path:
    exe_dir = Path(sys.executable).resolve().parent
    if (exe_dir / common.APP_EXE).is_file():
        return exe_dir
    registered = common.registered_install_dir()
    if registered:
        return registered
    return common.default_install_dir()


# -- running from %TEMP% -------------------------------------------------- #

def _temp_copy() -> Path | None:
    """Copy this exe out of the install folder so it can delete that folder."""
    if not getattr(sys, "frozen", False):
        return None
    destination = Path(tempfile.gettempdir()) / f"Diglota-Uninstall-{os.getpid()}.exe"
    try:
        shutil.copy2(sys.executable, destination)
        return destination
    except OSError:
        return None


def _is_temp_copy() -> bool:
    return Path(sys.executable).name.startswith("Diglota-Uninstall-")


def _relaunch_from_temp(target: Path, silent: bool, keep: bool) -> int | None:
    if not getattr(sys, "frozen", False) or _is_temp_copy():
        return None
    copy = _temp_copy()
    if copy is None:
        return None          # in-place removal still works; the exe itself may remain
    child = [str(copy), "--from-temp", "--target", str(target)]
    child.append("--silent" if silent else "--ui")
    child.append("--keep-library" if keep else "--delete-library")
    if silent:
        try:
            result = subprocess.run(child, creationflags=_NO_WINDOW,
                                    capture_output=True, text=True)
        except OSError:
            return 1
        # A windowed child's stdout is not the parent's terminal; relay it so a
        # script calling `Uninstall.exe --silent` still sees the outcome. The
        # parent may itself have no stdout at all (launched from Explorer).
        if result.stdout:
            common.say(result.stdout.rstrip("\n"))
        if result.stderr:
            common.say(result.stderr.rstrip("\n"), error=True)
        return result.returncode
    try:
        subprocess.Popen(child, close_fds=True)
    except OSError:
        return 1
    return 0


# -- silent mode ---------------------------------------------------------- #

def _finish_installed_copy(target: Path, keep: bool) -> None:
    """Delete the installed Uninstall.exe once the process holding it has exited.

    In a silent run the installed exe waits for this temp copy, so it still owns
    its own file when removal reaches it. It is gone a moment later; a delayed
    delete is what closes that gap.
    """
    if not _is_temp_copy():
        return
    installed = Path(target) / common.UNINSTALL_EXE
    if installed.exists():
        common.delete_self_later(installed, delay_seconds=3,
                                 remove_parent_if_empty=not keep)


def remove_silently(target: Path, keep: bool) -> int:
    try:
        if common.app_is_running() and not common.close_running_app():
            raise common.SetupError("Diglota is open; close it and run the uninstaller again.")
        common.remove_install(target, keep_library=keep)
        common.remove_shortcuts()
        common.remove_uninstall_entry()
    except common.SetupError as exc:
        common.say(f"Diglota was not removed: {exc}", error=True)
        return 1
    _finish_installed_copy(target, keep)
    where = "Your lessons and settings were kept." if keep else "Its folder was deleted."
    common.say(f"Diglota was removed. {where}")
    return 0


def main(argv: list[str] | None = None) -> int:
    common.attach_console()
    parser = argparse.ArgumentParser(prog="Remove Diglota")
    parser.add_argument("--target", default=None, help="the install folder")
    parser.add_argument("--silent", action="store_true")
    parser.add_argument("--ui", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--from-temp", action="store_true", help=argparse.SUPPRESS)
    library = parser.add_mutually_exclusive_group()
    library.add_argument("--keep-library", action="store_true",
                         help="keep lessons, reviews and settings (the default)")
    library.add_argument("--delete-library", action="store_true",
                         help="delete the reader's library as well")
    args = parser.parse_args(argv)

    target = Path(args.target) if args.target else _guess_target()
    keep = not args.delete_library

    code = _relaunch_from_temp(target, args.silent, keep)
    if code is not None:
        return code

    if args.silent:
        code = remove_silently(target, keep)
        if _is_temp_copy():
            common.delete_self_later(Path(sys.executable))
        return code

    ui.set_dpi_awareness()
    root = tk.Tk()
    app = UninstallerApp(root, target, delete_library=not keep)
    root.mainloop()

    # Only a copy we put in %TEMP% is ours to delete; the installed one may be
    # run directly and should stay where the Apps list points.
    if _is_temp_copy():
        common.delete_self_later(Path(sys.executable))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
