"""Fetching and reading the web.

Two jobs: turn a URL into the article text a human would say was on the page,
and search for candidate articles by topic. Both are dependency-free -- urllib
and html.parser -- because pulling in requests + BeautifulSoup + readability
to do this would triple the app's install size for a few hundred lines.

The extractor is a small readability clone. It builds a tree, scores every
container by how much paragraph text sits inside it while penalising link
density (which is what separates an article body from a nav bar or a list of
related links), and takes the winner. It is not as good as readability-lxml.
It is good enough for essays and blog posts, which is what this app reads.

Network note: on this machine outbound requests must go through the local
proxy; direct connections time out. The proxy is a setting, not a constant.
"""

from __future__ import annotations

import gzip
import html
import json
import re
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Iterable

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

TIMEOUT = 30

# Elements that never contain article prose.
_DROP = {"script", "style", "noscript", "svg", "form", "iframe", "nav", "aside",
         "footer", "header", "button", "select", "textarea", "template"}
# Elements that suggest a container is the article body.
_POSITIVE = {"article": 40, "main": 25, "section": 5}
# Class/id fragments that suggest a container is the article body...
_POSITIVE_HINT = re.compile(
    r"article|post|content|entry|story|prose|body|markdown|text|blog", re.I
)
# ...and ones that suggest it is not.
_NEGATIVE_HINT = re.compile(
    r"comment|share|related|sidebar|promo|newsletter|subscribe|footer|header|"
    r"nav|menu|social|advert|breadcrumb|meta|byline|tags|cookie|banner",
    re.I,
)
_BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "li", "blockquote", "pre", "figcaption"}


class FetchError(RuntimeError):
    """The page could not be retrieved or understood."""


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #


