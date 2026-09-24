/* Diglot — client
 *
 * Vanilla JS, no build step. Four views behind a hash router, plus two overlays
 * (word popover, modal) and a toast queue.
 *
 * The reader is the point of the app, so it gets the most care here:
 *  - Spanish spans are split into per-word click targets, each independently
 *    look-up-able and savable;
 *  - reading options (glosses, focus, text size, theme) persist across sessions;
 *  - scroll position is saved back to the server so a long article resumes
 *    where it was left, which is what makes reading in sittings practical.
 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/* local glossary ----------------------------------------------------------- */

/* The corpus already contains the author's own translations -- inline glosses,
   bolded focus phrases, the vocabulary box. The server indexes all of it and
   ships the whole index here once, so clicking a word the article already
   taught needs no request at all. The full lookup still goes out in the
   background to record the click and to fill in parts of speech and examples;
   what the reader sees does not wait for it. */

let glossaryIndex = null;
let glossaryLoading = null;

const fold = (text) => String(text).toLowerCase()
  .normalize('NFD').replace(/\p{M}/gu, '');

async function ensureGlossary() {
  if (glossaryIndex) return glossaryIndex;
  if (!glossaryLoading) {
    glossaryLoading = api('/api/glossary')
      .then(data => { glossaryIndex = data.entries || {}; return glossaryIndex; })
      .catch(() => { glossaryIndex = {}; return glossaryIndex; });
  }
  return glossaryLoading;
}

function localEntry(term) {
  if (!glossaryIndex) return null;
  return glossaryIndex[`w:${fold(term)}`] || null;
}

/* ---------------------------------------------------------------- api ----- */

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!response.ok) {
    const detail = (data && (data.detail || data.error)) || `HTTP ${response.status}`;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return data;
}

/* -------------------------------------------------------------- state ----- */

const store = {
  get(key, fallback) {
    try { const raw = localStorage.getItem(`diglot.${key}`); return raw === null ? fallback : JSON.parse(raw); }
    catch { return fallback; }
  },
  set(key, value) { try { localStorage.setItem(`diglot.${key}`, JSON.stringify(value)); } catch { /* private mode */ } },
};

const prefs = {
  theme: store.get('theme', 'paper'),
  gloss: store.get('gloss', true),
  dim: store.get('dim', false),
  esOnly: store.get('esOnly', false),
  size: store.get('size', 19.5),
};

function applyPrefs() {
  document.documentElement.dataset.theme = prefs.theme;
  document.documentElement.style.setProperty('--reader-size', `${prefs.size}px`);
  document.body.classList.toggle('hide-gloss', !prefs.gloss);
  document.body.classList.toggle('dim-english', prefs.dim);
  document.body.classList.toggle('es-only', prefs.esOnly);
  $$('.theme-switch button').forEach(b => b.classList.toggle('active', b.dataset.themeSet === prefs.theme));
  const g = $('#opt-gloss'), d = $('#opt-dim'), e = $('#opt-es-only');
  if (g) { g.checked = prefs.gloss; d.checked = prefs.dim; e.checked = prefs.esOnly; }
}

function savePref(key, value) {
  prefs[key] = value;
  store.set(key, value);
  applyPrefs();
  reportMode(key, value);
}

/* Mode changes are the one scaffolding signal the server cannot observe for
   itself -- it never sees which reading mode you were in. Sent with
   `sendBeacon`, which hands the request to the browser and returns
   immediately, so a toggle costs the reader nothing. Only fires on an actual
   change, which is a handful of events per session. */
function reportMode(key, value) {
  const kinds = {
    gloss: value ? 'gloss-on' : 'gloss-off',
    esOnly: value ? 'spanish-only' : null,
    dim: value ? 'focus' : null,
  };
  const kind = kinds[key];
  if (!kind || !navigator.sendBeacon) return;
  const slug = location.hash.startsWith('#/read/')
    ? decodeURIComponent(location.hash.replace('#/read/', '')) : null;
  try {
    navigator.sendBeacon('/api/scaffolding',
      new Blob([JSON.stringify({ kind, slug })], { type: 'application/json' }));
  } catch { /* telemetry must never break the reader */ }
}

/* ------------------------------------------------------------------ jobs --
 *
 * One source of truth for background work. The server pushes a job's state
 * over an event stream and this keeps the latest of each; the Activity panel,
 * the import dialog and the completion pop-ups all render from that same map,
 * so closing the dialog does not lose the job and reopening it does not
 * restart anything.
 */

const jobState = new Map();
const notifiedJobs = new Set();
let eventSource = null;
let pollTimer = null;
let reconnectTimer = null;

function activeJobCount() {
  return [...jobState.values()].filter(j => j.status === 'running' || j.status === 'queued').length;
}

function updateJob(job) {
  const previous = jobState.get(job.id);
  jobState.set(job.id, job);
  const settled = job.status === 'done' || job.status === 'failed' || job.status === 'cancelled';

  if (settled && !notifiedJobs.has(job.id)) {
    notifiedJobs.add(job.id);
    // A pop-up wherever the user is: the whole point of running the work in the
    // background is that they have walked away from the dialog by now.
    if (job.status === 'done') {
      const detail = (job.kind === 'import' || job.kind === 'writing') && job.result
        ? `${job.result.words} words · ${Math.round((job.result.spanish_ratio || 0) * 100)}% Spanish`
        : 'finished';
      toast(`✓ ${job.title.slice(0, 60)} — ${detail}`, 'ok');
      openLibraryIfIdle();
    } else if (job.status === 'failed') {
      toast(`✕ ${job.title.slice(0, 50)} — ${(job.error || 'failed').slice(0, 140)}`, 'err');
    } else {
      toast(`Cancelled: ${job.title.slice(0, 50)}`);
    }
  }
  void previous;
  renderActivity();
  renderJobDetail(job.id);
  if (job.kind === 'recommend') renderRecommendResult(job.id);
  refreshJobPill();
}

function refreshJobPill() {
  const pill = $('#job-pill');
  const count = activeJobCount();
  pill.hidden = !count;
  pill.textContent = count;
}

/* Reload the library view when an import lands, but only if the reader is
   sitting on it doing nothing -- never yank the page out from under someone
   who is mid-article. */
function openLibraryIfIdle() {
  if (location.hash === '' || location.hash === '#/library') viewLibrary();
}

function renderActivity() {
  const list = $('#activity-list');
  if (!list) return;
  const jobs = [...jobState.values()].sort((a, b) => (b.created_at || 0) - (a.created_at || 0));

  if (!jobs.length) {
    list.innerHTML = `<p class="muted small" style="padding:20px 4px">
      Nothing running. Imported articles and searches show up here while they work,
      and stay afterwards so you can see how they went.</p>`;
    return;
  }
  list.innerHTML = jobs.map(jobCard).join('');
  $$('[data-job-cancel]', list).forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    try { await api(`/api/jobs/${button.dataset.jobCancel}/cancel`, { method: 'POST' }); }
    catch (err) { toast(err.message, 'err'); }
  }));
  $$('[data-job-dismiss]', list).forEach(button => button.addEventListener('click', async () => {
    try {
      await api(`/api/jobs/${button.dataset.jobDismiss}`, { method: 'DELETE' });
      jobState.delete(button.dataset.jobDismiss);
      renderActivity();
      refreshJobPill();
    } catch (err) { toast(err.message, 'err'); }
  }));
  $$('[data-job-open]', list).forEach(button => button.addEventListener('click', () => {
    const slug = button.dataset.jobOpen;
    closeActivity();
    location.hash = `#/read/${encodeURIComponent(slug)}`;
  }));
}

const JOB_LABEL = { import: 'Importing', recommend: 'Finding something to read',
                    writing: 'Weaving your writing' };
const JOB_ICON = { running: '◐', queued: '○', done: '✓', failed: '✕', cancelled: '⊘' };

/* The four stages of a card's life, with the words and colours used wherever
   they are shown. One definition, because Progress and the deck page name the
   same four things and two lists of labels would drift. */
const STAGE_ORDER = [['new', 'New', 'var(--stage-1)'], ['learning', 'Learning', 'var(--stage-2)'],
                     ['young', 'Young', 'var(--stage-3)'], ['mature', 'Mature', 'var(--stage-4)']];
const STAGE_COLOR = Object.fromEntries(STAGE_ORDER.map(([key, , color]) => [key, color]));

function jobCard(job) {
  const pct = Math.round((job.fraction || 0) * 100);
  const running = job.status === 'running' || job.status === 'queued';
  const slug = job.result && job.result.slug;

  let body = '';
  if (job.status === 'running') {
    body = `<div class="job-step">${escapeHtml(job.step || 'working')}${job.detail ? ` · ${escapeHtml(job.detail)}` : ''}</div>
      <div class="bar thin"><i style="width:${pct}%"></i></div>`;
  } else if (job.status === 'queued') {
    body = `<div class="job-step muted">waiting for a free worker</div>`;
  } else if (job.status === 'done' && job.result) {
    const r = job.result;
    // A lesson whose post-reading notes failed is still a lesson, so the job
    // succeeds — but it should say so rather than looking identical to one
    // that came out complete.
    const partial = r.anchors && !r.anchors.vocab && !r.anchors.grammar
      ? '<div class="job-step" style="color:var(--warn)">the post-reading notes could not be generated</div>'
      : '';
    // Say when the Spanish is unevenly distributed: the headline percentage is
    // an average, and an average over lumpy paragraphs describes no paragraph
    // the reader will actually meet.
    const uneven = (r.spread || 0) > 0.45
      ? `<div class="job-step" style="color:var(--warn)">the Spanish is unevenly spread —
           paragraphs differ by ${Math.round(r.spread * 100)} points</div>`
      : '';
    // Content that did not survive the weave is the one failure a reader cannot
    // notice for themselves, so it is worth saying out loud.
    const lost = (r.retained || 1) < 0.9
      ? `<div class="job-step" style="color:var(--warn)">
           only ${Math.round((r.retained || 0) * 100)}% of the source text survived the weave</div>`
      : '';
    body = r.words
      ? `<div class="job-step">${r.words} words · ${Math.round((r.spanish_ratio || 0) * 100)}% Spanish
           ${r.level ? ` · ${escapeHtml(r.level)}` : ''}
           · ${(r.focus || []).length} focus phrases</div>${partial}${uneven}${lost}`
      : `<div class="job-step">${(r.candidates || []).length} candidates found</div>`;
  } else if (job.status === 'failed') {
    body = `<div class="job-step" style="color:var(--bad)">${escapeHtml((job.error || 'failed').slice(0, 200))}</div>`;
  } else {
    body = `<div class="job-step muted">cancelled</div>`;
  }

  return `<div class="job ${job.status}" data-job-id="${job.id}" data-job-status="${job.status}">
    <div class="job-head">
      <span class="job-icon" title="${job.status}">${JOB_ICON[job.status] || '•'}</span>
      <span class="job-title" title="${escapeHtml(job.title)}">${escapeHtml(job.title.slice(0, 90))}</span>
      <span class="spacer"></span>
      <span class="job-kind">${escapeHtml(JOB_LABEL[job.kind] || job.kind)}</span>
      ${running ? `<button class="icon-btn" data-job-cancel="${job.id}" title="Stop this job">⊘</button>`
                : `<button class="icon-btn" data-job-dismiss="${job.id}" title="Dismiss">✕</button>`}
    </div>
    ${body}
    ${slug ? `<div class="row" style="margin-top:8px">
      <button class="btn sm primary" data-job-open="${escapeHtml(slug)}">Read it</button>
      <span class="small muted">${Math.round(job.elapsed || 0)}s</span></div>`
      : `<div class="small muted" style="margin-top:4px">${Math.round(job.elapsed || 0)}s</div>`}
  </div>`;
}

/* The import dialog embeds a live view of its own job, so it and the Activity
   panel are never showing different things. */
function renderJobDetail(jobId) {
  const host = document.querySelector(`[data-job-detail="${jobId}"]`);
  if (!host) return;
  const job = jobState.get(jobId);
  if (!job) return;
  host.innerHTML = jobCard(job);
  const open = $('[data-job-open]', host);
  if (open) open.addEventListener('click', () => {
    closeModal();
    location.hash = `#/read/${encodeURIComponent(open.dataset.jobOpen)}`;
  });
  const cancel = $('[data-job-cancel]', host);
  if (cancel) cancel.addEventListener('click', async () => {
    cancel.disabled = true;
    try { await api(`/api/jobs/${jobId}/cancel`, { method: 'POST' }); } catch { /* shown in the panel */ }
  });
}

function openActivity() { $('#activity').hidden = false; renderActivity(); }
function closeActivity() { $('#activity').hidden = true; }

function connectJobStream() {
  if (!window.EventSource) { pollJobs(); return; }
  eventSource = new EventSource('/api/jobs/stream');
  eventSource.onmessage = (event) => {
    let message;
    try { message = JSON.parse(event.data); } catch { return; }
    if (message.type === 'snapshot') {
      // Arriving here after a reconnect: adopt the server's view wholesale.
      (message.jobs || []).forEach(job => {
        if (job.status !== 'running' && job.status !== 'queued') notifiedJobs.add(job.id);
        jobState.set(job.id, job);
      });
      renderActivity();
      refreshJobPill();
    } else if (message.type === 'job') {
      updateJob(message.job);
    }
  };
  eventSource.onerror = () => {
    // The stream is a convenience, so a drop falls back to polling rather than
    // leaving the panel stale. Polling is the fallback, not the destination:
    // try the stream again after a while in case the drop was transient.
    if (eventSource) { eventSource.close(); eventSource = null; }
    pollJobs();
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(() => {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      connectJobStream();
    }, 30000);
  };
}

function pollJobs() {
  if (pollTimer) return;
  const tick = async () => {
    try {
      const data = await api('/api/jobs');
      (data.jobs || []).forEach(job => {
        const known = jobState.get(job.id);
        if (job.status !== 'running' && job.status !== 'queued' && !known) notifiedJobs.add(job.id);
        jobState.set(job.id, job);
      });
      renderActivity();
      refreshJobPill();
    } catch { /* offline; try again next tick */ }
  };
  tick();
  pollTimer = setInterval(tick, 4000);
}

async function loadJobs() {
  try {
    const data = await api('/api/jobs');
    (data.jobs || []).forEach(job => {
      if (job.status !== 'running' && job.status !== 'queued') notifiedJobs.add(job.id);
      jobState.set(job.id, job);
    });
  } catch { /* the panel will fill in when the stream connects */ }
  renderActivity();
  refreshJobPill();
}

/* ------------------------------------------------------------ toasts ------ */

function toast(message, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = message;
  $('#toasts').append(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 250); }, kind === 'err' ? 5200 : 2600);
}

const escapeHtml = (s) => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* The corpus and the tutor both write light markdown inside prose -- grammar
   notes quote **será** and *volverse*, vocabulary examples quote the lesson.
   Escape first, then re-introduce only the inline tags we intend, so article
   text can never inject markup. */
function mdInline(text) {
  return escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
}

/* Explanations come back as a handful of `- **Label:** text` lines. Rendered as
   plain text the dashes read as punctuation soup in a narrow panel, so leading
   bullets become actual list rows. */
function mdBlock(text) {
  return String(text || '').split(/\n+/).map(line => {
    const trimmed = line.trim();
    if (!trimmed) return '';
    const bullet = trimmed.match(/^[-*]\s+(.*)$/);
    return bullet ? `<div class="li">${mdInline(bullet[1])}</div>` : `<div>${mdInline(trimmed)}</div>`;
  }).join('');
}

/* ------------------------------------------------------------ speech ------ */

let spanishVoice = null;
let englishVoice = null;

function pickVoice() {
  const voices = window.speechSynthesis ? speechSynthesis.getVoices() : [];
  spanishVoice = voices.find(v => /^es(-|_)?/i.test(v.lang) && /ES|MX|US|AR|CO/i.test(v.lang))
    || voices.find(v => /^es/i.test(v.lang)) || null;
  // Read-along alternates languages, so it needs a voice for each.
  englishVoice = voices.find(v => /^en(-|_)?US/i.test(v.lang))
    || voices.find(v => /^en/i.test(v.lang)) || null;
}

if (window.speechSynthesis) {
  pickVoice();
  speechSynthesis.addEventListener('voiceschanged', pickVoice);
}

/* Read-along --------------------------------------------------------------
 *
 * Plays the article a sentence at a time, switching voice between the two
 * languages within each sentence. A diglot read aloud by a person sounds
 * exactly like that -- the Spanish in a Spanish voice, the English in an
 * English one -- and reading only the Spanish fragments is what produced the
 * four-utterance mess this replaced.
 *
 * The glosses are skipped: they are a printed annotation, and hearing
 * "(*annals*)" read out is noise rather than language.
 */

const reader = { active: false, token: 0, node: null };

function utteranceFor(text, lang) {
  const utterance = new SpeechSynthesisUtterance(text);
  const voice = lang === 'es' ? spanishVoice : englishVoice;
  utterance.lang = voice ? voice.lang : (lang === 'es' ? 'es-ES' : 'en-US');
  if (voice) utterance.voice = voice;
  utterance.rate = lang === 'es' ? 0.92 : 1.0;
  return utterance;
}

function speakAsync(text, lang, token) {
  return new Promise((resolve) => {
    if (!window.speechSynthesis || reader.token !== token || !text.trim()) { resolve(); return; }
    const utterance = utteranceFor(text, lang);
    const done = () => resolve();
    utterance.onend = done;
    utterance.onerror = done;
    speechSynthesis.speak(utterance);
  });
}

/* The runs of one sentence, with adjacent runs of the same language joined.
 * This is what puts a Spanish sentence back together after a bold span split
 * it: three fragments become one utterance, as they were written. */
function sentenceRuns(sentence) {
  const runs = [];
  for (const child of sentence.children) {
    if (child.classList.contains('gloss')) continue;
    const lang = child.classList.contains('es') ? 'es' : 'en';
    const text = child.textContent.replace(/\s+/g, ' ').trim();
    if (!text) continue;
    const last = runs[runs.length - 1];
    if (last && last.lang === lang) last.text += ` ${text}`;
    else runs.push({ lang, text });
  }
  return runs;
}

function highlight(node) {
  if (reader.node) reader.node.classList.remove('speaking');
  reader.node = node;
  if (node) node.classList.add('speaking');
}

function setReadButton(active) {
  const button = $('#btn-read-aloud');
  if (!button) return;
  button.textContent = active ? '⏸ Stop reading' : '▶ Read aloud';
  button.classList.toggle('active', active);
}

function stopReading() {
  reader.active = false;
  reader.token += 1;
  if (window.speechSynthesis) speechSynthesis.cancel();
  highlight(null);
  setReadButton(false);
}

async function readAloud(fromNode) {
  const sentences = $$('#prose .sent');
  if (!sentences.length) { toast('Nothing to read here'); return; }
  stopReading();

  reader.active = true;
  reader.token += 1;
  const myToken = reader.token;
  const start = fromNode ? Math.max(sentences.indexOf(fromNode), 0) : 0;
  setReadButton(true);

  for (let i = start; i < sentences.length; i += 1) {
    if (!reader.active || reader.token !== myToken) return;
    const sentence = sentences[i];
    highlight(sentence);
    sentence.scrollIntoView({ block: 'center', behavior: 'smooth' });
    for (const run of sentenceRuns(sentence)) {
      if (reader.token !== myToken) return;
      await speakAsync(run.text, run.lang, myToken);
    }
    if (reader.token !== myToken) return;
    await new Promise(resolve => setTimeout(resolve, 120));
  }
  if (reader.token === myToken) stopReading();
}

function toggleReading() {
  if (reader.active) stopReading(); else readAloud(reader.node || null);
}

function speak(text) {
  if (!window.speechSynthesis) { toast('This browser has no speech synthesis', 'err'); return; }
  stopReading();
  speechSynthesis.speak(utteranceFor(text, 'es'));
  if (!spanishVoice) toast('No Spanish voice installed — using the default');
}

/* selection toolbar -------------------------------------------------------- */

/* Selecting a passage is the natural gesture for "what does this bit say?" in a
   text that is half in a language you are still learning. The three actions are
   the three things a reader wants at that moment: hear it, translate it, or
   have the grammar explained. */

function hideSelectionBar() { $('#selection-bar').hidden = true; }

function selectionTarget(englishText) {
  // Spanish if the passage looks Spanish; English otherwise. Cheap and good
  // enough -- the two languages are being told apart all over this app.
  return /[áéíóúñü¿¡]|\b(el|la|los|las|de|que|en|con|para|por|una?|es|son|se|del|al)\b/i
    .test(englishText) ? 'English' : 'Spanish';
}

function showSelectionBar() {
  const bar = $('#selection-bar');
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed) { hideSelectionBar(); return; }
  const text = selection.toString().trim();
  const prose = $('#prose');
  if (!text || text.length < 2 || !prose || !prose.contains(selection.anchorNode)) {
    hideSelectionBar();
    return;
  }
  const rect = selection.getRangeAt(0).getBoundingClientRect();
  if (!rect.width && !rect.height) { hideSelectionBar(); return; }

  bar.hidden = false;
  const width = bar.offsetWidth || 260;
  bar.style.left = `${Math.max(12, Math.min(
    rect.left + window.scrollX + rect.width / 2 - width / 2,
    window.scrollX + window.innerWidth - width - 12))}px`;
  bar.style.top = `${rect.top + window.scrollY - bar.offsetHeight - 8}px`;
}

/* The article the reader is showing, for the selection actions to work against.

   Module-level rather than closed over, because the selection bar outlives the
   article it was wired for: re-binding its buttons on every visit used to stack
   one handler per article opened, each still holding the slug that was current
   when it was added. Asking for a translation then sent the tutor whichever
   article had been open first, and keeping a sentence saved it against the wrong
   lesson. One set of handlers, reading the live slug. */
let readerSlug = null;

function wireSelectionBar(slug) {
  const bar = $('#selection-bar');
  readerSlug = slug;
  hideSelectionBar();

  if (wireSelectionBar.bound) return;
  wireSelectionBar.bound = true;

  $$('#selection-bar button').forEach(button => button.addEventListener('mousedown', (event) => {
    event.preventDefault();   // keep the selection alive through the click
  }));

  $$('#selection-bar button').forEach(button => button.addEventListener('click', async () => {
    const text = (window.getSelection() || {}).toString().trim();
    hideSelectionBar();
    if (!text) return;

    if (button.dataset.sel === 'speak') { speak(text); return; }
    if (button.dataset.sel === 'quote') { await keepSelection(readerSlug); return; }

    const target = button.dataset.sel === 'translate' ? selectionTarget(text) : null;
    showLookupPanel(text, $('#prose'));
    try {
      const result = target
        ? await api('/api/translate', { method: 'POST', body: { text, target } })
        : await api('/api/explain', { method: 'POST', body: { text, sentence: text } });
      const body = target
        ? `<div class="gloss-big">${escapeHtml(result.translation || '')}</div>
           <div class="small muted" style="margin-top:6px">translated into ${escapeHtml(target)}</div>`
        : `<div class="explanation">${mdBlock(result.explanation)}</div>`;
      setLookupPanel(text, target ? `into ${target}` : 'grammar', body);
    } catch (err) {
      setLookupPanel(text, '', `<div class="muted small">${escapeHtml(err.message)}</div>`);
    }
  }));

  document.addEventListener('mouseup', () => setTimeout(showSelectionBar, 0));
  document.addEventListener('scroll', hideSelectionBar, { passive: true });
}

/* The popover doubles as the panel for selection actions, so a translation and
   a dictionary entry look and behave the same way. */
function showLookupPanel(term, anchor) {
  const pop = $('#popover');
  popoverAnchor = anchor;
  $('#pop-term').textContent = term.length > 60 ? `${term.slice(0, 60)}…` : term;
  $('#pop-sub').textContent = 'working…';
  $('#pop-body').innerHTML = '<div class="loading"><span class="spinner"></span> asking the tutor</div>';
  $('#pop-actions').innerHTML = '';
  pop.hidden = false;
  placePopover(anchor);
}

function setLookupPanel(term, sub, bodyHtml) {
  $('#pop-term').textContent = term.length > 60 ? `${term.slice(0, 60)}…` : term;
  $('#pop-sub').textContent = sub;
  $('#pop-body').innerHTML = bodyHtml;
}

/* Keeping a sentence ------------------------------------------------------- */

/* The reader already renders every sentence as its own element, so a selection
   arrives at sentence granularity and this only has to collect the ones it
   touched. Whatever the reader selected -- a phrase, three words, half a
   sentence -- the kept quote is always whole sentences, because half a sentence
   is not something anyone wants to be shown again later. */
function nodeOf(node) {
  return node && node.nodeType === 3 ? node.parentElement : node;
}

function sentenceText(el) {
  // The glosses are rendered inside the sentence but are not part of the
  // article's text -- the parser stores them beside the Spanish, not within it.
  // Leaving them in would make the quote fail to match the sentence it came
  // from, and the span markup already shows them where they belong.
  const clone = el.cloneNode(true);
  clone.querySelectorAll('.gloss').forEach(gloss => gloss.remove());
  return clone.textContent.replace(/\s+/g, ' ').trim();
}

function selectedSentences() {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || !selection.rangeCount) return [];
  const range = selection.getRangeAt(0);
  const prose = $('#prose');
  if (!prose) return [];
  const first = nodeOf(range.startContainer) && nodeOf(range.startContainer).closest('.sent');
  const last = nodeOf(range.endContainer) && nodeOf(range.endContainer).closest('.sent');
  if (!first || !last || !prose.contains(first) || !prose.contains(last)) return [];

  const all = $$('.sent', prose);
  const from = all.indexOf(first), to = all.indexOf(last);
  if (from < 0 || to < 0) return [];

  // Grouped by paragraph: a selection across two paragraphs is two quotes, and
  // each is located in the paragraph it actually came from.
  const groups = new Map();
  for (let i = Math.min(from, to); i <= Math.max(from, to); i++) {
    const block = all[i].closest('[data-block]');
    if (!block) continue;
    const key = block.dataset.block;
    if (!groups.has(key)) groups.set(key, []);
    const text = sentenceText(all[i]);
    if (text) groups.get(key).push(text);
  }
  return [...groups.entries()].map(([block, parts]) => ({
    block_index: Number(block), text: parts.join(' '),
  }));
}

