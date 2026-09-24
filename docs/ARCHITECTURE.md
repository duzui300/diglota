# Working notes

For whoever is changing this next — most likely me. The README says what the
app does; `LOG.md` says how it got that way. This says what you need in your
head before you touch anything.

---

## The one idea

A **diglot** is a text where two languages are woven together: English carries
the meaning, Spanish is set into it, and each Spanish phrase is glossed where
its meaning is not obvious. The learner reads fluently and absorbs the Spanish
in context. Everything here exists to serve that, and every feature is judged
by whether it makes the reading better.

Two consequences that are easy to forget:

- **The reading surface is sacred.** It is the one place where added work is
  felt directly. Analytics, glossing, progress — all of it rides on requests
  the reader was already making. If you find yourself adding a call to the
  reading path, stop and look for a by-product instead.
- **The corpus format is the app's contract with itself.** What the app reads
  and what it writes are the same format. An imported article can be moved into
  `spanishDiglot/` and be indistinguishable from a hand-made one. Do not add a
  second format; extend this one.

---

## Invariants

Things that are load-bearing and non-obvious. Breaking one of these will not
fail a test immediately.

| Invariant | Why |
| --- | --- |
| `app/diglot.py` has **no imports from the rest of the app** | Everything depends on it; it must stay at the bottom of the stack |
| Spanish/English classification is decided **once**, in `diglot.segment` | Every downstream count — coverage, exposure, the graph — inherits its errors |
| Function words are excluded from every vocabulary measure | They are most of any Spanish text and none of what is being learned |
| The weave falls back rather than aborting | A lesson woven four-fifths of the way is a lesson; an exception is a lost import |
| AI results are cached **by prompt version** | `tutor.PROMPT_VERSION`; without it a prompt fix never reaches anything already cached |
| Background work runs in `JobManager`, bounded to 2 | Each import fans out over chunks; unbounded jobs multiply into rate limits |
| Nothing writes to the corpus directory **except a tag the reader asked for** | The user's hand-made articles are theirs; imports go to `data/library/`. The one exception is a Register the reader changes from the reader, which is a single front-matter line, only in a file holding one passage, and logged when it happens |
| A shared lesson carries **no personal state** | Progress, deck and review history are the reader's. A file that carried them would hand over someone else's history |
| An import never overwrites an existing file | The copy you have might be the one you corrected. A second import gets a suffixed name |
| A kept sentence is a **whole sentence**, cut to the article's own boundaries | Half a sentence is not something anyone wants to be shown again later. A three-word selection widens to its sentence |
| A quote with nothing to learn in it is refused, with a reason | The collection is only useful if everything in it is worth returning to |

---

## Where things live

```
app/
  diglot.py     THE FORMAT: parse + segment + segmenter lexicon   [no deps]
  vocab.py      stemming, coverage measurement
  reading.py    scroll position -> which words were read
  levels.py     the three import dials (level, amount, weave)
  corpus.py     passage graph: shared vocabulary, IDF, clusters
  analytics.py  exposure, scaffolding, headlines
  glossary.py   local translations mined from the corpus
  warm.py       resolves an article's vocabulary before it is clicked
  library.py    the shelves; corpus + imports behind one interface
  store.py      all SQLite; migrations live in _migrate
  srs.py        SM-2, pure functions
  llm.py        chat client (per-call timeout + attempts)
  tutor.py      what the chat model GENERATES  + PROMPT_VERSION
  judge.py      what Jev DECIDES; the Verdict type
  grading.py    the three grading tiers and what may be called wrong
  registers.py  news / essay / conversation / academic / fiction
  transfer.py   the share envelope: stamp, digest, pre-flight, import
  quotes.py     a selection in the reader -> a kept sentence
  challenges.py the weekly goal: six kinds, all measured from existing data
  writing.py    the writing prompt, modes, and the model-free measurements
  ingest.py     article, paste or your own writing -> woven lesson
  fetch.py      URL (or a paste) -> article blocks; keyless search
  recommend.py  vocabulary-driven discovery
  server.py     FastAPI; the App object holds all shared state
static/         no build step; app.js is one file by design
tools/          segment_qa (corpus health), ui_test (browser), ui_import_test
```

