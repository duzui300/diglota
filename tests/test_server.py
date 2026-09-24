"""Tests for the server's own logic, with the models faked out.

Everything else in the suite tests a pure module. The server had no fast tests
at all -- its branching was only exercised by the browser suite, which needs a
live model, a running server and four minutes. That is the wrong shape for
testing things like "does a deep lookup bypass the glossary" or "is a failure
remembered", which are decisions, not integrations.

No HTTP here either: the routes are thin wrappers, and driving them through
TestClient would add a dependency while testing FastAPI rather than this app.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.llm import LLMError  # noqa: E402
from app.server import App, _job_title, _writing_prompt  # noqa: E402
from app.tutor import PROMPT_VERSION, cache_key  # noqa: E402

CORPUS_ARTICLE = """### Article Identification & Preview

- **Article Title:** A Test Article
- **Author:** Someone

# A Test Article

**By Someone**

El arte se vuelve **más personal** (*more personal*) cada vez, y los anales (*annals*)
de la historia del arte **pintan** (*they paint*) un panorama diferente.

---

### POST-READING ANCHORS

**Recycled Vocabulary Box**

- **volverse** / **se vuelve** (*to become*)
"""


class FakeTutor:
    """Records what it was asked, and answers in the shape the real one does."""

    def __init__(self, *, fail: bool = False) -> None:
        self.asked: list[str] = []
        self.fail = fail

    def gloss_word(self, *, word: str, sentence: str, article_title: str) -> dict:
        self.asked.append(word)
        if self.fail:
            raise LLMError("the tutor is unreachable")
        return {
            "lemma": word.lower(), "display": word, "pos": "noun",
            "gloss": f"meaning of {word}", "sense": "a sense",
            "note": "a note", "related": [],
        }


@pytest.fixture()
def server(tmp_path, monkeypatch):
    """An App with its own corpus, database and settings, and no AI configured.

    The model settings are removed from the environment first: once any test has
    loaded a ``.env``, ``os.environ`` keeps those values for the rest of the run, so
    without this a test asserting "no model is configured" would pass or fail
    depending on whether the machine running it happens to have a key. The tutor is
    a fake, assigned below.
    """
    for name in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a-test-article.md").write_text(CORPUS_ARTICLE, encoding="utf-8")

    settings = Settings(corpus_dir=corpus, data_dir=tmp_path / "data")
    application = App(settings)
    application.library.refresh()
    application.tutor = FakeTutor()
    yield application
    application.jobs.stop()
    application.store.close()


# ---------------------------------------------------------------- job titles --


def test_job_title_reads_like_a_title_not_a_url():
    title = _job_title("https://en.wikipedia.org/wiki/Why_We_Sleep", None)
    assert title == "Why We Sleep · en.wikipedia.org"


def test_job_title_prefers_an_explicit_hint():
    assert _job_title("https://example.com/x", "Given Title") == "Given Title"


def test_job_title_survives_a_bare_hostname():
    assert _job_title("example.com", None) == "example.com"


def test_job_title_handles_nonsense():
    assert _job_title("", None) == ""


# --------------------------------------------------------------- resolution --


def test_a_corpus_word_is_answered_without_touching_the_model(server):
    """The whole point of the glossary: the lesson's own translation, for free."""
    result = server.resolve_word("anales", "los anales de la historia", "a-test-article")
    assert result["gloss"] == "annals"
    assert result["model"] is False
    assert server.tutor.asked == []


def test_an_inflection_finds_the_corpus_entry(server):
    server.resolve_word("vuelve", "", None)
    # "volverse" is in the vocabulary box; clicking an inflection must reach it.
    result = server.resolve_word("se vuelve", "", None)
    assert result["model"] is False
    assert "become" in (result["gloss"] or "")


def test_an_unknown_word_reaches_the_tutor(server):
    result = server.resolve_word("hipopótamo", "el hipopótamo come", None)
    assert result["model"] is True
    assert result["gloss"] == "meaning of hipopótamo"
    assert server.tutor.asked == ["hipopótamo"]


