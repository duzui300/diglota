"""Parser for the "diglot weave" article format.

The source articles live as Markdown in the corpus directory (``DIGLOT_CORPUS``)
and share a house style:

    ### Article Identification & Preview
    - **Article Title:** ...
    - **Author:** ...
    - **Direct URL:** ...
    - **Preview:** ...

    # Title
    **By Author**

    Body paragraphs, in which *English* and *Spanish* alternate mid-sentence.
    Spanish runs carry a parenthesised English gloss: ``(*by becoming*)``,
    and the lesson's focus words are bolded: ``**pintan**``.

    ---
    ### POST-READING ANCHORS
    **Recycled Vocabulary Box** ...
    **Grammar Breakdown** ...

The job of this module is to turn that prose back into structure: an ordered
list of paragraphs, each a list of alternating English / Spanish spans, with
glosses and focus-word bolding preserved. Everything downstream -- the reader,
the word-click dictionary, the flashcard generator -- is built on that.

The hard part is segmentation. Spanish is not delimited in the source; it is
simply *there*, mid-sentence, and a segmenter has to find where it starts and
stops. See :func:`segment` for how that is decided.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence

from .languages import (
    DEFAULT_PAIR,
    DEFAULT_PAIR_ID,
    EN_ES_PAIR,
    LanguagePair,
    fold_text,
    get_pair_or_default,
    slugify_text,
)
from .languages.en_es import (
    EN_ES_AMBIGUOUS,
    EN_HMM_WORDS,
    EN_SUFFIXES,
    ES_ACCENTS,
    ES_HMM_WORDS,
    ES_PROFILE,
    ES_SUFFIXES,
)

log = logging.getLogger("diglot.parser")

# --------------------------------------------------------------------------- #
# Markdown-level patterns
# --------------------------------------------------------------------------- #

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITALIC_GLOSS_RE = re.compile(r"\(\*(.+?)\*\)")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_SUP_RE = re.compile(r"<sup>(.*?)</sup>", re.S)
_HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED_RE = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
_FIELD_RE = re.compile(r"^\s*[-*+]?\s*\*\*(.+?):?\*\*\s*:?\s*(.*)$")

# Headings that introduce the machine-readable front matter rather than prose.
_FRONT_MATTER_HEADINGS = (
    "article identification",
    "article confirmation",
    "article preview",
    "article info",
)
_ANCHOR_HEADINGS = ("post-reading anchors", "post reading anchors", "anchors")

# A placeholder is smuggled through word segmentation as a single opaque token.
_PLACEHOLDER_RE = re.compile("\x00([BG])(\\d+)\x00")
# ...and it is also a unit boundary, so a placeholder glued to punctuation is
# still recognised as one. See the split in :func:`parse_spans`.
_UNIT_SPLIT = re.compile("(\\s+|\x00[BG]\\d+\x00)")


# --------------------------------------------------------------------------- #
# Language identification (delegated to ``app.languages``; EN-ES kept as default)
# --------------------------------------------------------------------------- #

_ES_WORDS = ES_HMM_WORDS
_EN_WORDS = EN_HMM_WORDS
_AMBIGUOUS = EN_ES_AMBIGUOUS | (_ES_WORDS & _EN_WORDS)
_ACCENTS = set(ES_ACCENTS)
_ES_SUFFIXES = ES_SUFFIXES
_EN_SUFFIXES = EN_SUFFIXES

# Clean Latin/Spanish word regex without inverted punctuation ``¿¡``.
_WORD_RE = ES_PROFILE.word_re


def _fold(word: str) -> str:
    """Lowercase and strip accents, for dictionary lookup only."""
    return fold_text(word)


def word_score(word: str, *, pair: LanguagePair | None = None) -> float:
    """Positive leans target language, negative leans base language, zero is undecided."""
    return (pair or EN_ES_PAIR).segmenter.word_score(word)


_SWITCH_PENALTY = 1.8
_PUNCTUATED_SWITCH = 0.40
_BREAK_CHARS = ".!?;:,…\"”’)]—–。！？；：、"
_MAX_SPAN_EVIDENCE = 3.5
_SWITCH_PUNCT_RE = re.compile(rf"[{re.escape(_BREAK_CHARS)}]\s*$")


def segment(
    scores: Sequence[float],
    weights: Sequence[float] | None = None,
    soft_break: Sequence[bool] | None = None,
    *,
    pair: LanguagePair | None = None,
) -> list[int]:
    """Viterbi decode per-token language scores into 0=base / 1=target labels."""
    return (pair or EN_ES_PAIR).segmenter.decode(scores, weights=weights, soft_break=soft_break)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class Span:
    """A run of text in one language, with optional gloss and emphasis."""

    lang: str
    text: str
    gloss: str | None = None
    bold: bool = False
    # True when the span was bolded in the source, i.e. it is a focus word of
    # the lesson rather than incidental target-language prose.
    target: bool = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"lang": self.lang, "text": self.text}
        if self.gloss:
            out["gloss"] = self.gloss
        if self.bold:
            out["bold"] = True
        if self.target:
            out["target"] = True
        return out


@dataclass
class Block:
    """One paragraph, heading, byline, date, quote or reference line."""

    kind: Literal["p", "h", "byline", "date", "quote", "ref"]
    spans: list[Span] = field(default_factory=list)
    level: int = 0

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "spans": [s.to_dict() for s in self.spans]}
        if self.kind == "h":
            out["level"] = self.level
        return out

    @property
    def plain(self) -> str:
        return "".join(s.text for s in self.spans)


@dataclass
class VocabAnchor:
    """An entry in the post-reading Recycled Vocabulary Box."""

    term: str
    gloss: str | None = None
    examples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"term": self.term, "gloss": self.gloss, "examples": self.examples}


@dataclass
class GrammarNote:
    """An entry in the post-reading Grammar Breakdown."""

    title: str
    example: str | None = None
    explanation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "example": self.example, "explanation": self.explanation}


@dataclass
class VocabPair:
    """A focus word and its English gloss, harvested from the body text."""

    es: str
    en: str | None = None
    count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"es": self.es, "en": self.en, "count": self.count}


@dataclass
class Article:
    slug: str
    title: str
    author: str | None
    url: str | None
    preview: str | None
    date: str | None
    day: str | None
    blocks: list[Block]
    vocab: list[VocabAnchor]
    grammar: list[GrammarNote]
    source: str
    level: str | None = None
    register: str | None = None
    # The grain the lesson was woven at: phrases inside sentences, or whole
    # sentences. Read from the front matter, so a lesson says how it was made --
    # the same reason the level and the amount are recorded there.
    weave: str | None = None
    language_pair: str = DEFAULT_PAIR_ID
    base_lang: str = DEFAULT_PAIR.base.code
    target_lang: str = DEFAULT_PAIR.target.code

    def to_dict(self, *, with_blocks: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "slug": self.slug,
            "title": self.title,
            "author": self.author,
            "url": self.url,
            "preview": self.preview,
            "date": self.date,
            "day": self.day,
            "level": self.level,
            "register": self.register,
            "weave": self.weave,
            "language_pair": self.language_pair,
            "base_lang": self.base_lang,
            "target_lang": self.target_lang,
            "source": self.source,
            "vocab": [v.to_dict() for v in self.vocab],
            "grammar": [g.to_dict() for g in self.grammar],
            "stats": self.stats(),
        }
        if with_blocks:
            data["blocks"] = [b.to_dict() for b in self.blocks]
        return data

    # -- derived ---------------------------------------------------------- #

    def focus_pairs(self) -> list[VocabPair]:
        """Bold target-language spans paired with the gloss that follows them."""
        pair = get_pair_or_default(self.language_pair)
        found: dict[str, VocabPair] = {}
        for block in self.blocks:
            for span in block.spans:
                if span.lang != self.target_lang or not span.target:
                    continue
                key = _focus_key(span.text, pair=pair)
                if not key:
                    continue
                if key in found:
                    found[key].count += 1
                    if found[key].en is None and span.gloss:
                        found[key].en = span.gloss
                else:
                    found[key] = VocabPair(es=span.text.strip(), en=span.gloss)
        return list(found.values())

    def stats(self) -> dict[str, Any]:
        pair = get_pair_or_default(self.language_pair)
        target_words = base_words = 0
        for block in self.blocks:
            for span in block.spans:
                if span.lang == self.target_lang:
                    target_words += pair.target.count_units(span.text)
                else:
                    base_words += pair.base.count_units(span.text)
        total = target_words + base_words
        ratio = round(target_words / total, 3) if total else 0.0
        return {
            # Legacy EN-ES keys retained for full API/UI backwards compatibility:
            "es_words": target_words,
            "en_words": base_words,
            "words": total,
            "spanish_ratio": ratio,
            # Language-neutral canonical keys:
            "target_words": target_words,
            "base_words": base_words,
            "target_ratio": ratio,
            "language_pair": self.language_pair,
            "base_lang": self.base_lang,
            "target_lang": self.target_lang,
            "paragraphs": sum(1 for b in self.blocks if b.kind == "p"),
        }

    def context_for(self, term: str) -> str | None:
        """The sentence a term appears in -- shown on the back of its card."""
        needle = _fold(term)
        for block in self.blocks:
            if block.kind != "p":
                continue
            for sentence in re.split(r"(?<=[.!?])\s+", block.plain):
                if needle in _fold(sentence):
                    return sentence.strip()
        return None


# --------------------------------------------------------------------------- #
# Body parsing
# --------------------------------------------------------------------------- #


def _strip_links(text: str) -> str:
    return _LINK_RE.sub(r"\1", text)


def _clean_gloss(text: str) -> str:
    """Strip whatever a parenthetical gloss was wrapped in.

    Sources write these several ways -- ``(*to become*)``, ``(**to become**)``,
    and ``(*to become*):`` when it is a list item, where the colon lands outside
    the parenthesis. When the wrapper is not exactly what the regex expected, the
    closing marks end up *inside* the value, and the reader shows them: ``To be
    made up of / composed of*):`` was in the vocabulary box of several lessons.
    """
    out = text.strip()
    while out and out[-1] in "):;,*":
        out = out[:-1].strip()
    while out and out[0] in "(*":
        out = out[1:].strip()
    return out


def _clean(text: str) -> str:
    text = _SUP_RE.sub("", text)
    return _strip_links(text)


def _soft_breaks(units: Sequence[str]) -> list[bool]:
    """Mark token positions where a language switch is expected to be cheap.

    True at index ``i`` when the nearest preceding non-space unit ends in
    punctuation, i.e. the switch would land at a clause boundary.
    """
    flags: list[bool] = []
    previous = ""
    for unit in units:
        flags.append(bool(previous) and bool(_SWITCH_PUNCT_RE.search(previous)))
        if not unit.isspace():
            previous = unit
    return flags


def _split_units(text: str, pair: LanguagePair) -> list[str]:
    """Split text into whitespace, placeholder, and script-coherent units.

    For same-script pairs (e.g. EN/ES, EN/FR), whitespace and placeholder
    boundaries are sufficient. For cross-script pairs (e.g. EN/ZH, EN/JA),
    script transitions inside an unspaced run (``"read中文now"``) are also
    split so each script run gets its own span.
    """
    raw_units = [u for u in _UNIT_SPLIT.split(text) if u != ""]
    if pair.base.script == pair.target.script:
        return raw_units

    from .languages.types import _script_predicate

    is_target = _script_predicate(pair.target.script)
    is_base = _script_predicate(pair.base.script)
    out: list[str] = []
    for unit in raw_units:
        if unit.isspace() or _PLACEHOLDER_RE.fullmatch(unit):
            out.append(unit)
            continue
        buf: list[str] = []
        cur_kind: str | None = None
        for ch in unit:
            kind = "t" if is_target(ch) else ("b" if is_base(ch) else "n")
            if cur_kind is not None and kind != "n" and cur_kind != "n" and kind != cur_kind:
                out.append("".join(buf))
                buf = [ch]
                cur_kind = kind
            else:
                buf.append(ch)
                if kind != "n":
                    cur_kind = kind
        if buf:
            out.append("".join(buf))
    return out


def parse_spans(raw: str, *, pair: LanguagePair | str | None = None) -> list[Span]:
    """Turn one line of diglot prose into alternating base / target spans."""
    resolved = pair if isinstance(pair, LanguagePair) else get_pair_or_default(pair)
    seg = resolved.segmenter
    base_code = resolved.base.code
    target_code = resolved.target.code
    token_re = resolved.target.word_re if resolved.base.script == resolved.target.script else None

    text = _clean(raw)

    bold_spans: list[str] = []
    gloss_spans: list[str] = []

    def _take_bold(match: re.Match[str]) -> str:
        bold_spans.append(match.group(1).strip())
        return f"\x00B{len(bold_spans) - 1}\x00"

    def _take_gloss(match: re.Match[str]) -> str:
        gloss_spans.append(_clean_gloss(match.group(1)))
        return f"\x00G{len(gloss_spans) - 1}\x00"

    text = _ITALIC_GLOSS_RE.sub(_take_gloss, text)
    text = _BOLD_RE.sub(_take_bold, text)
    text = _ITALIC_RE.sub(r"\1", text)

    units = _split_units(text, resolved)
    scores: list[float] = []
    weights: list[float] = []
    meta: list[tuple[str, int | None]] = []

    def _score_text_unit(chunk: str) -> tuple[float, float]:
        if token_re is not None:
            if any(ch in resolved.target.accent_chars for ch in chunk):
                return 2.5, 1.0
            words = token_re.findall(chunk)
            if not words:
                return 0.0, 0.0
            return sum(seg.word_score(w) for w in words) / len(words), 1.0
        s = seg.word_score(chunk)
        return (s, 1.0) if s != 0.0 else (0.0, 0.0)

    for unit in units:
        if unit.isspace():
            scores.append(0.0)
            weights.append(0.0)
            meta.append(("ws", None))
            continue
        match = _PLACEHOLDER_RE.fullmatch(unit)
        if match:
            kind, index = match.group(1), int(match.group(2))
            if kind == "B":
                inner_text = bold_spans[index]  # type: ignore[index]
                if token_re is not None:
                    inner = token_re.findall(inner_text)
                    own = sum(seg.word_score(w) for w in inner)
                    if any(ch in resolved.target.accent_chars for ch in inner_text):
                        own = max(own, 2.5)
                else:
                    own = seg.word_score(inner_text)
                scores.append(3.0 if own >= 0 else max(own, -_MAX_SPAN_EVIDENCE))
            else:
                scores.append(0.0)
            weights.append(1.0)
            meta.append((kind, index))
            continue
        score, weight = _score_text_unit(unit)
        scores.append(score)
        weights.append(weight)
        meta.append(("txt", None))

    labels = seg.decode(scores, weights, _soft_breaks(units))

    spans: list[Span] = []
    pending_break = False
    for unit, label, (kind, index) in zip(units, labels, meta):
        if kind == "ws":
            if spans:
                spans[-1].text += unit
            continue

        lang = target_code if label == 1 else base_code
        if kind == "B":
            inner = bold_spans[index]  # type: ignore[index]
            spans.append(Span(lang=lang, text=inner, bold=True, target=lang == target_code))
            continue
        if kind == "G":
            gloss = gloss_spans[index]  # type: ignore[index]
            host = next((s for s in reversed(spans) if s.lang == target_code), None)
            if host is not None and host.gloss is None:
                host.gloss = gloss
                host.text = host.text.rstrip() + " "
                pending_break = True
            else:
                spans.append(Span(lang=base_code, text=f"({gloss})"))
            continue

        if spans and spans[-1].lang == lang and not spans[-1].target and not pending_break:
            spans[-1].text += unit
        else:
            spans.append(Span(lang=lang, text=unit))
        pending_break = False

    spans = _absorb_plain_glosses(spans, pair=resolved)
    return _tidy(spans)


def _absorb_plain_glosses(spans: list[Span], *, pair: LanguagePair = EN_ES_PAIR) -> list[Span]:
    """Fold ``**phrase** (gloss)`` glosses into the phrase they follow."""
    base_code = pair.base.code
    out: list[Span] = []
    i = 0
    while i < len(spans):
        span = spans[i]
        if (
            span.lang == base_code
            and span.text.strip().startswith("(")
            and span.text.strip().endswith(")")
            and out
            and out[-1].target
            and out[-1].gloss is None
        ):
            inner = span.text.strip()[1:-1].strip()
            if not pair.segmenter.has_target_language(inner):
                out[-1].gloss = inner
                i += 1
                continue
        out.append(span)
        i += 1
    return out


def _tidy(spans: list[Span]) -> list[Span]:
    """Merge neighbours, move trailing whitespace to the next span, drop empties."""
    merged: list[Span] = []
    for span in spans:
        span.text = _PLACEHOLDER_RE.sub("", span.text)
        if not span.text.strip():
            if merged:
                merged[-1].text += span.text
            continue
        # A span carrying a gloss must not be merged into the next one: the
        # gloss belongs where the source put it, and merging would push it to
        # the end of the merged run. That is what put "(*by becoming*)" six
        # words downstream of "al volverse".
        if (merged and merged[-1].lang == span.lang and merged[-1].bold == span.bold
                and merged[-1].gloss is None):
            merged[-1].text += span.text
            if merged[-1].gloss is None:
                merged[-1].gloss = span.gloss
        else:
            merged.append(span)

    # Keep inter-span spacing sane: no leading space inside a paragraph, no
    # doubled spaces at the seams.
    for idx, span in enumerate(merged):
        if idx == 0:
            span.text = span.text.lstrip()
        span.text = re.sub(r"[ \t]{2,}", " ", span.text)
    return [s for s in merged if s.text.strip() or s.text == " "]


# --------------------------------------------------------------------------- #
# Anchors parsing
# --------------------------------------------------------------------------- #


_REFERENCE_RE = re.compile(r"^\s*(?:\[\d+\]|\[\w+\]|\d+\.\s+[A-Z][a-z]+,)|https?://\S+\s*$")


def _looks_like_reference(text: str) -> bool:
    """Bibliography and footnote lines, which are neither English prose nor
    Spanish, and read better set apart and muted than woven into a paragraph."""
    return bool(_REFERENCE_RE.search(text))


def _focus_key(term: str, *, pair: LanguagePair = EN_ES_PAIR) -> str:
    """A dedup key for a focus phrase."""
    words = [w for w in pair.target.tokenize(term) if w]
    if not words:
        return pair.target.fold(term.strip())
    longest = max(words, key=len)
    folded = pair.target.fold(longest)
    if not pair.target.word_spaced:
        return folded
    return folded[:5] if len(folded) >= 5 else folded


def _has_spanish(text: str, *, pair: LanguagePair = EN_ES_PAIR) -> bool:
    return pair.segmenter.has_target_language(text)


def _looks_like_heading_line(text: str) -> bool:
    """Detect the un-marked subheadings the source articles are full of.

    Several of these files lost their heading markup on the way into Markdown,
    leaving section titles as bare lines -- ``What Linguists Mean by
    'Language'`` sits in the middle of the prose looking exactly like a
    paragraph. A line is read as a heading when it is short, has no closing
    punctuation, and contains no Spanish; the last condition matters because a
    short Spanish sentence is content, not a title.

    Two shapes that fit the rule and are not titles are excluded, both of them
    seen in the wild: **metadata** -- "Posted April 29, 2026 | Reviewed by Lybi
    Ma", "Updated 3 October 2025" -- and anything containing a pipe, which in
    Markdown means a table row or a kicker naming the magazine and the column
    ("*The Stories We Tell* | *Loneliness*"). A lesson whose contents pane listed
    those was listing the page's furniture as if it were sections.
    """
    plain = _ITALIC_RE.sub(r"\1", _BOLD_RE.sub(r"\1", text)).strip()
    if not plain or len(plain) > 64:
        return False
    if len(_WORD_RE.findall(plain)) > 9:
        return False
    if plain[-1] in ".,;:—–-":
        return False
    if _has_spanish(plain):
        return False
    if _is_page_furniture(plain):
        return False
    return True


# A section title is not a publication line. Matched on the plain text, after the
# markup has been stripped, because that is what the title would be made of.
_FURNITURE_RE = re.compile(
    r"^(posted|published|updated|revised|reviewed|edited|written|last\s+update)\b"
    r"|\breviewed by\b|\bfact[- ]checked\b|\bmin(ute)? read\b",
    re.IGNORECASE,
)
_DATE_ONLY_RE = re.compile(
    r"^\s*(?:\d{1,2}\s+)?"
    r"(?:january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+\d{1,2},?\s+\d{4}\s*$"
    r"|^\s*\d{1,2}\s+[a-z]+\s+\d{4}\s*$"
    r"|^\s*\d{4}-\d{2}-\d{2}\s*$",
    re.IGNORECASE,
)


def _is_page_furniture(plain: str) -> bool:
    """Whether a heading-shaped line is the page's furniture rather than a title."""
    if "|" in plain:
        # A pipe means a Markdown table row, or the "magazine | column" kicker that
        # sits above an article's title. Neither is a section.
        return True
    if _FURNITURE_RE.search(plain):
        return True
    return bool(_DATE_ONLY_RE.match(plain))