**The settings panel writes to `data/settings.json`, and what it writes wins over
the `.env`.** The `.env` is how the machine is configured and the panel is how the
person using it is, so the recent deliberate act takes effect -- and clearing a value
clears it from the file and lets the environment through again, rather than storing
an empty string that shadows a working key. `Settings.apply_saved()` is authoritative
and idempotent for exactly that reason: it resets every panel key to what the
environment gave and then overlays the file, so clearing can genuinely go back.
Applying from a freshly loaded `Settings` instead would read whichever `data_dir` the
*environment* names rather than the one this app is using.

**Keys are `repr=False` in `Settings` and never serialised to the browser.** The
panel is told `key_set` and a four-character hint; the endpoint returns the same. The
repr matters as much as the endpoint: a dataclass printed in a traceback, a log line
or a failing assertion would otherwise put a credential in the output, and that is
not something a future caller should have to remember.

**`tutor` vs `judge` is the central split.** The chat model *generates*; Jev
*judges*. Asking a chat model to grade its own output is asking it to do the
thing it is worst at. Every exercise is graded by Jev and explained by the
tutor, with Jev's verdict passed into the tutor's prompt so the prose explains
the scores rather than re-deciding them.

**But grading must survive Jev being absent**, because most people running this
have no `TYPESAFE_API_KEY`. `grading.Grading` tries three tiers in order and the
verdict says which one answered:

| Tier | Reads meaning? | May call an answer wrong? |
| --- | --- | --- |
| `jev` | yes, with a calibrated probability | yes |
| `tutor` | yes, uncalibrated — a generation model judging | yes |
| `local` | **no** — folded content-word overlap against the article | only where one answer exists (a cloze) |

The third tier is deliberately asymmetric. High overlap is evidence an answer is
right; low overlap is no evidence at all, because a correct translation may share
no words with the reference. So it can confirm and it can fail a cloze, and it
reports everything else as **unverified** — a third state in the record, not a
euphemism for wrong. `grading.may_fail(verdict)` is that distinction; a grade
that cannot acquit must not convict.

---

## Traps that have already cost time

Read this list before debugging anything.

**The segmenter's switch cost is punctuation-sensitive.** Spanish runs begin
after `.`, `,`, `:` — a switch there is discounted. This is why the segmenter
works on this corpus and would need retuning on a different house style.

**`_looks_like_heading_line` and bold spans interact.** Bold is assumed Spanish
unless the span's own words lean English. An English bold label inside a Spanish
paragraph will otherwise drag the paragraph into the Spanish run.

**Gloss attachment is positional.** The parser ends a Spanish span where its
gloss *begins*, so the gloss belongs to the last word of that span. Merging spans
across a gloss (in `_tidy`) moves the gloss downstream — annotated in the code.

**`[hidden]` loses to any author `display` rule.** An empty modal covered the
whole app for a while. The stylesheet has `[hidden] { display: none !important }`
for this reason; do not remove it.

**Sticky elements occupy their normal-flow position.** The reader toolbar must
come *before* `<main>` or it pins below the whole article.

**A windowless start has no stdout at all.** `pythonw.exe` leaves `sys.stdout` as
`None`, so the first `print` raises rather than being ignored and uvicorn's log goes
nowhere. `run.py --log FILE` points both at a file, and the launcher
(`Open Diglot.vbs`) starts it that way: the reader's only clue when something does
not start is that file, so an app started without a console has to have one.

**A hash route does not re-render when you navigate to the page you are already
on.** `location.hash` is unchanged, no `hashchange` fires, and the DOM keeps whatever
it was showing -- a browser check in this suite read a shelf built before the import
it was checking, and reported the import had failed. When a check cares about fresh
data on a hash route, reload.

