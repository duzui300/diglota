"""Vocabulary deck, SRS cards, and review persistence."""

from __future__ import annotations

import sqlite3
from typing import Any, Sequence

from .. import srs
from ..languages import DEFAULT_PAIR_ID
from .db import Database, now_iso


class VocabularyRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

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
        language_pair: str = DEFAULT_PAIR_ID,
    ) -> dict[str, Any]:
        """Save a word and create its recall card (idempotent and race-safe)."""
        key = (lemma or term).strip().lower()
        stamp = now_iso()
        with self._db.write() as conn:
            existing = conn.execute("SELECT * FROM words WHERE lemma = ?", (key,)).fetchone()
            if existing:
                return dict(existing)
            cursor = conn.execute(
                """INSERT INTO words (lemma, term, gloss, pos, note, article_slug, context, created_at, language_pair)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (key, term.strip(), gloss, pos, note, article_slug, context, stamp, language_pair),
            )
            word_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO cards (word_id, kind, due_at) VALUES (?, 'recall', ?)",
                (word_id, stamp),
            )
            row = conn.execute("SELECT * FROM words WHERE id = ?", (word_id,)).fetchone()
        return dict(row)

    def list_words(
        self, *, limit: int = 500, search: str | None = None,
        language_pair: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT w.*, c.ease, c.interval_days, c.reps, c.lapses, c.due_at, c.last_reviewed_at
              FROM words w LEFT JOIN cards c ON c.word_id = w.id AND c.kind = 'recall'
        """
        clauses: list[str] = []
        params: list[Any] = []
        if language_pair:
            clauses.append("w.language_pair = ?")
            params.append(language_pair)
        if search:
            clauses.append("(w.term LIKE ? OR w.gloss LIKE ?)")
            params += [f"%{search}%", f"%{search}%"]
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY w.created_at DESC LIMIT ?"
        params.append(limit)
        return [self._word_row(row) for row in self._db.conn.execute(sql, params)]

    def word_count(self, *, language_pair: str | None = None) -> int:
        if language_pair:
            return int(
                self._db.conn.execute(
                    "SELECT COUNT(*) FROM words WHERE language_pair = ?", (language_pair,)
                ).fetchone()[0]
            )
        return int(self._db.conn.execute("SELECT COUNT(*) FROM words").fetchone()[0])

    def known_terms(self, *, language_pair: str | None = None) -> set[str]:
        if language_pair:
            return {
                row[0]
                for row in self._db.conn.execute(
                    "SELECT lemma FROM words WHERE language_pair = ?", (language_pair,)
                )
            }
        return {row[0] for row in self._db.conn.execute("SELECT lemma FROM words")}

    def delete_word(self, word_id: int) -> bool:
        with self._db.write() as conn:
            cursor = conn.execute("DELETE FROM words WHERE id = ?", (word_id,))
            return cursor.rowcount > 0

    def update_word(self, word_id: int, **fields: Any) -> None:
        allowed = {"gloss", "pos", "note"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        clause = ", ".join(f"{k} = ?" for k in updates)
        with self._db.write() as conn:
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

    def due_cards(
        self, *, limit: int = 40, include_new: int = 12,
        language_pair: str | None = None,
    ) -> list[dict[str, Any]]:
        stamp = now_iso()
        pair_clause = " AND w.language_pair = ?" if language_pair else ""
        due_params: list[Any] = [stamp]
        if language_pair:
            due_params.append(language_pair)
        due_params.append(limit)
        due = self._db.conn.execute(
            f"""SELECT c.id AS card_id, c.due_at, c.ease, c.interval_days, c.reps, c.lapses,
                       w.id AS word_id, w.term, w.lemma, w.gloss, w.pos, w.note,
                       w.context, w.article_slug, w.language_pair
                  FROM cards c JOIN words w ON w.id = c.word_id
                 WHERE c.due_at IS NOT NULL AND c.due_at <= ?
                   AND NOT (c.reps = 0 AND c.lapses = 0){pair_clause}
                 ORDER BY c.due_at ASC LIMIT ?""",
            due_params,
        ).fetchall()
        rows = [self._card_row(r) for r in due]

        if include_new and len(rows) < limit:
            fresh_params: list[Any] = []
            if language_pair:
                fresh_params.append(language_pair)
            fresh_params.append(min(include_new, limit - len(rows)))
            fresh = self._db.conn.execute(
                f"""SELECT c.id AS card_id, c.due_at, c.ease, c.interval_days, c.reps, c.lapses,
                           w.id AS word_id, w.term, w.lemma, w.gloss, w.pos, w.note,
                           w.context, w.article_slug, w.language_pair
                      FROM cards c JOIN words w ON w.id = c.word_id
                     WHERE c.reps = 0 AND c.lapses = 0{pair_clause}
                     ORDER BY w.created_at DESC LIMIT ?""",
                fresh_params,
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
        with self._db.write() as conn:
            row = conn.execute(
                """SELECT c.*, w.language_pair
                     FROM cards c JOIN words w ON w.id = c.word_id
                    WHERE c.id = ?""",
                (card_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"no card {card_id}")
            state = srs.CardState(
                ease=row["ease"], interval_days=row["interval_days"], reps=row["reps"], lapses=row["lapses"]
            )
            new_state, due = srs.review(state, rating)
            stamp = now_iso()
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
            conn.execute(
                "INSERT INTO events (at, kind, slug, amount, language_pair) VALUES (?, 'review', NULL, 1, ?)",
                (stamp, row["language_pair"]),
            )
        return {
            "card_id": card_id,
            "due_at": due.isoformat(timespec="seconds"),
            "interval_days": new_state.interval_days,
            "ease": new_state.ease,
            "stage": new_state.stage,
            "preview": srs.preview(new_state),
        }

    def due_count(self, *, language_pair: str | None = None) -> int:
        if language_pair:
            return int(
                self._db.conn.execute(
                    """SELECT COUNT(*) FROM cards c JOIN words w ON w.id = c.word_id
                        WHERE c.due_at IS NOT NULL AND c.due_at <= ?
                          AND NOT (c.reps = 0 AND c.lapses = 0)
                          AND w.language_pair = ?""",
                    (now_iso(), language_pair),
                ).fetchone()[0]
            )
        return int(
            self._db.conn.execute(
                """SELECT COUNT(*) FROM cards
                    WHERE due_at IS NOT NULL AND due_at <= ?
                      AND NOT (reps = 0 AND lapses = 0)""",
                (now_iso(),),
            ).fetchone()[0]
        )

    def export_rows(self, *, language_pair: str | None = None) -> Sequence[sqlite3.Row]:
        sql = """SELECT w.term, w.gloss, w.pos, w.context, w.article_slug, c.interval_days, c.ease,
                        w.created_at, c.due_at
                   FROM words w LEFT JOIN cards c ON c.word_id = w.id AND c.kind = 'recall'"""
        params: list[Any] = []
        if language_pair:
            sql += " WHERE w.language_pair = ?"
            params.append(language_pair)
        sql += " ORDER BY w.created_at"
        return self._db.conn.execute(sql, params).fetchall()
