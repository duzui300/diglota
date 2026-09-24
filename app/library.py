"""The article library: the corpus on disk, plus anything imported since.

Ordering in the library is by difficulty rather than by date, because the
useful question for a learner is "what can I read next that I can actually
handle", not "what did the author write most recently". A ``shelf`` is derived
from the share of words that are Spanish: a text that is 30% Spanish is a
gentler read than one that is 80%, whatever they are about.

Coverage -- how much of an article's focus vocabulary the learner has already
saved -- is computed per request from the vocabulary table. It is the single
most motivating number in the app: watching an unread article go from 12% known
to 40% known because of reading you have already done is visible proof of
progress that a streak counter cannot give.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .diglot import Article, parse_file
from . import registers, vocab

# Difficulty bands, by the share of the article's words that are Spanish. The
# cut points are set from the corpus distribution: the hand-made articles run
# from about 30% to 90%.
SHELVES = (
    ("gentle", 0.00, 0.45, "Mostly English, Spanish in short phrases"),
    ("steady", 0.45, 0.62, "A real weave, roughly half Spanish"),
    ("dense", 0.62, 0.78, "Spanish carries whole sentences"),
    ("immersion", 0.78, 1.01, "Spanish-dominant"),
)


def shelf_for(ratio: float) -> tuple[str, str]:
    for name, low, high, description in SHELVES:
        if low <= ratio < high:
            return name, description
    return "steady", ""


def deck_signature(deck: Iterable[dict[str, Any]]) -> int:
    """A cheap fingerprint of what the deck actually contains.

    Size alone is not enough: swapping one saved word for another leaves the
    count unchanged, and every cache keyed on it would keep serving numbers
    computed against the deck that no longer exists. Cheap enough to recompute on
    each request -- a personal deck is hundreds of words, not millions.
    """
    return hash(tuple(sorted(str(word.get("term") or "") for word in deck)))


@dataclass
class LibraryEntry:
    article: Article
    imported: bool
    # The file this article was read from, when there was one. Kept so a lesson
    # can be exported byte-for-byte as its author wrote it: regenerating from the
    # parsed model would be canonical but lossy, and a shareable file should be
    # the author's words rather than this app's rendering of them.
    path: Path | None = None
    # The register, computed once. Inferring it scans the article's text with a
    # dozen patterns, and the shelf is rendered on the app's most-visited page --
    # the same guess was being made twice per article per visit. Not part of the
    # entry's identity, so it stays out of equality and repr.
    _register: tuple[str, bool] | None = field(default=None, compare=False, repr=False)

    def register(self) -> tuple[str, bool]:
        """The register to show, and whether it was inferred rather than declared."""
        if self._register is None:
            self._register = registers.effective(self.article)
        return self._register

    def to_dict(self, *, with_blocks: bool = False) -> dict[str, Any]:
        data = self.article.to_dict(with_blocks=with_blocks)
        data["imported"] = self.imported
        shelf, description = shelf_for(data["stats"]["spanish_ratio"])
        data["shelf"] = shelf
        data["shelf_description"] = description
        data["focus_count"] = len(self.article.focus_pairs())
        # Register is carried here rather than written into the corpus files:
        # the user's hand-made articles are theirs, and a guess does not belong
        # in them. Imports get a judgment instead, written to their own file.
        register, inferred = self.register()
        data["register"] = register
        data["register_inferred"] = inferred
        data["register_label"] = registers.label(register)
        data["register_blurb"] = registers.describe(register)
        return data


class Library:
    """Loads and holds every article, from both the corpus and the imports."""

    def __init__(self, corpus_dir: Path, library_dir: Path) -> None:
        self.corpus_dir = corpus_dir
        self.library_dir = library_dir
        self._lock = threading.Lock()
        self._entries: dict[str, LibraryEntry] = {}
        self._loaded = False
        # Bumped whenever the shelves change, so derived indexes (the glossary)
        # can tell whether they are stale without diffing the whole library.
        self.revision = 0
        # Coverage is a stemming pass over every Spanish word in the article, and
        # the shelf asks for it for all of them on every visit. Cached against
        # the two things that can change it -- which articles exist, and what is
        # in the deck -- like the corpus graph, for the same reason.
        self._coverage: dict[tuple[int, int, str], dict[str, Any]] = {}

    # -- loading ---------------------------------------------------------- #

    def refresh(self) -> None:
        with self._lock:
            entries: dict[str, LibraryEntry] = {}
            for path in _markdown_files(self.corpus_dir):
                for article in _safe_parse(path):
                    entries.setdefault(article.slug, LibraryEntry(article, imported=False, path=path))
            for path in _markdown_files(self.library_dir):
                for article in _safe_parse(path):
                    entries[article.slug] = LibraryEntry(article, imported=True, path=path)
            self._entries = entries
            self._loaded = True
            self._coverage.clear()
            self.revision += 1

    def _ensure(self) -> None:
        if not self._loaded:
            self.refresh()

    # -- access ----------------------------------------------------------- #

    def all(self) -> list[LibraryEntry]:
        self._ensure()
        return sorted(self._entries.values(), key=lambda e: (e.article.stats()["spanish_ratio"], e.article.title))

    def get(self, slug: str) -> LibraryEntry | None:
        self._ensure()
        return self._entries.get(slug)

    def slugs(self) -> list[str]:
        self._ensure()
        return list(self._entries)

    def __len__(self) -> int:
        self._ensure()
        return len(self._entries)

    def add_article(self, slug: str, article: Article, *, imported: bool = True) -> None:
        """Register an article that was just written to disk."""
        with self._lock:
            self._entries[slug] = LibraryEntry(article, imported=imported)
            self._loaded = True

    # -- derived views ---------------------------------------------------- #

    def coverage(self, article: Article, deck: list[dict[str, Any]]) -> dict[str, Any]:
        """How much of an article the learner already has.

        Two measures, because they answer different questions. ``focus`` is the
        lesson's own syllabus -- the phrases it bolds -- and tells you how much
        of *what this article teaches* you have already met. ``text`` is the
        whole Spanish text, and tells you how comfortably you can read it. The
        second is the one that decides whether to open the article at all.

        Cached: the answer depends on the article, what is in the deck, and
        nothing else, so a page that asks about twenty-four articles pays for the
        stemming pass once rather than once per visit.
        """
        key = (self.revision, deck_signature(deck), article.slug)
        hit = self._coverage.get(key)
        if hit is not None:
            return hit
        result = self._measure_coverage(article, deck)
        self._coverage[key] = result
        return result

    def _measure_coverage(self, article: Article, deck: list[dict[str, Any]]) -> dict[str, Any]:
        pairs = article.focus_pairs()
        known_norm = {_normalise_term(w.get("term") or "") for w in deck}
        known_norm |= {_normalise_term(w.get("lemma") or "") for w in deck}
        known_norm.discard("")

        if pairs:
            hit = [p for p in pairs if _normalise_term(p.es) in known_norm
                   or _normalise_term(vocab_head(p.es)) in known_norm]
        else:
            hit = []
        missing = [p.es for p in pairs if p not in hit]

        text = vocab.text_coverage(article, deck)
        band, advice = vocab.verdict(text["token_ratio"])
        return {
            "total": len(pairs),
            "known": len(hit),
            "ratio": round(len(hit) / len(pairs), 3) if pairs else 1.0,
            "missing": missing[:12],
            "text": text,
            "band": band,
            "advice": advice,
        }

    def passages_per_file(self) -> dict[str, int]:
        """How many passages each file holds, keyed by path.

        Usually one. A hand-made compilation holds several -- the corpus ships one
        with four lessons under day headings -- and anything that edits a file *for
        a passage* has to know the difference, because a front-matter line in a
        compilation belongs to one passage rather than to the file. The parser
        reports passages and not their line ranges, so a single passage inside such
        a file cannot be addressed at all.
        """
        self._ensure()
        counts: dict[str, int] = {}
        for entry in self.all():
            key = str(entry.path or "")
            counts[key] = counts.get(key, 0) + 1
        return counts

    def catalogue(self, deck: list[dict[str, Any]], progress: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        self._ensure()
        counts = self.passages_per_file()
        out: list[dict[str, Any]] = []
        for entry in self.all():
            data = entry.to_dict()
            data["coverage"] = self.coverage(entry.article, deck)
            data["register_editable"] = counts.get(str(entry.path or ""), 1) == 1
            state = progress.get(entry.article.slug) or {}
            data["progress"] = {
                "position": state.get("position", 0.0),
                "completed": bool(state.get("completed_at")),
                "seconds_spent": state.get("seconds_spent", 0),
            }
            out.append(data)
        return out

    def vocabulary_index(self) -> dict[str, dict[str, Any]]:
        """Every focus phrase in the library, with where it comes from.

        Used to show, for a word the learner saved, which *other* articles it
        appears in -- the cheapest possible argument for reading the next one.
        """
        index: dict[str, dict[str, Any]] = {}
        for entry in self.all():
            for pair in entry.article.focus_pairs():
                key = pair.es.strip().lower()
                record = index.setdefault(key, {"term": pair.es, "gloss": pair.en, "articles": []})
                if pair.en and not record["gloss"]:
                    record["gloss"] = pair.en
                record["articles"].append({"slug": entry.article.slug, "title": entry.article.title})
        return index


def _head(term: str) -> str:
    """The first word of a phrase, used as a looser match key."""
    return re.split(r"[\s/,(]", term.strip(), maxsplit=1)[0]


# `vocab.head_word` under a name that reads better at the call site above.
vocab_head = _head


def _normalise_term(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _markdown_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return [p for p in sorted(directory.glob("*.md")) if not p.name.lower().startswith("readme")]


def _safe_parse(path: Path) -> Iterable[Article]:
    try:
        return parse_file(path)
    except Exception as exc:  # a malformed file must not sink the library
        print(f"[library] failed to parse {path.name}: {type(exc).__name__}: {exc}")
        return []
