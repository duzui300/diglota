"""Application runtime container: models, library, glossary, corpus graph, and job launchers."""

from __future__ import annotations

import logging
import re
import threading
import urllib.parse
from typing import Any

from . import corpus, writing
from .config import Settings
from .glossary import Glossary
from .grading import Grading
from .ingest import import_article, import_writing
from .jobs import Job, JobManager
from .judge import Judge
from .languages import LanguagePair, get_pair_or_default
from .levels import clamp_ratio, resolve_level, resolve_weave
from .library import Library, deck_signature
from .llm import LLMClient, LLMError
from .recommend import recommend
from .store import Store
from .tutor import Tutor, cache_key
from .warm import GlossWarmer

log = logging.getLogger("diglot")

# How long a failed lookup is remembered.
FAILURE_TTL = 180

# Ceiling on the deck page.
DECK_PAGE_LIMIT = 2000

# How many search results a discovery page shows.
DISCOVER_PAGE = 14


def _job_title(url: str, title_hint: str | None) -> str:
    """A readable label for the Activity panel."""
    if title_hint:
        return title_hint
    try:
        parsed = urllib.parse.urlparse(url if "://" in url else "https://" + url)
    except ValueError:
        return url
    site = parsed.netloc.removeprefix("www.")
    slug = urllib.parse.unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1])
    name = re.sub(r"[_\-]+", " ", slug).strip()
    return f"{name.title()}, {site}" if name else site or url


def _paste_title(text: str) -> str:
    """A label for a pasted import."""
    first = next((line.strip() for line in (text or "").splitlines() if line.strip()), "")
    first = re.sub(r"^#{1,6}\s*", "", first).strip()
    if len(first) > 56:
        first = first[:55].rstrip() + "…"
    return f"{first}, pasted text" if first else "Pasted text"