def _opener(proxy: str | None):
    handlers: list[Any] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def fetch_url(url: str, *, proxy: str | None = None, timeout: int = TIMEOUT) -> tuple[str, str]:
    """GET a URL. Returns ``(html, final_url)``."""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parsed = urllib.parse.urlparse(url)
    if not parsed.netloc:
        raise FetchError(f"not a usable URL: {url!r}")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    try:
        with _opener(proxy).open(request, timeout=timeout) as response:
            raw = response.read()
            final = response.geturl()
            encoding = response.headers.get("Content-Encoding", "")
            if "gzip" in encoding:
                raw = gzip.decompress(raw)
            elif "deflate" in encoding:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:
        raise FetchError(f"the site returned HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise FetchError(f"could not reach the site: {exc.reason}") from None
    except Exception as exc:
        raise FetchError(f"could not reach the site: {type(exc).__name__}: {exc}") from None

    try:
        return raw.decode(charset, "replace"), final
    except LookupError:
        return raw.decode("utf-8", "replace"), final


def fetch_json(url: str, *, proxy: str | None = None, timeout: int = 20) -> Any:
    text, _ = fetch_url(url, proxy=proxy, timeout=timeout)
    return json.loads(text)


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    parent: "_Node | None" = None
    # Children and text interleaved in document order, because "<p>see <b>this</b>
    # now</p>" has to come back as "see this now" and a separate list of child
    # nodes cannot express that.
    content: list["_Node | str"] = field(default_factory=list)

    @property
    def classes(self) -> str:
        return f"{self.attrs.get('class', '')} {self.attrs.get('id', '')}"

    @property
    def children(self) -> list["_Node"]:
        return [item for item in self.content if isinstance(item, _Node)]

    def all_text(self) -> str:
        parts: list[str] = []
        for item in self.content:
            parts.append(item.all_text() if isinstance(item, _Node) else item)
        return " ".join(p for p in parts if p)

    def find_all(self, tags: set[str]) -> Iterable["_Node"]:
        for child in self.children:
            if child.tag in tags:
                yield child
            yield from child.find_all(tags)


class _TreeBuilder(HTMLParser):
    _VOID = {"br", "img", "hr", "meta", "link", "input", "source", "area", "base", "col", "embed", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {})
        self.stack = [self.root]
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        node = _Node(tag, {k: (v or "") for k, v in attrs}, parent=self.stack[-1])
        self.stack[-1].content.append(node)
        if tag not in self._VOID:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = re.sub(r"\s+", " ", data)
        if text.strip():
            self.stack[-1].content.append(text)


def _link_density(node: _Node) -> float:
    total = len(node.all_text())
    if not total:
        return 1.0
    link_text = sum(len(a.all_text()) for a in node.find_all({"a"}))
    return min(link_text / total, 1.0)


def extract_article(html_text: str) -> dict[str, Any]:
    """Pull the title, byline and body paragraphs out of a page."""
    builder = _TreeBuilder()
    try:
        builder.feed(html_text)
    except Exception as exc:  # malformed markup is common and survivable
        raise FetchError(f"could not parse the page: {exc}") from None

    root = builder.root

    title = _page_title(root, html_text)
    byline = _find_byline(root)

    candidates: list[tuple[float, _Node]] = []
    for node in [root, *root.find_all({"article", "main", "section", "div", "td"})]:
        paragraphs = [p for p in node.find_all({"p"}) if len(p.all_text()) > 40]
        if len(paragraphs) < 2:
            continue
        score = float(sum(len(p.all_text()) for p in paragraphs))
        score += _POSITIVE.get(node.tag, 0)
        score *= 1.0 - _link_density(node)
        hint = node.classes
        if _POSITIVE_HINT.search(hint):
            score *= 1.35
        if _NEGATIVE_HINT.search(hint):
            score *= 0.25
        # Prefer the innermost container that still holds everything: a wrapper
        # around the whole page scores the same as the article inside it, and
        # picking the article keeps nav and footer out.
        score -= len(node.children) * 2
        candidates.append((score, node))

    if not candidates:
        raise FetchError("could not find any article text on that page")

    best = max(candidates, key=lambda pair: pair[0])[1]

    blocks: list[dict[str, str]] = []
    seen: set[str] = set()
    dropped_citations = 0
    for node in _walk_blocks(best):
        text = _clean_text(node.all_text())
        if len(text) < 25 or text in seen:
            continue
        if _link_density(node) > 0.55:  # a list of links, not prose
            continue
        if _looks_like_citation(text):
            dropped_citations += 1
            continue
        seen.add(text)
        kind = "h" if node.tag in {"h1", "h2", "h3", "h4"} else "p"
        if kind == "h" and len(text) > 120:
            kind = "p"
        blocks.append({"kind": kind, "text": text})

    # Drop headings that lead nothing.
    while blocks and blocks[0]["kind"] == "h":
        title = title or blocks[0]["text"]
        blocks.pop(0)
    while blocks and blocks[-1]["kind"] == "h":
        blocks.pop()

    paragraphs = [b for b in blocks if b["kind"] == "p"]
    words = sum(len(b["text"].split()) for b in paragraphs)
    if words < 120:
        raise FetchError(
            f"only found {words} words of article text on that page -- it is probably "
            "paywalled, JavaScript-rendered, or not an article"
        )

    return {"title": title, "byline": byline, "blocks": blocks, "words": words,
            "dropped_citations": dropped_citations}


# How much text there has to be before it is worth weaving.
#
# A fetched page and a paste are held to the same floor for the same reason: a
# headline, a summary or a fragment is not enough prose to weave a lesson out of,
# and finding that out after the weaving is a waste of everything it cost.
#
# The reader's own writing is held to a lower one on purpose. The workspace itself
# asks for forty words and up, and a piece someone wrote and saved is not a failed
# paste -- refusing it with "paste the article's body" would be answering a
# question they did not ask.
MIN_SOURCE_WORDS = 120
MIN_WRITING_WORDS = 40


def split_paragraphs(text: str) -> list[str]:
    """The block structure of plain text, in one place.

    Blank lines separate blocks, which is the convention every word processor,
    mail client and CMS writes on copy. With no blank lines anywhere there is no
    other structure to find, so the newlines themselves become the boundaries --
    the alternative is treating a whole document as one paragraph, and a single
    enormous block is the one shape the weave cannot distribute Spanish across.

    Shared with the translation of the reader's own writing, which has to come
    back in the same shape it went in: if the two disagreed about what a paragraph
    is, a five-paragraph passage would be woven as one.
    """
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    chunks = [c for c in re.split(r"\n[ \t]*\n", raw) if c.strip()]
    if len(chunks) <= 1:
        lines = [line for line in raw.split("\n") if line.strip()]
        if len(lines) > 1:
            chunks = lines
    return chunks


def blocks_from_text(text: str, *, min_words: int = MIN_SOURCE_WORDS) -> dict[str, Any]:
    """Read blocks out of text the reader pasted, rather than out of a page.

    For when the page cannot be fetched -- a paywall, a JavaScript-only reader, a
    host the app cannot reach -- but the reader can still read it themselves and
    copy it out. The paste stands in for the fetch, so everything downstream is
    the same pipeline rather than a reduced second path.

    The copy is not markup, so the structure has to be inferred from whitespace;
    see ``split_paragraphs``. Within a block, single newlines are joined back into
    one line, because most sources hard-wrap at a column and a paragraph broken
    every eighty characters would otherwise be woven as a dozen separate
    paragraphs.
    """
    chunks = split_paragraphs(text)

    blocks: list[dict[str, str]] = []
    title: str | None = None
    byline: str | None = None
    for chunk in chunks:
        lines = [line.strip() for line in chunk.split("\n") if line.strip()]
        if not lines:
            continue
        heading = re.match(r"^#{1,6}\s+(.*?)\s*#*$", lines[0])
        if heading and len(lines) == 1:
            text_ = _clean_text(heading.group(1))
            if text_:
                blocks.append({"kind": "h", "text": text_})
            continue
        joined = _clean_text(" ".join(lines))
        # The byline is tested before the headline, because "By Someone" is a
        # short line that does not end in punctuation and would otherwise be
        # taken for the title.
        if byline is None and not blocks and joined.lower().startswith("by ") and len(joined) < 90:
            byline = joined[3:].strip()
            continue
        # A pasted article normally arrives with its own headline on the first
        # line: short, and not a sentence. Taking it as the title keeps it out of
        # the weave, where it would otherwise be translated as a paragraph.
        if title is None and not blocks and len(lines) == 1 and _looks_like_headline(lines[0]):
            title = joined
            continue
        if joined:
            blocks.append({"kind": "p", "text": joined})

    # Headings that lead nothing, exactly as the page reader drops them: a first
    # heading is usually the article's own title, which is stored separately.
    while blocks and blocks[0]["kind"] == "h":
        title = title or blocks[0]["text"]
        blocks.pop(0)
    while blocks and blocks[-1]["kind"] == "h":
        blocks.pop()

    paragraphs = [b for b in blocks if b["kind"] == "p"]
    words = sum(len(b["text"].split()) for b in paragraphs)
    if words < min_words:
        raise FetchError(
            f"only {words} words of text, and weaving a lesson needs at least {min_words} -- "
            "a headline or a summary is not enough to build one from"
        )

    return {"title": title, "byline": byline, "blocks": blocks, "words": words,
            "dropped_citations": 0}


def _looks_like_headline(line: str) -> bool:
    """Short, and not a sentence. A genuine one-line paragraph is rare in prose
    and ends in punctuation; a headline does not."""
    stripped = line.strip()
    return 3 < len(stripped) <= 120 and not stripped.endswith((".", "!", "?", ":", ",", ";"))


def _walk_blocks(node: _Node) -> Iterable[_Node]:
    """Yield block elements in document order, not descending into one block
    and out into another's territory (a <p> inside a <blockquote>, say)."""
    for child in node.children:
        if child.tag in _BLOCK_TAGS:
            yield child
        elif child.tag not in {"a", "span", "strong", "em", "b", "i", "code", "small"}:
            yield from _walk_blocks(child)


def _clean_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:!?%)\]])", r"\1", text)
    # Wikipedia-style footnote markers come through as "[ 1]" with a space;
    # the weave preserves them, so tidy them before they reach the page.
    text = re.sub(r"\[\s*(\d+)\s*\]", r"[\1]", text)
    return text.strip()