async function keepSelection(slug) {
  const parts = selectedSentences();
  if (!parts.length) { toast('Select a sentence first'); return; }
  window.getSelection().removeAllRanges();
  hideSelectionBar();

  let kept = 0, already = 0, reason = '';
  for (const part of parts) {
    try {
      const result = await api('/api/quote', { method: 'POST', body: { slug, ...part } });
      if (result.created) kept += 1; else already += 1;
    } catch (err) {
      reason = err.message;
    }
  }
  if (kept) {
    toast(kept === 1 ? 'Kept that sentence' : `Kept ${kept} sentences`);
  } else if (already && !reason) {
    toast('That sentence is already in your quotes');
  } else if (reason) {
    toast(reason, 'err');
  }
}

/* ---------------------------------------------------------- overlays ------ */

function openModal(title, bodyHtml) {
  $('#modal-title').textContent = title;
  $('#modal-body').innerHTML = bodyHtml;
  $('#modal').hidden = false;
}

function closeModal() { $('#modal').hidden = true; }

$('#modal-close').addEventListener('click', closeModal);
$('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') closeModal(); });

/* word popover ------------------------------------------------------------- */

let popoverAnchor = null;

function placePopover(anchor) {
  const pop = $('#popover');
  const rect = anchor.getBoundingClientRect();
  const width = pop.offsetWidth || 344;
  const height = pop.offsetHeight || 220;
  let left = rect.left + window.scrollX - 12;
  left = Math.max(12, Math.min(left, window.scrollX + window.innerWidth - width - 12));
  let top = rect.bottom + window.scrollY + 8;
  if (rect.bottom + height + 24 > window.innerHeight && rect.top - height - 8 > 0) {
    top = rect.top + window.scrollY - height - 8;
  }
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;
}

function closePopover() { $('#popover').hidden = true; popoverAnchor = null; }

$('#pop-close').addEventListener('click', closePopover);

/* Clicking anywhere else dismisses the panel.
 *
 * Bound on `mousedown` in the capture phase rather than on `click`, because
 * clicking a *different* word has to close this panel and open that one, and
 * both happen on the same gesture: mousedown closes, click reopens. A `click`
 * handler on the document would run after the word's own handler and shut the
 * panel it had just opened. */
document.addEventListener('mousedown', (event) => {
  if ($('#popover').hidden) return;
  const target = event.target;
  if (target.closest && (target.closest('#popover') || target.closest('#selection-bar'))) return;
  closePopover();
}, true);

async function lookupWord(word, sentence, slug, anchor, savedTerm, options = {}) {
  const deep = options.deep === true;
  const pop = $('#popover');
  popoverAnchor = anchor;
  $('#pop-term').textContent = word;
  $('#pop-sub').textContent = deep ? 'asking the tutor…' : 'looking up…';
  $('#pop-body').innerHTML = `<div class="loading"><span class="spinner"></span>
    ${deep ? 'asking the tutor for the full entry' : 'looking up'}</div>`;
  $('#pop-actions').innerHTML = '';
  pop.hidden = false;
  placePopover(anchor);

  // Paint from the local index first, if this word is one the corpus already
  // translates. The request below still goes out -- it records the click and
  // offers the fuller tutor entry -- but the reader is not made to wait for it.
  // Skipped on a deliberate deep lookup, where the reader has already asked for
  // the slower answer and a flash of the short one would just be noise.
  const local = deep ? null : localEntry(word);
  if (local) {
    paintEntry(local, { word, sentence, slug, anchor, savedTerm, provisional: true });
    placePopover(anchor);
  }

  let data;
  try {
    data = await api(`/api/word/lookup?term=${encodeURIComponent(word)}`
      + `&sentence=${encodeURIComponent(sentence)}`
      + `&slug=${encodeURIComponent(slug)}${deep ? '&deep=true' : ''}`);
  } catch (err) {
    if (!local) {
      $('#pop-sub').textContent = '';
      $('#pop-body').innerHTML = `<div class="muted small">${escapeHtml(err.message)}</div>`;
    }
    return;
  }
  if (popoverAnchor !== anchor) return;   // the user moved on

  if (data.error) {
    if (local) return;                    // the local entry is still good
    $('#pop-sub').textContent = '';
    $('#pop-body').innerHTML = `<div class="muted small">${escapeHtml(data.error)}</div>`;
    return;
  }
  paintEntry(data, { word, sentence, slug, anchor, savedTerm, provisional: false });
}

function paintEntry(data, { word, sentence, slug, anchor, savedTerm, provisional }) {
  $('#pop-term').textContent = data.lemma || word;
  const source = data.source && data.source !== 'your deck'
    ? (data.source === 'focus' ? 'from this lesson' : `from ${data.source}`)
    : (data.source === 'your deck' ? 'from your deck' : '');
  $('#pop-sub').textContent = [
    data.pos,
    data.display && data.display !== data.lemma ? `as “${data.display}”` : '',
    source,
  ].filter(Boolean).join(' · ');

  const bits = [];
  if (data.looked_up_before > 1) {
    bits.push(`<div class="nudge">You have looked this up ${data.looked_up_before} times —
      it is probably worth saving.</div>`);
  }
  if (data.gloss) bits.push(`<div class="gloss-big">${escapeHtml(data.gloss)}</div>`);
  if (data.sense) bits.push(`<div class="small muted">${escapeHtml(data.sense)}</div>`);
  if (data.note) bits.push(`<div style="margin-top:8px">${escapeHtml(data.note)}</div>`);
  if (data.example) {
    bits.push(`<div style="margin-top:10px">
      <div class="ex">${escapeHtml(data.example)}</div>
      <div class="ex-en">${escapeHtml(data.example_en || '')}</div></div>`);
  }
  if (data.related && data.related.length) {
    bits.push(`<div style="margin-top:9px" class="small muted">also: ${data.related.map(escapeHtml).join(' · ')}</div>`);
  }
  // The connection that makes a collection worth keeping: you meet a word again
  // and the app shows you the sentences you chose it from. Shown here rather
  // than behind a link, because the value is in seeing it without leaving the
  // page you are reading.
  if (data.quotes && data.quotes.length) {
    bits.push(`<div class="pop-quotes">
      <div class="small muted">In your quotes</div>
      ${data.quotes.slice(0, 2).map(quote => `<div class="pop-quote">${escapeHtml(
        quote.es.length > 96 ? `${quote.es.slice(0, 96)}…` : quote.es)}</div>`).join('')}
      <a class="small" href="#/quotes">${data.quotes.length === 1 ? 'That sentence' : 'Those sentences'} →</a>
    </div>`);
  }
  if (provisional) {
    bits.push('<div class="loading" style="margin-top:8px"><span class="spinner"></span> full entry</div>');
  }
  $('#pop-body').innerHTML = bits.join('') || '<div class="muted small">No entry.</div>';

  if (provisional) {
    // Nothing to save yet: the model is about to return a lemma worth storing.
    $('#pop-actions').innerHTML = '<button class="btn sm ghost" id="pop-say">🔊 Hear it</button>';
    $('#pop-say').addEventListener('click', () => speak(word));
    return;
  }

  const already = savedTerm && data.lemma && savedTerm.has((data.lemma || '').toLowerCase());
  // A word the corpus already translates gets a no-waiting answer, but that
  // answer is a translation and nothing else -- no lemma, part of speech,
  // example or learner's note. The tutor is one click away rather than
  // automatic, because paying seconds for it on every click is what made
  // lookups slow in the first place.
  const fromCorpus = !data.model && !data.error;
  $('#pop-actions').innerHTML = `
    <button class="btn sm ${already ? '' : 'primary'}" id="pop-save" ${already ? 'disabled' : ''}>
      ${already ? '✓ Saved' : '＋ Save word'}
    </button>
    <button class="btn sm ghost" id="pop-say">🔊 Hear it</button>
    ${fromCorpus
      ? '<button class="btn sm ghost" id="pop-deep" title="Ask the tutor for the lemma, an example and a note">✨ Full entry</button>'
      : ''}
    <button class="btn sm ghost" id="pop-explain">💡 Explain</button>`;

  if ($('#pop-deep')) {
    $('#pop-deep').addEventListener('click', () =>
      lookupWord(word, sentence, slug, anchor, savedTerm, { deep: true }));
  }
  $('#pop-explain').addEventListener('click', () => explainPassage(data.display || word, sentence, anchor));

  $('#pop-say').addEventListener('click', () => speak(data.display || word));

  $('#pop-save').addEventListener('click', async () => {
    try {
      const result = await api('/api/word/save', {
        method: 'POST',
        body: {
          term: data.display || data.term || word,
          // A corpus entry carries no lemma of its own, so fall back to the form
          // the article indexed it under rather than the bare surface form the
          // reader happened to click: clicking "vuelve" should save under
          // "se vuelve" if that is the entry the index matched.
          lemma: data.lemma || data.term || word.toLowerCase(),
          gloss: data.gloss || null,
          pos: data.pos || null,
          note: [data.sense, data.note].filter(Boolean).join(' — ') || null,
          article_slug: slug,
          context: sentence,
        },
      });
      $('#pop-save').textContent = '✓ Saved';
      $('#pop-save').disabled = true;
      $$('.w').forEach(el => { if (el.dataset.word === (data.display || word)) el.classList.add('saved'); });
      toast(`Saved “${data.lemma || word}” · ${result.total} words`);
      refreshDuePill();
    } catch (err) { toast(err.message, 'err'); }
  });

  // Reposition last. The panel was placed when it still held a spinner or the
  // short local entry, so a full entry -- which is several times taller -- would
  // otherwise hang off the bottom of the viewport, flipping above the word only
  // when it was already too late.
  placePopover(anchor);
}

/* Ask for a grammar explanation and append it to whichever panel is open --
   the word popover or the selection toolbar's. Kept as its own function
   because both call it. */
async function explainPassage(text, sentence, anchor) {
  const body = $('#pop-body');
  if (!body) return;
  body.insertAdjacentHTML('beforeend',
    '<div class="loading" id="pop-explain-out" style="margin-top:10px">'
    + '<span class="spinner"></span> working out the grammar</div>');
  try {
    const result = await api('/api/explain', {
      method: 'POST',
      body: { text, sentence, question: null },
    });
    const out = $('#pop-explain-out');
    if (out) out.outerHTML = `<div class="explanation">${mdBlock(result.explanation)}</div>`;
  } catch (err) {
    const out = $('#pop-explain-out');
    if (out) out.outerHTML = `<div class="muted small">${escapeHtml(err.message)}</div>`;
  }
  placePopover(anchor);
}

/* --------------------------------------------------------------- util ----- */

/* How grading is configured, from /api/status. Null until that answers. Kept
   here because the reader is about to be told they were right or wrong, and who
   decided that is part of the answer -- while the call that carries it is one the
   page already makes. Only two states matter here: a calibrated judge, or not.
   The local tier shows up per-verdict, where it actually happens, since without
   a tutor there are no exercises to grade at all. */
let gradingInfo = null;

function gradingNote() {
  if (!gradingInfo || gradingInfo.primary === 'jev') return '';
  return `<div class="grading-note">No calibrated judge is configured — the tutor model
    grades these, so the scores are rougher than usual.</div>`;
}

async function refreshDuePill() {
  try {
    const status = await api('/api/status');
    const pill = $('#due-pill');
    pill.hidden = !status.due;
    pill.textContent = status.due;
    gradingInfo = status.grading || null;
  } catch { /* the pill is not worth an error toast */ }
}

function setActiveTab(name) {
  $$('.tabs a').forEach(a => a.classList.toggle('active', a.dataset.tab === name));
}

/* -- quotes -------------------------------------------------------------- */

/* The collection of sentences the reader kept. It has one job beyond showing
   them: making them findable. A learner remembers the meaning, or one word, or
   where they were -- never the exact Spanish -- so search covers the sentence,
   its translation, its glosses, its note and its focus word, and it folds
   accents, because nobody types `cuestión` from memory. */
let quoteSearch = '';
let quoteSource = 'all';
let quoteTimer = null;

const QUOTE_EMPTY = `<div class="empty"><h3>No sentences kept yet</h3>
  <p>Select a sentence while reading and press <strong>Keep</strong>. Sentences are what a
  language is made of — a word on its own is a translation, but a sentence is the memory
  of how it was used.</p>
  <p class="small muted">Keeping one costs nothing and calls no model, so it never
  interrupts the reading.</p></div>`;

function quoteSpans(spans) {
  // The reader's own markup, so a sentence in the collection looks like the
  // sentence it was cut from. No word-click handlers here: this is a place to
  // read your own collection, and a clickable word that did nothing would be
  // worse than a plain one.
  return spans.map(span => {
    if (span.lang !== 'es') return `<span class="en">${escapeHtml(span.text)}</span>`;
    const classes = `es${span.target ? ' target' : ''}${span.bold ? ' bold' : ''}`;
    return `<span class="${classes}">${escapeHtml(span.text)}</span>`
      + (span.gloss ? `<span class="gloss">${escapeHtml(span.gloss)}</span>` : '');
  }).join('');
}

async function viewQuotes() {
  setActiveTab('quotes');
  $('#reader-bar').hidden = true;
  document.body.classList.remove('dim-english', 'es-only');
  const view = $('#view');
  view.className = 'view';
  view.innerHTML = `
    <div class="page-head">
      <h1>Quotes</h1>
      <p id="quote-summary" class="muted"></p>
      <div class="field quote-search">
        <input type="search" id="quote-q" placeholder="Search your sentences — in Spanish or English"
               value="${escapeHtml(quoteSearch)}" aria-label="Search kept sentences">
      </div>
    </div>
    <div id="quote-sources" class="row filter-row"></div>
    <div id="quote-list"></div>`;

  const input = $('#quote-q');
  input.addEventListener('input', () => {
    clearTimeout(quoteTimer);
    // Long enough that typing a word does not fire a request per letter, short
    // enough that the list feels like it is keeping up.
    quoteTimer = setTimeout(() => {
      quoteSearch = input.value.trim();
      loadQuotes();
    }, 180);
  });

  await loadQuotes();
}

async function loadQuotes() {
  const params = new URLSearchParams();
  if (quoteSearch) params.set('q', quoteSearch);
  if (quoteSource !== 'all') params.set('slug', quoteSource);
  const query = params.toString();
  const list = $('#quote-list');
  if (!list) return;
  list.innerHTML = '<div class="empty"><span class="spinner"></span></div>';

  const data = await api(`/api/quotes${query ? `?${query}` : ''}`);
  const total = data.total || 0;
  const summary = $('#quote-summary');
  if (summary) {
    summary.textContent = total
      ? `${total} sentence${total === 1 ? '' : 's'} kept from ${data.sources.length}
         article${data.sources.length === 1 ? '' : 's'}.`
      : '';
  }
  renderQuoteSources(data.sources || []);

  if (!total) { list.innerHTML = QUOTE_EMPTY; return; }
  if (!data.quotes.length) {
    list.innerHTML = `<div class="empty"><h3>Nothing matches</h3>
      <p>No kept sentence contains “${escapeHtml(quoteSearch)}”.</p></div>`;
    return;
  }
  list.innerHTML = (quoteSearch || quoteSource !== 'all')
    ? `<div class="count-line">${data.quotes.length}
        ${data.quotes.length === 1 ? 'sentence' : 'sentences'} match</div>`
      + data.quotes.map(quoteCard).join('')
    : data.quotes.map(quoteCard).join('');
  wireQuoteCards(list);
}

function renderQuoteSources(sources) {
  const host = $('#quote-sources');
  if (!host) return;
  if (sources.length < 2) { host.innerHTML = ''; return; }
  // Only sources that still exist can be filtered to; a quote whose article was
  // removed is still the reader's, but there is no shelf to filter by.
  const live = sources.filter(source => source.slug && source.title !== '(removed)');
  if (!live.length) { host.innerHTML = ''; return; }
  if (quoteSource !== 'all' && !live.some(source => source.slug === quoteSource)) quoteSource = 'all';
  host.innerHTML = `
    <button class="choice sm ${quoteSource === 'all' ? 'active' : ''}" data-source="all">
      Every article</button>
    ${live.map(source => `
      <button class="choice sm ${source.slug === quoteSource ? 'active' : ''}"
              data-source="${escapeHtml(source.slug)}"
              title="${escapeHtml(source.title)}">
        ${escapeHtml(source.title.length > 34 ? `${source.title.slice(0, 34)}…` : source.title)}
        <span class="muted">${source.count}</span>
      </button>`).join('')}`;
  $$('[data-source]', host).forEach(button => button.addEventListener('click', () => {
    quoteSource = button.dataset.source;
    loadQuotes();
  }));
}

function quoteCard(row) {
  const body = row.spans && row.spans.length
    ? quoteSpans(row.spans)
    : `<span class="es">${escapeHtml(row.es || row.text)}</span>`;
  const term = row.term ? `<span class="chip accent" title="The word this lesson emphasised">${escapeHtml(row.term)}</span>` : '';
  const source = row.article_slug
    ? `<a class="quote-source" href="#/read/${encodeURIComponent(row.article_slug)}"
          title="Read this sentence where it came from">${escapeHtml(row.source_title || row.article_slug)}</a>`
    : '';
  const lost = row.present === false && row.spans
    ? '<span class="small muted" title="The article this came from has changed or gone">source changed</span>' : '';
  return `<article class="quote" data-id="${row.id}">
    <p class="quote-text">${body}</p>
    ${row.en ? `<p class="quote-en">${escapeHtml(row.en)}</p>` : ''}
    <div class="quote-meta">
      ${term}${source}${lost}
      <span class="spacer"></span>
      <button class="btn ghost sm" data-note>${row.note ? 'Edit note' : 'Add note'}</button>
      <button class="btn ghost sm" data-remove>Remove</button>
    </div>
    <p class="quote-note" ${row.note ? '' : 'hidden'}>${escapeHtml(row.note || '')}</p>
    <div class="quote-note-edit" hidden>
      <textarea rows="2" placeholder="What is worth remembering about this sentence?">${escapeHtml(row.note || '')}</textarea>
      <div class="row" style="margin-top:8px">
        <button class="btn primary sm" data-save-note>Save note</button>
        <button class="btn ghost sm" data-cancel-note>Cancel</button>
      </div>
    </div>
  </article>`;
}

function wireQuoteCards(host) {
  $$('.quote', host).forEach(card => {
    const id = Number(card.dataset.id);
    const edit = card.querySelector('.quote-note-edit');

    card.querySelector('[data-note]').addEventListener('click', () => {
      edit.hidden = false;
      card.querySelector('textarea').focus();
    });
    card.querySelector('[data-cancel-note]').addEventListener('click', () => { edit.hidden = true; });
    card.querySelector('[data-save-note]').addEventListener('click', async () => {
      const note = card.querySelector('textarea').value.trim();
      try {
        await api(`/api/quote/${id}`, { method: 'PATCH', body: { note } });
        const line = card.querySelector('.quote-note');
        line.textContent = note;
        line.hidden = !note;
        card.querySelector('[data-note]').textContent = note ? 'Edit note' : 'Add note';
        edit.hidden = true;
        toast(note ? 'Note saved' : 'Note removed');
      } catch (err) { toast(err.message, 'err'); }
    });

    // Two clicks rather than a confirm dialog: a sentence is one thing, and a
    // dialog for one thing is heavier than the loss it prevents. The second
    // click is the confirmation, and it lapses on its own.
    const remove = card.querySelector('[data-remove]');
    let armed = false;
    remove.addEventListener('click', async () => {
      if (!armed) {
        armed = true;
        remove.textContent = 'Remove?';
        remove.classList.add('danger');
        setTimeout(() => {
          armed = false;
          remove.textContent = 'Remove';
          remove.classList.remove('danger');
        }, 3000);
        return;
      }
      try {
        await api(`/api/quote/${id}`, { method: 'DELETE' });
        card.remove();
        toast('Removed from your quotes');
        loadQuotes();
      } catch (err) { toast(err.message, 'err'); }
    });
  });
}

/* -- a goal for the week --------------------------------------------------- */

/* One goal, a week, measured from what the reader already did. The app proposes
   and the reader decides; nothing here nags. Everything on the panel is either
   a number the app already had or a sentence explaining why the goal is being
   suggested, because a goal you cannot argue with is a chore. */
async function renderChallenge() {
  const host = $('#challenge-panel');
  if (!host) return;
  let data;
  try {
    data = await api('/api/challenge');
  } catch {
    host.innerHTML = '';
    return;
  }
  const challenge = data.challenge;
  if (challenge && !challenge.expired && !challenge.completed_at) {
    host.innerHTML = activeChallenge(challenge);
    $('#challenge-give-up').addEventListener('click', () => stopChallenge(challenge.id));
    return;
  }
  const justDone = challenge && challenge.completed_at && challenge.just_completed;
  host.innerHTML = `${justDone ? `<div class="cheer">✓ ${escapeHtml(challenge.title)} — done.
      ${challenge.done} ${escapeHtml(challenge.unit)}. That is the week.</div>` : ''}
    ${challenge && challenge.expired ? `<div class="cheer expired">That week ran out at
      ${challenge.done} of ${challenge.target} ${escapeHtml(challenge.unit)}. No matter —
      here is another.</div>` : ''}
    ${suggestCard(data.suggested, data.kinds)}`;
  wireSuggest(data.kinds);
}

function activeChallenge(challenge) {
  const percent = Math.round(challenge.ratio * 100);
  const left = challenge.days_left;
  return `<div class="panel challenge">
    <div class="challenge-head">
      <h3>${escapeHtml(challenge.title)}</h3>
      <span class="challenge-when">${left > 0
        ? `${left} day${left === 1 ? '' : 's'} left` : 'last day'}</span>
    </div>
    <div class="challenge-bar"><i style="width:${percent}%"></i></div>
    <div class="challenge-count"><strong>${challenge.done}</strong> of ${challenge.target}
      ${escapeHtml(challenge.unit)}</div>
    <p class="small muted">${escapeHtml(challenge.why)}</p>
    <button class="btn ghost sm" id="challenge-give-up">Give up on this</button>
  </div>`;
}

function suggestCard(suggested, kinds) {
  if (!suggested) return '';
  return `<div class="panel challenge">
    <div class="challenge-head"><h3>This week</h3>
      <span class="challenge-when">nothing running</span></div>
    <p class="challenge-what">${escapeHtml(suggested.title)}</p>
    <p class="small muted">${escapeHtml(suggested.reason)}</p>
    <p class="small muted">${escapeHtml(suggested.why)}</p>
    <div class="row" style="margin-top:12px">
      <button class="btn primary" id="challenge-start">Start this</button>
      <select id="challenge-kind" aria-label="Choose a different goal">
        ${(kinds || []).map(kind => `<option value="${escapeHtml(kind.id)}"
          ${kind.id === suggested.kind ? 'selected' : ''}>${escapeHtml(kind.label)}</option>`).join('')}
      </select>
      <select id="challenge-target" aria-label="Choose a target"></select>
    </div>
  </div>`;
}

/* The target list belongs to the chosen kind, so it is rebuilt whenever the kind
   changes -- a target is meaningless without knowing what is being counted. */
function wireSuggest(kinds) {
  const start = $('#challenge-start');
  if (!start) return;
  const kindSelect = $('#challenge-kind');
  const targetSelect = $('#challenge-target');
  const fill = () => {
    const kind = (kinds || []).find(entry => entry.id === kindSelect.value);
    targetSelect.innerHTML = (kind ? kind.targets : []).map((target, index) => `
      <option value="${target}" ${index === 0 ? 'selected' : ''}>${target} ${escapeHtml(kind.unit)}</option>`).join('');
  };
  kindSelect.addEventListener('change', fill);
  fill();
  start.addEventListener('click', async () => {
    start.disabled = true;
    try {
      await api('/api/challenge', { method: 'POST', body: {
        kind: kindSelect.value, target: Number(targetSelect.value),
      } });
      toast('Started — a week from today');
      await renderChallenge();
    } catch (err) { start.disabled = false; toast(err.message, 'err'); }
  });
}

async function stopChallenge(id) {
  try {
    await api(`/api/challenge/${id}`, { method: 'DELETE' });
    toast('Stopped. It stays in the history.');
    await renderChallenge();
  } catch (err) { toast(err.message, 'err'); }
}

/* -- writing workspace ----------------------------------------------------- */

/* Producing Spanish is the skill nothing else in the app could help with, so
   this is the one screen with a text box -- and it is a workspace rather than a
   form, because writing is a loop: draft, read it back, revise, read it back.

   Three things shape it.

   **A version is kept whenever the text is read or deliberately kept.** Feedback
   belongs to the exact text it judged; a note about a sentence that has since
   been edited is worse than no note. So a check writes a revision, and checking
   the same text twice updates that revision instead of inventing another.

   **A suggestion is never applied for the reader.** "Improve this" returns a
   revision shown *beside* their own, because a better piece than the one they
   wrote is not feedback -- the comparison is the lesson. Nothing is replaced
   unless they say so.

   **Nothing typed is ever lost.** The working text is autosaved to this browser
   on every keystroke, and the measurements come from the server's own segmenter
   on a debounce, so the Spanish share is exact rather than approximated.

   Annotations are checked against the text before they are shown: a fragment the
   tutor did not quote exactly cannot be highlighted, and a highlight in the
   wrong place teaches the wrong thing. */
let writeState = {
  pieceId: null,
  prompt: null,
  modes: [],
  checked: null,      // the revision whose feedback is on screen
  marks: [],          // the located annotations, for the popover
  timers: {},
};

function draftKey(pieceId) { return `writeDraft:${pieceId || 'new'}`; }

async function viewWrite(pieceId) {
  setActiveTab('write');
  $('#reader-bar').hidden = true;
  document.body.classList.remove('dim-english', 'es-only');
  const view = $('#view');
  view.className = 'view';
  view.innerHTML = `
    <div class="page-head write-head">
      <div>
        <h1 id="write-title">Write</h1>
        <p class="muted" id="write-summary"></p>
      </div>
      <button class="btn ghost sm" id="write-new">Start a new piece</button>
    </div>
    <div class="write-grid">
      <div class="write-canvas">
        <div class="write-prompt-box">
          <div class="small muted" id="write-source"></div>
          <p class="write-prompt" id="write-prompt"></p>
          <button class="btn ghost sm" id="write-another">Another prompt</button>
        </div>
        <textarea id="write-text" spellcheck="false"
          placeholder="Escribe aquí. No busques las palabras — usa las que tienes."></textarea>
        <div class="write-foot">
          <span class="small muted" id="write-count"></span>
          <span class="spacer"></span>
          <input type="file" id="write-file" hidden
                 accept=".txt,.md,.markdown,.text,text/plain,text/markdown">
          <button class="btn ghost sm" id="write-open"
                  title="Read a text file into the box. It stays on this machine.">Open a file</button>
          <button class="btn ghost sm" id="write-over" title="Clear the box. The piece and its versions stay.">Start over</button>
          <button class="btn ghost sm" id="write-draft">Keep as draft</button>
          <button class="btn ghost sm" id="write-lesson"
                  title="Translate it if it is not in English, then weave Spanish in">Make a lesson</button>
          <button class="btn primary sm" id="write-check">Check this draft</button>
        </div>
        <div id="write-stale-slot"></div>
        <div id="write-annotated"></div>
      </div>
      <aside class="write-side">
        <section id="write-feedback" class="write-block"></section>
        <section class="write-block">
          <h4>Improve this</h4>
          <p class="small muted">A suggestion, shown beside your words. Nothing is replaced until you say so.</p>
          <div class="row write-modes" id="write-modes"></div>
          <div id="write-suggestion"></div>
        </section>
        <section class="write-block" id="write-versions"></section>
        <section class="write-block" id="write-history"></section>
      </aside>
    </div>`;

  writeState = { pieceId: pieceId || null, prompt: null, modes: [], checked: null,
                 marks: [], timers: {} };
  wireWrite();
  if (pieceId) await loadPiece(pieceId); else await loadWritingPrompt();
  await loadWritings();
}

