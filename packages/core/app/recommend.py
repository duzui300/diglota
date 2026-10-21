"""Recommendation: find the next article by the vocabulary it will recycle.

The idea this is built on is that reading gets easier, fast, when the next text
reuses the words you just met. So the recommendation is not "more like what you
read" -- it is "likely to contain the words you are currently learning".

That claim needs evidence, and there is a neat way to get it without asking a
model to guess. Every saved word carries an English gloss, and a candidate
article found on the web is in English. So the gloss is a *probe*: saved
``el genoma`` glossed ``the genome`` means a candidate article can be checked
for the literal word "genome" before it is ever woven into Spanish. The overlap
score below is computed that way -- by fetching the candidate and looking -- so
the "12 of your words are likely to recur" line in the UI is measured, not
asserted.

Pipeline:

1.  Read the learner's deck and reading history.
2.  Ask the model for search topics that would naturally recycle that
    vocabulary. This is the one generative step; it turns a word list into
    something you can type into a search box.
3.  Search the web for each topic and gather candidates.
4.  Fetch each candidate and score it against the probe words. Cheap, parallel,
    and no further model calls.
5.  Rank by overlap, discounting anything already read.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Iterable

from .fetch import FetchError, extract_article, fetch_url, search_web
from . import registers
from .tutor import SYSTEM, Tutor

# Words too common to be evidence that two texts share a topic.
_STOP = frozenset(
    """a an the and or but if of to in on at by for with from as is are was were be been
    being am has have had do does did will would can could shall should may might must
    this that these those it its he she they them their his her we us our you your i me
    my not no so than then there here when where which who whom whose what how why all
    any some more most other such only also very just into over after before between
    during about up down out off again once because while both each few many much own
    same too one two something someone thing things way ways able like make makes made
    get gets got give gives given take takes put puts come comes go goes use used using
    """.split()
)

MIN_PROBE_LENGTH = 5


@dataclass
class Candidate:
    url: str
    title: str
    snippet: str = ""
    site: str = ""
    source: str = ""
    topic: str = ""
    words: int = 0
    hits: list[str] = field(default_factory=list)
    score: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url, "title": self.title, "snippet": self.snippet,
            "site": self.site, "source": self.source, "topic": self.topic,
            "words": self.words, "hits": self.hits, "score": round(self.score, 3),
            "error": self.error,
        }


def probe_words(words: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    """Map each saved Spanish term to the English words that would betray it.

    ``el genoma`` / ``the genome`` yields ``{'el genoma': ['genome']}``. Only
    content words of a decent length survive, because short ones ("rise", "art")
    match half the internet and would make the score meaningless.
    """
    probes: dict[str, list[str]] = {}
    for word in words:
        gloss = (word.get("gloss") or "").strip().lower()
        if not gloss:
            continue
        tokens = [
            token for token in re.findall(r"[a-z]{3,}", gloss)
            if token not in _STOP and len(token) >= MIN_PROBE_LENGTH
        ]
        # Keep the two most distinctive words of the gloss.
        tokens.sort(key=len, reverse=True)
        if tokens:
            probes[word["term"]] = tokens[:2]
    return probes


def score_text(text: str, probes: dict[str, list[str]]) -> tuple[float, list[str]]:
    """How many of the learner's words this text looks likely to reuse."""
    lowered = text.lower()
    hits: list[str] = []
    for term, tokens in probes.items():
        if any(_contains(lowered, token) for token in tokens):
            hits.append(term)
    if not probes:
        return 0.0, hits
    # Raw fraction, so a candidate is judged against the whole deck rather than
    # against whichever candidate happens to have the most matches.
    return len(hits) / len(probes), hits


def _contains(haystack: str, token: str) -> bool:
    """Prefix match, so ``genome`` finds ``genomes`` and ``genomic``."""
    stem = token[: max(MIN_PROBE_LENGTH, len(token) - 2)]
    return re.search(rf"\b{re.escape(stem)}\w*", haystack) is not None


# --------------------------------------------------------------------------- #
# Topics
# --------------------------------------------------------------------------- #


def suggest_topics(
    tutor: Tutor, *, words: list[dict[str, Any]], read: list[str], limit: int = 4
) -> list[dict[str, str]]:
    """Ask for search topics that would recycle the learner's vocabulary."""
    if words:
        deck = "\n".join(f"- {w['term']} ({w.get('gloss') or '?'})" for w in words[:50])
        basis = f"The learner has saved these words:\n{deck}"
    else:
        basis = "The learner has not saved any words yet, so recommend on general interest."

    history = ("\nThey have already read:\n" + "\n".join(f"- {t}" for t in read[:20])) if read else ""

    prompt = f"""{basis}{history}

Suggest {limit} search topics for finding *English* long-form articles that would naturally reuse this vocabulary, so that reading them is also revision.

Each topic should be a short search query (3-7 words) that would surface essays or encyclopedia articles, not news headlines. Vary them: at least one should stay close to what they already read, and at least one should push into an adjacent area where the same words still appear.

Return JSON:
{{
  "topics": [
    {{"query": "the search query", "why": "one short English sentence: which of their words this would bring back"}}
  ]
}}"""
    data, _ = tutor.client.complete_json(prompt, system=SYSTEM, max_tokens=800)
    topics = data.get("topics") if isinstance(data, dict) else None
    if not isinstance(topics, list) or not topics:
        raise ValueError("the tutor did not return usable topics")
    return [
        {"query": str(t["query"]).strip(), "why": str(t.get("why", "")).strip()}
        for t in topics
        if isinstance(t, dict) and t.get("query")
    ][:limit]


