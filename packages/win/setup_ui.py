"""The setup windows: the reader's palette and type, drawn with tkinter.

These are the only tkinter windows in the repository. They exist so that the
installer and the uninstaller need nothing but the Python standard library --
and they use the reader's own tokens, so the first thing a new install shows is
the same paper, the same serif and the same teal the app opens into. An
uninstall of a night reader is dark, because ``data/ui.json`` already says so.

The kit is small on purpose: a rounded button, a checkbox, a progress bar, and
the text. Everything else is placed by hand at one window size.
"""

from __future__ import annotations

import ctypes
import sys
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path

# Copied from packages/ui/static/style.css and the desktop splash, so setup
# never introduces a color the reader will not meet again.
PALETTES = {
    "paper": {
        "page": "#f4efe6", "surface": "#fdfbf7", "ink": "#211d18",
        "ink2": "#5c554b", "ink3": "#8b8375", "es": "#235e58",
        "accent": "#1f8f6d", "primary": "#166f55", "primary_hover": "#105642",
        "on_primary": "#ffffff", "line": "#e2d9c9",
    },
    "sepia": {
        "page": "#ece0c8", "surface": "#f8f0dd", "ink": "#2a2216",
        "ink2": "#6a5c42", "ink3": "#94856a", "es": "#1d534d",
        "accent": "#1a8266", "primary": "#136049", "primary_hover": "#0e4a38",
        "on_primary": "#ffffff", "line": "#ddcfae",
    },
    "night": {
        "page": "#0f1114", "surface": "#15181c", "ink": "#e9e6df",
        "ink2": "#a8a49a", "ink3": "#74716a", "es": "#7fc9b4",
        "accent": "#2ea07f", "primary": "#2ea07f", "primary_hover": "#3fbf9a",
        "on_primary": "#08110e", "line": "#2a2f36",
    },
}

DESTRUCTIVE = "#a33a2a"
DESTRUCTIVE_HOVER = "#8a2f22"

SERIF = "Palatino Linotype"
SANS = "Segoe UI"
MONO = "Consolas"


def set_dpi_awareness() -> None:
    """Ask Windows for physical pixels before Tk measures anything.

    Without this the window is drawn at 96 DPI and Windows stretches it, which
    is exactly the soft, slightly blurry look a desktop app should not have.
    """
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)     # system DPI aware
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def dpi_scale() -> float:
    if sys.platform != "win32":
        return 1.0
    try:
        dpi = int(ctypes.windll.user32.GetDpiForSystem())
    except (AttributeError, OSError):
        return 1.0
    return dpi / 96.0 if 72 <= dpi <= 480 else 1.0


class Kit:
    """Scale, color and type in one place, so every window measures the same."""

    def __init__(self, theme: str = "paper") -> None:
        self.theme = theme if theme in PALETTES else "paper"
        self.colors = PALETTES[self.theme]
        self.scale = dpi_scale()

    def px(self, value: float) -> int:
        return int(round(value * self.scale))

    def font(self, family: str, size: int, weight: str = "normal") -> tuple:
        face = {"serif": SERIF, "sans": SANS, "mono": MONO}.get(family, family)
        return (face, -self.px(size), weight)

    def measure(self, text: str, font: tuple) -> int:
        return tkfont.Font(font=font).measure(text)


def fit_text(kit: Kit, text: str, font: tuple, width: int) -> str:
    """Drop characters from the front of a path until it fits.

    The end of a path carries the name a person recognises; the drive and the
    intermediate folders are what can go.
    """
    if kit.measure(text, font) <= width:
        return text
    while len(text) > 2 and kit.measure("…" + text[1:], font) > width:
        text = text[1:]
    return "…" + text[1:]


def rounded(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
            radius: float, **options) -> int:
    """A rounded rectangle as a smoothed polygon -- tkinter has no primitive."""
    radius = min(radius, abs(x2 - x1) / 2, abs(y2 - y1) / 2)
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **options)