function wireWrite() {
  const text = $('#write-text');
  const grow = () => {
    text.style.height = 'auto';
    text.style.height = `${Math.max(240, text.scrollHeight + 2)}px`;
  };

  text.addEventListener('input', () => {
    grow();
    rememberDraft();
    clearTimeout(writeState.timers.measure);
    writeState.timers.measure = setTimeout(measureDraft, 900);
    markStale();
  });
  $('#write-check').addEventListener('click', checkDraft);
  $('#write-draft').addEventListener('click', keepDraft);
  $('#write-over').addEventListener('click', startOver);
  $('#write-open').addEventListener('click', openFile);
  $('#write-file').addEventListener('change', (event) => readFileIn(event.target));
  $('#write-lesson').addEventListener('click', openLesson);
  $('#write-another').addEventListener('click', () => { loadWritingPrompt(); });
  $('#write-new').addEventListener('click', () => {
    // With a piece open this is a navigation; with a blank page there is nothing
    // to navigate to, so it means "clear what I have typed" and asks first.
    if (writeState.pieceId) { location.hash = '#/write'; return; }
    startOver();
  });
}

/* The working text lives in this browser, on every keystroke. It is not a
   revision -- nothing the server keeps is written until the reader checks or
   keeps it -- but closing the tab should never cost a paragraph. */
function rememberDraft() {
  const box = $('#write-text');
  if (!box) return;
  store.set(draftKey(writeState.pieceId), box.value);
}

function restoreDraft() {
  const saved = store.get(draftKey(writeState.pieceId), '');
  const box = $('#write-text');
  if (!box) return false;
  if (typeof saved === 'string' && saved.trim()) {
    box.value = saved;
    return true;
  }
  return false;
}

async function measureDraft() {
  // The measure is debounced, so it can fire after the reader has left the view
  // and the box is gone. Nothing to measure is not an error.
  const box = $('#write-text');
  if (!box) return;
  const text = box.value;
  const words = text.trim() ? text.trim().split(/\s+/).length : 0;
  if (!words) { $('#write-count').textContent = ''; return; }
  try {
    const data = await api('/api/writing/measure', { method: 'POST', body: {
      text, prompt: (writeState.prompt || {}).instruction || '',
      words: (writeState.prompt || {}).words || [],
    }});
    const reading = data.reading || {};
    $('#write-count').innerHTML = [
      `<strong>${reading.words || 0}</strong> words`,
      `${reading.sentences || 0} sentences`,
      `<strong>${Math.round((reading.spanish_share || 0) * 100)}%</strong> Spanish`,
      reading.enough ? '' : 'aim for 40–120',
    ].filter(Boolean).join(' · ');
  } catch {
    // The count is a convenience; a failed measure should not become an error.
    $('#write-count').textContent = `${words} words`;
  }
}

async function loadWritingPrompt() {
  try {
    const data = await api('/api/writing/prompt');
    writeState.prompt = data.prompt;
    writeState.modes = data.modes || [];
    paintPrompt();
    paintModes();
    if (!writeState.pieceId) {
      restoreDraft();
      $('#write-text').dispatchEvent(new Event('input'));
    }
  } catch (err) {
    $('#write-prompt').textContent = `Could not build a prompt: ${err.message}`;
  }
}

function paintPrompt() {
  const prompt = writeState.prompt || {};
  $('#write-prompt').textContent = prompt.instruction || '';
  $('#write-source').textContent = prompt.source ? `Using ${prompt.source}` : '';
}

function paintModes() {
  const host = $('#write-modes');
  if (!host || !writeState.modes.length) return;
  host.innerHTML = writeState.modes.map(mode => `
    <button class="btn ghost sm" data-mode="${escapeHtml(mode.id)}"
            title="${escapeHtml(mode.blurb)}">${escapeHtml(mode.label)}</button>`).join('');
  $$('[data-mode]', host).forEach(button => button.addEventListener('click', () => {
    improveDraft(button.dataset.mode, button);
  }));
}

async function loadPiece(pieceId) {
  try {
    const data = await api(`/api/writing/${pieceId}`);
    writeState.modes = data.modes || writeState.modes;
    const piece = data.writing;
    writeState.pieceId = piece.id;
    writeState.prompt = { instruction: piece.prompt, words: [], source: '' };
    $('#write-title').textContent = piece.title;
    paintPrompt();
    paintModes();

    const restored = restoreDraft();
    const latest = (piece.revisions || [])[piece.revisions.length - 1];
    if (!restored && latest) $('#write-text').value = latest.text;
    $('#write-text').dispatchEvent(new Event('input'));
    if (restored && latest && $('#write-text').value.trim() !== latest.text.trim()) {
      toast('Restored the draft you had not checked yet');
    }
    if (latest) applyRevision(latest);
    renderVersions(piece);
  } catch (err) {
    $('#write-title').textContent = 'Write';
    $('#write-prompt').textContent = `Could not open that piece: ${err.message}`;
  }
}

/* -- checking and keeping -------------------------------------------------- */

async function checkDraft() {
  const text = $('#write-text').value.trim();
  if (!text) { toast('Write something first'); return; }
  const button = $('#write-check');
  button.disabled = true;
  $('#write-feedback').innerHTML =
    '<div class="loading"><span class="spinner"></span> reading it back</div>';
  try {
    const data = await api('/api/writing/check', { method: 'POST', body: {
      text, prompt: (writeState.prompt || {}).instruction || '',
      words: (writeState.prompt || {}).words || [], writing_id: writeState.pieceId,
    }});
    adoptPiece(data.writing_id, data.writing);
    applyRevision(data.revision, { reading: data.reading, note: data.note,
                                  verdict: data.verdict, feedback: data.feedback });
  } catch (err) {
    $('#write-feedback').innerHTML = `<p class="small" style="color:var(--bad)">${escapeHtml(err.message)}</p>`;
  }
  button.disabled = false;
}

async function keepDraft() {
  const text = $('#write-text').value.trim();
  if (!text) { toast('Write something first'); return; }
  const button = $('#write-draft');
  button.disabled = true;
  try {
    const data = await api('/api/writing/draft', { method: 'POST', body: {
      text, prompt: (writeState.prompt || {}).instruction || '',
      words: (writeState.prompt || {}).words || [], writing_id: writeState.pieceId,
    }});
    adoptPiece(data.writing_id, data.writing);
    toast('Kept as a version of this piece');
  } catch (err) { toast(err.message, 'err'); }
  button.disabled = false;
}

/* A new piece is created by the first save, so the address has to catch up
   without re-running the route -- reloading here would throw away the feedback
   that was just rendered. */
function adoptPiece(pieceId, piece) {
  if (writeState.pieceId !== pieceId) {
    const carried = store.get(draftKey(writeState.pieceId), '');
    writeState.pieceId = pieceId;
    if (typeof carried === 'string') store.set(draftKey(pieceId), carried);
    history.replaceState(null, '', `#/write/${pieceId}`);
  }
  store.set(draftKey(pieceId), $('#write-text').value);
  if (piece) {
    $('#write-title').textContent = piece.title || 'Write';
    renderVersions(piece);
  }
}

function startOver() {
  const button = $('#write-over');
  // Two clicks rather than a confirm dialog: this discards text that has not
  // been checked yet, and the second click is the confirmation. Versions already
  // kept are untouched, which is what makes it safe to offer at all.
  if (button && button.dataset.armed !== '1') {
    button.dataset.armed = '1';
    button.textContent = 'Clear it?';
    button.classList.add('danger');
    setTimeout(() => {
      button.dataset.armed = '0';
      button.textContent = 'Start over';
      button.classList.remove('danger');
    }, 3500);
    return;
  }
  if (button) {
    button.dataset.armed = '0';
    button.textContent = 'Start over';
    button.classList.remove('danger');
  }
  $('#write-text').value = '';
  store.set(draftKey(writeState.pieceId), '');
  $('#write-text').dispatchEvent(new Event('input'));
  $('#write-annotated').innerHTML = '';
  $('#write-suggestion').innerHTML = '';
  toast('Cleared — the versions you kept are still there');
}

/* -- writing your own lessons ---------------------------------------------- */

/* A passage the reader wrote, in whatever language they wrote it in, turned into
   a diglot lesson: translated into English if it needs to be, then woven with the
   same three dials an import gets. The alternative -- a separate, simpler flow for
   "your own text" -- would leave them with two kinds of lesson that behave
   differently, and the difference would only show up later. */

function openFile() {
  const button = $('#write-open');
  const existing = $('#write-text').value.trim();
  // Nothing to lose: go straight to the picker. With text in the box the first
  // click arms instead, the same two-click pattern as Start over, because the
  // file replaces what is there and there is no undo for that.
  if (existing && button && button.dataset.armed !== '1') {
    button.dataset.armed = '1';
    button.textContent = 'Replace your text?';
    button.classList.add('danger');
    setTimeout(() => {
      button.dataset.armed = '0';
      button.textContent = 'Open a file';
      button.classList.remove('danger');
    }, 3500);
    return;
  }
  if (button) {
    button.dataset.armed = '0';
    button.textContent = 'Open a file';
    button.classList.remove('danger');
  }
  $('#write-file').click();
}

/* Read locally, with the browser's own file reader: the text never leaves the
   machine, which is the only reason a button in the writing box can be trusted
   with a draft. Text formats only -- a PDF or a .docx is not text, and accepting
   one would mean shipping a parser and failing confusingly without it. */
async function readFileIn(input) {
  const file = input.files && input.files[0];
  input.value = '';
  if (!file) return;
  let text;
  try {
    text = await file.text();
  } catch (err) {
    toast(`That file could not be read: ${err.message}`, 'err');
    return;
  }
  if (!text.trim()) { toast('That file is empty', 'err'); return; }
  const box = $('#write-text');
  box.value = text.replace(/\r\n/g, '\n').trim();
  box.dispatchEvent(new Event('input'));
  box.focus();
  toast(`Read ${file.name}`);
}

async function openLesson() {
  const text = $('#write-text').value.trim();
  if (text.split(/\s+/).filter(Boolean).length < 40) {
    toast('Write a few sentences first — a lesson needs something to weave');
    return;
  }
  let options;
  try {
    options = await loadImportOptions();
  } catch (err) {
    toast(err.message, 'err');
    return;
  }
  openModal('Make a lesson from this', `
    <p class="small muted" style="margin-top:0">Your piece is put into English if it is not
    already, then woven into Spanish with the settings below. It joins your library like any
    other lesson — same reader, same notes, same cards — and this draft is kept as a version
    of the piece first, so the lesson refers back to something you can see.</p>
    <p class="small" id="lesson-note"></p>
    ${importControlsHtml(options)}
    <div class="row"><button class="btn primary" id="lesson-go">Weave it into Spanish</button>
      <span class="small muted">It runs in the background — you can close this.</span></div>
    <div id="import-job"></div>`);

  // "the article" is what the shared controls say by default; the piece in front
  // of the reader is not an article, it is theirs.
  wireImportControls(null, { subject: 'piece' });
  const note = await lessonNote(text);
  const slot = $('#lesson-note');
  if (slot) slot.textContent = note;

  $('#lesson-go').addEventListener('click', async () => {
    const button = $('#lesson-go');
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span> queueing';
    try {
      const response = await api('/api/writing/lesson', {
        method: 'POST',
        body: {
          text,
          writing_id: writeState.pieceId,
          prompt: (writeState.prompt && writeState.prompt.instruction) || '',
          words: (writeState.prompt && writeState.prompt.words) || [],
          level: importChoice.level,
          ratio: importChoice.ratio,
          weave: importChoice.weave,
        },
      });
      // The piece may have been started by this call, so adopt its id or the
      // next draft the reader keeps would start a second piece from the same text.
      if (response.writing_id && !writeState.pieceId) writeState.pieceId = response.writing_id;
      await loadWritings();
      $('#import-job').innerHTML = `<div data-job-detail="${response.job_id}" style="margin-top:16px"></div>`;
      const job = await api(`/api/jobs/${response.job_id}`);
      updateJob(job);
      button.textContent = 'Weaving — you can close this';
      button.classList.remove('primary');
    } catch (err) {
      toast(err.message, 'err');
      button.disabled = false;
      button.textContent = 'Weave it into Spanish';
    }
  });
}

/* What the lesson will be, said before it is made. The share of Spanish comes
   from the same measure the word count in the footer uses, rather than a second
   counter in the browser: two counts of the same thing drift, and this one is on
   screen next to that one. */
async function lessonNote(text) {
  const words = text.split(/\s+/).filter(Boolean).length;
  let share = 0;
  try {
    const data = await api('/api/writing/measure', {
      method: 'POST', body: { text, prompt: '', words: [] },
    });
    share = Math.round(((data.reading || {}).spanish_share || 0) * 100);
  } catch { /* the note is a nicety; the lesson still runs */ }
  if (!share) {
    return `About ${words} words of English. It will be woven into Spanish at the settings below.`;
  }
  return `About ${words} words, roughly ${share}% of them Spanish. What is not English is put `
    + 'into English first, so this becomes a diglot either way.';
}

function markStale() {
  const host = $('#write-stale-slot');
  if (!host) return;
  const shown = writeState.checked;
  // Its own slot rather than a header on the annotations: a draft with nothing
  // to flag has no annotations, and "you have edited since this was read" is
  // exactly the thing that still needs saying then.
  if (!shown) { host.innerHTML = ''; return; }
  const edited = $('#write-text').value.trim() !== String(shown.text || '').trim();
  host.innerHTML = edited
    ? '<div class="small muted write-stale">You have edited since this version was read — '
      + 'check again to see what changed.</div>'
    : '';
}

/* -- one revision on screen ------------------------------------------------ */

function applyRevision(revision, extra = {}) {
  writeState.checked = revision;
  writeState.marks = [];
  const reading = extra.reading || revision.reading || {};
  const verdict = extra.verdict || revision.verdict || {};
  // No tutor means no feedback at all, which is different from an empty one --
  // and every read below has to survive it.
  const feedback = (extra.feedback !== undefined ? extra.feedback : revision.feedback) || {};
  const note = extra.note || '';
  $('#write-count').innerHTML = [
    `<strong>${reading.words || 0}</strong> words`,
    `${reading.sentences || 0} sentences`,
    `<strong>${Math.round((reading.spanish_share || 0) * 100)}%</strong> Spanish`,
  ].join(' · ');
  $('#write-feedback').innerHTML = feedbackPanel(
    reading, verdict, feedback, note, revision.checked !== false);
  $('#write-annotated').innerHTML = annotatedPanel(revision.text, feedback);
  wireMarks();
  markStale();
  wireSaveWords(feedback, revision.text);
}

/* A correction can become a card, with the writer's own sentence as the context
   -- the thing they got wrong comes back in their words rather than in a
   model's example. */
function wireSaveWords(feedback, text) {
  $$('#write-feedback [data-card]').forEach(button => button.addEventListener('click', async () => {
    const item = ((feedback || {}).corrections || [])[Number(button.dataset.card)] || {};
    if (!item.should_be) return;
    button.disabled = true;
    try {
      const saved = await api('/api/word/save', { method: 'POST', body: {
        term: item.should_be, note: item.why || null,
        context: item.their_text || String(text || '').slice(0, 200),
      }});
      button.textContent = '✓ Saved';
      toast(`Saved “${item.should_be}” · ${saved.total} words`);
      refreshDuePill();
    } catch (err) { button.disabled = false; toast(err.message, 'err'); }
  }));
}

function feedbackPanel(reading, verdict, feedback, note, checked) {
  if (!checked) {
    return `<h4>This draft</h4>
      <p class="small muted">Kept without being read. Check it to get notes and a score.</p>
      <div class="write-facts">${facts(reading, null)}</div>`;
  }
  const labels = { well_formed: 'holds together', range: 'varied', did_the_task: 'did the prompt' };
  const meters = Object.entries(verdict.checks || {}).map(([key, value]) => `
    <div class="meter">
      <span>${escapeHtml(labels[key] || key)}</span>
      <span class="track"><i style="width:${Math.round(value * 100)}%;
        background:${value >= .6 ? 'var(--good)' : value >= .35 ? 'var(--warn)' : 'var(--bad)'}"></i></span>
      <span class="val">${Math.round(value * 100)}%</span>
    </div>`).join('');
  const loads = (feedback.corrections || []).map((item, index) => `
    <li><span class="was">${escapeHtml(item.their_text || '')}</span> →
        <span class="now">${escapeHtml(item.should_be || '')}</span>
        <span class="why">${escapeHtml(item.why || '')}</span>
        ${item.should_be ? `<button class="btn ghost sm" data-card="${index}"
           title="Add this to your vocabulary deck, with your own sentence as the context">Save word</button>` : ''}</li>`).join('');

  return `<h4>This draft</h4>
    <div class="write-facts">${facts(reading, verdict)}</div>
    ${note ? `<p class="headline small">${escapeHtml(note)}</p>` : ''}
    ${verdict.error ? `<p class="small muted">${escapeHtml(verdict.error)}</p>` : ''}
    ${meters ? `<div class="meters">${meters}</div>` : ''}
    ${feedback.failed
      ? `<p class="small muted">${escapeHtml(feedback.summary || '')}</p>`
      : (feedback.summary ? `<p>${escapeHtml(feedback.summary)}</p>` : '')}
    ${loads ? `<ul class="corrections">${loads}</ul>` : ''}
    ${feedback.praise ? `<p class="small">👍 ${escapeHtml(feedback.praise)}</p>` : ''}
    ${feedback.next ? `<p class="small muted">Next time: ${escapeHtml(feedback.next)}</p>` : ''}
    ${verdict.model ? `<p class="small muted">read by ${escapeHtml(verdict.model)}${
      verdict.latency_ms ? ` in ${verdict.latency_ms} ms` : ''}</p>` : ''}`;
}

function facts(reading, verdict) {
  return [
    `<span><strong>${reading.words || 0}</strong> words</span>`,
    `<span><strong>${Math.round((reading.spanish_share || 0) * 100)}%</strong> Spanish</span>`,
    `<span><strong>${reading.distinct || 0}</strong> different words</span>`,
    verdict && verdict.score != null
      ? `<span>overall <strong>${Number(verdict.score).toFixed(1)}</strong> / ${verdict.score_max || 4}</span>` : '',
  ].filter(Boolean).join('');
}

/* The draft with the tutor's notes placed on it.

   A note is only placed if its fragment is really in the text -- the server
   drops the rest -- and each note takes the first occurrence nothing else has
   claimed, so two notes can never overlap and produce a highlight that means two
   things at once. */
function annotatedPanel(text, feedback) {
  writeState.marks = [];
  const notes = (feedback || {}).notes || [];
  if (!notes.length) return '';
  const claimed = [];
  const placed = [];
  notes.forEach(note => {
    const at = freeIndex(text, note.fragment, claimed);
    if (at < 0) return;
    claimed.push([at, at + note.fragment.length]);
    placed.push({ ...note, start: at, end: at + note.fragment.length, index: placed.length });
  });
  if (!placed.length) return '';
  placed.sort((a, b) => a.start - b.start);

  let html = '';
  let cursor = 0;
  for (const mark of placed) {
    html += escapeHtml(text.slice(cursor, mark.start));
    html += `<mark class="w-note ${escapeHtml(mark.kind)}" data-note="${mark.index}"
      title="Click for the note">${escapeHtml(text.slice(mark.start, mark.end))}</mark>`;
    cursor = mark.end;
  }
  html += escapeHtml(text.slice(cursor));
  writeState.marks = placed;

  const good = placed.filter(m => m.kind === 'good').length;
  const bad = placed.length - good;
  return `<div class="write-annotated">
    <div class="small muted">What the tutor saw — ${bad} to look at${
      good ? `, ${good} worth keeping` : ''}. Click any mark.</div>
    <p class="annotated-text">${html}</p>
  </div>`;
}

function freeIndex(text, fragment, claimed) {
  if (!fragment) return -1;
  let from = 0;
  for (;;) {
    const at = text.indexOf(fragment, from);
    if (at < 0) return -1;
    const end = at + fragment.length;
    if (!claimed.some(([start, stop]) => at < stop && end > start)) return at;
    from = at + 1;
  }
}

function wireMarks() {
  $$('#write-annotated .w-note').forEach(mark => mark.addEventListener('click', (event) => {
    event.stopPropagation();
    const note = writeState.marks[Number(mark.dataset.note)];
    if (!note) return;
    const kind = note.kind === 'good' ? 'done well'
      : note.kind === 'style' ? 'would read better as' : (note.issue || 'needs changing');
    showLookupPanel(note.fragment, mark);
    setLookupPanel(note.fragment, kind,
      `${note.why ? `<div>${escapeHtml(note.why)}</div>` : ''}
       ${note.suggestion ? `<div class="gloss-big">${escapeHtml(note.suggestion)}</div>` : ''}`);
  }));
}

/* -- improving, without replacing ------------------------------------------ */

async function improveDraft(mode, button) {
  const text = $('#write-text').value.trim();
  if (!text) { toast('Write something first'); return; }
  const host = $('#write-suggestion');
  if (button) button.disabled = true;
  host.innerHTML = '<div class="loading"><span class="spinner"></span> working on a version</div>';
  try {
    const data = await api('/api/writing/improve', { method: 'POST', body: {
      text, mode, prompt: (writeState.prompt || {}).instruction || '',
      words: (writeState.prompt || {}).words || [],
    }});
    const suggestion = data.suggestion;
    if (!suggestion || suggestion.failed || !suggestion.revision) {
      host.innerHTML = `<p class="small muted">${escapeHtml(
        (suggestion && (suggestion.summary || suggestion.changed)) || 'No suggestion came back.')}</p>`;
    } else {
      host.innerHTML = `<div class="suggestion">
        <div class="small muted">${escapeHtml(suggestion.label || '')}${
          suggestion.changed ? ` — ${escapeHtml(suggestion.changed)}` : ''}</div>
        <p class="annotated-text">${escapeHtml(suggestion.revision)}</p>
        <div class="row">
          <button class="btn primary sm" id="write-use">Use as my next draft</button>
          <button class="btn ghost sm" id="write-drop">Discard</button>
        </div>
        <p class="small muted">Your own text is untouched. Taking it puts it in the box to edit.</p>
      </div>`;
      $('#write-use').addEventListener('click', () => {
        $('#write-text').value = suggestion.revision;
        $('#write-text').dispatchEvent(new Event('input'));
        $('#write-text').focus();
        host.innerHTML = '<p class="small muted">In the box. Edit it, then check it.</p>';
      });
      $('#write-drop').addEventListener('click', () => { host.innerHTML = ''; });
    }
  } catch (err) {
    host.innerHTML = `<p class="small" style="color:var(--bad)">${escapeHtml(err.message)}</p>`;
  } finally {
    if (button) button.disabled = false;
  }
}

/* -- versions and history -------------------------------------------------- */

function renderVersions(piece) {
  const host = $('#write-versions');
  if (!host) return;
  const revisions = (piece.revisions || []).slice().reverse();
  if (!revisions.length) {
    host.innerHTML = '<h4>Versions</h4><p class="small muted">Nothing kept yet. Checking a draft keeps it here.</p>';
    return;
  }
  host.innerHTML = `<h4>Versions <span class="muted">${revisions.length}</span></h4>
    <div class="versions">${revisions.map(revision => {
      const reading = revision.reading || {};
      const verdict = revision.verdict || {};
      const current = writeState.checked && writeState.checked.id === revision.id;
      return `<button class="version ${current ? 'current' : ''}" data-revision="${revision.id}">
        <span class="v-label">${escapeHtml(revision.label || 'Draft')}</span>
        <span class="v-meta">${(revision.created_at || '').slice(0, 10)} · ${reading.words || 0} words · ${
          Math.round((reading.spanish_share || 0) * 100)}% Spanish${
          revision.checked && verdict.score != null ? ` · ${Number(verdict.score).toFixed(1)}/4` : ' · not read'}</span>
      </button>`;
    }).join('')}</div>`;
  $$('[data-revision]', host).forEach(button => button.addEventListener('click', () => {
    const revision = revisions.find(item => String(item.id) === button.dataset.revision);
    if (!revision) return;
    $('#write-text').value = revision.text;
    $('#write-text').dispatchEvent(new Event('input'));
    applyRevision(revision);
    toast(revision.label ? `${revision.label} in the box` : 'That version is in the box');
  }));
}

