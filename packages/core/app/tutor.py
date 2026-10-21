"""The tutor: everything the chat model is asked to do.

The division of labour in this app is deliberate. The chat model *generates*
-- glosses, questions, corrections, diglot weaves -- and Jev *judges* (see
:mod:`app.judge`). Asking a chat model to grade its own output would be asking
it to do the one thing it is worst at; asking Jev to write a paragraph of
feedback would be asking it to do the one thing it does not do at all.

Prompts here are written to demand JSON and to pin the register: the learner is
an English speaker reading Spanish, so glosses are English, explanations are
English, and everything the learner has to *read* in Spanish is kept to the
vocabulary the article already uses.
"""

from __future__ import annotations

import logging
from typing import Any

from .diglot import Article
from .llm import LLMClient, LLMError
from .writing import BY_MODE, MODES, keep_notes

log = logging.getLogger("diglot.tutor")

SYSTEM = (
    "You are the tutor inside a diglot reader: an app where an English speaker "
    "learns Spanish by reading articles that weave Spanish into English prose. "
    "You are precise, you never pad, and you always answer in the JSON shape "
    "you are asked for. Glosses and explanations are in English; target-language "
    "material stays in Spanish."
)

# Timeouts by whether a human is waiting on the answer.
#
# A word gloss is interactive: the reader has clicked and is looking at a
# spinner, so it gets a short leash and fails fast to "the tutor could not be
# reached" rather than grinding through three long attempts. Exercises are
# asked for explicitly and can take a while. The weave runs in a background job
# nobody is watching, so it keeps the client default.
GLOSS_TIMEOUT = 20
EXPLAIN_TIMEOUT = 60
BATCH_TIMEOUT = 150

# Prompt identity, for cache keys.
#
# Every AI result is cached against a hash of its *input*, which says nothing
# about the prompt that produced it. So improving a prompt left every already
# cached answer serving the old one, silently and forever -- the explain prompt
# was rewritten to be a quarter the length and anything explained beforehand
# kept returning the long version. Bump the relevant number whenever its prompt
# changes; the stale entries are simply ignored and regenerated on demand.
PROMPT_VERSION = {
    "gloss": 1,
    "explain": 2,      # rewritten: 60-word bulleted shape, was 2-4 paragraphs
    "translate": 1,
    "quiz": 1,
    "drills": 1,
    "feedback": 1,
    "writing": 2,      # 2: notes placed on the draft, not only a block of feedback
    "suggest": 1,
    # Woven passages are cached so a retry or a restart does not redo work that
    # already succeeded -- a twelve-chunk weave is ten minutes.
    "weave": 3,        # 3: distribution rule no longer contradicts the density note
    "anchors": 1,
    # The reader's own writing, into English, before it can be woven.
    "english": 1,
}


def cache_key(feature: str, *parts: str) -> str:
    """A cache key that changes when the prompt behind it changes."""
    return ":".join([feature, f"v{PROMPT_VERSION.get(feature, 1)}", *parts])


def _article_context(article: Article, *, paragraphs: int = 6) -> str:
    """A trimmed plain-text rendering of the article, for use as prompt context."""
    chunks: list[str] = []
    for block in article.blocks:
        if block.kind == "h":
            chunks.append(f"## {block.plain}")
        elif block.kind == "p":
            chunks.append(block.plain)
        if len(chunks) >= paragraphs + 4:
            break
    return "\n\n".join(chunks)[:6000]


def _trim_answer(text: str, *, max_words: int = 130) -> str:
    """Backstop for an answer that ignores its length instruction.

    Deliberately loose: the prompt asks for 60 words, so this only fires on a
    genuine ramble, and it cuts at a line boundary first because the answer is
    bullets. Trimming at a boundary rather than mid-clause means a caught answer
    still reads as a shorter version of itself rather than as a truncated one.
    """
    if not text or len(text.split()) <= max_words:
        return text

    kept: list[str] = []
    used = 0
    for line in text.splitlines():
        words = len(line.split())
        if kept and used + words > max_words:
            break
        kept.append(line)
        used += words

    trimmed = "\n".join(kept).strip() or text
    # A single bullet can still be a paragraph in disguise; cut that at the last
    # sentence that fits.
    while len(trimmed.split()) > max_words:
        head, sep, _ = trimmed.rpartition(". ")
        if not sep or head == trimmed:
            break
        trimmed = head + "."
    return trimmed


