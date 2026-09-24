"""Sentences worth keeping, and what they are worth keeping *for*.

The useful unit of a language is not the word. A learner who collects *por lo
tanto* has a translation; a learner who collects the sentence they met it in has
a construction, a register and a memory. This is the oldest technique in
self-directed language learning and it has a name -- sentence mining -- and the
only thing an app needs to do to support it is make keeping a sentence free.

So this module turns a selection in the reader into a stored quote. It does the
work on the server rather than in the browser for one reason: the article's own
parsed spans are the authority on where the Spanish is, and the quote should be
cut to the same boundaries everything else in the app uses. A quote therefore
arrives with three things already separated, and none of them cost a model call:

*   ``text`` -- the sentence as it reads, mixed languages, exactly as the reader
    saw it. This is what is shown back to them.
*   ``es`` -- the Spanish part on its own. This is what gets searched, and what a
    cloze review would blank a word out of.
*   ``en`` -- the English part, which in a diglot is the sentence's own gloss.
    A fully Spanish sentence has none, and that is reported honestly rather than
    filled in: the translation is not in the article, so the app does not pretend
    it is.

Sentences are found by their own punctuation rather than by a model, because a
sentence boundary is the one thing here that is genuinely well defined.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .diglot import Article, Span
from .reading import lemmas_in

# Sentence-final punctuation, including the closing quote or bracket that often
# follows it -- and the Spanish inverted marks are openers, so they are not here.
_END = re.compile(r"[.!?…]+[\"”'’)\]]*")
# Neither ``Sr.`` nor ``3.14`` ends a sentence. Abbreviations are matched as a
# short run of letters before the dot, numbers by a digit on both sides.
_ABBREV = re.compile(r"\b(?:[A-Z]|Sr|Sra|Dr|Dra|EE|UU|etc|p\. ej|aprox|núm)\.$", re.I)


@dataclass
class Sentence:
    start: int
    end: int
    text: str


@dataclass
class Quote:
    """A kept sentence, with its languages separated."""

    text: str
    es: str
    en: str
    block_index: int
    start: int
    end: int
    term: str | None
    spans: list[dict[str, Any]]
    # The English glosses inside the sentence. Kept because a learner often
    # remembers the *meaning* and not the Spanish -- "the one about annals" --
    # and searching that should find the sentence they kept.
    glosses: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "es": self.es, "en": self.en,
                "block_index": self.block_index, "start": self.start, "end": self.end,
                "term": self.term, "spans": self.spans, "glosses": self.glosses}


def sentences(plain: str) -> list[Sentence]:
    """Split a paragraph into sentences by offset, keeping the text between them.

    Conservative on purpose: an abbreviation or a decimal that is read as a
    sentence end would cut a quote in half, and a quote cut in half is worse than
    a quote that runs two sentences together.
    """
    out: list[Sentence] = []
    start = 0
    for match in _END.finditer(plain):
        if _ABBREV.search(plain[start:match.end()]) or _is_decimal(plain, match.start()):
            continue
        end = match.end()
        text = plain[start:end].strip()
        if text:
            out.append(Sentence(start=start, end=end, text=text))
        start = end
    tail = plain[start:].strip()
    if tail:
        out.append(Sentence(start=start, end=len(plain), text=tail))
    return out


def _is_decimal(plain: str, dot: int) -> bool:
    return (dot > 0 and dot + 1 < len(plain)
            and plain[dot] == "." and plain[dot - 1].isdigit() and plain[dot + 1].isdigit())


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def locate(article: Article, block_index: int, text: str) -> tuple[Quote | None, str]:
    """Find the sentences a selection covers, and cut the quote to their edges.

    ``text`` is what the reader selected, already sentence-aligned by the browser
    -- the reader renders each sentence as its own element, so the selection
    arrives at sentence granularity and this only has to confirm it against the
    article and widen it if the browser handed back a fragment.

    Returns ``(quote, "")`` or ``(None, reason)``. The reason matters: "nothing
    happened" is a bad answer to a click, and every one of these is something the
    reader can act on.
    """
    if not text.strip():
        return None, "nothing was selected"
    if not (0 <= block_index < len(article.blocks)):
        return None, "that passage is not in this article"
    block = article.blocks[block_index]
    plain = block.plain
    needle = _norm(text)
    if not needle:
        return None, "nothing was selected"

    # Where the selection sits in the paragraph. Whitespace is normalised on both
    # sides, because the reader's rendered text collapses it and the article's
    # does not.
    haystack, index = _normalise(plain)
    at = haystack.find(needle)
    if at < 0:
        return None, "the page has changed since it was loaded — reload and try again"
    raw_start = index[at]
    raw_end = index[at + len(needle) - 1] + 1

    found = sentences(plain)
    touched = [s for s in found if s.end > raw_start and s.start < raw_end]
    if not touched:
        return None, "that is not a whole sentence"
    start, end = touched[0].start, touched[-1].end
    text_out = plain[start:end].strip()

    es, en = _split(block.spans, start, end)
    if not es.strip():
        return None, "there is no Spanish in that sentence"
    if not _vocabulary(es, plain):
        # A name, or a stray abbreviation the segmenter read as Spanish. Keeping
        # it would put a sentence with nothing to learn in the collection, and
        # the collection is only useful if everything in it is worth returning to.
        return None, "that sentence has no Spanish words to learn in it"

    clipped = clip(block.spans, start, end)
    return Quote(
        text=text_out, es=es.strip(), en=en.strip(),
        block_index=block_index, start=start, end=end,
        term=_focus(clipped),
        spans=[s.to_dict() for s in clipped],
        glosses=" ".join(s.gloss for s in clipped if s.gloss),
    ), ""


def _vocabulary(es: str, paragraph: str) -> list[str]:
    """The Spanish content words in a quote that are vocabulary worth learning.

    ``lemmas_in`` drops function words and capitals that are not sentence-initial,
    which is how the rest of the app decides what counts as Spanish vocabulary.
    But it judges "sentence-initial" from the string it is given, and a quote's
    Spanish usually starts *mid-sentence* -- "dijo Ai-Da" begins with a verb --
    so its first word would always be treated as starting a sentence and a name
    would slip through as vocabulary.

    The paragraph is the honest context: a word counts here only if it also
    counts there, which is the same answer the corpus graph and the exposure
    counts give for that word.
    """
    known = set(lemmas_in(paragraph))
    return [word for word in lemmas_in(es) if word in known]


def _normalise(plain: str) -> tuple[str, list[int]]:
    """The collapsed, trimmed, lowercased text, and where each character came from.

    Built in one pass because the two have to agree exactly: a map computed
    separately from the string it maps is a bug waiting for a paragraph that
    starts with a space.
    """
    out: list[str] = []
    index: list[int] = []
    pending_space = False
    for position, char in enumerate(plain):
        if char.isspace():
            pending_space = True
            continue
        if pending_space and out:
            out.append(" ")
            index.append(position)
        lowered = char.lower()
        out.append(lowered if len(lowered) == 1 else char)
        index.append(position)
        pending_space = False
    return "".join(out), index


def _split(spans: list[Span], start: int, end: int) -> tuple[str, str]:
    """The Spanish and the English inside a character range, as plain text."""
    spanish: list[str] = []
    english: list[str] = []
    for span, s_start, s_end in _placed(spans):
        overlap_start, overlap_end = max(start, s_start), min(end, s_end)
        if overlap_start >= overlap_end:
            continue
        piece = span.text[overlap_start - s_start:overlap_end - s_start]
        (spanish if span.lang == "es" else english).append(piece)
    return "".join(spanish), "".join(english)


def _placed(spans: list[Span]) -> list[tuple[Span, int, int]]:
    """Each span with the character range it occupies in the block's plain text."""
    out: list[tuple[Span, int, int]] = []
    at = 0
    for span in spans:
        out.append((span, at, at + len(span.text)))
        at += len(span.text)
    return out


