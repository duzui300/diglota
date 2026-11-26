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

from collections import Counter
from typing import Iterable

from .diglot import Article
from .languages import (
    DEFAULT_PAIR,
    LanguagePair,
    default_content_word,
    default_head_word,
    fold_text,
    get_pair_or_default,
    spanish_stem,
)

# Words too common to be worth counting as "vocabulary": auto-folded in
# ``app.languages.en_es`` so accented words like ``había`` and ``están`` match.
_FUNCTION_WORDS = DEFAULT_PAIR.target.folded_function_words

# Shortest stem worth comparing. Three, not four: stripping the ending from
# "lugar" leaves "lug" and from "quedar" leaves "quod", and both still
# distinguish the words they came from. The guard against noise is on the
# *source* word instead -- see `known_stems`.
MIN_STEM = DEFAULT_PAIR.target.min_stem_length
MIN_SOURCE = DEFAULT_PAIR.target.min_source_length


def fold(text: str) -> str:
    """Lowercase and strip accents, so ``atención`` matches ``atencion``."""
    return fold_text(text)


def content_word(term: str, *, pair: LanguagePair | None = None) -> str:
    """The word in a phrase that carries the meaning."""
    if pair is not None:
        return pair.target.content_word(term)
    return default_content_word(term)


def stem(word: str, *, pair: LanguagePair | None = None) -> str:
    """Target-language stemmer (Spanish by default)."""
    if pair is not None:
        return pair.target.stem(word)
    return spanish_stem(word)


def head_word(term: str, *, pair: LanguagePair | None = None) -> str:
    """The first word of a possibly multi-word entry."""
    if pair is not None:
        return pair.target.head_word(term)
    return default_head_word(term)


def known_stems(deck: Iterable[dict], *, pair: LanguagePair | None = None) -> set[str]:
    """Every stem the deck can recognise, from both the term and its lemma."""
    resolved = pair or DEFAULT_PAIR
    target = resolved.target
    out: set[str] = set()
    for word in deck:
        for field in ("term", "lemma"):
            value = (word.get(field) or "").strip()
            if not value:
                continue
            pieces = {value, target.head_word(value), target.content_word(value)}
            for piece in pieces:
                if len(target.fold(piece)) < target.min_source_length:
                    continue
                candidate = target.stem(piece)
                if len(candidate) >= target.min_stem_length:
                    out.add(candidate)
    return out


def article_vocabulary(article: Article, *, pair: LanguagePair | None = None) -> Counter[str]:
    """Content words in the article's target language, with their frequencies."""
    resolved = pair or get_pair_or_default(getattr(article, "language_pair", None))
    target = resolved.target
    target_code = getattr(article, "target_lang", target.code)
    counts: Counter[str] = Counter()
    for block in article.blocks:
        if block.kind not in ("p", "h"):
            continue
        for span in block.spans:
            if span.lang != target_code:
                continue
            for word in target.tokenize(span.text):
                folded = target.fold(word)
                if len(folded) < target.min_word_length or folded in target.folded_function_words:
                    continue
                counts[folded] += 1
    return counts


def text_coverage(article: Article, deck: Iterable[dict], *, pair: LanguagePair | None = None) -> dict:
    """What share of the article's target-language content words the deck covers."""
    resolved = pair or get_pair_or_default(getattr(article, "language_pair", None))
    vocabulary = article_vocabulary(article, pair=resolved)
    if not vocabulary:
        return {"token_ratio": 1.0, "type_ratio": 1.0, "tokens": 0, "types": 0,
                "known_types": 0, "unknown": []}

    stems = known_stems(deck, pair=resolved)
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
        if resolved.target.stem(word) in stems:
            known_tokens += count
            known_types += 1
        else:
            unknown.append((word, count))

    total = sum(vocabulary.values())
    unknown.sort(key=lambda item: -item[1])
    return {
        "token_ratio": round(known_tokens / total, 3),
        "type_ratio": round(known_types / len(vocabulary), 3),
        "tokens": total,
        "types": len(vocabulary),
        "known_types": known_types,
        "unknown": [word for word, _ in unknown[:14]],
    }


def verdict(token_ratio: float, *, pair: LanguagePair | None = None) -> tuple[str, str]:
    """A plain-language reading of the coverage number."""
    resolved = pair or DEFAULT_PAIR
    t_name = resolved.target.name
    b_name = resolved.base.name
    if token_ratio >= 0.97:
        return "comfortable", f"You know almost every {t_name} word here. Read it for fluency."
    if token_ratio >= 0.90:
        return "stretch", "A few new words per paragraph. This is the range where reading teaches most."
    if token_ratio >= 0.75:
        return "demanding", "Dense with new words. Expect to look things up."
    return "immersion", f"Most of this {t_name} is new. Lean on the {b_name} and enjoy the shape of it."