def test_a_deep_lookup_asks_the_tutor_even_when_the_corpus_can_answer(server):
    """The corpus gives a translation and nothing else. A reader who wants the
    lemma and the example should not be blocked by a shorter answer existing."""
    shallow = server.resolve_word("anales", "", None)
    assert shallow["model"] is False and "pos" not in shallow

    deep = server.resolve_word("anales", "", None, deep=True)
    assert deep["model"] is True
    assert deep["pos"] == "noun"
    assert server.tutor.asked == ["anales"]


def test_a_tutor_entry_outranks_the_corpus_afterwards(server):
    """Having paid for the fuller answer once, the reader keeps it."""
    server.resolve_word("anales", "", None, deep=True)
    again = server.resolve_word("anales", "", None)
    assert again["model"] is True or again["pos"] == "noun", again
    assert again["instant"] is True, "the second call must not hit the model again"
    assert server.tutor.asked == ["anales"], "asked only once"


def test_a_failure_is_remembered_rather_than_retried_every_click(server):
    """An outage must not cost the full timeout on every click of the same word."""
    server.tutor = FakeTutor(fail=True)
    first = server.resolve_word("hipopótamo", "", None)
    assert first.get("error")
    assert len(server.tutor.asked) == 1

    second = server.resolve_word("hipopótamo", "", None)
    assert second.get("error")
    assert len(server.tutor.asked) == 1, "the second click should not call out again"


def test_a_remembered_failure_expires(server):
    """...but the app has to heal itself once the endpoint comes back."""
    from app.store import now_iso

    key = cache_key("gloss", "hipopótamo")
    server.store.cache_put(key, {"gloss": None, "failed": True})
    # Backdate it beyond the failure TTL.
    with server.store.write() as conn:
        conn.execute("UPDATE cache SET created_at = ? WHERE key = ?", ("2020-01-01T00:00:00+00:00", key))

    result = server.resolve_word("hipopótamo", "", None)
    assert result["model"] is True, "a stale failure should not block a fresh attempt"


def test_a_glossless_tutor_is_reported_not_crashed(server):
    server.tutor = None
    result = server.resolve_word("hipopótamo", "", None)
    assert "not configured" in result["error"]


# -------------------------------------------------------------------- cache --


def test_cache_keys_carry_their_prompt_version():
    """Without this, improving a prompt leaves every cached answer serving the
    old one -- which is exactly what happened to the explain prompt."""
    key = cache_key("explain", "some text", "")
    assert f"v{PROMPT_VERSION['explain']}" in key
    assert key.startswith("explain:")


def test_bumping_a_prompt_version_changes_the_key():
    import app.tutor as tutor_module

    before = cache_key("quiz", "abc")
    original = tutor_module.PROMPT_VERSION["quiz"]
    try:
        tutor_module.PROMPT_VERSION["quiz"] = original + 1
        after = cache_key("quiz", "abc")
    finally:
        tutor_module.PROMPT_VERSION["quiz"] = original
    assert before != after


def test_every_ai_feature_has_a_declared_version():
    for feature in ("gloss", "explain", "translate", "quiz", "drills", "feedback", "suggest",
                    "weave", "anchors", "english"):
        assert feature in PROMPT_VERSION, feature
        assert PROMPT_VERSION[feature] >= 1


# ----------------------------------------------------------------- glossary --


def test_the_glossary_rebuilds_when_the_deck_changes(server):
    server.ensure_glossary()
    assert server.glossary.lookup("genoma") is None

    server.store.add_word(term="el genoma", lemma="genoma", gloss="the genome")
    # Rebuild happens on access, which is what every endpoint does.
    server.ensure_glossary()
    assert server.glossary.lookup("genoma").gloss == "the genome", "deck words are indexed"


def test_a_saved_word_is_answerable_without_the_model(server):
    """The deck feeds the glossary, so a word the learner saved weeks ago is
    answered instantly rather than costing a model call it already paid for."""
    server.store.add_word(term="el genoma", lemma="genoma", gloss="the genome")
    result = server.resolve_word("genoma", "", None)
    assert result["gloss"] == "the genome"
    assert result["model"] is False
    assert server.tutor.asked == []


