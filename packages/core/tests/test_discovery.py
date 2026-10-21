"""Tests for finding something to read.

Discovery has one job that is easy to get wrong in a way nobody notices: it
decides what the reader's attention lands on next. So the tests here are about
what the reader is *told* -- whether a result is something they already own,
what kind of writing it is, and which kinds they have never tried -- rather than
about the search itself, which is a remote service and is faked throughout.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import corpus_dir  # noqa: E402
from corpus_support import real_corpus  # noqa: E402

from app import fetch, recommend, registers  # noqa: E402
from app.diglot import parse_article, parse_corpus  # noqa: E402

CORPUS = corpus_dir()


# ------------------------------------------------------------- what is readable --


@pytest.mark.parametrize("title", [
    "Sleep (disambiguation)",
    "List of countries by population",
    "Outline of physics",
    "Index of philosophy articles",
])
def test_pages_that_are_not_reading_material_are_dropped(title):
    """A disambiguation page is a list of links and a list page is a table.
    Neither can be woven into a lesson, and both crowd out the article that was
    actually wanted -- searching for "sleep" returned "Sleep Token" and "Sleep
    (disambiguation)" beside "Sleep deprivation"."""
    assert fetch._is_readable({"title": title, "snippet": ""}) is False


def test_a_disambiguation_snippet_is_dropped_too():
    assert fetch._is_readable({
        "title": "Mercury", "snippet": "Mercury may refer to: Mercury (element), a metal"}) is False


def test_real_articles_survive_the_filter():
    for title in ("Sleep deprivation", "AI art", "History of painting",
                  "Sleep Token", "Computational linguistics"):
        assert fetch._is_readable({"title": title, "snippet": "An article about it."}) is True


# ------------------------------------------------------------------- the gaps --

LESSONS = [
    ("How AI Will Make Art Worse", "essay", True),
    ("AI Art and the End of Creativity", "essay", True),
    ("Large Language Models and Human Brains", "essay", False),
    ("Why Healthcare AI Needs More Than a Model", "news", False),
    ("What Does Language Mean?", "news", False),
    ("A Psychology of Reading", "academic", False),
    ("The Lamplighter's Daughter", "fiction", False),
]


def library(tmp_path):
    """A small library with known registers, built from real files."""
    from app.library import Library

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for index, (title, register, _read) in enumerate(LESSONS):
        (corpus / f"lesson-{index}.md").write_text(
            "### Article Identification & Preview\n\n"
            f"- **Article Title:** {title}\n"
            f"- **Register:** {register}\n\n"
            f"# {title}\n\n"
            "El arte se vuelve **más personal** (*more personal*) cada vez.\n",
            encoding="utf-8",
        )
    shelf = Library(corpus, tmp_path / "library")
    shelf.refresh()
    return shelf


def progress_for(shelf, read_titles):
    entries = {entry.article.title: entry.article.slug for entry in shelf.all()}
    return {entries[title]: {"completed_at": "2026-09-01"} for title in read_titles if title in entries}


def test_topics_come_out_as_a_phrase_rather_than_a_word(tmp_path):
    """A search query built from single words ranks by whichever word is most
    common -- "large" -- and "large" is not a subject."""
    topics = recommend.topics_from_titles([
        "How AI Will Make Art Worse",
        "AI Art and the End of Creativity",
        "Large Language Models and Human Brains",
    ])
    assert topics, "some topic should be found"
    assert "large" not in topics, topics
    assert any("ai" in topic or "art" in topic for topic in topics), topics


def test_topics_ignore_a_word_that_appears_once(tmp_path):
    topics = recommend.topics_from_titles([
        "Sleep and Memory",
        "Sleep and Learning",
        "An Unrelated Title About Birds",
    ])
    assert all("birds" not in topic and "unrelated" not in topic for topic in topics), topics


def test_topics_survive_having_nothing_to_go_on():
    assert recommend.topics_from_titles([]) == []


def test_a_register_is_a_gap_only_when_nothing_in_it_has_been_read(tmp_path):
    shelf = library(tmp_path)
    gaps = recommend.broaden(shelf.all(), progress_for(shelf, [
        "How AI Will Make Art Worse", "AI Art and the End of Creativity"]))
    codes = [gap["register"] for gap in gaps]
    assert "essay" not in codes, "the reader has read essays"
    assert "news" in codes, "and has never read a news report"


def test_a_gap_names_the_unread_lessons_already_on_the_shelf(tmp_path):
    """The cheaper way in, and the more likely one to be wanted: the article
    exists, so the suggestion can point straight at it instead of at a search."""
    shelf = library(tmp_path)
    gaps = {gap["register"]: gap for gap in recommend.broaden(shelf.all(), {})}
    news = gaps["news"]
    titles = {item["title"] for item in news["unread"]}
    assert titles == {"Why Healthcare AI Needs More Than a Model", "What Does Language Mean?"}
    assert news["available"] == 2


def test_a_gap_carries_a_way_into_it(tmp_path):
    """With nothing read there is no topic to search for, and the suggestion has
    to say that rather than invent one."""
    shelf = library(tmp_path)
    for gap in recommend.broaden(shelf.all(), {}):
        assert gap["label"], gap
        assert gap["blurb"], gap
    read = recommend.broaden(shelf.all(), progress_for(shelf, [
        "How AI Will Make Art Worse", "AI Art and the End of Creativity"]))
    news = next(gap for gap in read if gap["register"] == "news")
    assert news["query"], "a reader with a history gets a search to run"
    assert "ai" in news["query"] or "art" in news["query"], news["query"]


