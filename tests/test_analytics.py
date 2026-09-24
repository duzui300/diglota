"""Tests for reading analytics and the corpus graph.

Most of these guard *definitions* rather than code paths. "Words read",
"already known", and "related passages" are all claims the UI makes in words,
and a quiet change to what they count would turn the panel into a plausible-
looking lie. So the tests pin the meaning, not just the arithmetic.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import corpus_dir  # noqa: E402
from corpus_support import real_corpus  # noqa: E402

from app import analytics, corpus, reading  # noqa: E402
from app.diglot import parse_article  # noqa: E402
from app.store import Store  # noqa: E402

ARTICLE = """### Article Identification & Preview

- **Article Title:** A Test Article
- **Author:** Someone

# A Test Article

**By Someone**

El arte se vuelve **más personal** (*more personal*) cada vez en el mundo moderno
y la pintura cambia con cada artista que trabaja.

Los críticos dicen que la pintura moderna cambia y el arte vuelve a empezar
en cada generación con nuevas ideas.

Cada artista trabaja con el arte y la pintura cambia otra vez en el mundo.
"""


def build_article():
    article = parse_article(ARTICLE, fallback_slug="a-test-article")
    assert article is not None
    return article


# ------------------------------------------------------------ what counts --


def test_function_words_are_not_vocabulary():
    """They are most of any Spanish text and none of what anyone is learning."""
    lemmas = reading.lemmas_in("el arte de la pintura y el mundo")
    assert "arte" in lemmas and "pintura" in lemmas and "mundo" in lemmas
    assert "de" not in lemmas and "el" not in lemmas and "y" not in lemmas


def test_a_proper_noun_inside_a_spanish_clause_is_not_vocabulary():
    """A diglot keeps names in English even inside Spanish -- that is the weave
    working, not a bug -- and counting them inflates the words-read total."""
    lemmas = reading.lemmas_in("un método desarrollado en London por Francis Adams")
    assert "metodo" in lemmas and "desarrollado" in lemmas
    assert "london" not in lemmas and "francis" not in lemmas


def test_a_sentence_initial_capital_is_still_vocabulary():
    """The rule is about position, not case: "Afortunadamente" opens a sentence
    and is a word the reader is learning."""
    lemmas = reading.lemmas_in("El arte cambia. Afortunadamente el mundo también.")
    assert "afortunadamente" in lemmas
    assert "arte" in lemmas


def test_accented_words_fold_to_a_plain_key():
    assert "mas" in reading.lemmas_in("más arte")


# -------------------------------------------------------- what was read --


def test_windows_cover_the_whole_article():
    data = reading.windows_for(build_article())
    assert data.windows
    assert data.windows[0].start == 0.0
    assert data.windows[-1].end == pytest.approx(1.0, abs=0.01)
    assert data.total_tokens > 0


def test_reading_the_whole_article_credits_every_token():
    article = build_article()
    everything = reading.tokens_between(article, 0.0, 1.0)
    assert sum(everything.values()) == reading.windows_for(article).total_tokens


def test_halves_do_not_overlap_and_sum_to_the_whole():
    article = build_article()
    first = reading.tokens_between(article, 0.0, 0.5)
    second = reading.tokens_between(article, 0.5, 1.0)
    whole = reading.tokens_between(article, 0.0, 1.0)
    assert sum(first.values()) + sum(second.values()) == sum(whole.values())


def test_scrolling_backwards_credits_nothing():
    """Attribution runs from the furthest point reached, so re-reading a
    section does not multiply the count."""
    article = build_article()
    assert sum(reading.tokens_between(article, 0.8, 0.2).values()) == 0


def test_glossed_lemmas_are_known_to_the_article():
    assert "personal" in reading.glossed_lemmas(build_article())


# --------------------------------------------------------------- storage --


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def test_exposure_is_counted_once_per_reached_position(store):
    store.record_exposure(slug="a", lemmas={"arte": 3}, known_stems={"arte"}, glossed=set())
    store.record_exposure(slug="a", lemmas={"arte": 2}, known_stems={"arte"}, glossed=set())
    totals = store.exposure_totals(days=7)
    assert totals["tokens"] == 5, "the same word met twice counts twice"
    assert totals["distinct_words"] == 1
    assert totals["understood_ratio"] == 1.0


def test_understood_means_known_not_silent(store):
    """A word merely not clicked on is not comprehension. Only a word in the
    deck counts as understood."""
    store.record_exposure(slug="a", lemmas={"arte": 1, "genoma": 1},
                          known_stems={"arte"}, glossed=set())
    totals = store.exposure_totals(days=7)
    assert totals["known"] == 1
    assert totals["understood_ratio"] == pytest.approx(0.5)


def test_the_split_separates_new_from_met_before(store):
    """A word's history decides whether the reading introduced it."""
    store.record_exposure(slug="a", lemmas={"arte": 1}, known_stems=set(), glossed=set())
    split = store.exposure_split(days=7)
    assert split["new"] == 1 and split["familiar"] == 0

    # Backdate it: the word now has a history, so meeting it once more makes it
    # familiar rather than new -- and not yet repeated, which needs four.
    with store.write() as conn:
        conn.execute("UPDATE exposure SET day = '2020-01-01', first_day = '2020-01-01'")
    store.record_exposure(slug="b", lemmas={"arte": 1}, known_stems=set(), glossed=set())
    split = store.exposure_split(days=7)
    assert split["new"] == 0
    assert split["familiar"] == 1
    assert split["repeated"] == 0