def test_the_glossary_rebuilds_when_the_library_changes(server, tmp_path):
    server.ensure_glossary()
    assert server.glossary.lookup("hipopótamo") is None

    (server.settings.library_dir).mkdir(parents=True, exist_ok=True)
    (server.settings.library_dir / "imported.md").write_text(
        CORPUS_ARTICLE.replace("A Test Article", "An Imported Article")
        .replace("(*annals*)", "(*annals*)").replace("los anales", "los anales")
        .replace("**pintan** (*they paint*)", "**hipopótamos** (*hippos*)"),
        encoding="utf-8",
    )
    server.library.refresh()
    server.ensure_glossary()
    assert server.glossary.lookup("hipopótamos") is not None


# ------------------------------------------------------------- the writing --


def test_the_writing_prompt_survives_a_finished_article(server):
    """`_writing_prompt` called `recommend.topics_from_titles`, but the name
    `recommend` in the server is bound to the recommend *function* -- the module
    was shadowed by its own import. So `/api/writing` raised AttributeError for
    any reader who had finished an article, and the browser suite crashed on that
    line instead of reporting a failure, which is how it went unnoticed.

    The finished-article case is the one that broke, so it is the one tested.
    """
    server.store.save_progress("a-test-article", position=1.0, completed=True)
    prompt = _writing_prompt(server).to_dict()
    assert set(prompt) == {"topic", "words", "instruction", "source"}
    # The topic is read off the title of what was read, which is the only path
    # that reached the missing name -- so a topic here is the proof it ran.
    assert "article" in prompt["topic"], prompt


def test_the_writing_prompt_works_before_anything_is_finished(server):
    """A reader who has read nothing still gets something to write about."""
    prompt = _writing_prompt(server).to_dict()
    assert prompt["instruction"]


def test_a_saved_word_becomes_something_to_write_about(server):
    server.store.add_word(term="el genoma", lemma="genoma", gloss="the genome")
    prompt = _writing_prompt(server).to_dict()
    assert "el genoma" in prompt["words"] or "genoma" in prompt["words"], prompt


# ------------------------------------------------------------ the deck page --


@pytest.fixture()
def deck(server, monkeypatch):
    """The deck route, with a few words that came from the corpus article."""
    from app import server as server_module

    monkeypatch.setattr(server_module, "ctx", lambda: server)
    server.store.add_word(term="cuestión", lemma="cuestión", gloss="question",
                          context="la cuestión del arte moderno", article_slug="a-test-article")
    server.store.add_word(term="el genoma", lemma="genoma", gloss="the genome")
    return server_module.words


def test_the_deck_page_searches_the_way_the_rest_of_the_app_does(deck):
    """Accents fold, as they do in the quotes search. A reader who types
    "cuestion" expecting "cuestión" was taught that by the quotes box, and a
    second notion of "the same word" for the deck would be a small betrayal."""
    assert deck(search="cuestion")["matched"] == 1
    assert deck(search="cuestión")["matched"] == 1
    assert deck(search="question")["matched"] == 1, "the gloss is searchable"
    assert deck(search="genoma")["matched"] == 1


def test_the_deck_page_searches_the_sentence_and_the_article(deck):
    """"Which of my words came out of that piece" is a question worth being able
    to ask, and the sentence is what the card reviews you on."""
    assert deck(search="arte moderno")["matched"] == 1
    assert deck(search="test article")["matched"] == 1
    assert deck(search="nothing like this")["matched"] == 0


def test_every_row_says_what_it_is_and_where_it_came_from(deck):
    rows = deck()["words"]
    assert len(rows) == 2
    by_term = {row["term"]: row for row in rows}
    assert by_term["cuestión"]["article_title"] == "A Test Article"
    assert by_term["cuestión"]["stage"] == "new"
    assert by_term["cuestión"]["due_at"], "a new card is due now, not never"
    assert by_term["el genoma"]["article_title"] == "", "saved with no article behind it"


def test_the_page_is_told_the_real_total(deck):
    """A silent cap reads as "this is everything you have"."""
    payload = deck()
    assert payload["total"] == 2 and payload["limit"] >= 2


# ---------------------------------------------------------------- register --


