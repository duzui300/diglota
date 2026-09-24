"""Vocabulary measurement: how much of a text does the learner already know?

The number this module produces is the one that decides whether a text is worth
reading. Comprehension research puts the threshold for unassisted reading
somewhere around 95-98% of running words known; below that, a reader spends
their attention on decoding instead of on meaning. For a *diglot* the bar is
lower -- the English carries the sense -- but the same number tells you whether
the Spanish in a given article is a stretch or a wall.

Two things make this harder than a set intersection:

*   **Inflection.** The deck stores ``volverse``; the article says ``se vuelve``
    and ``me vuelvo``. Matching has to see through that, including the
    stem-changing verbs (o→ue, e→ie) that break naive prefix matching.
*   **Multi-word entries.** A saved ``se ponen de acuerdo`` is real vocabulary,
    but only the head word can be matched against a single token.

So matching is deliberately approximate, and the result is presented as an
estimate. Overcounting slightly is the right error to make: telling a learner
they know 90% of a text when they know 87% costs nothing, whereas telling them
they know 60% when they know 90% would stop them reading something they would
have enjoyed.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Iterable

from .diglot import Article, _WORD_RE

# Words too common to be worth counting as "vocabulary": a learner meets these
# in the first weeks, and left in they would dominate both the coverage figure
# and the "words standing in your way" list, which is meant to be studyable.
_FUNCTION_WORDS = frozenset(
    """de la el los las un una unos unas que y e o u en por para con sin sobre entre
    al del a se le les lo su sus mi tu es son era eran fue fueron ser estar está
    están estaba han ha he hay había ni no sí si más menos muy ya tan todo toda
    todos todas te os nos me mas esta este esto estos estas ese esa eso esos esas
    como será sera puede pueden podía tener tiene tienen hace hacen hacer todo
    pero aunque sino también tambien siempre nunca ahora antes después despues
    luego aquí aqui allí alli así asi cada desde hasta cuando donde quien quienes
    cual porque pues solo sólo mismo misma otro otra otros otras nada algo alguien
    nadie bien mal sea sean fuera fuese siendo sido cuál quien aún aun todavía
    todavia casi menos tanto tanta tantos tantas""".split()
)

_REFLEXIVE = re.compile(r"(?:se|me|te|nos|os|lo|la|le|los|las|les)$")
_ENDING = re.compile(r"(?:ando|iendo|ado|ido|ar|er|ir|es|os|as|o|a|e|s)$")

# Shortest stem worth comparing. Three, not four: stripping the ending from
# "lugar" leaves "lug" and from "quedar" leaves "quod", and both still
# distinguish the words they came from. The guard against noise is on the
# *source* word instead -- see `known_stems`.
MIN_STEM = 3
MIN_SOURCE = 4


def fold(text: str) -> str:
    """Lowercase and strip accents, so ``atención`` matches ``atencion``."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def content_word(term: str) -> str:
    """The word in a phrase that carries the meaning.

    A saved entry can be a whole construction -- ``se ponen de acuerdo``,
    ``dar lugar a`` -- but only one of its words will ever appear as a token in
    the article. The longest word is a good enough guess at which: it picks
    ``acuerdo``, ``lugar``, ``remonta`` and ``obligó`` out of the examples
    above, which is what matching needs.
    """
    pieces = [p for p in re.split(r"[\s/,(]+", term.strip()) if p]
    if not pieces:
        return term.strip()
    if len(pieces) == 1:
        return pieces[0]
    return max(pieces, key=len)


