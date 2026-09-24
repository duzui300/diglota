"""Writing practice, and the part of it that needs no model at all.

Producing Spanish is the skill the app was least able to help with. It grades
translations and drills vocabulary, but nothing asked the reader to *write*, and
writing is where the gaps show up: a word you recognise on the page but cannot
reach for is not yours yet.

Two halves, and they are deliberately separate.

**The prompt comes from the reader's own material.** The app knows what they keep
looking up and never save, what they have already read, and what they chose to
keep. A writing prompt assembled from those is worth more than a generic
"describe your weekend", and it is the only kind of prompt that can also be
marked: the app knows which words went in, so it can say which ones came out.

**The offline half is real feedback.** Before any model is involved, the app can
say how long the piece is, whether it is actually Spanish, and how many of the
prompted words the writer reached for. That is measured with the same segmenter
the reader uses, and for a learner it is genuinely useful -- "60% of this is
English" is the single most common thing wrong with a first attempt at free
writing.

**The model half is the corrections.** A reader of meaning is required to tell
someone their grammar is wrong, and there is no honest local substitute -- so
when no model is configured, feedback says so and the measurements still stand.
Pretending a word-overlap score could review a paragraph would be the same
mistake this app already made once with grading.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from .diglot import parse_spans
from .vocab import fold, stem

# Enough to be a piece of writing, short enough to finish in one sitting. The
# prompt asks for a range and the counters say where the piece sits, because a
# target nobody states is a target nobody hits.
MIN_WORDS = 40
COMFORTABLE_WORDS = 120

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_SENTENCE = re.compile(r"[.!?…]+")


@dataclass
class Prompt:
    """What to write about, and which words to reach for."""

    topic: str
    words: list[str]
    instruction: str
    source: str = ""          # where the topic came from, for the UI to credit

    def to_dict(self) -> dict[str, Any]:
        return {"topic": self.topic, "words": self.words,
                "instruction": self.instruction, "source": self.source}


@dataclass
class Reading:
    """What can be said about a piece of writing without asking a model."""

    words: int = 0
    sentences: int = 0
    distinct: int = 0
    spanish_words: int = 0
    english_words: int = 0
    used: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)

    @property
    def spanish_share(self) -> float:
        total = self.spanish_words + self.english_words
        return round(self.spanish_words / total, 3) if total else 0.0

    @property
    def enough(self) -> bool:
        """Whether the piece reached the length the prompt asked for."""
        return self.words >= MIN_WORDS

    def to_dict(self) -> dict[str, Any]:
        return {"words": self.words, "sentences": self.sentences, "distinct": self.distinct,
                "spanish_words": self.spanish_words, "english_words": self.english_words,
                "spanish_share": self.spanish_share, "used": self.used, "missed": self.missed,
                "enough": self.enough}


# --------------------------------------------------------------------------- #
# The prompt
# --------------------------------------------------------------------------- #


def fingerprint(text: str) -> str:
    """A stable hash of a piece, so its review can be cached against the text."""
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()[:16]


def title_for(text: str, *, limit: int = 6) -> str:
    """A name for a piece, taken from how it opens.

    Asking someone to title their practice writing is friction they do not need,
    and a list of pieces with no names is unusable. The opening words are what
    the piece is about often enough to be worth using, and they are stable: the
    title is fixed when the piece is created, so revising the text does not
    rename the thing you were working on.
    """
    body = " ".join((text or "").split())
    if not body:
        return "Untitled"
    first = re.split(r"(?<=[.!?…])\s", body)[0]
    words = first.split()
    if len(words) <= limit:
        return first.rstrip(".") or "Untitled"
    return " ".join(words[:limit]).rstrip(",;:") + "…"


# --------------------------------------------------------------------------- #
# Improving a draft
# --------------------------------------------------------------------------- #

# What "improve this" can mean. Four kinds of help, deliberately distinct: a
# learner asking for a revision should be able to say *which* thing they want
# changed, because "make it better" hands the decision to a model that does not
# know what they are working on.
#
# None of them replaces the writing. Each produces a suggestion shown beside it
# -- a better piece than the one they wrote is not feedback, it is a different
# piece, and the comparison is the lesson.
MODES: tuple[dict[str, str], ...] = (
    {
        "id": "grammar",
        "label": "Fix the grammar only",
        "blurb": "Nothing else changes. Not the wording, not the order, not the length.",
        "instruction": "Correct only what is grammatically wrong -- agreement, tense, "
                       "articles, prepositions, accents that change the meaning. Keep the "
                       "learner's wording and sentence order wherever the Spanish allows it. "
                       "Change as little as possible.",
    },
    {
        "id": "natural",
        "label": "Make it sound native",
        "blurb": "The same meaning, phrased the way a Spanish speaker would.",
        "instruction": "Rewrite it so a native speaker would write it naturally, keeping the "
                       "meaning exactly. Replace English-shaped phrasing with Spanish-shaped "
                       "phrasing, and use connectors and structures a Spanish speaker would "
                       "reach for.",
    },
    {
        "id": "expressive",
        "label": "Make it richer",
        "blurb": "More vivid, using the words you are learning.",
        "instruction": "Make it richer and more vivid without changing what it says: stronger "
                       "verbs, more precise nouns, a metaphor where one fits. Use the "
                       "vocabulary listed below wherever it belongs naturally -- but do not "
                       "force a word in.",
    },
    {
        "id": "minimal",
        "label": "Keep my words",
        "blurb": "The smallest change that makes it correct.",
        "instruction": "Keep the learner's exact words wherever they can possibly stand. Change "
                       "only what must change to make the Spanish correct, and prefer a "
                       "smaller change to a larger one every time. If a phrase is already "
                       "correct, leave it completely alone.",
    },
)

MODE_IDS: tuple[str, ...] = tuple(mode["id"] for mode in MODES)
BY_MODE: dict[str, dict[str, str]] = {mode["id"]: mode for mode in MODES}

# How many annotations to accept. A wall of highlights stops being feedback: past
# about a dozen, a learner reads none of them.
MAX_NOTES = 12
NOTE_KINDS = ("good", "fix", "style")


def mode_options() -> list[dict[str, str]]:
    return [dict(mode) for mode in MODES]


def keep_notes(text: str, notes: Any) -> list[dict[str, str]]:
    """Filter a model's annotations down to ones that can be shown.

    Two things are enforced, both because a broken annotation is worse than a
    missing one. A fragment that is not in the text cannot be highlighted, and a
    highlight in the wrong place teaches the wrong thing -- so the fragment has
    to appear. And the list is capped, because the useful annotations are always
    fewer than the available ones.
    """
    if not isinstance(notes, list):
        return []
    out: list[dict[str, str]] = []
    for item in notes:
        if not isinstance(item, dict):
            continue
        fragment = str(item.get("fragment") or "").strip()
        if not fragment or fragment not in text:
            continue
        kind = str(item.get("kind") or "fix").strip().lower()
        if kind not in NOTE_KINDS:
            kind = "fix"
        message = str(item.get("why") or item.get("message") or "").strip()
        suggestion = str(item.get("suggestion") or "").strip()
        if not message and not suggestion:
            continue
        out.append({"fragment": fragment, "kind": kind, "why": message,
                    "issue": str(item.get("issue") or "").strip(), "suggestion": suggestion})
        if len(out) >= MAX_NOTES:
            break
    return out


def prompt_for(
    *,
    topics: list[str] | None = None,
    shaky: list[str] | None = None,
    saved: list[str] | None = None,
    quotes: list[str] | None = None,
) -> Prompt:
    """Assemble something worth writing about out of what the reader already has.

    The sources are combined in priority order rather than one falling back to
    the next: words the reader keeps looking up come first, then words they saved
    recently, then the words from a sentence they chose to keep. A prompt that
    mixes two words they struggle with and one they know is easier to actually
    write than three they cannot reach for, and easier is what gets a piece
    finished.

    ``topics`` is the same title-derived subject the discovery engine uses, so a
    reader who has been reading about language models is asked to write about
    language models rather than about something unrelated.
    """
    words = _pick_words([*(shaky or []), *(saved or []), *(quotes or [])])
    topic = (topics or [""])[0].strip()
    source = ""
    if shaky:
        source = "the words you keep looking up"
    elif saved:
        source = "words you saved recently"
    elif quotes:
        source = "a sentence you kept"

    if topic and words:
        instruction = (f"Write about {topic}, using these words: "
                       f"{', '.join(words)}. {MIN_WORDS}–{COMFORTABLE_WORDS} words.")
    elif topic:
        instruction = (f"Write about {topic}. {MIN_WORDS}–{COMFORTABLE_WORDS} words. "
                       "Do not use a dictionary — reach for what you have.")
    elif words:
        instruction = (f"Write a short piece using these words: {', '.join(words)}. "
                       f"{MIN_WORDS}–{COMFORTABLE_WORDS} words.")
    else:
        instruction = (f"Write about anything you have read this week. "
                       f"{MIN_WORDS}–{COMFORTABLE_WORDS} words.")
    return Prompt(topic=topic, words=words, instruction=instruction, source=source)


def _pick_words(candidates: list[str], *, limit: int = 3) -> list[str]:
    """Distinct, non-overlapping words, in the order they were offered.

    Overlapping is the thing to avoid: asking for both ``volverse`` and ``se
    vuelve`` is asking for one word twice, and a reader who notices will trust
    the prompt less.
    """
    out: list[str] = []
    stems: set[str] = set()
    for candidate in candidates:
        term = str(candidate or "").strip()
        if not term or len(term) < 3:
            continue
        key = stem(term)
        if key in stems:
            continue
        stems.add(key)
        out.append(term)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# Reading it back
# --------------------------------------------------------------------------- #


def read(text: str, *, prompt: Prompt | None = None) -> Reading:
    """Measure a piece of writing with no model and no guessing.

    The language split uses the reader's own segmenter, so "how much of this is
    actually Spanish" is answered the same way the app answers it for an article
    -- and being told that half your paragraph is English is the most useful
    thing a first free-writing attempt can learn.
    """
    body = (text or "").strip()
    result = Reading()
    if not body:
        return result

    spanish: list[str] = []
    english: list[str] = []
    for span in parse_spans(body.replace("\n", " ")):
        bucket = spanish if span.lang == "es" else english
        bucket.extend(_WORD.findall(span.text))

    result.spanish_words = len(spanish)
    result.english_words = len(english)
    result.words = len(spanish) + len(english)
    result.distinct = len({fold(word) for word in spanish + english})
    result.sentences = len([s for s in _SENTENCE.split(body) if s.strip()])

    if prompt and prompt.words:
        written = {stem(word) for word in spanish + english}
        result.used = [term for term in prompt.words if stem(term) in written]
        result.missed = [term for term in prompt.words if stem(term) not in written]
    return result


def usage_note(reading: Reading) -> str:
    """One sentence about the piece, chosen by what is most worth saying.

    Ordered by what a learner should hear first. Being asked to write Spanish and
    writing English is the failure worth naming; a short piece is worth a nudge;
    using every prompted word is worth saying out loud, because it is the whole
    point of the prompt.
    """
    if reading.words == 0:
        return "Nothing written yet."
    if reading.spanish_share < 0.5:
        return (f"{round((1 - reading.spanish_share) * 100)}% of this is English. "
                f"That is a normal first attempt — try again and push the Spanish share up.")
    if not reading.enough:
        return (f"{reading.words} words. Worth going further: the interesting problems "
                f"appear after the first easy sentences.")
    if reading.missed and not reading.used:
        return f"None of the prompted words made it in. Try weaving one of them in."
    if reading.used and not reading.missed:
        return f"Every prompted word, used. That is the exercise done."
    if reading.used:
        return f"Good: {', '.join(reading.used)} used. Missing: {', '.join(reading.missed)}."
    return f"{reading.words} words, {round(reading.spanish_share * 100)}% Spanish."