def test_a_register_the_reader_sets_is_written_into_the_passage(server, monkeypatch):
    """The tag lives in the file rather than beside it, so this has to be a real
    edit. A tag the reader sets in the app and cannot see in the file would be a
    second place for one fact to live, with a stale copy waiting to happen."""
    from app import server as server_module

    monkeypatch.setattr(server_module, "ctx", lambda: server)
    entry = server.library.get("a-test-article")
    assert entry is not None and entry.path is not None

    result = server_module.set_register("a-test-article", server_module.RegisterBody(code="essay"))
    assert result["register"] == "essay" and result["inferred"] is False
    assert result["blurb"], "the picker shows the blurb beside each choice"

    written = entry.path.read_text(encoding="utf-8")
    assert "- **Register:** essay" in written
    assert "- **Article Title:** A Test Article" in written, "the rest of the block survives"

    # ...and the shelf agrees, because the library was re-read rather than patched.
    again = server.library.get("a-test-article")
    assert again is not None and again.register() == ("essay", False)


def test_setting_it_twice_replaces_rather_than_stacking(server, monkeypatch):
    from app import server as server_module

    monkeypatch.setattr(server_module, "ctx", lambda: server)
    entry = server.library.get("a-test-article")
    server_module.set_register("a-test-article", server_module.RegisterBody(code="news"))
    server_module.set_register("a-test-article", server_module.RegisterBody(code="academic"))
    written = entry.path.read_text(encoding="utf-8")
    assert written.count("**Register:**") == 1
    assert "- **Register:** academic" in written


def test_clearing_it_hands_the_decision_back_to_the_app(server, monkeypatch):
    from app import server as server_module

    monkeypatch.setattr(server_module, "ctx", lambda: server)
    server_module.set_register("a-test-article", server_module.RegisterBody(code="fiction"))
    cleared = server_module.set_register("a-test-article", server_module.RegisterBody(code=""))
    assert "- **Register:**" not in server.library.get("a-test-article").path.read_text(encoding="utf-8")
    # Nothing declared, so whatever is shown from now on is a guess and is marked
    # as one -- which is a different claim from "the author said so".
    assert cleared["inferred"] is True


def test_an_unknown_register_is_refused(server, monkeypatch):
    from fastapi import HTTPException

    from app import server as server_module

    monkeypatch.setattr(server_module, "ctx", lambda: server)
    with pytest.raises(HTTPException) as caught:
        server_module.set_register("a-test-article", server_module.RegisterBody(code="poetry"))
    assert caught.value.status_code == 400


def test_an_unknown_passage_is_refused(server, monkeypatch):
    from fastapi import HTTPException

    from app import server as server_module

    monkeypatch.setattr(server_module, "ctx", lambda: server)
    with pytest.raises(HTTPException) as caught:
        server_module.set_register("no-such-article", server_module.RegisterBody(code="essay"))
    assert caught.value.status_code == 404


# ------------------------------------------------------- deleting a lesson --


def test_deleting_a_lesson_takes_its_reading_progress_but_not_the_readers_words(server, monkeypatch, tmp_path):
    """The position is about a passage that no longer exists, so it goes. A saved
    word is the reader's knowledge, so it stays -- the article a word came from is
    a note in the margin, not a parent."""
    from app import server as server_module

    monkeypatch.setattr(server_module, "ctx", lambda: server)
    folder = tmp_path / "data" / "library"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "an-imported-lesson-abc123.md"
    path.write_text(CORPUS_ARTICLE.replace("A Test Article", "An Imported Lesson"),
                    encoding="utf-8")
    server.library.refresh()
    slug = "an-imported-lesson-abc123"
    server.store.save_progress(slug, position=0.5, seconds=60)
    server.store.add_word(term="anales", lemma="anales", gloss="annals", article_slug=slug)

    result = server_module.delete_article(slug)
    assert not path.exists() and result["removed"].endswith("an-imported-lesson-abc123.md")
    assert server.library.get(slug) is None
    assert server.store.progress_for(slug)["position"] == 0.0
    assert server.store.word_count() == 1, "the deck is not the lesson's to delete"
    assert result["words_kept"] == 1