def _item_body(line: str) -> tuple[bool, str] | None:
    """Split a list item into ``(is_numbered, content)``, or ``None``.

    The two list regexes expose different group counts, so unwrap them here
    once rather than at every call site.
    """
    numbered = _NUMBERED_RE.match(line)
    if numbered:
        return True, numbered.group(2)
    bullet = _BULLET_RE.match(line)
    if bullet:
        return False, bullet.group(1)
    return None


def _parse_vocab_box(lines: Sequence[str]) -> list[VocabAnchor]:
    anchors: list[VocabAnchor] = []
    current: VocabAnchor | None = None
    for line in lines:
        if not line.strip():
            continue
        item = _item_body(line)
        if item is not None:
            is_numbered, content = item
            # Numbered sub-items under a term, and quoted bullets, are usage
            # examples rather than new headwords.
            if current is not None and (is_numbered or _looks_like_example(content)):
                example = _clean(content).strip()
                if example:
                    current.examples.append(example)
                continue

            content = _clean(content).strip()
            term_part, sep, gloss_part = content.partition("(")
            raw_term = term_part.strip()
            term = _BOLD_RE.sub(r"\1", raw_term).strip()
            term = re.sub(r"\s+", " ", term).strip(" ,;:/*")
            # Stop at the *first* closing parenthesis. The gloss is parenthesised,
            # and these lines often carry prose after it -- ``- **sondear** (*to
            # probe*) — used twice: "..."`` -- which the partition above otherwise
            # swallows into the gloss.
            gloss = _clean_gloss(gloss_part.split(")", 1)[0]) if sep else None
            if term:
                current = VocabAnchor(term=term, gloss=gloss or None)
                anchors.append(current)
            continue

        # Continuation of the previous entry (wrapped prose).
        if current is not None:
            extra = _clean(line.strip())
            if not current.gloss and "(" in extra:
                current.gloss = extra.split("(", 1)[1].rstrip(")").strip()
            elif current.examples:
                current.examples[-1] += " " + extra
            else:
                current.examples.append(extra)
    return anchors


