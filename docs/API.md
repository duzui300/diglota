# Diglota HTTP & SSE API Reference

All endpoints are served by the local FastAPI application ([`packages/core/app/api/`](../packages/core/app/api/)) bound to `127.0.0.1`. API keys never leave the server.

### Requirement Tags
- **Offline** — works with no API keys configured.
- **LLM** — requires `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL` (returns HTTP `503` if unset).
- **Jev (optional)** — enhanced by `TYPESAFE_API_KEY` (falls back to Tutor or local comparison when absent).

### Security Note
All state-changing routes (`POST`, `PUT`, `PATCH`, `DELETE`) pass through the same-origin CSRF middleware in [`packages/core/app/api/__init__.py`](../packages/core/app/api/__init__.py): browser requests carrying a non-loopback `Origin` or `Referer` receive HTTP `403`.

---

## 1. System, Languages & Settings (`api/routes/system.py`)

| Method | Path | Tier | Description |
|---|---|---|---|
| `GET` | `/api/status` | Offline | Server readiness, article/word/due counts, grading tier description, and active language-pair profile. Never returns key material. |
| `GET` | `/api/languages` | Offline | Lists all registered language pairs (`en-es`, `en-fr`, …) and the currently active pair ID. |
| `GET` | `/api/registers` | Offline | Register taxonomy (`news`, `essay`, `conversation`, `academic`, `fiction`) with labels and blurbs. |
| `GET` | `/api/settings` | Offline | Current default, lookup (`lookup_llm`), and weave (`weave_llm`) model states plus `warm_glosses` and Jev status (`ready`, `key_set`, masked `key_hint`, `from`). |
| `POST` | `/api/settings` | Offline | Saves panel overrides (default, lookup, weave models, and `warm_glosses`) to `data/settings.json` (atomic write, validates `http(s)` on base URLs) and hot-reloads AI clients. Empty string clears an override back to `.env`. |
| `POST` | `/api/reset` | Offline | Takes an online SQLite backup under `data/backups/` and clears all accumulated study history (lessons on disk are untouched). |
| `POST` | `/api/quit` | Offline | Gracefully shuts down the local server when started with a shutdown hook (`run.py` / `Open Diglota.vbs`). Returns `501` if unmanaged. |

---

## 2. Library, Reading, Analytics & Challenges (`api/routes/library.py`)

| Method | Path | Tier | Description |
|---|---|---|---|
| `GET` | `/api/library` | Offline | Full article catalogue with shelf, register, vocabulary coverage, reading progress, total word count, due count, and `worth_saving` (words looked up `2+` times and not yet saved). |
| `GET` | `/api/article/{slug}` | Offline (+ background warming if LLM ready) | Full parsed article with blocks/spans, focus vocabulary, known terms, progress, coverage, and background gloss-warmer count. |
| `DELETE` | `/api/article/{slug}` | Offline | Deletes an app-imported lesson inside `data/library/` and forgets its reading progress (keeps saved words). Refuses (`409`) to delete user-owned files in `corpus/` or multi-passage compilations. |
| `POST` | `/api/article/{slug}/register` | Offline | Atomically updates or clears `- **Register:**` in a single-passage file's front matter and refreshes the library. |
| `POST` | `/api/article/{slug}/progress` | Offline | Saves scroll `position` (`0.0–1.0`), `seconds`, and `completed` flag; credits newly read target-language tokens into `exposure` (monotonic `credited_to` prevents double-counting). |
| `GET` | `/api/stats` | Offline | Deck stage distribution (`new`, `learning`, `young`, `mature`), due count, today/total reviews, completed articles, quote count, streak, and 30-day activity series. |
| `GET` | `/api/analytics/reading?days=7` | Offline | Reading exposure totals, new/familiar/repeated split, daily series, new-words-per-day series, top encountered words, and scaffolding summary (`days` clamped `1–365`). |
| `GET` | `/api/analytics/corpus` | Offline | Force-directed corpus vocabulary graph (`nodes`, `edges`, `clusters`, `isolated`, `stats`) built via IDF-weighted overlap and label propagation. |
| `POST` | `/api/scaffolding` | Offline | Logs a reading-mode change (`gloss-on`, `gloss-off`, `spanish-only`, `target-only`, `focus`), designed for `navigator.sendBeacon`. |
| `GET` | `/api/challenge` | Offline | Active weekly challenge with live progress (auto-marks completed when target is met), proposed next challenge, available kinds, and history. |
| `POST` | `/api/challenge` | Offline | Starts a weekly challenge (`kind`, optional `target`), closing any previously active challenge. |
| `DELETE` | `/api/challenge/{challenge_id}` | Offline | Ends an active challenge (kept in history). |

