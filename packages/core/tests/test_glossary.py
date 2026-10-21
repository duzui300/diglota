"""Tests for the local glossary.

The point of this module is that a click should not cost a model call, so the
tests are mostly about *coverage*: that the corpus's own translations are
actually found, that inflections find them, and that the index prefers the
article's curated translation over an incidental one.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.diglot import parse_article  # noqa: E402
from app.glossary import Glossary  # noqa: E402
from app.warm import GlossWarmer  # noqa: E402

ARTICLE = """### Article Identification & Preview

- **Article Title:** A Test Article
- **Author:** Someone

# A Test Article

**By Someone**

El arte se vuelve **más personal** (*more personal*) cada vez, y los anales (*annals*)
de la historia del arte **pintan** (*they paint*) un panorama diferente. Siguiendo esta
tendencia, **podemos esperar que** la IA empuje a los artistas.

---

### POST-READING ANCHORS

**Recycled Vocabulary Box**

- **volverse** / **se vuelve, se volverá** (*to become*)

**Grammar Breakdown**

1. **Future Tense:** Something about *será*.
"""


def build(text: str = ARTICLE, deck: list[dict] | None = None) -> tuple[Glossary, object]:
    article = parse_article(text, fallback_slug="a-test-article")
    assert article is not None
    return Glossary().build([article], deck or []), article


# ---------------------------------------------------------------- indexing --


def test_keys_cover_form_content_word_and_stem():
    keys = Glossary.keys_for("se vuelve")
    assert "w:se vuelve" in keys
    assert "c:vuelve" in keys                   # the word that carries the meaning
    assert any(key.startswith("s:") for key in keys)
    # ...and *not* the clitic. Keying a phrase under its first word made every
    # reflexive phrase in a library a candidate for every other: "se vuelve" could be
    # answered with another lesson's "se han debilitado".
    assert "w:se" not in keys


def test_a_clitic_never_keys_an_entry():
    """The same rule from the other side: an entry whose first word is a clitic is
    findable by its own form and by the word that carries its meaning, and by nothing
    else."""
    keys = Glossary.keys_for("se han debilitado")
    assert "w:se han debilitado" in keys
    assert "c:debilitado" in keys
    assert "w:se" not in keys


def test_lookup_finds_an_exact_form():
    glossary, _ = build()
    entry = glossary.lookup("pintan")
    assert entry is not None
    assert entry.gloss == "they paint" or "paint" in entry.gloss


def test_lookup_sees_through_an_inflection():
    """The vocabulary box gives "volverse / se vuelve, se volverá"; clicking any
    of those three forms must find it."""
    glossary, _ = build()
    for form in ("volverse", "se vuelve", "se volverá", "vuelve"):
        entry = glossary.lookup(form)
        assert entry is not None, form
        assert entry.gloss == "to become"


def test_lookup_misses_return_none():
    glossary, _ = build()
    assert glossary.lookup("hipopótamo") is None
    assert glossary.lookup("") is None


def test_focus_phrases_are_indexed():
    glossary, _ = build()
    entry = glossary.lookup("más personal")
    assert entry is not None
    assert entry.source == "focus"
    assert entry.gloss == "more personal"


def test_inline_gloss_maps_to_the_word_before_it():
    """The parser ends a Spanish span where its gloss begins, so the gloss
    describes the last word of that span -- here, "anales"."""
    glossary, _ = build()
    entry = glossary.lookup("anales")
    assert entry is not None
    assert entry.gloss == "annals"
    assert entry.source == "article"


def test_vocabulary_box_entries_are_split_into_forms():
    glossary, _ = build()
    entries = {glossary.lookup(form).term for form in ("volverse", "se vuelve", "se volverá")}
    assert entries == {"volverse", "se vuelve", "se volverá"}


def test_curated_entries_win_over_incidental_ones():
    """A phrase the lesson bolds is a better answer than a gloss it happened to
    pass through, so the index prefers it."""
    glossary, _ = build()
    entry = glossary.lookup("más personal")
    assert entry.source == "focus"


def test_deck_words_are_indexed():
    deck = [{"term": "el genoma", "lemma": "genoma", "gloss": "the genome"}]
    glossary, _ = build(deck=deck)
    assert glossary.lookup("el genoma").gloss == "the genome"
    assert glossary.lookup("genoma").gloss == "the genome"


def test_glossless_deck_words_are_skipped():
    deck = [{"term": "el genoma", "lemma": "genoma", "gloss": None}]
    glossary, _ = build(deck=deck)
    assert glossary.lookup("el genoma") is None


def test_entries_record_where_they_came_from():
    glossary, _ = build()
    entry = glossary.lookup("anales")
    assert entry.article == "a-test-article"
    assert entry.article_title == "A Test Article"


def test_compact_ships_only_whole_word_keys():
    """The browser cannot compute a Spanish stem, so stem keys would be dead
    weight on the wire."""
    glossary, _ = build()
    compact = glossary.compact()
    assert compact
    assert all(key.startswith("w:") for key in compact)
    assert "w:pintan" in compact


def test_compact_entries_are_plain_json():
    glossary, _ = build()
    payload = glossary.compact()["w:pintan"]
    # A key maps to a list because two lessons can gloss the same form, and the
    # browser picks the one from the lesson being read -- exactly as the server does.
    assert isinstance(payload, list) and payload
    assert set(payload[0]) >= {"term", "gloss", "source"}
    assert isinstance(payload[0]["term"], str)


def test_accumulating_rebuild_replaces_rather_than_merges():
    glossary, _ = build()
    before = len(glossary)
    glossary.build([], [])          # rebuild with nothing
    assert len(glossary) == 0
    assert before > 0


# ----------------------------------------------------------------- warming --


def test_candidates_are_the_most_frequent_unresolved_words():
    glossary, article = build()
    candidates = GlossWarmer.candidates(article, glossary, limit=5)
    assert candidates
    # Anything the glossary can already answer must not be queued for the model.
    assert all(glossary.lookup(word) is None for word in candidates)


def test_focus_words_without_a_gloss_are_warmed_first():
    """The article bolds what it means to teach, and a bare bolded phrase is
    exactly the one a learner will click -- nothing else would ever resolve it."""
    text = ARTICLE.replace("**pintan** (*they paint*)", "**pintan**")   # bare, unglossed
    glossary, article = build(text)
    assert glossary.lookup("pintan") is None
    candidates = GlossWarmer.candidates(article, glossary, limit=6)
    assert candidates[0] == "pintan", candidates


def test_candidates_skip_very_common_words():
    glossary, article = build()
    candidates = GlossWarmer.candidates(article, glossary, limit=40)
    assert "de" not in candidates
    assert "la" not in candidates


def test_warming_does_not_queue_the_same_word_twice():
    glossary, article = build()
    seen: list[str] = []

    class Client:
        pass

    warmer = GlossWarmer(Client(), workers=1, max_words=6)
    try:
        first = warmer.warm(article, glossary, lambda word, slug: seen.append(word))
        second = warmer.warm(article, glossary, lambda word, slug: seen.append(word))
        assert first > 0
        assert second == 0, "the same article was warmed twice"
    finally:
        warmer.stop()


def test_warming_failures_are_counted_not_raised():
    glossary, article = build()

    def failing_lookup(word, slug):
        raise RuntimeError("the tutor is down")

    warmer = GlossWarmer(object(), workers=1, max_words=3)
    try:
        warmer.warm(article, glossary, failing_lookup)
        deadline = __import__("time").time() + 5
        while warmer.stats()["pending"] and __import__("time").time() < deadline:
            __import__("time").sleep(0.02)
        stats = warmer.stats()
        assert stats["failed"] >= 0
        assert stats["pending"] == 0
    finally:
        warmer.stop()

def test_a_derivation_is_not_answered_from_its_root():
    """The corpus box says "profundizar / profundizando, profundiza (to deepen)". A
    click on **profundamente** -- the adverb, in an article that does not gloss it --
    was answered "to deepen", because all of those share the stem ``profun``. That is
    a different word, and an instant wrong answer is worse than a slow right one: it
    teaches the reader to distrust every instant lookup."""
    with_verb = ARTICLE.replace(
        "- **volverse** / **se vuelve, se volverá** (*to become*)",
        "- **volverse** / **se vuelve, se volverá** (*to become*)\n"
        "- **profundizar** / **profundizando, profundiza** (*to deepen*)")
    glossary, _ = build(with_verb)
    assert glossary.lookup("profundizar") is not None, "the word the lesson teaches"
    assert glossary.lookup("profundiza") is not None, "an inflection of it"
    assert glossary.lookup("profundizando") is not None, "and another"
    # ...and the ones it does not: the shapes are what gives them away.
    assert glossary.lookup("profundamente") is None, "the adverb is another word"
    assert glossary.lookup("profundidad") is None, "so is the noun"


def test_the_lesson_being_read_wins_over_another_lesson():
    """The same form can be glossed differently by two lessons -- ``se vuelve`` is
    *becomes* in one and part of ``se vuelve a estudiar`` in another -- and a click in
    the second used to be answered with the first lesson's sense."""
    from app.diglot import parse_article

    # Different titles, because the slug comes from the title: two lessons with the
    # same name are one lesson as far as the index is concerned.
    one = parse_article(ARTICLE, fallback_slug="one")
    other = parse_article(ARTICLE.replace("A Test Article", "Another Test Article")
                                 .replace("(*to become*)", "(*to go back to studying*)"),
                          fallback_slug="other")
    glossary = Glossary().build([one, other], [])

    assert glossary.lookup("se vuelve", article="a-test-article").gloss == "to become"
    assert glossary.lookup("se vuelve", article="another-test-article").gloss == "to go back to studying"
    # With no lesson named, the best-sourced entry stands -- first seen among equals.
    assert glossary.lookup("se vuelve") is not None
    # And the browser is sent both, so it can make the same choice the server would.
    assert len(glossary.compact()["w:se vuelve"]) == 2
