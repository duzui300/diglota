"""Grouped API routers for Diglota."""

from . import exercises, imports, jobs, library, quotes, system, vocabulary, writing

ROUTERS = (
    system.router,
    library.router,
    vocabulary.router,
    quotes.router,
    exercises.router,
    imports.router,
    writing.router,
    jobs.router,
)

__all__ = [
    "ROUTERS",
    "exercises",
    "imports",
    "jobs",
    "library",
    "quotes",
    "system",
    "vocabulary",
    "writing",
]
