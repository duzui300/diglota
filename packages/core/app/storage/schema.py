"""SQLite schema DDL and versioned migrations for Diglota."""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger("diglot.store")

SCHEMA_VERSION = 2

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
    created_at    TEXT NOT NULL,
    language_pair TEXT NOT NULL DEFAULT 'en-es'
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
    slug           TEXT PRIMARY KEY,
    position       REAL NOT NULL DEFAULT 0,
    completed_at   TEXT,
    last_opened_at TEXT,
    seconds_spent  INTEGER NOT NULL DEFAULT 0,
    credited_to    REAL NOT NULL DEFAULT 0,
    language_pair  TEXT NOT NULL DEFAULT 'en-es'
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,
    kind          TEXT NOT NULL,
    slug          TEXT,
    amount        REAL NOT NULL DEFAULT 0,
    language_pair TEXT NOT NULL DEFAULT 'en-es'
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

CREATE TABLE IF NOT EXISTS lookups (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    term          TEXT NOT NULL,
    lemma         TEXT,
    gloss         TEXT,
    article_slug  TEXT,
    context       TEXT,
    at            TEXT NOT NULL,
    language_pair TEXT NOT NULL DEFAULT 'en-es'
);

CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    title         TEXT NOT NULL,
    status        TEXT NOT NULL,
    step          TEXT NOT NULL DEFAULT '',
    done          INTEGER NOT NULL DEFAULT 0,
    total         INTEGER NOT NULL DEFAULT 0,
    detail        TEXT NOT NULL DEFAULT '',
    error         TEXT,
    result        TEXT,
    created_at    TEXT NOT NULL,
    finished_at   TEXT,
    language_pair TEXT NOT NULL DEFAULT 'en-es'
);

CREATE TABLE IF NOT EXISTS exposure (
    day           TEXT NOT NULL,
    slug          TEXT NOT NULL,
    lemma         TEXT NOT NULL,
    times         INTEGER NOT NULL DEFAULT 0,
    known         INTEGER NOT NULL DEFAULT 0,
    glossed       INTEGER NOT NULL DEFAULT 0,
    first_day     TEXT NOT NULL,
    language_pair TEXT NOT NULL DEFAULT 'en-es',
    PRIMARY KEY (day, slug, lemma)
);

CREATE TABLE IF NOT EXISTS scaffolding (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,
    kind          TEXT NOT NULL,
    slug          TEXT,
    term          TEXT,
    language_pair TEXT NOT NULL DEFAULT 'en-es'
);

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
    created_at    TEXT NOT NULL,
    language_pair TEXT NOT NULL DEFAULT 'en-es'
);

CREATE TABLE IF NOT EXISTS challenges (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,
    target        INTEGER NOT NULL,
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    completed_at  TEXT,
    language_pair TEXT NOT NULL DEFAULT 'en-es'
);

CREATE TABLE IF NOT EXISTS writings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL DEFAULT '',
    prompt        TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    language_pair TEXT NOT NULL DEFAULT 'en-es'
);

CREATE TABLE IF NOT EXISTS revisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    writing_id INTEGER NOT NULL REFERENCES writings(id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    reading    TEXT NOT NULL DEFAULT '',
    verdict    TEXT NOT NULL DEFAULT '',
    feedback   TEXT NOT NULL DEFAULT '',
    words      INTEGER NOT NULL DEFAULT 0,
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

_PAIR_SCOPED_TABLES = (
    "words",
    "article_progress",
    "events",
    "lookups",
    "jobs",
    "exposure",
    "scaffolding",
    "quotes",
    "challenges",
    "writings",
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn: sqlite3.Connection) -> None:
    """Apply idempotent, versioned schema migrations via ``PRAGMA user_version``."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])

    # Migration 1: credited_to + legacy single-version writings -> revisions.
    if version < 1:
        progress_cols = _columns(conn, "article_progress")
        if "credited_to" not in progress_cols:
            conn.execute(
                "ALTER TABLE article_progress ADD COLUMN credited_to REAL NOT NULL DEFAULT 0"
            )

        writing_cols = _columns(conn, "writings")
        if "text" in writing_cols:
            if "title" not in writing_cols:
                conn.execute("ALTER TABLE writings ADD COLUMN title TEXT NOT NULL DEFAULT ''")
            if "updated_at" not in writing_cols:
                conn.execute("ALTER TABLE writings ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
            # Only copy rows that do not already have a revision (idempotent on retries).
            for row in conn.execute("SELECT * FROM writings").fetchall():
                has_rev = conn.execute(
                    "SELECT 1 FROM revisions WHERE writing_id = ? LIMIT 1", (row["id"],)
                ).fetchone()
                if not has_rev:
                    conn.execute(
                        """INSERT INTO revisions (writing_id, text, reading, verdict, feedback,
                                                  words, checked, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            row["id"], row["text"], row["reading"], row["verdict"],
                            row["feedback"], row["words"], 1, row["created_at"],
                        ),
                    )
            conn.execute("UPDATE writings SET updated_at = created_at WHERE updated_at = ''")
            for dead in ("text", "reading", "verdict", "feedback", "words"):
                try:
                    conn.execute(f"ALTER TABLE writings DROP COLUMN {dead}")
                except sqlite3.OperationalError:
                    log.info("could not drop writings.%s; it is unused", dead)
        conn.execute("PRAGMA user_version = 1")
        version = 1

    # Migration 2: add language_pair column across domain tables for multi-pair readiness.
    if version < 2:
        for table in _PAIR_SCOPED_TABLES:
            cols = _columns(conn, table)
            if "language_pair" not in cols:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN language_pair TEXT NOT NULL DEFAULT 'en-es'"
                )
        conn.execute("PRAGMA user_version = 2")
