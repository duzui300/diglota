# Diglota

<p align="center">

  <a href="https://github.com/duzui300/diglota"><img src="https://img.shields.io/badge/python-3.10+-3776AB?logo=python&logoColor=white" alt="Python 3.10+" /></a>
  <a href="https://github.com/duzui300/diglota"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue" alt="License: AGPL-3.0" /></a>
  <a href="https://github.com/duzui300/diglota"><img src="https://img.shields.io/badge/platform-Web%20%7C%20Windows-lightgrey" alt="Platform: Web and Windows" /></a>
  <a href="https://github.com/duzui300/diglota"><img src="https://img.shields.io/badge/storage-SQLite-003B57?logo=sqlite&logoColor=white" alt="SQLite" /></a>
  <a href="https://github.com/duzui300/diglota"><img src="https://img.shields.io/badge/AI-Optional%20%7C%20Local--First-black" alt="Optional AI" /></a>
  <a href="https://hub.xerj.org/contributors/duzui300"><img src="https://hub.xerj.org/contributors/duzui300/badge.svg" alt="XERJ Corpus Hub contributor" /></a>

</p>

Diglota is a reading-first app for learning Spanish through English by the diglot weave, in which Spanish phrases carrying short English glosses are set into English prose. It is built for independent learners at any level: the reading interface, morphological dictionary lookups, vocabulary coverage metrics and spaced repetition all run offline, with no account and no API key.

As you read, click any Spanish word for an instant lemma, tense and usage entry; save words to meet them again in spaced repetition inside the exact sentences where you found them; and, if you want more, connect AI models — TypeSafe Jev for semantic grading, or any OpenAI-compatible chat model for tutoring and lesson synthesis.

![](orca-paste-1792960512875-8dacf183-d558-47ae-8671-547ea3196d37.png)

![](orca-paste-1792960487287-e4576908-8ca3-4c7e-8428-bd81a9d9e162.png)

---

## What's New in 1.0.1

- **Editorial redesign.** The library opens on the next lesson and a single shelf rail; the reader is a page with a working margin. Every room shares the same paper, hairlines and type instead of a grid of cards.
- **A reading background you choose.** Settings now has a custom page colour. Ink, rules and accents follow the colour's tone; the named Paper, Sepia and Night themes remain.
- **Your mark.** The masthead, splash, browser icon and Windows app icon now carry the open-book mark and wordmark.
- **Quieter ends to Review, Quotes and Write,** with a stage ladder, a quotation note and a weave legend instead of empty space.

Existing lessons, saved words and review history are untouched by the update.

## What's New in 1.0.0

- **Native Windows window.** Diglota now runs as a dedicated desktop app through pywebview and Edge WebView2. The FastAPI engine runs in-process on an OS-assigned loopback port, so it cannot collide with a web copy, and the window remembers its geometry and the reader's state.
- **One-file installer.** The setup program installs directly beside itself. It needs no administrator rights, creates no hidden copy in `%LOCALAPPDATA%`, and keeps your lessons and reading history when you update — and, by default, when you uninstall.
- **Monorepo architecture.** One engine now serves the web app, the command line and the desktop window: `packages/core`, `packages/ui`, `packages/web` and `packages/win`.
- **580 automated tests.** Forty desktop and setup tests join the 540 engine tests; `python -m pytest -q` passes all 580.

---

## Three Ways to Run

Diglota offers two desktop approaches on Windows, plus a cross-platform web server for every operating system.

### 1. One-File Windows Installer (Recommended on Windows)

The simplest way to use Diglota on Windows 10 or 11. The desktop app needs the WebView2 runtime; Windows 11 includes it by default.

1. Download or build `Diglota-Setup-1.0.1.exe`.
2. Move the setup file to the folder where Diglota should live — for example, `D:\Apps\Diglota`.
3. Run the installer. It installs **beside itself**, in that folder — no administrator permissions, and no scattered files in `%LOCALAPPDATA%`.
4. The setup window shows the install path before copying anything, offers **Change…** to pick another folder, and counts the files as it goes.
5. Setup creates a Start menu shortcut, an optional desktop shortcut, and an entry in Windows *Settings → Apps*.

