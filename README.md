# Diglota

<p align="center">
  <strong>A reading-first, dual-language learning platform for mastering Spanish in context.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10+-3776AB?logo=python&logoColor=white" alt="Python 3.10+" />
  <img src="https://img.shields.io/badge/backend-FastAPI-009688?logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/storage-SQLite-003B57?logo=sqlite&logoColor=white" alt="SQLite" />
  <img src="https://img.shields.io/badge/frontend-Zero--Build%20Vanilla%20JS-F7DF1E?logo=javascript&logoColor=black" alt="Vanilla JS" />
  <img src="https://img.shields.io/badge/AI-Optional%20%7C%20Local--First-black" alt="Optional AI" />
  <img src="https://img.shields.io/badge/tests-540%20passing-success" alt="Tests" />
  <img src="https://img.shields.io/badge/license-AGPL--3.0-blue" alt="License: AGPL-3.0" />
</p>

---

Diglota is designed around the **diglot method**: weaving authentic Spanish into English prose. English carries the narrative arc while Spanish phrases introduce target vocabulary, complete with English glosses and bolded focus forms.

Instead of rote memorization or artificial flashcards, Diglota transforms authentic reading into an active acquisition engine:
- **Instant Morphological Lookups:** Click any Spanish word to see its base lemma, conjugated form, usage notes, and common pitfalls.
- **Contextual Spaced Repetition (SM-2):** Saved words return inside the exact sentences you first encountered them in.
- **Semantic Translation Grading:** AI-driven evaluation that grades exercises on *meaning and nuance* rather than brittle string matching.
- **Autonomous Lesson Generation:** Turn any web article or personal writing into a calibrated, leveled diglot reading with automated vocabulary recycling and grammar breakdowns.

![The Diglota Library](assets/library.png)

---

## ⚡ Quick Start

### 1. Requirements

- **Python 3.10+** (Nothing compiled, no external runtimes required)
- **Minimal Dependencies**:
  ```bash
  pip install -r requirements.txt
  ```
  *Runtime stack:* `fastapi`, `uvicorn[standard]`, `python-dotenv`, and optionally `typesafe-sdk`. No external database server to run (embedded SQLite) and zero frontend build pipelines (`static/` is pure native web code).

### 2. Launch

#### Windows (Zero-Console Desktop Mode)
**Double-click `Open Diglot.vbs`.**  
Launches the background server via `pythonw` with zero flickering terminal windows, waits for the server to become healthy, and opens the app in your default browser. Application logs are written to `data/app.log`.

#### Cross-Platform / Command Line
```bash
python run.py                     # Runs at http://127.0.0.1:8787
python run.py --port 9000 --reload # Dev mode with auto-reload
pythonw run.py --log data/app.log # Detached background mode
```

> **Clean Shutdown:** Click **Quit** in the app's *Settings* modal. Diglota ensures all database transactions are committed, background tasks safely settled, and vocabulary warmers flushed.

---

## ⚙️ Configuration & Architecture

Diglota is strictly **local-first and privacy-respecting**. Core reading, coverage calculation, SQLite persistence, and spaced repetition work **100% offline with zero API keys**.

Configure optional AI capabilities directly in the web UI under **Settings** (persisted in `data/settings.json`) or via environment variables in `.env`:

```env
# Tier 1: Semantic Evaluation (TypeSafe System One / Jev)
TYPESAFE_API_KEY=your_typesafe_key
TYPESAFE_MODEL=jev-latest

# Tier 2: Interactive Tutor, Quizzes, Glossing & Lesson Weaving
LLM_BASE_URL=https://api.openai.com/v1     # Any OpenAI-compatible endpoint
LLM_API_KEY=your_llm_api_key
LLM_MODEL=deepseek-chat                   # or gpt-4o, claude-3-5-sonnet, etc.

# Storage & Network Overrides
DIGLOT_CORPUS=./corpus                    # Directory for curated Markdown lessons
DIGLOT_PROXY=http://127.0.0.1:7890        # Optional HTTP proxy for remote requests
```

### Feature Availability Matrix

