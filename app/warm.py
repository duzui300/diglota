"""Warming the glossary for an article you have just opened.

The local glossary answers most clicks instantly, but every article also
contains words it never glossed -- exactly the ones a learner is most likely to
click. Those still cost a model call, and the click that needs it is the click
the learner is waiting on.

So the work is moved off the click. Opening an article queues its most frequent
unresolved words for a small background pool: by the time anyone has read the
first paragraph, the words they are about to click are already in the cache.

Kept deliberately separate from :mod:`app.jobs`:

*   it is **invisible** -- filling a cache is not something to report, and a job
    entry per article opened would bury the imports that *are* worth reporting;
*   it is **small and bounded** -- two threads, a cap per article, and words
    already known are never requested;
*   it is **best-effort** -- failures are counted and dropped. The learner can
    still click the word and get the normal on-demand lookup.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .diglot import Article, _WORD_RE
from .glossary import Glossary
from .vocab import content_word, fold

log = logging.getLogger("diglot.warm")

# How many words to warm per article. Enough to cover what a reader clicks in
# the first few minutes of a new text; more than that spends model calls on
# words nobody was going to ask about.
MAX_WORDS = 24

# Why this many: the point is to be invisible. A pool large enough to matter and
# small enough never to compete with a real import for the endpoint.
WORKERS = 2

# Words this common are either known or glossed in the article already.
_SKIP = frozenset(
    """de la el los las un una unos unas que y e o u en por para con sin sobre
    entre al del a se le les lo su sus mi tu es son era eran fue fueron ser
    estar está están ni no sí si te os nos me más menos muy ya tan todo toda
    todos todas mas esta este esto""".split()
)


class GlossWarmer:
    """Fills the lookup cache ahead of the reader, without being asked."""

    def __init__(self, client: Any, *, workers: int = WORKERS, max_words: int = MAX_WORDS) -> None:
        self._client = client
        self._max_words = max_words
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gloss-warm")
        self._lock = threading.Lock()
        self._inflight: set[tuple[str, str]] = set()
        self._done: set[tuple[str, str]] = set()
        self.warmed = 0
        self.failed = 0

    # -- picking what to warm --------------------------------------------- #

    @staticmethod
    def candidates(article: Article, glossary: Glossary, *, limit: int = MAX_WORDS) -> list[str]:
        """The words worth resolving before anyone clicks them, best first.

        Two tiers. **Focus phrases first**: the article bolds exactly what it
        means to teach, and a learner who does not know what a bolded phrase
        means is the one most likely to click it. That matters because plenty of
        them carry no inline gloss -- ``**pintan**`` appears bare, and the
        post-reading vocabulary box does not always cover it, so nothing else
        would ever resolve it.

        Then **frequency**: a word the text uses eight times is eight chances to
        be clicked, and a word used once may never be.
        """
        chosen: list[str] = []
        seen: set[str] = set()

        for pair in article.focus_pairs():
            if pair.en:
                continue                       # the article already glossed it
            head = content_word(pair.es) or pair.es
            folded = fold(head)
            if len(folded) < 3 or folded in seen or glossary.lookup(pair.es) is not None:
                continue
            seen.add(folded)
            chosen.append(head)
            if len(chosen) >= limit:
                return chosen

        counts: Counter[str] = Counter()
        for block in article.blocks:
            if block.kind not in ("p", "h"):
                continue
            for span in block.spans:
                if span.lang != "es":
                    continue
                for word in _WORD_RE.findall(span.text):
                    folded = fold(word)
                    if len(folded) < 4 or folded in _SKIP or folded in seen:
                        continue
                    counts[word] += 1

        for word, _ in counts.most_common():
            if glossary.lookup(word) is not None:
                continue          # already answerable offline
            chosen.append(word)
            if len(chosen) >= limit:
                break
        return chosen

    # -- running ---------------------------------------------------------- #

    def warm(self, article: Article, glossary: Glossary, lookup: Callable[[str, str], Any]) -> int:
        """Queue this article's unknown words. Returns how many were queued."""
        queued = 0
        for word in self.candidates(article, glossary, limit=self._max_words):
            key = (article.slug, fold(word))
            with self._lock:
                if key in self._inflight or key in self._done:
                    continue
                self._inflight.add(key)
            queued += 1
            self._pool.submit(self._one, key, word, article, lookup)
        return queued

    def _one(self, key: tuple[str, str], word: str, article: Article,
             lookup: Callable[[str, str], Any]) -> None:
        try:
            lookup(word, article.slug)
            self.warmed += 1
        except Exception as exc:  # best-effort by design
            self.failed += 1
            log.debug("warming %r failed: %s", word, exc)
        finally:
            with self._lock:
                self._inflight.discard(key)
                self._done.add(key)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"warmed": self.warmed, "failed": self.failed, "pending": len(self._inflight)}

    def stop(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def words_in(article: Article, glossary: Glossary, limit: int = MAX_WORDS) -> list[str]:
    """What would be warmed for this article, without doing it. For diagnostics."""
    return GlossWarmer.candidates(article, glossary, limit=limit)