async function loadWritings() {
  const host = $('#write-history');
  if (!host) return;
  let data;
  try { data = await api('/api/writing'); } catch { return; }
  const totals = data.totals || {};
  $('#write-summary').textContent = totals.pieces
    ? `${totals.pieces} piece${totals.pieces === 1 ? '' : 's'}, ${totals.revisions} version${
        totals.revisions === 1 ? '' : 's'} · ${totals.words} words written, ${
        Math.round((totals.spanish_share || 0) * 100)}% of them Spanish.`
    : 'Nothing written yet. The prompt is drawn from what you have been reading and looking up.';
  if (!writeState.prompt && data.prompt) {
    writeState.prompt = data.prompt;
    paintPrompt();
  }
  if (!writeState.modes.length) { writeState.modes = data.modes || []; paintModes(); }

  const pieces = (data.writings || []).filter(piece => piece.id !== writeState.pieceId);
  // An empty bordered card reads as something that failed to load.
  host.hidden = !pieces.length;
  host.innerHTML = pieces.length
    ? `<h4>Earlier pieces</h4><div class="pieces">${pieces.map(piece => `
        <div class="piece" data-piece="${piece.id}">
          <button class="piece-open">
            <span class="p-title">${escapeHtml(piece.title || piece.excerpt || 'Untitled')}</span>
            <span class="p-meta">${(piece.updated_at || '').slice(0, 10)} · ${
              piece.revision_count} version${piece.revision_count === 1 ? '' : 's'} · ${
              piece.words} words${piece.verdict && piece.verdict.score != null
                ? ` · ${Number(piece.verdict.score).toFixed(1)}/4` : ''}</span>
          </button>
          <button class="btn ghost sm" data-drop="${piece.id}" title="Delete this piece and its versions">×</button>
        </div>`).join('')}</div>`
    : '';

  $$('[data-piece]', host).forEach(row => row.querySelector('.piece-open')
    .addEventListener('click', () => { location.hash = `#/write/${row.dataset.piece}`; }));
  $$('[data-drop]', host).forEach(button => {
    let armed = false;
    button.addEventListener('click', async () => {
      if (!armed) {
        armed = true;
        button.textContent = 'sure?';
        setTimeout(() => { armed = false; button.textContent = '×'; }, 3000);
        return;
      }
      try {
        await api(`/api/writing/${button.dataset.drop}`, { method: 'DELETE' });
        toast('Deleted that piece');
        await loadWritings();
      } catch (err) { toast(err.message, 'err'); }
    });
  });
}

/* ================================================================ views === */

/* -- library --------------------------------------------------------------- */

/* Which articles the library shows. Remembered, because someone working through
   a shelf wants that narrow view to survive a visit to the reader and back. */
const SHELF_FILTERS = [
  ['all', 'All'],
  ['unread', 'Unread'],
  ['reading', 'Started'],
  ['finished', 'Finished'],
];

let libraryFilter = store.get('libraryFilter', 'all');
let registerFilter = store.get('registerFilter', 'all');

const REGISTER_LABELS = {
  news: '📰 News', essay: '📚 Essay', conversation: '💬 Conversation',
  academic: '🎓 Academic', fiction: '📖 Fiction', unspecified: 'Other',
};

// The taxonomy's order, so the filter row does not reshuffle as the library
// grows -- a moving row has to be re-read every time.
const REGISTER_ORDER = ['news', 'essay', 'conversation', 'academic', 'fiction', 'unspecified'];

function articleMatchesFilter(article, filter) {
  const progress = article.progress || {};
  if (filter === 'unread') return !progress.completed && !(progress.position > 0.02);
  if (filter === 'reading') return !progress.completed && progress.position > 0.02;
  if (filter === 'finished') return !!progress.completed;
  return true;
}

function articleMatchesRegister(article, register) {
  return register === 'all' || (article.register || 'unspecified') === register;
}

async function viewLibrary() {
  setActiveTab('library');
  $('#reader-bar').hidden = true;
  document.body.classList.remove('dim-english', 'es-only');
  const view = $('#view');
  view.className = 'view';
  view.innerHTML = '<div class="empty"><span class="spinner"></span></div>';

  const data = await api('/api/library');
  if (!data.articles.length) {
    view.innerHTML = `<div class="empty"><h3>The shelves are empty</h3>
      <p>Put diglot <code>.md</code> files in the corpus folder, or import one from the web.</p></div>`;
    return;
  }

  const counts = {};
  const registerCounts = {};
  for (const [key] of SHELF_FILTERS) {
    counts[key] = data.articles.filter(a => articleMatchesFilter(a, key)).length;
  }
  for (const article of data.articles) {
    const code = article.register || 'unspecified';
    registerCounts[code] = (registerCounts[code] || 0) + 1;
  }
  // Only offer registers the library actually contains, in the taxonomy's
  // order rather than by count: a filter row that reshuffles as you read is a
  // filter row you have to re-read every time.
  const presentRegisters = REGISTER_ORDER.filter(code => registerCounts[code]);
  if (registerFilter !== 'all' && !registerCounts[registerFilter]) registerFilter = 'all';

  const shown = data.articles.filter(a => articleMatchesFilter(a, libraryFilter)
    && articleMatchesRegister(a, registerFilter));

  const shelves = new Map();
  for (const article of shown) {
    if (!shelves.has(article.shelf)) shelves.set(article.shelf, []);
    shelves.get(article.shelf).push(article);
  }

  const readCount = counts.finished;
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const parts = [`<div class="page-head">
      <h1>Your library</h1>
      <p>${plural(data.articles.length, 'article', 'articles')} ·
      ${readCount} finished · ${plural(data.words, 'word', 'words')} saved.
      Shelves run from mostly-English to Spanish-dominant, so you can pick the next thing you can actually read.</p>
      <div class="row" style="margin-top:10px">
        <button class="btn ghost sm" id="btn-open-share"
                title="A diglot .md file someone sent you">Open a shared lesson</button>
      </div>
    </div>`];

  parts.push(`<div class="row filter-row" role="group" aria-label="Filter articles">
    ${SHELF_FILTERS.map(([key, label]) => `
      <button class="choice sm ${key === libraryFilter ? 'active' : ''}" data-filter="${key}">
        ${label} <span class="muted">${counts[key]}</span>
      </button>`).join('')}
    <span class="spacer"></span>
    ${counts.reading ? `<span class="small muted">${counts.reading} part-read</span>` : ''}
  </div>`);

  if (presentRegisters.length > 1) {
    parts.push(`<div class="row filter-row" role="group" aria-label="Filter by register">
      <button class="choice sm ${registerFilter === 'all' ? 'active' : ''}" data-register="all">
        Every register</button>
      ${presentRegisters.map(code => `
        <button class="choice sm ${code === registerFilter ? 'active' : ''}" data-register="${code}"
                title="${escapeHtml((data.articles.find(a => (a.register || 'unspecified') === code) || {}).register_blurb || '')}">
          ${REGISTER_LABELS[code] || code} <span class="muted">${registerCounts[code]}</span>
        </button>`).join('')}
    </div>`);
  }

  if ((data.worth_saving || []).length && libraryFilter === 'all') {
    parts.push(`<div class="panel worth">
      <h3>Words you keep looking up</h3>
      <p class="panel-sub">You have looked these up more than once and not saved them yet —
        which usually means they have not stuck.</p>
      <div class="row">
        ${data.worth_saving.map(word => `<button class="btn sm" data-worth="${escapeHtml(word.lemma)}"
            data-gloss="${escapeHtml(word.gloss || '')}" data-term="${escapeHtml(word.term)}"
            data-context="${escapeHtml(word.context || '')}" data-slug="${escapeHtml(word.article_slug || '')}"
            title="Looked up ${word.times} times">
          ${escapeHtml(word.term)} <span class="muted">${word.times}×</span></button>`).join('')}
      </div>
    </div>`);
  }

  if (!shown.length) {
    parts.push(`<div class="empty"><h3>Nothing ${libraryFilter === 'finished' ? 'finished' : 'unread'}</h3>
      <p>${libraryFilter === 'finished'
        ? 'Articles you mark as finished will collect here.'
        : 'Every article has been opened at least once. Try another filter.'}</p></div>`);
  }

  for (const [shelf, articles] of shelves) {
    const description = articles[0].shelf_description || '';
    parts.push(`<section class="shelf">
      <div class="shelf-head"><h2>${escapeHtml(shelf)}</h2>
        <span class="count">${articles.length}</span></div>
      <p class="shelf-desc">${escapeHtml(description)}</p>
      <div class="grid">${articles.map(articleCard).join('')}</div>
    </section>`);
  }
  view.innerHTML = parts.join('');

  const openShare = $('#btn-open-share', view);
  if (openShare) openShare.addEventListener('click', openSharedLesson);

  $$('[data-filter]', view).forEach(button => button.addEventListener('click', () => {
    libraryFilter = button.dataset.filter;
    store.set('libraryFilter', libraryFilter);
    viewLibrary();
  }));
  $$('[data-register]', view).forEach(button => button.addEventListener('click', () => {
    registerFilter = button.dataset.register;
    store.set('registerFilter', registerFilter);
    viewLibrary();
  }));
  $$('.card', view).forEach(card => card.addEventListener('click', () => { location.hash = `#/read/${card.dataset.slug}`; }));
  $$('[data-remove-lesson]', view).forEach(button => button.addEventListener('click', async (event) => {
    event.stopPropagation();
    // Two clicks rather than a dialog: the file it deletes cannot be got back, and
    // the second click is the confirmation. Same pattern as Start over and the deck.
    if (button.dataset.armed !== '1') {
      button.dataset.armed = '1';
      button.classList.add('danger');
      button.textContent = 'Remove it?';
      setTimeout(() => {
        if (button.dataset.armed === '1') {
          button.dataset.armed = '0';
          button.classList.remove('danger');
          button.textContent = 'Remove';
        }
      }, 3500);
      return;
    }
    button.disabled = true;
    try {
      const result = await api(`/api/article/${encodeURIComponent(button.dataset.removeLesson)}`,
                               { method: 'DELETE' });
      toast(`Removed · your ${result.words_kept} saved words stay in the deck`);
      viewLibrary();
    } catch (err) {
      button.disabled = false;
      button.dataset.armed = '0';
      button.classList.remove('danger');
      button.textContent = 'Remove';
      toast(err.message, 'err');
    }
  }));
  $$('[data-worth]', view).forEach(button => button.addEventListener('click', async (event) => {
    event.stopPropagation();
    try {
      const result = await api('/api/word/save', {
        method: 'POST',
        body: {
          term: button.dataset.term, lemma: button.dataset.worth, gloss: button.dataset.gloss || null,
          article_slug: button.dataset.slug || null, context: button.dataset.context || null,
        },
      });
      button.disabled = true;
      button.innerHTML = `✓ ${escapeHtml(button.dataset.term)}`;
      toast(`Saved · ${result.total} words`);
      refreshDuePill();
    } catch (err) { toast(err.message, 'err'); }
  }));
}

function articleCard(article) {
  const coverage = article.coverage || {};
  const text = coverage.text || {};
  const known = Math.round((text.token_ratio || 0) * 100);
  const ratio = Math.round(article.stats.spanish_ratio * 100);
  const progress = article.progress;
  const band = coverage.band || 'immersion';
  const hasDeck = (text.types || 0) > 0 && known > 0;
  return `<article class="card" data-slug="${escapeHtml(article.slug)}">
    <h3>${escapeHtml(article.title)}</h3>
    ${article.author ? `<div class="byline">${escapeHtml(article.author)}</div>` : ''}
    ${article.preview ? `<p class="preview">${escapeHtml(article.preview)}</p>` : ''}
    <div class="meta">
      <span class="chip">${ratio}% Spanish</span>
      <span class="chip">${article.stats.words} words</span>
      ${article.level ? `<span class="chip" title="Estimated CEFR level of the Spanish">${escapeHtml(article.level)}</span>` : ''}
      ${article.register && article.register !== 'unspecified'
        ? `<span class="chip register" title="${escapeHtml(article.register_blurb || '')}">
             ${REGISTER_LABELS[article.register] || article.register}</span>` : ''}
      ${hasDeck ? `<span class="chip accent" title="${escapeHtml(coverage.advice || '')}">
        ${known}% of Spanish known</span>` : ''}
      ${article.imported ? '<span class="chip imported">imported</span>' : ''}
      ${progress.completed ? '<span class="complete-tick">✓ read</span>' : ''}
    </div>
    ${hasDeck ? `<div class="bar thin band-${band}"
        title="${known}% of the Spanish words in this article are already in your deck">
      <i style="width:${Math.min(known, 100)}%"></i></div>` : ''}
    ${!progress.completed && progress.position > 0.02
      ? `<div class="bar thin" title="Where you left off"><i style="width:${Math.round(progress.position * 100)}%;background:var(--focus-line)"></i></div>` : ''}
    ${article.imported ? `<div class="card-foot">
      <button class="btn ghost xs danger-hover" data-remove-lesson="${escapeHtml(article.slug)}"
              title="Delete this lesson's file and its reading progress. Words you saved from it stay in your deck.">Remove</button>
    </div>` : ''}
  </article>`;
}

/* -- reader ---------------------------------------------------------------- */

let readerState = null;

async function viewReader(slug) {
  setActiveTab('library');
  const view = $('#view');
  view.className = 'view';
  view.innerHTML = '<div class="empty"><span class="spinner"></span></div>';

  let article;
  try {
    article = await api(`/api/article/${encodeURIComponent(slug)}`);
  } catch (err) {
    view.innerHTML = `<div class="empty"><h3>Could not open that article</h3><p>${escapeHtml(err.message)}</p></div>`;
    return;
  }

  readerState = { article, startedAt: Date.now(), saved: new Set(article.known || []) };
  // Load the local glossary in the background so the first word click is
  // already answerable without a round trip. Deliberately not awaited.
  ensureGlossary();
  $('#reader-bar').hidden = false;
  document.body.classList.remove('dim-english', 'es-only');
  applyPrefs();

  const blocks = article.blocks.map(renderBlock).join('');
  const rail = renderRail(article);
  const coverage = article.coverage || {};
  const text = coverage.text || {};
  const knownPct = Math.round((text.token_ratio || 0) * 100);

  view.innerHTML = `
    <div class="reader-wrap ${rail ? 'has-rail' : ''}">
      <div class="article">
        <div class="article-head">
          <h1>${escapeHtml(article.title)}</h1>
          ${article.author ? `<div class="byline">${escapeHtml(article.author)}</div>` : ''}
          ${article.url ? `<div class="source"><a href="${escapeHtml(article.url)}" target="_blank" rel="noopener">${escapeHtml(hostOf(article.url))} ↗</a>
            · ${article.stats.words} words · ${Math.round(article.stats.spanish_ratio * 100)}% Spanish</div>` : ''}
          <div class="register-note" id="register-note"></div>
          ${text.types && knownPct > 0 ? `<div class="coverage-line band-${escapeHtml(coverage.band || '')}">
            <strong>${knownPct}%</strong> of the Spanish words here are already in your deck —
            ${escapeHtml(coverage.advice || '')}
            ${(text.unknown || []).length ? `<div class="unknown-words">New to you:
              ${text.unknown.slice(0, 10).map(word => `<span class="chip" data-look="${escapeHtml(word)}">${escapeHtml(word)}</span>`).join('')}
            </div>` : ''}
          </div>` : ''}
        </div>
        <div class="prose" id="prose">${blocks}</div>

        <div class="article-foot">
          <div class="row">
            <button class="btn primary" id="btn-quiz">Comprehension questions</button>
            <button class="btn" id="btn-drills">Translate into Spanish</button>
            <span class="spacer"></span>
            <button class="btn" id="btn-done">${article.progress.completed_at ? '✓ Finished' : 'Mark as finished'}</button>
          </div>
          <div class="share-row">
            <input id="share-name" class="share-name" placeholder="your name (optional)"
                   aria-label="Your name, recorded in the file you share"
                   value="${escapeHtml(store.get('shareName', ''))}">
            <button class="btn ghost sm" id="btn-share"
                    title="Download this lesson as a file to send to someone">Share this lesson</button>
          </div>
          <div id="exercises" style="margin-top:20px"></div>
        </div>
      </div>
      ${rail}
    </div>`;

  wireReader(article);
  paintRegister(article.slug, article);
  restoreScroll(article.progress.position);
  updateProgressBar();
}

/* What kind of writing this is, and who decided.

   The distinction is the whole point of showing it: a register the author declared
   in the file is a fact about the passage, and one the app inferred from how the
   text is written is a guess. A reader who knows a piece is an essay when the app
   read it as academic should be able to say so -- and saying so writes the line
   into the passage's own file, so the file stops being wrong rather than the app
   holding a private correction. */
function paintRegister(slug, article) {
  const host = $('#register-note');
  if (!host) return;
  const code = article.register || 'unspecified';
  const known = code !== 'unspecified';
  // A passage inside a compilation cannot be tagged from here: one front-matter
  // block sits between several passages, so a single line would not say which one
  // the reader meant. Saying that is better than a button that refuses.
  const editable = article.register_editable !== false;
  host.innerHTML = `
    <span class="register-name">${known
      ? escapeHtml(REGISTER_LABELS[code] || code) : 'No register'}</span>
    <span class="register-blurb">${escapeHtml(article.register_blurb
      || (known ? '' : 'nothing in the text said what kind of writing this is'))}</span>
    ${known && article.register_inferred
      ? '<span class="register-inferred" title="Guessed from how the text is written; not declared by the author">inferred</span>'
      : ''}
    ${editable
      ? `<button class="btn ghost sm" data-set-register="${escapeHtml(slug)}">${known ? 'Change' : 'Set it'}</button>`
      : '<span class="small muted" title="This file holds several passages, and the app cannot tell which one a tag in it would belong to">set in the file — this one holds several passages</span>'}`;
  const button = $('[data-set-register]', host);
  if (button) button.addEventListener('click', () => openRegisterPicker(slug, known ? code : ''));
}

let registerOptions = null;

async function openRegisterPicker(slug, current) {
  try {
    if (!registerOptions) registerOptions = (await api('/api/registers')).registers;
  } catch (err) {
    toast(err.message, 'err');
    return;
  }
  openModal('What kind of writing is this?', `
    <p class="small muted" style="margin-top:0">Some passages declare their own kind in the file they
    came from; the rest are guessed from how they are written, and the guess is marked <em>inferred</em>.
    Whatever you choose here is written into the passage's file, so it stays right if the file is moved
    or shared — and a passage you wrote yourself keeps its every other word.</p>
    <div class="register-choices">
      ${registerOptions.map(option => `
        <button type="button" class="choice ${option.code === current ? 'active' : ''}"
                data-register-pick="${escapeHtml(option.code)}">
          <span class="choice-name">${escapeHtml(REGISTER_LABELS[option.code] || option.name)}</span>
          <span class="small muted">${escapeHtml(option.blurb)}</span>
        </button>`).join('')}
      <button type="button" class="choice ${current ? '' : 'active'}" data-register-pick="">
        <span class="choice-name">Let the app guess</span>
        <span class="small muted">Takes the tag out of the file; the app infers one and says that it did</span>
      </button>
    </div>`);

  $$('[data-register-pick]').forEach(button => button.addEventListener('click', async () => {
    try {
      const fresh = await api(`/api/article/${encodeURIComponent(slug)}/register`, {
        method: 'POST', body: { code: button.dataset.registerPick },
      });
      closeModal();
      toast(fresh.register
        ? `Marked as ${REGISTER_LABELS[fresh.register] || fresh.register}`
        : 'Tag removed — the app will guess');
      // Painted in place rather than by re-rendering the reader: a re-render
      // drops the reader back to their last saved position, which for a change
      // this small is a page of lost reading.
      paintRegister(slug, { register: fresh.register || 'unspecified',
                            register_blurb: fresh.blurb, register_inferred: fresh.inferred,
                            register_editable: true });
    } catch (err) {
      toast(err.message, 'err');
    }
  }));
}

function hostOf(url) { try { return new URL(url).host.replace(/^www\./, ''); } catch { return url; } }

/* Paragraphs are rendered as *sentences*, not as a flat run of language spans.
 *
 * Two reasons, and the second is the one that matters. A sentence is the unit
 * the read-along should speak, and it is also the unit a language actually
 * breaks at: "Afortunadamente, los anales de la historia del arte **pintan** un
 * panorama diferente" is one Spanish sentence, but the bold span splits it into
 * three spans, so playing span by span produced four utterances for one
 * sentence -- one of them the single word "pintan".
 *
 * Wrapping each sentence lets playback merge the adjacent same-language runs
 * back together, and gives the highlight something exact to cover.
 */
