"""Turning any article on the web into a diglot lesson.

This is the part of the app that manufactures its own content. Given a URL it
fetches the page, extracts the prose, and rewrites it in the same weave the
hand-made corpus uses: English backbone, Spanish phrases set into it, English
glosses in parentheses where the meaning is not obvious, focus vocabulary in
bold, and post-reading anchors at the foot.

Three things make the output usable rather than merely Spanish-flavoured:

*   **Vocabulary is chosen once, for the whole article,** before any weaving
    happens. Chunk-by-chunk weaving with no shared word list produces an
    article that teaches nothing -- every paragraph picks different words. The
    focus list is what makes the recycled vocabulary actually recycle.
*   **The weave is measured, not assumed.** After generating, the result is run
    back through the same parser that reads the hand-made corpus, and its
    Spanish share is checked against the target band. A chunk that comes back
    with 10% or 90% Spanish is regenerated once with the measurement quoted
    back at the model. This is the only reason the generated articles sit
    comfortably on the same difficulty shelves as the originals.
*   **Output is the corpus format.** What gets written is a ``.md`` file in the
    same shape as the articles in ``spanishDiglot``, so the reader, the parser,
    the vocabulary harvester and the export all work on it unchanged, and a
    generated article can be moved into the corpus folder and be indistinguish-
    able from a hand-made one.
"""

from __future__ import annotations

import hashlib
import re
import textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .diglot import parse_article, parse_spans
from .fetch import (MIN_SOURCE_WORDS, MIN_WRITING_WORDS, FetchError, blocks_from_text,
                    extract_article, fetch_url, split_paragraphs)
from .levels import (
    DEFAULT_LEVEL,
    Level,
    Weave,
    clamp_ratio,
    describe,
    resolve_level,
    resolve_weave,
    tolerance_for,
)
from . import registers
from .llm import LLMError
from .tutor import SYSTEM, Tutor, cache_key

# Words per weaving call. Long enough that the model can hold a thread of
# argument, short enough that it does not lose the instructions -- and short
# enough that the answer fits comfortably inside a token budget the model also
# wants to spend on hidden reasoning.
CHUNK_WORDS = 420

# The amount of Spanish when nobody has asked for anything in particular --
# the middle of the corpus's own range, and the "Balanced" preset. The learner
# picks the real number; see app/levels.py.
TARGET_RATIO = 0.38

PARAGRAPH_MARK = "@@@"


class ImportError_(RuntimeError):
    """The article could not be fetched, woven, or validated."""


class _SilentProgress:
    """Stand-in for the job context when import runs outside the job system."""

    def step(self, name: str, **_: Any) -> None:
        pass

    def check(self) -> None:
        pass


@dataclass
class WovenChunk:
    paragraphs: list[str]
    attempts: int = 0
    ratio: float = 0.0
    spread: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"paragraphs": self.paragraphs, "attempts": self.attempts,
                "ratio": self.ratio, "spread": self.spread, "notes": self.notes,
                "reused": False}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WovenChunk":
        return cls(
            paragraphs=list(data.get("paragraphs") or []),
            attempts=int(data.get("attempts") or 0),
            ratio=float(data.get("ratio") or 0.0),
            spread=float(data.get("spread") or 0.0),
            notes=["reused a passage woven earlier"] + list(data.get("notes") or []),
        )


def weave_cache_key(
    chunk: str, focus: list[dict[str, str]], target: float, level: Level,
    weave: Weave | str | None = None,
) -> str:
    """Identify a woven passage by everything that determines its output.

    The point is that a *retry* is cheap. Weaving twelve passages takes about
    ten minutes; when one fails, or the server restarts mid-import, everything
    that already succeeded should be reused rather than paid for twice. Keying
    on the passage, the focus list, the amount, the level and the grain means a
    genuine change to any of them misses the cache, as it should -- and the
    grain is in there because the same passage at the same amount is a
    completely different text depending on the unit it arrives in.
    """
    digest = hashlib.sha256()
    digest.update(chunk.encode("utf-8"))
    digest.update(b"|")
    digest.update("|".join(sorted(item.get("es", "") for item in focus)).encode("utf-8"))
    digest.update(f"|{target:.3f}|{level.code}|{resolve_weave(weave).code}".encode("utf-8"))
    return cache_key("weave", digest.hexdigest()[:24])


# --------------------------------------------------------------------------- #
# Vocabulary selection
# --------------------------------------------------------------------------- #


def choose_focus_vocabulary(
    tutor: Tutor, *, title: str, text: str, count: int = 6, level: Level | None = None
) -> list[dict[str, str]]:
    """Pick the phrases the lesson will teach, before any weaving happens.

    The model is asked for *useful* items -- constructions a learner at this
    level would actually deploy -- rather than the rarest words in the text.
    The level matters here more than anywhere else: the same article yields
    "el sueño / dormir" for a beginner and "dar lugar a / se dio cuenta de" for
    someone ready for periphrases.
    """
    level = level or resolve_level(None)
    level_rules = (
        f"The learner is at roughly {level.code} ({level.name}). Choose words and "
        f"constructions at that level: {level.vocabulary}"
        if level.code != "auto" else
        f"Choose words and constructions at roughly the level of the article: {level.vocabulary}"
    )
    prompt = f"""You are choosing the vocabulary to teach in a diglot lesson built from this article.

Title: {title}

---
{text[:5000]}
---

Choose exactly {count} Spanish words or short phrases that:
- recur naturally in a text like this, so they can appear several times;
- are useful to an English speaker who reads but does not yet write Spanish;
- include at least two multi-word constructions (a periphrastic verb, a
  conjunctive phrase, a reflexive verb) rather than only single nouns.

{level_rules}
Do not repeat what is essentially the same item twice: if you choose
"relacionado con", do not also return "relacionada con" and "estar relacionado con".

Return JSON:
{{
  "items": [
    {{
      "es": "the Spanish, in the form it would appear in a sentence",
      "en": "its English meaning, 1-4 words",
      "kind": "one of: verb, noun, adjective, connector, phrase",
      "why": "why this is worth teaching here, at most 12 words"
    }}
  ],
  "level": "the CEFR level of the Spanish you actually chose, A1-C1"
}}"""
    data, _ = tutor.client.complete_json(prompt, system=SYSTEM, max_tokens=1200)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise ImportError_("the tutor did not return a usable vocabulary list")
    return [i for i in items if isinstance(i, dict) and i.get("es")][:count]


