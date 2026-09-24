"""Tests for kept sentences.

Two properties matter and they pull in opposite directions.

**Nothing may be lost.** A quote is cut out of a paragraph, and the split has to
account for every character -- a sentence boundary that swallows a word would
silently damage the one thing the reader chose to keep.

**Nothing may be kept that teaches nothing.** The collection is only useful if
everything in it is worth returning to, so an English sentence, or one whose
Spanish is a proper noun the segmenter misread, is refused. Refused with a
reason, because "nothing happened" is a bad answer to a click.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import quotes  # noqa: E402
from app.config import corpus_dir  # noqa: E402
from app.diglot import parse_article, parse_corpus  # noqa: E402

CORPUS = corpus_dir()

ARTICLE = """### Article Identification & Preview

- **Article Title:** A Test Lesson
- **Author:** Someone

# A Test Lesson

**By Someone**

El arte se vuelve **más personal** (*more personal*) cada vez. Los anales de la
historia del arte **pintan** un panorama diferente.

This sentence is entirely in English and teaches nothing at all.
"""


def parsed(text: str = ARTICLE):
    article = parse_article(text, fallback_slug="a-test-lesson")
    assert article is not None
    return article


def paragraph(article):
    """The index of the first paragraph with Spanish in it."""
    return next(i for i, block in enumerate(article.blocks)
                if block.kind == "p" and any(s.lang == "es" for s in block.spans))


def english_paragraph(article):
    """The index of the first paragraph with no Spanish at all."""
    return next(i for i, block in enumerate(article.blocks)
                if block.kind == "p" and not any(s.lang == "es" for s in block.spans))


# ------------------------------------------------------------------ splitting --


@pytest.mark.parametrize("folder", [CORPUS])
def test_sentence_splitting_accounts_for_every_character(folder):
    """The invariant the whole feature rests on: splitting a paragraph into
    sentences and putting them back together must reproduce it exactly. Compared
    with whitespace removed, because the split trims and the paragraph does not."""
    articles = parse_corpus(folder) if folder.is_dir() else []
    if not articles:
        pytest.skip(f"no corpus at {folder}")
    checked = 0
    for article in articles:
        for index, block in enumerate(article.blocks):
            if block.kind != "p":
                continue
            rejoined = "".join(s.text for s in quotes.sentences(block.plain))
            assert "".join(rejoined.split()) == "".join(block.plain.split()), (
                f"{article.slug} block {index} lost text")
            checked += 1
    assert checked > 100, "the corpus should have real paragraphs in it"


def test_an_abbreviation_does_not_end_a_sentence():
    """Cutting a quote in half is worse than letting two sentences run together,
    so the splitter errs towards not splitting."""
    text = "El Sr. García llegó a las 3.30 de la tarde. Trajo libros."
    found = quotes.sentences(text)
    assert len(found) == 2, [s.text for s in found]
    assert found[0].text.endswith("tarde.")


def test_a_paragraph_with_no_final_punctuation_still_yields_a_sentence():
    found = quotes.sentences("Sin punto final al final")
    assert [s.text for s in found] == ["Sin punto final al final"]


# -------------------------------------------------------------------- locating --


def test_a_whole_sentence_is_kept_as_it_reads():
    article = parsed()
    index = paragraph(article)
    sentence = next(s for s in quotes.sentences(article.blocks[index].plain)
                    if "más personal" in s.text)
    quote, why = quotes.locate(article, index, sentence.text)
    assert why == ""
    assert quote is not None
    assert quote.text == sentence.text
    assert quote.block_index == index


def test_a_selected_fragment_widens_to_the_whole_sentence():
    """The reader selects a phrase; the quote is the sentence it lives in. Half a
    sentence is not something anyone wants to be shown again later."""
    article = parsed()
    index = paragraph(article)
    sentence = next(s for s in quotes.sentences(article.blocks[index].plain)
                    if "más personal" in s.text)
    quote, _why = quotes.locate(article, index, "se vuelve más")
    assert quote is not None
    assert quote.text == sentence.text


def test_the_two_languages_are_separated():
    article = parsed()
    index = paragraph(article)
    quote, _why = quotes.locate(article, index, "El arte se vuelve")
    assert quote is not None
    assert quote.es.startswith("El arte se vuelve")
    assert "more personal" not in quote.es, "the gloss is not part of the Spanish"
    assert "more personal" in quote.glosses


def test_the_focus_word_is_the_one_the_lesson_bolded():
    article = parsed()
    index = paragraph(article)
    quote, _why = quotes.locate(article, index, "El arte se vuelve")
    assert quote.term == "más personal"


def test_spans_come_back_with_the_markup_the_reader_used():
    article = parsed()
    index = paragraph(article)
    quote, _why = quotes.locate(article, index, "El arte se vuelve")
    kinds = [(span["lang"], span.get("bold", False)) for span in quote.spans]
    assert ("es", True) in kinds, "the bold target survives"
    assert any(span.get("gloss") for span in quote.spans), "and so does the gloss"


def test_a_gloss_is_dropped_when_the_cut_left_its_word_behind():
    """The gloss belongs to the span's *last* word, so a cut that stops earlier
    must not carry it -- otherwise the quote shows a gloss for a word it does not
    contain. Tested on the clipper, because locating always widens to a whole
    sentence and a sentence boundary only rarely falls inside a span."""
    article = parsed()
    block = article.blocks[paragraph(article)]
    glossed = next(s for s in block.spans if s.gloss)
    whole = "".join(s.text for s in block.spans)
    end = whole.index(glossed.text) + len(glossed.text) - 1  # one character short

    clipped = quotes.clip(block.spans, 0, end)
    assert not any(span.gloss for span in clipped), "the truncated span loses its gloss"

    clipped_all = quotes.clip(block.spans, 0, len(whole))
    assert any(span.gloss for span in clipped_all), "the whole span keeps it"


def test_the_quote_has_no_surrounding_whitespace_or_double_spaces():
    article = parsed()
    index = paragraph(article)
    quote, _why = quotes.locate(article, index, "pintan un panorama")
    assert quote is not None
    assert quote.text == quote.text.strip()
    assert "  " not in quote.text
    assert quote.spans[0]["text"] == quote.spans[0]["text"].lstrip()
    assert quote.spans[-1]["text"] == quote.spans[-1]["text"].rstrip()


# ------------------------------------------------------------------- refusals --


def test_an_english_sentence_is_refused_with_a_reason():
    article = parsed()
    index = english_paragraph(article)
    quote, why = quotes.locate(article, index, "This sentence is entirely in English")
    assert quote is None
    assert why == "there is no Spanish in that sentence"


def test_a_sentence_whose_only_spanish_is_a_name_is_refused():
    """A proper noun is not vocabulary. Keeping it would put a sentence with
    nothing to learn in a collection that is only useful if everything in it is
    worth returning to.

    Built by hand rather than found in the corpus: the segmenter only rarely
    marks a name as Spanish, and when it does it usually runs on into the rest of
    the sentence, so the case is easier to state than to stumble across. It does
    happen -- `Ai-Da` in one article was the first one found.
    """
    from app.diglot import Article, Block, Span

    article = Article(
        slug="t", title="T", author=None, url=None, preview=None, date=None, day=None,
        blocks=[Block(kind="p", spans=[
            Span(lang="en", text="The director was "),
            Span(lang="es", text="Almodóvar"),
            Span(lang="en", text="."),
        ])],
        vocab=[], grammar=[], source="t",
    )
    quote, why = quotes.locate(article, 0, "The director was Almodóvar.")
    assert quote is None
    assert why == "that sentence has no Spanish words to learn in it"

    # The same sentence with a word to learn in it goes through.
    article.blocks[0].spans.insert(2, Span(lang="es", text=" famoso"))
    quote, why = quotes.locate(article, 0, "The director was Almodóvar famoso.")
    assert quote is not None, why
    assert "famoso" in quote.es


def test_nothing_selected_is_refused():
    article = parsed()
    assert quotes.locate(article, 0, "   ") == (None, "nothing was selected")


def test_text_from_another_paragraph_is_refused():
    article = parsed()
    index = paragraph(article)
    quote, why = quotes.locate(article, index + 1, "El arte se vuelve")
    assert quote is None
    assert "page has changed" in why


def test_a_block_index_off_the_end_is_refused():
    article = parsed()
    quote, why = quotes.locate(article, 9999, "El arte")
    assert quote is None
    assert why == "that passage is not in this article"


def test_locating_never_raises_on_any_block_of_the_corpus():
    """Cheap insurance: the endpoint takes a block index from the browser, and an
    index the article cannot support must be an answer rather than a traceback."""
    articles = parse_corpus(CORPUS) if CORPUS.is_dir() else []
    if not articles:
        pytest.skip("no corpus")
    for article in articles:
        for index in (-1, 0, len(article.blocks), 9999):
            quote, _why = quotes.locate(article, index, "zzzznotpresentanywhere")
            assert quote is None


# ---------------------------------------------------------------------- store --


@pytest.fixture()
def store(tmp_path):
    from app.store import Store

    handle = Store(tmp_path / "test.db")
    yield handle
    handle.close()


def test_saving_the_same_sentence_twice_keeps_one(store):
    first, new_first = store.save_quote(text="El arte se vuelve personal.", es="El arte se vuelve personal.")
    second, new_second = store.save_quote(text="El arte se vuelve personal.", es="El arte se vuelve personal.")
    assert first == second
    assert new_first is True and new_second is False, "the second save is reported, not hidden"
    assert store.quote_count() == 1


def test_search_folds_accents_and_covers_every_field(store):
    store.save_quote(text="La cuestión es otra.", es="La cuestión es otra.", term="cuestión",
                     glosses="matter", note="a connector")
    assert len(store.quotes(search="cuestion")) == 1, "typed without the accent"
    assert len(store.quotes(search="cuestión")) == 1
    assert len(store.quotes(search="matter")) == 1, "found by its gloss"
    assert len(store.quotes(search="connector")) == 1, "found by the reader's note"
    assert len(store.quotes(search="otra")) == 1
    assert len(store.quotes(search="nothing here")) == 0


def test_editing_a_note_does_not_make_the_glosses_unsearchable(store):
    """The first version kept a folded ``search`` column and rebuilt it on every
    write -- so editing a note dropped the sentence's glosses out of the index.
    Search reads the row's fields now, and this is the regression test for it."""
    quote_id, _new = store.save_quote(text="Los anales cambian.", es="Los anales cambian.",
                                      glosses="annals")
    assert len(store.quotes(search="annals")) == 1
    assert store.set_quote_note(quote_id, "the annals one") is True
    assert len(store.quotes(search="annals")) == 1, "still findable by its gloss"
    assert len(store.quotes(search="the annals one")) == 1, "and by the new note"
    assert len(store.quotes(search="")) == 1


