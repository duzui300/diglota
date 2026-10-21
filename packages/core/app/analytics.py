"""Reading analytics: what the reading is actually doing to your vocabulary.

The app already knows a great deal it never showed anyone. It records every
lookup, every review, every saved word -- but nothing about the *reading
itself*: how much Spanish passed in front of the reader, how much of it they
already knew, and how often they had to reach for the English.

Those are the numbers that answer the only question worth asking about a
reading app: is the reading working? A learner who reads 12,000 Spanish words a
week and already knows 83% of them is doing something very different from one
who reads 800 and knows 40%.

Three deliberate positions:

*   **"Understood" means already in the deck**, not "did not click". Silence is
    not comprehension -- a reader can pass a word without understanding it and
    without asking. A word in their vocabulary is a claim the app can stand
    behind; a word merely not clicked on is not.
*   **Function words are excluded everywhere.** They are most of any Spanish
    text and none of what anyone is learning. Counting them would make every
    number enormous and meaningless.
*   **Scaffolding is reported as a rate, not a score.** Lookups per thousand
    words read is a measurement; "over-reliant on translation" would be a
    judgement the data does not support.

Nothing here runs on the reading path. Exposure is credited from the periodic
progress save, and the scaffolding counts come from events those endpoints were
already recording.
"""

from __future__ import annotations

from typing import Any

from .glossary import STOPWORDS


def _rate(count: int, tokens: int) -> float:
    """Events per thousand words read -- the only comparable form across days."""
    if tokens <= 0:
        return 0.0
    return round(count / tokens * 1000, 1)


def headline(exposure: dict[str, Any], window: str) -> str:
    """The one sentence worth putting at the top.

    Written here rather than assembled in the browser so the claim and the
    number it is based on cannot drift apart.
    """
    tokens = exposure.get("tokens", 0)
    if not tokens:
        return f"Nothing read {window} yet. Open an article and the numbers start here."
    share = round(exposure.get("understood_ratio", 0.0) * 100)
    words = f"{tokens:,}"
    if exposure.get("articles", 0) == 1:
        return f"You read {words} Spanish words {window}. {share}% were already in your vocabulary."
    return (f"You read {words} Spanish words across {exposure['articles']} articles {window}. "
            f"{share}% were already in your vocabulary.")


def reading_analytics(store: Any, library: Any, *, days: int = 7) -> dict[str, Any]:
    """Everything the Reading Analytics panel shows."""
    windows = {"7": "this week", "30": "this month", "1": "today"}
    window = windows.get(str(days), f"in the last {days} days")

    exposure = store.exposure_totals(days=days)
    split = store.exposure_split(days=days)
    scaffolding = store.scaffolding_summary(days=days)

    by_article: list[dict[str, Any]] = []
    for row in store.exposure_by_article(days=days, limit=10):
        entry = library.get(row["slug"])
        by_article.append({
            **row,
            "title": entry.article.title if entry else row["slug"],
            "understood_ratio": round(row["known"] / row["tokens"], 3) if row["tokens"] else 0.0,
        })

    # Built once: the index walks every article in the library.
    index = library.vocabulary_index()
    top = [
        {**row, "gloss": (index.get(row["lemma"], {}) or {}).get("gloss")}
        for row in store.top_encountered(days=days, limit=14, skip=STOPWORDS)
    ]

    tokens = exposure["tokens"]
    return {
        "window_days": days,
        "window_label": window,
        "headline": headline(exposure, window),
        "exposure": exposure,
        "split": split,
        "top_words": top,
        "by_article": by_article,
        "series": store.exposure_series(days=30),
        "new_words": store.new_words_series(days=60),
        "scaffolding": {
            **scaffolding,
            # Rates, because 40 lookups is a lot in a week of 800 words and
            # nothing in a week of 12,000.
            "lookups_per_1000": _rate(scaffolding["lookups"], tokens),
            "explains_per_1000": _rate(scaffolding["counts"].get("explain", 0), tokens),
            "translations_per_1000": _rate(scaffolding["counts"].get("translate", 0), tokens),
            "deep_entries": scaffolding["counts"].get("full-entry", 0),
            "gloss_reveals": scaffolding["counts"].get("gloss-on", 0),
        },
        "has_data": tokens > 0,
    }


def corpus_analytics(graph: dict[str, Any], store: Any, library: Any) -> dict[str, Any]:
    """The passage graph plus the reading history that belongs on it."""
    progress = store.all_progress()
    read = {slug for slug, state in progress.items() if state.get("completed_at")}
    started = {slug for slug, state in progress.items() if state.get("position", 0) > 0.02}

    nodes = []
    for node in graph["nodes"]:
        nodes.append({
            **node,
            "finished": node["slug"] in read,
            "started": node["slug"] in started,
        })

    # The most useful thing the graph can say beyond "these are related": which
    # passages the reader is already equipped for. Coverage, not similarity.
    ready = sorted(
        (n for n in nodes if not n["finished"]),
        key=lambda n: (-n["coverage"], n["spanish_ratio"]),
    )[:5]

    return {
        **graph,
        "nodes": nodes,
        "read": len(read),
        "started": len(started),
        "within_reach": [
            {"slug": n["slug"], "title": n["title"], "coverage": n["coverage"],
             "shelf": n["shelf"]}
            for n in ready
        ],
    }