# --------------------------------------------------------------------------- #
# Weaving
# --------------------------------------------------------------------------- #


def _weave_prompt(
    *, chunk: str, focus: list[dict[str, str]], target: float, level: Level,
    weave: Weave | str | None = None, feedback: str | None = None,
) -> str:
    """The weaving instruction.

    Kept short and concrete on purpose. Long rule lists make a reasoning model
    deliberate: an earlier version with nine rules and an instruction to "count
    as you go" reliably spent its entire token budget thinking and emitted
    nothing. Fewer rules, an explicit example of the transformation, and the
    percentage stated as a rough feel rather than a quantity to be counted.

    The level changes what the *Spanish may contain*; the amount changes how much
    of it there is; the weave changes the *unit* it arrives in. All three are
    stated plainly because a model given "about a third" and nothing else will
    reach for whatever vocabulary it likes, and one given a percentage without a
    grain will produce the mosaic -- English and Spanish inside one sentence,
    which is the hardest thing on this list to read.
    """
    grain = resolve_weave(weave)
    vocab = "\n".join(f"  - {item['es']} ({item.get('en', '')})" for item in focus)
    feedback_block = f"\n\nYour previous attempt was rejected: {feedback}\n" if feedback else ""
    register = (
        f"The reader is learning at roughly {level.code} ({level.name}), so keep the "
        f"Spanish to this range: {level.grammar}"
        if level.code != "auto" else
        f"Match the register of the original: {level.grammar}"
    )
    # The model systematically overshoots a light target: "keep English as the
    # majority" is satisfied by 29%, so it needs telling what a *light* weave
    # actually looks like rather than just the number.
    #
    # These notes have to agree with the distribution rule below, not fight it.
    # An earlier version said "spread the Spanish evenly -- a phrase in most
    # sentences" while also asking for 22%, and the two instructions cannot both
    # be obeyed: in short sentences, a phrase in most of them *is* more than
    # 22%. The model resolved the conflict upward, which is most of why a
    # requested 22% kept arriving at 29-32%.
    if grain.mixes_within_a_sentence:
        # How *many* sentences carry Spanish, not how big the piece of Spanish in
        # each one is. Saying "swap a single short phrase and leave the rest
        # alone" also forbade translating a whole sentence, which is a perfectly
        # good way to spend 22% of a paragraph -- and the reader does not care
        # which unit the Spanish arrived in, only whether the passage reads well.
        if target <= 0.18:
            density = (
                "A very light touch: roughly one sentence in four or five carries any Spanish "
                "at all, and inside it a phrase is usually enough. At this amount a whole "
                "translated sentence will put you over, unless the sentences are long."
            )
        elif target <= 0.28:
            density = (
                "A light weave: roughly every second or third sentence carries Spanish. In "
                "each one, either swap a phrase or turn the whole sentence over -- the second "
                "is often the better read when the sentence is short. Most sentences should "
                "still be entirely English."
            )
        elif target <= 0.45:
            density = (
                "A middle weave: roughly every other sentence carries Spanish, and the ones "
                "that do can turn over most of their length, or all of it."
            )
        else:
            density = (
                "A heavy weave: most sentences should be substantially or wholly Spanish, with "
                "English carrying only the parts that would be hard to follow."
            )
        distribution = (
            "- Distribute the Spanish rather than concentrating it: do not leave a run of "
            "sentences in English and then translate a whole paragraph at once."
        )
    else:
        if target <= 0.18:
            density = (
                "A very light weave. Turn over roughly one sentence in four or five, and "
                "choose the longer ones -- a single long Spanish sentence is easier to read "
                "than three short ones, and easier to keep near the target."
            )
        elif target <= 0.28:
            density = (
                "A light weave. Turn over roughly one sentence in three, preferring the "
                "longer and more contentful ones. On a page of a dozen sentences, three or "
                "four should be Spanish and the rest should be untouched English."
            )
        elif target <= 0.45:
            density = (
                "A middle weave: roughly every other sentence, alternating rather than in "
                "blocks, so the reader is never far from either language."
            )
        elif target <= 0.62:
            density = (
                "A heavy weave: Spanish for most sentences. The English ones are the break "
                "the reader gets, so spend them on the short or the hardest sentences."
            )
        else:
            density = (
                "Immersion: nearly every sentence is Spanish, with English left only for "
                "the sentences that would be unreadable otherwise."
            )
        distribution = (
            "- Spread the Spanish across the passage rather than piling it into one part: "
            "most paragraphs should contain at least one Spanish sentence and one English "
            "one, unless the amount is high enough that a paragraph has no room for both."
        )
    return f"""Rewrite the English passage below, weaving Spanish into it for an English speaker learning Spanish.

{grain.instruction}

Example of the transformation:
{grain.example}

{register}

Amount: roughly {target:.0%} of the words should end up Spanish -- it is fine to be a little over or under, but do not overshoot. {density}

Rules:
- The author's meaning must survive exactly. Add nothing, cut nothing, do not summarise.
- Every sentence of the original must still be present, in order, and complete.
{distribution}
- Straight after a Spanish phrase whose meaning is not obvious from the English around it, add the English in italics in parentheses: (*like this*). Do this for {level.gloss_rate}.
- Bold a focus phrase the first two times it appears: **así**.
- Leave proper nouns, work titles and quoted speech alone. Captions and photo credits are not prose: leave them in English and do not weave them.

Use these focus phrases repeatedly, and bold them:
{vocab}

Output one paragraph per input paragraph, in order, separated by a line containing only {PARAGRAPH_MARK}. No headings, no commentary, no code fences.{feedback_block}

PASSAGE
{chunk}"""


