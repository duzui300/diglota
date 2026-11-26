"""Import, discovery, recommendation, and lesson transfer/sharing routes."""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from ... import ingest, levels, transfer
from ...diglot import Article, content_fingerprint
from ...levels import clamp_ratio, resolve_level, resolve_weave
from ...library import LibraryEntry
from ...recommend import broaden as recommend_broaden
from ...runtime import App, DISCOVER_PAGE
from ..deps import get_app, require_article, require_tutor, require_weave_tutor
from ..schemas import DiscoverBody, ImportBody, PreviewBody, ShareBody

log = logging.getLogger("diglot")
router = APIRouter()


@router.post("/api/import")
def import_url(body: ImportBody) -> dict[str, Any]:
    """Queue a link -- or a paste -- to be woven into a lesson. Returns the job."""
    application = require_weave_tutor()
    url = (body.url or "").strip()
    text = (body.text or "").strip()
    if not url and not text:
        raise HTTPException(400, "give a link to fetch, or paste the article's text")
    if url and text:
        raise HTTPException(400, "give either a link or pasted text, not both")
    job = application.start_import(
        url or None, body.title_hint, body.level, body.ratio, body.weave,
        text=text or None, source_url=(body.source_url or "").strip() or None,
    )
    return {"job_id": job.id, "status": job.status, "queued": application.jobs.active > 1}


@router.get("/api/import/options")
def import_options() -> dict[str, Any]:
    return levels.options()


@router.post("/api/import/preview")
def import_preview(body: PreviewBody) -> dict[str, Any]:
    chosen = resolve_level(body.level)
    grain = resolve_weave(body.weave)
    amount = clamp_ratio(body.ratio, chosen)
    return {
        "level": chosen.code,
        "ratio": amount,
        "weave": grain.code,
        "weave_name": grain.name,
        "description": levels.describe(
            chosen, amount, grain, what=(body.subject or "article").strip()
        ),
    }


# -- transfer / sharing --------------------------------------------------- #


def _origin(application: App, entry: LibraryEntry) -> str:
    if not entry.imported:
        return "hand-made"
    weave = application.settings.resolve_llm("weave")
    model = weave.model if weave.ready else ""
    return f"woven by {model}" if model else "woven by a model"


def _free_slug(article: Article, application: App, *, requested: str | None) -> str:
    base = (
        ingest.slugify(article.title, article.url or article.slug)
        if article.url
        else re.sub(r"[^a-z0-9]+", "-", article.title.lower()).strip("-")[:48] or "shared"
    )
    if requested:
        base = re.sub(r"[^a-z0-9-]+", "-", requested.lower()).strip("-") or base
    if application.library.get(base) is None and not (application.settings.library_dir / f"{base}.md").exists():
        return base
    for suffix in range(2, 100):
        candidate = f"{base}-{suffix}"
        if (
            application.library.get(candidate) is None
            and not (application.settings.library_dir / f"{candidate}.md").exists()
        ):
            return candidate
    return f"{base}-{content_fingerprint(article)[:8]}"


@router.get("/api/transfer/export/{slug}")
def export_passage(slug: str, creator: str = "") -> Response:
    application = get_app()
    entry = require_article(application, slug)
    text = transfer.export_text(entry, creator=creator.strip(), origin=_origin(application, entry))
    name = transfer.filename(entry.article)
    return Response(
        content=text,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/api/transfer/inspect")
def inspect_share(body: ShareBody) -> dict[str, Any]:
    application = get_app()
    return transfer.inspect(
        body.text, library=application.library, deck=application.store.list_words(limit=5000)
    )


@router.post("/api/transfer/import")
def import_share(body: ShareBody) -> dict[str, Any]:
    application = get_app()
    report = transfer.inspect(
        body.text, library=application.library, deck=application.store.list_words(limit=5000)
    )
    if not report["ok"]:
        raise HTTPException(400, report["error"])

    article, _share, _intact = transfer.verify(body.text)
    assert article is not None
    slug = _free_slug(article, application, requested=body.slug)
    path = transfer.write(body.text, library_dir=application.settings.library_dir, slug=slug)
    application.library.refresh()
    entry = application.library.get(slug)
    if entry is not None:
        entry.path = path
    log.info("imported shared lesson %s (%d words)", slug, report["stats"]["words"])
    return {"slug": slug, "title": article.title, "report": report}


# -- discovery & recommendations ----------------------------------------- #


def _annotate(results: list[dict[str, Any]], application: App) -> list[dict[str, Any]]:
    """Add what the app already knows about each search result."""
    from ...registers import host_register, label

    have: dict[str, str] = {}
    for entry in application.library.all():
        url = (entry.article.url or "").strip().rstrip("/")
        if url:
            have.setdefault(url, entry.article.slug)
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


@router.post("/api/discover")
def discover(body: DiscoverBody) -> dict[str, Any]:
    from ...fetch import FetchError, search_web

    application = get_app()
    query = body.query.strip()
    if not query:
        raise HTTPException(400, "query is required")
    try:
        found = search_web(
            query,
            proxy=application.settings.proxy,
            limit=DISCOVER_PAGE + 1 + len(body.exclude),
            exclude=set(body.exclude),
        )
    except FetchError as exc:
        raise HTTPException(502, str(exc)) from None
    return {
        "query": query,
        "results": _annotate(found[:DISCOVER_PAGE], application),
        "more": len(found) > DISCOVER_PAGE,
    }


@router.get("/api/discover/gaps")
def discovery_gaps() -> dict[str, Any]:
    application = get_app()
    return {"gaps": recommend_broaden(application.library.all(), application.store.all_progress())}


@router.get("/api/suggest")
def suggest() -> dict[str, Any]:
    application = require_tutor()
    progress = application.store.all_progress()
    read = [
        entry.article.title
        for slug, entry in ((s, application.library.get(s)) for s in progress)
        if entry is not None and progress[slug].get("completed_at")
    ]
    saved = [w["term"] for w in application.store.list_words(limit=60)]
    available = [e.article.title for e in application.library.all()]
    return {"suggestion": application.tutor.suggest_next(read=read, saved=saved, library=available)}


@router.post("/api/recommend")
def start_recommend() -> dict[str, Any]:
    """Start looking for the next article by vocabulary overlap."""
    application = require_tutor()
    job = application.start_recommend()
    return {"job_id": job.id, "status": job.status}