def _page_title(root: _Node, html_text: str) -> str | None:
    for meta in root.find_all({"meta"}):
        if meta.attrs.get("property") in ("og:title", "twitter:title"):
            content = meta.attrs.get("content")
            if content:
                return _clean_text(content)
    for node in root.find_all({"h1"}):
        text = _clean_text(node.all_text())
        if 8 < len(text) < 200:
            return text
    match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.S | re.I)
    if match:
        title = _clean_text(match.group(1))
        return re.split(r"\s+[|—–-]\s+", title)[0].strip() or title
    return None


_BYLINE_RE = re.compile(r"^\s*(?:by|por|written by)\s+([A-Z][\w.'-]+(?:\s+[A-Z][\w.'-]+){0,3})", re.I)

# A bibliography entry: "Bamford, J., Day, R. (2004). Extensive reading
# activities..." or "Smith, A. B. (1999)". Wikipedia's reference lists are
# ordinary <p> or <li> elements, so the extractor picks them up as prose and
# they end up mid-lesson -- untranslatable, unreadable, and enough to make a
# paragraph count as "0% Spanish" and wreck any measure of how evenly the weave
# is spread.
_CITATION_RE = re.compile(
    r"[A-Z][\w'’-]+,\s*(?:[A-Z]\.\s*){1,3}(?:et al\.?)?\s*[,(]?\s*(?:19|20)\d{2}"
)
# "Rott, Susanne; Williams, Jessica (2002)" -- full given names rather than
# initials. The trailing ; or ( is what keeps this off place names in ordinary
# prose ("Berkeley, California." does not match; "Berkeley, California;" would
# only appear in a list).
_FULLNAME_CITE_RE = re.compile(
    r"[A-Z][\w'’-]+,\s*[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s*(?:[;(]|\d)"
)
_QUOTED_TITLE_RE = re.compile(r"\((?:19|20)\d{2}[a-z]?\),?\s*[\"“]")
_YEAR_PAREN_RE = re.compile(r"\((?:19|20)\d{2}[a-z]?\)")
# "1993, ELT J (1993) 47 (3): 250-267. doi: 10.1093/elt/47.3.250" -- a journal
# citation. The doi is decisive on its own.
_DOI_RE = re.compile(r"\bdoi:\s*10\.\d{4,}/|\bhttps?://doi\.org/")
_JOURNAL_RE = re.compile(r"\(\d{4}\)\s*\d+\s*\(\d+\)\s*:\s*\d+")