def test_filtering_by_article_and_by_term(store):
    store.save_quote(text="Uno.", es="Uno.", article_slug="a", term="volverse")
    store.save_quote(text="Dos.", es="Dos.", article_slug="b", term="pintar")
    assert [row["text"] for row in store.quotes(slug="a")] == ["Uno."]
    assert [row["text"] for row in store.quotes(term="pintar")] == ["Dos."]
    assert len(store.quotes()) == 2


def test_quotes_using_matches_whole_words(store):
    """Looking up *es* must not return every sentence containing *estas*."""
    store.save_quote(text="Eso es lo que importa.", es="Eso es lo que importa.")
    store.save_quote(text="Las estas no existen.", es="Las estas no existen.")
    assert len(store.quotes_using("importa")) == 1
    assert len(store.quotes_using("es")) == 0, "too short to be worth looking up"
    assert len(store.quotes_using("estas")) == 1
    assert len(store.quotes_using("existen")) == 1


def test_quotes_using_matches_a_phrase(store):
    store.save_quote(text="Podemos esperar que llegue.", es="Podemos esperar que llegue.")
    assert len(store.quotes_using("esperar que")) == 1


def test_notes_can_be_added_and_removed(store):
    quote_id, _new = store.save_quote(text="Uno.", es="Uno.")
    assert store.set_quote_note(quote_id, "worth remembering") is True
    assert store.quote(quote_id)["note"] == "worth remembering"
    store.set_quote_note(quote_id, "")
    assert store.quote(quote_id)["note"] == ""


