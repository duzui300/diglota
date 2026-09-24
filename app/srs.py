"""Spaced repetition scheduling.

A deliberately plain SM-2. The interesting decisions for a *reading* app are
not in the interval arithmetic but in what gets scheduled and when a card is
allowed to retire:

*   A word met in an article is worth more than a word met in a word list, so
    every card carries the sentence it came from and reviews show that sentence
    rather than an isolated gloss.
*   Four buttons (again / hard / good / easy) map onto SM-2's 0-5 quality
    scale. "Easy" is not just "good but sooner" -- it also raises the ease
    factor, which is what makes a genuinely known word stop appearing.
*   A lapsed card comes back in ten minutes, not tomorrow. Forgetting something
    you just proved you knew is a same-session problem.

The functions here are pure: they take a state and a rating and return a new
state plus a due time. That keeps the whole scheduler testable without a
database or a clock.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

# SM-2 quality scores behind the four buttons the UI offers.
RATINGS: dict[str, int] = {
    "again": 2,  # failed to recall
    "hard": 3,  # recalled, with real difficulty
    "good": 4,  # recalled
    "easy": 5,  # instant
}

MIN_EASE = 1.3
START_EASE = 2.5

# Where a failed card goes. Short enough to come back inside the same sitting.
RELEARN_MINUTES = 10

# Interval by rating, in days. The first two reviews take the rating directly;
# after that the rating scales the growing interval. The numbers are chosen so
# the four buttons are visibly different from the very first press.
_FIRST_INTERVAL = {"hard": 0.5, "good": 1.0, "easy": 4.0}
_SECOND_INTERVAL = {"hard": 2.0, "good": 6.0, "easy": 12.0}
_GROWTH = {"hard": 0.6, "good": 1.0, "easy": 1.35}

_MIN_INTERVAL_DAYS = 0.25

# Never schedule further out than this, so a long absence cannot bury a word.
MAX_INTERVAL_DAYS = 365.0

# A brand-new word the learner has never reviewed.
NEW = "new"
LEARNING = "learning"
YOUNG = "young"
MATURE = "mature"


@dataclass(frozen=True)
class CardState:
    """Everything the scheduler needs to know about one card."""

    ease: float = START_EASE
    interval_days: float = 0.0
    reps: int = 0
    lapses: int = 0

    @property
    def stage(self) -> str:
        if self.reps == 0 and self.lapses == 0:
            return NEW
        if self.interval_days < 1:
            return LEARNING
        return MATURE if self.interval_days >= 21 else YOUNG


def review(state: CardState, rating: str, now: datetime | None = None) -> tuple[CardState, datetime]:
    """Apply one review. Returns the new state and when the card is next due."""
    if rating not in RATINGS:
        raise ValueError(f"unknown rating {rating!r}; expected one of {sorted(RATINGS)}")
    now = now or datetime.now(timezone.utc)
    quality = RATINGS[rating]

    if quality < 3:
        # Forgotten. Keep the ease (a lapse is not evidence the word is hard to
        # *learn*, only that it has not stuck yet) but reset the interval.
        new_state = replace(
            state,
            reps=0,
            lapses=state.lapses + 1,
            interval_days=RELEARN_MINUTES / (60 * 24),
        )
        return new_state, now + timedelta(minutes=RELEARN_MINUTES)

    reps = state.reps + 1

    # The first two intervals are set by the rating rather than derived from
    # ease. SM-2's plain "1 day, then 6" makes the four buttons identical on a
    # new card -- pressing Hard and pressing Good would show the same next-due
    # date, which teaches the learner that the buttons do not mean anything.
    if reps == 1:
        interval = _FIRST_INTERVAL[rating]
    elif reps == 2:
        interval = _SECOND_INTERVAL[rating]
    else:
        # After that the rating scales one growing interval instead of
        # restarting it, so "hard" slows a card down without sending it back.
        interval = state.interval_days * state.ease * _GROWTH[rating]

    interval = min(max(interval, _MIN_INTERVAL_DAYS), MAX_INTERVAL_DAYS)

    ease = state.ease + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    ease = max(MIN_EASE, ease)

    new_state = CardState(ease=ease, interval_days=interval, reps=reps, lapses=state.lapses)
    return new_state, now + timedelta(days=interval)


def preview(state: CardState, now: datetime | None = None) -> dict[str, str]:
    """What each button would do, for labelling them in the UI.

    Showing "good -> 6d" on the button is a small thing that makes the schedule
    legible instead of mysterious, which is most of why people trust or distrust
    a spaced-repetition app.
    """
    now = now or datetime.now(timezone.utc)
    out: dict[str, str] = {}
    for rating in RATINGS:
        _, due = review(state, rating, now)
        out[rating] = humanise(due - now)
    return out


def humanise(delta: timedelta) -> str:
    seconds = max(delta.total_seconds(), 0)
    if seconds < 90:
        return f"{int(seconds // 60) or 1}m"
    minutes = seconds / 60
    if minutes < 90:
        return f"{round(minutes)}m"
    hours = minutes / 60
    if hours < 36:
        return f"{round(hours)}h"
    days = hours / 24
    if days < 30:
        return f"{round(days)}d"
    months = days / 30.44
    if months < 18:
        return f"{round(months)}mo"
    return f"{days / 365:.1f}y"
