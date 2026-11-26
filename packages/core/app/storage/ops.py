"""Operational persistence: AI cache, background jobs, quizzes, and weekly challenges."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ..languages import DEFAULT_PAIR_ID
from .db import Database, now_iso, parse_iso


class OpsRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- quizzes ---------------------------------------------------------- #

    def save_attempt(
        self, *, slug: str | None, question: str, answer: str | None,
        verdict: str | None, score: float | None, feedback: str | None,
    ) -> None:
        stamp = now_iso()
        with self._db.write() as conn:
            conn.execute(
                """INSERT INTO quiz_attempts (slug, question, answer, verdict, score, feedback, at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (slug, question, answer, verdict, score, feedback, stamp),
            )
            conn.execute(
                "INSERT INTO events (at, kind, slug, amount) VALUES (?, 'quiz', ?, 1)",
                (stamp, slug),
            )

    def attempts_for(self, slug: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._db.conn.execute(
                "SELECT * FROM quiz_attempts WHERE slug = ? ORDER BY at DESC LIMIT 50", (slug,)
            )
        ]

    # -- AI result cache -------------------------------------------------- #

    def cache_get(self, key: str, *, max_age_seconds: int | None = None) -> Any | None:
        row = self._db.conn.execute(
            "SELECT value, created_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
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
        with self._db.write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, value, created_at) VALUES (?, ?, ?)",
                (key, json.dumps(value, ensure_ascii=False), now_iso()),
            )

    def cache_forget(self, key: str) -> None:
        with self._db.write() as conn:
            conn.execute("DELETE FROM cache WHERE key = ?", (key,))

    # -- background jobs -------------------------------------------------- #

    def save_job(self, job: dict[str, Any]) -> None:
        payload = {
            "language_pair": DEFAULT_PAIR_ID,
            **job,
            "result": json.dumps(job.get("result"), ensure_ascii=False)
            if job.get("result") is not None else None,
        }
        with self._db.write() as conn:
            conn.execute(
                """INSERT INTO jobs (id, kind, title, status, step, done, total, detail,
                                     error, result, created_at, finished_at, language_pair)
                   VALUES (:id, :kind, :title, :status, :step, :done, :total, :detail,
                           :error, :result, :created_at, :finished_at, :language_pair)
                   ON CONFLICT(id) DO UPDATE SET
                       status = excluded.status, step = excluded.step,
                       done = excluded.done, total = excluded.total,
                       detail = excluded.detail, error = excluded.error,
                       result = excluded.result, finished_at = excluded.finished_at""",
                payload,
            )

    def list_jobs(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._db.conn.execute(
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
        with self._db.write() as conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def clear_finished_jobs(self) -> int:
        with self._db.write() as conn:
            cursor = conn.execute("DELETE FROM jobs WHERE status != 'running' AND status != 'queued'")
            return cursor.rowcount

    # -- challenges ------------------------------------------------------- #

    def start_challenge(
        self, kind: str, target: int, *, language_pair: str = DEFAULT_PAIR_ID
    ) -> dict[str, Any]:
        now = now_iso()
        with self._db.write() as conn:
            conn.execute("UPDATE challenges SET ended_at = ? WHERE ended_at IS NULL", (now,))
            cursor = conn.execute(
                "INSERT INTO challenges (kind, target, started_at, language_pair) VALUES (?, ?, ?, ?)",
                (kind, max(1, int(target)), now, language_pair),
            )
            conn.execute(
                "INSERT INTO events (at, kind, slug, amount, language_pair) VALUES (?, 'challenge', NULL, 1, ?)",
                (now, language_pair),
            )
            row = conn.execute(
                "SELECT * FROM challenges WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return dict(row)

    def active_challenge(self) -> dict[str, Any] | None:
        row = self._db.conn.execute(
            "SELECT * FROM challenges WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def complete_challenge(self, challenge_id: int) -> bool:
        with self._db.write() as conn:
            cursor = conn.execute(
                "UPDATE challenges SET completed_at = ? WHERE id = ? AND completed_at IS NULL",
                (now_iso(), challenge_id),
            )
            return cursor.rowcount > 0

    def end_challenge(self, challenge_id: int) -> bool:
        with self._db.write() as conn:
            cursor = conn.execute(
                "UPDATE challenges SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                (now_iso(), challenge_id),
            )
            return cursor.rowcount > 0

    def challenge_history(self, *, limit: int = 12) -> list[dict[str, Any]]:
        rows = self._db.conn.execute(
            "SELECT * FROM challenges ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