class Tutor:
    def __init__(self, client: LLMClient) -> None:
        self.client = client

    # -- word lookup ------------------------------------------------------ #

    def gloss_word(self, *, word: str, sentence: str, article_title: str) -> dict[str, Any]:
        """The dictionary entry behind a click in the reader.

        Consulting the model per word is more expensive than shipping a
        dictionary, and much better: it can say that *se volverá* is
        ``volverse`` in the future tense rather than treating it as an unknown
        token, and it can explain the word *as used in this sentence*.
        """
        prompt = f"""A learner clicked the Spanish word or phrase "{word}" while reading "{article_title}".

The sentence it appeared in:
{sentence}

Return JSON with exactly these keys:
{{
  "lemma": "the dictionary form (infinitive for verbs, singular masculine for nouns/adjectives)",
  "display": "the form as it appears in the sentence",
  "pos": "part of speech, one of: noun, verb, adjective, adverb, pronoun, preposition, conjunction, phrase, interjection",
  "gloss": "a short English translation, 1-4 words",
  "sense": "which sense of the word this is, in English, at most 12 words",
  "note": "one or two sentences of English explanation aimed at a learner: why it takes this form here, what to watch out for. Empty string if there is nothing worth saying.",
  "example": "a short, different Spanish sentence using the same word in the same sense",
  "example_en": "its English translation",
  "related": ["up to 3 other forms of the same word worth knowing, e.g. infinitive or key conjugations"]
}}"""
        data, _ = self.client.complete_json(prompt, system=SYSTEM, max_tokens=900,
                                           timeout=GLOSS_TIMEOUT, attempts=2)
        return _as_dict(data)

    # -- explanation ------------------------------------------------------ #

    def explain(self, *, text: str, sentence: str, question: str | None = None) -> str:
        """A short explanation of a phrase, optionally answering a question.

        Length is the whole design problem here. An earlier version asked for
        "2-4 short paragraphs" and got a philology lecture: the complete
        morphology of the head word, its regional variants, a note on a typo in
        the article, and a restatement of the sentence. It appeared in a 344px
        popover, mid-read, where nobody is going to read 300 words.

        So the answer is now a fixed, small shape -- what it means, the one
        grammar point that earns its place, the mistake worth avoiding -- and
        every part of the prompt is there to stop the model volunteering the
        rest.
        """
        prompt = f"""A learner is reading a Spanish article and asked about this passage:

"{text}"

In this sentence:
{sentence}

{("Their question: " + question) if question else "Explain what it means here, and the grammar that matters."}

Answer in at most 60 words. Use exactly this shape, and drop a line rather than pad it:

- **Means:** what it means in this sentence, in a few words
- **Grammar:** the one construction worth knowing -- only if there is something surprising
- **Watch out:** the mistake a learner actually makes -- only if there is one

Rules: no preamble, do not restate the question or the sentence, do not explain words the learner did not ask about, do not list every word's morphology, do not add a closing summary. One short clause per line."""
        result = self.client.complete(prompt, system=SYSTEM, max_tokens=700, effort="low",
                                      timeout=EXPLAIN_TIMEOUT, attempts=2)
        return _trim_answer(result.text.strip())

    # -- comprehension quiz ----------------------------------------------- #

    def quiz(self, article: Article) -> list[dict[str, Any]]:
        """Comprehension questions for an article, asked in Spanish.

        Questions are in Spanish because answering them is reading practice;
        the expected answer is given in Spanish too, so the same item can be
        graded by Jev and shown as a model answer. Vocabulary comes from the
        article, so the question is never harder than the text.
        """
        prompt = f"""Here is an article from a Spanish-learning diglot reader.

Title: {article.title}

---
{_article_context(article, paragraphs=14)}
---

Write exactly 5 reading-comprehension questions **in Spanish**, using only vocabulary that appears in the article. Ask about the argument, not trivia.

Return JSON:
{{
  "questions": [
    {{
      "question": "the question, in Spanish",
      "expected": "a good answer, in Spanish, 1-2 sentences",
      "hint": "a 3-8 word English hint pointing at where in the article the answer is"
    }}
  ]
}}"""
        data, _ = self.client.complete_json(prompt, system=SYSTEM, max_tokens=1800, effort="medium",
                                           timeout=BATCH_TIMEOUT, attempts=2)
        questions = _as_dict(data).get("questions") or []
        return [q for q in questions if isinstance(q, dict) and q.get("question")]

    # -- production drills ------------------------------------------------ #

    def translation_drills(self, article: Article) -> list[dict[str, Any]]:
        """English sentences to translate into Spanish, with reference answers.

        This is the production half of the app. Where the article already
        renders a sentence partly in Spanish, that Spanish is the reference; the
        rest are written to reuse the article's own vocabulary so the exercise
        tests recall rather than new material.
        """
        prompt = f"""Build translation practice from this diglot article.

Title: {article.title}

---
{_article_context(article, paragraphs=12)}
---

Choose 6 sentences that are worth being able to say in Spanish. Where the article already gives Spanish for an idea, use that Spanish as the reference translation, unchanged. Where it does not, write the Spanish yourself, reusing the article's own vocabulary and structures so the learner has already met every word.

Vary them: include at least one sentence using a focus construction the article bolds, and at least one that is short.

Return JSON:
{{
  "drills": [
    {{
      "en": "the English sentence to translate",
      "es": "the reference Spanish translation",
      "focus": "3-6 English words naming what this drill practises, e.g. 'future tense for predictions'",
      "vocabulary": ["2-4 Spanish words from the sentence the learner should have ready"]
    }}
  ]
}}"""
        data, _ = self.client.complete_json(prompt, system=SYSTEM, max_tokens=2000, effort="medium",
                                           timeout=BATCH_TIMEOUT, attempts=2)
        drills = _as_dict(data).get("drills") or []
        return [d for d in drills if isinstance(d, dict) and d.get("en") and d.get("es")]

    # -- cloze ------------------------------------------------------------ #

    def cloze_candidates(self, *, word: str, sentence: str) -> str | None:
        """A sentence from the article with the target word blanked out.

        Blanking is done in code, not by the model -- the model is only asked
        which surface form to blank, so the sentence the learner sees is
        verbatim from the article.
        """
        prompt = f"""In this Spanish sentence, the learner is studying the word "{word}":

{sentence}

Return JSON: {{"surface": "the exact substring of the sentence to replace with a blank -- the inflected form of {word} as it appears, including any attached pronoun. Empty string if the word does not appear."}}"""
        try:
            data, _ = self.client.complete_json(prompt, system=SYSTEM, max_tokens=400,
                                           timeout=GLOSS_TIMEOUT, attempts=1)
        except LLMError:
            return None
        surface = str(_as_dict(data).get("surface") or "").strip()
        if surface and surface in sentence:
            return sentence.replace(surface, "_____", 1)
        return None

    # -- feedback --------------------------------------------------------- #

    def translation_feedback(
        self, *, source: str, reference: str, attempt: str, verdict: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Turn a Jev verdict into specific, actionable correction.

        Jev supplies the judgment (see :mod:`app.judge`); this supplies the
        *why*. Passing the verdict in as context keeps the two consistent --
        the prose is asked to explain the scores, not to re-decide them.
        """
        scores = ""
        if verdict:
            checks = verdict.get("checks") or {}
            if checks:
                scores = "\n".join(f"- {k}: {v:.0%} confident yes" for k, v in checks.items())
            if verdict.get("score") is not None:
                scores += f"\n- overall quality: {verdict['score']:.1f} / {verdict.get('score_max', 4)}"

        prompt = f"""A learner translated this English sentence into Spanish.

English: {source}
Their Spanish: {attempt}
The article's own Spanish for this idea: {reference}

An automated judge scored it:
{scores or "(no scores available)"}

Return JSON:
{{
  "verdict": "one of: correct, close, wrong",
  "summary": "one sentence in English telling them how they did",
  "corrections": [
    {{"their_text": "the exact fragment they got wrong", "should_be": "what it should be", "why": "the rule, in one short English sentence"}}
  ],
  "better": "your own best Spanish rendering of the sentence",
  "praise": "one specific thing they got right, or empty string"
}}
Be accurate about what is actually wrong: do not invent errors, and do not flag a valid alternative wording as a mistake."""
        try:
            data, _ = self.client.complete_json(prompt, system=SYSTEM, max_tokens=1200, effort="medium",
                                           timeout=BATCH_TIMEOUT, attempts=2)
        except LLMError as exc:
            return {"verdict": "unknown", "summary": f"could not generate feedback: {exc}",
                    "corrections": [], "better": reference, "praise": ""}
        out = _as_dict(data)
        out.setdefault("corrections", [])
        return out

    # -- reader helpers --------------------------------------------------- #

    def writing_feedback(
        self, *, text: str, prompt: str, reading: dict[str, Any] | None = None,
        verdict: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Specific correction for a piece of the learner's own Spanish.

        The shape is the same as translation feedback -- corrections, not prose --
        because "your paragraph is good but here are four things" is more useful
        than a paragraph of encouragement. The one thing asked for that
        translation feedback does not need is a *corrected* version of the same
        piece: not a rewrite, which would be a better piece than the one they
        wrote, but their own sentences with the errors taken out, so the
        comparison is theirs to make.
        """
        scores = ""
        if verdict:
            checks = verdict.get("checks") or {}
            if checks:
                scores = "\n".join(f"- {k}: {v:.0%} confident yes" for k, v in checks.items())
            if verdict.get("score") is not None:
                scores += f"\n- overall quality: {verdict['score']:.1f} / {verdict.get('score_max', 4)}"
        measured = ""
        if reading:
            measured = (f"\nMeasured without a model: {reading.get('words')} words, "
                        f"{round(float(reading.get('spanish_share') or 0) * 100)}% Spanish, "
                        f"{reading.get('sentences')} sentences. "
                        f"Prompted words used: {', '.join(reading.get('used') or []) or 'none'}. "
                        f"Missed: {', '.join(reading.get('missed') or []) or 'none'}.")

        prompt_text = f"""A learner wrote this in Spanish for practice.

The prompt they were given: {prompt or "(no prompt)"}
What they wrote:
{text}
{measured}

An automated judge scored it:
{scores or "(no scores available)"}

Return JSON:
{{
  "summary": "one sentence in English telling them how it reads",
  "notes": [
    {{"fragment": "an exact substring of what they wrote",
      "kind": "good" | "fix" | "style",
      "issue": "what is wrong with it, at most 5 words (empty for good)",
      "suggestion": "what it should be (empty for good)",
      "why": "one short English sentence"}}
  ],
  "corrections": [
    {{"their_text": "the exact fragment from their piece", "should_be": "what it should be", "why": "the rule, in one short English sentence"}}
  ],
  "praise": "one specific thing they got right, or empty string",
  "next": "one thing to try in the next piece, in one short English sentence"
}}

About "notes": these are placed *on* their text, so the fragment must be copied
character for character from what they wrote -- a fragment that is not there
cannot be shown. Mark at least one thing they got right with "good" before you
mark anything wrong: a piece with four errors and no acknowledgement reads as
hostile. Use "fix" for what is wrong and "style" for what is correct but would
be better said another way. At most eight notes, and fewer is better -- the
useful ones are always fewer than the available ones.

About "corrections": only for errors the notes do not already cover, and empty
if the notes say everything -- repeating them twice is a longer answer, not a
better one.

Be accurate: do not invent errors, and do not flag a valid alternative as a mistake. If the piece is mostly English, say so plainly -- that is the most useful thing you can tell them."""
        try:
            data, _ = self.client.complete_json(prompt_text, system=SYSTEM, max_tokens=1400,
                                                effort="medium", timeout=BATCH_TIMEOUT, attempts=2)
        except LLMError as exc:
            # Short and quiet on purpose. The learner is looking at a piece they
            # just wrote; the useful thing to tell them is that the corrections
            # are missing and the scores are not, not the upstream status code.
            log.warning("writing feedback failed: %s", exc)
            return {"failed": True, "summary": "The tutor could not be reached, so there are no "
                                               "corrections for this piece. The measurements and "
                                               "the scores above are still yours.",
                    "notes": [], "corrections": [], "praise": "", "next": ""}
        out = _as_dict(data)
        out.setdefault("corrections", [])
        # The notes are the one part of this that has to be *true of the text*,
        # so they are checked against it rather than trusted.
        out["notes"] = keep_notes(text, out.get("notes"))
        return out

    def improve_writing(
        self, *, text: str, mode: str, prompt: str = "", focus: list[str] | None = None,
    ) -> dict[str, Any]:
        """One revision of a piece, and why it is a revision.

        Returned as a *suggestion*, never applied: the reader's own words stay on
        screen next to it. A model that silently replaces what someone wrote
        teaches nothing, because the only part worth learning is the difference.
        """
        chosen = BY_MODE.get(mode) or MODES[0]
        words = ", ".join(focus or [])
        asked = f"""{chosen["instruction"]}

The learner wrote this in Spanish:
{text}

The prompt they were given: {prompt or "(none)"}
{f"Vocabulary they are currently learning, to use where it belongs naturally: {words}" if words else ""}

Return JSON:
{{
  "revision": "the whole piece, revised, in Spanish -- complete sentences, nothing omitted",
  "changed": "one short English sentence on what you changed and why"
}}
Keep it the same length unless the instruction says otherwise. Do not add new ideas
or comment on the piece; return the revision itself and nothing else."""
        try:
            data, _ = self.client.complete_json(asked, system=SYSTEM, max_tokens=1800,
                                                effort="medium", timeout=BATCH_TIMEOUT, attempts=2)
        except LLMError as exc:
            log.warning("improve failed: %s", exc)
            return {"failed": True, "mode": chosen["id"], "revision": "",
                    "changed": "", "summary": "The tutor could not be reached, so there is no "
                                              "suggestion for this draft."}
        out = _as_dict(data)
        out["mode"] = chosen["id"]
        out["label"] = chosen["label"]
        return out

    def sentence_translation(self, *, sentence: str, target: str) -> str:
        """A full translation of one sentence, for the reader's "show me" button."""
        prompt = f"""Translate this sentence into {target}. Return only the translation, no commentary.

{sentence}"""
        result = self.client.complete(prompt, system=SYSTEM, max_tokens=600, temperature=0.0,
                                     timeout=GLOSS_TIMEOUT, attempts=2)
        return result.text.strip()

    def to_english(self, *, text: str) -> dict[str, Any]:
        """The reader's own passage, in English, whatever language they wrote it in.

        The diglot format is English prose with Spanish woven in, so a passage
        written in Spanish -- or French, or Chinese -- has to be rendered as
        English before it can be woven. This is that step.

        The answer reports the language it found, because the caller must not use
        the returned English when the passage was already English. This is the
        reader's own writing: a model asked to "translate" English will paraphrase
        it, and their words are the point. So for English input the model is only
        trusted for its judgement, and the original text is what gets woven.
        """
        prompt = f"""Someone wrote this passage, possibly not in English. Put it into English.

{text}

Return JSON with exactly these keys:
{{
  "language": "the ISO 639-1 code of the language the passage is written in, e.g. en, es, fr, zh",
  "language_name": "that language in English, e.g. English, Spanish, French, Chinese",
  "english": "the passage in English"
}}

Rules for "english":
- Keep the paragraph breaks exactly as they are: the same number of paragraphs, in the same
  order, separated by a blank line. Do not merge them or split them.
- Translate faithfully. Do not add sentences, do not drop sentences, do not summarise, and do
  not explain anything.
- Keep the writer's own register and tone -- a diary entry stays a diary entry.
- If the passage is already English, return it character for character, exactly as given:
  do not fix it, shorten it, rephrase it or improve it."""
        data, _ = self.client.complete_json(prompt, system=SYSTEM, max_tokens=4000,
                                           timeout=BATCH_TIMEOUT, attempts=2)
        return _as_dict(data)

    def suggest_next(self, *, read: list[str], saved: list[str], library: list[str]) -> str:
        """A short note on what to read next, from what the learner has read and saved."""
        prompt = f"""A Spanish learner using a diglot reader has finished these articles:
{chr(10).join('- ' + t for t in read) or '(none yet)'}

They have saved these words: {', '.join(saved[:40]) or '(none yet)'}

Articles still available:
{chr(10).join('- ' + t for t in library) or '(none)'}

In 2-3 sentences of English, say what to read next and why -- pick one article and give a concrete reason connected to what they have already read or saved. If nothing fits, say so plainly. No headings, no lists."""
        result = self.client.complete(prompt, system=SYSTEM, max_tokens=500, temperature=0.4,
                                     timeout=EXPLAIN_TIMEOUT, attempts=2)
        return result.text.strip()


def _as_dict(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return {"items": data}
    return {}
