"""Shared dependency helpers for API route handlers."""

from __future__ import annotations

from fastapi import HTTPException

from ..library import LibraryEntry
from ..runtime import App


def get_app() -> App:
    """Resolve the active :class:`App` through ``app.server.ctx``.

    Delegating to ``app.server.ctx()`` preserves monkeypatch compatibility for
    the existing unit-test suite without coupling routes to global state.
    """
    from .. import server

    return server.ctx()


def require_tutor() -> App:
    application = get_app()
    if application.tutor is None:
        raise HTTPException(
            503,
            "the tutor is not configured; set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL",
        )
    return application


def require_weave_tutor() -> App:
    application = get_app()
    if application.weave_tutor is None:
        raise HTTPException(
            503,
            "the passage weave model is not configured; set a default or weave model in Settings",
        )
    return application


def require_article(application: App, slug: str) -> LibraryEntry:
    entry = application.library.get(slug)
    if entry is None:
        raise HTTPException(404, f"no article {slug!r}")
    return entry