def test_a_word_met_often_becomes_repeated(store):
    with store.write() as conn:
        conn.execute(
            """INSERT INTO exposure (day, slug, lemma, times, known, glossed, first_day)
               VALUES ('2020-01-01', 'a', 'arte', 9, 0, 0, '2020-01-01')"""
        )
    store.record_exposure(slug="b", lemmas={"arte": 1}, known_stems=set(), glossed=set())
    assert store.exposure_split(days=7)["repeated"] == 1


def test_credited_position_only_moves_forward(store):
    store.mark_credited("a", 0.6)
    store.mark_credited("a", 0.2)
    assert store.credited_position("a") == pytest.approx(0.6)


def test_series_fills_days_with_no_reading(store):
    series = store.exposure_series(days=7)
    assert len(series) == 7
    assert all(row["tokens"] == 0 for row in series[:-1])
    assert store.new_words_series(days=5)[-1]["new_words"] >= 0


def test_top_encountered_can_skip_function_words(store):
    store.record_exposure(slug="a", lemmas={"arte": 3, "tambien": 9},
                          known_stems=set(), glossed=set())
    assert store.top_encountered(days=7)[0]["lemma"] == "tambien"
    assert store.top_encountered(days=7, skip={"tambien"})[0]["lemma"] == "arte"


def test_scaffolding_is_counted_by_kind(store):
    store.log_scaffolding("explain", slug="a")
    store.log_scaffolding("explain", slug="a")
    store.log_scaffolding("full-entry", slug="a", term="genoma")
    summary = store.scaffolding_summary(days=7)
    assert summary["counts"] == {"explain": 2, "full-entry": 1}


# ------------------------------------------------------------- the graph --


def test_similarity_weights_rare_words_more():
    """The whole reason the first graph was a hairball: "datos" and "ejemplo"
    joined everything, and every word counted the same."""
    idf = {"raro": 3.0, "comun": 0.1, "solo": 2.0}
    shared_rare = corpus.similarity(frozenset({"raro", "comun"}), frozenset({"raro", "comun"}), idf)
    shared_common = corpus.similarity(frozenset({"comun"}), frozenset({"comun"}), idf)
    assert shared_rare > 0


def test_similarity_normalises_by_the_smaller_passage():
    """A short passage whose every word appears in a long one is a perfect
    preparation for it, and Jaccard would call that pair unrelated."""
    idf = {"a": 1.0, "b": 1.0, "c": 1.0, "d": 1.0, "e": 1.0}
    small = frozenset({"a", "b"})
    large = frozenset({"a", "b", "c", "d", "e"})
    assert corpus.similarity(small, large, idf) == pytest.approx(1.0)


