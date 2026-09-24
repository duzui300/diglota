"""FastAPI application: the HTTP surface of the diglot reader.

Every endpoint is synchronous, which is what FastAPI wants for blocking work
(SQLite, urllib) -- it runs them on a thread pool rather than stalling the event
loop. The two AI tiers are slow enough that they are never called on a path the
UI is waiting on for anything else; results are cached against a content hash so
that the cost is paid once per article, not once per visit.

API keys stay here. The browser receives article text, verdicts, probabilities
and token counts -- never a credential.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import threading
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analytics, challenges, corpus, ingest, registers, transfer, vocab, writing
from .config import Settings
from .diglot import Article, content_fingerprint
from .glossary import STOPWORDS, Glossary
from .grading import Grading, is_correct, is_provisional, may_fail
from .ingest import import_article, import_writing
from .jobs import Job, JobManager
from .judge import Judge
from . import levels
from .levels import clamp_ratio, resolve_level, resolve_weave
from .library import Library, LibraryEntry, deck_signature
from .llm import LLMClient, LLMError
# Imported under a name of its own because "quote" is a noun this module uses
# constantly, and a module called `quotes` next to a local `quotes` is a bug
# waiting to be written.
from .quotes import locate as quote_pick
from .reading import glossed_lemmas, tokens_between
# The module and its main function share a name, and this file needs both:
# `recommend` is called to run the pipeline, `broaden` to suggest what is missing.
from .recommend import broaden as recommend_broaden
from .recommend import recommend
from .recommend import topics_from_titles
from .store import Store
from .tutor import Tutor, cache_key
from .warm import GlossWarmer

log = logging.getLogger("diglot")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# How long a failed lookup is remembered. Long enough that repeatedly clicking
# a word during an outage is cheap, short enough that the app heals itself once
# the endpoint comes back without anyone clearing a cache.
FAILURE_TTL = 180

# Ceiling on the deck page. A personal deck is hundreds of cards, so this is not
# a limit anyone meets -- but a silent cap reads as "this is everything you have",
# so the page is told the real total and says so if they ever diverge.
DECK_PAGE_LIMIT = 2000

# How many search results a discovery page shows. The dialog asks the sources for
# one more than this so it can tell "there are more" from "that is all of them".
DISCOVER_PAGE = 14


def _job_title(url: str, title_hint: str | None) -> str:
    """A readable label for the Activity panel.

    A raw URL is the wrong thing to show: it is long, it is truncated in the
    middle, and "https://en.wikipedia.org/wiki/Why_We_Sleep" tells the reader
    less at a glance than "Why We Sleep · en.wikipedia.org".
    """
    if title_hint:
        return title_hint
    try:
        parsed = urllib.parse.urlparse(url if "://" in url else "https://" + url)
    except ValueError:
        return url
    site = parsed.netloc.removeprefix("www.")
    slug = urllib.parse.unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1])
    name = re.sub(r"[_\-]+", " ", slug).strip()
    return f"{name.title()} · {site}" if name else site or url


def _paste_title(text: str) -> str:
    """A label for a pasted import.

    A paste has no URL to read a name out of, so the name comes from its own
    first line -- which for a pasted article is usually its headline. Truncated
    rather than wrapped, because this is an Activity-panel row.
    """
    first = next((line.strip() for line in (text or "").splitlines() if line.strip()), "")
    first = re.sub(r"^#{1,6}\s*", "", first).strip()
    if len(first) > 56:
        first = first[:55].rstrip() + "…"
    return f"{first} · pasted text" if first else "Pasted text"


class _WeaveCache:
    """Adapts the store's cache to the two-method shape the weaver wants.

    The weaver should not know about SQLite, and the store should not grow a
    second pair of cache methods just to look like something it is not.
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    def get(self, key: str) -> Any | None:
        return self._store.cache_get(key)

    def put(self, key: str, value: Any) -> None:
        self._store.cache_put(key, value)


