"""Tests for the import difficulty settings.

Three dials that must stay independent: the *level* decides what kind of Spanish
a lesson may contain, the *amount* decides how much of it there is, and the
*weave* decides what unit it arrives in. Most of these tests are about that
independence holding, because it is the thing that would quietly break if one
were folded into another.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import levels  # noqa: E402


# ------------------------------------------------------------------ lookup --


def test_every_level_is_known():
    for level in levels.LEVELS:
        assert levels.resolve_level(level.code) is level


def test_lookup_is_case_insensitive():
    assert levels.resolve_level("b1").code == "B1"
    assert levels.resolve_level("B2").code == "B2"


def test_unknown_levels_fall_back_to_auto():
    for value in (None, "", "Z9", "advanced", "  "):
        assert levels.resolve_level(value).code == "auto"


def test_auto_is_offered_as_a_choice():
    codes = [level["code"] for level in levels.options()["levels"]]
    assert "auto" in codes
    assert codes[-1] == "auto", "auto goes last, as the fallback"


# ------------------------------------------------------------------ amount --


def test_amount_defaults_to_the_levels_suggestion():
    for code in ("A1", "A2", "B1", "B2", "C1"):
        level = levels.resolve_level(code)
        assert levels.clamp_ratio(None, level) == pytest.approx(level.suggested_ratio)


def test_choices_are_ordered_by_difficulty():
    ratios = [level.suggested_ratio for level in levels.LEVELS]
    assert ratios == sorted(ratios)
    assert ratios[0] < ratios[-1]


def test_an_explicit_amount_overrides_the_level():
    """Someone reading B2 but wanting a light weave must get a light weave."""
    level = levels.resolve_level("B2")
    assert levels.clamp_ratio(0.22, level) == pytest.approx(0.22)
    assert levels.clamp_ratio(0.22, level) != pytest.approx(level.suggested_ratio)


def test_amounts_are_clamped_to_a_usable_range():
    level = levels.resolve_level("B1")
    assert levels.clamp_ratio(0.01, level) == pytest.approx(levels.MIN_RATIO)
    assert levels.clamp_ratio(0.99, level) == pytest.approx(levels.MAX_RATIO)
    assert levels.clamp_ratio(-3, level) == pytest.approx(levels.MIN_RATIO)


def test_a_percentage_is_accepted_as_well_as_a_fraction():
    """A slider sends 40; an API client might send 0.40. Both mean the same."""
    level = levels.resolve_level("auto")
    assert levels.clamp_ratio(40, level) == pytest.approx(0.40)
    assert levels.clamp_ratio(0.40, level) == pytest.approx(0.40)


def test_nonsense_amounts_fall_back_to_the_suggestion():
    level = levels.resolve_level("B1")
    assert levels.clamp_ratio("half", level) == pytest.approx(level.suggested_ratio)


def test_tolerance_scales_with_the_target():
    """±0.14 is most of a light weave and a fraction of an immersion one, so a
    fixed tolerance would be too slack in one case and too strict in the other."""
    light = levels.tolerance_for(0.22)
    heavy = levels.tolerance_for(0.68)
    assert light < heavy
    assert levels.tolerance_for(0.05) >= 0.06, "never so tight it cannot be hit"
    assert levels.tolerance_for(0.95) <= 0.14, "never so slack it means nothing"


# ------------------------------------------------------------- description --


def test_description_names_the_amount_and_the_feel():
    level = levels.resolve_level("A1")
    text = levels.describe(level, level.suggested_ratio)
    assert "22%" in text
    assert "Beginner" in text
    assert len(text) < 220, "it is a caption, not a paragraph"


def test_description_for_auto_does_not_pretend_to_be_a_level():
    text = levels.describe(levels.resolve_level(None), 0.38)
    assert "auto" not in text.lower() or "app will" in text
    assert "The app will judge" in text


def test_the_description_names_what_is_being_woven():
    """The same three controls serve an imported article and a piece the reader
    wrote themselves, and a dialog about someone's own writing calling it "the
    article" is the kind of small wrongness that makes an app feel careless."""
    level = levels.resolve_level("auto")
    assert "from the article itself" in levels.describe(level, 0.38)
    assert "from the piece itself" in levels.describe(level, 0.38, what="piece")


def test_description_changes_with_the_amount():
    level = levels.resolve_level("B1")
    assert levels.describe(level, 0.20) != levels.describe(level, 0.68)


# -------------------------------------------------------- the two are free --


def test_every_level_can_be_paired_with_every_amount():
    """The dials are meant to be independent: a light weave of advanced Spanish
    is a legitimate thing to ask for."""
    seen = set()
    for level in levels.LEVELS:
        for ratio in (levels.MIN_RATIO, 0.38, levels.MAX_RATIO):
            resolved = levels.clamp_ratio(ratio, level)
            assert levels.MIN_RATIO <= resolved <= levels.MAX_RATIO
            seen.add((level.code, round(resolved, 2)))
    assert len(seen) == len(levels.LEVELS) * 3, "some combination was collapsed"


def test_options_payload_is_serialisable():
    import json

    payload = levels.options()
    assert json.loads(json.dumps(payload)) == payload
    for level in payload["levels"]:
        assert {"code", "name", "grammar", "vocabulary", "gloss_rate", "suggested_ratio"} <= set(level)
    assert payload["min_ratio"] < payload["max_ratio"]
    assert len(payload["amounts"]) >= 3
    # The dialog renders the grain from this payload, so it has to be complete
    # enough to choose between the forms without any other data.
    assert [w["code"] for w in payload["weaves"]] == list(levels.BY_WEAVE)
    for weave in payload["weaves"]:
        assert {"code", "name", "blurb", "example"} <= set(weave)


