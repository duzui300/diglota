"""Moving a diglot between people.

The corpus format is already portable: it is Markdown, it carries its own front
matter, its vocabulary and its grammar notes, and any file in it can be read by
this app and by a person with a text editor. What is missing is not a format but
an *envelope* -- the things a recipient needs in order to trust a file they were
handed, and to decide whether they want it at all.

So a shared lesson is the same Markdown with a few extra front-matter fields:

    - **Diglot Format:** diglot/1
    - **Shared By:** someone@example.com
    - **Shared On:** 2026-09-20
    - **Lesson Origin:** woven by deepseek-v4.1-flash
    - **Body Digest:** 8f2a1c9d4e77

Three deliberate choices in there.

**The stamp goes in the existing front matter, not under a heading of its own.**
The parser reads front matter as a run of field lines; a heading part-way down
would end that run, and the blank lines after it would then be swallowed rather
than closing a paragraph. Fields are also what a person expects to find there.

**Plain text, not a JSON envelope.** A second format would break the guarantee
that what the app reads and what it writes are the same thing, and it would turn
a lesson into something you cannot read without a program.

**The digest covers the lesson, and it is advisory.** It is taken over the title,
the prose and both anchor boxes -- not the front matter, so adding the stamp does
not invalidate it. A mismatch on import means "this has been edited since it was
exported", which is information, not a verdict: hand-fixing a typo in a shared
file is a reasonable thing to do, and refusing to import it would be hostile.

What is deliberately *not* in the file: anything personal. Progress, saved words,
the review schedule, which words the reader has met. Those are the reader's, they
belong to their own database, and a shared lesson that carried them would be
handing over someone else's history.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .diglot import Article, Span, article_chunks, content_fingerprint, parse_article

FORMAT = "diglot/1"

# Written as ``- **Label:** value`` inside the identification block.
LABEL_FORMAT = "Diglot Format"
LABEL_FROM = "Shared By"
LABEL_ON = "Shared On"
LABEL_ORIGIN = "Lesson Origin"
LABEL_DIGEST = "Body Digest"
LABELS = (LABEL_FORMAT, LABEL_FROM, LABEL_ON, LABEL_ORIGIN, LABEL_DIGEST)

# Enough for a lesson and far short of anything that could exhaust memory. An
# imported file is untrusted input and gets a ceiling like any other.
MAX_BYTES = 512 * 1024

_FRONT_HEADINGS = ("article identification", "article confirmation", "article info",
                   "article preview")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FIELD_RE = re.compile(r"^\s*[-*+]?\s*\*\*(.+?):?\*\*\s*:?\s*(.*)$")
# Field labels are matched loosely: someone editing a shared file by hand will
# write "Shared by" or "shared_by" and mean the same thing.
_LABEL_KEYS = {
    "diglot format": "format", "format": "format", "diglot": "format",
    "shared by": "creator", "shared": "creator", "from": "creator", "creator": "creator",
    "shared on": "created", "exported": "created", "created": "created",
    "lesson origin": "origin", "origin": "origin", "made by": "origin",
    "body digest": "digest", "digest": "digest", "checksum": "digest",
}


@dataclass
class Share:
    """What a shared file says about where it came from."""

    format: str = ""
    creator: str = ""
    created: str = ""
    origin: str = ""
    digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"format": self.format, "creator": self.creator, "created": self.created,
                "origin": self.origin, "digest": self.digest}

    @property
    def foreign(self) -> bool:
        """A file from a newer format: readable, but not something to trust
        without saying so."""
        return bool(self.format) and self.format != FORMAT


# --------------------------------------------------------------------------- #
# Integrity
# --------------------------------------------------------------------------- #


def body_digest(article: Article) -> str:
    """A hash of the lesson itself: title, prose, and both anchor boxes.

    Covers the anchors as well as the prose because the vocabulary box is part of
    what the lesson teaches -- a tampered word list is as much a change as a
    tampered paragraph. Excludes the front matter, so stamping a file does not
    invalidate its own digest.
    """
    digest = hashlib.sha256()
    digest.update(article.title.encode("utf-8"))
    for block in article.blocks:
        digest.update(b"\x1f")
        digest.update(block.plain.encode("utf-8"))
    for anchor in article.vocab:
        digest.update(b"\x1e")
        digest.update(f"{anchor.term}|{anchor.gloss or ''}".encode("utf-8"))
    for note in article.grammar:
        digest.update(b"\x1d")
        digest.update(f"{note.title}|{note.example or ''}".encode("utf-8"))
    return digest.hexdigest()[:12]


def verify(text: str) -> tuple[Article | None, Share, bool | None]:
    """Parse a candidate file and check it against its own digest.

    Returns the article (or None if it is not a lesson), the stamp, and whether
    the body still matches the digest: None when there is no digest to check.
    """
    article = parse_article(text, fallback_slug="shared")
    if article is None or not article.blocks:
        return None, Share(), None
    share = read_share(text)
    if not share.digest:
        return article, share, None
    return article, share, share.digest == body_digest(article)


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def _span_markup(span: Span) -> str:
    """One span back into the format's inline markup.

    Both markers have to be *whole whitespace-delimited units* to survive a
    reparse: the parser lifts ``**bold**`` and ``(*gloss*)`` out into placeholder
    tokens and recognises a token only when it is a unit on its own. So a marker
    glued to a neighbouring word is not markup at all -- and since the token is
    what carries the text, a glued ``**bold**`` doesn't just lose its emphasis,
    it loses the word.

    Hence the fiddling with whitespace: emphasis goes inside the span's own
    padding, and the gloss goes immediately after the word it glosses -- which is
    the last word of the span, not after its trailing space -- with that space
    moved out to follow the gloss.
    """
    text = span.text
    if span.lang == "es" and span.bold and text.strip():
        lead = text[: len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()):]
        text = f"{lead}**{text.strip()}**{trail}"
    if span.gloss and text.strip():
        core = text.rstrip()
        trail = text[len(core):]
        text = f"{core} (*{span.gloss}*){trail}"
    return text


def lesson_markdown(article: Article) -> str:
    """The lesson body and anchors, canonical, with no front matter.

    Regenerated from the parsed article rather than copied from a file on disk:
    an article may have come from a compilation holding several, and the corpus
    itself is read-only. Serialising the model gives one shape for every source.
    """
    lines: list[str] = []
    for block in article.blocks:
        if block.kind == "h":
            depth = min(max(block.level or 3, 1), 6)
            lines.append(f"{'#' * depth} {block.plain.strip()}")
        elif block.kind == "byline":
            lines.append(block.plain.strip())
        elif block.kind == "date":
            lines.append(f"*{block.plain.strip()}*")
        else:
            lines.append("".join(_span_markup(s) for s in block.spans).strip())
        lines.append("")

    if article.vocab or article.grammar:
        lines += ["---", "", "### POST-READING ANCHORS", ""]
        if article.vocab:
            lines += ["**Recycled Vocabulary Box**", ""]
            for anchor in article.vocab:
                gloss = f" (*{anchor.gloss}*)" if anchor.gloss else ""
                lines.append(f"- **{anchor.term}**{gloss}")
                # Examples are numbered sub-items; a bullet here would be read as
                # a new headword, or dropped as one that is not.
                for index, example in enumerate(anchor.examples, start=1):
                    lines.append(f"{index}. {example}")
            lines.append("")
        if article.grammar:
            lines += ["**Grammar Breakdown**", ""]
            for index, note in enumerate(article.grammar, start=1):
                # Numbered, not bulleted: only a numbered item opens a new note,
                # and a bulleted one is read as a field of the note before it --
                # which is how a serialised lesson came back with no grammar at
                # all while its vocabulary survived.
                #
                # Title and explanation share the line, which is the shape the
                # corpus uses and the one the parser reads most directly. The
                # example is written verbatim: it already carries its own quotes
                # when it has them, and adding a second pair nests them.
                head = f"{index}. **{note.title.rstrip(':')}:**"
                lines.append(f"{head} {note.explanation}".rstrip() if note.explanation else head)
                if note.example:
                    lines.append(f"- *Example:* {note.example}")
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _front_matter_lines(article: Article) -> list[str]:
    lines = ["### Article Identification & Preview", "", f"- **Article Title:** {article.title}"]
    if article.author:
        lines.append(f"- **Author:** {article.author}")
    if article.url:
        lines.append(f"- **Direct URL:** {article.url}")
    if article.preview:
        lines.append(f"- **Preview:** {article.preview}")
    if article.level:
        lines.append(f"- **Level:** {article.level}")
    if article.register:
        lines.append(f"- **Register:** {article.register}")
    return lines


def share_text(
    article: Article,
    *,
    creator: str = "",
    origin: str = "",
    created: str | None = None,
) -> str:
    """The whole exportable file: front matter, stamp, lesson, anchors."""
    stamp = [
        f"- **{LABEL_FORMAT}:** {FORMAT}",
    ]
    if creator:
        stamp.append(f"- **{LABEL_FROM}:** {creator}")
    stamp.append(f"- **{LABEL_ON}:** {created or datetime.now(timezone.utc).date().isoformat()}")
    if origin:
        stamp.append(f"- **{LABEL_ORIGIN}:** {origin}")
    stamp.append(f"- **{LABEL_DIGEST}:** {body_digest(article)}")

    lines = _front_matter_lines(article) + [""] + stamp + [""]
    lines += [f"# {article.title}", ""]
    if article.author:
        lines += [f"**By {article.author}**", ""]
    return "\n".join(lines) + lesson_markdown(article)


def filename(article: Article) -> str:
    """A safe name for a downloaded file.

    Derived from the title, never from anything in the file, and suffixed with
    the digest so two lessons with the same title cannot collide on disk.
    """
    stem = re.sub(r"[^a-z0-9]+", "-", article.title.lower()).strip("-")[:48].strip("-")
    return f"{stem or 'diglot'}-{body_digest(article)}.md"


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def read_share(text: str) -> Share:
    """Pull the stamp out of a file, or an empty Share if there is none."""
    share = Share()
    for label, value in _front_fields(text):
        key = _LABEL_KEYS.get(label)
        if key and not getattr(share, key):
            setattr(share, key, value)
    return share


def _front_fields(text: str) -> list[tuple[str, str]]:
    """The ``label -> value`` pairs of the front-matter block, lowercased labels."""
    _lines, fields, _first, _last = _scan_front(text)
    return fields


def _scan_front(text: str) -> tuple[list[str], list[tuple[str, str]], int | None, int | None]:
    """Locate the front-matter field run.

    Returns the lines, the fields, and the inclusive index range of the field
    lines -- enough to read the stamp and to splice a new one in. Scanned to the
    same rule the parser uses: the run ends at the first non-field, non-blank
    line, so a ``- **X:**`` in the middle of a paragraph is not front matter.
    """
    lines = text.splitlines()
    fields: list[tuple[str, str]] = []
    first = last = None
    in_front = False
    for index, raw in enumerate(lines):
        line = raw.rstrip()
        heading = _HEADING_RE.match(line)
        if heading:
            low = heading.group(2).strip().lower()
            in_front = any(h in low for h in _FRONT_HEADINGS)
            continue
        if not in_front or not line.strip():
            continue
        field = _FIELD_RE.match(line)
        if not field:
            break
        if first is None:
            first = index
        last = index
        fields.append((field.group(1).strip().lower().rstrip(":"), field.group(2).strip()))
    return lines, fields, first, last


def _stamp_lines(
    article: Article, *, creator: str, origin: str, created: str | None
) -> list[str]:
    out = [f"- **{LABEL_FORMAT}:** {FORMAT}"]
    if creator:
        out.append(f"- **{LABEL_FROM}:** {creator}")
    out.append(f"- **{LABEL_ON}:** {created or datetime.now(timezone.utc).date().isoformat()}")
    if origin:
        out.append(f"- **{LABEL_ORIGIN}:** {origin}")
    out.append(f"- **{LABEL_DIGEST}:** {body_digest(article)}")
    return out


def _is_stamp_line(line: str) -> bool:
    """Whether a front-matter line is one of ours, and so to be replaced.

    Matched against the exact labels rather than the loose read-side keys: a file
    that has its own ``- **Format:**`` field is not carrying our stamp, and
    should keep it.
    """
    field = _FIELD_RE.match(line)
    if not field:
        return False
    return field.group(1).strip().lower().rstrip(":") in {label.lower() for label in LABELS}


def stamp_text(
    source: str,
    *,
    article: Article,
    creator: str = "",
    origin: str = "",
    created: str | None = None,
) -> str:
    """Add or refresh the stamp in a file, leaving the author's text alone.

    Preferred over regenerating from the parsed model, which is canonical but
    lossy: a lesson the tutor wove with malformed markup parses into literal
    text, and re-emitting that text can re-introduce markup where the author had
    none. Exporting someone's own file means never having to be faithful to it.

    Front matter is created when the file has none, because a shareable lesson
    should say what it is -- and because a ``- **Field:** value`` line at the top
    of a file with no front matter would be read as the first line of prose.
    """
    lines, _fields, first, last = _scan_front(source)
    stamp = _stamp_lines(article, creator=creator, origin=origin, created=created)
    trailing = "\n" if source.endswith("\n") else ""

    if first is None:
        head = _front_matter_lines(article) + [""] + stamp + [""]
        return "\n".join(head + lines) + trailing

    kept = [line for line in lines[first:last + 1] if not _is_stamp_line(line)]
    return "\n".join(lines[:first] + kept + stamp + lines[last + 1:]) + trailing


def set_front_matter_field(source: str, label: str, value: str) -> str | None:
    """Set or clear one front-matter field, leaving every other line alone.

    Returns the new text, or ``None`` when the file has no front-matter block to
    write into. Creating one is not offered: it would mean regenerating the block
    from the parsed article, and parsed articles are lossy in exactly the ways that
    matter for a file the reader may have written by hand -- malformed markup parses
    into literal text, and re-emitting it changes what it means. Refusing is the
    honest answer rather than restructuring someone's document to hold one label.

    An empty ``value`` removes the line. That is how a reader takes back a tag they
    set: the app falls back to its own inference and says it is inferring, which is
    a different claim from "the author declared this".

    Same splice as the share stamp -- lines either side are passed through
    untouched -- because the file is the author's, and the app's business in it is
    one line at a time.

    **This edits a file, not a passage, and a file can hold several passages.** The
    corpus ships a compilation with four lessons under day headings and one
    front-matter run between them, so a caller editing "the Register of this
    passage" has to have established that the file holds only that passage -- see
    ``Library.passages_per_file``. Called on a compilation this will write the line
    into whichever passage owns that part of the block, which is a different lesson
    from the one the reader was looking at. It did exactly that once.
    """
    lines, _fields, first, last = _scan_front(source)
    if first is None:
        return None

    wanted = label.strip().lower().rstrip(":")
    kept: list[str] = []
    written = False
    for line in lines[first:last + 1]:
        field = _FIELD_RE.match(line)
        key = field.group(1).strip().lower().rstrip(":") if field else ""
        if key == wanted:
            # The first occurrence is replaced, any later duplicates dropped: two
            # Register lines would leave the reader with a tag they cannot change
            # because the parser reads the first one.
            if value and not written:
                kept.append(f"- **{label}:** {value}")
                written = True
            continue
        kept.append(line)
    if value and not written:
        kept.append(f"- **{label}:** {value}")

    trailing = "\n" if source.endswith("\n") else ""
    return "\n".join(lines[:first] + kept + lines[last + 1:]) + trailing


def export_text(
    entry: Any, *, creator: str = "", origin: str = "", created: str | None = None
) -> str:
    """The file to hand someone for one passage: the author's, stamped.

    The lesson's own text is preferred over regenerating it, always. Regeneration
    is canonical but cannot be faithful to text containing markup -- a sentence
    with an italic aside or a malformed gloss parses into literal asterisks, and
    re-emitting them changes what they mean. So a file is read and stamped, and
    for a compilation the author's chunk of it is taken rather than the whole.
    """
    article = entry.article
    path = getattr(entry, "path", None)
    if path is not None:
        try:
            raw = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw = ""
        wanted = content_fingerprint(article)
        for _day, chunk in article_chunks(raw):
            one, _share, _intact = verify(chunk)
            if one is not None and content_fingerprint(one) == wanted:
                return stamp_text(chunk, article=article, creator=creator,
                                  origin=origin, created=created)
    return share_text(article, creator=creator, origin=origin, created=created)


# --------------------------------------------------------------------------- #
# Inspection -- the pre-flight
# --------------------------------------------------------------------------- #


def inspect(text: str, *, library: Any = None, deck: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Describe a candidate file without importing it.

    This is the point of the whole feature. The question a reader has about a
    file someone sent them is not "will it parse" but *should I read this* --
    what does it teach, how much of that do I already know, and do I already
    have it. An import that only answers the first question is a copy command.
    """
    size = len(text.encode("utf-8"))
    if size > MAX_BYTES:
        return {"ok": False, "error": f"file is {size // 1024} kB; the limit is {MAX_BYTES // 1024} kB"}

    article, share, intact = verify(text)
    if article is None:
        return {"ok": False, "error": "this does not look like a diglot lesson",
                "share": share.to_dict()}

    stats = article.stats()
    result: dict[str, Any] = {
        "ok": True,
        "error": None,
        "share": share.to_dict(),
        "intact": intact,
        "foreign_format": share.foreign,
        "title": article.title,
        "author": article.author,
        "url": article.url,
        "preview": article.preview,
        "level": article.level,
        "register": article.register,
        "stats": stats,
        "suggested_filename": filename(article),
        "teaches": [p.to_dict() for p in article.focus_pairs()[:24]],
        "anchor_count": len(article.vocab),
        "grammar_count": len(article.grammar),
        "duplicate": None,
        "known": None,
    }

    if library is not None:
        result["duplicate"] = _duplicate(article, library)
    if library is not None and deck is not None:
        result["known"] = library.coverage(article, deck)
    return result


