"""Register: what kind of writing a passage is, as a first-class property.

Learning Spanish from one kind of text teaches one kind of Spanish. A news
report is dense with nominalisation and impersonal constructions; an essay
argues with connectives; fiction runs on narrative past and dialogue; academic
writing subordinates clause inside clause; conversation is fragments, fillers
and second person. A reader who has only met essays will find a news report
hard for reasons that have nothing to do with vocabulary.

So register is attached to every passage and used in three places: the library
can be filtered and browsed by it, discovery can be asked for it, and a reading
challenge can require a spread of it.

Two sources, deliberately:

*   **Explicit**, from the front matter. An author who says what a piece is is
    right, and nothing here should second-guess them.
*   **Inferred**, by a heuristic, for the hand-made corpus and anything else
    that predates the field. This is a guess, and it is labelled as one in the
    UI -- a confident wrong register is worse than no register, because the
    whole point is that the reader trusts the label enough to act on it.

Imports get a judgment instead of a guess: the register is one of the typed
questions asked of Jev alongside the difficulty estimate, so it costs no extra
call. Jev is a judgment model; "what kind of writing is this" is a judgment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .diglot import Article


@dataclass(frozen=True)
class Register:
    code: str
    name: str
    icon: str
    # What is different about the *Spanish* here. This is the learning value of
    # the label, so it is written as a reading note rather than a definition.
    blurb: str
    # What to look for when asking a model to classify.
    signal: str
    # A word to add to a discovery query to bias it towards this register. Empty
    # where the default queries already find it -- an essay is what a search box
    # gives you unless you ask for something else.
    search: str = ""


REGISTERS: tuple[Register, ...] = (
    Register(
        code="news",
        name="News",
        icon="📰",
        blurb="Reported events in the past tense, with attributed quotes. Spanish news "
              "leans on impersonal and passive constructions and turns verbs into nouns.",
        signal="reports events to a general reader; attributed quotes; datelines; "
               "past tense reporting; few first-person asides",
        search="news",
    ),
    Register(
        code="essay",
        name="Essay",
        icon="📚",
        blurb="One voice arguing a position. Connectives do the work — sin embargo, "
              "por lo tanto, en cambio — and the first person is present.",
        signal="a first-person argument; rhetorical questions; connectives carrying "
               "the reasoning; no citations and no plot",
    ),
    Register(
        code="conversation",
        name="Conversation",
        icon="💬",
        blurb="People talking. Short turns, fillers, second person, and the present "
              "tense where writing would use something more formal.",
        signal="dialogue between speakers; short turns; fillers and interjections; "
               "second person; contractions and informal vocabulary",
        search="interview",
    ),
    Register(
        code="academic",
        name="Academic",
        icon="🎓",
        blurb="Impersonal and heavily subordinated. Terms are defined before use and "
              "the subjunctive appears in hedging — puede que, es posible que.",
        signal="a research or textbook voice; citations or numbered references; "
               "definitions; impersonal se; nominalisations and nested subordinate clauses",
        search="research",
    ),
    Register(
        code="fiction",
        name="Fiction",
        icon="📖",
        blurb="Narrative past and description. Dialogue is marked with dashes, and the "
              "past tenses alternate for background and event.",
        signal="a narrative; characters and setting; description; dialogue marked with "
               "dashes or quotes; no citations and no argument",
        search="short story",
    ),
)

BY_CODE: dict[str, Register] = {register.code: register for register in REGISTERS}
CODES: tuple[str, ...] = tuple(BY_CODE)

# An article with nothing to go on. Better an explicit "unknown" than a coin
# flip presented as a fact.
UNKNOWN = "unspecified"


def resolve(code: str | None) -> Register | None:
    if not code:
        return None
    return BY_CODE.get(code.strip().lower())


def label(code: str | None) -> str:
    register = resolve(code)
    return f"{register.icon} {register.name}" if register else ""


# --------------------------------------------------------------------------- #
# Inference
# --------------------------------------------------------------------------- #

_CITATION = re.compile(r"\[\d+\]|\((?:19|20)\d{2}\)|\bet al\.|doi:")
# An actual dialogue turn. Two ways to be one, and both are narrow on purpose.
#
# A line opening with a dash -- the em dash Spanish dialogue uses, *not* the
# hyphen Markdown bullets use, or every bulleted list reads as a conversation.
#
# Or a quotation that ends the way speech does: terminal punctuation inside the
# quotes, or an attribution immediately after. Scare quotes around a term --
# "Stochastic Parrots", "Chinese room argument" -- are citations, and counting
# those made three essays in this corpus read as interviews.
_DIALOGUE_TURN = re.compile(
    r"(?:^|\n)\s*[—–]\s*\S"
    r"|[\"“][^\"”]{10,}[.!?…][\"”]"
    r"|[\"“][^\"”]{10,}[\"”]\s*,?\s*(?:said|asked|replied|dijo|preguntó|respondió)\b",
    re.M | re.I,
)
_SPEECH_VERB = re.compile(r"\b(?:said|asked|replied|told|dijo|preguntó|respondió)\b", re.I)
# Reported speech attributed to a body rather than to a character: somebody on
# the record, speaking for an institution, to nobody in particular.
#
# Naming the *speaker* is what makes this evidence. A bare "according to" or
# "reported" is how an essay discusses research -- "according to a study",
# "researchers have reported" -- and counting those made two essays in this
# corpus read as news reports.
_ATTRIBUTION = re.compile(
    r"\b(?:officials? said|a spokesperson|police said|"
    r"the (?:minister|department|report|study|paper) said|researchers said|"
    r"according to (?:officials|police|the ministry|the department|a spokesperson)|"
    r"said (?:in a statement|on Tuesday|on Monday)|"
    r"según (?:fuentes|el ministerio|el gobierno)|fuentes oficiales)\b", re.I)
_FIRST_PERSON = re.compile(r"\b(?:I|we|my|our|yo|creo|pienso|considero|argumento)\b")
_CONNECTIVE = re.compile(
    r"\b(?:however|therefore|moreover|thus|sin embargo|por lo tanto|además|"
    r"en cambio|no obstante|por consiguiente)\b", re.I)
_NARRATIVE = re.compile(
    r"\b(?:walked|looked|felt|said nothing|miró|sintió|caminó|recordó|"
    r"once upon|had been|había sido)\b", re.I)
_IMPRESONAL = re.compile(r"\b(?:se observa|se define|se considera|it is argued|"
                         r"we propose|this paper|este artículo)\b", re.I)

# News sites are recognisable from the source, and a domain is stronger evidence
# than any amount of prose. Deliberately short -- a wrong guess costs more than
# a missing one.
_NEWS_HOSTS = ("bbc.", "reuters.", "apnews.", "elpais.", "nytimes.", "theguardian.",
               "washingtonpost.", "cnn.", "aljazeera.", "dw.com", "efe.com")
_ACADEMIC_HOSTS = ("arxiv.", "nature.com", "science.org", "springer.", "wiley.",
                   "tandfonline.", "jstor.", "pubmed.", "aclanthology.", "mdpi.")


def host_register(url: str | None) -> str | None:
    """The register a *source* implies, when the source says so on its own.

    A domain is stronger evidence than any amount of prose and costs nothing to
    read, which is why discovery can label a search result as news or academic
    before the article is fetched, let alone woven. Returns None when the host
    says nothing, rather than guessing from the title.
    """
    host = (url or "").lower()
    if any(site in host for site in _NEWS_HOSTS):
        return "news"
    if any(site in host for site in _ACADEMIC_HOSTS):
        return "academic"
    return None


def infer(article: Article) -> str:
    """Guess a register from how the text is written.

    Deliberately conservative: a score has to be clearly ahead before it wins,
    because a confident wrong label is worse than an unlabelled passage. The
    result is presented as inferred wherever it is shown.
    """
    text = " ".join(block.plain for block in article.blocks)[:6000]
    lowered = text.lower()
    scores: dict[str, float] = {code: 0.0 for code in CODES}

    from_host = host_register(article.url)
    if from_host:
        scores[from_host] += 3.0

    citations = len(_CITATION.findall(text))
    if citations >= 4:
        scores["academic"] += 2.5
    elif citations >= 1:
        scores["academic"] += 0.8
        scores["essay"] += 0.3

    turns = len(_DIALOGUE_TURN.findall(text))
    if turns >= 3:
        scores["conversation"] += 2.0
        scores["fiction"] += 1.2
    elif turns >= 1:
        scores["fiction"] += 0.6

    # Reporting says who said it, on the record, to nobody in particular.
    attributions = len(_ATTRIBUTION.findall(text))
    if attributions >= 2:
        scores["news"] += 2.2
    elif attributions >= 1 and len(_SPEECH_VERB.findall(text)) >= 2:
        scores["news"] += 1.2

    if len(_FIRST_PERSON.findall(text)) >= 3:
        scores["essay"] += 1.6
    if len(_CONNECTIVE.findall(text)) >= 3:
        scores["essay"] += 1.4
    if len(_NARRATIVE.findall(text)) >= 3:
        scores["fiction"] += 1.8
    if _IMPRESONAL.search(text):
        scores["academic"] += 1.0

    # Question marks are *not* evidence of conversation on their own: a
    # rhetorical question is an essay device, and counting bare question marks
    # scored essays as interviews. Only count them next to actual dialogue.
    if turns >= 1 and lowered.count("?") >= 3:
        scores["conversation"] += 1.2

    best, top = max(scores.items(), key=lambda pair: pair[1])
    if top < 1.0:
        return UNKNOWN
    # A narrow win is not a win.
    runner_up = sorted(scores.values(), reverse=True)[1]
    return best if top - runner_up >= 0.6 else UNKNOWN


def effective(article: Article, *, trusted: bool = True) -> tuple[str, bool]:
    """The register to use, and whether it was inferred rather than declared.

    ``trusted`` is False for imported lessons, whose register came from a model
    judgment rather than from the author -- those are stored explicitly but are
    still not a declaration, and the UI says so.
    """
    declared = resolve(article.register)
    if declared:
        return declared.code, not trusted
    return infer(article), True


def describe(code: str) -> str:
    register = resolve(code)
    return register.blurb if register else ""


def options() -> list[dict[str, Any]]:
    return [
        {"code": register.code, "name": register.name, "icon": register.icon,
         "blurb": register.blurb, "signal": register.signal}
        for register in REGISTERS
    ]