def _looks_like_citation(text: str) -> bool:
    """Bibliographies, reference lists and further-reading sections."""
    if len(text) > 700:
        return False                     # real prose can cite; a citation list cannot be long prose
    if _DOI_RE.search(text) or _JOURNAL_RE.search(text):
        return True
    citations = len(_CITATION_RE.findall(text)) + len(_FULLNAME_CITE_RE.findall(text))
    if citations >= 2:
        return True
    if citations == 1 and _QUOTED_TITLE_RE.search(text):
        return True
    if citations == 1 and len(_YEAR_PAREN_RE.findall(text)) >= 1 and len(text.split()) < 45:
        return True
    # "1 2 3 4 5 Author, A. (2001)..." -- a numbered reference run.
    if re.match(r"^\s*(?:\d{1,3}\s+){3,}", text):
        return True
    return False

# "By Jake Buehler September 21, 2026" otherwise yields an author called
# "Jake Buehler September" -- the date is capitalised too.
_DATE_WORDS = frozenset(
    "january february march april may june july august september october november december "
    "monday tuesday wednesday thursday friday saturday sunday".split()
)


def _find_byline(root: _Node) -> str | None:
    for node in root.find_all({"meta"}):
        if node.attrs.get("name") in ("author", "article:author", "dc.creator"):
            content = node.attrs.get("content")
            if content:
                return _clean_text(content)
    for node in root.find_all({"p", "span", "div", "a", "address"}):
        text = _clean_text(node.all_text())
        if len(text) > 120:
            continue
        match = _BYLINE_RE.match(text)
        if not match:
            continue
        words = [w for w in match.group(1).split() if w.lower().strip(".,") not in _DATE_WORDS]
        if words:
            return " ".join(words)
    return None


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

# Essay-quality feeds, used as a keyless search index. Each is long-form prose
# with a direct article URL -- which is the only kind of page this app can turn
# into a lesson. Ordered roughly by how well they suit a reader who wants
# something worth reading in two languages.
FEEDS: tuple[tuple[str, str, str], ...] = (
    ("Quanta", "https://api.quantamagazine.org/feed/", "science"),
    ("Aeon", "https://aeon.co/feed.rss", "philosophy and culture"),
    ("Nautilus", "https://nautil.us/feed/", "science and culture"),
    ("The Conversation", "https://theconversation.com/articles.atom", "academic, plain English"),
    ("MIT News", "https://news.mit.edu/rss/topic/artificial-intelligence2", "AI research"),
    ("Literary Hub", "https://lithub.com/feed/", "books and criticism"),
    ("BBC Future", "https://feeds.bbci.co.uk/future/rss.xml", "long reads"),
)