**A front-matter line belongs to a file, and a file may hold several passages.**
The corpus ships one compilation — four lessons under day headings, `diglot.md` —
whose identification blocks run together into a single front-matter block, and the
parser reports passages without their line ranges. So "set the Register of this
passage" cannot be answered by editing the file: the line lands near the *end of
the run*, which belongs to whichever passage owns that part of the block. This
happened once — a test edited a Register line and thereby retagged the passage after
the one it was looking at — and the repair was to remove the line, having first
proved the original bytes by reconstructing them against a recorded md5. Anything
that edits a file for a passage must ask `Library.passages_per_file` first, and the
route refuses with 409 when the answer is more than one. The reader is not offered a
button that would refuse: `register_editable` rides in the payload.

**A cluster is named from what its members' titles share, not from their text.**
The grouping was always topic-shaped; the *label* was the problem. Naming a group
from the vocabulary its members share in their prose put `escuela · archivado ·
camara` on fifteen articles about art — true, shared, and no help at all in deciding
whether you want the group. Titles are the writer's own label for the subject, and
because the name is drawn from words the reader can see in those titles, it can be
checked rather than believed. Two members is the floor for a word to count, and a
word the other groups use just as much names neither of them — without that, `ai`
labelled every group on this library.

The tail of a title is a source label, not a subject: several arrive as
"… - Wikipedia" or "… | Quanta Magazine", and `wikipedi` scored top of the art
cluster before that was handled. The rule is the shape of the title (a dash, pipe or
em dash plus up to three words) rather than a list of site names, so it survives the
next feed added.

That work also found a hole in `glossary.STOPWORDS`: the short English function
words were missing, and the first version of this named a cluster "of". The list is
shared with the frequent-lookups filter and the exposure totals, so filling the hole
helped all three.

**A drag must not navigate.** The passage map's click handler checks how far the
pointer moved. Anything bound on `click` alone will open an article on every pan.

**Attribution must run from the furthest point reached.** `store.credited_position`
exists so scrolling back and forth does not multiply the words-read count. A
paragraph is credited to the range containing its **start**, not to every range
it overlaps — with saves every six seconds, overlap double-counts everywhere.

**A truncated answer is not a broken one.** When the model runs out of budget
mid-JSON, the text is valid right up to where it stops and `finish_reason` says
`length`. `complete` escalates the budget, but only for *empty* answers, so this
arrives at `complete_json` as a plain parse failure — and everything that fails
softly (the anchors) degrades in silence. `complete_json` asks once more with
three times the room. If an AI feature comes back empty, check the report's notes
for a parse error before suspecting the prompt.

**Heading-shaped lines are not headings.** `_looks_like_heading_line` guesses at
bare lines -- short, no closing punctuation, no Spanish -- because several source
files lost their heading markup. That shape also fits a lot of furniture, and a
reader's contents pane listed it: "Posted April 29, 2026 | Reviewed by Lybi Ma",
the "*The Stories We Tell* | *Loneliness*" kicker, a Markdown table header row, and a
Psychology Today sidebar that appears in the file *twice* (so its four entries
appeared twice). Two rules now carry that: a line with a pipe in it or a
publication-line shape is not a title, and the contents pane drops any heading whose
text occurs more than once in the article -- a section title does not repeat inside
one piece, so a repeat is boilerplate. The body still renders what the file says; it
is navigation that refuses to point at furniture.

**Cancellation is cooperative.** `JobContext.check()` is called at stage
boundaries. Nothing can interrupt a socket read, and pretending otherwise leaves
half-written files.

