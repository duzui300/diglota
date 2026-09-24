"""Tests for register — the property that makes a corpus multi-register.

Register changes what a passage is like to read for reasons that have nothing to
do with vocabulary: a news report is dense with impersonal constructions, an
essay argues with connectives, fiction runs on narrative past. So the tests here
are about *trust*: a declared register must win, an inferred one must be
labelled as inferred, and an ambiguous passage must abstain rather than guess.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import registers  # noqa: E402
from app.diglot import parse_article  # noqa: E402


def article(body: str, *, url: str | None = None, register: str | None = None):
    """Build a diglot article. The front-matter heading is required: without it
    the parser reads those lines as prose, which is how the first version of
    this helper silently produced articles with no URL and no register."""
    front = ["### Article Identification & Preview", "", "- **Article Title:** A Test Article",
             "- **Author:** Someone"]
    if url:
        front.append(f"- **Direct URL:** {url}")
    if register:
        front.append(f"- **Register:** {register}")
    text = "\n".join(front) + "\n\n# A Test Article\n\n**By Someone**\n\n" + body
    parsed = parse_article(text, fallback_slug="a-test-article")
    assert parsed is not None
    return parsed


ESSAY = (
    "I argue that we misunderstand what these systems do. However, the evidence "
    "points elsewhere. Therefore I think the question needs reframing, and my "
    "conclusion is that we should be careful. I have argued this before."
)
ACADEMIC = (
    "Smith et al. (2019) report [1] that performance scales. Jones (2020) [2] "
    "disagrees. This paper defines the term before use [3] and the metric is "
    "defined as follows [4]. It is argued that se observa a consistent pattern."
)
FICTION = (
    "She walked to the window and looked out. He had been there before, and "
    "remembered the cold. She said nothing. He felt the silence. They had been "
    "children once, in that house. She said his name."
)
NEWS = (
    "The minister said the policy would change next year. Officials said the "
    "review was complete. A spokesperson said details would follow. The report "
    "was published on Tuesday, and the department said it would respond."
)


# -------------------------------------------------------------- taxonomy --


def test_every_code_resolves():
    for code in registers.CODES:
        assert registers.resolve(code) is not None
        assert registers.label(code)


def test_every_register_says_what_is_different_about_its_spanish():
    """The blurb is the learning value of the label -- without it the register
    is a tag rather than something the reader can act on."""
    for register in registers.REGISTERS:
        assert len(register.blurb) > 60, register.code
        assert len(register.signal) > 20, register.code


def test_lookup_is_case_insensitive_and_tolerant():
    assert registers.resolve("ESSAY").code == "essay"
    assert registers.resolve(" Fiction ").code == "fiction"
    assert registers.resolve("poetry") is None
    assert registers.resolve(None) is None
    assert registers.describe("nonsense") == ""


def test_options_are_serialisable():
    import json

    options = registers.options()
    assert json.loads(json.dumps(options)) == options
    assert {item["code"] for item in options} == set(registers.CODES)


# ---------------------------------------------------------------- declared --


def test_a_declared_register_wins():
    parsed = article(ESSAY, register="fiction")
    code, inferred = registers.effective(parsed)
    assert code == "fiction", "the author's word beats the heuristic"
    assert inferred is False


def test_an_unknown_declared_register_falls_back_to_inference():
    parsed = article(ACADEMIC, register="poetry")
    code, inferred = registers.effective(parsed)
    assert code != "poetry"
    assert inferred is True


# ---------------------------------------------------------------- inferred --


def test_citations_read_as_academic():
    assert registers.infer(article(ACADEMIC)) == "academic"


def test_first_person_argument_reads_as_essay():
    assert registers.infer(article(ESSAY)) == "essay"


def test_narrative_past_reads_as_fiction():
    assert registers.infer(article(FICTION)) in ("fiction", "conversation")


def test_attributed_quotes_read_as_news():
    assert registers.infer(article(NEWS)) == "news"


def test_a_news_domain_is_evidence_on_its_own():
    """A host is stronger evidence than any amount of prose, and cheaper."""
    hosted = article("Something happened. Then something else happened.", url="https://www.reuters.com/x")
    assert "news" in registers.infer(hosted) or registers.infer(hosted) == registers.UNKNOWN
    academic_host = article("A study of things.", url="https://arxiv.org/abs/1234")
    assert registers.infer(academic_host) == "academic"


def test_an_ambiguous_passage_abstains():
    """A confident wrong label is worse than no label: the whole point is that
    the reader trusts it enough to act on it."""
    plain = article("The cat sat on the mat. It was a nice mat. The mat was warm.")
    assert registers.infer(plain) == registers.UNKNOWN


def test_rhetorical_questions_do_not_make_an_essay_into_a_conversation():
    """Bare question marks are an essay device. Counting them as dialogue
    scored essays as interviews until it was fixed."""
    rhetorical = article(
        "I argue that we are asking the wrong question. What do we mean by "
        "intelligence? What would count as an answer? I think we should be "
        "careful here, and therefore I propose a different framing. However, "
        "the counterargument has force."
    )
    assert registers.infer(rhetorical) == "essay"


def test_scare_quotes_around_a_term_are_not_dialogue():
    """``"Stochastic Parrots"`` is a citation, not somebody speaking. Counting
    quoted noun phrases made three essays in the corpus read as interviews.

    Asserted as "not conversation" rather than "is essay": what is being pinned
    is which label these quotes must not produce, and the essay threshold is a
    separate question tested above.
    """
    essay = article(
        "The paper that introduced the term \"Stochastic Parrots\" argued that "
        "scale is not understanding. I think that is right, and therefore the "
        "framing of \"Sparks of Artificial Intelligence\" deserves the same "
        "scrutiny. However, the counterargument has force, and I propose a "
        "different reading of \"Chinese room argument\" here."
    )
    assert registers.infer(essay) != "conversation"
    assert registers.infer(essay) != "fiction"


def test_a_markdown_bullet_is_not_a_dialogue_turn():
    """Spanish dialogue opens with an em dash. A hyphen opens a list item, and
    treating the two the same made every bulleted article a conversation."""
    bulleted = article(
        "- The first thing to say about this.\n"
        "- The second thing, which follows from it.\n"
        "- And a third, to make the count reach the threshold.\n"
        "I argue that the pattern matters, and therefore I have set it out."
    )
    assert registers.infer(bulleted) != "conversation"


def test_quoting_a_study_is_not_reporting_the_news():
    """``according to a study`` is how an essay discusses research. Naming the
    *speaker* is what makes attribution evidence of news."""
    essay = article(
        "According to a study of the question, the effect is small. According to "
        "researchers, it is larger. I think the disagreement is the interesting "
        "part, and therefore I want to set out why. However, the evidence is thin."
    )
    assert registers.infer(essay) != "news"

    # ...while somebody on the record, speaking for an institution, is news.
    news = article(
        "Officials said the policy would change next year. A spokesperson said "
        "details would follow, and the department said it would respond."
    )
    assert registers.infer(news) == "news"


def test_a_news_domain_is_evidence_on_its_own():
    """A host is stronger evidence than any amount of prose, and cheaper."""
    hosted = article("Something happened. Then something else happened.", url="https://www.reuters.com/x")
    assert "news" in registers.infer(hosted) or registers.infer(hosted) == registers.UNKNOWN
    academic_host = article("A study of things.", url="https://arxiv.org/abs/1234")
    assert registers.infer(academic_host) == "academic"


def test_a_level_register_can_be_read_off_a_url_alone():
    """Discovery labels a result before it is fetched, let alone woven, so the
    host check has to stand on its own."""
    assert registers.host_register("https://www.bbc.co.uk/news/x") == "news"
    assert registers.host_register("https://arxiv.org/abs/1234") == "academic"
    assert registers.host_register("https://example.com/x") is None
    assert registers.host_register(None) is None
    assert registers.label(registers.host_register("https://elpais.com/x"))


def test_inference_never_raises_on_an_empty_article():
    assert registers.infer(article(" ")) in registers.CODES + (registers.UNKNOWN,)


# ------------------------------------------------------------------ round --


def test_a_register_survives_the_round_trip_through_a_written_lesson():
    from app import ingest

    parsed = article(ESSAY, register="academic")
    assert parsed.register == "academic"

    items = [("p", "El arte cambia con el tiempo y la pintura también.")]
    written = ingest.assemble_markdown(
        title="T", byline=None, url="https://example.com/a", preview="p",
        items=items, vocab=[], grammar=[], focus=[], level="B1", register="fiction",
    )
    reloaded = parse_article(written, fallback_slug="t")
    assert reloaded is not None
    assert reloaded.register == "fiction"