![](orca-paste-1792960473191-3af42e14-25e8-42ac-9075-13dadf164006.png)

> **Updating and uninstalling.** To update, run the setup program again in the install folder; your database (`data/`) and lessons (`corpus/`) are left untouched. The uninstaller removes program files while keeping your library, settings and cards by default, unless you explicitly tick the box to erase them.

> **Windows SmartScreen.** The setup program is unsigned, so Windows may show a one-time warning on first launch. Choose **More info → Run anyway**.

### 2. Portable Desktop Folder (Windows)

You can run the desktop app from source, or build the portable folder without using an installer:

```bash
# Install desktop requirements
pip install -r packages/win/requirements.txt

# Run the native desktop window from source
cd packages/win
python -m diglota_win

# Or build the portable folder (from the repository root)
python packages/win/build.py
```

The build lands in `packages/win/dist/Diglota/Diglota.exe`. The folder is fully portable: copy it anywhere, and `data/` is created directly beside the executable. The app preserves window bounds and the reader's theme across sessions.

### 3. Web / CLI (Cross-Platform)

The web app runs anywhere Python 3.10+ is available:

```bash
# Install core dependencies
pip install -r requirements.txt

# Start the server (http://127.0.0.1:8787)
python run.py

# Optional flags
python run.py --port 9000 --reload
pythonw run.py --log data/app.log
```

> **Windows browser launcher (`Open Diglot.vbs`).** If you would rather use your regular browser without keeping a command prompt open, double-click `Open Diglot.vbs`. It starts the server with `pythonw` (logging to `data/app.log`), waits for it to answer, and opens the app in your default browser. If the app is already running, it simply opens the page. To stop the server, click **Quit the app** in the app's Settings panel.

---

## Configuration &amp; Local-First Storage

Diglota is local-first by design:

- Your reading progress, reviews and settings live in SQLite under `data/`.
- Hand-crafted lessons live in `corpus/`.
- Neither directory is tracked in git, and neither is touched when the program is updated.

Reading, dictionary lookups, vocabulary coverage, quotes and spaced repetition work fully offline. Optional AI features can be configured in the app's Settings panel (persisted in `data/settings.json`) or in a `.env` file:

```env
# Tier 1: semantic evaluation (TypeSafe System One / Jev)
TYPESAFE_API_KEY=your_key_here
TYPESAFE_MODEL=jev-latest

# Tier 2: interactive tutor, quizzes, glossing and lesson weaving
LLM_BASE_URL=https://api.openai.com/v1     # Any OpenAI-compatible endpoint
LLM_API_KEY=your_key_here
LLM_MODEL=deepseek-chat                   # Or gpt-4o, claude-3-5-sonnet, etc.

# Path and network overrides
DIGLOT_CORPUS=./corpus                    # Curated lesson directory (default: ./corpus)
DIGLOT_PROXY=http://127.0.0.1:7890        # HTTP/HTTPS proxy, if one is required
```

> **Keys.** Keys entered in Settings override `.env` values. They are stored locally and are never serialized or sent back to the browser (`repr=False`).

### Feature Availability Matrix