**The weave amount and its tolerance band are two different things.** Measured on
four live weaves at a requested 22%: 19%, 20%, 22%, 28%. All four were
*accepted* — `tolerance_for(0.22)` is ±7.7 points, 35% relative, so 29% is
inside the specification rather than a failure of it. What looked like the model
overshooting was largely the band being wide at light targets. If the band is
ever tightened, do it with a proper sample: three runs is not enough to tell a
bias from variance, and a fixed correction constant fitted to that would be
fitting noise. What the retry does instead is use the *observed* error —
overshoot by 8 points and it asks for 8 points less next time, which also
crosses into a different density instruction (see below).

**A retry must change the instruction, not repeat it.** The density wording in
the weave prompt is chosen *from* the target number, so re-asking for the same
number with a note saying it was wrong builds the same prompt the model already
ignored. `weave_chunk` now carries a separate `ask` that is corrected by the
measured error, while the requested target stays what acceptance is judged
against and what the front matter records.

**The correction is measured from the number the model was given, not from the
target.** These are not the same, and using the target double-counts the previous
correction. A live import: asked 54%, got 81%, so the retry asked 27%; the model
obeyed and produced 29%, which is near the ask and far from the target, so a
target-based correction swung the next ask to 79%. The loop oscillated instead of
converging — the exact failure the correction was added to prevent. `error =
ratio - ask`.

**Grain is a third dial, and it is not a difficulty.** `weave` decides what unit
the Spanish arrives in: *mixed* (the weaver picks per sentence — a phrase, a
clause, a whole sentence, both languages sharing one sentence where that reads
best) or *whole sentences only* (each sentence entirely one language). The
permission runs one way and only one way: the strict form forbids something the
mixed form allows, and the mixed form forbids nothing the strict form does. So a
test asserts both directions of that, because writing the mixed form as "swap a
phrase and leave the rest alone" would have silently banned whole-sentence
translations — which is what "mixed" is *for*.

**Quality gates built for phrase weaving reject sentence weaving, so the gates
are grain-aware rather than globally loosened.** At sentence grain the amount is
quantised by sentences: a five-sentence paragraph can be 0%, 20% or 40% Spanish
and nothing in between. A tolerance tight enough to mean something at phrase
level is unattainable there, so `tolerance_for` widens (±0.10–0.20 vs ±0.06–0.14)
and the accepted spread loosens (`SENTENCE_SPREAD_LIMIT` 0.86 vs `SPREAD_LIMIT`
0.62) — a sentence-grain weave really is lumpier passage to passage, and flagging
that as a defect would put a completed lesson into an endless reweave.

**The grain is in the weave cache key.** The same passage at the same amount is a
completely different text depending on the unit it arrives in, so a shared key
would serve a mixed weave to a reader who asked for whole sentences — silently,
and forever.

**`resolve_weave` accepts a `Weave` and returns it unchanged.** Every function in
the pipeline takes "a code or a weave" and hands it on. The version that only
understood strings returned the *default* for a `Weave`, so asking for whole
sentences quietly produced mixed ones and the two prompts were byte-identical.
That was found by printing both prompts side by side, not by a test — the test
came after.

**Captions, photo credits, proper nouns, work titles and quoted speech are not
prose.** A live import wove a photo credit token by token. The weave prompt says
to leave them alone, in both grains.

**A marker is only markup when it is a whole whitespace-delimited unit.**
`parse_spans` lifts `**bold**` and `(*gloss*)` out into placeholder tokens,
splits on whitespace, and recognises a token only if it fills a unit on its own.
So `**presento**un` and `amenaza (*threat*)o` are not markup — and because the
token is what carries the text, a glued marker does not merely lose its emphasis,
it loses the word. Markers are therefore *unit boundaries* as well as tokens
(`_UNIT_SPLIT`); anything that writes this format must bound its markers with
whitespace, and `transfer._span_markup` is the worked example.