def test_editing_or_deleting_something_that_is_gone_is_reported(store):
    assert store.set_quote_note(4242, "x") is False
    assert store.delete_quote(4242) is False


def test_the_count_tracks_deletions(store):
    first, _ = store.save_quote(text="Uno.", es="Uno.")
    store.save_quote(text="Dos.", es="Dos.")
    assert store.stats()["quotes"] == 2
    assert store.delete_quote(first) is True
    assert store.quote_count() == 1
    assert store.stats()["quotes"] == 1


# --------------------------------------------------------------------- server --


@pytest.fixture()
def app(tmp_path):
    from app.config import Settings, corpus_dir
    from app.server import App

    settings = Settings(corpus_dir=CORPUS, data_dir=tmp_path / "data")
    application = App(settings)
    application.library.refresh()
    return application


def first_sentence(application, slug="how-ai-will-make-art-worse"):
    """A sentence from a real article that the app will actually keep.

    Chosen by asking :func:`quotes.locate` rather than by guessing, because
    "which sentence has Spanish in it" is precisely the question this module
    exists to answer, and a guess that picked an English sentence made three
    tests fail in confusing ways.
    """
    article = application.library.get(slug).article
    for index, block in enumerate(article.blocks):
        if block.kind != "p":
            continue
        for sentence in quotes.sentences(block.plain):
            if len(sentence.text) < 40:
                continue
            quote, _why = quotes.locate(article, index, sentence.text)
            if quote is not None:
                return slug, index, sentence.text
    raise AssertionError(f"no quotable sentence in {slug}")