| Feature                                        | Without API Keys (Offline)         | With LLM Configured                  | With TypeSafe / Jev                  |
| :---------------------------------------------- | :----------------------------------: | :------------------------------------: | :------------------------------------: |
| **Diglot Reader &amp; Text Layout**            | ✅ Full                             | ✅ Full                               | ✅ Full                               |
| **Corpus Morphological Glosses**               | ✅ Full (Instant pre-indexed)       | ✅ Full                               | ✅ Full                               |
| **Deep Explanations &amp; Lemmatization**      | ⚠️ Fallback to corpus gloss        | ✅ Interactive AI tutor               | ✅ Interactive AI tutor               |
| **Bilingual TTS Read-Aloud**                   | ✅ Web Speech API                   | ✅ Web Speech API                     | ✅ Web Speech API                     |
| **SM-2 Spaced Repetition (Deck &amp; Quotes)** | ✅ Full                             | ✅ Full                               | ✅ Full                               |
| **Writing Workspace Metrics**                  | ✅ Spanish % &amp; word counts      | ✅ Real-time feedback &amp; revisions | ✅ Real-time feedback &amp; revisions |
| **Semantic Exercise Grading**                  | ⚠️ String &amp; cloze verification | ⚠️ Conversational LLM judge          | ✅ Calibrated semantic confidence     |
| **Web Import &amp; Lesson Weaver**             | ❌ Requires LLM                     | ✅ Autonomous lesson synthesis        | ✅ Autonomous lesson synthesis        |


---

## Core Features

### 1. The Immersive Reader

![The reader interface](assets/reader.png)

- **Dual-language typography.** English appears in warm near-black, Spanish in deep teal, lesson target words in bold, and glosses in muted italics.
- **Adjustable scaffolding.** Keep glosses visible, fade the English prose, or hide English entirely to read only the Spanish segments.
- **Zero-latency morphological lookups.** Author glosses and vocabulary boxes are compiled into a local glossary the browser loads directly. A word already present in the corpus resolves instantly, without a network request. Words that are not in the glossary are warmed asynchronously when an article opens.
- **Full entry and explanations.** Ask for a full dictionary entry — lemma, part of speech, contextual examples and common learner traps — or a concise grammar breakdown.
- **Sentence-synchronized audio.** Read-aloud switches voices at sentence boundaries: Spanish text in a Spanish voice, English prose in an English voice.
- **Navigation without clutter.** The contents pane indexes genuine document sections while ignoring duplicate links, kicker lines and page furniture.
- **Register tracking.** Identifies whether a text is news, essay, conversation, academic or fiction, and lets you update register tags in the article file.

### 2. Vocabulary Coverage &amp; Readiness

![](orca-paste-1792960457879-8ec583f2-3e25-481e-8cc9-0d959b4a7612.png)

Before opening any lesson, the library shows what percentage of its Spanish vocabulary you already know from your personal deck. Coverage calculations stem conjugated verbs, account for diphthong stem changes, strip reflexive clitics and exclude Spanish function words, so the figure reflects what you would actually have to learn.

### 3. In-Context Spaced Repetition (SM-2)

![Card review](assets/review.png)

- **Cards in context.** Words return for review inside the exact sentence where you met them, not as isolated vocabulary pairs.
- **Lemma awareness.** Conjugated forms link back to their dictionary lemma; for example, *enfatizan* reveals both *they emphasise* and the infinitive *enfatizar*.
- **Two review modes.** Practise comprehension in **Recognise** mode, or test active recall in **Produce** mode by typing the missing word into a cloze sentence.
- **High-frequency lookup lists.** Words looked up three or more times without being saved are surfaced as recommended study items.
- **Deck management.** Search and filter your cards by review stage, retention rate or forgotten frequency, with accent-folded matching (`cuestion` matches `cuestión`).

![Saved words view](assets/saved-words.png)

### 4. Sentence Capture (Quotes)

Select any passage while reading and click **Keep** to save the full sentence to your Quotes collection, with no model call. Selections expand automatically to complete sentence boundaries. When you look up a word in later readings, any saved quotes containing that word appear in the lookup popover.

### 5. Writing Workspace

Practise producing Spanish in an active drafting environment:

- **Targeted prompts.** Prompts draw on words you frequently look up, recently saved cards, and themes from articles you have read.
- **Live language ratio.** Word count, sentence count and the exact percentage of your text written in Spanish, updated as you type.
- **Inline feedback.** With an LLM configured, receive specific annotations — **Good**, **Fix** or **Style** — placed directly on your sentences.
- **Revision options.** Ask for a revision that fixes grammar, sounds more native, enriches the phrasing, or keeps your original wording.
- **Make a lesson.** Convert a draft into a full diglot lesson, with vocabulary tables and grammar notes, ready for your library.

