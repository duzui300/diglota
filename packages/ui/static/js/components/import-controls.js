/**
 * Shared level / ratio / weave controls used by Import and Write -> Make a lesson.
 */

import { $, $$, escapeHtml } from '../lib/dom.js';
import { api } from '../lib/api.js';
import { store } from '../lib/state.js';
import { targetName } from '../lib/language.js';

let importOptions = null;

export const importChoice = {
  level: store.get('importLevel', 'auto'),
  ratio: store.get('importRatio', null),
  weave: store.get('importWeave', 'chunk'),
};

export async function loadImportOptions() {
  if (importOptions) return importOptions;
  importOptions = await api('/api/import/options');
  return importOptions;
}

/* The weave examples come from the tutor's own instructions, where a gloss is
   written (*like this*) so the model marks it in italics. That convention is
   useful in the prompt and not in the dialog, so it is rendered rather than
   shown as asterisks. */
export function weaveExampleHtml(text) {
  return escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\(\*(.+?)\*\)/g, '(<em>$1</em>)');
}

export function importControlsHtml(options) {
  const selected = importChoice.level || options.default_level;
  const preset = options.amounts.find((a) => Math.abs(a.ratio - (importChoice.ratio ?? -1)) < 0.001);
  const ratio = importChoice.ratio ?? (options.levels.find((l) => l.code === selected) || {}).suggested_ratio
    ?? 0.38;
  const langLabel = targetName();
  return `
    <div class="field">
      <label>What level should the ${escapeHtml(langLabel)} be?</label>
      <div class="chips" id="level-chips">
        ${options.levels.map((level) => `
          <button type="button" class="choice ${level.code === selected ? 'active' : ''}"
                  data-level="${escapeHtml(level.code)}" title="${escapeHtml(level.grammar)}">
            <span class="choice-code">${escapeHtml(level.code === 'auto' ? 'Auto' : level.code)}</span>
            <span class="choice-name">${escapeHtml(level.name)}</span>
          </button>`).join('')}
      </div>
    </div>
    <div class="field">
      <label for="import-ratio">How much ${escapeHtml(langLabel)}?
        <span class="ratio-value" id="ratio-value">${Math.round(ratio * 100)}%</span></label>
      <input type="range" id="import-ratio" min="${Math.round(options.min_ratio * 100)}"
             max="${Math.round(options.max_ratio * 100)}" step="1" value="${Math.round(ratio * 100)}">
      <div class="chips tight" id="amount-chips">
        ${options.amounts.map((a) => `
          <button type="button" class="choice sm ${preset && preset.label === a.label ? 'active' : ''}"
                  data-amount="${escapeHtml(a.ratio)}" title="${escapeHtml(a.note)}">${escapeHtml(a.label)}</button>`).join('')}
      </div>
    </div>
    ${weaveControlsHtml(options)}
    <p class="import-preview" id="import-preview"></p>`;
}

function weaveControlsHtml(options) {
  const weaves = options.weaves || [];
  if (weaves.length < 2) return '';
  const chosen = weaves.find((w) => w.code === importChoice.weave) || weaves[0];
  return `
    <div class="field">
      <label>How should the two languages mix?</label>
      <div class="chips" id="weave-chips">
        ${weaves.map((weave) => `
          <button type="button" class="choice ${weave.code === chosen.code ? 'active' : ''}"
                  data-weave="${escapeHtml(weave.code)}" title="${escapeHtml(weave.blurb)}">
            <span class="choice-name">${escapeHtml(weave.name)}</span>
          </button>`).join('')}
      </div>
      <p class="small muted" id="weave-blurb" style="margin:8px 0 6px">${escapeHtml(chosen.blurb)}</p>
      <pre class="weave-example" id="weave-example">${weaveExampleHtml(chosen.example)}</pre>
    </div>`;
}

export function wireImportControls(onChange, extraBody = {}) {
  const host = $('#modal-body');
  const refresh = async () => {
    try {
      const preview = await api('/api/import/preview', {
        method: 'POST',
        body: {
          level: importChoice.level,
          ratio: importChoice.ratio,
          weave: importChoice.weave,
          ...extraBody,
        },
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

  $$('[data-level]', host).forEach((button) => button.addEventListener('click', () => {
    importChoice.level = button.dataset.level;
    store.set('importLevel', importChoice.level);
    const suggested = (importOptions.levels.find((l) => l.code === importChoice.level) || {}).suggested_ratio;
    if (suggested) {
      importChoice.ratio = suggested;
      store.set('importRatio', suggested);
    }
    $$('[data-level]', host).forEach((b) => b.classList.toggle('active', b.dataset.level === importChoice.level));
    $$('[data-amount]', host).forEach((b) => b.classList.remove('active'));
    refresh();
  }));

  $$('[data-amount]', host).forEach((button) => button.addEventListener('click', () => {
    importChoice.ratio = parseFloat(button.dataset.amount);
    store.set('importRatio', importChoice.ratio);
    $$('[data-amount]', host).forEach((b) => b.classList.toggle('active', b === button));
    refresh();
  }));

  $$('[data-weave]', host).forEach((button) => button.addEventListener('click', () => {
    importChoice.weave = button.dataset.weave;
    store.set('importWeave', importChoice.weave);
    const chosen = (importOptions.weaves || []).find((w) => w.code === importChoice.weave);
    $$('[data-weave]', host).forEach((b) => b.classList.toggle('active', b === button));
    const blurb = $('#weave-blurb');
    if (blurb && chosen) blurb.textContent = chosen.blurb;
    const example = $('#weave-example');
    if (example && chosen) example.innerHTML = weaveExampleHtml(chosen.example);
    refresh();
  }));

  const slider = $('#import-ratio');
  if (slider) {
    slider.addEventListener('input', () => {
      importChoice.ratio = Number(slider.value) / 100;
      store.set('importRatio', importChoice.ratio);
      const label = $('#ratio-value');
      if (label) label.textContent = `${slider.value}%`;
      $$('[data-amount]', host).forEach((b) => b.classList.remove('active'));
    });
    slider.addEventListener('change', refresh);
  }
  refresh();
}
