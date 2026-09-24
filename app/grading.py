"""Grading with fallbacks, so the app still teaches when Jev is not there.

Every exercise in this app was graded by Jev, and when Jev is not configured --
no `TYPESAFE_API_KEY`, or the service is down -- the methods returned an error
verdict. What the learner then saw was worse than an error: `_is_correct` read
the empty checks, got `False`, and told them they were **wrong**. A missing key
made the app lie.

So grading now tries three tiers in order and says which one answered:

1.  **Jev.** A judgment model: a calibrated probability per dimension, which is
    what makes "grammatical but means the opposite" expressible. Best answer,
    and the only tier that reads meaning rather than similarity.
2.  **The tutor.** The same judgment asked of the chat model as JSON. Slower,
    less calibrated, and it is a generation model being asked to judge -- but it
    understands meaning, which the third tier does not.
3.  **A local comparison.** No network at all: folded token overlap against the
    article's own Spanish. It can confirm an answer that matches well and it
    cannot condemn one that does not, because a correct translation may share
    almost no words with the reference. So it is deliberately asymmetric --
    high overlap counts as correct, low overlap counts as *unverified*, never as
    wrong -- and the UI labels it as a rough check.

The tier is carried on the verdict and shown. A grade from a token comparison
should not look like a grade from a calibrated judge.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .judge import Judge, Verdict
from .vocab import fold, stem
from .diglot import _WORD_RE

log = logging.getLogger("diglot.grading")

# Folded content words, for the local comparison. Function words carry no
# meaning to compare and are most of any Spanish sentence.
_IGNORE = frozenset(
    """de la el los las un una unos unas que y e o u en por para con sin sobre
    entre al del a se le les lo su sus mi tu es son era eran fue fueron ser
    estar está están ni no sí si te os nos me más mas menos muy ya""".split()
)

# How much of the reference a learner's answer has to share before a local
# comparison will call it correct. Set high on purpose: the fallback should be
# confident or silent, never confidently wrong.
MATCH_THRESHOLD = 0.6
PARTIAL_THRESHOLD = 0.3

# Where a score stops being doubt and starts being a verdict. Deliberately
# *above* the midpoint: 0.5 is what "no idea" looks like, both to Jev (a noul at
# 0.5 is a coin flip) and to the local tier, which uses 0.5 to mean "unverified".
# Reading doubt as correctness is the same mistake as reading doubt as failure,
# just in the direction that flatters the learner.
CORRECT_AT = MATCH_THRESHOLD


def content_words(text: str) -> set[str]:
    """Stems of the content words in a passage."""
    out: set[str] = set()
    for word in _WORD_RE.findall(text or ""):
        folded = fold(word)
        if len(folded) < 3 or folded in _IGNORE:
            continue
        out.add(stem(word))
    return out


def overlap(left: str, right: str) -> float:
    """How much of the reference's content the attempt shares.

    Normalised by the reference, not the union: an answer that says the same
    thing more briefly is fine, an answer that says half of it is not.
    """
    wanted = content_words(right)
    if not wanted:
        return 0.0
    return len(content_words(left) & wanted) / len(wanted)


class Grading:
    """The three tiers, tried in order. Every method returns a Verdict."""

    def __init__(self, judge: Judge, tutor: Any = None) -> None:
        self.judge = judge
        self.tutor = tutor

    # -- what will answer ------------------------------------------------ #

    @property
    def tiers(self) -> list[str]:
        available = []
        if self.judge.available:
            available.append("jev")
        if self.tutor is not None:
            available.append("tutor")
        available.append("local")
        return available

    @property
    def primary(self) -> str:
        return self.tiers[0]

    def describe(self) -> dict[str, Any]:
        # "Which tier grades" and "can there be exercises at all" are different
        # questions, and only the tutor generates them. Reporting a grading tier
        # on a machine with no model would promise something unreachable.
        generates = self.tutor is not None
        if self.judge.available:
            note = "Jev grades meaning with a calibrated confidence."
        elif generates:
            note = "No judgment model configured, so the tutor grades instead."
        else:
            note = ("No model configured, so exercises cannot be generated. Add one and "
                    "answers are checked by comparison with the article's own Spanish — "
                    "unusual ones are reported as unverified rather than wrong.")
        return {
            "tiers": self.tiers,
            "primary": self.primary,
            "calibrated": self.judge.available,
            "generates": generates,
            "note": note,
        }

    # -- translation ------------------------------------------------------ #

    def grade_translation(self, *, source: str, reference: str, attempt: str) -> Verdict:
        verdict = self.judge.grade_translation(source=source, reference=reference, attempt=attempt)
        if verdict.ok:
            verdict.method = "jev"
            return verdict
        log.info("jev unavailable for translation (%s); falling back", verdict.error)

        verdict = self._tutor_translation(source=source, reference=reference, attempt=attempt)
        if verdict is not None:
            return verdict
        return self._local_translation(reference=reference, attempt=attempt)

    def _tutor_translation(self, *, source: str, reference: str, attempt: str) -> Verdict | None:
        if self.tutor is None:
            return None
        prompt = f"""Judge a language learner's Spanish translation.

