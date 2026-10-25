"""What the installer promises, tested without showing a window.

The promises are the interesting part: a reinstall must not eat the reader's
library, a payload must not be able to write outside the folder it was given,
and uninstalling must default to keeping the lessons and progress.

All of this lives in ``setup_common``; the windows are only a way to reach it.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

WIN = Path(__file__).resolve().parents[1]          # packages/win
sys.path.insert(0, str(WIN))

import setup_common as common  # noqa: E402


def make_payload(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def app_payload(path: Path, extra: dict[str, str] | None = None) -> Path:
    files = {"Diglota.exe": "program", "corpus/sample.md": "sample lesson"}
    files.update(extra or {})
    return make_payload(path, files)


# -- installing ----------------------------------------------------------- #

def test_a_payload_lands_in_the_chosen_folder(tmp_path: Path) -> None:
    payload = app_payload(tmp_path / "payload.zip")
    target = tmp_path / "install"

    report = common.install_from_payload(payload, target)

    assert (target / "Diglota.exe").read_text() == "program"
    assert (target / "corpus" / "sample.md").read_text() == "sample lesson"
    assert report.files == 2
    assert report.existed is False


def test_progress_reaches_the_last_file(tmp_path: Path) -> None:
    payload = app_payload(tmp_path / "payload.zip")
    seen: list[tuple[int, int]] = []

    common.install_from_payload(payload, tmp_path / "install",
                                progress=lambda done, total, _name: seen.append((done, total)))

    assert seen[-1] == (2, 2)


def test_an_existing_program_is_replaced(tmp_path: Path) -> None:
    target = tmp_path / "install"
    target.mkdir()
    (target / "Diglota.exe").write_text("old")

    common.install_from_payload(app_payload(tmp_path / "payload.zip"), target)

    assert (target / "Diglota.exe").read_text() == "program"


def test_the_readers_lessons_are_not_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "install"
    (target / "corpus").mkdir(parents=True)
    (target / "corpus" / "sample.md").write_text("my own annotations")
    payload = app_payload(tmp_path / "payload.zip",
                          {"corpus/added.md": "a new sample"})

    report = common.install_from_payload(payload, target)

    assert (target / "corpus" / "sample.md").read_text() == "my own annotations"
    assert (target / "corpus" / "added.md").read_text() == "a new sample"
    assert report.kept == 1


def test_data_is_never_written_even_if_the_payload_contains_it(tmp_path: Path) -> None:
    target = tmp_path / "install"
    (target / "data").mkdir(parents=True)
    (target / "data" / "diglot.db").write_text("their library")
    payload = app_payload(tmp_path / "payload.zip", {
        "data/diglot.db": "not theirs",
        "data/new.db": "invented",
    })

    common.install_from_payload(payload, target)

    assert (target / "data" / "diglot.db").read_text() == "their library"
    assert not (target / "data" / "new.db").exists()


def test_a_payload_cannot_write_outside_the_folder(tmp_path: Path) -> None:
    payload = make_payload(tmp_path / "payload.zip", {
        "Diglota.exe": "program",
        "../escaped.txt": "should never land here",
    })

    with pytest.raises(common.SetupError, match="unsafe path"):
        common.install_from_payload(payload, tmp_path / "install")

    assert not (tmp_path / "escaped.txt").exists()


def test_a_payload_cannot_use_a_drive_letter(tmp_path: Path) -> None:
    payload = make_payload(tmp_path / "payload.zip", {
        "Diglota.exe": "program",
        "C:/Windows/escaped.txt": "nope",
    })

    with pytest.raises(common.SetupError, match="unsafe path"):
        common.install_from_payload(payload, tmp_path / "install")


def test_a_damaged_payload_says_so(tmp_path: Path) -> None:
    payload = tmp_path / "payload.zip"
    payload.write_text("this is not a zip")

    with pytest.raises(common.SetupError, match="damaged"):
        common.install_from_payload(payload, tmp_path / "install")


def test_a_missing_payload_says_so(tmp_path: Path) -> None:
    with pytest.raises(common.SetupError, match="payload is missing"):
        common.install_from_payload(tmp_path / "nope.zip", tmp_path / "install")


def test_a_payload_without_the_exe_is_refused(tmp_path: Path) -> None:
    payload = make_payload(tmp_path / "payload.zip", {"corpus/x.md": "x"})

    with pytest.raises(common.SetupError, match="without Diglota.exe"):
        common.install_from_payload(payload, tmp_path / "install")


def test_cancelling_stops_the_copy(tmp_path: Path) -> None:
    payload = app_payload(tmp_path / "payload.zip", {"corpus/b.md": "b"})

    with pytest.raises(common.SetupError, match="cancelled"):
        common.install_from_payload(payload, tmp_path / "install",
                                    should_stop=lambda: True)


# -- removing ------------------------------------------------------------- #

def install_with_library(tmp_path: Path) -> Path:
    target = tmp_path / "install"
    (target / "corpus").mkdir(parents=True)
    (target / "data").mkdir()
    (target / "data" / "diglot.db").write_text("lib")
    (target / "corpus" / "mine.md").write_text("lesson")
    (target / "Diglota.exe").write_text("program")
    (target / "Uninstall.exe").write_text("remover")
    (target / "README.md").write_text("readme")
    (target / "_internal").mkdir()
    (target / "_internal" / "python313.dll").write_text("dll")
    return target


def test_removing_keeps_the_library_by_default(tmp_path: Path) -> None:
    target = install_with_library(tmp_path)

    report = common.remove_install(target, keep_library=True)

    assert report.kept_library is True
    assert not (target / "Diglota.exe").exists()
    assert not (target / "_internal").exists()
    assert (target / "data" / "diglot.db").read_text() == "lib"
    assert (target / "corpus" / "mine.md").read_text() == "lesson"


def test_removing_everything_deletes_the_folder(tmp_path: Path) -> None:
    target = install_with_library(tmp_path)

    common.remove_install(target, keep_library=False)

    assert not target.exists()


def test_a_folder_that_is_not_an_install_is_left_alone(tmp_path: Path) -> None:
    target = tmp_path / "documents"
    target.mkdir()
    (target / "notes.txt").write_text("keep me")

    with pytest.raises(common.SetupError, match="does not look like"):
        common.remove_install(target, keep_library=True)

    assert (target / "notes.txt").exists()


def test_an_empty_shell_is_removed_afterward(tmp_path: Path) -> None:
    target = tmp_path / "install"
    target.mkdir()
    (target / "Diglota.exe").write_text("program")

    common.remove_install(target, keep_library=True)

    assert not target.exists()


# -- small pieces --------------------------------------------------------- #

def test_uninstall_entry_points_at_this_folder(tmp_path: Path) -> None:
    target = tmp_path / "Programs" / "Diglota"

    values = common.uninstall_values(target, size_bytes=5 * 1024 * 1024)

    assert values["DisplayName"] == "Diglota"
    assert values["DisplayVersion"] == common.VERSION
    assert values["InstallLocation"] == str(target)
    assert values["UninstallString"] == f'"{target / "Uninstall.exe"}"'
    assert values["QuietUninstallString"] == f'"{target / "Uninstall.exe"}" --silent'
    assert values["EstimatedSize"] == 5 * 1024


def test_size_is_written_in_units_people_read() -> None:
    assert common.format_size(999) == "999 B"
    assert common.format_size(1536) == "1.5 KB"
    assert common.format_size(5 * 1024 ** 2) == "5.0 MB"


def test_the_default_folder_is_the_folder_setup_runs_from(tmp_path: Path,
                                                          monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable",
                        str(tmp_path / f"Diglota-Setup-{common.VERSION}.exe"))
    assert common.default_install_dir() == tmp_path


def test_from_a_checkout_the_current_folder_stands_in(tmp_path: Path,
                                                      monkeypatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.chdir(tmp_path)
    assert common.default_install_dir() == tmp_path


def test_the_saved_theme_is_respected(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "ui.json").write_text('{"theme": "night"}')
    assert common.saved_theme(tmp_path) == "night"


def test_an_unknown_or_missing_theme_falls_back_to_paper(tmp_path: Path) -> None:
    assert common.saved_theme(tmp_path) == "paper"
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "ui.json").write_text('{"theme": "neon"}')
    assert common.saved_theme(tmp_path) == "paper"


def test_powershell_quoting_survives_an_apostrophe() -> None:
    assert common._powershell_quote("C:\\Reader's Copy") == "'C:\\Reader''s Copy'"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcuts")
def test_a_shortcut_is_actually_created(tmp_path: Path) -> None:
    target = tmp_path / "Diglota.exe"
    target.write_text("program")
    link = tmp_path / "Diglota.lnk"

    assert common.create_shortcut(link, target) is True
    assert link.is_file()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shell folders")
def test_the_shortcut_folders_exist() -> None:
    assert common.start_menu_dir().is_dir()
    assert common.desktop_dir().is_dir()
    assert common._find_window("A window that cannot exist 4C1F00B2") is None


def test_a_window_only_counts_as_the_reader_when_diglota_owns_it() -> None:
    assert common._is_app_executable(r"C:\Apps\Diglota\Diglota.exe")
    assert not common._is_app_executable(r"C:\Windows\explorer.exe")
    assert not common._is_app_executable(
        r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    assert not common._is_app_executable("")


def test_the_payload_in_a_checkout_is_found_where_the_build_leaves_it() -> None:
    path = common.bundled_payload()
    if getattr(sys, "_MEIPASS", None):
        assert path.name == "payload.zip"
    else:
        assert path == WIN / "build" / "payload.zip"
