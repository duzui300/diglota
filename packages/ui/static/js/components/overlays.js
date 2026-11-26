/**
 * Overlays: modal dialog, word lookup / tutor popover, and reader selection toolbar.
 */

import { $, $$, escapeHtml, mdBlock, toast } from '../lib/dom.js';
import { api, localEntry } from '../lib/api.js';
import { refreshDuePill } from '../lib/state.js';
import { speak } from '../lib/speech.js';
import { selectionTarget } from '../lib/language.js';

let popoverAnchor = null;
let lookupController = null;
let readerSlug = null;
let selectionBarBound = false;
let overlaysInitialized = false;

export function openModal(title, bodyHtml) {
  $('#modal-title').textContent = title;
  $('#modal-body').innerHTML = bodyHtml;
  $('#modal').hidden = false;
}

export function closeModal() {
  const modal = $('#modal');
  if (modal) modal.hidden = true;
}

export function placePopover(anchor) {
  const pop = $('#popover');
  if (!pop || !anchor) return;
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

export function closePopover() {
  const pop = $('#popover');
  if (pop) pop.hidden = true;
  if (lookupController) {
    try { lookupController.abort(); } catch { /* ignore */ }
    lookupController = null;
  }
  popoverAnchor = null;
}

export function initOverlays() {
  if (overlaysInitialized || typeof document === 'undefined') return;
  overlaysInitialized = true;

  $('#modal-close')?.addEventListener('click', closeModal);
  $('#modal')?.addEventListener('click', (e) => { if (e.target.id === 'modal') closeModal(); });
  $('#pop-close')?.addEventListener('click', closePopover);

  /* Clicking anywhere else dismisses the panel.
   *
   * Bound on `mousedown` in the capture phase rather than on `click`, because
   * clicking a *different* word has to close this panel and open that one, and
   * both happen on the same gesture: mousedown closes, click reopens. A `click`
   * handler on the document would run after the word's own handler and shut the
   * panel it had just opened. */
  document.addEventListener('mousedown', (event) => {
    const pop = $('#popover');
    if (!pop || pop.hidden) return;
    const target = event.target;
    if (target.closest && (target.closest('#popover') || target.closest('#selection-bar'))) return;
    closePopover();
  }, true);
}

/* The popover doubles as the panel for selection actions, so a translation and
   a dictionary entry look and behave the same way. */
export function showLookupPanel(term, anchor) {
  const pop = $('#popover');
  popoverAnchor = anchor;
  $('#pop-term').textContent = term.length > 60 ? `${term.slice(0, 60)}…` : term;
  $('#pop-sub').textContent = 'working…';
  $('#pop-body').innerHTML = '<div class="loading"><span class="spinner"></span> asking the tutor</div>';
  $('#pop-actions').innerHTML = '';
  pop.hidden = false;
  placePopover(anchor);
}

export function setLookupPanel(term, sub, bodyHtml) {
  $('#pop-term').textContent = term.length > 60 ? `${term.slice(0, 60)}…` : term;
  $('#pop-sub').textContent = sub;
  $('#pop-body').innerHTML = bodyHtml;
}

export async function lookupWord(word, sentence, slug, anchor, savedTerm, options = {}) {
  const deep = options.deep === true;
  const fresh = options.fresh === true;
  if (lookupController) {
    try { lookupController.abort(); } catch { /* ignore */ }
  }
  const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
  lookupController = controller;

  const pop = $('#popover');
  popoverAnchor = anchor;
  $('#pop-term').textContent = word;
  $('#pop-sub').textContent = deep ? 'asking the tutor…' : 'looking up…';
  $('#pop-body').innerHTML = `<div class="loading"><span class="spinner"></span>
    ${deep ? 'asking the tutor for the full entry' : 'looking up'}</div>`;
  $('#pop-actions').innerHTML = '';
  pop.hidden = false;
  placePopover(anchor);

  const local = (deep || fresh) ? null : localEntry(word, slug);
  if (local) {
    paintEntry(local, { word, sentence, slug, anchor, savedTerm, provisional: true });
    placePopover(anchor);
  }

  let data;
  try {
    data = await api(`/api/word/lookup?term=${encodeURIComponent(word)}`
      + `&sentence=${encodeURIComponent(sentence)}`
      + `&slug=${encodeURIComponent(slug)}`
      + `${deep ? '&deep=true' : ''}${fresh ? '&fresh=true' : ''}`,
    controller ? { signal: controller.signal } : {});
  } catch (err) {
    if (err && err.name === 'AbortError') return;
    if (popoverAnchor !== anchor) return;
    if (local) {
      paintEntry(local, { word, sentence, slug, anchor, savedTerm, provisional: false });
      return;
    }
    $('#pop-sub').textContent = '';
    $('#pop-body').innerHTML = `<div class="muted small">${escapeHtml(err.message)}</div>`;
    $('#pop-actions').innerHTML = '<button class="btn sm ghost" id="pop-retry">Retry</button>';
    $('#pop-retry')?.addEventListener('click', () =>
      lookupWord(word, sentence, slug, anchor, savedTerm, { deep, fresh: true }));
    placePopover(anchor);
    return;
  }
  if (popoverAnchor !== anchor) return;   // the user moved on

  if (data.error) {
    if (local) {
      // Clear the provisional "full entry" spinner cleanly and keep the local entry usable.
      paintEntry(local, { word, sentence, slug, anchor, savedTerm, provisional: false });
      return;
    }
    $('#pop-sub').textContent = '';
    const detail = data.detail
      ? `<div class="muted small" style="margin-top:4px;opacity:.8">${escapeHtml(data.detail)}</div>`
      : '';
    $('#pop-body').innerHTML = `<div class="muted small">${escapeHtml(data.error)}</div>${detail}`;
    if (data.retryable) {
      $('#pop-actions').innerHTML = '<button class="btn sm ghost" id="pop-retry">Retry</button>';
      $('#pop-retry')?.addEventListener('click', () =>
        lookupWord(word, sentence, slug, anchor, savedTerm, { deep, fresh: true }));
    }
    placePopover(anchor);
    return;
  }
  paintEntry(data, { word, sentence, slug, anchor, savedTerm, provisional: false });
}

export function paintEntry(data, { word, sentence, slug, anchor, savedTerm, provisional }) {
  $('#pop-term').textContent = data.lemma || word;
  const source = data.source && data.source !== 'your deck'
    ? (data.source === 'focus' ? 'from this lesson' : `from ${data.source}`)
    : (data.source === 'your deck' ? 'from your deck' : '');
  $('#pop-sub').textContent = [
    data.pos,
    data.display && data.display !== data.lemma ? `as “${data.display}”` : '',
    source,
  ].filter(Boolean).join(', ');

  const bits = [];
  if (data.looked_up_before > 1) {
    bits.push(`<div class="nudge">You have looked this up ${data.looked_up_before} times.
      It is probably worth saving.</div>`);
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
    bits.push(`<div style="margin-top:9px" class="small muted">also: ${data.related.map(escapeHtml).join(', ')}</div>`);
  }
  if (data.quotes && data.quotes.length) {
    bits.push(`<div class="pop-quotes">
      <div class="small muted">In your quotes</div>
      ${data.quotes.slice(0, 2).map((quote) => `<div class="pop-quote">${escapeHtml(
        quote.es.length > 96 ? `${quote.es.slice(0, 96)}…` : quote.es)}</div>`).join('')}
      <a class="small" href="#/quotes">${data.quotes.length === 1 ? 'See that sentence' : 'See those sentences'}</a>
    </div>`);
  }
  if (provisional) {
    bits.push('<div class="loading" style="margin-top:8px"><span class="spinner"></span> full entry</div>');
  }
  $('#pop-body').innerHTML = bits.join('') || '<div class="muted small">No entry.</div>';

  if (provisional) {
    // Nothing to save yet: the model is about to return a lemma worth storing.
    $('#pop-actions').innerHTML = '<button class="btn sm ghost" id="pop-say">Hear it</button>';
    $('#pop-say').addEventListener('click', () => speak(word));
    return;
  }

  const lemmaKey = String(data.lemma || '').toLowerCase();
  const displayKey = String(data.display || data.term || word || '').toLowerCase();
  const already = Boolean(
    savedTerm && (
      (lemmaKey && savedTerm.has(lemmaKey))
      || (displayKey && savedTerm.has(displayKey))
    ),
  );
  const fromCorpus = !data.model && !data.error;
  $('#pop-actions').innerHTML = `
    <button class="btn sm ${already ? '' : 'primary'}" id="pop-save" ${already ? 'disabled' : ''}>
      ${already ? 'Saved' : 'Save word'}
    </button>
    <button class="btn sm ghost" id="pop-say">Hear it</button>
    ${fromCorpus
      ? '<button class="btn sm ghost" id="pop-deep" title="Ask the tutor for the lemma, an example and a note">Full entry</button>'
      : ''}
    <button class="btn sm ghost" id="pop-explain">Explain</button>`;

  if ($('#pop-deep')) {
    $('#pop-deep').addEventListener('click', () =>
      lookupWord(word, sentence, slug, anchor, savedTerm, { deep: true }));
  }
  $('#pop-explain').addEventListener('click', () => explainPassage(data.display || word, sentence, anchor));
  $('#pop-say').addEventListener('click', () => speak(data.display || word));

  $('#pop-save').addEventListener('click', async () => {
    try {
      const termToSave = data.display || data.term || word;
      const lemmaToSave = data.lemma || data.term || word.toLowerCase();
      const result = await api('/api/word/save', {
        method: 'POST',
        body: {
          term: termToSave,
          // A corpus entry carries no lemma of its own, so fall back to the form
          // the article indexed it under rather than the bare surface form the
          // reader happened to click.
          lemma: lemmaToSave,
          gloss: data.gloss || null,
          pos: data.pos || null,
          note: [data.sense, data.note].filter(Boolean).join('. ') || null,
          article_slug: slug,
          context: sentence,
        },
      });
      if (savedTerm && typeof savedTerm.add === 'function') {
        if (lemmaToSave) savedTerm.add(String(lemmaToSave).toLowerCase());
        if (termToSave) savedTerm.add(String(termToSave).toLowerCase());
        if (word) savedTerm.add(String(word).toLowerCase());
      }
      const saveBtn = $('#pop-save');
      if (saveBtn) {
        saveBtn.textContent = 'Saved';
        saveBtn.disabled = true;
        saveBtn.classList.remove('primary');
      }
      $$('.w').forEach((el) => {
        if (el.dataset.word && el.dataset.word.toLowerCase() === String(termToSave).toLowerCase()) {
          el.classList.add('saved');
        }
      });
      toast(`Saved “${data.lemma || word}”. Your deck now holds ${result.total} words.`);
      refreshDuePill();
    } catch (err) { toast(err.message, 'err'); }
  });

  // Reposition last so a taller full entry never overhangs the viewport bottom.
  placePopover(anchor);
}

/* Ask for a grammar explanation and append it to whichever panel is open --
   the word popover or the selection toolbar's. Kept as its own function
   because both call it. */
export async function explainPassage(text, sentence, anchor) {
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

/* selection toolbar -------------------------------------------------------- */

export function hideSelectionBar() {
  const bar = $('#selection-bar');
  if (bar) bar.hidden = true;
}

function showSelectionBar() {
  const bar = $('#selection-bar');
  if (!bar) return;
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

export function wireSelectionBar(slug) {
  readerSlug = slug;
  hideSelectionBar();

  if (selectionBarBound) return;
  selectionBarBound = true;

  $$('#selection-bar button').forEach((button) => button.addEventListener('mousedown', (event) => {
    event.preventDefault();   // keep the selection alive through the click
  }));

  $$('#selection-bar button').forEach((button) => button.addEventListener('click', async () => {
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

/* Keeping a sentence ------------------------------------------------------- */

function nodeOf(node) {
  return node && node.nodeType === 3 ? node.parentElement : node;
}

function sentenceText(el) {
  const clone = el.cloneNode(true);
  clone.querySelectorAll('.gloss').forEach((gloss) => gloss.remove());
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
  const from = all.indexOf(first);
  const to = all.indexOf(last);
  if (from < 0 || to < 0) return [];

  // Grouped by paragraph: a selection across two paragraphs is two quotes, and
  // each is located in the paragraph it actually came from.
  const groups = new Map();
  for (let i = Math.min(from, to); i <= Math.max(from, to); i += 1) {
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

export async function keepSelection(slug) {
  const parts = selectedSentences();
  if (!parts.length) { toast('Select a sentence first'); return; }
  window.getSelection().removeAllRanges();
  hideSelectionBar();

  let kept = 0;
  let already = 0;
  let reason = '';
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
