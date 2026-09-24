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
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence

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
# Language identification
# --------------------------------------------------------------------------- #

# Content words matter as much as function words: a clause built entirely of
# vocabulary the lexicon has never seen decodes as a coin flip. This list is
# tuned to the corpus (AI, art, language, philosophy) rather than to Spanish at
# large, which is why it carries ``lenguaje`` and ``modelo`` but not ``cocina``.
_ES_WORDS = frozenset(
    """
    de la el los las un una unos unas que y e o u en por para con sin sobre entre
    hasta desde segun según durante mediante se le les lo su sus mi tu es son era
    eran fue fueron ser estar está están estaba estaban estan han ha he hay había
    habia puede pueden podía podia tiene tienen tenía tenia hacer hace hacen más
    mas menos muy también tambien pero sino aunque porque pues como cuando donde
    quien quienes cual cuales este esta esto estos estas ese esa eso esos esas
    aquel aquella al del ni ya aún aun todavía todavia siempre nunca ahora antes
    después despues luego aquí aqui allí alli así asi tan tanto todo toda todos
    todas otro otra otros otras mismo misma cada algún algun alguna ningún ninguna
    nada nadie algo alguien tal tales pues sea sean fuera fuese siendo sido esta
    éste ésta aquello cuyo cuya cuyos cuyas ambos ambas cualquiera quienes quiera
    debía debia podría podria debería deberia había habia siendo hemos habéis
    nuestros nuestras vuestro vuestra ellos ellas nosotros vosotros usted ustedes
    te os nos me les consiste resultan resulta existen existe hace hacía hacia
    arte artista artistas obra obras mundo humano humanos humanidad vida tiempo
    años año siglo siglos historia cultura lenguaje lengua lenguas idioma palabra
    palabras texto textos ideas idea mente cerebro pensamiento conocimiento
    pregunta respuestas forma formas manera modos modo parte partes cosa cosas
    caso casos ejemplo ejemplos cambio cambios proceso procesos sistema sistemas
    modelo modelos datos información tecnologia tecnología maquina máquina
    máquinas herramientas herramienta trabajo trabajos desarrollo crecimiento
    futuro pasado presente real realidad verdad sentido significado valor valores
    poder fuerza razón razon razones efecto efectos causa causas resultado
    resultados problema problemas solución solucion pregunta tema temas punto
    puntos lugar lugares grupo grupos sociedad social persona personas gente
    nombre nombres número numero parte nuevo nueva viejos viejo grandes grande
    mejor peor mayor menor primero primer último ultimo mismo propio propia
    posible imposible importante diferentes diferente distintos distinto general
    común comun simple complejo difícil dificil fácil facil claro oscuro
    entender comprender aprender aprender enseñar mostrar decir dice dicen habla
    hablar escribir escribe lee leer pensar piensa creer cree crear crea crear
    producir generar cambiar cambiar seguir sigue lograr conseguir permitir
    parecer parece queda quedan tener tiene deben debe puede debemos podemos
    quieren quiere hacer hacen ver vemos da dan van vamos solo sólo además ademas
    incluso entonces mientras tanto sin embargo aunque quizá quiza tal vez
    """.split()
)

# Words that are strong evidence of English.
_EN_WORDS = frozenset(
    """
    the a an and or but if of to in on at by for with from as is are was were be
    been being am has have had do does did will would can could shall should may
    might must this that these those it its he she they them their his her we us
    our you your i me my not no so than then there here when where which who whom
    whose what how why all any some more most other such only also very just into
    over after before between during about up down out off again once because
    while both each few many much own same too s t don now
    art artist artists work works world human humans life time year years century
    history culture language languages word words text texts idea ideas mind brain
    thought knowledge question questions form forms way ways part parts thing
    things case cases example examples change changes process processes system
    systems model models data information technology machine machines tool tools
    future past present real reality truth meaning value values power reason
    reasons effect effects cause causes result results problem problems solution
    theme point points place places group groups society social person people
    name names number new old great large big better worse greater less first last
    own possible impossible important different general common simple complex
    difficult easy clear dark understand learn teach show say says said speak talk
    write writes read think thinks believe believes create creates produce
    generate change follow continue achieve get allow seem seems remain stay
    become becomes need needs want wants see seen make makes made give gives
    take takes come comes go goes know knows find finds look looks feel feels
    still even though however perhaps maybe almost often always never sometimes
    """.split()
)