This cost 136 words. `**será**,` and `**conlleva**.` were being deleted from the
reading text across 73 spans in the corpus, and the failure was invisible because
`_tidy` strips leftover markers as a safety net — so instead of visible garbage,
the sentence simply had a hole in it. If you change anything about how markup is
split or recognised, run `test_every_bold_span_in_the_corpus_survives_into_the_text`,
which asserts the one invariant that matters: what is inside `**...**` must still
be somewhere in the parse.

**Handlers on elements that outlive the view must be bound once.** `#selection-bar`
and `#reader-bar` live in `index.html`, not inside the re-rendered view, so
`addEventListener` on their buttons accumulates: the reader used to re-bind on
every article, and one click then ran one handler per article ever opened, each
pinned to the slug that was current when it was added. Translations were asked
with the wrong article as context and kept sentences were saved against the wrong
lesson. Anything persistent is bound once and reads live state — see `readerSlug`.

**A captured gloss must not keep the marks it was wrapped in.** Sources write
`(*to become*)`, `(**to become**)` and `(*to become*):`; the last two leave
asterisks or a colon inside the value, which the reader shows. `_clean_gloss` is
the single place that strips them, and both the inline and the vocabulary-box
paths go through it.

**Writing is a piece with revisions, not a row per attempt.** A check keeps the
version it judged, because feedback belongs to an exact text; checking the same
text twice updates that revision rather than adding one. Nothing about a piece is
denormalised onto it -- there is no "latest text" column, for the same reason the
quotes table has no folded search column.

**A failed AI result must not be cached.** `App.cached` stores whatever it is
given, so an outage remembered against a text means that text never gets an
answer. `_writing_feedback` in the server remembers only real feedback; the
lookup path has the same idea with `FAILURE_TTL`. If you add a cached AI call,
decide what happens when the call fails -- a failure cached forever is worse than
no cache.

**Free writing is graded by two tiers, not three, and the missing one is the
point.** Reviewing a paragraph requires reading it; a word-overlap comparison can
say nothing about whether Spanish holds together. `Grading.grade_writing` returns
an error verdict with no model rather than inventing a judgment -- and the
measurements (length, language mix, which prompted words appeared) are produced
with no model at all, so something useful always comes back.

**A failed feedback attempt must not be stored as feedback.** The tutor returns
`{"failed": True}` when it cannot be reached; the endpoint drops that instead of
writing "the tutor could not be reached" into the record as a note about the
writing. Same reason the error text is one short sentence: a learner who just
wrote something should not be shown an upstream status code.

**"Completed" and "ended" are different states.** A challenge that is reached
keeps its row active so the panel can say so; `ended_at` means the reader moved
on. Setting both at once hid the goal at the exact moment it was achieved --
check which of the two a new write is really asserting.

**A goal must be measurable from data that already exists.** If a new kind of
challenge needs a counter of its own, it is the wrong kind -- the app has been
built so that reading is instrumented once and everything reads from that. A kind
whose measurement cannot be computed (the topic clusters, before the graph is
built) is not offered at all rather than offered as a permanent zero.

**A fact about a word belongs on every path that shows the word.** The review
queue comes from two queries -- cards in rotation and new cards -- written
separately with the column list copied between them, so a column added to one and
not the other gives a feature that works until the first review and then silently
disappears. Both carry `lemma` and `pos`; a test drives a card through both.
Recognise and Produce mode are the same trap for the reveal.

**Writing notes are placed on the reader's text, so they must be true of it.**
`writing.keep_notes` drops any note whose fragment is not in the text character
for character, and the UI gives each note the first occurrence nothing else has
claimed -- overlapping highlights would mean two things at once. Nothing in the
workspace ever replaces what the reader wrote: a suggestion is shown beside it.

**A module and its main function must not share a name in the same file.**
`from .recommend import recommend` shadows the module, so `recommend.broaden(...)`
became "function has no attribute broaden" — a 500 at runtime, not at import.
Import the specific name you need under a name of its own (`recommend_broaden`),
or the module itself.

