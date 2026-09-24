"""Tests for the diglot parser and segmenter.

The segmenter is the piece with the least obvious correctness story -- there is
no gold label for "which words here are Spanish" -- so it is tested two ways:
on hand-written cases where the answer is not in doubt, and against the real
corpus with a plausibility metric (a Spanish span that is mostly English
function words is a decode error). The corpus test is skipped when the corpus
is not present, so the suite still runs on a machine without it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import corpus_dir  # noqa: E402

from app import diglot  # noqa: E402

CORPUS = corpus_dir()


# --------------------------------------------------------------- segmenter --


def langs(text: str) -> list[tuple[str, str]]:
    return [(span.lang, span.text.strip()) for span in diglot.parse_spans(text)]


def test_pure_english_is_one_span():
    spans = langs("The camera did not kill painting, it forced it to evolve.")
    assert [lang for lang, _ in spans] == ["en"]


def test_pure_spanish_is_one_span():
    spans = langs("La cámara no mató la pintura, sino que la obligó a evolucionar.")
    assert [lang for lang, _ in spans] == ["es"]


def test_switch_lands_on_the_sentence_boundary():
    # The English clause must not be dragged into the Spanish run. This is the
    # case that the punctuation-discounted switch cost exists for.
    spans = langs(
        "Between Hollywood screenwriters and robot artists, the future of art can "
        "seem rather grim. Afortunadamente, los anales de la historia del arte "
        "pintan un panorama diferente."
    )
    assert [lang for lang, _ in spans] == ["en", "es"]
    assert spans[1][1].startswith("Afortunadamente")


def test_switch_back_lands_after_the_spanish():
    spans = langs(
        "En realidad, la cámara no mató la pintura, sino que la obligó a evolucionar. "
        "Movements like Impressionism arose from painters asking the same question."
    )
    assert [lang for lang, _ in spans] == ["es", "en"]
    assert spans[0][1].endswith("evolucionar.")
    assert spans[1][1].startswith("Movements")


def test_short_spanish_island_inside_english_survives():
    # A single bolded phrase is outvoted word by word; it has to win as a path.
    # It comes back as its own span because bold is a span boundary, so what is
    # asserted is that the phrase is Spanish and the English after it is not.
    spans = langs("Siguiendo esta tendencia, **podemos esperar que** the future will differ.")
    assert spans[-1][0] == "en", spans
    spanish = " ".join(text for lang, text in spans if lang == "es")
    assert "podemos esperar que" in spanish
    assert "the future" not in spanish


def test_bold_spanish_is_marked_as_a_target():
    spans = diglot.parse_spans("la historia del arte **pintan** un panorama")
    targets = [s for s in spans if s.target]
    assert len(targets) == 1
    assert targets[0].text.strip() == "pintan"
    assert targets[0].bold


def test_bold_english_label_is_not_spanish():
    # Some articles bold English run-in labels; those are not vocabulary.
    spans = diglot.parse_spans(
        "- **In-context learning and pattern understanding:** Durante el entrenamiento "
        "de los LLM, algunas cabezas de atención **adquieren** la capacidad."
    )
    labels = [s for s in spans if s.bold and not s.target]
    assert labels, "the English label should be bold but not a Spanish target"
    assert "In-context" in labels[0].text
    assert any(s.target and s.text.strip() == "adquirieren" or s.text.strip() == "adquieren" for s in spans)


def test_a_bold_word_touching_punctuation_is_not_lost():
    """A bold span followed straight by a comma used to be deleted.

    The markers are lifted into placeholder tokens that everything downstream
    recognises with a *full* match, so ``**será**,`` arrived as a single unit,
    failed that match, and was appended as ordinary text -- and `_tidy` then
    stripped the marker, taking the word with it. Silently: a hole in a sentence
    reads as nothing at all, and 65 spans across this corpus were being removed
    that way.
    """
    for text, word in (
        ("El arte **pintan**. Otra frase.", "pintan"),
        ("El arte **pintan**, dice.", "pintan"),
        ("**Sin embargo**, la cuestión cambia.", "Sin embargo"),
        ("la respuesta **será** más difícil.", "será"),
        ("cuesta 3,5 **euros**; es caro.", "euros"),
        ("dijo **Ai-Da**.", "Ai-Da"),
        ("¿(el **arte**)?", "arte"),
        ("una frase **así:**", "así"),
    ):
        spans = diglot.parse_spans(text)
        plain = "".join(s.text for s in spans)
        assert word in plain, f"{word!r} lost from {text!r} -> {plain!r}"
        assert "\x00" not in plain, "no placeholder marker may reach the text"
        assert "".join(plain.split()) == "".join(text.replace("**", "").split()), \
            f"the text changed: {text!r} -> {plain!r}"


def test_bold_emphasis_survives_beside_punctuation():
    """Losing the word was the bug; losing the emphasis while keeping the word
    would be the quieter half of it."""
    spans = diglot.parse_spans("El arte **pintan**, y **sigue**.")
    targets = [s for s in spans if s.target]
    assert [s.text.strip() for s in targets] == ["pintan", "sigue"], spans


def test_every_bold_span_in_the_corpus_survives_into_the_text():
    """The corpus-wide form of the same check, because 65 of these were being
    deleted and no hand-written unit test would have guessed the pattern.

    The invariant: whatever is inside ``**...**`` must still be somewhere in the
    parse -- either in the reading text or in a gloss. A gloss is allowed because
    with no closing ``` `` ``` of its own, ``(**las instrucciones**)`` is a bolded
    translation in parentheses, and a parenthetical translation is exactly what a
    gloss is. What is *not* allowed is for the words to be nowhere at all, which
    is what the bug did.
    """
    corpus = corpus_dir()
    if not corpus.is_dir():
        pytest.skip("corpus not available")
    bold = re.compile(r"\*\*([^*]+)\*\*")
    words = re.compile(r"[^\W\d_]+", re.UNICODE)
    checked = 0
    lost: list[str] = []
    for path in sorted(corpus.glob("*.md")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "**" not in line:
                continue
            spans = diglot.parse_spans(line)
            plain = "".join(s.text for s in spans)
            glossed = " ".join(s.gloss or "" for s in spans)
            for match in bold.finditer(line):
                for word in words.findall(match.group(1)):
                    checked += 1
                    if word not in plain and word not in glossed:
                        lost.append(f"{path.name}: {word!r} from {match.group(0)!r}")
    assert checked > 500, f"expected to check many bold words, checked {checked}"
    assert not lost, "bold words vanished into the parse:\n" + "\n".join(lost[:20])


def test_italic_gloss_attaches_to_the_spanish_run():
    spans = diglot.parse_spans("la historia del arte **pintan** (*they paint*) un panorama")
    spanish = [s for s in spans if s.lang == "es"]
    assert any(s.gloss == "they paint" for s in spanish), spans


def test_a_gloss_never_keeps_the_marks_it_was_wrapped_in():
    """Glosses are written several ways and the closing marks ended up inside the
    value, which the reader then showed literally: ``To be made up of / composed
    of*):`` was in the vocabulary box of several lessons."""
    from app.diglot import _clean_gloss

    for raw, want in (
        ("to become", "to become"),
        ("*to become*", "to become"),
        (" to become ", "to become"),
        ("*to become*)", "to become"),
        ("*to become*):", "to become"),
        ("to become):", "to become"),
        ("(*to become*)", "to become"),
        ("to become,", "to become"),
        ("", ""),
    ):
        assert _clean_gloss(raw) == want, raw

    # And end to end, through the two places a gloss is read.
    spans = diglot.parse_spans("el estilo que **se remonta a** (**goes back to**) un puñado")
    assert any(s.gloss == "goes back to" for s in spans), spans

    text = "# T\n\nEl arte crece.\n\n---\n\n### POST-READING ANCHORS\n\n"
    text += "**Recycled Vocabulary Box**\n\n- **sondear** (*to probe*) — used twice: “vale la pena sondear”\n"
    article = diglot.parse_article(text, fallback_slug="t")
    assert article.vocab[0].gloss == "to probe", article.vocab[0].gloss


def test_bare_parenthesis_gloss_attaches():
    spans = diglot.parse_spans("el estilo que **se remonta a** (goes back to) un puñado")
    spanish = [s for s in spans if s.lang == "es"]
    assert any(s.gloss == "goes back to" for s in spanish)


def test_english_parenthetical_is_not_swallowed_as_a_gloss():
    spans = langs(
        "The cadence is reminiscent of the Song of Solomon (one of the greatest love "
        "poems ever composed), which is saying something."
    )
    assert [lang for lang, _ in spans] == ["en"]
    assert not any(s.gloss for s in diglot.parse_spans(
        "The cadence is reminiscent of the Song of Solomon (one of the greatest love "
        "poems ever composed), which is saying something."
    ))


def test_links_are_unwrapped_and_superscripts_dropped():
    spans = diglot.parse_spans("See [the paper](https://example.com/x)<sup>1</sup> for detail.")
    text = "".join(s.text for s in spans)
    assert "https://example.com" not in text
    assert "<sup>" not in text
    assert "the paper" in text


def test_accented_word_is_spanish_evidence():
    assert diglot.word_score("atención") > 0
    assert diglot.word_score("the") < 0
    assert diglot.word_score("no") == 0  # genuinely both


def test_words_in_both_lexicons_are_neutral():
    assert "real" in diglot._ES_WORDS and "real" in diglot._EN_WORDS
    assert diglot.word_score("real") == 0


# ------------------------------------------------------------------ parser --


ARTICLE = """### Article Identification & Preview