class App:
    """Everything the routes need, built once at startup."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = Store(settings.db_path)
        self.library = Library(settings.corpus_dir, settings.library_dir)
        self.jobs = JobManager(store=self.store)
        self.glossary = Glossary()
        self.weave_cache = _WeaveCache(self.store)
        self._glossary_revision = -1
        self._glossary_deck = 0
        self._graph: dict[str, Any] | None = None
        self._graph_key: tuple[Any, ...] | None = None
        self.warmer: GlossWarmer | None = None
        self.grading: Grading | None = None
        self.llm: LLMClient | None = None
        self.tutor: Tutor | None = None
        self._build_models()

    def _build_models(self) -> None:
        """Build the two AI tiers from the current settings.

        Separate from __init__ because the settings panel can change the key while
        the app is running, and a reader who has just pasted one should not have to
        restart to find out whether it worked -- most people running this have no
        terminal open, so a setting that needs a restart is a setting that looks
        broken.
        """
        self.llm = None
        self.tutor = None
        # Each warmer owns a thread pool, so the one being replaced is shut down
        # rather than left for the garbage collector -- saving settings twice should
        # not leave two pools warming glosses through a client nobody is using.
        if self.warmer is not None:
            self.warmer.stop()
        self.warmer = None
        if self.settings.llm_ready:
            try:
                self.llm = LLMClient(self.settings.llm_base_url, self.settings.llm_api_key,
                                     self.settings.llm_model)
                self.tutor = Tutor(self.llm)
                self.warmer = GlossWarmer(self)
            except LLMError as exc:
                log.warning("LLM tier unavailable: %s", exc)
        # Rebuilt every time, and built even with no tutor: the local tier always
        # exists, so an exercise can always be answered and never silently marked
        # wrong. The judge is its own service and its own key.
        self.judge = Judge(self.settings.typesafe_api_key, self.settings.typesafe_model)
        self.grading = Grading(self.judge, self.tutor)

    def reload_models(self) -> None:
        """Re-read the settings file and rebuild the clients.

        Re-applied onto *this* app's settings rather than a freshly loaded
        ``Settings``: a fresh one derives ``data_dir`` from the environment, which is
        not necessarily where this app is keeping its files -- so a reload could read
        someone else's settings file, or none.
        """
        self.settings.apply_saved()
        self._build_models()
        log.info("models rebuilt: tutor %s, judge %s",
                 "ready" if self.tutor else "not configured",
                 "ready" if self.judge.available else "not configured")

    # -- glossary ---------------------------------------------------------- #

    def ensure_glossary(self) -> Glossary:
        """Rebuild the local glossary if the shelves or the deck have changed.

        Rebuilding is cheap -- it indexes articles already parsed and held in
        memory -- so it is done lazily on access rather than invalidated by
        hand at every call site that might change something.
        """
        deck = self.store.list_words(limit=5000)
        signature = deck_signature(deck)
        if (self.library.revision != self._glossary_revision
                or signature != self._glossary_deck):
            self.glossary.build((entry.article for entry in self.library.all()), deck)
            self._glossary_revision = self.library.revision
            self._glossary_deck = signature
            log.info("glossary rebuilt: %d entries", len(self.glossary))
        return self.glossary

    def corpus_graph(self) -> dict[str, Any]:
        """The passage graph, rebuilt only when the library or deck changes.

        Comparing every pair of articles is O(n^2) set intersections; cheap at
        two dozen passages, wasteful to redo on every page load. Cached against
        the library revision and the contents of the deck -- what the passages
        are, and how much of each the reader already knows.
        """
        deck = self.store.list_words(limit=5000)
        key = (self.library.revision, deck_signature(deck))
        if self._graph_key != key:
            coverage = {
                entry.article.slug: self.library.coverage(entry.article, deck)["text"]["token_ratio"]
                for entry in self.library.all()
            }
            self._graph = corpus.build_graph(self.library.all(), coverage)
            self._graph_key = key
            log.info("corpus graph rebuilt: %s", self._graph["stats"])
        return self._graph

    def _cached_lookup(self, key: str) -> dict[str, Any] | None:
        """Read a cached lookup, honouring a short TTL on remembered failures."""
        hit = self.store.cache_get(key)
        if hit is None:
            return None
        if hit.get("failed"):
            # A real entry is good forever; a remembered outage only for a few
            # minutes, so the app recovers on its own once the endpoint is back.
            return self.store.cache_get(key, max_age_seconds=FAILURE_TTL)
        return hit

    @staticmethod
    def _from_cache(term: str, hit: dict[str, Any]) -> dict[str, Any]:
        """Turn a cached record into a response.

        A remembered *failure* has to come back as an error, not as an entry
        with a null gloss -- otherwise clicking a word during an outage shows an
        empty panel that looks like the word has no meaning, rather than one
        that says the tutor is unreachable.
        """
        if hit.get("failed"):
            return {"term": term, "error": "the tutor could not be reached",
                    "retryable": True, "instant": True, "model": False}
        return {"term": term, **hit, "instant": True, "model": False}

    def resolve_word(self, term: str, sentence: str, slug: str | None,
                     *, deep: bool = False) -> dict[str, Any]:
        """Answer a lookup, cheapest source first.

        The order is the whole point: most clicks should never reach the model.
        Local first -- instant, and it is the lesson's own translation.

        With ``deep``, the model is consulted regardless. That matters because
        the two answers are not the same *kind* of thing: the corpus gives a
        translation, while the tutor gives the lemma, the part of speech, an
        example, and a note on what learners get wrong. A reader who wants that
        should not be blocked by the fact that a shorter answer existed.
        """
        if deep:
            # Asking for the fuller entry is a scaffolding event: the short
            # answer was not enough. Logged here rather than in the browser,
            # because the browser would need another request to say so.
            self.store.log_scaffolding("full-entry", slug=slug, term=term)
            return self._ask_tutor(term, sentence, slug)

        # A tutor entry already fetched for this word outranks the glossary:
        # its presence means someone asked for the fuller answer, and it would
        # be odd to hide it the second time.
        hit = self._cached_lookup(cache_key("gloss", term.lower()))
        if hit is not None:
            return self._from_cache(term, hit)

        glossary = self.ensure_glossary()
        entry = glossary.lookup(term)
        if entry is not None:
            return {"term": term, **entry.to_dict(), "instant": True, "model": False}

        # An inflection of something already known: answer with that rather than
        # paying for a call whose result we effectively have.
        for candidate in glossary.keys_for(term):
            if not candidate.startswith("s:"):
                continue
            stem_hit = self._cached_lookup(cache_key("gloss", "stem", candidate[2:]))
            if stem_hit is not None:
                answer = self._from_cache(term, stem_hit)
                answer["note_from"] = "a related form"
                return answer

        return self._ask_tutor(term, sentence, slug)

    def _ask_tutor(self, term: str, sentence: str, slug: str | None) -> dict[str, Any]:
        if self.tutor is None:
            return {"term": term, "gloss": None,
                    "error": "not in the glossary, and the tutor is not configured"}
        title = ""
        if slug:
            entry = self.library.get(slug)
            title = entry.article.title if entry else ""
        try:
            data = self.tutor.gloss_word(word=term, sentence=sentence or term, article_title=title)
        except LLMError as exc:
            log.warning("gloss failed for %r: %s", term, exc)
            # Remember the failure briefly. Without this, clicking the same word
            # again while the endpoint is down pays the whole timeout again --
            # which is how one slow lookup becomes an app that feels broken.
            self.store.cache_put(cache_key("gloss", term.lower()), {"gloss": None, "failed": True})
            return {"term": term, "error": "the tutor could not be reached", "retryable": True}
        self.store.cache_put(cache_key("gloss", term.lower()), data)
        for candidate in self.ensure_glossary().keys_for(term):
            if candidate.startswith("s:"):
                self.store.cache_put(cache_key("gloss", "stem", candidate[2:]), data)
        return {"term": term, **data, "instant": False, "model": True}

    def cached(self, key: str, produce):
        """Return a cached AI result, or produce and cache it. Never raises."""
        hit = self.store.cache_get(key)
        if hit is not None:
            return hit
        value = produce()
        if value is not None:
            self.store.cache_put(key, value)
        return value

    # -- background jobs --------------------------------------------------- #

    def start_import(self, url: str | None, title_hint: str | None,
                     level: str | None = None, ratio: float | None = None,
                     weave: str | None = None, text: str | None = None,
                     source_url: str | None = None) -> Job:
        if self.tutor is None:
            raise LLMError("the tutor is not configured")
        chosen = resolve_level(level)
        grain = resolve_weave(weave)
        amount = clamp_ratio(ratio, chosen)

        def work(progress):
            result = import_article(
                url=url, settings=self.settings, tutor=self.tutor, title_hint=title_hint,
                judge=self.judge, progress=progress, level_code=chosen.code, ratio=amount,
                weave=grain, cache=self.weave_cache, text=text, source_url=source_url,
            )
            self.library.refresh()
            return result

        # The title carries the settings, because two imports of the same
        # article at different levels are different lessons and should not look
        # identical in the Activity panel.
        settings_note = f"{chosen.code} · {round(amount * 100)}% Spanish"
        label = title_hint or (_paste_title(text) if (text or "").strip() else _job_title(url or "", None))
        return self.jobs.submit(kind="import", title=f"{label} — {settings_note}", work=work)

    def start_lesson_from_writing(self, text: str, title: str,
                                  level: str | None = None, ratio: float | None = None,
                                  weave: str | None = None) -> Job:
        """Weave one of the reader's own pieces into a lesson, in any language."""
        if self.tutor is None:
            raise LLMError("the tutor is not configured")
        chosen = resolve_level(level)
        grain = resolve_weave(weave)
        amount = clamp_ratio(ratio, chosen)

        def work(progress):
            result = import_writing(
                text=text, settings=self.settings, tutor=self.tutor, title=title,
                judge=self.judge, progress=progress, level_code=chosen.code, ratio=amount,
                weave=grain, cache=self.weave_cache,
            )
            self.library.refresh()
            return result

        label = (title or writing.title_for(text))[:70]
        settings_note = f"{chosen.code} · {round(amount * 100)}% Spanish"
        return self.jobs.submit(kind="writing", title=f"{label} — {settings_note}", work=work)

    def start_recommend(self) -> Job:
        if self.tutor is None:
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
            return recommend(tutor=self.tutor, words=words, read=read,
                             proxy=self.settings.proxy, progress=progress)

        return self.jobs.submit(kind="recommend", title="Finding something to read next", work=work)


class ShareBody(BaseModel):
    text: str
    # Optional: the reader's name, written into the file so the recipient knows
    # who it came from. The origin is filled in by the app, not asked for.
    creator: str = ""
    slug: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.load()
    application = App(settings)
    application.library.refresh()
    app.state.app = application
    log.info(
        "diglot ready: %d articles, %d words, llm=%s judge=%s",
        len(application.library), application.store.word_count(),
        settings.llm_ready, settings.judge_ready,
    )
    try:
        yield
    finally:
        if application.warmer is not None:
            application.warmer.stop()
        application.jobs.stop()
        application.judge.close()
        application.store.close()


app = FastAPI(title="Diglot", version="1.0", lifespan=lifespan)


def ctx() -> App:
    return app.state.app


# Stopping the app from inside the app.
#
# The launcher starts it with pythonw, which has no console and no window: without
# this the only way to stop it is the Task Manager, and "just close the window" --
# the answer on every other app -- is not available. run.py hands the server's own
# exit flag in here.
_shutdown: Callable[[], None] | None = None


def set_shutdown_hook(hook: Callable[[], None] | None) -> None:
    global _shutdown
    _shutdown = hook


@app.post("/api/quit")
def quit_app() -> dict[str, Any]:
    """Stop the server, because the reader asked it to.

    The reply goes out first -- the exit is scheduled a moment later on a timer -- so
    the browser gets an answer rather than a dropped connection. Nothing is lost in
    the shutdown: every save is its own committed transaction and the jobs, the
    warmer and the database are closed by the lifespan, which has been doing that for
    every other kind of stop.

    Not available when the app was started some other way (an embedding server, a
    test client), and that is said rather than silently ignored.
    """
    if _shutdown is None:
        raise HTTPException(
            501, "this app was started in a way that cannot be stopped from here")
    threading.Timer(0.4, _shutdown).start()
    return {"stopping": True}


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #


class ProgressBody(BaseModel):
    position: float | None = None
    seconds: int = 0
    completed: bool = False


class RegisterBody(BaseModel):
    # A register code, or empty to take the tag back and let the app infer one.
    # Not called "register": that shadows an attribute of BaseModel itself, and
    # pydantic says so on every start.
    code: str | None = None


class SaveWordBody(BaseModel):
    term: str
    gloss: str | None = None
    lemma: str | None = None
    pos: str | None = None
    note: str | None = None
    article_slug: str | None = None
    context: str | None = None


class ReviewBody(BaseModel):
    rating: str = Field(pattern="^(again|hard|good|easy)$")
    elapsed_ms: int = 0


class AttemptBody(BaseModel):
    kind: str = Field(pattern="^(translate|cloze|comprehension)$")
    question: str = ""
    answer: str
    source: str | None = None        # English source, for translation drills
    reference: str | None = None     # the article's own Spanish
    slug: str | None = None