const SENTENCE_END = /[.!?…]["'”’)]?\s*$/;

function splitIntoRuns(spans) {
  const runs = [];
  for (const span of spans) {
    const pieces = String(span.text).split(/(?<=[.!?…])\s+/).filter(piece => piece.length);
    pieces.forEach((piece, index) => {
      runs.push({ span, text: piece, endsSpan: index === pieces.length - 1 });
    });
  }
  return runs;
}

function groupSentences(runs) {
  const sentences = [];
  let current = [];
  for (const run of runs) {
    current.push(run);
    if (SENTENCE_END.test(run.text)) { sentences.push(current); current = []; }
  }
  if (current.length) sentences.push(current);
  return sentences.length ? sentences : [runs];
}

function spanishWords(text) {
  return String(text).split(/(\s+)/).map(part => {
    if (!part.trim()) return part;
    const clean = part.replace(/[̀-ͯ]/g, '');
    return `<span class="w" data-word="${escapeHtml(clean)}">${escapeHtml(part)}</span>`;
  }).join('');
}

function renderSentence(runs) {
  const html = runs.map(run => {
    const span = run.span;
    if (span.lang !== 'es') return `<span class="en">${escapeHtml(run.text)}</span>`;
    const classes = `es${span.target ? ' target' : ''}${span.bold ? ' bold' : ''}`;
    const gloss = run.endsSpan && span.gloss
      ? `<span class="gloss">${escapeHtml(span.gloss)}</span>` : '';
    return `<span class="${classes}">${spanishWords(run.text)}</span>${gloss}`;
  }).join('');
  return `<span class="sent">${html}</span>`;
}

function renderParagraph(block) {
  const sentences = groupSentences(splitIntoRuns(block.spans));
  return sentences.map(renderSentence).join('');
}

function renderBlock(block, index) {
  // ``data-block`` is what a kept sentence points back at: the reader selects a
  // sentence, the browser says which paragraph it was in, and the server finds
  // it again by index rather than by matching text against a whole article.
  if (block.kind === 'h') {
    const tag = block.level <= 2 ? 'h2' : 'h3';
    // The id is what the contents pane scrolls to. Keyed by block index rather
    // than by the heading text, because two sections can share a title.
    return `<${tag} id="sec-${index}" data-block="${index}">${escapeHtml(block.spans.map(s => s.text).join(''))}</${tag}>`;
  }
  if (block.kind === 'byline') return `<p class="byline-line" data-block="${index}">${escapeHtml(block.spans.map(s => s.text).join(''))}</p>`;
  if (block.kind === 'date') return `<p class="byline-line" data-block="${index}">${escapeHtml(block.spans.map(s => s.text).join(''))}</p>`;
  if (block.kind === 'ref') return `<p class="ref" data-block="${index}">${escapeHtml(block.spans.map(s => s.text).join(''))}</p>`;
  return `<p data-block="${index}">${renderParagraph(block)}</p>`;
}

/* The article's own headings, as a contents list.
 *
 * A generated lesson often arrives with real section headings, and a long
 * imported article without a contents pane is a page you scroll through rather
 * than navigate. Returns nothing for a short piece with no headings, and the
 * rail section is skipped rather than shown empty.
 *
 * A heading that occurs **twice in the same article** is left out. A section title
 * does not repeat within one piece, so a repeat is the page's furniture that the
 * extractor swept up more than once -- a "related links" box, a sidebar, the
 * "THE BASICS" panel Psychology Today prints beside the article and again inside
 * it. Listing those made a four-section article look like a fourteen-section one,
 * which is worse than a shorter list: it claims the article has sections it does
 * not have. The body still shows them; this only stops them being navigation. */
function articleSections(article) {
  const seen = new Map();
  const collected = [];
  article.blocks.forEach((block, index) => {
    if (block.kind !== 'h') return;
    const text = block.spans.map(s => s.text).join('').trim();
    if (!text) return;
    seen.set(text, (seen.get(text) || 0) + 1);
    collected.push({ index, text, level: Math.min(block.level || 2, 4) });
  });
  return collected.filter(section => seen.get(section.text) === 1);
}

function renderToc(article) {
  const sections = articleSections(article);
  if (sections.length < 2) return '';
  return `<section class="toc-section"><h4>Contents</h4>
    <nav class="toc" id="toc">
      ${sections.map(section => `
        <a href="#/read/${encodeURIComponent(article.slug)}"
           data-goto="${section.index}" class="toc-link lvl-${section.level}"
           title="${escapeHtml(section.text)}">${escapeHtml(section.text)}</a>`).join('')}
    </nav></section>`;
}

function wireToc(article) {
  const links = $$('#toc .toc-link');
  if (!links.length) return;

  links.forEach(link => link.addEventListener('click', (event) => {
    event.preventDefault();
    const target = document.getElementById(`sec-${link.dataset.goto}`);
    if (!target) return;
    stopReading();
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }));

  // Which section you are in, updated from the reader's existing scroll
  // handler rather than a second observer: the pane is only useful if it tells
  // you where you are, not just where you could go.
  readerState.sections = articleSections(article);
  readerState.tocLinks = links;
  markCurrentSection();
}

function markCurrentSection() {
  const sections = readerState && readerState.sections;
  const links = readerState && readerState.tocLinks;
  if (!sections || !sections.length || !links) return;
  let current = -1;
  for (const section of sections) {
    const node = document.getElementById(`sec-${section.index}`);
    if (node && node.getBoundingClientRect().top <= 140) current = section.index;
  }
  links.forEach(link => link.classList.toggle('current', Number(link.dataset.goto) === current));
}

function renderSpan(span) {
  if (span.lang !== 'es') return `<span class="en">${escapeHtml(span.text)}</span>`;
  const words = span.text.split(/(\s+)/).map(part => {
    if (!part.trim()) return part;
    const clean = part.replace(/[.,;:!?¡¿()"“”'’—–]/g, '');
    return `<span class="w" data-word="${escapeHtml(clean)}">${escapeHtml(part)}</span>`;
  }).join('');
  const gloss = span.gloss ? `<span class="gloss">${escapeHtml(span.gloss)}</span>` : '';
  return `<span class="es${span.target ? ' target' : ''}">${words}</span>${gloss}`;
}

function renderRail(article) {
  const vocab = (article.vocab || []);
  const focus = article.focus || [];
  const grammar = article.grammar || [];
  const toc = renderToc(article);
  if (!vocab.length && !grammar.length && !focus.length && !toc) return '';
  return `<aside class="rail">
    ${toc}
    ${focus.length ? `<section><h4>Focus vocabulary</h4>${focus.map(f => `
      <div class="vocab-item">
        <div><div class="t">${escapeHtml(f.es)}</div><div class="g">${escapeHtml(f.en || '')}</div></div>
        <button class="icon-btn add" title="Save to your deck" data-save-term="${escapeHtml(f.es)}" data-save-gloss="${escapeHtml(f.en || '')}">＋</button>
      </div>`).join('')}</section>` : ''}
    ${vocab.length ? `<section><h4>Recycled vocabulary</h4>${vocab.map(v => `
      <div class="vocab-item">
        <div><div class="t">${escapeHtml(v.term)}</div><div class="g">${escapeHtml(v.gloss || '')}</div>
        ${(v.examples || []).length ? `<div class="g" style="margin-top:4px">${mdInline(v.examples[0])}</div>` : ''}</div>
      </div>`).join('')}</section>` : ''}
    ${grammar.length ? `<section><h4>Grammar</h4>${grammar.map(g => `
      <div class="grammar-note"><strong>${mdInline(g.title)}</strong>
        ${g.example ? `<p style="font-family:var(--serif);color:var(--es-ink)">${mdInline(g.example)}</p>` : ''}
        ${g.explanation ? `<p>${mdInline(g.explanation)}</p>` : ''}
      </div>`).join('')}</section>` : ''}
  </aside>`;
}

function wireReader(article) {
  const slug = article.slug;

  // word clicks
  $('#prose').addEventListener('click', (event) => {
    const wordEl = event.target.closest('.w');
    if (!wordEl) return;
    const word = wordEl.dataset.word;
    if (!word) return;
    stopReading();                       // looking a word up means you stopped listening
    const sentence = sentenceAround(wordEl);
    lookupWord(word, sentence, slug, wordEl, readerState.saved);
  });

  // rail save buttons
  $$('.rail [data-save-term]').forEach(button => button.addEventListener('click', async (event) => {
    event.stopPropagation();
    try {
      const result = await api('/api/word/save', {
        method: 'POST',
        body: {
          term: button.dataset.saveTerm,
          gloss: button.dataset.saveGloss || null,
          article_slug: slug,
        },
      });
      button.textContent = '✓';
      button.disabled = true;
      toast(`Saved “${button.dataset.saveTerm}” · ${result.total} words`);
      refreshDuePill();
    } catch (err) { toast(err.message, 'err'); }
  }));

  // "New to you" chips in the coverage line open the same lookup as a word click
  $$('[data-look]', $('#view')).forEach(chip => chip.addEventListener('click', () => {
    lookupWord(chip.dataset.look, '', slug, chip, readerState.saved);
  }));

  wireSelectionBar(slug);
  wireToc(article);

  $('#btn-quiz').addEventListener('click', () => loadExercises(slug, 'quiz'));
  $('#btn-drills').addEventListener('click', () => loadExercises(slug, 'drills'));
  $('#btn-done').addEventListener('click', async () => {
    await api(`/api/article/${slug}/progress`, { method: 'POST', body: { completed: true, position: 1 } });
    $('#btn-done').textContent = '✓ Finished';
    toast('Marked as finished');
  });
  $('#btn-share').addEventListener('click', () => downloadShare(slug));

  // progress tracking
  let lastSaved = 0;
  const onScroll = () => {
    updateProgressBar();
    markCurrentSection();
    const now = Date.now();
    if (now - lastSaved > 6000) { lastSaved = now; saveProgress(slug, false); }
  };
  window.removeEventListener('scroll', window.__diglotScroll || (() => {}));
  window.__diglotScroll = onScroll;
  window.addEventListener('scroll', onScroll, { passive: true });
}

function sentenceAround(node) {
  // Prefer the rendered sentence wrapper: it is the actual sentence, where the
  // paragraph would give the model a whole paragraph of context for one word.
  const paragraph = node.closest('.sent') || node.closest('p, li, blockquote') || node.parentElement;
  if (!paragraph) return node.textContent;
  const text = paragraph.textContent;
  const offset = textOffsetWithin(paragraph, node);
  if (offset < 0) return text;
  const before = text.slice(0, offset);
  const after = text.slice(offset + node.textContent.length);
  const start = Math.max(before.lastIndexOf('. '), before.lastIndexOf('? '), before.lastIndexOf('! '), before.lastIndexOf('” ')) + 1;
  const endMatch = after.match(/[.!?]/);
  const end = endMatch ? offset + node.textContent.length + endMatch.index + 1 : text.length;
  return text.slice(start, end).trim();
}

function textOffsetWithin(root, node) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let offset = 0;
  let current;
  while ((current = walker.nextNode())) {
    if (node.contains(current)) return offset;
    offset += current.textContent.length;
  }
  return -1;
}

function updateProgressBar() {
  const prose = $('#prose');
  if (!prose) return;
  const rect = prose.getBoundingClientRect();
  const total = rect.height - window.innerHeight * 0.5;
  const seen = Math.min(Math.max(-rect.top + window.innerHeight * 0.4, 0), Math.max(total, 1));
  $('#reader-progress-fill').style.width = `${Math.round((seen / Math.max(total, 1)) * 100)}%`;
}

function restoreScroll(position) {
  if (!position || position < 0.02) return;
  requestAnimationFrame(() => {
    const prose = $('#prose');
    if (!prose) return;
    const target = prose.offsetTop + position * prose.offsetHeight - window.innerHeight * 0.3;
    window.scrollTo({ top: Math.max(target, 0), behavior: 'instant' in window ? 'instant' : 'auto' });
    toast('Resumed where you left off');
  });
}

async function saveProgress(slug, completed) {
  const prose = $('#prose');
  if (!prose) return;
  const rect = prose.getBoundingClientRect();
  const seen = Math.min(Math.max(-rect.top + window.innerHeight * 0.4, 0), prose.offsetHeight);
  const position = Math.min(seen / Math.max(prose.offsetHeight, 1), 1);
  const seconds = Math.round((Date.now() - readerState.startedAt) / 1000);
  if (seconds < 3 && !completed) return;
  readerState.startedAt = Date.now();
  // Reaching the end counts as finished. Requiring a button press for it would
  // leave the Finished shelf empty for anyone who simply read the article,
  // which is what the shelf is supposed to collect.
  const finished = completed || position >= 0.98;
  try {
    await api(`/api/article/${slug}/progress`, { method: 'POST', body: { position, seconds, completed: finished } });
  } catch { /* progress saving is best-effort */ }
}

/* -- exercises ------------------------------------------------------------- */

async function loadExercises(slug, kind) {
  const host = $('#exercises');
  host.innerHTML = `<div class="empty"><span class="spinner"></span>
    <div class="small muted" style="margin-top:10px">${kind === 'quiz'
      ? 'writing comprehension questions in Spanish' : 'building translation drills'} — this takes a moment the first time</div></div>`;

  let data;
  try {
    data = await api(`/api/article/${slug}/${kind}`);
  } catch (err) {
    host.innerHTML = `<div class="empty"><h3>Could not load exercises</h3><p>${escapeHtml(err.message)}</p></div>`;
    return;
  }

  if (kind === 'quiz') {
    if (!data.questions.length) { host.innerHTML = '<div class="empty">No questions were generated.</div>'; return; }
    host.innerHTML = gradingNote() + data.questions.map((q, i) => `
      <div class="exercise" data-kind="comprehension" data-index="${i}">
        <span class="focus-chip">Question ${i + 1} of ${data.questions.length}</span>
        <div class="q">${escapeHtml(q.question)}</div>
        ${q.hint ? `<div class="small muted" style="margin-bottom:9px">hint: ${escapeHtml(q.hint)}</div>` : ''}
        <textarea placeholder="Answer in Spanish — or in English if you are stuck"></textarea>
        <div class="row" style="margin-top:9px">
          <button class="btn primary sm" data-check>Check answer</button>
          <button class="btn ghost sm" data-reveal>Show a model answer</button>
        </div>
        <div class="verdict-slot"></div>
      </div>`).join('');
    $$('[data-check]', host).forEach(button => button.addEventListener('click', () => checkAnswer(button, 'comprehension', slug, data.questions)));
    $$('[data-reveal]', host).forEach(button => button.addEventListener('click', () => {
      const box = button.closest('.exercise');
      box.querySelector('.verdict-slot').innerHTML =
        `<div class="verdict"><div class="headline">Model answer</div>
         <div style="font-family:var(--serif);color:var(--es-ink);font-size:15px">${escapeHtml(data.questions[+box.dataset.index].expected)}</div></div>`;
    }));
  } else {
    if (!data.drills.length) { host.innerHTML = '<div class="empty">No drills were generated.</div>'; return; }
    host.innerHTML = gradingNote() + data.drills.map((d, i) => `
      <div class="exercise" data-kind="translate" data-index="${i}">
        <span class="focus-chip">${escapeHtml(d.focus || 'translation')}${d.vocabulary && d.vocabulary.length ? ` · ${d.vocabulary.map(escapeHtml).join(', ')}` : ''}</span>
        <div class="q">${escapeHtml(d.en)}</div>
        <textarea placeholder="Write it in Spanish"></textarea>
        <div class="row" style="margin-top:9px">
          <button class="btn primary sm" data-check>Check my Spanish</button>
          <button class="btn ghost sm" data-reveal>Show the article's version</button>
        </div>
        <div class="verdict-slot"></div>
      </div>`).join('');
    $$('[data-check]', host).forEach(button => button.addEventListener('click', () => checkAnswer(button, 'translate', slug, data.drills)));
    $$('[data-reveal]', host).forEach(button => button.addEventListener('click', () => {
      const box = button.closest('.exercise');
      box.querySelector('.verdict-slot').innerHTML =
        `<div class="verdict"><div class="headline">The article's Spanish</div>
         <div style="font-family:var(--serif);color:var(--es-ink);font-size:15px">${escapeHtml(data.drills[+box.dataset.index].es)}</div></div>`;
    }));
  }
  host.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function checkAnswer(button, kind, slug, items) {
  const box = button.closest('.exercise');
  const item = items[+box.dataset.index];
  const answer = box.querySelector('textarea').value.trim();
  if (!answer) { toast('Write something first'); return; }

  const slot = box.querySelector('.verdict-slot');
  button.disabled = true;
  slot.innerHTML = '<div class="verdict"><div class="loading"><span class="spinner"></span> judging your answer</div></div>';

  try {
    const result = await api('/api/attempt', {
      method: 'POST',
      body: kind === 'translate'
        ? { kind, answer, source: item.en, reference: item.es, question: item.en, slug }
        : { kind, answer, question: item.question, reference: item.expected, slug },
    });
    slot.innerHTML = renderVerdict(result, kind, item);
    refreshDuePill();
  } catch (err) {
    slot.innerHTML = `<div class="verdict wrong"><div class="headline">Could not grade that</div>
      <div class="small">${escapeHtml(err.message)}</div></div>`;
  } finally {
    button.disabled = false;
  }
}

function renderVerdict(result, kind, item) {
  const verdict = result.verdict || {};
  const checks = verdict.checks || {};
  const feedback = result.feedback || {};
  const correct = result.correct;
  const provisional = result.provisional === true;

  const labels = {
    meaning: 'meaning kept', grammar: 'grammar', natural: 'naturalness',
    fits: 'fits the blank', same_word: 'exact word', correct: 'correct', understood: 'understood',
  };
  const meters = Object.entries(checks).map(([key, value]) => `
    <div class="meter">
      <span>${escapeHtml(labels[key] || key)}</span>
      <span class="track"><i style="width:${Math.round(value * 100)}%;
        background:${value >= .6 ? 'var(--good)' : value >= .35 ? 'var(--warn)' : 'var(--bad)'}"></i></span>
      <span class="val">${Math.round(value * 100)}%</span>
    </div>`).join('');

  // Who graded this matters. A calibrated judgment and a word comparison are
  // not the same kind of answer, and showing them identically would let the
  // weaker one borrow the stronger one's authority.
  const gradedBy = verdict.model
    ? `${verdict.method === 'local' ? 'checked' : 'judged'} by ${escapeHtml(verdict.model)}`
      + (verdict.latency_ms ? ` in ${verdict.latency_ms} ms` : '')
    : '';
  const scoreLine = verdict.score != null
    ? `<div class="small muted" style="margin-top:6px">overall ${verdict.score.toFixed(1)} / ${verdict.score_max}
       ${gradedBy ? ` · ${gradedBy}` : ''}</div>`
    : (gradedBy ? `<div class="small muted" style="margin-top:6px">${gradedBy}</div>` : '');

  const corrections = (feedback.corrections || []).length ? `
    <ul class="corrections">${feedback.corrections.map(c => `
      <li><span class="was">${escapeHtml(c.their_text || '')}</span> →
          <span class="now">${escapeHtml(c.should_be || '')}</span>
          <span class="why">${escapeHtml(c.why || '')}</span></li>`).join('')}</ul>` : '';

  const reference = kind === 'translate' ? item.es : item.expected;

  // A word comparison is not a judgment, so its grades are presented for what
  // they are -- but "checked by comparison" is not the same as "failed". An
  // answer that matches the article's wording is real evidence and is kept; so
  // is a wrong cloze, where one word is right and the comparison can see that.
  // Only a verdict with no standing to fail is shown as uncheckable.
  const uncheckable = provisional && result.may_fail === false && !correct;
  const comparison = provisional ? (verdict.labels || {}).note || '' : '';
  const headline = uncheckable
    ? 'This answer is too different from the article’s wording to check'
    : (correct ? '✓ ' : '✗ ') + escapeHtml(
        comparison && correct ? comparison
          : feedback.summary || (correct ? 'That works.' : 'Not quite.'));

  return `<div class="verdict ${uncheckable ? 'provisional' : correct ? 'correct' : 'wrong'}">
    <div class="headline">${headline}</div>
    ${provisional ? `<div class="small muted">No judgment model is configured, so this was
      compared word by word against the article. An unusual but correct answer cannot be
      recognised this way — judge it yourself.</div>` : ''}
    ${verdict.error ? `<div class="small muted">${escapeHtml(verdict.error)}</div>` : ''}
    <div class="meters">${meters}</div>
    ${scoreLine}
    ${corrections}
    ${feedback.praise ? `<div class="small" style="margin-top:8px">👍 ${escapeHtml(feedback.praise)}</div>` : ''}
    ${feedback.better ? `<div style="margin-top:10px"><div class="small muted">A natural rendering</div>
      <div style="font-family:var(--serif);color:var(--es-ink);font-size:15px">${escapeHtml(feedback.better)}</div></div>` : ''}
    <div style="margin-top:10px"><div class="small muted">${kind === 'translate' ? "The article's version" : 'Model answer'}</div>
      <div style="font-family:var(--serif);color:var(--es-ink);font-size:15px">${escapeHtml(reference || '')}</div></div>
  </div>`;
}

/* -- review ---------------------------------------------------------------- */

let reviewQueue = [];
let reviewIndex = 0;
let reviewRevealed = false;
let reviewShownAt = 0;
let reviewMode = store.get('reviewMode', 'recognise');

/* -- every saved card ------------------------------------------------------ */

/* The deck as a page, rather than as the queue Review shows.

   Review answers "what is due"; this answers "what have I got" -- a different
   question, asked when a word will not stick, or when you want the words that
   came out of one article, or when you want to see whether anything has been
   sitting in Learning for a month. Nothing here is scheduled or graded; it is
   the deck, listed. */

let cardsState = { query: '', stage: 'all', sort: 'recent', rows: [], total: 0, limit: 0 };

function daysUntil(iso) {
  if (!iso) return null;
  const due = new Date(iso).getTime();
  if (Number.isNaN(due)) return null;
  return (due - Date.now()) / 86400000;
}

/* Said the way a reader would say it: "due now", "in 3 days", "6 weeks ago" for
   something overdue. The exact timestamp is in the tooltip, because a card list
   is scanned and a date is not what is being asked. */
function dueText(iso, stage) {
  const days = daysUntil(iso);
  if (days === null) return { text: stage === 'new' ? 'new' : 'never reviewed', late: false };
  if (days < 0) return { text: days > -1 ? 'due now' : `${Math.round(-days)} days overdue`, late: true };
  if (days < 1) return { text: 'due today', late: false };
  if (days < 14) return { text: `in ${Math.round(days)} day${Math.round(days) === 1 ? '' : 's'}` };
  if (days < 60) return { text: `in ${Math.round(days / 7)} weeks` };
  return { text: `in ${Math.round(days / 30)} months` };
}

async function viewCards() {
  setActiveTab('review');
  $('#reader-bar').hidden = true;
  const view = $('#view');
  view.className = 'view';
  view.innerHTML = '<div class="empty"><span class="spinner"></span></div>';

  const data = await api(`/api/words?search=${encodeURIComponent(cardsState.query)}`);
  cardsState.rows = data.words || [];
  cardsState.total = data.total || 0;
  cardsState.limit = data.limit || 0;

  const stages = Object.fromEntries(STAGE_ORDER.map(([key]) => [key, 0]));
  cardsState.rows.forEach(row => { if (row.stage in stages) stages[row.stage] += 1; });
  const dueNow = cardsState.rows.filter(row => (daysUntil(row.due_at) ?? 1) <= 0).length;

  view.innerHTML = `
    <div class="page-head cards-head">
      <div>
        <h1>Saved words</h1>
        <p class="muted" id="cards-summary"></p>
      </div>
      <div class="row">
        <a class="btn ghost sm" href="/api/export.csv">Export for Anki</a>
        <a class="btn ghost sm" href="#/review">Review</a>
      </div>
    </div>
    <div class="cards-tools">
      <input type="search" id="cards-search" placeholder="Search the Spanish, the English, the sentence you met it in, or the article"
             value="${escapeHtml(cardsState.query)}">
      <div class="chips" id="cards-stages">
        <button class="choice sm ${cardsState.stage === 'all' ? 'active' : ''}" data-stage="all">
          All <span class="muted">${cardsState.rows.length}</span></button>
        ${STAGE_ORDER.filter(([key]) => stages[key]).map(([key, label]) => `
          <button class="choice sm ${cardsState.stage === key ? 'active' : ''}" data-stage="${key}">
            <span class="swatch" style="background:${STAGE_COLOR[key]}"></span>${label}
            <span class="muted">${stages[key]}</span></button>`).join('')}
      </div>
      <label class="cards-sort">Order
        <select id="cards-sort">
          <option value="recent">recently saved</option>
          <option value="alpha">A–Z</option>
          <option value="due">due first</option>
          <option value="stuck">most forgotten</option>
        </select>
      </label>
    </div>
    <div id="cards-list"></div>`;

  $('#cards-sort').value = cardsState.sort;
  renderCardList();

  // Typing is debounced: the search is a request, and it is the whole deck that
  // is being filtered rather than a handful of rows.
  let timer = null;
  $('#cards-search').addEventListener('input', (event) => {
    cardsState.query = event.target.value;
    clearTimeout(timer);
    timer = setTimeout(() => {
      // Keep the caret where it is: re-rendering the whole page to filter a list
      // is how a search box loses focus mid-word.
      const caret = event.target.selectionStart;
      viewCards().then(() => {
        const box = $('#cards-search');
        if (box) { box.focus(); box.setSelectionRange(caret, caret); }
      });
    }, 320);
  });
  $$('[data-stage]', view).forEach(button => button.addEventListener('click', () => {
    cardsState.stage = button.dataset.stage;
    $$('[data-stage]', view).forEach(b => b.classList.toggle('active', b === button));
    renderCardList();
  }));
  $('#cards-sort').addEventListener('change', (event) => {
    cardsState.sort = event.target.value;
    renderCardList();
  });
}

function renderCardList() {
  const host = $('#cards-list');
  if (!host) return;
  const rows = visibleCards();
  const summary = $('#cards-summary');
  const shape = [];
  shape.push(`<strong>${cardsState.total}</strong> saved`);
  if (cardsState.query) shape.push(`${rows.length} matching “${escapeHtml(cardsState.query)}”`);
  if (cardsState.total > cardsState.limit) {
    // A cap nobody meets is harmless; a cap that silently reads as "this is all
    // you have" is not, so it is said out loud.
    shape.push(`showing the ${cardsState.limit} most recent`);
  }
  if (summary) summary.innerHTML = shape.join(' · ');

  if (!rows.length) {
    host.innerHTML = `<div class="empty"><p>${cardsState.query
      ? 'No saved word matches that.'
      : 'Nothing saved yet. Click any Spanish word while reading.'}</p></div>`;
    return;
  }

  host.innerHTML = `<div class="deck">${rows.map(cardRowHtml).join('')}</div>`;
  $$('[data-hear]', host).forEach(button => button.addEventListener('click',
    () => speak(button.dataset.hear)));
  $$('[data-drop-card]', host).forEach(button => button.addEventListener('click', async () => {
    // Two clicks, the app's pattern for anything with no undo.
    if (button.dataset.armed !== '1') {
      button.dataset.armed = '1';
      button.classList.add('danger');
      button.title = 'Click again to remove this word';
      setTimeout(() => {
        if (button.dataset.armed === '1') {
          button.dataset.armed = '0';
          button.classList.remove('danger');
          button.title = 'Remove';
        }
      }, 3500);
      return;
    }
    button.disabled = true;
    try {
      await api(`/api/word/${button.dataset.dropCard}`, { method: 'DELETE' });
      cardsState.rows = cardsState.rows.filter(row => String(row.id) !== button.dataset.dropCard);
      cardsState.total = Math.max(0, cardsState.total - 1);
      toast('Removed from the deck');
      renderCardList();
    } catch (err) {
      button.disabled = false;
      toast(err.message, 'err');
    }
  }));
}

function visibleCards() {
  const rows = cardsState.stage === 'all'
    ? [...cardsState.rows]
    : cardsState.rows.filter(row => row.stage === cardsState.stage);
  // The app's own `fold`: lowercased *and* accent-stripped, so "índice" sorts
  // with the i's rather than after every z.
  if (cardsState.sort === 'alpha') return rows.sort((a, b) => fold(a.term).localeCompare(fold(b.term)));
  if (cardsState.sort === 'due') return rows.sort((a, b) => fold(a.due_at || '9999') < fold(b.due_at || '9999') ? -1 : 1);
  // "Most forgotten" is lapses first, then the words that keep coming back early
  // -- the two things that say a word is not sticking.
  if (cardsState.sort === 'stuck') {
    return rows.sort((a, b) => (b.lapses || 0) - (a.lapses || 0) || (a.interval_days || 0) - (b.interval_days || 0));
  }
  return rows;   // the API already returns them newest first
}

function cardRowHtml(card) {
  const due = dueText(card.due_at, card.stage);
  const lemma = String(card.lemma || '').trim();
  const showsForm = lemma && fold(lemma) !== fold(card.term);
  const reviews = card.reps ? `${card.reps} review${card.reps === 1 ? '' : 's'}` : 'not reviewed';
  const lapses = card.lapses ? ` · ${card.lapses} lapse${card.lapses === 1 ? '' : 's'}` : '';
  return `<div class="deck-row">
    <div class="deck-word">
      <button class="btn-link es" data-hear="${escapeHtml(card.term)}"
              title="Hear it">${escapeHtml(card.term)}</button>
      ${showsForm ? `<div class="small muted">${escapeHtml(card.pos === 'verb' ? 'infinitive' : 'dictionary form')}
        ${escapeHtml(lemma)}</div>` : ''}
      ${card.context ? `<div class="deck-context">${escapeHtml(card.context)}</div>` : ''}
    </div>
    <div class="deck-gloss">${escapeHtml(card.gloss || '—')}</div>
    <div class="deck-when" title="${escapeHtml(card.due_at || '')}">
      <span class="swatch" style="background:${STAGE_COLOR[card.stage] || 'var(--line)'}"></span>
      <span class="${due.late ? 'is-late' : ''}">${escapeHtml(due.text)}</span>
      <div class="small muted">${reviews}${lapses}</div>
    </div>
    <div class="deck-from">${card.article_slug
      ? `<a class="quote-source" href="#/read/${encodeURIComponent(card.article_slug)}"
            title="${escapeHtml(card.article_title || '')}">${escapeHtml(card.article_title || card.article_slug)}</a>`
      : '<span class="muted small">no article</span>'}</div>
    <button class="icon-btn" data-drop-card="${card.id}" title="Remove">✕</button>
  </div>`;
}

async function viewReview() {
  setActiveTab('review');
  $('#reader-bar').hidden = true;
  const view = $('#view');
  view.className = 'view';
  view.innerHTML = '<div class="empty"><span class="spinner"></span></div>';

  const data = await api('/api/review');
  reviewQueue = data.cards || [];
  reviewIndex = 0;
  if (!reviewQueue.length) {
    view.innerHTML = `<div class="empty"><h3>Nothing due</h3>
      <p>${data.due ? '' : 'Every card is scheduled for later. '}Save words while reading and they will come back here.</p>
      <p style="margin-top:16px"><a class="btn primary" href="#/library">Read something</a>
        <a class="btn ghost" href="#/cards">See every word you have saved</a></p></div>`;
    return;
  }
  view.innerHTML = `<div class="review-stage">
    <div class="row" style="margin-bottom:14px">
      <div class="segmented" role="group" aria-label="Review mode">
        <button data-mode="recognise" class="${reviewMode === 'recognise' ? 'active' : ''}"
          title="See the Spanish, recall the meaning">Recognise</button>
        <button data-mode="produce" class="${reviewMode === 'produce' ? 'active' : ''}"
          title="See the sentence with the word blanked, type it back">Produce</button>
      </div>
      <span class="spacer"></span>
      <span class="small muted">${reviewQueue.length} card${reviewQueue.length === 1 ? '' : 's'}</span>
      <a class="btn ghost sm" href="#/cards" title="Every saved word, not only what is due">All saved words</a>
    </div>
    <div id="review-stage"></div>
  </div>`;
  $$('[data-mode]', view).forEach(button => button.addEventListener('click', () => {
    reviewMode = button.dataset.mode;
    store.set('reviewMode', reviewMode);
    $$('[data-mode]', view).forEach(b => b.classList.toggle('active', b.dataset.mode === reviewMode));
    renderCard();
  }));
  renderCard();
}

/* Blank the target out of the sentence it was met in.

   For a single word this is easy. For a *construction* -- "se ponen de
   acuerdo", "dar lugar a" -- blanking one word out of it tests almost nothing:
   the phrase is the unit the lesson taught, so the phrase is what gets
   removed. The two are matched differently because they need to be: a word is
   found by its stem, a phrase by its first and last content words with a short
   gap allowed between them for whatever inflection sits in the middle. */
const BLANK_STOPWORDS = new Set(['se', 'me', 'te', 'nos', 'os', 'lo', 'la', 'le', 'los', 'las',
  'el', 'un', 'una', 'de', 'del', 'a', 'al', 'en', 'con', 'por', 'para', 'que', 'y', 'o', 'su']);

const escapeRegExp = (text) => String(text).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function blankOut(sentence, term) {
  const clean = (word) => word.replace(/[.,;:!?¡¿"“”]/g, '');
  const words = String(term).split(/[\s/,(]+/).map(clean).filter(Boolean);
  const content = words.filter(w => w.length >= 3 && !BLANK_STOPWORDS.has(w.toLowerCase()));

  if (content.length >= 2) {
    const first = content[0];
    const last = content[content.length - 1];
    // Allow a short run of anything between them: "ponen de acuerdo",
    // "ponen mucho de acuerdo". Bounded so it cannot swallow the sentence.
    const pattern = new RegExp(
      `\\b(${escapeRegExp(first.slice(0, Math.max(4, first.length - 2)))}[^\\s.,;:!?]*` +
      `[^.!?]{0,42}?${escapeRegExp(last.slice(0, Math.max(4, last.length - 2)))}[^\\s.,;:!?]*)`, 'i');
    if (pattern.test(sentence)) {
      return { sentence: sentence.replace(pattern, '_____'), kind: 'phrase' };
    }
  }

  for (const candidate of content.length ? content : words) {
    if (candidate.length < 4) continue;
    const head = candidate.slice(0, Math.max(4, candidate.length - 2));
    const pattern = new RegExp(`\\b(${escapeRegExp(head)}[^\\s.,;:!?]*)`, 'i');
    if (pattern.test(sentence)) {
      return { sentence: sentence.replace(pattern, '_____'), kind: 'word' };
    }
  }
  return null;
}

function renderCard() {
  const stage = $('#review-stage');
  if (!stage) return;
  if (reviewIndex >= reviewQueue.length) {
    stage.innerHTML = `<div class="empty"><h3>That's the queue</h3>
      <p>${reviewQueue.length} card${reviewQueue.length === 1 ? '' : 's'} reviewed.</p>
      <p style="margin-top:16px"><a class="btn primary" href="#/library">Back to reading</a>
      <a class="btn ghost" href="#/stats">See progress</a></p></div>`;
    refreshDuePill();
    return;
  }

  const card = reviewQueue[reviewIndex];
  reviewRevealed = false;
  reviewShownAt = Date.now();

  const context = card.context ? highlightIn(card.context, card.term) : '';
  const blank = reviewMode === 'produce' && card.context ? blankOut(card.context, card.term) : null;
  const prompt = blank
    ? `<div class="cloze-sentence">${escapeHtml(blank.sentence).replace('_____', '<span class="blank">_____</span>')}</div>`
    : `<div class="prompt">${escapeHtml(card.term)}</div>`;
  const wanted = blank ? (blank.kind === 'phrase' ? 'the whole phrase' : `the missing ${card.pos || 'word'}`) : '';

  stage.innerHTML = `
    <div class="row" style="margin-bottom:12px">
      <span class="small muted">${reviewIndex + 1} of ${reviewQueue.length}</span>
      <span class="spacer"></span>
      <span class="chip">${escapeHtml(card.stage)}</span>
    </div>
    <div class="card-face">
      ${prompt}
      ${blank ? `<div class="small muted" style="margin-top:10px">type ${escapeHtml(wanted)}</div>` : ''}
      ${!blank && card.pos ? `<div class="small muted" style="margin-top:6px">${escapeHtml(card.pos)}</div>` : ''}
      ${!blank && context ? `<div class="context">${context}</div>` : ''}
      ${blank ? `<div class="cloze-input">
        <input type="text" id="cloze-answer" autocomplete="off" autocapitalize="off" spellcheck="false"
               placeholder="${escapeHtml(card.gloss || 'the missing word')}">
        <button class="btn primary sm" id="cloze-check">Check</button>
      </div>` : ''}
      <div id="card-answer"></div>
    </div>
    <div id="card-grades"></div>
    <p class="small muted" style="text-align:center;margin-top:14px">
      <span class="kbd">space</span> reveal · <span class="kbd">1</span>–<span class="kbd">4</span> grade ·
      <span class="kbd">S</span> hear it</p>`;

  if (blank) {
    $('#card-answer').innerHTML = '<div class="muted small" style="margin-top:16px">— write it, then check —</div>';
    const input = $('#cloze-answer');
    input.focus();
    const submit = () => checkCloze(card);
    $('#cloze-check').addEventListener('click', submit);
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); event.stopPropagation(); submit(); }
    });
  } else {
    $('#card-answer').innerHTML = '<div class="muted small" style="margin-top:20px">— recall the meaning, then reveal —</div>';
  }
}

/* The dictionary form, when the word on the card is not already it.

   A word is saved in whatever form it appeared in — `enfatizan`, `se vuelve` —
   so the card reviews that form, and "enfatizan means they emphasise" is not the
   same knowledge as knowing the verb. Naming the form it belongs to is what
   turns one into the other.

   Shown only when it differs from the card's own word: for a word saved in its
   dictionary form the line would say nothing. Labelled "infinitive" for a verb
   because that is the useful name, and "dictionary form" otherwise, since a noun
   has no infinitive and saying so would be wrong. */
function dictionaryForm(card) {
  const lemma = String(card.lemma || '').trim();
  const term = String(card.term || '').trim();
  if (!lemma || !term || fold(lemma) === fold(term)) return '';
  const label = String(card.pos || '').toLowerCase() === 'verb' ? 'infinitive' : 'dictionary form';
  return `<div class="form-of">${escapeHtml(label)} <b>${escapeHtml(lemma)}</b></div>`;
}

async function checkCloze(card) {
  const input = $('#cloze-answer');
  if (!input || input.disabled) return;
  const answer = input.value.trim();
  if (!answer) { toast('Write something first'); return; }

  input.disabled = true;
  $('#cloze-check').disabled = true;
  $('#card-answer').innerHTML = '<div class="loading"><span class="spinner"></span> checking</div>';

  let result = null;
  try {
    result = await api('/api/attempt', {
      method: 'POST',
      body: {
        kind: 'cloze', answer, question: card.context || '',
        reference: card.term, slug: card.article_slug,
      },
    });
  } catch (err) {
    // Grading is a nicety here; never block the review on it.
    $('#card-answer').innerHTML = `<div class="small muted">could not grade: ${escapeHtml(err.message)}</div>`;
  }

  const correct = result ? result.correct : null;
  const checks = (result && result.verdict && result.verdict.checks) || {};
  $('#card-answer').innerHTML = `<div class="answer">
    <div class="gloss">${escapeHtml(card.term)}</div>
    ${dictionaryForm(card)}
    <div class="note">${escapeHtml(card.gloss || '')}${card.note ? ` — ${escapeHtml(card.note)}` : ''}</div>
    ${result ? `<div class="meters" style="max-width:340px;margin:14px auto 0">
      ${Object.entries(checks).map(([key, value]) => `
        <div class="meter"><span>${key === 'fits' ? 'fits the blank' : 'exact word'}</span>
        <span class="track"><i style="width:${Math.round(value * 100)}%;
          background:${value >= .6 ? 'var(--good)' : value >= .35 ? 'var(--warn)' : 'var(--bad)'}"></i></span>
        <span class="val">${Math.round(value * 100)}%</span></div>`).join('')}
    </div>` : ''}
    ${correct === false ? '<div class="small" style="margin-top:8px;color:var(--bad)">not quite — but the meaning may still fit, so judge yourself</div>' : ''}
  </div>`;
  reviewRevealed = true;
  renderGrades(card);
}

function renderGrades(card) {
  const order = [['again', 'Again'], ['hard', 'Hard'], ['good', 'Good'], ['easy', 'Easy']];
  $('#card-grades').innerHTML = `<div class="grades">${order.map(([key, label]) => `
    <button data-rating="${key}"><span class="label">${label}</span>
      <span class="when">${escapeHtml((card.preview || {})[key] || '')}</span></button>`).join('')}</div>`;
  $$('#card-grades button').forEach(button =>
    button.addEventListener('click', () => gradeCard(button.dataset.rating)));
}

function highlightIn(sentence, term) {
  const escaped = escapeHtml(sentence);
  const stem = escapeHtml(term.split(/\s+/)[0].replace(/[.,;:!?¡¿]/g, ''));
  if (!stem) return escaped;
  return escaped.replace(new RegExp(`\\b(${stem}\\w*)`, 'i'), '<b>$1</b>');
}

function revealCard() {
  if (reviewRevealed) return;
  if (reviewMode === 'produce' && $('#cloze-answer') && !$('#cloze-answer').disabled) {
    // In produce mode, revealing without answering forfeits the recall attempt.
    checkCloze(reviewQueue[reviewIndex]);
    return;
  }
  reviewRevealed = true;
  const card = reviewQueue[reviewIndex];
  $('#card-answer').innerHTML = `<div class="answer">
    <div class="gloss">${escapeHtml(card.gloss || '—')}</div>
    ${dictionaryForm(card)}
    ${card.note ? `<div class="note">${escapeHtml(card.note)}</div>` : ''}
  </div>`;
  renderGrades(card);
}

async function gradeCard(rating) {
  if (!reviewRevealed) { revealCard(); return; }
  const card = reviewQueue[reviewIndex];
  try {
    await api(`/api/review/${card.card_id}`, {
      method: 'POST',
      body: { rating, elapsed_ms: Date.now() - reviewShownAt },
    });
  } catch (err) { toast(err.message, 'err'); }
  reviewIndex += 1;
  renderCard();
}

/* -- reading analytics ------------------------------------------------------ */

let analyticsDays = store.get('analyticsDays', 7);

async function renderReadingAnalytics(host) {
  let data;
  try {
    data = await api(`/api/analytics/reading?days=${analyticsDays}`);
  } catch (err) {
    host.innerHTML = `<div class="panel"><h3>Reading</h3>
      <p class="panel-sub">${escapeHtml(err.message)}</p></div>`;
    return;
  }

  const exposure = data.exposure;
  const split = data.split;
  const scaffold = data.scaffolding;
  const share = (ratio) => `${Math.round((ratio || 0) * 100)}%`;

  const tiles = [
    ['Spanish words read', exposure.tokens.toLocaleString(), data.window_label],
    ['Already known', share(exposure.understood_ratio), `${exposure.known.toLocaleString()} of ${exposure.tokens.toLocaleString()}`],
    ['Different words', exposure.distinct_words.toLocaleString(), `${exposure.articles} article${exposure.articles === 1 ? '' : 's'}`],
    ['Lookups / 1k words', scaffold.lookups_per_1000.toFixed(0), `${scaffold.lookups} lookups`],
  ];

  host.innerHTML = `
    <div class="panel">
      <h3>Reading</h3>
      <p class="panel-sub">${escapeHtml(data.headline)}</p>
      <div class="row" style="margin-bottom:16px">
        <div class="segmented" role="group" aria-label="Time window">
          ${[7, 30].map(d => `<button data-window="${d}" class="${d === analyticsDays ? 'active' : ''}">
            ${d} days</button>`).join('')}
        </div>
      </div>

      ${data.has_data ? `
        <div class="tiles tight-tiles">
          ${tiles.map(([k, v, sub]) => `<div class="tile">
            <div class="k">${k}</div><div class="v">${v}</div><div class="sub">${escapeHtml(sub)}</div>
          </div>`).join('')}
        </div>

        <h4 class="sub-head">How much of it was new</h4>
        <p class="panel-sub">A word met for the first time, versus one you have met before,
          versus one you keep meeting — which is what the reading is actually drilling.</p>
        <div class="stack">
          ${[['new', 'var(--stage-1)'], ['familiar', 'var(--stage-2)'], ['repeated', 'var(--stage-4)']]
            .map(([key, colour]) => split[key]
              ? `<span style="background:${colour};width:${(split[key] / Math.max(split.total, 1) * 100).toFixed(1)}%"
                       title="${key}: ${split[key]}"></span>` : '').join('')}
        </div>
        <div class="legend">
          ${[['new', 'New', 'var(--stage-1)'], ['familiar', 'Met before', 'var(--stage-2)'],
             ['repeated', 'Keep meeting', 'var(--stage-4)']]
            .map(([key, label, colour]) => `<span class="item">
              <span class="swatch" style="background:${colour}"></span>${label}
              <span class="n">${split[key].toLocaleString()} · ${share(split.shares[key])}</span></span>`).join('')}
        </div>

        <h4 class="sub-head">Words read per day</h4>
        <p class="panel-sub">The filled part is words already in your vocabulary; the pale part
          is words you had not met before.</p>
        ${dailyExposureChart(data.series)}

        <h4 class="sub-head">Depending on the English</h4>
        <p class="panel-sub">Every time you reached for help, per thousand Spanish words read.
          A rate rather than a score — 40 lookups is a lot in 800 words and nothing in 12,000.</p>
        <div class="tiles tight-tiles">
          ${[
            ['Lookups', scaffold.lookups, scaffold.lookups_per_1000],
            ['Full entries', scaffold.deep_entries, null],
            ['Explanations', scaffold.counts.explain || 0, scaffold.explains_per_1000],
            ['Translations', scaffold.counts.translate || 0, scaffold.translations_per_1000],
            ['Glosses turned back on', scaffold.gloss_reveals, null],
          ].map(([k, n, rate]) => `<div class="tile">
            <div class="k">${k}</div><div class="v">${n}</div>
            <div class="sub">${rate == null ? 'total' : `${rate.toFixed(0)} per 1,000 words`}</div>
          </div>`).join('')}
        </div>

        ${data.top_words.length ? `
          <h4 class="sub-head">Words you keep meeting</h4>
          <p class="panel-sub">Not yet in your deck. These are the ones the reading is
            repeatedly putting in front of you.</p>
          <div class="row">
            ${data.top_words.map(word => `<button class="btn sm" data-encounter="${escapeHtml(word.lemma)}"
                data-gloss="${escapeHtml(word.gloss || '')}" title="read ${word.times} times">
              ${escapeHtml(word.lemma)} <span class="muted">${word.times}×</span></button>`).join('')}
          </div>` : ''}

        ${data.by_article.length ? `
          <h4 class="sub-head">Where the reading happened</h4>
          <table class="data">
            <thead><tr><th>Article</th><th>Words</th><th>Known</th></tr></thead>
            <tbody>${data.by_article.map(row => `<tr>
              <td><a href="#/read/${encodeURIComponent(row.slug)}" style="color:var(--es-ink)">${escapeHtml(row.title.slice(0, 58))}</a></td>
              <td>${row.tokens.toLocaleString()}</td>
              <td>${share(row.understood_ratio)}</td>
            </tr>`).join('')}</tbody>
          </table>` : ''}
      ` : `
        <div class="empty" style="padding:28px">
          <h3>Nothing counted yet</h3>
          <p>Open an article and read a little. Words are credited as you scroll —
            the reader already reports its position, so nothing extra is asked of it.</p>
        </div>`}
    </div>`;

  $$('[data-window]', host).forEach(button => button.addEventListener('click', () => {
    analyticsDays = Number(button.dataset.window);
    store.set('analyticsDays', analyticsDays);
    renderReadingAnalytics(host);
  }));
  $$('[data-encounter]', host).forEach(button => button.addEventListener('click', async () => {
    try {
      const result = await api('/api/word/save', {
        method: 'POST',
        body: { term: button.dataset.encounter, lemma: button.dataset.encounter,
                gloss: button.dataset.gloss || null },
      });
      button.disabled = true;
      button.innerHTML = `✓ ${escapeHtml(button.dataset.encounter)}`;
      toast(`Saved · ${result.total} words`);
      refreshDuePill();
    } catch (err) { toast(err.message, 'err'); }
  }));
}

/* Two series but one question: of the words read today, how many did you
   already know. Drawn as one bar with a filled portion rather than two bars,
   because the second number is a part of the first, not a peer of it. */
function dailyExposureChart(series) {
  const recent = series.slice(-30);
  const peak = Math.max(1, ...recent.map(d => d.tokens));
  return `<div class="chart exposure" id="exposure-chart">
    ${recent.map((day, index) => {
      const total = day.tokens;
      const known = day.known;
      const height = total ? Math.max(4, Math.round(total / peak * 100)) : 2;
      const share = total ? Math.round(known / total * 100) : 0;
      return `<div class="col ${total ? '' : 'zero'}"
        title="${day.day}: ${total} words, ${share}% already known">
        <i style="height:${height}%;position:relative;display:flex;align-items:flex-end">
          ${total ? `<span style="display:block;width:100%;height:${share}%;background:var(--accent-ink);
            border-radius:4px 4px 0 0"></span>` : ''}
        </i></div>`;
    }).join('')}
  </div>
  <div class="chart-axis"><span>${recent[0] ? recent[0].day.slice(5) : ''}</span><span>today</span></div>`;
}

/* -- corpus map ------------------------------------------------------------- */

/* A force-directed layout, in about thirty lines.
 *
 * Small enough not to warrant a library: two dozen nodes settle in a few
 * hundred iterations, which is a couple of milliseconds, and the alternative
 * is a dependency for one view. Seeded on a circle rather than at random so the
 * same library always produces the same map -- a graph that reshuffles itself
 * on every visit cannot be learned. */
function layoutGraph(nodes, edges, width, height, iterations = 400) {
  const bySlug = new Map(nodes.map(n => [n.slug, n]));
  const radius = Math.min(width, height) * 0.36;
  nodes.forEach((node, index) => {
    const angle = (index / nodes.length) * Math.PI * 2;
    node.x = width / 2 + Math.cos(angle) * radius;
    node.y = height / 2 + Math.sin(angle) * radius;
  });

  const area = width * height;
  const k = Math.sqrt(area / Math.max(nodes.length, 1));
  for (let step = 0; step < iterations; step += 1) {
    const cooling = 1 - step / iterations;
    nodes.forEach(node => { node.dx = 0; node.dy = 0; });

    for (let i = 0; i < nodes.length; i += 1) {
      for (let j = i + 1; j < nodes.length; j += 1) {
        const a = nodes[i], b = nodes[j];
        let dx = a.x - b.x, dy = a.y - b.y;
        let distance = Math.hypot(dx, dy) || 0.01;
        const force = (k * k) / distance;
        dx /= distance; dy /= distance;
        a.dx += dx * force; a.dy += dy * force;
        b.dx -= dx * force; b.dy -= dy * force;
      }
    }
    for (const edge of edges) {
      const a = bySlug.get(edge.source), b = bySlug.get(edge.target);
      if (!a || !b) continue;
      let dx = a.x - b.x, dy = a.y - b.y;
      const distance = Math.hypot(dx, dy) || 0.01;
      const force = (distance * distance) / k * (0.4 + edge.weight);
      dx /= distance; dy /= distance;
      a.dx -= dx * force; a.dy -= dy * force;
      b.dx += dx * force; b.dy += dy * force;
    }
    for (const node of nodes) {
      const length = Math.hypot(node.dx, node.dy) || 0.01;
      const limit = Math.min(length, k * cooling);
      node.x += (node.dx / length) * limit;
      node.y += (node.dy / length) * limit;
      node.x = Math.max(30, Math.min(width - 30, node.x));
      node.y = Math.max(24, Math.min(height - 24, node.y));
    }
  }
  return nodes;
}

async function renderCorpusMap(host) {
  let data;
  try {
    data = await api('/api/analytics/corpus');
  } catch (err) {
    host.innerHTML = `<div class="panel"><h3>Passage map</h3>
      <p class="panel-sub">${escapeHtml(err.message)}</p></div>`;
    return;
  }

  const width = 860, height = 520;
  const nodes = layoutGraph(data.nodes.map(n => ({ ...n })), data.edges, width, height);
  const bySlug = new Map(nodes.map(n => [n.slug, n]));
  const maxWords = Math.max(...nodes.map(n => n.words), 1);
  const radius = (words) => 5 + Math.sqrt(words / maxWords) * 13;
  /* Categorical, not ordinal. Clusters are identities -- one is not "more"
     than another -- and the first attempt coloured them with three steps of the
     same teal ramp, which passed every check and was still unreadable: three
     shades of one hue cannot say "these are different groups". These are the
     first slots of the validated categorical order, checked against this
     surface (worst adjacent CVD ΔE 9.2, normal-vision 27.6). The palette
     validator flags two of them as below 3:1 on paper, which is why every node
     carries a visible label and there is a legend. */
  const clusterColour = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100'];
  const labelled = placeLabels(nodes, radius, width, height);
  const clusterOf = new Map(nodes.map(node => [node.slug, node.cluster]));

  host.innerHTML = `<div class="panel">
    <h3>Passage map</h3>
    <p class="panel-sub">Two passages are joined when they share vocabulary — rare words
      count for more than common ones, so the links are real subjects rather than
      articles that both happen to say "ejemplo". Node size is length; colour is the
      group the layout found, and each group is named from the words its members'
      titles share, so you can check the name against the passages in it. The ring is
      how much of it you already know. Hover a node to see what it shares, click it to
      read it, and click a group in the legend to see only that group.</p>
    <div class="map-wrap">
      <svg id="corpus-map" viewBox="0 0 ${width} ${height}" role="img"
           aria-label="Articles connected by shared vocabulary">
        <g class="map-view">
          <g class="map-edges">
            ${data.edges.map(edge => {
              const a = bySlug.get(edge.source), b = bySlug.get(edge.target);
              if (!a || !b) return '';
              return `<line x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}"
                            x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}"
                            data-a="${escapeHtml(edge.source)}" data-b="${escapeHtml(edge.target)}"
                            stroke-width="${(0.6 + edge.weight * 2.4).toFixed(2)}"
                            stroke-opacity="${(0.18 + edge.weight * 0.5).toFixed(2)}"></line>`;
            }).join('')}
          </g>
          <g class="map-nodes">
            ${nodes.map(node => `
              <g class="map-node" data-slug="${escapeHtml(node.slug)}"
                 data-cluster="${node.cluster}"
                 data-title="${escapeHtml(node.title)}"
                 data-shared="${escapeHtml(sharedFor(data.edges, node.slug))}">
                <circle cx="${node.x.toFixed(1)}" cy="${node.y.toFixed(1)}" r="${radius(node.words).toFixed(1)}"
                        fill="${clusterColour[node.cluster % clusterColour.length]}"
                        stroke="var(--surface)" stroke-width="2"></circle>
                <circle cx="${node.x.toFixed(1)}" cy="${node.y.toFixed(1)}" r="${(radius(node.words) + 3.5).toFixed(1)}"
                        fill="none" stroke="var(--ink-2)" stroke-width="1.6"
                        stroke-dasharray="${(2 * Math.PI * (radius(node.words) + 3.5) * Math.max(node.coverage, 0.02)).toFixed(1)} 999"
                        transform="rotate(-90 ${node.x.toFixed(1)} ${node.y.toFixed(1)})"
                        opacity="${node.coverage > 0.02 ? 0.85 : 0}"></circle>
                ${node.__label ? `<text x="${node.__label.x.toFixed(1)}" y="${node.__label.y.toFixed(1)}"
                      text-anchor="middle">${escapeHtml(shortTitle(node.title))}</text>` : ''}
              </g>`).join('')}
          </g>
        </g>
      </svg>
      <div class="map-controls">
        <button data-map-zoom="in" title="Zoom in">＋</button>
        <button data-map-zoom="out" title="Zoom out">−</button>
        <button data-map-zoom="reset" title="Fit the whole map">Fit</button>
      </div>
    </div>
    <p class="small muted map-hint">Drag to pan · scroll to zoom · click a passage to read it ·
      click a group to see only it</p>
    <div class="legend legend-groups">
      ${data.clusters.map(cluster => `<button class="item" data-cluster="${cluster.id}"
          title="${escapeHtml(cluster.terms.length
            ? `Shares the vocabulary: ${cluster.terms.join(', ')}` : 'No words in common with the rest')}">
        <span class="swatch" style="background:${clusterColour[cluster.id % clusterColour.length]}"></span>
        ${cluster.size} ${cluster.size === 1 ? 'passage' : 'passages'}
        <span class="n">${escapeHtml(clusterLabel(cluster))}</span>
      </button>`).join('')}
      <span class="item"><span class="swatch ring-swatch"></span>ring = how much you already know</span>
    </div>
    ${labelled < nodes.length ? `<p class="small muted" style="margin-top:8px">
      ${nodes.length - labelled} of ${nodes.length} labels are hidden to keep the map legible —
      hover any node for its title.</p>` : ''}

    ${data.within_reach.length ? `
      <h4 class="sub-head">Closest to being readable</h4>
      <p class="panel-sub">Ranked by how much of their Spanish is already in your vocabulary.</p>
      <div class="row">
        ${data.within_reach.map(item => `<a class="btn sm" href="#/read/${encodeURIComponent(item.slug)}">
          ${escapeHtml(shortTitle(item.title))}
          <span class="muted">${Math.round(item.coverage * 100)}% known</span></a>`).join('')}
      </div>` : ''}
  </div>`;

  const map = wireMapInteractions($('#corpus-map', host), host);

  /* Two things dim the map, and they have to compose: hovering a passage
     isolates it and its neighbours for as long as the pointer is there, and
     clicking a group in the legend keeps only that group. One paint, computed
     from both, rather than two handlers fighting over the same classes. */
  let focusedCluster = null;
  let hovered = null;
  const paint = () => {
    const near = new Set();
    if (hovered) {
      near.add(hovered);
      data.edges.forEach(edge => {
        if (edge.source === hovered) near.add(edge.target);
        if (edge.target === hovered) near.add(edge.source);
      });
    }
    $$('.map-node', host).forEach(other => {
      const inCluster = focusedCluster === null || Number(other.dataset.cluster) === focusedCluster;
      const nearHover = !hovered || near.has(other.dataset.slug);
      other.classList.toggle('dim', !(inCluster && nearHover));
    });
    $$('.map-edges line', host).forEach(line => {
      const both = clusterOf.get(line.dataset.a) === focusedCluster
        && clusterOf.get(line.dataset.b) === focusedCluster;
      const touches = !hovered || line.dataset.a === hovered || line.dataset.b === hovered;
      line.classList.toggle('dim', !((focusedCluster === null || both) && touches));
    });
  };

  $$('.legend-groups [data-cluster]', host).forEach(button => button.addEventListener('click', () => {
    const id = Number(button.dataset.cluster);
    // Clicking the group you are already in lets go of it, so the map is never
    // stuck filtered with no obvious way back.
    focusedCluster = focusedCluster === id ? null : id;
    $$('.legend-groups [data-cluster]', host).forEach(other =>
      other.classList.toggle('active', Number(other.dataset.cluster) === focusedCluster));
    paint();
  }));

  $$('.map-node', host).forEach(group => {
    group.addEventListener('click', () => {
      // A pan that ended over a node is not a click on that node.
      if (host.__mapSuppressed && host.__mapSuppressed()) return;
      location.hash = `#/read/${encodeURIComponent(group.dataset.slug)}`;
    });
    group.addEventListener('mouseenter', () => { hovered = group.dataset.slug; paint(); });
    group.addEventListener('mouseleave', () => { hovered = null; paint(); });
    const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
    title.textContent = `${group.dataset.title}${group.dataset.shared ? `\nshares: ${group.dataset.shared}` : ''}`;
    group.appendChild(title);
  });
}

