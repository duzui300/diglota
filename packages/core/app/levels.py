"""Learner-facing difficulty settings for an imported lesson.

Three dials decide what a woven article is actually like to read. The first two
were previously fixed in code or guessed after the fact:

*   **How much Spanish** -- the share of words that end up in the target
    language. This is the single biggest lever on how hard a diglot is. At 20%
    the Spanish sits in short phrases inside comfortable English; at 70% the
    English is the scaffolding and Spanish carries whole paragraphs.
*   **Level** -- what *kind* of Spanish it is. A 40% weave of ``y``, ``pero``
    and the present tense is a very different text from a 40% weave of the
    subjunctive and relative clauses.
*   **Weave** -- the *grain* of the mixture, which is two ways of reading a
    bilingual text rather than two difficulty settings. *Mixed* lets the weaver
    choose the unit per sentence -- a phrase, a clause, a whole sentence -- and
    explicitly allows both languages to share one sentence. *Whole sentences
    only* is the strict form: every sentence is entirely one language or the
    other, so the reader is never switching mid-clause. Note which way round the
    permission runs: the strict form forbids something the mixed form allows, and
    the mixed form forbids nothing the strict form does.

Keeping them separate matters, because they are genuinely independent: you can
have a dense weave of simple Spanish or a light weave of idiomatic Spanish, you
can do either a phrase at a time or a sentence at a time, and a learner may want
any of the combinations. Level also nudges the *gloss rate* -- at A1 nearly
every Spanish phrase needs an English crutch and by C1 annotation just gets in
the way -- and suggests a sensible default amount, which the learner is free to
override.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Level:
    code: str
    name: str
    # What the Spanish is allowed to contain. Goes into the weaving prompt.
    grammar: str
    # What kind of words to teach and use.
    vocabulary: str
    # How often to annotate a Spanish phrase with its English.
    gloss_rate: str
    # A sensible amount of Spanish for this level, which the learner can
    # override with the other dial.
    suggested_ratio: float


LEVELS: tuple[Level, ...] = (
    Level(
        code="A1",
        name="Beginner",
        grammar=(
            "present tense only; simple subject-verb-object sentences; the "
            "connectors y, pero, porque, también. No subjunctive, no compound tenses."
        ),
        vocabulary=(
            "the highest-frequency nouns and verbs -- things, people, and everyday "
            "actions. Concrete words a beginner meets in their first weeks."
        ),
        gloss_rate="almost every Spanish phrase",
        suggested_ratio=0.22,
    ),
    Level(
        code="A2",
        name="Elementary",
        grammar=(
            "present, preterite and imperfect; the near future with ir a + infinitive; "
            "obligation with tener que. No subjunctive."
        ),
        vocabulary=(
            "common verbs and nouns, including frequent reflexive verbs such as "
            "quedarse or darse cuenta de. Useful everyday constructions rather than "
            "single rare words."
        ),
        gloss_rate="most Spanish phrases",
        suggested_ratio=0.30,
    ),
    Level(
        code="B1",
        name="Intermediate",
        grammar=(
            "most tenses including future and conditional; the present subjunctive "
            "after common triggers (esperar que, aunque, para que); object pronouns "
            "in their natural positions."
        ),
        vocabulary=(
            "periphrastic verbs, conjunctive phrases and reflexive constructions -- "
            "the connectors and frames that hold an argument together."
        ),
        gloss_rate="about a third of them",
        suggested_ratio=0.40,
    ),
    Level(
        code="B2",
        name="Upper intermediate",
        grammar=(
            "the subjunctive used freely; passive and impersonal se; relative clauses; "
            "longer sentences with subordinate clauses."
        ),
        vocabulary=(
            "abstract and academic vocabulary; hedged and impersonal constructions; "
            "idioms that are still transparent from their parts."
        ),
        gloss_rate="only where the meaning is not obvious",
        suggested_ratio=0.50,
    ),
    Level(
        code="C1",
        name="Advanced",
        grammar=(
            "full range, including literary tenses and complex subordination; "
            "deliberate variation in sentence length and rhythm."
        ),
        vocabulary=(
            "idiomatic and technical register -- set phrases, collocations and "
            "figurative language that a literal reading would get wrong."
        ),
        gloss_rate="rarely, and only for true idioms",
        suggested_ratio=0.60,
    ),
)

BY_CODE: dict[str, Level] = {level.code: level for level in LEVELS}
BY_CODE["auto"] = Level(
    code="auto",
    name="Let the app estimate",
    grammar="match the complexity of the article's own argument",
    vocabulary="whatever the article itself makes useful",
    gloss_rate="about a third of them",
    suggested_ratio=0.38,
)

DEFAULT_LEVEL = "auto"

# Presets for the amount slider, so the number has names as well as a value.
AMOUNTS: tuple[tuple[str, float, str], ...] = (
    ("Light", 0.22, "short Spanish phrases inside comfortable English"),
    ("Balanced", 0.38, "a real weave — the two languages trade off"),
    ("Strong", 0.52, "Spanish carries whole clauses; English scaffolds"),
    ("Immersion", 0.68, "Spanish-dominant; English for the hard parts only"),
)

MIN_RATIO = 0.15
MAX_RATIO = 0.75


def resolve_level(code: str | None) -> Level:
    """Look up a level, falling back to "auto" for anything unrecognised."""
    if not code:
        return BY_CODE[DEFAULT_LEVEL]
    return BY_CODE.get(code.strip().lower()) or BY_CODE.get(code.strip().upper()) or BY_CODE[DEFAULT_LEVEL]


def clamp_ratio(value: float | None, level: Level) -> float:
    """Clamp a requested amount, defaulting to the level's suggestion."""
    if value is None:
        return level.suggested_ratio
    try:
        number = float(value)
    except (TypeError, ValueError):
        return level.suggested_ratio
    if number > 1.0:            # accept 40 as well as 0.40
        number = number / 100.0
    return max(MIN_RATIO, min(MAX_RATIO, number))


