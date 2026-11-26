/**
 * Library view: shelves, reading-state filters, register filters, live search, and worth-saving panel.
 */

import { $, $$, escapeHtml, fold, hostOf, shortenText, toast } from '../lib/dom.js';
import { api } from '../lib/api.js';
import {
  REGISTER_LABELS,
  REGISTER_ORDER,
  refreshDuePill,
  setActiveTab,
  store,
} from '../lib/state.js';
import { targetName } from '../lib/language.js';
import { openSharedLesson } from '../modals/modals.js';

const SHELF_FILTERS = [
  ['all', 'All'],
  ['unread', 'Unread'],
  ['reading', 'Started'],
  ['finished', 'Finished'],
];

let libraryFilter = store.get('libraryFilter', 'all');
let registerFilter = store.get('registerFilter', 'all');
let libraryQuery = '';

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

function articleMatchesQuery(article, query) {
  if (!query) return true;
  const haystack = fold([
    article.title,
    article.author,
    article.preview,
    article.level,
    article.shelf,
    article.url ? hostOf(article.url) : '',
    REGISTER_LABELS[article.register] || article.register,
  ].filter(Boolean).join(' '));
  return haystack.includes(fold(query));
}

function libraryShelvesHtml(articles, query) {
  if (!articles.length) {
    if (query) {
      return `<div class="empty"><h3>Nothing matches “${escapeHtml(query)}”</h3>
        <p>Searched titles, authors, sources, levels and registers, but not the text of
        the lessons themselves.</p>
        <p style="margin-top:14px"><button class="btn ghost sm" id="library-search-clear">
          Clear the search</button></p></div>`;
    }
    return `<div class="empty"><h3>Nothing ${libraryFilter === 'finished' ? 'finished' : 'unread'}</h3>
      <p>${libraryFilter === 'finished'
        ? 'Articles you mark as finished will collect here.'
        : 'Every article has been opened at least once. Try another filter.'}</p></div>`;
  }

  const shelves = new Map();
  for (const article of articles) {
    if (!shelves.has(article.shelf)) shelves.set(article.shelf, []);
    shelves.get(article.shelf).push(article);
  }
  return [...shelves].map(([shelf, group]) => `<section class="shelf">
      <div class="shelf-head"><h2>${escapeHtml(shelf)}</h2>
        <span class="count">${group.length}</span></div>
      <p class="shelf-desc">${escapeHtml(group[0].shelf_description || '')}</p>
      <div class="grid">${group.map(articleCard).join('')}</div>
    </section>`).join('');
}

function nextLesson(articles) {
  return articles.find((article) => {
    const progress = article.progress || {};
    return progress.position > 0.02 && !progress.completed;
  }) || articles.find((article) => !(article.progress || {}).completed) || articles[0];
}

function featuredLessonHtml(article, allRead) {
  const coverage = article.coverage || {};
  const text = coverage.text || {};
  const known = Math.round((text.token_ratio || 0) * 100);
  const ratio = Math.round(article.stats.spanish_ratio * 100);
  const progress = article.progress || {};
  const band = coverage.band || 'immersion';
  const started = progress.position > 0.02 && !progress.completed;
  const hasDeck = (text.types || 0) > 0 && known > 0;
  const advice = coverage.advice || '';
  const langLabel = targetName();
  const heading = allRead ? 'Everything here is read'
    : started ? 'Pick up where you left off'
    : 'The next lesson';
  const note = allRead
    ? 'Nothing on the shelves is unread. This is a good one to come back to.'
    : started
      ? 'You were partway through this one, and a little further is a fair place to stop.'
      : `The first lesson here you have not opened. Its shelf says how much ${langLabel} it asks of you.`;
  return `<section class="library-feature" aria-labelledby="feature-title">
    <a class="cover" href="#/read/${encodeURIComponent(article.slug)}">
      <div>
        <h2>${escapeHtml(article.title)}</h2>
        ${article.author ? `<div class="byline">${escapeHtml(article.author)}</div>` : ''}
        <div class="cover-meta">
          <span>${ratio}% ${escapeHtml(langLabel)}</span>
          <span>${article.stats.words} words</span>
          ${article.level ? `<span>${escapeHtml(article.level)}</span>` : ''}
          ${article.register && article.register !== 'unspecified'
            ? `<span>${escapeHtml(REGISTER_LABELS[article.register] || article.register)}</span>` : ''}
        </div>
      </div>
      ${article.preview ? `<p class="cover-quote">“${escapeHtml(shortenText(article.preview, 130))}”</p>` : '<span></span>'}
      <div class="cover-foot">
        ${hasDeck ? `<span class="chip">${known}% of the ${escapeHtml(langLabel)} known</span>` : '<span></span>'}
        ${progress.completed
          ? '<span class="complete-tick">Read</span>'
          : started
            ? `<span class="bar" title="Where you left off"><i style="width:${Math.round(progress.position * 100)}%"></i></span>`
            : ''}
      </div>
    </a>
    <div class="feature-side">
      <h2 id="feature-title">${heading}</h2>
      <p>${escapeHtml(note)}${advice ? ` ${escapeHtml(advice)}` : ''}</p>
      ${hasDeck ? `<div class="read-line">
        <span class="read-label">${known}% of the ${escapeHtml(langLabel)} known</span>
        <span class="bar band-${escapeHtml(band)}" title="${escapeHtml(advice)}"><i style="width:${Math.min(known, 100)}%"></i></span>
      </div>` : ''}
      <a class="btn primary" href="#/read/${encodeURIComponent(article.slug)}">${allRead ? 'Read it again' : 'Open the next lesson'}</a>
    </div>
  </section>`;
}