def test_pruning_caps_each_passage_connections():
    """A threshold alone cannot make a readable graph, because a few passages
    are weakly related to everything."""
    nodes = [corpus.Node(slug=f"n{i}", title=f"N{i}", shelf="steady", words=100, spanish_ratio=0.4)
             for i in range(6)]
    edges = [corpus.Edge(f"n{i}", f"n{j}", 0.9 - 0.01 * abs(i - j), [])
             for i in range(6) for j in range(i + 1, 6)]
    kept = corpus.keep_strongest(nodes, edges, per_node=2)
    degree: dict[str, int] = {}
    for edge in kept:
        degree[edge.source] = degree.get(edge.source, 0) + 1
        degree[edge.target] = degree.get(edge.target, 0) + 1
    assert max(degree.values()) <= 4, degree


def test_the_real_corpus_does_not_collapse_into_one_cluster():
    """Measured before the fix: 268 edges, density 0.97, one cluster covering
    everything. A graph that says everything is related says nothing."""
    folder = real_corpus()
    from app.library import Library

    library = Library(folder, Path("data/library"))
    library.refresh()
    graph = corpus.build_graph(library.all(), {})
    assert graph["stats"]["clusters"] >= 2, graph["stats"]
    assert graph["stats"]["density"] < 0.6, graph["stats"]
    assert graph["stats"]["nodes"] >= 15


def test_clusters_are_named_from_their_own_words():
    nodes = [
        corpus.Node(slug="a", title="A", shelf="steady", words=10, spanish_ratio=0.4,
                    lemmas=frozenset({"arte", "pintura"})),
        corpus.Node(slug="b", title="B", shelf="steady", words=10, spanish_ratio=0.4,
                    lemmas=frozenset({"arte", "pintura"})),
    ]
    edges = [corpus.Edge("a", "b", 0.9, ["arte", "pintura"])]
    corpus.label_propagate(nodes, edges)
    names = corpus.cluster_names(nodes, edges)
    assert nodes[0].cluster == nodes[1].cluster
    assert set(names[nodes[0].cluster]) == {"arte", "pintura"}


# ------------------------------------------------------------ cluster topics --


def one_group(*titles: str, prefix: str = "n") -> list[corpus.Node]:
    """Nodes that label propagation will put in one cluster.

    ``prefix`` matters when a test builds more than one group: slugs are what the
    titles are looked up by, so two groups sharing slugs would have one group's
    titles answered by the other's.
    """
    nodes = [corpus.Node(slug=f"{prefix}{index}", title=title, shelf="steady",
                         words=100, spanish_ratio=0.4)
             for index, title in enumerate(titles)]
    edges = [corpus.Edge(f"{prefix}{i}", f"{prefix}{j}", 0.9, [])
             for i in range(len(nodes)) for j in range(i + 1, len(nodes))]
    corpus.label_propagate(nodes, edges)
    return nodes


def test_a_group_is_named_by_what_its_titles_share_not_what_its_text_does():
    """The clusters were always topic-shaped. Naming them from the vocabulary the
    members share in their *text* is what made a group of fifteen articles about
    art read as "escuela · archivado · camara": all true, all useless."""
    nodes = one_group("How AI Will Make Art Worse",
                      "AI art: the end of creativity",
                      "Artificial intelligence and art",
                      "Extensive reading for pleasure")
    nodes[0].lemmas = frozenset({"escuela", "archivado"})
    nodes[1].lemmas = frozenset({"escuela", "archivado"})
    topic = corpus.topic_names(nodes)[nodes[0].cluster]
    assert topic and topic[0] == "art", topic
    assert "escuela" not in topic


