"""Tests for the pluggable language-pair layer (en-es, en-fr, cross-script en-zh)
and regression tests for bugs and security issues fixed during the refactor.
"""

from __future__ import annotations

import io
import json
import mimetypes
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import diglot, fetch, grading, quotes, reading, server, transfer, vocab  # noqa: E402
from app.api.routes.vocabulary import _csv_safe  # noqa: E402
from app.config import Settings, validate_base_url  # noqa: E402
from app.glossary import Glossary  # noqa: E402
from app.jobs import JobManager  # noqa: E402
from app.judge import Verdict  # noqa: E402
from app.languages import (  # noqa: E402
    CrossScriptSegmenter,
    EN_ES_PAIR,
    EN_FR_PAIR,
    EN_PROFILE,
    LanguagePair,
    LanguageProfile,
    get_pair,
    list_pairs,
    resolve_language_name,
)
from app.library import Library  # noqa: E402
from app.llm import LLMClient, LLMError  # noqa: E402
from app.store import Store  # noqa: E402
from app.warm import GlossWarmer  # noqa: E402


# ---------------------------------------------------- language pairs: en-fr --


def test_registry_ships_en_es_and_en_fr():
    ids = [p.id for p in list_pairs()]
    assert "en-es" in ids
    assert "en-fr" in ids
    assert get_pair("en-es") is EN_ES_PAIR
    assert get_pair("en-fr") is EN_FR_PAIR


def test_en_fr_segmentation_glossary_vocab_quotes_and_transfer():
    md = """### Article Identification & Preview

- **Article Title:** French Art and Reading
- **Author:** Claire
- **Language Pair:** en-fr

# French Art and Reading

The camera did not kill painting. **La peinture moderne** (*modern painting*)
a transformé notre regard sur la culture, et les artistes ont choisi une nouvelle
manière de comprendre le monde.
"""
    article = diglot.parse_article(md, fallback_slug="french-art")
    assert article is not None
    assert article.language_pair == "en-fr"
    assert article.base_lang == "en"
    assert article.target_lang == "fr"

    stats = article.stats()
    assert stats["target_lang"] == "fr"
    assert stats["target_words"] > 0
    assert stats["target_ratio"] > 0.3

    focus = article.focus_pairs()
    assert any("peinture moderne" in f.es.lower() and f.en == "modern painting" for f in focus)

    glossary = Glossary(pair=EN_FR_PAIR).build([article], [])
    hit = glossary.lookup("peinture", article=article.slug, pair=EN_FR_PAIR)
    assert hit is not None and "modern painting" in hit.gloss

    cov = vocab.text_coverage(article, [{"term": "peinture", "lemma": "peinture"}], pair=EN_FR_PAIR)
    assert cov["tokens"] > 0 and cov["known_types"] >= 1

    # Quote extraction respects target_lang='fr'
    p_idx = next(i for i, b in enumerate(article.blocks) if b.kind == "p")
    quote, why = quotes.locate(article, p_idx, "La peinture moderne a transformé notre regard sur la culture")
    assert why == "" and quote is not None
    assert "peinture" in quote.es.lower()

    # Transfer round-trip preserves Language Pair front matter
    exported = transfer.share_text(article, creator="tester")
    assert "**Language Pair:** en-fr" in exported
    reparsed = diglot.parse_article(exported, fallback_slug="french-art-2")
    assert reparsed is not None and reparsed.language_pair == "en-fr"


# -------------------------------------------- language pairs: cross-script --


