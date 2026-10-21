"""Tests for the tutor's output shaping.

Almost all of this is about *length*. The model is a reasoning model with a
tendency to over-deliver: asked to explain a word it will give the complete
morphology, the regional variants, and a closing summary. In a 344px popover
mid-article that is unusable, so the prompt pins a shape and a guard catches the
cases where the prompt is ignored.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tutor import Tutor, _trim_answer  # noqa: E402


class RecordingClient:
    """Answers with one canned payload and keeps every prompt it was given."""

    def __init__(self, payload):
        self.payload = payload
        self.prompts: list[str] = []

    def complete_json(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return self.payload, None


# --------------------------------------------------------------- trimming --


def test_a_short_answer_is_left_alone():
    answer = "- **Means:** a referral\n- **Watch out:** not “derivation”"
    assert _trim_answer(answer) == answer


def test_a_long_answer_is_cut_at_a_line_boundary():
    """The answer is bullets, so cutting between them keeps every surviving line
    whole."""
    answer = "\n".join(f"- **Point {i}:** " + "word " * 15 for i in range(12))
    trimmed = _trim_answer(answer, max_words=60)
    assert len(trimmed.split()) <= 60
    assert trimmed.startswith("- **Point 0:**")
    assert all(line.startswith("- **Point") for line in trimmed.splitlines())


def test_a_single_runaway_line_is_cut_at_a_sentence():
    answer = "- **Means:** " + ". ".join(f"sentence number {i} about the word" for i in range(20)) + "."
    trimmed = _trim_answer(answer, max_words=40)
    assert len(trimmed.split()) <= 45
    assert trimmed.endswith("."), "should not end mid-clause"


def test_trimming_never_returns_nothing():
    answer = "word " * 400
    trimmed = _trim_answer(answer, max_words=20)
    assert trimmed.strip(), "a pathological answer must still yield something"


def test_empty_input_is_handled():
    assert _trim_answer("") == ""
    assert _trim_answer("   ") == "   "


def test_the_guard_is_loose_enough_not_to_fire_normally():
    """The prompt asks for 60 words; the guard sits well above that so it only
    catches a genuine ramble rather than reshaping ordinary answers."""
    ordinary = "\n".join([
        "- **Means:** a referral that leads to an appointment on the same day, "
        "rather than just advice to come back later",
        "- **Grammar:** conllevar takes a plain noun here -- conlleva una visita -- "
        "with no preposition, so it reads as entails or involves",
        "- **Watch out:** derivación is not \"derivation\" in clinical Spanish, and "
        "el mismo día takes no en",
    ])
    assert 50 < len(ordinary.split()) < 100, "that is the length the prompt asks for"
    assert _trim_answer(ordinary) == ordinary


# ------------------------------------------------- the reader's writing --


def test_putting_a_passage_into_english_asks_for_the_language_it_found():
    """The caller needs the language, not just the English: it has to know whether
    the passage was already English, because in that case the model's own text is
    thrown away and the reader's is kept instead."""
    client = RecordingClient({"language": "es", "language_name": "Spanish",
                              "english": "The house is big."})
    result = Tutor(client).to_english(text="La casa es grande.")
    prompt = client.prompts[0]
    assert '"language"' in prompt and '"language_name"' in prompt and '"english"' in prompt
    assert "La casa es grande." in prompt
    assert result["language"] == "es"


def test_the_prompt_tells_the_model_to_leave_english_alone():
    """The reader's own writing is the point. A model asked to "translate" English
    paraphrases it, so the instruction to return it character for character is what
    makes this step safe to run on every piece unconditionally."""
    client = RecordingClient({"language": "en", "language_name": "English",
                              "english": "Anything at all."})
    Tutor(client).to_english(text="My own words, exactly.")
    prompt = client.prompts[0]
    assert "return it character for character" in prompt
    assert "do not fix it" in prompt


def test_the_prompt_pins_the_paragraph_shape():
    """One paragraph out for one paragraph in: the caller checks the count and
    rebuilds the breaks when it does not match, and a rectangle of text is the one
    shape the weave cannot distribute Spanish across."""
    client = RecordingClient({"language": "fr", "language_name": "French", "english": "x"})
    Tutor(client).to_english(text="Bonjour.")
    assert "the same number of paragraphs" in client.prompts[0]