class ImportBody(BaseModel):
    # One of these two, not both: a link to fetch, or the reader's own paste for
    # when the page cannot be fetched at all. The paste stands in for the fetch,
    # not for the lesson -- everything after it is the same pipeline.
    url: str | None = None
    text: str | None = None
    # Where the paste came from. Kept for the reader's own reference; never
    # fetched, since by definition the fetch is the thing that did not work.
    source_url: str | None = None
    title_hint: str | None = None
    # The learner's three dials: what kind of Spanish, how much of it, and what
    # unit it arrives in. All optional -- omit them and the app estimates a level
    # and weaves to its own defaults.
    level: str | None = None
    ratio: float | None = None
    weave: str | None = None


class DiscoverBody(BaseModel):
    query: str
    # URLs the reader has already been shown. Re-asking the same sources the same
    # question returns the same list, so "show me others" can only be honoured by
    # asking for the ones beyond these.
    exclude: list[str] = []


# --------------------------------------------------------------------------- #
# Status and library
# --------------------------------------------------------------------------- #


@app.get("/api/status")
def status() -> dict[str, Any]:
    application = ctx()
    describe = application.settings.describe()
    describe["articles"] = len(application.library)
    describe["words"] = application.store.word_count()
    describe["due"] = application.store.due_count()
    describe["grading"] = application.grading.describe()
    return describe


@app.get("/api/library")
def library() -> dict[str, Any]:
    application = ctx()
    # The whole deck, not just the lemma set: coverage matching needs each
    # word's surface form as well, because a saved "se ponen de acuerdo" only
    # matches the article through its head word.
    deck = application.store.list_words(limit=2000)
    progress = application.store.all_progress()
    return {
        "articles": application.library.catalogue(deck, progress),
        "words": len(deck),
        "due": application.store.due_count(),
        "worth_saving": application.store.frequent_lookups(minimum=2, limit=8, skip=STOPWORDS),
    }


@app.get("/api/article/{slug}")
def article(slug: str) -> dict[str, Any]:
    application = ctx()
    entry = application.library.get(slug)
    if entry is None:
        raise HTTPException(404, f"no article {slug!r}")
    deck = application.store.list_words(limit=2000)
    data = entry.to_dict(with_blocks=True)
    data["coverage"] = application.library.coverage(entry.article, deck)
    # Whether the tag can be changed from here: one passage per file, so the reader
    # is not offered a button that will refuse.
    data["register_editable"] = application.library.passages_per_file().get(str(entry.path or ""), 1) == 1
    data["progress"] = application.store.progress_for(slug)
    # The focus phrases the lesson bolds, in order of first appearance: the
    # reader's sidebar is built from these, and they are the article's syllabus.
    data["focus"] = [pair.to_dict() for pair in entry.article.focus_pairs()]
    # Every form the deck knows, so the reader can mark words already saved.
    # A personal library is hundreds of entries, not millions; sending it once
    # per article is cheaper than a request per word.
    known = {w.get("lemma") or "" for w in deck} | {w.get("term") or "" for w in deck}
    data["known"] = sorted(term for term in known if term)

    # Start warming the words this article uses most and nothing can gloss yet,
    # so that by the time the reader clicks one it is already cached.
    warming = 0
    if application.warmer is not None:
        glossary = application.ensure_glossary()
        warming = application.warmer.warm(
            entry.article, glossary,
            lambda word, slug_: application.resolve_word(word, "", slug_),
        )
    data["warming"] = warming
    return data


class ExplainBody(BaseModel):
    text: str
    sentence: str = ""
    question: str | None = None


@app.post("/api/explain")
def explain(body: ExplainBody) -> dict[str, Any]:
    """A longer, on-demand grammar explanation of a passage."""
    application = _require_tutor()
    if not body.text.strip():
        raise HTTPException(400, "text is required")
    # An explanation is the reader saying the text alone was not enough, which
    # is the scaffolding signal this app is named after.
    application.store.log_scaffolding("explain", term=body.text.strip()[:60])
    key = cache_key("explain", body.text.strip().lower(), (body.question or "").strip().lower())
    data = application.cached(
        key,
        lambda: {
            "explanation": application.tutor.explain(
                text=body.text, sentence=body.sentence, question=body.question
            )
        },
    )
    return data or {"explanation": ""}


class TranslateBody(BaseModel):
    text: str
    target: str = "Spanish"


@app.post("/api/translate")
def translate(body: TranslateBody) -> dict[str, Any]:
    """Translate a passage the reader selected.

    In a diglot most sentences are already half in the target language, so this
    is mostly used the other way round -- reading a Spanish sentence and asking
    what the whole of it says.
    """
    application = _require_tutor()
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "text is required")
    if len(text) > 1200:
        text = text[:1200]
    target = "English" if body.target.lower().startswith("en") else "Spanish"
    application.store.log_scaffolding("translate", term=text[:60])
    key = cache_key("translate", target, text.lower())
    data = application.cached(
        key, lambda: {"translation": application.tutor.sentence_translation(sentence=text, target=target)}
    )
    return data or {"translation": ""}


@app.delete("/api/article/{slug}")
def delete_article(slug: str) -> dict[str, Any]:
    """Remove a lesson this app made, and what the reader accumulated on it.

    Only lessons the app wrote. A passage from the reader's own corpus folder is
    their file, and deleting it is not the app's business -- so this says where it
    is rather than doing it, which is also the only answer that cannot lose
    something someone typed by hand.

    What the reader *learned* here stays: saved words are their knowledge, and the
    article a word came from is a note in the margin rather than a parent. Only the
    reading position and completion go, because they are about a passage that is
    no longer there.
    """
    application = ctx()
    entry = application.library.get(slug)
    if entry is None:
        raise HTTPException(404, f"no article {slug}")
    if not entry.imported:
        raise HTTPException(
            409,
            f"“{entry.article.title}” is one of your own files, in "
            f"{application.settings.corpus_dir} — the app reads that folder and does not "
            "delete from it. Remove the file there and it will disappear from the library",
        )
    if entry.path is None:
        raise HTTPException(409, "this lesson has no file to delete")

    library_dir = application.settings.library_dir.resolve()
    path = entry.path.resolve()
    # Belt and braces: nothing outside the app's own folder is ever unlinked, however
    # the entry got here.
    if not path.is_relative_to(library_dir):
        raise HTTPException(409, f"refusing to delete {path}, which is outside the app's library folder")
    if application.library.passages_per_file().get(str(entry.path), 1) > 1:
        raise HTTPException(
            409, "this file holds more than one passage, so deleting it would take the others too")

    path.unlink(missing_ok=True)
    application.store.forget_article(slug)
    application.library.refresh()
    log.info("deleted lesson %s (%s)", slug, path)
    return {"slug": slug, "removed": str(path), "articles": len(application.library),
            "words_kept": application.store.word_count()}


class SettingsBody(BaseModel):
    """What the settings panel can set.

    Every field is optional, and an empty string means "clear it" -- going back to
    whatever the ``.env`` says rather than storing a blank key. That is how a reader
    undoes a typo, so the two cases have to be distinguishable here.
    """
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    typesafe_api_key: str | None = None
    typesafe_model: str | None = None


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    """What the app is configured with, and where each value came from.

    Never the key itself. The browser gets a hint -- three characters and the last
    four -- which is enough for a reader to recognise which key is set and useless
    to anyone who reads it.
    """
    application = ctx()
    settings = application.settings
    return {
        "llm": {
            "base_url": settings.llm_base_url,
            "model": settings.llm_model,
            "ready": settings.llm_ready,
            "key_set": bool(settings.llm_api_key),
            "key_hint": settings.key_hint(),
            "from": settings.llm_from,
        },
        "judge": {
            "ready": settings.judge_ready,
            "model": settings.typesafe_model,
            "key_set": bool(settings.typesafe_api_key),
            "from": "app" if "typesafe_api_key" in settings.saved() else
                    ("env" if settings.typesafe_api_key else None),
        },
        "env_file": str(settings.env_path) if settings.env_path else None,
        "settings_file": str(settings.settings_file),
    }


@app.post("/api/settings")
def save_settings(body: SettingsBody) -> dict[str, Any]:
    """Write the panel's values and put them to work immediately.

    The models are rebuilt on the spot rather than at the next start: someone who
    has just pasted a key wants to know whether it works, and the launcher they
    started the app with has no terminal to restart it from.
    """
    application = ctx()
    changes = {key: value for key, value in body.model_dump().items() if value is not None}
    application.settings.update_saved(changes)
    application.reload_models()
    return get_settings()


