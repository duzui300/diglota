"""User-authored content persistence: kept quotes and writing pieces/revisions."""

from __future__ import annotations

import json
import re
from typing import Any

from ..languages import DEFAULT_PAIR_ID, fold_text
from .db import Database, now_iso

_WORDS = re.compile(r"[^\W\d_]+", re.UNICODE)


def _contains_word(text: str, needle: str) -> bool:
    folded = fold_text(text)
    if " " in needle:
        return needle in folded
    return needle in _WORDS.findall(folded)


def _json_or_empty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False) if value else ""


def _unpack_revision(row: dict[str, Any]) -> dict[str, Any]:
    for field_name in ("reading", "verdict", "feedback"):
        try:
            row[field_name] = json.loads(row.get(field_name) or "{}") or {}
        except (json.JSONDecodeError, TypeError):
            row[field_name] = {}
    row["checked"] = bool(row.get("checked"))
    return row


def _attach_revisions(piece: dict[str, Any], revisions: list[dict[str, Any]]) -> dict[str, Any]:
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
    return fold_text(
        " ".join(str(row.get(field) or "") for field in ("text", "es", "en", "glosses", "note", "term"))
    )


class ContentRepo:
    QUOTE_SCAN = 5000

    def __init__(self, db: Database) -> None:
        self._db = db

    # -- quotes ----------------------------------------------------------- #

    def save_quote(
        self, *, text: str, es: str, en: str = "", glosses: str = "", note: str | None = None,
        article_slug: str | None = None, block_index: int | None = None,
        start: int | None = None, end: int | None = None, term: str | None = None,
        language_pair: str = DEFAULT_PAIR_ID,
    ) -> tuple[int, bool]:
        stamp = now_iso()
        with self._db.write() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO quotes
                   (text, es, en, glosses, note, article_slug, block_index,
                    start, end, term, created_at, language_pair)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (text, es, en, glosses, note, article_slug, block_index,
                 start, end, term, stamp, language_pair),
            )
            created = cursor.rowcount > 0
            if created:
                quote_id = int(cursor.lastrowid or 0)
                conn.execute(
                    "INSERT INTO events (at, kind, slug, amount, language_pair) VALUES (?, 'quote', ?, 1, ?)",
                    (stamp, article_slug, language_pair),
                )
            else:
                row = conn.execute("SELECT id FROM quotes WHERE text = ?", (text,)).fetchone()
                quote_id = int(row["id"]) if row else 0
        return quote_id, created

    def quotes(
        self, *, search: str | None = None, slug: str | None = None,
        term: str | None = None, limit: int = 400,
        language_pair: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if slug:
            clauses.append("article_slug = ?")
            params.append(slug)
        if term:
            clauses.append("term = ?")
            params.append(term)
        if language_pair:
            clauses.append("language_pair = ?")
            params.append(language_pair)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql_limit = self.QUOTE_SCAN if search else limit
        params.append(sql_limit)
        rows = self._db.conn.execute(
            f"SELECT * FROM quotes{where} ORDER BY created_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        out = [dict(row) for row in rows]
        if search:
            needle = fold_text(search)
            out = [row for row in out if needle in _searchable(row)]
        return out[:limit]

    def quote(self, quote_id: int) -> dict[str, Any] | None:
        row = self._db.conn.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
        return dict(row) if row else None

    def set_quote_note(self, quote_id: int, note: str) -> bool:
        with self._db.write() as conn:
            cursor = conn.execute("UPDATE quotes SET note = ? WHERE id = ?", (note, quote_id))
            return cursor.rowcount > 0

    def delete_quote(self, quote_id: int) -> bool:
        with self._db.write() as conn:
            cursor = conn.execute("DELETE FROM quotes WHERE id = ?", (quote_id,))
            return cursor.rowcount > 0

    def quote_count(self) -> int:
        return int(self._db.conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0])

    def quotes_using(self, term: str, *, limit: int = 6) -> list[dict[str, Any]]:
        needle = fold_text(term)
        if len(needle) < 3:
            return []
        rows = self._db.conn.execute(
            "SELECT * FROM quotes ORDER BY created_at DESC LIMIT ?", (self.QUOTE_SCAN,)
        ).fetchall()
        return [dict(row) for row in rows if _contains_word(row["es"], needle)][:limit]

    # -- writing ---------------------------------------------------------- #

    def start_writing(
        self, *, title: str, prompt: str = "", language_pair: str = DEFAULT_PAIR_ID
    ) -> dict[str, Any]:
        now = now_iso()
        with self._db.write() as conn:
            cursor = conn.execute(
                """INSERT INTO writings (title, prompt, created_at, updated_at, language_pair)
                   VALUES (?, ?, ?, ?, ?)""",
                (title, prompt, now, now, language_pair),
            )
            row = conn.execute("SELECT * FROM writings WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    def add_revision(
        self, writing_id: int, *, text: str, reading: dict | None = None,
        verdict: dict | None = None, feedback: dict | None = None,
        words: int = 0, checked: bool = False,
    ) -> dict[str, Any] | None:
        stamp = now_iso()
        word_count = max(0, int(words))
        with self._db.write() as conn:
            piece = conn.execute(
                "SELECT id, language_pair FROM writings WHERE id = ?", (writing_id,)
            ).fetchone()
            if piece is None:
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
                    (
                        _json_or_empty(reading), _json_or_empty(verdict), _json_or_empty(feedback),
                        word_count, 1 if checked else int(last["checked"]),
                        last["created_at"], last["id"],
                    ),
                )
                revision_id = int(last["id"])
            else:
                cursor = conn.execute(
                    """INSERT INTO revisions (writing_id, text, reading, verdict, feedback,
                                              words, checked, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        writing_id, text, _json_or_empty(reading), _json_or_empty(verdict),
                        _json_or_empty(feedback), word_count, 1 if checked else 0, stamp,
                    ),
                )
                revision_id = int(cursor.lastrowid or 0)
            conn.execute("UPDATE writings SET updated_at = ? WHERE id = ?", (stamp, writing_id))
            conn.execute(
                "INSERT INTO events (at, kind, slug, amount, language_pair) VALUES (?, 'writing', NULL, ?, ?)",
                (stamp, word_count, piece["language_pair"]),
            )
            row = conn.execute("SELECT * FROM revisions WHERE id = ?", (revision_id,)).fetchone()
        return _unpack_revision(dict(row)) if row else None

    def writings(self, *, limit: int = 60) -> list[dict[str, Any]]:
        """Pieces newest first, batch-loading revisions in a single query (no N+1)."""
        rows = self._db.conn.execute(
            "SELECT * FROM writings ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        pieces = [dict(row) for row in rows]
        if not pieces:
            return []
        ids = [p["id"] for p in pieces]
        placeholders = ",".join("?" for _ in ids)
        rev_rows = self._db.conn.execute(
            f"SELECT * FROM revisions WHERE writing_id IN ({placeholders}) ORDER BY id", ids
        ).fetchall()
        by_writing: dict[int, list[dict[str, Any]]] = {wid: [] for wid in ids}
        for rev in rev_rows:
            by_writing[rev["writing_id"]].append(_unpack_revision(dict(rev)))
        return [_attach_revisions(piece, by_writing.get(piece["id"], [])) for piece in pieces]

    def writing(self, writing_id: int) -> dict[str, Any] | None:
        row = self._db.conn.execute("SELECT * FROM writings WHERE id = ?", (writing_id,)).fetchone()
        if row is None:
            return None
        rev_rows = self._db.conn.execute(
            "SELECT * FROM revisions WHERE writing_id = ? ORDER BY id", (writing_id,)
        ).fetchall()
        return _attach_revisions(dict(row), [_unpack_revision(dict(r)) for r in rev_rows])

    def latest_revision(self, writing_id: int) -> dict[str, Any] | None:
        row = self._db.conn.execute(
            "SELECT * FROM revisions WHERE writing_id = ? ORDER BY id DESC LIMIT 1", (writing_id,)
        ).fetchone()
        return _unpack_revision(dict(row)) if row else None

    def delete_writing(self, writing_id: int) -> bool:
        with self._db.write() as conn:
            conn.execute("DELETE FROM revisions WHERE writing_id = ?", (writing_id,))
            return conn.execute("DELETE FROM writings WHERE id = ?", (writing_id,)).rowcount > 0

    def writing_totals(self) -> dict[str, Any]:
        rows = self._db.conn.execute("SELECT reading, words FROM revisions").fetchall()
        pieces = int(self._db.conn.execute("SELECT COUNT(*) FROM writings").fetchone()[0])
        target_words = total = 0
        for row in rows:
            try:
                reading = json.loads(row["reading"] or "{}")
            except json.JSONDecodeError:
                reading = {}
            target_words += int(reading.get("target_words", reading.get("spanish_words")) or 0)
            total += int(reading.get("words") or 0)
        share = round(target_words / total, 3) if total else 0.0
        return {
            "pieces": pieces,
            "revisions": len(rows),
            "words": sum(int(row["words"] or 0) for row in rows),
            "spanish_words": target_words,
            "target_words": target_words,
            "measured_words": total,
            "spanish_share": share,
            "target_share": share,
        }
