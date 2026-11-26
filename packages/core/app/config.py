"""Configuration: where the corpus lives, where the database lives, and the keys.

The API keys are read from the settings panel in the app first, then from the
project's ``.env`` (or an explicit ``DIGLOT_ENV_FILE`` path). Nothing else in
the app reads ``os.environ`` directly -- everything goes through
:class:`Settings`, so there is one place to look when a key is missing.

The panel writes to ``data/settings.json``, and what it writes wins: the ``.env``
is how this *machine* is configured and the panel is how the person using it is,
so when both hold a value the recent, deliberate act in the app is the one that
takes effect. The panel says where each value came from, so there is never a
question of which one is in force.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from .languages import DEFAULT_PAIR_ID

log = logging.getLogger("diglot")

# The settings the panel owns, in the order it shows them. Anything not listed here
# comes from the environment alone.
PANEL_KEYS = (
    "llm_base_url",
    "llm_api_key",
    "llm_model",
    "lookup_llm_base_url",
    "lookup_llm_api_key",
    "lookup_llm_model",
    "weave_llm_base_url",
    "weave_llm_api_key",
    "weave_llm_model",
    "warm_glosses",
    "typesafe_api_key",
    "typesafe_model",
)

LLM_ROLES = ("default", "lookup", "weave")

def _workspace_root() -> Path:
    """Where the reader's own things live: ``data/``, ``corpus/``, ``.env``.

    Two layouts reach this file. In a checkout it is
    ``<root>/packages/core/app/config.py``, so the root is three parents up. In a
    packaged desktop build the source sits inside the bundle but the reader's
    database must not: ``data/`` belongs next to the executable, where it is
    writable and visible, not inside PyInstaller's extraction directory. The
    frozen check is therefore about *writes*, not about resources -- those are
    found through :data:`BUNDLE_ROOT` instead.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


PROJECT_ROOT = _workspace_root()

# Read-only resources that ship with the app -- the web UI. In a checkout that is
# the repository itself; frozen, PyInstaller has unpacked the same tree under
# ``sys._MEIPASS``. Keeping the two roots apart is what lets an installed copy keep
# its ``data/`` beside the .exe while still finding its own HTML.
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
UI_DIR = BUNDLE_ROOT / "packages" / "ui" / "static"
STATIC_DIR = UI_DIR

# Where the diglot articles live. ``corpus/`` beside the project by default, so a
# fresh clone has somewhere obvious to put them, and ``DIGLOT_CORPUS`` moves it --
# the corpus is the reader's own shelf of hand-made lessons and is not shipped with
# the app. The app runs perfectly well with none: import a link and it writes a
# lesson of its own.
DEFAULT_CORPUS = PROJECT_ROOT / "corpus"
DEFAULT_DATA = PROJECT_ROOT / "data"

def _env_candidates() -> tuple[Path, ...]:
    paths: list[Path] = []
    explicit = os.environ.get("DIGLOT_ENV_FILE", "").strip()
    if explicit:
        paths.append(Path(explicit))
    paths.append(PROJECT_ROOT / ".env")
    return tuple(paths)


def validate_base_url(url: str) -> str:
    """Validate that a configured LLM base URL uses http(s) with a host."""
    cleaned = (url or "").strip().rstrip("/")
    if not cleaned:
        return ""
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("LLM base URL must start with http:// or https:// and include a host.")
    return cleaned


def is_local_base_url(url: str) -> bool:
    """True for localhost / loopback / LAN endpoints where API keys are often optional."""
    cleaned = (url or "").strip()
    if not cleaned:
        return False
    parsed = urllib.parse.urlparse(cleaned)
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private
    except ValueError:
        return False


def key_hint_for(key: str) -> str:
    """Enough of a key to recognize which one is set, never disclosing the secret."""
    if not key:
        return ""
    return "set" if len(key) <= 8 else f"{key[:3]}…{key[-4:]}"


