"""Tests for the scheduler, the store, and the recommendation scoring."""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import diglot  # noqa: E402
from app import ingest  # noqa: E402
from app import levels  # noqa: E402
from app import srs  # noqa: E402
from app import vocab  # noqa: E402
from app.recommend import probe_words, score_text  # noqa: E402
from app.store import Store  # noqa: E402

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------- srs --


def test_first_review_ratings_differ():
    """The four buttons must not all show the same next-due date -- if they do,
    the learner concludes the buttons are decorative."""
    state = srs.CardState()
    intervals = {r: srs.review(state, r, NOW)[1] for r in ("hard", "good", "easy")}
    assert intervals["hard"] < intervals["good"] < intervals["easy"]


def test_good_then_good_grows():
    state, due = srs.review(srs.CardState(), "good", NOW)
    assert state.reps == 1
    later, due2 = srs.review(state, "good", due)
    assert later.reps == 2
    assert later.interval_days > state.interval_days
    assert due2 > due


def test_again_resets_and_returns_soon():
    state, _ = srs.review(srs.CardState(), "good", NOW)
    lapsed, due = srs.review(state, "again", NOW)
    assert lapsed.reps == 0
    assert lapsed.lapses == 1
    assert due - NOW <= timedelta(minutes=15)
    assert lapsed.stage == srs.LEARNING


def test_again_keeps_the_ease():
    state, _ = srs.review(srs.CardState(), "easy", NOW)
    lapsed, _ = srs.review(state, "again", NOW)
    assert lapsed.ease == pytest.approx(state.ease)


def test_ease_rises_with_easy_and_falls_with_hard():
    base = srs.CardState(reps=3, interval_days=10)
    easy, _ = srs.review(base, "easy", NOW)
    hard, _ = srs.review(base, "hard", NOW)
    assert easy.ease > base.ease > hard.ease


def test_ease_has_a_floor():
    state = srs.CardState(ease=srs.MIN_EASE, reps=3, interval_days=5)
    for _ in range(20):
        state, _ = srs.review(state, "hard", NOW)
    assert state.ease >= srs.MIN_EASE


def test_interval_is_capped():
    state = srs.CardState(ease=2.5, reps=20, interval_days=srs.MAX_INTERVAL_DAYS)
    grown, _ = srs.review(state, "easy", NOW)
    assert grown.interval_days <= srs.MAX_INTERVAL_DAYS


def test_hard_does_not_shorten_an_established_interval():
    state = srs.CardState(reps=5, interval_days=60)
    grown, _ = srs.review(state, "hard", NOW)
    assert grown.interval_days > 0


def test_stages_progress():
    assert srs.CardState().stage == srs.NEW
    learning = srs.CardState(reps=1, interval_days=0.5)
    assert learning.stage == srs.LEARNING
    assert srs.CardState(reps=2, interval_days=6).stage == srs.YOUNG
    assert srs.CardState(reps=6, interval_days=40).stage == srs.MATURE


def test_unknown_rating_is_rejected():
    with pytest.raises(ValueError):
        srs.review(srs.CardState(), "perfect")


def test_preview_labels_every_button():
    preview = srs.preview(srs.CardState(), NOW)
    assert set(preview) == set(srs.RATINGS)
    assert all(isinstance(v, str) and v for v in preview.values())


# ------------------------------------------------------------------- store --


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def test_saving_the_same_word_twice_is_idempotent(store):
    first = store.add_word(term="se vuelve", lemma="volverse", gloss="becomes")
    second = store.add_word(term="se vuelve", lemma="volverse", gloss="becomes")
    assert first["id"] == second["id"]
    assert store.word_count() == 1


def test_a_new_word_is_due_immediately(store):
    store.add_word(term="genoma", gloss="genome")
    cards = store.due_cards()
    assert len(cards) == 1
    assert cards[0]["term"] == "genoma"
    assert cards[0]["stage"] == "new"


def test_a_card_carries_the_dictionary_form_it_is_a_form_of(store):
    """A word is saved in whatever form it was met in, so the card reviews
    ``enfatizan`` -- and reviewing the form is not the same knowledge as knowing
    the verb. The reveal needs the lemma to say so, on both the due query and the
    new-card one, which are written separately and drifted apart before."""
    store.add_word(term="enfatizan", lemma="enfatizar", pos="verb", gloss="they emphasise")
    fresh = store.due_cards()
    assert fresh[0]["lemma"] == "enfatizar" and fresh[0]["pos"] == "verb"

    # ...and on the due path, which is a separate query with its own column list,
    # so it has to carry the same thing. Placed in the past rather than reached by
    # rating, because a rating schedules forward whatever it says.
    with store.write() as conn:
        conn.execute("UPDATE cards SET due_at = '2020-01-01T00:00:00+00:00', reps = 1 WHERE id = ?",
                     (fresh[0]["card_id"],))
    due = store.due_cards(include_new=0)
    assert due and due[0]["lemma"] == "enfatizar" and due[0]["pos"] == "verb"


def test_reviewing_schedules_the_card_forward(store):
    store.add_word(term="genoma", gloss="genome")
    card = store.due_cards()[0]
    result = store.review_card(card["card_id"], "good")
    assert result["interval_days"] == pytest.approx(1.0)
    assert store.due_count() == 0
    stages = store.stats()["stages"]
    assert stages["young"] == 1


def test_reviewing_an_unknown_card_raises(store):
    with pytest.raises(KeyError):
        store.review_card(999, "good")


