"""The desktop window: the same engine and the same pages, with no browser around them.

Two decisions shape this file.

**The engine runs in the same process.** The web version is a FastAPI app on a
port; the desktop version is that same app on a port the operating system picks,
started on a thread, with the window pointed at it. Nothing is reimplemented and
nothing is a second copy: a change to the reader is a change to both. The port is
ephemeral (0) and loopback-only, so a desktop launch never collides with a web one
already running, and never listens further than this machine.

**The window is a normal Windows window.** No frameless chrome, no custom window
buttons, no fake title bar: the title bar is the operating system's, close means
close, and the icon is the app's. A `pywebview` window also gives us persistent
storage (``private_mode=False``), which is what keeps the reader's theme, text size
and place in an article across launches -- the same state the browser kept.

The splash is the only UI this package owns. It exists because the engine takes a
moment to start, and because a windowed build has nowhere else to say why it did
not: the failure state names the reason and the log file, and offers to try again.
"""

from __future__ import annotations

import ctypes
import json
import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "Diglota"
MUTEX_NAME = "Local\\Diglota.Desktop"
DEFAULT_SIZE = (1280, 860)
SPLASH_SIZE = (720, 520)

PALETTES = {"paper": "#e9ece0", "sepia": "#e4d6bd", "night": "#151d17"}

log = logging.getLogger("diglot.desktop")


def _workspace_root() -> Path:
    """The folder that holds ``data/`` and ``corpus/`` -- the same rule config.py
    applies, repeated here because logging must work before the engine is imported."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


WORKSPACE = _workspace_root()
CORE = WORKSPACE / "packages" / "core"
if not getattr(sys, "frozen", False) and str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

import uvicorn  # noqa: E402

from app.config import Settings  # noqa: E402
from app.server import app as asgi_app  # noqa: E402
from app.server import set_shutdown_hook  # noqa: E402

try:
    import webview  # noqa: E402
except ImportError:  # pragma: no cover - the packaged build always has it
    webview = None


# -- the engine ----------------------------------------------------------- #

class Engine:
    """The FastAPI app on a background thread, on a port the OS chooses."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.server = uvicorn.Server(uvicorn.Config(
            asgi_app, host="127.0.0.1", port=0, log_config=None, access_log=False))
        self.thread = threading.Thread(target=self._run, name="diglota-engine", daemon=True)
        self.error: str | None = None
        self._stopped = False

    def _run(self) -> None:
        try:
            self.server.run()
        except BaseException as exc:  # noqa: BLE001 - shown in the window
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("engine thread failed")

    def start(self, timeout: float = 30.0) -> bool:
        self.thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.server.started:
                return True
            if self.error:
                return False
            if not self.thread.is_alive():
                self.error = self.error or "The engine stopped before it was ready."
                return False
            time.sleep(0.05)
        self.error = f"The engine did not answer within {int(timeout)} seconds."
        return False

    @property
    def port(self) -> int:
        for server in self.server.servers:
            for sock in server.sockets:
                return int(sock.getsockname()[1])
        raise RuntimeError("the engine is not listening")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def stop(self) -> None:
        """Idempotent: the quit button, the title-bar X and a failed boot can all
        reach this, in any order."""
        if self._stopped:
            return
        self._stopped = True
        self.server.should_exit = True


# -- window state --------------------------------------------------------- #

