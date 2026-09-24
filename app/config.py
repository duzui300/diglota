"""Configuration: where the corpus lives, where the database lives, and the keys.

The API keys are read from the settings panel in the app first, then from the first
``.env`` found walking up from this project, then from a machine-wide file
(``D:/aislop/.env`` here, which is where this machine keeps them). Nothing else in the app reads ``os.environ``
directly -- everything goes through :class:`Settings`, so there is one place to look
when a key is missing.

The panel writes to ``data/settings.json``, and what it writes wins: the ``.env`` is
how this *machine* is configured and the panel is how the person using it is, so
when both hold a value the recent, deliberate act in the app is the one that takes
effect. The panel says where each value came from, so there is never a question of
which one is in force.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("diglot")

# The settings the panel owns, in the order it shows them. Anything not listed here
# comes from the environment alone.
PANEL_KEYS = ("llm_base_url", "llm_api_key", "llm_model", "typesafe_api_key", "typesafe_model")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Where the diglot articles live. ``corpus/`` inside the project by default, so a
# fresh clone has somewhere obvious to put them, and ``DIGLOT_CORPUS`` moves it --
# the corpus is the reader's own shelf of hand-made lessons and is not shipped with
# the app. The app runs perfectly well with none: import a link and it writes a
# lesson of its own.
DEFAULT_CORPUS = PROJECT_ROOT / "corpus"
DEFAULT_DATA = PROJECT_ROOT / "data"

_ENV_CANDIDATES = (
    PROJECT_ROOT / ".env",
    Path("D:/aislop/.env"),
)


def corpus_dir() -> Path:
    """The corpus this machine is configured with.

    For the test suite and the corpus tools, which run against whatever corpus the
    developer has rather than a path written into the code. The tests that need a
    real corpus skip when there is not one, so a fresh clone runs green.
    """
    return Settings.load().corpus_dir


def load_env() -> Path | None:
    """Load the first ``.env`` that exists. Returns the path used, if any.

    ``override=False`` throughout: a variable already set in the real
    environment wins over the file, which is what you want when running tests
    or pointing the app at a different model for one session.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dependency is in requirements
        return None
    for path in _ENV_CANDIDATES:
        if path.is_file():
            load_dotenv(path, override=False)
            return path
    load_dotenv(override=False)
    return None