# --------------------------------------------------------------------------- #
# Grain: phrases inside a sentence, or whole sentences
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Weave:
    code: str
    name: str
    # What it is like to read. Shown in the import dialog, so it is written as a
    # reading note rather than a definition.
    blurb: str
    # What the model must do differently. Goes into the weaving prompt.
    instruction: str
    # A worked transformation. An example beats a rule list for this: the first
    # version of the weaving prompt had nine rules and reliably emitted nothing.
    example: str
    # Whether English prose may appear *inside* a Spanish sentence. This is the
    # whole difference between the two forms, and it is also what the quality
    # gates key off -- see tolerance_for and the spread limit.
    mixes_within_a_sentence: bool


WEAVES: tuple[Weave, ...] = (
    Weave(
        code="chunk",
        name="Mixed, as it reads best",
        blurb="The weaver picks the unit per sentence — a phrase, a clause, a whole sentence, "
              "and both languages may share one sentence. Denser practice per line, and the "
              "form that adapts most to the article.",
        instruction=(
            "Work sentence by sentence, and in each one use whatever unit reads best: swap a "
            "phrase, turn over a clause, or translate the whole sentence into Spanish. Mixed "
            "sentences are allowed here -- English and Spanish may share one sentence -- so a "
            "sentence can be half English and half Spanish where that is the natural way to "
            "render it. Never translate word by word, and never leave a translation so mixed "
            "that neither language can be read on its own."
        ),
        example=(
            '  in:  "Painters feared the camera would replace them, but it forced them to evolve."\n'
            '  out: "Los pintores temían que la cámara los reemplazara (*would replace them*), '
            'but it forced them to evolve."\n'
            "  (note: the Spanish takes the first half of that sentence and English keeps the "
            "second. This form may also turn over a whole sentence where that reads better.)"
        ),
        mixes_within_a_sentence=True,
    ),
    Weave(
        code="sentence",
        name="Whole sentences only",
        blurb="Some sentences are entirely Spanish and the rest entirely English, so each one "
              "is read with one language in your head rather than switching mid-clause.",
        instruction=(
            "Choose whole sentences to turn over into Spanish, and translate each one "
            "completely. A Spanish sentence must be Spanish throughout and an English "
            "sentence must stay English throughout -- never mix the two languages inside "
            "one sentence. Choose which ones by length and importance so the total lands "
            "near the amount below: at a light target take the longer, more contentful "
            "sentences and leave the short ones in English."
        ),
        example=(
            '  in:  "The camera did not kill painting. It forced it to evolve. Something '
            'similar is happening now."\n'
            '  out: "La cámara no mató la pintura. It forced it to evolve. Algo parecido '
            'está pasando ahora."\n'
            "  (note: whole sentences turn over; the English ones stay entirely English)"
        ),
        mixes_within_a_sentence=False,
    ),
)

BY_WEAVE: dict[str, Weave] = {weave.code: weave for weave in WEAVES}
DEFAULT_WEAVE = "chunk"


