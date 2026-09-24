"""The corpus as a graph: which passages share a vocabulary.

The reading analytics answer "what have I read". This answers the question the
library cannot: **which passages are near each other**, and therefore which one
prepares you for which.

Two passages are joined when they share content words, weighted by how much of
the *smaller* one's vocabulary the larger one contains. That asymmetry is
deliberate -- Jaccard would rank a short article and a long one as dissimilar
however thoroughly the short one's words appear in the long one, and "everything
I need for this passage I already met there" is the relationship a reader can
act on.

Clusters come from label propagation over those weights: cheap, deterministic
enough for a graph this size, and it needs no parameter tuning, which matters
because a wrong cluster count is worse than a coarse one. Each cluster is named
by the words its members actually share rather than by a model's summary, so the
label is checkable.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from .diglot import Article
from .reading import windows_for

# A shared word has to be reasonably common to mean anything. With a threshold
# of one, two articles about anything at all are joined by "tiempo".
MIN_SHARED_TOKENS = 2

# Below this the two passages are only incidentally related. Measured against
# the real library after IDF weighting: 0.12 keeps the joins a reader would
# recognise and drops the coincidences.
MIN_EDGE_WEIGHT = 0.12

# How many connections each passage may have. A threshold alone cannot make a
# readable graph, because a few passages are weakly related to everything.
EDGES_PER_NODE = 5

# How many propagation rounds before accepting whatever has settled. Label
# propagation always terminates in practice on a graph this size; the cap is
# there so a pathological case cannot hang a request.
_MAX_ROUNDS = 12


@dataclass
class Node:
    slug: str
    title: str
    shelf: str
    words: int
    spanish_ratio: float
    lemmas: frozenset[str] = field(default_factory=frozenset, repr=False)
    # Filled in by the caller from the learner's deck, so the graph can show
    # what is within reach as well as what is related.
    coverage: float = 0.0
    cluster: int = -1

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug, "title": self.title, "shelf": self.shelf,
            "words": self.words, "spanish_ratio": round(self.spanish_ratio, 3),
            "coverage": round(self.coverage, 3), "cluster": self.cluster,
            "vocabulary": len(self.lemmas),
        }


@dataclass
class Edge:
    source: str
    target: str
    weight: float
    shared: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "target": self.target,
                "weight": round(self.weight, 3), "shared": self.shared[:8],
                "shared_count": len(self.shared)}


def content_lemmas(article: Article) -> frozenset[str]:
    """Every Spanish content word in a passage."""
    data = windows_for(article)
    lemmas: set[str] = set()
    for window in data.windows:
        lemmas.update(window.lemmas)
    return frozenset(lemmas)


def document_frequencies(node_lemmas: list[frozenset[str]]) -> dict[str, float]:
    """Inverse document frequency per lemma.

    Without this the graph is a hairball. Measured on the real library: every
    pair of passages shared at least two words, so 268 of 276 possible edges
    cleared the threshold and label propagation collapsed everything into one
    cluster. The culprit is words like *datos* and *ejemplo*, which appear in
    almost every article about anything.

    A word's evidence is how *rarely* it appears across the corpus, so that is
    what it contributes: a word in two passages out of twenty-four says a great
    deal about those two, and a word in twenty says nothing.
    """
    total = max(len(node_lemmas), 1)
    df: Counter[str] = Counter()
    for lemmas in node_lemmas:
        df.update(lemmas)
    return {lemma: math.log(total / count) for lemma, count in df.items()}


def similarity(left: frozenset[str], right: frozenset[str], idf: dict[str, float]) -> float:
    """How much of the smaller vocabulary the pair shares, weighted by rarity.

    Normalised by the smaller passage rather than the union: a two-hundred-word
    article whose every word appears in a three-thousand-word one is a perfect
    preparation for reading it, and Jaccard would score that pair as barely
    related.
    """
    if not left or not right:
        return 0.0
    smaller = left if len(left) <= len(right) else right
    mass = sum(idf.get(lemma, 0.0) for lemma in smaller)
    if mass <= 0:
        return 0.0
    shared = sum(idf.get(lemma, 0.0) for lemma in (left & right))
    return shared / mass


def keep_strongest(nodes: list[Node], edges: list[Edge], *, per_node: int = 5) -> list[Edge]:
    """Prune to each node's strongest connections.

    A similarity threshold alone cannot produce a readable graph, because a few
    passages are weakly related to everything and a few words are shared by
    everything. Keeping each node's top handful is the standard remedy and it
    makes clusters mean something: a node sits with the passages it is most
    like, not with all of them.

    An edge is dropped only when *both* ends are already full -- the union of
    each passage's nearest neighbours. Trying the stricter rule (drop as soon as
    either end is full) was measured and reverted: it cut the graph from 43
    edges to 18 and shattered the clusters into a dozen isolated nodes, because
    once the strongest edges are placed most nodes are saturated and everything
    remaining is discarded. The hub it leaves behind is real: one passage
    genuinely does share vocabulary with much of the corpus.
    """
    ranked = sorted(edges, key=lambda edge: -edge.weight)
    kept: list[Edge] = []
    degree: Counter[str] = Counter()
    for edge in ranked:
        if degree[edge.source] >= per_node and degree[edge.target] >= per_node:
            continue
        kept.append(edge)
        degree[edge.source] += 1
        degree[edge.target] += 1
    return kept


def label_propagate(nodes: list[Node], edges: list[Edge]) -> None:
    """Assign each node to a cluster by adopting its neighbours' label.

    Weighted by edge strength, ties broken by the lowest label so the result is
    stable across runs -- a graph that reshuffles its clusters on every page
    load is unreadable.
    """
    adjacency: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for edge in edges:
        adjacency[edge.source].append((edge.target, edge.weight))
        adjacency[edge.target].append((edge.source, edge.weight))

    labels = {node.slug: index for index, node in enumerate(nodes)}
    for _ in range(_MAX_ROUNDS):
        changed = False
        for node in nodes:
            neighbours = adjacency.get(node.slug)
            if not neighbours:
                continue
            tally: dict[int, float] = defaultdict(float)
            for other, weight in neighbours:
                tally[labels[other]] += weight
            best = max(sorted(tally.items()), key=lambda pair: (pair[1], -pair[0]))[0]
            if labels[node.slug] != best:
                labels[node.slug] = best
                changed = True
        if not changed:
            break

    # Renumber so the ids are dense and ordered by cluster size, which keeps
    # the colour assignment stable between requests.
    sizes = Counter(labels.values())
    ordered = {label: index for index, (label, _) in enumerate(sizes.most_common())}
    for node in nodes:
        node.cluster = ordered[labels[node.slug]]


def cluster_names(nodes: list[Node], edges: list[Edge], *, per_cluster: int = 4) -> dict[int, list[str]]:
    """Name each cluster by the words its members actually share.

    A cluster is named from its own contents rather than by asking a model to
    summarise it, so the reader can check the label against the passages and
    see whether it is fair.
    """
    cluster_of = {node.slug: node.cluster for node in nodes}
    shared_within: dict[int, Counter[str]] = defaultdict(Counter)
    for edge in edges:
        source, target = cluster_of.get(edge.source, -1), cluster_of.get(edge.target, -2)
        if source >= 0 and source == target:
            shared_within[source].update(edge.shared)

    return {
        node.cluster: names
        for node in nodes
        if node.cluster >= 0
        for names in [[word for word, _ in shared_within.get(node.cluster, Counter()).most_common(per_cluster)]]
    }


def topic_names(nodes: list[Node], *, per_cluster: int = 2) -> dict[int, list[str]]:
    """Name each cluster by the subject its members' *titles* share.

    The clusters were always topic-shaped -- on the live library one holds fifteen
    passages about art and another nine about language models -- but naming them
    from the vocabulary the members share *in their text* put ``escuela`` and
    ``archivado`` at the top of the art cluster: true, shared, and saying nothing
    about what the group is about.

    A title is the writer's own label for the subject, so a word several members
    have in common is the closest thing available here to a topic name, and the
    reader can check it by reading the titles in the cluster. Two members is the
    floor: one article mentioning "film" says nothing about the group.

    A word that is as common outside the cluster as inside names neither -- that is
    what keeps ``ai`` from labelling both of these groups when the whole library is
    about AI -- and the score is the difference between the two shares. The second
    word is only kept if it is nearly as characteristic as the first, because a
    topic name with a weak word in it reads as a keyword pile again.

    Returns an empty list for a cluster with no such subject, and the caller says
    so rather than printing words that mean nothing together.
    """
    if not nodes:
        return {}

    # The titles, with their source labels taken off: several end in
    # " - Wikipedia" or " | Quanta Magazine", and those words are the app's own
    # sources rather than anything about the subject. The rule is the shape of the
    # title -- a dash, pipe or em dash followed by up to three words -- rather than
    # a list of site names, so it holds for a source nobody has heard of yet.
    subjects = {node.slug: _subject_of(node.title) for node in nodes}
    words_of = {slug: _title_words(subject) for slug, subject in subjects.items()}
    stems_of = {slug: set(words.values()) for slug, words in words_of.items()}

    members: dict[int, list[str]] = defaultdict(list)
    for node in nodes:
        if node.cluster >= 0:
            members[node.cluster].append(node.slug)

    # stem -> the surface forms that produced it, and how many members' titles
    # contain the stem.
    forms: dict[int, dict[str, Counter[str]]] = {
        cluster: defaultdict(Counter) for cluster in members
    }
    for cluster, slugs in members.items():
        for slug in slugs:
            for surface, word in words_of[slug].items():
                forms[cluster][word][surface] += 1

    sizes = {cluster: len(slugs) for cluster, slugs in members.items()}
    total = sum(sizes.values())
    seen_in: dict[str, int] = Counter()
    for cluster, slugs in members.items():
        for word in forms[cluster]:
            seen_in[word] += sum(1 for slug in slugs if word in stems_of[slug])

    names: dict[int, list[str]] = {}
    for cluster, words in forms.items():
        size = sizes[cluster]
        inside_of = {word: sum(1 for slug in members[cluster] if word in stems_of[slug])
                     for word in words}
        scored: list[tuple[float, str]] = []
        for word, surfaces in words.items():
            inside = inside_of[word]
            if inside < 2:
                continue
            outside = (seen_in[word] - inside) / max(1, total - size)
            score = inside / size - outside
            if score <= 0:
                continue
            # The label is what a reader would recognise, not the stem that made
            # "language" and "languages" one word: "langua" is not a word.
            display = sorted(surfaces.items(),
                             key=lambda pair: (-pair[1], len(pair[0]), pair[0]))[0][0]
            scored.append((score, display))
        # Ties are broken towards the longer word: among equally characteristic
        # words the longer one carries more of the subject. This is what chooses
        # "language · models" over "language · large" for a cluster of pieces
        # about large language models, where both words appear in three titles
        # out of nine and only one of them is a thing.
        scored.sort(key=lambda pair: (-pair[0], -len(pair[1]), pair[1]))
        if scored:
            top = scored[0][0]
            names[cluster] = [word for score, word in scored if score >= top * 0.6][:per_cluster]
    return names


# A trailing source label: " - Wikipedia", " | Quanta Magazine", " — Nautilus".
_TITLE_TAIL = re.compile(r"\s+[-–—|]\s+\S+(?:\s+\S+){0,2}\s*$")


def _subject_of(title: str) -> str:
    """The title without its source label, when the label is at the end."""
    stripped = _TITLE_TAIL.sub("", title or "").strip()
    return stripped if len(stripped.split()) >= 2 else (title or "").strip()


def _title_words(text: str) -> dict[str, str]:
    """{surface form: stem} for the content words of a title."""
    from .glossary import STOPWORDS
    from .vocab import stem

    out: dict[str, str] = {}
    for word in _WORDS_RE.split((text or "").lower()):
        if len(word) >= 2 and word not in STOPWORDS:
            out.setdefault(word, stem(word))
    return out


_WORDS_RE = re.compile(r"[^0-9a-záéíóúüñ]+")


def build_graph(entries: Iterable[Any], coverage: dict[str, float]) -> dict[str, Any]:
    """Nodes, edges and clusters for the whole library.

    ``entries`` are the library's entries; ``coverage`` maps a slug to the
    share of its Spanish the learner already knows.
    """
    from .library import shelf_for

    nodes: list[Node] = []
    for entry in entries:
        article = entry.article
        stats = article.stats()
        shelf, _ = shelf_for(stats["spanish_ratio"])
        nodes.append(Node(
            slug=article.slug,
            title=article.title,
            shelf=shelf,
            words=stats["words"],
            spanish_ratio=stats["spanish_ratio"],
            lemmas=content_lemmas(article),
            coverage=coverage.get(article.slug, 0.0),
        ))

    idf = document_frequencies([node.lemmas for node in nodes])

    edges: list[Edge] = []
    for i, left in enumerate(nodes):
        for right in nodes[i + 1:]:
            shared = left.lemmas & right.lemmas
            if len(shared) < MIN_SHARED_TOKENS:
                continue
            weight = similarity(left.lemmas, right.lemmas, idf)
            if weight < MIN_EDGE_WEIGHT:
                continue
            # Most characteristic first: the rare words that make the pair
            # related are the ones worth studying to make the second passage
            # easier, where a word shared with everything is not.
            ranked = sorted(shared, key=lambda word: (-idf.get(word, 0.0), word))[:12]
            edges.append(Edge(left.slug, right.slug, weight, ranked))

    edges = keep_strongest(nodes, edges, per_node=EDGES_PER_NODE)
    label_propagate(nodes, edges)
    names = cluster_names(nodes, edges)
    topics = topic_names(nodes)

    return {
        "nodes": [node.to_dict() for node in nodes],
        "edges": [edge.to_dict() for edge in edges],
        "clusters": [
            {"id": cluster,
             "size": sum(1 for node in nodes if node.cluster == cluster),
             # The shared vocabulary the cluster was built from, and the subject
             # its titles share. The second is what the legend shows: the first is
             # how the grouping was arrived at, and reads like a word list.
             "terms": names.get(cluster, []),
             "topic": topics.get(cluster, [])}
            for cluster in sorted({node.cluster for node in nodes if node.cluster >= 0})
        ],
        "isolated": [node.slug for node in nodes if node.cluster < 0
                     or not any(e.source == node.slug or e.target == node.slug for e in edges)],
        "stats": {
            "nodes": len(nodes),
            "edges": len(edges),
            "clusters": len({node.cluster for node in nodes if node.cluster >= 0}),
            "density": round(len(edges) / max(len(nodes) * (len(nodes) - 1) / 2, 1), 3),
        },
    }
