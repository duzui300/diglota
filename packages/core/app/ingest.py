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
import json
import logging
import re
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from .diglot import parse_article, parse_spans
from .fetch import (MIN_SOURCE_WORDS, MIN_WRITING_WORDS, FetchError, blocks_from_text,
                    extract_article, fetch_url, split_paragraphs)
from .languages import DEFAULT_PAIR, LanguagePair, get_pair_or_default
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
from .llm import LLMError, LLMResult, _strip_thinking
from .tutor import SYSTEM, Tutor, cache_key

log = logging.getLogger("diglot.ingest")

# Words per weaving call. Local models stay compact at 420 words; cloud
# endpoints pack 600 words per call (fits cleanly inside 8k output caps while
# reducing round-trips and keeping good discourse continuity).
CHUNK_WORDS = 420
CLOUD_CHUNK_WORDS = 520

# The amount of Spanish when nobody has asked for anything in particular --
# the middle of the corpus's own range, and the "Balanced" preset. The learner
# picks the real number; see app/levels.py.
TARGET_RATIO = 0.38

PARAGRAPH_MARK = "@@@"
_INLINE_GLOSS_RE = re.compile(r"\(\*[^*]+?\*\)")


def _client_model(tutor: Any) -> str:
    return str(getattr(getattr(tutor, "client", None), "model", "") or "").strip()