English source: {source}
The article's own Spanish: {reference}
Learner's translation: {attempt}

The reference is one correct rendering, not the only one -- different wording
can be fully correct.

Return JSON:
{{"meaning": 0.0-1.0, "grammar": 0.0-1.0, "natural": 0.0-1.0,
  "quality": 0-4, "why": "one short English sentence"}}
Numbers are your confidence that the dimension holds."""
        try:
            data, _ = self.tutor.client.complete_json(prompt, max_tokens=400, timeout=45)
        except Exception as exc:
            log.warning("tutor grading failed: %s", exc)
            return None
        if not isinstance(data, dict):
            return None
        return Verdict(
            score=_number(data.get("quality")),
            checks={
                "meaning": _number(data.get("meaning")) or 0.0,
                "grammar": _number(data.get("grammar")) or 0.0,
                "natural": _number(data.get("natural")) or 0.0,
            },
            method="tutor",
            model="tutor",
        )

    def _local_translation(self, *, reference: str, attempt: str) -> Verdict:
        """The floor: how much of the reference's content the answer shares.

        Asymmetric on purpose. High overlap is good evidence the answer is
        right; low overlap is no evidence at all, because a correct translation
        may use entirely different words. So it reports a *partial* score rather
        than a failure, and the UI presents it as unverified.

        A blank answer is the one case the fallback *can* call: there is no
        wording it could have been instead, so a mismatch is not a difference.
        """
        if not content_words(attempt):
            return Verdict(
                score=0.0,
                checks={"meaning": 0.0, "grammar": 0.0, "natural": 0.0},
                labels={"note": "nothing was written"},
                method="local",
                model="word comparison",
                may_fail=False,
            )
        share = overlap(attempt, reference)
        if share >= MATCH_THRESHOLD:
            meaning, note = share, "shares most of the article's wording"
        elif share >= PARTIAL_THRESHOLD:
            meaning, note = share * 0.7, "shares some of the article's wording"
        else:
            # Deliberately above the correctness threshold: an unusual but
            # correct answer must not be marked wrong by a word comparison.
            meaning, note = 0.5, "too different from the article's wording to check"
        return Verdict(
            score=round(meaning * 4, 2),
            checks={"meaning": meaning, "grammar": meaning, "natural": meaning},
            labels={"note": note},
            method="local",
            model="word comparison",
            may_fail=False,
        )

    # -- cloze ------------------------------------------------------------ #

    def grade_cloze(self, *, sentence: str, blank: str, target: str, attempt: str) -> Verdict:
        verdict = self.judge.grade_cloze(sentence=sentence, blank=blank, target=target, attempt=attempt)
        if verdict.ok:
            verdict.method = "jev"
            return verdict

        # A cloze *does* have a right answer, so a comparison is legitimate here
        # -- it simply cannot accept a synonym, which is what Jev adds.
        same = stem(attempt) == stem(target) if target else False
        return Verdict(
            checks={"fits": 1.0 if same else 0.0, "same_word": 1.0 if same else 0.0},
            labels={"note": "compared with the article's word"},
            method="local",
            model="word comparison",
        )

    # -- comprehension ---------------------------------------------------- #

    def grade_answer(self, *, question: str, reference: str, attempt: str) -> Verdict:
        verdict = self.judge.grade_answer(question=question, reference=reference, attempt=attempt)
        if verdict.ok:
            verdict.method = "jev"
            return verdict

        if self.tutor is not None:
            prompt = f"""Grade a learner's answer to a reading-comprehension question.

Question: {question}
Expected answer: {reference}
Learner's answer: {attempt}

