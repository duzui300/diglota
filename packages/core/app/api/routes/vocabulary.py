"""Vocabulary deck, lookups, glossary, CSV export, and SRS review routes."""

from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ... import vocab
from ...glossary import STOPWORDS
from ...runtime import DECK_PAGE_LIMIT
from ..deps import get_app
from ..schemas import ReviewBody, SaveWordBody

router = APIRouter()

_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: Any) -> str:
    """Neutralize spreadsheet formula prefixes (CSV injection defense)."""
    text = str(value or "")
    if text and text[0] in _CSV_FORMULA_PREFIXES:
        return "'" + text
    return text


@router.get("/api/word/lookup")
def lookup(
    term: str,
    sentence: str = "",
    slug: str | None = None,
    deep: bool = False,
    fresh: bool = False,
) -> dict[str, Any]:
    application = get_app()
    term = term.strip()
    if not term:
        raise HTTPException(400, "term is required")

    data = application.resolve_word(term, sentence, slug, deep=deep, fresh=fresh)
    lemma = str(data.get("lemma") or term)
    application.store.log_lookup(
        term=term, lemma=lemma, gloss=data.get("gloss"),
        article_slug=slug, context=sentence or None,
    )
    data["looked_up_before"] = application.store.lookup_count(lemma)
    data["quotes"] = application.store.quotes_using(term)
    return data


@router.get("/api/glossary")
def glossary() -> dict[str, Any]:
    application = get_app()
    index = application.ensure_glossary()
    return {"entries": index.compact(), "count": len(index)}


@router.get("/api/lookups")
def lookups(minimum: int = 2) -> dict[str, Any]:
    return {"words": get_app().store.frequent_lookups(minimum=minimum, skip=STOPWORDS)}


@router.post("/api/word/save")
def save_word(body: SaveWordBody) -> dict[str, Any]:
    application = get_app()
    word = application.store.add_word(
        term=body.term, gloss=body.gloss, lemma=body.lemma, pos=body.pos,
        note=body.note, article_slug=body.article_slug, context=body.context,
    )
    return {"word": word, "total": application.store.word_count()}


@router.get("/api/words")
def words(search: str | None = None) -> dict[str, Any]:
    application = get_app()
    rows = application.store.list_words(limit=DECK_PAGE_LIMIT)
    titles = {entry.article.slug: entry.article.title for entry in application.library.all()}
    for row in rows:
        row["article_title"] = titles.get(row.get("article_slug") or "", "")

    query = vocab.fold((search or "").strip())
    if query:
        fields = ("term", "gloss", "lemma", "pos", "context", "article_title")
        rows = [
            row for row in rows
            if query in vocab.fold(" ".join(str(row.get(f) or "") for f in fields))
        ]
    return {
        "words": rows,
        "total": application.store.word_count(),
        "limit": DECK_PAGE_LIMIT,
        "matched": len(rows),
    }


@router.delete("/api/word/{word_id}")
def delete_word(word_id: int) -> dict[str, Any]:
    get_app().store.delete_word(word_id)
    return {"ok": True}


@router.get("/api/export.csv")
def export_csv() -> StreamingResponse:
    application = get_app()
    pair = application.active_pair
    rows = application.store.export_rows()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        pair.target.name, pair.base.name, "Part of speech",
        "Context", "Article", "Interval (days)", "Ease",
    ])
    for row in rows:
        writer.writerow([
            _csv_safe(row["term"]),
            _csv_safe(row["gloss"] or ""),
            _csv_safe(row["pos"] or ""),
            _csv_safe(row["context"] or ""),
            _csv_safe(row["article_slug"] or ""),
            f"{row['interval_days'] or 0:.1f}",
            f"{row['ease'] or 2.5:.2f}",
        ])
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="diglot-vocabulary.csv"'},
    )


@router.get("/api/vocabulary-index")
def vocabulary_index() -> dict[str, Any]:
    application = get_app()
    index = application.library.vocabulary_index()
    known = application.store.known_terms()
    out: dict[str, list[dict[str, str]]] = {}
    for key, record in index.items():
        if key not in known:
            continue
        for article_ref in record["articles"]:
            out.setdefault(article_ref["slug"], []).append(
                {"term": record["term"], "gloss": record.get("gloss") or ""}
            )
    return {"by_article": out}


@router.get("/api/review")
def review_queue(limit: int = 40, new: int = 12) -> dict[str, Any]:
    application = get_app()
    cards = application.store.due_cards(limit=limit, include_new=new)
    for card in cards:
        if card.get("context"):
            continue
        entry = application.library.get(card.get("article_slug") or "")
        if entry is not None:
            card["context"] = entry.article.context_for(card["term"])
    return {"cards": cards, "due": application.store.due_count()}


@router.post("/api/review/{card_id}")
def submit_review(card_id: int, body: ReviewBody) -> dict[str, Any]:
    application = get_app()
    try:
        return application.store.review_card(card_id, body.rating, elapsed_ms=body.elapsed_ms)
    except KeyError:
        raise HTTPException(404, f"no card {card_id}") from None
