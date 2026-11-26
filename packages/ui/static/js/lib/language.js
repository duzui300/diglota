/**
 * Language-pair profile & script-aware text helpers.
 *
 * Defaults to `en-es` and can hydrate from `/api/status` or `/api/languages`
 * payloads when available.
 */

import { escapeHtml, escapeRegExp, fold } from './dom.js';

const DEFAULT_BASE_STOPWORDS = [
  'the', 'and', 'for', 'that', 'with', 'this', 'from', 'have', 'been', 'were',
  'are', 'was', 'will', 'would', 'could', 'should', 'they', 'them', 'their',
  'there', 'here', 'what', 'when', 'where', 'which', 'while', 'whose', 'about',
  'into', 'over', 'than', 'then', 'more', 'most', 'some', 'such', 'only',
  'also', 'just', 'very', 'much', 'many', 'other', 'these', 'those', 'your',
  'our', 'ours', 'its', 'does', 'did', 'doing', 'done', 'make', 'makes', 'made',
  'a', 'an', 'of', 'in', 'on', 'at', 'by', 'to', 'as', 'is', 'am', 'it', 'or',
  'but', 'not', 'no', 'nor', 'so', 'if', 'be', 'being', 'do', 'has', 'had',
  'can', 'may', 'might', 'must', 'my', 'me', 'we', 'us', 'he', 'she', 'his',
  'her', 'him', 'one', 'two', 'all', 'any', 'out', 'up', 'down', 'off', 'own',
  'same', 'too', 'why', 'how', 'who', 'whom', 'both', 'each', 'few', 'i',
];

const DEFAULT_SPANISH_STOPWORDS = [
  'el', 'la', 'los', 'las', 'de', 'que', 'en', 'con', 'para', 'por',
  'un', 'una', 'es', 'son', 'se', 'del', 'al', 'y', 'o', 'su',
  'me', 'te', 'nos', 'os', 'lo', 'le',
];

const DEFAULT_PAIR = {
  id: 'en-es',
  label: 'Spanish in English',
  base: {
    code: 'en',
    name: 'English',
    native_name: 'English',
    script: 'latin',
    word_spaced: true,
    cased: true,
    proficiency_scale: 'CEFR',
    tts_locales: ['en-US', 'en-GB', 'en'],
    tts_rate: 1.0,
    stopwords: DEFAULT_BASE_STOPWORDS,
  },
  target: {
    code: 'es',
    name: 'Spanish',
    native_name: 'Español',
    script: 'latin',
    word_spaced: true,
    cased: true,
    proficiency_scale: 'CEFR',
    tts_locales: ['es-ES', 'es-MX', 'es-US', 'es-AR', 'es-CO', 'es'],
    tts_rate: 0.92,
    accent_chars: 'áéíóúñü¿¡ÁÉÍÓÚÑÜ',
    stopwords: DEFAULT_SPANISH_STOPWORDS,
  },
  write_placeholder: 'Escribe aquí. No busques las palabras, usa las que tienes.',
};

const DEFAULT_BLANK_STOPWORDS = new Set([
  'se', 'me', 'te', 'nos', 'os', 'lo', 'la', 'le', 'los', 'las',
  'el', 'un', 'una', 'de', 'del', 'a', 'al', 'en', 'con', 'por', 'para', 'que', 'y', 'o', 'su',
]);

const SCRIPT_PATTERNS = {
  cjk: /[\u4e00-\u9fff\u3400-\u4dbf]/,
  kana: /[\u3040-\u30ff\u4e00-\u9fff]/,
  hangul: /[\uac00-\ud7af\u1100-\u11ff]/,
  cyrillic: /[\u0400-\u04ff]/,
  arabic: /[\u0600-\u06ff]/,
  greek: /[\u0370-\u03ff]/,
};

const CJK_CHAR_RE = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u;
const TRIM_PUNCT_RE = /^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu;

let activePair = clonePair(DEFAULT_PAIR);

function clonePair(pair) {
  return {
    ...pair,
    base: { ...pair.base, tts_locales: [...(pair.base.tts_locales || [])], stopwords: [...(pair.base.stopwords || [])] },
    target: { ...pair.target, tts_locales: [...(pair.target.tts_locales || [])], stopwords: [...(pair.target.stopwords || [])] },
  };
}

function mergeProfile(fallback, incoming) {
  if (!incoming || typeof incoming !== 'object') return { ...fallback };
  return {
    ...fallback,
    ...incoming,
    tts_locales: Array.isArray(incoming.tts_locales) && incoming.tts_locales.length
      ? [...incoming.tts_locales]
      : [...(fallback.tts_locales || [])],
    stopwords: Array.isArray(incoming.stopwords)
      ? [...incoming.stopwords]
      : [...(fallback.stopwords || [])],
  };
}

