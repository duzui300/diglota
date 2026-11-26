/**
 * Shared client preferences, constants, status/grading state, and route cleanup registry.
 */

import { $, $$ } from './dom.js';
import { api } from './api.js';
import { hydrateLanguage, loadLanguagePair } from './language.js';

export const store = {
  get(key, fallback) {
    try {
      const raw = localStorage.getItem(`diglot.${key}`);
      return raw === null ? fallback : JSON.parse(raw);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try { localStorage.setItem(`diglot.${key}`, JSON.stringify(value)); } catch { /* private mode */ }
  },
};

export const prefs = {
  theme: store.get('theme', 'paper'),
  pageColor: store.get('pageColor', ''),
  gloss: store.get('gloss', true),
  dim: store.get('dim', false),
  esOnly: store.get('esOnly', false),
  size: store.get('size', 19.5),
};

export const STAGE_ORDER = [
  ['new', 'New', 'var(--stage-1)'],
  ['learning', 'Learning', 'var(--stage-2)'],
  ['young', 'Young', 'var(--stage-3)'],
  ['mature', 'Mature', 'var(--stage-4)'],
];

export const REGISTER_LABELS = {
  news: 'News',
  essay: 'Essay',
  conversation: 'Conversation',
  academic: 'Academic',
  fiction: 'Fiction',
  unspecified: 'Other',
};

// The taxonomy's order, so the filter row does not reshuffle as the library
// grows -- a moving row has to be re-read every time.
export const REGISTER_ORDER = ['news', 'essay', 'conversation', 'academic', 'fiction', 'unspecified'];

function relativeLuminance(hex) {
  const match = /^#?([0-9a-f]{6})$/i.exec(String(hex || ''));
  if (!match) return 0.6;
  const value = parseInt(match[1], 16);
  const channel = (part) => {
    const c = part / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel((value >> 16) & 255)
    + 0.7152 * channel((value >> 8) & 255)
    + 0.0722 * channel(value & 255);
}

function pageTone(hex) {
  return relativeLuminance(hex) < 0.18 ? 'dark' : 'light';
}

export function themePageHex(theme) {
  return { paper: '#e9ece0', sepia: '#e4d6bd', night: '#151d17' }[theme] || '#e9ece0';
}

export function applyPrefs() {
  if (typeof document === 'undefined') return;
  document.documentElement.dataset.theme = prefs.theme;
  if (prefs.pageColor) {
    document.documentElement.style.setProperty('--page', prefs.pageColor);
    document.documentElement.dataset.pageTone = pageTone(prefs.pageColor);
  } else {
    document.documentElement.style.removeProperty('--page');
    delete document.documentElement.dataset.pageTone;
  }
  document.documentElement.style.setProperty('--reader-size', `${prefs.size}px`);
  document.body.classList.toggle('hide-gloss', !prefs.gloss);
  document.body.classList.toggle('dim-english', prefs.dim);
  document.body.classList.toggle('es-only', prefs.esOnly);
  $$('.theme-switch button').forEach((b) => b.classList.toggle('active',
    !prefs.pageColor && b.dataset.themeSet === prefs.theme));
  const g = $('#opt-gloss');
  const d = $('#opt-dim');
  const e = $('#opt-es-only');
  if (g && d && e) {
    g.checked = prefs.gloss;
    d.checked = prefs.dim;
    e.checked = prefs.esOnly;
  }
}

export function savePref(key, value) {
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
export function reportMode(key, value) {
  const kinds = {
    gloss: value ? 'gloss-on' : 'gloss-off',
    esOnly: value ? 'spanish-only' : null,
    dim: value ? 'focus' : null,
  };
  const kind = kinds[key];
  if (!kind || typeof navigator === 'undefined' || !navigator.sendBeacon) return;
  const slug = location.hash.startsWith('#/read/')
    ? decodeURIComponent(location.hash.replace('#/read/', '')) : null;
  try {
    navigator.sendBeacon('/api/scaffolding',
      new Blob([JSON.stringify({ kind, slug })], { type: 'application/json' }));
  } catch { /* telemetry must never break the reader */ }
}

export let gradingInfo = null;
export let statusInfo = null;

export function gradingNote() {
  if (!gradingInfo || gradingInfo.primary === 'jev') return '';
  return `<div class="grading-note">No calibrated judge is configured. The tutor model
    grades these, so the scores are rougher than usual.</div>`;
}

export async function refreshDuePill() {
  try {
    const status = await api('/api/status');
    statusInfo = status;
    const pill = $('#due-pill');
    if (pill) {
      pill.hidden = !status.due;
      pill.textContent = status.due;
    }
    gradingInfo = status.grading || null;
    hydrateLanguage(status);
    if (status && status.capabilities && status.capabilities.languages) {
      await loadLanguagePair(api, '/api/languages');
    }
  } catch { /* the pill is not worth an error toast */ }
}

export function setActiveTab(name) {
  $$('.tabs a').forEach((a) => a.classList.toggle('active', a.dataset.tab === name));
}

const routeCleanups = new Set();

/**
 * Register a cleanup callback to be invoked on the next hash route transition.
 * Returns an unsubscribe function if a view cleans up earlier (e.g. in-page re-render).
 */
export function registerRouteCleanup(fn) {
  if (typeof fn !== 'function') return () => {};
  routeCleanups.add(fn);
  return () => {
    routeCleanups.delete(fn);
  };
}

export function runRouteCleanups() {
  for (const fn of [...routeCleanups]) {
    routeCleanups.delete(fn);
    try { fn(); } catch { /* cleanup must never block navigation */ }
  }
}