def test_new_card_intake_is_capped(store):
    for index in range(30):
        store.add_word(term=f"palabra{index}", gloss=f"word{index}")
    assert len(store.due_cards(limit=40, include_new=5)) == 5
    assert len(store.due_cards(limit=40, include_new=50)) == 30


def test_known_terms_tracks_the_deck(store):
    store.add_word(term="Se Vuelve", lemma="volverse")
    assert "volverse" in store.known_terms()


def test_progress_is_accumulated_not_replaced(store):
    store.save_progress("a", position=0.2, seconds=30)
    store.save_progress("a", position=0.5, seconds=20)
    progress = store.progress_for("a")
    assert progress["position"] == 0.5
    assert progress["seconds_spent"] == 50


def test_completion_is_sticky(store):
    store.save_progress("a", position=1.0, completed=True)
    store.save_progress("a", position=0.1)
    assert store.progress_for("a")["completed_at"] is not None


def test_streak_counts_back_from_today(store):
    store.log_event("read")
    assert store.streak()["current"] >= 1


def test_activity_fills_empty_days(store):
    store.log_event("review")
    activity = store.activity(7)
    assert len(activity) == 7
    assert activity[-1]["reviews"] == 1
    assert activity[0]["reviews"] == 0


def test_cache_round_trips_json(store):
    store.cache_put("k", {"a": [1, 2, 3]})
    assert store.cache_get("k") == {"a": [1, 2, 3]}
    assert store.cache_get("missing") is None


def test_export_includes_schedule(store):
    store.add_word(term="genoma", gloss="genome", context="el genoma humano")
    row = store.export_rows()[0]
    assert row["term"] == "genoma"
    assert row["context"] == "el genoma humano"


# ---------------------------------------------------------------- vocabulary --


def test_stem_matches_inflections():
    assert vocab.stem("volverse") == vocab.stem("vuelve")
    assert vocab.stem("pensar") == vocab.stem("piensa")
    assert vocab.stem("obligar") == vocab.stem("obligó")


def test_stem_sees_through_the_reflexive():
    assert vocab.stem("volverse") == vocab.stem("se vuelve")
    assert vocab.stem("quedarse con") == vocab.stem("me quedo")


def test_stem_keeps_different_words_apart():
    assert vocab.stem("arte") != vocab.stem("artistas")
    assert vocab.stem("casa") != vocab.stem("cosa")


def test_content_word_picks_the_meaningful_word():
    assert vocab.content_word("se ponen de acuerdo") == "acuerdo"
    assert vocab.content_word("dar lugar a") == "lugar"
    assert vocab.content_word("volverse") == "volverse"


def test_known_stems_indexes_every_word_of_a_phrase():
    stems = vocab.known_stems([{"term": "dar lugar a", "lemma": "dar lugar a"}])
    assert vocab.stem("lugar") in stems


def test_article_vocabulary_skips_function_words():
    from app.diglot import parse_article

    article = parse_article(
        "- **Article Title:** T\n\nEl arte de la pintura y el **genoma** humano.\n",
        fallback_slug="t",
    )
    counts = vocab.article_vocabulary(article)
    assert "arte" in counts and "pintura" in counts
    assert "de" not in counts and "el" not in counts


def test_text_coverage_rises_with_the_deck():
    from app.diglot import parse_article

    article = parse_article(
        "- **Article Title:** T\n\nEl arte del genoma y la pintura del arte.\n",
        fallback_slug="t",
    )
    empty = vocab.text_coverage(article, [])
    some = vocab.text_coverage(article, [{"term": "el arte", "lemma": "arte"}])
    assert empty["token_ratio"] == 0.0
    assert some["token_ratio"] > empty["token_ratio"]
    assert some["known_types"] == 1


def test_text_coverage_matches_inflections():
    from app.diglot import parse_article

    article = parse_article(
        "- **Article Title:** T\n\nEl arte se vuelve más personal cada vez.\n",
        fallback_slug="t",
    )
    coverage = vocab.text_coverage(article, [{"term": "volverse", "lemma": "volverse"}])
    assert coverage["token_ratio"] > 0


def test_verdict_bands():
    assert vocab.verdict(0.99)[0] == "comfortable"
    assert vocab.verdict(0.93)[0] == "stretch"
    assert vocab.verdict(0.80)[0] == "demanding"
    assert vocab.verdict(0.40)[0] == "immersion"


# ------------------------------------------------------------------- ingest --


def test_imported_headings_survive_so_the_passage_gets_a_contents_pane(monkeypatch, tmp_path):
    """Headings were extracted from the page and then thrown away, so every
    imported passage had no sections at all -- and so no contents pane, which
    looks like a broken feature rather than an absent one."""
    from app.llm import LLMResult

    class Client:
        @staticmethod
        def complete_json(prompt, **kwargs):
            return {"items": [{"es": "el gato", "en": "the cat", "kind": "noun"}]}, None

        @staticmethod
        def complete(*a, **k):
            return LLMResult(text="El gato está aquí. The cat is here in the room.")

    class Tutor:
        client = Client()

    blocks = [
        {"kind": "p", "text": "Opening paragraph about reading. " * 12},
        {"kind": "h", "text": "The First Section"},
        {"kind": "p", "text": "Middle paragraph about reading. " * 12},
        {"kind": "h", "text": "The Second Section"},
        {"kind": "p", "text": "Closing paragraph about reading. " * 12},
    ]
    monkeypatch.setattr(ingest, "extract_article",
                        lambda html: {"title": "T", "byline": None, "blocks": blocks,
                                      "words": 400, "dropped_citations": 0})
    monkeypatch.setattr(ingest, "fetch_url", lambda *a, **k: ("<html></html>", "https://example.com/a"))

    class Settings:
        proxy = None
        library_dir = tmp_path

    result = ingest.import_article(url="https://example.com/a", settings=Settings(),
                                   tutor=Tutor(), level_code="auto", ratio=0.38)
    assert result["headings"] == 2, result

    written = (tmp_path / f"{result['slug']}.md").read_text(encoding="utf-8")
    article = diglot.parse_article(written, fallback_slug="x")
    assert article is not None
    headings = [b.plain.strip() for b in article.blocks if b.kind == "h"]
    assert headings == ["The First Section", "The Second Section"], headings

    # And in place, not appended: a section heading belongs between the
    # paragraphs it separates.
    order = [b.kind for b in article.blocks]
    first_heading = order.index("h")
    assert "p" in order[:first_heading] and "p" in order[first_heading:], order