- **Article Title:** A Test Article
- **Author:** Someone
- **Direct URL:** https://example.com/a
- **Preview:** A short preview.

# A Test Article

**By Someone**

El primer párrafo **tiene** una frase en español y el resto en inglés.

A second paragraph, entirely in English, with no Spanish at all.

---

### POST-READING ANCHORS

**Recycled Vocabulary Box**

- **tener** / **tiene** (*to have*)

---

**Grammar Breakdown**

1. **Present Tense:** The verb *tiene* is third person singular.
"""


def write(tmp_path: Path, text: str, name: str = "a-test-article.md") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_front_matter(tmp_path):
    article = diglot.parse_file(write(tmp_path, ARTICLE))[0]
    assert article.title == "A Test Article"
    assert article.author == "Someone"
    assert article.url == "https://example.com/a"
    assert article.preview == "A short preview."
    assert article.level is None


def test_parse_level_from_front_matter(tmp_path):
    text = ARTICLE.replace("- **Preview:** A short preview.",
                           "- **Preview:** A short preview.\n- **Level:** B1 - intermediate")
    article = diglot.parse_file(write(tmp_path, text))[0]
    assert article.level == "B1 - intermediate"


def test_parse_body_blocks(tmp_path):
    article = diglot.parse_file(write(tmp_path, ARTICLE))[0]
    kinds = [b.kind for b in article.blocks]
    assert kinds.count("p") == 2
    assert all(b.kind != "h" or b.plain != "POST-READING ANCHORS" for b in article.blocks)


def test_body_repeat_of_the_title_is_dropped(tmp_path):
    """The body opens by repeating the front matter's title and byline; the
    reader renders those as the page header, so the copies must go."""
    article = diglot.parse_file(write(tmp_path, ARTICLE))[0]
    assert not any(b.kind == "byline" for b in article.blocks)
    assert not any(b.kind == "h" and b.plain == "A Test Article" for b in article.blocks)


def test_a_later_heading_that_matches_the_title_is_kept(tmp_path):
    text = ARTICLE.replace(
        "A second paragraph, entirely in English, with no Spanish at all.",
        "# A Test Article\n\nA second paragraph, entirely in English, with no Spanish at all.",
    )
    article = diglot.parse_file(write(tmp_path, text))[0]
    assert any(b.kind == "h" and b.plain == "A Test Article" for b in article.blocks)


# ------------------------------------------ what is a heading, and what is not --


@pytest.mark.parametrize("line", [
    "*The Stories We Tell* | *Loneliness*",          # the magazine | column kicker
    "Posted April 29, 2026 | Reviewed by Lybi Ma",   # the publication line
    "Updated 3 October 2025",
    "9 February 2026",
    "| Topic | JEPA | CAT-AID |",                    # a Markdown table's header row
])
def test_page_furniture_is_not_read_as_a_heading(line):
    """All of these are short, contain no Spanish and end in no punctuation, so the
    shape test alone cannot tell them from a title -- and a reader's contents pane
    listed them as if the article had those sections."""
    assert not diglot._looks_like_heading_line(line)


@pytest.mark.parametrize("line", [
    "Loneliness is an emotional state",
    "A catalyst",
    "References",
    "What Linguists Mean by 'Language'",
    "The Stories We Tell",
])
def test_a_real_title_is_still_a_heading(line):
    """The tightening has to lean on shapes a title does not have, not on shortness:
    a bare subheading is exactly what this rule exists to find."""
    assert diglot._looks_like_heading_line(line)


def test_a_heading_that_repeats_is_read_as_boilerplate(tmp_path):
    """The contents pane drops repeats, because a section title does not occur twice
    in one article -- when one does, it is a sidebar the extractor swept up twice, and
    listing it claims sections the article does not have."""
    sidebar = "### THE BASICS\n**Understanding Loneliness**\n"
    text = ARTICLE.replace(
        "A second paragraph, entirely in English, with no Spanish at all.",
        f"{sidebar}\nA paragraph of prose, in English, with no Spanish at all.\n\n"
        f"{sidebar}\nAnother paragraph of prose, in English, with no Spanish in it.",
    )
    article = diglot.parse_file(write(tmp_path, text))[0]
    headings = [b.plain for b in article.blocks if b.kind == "h"]
    assert headings.count("THE BASICS") == 2, headings
    # Both are still *in* the lesson -- the body shows what the file says. It is the
    # contents list that refuses to navigate to furniture.
    assert len(headings) == len(set(headings)) + 2


def test_parse_anchors(tmp_path):
    article = diglot.parse_file(write(tmp_path, ARTICLE))[0]
    assert len(article.vocab) == 1
    assert "tener" in article.vocab[0].term
    assert article.vocab[0].gloss == "to have"
    assert len(article.grammar) == 1
    assert article.grammar[0].title == "Present Tense"
    assert "third person" in (article.grammar[0].explanation or "")


def test_focus_pairs_harvest_bold_words(tmp_path):
    article = diglot.parse_file(write(tmp_path, ARTICLE))[0]
    pairs = article.focus_pairs()
    assert [p.es.strip() for p in pairs] == ["tiene"]


def test_stats_count_words_by_language(tmp_path):
    article = diglot.parse_file(write(tmp_path, ARTICLE))[0]
    stats = article.stats()
    assert stats["es_words"] > 0 and stats["en_words"] > 0
    assert 0 < stats["spanish_ratio"] < 1
    assert stats["paragraphs"] == 2


def test_article_without_a_title_is_rejected(tmp_path):
    path = write(tmp_path, "just some prose with no structure at all.\n", "untitled.md")
    assert diglot.parse_file(path) == []


def test_slug_comes_from_the_filename(tmp_path):
    """Two imported articles can share a title; the filename disambiguates."""
    path = write(tmp_path, ARTICLE, "a-test-article-fcd6ab.md")
    article = diglot.parse_file(path)[0]
    assert article.slug == "a-test-article-fcd6ab"
    assert article.title == "A Test Article"


def test_day_headings_split_a_compilation(tmp_path):
    text = (
        "### Day 1\n\n### Article Identification & Preview\n\n"
        "- **Article Title:** First\n\nEl uno **tiene** algo.\n\n"
        "---\n\n### POST-READING ANCHORS\n\n**Recycled Vocabulary Box**\n\n- **tener** (*to have*)\n\n"
        "### Day 2\n\n### Article Identification & Preview\n\n"
        "- **Article Title:** Second\n\nEl dos **tiene** algo.\n\n"
    )
    articles = diglot.parse_file(write(tmp_path, text, "diglot.md"))
    assert [a.title for a in articles] == ["First", "Second"]
    assert [a.day for a in articles] == ["Day 1", "Day 2"]


def test_reference_lines_are_set_apart(tmp_path):
    text = ARTICLE.replace(
        "A second paragraph, entirely in English, with no Spanish at all.",
        "[7] Olsson, et al., Induction Heads, Transformer Circuits Thread, 2022. https://example.com/x",
    )
    article = diglot.parse_file(write(tmp_path, text))[0]
    assert any(b.kind == "ref" for b in article.blocks)


# ------------------------------------------------------------------ corpus --


@pytest.mark.skipif(not CORPUS.is_dir(), reason="the diglot corpus is not on this machine")
def test_corpus_parses():
    articles = diglot.parse_corpus(CORPUS)
    assert len(articles) >= 15
    for article in articles:
        assert article.title
        assert article.blocks
        assert 0.1 < article.stats()["spanish_ratio"] < 0.95
        # Every article in the corpus has a post-reading section.
        assert article.vocab or article.grammar


@pytest.mark.skipif(not CORPUS.is_dir(), reason="the diglot corpus is not on this machine")
def test_corpus_segmentation_is_plausible():
    """No long span should be mostly in the other language.

    This is the regression net for the segmenter: it caught the inverted Viterbi
    cost and the hardcoded-Spanish bold spans, both of which produced spans of
    fluent English labelled Spanish.
    """
    offenders: list[str] = []
    checked = 0
    for article in diglot.parse_corpus(CORPUS):
        for block in article.blocks:
            if block.kind != "p":
                continue
            for span in block.spans:
                words = diglot._WORD_RE.findall(span.text)
                if len(words) < 3:
                    continue
                checked += 1
                if span.lang == "es":
                    wrong = [w for w in words if diglot.word_score(w) < 0]
                else:
                    wrong = [w for w in words if diglot.word_score(w) > 0]
                if len(wrong) / len(words) > 0.34:
                    offenders.append(f"[{span.lang}] {span.text[:90]}")
    assert checked > 1000, f"expected a large corpus, only checked {checked} spans"
    assert not offenders, f"{len(offenders)} implausible spans:\n" + "\n".join(offenders[:10])
