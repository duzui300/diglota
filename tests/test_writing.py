"""Tests for the writing workspace.

Writing is the one thing in this app the reader produces rather than consumes,
and it is now a loop rather than a form: draft, read it, revise, read it again.
Three properties carry the feature.

**The prompt comes from their own material.** A prompt assembled from what they
keep looking up is both more useful and the only kind that can be marked -- the
app knows which words went in, so it can say which came out.

**The offline half is exact and always available.** Length, language mix and
which prompted words appeared are measured with the same segmenter the reader
reads with, and no model can make them wrong.

**Nothing is applied on the reader's behalf.** A suggestion is a suggestion, an
annotation must be true of the text it is placed on, and a version is only ever
what they actually wrote.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import writing  # noqa: E402

SPANISH = ("Los modelos de lenguaje han cambiado la manera en que escribimos. "
           "El arte se vuelve más personal cada vez, y los pintores analizan su trabajo.")
MIXED = ("I think that language models are interesting. Los modelos de lenguaje "
         "han cambiado la manera en que escribimos todos los días.")
ENGLISH = "This is an English paragraph about nothing in particular, written by mistake."


# ------------------------------------------------------------------- the prompt --


def test_the_prompt_prefers_the_words_the_reader_keeps_looking_up():
    prompt = writing.prompt_for(topics=["art"], shaky=["volverse", "pintar"],
                                saved=["mesa"], quotes=["cámara"])
    # Shaky words first, and the solid one fills the last slot -- a prompt that
    # is all words you cannot reach for is a prompt you do not finish.
    assert prompt.words == ["volverse", "pintar", "mesa"]
    assert "art" in prompt.instruction
    assert prompt.source == "the words you keep looking up", "and it credits the main source"


def test_the_prompt_falls_back_through_saved_words_and_quotes():
    saved = writing.prompt_for(saved=["mesa", "silla"])
    assert saved.words == ["mesa", "silla"] and "saved" in saved.source
    quoted = writing.prompt_for(quotes=["cámara"])
    assert quoted.words == ["cámara"] and "kept" in quoted.source


def test_the_prompt_does_not_ask_for_the_same_word_twice():
    """``volverse`` and ``se vuelve`` are one word -- the app's own vocabulary box
    writes them as one entry. Asking for both makes the prompt look careless.

    The check is the app's own stemmer, so it collapses exactly what the rest of
    the app treats as one word: reflexives and plurals. It does not collapse an
    accented future (``volverá``) because the stemmer does not either, and being
    consistent with the shared notion of "same word" matters more than catching
    every derivation.
    """
    assert writing.prompt_for(shaky=["volverse", "se vuelve", "volver"]).words == ["volverse"]
    assert writing.prompt_for(shaky=["casa", "casas"]).words == ["casa"]


def test_the_prompt_survives_having_almost_nothing_to_go_on():
    prompt = writing.prompt_for()
    assert prompt.instruction and prompt.words == []
    assert "anything you have read" in prompt.instruction
    topic_only = writing.prompt_for(topics=["sleep"])
    assert "sleep" in topic_only.instruction and "dictionary" in topic_only.instruction


def test_the_prompt_names_a_length():
    assert str(writing.MIN_WORDS) in writing.prompt_for(topics=["art"], shaky=["pintar"]).instruction


# ------------------------------------------------------------------ measuring --


def test_an_empty_piece_measures_as_empty():
    reading = writing.read("")
    assert reading.words == 0 and reading.sentences == 0
    assert reading.spanish_share == 0.0 and reading.enough is False
    assert writing.read("   \n  ").words == 0
    assert writing.usage_note(reading) == "Nothing written yet."


def test_the_language_mix_is_measured_with_the_reader_s_own_segmenter():
    reading = writing.read(MIXED)
    assert reading.spanish_words > 0 and reading.english_words > 0
    assert 0 < reading.spanish_share < 1
    assert writing.read(SPANISH).spanish_share > 0.9
    assert writing.read(ENGLISH).spanish_share < 0.2


def test_a_piece_is_measured_for_length_and_variety():
    reading = writing.read(SPANISH)
    assert reading.words == len(SPANISH.split())
    assert reading.sentences >= 2 and reading.distinct > 5
    assert reading.enough is (reading.words >= writing.MIN_WORDS)


def test_the_prompted_words_that_appeared_are_reported():
    prompt = writing.prompt_for(shaky=["volverse", "analizan", "cámara"])
    reading = writing.read(SPANISH, prompt=prompt)
    assert "analizan" in reading.used and "cámara" in reading.missed
    assert set(reading.used) | set(reading.missed) == set(prompt.words)


def test_an_inflected_form_counts_as_using_the_word():
    """A learner who writes ``se vuelve`` has used ``volverse``. A string match
    would tell them they had not, which is the kind of wrong answer that teaches
    the wrong thing."""
    prompt = writing.prompt_for(shaky=["volverse"])
    assert writing.read(SPANISH, prompt=prompt).used == ["volverse"]


def test_measuring_never_raises_on_odd_input():
    for text in ("", "   ", "1234", "!!!", "¿?", "a" * 5000, "ÿ" * 100, "\n\n\n"):
        reading = writing.read(text, prompt=writing.prompt_for(shaky=["arte"]))
        assert reading.words >= 0 and 0.0 <= reading.spanish_share <= 1.0


def test_the_note_names_english_first_because_that_is_the_useful_thing():
    assert "English" in writing.usage_note(writing.read(ENGLISH))


def test_a_short_piece_is_nudged_rather_than_scored():
    note = writing.usage_note(writing.read("El arte es bello y bueno."))
    assert "words" in note and "further" in note


def test_using_every_prompted_word_is_said_out_loud():
    prompt = writing.prompt_for(shaky=["arte", "analizan"])
    note = writing.usage_note(writing.read(SPANISH + " " + SPANISH, prompt=prompt))
    assert "Every prompted word" in note


def test_missing_some_words_says_which():
    prompt = writing.prompt_for(shaky=["arte", "cámara"])
    note = writing.usage_note(writing.read(SPANISH + " " + SPANISH, prompt=prompt))
    assert "arte" in note and "cámara" in note


def test_a_fingerprint_is_stable_and_distinguishes_pieces():
    assert writing.fingerprint("hola") == writing.fingerprint("  hola  ")
    assert writing.fingerprint("hola") != writing.fingerprint("hola.")


# --------------------------------------------------------------------- a title --


def test_a_piece_is_named_by_how_it_opens():
    """Asking someone to title their practice writing is friction they do not
    need, and a list of pieces with no names is unusable."""
    assert writing.title_for("El arte se vuelve personal cada vez. Y más.") == \
        "El arte se vuelve personal cada…"
    assert writing.title_for("Hola.") == "Hola"
    assert writing.title_for("Corta") == "Corta"
    assert writing.title_for("") == "Untitled"
    assert writing.title_for("   \n  ") == "Untitled"


def test_a_title_does_not_grow_without_bound():
    title = writing.title_for("palabra " * 60)
    assert len(title.split()) <= 6 and title.endswith("…")


def test_a_title_survives_odd_input():
    for text in ("...", "¿?", "a", "Aa. Bb. Cc.", "🙂 hola"):
        assert isinstance(writing.title_for(text), str)
        assert writing.title_for(text)


# -------------------------------------------------------------------- improving --


def test_every_way_of_improving_says_what_it_will_and_will_not_do():
    """A learner asking for a revision should be able to say *which* thing they
    want changed -- "make it better" hands the decision to a model that does not
    know what they are working on."""
    for mode in writing.MODES:
        assert mode["id"] in writing.MODE_IDS
        assert len(mode["label"]) > 5, mode
        assert len(mode["blurb"]) > 20, mode
        assert len(mode["instruction"]) > 40, mode
    assert len(writing.MODE_IDS) == len(set(writing.MODE_IDS))
    assert writing.mode_options() == [dict(mode) for mode in writing.MODES]


def test_one_of_the_ways_is_do_as_little_as_possible():
    """Because the risk with a model rewriting your Spanish is that it writes its
    own instead, and "keep my words" is the reader saying so in advance."""
    minimal = writing.BY_MODE["minimal"]
    assert "exact words" in minimal["instruction"]
    assert "smaller change" in minimal["instruction"]


# --------------------------------------------------------------- the annotations --


def test_an_annotation_that_is_not_in_the_text_is_dropped():
    """A highlight in the wrong place teaches the wrong thing, so a fragment the
    tutor did not quote exactly cannot be shown at all."""
    notes = writing.keep_notes("El arte es bello.", [
        {"fragment": "El arte", "kind": "good", "why": "correct"},
        {"fragment": "no está aquí", "kind": "fix", "why": "invented"},
    ])
    assert [note["fragment"] for note in notes] == ["El arte"]


def test_annotations_are_capped_and_normalised():
    text = "El arte es bello."
    many = writing.keep_notes(text, [{"fragment": "El arte", "why": "x"}] * 40)
    assert len(many) == writing.MAX_NOTES

    coerced = writing.keep_notes(text, [{"fragment": "arte", "kind": "NONSENSE", "why": "x"}])
    assert coerced[0]["kind"] == "fix", "an unknown kind becomes the safe one"

    both = writing.keep_notes(text, [
        {"fragment": "bello", "kind": "good", "why": "nice word"},
        {"fragment": "bello", "kind": "style", "suggestion": "hermoso"},
    ])
    assert [note["kind"] for note in both] == ["good", "style"]


def test_an_annotation_with_nothing_to_say_is_dropped():
    notes = writing.keep_notes("El arte es bello.", [
        {"fragment": "Arte", "kind": "good"},                      # no why, no suggestion
        {"fragment": "bello", "kind": "good", "why": "yes"},
    ])
    assert [note["fragment"] for note in notes] == ["bello"]


def test_keeping_annotations_survives_rubbish():
    for junk in (None, "no", 42, {"fragment": "arte"}, ["no", None, {}]):
        assert writing.keep_notes("El arte.", junk) == []


# ----------------------------------------------------------------------- store --


@pytest.fixture()
def store(tmp_path):
    from app.store import Store

    handle = Store(tmp_path / "test.db")
    yield handle
    handle.close()


def test_a_piece_takes_versions_in_order(store):
    piece = store.start_writing(title="El arte", prompt="Escribe.")
    assert piece["id"] and piece["updated_at"]
    store.add_revision(piece["id"], text="Draft one.", words=2)
    store.add_revision(piece["id"], text="Draft two, better.", words=3)
    got = store.writing(piece["id"])
    assert [r["text"] for r in got["revisions"]] == ["Draft one.", "Draft two, better."]
    assert got["text"] == "Draft two, better.", "the latest is what the piece is now"
    assert got["revision_count"] == 2


def test_checking_the_same_text_twice_does_not_invent_a_version(store):
    """Checking a draft, changing a word and checking again is two versions, not
    three -- and the first version's notes would otherwise be attached to text
    they no longer describe."""
    piece = store.start_writing(title="T")
    store.add_revision(piece["id"], text="El arte.", reading={"words": 2}, checked=False)
    store.add_revision(piece["id"], text="El arte.", reading={"words": 2},
                       verdict={"score": 3.0}, checked=True)
    got = store.writing(piece["id"])
    assert got["revision_count"] == 1
    assert got["revisions"][0]["checked"] is True
    assert got["revisions"][0]["verdict"]["score"] == 3.0


def test_a_version_remembers_whether_it_was_read(store):
    """ "Not checked" and "checked and clean" look identical otherwise."""
    piece = store.start_writing(title="T")
    store.add_revision(piece["id"], text="uno.", words=1, checked=False)
    store.add_revision(piece["id"], text="uno dos.", words=2, checked=True)
    revisions = store.writing(piece["id"])["revisions"]
    assert [r["checked"] for r in revisions] == [False, True]


def test_versions_carry_their_own_feedback(store):
    piece = store.start_writing(title="T")
    store.add_revision(piece["id"], text="uno.", feedback={"summary": "first"},
                       reading={"words": 1, "spanish_words": 1}, checked=True)
    store.add_revision(piece["id"], text="dos.", feedback={"summary": "second"},
                       reading={"words": 1, "spanish_words": 1}, checked=True)
    assert [r["feedback"]["summary"] for r in store.writing(piece["id"])["revisions"]] == \
        ["first", "second"]


def test_a_piece_in_the_list_carries_its_latest_and_its_count(store):
    piece = store.start_writing(title="El arte en mi mundo", prompt="Escribe.")
    store.add_revision(piece["id"], text="uno.", words=1)
    store.add_revision(piece["id"], text="dos.", words=1, verdict={"score": 2.5},
                       reading={"words": 1, "spanish_share": 1.0}, checked=True)
    rows = store.writings()
    assert len(rows) == 1
    assert rows[0]["title"] == "El arte en mi mundo"
    assert rows[0]["text"] == "dos." and rows[0]["revision_count"] == 2
    assert rows[0]["verdict"]["score"] == 2.5
    assert rows[0]["excerpt"] == "dos."


def test_deleting_a_piece_takes_its_versions_with_it(store):
    piece = store.start_writing(title="T")
    store.add_revision(piece["id"], text="uno.")
    store.add_revision(piece["id"], text="dos.")
    assert store.delete_writing(piece["id"]) is True
    assert store.writing(piece["id"]) is None
    left = store.conn.execute("SELECT COUNT(*) FROM revisions WHERE writing_id = ?",
                              (piece["id"],)).fetchone()[0]
    assert left == 0, "a deleted piece must not leave its versions behind"


def test_writing_to_a_piece_that_is_not_there_is_refused(store):
    assert store.add_revision(9999, text="hola") is None
    assert store.writing(9999) is None
    assert store.delete_writing(9999) is False


def test_the_totals_count_every_version_because_every_version_was_written(store):
    """Counting only the final text would make rewriting look like it cost
    nothing, which is the opposite of what the history is for."""
    piece = store.start_writing(title="T")
    store.add_revision(piece["id"], text="uno dos.", words=2,
                       reading={"words": 2, "spanish_words": 2})
    store.add_revision(piece["id"], text="uno dos tres.", words=3,
                       reading={"words": 3, "spanish_words": 3})
    totals = store.writing_totals()
    assert totals["pieces"] == 1 and totals["revisions"] == 2
    assert totals["words"] == 5, "both versions were written"
    assert totals["spanish_share"] == 1.0


def test_a_piece_from_the_older_shape_is_migrated_rather_than_lost(tmp_path):
    """Writing used to be one row per piece holding a single version. Nothing is
    thrown away: the row becomes the piece plus its first revision."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE writings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, prompt TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL, reading TEXT NOT NULL DEFAULT '',
            verdict TEXT NOT NULL DEFAULT '', feedback TEXT NOT NULL DEFAULT '',
            words INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        INSERT INTO writings (prompt, text, reading, verdict, feedback, words, created_at)
        VALUES ('Escribe.', 'El arte se vuelve personal.', '{"words":5,"spanish_words":5}',
                '{"score":3.0}', '{"summary":"bueno"}', 5, '2026-09-20T10:00:00+00:00');
    """)
    conn.commit()
    conn.close()

    from app.store import Store

    store = Store(path)
    try:
        pieces = store.writings()
        assert len(pieces) == 1
        revision = pieces[0]["revisions"][0]
        assert revision["text"] == "El arte se vuelve personal."
        assert revision["verdict"]["score"] == 3.0
        assert revision["feedback"]["summary"] == "bueno"
        assert revision["checked"] is True, "it had been read"
        assert pieces[0]["updated_at"][:10] == "2026-09-20"
        # the columns that held the version are gone, so nothing reads them twice
        columns = {row[1] for row in store.conn.execute("PRAGMA table_info(writings)")}
        assert "text" not in columns
    finally:
        store.close()


def test_migrating_twice_changes_nothing_the_second_time(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE writings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, prompt TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL, reading TEXT NOT NULL DEFAULT '',
            verdict TEXT NOT NULL DEFAULT '', feedback TEXT NOT NULL DEFAULT '',
            words INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        INSERT INTO writings (prompt, text, created_at) VALUES ('p', 'uno.', '2026-09-20');
    """)
    conn.commit()
    conn.close()

    from app.store import Store

    for _ in range(3):
        store = Store(path)
        assert store.writing(1)["revision_count"] == 1
        store.close()