@app.post("/api/reset")
def reset_progress() -> dict[str, Any]:
    """Clear everything the reader has accumulated, keeping their lessons.

    The lessons are files, not rows, so the library is untouched: what goes is the
    deck, the schedule, the reviews, the reading positions, the kept sentences, the
    writings, the lookups and the history -- everything the app has *measured*.

    The database is copied first. This is the one button that can lose months of
    work, and a consistent copy costs a file and buys the reader a way back.
    """
    application = ctx()
    backup = application.store.backup(application.settings.data_dir / "backups")
    before = {
        "words": application.store.word_count(),
        "articles": len(application.library),
        "reviews": application.store.stats().get("reviews_total", 0),
    }
    application.store.reset()
    log.info("reset: cleared the reader's records (%s), backup at %s", before, backup)
    return {"cleared": before, "backup": str(backup),
            "kept": {"articles": len(application.library),
                     "library": str(application.settings.library_dir)}}


@app.post("/api/article/{slug}/register")
def set_register(slug: str, body: RegisterBody) -> dict[str, Any]:
    """Record what kind of writing a passage is.

    The register lives in the file rather than beside it. The front matter is the
    app's contract with itself -- "the file says how it was made" -- and a tag the
    reader sets in the app but cannot see in the file would be a second place for
    one fact to live, with a stale copy waiting to happen. So this writes exactly
    one line: the passage keeps every other byte, including the reader's own
    wording if they wrote it by hand.

    An empty code takes the tag back, and the app goes back to inferring one from
    the text and saying so -- which is a different claim from "this was declared",
    and the reader should be able to make either one.
    """
    application = ctx()
    entry = application.library.get(slug)
    if entry is None:
        raise HTTPException(404, f"no article {slug}")
    if entry.path is None:
        raise HTTPException(409, "this passage has no file to write to")
    # A compilation holds several passages and one front-matter block between them,
    # so a Register line in it belongs to one passage rather than to the file. The
    # parser reports passages without their line ranges, so the app cannot tell
    # which one the reader is looking at -- and guessing would quietly retag a
    # different lesson. Refusing is the only honest answer.
    if application.library.passages_per_file().get(str(entry.path), 1) > 1:
        raise HTTPException(
            409,
            "this file holds several passages, so a single tag in it would not be clear "
            "about which one you meant -- the tag lives in the file itself for a "
            "compilation like this",
        )

    code = (body.code or "").strip().lower()
    if code and code not in registers.CODES:
        raise HTTPException(400, f"unknown register {code!r}")

    source = entry.path.read_text(encoding="utf-8")
    updated = transfer.set_front_matter_field(source, "Register", code)
    if updated is None:
        raise HTTPException(
            409,
            "this file has no front matter for the tag to live in, and inventing one "
            "would rewrite the passage -- add a '- **Register:** ...' line under the "
            "identification block and it will be read",
        )
    if updated != source:
        entry.path.write_text(updated, encoding="utf-8")
        application.library.refresh()
        # The only place the app writes inside the reader's own corpus directory, so
        # it says so in the log: if a passage ever turns up retagged and nobody
        # remembers doing it, there is one line to look for.
        log.info("register %r written into %s", code, entry.path)

    fresh = application.library.get(slug)
    register, inferred = fresh.register() if fresh is not None else ("", True)
    return {"slug": slug, "register": register or None, "inferred": inferred,
            "blurb": registers.describe(register)}


@app.get("/api/registers")
def register_options() -> dict[str, Any]:
    """The taxonomy, for the control that lets a reader correct a tag."""
    return {"registers": registers.options()}


@app.post("/api/article/{slug}/progress")
def save_progress(slug: str, body: ProgressBody) -> dict[str, Any]:
    """Save reading position, and credit the words passed since the last save.

    The exposure accounting rides on this request rather than adding one. The
    reader already calls it every few seconds; the tokens between the old
    position and the new are known server-side, so the reading path gains
    nothing to do and the word-lookup endpoint is untouched.
    """
    application = ctx()
    entry = application.library.get(slug)
    credited = 0

    if entry is not None and body.position is not None:
        reached = min(max(body.position, 0.0), 1.0)
        already = application.store.credited_position(slug)
        # Only newly-covered ground, so scrolling back and forth does not
        # multiply the count -- but a word met again in a *different* article
        # still counts, which is what makes "repeatedly encountered" mean
        # something.
        if reached > already + 0.005:
            counted = tokens_between(entry.article, already, reached)
            if counted:
                # Stems, not terms: a reader who knows "volverse" knows "se
                # vuelve", and counting inflections as unknown would understate
                # comprehension on exactly the verbs worth crediting.
                known = vocab.known_stems(application.store.list_words(limit=5000))
                credited = application.store.record_exposure(
                    slug=slug, lemmas=dict(counted),
                    known_stems=known,
                    glossed=glossed_lemmas(entry.article),
                )
            application.store.mark_credited(slug, reached)

    application.store.save_progress(
        slug, position=body.position, seconds=body.seconds, completed=body.completed
    )
    if body.seconds:
        application.store.log_event("read", slug=slug, amount=body.seconds)
    return {"ok": True, "words_credited": credited}


# --------------------------------------------------------------------------- #
# Words
# --------------------------------------------------------------------------- #


@app.get("/api/word/lookup")
def lookup(term: str, sentence: str = "", slug: str | None = None,
           deep: bool = False) -> dict[str, Any]:
    """Look a word up.

    ``deep=true`` asks the tutor even when the corpus can already answer, which
    is how the reader gets the fuller entry -- lemma, part of speech, example,
    and the note on what learners get wrong -- for a word the article already
    translated.
    """
    application = ctx()
    term = term.strip()
    if not term:
        raise HTTPException(400, "term is required")

    data = application.resolve_word(term, sentence, slug, deep=deep)
    lemma = str(data.get("lemma") or term)
    application.store.log_lookup(
        term=term, lemma=lemma, gloss=data.get("gloss"),
        article_slug=slug, context=sentence or None,
    )
    data["looked_up_before"] = application.store.lookup_count(lemma)
    # Sentences the reader kept that use this word. It costs one indexed query on
    # a path that already touches the database, and it is what makes a collection
    # worth having while you are still reading: you meet *por lo tanto* again and
    # the app can show you the two sentences you chose it from.
    data["quotes"] = application.store.quotes_using(term)
    return data


# --------------------------------------------------------------------------- #
# Kept sentences
# --------------------------------------------------------------------------- #


class QuoteBody(BaseModel):
    slug: str
    block_index: int
    text: str


class QuoteNoteBody(BaseModel):
    note: str = ""


@app.post("/api/quote")
def save_quote(body: QuoteBody) -> dict[str, Any]:
    """Keep one sentence from an article.

    The browser sends the sentences it selected; the article's own parsed spans
    decide where the Spanish is, so a quote is cut to the same boundaries every
    other measure in the app uses. No model is called, which is the point -- this
    has to be free enough to do mid-sentence without breaking the reading.
    """
    application = ctx()
    entry = application.library.get(body.slug)
    if entry is None:
        raise HTTPException(404, f"no article {body.slug!r}")
    quote, reason = quote_pick(entry.article, body.block_index, body.text)
    if quote is None:
        raise HTTPException(400, reason)
    quote_id, created = application.store.save_quote(
        text=quote.text, es=quote.es, en=quote.en,
        article_slug=body.slug, block_index=quote.block_index,
        start=quote.start, end=quote.end, term=quote.term, glosses=quote.glosses,
    )
    return {"id": quote_id, "created": created, "quote": quote.to_dict(),
            "total": application.store.quote_count()}


@app.get("/api/quotes")
def list_quotes(q: str | None = None, slug: str | None = None) -> dict[str, Any]:
    """The reader's kept sentences, with enough to render them as they read."""
    application = ctx()
    rows = application.store.quotes(search=q, slug=slug)
    out = [_decorate(row, application) for row in rows]
    return {
        "quotes": out,
        "count": len(out),
        "total": application.store.quote_count(),
        "sources": _quote_sources(application),
    }


def _decorate(row: dict[str, Any], application: App) -> dict[str, Any]:
    """Add the article's title and the sentence's own spans to a stored quote.

    Derived rather than stored, and re-derived on every read: spans are a
    rendering of the article, and an article can be corrected or removed. A quote
    whose source has gone keeps its text and loses only its pretty markup.
    """
    entry = application.library.get(row.get("article_slug") or "")
    row["source_title"] = entry.article.title if entry else None
    row["spans"] = None
    if entry is not None and row.get("block_index") is not None:
        again, _reason = quote_pick(entry.article, row["block_index"], row["text"])
        if again is not None:
            row["spans"] = again.spans
            row["present"] = True
        else:
            row["present"] = False
    else:
        row["present"] = False
    return row


