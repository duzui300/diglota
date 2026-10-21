"""A goal worth a week, measured from what the reader already did.

A tracker for its own sake is a way of making reading feel like homework, so
three constraints shape everything here.

**Nothing new is tracked.** Every measurement comes from data the app already
records because something else needed it -- exposure, completion, per-day
activity, the register of each passage. A challenge that required its own
instrumentation would be a challenge that only existed to be measured.

**One at a time.** A list of goals is a list of things not done. There is one
active challenge, it lasts a week, and when the week is up it is over -- reported
once, not nagged about.

**The proposal addresses what is actually missing.** Most of these exist because
of a specific quiet failure: reading only one kind of writing, starting far more
than you finish, bingeing on a Sunday. Which one is offered is decided by looking
at the reader's own numbers, and a register gap outranks everything, because that
is the one the reader cannot see from the inside.

The kinds are deliberately few. Each has to be worth a week of someone's
attention, and measurable without guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from . import registers


@dataclass(frozen=True)
class Kind:
    id: str
    title: str          # ``{n}`` is the target
    unit: str           # "articles", "words", ...
    targets: tuple[int, ...]
    why: str
    # A short name for the picker. The title with its number removed reads like a
    # command -- "Read Spanish words" -- and a list of commands is not a menu.
    label: str = ""


KINDS: tuple[Kind, ...] = (
    Kind(
        id="volume", label="Spanish words read",
        title="Read {n} Spanish words",
        unit="words",
        targets=(3000, 6000, 12000),
        why="Volume is the strongest single predictor of progress, and only counts "
            "Spanish you actually scrolled past.",
    ),
    Kind(
        id="new_words", label="New words met",
        title="Meet {n} words for the first time",
        unit="new words",
        targets=(40, 80, 150),
        why="New words are where the reading is doing work. If this stays near zero "
            "you are reading comfortably, which is pleasant and does not teach much.",
    ),
    Kind(
        id="spread", label="Kinds of writing",
        title="Read {n} different kinds of writing",
        unit="kinds",
        targets=(3, 4),
        why="A news report, an essay and a story use different Spanish. Reading one "
            "kind well is not the same as reading the language.",
    ),
    Kind(
        id="finish", label="Articles finished",
        title="Finish {n} articles",
        unit="articles",
        targets=(2, 3, 5),
        why="Starting is where vocabulary gets met; finishing is where it gets "
            "consolidated, and half-read articles are the easiest thing to accumulate.",
    ),
    Kind(
        id="days", label="Days read",
        title="Read on {n} different days",
        unit="days",
        targets=(3, 5, 6),
        why="Spacing beats bingeing, for the same reason the review schedule exists.",
    ),
    Kind(
        id="topics", label="Subjects read",
        title="Read from {n} different subjects",
        unit="subjects",
        targets=(2, 3),
        why="Related passages reinforce each other's vocabulary; unrelated ones test "
            "whether it is actually yours.",
    ),
)

BY_ID: dict[str, Kind] = {kind.id: kind for kind in KINDS}

# A week. Long enough that a missed day does not end it, short enough that the
# number on the page is about now rather than about the last three months.
WINDOW_DAYS = 7


def days_left(started_at: str) -> int:
    """Whole days remaining in the window, negative once it has passed."""
    try:
        started = datetime.fromisoformat(started_at)
    except (TypeError, ValueError):
        return 0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - started).days
    return WINDOW_DAYS - elapsed


@dataclass
class Context:
    """What a measurement is allowed to look at."""

    store: Any
    library: Any
    days: int = WINDOW_DAYS
    # slug -> cluster index, from the corpus graph. Empty when the graph has not
    # been built, in which case the topic challenge is not offered at all --
    # better to leave it out than to offer a goal that cannot be measured.
    cluster_of: dict[str, int] = field(default_factory=dict)


def _slugs_read(ctx: Context) -> list[str]:
    return [row["slug"] for row in ctx.store.exposure_by_article(days=ctx.days, limit=200) if row["slug"]]


def _register_of(ctx: Context, slug: str) -> str:
    entry = ctx.library.get(slug)
    if entry is None:
        return registers.UNKNOWN
    return registers.effective(entry.article)[0]


def _completed_since(ctx: Context, since: str) -> int:
    return sum(1 for state in ctx.store.all_progress().values()
               if (state.get("completed_at") or "") >= since)


def measure(kind: str, ctx: Context, since: str) -> int:
    """How far along a challenge of this kind is. Never raises, never guesses."""
    if kind == "volume":
        return int(ctx.store.exposure_totals(days=ctx.days)["tokens"])
    if kind == "new_words":
        return int(ctx.store.exposure_split(days=ctx.days)["new"])
    if kind == "days":
        return sum(1 for day in ctx.store.exposure_series(days=ctx.days) if day["tokens"] > 0)
    if kind == "finish":
        return _completed_since(ctx, since)
    if kind == "spread":
        return len({_register_of(ctx, slug) for slug in _slugs_read(ctx)} - {registers.UNKNOWN})
    if kind == "topics":
        if not ctx.cluster_of:
            return 0
        return len({ctx.cluster_of[slug] for slug in _slugs_read(ctx) if slug in ctx.cluster_of})
    return 0


def available(ctx: Context) -> list[dict[str, Any]]:
    """The kinds this reader can be measured on right now."""
    out = []
    for kind in KINDS:
        if kind.id == "topics" and not ctx.cluster_of:
            continue
        out.append({
            "id": kind.id, "title": kind.title, "unit": kind.unit,
            "label": kind.label or kind.unit, "targets": list(kind.targets),
            "why": kind.why,
        })
    return out


def describe(kind_id: str, target: int) -> str:
    kind = BY_ID.get(kind_id)
    return kind.title.format(n=target) if kind else kind_id


# --------------------------------------------------------------------------- #
# What to propose
# --------------------------------------------------------------------------- #


def propose(ctx: Context, gaps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The one challenge worth suggesting to this reader, and why.

    Ordered by how invisible the failure is. A register gap comes first because
    nothing in the reader's own experience reveals it -- they cannot miss what
    they never look at. The rest are visible in their numbers but easy to
    misread, and the reasons are stated so the suggestion can be refused on the
    merits rather than taken on trust.
    """
    progress = ctx.store.all_progress()
    started = sum(1 for state in progress.values()
                  if not state.get("completed_at") and (state.get("position") or 0) > 0.02)
    finished = sum(1 for state in progress.values() if state.get("completed_at"))

    for gap in (gaps or []):
        if gap.get("register") in ("news", "conversation", "fiction", "academic"):
            return _pick("spread", ctx, 3,
                         reason=f"You have never read anything in the {gap['label']} register. "
                                f"{gap['available']} of them are already on your shelves.")

    if started >= 2 and started > finished:
        return _pick("finish", ctx, 2,
                     reason=f"You have {started} articles open and unfinished. "
                            f"Finishing them is the cheapest vocabulary work available.")

    days_read = measure("days", ctx, "")
    if days_read <= 1:
        return _pick("days", ctx, 3,
                     reason="Your reading happens in bursts. Three separate days rehearse "
                            "the same words more times than one long sitting does.")

    if ctx.store.word_count() == 0:
        return _pick("new_words", ctx, 40,
                     reason="You have not saved any words yet, so this counts the ones the "
                            "reading introduces rather than the ones you keep.")

    return _pick("volume", ctx, 6000,
                 reason="A steady week. Raise the target if it looks easy -- the number is "
                        "a floor, not an achievement.")


