"""Language and language-pair abstractions for Diglota.

Everything language-specific in the domain layer -- script properties,
tokenization, accent folding, stemming, stopwords, diglot segmentation,
proficiency scales, shelves, TTS hints, and prompt context -- flows through
:class:`LanguageProfile` and :class:`LanguagePair`.

Adding a new language pair (for example ``en-zh``, ``zh-en``, ``en-fr``, or
``en-ja``) means defining its base/target :class:`LanguageProfile` objects and
choosing a :class:`DiglotSegmenter` (:class:`SameScriptHMMSegmenter` for
same-script pairs or :class:`CrossScriptSegmenter` for cross-script pairs), then
registering the :class:`LanguagePair` in :mod:`app.languages.registry`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

# --------------------------------------------------------------------------- #
# Canonical script-aware text utilities
# --------------------------------------------------------------------------- #

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def fold_text(text: str) -> str:
    """Lowercase and strip combining diacritics (``atención`` -> ``atencion``).

    Safe for Latin and Cyrillic scripts; for scripts where NFD decomposes base
    characters semantically (such as Hangul), callers can supply a custom
    ``folder`` on :class:`LanguageProfile`.
    """
    decomposed = unicodedata.normalize("NFD", (text or "").lower())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def slugify_text(text: str, fallback: str = "article", *, max_length: int = 80) -> str:
    """Turn a title into a portable ASCII filename slug."""
    folded = fold_text(text or "")
    slug = _SLUG_RE.sub("-", folded).strip("-")
    return slug[:max_length] or fallback


def default_head_word(term: str) -> str:
    """The first word of a possibly multi-word entry."""
    return re.split(r"[\s/,(]", (term or "").strip(), maxsplit=1)[0]


def default_content_word(term: str) -> str:
    """The longest token in a phrase, used as a heuristic content head."""
    pieces = [p for p in re.split(r"[\s/,(]+", (term or "").strip()) if p]
    if not pieces:
        return (term or "").strip()
    if len(pieces) == 1:
        return pieces[0]
    return max(pieces, key=len)


# --------------------------------------------------------------------------- #
# Language profile
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LanguageProfile:
    """Linguistic and pedagogical profile for a single language."""

    code: str
    name: str
    native_name: str
    script: str = "latin"  # latin | cjk | kana | hangul | cyrillic | arabic | ...
    word_spaced: bool = True
    cased: bool = True
    nouns_capitalized: bool = False
    proficiency_scale: str = "CEFR"

    word_pattern: str = r"[A-Za-z]+"
    accent_chars: frozenset[str] = frozenset()
    stopwords: frozenset[str] = frozenset()
    function_words: frozenset[str] = frozenset()
    derived_suffixes: tuple[str, ...] = ()

    sentence_end_pattern: str = r"[.!?…]+[\"”'’)\]]*"
    sentence_punct: str = ".!?…\"'“‘-–—("
    abbreviations: tuple[str, ...] = ()

    min_word_length: int = 3
    min_stem_length: int = 3
    min_source_length: int = 4

    tts_locales: tuple[str, ...] = ()
    tts_rate: float = 1.0

    # Optional pluggable callables for language-specific NLP.
    stemmer: Callable[[str], str] | None = field(default=None, repr=False, compare=False)
    tokenizer: Callable[[str], list[str]] | None = field(default=None, repr=False, compare=False)
    folder: Callable[[str], str] | None = field(default=None, repr=False, compare=False)

    # Compiled regexes & pre-folded sets cached at construction.
    _word_re: re.Pattern[str] = field(init=False, repr=False, compare=False)
    _sentence_end_re: re.Pattern[str] = field(init=False, repr=False, compare=False)
    _abbrev_re: re.Pattern[str] | None = field(init=False, repr=False, compare=False)
    _folded_stopwords: frozenset[str] = field(init=False, repr=False, compare=False)
    _folded_function_words: frozenset[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_word_re", re.compile(self.word_pattern))
        object.__setattr__(self, "_sentence_end_re", re.compile(self.sentence_end_pattern))
        if self.abbreviations:
            # Match either a single uppercase initial (case-sensitive!) or one of
            # the explicit abbreviations (case-insensitive). Keeping the single
            # initial case-sensitive avoids merging sentences ending in "plan a.".
            joined = "|".join(re.escape(item) for item in self.abbreviations)
            abbrev_re = re.compile(rf"(?:\b[A-Z]\.|(?i:\b(?:{joined})\.))$")
        else:
            abbrev_re = None
        object.__setattr__(self, "_abbrev_re", abbrev_re)
        object.__setattr__(
            self, "_folded_stopwords", frozenset(self.fold(w) for w in self.stopwords if w)
        )
        object.__setattr__(
            self,
            "_folded_function_words",
            frozenset(self.fold(w) for w in self.function_words if w),
        )

    @property
    def word_re(self) -> re.Pattern[str]:
        return self._word_re

    @property
    def sentence_end_re(self) -> re.Pattern[str]:
        return self._sentence_end_re

    @property
    def abbrev_re(self) -> re.Pattern[str] | None:
        return self._abbrev_re

    @property
    def folded_stopwords(self) -> frozenset[str]:
        return self._folded_stopwords

    @property
    def folded_function_words(self) -> frozenset[str]:
        return self._folded_function_words

    def fold(self, text: str) -> str:
        if self.folder is not None:
            return self.folder(text)
        if self.script in ("cjk", "kana", "hangul"):
            return unicodedata.normalize("NFKC", (text or "").lower())
        return fold_text(text)

    def tokenize(self, text: str) -> list[str]:
        if self.tokenizer is not None:
            return self.tokenizer(text or "")
        return self._word_re.findall(text or "")

    def word_matches(self, text: str) -> Sequence[re.Match[str]]:
        return list(self._word_re.finditer(text or ""))

    def head_word(self, term: str) -> str:
        if not self.word_spaced:
            tokens = self.tokenize(term)
            return tokens[0] if tokens else (term or "").strip()
        return default_head_word(term)

    def content_word(self, term: str) -> str:
        if not self.word_spaced:
            tokens = self.tokenize(term)
            return max(tokens, key=len) if tokens else (term or "").strip()
        return default_content_word(term)

    def stem(self, word: str) -> str:
        if self.stemmer is not None:
            return self.stemmer(word)
        return self.fold(self.content_word(word))

    def is_stopword(self, word: str) -> bool:
        return self.fold(word) in self._folded_stopwords

    def is_function_word(self, word: str) -> bool:
        return self.fold(word) in self._folded_function_words

    def is_proper_noun(self, word: str, *, at_sentence_start: bool) -> bool:
        if not self.cased or self.nouns_capitalized or not word:
            return False
        return word[:1].isupper() and not at_sentence_start

    def is_inflection(self, form: str, term: str) -> bool:
        """Whether ``form`` is likely an inflection of ``term`` rather than a derivation."""
        f, t = self.fold(form).strip(), self.fold(term).strip()
        if not f or not t or f == t:
            return f == t
        if t.startswith(f) or f.startswith(t):
            return True
        if f.endswith(t):
            return True
        for suffix in self.derived_suffixes:
            if f.endswith(suffix) and not t.endswith(suffix):
                return False
        return True

    def count_units(self, text: str) -> int:
        """Count lexical units (words for spaced scripts, tokens/chars for CJK)."""
        return len(self.tokenize(text))

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "native_name": self.native_name,
            "script": self.script,
            "word_spaced": self.word_spaced,
            "cased": self.cased,
            "proficiency_scale": self.proficiency_scale,
            "tts_locales": list(self.tts_locales),
            "tts_rate": self.tts_rate,
            "stopwords": sorted(self._folded_stopwords),
        }


# --------------------------------------------------------------------------- #
# Segmenter protocol and built-in implementations
# --------------------------------------------------------------------------- #


class DiglotSegmenter(Protocol):
    """Classifies token runs inside a mixed base/target diglot passage."""

    base_code: str
    target_code: str

    def word_score(self, word: str) -> float:
        """Positive leans target language, negative leans base language, 0 undecided."""
        ...

    def decode(
        self,
        scores: Sequence[float],
        weights: Sequence[float] | None = None,
        soft_break: Sequence[bool] | None = None,
    ) -> list[int]:
        """Return 0 (base language) or 1 (target language) for each token."""
        ...

    def has_target_language(self, text: str) -> bool:
        """Fast check for whether a short string contains target-language evidence."""
        ...


@dataclass(frozen=True)
class SameScriptHMMSegmenter:
    """Two-state Viterbi HMM segmenter for same-script language pairs (e.g., EN/ES, EN/FR)."""

    base_code: str
    target_code: str
    base_words: frozenset[str]
    target_words: frozenset[str]
    ambiguous_words: frozenset[str]
    target_accents: frozenset[str]
    target_suffixes: tuple[str, ...]
    base_suffixes: tuple[str, ...]
    word_re: re.Pattern[str]
    switch_penalty: float = 1.8
    punctuated_switch: float = 0.40

    _all_ambiguous: frozenset[str] = field(init=False, repr=False, compare=False)
    _folded_base: frozenset[str] = field(init=False, repr=False, compare=False)
    _folded_target: frozenset[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        folded_base = frozenset(fold_text(w) for w in self.base_words if w)
        folded_target = frozenset(fold_text(w) for w in self.target_words if w)
        folded_ambig = frozenset(fold_text(w) for w in self.ambiguous_words if w)
        object.__setattr__(self, "_folded_base", folded_base)
        object.__setattr__(self, "_folded_target", folded_target)
        object.__setattr__(self, "_all_ambiguous", folded_ambig | (folded_base & folded_target))

    def word_score(self, word: str) -> float:
        if not word:
            return 0.0
        if any(ch in self.target_accents for ch in word):
            return 2.5
        folded = fold_text(word)
        if not folded or folded in self._all_ambiguous:
            return 0.0
        if folded in self._folded_target:
            return 1.2
        if folded in self._folded_base:
            return -1.2
        if len(folded) > 4 and folded.endswith(self.target_suffixes):
            return 0.9
        if len(folded) > 5 and folded.endswith(self.base_suffixes):
            return -0.5
        return 0.0

    def decode(
        self,
        scores: Sequence[float],
        weights: Sequence[float] | None = None,
        soft_break: Sequence[bool] | None = None,
    ) -> list[int]:
        if not scores:
            return []
        w_list = list(weights) if weights is not None else [1.0] * len(scores)
        sb_list = list(soft_break) if soft_break is not None else [False] * len(scores)

        emit = [s * w for s, w in zip(scores, w_list)]
        penalty = [
            0.0,
            *(
                self.switch_penalty * self.punctuated_switch if soft else self.switch_penalty
                for soft in sb_list[1:]
            ),
        ]

        cost = [emit[0], -emit[0]]
        back: list[tuple[int, int]] = [(0, 0)]

        for i in range(1, len(emit)):
            value = emit[i]
            switch = penalty[i]
            stay_base, from_target = cost[0], cost[1] + switch
            stay_target, from_base = cost[1], cost[0] + switch
            back.append((0 if stay_base <= from_target else 1, 1 if stay_target <= from_base else 0))
            cost = [min(stay_base, from_target) + value, min(stay_target, from_base) - value]

        state = 0 if cost[0] <= cost[1] else 1
        labels = [state]
        for i in range(len(emit) - 1, 0, -1):
            state = back[i][state]
            labels.append(state)
        labels.reverse()
        return labels

    def has_target_language(self, text: str) -> bool:
        if any(ch in self.target_accents for ch in text):
            return True
        return any(self.word_score(w) > 0 for w in self.word_re.findall(text))


def _script_predicate(script: str) -> Callable[[str], bool]:
    """Return a character predicate for a Unicode writing system."""
    if script == "cjk":
        return lambda ch: "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf"
    if script == "kana":
        return lambda ch: ("\u3040" <= ch <= "\u30ff") or ("\u4e00" <= ch <= "\u9fff")
    if script == "hangul":
        return lambda ch: "\uac00" <= ch <= "\ud7af" or "\u1100" <= ch <= "\u11ff"
    if script == "cyrillic":
        return lambda ch: "\u0400" <= ch <= "\u04ff"
    if script == "arabic":
        return lambda ch: "\u0600" <= ch <= "\u06ff"
    if script == "greek":
        return lambda ch: "\u0370" <= ch <= "\u03ff"
    # Default: Latin letters.
    return lambda ch: ("A" <= ch <= "Z") or ("a" <= ch <= "z") or ("\u00c0" <= ch <= "\u024f")


@dataclass(frozen=True)
class CrossScriptSegmenter:
    """Zero-lexicon segmenter for pairs written in distinct scripts (e.g. EN/ZH, EN/JA, ZH/EN)."""

    base_code: str
    target_code: str
    base_script: str
    target_script: str
    switch_penalty: float = 0.6

    def word_score(self, word: str) -> float:
        if not word:
            return 0.0
        is_target = _script_predicate(self.target_script)
        is_base = _script_predicate(self.base_script)
        t_count = sum(1 for ch in word if is_target(ch))
        b_count = sum(1 for ch in word if is_base(ch))
        if t_count > b_count:
            return 2.5
        if b_count > t_count:
            return -2.5
        return 0.0

    def decode(
        self,
        scores: Sequence[float],
        weights: Sequence[float] | None = None,
        soft_break: Sequence[bool] | None = None,
    ) -> list[int]:
        if not scores:
            return []
        # Cross-script runs carry decisive per-token evidence; neutral tokens
        # (punctuation, digits) inherit their neighbours via low-penalty HMM.
        w_list = list(weights) if weights is not None else [1.0] * len(scores)
        emit = [s * w for s, w in zip(scores, w_list)]
        cost = [emit[0], -emit[0]]
        back: list[tuple[int, int]] = [(0, 0)]
        sw = self.switch_penalty
        for i in range(1, len(emit)):
            v = emit[i]
            stay_b, from_t = cost[0], cost[1] + sw
            stay_t, from_b = cost[1], cost[0] + sw
            back.append((0 if stay_b <= from_t else 1, 1 if stay_t <= from_b else 0))
            cost = [min(stay_b, from_t) + v, min(stay_t, from_b) - v]
        state = 0 if cost[0] <= cost[1] else 1
        labels = [state]
        for i in range(len(emit) - 1, 0, -1):
            state = back[i][state]
            labels.append(state)
        labels.reverse()
        return labels

    def has_target_language(self, text: str) -> bool:
        is_target = _script_predicate(self.target_script)
        return any(is_target(ch) for ch in (text or ""))


# --------------------------------------------------------------------------- #
# Language pair
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LanguagePair:
    """A configured (base L1 -> target L2) reading pair."""

    id: str
    base: LanguageProfile
    target: LanguageProfile
    segmenter: DiglotSegmenter
    shelves: tuple[tuple[str, float, float, str], ...]
    accept_language: str = "en-US,en;q=0.9"
    wikipedia_host: str = "en.wikipedia.org"
    default_feeds: tuple[tuple[str, str, str], ...] = ()
    min_probe_length: int = 5
    write_placeholder: str = ""

    _combined_stopwords: frozenset[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_combined_stopwords",
            self.base.folded_stopwords | self.target.folded_stopwords,
        )

    @property
    def combined_stopwords(self) -> frozenset[str]:
        return self._combined_stopwords

    def is_target_span(self, lang: str) -> bool:
        return lang == self.target.code

    def shelf_for(self, ratio: float) -> tuple[str, str]:
        for name, low, high, description in self.shelves:
            if low <= ratio < high:
                return name, description
        return "steady", ""

    def system_prompt(self) -> str:
        """Tutor system prompt parameterized for this language pair."""
        return (
            f"You are the tutor inside a diglot reader: an app where a {self.base.name} speaker "
            f"learns {self.target.name} by reading articles that weave {self.target.name} into "
            f"{self.base.name} prose. You are precise, you never pad, and you always answer in "
            f"the JSON shape you are asked for. Glosses and explanations are in {self.base.name}; "
            f"target-language material stays in {self.target.name}."
        )

    def translation_system_prompt(self, target_name: str) -> str:
        """System prompt for direct sentence translation into an arbitrary target language."""
        return (
            f"You are a precise translator inside a language-learning reader. "
            f"Translate faithfully into {target_name} and return only the translation, "
            f"with no commentary or extra markup."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": f"{self.target.name} in {self.base.name}",
            "base": self.base.to_dict(),
            "target": self.target.to_dict(),
            "shelves": [
                {"id": name, "min": low, "max": high, "description": desc}
                for name, low, high, desc in self.shelves
            ],
            "write_placeholder": self.write_placeholder,
        }
