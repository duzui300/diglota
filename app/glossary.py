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
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

from .diglot import Article, _WORD_RE
from .vocab import content_word, fold, head_word, stem

# Words too common to be worth an entry: a gloss for "de" is noise.
_STOP = frozenset(
    """de la el los las un unas unas que y e o u en por para con sin sobre
    entre al del a se le les lo su sus mi tu es son era eran fue fueron ser
    estar está están ni no sí si te os nos me más menos muy ya""".split()
)

# Everything a learner will click constantly and never want to study. Exported
# so other parts of the app can ask the same question -- the "words you keep
# looking up" list was offering to save "del" and "a", because those *are* what
# you look up most often, and that is exactly why they are not worth saving.
#
# The English words are here for a different reason: in a diglot some English
# inevitably lands inside a span the segmenter called Spanish ("for", "main",
# "that"). Counting those as Spanish vocabulary inflates the exposure totals and
# puts junk into the corpus graph, and the fix is the same list.
#
# The short function words ("of", "in", "at", "is") were missing until the passage
# map started naming clusters from what their titles share -- and promptly named a
# cluster of fifteen art articles "of", because "the end of creativity" and "the
# start of a new movement" both have one. Any list this long is going to have a
# hole in it; this is where the hole was.
STOPWORDS = frozenset(_STOP) | frozenset(
    """esto esta este estos estas eso esa esos esas aquel aquella ello ellos ellas
    nosotras vosotros vosotras cual cuales quien quienes cuando donde como porque
    aunque pero sino también tambien siempre nunca ahora antes despues luego aquí
    alli así tan tanto todo toda todos todas otro otra mismo misma cada algun
    alguna ningun ninguna nada nadie algo alguien sea sean fuera fuese siendo sido
    hay había habia hace hacen hacer puede pueden podía podia tiene tienen tenía
    tenia debe deben era eres somos sois estábamos estaban ser estar haber tener
    qué que cómo quien cuál dónde cuándo cuánto""".split()
) | frozenset(
    """the and for that with this from have been were are was will would could should
    they them their there here what when where which while whose about into over
    than then they're more most some such only also just very much many other
    these those your you're our ours its it's does did doing done make makes made
    take takes taken give gives given come comes came goes went going said says
    say see sees seen look looks looked know knows known think thinks thought
    a an of in on at by to as is am it or but not no nor so if be been being do
    has had can may might must my me we us he she his her him one two all any
    out up down off own same too why how who whom both each few per via within
    without across along around near upon toward towards against between among
    during before after above below under""".split()
)

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

    entries: dict[str, Entry] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # -- indexing --------------------------------------------------------- #

    @staticmethod
    def keys_for(term: str) -> list[str]:
        """Every key a term should be findable under, best first.

        The stem key is what makes inflections work: ``vuelve`` and ``volverse``
        share one, so an entry for either answers a click on the other.
        """
        term = term.strip()
        if not term:
            return []
        keys = [f"w:{fold(term)}"]
        head = head_word(term)
        if head and fold(head) != fold(term):
            keys.append(f"w:{fold(head)}")
        content = content_word(term)
        if content:
            keys.append(f"c:{fold(content)}")
        for piece in (term, head, content):
            candidate = stem(piece)
            if len(candidate) >= 3:
                keys.append(f"s:{candidate}")
        return list(dict.fromkeys(keys))

    def _add(self, entry: Entry) -> None:
        if not entry.gloss or not entry.term:
            return
        for key in self.keys_for(entry.term):
            existing = self.entries.get(key)
            # Keep the best-sourced entry for a key; ties go to the first seen.
            if existing is None or _SOURCE_RANK.get(entry.source, 9) < _SOURCE_RANK.get(existing.source, 9):
                self.entries[key] = entry

    # -- building --------------------------------------------------------- #

    def build(self, articles: Iterable[Article], deck: Iterable[dict]) -> "Glossary":
        fresh = Glossary()
        for article in articles:
            fresh._absorb_article(article)
        for word in deck:
            fresh._absorb_deck_word(word)
        with self._lock:
            self.entries = fresh.entries
        return self

    def _absorb_article(self, article: Article) -> None:
        # 1. Bold focus phrases with their gloss: the cleanest source, because
        #    the author bolded them precisely to teach them.
        for pair in article.focus_pairs():
            if pair.en:
                self._add(Entry(term=pair.es, gloss=pair.en, source="focus",
                                article=article.slug, article_title=article.title))

        # 2. The post-reading vocabulary box, which lists the headword together
        #    with its inflections: "volverse / se vuelva, se volverá".
        for anchor in article.vocab:
            if not anchor.gloss:
                continue
            for form in re.split(r"[/,;]", anchor.term):
                form = form.strip().strip("*").strip()
                if len(form) < _MIN_WORD:
                    continue
                self._add(Entry(term=form, gloss=anchor.gloss, source="vocabulary box",
                                article=article.slug, article_title=article.title))

        # 3. Inline glosses. The parser ends a Spanish span where its gloss
        #    begins, so the gloss describes the end of that span -- the last
        #    word, or the last few words when the gloss is a phrase.
        for block in article.blocks:
            for span in block.spans:
                if span.lang != "es" or not span.gloss:
                    continue
                words = _WORD_RE.findall(span.text)
                if not words:
                    continue
                last = words[-1]
                if len(last) < _MIN_WORD or fold(last) in _STOP:
                    continue
                gloss_words = len(span.gloss.split())
                if gloss_words <= 2:
                    self._add(Entry(term=last, gloss=span.gloss, source="article",
                                    article=article.slug, article_title=article.title))
                elif gloss_words <= 4 and len(words) >= gloss_words:
                    phrase = " ".join(words[-gloss_words:])
                    self._add(Entry(term=phrase, gloss=span.gloss, source="article",
                                    article=article.slug, article_title=article.title))

    def _absorb_deck_word(self, word: dict) -> None:
        gloss = (word.get("gloss") or "").strip()
        if not gloss:
            return
        term = (word.get("term") or "").strip()
        if not term:
            return
        entry = Entry(
            term=term, gloss=gloss, source="your deck",
            lemma=(word.get("lemma") or None), pos=(word.get("pos") or None),
        )
        self._add(entry)
        lemma = (word.get("lemma") or "").strip()
        if lemma and fold(lemma) != fold(term):
            self._add(Entry(term=lemma, gloss=gloss, source="your deck", pos=entry.pos))

    # -- lookup ----------------------------------------------------------- #

    def lookup(self, term: str) -> Entry | None:
        with self._lock:
            for key in self.keys_for(term):
                entry = self.entries.get(key)
                if entry is not None:
                    return entry
        return None

    def __len__(self) -> int:
        with self._lock:
            return len({id(entry) for entry in self.entries.values()})

    def compact(self) -> dict[str, dict[str, Any]]:
        """The index in a form the browser can search, for instant local lookups.

        Only whole-word keys are sent. Stem keys would be dead weight: the
        browser cannot compute a Spanish stem, so it could never look one up --
        and a reader only ever clicks a word as the article wrote it. The server
        still consults stems, so a click that misses locally costs one very fast
        round trip rather than a model call.
        """
        with self._lock:
            return {
                key: entry.to_dict()
                for key, entry in self.entries.items()
                if key.startswith("w:")
            }


def normalise(text: str) -> str:
    """Lowercase and strip accents -- exposed for callers doing their own keys."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