### 6. Reading Analytics &amp; Passage Map

![Progress and analytics](assets/progress.png)

- **Exposure metrics.** Tracks Spanish words read over time, sorting vocabulary into new, previously met and repeatedly seen words, while excluding function words.
- **Scaffolding dependence.** Measures lookups, full entries and translations per 1,000 words read, so you can see your reliance on reading aids fall.
- **Weekly goals.** Reads your history and proposes balanced targets, such as reading an unread register or finishing an article already in progress.
- **Passage map.** Draws your library as a connected graph clustered by shared vocabulary using IDF weighting, showing which articles prepare you for the others.

### 7. Semantic Translation Grading

Translation drills test understanding rather than exact string matches. Diglota uses a three-tier grading system:

1. **TypeSafe Jev.** Evaluates meaning, grammar and naturalness with calibrated confidence scores — distinguishing, for example, a grammatically correct sentence with inverted meaning from an accurate translation that happens to use different vocabulary.
2. **Chat LLM fallback.** Gives a qualitative evaluation and explanation when Jev is not configured.
3. **Offline heuristic matching.** Checks the answer against the reference. An offline answer that differs from the reference is labelled **Unverified** rather than incorrect.

### 8. Lesson Ingestion &amp; Discovery

![Discover engine](assets/discover.png)

- **Web and text import.** Paste an article URL or raw text. The pipeline extracts the body, determines CEFR difficulty, weaves Spanish into the English prose, formats vocabulary tables and validates the result.
- **Three independent settings.**
  - **Level:** A1 to C1 (or Auto), controlling vocabulary frequency, tenses and glossing depth.
  - **Spanish amount:** Light (22%) to Immersion (68%).
  - **Weave mode:** Mixed (weaving within sentences at natural clause boundaries) or Strict (whole sentences alternate).
- **Discover search.** Search Wikipedia and curated long-form articles by topic, or look for pieces that would fill gaps in your review deck.
- **Background job queue.** Imports and searches run in a persistent, cancellable queue with bounded concurrency, streaming live progress over Server-Sent Events.

---

## The Diglot Corpus Format

Lessons are plain Markdown files. Hand-crafted articles in `corpus/` and imports in `data/library/` share exactly the same format:

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

Click **Share this lesson** on any article to export a `.md` file with standard sharing metadata:

```markdown
- **Diglot Format:** diglot/1
- **Shared By:** alice
- **Shared On:** 2026-10-09
- **Lesson Origin:** woven by deepseek-chat
- **Body Digest:** 4baf5139c17e
```

When you import a shared file, the library verifies the body digest and shows the author and vocabulary previews before adding the file to your shelf. Shared files never export personal review schedules, cards or reading history.

---

## Repository Structure

