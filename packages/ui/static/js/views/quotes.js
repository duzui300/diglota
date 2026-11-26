/**
 * Quotes view: kept sentences, bilingual search, source filtering, and inline notes.
 */

import { $, $$, escapeHtml, toast } from '../lib/dom.js';
import { api } from '../lib/api.js';
import { registerRouteCleanup, setActiveTab } from '../lib/state.js';
import { isTargetLang, targetName } from '../lib/language.js';

let quoteSearch = '';
let quoteSource = 'all';
let quoteTimer = null;

const QUOTE_EMPTY = `<div class="empty"><h3>No sentences kept yet</h3>
  <p>Select a sentence while reading and press <strong>Keep</strong>. Sentences are what a
  language is made of. A word on its own is a translation, but a sentence is the memory
  of how it was used.</p>
  <p class="small muted">Keeping one costs nothing and calls no model, so it never
  interrupts the reading.</p>
  <p><a class="btn primary sm" href="#/library">Read something</a></p></div>
  <section class="page-adjunct quote-adjunct">
    <div class="adjunct-quote" aria-hidden="true">“</div>
    <div class="adjunct-lead">
      <h2>A sentence keeps the word in place.</h2>
      <p>Keep the line when the phrase is one you would use, when the shape of the
      grammar surprised you, or when its sound is worth coming back to.</p>
    </div>
    <div class="adjunct-list">
      <p><strong>For the phrase.</strong> You can already hear yourself saying it.</p>
      <p><strong>For the shape.</strong> A construction you had to stop and work out.</p>
      <p><strong>For the sound.</strong> A line whose rhythm stayed after the page.</p>
    </div>
  </section>`;

function quoteSpans(spans) {
  return spans.map((span) => {
    if (!isTargetLang(span.lang)) return `<span class="en">${escapeHtml(span.text)}</span>`;
    const classes = `es${span.target ? ' target' : ''}${span.bold ? ' bold' : ''}`;
    return `<span class="${classes}">${escapeHtml(span.text)}</span>`
      + (span.gloss ? `<span class="gloss">${escapeHtml(span.gloss)}</span>` : '');
  }).join('');
}

function renderQuoteSources(sources) {
  const host = $('#quote-sources');
  if (!host) return;
  if (sources.length < 2) { host.innerHTML = ''; return; }
  const live = sources.filter((source) => source.slug && source.title !== '(removed)');
  if (!live.length) { host.innerHTML = ''; return; }
  if (quoteSource !== 'all' && !live.some((source) => source.slug === quoteSource)) quoteSource = 'all';
  host.innerHTML = `
    <button class="choice sm ${quoteSource === 'all' ? 'active' : ''}" data-source="all">
      Every article</button>
    ${live.map((source) => `
      <button class="choice sm ${source.slug === quoteSource ? 'active' : ''}"
              data-source="${escapeHtml(source.slug)}"
              title="${escapeHtml(source.title)}">
        ${escapeHtml(source.title.length > 34 ? `${source.title.slice(0, 34)}…` : source.title)}
        <span class="muted">${source.count}</span>
      </button>`).join('')}`;
  $$('[data-source]', host).forEach((button) => button.addEventListener('click', () => {
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
  return `<article class="quote" data-id="${escapeHtml(row.id)}">
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
  $$('.quote', host).forEach((card) => {
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
        await api(`/api/quote/${encodeURIComponent(id)}`, { method: 'PATCH', body: { note } });
        const line = card.querySelector('.quote-note');
        line.textContent = note;
        line.hidden = !note;
        card.querySelector('[data-note]').textContent = note ? 'Edit note' : 'Add note';
        edit.hidden = true;
        toast(note ? 'Note saved' : 'Note removed');
      } catch (err) { toast(err.message, 'err'); }
    });

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
        await api(`/api/quote/${encodeURIComponent(id)}`, { method: 'DELETE' });
        card.remove();
        toast('Removed from your quotes');
        loadQuotes();
      } catch (err) { toast(err.message, 'err'); }
    });
  });
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
        ${data.quotes.length === 1 ? 'sentence matches' : 'sentences match'}</div>`
      + data.quotes.map(quoteCard).join('')
    : data.quotes.map(quoteCard).join('');
  wireQuoteCards(list);
}

export async function viewQuotes() {
  setActiveTab('quotes');
  $('#reader-bar').hidden = true;
  document.body.classList.remove('dim-english', 'es-only');
  const view = $('#view');
  view.className = 'view';
  const langLabel = targetName();
  view.innerHTML = `
    <div class="page-head">
      <h1>Quotes</h1>
      <p id="quote-summary" class="muted"></p>
      <div class="field quote-search">
        <input type="search" id="quote-q" placeholder="Search your sentences in ${escapeHtml(langLabel)} or English"
               value="${escapeHtml(quoteSearch)}" aria-label="Search kept sentences">
      </div>
    </div>
    <div id="quote-sources" class="row filter-row"></div>
    <div id="quote-list"></div>`;

  const input = $('#quote-q');
  input.addEventListener('input', () => {
    clearTimeout(quoteTimer);
    quoteTimer = setTimeout(() => {
      quoteSearch = input.value.trim();
      loadQuotes();
    }, 180);
  });

  registerRouteCleanup(() => {
    clearTimeout(quoteTimer);
    quoteTimer = null;
  });

  await loadQuotes();
}