# --------------------------------------------------------------- the endpoints --


@pytest.fixture()
def app(tmp_path):
    """An App with a corpus and a database, and no model configured.

    ``llm_*`` is passed as empty rather than left to the environment: once any test
    in the run has loaded a ``.env``, ``os.environ`` keeps those values, and a test
    about how the app behaves with *no* model must not pass or fail on whether the
    machine it runs on happens to have a key.
    """
    from app.config import Settings, corpus_dir
    from app.server import App

    settings = Settings(corpus_dir=corpus_dir(),
                        data_dir=tmp_path / "data",
                        llm_base_url="", llm_api_key="", llm_model="",
                        typesafe_api_key="")
    application = App(settings)
    application.library.refresh()
    return application


def test_the_prompt_endpoint_always_answers(app, monkeypatch):
    from app import server

    monkeypatch.setattr(server, "ctx", lambda: app)
    out = server.writing_prompt()
    assert out["prompt"]["instruction"]
    assert out["totals"]["pieces"] == 0
    assert [mode["id"] for mode in out["modes"]] == list(writing.MODE_IDS)


def test_measuring_writes_nothing_down(app, monkeypatch):
    """It is called while the reader types, so it has to be free -- and it must
    not quietly create a piece from a half-finished sentence."""
    from app import server

    monkeypatch.setattr(server, "ctx", lambda: app)
    out = server.measure_writing(server.WritingBody(text=SPANISH, words=["arte"]))
    assert out["reading"]["words"] > 0
    assert "arte" in out["reading"]["used"]
    assert app.store.writings() == []


