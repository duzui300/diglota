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

from .diglot import Article
from .glossary import Glossary
from .languages import DEFAULT_PAIR, LanguagePair, get_pair_or_default

log = logging.getLogger("diglot.warm")

MAX_WORDS = 12
WORKERS = 1

# Auto-folded function words from the default target language profile.
_SKIP = DEFAULT_PAIR.target.folded_function_words


class GlossWarmer:
    """Fills the lookup cache ahead of the reader, without being asked."""

    def __init__(self, client: Any = None, *, workers: int = WORKERS, max_words: int = MAX_WORDS) -> None:
        del client  # kept in signature for backwards compatibility; no strong ref retained
        self._max_words = max_words
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gloss-warm")
        self._lock = threading.Lock()
        self._stopped = False
        self._inflight: set[tuple[str, str]] = set()
        self._done: set[tuple[str, str]] = set()
        self.warmed = 0
        self.failed = 0

    # -- picking what to warm --------------------------------------------- #

    @staticmethod
    def candidates(article: Article, glossary: Glossary, *, limit: int = MAX_WORDS,
                   pair: LanguagePair | None = None) -> list[str]:
        """The words worth resolving before anyone clicks them, best first."""
        resolved = pair or get_pair_or_default(getattr(article, "language_pair", None))
        target = resolved.target
        target_code = getattr(article, "target_lang", target.code)
        skip = target.folded_function_words
        chosen: list[str] = []
        seen: set[str] = set()

        for item in article.focus_pairs():
            if item.en:
                continue                       # the article already glossed it
            head = target.content_word(item.es) or item.es
            folded = target.fold(head)
            if len(folded) < target.min_word_length or folded in seen or glossary.lookup(item.es, pair=resolved) is not None:
                continue
            seen.add(folded)
            chosen.append(head)
            if len(chosen) >= limit:
                return chosen

        counts: Counter[str] = Counter()
        min_len = max(target.min_source_length, target.min_word_length)
        for block in article.blocks:
            if block.kind not in ("p", "h"):
                continue
            for span in block.spans:
                if span.lang != target_code:
                    continue
                for word in target.tokenize(span.text):
                    folded = target.fold(word)
                    if len(folded) < min_len or folded in skip or folded in seen:
                        continue
                    counts[word] += 1

        for word, _ in counts.most_common():
            if glossary.lookup(word, pair=resolved) is not None:
                continue          # already answerable offline
            chosen.append(word)
            if len(chosen) >= limit:
                break
        return chosen

    # -- running ---------------------------------------------------------- #

    def warm(self, article: Article, glossary: Glossary, lookup: Callable[[str, str], Any]) -> int:
        """Queue this article's unknown words. Returns how many were queued."""
        resolved = get_pair_or_default(getattr(article, "language_pair", None))
        queued = 0
        for word in self.candidates(article, glossary, limit=self._max_words, pair=resolved):
            key = (article.slug, resolved.target.fold(word))
            with self._lock:
                if self._stopped or key in self._inflight or key in self._done:
                    continue
                self._inflight.add(key)
            try:
                self._pool.submit(self._one, key, word, article, lookup)
                queued += 1
            except RuntimeError:
                with self._lock:
                    self._inflight.discard(key)
                break
        return queued

    def _one(self, key: tuple[str, str], word: str, article: Article,
             lookup: Callable[[str, str], Any]) -> None:
        succeeded = False
        try:
            result = lookup(word, article.slug)
            if isinstance(result, dict) and result.get("error"):
                log.debug("warming %r returned error: %s", word, result.get("error"))
            else:
                succeeded = True
        except Exception as exc:  # best-effort by design
            log.debug("warming %r failed: %s", word, exc)
        finally:
            with self._lock:
                if succeeded:
                    self.warmed += 1
                    self._done.add(key)
                else:
                    self.failed += 1
                self._inflight.discard(key)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"warmed": self.warmed, "failed": self.failed, "pending": len(self._inflight)}

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
        self._pool.shutdown(wait=False, cancel_futures=True)


def words_in(article: Article, glossary: Glossary, limit: int = MAX_WORDS) -> list[str]:
    """What would be warmed for this article, without doing it. For diagnostics."""
    return GlossWarmer.candidates(article, glossary, limit=limit)