**Register is a guess and is labelled as one.** Everything downstream inherits
its errors: a discovery suggestion, a library filter, a reading challenge. The
signals are deliberately conservative and abstain rather than guess, and the
corpus still has more `unspecified` than anything else. Before *adding* a signal,
check what it does to the abstention count — the ones that looked obviously
right (any quotation, `according to`, a leading hyphen) were each wrong in a way
that took a measurement to see.

**Writing the format is harder than reading it, and only a round trip proves it.**
`tests/test_transfer.py` exports every real lesson and reads it back. It is the
only test in the suite that re-reads what the app writes, and it found two
serialiser bugs in minutes plus a third that was already live: every imported
lesson's grammar notes had been stored with a stray leading `*`, because the
canonical writer puts the colon *inside* the italics (`- *Example:* "..."`) and
the parser split the value on it. Run that file before touching how the format is
written.

**Export stamps the author's file; it does not regenerate it.** Regenerating from
the parsed model is canonical but lossy — text with malformed markup parses into
literal asterisks, and re-emitting them re-introduces markup and can swallow the
words after it. `LibraryEntry.path` exists so a lesson can leave as the file its
author wrote; regeneration is the fallback for a compilation holding several.

**A digest mismatch on import is a warning, not a refusal.** Hand-fixing a typo in
a shared lesson is a reasonable thing to do, so `transfer.inspect` reports
`intact: false` and lets the reader decide.

**An imported lesson is untrusted text.** It is stored as prose and escaped on
render, so it cannot inject markup — but it *is* later fed to the tutor for
explanations and quizzes, so a crafted file can try to steer the model's prose.
The blast radius is small (the tutor writes text and takes no actions) and this is
a known boundary rather than a solved problem.

**`Settings()` is not `Settings.load()`.** The dataclass field defaults read
`os.environ` at construction, and `.env` is loaded by `load_env()` — so a bare
`Settings(...)` on a fresh process sees no keys at all, silently, and every
model call quietly falls to the local tier. The app uses `Settings.load()`.
Tests use the bare constructor deliberately, to get a no-network app.

**A score of 0.5 is doubt, not a pass.** It is what Jev's coin flip and the
local tier's "unverified" both look like. `grading.CORRECT_AT` is deliberately
above the midpoint: reading doubt as correctness flatters the learner exactly
the way reading it as failure punishes them. Both happened here.

**`is_provisional` is not `may_fail`.** Provisional means "no reader produced
this grade". `may_fail` means "this grade has standing to say you were wrong".
They differ on the two ends: an exact match from the local tier is provisional
and passes, a wrong cloze from it is provisional and fails.

**A declared register beats an inferred one, and the UI must say which.** An
author who says what a piece is is right; a heuristic guessing is not, and a
confident wrong register is worse than none, because the whole point is that the
reader trusts the label. `registers.infer` abstains unless a score is clearly
ahead — keep it that way when adding signals.

---

## Data flow, in one pass

```
corpus .md  ──parse──>  Article ──┬──> Library ──> /api/library ──> shelves, filters
                                  │
                                  ├──> reading.windows_for ──> exposure, corpus graph
                                  ├──> glossary.build ──> instant lookups (+ shipped to browser)
                                  └──> library.coverage ──> "you know 94% of this"

URL  ──fetch──>  html ──extract──┐
                                 ├──> blocks ──ingest──> woven .md ──(same parse)──> Article
paste ───────────────────────────┘
                                    │
                            job queue (2 workers, persisted, cancellable)
```

**A paste stands in for the fetch, not for the lesson.** `blocks_from_text` is the
paste's half of `fetch_url` + `extract_article` and nothing else: everything after
the blocks — focus vocabulary, the three dials, the weave, the anchors, the
round-trip check, the front matter — is one code path for both. A second, reduced
path would drift from the first within a release, and the reader would end up with
two kinds of article that behave differently. Its structure has to be inferred
from whitespace (blank lines separate blocks; single newlines inside a block are
hard wrapping and get joined back), and with no blank lines at all the newlines
themselves become the boundaries — one enormous paragraph is the one shape the
weave cannot distribute Spanish across.

