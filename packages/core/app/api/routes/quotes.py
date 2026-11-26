"""Kept sentences (quotes) routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ...quotes import locate as quote_pick
from ...runtime import App
from ..deps import get_app, require_article
from ..schemas import QuoteBody, QuoteNoteBody

router = APIRouter()


@router.post("/api/quote")
def save_quote(body: QuoteBody) -> dict[str, Any]:
    application = get_app()
    entry = require_article(application, body.slug)
    quote, reason = quote_pick(entry.article, body.block_index, body.text)
    if quote is None:
        raise HTTPException(400, reason)
    quote_id, created = application.store.save_quote(
        text=quote.text, es=quote.es, en=quote.en,
        article_slug=body.slug, block_index=quote.block_index,
        start=quote.start, end=quote.end, term=quote.term, glosses=quote.glosses,
        language_pair=getattr(entry.article, "language_pair", "en-es"),
    )
    return {
        "id": quote_id,
        "created": created,
        "quote": quote.to_dict(),
        "total": application.store.quote_count(),
    }


@router.get("/api/quotes")
def list_quotes(q: str | None = None, slug: str | None = None) -> dict[str, Any]:
    application = get_app()
    rows = application.store.quotes(search=q, slug=slug)
    out = [_decorate(row, application) for row in rows]
    return {
        "quotes": out,
        "count": len(out),
        "total": application.store.quote_count(),
        "sources": _quote_sources(application),
    }


def _decorate(row: dict[str, Any], application: App) -> dict[str, Any]:
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
    counts: dict[str, int] = {}
    for row in application.store.quotes(limit=2000):
        slug = row.get("article_slug") or ""
        counts[slug] = counts.get(slug, 0) + 1
    out = []
    for slug, count in sorted(counts.items(), key=lambda pair: -pair[1]):
        entry = application.library.get(slug) if slug else None
        out.append({
            "slug": slug,
            "count": count,
            "title": entry.article.title if entry else "(removed)",
        })
    return out


@router.patch("/api/quote/{quote_id}")
def annotate_quote(quote_id: int, body: QuoteNoteBody) -> dict[str, Any]:
    if not get_app().store.set_quote_note(quote_id, body.note.strip()):
        raise HTTPException(404, f"no quote {quote_id}")
    return {"ok": True, "id": quote_id}


@router.delete("/api/quote/{quote_id}")
def delete_quote(quote_id: int) -> dict[str, Any]:
    application = get_app()
    if not application.store.delete_quote(quote_id):
        raise HTTPException(404, f"no quote {quote_id}")
    return {"ok": True, "total": application.store.quote_count()}
