"""Library, article reading, progress, analytics, scaffolding, stats, and challenge routes."""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException

from ... import analytics, challenges, registers, transfer, vocab
from ...glossary import STOPWORDS
from ...reading import glossed_lemmas, tokens_between
from ...recommend import broaden as recommend_broaden
from ...runtime import App, DECK_PAGE_LIMIT
from ..deps import get_app, require_article
from ..schemas import ChallengeBody, ProgressBody, RegisterBody, ScaffoldingBody

log = logging.getLogger("diglot")
router = APIRouter()


@router.get("/api/library")
def library() -> dict[str, Any]:
    application = get_app()
    deck = application.store.list_words(limit=DECK_PAGE_LIMIT)
    progress = application.store.all_progress()
    return {
        "articles": application.library.catalogue(deck, progress),
        "words": application.store.word_count(),
        "due": application.store.due_count(),
        "worth_saving": application.store.frequent_lookups(minimum=2, limit=8, skip=STOPWORDS),
    }


@router.get("/api/article/{slug}")
def article(slug: str) -> dict[str, Any]:
    application = get_app()
    entry = require_article(application, slug)
    deck = application.store.list_words(limit=DECK_PAGE_LIMIT)
    data = entry.to_dict(with_blocks=True)
    data["coverage"] = application.library.coverage(entry.article, deck)
    data["register_editable"] = application.library.passages_per_file().get(str(entry.path or ""), 1) == 1
    data["progress"] = application.store.progress_for(slug)
    data["focus"] = [pair.to_dict() for pair in entry.article.focus_pairs()]
    known = {w.get("lemma") or "" for w in deck} | {w.get("term") or "" for w in deck}
    data["known"] = sorted(term for term in known if term)

    warming = 0
    warmer = application.warmer
    if warmer is not None:
        glossary = application.ensure_glossary()
        warming = warmer.warm(
            entry.article, glossary,
            lambda word, slug_: application.resolve_word(word, "", slug_, from_warmer=True),
        )
    data["warming"] = warming
    return data


@router.delete("/api/article/{slug}")
def delete_article(slug: str) -> dict[str, Any]:
    """Remove a lesson this app made, and what the reader accumulated on it."""
    application = get_app()
    entry = application.library.get(slug)
    if entry is None:
        raise HTTPException(404, f"no article {slug}")
    if not entry.imported:
        raise HTTPException(
            409,
            f"“{entry.article.title}” is one of your own files, in "
            f"{application.settings.corpus_dir}. The app reads that folder and does not "
            "delete from it. Remove the file there and it will disappear from the library.",
        )
    if entry.path is None:
        raise HTTPException(409, "this lesson has no file to delete")

    library_dir = application.settings.library_dir.resolve()
    path = entry.path.resolve()
    if not path.is_relative_to(library_dir):
        raise HTTPException(409, f"refusing to delete {path}, which is outside the app's library folder")
    if application.library.passages_per_file().get(str(entry.path), 1) > 1:
        raise HTTPException(
            409, "this file holds more than one passage, so deleting it would take the others too"
        )

    path.unlink(missing_ok=True)
    application.store.forget_article(slug)
    application.library.refresh()
    log.info("deleted lesson %s (%s)", slug, path)
    return {
        "slug": slug,
        "removed": str(path),
        "articles": len(application.library),
        "words_kept": application.store.word_count(),
    }


@router.post("/api/article/{slug}/register")
def set_register(slug: str, body: RegisterBody) -> dict[str, Any]:
    """Record what kind of writing a passage is."""
    application = get_app()
    entry = application.library.get(slug)
    if entry is None:
        raise HTTPException(404, f"no article {slug}")
    if entry.path is None:
        raise HTTPException(409, "this passage has no file to write to")
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
        # Atomic replace so a crash mid-write never truncates a hand-made file.
        tmp = entry.path.with_suffix(entry.path.suffix + ".tmp")
        tmp.write_text(updated, encoding="utf-8")
        os.replace(tmp, entry.path)
        application.library.refresh()
        log.info("register %r written into %s", code, entry.path)

    fresh = application.library.get(slug)
    register, inferred = fresh.register() if fresh is not None else ("", True)
    return {
        "slug": slug,
        "register": register or None,
        "inferred": inferred,
        "blurb": registers.describe(register),
    }


