"""SQLite connection management, BEGIN IMMEDIATE transactions, and maintenance."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .schema import SCHEMA, migrate


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def days_ago(days: int) -> str:
    """The date ``days`` days back, as ``YYYY-MM-DD``."""
    return (datetime.now(timezone.utc).date() - timedelta(days=max(days, 0))).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Database:
    """Thread-local SQLite pool with explicit ``BEGIN IMMEDIATE`` write transactions.

    Tracking every opened connection lets :meth:`close` cleanly release WAL/SHM
    handles across all worker threads on shutdown (critical on Windows).
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._lock = threading.Lock()
        self._conns: set[sqlite3.Connection] = set()

        bootstrap = self._connect()
        try:
            bootstrap.executescript(SCHEMA)
            migrate(bootstrap)
        finally:
            bootstrap.close()

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None enables explicit BEGIN IMMEDIATE transactions
        # in write(), eliminating WAL read-to-write lock upgrade deadlocks.
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
            self._local.tx_depth = 0
            with self._lock:
                self._conns.add(conn)
        return conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """An explicit ``BEGIN IMMEDIATE`` write transaction (nest-safe)."""
        conn = self.conn
        depth = getattr(self._local, "tx_depth", 0)
        if depth == 0:
            conn.execute("BEGIN IMMEDIATE")
        self._local.tx_depth = depth + 1
        try:
            yield conn
        except Exception:
            self._local.tx_depth = depth
            if depth == 0 and conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        else:
            self._local.tx_depth = depth
            if depth == 0 and conn.in_transaction:
                conn.execute("COMMIT")

    def close(self) -> None:
        """Close every thread's SQLite connection opened through this instance."""
        with self._lock:
            conns = list(self._conns)
            self._conns.clear()
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        self._local.conn = None
        self._local.tx_depth = 0

    def reset(self, keep_words: bool = False) -> None:
        """Clear accumulated study history while keeping the corpus files intact."""
        tables = [
            "cards", "reviews", "events", "article_progress", "quiz_attempts", "cache",
            "lookups", "jobs", "exposure", "scaffolding", "quotes", "challenges",
            "revisions", "writings",
        ]
        if not keep_words:
            tables.append("words")
        with self.write() as conn:
            for table in tables:
                conn.execute(f"DELETE FROM {table}")

    def backup(self, folder: Path) -> Path:
        """Take a consistent online SQLite backup (WAL included)."""
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
        """Drop per-article progress when a lesson is removed from the library."""
        with self.write() as conn:
            conn.execute("DELETE FROM article_progress WHERE slug = ?", (slug,))