_ATOM = "{http://www.w3.org/2005/Atom}"

_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>'
    r'(?P<rest>.*?)(?=<a[^>]+class="[^"]*result__a|</div>\s*</div>\s*</div>)',
    re.S | re.I,
)
_SNIPPET_RE = re.compile(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.S | re.I)
_TAGS_RE = re.compile(r"<[^>]+>")

# A candidate has to be substantial enough to be worth weaving.
MIN_ARTICLE_WORDS = 400


# Pages that come back from a search and are not reading material. A
# disambiguation page is a list of links to other pages, and a list page is a
# table; neither can be woven into a lesson, and both crowd out the article the
# reader actually wanted -- searching for "sleep" returned "Sleep Token" and
# "Sleep (disambiguation)" alongside "Sleep deprivation".
_NOT_AN_ARTICLE = re.compile(r"\(disambiguation\)|^list of\b|^outline of\b|^index of\b", re.I)


def _is_readable(item: dict[str, str]) -> bool:
    """Whether a search result is prose that could become a lesson."""
    if _NOT_AN_ARTICLE.search(str(item.get("title", "")).strip()):
        return False
    return "may refer to" not in str(item.get("snippet", ""))[:120].lower()


def search_web(query: str, *, proxy: str | None = None, limit: int = 14,
               exclude: set[str] | None = None) -> list[dict[str, str]]:
    """Find readable articles about a topic, from several keyless sources.

    DuckDuckGo is tried first because it is a real search engine and finds the
    best match when it answers. On this machine it returns a bot challenge
    rather than results, so the work is done by Wikipedia's search API and a set
    of curated long-form RSS feeds -- both of which return direct article URLs
    and neither of which needs a key.

    Results carry the ``source`` they came from so the UI can say where a
    suggestion came from, and are de-duplicated by URL.

    ``exclude`` drops URLs the caller has already shown. That is what makes "these
    are not what I want, show me others" possible at all: re-asking the same
    sources the same question returns the same list, so the only honest way to
    offer different results is to ask for the ones further down.
    """
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    skip = {url.strip().rstrip("/") for url in (exclude or set()) if url.strip()}

    def add(item: dict[str, str]) -> None:
        url = item.get("url", "")
        key = url.strip().rstrip("/")
        if not url or key in seen or key in skip or not _is_readable(item):
            return
        seen.add(key)
        results.append(item)

    for item in _search_duckduckgo(query, proxy=proxy):
        add(item)
    for item in _search_wikipedia(query, proxy=proxy):
        add(item)
    for item in _search_feeds(query, proxy=proxy):
        add(item)

    return results[:limit]


def _search_duckduckgo(query: str, *, proxy: str | None) -> list[dict[str, str]]:
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    try:
        html_text, _ = fetch_url(url, proxy=proxy, timeout=20)
    except FetchError:
        return []
    out: list[dict[str, str]] = []
    for match in _RESULT_RE.finditer(html_text):
        link = _unwrap_ddg(html.unescape(match.group("href")))
        if not link.startswith("http"):
            continue
        snippet_match = _SNIPPET_RE.search(match.group("rest") or "")
        out.append({
            "url": link,
            "title": _plain(match.group("title")),
            "snippet": _plain(snippet_match.group(1)) if snippet_match else "",
            "site": urllib.parse.urlparse(link).netloc.removeprefix("www."),
            "source": "DuckDuckGo",
        })
        if len(out) >= 8:
            break
    return out


def _search_wikipedia(query: str, *, proxy: str | None) -> list[dict[str, str]]:
    """Wikipedia's search API. Encyclopedic, long, and cleanly extractable --
    which makes it the most reliable source of lesson material here."""
    api = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": query,
        "srlimit": "20", "format": "json", "srprop": "snippet|wordcount",
    })
    try:
        html_text, _ = fetch_url(api, proxy=proxy, timeout=20)
        data = json.loads(html_text)
    except (FetchError, json.JSONDecodeError):
        return []
    out: list[dict[str, str]] = []
    for hit in (data.get("query", {}).get("search") or []):
        title = str(hit.get("title", "")).strip()
        if not title:
            continue
        if int(hit.get("wordcount") or 0) < MIN_ARTICLE_WORDS:
            continue
        out.append({
            "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
            "title": title,
            "snippet": _plain(str(hit.get("snippet", ""))) + f" · {hit.get('wordcount', 0)} words",
            "site": "en.wikipedia.org",
            "source": "Wikipedia",
        })
    return out


