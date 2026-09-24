"""Tests for the weekly goal.

A challenge is a promise the app makes about the reader's own week, so the two
things that matter are that a number is never invented and that a goal is never
imposed. Every measurement has to come from data that already existed, every
proposal has to be refusable on stated grounds, and running out of time has to be
an ending rather than a nag.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import challenges  # noqa: E402


class FakeStore:
    """A store that answers the six questions a challenge can ask."""

    def __init__(self, *, tokens=0, new=0, slugs_read=(), days_read=0, progress=None):
        self._tokens = tokens
        self._new = new
        self._slugs_read = list(slugs_read)
        self._days_read = days_read
        self._progress = progress or {}

    def exposure_totals(self, *, days=7):
        return {"tokens": self._tokens}

    def exposure_split(self, *, days=7):
        return {"new": self._new, "familiar": 0, "repeated": 0}

    def exposure_series(self, *, days=30):
        return [{"day": f"day-{index}", "tokens": 100 if index < self._days_read else 0}
                for index in range(days)]

    def exposure_by_article(self, *, days=30, limit=12):
        return [{"slug": slug, "tokens": 100, "known": 0} for slug in self._slugs_read]

    def all_progress(self):
        return dict(self._progress)


class FakeLibrary:
    def __init__(self, registers_by_slug=None):
        self._registers = registers_by_slug or {}

    def get(self, slug):
        code = self._registers.get(slug)
        if code is None:
            return None
        return type("Entry", (), {"article": type("A", (), {"register": code})()})()


def context(**kwargs):
    store = kwargs.pop("store", None)
    return challenges.Context(store=store or FakeStore(), library=FakeLibrary(),
                              **kwargs)


# ------------------------------------------------------------------ measuring --


@pytest.mark.parametrize("kind", [k.id for k in challenges.KINDS])
def test_every_kind_can_be_measured_on_a_blank_store(kind):
    """A goal has to be answerable on day one, from an empty database, or the app
    is promising to count something it cannot see."""
    value = challenges.measure(kind, context(), "")
    assert isinstance(value, int) and value >= 0


def test_volume_counts_the_words_actually_read():
    assert challenges.measure("volume", context(store=FakeStore(tokens=1234)), "") == 1234


def test_days_counts_days_with_reading_on_them():
    assert challenges.measure("days", context(store=FakeStore(days_read=4)), "") == 4


def test_finishing_counts_only_what_was_finished_inside_the_window():
    store = FakeStore(progress={
        "recent": {"completed_at": "2026-09-20", "position": 1.0},
        "old": {"completed_at": "2026-01-01", "position": 1.0},
        "half": {"completed_at": None, "position": 0.4},
    })
    assert challenges.measure("finish", context(store=store), "2026-09-15") == 1


def test_spread_counts_distinct_registers_read():
    store = FakeStore(slugs_read=["a", "b", "c"])
    library = FakeLibrary({"a": "essay", "b": "essay", "c": "news"})
    ctx = challenges.Context(store=store, library=library)
    assert challenges.measure("spread", ctx, "") == 2, "two kinds, not three articles"


def test_spread_does_not_count_an_unknown_register_as_a_kind():
    store = FakeStore(slugs_read=["a", "b"])
    ctx = challenges.Context(store=store, library=FakeLibrary({"a": "essay"}))
    assert challenges.measure("spread", ctx, "") == 1


def test_topics_counts_distinct_clusters():
    store = FakeStore(slugs_read=["a", "b", "c"])
    ctx = challenges.Context(store=store, library=FakeLibrary(),
                             cluster_of={"a": 0, "b": 0, "c": 1})
    assert challenges.measure("topics", ctx, "") == 2


def test_a_kind_that_cannot_be_measured_is_not_offered():
    """Better to leave it out than to offer a goal whose number would always be
    zero: the corpus graph is not always built."""
    ids = [kind["id"] for kind in challenges.available(context())]
    assert "topics" not in ids
    with_graph = challenges.available(context(cluster_of={"a": 0}))
    assert "topics" in [kind["id"] for kind in with_graph]


# ------------------------------------------------------------------- the window --


def test_the_window_is_a_week_from_the_day_it_started():
    now = datetime.now(timezone.utc)
    assert challenges.days_left(now.isoformat()) == challenges.WINDOW_DAYS
    assert challenges.days_left((now - timedelta(days=3)).isoformat()) == 4
    assert challenges.days_left((now - timedelta(days=10)).isoformat()) < 0


def test_days_left_survives_a_timestamp_it_cannot_read():
    assert challenges.days_left("not a date") == 0
    assert challenges.days_left("") == 0


def test_a_goal_started_today_measures_today_only():
    """Measuring the last seven days would count days from before the goal was
    accepted -- which flatters a challenge started this morning."""
    row = {"started_at": datetime.now(timezone.utc).isoformat()}
    assert challenges.context_for(row, context()).days == 1
    older = {"started_at": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()}
    assert challenges.context_for(older, context()).days == 4
    ancient = {"started_at": (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()}
    assert challenges.context_for(ancient, context()).days < 365


# -------------------------------------------------------------------- proposing --


def test_a_register_gap_outranks_everything_else():
    """It is the one failure the reader cannot see from the inside: you cannot
    miss what you never look at."""
    gaps = [{"register": "news", "label": "📰 News", "available": 2}]
    proposal = challenges.propose(context(), gaps)
    assert proposal["kind"] == "spread"
    assert "News" in proposal["reason"]


def test_unfinished_articles_are_proposed_when_there_is_no_gap():
    store = FakeStore(progress={
        "a": {"position": 0.5, "completed_at": None},
        "b": {"position": 0.3, "completed_at": None},
        "c": {"position": 1.0, "completed_at": "2026-09-01"},
    })
    proposal = challenges.propose(context(store=store), [])
    assert proposal["kind"] == "finish"
    assert "2" in proposal["reason"]


def test_bursty_reading_is_proposed_when_there_is_nothing_to_finish():
    proposal = challenges.propose(context(), [])
    assert proposal["kind"] == "days"
    assert "bursts" in proposal["reason"]


def test_a_proposal_always_says_why():
    for gaps in ([{"register": "news", "label": "📰 News", "available": 1}], []):
        proposal = challenges.propose(context(), gaps)
        assert proposal["reason"] and proposal["why"]
        assert proposal["target"] >= 1
        assert challenges.BY_ID[proposal["kind"]].title.format(n=proposal["target"]) == proposal["title"]


# -------------------------------------------------------------------- reporting --


def test_a_report_carries_progress_and_the_time_left():
    now = datetime.now(timezone.utc).isoformat()
    row = {"id": 1, "kind": "volume", "target": 1000, "started_at": now, "completed_at": None}
    report = challenges.report(row, context(store=FakeStore(tokens=400)))
    assert report["done"] == 400 and report["target"] == 1000
    assert report["ratio"] == 0.4
    assert report["expired"] is False and report["days_left"] == 7


def test_progress_past_the_target_is_capped_but_the_count_is_not():
    now = datetime.now(timezone.utc).isoformat()
    row = {"id": 1, "kind": "volume", "target": 100, "started_at": now, "completed_at": None}
    report = challenges.report(row, context(store=FakeStore(tokens=950)))
    assert report["ratio"] == 1.0
    assert report["done"] == 950, "the real number is still shown"


def test_a_week_that_ran_out_is_expired_rather_than_failed():
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    row = {"id": 1, "kind": "volume", "target": 1000, "started_at": old, "completed_at": None}
    report = challenges.report(row, context(store=FakeStore(tokens=10)))
    assert report["expired"] is True
    assert report["days_left"] < 0


def test_a_goal_met_after_the_week_is_not_expired():
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    row = {"id": 1, "kind": "volume", "target": 100, "started_at": old, "completed_at": None}
    report = challenges.report(row, context(store=FakeStore(tokens=400)))
    assert report["expired"] is False, "it was reached, however long it took"


def test_reporting_nothing_is_nothing():
    assert challenges.report(None, context()) is None
    assert challenges.report({"id": 1, "kind": "nonsense", "target": 3}, context()) is None


# ----------------------------------------------------------------------- store --


@pytest.fixture()
def store(tmp_path):
    from app.store import Store

    handle = Store(tmp_path / "test.db")
    yield handle
    handle.close()


def test_one_goal_is_active_at_a_time(store):
    """A list of goals is a list of things not done."""
    store.start_challenge("volume", 6000)
    second = store.start_challenge("days", 3)
    active = store.active_challenge()
    assert active["id"] == second["id"]
    history = store.challenge_history()
    assert len(history) == 2
    replaced = [row for row in history if row["id"] != second["id"]][0]
    assert replaced["ended_at"] and not replaced["completed_at"], "kept, and honestly closed"


def test_reaching_a_goal_does_not_hide_it(store):
    """Reaching a goal is worth showing. Ending the row at the same moment would
    hide the one moment the feature exists for."""
    row = store.start_challenge("volume", 6000)
    assert store.complete_challenge(row["id"]) is True
    active = store.active_challenge()
    assert active is not None and active["completed_at"], "still on screen, marked done"
    assert active["ended_at"] is None, "and not ended"


def test_giving_up_ends_the_row_without_claiming_completion(store):
    row = store.start_challenge("volume", 6000)
    assert store.end_challenge(row["id"]) is True
    assert store.active_challenge() is None
    kept = store.challenge_history()[0]
    assert kept["ended_at"] and not kept["completed_at"]


def test_a_second_completion_or_ending_is_refused_rather_than_rewriting_history(store):
    row = store.start_challenge("volume", 6000)
    assert store.complete_challenge(row["id"]) is True
    assert store.complete_challenge(row["id"]) is False, "the first time stands"
    assert store.end_challenge(row["id"]) is True
    assert store.end_challenge(row["id"]) is False


def test_a_target_of_zero_is_not_a_goal(store):
    row = store.start_challenge("volume", 0)
    assert row["target"] == 1


def test_history_is_newest_first_and_bounded(store):
    for target in (1, 2, 3, 4, 5):
        store.start_challenge("volume", target)
    history = store.challenge_history(limit=3)
    assert [row["target"] for row in history] == [5, 4, 3]


# -------------------------------------------------------------------- endpoints --


@pytest.fixture()
def app(tmp_path):
    from app.config import Settings, corpus_dir
    from app.server import App

    settings = Settings(corpus_dir=corpus_dir(),
                        data_dir=tmp_path / "data")
    application = App(settings)
    application.library.refresh()
    return application


def test_the_endpoint_proposes_before_it_imposes(app, monkeypatch):
    from app import server

    monkeypatch.setattr(server, "ctx", lambda: app)
    out = server.get_challenge()
    assert out["challenge"] is None, "nothing is running until the reader says so"
    assert out["suggested"]["kind"] in challenges.BY_ID
    assert out["suggested"]["reason"]
    assert out["kinds"], "and the reader is offered alternatives"


def test_a_goal_is_recorded_as_reached_once(app, monkeypatch):
    """The completion is noticed on the read that sees it and never again -- the
    panel should be able to say 'done' without saying it every time.

    Starting a goal that is already met completes it immediately, and that is
    honest: the window starts today, so "read 3,000 words" accepted after a long
    morning is a goal the reader has already kept.
    """
    from app import server

    monkeypatch.setattr(server, "ctx", lambda: app)
    app.store.record_exposure(slug="a", lemmas={"arte": 9}, known_stems=set(), glossed=set())

    first = server.start_challenge(server.ChallengeBody(kind="volume", target=5))["challenge"]
    assert first["done"] == 9 and first["completed_at"] is not None
    assert first["just_completed"] is True

    second = server.get_challenge()["challenge"]
    assert second["completed_at"], "still shown as reached"
    assert not second.get("just_completed"), "but not announced twice"
    assert second["id"] == first["id"], "and it is still the reader's goal, not a new suggestion"


def test_a_reached_goal_still_leaves_the_reader_something_to_do(app, monkeypatch):
    """Once a goal is met the panel offers the next one, without taking the
    finished goal off the screen and without leaving the panel empty."""
    from app import server

    monkeypatch.setattr(server, "ctx", lambda: app)
    app.store.record_exposure(slug="a", lemmas={"arte": 9}, known_stems=set(), glossed=set())

    running = server.start_challenge(server.ChallengeBody(kind="volume", target=5))
    assert running["challenge"]["completed_at"]
    assert running["suggested"] is not None, "a finished goal is followed by another"
    assert running["kinds"], "and the reader can still choose"

    # While a goal *is* running there is no second suggestion: one goal, one week.
    server.start_challenge(server.ChallengeBody(kind="days", target=6))
    live = server.get_challenge()
    assert live["challenge"]["kind"] == "days"
    assert live["suggested"] is None
    assert live["challenge"]["completed_at"] is None


def test_giving_up_through_the_endpoint_leaves_the_history(app, monkeypatch):
    from app import server

    monkeypatch.setattr(server, "ctx", lambda: app)
    started = server.start_challenge(server.ChallengeBody(kind="days", target=3))
    challenge_id = started["challenge"]["id"]
    after = server.stop_challenge(challenge_id)
    assert after["challenge"] is None
    assert any(row["id"] == challenge_id and row["ended_at"]
               for row in after["history"]), "the attempt is kept"


def test_the_endpoint_refuses_a_goal_it_cannot_measure(app, monkeypatch):
    from app import server
    from fastapi import HTTPException

    monkeypatch.setattr(server, "ctx", lambda: app)
    with pytest.raises(HTTPException) as caught:
        server.start_challenge(server.ChallengeBody(kind="get-fit"))
    assert caught.value.status_code == 400

    with pytest.raises(HTTPException):
        server.start_challenge(server.ChallengeBody(kind="volume", target=999_999))
    with pytest.raises(HTTPException):
        server.start_challenge(server.ChallengeBody(kind="volume", target=-1))


def test_stopping_something_that_is_not_running_is_a_404(app, monkeypatch):
    from app import server
    from fastapi import HTTPException

    monkeypatch.setattr(server, "ctx", lambda: app)
    with pytest.raises(HTTPException) as caught:
        server.stop_challenge(4242)
    assert caught.value.status_code == 404