| Feature | Without API Keys (Offline) | With LLM Configured | With TypeSafe / Jev |
| :--- | :---: | :---: | :---: |
| **Diglot Reader & Text Layout** | ✅ Full | ✅ Full | ✅ Full |
| **Corpus Morphological Glosses** | ✅ Full (Instant pre-indexed) | ✅ Full | ✅ Full |
| **Deep Explanations & Lemmatization** | ⚠️ Fallback to corpus gloss | ✅ Interactive AI tutor | ✅ Interactive AI tutor |
| **Bilingual TTS Read-Aloud** | ✅ Web Speech API | ✅ Web Speech API | ✅ Web Speech API |
| **SM-2 Spaced Repetition (Deck & Quotes)** | ✅ Full | ✅ Full | ✅ Full |
| **Writing Workspace Metrics** | ✅ Spanish % & word counts | ✅ Real-time feedback & revisions | ✅ Real-time feedback & revisions |
| **Semantic Exercise Grading** | ⚠️ String & cloze verification | ⚠️ Conversational LLM judge | ✅ Calibrated semantic confidence |
| **Web Import & Lesson Weaver** | ❌ Requires LLM | ✅ Autonomous lesson synthesis | ✅ Autonomous lesson synthesis |

---

## 🎯 Core Features

### 1. The Immersive Reader

![The Reader Interface](assets/reader.png)

- **Dual-Language Typography:** Spanish in deep teal, English in warm charcoal, lesson focus words in bold, and contextual glosses in muted italic.
- **Dynamic Scaffolding Dials:**
  - *Full Assistance:* English text with inline Spanish phrases and glosses.
  - *Intermediate:* Glosses hidden, English text slightly faded.
  - *Spanish Immersion:* English prose hidden completely, revealing purely the Spanish passages.
- **Zero-Latency Word Lookup:** The corpus's built-in vocabulary tables and author glosses are pre-indexed into a local client glossary. Words not in the glossary are warmed asynchronously in the background upon opening an article.
- **"💡 Explain" & "✨ Full Entry":** Instant morphological breakdowns that identify roots, tenses (e.g., `se volverá` $\rightarrow$ `volverse`, future), usage notes, and common pitfalls without pedantic fluff.
- **Sentence-Synchronized Bilingual TTS:** Speaks English in native English voices and Spanish in native Spanish voices, correctly segmenting at grammatical clause boundaries rather than fragmented markup tags.
- **Intelligent Table of Contents:** Automatically maps sections while filtering out web furniture, repeated kickers, and noise.
- **Register Metadata:** Classifies writing register (*news, essay, conversation, academic, fiction*) and stores manual user corrections directly into the article file.

---

### 2. Spaced Repetition & Contextual Memory

![Card Review](assets/review.png)

- **In-Situ Flashcards:** Cards display the target word **inside the exact sentence you met it in**, preserving situational context.
- **Lemma Intelligence:** Reviewing a conjugated form like `enfatizan` reveals both the contextual meaning (*they emphasise*) and the infinitive lemma (`enfatizar`).
- **Dual Recall Modes:**
  - **Recognise:** Comprehend Spanish within context.
  - **Produce:** Blanked-out cloze recall where you type the target word.
- **Adaptive Study Lists:** Words looked up three or more times without being explicitly saved are automatically flagged as high-priority struggle words.
- **Comprehensive Deck Browser:** Search and filter your entire deck by stage, retention rate, or forgotten frequency. All queries feature accent-folded search (`cuestion` matches `cuestión`).

![Saved Words View](assets/saved-words.png)

---

### 3. Kept Quotes & Sentence Collection

- **One-Click Sentence Capture:** Select any passage and click **Keep** to store the complete sentence in your **Quotes** archive with zero model latency.
- **Automatic Boundary Widening:** A fragment selection automatically widens to its grammatical sentence boundary.
- **Bidirectional Linking:** Look up any word in the reader, and your saved quotes featuring that word will immediately surface in the popover.

---

### 4. Interactive Writing Studio

- **Context-Aware Prompts:** Prompts are generated from your personal struggle words, recently saved vocabulary, and currently active reading themes.
- **Real-Time Language Ratio Meter:** Live analysis displays word count, sentence count, and the exact proportion of your draft written in authentic Spanish.
- **Structured Annotation Engine:**
  - 🟢 **Good:** Effective phrasing and correct idioms.
  - 🔴 **Fix:** Grammatical errors and morphological mismatches.
  - 🟡 **Style:** Opportunities for more natural native expression.