# Tokens that are genuinely both (``no``, ``a``, ``me``, ``he``, ``son`` ...).
# They carry no signal either way and are resolved by their neighbours.
_AMBIGUOUS = frozenset("no a me he son as has sea da la di mi si an".split())

# A word claimed by both lexicons is by definition no evidence either way --
# ``real``, ``general`` and ``simple`` are spelled the same in both languages.
# Derived rather than hand-maintained so the two lists can grow independently.
_AMBIGUOUS = _AMBIGUOUS | (_ES_WORDS & _EN_WORDS)

_ACCENTS = set("áéíóúüñ¿¡ÁÉÍÓÚÜÑ")

# Spanish orthography is generous with these endings, and English is not.
_ES_SUFFIXES = ("ción", "ciones", "dad", "dades", "mente", "ísimo", "ísima", "aban", "iesen")
_EN_SUFFIXES = ("ing", "tion", "sion", "ough")

_WORD_RE = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ¿¡]+")


def _fold(word: str) -> str:
    """Lowercase and strip accents, for dictionary lookup only."""
    decomposed = unicodedata.normalize("NFD", word.lower())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def word_score(word: str) -> float:
    """Positive leans Spanish, negative leans English, zero is undecided.

    Accents are treated as decisive: no English word in this corpus carries
    one, and they survive in ``más``, ``está``, ``según`` -- exactly the words
    that anchor a Spanish run.
    """
    if not word:
        return 0.0
    if any(ch in _ACCENTS for ch in word):
        return 2.5
    folded = _fold(word)
    if not folded:
        return 0.0
    if folded in _AMBIGUOUS:
        return 0.0
    if folded in _ES_WORDS:
        return 1.2
    if folded in _EN_WORDS:
        return -1.2
    if len(folded) > 4 and folded.endswith(_ES_SUFFIXES):
        return 0.9
    if len(folded) > 5 and folded.endswith(_EN_SUFFIXES):
        return -0.5
    return 0.0


# Switching languages mid-phrase costs something, which is what keeps a lone
# foreign loanword inside a sentence from splitting it in two.
_SWITCH_PENALTY = 1.8

# ...but a switch that lands right after punctuation is the normal case in this
# format -- the weave turns over at sentence and clause boundaries -- so it is
# discounted. Without this the decoder is genuinely indifferent about *where*
# inside a run of neutral words it crosses over, and picks the boundary
# arbitrarily: "the future of art can | seem rather grim. | Afortunadamente"
# comes out with the English clause marked Spanish.
_PUNCTUATED_SWITCH = 0.40
_BREAK_CHARS = ".!?;:,…\"”’)]—–"

# Ceiling on how much a single bolded span may argue for its own language, so a
# long English-looking section title cannot outvote the paragraph around it.
_MAX_SPAN_EVIDENCE = 3.5

_SWITCH_PUNCT_RE = re.compile(rf"[{re.escape(_BREAK_CHARS)}]\s*$")