def test_cross_script_en_zh_pair_parses_mixed_and_unspaced_text():
    zh_profile = LanguageProfile(
        code="zh",
        name="Chinese",
        native_name="中文",
        script="cjk",
        word_spaced=False,
        cased=False,
        proficiency_scale="HSK",
        word_pattern=r"[\u4e00-\u9fff]+",
        stopwords=frozenset({"的", "了", "在", "是"}),
        function_words=frozenset({"的", "了", "在", "是"}),
        min_word_length=1,
        min_stem_length=1,
        min_source_length=1,
        tts_locales=("zh-CN", "zh"),
        tts_rate=0.95,
    )
    en_zh = LanguagePair(
        id="en-zh",
        base=EN_PROFILE,
        target=zh_profile,
        segmenter=CrossScriptSegmenter(
            base_code="en", target_code="zh", base_script="latin", target_script="cjk"
        ),
        shelves=(
            ("gentle", 0.0, 0.45, "Mostly English, Chinese in short phrases"),
            ("steady", 0.45, 0.70, "Balanced English and Chinese"),
            ("immersion", 0.70, 1.01, "Chinese-dominant"),
        ),
    )

    spans = diglot.parse_spans(
        "Reading **语言学习** (*language learning*) helps build fluency every day.",
        pair=en_zh,
    )
    target_spans = [s for s in spans if s.lang == "zh"]
    assert target_spans, spans
    assert target_spans[0].text.strip() == "语言学习"
    assert target_spans[0].target is True
    assert target_spans[0].gloss == "language learning"

    # Unspaced script boundary splits cleanly too
    unspaced = diglot.parse_spans("Read中文now", pair=en_zh)
    assert [(s.lang, s.text) for s in unspaced] == [("en", "Read"), ("zh", "中文"), ("en", "now")]


# ---------------------------------------------------------- core bug fixes --


def test_grading_tutor_quality_is_not_clamped_to_one():
    class FakeJudge:
        available = False

        def grade_translation(self, **kwargs):
            return Verdict(error="no key")

        def grade_writing(self, **kwargs):
            return Verdict(error="no key")

    class FakeClient:
        def __init__(self, payload):
            self.payload = payload

        def complete_json(self, prompt, **kwargs):
            return self.payload, {}

    class FakeTutor:
        def __init__(self, payload):
            self.client = FakeClient(payload)

    g_tr = grading.Grading(FakeJudge(), FakeTutor({"meaning": 0.9, "grammar": 0.9, "natural": 0.85, "quality": 3.5}))
    v_tr = g_tr.grade_translation(source="a", reference="b", attempt="c")
    assert v_tr.score == pytest.approx(3.5)

    g_wr = grading.Grading(FakeJudge(), FakeTutor({"well_formed": 0.9, "range": 0.8, "did_the_task": 0.9, "quality": 4}))
    v_wr = g_wr.grade_writing(text="texto", prompt="p", focus=[])
    assert v_wr.score == pytest.approx(4.0)


def test_llm_does_not_leak_reasoning_content_on_empty_content(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout=None):
        body = json.loads(request.data.decode())
        calls.append(body)
        if len(calls) == 1:
            payload = {
                "model": "reasoner",
                "choices": [{
                    "message": {"content": "", "reasoning_content": "secret chain of thought"},
                    "finish_reason": "length",
                }],
                "usage": {"completion_tokens_details": {"reasoning_tokens": 500}},
            }
        else:
            payload = {
                "model": "reasoner",
                "choices": [{"message": {"content": "Final answer"}, "finish_reason": "stop"}],
                "usage": {},
            }
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr("app.llm.urllib.request.urlopen", fake_urlopen)
    client = LLMClient("https://example.invalid/v1", "k", "reasoner", max_attempts=1)
    result = client.complete("hi", max_tokens=500)
    assert result.text == "Final answer"
    assert len(calls) == 2
    assert calls[1]["max_tokens"] > 500


def test_word_re_excludes_inverted_punctuation_while_word_score_keeps_signal():
    assert diglot._WORD_RE.findall("¿Qué pasa? ¡Hola!") == ["Qué", "pasa", "Hola"]
    assert diglot.word_score("¿Qué") > 0


def test_quotes_sentences_does_not_merge_lowercase_single_letter_endings():
    found = quotes.sentences("We chose plan a. Next sentence starts here.")
    assert len(found) == 2, [s.text for s in found]
    assert found[0].text == "We chose plan a."
    assert found[1].text == "Next sentence starts here."


def test_library_add_article_bumps_revision_and_clears_coverage(tmp_path):
    lib = Library(tmp_path / "corpus", tmp_path / "library")
    lib.refresh()
    before = lib.revision
    art = diglot.parse_article("# Title\n\nEl arte crece.", fallback_slug="t")
    assert art is not None
    lib.add_article("t", art, path=tmp_path / "library" / "t.md")
    assert lib.revision == before + 1
    assert lib.get("t").path == tmp_path / "library" / "t.md"