**The slug's digest identifies the source, and the source of a paste is its text.**
Keying it on the (absent) URL would make every pasted article with the same
headline the same lesson. And a write never overwrites: a repeated import gets a
suffixed name, which the notes below have claimed all along while the code wrote
straight over the top.

**A piece of the reader's own writing is one translation away from the same
pipeline.** `import_writing` puts the passage into English and then calls
`import_article` — the same three dials, the same focus vocabulary, the same
anchors. The alternative, a lighter path for "your own text", would leave two
kinds of lesson that behave differently and a bug that reproduces in one of them.

**For English input the translation step uses the model's judgement, not its
output.** This is the whole subtlety of that step: it runs on every piece, so the
case that has to be right is the one where it should do nothing. A model asked to
put English "into English" paraphrases it, and the piece is the reader's own
writing — their words are the point. So the answer carries the language it found,
and when that language is English the original text is what gets woven. A piece
written in Spanish comes back with the reader's own sentences as the *Spanish*,
which is a happy consequence rather than a design.

**The original passage identifies the lesson, not the translation of it**, and it
is what the slug digests. Two runs must agree; a revision must not look like the
version before it.

**Two minimums, on purpose.** A fetched page and a paste are both held to 120
words of prose — a headline or a summary is not enough to weave a lesson from, and
finding that out afterwards wastes the whole weave. A piece the reader *wrote* is
held to 40, which is the floor the writing workspace itself asks for: refusing
someone's own saved paragraph with "paste the article's body" is answering a
question they did not ask.

**A truncated JSON answer is a parse failure with a cause.** `complete` escalates
the budget, but only when the answer came back *empty*; text that is merely cut
short is returned as-is with `finish_reason="length"`. `complete_json` now asks
once more with three times the room. A lesson woven from the reader's own writing
lost its entire vocabulary box this way — and because the anchors degrade softly by
design, the only evidence was an empty vocabulary box and one line in the report.

Reading a whole article changes: `article_progress`, `exposure` (words read),
`events` (time), `lookups` (clicks), `scaffolding` (mode changes). None of it
costs the reader a request.

---

## Conventions

- **Never write a comment that says what the code does.** Only why: a hidden
  constraint, a workaround, something a reader would otherwise get wrong.
- **Tests pin meanings, not just behaviour.** "Understood means in the deck",
  "the three dials are independent" — a quiet redefinition turns a panel into a
  plausible-looking lie.
- **Prefer deriving to storing.** Coverage, exposure splits and cluster names
  are all computed from what already exists. Every extra column is a thing that
  can go stale.
- **Fail soft after the expensive part.** A weave that finished should not be
  thrown away because the post-reading notes failed.
- **Say when something did not work.** The LOG records failures and open
  questions, not just wins.

---

## Running it

```bash
pip install -r requirements.txt
python run.py                        # http://127.0.0.1:8787

python -m pytest tests/ -q           # 530 unit tests
python tools/ui_test.py              # 275 browser checks (294 with --with-ai)
python tools/ui_import_test.py       # slow: imports a real article end to end
python tools/segment_qa.py           # corpus segmentation health (want 0 suspect)
```

The server falls back to a machine-wide `.env` if the project has none. Outbound web
requests need the proxy in `DIGLOT_PROXY`; direct connections time out here.

To see the grading fallback, start a server with no key — `load_dotenv` does not
override an existing variable, so setting it empty wins:

```bash
TYPESAFE_API_KEY= python run.py --port 8788    # exercises fall back to the tutor
```

`/api/status` reports which tier will answer under `grading`.

**Backup habit:** commit after each feature lands, locally. `data/` is ignored
on purpose — it is the user's deck and reading history, not source.
