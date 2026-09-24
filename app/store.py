"""SQLite persistence.

One file, no server, no ORM. The schema is small enough to read in one go and
the queries are written out in full rather than composed, because at this size
an ORM would be more machinery than the problem has.

Connection handling: FastAPI runs synchronous endpoints on a thread pool, so
each thread gets its own connection via :class:`threading.local`. WAL mode lets
a background AI request read while a review write is in flight.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from . import srs
from .vocab import fold

log = logging.getLogger("diglot.store")

# Letter runs, for whole-word matching. Folding has already stripped accents, so
# ``\w`` would do -- but it would also match digits, and a lemma is never one.
_WORDS = re.compile(r"[^\W\d_]+", re.UNICODE)


def _contains_word(text: str, needle: str) -> bool:
    """Whether folded ``text`` contains ``needle`` as a word rather than inside one.

    A single word has to match a whole word, so looking up *es* does not return
    every quote containing *estas*. A phrase is matched as a substring, because
    its own word boundaries are already in the needle.
    """
    folded = fold(text)
    if " " in needle:
        return needle in folded
    return needle in _WORDS.findall(folded)


def _json_or_empty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False) if value else ""


def _unpack_revision(row: dict[str, Any]) -> dict[str, Any]:
    """One revision, with its JSON columns decoded.

    A column that will not parse becomes empty rather than raising: it is a
    record of something that happened, and an unreadable record should not take
    the reader's own writing down with it.
    """
    for field_name in ("reading", "verdict", "feedback"):
        try:
            row[field_name] = json.loads(row.get(field_name) or "{}") or {}
        except (json.JSONDecodeError, TypeError):
            row[field_name] = {}
    row["checked"] = bool(row.get("checked"))
    return row


def _with_revisions(conn: sqlite3.Connection, piece: dict[str, Any]) -> dict[str, Any]:
    """A piece with its revisions, oldest first, and a summary of where it got to.

    The summary is worked out here rather than stored on the piece, for the same
    reason the quotes table has no folded search column: a denormalised copy of
    "the latest revision" is a thing that goes stale the first time a write
    touches one half of it.
    """
    rows = conn.execute(
        "SELECT * FROM revisions WHERE writing_id = ? ORDER BY id", (piece["id"],)
    ).fetchall()
    revisions = [_unpack_revision(dict(row)) for row in rows]
    latest = revisions[-1] if revisions else None
    piece["revisions"] = revisions
    piece["revision_count"] = len(revisions)
    piece["updated_at"] = piece.get("updated_at") or piece.get("created_at") or ""
    piece["words"] = latest["words"] if latest else 0
    piece["text"] = latest["text"] if latest else ""
    piece["reading"] = latest["reading"] if latest else {}
    piece["verdict"] = latest["verdict"] if latest else {}
    piece["feedback"] = latest["feedback"] if latest else {}
    piece["checked"] = bool(latest["checked"]) if latest else False
    piece["excerpt"] = (latest["text"][:90].strip() if latest else "")
    return piece


def _searchable(row: dict[str, Any]) -> str:
    """Everything a kept sentence can be found by, folded.

    The glosses and the English half are in here on purpose: a learner often
    remembers the meaning and not the Spanish, so "the one about annals" has to
    find the sentence they kept.
    """
    return fold(" ".join(str(row.get(field) or "") for field in
                         ("text", "es", "en", "glosses", "note", "term")))

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS words (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    lemma         TEXT NOT NULL UNIQUE,
    term          TEXT NOT NULL,
    gloss         TEXT,
    pos           TEXT,
    note          TEXT,
    article_slug  TEXT,
    context       TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cards (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    word_id          INTEGER NOT NULL REFERENCES words(id) ON DELETE CASCADE,
    kind             TEXT NOT NULL DEFAULT 'recall',
    ease             REAL NOT NULL DEFAULT 2.5,
    interval_days    REAL NOT NULL DEFAULT 0,
    reps             INTEGER NOT NULL DEFAULT 0,
    lapses           INTEGER NOT NULL DEFAULT 0,
    due_at           TEXT,
    last_reviewed_at TEXT,
    UNIQUE (word_id, kind)
);

CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id     INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    rating      TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    elapsed_ms  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS article_progress (
    slug          TEXT PRIMARY KEY,
    position      REAL NOT NULL DEFAULT 0,
    completed_at  TEXT,
    last_opened_at TEXT,
    seconds_spent INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at     TEXT NOT NULL,
    kind   TEXT NOT NULL,
    slug   TEXT,
    amount REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS quiz_attempts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    slug       TEXT,
    question   TEXT NOT NULL,
    answer     TEXT,
    verdict    TEXT,
    score      REAL,
    feedback   TEXT,
    at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cache (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Every dictionary lookup, saved or not. A word looked up three times and never
-- saved is the clearest signal the app has that the learner is struggling with
-- it, and it is invisible if only saved words are recorded.
CREATE TABLE IF NOT EXISTS lookups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    term       TEXT NOT NULL,
    lemma      TEXT,
    gloss      TEXT,
    article_slug TEXT,
    context    TEXT,
    at         TEXT NOT NULL
);

-- Background jobs. Kept in the database rather than in memory so the Activity
-- panel still shows what happened after a restart -- and so a job that was
-- mid-flight when the server stopped can be reported as interrupted instead of
-- silently vanishing.
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL,
    step        TEXT NOT NULL DEFAULT '',
    done        INTEGER NOT NULL DEFAULT 0,
    total       INTEGER NOT NULL DEFAULT 0,
    detail      TEXT NOT NULL DEFAULT '',
    error       TEXT,
    result      TEXT,
    created_at  TEXT NOT NULL,
    finished_at TEXT
);

-- What the reader actually read past, one row per (day, article, word).
--
-- Aggregated rather than logged per occurrence: a long article is a few hundred
-- distinct words, and one row per reading is enough to answer every question
-- the analytics ask. `first_day` is what separates a word met for the first
-- time from one met for the fifth.
CREATE TABLE IF NOT EXISTS exposure (
    day       TEXT NOT NULL,
    slug      TEXT NOT NULL,
    lemma     TEXT NOT NULL,
    times     INTEGER NOT NULL DEFAULT 0,
    known     INTEGER NOT NULL DEFAULT 0,
    glossed   INTEGER NOT NULL DEFAULT 0,
    first_day TEXT NOT NULL,
    PRIMARY KEY (day, slug, lemma)
);

-- Scaffolding events: the ways a reader asked for help. Kept apart from
-- `lookups`, which is a study signal, because this is a dependence signal.
CREATE TABLE IF NOT EXISTS scaffolding (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    kind    TEXT NOT NULL,        -- gloss-on | full-entry | explain | translate | spanish-only
    slug    TEXT,
    term    TEXT
);

-- Sentences the reader kept. The unit is the sentence rather than the word
-- because that is the unit a language is actually made of, and because a word
-- out of context is a translation while a sentence is a memory.
--
-- ``es`` and ``en`` are the two halves of the diglot sentence, stored separately
-- so the Spanish can be searched and reviewed on its own.
--
-- Nothing here is denormalised for searching. A folded `search` column was the
-- first attempt and it went stale on the first write that touched one field --
-- editing a note rebuilt it without the sentence's glosses and quietly made them
-- unsearchable. Search folds the row's fields at read time instead: a personal
-- collection is hundreds of sentences, not millions, and correctness that is
-- structural beats an index that has to be maintained.
--
-- UNIQUE on the text: the same sentence saved twice is one quote. The reader is
-- told it was already there rather than getting a silent duplicate.
CREATE TABLE IF NOT EXISTS quotes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    text          TEXT NOT NULL UNIQUE,
    es            TEXT NOT NULL,
    en            TEXT NOT NULL DEFAULT '',
    glosses       TEXT NOT NULL DEFAULT '',
    note          TEXT,
    article_slug  TEXT,
    block_index   INTEGER,
    start         INTEGER,
    end           INTEGER,
    term          TEXT,
    created_at    TEXT NOT NULL
);

-- A goal the reader accepted, and its window. One active at a time: a list of
-- goals is a list of things not done. ``ended_at`` without ``completed_at`` is a
-- week that ran out, or a goal replaced by another one -- kept, because "I tried
-- this and did not finish" is information about the reader rather than a failure
-- to be hidden.
CREATE TABLE IF NOT EXISTS challenges (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    target       INTEGER NOT NULL,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    completed_at TEXT
);

-- Writing practice. A *piece* is something someone is working on; its
-- *revisions* are what it has been through. Two tables rather than one because
-- the revision loop is the point: draft, read it, revise, read it again, and
-- each reading belongs to the exact text it judged.
--
-- Nothing about a piece is derived and cached -- no "latest text" column on
-- ``writings``. That denormalisation is what went stale in the quotes table, and
-- the list of pieces is short enough to aggregate in Python.
CREATE TABLE IF NOT EXISTS writings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL DEFAULT '',
    prompt     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    writing_id INTEGER NOT NULL REFERENCES writings(id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    reading    TEXT NOT NULL DEFAULT '',    -- JSON: length, language mix, words used
    verdict    TEXT NOT NULL DEFAULT '',    -- JSON: the graded verdict, when there was one
    feedback   TEXT NOT NULL DEFAULT '',    -- JSON: summary, notes, corrections, next
    words      INTEGER NOT NULL DEFAULT 0,
    -- Whether a model read this revision, or it was only kept. The difference
    -- matters: "not checked" and "checked and clean" look identical otherwise.
    checked    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_revisions_writing ON revisions (writing_id, id);
CREATE INDEX IF NOT EXISTS idx_cards_due ON cards (due_at);
CREATE INDEX IF NOT EXISTS idx_events_at ON events (at);
CREATE INDEX IF NOT EXISTS idx_reviews_at ON reviews (reviewed_at);
CREATE INDEX IF NOT EXISTS idx_lookups_lemma ON lookups (lemma);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_exposure_day ON exposure (day);
CREATE INDEX IF NOT EXISTS idx_exposure_lemma ON exposure (lemma);
CREATE INDEX IF NOT EXISTS idx_scaffolding_at ON scaffolding (at);
CREATE INDEX IF NOT EXISTS idx_quotes_slug ON quotes (article_slug);
CREATE INDEX IF NOT EXISTS idx_quotes_created ON quotes (created_at DESC);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _days_ago(days: int) -> str:
    """The date ``days`` days back, as ``YYYY-MM-DD``.

    Inclusive of today, so ``_days_ago(6)`` is the start of a seven-day window.
    """
    return (datetime.now(timezone.utc).date() - timedelta(days=max(days, 0))).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns that ``CREATE TABLE IF NOT EXISTS`` cannot.

        An existing database keeps its table definition, so a new column has to
        be added explicitly. Cheap enough to check on every startup, and it
        means the app never needs the user to delete their data.
        """
        columns = {row[1] for row in conn.execute("PRAGMA table_info(article_progress)")}
        if "credited_to" not in columns:
            # The furthest point whose words have been counted as read, so a
            # reader scrolling back and forth does not inflate the total.
            conn.execute("ALTER TABLE article_progress ADD COLUMN credited_to REAL NOT NULL DEFAULT 0")

        # Writing used to be one row per piece holding a single version. Now a
        # piece has revisions, so an existing row becomes the piece plus its
        # first revision -- nothing is thrown away, and the columns that held the
        # version are dropped once their data has moved.
        writing_columns = {row[1] for row in conn.execute("PRAGMA table_info(writings)")}
        if "text" in writing_columns:
            conn.execute("ALTER TABLE writings ADD COLUMN title TEXT NOT NULL DEFAULT ''")
            conn.execute("ALTER TABLE writings ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
            for row in conn.execute("SELECT * FROM writings").fetchall():
                conn.execute(
                    """INSERT INTO revisions (writing_id, text, reading, verdict, feedback,
                                              words, checked, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (row["id"], row["text"], row["reading"], row["verdict"], row["feedback"],
                     row["words"], 1, row["created_at"]),
                )
            conn.execute("UPDATE writings SET updated_at = created_at WHERE updated_at = ''")
            for dead in ("text", "reading", "verdict", "feedback", "words"):
                try:
                    conn.execute(f"ALTER TABLE writings DROP COLUMN {dead}")
                except sqlite3.OperationalError:
                    # Older SQLite cannot drop columns. Harmless: nothing reads
                    # them any more, and the data is in `revisions`.
                    log.info("could not drop writings.%s; it is unused", dead)

    # -- plumbing --------------------------------------------------------- #

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """A transaction. Commits on success, rolls back on any exception."""
        conn = self.conn
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        conn.commit()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- vocabulary ------------------------------------------------------- #

    def add_word(
        self,
        *,
        term: str,
        gloss: str | None = None,
        lemma: str | None = None,
        pos: str | None = None,
        note: str | None = None,
        article_slug: str | None = None,
        context: str | None = None,
    ) -> dict[str, Any]:
        """Save a word and create its cards. Saving an existing word is a no-op
        that returns the existing row -- the reader is a one-click save button
        and clicking it twice should not create a duplicate."""
        key = (lemma or term).strip().lower()
        existing = self.conn.execute("SELECT * FROM words WHERE lemma = ?", (key,)).fetchone()
        if existing:
            return dict(existing)

        with self.write() as conn:
            cursor = conn.execute(
                """INSERT INTO words (lemma, term, gloss, pos, note, article_slug, context, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (key, term.strip(), gloss, pos, note, article_slug, context, now_iso()),
            )
            word_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO cards (word_id, kind, due_at) VALUES (?, 'recall', ?)",
                (word_id, now_iso()),
            )
        return dict(self.conn.execute("SELECT * FROM words WHERE id = ?", (word_id,)).fetchone())

    def list_words(self, *, limit: int = 500, search: str | None = None) -> list[dict[str, Any]]:
        sql = """
            SELECT w.*, c.ease, c.interval_days, c.reps, c.lapses, c.due_at, c.last_reviewed_at
              FROM words w LEFT JOIN cards c ON c.word_id = w.id AND c.kind = 'recall'
        """
        params: list[Any] = []
        if search:
            sql += " WHERE w.term LIKE ? OR w.gloss LIKE ?"
            params += [f"%{search}%", f"%{search}%"]
        sql += " ORDER BY w.created_at DESC LIMIT ?"
        params.append(limit)
        return [self._word_row(row) for row in self.conn.execute(sql, params)]

    def word_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM words").fetchone()[0])

    def known_terms(self) -> set[str]:
        """Every lemma the learner has saved, for coverage calculations."""
        return {row[0] for row in self.conn.execute("SELECT lemma FROM words")}

    def delete_word(self, word_id: int) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM words WHERE id = ?", (word_id,))

    def update_word(self, word_id: int, **fields: Any) -> None:
        allowed = {"gloss", "pos", "note"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        clause = ", ".join(f"{k} = ?" for k in updates)
        with self.write() as conn:
            conn.execute(f"UPDATE words SET {clause} WHERE id = ?", (*updates.values(), word_id))

    @staticmethod
    def _word_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        state = srs.CardState(
            ease=data.get("ease") or srs.START_EASE,
            interval_days=data.get("interval_days") or 0.0,
            reps=data.get("reps") or 0,
            lapses=data.get("lapses") or 0,
        )
        data["stage"] = state.stage
        data["preview"] = srs.preview(state)
        return data

    # -- review queue ----------------------------------------------------- #

    def due_cards(self, *, limit: int = 40, include_new: int = 12) -> list[dict[str, Any]]:
        """Cards to review now: everything already in rotation, plus a trickle
        of new words.

        The two are queried separately on purpose. A new card is due the moment
        it is created, so asking for "everything due" would return the entire
        deck in one go and a bulk import would bury the learner under hundreds
        of unseen cards. New words are therefore excluded from the due query and
        added afterwards under their own cap.
        """
        stamp = now_iso()
        due = self.conn.execute(
            """SELECT c.id AS card_id, c.due_at, c.ease, c.interval_days, c.reps, c.lapses,
                      w.id AS word_id, w.term, w.lemma, w.gloss, w.pos, w.note,
                      w.context, w.article_slug
                 FROM cards c JOIN words w ON w.id = c.word_id
                WHERE c.due_at IS NOT NULL AND c.due_at <= ?
                  AND NOT (c.reps = 0 AND c.lapses = 0)
                ORDER BY c.due_at ASC LIMIT ?""",
            (stamp, limit),
        ).fetchall()
        rows = [self._card_row(r) for r in due]

        if include_new and len(rows) < limit:
            fresh = self.conn.execute(
                """SELECT c.id AS card_id, c.due_at, c.ease, c.interval_days, c.reps, c.lapses,
                          w.id AS word_id, w.term, w.lemma, w.gloss, w.pos, w.note,
                      w.context, w.article_slug
                     FROM cards c JOIN words w ON w.id = c.word_id
                    WHERE c.reps = 0 AND c.lapses = 0
                    ORDER BY w.created_at DESC LIMIT ?""",
                (min(include_new, limit - len(rows)),),
            ).fetchall()
            seen = {r["card_id"] for r in rows}
            rows += [self._card_row(r) for r in fresh if r["card_id"] not in seen]
        return rows

    @staticmethod
    def _card_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        state = srs.CardState(
            ease=data["ease"], interval_days=data["interval_days"], reps=data["reps"], lapses=data["lapses"]
        )
        data["stage"] = state.stage
        data["preview"] = srs.preview(state)
        return data

    def review_card(self, card_id: int, rating: str, *, elapsed_ms: int = 0) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        if row is None:
            raise KeyError(f"no card {card_id}")
        state = srs.CardState(
            ease=row["ease"], interval_days=row["interval_days"], reps=row["reps"], lapses=row["lapses"]
        )
        new_state, due = srs.review(state, rating)
        stamp = now_iso()
        with self.write() as conn:
            conn.execute(
                """UPDATE cards SET ease = ?, interval_days = ?, reps = ?, lapses = ?,
                          due_at = ?, last_reviewed_at = ? WHERE id = ?""",
                (new_state.ease, new_state.interval_days, new_state.reps, new_state.lapses,
                 due.isoformat(timespec="seconds"), stamp, card_id),
            )
            conn.execute(
                "INSERT INTO reviews (card_id, rating, reviewed_at, elapsed_ms) VALUES (?, ?, ?, ?)",
                (card_id, rating, stamp, elapsed_ms),
            )
        self.log_event("review", amount=1)
        return {
            "card_id": card_id,
            "due_at": due.isoformat(timespec="seconds"),
            "interval_days": new_state.interval_days,
            "ease": new_state.ease,
            "stage": new_state.stage,
            "preview": srs.preview(new_state),
        }

    def due_count(self) -> int:
        """Cards already in rotation that are due. Brand-new words are not
        counted -- they are introduced on a budget, not owed."""
        return int(
            self.conn.execute(
                """SELECT COUNT(*) FROM cards
                    WHERE due_at IS NOT NULL AND due_at <= ?
                      AND NOT (reps = 0 AND lapses = 0)""",
                (now_iso(),),
            ).fetchone()[0]
        )

    # -- lookups ---------------------------------------------------------- #

    def log_lookup(
        self, *, term: str, lemma: str | None = None, gloss: str | None = None,
        article_slug: str | None = None, context: str | None = None,
    ) -> None:
        """Record that a word was looked up, whether or not it was saved."""
        with self.write() as conn:
            conn.execute(
                """INSERT INTO lookups (term, lemma, gloss, article_slug, context, at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (term.strip(), (lemma or term).strip().lower(), gloss, article_slug, context, now_iso()),
            )

    def frequent_lookups(self, *, minimum: int = 2, limit: int = 12,
                         skip: frozenset[str] | set[str] = frozenset()) -> list[dict[str, Any]]:
        """Words looked up repeatedly and never saved.

        This is a study list the learner did not have to build: needing the same
        word three times is a stronger signal than any self-assessment.

        ``skip`` is for function words. Without it the list is dominated by
        "del" and "a" -- which are genuinely the most-looked-up *tokens* and are
        precisely the ones nobody wants a flashcard for. The caller supplies
        them because this layer knows SQLite and not Spanish.
        """
        rows = self.conn.execute(
            """SELECT l.lemma, COUNT(*) AS times,
                      MAX(l.term) AS term, MAX(l.gloss) AS gloss,
                      MAX(l.context) AS context, MAX(l.article_slug) AS article_slug
                 FROM lookups l
                 LEFT JOIN words w ON w.lemma = l.lemma
                WHERE w.id IS NULL
                GROUP BY l.lemma
               HAVING times >= ?
                ORDER BY times DESC, term ASC
                LIMIT ?""",
            (minimum, limit * 4 if skip else limit),
        ).fetchall()
        out = [dict(r) for r in rows]
        if skip:
            lowered = {word.lower() for word in skip}
            out = [row for row in out if row["lemma"] not in lowered]
        return out[:limit]

    def lookup_count(self, lemma: str) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM lookups WHERE lemma = ?", (lemma.strip().lower(),)
            ).fetchone()[0]
        )

    # -- article progress ------------------------------------------------- #

    def progress_for(self, slug: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM article_progress WHERE slug = ?", (slug,)).fetchone()
        return dict(row) if row else {"slug": slug, "position": 0.0, "completed_at": None,
                                      "last_opened_at": None, "seconds_spent": 0}

    def all_progress(self) -> dict[str, dict[str, Any]]:
        return {r["slug"]: dict(r) for r in self.conn.execute("SELECT * FROM article_progress")}

    def save_progress(
        self, slug: str, *, position: float | None = None,
        seconds: int = 0, completed: bool = False,
    ) -> None:
        current = self.progress_for(slug)
        with self.write() as conn:
            conn.execute(
                """INSERT INTO article_progress (slug, position, completed_at, last_opened_at, seconds_spent)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(slug) DO UPDATE SET
                       position       = excluded.position,
                       completed_at   = COALESCE(excluded.completed_at, article_progress.completed_at),
                       last_opened_at = excluded.last_opened_at,
                       seconds_spent  = article_progress.seconds_spent + ?""",
                (
                    slug,
                    position if position is not None else current["position"],
                    now_iso() if completed else None,
                    now_iso(),
                    seconds,
                    seconds,
                ),
            )

    # -- activity --------------------------------------------------------- #

    def log_event(self, kind: str, *, slug: str | None = None, amount: float = 0.0) -> None:
        with self.write() as conn:
            conn.execute(
                "INSERT INTO events (at, kind, slug, amount) VALUES (?, ?, ?, ?)",
                (now_iso(), kind, slug, amount),
            )

    def activity(self, days: int = 30) -> list[dict[str, Any]]:
        """Per-day counts, oldest first, with empty days filled in."""
        since = (datetime.now(timezone.utc) - timedelta(days=days - 1)).date()
        buckets: dict[str, dict[str, float]] = {}
        for row in self.conn.execute(
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
        """Consecutive days with any activity, counting back from today.

        Yesterday counts as the anchor if today has nothing yet, so the streak
        does not visibly break at midnight before the learner has read.
        """
        days = {row[0] for row in self.conn.execute("SELECT DISTINCT substr(at, 1, 10) FROM events")}
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

    def stats(self) -> dict[str, Any]:
        total_words = self.word_count()
        stage_rows = self.conn.execute(
            """SELECT CASE
                          WHEN c.reps = 0 AND c.lapses = 0 THEN 'new'
                          WHEN c.interval_days < 1 THEN 'learning'
                          WHEN c.interval_days < 21 THEN 'young'
                          ELSE 'mature'
                      END AS stage, COUNT(*) AS n
                 FROM cards c GROUP BY stage"""
        ).fetchall()
        stages = {r["stage"]: r["n"] for r in stage_rows}
        reviews_today = self.conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE substr(reviewed_at, 1, 10) = ?",
            (datetime.now(timezone.utc).date().isoformat(),),
        ).fetchone()[0]
        total_reviews = self.conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        completed = self.conn.execute(
            "SELECT COUNT(*) FROM article_progress WHERE completed_at IS NOT NULL"
        ).fetchone()[0]
        return {
            "words": total_words,
            "stages": {k: stages.get(k, 0) for k in ("new", "learning", "young", "mature")},
            "due": self.due_count(),
            "reviews_today": reviews_today,
            "reviews_total": total_reviews,
            "articles_completed": completed,
            "quotes": self.quote_count(),
            "streak": self.streak(),
            "activity": self.activity(30),
        }

    # -- quizzes ---------------------------------------------------------- #

    def save_attempt(
        self, *, slug: str | None, question: str, answer: str | None,
        verdict: str | None, score: float | None, feedback: str | None,
    ) -> None:
        with self.write() as conn:
            conn.execute(
                """INSERT INTO quiz_attempts (slug, question, answer, verdict, score, feedback, at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (slug, question, answer, verdict, score, feedback, now_iso()),
            )
        self.log_event("quiz", slug=slug, amount=1)

    def attempts_for(self, slug: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM quiz_attempts WHERE slug = ? ORDER BY at DESC LIMIT 50", (slug,)
            )
        ]

    # -- AI result cache -------------------------------------------------- #

    def cache_get(self, key: str, *, max_age_seconds: int | None = None) -> Any | None:
        """Read a cached value, optionally ignoring entries older than a limit.

        The age limit exists for negative caching: a failed lookup is worth
        remembering for a few minutes so a reader clicking the same word again
        does not pay another full timeout, but not worth remembering forever.
        """
        row = self.conn.execute("SELECT value, created_at FROM cache WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        if max_age_seconds is not None:
            stored = parse_iso(row["created_at"])
            if stored is not None:
                age = (datetime.now(timezone.utc) - stored).total_seconds()
                if age > max_age_seconds:
                    return None
        return json.loads(row["value"])

    def cache_put(self, key: str, value: Any) -> None:
        with self.write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, value, created_at) VALUES (?, ?, ?)",
                (key, json.dumps(value, ensure_ascii=False), now_iso()),
            )

    def cache_forget(self, key: str) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM cache WHERE key = ?", (key,))

    # -- background jobs -------------------------------------------------- #

    def save_job(self, job: dict[str, Any]) -> None:
        with self.write() as conn:
            conn.execute(
                """INSERT INTO jobs (id, kind, title, status, step, done, total, detail,
                                     error, result, created_at, finished_at)
                   VALUES (:id, :kind, :title, :status, :step, :done, :total, :detail,
                           :error, :result, :created_at, :finished_at)
                   ON CONFLICT(id) DO UPDATE SET
                       status = excluded.status, step = excluded.step,
                       done = excluded.done, total = excluded.total,
                       detail = excluded.detail, error = excluded.error,
                       result = excluded.result, finished_at = excluded.finished_at""",
                {**job, "result": json.dumps(job.get("result"), ensure_ascii=False)
                 if job.get("result") is not None else None},
            )

    def list_jobs(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            if data.get("result"):
                try:
                    data["result"] = json.loads(data["result"])
                except json.JSONDecodeError:
                    data["result"] = None
            out.append(data)
        return out

    def delete_job(self, job_id: str) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def clear_finished_jobs(self) -> int:
        with self.write() as conn:
            cursor = conn.execute("DELETE FROM jobs WHERE status != 'running' AND status != 'queued'")
            return cursor.rowcount

    # -- reading exposure -------------------------------------------------- #

    def credited_position(self, slug: str) -> float:
        """How far through an article its words have already been counted."""
        row = self.conn.execute(
            "SELECT credited_to FROM article_progress WHERE slug = ?", (slug,)
        ).fetchone()
        return float(row[0]) if row else 0.0

    def record_exposure(self, *, slug: str, lemmas: dict[str, int],
                        known_stems: set[str], glossed: set[str]) -> int:
        """Credit the Spanish words read between two positions.

        One upsert per distinct word rather than one per occurrence: a long
        article yields a few hundred rows, and ``times`` carries the count.
        ``first_day`` is written once and never moved, which is what makes
        "met for the first time" answerable later.
        """
        if not lemmas:
            return 0
        day = now_iso()[:10]
        rows = [
            (day, slug, lemma, count,
             1 if lemma in known_stems else 0,
             1 if lemma in glossed else 0,
             day)
            for lemma, count in lemmas.items()
        ]
        with self.write() as conn:
            conn.executemany(
                """INSERT INTO exposure (day, slug, lemma, times, known, glossed, first_day)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(day, slug, lemma) DO UPDATE SET
                       times = exposure.times + excluded.times,
                       known = MAX(exposure.known, excluded.known),
                       glossed = MAX(exposure.glossed, excluded.glossed)""",
                rows,
            )
        return len(rows)

    def mark_credited(self, slug: str, position: float) -> None:
        with self.write() as conn:
            conn.execute(
                """INSERT INTO article_progress (slug, credited_to) VALUES (?, ?)
                   ON CONFLICT(slug) DO UPDATE SET
                       credited_to = MAX(article_progress.credited_to, excluded.credited_to)""",
                (slug, position),
            )

    def exposure_totals(self, *, days: int = 7) -> dict[str, Any]:
        """Headline exposure numbers for the last N days.

        ``understood`` counts tokens whose stem is already in the deck: read
        without needing the English. ``glossed`` is the article translating
        inline, which is still scaffolding, just supplied by the text rather
        than asked for.
        """
        since = _days_ago(days)
        row = self.conn.execute(
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
        """How much of the reading was new, familiar, or already seen often.

        The three buckets are decided by the word's history *outside* the
        window, so a word read for the first time today is new even though it
        has a row today.
        """
        since = _days_ago(days)
        rows = self.conn.execute(
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
        """Per-day Spanish tokens read, with the share already known."""
        since = _days_ago(days - 1)
        rows = self.conn.execute(
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
        since = _days_ago(days)
        rows = self.conn.execute(
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
        """Distinct words met for the first time on each day.

        Derived from ``first_day`` rather than from the deck: a word the reader
        met but never saved is still part of their exposure, and counting only
        saved words would understate what the reading is doing.
        """
        since = _days_ago(days - 1)
        rows = self.conn.execute(
            """SELECT first_day AS day, COUNT(*) AS new_words
                 FROM exposure WHERE first_day >= ? GROUP BY first_day""",
            (since,),
        ).fetchall()
        by_day = {row["day"]: int(row["new_words"]) for row in rows}
        out: list[dict[str, Any]] = []
        start = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
        for offset in range(days):
            day = (start + timedelta(days=offset)).isoformat()
            out.append({"day": day, "new_words": by_day.get(day, 0)})
        return out

    def top_encountered(self, *, days: int = 30, limit: int = 20,
                        skip: frozenset[str] | set[str] = frozenset()) -> list[dict[str, Any]]:
        """The words met most often -- the ones the reading is drilling."""
        since = _days_ago(days)
        rows = self.conn.execute(
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

    def log_scaffolding(self, kind: str, *, slug: str | None = None, term: str | None = None) -> None:
        with self.write() as conn:
            conn.execute(
                "INSERT INTO scaffolding (at, kind, slug, term) VALUES (?, ?, ?, ?)",
                (now_iso(), kind, slug, term),
            )

    def scaffolding_summary(self, *, days: int = 7) -> dict[str, Any]:
        since = _days_ago(days)
        rows = self.conn.execute(
            """SELECT kind, COUNT(*) AS n FROM scaffolding
                WHERE substr(at, 1, 10) >= ? GROUP BY kind""",
            (since,),
        ).fetchall()
        counts = {row["kind"]: int(row["n"]) for row in rows}
        lookups = int(self.conn.execute(
            "SELECT COUNT(*) FROM lookups WHERE substr(at, 1, 10) >= ?", (since,)
        ).fetchone()[0])
        saved = int(self.conn.execute(
            "SELECT COUNT(*) FROM words WHERE substr(created_at, 1, 10) >= ?", (since,)
        ).fetchone()[0])
        return {"days": days, "counts": counts, "lookups": lookups, "saved": saved}

    # -- quotes ----------------------------------------------------------- #

    # How many kept sentences a search will scan. A personal collection is
    # hundreds, so this is generous rather than tight -- but a limit that is not
    # stated is a limit that quietly hides results, so it is stated here.
    QUOTE_SCAN = 5000

    def save_quote(
        self, *, text: str, es: str, en: str = "", glosses: str = "", note: str | None = None,
        article_slug: str | None = None, block_index: int | None = None,
        start: int | None = None, end: int | None = None, term: str | None = None,
    ) -> tuple[int, bool]:
        """Keep a sentence. Returns ``(id, was_new)``.

        The same sentence twice is one quote, and the caller is told which
        happened: silently returning the existing row would look like a save that
        worked, and silently inserting a duplicate would fill the collection with
        things the reader thought they had already kept.
        """
        with self.write() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO quotes
                   (text, es, en, glosses, note, article_slug, block_index,
                    start, end, term, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (text, es, en, glosses, note, article_slug, block_index,
                 start, end, term, now_iso()),
            )
            created = cursor.rowcount > 0
            if created:
                quote_id = int(cursor.lastrowid or 0)
            else:
                row = conn.execute("SELECT id FROM quotes WHERE text = ?", (text,)).fetchone()
                quote_id = int(row["id"]) if row else 0
        if created:
            self.log_event("quote", slug=article_slug, amount=1)
        return quote_id, created

    def quotes(self, *, search: str | None = None, slug: str | None = None,
               term: str | None = None, limit: int = 400) -> list[dict[str, Any]]:
        """Kept sentences, newest first, optionally narrowed.

        Filtering happens here rather than in SQL because the searchable text is
        spread across five columns and one of them is folded -- see the schema
        comment for why that is not denormalised into a column of its own.
        """
        rows = self.conn.execute(
            "SELECT * FROM quotes ORDER BY created_at DESC, id DESC LIMIT ?",
            (self.QUOTE_SCAN,),
        ).fetchall()
        out = [dict(row) for row in rows]
        if slug:
            out = [row for row in out if (row.get("article_slug") or "") == slug]
        if term:
            out = [row for row in out if (row.get("term") or "") == term]
        if search:
            needle = fold(search)
            out = [row for row in out if needle in _searchable(row)]
        return out[:limit]

    def quote(self, quote_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
        return dict(row) if row else None

    def set_quote_note(self, quote_id: int, note: str) -> bool:
        with self.write() as conn:
            cursor = conn.execute("UPDATE quotes SET note = ? WHERE id = ?", (note, quote_id))
            return cursor.rowcount > 0

    def delete_quote(self, quote_id: int) -> bool:
        with self.write() as conn:
            cursor = conn.execute("DELETE FROM quotes WHERE id = ?", (quote_id,))
            return cursor.rowcount > 0

    def quote_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0])

    def quotes_using(self, term: str, *, limit: int = 6) -> list[dict[str, Any]]:
        """Quotes whose own Spanish contains a word -- the connection that makes a
        collection worth having while you are still reading."""
        needle = fold(term)
        if len(needle) < 3:
            return []
        rows = self.conn.execute(
            "SELECT * FROM quotes ORDER BY created_at DESC LIMIT ?", (self.QUOTE_SCAN,)
        ).fetchall()
        return [dict(row) for row in rows if _contains_word(row["es"], needle)][:limit]

    # -- challenges ------------------------------------------------------- #

    def start_challenge(self, kind: str, target: int) -> dict[str, Any]:
        """Begin a goal, ending whatever was active.

        Starting a new one replaces the old rather than refusing: the reader
        changed their mind, which is theirs to do. The replaced row is closed
        without a completion time so the history stays truthful.
        """
        now = now_iso()
        with self.write() as conn:
            conn.execute("UPDATE challenges SET ended_at = ? WHERE ended_at IS NULL", (now,))
            cursor = conn.execute(
                "INSERT INTO challenges (kind, target, started_at) VALUES (?, ?, ?)",
                (kind, max(1, int(target)), now),
            )
            row = conn.execute("SELECT * FROM challenges WHERE id = ?",
                               (cursor.lastrowid,)).fetchone()
        self.log_event("challenge", amount=1)
        return dict(row)

    def active_challenge(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM challenges WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def complete_challenge(self, challenge_id: int) -> bool:
        """Record that the goal was reached.

        Deliberately does *not* end the challenge. Reaching a goal is worth
        showing, and ending it in the same breath would hide the one moment the
        feature exists for -- the row stays active until the reader moves on.
        """
        with self.write() as conn:
            cursor = conn.execute(
                "UPDATE challenges SET completed_at = ? WHERE id = ? AND completed_at IS NULL",
                (now_iso(), challenge_id),
            )
            return cursor.rowcount > 0

    def end_challenge(self, challenge_id: int) -> bool:
        """Stop showing a goal: given up on, or replaced by another one."""
        with self.write() as conn:
            cursor = conn.execute(
                "UPDATE challenges SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                (now_iso(), challenge_id),
            )
            return cursor.rowcount > 0

    def challenge_history(self, *, limit: int = 12) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM challenges ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    # -- writing ---------------------------------------------------------- #

    def start_writing(self, *, title: str, prompt: str = "") -> dict[str, Any]:
        """Open a piece. A piece with no revisions yet is just a title and a prompt."""
        now = now_iso()
        with self.write() as conn:
            cursor = conn.execute(
                "INSERT INTO writings (title, prompt, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (title, prompt, now, now),
            )
            row = conn.execute("SELECT * FROM writings WHERE id = ?",
                               (cursor.lastrowid,)).fetchone()
        return dict(row)

    def add_revision(
        self, writing_id: int, *, text: str, reading: dict | None = None,
        verdict: dict | None = None, feedback: dict | None = None,
        words: int = 0, checked: bool = False,
    ) -> dict[str, Any] | None:
        """Record a version of a piece.

        The same text twice updates the revision rather than making another one:
        checking a draft, editing a word and checking again should not read as
        three versions when only two were written, and the notes from the first
        check would be attached to text they no longer describe.
        """
        with self.write() as conn:
            exists = conn.execute("SELECT id FROM writings WHERE id = ?", (writing_id,)).fetchone()
            if exists is None:
                return None
            last = conn.execute(
                "SELECT * FROM revisions WHERE writing_id = ? ORDER BY id DESC LIMIT 1",
                (writing_id,),
            ).fetchone()
            if last is not None and last["text"] == text:
                conn.execute(
                    """UPDATE revisions SET reading = ?, verdict = ?, feedback = ?, words = ?,
                                            checked = ?, created_at = ?
                        WHERE id = ?""",
                    (_json_or_empty(reading), _json_or_empty(verdict), _json_or_empty(feedback),
                     max(0, int(words)), 1 if checked else int(last["checked"]),
                     last["created_at"], last["id"]),
                )
                revision_id = int(last["id"])
            else:
                cursor = conn.execute(
                    """INSERT INTO revisions (writing_id, text, reading, verdict, feedback,
                                              words, checked, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (writing_id, text, _json_or_empty(reading), _json_or_empty(verdict),
                     _json_or_empty(feedback), max(0, int(words)), 1 if checked else 0, now_iso()),
                )
                revision_id = int(cursor.lastrowid or 0)
            conn.execute("UPDATE writings SET updated_at = ? WHERE id = ?", (now_iso(), writing_id))
            row = conn.execute("SELECT * FROM revisions WHERE id = ?", (revision_id,)).fetchone()
        self.log_event("writing", amount=max(0, int(words)))
        return _unpack_revision(dict(row)) if row else None

    def writings(self, *, limit: int = 60) -> list[dict[str, Any]]:
        """Pieces, newest first, each with a summary of where it got to."""
        rows = self.conn.execute(
            "SELECT * FROM writings ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_with_revisions(self.conn, dict(row)) for row in rows]

    def writing(self, writing_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM writings WHERE id = ?", (writing_id,)).fetchone()
        return _with_revisions(self.conn, dict(row)) if row else None

    def latest_revision(self, writing_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM revisions WHERE writing_id = ? ORDER BY id DESC LIMIT 1", (writing_id,)
        ).fetchone()
        return _unpack_revision(dict(row)) if row else None

    def delete_writing(self, writing_id: int) -> bool:
        with self.write() as conn:
            # Explicit rather than trusting ON DELETE CASCADE: the pragma that
            # enables it is per-connection, and losing the revisions of a piece
            # that appeared to be deleted would be a silent data loss.
            conn.execute("DELETE FROM revisions WHERE writing_id = ?", (writing_id,))
            return conn.execute("DELETE FROM writings WHERE id = ?", (writing_id,)).rowcount > 0

    def writing_totals(self) -> dict[str, Any]:
        """How much has been written, across every version of every piece.

        Per *revision*, deliberately: the words in a draft that was later revised
        are words that were written. Counting only the final text would make
        rewriting look like it cost nothing.
        """
        rows = self.conn.execute("SELECT reading, words FROM revisions").fetchall()
        pieces = int(self.conn.execute("SELECT COUNT(*) FROM writings").fetchone()[0])
        spanish = total = 0
        for row in rows:
            try:
                reading = json.loads(row["reading"] or "{}")
            except json.JSONDecodeError:
                reading = {}
            spanish += int(reading.get("spanish_words") or 0)
            total += int(reading.get("words") or 0)
        return {
            "pieces": pieces,
            "revisions": len(rows),
            "words": sum(int(row["words"] or 0) for row in rows),
            "spanish_words": spanish,
            "measured_words": total,
            "spanish_share": round(spanish / total, 3) if total else 0.0,
        }

    # -- maintenance ------------------------------------------------------ #

    def reset(self, keep_words: bool = False) -> None:
        """Clear everything the reader has accumulated.

        The lessons themselves are files, not rows, so they stay: this is the
        history, not the library. ``keep_words`` keeps the deck and drops only the
        schedule and everything measured from it.
        """
        tables = ["cards", "reviews", "events", "article_progress", "quiz_attempts", "cache",
                  "lookups", "jobs", "exposure", "scaffolding", "quotes", "challenges",
                  "revisions", "writings"]
        if not keep_words:
            tables.append("words")
        with self.write() as conn:
            for table in tables:
                conn.execute(f"DELETE FROM {table}")

    def backup(self, folder: Path) -> Path:
        """A consistent copy of the database, write-ahead log and all.

        Copying the .db file alone silently misses whatever is still in the WAL --
        after a session's reading that is most of the recent rows -- so this uses
        SQLite's own backup, which knows how to take a copy while WAL is in use.
        Written before anything destructive runs, because the one button that can
        lose months of work should be recoverable.
        """
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = folder / f"diglot-{stamp}.db"
        destination = sqlite3.connect(target)
        try:
            with destination:
                self.conn.backup(destination)
        finally:
            destination.close()
        return target

    def forget_article(self, slug: str) -> None:
        """Drop what the reader accumulated *about* one article.

        Called when a lesson is deleted: its reading position and completion are
        about a passage that no longer exists. What the reader learned from it is
        not touched -- saved words are their knowledge, and the article they came
        from is a note in the margin, not a parent.
        """
        with self.write() as conn:
            conn.execute("DELETE FROM article_progress WHERE slug = ?", (slug,))

    def export_rows(self) -> Sequence[sqlite3.Row]:
        """Words plus their schedule, for CSV export."""
        return self.conn.execute(
            """SELECT w.term, w.gloss, w.pos, w.context, w.article_slug, c.interval_days, c.ease,
                      w.created_at, c.due_at
                 FROM words w LEFT JOIN cards c ON c.word_id = w.id AND c.kind = 'recall'
                ORDER BY w.created_at"""
        ).fetchall()
