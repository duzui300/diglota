"""Pydantic request schemas for all HTTP endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ShareBody(BaseModel):
    text: str
    creator: str = ""
    slug: str | None = None


class ProgressBody(BaseModel):
    position: float | None = None
    seconds: int = 0
    completed: bool = False


class RegisterBody(BaseModel):
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
    source: str | None = None
    reference: str | None = None
    slug: str | None = None


class ImportBody(BaseModel):
    url: str | None = None
    text: str | None = None
    source_url: str | None = None
    title_hint: str | None = None
    level: str | None = None
    ratio: float | None = None
    weave: str | None = None


class DiscoverBody(BaseModel):
    query: str
    exclude: list[str] = []


class ExplainBody(BaseModel):
    text: str
    sentence: str = ""
    question: str | None = None


class TranslateBody(BaseModel):
    text: str
    target: str = "Spanish"


class SettingsBody(BaseModel):
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    lookup_llm_base_url: str | None = None
    lookup_llm_api_key: str | None = None
    lookup_llm_model: str | None = None
    weave_llm_base_url: str | None = None
    weave_llm_api_key: str | None = None
    weave_llm_model: str | None = None
    warm_glosses: str | None = None
    typesafe_api_key: str | None = None
    typesafe_model: str | None = None


class QuoteBody(BaseModel):
    slug: str
    block_index: int
    text: str


class QuoteNoteBody(BaseModel):
    note: str = ""


class ScaffoldingBody(BaseModel):
    kind: str = Field(pattern="^(gloss-on|gloss-off|spanish-only|target-only|focus)$")
    slug: str | None = None


class PreviewBody(BaseModel):
    level: str | None = None
    ratio: float | None = None
    weave: str | None = None
    subject: str | None = None


class WritingBody(BaseModel):
    text: str
    prompt: str = ""
    words: list[str] = []


class DraftBody(WritingBody):
    writing_id: int | None = None


class ImproveBody(WritingBody):
    mode: str = "grammar"


class LessonBody(WritingBody):
    writing_id: int | None = None
    title: str | None = None
    level: str | None = None
    ratio: float | None = None
    weave: str | None = None


class ChallengeBody(BaseModel):
    kind: str
    target: int | None = None