def test_a_summarised_passage_is_retried():
    """Weaving swaps phrases between languages; it must not change how much is
    said. A passage that comes back at half length has been summarised or
    truncated -- the one failure the reader cannot see for themselves."""
    from app import levels
    from app.llm import LLMResult

    full = ("El gato está en la casa y el perro duerme. The cat is in the house "
            "and the dog is sleeping nearby. ") * 12
    short = "El gato está en la casa. The cat is in the house."

    class Client:
        def __init__(self, replies):
            self.replies, self.calls = list(replies), 0

        def complete(self, *a, **k):
            text = self.replies[min(self.calls, len(self.replies) - 1)]
            self.calls += 1
            return LLMResult(text=text)

    class Tutor:
        def __init__(self, replies):
            self.client = Client(replies)

    chunk = ("The cat is in the house and the dog is sleeping nearby. " * 12).strip()
    result = ingest.weave_chunk(Tutor([short, full]), chunk=chunk, focus=[],
                                target=0.5, level=levels.resolve_level("auto"))
    assert any("kept only" in note for note in result.notes), result.notes


def test_a_full_length_passage_is_not_flagged():
    from app import levels
    from app.llm import LLMResult

    full = ("El gato está en la casa y el perro duerme. The cat is in the house "
            "and the dog is sleeping nearby. ") * 12

    class Client:
        def complete(self, *a, **k):
            return LLMResult(text=full)

    class Tutor:
        client = Client()

    chunk = ("The cat is in the house and the dog is sleeping nearby. " * 12).strip()
    result = ingest.weave_chunk(Tutor(), chunk=chunk, focus=[],
                                target=0.5, level=levels.resolve_level("auto"))
    assert not any("kept only" in note for note in result.notes), result.notes


def test_redistribute_rebuilds_a_merged_paragraph():
    """A Wikipedia import came back as one 285-word paragraph. The content was
    all there; only the breaks were missing, and they are recoverable from the
    source's shape."""
    source = ["word " * n for n in (40, 80, 20, 60)]
    merged = [" ".join(f"Sentence {i} of the passage." for i in range(16))]
    out = ingest._redistribute(merged, source)
    assert len(out) == len(source), "one paragraph per source paragraph"
    assert all(p.strip() for p in out), "no empty paragraphs"
    # Longer source paragraphs get more of the sentences.
    counts = [len(re.findall(r"Sentence", p)) for p in out]
    assert counts[1] > counts[2], counts
    assert sum(counts) == 16, "no sentences lost"


def test_redistribute_leaves_an_already_correct_split_alone():
    source = ["a b c d e", "f g h i j"]
    already = ["one two three.", "four five six."]
    assert ingest._redistribute(already, source) == already


def test_redistribute_gives_up_rather_than_losing_text():
    """Two sentences cannot be dealt into five paragraphs without inventing
    text or dropping it, so the original is returned untouched."""
    out = ingest._redistribute(["Only one sentence."], ["a b", "c d", "e f"])
    assert out == ["Only one sentence."]


def long_paragraph(spanish_fraction: float) -> str:
    spanish = "el gato negro está en la casa grande y el perro duerme"
    english = "the cat is in the big house and the dog is sleeping nearby now"
    take = int(round(spanish_fraction * 10))
    return " ".join([spanish] * take + [english] * (10 - take))


def test_spread_measures_the_gap_between_paragraphs():
    lumpy = [long_paragraph(1.0), long_paragraph(0.0), long_paragraph(0.5)]
    even = [long_paragraph(0.5), long_paragraph(0.5), long_paragraph(0.5)]
    assert ingest._spread(lumpy) > 0.8
    assert ingest._spread(even) < 0.1
    assert ingest._spread(even) < ingest._spread(lumpy)


def test_spread_ignores_paragraphs_too_short_to_mean_anything():
    assert ingest._spread(["El gato.", "The cat is here.", "El perro está aquí."]) == 0.0
    assert ingest._spread([]) == 0.0
    assert ingest._spread([long_paragraph(0.5)]) == 0.0, "one paragraph has no spread"


# ------------------------------------------------------------------- ingest --


def weave_prompt(target: float, level_code: str = "auto", weave: str = "chunk") -> str:
    from app import ingest
    from app.levels import resolve_level

    return ingest._weave_prompt(
        chunk="The camera did not kill painting.",
        focus=[{"es": "la cámara", "en": "the camera"}],
        target=target,
        level=resolve_level(level_code),
        weave=weave,
    )


def test_prompt_states_the_requested_amount():
    assert "22%" in weave_prompt(0.22)
    assert "68%" in weave_prompt(0.68)