- **"Improve This" Assistants:**
  - *Fix grammar only*
  - *Sound native*
  - *Enrich vocabulary*
  - *Keep my exact words*
- **Draft-to-Lesson Compiler:** Convert your own written drafts directly into full-fledged diglot reading lessons with glosses and vocabulary anchors.

---

### 5. Reading Analytics & The Passage Map

![Progress and Analytics](assets/progress.png)

- **Evidence-Based Comprehension:** Analyzes vocabulary exposure across *New*, *Met Before*, and *Consolidated* words (with Spanish function words excluded from statistics).
- **Scaffolding Dependence Rate:** Tracks lookup and translation frequency per 1,000 words read over time.
- **Weekly Behavioral Challenges:** Suggests targeted learning habits based on detected gaps (e.g., branching into unread registers or finishing in-progress articles).
- **TF-IDF Passage Map:** An interactive network graph clustering your library by shared thematic vocabulary and read readiness.

---

### 6. Semantic Translation Evaluation

Traditional flashcard systems fail at open translation because string matching cannot distinguish valid synonyms from severe misunderstandings. Diglota deploys a multi-tiered grading pipeline:

```
[Student Response]
        │
        ▼
┌───────────────────────────────┐
│ Tier 1: TypeSafe Jev Engine   │ ──► Multi-dimensional confidence scoring
└───────────────────────────────┘     (Meaning 98% · Grammar 97% · Naturalness 95%)
        │ (if unavailable)
        ▼
┌───────────────────────────────┐
│ Tier 2: General Chat LLM      │ ──► Semantic feedback with qualitative rationale
└───────────────────────────────┘
        │ (if offline)
        ▼
┌───────────────────────────────┐
│ Tier 3: Heuristic Baseline    │ ──► Reference match & non-punitive verification
└───────────────────────────────┘
```

> **Design Principle:** Diglota *never* penalizes alternative valid translations when operating offline—unrecognized variations are marked as **Unverified** rather than incorrect.

---

### 7. Lesson Ingestion & The Discover Engine

![Discover Engine](assets/discover.png)

- **Multi-Source Ingestion:** Import any web URL or paste raw text. The ingestion engine strips boilerplate, determines target CEFR levels (A1–C1), and weaves Spanish into English prose.
- **Precise Dial Controls:**
  - **Level:** A1 (high glossing, present tense, high-frequency lexicon) to C1 (idiomatic, literary nuances).
  - **Spanish Density:** From Light (22%) to Immersion (68%).
  - **Weave Geometry:** *Mixed (natural clause boundaries)* or *Strict (whole sentences only)*.
- **Targeted Discover Search:** Finds articles across Wikipedia and curated publications matching specific topics or missing vocabulary clusters.
- **Asynchronous Background Processing:** Imports run in a managed, cancellable job queue (bounded concurrency) with Server-Sent Events (SSE) tracking progress.

---

## 📄 The Diglot Corpus Format

Lessons in Diglota are stored as clean, human-readable Markdown files. Hand-crafted lessons and AI-generated imports share the identical format:

```markdown
### Article Identification & Preview
- **Article Title:** How AI Will Make Art Worse
- **Author:** Tim Brinkhof
- **Direct URL:** https://example.com/article
- **Preview:** An examination of generative models and human creativity...

# How AI Will Make Art Worse
**By Tim Brinkhof**

Many live in quiet fear that AI will someday be the death of art. Afortunadamente,
los anales (*annals*) de la historia del arte **pintan** un panorama diferente.
Viewed from a distance, the pressure AI exerts appears as part of an evolutionary
process...

---
### POST-READING ANCHORS
**Recycled Vocabulary Box**
- **volverse** / **se vuelva, se volverá** (*to become*)

**Grammar Breakdown**
1. **Future Tense for Predictions:** Spanish future endings express prediction...
```

### Portable Lesson Sharing (`diglot/1`)

Export any lesson as a standalone Markdown file. Shared files append advisory verification metadata without exporting personal study history:

```markdown
- **Diglot Format:** diglot/1
- **Shared By:** maria
- **Shared On:** 2026-10-09
- **Lesson Origin:** woven by deepseek-v4.1-flash
- **Body Digest:** 4baf5139c17e
```

Importing a shared lesson presents a pre-flight summary detailing author, origin, and vocabulary overlap before committing it to your local library.

---

## 📁 Repository Structure