def test_checking_opens_a_piece_and_keeps_the_version(app, monkeypatch):
    from app import server

    monkeypatch.setattr(server, "ctx", lambda: app)
    out = server.check_writing(server.DraftBody(text=SPANISH, prompt="Escribe algo.",
                                                words=["arte"]))
    assert out["writing_id"]
    assert out["writing"]["title"] == writing.title_for(SPANISH)
    assert [r["label"] for r in out["writing"]["revisions"]] == ["Draft 1"]
    assert out["revision"]["text"] == SPANISH
    assert app.store.writing_totals()["pieces"] == 1


def test_the_second_check_lands_on_the_same_piece(app, monkeypatch):
    from app import server

    monkeypatch.setattr(server, "ctx", lambda: app)
    first = server.check_writing(server.DraftBody(text=SPANISH))
    second = server.check_writing(server.DraftBody(writing_id=first["writing_id"],
                                                   text=SPANISH + " Más."))
    assert second["writing_id"] == first["writing_id"]
    assert second["writing"]["revision_count"] == 2
    assert len(app.store.writings()) == 1, "one piece, not two"


def test_a_piece_can_be_kept_without_being_read(app, monkeypatch):
    """Kept text is not read text: the difference has to survive, or a draft
    nobody looked at reads as a draft that passed."""
    from app import server

    monkeypatch.setattr(server, "ctx", lambda: app)
    out = server.save_draft(server.DraftBody(text=SPANISH, prompt="Escribe."))
    revision = out["writing"]["revisions"][0]
    assert revision["checked"] is False
    assert revision["verdict"] == {} and revision["feedback"] == {}
    assert out["writing"]["checked"] is False


