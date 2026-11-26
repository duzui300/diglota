/**
 * Modals: Import, Discover, Recommendations, Settings, and Share/Export-Import.
 */

import { $, $$, escapeHtml, safeHttpUrl, toast } from '../lib/dom.js';
import { api } from '../lib/api.js';
import {
  REGISTER_LABELS,
  gradingInfo,
  prefs,
  refreshDuePill,
  savePref,
  statusInfo,
  store,
  themePageHex,
} from '../lib/state.js';
import { targetName } from '../lib/language.js';
import { closeModal, openModal } from '../components/overlays.js';
import { jobState, updateJob } from '../components/jobs.js';
import {
  importChoice,
  importControlsHtml,
  loadImportOptions,
  wireImportControls,
} from '../components/import-controls.js';

/* -- import ---------------------------------------------------------------- */

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
                placeholder="Select the article in your browser, copy it, and paste it here, headline and all."></textarea>
      <p class="small muted" style="margin:6px 0 0">Leave the blank lines between paragraphs: they are what
      tells the app where one paragraph ends. For a page the app cannot reach at all (a paywall, a reader
      that needs JavaScript), this is the way in.</p>
    </div>
    <div class="field">
      <label for="import-title">Title <span class="muted" id="import-title-note"></span></label>
      <input type="text" id="import-title" value="${escapeHtml(titleHint)}">
    </div>
    <div class="field" id="import-source-field" ${link ? 'hidden' : ''}>
      <label for="import-source-url">Original URL (optional)</label>
      <input type="url" id="import-source-url" placeholder="https://…, kept with the lesson for reference">
    </div>`;
}

function applyImportMode(mode) {
  importMode = mode;
  store.set('importMode', mode);
  $$('[data-way]').forEach((b) => b.classList.toggle('active', b.dataset.way === mode));
  const link = mode === 'link';
  const langLabel = targetName();
  $('#import-link-fields').hidden = !link;
  $('#import-paste-fields').hidden = link;
  $('#import-source-field').hidden = link;
  const lede = $('#import-lede');
  if (lede) {
    lede.textContent = link
      ? `The page is fetched, woven into ${langLabel}, and given post-reading notes. Then it joins your `
        + 'library like any other text. That takes a few minutes and runs in the background, so you '
        + 'can close this and keep reading.'
      : `What you paste is woven into ${langLabel} and given post-reading notes, with no fetching at all. `
        + 'So this works for a page the app cannot reach. It takes a few minutes and runs in the '
        + 'background, so you can close this and keep reading.';
  }
  const note = $('#import-title-note');
  if (note) {
    note.textContent = link
      ? '(optional, only if the page title comes out wrong)'
      : '(optional, the headline if it is not the first line)';
  }
  const focus = $(link ? '#import-url' : '#import-text');
  if (focus && !focus.value) focus.focus();
}

export async function openImport(prefill = '', titleHint = '') {
  openModal('Import', '<div id="import-form"><div class="empty"><span class="spinner"></span></div></div>');
  let options;
  try {
    options = await loadImportOptions();
  } catch (err) {
    $('#import-form').innerHTML = `<p class="small" style="color:var(--bad)">${escapeHtml(err.message)}</p>`;
    return;
  }

  if (prefill) importMode = 'link';
  const langLabel = targetName();

  $('#import-form').innerHTML = `
    ${importSourceHtml(prefill, titleHint)}
    ${importControlsHtml(options)}
    <div class="row"><button class="btn primary" id="import-go">Weave it into ${escapeHtml(langLabel)}</button>
      <span class="small muted">Best with long-form prose: essays, Wikipedia, magazine features.</span></div>
    <div id="import-job"></div>`;

  wireImportControls();
  applyImportMode(importMode);
  $$('[data-way]').forEach((button) => button.addEventListener('click',
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
      $('#import-job').innerHTML = `<div data-job-detail="${escapeHtml(response.job_id)}" style="margin-top:16px"></div>`;
      const job = await api(`/api/jobs/${encodeURIComponent(response.job_id)}`);
      updateJob(job);
      button.textContent = 'Importing. You can close this';
      button.classList.remove('primary');
      button.disabled = true;
    } catch (err) {
      toast(err.message, 'err');
      button.disabled = false;
      button.textContent = `Weave it into ${langLabel}`;
    }
  });
}

/* -- recommendations ------------------------------------------------------- */

function renderRecommendations(host, result) {
  const basis = result.basis || {};
  const candidates = (result.candidates || []).filter((c) => !c.error);
  const topics = result.topics || [];
  const langLabel = targetName();

  const topicList = topics.length
    ? `<div class="small muted" style="margin-bottom:12px">Looking for:
        ${topics.map((t) => `<span class="chip" title="${escapeHtml(t.why)}">${escapeHtml(t.query)}</span>`).join(' ')}</div>`
    : '';
  const basisLine = `<div class="small muted" style="margin-bottom:12px">${escapeHtml(basis.note || '')}
    ${basis.words ? `, matching against ${basis.words} saved word${basis.words === 1 ? '' : 's'}` : ''}</div>`;

  if (!candidates.length) {
    host.innerHTML = `${topicList}${basisLine}
      <p class="small muted">Nothing came back. Try importing a link directly instead.</p>`;
    return;
  }

  const best = candidates[0];
  host.innerHTML = `${topicList}${basisLine}
    ${basis.words ? `<div class="verdict correct" style="margin-bottom:12px">
      <div class="headline">Best match: ${Math.round(best.score * 100)}% of your words are likely to appear</div>
      <div class="small">${best.hits.slice(0, 14).map(escapeHtml).join(', ')}</div>
    </div>` : ''}
    <div class="results">
      ${candidates.map((c) => `
        <div class="result" data-url="${escapeHtml(c.url)}" data-title="${escapeHtml(c.title)}">
          <div class="t">${escapeHtml(c.title)}</div>
          <div class="s">${escapeHtml((c.snippet || '').slice(0, 210))}</div>
          <div class="u">
            ${c.words ? `${c.words} words, ` : ''}${escapeHtml(c.site)}
            ${c.hits && c.hits.length ? `, <span style="color:var(--accent-ink)">reuses ${c.hits.length}: ${escapeHtml(c.hits.slice(0, 6).join(', '))}</span>` : ''}
          </div>
        </div>`).join('')}
    </div>
    <p class="small muted" style="margin-top:12px">Pick one to weave into ${escapeHtml(langLabel)}. It takes a few minutes.</p>`;

  $$('.result', host).forEach((el) => el.addEventListener('click', () => {
    closeModal();
    openImport(el.dataset.url, el.dataset.title);
  }));
}

export function renderRecommendResult(jobId) {
  const host = $('#recommend-result');
  const job = jobState.get(jobId);
  if (!host || !job || !job.result) return;
  if (host.dataset.rendered === jobId) return;
  host.dataset.rendered = jobId;
  renderRecommendations(host, job.result);
}

/* -- discover -------------------------------------------------------------- */

let discoverState = { query: '', shown: [], rows: [] };

function discoverMoreHtml(more, query) {
  if (more) {
    return `<div class="row discover-more">
      <button class="btn ghost sm" id="discover-more"
              title="Ask for the results beyond these. The same search has more to give">Show different results</button>
    </div>`;
  }
  return `<p class="small muted discover-more">That is everything those sources have for
    “${escapeHtml(query)}” right now. Different words will find different articles.</p>`;
}

function discoverResult(row) {
  const have = row.already_have
    ? '<span class="chip accent" title="You already have this lesson">in your library</span>' : '';
  const register = row.register_label
    ? `<span class="chip" title="Judged from the source, not the text">${escapeHtml(row.register_label)}</span>` : '';
  return `<div class="result ${row.already_have ? 'have' : ''}" data-url="${escapeHtml(row.url)}"
       data-title="${escapeHtml(row.title)}" data-have="${row.already_have ? '1' : ''}">
    <div class="t">${escapeHtml(row.title)}</div>
    <div class="s">${escapeHtml(row.snippet || '')}</div>
    <div class="u">${escapeHtml(row.source)}, ${escapeHtml(row.site)} ${have}${register}</div>
  </div>`;
}

async function loadGaps(run) {
  const host = $('#discover-gaps');
  if (!host) return;
  let data;
  try {
    data = await api('/api/discover/gaps');
  } catch { return; }
  if (!data.gaps || !data.gaps.length) return;

  host.innerHTML = `<div class="gaps">
    <div class="small muted">You have never read</div>
    ${data.gaps.map((gap) => `
      <div class="gap" data-register="${escapeHtml(gap.register)}">
        <div class="gap-head">
          <strong>${escapeHtml(gap.label)}</strong>
          <span class="small muted">${gap.available} on your shelves, none opened</span>
        </div>
        <p class="small muted">${escapeHtml(gap.blurb || '')}</p>
        ${gap.unread.length ? `<div class="row">
          ${gap.unread.map((item) => `<a class="btn ghost sm" href="#/read/${encodeURIComponent(item.slug)}"
              title="${escapeHtml(item.title)}">${escapeHtml(item.title.length > 38 ? `${item.title.slice(0, 38)}…` : item.title)}</a>`).join('')}
        </div>` : ''}
        ${gap.query ? `<div class="row" style="margin-top:6px">
          <button class="btn ghost sm" data-search="${escapeHtml(gap.query)}">Search the web for “${escapeHtml(gap.topic)}”</button>
        </div>` : ''}
      </div>`).join('')}
  </div>`;

  $$('[data-search]', host).forEach((button) => button.addEventListener('click', () => run(button.dataset.search)));
  $$('a.btn', host).forEach((link) => link.addEventListener('click', closeModal));
}

export function openDiscover() {
  const canWeave = Boolean(statusInfo?.llm?.weave?.ready)
    || !gradingInfo
    || gradingInfo.generates !== false;
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
      discoverState.shown = discoverState.rows.map((row) => row.url);
      if (!discoverState.rows.length) {
        host.innerHTML = '<p class="muted small">Nothing found. Try different words, or paste a link directly.</p>';
        return;
      }
      host.innerHTML = `<div class="count-line">${discoverState.rows.length} result${
        discoverState.rows.length === 1 ? '' : 's'}</div>`
        + discoverState.rows.map(discoverResult).join('')
        + discoverMoreHtml(data.more, discoverState.query);
      $$('.result', host).forEach((el) => el.addEventListener('click', () => {
        if (el.dataset.have) return;
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
      $('#recommend-job').innerHTML = `<div data-job-detail="${escapeHtml(response.job_id)}" style="margin-top:12px"></div>`;
      const job = await api(`/api/jobs/${encodeURIComponent(response.job_id)}`);
      updateJob(job);
      button.textContent = 'Looking. You can close this';
    } catch (err) {
      button.disabled = false;
      button.textContent = 'Find something for my words';
      toast(err.message, 'err');
    }
  });
}

/* -- settings -------------------------------------------------------------- */

function roleOverrideHtml({ id, title, desc, cfg, urlPlaceholder, modelPlaceholder }) {
  const override = Boolean(cfg.override);
  const badge = override
    ? (cfg.ready
      ? `Custom: ${escapeHtml(cfg.effective_model || cfg.model || 'ready')}`
      : 'Custom (incomplete)')
    : 'Using default';
  const keyNote = cfg.key_set
    ? `In use: <code>${escapeHtml(cfg.key_hint || 'set')}</code>${cfg.from ? ` from your ${cfg.from === 'app' ? 'settings' : '.env'}` : ''}.`
    : (cfg.local
      ? 'Optional for localhost.'
      : (cfg.effective_key_set ? 'Inherited from default endpoint.' : 'None set (optional for localhost).'));

  return `
    <details class="settings-override" id="set-${id}-details" ${override ? 'open' : ''}>
      <summary>
        <span class="settings-override-title">${escapeHtml(title)}</span>
        <span class="small muted settings-override-badge">${badge}</span>
      </summary>
      <div class="settings-override-body">
        <p class="small muted" style="margin:0 0 12px">${desc}</p>
        <div class="field">
          <label for="set-${id}-base-url">API base URL <span class="muted">(optional — leave blank to use default endpoint)</span></label>
          <input type="text" id="set-${id}-base-url" autocapitalize="off" autocorrect="off" spellcheck="false"
                 placeholder="${escapeHtml(urlPlaceholder)}" value="${escapeHtml(cfg.base_url || '')}">
        </div>
        <div class="field">
          <label for="set-${id}-model">Model <span class="muted">(optional — leave blank to use default model)</span></label>
          <input type="text" id="set-${id}-model" autocapitalize="off" autocorrect="off" spellcheck="false"
                 placeholder="${escapeHtml(modelPlaceholder)}" value="${escapeHtml(cfg.model || '')}">
        </div>
        <div class="field">
          <label for="set-${id}-key">API key <span class="muted">${keyNote}</span></label>
          <input type="password" id="set-${id}-key" autocapitalize="off" autocorrect="off" spellcheck="false"
                 placeholder="${cfg.key_set ? 'leave blank to keep the one you have' : 'optional for localhost or same endpoint'}">
        </div>
        ${override ? `<div class="row" style="margin-top:8px">
          <button type="button" class="btn ghost sm" id="set-${id}-clear">Use default model</button>
        </div>` : ''}
      </div>
    </details>`;
}

export async function openSettings() {
  let current;
  try {
    current = await api('/api/settings');
  } catch (err) {
    toast(err.message, 'err');
    return;
  }
  const llm = current.llm || {};
  const lookup = current.lookup_llm || {};
  const weave = current.weave_llm || {};
  const warmMode = current.warm_glosses || 'off';
  const judge = current.judge || {};

  const statusChips = [];
  if (llm.ready) {
    statusChips.push(`<span class="chip ok">Default ready</span> ${escapeHtml(llm.model || '')}`);
  } else if (!lookup.ready && !weave.ready) {
    statusChips.push('<span class="chip warn">No model configured</span> Searching and reading still work.');
  }
  if (lookup.override) {
    statusChips.push(lookup.ready
      ? `<span class="chip ok">Lookup: ${escapeHtml(lookup.effective_model || '')}</span>`
      : '<span class="chip warn">Lookup incomplete</span>');
  }
  if (weave.override) {
    statusChips.push(weave.ready
      ? `<span class="chip ok">Weave: ${escapeHtml(weave.effective_model || '')}</span>`
      : '<span class="chip warn">Weave incomplete</span>');
  }
  if (judge.ready) statusChips.push('<span class="chip">Graded by Jev</span>');

  openModal('Settings', `
    <p class="small muted" style="margin-top:0">The default model handles quizzes, exercises, explanations,
    and — unless overridden below — word lookups and passage weaving. Everything else works without one:
    reading, saving words, review.${llm.from === 'env' ? ' These are currently coming from your <code>.env</code>;'
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
          ? `In use: <code>${escapeHtml(llm.key_hint || 'set')}</code>${llm.from ? ` from your ${llm.from === 'app' ? 'settings' : '.env'}` : ''}.`
          : (llm.local ? 'Optional for localhost.' : 'None set.')}</span></label>
      <input type="password" id="set-key" autocapitalize="off" autocorrect="off" spellcheck="false"
             placeholder="${llm.key_set ? 'leave blank to keep the one you have' : 'paste your key (optional for localhost)'}">
      <p class="small muted" style="margin:6px 0 0">Stored in <code>data/settings.json</code> on this machine,
      next to your library. It is sent only to your own endpoint, and never back to this page.</p>
    </div>
    ${roleOverrideHtml({
      id: 'lookup',
      title: 'Word lookup model (optional)',
      desc: 'Used when clicking words for glosses. Great for a fast local small model (e.g. Ollama or LM Studio at <code>http://localhost:11434/v1</code>) or a cheaper fast API model.',
      cfg: lookup,
      urlPlaceholder: 'e.g. http://localhost:11434/v1',
      modelPlaceholder: 'e.g. qwen3:4b or gpt-4o-mini',
    })}
    ${roleOverrideHtml({
      id: 'weave',
      title: 'Passage weave model (optional)',
      desc: 'Used when importing articles or turning your writing into diglot lessons. Set a stronger model here for higher weave quality while keeping lookup fast and cheap.',
      cfg: weave,
      urlPlaceholder: 'e.g. https://api.example.com/v1',
      modelPlaceholder: 'e.g. claude-sonnet-4 or gpt-4.1',
    })}
    <div class="field" style="margin-top:12px">
      <label for="set-warm-glosses">Background word pre-warming
        <span class="muted">Pre-look up unknown words when opening an article. Off by default to save tokens and keep local models responsive.</span>
      </label>
      <select id="set-warm-glosses">
        <option value="off" ${warmMode === 'off' ? 'selected' : ''}>Off — look up only when clicked (recommended)</option>
        <option value="auto" ${warmMode === 'auto' ? 'selected' : ''}>Auto — cloud endpoints only, off for localhost</option>
        <option value="on" ${warmMode === 'on' ? 'selected' : ''}>On — pre-warm up to 12 words per article</option>
      </select>
    </div>
    <div class="settings-state" id="settings-state">
      ${statusChips.join(' ')}
    </div>
    <div class="row" style="margin-top:14px">
      <button class="btn primary" id="settings-save">Save</button>
      <button class="btn ghost" id="settings-clear">Forget default key</button>
      <span class="small muted" id="settings-note"></span>
    </div>
    <p class="small muted" style="margin-bottom:0">Advanced: the TypeSafe key for graded exercises is read
    from <code>${escapeHtml(current.env_file || '.env')}</code> and left alone here${judge.ready ? '' : '. It is not set.'}</p>
    <div class="settings-pref">
      <div class="settings-pref-head">
        <h3>Reading background</h3>
        <span class="small muted" id="set-bg-note">${prefs.pageColor
          ? 'A custom colour is in use.' : `Following the ${escapeHtml(prefs.theme)} theme.`}</span>
      </div>
      <div class="row" style="margin-top:10px">
        <label class="settings-bg" for="set-bg">
          <input type="color" id="set-bg"
                 value="${escapeHtml(prefs.pageColor || themePageHex(prefs.theme))}">
          <span>Custom colour</span>
        </label>
        <button class="btn ghost sm" id="set-bg-reset" ${prefs.pageColor ? '' : 'hidden'}>Back to the theme colour</button>
      </div>
      <p class="small muted" style="margin:6px 0 0">The colour of the page behind the text. Ink switches
      between dark and light so the page stays readable.</p>
    </div>
    <div class="row settings-quit" style="margin-top:16px; padding-top:14px">
      <button class="btn ghost sm" id="settings-quit"
              title="Stop the app on this machine. Everything is already saved; start it again the same way you started it.">Quit the app</button>
      <span class="small muted">Nothing is lost. Every save is already on disk.</span>
    </div>`);

  const summarizeSaved = (fresh) => {
    const parts = [];
    if (fresh.llm?.ready) parts.push(`default: ${fresh.llm.model}`);
    if (fresh.lookup_llm?.override) {
      parts.push(fresh.lookup_llm.ready
        ? `lookup: ${fresh.lookup_llm.effective_model}`
        : 'lookup: incomplete');
    }
    if (fresh.weave_llm?.override) {
      parts.push(fresh.weave_llm.ready
        ? `weave: ${fresh.weave_llm.effective_model}`
        : 'weave: incomplete');
    }
    if (parts.length) return `Saved (${parts.join(', ')}).`;
    return 'Saved, but the model still needs a base URL, a model name, and (for remote endpoints) a key.';
  };

  const send = async (body, button, { reopen = false } = {}) => {
    const label = button.textContent;
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span> saving';
    try {
      const fresh = await api('/api/settings', { method: 'POST', body });
      toast(summarizeSaved(fresh));
      refreshDuePill();
      if (reopen) openSettings();
      else closeModal();
    } catch (err) {
      button.disabled = false;
      button.textContent = label;
      toast(err.message, 'err');
    }
  };

  $('#settings-save').addEventListener('click', () => {
    const body = {
      llm_base_url: $('#set-base-url').value.trim(),
      llm_model: $('#set-model').value.trim(),
      lookup_llm_base_url: $('#set-lookup-base-url').value.trim(),
      lookup_llm_model: $('#set-lookup-model').value.trim(),
      weave_llm_base_url: $('#set-weave-base-url').value.trim(),
      weave_llm_model: $('#set-weave-model').value.trim(),
      warm_glosses: $('#set-warm-glosses')?.value || 'off',
    };
    const defaultKey = $('#set-key').value.trim();
    if (defaultKey) body.llm_api_key = defaultKey;

    const lookupKey = $('#set-lookup-key').value.trim();
    if (lookupKey) body.lookup_llm_api_key = lookupKey;
    else if (!body.lookup_llm_base_url && !body.lookup_llm_model) body.lookup_llm_api_key = '';

    const weaveKey = $('#set-weave-key').value.trim();
    if (weaveKey) body.weave_llm_api_key = weaveKey;
    else if (!body.weave_llm_base_url && !body.weave_llm_model) body.weave_llm_api_key = '';

    send(body, $('#settings-save'));
  });
  $('#settings-clear').addEventListener('click', () =>
    send({ llm_api_key: '', llm_base_url: '', llm_model: '' }, $('#settings-clear'), { reopen: true }));
  $('#set-lookup-clear')?.addEventListener('click', () =>
    send({ lookup_llm_api_key: '', lookup_llm_base_url: '', lookup_llm_model: '' }, $('#set-lookup-clear'), { reopen: true }));
  $('#set-weave-clear')?.addEventListener('click', () =>
    send({ weave_llm_api_key: '', weave_llm_base_url: '', weave_llm_model: '' }, $('#set-weave-clear'), { reopen: true }));

  const bg = $('#set-bg');
  const bgNote = $('#set-bg-note');
  const bgReset = $('#set-bg-reset');
  const paintBgNote = () => {
    if (bgReset) bgReset.hidden = !prefs.pageColor;
    if (!bgNote) return;
    bgNote.textContent = prefs.pageColor
      ? 'A custom colour is in use.'
      : `Following the ${prefs.theme} theme.`;
  };
  if (bg) bg.addEventListener('input', () => { savePref('pageColor', bg.value); paintBgNote(); });
  if (bgReset) bgReset.addEventListener('click', () => {
    savePref('pageColor', '');
    if (bg) bg.value = themePageHex(prefs.theme);
    paintBgNote();
  });

  $('#settings-quit').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    if (button.dataset.armed !== '1') {
      button.dataset.armed = '1';
      button.classList.add('danger');
      button.textContent = 'Quit the app?';
      setTimeout(() => {
        if (button.dataset.armed === '1') {
          button.dataset.armed = '0';
          button.classList.remove('danger');
          button.textContent = 'Quit the app';
        }
      }, 3500);
      return;
    }
    button.disabled = true;
    try {
      await api('/api/quit', { method: 'POST' });
      closeModal();
      toast('Closed. Everything is saved. You can close this tab.');
    } catch (err) {
      button.disabled = false;
      button.dataset.armed = '0';
      button.classList.remove('danger');
      button.textContent = 'Quit the app';
      toast(err.message, 'err');
    }
  });
}

/* -- sharing a lesson ------------------------------------------------------ */

export function downloadShare(slug) {
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

export function openSharedLesson() {
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
    if (accept) accept.addEventListener('click', () => acceptShared(text));
  } catch (err) {
    host.innerHTML = `<div class="empty"><h3>Could not read that</h3><p>${escapeHtml(err.message)}</p></div>`;
  }
}

function shareReport(report) {
  if (!report.ok) {
    return `<div class="panel"><h3>That is not a diglot lesson</h3>
      <p class="panel-sub">${escapeHtml(report.error || 'It could not be read.')}</p>
      <p class="small muted">A lesson is Markdown with an "Article Identification &amp; Preview"
      front matter block, the woven text, and a post-reading section. A plain article, a web page or a
      screenshot needs importing by link instead.</p></div>`;
  }

  const stats = report.stats || {};
  const share = report.share || {};
  const known = report.known || {};
  const percent = Math.round((stats.spanish_ratio || 0) * 100);
  const langLabel = targetName();
  const safeReportUrl = safeHttpUrl(report.url);

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
      hand. It is not a reason to refuse it, just something to know.</div>`);
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
      <span>${percent}% ${escapeHtml(langLabel)}</span>
      <span>${stats.paragraphs || 0} paragraphs</span>
      ${report.register ? `<span>${escapeHtml(REGISTER_LABELS[report.register] || report.register)}</span>` : ''}
      ${report.level ? `<span>level ${escapeHtml(report.level)}</span>` : ''}
      ${report.anchor_count ? `<span>${report.anchor_count} vocabulary notes</span>` : ''}
      ${report.grammar_count ? `<span>${report.grammar_count} grammar notes</span>` : ''}
    </div>
    ${report.author ? `<p class="small muted">By ${escapeHtml(report.author)}${safeReportUrl ? `, <a href="${escapeHtml(safeReportUrl)}" target="_blank" rel="noopener">original</a>` : ''}</p>` : ''}
    ${report.preview ? `<p class="share-preview">${escapeHtml(report.preview)}</p>` : ''}
    ${(report.teaches || []).length ? `<div class="share-teaches"><div class="small muted">It teaches</div>
      <div class="row">${report.teaches.slice(0, 14).map((t) => `<span class="chip">${escapeHtml(t.es)}${t.en ? ` <span class="muted">${escapeHtml(t.en)}</span>` : ''}</span>`).join('')}</div></div>` : ''}
    ${taught}
    ${warnings.join('')}
    <div class="row share-actions">
      <button class="btn primary" id="share-accept">Add to my library</button>
      <span class="small muted">Saved as <code>${escapeHtml(report.suggested_filename || '')}</code></span>
    </div>
  </div>`;
}

async function acceptShared(text) {
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