export function getLanguagePair() {
  return activePair;
}

export function setLanguagePair(pair) {
  if (!pair || typeof pair !== 'object') return activePair;
  const baseDefaults = pair.base && pair.base.code && pair.base.code !== DEFAULT_PAIR.base.code
    ? { code: 'en', name: 'English', native_name: 'English', script: 'latin', word_spaced: true, cased: true, tts_locales: [], tts_rate: 1.0, stopwords: [] }
    : DEFAULT_PAIR.base;
  const targetDefaults = pair.target && pair.target.code && pair.target.code !== DEFAULT_PAIR.target.code
    ? { code: 'es', name: 'Spanish', native_name: 'Español', script: 'latin', word_spaced: true, cased: true, tts_locales: [], tts_rate: 0.92, accent_chars: '', stopwords: [] }
    : DEFAULT_PAIR.target;

  activePair = {
    ...DEFAULT_PAIR,
    ...pair,
    base: mergeProfile(baseDefaults, pair.base),
    target: mergeProfile(targetDefaults, pair.target),
  };
  return activePair;
}

/**
 * Hydrate language pair metadata from `/api/status` or `/api/languages` responses.
 */
export function hydrateLanguage(payload) {
  if (!payload || typeof payload !== 'object') return activePair;
  const candidate = payload.active || payload.pair || payload.language_pair || payload.language
    || (payload.base && payload.target ? payload : null);
  if (candidate && (candidate.base || candidate.target)) {
    return setLanguagePair(candidate);
  }
  return activePair;
}

export async function loadLanguagePair(fetcher, url = '/api/languages') {
  if (typeof fetcher !== 'function') return activePair;
  try {
    const data = await fetcher(url);
    return hydrateLanguage(data);
  } catch {
    return activePair;
  }
}

export const targetCode = () => activePair.target.code || 'es';
export const baseCode = () => activePair.base.code || 'en';
export const targetName = () => activePair.target.name || 'Spanish';
export const baseName = () => activePair.base.name || 'English';
export const writePlaceholder = () => activePair.write_placeholder || DEFAULT_PAIR.write_placeholder;

export function isTargetLang(lang) {
  return String(lang || '') === targetCode();
}

function ttsProfile(role) {
  return (role === 'base' || role === baseCode()) ? activePair.base : activePair.target;
}

/**
 * Pick the best SpeechSynthesisVoice using the profile's `tts_locales` list.
 */
export function pickVoiceFor(voices, role) {
  const list = Array.isArray(voices) ? voices : [];
  if (!list.length) return null;
  const profile = ttsProfile(role);
  const locales = profile.tts_locales && profile.tts_locales.length
    ? profile.tts_locales
    : [profile.code].filter(Boolean);

  const normalize = (tag) => String(tag || '').replace(/_/g, '-').toLowerCase();
  const specific = locales.filter((loc) => loc.includes('-') || loc.includes('_'));

  for (const loc of specific) {
    const want = normalize(loc);
    const hit = list.find((v) => normalize(v.lang) === want);
    if (hit) return hit;
  }

  const code = normalize(profile.code || '');
  if (code) {
    const prefixRe = new RegExp(`^${escapeRegExp(code)}(?:$|-)`, 'i');
    const regionalHit = specific.length
      ? list.find((v) => prefixRe.test(normalize(v.lang))
          && specific.some((loc) => normalize(v.lang).includes(normalize(loc).split('-')[1])))
      : null;
    if (regionalHit) return regionalHit;
    const anyHit = list.find((v) => prefixRe.test(normalize(v.lang)));
    if (anyHit) return anyHit;
  }

  for (const loc of locales) {
    const want = normalize(loc);
    const re = new RegExp(`^${escapeRegExp(want)}(?:$|-)`, 'i');
    const hit = list.find((v) => re.test(normalize(v.lang)));
    if (hit) return hit;
  }
  return null;
}

export function ttsLangFor(role, voice) {
  if (voice && voice.lang) return voice.lang;
  const profile = ttsProfile(role);
  return (profile.tts_locales && profile.tts_locales[0]) || profile.code || 'en-US';
}

export function ttsRateFor(role) {
  const profile = ttsProfile(role);
  if (typeof profile.tts_rate === 'number') return profile.tts_rate;
  return (role === 'base' || role === baseCode()) ? 1.0 : 0.92;
}

export const SENTENCE_END = /(?:[.!?…]["'”’)\]]*|[。！？][」』”’"')】）\]]*)\s*$/;

const SENTENCE_SPLIT_RE = /(?<=[.!?…]["'”’)\]]*)\s+|(?<=[。！？][」』”’"')】）\]]*)\s+|(?<=[。！？][」』”’"')】）\]]*)(?=[^\s])/u;