def _looks_like_example(text: str) -> bool:
    """Numbered sub-items under a term are usage examples, not new terms."""
    return text.lstrip().startswith(("“", '"', "'", "*“", "...", "…"))


def _parse_grammar(lines: Sequence[str]) -> list[GrammarNote]:
    notes: list[GrammarNote] = []
    current: GrammarNote | None = None
    for line in lines:
        if not line.strip():
            continue
        item = _item_body(line)
        if item is not None:
            is_numbered, content = item
            if is_numbered:
                title = _clean(content).strip()
                # ``1. **Future Tense for Predictions:** Several Spanish ...``
                # packs the title and the body onto one line.
                bold = _BOLD_RE.match(title)
                if bold:
                    current = GrammarNote(title=bold.group(1).rstrip(":").strip())
                    rest = title[bold.end():].strip()
                    if rest:
                        current.explanation = rest
                    notes.append(current)
                else:
                    current = GrammarNote(title=title.rstrip(":").strip())
                    notes.append(current)
                continue

            content = _clean(content).strip()
            label, sep, value = content.partition(":")
            label_clean = label.strip().strip("*").lower()
            # ``- *Example:* "..."`` puts the colon *inside* the italics, so the
            # closing ``*`` ends up at the head of the value and every imported
            # lesson's grammar notes were stored -- and shown -- with a stray
            # asterisk. Only when the label's marks are unbalanced is the marker
            # in the value; a label whose marks are closed before the colon
            # (``*Example*:``) leaves it alone.
            if sep and label.count("*") % 2 == 1 and value.lstrip().startswith("*"):
                value = value.lstrip()[1:]
            value = value.strip()
            if current is not None and sep and label_clean in ("example", "examples"):
                current.example = f"{current.example} {value}".strip() if current.example else value
            elif current is not None and sep and label_clean in (
                "explanation", "why", "note", "grammar concept", "concept", "pattern",
            ):
                current.explanation = f"{current.explanation} {value}".strip() if current.explanation else value
            elif current is not None:
                current.explanation = f"{current.explanation or ''} {content}".strip()
            continue

        if current is not None:
            current.explanation = f"{current.explanation or ''} {_clean(line.strip())}".strip()
    return notes