class Button(tk.Canvas):
    """A rounded button with hover, pressed, focus and disabled states."""

    def __init__(self, master: tk.Misc, kit: Kit, text: str, command,
                 kind: str = "primary", width: int | None = None) -> None:
        self.kit = kit
        self.command = command
        self.kind = kind
        self._text = text
        self._font = kit.font("sans", 14, "bold" if kind == "primary" else "normal")
        self._height = kit.px(40)
        self._width = width or kit.measure(text, self._font) + 2 * kit.px(20)
        super().__init__(master, width=self._width, height=self._height,
                         bg=kit.colors["page"], bd=0, highlightthickness=0,
                         takefocus=1, cursor="hand2")
        self._enabled = True
        self._hover = False
        self._pressed = False
        self._draw()

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Return>", self._activate)
        self.bind("<KP_Enter>", self._activate)
        self.bind("<space>", self._activate)
        self.bind("<FocusIn>", lambda _event: self._draw())
        self.bind("<FocusOut>", lambda _event: self._draw())

    # -- appearance ------------------------------------------------------- #

    def _palette(self) -> tuple[str, str, str]:
        c = self.kit.colors
        if not self._enabled:
            if self.kind == "primary" or self.kind == "destructive":
                return c["line"], c["ink3"], ""
            return c["page"], c["ink3"], ""
        if self.kind == "primary":
            fill = c["primary_hover"] if (self._hover or self._pressed) else c["primary"]
            return fill, c["on_primary"], ""
        if self.kind == "destructive":
            fill = DESTRUCTIVE_HOVER if (self._hover or self._pressed) else DESTRUCTIVE
            return fill, "#ffffff", ""
        if self.kind == "secondary":
            fill = c["surface"]
            outline = c["ink3"] if (self._hover or self._pressed) else c["line"]
            return fill, c["ink"], outline
        # ghost
        fill = c["surface"] if (self._hover or self._pressed) else c["page"]
        return fill, c["ink"] if self._hover else c["ink2"], ""

    def _draw(self) -> None:
        self.delete("all")
        fill, ink, outline = self._palette()
        radius = self.kit.px(9)
        focused = self.focus_get() is self
        # Focus is a halo, not an inner border: the fill shrinks a little and the
        # ring sits outside it with a breath of page between. An accent ring drawn
        # inside a dark green button is invisible; this one works on every fill.
        inset = self.kit.px(4 if focused else 1)
        rounded(self, inset, inset, self._width - inset, self._height - inset, radius,
                fill=fill, outline=outline or fill,
                width=max(1, self.kit.px(1)))
        self.create_text(self._width / 2, self._height / 2, text=self._text,
                         fill=ink, font=self._font, anchor="center")
        if focused:
            pad = self.kit.px(1)
            rounded(self, pad, pad, self._width - pad, self._height - pad, radius,
                    fill="", outline=self.kit.colors["accent"],
                    width=max(2, self.kit.px(2)))

    # -- behaviour -------------------------------------------------------- #

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self._pressed = False
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    def _on_enter(self, _event) -> None:
        self._hover = True
        self._draw()

    def _on_leave(self, _event) -> None:
        self._hover = False
        self._pressed = False
        self._draw()

    def _on_press(self, _event) -> None:
        if not self._enabled:
            return
        self.focus_set()
        self._pressed = True
        self._draw()

    def _on_release(self, event) -> None:
        if not self._enabled or not self._pressed:
            return
        self._pressed = False
        self._draw()
        inside = self.winfo_containing(event.x_root, event.y_root) is self
        if inside and self.command:
            self.command()

    def _activate(self, _event) -> str | None:
        if self._enabled and self.command:
            self.command()
        return "break"


