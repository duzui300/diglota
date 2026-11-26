/**
 * Writing workspace view: prompt, live measurement, draft autosave, check, annotations, improve, versions, and lesson weaving.
 */

import { $, $$, escapeHtml, toast } from '../lib/dom.js';
import { api } from '../lib/api.js';
import { refreshDuePill, registerRouteCleanup, setActiveTab, store } from '../lib/state.js';
import { targetName, writePlaceholder } from '../lib/language.js';
import { openModal, setLookupPanel, showLookupPanel } from '../components/overlays.js';
import { updateJob } from '../components/jobs.js';
import {
  importChoice,
  importControlsHtml,
  loadImportOptions,
  wireImportControls,
} from '../components/import-controls.js';

let writeState = {
  pieceId: null,
  prompt: null,
  modes: [],
  checked: null,
  marks: [],
  timers: {},
};

function draftKey(pieceId) {
  return `writeDraft:${pieceId || 'new'}`;
}

export async function viewWrite(pieceId) {
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
          placeholder="${escapeHtml(writePlaceholder())}"></textarea>
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
                  title="Translate it if it is not in English, then weave ${escapeHtml(targetName())} in">Make a lesson</button>
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
    </div>
    <section class="page-adjunct write-adjunct">
      <div class="adjunct-lead">
        <h2>From draft to lesson</h2>
        <p>A draft becomes a lesson in three passes.</p>
      </div>
      <div class="write-diagram" role="img" aria-label="A draft, checked, then woven into a lesson">
        <div class="diagram-step">
          <span class="diagram-lines" aria-hidden="true"><i></i><i></i><i></i></span>
          <span class="diagram-label">Draft</span>
        </div>
        <div class="diagram-step">
          <span class="diagram-lines" aria-hidden="true"><i></i><i></i><i></i></span>
          <span class="diagram-label">Checked</span>
        </div>
        <div class="diagram-step">
          <span class="diagram-lines" aria-hidden="true"><i></i><i></i><i></i></span>
          <span class="diagram-label">Lesson</span>
        </div>
      </div>
    </section>`;

  writeState = {
    pieceId: pieceId || null,
    prompt: null,
    modes: [],
    checked: null,
    marks: [],
    timers: {},
  };
  registerRouteCleanup(() => {
    clearTimeout(writeState.timers.measure);
  });

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
    if (writeState.pieceId) { location.hash = '#/write'; return; }
    startOver();
  });
}

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
  const box = $('#write-text');
  if (!box) return;
  const text = box.value;
  const words = text.trim() ? text.trim().split(/\s+/).length : 0;
  if (!words) { $('#write-count').textContent = ''; return; }
  const langLabel = targetName();
  try {
    const data = await api('/api/writing/measure', {
      method: 'POST',
      body: {
        text,
        prompt: (writeState.prompt || {}).instruction || '',
        words: (writeState.prompt || {}).words || [],
      },
    });
    const reading = data.reading || {};
    $('#write-count').innerHTML = [
      `<strong>${reading.words || 0}</strong> words`,
      `${reading.sentences || 0} sentences`,
      `<strong>${Math.round((reading.spanish_share || 0) * 100)}%</strong> ${escapeHtml(langLabel)}`,
      reading.enough ? '' : 'aim for 40–120',
    ].filter(Boolean).join(', ');
  } catch {
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
  host.innerHTML = writeState.modes.map((mode) => `
    <button class="btn ghost sm" data-mode="${escapeHtml(mode.id)}"
            title="${escapeHtml(mode.blurb)}">${escapeHtml(mode.label)}</button>`).join('');
  $$('[data-mode]', host).forEach((button) => button.addEventListener('click', () => {
    improveDraft(button.dataset.mode, button);
  }));
}

async function loadPiece(pieceId) {
  try {
    const data = await api(`/api/writing/${encodeURIComponent(pieceId)}`);
    writeState.modes = data.modes || writeState.modes;
    const piece = data.writing;
    writeState.pieceId = piece.id;
    writeState.prompt = { instruction: piece.prompt, words: [], source: '' };
    $('#write-title').textContent = piece.title;
    paintPrompt();
    paintModes();

    const restored = restoreDraft();
    const revisions = piece.revisions || [];
    const latest = revisions[revisions.length - 1];
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

async function checkDraft() {
  const text = $('#write-text').value.trim();
  if (!text) { toast('Write something first'); return; }
  const button = $('#write-check');
  button.disabled = true;
  $('#write-feedback').innerHTML =
    '<div class="loading"><span class="spinner"></span> reading it back</div>';
  try {
    const data = await api('/api/writing/check', {
      method: 'POST',
      body: {
        text,
        prompt: (writeState.prompt || {}).instruction || '',
        words: (writeState.prompt || {}).words || [],
        writing_id: writeState.pieceId,
      },
    });
    adoptPiece(data.writing_id, data.writing);
    applyRevision(data.revision, {
      reading: data.reading,
      note: data.note,
      verdict: data.verdict,
      feedback: data.feedback,
    });
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
    const data = await api('/api/writing/draft', {
      method: 'POST',
      body: {
        text,
        prompt: (writeState.prompt || {}).instruction || '',
        words: (writeState.prompt || {}).words || [],
        writing_id: writeState.pieceId,
      },
    });
    adoptPiece(data.writing_id, data.writing);
    toast('Kept as a version of this piece');
  } catch (err) { toast(err.message, 'err'); }
  button.disabled = false;
}

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
  toast('Cleared. The versions you kept are still there.');
}

function openFile() {
  const button = $('#write-open');
  const existing = $('#write-text').value.trim();
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
    toast('Write a few sentences first. A lesson needs something to weave.');
    return;
  }
  let options;
  try {
    options = await loadImportOptions();
  } catch (err) {
    toast(err.message, 'err');
    return;
  }
  const langLabel = targetName();
  openModal('Make a lesson from this', `
    <p class="small muted" style="margin-top:0">Your piece is put into English if it is not
    already, then woven into ${escapeHtml(langLabel)} with the settings below. It joins your library like any
    other lesson, with the same reader, notes and cards. This draft is kept as a version
    of the piece first, so the lesson refers back to something you can see.</p>
    <p class="small" id="lesson-note"></p>
    ${importControlsHtml(options)}
    <div class="row"><button class="btn primary" id="lesson-go">Weave it into ${escapeHtml(langLabel)}</button>
      <span class="small muted">It runs in the background. You can close this.</span></div>
    <div id="import-job"></div>`);

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
      if (response.writing_id && !writeState.pieceId) writeState.pieceId = response.writing_id;
      await loadWritings();
      $('#import-job').innerHTML = `<div data-job-detail="${escapeHtml(response.job_id)}" style="margin-top:16px"></div>`;
      const job = await api(`/api/jobs/${encodeURIComponent(response.job_id)}`);
      updateJob(job);
      button.textContent = 'Weaving. You can close this';
      button.classList.remove('primary');
    } catch (err) {
      toast(err.message, 'err');
      button.disabled = false;
      button.textContent = `Weave it into ${langLabel}`;
    }
  });
}

async function lessonNote(text) {
  const words = text.split(/\s+/).filter(Boolean).length;
  const langLabel = targetName();
  let share = 0;
  try {
    const data = await api('/api/writing/measure', {
      method: 'POST', body: { text, prompt: '', words: [] },
    });
    share = Math.round(((data.reading || {}).spanish_share || 0) * 100);
  } catch { /* the note is a nicety; the lesson still runs */ }
  if (!share) {
    return `About ${words} words of English. It will be woven into ${langLabel} at the settings below.`;
  }
  return `About ${words} words, roughly ${share}% of them ${langLabel}. What is not English is put `
    + 'into English first, so this becomes a diglot either way.';
}

function markStale() {
  const host = $('#write-stale-slot');
  if (!host) return;
  const shown = writeState.checked;
  if (!shown) { host.innerHTML = ''; return; }
  const edited = $('#write-text').value.trim() !== String(shown.text || '').trim();
  host.innerHTML = edited
    ? '<div class="small muted write-stale">You have edited since this version was read. '
      + 'check again to see what changed.</div>'
    : '';
}

function applyRevision(revision, extra = {}) {
  writeState.checked = revision;
  writeState.marks = [];
  const reading = extra.reading || revision.reading || {};
  const verdict = extra.verdict || revision.verdict || {};
  const feedback = (extra.feedback !== undefined ? extra.feedback : revision.feedback) || {};
  const note = extra.note || '';
  const langLabel = targetName();
  $('#write-count').innerHTML = [
    `<strong>${reading.words || 0}</strong> words`,
    `${reading.sentences || 0} sentences`,
    `<strong>${Math.round((reading.spanish_share || 0) * 100)}%</strong> ${escapeHtml(langLabel)}`,
  ].join(', ');
  $('#write-feedback').innerHTML = feedbackPanel(
    reading, verdict, feedback, note, revision.checked !== false);
  $('#write-annotated').innerHTML = annotatedPanel(revision.text, feedback);
  wireMarks();
  markStale();
  wireSaveWords(feedback, revision.text);
}

function wireSaveWords(feedback, text) {
  $$('#write-feedback [data-card]').forEach((button) => button.addEventListener('click', async () => {
    const item = ((feedback || {}).corrections || [])[Number(button.dataset.card)] || {};
    if (!item.should_be) return;
    button.disabled = true;
    try {
      const saved = await api('/api/word/save', {
        method: 'POST',
        body: {
          term: item.should_be,
          note: item.why || null,
          context: item.their_text || String(text || '').slice(0, 200),
        },
      });
      button.textContent = 'Saved';
      toast(`Saved “${item.should_be}”. Your deck now holds ${saved.total} words.`);
      refreshDuePill();
    } catch (err) { button.disabled = false; toast(err.message, 'err'); }
  }));
}

function facts(reading, verdict) {
  const langLabel = targetName();
  return [
    `<span><strong>${reading.words || 0}</strong> words</span>`,
    `<span><strong>${Math.round((reading.spanish_share || 0) * 100)}%</strong> ${escapeHtml(langLabel)}</span>`,
    `<span><strong>${reading.distinct || 0}</strong> different words</span>`,
    verdict && verdict.score != null
      ? `<span>overall <strong>${Number(verdict.score).toFixed(1)}</strong> / ${verdict.score_max || 4}</span>` : '',
  ].filter(Boolean).join('');
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
        background:${value >= 0.6 ? 'var(--good)' : value >= 0.35 ? 'var(--warn)' : 'var(--bad)'}"></i></span>
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
    ${feedback.praise ? `<p class="small">${escapeHtml(feedback.praise)}</p>` : ''}
    ${feedback.next ? `<p class="small muted">Next time: ${escapeHtml(feedback.next)}</p>` : ''}
    ${verdict.model ? `<p class="small muted">read by ${escapeHtml(verdict.model)}${
      verdict.latency_ms ? ` in ${verdict.latency_ms} ms` : ''}</p>` : ''}`;
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

function annotatedPanel(text, feedback) {
  writeState.marks = [];
  const notes = (feedback || {}).notes || [];
  if (!notes.length) return '';
  const claimed = [];
  const placed = [];
  notes.forEach((note) => {
    const at = freeIndex(text, note.fragment, claimed);
    if (at < 0) return;
    claimed.push([at, at + note.fragment.length]);
    placed.push({ ...note, start: at, end: at + note.fragment.length });
  });
  if (!placed.length) return '';
  placed.sort((a, b) => a.start - b.start);
  placed.forEach((mark, index) => { mark.index = index; });

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

  const good = placed.filter((m) => m.kind === 'good').length;
  const bad = placed.length - good;
  return `<div class="write-annotated">
    <div class="small muted">What the tutor saw: ${bad} to look at${
      good ? `, ${good} worth keeping` : ''}. Click any mark.</div>
    <p class="annotated-text">${html}</p>
  </div>`;
}

function wireMarks() {
  $$('#write-annotated .w-note').forEach((mark) => mark.addEventListener('click', (event) => {
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

async function improveDraft(mode, button) {
  const text = $('#write-text').value.trim();
  if (!text) { toast('Write something first'); return; }
  const host = $('#write-suggestion');
  if (button) button.disabled = true;
  host.innerHTML = '<div class="loading"><span class="spinner"></span> working on a version</div>';
  try {
    const data = await api('/api/writing/improve', {
      method: 'POST',
      body: {
        text,
        mode,
        prompt: (writeState.prompt || {}).instruction || '',
        words: (writeState.prompt || {}).words || [],
      },
    });
    const suggestion = data.suggestion;
    if (!suggestion || suggestion.failed || !suggestion.revision) {
      host.innerHTML = `<p class="small muted">${escapeHtml(
        (suggestion && (suggestion.summary || suggestion.changed)) || 'No suggestion came back.')}</p>`;
    } else {
      host.innerHTML = `<div class="suggestion">
        <div class="small muted">${escapeHtml(suggestion.label || '')}${
          suggestion.changed ? `. ${escapeHtml(suggestion.changed)}` : ''}</div>
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

function renderVersions(piece) {
  const host = $('#write-versions');
  if (!host) return;
  const revisions = (piece.revisions || []).slice().reverse();
  const langLabel = targetName();
  if (!revisions.length) {
    host.innerHTML = '<h4>Versions</h4><p class="small muted">Nothing kept yet. Checking a draft keeps it here.</p>';
    return;
  }
  host.innerHTML = `<h4>Versions <span class="muted">${revisions.length}</span></h4>
    <div class="versions">${revisions.map((revision) => {
      const reading = revision.reading || {};
      const verdict = revision.verdict || {};
      const current = writeState.checked && writeState.checked.id === revision.id;
      return `<button class="version ${current ? 'current' : ''}" data-revision="${escapeHtml(revision.id)}">
        <span class="v-label">${escapeHtml(revision.label || 'Draft')}</span>
        <span class="v-meta">${escapeHtml((revision.created_at || '').slice(0, 10))}, ${reading.words || 0} words, ${
          Math.round((reading.spanish_share || 0) * 100)}% ${escapeHtml(langLabel)}${
          revision.checked && verdict.score != null ? `, ${Number(verdict.score).toFixed(1)}/4` : ', not read'}</span>
      </button>`;
    }).join('')}</div>`;
  $$('[data-revision]', host).forEach((button) => button.addEventListener('click', () => {
    const revision = revisions.find((item) => String(item.id) === button.dataset.revision);
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
  const langLabel = targetName();
  $('#write-summary').textContent = totals.pieces
    ? `${totals.pieces} piece${totals.pieces === 1 ? '' : 's'}, ${totals.revisions} version${
        totals.revisions === 1 ? '' : 's'}, ${totals.words} words written, ${
        Math.round((totals.spanish_share || 0) * 100)}% of them ${langLabel}.`
    : 'Nothing written yet. The prompt is drawn from what you have been reading and looking up.';
  if (!writeState.prompt && data.prompt) {
    writeState.prompt = data.prompt;
    paintPrompt();
  }
  if (!writeState.modes.length) { writeState.modes = data.modes || []; paintModes(); }

  const pieces = (data.writings || []).filter((piece) => piece.id !== writeState.pieceId);
  host.hidden = !pieces.length;
  host.innerHTML = pieces.length
    ? `<h4>Earlier pieces</h4><div class="pieces">${pieces.map((piece) => `
        <div class="piece" data-piece="${escapeHtml(piece.id)}">
          <button class="piece-open">
            <span class="p-title">${escapeHtml(piece.title || piece.excerpt || 'Untitled')}</span>
            <span class="p-meta">${escapeHtml((piece.updated_at || '').slice(0, 10))}, ${
              piece.revision_count} version${piece.revision_count === 1 ? '' : 's'}, ${
              piece.words} words${piece.verdict && piece.verdict.score != null
                ? `, ${Number(piece.verdict.score).toFixed(1)}/4` : ''}</span>
          </button>
          <button class="btn ghost sm" data-drop="${escapeHtml(piece.id)}" title="Delete this piece and its versions">×</button>
        </div>`).join('')}</div>`
    : '';

  $$('[data-piece]', host).forEach((row) => row.querySelector('.piece-open')
    .addEventListener('click', () => { location.hash = `#/write/${encodeURIComponent(row.dataset.piece)}`; }));
  $$('[data-drop]', host).forEach((button) => {
    let armed = false;
    button.addEventListener('click', async () => {
      if (!armed) {
        armed = true;
        button.textContent = 'sure?';
        setTimeout(() => { armed = false; button.textContent = '×'; }, 3000);
        return;
      }
      try {
        await api(`/api/writing/${encodeURIComponent(button.dataset.drop)}`, { method: 'DELETE' });
        toast('Deleted that piece');
        await loadWritings();
      } catch (err) { toast(err.message, 'err'); }
    });
  });
}
