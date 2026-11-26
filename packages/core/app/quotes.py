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
from .languages import DEFAULT_PAIR, LanguagePair, get_pair_or_default
from .reading import lemmas_in

# Sentence-final punctuation and abbreviations (case-sensitive single initials,
# case-insensitive explicit abbreviations) delegated to ``app.languages``.
_END = DEFAULT_PAIR.target.sentence_end_re
_ABBREV = DEFAULT_PAIR.target.abbrev_re


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


def sentences(plain: str, *, pair: LanguagePair | None = None) -> list[Sentence]:
    """Split a paragraph into sentences by offset, keeping the text between them."""
    resolved = pair or DEFAULT_PAIR
    end_re = resolved.target.sentence_end_re
    abbrev_re = resolved.target.abbrev_re
    out: list[Sentence] = []
    start = 0
    for match in end_re.finditer(plain):
        if (abbrev_re and abbrev_re.search(plain[start:match.end()])) or _is_decimal(plain, match.start()):
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
        return None, "the page has changed since it was loaded. Reload and try again."
    raw_start = index[at]
    raw_end = index[at + len(needle) - 1] + 1

    pair = get_pair_or_default(getattr(article, "language_pair", None))
    target_code = getattr(article, "target_lang", pair.target.code)
    target_name = pair.target.name

    found = sentences(plain, pair=pair)
    touched = [s for s in found if s.end > raw_start and s.start < raw_end]
    if not touched:
        return None, "that is not a whole sentence"
    start, end = touched[0].start, touched[-1].end
    text_out = plain[start:end].strip()

    es, en = _split(block.spans, start, end, target_lang=target_code)
    if not es.strip():
        return None, f"there is no {target_name} in that sentence"
    if not _vocabulary(es, plain, pair=pair):
        return None, f"that sentence has no {target_name} words to learn in it"

    clipped = clip(block.spans, start, end)
    return Quote(
        text=text_out, es=es.strip(), en=en.strip(),
        block_index=block_index, start=start, end=end,
        term=_focus(clipped, target_lang=target_code),
        spans=[s.to_dict() for s in clipped],
        glosses=" ".join(s.gloss for s in clipped if s.gloss),
    ), ""


def _vocabulary(es: str, paragraph: str, *, pair: LanguagePair | None = None) -> list[str]:
    known = set(lemmas_in(paragraph, pair=pair))
    return [word for word in lemmas_in(es, pair=pair) if word in known]


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


def _split(spans: list[Span], start: int, end: int, *, target_lang: str = "es") -> tuple[str, str]:
    """The target language and the base language inside a character range, as plain text."""
    target_parts: list[str] = []
    base_parts: list[str] = []
    for span, s_start, s_end in _placed(spans):
        overlap_start, overlap_end = max(start, s_start), min(end, s_end)
        if overlap_start >= overlap_end:
            continue
        piece = span.text[overlap_start - s_start:overlap_end - s_start]
        (target_parts if span.lang == target_lang else base_parts).append(piece)
    return "".join(target_parts), "".join(base_parts)


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


def _focus(clipped: list[Span], *, target_lang: str = "es") -> str | None:
    """The word the lesson itself bolded, which is what the quote is teaching."""
    for span in clipped:
        if span.lang == target_lang and span.target and span.text.strip():
            return span.text.strip()
    return None