# --------------------------------------------------------------------------- #
# Article assembly
# --------------------------------------------------------------------------- #


def _slugify(text: str, fallback: str) -> str:
    return slugify_text(text, fallback, max_length=60)


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _drop_repeated_front_matter(blocks: list[Block], title: str | None, author: str | None) -> list[Block]:
    """Remove the title and byline the body repeats from the front matter.

    Most of these articles open with their own title as a heading, then the
    byline again. The reader renders the front-matter title and author as the
    page header, so leaving the body's copies in makes every article look like
    it stutters. Only a *leading* heading that matches the title is dropped, so
    a genuine later repeat survives.
    """
    out = list(blocks)
    if out and out[0].kind == "h" and title and _normalise(out[0].plain) == _normalise(title):
        out.pop(0)
    if out and out[0].kind == "byline" and author:
        byline = _normalise(re.sub(r"^by\s+", "", out[0].plain, flags=re.I))
        if byline and (_normalise(author).startswith(byline) or byline.startswith(_normalise(author))):
            out.pop(0)
    if out and out[0].kind == "date":
        out.pop(0)
    return out


def parse_article(
    source: str, *, fallback_slug: str, day: str | None = None,
    pair: LanguagePair | str | None = None,
) -> Article | None:
    """Parse one article section (a whole file, or one ``### Day N`` chunk)."""
    resolved_pair: LanguagePair = pair if isinstance(pair, LanguagePair) else get_pair_or_default(pair)
    lines = source.splitlines()

    title = author = url = preview = date = level = register = weave = None
    body: list[Block] = []
    vocab_lines: list[str] = []
    grammar_lines: list[str] = []

    section: Literal["front", "body", "anchors", "vocab", "grammar"] = "body"
    paragraph: list[str] = []
    in_anchors = False
    saw_vocab = False

    def flush() -> None:
        nonlocal paragraph
        if not paragraph:
            return
        raw = " ".join(paragraph).strip()
        paragraph = []
        if not raw:
            return
        if _HR_RE.match(raw):
            return
        spans = parse_spans(raw, pair=resolved_pair)
        if not spans:
            return
        body.append(Block(kind="p", spans=spans))

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()

        # Strip an outer code fence if the file was saved wrapped.
        if stripped.startswith("```"):
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            flush()
            depth, text = len(heading.group(1)), heading.group(2).strip()
            low = text.lower()
            # Substring, not prefix: the corpus also has "ADDITIONAL
            # POST-READING ANCHORS" and "FINAL POST-READING ANCHORS", and every
            # one of them is still the anchors section.
            if any(h in low for h in _ANCHOR_HEADINGS):
                in_anchors = True
                section = "anchors"
                continue
            if any(h in low for h in _FRONT_MATTER_HEADINGS):
                section = "front"
                continue
            if in_anchors:
                # ``#### 1. Recycled Vocabulary Box`` / ``#### 2. Grammar Breakdown``
                if "vocab" in low:
                    section = "vocab"
                    saw_vocab = True
                elif "grammar" in low:
                    section = "grammar"
                else:
                    section = "anchors"
                continue
            section = "body"
            if day and day.lower() in low:
                continue
            body.append(Block(kind="h", level=depth, spans=[Span(lang=resolved_pair.base.code, text=_clean(text))]))
            continue

        if _HR_RE.match(line):
            flush()
            continue

        if in_anchors:
            if any(k in stripped.lower() for k in ("recycled vocabulary", "vocabulary box")):
                section = "vocab"
                saw_vocab = True
                continue
            if "grammar breakdown" in stripped.lower():
                section = "grammar"
                continue
            if section == "grammar":
                grammar_lines.append(line)
            else:
                vocab_lines.append(line)
            continue

        if section == "front":
            field = _FIELD_RE.match(line)
            if field:
                key = field.group(1).strip().lower().rstrip(":")
                value = _clean(field.group(2)).strip()
                if key.startswith("article title") or key == "title":
                    title = _BOLD_RE.sub(r"\1", value).strip()
                elif key.startswith("author"):
                    author = value
                elif "url" in key or "link" in key:
                    url = value
                elif "preview" in key or "summary" in key:
                    preview = value
                elif "level" in key or "cefr" in key:
                    level = value
                elif "register" in key or "genre" in key or "text type" in key:
                    register = value.strip().lower()
                elif "weave" in key or "grain" in key:
                    weave = value.strip().lower()
                elif "language pair" in key or key == "languages":
                    if pair is None:
                        resolved_pair = get_pair_or_default(value.strip().lower())
                elif "date" in key:
                    date = value
                continue
            if not stripped:
                continue
            section = "body"

        if not stripped:
            flush()
            continue

        byline = _BOLD_RE.fullmatch(stripped)
        if byline and byline.group(1).lower().startswith("by "):
            flush()
            body.append(Block(kind="byline", spans=[Span(lang=resolved_pair.base.code, text=byline.group(1))]))
            continue

        if byline:
            label = byline.group(1).strip().lower().rstrip(":")
            if "recycled vocabulary" in label or "grammar breakdown" in label:
                flush()
                in_anchors = True
                if "vocab" in label:
                    section = "vocab"
                    saw_vocab = True
                else:
                    section = "grammar"
                continue

        if byline and not _has_spanish(byline.group(1), pair=resolved_pair) and not paragraph:
            flush()
            body.append(Block(kind="h", level=3, spans=[Span(lang=resolved_pair.base.code, text=_clean(byline.group(1)).strip())]))
            continue

        if stripped.startswith("*") and stripped.endswith("*") and len(stripped) > 4 and not stripped.startswith("**"):
            inner = stripped.strip("*").strip()
            if re.match(r"^[A-Z][a-z]+ \d{1,2},? \d{4}$", inner):
                flush()
                body.append(Block(kind="date", spans=[Span(lang=resolved_pair.base.code, text=inner)]))
                continue

        if not paragraph and _looks_like_heading_line(stripped):
            flush()
            body.append(Block(kind="h", level=3, spans=[Span(lang=resolved_pair.base.code, text=_clean(stripped).strip())]))
            continue

        if _looks_like_reference(stripped):
            flush()
            body.append(Block(kind="ref", spans=[Span(lang=resolved_pair.base.code, text=_clean(stripped).strip())]))
            continue

        paragraph.append(stripped)

    flush()

    if not body:
        return None

    if title is None:
        for block in body:
            if block.kind == "h":
                title = block.plain
                break
    if title is None:
        return None

    if section == "grammar" and not grammar_lines and vocab_lines and not saw_vocab:
        grammar_lines, vocab_lines = vocab_lines, []

    vocab = _parse_vocab_box(vocab_lines)
    grammar = _parse_grammar(grammar_lines)

    body = _drop_repeated_front_matter(body, title, author)

    return Article(
        slug=_slugify(title, fallback_slug),
        title=title,
        author=author,
        url=url,
        preview=preview,
        date=date,
        day=day,
        blocks=body,
        vocab=vocab,
        grammar=grammar,
        source=fallback_slug,
        level=level,
        register=register,
        weave=weave,
        language_pair=resolved_pair.id,
        base_lang=resolved_pair.base.code,
        target_lang=resolved_pair.target.code,
    )


