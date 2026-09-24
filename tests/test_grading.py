"""Tests for the grading fallbacks.

The bug these exist to prevent: with no Jev key configured, the app used to tell
learners they were **wrong**. An empty checks dict was read as `False`, so a
missing API key turned every answer into a failure -- the worst possible failure
mode for a learning tool, because a learner believes it.

So the tests are mostly about the floor: the local tier must recognise a good
answer, must never condemn an answer it cannot read, and every tier must say
which one it was.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import grading  # noqa: E402
from app.grading import Grading, is_correct, is_provisional, may_fail, overlap  # noqa: E402
from app.judge import Verdict  # noqa: E402


# --------------------------------------------------------------- doubles --


class FakeJudge:
    """A Judge that either answers or refuses, without a network."""

    def __init__(self, *, available=True, verdict=None):
        self.available = available
        self._verdict = verdict
        self.calls = 0

    def _reply(self, fallback_error="no key"):
        self.calls += 1
        if self._verdict is not None:
            return self._verdict
        return Verdict(error=fallback_error)

    def grade_translation(self, **kwargs):
        return self._reply()

    def grade_cloze(self, **kwargs):
        return self._reply()

    def grade_answer(self, **kwargs):
        return self._reply()

    def rate_difficulty(self, **kwargs):
        return self._reply()


class FakeClient:
    def __init__(self, data):
        self.data = data
        self.calls = 0

    def complete_json(self, prompt, **kwargs):
        self.calls += 1
        return self.data, {}


class FakeTutor:
    def __init__(self, data=None, *, raises=False):
        self.client = FakeClient(data)
        self.raises = raises


class ExplodingTutor(FakeTutor):
    def __init__(self):
        super().__init__(None)

    def __getattr__(self, name):
        raise AssertionError("the tutor must not be called")


# ------------------------------------------------------------ which tier --


def test_all_three_tiers_when_both_models_are_present():
    assert Grading(FakeJudge(available=True), FakeTutor()).tiers == ["jev", "tutor", "local"]


def test_the_tutor_is_skipped_when_jev_is_configured_but_fails():
    """Order, not availability: a configured Jev that errors should fall through
    to the tutor, not skip to the word comparison."""
    judge = FakeJudge(available=True, verdict=Verdict(error="upstream 503"))
    assert Grading(judge, FakeTutor()).tiers == ["jev", "tutor", "local"]


def test_no_jev_key_drops_the_first_tier():
    assert Grading(FakeJudge(available=False), FakeTutor()).tiers == ["tutor", "local"]


def test_no_tutor_either_leaves_the_local_tier():
    assert Grading(FakeJudge(available=False), None).tiers == ["local"]


def test_the_local_tier_always_exists():
    """It has no dependency to be missing, which is the point: there is always
    something to answer with."""
    for tutor in (None, FakeTutor()):
        assert Grading(FakeJudge(available=False), tutor).tiers[-1] == "local"
        assert "local" in Grading(FakeJudge(available=True), tutor).tiers


def test_describe_says_which_tier_will_answer():
    both = Grading(FakeJudge(available=True), FakeTutor()).describe()
    assert both["primary"] == "jev" and both["calibrated"] is True
    assert both["generates"] is True

    tutor = Grading(FakeJudge(available=False), FakeTutor()).describe()
    assert tutor["primary"] == "tutor" and tutor["calibrated"] is False
    assert "tutor" in tutor["note"]

    local = Grading(FakeJudge(available=False), None).describe()
    assert local["primary"] == "local" and local["calibrated"] is False
    assert local["generates"] is False, "no tutor means no exercises to grade"
    assert "unverified" in local["note"]
    assert "cannot be generated" in local["note"], "and it must not promise a grade"


# ------------------------------------------------------------- the floor --


def test_an_exact_answer_is_correct_with_no_model_at_all():
    """The regression test for the bug that motivated this module."""
    grading_ = Grading(FakeJudge(available=False), None)
    verdict = grading_.grade_translation(
        source="The house was old.",
        reference="La casa era vieja.",
        attempt="La casa era vieja.",
    ).to_dict()
    assert is_correct("translate", verdict) is True
    assert is_provisional(verdict) is True


def test_a_correct_answer_worded_differently_is_never_marked_wrong():
    """A correct translation may share almost no words with the article's own.
    A comparison that called this wrong would be actively harmful, so it reports
    it as unverified instead."""
    grading_ = Grading(FakeJudge(available=False), None)
    verdict = grading_.grade_translation(
        source="The house was old.",
        reference="La casa era vieja.",
        attempt="El edificio tenía muchos años.",
    ).to_dict()
    assert is_correct("translate", verdict) is False, "not claimable as correct"
    assert verdict["checks"]["meaning"] == 0.5, "and not claimed as wrong either"
    assert verdict["labels"]["note"].startswith("too different")
    assert is_provisional(verdict) is True


def test_a_partly_matching_answer_scores_between_the_two():
    grading_ = Grading(FakeJudge(available=False), None)
    # "casa" and "vieja" survive out of four content words.
    verdict = grading_.grade_translation(
        source="The house was old.",
        reference="La casa era vieja.",
        attempt="La casa es nueva.",
    ).to_dict()
    share = verdict["checks"]["meaning"]
    assert 0.3 <= share < 0.6


def test_content_words_ignore_function_words():
    """`de`, `la`, `que` are most of a Spanish sentence and carry nothing to
    compare, so they must not inflate the share. They are stemmed, so the
    comparison is on `cas`/`abuel`, not on the surface form."""
    assert grading.content_words("la casa de la abuela") == grading.content_words("casa abuela")
    assert grading.content_words("la casa") == {grading.stem("casa")}


def test_a_score_of_one_half_is_doubt_not_a_pass():
    """0.5 is what "no idea" looks like -- Jev's coin flip and the local tier's
    unverified marker. Reading it as correct would flatter the learner the same
    way the original bug punished them."""
    doubt = {"checks": {"meaning": 0.5, "grammar": 0.5}}
    assert is_correct("translate", doubt) is False
    assert is_correct("cloze", {"checks": {"fits": 0.5}}) is False
    assert is_correct("comprehension", {"checks": {"correct": 0.5}}) is False

    sure = {"checks": {"meaning": 0.8, "grammar": 0.8}}
    assert is_correct("translate", sure) is True


def test_overlap_is_normalised_by_the_reference():
    """Saying the same thing more briefly is fine; saying half of it is not.
    Normalising by the union would reward padding and punish concision."""
    assert overlap("la casa era vieja", "la casa era vieja") == 1.0
    assert overlap("la casa era vieja y grande", "la casa era vieja") == 1.0
    assert overlap("la casa", "la casa era vieja") < 1.0


def test_overlap_with_an_empty_reference_is_zero_not_a_crash():
    assert overlap("anything", "") == 0.0
    assert overlap("", "") == 0.0


def test_an_empty_answer_is_not_correct():
    """A blank is the one thing a word comparison can call, because there is no
    wording it could have been instead."""
    grading_ = Grading(FakeJudge(available=False), None)
    verdict = grading_.grade_translation(
        source="x", reference="La casa era vieja.", attempt="   "
    ).to_dict()
    assert is_correct("translate", verdict) is False
    assert verdict["checks"]["meaning"] == 0.0
    assert verdict["labels"]["note"] == "nothing was written"


# ------------------------------------------------------------------ cloze --


def test_a_cloze_does_have_a_right_answer_so_the_comparison_may_condemn():
    """The asymmetry in translation is because meaning can be worded many ways.
    A cloze is not: one word goes in the blank, so a mismatch is real evidence."""
    grading_ = Grading(FakeJudge(available=False), None)
    verdict = grading_.grade_cloze(
        sentence="La ___ era vieja.", blank="casa", target="casa", attempt="casa"
    ).to_dict()
    assert is_correct("cloze", verdict) is True

    wrong = grading_.grade_cloze(
        sentence="La ___ era vieja.", blank="casa", target="casa", attempt="perro"
    ).to_dict()
    assert is_correct("cloze", wrong) is False


def test_a_cloze_matches_inflected_forms():
    """`casas` for `casa` is the same word; a string comparison would reject it."""
    grading_ = Grading(FakeJudge(available=False), None)
    verdict = grading_.grade_cloze(
        sentence="Las ___ eran viejas.", blank="casas", target="casa", attempt="casas"
    ).to_dict()
    assert verdict["checks"]["fits"] == 1.0


# ----------------------------------------------------------- comprehension --


def test_comprehension_falls_to_the_local_tier_without_a_tutor():
    grading_ = Grading(FakeJudge(available=False), None)
    verdict = grading_.grade_answer(
        question="¿Dónde vivía?", reference="En una casa vieja.", attempt="En una casa vieja."
    ).to_dict()
    assert verdict["method"] == "local"
    assert is_correct("comprehension", verdict) is True


def test_the_tutor_grades_comprehension_when_there_is_no_jev():
    judge = FakeJudge(available=False)
    tutor = FakeTutor({"correct": 0.9, "understood": 0.85, "why": "yes"})
    verdict = Grading(judge, tutor).grade_answer(
        question="q", reference="r", attempt="a"
    ).to_dict()
    assert verdict["method"] == "tutor"
    assert verdict["checks"]["understood"] == 0.85


def test_a_tutor_that_raises_falls_through_to_the_word_comparison():
    """A model that is down must not take the whole exercise with it."""
    grading_ = Grading(FakeJudge(available=False), ExplodingTutor())
    verdict = grading_.grade_translation(
        source="The house was old.", reference="La casa era vieja.", attempt="La casa era vieja."
    ).to_dict()
    assert verdict["method"] == "local"


def test_a_tutor_returning_nonsense_falls_through():
    grading_ = Grading(FakeJudge(available=False), FakeTutor("not a dict"))
    verdict = grading_.grade_translation(
        source="The house was old.", reference="La casa era vieja.", attempt="La casa era vieja."
    ).to_dict()
    assert verdict["method"] == "local"


def test_tutor_numbers_are_clamped():
    """A model that answers on a 0-10 scale must not produce a 700% meter."""
    grading_ = Grading(FakeJudge(available=False), FakeTutor({"meaning": 7, "grammar": -1}))
    verdict = grading_.grade_translation(source="a", reference="b", attempt="c").to_dict()
    assert verdict["checks"]["meaning"] == 1.0
    assert verdict["checks"]["grammar"] == 0.0


# ------------------------------------------------------------------- jev --


def test_jev_is_used_when_it_answers():
    judge = FakeJudge(available=True, verdict=Verdict(
        score=3.5, checks={"meaning": 0.9, "grammar": 0.8, "natural": 0.7}, model="jev-1"))
    verdict = Grading(judge, FakeTutor()).grade_translation(
        source="a", reference="b", attempt="c"
    ).to_dict()
    assert verdict["method"] == "jev"
    assert verdict["score"] == 3.5
    assert is_provisional(verdict) is False


def test_jev_is_not_asked_twice():
    """A fallback that re-asks an unavailable model wastes the learner's time
    on every single answer."""
    judge = FakeJudge(available=False)
    grading_ = Grading(judge, None)
    for _ in range(3):
        grading_.grade_translation(source="a", reference="b", attempt="c")
    assert judge.calls == 3, "one attempt per answer, not a retry loop"


# ------------------------------------------------------------- verdicts ----


def test_a_missing_verdict_is_never_read_as_correct():
    assert is_correct("translate", None) is False
    assert is_correct("translate", {}) is False
    assert is_provisional(None) is False


def test_an_error_verdict_is_not_correct_and_not_provisional():
    """An error is not a grade. It should be shown as an error rather than
    coloured red as a wrong answer."""
    errored = {"error": "upstream 503", "checks": {}}
    assert is_correct("translate", errored) is False
    assert is_provisional(errored) is False


def test_placement_returns_the_judges_verdict_unwrapped():
    judge = FakeJudge(available=True, verdict=Verdict(score=2.5, model="jev"))
    verdict = Grading(judge, FakeTutor()).place(title="T", sample="s", spanish_ratio=0.3)
    assert verdict.method == "jev"


def test_placement_without_jev_keeps_its_error():
    """Placement has no meaningful floor: word comparison cannot estimate
    difficulty. It must report that it could not, not invent a level."""
    verdict = Grading(FakeJudge(available=False), None).place(title="T", sample="s", spanish_ratio=0.3)
    assert verdict.error
    assert is_provisional(verdict.to_dict()) is False


# ------------------------------------------------------------- standing ---


def test_a_comparison_may_fail_a_cloze_but_not_a_translation():
    """The distinction the three-state record turns on. A cloze has one right
    word, so a mismatch is evidence. A translation has many correct wordings, so
    a mismatch is not -- and a grade that cannot acquit must not convict."""
    grading_ = Grading(FakeJudge(available=False), None)

    cloze = grading_.grade_cloze(
        sentence="La ___ era vieja.", blank="casa", target="casa", attempt="perro"
    ).to_dict()
    assert is_provisional(cloze) is True, "still a comparison, not a judgment"
    assert may_fail(cloze) is True, "but it can see that the word is wrong"

    translation = grading_.grade_translation(
        source="The house was old.", reference="La casa era vieja.", attempt="El edificio era antiguo."
    ).to_dict()
    assert is_provisional(translation) is True
    assert may_fail(translation) is False


def test_a_confirmed_translation_keeps_its_standing_to_pass():
    """Not failing is not the same as not confirming: an exact match is evidence
    and is recorded as correct, not as unverified."""
    grading_ = Grading(FakeJudge(available=False), None)
    verdict = grading_.grade_translation(
        source="The house was old.", reference="La casa era vieja.", attempt="La casa era vieja."
    ).to_dict()
    assert is_correct("translate", verdict) is True
    assert may_fail(verdict) is False, "no standing to fail, but it passed anyway"


def test_an_error_never_has_standing_to_fail():
    """A request that never arrived is not a verdict. Recording it as a wrong
    answer would be the original bug wearing a different hat."""
    assert may_fail({"error": "upstream 503", "checks": {}}) is False
    assert may_fail({"error": "upstream 503", "may_fail": True}) is False
    assert may_fail(None) is False


def test_a_jev_verdict_may_fail():
    judge = FakeJudge(available=True, verdict=Verdict(
        checks={"meaning": 0.1, "grammar": 0.2, "natural": 0.1}, model="jev"))
    verdict = Grading(judge, FakeTutor()).grade_translation(
        source="a", reference="b", attempt="c"
    ).to_dict()
    assert may_fail(verdict) is True