def _quote_sources(application: App) -> list[dict[str, Any]]:
    """Which articles the collection came from, biggest first."""
    counts: dict[str, int] = {}
    for row in application.store.quotes(limit=2000):
        slug = row.get("article_slug") or ""
        counts[slug] = counts.get(slug, 0) + 1
    out = []
    for slug, count in sorted(counts.items(), key=lambda pair: -pair[1]):
        entry = application.library.get(slug) if slug else None
        out.append({"slug": slug, "count": count,
                    "title": entry.article.title if entry else "(removed)"})
    return out


@app.patch("/api/quote/{quote_id}")
def annotate_quote(quote_id: int, body: QuoteNoteBody) -> dict[str, Any]:
    """The reader's own note on a sentence they kept."""
    if not ctx().store.set_quote_note(quote_id, body.note.strip()):
        raise HTTPException(404, f"no quote {quote_id}")
    return {"ok": True, "id": quote_id}


@app.delete("/api/quote/{quote_id}")
def delete_quote(quote_id: int) -> dict[str, Any]:
    application = ctx()
    if not application.store.delete_quote(quote_id):
        raise HTTPException(404, f"no quote {quote_id}")
    return {"ok": True, "total": application.store.quote_count()}


@app.get("/api/glossary")
def glossary() -> dict[str, Any]:
    """The whole local index, for the browser to answer clicks without a request.

    Small enough to ship (a few hundred entries), and it turns the common case
    -- clicking a word the article already glossed -- into no network at all.
    """
    application = ctx()
    entries = application.ensure_glossary().compact()
    return {"entries": entries, "count": len(application.ensure_glossary())}


@app.get("/api/lookups")
def lookups(minimum: int = 2) -> dict[str, Any]:
    """Words looked up repeatedly but never saved -- a study list built for free."""
    return {"words": ctx().store.frequent_lookups(minimum=minimum, skip=STOPWORDS)}


@app.post("/api/word/save")
def save_word(body: SaveWordBody) -> dict[str, Any]:
    application = ctx()
    word = application.store.add_word(
        term=body.term, gloss=body.gloss, lemma=body.lemma, pos=body.pos,
        note=body.note, article_slug=body.article_slug, context=body.context,
    )
    return {"word": word, "total": application.store.word_count()}


@app.get("/api/words")
def words(search: str | None = None) -> dict[str, Any]:
    """The whole deck, for the page that lists every saved card.

    The search folds accents, the way the quotes search does -- a reader who types
    "cuestion" expecting to find "cuestión" has been taught that by the rest of the
    app. That cannot be done in SQL without a second, unfolded notion of "the same
    word", so the matching happens here, over the deck rather than over the
    database. A personal deck is hundreds of rows; this is not a query path.

    It searches the sentence the word was met in, and the article it came from,
    as well as the word and its gloss: "which of my words came from that piece
    about handwriting" is a question worth being able to ask.
    """
    application = ctx()
    rows = application.store.list_words(limit=DECK_PAGE_LIMIT)
    titles = {entry.article.slug: entry.article.title for entry in application.library.all()}
    for row in rows:
        row["article_title"] = titles.get(row.get("article_slug") or "", "")

    query = vocab.fold((search or "").strip())
    if query:
        fields = ("term", "gloss", "lemma", "pos", "context", "article_title")
        rows = [row for row in rows
                if query in vocab.fold(" ".join(str(row.get(f) or "") for f in fields))]
    return {"words": rows, "total": application.store.word_count(),
            "limit": DECK_PAGE_LIMIT, "matched": len(rows)}


@app.delete("/api/word/{word_id}")
def delete_word(word_id: int) -> dict[str, Any]:
    ctx().store.delete_word(word_id)
    return {"ok": True}


@app.get("/api/export.csv")
def export_csv() -> StreamingResponse:
    """Anki-compatible export. Front is the Spanish, back the gloss plus the
    sentence it was met in, so the card tests recall in context."""
    rows = ctx().store.export_rows()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Spanish", "English", "Part of speech", "Context", "Article", "Interval (days)", "Ease"])
    for row in rows:
        writer.writerow([
            row["term"], row["gloss"] or "", row["pos"] or "", row["context"] or "",
            row["article_slug"] or "", f"{row['interval_days'] or 0:.1f}", f"{row['ease'] or 2.5:.2f}",
        ])
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="diglot-vocabulary.csv"'},
    )


# --------------------------------------------------------------------------- #
# Review
# --------------------------------------------------------------------------- #


@app.get("/api/review")
def review_queue(limit: int = 40, new: int = 12) -> dict[str, Any]:
    application = ctx()
    cards = application.store.due_cards(limit=limit, include_new=new)
    for card in cards:
        if card.get("context"):
            continue
        entry = application.library.get(card.get("article_slug") or "")
        if entry is not None:
            card["context"] = entry.article.context_for(card["term"])
    return {"cards": cards, "due": application.store.due_count()}


@app.post("/api/review/{card_id}")
def submit_review(card_id: int, body: ReviewBody) -> dict[str, Any]:
    application = ctx()
    try:
        return application.store.review_card(card_id, body.rating, elapsed_ms=body.elapsed_ms)
    except KeyError:
        raise HTTPException(404, f"no card {card_id}") from None


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    return ctx().store.stats()


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #


@app.get("/api/analytics/reading")
def reading_analytics(days: int = 7) -> dict[str, Any]:
    """Vocabulary exposure and scaffolding dependence over a window."""
    application = ctx()
    return analytics.reading_analytics(application.store, application.library,
                                       days=max(1, min(days, 365)))


@app.get("/api/analytics/corpus")
def corpus_analytics() -> dict[str, Any]:
    """The passage graph: what is related to what, and what is within reach."""
    application = ctx()
    return analytics.corpus_analytics(application.corpus_graph(), application.store,
                                      application.library)


class ScaffoldingBody(BaseModel):
    kind: str = Field(pattern="^(gloss-on|gloss-off|spanish-only|focus)$")
    slug: str | None = None


@app.post("/api/scaffolding")
def log_scaffolding(body: ScaffoldingBody) -> dict[str, Any]:
    """Record a reading-mode change.

    Sent with ``sendBeacon`` so it never blocks anything, and only when the
    reader actually changes a mode -- a handful of events per session. Turning
    the glosses back on after reading without them is the clearest signal of
    dependence the app can observe without asking.
    """
    ctx().store.log_scaffolding(body.kind, slug=body.slug)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Exercises
# --------------------------------------------------------------------------- #


def _require_tutor() -> App:
    application = ctx()
    if application.tutor is None:
        raise HTTPException(503, "the tutor is not configured; set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL")
    return application


@app.get("/api/article/{slug}/quiz")
def quiz(slug: str) -> dict[str, Any]:
    application = _require_tutor()
    entry = application.library.get(slug)
    if entry is None:
        raise HTTPException(404, f"no article {slug!r}")
    key = cache_key("quiz", content_fingerprint(entry.article))
    data = application.cached(key, lambda: {"questions": application.tutor.quiz(entry.article)})
    return {"slug": slug, "questions": (data or {}).get("questions", [])}


@app.get("/api/article/{slug}/drills")
def drills(slug: str) -> dict[str, Any]:
    application = _require_tutor()
    entry = application.library.get(slug)
    if entry is None:
        raise HTTPException(404, f"no article {slug!r}")
    key = cache_key("drills", content_fingerprint(entry.article))
    data = application.cached(key, lambda: {"drills": application.tutor.translation_drills(entry.article)})
    return {"slug": slug, "drills": (data or {}).get("drills", [])}


@app.post("/api/attempt")
def attempt(body: AttemptBody) -> dict[str, Any]:
    """Grade one exercise. Jev decides, the tutor explains."""
    application = ctx()
    answer = body.answer.strip()
    if not answer:
        raise HTTPException(400, "answer is required")

    verdict = None
    if body.kind == "translate":
        if not (body.source and body.reference):
            raise HTTPException(400, "translate needs source and reference")
        verdict = application.grading.grade_translation(
            source=body.source, reference=body.reference, attempt=answer
        )
    elif body.kind == "cloze":
        verdict = application.grading.grade_cloze(
            sentence=body.question, blank=body.reference or "", target=body.reference or "", attempt=answer
        )
    else:
        verdict = application.grading.grade_answer(
            question=body.question, reference=body.reference or "", attempt=answer
        )

    payload = verdict.to_dict()
    feedback: dict[str, Any] | None = None
    correct = is_correct(body.kind, payload)

    if application.tutor is not None and body.kind in ("translate", "comprehension"):
        feedback = application.tutor.translation_feedback(
            source=body.source or body.question,
            reference=body.reference or "",
            attempt=answer,
            verdict=payload,
        )

    if body.kind == "translate" or body.kind == "cloze":
        # Three states, not two. A comparison that matched is evidence and is
        # recorded as correct; one that did not match is only "incorrect" if the
        # tier had the standing to say so -- otherwise the fallback's uncertainty
        # would enter the permanent record as a mistake nobody made.
        if correct:
            outcome = "correct"
        elif may_fail(payload):
            outcome = "incorrect"
        else:
            outcome = "unverified"
        application.store.save_attempt(
            slug=body.slug, question=body.question or body.source or "", answer=answer,
            verdict=outcome, score=payload.get("score"),
            feedback=(feedback or {}).get("summary"),
        )

    return {"verdict": payload, "feedback": feedback, "correct": correct,
            "provisional": is_provisional(payload), "may_fail": may_fail(payload)}