@dataclass
class Settings:
    corpus_dir: Path = field(default_factory=lambda: Path(os.environ.get("DIGLOT_CORPUS", DEFAULT_CORPUS)))
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("DIGLOT_DATA", DEFAULT_DATA)))
    llm_base_url: str = field(default_factory=lambda: os.environ.get("LLM_BASE_URL", "").rstrip("/"))
    # repr=False on both keys: a dataclass printed in a traceback, a log line or a
    # failing test would otherwise put the credential in the output. Nothing should
    # have to remember not to print them.
    llm_api_key: str = field(default_factory=lambda: os.environ.get("LLM_API_KEY", ""), repr=False)
    llm_model: str = field(default_factory=lambda: os.environ.get("LLM_MODEL", ""))
    typesafe_api_key: str = field(default_factory=lambda: os.environ.get("TYPESAFE_API_KEY", ""), repr=False)
    typesafe_model: str = field(default_factory=lambda: os.environ.get("TYPESAFE_MODEL", "jev-latest"))
    # Outbound web access on this machine only works through the local proxy;
    # direct connections time out. Empty string means "connect directly", which
    # is what you want anywhere else.
    proxy: str = field(default_factory=lambda: os.environ.get("DIGLOT_PROXY", "http://127.0.0.1:7890"))
    env_path: Path | None = None
    # The values the environment gave, kept so clearing a panel setting can put
    # them back: the file is an overlay, and an overlay needs something to sit on.
    _env: dict[str, str] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._env = {key: getattr(self, key) for key in PANEL_KEYS}

    @classmethod
    def load(cls) -> "Settings":
        env_path = load_env()
        settings = cls()
        settings.env_path = env_path
        settings.apply_saved()
        return settings

    # -- the settings panel ------------------------------------------------ #

    @property
    def settings_file(self) -> Path:
        """Where the panel writes. Beside the database, not in the project.

        Both are this reader's own state, and neither belongs in a checkout that
        might be committed or shared -- which is also why the key goes here rather
        than into ``.env``, a file that tends to be copied around.
        """
        return self.data_dir / "settings.json"

    def saved(self) -> dict[str, str]:
        """What the panel has set, ignoring anything else in the file."""
        try:
            data = json.loads(self.settings_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {key: str(data[key]).strip() for key in PANEL_KEYS
                if isinstance(data.get(key), str) and str(data[key]).strip()}

    def apply_saved(self) -> None:
        """Put the panel's values on top of the environment's.

        Idempotent and authoritative: every panel key is reset to what the
        environment gave and then overlain with the file, so clearing a setting
        genuinely goes back to the ``.env`` rather than leaving the old value in
        memory for the rest of the session.
        """
        for key in PANEL_KEYS:
            setattr(self, key, self._env.get(key, ""))
        for key, value in self.saved().items():
            setattr(self, key, value.rstrip("/") if key == "llm_base_url" else value)

    def update_saved(self, changes: dict[str, str]) -> dict[str, str]:
        """Set or clear panel values, and write the file.

        An empty string clears a value, which means "go back to the .env" rather
        than "use an empty one" -- the difference matters, because clearing is how
        someone undoes a typo.
        """
        current = self.saved()
        for key, value in changes.items():
            if key not in PANEL_KEYS:
                continue
            text = str(value or "").strip()
            if text:
                current[key] = text
            else:
                current.pop(key, None)
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        if current:
            self.settings_file.write_text(
                json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        else:
            # Nothing left to say: the file goes, rather than lingering as an empty
            # object for the next reader of the folder to wonder about.
            self.settings_file.unlink(missing_ok=True)
        # Applied here as well as written: "save these settings" means use them, and
        # a caller that had to remember a second step would eventually forget one.
        self.apply_saved()
        log.info("settings written: %s", ", ".join(sorted(current)) or "(nothing)")
        return current

    def key_hint(self) -> str:
        """Enough of the key for the reader to recognise which one is set, and no
        more: the browser is never sent key material, only this."""
        key = self.llm_api_key
        if not key:
            return ""
        return "set" if len(key) <= 8 else f"{key[:3]}…{key[-4:]}"

    @property
    def llm_from(self) -> str | None:
        """Where the model settings came from, for the panel to say out loud."""
        if set(self.saved()) & {"llm_api_key", "llm_base_url", "llm_model"}:
            return "app"
        return "env" if (self.llm_api_key or self.llm_base_url or self.llm_model) else None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "diglot.db"

    @property
    def library_dir(self) -> Path:
        """Where imported articles are written.

        They are stored as diglot Markdown -- the same format the hand-made
        corpus uses -- so an imported article can be moved into the corpus
        folder and read unchanged, and the parser that reads the corpus is also
        the one that reads everything the app generates.
        """
        return self.data_dir / "library"

    @property
    def llm_ready(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def judge_ready(self) -> bool:
        return bool(self.typesafe_api_key)

    def describe(self) -> dict[str, object]:
        """What the status endpoint reports. Never includes key material."""
        return {
            "env_file": str(self.env_path) if self.env_path else None,
            "corpus_dir": str(self.corpus_dir),
            "corpus_present": self.corpus_dir.is_dir(),
            "llm": {
                "ready": self.llm_ready,
                "model": self.llm_model or None,
                "base_url": self.llm_base_url or None,
                "from": self.llm_from,
            },
            "judge": {
                "ready": self.judge_ready,
                "model": self.typesafe_model or None,
            },
        }
