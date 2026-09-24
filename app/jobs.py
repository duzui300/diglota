"""Background jobs: a small work queue with progress, cancellation and history.

Importing an article takes minutes and makes dozens of network calls. Running
that on the request thread means a client staring at a spinner that will
probably time out at the proxy before the work finishes, and it means the work
dies with the request. So it runs here instead, and the UI watches rather than
waits.

Three things this does that the previous ad-hoc thread did not:

*   **A bounded pool.** Jobs used to be one thread each, so importing four
    articles started four weaves, each of which starts four more chunk calls.
    The provider rate-limits that, and everything gets slower. Work now goes
    through a queue with a fixed number of workers.
*   **Progress that means something.** A job reports a step name, a unit count
    and a total, so the UI can show "weaving passage 3 of 7" and a real bar,
    rather than a spinner that says "working".
*   **History.** Jobs are written to the database, so the Activity panel still
    shows what happened after a restart -- and a job that was mid-flight when
    the server stopped is reported as interrupted rather than vanishing.

Cancellation is cooperative: the worker sets an event and the job's own code
checks in at stage boundaries. Nothing here can interrupt a socket read, and
pretending otherwise would leave half-written articles behind.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# How many jobs run at once. Each import already fans out over its chunks, so
# this multiplies: two jobs is eight concurrent model calls at the peak, which
# is about as much as the endpoint will take before it starts queueing.
MAX_CONCURRENT_JOBS = 2

# Finished jobs kept in the panel.
HISTORY_LIMIT = 30

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

_TERMINAL = {DONE, FAILED, CANCELLED}


class JobCancelled(RuntimeError):
    """Raised inside a job's own code when it checks in and finds itself cancelled."""


@dataclass
class Job:
    id: str
    kind: str
    title: str
    status: str = QUEUED
    step: str = ""
    done: int = 0
    total: int = 0
    detail: str = ""
    error: str | None = None
    result: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def running(self) -> bool:
        return self.status in (QUEUED, RUNNING)

    @property
    def fraction(self) -> float:
        if self.total <= 0:
            # No units were ever reported, so there is nothing to be a fraction
            # *of*. Only a finished job can claim to be complete -- a cancelled
            # one that never started is at zero, not at one.
            return 1.0 if self.status == DONE else 0.0
        return min(self.done / self.total, 1.0)

    def to_dict(self, *, include_result: bool = True) -> dict[str, Any]:
        data = {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "step": self.step,
            "done": self.done,
            "total": self.total,
            "detail": self.detail,
            "fraction": round(self.fraction, 3),
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "elapsed": round((self.finished_at or time.time()) - self.created_at, 1),
        }
        if include_result:
            data["result"] = self.result
        return data


class JobContext:
    """What a job's code uses to report progress and notice cancellation."""

    def __init__(self, job: Job, manager: "JobManager") -> None:
        self._job = job
        self._manager = manager

    @property
    def cancelled(self) -> bool:
        return self._job.cancel.is_set()

    def check(self) -> None:
        """Raise if the job has been cancelled. Call at stage boundaries."""
        if self.cancelled:
            raise JobCancelled("cancelled")

    def step(self, name: str, *, done: int | None = None, total: int | None = None,
             detail: str = "") -> None:
        """Report the current stage. ``done``/``total`` are optional units."""
        if self.cancelled:
            raise JobCancelled("cancelled")
        self._job.step = name
        if done is not None:
            self._job.done = done
        if total is not None:
            self._job.total = total
        self._job.detail = detail
        self._manager.publish(self._job)


