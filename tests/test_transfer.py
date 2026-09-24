"""Tests for passing a lesson to someone.

Two things are being pinned here, and they are different in kind.

The first is *fidelity*: a lesson must survive the trip out and back with the
same words, the same vocabulary notes and the same grammar notes. That is the
test that matters, and it is the one that found the bugs -- a serialiser that
emitted `**presento**un` lost the word, and a grammar block written with bullets
came back with no grammar at all. Both were invisible to every other test in the
suite, because nothing else re-reads what this app writes.

The second is *honesty*: the stamp must say who made the lesson, the digest must
notice a change without refusing the file, and an import must never quietly
overwrite a lesson the reader already has.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import corpus_dir  # noqa: E402
from corpus_support import has_real_corpus  # noqa: E402

from app import transfer  # noqa: E402
from app.diglot import content_fingerprint, parse_article, parse_file  # noqa: E402

CORPUS = corpus_dir()
IMPORTS = Path(__file__).resolve().parents[1] / "data" / "library"


class Entry:
    """A LibraryEntry, without building a Library around it."""

    def __init__(self, article, path=None, imported=True):
        self.article = article
        self.imported = imported
        self.path = path


def _real_lessons(folder):
    """Every lesson on disk, with the file it came from -- which is what the
    export path actually uses."""
    if not folder.is_dir():
        return []
    return [(article, path)
            for path in sorted(folder.glob("*.md"))
            if not path.name.lower().startswith("readme")
            for article in parse_file(path)]


LESSON = """### Article Identification & Preview

- **Article Title:** A Woven Lesson
- **Author:** Someone
- **Direct URL:** https://example.com/a
- **Preview:** A short preview of what this is about.
- **Register:** essay

# A Woven Lesson

**By Someone**

El arte se vuelve **más personal** (*more personal*) cada vez, y los anales (*annals*)
de la historia del arte **pintan** (*they paint*) un panorama diferente.

---

### POST-READING ANCHORS

**Recycled Vocabulary Box**

- **volverse** / **se vuelve** (*to become*)
1. Se vuelve más difícil con el tiempo.

**Grammar Breakdown**