def _ratio_of(text: str) -> float:
    """Share of words the segmenter reads as Spanish, for one passage."""
    if not text.strip():
        return 0.0
    es = en = 0
    for span in parse_spans(" ".join(text.split())):
        n = len(re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", span.text))
        if span.lang == "es":
            es += n
        else:
            en += n
    total = es + en
    return es / total if total else 0.0


def _measure(text: str) -> float:
    """Share of words the segmenter reads as Spanish. Uses the real parser, so
    the number measured is the number the reader will act on."""
    return _ratio_of(text)


# How unevenly the Spanish may be spread before a chunk is rewoven. The
# article-wide average was hiding this: a lesson asked for at 22% came out with
# paragraphs between 12% and 61%, because the model would leave several
# sentences untouched and then translate a whole paragraph at once.
SPREAD_LIMIT = 0.62

# The same measure at sentence grain, where unevenness is the form rather than a
# fault: a five-sentence paragraph with one Spanish sentence is 20% Spanish and
# the next with two is 40%, so a paragraph-level spread of 20 points is normal
# and unavoidable. This limit only catches the case the rule exists for -- one
# paragraph translated end to end while its neighbours are untouched.
SENTENCE_SPREAD_LIMIT = 0.86
MIN_PARAGRAPH_WORDS = 30


def _spread(paragraphs: list[str], *, floor: int = MIN_PARAGRAPH_WORDS) -> float:
    """The gap between the least and most Spanish paragraph.

    The average alone cannot express what a reader experiences, which is one
    paragraph at a time. This is that, as a single number.
    """
    ratios = [_ratio_of(p) for p in paragraphs if len(p.split()) >= floor]
    if len(ratios) < 2:
        return 0.0
    return max(ratios) - min(ratios)


# How much shorter than its source a woven passage may be before it is treated
# as having lost content. Weaving swaps phrases between languages; it should
# not change how much is said. A passage that comes back at half length has been
# summarised or truncated -- both explicitly forbidden by the prompt, and both
# worse than any formatting problem, because the reader never learns that part
# of the article is missing.
MIN_LENGTH_RATIO = 0.72


def _redistribute(paragraphs: list[str], wanted: list[str]) -> list[str]:
    """Re-split a merged weave back into the source's paragraph structure.

    The weave is asked for one output paragraph per input paragraph, and
    sometimes returns one enormous block instead -- a Wikipedia article came
    back as a single 285-word paragraph, which the reader renders as a wall of
    text. The content is all there; only the breaks are missing.

    Sentences are dealt out to paragraphs in proportion to how long each source
    paragraph was, so the shape of the original survives even though the exact
    break points cannot be recovered.
    """
    text = " ".join(p.strip() for p in paragraphs if p.strip())
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    if len(sentences) < len(wanted):
        return paragraphs          # too little material to divide honestly

    weights = [max(len(w.split()), 1) for w in wanted]
    total = sum(weights)
    out: list[str] = []
    index = 0
    for position, weight in enumerate(weights):
        remaining_paragraphs = len(weights) - position - 1
        if remaining_paragraphs == 0:
            take = len(sentences) - index
        else:
            share = weight / total
            take = max(1, round(len(sentences) * share))
            # Always leave at least one sentence for each paragraph still to come.
            take = min(take, len(sentences) - index - remaining_paragraphs)
        if take <= 0:
            continue
        out.append(" ".join(sentences[index:index + take]))
        index += take
    return out if len(out) == len(wanted) else paragraphs


def weave_chunk(
    tutor: Tutor, *, chunk: str, focus: list[dict[str, str]], target: float = TARGET_RATIO,
    level: Level | None = None, weave: Weave | str | None = None,
) -> WovenChunk:
    """Weave one chunk, measuring the result and retrying once if it is off.

    Two failure modes are handled rather than propagated. A model that returns
    *nothing* is retried paragraph by paragraph -- short inputs almost always
    come back, and a chunk that weaves four paragraphs out of five is a lesson,
    whereas a chunk that raises is a failed import. A model that returns the
    *wrong amount* of Spanish is retried once with the measurement quoted back
    at it.

    Both gates -- how far the amount may drift, and how unevenly it may be spread
    -- are looser when the unit is a whole sentence, because the amount is
    quantised by sentences and an even spread is not something a sentence-grain
    weave can offer. Judged by the phrase-grain limits, a perfectly good
    sentence-level passage would be rejected and rewoven forever.
    """
    level = level or resolve_level(None)
    grain = resolve_weave(weave)
    tolerance = tolerance_for(target, grain)
    spread_limit = SPREAD_LIMIT if grain.mixes_within_a_sentence else SENTENCE_SPREAD_LIMIT
    wanted = [p for p in chunk.split("\n") if p.strip()]
    result = WovenChunk(paragraphs=[])
    feedback: str | None = None
    # What the prompt asks for, which stops being what the reader asked for as
    # soon as an attempt comes back off target.
    #
    # Re-asking for the same number with a note saying it was wrong is what made
    # this loop oscillate: the density instruction is *chosen from* the number,
    # so the retry built the same instruction the model had already ignored, and
    # the model's answer landed in the same place. Correcting the number means
    # the retry also crosses into a different density instruction, which is the
    # thing the model actually responds to.
    ask = target

    for attempt in (1, 2, 3):
        prompt = _weave_prompt(chunk=chunk, focus=focus, target=ask, level=level,
                               weave=grain, feedback=feedback)
        try:
            response = tutor.client.complete(prompt, system=SYSTEM, max_tokens=8000, temperature=0.5)
        except LLMError as exc:
            # A chunk that cannot be generated must not take the whole import
            # down with it -- the paragraph fallback below is the safety net.
            result.notes.append(f"attempt {attempt}: {exc}")
            feedback = None
            continue
        paragraphs = _split_paragraphs(response.text)
        if not paragraphs:
            feedback = "The output was empty. Return the woven passage."
            result.notes.append(f"attempt {attempt}: empty output")
            continue

        # Repair the structure before measuring anything: a merged blob would
        # otherwise be judged -- and shown -- as one enormous paragraph.
        if len(paragraphs) < len(wanted) and len(wanted) > 1:
            repaired = _redistribute(paragraphs, wanted)
            result.notes.append(
                f"attempt {attempt}: {len(paragraphs)} paragraphs for {len(wanted)} input "
                f"paragraphs, redistributed to {len(repaired)}"
            )
            paragraphs = repaired

        blob = "\n\n".join(paragraphs)
        ratio = _measure(blob)
        spread = _spread(paragraphs)
        source_words = len(chunk.split())
        output_words = len(blob.split())
        kept = output_words / source_words if source_words else 1.0
        result.attempts = attempt
        result.ratio = ratio
        result.spread = spread
        result.paragraphs = paragraphs

        if len(paragraphs) != len(wanted):
            result.notes.append(
                f"attempt {attempt}: {len(paragraphs)} paragraphs for {len(wanted)} input paragraphs"
            )

        truncated = kept < MIN_LENGTH_RATIO
        amount_off = abs(ratio - target) > tolerance
        lumpy = spread > spread_limit
        if not truncated and not amount_off and not lumpy:
            return result

        # Say everything that is wrong, not just the first thing. Asking only
        # about the amount leaves a shortened passage to survive the attempt.
        problems: list[str] = []
        if truncated:
            problems.append(
                f"You returned {output_words} words for a passage of {source_words}. "
                "Every sentence must be present and complete -- translate phrases within the "
                "sentences, but do not shorten, summarise or drop any of them."
            )
            result.notes.append(f"attempt {attempt}: kept only {kept:.0%} of the words, retrying")
        if amount_off:
            # Correct by the error against what was *asked*, not against the
            # reader's target -- and say the corrected number out loud so the
            # instruction is concrete.
            #
            # Measuring against the target double-counts the previous correction.
            # A live run showed what that does: asked 54%, came back 81% (too
            # high), so the retry asked 27%. The model obeyed and produced 29%,
            # which is close to the ask and far from the target -- so correcting
            # against the target swung the next ask to 79%, and the loop
            # oscillated instead of converging. The model's error is measured
            # from the number it was given.
            error = ratio - ask
            ask = round(min(0.85, max(0.06, ask - error)), 3)
            direction = "less" if error > 0 else "more"
            problems.append(
                f"It came out {ratio:.0%} Spanish and the target is {target:.0%}. "
                f"Use noticeably {direction} Spanish this time -- aim for about {ask:.0%}."
            )
        if lumpy:
            problems.append(
                f"The Spanish is unevenly spread -- some paragraphs are {spread:.0%} apart in how "
                "much they carry. Keep the amount the same but distribute it evenly: a phrase in "
                "most sentences, rather than a run of untouched English then a whole Spanish paragraph."
            )
        feedback = " ".join(problems)
        if not truncated:
            result.notes.append(
                f"attempt {attempt}: {ratio:.0%} Spanish"
                + (f", {spread:.0%} spread" if lumpy else "")
                + (f", retrying at {ask:.0%}" if amount_off else ", retrying")
            )

    if not result.paragraphs and wanted:
        # Last resort: weave smaller pieces. A single-paragraph chunk has
        # nothing smaller to fall back to, so it is split into sentences --
        # short inputs come back when long ones do not.
        if len(wanted) > 1:
            pieces = wanted
            result.notes.append("falling back to paragraph-by-paragraph weaving")
        else:
            pieces = [s for s in re.split(r"(?<=[.!?])\s+", wanted[0]) if s.strip()]
            result.notes.append(f"falling back to sentence-by-sentence weaving ({len(pieces)} sentences)")
        if not pieces:
            return result

        salvaged: list[str] = []
        for piece in pieces:
            prompt = _weave_prompt(chunk=piece, focus=focus, target=target, level=level)
            try:
                response = tutor.client.complete(prompt, system=SYSTEM, max_tokens=5000, temperature=0.5)
            except LLMError as exc:  # keep whatever we already have
                result.notes.append(f"piece failed: {exc}")
                salvaged.append(piece)
                continue
            parts = _split_paragraphs(response.text)
            salvaged.append(parts[0] if parts else piece)

        if len(wanted) > 1:
            result.paragraphs = salvaged
        else:
            # The sentence pieces belong to one paragraph; put them back as one.
            result.paragraphs = [" ".join(salvaged)]
        result.ratio = _measure("\n\n".join(result.paragraphs))

    return result


def _split_paragraphs(text: str) -> list[str]:
    text = re.sub(r"^\s*```[a-z]*\s*$", "", text, flags=re.M).strip()
    if PARAGRAPH_MARK in text:
        parts = [p.strip() for p in text.split(PARAGRAPH_MARK)]
    else:
        parts = [p.strip() for p in re.split(r"\n\s*\n", text)]
    return [re.sub(r"\s+", " ", p) for p in parts if p.strip()]


def _chunk_paragraphs(paragraphs: list[str], max_words: int = CHUNK_WORDS) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    count = 0
    for paragraph in paragraphs:
        words = len(paragraph.split())
        if current and count + words > max_words:
            chunks.append(current)
            current, count = [], 0
        current.append(paragraph)
        count += words
    if current:
        chunks.append(current)
    return chunks


def _chunk_pieces(pieces: list[tuple[str, str]], max_words: int = CHUNK_WORDS
                  ) -> tuple[list[list[str]], list[tuple[str, Any]]]:
    """Split an ordered block list into weavable groups, plus a layout to reassemble.

    Only paragraphs are woven; headings are structural and stay as they are,
    in English, exactly where the author put them. The layout records the
    original order as a sequence of ``("h", text)`` and ``("g", group_index)``
    so the woven groups can be spliced back between the headings they belong
    to. Without this an imported passage has no headings at all -- the extractor
    finds them and the assembler simply dropped them -- and so no contents pane.
    """
    groups: list[list[str]] = []
    layout: list[tuple[str, Any]] = []
    current: list[str] = []
    words = 0

    def close_group() -> None:
        nonlocal current, words
        if current:
            groups.append(current)
            layout.append(("g", len(groups) - 1))
            current, words = [], 0

    for kind, text in pieces:
        if kind == "h":
            close_group()
            layout.append(("h", text))
            continue
        size = len(text.split())
        if current and words + size > max_words:
            close_group()
        current.append(text)
        words += size
    close_group()
    return groups, layout


# --------------------------------------------------------------------------- #
# Anchors
# --------------------------------------------------------------------------- #


def build_anchors(tutor: Tutor, *, title: str, woven: str, focus: list[dict[str, str]]) -> tuple[list[dict], list[dict]]:
    """Generate the post-reading Recycled Vocabulary Box and Grammar Breakdown.

    Quoting the *woven* text back, not the source, is deliberate: the examples
    have to be sentences the learner has actually read.
    """
    prompt = f"""Here is a diglot lesson -- English prose with Spanish woven into it -- that a learner has just read.

Title: {title}

---
{woven[:6000]}
---

The lesson was built around these focus items:
{chr(10).join(f"- {i['es']} ({i.get('en','')})" for i in focus)}

Produce the post-reading anchors.

1. "vocab": for each focus item, the headword, its English gloss, and 2-3 examples
   of it **copied verbatim from the lesson text above**, each with a short English
   translation in italics in parentheses.
2. "grammar": exactly 3 notes on the grammar the lesson actually exercises.
   Each note names a construction, quotes a real sentence from the lesson, and
   explains in 2-4 sentences of English why it works that way and what a learner
   gets wrong about it.

Return JSON:
{{
  "vocab": [
    {{"term": "headword / inflected forms", "gloss": "to ...", "examples": ["Spanish sentence (English translation)", "..."]}}
  ],
  "grammar": [
    {{"title": "Name of the construction", "example": "the Spanish sentence from the lesson", "explanation": "2-4 sentences of English"}}
  ]
}}"""
    data, _ = tutor.client.complete_json(prompt, system=SYSTEM, max_tokens=2600, effort="medium")
    vocab = data.get("vocab") if isinstance(data, dict) else None
    grammar = data.get("grammar") if isinstance(data, dict) else None
    return (
        [v for v in (vocab or []) if isinstance(v, dict) and v.get("term")],
        [g for g in (grammar or []) if isinstance(g, dict) and g.get("title")],
    )


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def slugify(title: str, url: str) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48].strip("-")
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:6]
    return f"{stem or 'imported'}-{digest}"


