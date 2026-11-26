"""Writing workspace routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ... import writing
from ...glossary import STOPWORDS
from ...grading import is_provisional
from ...recommend import topics_from_titles
from ...runtime import App
from ...tutor import cache_key
from ..deps import get_app
from ..schemas import DraftBody, ImproveBody, LessonBody, WritingBody

router = APIRouter()


def _writing_prompt(application: App) -> writing.Prompt:
    read_titles: list[str] = []
    for slug, state in application.store.all_progress().items():
        entry = application.library.get(slug)
        if entry is not None and state.get("completed_at"):
            read_titles.append(entry.article.title)
    shaky = [
        row["term"]
        for row in application.store.frequent_lookups(minimum=2, limit=6, skip=STOPWORDS)
    ]
    saved = [row["term"] for row in application.store.list_words(limit=6)]
    quoted = [row["term"] for row in application.store.quotes(limit=6) if row.get("term")]
    return writing.prompt_for(
        topics=topics_from_titles(read_titles) if read_titles else [],
        shaky=shaky,
        saved=saved,
        quotes=quoted,
    )


def _piece(application: App, row: dict[str, Any] | None) -> dict[str, Any] | None:
    del application
    if row is None:
        return None
    if not row.get("title"):
        first = (row.get("revisions") or [{}])[0].get("text", "")
        row["title"] = writing.title_for(first)
    for index, revision in enumerate(row.get("revisions") or [], start=1):
        revision["label"] = f"Draft {index}"
    return row


def _read_body(body: WritingBody, text: str) -> writing.Reading:
    prompt = writing.Prompt(topic="", words=list(body.words), instruction=body.prompt)
    return writing.read(text, prompt=prompt)


def _open_piece(application: App, body: DraftBody | LessonBody, text: str) -> dict[str, Any]:
    if body.writing_id:
        piece = application.store.writing(body.writing_id)
        if piece is None:
            raise HTTPException(404, f"no writing {body.writing_id}")
        return piece
    return application.store.start_writing(title=writing.title_for(text), prompt=body.prompt)


def _writing_feedback(
    application: App, key: str, *, text: str, prompt: str, reading: Any, verdict: Any
) -> dict[str, Any] | None:
    cached = application.store.cache_get(key)
    if cached:
        remembered = cached.get("feedback") or {}
        if remembered and not remembered.get("failed"):
            return remembered
    if application.tutor is None or not verdict.ok:
        return None
    feedback = application.tutor.writing_feedback(
        text=text, prompt=prompt, reading=reading.to_dict(), verdict=verdict.to_dict()
    )
    if not feedback.get("failed"):
        application.store.cache_put(key, {"feedback": feedback})
    return feedback


@router.get("/api/writing")
def list_writings() -> dict[str, Any]:
    application = get_app()
    return {
        "writings": [_piece(application, row) for row in application.store.writings(limit=60)],
        "totals": application.store.writing_totals(),
        "prompt": _writing_prompt(application).to_dict(),
        "modes": writing.mode_options(),
    }


@router.get("/api/writing/prompt")
def writing_prompt() -> dict[str, Any]:
    application = get_app()
    return {
        "prompt": _writing_prompt(application).to_dict(),
        "totals": application.store.writing_totals(),
        "modes": writing.mode_options(),
    }


@router.get("/api/writing/{writing_id}")
def get_writing(writing_id: int) -> dict[str, Any]:
    application = get_app()
    piece = _piece(application, application.store.writing(writing_id))
    if piece is None:
        raise HTTPException(404, f"no writing {writing_id}")
    return {
        "writing": piece,
        "totals": application.store.writing_totals(),
        "modes": writing.mode_options(),
    }


@router.post("/api/writing/measure")
def measure_writing(body: WritingBody) -> dict[str, Any]:
    reading = _read_body(body, body.text)
    return {"reading": reading.to_dict(), "note": writing.usage_note(reading)}


@router.post("/api/writing/check")
def check_writing(body: DraftBody) -> dict[str, Any]:
    application = get_app()
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "nothing was written")
    reading = _read_body(body, text)

    verdict = application.grading.grade_writing(text=text, prompt=body.prompt, focus=body.words)
    key = cache_key("writing", writing.fingerprint(text), str(len(body.words)))
    feedback = _writing_feedback(
        application, key, text=text, prompt=body.prompt, reading=reading, verdict=verdict
    )
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


@router.post("/api/writing/draft")
def save_draft(body: DraftBody) -> dict[str, Any]:
    application = get_app()
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "nothing was written")
    reading = _read_body(body, text)
    piece = _open_piece(application, body, text)
    revision = application.store.add_revision(
        piece["id"], text=text, reading=reading.to_dict(), words=reading.words, checked=False
    )
    return {
        "writing_id": piece["id"],
        "revision": revision,
        "writing": _piece(application, application.store.writing(piece["id"])),
        "totals": application.store.writing_totals(),
    }


@router.post("/api/writing/lesson")
def writing_lesson(body: LessonBody) -> dict[str, Any]:
    application = get_app()
    if application.weave_tutor is None:
        raise HTTPException(
            503, "the passage weave model is not configured; set a default or weave model in Settings"
        )
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "nothing was written")
    reading = _read_body(body, text)
    piece = _open_piece(application, body, text)
    revision = application.store.add_revision(
        piece["id"], text=text, reading=reading.to_dict(), words=reading.words, checked=False
    )
    title = (body.title or piece.get("title") or writing.title_for(text)).strip()
    job = application.start_lesson_from_writing(text, title, body.level, body.ratio, body.weave)
    return {
        "job_id": job.id,
        "status": job.status,
        "writing_id": piece["id"],
        "revision": revision,
        "title": title,
    }


@router.post("/api/writing/improve")
def improve_writing(body: ImproveBody) -> dict[str, Any]:
    application = get_app()
    if not (body.text or "").strip():
        raise HTTPException(400, "nothing was written")
    if body.mode not in writing.MODE_IDS:
        raise HTTPException(400, f"unknown way of improving a draft: {body.mode!r}")
    if application.tutor is None:
        raise HTTPException(
            503, "improving a draft needs the tutor; set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL"
        )
    suggestion = application.tutor.improve_writing(
        text=body.text, mode=body.mode, prompt=body.prompt, focus=body.words
    )
    if suggestion.get("failed"):
        return {"suggestion": None, **suggestion}
    return {"suggestion": suggestion}


@router.delete("/api/writing/{writing_id}")
def delete_writing(writing_id: int) -> dict[str, Any]:
    application = get_app()
    if not application.store.delete_writing(writing_id):
        raise HTTPException(404, f"no writing {writing_id}")
    return {"ok": True, "totals": application.store.writing_totals()}
