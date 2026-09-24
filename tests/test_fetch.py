"""Tests for the extractor's judgement about what counts as article prose.

The specific thing being tested is bibliography detection, which is a small
piece of code with an outsized effect. Wikipedia's reference list is ordinary
`<p>` and `<li>` markup, so the extractor swept it up as prose and it ended up
in the middle of woven lessons -- untranslatable, unreadable, and enough to
make a paragraph register as "0% Spanish" and wreck any measurement of how
evenly the weave is spread.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.fetch import (FetchError, _looks_like_citation,  # noqa: E402
                       blocks_from_text, extract_article, search_web,
                       _parse_feed, _unwrap_ddg)


# ------------------------------------------------------------- citations --


def test_recognises_an_initials_bibliography():
    assert _looks_like_citation(
        "Bamford, J., Day, R. (2004). Extensive reading activities for teaching language. "
        "Cambridge University Press."
    )
    assert _looks_like_citation("Krashen, S. D. (1982). Principles and practice.")


def test_recognises_a_full_name_bibliography():
    assert _looks_like_citation(
        'Rott, Susanne; Williams, Jessica; Cameron, Richard (2002), "The effect of '
        'multiple-choice L1 glosses and input-output cycles on lexical acquisition"'
    )


def test_recognises_a_journal_citation_with_a_doi():
    assert _looks_like_citation(
        "1993, ELT J (1993) 47 (3): 250-267. doi: 10.1093/elt/47.3.250"
    )


def test_recognises_a_numbered_reference_run():
    assert _looks_like_citation(
        "1 2 3 4 Bamford, J. (2004). Extensive reading activities. Cambridge."
    )


def test_leaves_ordinary_prose_alone():
    """The filter has to be conservative: dropping a real paragraph loses part
    of the article, which is worse than keeping a citation."""
    prose = [
        "Extensive reading is the process of reading longer, easier texts for an "
        "extended period of time without a breakdown of comprehension.",
        "Walker, a professor of neuroscience, argues that sleep is the single most "
        "effective thing we can do to reset our brain and body health.",
        "The study, published in 2019, followed two hundred students for a year.",
        "Berkeley, California is where the research centre is based.",
        "Cobb (2007), McQuillan and Krashen (2008), and Cobb (2008) offer contrasting "
        "perspectives on whether graded readers build vocabulary.",
        "In 1993 the journal published a special issue on reading.",
    ]
    for text in prose:
        assert not _looks_like_citation(text), text


def test_a_long_paragraph_is_never_treated_as_a_citation():
    """Even a citation-heavy paragraph is prose if it is long enough -- real
    writing cites sources, and cutting it would be worse than keeping it."""
    long_text = ("The study of reading has a long history. " * 25) + "Smith, A. (1999)."
    assert not _looks_like_citation(long_text)


# -------------------------------------------------------------- extraction --


def filler(subject: str, times: int = 6) -> str:
    return " ".join(
        f"Extensive reading means reading a great deal of easy {subject} material "
        "for pleasure, and it is one of the most reliable routes to fluency."
        for _ in range(times)
    )


def test_extract_drops_a_reference_list():
    html = f"""
    <html><head><title>A Page</title></head><body><article>
      <p>{filler("language")}</p>
      <p>Krashen argued that comprehensible input drives acquisition, and that
         overt grammar study plays a much smaller role than was once assumed.</p>
      <p>Bamford, J., Day, R. (2004). Extensive reading activities for teaching
         language. Cambridge University Press.</p>
      <p>Krashen, S. D. (1982). Principles and practice in second language
         acquisition. Pergamon Press.</p>
      <p>{filler("graded reader")}</p>
    </article></body></html>
    """
    result = extract_article(html)
    texts = [b["text"] for b in result["blocks"]]
    assert result["dropped_citations"] == 2, [t[:60] for t in texts]
    assert not any("Pergamon" in t for t in texts)
    assert not any("Cambridge University Press" in t for t in texts)


def test_extract_still_finds_the_article_beside_the_references():
    html = f"""
    <html><head><title>T</title></head><body><article>
      <p>{filler("reading", 3)}</p>
      <p>{filler("graded reader", 3)}</p>
      <p>{filler("vocabulary", 3)}</p>
    </article></body></html>
    """
    result = extract_article(html)
    assert result["words"] > 100
    assert result["dropped_citations"] == 0


# ------------------------------------------------------------------ feeds --


def test_parses_an_rss_feed():
    rss = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>First</title><link>https://example.com/1</link>
        <description>About reading and language</description></item>
    </channel></rss>"""
    items = _parse_feed(rss)
    assert items == [{"title": "First", "url": "https://example.com/1",
                      "snippet": "About reading and language"}]


