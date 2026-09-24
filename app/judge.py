"""Typed judgments from TypeSafe's System One (Jev).

This is the part of the app that could not be built with a chat model alone.
"Did this translation preserve the meaning?" and "does this word fit the blank?"
are *decisions*, not generations, and what the learner needs back is a verdict
with a confidence attached -- not a paragraph of prose that may or may not
contain one. Jev answers exactly that: a probability over yes/no, or an
expected score on a rubric, in a few hundred milliseconds.

Three judgments are defined here:

*   :meth:`Judge.grade_translation` -- free production, the hardest skill and
    the one a multiple-choice quiz cannot test. Scored on a five-point rubric
    plus separate yes/no calls for meaning, grammar and naturalness, because
    "mostly right but the gender is wrong" is a different lesson from "you said
    the opposite".
*   :meth:`Judge.grade_cloze` -- filling a blank from the article. Judged
    semantically, so a good synonym is accepted rather than marked wrong
    against a string.
*   :meth:`Judge.rate_difficulty` -- how hard an article is, used to place
    imported texts in the library.

Every method returns ``None`` rather than raising when the model is
unavailable, so the app degrades to its non-AI fallback instead of failing.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("diglot.judge")

# The rubric behind the translation score. Ordered from zero, as Jev's Score
# primitive expects: each entry describes what that score means.
TRANSLATION_RUBRIC = [
    "The Spanish does not convey the source meaning, or is not intelligible Spanish.",
    "Only fragments of the meaning survive; a reader would misunderstand the sentence.",
    "The meaning is roughly there but the sentence is broken or hard to read.",
    "The meaning is preserved; there are errors a native speaker would notice.",
    "Accurate and idiomatic; a native speaker would accept it unchanged.",
]

# The rubric for free writing, which is judged on different things: a paragraph
# has no source to be faithful to, so correctness, variety and whether it did
# what was asked are the questions. Deliberately not the translation rubric --
# "the meaning is preserved" is not a thing to say about someone's own piece.
WRITING_RUBRIC = [
    "Not yet Spanish: mostly English, or not intelligible.",
    "Understandable in places, but the Spanish breaks down often enough to lose a reader.",
    "Communicates; a sympathetic reader would follow it, noticing errors throughout.",
    "Clear and correct enough to read comfortably, with a few things a native would change.",
    "Confident and varied; a native speaker would read it without noticing the seams.",
]


@dataclass
class Verdict:
    """One judged answer, in the shape the UI renders.

    A request can carry several typed questions over the same state -- the
    placement call asks for a level *and* a register -- so the raw answer to
    each is kept in ``by_question`` under its name. The flat ``score`` and
    ``checks`` fields are conveniences for the single-score calls
    (translation, cloze), and hold the first scored question.
    """

    score: float | None = None
    score_max: int = 4
    probabilities: dict[str, float] = field(default_factory=dict)
    checks: dict[str, float] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    by_question: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Which tier produced this: 'jev', 'tutor' or 'local'. Set by
    # app.grading, which falls back down the list when Jev is unavailable.
    method: str = ""
    model: str = ""
    latency_ms: int = 0
    error: str | None = None
    # Whether this grade has the standing to tell the learner they were wrong.
    # False when the answer has many correct wordings and the grade came from a
    # comparison rather than a reader -- a word comparison can confirm a
    # translation but can never acquit one, so it must not convict either.
    # A cloze *does* have one right word, so the same comparison may fail it.
    may_fail: bool = True

    @property
    def ok(self) -> bool:
        return self.error is None

    def score_of(self, question: str) -> float | None:
        return (self.by_question.get(question) or {}).get("score")

    def probs_of(self, question: str) -> dict[str, float]:
        return (self.by_question.get(question) or {}).get("probabilities") or {}

    def labels_of(self, question: str) -> dict[str, str]:
        return (self.by_question.get(question) or {}).get("labels") or {}

    def choice_of(self, question: str) -> str | None:
        return (self.by_question.get(question) or {}).get("choice")

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "score_max": self.score_max,
            "probabilities": self.probabilities,
            "checks": self.checks,
            "labels": self.labels,
            "by_question": self.by_question,
            "method": self.method,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "may_fail": self.may_fail,
        }


class Judge:
    """Wraps the TypeSafe client. Safe to construct with no key configured."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY", "")
        self.model = model or os.environ.get("TYPESAFE_MODEL", "jev-latest")
        self._client: Any = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise RuntimeError("TYPESAFE_API_KEY is not set")
        try:
            from typesafe_sdk import TypeSafeClient
        except ImportError as exc:  # pragma: no cover - install-time problem
            raise RuntimeError("typesafe-sdk is not installed; run: pip install typesafe-sdk") from exc
        self._client = TypeSafeClient(model=self.model)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- translation ------------------------------------------------------ #

    def grade_translation(self, *, source: str, reference: str, attempt: str) -> Verdict:
        """Judge a learner's Spanish translation of an English sentence.

        ``reference`` is the article's own Spanish for that sentence. It is
        given as context, not as an answer key: the learner is allowed to
        produce something different that means the same thing, which is the
        entire reason a semantic judge is needed here.
        """
        state = {
            "task": "Grade a language learner's Spanish translation.",
            "english_source": source,
            "spanish_reference_translation_from_the_article": reference,
            "learner_translation": attempt,
            "note": (
                "The reference is one correct rendering, not the only one. The learner's "
                "sentence may be worded differently and still be fully correct. Judge "
                "whether it means the same thing and whether it is well-formed Spanish."
            ),
        }
        questions = {
            "meaning": (
                "noul",
                "Does the learner's translation convey the same meaning as the English source?",
                {"true": "the meaning is preserved", "false": "the meaning is changed, lost or invented"},
            ),
            "grammar": (
                "noul",
                "Is the learner's Spanish grammatically well-formed?",
                {"true": "no grammatical errors", "false": "contains grammatical errors"},
            ),
            "natural": (
                "noul",
                "Would a native Spanish speaker write it this way?",
                {"true": "sounds natural", "false": "understandable but not how a native would say it"},
            ),
            "quality_score": ("score", "overall quality of this translation", TRANSLATION_RUBRIC),
        }
        return self._ask(state, questions)

    # -- cloze ------------------------------------------------------------ #

    def grade_cloze(self, *, sentence: str, blank: str, target: str, attempt: str) -> Verdict:
        """Judge a word the learner supplied for a blank in a sentence."""
        state = {
            "task": "A learner filled a blank in a Spanish sentence.",
            "sentence_with_blank": sentence,
            "blank_represents": blank,
            "word_from_the_original_article": target,
            "learner_answer": attempt,
            "note": (
                "The original word is the one the article used, but another word that fits "
                "the blank grammatically and in meaning should be accepted."
            ),
        }
        questions = {
            "fits": (
                "noul",
                "Does the learner's word fit the blank, in both grammar and meaning?",
                {"true": "it fits", "false": "it does not fit"},
            ),
            "same_word": (
                "noul",
                "Is the learner's word the same word the original article used?",
                {"true": "the same word, or an inflection of it", "false": "a different word"},
            ),
        }
        return self._ask(state, questions)

    # -- comprehension ---------------------------------------------------- #

    def grade_answer(self, *, question: str, reference: str, attempt: str) -> Verdict:
        """Judge a free-text answer to a comprehension question, in Spanish."""
        state = {
            "task": "Grade a learner's answer to a reading-comprehension question.",
            "question": question,
            "expected_answer": reference,
            "learner_answer": attempt,
        }
        questions = {
            "correct": (
                "noul",
                "Is the learner's answer correct, given the expected answer?",
                {"true": "the answer is correct", "false": "the answer is wrong or misses the point"},
            ),
            "understood": (
                "noul",
                "Even if imperfectly worded, did the learner show they understood the text?",
                {"true": "shows understanding", "false": "does not show understanding"},
            ),
        }
        return self._ask(state, questions)

    def grade_writing(self, *, text: str, prompt: str, focus: list[str] | None = None) -> Verdict:
        """Judge a piece of free writing the learner produced themselves.

        No reference and no source: this is the one judgment in the app that is
        about someone's *own* Spanish rather than their fidelity to a text. So it
        asks different questions -- whether the Spanish holds up, whether it is
        varied, and whether it did what the prompt asked -- and scores it on a
        rubric written for a paragraph rather than for a translation.

        The prompted words are given as context, not as a checklist to mark
        against: the count of which ones appeared is already measured exactly
        (see :mod:`app.writing`), and asking a model to count would make a
        counted thing approximate.
        """
        state = {
            "task": "Review a language learner's own piece of Spanish writing.",
            "prompt_they_were_given": prompt or "(they were not given a prompt)",
            "what_they_wrote": text,
            "words_they_were_asked_to_use": ", ".join(focus or []) or "(none)",
            "note": (
                "Judge the Spanish as written. Do not penalise a piece for failing to "
                "translate something -- there is no source text. Ignore trivia like a "
                "missing accent unless it changes the meaning. Judge whether this reads "
                "as Spanish someone could follow, and whether it is doing anything beyond "
                "one repeated pattern."
            ),
        }
        questions = {
            "well_formed": (
                "noul",
                "Is the Spanish grammatical, allowing for errors a learner would plausibly make?",
                {"true": "no errors that break the sentence", "false": "errors that break sentences"},
            ),
            "range": (
                "noul",
                "Does it use varied vocabulary and sentence structure, rather than one pattern repeated?",
                {"true": "varied", "false": "repetitive or minimal"},
            ),
            "did_the_task": (
                "noul",
                "Did it do what the prompt asked -- the topic, and the words if any were named?",
                {"true": "did what was asked", "false": "ignored the prompt"},
            ),
            "quality_score": ("score", "overall quality of this piece of writing", WRITING_RUBRIC),
        }
        return self._ask(state, questions)

    # -- placement -------------------------------------------------------- #

    def rate_difficulty(self, *, title: str, sample: str, spanish_ratio: float) -> Verdict:
        """Place a text: how hard is it, and what kind of writing is it.

        Two typed questions over one state, which is the point of asking a
        judgment model rather than a chat model -- the same text is scored for
        level and classified for register in a single round trip, and the
        register costs nothing extra.
        """
        state = {
            "task": "Place a Spanish/English diglot text for a learner.",
            "title": title,
            "english_share_of_words": f"{1 - spanish_ratio:.0%}",
            "sample": sample[:1500],
            "note": (
                "The reader is an English speaker learning Spanish. In a diglot text the "
                "English carries the meaning and the Spanish is embedded in it, so what "
                "makes it hard is the density and complexity of the Spanish, not the topic."
            ),
        }
        questions: dict[str, Any] = {
            "level": (
                "score",
                "What CEFR level is the Spanish in this text?",
                [
                    "A1 - beginner: present tense, basic vocabulary",
                    "A2 - elementary: common past tenses, everyday vocabulary",
                    "B1 - intermediate: most tenses, connected argument",
                    "B2 - upper intermediate: subjunctive, abstract topics",
                    "C1 - advanced: idiomatic, literary or technical Spanish",
                ],
            ),
            # A Choice, not a Score: news and essay are different things rather
            # than points on a scale, and scoring them would imply an order.
            "register": (
                "choice",
                "What kind of writing is this?",
                {
                    "news": "reported events, attributed quotes, datelines",
                    "essay": "one voice arguing a position, addressing the reader",
                    "conversation": "dialogue, interviews, spoken turns",
                    "academic": "research or textbook, citations, definitions, impersonal",
                    "fiction": "narrative, characters, description, dialogue in a story",
                },
            ),
        }
        return self._ask(state, questions)

    # -- plumbing --------------------------------------------------------- #

    def _ask(self, state: dict[str, Any], questions: dict[str, tuple[str, str, Any]]) -> Verdict:
        """Ask several typed questions over one piece of state.

        Each entry is ``(kind, instructions, spec)`` where kind is one of
        ``noul`` (a probability of yes), ``choice`` (an unordered set of
        labels), or ``score`` (an *ordered* rubric whose expected value is
        meaningful). The distinction matters: register is a choice, because
        news and essay are different things rather than points on a scale, and
        asking for it as a score would imply an order that does not exist.
        """
        if not self.available:
            return Verdict(error="no TYPESAFE_API_KEY configured")
        try:
            from typesafe_sdk import Choice, Noul, Score
            import time

            client = self._get_client()
            built = {}
            kinds: dict[str, str] = {}
            for name, (kind, instructions, spec) in questions.items():
                if kind == "score":
                    built[name] = Score(instructions=instructions, criteria=spec)
                elif kind == "choice":
                    built[name] = Choice(instructions=instructions, criteria=spec)
                else:
                    built[name] = Noul(instructions=instructions, criteria=spec)
                kinds[name] = kind

            started = time.perf_counter()
            response = client.system_one(state=state, questions=built)
            latency_ms = int((time.perf_counter() - started) * 1000)

            verdict = Verdict(model=response.model, latency_ms=latency_ms)
            for name, kind in kinds.items():
                answer = response.answers.get(name)
                if answer is None:
                    continue
                if kind == "noul":
                    # `noul` is the probability of "yes".
                    verdict.checks[name] = float(answer.noul)
                    verdict.by_question[name] = {"probability": float(answer.noul)}
                elif kind == "choice":
                    verdict.by_question[name] = {
                        "choice": str(answer.choice),
                        "probabilities": {str(k): float(v) for k, v in (answer.probabilities or {}).items()},
                    }
                else:
                    entry = {
                        "score": float(answer.score),
                        "score_max": len(questions[name][2]) - 1,
                        "probabilities": {str(k): float(v) for k, v in (answer.probabilities or {}).items()},
                        "labels": {str(k): str(v) for k, v in (answer.legend or {}).items()},
                    }
                    verdict.by_question[name] = entry
                    if verdict.score is None:      # the first scored question is the primary
                        verdict.score = entry["score"]
                        verdict.score_max = entry["score_max"]
                        verdict.probabilities = entry["probabilities"]
                        verdict.labels = entry["labels"]
            return verdict
        except Exception as exc:
            log.warning("judge call failed: %s: %s", type(exc).__name__, exc)
            return Verdict(error=f"{type(exc).__name__}: {exc}")