/**
 * Split a span's text into sentence-bounded pieces (works for Latin and CJK).
 */
export function splitSentencePieces(text) {
  return String(text ?? '').split(SENTENCE_SPLIT_RE).filter((piece) => piece.length);
}

function isNonSpacedProfile(profile, text = '') {
  if (profile && profile.word_spaced === false) return true;
  if (profile && (profile.script === 'cjk' || profile.script === 'kana')) return true;
  return CJK_CHAR_RE.test(text);
}

/**
 * Split target-language text into renderable segments `{ text, isWord, word }`.
 * Strips leading/trailing punctuation from `word`, preserves whitespace, and
 * uses `Intl.Segmenter` for non-spaced CJK scripts.
 */
export function splitTargetTokens(text) {
  const raw = String(text ?? '');
  if (!raw) return [];

  if (isNonSpacedProfile(activePair.target, raw)) {
    if (typeof Intl !== 'undefined' && typeof Intl.Segmenter === 'function') {
      const seg = new Intl.Segmenter(targetCode() || 'zh', { granularity: 'word' });
      const out = [];
      for (const part of seg.segment(raw)) {
        if (!part.segment) continue;
        if (!part.isWordLike) {
          out.push({ text: part.segment, isWord: false, word: '' });
          continue;
        }
        const clean = part.segment.replace(TRIM_PUNCT_RE, '');
        out.push({ text: part.segment, isWord: Boolean(clean), word: clean });
      }
      return out;
    }
    const fallbackRe = /([\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]|[\p{L}\p{N}]+|\s+|[^\s\p{L}\p{N}]+)/gu;
    const parts = raw.match(fallbackRe) || [raw];
    return parts.map((part) => {
      if (!part.trim()) return { text: part, isWord: false, word: '' };
      const clean = part.replace(TRIM_PUNCT_RE, '');
      return { text: part, isWord: Boolean(clean), word: clean };
    });
  }

  return raw.split(/(\s+)/).map((part) => {
    if (!part) return null;
    if (!part.trim()) return { text: part, isWord: false, word: '' };
    const clean = part.replace(TRIM_PUNCT_RE, '');
    return { text: part, isWord: true, word: clean };
  }).filter(Boolean);
}

/**
 * Extract clean lexical tokens for spaced or non-spaced scripts.
 */
export function tokenizeWords(text, role = 'target') {
  const profile = ttsProfile(role);
  const raw = String(text ?? '');
  if (!raw.trim()) return [];
  if (isNonSpacedProfile(profile, raw)) {
    if (typeof Intl !== 'undefined' && typeof Intl.Segmenter === 'function') {
      const seg = new Intl.Segmenter(profile.code || 'zh', { granularity: 'word' });
      const words = [];
      for (const part of seg.segment(raw)) {
        if (!part.isWordLike) continue;
        const clean = part.segment.replace(TRIM_PUNCT_RE, '');
        if (clean) words.push(clean);
      }
      return words;
    }
    return raw.match(/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]|[\p{L}\p{N}]+/gu) || [];
  }
  return raw.split(/[\s/,(]+/)
    .map((w) => w.replace(TRIM_PUNCT_RE, ''))
    .filter(Boolean);
}

/**
 * Guess which language a reader's selected passage should be translated INTO.
 */
export function selectionTarget(text) {
  const sample = String(text || '');
  const target = activePair.target;
  const base = activePair.base;

  if (target.script && base.script && target.script !== base.script) {
    const scriptRe = SCRIPT_PATTERNS[target.script];
    if (scriptRe && scriptRe.test(sample)) return base.name;
    return target.name;
  }

  if (target.accent_chars) {
    const accentRe = new RegExp(`[${escapeRegExp(target.accent_chars)}]`);
    if (accentRe.test(sample)) return base.name;
  }

  const baseSet = new Set((base.stopwords || DEFAULT_BASE_STOPWORDS).map((w) => fold(w)));
  const rawCues = target.code === 'es'
    ? DEFAULT_SPANISH_STOPWORDS
    : (target.stopwords || []);
  const cues = rawCues.filter((w) => w && !baseSet.has(fold(w)));

  if (cues.length) {
    const cuePattern = cues.map((w) => escapeRegExp(w)).join('|');
    const cueRe = new RegExp(`(?<![\\p{L}\\p{N}\\p{M}])(?:${cuePattern})(?![\\p{L}\\p{N}\\p{M}])`, 'iu');
    if (cueRe.test(sample)) return base.name;
  }

  return target.name;
}

function activeBlankStopwords() {
  const stop = new Set(DEFAULT_BLANK_STOPWORDS);
  for (const w of activePair.target.stopwords || []) {
    if (w) stop.add(fold(w));
  }
  return stop;
}