@app.get("/api/article/{slug}/attempts")
def attempts(slug: str) -> dict[str, Any]:
    return {"attempts": ctx().store.attempts_for(slug)}


# --------------------------------------------------------------------------- #
# Import and discovery
# --------------------------------------------------------------------------- #


@app.post("/api/import")
def import_url(body: ImportBody) -> dict[str, Any]:
    """Queue a link -- or a paste -- to be woven into a lesson. Returns the job."""
    application = ctx()
    if application.tutor is None:
        raise HTTPException(503, "the tutor is not configured; set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL")
    url = (body.url or "").strip()
    text = (body.text or "").strip()
    if not url and not text:
        raise HTTPException(400, "give a link to fetch, or paste the article's text")
    if url and text:
        raise HTTPException(400, "give either a link or pasted text, not both")
    job = application.start_import(url or None, body.title_hint, body.level, body.ratio,
                                   body.weave, text=text or None,
                                   source_url=(body.source_url or "").strip() or None)
    return {"job_id": job.id, "status": job.status, "queued": application.jobs.active > 1}


@app.get("/api/import/options")
def import_options() -> dict[str, Any]:
    """The level and amount a lesson can be woven at, for the import dialog."""
    return levels.options()


class PreviewBody(BaseModel):
    level: str | None = None
    ratio: float | None = None
    weave: str | None = None
    # What is being woven, for the wording: "article", or "piece" when the
    # reader is looking at something they wrote themselves.
    subject: str | None = None


@app.post("/api/import/preview")
def import_preview(body: PreviewBody) -> dict[str, Any]:
    """A one-line description of what the current settings produce.

    Server-side rather than duplicated in the browser, so the wording the
    learner reads is the same wording the weaving prompt is built from.
    """
    chosen = resolve_level(body.level)
    grain = resolve_weave(body.weave)
    amount = clamp_ratio(body.ratio, chosen)
    return {"level": chosen.code, "ratio": amount, "weave": grain.code,
            "weave_name": grain.name,
            "description": levels.describe(chosen, amount, grain,
                                           what=(body.subject or "article").strip())}


# --------------------------------------------------------------------------- #
# Passing a lesson to someone
# --------------------------------------------------------------------------- #


