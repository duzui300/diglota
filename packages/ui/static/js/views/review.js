/**
 * Review (#/review) and Saved Words Deck (#/cards) views.
 */

import { $, $$, escapeHtml, fold, toast } from '../lib/dom.js';
import { api } from '../lib/api.js';
import { STAGE_ORDER, refreshDuePill, setActiveTab, store } from '../lib/state.js';
import { speak } from '../lib/speech.js';
import { blankOut, dictionaryForm, highlightIn, targetName } from '../lib/language.js';

let reviewQueue = [];
let reviewIndex = 0;
let reviewRevealed = false;
let reviewShownAt = 0;
let reviewMode = store.get('reviewMode', 'recognise');

let cardsState = { query: '', stage: 'all', sort: 'recent', rows: [], total: 0, limit: 0 };

export const isReviewRevealed = () => reviewRevealed;
export const currentReviewCard = () => reviewQueue[reviewIndex] || null;

function daysUntil(iso) {
  if (!iso) return null;
  const due = new Date(iso).getTime();
  if (Number.isNaN(due)) return null;
  return (due - Date.now()) / 86400000;
}

function dueText(iso, stage) {
  const days = daysUntil(iso);
  if (days === null) return { text: stage === 'new' ? 'new' : 'never reviewed', late: false };
  if (days < 0) return { text: days > -1 ? 'due now' : `${Math.round(-days)} days overdue`, late: true };
  if (days < 1) return { text: 'due today', late: false };
  if (days < 14) return { text: `in ${Math.round(days)} day${Math.round(days) === 1 ? '' : 's'}` };
  if (days < 60) return { text: `in ${Math.round(days / 7)} weeks` };
  return { text: `in ${Math.round(days / 30)} months` };
}

function visibleCards() {
  const rows = cardsState.stage === 'all'
    ? [...cardsState.rows]
    : cardsState.rows.filter((row) => row.stage === cardsState.stage);
  if (cardsState.sort === 'alpha') {
    return rows.sort((a, b) => fold(a.term).localeCompare(fold(b.term)));
  }
  if (cardsState.sort === 'due') {
    return rows.sort((a, b) => {
      const da = fold(a.due_at || '9999');
      const db = fold(b.due_at || '9999');
      return da < db ? -1 : da > db ? 1 : 0;
    });
  }
  if (cardsState.sort === 'stuck') {
    return rows.sort((a, b) => (b.lapses || 0) - (a.lapses || 0) || (a.interval_days || 0) - (b.interval_days || 0));
  }
  return rows;
}

function cardRowHtml(card) {
  const due = dueText(card.due_at, card.stage);
  const lemma = String(card.lemma || '').trim();
  const showsForm = lemma && fold(lemma) !== fold(card.term);
  const reviews = card.reps ? `${card.reps} review${card.reps === 1 ? '' : 's'}` : 'not reviewed';
  const lapses = card.lapses ? `, ${card.lapses} lapse${card.lapses === 1 ? '' : 's'}` : '';
  return `<div class="deck-row">
    <div class="deck-word">
      <button class="btn-link es" data-hear="${escapeHtml(card.term)}"
              title="Hear it">${escapeHtml(card.term)}</button>
      ${showsForm ? `<div class="small muted">${escapeHtml(card.pos === 'verb' ? 'infinitive' : 'dictionary form')}
        ${escapeHtml(lemma)}</div>` : ''}
      ${card.context ? `<div class="deck-context">${escapeHtml(card.context)}</div>` : ''}
    </div>
    <div class="deck-gloss">${escapeHtml(card.gloss || 'no gloss')}</div>
    <div class="deck-when" title="${escapeHtml(card.due_at || '')}">
      <span class="${due.late ? 'is-late' : ''}">${escapeHtml(due.text)}</span>
      <div class="small muted">${reviews}${lapses}</div>
    </div>
    <div class="deck-from">${card.article_slug
      ? `<a class="quote-source" href="#/read/${encodeURIComponent(card.article_slug)}"
            title="${escapeHtml(card.article_title || '')}">${escapeHtml(card.article_title || card.article_slug)}</a>`
      : '<span class="muted small">no article</span>'}</div>
    <button class="icon-btn" data-drop-card="${escapeHtml(card.id)}" title="Remove" aria-label="Remove ${escapeHtml(card.term)} from the deck">✕</button>
  </div>`;
}