_DAY_RE = re.compile(r"^#{1,6}\s+(Day\s+\d+)\s*$", re.IGNORECASE | re.MULTILINE)


def article_chunks(text: str) -> list[tuple[str | None, str]]:
    """Split a file's text into one chunk per lesson, as ``(day, text)`` pairs.

    Most files hold a single lesson and come back whole, with ``None`` for the
    day. A compilation like ``diglot.md`` separates its lessons with ``### Day N``
    headings and each chunk then carries its own front matter.

    Exposed rather than kept inside :func:`parse_file` because a caller sometimes
    needs one lesson's *text* and not just its parse -- exporting a single lesson
    out of a compilation has to hand on the author's own words, and regenerating
    them from the parsed model cannot be faithful to text that contains markup.
    """
    marks = list(_DAY_RE.finditer(text))
    if len(marks) < 2:
        return [(None, text)]
    out: list[tuple[str | None, str]] = []
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        out.append((mark.group(1), text[mark.end():end]))
    return out


def parse_file(path: Path) -> list[Article]:
    """Parse a diglot Markdown file into one or more articles.

    Some files are single essays; ``diglot.md`` is a compilation with
    ``### Day N`` separators, so it splits into one article per day.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    stem = path.stem
    # ``foo-2026-09-05T11-41-35.md.md`` -> ``foo``
    stem = re.sub(r"\.md$", "", stem)
    stem = re.sub(r"[-_]?\d{4}-\d{2}-\d{2}T[\d-]+$", "", stem).strip("-")

    chunks = article_chunks(text)
    if chunks[0][0] is not None:
        articles: list[Article] = []
        for day, chunk in chunks:
            label = (day or "Day").title()
            article = parse_article(chunk, fallback_slug=f"{stem}-{_slugify(label, 'day')}", day=label)
            if article:
                article.slug = f"{stem}-{article.slug}"
                articles.append(article)
        if articles:
            return articles

    article = parse_article(text, fallback_slug=stem)
    if article is None:
        return []
    # The filename is the article's identity, not its title. Two imported
    # articles can share a title -- and did, which produced a slug the API
    # could not resolve, because the file was written under a name that
    # included a hash suffix the title-derived slug did not have.
    article.slug = stem
    return [article]


def parse_corpus(directory: Path) -> list[Article]:
    """Parse every ``*.md`` in a directory, newest-first ordering applied later."""
    articles: list[Article] = []
    for path in sorted(directory.glob("*.md")):
        if path.name.lower().startswith("readme"):
            continue
        try:
            articles.extend(parse_file(path))
        except Exception as exc:  # a malformed file must not sink the library
            log.warning("failed to parse %s: %s: %s", path.name, type(exc).__name__, exc)
    return articles


def content_fingerprint(article: Article) -> str:
    """Stable hash of an article's text, used to invalidate cached AI output."""
    digest = hashlib.sha256()
    digest.update(article.title.encode("utf-8"))
    for block in article.blocks:
        digest.update(block.plain.encode("utf-8"))
    return digest.hexdigest()[:16]


if __name__ == "__main__":  # pragma: no cover - manual inspection helper
    import sys

    # No app imports here by design, so the default is relative to this file.
    fallback = Path(__file__).resolve().parent.parent / "corpus"
    target = Path(sys.argv[1] if len(sys.argv) > 1 else fallback)
    for art in parse_corpus(target):
        s = art.stats()
        print(f"{art.slug}")
        print(f"    title   : {art.title}")
        print(f"    author  : {art.author}")
        print(f"    day     : {art.day}   blocks: {len(art.blocks)}")
        print(f"    spanish : {s['spanish_ratio']:.0%} of {s['words']} words")
        print(f"    vocab   : {len(art.focus_pairs())} focus pairs, {len(art.vocab)} anchors")
        print(f"    grammar : {len(art.grammar)} notes")
