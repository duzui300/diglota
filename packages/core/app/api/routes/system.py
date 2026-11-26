"""System, status, language-pair metadata, settings, reset, and shutdown routes."""

from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import APIRouter, HTTPException

from ... import registers
from ...config import validate_base_url
from ...languages import list_pairs
from ..deps import get_app
from ..schemas import SettingsBody

log = logging.getLogger("diglot")
router = APIRouter()


@router.post("/api/quit")
def quit_app() -> dict[str, Any]:
    """Stop the server, because the reader asked it to."""
    from ... import server

    hook = server._shutdown
    if hook is None:
        raise HTTPException(
            501, "this app was started in a way that cannot be stopped from here"
        )
    threading.Timer(0.4, hook).start()
    return {"stopping": True}


@router.get("/api/status")
def status() -> dict[str, Any]:
    application = get_app()
    describe = application.settings.describe()
    describe["articles"] = len(application.library)
    describe["words"] = application.store.word_count()
    describe["due"] = application.store.due_count()
    describe["grading"] = application.grading.describe()
    describe["language"] = application.active_pair.to_dict()
    return describe


@router.get("/api/languages")
def language_pairs() -> dict[str, Any]:
    """Registered language pairs and the currently active pair."""
    application = get_app()
    return {
        "active": application.active_pair.id,
        "pairs": [pair.to_dict() for pair in list_pairs()],
    }


@router.get("/api/registers")
def register_options() -> dict[str, Any]:
    """The taxonomy, for the control that lets a reader correct a tag."""
    return {"registers": registers.options()}


@router.get("/api/settings")
def get_settings() -> dict[str, Any]:
    """What the app is configured with, and where each value came from."""
    application = get_app()
    settings = application.settings
    return {
        "llm": settings.role_settings("default"),
        "lookup_llm": settings.role_settings("lookup"),
        "weave_llm": settings.role_settings("weave"),
        "warm_glosses": settings.warm_glosses or "off",
        "warm_glosses_active": settings.should_warm_glosses(),
        "judge": {
            "ready": settings.judge_ready,
            "model": settings.typesafe_model,
            "key_set": bool(settings.typesafe_api_key),
            "from": "app" if "typesafe_api_key" in settings.saved() else
                    ("env" if settings.typesafe_api_key else None),
        },
        "env_file": str(settings.env_path) if settings.env_path else None,
        "settings_file": str(settings.settings_file),
    }


@router.post("/api/settings")
def save_settings(body: SettingsBody) -> dict[str, Any]:
    """Write the panel's values and put them to work immediately."""
    application = get_app()
    changes = {key: value for key, value in body.model_dump().items() if value is not None}
    for url_key in ("llm_base_url", "lookup_llm_base_url", "weave_llm_base_url"):
        if url_key in changes and changes[url_key].strip():
            try:
                changes[url_key] = validate_base_url(changes[url_key])
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
    if "warm_glosses" in changes:
        mode = changes["warm_glosses"].strip().lower()
        if mode and mode not in ("off", "auto", "on"):
            raise HTTPException(400, "warm_glosses must be 'off', 'auto', or 'on'")
        changes["warm_glosses"] = mode
    application.settings.update_saved(changes)
    application.reload_models()
    return get_settings()


@router.post("/api/reset")
def reset_progress() -> dict[str, Any]:
    """Clear everything the reader has accumulated, keeping their lessons."""
    application = get_app()
    backup = application.store.backup(application.settings.data_dir / "backups")
    before = {
        "words": application.store.word_count(),
        "articles": len(application.library),
        "reviews": application.store.stats().get("reviews_total", 0),
    }
    application.store.reset()
    log.info("reset: cleared the reader's records (%s), backup at %s", before, backup)
    return {
        "cleared": before,
        "backup": str(backup),
        "kept": {
            "articles": len(application.library),
            "library": str(application.settings.library_dir),
        },
    }