/* What a group of passages is about.

   Named from the words its members' *titles* share, not from the vocabulary they
   have in common in their text -- which is how a group of fifteen articles about
   art came to be labelled "escuela · archivado · camara": true, shared, and no use
   at all for deciding whether you want to read the group. The words it is named
   from are in the titles the reader can see, so the label can be checked rather
   than believed. */
function clusterLabel(cluster) {
  const topic = (cluster.topic || []).join(' · ');
  if (topic) return topic;
  return cluster.size === 1 ? 'stands alone' : 'no shared subject';
}

/* Pan and zoom for the passage map.
 *
 * A force layout is only useful if you can get inside it: two dozen nodes in a
 * fixed frame means the interesting ones -- the passages you have read, the
 * cluster you are working through -- are unavoidably small.
 *
 * Three decisions worth stating:
 *
 * *   **Zoom is anchored to the cursor**, not the centre. Zooming toward what
 *     you are pointing at is the difference between navigating and fighting.
 * *   **A drag must not navigate.** Clicking a node opens the article, so the
 *     drag tracks how far the pointer moved and the click handler ignores
 *     anything past a few pixels -- otherwise every pan ends by opening
 *     whichever passage you happened to release over.
 * *   **Wheel zoom yields at the limits.** At the closest or furthest zoom,
 *     continuing to scroll is passed through to the page rather than swallowed,
 *     so the map never becomes a trap you cannot scroll past.
 */