@router.post("/api/article/{slug}/progress")
def save_progress(slug: str, body: ProgressBody) -> dict[str, Any]:
    """Save reading position, and credit the words passed since the last save."""
    application = get_app()
    entry = application.library.get(slug)
    credited = 0
    seconds = max(0, int(body.seconds or 0))

    if entry is not None and body.position is not None:
        reached = min(max(body.position, 0.0), 1.0)
        already = application.store.credited_position(slug)
        if reached > already + 0.005:
            counted = tokens_between(entry.article, already, reached)
            if counted:
                known = vocab.known_stems(application.store.list_words(limit=5000))
                credited = application.store.record_exposure(
                    slug=slug,
                    lemmas=dict(counted),
                    known_stems=known,
                    glossed=glossed_lemmas(entry.article),
                )
            application.store.mark_credited(slug, reached)

    application.store.save_progress(
        slug, position=body.position, seconds=seconds, completed=body.completed
    )
    if seconds:
        application.store.log_event("read", slug=slug, amount=seconds)
    return {"ok": True, "words_credited": credited}


@router.get("/api/stats")
def stats() -> dict[str, Any]:
    return get_app().store.stats()


@router.get("/api/analytics/reading")
def reading_analytics(days: int = 7) -> dict[str, Any]:
    application = get_app()
    return analytics.reading_analytics(
        application.store, application.library, days=max(1, min(days, 365))
    )


@router.get("/api/analytics/corpus")
def corpus_analytics() -> dict[str, Any]:
    application = get_app()
    return analytics.corpus_analytics(
        application.corpus_graph(), application.store, application.library
    )


@router.post("/api/scaffolding")
def log_scaffolding(body: ScaffoldingBody) -> dict[str, Any]:
    get_app().store.log_scaffolding(body.kind, slug=body.slug)
    return {"ok": True}


# -- challenges ----------------------------------------------------------- #


def _challenge_context(application: App) -> challenges.Context:
    graph = application.corpus_graph()
    cluster_of = {
        node["slug"]: node["cluster"]
        for node in graph.get("nodes", [])
        if node.get("cluster") is not None and node["cluster"] >= 0
    }
    return challenges.Context(
        store=application.store, library=application.library, cluster_of=cluster_of
    )


@router.get("/api/challenge")
def get_challenge() -> dict[str, Any]:
    application = get_app()
    context = _challenge_context(application)
    row = application.store.active_challenge()
    report = challenges.report(row, context)
    if report and not report["completed_at"] and report["done"] >= report["target"]:
        if application.store.complete_challenge(report["id"]):
            report["completed_at"] = report["started_at"]
            report["just_completed"] = True

    gaps = recommend_broaden(application.library.all(), application.store.all_progress())
    running = report is not None and not report["completed_at"] and not report["expired"]
    return {
        "challenge": report,
        "suggested": None if running else challenges.propose(context, gaps),
        "kinds": challenges.available(context),
        "history": application.store.challenge_history(limit=6),
    }


@router.post("/api/challenge")
def start_challenge(body: ChallengeBody) -> dict[str, Any]:
    application = get_app()
    kind = challenges.BY_ID.get(body.kind)
    if kind is None:
        raise HTTPException(400, f"unknown challenge {body.kind!r}")
    target = body.target or kind.targets[0]
    if target < 1 or target > 100_000:
        raise HTTPException(400, "that target is not a number anyone could hit")
    application.store.start_challenge(kind.id, target)
    return get_challenge()


@router.delete("/api/challenge/{challenge_id}")
def stop_challenge(challenge_id: int) -> dict[str, Any]:
    if not get_app().store.end_challenge(challenge_id):
        raise HTTPException(404, f"no active challenge {challenge_id}")
    return get_challenge()