# ------------------------------------------------ storage & jobs bug fixes --


def test_store_new_words_series_counts_each_lemma_once(tmp_path):
    store = Store(tmp_path / "test.db")
    try:
        # Same lemma met in two articles on the same day -> 1 new word, not 2!
        store.record_exposure(slug="a", lemmas={"pintura": 3}, known_stems=set(), glossed=set())
        store.record_exposure(slug="b", lemmas={"pintura": 2, "arte": 1}, known_stems=set(), glossed=set())
        series = store.new_words_series(days=7)
        assert sum(row["new_words"] for row in series) == 2

        # Verify schema version and language_pair column
        ver = store.conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver == 2
        cols = {row[1] for row in store.conn.execute("PRAGMA table_info(words)")}
        assert "language_pair" in cols
    finally:
        store.close()


def test_job_manager_restores_timestamps_and_stops_sse_cleanly(tmp_path):
    store = Store(tmp_path / "jobs.db")
    try:
        store.save_job({
            "id": "old123",
            "kind": "import",
            "title": "Old Job",
            "status": "done",
            "step": "done",
            "done": 1,
            "total": 1,
            "detail": "",
            "error": None,
            "result": {"ok": True},
            "created_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:01:00+00:00",
        })
        mgr = JobManager(workers=1, store=store)
        reloaded = mgr.get("old123")
        assert reloaded is not None
        assert reloaded.to_dict()["elapsed"] == pytest.approx(60.0, abs=0.2)

        # SSE stream terminates immediately when mgr.stop() is called
        stream = iter(mgr.event_stream(heartbeat=30.0))
        first = next(stream)
        assert "snapshot" in first
        t = threading.Timer(0.05, mgr.stop)
        t.start()
        started = time.monotonic()
        rest = list(stream)
        elapsed = time.monotonic() - started
        assert rest == []
        assert elapsed < 2.0
    finally:
        store.close()


def test_warmer_detects_error_dict_and_does_not_mark_done():
    art = diglot.parse_article("# Title\n\nEl hipopótamo camina.", fallback_slug="h")
    assert art is not None
    glossary = Glossary()
    warmer = GlossWarmer(workers=1)
    try:
        warmer._one(("h", "hipopotamo"), "hipopótamo", art,
                    lambda w, s: {"term": w, "error": "down", "retryable": True})
        assert warmer.stats()["failed"] == 1
        assert warmer.stats()["warmed"] == 0
        assert ("h", "hipopotamo") not in warmer._done
    finally:
        warmer.stop()


# ----------------------------------------- server, security & MIME fixes ---


def test_recommend_endpoint_returns_serializable_job_id(tmp_path, monkeypatch):
    for name in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    app_inst = server.App(Settings(corpus_dir=tmp_path / "c", data_dir=tmp_path / "d"))
    app_inst.tutor = object()
    monkeypatch.setattr(server, "ctx", lambda: app_inst)
    try:
        resp = server.start_recommend()
        assert isinstance(resp["job_id"], str) and resp["job_id"]
        json.dumps(resp)  # must be JSON serializable!
    finally:
        app_inst.jobs.stop()
        app_inst.store.close()


def test_deep_lookup_failure_preserves_existing_cached_gloss(tmp_path, monkeypatch):
    for name in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    app_inst = server.App(Settings(corpus_dir=tmp_path / "c", data_dir=tmp_path / "d"))

    class FailingTutor:
        def gloss_word(self, **kwargs):
            raise LLMError("outage")

    app_inst.tutor = FailingTutor()
    try:
        from app.tutor import cache_key

        app_inst.store.cache_put(cache_key("gloss", "anales"), {"gloss": "annals", "lemma": "anal"})
        failed = app_inst.resolve_word("anales", "", None, deep=True)
        assert failed.get("error")
        # Subsequent normal lookup still gets the valid cached gloss!
        still_good = app_inst.resolve_word("anales", "", None, deep=False)
        assert still_good.get("gloss") == "annals"
    finally:
        app_inst.jobs.stop()
        app_inst.store.close()


