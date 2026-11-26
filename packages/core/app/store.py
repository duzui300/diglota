"""SQLite persistence facade.

Composes the modular repositories under :mod:`app.storage` (``db``, ``schema``,
``vocabulary``, ``reading``, ``content``, ``ops``) while preserving the exact
``Store`` interface relied on by the API layer and test suite.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterator, Sequence

from .languages import DEFAULT_PAIR_ID
from .storage import (
    SCHEMA,
    ContentRepo,
    Database,
    OpsRepo,
    ReadingRepo,
    VocabularyRepo,
    days_ago as _days_ago,
    now_iso,
    parse_iso,
)


class Store:
    """High-level SQLite store backed by domain repositories."""

    QUOTE_SCAN = ContentRepo.QUOTE_SCAN

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.db = Database(self.path)
        self.vocab = VocabularyRepo(self.db)
        self.reading = ReadingRepo(self.db)
        self.content = ContentRepo(self.db)
        self.ops = OpsRepo(self.db)

    # -- plumbing --------------------------------------------------------- #

    @property
    def conn(self) -> sqlite3.Connection:
        return self.db.conn

    def write(self) -> Iterator[sqlite3.Connection]:
        return self.db.write()

    def close(self) -> None:
        self.db.close()

    # -- vocabulary & reviews --------------------------------------------- #

    def add_word(
        self, *, term: str, gloss: str | None = None, lemma: str | None = None,
        pos: str | None = None, note: str | None = None,
        article_slug: str | None = None, context: str | None = None,
        language_pair: str = DEFAULT_PAIR_ID,
    ) -> dict[str, Any]:
        return self.vocab.add_word(
            term=term, gloss=gloss, lemma=lemma, pos=pos, note=note,
            article_slug=article_slug, context=context, language_pair=language_pair,
        )

    def list_words(
        self, *, limit: int = 500, search: str | None = None,
        language_pair: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.vocab.list_words(limit=limit, search=search, language_pair=language_pair)

    def word_count(self, *, language_pair: str | None = None) -> int:
        return self.vocab.word_count(language_pair=language_pair)

    def known_terms(self, *, language_pair: str | None = None) -> set[str]:
        return self.vocab.known_terms(language_pair=language_pair)

    def delete_word(self, word_id: int) -> bool:
        return self.vocab.delete_word(word_id)

    def update_word(self, word_id: int, **fields: Any) -> None:
        self.vocab.update_word(word_id, **fields)

    def due_cards(
        self, *, limit: int = 40, include_new: int = 12,
        language_pair: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.vocab.due_cards(limit=limit, include_new=include_new, language_pair=language_pair)

    def review_card(self, card_id: int, rating: str, *, elapsed_ms: int = 0) -> dict[str, Any]:
        return self.vocab.review_card(card_id, rating, elapsed_ms=elapsed_ms)

    def due_count(self, *, language_pair: str | None = None) -> int:
        return self.vocab.due_count(language_pair=language_pair)

    def export_rows(self, *, language_pair: str | None = None) -> Sequence[sqlite3.Row]:
        return self.vocab.export_rows(language_pair=language_pair)

    # -- lookups, reading, exposure, scaffolding, activity ---------------- #

    def log_lookup(
        self, *, term: str, lemma: str | None = None, gloss: str | None = None,
        article_slug: str | None = None, context: str | None = None,
        language_pair: str = DEFAULT_PAIR_ID,
    ) -> None:
        self.reading.log_lookup(
            term=term, lemma=lemma, gloss=gloss, article_slug=article_slug,
            context=context, language_pair=language_pair,
        )

    def frequent_lookups(
        self, *, minimum: int = 2, limit: int = 12,
        skip: frozenset[str] | set[str] = frozenset(),
        language_pair: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.reading.frequent_lookups(
            minimum=minimum, limit=limit, skip=skip, language_pair=language_pair
        )

    def lookup_count(self, lemma: str) -> int:
        return self.reading.lookup_count(lemma)

    def progress_for(self, slug: str) -> dict[str, Any]:
        return self.reading.progress_for(slug)

    def all_progress(self) -> dict[str, dict[str, Any]]:
        return self.reading.all_progress()

    def save_progress(
        self, slug: str, *, position: float | None = None,
        seconds: int = 0, completed: bool = False,
        language_pair: str = DEFAULT_PAIR_ID,
    ) -> None:
        self.reading.save_progress(
            slug, position=position, seconds=seconds, completed=completed,
            language_pair=language_pair,
        )

    def credited_position(self, slug: str) -> float:
        return self.reading.credited_position(slug)

    def mark_credited(self, slug: str, position: float) -> None:
        self.reading.mark_credited(slug, position)

    def record_exposure(
        self, *, slug: str, lemmas: dict[str, int],
        known_stems: set[str], glossed: set[str],
        language_pair: str = DEFAULT_PAIR_ID,
    ) -> int:
        return self.reading.record_exposure(
            slug=slug, lemmas=lemmas, known_stems=known_stems, glossed=glossed,
            language_pair=language_pair,
        )

    def exposure_totals(self, *, days: int = 7) -> dict[str, Any]:
        return self.reading.exposure_totals(days=days)

    def exposure_split(self, *, days: int = 7, repeated_at: int = 4) -> dict[str, Any]:
        return self.reading.exposure_split(days=days, repeated_at=repeated_at)

    def exposure_series(self, *, days: int = 30) -> list[dict[str, Any]]:
        return self.reading.exposure_series(days=days)

    def exposure_by_article(self, *, days: int = 30, limit: int = 12) -> list[dict[str, Any]]:
        return self.reading.exposure_by_article(days=days, limit=limit)

    def new_words_series(self, *, days: int = 60) -> list[dict[str, Any]]:
        return self.reading.new_words_series(days=days)

    def top_encountered(
        self, *, days: int = 30, limit: int = 20,
        skip: frozenset[str] | set[str] = frozenset(),
    ) -> list[dict[str, Any]]:
        return self.reading.top_encountered(days=days, limit=limit, skip=skip)

    def log_scaffolding(
        self, kind: str, *, slug: str | None = None, term: str | None = None,
        language_pair: str = DEFAULT_PAIR_ID,
    ) -> None:
        self.reading.log_scaffolding(kind, slug=slug, term=term, language_pair=language_pair)

    def scaffolding_summary(self, *, days: int = 7) -> dict[str, Any]:
        return self.reading.scaffolding_summary(days=days)

    def log_event(
        self, kind: str, *, slug: str | None = None, amount: float = 0.0,
        language_pair: str = DEFAULT_PAIR_ID,
    ) -> None:
        self.reading.log_event(kind, slug=slug, amount=amount, language_pair=language_pair)

    def activity(self, days: int = 30) -> list[dict[str, Any]]:
        return self.reading.activity(days=days)

    def streak(self) -> dict[str, int]:
        return self.reading.streak()

    def stats(self) -> dict[str, Any]:
        return self.reading.stats(
            word_count=self.word_count(),
            due_count=self.due_count(),
            quote_count=self.quote_count(),
        )

    # -- quotes & writing ------------------------------------------------- #

    def save_quote(
        self, *, text: str, es: str, en: str = "", glosses: str = "", note: str | None = None,
        article_slug: str | None = None, block_index: int | None = None,
        start: int | None = None, end: int | None = None, term: str | None = None,
        language_pair: str = DEFAULT_PAIR_ID,
    ) -> tuple[int, bool]:
        return self.content.save_quote(
            text=text, es=es, en=en, glosses=glosses, note=note,
            article_slug=article_slug, block_index=block_index,
            start=start, end=end, term=term, language_pair=language_pair,
        )

    def quotes(
        self, *, search: str | None = None, slug: str | None = None,
        term: str | None = None, limit: int = 400,
        language_pair: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.content.quotes(
            search=search, slug=slug, term=term, limit=limit, language_pair=language_pair
        )

    def quote(self, quote_id: int) -> dict[str, Any] | None:
        return self.content.quote(quote_id)

    def set_quote_note(self, quote_id: int, note: str) -> bool:
        return self.content.set_quote_note(quote_id, note)

    def delete_quote(self, quote_id: int) -> bool:
        return self.content.delete_quote(quote_id)

    def quote_count(self) -> int:
        return self.content.quote_count()

    def quotes_using(self, term: str, *, limit: int = 6) -> list[dict[str, Any]]:
        return self.content.quotes_using(term, limit=limit)

    def start_writing(
        self, *, title: str, prompt: str = "", language_pair: str = DEFAULT_PAIR_ID
    ) -> dict[str, Any]:
        return self.content.start_writing(title=title, prompt=prompt, language_pair=language_pair)

    def add_revision(
        self, writing_id: int, *, text: str, reading: dict | None = None,
        verdict: dict | None = None, feedback: dict | None = None,
        words: int = 0, checked: bool = False,
    ) -> dict[str, Any] | None:
        return self.content.add_revision(
            writing_id, text=text, reading=reading, verdict=verdict,
            feedback=feedback, words=words, checked=checked,
        )

    def writings(self, *, limit: int = 60) -> list[dict[str, Any]]:
        return self.content.writings(limit=limit)

    def writing(self, writing_id: int) -> dict[str, Any] | None:
        return self.content.writing(writing_id)

    def latest_revision(self, writing_id: int) -> dict[str, Any] | None:
        return self.content.latest_revision(writing_id)

    def delete_writing(self, writing_id: int) -> bool:
        return self.content.delete_writing(writing_id)

    def writing_totals(self) -> dict[str, Any]:
        return self.content.writing_totals()

    # -- quizzes, cache, jobs, challenges --------------------------------- #

    def save_attempt(
        self, *, slug: str | None, question: str, answer: str | None,
        verdict: str | None, score: float | None, feedback: str | None,
    ) -> None:
        self.ops.save_attempt(
            slug=slug, question=question, answer=answer,
            verdict=verdict, score=score, feedback=feedback,
        )

    def attempts_for(self, slug: str) -> list[dict[str, Any]]:
        return self.ops.attempts_for(slug)

    def cache_get(self, key: str, *, max_age_seconds: int | None = None) -> Any | None:
        return self.ops.cache_get(key, max_age_seconds=max_age_seconds)

    def cache_put(self, key: str, value: Any) -> None:
        self.ops.cache_put(key, value)

    def cache_forget(self, key: str) -> None:
        self.ops.cache_forget(key)

    def save_job(self, job: dict[str, Any]) -> None:
        self.ops.save_job(job)

    def list_jobs(self, limit: int = 30) -> list[dict[str, Any]]:
        return self.ops.list_jobs(limit=limit)

    def delete_job(self, job_id: str) -> None:
        self.ops.delete_job(job_id)

    def clear_finished_jobs(self) -> int:
        return self.ops.clear_finished_jobs()

    def start_challenge(
        self, kind: str, target: int, *, language_pair: str = DEFAULT_PAIR_ID
    ) -> dict[str, Any]:
        return self.ops.start_challenge(kind, target, language_pair=language_pair)

    def active_challenge(self) -> dict[str, Any] | None:
        return self.ops.active_challenge()

    def complete_challenge(self, challenge_id: int) -> bool:
        return self.ops.complete_challenge(challenge_id)

    def end_challenge(self, challenge_id: int) -> bool:
        return self.ops.end_challenge(challenge_id)

    def challenge_history(self, *, limit: int = 12) -> list[dict[str, Any]]:
        return self.ops.challenge_history(limit=limit)

    # -- maintenance ------------------------------------------------------ #

    def reset(self, keep_words: bool = False) -> None:
        self.db.reset(keep_words=keep_words)

    def backup(self, folder: Path) -> Path:
        return self.db.backup(folder)

    def forget_article(self, slug: str) -> None:
        self.db.forget_article(slug)


__all__ = ["SCHEMA", "Store", "_days_ago", "now_iso", "parse_iso"]