def resolve_weave(code: "Weave | str | None") -> Weave:
    """Look up a grain.

    Accepts a ``Weave`` and returns it unchanged, because this is what every
    caller threads a grain through: a function that takes "a code or a weave"
    and hands it on should not have to ask which it was given, and the version of
    this that only understood strings silently returned the *default* for a
    ``Weave`` -- so asking for whole sentences quietly produced mixed ones.

    Accepts the code or the name, because the name is what is written into a
    lesson's front matter -- ``- **Weave:** Whole sentences only`` reads like
    something a person wrote, and the format is one people edit by hand. A string
    that could mean either grain is not guessed at.
    """
    if isinstance(code, Weave):
        return code
    if not code:
        return BY_WEAVE[DEFAULT_WEAVE]
    key = str(code).strip().lower()
    if key in BY_WEAVE:
        return BY_WEAVE[key]
    hits = [weave for weave in WEAVES if key in weave.name.lower()]
    return hits[0] if len(hits) == 1 else BY_WEAVE[DEFAULT_WEAVE]


def weave_options() -> list[dict[str, str]]:
    return [
        {"code": weave.code, "name": weave.name, "blurb": weave.blurb,
         "example": weave.example}
        for weave in WEAVES
    ]


def tolerance_for(target: float, weave: "Weave | str | None" = None) -> float:
    """How far the measured weave may drift before it is regenerated.

    Proportional, because an absolute tolerance means different things at
    different targets: ±0.14 is over half of a light 22% weave and barely a
    fifth of an immersion one.

    Wider when whole sentences are the unit, because the amount is quantised by
    them: a five-sentence paragraph can be 0%, 20%, 40% Spanish and nothing in
    between, so a tolerance tight enough to be meaningful at phrase level would
    be unattainable here and the retry loop would spin.
    """
    resolved = resolve_weave(weave)
    if not resolved.mixes_within_a_sentence:
        return max(0.10, min(0.20, target * 0.5))
    return max(0.06, min(0.14, target * 0.35))


def describe(level: Level, ratio: float, weave: "Weave | str | None" = None,
             what: str = "article") -> str:
    """One line the UI can show under the controls, in plain language.

    ``what`` names the thing being woven, because the same controls serve an
    imported article and a piece the reader wrote themselves -- and a dialog about
    someone's own writing should not call it an article.
    """
    percent = round(ratio * 100)
    resolved = resolve_weave(weave)
    if resolved.mixes_within_a_sentence:
        if ratio <= 0.26:
            feel = "Easy going — the Spanish arrives in short, frequent bursts."
        elif ratio <= 0.44:
            feel = "A genuine weave — roughly every other sentence turns over."
        elif ratio <= 0.58:
            feel = "Demanding — Spanish holds the argument and English fills the gaps."
        else:
            feel = "Immersion — you will be reading Spanish, with English for the hard parts."
    else:
        # Sentence grain changes what the number means: the same share of words
        # arrives in fewer, larger pieces, and a reader is either in a Spanish
        # sentence or an English one.
        if ratio <= 0.26:
            feel = "Easy going — a Spanish sentence every few lines, the rest untouched."
        elif ratio <= 0.44:
            feel = "A genuine weave — you alternate between whole Spanish and English sentences."
        elif ratio <= 0.58:
            feel = "Demanding — most sentences are Spanish, and the English ones are the break."
        else:
            feel = "Immersion — nearly every sentence is Spanish."

    if level.code == "auto":
        kind = f"The app will judge the level from the {what} itself."
    else:
        # First clause only: the full grammar note is a paragraph, and this line
        # is a caption.
        kind = f"{level.name}: {level.grammar.split(';')[0].strip()}."
    return f"{feel} About {percent}% Spanish. {kind}"


def options() -> dict:
    """The whole taxonomy, for the import dialog to render."""
    return {
        "levels": [
            {
                "code": level.code,
                "name": level.name,
                "grammar": level.grammar,
                "vocabulary": level.vocabulary,
                "gloss_rate": level.gloss_rate,
                "suggested_ratio": level.suggested_ratio,
            }
            for level in (*LEVELS, BY_CODE["auto"])
        ],
        "weaves": weave_options(),
        "amounts": [{"label": label, "ratio": ratio, "note": note} for label, ratio, note in AMOUNTS],
        "min_ratio": MIN_RATIO,
        "max_ratio": MAX_RATIO,
        "default_level": DEFAULT_LEVEL,
        "default_weave": DEFAULT_WEAVE,
    }
