/**
 * Diglot client entry point: hash router, global keyboard shortcuts, toolbar bindings, and startup.
 */

import { $, $$, escapeHtml, toast } from './lib/dom.js';
import { dictionaryForm, targetName } from './lib/language.js';
import {
  applyPrefs,
  prefs,
  refreshDuePill,
  runRouteCleanups,
  savePref,
} from './lib/state.js';
import { initSpeech, reader, speak, stopReading, toggleReading } from './lib/speech.js';
import { closeModal, closePopover, initOverlays } from './components/overlays.js';
import { closeActivity, connectJobStream, initJobs, loadJobs, onJobUpdate } from './components/jobs.js';
import { viewLibrary } from './views/library.js';
import { currentReaderProgress, saveProgress, viewReader } from './views/reader.js';
import {
  currentReviewCard,
  gradeCard,
  isReviewRevealed,
  revealCard,
  viewCards,
  viewReview,
} from './views/review.js';
import { viewQuotes } from './views/quotes.js';
import { viewWrite } from './views/write.js';
import { viewStats } from './views/stats.js';
import {
  openDiscover,
  openImport,
  openSettings,
  renderRecommendResult,
} from './modals/modals.js';

// Exposed globally for tools/ui_test.py compatibility.
window.dictionaryForm = dictionaryForm;

async function route() {
  runRouteCleanups();
  closePopover();
  closeActivity();
  if (!location.hash.startsWith('#/read/')) stopReading();
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

onJobUpdate((job, { justSettled }) => {
  if (justSettled && job.status === 'done' && (location.hash === '' || location.hash === '#/library')) {
    viewLibrary();
  }
  if (job.kind === 'recommend') renderRecommendResult(job.id);
});

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
    if (['1', '2', '3', '4'].includes(event.key) && isReviewRevealed()) {
      event.preventDefault();
      gradeCard(['again', 'hard', 'good', 'easy'][+event.key - 1]);
      return;
    }
    if (event.key.toLowerCase() === 's') {
      const card = currentReviewCard();
      if (card) speak(card.term);
      return;
    }
  }

  if (location.hash.startsWith('#/read/')) {
    const langLabel = targetName();
    const key = event.key.toLowerCase();
    if (key === 'r') { toggleReading(); return; }
    if (key === 'g') { savePref('gloss', !prefs.gloss); toast(prefs.gloss ? 'Glosses on' : 'Glosses off'); return; }
    if (key === 'f') { savePref('dim', !prefs.dim); toast(prefs.dim ? `Focusing ${langLabel}` : 'English at full strength'); return; }
    if (key === 'e') { savePref('esOnly', !prefs.esOnly); toast(prefs.esOnly ? `${langLabel} only` : 'English shown'); return; }
    if (key === 's') {
      const selection = window.getSelection();
      if (selection && selection.toString().trim()) speak(selection.toString().trim());
    }
  }
});

$('#opt-gloss').addEventListener('change', (e) => savePref('gloss', e.target.checked));
$('#opt-dim').addEventListener('change', (e) => savePref('dim', e.target.checked));
$('#opt-es-only').addEventListener('change', (e) => savePref('esOnly', e.target.checked));
$('#font-up').addEventListener('click', () => savePref('size', Math.min(prefs.size + 1, 26)));
$('#font-down').addEventListener('click', () => savePref('size', Math.max(prefs.size - 1, 15)));
$('#btn-import').addEventListener('click', () => openImport());
$('#btn-discover').addEventListener('click', openDiscover);
$('#btn-settings').addEventListener('click', openSettings);

$('#reader-back').addEventListener('click', async () => {
  const slug = location.hash.replace('#/read/', '');
  await saveProgress(decodeURIComponent(slug), false);
  location.hash = '#/library';
});

$('#btn-read-aloud').addEventListener('click', toggleReading);

window.addEventListener('beforeunload', () => {
  if (location.hash.startsWith('#/read/')) {
    const slug = decodeURIComponent(location.hash.replace('#/read/', ''));
    const payload = currentReaderProgress(slug);
    if (payload) {
      navigator.sendBeacon?.(`/api/article/${encodeURIComponent(slug)}/progress`,
        new Blob([JSON.stringify(payload)], { type: 'application/json' }));
    }
  }
});

$$('.theme-switch button').forEach((button) =>
  button.addEventListener('click', () => {
    if (prefs.pageColor) savePref('pageColor', '');
    savePref('theme', button.dataset.themeSet);
  }));

initSpeech();
initOverlays();
initJobs();
applyPrefs();
route();
refreshDuePill();
setInterval(refreshDuePill, 60000);
loadJobs();
connectJobStream();
