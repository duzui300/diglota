/**
 * DOM, formatting, and sanitization helpers.
 */

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export const escapeHtml = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

export const escapeRegExp = (text) => String(text ?? '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/**
 * Validate external article/source URLs before putting them in <a href>.
 * Blocks javascript:, data:, vbscript:, and malformed URLs.
 */
export function safeHttpUrl(url) {
  const raw = String(url || '').trim();
  if (!raw) return '';
  try {
    const parsed = new URL(raw);
    return (parsed.protocol === 'http:' || parsed.protocol === 'https:') ? parsed.href : '';
  } catch {
    return '';
  }
}

export const fold = (text) => String(text ?? '').toLowerCase()
  .normalize('NFD').replace(/\p{M}/gu, '');

/* The corpus and the tutor both write light markdown inside prose -- grammar
   notes quote **será** and *volverse*, vocabulary examples quote the lesson.
   Escape first, then re-introduce only the inline tags we intend, so article
   text can never inject markup. */
export function mdInline(text) {
  return escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
}

/* Explanations come back as a handful of `- **Label:** text` lines. Rendered as
   plain text the dashes read as punctuation soup in a narrow panel, so leading
   bullets become actual list rows. */
export function mdBlock(text) {
  return String(text || '').split(/\n+/).map((line) => {
    const trimmed = line.trim();
    if (!trimmed) return '';
    const bullet = trimmed.match(/^[-*]\s+(.*)$/);
    return bullet ? `<div class="li">${mdInline(bullet[1])}</div>` : `<div>${mdInline(trimmed)}</div>`;
  }).join('');
}

export function shortenText(text, max) {
  const value = String(text || '');
  if (value.length <= max) return value;
  return `${value.slice(0, max).replace(/\s+\S*$/, '')}…`;
}

export function hostOf(url) {
  try { return new URL(url).host.replace(/^www\./, ''); } catch { return url; }
}

export function toast(message, kind = '') {
  const container = $('#toasts');
  if (!container) return;
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = message;
  container.append(el);
  setTimeout(() => {
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 250);
  }, kind === 'err' ? 5200 : 2600);
}