1. **Present Tense:** The third person singular is used for general statements.
- *Example:* "El arte se vuelve personal."
"""


def parsed(text: str = LESSON):
    article = parse_article(text, fallback_slug="a-woven-lesson")
    assert article is not None
    return article


# ---------------------------------------------------- editing the front matter --


def test_setting_a_register_replaces_the_one_already_in_the_file():
    out = transfer.set_front_matter_field(LESSON, "Register", "academic")
    assert "- **Register:** academic" in out
    assert "- **Register:** essay" not in out


def test_editing_one_line_leaves_every_other_line_exactly_as_it_was():
    """The file may be the reader's own writing, so the edit is one line: nothing is
    reflowed, nothing is re-emitted from the parsed model, and the malformed glosses
    this format has to survive stay exactly as malformed as they were."""
    out = transfer.set_front_matter_field(LESSON, "Register", "news")
    assert out.replace("- **Register:** news\n", "") == LESSON.replace("- **Register:** essay\n", "")


def test_a_tag_can_be_added_to_a_passage_that_has_none():
    without = LESSON.replace("- **Register:** essay\n", "")
    out = transfer.set_front_matter_field(without, "Register", "fiction")
    assert "- **Register:** fiction" in out
    # ...and it lands where the parser will read it, not after the front matter ends.
    assert parsed(out).register == "fiction"


def test_clearing_a_tag_removes_the_line_rather_than_emptying_it():
    """A line with an empty value would parse as a *declared* register of nothing and
    shadow the inference; removing it hands the decision back to the app, which
    then says the tag is inferred."""
    out = transfer.set_front_matter_field(LESSON, "Register", "")
    assert "Register" not in out
    assert not parsed(out).register


def test_a_second_register_line_is_dropped_while_setting_one():
    """Two of them would leave the reader with a tag they cannot change, because the
    parser reads the first."""
    doubled = LESSON.replace("- **Register:** essay",
                             "- **Register:** essay\n- **Register:** news")
    out = transfer.set_front_matter_field(doubled, "Register", "academic")
    assert out.count("**Register:**") == 1
    assert parsed(out).register == "academic"


def test_the_other_front_matter_fields_survive():
    out = transfer.set_front_matter_field(LESSON, "Register", "news")
    assert "### Article Identification & Preview" in out
    assert "- **Article Title:** A Woven Lesson" in out
    assert "- **Preview:** A short preview of what this is about." in out
    assert out.endswith("\n")


def test_a_file_with_no_front_matter_is_refused_rather_than_restructured():
    """Writing a block would mean regenerating it from the parsed article, and a
    parsed article is lossy in exactly the ways that matter for a file the reader
    wrote by hand. Refusing is the honest answer."""
    prose = "El arte se vuelve **más personal** (*more personal*) cada vez.\n"
    assert transfer.set_front_matter_field(prose, "Register", "essay") is None


# ------------------------------------------------------------------ fidelity --


@pytest.mark.parametrize("folder", [CORPUS, IMPORTS])
def test_every_lesson_survives_the_round_trip(folder):
    """The test that found the serialiser's bugs.

    Every real lesson is exported and read back, and the reading text, the
    vocabulary and the grammar must all be identical. Deliberately run over the
    files on disk rather than over fixtures: the fixtures were all well-formed,
    and both failures were in markup a model actually produced. One import in the
    corpus is malformed enough that regenerating it cannot be faithful, which is
    the whole reason export stamps the author's own file instead.
    """
    lessons = _real_lessons(folder)
    if not lessons:
        pytest.skip(f"no lessons at {folder}")
    broken = []
    for article, path in lessons:
        out = transfer.export_text(Entry(article, path), creator="alice", origin="hand-made")
        back, _share, intact = transfer.verify(out)
        if back is None:
            broken.append(f"{article.slug}: unparseable")
            continue
        problems = []
        if content_fingerprint(article) != content_fingerprint(back):
            problems.append("reading text differs")
        if intact is not True:
            problems.append(f"digest mismatch ({intact})")
        if article.vocab != back.vocab:
            problems.append("vocabulary differs")
        if article.grammar != back.grammar:
            problems.append("grammar differs")
        if problems:
            broken.append(f"{article.slug}: {', '.join(problems)}")
    assert not broken, "round trip lost something:\n" + "\n".join(broken)


def test_export_prefers_the_authors_file_over_regenerating_it():
    """Why the export path reads a file instead of writing one.

    Regeneration from the parsed model cannot always be faithful to text that
    contains markup: a sentence with an italic aside or a malformed gloss parses
    into literal asterisks, and re-emitting them changes what they mean. The test
    finds at least one real lesson where that happens and shows the file-copying
    path handling it -- rather than naming the lessons, because which ones is a
    consequence of the parser and not a fact worth maintaining.
    """
    lessons = _real_lessons(IMPORTS) + _real_lessons(CORPUS)
    if len(lessons) < 15:
        pytest.skip(f"needs a shelf of lessons to find one of them lossy ({len(lessons)} here)")
    lossy = 0
    for article, path in lessons:
        regenerated, _share, intact = transfer.verify(
            transfer.share_text(article, creator="alice"))
        if regenerated is not None and intact is True:
            continue
        lossy += 1
        copied, _s, copied_intact = transfer.verify(
            transfer.export_text(Entry(article, path), creator="alice"))
        assert copied is not None and copied_intact is True, article.slug
        assert content_fingerprint(copied) == content_fingerprint(article), article.slug
    assert lossy, "no lesson regenerates lossily; this test's premise is gone"


def test_every_real_lesson_exports_faithfully_including_compilations():
    """A compilation holds several lessons in one file, so export has to take the
    author's own chunk of it. Stamping the whole file would export all of them
    under one name, and regenerating the chunk is lossy."""
    lessons = _real_lessons(IMPORTS) + _real_lessons(CORPUS)
    if not lessons:
        pytest.skip("no lessons on disk to export")
    files = {path for _article, path in lessons}
    for article, path in lessons:
        out = transfer.export_text(Entry(article, path), creator="alice")
        back, _share, intact = transfer.verify(out)
        assert back is not None and intact is True, article.slug
        assert content_fingerprint(back) == content_fingerprint(article), article.slug
        # One lesson per export, never a whole compilation.
        assert not re.search(r"^#{1,6}\s+Day\s+\d+", out, re.M), article.slug
    # The compilation half only means something where there is one to export; the
    # fidelity checks above ran either way.
    if not any(len(parse_file(path)) > 1 for path in {path for _article, path in lessons}):
        pytest.skip("no compilation in this corpus, so that half proves nothing")


def test_a_bold_word_keeps_its_surrounding_spaces():
    """`**presento**un` is not markup to the parser, and because the placeholder
    carries the text, a bold span written that way loses the word entirely."""
    text = "El arte **pintan** un panorama, y **sigue** (*continues*) igual."
    block = parsed(f"# T\n\n{text}").blocks[0]
    out = "".join(transfer._span_markup(s) for s in block.spans)
    assert "**pintan**" in out and "**sigue**" in out
    reread = parse_article(f"# T\n\n{text}", fallback_slug="x")
    assert reread is not None
    assert "pintan" in reread.blocks[0].plain
    assert "sigue" in reread.blocks[0].plain


def test_a_gloss_goes_after_the_word_not_after_the_space():
    """The gloss attaches to the span it *ends* -- the word, not the span's
    trailing space. Written after the space it lands before one instead, which
    makes it a unit of its own and glues the gloss to the next word."""
    text = "El arte **pintan** (*they paint*) un panorama diferente."
    block = parsed(f"# T\n\n{text}").blocks[0]
    out = "".join(transfer._span_markup(s) for s in block.spans)
    assert "**pintan** (*they paint*) un panorama" in out, out
    # and the whole thing survives a reparse, gloss attached to the right word
    back = parse_article(f"# T\n\n{out}", fallback_slug="t")
    assert back is not None
    assert back.blocks[0].plain == block.plain
    glossed = [s for s in back.blocks[0].spans if s.gloss]
    assert [s.gloss for s in glossed] == ["they paint"]


def test_the_anchors_are_written_in_shapes_the_parser_reads():
    """A bulleted grammar title is read as a field of the *previous* note, so a
    lesson serialised that way comes back with no grammar at all."""
    article = parsed()
    out = transfer.lesson_markdown(article)
    assert "1. **Present Tense:**" in out, out
    assert "- *Example:*" in out
    back = parse_article(f"# T\n\n{out}", fallback_slug="t")
    assert back is not None
    assert len(back.grammar) == 1
    assert back.grammar[0].title == "Present Tense"


# ------------------------------------------------------------------- digest --


def test_the_digest_ignores_front_matter():
    """Otherwise stamping a file would invalidate its own digest."""
    article = parsed()
    stamped = transfer.share_text(article, creator="alice", origin="hand-made")
    assert transfer.read_share(stamped).digest == transfer.body_digest(article)


def test_the_digest_covers_the_vocabulary_box():
    """A tampered word list is as much a change as a tampered paragraph -- the
    box is part of what the lesson teaches."""
    article = parsed()
    article.vocab[0].gloss = "something else entirely"
    assert transfer.body_digest(article) != transfer.body_digest(parsed())


def test_the_digest_covers_the_grammar_notes():
    article = parsed()
    article.grammar[0].title = "Something Else"
    assert transfer.body_digest(article) != transfer.body_digest(parsed())


def test_a_change_is_reported_without_refusing_the_file():
    """Advisory, not a verdict: hand-fixing a typo in a shared lesson is a
    reasonable thing to do, and refusing to read it would be hostile."""
    text = transfer.share_text(parsed(), creator="alice")
    edited = text.replace("panorama diferente", "panorama muy diferente")
    article, share, intact = transfer.verify(edited)
    assert article is not None, "it still parses"
    assert intact is False, "and it says so"
    assert share.creator == "alice"

    report = transfer.inspect(edited)
    assert report["ok"] is True
    assert report["intact"] is False


def test_a_file_with_no_digest_reports_no_opinion():
    article, _share, intact = transfer.verify(LESSON)
    assert article is not None
    assert intact is None, "no digest is not the same as a failing digest"


# -------------------------------------------------------------------- stamp --


def test_the_stamp_records_who_and_how():
    article = parsed()
    text = transfer.share_text(article, creator="alice", origin="woven by a model")
    share = transfer.read_share(text)
    assert share.format == transfer.FORMAT
    assert share.creator == "alice"
    assert share.origin == "woven by a model"
    assert share.created and share.digest
    assert share.foreign is False


def test_a_newer_format_is_flagged_rather_than_trusted():
    text = transfer.share_text(parsed()).replace("diglot/1", "diglot/99")
    share = transfer.read_share(text)
    assert share.foreign is True
    assert transfer.inspect(text)["foreign_format"] is True


def test_restamping_replaces_rather_than_stacks():
    """A lesson shared twice should carry one stamp, not a growing stack."""
    article = parsed()
    once = transfer.share_text(article, creator="alice")
    twice = transfer.stamp_text(once, article=article, creator="bob")
    assert twice.count("Diglot Format") == 1
    assert twice.count("Shared By") == 1
    assert transfer.read_share(twice).creator == "bob", "the newest sender wins"


def test_a_file_with_no_front_matter_gets_one():
    """A bare ``- **Field:** value`` at the top of a file with no front matter is
    read as the first line of prose, so the block has to be created."""
    article = parsed()
    bare = transfer.lesson_markdown(article)
    stamped = transfer.stamp_text(bare, article=article, creator="alice")
    assert stamped.startswith("### Article Identification & Preview")
    back, share, _intact = transfer.verify(stamped)
    assert back is not None
    assert share.creator == "alice"
    assert content_fingerprint(back) == content_fingerprint(article), "and the prose is untouched"


def test_a_files_own_format_field_is_not_ours_to_replace():
    text = LESSON.replace("- **Author:** Someone", "- **Format:** a house style\n- **Author:** Someone")
    article = parsed(text)
    stamped = transfer.stamp_text(text, article=article, creator="alice")
    assert "a house style" in stamped
    assert stamped.count("Diglot Format") == 1


def test_a_filename_is_safe_and_content_addressed():
    article = parsed()
    name = transfer.filename(article)
    assert name.endswith(".md")
    assert "/" not in name and "\\" not in name and ":" not in name
    assert transfer.body_digest(article) in name, "two lessons with one title cannot collide"


# ------------------------------------------------------------------- inspect --


def test_something_that_is_not_a_lesson_is_refused_with_a_reason():
    for junk in ("", "   ", "just some prose, no front matter, no anchors",
                 "<html><body>a web page</body></html>"):
        report = transfer.inspect(junk)
        assert report["ok"] is False, junk
        assert report["error"]


def test_inspect_describes_what_the_lesson_is():
    report = transfer.inspect(transfer.share_text(parsed(), creator="alice", origin="hand-made"))
    assert report["ok"] is True
    assert report["title"] == "A Woven Lesson"
    assert report["author"] == "Someone"
    assert report["register"] == "essay"
    assert report["stats"]["words"] > 0
    assert report["anchor_count"] == 1
    assert report["grammar_count"] == 1
    assert report["share"]["creator"] == "alice"
    assert [t["es"] for t in report["teaches"]], "the lesson's own syllabus is listed"


def test_an_oversized_file_is_refused_before_it_is_parsed():
    report = transfer.inspect("x" * (transfer.MAX_BYTES + 1))
    assert report["ok"] is False
    assert "limit" in report["error"]


class FakeLibrary:
    """Just enough of Library for duplicate detection and coverage."""

    def __init__(self, entries=()):
        self._entries = list(entries)

    def all(self):
        return self._entries

    def coverage(self, article, deck):
        pairs = article.focus_pairs()
        known = {w["term"] for w in deck}
        hit = [p for p in pairs if p.es.strip() in known]
        return {"total": len(pairs), "known": len(hit),
                "ratio": round(len(hit) / len(pairs), 3) if pairs else 1.0}


def test_a_lesson_already_on_the_shelf_is_recognised():
    existing = parsed()
    report = transfer.inspect(
        transfer.share_text(existing), library=FakeLibrary([Entry(existing)])
    )
    assert report["duplicate"]["match"] == "same text"


def test_the_same_source_under_a_different_weave_is_still_a_duplicate():
    """The same article fetched twice is the likeliest duplicate of all, and its
    Spanish can differ by a paragraph."""
    existing = parsed()
    other = parsed(LESSON.replace("un panorama diferente", "un panorama bastante diferente"))
    assert content_fingerprint(other) != content_fingerprint(existing)
    report = transfer.inspect(
        transfer.share_text(other), library=FakeLibrary([Entry(existing)])
    )
    assert report["duplicate"]["match"] == "same source"


def test_a_genuinely_new_lesson_is_not_a_duplicate():
    report = transfer.inspect(
        transfer.share_text(parsed()), library=FakeLibrary([Entry(parsed(
            LESSON.replace("A Woven Lesson", "Another Lesson")
                  .replace("https://example.com/a", "https://example.com/b")))]))
    assert report["duplicate"] is None


def test_inspect_reports_how_much_of_the_lesson_is_already_known():
    """The question a recipient actually has: is this worth my time?"""
    text = transfer.share_text(parsed())
    empty = transfer.inspect(text, library=FakeLibrary(), deck=[])
    assert empty["known"]["known"] == 0
    full = transfer.inspect(text, library=FakeLibrary(), deck=[{"term": "pintan"}])
    assert full["known"]["known"] >= 1


# ---------------------------------------------------------------------- write --


def test_writing_never_overwrites_an_existing_lesson(tmp_path):
    out = transfer.write(LESSON, library_dir=tmp_path, slug="a-lesson")
    assert out.exists()
    with pytest.raises(FileExistsError):
        transfer.write(LESSON.replace("Someone", "Someone Else"), library_dir=tmp_path, slug="a-lesson")
    assert out.read_text(encoding="utf-8") == LESSON, "the first copy is untouched"


def test_writing_creates_the_folder_it_is_given(tmp_path):
    target = tmp_path / "not" / "yet"
    out = transfer.write(LESSON, library_dir=target, slug="a-lesson")
    assert out.parent.is_dir()