def assemble_markdown(
    *,
    title: str,
    byline: str | None,
    url: str,
    preview: str,
    items: list[tuple[str, str]],
    vocab: list[dict],
    grammar: list[dict],
    focus: list[dict],
    level: str | None,
    register: str | None = None,
    weave: Weave | str | None = None,
    target: float | None = None,
    requested_level: Level | None = None,
    source_note: str | None = None,
) -> str:
    """Write the lesson out in the corpus's own Markdown dialect."""
    lines: list[str] = [
        "### Article Identification & Preview",
        "",
        f"- **Article Title:** {title}",
    ]
    if byline:
        lines.append(f"- **Author:** {byline}")
    lines += [
        f"- **Direct URL:** {url}" if url else f"- **Source:** {source_note or 'pasted text'}",
        f"- **Preview:** {preview}",
        f"- **Imported:** {datetime.now(timezone.utc).date().isoformat()}",
    ]
    if level:
        lines.append(f"- **Level:** {level}")
    # Record how the lesson was made, so the file explains itself and a later
    # re-read can tell a deliberate 25% weave from a bad one.
    if register:
        lines.append(f"- **Register:** {register}")
    if target is not None:
        asked = requested_level.name if requested_level and requested_level.code != "auto" else "auto level"
        lines.append(f"- **Spanish:** about {round(target * 100)}% ({asked})")
        # The grain, for the same reason as the amount: a lesson woven sentence by
        # sentence is a different text from the same lesson woven phrase by
        # phrase, and the file should say which one it is. Written as the name
        # rather than the code, because this line is read by people.
        lines.append(f"- **Weave:** {resolve_weave(weave).name}")
    lines += ["", f"# {title}", ""]
    if byline:
        lines += [f"**By {byline}**", ""]

    # Each block on its own line with a blank line after it: the parser splits
    # on blank lines, so writing them flush would silently fuse the whole
    # article into one enormous paragraph.
    #
    # The woven text goes in verbatim. It is Markdown by design -- ``(*gloss*)``
    # and ``**focus**`` are the format's markup, and escaping the asterisks
    # would leave glosses on the page as literal text.
    #
    # Headings are written back as headings, in English, where the source had
    # them: they are structure, and they are what the reader's contents pane is
    # built from.
    for kind, text in items:
        lines += [f"### {text}" if kind == "h" else text, ""]
    lines += ["---", "", "### POST-READING ANCHORS", "", "**Recycled Vocabulary Box**", ""]

    by_term = {item["es"].lower(): item for item in focus}
    for entry in vocab:
        term = str(entry.get("term", "")).strip()
        gloss = str(entry.get("gloss", "")).strip()
        lines.append(f"- **{term}** (*{gloss}*)")
        head = by_term.get(term.lower(), {}).get("es", term.split("/")[0].strip().lower())
        for index, example in enumerate(entry.get("examples") or [], start=1):
            lines.append(f"{index}. {example}")
        if not entry.get("examples"):
            lines.append(f"1. Search the lesson for **{head}** and read it in place.")
    lines += ["", "---", "", "**Grammar Breakdown**", ""]
    for index, note in enumerate(grammar, start=1):
        lines.append(f"{index}. **{str(note.get('title','')).rstrip(':')}:**")
        if note.get("example"):
            lines.append(f"- *Example:* \"{note['example']}\"")
        if note.get("explanation"):
            lines.append(f"- *Explanation:* {note['explanation']}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def english_text(tutor: Tutor, *, text: str, cache: Any = None) -> tuple[str, str | None, list[str]]:
    """A passage in English, and the language it was written in.

    Returns ``(english, language, notes)``. ``language`` is ``None`` when the
    passage was already English -- and in that case the text comes back untouched,
    because it is the reader's own writing and their words are the point. The
    model is trusted for its judgement there and nothing else: a model asked to
    "translate" English paraphrases it, which for someone's own essay is a worse
    outcome than not running the step at all.

    Cached against the passage, so re-weaving a piece at a different amount does
    not pay for the same translation again.
    """
    key = cache_key("english", hashlib.sha1(text.encode("utf-8")).hexdigest()[:24])
    found = cache.get(key) if cache is not None else None
    if not found:
        found = tutor.to_english(text=text)
        if cache is not None and found:
            cache.put(key, found)

    language = str(found.get("language_name") or "").strip()
    if not language or str(found.get("language") or "").strip().lower().startswith("en"):
        return text, None, []

    english = str(found.get("english") or "").strip()
    if not english:
        raise ImportError_(
            "the passage could not be put into English, and the diglot format needs an "
            "English base to weave Spanish into"
        )
    notes: list[str] = []
    return _keep_paragraph_shape(english, text, notes), language, notes


def _keep_paragraph_shape(english: str, source: str, notes: list[str]) -> str:
    """One paragraph out for one paragraph in.

    The same failure the weave has: a model handed five paragraphs can return one
    enormous block, and the reader then gets a wall of text. The content is all
    there, so the breaks are recovered from the source's own shape rather than the
    translation being thrown away -- which is what _redistribute already does for
    the weave.
    """
    wanted = split_paragraphs(source)
    got = split_paragraphs(english)
    if len(got) == len(wanted):
        return english
    if len(got) < len(wanted):
        fixed = _redistribute(got, wanted)
        if len(fixed) == len(wanted):
            notes.append(
                f"the translation came back as {len(got)} paragraph(s) against {len(wanted)} "
                "-- the breaks were rebuilt from yours"
            )
            return "\n\n".join(fixed)
    notes.append(f"the translation came back as {len(got)} paragraph(s) against {len(wanted)}")
    return english


def import_writing(
    *,
    text: str,
    settings: Any,
    tutor: Tutor,
    title: str | None = None,
    judge: Any = None,
    progress: Any = None,
    level_code: str | None = None,
    ratio: float | None = None,
    weave: "Weave | str | None" = None,
    cache: Any = None,
) -> dict[str, Any]:
    """Turn something the reader wrote into a lesson, in whatever language.

    Two steps, in this order and for a reason. First the passage is put into
    English: the diglot format is English prose with Spanish woven in, so a
    passage written in Spanish -- or French, or Chinese -- has to be rendered
    before it can be woven at all. Then it goes through the *same* pipeline as a
    fetched article: the same three dials, the same focus vocabulary, the same
    anchors, the same front matter. A lesson made from the reader's own writing is
    not a second-class lesson; what they wrote is just a different way in.

    The original passage identifies the lesson, not the translation of it: two
    runs must produce the same slug, and a revision must not look like the version
    before it.
    """
    progress = progress or _SilentProgress()
    progress.step("Reading what you wrote", done=0, total=1)
    english, language, notes = english_text(tutor, text=text, cache=cache)
    progress.check()

    report = import_article(
        text=english, settings=settings, tutor=tutor, title_hint=title, judge=judge,
        progress=progress, level_code=level_code, ratio=ratio, weave=weave, cache=cache,
        source_label=("your own writing" if not language
                      else f"your own writing, written in {language}"),
        source_id=f"writing:{hashlib.sha1(text.encode('utf-8')).hexdigest()}",
        min_words=MIN_WRITING_WORDS,
    )
    report["notes"] = notes + report["notes"]
    report["source_language"] = language
    return report


def import_article(
    *,
    url: str | None = None,
    settings: Any,
    tutor: Tutor,
    title_hint: str | None = None,
    judge: Any = None,
    progress: Any = None,
    level_code: str | None = None,
    ratio: float | None = None,
    weave: "Weave | str | None" = None,
    cache: Any = None,
    text: str | None = None,
    source_url: str | None = None,
    source_label: str | None = None,
    source_id: str | None = None,
    min_words: int = MIN_SOURCE_WORDS,
) -> dict[str, Any]:
    """Fetch, extract, weave and shelve one article. Returns a report.

    ``text`` is the reader's own paste, standing in for the fetch and the
    extraction: some pages cannot be fetched at all -- paywalls, JavaScript-only
    readers, hosts this machine cannot reach -- but the reader can read them and
    copy them out. Everything after that point is the same pipeline, which is the
    point: a pasted article is a lesson like any other, with the same three dials,
    the same notes and the same front matter. ``source_url`` is where it came
    from, kept for the reader's reference without being fetched.

    ``source_label`` and ``source_id`` are provenance for a lesson that had no
    page behind it: what to write on the front matter's ``Source:`` line when there
    is no URL, and what the slug should digest so that two different sources do not
    become one lesson. See ``import_writing``.

    ``level_code``, ``ratio`` and ``weave`` are the learner's three dials: what
    kind of Spanish the lesson may use, how much of it there should be, and what
    unit it arrives in. All three are optional -- with none, the app estimates a
    level afterwards and weaves to its own defaults. With any, the choice is
    honoured and recorded in the lesson's front matter so the file says how it
    was made.

    ``progress`` is the job context: it receives a step name and unit counts so
    the Activity panel can show "weaving passage 3 of 7" rather than a spinner,
    and it is asked to check for cancellation between stages. Passing ``None``
    runs the import silently, which is what the tests do.
    """
    progress = progress or _SilentProgress()
    requested_level = resolve_level(level_code)
    grain = resolve_weave(weave)
    target = clamp_ratio(ratio, requested_level)

    pasted = bool((text or "").strip())
    if pasted:
        progress.step("Reading what you pasted", done=1, total=1)
        try:
            extracted = blocks_from_text(text, min_words=min_words)
        except FetchError as exc:
            raise ImportError_(str(exc)) from None
        final_url = (source_url or "").strip()
    else:
        if not (url or "").strip():
            raise ImportError_("no article to import: give a link or paste the text")
        progress.step("Fetching the page", done=0, total=1)
        try:
            html_text, final_url = fetch_url(url, proxy=settings.proxy)
        except FetchError as exc:
            raise ImportError_(str(exc)) from None

        progress.check()
        progress.step("Reading the article", done=1)
        try:
            extracted = extract_article(html_text)
        except FetchError as exc:
            raise ImportError_(str(exc)) from None

    title = (title_hint or extracted["title"] or final_url or "Pasted text").strip()
    blocks = extracted["blocks"]
    paragraphs = [b["text"] for b in blocks if b["kind"] == "p"]
    if not paragraphs:
        raise ImportError_("no body text found on that page")

    # Headings are kept in the output. They were being extracted and then
    # discarded, which cost every imported passage its contents pane -- the
    # corpus articles have headings, so only imported ones had no way to
    # navigate, and it looked like the feature was broken rather than absent.
    pieces: list[tuple[str, str]] = [
        ("h" if b["kind"] == "h" else "p", b["text"])
        for b in blocks
        if b["kind"] in ("h", "p")
    ]
    heading_count = sum(1 for kind, _ in pieces if kind == "h")

    whole = "\n\n".join(paragraphs)
    groups, layout = _chunk_pieces(pieces)

    # Everything after the page is fetched is known up front, so the bar can be
    # honest from here on: one unit for vocabulary, one per passage, one each
    # for the notes and the read-back check.
    total_units = len(groups) + 3
    progress.step(f"Choosing the vocabulary to teach ({len(groups)} passages to weave)",
                  done=2, total=total_units)
    focus = choose_focus_vocabulary(tutor, title=title, text=whole, level=requested_level)
    level: str | None = requested_level.code if requested_level.code != "auto" else None
    register: str | None = None

    # Weave the chunks concurrently: each call is independent and takes seconds,
    # and on a long article they are most of the wall-clock time. Progress is
    # reported as each one lands rather than when the whole batch does.
    progress.check()
    progress.step(f"Weaving Spanish into the text (aiming for {target:.0%})",
                  done=2, total=total_units)

    def weave_one(group: list[str]) -> WovenChunk:
        """Weave a passage, reusing an earlier identical one if there is one.

        This is what makes a retry cheap. A twelve-passage weave is about ten
        minutes; when one passage fails, or the server restarts mid-import,
        everything that already succeeded should be kept rather than paid for a
        second time. The key covers the passage, the focus list, the amount and
        the level, so a genuine change to any of them misses -- as it should.
        """
        chunk = "\n".join(group)
        key = weave_cache_key(chunk, focus, target, requested_level, grain)
        if cache is not None:
            hit = cache.get(key)
            if hit:
                return WovenChunk.from_dict(hit)
        result = weave_chunk(tutor, chunk=chunk, focus=focus, target=target,
                             level=requested_level, weave=grain)
        if cache is not None and result.paragraphs:
            cache.put(key, result.to_dict())
        return result

    woven: list[WovenChunk] = [WovenChunk(paragraphs=[]) for _ in groups]
    done_units = 2
    with ThreadPoolExecutor(max_workers=min(4, len(groups))) as pool:
        futures = {pool.submit(weave_one, group): index for index, group in enumerate(groups)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                woven[index] = future.result()
            except Exception as exc:  # a failed chunk falls back, it does not abort
                woven[index] = WovenChunk(paragraphs=[], notes=[f"{type(exc).__name__}: {exc}"])
            done_units += 1
            progress.step("Weaving Spanish into the text", done=done_units, total=total_units,
                          detail=f"passage {done_units - 2} of {len(groups)}")
    reused = sum(1 for result in woven if any("reused" in note for note in result.notes))

    # Reassemble in the source's order: headings as they were, woven groups
    # where the paragraphs they came from used to be.
    woven_by_group: list[list[str]] = []
    notes: list[str] = []
    for group, result in zip(groups, woven):
        notes += result.notes
        if result.paragraphs and len(result.paragraphs) == len(group):
            woven_by_group.append(result.paragraphs)
        else:
            # The weave drifted structurally. Keeping the model's paragraphing
            # is better than dropping text, so take what came back.
            woven_by_group.append(result.paragraphs or group)

    out_items: list[tuple[str, str]] = []
    for kind, payload in layout:
        if kind == "h":
            out_items.append(("h", payload))
        else:
            out_items.extend(("p", text) for text in woven_by_group[payload])

    out_paragraphs = [text for kind, text in out_items if kind == "p"]
    woven_text = "\n\n".join(out_paragraphs)
    measured = _measure(woven_text)
    spread = _spread(out_paragraphs)
    # How much of the source survived. A dip here means content was summarised
    # or cut -- the one failure mode a reader cannot see for themselves.
    source_words = len(whole.split())
    retained = round(len(woven_text.split()) / source_words, 3) if source_words else 1.0
    # The gate is the requested target, not a fixed band: asking for a light
    # weave and being handed an immersion one is a failure even though 70% is a
    # perfectly good number in itself. The tolerance is the grain's, because at
    # sentence grain the pass was never going to land as close as a phrase-grain
    # one -- see tolerance_for.
    if abs(measured - target) > max(0.28, tolerance_for(target, grain) * 2.2):
        raise ImportError_(
            f"the weave came out {measured:.0%} Spanish against a target of {target:.0%}, "
            "which is too far off to be the lesson that was asked for -- try again, "
            "or pick a different amount"
        )

    progress.check()
    progress.step("Writing the post-reading notes", done=done_units, total=total_units)
    # The weave is the expensive part and it is already done by now. If the
    # post-reading notes fail, the lesson is still a lesson -- a DNS blip at
    # this last step should not throw away eight minutes of work. So the
    # anchors degrade to empty and the article is written anyway.
    try:
        vocab, grammar = build_anchors(tutor, title=title, woven=woven_text, focus=focus)
    except Exception as exc:
        notes.append(f"post-reading notes could not be generated: {type(exc).__name__}: {exc}")
        vocab, grammar = [], []
    preview = textwrap.shorten(" ".join(paragraphs[0].split()), width=320, placeholder=" ...")

    # Place the lesson on the CEFR scale -- but only when the learner asked the
    # app to decide. If they picked a level, that is the level; overriding a
    # deliberate choice with an estimate would be the app arguing with them.
    if level is None and judge is not None and getattr(judge, "available", False):
        progress.step("Placing the lesson on the CEFR scale", done=done_units + 1, total=total_units)
        try:
            verdict = judge.rate_difficulty(title=title, sample=whole, spanish_ratio=measured)
            if verdict.ok and verdict.probabilities:
                best = max(verdict.probabilities, key=lambda k: verdict.probabilities[k])
                level = (verdict.labels.get(str(best)) or "").split(" - ")[0].strip() or None
            # The register comes from the same call as the level: one state,
            # two typed questions, no extra round trip.
            judged = (verdict.choice_of("register") or "").strip().lower()
            register = judged if judged in registers.CODES else None
        except Exception as exc:  # a missing level must not fail an import
            notes.append(f"level estimate failed: {type(exc).__name__}")

    # The slug's digest identifies the source: the URL for a fetch, the text for a
    # paste -- which has no URL -- and whatever the caller names for a lesson with
    # no page behind it at all. Two lessons from two sources must not be one.
    source_id = source_id or final_url or f"pasted:{hashlib.sha1((text or '').encode('utf-8')).hexdigest()}"
    base_slug = slugify(title, source_id)
    markdown = assemble_markdown(
        title=title, byline=extracted.get("byline"), url=final_url, preview=preview,
        items=out_items, vocab=vocab, grammar=grammar, focus=focus, level=level, register=register,
        weave=grain, target=target, requested_level=requested_level,
        source_note=source_label or ("pasted text" if pasted else None),
    )

    settings.library_dir.mkdir(parents=True, exist_ok=True)
    # Never overwrite what is already there: the copy on disk might be the one
    # the reader has corrected by hand. A repeated import gets a suffixed name,
    # which is what the architecture notes have claimed all along -- the code did
    # not do it, and a paste makes the collision easy to hit, since two pastes can
    # carry the same title.
    path = settings.library_dir / f"{base_slug}.md"
    counter = 2
    while path.exists():
        path = settings.library_dir / f"{base_slug}-{counter}.md"
        counter += 1
    slug = path.stem
    path.write_text(markdown, encoding="utf-8")

    progress.step("Checking the lesson reads correctly", done=total_units, total=total_units)
    # Read back what was written, with the same parser the corpus uses. If it
    # does not survive that round trip, the user should hear about it now.
    article = parse_article(markdown, fallback_slug=slug)
    if article is None:
        path.unlink(missing_ok=True)
        raise ImportError_("the generated lesson did not parse back cleanly; nothing was saved")

    stats = article.stats()
    return {
        "slug": slug,
        "title": article.title,
        "author": article.author,
        "url": final_url,
        "path": str(path),
        "level": article.level,
        "register": article.register,
        "weave": grain.code,
        "weave_name": grain.name,
        "focus": [f["es"] for f in focus],
        "spanish_ratio": stats["spanish_ratio"],
        "spread": round(spread, 3),
        "retained": retained,
        "words": stats["words"],
        "paragraphs": stats["paragraphs"],
        "anchors": {"vocab": len(article.vocab), "grammar": len(article.grammar)},
        "notes": notes,
        "headings": heading_count,
        "requested": {
            "level": requested_level.code,
            "level_name": requested_level.name,
            "target_ratio": round(target, 3),
        },
        "level_estimated": requested_level.code == "auto",
        "reused": reused,
    }