def _duplicate(article: Article, library: Any) -> dict[str, Any] | None:
    """An existing passage with the same content, if there is one.

    Matched on the reading text rather than the title: two files can be named the
    same thing and be different lessons, and one lesson can travel under two
    names. The url is checked too, because the same article fetched twice is the
    likeliest duplicate of all and its shake can differ by a paragraph.
    """
    fingerprint = content_fingerprint(article)
    for entry in library.all():
        other = entry.article
        if content_fingerprint(other) == fingerprint:
            return {"slug": other.slug, "title": other.title, "match": "same text"}
        if article.url and other.url and article.url.strip() == other.url.strip():
            return {"slug": other.slug, "title": other.title, "match": "same source"}
    return None


def write(text: str, *, library_dir: Any, slug: str) -> Any:
    """Write an accepted lesson into the library folder.

    The name comes from the slug the caller derived, never from the file, and an
    existing file of that name is not overwritten -- the caller picks a free name
    first, so a second copy of a lesson cannot silently replace the first.
    """
    library_dir.mkdir(parents=True, exist_ok=True)
    path = library_dir / f"{slug}.md"
    if path.exists():
        raise FileExistsError(path)
    path.write_text(text, encoding="utf-8")
    return path


if __name__ == "__main__":  # pragma: no cover - manual inspection helper
    import sys
    from pathlib import Path

    for target in sys.argv[1:]:
        source = Path(target).read_text(encoding="utf-8", errors="replace")
        info = inspect(source)
        print(f"{target}: ok={info['ok']} intact={info.get('intact')} "
              f"title={info.get('title')!r} {info.get('stats', {}).get('spanish_ratio')}")
