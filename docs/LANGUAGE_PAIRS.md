# Extending Diglota to New Language Pairs

Diglota is built around a pluggable language-pair layer in [`packages/core/app/languages/`](../packages/core/app/languages/) and a lightweight client companion in [`packages/ui/static/js/lib/language.js`](../packages/ui/static/js/lib/language.js).

Out of the box, the registry ships two pairs:

- **`en-es`** (English → Spanish) — default pair, calibrated against the full reading corpus.
- **`en-fr`** (English → French) — second built-in same-script pair demonstrating multi-pair registration.

This guide explains how every layer participates when you add another language pair — both **same-script** pairs (e.g., `en-de`, `en-it`, `en-pt`) and **cross-script** pairs (e.g., `en-zh`, `zh-en`, `en-ja`, `en-ko`, `en-ru`).

---

## 1. Architecture Overview

Every language-specific rule flows through three core abstractions in [`packages/core/app/languages/types.py`](../packages/core/app/languages/types.py):

| Abstraction | Responsibility |
|---|---|
| `LanguageProfile` | Single-language metadata & NLP hooks: ISO code, script (`latin`, `cjk`, `kana`, `hangul`, `cyrillic`, `arabic`, `greek`), `word_spaced`, `cased`, `nouns_capitalized`, `proficiency_scale` (`CEFR`, `HSK`, `JLPT`, …), `word_pattern`, auto-folded `stopwords` & `function_words`, `derived_suffixes`, sentence boundary regex & case-sensitive abbreviations, TTS locale priority & speaking rate, and optional custom `stemmer` / `tokenizer` / `folder`. |
| `DiglotSegmenter` | Classifies mixed prose into base (`0`) vs. target (`1`) runs. Two built-in implementations cover almost all pairs:<br>• **`SameScriptHMMSegmenter`** — two-state Viterbi HMM with lexicons, diacritics, and suffixes (for `en-es`, `en-fr`, `en-de`, …).<br>• **`CrossScriptSegmenter`** — zero-lexicon Unicode script classifier (for `en-zh`, `zh-en`, `en-ja`, `en-ko`, `en-ru`, `en-ar`). |
| `LanguagePair` | Combines `base: LanguageProfile`, `target: LanguageProfile`, `segmenter`, difficulty `shelves`, web discovery settings (`accept_language`, `wikipedia_host`, `default_feeds`), and tutor prompt helpers (`system_prompt()`, `translation_system_prompt()`). |

All pairs are registered in `LanguageRegistry` ([`packages/core/app/languages/registry.py`](../packages/core/app/languages/registry.py)) and exposed to the UI over `GET /api/languages` and `GET /api/status`.

---

## 2. Step-by-Step: Adding a Same-Script Pair (e.g., `en-de`)

Use [`packages/core/app/languages/en_fr.py`](../packages/core/app/languages/en_fr.py) as your template:

1. **Create `packages/core/app/languages/en_de.py`:**
   - Define `DE_PROFILE = LanguageProfile(...)`:
     - Set `nouns_capitalized=True` (important for German so `is_proper_noun()` does not filter out capitalized mid-sentence nouns!).
     - Provide `word_pattern` covering `ÄÖÜäöüß`, `accent_chars`, `stopwords`, `function_words`, `derived_suffixes`, `abbreviations`, `tts_locales=("de-DE", "de-AT", "de-CH", "de")`, and a lightweight `german_stem` callable.
     - Note: `LanguageProfile.__post_init__` automatically folds all `stopwords` and `function_words`, so accented entries never fail to match folded tokens.
   - Define `EN_DE_SEGMENTER = SameScriptHMMSegmenter(...)` with German/English word lists, ambiguous homographs, diacritics, and characteristic suffixes.
   - Define `EN_DE_PAIR = LanguagePair(id="en-de", base=EN_PROFILE, target=DE_PROFILE, segmenter=EN_DE_SEGMENTER, shelves=...)`.
2. **Register the pair:**
   - Import `EN_DE_PAIR` in [`packages/core/app/languages/registry.py`](../packages/core/app/languages/registry.py) and include it in `LanguageRegistry.__init__`'s default tuple (or call `register_pair(EN_DE_PAIR)` at startup).
   - Re-export it in [`packages/core/app/languages/__init__.py`](../packages/core/app/languages/__init__.py).

---

## 3. Step-by-Step: Adding a Cross-Script Pair (e.g., `en-zh` or `zh-en`)

Cross-script pairs need **no HMM lexicon maintenance** because `CrossScriptSegmenter` classifies characters directly by Unicode script block, and `diglot._split_units()` automatically splits script boundaries even when there are no spaces between them (e.g., `Read中文now` → `en("Read")`, `zh("中文")`, `en("now")`).

