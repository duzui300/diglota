"""FastAPI application compatibility facade.

The server implementation now lives in :mod:`app.runtime` (application container)
and :mod:`app.api` (schemas, dependencies, middleware, and grouped route modules).
This module exposes ``app``, ``App``, ``ctx``, ``set_shutdown_hook``, and every
route callable and Pydantic schema so existing launchers, Windows packaging, and
unit tests keep working unchanged.
"""

from __future__ import annotations

from typing import Callable

from .api import create_app, lifespan
from .api.deps import require_tutor as _require_tutor
from .api.routes.exercises import attempt, attempts, drills, explain, quiz, translate
from .api.routes.imports import (
    _annotate,
    _free_slug,
    _origin,
    discover,
    discovery_gaps,
    export_passage,
    import_options,
    import_preview,
    import_share,
    import_url,
    inspect_share,
    start_recommend,
    suggest,
)
from .api.routes.jobs import (
    job_cancel,
    job_clear,
    job_dismiss,
    job_status,
    jobs,
    jobs_stream,
)
from .api.routes.library import (
    _challenge_context,
    article,
    corpus_analytics,
    delete_article,
    get_challenge,
    library,
    log_scaffolding,
    reading_analytics,
    save_progress,
    set_register,
    start_challenge,
    stats,
    stop_challenge,
)
from .api.routes.quotes import (
    _decorate,
    _quote_sources,
    annotate_quote,
    delete_quote,
    list_quotes,
    save_quote,
)
from .api.routes.system import (
    get_settings,
    language_pairs,
    quit_app,
    register_options,
    reset_progress,
    save_settings,
    status,
)
from .api.routes.vocabulary import (
    delete_word,
    export_csv,
    glossary,
    lookup,
    lookups,
    review_queue,
    save_word,
    submit_review,
    vocabulary_index,
    words,
)
from .api.routes.writing import (
    _open_piece,
    _piece,
    _writing_feedback,
    _writing_prompt,
    check_writing,
    delete_writing,
    get_writing,
    improve_writing,
    list_writings,
    measure_writing,
    save_draft,
    writing_lesson,
    writing_prompt,
)
from .api.schemas import (
    AttemptBody,
    ChallengeBody,
    DiscoverBody,
    DraftBody,
    ExplainBody,
    ImportBody,
    ImproveBody,
    LessonBody,
    PreviewBody,
    ProgressBody,
    QuoteBody,
    QuoteNoteBody,
    RegisterBody,
    ReviewBody,
    SaveWordBody,
    ScaffoldingBody,
    SettingsBody,
    ShareBody,
    TranslateBody,
    WritingBody,
)
from .runtime import (
    DECK_PAGE_LIMIT,
    DISCOVER_PAGE,
    FAILURE_TTL,
    App,
    _WeaveCache,
    _job_title,
    _paste_title,
)

app = create_app()


def ctx() -> App:
    return app.state.app


_shutdown: Callable[[], None] | None = None


def set_shutdown_hook(hook: Callable[[], None] | None) -> None:
    global _shutdown
    _shutdown = hook


__all__ = [
    "App",
    "AttemptBody",
    "ChallengeBody",
    "DECK_PAGE_LIMIT",
    "DISCOVER_PAGE",
    "DiscoverBody",
    "DraftBody",
    "ExplainBody",
    "FAILURE_TTL",
    "ImportBody",
    "ImproveBody",
    "LessonBody",
    "PreviewBody",
    "ProgressBody",
    "QuoteBody",
    "QuoteNoteBody",
    "RegisterBody",
    "ReviewBody",
    "SaveWordBody",
    "ScaffoldingBody",
    "SettingsBody",
    "ShareBody",
    "TranslateBody",
    "WritingBody",
    "_WeaveCache",
    "_annotate",
    "_challenge_context",
    "_decorate",
    "_free_slug",
    "_job_title",
    "_open_piece",
    "_origin",
    "_paste_title",
    "_piece",
    "_quote_sources",
    "_require_tutor",
    "_shutdown",
    "_writing_feedback",
    "_writing_prompt",
    "annotate_quote",
    "app",
    "article",
    "attempt",
    "attempts",
    "check_writing",
    "corpus_analytics",
    "create_app",
    "ctx",
    "delete_article",
    "delete_quote",
    "delete_word",
    "delete_writing",
    "discover",
    "discovery_gaps",
    "drills",
    "explain",
    "export_csv",
    "export_passage",
    "get_challenge",
    "get_settings",
    "get_writing",
    "glossary",
    "import_options",
    "import_preview",
    "import_share",
    "import_url",
    "improve_writing",
    "inspect_share",
    "job_cancel",
    "job_clear",
    "job_dismiss",
    "job_status",
    "jobs",
    "jobs_stream",
    "language_pairs",
    "library",
    "lifespan",
    "list_quotes",
    "list_writings",
    "log_scaffolding",
    "lookup",
    "lookups",
    "measure_writing",
    "quit_app",
    "quiz",
    "reading_analytics",
    "register_options",
    "reset_progress",
    "review_queue",
    "save_draft",
    "save_progress",
    "save_quote",
    "save_settings",
    "save_word",
    "set_register",
    "set_shutdown_hook",
    "start_challenge",
    "start_recommend",
    "stats",
    "status",
    "stop_challenge",
    "submit_review",
    "suggest",
    "translate",
    "vocabulary_index",
    "words",
    "writing_lesson",
    "writing_prompt",
]