# -------------------------------------------------------------------- grain --


def test_there_are_two_ways_for_the_languages_to_mix():
    assert len(levels.WEAVES) == 2
    assert set(levels.BY_WEAVE) == {"chunk", "sentence"}
    for weave in levels.WEAVES:
        assert len(weave.name) > 5, weave
        assert len(weave.blurb) > 40, weave
        assert len(weave.instruction) > 80, weave
        assert "in:" in weave.example and "out:" in weave.example, weave


def test_only_one_form_forbids_mixing_inside_a_sentence():
    """The permission runs one way: the strict form forbids something the mixed
    form allows, and the mixed form forbids nothing the strict form does. A
    reader choosing between them is choosing whether the two languages may share
    a sentence -- not choosing a difficulty."""
    mixed = levels.resolve_weave("chunk")
    strict = levels.resolve_weave("sentence")
    assert mixed.mixes_within_a_sentence is True
    assert strict.mixes_within_a_sentence is False
    assert "never mix the two languages inside" in strict.instruction
    assert "Mixed sentences are allowed here" in mixed.instruction


def test_the_mixed_form_permits_sentences_and_paragraphs_too():
    """It is the form that lets the weaver choose the unit, so it must not read
    as a rule that every sentence has to be half English. An earlier version said
    "swap a single short phrase and leave the rest alone", which forbade
    translating a whole sentence -- a perfectly good way to spend 22% of a
    paragraph, and the reader does not care which unit the Spanish arrived in."""
    mixed = levels.resolve_weave("chunk")
    assert "translate the whole sentence" in mixed.instruction
    assert "whole sentence" in mixed.example.lower()
    assert "leave the rest alone" not in mixed.instruction


def test_the_two_examples_are_different_and_show_their_own_form():
    """Shown side by side in the import dialog, so identical examples would
    teach nothing -- and the mixed one has to actually demonstrate a mixed
    sentence, which is the whole difference."""
    mixed, strict = levels.resolve_weave("chunk"), levels.resolve_weave("sentence")
    assert mixed.example != strict.example
    assert "but it forced them to evolve" in mixed.example, "a sentence half Spanish"
    assert "stay entirely English" in strict.example


def test_the_grain_is_chosen_not_guessed():
    assert levels.resolve_weave("sentence").code == "sentence"
    assert levels.resolve_weave("chunk").code == "chunk"
    assert levels.resolve_weave("  sentence  ").code == "sentence"
    # ...and the name is accepted, because the name is what goes in the file
    assert levels.resolve_weave("Whole sentences only").code == "sentence"
    assert levels.resolve_weave("Mixed, as it reads best").code == "chunk"
    # Anything unrecognised falls back to the default rather than raising: the
    # front matter is a format people edit by hand, and a typo in one line of it
    # should not make a lesson unreadable.
    assert levels.resolve_weave(None).code == levels.DEFAULT_WEAVE
    assert levels.resolve_weave("").code == levels.DEFAULT_WEAVE
    assert levels.resolve_weave("junk").code == levels.DEFAULT_WEAVE
    # A fragment that fits both names is genuinely ambiguous and is not guessed
    # at -- picking one silently is how "whole sentences" turns into a mixed
    # weave, which is the failure this whole dial exists to prevent.
    assert levels.resolve_weave("e").code == levels.DEFAULT_WEAVE


def test_a_weave_object_passes_through_the_resolver():
    """Every function threads "a code or a weave" through to the next one, so
    this has to accept what it just returned. The version that only understood
    strings returned the *default* for a weave -- so asking for whole sentences
    quietly produced mixed ones, and the prompts were identical."""
    strict = levels.resolve_weave("sentence")
    assert levels.resolve_weave(strict) is strict
    assert levels.tolerance_for(0.30, strict) != levels.tolerance_for(0.30, levels.resolve_weave("chunk"))


def test_sentence_grain_is_allowed_to_miss_by_more():
    """Because the amount is quantised by sentences: a five-sentence paragraph
    can be 0%, 20% or 40% Spanish and nothing in between, so a tolerance tight
    enough to be meaningful at phrase level is unattainable here, and the retry
    loop would spin."""
    for target in (0.22, 0.38, 0.60):
        loose = levels.tolerance_for(target, "sentence")
        tight = levels.tolerance_for(target, "chunk")
        assert loose > tight, target
        assert 0.10 <= loose <= 0.20, (target, loose)


def test_the_description_says_what_the_grain_feels_like():
    level = levels.resolve_level("auto")
    mixed = levels.describe(level, 0.22, "chunk")
    strict = levels.describe(level, 0.22, "sentence")
    assert "bursts" in mixed, mixed
    assert "a Spanish sentence every few lines" in strict, strict
    assert mixed != strict
    # At a high amount the sentence form is described as Spanish throughout,
    # not as a scattering of sentences.
    assert "nearly every sentence is Spanish" in levels.describe(level, 0.70, "sentence")


def test_the_grain_does_not_change_the_amount():
    """Three dials, independent. Choosing whole sentences must not quietly move
    the number the reader set."""
    level = levels.resolve_level("B1")
    for weave in levels.BY_WEAVE:
        assert levels.clamp_ratio(0.45, level) == 0.45
        assert levels.clamp_ratio(None, level) == level.suggested_ratio