```python
from app.languages import (
    CrossScriptSegmenter,
    EN_PROFILE,
    LanguagePair,
    LanguageProfile,
    register_pair,
)

ZH_PROFILE = LanguageProfile(
    code="zh",
    name="Chinese",
    native_name="中文",
    script="cjk",
    word_spaced=False,
    cased=False,
    proficiency_scale="HSK",
    word_pattern=r"[\u4e00-\u9fff]+",
    stopwords=frozenset({"的", "了", "在", "是", "和", "有", "也", "就", "都", "而"}),
    function_words=frozenset({"的", "了", "在", "是", "和", "有", "也", "就", "都", "而"}),
    sentence_end_pattern=r"[。！？.!?…]+[”’」』)\]]*",
    sentence_punct=".!?…。！？\"'“‘「『-–—(",
    min_word_length=1,
    min_stem_length=1,
    min_source_length=1,
    tts_locales=("zh-CN", "zh-TW", "zh"),
    tts_rate=0.95,
    # Optional: plug in jieba or a custom word segmenter:
    # tokenizer=lambda text: list(jieba.cut(text)),
)

EN_ZH_PAIR = LanguagePair(
    id="en-zh",
    base=EN_PROFILE,
    target=ZH_PROFILE,
    segmenter=CrossScriptSegmenter(
        base_code="en",
        target_code="zh",
        base_script="latin",
        target_script="cjk",
    ),
    shelves=(
        ("gentle", 0.00, 0.45, "Mostly English, Chinese in short phrases"),
        ("steady", 0.45, 0.65, "Balanced English and Chinese"),
        ("dense", 0.65, 0.80, "Chinese carries whole sentences"),
        ("immersion", 0.80, 1.01, "Chinese-dominant"),
    ),
    accept_language="en-US,en;q=0.9,zh-CN;q=0.8",
    wikipedia_host="en.wikipedia.org",
    write_placeholder="在这里写作。不要查词典，只用你已经掌握的词。",
)

register_pair(EN_ZH_PAIR)
```

> **Note on non-spaced scripts (`zh`, `ja`, `th`):**
> - By default, `word_pattern=r"[\u4e00-\u9fff]+"` treats contiguous Hanzi runs as tokens. For true word-level click-to-lookup and vocabulary coverage in Chinese or Japanese, pass a `tokenizer` (e.g. `jieba` for Chinese, `fugashi` for Japanese) on `LanguageProfile` and adjust chunk sizing in `ingest.py` if you also enable AI weaving for CJK.
> - Bundled fonts (`Newsreader`, `Hanken Grotesk`) ship `latin` and `latin-ext` glyph ranges only; non-Latin scripts fall back cleanly to OS system fonts.

---

## 4. Lesson Front Matter & Storage Conventions

### Markdown Front Matter (`diglot/1`)

Lessons default to `en-es` when no language pair is declared. For any other pair, add a single line to the front-matter block:

```markdown
### Article Identification & Preview

- **Article Title:** French Art and Reading
- **Author:** Claire
- **Language Pair:** en-fr
```

- `diglot.parse_article()` reads `- **Language Pair:** ...`, selects the matching registered `LanguagePair`, and tags `Span.lang` with the pair's `base.code` / `target.code`.
- `transfer.share_text()` automatically preserves `- **Language Pair:** ...` on export so shared files round-trip losslessly.
- `Article.stats()` emits language-neutral keys (`target_words`, `base_words`, `target_ratio`, `language_pair`, `base_lang`, `target_lang`) alongside legacy `es_words` / `en_words` / `spanish_ratio` aliases for backwards compatibility.

### SQLite Schema (`user_version = 2`)

Every domain table (`words`, `article_progress`, `events`, `lookups`, `jobs`, `exposure`, `scaffolding`, `quotes`, `challenges`, `writings`) includes a `language_pair TEXT NOT NULL DEFAULT 'en-es'` column managed by the versioned migration runner in [`packages/core/app/storage/schema.py`](../packages/core/app/storage/schema.py).

If you later enable simultaneous multi-pair decks inside the same database (where cognates like `animal`, `hotel`, or `fin` exist in two target languages at once), add schema migration `v3` to widen `words.UNIQUE(lemma)` to `UNIQUE(language_pair, lemma)`, `article_progress.PK` to `(language_pair, slug)`, and `exposure.PK` to `(language_pair, day, slug, lemma)`.

### Activating a Pair via Configuration

Set `DIGLOT_PAIR` in `.env` or your process environment (default: `en-es`):

```env
DIGLOT_PAIR=en-fr
```

---

## 5. Remaining Touchpoints for Full AI Weaving & UI Localization

The core parser, glossary, vocabulary coverage, reading exposure, quotes, storage schema, transfer format, and frontend language helper (`js/lib/language.js`) already consume `LanguagePair`. To make a new pair 100% first-class across AI generation and UI copy:

1. **Proficiency Levels & Weave Examples** (`packages/core/app/levels.py`): Add target-language grammar guidelines (e.g., HSK 1–6 or French CEFR cues) and pair-specific worked weave examples.
2. **Register Heuristics** (`packages/core/app/registers.py`): Add target-language discourse/connective regexes and news/academic domains.
3. **LLM Prompts & Rubrics** (`packages/core/app/ingest.py`, `tutor.py`, `judge.py`, `grading.py`, `writing.py`): Parameterize prompt instructions and rubrics from `pair.base.name` / `pair.target.name` (and bump `tutor.PROMPT_VERSION` when changing cached prompts).
4. **Verification**: Add a test in [`packages/core/tests/test_languages_and_fixes.py`](../packages/core/tests/test_languages_and_fixes.py) verifying segmentation, glossary lookup, vocabulary coverage, quote extraction, and transfer round-trip for your new pair.