def clip(spans: list[Span], start: int, end: int) -> list[Span]:
    """The spans a quote covers, cut to its edges.

    A quote is rendered with the reader's own span markup, so the Spanish stays
    Spanish and the glosses stay attached. Slicing rather than re-parsing is what
    keeps the quote looking exactly like the sentence the reader selected.
    """
    out: list[Span] = []
    for span, s_start, s_end in _placed(spans):
        overlap_start, overlap_end = max(start, s_start), min(end, s_end)
        if overlap_start >= overlap_end:
            continue
        piece = span.text[overlap_start - s_start:overlap_end - s_start]
        if not piece:
            continue
        # The gloss belongs to the span's final word, so it survives only if the
        # cut kept that word.
        keeps_gloss = overlap_end >= s_end
        out.append(Span(lang=span.lang, text=piece,
                        gloss=span.gloss if keeps_gloss else None,
                        bold=span.bold, target=span.target))
    _trim(out)
    return out


def _trim(spans: list[Span]) -> None:
    """Drop the leading and trailing whitespace the cut left behind."""
    while spans and not spans[0].text.strip():
        spans.pop(0)
    while spans and not spans[-1].text.strip():
        spans.pop()
    if spans:
        spans[0] = _retext(spans[0], spans[0].text.lstrip())
        spans[-1] = _retext(spans[-1], spans[-1].text.rstrip())


def _retext(span: Span, text: str) -> Span:
    return Span(lang=span.lang, text=text, gloss=span.gloss, bold=span.bold, target=span.target)


def _focus(clipped: list[Span]) -> str | None:
    """The word the lesson itself bolded, which is what the quote is teaching."""
    for span in clipped:
        if span.lang == "es" and span.target and span.text.strip():
            return span.text.strip()
    return None
