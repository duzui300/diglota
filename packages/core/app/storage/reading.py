"""Reading progress, lookups, exposure analytics, scaffolding, and activity events."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..languages import DEFAULT_PAIR_ID
from .db import Database, days_ago, now_iso


class ReadingRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- lookups ---------------------------------------------------------- #

    def log_lookup(
        self, *, term: str, lemma: str | None = None, gloss: str | None = None,
        article_slug: str | None = None, context: str | None = None,
        language_pair: str = DEFAULT_PAIR_ID,
    ) -> None:
        with self._db.write() as conn:
            conn.execute(
                """INSERT INTO lookups (term, lemma, gloss, article_slug, context, at, language_pair)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (term.strip(), (lemma or term).strip().lower(), gloss, article_slug, context,
                 now_iso(), language_pair),
            )

    def frequent_lookups(
        self, *, minimum: int = 2, limit: int = 12,
        skip: frozenset[str] | set[str] = frozenset(),
        language_pair: str | None = None,
    ) -> list[dict[str, Any]]:
        pair_clause = " AND l.language_pair = ?" if language_pair else ""
        params: list[Any] = []
        if language_pair:
            params.append(language_pair)
        params += [minimum, limit * 4 if skip else limit]
        rows = self._db.conn.execute(
            f"""SELECT l.lemma, COUNT(*) AS times,
                       MAX(l.term) AS term, MAX(l.gloss) AS gloss,
                       MAX(l.context) AS context, MAX(l.article_slug) AS article_slug
                  FROM lookups l
                  LEFT JOIN words w ON w.lemma = l.lemma
                 WHERE w.id IS NULL{pair_clause}
                 GROUP BY l.lemma
                HAVING times >= ?
                 ORDER BY times DESC, term ASC
                 LIMIT ?""",
            params,
        ).fetchall()
        out = [dict(r) for r in rows]
        if skip:
            lowered = {word.lower() for word in skip}
            out = [row for row in out if row["lemma"] not in lowered]
        return out[:limit]

    def lookup_count(self, lemma: str) -> int:
        return int(
            self._db.conn.execute(
                "SELECT COUNT(*) FROM lookups WHERE lemma = ?", (lemma.strip().lower(),)
            ).fetchone()[0]
        )

    # -- article progress ------------------------------------------------- #

    def progress_for(self, slug: str) -> dict[str, Any]:
        row = self._db.conn.execute(
            "SELECT * FROM article_progress WHERE slug = ?", (slug,)
        ).fetchone()
        return dict(row) if row else {
            "slug": slug, "position": 0.0, "completed_at": None,
            "last_opened_at": None, "seconds_spent": 0,
        }

    def all_progress(self) -> dict[str, dict[str, Any]]:
        return {r["slug"]: dict(r) for r in self._db.conn.execute("SELECT * FROM article_progress")}

    def save_progress(
        self, slug: str, *, position: float | None = None,
        seconds: int = 0, completed: bool = False,
        language_pair: str = DEFAULT_PAIR_ID,
    ) -> None:
        stamp = now_iso()
        secs = max(0, int(seconds))
        with self._db.write() as conn:
            current = conn.execute(
                "SELECT position FROM article_progress WHERE slug = ?", (slug,)
            ).fetchone()
            cur_pos = float(current["position"]) if current else 0.0
            conn.execute(
                """INSERT INTO article_progress
                   (slug, position, completed_at, last_opened_at, seconds_spent, language_pair)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(slug) DO UPDATE SET
                       position       = excluded.position,
                       completed_at   = COALESCE(excluded.completed_at, article_progress.completed_at),
                       last_opened_at = excluded.last_opened_at,
                       seconds_spent  = article_progress.seconds_spent + ?""",
                (
                    slug,
                    position if position is not None else cur_pos,
                    stamp if completed else None,
                    stamp,
                    secs,
                    language_pair,
                    secs,
                ),
            )

    def credited_position(self, slug: str) -> float:
        row = self._db.conn.execute(
            "SELECT credited_to FROM article_progress WHERE slug = ?", (slug,)
        ).fetchone()
        return float(row[0]) if row else 0.0

    def mark_credited(self, slug: str, position: float) -> None:
        with self._db.write() as conn:
            conn.execute(
                """INSERT INTO article_progress (slug, credited_to) VALUES (?, ?)
                   ON CONFLICT(slug) DO UPDATE SET
                       credited_to = MAX(article_progress.credited_to, excluded.credited_to)""",
                (slug, position),
            )

    # -- reading exposure -------------------------------------------------- #

    def record_exposure(
        self, *, slug: str, lemmas: dict[str, int],
        known_stems: set[str], glossed: set[str],
        language_pair: str = DEFAULT_PAIR_ID,
    ) -> int:
        if not lemmas:
            return 0
        day = now_iso()[:10]
        rows = [
            (
                day, slug, lemma, count,
                1 if lemma in known_stems else 0,
                1 if lemma in glossed else 0,
                day, language_pair,
            )
            for lemma, count in lemmas.items()
        ]
        with self._db.write() as conn:
            conn.executemany(
                """INSERT INTO exposure
                   (day, slug, lemma, times, known, glossed, first_day, language_pair)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(day, slug, lemma) DO UPDATE SET
                       times = exposure.times + excluded.times,
                       known = MAX(exposure.known, excluded.known),
                       glossed = MAX(exposure.glossed, excluded.glossed)""",
                rows,
            )
        return len(rows)

    def exposure_totals(self, *, days: int = 7) -> dict[str, Any]:
        since = days_ago(days)
        row = self._db.conn.execute(
            """SELECT COALESCE(SUM(times), 0)                                  AS tokens,
                      COALESCE(SUM(CASE WHEN known = 1 THEN times END), 0)    AS known,
                      COALESCE(SUM(CASE WHEN glossed = 1 THEN times END), 0)  AS glossed,
                      COUNT(DISTINCT lemma)                                   AS distinct_words,
                      COUNT(DISTINCT slug)                                    AS articles
                 FROM exposure WHERE day >= ?""",
            (since,),
        ).fetchone()
        tokens = int(row["tokens"])
        known = int(row["known"])
        return {
            "days": days,
            "tokens": tokens,
            "known": known,
            "glossed": int(row["glossed"]),
            "distinct_words": int(row["distinct_words"]),
            "articles": int(row["articles"]),
            "understood_ratio": round(known / tokens, 3) if tokens else 0.0,
        }

    def exposure_split(self, *, days: int = 7, repeated_at: int = 4) -> dict[str, Any]:
        since = days_ago(days)
        rows = self._db.conn.execute(
            """SELECT lemma,
                      SUM(CASE WHEN day >= ? THEN times ELSE 0 END) AS recent,
                      SUM(CASE WHEN day <  ? THEN times ELSE 0 END) AS before
                 FROM exposure GROUP BY lemma""",
            (since, since),
        ).fetchall()

        buckets = {"new": 0, "familiar": 0, "repeated": 0}
        for row in rows:
            recent, before = int(row["recent"] or 0), int(row["before"] or 0)
            if not recent:
                continue
            if before == 0:
                buckets["new"] += recent
            elif before + recent >= repeated_at:
                buckets["repeated"] += recent
            else:
                buckets["familiar"] += recent
        total = sum(buckets.values())
        return {
            **buckets,
            "total": total,
            "shares": {key: (round(value / total, 3) if total else 0.0)
                       for key, value in buckets.items()},
        }

    def exposure_series(self, *, days: int = 30) -> list[dict[str, Any]]:
        since = days_ago(days - 1)
        rows = self._db.conn.execute(
            """SELECT day,
                      SUM(times) AS tokens,
                      SUM(CASE WHEN known = 1 THEN times END) AS known,
                      COUNT(DISTINCT lemma) AS distinct_words
                 FROM exposure WHERE day >= ? GROUP BY day""",
            (since,),
        ).fetchall()
        by_day = {row["day"]: row for row in rows}
        out: list[dict[str, Any]] = []
        start = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
        for offset in range(days):
            day = (start + timedelta(days=offset)).isoformat()
            row = by_day.get(day)
            out.append({
                "day": day,
                "tokens": int(row["tokens"] or 0) if row else 0,
                "known": int(row["known"] or 0) if row else 0,
                "distinct_words": int(row["distinct_words"] or 0) if row else 0,
            })
        return out

    def exposure_by_article(self, *, days: int = 30, limit: int = 12) -> list[dict[str, Any]]:
        since = days_ago(days)
        rows = self._db.conn.execute(
            """SELECT slug, SUM(times) AS tokens,
                      SUM(CASE WHEN known = 1 THEN times END) AS known
                 FROM exposure WHERE day >= ?
                GROUP BY slug ORDER BY tokens DESC LIMIT ?""",
            (since, limit),
        ).fetchall()
        return [
            {"slug": row["slug"], "tokens": int(row["tokens"] or 0),
             "known": int(row["known"] or 0)} for row in rows
        ]

    def new_words_series(self, *, days: int = 60) -> list[dict[str, Any]]:
        """Distinct words met for the very first time on each day.

        Computes ``MIN(day)`` per ``lemma`` across the entire exposure history so
        meeting the same word again in a second article or on a later day is
        never double-counted as a brand-new word.
        """
        since = days_ago(days - 1)
        rows = self._db.conn.execute(
            """SELECT first_day AS day, COUNT(*) AS new_words
                 FROM (SELECT lemma, MIN(day) AS first_day FROM exposure GROUP BY lemma)
                WHERE first_day >= ?
                GROUP BY first_day""",
            (since,),
        ).fetchall()
        by_day = {row["day"]: int(row["new_words"]) for row in rows}
        out: list[dict[str, Any]] = []
        start = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
        for offset in range(days):
            day = (start + timedelta(days=offset)).isoformat()
            out.append({"day": day, "new_words": by_day.get(day, 0)})
        return out

    def top_encountered(
        self, *, days: int = 30, limit: int = 20,
        skip: frozenset[str] | set[str] = frozenset(),
    ) -> list[dict[str, Any]]:
        since = days_ago(days)
        rows = self._db.conn.execute(
            """SELECT lemma, SUM(times) AS times,
                      MIN(first_day) AS first_day,
                      MAX(slug) AS slug
                 FROM exposure WHERE day >= ?
                GROUP BY lemma ORDER BY times DESC LIMIT ?""",
            (since, limit * 3 if skip else limit),
        ).fetchall()
        out = [dict(row) for row in rows]
        if skip:
            lowered = {word.lower() for word in skip}
            out = [row for row in out if row["lemma"] not in lowered]
        return out[:limit]

    # -- scaffolding ------------------------------------------------------- #

    def log_scaffolding(
        self, kind: str, *, slug: str | None = None, term: str | None = None,
        language_pair: str = DEFAULT_PAIR_ID,
    ) -> None:
        with self._db.write() as conn:
            conn.execute(
                "INSERT INTO scaffolding (at, kind, slug, term, language_pair) VALUES (?, ?, ?, ?, ?)",
                (now_iso(), kind, slug, term, language_pair),
            )

    def scaffolding_summary(self, *, days: int = 7) -> dict[str, Any]:
        # ISO-8601 timestamps sort lexicographically against YYYY-MM-DD, allowing
        # SQLite to use idx_scaffolding_at directly instead of a full table scan.
        since = days_ago(days)
        rows = self._db.conn.execute(
            "SELECT kind, COUNT(*) AS n FROM scaffolding WHERE at >= ? GROUP BY kind",
            (since,),
        ).fetchall()
        counts = {row["kind"]: int(row["n"]) for row in rows}
        lookups = int(self._db.conn.execute(
            "SELECT COUNT(*) FROM lookups WHERE at >= ?", (since,)
        ).fetchone()[0])
        saved = int(self._db.conn.execute(
            "SELECT COUNT(*) FROM words WHERE created_at >= ?", (since,)
        ).fetchone()[0])
        return {"days": days, "counts": counts, "lookups": lookups, "saved": saved}

    # -- activity & summary stats ----------------------------------------- #

    def log_event(
        self, kind: str, *, slug: str | None = None, amount: float = 0.0,
        language_pair: str = DEFAULT_PAIR_ID,
    ) -> None:
        with self._db.write() as conn:
            conn.execute(
                "INSERT INTO events (at, kind, slug, amount, language_pair) VALUES (?, ?, ?, ?, ?)",
                (now_iso(), kind, slug, amount, language_pair),
            )

    def activity(self, days: int = 30) -> list[dict[str, Any]]:
        since = (datetime.now(timezone.utc) - timedelta(days=days - 1)).date()
        buckets: dict[str, dict[str, float]] = {}
        for row in self._db.conn.execute(
            "SELECT at, kind, amount FROM events WHERE at >= ?", (since.isoformat(),)
        ):
            day = row["at"][:10]
            bucket = buckets.setdefault(day, {"reviews": 0.0, "words_read": 0.0, "events": 0.0})
            bucket["events"] += 1
            if row["kind"] == "review":
                bucket["reviews"] += 1
            elif row["kind"] == "read":
                bucket["words_read"] += row["amount"]
        out: list[dict[str, Any]] = []
        for offset in range(days):
            day = (since + timedelta(days=offset)).isoformat()
            bucket = buckets.get(day, {"reviews": 0.0, "words_read": 0.0, "events": 0.0})
            out.append({"day": day, **bucket})
        return out

    def streak(self) -> dict[str, int]:
        days = {row[0] for row in self._db.conn.execute("SELECT DISTINCT substr(at, 1, 10) FROM events")}
        if not days:
            return {"current": 0, "longest": 0}
        today = datetime.now(timezone.utc).date()
        start = today if today.isoformat() in days else today - timedelta(days=1)
        current = 0
        cursor = start
        while cursor.isoformat() in days:
            current += 1
            cursor -= timedelta(days=1)
        ordered = sorted(days)
        longest = run = 1
        for previous, day in zip(ordered, ordered[1:]):
            gap = (datetime.fromisoformat(day) - datetime.fromisoformat(previous)).days
            run = run + 1 if gap == 1 else 1
            longest = max(longest, run)
        return {"current": current, "longest": max(longest, 1)}

    def stats(self, *, word_count: int, due_count: int, quote_count: int) -> dict[str, Any]:
        stage_rows = self._db.conn.execute(
            """SELECT CASE
                          WHEN c.reps = 0 AND c.lapses = 0 THEN 'new'
                          WHEN c.interval_days < 1 THEN 'learning'
                          WHEN c.interval_days < 21 THEN 'young'
                          ELSE 'mature'
                      END AS stage, COUNT(*) AS n
                 FROM cards c GROUP BY stage"""
        ).fetchall()
        stages = {r["stage"]: r["n"] for r in stage_rows}
        today = datetime.now(timezone.utc).date()
        today_start = today.isoformat()
        tomorrow_start = (today + timedelta(days=1)).isoformat()
        reviews_today = self._db.conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE reviewed_at >= ? AND reviewed_at < ?",
            (today_start, tomorrow_start),
        ).fetchone()[0]
        total_reviews = self._db.conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        completed = self._db.conn.execute(
            "SELECT COUNT(*) FROM article_progress WHERE completed_at IS NOT NULL"
        ).fetchone()[0]
        return {
            "words": word_count,
            "stages": {k: stages.get(k, 0) for k in ("new", "learning", "young", "mature")},
            "due": due_count,
            "reviews_today": reviews_today,
            "reviews_total": total_reviews,
            "articles_completed": completed,
            "quotes": quote_count,
            "streak": self.streak(),
            "activity": self.activity(30),
        }