def stem(word: str) -> str:
    """A crude Spanish stem, good enough to match inflections of one word.

    Handles the two things that actually matter here: the reflexive clitic that
    attaches in ``volverse`` but stands alone in ``se vuelve``, and the
    stem-changing diphthongs (``vuelve`` / ``volverse``, ``piensa`` /
    ``pensar``) that would otherwise make a prefix match fail on exactly the
    verbs a learner most needs credit for.
    """
    text = fold(content_word(word))
    if len(text) > 6:
        text = _REFLEXIVE.sub("", text)
    text = text.replace("ue", "o").replace("ie", "e")
    trimmed = _ENDING.sub("", text)
    # Falling back to the untrimmed form keeps inflections of the same word
    # together: "quedar" and "quedo" both trim to "quod", so they must both
    # take the same branch here or they would stop matching each other.
    return (trimmed if len(trimmed) >= MIN_STEM else text)[:6]


def head_word(term: str) -> str:
    """The first word of a possibly multi-word entry."""
    return re.split(r"[\s/,(]", term.strip(), maxsplit=1)[0]


def known_stems(deck: Iterable[dict]) -> set[str]:
    """Every stem the deck can recognise, from both the term and its lemma.

    Also indexes each word of a multi-word entry, so ``dar lugar a`` is
    recognised when the article says ``lugar``.
    """
    out: set[str] = set()
    for word in deck:
        for field in ("term", "lemma"):
            value = (word.get(field) or "").strip()
            if not value:
                continue
            pieces = {value, head_word(value), content_word(value)}
            for piece in pieces:
                if len(fold(piece)) < MIN_SOURCE:
                    continue
                candidate = stem(piece)
                if len(candidate) >= MIN_STEM:
                    out.add(candidate)
    return out


def article_vocabulary(article: Article) -> Counter[str]:
    """Content words in the article's Spanish, with their frequencies."""
    counts: Counter[str] = Counter()
    for block in article.blocks:
        if block.kind not in ("p", "h"):
            continue
        for span in block.spans:
            if span.lang != "es":
                continue
            for word in _WORD_RE.findall(span.text):
                folded = fold(word)
                if len(folded) < 3 or folded in _FUNCTION_WORDS:
                    continue
                counts[folded] += 1
    return counts


def text_coverage(article: Article, deck: Iterable[dict]) -> dict:
    """What share of the article's Spanish content words the deck covers.

    Reported two ways, because they answer different questions. ``token_ratio``
    is the running-word figure -- the one that predicts how hard the reading
    will feel. ``type_ratio`` is the share of *distinct* words known, which is
    lower and moves faster, so it is the better measure of progress.
    """
    vocabulary = article_vocabulary(article)
    if not vocabulary:
        return {"token_ratio": 1.0, "type_ratio": 1.0, "tokens": 0, "types": 0,
                "known_types": 0, "unknown": []}

    stems = known_stems(deck)
    if not stems:
        return {
            "token_ratio": 0.0, "type_ratio": 0.0,
            "tokens": sum(vocabulary.values()), "types": len(vocabulary),
            "known_types": 0,
            "unknown": [word for word, _ in vocabulary.most_common(12)],
        }

    known_tokens = 0
    known_types = 0
    unknown: list[tuple[str, int]] = []
    for word, count in vocabulary.items():
        if stem(word) in stems:
            known_tokens += count
            known_types += 1
        else:
            unknown.append((word, count))

    total = sum(vocabulary.values())
    unknown.sort(key=lambda pair: -pair[1])
    return {
        "token_ratio": round(known_tokens / total, 3),
        "type_ratio": round(known_types / len(vocabulary), 3),
        "tokens": total,
        "types": len(vocabulary),
        "known_types": known_types,
        # The words standing between the learner and a comfortable read, most
        # frequent first: the most useful thing to study before starting.
        "unknown": [word for word, _ in unknown[:14]],
    }


def verdict(token_ratio: float) -> tuple[str, str]:
    """A plain-language reading of the coverage number."""
    if token_ratio >= 0.97:
        return "comfortable", "You know almost every Spanish word here — read it for fluency."
    if token_ratio >= 0.90:
        return "stretch", "A few new words per paragraph. This is the range where reading teaches most."
    if token_ratio >= 0.75:
        return "demanding", "Dense with new words — expect to look things up."
    return "immersion", "Most of this Spanish is new. Lean on the English and enjoy the shape of it."