function articleCard(article) {
  const coverage = article.coverage || {};
  const text = coverage.text || {};
  const known = Math.round((text.token_ratio || 0) * 100);
  const ratio = Math.round(article.stats.spanish_ratio * 100);
  const progress = article.progress;
  const band = coverage.band || 'immersion';
  const hasDeck = (text.types || 0) > 0 && known > 0;
  const langLabel = targetName();
  return `<article class="card" data-slug="${escapeHtml(article.slug)}" tabindex="0" role="link">
    <h3>${escapeHtml(article.title)}</h3>
    ${article.author ? `<div class="byline">${escapeHtml(article.author)}</div>` : ''}
    ${article.preview ? `<p class="preview">${escapeHtml(shortenText(article.preview, 90))}</p>` : ''}
    <div class="meta">
      <span class="chip">${ratio}% ${escapeHtml(langLabel)}</span>
      <span class="chip">${article.stats.words} words</span>
      ${article.level ? `<span class="chip" title="Estimated CEFR level of the ${escapeHtml(langLabel)}">${escapeHtml(article.level)}</span>` : ''}
      ${article.register && article.register !== 'unspecified'
        ? `<span class="chip register" title="${escapeHtml(article.register_blurb || '')}">
             ${escapeHtml(REGISTER_LABELS[article.register] || article.register)}</span>` : ''}
      ${hasDeck ? `<span class="chip accent" title="${escapeHtml(coverage.advice || '')}">
        ${known}% of ${escapeHtml(langLabel)} known</span>` : ''}
      ${article.imported ? '<span class="chip imported">imported</span>' : ''}
      ${progress.completed ? '<span class="complete-tick">Read</span>' : ''}
    </div>
    ${hasDeck ? `<div class="bar thin band-${escapeHtml(band)}"
        title="${known}% of the ${escapeHtml(langLabel)} words in this article are already in your deck">
      <i style="width:${Math.min(known, 100)}%"></i></div>` : ''}
    ${!progress.completed && progress.position > 0.02
      ? `<div class="bar thin" title="Where you left off"><i style="width:${Math.round(progress.position * 100)}%"></i></div>` : ''}
    ${article.imported ? `<div class="card-foot">
      <button class="btn ghost xs danger-hover" data-remove-lesson="${escapeHtml(article.slug)}"
              title="Delete this lesson's file and its reading progress. Words you saved from it stay in your deck.">Remove</button>
    </div>` : ''}
  </article>`;
}

