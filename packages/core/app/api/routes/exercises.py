"""Tutor assist (explain/translate) and exercises/grading routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ...diglot import content_fingerprint
from ...grading import is_correct, is_provisional, may_fail
from ...languages import resolve_language_name
from ...tutor import cache_key
from ..deps import get_app, require_article, require_tutor
from ..schemas import AttemptBody, ExplainBody, TranslateBody

router = APIRouter()


@router.post("/api/explain")
def explain(body: ExplainBody) -> dict[str, Any]:
    """A longer, on-demand grammar explanation of a passage."""
    application = require_tutor()
    if not body.text.strip():
        raise HTTPException(400, "text is required")
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


@router.post("/api/translate")
def translate(body: TranslateBody) -> dict[str, Any]:
    """Translate a passage the reader selected into any target language."""
    application = require_tutor()
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "text is required")
    if len(text) > 1200:
        text = text[:1200]
    target = resolve_language_name(body.target, default_pair=application.active_pair)
    application.store.log_scaffolding("translate", term=text[:60])
    key = cache_key("translate", target, text.lower())
    data = application.cached(
        key,
        lambda: {"translation": application.tutor.sentence_translation(sentence=text, target=target)},
    )
    return data or {"translation": ""}


@router.get("/api/article/{slug}/quiz")
def quiz(slug: str) -> dict[str, Any]:
    application = require_tutor()
    entry = require_article(application, slug)
    key = cache_key("quiz", content_fingerprint(entry.article))
    data = application.cached(key, lambda: {"questions": application.tutor.quiz(entry.article)})
    return {"slug": slug, "questions": (data or {}).get("questions", [])}


@router.get("/api/article/{slug}/drills")
def drills(slug: str) -> dict[str, Any]:
    application = require_tutor()
    entry = require_article(application, slug)
    key = cache_key("drills", content_fingerprint(entry.article))
    data = application.cached(key, lambda: {"drills": application.tutor.translation_drills(entry.article)})
    return {"slug": slug, "drills": (data or {}).get("drills", [])}


@router.post("/api/attempt")
def attempt(body: AttemptBody) -> dict[str, Any]:
    """Grade one exercise. Jev decides, the tutor explains."""
    application = get_app()
    answer = body.answer.strip()
    if not answer:
        raise HTTPException(400, "answer is required")

    if body.kind == "translate":
        if not (body.source and body.reference):
            raise HTTPException(400, "translate needs source and reference")
        verdict = application.grading.grade_translation(
            source=body.source, reference=body.reference, attempt=answer
        )
    elif body.kind == "cloze":
        verdict = application.grading.grade_cloze(
            sentence=body.question, blank=body.reference or "",
            target=body.reference or "", attempt=answer,
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

    if body.kind in ("translate", "cloze"):
        if correct:
            outcome = "correct"
        elif may_fail(payload):
            outcome = "incorrect"
        else:
            outcome = "unverified"
        application.store.save_attempt(
            slug=body.slug,
            question=body.question or body.source or "",
            answer=answer,
            verdict=outcome,
            score=payload.get("score"),
            feedback=(feedback or {}).get("summary"),
        )

    return {
        "verdict": payload,
        "feedback": feedback,
        "correct": correct,
        "provisional": is_provisional(payload),
        "may_fail": may_fail(payload),
    }


@router.get("/api/article/{slug}/attempts")
def attempts(slug: str) -> dict[str, Any]:
    return {"attempts": get_app().store.attempts_for(slug)}