def test_a_passage_from_the_readers_own_corpus_is_never_deleted(server, monkeypatch):
    """It is their file. The answer says where it is, because that is the only one
    that cannot lose something someone typed by hand."""
    from fastapi import HTTPException

    from app import server as server_module

    monkeypatch.setattr(server_module, "ctx", lambda: server)
    entry = server.library.get("a-test-article")
    assert entry is not None and entry.path is not None and entry.path.exists()
    with pytest.raises(HTTPException) as caught:
        server_module.delete_article("a-test-article")
    assert caught.value.status_code == 409
    assert "your own file" in caught.value.detail
    assert entry.path.exists(), "the file is still there"


def test_deleting_something_that_is_not_there_is_a_404(server, monkeypatch):
    from fastapi import HTTPException

    from app import server as server_module

    monkeypatch.setattr(server_module, "ctx", lambda: server)
    with pytest.raises(HTTPException) as caught:
        server_module.delete_article("no-such-lesson")
    assert caught.value.status_code == 404


# ---------------------------------------------------------------- settings --


def test_the_settings_panel_is_never_sent_the_key(server, monkeypatch):
    """The browser is told whether a key is set and a hint that identifies which one.
    The key itself stays on this machine -- asserted against the *serialised*
    response, because a field that leaks a credential is exactly what a
    well-meaning refactor adds back."""
    import json

    from app import server as server_module

    monkeypatch.setattr(server_module, "ctx", lambda: server)
    secret = "sk-test-abcdefghijklmnop"
    server.settings.update_saved({"llm_base_url": "https://api.example.com/v1",
                                 "llm_model": "a-model", "llm_api_key": secret})
    payload = server_module.get_settings()
    assert secret not in json.dumps(payload)
    assert payload["llm"]["key_set"] is True and payload["llm"]["ready"] is True
    assert payload["llm"]["key_hint"] and secret not in payload["llm"]["key_hint"]
    assert payload["llm"]["from"] == "app"