def test_language_endpoint_and_translate_target_resolution(tmp_path, monkeypatch):
    for name in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    app_inst = server.App(Settings(corpus_dir=tmp_path / "c", data_dir=tmp_path / "d"))
    monkeypatch.setattr(server, "ctx", lambda: app_inst)
    try:
        data = server.language_pairs()
        assert data["active"] == "en-es"
        assert {p["id"] for p in data["pairs"]} >= {"en-es", "en-fr"}
        assert resolve_language_name("fr") == "French"
        assert resolve_language_name("Chinese") == "Chinese"
        assert resolve_language_name("en") == "English"
    finally:
        app_inst.jobs.stop()
        app_inst.store.close()


@pytest.mark.parametrize("bad_url", [
    "http://127.0.0.1:8787/api/reset",
    "http://localhost/admin",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.1/internal",
    "http://198.18.0.1/raw-fake-ip",
    "http://[::1]:8000/",
    "ftp://example.com/file",
    "file:///etc/passwd",
])
def test_fetch_blocks_ssrf_and_non_http_urls(bad_url):
    with pytest.raises(fetch.FetchError):
        fetch.fetch_url(bad_url)


def test_fetch_allows_clash_tun_fake_ip_for_public_hostnames(monkeypatch):
    """Transparent TUN proxies (Clash / Mihomo / Sing-box) map public domains to
    198.18.0.0/15 Fake-IPs. DNS-resolved public hostnames must pass while real
    LAN/loopback/metadata targets remain blocked."""
    monkeypatch.setattr(
        fetch.socket, "getaddrinfo",
        lambda host, port, **kw: [(fetch.socket.AF_INET, fetch.socket.SOCK_STREAM, 6, "", ("198.18.0.154", 443))],
    )
    parsed = fetch._validate_public_url("https://en.wikipedia.org/wiki/Reading")
    assert parsed.hostname == "en.wikipedia.org"

    # A hostname resolving to loopback or cloud metadata must still be rejected.
    monkeypatch.setattr(
        fetch.socket, "getaddrinfo",
        lambda host, port, **kw: [(fetch.socket.AF_INET, fetch.socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80))],
    )
    with pytest.raises(fetch.FetchError):
        fetch._validate_public_url("https://rebind.example/meta")


def test_security_helpers_base_url_csv_and_js_mime():
    assert validate_base_url("https://api.example.com/v1/") == "https://api.example.com/v1"
    with pytest.raises(ValueError):
        validate_base_url("javascript:alert(1)")
    with pytest.raises(ValueError):
        validate_base_url("file:///tmp")

    assert _csv_safe("=CMD|' /C calc'!A0").startswith("'")
    assert _csv_safe("+1+1").startswith("'")
    assert _csv_safe("hola") == "hola"

    mime, _ = mimetypes.guess_type("main.js")
    assert mime in ("text/javascript", "application/javascript")


# ------------------------------------ lookup, cache & weave overhaul tests ---