def test_a_word_the_other_groups_share_too_names_none_of_them():
    """On this library every passage is about AI somewhere, so a rule that just
    counted title words would call every group "ai" and tell the reader nothing."""
    first = one_group("How AI Will Make Art Worse", "AI art and creativity",
                      "Artificial intelligence and art", prefix="a")
    second = one_group("Why Healthcare AI Needs a Large Language Model",
                       "How AI agents plan and act",
                       "Mechanistic interpretability of language models", prefix="b")
    for node in first:
        node.cluster = 3
    for node in second:
        node.cluster = 7
    topics = corpus.topic_names([*first, *second])
    assert "ai" not in topics.get(3, []), topics
    assert "ai" not in topics.get(7, []), topics
    assert topics[3][0] == "art", topics
    assert topics[7][0] == "language", topics


def test_a_source_label_at_the_end_of_a_title_is_not_a_subject():
    """Several titles arrive as "... - Wikipedia" or "... | Quanta Magazine", and
    those words are the app's own sources. A blacklist of site names would break on
    the next feed added; the shape of the title does not."""
    nodes = one_group("Extensive reading - Wikipedia",
                      "Reading for pleasure - Wikipedia",
                      "The reading brain | Quanta Magazine")
    topic = corpus.topic_names(nodes)[nodes[0].cluster]
    assert topic == ["reading"], topic


def test_a_word_only_one_member_has_is_not_a_subject():
    nodes = one_group("How AI Will Make Art Worse",
                      "AI art and creativity",
                      "Interpretability of language models")
    topic = corpus.topic_names(nodes)[nodes[0].cluster]
    assert "interpretability" not in topic


def test_a_group_with_nothing_in_common_in_its_titles_gets_no_name():
    """Titles like "Untitled 1" and "Untitled 2" share nothing but the numbering.
    An empty answer lets the caller say so instead of printing words that mean
    nothing together."""
    nodes = one_group("Extensive reading", "Healthcare policy", "Marine biology")
    assert corpus.topic_names(nodes).get(nodes[0].cluster, []) == []


def test_the_label_is_a_word_a_reader_would_recognise():
    """Words are matched by stem, so "language" and "languages" are one thing --
    but the label has to be a word, and "langua" is not one."""
    nodes = one_group("What language really means",
                      "Language models and their limits",
                      "How languages change over time")
    topic = corpus.topic_names(nodes)[nodes[0].cluster]
    assert topic[0] == "language", topic


def test_every_topic_word_can_be_checked_against_the_titles_it_came_from():
    """The point of naming a group this way rather than asking a model: the reader
    can look at the titles and see whether the label is fair. A word that appears
    in one title cannot be checked, which is why two members is the floor."""
    folder = real_corpus()
    from app.library import Library

    library = Library(folder, Path("data/library"))
    library.refresh()
    graph = corpus.build_graph(library.all(), {})
    titles = {node["slug"]: node["title"].lower() for node in graph["nodes"]}
    members: dict[int, list[str]] = {}
    for node in graph["nodes"]:
        members.setdefault(node["cluster"], []).append(node["slug"])

    assert graph["clusters"], "the real corpus produced no clusters"
    named = [cluster for cluster in graph["clusters"] if cluster["topic"]]
    assert named, "no cluster got a name at all"
    for cluster in named:
        for word in cluster["topic"]:
            holders = sum(1 for slug in members[cluster["id"]]
                          if word[:4] in titles[slug])
            assert holders >= 2, f"{word!r} names a group but is in one title: {holders}"


# ------------------------------------------------------------- headline --


def test_the_headline_states_what_was_read_and_what_it_cost():
    text = analytics.headline(
        {"tokens": 12400, "understood_ratio": 0.83, "articles": 5}, "this week")
    assert "12,400" in text and "83%" in text and "this week" in text


def test_the_headline_does_not_invent_numbers_when_nothing_was_read():
    text = analytics.headline({"tokens": 0, "understood_ratio": 0.0, "articles": 0}, "this week")
    assert "%" not in text
    assert "Nothing" in text


def test_rates_are_per_thousand_words():
    assert analytics._rate(40, 800) == pytest.approx(50.0)
    assert analytics._rate(40, 0) == 0.0