@dataclass
class DesktopState:
    settings: Settings
    log_path: Path
    engine: Engine | None = None
    window: object | None = None
    theme: str = "paper"
    size: tuple[int, int] = DEFAULT_SIZE
    position: tuple[int, int] | None = None
    geometry: dict[str, int] | None = None
    navigated: bool = False
    splash_loaded: threading.Event = field(default_factory=threading.Event)
    closed: bool = False

    @property
    def ui_file(self) -> Path:
        return self.settings.data_dir / "ui.json"

    def load_ui(self) -> None:
        try:
            data = json.loads(self.ui_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        theme = data.get("theme")
        if theme in PALETTES:
            self.theme = theme
        saved = data.get("window")
        if isinstance(saved, dict):
            try:
                width, height = int(saved["width"]), int(saved["height"])
            except (KeyError, TypeError, ValueError):
                return
            # Fitted to the screen later, in _boot: before the GUI starts this
            # process is not yet DPI-aware, so "the screen" is not yet measured in
            # the units the window will use.
            if width >= 640 and height >= 480:
                self.size = (width, height)
            try:
                self.position = (int(saved["x"]), int(saved["y"]))
            except (KeyError, TypeError, ValueError):
                self.position = None

    def capture_geometry(self) -> None:
        """Read the window rectangle while it still exists.

        Called from the ``closing`` event, not after ``webview.start`` returns:
        by then the native window is gone and ``window.width`` raises, which is
        how the first version silently saved no geometry at all.
        """
        if self.window is None:
            return
        try:
            self.geometry = {
                "width": int(self.window.width), "height": int(self.window.height),
                "x": int(self.window.x), "y": int(self.window.y),
            }
        except Exception:  # noqa: BLE001 - geometry is a nicety, never a crash
            log.debug("could not read window geometry", exc_info=True)

    def save_ui(self) -> None:
        data = {"theme": self.theme}
        if self.geometry:
            data["window"] = self.geometry
        try:
            self.ui_file.parent.mkdir(parents=True, exist_ok=True)
            self.ui_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except OSError:
            log.warning("could not save window state", exc_info=True)

    def request_quit(self) -> None:
        """What the in-app *Quit* button reaches, through the server's hook."""
        if self.engine:
            self.engine.stop()
        if self.window is not None:
            self.capture_geometry()   # destroy() may not raise `closing`
            # Let the HTTP response leave first: this is called from a request thread.
            threading.Timer(0.35, _destroy, args=(self.window,)).start()


def _destroy(window: object) -> None:
    try:
        window.destroy()
    except Exception:  # noqa: BLE001 - already gone is the outcome we wanted
        pass


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _dpi_scale() -> float:
    """Windows' display scale (1.5 at 150%), as pywebview uses it.

    pywebview calls ``SetProcessDPIAware`` when it starts, so from that point on
    Windows reports this process physical pixels -- 2560 wide, not 1707 -- while
    ``window.move`` takes logical ones. This is the divisor between the two.
    """
    if sys.platform != "win32":
        return 1.0
    try:
        dpi = int(ctypes.windll.user32.GetDpiForSystem())
    except Exception:  # noqa: BLE001 - older Windows, or a non-Windows build
        return 1.0
    return dpi / 96.0 if 72 <= dpi <= 480 else 1.0


def _work_area() -> tuple[int, int, int, int] | None:
    """The desktop minus the taskbar, in the logical units ``window.move`` uses.

    ``SPI_GETWORKAREA``, not the full screen: a window sized to the screen puts its
    bottom edge under the taskbar, which is exactly how a maximised-looking app
    starts with a button no one can reach.
    """
    if sys.platform != "win32":
        return None
    rect = _RECT()
    try:
        ok = ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
    except Exception:  # noqa: BLE001
        return None
    if not ok:
        return None
    scale = _dpi_scale()
    return (round(rect.left / scale), round(rect.top / scale),
            round(rect.right / scale), round(rect.bottom / scale))


def _fit_to_screen(width: int, height: int) -> tuple[int, int]:
    """A window saved on a larger monitor must not open off this one."""
    area = _work_area()
    if not area:
        return width, height
    left, top, right, bottom = area
    return min(width, right - left), min(height, bottom - top)


def _place(x: int, y: int, width: int, height: int) -> tuple[int, int]:
    """Pull a saved position back onto the work area.

    A position saved on a monitor that is no longer connected is not a reason to
    open the app somewhere invisible; neither is a position that was already
    half off-screen when it was saved. Clamping keeps the whole window reachable
    and still respects a window the reader moved deliberately.
    """
    area = _work_area()
    if not area:
        return x, y
    left, top, right, bottom = area
    if left <= x and top <= y and x + width <= right and y + height <= bottom:
        return x, y
    return _centered(width, height) or (x, y)


def _centered(width: int, height: int) -> tuple[int, int] | None:
    area = _work_area()
    if not area:
        return None
    left, top, right, bottom = area
    return left + max(0, (right - left - width) // 2), top + max(0, (bottom - top - height) // 2)


# -- the bridge the splash calls ------------------------------------------ #

class Bridge:
    """``window.pywebview.api`` -- used only by the splash, never by the app.

    The state is stored under a private name on purpose. pywebview exposes a
    ``js_api`` object by recursively walking every *public* attribute, so a plain
    ``self.state`` here would hand it the window, the state would hand it the .NET
    window, and the walk would recurse through the accessibility tree until Python's
    stack ran out -- which also stalled the ``loaded`` event this shell waits for.
    """

    def __init__(self, state: DesktopState) -> None:
        self._state = state

    def quit(self) -> bool:
        self._state.request_quit()
        return True

    def retry(self) -> bool:
        if self._state.engine:
            self._state.engine.stop()
        threading.Thread(target=_boot, args=(self._state,), name="diglota-retry",
                         daemon=True).start()
        return True


# -- startup -------------------------------------------------------------- #

def _boot(state: DesktopState) -> None:
    log.info("boot thread entered; waiting for the splash to load")
    window = state.window
    if not state.splash_loaded.wait(10):
        log.warning("splash never reported loaded; starting the engine anyway")

    engine = Engine(state.settings)
    state.engine = engine
    log.info("starting the engine")
    ok = engine.start()
    if not ok:
        log.error("engine did not start: %s", engine.error)
        _js(window, "window.diglota && window.diglota.fail(%s, %s)" % (
            json.dumps(engine.error or "No reason was reported."),
            json.dumps(str(state.log_path))))
        return

    log.info("engine listening on %s", engine.url)
    _js(window, "window.diglota && window.diglota.ready()")
    # One beat, long enough to see the gloss arrive and short enough not to wait
    # for it: the splash is a hand-over, not a title screen.
    time.sleep(0.36)

    state.navigated = True
    try:
        width, height = _fit_to_screen(*state.size)
        window.resize(width, height)
        # The splash was centred at its own size; growing it in place would push
        # the right and bottom edges off-screen. Put it back on the work area --
        # at the saved position if that is still reachable, otherwise centred.
        position = (_place(*state.position, width, height) if state.position
                    else _centered(width, height))
        log.info("window %sx%s at %s (work area %s)", width, height, position, _work_area())
        if position:
            window.move(*position)
        window.load_url(engine.url)
    except Exception:  # noqa: BLE001 - the window may have been closed mid-boot
        log.warning("could not hand over to the app", exc_info=True)


def _js(window: object, code: str) -> None:
    try:
        window.evaluate_js(code)
    except Exception:  # noqa: BLE001
        log.debug("evaluate_js failed: %s", code, exc_info=True)


def _remember_theme(window: object, state: DesktopState) -> None:
    """Read the theme the reader actually chose, a moment after the reader loads,
    so the next launch's splash does not flash paper at someone reading at night."""
    def work() -> None:
        time.sleep(1.2)
        try:
            theme = window.evaluate_js("document.documentElement.dataset.theme || ''")
        except Exception:  # noqa: BLE001
            return
        if theme in PALETTES:
            state.theme = theme
    threading.Thread(target=work, name="diglota-theme", daemon=True).start()


# -- single instance ------------------------------------------------------ #

_mutex_handle: object | None = None


def _single_instance() -> bool:
    """One copy per user. A second double-click restores the first window instead
    of starting a second engine against the same database."""
    global _mutex_handle
    if sys.platform != "win32":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    _mutex_handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
        return True
    _focus_existing()
    return False


def _window_process_name(hwnd: int) -> str:
    """Lowercase executable basename owning ``hwnd``, or empty string."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = ctypes.c_ulong(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name.lower()
    finally:
        kernel32.CloseHandle(handle)
    return ""


def _focus_existing() -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.restype = ctypes.c_void_p
    expected = Path(sys.executable).name.lower() if getattr(sys, "frozen", False) else ""
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        hwnd = user32.FindWindowW(None, APP_NAME)
        if hwnd and (not expected or _window_process_name(hwnd) == expected):
            user32.ShowWindow(ctypes.c_void_p(hwnd), 9)       # SW_RESTORE
            user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
            return
        time.sleep(0.2)


# -- logging and the last word on failure --------------------------------- #

def _setup_logging(settings: Settings) -> Path:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    path = settings.data_dir / "desktop.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    # A windowed build has no stdout at all, and a stray print would raise rather
    # than be ignored. Point both at the same file the log uses.
    if sys.stdout is None or sys.stderr is None:
        stream = open(path, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
    return path


def _message_box(text: str) -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x10)
        except Exception:  # noqa: BLE001
            pass


def _splash_html(state: DesktopState) -> str:
    path = Path(__file__).resolve().parent / "splash.html"
    return path.read_text(encoding="utf-8").replace("__THEME__", state.theme)


def main() -> int:
    if webview is None:
        _message_box("The desktop app needs pywebview.\n\n"
                     "Run: pip install -r packages/win/requirements.txt")
        return 2
    if not _single_instance():
        return 0

    settings = Settings.load()
    log_path = _setup_logging(settings)
    log.info("desktop start: workspace=%s", WORKSPACE)

    state = DesktopState(settings=settings, log_path=log_path)
    state.load_ui()

    window = webview.create_window(
        APP_NAME,
        html=_splash_html(state),
        width=SPLASH_SIZE[0], height=SPLASH_SIZE[1],
        min_size=(560, 400),
        background_color=PALETTES[state.theme],
        js_api=Bridge(state),
    )
    state.window = window

    def on_loaded(*_args: object) -> None:
        if not state.navigated:
            state.splash_loaded.set()
        else:
            _remember_theme(window, state)

    def on_closing(*_args: object) -> None:
        state.capture_geometry()

    def on_closed(*_args: object) -> None:
        state.closed = True

    window.events.loaded += on_loaded
    window.events.closing += on_closing
    window.events.closed += on_closed

    set_shutdown_hook(state.request_quit)
    try:
        webview.start(_boot, (state,), private_mode=False,
                      storage_path=str(settings.data_dir / "webview"))
    finally:
        if state.engine:
            state.engine.stop()
        state.save_ui()
        log.info("desktop stop")
    return 0


def run() -> int:
    """Entry point that never dies silently: a windowed build has no console, so a
    startup failure writes the log and shows the one message it can."""
    try:
        return main()
    except BaseException as exc:  # noqa: BLE001
        log.exception("desktop app failed")
        _message_box(f"Diglota could not start.\n\n{type(exc).__name__}: {exc}\n\n"
                     f"The details are in:\n{WORKSPACE / 'data' / 'desktop.log'}")
        return 1