def test_llm_strips_think_blocks_and_parses_cached_tokens(monkeypatch):
    import io
    from app.llm import LLMClient

    def fake_urlopen(request, timeout=None):
        body = json.loads(request.data.decode())
        assert "reasoning_effort" not in body, "local/none effort should omit reasoning_effort"
        payload = {
            "model": "qwen3:4b",
            "choices": [{
                "message": {"content": '<think>internal chain of thought</think>\n{"gloss": "cat", "lemma": "gato"}'},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 200,
                "completion_tokens": 30,
                "prompt_tokens_details": {"cached_tokens": 150},
            },
        }
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr("app.llm.urllib.request.urlopen", fake_urlopen)
    client = LLMClient("http://localhost:11434/v1", "", "qwen3:4b", max_attempts=1)
    assert client.is_local is True
    data, res = client.complete_json("Word: gato", effort="none")
    assert data == {"gloss": "cat", "lemma": "gato"}
    assert res.cached_tokens == 150


def test_gloss_word_uses_prefix_cache_system_and_local_timeout():
    from app.tutor import LOCAL_GLOSS_TIMEOUT, Tutor

    recorded: dict[str, object] = {}

    class StubLocalClient:
        is_local = True
        model = "qwen3:4b"

        def complete_json(self, prompt, **kwargs):
            recorded["prompt"] = prompt
            recorded.update(kwargs)
            return {"lemma": "hablar", "gloss": "to speak"}, None

    out = Tutor(StubLocalClient()).gloss_word(
        word="hablaba", sentence="Ella hablaba despacio.", article_title="Demo"
    )
    assert out["lemma"] == "hablar"
    assert '"lemma"' in str(recorded["system"]) and '"lemma"' not in str(recorded["prompt"])
    assert recorded["timeout"] == LOCAL_GLOSS_TIMEOUT
    assert recorded["attempts"] == 1
    assert recorded["effort"] == "none"


def test_lookup_fold_lemma_cache_singleflight_and_warmer_no_poison(tmp_path, monkeypatch):
    import threading
    import time

    for name in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "TYPESAFE_API_KEY", "DIGLOT_WARM_GLOSSES"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(corpus_dir=tmp_path / "c", data_dir=tmp_path / "d")
    assert settings.should_warm_glosses() is False
    app_inst = server.App(settings)
    assert app_inst.warmer is None

    calls = 0

    class SlowTutor:
        def __init__(self):
            self.fail = False

        def gloss_word(self, *, word, sentence, article_title):
            nonlocal calls
            calls += 1
            if self.fail:
                raise LLMError("timed out")
            time.sleep(0.05)
            return {"lemma": "hablar", "display": word, "pos": "verb", "gloss": "to speak"}

    stub = SlowTutor()
    app_inst.tutor = stub
    try:
        # 1. Warmer failure must NOT poison the cache for real user clicks.
        stub.fail = True
        warm_fail = app_inst.resolve_word("caminar", "", None, from_warmer=True)
        assert warm_fail.get("error")
        stub.fail = False
        user_ok = app_inst.resolve_word("caminar", "", None)
        assert user_ok["gloss"] == "to speak"

        # 2. Singleflight coalesces concurrent clicks on the same word into 1 call.
        before = calls
        results: list[dict] = []
        t1 = threading.Thread(target=lambda: results.append(app_inst.resolve_word("hablaba", "", None)))
        t2 = threading.Thread(target=lambda: results.append(app_inst.resolve_word("Hablaba!", "", None)))
        t1.start(); t2.start(); t1.join(); t2.join()
        assert calls - before == 1, "concurrent clicks must coalesce into a single LLM call"
        assert len(results) == 2 and all(r["gloss"] == "to speak" for r in results)

        # 3. Lemma cached from 'hablaba' answers 'hablar' without hitting the model.
        before = calls
        lemma_hit = app_inst.resolve_word("hablar", "", None)
        assert lemma_hit["gloss"] == "to speak" and lemma_hit["instant"] is True
        assert calls == before

        # 4. fresh=True bypasses a user failure cache entry and retries immediately.
        stub.fail = True
        assert app_inst.resolve_word("desconocido", "", None).get("error")
        stub.fail = False
        retried = app_inst.resolve_word("desconocido", "", None, fresh=True)
        assert retried["gloss"] == "to speak"
    finally:
        app_inst.jobs.stop()
        app_inst.store.close()


def test_weave_best_of_n_model_cache_key_and_json_paragraphs():
    from app import ingest, levels
    from app.llm import LLMResult

    # _split_paragraphs handles JSON {"paragraphs": [...]} and strips <think>
    parsed = ingest._split_paragraphs('<think>plan</think>\n{"paragraphs": ["Para one.", "Para two."]}')
    assert parsed == ["Para one.", "Para two."]

    # _prose_word_count excludes inline glosses (*...*)
    assert ingest._prose_word_count("La casa (*the big red house*) es grande.") == 4

    # weave_cache_key varies with model
    auto = levels.resolve_level("auto")
    k1 = ingest.weave_cache_key("Hello world.", [], 0.38, auto, "chunk", model="small-m")
    k2 = ingest.weave_cache_key("Hello world.", [], 0.38, auto, "chunk", model="big-m")
    assert k1 != k2

    # Best-of-N keeps attempt 1 (closer to 0.50) even when attempt 2 & 3 oscillate worse.
    close_ish = ("El gato está en la casa y el perro duerme tranquilo. " * 2
                 + "The cat is in the house and the dog sleeps quietly in the garden today. " * 4)
    way_off = "The cat is in the house and the dog sleeps quietly in the garden today. " * 6

    class OscillatingClient:
        model = "test-model"
        is_local = False

        def __init__(self):
            self.calls = 0

        def complete(self, prompt, **kwargs):
            self.calls += 1
            text = close_ish if self.calls == 1 else way_off
            return LLMResult(text=text, prompt_tokens=100, completion_tokens=50, cached_tokens=40)

    class StubTutor:
        def __init__(self):
            self.client = OscillatingClient()

    chunk = ("The cat is in the house and the dog sleeps quietly in the garden today. " * 6).strip()
    usage = ingest._UsageCounter()
    res = ingest.weave_chunk(StubTutor(), chunk=chunk, focus=[], target=0.50, level=auto, usage=usage)
    assert res.ratio > 0.15, "must retain closer attempt 1 instead of 0% attempt 3"
    assert any("kept best attempt" in n for n in res.notes)
    assert usage.to_dict()["cached_tokens"] > 0


def test_import_article_caches_focus_and_anchors_and_reports_usage(tmp_path):
    from app import ingest
    from app.llm import LLMResult

    class MemCache:
        def __init__(self):
            self.data = {}

        def get(self, k):
            return self.data.get(k)

        def put(self, k, v):
            self.data[k] = v

    focus_calls = 0
    weave_calls = 0
    anchor_calls = 0

    woven_para = (
        "La lectura extensiva es una herramienta muy útil para aprender idiomas. "
        "Extensive reading lets learners encounter familiar words in rich context, "
        "y poco a poco la comprensión mejora sin esfuerzo constante. "
        "Over time, repeated exposure builds fluency and confidence in natural prose. " * 5
    ).strip()

    class FakeClient:
        model = "cloud-big"
        is_local = False

        def complete_json(self, prompt, **kwargs):
            nonlocal focus_calls, anchor_calls
            if '"items"' in prompt:
                focus_calls += 1
                return (
                    {"items": [{"es": "poco a poco", "en": "little by little", "kind": "phrase", "why": "useful"}]},
                    LLMResult(text="{}", prompt_tokens=300, completion_tokens=80, cached_tokens=0),
                )
            anchor_calls += 1
            return (
                {"vocab": [{"term": "poco a poco", "gloss": "little by little", "examples": ["poco a poco"]}],
                 "grammar": [{"title": "Ser", "example": "La lectura es útil.", "explanation": "Description."}]},
                LLMResult(text="{}", prompt_tokens=400, completion_tokens=120, cached_tokens=200),
            )

        def complete(self, prompt, **kwargs):
            nonlocal weave_calls
            weave_calls += 1
            return LLMResult(text=woven_para, prompt_tokens=500, completion_tokens=200, cached_tokens=300)

    class FakeTutor:
        def __init__(self):
            self.client = FakeClient()

    source_text = (
        "Extensive Reading Works\n\n"
        + ("Extensive reading lets learners encounter familiar words in rich context and build fluency over time. " * 12)
    )
    settings = Settings(corpus_dir=tmp_path / "c", data_dir=tmp_path / "d")
    cache = MemCache()

    first = ingest.import_article(
        text=source_text, settings=settings, tutor=FakeTutor(),
        level_code="B1", ratio=0.40, weave="chunk", cache=cache,
    )
    assert focus_calls == 1 and weave_calls == 1 and anchor_calls == 1
    assert first["usage"]["total_tokens"] == (300 + 80 + 500 + 200 + 400 + 120)
    assert first["usage"]["cached_tokens"] == 500

    # Re-running the same import must hit focus, chunk, and anchor caches (0 new LLM calls!).
    second = ingest.import_article(
        text=source_text, settings=settings, tutor=FakeTutor(),
        level_code="B1", ratio=0.40, weave="chunk", cache=cache,
    )
    assert focus_calls == 1 and weave_calls == 1 and anchor_calls == 1
    assert second["reused"] == 1
    assert second["usage"]["cache_hits"] == 3
    assert second["usage"]["llm_calls"] == 0


def test_llm_sse_streaming_and_http_400_param_autofallback(monkeypatch):
    import io
    import urllib.error
    from app.llm import LLMClient

    # 1. SSE streaming parses deltas, strips <think>, and reads usage.
    sse_body = (
        b'data: {"model":"stream-m","choices":[{"delta":{"content":"<think>hmm</think>Hola "}}]}\n\n'
        b'data: {"model":"stream-m","choices":[{"delta":{"content":"mundo."},"finish_reason":"stop"}]}\n\n'
        b'data: {"model":"stream-m","choices":[],"usage":{"prompt_tokens":120,"completion_tokens":15,"prompt_tokens_details":{"cached_tokens":90}}}\n\n'
        b'data: [DONE]\n\n'
    )

    class FakeSSEResponse(io.BytesIO):
        headers = {"Content-Type": "text/event-stream; charset=utf-8"}

    monkeypatch.setattr("app.llm.urllib.request.urlopen", lambda req, timeout=None: FakeSSEResponse(sse_body))
    client = LLMClient("https://api.example.com/v1", "sk-1234567890", "stream-m", max_attempts=1)
    res = client.complete("hello", stream=True)
    assert res.text == "Hola mundo."
    assert res.prompt_tokens == 120 and res.cached_tokens == 90

    # 2. HTTP 400 rejecting reasoning_effort and max_tokens auto-adapts and succeeds.
    seen_payloads: list[dict] = []

    def fake_urlopen_400(req, timeout=None):
        body = json.loads(req.data.decode())
        seen_payloads.append(body)
        if "reasoning_effort" in body:
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {},
                io.BytesIO(b'{"error": "unsupported parameter: reasoning_effort"}'),
            )
        if body.get("max_tokens", 0) > 8192:
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {},
                io.BytesIO(b'{"error": "max_tokens must be <= 8192"}'),
            )
        return io.BytesIO(json.dumps({
            "model": "strict-m",
            "choices": [{"message": {"content": "Recovered cleanly"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5},
        }).encode())

    monkeypatch.setattr("app.llm.urllib.request.urlopen", fake_urlopen_400)
    strict = LLMClient("https://api.example.com/v1", "sk-1234567890", "strict-m", max_attempts=1)
    ok = strict.complete("hello", max_tokens=12000, effort="low")
    assert ok.text == "Recovered cleanly"
    assert len(seen_payloads) == 3
    assert "reasoning_effort" not in seen_payloads[-1]
    assert seen_payloads[-1]["max_tokens"] == 8192


def test_weave_chunk_fails_fast_on_fatal_llm_error():
    from app import ingest, levels

    calls = 0

    class FatalClient:
        model = "bad-m"
        is_local = False

        def complete(self, prompt, **kwargs):
            nonlocal calls
            calls += 1
            raise LLMError("HTTP 401: invalid api key", status_code=401, fatal=True)

    class StubTutor:
        client = FatalClient()

    with pytest.raises(ingest.ImportError_, match="401"):
        ingest.weave_chunk(
            StubTutor(),
            chunk="Paragraph one.\nParagraph two.\nParagraph three.",
            focus=[],
            target=0.40,
            level=levels.resolve_level("B1"),
        )
    assert calls == 1, "fatal error must fail immediately instead of looping retries and paragraphs"


def test_sentence_mode_spread_ignores_single_sentence_and_quoted_paragraphs():
    from app import ingest

    # News-style prose: one 32-word single-sentence Spanish lede (100% ES) and one
    # 60-word direct-quote English paragraph (0% ES). Sentence-mode spread must NOT
    # trigger a false 100% spread rewrite.
    single_sent_es = (
        "Los laboratorios de inteligencia artificial han sido criticados anteriormente por "
        "apresurarse a publicar resultados matemáticos innovadores reclamando gloria mientras "
        "dejan a la comunidad académica digerir los resultados complejos y limpiar errores."
    )
    quoted_en = (
        '“No human being can reasonably claim to be responsible for, or even to understand, '
        'such an enormous machine-generated mathematical proof document,” the association wrote '
        'in a statement on Friday. It complained that the announcement “builds upon over a '
        'century of human work, but fails to advance mathematical understanding in the same way.”'
    )
    assert ingest._spread([single_sent_es, quoted_en], floor=55, sentence_mode=True) == 0.0