def test_parses_an_atom_feed():
    atom = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>Second</title><link href="https://example.com/2"/>
        <summary>A summary</summary></entry>
    </feed>"""
    items = _parse_feed(atom)
    assert len(items) == 1
    assert items[0]["url"] == "https://example.com/2"
    assert items[0]["title"] == "Second"


def test_a_malformed_feed_yields_nothing_rather_than_raising():
    assert _parse_feed("this is not xml at all") == []


def test_unwraps_a_duckduckgo_redirect():
    wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Farticle&rut=abc"
    assert _unwrap_ddg(wrapped) == "https://example.com/article"


def test_leaves_a_plain_link_alone():
    assert _unwrap_ddg("https://example.com/x") == "https://example.com/x"


# --------------------------------------------------------------- searching --


def _a_search_that_returns(count: int):
    """A fixed list of results, standing in for the network."""
    return [
        {"url": f"https://example.com/article-{index}", "title": f"Article number {index}",
         "snippet": "A long piece about something worth reading.", "site": "example.com",
         "source": "scripted"}
        for index in range(count)
    ]


def test_a_search_shows_a_page_at_a_time(monkeypatch):
    from app import fetch

    scripted = _a_search_that_returns(5)
    monkeypatch.setattr(fetch, "_search_duckduckgo", lambda q, proxy=None: list(scripted))
    monkeypatch.setattr(fetch, "_search_wikipedia", lambda q, proxy=None: [])
    monkeypatch.setattr(fetch, "_search_feeds", lambda q, proxy=None: [])

    page = fetch.search_web("anything", limit=2)
    assert [row["url"] for row in page] == ["https://example.com/article-0",
                                            "https://example.com/article-1"]


def test_asking_for_different_results_returns_the_ones_further_down(monkeypatch):
    """This is the whole mechanism behind "these are not what I want". The
    sources are deterministic, so re-running the query returns the identical
    list -- the only honest way to show different results is to exclude the ones
    already seen and ask for what comes next."""
    from app import fetch

    scripted = _a_search_that_returns(5)
    monkeypatch.setattr(fetch, "_search_duckduckgo", lambda q, proxy=None: list(scripted))
    monkeypatch.setattr(fetch, "_search_wikipedia", lambda q, proxy=None: [])
    monkeypatch.setattr(fetch, "_search_feeds", lambda q, proxy=None: [])

    first = fetch.search_web("anything", limit=2)
    second = fetch.search_web("anything", limit=2, exclude={row["url"] for row in first})
    assert second, "there were more results to give"
    assert not {row["url"] for row in first} & {row["url"] for row in second}
    assert [row["url"] for row in second] == ["https://example.com/article-2",
                                              "https://example.com/article-3"]


def test_excluding_is_indifferent_to_a_trailing_slash(monkeypatch):
    """The URL that comes back from a source and the URL the browser sends back
    are the same address written twice, and a difference of one character must
    not make the reader see a result they just said they did not want."""
    from app import fetch

    scripted = _a_search_that_returns(3)
    monkeypatch.setattr(fetch, "_search_duckduckgo", lambda q, proxy=None: list(scripted))
    monkeypatch.setattr(fetch, "_search_wikipedia", lambda q, proxy=None: [])
    monkeypatch.setattr(fetch, "_search_feeds", lambda q, proxy=None: [])

    first = fetch.search_web("anything", limit=2)
    second = fetch.search_web("anything", limit=2,
                              exclude={row["url"] + "/" for row in first})
    assert [row["url"] for row in second] == ["https://example.com/article-2"]


def test_excluding_everything_yields_nothing_rather_than_repeating_itself(monkeypatch):
    """When the sources have no more to give, the answer is an empty list. A
    quiet fallback to the results already shown would look like the button
    worked and would waste the reader's click."""
    from app import fetch

    scripted = _a_search_that_returns(3)
    monkeypatch.setattr(fetch, "_search_duckduckgo", lambda q, proxy=None: list(scripted))
    monkeypatch.setattr(fetch, "_search_wikipedia", lambda q, proxy=None: [])
    monkeypatch.setattr(fetch, "_search_feeds", lambda q, proxy=None: [])

    assert fetch.search_web("anything", exclude={row["url"] for row in scripted}) == []


