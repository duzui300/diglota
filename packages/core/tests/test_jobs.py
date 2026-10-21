"""Tests for the background job system.

The interesting properties here are the ones that are invisible until they are
wrong: that work actually runs off the calling thread, that concurrency is
bounded, that a cancel is honoured rather than ignored, and that a job which was
mid-flight when the process died is reported as interrupted instead of sitting
in the list forever claiming to be running.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.jobs import CANCELLED, DONE, FAILED, RUNNING, JobCancelled, JobManager  # noqa: E402
from app.store import Store  # noqa: E402


@pytest.fixture()
def manager():
    m = JobManager(workers=2)
    yield m
    m.stop()


def wait_for(predicate, timeout=20.0):
    """Wait for a condition, generously.

    These are behavioural assertions, not performance ones: what matters is
    that a job eventually reaches DONE, not that it does so in under five
    seconds. A tight timeout made the suite flaky whenever the machine was
    busy -- which it is, if a real import is weaving in the background.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ------------------------------------------------------------------ basics --


def test_job_runs_and_records_its_result(manager):
    job = manager.submit(kind="test", title="quick", work=lambda p: {"answer": 42})
    assert wait_for(lambda: manager.get(job.id).status == DONE)
    assert manager.get(job.id).result == {"answer": 42}


def test_work_runs_off_the_calling_thread(manager):
    caller = threading.current_thread().name
    seen = {}

    def work(progress):
        seen["thread"] = threading.current_thread().name
        return {}

    job = manager.submit(kind="test", title="threads", work=work)
    assert wait_for(lambda: manager.get(job.id).status == DONE)
    assert seen["thread"] != caller
    assert seen["thread"].startswith("job-worker")


def test_failure_is_captured_not_raised(manager):
    def work(progress):
        raise ValueError("the page was paywalled")

    job = manager.submit(kind="test", title="doomed", work=work)
    assert wait_for(lambda: manager.get(job.id).status == FAILED)
    assert "paywalled" in manager.get(job.id).error
    assert "ValueError" in manager.get(job.id).error


def test_a_job_starts_queued(manager):
    release = threading.Event()
    job = manager.submit(kind="test", title="blocked", work=lambda p: release.wait(2) or {})
    assert job.status in ("queued", "running")
    release.set()
    assert wait_for(lambda: manager.get(job.id).status == DONE)


# --------------------------------------------------------------- progress ---


def test_progress_is_recorded(manager):
    def work(progress):
        progress.step("weaving", done=0, total=4)
        for index in range(1, 5):
            progress.step("weaving", done=index, total=4, detail=f"passage {index} of 4")
        return {}

    job = manager.submit(kind="test", title="progress", work=work)
    assert wait_for(lambda: manager.get(job.id).status == DONE)
    record = manager.get(job.id)
    assert record.total == 4
    assert record.done == 4
    assert record.fraction == 1.0
    assert record.step == "weaving"


def test_fraction_is_zero_when_no_total_is_known(manager):
    release = threading.Event()
    job = manager.submit(kind="test", title="opaque", work=lambda p: release.wait(2) or {})
    assert wait_for(lambda: manager.get(job.id).status == RUNNING)
    assert manager.get(job.id).fraction == 0.0
    release.set()
    assert wait_for(lambda: manager.get(job.id).status == DONE)
    assert manager.get(job.id).fraction == 1.0


def test_a_cancelled_job_that_never_ran_is_at_zero(manager):
    """A job stopped before it started has done none of its work -- reporting
    it as 100% complete would read as success in the Activity panel."""
    release = threading.Event()
    blockers = [manager.submit(kind="test", title=f"block {i}", work=lambda p: release.wait(3) or {})
                for i in range(2)]
    queued = manager.submit(kind="test", title="never", work=lambda p: {})
    assert wait_for(lambda: queued.status == "queued" or queued.status == RUNNING)
    manager.cancel(queued.id)
    assert manager.get(queued.id).fraction == 0.0
    release.set()
    assert wait_for(lambda: all(manager.get(b.id).status == DONE for b in blockers), timeout=10)


# ------------------------------------------------------------- concurrency --


def test_concurrency_is_bounded(manager):
    """Four jobs, two workers: the fan-out inside each job must not multiply
    into an unbounded number of model calls."""
    lock = threading.Lock()
    live = 0
    peak = 0

    def work(progress):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.15)
        with lock:
            live -= 1
        return {}

    jobs = [manager.submit(kind="test", title=f"job {i}", work=work) for i in range(4)]
    assert wait_for(lambda: all(manager.get(j.id).status == DONE for j in jobs), timeout=10)
    assert peak <= 2, f"peak concurrency was {peak}, expected at most 2"


def test_active_counts_both_queued_and_running(manager):
    release = threading.Event()
    jobs = [manager.submit(kind="test", title=f"job {i}", work=lambda p: release.wait(3) or {})
            for i in range(4)]
    assert wait_for(lambda: manager.active == 4)
    release.set()
    assert wait_for(lambda: manager.active == 0, timeout=10)
    assert all(manager.get(j.id).status == DONE for j in jobs)


# ------------------------------------------------------------ cancellation --


def test_cancelling_a_running_job_stops_it(manager):
    started = threading.Event()

    def work(progress):
        started.set()
        for _ in range(200):
            time.sleep(0.01)
            progress.check()      # cooperative: this is where cancellation lands
        return {"finished": True}

    job = manager.submit(kind="test", title="long", work=work)
    assert started.wait(3)
    assert manager.cancel(job.id) is True
    assert wait_for(lambda: manager.get(job.id).status == CANCELLED)
    assert manager.get(job.id).result is None


