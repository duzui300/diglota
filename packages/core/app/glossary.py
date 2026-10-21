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

# Endings that *build* a word rather than inflect one: ``profundamente`` and
# ``profundidad`` are not forms of ``profundizar``, they are different words on the
# same root -- and all four share the stem ``profun``, so the stem key alone cannot
# tell them apart.
#
# That is how clicking the adverb ``profundamente`` came back with "to deepen", from
# the lesson's entry for the verb. It looked authoritative: instant, no model call,
# nothing saying it was a guess. A reader who hits two of those stops trusting the
# instant lookups altogether, which is worse than the lookup being slow.
#
# A blocklist rather than a grammar of Spanish inflection, deliberately: the cost of
# a miss here is one model call, and the cost of a false match is a wrong meaning
# presented as fact.
_DERIVED_SUFFIXES = (
    "mente",                                     # adverbs: profundamente, claramente
    "idad", "edad", "dad",                       # nouns: profundidad, soledad
    "cion", "sion", "zon",                       # nouns: conexion, decision
    "miento", "mientos",                         # nouns: sentimiento
    "oso", "osa", "osos", "osas",                # adjectives: peligroso
    "ivo", "iva", "ivos", "ivas",                # adjectives: comprensivo
    "able", "ables", "ible", "ibles",            # adjectives: disponible
    "ancia", "encia",                            # nouns: importancia, paciencia
    "eza", "ura",                                # nouns: firmeza, cordura
    "ismo", "ista",                              # nouns: realismo, artista
    "isimo", "isima",                            # superlatives: altisimo
    "ito", "ita", "illo", "illa",                # diminutives
    "ador", "adora", "edor", "ente", "ante",     # agent nouns and participles
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
        # Only when the head is a word worth keying on. For a Spanish phrase the first
        # word is often a clitic -- the head of "se vuelve" is "se" -- and keying entries
        # under it made seventeen reflexive phrases in this library candidates for each
        # other: a click on "se vuelve" could be answered with whichever lesson's
        # "se han debilitado" ranked first, which is how it came back as "to weaken".
        # The content-word key below is the one that was wanted.
        if head and fold(head) != fold(term) and fold(head) not in STOPWORDS:
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
            bucket = self.entries.setdefault(key, [])
            if any(same.term == entry.term and same.gloss == entry.gloss
                   and same.article == entry.article for same in bucket):
                continue
            bucket.append(entry)
            # Best source first, so a reader of this dataclass can take [0] and get
            # what they would have got before there was a list at all.
            bucket.sort(key=lambda item: _SOURCE_RANK.get(item.source, 9))

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

    def lookup(self, term: str, *, article: str | None = None) -> Entry | None:
        """The lesson's own translation of a word, or ``None``.

        The stem key is what lets a click on ``vuelve`` reach the ``volverse`` entry,
        and it is also how a click on ``profundamente`` reached the ``profundizar``
        entry and was told "to deepen". So a stem hit is checked against the entry it
        found: inflections answer, derivations do not, and a word the lesson does not
        translate falls through to the model -- an instant wrong answer is worse than
        a slow right one.

        ``article`` is the passage being read, and an entry from it wins over an equally
        good entry from somewhere else. A form can be glossed differently by two
        lessons, and the reader wants the one in front of them.
        """
        with self._lock:
            for key in self.keys_for(term):
                bucket = self.entries.get(key)
                if not bucket:
                    continue
                if key.startswith("s:"):
                    bucket = [entry for entry in bucket if is_inflection(term, entry.term)]
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
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def is_inflection(form: str, term: str) -> bool:
    """Whether a stem match is the same word in another form.

    Shape, not grammar: a verb's person and tense endings pass, a nominal or
    adjectival ending passes, and an ending that *builds* a different word does not.
    ``profundiza`` is a form of ``profundizar``; ``profundamente`` is not.

    Two cases are left deliberately unresolved because the shape cannot decide them:
    ``profundo`` is both an adjective in its own right and the first person singular
    of ``profundizar``, and the ending ``-o`` is a legitimate one for both. Answering
    from the stem there is a coin flip, and it is the sort of coin flip this function
    exists to stop making -- but it is caught only when the ending gives it away.

    A miss costs one model call. A false match costs a wrong meaning shown as fact,
    which is why the list is of endings rather than a promise of coverage.
    """
    f, t = fold(form).strip(), fold(term).strip()
    if not f or not t or f == t:
        return f == t
    if t.startswith(f) or f.startswith(t):
        # One is the other with something glued on: "darse" for "darse cuenta de".
        return True
    if f.endswith(t):
        return True
    for suffix in _DERIVED_SUFFIXES:
        if f.endswith(suffix) and not t.endswith(suffix):
            return False
    return True
