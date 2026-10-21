"""What the reader actually read, inferred from scroll position.

The analytics need one fact the app never recorded: which Spanish words passed
in front of someone's eyes. Asking the browser to report that would mean
instrumenting the reading surface, which is the last place to add work.

It does not have to. The reader already saves its scroll position every few
seconds, and an article's words are in a known order, so the tokens between the
previous position and the current one can be worked out on the server from a
request that was happening anyway. The reading path gains nothing to do.

Two deliberate choices:

*   **Position maps to words, not pixels.** Block heights vary, but word count
    is a good proxy and the alternative -- measuring rendered heights -- needs
    the layout engine. The result is an estimate, and only ever used for
    aggregate counts, where a block's worth of drift does not matter.
*   **Only newly-covered ground is credited.** Re-reading a section should not
    double the word count, so attribution runs from the furthest point already
    reached. A word met again in a *different* article still counts, which is
    what makes "repeatedly encountered" mean something.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .diglot import Article, _WORD_RE, content_fingerprint
from .vocab import fold
from .glossary import STOPWORDS


@dataclass(frozen=True)
class Window:
    """One paragraph's worth of the article, positioned along the scroll."""

    start: float                 # cumulative share of the prose at this point
    end: float
    lemmas: Counter[str]         # Spanish content words, folded
    glossed: frozenset[str]      # lemmas the article itself translates


@dataclass(frozen=True)
class ArticleWindows:
    windows: tuple[Window, ...]
    glossed: frozenset[str]

    @property
    def total_tokens(self) -> int:
        return sum(sum(window.lemmas.values()) for window in self.windows)


_SENTENCE_PUNCT = ".!?…¿¡\"'“‘-–—("


def lemmas_in(text: str) -> list[str]:
    """Folded Spanish content words in a passage, in order.

    Two filters, and the second is subtle. Function words go because they are
    most of any Spanish text and none of what anyone is learning. **Capitalised
    words go unless they begin a sentence**: a proper noun stays in English even
    inside a Spanish clause -- "desarrollado en London por Francis" is the weave
    working correctly -- and counting those as Spanish vocabulary inflates the
    words-read total and puts noise in the corpus graph.
    """
    out: list[str] = []
    at_sentence_start = True
    for match in _WORD_RE.finditer(text):
        word = match.group(0)
        previous = text[:match.start()].rstrip()
        starts_sentence = at_sentence_start or (previous[-1:] in _SENTENCE_PUNCT if previous else True)
        at_sentence_start = False

        if word[:1].isupper() and not starts_sentence:
            continue
        folded = fold(word)
        if len(folded) < 3 or folded in STOPWORDS:
            continue
        out.append(folded)
    return out


_CACHE: dict[str, ArticleWindows] = {}
_CACHE_LIMIT = 64


def windows_for(article: Article) -> ArticleWindows:
    """Windows for an article, cached against its content fingerprint.

    Keyed by content rather than by slug so an edited or re-imported article
    recomputes, and held in a plain dict because ``Article`` is not hashable
    (it carries lists) and the object itself is already parsed and in memory.
    """
    key = content_fingerprint(article)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit

    counts: list[tuple[int, Counter[str], frozenset[str]]] = []
    total_words = 0
    for block in article.blocks:
        if block.kind not in ("p", "h"):
            continue
        lemmas: Counter[str] = Counter()
        glossed: set[str] = set()
        words = 0
        for span in block.spans:
            words += len(_WORD_RE.findall(span.text))
            if span.lang != "es":
                continue
            lemmas_here = lemmas_in(span.text)
            for lemma in lemmas_here:
                lemmas[lemma] += 1
            if span.gloss:
                glossed.update(lemmas_here)
        if words:
            counts.append((words, lemmas, frozenset(glossed)))
            total_words += words

    if not total_words:
        data = ArticleWindows(windows=(), glossed=frozenset())
    else:
        windows: list[Window] = []
        cumulative = 0
        for words, lemmas, glossed in counts:
            start = cumulative / total_words
            cumulative += words
            windows.append(Window(start=start, end=cumulative / total_words,
                                  lemmas=lemmas, glossed=glossed))
        data = ArticleWindows(
            windows=tuple(windows),
            glossed=frozenset().union(*(w.glossed for w in windows)),
        )

    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[key] = data
    return data


def tokens_between(article: Article, start: float, end: float) -> Counter[str]:
    """Spanish tokens lying between two scroll positions.

    A paragraph belongs to the range that contains its **start**, not to every
    range it touches. Counting on overlap instead double-credits whatever
    paragraph straddles a boundary -- and since the reader saves its position
    every few seconds, boundaries are everywhere, so the totals ran high. With
    attribution on the start offset each paragraph is counted exactly once and
    consecutive ranges sum to their union.
    """
    if end <= start:
        return Counter()
    data = windows_for(article)
    counted: Counter[str] = Counter()
    for window in data.windows:
        if start <= window.start < end:
            counted.update(window.lemmas)
    return counted


def glossed_lemmas(article: Article) -> frozenset[str]:
    """Every lemma the article translates inline."""
    return windows_for(article).glossed
