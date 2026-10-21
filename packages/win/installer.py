"""Diglota Setup -- the program that puts the reader on a Windows machine.

    python packages/win/installer.py              # from a checkout
    python packages/win/build.py --installer      # the exe people double-click

It installs into the folder it is run from -- move the setup exe first, then
double-click it. Running it again is how you update: program files are
replaced, and ``data/`` and ``corpus/`` are left exactly as they were.

Three pieces, kept apart on purpose:

* the window (this file),
* the shared drawing kit (``setup_ui.py``),
* every filesystem and registry decision (``setup_common.py``), which is what
  the tests exercise.

``--silent`` installs with no window at all; it exists because a script should
be able to do what a person can, and because it is how the build verifies itself.
"""

from __future__ import annotations

import argparse
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

import setup_common as common
import setup_ui as ui

WINDOW_WIDTH = 680
WINDOW_HEIGHT = 470

# The install is quick; announcing a file count is more honest than a spinner.
PROGRESS_EVERY = 16


class InstallerApp:
    def __init__(self, root: tk.Tk, target: Path, payload: Path,
                 desktop: bool = True) -> None:
        self.root = root
        self.payload = payload
        self.target = Path(target)
        self.want_desktop_shortcut = desktop
        self.kit = ui.Kit(common.saved_theme(self.target))

        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.exit_code = 0
        self.report: common.InstallReport | None = None
        self.shortcut_ok = True

        root.title("Diglota Setup")
        root.resizable(False, False)
        root.configure(bg=self.kit.colors["page"])
        ui.apply_icon(root)
        ui.center_on_screen(root, self.kit.px(WINDOW_WIDTH), self.kit.px(WINDOW_HEIGHT))
        root.protocol("WM_DELETE_WINDOW", self._close)
        root.bind("<Escape>", lambda _event: self._close())

        self._chrome()
        self.show_setup()

    # -- persistent chrome ------------------------------------------------ #

    def _chrome(self) -> None:
        kit, page, ink, ink3 = self.kit, self.kit.colors["page"], self.kit.colors["ink"], self.kit.colors["ink3"]
        tk.Label(self.root, text=common.APP_NAME, font=kit.font("serif", 26, "bold"),
                 bg=page, fg=ink).place(x=kit.px(44), y=kit.px(30), anchor="nw")
        tk.Label(self.root,
                 text=f"Version {common.VERSION}. Free software under the AGPL-3.0.",
                 font=kit.font("sans", 11), bg=page, fg=ink3).place(
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
               weight: str = "normal", fg: str | None = None, wrap: int = 560,
               x: int = 0, y: int = 0) -> tk.Label:
        label = tk.Label(self.body, text=text, font=self.kit.font(family, size, weight),
                         bg=self.kit.colors["page"],
                         fg=fg or self.kit.colors["ink"],
                         justify="left", anchor="nw", wraplength=self.kit.px(wrap))
        label.place(x=self.kit.px(x), y=self.kit.px(y), anchor="nw")
        return label

    def _actions(self, buttons: list[tuple[str, object, str]]) -> None:
        """Pack right to left: the primary action ends up furthest right."""
        for text, command, kind in buttons:
            button = ui.Button(self.actions, self.kit, text, command, kind=kind)
            button.pack(side="right", padx=(self.kit.px(8), 0))
            if kind == "primary":
                self.primary_button = button

    # -- states ------------------------------------------------------------ #

    def show_setup(self) -> None:
        self._clear()
        kit = self.kit
        self._label("Install the reader here.", family="serif", size=23,
                    weight="bold", y=0)
        self._label(
            "Diglota goes into the folder this setup program is in. Move that folder "
            "wherever you want the reader to live, then run setup from there. Your "
            "lessons, reviews and place in every text stay beside it, so running "
            "setup again updates the program and leaves them alone.",
            size=14, fg=kit.colors["ink2"], wrap=566, y=44)

        self._label("Installs to", size=12, fg=kit.colors["ink2"], y=132)

        box = tk.Canvas(self.body, width=kit.px(460), height=kit.px(40),
                        bg=kit.colors["page"], bd=0, highlightthickness=0)
        box.place(x=0, y=kit.px(152))
        ui.rounded(box, kit.px(1), kit.px(1), kit.px(459), kit.px(39), kit.px(9),
                   fill=kit.colors["surface"], outline=kit.colors["line"],
                   width=max(1, kit.px(1)))
        path_font = kit.font("mono", 12)
        box.create_text(kit.px(13), kit.px(20), text=self._short_path(box.winfo_reqwidth()),
                        anchor="w", fill=kit.colors["ink"], font=path_font)

        change = ui.Button(self.body, kit, "Change\u2026", self._choose_folder,
                           kind="secondary", width=kit.px(120))
        change.place(x=kit.px(472), y=kit.px(152))

        check = ui.Check(self.body, kit, "Add a shortcut to the desktop",
                         value=self.want_desktop_shortcut,
                         command=self._set_desktop_shortcut)
        check.place(x=0, y=kit.px(206))

        note = self._note_text()
        if note:
            self._label(note, size=12, fg=kit.colors["ink2"], wrap=566, y=240)

        self._actions([("Install", self.start_install, "primary"),
                       ("Cancel", self._close, "ghost")])
        self.primary_button.focus_set()

    def _note_text(self) -> str:
        if (self.target / "data").is_dir():
            return "Your library is already here. Installing again leaves it exactly as it is."
        if (self.target / common.APP_EXE).is_file():
            return "An earlier copy is here. Your library and settings stay where they are."
        return "It makes its library folder the first time you open it."

    def _short_path(self, available: int) -> str:
        return ui.fit_text(self.kit, str(self.target), self.kit.font("mono", 12),
                           available - self.kit.px(24))

    def show_progress(self) -> None:
        self._clear()
        kit = self.kit
        self._label("Installing the reader.", family="serif", size=23, weight="bold", y=0)
        self.detail = self._label("Copying the program files", size=14,
                                  fg=kit.colors["ink2"], wrap=566, y=44)
        self.bar = ui.Progress(self.body, kit, kit.px(566))
        self.bar.place(x=0, y=kit.px(104))
        self.count = self._label("Starting", size=12, fg=kit.colors["ink3"], y=120)

    def show_done(self) -> None:
        self._clear()
        kit = self.kit
        report = self.report
        self._label("Installed.", family="serif", size=25, weight="bold", y=0)
        self._label(f"Diglota {common.VERSION} is in", size=14,
                    fg=kit.colors["ink2"], wrap=300, y=48)
        path = str(self.target)
        self._label(path, family="mono", size=12, wrap=566, y=72)

        if report and report.existed:
            second = "Your library and settings were left as they were."
        else:
            second = "It will make its library folder when you first open it."
        y = 118
        self._label(second, size=14, fg=kit.colors["ink2"], wrap=566, y=y)
        y += 34

        if report and report.kept and report.existed:
            kept = ("1 existing file" if report.kept == 1
                    else f"{report.kept} existing files")
            self._label(f"{kept} were kept.", size=12,
                        fg=kit.colors["ink2"], y=y)
            y += 28
        if not self.shortcut_ok:
            self._label("The Start menu shortcut could not be created; the program "
                        "itself is installed.", size=12, fg=kit.colors["ink2"],
                        wrap=566, y=y)
            y += 44

        self._label("You can remove it later from Settings \u2192 Apps.",
                    size=12, fg=kit.colors["ink2"], y=y + 8)

        self._actions([("Open Diglota", self._open, "primary"),
                       ("Close", self._close, "ghost")])
        self.primary_button.focus_set()

    def show_error(self, message: str) -> None:
        self._clear()
        self.busy = False
        kit = self.kit
        self._label("The install did not finish.", family="serif", size=23,
                    weight="bold", y=0)
        self._label("Your lessons and settings were not touched.", size=14,
                    fg=kit.colors["ink2"], wrap=566, y=46)

        frame = tk.Frame(self.body, bg=kit.colors["surface"],
                         highlightbackground=kit.colors["line"],
                         highlightthickness=max(1, kit.px(1)))
        frame.place(x=0, y=kit.px(88), width=kit.px(566), height=kit.px(92))
        reason = tk.Text(frame, wrap="word", bd=0, highlightthickness=0,
                         bg=kit.colors["surface"], fg=kit.colors["ink"],
                         font=kit.font("mono", 11), padx=kit.px(12), pady=kit.px(9),
                         height=3)
        reason.insert("1.0", message)
        reason.configure(state="disabled")
        reason.pack(fill="both", expand=True)

        self._label("Close Diglota if it is open, then try again.",
                    size=12, fg=kit.colors["ink3"], wrap=566, y=194)

        self._actions([("Try again", self.show_setup, "primary"),
                       ("Close", self._close, "ghost")])
        self.primary_button.focus_set()

    # -- actions ----------------------------------------------------------- #

    def _set_desktop_shortcut(self, value: bool) -> None:
        self.want_desktop_shortcut = value

    def _choose_folder(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self.root, title="Choose where Diglota goes",
            initialdir=str(self.target.parent), mustexist=False)
        if not chosen:
            return
        # The chosen folder is the app folder -- the same place the setup exe
        # would have used if it had been moved there first.
        self.target = Path(chosen)
        self.show_setup()

    def start_install(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.cancel.clear()
        self.show_progress()
        thread = threading.Thread(target=self._work, name="diglota-install", daemon=True)
        thread.start()
        self.root.after(80, self._poll)

    def _work(self) -> None:
        try:
            if common.app_is_running():
                self.events.put(("phase", "Closing the open reader"))
                if not common.close_running_app():
                    raise common.SetupError(
                        "Diglota is still open. Close its window, then choose Install again.")
            self.events.put(("phase", "Copying the program files"))

            def progress(done: int, total: int, _name: str) -> None:
                if done == total or done % PROGRESS_EVERY == 0:
                    self.events.put(("progress", done, total))

            report = common.install_from_payload(
                self.payload, self.target, progress=progress,
                should_stop=self.cancel.is_set)
            self.events.put(("phase", "Adding shortcuts"))
            self.shortcut_ok = common.create_shortcut(
                common.start_menu_shortcut(), self.target / common.APP_EXE)
            if self.want_desktop_shortcut:
                common.create_shortcut(
                    common.desktop_shortcut(), self.target / common.APP_EXE)
            common.write_uninstall_entry(self.target, common.folder_size(self.target))
            self.events.put(("done", report))
        except common.SetupError as exc:
            self.events.put(("error", str(exc)))
        except Exception as exc:  # noqa: BLE001 - shown in the window, logged by Windows
            self.events.put(("error", f"{type(exc).__name__}: {exc}"))

    def _poll(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                self._handle(event)
        except queue.Empty:
            pass
        if self.busy:
            self.root.after(80, self._poll)

    def _handle(self, event: tuple) -> None:
        kind = event[0]
        if kind == "phase":
            self.detail.configure(text=event[1])
        elif kind == "progress":
            done, total = event[1], event[2]
            self.bar.set(done / total if total else 0)
            self.count.configure(text=f"{done:,} of {total:,} files")
        elif kind == "done":
            self.report = event[1]
            self.busy = False
            self.show_done()
        elif kind == "error":
            self.show_error(event[1])

    def _open(self) -> None:
        try:
            subprocess.Popen([str(self.target / common.APP_EXE)], cwd=str(self.target))
        except OSError as exc:
            self.show_error(f"Could not start Diglota:\n{exc}")
            return
        self.root.after(400, self.root.destroy)

    def _close(self) -> None:
        if self.busy:
            return          # the files are half copied; finish or fail first
        self.root.destroy()


# -- silent mode ---------------------------------------------------------- #

def install_silently(target: Path, payload: Path, desktop: bool) -> int:
    try:
        if common.app_is_running() and not common.close_running_app():
            raise common.SetupError("Diglota is open; close it and run setup again.")
        report = common.install_from_payload(payload, target)
        common.create_shortcut(common.start_menu_shortcut(), target / common.APP_EXE)
        if desktop:
            common.create_shortcut(common.desktop_shortcut(), target / common.APP_EXE)
        common.write_uninstall_entry(target, common.folder_size(target))
    except common.SetupError as exc:
        common.say(f"Diglota was not installed: {exc}", error=True)
        return 1
    common.say(f"Diglota {common.VERSION} installed to {report.path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    common.attach_console()
    parser = argparse.ArgumentParser(prog="Diglota Setup", description="Install Diglota.")
    parser.add_argument("--silent", action="store_true",
                        help="install without showing a window")
    parser.add_argument("--dir", dest="target", default=None,
                        help="install folder (default: the folder this setup is in)")
    parser.add_argument("--no-desktop-shortcut", action="store_true",
                        help="do not add a shortcut to the desktop")
    parser.add_argument("--payload", default=None,
                        help=argparse.SUPPRESS)     # development only
    args = parser.parse_args(argv)

    target = Path(args.target) if args.target else common.default_install_dir()
    payload = Path(args.payload) if args.payload else common.bundled_payload()
    desktop = not args.no_desktop_shortcut

    if args.silent:
        return install_silently(target, payload, desktop)

    ui.set_dpi_awareness()
    root = tk.Tk()
    app = InstallerApp(root, target, payload, desktop)
    root.mainloop()
    return app.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