```
.
├── packages/
│   ├── core/                      # Core processing and language engine
│   │   ├── app/
│   │   │   ├── diglot.py          # Parser, segmenter and lexicon (zero dependencies)
│   │   │   ├── vocab.py           # Morphology, stemming, clitics, coverage tracking
│   │   │   ├── reading.py         # Scroll telemetry to word-reading conversion
│   │   │   ├── analytics.py       # Exposure, scaffolding ratios, reading metrics
│   │   │   ├── corpus.py          # Passage graph, TF-IDF weights, topic clusters
│   │   │   ├── levels.py          # CEFR calibration, density and weave presets
│   │   │   ├── glossary.py        # Local translations extracted from corpus anchors
│   │   │   ├── warm.py            # Speculative background vocabulary resolution
│   │   │   ├── library.py         # Unified catalog (curated corpus + imported lessons)
│   │   │   ├── config.py          # Settings management (merging file and environment)
│   │   │   ├── store.py           # SQLite persistence (cards, reviews, progress, jobs)
│   │   │   ├── srs.py             # SM-2 spaced repetition algorithms
│   │   │   ├── llm.py             # OpenAI-compatible HTTP client
│   │   │   ├── tutor.py           # Explanations, dictionary lookups, writing prompts
│   │   │   ├── judge.py           # TypeSafe Jev semantic evaluation client
│   │   │   ├── grading.py         # Three-tier grading pipeline with fallbacks
│   │   │   ├── registers.py       # Register classification (news, essay, fiction, etc.)
│   │   │   ├── transfer.py        # Sharing serialization, digest checks and import
│   │   │   ├── quotes.py          # Kept sentences extraction and lookup indexing
│   │   │   ├── challenges.py      # Dynamic weekly goal generation
│   │   │   ├── writing.py         # Writing workspace measurements and feedback parsing
│   │   │   ├── ingest.py          # Web and paste ingestion into woven Markdown
│   │   │   ├── fetch.py           # Article content extraction and search scrapers
│   │   │   ├── recommend.py       # Vocabulary-driven recommendations and gap detection
│   │   │   ├── jobs.py            # Bounded background task manager with SSE events
│   │   │   └── server.py          # FastAPI application server and routes
│   │   └── tests/                 # 540 core engine tests
│   ├── ui/
│   │   └── static/                # Single-page frontend (HTML, CSS, JS; no build step)
│   ├── web/
│   │   └── run.py                 # CLI entry point (invoked by the root run.py)
│   └── win/                       # Windows desktop app and installer packages
│       ├── diglota_win/           # Desktop shell (pywebview + in-process uvicorn)
│       ├── installer.py           # Setup window; installs into the folder it runs from
│       ├── uninstaller.py         # Removal window; runs from %TEMP% so it can delete itself
│       ├── setup_common.py        # Every install decision, with no UI (the tested part)
│       ├── setup_ui.py            # The setup windows' small tkinter kit
│       ├── build.py               # PyInstaller build script for the app and setup exes
│       ├── make_icon.py           # Windows icon asset builder
│       ├── *.spec                 # PyInstaller specification files
│       └── tests/                 # 41 desktop and setup tests
├── corpus/                        # Curated Markdown lessons (one sample ships)
├── data/                          # User database, settings and library (not in git)
├── assets/                        # Screenshots, documentation images and the logo sources
├── tools/                         # QA tooling (segment_qa, ui_test, publish, etc.)
├── run.py                         # Root entry point delegating to packages/web/run.py
└── Open Diglot.vbs                # Windows browser launcher (runs the server via pythonw)
```

The brand source files are in `assets/`. The UI serves its own copies from
`packages/ui/static/brand/`, including lifted-ink versions for night paper; the
desktop icon is built from `packages/win/assets/diglota-mark.png`. Newsreader and
Hanken Grotesk are self-hosted under the SIL Open Font License, with their
licences beside the font files.

---

## Testing &amp; Quality Assurance

The suite contains 581 automated tests covering the language engine, desktop packaging and setup safety:

```bash
# Install development and testing tooling
pip install -r requirements-dev.txt

# Run the full suite (581 tests)
python -m pytest -q

# Run engine tests only (540 tests)
python -m pytest packages/core/tests/ -q

# Run desktop and setup tests only (41 tests)
python -m pytest packages/win/tests/ -q

# Run browser checks (drives local Chrome via Playwright)
python tools/ui_test.py
python tools/ui_test.py --with-ai

# Audit the segmenter across the corpus
python tools/segment_qa.py
```

`tools/segment_qa.py` checks for implausible Spanish spans — for example, spans saturated with English function words — and currently reports zero suspect spans across the bundled lessons.

---

## Licence

**GNU Affero General Public License v3.0** — see [`LICENSE`](LICENSE).

Use it, change it, share it. Anyone distributing it, or running a modified version as a network service, must publish their source under the same terms (AGPL §13).