def configured_app(tmp_path, monkeypatch) -> tuple:
    """An App built on a machine whose .env holds model settings.

    The environment has to be set *before* the app is constructed, because that is
    when the app reads it -- setting it afterwards would describe a machine that
    cannot exist. Returns the app and the server module for monkeypatching.
    """
    from app import server as server_module
    from app.config import Settings

    monkeypatch.setenv("LLM_BASE_URL", "https://from-env.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "env-key")
    monkeypatch.setenv("LLM_MODEL", "env-model")
    corpus = tmp_path / "corpus"
    corpus.mkdir(exist_ok=True)
    (corpus / "a-test-article.md").write_text(CORPUS_ARTICLE, encoding="utf-8")
    application = App(Settings(corpus_dir=corpus, data_dir=tmp_path / "data"))
    application.library.refresh()
    monkeypatch.setattr(server_module, "ctx", lambda: application)
    return application, server_module


def test_saving_settings_puts_them_to_work_without_a_restart(tmp_path, monkeypatch):
    """Someone who has just pasted a key wants to know whether it works, and the
    launcher they started the app with has no terminal to restart it from."""
    application, server_module = configured_app(tmp_path, monkeypatch)
    assert application.settings.llm_from == "env", "as configured to begin with"

    result = server_module.save_settings(server_module.SettingsBody(
        llm_base_url="https://api.example.com/v1/", llm_api_key="sk-test-abcdefghijkl",
        llm_model="a-model"))
    assert result["llm"]["ready"] is True
    assert application.llm is not None and application.tutor is not None, "rebuilt, not restarted"
    assert application.settings.llm_base_url == "https://api.example.com/v1", "trailing slash trimmed"
    assert application.settings.llm_model == "a-model", "and it wins over the .env"
    assert application.settings.settings_file.exists()
    assert application.settings.llm_from == "app"
    application.jobs.stop()
    application.store.close()


def test_clearing_in_the_panel_gives_the_environment_back(tmp_path, monkeypatch):
    """Clearing is how someone undoes a typo, so it has to mean "forget this" and
    fall back -- not "use an empty key", which would leave the app configured with
    something present and wrong."""
    application, server_module = configured_app(tmp_path, monkeypatch)

    server_module.save_settings(server_module.SettingsBody(llm_model="a-model"))
    assert application.settings.llm_from == "app" and application.settings.llm_model == "a-model"

    server_module.save_settings(server_module.SettingsBody(llm_model=""))
    assert not application.settings.saved(), "the file no longer claims to set anything"
    assert application.settings.llm_model == "env-model", "back to the .env value"
    assert application.settings.llm_from == "env"
    assert application.tutor is not None, "and the client is rebuilt from what is left"
    application.jobs.stop()
    application.store.close()


def test_a_reset_clears_the_records_and_leaves_the_lessons(server, monkeypatch, tmp_path):
    """Lessons are files, not rows. What goes is everything the app measured."""
    from app import server as server_module

    monkeypatch.setattr(server_module, "ctx", lambda: server)
    server.store.add_word(term="anales", lemma="anales", gloss="annals")
    server.store.add_word(term="genoma", lemma="genoma", gloss="genome")
    server.store.save_progress("a-test-article", position=0.8, seconds=120, completed=True)
    server.store.log_event("review")

    result = server_module.reset_progress()
    assert result["cleared"]["words"] == 2
    assert server.store.word_count() == 0
    assert server.store.progress_for("a-test-article")["completed_at"] is None
    assert server.store.stats()["reviews_total"] == 0
    # ...and the library is exactly as it was.
    assert server.library.get("a-test-article") is not None
    assert result["kept"]["articles"] == len(server.library)


def test_a_reset_copies_the_database_first(server, monkeypatch, tmp_path):
    """The one button that can lose months of work has to be recoverable, and the
    copy has to be *consistent*: reading the .db file alone would miss whatever is
    still in the write-ahead log, which after a session is most of the recent rows."""
    import sqlite3

    from app import server as server_module

    monkeypatch.setattr(server_module, "ctx", lambda: server)
    for index in range(5):
        server.store.add_word(term=f"palabra{index}", lemma=f"palabra{index}", gloss=f"word {index}")

    result = server_module.reset_progress()
    backup = Path(result["backup"])
    assert backup.exists() and backup.parent.name == "backups"
    assert server.store.word_count() == 0

    # The backup holds what was cleared, WAL and all.
    saved = sqlite3.connect(backup)
    try:
        assert saved.execute("SELECT COUNT(*) FROM words").fetchone()[0] == 5
    finally:
        saved.close()


# ---------------------------------------------------- a lesson from writing --


def test_a_lesson_from_your_own_writing_saves_the_version_it_weaves(server, monkeypatch):
    """The lesson refers back to a revision the reader can actually see.

    Without the save, the lesson would be the only copy of that text -- and
    re-weaving the same piece at a different amount would leave nothing to compare
    the two lessons against.
    """
    from app import server as server_module

    monkeypatch.setattr(server_module, "ctx", lambda: server)
    queued: dict = {}

    class Job:
        id = "job-1"
        status = "queued"

    def fake_start(self, text, title, level, ratio, weave):
        queued.update(text=text, title=title, level=level, ratio=ratio, weave=weave)
        return Job()

    monkeypatch.setattr(server_module.App, "start_lesson_from_writing", fake_start)

    result = server_module.writing_lesson(server_module.LessonBody(
        text="Escribí sobre el río y el paseo largo de la tarde. " * 5,
        level="B1", ratio=0.38, weave="sentence",
    ))
    assert result["job_id"] == "job-1"
    piece = server.store.writing(result["writing_id"])
    assert piece["revisions"], "the draft has to become a version of the piece"
    assert result["revision"]["text"].startswith("Escribí")
    assert queued["text"].startswith("Escribí")
    assert queued["weave"] == "sentence" and queued["ratio"] == 0.38
    assert queued["title"], "a job with no title is a blank row in the Activity panel"


def test_a_lesson_from_nothing_is_refused(server, monkeypatch):
    from app import server as server_module
    from fastapi import HTTPException

    monkeypatch.setattr(server_module, "ctx", lambda: server)
    with pytest.raises(HTTPException) as caught:
        server_module.writing_lesson(server_module.LessonBody(text="   "))
    assert caught.value.status_code == 400
