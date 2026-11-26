"""FastAPI application factory, middleware, static mounting, and lifespan."""

from __future__ import annotations

import logging
import mimetypes
import urllib.parse
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ..config import STATIC_DIR, Settings
from ..llm import LLMError
from ..runtime import App
from .routes import ROUTERS

log = logging.getLogger("diglot")

# Windows HKCR registries sometimes map .js to text/plain, which causes
# browsers and Edge WebView2 to refuse <script type="module">. Registering the
# MIME type explicitly guarantees ES modules always execute cleanly.
mimetypes.add_type("text/javascript", ".js")

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_loopback_origin(header_value: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(header_value)
    except ValueError:
        return False
    host = (parsed.hostname or "").strip().lower()
    return parsed.scheme in ("http", "https") and host in _LOOPBACK_HOSTS


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.load()
    application = App(settings)
    application.library.refresh()
    app.state.app = application
    log.info(
        "diglota ready: %d articles, %d words, llm=%s judge=%s pair=%s",
        len(application.library), application.store.word_count(),
        settings.llm_ready, settings.judge_ready, settings.active_pair,
    )
    try:
        yield
    finally:
        if application.warmer is not None:
            application.warmer.stop()
        application.jobs.stop()
        if application.judge is not None:
            application.judge.close()
        application.store.close()


class _RevalidatingStatic(StaticFiles):
    """Static files that revalidate before reuse and guarantee JS MIME types."""

    def file_response(self, *args, **kwargs) -> Response:  # type: ignore[no-untyped-def]
        response = super().file_response(*args, **kwargs)
        response.headers["cache-control"] = "no-cache"
        full_path = str(args[0]) if args else ""
        if full_path.lower().endswith(".js"):
            response.headers["content-type"] = "text/javascript; charset=utf-8"
        return response


def create_app() -> FastAPI:
    application = FastAPI(title="Diglota", version="1.1.0", lifespan=lifespan)

    @application.middleware("http")
    async def same_origin_csrf_guard(request: Request, call_next):
        """Reject cross-site browser mutations (e.g. form/sendBeacon CSRF from another origin)."""
        if request.method in _MUTATING_METHODS:
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            if origin and origin.lower() != "null" and not _is_loopback_origin(origin):
                return JSONResponse({"detail": "cross-origin mutation refused"}, status_code=403)
            if not origin and referer and not _is_loopback_origin(referer):
                return JSONResponse({"detail": "cross-origin mutation refused"}, status_code=403)
        return await call_next(request)

    for router in ROUTERS:
        application.include_router(router)

    @application.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"cache-control": "no-cache"})

    @application.exception_handler(LLMError)
    def llm_error_handler(_request: Request, exc: LLMError) -> JSONResponse:  # pragma: no cover
        return JSONResponse({"detail": str(exc)}, status_code=502)

    application.mount("/static", _RevalidatingStatic(directory=STATIC_DIR), name="static")
    return application