---

## 3. Vocabulary, Glossary & Spaced Repetition (`api/routes/vocabulary.py`)

| Method | Path | Tier | Description |
|---|---|---|---|
| `GET` | `/api/word/lookup?term=...&sentence=...&slug=...&deep=false&fresh=false` | Offline (glossary/cache) / LLM (miss, `deep=true`, or `fresh=true`) | Resolves a word cheapest-source-first (exact/fold/lemma cache → local glossary → stem cache → Lookup Tutor with singleflight coalescing), logs lookup, and attaches matching kept quotes. `fresh=true` bypasses failure cache; transient `deep=true` outages preserve existing valid cache entries. |
| `GET` | `/api/glossary` | Offline | Compact whole-word local glossary index shipped to the browser for zero-latency clicks. |
| `GET` | `/api/lookups?minimum=2` | Offline | Frequently looked-up unsaved words (stopword-filtered). |
| `POST` | `/api/word/save` | Offline | Idempotently saves a word and creates its SM-2 recall card. |
| `GET` | `/api/words?search=...` | Offline | Lists up to `2,000` saved words with card stage, interval preview, and accent-folded full-text search across term, gloss, lemma, POS, context, and article title. |
| `DELETE` | `/api/word/{word_id}` | Offline | Deletes a saved word and cascades to its cards and reviews. |
| `GET` | `/api/export.csv` | Offline | Downloads Anki-compatible UTF-8 CSV (`diglot-vocabulary.csv`) with spreadsheet formula-injection neutralization. |
| `GET` | `/api/vocabulary-index` | Offline | Maps library article slugs to saved deck words appearing in their focus vocabulary. |
| `GET` | `/api/review?limit=40&new=12` | Offline | Returns due SRS cards plus up to `new` fresh cards, backfilling context sentences from the article when missing. |
| `POST` | `/api/review/{card_id}` | Offline | Submits an SM-2 rating (`again`, `hard`, `good`, `easy`) and optional `elapsed_ms`; returns updated schedule and button previews. |

---

## 4. Kept Sentences / Quotes (`api/routes/quotes.py`)

| Method | Path | Tier | Description |
|---|---|---|---|
| `POST` | `/api/quote` | Offline | Cuts a reader selection to exact sentence boundaries using the article's parsed spans and saves it (deduplicated on text). |
| `GET` | `/api/quotes?q=...&slug=...` | Offline | Lists kept sentences with accent-folded search, optional article filter, re-derived span markup, and per-source counts. |
| `PATCH` | `/api/quote/{quote_id}` | Offline | Updates the reader's personal note on a kept sentence. |
| `DELETE` | `/api/quote/{quote_id}` | Offline | Deletes a kept sentence. |

---

## 5. Tutor Assist & Exercises (`api/routes/exercises.py`)

| Method | Path | Tier | Description |
|---|---|---|---|
| `POST` | `/api/explain` | LLM | On-demand concise grammar/usage explanation for a selected phrase (cached by prompt version). |
| `POST` | `/api/translate` | LLM | Translates selected text into any requested language code or name (`en`, `es`, `fr`, `French`, `Chinese`, …). Cached by prompt version. |
| `GET` | `/api/article/{slug}/quiz` | LLM | Generates (and caches by article content fingerprint) reading comprehension questions. |
| `GET` | `/api/article/{slug}/drills` | LLM | Generates (and caches by article content fingerprint) translation drills. |
| `POST` | `/api/attempt` | Offline / LLM / Jev | Grades a `translate`, `cloze`, or `comprehension` attempt using the 3-tier ladder (`jev → tutor → local`), optionally attaches Tutor feedback, and records the attempt. |
| `GET` | `/api/article/{slug}/attempts` | Offline | Recent recorded exercise attempts for an article (up to 50). |