Return JSON: {{"correct": 0.0-1.0, "understood": 0.0-1.0, "why": "one short sentence"}}
Confidence that the answer is correct and that the learner understood, even if worded differently."""
            try:
                data, _ = self.tutor.client.complete_json(prompt, max_tokens=400, timeout=45)
                if isinstance(data, dict):
                    return Verdict(
                        checks={"correct": _number(data.get("correct")) or 0.0,
                                "understood": _number(data.get("understood")) or 0.0},
                        method="tutor", model="tutor",
                    )
            except Exception as exc:
                log.warning("tutor comprehension grading failed: %s", exc)

        share = overlap(attempt, reference)
        # Unlike translation, this *may* condemn. A comprehension question asks
        # about a fact that was stated, so a correct answer normally reuses the
        # statement's content words; an answer sharing none of them is far more
        # likely wrong than cleverly paraphrased. The asymmetry in translation
        # exists because there the reference is a rendering, not a statement.
        return Verdict(
            checks={"correct": share, "understood": share},
            labels={"note": "compared with the model answer"},
            method="local", model="word comparison",
        )

    # -- free writing ------------------------------------------------------ #

    def grade_writing(self, *, text: str, prompt: str, focus: list[str] | None = None) -> Verdict:
        """Judge a piece of the learner's own writing.

        Two tiers, not three, and the missing one is the point. Reviewing a
        paragraph requires reading it: a word-overlap comparison can say nothing
        about whether Spanish holds together, and offering it as a fallback would
        be inventing a judgment. When neither model is configured this returns an
        error verdict that says so, and the caller keeps the measurements that
        *were* made -- length, language mix, which prompted words appeared -- all
        of which are exact and none of which need a model.
        """
        verdict = self.judge.grade_writing(text=text, prompt=prompt, focus=focus)
        if verdict.ok:
            verdict.method = "jev"
            return verdict
        log.info("jev unavailable for writing (%s); trying the tutor", verdict.error)

        if self.tutor is not None:
            graded = self._tutor_writing(text=text, prompt=prompt, focus=focus)
            if graded is not None:
                return graded
        return Verdict(
            error="reviewing writing needs a model — set LLM_* to have this piece "
                  "read. What can be measured without one is shown above.",
            method="local",
            model="",
            may_fail=False,
        )

    def _tutor_writing(self, *, text: str, prompt: str, focus: list[str] | None) -> Verdict | None:
        asked = f"""Judge a language learner's own piece of Spanish writing.

The prompt they were given: {prompt or "(none)"}
Words they were asked to use: {", ".join(focus or []) or "(none)"}
What they wrote:
{text}

There is no source text: this is their own Spanish, not a translation. Ignore
trivial slips like a missing accent unless the meaning changes.

Return JSON:
{{"well_formed": 0.0-1.0, "range": 0.0-1.0, "did_the_task": 0.0-1.0,
  "quality": 0-4, "why": "one short English sentence"}}
Confidence that the Spanish is grammatical, that it is varied rather than one
pattern repeated, and that it did what the prompt asked."""
        try:
            data, _ = self.tutor.client.complete_json(asked, max_tokens=500, timeout=60)
        except Exception as exc:
            log.warning("tutor writing judgment failed: %s", exc)
            return None
        if not isinstance(data, dict):
            return None
        return Verdict(
            score=_number(data.get("quality")),
            checks={
                "well_formed": _number(data.get("well_formed")) or 0.0,
                "range": _number(data.get("range")) or 0.0,
                "did_the_task": _number(data.get("did_the_task")) or 0.0,
            },
            method="tutor",
            model="tutor",
        )

    # -- placement -------------------------------------------------------- #

    def place(self, *, title: str, sample: str, spanish_ratio: float) -> Verdict:
        verdict = self.judge.rate_difficulty(title=title, sample=sample, spanish_ratio=spanish_ratio)
        if verdict.ok:
            verdict.method = "jev"
        return verdict


def _number(value: Any) -> float | None:
    try:
        return max(0.0, min(1.0, float(value))) if value is not None else None
    except (TypeError, ValueError):
        return None


def is_correct(kind: str, verdict: dict[str, Any] | None) -> bool:
    """A single boolean the UI can colour by, derived from the typed verdict.

    Lives here rather than in the server so the rule about what counts as
    correct sits beside the grading that produces it -- and so that a verdict
    with no checks is never silently read as failure.
    """
    if not verdict or verdict.get("error"):
        return False
    checks = verdict.get("checks") or {}
    if kind == "translate":
        return bool(checks.get("meaning", 0) >= CORRECT_AT and checks.get("grammar", 0) >= CORRECT_AT)
    if kind == "cloze":
        return bool(checks.get("fits", 0) >= CORRECT_AT)
    if kind == "writing":
        # Not a pass/fail question: a piece can be worth keeping while still
        # having things to fix, and there is no single right answer to be right
        # about. "Well formed enough to read" is the bar.
        return bool(checks.get("well_formed", 0) >= CORRECT_AT)
    return bool(checks.get("correct", 0) >= CORRECT_AT)


def is_provisional(verdict: dict[str, Any] | None) -> bool:
    """Whether this grade came from a tier that cannot really read meaning.

    Provisional grades are shown as checked-not-judged. They are not failures:
    see :func:`may_fail` for the separate question of whether one can fail.
    """
    return bool(verdict) and verdict.get("method") == "local"


def may_fail(verdict: dict[str, Any] | None) -> bool:
    """Whether this grade has the standing to call the answer wrong.

    Distinct from :func:`is_provisional`, and the distinction is the whole point:
    a word comparison may fail a cloze, where a single word is right, but never a
    translation, where a correct answer may share no words with the reference. An
    error has no standing either -- a request that never arrived is not a verdict.
    """
    if not verdict or verdict.get("error"):
        return False
    return bool(verdict.get("may_fail", True))