def test_work_that_ignores_cancellation_still_completes(manager):
    """Cancellation is cooperative, so a job that never checks in finishes.
    That is a deliberate limit, not a bug -- nothing here can interrupt a
    socket read, and pretending otherwise would leave half-written files."""
    def work(progress):
        time.sleep(0.2)
        return {"done": True}

    job = manager.submit(kind="test", title="rude", work=work)
    time.sleep(0.05)
    manager.cancel(job.id)
    assert wait_for(lambda: manager.get(job.id).status in (DONE, CANCELLED))
    assert manager.get(job.id).status == DONE


def test_cancelling_a_queued_job_never_runs_it(manager):
    release = threading.Event()
    blockers = [manager.submit(kind="test", title=f"block {i}", work=lambda p: release.wait(3) or {})
                for i in range(2)]
    ran = threading.Event()
    queued = manager.submit(kind="test", title="never", work=lambda p: ran.set() or {})
    assert wait_for(lambda: queued.status == "queued" or queued.status == RUNNING)
    assert manager.cancel(queued.id) is True
    assert queued.status == CANCELLED
    release.set()
    assert wait_for(lambda: all(manager.get(b.id).status == DONE for b in blockers), timeout=10)
    time.sleep(0.2)
    assert not ran.is_set()


def test_cancelling_a_finished_job_reports_false(manager):
    job = manager.submit(kind="test", title="done", work=lambda p: {})
    assert wait_for(lambda: manager.get(job.id).status == DONE)
    assert manager.cancel(job.id) is False


def test_job_cancelled_exception_is_not_reported_as_a_failure(manager):
    job = manager.submit(kind="test", title="raises",
                         work=lambda p: (_ for _ in ()).throw(JobCancelled("stop")))
    assert wait_for(lambda: manager.get(job.id).status == CANCELLED)
    assert manager.get(job.id).error == "cancelled"


# --------------------------------------------------------------- lifecycle --


def test_dismiss_removes_a_finished_job(manager):
    job = manager.submit(kind="test", title="bye", work=lambda p: {})
    assert wait_for(lambda: manager.get(job.id).status == DONE)
    assert manager.dismiss(job.id) is True
    assert manager.get(job.id) is None


def test_dismiss_refuses_a_running_job(manager):
    release = threading.Event()
    job = manager.submit(kind="test", title="busy", work=lambda p: release.wait(2) or {})
    assert wait_for(lambda: manager.get(job.id).status == RUNNING)
    assert manager.dismiss(job.id) is False
    release.set()
    assert wait_for(lambda: manager.get(job.id).status == DONE)


def test_snapshot_is_newest_first(manager):
    first = manager.submit(kind="test", title="first", work=lambda p: {})
    assert wait_for(lambda: manager.get(first.id).status == DONE)
    second = manager.submit(kind="test", title="second", work=lambda p: {})
    assert wait_for(lambda: manager.get(second.id).status == DONE)
    titles = [job["title"] for job in manager.snapshot()]
    assert titles.index("second") < titles.index("first")


def test_clear_finished_leaves_running_jobs_alone(manager):
    release = threading.Event()
    running = manager.submit(kind="test", title="running", work=lambda p: release.wait(2) or {})
    finished = manager.submit(kind="test", title="finished", work=lambda p: {})
    assert wait_for(lambda: manager.get(finished.id).status == DONE)
    manager.clear_finished()
    assert manager.get(running.id) is not None
    assert manager.get(finished.id) is None
    release.set()


# ------------------------------------------------------------ persistence ---


def test_history_survives_a_restart(tmp_path):
    store = Store(tmp_path / "jobs.db")
    first = JobManager(workers=1, store=store)
    job = first.submit(kind="import", title="An article", work=lambda p: {"words": 1200})
    assert wait_for(lambda: first.get(job.id).status == DONE)
    first.stop()

    second = JobManager(workers=1, store=store)
    try:
        reloaded = second.get(job.id)
        assert reloaded is not None
        assert reloaded.status == DONE
        assert reloaded.result == {"words": 1200}
    finally:
        second.stop()
        store.close()


def test_a_job_interrupted_by_a_restart_is_reported_as_failed(tmp_path):
    store = Store(tmp_path / "jobs.db")
    first = JobManager(workers=1, store=store)
    release = threading.Event()
    job = first.submit(kind="import", title="Interrupted", work=lambda p: release.wait(5) or {})
    assert wait_for(lambda: first.get(job.id).status == RUNNING)
    first.stop()          # the process "dies" with the job still running
    release.set()

    second = JobManager(workers=1, store=store)
    try:
        reloaded = second.get(job.id)
        assert reloaded.status == FAILED
        assert "interrupted" in reloaded.error
    finally:
        second.stop()
        store.close()


# ------------------------------------------------------------- subscribers --


def test_subscribers_receive_updates(manager):
    channel = manager.subscribe()
    try:
        job = manager.submit(kind="test", title="watched", work=lambda p: {})
        assert wait_for(lambda: manager.get(job.id).status == DONE)
        seen = []
        while not channel.empty():
            seen.append(channel.get_nowait())
        assert seen, "expected at least one published update"
        assert any('"done"' in item for item in seen)
    finally:
        manager.unsubscribe(channel)


def test_event_stream_opens_with_a_snapshot(manager):
    manager.submit(kind="test", title="listed", work=lambda p: {})
    stream = manager.event_stream(heartbeat=0.05)
    first = next(stream)
    assert first.startswith("data: ")
    assert '"snapshot"' in first
    stream.close()


def test_unsubscribing_stops_delivery(manager):
    channel = manager.subscribe()
    manager.unsubscribe(channel)
    manager.submit(kind="test", title="ignored", work=lambda p: {})
    time.sleep(0.3)
    assert channel.empty()