@dataclass(frozen=True)
class LLMEndpoint:
    """Resolved connection parameters for one LLM role (default, lookup, or weave)."""

    role: str
    base_url: str
    api_key: str = field(repr=False)
    model: str
    inherited: bool
    source: str | None
    local: bool
    ready: bool


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
    for path in _env_candidates():
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
    # repr=False on all keys: a dataclass printed in a traceback, a log line or a
    # failing test would otherwise put the credential in the output. Nothing should
    # have to remember not to print them.
    llm_api_key: str = field(default_factory=lambda: os.environ.get("LLM_API_KEY", ""), repr=False)
    llm_model: str = field(default_factory=lambda: os.environ.get("LLM_MODEL", ""))
    lookup_llm_base_url: str = field(default_factory=lambda: os.environ.get("LOOKUP_LLM_BASE_URL", "").rstrip("/"))
    lookup_llm_api_key: str = field(default_factory=lambda: os.environ.get("LOOKUP_LLM_API_KEY", ""), repr=False)
    lookup_llm_model: str = field(default_factory=lambda: os.environ.get("LOOKUP_LLM_MODEL", ""))
    weave_llm_base_url: str = field(default_factory=lambda: os.environ.get("WEAVE_LLM_BASE_URL", "").rstrip("/"))
    weave_llm_api_key: str = field(default_factory=lambda: os.environ.get("WEAVE_LLM_API_KEY", ""), repr=False)
    weave_llm_model: str = field(default_factory=lambda: os.environ.get("WEAVE_LLM_MODEL", ""))
    # Background word pre-warming on article open: "auto" (off for local models
    # and on for remote cloud endpoints), "on", or "off".
    warm_glosses: str = field(default_factory=lambda: (os.environ.get("DIGLOT_WARM_GLOSSES", "off").strip().lower() or "off"))
    typesafe_api_key: str = field(default_factory=lambda: os.environ.get("TYPESAFE_API_KEY", ""), repr=False)
    typesafe_model: str = field(default_factory=lambda: os.environ.get("TYPESAFE_MODEL", "jev-latest"))
    # Outbound web access, for a machine that needs a proxy. **Empty means connect
    # directly**, which is right nearly everywhere -- so it defaults to empty and a
    # machine behind one sets DIGLOT_PROXY. It used to default to this machine's own
    # proxy, which meant a fresh clone failed every fetch with a connection error
    # until the reader worked out why.
    proxy: str = field(default_factory=lambda: os.environ.get("DIGLOT_PROXY", ""))
    active_pair: str = field(default_factory=lambda: os.environ.get("DIGLOT_PAIR", DEFAULT_PAIR_ID).strip().lower() or DEFAULT_PAIR_ID)
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
            setattr(self, key, value.rstrip("/") if key.endswith("_base_url") else value)

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
            payload = json.dumps(current, indent=2, sort_keys=True) + "\n"
            tmp = self.settings_file.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.settings_file)
        else:
            self.settings_file.unlink(missing_ok=True)
        # Applied here as well as written: "save these settings" means use them, and
        # a caller that had to remember a second step would eventually forget one.
        self.apply_saved()
        log.info("settings written: %s", ", ".join(sorted(current)) or "(nothing)")
        return current

    def key_hint(self, role: str = "default") -> str:
        """Enough of the key for the reader to recognise which one is set, and no
        more: the browser is never sent key material, only this."""
        if role == "default":
            return key_hint_for(self.llm_api_key)
        return key_hint_for(getattr(self, f"{role}_llm_api_key", ""))

    @property
    def llm_from(self) -> str | None:
        """Where the model settings came from, for the panel to say out loud."""
        if set(self.saved()) & {"llm_api_key", "llm_base_url", "llm_model"}:
            return "app"
        return "env" if (self.llm_api_key or self.llm_base_url or self.llm_model) else None

    def resolve_llm(self, role: str = "default") -> LLMEndpoint:
        """Resolve effective endpoint credentials for ``default``, ``lookup``, or ``weave``.

        Task roles inherit from ``default`` when left blank. If a task specifies
        a different ``base_url`` (e.g. a local Ollama/LM Studio server for word
        lookups while the default points at a cloud API), the default API key is
        never leaked to the other host, and localhost/LAN endpoints do not
        require an API key.
        """
        if role not in LLM_ROLES:
            raise ValueError(f"unknown LLM role: {role!r}")
        if role == "default":
            base_url = self.llm_base_url.rstrip("/")
            api_key = self.llm_api_key
            model = self.llm_model
            local = is_local_base_url(base_url)
            ready = bool(base_url and model and (api_key or local))
            return LLMEndpoint(
                role="default",
                base_url=base_url,
                api_key=api_key,
                model=model,
                inherited=False,
                source=self.llm_from,
                local=local,
                ready=ready,
            )

        prefix = f"{role}_llm_"
        own_url = getattr(self, f"{prefix}base_url").rstrip("/")
        own_key = getattr(self, f"{prefix}api_key")
        own_model = getattr(self, f"{prefix}model")
        has_override = bool(own_url or own_key or own_model)

        if not has_override:
            default = self.resolve_llm("default")
            return LLMEndpoint(
                role=role,
                base_url=default.base_url,
                api_key=default.api_key,
                model=default.model,
                inherited=True,
                source=default.source,
                local=default.local,
                ready=default.ready,
            )

        default_url = self.llm_base_url.rstrip("/")
        base_url = own_url or default_url
        same_endpoint = (not own_url) or (own_url == default_url)
        api_key = own_key or (self.llm_api_key if same_endpoint else "")
        model = own_model or self.llm_model
        local = is_local_base_url(base_url)
        ready = bool(base_url and model and (api_key or local))
        saved_keys = set(self.saved())
        role_keys = {f"{prefix}base_url", f"{prefix}api_key", f"{prefix}model"}
        source = "app" if (saved_keys & role_keys) else "env"
        return LLMEndpoint(
            role=role,
            base_url=base_url,
            api_key=api_key,
            model=model,
            inherited=False,
            source=source,
            local=local,
            ready=ready,
        )

    def role_settings(self, role: str) -> dict[str, object]:
        """Safe, non-secret settings view for ``GET /api/settings``."""
        endpoint = self.resolve_llm(role)
        if role == "default":
            return {
                "base_url": self.llm_base_url,
                "model": self.llm_model,
                "ready": endpoint.ready,
                "local": endpoint.local,
                "key_set": bool(self.llm_api_key),
                "key_hint": key_hint_for(self.llm_api_key),
                "from": self.llm_from,
            }
        prefix = f"{role}_llm_"
        own_url = getattr(self, f"{prefix}base_url")
        own_key = getattr(self, f"{prefix}api_key")
        own_model = getattr(self, f"{prefix}model")
        return {
            "base_url": own_url,
            "model": own_model,
            "effective_base_url": endpoint.base_url,
            "effective_model": endpoint.model,
            "override": not endpoint.inherited,
            "ready": endpoint.ready,
            "local": endpoint.local,
            "key_set": bool(own_key),
            "key_hint": key_hint_for(own_key),
            "effective_key_set": bool(endpoint.api_key),
            "from": None if endpoint.inherited else endpoint.source,
        }

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
        return self.resolve_llm("default").ready

    @property
    def lookup_llm_ready(self) -> bool:
        return self.resolve_llm("lookup").ready

    @property
    def weave_llm_ready(self) -> bool:
        return self.resolve_llm("weave").ready

    def should_warm_glosses(self) -> bool:
        """Whether opening an article should pre-warm unknown words in the background.

        Defaults to off (on-demand lookup saves ~80% lookup tokens and avoids
        flooding single-slot local servers like Ollama/LM Studio). Can be set
        to ``"on"`` or ``"auto"`` (remote endpoints only) in Settings or ``.env``.
        """
        mode = (self.warm_glosses or "off").strip().lower()
        if mode in ("on", "true", "1", "yes"):
            return True
        if mode == "auto":
            lookup = self.resolve_llm("lookup")
            return lookup.ready and not lookup.local
        return False

    @property
    def judge_ready(self) -> bool:
        return bool(self.typesafe_api_key)

    def describe(self) -> dict[str, object]:
        """What the status endpoint reports. Never includes key material."""
        default = self.resolve_llm("default")
        lookup = self.resolve_llm("lookup")
        weave = self.resolve_llm("weave")
        return {
            "env_file": str(self.env_path) if self.env_path else None,
            "corpus_dir": str(self.corpus_dir),
            "corpus_present": self.corpus_dir.is_dir(),
            "llm": {
                "ready": default.ready,
                "model": default.model or None,
                "base_url": default.base_url or None,
                "from": self.llm_from,
                "lookup": {
                    "ready": lookup.ready,
                    "model": lookup.model or None,
                    "override": not lookup.inherited,
                },
                "weave": {
                    "ready": weave.ready,
                    "model": weave.model or None,
                    "override": not weave.inherited,
                },
            },
            "judge": {
                "ready": self.judge_ready,
                "model": self.typesafe_model or None,
            },
            "language_pair": self.active_pair,
        }