def test_a_retry_corrects_against_what_was_asked_not_against_the_target():
    """The correction has to be measured from the number the model was given.

    A live import made the mistake obvious. Asked for 54%, the model returned
    81%; the retry asked for 27%. It obeyed and produced 29% -- close to the ask
    and far from the target -- so a correction measured against the *target*
    swung the next ask to 79%. The loop oscillated instead of converging, which
    is exactly the failure the correction was added to fix.
    """
    from app import levels, ingest
    from app.llm import LLMResult

    # Two passages with known mixtures, long enough that the length gate is not
    # what is being tested. Measured rather than assumed.
    heavy = ("La casa es grande y el perro duerme en el jardín tranquilo. "
             "El sol calienta la piedra y el viento mueve las hojas. One English clause here. ") * 8
    light = ("La casa es grande. The dog is sleeping in the quiet garden tonight and the sun "
             "warms the old stone wall beside the river. ") * 8
    assert ingest._ratio_of(heavy) > 0.8 and ingest._ratio_of(light) < 0.2

    class Client:
        def __init__(self):
            self.prompts: list[str] = []

        def complete(self, prompt, *a, **k):
            self.prompts.append(prompt)
            # Overshoot, then obey the corrected number.
            return LLMResult(text=heavy if len(self.prompts) == 1 else light)

    class Tutor:
        def __init__(self):
            self.client = Client()

    tutor = Tutor()
    # Sized so the length gate passes for both fake answers: a shrunken passage
    # is retried for *that*, and the amount correction under test never runs.
    chunk = ("The dog is sleeping in the quiet garden and the sun warms the old stone "
             "wall beside the river tonight. " * 10).strip()
    result = ingest.weave_chunk(tutor, chunk=chunk, focus=[], target=0.50,
                                level=levels.resolve_level("auto"))

    asks = [float(note.split("retrying at ")[1].rstrip("%")) / 100
            for note in result.notes if "retrying at" in note]
    assert len(asks) >= 2, result.notes
    assert asks[0] < 0.20, f"the first correction should come down hard: {asks}"
    # The second ask refines the first rather than swinging back to the target.
    assert asks[1] < asks[0] + 0.02, (
        f"the retry is oscillating instead of converging: {asks}")
    assert asks[1] < 0.25, asks


def test_prompt_describes_what_a_light_weave_looks_like():
    """The number alone is not enough: "keep English as the majority" is
    satisfied by 29% when 22% was asked for, and the model overshot until the
    prompt said what a light weave actually is."""
    light = weave_prompt(0.22)
    heavy = weave_prompt(0.68)
    assert "light weave" in light.lower()
    assert "heavy weave" in heavy.lower()
    assert "light weave" not in heavy.lower()


def test_a_corrected_target_gets_a_lighter_instruction_than_the_original():
    """The point of correcting the ask, rather than repeating it.

    The density wording is chosen *from* the number, so an overshoot corrected
    from 22% down to 14% has to cross into a different instruction -- otherwise
    the retry builds the same prompt the model already ignored, which is why the
    loop used to oscillate instead of converging.
    """
    original = weave_prompt(0.22)
    corrected = weave_prompt(0.14)
    assert "very light" in corrected.lower()
    assert "very light" not in original.lower()
    assert "14%" in corrected and "22%" not in corrected
    assert "one sentence in four or five" in corrected


def test_a_retry_asks_for_a_corrected_number_and_says_so():
    """An attempt that overshoots by 8 points is retried asking for 8 points
    less, and the note says what it is retrying at, so the import report shows
    the correction rather than just another failure."""
    from app import levels, ingest
    from app.llm import LLMResult

    over = ("El gato está en la casa y el perro duerme en el jardín. " * 12)
    at_target = ("El gato está en la casa. The cat is in the house and the dog "
                 "is sleeping nearby in the garden tonight. ") * 6

    class Client:
        def __init__(self):
            self.prompts: list[str] = []

        def complete(self, prompt, *a, **k):
            self.prompts.append(prompt)
            return LLMResult(text=over if len(self.prompts) == 1 else at_target)

    class Tutor:
        def __init__(self):
            self.client = Client()

    tutor = Tutor()
    chunk = ("The cat is in the house and the dog is sleeping nearby in the garden. " * 6).strip()
    result = ingest.weave_chunk(tutor, chunk=chunk, focus=[], target=0.30,
                                level=levels.resolve_level("auto"))

    assert len(tutor.client.prompts) >= 2, result.notes
    assert "retrying at" in " ".join(result.notes), result.notes
    first, second = tutor.client.prompts[0], tutor.client.prompts[1]
    assert "30%" in first
    # The retry asks for something lower, not the same number again.
    assert "30%" not in [line for line in second.splitlines() if line.startswith("Amount:")][0]


def test_prompt_carries_the_levels_register():
    assert "A1" in weave_prompt(0.22, "A1")
    assert "present tense only" in weave_prompt(0.22, "A1")
    assert "subjunctive" in weave_prompt(0.5, "B2")


def test_prompt_carries_the_levels_gloss_rate():
    a1 = weave_prompt(0.22, "A1")
    c1 = weave_prompt(0.6, "C1")
    assert "almost every Spanish phrase" in a1
    assert "rarely" in c1
    assert a1 != c1


def test_prompt_lists_the_focus_vocabulary():
    assert "la cámara (the camera)" in weave_prompt(0.38)