function wireMapInteractions(svg, host) {
  const content = $('.map-view', svg);
  if (!content) return;

  const MIN_ZOOM = 0.35, MAX_ZOOM = 6;
  const view = { x: 0, y: 0, k: 1 };
  let drag = null;
  let suppressClick = false;

  const apply = () => content.setAttribute(
    'transform', `translate(${view.x.toFixed(2)} ${view.y.toFixed(2)}) scale(${view.k.toFixed(4)})`);
  apply();

  /* Client pixels to viewBox units. The SVG scales to its container, so a
     screen delta means different things at different widths. */
  const toView = (clientX, clientY) => {
    const rect = svg.getBoundingClientRect();
    const box = svg.viewBox.baseVal;
    return {
      x: ((clientX - rect.left) / Math.max(rect.width, 1)) * box.width,
      y: ((clientY - rect.top) / Math.max(rect.height, 1)) * box.height,
    };
  };

  const zoomAt = (point, factor) => {
    const next = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, view.k * factor));
    if (Math.abs(next - view.k) < 1e-4) return false;
    const ratio = next / view.k;
    // Keep the point under the cursor where it is.
    view.x = point.x - (point.x - view.x) * ratio;
    view.y = point.y - (point.y - view.y) * ratio;
    view.k = next;
    apply();
    return true;
  };

  svg.addEventListener('wheel', (event) => {
    const point = toView(event.clientX, event.clientY);
    // Proportional to the scroll, not one step per event: a trackpad emits a
    // stream of tiny deltas and a mouse wheel a few large ones, and a fixed
    // step makes one feel stuck and the other uncontrollable.
    const steps = Math.min(Math.abs(event.deltaY) / 120, 4);
    if (steps < 0.05) return;
    const factor = Math.pow(event.deltaY < 0 ? 1.15 : 1 / 1.15, steps);
    if (!zoomAt(point, factor)) return;    // at the limit: let the page scroll
    event.preventDefault();
  }, { passive: false });

  svg.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    drag = { id: event.pointerId, x: event.clientX, y: event.clientY, vx: view.x, vy: view.y, moved: 0 };
    svg.classList.add('dragging');
  });

  // Bound on the window rather than the element: with pointer capture the node's
  // own click is retargeted, and then clicking a passage stops opening it.
  const onMove = (event) => {
    if (!drag || event.pointerId !== drag.id) return;
    drag.moved = Math.max(drag.moved, Math.hypot(event.clientX - drag.x, event.clientY - drag.y));
    const from = toView(drag.x, drag.y);
    const now = toView(event.clientX, event.clientY);
    view.x = drag.vx + (now.x - from.x);
    view.y = drag.vy + (now.y - from.y);
    apply();
  };

  const onUp = (event) => {
    if (!drag || event.pointerId !== drag.id) return;
    suppressClick = drag.moved > 4;
    drag = null;
    svg.classList.remove('dragging');
  };

  window.addEventListener('pointermove', onMove);
  window.addEventListener('pointerup', onUp);
  window.addEventListener('pointercancel', onUp);

  // Clear the suppression after the click that follows this drag has been seen.
  svg.addEventListener('click', () => { setTimeout(() => { suppressClick = false; }, 0); }, true);

  $$('[data-map-zoom]', host).forEach(button => button.addEventListener('click', () => {
    const action = button.dataset.mapZoom;
    const box = svg.viewBox.baseVal;
    const centre = { x: box.width / 2, y: box.height / 2 };
    if (action === 'in') zoomAt(centre, 1.3);
    else if (action === 'out') zoomAt(centre, 1 / 1.3);
    else { view.x = 0; view.y = 0; view.k = 1; apply(); }
  }));

  host.__mapSuppressed = () => suppressClick;
  return view;
}
/* Labels collide badly at default spacing -- a dozen titles smeared across the
 * top of the canvas. Placing them largest-node-first and skipping any that
 * would overlap keeps the map readable and keeps the largest passages named,
 * which are the ones worth finding. The rest are one hover away, and the count
 * of hidden labels is stated rather than left to be discovered. */
function placeLabels(nodes, radius, width, height) {
  const LINE = 12, CHAR = 5.7;
  const boxes = [];
  let placed = 0;
  for (const node of [...nodes].sort((a, b) => b.words - a.words)) {
    const text = shortTitle(node.title);
    const boxWidth = text.length * CHAR;
    const y = node.y + radius(node.words) + LINE - 1;
    // Clamp into the canvas rather than skipping: a node near an edge would
    // otherwise never be named.
    const x = Math.max(boxWidth / 2 + 4, Math.min(width - boxWidth / 2 - 4, node.x));
    const box = { x1: x - boxWidth / 2 - 2, x2: x + boxWidth / 2 + 2, y1: y - LINE + 3, y2: y + 3 };
    if (y > height - 4) continue;
    const clash = boxes.some(other =>
      box.x1 < other.x2 && box.x2 > other.x1 && box.y1 < other.y2 && box.y2 > other.y1);
    if (clash) {
      // Try above the node before giving up on it.
      const top = node.y - radius(node.words) - 5;
      const above = { x1: box.x1, x2: box.x2, y1: top - LINE + 3, y2: top + 3 };
      const clashAbove = top < LINE ||
        boxes.some(other => above.x1 < other.x2 && above.x2 > other.x1 && above.y1 < other.y2 && above.y2 > other.y1);
      if (clashAbove) continue;
      node.__label = { x, y: top };
      boxes.push(above);
      placed += 1;
      continue;
    }
    node.__label = { x, y };
    boxes.push(box);
    placed += 1;
  }
  return placed;
}

function sharedFor(edges, slug) {
  const found = edges.filter(edge => edge.source === slug || edge.target === slug)
    .sort((a, b) => b.weight - a.weight)[0];
  return found ? found.shared.join(', ') : '';
}

const shortTitle = (title) => (title.length > 26 ? `${title.slice(0, 24)}…` : title);

async function viewStats() {
  setActiveTab('stats');
  $('#reader-bar').hidden = true;
  const view = $('#view');
  view.className = 'view';
  view.innerHTML = '<div class="empty"><span class="spinner"></span></div>';

  const [stats, library, words] = await Promise.all([
    api('/api/stats'), api('/api/library'), api('/api/words'),
  ]);

  const activity = stats.activity || [];
  const peak = Math.max(1, ...activity.map(d => d.reviews));
  const totalReviews = activity.reduce((sum, d) => sum + d.reviews, 0);
  const stageOrder = STAGE_ORDER;
  const stageTotal = Math.max(1, Object.values(stats.stages).reduce((a, b) => a + b, 0));

  view.innerHTML = `
    <div class="page-head">
      <h1>Progress</h1>
      <p>How much you have read, and how well the words have stuck.</p>
    </div>

    <div id="challenge-panel"></div>
    <div id="reading-analytics"></div>
    <div id="corpus-map-panel" class="panel"><div class="loading">
      <span class="spinner"></span> mapping the corpus</div></div>

    <div class="tiles">
      <div class="tile"><div class="k">Streak</div><div class="v">${stats.streak.current}</div>
        <div class="sub">day${stats.streak.current === 1 ? '' : 's'} · best ${stats.streak.longest}</div></div>
      <div class="tile"><div class="k">Words saved</div><div class="v">${stats.words}</div>
        <div class="sub">${stats.stages.mature} mature</div></div>
      <div class="tile"><div class="k">Due now</div><div class="v">${stats.due}</div>
        <div class="sub">${stats.reviews_today} reviewed today</div></div>
      <div class="tile"><div class="k">Articles read</div><div class="v">${stats.articles_completed}</div>
        <div class="sub">of ${library.articles.length} in the library</div></div>
      <div class="tile"><div class="k">Sentences kept</div><div class="v">${stats.quotes || 0}</div>
        <div class="sub">${(stats.quotes || 0) ? '<a href="#/quotes">your quotes</a>' : 'keep one while reading'}</div></div>
    </div>

    <div class="panel">
      <h3>Reviews, last 30 days</h3>
      <p class="panel-sub">${totalReviews} review${totalReviews === 1 ? '' : 's'} in this window · peak ${peak} in a day</p>
      <div class="chart" id="reviews-chart">
        ${activity.map(d => `<div class="col ${d.reviews ? '' : 'zero'}" title="${d.day}: ${d.reviews} review${d.reviews === 1 ? '' : 's'}">
          <i style="height:${d.reviews ? Math.max(4, Math.round(d.reviews / peak * 100)) : 2}%"></i></div>`).join('')}
      </div>
      <div class="chart-axis"><span>${activity[0] ? activity[0].day.slice(5) : ''}</span>
        <span>today</span></div>
    </div>

    <div class="panel">
      <h3>How well the vocabulary has stuck</h3>
      <p class="panel-sub">Spaced repetition stages — a word graduates to <em>mature</em> after three weeks of intervals</p>
      <div class="stack">
        ${stageOrder.map(([key, , color]) => stats.stages[key]
          ? `<span style="background:${color};width:${(stats.stages[key] / stageTotal * 100).toFixed(1)}%"
                   title="${key}: ${stats.stages[key]}"></span>` : '').join('')}
      </div>
      <div class="legend">
        ${stageOrder.map(([key, label, color]) => `<span class="item">
          <span class="swatch" style="background:${color}"></span>${label}
          <span class="n">${stats.stages[key]}</span></span>`).join('')}
      </div>
    </div>

    <div class="panel">
      <h3>Words</h3>
      <p class="panel-sub">${words.words.length} saved${stats.reviews_total ? ` · ${stats.reviews_total} reviews all time` : ''}
        · <a href="#/cards" style="color:var(--accent-ink)">see every card</a>
        · <a href="/api/export.csv" style="color:var(--accent-ink)">export for Anki</a></p>
      ${words.words.length ? `<p class="small muted">Most recent:
        ${words.words.slice(0, 6).map(w => `<span class="chip es">${escapeHtml(w.term)}</span>`).join(' ')}</p>`
        : '<p class="muted small">Nothing saved yet. Click any Spanish word while reading.</p>'}
    </div>

    <div class="panel danger-zone">
      <h3>Start over</h3>
      <p class="panel-sub">Clears the deck, the schedule, your reading positions, the sentences you kept,
        your writing, and all of the history measured from them. <strong>Your lessons stay</strong> —
        they are files, and this does not touch files.</p>
      <div class="row">
        <button class="btn ghost sm danger-hover" id="btn-reset">Reset all records and progress</button>
        <span class="small muted">A copy of the database is saved to <code>data/backups</code> first.</span>
      </div>
    </div>`;

  // Loaded after the resting view is painted: none of them blocks another, and
  // the page is readable while the graph is still being laid out.
  renderChallenge();
  renderReadingAnalytics($('#reading-analytics'));
  renderCorpusMap($('#corpus-map-panel'));
  $('#btn-reset').addEventListener('click', () => openReset(stats));
}

/* The one button that can lose months of work, so it explains itself before it
   does anything: what goes, what stays, and where the copy of the database went.
   The list is read from the server rather than written here, because a description
   of what a reset clears is exactly the kind of text that goes stale when the
   tables change. */
function openReset(stats) {
  openModal('Start over?', `
    <p style="margin-top:0">This clears everything the app has measured about you:</p>
    <ul class="reset-list">
      <li>your saved words and their review schedule — <strong>${stats.words}</strong> cards</li>
      <li>every review, every word lookup, and the reading history behind your streak</li>
      <li>where you are in each article, and which ones you finished</li>
      <li>the sentences you kept, your writing, your weekly goal</li>
    </ul>
    <p>It keeps <strong>your lessons</strong>: the corpus, and everything in your library folder.
      Files are not touched by this.</p>
    <p class="small muted">A copy of the database is written to <code>data/backups</code> first, so this
      is recoverable — but only by hand, from that file.</p>
    <div class="row">
      <button class="btn danger" id="reset-go">Clear all records and progress</button>
      <button class="btn ghost" id="reset-cancel">Cancel</button>
    </div>`);
  $('#reset-cancel').addEventListener('click', closeModal);
  $('#reset-go').addEventListener('click', async () => {
    const button = $('#reset-go');
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span> clearing';
    try {
      const result = await api('/api/reset', { method: 'POST' });
      closeModal();
      toast(`Cleared ${result.cleared.words} words and ${result.cleared.reviews} reviews — `
        + `lessons untouched, backup in data/backups`);
      refreshDuePill();
      viewStats();
    } catch (err) {
      button.disabled = false;
      button.textContent = 'Clear all records and progress';
      toast(err.message, 'err');
    }
  });
}

/* -- recommendations ------------------------------------------------------- */

/* -- recommendations ------------------------------------------------------- */

function renderRecommendations(host, result) {
  const basis = result.basis || {};
  const candidates = (result.candidates || []).filter(c => !c.error);
  const topics = result.topics || [];

  const topicList = topics.length
    ? `<div class="small muted" style="margin-bottom:12px">Looking for:
        ${topics.map(t => `<span class="chip" title="${escapeHtml(t.why)}">${escapeHtml(t.query)}</span>`).join(' ')}</div>`
    : '';
  const basisLine = `<div class="small muted" style="margin-bottom:12px">${escapeHtml(basis.note || '')}
    ${basis.words ? ` · matching against ${basis.words} saved word${basis.words === 1 ? '' : 's'}` : ''}</div>`;

  if (!candidates.length) {
    host.innerHTML = `${topicList}${basisLine}
      <p class="small muted">Nothing came back. Try importing a link directly instead.</p>`;
    return;
  }

  const best = candidates[0];
  host.innerHTML = `${topicList}${basisLine}
    ${basis.words ? `<div class="verdict correct" style="margin-bottom:12px">
      <div class="headline">Best match: ${Math.round(best.score * 100)}% of your words are likely to appear</div>
      <div class="small">${best.hits.slice(0, 14).map(escapeHtml).join(' · ')}</div>
    </div>` : ''}
    <div class="results">
      ${candidates.map(c => `
        <div class="result" data-url="${escapeHtml(c.url)}" data-title="${escapeHtml(c.title)}">
          <div class="t">${escapeHtml(c.title)}</div>
          <div class="s">${escapeHtml((c.snippet || '').slice(0, 210))}</div>
          <div class="u">
            ${c.words ? `${c.words} words · ` : ''}${escapeHtml(c.site)}
            ${c.hits && c.hits.length ? ` · <span style="color:var(--accent-ink)">reuses ${c.hits.length}: ${escapeHtml(c.hits.slice(0, 6).join(', '))}</span>` : ''}
          </div>
        </div>`).join('')}
    </div>
    <p class="small muted" style="margin-top:12px">Pick one to weave into Spanish — it takes a few minutes.</p>`;

  $$('.result', host).forEach(el => el.addEventListener('click', () => {
    closeModal();
    openImport(el.dataset.url, el.dataset.title);
  }));
}

/* -- import and discover --------------------------------------------------- */

/* The three dials that decide what an imported lesson is like: what kind of
   Spanish, how much of it, and what unit it arrives in. All three are remembered,
   because a learner reading at a particular level wants the next article the same
   way. */
let importOptions = null;
const importChoice = {
  level: store.get('importLevel', 'auto'),
  ratio: store.get('importRatio', null),
  weave: store.get('importWeave', 'chunk'),
};

async function loadImportOptions() {
  if (importOptions) return importOptions;
  importOptions = await api('/api/import/options');
  return importOptions;
}

function importControlsHtml(options) {
  const selected = importChoice.level || options.default_level;
  const preset = options.amounts.find(a => Math.abs(a.ratio - (importChoice.ratio ?? -1)) < 0.001);
  const ratio = importChoice.ratio ?? (options.levels.find(l => l.code === selected) || {}).suggested_ratio
    ?? 0.38;
  return `
    <div class="field">
      <label>What level should the Spanish be?</label>
      <div class="chips" id="level-chips">
        ${options.levels.map(level => `
          <button type="button" class="choice ${level.code === selected ? 'active' : ''}"
                  data-level="${escapeHtml(level.code)}" title="${escapeHtml(level.grammar)}">
            <span class="choice-code">${escapeHtml(level.code === 'auto' ? 'Auto' : level.code)}</span>
            <span class="choice-name">${escapeHtml(level.name)}</span>
          </button>`).join('')}
      </div>
    </div>
    <div class="field">
      <label for="import-ratio">How much Spanish?
        <span class="ratio-value" id="ratio-value">${Math.round(ratio * 100)}%</span></label>
      <input type="range" id="import-ratio" min="${Math.round(options.min_ratio * 100)}"
             max="${Math.round(options.max_ratio * 100)}" step="1" value="${Math.round(ratio * 100)}">
      <div class="chips tight" id="amount-chips">
        ${options.amounts.map(a => `
          <button type="button" class="choice sm ${preset && preset.label === a.label ? 'active' : ''}"
                  data-amount="${a.ratio}" title="${escapeHtml(a.note)}">${escapeHtml(a.label)}</button>`).join('')}
      </div>
    </div>
    ${weaveControlsHtml(options)}
    <p class="import-preview" id="import-preview"></p>`;
}

/* The grain of the mixture: whether the two languages may share a sentence.

   Given its own control with the worked example shown, because the difference is
   easier to see than to describe -- and because a reader who has only met mixed
   lessons may not know that the other form exists. */
function weaveControlsHtml(options) {
  const weaves = options.weaves || [];
  if (weaves.length < 2) return '';
  const chosen = weaves.find(w => w.code === importChoice.weave) || weaves[0];
  return `
    <div class="field">
      <label>How should the two languages mix?</label>
      <div class="chips" id="weave-chips">
        ${weaves.map(weave => `
          <button type="button" class="choice ${weave.code === chosen.code ? 'active' : ''}"
                  data-weave="${escapeHtml(weave.code)}" title="${escapeHtml(weave.blurb)}">
            <span class="choice-name">${escapeHtml(weave.name)}</span>
          </button>`).join('')}
      </div>
      <p class="small muted" id="weave-blurb" style="margin:8px 0 6px">${escapeHtml(chosen.blurb)}</p>
      <pre class="weave-example" id="weave-example">${escapeHtml(chosen.example)}</pre>
    </div>`;
}

function wireImportControls(onChange, extraBody = {}) {
  const host = $('#modal-body');
  const refresh = async () => {
    try {
      const preview = await api('/api/import/preview', {
        method: 'POST',
        body: { level: importChoice.level, ratio: importChoice.ratio, weave: importChoice.weave,
                ...extraBody },
      });
      const box = $('#import-preview');
      if (box) box.textContent = preview.description;
      const label = $('#ratio-value');
      if (label) label.textContent = `${Math.round(preview.ratio * 100)}%`;
      const slider = $('#import-ratio');
      if (slider) slider.value = Math.round(preview.ratio * 100);
    } catch { /* the preview is a nicety; the import still works */ }
    if (onChange) onChange();
  };

  $$('[data-level]', host).forEach(button => button.addEventListener('click', () => {
    importChoice.level = button.dataset.level;
    store.set('importLevel', importChoice.level);
    // A level carries a sensible default amount; offering it is more useful
    // than leaving the previous level's number in place.
    const suggested = (importOptions.levels.find(l => l.code === importChoice.level) || {}).suggested_ratio;
    if (suggested) { importChoice.ratio = suggested; store.set('importRatio', suggested); }
    $$('[data-level]', host).forEach(b => b.classList.toggle('active', b.dataset.level === importChoice.level));
    $$('[data-amount]', host).forEach(b => b.classList.remove('active'));
    refresh();
  }));

  $$('[data-amount]', host).forEach(button => button.addEventListener('click', () => {
    importChoice.ratio = parseFloat(button.dataset.amount);
    store.set('importRatio', importChoice.ratio);
    $$('[data-amount]', host).forEach(b => b.classList.toggle('active', b === button));
    refresh();
  }));

  // The grain does not change the amount, so it only repaints its own note and
  // the preview caption -- no need to ask the server what the amount now means.
  $$('[data-weave]', host).forEach(button => button.addEventListener('click', () => {
    importChoice.weave = button.dataset.weave;
    store.set('importWeave', importChoice.weave);
    const chosen = (importOptions.weaves || []).find(w => w.code === importChoice.weave);
    $$('[data-weave]', host).forEach(b => b.classList.toggle('active', b === button));
    const blurb = $('#weave-blurb');
    if (blurb && chosen) blurb.textContent = chosen.blurb;
    const example = $('#weave-example');
    if (example && chosen) example.textContent = chosen.example;
    refresh();
  }));

  const slider = $('#import-ratio');
  if (slider) {
    slider.addEventListener('input', () => {
      importChoice.ratio = Number(slider.value) / 100;
      store.set('importRatio', importChoice.ratio);
      const label = $('#ratio-value');
      if (label) label.textContent = `${slider.value}%`;
      $$('[data-amount]', host).forEach(b => b.classList.remove('active'));
    });
    slider.addEventListener('change', refresh);
  }
  refresh();
}

/* Two ways in, one lesson out.

   A link is the easy case: the app fetches the page and reads the article out of
   it. But some pages cannot be fetched at all -- paywalls, readers that build
   themselves in JavaScript, hosts this machine cannot reach -- and the reader can
   still read them and copy them out. So the paste is offered beside the link
   rather than as a fallback buried behind a failure, and the settings below it
   are the same controls either way: a pasted article is a lesson like any other,
   not a reduced one.

   Both blocks are in the DOM at once and one is hidden, rather than re-rendering
   on every switch, so a half-typed URL survives a look at the other tab. */
let importMode = store.get('importMode', 'link');

function importSourceHtml(prefill, titleHint) {
  const link = importMode === 'link';
  return `
    <p class="small muted" style="margin-top:0" id="import-lede"></p>
    <div class="chips" id="import-modes">
      <button type="button" class="choice sm ${link ? 'active' : ''}" data-way="link">From a link</button>
      <button type="button" class="choice sm ${link ? '' : 'active'}" data-way="paste">Paste the text</button>
    </div>
    <div class="field" id="import-link-fields" ${link ? '' : 'hidden'}>
      <label for="import-url">Article URL</label>
      <input type="url" id="import-url" placeholder="https://…" value="${escapeHtml(prefill)}">
    </div>
    <div class="field" id="import-paste-fields" ${link ? 'hidden' : ''}>
      <label for="import-text">Article text</label>
      <textarea id="import-text" rows="9"
                placeholder="Select the article in your browser, copy it, and paste it here — headline and all."></textarea>
      <p class="small muted" style="margin:6px 0 0">Leave the blank lines between paragraphs: they are what
      tells the app where one paragraph ends. For a page the app cannot reach at all — a paywall, a reader
      that needs JavaScript — this is the way in.</p>
    </div>
    <div class="field">
      <label for="import-title">Title <span class="muted" id="import-title-note"></span></label>
      <input type="text" id="import-title" value="${escapeHtml(titleHint)}">
    </div>
    <div class="field" id="import-source-field" ${link ? 'hidden' : ''}>
      <label for="import-source-url">Original URL (optional)</label>
      <input type="url" id="import-source-url" placeholder="https://… — kept with the lesson for reference">
    </div>`;
}

function applyImportMode(mode) {
  importMode = mode;
  store.set('importMode', mode);
  $$('[data-way]').forEach(b => b.classList.toggle('active', b.dataset.way === mode));
  const link = mode === 'link';
  $('#import-link-fields').hidden = !link;
  $('#import-paste-fields').hidden = link;
  $('#import-source-field').hidden = link;
  const lede = $('#import-lede');
  if (lede) {
    lede.textContent = link
      ? 'The page is fetched, woven into Spanish, and given post-reading notes — then it joins your '
        + 'library like any other text. That takes a few minutes and runs in the background, so you '
        + 'can close this and keep reading.'
      : 'What you paste is woven into Spanish and given post-reading notes, with no fetching at all — '
        + 'so this works for a page the app cannot reach. It takes a few minutes and runs in the '
        + 'background, so you can close this and keep reading.';
  }
  const note = $('#import-title-note');
  if (note) {
    note.textContent = link
      ? '(optional — only if the page title comes out wrong)'
      : '(optional — the headline, if it is not the first line)';
  }
  const focus = $(link ? '#import-url' : '#import-text');
  if (focus && !focus.value) focus.focus();
}