function renderCardList() {
  const host = $('#cards-list');
  if (!host) return;
  const rows = visibleCards();
  const summary = $('#cards-summary');
  const langLabel = targetName();
  const shape = [];
  shape.push(`<strong>${cardsState.total}</strong> saved`);
  if (cardsState.query) shape.push(`${rows.length} matching “${escapeHtml(cardsState.query)}”`);
  if (cardsState.total > cardsState.limit) {
    shape.push(`showing the ${cardsState.limit} most recent`);
  }
  if (summary) summary.innerHTML = shape.join(', ');

  if (!rows.length) {
    host.innerHTML = `<div class="empty"><p>${cardsState.query
      ? 'No saved word matches that.'
      : `Nothing saved yet. Click any ${escapeHtml(langLabel)} word while reading.`}</p></div>`;
    return;
  }

  host.innerHTML = `<div class="deck">${rows.map(cardRowHtml).join('')}</div>`;
  $$('[data-hear]', host).forEach((button) => button.addEventListener('click',
    () => speak(button.dataset.hear)));
  $$('[data-drop-card]', host).forEach((button) => button.addEventListener('click', async () => {
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
      const id = button.dataset.dropCard;
      await api(`/api/word/${encodeURIComponent(id)}`, { method: 'DELETE' });
      cardsState.rows = cardsState.rows.filter((row) => String(row.id) !== id);
      cardsState.total = Math.max(0, cardsState.total - 1);
      toast('Removed from the deck');
      renderCardList();
    } catch (err) {
      button.disabled = false;
      toast(err.message, 'err');
    }
  }));
}

export async function viewCards() {
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
  cardsState.rows.forEach((row) => { if (row.stage in stages) stages[row.stage] += 1; });
  const langLabel = targetName();

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
      <input type="search" id="cards-search" placeholder="Search the ${escapeHtml(langLabel)}, the English, the sentence you met it in, or the article"
             value="${escapeHtml(cardsState.query)}">
      <div class="chips" id="cards-stages">
        <button class="choice sm ${cardsState.stage === 'all' ? 'active' : ''}" data-stage="all">
          All <span class="muted">${cardsState.rows.length}</span></button>
        ${STAGE_ORDER.filter(([key]) => stages[key]).map(([key, label]) => `
          <button class="choice sm ${cardsState.stage === key ? 'active' : ''}" data-stage="${escapeHtml(key)}">
            ${escapeHtml(label)} <span class="muted">${stages[key]}</span></button>`).join('')}
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

  let timer = null;
  $('#cards-search').addEventListener('input', (event) => {
    cardsState.query = event.target.value;
    clearTimeout(timer);
    timer = setTimeout(() => {
      const caret = event.target.selectionStart;
      viewCards().then(() => {
        const box = $('#cards-search');
        if (box) { box.focus(); box.setSelectionRange(caret, caret); }
      });
    }, 320);
  });
  $$('[data-stage]', view).forEach((button) => button.addEventListener('click', () => {
    cardsState.stage = button.dataset.stage;
    $$('[data-stage]', view).forEach((b) => b.classList.toggle('active', b === button));
    renderCardList();
  }));
  $('#cards-sort').addEventListener('change', (event) => {
    cardsState.sort = event.target.value;
    renderCardList();
  });
}