def test_the_prompt_teaches_the_chosen_grain():
    """The dial has to reach the prompt, or the two forms produce the same text.

    This is the exact bug a reader hit: it asked for sentence-level and got
    "Research shows that el acto de poner pluma al papel pen to paper activa
    varias brain regions" -- English and Spanish interleaved word by word. The
    instruction was never in the prompt.
    """
    from app.levels import resolve_weave

    mixed = weave_prompt(0.38, "auto", "chunk")
    strict = weave_prompt(0.38, "auto", "sentence")
    assert mixed != strict
    assert resolve_weave("chunk").instruction in mixed
    assert resolve_weave("sentence").instruction in strict
    assert "never mix the two languages inside" in strict
    assert "Mixed sentences are allowed here" in mixed
    # Both keep the worked example, which does more than the rule list: an early
    # version of this prompt had nine rules and produced nothing usable.
    assert "La cámara no mató la pintura" in strict
    assert "los reemplazara" in mixed


def test_sentence_grain_tells_the_model_which_sentences_to_spend():
    """At a light target the grain is spent by *choosing*, not by trimming: the
    model has to know to take whole contentful sentences and leave the short
    ones, or it translates fragments to hit the number and breaks the rule."""
    strict = weave_prompt(0.22, "auto", "sentence")
    assert "take the longer, more contentful sentences" in strict


def test_the_prompt_protects_captions_and_proper_nouns():
    """Straight from a bad import: a photo credit came back woven.

    "Fotografía de Oksana Nazarchuk, Getty Images" is not prose, and neither are
    work titles or quoted speech. The reader's complaint was about mixing inside
    a sentence, but this line was wrong in both forms.
    """
    prompt = weave_prompt(0.38)
    assert "Captions and photo credits are not prose" in prompt
    assert "Leave proper nouns, work titles and quoted speech alone" in prompt


def test_the_grain_changes_the_cache_key():
    """The cache is keyed on what was asked for. The same passage at the same
    amount is a completely different text depending on the unit it arrives in,
    so a shared key would serve a mixed weave to a reader who asked for whole
    sentences -- silently, and forever."""
    from app import ingest

    chunk = "The camera did not kill painting. It forced it to evolve."
    auto = levels.resolve_level("auto")
    mixed = ingest.weave_cache_key(chunk, [], 0.38, auto, "chunk")
    strict = ingest.weave_cache_key(chunk, [], 0.38, auto, "sentence")
    assert mixed != strict
    # ...and it is stable, or nothing would ever be a cache hit.
    assert strict == ingest.weave_cache_key(chunk, [], 0.38, auto, "sentence")


def test_the_grain_is_written_into_the_lesson_and_read_back():
    """A lesson explains how it was made, and re-reading the file must recover
    the grain -- the file is the source of truth, and the front matter is a
    format people edit by hand, so the *name* is written and the name is
    understood on the way back in."""
    from app import diglot, ingest

    markdown = ingest.assemble_markdown(
        title="A Test Article", byline=None, url="https://example.com/a",
        preview="A preview.", items=[("p", "El genoma es un libro.")],
        vocab=[], grammar=[], focus=[], level="B1", register="reportage",
        weave="sentence", target=0.54,
    )
    assert "- **Weave:** Whole sentences only" in markdown
    assert "- **Spanish:** about 54%" in markdown
    assert "- **Level:** B1" in markdown

    article = diglot.parse_article(markdown, fallback_slug="a-test-article")
    assert article is not None
    assert article.weave == "whole sentences only"
    assert levels.resolve_weave(article.weave).code == "sentence"
    assert article.to_dict()["weave"] == "whole sentences only"


def test_a_lesson_that_never_says_how_it_was_woven_reads_as_the_default():
    """Corpus files predate the dial and the front-matter line is optional."""
    from app import diglot, levels

    markdown = "### Article Identification & Preview\n\n- **Article Title:** Old\n\n# Old\n\nLa casa es grande.\n"
    article = diglot.parse_article(markdown, fallback_slug="old")
    assert article is not None
    assert article.weave is None
    assert levels.resolve_weave(article.weave).code == levels.DEFAULT_WEAVE


# ------------------------------------------------------------------ paste --


PASTED = (
    "How Handwriting Trains the Brain\n\n"
    + "The act of putting pen to paper makes the brain work differently from typing, and "
      "the difference shows up in children learning to read. Writing by hand engages the "
      "motor system, and the motor system is what makes a letter a shape rather than a "
      "code, so the memory of the letter is stored with the movement that made it. "
      "Typing, which is the same movement for every letter, leaves the shape out. "
    * 4
)


def _paste_tutor():
    from app.llm import LLMResult

    class Client:
        @staticmethod
        def complete_json(prompt, **kwargs):
            return {"items": [{"es": "la pluma", "en": "the pen", "kind": "noun"}]}, None

        @staticmethod
        def complete(*a, **k):
            return LLMResult(text=(
                "El acto de poner la pluma sobre el papel hace que el cerebro trabaje de "
                "forma distinta a escribir a máquina (*typing*), y la diferencia se ve en "
                "los niños que aprenden a leer. Writing by hand engages the motor system, "
                "and the motor system is what makes a letter a shape rather than a code, "
                "so the memory of the letter is stored with the movement that made it. "
                "Escribir a máquina, que es el mismo movimiento para cada letra, deja la "
                "forma fuera."
            ))

    class Tutor:
        client = Client()

    return Tutor()