def test_a_piece_can_be_opened_by_id_and_listed(app, monkeypatch):
    from app import server

    monkeypatch.setattr(server, "ctx", lambda: app)
    started = server.save_draft(server.DraftBody(text="El arte se vuelve personal."))
    got = server.get_writing(started["writing_id"])["writing"]
    assert got["title"] and got["revisions"]
    listed = server.list_writings()
    assert [piece["id"] for piece in listed["writings"]] == [started["writing_id"]]
    assert listed["modes"]


def test_empty_writing_is_refused(app, monkeypatch):
    from app import server
    from fastapi import HTTPException

    monkeypatch.setattr(server, "ctx", lambda: app)
    for body in (server.WritingBody(text=""), server.WritingBody(text="   ")):
        with pytest.raises(HTTPException) as caught:
            server.check_writing(body)
        assert caught.value.status_code == 400
        with pytest.raises(HTTPException):
            server.save_draft(body)


def test_a_check_against_a_piece_that_is_gone_is_a_404(app, monkeypatch):
    from app import server
    from fastapi import HTTPException

    monkeypatch.setattr(server, "ctx", lambda: app)
    with pytest.raises(HTTPException) as caught:
        server.check_writing(server.DraftBody(writing_id=9999, text="El arte."))
    assert caught.value.status_code == 404
    with pytest.raises(HTTPException) as caught:
        server.get_writing(9999)
    assert caught.value.status_code == 404
    with pytest.raises(HTTPException) as caught:
        server.delete_writing(9999)
    assert caught.value.status_code == 404