def _search_feeds(query: str, *, proxy: str | None) -> list[dict[str, str]]:
    """Keyword-filter the curated feeds.

    Matching is on the feed's own title and summary, so "creativity" finds the
    Aeon and Quanta pieces about creativity without needing a search engine.
    Feeds with no match contribute nothing -- padding the list with unrelated
    recent items would make the feature feel broken.

    The feeds are independent, so they are fetched concurrently; a slow or dead
    feed costs one timeout rather than seven in sequence.
    """
    terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]
    if not terms:
        return []

    def one(feed: tuple[str, str, str]) -> list[dict[str, str]]:
        name, url, topic = feed
        try:
            text, _ = fetch_url(url, proxy=proxy, timeout=15)
            items = _parse_feed(text)
        except (FetchError, Exception):
            return []
        hits: list[tuple[int, dict[str, str]]] = []
        for item in items:
            haystack = f"{item['title']} {item['snippet']}".lower()
            matched = sum(1 for term in terms if term in haystack)
            # One matched word out of a three-word query is a coincidence, not
            # a result -- "language acquisition" should not surface an article
            # that merely says "language" somewhere.
            if matched < min(2, len(terms)):
                continue
            hits.append((matched, {
                "url": item["url"],
                "title": item["title"],
                "snippet": textwrap.shorten(item["snippet"], width=220, placeholder=" ..."),
                "site": urllib.parse.urlparse(item["url"]).netloc.removeprefix("www."),
                "source": f"{name} · {topic}",
            }))
        hits.sort(key=lambda pair: -pair[0])
        return [item for _, item in hits[:4]]

    with ThreadPoolExecutor(max_workers=len(FEEDS)) as pool:
        collected = list(pool.map(one, FEEDS))
    return [item for group in collected for item in group]


def _parse_feed(text: str) -> list[dict[str, str]]:
    """Read RSS or Atom with the stdlib parser. Returns title/url/snippet."""
    try:
        root = ET.fromstring(text.encode("utf-8", "replace"))
    except ET.ParseError:
        return []
    out: list[dict[str, str]] = []
    entries = list(root.iter("item")) or list(root.iter(f"{_ATOM}entry"))
    for entry in entries:
        title = (entry.findtext("title") or entry.findtext(f"{_ATOM}title") or "").strip()
        link = (entry.findtext("link") or "").strip()
        if not link:
            node = entry.find(f"{_ATOM}link")
            if node is not None:
                link = (node.get("href") or "").strip()
        summary = (
            entry.findtext("description")
            or entry.findtext(f"{_ATOM}summary")
            or entry.findtext(f"{_ATOM}content")
            or ""
        )
        if title and link.startswith("http"):
            out.append({"title": _clean_text(title), "url": link, "snippet": _plain(summary)})
    return out


def _unwrap_ddg(href: str) -> str:
    """DuckDuckGo wraps results in ``/l/?uddg=<encoded>`` redirects."""
    if "duckduckgo.com/l/" in href or href.startswith("//duckduckgo.com/l/"):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(
            href if href.startswith("http") else "https:" + href
        ).query)
        target = params.get("uddg")
        if target:
            return urllib.parse.unquote(target[0])
    if href.startswith("//"):
        return "https:" + href
    return href


def _plain(fragment: str) -> str:
    return _clean_text(html.unescape(_TAGS_RE.sub(" ", fragment)))


def _unwrap_ddg(href: str) -> str:
    """DuckDuckGo wraps results in ``/l/?uddg=<encoded>`` redirects."""
    if "duckduckgo.com/l/" in href or href.startswith("//duckduckgo.com/l/"):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(
            href if href.startswith("http") else "https:" + href
        ).query)
        target = params.get("uddg")
        if target:
            return urllib.parse.unquote(target[0])
    if href.startswith("//"):
        return "https:" + href
    return href


def _plain(fragment: str) -> str:
    return _clean_text(html.unescape(_TAGS_RE.sub(" ", fragment)))