# ------------------------------------------------------------ pasted text --


def test_blank_lines_separate_paragraphs():
    result = blocks_from_text(f"{filler('handwriting')}\n\n{filler('typing')}\n")
    texts = [b["text"] for b in result["blocks"]]
    assert len(texts) == 2
    assert result["words"] > 200


def test_hard_wrapped_lines_are_joined_back_into_one_paragraph():
    """Most sources wrap at a column width, so a copy arrives with a newline every
    eighty characters. Treating each of those as its own paragraph would weave a
    single paragraph as a dozen one-line ones."""
    wrapped = "\n".join(filler("handwriting").split())
    result = blocks_from_text(f"{wrapped}\n\n{filler('typing')}")
    assert len(result["blocks"]) == 2
    assert "\n" not in result["blocks"][0]["text"]


def test_the_headline_becomes_the_title_rather_than_a_paragraph():
    result = blocks_from_text("How Handwriting Trains the Brain\n\n" + filler("handwriting"))
    assert result["title"] == "How Handwriting Trains the Brain"
    assert all("Trains the Brain" not in b["text"] for b in result["blocks"])


def test_a_byline_is_read_as_a_byline_not_as_a_headline():
    """"By Sarah Smith" is short and ends in no punctuation, so the headline rule
    would take it for the title if the byline were tested second."""
    text = "How Handwriting Trains the Brain\n\nBy Sarah Smith\n\n" + filler("handwriting")
    result = blocks_from_text(text)
    assert result["title"] == "How Handwriting Trains the Brain"
    assert result["byline"] == "Sarah Smith"


def test_a_short_opening_paragraph_is_not_mistaken_for_a_headline():
    """The headline rule keys off the absence of a full stop, so a real one-line
    opening paragraph -- which ends like a sentence -- stays in the body."""
    opening = "This is a short opening line that still ends like a sentence."
    result = blocks_from_text(f"{opening}\n\n{filler('handwriting')}")
    assert result["title"] is None
    assert result["blocks"][0]["text"] == opening


def test_markdown_headings_survive_as_headings():
    """A paste from a Markdown source, or from the app's own lessons: headings are
    what the reader's contents pane is built from, so they have to survive."""
    result = blocks_from_text(
        "# The Title\n\n" + filler("handwriting") + "\n\n## A Section\n\n" + filler("typing")
    )
    kinds = [b["kind"] for b in result["blocks"]]
    assert kinds == ["p", "h", "p"], kinds
    assert result["blocks"][1]["text"] == "A Section"
    # A leading heading is the article's own title, which is stored separately.
    assert result["title"] == "The Title"


def test_a_paste_with_no_blank_lines_falls_back_to_its_newlines():
    """There is no other structure to find, and one enormous paragraph is the one
    shape the weave cannot distribute Spanish across."""
    lines = "\n".join(
        "Reading a great deal of easy material for pleasure is the reliable route to fluency."
        for _ in range(20)
    )
    result = blocks_from_text(lines)
    assert len(result["blocks"]) == 20


def test_windows_line_endings_are_handled():
    result = blocks_from_text(f"{filler('handwriting')}\r\n\r\n{filler('typing')}\r\n")
    assert len(result["blocks"]) == 2


def test_a_paste_too_short_to_be_an_article_is_refused():
    """With the same reason the fetcher gives: a headline or a summary is not a
    lesson, and the reader should hear why rather than get a two-line one."""
    with pytest.raises(FetchError) as caught:
        blocks_from_text("How Handwriting Trains the Brain\n\nA short summary of the piece.")
    assert "words" in str(caught.value)


def test_an_empty_paste_is_refused():
    with pytest.raises(FetchError):
        blocks_from_text("   \n\n  ")