def test_with_no_model_the_scores_say_so_and_the_measurements_stand(app, monkeypatch):
    """The honest half. Reviewing a paragraph needs a reader of meaning, and
    there is no local substitute -- so the app says that rather than returning a
    number that would mean nothing."""
    from app import server

    monkeypatch.setattr(server, "ctx", lambda: app)
    app.judge.api_key = ""          # no Jev
    app.tutor = None                # no chat model
    out = server.check_writing(server.DraftBody(text=SPANISH, words=["arte"]))

    assert out["reading"]["words"] > 0, "the measurements do not need a model"
    assert out["reading"]["spanish_share"] > 0.9
    assert out["feedback"] is None
    assert out["verdict"]["error"], "and the missing review is stated, not invented"
    assert out["verdict"]["method"] == "local"
    assert out["verdict"]["may_fail"] is False, "nothing here may be called wrong"
    assert "needs a model" in out["verdict"]["error"]
    # and it is still kept, because the text is the reader's
    assert app.store.writing(out["writing_id"])["revision_count"] == 1


def test_improving_needs_the_tutor_and_says_so(app, monkeypatch):
    from app import server
    from fastapi import HTTPException

    monkeypatch.setattr(server, "ctx", lambda: app)
    app.tutor = None
    with pytest.raises(HTTPException) as caught:
        server.improve_writing(server.ImproveBody(text=SPANISH, mode="grammar"))
    assert caught.value.status_code == 503
    assert "LLM_BASE_URL" in caught.value.detail, "and names what to set"