def segment(
    scores: Sequence[float],
    weights: Sequence[float] | None = None,
    soft_break: Sequence[bool] | None = None,
) -> list[int]:
    """Viterbi decode a per-token language score into 0=English / 1=Spanish labels.

    Two-state HMM with no state priors: emissions are the per-token scores
    above, and changing language costs :data:`_SWITCH_PENALTY` (discounted by
    :data:`_PUNCTUATED_SWITCH` where ``soft_break`` marks a clause boundary).
    Decoding globally rather than thresholding per token is what lets short
    Spanish islands inside English sentences survive -- a lone ``**podemos
    esperar que**`` is outvoted word by word but wins as a path.

    Costs, not scores: a positive token score is evidence *for* Spanish, so it
    lowers the cost of state 1 and raises the cost of state 0.
    """
    if not scores:
        return []
    weights = list(weights) if weights is not None else [1.0] * len(scores)
    soft_break = list(soft_break) if soft_break is not None else [False] * len(scores)

    emit = [s * w for s, w in zip(scores, weights)]
    penalty = [
        0.0,
        *(_SWITCH_PENALTY * _PUNCTUATED_SWITCH if soft else _SWITCH_PENALTY for soft in soft_break[1:]),
    ]

    # Cheapest way to end at each state. Backpointers per token are stored as
    # (came_from_for_en, came_from_for_es).
    cost = [emit[0], -emit[0]]
    back: list[tuple[int, int]] = [(0, 0)]

    for i in range(1, len(emit)):
        value = emit[i]
        switch = penalty[i]
        stay_en, from_es = cost[0], cost[1] + switch
        stay_es, from_en = cost[1], cost[0] + switch
        back.append((0 if stay_en <= from_es else 1, 1 if stay_es <= from_en else 0))
        cost = [min(stay_en, from_es) + value, min(stay_es, from_en) - value]

    state = 0 if cost[0] <= cost[1] else 1
    labels = [state]
    for i in range(len(emit) - 1, 0, -1):
        state = back[i][state]
        labels.append(state)
    labels.reverse()
    return labels


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class Span:
    """A run of text in one language, with optional gloss and emphasis."""

    lang: Literal["en", "es"]
    text: str
    gloss: str | None = None
    bold: bool = False
    # True when the span was bolded in the source, i.e. it is a focus word of
    # the lesson rather than incidental Spanish.
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
    """One paragraph, heading or byline."""

    kind: Literal["p", "h", "byline", "date", "quote"]
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
        """Bold Spanish spans paired with the gloss that follows them.

        This is the article's *implicit* syllabus: the phrases the author chose
        to bold are the ones the lesson is teaching, and most of them are
        glossed in place. Harvesting them here means the vocabulary deck seeds
        itself from the text instead of needing a separate word list.
        """
        found: dict[str, VocabPair] = {}
        for block in self.blocks:
            for span in block.spans:
                if span.lang != "es" or not span.target:
                    continue
                key = _focus_key(span.text)
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
        es_words = en_words = 0
        for block in self.blocks:
            for span in block.spans:
                n = len(_WORD_RE.findall(span.text))
                if span.lang == "es":
                    es_words += n
                else:
                    en_words += n
        total = es_words + en_words
        return {
            "es_words": es_words,
            "en_words": en_words,
            "words": total,
            "spanish_ratio": round(es_words / total, 3) if total else 0.0,
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


def parse_spans(raw: str) -> list[Span]:
    """Turn one line of diglot prose into alternating English / Spanish spans."""
    text = _clean(raw)

    # Pull the two kinds of markup out into placeholder tokens so the
    # segmenter sees a flat word stream. Bold spans become ``\\x00B{i}\\x00``
    # and italic glosses ``\\x00G{i}\\x00``; both are opaque single tokens.
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
    # Remaining italics are emphasis on ordinary text (English titles, the
    # ``*the happy wombat*`` style quotation); keep the words, drop the marks.
    text = _ITALIC_RE.sub(r"\1", text)

    # Split into alternating text / whitespace / placeholder units.
    #
    # A placeholder has to be a unit of its own, and it is a split boundary here
    # rather than merely a token, because everything downstream recognises it
    # with a full match. ``**será**,`` -- a bold word followed straight by a comma
    # -- used to arrive as one unit, fail that match, and be appended as ordinary
    # text; ``_tidy`` then strips the marker and takes the word with it. 65 spans
    # in this corpus were being deleted from the reading text that way, silently,
    # which is the worst possible shape for this bug: a hole in a sentence reads
    # as nothing at all.
    units = [u for u in _UNIT_SPLIT.split(text) if u != ""]
    scores: list[float] = []
    weights: list[float] = []
    meta: list[tuple[str, int | None]] = []  # (kind, placeholder index)

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
                # A bolded span is usually a focus phrase -- the author bolded
                # it *because* it is the Spanish being taught -- so it votes
                # Spanish hard. But some articles bold English run-in labels
                # too (``- **In-context learning:** Durante el ...``), and those
                # must not drag the label into the Spanish run. So the span is
                # scored on its own words first: total, not average, because a
                # three-word English label has three words' worth of evidence,
                # and clamped so a long bold title cannot outvote a paragraph.
                inner = _WORD_RE.findall(bold_spans[index])  # type: ignore[index]
                own = sum(word_score(w) for w in inner)
                scores.append(3.0 if own >= 0 else max(own, -_MAX_SPAN_EVIDENCE))
            else:
                scores.append(0.0)
            weights.append(1.0)
            meta.append((kind, index))
            continue
        words = _WORD_RE.findall(unit)
        if not words:
            scores.append(0.0)
            weights.append(0.0)
            meta.append(("txt", None))
        else:
            # Average over the words in the unit so a long word does not
            # outvote a short one purely by existing.
            scores.append(sum(word_score(w) for w in words) / len(words))
            weights.append(1.0)
            meta.append(("txt", None))

    labels = segment(scores, weights, _soft_breaks(units))

    # Reassemble into spans, splitting on language change.
    spans: list[Span] = []
    pending_break = False
    for unit, label, (kind, index) in zip(units, labels, meta):
        if kind == "ws":
            if spans:
                spans[-1].text += unit
            continue

        lang: Literal["en", "es"] = "es" if label == 1 else "en"
        if kind == "B":
            inner = bold_spans[index]  # type: ignore[index]
            # Bold survives either way -- it is the source's emphasis. Only
            # Spanish bold counts as a focus word for the vocabulary deck.
            spans.append(Span(lang=lang, text=inner, bold=True, target=lang == "es"))
            continue
        if kind == "G":
            gloss = gloss_spans[index]  # type: ignore[index]
            # Attach the gloss to the Spanish run it annotates, and end that run
            # there. Without the break the rest of the sentence keeps merging
            # into the same span and the gloss drifts to the end of it -- which
            # is how "al volverse (*by becoming*) algo peor" ended up glossed
            # after "peor".
            host = next((s for s in reversed(spans) if s.lang == "es"), None)
            if host is not None and host.gloss is None:
                host.gloss = gloss
                host.text = host.text.rstrip() + " "
                pending_break = True
            else:
                spans.append(Span(lang="en", text=f"({gloss})"))
            continue

        if spans and spans[-1].lang == lang and not spans[-1].target and not pending_break:
            spans[-1].text += unit
        else:
            spans.append(Span(lang=lang, text=unit))
        pending_break = False

    spans = _absorb_plain_glosses(spans)
    return _tidy(spans)


def _absorb_plain_glosses(spans: list[Span]) -> list[Span]:
    """Fold ``**phrase** (english)`` glosses into the phrase they follow.

    Some articles write the gloss without italics -- ``**funcionan según**
    (operate on)``. A parenthetical is only treated as a gloss when it sits
    immediately after a bolded Spanish span and contains no Spanish of its own,
    which is narrow enough not to swallow ordinary English asides.
    """
    out: list[Span] = []
    i = 0
    while i < len(spans):
        span = spans[i]
        if (
            span.lang == "en"
            and span.text.strip().startswith("(")
            and span.text.strip().endswith(")")
            and out
            and out[-1].target
            and out[-1].gloss is None
        ):
            inner = span.text.strip()[1:-1].strip()
            if not any(word_score(w) > 0 for w in _WORD_RE.findall(inner)):
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


def _focus_key(term: str) -> str:
    """A dedup key for a focus phrase.

    Imported lessons pick their own vocabulary, and a model asked for six items
    will happily return ``relacionado con``, ``relacionada con``,
    ``relacionadas con`` and ``estar relacionado con`` as four of them. Keying
    on the first five letters of the *longest* word collapses those to one
    without merging genuinely different entries -- ``vuelva`` and ``volverá``
    still differ, and ``el sueño`` and ``del sueño`` collapse.
    """
    words = [w for w in _WORD_RE.findall(term) if w]
    if not words:
        return term.strip().lower()
    longest = max(words, key=len)
    folded = "".join(
        ch for ch in unicodedata.normalize("NFD", longest.lower())
        if unicodedata.category(ch) != "Mn"
    )
    return folded[:5] if len(folded) >= 5 else folded


def _has_spanish(text: str) -> bool:
    if any(ch in _ACCENTS for ch in text):
        return True
    return any(word_score(w) > 0 for w in _WORD_RE.findall(text))


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
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or fallback


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


def parse_article(source: str, *, fallback_slug: str, day: str | None = None) -> Article | None:
    """Parse one article section (a whole file, or one ``### Day N`` chunk)."""
    lines = source.splitlines()

    title = author = url = preview = date = level = register = weave = None
    body: list[Block] = []
    vocab_lines: list[str] = []
    grammar_lines: list[str] = []

    section: Literal["front", "body", "anchors", "vocab", "grammar"] = "body"
    paragraph: list[str] = []
    in_anchors = False

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
        spans = parse_spans(raw)
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
                elif "grammar" in low:
                    section = "grammar"
                else:
                    section = "anchors"
                continue
            section = "body"
            if day and day.lower() in low:
                continue
            body.append(Block(kind="h", level=depth, spans=[Span(lang="en", text=_clean(text))]))
            continue

        if _HR_RE.match(line):
            flush()
            continue

        if in_anchors:
            if any(k in stripped.lower() for k in ("recycled vocabulary", "vocabulary box")):
                section = "vocab"
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
                elif "date" in key:
                    date = value
                continue
            if not stripped:
                continue
            # The first non-field line ends the front matter, so fall through
            # and treat it as the start of the article body. Not every file has
            # a title heading -- some open straight into prose, and some use a
            # bare bold line for the title.
            section = "body"

        if not stripped:
            flush()
            continue

        # ``**By Author**`` on its own line.
        byline = _BOLD_RE.fullmatch(stripped)
        if byline and byline.group(1).lower().startswith("by "):
            flush()
            body.append(Block(kind="byline", spans=[Span(lang="en", text=byline.group(1))]))
            continue

        # Some articles reach the anchors without a heading, going straight to a
        # bold ``**Recycled Vocabulary Box**``. Without this the whole
        # post-reading section is read as more body prose.
        if byline:
            label = byline.group(1).strip().lower().rstrip(":")
            if "recycled vocabulary" in label or "grammar breakdown" in label:
                flush()
                in_anchors = True
                section = "vocab" if "vocab" in label else "grammar"
                continue

        if byline and not _has_spanish(byline.group(1)) and not paragraph:
            flush()
            body.append(Block(kind="h", level=3, spans=[Span(lang="en", text=_clean(byline.group(1)).strip())]))
            continue

        if stripped.startswith("*") and stripped.endswith("*") and len(stripped) > 4 and not stripped.startswith("**"):
            inner = stripped.strip("*").strip()
            if re.match(r"^[A-Z][a-z]+ \d{1,2},? \d{4}$", inner):
                flush()
                body.append(Block(kind="date", spans=[Span(lang="en", text=inner)]))
                continue

        if not paragraph and _looks_like_heading_line(stripped):
            flush()
            body.append(Block(kind="h", level=3, spans=[Span(lang="en", text=_clean(stripped).strip())]))
            continue

        if _looks_like_reference(stripped):
            flush()
            body.append(Block(kind="ref", spans=[Span(lang="en", text=_clean(stripped).strip())]))
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

    if section == "grammar" and not grammar_lines and vocab_lines:
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
            print(f"[diglot] failed to parse {path.name}: {type(exc).__name__}: {exc}")
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