export async function viewLibrary() {
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
    counts[key] = data.articles.filter((a) => articleMatchesFilter(a, key)).length;
  }
  for (const article of data.articles) {
    const code = article.register || 'unspecified';
    registerCounts[code] = (registerCounts[code] || 0) + 1;
  }
  const presentRegisters = REGISTER_ORDER.filter((code) => registerCounts[code]);
  if (registerFilter !== 'all' && !registerCounts[registerFilter]) registerFilter = 'all';

  const query = libraryQuery.trim();
  const shown = data.articles.filter((a) => articleMatchesFilter(a, libraryFilter)
    && articleMatchesRegister(a, registerFilter) && articleMatchesQuery(a, query));

  const readCount = counts.finished;
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const candidate = nextLesson(data.articles);
  const allRead = data.articles.every((article) => (article.progress || {}).completed);
  const langLabel = targetName();
  const parts = [`<div class="page-head">
      <h1>Your library</h1>
      <p class="library-summary">
        <span>${plural(data.articles.length, 'article', 'articles')}</span>
        <span>${readCount} finished</span>
        <span>${plural(data.words, 'word', 'words')} saved</span>
      </p>
      <p class="library-note">Shelves run from mostly-English to ${escapeHtml(langLabel)}-dominant, so you can pick the next thing you can actually read.</p>
    </div>`];

  parts.push(featuredLessonHtml(candidate, allRead));

  parts.push(`<div class="library-toolbar">
    <div class="row filter-row" role="group" aria-label="Filter articles">
      ${SHELF_FILTERS.map(([key, label]) => `
        <button class="choice sm ${key === libraryFilter ? 'active' : ''}" data-filter="${escapeHtml(key)}">
          ${escapeHtml(label)} <span class="muted">${counts[key]}</span>
        </button>`).join('')}
    </div>
    <div class="library-tools">
      <span class="small muted" id="library-match" aria-live="polite"></span>
      <input type="search" id="library-search" class="library-search"
             placeholder="Search titles, authors, sources…" aria-label="Search the library"
             value="${escapeHtml(libraryQuery)}">
      <button class="btn ghost sm" id="btn-open-share"
              title="A diglot .md file someone sent you">Open a shared lesson</button>
    </div>
  </div>`);

  if (presentRegisters.length > 1) {
    parts.push(`<div class="row filter-row register-row" role="group" aria-label="Filter by register">
      <button class="choice sm ${registerFilter === 'all' ? 'active' : ''}" data-register="all">
        Every register</button>
      ${presentRegisters.map((code) => `
        <button class="choice sm ${code === registerFilter ? 'active' : ''}" data-register="${escapeHtml(code)}"
                title="${escapeHtml((data.articles.find((a) => (a.register || 'unspecified') === code) || {}).register_blurb || '')}">
          ${escapeHtml(REGISTER_LABELS[code] || code)} <span class="muted">${registerCounts[code]}</span>
        </button>`).join('')}
    </div>`);
  }

  parts.push(`<div id="library-shelves">${libraryShelvesHtml(shown, query)}</div>`);

  if ((data.worth_saving || []).length && libraryFilter === 'all') {
    parts.push(`<div class="panel worth">
      <h3>Words you keep looking up</h3>
      <p class="panel-sub">You have looked these up more than once and not saved them yet.
        That usually means they have not stuck.</p>
      <div class="row">
        ${data.worth_saving.map((word) => `<button class="btn sm" data-worth="${escapeHtml(word.lemma)}"
            data-gloss="${escapeHtml(word.gloss || '')}" data-term="${escapeHtml(word.term)}"
            data-context="${escapeHtml(word.context || '')}" data-slug="${escapeHtml(word.article_slug || '')}"
            title="Looked up ${word.times} times">
          ${escapeHtml(word.term)} <span class="muted">${word.times}×</span></button>`).join('')}
      </div>
    </div>`);
  }

  view.innerHTML = parts.join('');

  const openShare = $('#btn-open-share', view);
  if (openShare) openShare.addEventListener('click', openSharedLesson);

  $$('[data-filter]', view).forEach((button) => button.addEventListener('click', () => {
    libraryFilter = button.dataset.filter;
    store.set('libraryFilter', libraryFilter);
    viewLibrary();
  }));
  $$('[data-register]', view).forEach((button) => button.addEventListener('click', () => {
    registerFilter = button.dataset.register;
    store.set('registerFilter', registerFilter);
    viewLibrary();
  }));

  const wireCards = (scope) => {
    $$('.card', scope).forEach((card) => {
      card.addEventListener('click', () => {
        location.hash = `#/read/${encodeURIComponent(card.dataset.slug)}`;
      });
      card.addEventListener('keydown', (event) => {
        if (event.target !== card) return;
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        location.hash = `#/read/${encodeURIComponent(card.dataset.slug)}`;
      });
    });
    $$('[data-remove-lesson]', scope).forEach((button) => button.addEventListener('click', async (event) => {
      event.stopPropagation();
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
        toast(`Removed. Your ${result.words_kept} saved words stay in the deck.`);
        viewLibrary();
      } catch (err) {
        button.disabled = false;
        button.dataset.armed = '0';
        button.classList.remove('danger');
        button.textContent = 'Remove';
        toast(err.message, 'err');
      }
    }));
  };
  wireCards(view);

  const search = $('#library-search');
  const match = $('#library-match');
  const filtered = () => data.articles.filter((a) => articleMatchesFilter(a, libraryFilter)
    && articleMatchesRegister(a, registerFilter) && articleMatchesQuery(a, libraryQuery.trim()));

  const paintMatch = (rows) => {
    if (!match) return;
    match.textContent = libraryQuery.trim()
      ? `${rows.length} of ${data.articles.length} match${rows.length === 1 ? '' : 'es'}`
      : (counts.reading ? `${counts.reading} part-read` : '');
  };

  const refilter = () => {
    const host = $('#library-shelves');
    if (!host) return;
    const rows = filtered();
    host.innerHTML = libraryShelvesHtml(rows, libraryQuery.trim());
    paintMatch(rows);
    wireCards(host);
    const clear = $('#library-search-clear');
    if (clear) clear.addEventListener('click', () => setQuery(''));
  };

  const setQuery = (value) => {
    libraryQuery = value;
    if (search) search.value = value;
    refilter();
    if (search) search.focus();
  };

  paintMatch(shown);
  if (search) {
    search.addEventListener('input', () => { libraryQuery = search.value; refilter(); });
    search.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      event.stopPropagation();
      setQuery('');
    });
  }

  $$('[data-worth]', view).forEach((button) => button.addEventListener('click', async (event) => {
    event.stopPropagation();
    try {
      const result = await api('/api/word/save', {
        method: 'POST',
        body: {
          term: button.dataset.term,
          lemma: button.dataset.worth,
          gloss: button.dataset.gloss || null,
          article_slug: button.dataset.slug || null,
          context: button.dataset.context || null,
        },
      });
      button.disabled = true;
      button.innerHTML = `${escapeHtml(button.dataset.term)} saved`;
      toast(`Saved. Your deck now holds ${result.total} words.`);
      refreshDuePill();
    } catch (err) { toast(err.message, 'err'); }
  }));
}