# --------------------------------------------------------------------------- #
# What the reader is not reading
# --------------------------------------------------------------------------- #

# Words that say nothing about a topic. Titles are short and concrete, so this is
# only here to stop "the" and "how" becoming somebody's stated interest.
_TITLE_STOP = _STOP | frozenset(
    """will make made worse end start new what really means mean about into
    towards toward their there where when why who whom whose""".split()
)


def topics_from_titles(titles: Iterable[str], *, limit: int = 2) -> list[str]:
    """The subjects someone keeps choosing, read off the titles they read.

    Titles rather than text, because what is wanted here is a *search query* and
    the corpus's own vocabulary is Spanish -- ``arte`` is not something to type
    into a search box for English articles, while "art" is what the titles say.

    Phrases before words. Counting single words gave "large" as a subject, from
    titles about *large language models*; a phrase that recurs across titles is
    what the reader actually keeps choosing, so one is preferred and single words
    are the fallback for a shelf too small to repeat anything.
    """
    singles: dict[str, int] = {}
    pairs: dict[str, int] = {}

    def keep(word: str) -> bool:
        # Three, not four: "art" and "artificial" are both subjects, and a length
        # filter tuned to drop noise dropped the topics this corpus is about.
        return len(word) >= 3 and word not in _TITLE_STOP

    for title in titles:
        words = [w.lower().strip("'-") for w in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", title or "")]
        for word in words:
            if keep(word):
                singles[word] = singles.get(word, 0) + 1
        for left, right in zip(words, words[1:]):
            if keep(left) and keep(right):
                pairs[f"{left} {right}"] = pairs.get(f"{left} {right}", 0) + 1

    def ranked(counts: dict[str, int]) -> list[str]:
        # Recurrence first, then the longer phrase, then alphabetical so the
        # answer is the same every time it is asked.
        return [text for text, count in
                sorted(counts.items(), key=lambda pair: (-pair[1], -len(pair[0]), pair[0]))
                if count > 1]

    return (ranked(pairs) or ranked(singles)
            or [text for text, _ in sorted(singles.items(), key=lambda pair: (-pair[1], pair[0]))])[:limit]


def join_topics(topics: list[str]) -> str:
    """The chosen subjects as one query, with overlapping phrases merged.

    A shelf about language models yields both "large language" and "language
    models", which are the same subject seen twice; asking for both reads as a
    stutter. Where two phrases share an end word -- in either order, since the
    ranking does not know which way round they came out -- they are one phrase.
    """
    out = ""
    for phrase in topics:
        words = phrase.split()
        if not out or not words:
            out = out or phrase
            continue
        if words[0] == out.split()[-1]:
            out = f"{out} {' '.join(words[1:])}".strip()
        elif words[-1] == out.split()[0]:
            out = f"{' '.join(words[:-1])} {out}".strip()
        else:
            out = f"{out} {phrase}".strip()
    return out


def broaden(
    entries: Iterable[Any], progress: dict[str, dict[str, Any]], *, limit: int = 3
) -> list[dict[str, Any]]:
    """Registers the reader has not touched, and a way into each.

    The failure mode this exists for is quiet: someone reads eleven essays about
    art and never notices they have not read a single news report, and no screen
    in the app says so. Register is the whole point of a multi-register corpus,
    and a gap is only visible if something counts it.

    Each suggestion carries two ways in, because they are different acts. There
    may be unread articles of that register already on the shelf, in which case
    the suggestion names them -- that costs nothing and is the more likely thing
    to be wanted. There may not be, in which case it offers a search instead,
    phrased around a subject the reader has actually chosen before.
    """
    register_of: dict[str, str] = {}
    by_register: dict[str, list[Any]] = {}
    for entry in entries:
        code, _inferred = registers.effective(entry.article)
        register_of[entry.article.slug] = code
        by_register.setdefault(code, []).append(entry)

    # "Met" rather than "finished". Two per cent in is enough to have met a news
    # report, and stopping there is a normal thing to do -- treating an abandoned
    # article as unread would keep recommending the register the reader already
    # bounced off. The library's own notion of started, so the two agree.
    def met(slug: str) -> bool:
        state = (progress or {}).get(slug) or {}
        return bool(state.get("completed_at")) or (state.get("position") or 0) > 0.02

    met_by_register: dict[str, int] = {}
    read_titles: list[str] = []
    for entry in entries:
        if not met(entry.article.slug):
            continue
        code = register_of.get(entry.article.slug)
        if code:
            met_by_register[code] = met_by_register.get(code, 0) + 1
        read_titles.append(entry.article.title)

    topics = topics_from_titles(read_titles)
    base = join_topics(topics)

    gaps = []
    for code in registers.CODES:
        have = by_register.get(code, [])
        if not have:
            continue
        if met_by_register.get(code, 0):
            continue
        unread = [e for e in have if not met(e.article.slug)]
        blurb = registers.describe(code)
        # The same subject, asked for as a different kind of writing. Without
        # this every gap suggests one identical search, and clicking three
        # suggestions would return the same three results.
        register = registers.resolve(code)
        hint = register.search if register else ""
        gaps.append({
            "register": code,
            "label": registers.label(code),
            "blurb": blurb,
            "read": met_by_register.get(code, 0),
            "available": len(have),
            "unread": [{"slug": e.article.slug, "title": e.article.title} for e in unread[:3]],
            "query": f"{base} {hint}".strip() if base else "",
            "topic": ", ".join(topics),
        })
    # The biggest shelf the reader has never opened is the most worth naming.
    gaps.sort(key=lambda gap: (-gap["available"], gap["register"]))
    return gaps[:limit]


# --------------------------------------------------------------------------- #
# Scoring candidates
# --------------------------------------------------------------------------- #


def score_candidate(candidate: Candidate, probes: dict[str, list[str]], proxy: str | None) -> Candidate:
    """Fetch one candidate and measure the vocabulary overlap."""
    try:
        html_text, _ = fetch_url(candidate.url, proxy=proxy, timeout=25)
        extracted = extract_article(html_text)
    except FetchError as exc:
        candidate.error = str(exc)
        return candidate
    except Exception as exc:
        candidate.error = f"{type(exc).__name__}: {exc}"
        return candidate

    body = "\n".join(b["text"] for b in extracted["blocks"])
    candidate.words = extracted["words"]
    if not candidate.title or candidate.title.lower().startswith("http"):
        candidate.title = extracted.get("title") or candidate.title
    if not candidate.snippet:
        first = next((b["text"] for b in extracted["blocks"] if b["kind"] == "p"), "")
        candidate.snippet = first[:260]

    ratio, hits = score_text(f"{candidate.title}\n{body}", probes)
    candidate.hits = hits
    candidate.score = ratio
    return candidate


def recommend(
    *,
    tutor: Tutor,
    words: list[dict[str, Any]],
    read: list[str],
    proxy: str | None,
    per_topic: int = 4,
    max_candidates: int = 10,
    progress: Any = None,
) -> dict[str, Any]:
    """The whole pipeline. Returns topics, scored candidates and its basis."""

    class _Silent:
        def step(self, name: str, **_: Any) -> None: ...
        def check(self) -> None: ...

    progress = progress or _Silent()

    probes = probe_words(words)
    progress.step("Thinking of topics that would reuse your words", done=0, total=2)
    topics = suggest_topics(tutor, words=words, read=read)
    progress.check()

    # Gather candidates from every topic in parallel -- each search is an
    # independent network round trip.
    def search(topic: dict[str, str]) -> list[Candidate]:
        try:
            results = search_web(topic["query"], proxy=proxy, limit=per_topic)
        except FetchError:
            return []
        return [
            Candidate(
                url=item["url"], title=item["title"], snippet=item.get("snippet", ""),
                site=item.get("site", ""), source=item.get("source", ""), topic=topic["query"],
            )
            for item in results
        ]

    with ThreadPoolExecutor(max_workers=len(topics) or 1) as pool:
        grouped = list(pool.map(search, topics))

    seen: set[str] = set()
    candidates: list[Candidate] = []
    for group in grouped:
        for candidate in group:
            if candidate.url in seen:
                continue
            seen.add(candidate.url)
            candidates.append(candidate)

    # Only score as many as we will show; scoring means fetching the page.
    candidates = candidates[:max_candidates]
    progress.check()

    if probes:
        progress.step("Checking each candidate against your vocabulary", done=0,
                      total=len(candidates) or 1)
        checked = 0
        if candidates:
            with ThreadPoolExecutor(max_workers=min(6, len(candidates))) as pool:
                futures = {pool.submit(score_candidate, c, probes, proxy): c for c in candidates}
                scored: list[Candidate] = []
                for future in as_completed(futures):
                    try:
                        scored.append(future.result())
                    except Exception:
                        pass
                    checked += 1
                    progress.step("Checking each candidate against your vocabulary",
                                  done=checked, total=len(candidates),
                                  detail=f"{checked} of {len(candidates)} fetched")
            candidates = [c for c in scored if c.error is None]
        candidates.sort(key=lambda c: (-c.score, c.words or 10**9))
    else:
        # No deck yet: fall back to substance, so the first recommendation is
        # still a real article rather than whatever ranked first.
        candidates.sort(key=lambda c: c.title)

    return {
        "topics": topics,
        "candidates": [c.to_dict() for c in candidates],
        "basis": {
            "words": len(probes),
            "probed": list(probes)[:40],
            "read": len(read),
            "note": (
                "Scored by fetching each candidate and looking for the English words behind "
                "your saved Spanish. No score means no deck yet."
                if probes else
                "You have not saved any words yet, so there is nothing to match against — "
                "these are simply substantial articles on the topics suggested."
            ),
        },
    }