---

## 6. Import, Discovery, Recommendations & Sharing (`api/routes/imports.py`)

| Method | Path | Tier | Description |
|---|---|---|---|
| `POST` | `/api/import` | LLM (+ Jev optional) | Queues a background job to fetch a public URL (SSRF-guarded) or weave pasted text into a diglot Markdown lesson. Returns `{job_id, status, queued}`. |
| `GET` | `/api/import/options` | Offline | Available CEFR levels, target-share presets/bounds, and weave grains. |
| `POST` | `/api/import/preview` | Offline | Human-readable one-line summary of the selected import dials. |
| `POST` | `/api/discover` | Offline (web search) | Searches DuckDuckGo, Wikipedia, and curated RSS/Atom feeds; annotates results with inferred register and `already_have` slug. |
| `GET` | `/api/discover/gaps` | Offline | Identifies unread registers on the shelf with unread recommendations and topic-based search queries. |
| `GET` | `/api/suggest` | LLM | Short prose suggestion of what to read next based on finished lessons and saved words. |
| `POST` | `/api/recommend` | LLM | Queues a background job to suggest vocabulary-recycling topics, fetch candidates, and score overlap. Returns `{job_id, status}`. |
| `GET` | `/api/transfer/export/{slug}?creator=...` | Offline | Downloads a `diglot/1` stamped Markdown lesson (`.md` attachment). |
| `POST` | `/api/transfer/inspect` | Offline | Inspects a shared `.md` lesson without importing: verifies digest, checks duplicates, and measures vocabulary coverage. |
| `POST` | `/api/transfer/import` | Offline | Synchronously saves a verified shared lesson into `data/library/` under a collision-free slug and refreshes the library. |

---

## 7. Writing Practice (`api/routes/writing.py`)

| Method | Path | Tier | Description |
|---|---|---|---|
| `GET` | `/api/writing` | Offline | Lists up to 60 writing pieces (batch-loaded revisions), cumulative totals, suggested prompt, and improvement modes. |
| `GET` | `/api/writing/prompt` | Offline | Generates a fresh writing prompt drawn from reading topics, frequently looked-up words, saved words, and kept quotes. |
| `GET` | `/api/writing/{writing_id}` | Offline | Fetches a single piece and all its revisions (`Draft 1`, `Draft 2`, …). |
| `POST` | `/api/writing/measure` | Offline | Live word count, language mix, and prompted-word usage check without writing to disk. |
| `POST` | `/api/writing/check` | Offline / LLM / Jev | Measures, grades (Jev → Tutor; honest error if no model), generates cached inline annotations, and saves/updates a revision. |
| `POST` | `/api/writing/draft` | Offline | Saves a revision bookmark without calling any model. |
| `POST` | `/api/writing/lesson` | LLM | Saves the draft as a revision and queues a background job to weave the reader's own writing into a diglot lesson. |
| `POST` | `/api/writing/improve` | LLM | Returns a side-by-side suggested revision for a chosen mode (`grammar`, `natural`, `concise`, …) without saving automatically. |
| `DELETE` | `/api/writing/{writing_id}` | Offline | Deletes a piece and all its revisions. |

---

## 8. Background Jobs & SSE (`api/routes/jobs.py`)

| Method | Path | Tier | Description |
|---|---|---|---|
| `GET` | `/api/jobs` | Offline | Snapshot of running, queued, and recent finished jobs plus `active` count. |
| `GET` | `/api/jobs/stream` | Offline | Server-Sent Events stream (`text/event-stream`): emits an initial `snapshot` event, then live `job` updates and `: keep-alive` heartbeats; exits cleanly on server shutdown. |
| `GET` | `/api/jobs/{job_id}` | Offline | Current state, step, progress fraction, elapsed seconds, error, and result of one job. |
| `POST` | `/api/jobs/{job_id}/cancel` | Offline | Requests cooperative cancellation of a queued or running job. |
| `DELETE` | `/api/jobs/{job_id}` | Offline | Dismisses a finished job (`404` if missing, `409` if still running). |
| `DELETE` | `/api/jobs` | Offline | Clears all finished jobs from memory and SQLite. |
