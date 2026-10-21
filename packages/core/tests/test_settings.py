"""Tests for the settings the reader sets in the app.

Two things matter here and they are different in kind.

The first is *precedence*: the ``.env`` is how the machine is configured and the
panel is how the person using it is, so what the panel writes has to win -- and
clearing a value has to mean "go back to the .env" rather than "use an empty one",
because clearing is how someone undoes a typo.

The second is *disclosure*. The browser is told whether a key is set and a hint that
identifies which one, and never the key itself: these tests assert that against the
serialised response, because a field that leaks a credential is the kind of thing
that gets added back by a well-meaning refactor.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402

SECRET = "sk-test-1234567890abcdef"


def a_settings(tmp_path, **env) -> Settings:
    """Settings whose file lives somewhere disposable, with the env part fixed.

    The environment's values are passed to the constructor, because that is where
    they come from in the app -- ``Settings.load()`` builds the dataclass from
    ``os.environ`` and only then overlays the file. They start empty so the machine
    running the tests cannot decide what they are testing.
    """
    values = {"llm_base_url": "", "llm_api_key": "", "llm_model": "", "typesafe_api_key": ""}
    values.update(env)
    return Settings(data_dir=tmp_path / "data", **values)


def test_what_the_panel_writes_is_read_back(tmp_path):
    settings = a_settings(tmp_path)
    assert settings.saved() == {}
    settings.update_saved({"llm_base_url": "https://api.example.com/v1/", "llm_model": "a-model",
                           "llm_api_key": SECRET})
    stored = json.loads(settings.settings_file.read_text(encoding="utf-8"))
    assert stored["llm_model"] == "a-model"
    assert stored["llm_base_url"] == "https://api.example.com/v1/"
    assert settings.saved()["llm_api_key"] == SECRET


def test_the_panel_wins_over_the_environment(tmp_path):
    """The reader's deliberate act in the app is the recent one, so it takes effect."""
    settings = a_settings(tmp_path, llm_base_url="https://from-env.example/v1", llm_model="env-model",
                          llm_api_key="env-key")
    assert settings.llm_from == "env"

    settings.update_saved({"llm_model": "app-model"})
    settings.apply_saved()
    assert settings.llm_model == "app-model"
    assert settings.llm_base_url == "https://from-env.example/v1", "untouched values stay as they were"
    assert settings.llm_from == "app"


def test_clearing_a_value_hands_it_back_to_the_environment(tmp_path):
    """Empty means "forget this", not "use an empty key" -- otherwise undoing a typo
    would leave a key that is present and wrong."""
    settings = a_settings(tmp_path, llm_api_key="env-key", llm_model="env-model")
    settings.update_saved({"llm_api_key": SECRET, "llm_model": "app-model"})
    assert settings.saved()["llm_api_key"] == SECRET

    settings.update_saved({"llm_api_key": "", "llm_model": ""})
    assert settings.saved() == {}
    settings.apply_saved()
    assert settings.llm_api_key == "env-key", "back to the .env value"
    assert settings.llm_model == "env-model"


def test_the_file_holds_only_what_the_panel_owns(tmp_path):
    """It is a file a reader can open, so it says what it is for and holds nothing
    else -- no corpus path, no proxy, nothing that a stray edit could break."""
    settings = a_settings(tmp_path)
    settings.update_saved({"llm_model": "a-model", "llm_api_key": SECRET,
                           "data_dir": "/somewhere/else", "proxy": "http://evil.example"})
    stored = json.loads(settings.settings_file.read_text(encoding="utf-8"))
    assert set(stored) == {"llm_model", "llm_api_key"}


def test_a_damaged_settings_file_does_not_stop_the_app(tmp_path):
    """A file a person can edit is a file a person can break, and losing the app
    because of it would be a bad trade for a convenience."""
    settings = a_settings(tmp_path, llm_model="env-model")
    settings.settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings.settings_file.write_text("{not json at all", encoding="utf-8")
    assert settings.saved() == {}
    settings.apply_saved()
    assert settings.llm_model == "env-model"

    settings.settings_file.write_text('["a list, not an object"]', encoding="utf-8")
    assert settings.saved() == {}


def test_the_hint_identifies_a_key_without_being_one(tmp_path):
    settings = a_settings(tmp_path, llm_api_key=SECRET)
    hint = settings.key_hint()
    assert hint and hint != SECRET
    assert SECRET not in hint
    assert len(hint) < len(SECRET) / 2
    # Short keys are not partially disclosed either -- there is nothing to hint at.
    short = a_settings(tmp_path, llm_api_key="abc123")
    assert short.key_hint() == "set"


def test_nothing_is_reported_as_ready_until_all_three_are_there(tmp_path):
    settings = a_settings(tmp_path)
    assert not settings.llm_ready
    settings.update_saved({"llm_base_url": "https://api.example.com/v1"})
    assert not settings.llm_ready, "a base URL with no key and no model is not ready"
    settings.update_saved({"llm_api_key": SECRET, "llm_model": "a-model"})
    assert settings.llm_ready