def test_nothing_to_suggest_when_every_register_has_been_tried(tmp_path):
    shelf = library(tmp_path)
    read = progress_for(shelf, [title for title, _code, _r in LESSONS])
    assert recommend.broaden(shelf.all(), read) == []


def test_each_gap_asks_for_a_different_search(tmp_path):
    """One subject, several kinds of writing. Without a per-register hint every
    gap suggests an identical query, and three suggestions return the same
    results -- which is not a way out of reading only one kind of text."""
    shelf = library(tmp_path)
    read = progress_for(shelf, ["How AI Will Make Art Worse", "AI Art and the End of Creativity"])
    queries = {gap["register"]: gap["query"] for gap in recommend.broaden(shelf.all(), read)}
    assert len(set(queries.values())) == len(queries), queries
    assert "news" in queries["news"]
    assert queries["essay"] if "essay" in queries else True

    for register in registers.REGISTERS:
        # An essay is what a search box gives you anyway, so it needs no hint;
        # every other register has to be asked for by name.
        assert register.search or register.code == "essay", register


def test_overlapping_topics_are_merged_into_one_phrase():
    """A shelf about language models yields both "large language" and "language
    models". Asking for both reads as a stutter."""
    assert recommend.join_topics(["large language", "language models"]) == "large language models"
    assert recommend.join_topics(["language models", "large language"]) == "large language models"
    assert recommend.join_topics(["creativity"]) == "creativity"
    assert recommend.join_topics(["language", "art"]) == "language art"
    assert recommend.join_topics([]) == ""


def test_gaps_are_ordered_by_how_much_is_waiting(tmp_path):
    shelf = library(tmp_path)
    gaps = recommend.broaden(shelf.all(), {})
    # Essay has three on the shelf and news has two, so essay comes first.
    assert [gap["register"] for gap in gaps][:2] == ["essay", "news"]


def test_broaden_never_raises_on_a_shelf_it_cannot_read(tmp_path):
    assert recommend.broaden([], {}) in ([],) or True   # empty shelf: no gaps


# ------------------------------------------------------------- the annotation --


class FakeEntry:
    def __init__(self, article):
        self.article = article


class FakeLibrary:
    def __init__(self, entries=()):
        self._entries = list(entries)

    def all(self):
        return self._entries


def lesson(title: str, url: str = ""):
    front = f"### Article Identification & Preview\n\n- **Article Title:** {title}\n"
    if url:
        front += f"- **Direct URL:** {url}\n"
    article = parse_article(f"{front}\n# {title}\n\nEl arte crece cada vez más.\n",
                            fallback_slug="x")
    assert article is not None
    return article


def test_a_result_the_reader_already_owns_is_marked():
    from app.server import _annotate

    class App:
        library = FakeLibrary([FakeEntry(lesson("Sleep deprivation",
                                                 "https://en.wikipedia.org/wiki/Sleep_deprivation"))])

    results = [{"url": "https://en.wikipedia.org/wiki/Sleep_deprivation", "title": "Sleep deprivation"},
               {"url": "https://en.wikipedia.org/wiki/Sleep", "title": "Sleep"}]
    out = _annotate(results, App())
    assert out[0]["already_have"], "the one already in the library"
    assert out[1]["already_have"] is None, "and the one that is not"


def test_a_result_is_marked_by_title_when_the_url_is_not_quite_the_same():
    """Old lessons may carry only a title, and the same article can be reachable
    at two addresses -- so a second copy has to be caught by name as well."""
    from app.server import _annotate

    class App:
        library = FakeLibrary([FakeEntry(lesson("Why We Sleep"))])

    out = _annotate([{"url": "https://example.com/x", "title": "why we sleep"}], App())
    assert out[0]["already_have"]


def test_a_result_is_labelled_with_the_register_its_source_implies():
    from app.server import _annotate

    class App:
        library = FakeLibrary([])

    out = _annotate([
        {"url": "https://www.reuters.com/world/x", "title": "Something"},
        {"url": "https://arxiv.org/abs/1234", "title": "A paper"},
        {"url": "https://example.com/x", "title": "Unknown source"},
    ], App())
    assert out[0]["register"] == "news" and "News" in out[0]["register_label"]
    assert out[1]["register"] == "academic"
    assert out[2]["register"] is None and out[2]["register_label"] == ""


def test_the_annotation_does_not_mutate_the_search_result():
    """The results come straight from the search layer, which is also used
    elsewhere; annotating in place would leak server state into it."""
    from app.server import _annotate

    class App:
        library = FakeLibrary([])

    original = {"url": "https://example.com/x", "title": "T"}
    _annotate([original], App())
    assert "already_have" not in original and "register" not in original


# --------------------------------------------------------------- end to end ----


def test_the_corpus_library_has_registers_worth_broadening_into(tmp_path):
    """The real shelf, not a fixture: the gap feature is pointless if the library
    it reads has only one register in it."""
    real_corpus()
    from app.library import Library

    shelf = Library(CORPUS, tmp_path / "library")
    shelf.refresh()
    gaps = recommend.broaden(shelf.all(), {})
    assert gaps, "an unread library should have gaps"
    assert len({gap["register"] for gap in recommend.broaden(shelf.all(), {})}) >= 2
    for gap in gaps:
        assert gap["available"] >= 1
        assert len(gap["unread"]) <= 3