export async function viewReview() {
  setActiveTab('review');
  $('#reader-bar').hidden = true;
  const view = $('#view');
  view.className = 'view';
  view.innerHTML = '<div class="empty"><span class="spinner"></span></div>';

  const data = await api('/api/review');
  reviewQueue = data.cards || [];
  reviewIndex = 0;
  const langLabel = targetName();
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
          title="See the ${escapeHtml(langLabel)}, recall the meaning">Recognise</button>
        <button data-mode="produce" class="${reviewMode === 'produce' ? 'active' : ''}"
          title="See the sentence with the word blanked, type it back">Produce</button>
      </div>
      <span class="spacer"></span>
      <span class="small muted">${reviewQueue.length} card${reviewQueue.length === 1 ? '' : 's'}</span>
      <a class="btn ghost sm" href="#/cards" title="Every saved word, not only what is due">All saved words</a>
    </div>
    <div id="review-stage"></div>
  </div>
  <section class="page-adjunct review-adjunct">
    <div class="adjunct-lead">
      <h2>How a card moves</h2>
      <p>An answer you recall easily moves the card to the next interval. A hard one
      brings it back sooner, so the schedule follows what you actually remember.</p>
    </div>
    <div class="stage-ladder">
      <div class="stage-step"><span class="name">New</span><span class="when">not yet reviewed</span></div>
      <div class="stage-step"><span class="name">Learning</span><span class="when">back within a day</span></div>
      <div class="stage-step"><span class="name">Young</span><span class="when">under three weeks</span></div>
      <div class="stage-step"><span class="name">Mature</span><span class="when">three weeks or more</span></div>
    </div>
  </section>`;
  $$('[data-mode]', view).forEach((button) => button.addEventListener('click', () => {
    reviewMode = button.dataset.mode;
    store.set('reviewMode', reviewMode);
    $$('[data-mode]', view).forEach((b) => b.classList.toggle('active', b.dataset.mode === reviewMode));
    renderCard();
  }));
  renderCard();
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
    <p class="shortcut-line small muted">
      <span><span class="kbd">space</span> reveal</span>
      <span><span class="kbd">1</span> to <span class="kbd">4</span> grade</span>
      <span><span class="kbd">S</span> hear it</span>
    </p>`;

  if (blank) {
    $('#card-answer').innerHTML = '<div class="muted small review-hint">Write it, then check your answer.</div>';
    const input = $('#cloze-answer');
    input.focus();
    const submit = () => checkCloze(card);
    $('#cloze-check').addEventListener('click', submit);
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); event.stopPropagation(); submit(); }
    });
  } else {
    $('#card-answer').innerHTML = '<div class="muted small review-hint">Recall the meaning, then reveal it.</div>';
  }
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
    $('#card-answer').innerHTML = `<div class="small muted">could not grade: ${escapeHtml(err.message)}</div>`;
  }

  const correct = result ? result.correct : null;
  const checks = (result && result.verdict && result.verdict.checks) || {};
  $('#card-answer').innerHTML = `<div class="answer">
    <div class="gloss">${escapeHtml(card.term)}</div>
    ${dictionaryForm(card)}
    <div class="note">${escapeHtml(card.gloss || '')}${card.note ? ` (${escapeHtml(card.note)})` : ''}</div>
    ${result ? `<div class="meters" style="max-width:340px;margin:14px auto 0">
      ${Object.entries(checks).map(([key, value]) => `
        <div class="meter"><span>${key === 'fits' ? 'fits the blank' : 'exact word'}</span>
        <span class="track"><i style="width:${Math.round(value * 100)}%;
          background:${value >= 0.6 ? 'var(--good)' : value >= 0.35 ? 'var(--warn)' : 'var(--bad)'}"></i></span>
        <span class="val">${Math.round(value * 100)}%</span></div>`).join('')}
    </div>` : ''}
    ${correct === false ? '<div class="small" style="margin-top:8px;color:var(--bad)">not quite, but the meaning may still fit, so judge yourself</div>' : ''}
  </div>`;
  reviewRevealed = true;
  renderGrades(card);
}

function renderGrades(card) {
  const order = [['again', 'Again'], ['hard', 'Hard'], ['good', 'Good'], ['easy', 'Easy']];
  $('#card-grades').innerHTML = `<div class="grades">${order.map(([key, label]) => `
    <button data-rating="${escapeHtml(key)}"><span class="label">${escapeHtml(label)}</span>
      <span class="when">${escapeHtml((card.preview || {})[key] || '')}</span></button>`).join('')}</div>`;
  $$('#card-grades button').forEach((button) =>
    button.addEventListener('click', () => gradeCard(button.dataset.rating)));
}

export function revealCard() {
  if (reviewRevealed) return;
  if (reviewMode === 'produce' && $('#cloze-answer') && !$('#cloze-answer').disabled) {
    checkCloze(reviewQueue[reviewIndex]);
    return;
  }
  reviewRevealed = true;
  const card = reviewQueue[reviewIndex];
  if (!card) return;
  $('#card-answer').innerHTML = `<div class="answer">
    <div class="gloss">${escapeHtml(card.gloss || 'no gloss')}</div>
    ${dictionaryForm(card)}
    ${card.note ? `<div class="note">${escapeHtml(card.note)}</div>` : ''}
  </div>`;
  renderGrades(card);
}

export async function gradeCard(rating) {
  if (!reviewRevealed) { revealCard(); return; }
  const card = reviewQueue[reviewIndex];
  if (!card) return;
  try {
    await api(`/api/review/${encodeURIComponent(card.card_id)}`, {
      method: 'POST',
      body: { rating, elapsed_ms: Date.now() - reviewShownAt },
    });
  } catch (err) { toast(err.message, 'err'); }
  reviewIndex += 1;
  renderCard();
}