class JobManager:
    """A queue, a fixed pool of workers, and a list of subscribers."""

    def __init__(self, workers: int = MAX_CONCURRENT_JOBS, store: Any = None) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._queue: queue.Queue[str] = queue.Queue()
        self._work: dict[str, Callable[[JobContext], Any]] = {}
        self._subscribers: set[queue.Queue] = set()
        self._store = store
        self._stopping = False
        self._workers: list[threading.Thread] = []

        if store is not None:
            self._load_history()

        for index in range(max(1, workers)):
            thread = threading.Thread(target=self._run, name=f"job-worker-{index}", daemon=True)
            thread.start()
            self._workers.append(thread)

    # -- history ---------------------------------------------------------- #

    def _load_history(self) -> None:
        """Reload past jobs, and mark anything that was mid-flight as interrupted.

        A job recorded as running at startup cannot still be running -- the
        process that was running it is gone.
        """
        try:
            for record in self._store.list_jobs(limit=HISTORY_LIMIT):
                job = Job(
                    id=record["id"], kind=record["kind"], title=record["title"],
                    status=record["status"], step=record["step"],
                    done=record["done"], total=record["total"], detail=record["detail"],
                    error=record["error"], result=record.get("result"),
                )
                if job.running:
                    job.status = FAILED
                    job.error = "interrupted — the server restarted while this was running"
                    self._persist(job)
                self._jobs[job.id] = job
                self._order.append(job.id)
        except Exception:  # a broken history must not stop the server booting
            pass

    def _persist(self, job: Job, **overrides: Any) -> None:
        """Write a job's state, with any overrides applied to the record.

        Overrides exist so a state change can be made durable *before* it is
        applied to the shared object -- see :meth:`_finish`.
        """
        if self._store is None:
            return
        try:
            record = job.to_dict()
            record.update(overrides)
            created = overrides.get("created_at", job.created_at)
            finished = overrides.get("finished_at", job.finished_at)
            record["created_at"] = _iso(created)
            record["finished_at"] = _iso(finished) if finished else None
            self._store.save_job(record)
        except Exception:  # persistence is a nicety; the job still runs
            pass

    # -- submission ------------------------------------------------------- #

    def submit(self, *, kind: str, title: str, work: Callable[[JobContext], Any]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, title=title)
        with self._lock:
            self._jobs[job.id] = job
            self._order.insert(0, job.id)
            self._work[job.id] = work
            self._trim()
        self._persist(job)
        self.publish(job)
        self._queue.put(job.id)
        return job

    def _trim(self) -> None:
        finished = [jid for jid in self._order
                    if jid in self._jobs and not self._jobs[jid].running]
        for job_id in finished[HISTORY_LIMIT:]:
            self._jobs.pop(job_id, None)
            self._work.pop(job_id, None)
            self._order.remove(job_id)

    # -- worker loop ------------------------------------------------------ #

    def _run(self) -> None:
        while not self._stopping:
            try:
                job_id = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            with self._lock:
                job = self._jobs.get(job_id)
                work = self._work.pop(job_id, None)
            if job is None or work is None:
                continue
            if job.cancel.is_set():
                self._finish(job, CANCELLED, error="cancelled before it started")
                continue

            job.status = RUNNING
            job.step = "starting"
            self.publish(job)
            context = JobContext(job, self)
            try:
                result = work(context)
                self._finish(job, DONE, result=result if isinstance(result, dict) else None)
            except JobCancelled:
                self._finish(job, CANCELLED, error="cancelled")
            except Exception as exc:
                self._finish(job, FAILED, error=f"{type(exc).__name__}: {exc}")

    def _finish(self, job: Job, status: str, *, result: dict | None = None,
                error: str | None = None) -> None:
        """Move a job to a terminal state.

        The order matters. The database is written first and the shared object
        is updated second, so a reader can never observe the finished state
        while the stored record still says "running". Otherwise a restart in
        that window would report a completed job as interrupted -- which is
        exactly what a test caught.
        """
        finished = time.time()
        step = "Cancelled" if status == CANCELLED else job.step
        done = max(job.done, job.total) if status == DONE else job.done

        self._persist(job, status=status, result=result, error=error,
                      finished_at=finished, step=step, done=done)

        with self._lock:
            job.status = status
            job.result = result
            job.error = error
            job.finished_at = finished
            job.step = step
            job.done = done
        self.publish(job)

    # -- control ---------------------------------------------------------- #

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def snapshot(self, limit: int = HISTORY_LIMIT) -> list[dict[str, Any]]:
        with self._lock:
            ids = self._order[:limit]
            return [self._jobs[jid].to_dict() for jid in ids if jid in self._jobs]

    def cancel(self, job_id: str) -> bool:
        """Ask a job to stop. Returns False if it is not around or already done."""
        job = self.get(job_id)
        if job is None or not job.running:
            return False
        job.cancel.set()
        # A queued job has no worker to notice the flag.
        if job.status == QUEUED:
            self._finish(job, CANCELLED, error="cancelled before it started")
        else:
            job.step = "cancelling"
            job.detail = "finishing the current step"
            self.publish(job)
        return True

    def dismiss(self, job_id: str) -> bool:
        """Remove a finished job from the list."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.running:
                return False
            self._jobs.pop(job_id, None)
            self._order.remove(job_id)
        if self._store is not None:
            try:
                self._store.delete_job(job_id)
            except Exception:
                pass
        return True

    def clear_finished(self) -> None:
        with self._lock:
            for job_id in [jid for jid in self._order
                           if jid in self._jobs and not self._jobs[jid].running]:
                self._jobs.pop(job_id, None)
                self._order.remove(job_id)
        if self._store is not None:
            try:
                self._store.clear_finished_jobs()
            except Exception:
                pass

    @property
    def active(self) -> int:
        with self._lock:
            return sum(1 for job in self._jobs.values() if job.running)

    def stop(self) -> None:
        self._stopping = True

    # -- subscribers ------------------------------------------------------ #

    def subscribe(self) -> queue.Queue:
        channel: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            self._subscribers.add(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(channel)

    def publish(self, job: Job) -> None:
        """Tell every listener about a change. Never blocks a worker."""
        payload = json.dumps({"type": "job", "job": job.to_dict()}, ensure_ascii=False)
        with self._lock:
            channels = list(self._subscribers)
        for channel in channels:
            try:
                channel.put_nowait(payload)
            except queue.Full:
                # A listener that cannot keep up is not worth stalling a job
                # for; it will resync from the snapshot endpoint.
                pass

    def event_stream(self, heartbeat: float = 15.0) -> Iterable[str]:
        """Server-sent events: a snapshot on connect, then a line per change."""
        channel = self.subscribe()
        try:
            yield f"data: {json.dumps({'type': 'snapshot', 'jobs': self.snapshot()})}\n\n"
            while True:
                try:
                    yield f"data: {channel.get(timeout=heartbeat)}\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"
        finally:
            self.unsubscribe(channel)


def _iso(stamp: float | None) -> str | None:
    if stamp is None:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")