@app.get("/api/transfer/export/{slug}")
def export_passage(slug: str, creator: str = "") -> Response:
    """One passage as a file to hand to someone.

    Stamped with who sent it and a digest of the lesson, and delivered as an
    attachment so the browser saves it rather than rendering it.
    """
    application = ctx()
    entry = application.library.get(slug)
    if entry is None:
        raise HTTPException(404, f"no article {slug!r}")
    text = transfer.export_text(entry, creator=creator.strip(), origin=_origin(application, entry))
    name = transfer.filename(entry.article)
    return Response(
        content=text,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def _origin(application: App, entry: LibraryEntry) -> str:
    """How *this lesson* came to exist, as one phrase for the stamp.

    Recorded because the recipient is being asked to trust a stranger's Spanish,
    and "hand-made" and "woven by a model" are different offers. It is a fact
    about the lesson, not about this server -- a hand-made article from the
    corpus is not "woven by" whatever model happens to be configured here.
    """
    if not entry.imported:
        return "hand-made"
    model = application.settings.llm_model if application.settings.llm_ready else ""
    return f"woven by {model}" if model else "woven by a model"


@app.post("/api/transfer/inspect")
def inspect_share(body: ShareBody) -> dict[str, Any]:
    """Describe a file someone sent, without importing it.

    The question this answers is not whether the file parses but whether the
    reader wants it: what it teaches, how much of that they already know, and
    whether they already have it.
    """
    application = ctx()
    return transfer.inspect(
        body.text,
        library=application.library,
        deck=application.store.list_words(limit=5000),
    )


@app.post("/api/transfer/import")
def import_share(body: ShareBody) -> dict[str, Any]:
    """Accept a shared lesson into the library.

    Synchronous, unlike a URL import: there is no fetching, no weaving and no
    model call, so a job queue would only add a spinner to something that takes
    a millisecond.
    """
    application = ctx()
    report = transfer.inspect(
        body.text,
        library=application.library,
        deck=application.store.list_words(limit=5000),
    )
    if not report["ok"]:
        raise HTTPException(400, report["error"])

    article, _share, _intact = transfer.verify(body.text)
    assert article is not None  # inspect() proved it parses
    slug = _free_slug(article, application, requested=body.slug)
    path = transfer.write(body.text, library_dir=application.settings.library_dir, slug=slug)
    application.library.refresh()
    entry = application.library.get(slug)
    if entry is not None:
        entry.path = path
    log.info("imported shared lesson %s (%d words)", slug, report["stats"]["words"])
    return {"slug": slug, "title": article.title, "report": report}


def _free_slug(article: Article, application: App, *, requested: str | None) -> str:
    """A filename not already taken.

    Never overwrites: importing the same lesson twice should give you two copies
    with distinguishable names, not silently replace the one you had -- which
    might be the one you had annotated by hand.
    """
    base = ingest.slugify(article.title, article.url or article.slug) if article.url \
        else re.sub(r"[^a-z0-9]+", "-", article.title.lower()).strip("-")[:48] or "shared"
    if requested:
        base = re.sub(r"[^a-z0-9-]+", "-", requested.lower()).strip("-") or base
    if application.library.get(base) is None and not (application.settings.library_dir / f"{base}.md").exists():
        return base
    for suffix in range(2, 100):
        candidate = f"{base}-{suffix}"
        if application.library.get(candidate) is None and \
                not (application.settings.library_dir / f"{candidate}.md").exists():
            return candidate
    return f"{base}-{content_fingerprint(article)[:8]}"


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #


@app.get("/api/jobs")
def jobs() -> dict[str, Any]:
    """Everything the Activity panel shows: running, queued, and recent history."""
    application = ctx()
    return {"jobs": application.jobs.snapshot(), "active": application.jobs.active}


@app.get("/api/jobs/stream")
def jobs_stream(request: Request) -> StreamingResponse:
    """Server-sent events, so the UI is told about a finished import rather
    than polling for it — which is what makes an out-of-the-box pop-up on
    success or failure possible wherever the reader happens to be."""
    application = ctx()
    return StreamingResponse(
        application.jobs.event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Nginx and friends buffer event streams by default, which would
            # hold every update until the connection closed.
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    job = ctx().jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"no job {job_id!r}")
    return job.to_dict()


@app.post("/api/jobs/{job_id}/cancel")
def job_cancel(job_id: str) -> dict[str, Any]:
    application = ctx()
    if application.jobs.get(job_id) is None:
        raise HTTPException(404, f"no job {job_id!r}")
    return {"cancelled": application.jobs.cancel(job_id)}


@app.delete("/api/jobs/{job_id}")
def job_dismiss(job_id: str) -> dict[str, Any]:
    application = ctx()
    if not application.jobs.dismiss(job_id):
        raise HTTPException(409, "that job is still running")
    return {"ok": True}


@app.delete("/api/jobs")
def job_clear() -> dict[str, Any]:
    ctx().jobs.clear_finished()
    return {"ok": True}


@app.post("/api/discover")
def discover(body: DiscoverBody) -> dict[str, Any]:
    """Search the web for readable articles on a topic.

    Results are annotated before they are shown: the register the *source*
    implies, when the domain says so on its own, and whether the reader already
    has the article. Both are facts the app can establish without fetching
    anything, and both change which result is worth clicking -- the first helps a
    reader deliberately looking for a register they have not read, the second
    stops them importing something they already own.
    """
    from .fetch import FetchError, search_web

    application = ctx()
    query = body.query.strip()
    if not query:
        raise HTTPException(400, "query is required")
    # One more than is shown, so "there is more" is measured rather than guessed
    # -- the reader is told when those sources have nothing further, instead of
    # being left clicking a button that does nothing. The pile asked for grows
    # with what has already been shown, because otherwise the flag would be
    # measuring only the first page's worth and would say "that is everything"
    # while the sources still had another page in them.
    try:
        found = search_web(query, proxy=application.settings.proxy,
                           limit=DISCOVER_PAGE + 1 + len(body.exclude),
                           exclude=set(body.exclude))
    except FetchError as exc:
        raise HTTPException(502, str(exc)) from None
    return {"query": query, "results": _annotate(found[:DISCOVER_PAGE], application),
            "more": len(found) > DISCOVER_PAGE}


def _annotate(results: list[dict[str, Any]], application: App) -> list[dict[str, Any]]:
    """Add what the app already knows about each search result."""
    from .registers import host_register, label

    have = {}
    for entry in application.library.all():
        url = (entry.article.url or "").strip().rstrip("/")
        if url:
            have.setdefault(url, entry.article.slug)
        # A woven lesson's front matter records where it came from; the article's
        # own url field is the same address, but older lessons may only carry the
        # title, so titles are matched too.
        have.setdefault(f"title:{(entry.article.title or '').strip().lower()}", entry.article.slug)

    out = []
    for item in results:
        row = dict(item)
        url = str(row.get("url", "")).strip().rstrip("/")
        row["already_have"] = have.get(url) or have.get(f"title:{str(row.get('title', '')).strip().lower()}")
        code = host_register(url)
        row["register"] = code
        row["register_label"] = label(code) if code else ""
        out.append(row)
    return out


@app.get("/api/discover/gaps")
def discovery_gaps() -> dict[str, Any]:
    """What the reader is not reading, and a way into it.

    Register is the point of a multi-register corpus, and the failure mode is
    quiet: someone reads eleven essays about art and never notices they have
    never read a news report. The library can see that, so it says so -- with the
    unread articles that are already on the shelf, and a topic drawn from what
    they *have* read, which is the difference between "read some news" and
    "read some news about AI".
    """
    application = ctx()
    return {"gaps": recommend_broaden(application.library.all(), application.store.all_progress())}


# --------------------------------------------------------------------------- #
# Writing practice
# --------------------------------------------------------------------------- #


class WritingBody(BaseModel):
    text: str
    prompt: str = ""
    words: list[str] = []


class DraftBody(WritingBody):
    # Absent for a new piece: the app names it from the opening words, and
    # asking someone to title their practice writing is friction they do not need.
    writing_id: int | None = None


class ImproveBody(WritingBody):
    mode: str = "grammar"


class LessonBody(WritingBody):
    """A piece of the reader's own writing, to be turned into a lesson.

    The text arrives here rather than being read back from the stored revision,
    because the box on screen is the authority on what they wrote: saving is what
    makes it a revision, and it happens as part of this call, so a lesson is never
    woven from text the reader cannot see in their version list.
    """
    writing_id: int | None = None
    title: str | None = None
    # The same three dials as an import. Omitted means the app estimates a level
    # and weaves to its own defaults, exactly as it would for a fetched article.
    level: str | None = None
    ratio: float | None = None
    weave: str | None = None


def _writing_prompt(application: App) -> writing.Prompt:
    """A prompt assembled from the reader's own material.

    The three sources are in priority order and each says something different.
    Words looked up repeatedly and never saved are the best thing to ask someone
    to *use* -- needing a word three times is a stronger signal than any
    self-assessment. Then words they saved recently, then the words from a
    sentence they chose to keep. The topic comes from their reading, the same
    title-derived subject the discovery engine uses.
    """
    read_titles: list[str] = []
    for slug, state in application.store.all_progress().items():
        entry = application.library.get(slug)
        if entry is not None and state.get("completed_at"):
            read_titles.append(entry.article.title)
    shaky = [row["term"] for row in application.store.frequent_lookups(
        minimum=2, limit=6, skip=STOPWORDS)]
    saved = [row["term"] for row in application.store.list_words(limit=6)]
    quoted = [row["term"] for row in application.store.quotes(limit=6) if row.get("term")]
    return writing.prompt_for(
        topics=topics_from_titles(read_titles) if read_titles else [],
        shaky=shaky, saved=saved, quotes=quoted,
    )


def _piece(application: App, row: dict[str, Any] | None) -> dict[str, Any] | None:
    """A piece as the UI wants it: named, and with a name for every revision.

    A piece with no stored title is one that predates titles or was migrated --
    it gets its name from how its first version opened, which is where a stored
    title would have come from anyway.
    """
    if row is None:
        return None
    if not row.get("title"):
        first = (row.get("revisions") or [{}])[0].get("text", "")
        row["title"] = writing.title_for(first)
    for index, revision in enumerate(row.get("revisions") or [], start=1):
        revision["label"] = f"Draft {index}"
    return row


@app.get("/api/writing")
def list_writings() -> dict[str, Any]:
    """The reader's pieces, newest first, and what to write next."""
    application = ctx()
    return {
        "writings": [_piece(application, row) for row in application.store.writings(limit=60)],
        "totals": application.store.writing_totals(),
        "prompt": _writing_prompt(application).to_dict(),
        "modes": writing.mode_options(),
    }


@app.get("/api/writing/prompt")
def writing_prompt() -> dict[str, Any]:
    """Something to write about, drawn from what the reader already has."""
    application = ctx()
    return {"prompt": _writing_prompt(application).to_dict(),
            "totals": application.store.writing_totals(),
            "modes": writing.mode_options()}


@app.get("/api/writing/{writing_id}")
def get_writing(writing_id: int) -> dict[str, Any]:
    """One piece and everything it has been through."""
    application = ctx()
    piece = _piece(application, application.store.writing(writing_id))
    if piece is None:
        raise HTTPException(404, f"no writing {writing_id}")
    return {"writing": piece, "totals": application.store.writing_totals(),
            "modes": writing.mode_options()}


@app.post("/api/writing/measure")
def measure_writing(body: WritingBody) -> dict[str, Any]:
    """Length and language mix, with no model and nothing written down.

    Called while the reader types, so it has to be free and it has to be exact --
    a language-mix figure that was approximated client-side would be a number
    about the wrong thing, and this one comes from the same segmenter the reader
    reads with.
    """
    prompt = writing.Prompt(topic="", words=list(body.words), instruction=body.prompt)
    reading = writing.read(body.text, prompt=prompt)
    return {"reading": reading.to_dict(), "note": writing.usage_note(reading)}


@app.post("/api/writing/check")
def check_writing(body: DraftBody) -> dict[str, Any]:
    """Read a draft back, and keep it as a version of the piece.

    Checking *saves*, because the feedback belongs to the exact text it judged --
    a note about a sentence that has since been edited is worse than no note, and
    a reading you cannot find again is not a reading. Checking the same text
    twice updates that version rather than adding another.
    """
    application = ctx()
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "nothing was written")
    prompt = writing.Prompt(topic="", words=list(body.words), instruction=body.prompt)
    reading = writing.read(text, prompt=prompt)

    verdict = application.grading.grade_writing(text=text, prompt=body.prompt, focus=body.words)
    key = cache_key("writing", writing.fingerprint(text), str(len(body.words)))
    feedback = _writing_feedback(application, key, text=text, prompt=body.prompt,
                                 reading=reading, verdict=verdict)
    # A feedback attempt that failed is not feedback. Keeping it would put "the
    # tutor could not be reached" in the record as though it were a note about
    # the writing.
    stored = {} if (feedback or {}).get("failed") else (feedback or {})

    piece = _open_piece(application, body, text)
    revision = application.store.add_revision(
        piece["id"], text=text, reading=reading.to_dict(),
        verdict=verdict.to_dict() if verdict.ok else {}, feedback=stored,
        words=reading.words, checked=bool(verdict.ok),
    )
    return {
        "writing_id": piece["id"],
        "revision": revision,
        "reading": reading.to_dict(),
        "note": writing.usage_note(reading),
        "verdict": verdict.to_dict(),
        "provisional": is_provisional(verdict.to_dict()),
        "feedback": feedback,
        "writing": _piece(application, application.store.writing(piece["id"])),
    }


def _writing_feedback(
    application: App, key: str, *, text: str, prompt: str, reading: Any, verdict: Any
) -> dict[str, Any] | None:
    """The tutor's reading of a draft, remembered only when it actually arrived.

    Deliberately not ``App.cached``, which stores whatever it is given. An
    outage is not an answer, and caching one would mean a reader never gets notes
    for that draft again -- the text is the cache key, so re-checking it would
    keep serving the failure. Re-asking costs one call; being permanently told
    "the tutor could not be reached" costs the feature.
    """
    cached = application.store.cache_get(key)
    if cached:
        remembered = cached.get("feedback") or {}
        if remembered and not remembered.get("failed"):
            return remembered
    if application.tutor is None or not verdict.ok:
        return None
    feedback = application.tutor.writing_feedback(
        text=text, prompt=prompt, reading=reading.to_dict(), verdict=verdict.to_dict())
    if not feedback.get("failed"):
        application.store.cache_put(key, {"feedback": feedback})
    return feedback


