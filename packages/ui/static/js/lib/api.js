/**
 * Fetch wrapper and client-side local glossary cache.
 */

import { fold } from './dom.js';

let glossaryIndex = null;
let glossaryLoading = null;

export async function api(path, options = {}) {
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

export async function ensureGlossary() {
  if (glossaryIndex) return glossaryIndex;
  if (!glossaryLoading) {
    glossaryLoading = api('/api/glossary')
      .then((data) => { glossaryIndex = data.entries || {}; return glossaryIndex; })
      .catch(() => { glossaryIndex = {}; return glossaryIndex; });
  }
  return glossaryLoading;
}

export function localEntry(term, slug) {
  const candidates = glossaryIndex ? glossaryIndex[`w:${fold(term)}`] : null;
  if (!candidates || !candidates.length) return null;
  // The lesson being read wins, as it does on the server: a form can be glossed
  // differently by two lessons, and answering with the other one's sense is the same
  // failure as answering with the wrong word.
  return candidates.find((entry) => entry.article && entry.article === slug) || candidates[0];
}
