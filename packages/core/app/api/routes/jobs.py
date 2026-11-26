"""Background jobs and Server-Sent Events routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..deps import get_app

router = APIRouter()


@router.get("/api/jobs")
def jobs() -> dict[str, Any]:
    application = get_app()
    return {"jobs": application.jobs.snapshot(), "active": application.jobs.active}


@router.get("/api/jobs/stream")
def jobs_stream(request: Request) -> StreamingResponse:
    del request
    application = get_app()
    return StreamingResponse(
        application.jobs.event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    job = get_app().jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"no job {job_id!r}")
    return job.to_dict()


@router.post("/api/jobs/{job_id}/cancel")
def job_cancel(job_id: str) -> dict[str, Any]:
    application = get_app()
    if application.jobs.get(job_id) is None:
        raise HTTPException(404, f"no job {job_id!r}")
    return {"cancelled": application.jobs.cancel(job_id)}


@router.delete("/api/jobs/{job_id}")
def job_dismiss(job_id: str) -> dict[str, Any]:
    application = get_app()
    job = application.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"no job {job_id!r}")
    if not application.jobs.dismiss(job_id):
        raise HTTPException(409, "that job is still running")
    return {"ok": True}


@router.delete("/api/jobs")
def job_clear() -> dict[str, Any]:
    get_app().jobs.clear_finished()
    return {"ok": True}