def _pick(kind_id: str, ctx: Context, target: int, *, reason: str) -> dict[str, Any]:
    kind = BY_ID[kind_id]
    return {"kind": kind_id, "target": target, "title": kind.title.format(n=target),
            "unit": kind.unit, "why": kind.why, "reason": reason}


# --------------------------------------------------------------------------- #
# Progress on the active one
# --------------------------------------------------------------------------- #


def context_for(row: dict[str, Any], ctx: Context) -> Context:
    """The window that belongs to this challenge rather than to today.

    Measuring the last seven days would count two days from before the goal was
    accepted, which flatters a challenge started this morning and is simply wrong
    on the day it is accepted.
    """
    elapsed = WINDOW_DAYS - days_left(row.get("started_at") or "")
    return replace(ctx, days=max(1, min(365, elapsed + 1)))


def report(row: dict[str, Any] | None, ctx: Context) -> dict[str, Any] | None:
    """An active challenge with its progress, or None."""
    if not row:
        return None
    kind = BY_ID.get(row.get("kind") or "")
    if kind is None:
        return None
    target = max(1, int(row.get("target") or 1))
    windowed = context_for(row, ctx)
    done = measure(kind.id, windowed, row.get("started_at") or "")
    left = days_left(row.get("started_at") or "")
    return {
        "id": row["id"],
        "kind": kind.id,
        "title": kind.title.format(n=target),
        "unit": kind.unit,
        "why": kind.why,
        "target": target,
        "done": done,
        "ratio": round(min(1.0, done / target), 3),
        "days_left": left,
        "expired": left < 0 and done < target,
        "completed_at": row.get("completed_at"),
        "started_at": row.get("started_at"),
    }