def test_a_bad_request_is_rejected_as_a_bad_request(app, monkeypatch):
    """Not as a missing model: answering "no tutor" to a bad mode sends the
    reader off to fix the wrong thing."""
    from app import server
    from fastapi import HTTPException

    monkeypatch.setattr(server, "ctx", lambda: app)
    app.tutor = None                # even with nothing configured
    with pytest.raises(HTTPException) as caught:
        server.improve_writing(server.ImproveBody(text=SPANISH, mode="make-it-nice"))
    assert caught.value.status_code == 400
    with pytest.raises(HTTPException) as caught:
        server.improve_writing(server.ImproveBody(text="   ", mode="grammar"))
    assert caught.value.status_code == 400


def test_improving_suggests_and_never_applies(app, monkeypatch):
    """The suggestion is returned, not written down: a suggestion is not a version
    of the piece until the reader makes it one."""
    from app import server

    class FakeTutor:
        def improve_writing(self, *, text, mode, prompt="", focus=None):
            return {"mode": mode, "label": writing.BY_MODE[mode]["label"],
                    "revision": text + " Mejorado.", "changed": "made it better"}

    monkeypatch.setattr(server, "ctx", lambda: app)
    app.tutor = FakeTutor()
    out = server.improve_writing(server.ImproveBody(text="El arte.", mode="natural"))
    assert out["suggestion"]["revision"] == "El arte. Mejorado."
    assert app.store.writings() == [], "nothing kept, nothing changed"


def test_an_improvement_that_failed_says_so_rather_than_looking_empty(app, monkeypatch):
    from app import server

    class BrokenTutor:
        def improve_writing(self, *, text, mode, prompt="", focus=None):
            return {"failed": True, "revision": "", "changed": "",
                    "summary": "The tutor could not be reached."}

    monkeypatch.setattr(server, "ctx", lambda: app)
    app.tutor = BrokenTutor()
    out = server.improve_writing(server.ImproveBody(text="El arte.", mode="grammar"))
    assert out["suggestion"] is None
    assert out["failed"] is True and "could not be reached" in out["summary"]