@app.post("/api/writing/draft")
def save_draft(body: DraftBody) -> dict[str, Any]:
    """Keep a version without having it read. The reader's own bookmark."""
    application = ctx()
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "nothing was written")
    prompt = writing.Prompt(topic="", words=list(body.words), instruction=body.prompt)
    reading = writing.read(text, prompt=prompt)
    piece = _open_piece(application, body, text)
    revision = application.store.add_revision(
        piece["id"], text=text, reading=reading.to_dict(), words=reading.words, checked=False)
    return {"writing_id": piece["id"], "revision": revision,
            "writing": _piece(application, application.store.writing(piece["id"])),
            "totals": application.store.writing_totals()}


def _open_piece(application: App, body: DraftBody, text: str) -> dict[str, Any]:
    """The piece this draft belongs to, started if it does not exist yet.

    The title is fixed here, when the piece is created, so revising the text does
    not rename the thing the reader has been working on.
    """
    if body.writing_id:
        piece = application.store.writing(body.writing_id)
        if piece is None:
            raise HTTPException(404, f"no writing {body.writing_id}")
        return piece
    return application.store.start_writing(title=writing.title_for(text), prompt=body.prompt)


@app.post("/api/writing/lesson")
def writing_lesson(body: LessonBody) -> dict[str, Any]:
    """Weave one of the reader's pieces into a lesson, whatever language it is in.

    The draft is saved first, as a revision, so the lesson is woven from a version
    that appears in the reader's own history. Otherwise the lesson would be the
    only record of some text they cannot see, and re-woven at a different amount
    it would quietly have nothing to refer back to.
    """
    application = ctx()
    if application.tutor is None:
        raise HTTPException(
            503, "the tutor is not configured; set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "nothing was written")
    prompt = writing.Prompt(topic="", words=list(body.words), instruction=body.prompt)
    reading = writing.read(text, prompt=prompt)
    piece = _open_piece(application, body, text)
    revision = application.store.add_revision(
        piece["id"], text=text, reading=reading.to_dict(), words=reading.words, checked=False)
    title = (body.title or piece.get("title") or writing.title_for(text)).strip()
    job = application.start_lesson_from_writing(text, title, body.level, body.ratio, body.weave)
    return {"job_id": job.id, "status": job.status, "writing_id": piece["id"],
            "revision": revision, "title": title}


@app.post("/api/writing/improve")
def improve_writing(body: ImproveBody) -> dict[str, Any]:
    """One suggested revision, shown beside the reader's own text.

    Generation, not judgment, so it needs the tutor and there is no fallback tier
    -- which is stated rather than papered over. Nothing is written down: a
    suggestion is not a version until the reader makes it one.
    """
    application = ctx()
    # Validate the request before checking what is configured: a malformed
    # request is malformed whatever happens to be installed, and answering
    # "no tutor" to a bad mode sends the reader off to fix the wrong thing.
    if not (body.text or "").strip():
        raise HTTPException(400, "nothing was written")
    if body.mode not in writing.MODE_IDS:
        raise HTTPException(400, f"unknown way of improving a draft: {body.mode!r}")
    if application.tutor is None:
        raise HTTPException(503, "improving a draft needs the tutor; set LLM_BASE_URL, "
                                 "LLM_API_KEY and LLM_MODEL")
    suggestion = application.tutor.improve_writing(
        text=body.text, mode=body.mode, prompt=body.prompt, focus=body.words)
    if suggestion.get("failed"):
        return {"suggestion": None, **suggestion}
    return {"suggestion": suggestion}


@app.delete("/api/writing/{writing_id}")
def delete_writing(writing_id: int) -> dict[str, Any]:
    application = ctx()
    if not application.store.delete_writing(writing_id):
        raise HTTPException(404, f"no writing {writing_id}")
    return {"ok": True, "totals": application.store.writing_totals()}


# --------------------------------------------------------------------------- #
# A goal for the week
# --------------------------------------------------------------------------- #


class ChallengeBody(BaseModel):
    kind: str
    target: int | None = None


def _challenge_context(application: App) -> challenges.Context:
    """What a challenge measurement is allowed to look at.

    The corpus graph is taken from the cache rather than built here. The
    challenge panel lives on the Progress page, which builds it anyway, so this
    is the same graph the reader is looking at -- and a goal should never be the
    reason the app does work it would not otherwise do.
    """
    graph = application.corpus_graph()
    cluster_of = {node["slug"]: node["cluster"] for node in graph.get("nodes", [])
                  if node.get("cluster") is not None and node["cluster"] >= 0}
    return challenges.Context(store=application.store, library=application.library,
                              cluster_of=cluster_of)


@app.get("/api/challenge")
def get_challenge() -> dict[str, Any]:
    """The active goal, its progress, and what would be suggested instead.

    Reaching the target is recorded here rather than in a write of its own,
    because this is the only place that computes it -- and it is idempotent: the
    first read that sees the goal met, and no other, marks it done. The goal then
    stays active until the reader starts another one, so "you did it" is visible
    rather than replaced by a fresh suggestion the moment it is earned.
    """
    application = ctx()
    context = _challenge_context(application)
    row = application.store.active_challenge()
    report = challenges.report(row, context)
    if report and not report["completed_at"] and report["done"] >= report["target"]:
        if application.store.complete_challenge(report["id"]):
            report["completed_at"] = report["started_at"]  # truthy; the exact time is not shown
            report["just_completed"] = True

    gaps = recommend_broaden(application.library.all(), application.store.all_progress())
    # A suggestion is offered whenever nothing is *running* -- so a goal that was
    # reached or whose week ran out is acknowledged and immediately followed by
    # something else, rather than leaving the panel empty with no way onward.
    running = report is not None and not report["completed_at"] and not report["expired"]
    return {
        "challenge": report,
        "suggested": None if running else challenges.propose(context, gaps),
        "kinds": challenges.available(context),
        "history": application.store.challenge_history(limit=6),
    }


@app.post("/api/challenge")
def start_challenge(body: ChallengeBody) -> dict[str, Any]:
    """Accept a goal, or set one of your own."""
    application = ctx()
    kind = challenges.BY_ID.get(body.kind)
    if kind is None:
        raise HTTPException(400, f"unknown challenge {body.kind!r}")
    target = body.target or kind.targets[0]
    if target < 1 or target > 100_000:
        raise HTTPException(400, "that target is not a number anyone could hit")
    application.store.start_challenge(kind.id, target)
    return get_challenge()


@app.delete("/api/challenge/{challenge_id}")
def stop_challenge(challenge_id: int) -> dict[str, Any]:
    """Give up on a goal. Kept in the history rather than deleted."""
    if not ctx().store.end_challenge(challenge_id):
        raise HTTPException(404, f"no active challenge {challenge_id}")
    return get_challenge()


@app.get("/api/suggest")
def suggest() -> dict[str, Any]:
    application = _require_tutor()
    progress = application.store.all_progress()
    read = [
        entry.article.title
        for slug, entry in ((s, application.library.get(s)) for s in progress)
        if entry is not None and progress[slug].get("completed_at")
    ]
    saved = [w["term"] for w in application.store.list_words(limit=60)]
    available = [e.article.title for e in application.library.all()]
    return {"suggestion": application.tutor.suggest_next(read=read, saved=saved, library=available)}


@app.get("/api/vocabulary-index")
def vocabulary_index() -> dict[str, Any]:
    """Which of the learner's saved words appear in which library articles.

    The cheapest possible answer to "what should I read next": no network, no
    model, just the focus vocabulary of every article intersected with the deck.
    """
    application = ctx()
    index = application.library.vocabulary_index()
    known = application.store.known_terms()
    out: dict[str, list[dict[str, str]]] = {}
    for key, record in index.items():
        if key not in known:
            continue
        for article in record["articles"]:
            out.setdefault(article["slug"], []).append({"term": record["term"], "gloss": record.get("gloss") or ""})
    return {"by_article": out}


@app.post("/api/recommend")
def start_recommend() -> dict[str, Any]:
    """Start looking for the next article by vocabulary overlap."""
    application = ctx()
    if application.tutor is None:
        raise HTTPException(503, "the tutor is not configured; set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL")
    return {"job_id": application.start_recommend(), "status": "running"}


# --------------------------------------------------------------------------- #
# Static
# --------------------------------------------------------------------------- #


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.exception_handler(LLMError)
def llm_error_handler(_request, exc: LLMError) -> JSONResponse:  # pragma: no cover
    return JSONResponse({"detail": str(exc)}, status_code=502)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
