# Development, Testing & Packaging Guide

This guide covers local development setup, running the test and QA suites, and building the Windows desktop distribution and installer.

---

## 1. Local Environment Setup

Requires **Python 3.11+** (tested on Python 3.13).

```bash
# Core web server dependencies
pip install -r requirements.txt

# Unit & browser testing dependencies (on top of requirements.txt)
pip install -r requirements-dev.txt

# Windows native desktop shell & PyInstaller build dependencies
pip install -r packages/win/requirements.txt
```

> **Icon regeneration:** `packages/win/make_icon.py` uses `Pillow` (`pip install Pillow`). You only need Pillow if you delete or regenerate `packages/win/assets/diglota.ico`.

### Configuration (`.env`)

Copy `.env.example` to `.env` if you want environment-level defaults (optional — everything works offline without keys, and keys can also be entered in the in-app **Settings** panel, which writes `data/settings.json`):

| Variable | Default | Purpose |
|---|---|---|
| `LLM_BASE_URL` | *(empty)* | OpenAI-compatible chat API base URL (`http://` or `https://`). |
| `LLM_API_KEY` | *(empty)* | API key for the Tutor LLM (never sent to the browser). |
| `LLM_MODEL` | *(empty)* | Chat model identifier. |
| `TYPESAFE_API_KEY` | *(empty)* | Optional TypeSafe / Jev API key for calibrated semantic grading and placement. |
| `TYPESAFE_MODEL` | `jev-latest` | Jev model identifier. |
| `DIGLOT_CORPUS` | `./corpus` | Directory of curated hand-made Markdown lessons (read-only except explicit Register edits). |
| `DIGLOT_DATA` | `./data` | Directory for `diglot.db`, `settings.json`, `library/`, `backups/`, and logs. |
| `DIGLOT_PROXY` | *(empty)* | Optional HTTP/HTTPS proxy URL for outbound web fetches. Empty connects directly. |
| `DIGLOT_PAIR` | `en-es` | Active language pair (`en-es` or `en-fr`). |
| `DIGLOT_ENV_FILE` | *(empty)* | Optional explicit `.env` file path checked ahead of `./.env`. |

### Running Locally

```bash
# Browser mode (default http://127.0.0.1:8787)
python run.py

# Browser mode with file logging (automatically creates parent directories)
pythonw run.py --log data/app.log

# Windows double-click browser launcher (windowless)
wscript "Open Diglota.vbs"

# Native Windows desktop window (pywebview / Edge WebView2)
python -m diglota_win          # with PYTHONPATH=packages/win
```

---

## 2. Testing & Quality Assurance

### Unit Tests (`pytest`)

`pytest.ini` configures `testpaths = packages/core/tests packages/win/tests` and `pythonpath = packages/core packages/win`, keeping the Playwright scripts under `tools/` out of pytest discovery.

```bash
python -m pytest -q
```

- **Fresh clone behavior:** Calibration tests that validate segmenter accuracy and round-trip fidelity across a large library require at least `15` `.md` lessons in `DIGLOT_CORPUS` (`packages/core/tests/corpus_support.py`, `MIN_REAL_CORPUS = 15`). On a fresh clone with only the single bundled sample passage (`corpus/why-reading-teaches-a-language.md`), those 13 calibration tests skip cleanly while the rest of the suite runs and passes.
- **Testing grading fallbacks explicitly:**
  ```bash
  TYPESAFE_API_KEY= python -m pytest -q packages/core/tests/test_grading.py
  ```

### Frontend Static Verification

The UI uses **zero-build native ES Modules** under [`packages/ui/static/js/`](../packages/ui/static/js/). To verify all JS syntax and relative module imports without needing a bundler:

```bash
# Check syntax of every ES module with Node (if installed)
node --check packages/ui/static/js/main.js
```

### Browser E2E Suite (`tools/ui_test.py` & `tools/ui_import_test.py`)

Both scripts drive an installed Chrome via Playwright against an **already-running** server at `http://127.0.0.1:8787`:

```bash
# Terminal 1
python run.py

# Terminal 2
python tools/ui_test.py                 # ~307 offline UI checks (expects >10 lessons in library)
python tools/ui_test.py --with-ai       # also exercises live AI grading/tutor paths
python tools/ui_import_test.py          # end-to-end live import + toast + reader check
```

> **Note:** `tools/ui_test.py` asserts `cards > 10` in the library shelf and writes screenshots into `shots/` (gitignored). Run it against a populated `DIGLOT_CORPUS` or after importing sample articles.

### Corpus & Segmenter QA Tools

```bash
# Inspect how diglot.py segments a single Markdown lesson
python tools/segment_preview.py corpus/why-reading-teaches-a-language.md

# Audit every lesson in DIGLOT_CORPUS for suspect segmentation spans
python tools/segment_qa.py
```

---

## 3. Windows Desktop Packaging & Installer (`packages/win/`)

The Windows desktop package embeds the exact same FastAPI engine + static ES-module UI into a single-process `pywebview` (Edge WebView2) window on an ephemeral loopback port (`127.0.0.1:0`).

### Key Files

| File | Role |
|---|---|
| `packages/win/diglota_win/shell.py` | Launches FastAPI/Uvicorn in-process on an ephemeral port, manages the single-instance mutex (`Local\Diglota.Desktop`), saves/restores DPI-aware window geometry in `data/ui.json`, and serves `splash.html` until `/api/status` responds. |
| `packages/win/diglota.spec` | PyInstaller one-dir spec bundling `packages/core/app`, `packages/ui/static` (recursive, including `js/`, `brand/`, `fonts/`), and `corpus/why-reading-teaches-a-language.md` into `dist/Diglota/`. |
| `packages/win/build.py` | Build driver for portable folder, uninstaller, payload zip, and setup exe. |
| `packages/win/installer.py` / `installer.spec` | Self-contained Tkinter installer (`Diglota-Setup-<VERSION>.exe`) embedding `build/payload.zip`. Preserves existing `data/` and `corpus/` files on upgrade. |
| `packages/win/uninstaller.py` / `uninstaller.spec` | Self-relocating uninstaller copied into the install folder. |
| `packages/win/setup_common.py` | Single source of truth for `VERSION`, zip-slip-safe extraction, shortcut creation, and process detection. |

### Build Commands

```bash
# 1. Portable desktop folder only -> dist/Diglota/Diglota.exe
python packages/win/build.py

# 2. Full release: portable folder + uninstaller + Diglota-Setup-<VERSION>.exe
python packages/win/build.py --installer

# 3. Rebuild only the installer from an existing dist/Diglota/
python packages/win/build.py --installer-only
```

### Silent Install & Uninstall CLI Flags

```powershell
# Unattended install
.\Diglota-Setup-1.1.0.exe --silent --dir "C:\Apps\Diglota" --no-desktop-shortcut

# Unattended uninstall (choose whether to keep or delete user library/data)
.\Uninstall-Diglota.exe --silent --keep-library
.\Uninstall-Diglota.exe --silent --delete-library
```

### Version Bump Checklist

When cutting a new release, keep versions aligned in:
1. [`packages/win/setup_common.py`](../packages/win/setup_common.py) (`VERSION`)
2. [`packages/core/app/api/__init__.py`](../packages/core/app/api/__init__.py) (`FastAPI(..., version=...)`)
3. [`README.md`](../README.md) (release notes & installer filename)

### Maintainer Export Tool (`tools/publish.py`)

`tools/publish.py` exports git-tracked files into a **separate** clean repository directory (`--target <other-dir>`). It refuses to run if `--target` resolves to the source repository itself.