def test_a_pasted_article_is_a_lesson_like_any_other(monkeypatch, tmp_path):
    """When the page cannot be fetched the reader copies it out and pastes it.

    The paste stands in for the fetch, not for the lesson: the same dials, the
    same weave, the same front matter and the same shelf. A reduced second path
    through the pipeline would drift away from the first one within a release.
    """
    from app import diglot, ingest

    def no_network(*a, **k):
        raise AssertionError("a paste must not touch the network")

    monkeypatch.setattr(ingest, "fetch_url", no_network)
    monkeypatch.setattr(ingest, "extract_article", no_network)

    class Settings:
        proxy = None
        library_dir = tmp_path

    result = ingest.import_article(text=PASTED, settings=Settings(), tutor=_paste_tutor(),
                                  level_code="B1", ratio=0.38, weave="sentence")
    assert result["url"] == "", "a paste has no URL to claim"
    assert result["weave"] == "sentence"
    assert result["requested"]["target_ratio"] == pytest.approx(0.38)

    written = (tmp_path / f"{result['slug']}.md").read_text(encoding="utf-8")
    # The headline is the lesson's name, not a paragraph in it.
    assert result["title"] == "How Handwriting Trains the Brain"
    assert written.count("How Handwriting Trains the Brain") >= 1
    assert "- **Source:** pasted text" in written
    assert "- **Direct URL:**" not in written
    assert "- **Weave:** Whole sentences only" in written

    article = diglot.parse_article(written, fallback_slug=result["slug"])
    assert article is not None
    assert article.title == "How Handwriting Trains the Brain"
    assert article.weave == "whole sentences only"
    assert article.blocks, "the pasted body has to survive as text"