/**
 * Blank the target word or multi-word construction out of a sentence.
 */
export function blankOut(sentence, term) {
  const text = String(sentence ?? '');
  const targetTerm = String(term ?? '').trim();
  if (!text || !targetTerm) return null;

  if (isNonSpacedProfile(activePair.target, targetTerm) || CJK_CHAR_RE.test(targetTerm)) {
    const cleanTerm = targetTerm.replace(TRIM_PUNCT_RE, '');
    if (cleanTerm && text.includes(cleanTerm)) {
      return { sentence: text.replace(cleanTerm, '_____'), kind: 'word' };
    }
    const tokens = tokenizeWords(targetTerm, 'target');
    if (tokens.length >= 2) {
      const first = tokens[0];
      const last = tokens[tokens.length - 1];
      const pattern = new RegExp(`(${escapeRegExp(first)}[^。！？.!?]{0,24}?${escapeRegExp(last)})`, 'u');
      if (pattern.test(text)) {
        return { sentence: text.replace(pattern, '_____'), kind: 'phrase' };
      }
    }
    for (const candidate of tokens) {
      if (text.includes(candidate)) {
        return { sentence: text.replace(candidate, '_____'), kind: 'word' };
      }
    }
    return null;
  }

  const clean = (word) => word.replace(/[.,;:!?¡¿"“”]/g, '');
  const words = targetTerm.split(/[\s/,(]+/).map(clean).filter(Boolean);
  const stopwords = activeBlankStopwords();
  const content = words.filter((w) => w.length >= 3 && !stopwords.has(fold(w)));

  if (content.length >= 2) {
    const first = content[0];
    const last = content[content.length - 1];
    const firstHead = first.slice(0, Math.max(4, first.length - 2));
    const lastHead = last.slice(0, Math.max(4, last.length - 2));
    const pattern = new RegExp(
      `(?<![\\p{L}\\p{N}\\p{M}])(${escapeRegExp(firstHead)}[^\\s.,;:!?。！？]*`
      + `[^.!?。！？]{0,42}?${escapeRegExp(lastHead)}[^\\s.,;:!?。！？]*)`,
      'iu',
    );
    if (pattern.test(text)) {
      return { sentence: text.replace(pattern, '_____'), kind: 'phrase' };
    }
  }

  for (const candidate of content.length ? content : words) {
    if (candidate.length < 4) continue;
    const head = candidate.slice(0, Math.max(4, candidate.length - 2));
    const pattern = new RegExp(`(?<![\\p{L}\\p{N}\\p{M}])(${escapeRegExp(head)}[^\\s.,;:!?。！？]*)`, 'iu');
    if (pattern.test(text)) {
      return { sentence: text.replace(pattern, '_____'), kind: 'word' };
    }
  }
  return null;
}

function foldForCompare(text) {
  const script = activePair.target.script;
  if (script === 'cjk' || script === 'kana' || script === 'hangul') {
    return String(text ?? '').toLowerCase().normalize('NFKC');
  }
  return fold(text);
}

/* The dictionary form, when the word on the card is not already it. */
export function dictionaryForm(card) {
  const lemma = String((card && card.lemma) || '').trim();
  const term = String((card && card.term) || '').trim();
  if (!lemma || !term || foldForCompare(lemma) === foldForCompare(term)) return '';
  const label = String((card && card.pos) || '').toLowerCase() === 'verb' ? 'infinitive' : 'dictionary form';
  return `<div class="form-of">${escapeHtml(label)} <b>${escapeHtml(lemma)}</b></div>`;
}

/**
 * Safely highlight `term` inside `sentence`, returning HTML with `<b>...</b>`.
 * Uses escapeRegExp and Unicode-aware letter classes (`\p{L}\p{M}`) rather than ASCII `\w*`.
 */
export function highlightIn(sentence, term) {
  const raw = String(sentence ?? '');
  const head = String(term ?? '').trim().split(/\s+/)[0] || '';
  const stem = head.replace(TRIM_PUNCT_RE, '');
  if (!stem) return escapeHtml(raw);

  const escapedStem = escapeRegExp(stem);
  const pattern = isNonSpacedProfile(activePair.target, stem) || CJK_CHAR_RE.test(stem)
    ? new RegExp(`(${escapedStem})`, 'iu')
    : new RegExp(`(?<![\\p{L}\\p{N}\\p{M}])(${escapedStem}[\\p{L}\\p{M}]*)`, 'iu');

  const match = pattern.exec(raw);
  if (!match) return escapeHtml(raw);

  const start = match.index;
  const end = start + match[0].length;
  return `${escapeHtml(raw.slice(0, start))}<b>${escapeHtml(match[0])}</b>${escapeHtml(raw.slice(end))}`;
}