```
.
├── app/
│   ├── diglot.py          # Core parser, token segmenter, lexicon (zero external dependencies)
│   ├── vocab.py           # Morphology, stemming, clitics, vocabulary coverage metrics
│   ├── reading.py         # Reading tracker (scroll telemetry -> actual consumed words)
│   ├── analytics.py       # Exposure, scaffolding ratios, and progress aggregations
│   ├── corpus.py          # Passage network graph, TF-IDF weights, vocabulary clustering
│   ├── levels.py          # CEFR calibration, density ratios, and weave presets
│   ├── glossary.py        # Local morphology dictionary extracted from corpus anchors
│   ├── warm.py            # Speculative background vocabulary resolution
│   ├── library.py         # Unified catalog interface (curated corpus + imported lessons)
│   ├── config.py          # Settings, API keys, paths, and .env vs panel precedence
│   ├── store.py           # SQLite persistence layer (cards, SRS reviews, progress, jobs)
│   ├── srs.py             # Pure SM-2 spaced repetition algorithms
│   ├── llm.py             # Resilient OpenAI-compatible HTTP client
│   ├── tutor.py           # Conversational explanations, dictionary expansions, prompts
│   ├── judge.py           # TypeSafe Jev semantic evaluation client
│   ├── grading.py         # Three-tier grading pipeline with graceful fallbacks
│   ├── registers.py       # Discourse register classification & tag management
│   ├── transfer.py        # Lesson serialization, digest validation, and sharing
│   ├── quotes.py          # Kept sentences extraction and lookup indexing
│   ├── challenges.py      # Dynamic weekly learning goals
│   ├── writing.py         # Writing studio metrics and correction parsers
│   ├── ingest.py          # Web / text ingestion into woven diglot Markdown
│   ├── fetch.py           # Article extractor and search scrapers
│   ├── recommend.py       # Gap-filling and topic discovery recommendations
│   ├── jobs.py            # Bounded background task manager with SSE telemetry
│   └── server.py          # FastAPI application entrypoint and route declarations
├── corpus/                # Curated Markdown lessons (one sample ships with the app)
├── data/                  # User database (SQLite), settings, and imported library
├── static/                # Single-page frontend (Vanilla JS, CSS, zero build step)
├── tools/                 # Diagnostics: segment_qa, segment_preview, ui_test, ui_import_test
├── tests/                 # 540 automated pytest tests
├── run.py                 # CLI application server launcher
└── Open Diglot.vbs        # Windows desktop double-click launcher
```

---

## 🧪 Testing & Verification

Diglota maintains high code quality and test coverage across unit logic, linguistic segmentation, and end-to-end browser workflows:

```bash
# 1. Install development & testing tooling
pip install -r requirements-dev.txt

# 2. Run the unit test suite (540 tests)
python -m pytest tests/ -q

# 3. Run browser end-to-end checks (Playwright driving Chrome)
python tools/ui_test.py

# 4. Run end-to-end browser test with AI grading validation
python tools/ui_test.py --with-ai

# 5. Verify linguistic segmenter integrity across the corpus
python tools/segment_qa.py
```

> **Linguistic Invariant:** `tools/segment_qa.py` audits the corpus segmenter against implausible spans (e.g. Spanish spans saturated with English function words). It verifies zero classification anomalies across the entire corpus.

---

## 💡 Engineering Highlights

- **The Reading Surface is Sacred:** Reading performance has zero blocking network overhead. Telemetry, analytics, and lookups use optimistic caching, pre-compiled glossaries, and passive request piggybacking.
- **Format Symmetry:** Imported lessons, user writings, and hand-crafted articles all serialize to the identical Markdown corpus format.
- **Function Word Filtering:** All coverage and exposure calculations filter out Spanish function words (articles, prepositions, conjunctions) to ensure metrics reflect real vocabulary acquisition.
- **Fail-Soft Degradation:** The entire system gracefully falls back across every layer: Jev $\rightarrow$ LLM $\rightarrow$ Heuristic for grading; LLM $\rightarrow$ Corpus glossary for definitions; SSE $\rightarrow$ Polling for background tasks.

---

## 📜 License

**GNU Affero General Public License v3.0** — see [`LICENSE`](LICENSE).

Use it, change it, share it. Anyone distributing it, or running a modified
version as a network service, must publish their source under the same terms
(AGPL §13).