class _WeaveCache:
    """Adapts the store's cache to the two-method shape the weaver wants."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def get(self, key: str) -> Any | None:
        return self._store.cache_get(key)

    def put(self, key: str, value: Any) -> None:
        self._store.cache_put(key, value)


class App:
    """Everything the routes need, built once at startup with thread-safe reloads."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = Store(settings.db_path)
        self.library = Library(settings.corpus_dir, settings.library_dir)
        self.jobs = JobManager(store=self.store)
        self.glossary = Glossary(pair=self.active_pair)
        self.weave_cache = _WeaveCache(self.store)

        self._model_lock = threading.RLock()
        self._glossary_lock = threading.Lock()
        self._graph_lock = threading.Lock()
        self._inflight_lock = threading.Lock()
        self._inflight_lookups: dict[str, tuple[threading.Event, list[dict[str, Any]]]] = {}

        self._glossary_revision = -1
        self._glossary_deck = 0
        self._graph: dict[str, Any] | None = None
        self._graph_key: tuple[Any, ...] | None = None

        self.warmer: GlossWarmer | None = None
        self.judge: Judge | None = None
        self.grading: Grading | None = None
        self.llm: LLMClient | None = None
        self.tutor: Tutor | None = None
        self._lookup_llm: LLMClient | None = None
        self._lookup_tutor: Tutor | None = None
        self._lookup_override: bool = False
        self._weave_llm: LLMClient | None = None
        self._weave_tutor: Tutor | None = None
        self._weave_override: bool = False
        self._build_models()

    @property
    def active_pair(self) -> LanguagePair:
        return get_pair_or_default(self.settings.active_pair)

    @property
    def lookup_tutor(self) -> Tutor | None:
        """Tutor used for word gloss lookups (falls back to default tutor unless overridden)."""
        if self._lookup_override:
            return self._lookup_tutor
        return self.tutor

    @property
    def weave_tutor(self) -> Tutor | None:
        """Tutor used for weaving passages into diglot lessons (falls back to default unless overridden)."""
        if self._weave_override:
            return self._weave_tutor
        return self.tutor

    def _build_client_for(self, role: str) -> tuple[LLMClient | None, Tutor | None, bool]:
        endpoint = self.settings.resolve_llm(role)
        override = role != "default" and not endpoint.inherited
        if role != "default" and not override:
            return None, None, False
        if not endpoint.ready:
            return None, None, override
        try:
            client = LLMClient(endpoint.base_url, endpoint.api_key, endpoint.model)
            return client, Tutor(client, pair=self.active_pair), override
        except LLMError as exc:
            log.warning("LLM tier %s unavailable: %s", role, exc)
            return None, None, override

    def _build_models(self) -> None:
        """Build the AI tiers from the current settings (thread-safe)."""
        with self._model_lock:
            old_warmer = self.warmer
            old_judge = self.judge

            self.llm, self.tutor, _ = self._build_client_for("default")
            self._lookup_llm, self._lookup_tutor, self._lookup_override = self._build_client_for("lookup")
            self._weave_llm, self._weave_tutor, self._weave_override = self._build_client_for("weave")
            self.warmer = (
                GlossWarmer()
                if (self.lookup_tutor is not None and self.settings.should_warm_glosses())
                else None
            )

            self.judge = Judge(self.settings.typesafe_api_key, self.settings.typesafe_model)
            self.grading = Grading(self.judge, self.tutor, pair=self.active_pair)

        if old_warmer is not None:
            old_warmer.stop()
        if old_judge is not None:
            try:
                old_judge.close()
            except Exception:
                pass

    def reload_models(self) -> None:
        """Re-read the settings file and rebuild the clients."""
        self.settings.apply_saved()
        self._build_models()
        log.info(
            "models rebuilt: default %s, lookup %s, weave %s, judge %s",
            "ready" if self.tutor else "not configured",
            "ready" if self.lookup_tutor else "not configured",
            "ready" if self.weave_tutor else "not configured",
            "ready" if self.judge and self.judge.available else "not configured",
        )

    # -- glossary & corpus graph ------------------------------------------ #

    def ensure_glossary(self) -> Glossary:
        """Rebuild the local glossary if the shelves or the deck have changed."""
        deck = self.store.list_words(limit=5000)
        signature = deck_signature(deck)
        with self._glossary_lock:
            if (
                self.library.revision != self._glossary_revision
                or signature != self._glossary_deck
            ):
                self.glossary.build((entry.article for entry in self.library.all()), deck)
                self._glossary_revision = self.library.revision
                self._glossary_deck = signature
                log.info("glossary rebuilt: %d entries", len(self.glossary))
        return self.glossary

    def corpus_graph(self) -> dict[str, Any]:
        """The passage graph, rebuilt only when the library or deck changes."""
        deck = self.store.list_words(limit=5000)
        key = (self.library.revision, deck_signature(deck))
        with self._graph_lock:
            if self._graph_key != key:
                entries = self.library.all()
                coverage = {
                    entry.article.slug: self.library.coverage(entry.article, deck)["text"]["token_ratio"]
                    for entry in entries
                }
                self._graph = corpus.build_graph(entries, coverage)
                self._graph_key = key
                log.info("corpus graph rebuilt: %s", self._graph["stats"])
            return self._graph

    # -- word lookups ----------------------------------------------------- #

    def _cached_lookup(self, key: str, *, allow_failed: bool = True) -> dict[str, Any] | None:
        hit = self.store.cache_get(key)
        if hit is None:
            return None
        if hit.get("failed"):
            if not allow_failed:
                return None
            return self.store.cache_get(key, max_age_seconds=FAILURE_TTL)
        return hit

    @staticmethod
    def _from_cache(term: str, hit: dict[str, Any]) -> dict[str, Any]:
        if hit.get("failed"):
            out: dict[str, Any] = {
                "term": term, "error": "the tutor could not be reached",
                "retryable": True, "instant": True, "model": False,
            }
            if hit.get("detail"):
                out["detail"] = hit["detail"]
            return out
        return {"term": term, **hit, "instant": True, "model": False}

    def _normalize_term(self, term: str) -> tuple[str, str]:
        cleaned = term.strip(" \t\r\n.,;:!?\"'“”‘’()[]¿¡—–-")
        lower = (cleaned or term.strip()).lower()
        folded = self.active_pair.target.fold(lower)
        return lower, folded

    def resolve_word(
        self, term: str, sentence: str, slug: str | None,
        *, deep: bool = False, fresh: bool = False, from_warmer: bool = False,
    ) -> dict[str, Any]:
        """Answer a lookup, cheapest source first."""
        if deep:
            self.store.log_scaffolding("full-entry", slug=slug, term=term)
            return self._ask_tutor(
                term, sentence, slug,
                preserve_existing_cache=True, from_warmer=from_warmer,
            )

        lower, folded = self._normalize_term(term)
        allow_failed = not fresh

        for key in (
            cache_key("gloss", term.lower(), pair=self.active_pair),
            cache_key("gloss", lower, pair=self.active_pair),
            cache_key("gloss", "fold", folded, pair=self.active_pair) if folded else "",
            cache_key("gloss", "lemma", folded, pair=self.active_pair) if folded else "",
        ):
            if not key:
                continue
            hit = self._cached_lookup(key, allow_failed=allow_failed)
            if hit is not None:
                return self._from_cache(term, hit)

        glossary = self.ensure_glossary()
        entry = glossary.lookup(term, article=slug)
        if entry is not None:
            return {"term": term, **entry.to_dict(), "instant": True, "model": False}

        for candidate in glossary.keys_for(term):
            if not candidate.startswith("s:"):
                continue
            stem_hit = self._cached_lookup(
                cache_key("gloss", "stem", candidate[2:], pair=self.active_pair),
                allow_failed=allow_failed,
            )
            if stem_hit is not None:
                answer = self._from_cache(term, stem_hit)
                answer["note_from"] = "a related form"
                return answer

        return self._ask_tutor(term, sentence, slug, from_warmer=from_warmer)

    def _ask_tutor(
        self, term: str, sentence: str, slug: str | None,
        *, preserve_existing_cache: bool = False, from_warmer: bool = False,
    ) -> dict[str, Any]:
        tutor = self.lookup_tutor
        if tutor is None:
            return {
                "term": term, "gloss": None,
                "error": "not in the glossary, and the lookup model is not configured",
            }
        lower, folded = self._normalize_term(term)
        flight_key = f"{self.active_pair.id}:{folded or lower}:{1 if preserve_existing_cache else 0}"
        leader = False
        with self._inflight_lock:
            existing = self._inflight_lookups.get(flight_key)
            if existing is None:
                event = threading.Event()
                holder: list[dict[str, Any]] = []
                self._inflight_lookups[flight_key] = (event, holder)
                leader = True
            else:
                event, holder = existing

        if not leader:
            event.wait(timeout=65)
            if holder:
                return {"term": term, **{k: v for k, v in holder[0].items() if k != "term"}}

        try:
            result = self._run_tutor_lookup(
                tutor, term, lower, folded, sentence, slug,
                preserve_existing_cache=preserve_existing_cache,
                from_warmer=from_warmer,
            )
            if leader:
                holder.append(result)
            return result
        finally:
            if leader:
                with self._inflight_lock:
                    self._inflight_lookups.pop(flight_key, None)
                event.set()

    def _run_tutor_lookup(
        self, tutor: Tutor, term: str, lower: str, folded: str,
        sentence: str, slug: str | None,
        *, preserve_existing_cache: bool, from_warmer: bool,
    ) -> dict[str, Any]:
        title = ""
        if slug:
            entry = self.library.get(slug)
            title = entry.article.title if entry else ""
        key = cache_key("gloss", term.lower(), pair=self.active_pair)
        try:
            data = tutor.gloss_word(word=term, sentence=sentence or term, article_title=title)
        except LLMError as exc:
            detail = str(exc)[:180]
            log.warning("gloss failed for %r: %s", term, detail)
            # Background warming must never poison the failure cache for a real click;
            # and a deep=True retry must never overwrite a valid cached gloss.
            if not from_warmer:
                existing = self.store.cache_get(key) if preserve_existing_cache else None
                if not (existing and not existing.get("failed")):
                    self.store.cache_put(key, {"gloss": None, "failed": True, "detail": detail})
            return {
                "term": term,
                "error": "the tutor could not be reached",
                "detail": detail,
                "retryable": True,
            }

        self.store.cache_put(key, data)
        if lower != term.lower():
            self.store.cache_put(cache_key("gloss", lower, pair=self.active_pair), data)
        if folded:
            self.store.cache_put(cache_key("gloss", "fold", folded, pair=self.active_pair), data)
        lemma = str(data.get("lemma") or "").strip()
        if lemma:
            lemma_folded = self.active_pair.target.fold(lemma.lower())
            if lemma_folded:
                self.store.cache_put(cache_key("gloss", "lemma", lemma_folded, pair=self.active_pair), data)
        for candidate in self.ensure_glossary().keys_for(term):
            if candidate.startswith("s:"):
                self.store.cache_put(cache_key("gloss", "stem", candidate[2:], pair=self.active_pair), data)
        model_name = getattr(getattr(tutor, "client", None), "model", "")
        out = {"term": term, **data, "instant": False, "model": True}
        if model_name:
            out["model_name"] = model_name
        return out

    def cached(self, key: str, produce):
        """Return a cached AI result, or produce and cache it."""
        hit = self.store.cache_get(key)
        if hit is not None:
            return hit
        value = produce()
        if value is not None:
            self.store.cache_put(key, value)
        return value

    # -- background jobs --------------------------------------------------- #

    def already_taught(self, *, exclude: str | None = None) -> dict[str, list[str]]:
        vocab_items: list[str] = []
        grammar_items: list[str] = []
        for entry in self.library.all():
            if exclude and entry.article.slug == exclude:
                continue
            vocab_items += [anchor.term for anchor in entry.article.vocab if anchor.term]
            grammar_items += [note.title for note in entry.article.grammar if note.title]
        saved = [row.get("term") or "" for row in self.store.list_words(limit=DECK_PAGE_LIMIT)]
        return {"taught": vocab_items + [term for term in saved if term], "explained": grammar_items}

    def start_import(
        self, url: str | None, title_hint: str | None,
        level: str | None = None, ratio: float | None = None,
        weave: str | None = None, text: str | None = None,
        source_url: str | None = None,
    ) -> Job:
        tutor = self.weave_tutor
        judge = self.judge
        if tutor is None:
            raise LLMError("the weave model is not configured")
        chosen = resolve_level(level)
        grain = resolve_weave(weave)
        amount = clamp_ratio(ratio, chosen)
        target_name = self.active_pair.target.name

        def work(progress):
            known = self.already_taught()
            result = import_article(
                url=url, settings=self.settings, tutor=tutor, title_hint=title_hint,
                judge=judge, progress=progress, level_code=chosen.code, ratio=amount,
                weave=grain, cache=self.weave_cache, text=text, source_url=source_url,
                already_taught=known["taught"], already_explained=known["explained"],
            )
            self.library.refresh()
            return result

        settings_note = f"{chosen.code}, {round(amount * 100)}% {target_name}"
        label = title_hint or (_paste_title(text) if (text or "").strip() else _job_title(url or "", None))
        return self.jobs.submit(kind="import", title=f"{label}. {settings_note}", work=work)

    def start_lesson_from_writing(
        self, text: str, title: str,
        level: str | None = None, ratio: float | None = None,
        weave: str | None = None,
    ) -> Job:
        tutor = self.weave_tutor
        judge = self.judge
        if tutor is None:
            raise LLMError("the weave model is not configured")
        chosen = resolve_level(level)
        grain = resolve_weave(weave)
        amount = clamp_ratio(ratio, chosen)
        target_name = self.active_pair.target.name

        def work(progress):
            known = self.already_taught()
            result = import_writing(
                text=text, settings=self.settings, tutor=tutor, title=title,
                judge=judge, progress=progress, level_code=chosen.code, ratio=amount,
                weave=grain, cache=self.weave_cache,
                already_taught=known["taught"], already_explained=known["explained"],
            )
            self.library.refresh()
            return result

        label = (title or writing.title_for(text))[:70]
        settings_note = f"{chosen.code}, {round(amount * 100)}% {target_name}"
        return self.jobs.submit(kind="writing", title=f"{label}. {settings_note}", work=work)

    def start_recommend(self) -> Job:
        tutor = self.tutor
        if tutor is None:
            raise LLMError("the tutor is not configured")

        def work(progress):
            progress.step("Reading your vocabulary", done=0, total=1)
            words = self.store.list_words(limit=200)
            read = [
                entry.article.title
                for slug, state in self.store.all_progress().items()
                if state.get("completed_at")
                for entry in [self.library.get(slug)]
                if entry is not None
            ]
            return recommend(
                tutor=tutor, words=words, read=read,
                proxy=self.settings.proxy, progress=progress,
            )

        return self.jobs.submit(kind="recommend", title="Finding something to read next", work=work)