def _sample_text(text: str, max_chars: int) -> str:
    """Sample beginning, middle, and end of long text instead of truncating to the opening."""
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.5)
    mid = int(max_chars * 0.25)
    tail = max_chars - head - mid
    mid_start = max(head, (len(text) - mid) // 2)
    return (
        text[:head].rstrip()
        + "\n\n[...]\n\n"
        + text[mid_start : mid_start + mid].strip()
        + "\n\n[...]\n\n"
        + text[-tail:].lstrip()
    )


def _prioritize_taught(items: Sequence[str], text: str, *, limit: int = 50) -> tuple[list[str], int]:
    """Deduplicate already-taught items, ranking ones overlapping the text first."""
    unique = list(dict.fromkeys(item.strip() for item in items if item and item.strip()))
    if len(unique) <= limit:
        return unique, 0
    lowered = text.lower()
    in_text: list[str] = []
    other: list[str] = []
    for item in unique:
        tokens = [w for w in re.findall(r"[A-Za-zÀ-ÿ]+", item.lower()) if len(w) >= 4]
        if tokens and any(tok in lowered for tok in tokens):
            in_text.append(item)
        else:
            other.append(item)
    chosen = (in_text + other)[:limit]
    return chosen, max(0, len(unique) - len(chosen))


class _UsageCounter:
    """Thread-safe accumulator for LLM token usage and cache hits across an import."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.reasoning_tokens = 0
        self.cached_tokens = 0
        self.llm_calls = 0
        self.cache_hits = 0

    def add(self, result: Any) -> None:
        if not isinstance(result, LLMResult):
            return
        with self._lock:
            self.prompt_tokens += int(result.prompt_tokens or 0)
            self.completion_tokens += int(result.completion_tokens or 0)
            self.reasoning_tokens += int(result.reasoning_tokens or 0)
            self.cached_tokens += int(result.cached_tokens or 0)
            self.llm_calls += 1

    def add_cache_hit(self) -> None:
        with self._lock:
            self.cache_hits += 1

    def to_dict(self) -> dict[str, int]:
        with self._lock:
            return {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "reasoning_tokens": self.reasoning_tokens,
                "cached_tokens": self.cached_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens,
                "llm_calls": self.llm_calls,
                "cache_hits": self.cache_hits,
            }


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
    weave: Weave | str | None = None, *, model: str = "", pair: LanguagePair | None = None,
) -> str:
    """Identify a woven passage by everything that determines its output.

    Keying on passage, focus list, amount, level, grain, and model means a retry
    or resumed import reuses good passages while switching to a stronger model
    never resurrects an earlier weak weave.
    """
    digest = hashlib.sha256()
    digest.update(chunk.encode("utf-8"))
    digest.update(b"|")
    digest.update("|".join(sorted(item.get("es", "") for item in focus)).encode("utf-8"))
    digest.update(f"|{target:.3f}|{level.code}|{resolve_weave(weave).code}".encode("utf-8"))
    if model:
        digest.update(f"|{model}".encode("utf-8"))
    return cache_key("weave", digest.hexdigest()[:24], pair=pair)


def focus_cache_key(
    text: str, level: Level, *, model: str = "", pair: LanguagePair | None = None,
) -> str:
    """Cache key for article focus-vocabulary selection so retries keep the same focus list."""
    digest = hashlib.sha256()
    digest.update(text.encode("utf-8"))
    digest.update(f"|{level.code}|{model}".encode("utf-8"))
    return cache_key("focus", digest.hexdigest()[:24], pair=pair)


def anchors_cache_key(
    woven: str, focus: list[dict[str, str]], *, model: str = "", pair: LanguagePair | None = None,
) -> str:
    """Cache key for post-reading vocabulary and grammar anchors."""
    digest = hashlib.sha256()
    digest.update(woven.encode("utf-8"))
    digest.update(b"|")
    digest.update("|".join(sorted(item.get("es", "") for item in focus)).encode("utf-8"))
    if model:
        digest.update(f"|{model}".encode("utf-8"))
    return cache_key("anchors", digest.hexdigest()[:24], pair=pair)


# --------------------------------------------------------------------------- #
# Vocabulary selection
# --------------------------------------------------------------------------- #


def choose_focus_vocabulary(
    tutor: Tutor, *, title: str, text: str, count: int = 6, level: Level | None = None,
    already_taught: Sequence[str] | None = None, pair: LanguagePair | None = None,
    usage: _UsageCounter | None = None,
) -> list[dict[str, str]]:
    """Pick the phrases the lesson will teach, before any weaving happens."""
    resolved = pair or getattr(tutor, "pair", None) or DEFAULT_PAIR
    target_name = resolved.target.name
    base_name = resolved.base.name
    level = level or resolve_level(None)
    level_rules = (
        f"The learner is at roughly {level.code} ({level.name}). Choose words and "
        f"constructions at that level: {level.vocabulary}"
        if level.code != "auto" else
        f"Choose words and constructions at roughly the level of the article: {level.vocabulary}"
    )
    avoid_block = ""
    if already_taught:
        chosen_avoid, omitted = _prioritize_taught(already_taught, text, limit=50)
        if chosen_avoid:
            listed = "\n".join(f"- {item}" for item in chosen_avoid)
            more = f"\n(and {omitted} more, also already taught)" if omitted else ""
            avoid_block = f"""
The reader has already been taught these -- they are in lessons they have, or in their
own deck. Do not choose any of them again. Pick the words and constructions *this*
passage actually uses that are not on the list: the point of a new lesson is that it
adds something rather than teaching the same few phrases for the tenth time.

{listed}{more}
"""
    sample = _sample_text(text, 5000)
    prompt = f"""You are choosing the vocabulary to teach in a diglot lesson built from this article.

Choose exactly {count} {target_name} words or short phrases that:
- recur naturally in a text like this, so they can appear several times;
- are useful to an {base_name} speaker who reads but does not yet write {target_name};
- include at least two multi-word constructions (a periphrastic verb, a
  conjunctive phrase, a reflexive verb) rather than only single nouns.
- appear in the text below: pick what this passage actually says, not what a text
  like it might say.

{level_rules}
Do not repeat what is essentially the same item twice: if you choose
"relacionado con", do not also return "relacionada con" and "estar relacionado con".
{avoid_block}
Return JSON:
{{
  "items": [
    {{
      "es": "the {target_name}, in the form it would appear in a sentence",
      "en": "its {base_name} meaning, 1-4 words",
      "kind": "one of: verb, noun, adjective, connector, phrase",
      "why": "why this is worth teaching here, at most 12 words"
    }}
  ],
  "level": "the CEFR level of the {target_name} you actually chose, A1-C1"
}}

Title: {title}

---
{sample}
---"""
    data, llm_res = tutor.client.complete_json(prompt, system=resolved.system_prompt(), max_tokens=4000)
    if usage is not None:
        usage.add(llm_res)
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


def _ratio_of(text: str, *, pair: LanguagePair | None = None) -> float:
    """Share of words the segmenter reads as target language, for one passage."""
    if not text.strip():
        return 0.0
    resolved = pair or DEFAULT_PAIR
    target_code = resolved.target.code
    target_n = base_n = 0
    for span in parse_spans(" ".join(text.split()), pair=resolved):
        if span.lang == target_code:
            target_n += resolved.target.count_units(span.text)
        else:
            base_n += resolved.base.count_units(span.text)
    total = target_n + base_n
    return target_n / total if total else 0.0


def _measure(text: str, *, pair: LanguagePair | None = None) -> float:
    """Share of words the segmenter reads as target language."""
    return _ratio_of(text, pair=pair)


def _prose_word_count(text: str) -> int:
    """Word count excluding inline parenthesized glosses ``(*...*)``."""
    stripped = _INLINE_GLOSS_RE.sub("", text or "")
    return len(stripped.split())


# How unevenly the target language may be spread before a chunk is rewoven.
SPREAD_LIMIT = 0.62
SENTENCE_SPREAD_LIMIT = 0.86
MIN_PARAGRAPH_WORDS = 30


_QUOTE_SPAN_RE = re.compile(r'["“][^"”]{8,}["”]')


def _is_quote_heavy(text: str) -> bool:
    """True when half or more of a paragraph is direct quoted speech (left in English by rule)."""
    if not text:
        return False
    quoted = sum(len(m.group(0)) for m in _QUOTE_SPAN_RE.finditer(text))
    return quoted >= len(text) * 0.45


def _sentence_count(text: str) -> int:
    parts = [s for s in re.split(r"[.!?。！？]+(?:\s|$)", text.strip()) if s.strip()]
    return len(parts)


def _spread(
    paragraphs: list[str],
    *,
    floor: int = MIN_PARAGRAPH_WORDS,
    pair: LanguagePair | None = None,
    sentence_mode: bool = False,
) -> float:
    """The gap between the least and most target-language paragraph.

    In whole-sentence mode, single-sentence paragraphs and direct-quote
    paragraphs are naturally 0% or 100% target language and are excluded so
    journalistic prose with short ledes or block quotes is not needlessly
    rewritten.
    """
    eligible: list[str] = []
    for p in paragraphs:
        words = len(p.split())
        if words < floor:
            continue
        if sentence_mode and (_sentence_count(p) < 2 or _is_quote_heavy(p)):
            continue
        eligible.append(p)
    ratios = [_ratio_of(p, pair=pair) for p in eligible]
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
    pair: LanguagePair | None = None, usage: _UsageCounter | None = None,
    progress: Any = None, chunk_label: str = "",
) -> WovenChunk:
    """Weave one chunk, measuring the result, retrying with feedback, and keeping the best attempt."""
    resolved = pair or getattr(tutor, "pair", None) or DEFAULT_PAIR
    target_name = resolved.target.name
    level = level or resolve_level(None)
    grain = resolve_weave(weave)
    tolerance = tolerance_for(target, grain)
    spread_limit = SPREAD_LIMIT if grain.mixes_within_a_sentence else SENTENCE_SPREAD_LIMIT
    wanted = [p for p in chunk.split("\n") if p.strip()]
    source_words = len(chunk.split())
    # Give enough initial headroom for always-on reasoning models even when
    # choose_focus_vocabulary hit cache (non-reasoning models ignore extra
    # ceiling at zero cost; providers with 8k caps auto-adapt on HTTP 400).
    budget = min(20000, max(12000, int(source_words * 10.0) + 4000))

    result = WovenChunk(paragraphs=[])
    feedback: str | None = None
    ask = target

    best_score: tuple[int, int, float, float] | None = None
    best_snapshot: tuple[list[str], float, float, int] | None = None
    consecutive_llm_errors = 0
    last_llm_error: LLMError | None = None

    def _report(detail: str) -> None:
        if progress is not None and chunk_label:
            progress.step(f"Weaving {target_name} into the text", detail=f"{chunk_label} ({detail})")

    for attempt in (1, 2, 3):
        if progress is not None:
            progress.check()
        if attempt > 1:
            _report(f"attempt {attempt}/3")
        prompt = _weave_prompt(chunk=chunk, focus=focus, target=ask, level=level,
                               weave=grain, feedback=feedback)
        try:
            response = tutor.client.complete(
                prompt,
                system=resolved.system_prompt(),
                max_tokens=budget,
                temperature=0.5,
                effort="low",
                attempts=2,
                stream=True,
                cancel_check=(progress.check if progress is not None else None),
            )
            if usage is not None:
                usage.add(response)
            consecutive_llm_errors = 0
        except LLMError as exc:
            last_llm_error = exc
            consecutive_llm_errors += 1
            result.notes.append(f"attempt {attempt}: {exc}")
            log.warning("weave %s attempt %d failed: %s", chunk_label or "chunk", attempt, exc)
            if getattr(exc, "fatal", False):
                raise ImportError_(f"weave model rejected the request: {exc}") from None
            feedback = None
            if consecutive_llm_errors >= 2:
                # Two consecutive transport/model failures on the same chunk: stop spinning.
                break
            continue
        paragraphs = _split_paragraphs(response.text)
        if not paragraphs:
            feedback = "The output was empty. Return the woven passage."
            result.notes.append(f"attempt {attempt}: empty output")
            continue

        if len(paragraphs) < len(wanted) and len(wanted) > 1:
            repaired = _redistribute(paragraphs, wanted)
            result.notes.append(
                f"attempt {attempt}: {len(paragraphs)} paragraphs for {len(wanted)} input "
                f"paragraphs, redistributed to {len(repaired)}"
            )
            paragraphs = repaired

        blob = "\n\n".join(paragraphs)
        ratio = _measure(blob, pair=resolved)
        sentence_mode = not grain.mixes_within_a_sentence
        spread = _spread(
            paragraphs,
            floor=(55 if sentence_mode else MIN_PARAGRAPH_WORDS),
            pair=resolved,
            sentence_mode=sentence_mode,
        )
        output_words = _prose_word_count(blob)
        kept = output_words / source_words if source_words else 1.0

        truncated = kept < MIN_LENGTH_RATIO
        amount_off = abs(ratio - target) > tolerance
        lumpy = spread > spread_limit

        score = (
            0 if truncated else 1,
            1 if len(paragraphs) == len(wanted) else 0,
            -abs(ratio - target),
            -max(0.0, spread - spread_limit),
        )
        if best_score is None or score > best_score:
            best_score = score
            best_snapshot = (paragraphs, ratio, spread, attempt)

        result.attempts = attempt
        result.ratio = ratio
        result.spread = spread
        result.paragraphs = paragraphs

        if len(paragraphs) != len(wanted):
            result.notes.append(
                f"attempt {attempt}: {len(paragraphs)} paragraphs for {len(wanted)} input paragraphs"
            )

        if not truncated and not amount_off and not lumpy:
            return result

        problems: list[str] = []
        if truncated:
            problems.append(
                f"You returned {output_words} words for a passage of {source_words}. "
                "Every sentence must be present and complete -- translate phrases within the "
                "sentences, but do not shorten, summarise or drop any of them."
            )
            result.notes.append(f"attempt {attempt}: kept only {kept:.0%} of the words, retrying")
        if amount_off:
            error = ratio - ask
            ask = round(min(0.85, max(0.06, ask - error)), 3)
            direction = "less" if error > 0 else "more"
            problems.append(
                f"It came out {ratio:.0%} {target_name} and the target is {target:.0%}. "
                f"Use noticeably {direction} {target_name} this time -- aim for about {ask:.0%}."
            )
        if lumpy:
            problems.append(
                f"The {target_name} is unevenly spread -- some paragraphs are {spread:.0%} apart in how "
                "much they carry. Keep the amount the same but distribute it evenly: a phrase in "
                f"most sentences, rather than a run of untouched English then a whole {target_name} paragraph."
            )
        feedback = " ".join(problems)
        if not truncated:
            result.notes.append(
                f"attempt {attempt}: {ratio:.0%} {target_name}"
                + (f", {spread:.0%} spread" if lumpy else "")
                + (f", retrying at {ask:.0%}" if amount_off else ", retrying")
            )

    # Keep the best-scoring attempt across retries instead of blindly taking an oscillated last run.
    if best_snapshot is not None:
        best_paragraphs, best_ratio, best_spread, best_attempt = best_snapshot
        if best_attempt != result.attempts:
            result.notes.append(f"kept best attempt ({best_attempt} at {best_ratio:.0%})")
        result.paragraphs = best_paragraphs
        result.ratio = best_ratio
        result.spread = best_spread

    if not result.paragraphs and wanted:
        # Only fall back to smaller pieces when chunk size may have caused the failure;
        # abort fast if consecutive pieces fail with LLM errors so a broken endpoint
        # never burns dozens of retries while the UI appears stuck.
        if len(wanted) > 1:
            pieces = wanted
            result.notes.append("falling back to paragraph-by-paragraph weaving")
            _report(f"splitting into {len(pieces)} paragraphs")
        elif last_llm_error is not None and consecutive_llm_errors >= 2:
            raise ImportError_(f"weave model failed: {last_llm_error}") from None
        else:
            pieces = [s for s in re.split(r"(?<=[.!?])\s+", wanted[0]) if s.strip()]
            result.notes.append(f"falling back to sentence-by-sentence weaving ({len(pieces)} sentences)")
            _report(f"splitting into {len(pieces)} sentences")
        if not pieces:
            return result

        salvaged: list[str] = []
        piece_errors = 0
        for idx, piece in enumerate(pieces, start=1):
            if progress is not None:
                progress.check()
            prompt = _weave_prompt(chunk=piece, focus=focus, target=target, level=level, weave=grain)
            piece_budget = min(5000, max(1500, int(len(piece.split()) * 5.0) + 800))
            try:
                response = tutor.client.complete(
                    prompt,
                    system=resolved.system_prompt(),
                    max_tokens=piece_budget,
                    temperature=0.5,
                    effort="low",
                    attempts=1,
                    stream=True,
                    cancel_check=(progress.check if progress is not None else None),
                )
                if usage is not None:
                    usage.add(response)
                piece_errors = 0
            except LLMError as exc:
                piece_errors += 1
                result.notes.append(f"piece {idx} failed: {exc}")
                log.warning("weave %s piece %d/%d failed: %s", chunk_label or "chunk", idx, len(pieces), exc)
                if getattr(exc, "fatal", False) or piece_errors >= 2:
                    raise ImportError_(f"weave model failed: {exc}") from None
                salvaged.append(piece)
                continue
            parts = _split_paragraphs(response.text)
            salvaged.append(parts[0] if parts else piece)

        if len(wanted) > 1:
            result.paragraphs = salvaged
        else:
            result.paragraphs = [" ".join(salvaged)]
        result.ratio = _measure("\n\n".join(result.paragraphs), pair=resolved)

    return result


def _split_paragraphs(text: str) -> list[str]:
    text = _strip_thinking(text or "")
    text = re.sub(r"^\s*```[a-z]*\s*$", "", text, flags=re.M).strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and isinstance(parsed.get("paragraphs"), list):
                items = [re.sub(r"\s+", " ", str(p)).strip() for p in parsed["paragraphs"] if str(p).strip()]
                if items:
                    return items
        except json.JSONDecodeError:
            pass
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


def build_anchors(
    tutor: Tutor, *, title: str, woven: str, focus: list[dict[str, str]],
    already_explained: Sequence[str] | None = None,
    pair: LanguagePair | None = None, usage: _UsageCounter | None = None,
) -> tuple[list[dict], list[dict]]:
    """Generate the post-reading Recycled Vocabulary Box and Grammar Breakdown."""
    resolved = pair or getattr(tutor, "pair", None) or DEFAULT_PAIR
    target_name = resolved.target.name
    base_name = resolved.base.name
    avoid_block = ""
    if already_explained:
        chosen_avoid, _ = _prioritize_taught(already_explained, woven, limit=40)
        if chosen_avoid:
            listed = "\n".join(f"- {item}" for item in chosen_avoid)
            avoid_block = f"""
These constructions have already been explained in the reader's other lessons. Do not
write about them again unless this lesson is genuinely the clearest place to meet them:
pick the grammar this passage actually exercises that is not on this list.

{listed}
"""
    sample = _sample_text(woven, 6000)
    prompt = f"""Produce the post-reading anchors for this diglot lesson ({base_name} prose with {target_name} woven into it).

1. "vocab": for each focus item, the headword, its {base_name} gloss, and 2-3 examples
   of it **copied verbatim from the lesson text below**, each with a short {base_name}
   translation in italics in parentheses.
2. "grammar": exactly 3 notes on the grammar the lesson actually exercises.
   Each note names a construction, quotes a real sentence from the lesson, and
   explains in 2-4 sentences of {base_name} why it works that way and what a learner
   gets wrong about it. The sentence quoted must be in the lesson text below.
{avoid_block}
Return JSON:
{{
  "vocab": [
    {{"term": "headword / inflected forms", "gloss": "to ...", "examples": ["{target_name} sentence ({base_name} translation)", "..."]}}
  ],
  "grammar": [
    {{"title": "Name of the construction", "example": "the {target_name} sentence from the lesson", "explanation": "2-4 sentences of {base_name}"}}
  ]
}}

The lesson was built around these focus items:
{chr(10).join(f"- {i['es']} ({i.get('en','')})" for i in focus)}

Title: {title}

---
{sample}
---"""
    data, llm_res = tutor.client.complete_json(
        prompt, system=resolved.system_prompt(), max_tokens=6000, effort="low",
    )
    if usage is not None:
        usage.add(llm_res)
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
    already_taught: Sequence[str] | None = None,
    already_explained: Sequence[str] | None = None,
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
        already_taught=already_taught, already_explained=already_explained,
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
    already_taught: Sequence[str] | None = None,
    already_explained: Sequence[str] | None = None,
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
    pair = get_pair_or_default(getattr(settings, "active_pair", None))
    model_name = _client_model(tutor)
    is_local_model = bool(getattr(getattr(tutor, "client", None), "is_local", False))
    has_api_key = bool(getattr(getattr(tutor, "client", None), "api_key", ""))
    # A localhost endpoint *without* an API key is typically single-slot Ollama/LM Studio;
    # a localhost gateway *with* an API key (e.g. Magpie/LiteLLM) forwards to cloud APIs.
    single_slot_local = is_local_model and not has_api_key
    chunk_limit = CHUNK_WORDS if single_slot_local else CLOUD_CHUNK_WORDS
    groups, layout = _chunk_pieces(pieces, max_words=chunk_limit)
    usage = _UsageCounter()

    # Everything after the page is fetched is known up front, so the bar can be
    # honest from here on: one unit for vocabulary, one per passage, one each
    # for the notes and the read-back check.
    total_units = len(groups) + 3
    progress.step(f"Choosing the vocabulary to teach ({len(groups)} passages to weave)",
                  done=2, total=total_units)
    f_key = focus_cache_key(whole, requested_level, model=model_name, pair=pair)
    cached_focus = cache.get(f_key) if cache is not None else None
    if isinstance(cached_focus, list) and cached_focus:
        focus = cached_focus
        usage.add_cache_hit()
    else:
        try:
            focus = choose_focus_vocabulary(
                tutor, title=title, text=whole, level=requested_level,
                already_taught=already_taught, pair=pair, usage=usage,
            )
        except TypeError:
            # Preserve monkeypatched 4-arg test stubs for choose_focus_vocabulary.
            focus = choose_focus_vocabulary(
                tutor, title=title, text=whole, level=requested_level,
                already_taught=already_taught,
            )
        if cache is not None and focus:
            cache.put(f_key, focus)
    level: str | None = requested_level.code if requested_level.code != "auto" else None
    register: str | None = None

    progress.check()
    target_name = pair.target.name
    progress.step(f"Weaving {target_name} into the text (aiming for {target:.0%})",
                  done=2, total=total_units)

    def weave_one(index: int, group: list[str]) -> WovenChunk:
        """Weave a passage, reusing an earlier identical one for the same model."""
        chunk = "\n".join(group)
        key = weave_cache_key(chunk, focus, target, requested_level, grain, model=model_name, pair=pair)
        if cache is not None:
            hit = cache.get(key)
            if hit:
                usage.add_cache_hit()
                return WovenChunk.from_dict(hit)
        label = f"passage {index + 1} of {len(groups)}"
        try:
            result = weave_chunk(
                tutor, chunk=chunk, focus=focus, target=target,
                level=requested_level, weave=grain, pair=pair, usage=usage,
                progress=progress, chunk_label=label,
            )
        except TypeError:
            # Preserve monkeypatched test stubs for weave_chunk.
            result = weave_chunk(
                tutor, chunk=chunk, focus=focus, target=target,
                level=requested_level, weave=grain,
            )
        if cache is not None and result.paragraphs:
            cache.put(key, result.to_dict())
        return result

    woven: list[WovenChunk] = [WovenChunk(paragraphs=[]) for _ in groups]
    done_units = 2

    def _record_chunk(index: int, result: WovenChunk) -> None:
        nonlocal done_units
        woven[index] = result
        done_units += 1
        progress.step(
            f"Weaving {target_name} into the text", done=done_units, total=total_units,
            detail=f"passage {done_units - 2} of {len(groups)}",
        )

    # Seed the provider's Prompt Prefix Cache with passage 0 before fanning out
    # remaining passages so chunks 1..N hit the cached static instruction + focus prefix.
    if groups:
        progress.check()
        progress.step(
            f"Weaving {target_name} into the text", done=done_units, total=total_units,
            detail=f"passage 1 of {len(groups)}",
        )
        # Fail fast if the very first passage fails fatally (broken model/endpoint/quota).
        _record_chunk(0, weave_one(0, groups[0]))

    remaining = list(enumerate(groups[1:], start=1))
    if remaining:
        max_workers = 1 if single_slot_local else min(3, len(remaining))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(weave_one, idx, group): idx for idx, group in remaining}
            try:
                for future in as_completed(futures):
                    progress.check()
                    idx = futures[future]
                    try:
                        chunk_result = future.result()
                    except ImportError_:
                        raise
                    except Exception as exc:
                        if type(exc).__name__ == "JobCancelled":
                            raise
                        log.warning("weave passage %d failed: %s: %s", idx + 1, type(exc).__name__, exc)
                        chunk_result = WovenChunk(paragraphs=[], notes=[f"{type(exc).__name__}: {exc}"])
                    _record_chunk(idx, chunk_result)
            except BaseException:
                for pending in futures:
                    pending.cancel()
                raise
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
    measured = _measure(woven_text, pair=pair)
    sentence_mode = not grain.mixes_within_a_sentence
    spread = _spread(
        out_paragraphs,
        floor=(55 if sentence_mode else MIN_PARAGRAPH_WORDS),
        pair=pair,
        sentence_mode=sentence_mode,
    )
    source_words = len(whole.split())
    retained = round(_prose_word_count(woven_text) / source_words, 3) if source_words else 1.0
    if abs(measured - target) > max(0.32, tolerance_for(target, grain) * 2.5):
        raise ImportError_(
            f"the weave came out {measured:.0%} {target_name} against a target of {target:.0%}, "
            "which is too far off to be the lesson that was asked for -- try again, "
            "or pick a different amount"
        )

    progress.check()
    progress.step("Writing the post-reading notes", done=done_units, total=total_units)
    a_key = anchors_cache_key(woven_text, focus, model=model_name, pair=pair)
    cached_anchors = cache.get(a_key) if cache is not None else None
    if isinstance(cached_anchors, dict) and ("vocab" in cached_anchors or "grammar" in cached_anchors):
        vocab = list(cached_anchors.get("vocab") or [])
        grammar = list(cached_anchors.get("grammar") or [])
        usage.add_cache_hit()
    else:
        try:
            try:
                vocab, grammar = build_anchors(
                    tutor, title=title, woven=woven_text, focus=focus,
                    already_explained=already_explained, pair=pair, usage=usage,
                )
            except TypeError:
                vocab, grammar = build_anchors(
                    tutor, title=title, woven=woven_text, focus=focus,
                    already_explained=already_explained,
                )
            if cache is not None and (vocab or grammar):
                cache.put(a_key, {"vocab": vocab, "grammar": grammar})
        except Exception as exc:
            notes.append(f"post-reading notes could not be generated: {type(exc).__name__}: {exc}")
            vocab, grammar = [], []
    preview = textwrap.shorten(" ".join(paragraphs[0].split()), width=320, placeholder=" ...")

    # Place the lesson on the CEFR scale and classify its register. If the
    # learner picked a level explicitly, never override their choice -- still
    # query Jev when available so the register is judged rather than guessed.
    if judge is not None and getattr(judge, "available", False):
        if level is None:
            progress.step("Placing the lesson on the CEFR scale", done=done_units + 1, total=total_units)
        try:
            verdict = judge.rate_difficulty(title=title, sample=whole, spanish_ratio=measured)
            if level is None and verdict.ok and verdict.probabilities:
                best = max(verdict.probabilities, key=lambda k: verdict.probabilities[k])
                level = (verdict.labels.get(str(best)) or "").split(" - ")[0].strip() or None
            judged = (verdict.choice_of("register") or "").strip().lower()
            register = judged if judged in registers.CODES else None
        except Exception as exc:  # a missing level/register must not fail an import
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
    article = parse_article(markdown, fallback_slug=slug, pair=pair)
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
        "usage": usage.to_dict(),
    }
