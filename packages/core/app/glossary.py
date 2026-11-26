"""A local glossary, mined from the corpus the app already reads.

A word lookup was one model call per click: several seconds of latency to
answer a question the corpus had often already answered in print. Every diglot
article carries the author's own translations -- inline glosses
(``los anales (*annals*)``), bolded focus phrases with their meanings, and a
post-reading vocabulary box that is effectively a morphology table
(``volverse / se vuelva, se volverá`` ``(*to become*)``). All of it is parsed on
every startup and then thrown away.

This keeps it. The result answers most clicks with no network at all, which is
both faster and more trustworthy than a model's guess: these are the
translations the lesson intended to teach.

Three properties make it work in practice:

*   **Inflections hit.** Entries are indexed by folded form, by head word and by
    stem, so clicking ``vuelve`` finds the ``volverse`` entry.
*   **The learner's own deck is included.** A word already saved is one they are
    most likely to click again, and their gloss is the one they chose.
*   **Every entry says where it came from**, so the UI can present a real
    dictionary entry differently from the article's in-passing translation.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable

from .diglot import Article
from .languages import DEFAULT_PAIR, LanguagePair, fold_text, get_pair_or_default

# Words too common to be worth an entry: auto-folded in ``app.languages``.
_STOP = DEFAULT_PAIR.target.folded_function_words
_DERIVED_SUFFIXES = DEFAULT_PAIR.target.derived_suffixes
STOPWORDS = DEFAULT_PAIR.combined_stopwords

_MIN_WORD = 2

# Where an entry came from, in the order we prefer to show it. A curated phrase
# beats an inline gloss, which beats the learner's own note.
_SOURCE_RANK = {"focus": 0, "vocabulary box": 1, "article": 2, "your deck": 3}


@dataclass
class Entry:
    term: str                      # the form as the article wrote it
    gloss: str
    source: str = "article"        # focus | vocabulary box | article | your deck
    article: str | None = None     # slug, when it came from an article
    article_title: str | None = None
    lemma: str | None = None
    pos: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {"term": self.term, "gloss": self.gloss, "source": self.source}
        for key in ("article", "article_title", "lemma", "pos"):
            value = getattr(self, key)
            if value:
                data[key] = value
        return data


@dataclass
class Glossary:
    """A lookup index over the corpus and the learner's deck."""

    # key -> every entry that claims it, best-sourced first.
    #
    # A key can have more than one answer, and for the whole-word keys that matters:
    # two lessons gloss the same form differently -- ``se vuelve`` is *becomes* in one
    # and part of ``se vuelve a estudiar`` in another -- so a click in the second
    # lesson used to be answered with the first lesson's sense. Keeping the candidates
    # and ranking them at read time lets the lesson being read win, which is the same
    # argument as the inflection check: the lookup has to be right about *this* word in
    # *this* passage, or the reader learns to distrust it.
    entries: dict[str, list["Entry"]] = field(default_factory=dict)
    pair: LanguagePair = field(default=DEFAULT_PAIR, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # -- indexing --------------------------------------------------------- #

    @staticmethod
    def keys_for(term: str, *, pair: LanguagePair | None = None) -> list[str]:
        """Every key a term should be findable under, best first."""
        resolved = pair or DEFAULT_PAIR
        target = resolved.target
        term = term.strip()
        if not term:
            return []
        folded_term = target.fold(term)
        keys = [f"w:{folded_term}"]
        head = target.head_word(term)
        if head and target.fold(head) != folded_term and target.fold(head) not in resolved.combined_stopwords:
            keys.append(f"w:{target.fold(head)}")
        content = target.content_word(term)
        if content:
            keys.append(f"c:{target.fold(content)}")
        for piece in (term, head, content):
            candidate = target.stem(piece)
            if len(candidate) >= target.min_stem_length:
                keys.append(f"s:{candidate}")
        return list(dict.fromkeys(keys))

    def _add(self, entry: Entry, *, pair: LanguagePair | None = None) -> None:
        if not entry.gloss or not entry.term:
            return
        for key in self.keys_for(entry.term, pair=pair or self.pair):
            bucket = self.entries.setdefault(key, [])
            if any(same.term == entry.term and same.gloss == entry.gloss
                   and same.article == entry.article for same in bucket):
                continue
            bucket.append(entry)
            bucket.sort(key=lambda item: _SOURCE_RANK.get(item.source, 9))

    # -- building --------------------------------------------------------- #

    def build(self, articles: Iterable[Article], deck: Iterable[dict]) -> "Glossary":
        fresh = Glossary(pair=self.pair)
        for article in articles:
            fresh._absorb_article(article)
        for word in deck:
            fresh._absorb_deck_word(word)
        with self._lock:
            self.entries = fresh.entries
        return self

    def _absorb_article(self, article: Article) -> None:
        pair = get_pair_or_default(getattr(article, "language_pair", None))
        target = pair.target
        target_code = getattr(article, "target_lang", target.code)
        # 1. Bold focus phrases with their gloss.
        for item in article.focus_pairs():
            if item.en:
                self._add(Entry(term=item.es, gloss=item.en, source="focus",
                                article=article.slug, article_title=article.title), pair=pair)

        # 2. The post-reading vocabulary box.
        for anchor in article.vocab:
            if not anchor.gloss:
                continue
            for form in re.split(r"[/,;]", anchor.term):
                form = form.strip().strip("*").strip()
                if len(form) < _MIN_WORD:
                    continue
                self._add(Entry(term=form, gloss=anchor.gloss, source="vocabulary box",
                                article=article.slug, article_title=article.title), pair=pair)

        # 3. Inline glosses.
        for block in article.blocks:
            for span in block.spans:
                if span.lang != target_code or not span.gloss:
                    continue
                words = target.tokenize(span.text)
                if not words:
                    continue
                last = words[-1]
                if len(last) < _MIN_WORD or target.fold(last) in target.folded_function_words:
                    continue
                gloss_words = len(span.gloss.split())
                if gloss_words <= 2:
                    self._add(Entry(term=last, gloss=span.gloss, source="article",
                                    article=article.slug, article_title=article.title), pair=pair)
                elif gloss_words <= 4 and len(words) >= gloss_words:
                    joiner = " " if target.word_spaced else ""
                    phrase = joiner.join(words[-gloss_words:])
                    self._add(Entry(term=phrase, gloss=span.gloss, source="article",
                                    article=article.slug, article_title=article.title), pair=pair)

    def _absorb_deck_word(self, word: dict) -> None:
        gloss = (word.get("gloss") or "").strip()
        if not gloss:
            return
        term = (word.get("term") or "").strip()
        if not term:
            return
        pair = get_pair_or_default(word.get("language_pair"))
        entry = Entry(
            term=term, gloss=gloss, source="your deck",
            lemma=(word.get("lemma") or None), pos=(word.get("pos") or None),
        )
        self._add(entry, pair=pair)
        lemma = (word.get("lemma") or "").strip()
        if lemma and pair.target.fold(lemma) != pair.target.fold(term):
            self._add(Entry(term=lemma, gloss=gloss, source="your deck", pos=entry.pos), pair=pair)

    # -- lookup ----------------------------------------------------------- #

    def lookup(self, term: str, *, article: str | None = None,
               pair: LanguagePair | None = None) -> Entry | None:
        resolved = pair or self.pair
        with self._lock:
            for key in self.keys_for(term, pair=resolved):
                bucket = self.entries.get(key)
                if not bucket:
                    continue
                if key.startswith("s:"):
                    bucket = [entry for entry in bucket if is_inflection(term, entry.term, pair=resolved)]
                    if not bucket:
                        continue
                if article:
                    here = [entry for entry in bucket if entry.article == article]
                    if here:
                        return here[0]
                return bucket[0]
        return None

    def __len__(self) -> int:
        with self._lock:
            return len({id(entry) for bucket in self.entries.values() for entry in bucket})

    def compact(self) -> dict[str, list[dict[str, Any]]]:
        """The index in a form the browser can search, for instant local lookups.

        Only whole-word keys are sent. Stem keys would be dead weight: the
        browser cannot compute a Spanish stem, so it could never look one up --
        and a reader only ever clicks a word as the article wrote it. The server
        still consults stems, so a click that misses locally costs one very fast
        round trip rather than a model call.

        Each key carries *every* entry that claims it, not just the best one, so the
        browser can prefer the lesson being read exactly as the server does. Two
        lessons rarely disagree about a word, and when they do the reader is looking at
        one of them.
        """
        with self._lock:
            return {
                key: [entry.to_dict() for entry in bucket]
                for key, bucket in self.entries.items()
                if key.startswith("w:")
            }


def normalise(text: str) -> str:
    """Lowercase and strip accents -- exposed for callers doing their own keys."""
    return fold_text(text)


def is_inflection(form: str, term: str, *, pair: LanguagePair | None = None) -> bool:
    """Whether a stem match is the same word in another form."""
    return (pair or DEFAULT_PAIR).target.is_inflection(form, term)