class Check(tk.Canvas):
    """A checkbox with a text label, toggled by click, Space or Return."""

    def __init__(self, master: tk.Misc, kit: Kit, text: str, value: bool = False,
                 command=None) -> None:
        self.kit = kit
        self.text = text
        self.value = value
        self.command = command
        self._font = kit.font("sans", 13)
        self._box = kit.px(20)
        gap = kit.px(10)
        height = max(self._box, kit.px(22))
        width = self._box + gap + kit.measure(text, self._font) + kit.px(8)
        super().__init__(master, width=width, height=height, bg=kit.colors["page"],
                         bd=0, highlightthickness=0, takefocus=1, cursor="hand2")
        self._gap = gap
        self._height = height
        self._width = width
        self._draw()
        self.bind("<Button-1>", lambda _event: self._toggle())
        self.bind("<space>", lambda _event: self._toggle())
        self.bind("<Return>", lambda _event: self._toggle())
        self.bind("<FocusIn>", lambda _event: self._draw())
        self.bind("<FocusOut>", lambda _event: self._draw())

    def _draw(self) -> None:
        self.delete("all")
        c = self.kit.colors
        inset = self.kit.px(2)
        top = (self._height - self._box) / 2
        radius = self.kit.px(5)
        if self.value:
            rounded(self, inset, top, self._box - inset, top + self._box, radius,
                    fill=c["accent"], outline=c["accent"])
            mark = self._box
            self.create_line(self._box * 0.26, top + mark * 0.52,
                             self._box * 0.43, top + mark * 0.69,
                             self._box * 0.74, top + mark * 0.31,
                             fill=c["on_primary"], width=max(2, self.kit.px(2)),
                             capstyle="round", joinstyle="round")
        else:
            rounded(self, inset, top, self._box - inset, top + self._box, radius,
                    fill=c["surface"], outline=c["line"],
                    width=max(1, self.kit.px(1)))
        self.create_text(self._box + self._gap, self._height / 2, text=self.text,
                         anchor="w", fill=c["ink2"], font=self._font)
        if self.focus_get() is self:
            rounded(self, 0, 0, self._width - 1, self._height - 1, radius,
                    fill="", outline=c["accent"], width=max(1, self.kit.px(1)))

    def _toggle(self) -> None:
        self.value = not self.value
        self._draw()
        if self.command:
            self.command(self.value)


class Progress(tk.Canvas):
    """A thin progress bar; no percentage inside it, the line below says the count."""

    def __init__(self, master: tk.Misc, kit: Kit, width: int, height: int = 6) -> None:
        self.kit = kit
        self._width = width
        self._height = kit.px(height)
        super().__init__(master, width=width, height=self._height,
                         bg=kit.colors["page"], bd=0, highlightthickness=0)
        self._fraction = 0.0
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        c = self.kit.colors
        radius = self._height / 2
        rounded(self, 0, 0, self._width, self._height, radius, fill=c["line"])
        width = max(0, min(1.0, self._fraction)) * self._width
        if width >= self._height:
            rounded(self, 0, 0, width, self._height, radius, fill=c["accent"])

    def set(self, fraction: float) -> None:
        self._fraction = fraction
        self._draw()


def center_on_screen(window: tk.Tk, width: int, height: int) -> None:
    window.update_idletasks()
    x = max(0, (window.winfo_screenwidth() - width) // 2)
    # A touch above dead centre: the window reads better sitting a little high.
    y = max(0, (window.winfo_screenheight() - height) // 2 - int(height * 0.04))
    window.geometry(f"{width}x{height}+{x}+{y}")


def asset(name: str) -> Path:
    """A file bundled with the frozen program, or its place in the checkout."""
    frozen = getattr(sys, "_MEIPASS", None)
    if not frozen:
        return Path(__file__).resolve().parent / "assets" / name
    base = Path(frozen)
    for candidate in (base / "assets" / name, base / "_internal" / "assets" / name):
        if candidate.is_file():
            return candidate
    return base / "assets" / name


def apply_icon(window: tk.Tk) -> None:
    try:
        window.iconbitmap(default=str(asset("diglota.ico")))
    except tk.TclError:
        pass
