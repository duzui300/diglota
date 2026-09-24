"""Tests for the LLM client's handling of answers that do not come back whole.

The interesting failures are not network failures -- those are retried and were
already understood. They are answers that arrive *cut short*: valid text, valid
JSON syntax right up to the point where the budget ran out, and a
``finish_reason`` that says exactly what happened. A lesson woven from the
reader's own writing lost its whole vocabulary box this way, and because the
anchors degrade softly by design, the only sign was a lesson with an empty
vocabulary box and a line in the report.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.llm import LLMClient, LLMError  # noqa: E402


class FakeHTTP:
    """Answers with a scripted sequence of completions, and records the requests."""

    def __init__(self, *answers: tuple[str, str]):
        self.answers = list(answers)
        self.asked: list[dict] = []

    def __call__(self, request, timeout=None):
        body = json.loads(request.data.decode())
        self.asked.append(body)
        content, finish = self.answers.pop(0)
        payload = {
            "model": "scripted",
            "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }
        return io.BytesIO(json.dumps(payload).encode())


@pytest.fixture()
def client(monkeypatch):
    def build(*answers):
        fake = FakeHTTP(*answers)
        monkeypatch.setattr("app.llm.urllib.request.urlopen", fake)
        return LLMClient("https://example.invalid/v1", "k", "scripted", max_attempts=1), fake

    return build


def test_a_truncated_answer_is_asked_for_again_with_more_room(client):
    """The retry is the whole point: the content was fine, it simply stopped."""
    llm, fake = client(('{"vocab": [{"term": "el río"', "length"),
                       ('{"vocab": [{"term": "el río"}], "grammar": []}', "stop"))
    data, result = llm.complete_json("give me JSON", max_tokens=1000)
    assert data == {"vocab": [{"term": "el río"}], "grammar": []}
    assert len(fake.asked) == 2
    assert fake.asked[0]["max_tokens"] == 1000
    assert fake.asked[1]["max_tokens"] > 1000, "the second attempt needs more room"


def test_an_unparseable_answer_that_was_not_cut_off_is_not_retried(client):
    """Asking again only helps when the answer ran out of room. A model that
    simply refused gives the same refusal, and the cost of finding that out twice
    is paid on every lesson."""
    llm, fake = client(("I would rather not.", "stop"))
    with pytest.raises(LLMError) as caught:
        llm.complete_json("give me JSON", max_tokens=1000)
    assert len(fake.asked) == 1
    assert "could not parse JSON" in str(caught.value)


def test_the_retry_is_bounded(client):
    """A model that truncates at every budget must not be asked forever -- the
    call is inside a job that is spending the reader's time."""
    llm, fake = client(('{"vocab": [', "length"), ('{"vocab": [', "length"))
    with pytest.raises(LLMError):
        llm.complete_json("give me JSON", max_tokens=1000)
    assert len(fake.asked) == 2, fake.asked


def test_json_wrapped_in_prose_or_fences_still_parses(client):
    """Models add a sentence before the JSON even in JSON mode, and this is the
    path that already existed -- it must keep working through the refactor."""
    llm, _ = client(('Sure, here it is:\n```json\n{"a": [1, 2]}\n```\nHope that helps.', "stop"))
    data, _ = llm.complete_json("give me JSON")
    assert data == {"a": [1, 2]}


def test_an_empty_answer_is_still_an_error(client):
    llm, _ = client(("", "stop"))
    with pytest.raises(LLMError):
        llm.complete_json("give me JSON")