async function openImport(prefill = '', titleHint = '') {
  openModal('Import', '<div id="import-form"><div class="empty"><span class="spinner"></span></div></div>');
  let options;
  try {
    options = await loadImportOptions();
  } catch (err) {
    $('#import-form').innerHTML = `<p class="small" style="color:var(--bad)">${escapeHtml(err.message)}</p>`;
    return;
  }

  // Arriving from a search result means there is already a URL, so the link form
  // is what this reader wants regardless of what they chose last time.
  if (prefill) importMode = 'link';

  $('#import-form').innerHTML = `
    ${importSourceHtml(prefill, titleHint)}
    ${importControlsHtml(options)}
    <div class="row"><button class="btn primary" id="import-go">Weave it into Spanish</button>
      <span class="small muted">Best with long-form prose: essays, Wikipedia, magazine features.</span></div>
    <div id="import-job"></div>`;

  wireImportControls();
  applyImportMode(importMode);
  $$('[data-way]').forEach(button => button.addEventListener('click',
    () => applyImportMode(button.dataset.way)));

  $('#import-go').addEventListener('click', async () => {
    const link = importMode === 'link';
    const url = link ? $('#import-url').value.trim() : '';
    const text = link ? '' : $('#import-text').value.trim();
    if (link && !url) { toast('Paste a URL first, or switch to pasting the text'); return; }
    if (!link && !text) { toast('Paste the article text first'); return; }
    const button = $('#import-go');
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span> queueing';
    try {
      const response = await api('/api/import', {
        method: 'POST',
        body: {
          url: url || null,
          text: text || null,
          source_url: link ? null : ($('#import-source-url').value.trim() || null),
          title_hint: $('#import-title').value.trim() || null,
          level: importChoice.level,
          ratio: importChoice.ratio,
          weave: importChoice.weave,
        },
      });
      $('#import-job').innerHTML = `<div data-job-detail="${response.job_id}" style="margin-top:16px"></div>`;
      const job = await api(`/api/jobs/${response.job_id}`);
      updateJob(job);
      button.textContent = 'Importing — you can close this';
      button.classList.remove('primary');
      button.disabled = true;
    } catch (err) {
      toast(err.message, 'err');
      button.disabled = false;
      button.textContent = 'Weave it into Spanish';
    }
  });
}

function renderRecommendResult(jobId) {
  const host = $('#recommend-result');
  const job = jobState.get(jobId);
  if (!host || !job || !job.result) return;
  if (host.dataset.rendered === jobId) return;
  host.dataset.rendered = jobId;
  renderRecommendations(host, job.result);
}

/* -- settings -------------------------------------------------------------- */

/* Where the model key lives, for anyone who is running this without a terminal.

   The values the reader sets here are written to `data/settings.json` and win over
   the `.env`, and the models are rebuilt the moment they are saved -- so pasting a
   key takes effect there and then. The key itself is never sent back: the panel is
   told whether one is set and a four-character hint, which is enough to recognise
   which key it is and useless to anyone reading over a shoulder. */
async function openSettings() {
  let current;
  try {
    current = await api('/api/settings');
  } catch (err) {
    toast(err.message, 'err');
    return;
  }
  const llm = current.llm || {};
  const judge = current.judge || {};

  openModal('Settings', `
    <p class="small muted" style="margin-top:0">The model is what turns an article into a lesson: weaving,
    glosses, quizzes and exercises all go through it. Everything else — reading, saving words, review —
    works without one.${llm.from === 'env' ? ' These are currently coming from your <code>.env</code>;'
      + ' anything you set here takes over from it.' : ''}</p>
    <div class="field">
      <label for="set-base-url">API base URL</label>
      <input type="text" id="set-base-url" autocapitalize="off" autocorrect="off" spellcheck="false"
             placeholder="https://api.example.com/v1" value="${escapeHtml(llm.base_url || '')}">
    </div>
    <div class="field">
      <label for="set-model">Model</label>
      <input type="text" id="set-model" autocapitalize="off" autocorrect="off" spellcheck="false"
             placeholder="the model name your endpoint expects" value="${escapeHtml(llm.model || '')}">
    </div>
    <div class="field">
      <label for="set-key">API key
        <span class="muted">${llm.key_set
          ? `— ${escapeHtml(llm.key_hint || 'set')} is in use${llm.from ? ` (from your ${llm.from === 'app' ? 'settings' : '.env'})` : ''}`
          : '— none set'}</span></label>
      <input type="password" id="set-key" autocapitalize="off" autocorrect="off" spellcheck="false"
             placeholder="${llm.key_set ? 'leave blank to keep the one you have' : 'paste your key'}">
      <p class="small muted" style="margin:6px 0 0">Stored in <code>data/settings.json</code> on this machine,
      next to your library — never sent anywhere except your own endpoint, and never back to this page.</p>
    </div>
    <div class="settings-state" id="settings-state">
      ${llm.ready
        ? `<span class="chip ok">model ready</span> ${escapeHtml(llm.model || '')}`
        : '<span class="chip warn">no model configured</span> searching and reading still work'}
      ${judge.ready ? '<span class="chip">graded by Jev</span>' : ''}
    </div>
    <div class="row" style="margin-top:14px">
      <button class="btn primary" id="settings-save">Save</button>
      <button class="btn ghost" id="settings-clear">Forget the key</button>
      <span class="small muted" id="settings-note"></span>
    </div>
    <p class="small muted" style="margin-bottom:0">Advanced: the TypeSafe key for graded exercises is read
    from <code>${escapeHtml(current.env_file || '.env')}</code>, and left alone here${judge.ready ? '' : ' (it is not set)'}.</p>`);

  const send = async (body, button) => {
    const label = button.textContent;
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span> saving';
    try {
      const fresh = await api('/api/settings', { method: 'POST', body });
      closeModal();
      toast(fresh.llm.ready
        ? `Model ready — ${fresh.llm.model}`
        : 'Saved, but the model still needs a base URL, a key and a model name');
      // The import dialog and Discover read the app's status, so it is refreshed
      // rather than assumed -- a key that just started working should stop them
      // saying that no model is configured.
      refreshDuePill();
    } catch (err) {
      button.disabled = false;
      button.textContent = label;
      toast(err.message, 'err');
    }
  };

  $('#settings-save').addEventListener('click', () => {
    // A blank key field means "keep the one that is set"; a blank base URL or
    // model means "clear it and fall back to the .env". Sending the key only when
    // it was typed keeps a password field that the browser autofilled empty from
    // wiping a working key.
    const body = {
      llm_base_url: $('#set-base-url').value.trim(),
      llm_model: $('#set-model').value.trim(),
    };
    const typed = $('#set-key').value.trim();
    if (typed) body.llm_api_key = typed;
    send(body, $('#settings-save'));
  });
  $('#settings-clear').addEventListener('click', () =>
    send({ llm_api_key: '', llm_base_url: '', llm_model: '' }, $('#settings-clear')));
}

/* -- sharing a lesson ------------------------------------------------------ */

/* A lesson leaves as the author's own `.md` file with a few extra front-matter
   fields: the format, who sent it, how it was made, and a digest of the lesson.
   The reader's name is asked for once and remembered, because a file that says
   who sent it is the difference between a recommendation and an anonymous
   document -- and because the recipient is being asked to trust a stranger's
   Spanish. */
function downloadShare(slug) {
  const name = ($('#share-name').value || '').trim();
  store.set('shareName', name);
  const url = `/api/transfer/export/${encodeURIComponent(slug)}?creator=${encodeURIComponent(name)}`;
  const link = document.createElement('a');
  link.href = url;
  link.download = '';
  document.body.appendChild(link);
  link.click();
  link.remove();
  toast('Saved a file you can send to anyone');
}

function openSharedLesson() {
  openModal('Open a shared lesson', `
    <p class="small muted" style="margin-top:0">A diglot lesson someone sent you, as a
    <code>.md</code> file or pasted text. Nothing is added to your library until you have
    seen what it teaches and how much of it you already know.</p>
    <div class="field">
      <label for="share-file">Choose a file</label>
      <input type="file" id="share-file" accept=".md,.markdown,text/markdown,text/plain">
    </div>
    <div class="field">
      <label for="share-paste">…or paste it here</label>
      <textarea id="share-paste" rows="5" placeholder="### Article Identification & Preview"></textarea>
    </div>
    <div class="row"><button class="btn primary" id="share-look">Look at it</button>
      <span class="small muted">No model is called, so this is instant.</span></div>
    <div id="share-report"></div>`);

  $('#share-file').addEventListener('change', (event) => {
    const chosen = event.target.files && event.target.files[0];
    if (!chosen) return;
    const reader = new FileReader();
    reader.onload = () => { $('#share-paste').value = String(reader.result || ''); };
    reader.onerror = () => toast('Could not read that file', 'err');
    reader.readAsText(chosen);
  });
  $('#share-look').addEventListener('click', lookAtShared);
}

async function lookAtShared() {
  const text = $('#share-paste').value;
  const host = $('#share-report');
  if (!text.trim()) { toast('Choose a file or paste a lesson first'); return; }
  host.innerHTML = '<div class="empty"><span class="spinner"></span></div>';
  try {
    const report = await api('/api/transfer/inspect', { method: 'POST', body: { text } });
    host.innerHTML = shareReport(report);
    const accept = $('#share-accept');
    if (accept) accept.addEventListener('click', () => acceptShared(text, report));
  } catch (err) {
    host.innerHTML = `<div class="empty"><h3>Could not read that</h3><p>${escapeHtml(err.message)}</p></div>`;
  }
}

/* What the recipient needs before saying yes. Provenance first, because the
   question is whether to trust the Spanish; then the lesson's own syllabus
   against the deck, because a lesson you already know is not worth importing;
   then the structural warnings, which are advisory rather than blocking. */
function shareReport(report) {
  if (!report.ok) {
    return `<div class="panel"><h3>That is not a diglot lesson</h3>
      <p class="panel-sub">${escapeHtml(report.error || 'It could not be read.')}</p>
      <p class="small muted">A lesson is Markdown with an "Article Identification &amp; Preview"
      front matter block, the woven text, and a post-reading section. Anything else — a plain
      article, a web page, a screenshot — needs importing by link instead.</p></div>`;
  }

  const stats = report.stats || {};
  const share = report.share || {};
  const known = report.known || {};
  const percent = Math.round((stats.spanish_ratio || 0) * 100);

  const provenance = [];
  if (share.creator) provenance.push(`from <strong>${escapeHtml(share.creator)}</strong>`);
  if (share.created) provenance.push(`on ${escapeHtml(share.created)}`);
  if (share.origin) provenance.push(escapeHtml(share.origin));
  const provenanceLine = provenance.length
    ? `Sent ${provenance.join(' ')}.`
    : 'No stamp on this file, so there is nothing recorded about where it came from.';

  const warnings = [];
  if (report.intact === false) {
    warnings.push(`<div class="share-warning">The text has changed since this file was
      exported, so the digest no longer matches. That happens when someone fixes a typo by
      hand — it is not a reason to refuse it, just something to know.</div>`);
  }
  if (report.intact === null) {
    warnings.push(`<div class="share-warning subtle">No digest in this file, so there is no
      way to tell whether it was edited after it was made.</div>`);
  }
  if (report.foreign_format) {
    warnings.push(`<div class="share-warning">Written in format
      <code>${escapeHtml(share.format)}</code>, which this app does not know. Readable now,
      but a future version may mean something different by it.</div>`);
  }
  if (report.duplicate) {
    warnings.push(`<div class="panel duplicate">
      <h3>You already have this${report.duplicate.match === 'same source' ? ' source' : ''}</h3>
      <p class="panel-sub">It matches <a href="#/read/${encodeURIComponent(report.duplicate.slug)}">${escapeHtml(report.duplicate.title)}</a>
      (${escapeHtml(report.duplicate.match)}). Importing will add a second copy rather than
      replacing the one you have.</p></div>`);
  }

  const taught = known && known.total
    ? `<p class="panel-sub">Of the <strong>${known.total}</strong> phrases this lesson bolds,
        <strong>${known.known || 0}</strong> are already in your deck
        (${Math.round((known.ratio || 0) * 100)}%).</p>`
    : '';

  return `<div class="panel share-panel">
    <h3>${escapeHtml(report.title || 'Untitled')}</h3>
    <p class="panel-sub">${provenanceLine}</p>
    <div class="share-facts">
      <span>${stats.words || 0} words</span>
      <span>${percent}% Spanish</span>
      <span>${stats.paragraphs || 0} paragraphs</span>
      ${report.register ? `<span>${escapeHtml(REGISTER_LABELS[report.register] || report.register)}</span>` : ''}
      ${report.level ? `<span>level ${escapeHtml(report.level)}</span>` : ''}
      ${report.anchor_count ? `<span>${report.anchor_count} vocabulary notes</span>` : ''}
      ${report.grammar_count ? `<span>${report.grammar_count} grammar notes</span>` : ''}
    </div>
    ${report.author ? `<p class="small muted">By ${escapeHtml(report.author)}${report.url ? ` · <a href="${escapeHtml(report.url)}" target="_blank" rel="noopener">original</a>` : ''}</p>` : ''}
    ${report.preview ? `<p class="share-preview">${escapeHtml(report.preview)}</p>` : ''}
    ${(report.teaches || []).length ? `<div class="share-teaches"><div class="small muted">It teaches</div>
      <div class="row">${report.teaches.slice(0, 14).map(t => `<span class="chip">${escapeHtml(t.es)}${t.en ? ` <span class="muted">${escapeHtml(t.en)}</span>` : ''}</span>`).join('')}</div></div>` : ''}
    ${taught}
    ${warnings.join('')}
    <div class="row share-actions">
      <button class="btn primary" id="share-accept">Add to my library</button>
      <span class="small muted">Saved as <code>${escapeHtml(report.suggested_filename || '')}</code></span>
    </div>
  </div>`;
}

async function acceptShared(text, report) {
  const button = $('#share-accept');
  button.disabled = true;
  try {
    const result = await api('/api/transfer/import', { method: 'POST', body: { text } });
    closeModal();
    toast(`Added “${result.title}” to your library`);
    location.hash = `#/read/${encodeURIComponent(result.slug)}`;
  } catch (err) {
    button.disabled = false;
    toast(err.message, 'err');
  }
}

/* What the last search showed, so "show different results" can ask for what
   comes after it rather than the same list again. */
let discoverState = { query: '', shown: [], rows: [] };

function openDiscover() {
  // Searching needs nothing but the network; weaving a lesson out of a result
  // needs a model. Saying which of the two is missing, before the reader picks
  // something, beats letting them choose an article and then refusing.
  const canWeave = !gradingInfo || gradingInfo.generates !== false;
  openModal('Find something to read', `
    <p class="small muted" style="margin-top:0">Search Wikipedia and a set of long-form feeds for an article
    worth reading, then import whichever one you pick.</p>
    ${canWeave ? '' : `<div class="grading-note">No model is configured, so results can be
      found but not turned into lessons yet. Set <code>LLM_BASE_URL</code>,
      <code>LLM_API_KEY</code> and <code>LLM_MODEL</code> in <a href="#" id="discover-settings">Settings</a>
      to import them.</div>`}
    <div class="field">
      <label for="discover-q">What do you want to read about?</label>
      <input type="search" id="discover-q" placeholder="e.g. creativity and artificial intelligence">
    </div>
    <div class="row"><button class="btn primary" id="discover-go">Search</button>
      <span class="small muted">Wikipedia gives the most reliable results.</span></div>
    <div id="discover-gaps"></div>
    <div class="panel-inset" id="discover-recommend">
      <h4>From the words you have saved</h4>
      <p class="small muted">The other way round from a search: this reads your deck, works out which
        topics would bring those words back, finds articles on them, then <em>fetches each candidate</em>
        to count how many of your words actually appear in it. The match count is measured, not guessed.</p>
      <div class="row"><button class="btn" id="discover-recommend-go">Find something for my words</button>
        <span class="small muted">Takes a few minutes; it runs in the background.</span></div>
      <div id="recommend-job"></div>
      <div id="recommend-result"></div>
    </div>
    <div id="discover-results" class="results"></div>`);

  const run = async (query, { append = false } = {}) => {
    const host = $('#discover-results');
    const asked = (query || $('#discover-q').value).trim();
    if (!asked) return;
    // A new question starts a new list; asking for different results of the same
    // question continues the one on screen.
    if (!append || asked !== discoverState.query) {
      discoverState = { query: asked, shown: [], rows: [] };
    }
    $('#discover-q').value = asked;
    if (!append) host.innerHTML = '<div class="loading"><span class="spinner"></span> searching</div>';
    const button = $('#discover-more');
    if (button) { button.disabled = true; button.innerHTML = '<span class="spinner"></span> looking'; }
    try {
      const data = await api('/api/discover', {
        method: 'POST', body: { query: asked, exclude: discoverState.shown },
      });
      discoverState.rows = discoverState.rows.concat(data.results);
      discoverState.shown = discoverState.rows.map(row => row.url);
      if (!discoverState.rows.length) {
        host.innerHTML = '<p class="muted small">Nothing found. Try different words, or paste a link directly.</p>';
        return;
      }
      host.innerHTML = `<div class="count-line">${discoverState.rows.length} result${
        discoverState.rows.length === 1 ? '' : 's'}</div>`
        + discoverState.rows.map(discoverResult).join('')
        + discoverMoreHtml(data.more, discoverState.query);
      $$('.result', host).forEach(el => el.addEventListener('click', () => {
        if (el.dataset.have) return;          // already in the library: nothing to import
        closeModal();
        openImport(el.dataset.url, el.dataset.title);
      }));
      const more = $('#discover-more');
      if (more) more.addEventListener('click', () => run(discoverState.query, { append: true }));
    } catch (err) {
      host.innerHTML = `<p class="small" style="color:var(--bad)">${escapeHtml(err.message)}</p>`;
    }
  };

  $('#discover-go').addEventListener('click', () => run());
  $('#discover-q').addEventListener('keydown', (e) => { if (e.key === 'Enter') run(); });
  $('#discover-q').focus();
  loadGaps((query) => run(query));

  // "What next?" lives here rather than in the top bar: searching for something
  // to read has two ways in -- a topic you name, and the words you already have --
  // and they belong in one place rather than in two dialogs a reader has to know
  // the difference between.
  const settingsLink = $('#discover-settings');
  if (settingsLink) settingsLink.addEventListener('click', (event) => {
    event.preventDefault();
    openSettings();
  });

  $('#discover-recommend-go').addEventListener('click', async () => {
    const button = $('#discover-recommend-go');
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span> reading your deck';
    try {
      const response = await api('/api/recommend', { method: 'POST' });
      $('#recommend-job').innerHTML = `<div data-job-detail="${response.job_id}" style="margin-top:12px"></div>`;
      const job = await api(`/api/jobs/${response.job_id}`);
      updateJob(job);
      button.textContent = 'Looking — you can close this';
    } catch (err) {
      button.disabled = false;
      button.textContent = 'Find something for my words';
      toast(err.message, 'err');
    }
  });
}

/* "Show different results", not "refresh".

   Refresh means "the same thing again", and the same thing again is exactly what
   the reader does not want -- the sources are deterministic, so re-running the
   query returns the identical list. What the button actually does is exclude what
   has been shown and ask for what comes next, and when there is nothing next it
   says so rather than offering a button that cannot do anything. */
function discoverMoreHtml(more, query) {
  if (more) {
    return `<div class="row discover-more">
      <button class="btn ghost sm" id="discover-more"
              title="Ask for the results beyond these — the same search has more to give">Show different results</button>
    </div>`;
  }
  // "right now" is doing real work: the feeds move and a search engine that
  // answered this time may not answer the next, so the supply is not fixed and
  // promising "everything there is" would be a claim the app cannot make.
  return `<p class="small muted discover-more">That is everything those sources have for
    “${escapeHtml(query)}” right now. Different words will find different articles.</p>`;
}

/* One search result. Two things change which result is worth clicking, and both
   come from what the app already knows: whether the reader has it, and what
   register the source implies. */
function discoverResult(row) {
  const have = row.already_have
    ? `<span class="chip accent" title="You already have this lesson">in your library</span>` : '';
  const register = row.register_label
    ? `<span class="chip" title="Judged from the source, not the text">${escapeHtml(row.register_label)}</span>` : '';
  return `<div class="result ${row.already_have ? 'have' : ''}" data-url="${escapeHtml(row.url)}"
       data-title="${escapeHtml(row.title)}" data-have="${row.already_have ? '1' : ''}">
    <div class="t">${escapeHtml(row.title)}</div>
    <div class="s">${escapeHtml(row.snippet || '')}</div>
    <div class="u">${escapeHtml(row.source)} · ${escapeHtml(row.site)} ${have}${register}</div>
  </div>`;
}

/* What the reader is not reading.

   A register gap is invisible from inside the app: you cannot miss what you
   never look at. So the library counts it, and this offers two ways in -- the
   unread lessons already on the shelf, which cost nothing, or a search phrased
   around a subject the reader has actually chosen before. */
async function loadGaps(run) {
  const host = $('#discover-gaps');
  if (!host) return;
  let data;
  try {
    data = await api('/api/discover/gaps');
  } catch { return; }   // a suggestion is not worth an error
  if (!data.gaps || !data.gaps.length) return;

  host.innerHTML = `<div class="gaps">
    <div class="small muted">You have never read</div>
    ${data.gaps.map(gap => `
      <div class="gap" data-register="${escapeHtml(gap.register)}">
        <div class="gap-head">
          <strong>${escapeHtml(gap.label)}</strong>
          <span class="small muted">${gap.available} on your shelves, none opened</span>
        </div>
        <p class="small muted">${escapeHtml(gap.blurb || '')}</p>
        ${gap.unread.length ? `<div class="row">
          ${gap.unread.map(item => `<a class="btn ghost sm" href="#/read/${encodeURIComponent(item.slug)}"
              title="${escapeHtml(item.title)}">${escapeHtml(item.title.length > 38 ? item.title.slice(0, 38) + '…' : item.title)}</a>`).join('')}
        </div>` : ''}
        ${gap.query ? `<div class="row" style="margin-top:6px">
          <button class="btn ghost sm" data-search="${escapeHtml(gap.query)}">Search the web for “${escapeHtml(gap.topic)}”</button>
        </div>` : ''}
      </div>`).join('')}
  </div>`;

  $$('[data-search]', host).forEach(button => button.addEventListener('click', () => run(button.dataset.search)));
  // A gap suggestion opens the article, so the dialog has to get out of the way.
  $$('a.btn', host).forEach(link => link.addEventListener('click', closeModal));
}

/* =============================================================== router === */

async function route() {
  closePopover();
  window.scrollTo({ top: 0 });
  const hash = location.hash || '#/library';
  const [, path, param] = hash.match(/^#\/([^/]*)\/?(.*)$/) || [, 'library', ''];
  try {
    if (path === 'read' && param) { await viewReader(decodeURIComponent(param)); return; }
    if (path === 'review') { await viewReview(); return; }
    if (path === 'cards') { await viewCards(); return; }
    if (path === 'quotes') { await viewQuotes(); return; }
    if (path === 'write') { await viewWrite(param ? Number(param) : null); return; }
    if (path === 'stats') { await viewStats(); return; }
    await viewLibrary();
  } catch (err) {
    $('#view').innerHTML = `<div class="empty"><h3>Something went wrong</h3><p>${escapeHtml(err.message)}</p></div>`;
  }
}

window.addEventListener('hashchange', route);

/* --------------------------------------------------------- global keys ---- */

document.addEventListener('keydown', (event) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName);

  if (event.key === 'Escape') {
    if (!$('#modal').hidden) { closeModal(); return; }
    if (!$('#popover').hidden) { closePopover(); return; }
    if (!$('#activity').hidden) { closeActivity(); return; }
    if (reader.active) { stopReading(); return; }
  }
  if (typing) return;

  if (location.hash.startsWith('#/review')) {
    if (event.key === ' ') { event.preventDefault(); revealCard(); return; }
    if (['1', '2', '3', '4'].includes(event.key) && reviewRevealed) {
      event.preventDefault();
      gradeCard(['again', 'hard', 'good', 'easy'][+event.key - 1]);
      return;
    }
    if (event.key.toLowerCase() === 's') {
      const card = reviewQueue[reviewIndex];
      if (card) speak(card.term);
      return;
    }
  }

  if (location.hash.startsWith('#/read/')) {
    // Reading modes are the thing you change most often while reading, so they
    // get single keys rather than a reach for the toolbar.
    const key = event.key.toLowerCase();
    if (key === 'r') { toggleReading(); return; }
    if (key === 'g') { savePref('gloss', !prefs.gloss); toast(prefs.gloss ? 'Glosses on' : 'Glosses off'); return; }
    if (key === 'f') { savePref('dim', !prefs.dim); toast(prefs.dim ? 'Focusing Spanish' : 'English at full strength'); return; }
    if (key === 'e') { savePref('esOnly', !prefs.esOnly); toast(prefs.esOnly ? 'Spanish only' : 'English shown'); return; }
    if (key === 's') {
      const selection = window.getSelection();
      if (selection && selection.toString().trim()) speak(selection.toString().trim());
    }
  }
});

/* ----------------------------------------------------------- reader bar --- */

$('#opt-gloss').addEventListener('change', (e) => savePref('gloss', e.target.checked));
$('#opt-dim').addEventListener('change', (e) => savePref('dim', e.target.checked));
$('#opt-es-only').addEventListener('change', (e) => savePref('esOnly', e.target.checked));
$('#font-up').addEventListener('click', () => savePref('size', Math.min(prefs.size + 1, 26)));
$('#font-down').addEventListener('click', () => savePref('size', Math.max(prefs.size - 1, 15)));
$('#btn-import').addEventListener('click', () => openImport());
$('#btn-discover').addEventListener('click', openDiscover);
$('#btn-settings').addEventListener('click', openSettings);

$('#btn-activity').addEventListener('click', () => {
  if ($('#activity').hidden) openActivity(); else closeActivity();
});
$('#activity-close').addEventListener('click', closeActivity);
$('#activity-clear').addEventListener('click', async () => {
  try {
    await api('/api/jobs', { method: 'DELETE' });
    for (const [id, job] of jobState) {
      if (job.status !== 'running' && job.status !== 'queued') jobState.delete(id);
    }
    renderActivity();
    refreshJobPill();
  } catch (err) { toast(err.message, 'err'); }
});

$('#reader-back').addEventListener('click', async () => {
  const slug = location.hash.replace('#/read/', '');
  await saveProgress(decodeURIComponent(slug), false);
  location.hash = '#/library';
});

$('#btn-read-aloud').addEventListener('click', toggleReading);

// Leaving the reader stops the audio: a voice reading an article you have
// navigated away from is worse than silence.
window.addEventListener('hashchange', () => { if (!location.hash.startsWith('#/read/')) stopReading(); });

window.addEventListener('beforeunload', () => {
  if (location.hash.startsWith('#/read/')) {
    const slug = decodeURIComponent(location.hash.replace('#/read/', ''));
    navigator.sendBeacon?.(`/api/article/${encodeURIComponent(slug)}/progress`,
      new Blob([JSON.stringify({ position: 0 })], { type: 'application/json' }));
  }
});

$$('.theme-switch button').forEach(button =>
  button.addEventListener('click', () => savePref('theme', button.dataset.themeSet)));

/* ------------------------------------------------------------- startup ---- */

applyPrefs();
route();
refreshDuePill();
setInterval(refreshDuePill, 60000);
loadJobs();
connectJobStream();