def test_the_endpoint_keeps_a_sentence_and_the_popover_can_find_it(app, monkeypatch):
    from app import server

    if app.library.get("how-ai-will-make-art-worse") is None:
        pytest.skip("corpus not available")
    monkeypatch.setattr(server, "ctx", lambda: app)
    slug, index, text = first_sentence(app)

    out = server.save_quote(server.QuoteBody(slug=slug, block_index=index, text=text))
    assert out["created"] is True and out["total"] == 1

    listed = server.list_quotes()
    assert listed["count"] == 1
    assert listed["quotes"][0]["source_title"]
    assert listed["quotes"][0]["spans"], "renderable as it read"

    # and the connection back into reading
    term = out["quote"]["term"]
    if term:
        assert app.store.quotes_using(term)


def test_the_endpoint_reports_why_it_refused(app, monkeypatch):
    from app import server
    from fastapi import HTTPException

    if app.library.get("how-ai-will-make-art-worse") is None:
        pytest.skip("corpus not available")
    monkeypatch.setattr(server, "ctx", lambda: app)
    with pytest.raises(HTTPException) as caught:
        server.save_quote(server.QuoteBody(
            slug="how-ai-will-make-art-worse", block_index=0,
            text="Many live in quiet fear that AI will someday be, if not the death of art"))
    assert caught.value.status_code == 400
    assert "no Spanish" in caught.value.detail


def test_an_unknown_article_is_a_404(app, monkeypatch):
    from app import server
    from fastapi import HTTPException

    monkeypatch.setattr(server, "ctx", lambda: app)
    with pytest.raises(HTTPException) as caught:
        server.save_quote(server.QuoteBody(slug="nope", block_index=0, text="algo"))
    assert caught.value.status_code == 404


def test_a_quote_survives_its_article_being_deleted(app, monkeypatch):
    """The sentence is the reader's, not the article's. If the source goes, the
    quote stays and only loses its markup."""
    from app import server

    if app.library.get("how-ai-will-make-art-worse") is None:
        pytest.skip("corpus not available")
    monkeypatch.setattr(server, "ctx", lambda: app)
    slug, index, text = first_sentence(app)
    server.save_quote(server.QuoteBody(slug=slug, block_index=index, text=text))

    del app.library._entries[slug]
    listed = server.list_quotes()
    assert listed["count"] == 1
    row = listed["quotes"][0]
    assert row["text"]
    assert row["spans"] is None and row["present"] is False
    assert row["source_title"] is None