def test_a_paste_keeps_the_url_it_came_from(monkeypatch, tmp_path):
    """The reader usually knows where it came from. Recording it costs nothing and
    makes the lesson findable again; fetching it is exactly what did not work."""
    from app import ingest

    monkeypatch.setattr(ingest, "fetch_url",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch")))

    class Settings:
        proxy = None
        library_dir = tmp_path

    result = ingest.import_article(
        text=PASTED, source_url="https://example.com/handwriting",
        settings=Settings(), tutor=_paste_tutor(), level_code="B1", ratio=0.38,
    )
    written = (tmp_path / f"{result['slug']}.md").read_text(encoding="utf-8")
    assert "- **Direct URL:** https://example.com/handwriting" in written
    assert result["url"] == "https://example.com/handwriting"


def test_a_second_import_does_not_overwrite_the_first(monkeypatch, tmp_path):
    """The architecture notes have always claimed this and the code never did it,
    which a paste makes easy to hit: two pastes can carry the same title, and the
    slug keys off the source. The copy on disk might be the one the reader has
    corrected by hand."""
    from app import ingest

    class Settings:
        proxy = None
        library_dir = tmp_path

    first = ingest.import_article(text=PASTED, settings=Settings(), tutor=_paste_tutor(),
                                  level_code="B1", ratio=0.38)
    second = ingest.import_article(text=PASTED, settings=Settings(), tutor=_paste_tutor(),
                                   level_code="B1", ratio=0.38)
    assert first["slug"] != second["slug"], "the second import took the first one's name"
    assert (tmp_path / f"{first['slug']}.md").is_file()
    assert (tmp_path / f"{second['slug']}.md").is_file()

    # And the article's own slug matches the file it is in, or the library would
    # index it under a name that is not there.
    assert second["slug"] == Path(second["path"]).stem


def test_two_different_pastes_with_the_same_title_stay_apart(monkeypatch, tmp_path):
    """The slug's digest identifies the source. A paste has no URL, so it is the
    text itself -- otherwise every pasted article sharing a headline would be one
    lesson, and the second would overwrite the first."""
    from app import ingest

    class Settings:
        proxy = None
        library_dir = tmp_path

    other = PASTED.replace("motor system", "visual system")
    one = ingest.import_article(text=PASTED, settings=Settings(), tutor=_paste_tutor(),
                                level_code="B1", ratio=0.38)
    two = ingest.import_article(text=other, settings=Settings(), tutor=_paste_tutor(),
                               level_code="B1", ratio=0.38)
    assert one["slug"] != two["slug"]
    assert one["title"] == two["title"], "same headline, which is the point"


def test_a_paste_too_short_to_be_a_lesson_says_so(monkeypatch, tmp_path):
    from app import ingest

    class Settings:
        proxy = None
        library_dir = tmp_path

    with pytest.raises(Exception) as caught:
        ingest.import_article(text="A headline\n\nOne short line of text.",
                              settings=Settings(), tutor=_paste_tutor())
    assert "words" in str(caught.value)


def test_neither_a_link_nor_a_paste_is_refused(monkeypatch, tmp_path):
    from app import ingest

    class Settings:
        proxy = None
        library_dir = tmp_path

    with pytest.raises(ingest.ImportError_) as caught:
        ingest.import_article(settings=Settings(), tutor=_paste_tutor())
    assert "paste" in str(caught.value) or "link" in str(caught.value)


# ------------------------------------------------- the reader's own writing --


def _bilingual_tutor(*, language="Spanish", english=None, weave_text=None):
    """A real tutor over a scripted client, so the prompts under test are the real
    ones and the answers are the shapes the model actually returns."""
    from app.llm import LLMResult
    from app.tutor import Tutor as RealTutor

    class Client:
        def __init__(self):
            self.prompts: list[str] = []

        def complete_json(self, prompt, **kwargs):
            self.prompts.append(prompt)
            if "language_name" in prompt:
                return {"language": "en" if language == "English" else "xx",
                        "language_name": language, "english": english or ""}, None
            if "post-reading anchors" in prompt:
                return {"vocab": [], "grammar": []}, None
            return {"items": [{"es": "la casa", "en": "the house", "kind": "noun"}]}, None

        def complete(self, prompt, *a, **k):
            self.prompts.append(prompt)
            return LLMResult(
                text=weave_text or "La casa es grande. The house is big and the room is bright.")

    return RealTutor(Client())


class _WritingSettings:
    proxy = None

    def __init__(self, tmp_path):
        self.library_dir = tmp_path


def test_a_piece_written_in_english_is_woven_from_the_readers_own_words():
    """The translation step runs on every piece, so the case that has to be right
    is the one where it should do nothing. A model asked to put English "into
    English" paraphrases it, and this is the reader's own writing: their words are
    the point, so the model is trusted for its judgement only."""
    from app import ingest

    mine = "I walked to the river and watched the water for an hour. " * 6
    tutor = _bilingual_tutor(language="English", english="A smoothed over retelling, not their words.")
    english, language, notes = ingest.english_text(tutor, text=mine)
    assert english == mine, "their text must come back untouched"
    assert language is None, "nothing was translated, so there is no language to report"
    assert notes == []


def test_a_piece_written_in_spanish_is_put_into_english_first():
    """The diglot format is English prose with Spanish woven in, so a Spanish
    passage has to be rendered before it can be woven at all."""
    from app import ingest

    english, language, notes = ingest.english_text(
        _bilingual_tutor(language="Spanish", english="The house is big."),
        text="La casa es grande.",
    )
    assert english == "The house is big."
    assert language == "Spanish"


def test_the_translation_is_cached_so_reweaving_does_not_pay_twice():
    """Re-weaving a piece at a different amount must not buy the same translation
    again -- the dials are meant to be tried."""
    from app import ingest

    class Cache:
        def __init__(self):
            self.store: dict = {}

        def get(self, key):
            return self.store.get(key)

        def put(self, key, value):
            self.store[key] = value

    cache = Cache()
    tutor = _bilingual_tutor(language="French", english="The house is big.")
    ingest.english_text(tutor, text="La maison est grande.", cache=cache)
    asked = len(tutor.client.prompts)
    again, language, _ = ingest.english_text(tutor, text="La maison est grande.", cache=cache)
    assert again == "The house is big." and language == "French"
    assert len(tutor.client.prompts) == asked, "the second call should be a cache hit"


def test_a_merged_translation_is_resplit_to_the_writers_paragraphs():
    """A model handed five paragraphs can return one enormous block, which the
    reader then gets as a wall of text. The content is all there, so the breaks are
    recovered from the source rather than the translation being thrown away."""
    from app import ingest

    source = "\n\n".join(
        f"Paragraph {n} of my own writing, about the river and the walk. " * 3 for n in range(3)
    )
    merged = " ".join(source.split())
    english, language, notes = ingest.english_text(
        _bilingual_tutor(language="Spanish", english=merged), text=source)
    assert english.count("\n\n") == 2, english
    assert language == "Spanish"
    assert any("paragraph" in note for note in notes), notes


def test_a_translation_that_fails_entirely_says_so_rather_than_writing_an_empty_lesson():
    from app import ingest

    with pytest.raises(ingest.ImportError_) as caught:
        ingest.english_text(_bilingual_tutor(language="Chinese", english=""), text="你好。" * 40)
    assert "English" in str(caught.value)


def test_a_lesson_made_from_writing_records_where_it_came_from(monkeypatch, tmp_path):
    """It is a lesson like any other -- the same dials, the same anchors, the same
    reader -- and the front matter says the source was the reader's own writing,
    and in what language, so the file explains itself a year later."""
    from app import ingest

    def no_network(*a, **k):
        raise AssertionError("a piece you wrote must not touch the network")

    monkeypatch.setattr(ingest, "fetch_url", no_network)
    monkeypatch.setattr(ingest, "extract_article", no_network)

    mine = "Escribí sobre el río y el paseo largo de la tarde. " * 8
    result = ingest.import_writing(
        text=mine, settings=_WritingSettings(tmp_path),
        tutor=_bilingual_tutor(language="Spanish",
                               english=("I wrote about the river and the long walk in the afternoon. " * 8)),
        title="El río", level_code="B1", ratio=0.38, weave="sentence",
    )
    assert result["source_language"] == "Spanish"
    assert result["title"] == "El río"
    written = (tmp_path / f"{result['slug']}.md").read_text(encoding="utf-8")
    assert "- **Source:** your own writing, written in Spanish" in written
    assert "- **Direct URL:**" not in written
    assert "- **Weave:** Whole sentences only" in written


def test_a_rewritten_piece_is_a_different_lesson(tmp_path):
    """The original passage identifies the lesson, so a revision must not take the
    slug of the version before it -- and asking twice for the same text must not
    write over the first file either."""
    from app import ingest

    first = "Escribí sobre el río. " * 20
    second = "Escribí sobre el río y el mar. " * 20
    english = "I wrote about the river and the long walk in the afternoon. " * 12

    def run(text):
        return ingest.import_writing(
            text=text, settings=_WritingSettings(tmp_path),
            tutor=_bilingual_tutor(language="Spanish", english=english),
            title="El río", level_code="B1", ratio=0.38,
        )

    one = run(first)
    two = run(second)
    three = run(first)
    assert one["slug"] != two["slug"], "a revision is not the piece it revised"
    assert one["slug"] != three["slug"], "and a repeat does not overwrite the file"
    assert (tmp_path / f"{one['slug']}.md").is_file()


def test_import_survives_failing_post_reading_notes(monkeypatch, tmp_path):
    """The weave is the expensive part; losing the anchors at the last step
    must not throw the article away. A DNS blip during an import is what
    prompted this -- it discarded eight minutes of finished work."""
    from app import ingest

    class FakeClient:
        def complete(self, *a, **k):
            raise AssertionError("weave_chunk should not be reached")

        def complete_json(self, prompt, **kwargs):
            raise RuntimeError("getaddrinfo failed")

    class FakeTutor:
        client = FakeClient()

    seen: dict[str, Any] = {}

    def fake_weave(tutor, *, chunk, focus, target=ingest.TARGET_RATIO, level=None, weave=None):
        # Roughly a third Spanish, so the weave passes the ratio gate and the
        # test exercises the anchors path rather than failing earlier.
        seen["weave"] = weave
        return ingest.WovenChunk(paragraphs=[
            "El genoma es un libro de la vida. The genome is a book of life, "
            "written in a language that we are still learning to read, and "
            "**dar lugar a** (*to give rise to*) new questions every year. "
            "Los científicos siguen estudiando el texto, line by line."
        ])

    monkeypatch.setattr(ingest, "weave_chunk", fake_weave)
    monkeypatch.setattr(ingest, "choose_focus_vocabulary",
                        lambda *a, **k: [{"es": "el genoma", "en": "the genome"}])
    monkeypatch.setattr(ingest, "fetch_url", lambda *a, **k: ("<html></html>", "https://example.com/a"))
    monkeypatch.setattr(ingest, "extract_article", lambda html: {
        "title": "A Test Article", "byline": None,
        "blocks": [{"kind": "p", "text": "The genome is a book of life. " * 12}],
        "words": 200, "dropped_citations": 0,
    })

    class Settings:
        proxy = None
        library_dir = tmp_path

    result = ingest.import_article(url="https://example.com/a", settings=Settings(),
                                   tutor=FakeTutor(), judge=None, weave="sentence")
    assert result["anchors"] == {"vocab": 0, "grammar": 0}
    # The grain reaches the weaver and comes back in the report: a dial that is
    # accepted and then dropped on the floor is worse than no dial.
    assert seen["weave"].code == "sentence"
    assert result["weave"] == "sentence" and result["weave_name"]
    assert any("post-reading notes" in note for note in result["notes"])
    assert (tmp_path / f"{result['slug']}.md").is_file()


# ------------------------------------------------------------------- lookups --


def test_lookups_are_recorded(store):
    store.log_lookup(term="se vuelve", lemma="volverse", gloss="becomes")
    store.log_lookup(term="se vuelve", lemma="volverse", gloss="becomes")
    assert store.lookup_count("volverse") == 2


def test_frequent_lookups_exclude_saved_words(store):
    for _ in range(3):
        store.log_lookup(term="genoma", lemma="genoma", gloss="genome")
    assert [w["lemma"] for w in store.frequent_lookups(minimum=2)] == ["genoma"]
    store.add_word(term="genoma", lemma="genoma", gloss="genome")
    assert store.frequent_lookups(minimum=2) == []


def test_frequent_lookups_respects_the_threshold(store):
    store.log_lookup(term="genoma", lemma="genoma")
    assert store.frequent_lookups(minimum=2) == []
    assert len(store.frequent_lookups(minimum=1)) == 1


def test_frequent_lookups_can_skip_function_words(store):
    """Without this the study list is topped by "del" and "a" -- genuinely the
    most-clicked tokens, and precisely the ones nobody wants a card for."""
    for _ in range(5):
        store.log_lookup(term="del", lemma="del")
    for _ in range(3):
        store.log_lookup(term="genoma", lemma="genoma")

    everything = store.frequent_lookups(minimum=2)
    assert [row["lemma"] for row in everything][0] == "del", "it really is looked up most"

    filtered = store.frequent_lookups(minimum=2, skip={"del", "a", "la"})
    assert [row["lemma"] for row in filtered] == ["genoma"]


def test_probe_words_extracts_content_words():
    probes = probe_words([{"term": "el genoma", "gloss": "the genome"}])
    assert probes == {"el genoma": ["genome"]}


def test_probe_words_skips_stopwords_and_short_words():
    probes = probe_words([{"term": "a la vez", "gloss": "at the same moment"}])
    # "moment" is the only content word long enough to be usable evidence:
    # "same" and "time" are too common to prove two texts share a topic.
    assert probes == {"a la vez": ["moment"]}


def test_probe_words_ignores_glossless_words():
    assert probe_words([{"term": "x", "gloss": None}]) == {}


def test_score_text_counts_matching_words():
    probes = {"el genoma": ["genome"], "la célula": ["cell"]}
    score, hits = score_text("The genome contains many cells.", probes)
    assert score == pytest.approx(1.0)
    assert set(hits) == {"el genoma", "la célula"}


def test_score_text_uses_stems():
    probes = {"el genoma": ["genome"]}
    score, _ = score_text("Genomic studies of genomes.", probes)
    assert score == pytest.approx(1.0)


def test_score_text_is_a_fraction_of_the_whole_deck():
    probes = {"el genoma": ["genome"], "la célula": ["cell"], "el arte": ["painting"]}
    score, hits = score_text("A text about the genome only.", probes)
    assert score == pytest.approx(1 / 3)
    assert hits == ["el genoma"]


def test_score_text_with_no_probes_is_zero():
    assert score_text("anything", {}) == (0.0, [])
