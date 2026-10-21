"""The desktop shell's own logic: placement, saved state, and the bridge surface.

None of this needs a window. The bugs worth pinning are the ones that only showed
up on a real screen -- a window saved on another monitor, a resize that grows off
the right edge, and a js_api object that exposed more than it meant to.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

WIN = Path(__file__).resolve().parents[1]          # packages/win
sys.path.insert(0, str(WIN))
sys.path.insert(0, str(WIN.parent / "core"))

from diglota_win import shell  # noqa: E402

from app.config import Settings  # noqa: E402


AREA = (0, 0, 1707, 1019)   # 2560x1600 at 150%, taskbar excluded


@pytest.fixture
def state(tmp_path: Path) -> shell.DesktopState:
    settings = Settings(data_dir=tmp_path, corpus_dir=tmp_path / "corpus")
    return shell.DesktopState(settings=settings, log_path=tmp_path / "desktop.log")


@pytest.fixture
def area(monkeypatch: pytest.MonkeyPatch) -> tuple[int, int, int, int]:
    monkeypatch.setattr(shell, "_work_area", lambda: AREA)
    return AREA


# -- placement ------------------------------------------------------------ #

def test_a_window_that_fits_is_left_where_the_reader_put_it(area) -> None:
    assert shell._place(100, 100, 1280, 860) == (100, 100)


def test_a_window_half_off_screen_is_centred_instead(area) -> None:
    assert shell._place(640, 334, 1280, 860) == (213, 79)


def test_a_window_saved_on_a_larger_monitor_comes_back(area) -> None:
    assert shell._place(3000, 2000, 1280, 860) == (213, 79)


def test_a_saved_size_is_fitted_to_the_work_area(area) -> None:
    assert shell._fit_to_screen(3000, 2000) == (1707, 1019)
    assert shell._fit_to_screen(1280, 860) == (1280, 860)


def test_centring_uses_the_work_area_not_the_screen(area) -> None:
    # 1019 is the work area's height; centring on the full 1067 leaves the window
    # under the taskbar.
    assert shell._centered(1280, 860) == (213, 79)


def test_without_geometry_nothing_is_invented(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell, "_work_area", lambda: None)
    assert shell._place(12, 34, 800, 600) == (12, 34)
    assert shell._centered(800, 600) is None
    assert shell._fit_to_screen(800, 600) == (800, 600)


# -- saved state ---------------------------------------------------------- #

def test_theme_and_geometry_survive_a_round_trip(state: shell.DesktopState) -> None:
    state.theme = "night"
    state.geometry = {"width": 1200, "height": 800, "x": 40, "y": 50}
    state.save_ui()

    other = shell.DesktopState(settings=state.settings, log_path=state.log_path)
    other.load_ui()
    assert other.theme == "night"
    assert other.size == (1200, 800)
    assert other.position == (40, 50)


def test_an_unknown_theme_falls_back_to_paper(state: shell.DesktopState) -> None:
    state.ui_file.parent.mkdir(parents=True, exist_ok=True)
    state.ui_file.write_text(json.dumps({"theme": "neon"}), encoding="utf-8")
    state.load_ui()
    assert state.theme == "paper"


def test_a_tiny_saved_window_is_ignored(state: shell.DesktopState) -> None:
    state.ui_file.parent.mkdir(parents=True, exist_ok=True)
    state.ui_file.write_text(json.dumps({"window": {"width": 100, "height": 80}}),
                             encoding="utf-8")
    state.load_ui()
    assert state.size == shell.DEFAULT_SIZE


def test_a_missing_ui_file_is_not_an_error(state: shell.DesktopState) -> None:
    state.load_ui()
    assert state.theme == "paper"
    assert state.size == shell.DEFAULT_SIZE


# -- the bridge ----------------------------------------------------------- #

def test_the_bridge_exposes_only_its_own_methods(state: shell.DesktopState) -> None:
    """pywebview walks every public attribute of js_api. A public `state` here
    would hand it the window, and the walk recurses through .NET until the stack
    ends -- which is exactly what stalled the first launch."""
    bridge = shell.Bridge(state)
    assert sorted(n for n in dir(bridge) if not n.startswith("_")) == ["quit", "retry"]


# -- the splash ----------------------------------------------------------- #

def test_the_splash_gets_the_saved_theme(state: shell.DesktopState) -> None:
    state.theme = "night"
    html = shell._splash_html(state)
    assert 'data-theme="night"' in html
    assert "__THEME__" not in html


def test_the_splash_names_the_failure_state(state: shell.DesktopState) -> None:
    html = shell._splash_html(state)
    assert "The reader did not start." in html
    assert "window.diglota" in html


# -- the engine ----------------------------------------------------------- #

def test_the_port_is_the_one_the_operating_system_chose() -> None:
    class Socket:
        def getsockname(self):
            return ("127.0.0.1", 51234)

    engine = shell.Engine.__new__(shell.Engine)
    engine.server = SimpleNamespace(servers=[SimpleNamespace(sockets=[Socket()])])
    assert engine.port == 51234
    assert engine.url == "http://127.0.0.1:51234/"


def test_stopping_twice_is_harmless() -> None:
    engine = shell.Engine.__new__(shell.Engine)
    engine.server = SimpleNamespace(should_exit=False)
    engine._stopped = False
    engine.stop()
    engine.stop()
    assert engine.server.should_exit is True
