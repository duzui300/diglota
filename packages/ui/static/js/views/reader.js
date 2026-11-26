/**
 * Reader view: prose rendering, word lookup, register tagger, TOC, scroll progress, and exercises.
 */

import { $, $$, escapeHtml, hostOf, mdInline, safeHttpUrl, toast } from '../lib/dom.js';
import { api, ensureGlossary } from '../lib/api.js';
import {
  REGISTER_LABELS,
  applyPrefs,
  gradingNote,
  refreshDuePill,
  registerRouteCleanup,
  setActiveTab,
  store,
} from '../lib/state.js';
import { stopReading } from '../lib/speech.js';
import {
  SENTENCE_END,
  isTargetLang,
  splitSentencePieces,
  splitTargetTokens,
  targetName,
} from '../lib/language.js';
import {
  closeModal,
  hideSelectionBar,
  lookupWord,
  openModal,
  wireSelectionBar,
} from '../components/overlays.js';
import { downloadShare } from '../modals/modals.js';

let readerState = null;
let registerOptions = null;
let activeScrollCleanup = null;

function splitIntoRuns(spans) {
  const runs = [];
  for (const span of spans) {
    const pieces = splitSentencePieces(span.text);
    pieces.forEach((piece, index) => {
      runs.push({ span, text: piece, endsSpan: index === pieces.length - 1 });
    });
  }
  return runs;
}

function groupSentences(runs) {
  const sentences = [];
  let current = [];
  for (const run of runs) {
    current.push(run);
    if (SENTENCE_END.test(run.text)) {
      sentences.push(current);
      current = [];
    }
  }
  if (current.length) sentences.push(current);
  return sentences.length ? sentences : [runs];
}

function targetWordsHtml(text) {
  return splitTargetTokens(text).map((seg) => {
    if (!seg.isWord) return escapeHtml(seg.text);
    return `<span class="w" data-word="${escapeHtml(seg.word)}">${escapeHtml(seg.text)}</span>`;
  }).join('');
}

function renderSentence(runs) {
  const html = runs.map((run) => {
    const span = run.span;
    if (!isTargetLang(span.lang)) return `<span class="en">${escapeHtml(run.text)}</span>`;
    const classes = `es${span.target ? ' target' : ''}${span.bold ? ' bold' : ''}`;
    const gloss = run.endsSpan && span.gloss
      ? `<span class="gloss">${escapeHtml(span.gloss)}</span>` : '';
    return `<span class="${classes}">${targetWordsHtml(run.text)}</span>${gloss}`;
  }).join('');
  return `<span class="sent">${html}</span>`;
}

function renderParagraph(block) {
  const sentences = groupSentences(splitIntoRuns(block.spans));
  return sentences.map(renderSentence).join('');
}

function renderBlock(block, index) {
  if (block.kind === 'h') {
    const tag = block.level <= 2 ? 'h2' : 'h3';
    return `<${tag} id="sec-${index}" data-block="${index}">${escapeHtml(block.spans.map((s) => s.text).join(''))}</${tag}>`;
  }
  if (block.kind === 'byline') return `<p class="byline-line" data-block="${index}">${escapeHtml(block.spans.map((s) => s.text).join(''))}</p>`;
  if (block.kind === 'date') return `<p class="byline-line" data-block="${index}">${escapeHtml(block.spans.map((s) => s.text).join(''))}</p>`;
  if (block.kind === 'ref') return `<p class="ref" data-block="${index}">${escapeHtml(block.spans.map((s) => s.text).join(''))}</p>`;
  return `<p data-block="${index}">${renderParagraph(block)}</p>`;
}

function articleSections(article) {
  const seen = new Map();
  const collected = [];
  article.blocks.forEach((block, index) => {
    if (block.kind !== 'h') return;
    const text = block.spans.map((s) => s.text).join('').trim();
    if (!text) return;
    seen.set(text, (seen.get(text) || 0) + 1);
    collected.push({ index, text, level: Math.min(block.level || 2, 4) });
  });
  return collected.filter((section) => seen.get(section.text) === 1);
}

function renderToc(article) {
  const sections = articleSections(article);
  if (sections.length < 2) return '';
  return `<section class="toc-section"><h4>Contents</h4>
    <nav class="toc" id="toc">
      ${sections.map((section) => `
        <a href="#/read/${encodeURIComponent(article.slug)}"
           data-goto="${escapeHtml(section.index)}" class="toc-link lvl-${section.level}"
           title="${escapeHtml(section.text)}">${escapeHtml(section.text)}</a>`).join('')}
    </nav></section>`;
}

function wireToc(article) {
  const links = $$('#toc .toc-link');
  if (!links.length) return;

  links.forEach((link) => link.addEventListener('click', (event) => {
    event.preventDefault();
    const target = document.getElementById(`sec-${link.dataset.goto}`);
    if (!target) return;
    stopReading();
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }));

  readerState.sections = articleSections(article);
  readerState.tocLinks = links;
  markCurrentSection();
}

function markCurrentSection() {
  const sections = readerState && readerState.sections;
  const links = readerState && readerState.tocLinks;
  if (!sections || !sections.length || !links) return;
  let current = -1;
  for (const section of sections) {
    const node = document.getElementById(`sec-${section.index}`);
    if (node && node.getBoundingClientRect().top <= 140) current = section.index;
  }
  links.forEach((link) => link.classList.toggle('current', Number(link.dataset.goto) === current));
}

function renderRail(article) {
  const vocab = article.vocab || [];
  const focus = article.focus || [];
  const grammar = article.grammar || [];
  const toc = renderToc(article);
  if (!vocab.length && !grammar.length && !focus.length && !toc) return '';
  return `<aside class="rail">
    ${toc}
    ${focus.length ? `<section><h4>Focus vocabulary</h4>${focus.map((f) => `
      <div class="vocab-item">
        <div class="vocab-body">
          <div class="vocab-line"><span class="t">${escapeHtml(f.es)}</span> <span class="g">${escapeHtml(f.en || '')}</span></div>
        </div>
        <button class="btn ghost xs add" title="Save to your deck" aria-label="Save ${escapeHtml(f.es)} to your deck" data-save-term="${escapeHtml(f.es)}" data-save-gloss="${escapeHtml(f.en || '')}">Save</button>
      </div>`).join('')}</section>` : ''}
    ${vocab.length ? `<section><h4>Recycled vocabulary</h4>${vocab.map((v) => `
      <div class="vocab-item">
        <div class="vocab-body">
          <div class="vocab-line"><span class="t">${escapeHtml(v.term)}</span> <span class="g">${escapeHtml(v.gloss || '')}</span></div>
          ${(v.examples || []).length ? `<div class="g vocab-example">${mdInline(v.examples[0])}</div>` : ''}
        </div>
      </div>`).join('')}</section>` : ''}
    ${grammar.length ? `<section><h4>Grammar</h4>${grammar.map((g) => `
      <div class="grammar-note"><strong>${mdInline(g.title)}</strong>
        ${g.example ? `<p style="font-family:var(--serif);color:var(--es-ink)">${mdInline(g.example)}</p>` : ''}
        ${g.explanation ? `<p>${mdInline(g.explanation)}</p>` : ''}
      </div>`).join('')}</section>` : ''}
  </aside>`;
}

function paintRegister(slug, article) {
  const host = $('#register-note');
  if (!host) return;
  const code = article.register || 'unspecified';
  const known = code !== 'unspecified';
  const editable = article.register_editable !== false;
  host.innerHTML = `
    <span class="register-name">${known
      ? escapeHtml(REGISTER_LABELS[code] || code) : 'No register'}</span>
    <span class="register-blurb">${escapeHtml(article.register_blurb
      || (known ? '' : 'nothing in the text said what kind of writing this is'))}</span>
    ${known && article.register_inferred
      ? '<span class="register-inferred" title="Guessed from how the text is written; not declared by the author">inferred</span>'
      : ''}
    ${editable
      ? `<button class="btn ghost sm" data-set-register="${escapeHtml(slug)}">${known ? 'Change' : 'Set it'}</button>`
      : '<span class="small muted" title="This file holds several passages, and the app cannot tell which one a tag in it would belong to">set in the file; this one holds several passages</span>'}`;
  const button = $('[data-set-register]', host);
  if (button) button.addEventListener('click', () => openRegisterPicker(slug, known ? code : ''));
}

async function openRegisterPicker(slug, current) {
  try {
    if (!registerOptions) registerOptions = (await api('/api/registers')).registers;
  } catch (err) {
    toast(err.message, 'err');
    return;
  }
  openModal('What kind of writing is this?', `
    <p class="small muted" style="margin-top:0">Some passages declare their own kind in the file they
    came from; the rest are guessed from how they are written, and the guess is marked <em>inferred</em>.
    Whatever you choose here is written into the passage's file, so it stays right if the file is moved
    or shared. A passage you wrote yourself keeps its every other word.</p>
    <div class="register-choices">
      ${registerOptions.map((option) => `
        <button type="button" class="choice ${option.code === current ? 'active' : ''}"
                data-register-pick="${escapeHtml(option.code)}">
          <span class="choice-name">${escapeHtml(REGISTER_LABELS[option.code] || option.name)}</span>
          <span class="small muted">${escapeHtml(option.blurb)}</span>
        </button>`).join('')}
      <button type="button" class="choice ${current ? '' : 'active'}" data-register-pick="">
        <span class="choice-name">Let the app guess</span>
        <span class="small muted">Takes the tag out of the file; the app infers one and says that it did</span>
      </button>
    </div>`);

  $$('[data-register-pick]').forEach((button) => button.addEventListener('click', async () => {
    try {
      const fresh = await api(`/api/article/${encodeURIComponent(slug)}/register`, {
        method: 'POST', body: { code: button.dataset.registerPick },
      });
      closeModal();
      toast(fresh.register
        ? `Marked as ${REGISTER_LABELS[fresh.register] || fresh.register}`
        : 'Tag removed. The app will guess');
      paintRegister(slug, {
        register: fresh.register || 'unspecified',
        register_blurb: fresh.blurb,
        register_inferred: fresh.inferred,
        register_editable: true,
      });
    } catch (err) {
      toast(err.message, 'err');
    }
  }));
}

function textOffsetWithin(root, node) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let offset = 0;
  let current;
  while ((current = walker.nextNode())) {
    if (node.contains(current)) return offset;
    offset += current.textContent.length;
  }
  return -1;
}

function sentenceAround(node) {
  const paragraph = node.closest('.sent') || node.closest('p, li, blockquote') || node.parentElement;
  if (!paragraph) return node.textContent;
  const marker = 'data-diglot-ctx-target';
  node.setAttribute(marker, '1');
  const clone = paragraph.cloneNode(true);
  node.removeAttribute(marker);
  clone.querySelectorAll('.gloss').forEach((gloss) => gloss.remove());
  const clonedNode = clone.hasAttribute(marker) ? clone : clone.querySelector(`[${marker}]`);
  const text = clone.textContent;
  const offset = clonedNode ? textOffsetWithin(clone, clonedNode) : -1;
  if (offset < 0) return text.replace(/\s+/g, ' ').trim();
  const nodeLen = clonedNode.textContent.length;
  const before = text.slice(0, offset);
  const after = text.slice(offset + nodeLen);
  const start = Math.max(
    before.lastIndexOf('. '),
    before.lastIndexOf('? '),
    before.lastIndexOf('! '),
    before.lastIndexOf('” '),
    before.lastIndexOf('。'),
    before.lastIndexOf('！'),
    before.lastIndexOf('？'),
  ) + 1;
  const endMatch = after.match(/[.!?。！？]/);
  const end = endMatch ? offset + nodeLen + endMatch.index + 1 : text.length;
  return text.slice(start, end).replace(/\s+/g, ' ').trim();
}

function computeScrollPosition() {
  const prose = $('#prose');
  if (!prose) return null;
  const rect = prose.getBoundingClientRect();
  const seen = Math.min(Math.max(-rect.top + window.innerHeight * 0.4, 0), prose.offsetHeight);
  return Math.min(seen / Math.max(prose.offsetHeight, 1), 1);
}

function updateProgressBar() {
  const prose = $('#prose');
  if (!prose) return;
  const rect = prose.getBoundingClientRect();
  const total = prose.offsetHeight - window.innerHeight * 0.5;
  const seen = Math.min(Math.max(-rect.top + window.innerHeight * 0.4, 0), Math.max(total, 1));
  const fill = $('#reader-progress-fill');
  if (fill) fill.style.width = `${Math.round((seen / Math.max(total, 1)) * 100)}%`;
  const pos = computeScrollPosition();
  if (readerState && pos !== null && readerState.scrollRestored) {
    readerState.lastPosition = pos;
  }
}

function restoreScroll(position) {
  if (!position || position < 0.02) {
    if (readerState) readerState.scrollRestored = true;
    return;
  }
  requestAnimationFrame(() => {
    const prose = $('#prose');
    if (!prose) return;
    const target = prose.offsetTop + position * prose.offsetHeight - window.innerHeight * 0.3;
    window.scrollTo({ top: Math.max(target, 0), behavior: 'instant' in window ? 'instant' : 'auto' });
    if (readerState) {
      readerState.scrollRestored = true;
      readerState.lastPosition = position;
    }
    updateProgressBar();
    toast('Resumed where you left off');
  });
}

export function currentReaderProgress(slug) {
  if (!readerState || !readerState.article || readerState.article.slug !== slug) return null;
  const livePos = readerState.scrollRestored ? computeScrollPosition() : null;
  const position = livePos !== null ? livePos : (readerState.lastPosition ?? 0);
  const seconds = Math.max(0, Math.round((Date.now() - readerState.startedAt) / 1000));
  return {
    position,
    seconds,
    completed: Boolean(readerState.article.progress?.completed_at) || position >= 0.98,
  };
}

export async function saveProgress(slug, completed) {
  const prose = $('#prose');
  if (!prose || !readerState) return;
  const position = computeScrollPosition() ?? (readerState.lastPosition ?? 0);
  const seconds = Math.round((Date.now() - readerState.startedAt) / 1000);
  if (seconds < 3 && !completed) return;
  readerState.startedAt = Date.now();
  readerState.lastPosition = position;
  const finished = completed || position >= 0.98;
  try {
    await api(`/api/article/${encodeURIComponent(slug)}/progress`, {
      method: 'POST',
      body: { position, seconds, completed: finished },
    });
  } catch { /* progress saving is best-effort */ }
}

function wireReader(article) {
  const slug = article.slug;

  $('#prose').addEventListener('click', (event) => {
    const wordEl = event.target.closest('.w');
    if (!wordEl) return;
    const word = wordEl.dataset.word;
    if (!word) return;
    stopReading();
    const sentence = sentenceAround(wordEl);
    lookupWord(word, sentence, slug, wordEl, readerState.saved);
  });

  $$('.rail [data-save-term]').forEach((button) => button.addEventListener('click', async (event) => {
    event.stopPropagation();
    try {
      const result = await api('/api/word/save', {
        method: 'POST',
        body: {
          term: button.dataset.saveTerm,
          gloss: button.dataset.saveGloss || null,
          article_slug: slug,
        },
      });
      if (readerState && readerState.saved && button.dataset.saveTerm) {
        readerState.saved.add(button.dataset.saveTerm.toLowerCase());
      }
      button.textContent = 'Saved';
      button.disabled = true;
      toast(`Saved “${button.dataset.saveTerm}”. Your deck now holds ${result.total} words.`);
      refreshDuePill();
    } catch (err) { toast(err.message, 'err'); }
  }));

  $$('[data-look]', $('#view')).forEach((chip) => chip.addEventListener('click', () => {
    lookupWord(chip.dataset.look, '', slug, chip, readerState.saved);
  }));

  wireSelectionBar(slug);
  wireToc(article);

  $('#btn-quiz').addEventListener('click', () => loadExercises(slug, 'quiz'));
  $('#btn-drills').addEventListener('click', () => loadExercises(slug, 'drills'));
  $('#btn-done').addEventListener('click', async () => {
    await api(`/api/article/${encodeURIComponent(slug)}/progress`, {
      method: 'POST',
      body: { completed: true, position: 1 },
    });
    if (readerState) readerState.lastPosition = 1;
    $('#btn-done').textContent = 'Finished';
    toast('Marked as finished');
  });
  $('#btn-share').addEventListener('click', () => downloadShare(slug));

  if (activeScrollCleanup) {
    activeScrollCleanup();
    activeScrollCleanup = null;
  }

  let lastSaved = 0;
  const onScroll = () => {
    updateProgressBar();
    markCurrentSection();
    const now = Date.now();
    if (now - lastSaved > 6000) {
      lastSaved = now;
      saveProgress(slug, false);
    }
  };
  window.addEventListener('scroll', onScroll, { passive: true });
  const unregister = registerRouteCleanup(() => {
    window.removeEventListener('scroll', onScroll);
    stopReading();
    hideSelectionBar();
    activeScrollCleanup = null;
  });
  activeScrollCleanup = () => {
    window.removeEventListener('scroll', onScroll);
    unregister();
  };
}

/* -- exercises ------------------------------------------------------------- */

async function loadExercises(slug, kind) {
  const host = $('#exercises');
  const langLabel = targetName();
  host.innerHTML = `<div class="empty"><span class="spinner"></span>
    <div class="small muted" style="margin-top:10px">${kind === 'quiz'
      ? `writing comprehension questions in ${escapeHtml(langLabel)}` : 'building translation drills'}. This takes a moment the first time.</div></div>`;

  let data;
  try {
    data = await api(`/api/article/${encodeURIComponent(slug)}/${kind}`);
  } catch (err) {
    host.innerHTML = `<div class="empty"><h3>Could not load exercises</h3><p>${escapeHtml(err.message)}</p></div>`;
    return;
  }

  if (kind === 'quiz') {
    if (!data.questions.length) { host.innerHTML = '<div class="empty">No questions were generated.</div>'; return; }
    host.innerHTML = gradingNote() + data.questions.map((q, i) => `
      <div class="exercise" data-kind="comprehension" data-index="${i}">
        <span class="focus-chip">Question ${i + 1} of ${data.questions.length}</span>
        <div class="q">${escapeHtml(q.question)}</div>
        ${q.hint ? `<div class="small muted" style="margin-bottom:9px">hint: ${escapeHtml(q.hint)}</div>` : ''}
        <textarea placeholder="Answer in ${escapeHtml(langLabel)}, or in English if you are stuck"></textarea>
        <div class="row" style="margin-top:9px">
          <button class="btn primary sm" data-check>Check answer</button>
          <button class="btn ghost sm" data-reveal>Show a model answer</button>
        </div>
        <div class="verdict-slot"></div>
      </div>`).join('');
    $$('[data-check]', host).forEach((button) => button.addEventListener('click', () => checkAnswer(button, 'comprehension', slug, data.questions)));
    $$('[data-reveal]', host).forEach((button) => button.addEventListener('click', () => {
      const box = button.closest('.exercise');
      box.querySelector('.verdict-slot').innerHTML =
        `<div class="verdict"><div class="headline">Model answer</div>
         <div style="font-family:var(--serif);color:var(--es-ink);font-size:15px">${escapeHtml(data.questions[+box.dataset.index].expected)}</div></div>`;
    }));
  } else {
    if (!data.drills.length) { host.innerHTML = '<div class="empty">No drills were generated.</div>'; return; }
    host.innerHTML = gradingNote() + data.drills.map((d, i) => `
      <div class="exercise" data-kind="translate" data-index="${i}">
        <span class="focus-chip">${escapeHtml(d.focus || 'translation')}${d.vocabulary && d.vocabulary.length ? `, ${d.vocabulary.map(escapeHtml).join(', ')}` : ''}</span>
        <div class="q">${escapeHtml(d.en)}</div>
        <textarea placeholder="Write it in ${escapeHtml(langLabel)}"></textarea>
        <div class="row" style="margin-top:9px">
          <button class="btn primary sm" data-check>Check my ${escapeHtml(langLabel)}</button>
          <button class="btn ghost sm" data-reveal>Show the article's version</button>
        </div>
        <div class="verdict-slot"></div>
      </div>`).join('');
    $$('[data-check]', host).forEach((button) => button.addEventListener('click', () => checkAnswer(button, 'translate', slug, data.drills)));
    $$('[data-reveal]', host).forEach((button) => button.addEventListener('click', () => {
      const box = button.closest('.exercise');
      box.querySelector('.verdict-slot').innerHTML =
        `<div class="verdict"><div class="headline">The article's ${escapeHtml(langLabel)}</div>
         <div style="font-family:var(--serif);color:var(--es-ink);font-size:15px">${escapeHtml(data.drills[+box.dataset.index].es)}</div></div>`;
    }));
  }
  host.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function checkAnswer(button, kind, slug, items) {
  const box = button.closest('.exercise');
  const item = items[+box.dataset.index];
  const answer = box.querySelector('textarea').value.trim();
  if (!answer) { toast('Write something first'); return; }

  const slot = box.querySelector('.verdict-slot');
  button.disabled = true;
  slot.innerHTML = '<div class="verdict"><div class="loading"><span class="spinner"></span> judging your answer</div></div>';

  try {
    const result = await api('/api/attempt', {
      method: 'POST',
      body: kind === 'translate'
        ? { kind, answer, source: item.en, reference: item.es, question: item.en, slug }
        : { kind, answer, question: item.question, reference: item.expected, slug },
    });
    slot.innerHTML = renderVerdict(result, kind, item);
    refreshDuePill();
  } catch (err) {
    slot.innerHTML = `<div class="verdict wrong"><div class="headline">Could not grade that</div>
      <div class="small">${escapeHtml(err.message)}</div></div>`;
  } finally {
    button.disabled = false;
  }
}

function renderVerdict(result, kind, item) {
  const verdict = result.verdict || {};
  const checks = verdict.checks || {};
  const feedback = result.feedback || {};
  const correct = result.correct;
  const provisional = result.provisional === true;

  const labels = {
    meaning: 'meaning kept', grammar: 'grammar', natural: 'naturalness',
    fits: 'fits the blank', same_word: 'exact word', correct: 'correct', understood: 'understood',
  };
  const meters = Object.entries(checks).map(([key, value]) => `
    <div class="meter">
      <span>${escapeHtml(labels[key] || key)}</span>
      <span class="track"><i style="width:${Math.round(value * 100)}%;
        background:${value >= 0.6 ? 'var(--good)' : value >= 0.35 ? 'var(--warn)' : 'var(--bad)'}"></i></span>
      <span class="val">${Math.round(value * 100)}%</span>
    </div>`).join('');

  const gradedBy = verdict.model
    ? `${verdict.method === 'local' ? 'checked' : 'judged'} by ${escapeHtml(verdict.model)}`
      + (verdict.latency_ms ? ` in ${verdict.latency_ms} ms` : '')
    : '';
  const scoreLine = verdict.score != null
    ? `<div class="small muted" style="margin-top:6px">overall ${verdict.score.toFixed(1)} / ${verdict.score_max}
       ${gradedBy ? `, ${gradedBy}` : ''}</div>`
    : (gradedBy ? `<div class="small muted" style="margin-top:6px">${gradedBy}</div>` : '');

  const corrections = (feedback.corrections || []).length ? `
    <ul class="corrections">${feedback.corrections.map((c) => `
      <li><span class="was">${escapeHtml(c.their_text || '')}</span> →
          <span class="now">${escapeHtml(c.should_be || '')}</span>
          <span class="why">${escapeHtml(c.why || '')}</span></li>`).join('')}</ul>` : '';

  const reference = kind === 'translate' ? item.es : item.expected;
  const uncheckable = provisional && result.may_fail === false && !correct;
  const comparison = provisional ? (verdict.labels || {}).note || '' : '';
  const headline = uncheckable
    ? 'This answer is too different from the article’s wording to check'
    : (correct ? '✓ ' : '✗ ') + escapeHtml(
      comparison && correct ? comparison
        : feedback.summary || (correct ? 'That works.' : 'Not quite.'),
    );

  return `<div class="verdict ${uncheckable ? 'provisional' : correct ? 'correct' : 'wrong'}">
    <div class="headline">${headline}</div>
    ${provisional ? `<div class="small muted">No judgment model is configured, so this was
      compared word by word against the article. An unusual but correct answer cannot be
      recognised this way. Judge it yourself.</div>` : ''}
    ${verdict.error ? `<div class="small muted">${escapeHtml(verdict.error)}</div>` : ''}
    <div class="meters">${meters}</div>
    ${scoreLine}
    ${corrections}
    ${feedback.praise ? `<div class="small" style="margin-top:8px">${escapeHtml(feedback.praise)}</div>` : ''}
    ${feedback.better ? `<div style="margin-top:10px"><div class="small muted">A natural rendering</div>
      <div style="font-family:var(--serif);color:var(--es-ink);font-size:15px">${escapeHtml(feedback.better)}</div></div>` : ''}
    <div style="margin-top:10px"><div class="small muted">${kind === 'translate' ? "The article's version" : 'Model answer'}</div>
      <div style="font-family:var(--serif);color:var(--es-ink);font-size:15px">${escapeHtml(reference || '')}</div></div>
  </div>`;
}

export async function viewReader(slug) {
  setActiveTab('library');
  const view = $('#view');
  view.className = 'view';
  view.innerHTML = '<div class="empty"><span class="spinner"></span></div>';

  let article;
  try {
    article = await api(`/api/article/${encodeURIComponent(slug)}`);
  } catch (err) {
    view.innerHTML = `<div class="empty"><h3>Could not open that article</h3><p>${escapeHtml(err.message)}</p></div>`;
    return;
  }

  readerState = {
    article,
    startedAt: Date.now(),
    saved: new Set((article.known || []).map((w) => String(w).toLowerCase())),
    lastPosition: Number((article.progress && article.progress.position) || 0),
    scrollRestored: false,
  };
  ensureGlossary();
  $('#reader-bar').hidden = false;
  document.body.classList.remove('dim-english', 'es-only');
  applyPrefs();

  const blocks = article.blocks.map(renderBlock).join('');
  const rail = renderRail(article);
  const coverage = article.coverage || {};
  const text = coverage.text || {};
  const knownPct = Math.round((text.token_ratio || 0) * 100);
  const langLabel = targetName();
  const safeArticleUrl = safeHttpUrl(article.url);

  view.innerHTML = `
    <div class="reader-wrap ${rail ? 'has-rail' : ''}">
      <div class="article">
        <div class="article-head">
          <h1>${escapeHtml(article.title)}</h1>
          ${article.author ? `<div class="byline">${escapeHtml(article.author)}</div>` : ''}
          <div class="source">
            ${safeArticleUrl ? `<a href="${escapeHtml(safeArticleUrl)}" target="_blank" rel="noopener">${escapeHtml(hostOf(safeArticleUrl))}</a>` : ''}
            <span>${article.stats.words} words</span>
            <span>${Math.round(article.stats.spanish_ratio * 100)}% ${escapeHtml(langLabel)}</span>
          </div>
          <div class="register-note" id="register-note"></div>
          ${text.types && knownPct > 0 ? `<div class="coverage-line band-${escapeHtml(coverage.band || '')}">
            <strong>${knownPct}%</strong> of the ${escapeHtml(langLabel)} words here are already in your deck.
            ${escapeHtml(coverage.advice || '')}
            ${(text.unknown || []).length ? `<div class="unknown-words"><span class="muted">New to you</span>
              ${text.unknown.slice(0, 10).map((word) => `<span class="chip" data-look="${escapeHtml(word)}">${escapeHtml(word)}</span>`).join('')}
            </div>` : ''}
          </div>` : ''}
        </div>
        <div class="prose" id="prose">${blocks}</div>

        <div class="article-foot">
          <div class="row">
            <button class="btn primary" id="btn-quiz">Comprehension questions</button>
            <button class="btn" id="btn-drills">Translate into ${escapeHtml(langLabel)}</button>
            <span class="spacer"></span>
            <button class="btn" id="btn-done">${article.progress.completed_at ? 'Finished' : 'Mark as finished'}</button>
          </div>
          <div class="share-row">
            <input id="share-name" class="share-name" placeholder="your name (optional)"
                   aria-label="Your name, recorded in the file you share"
                   value="${escapeHtml(store.get('shareName', ''))}">
            <button class="btn ghost sm" id="btn-share"
                    title="Download this lesson as a file to send to someone">Share this lesson</button>
          </div>
          <div id="exercises" style="margin-top:20px"></div>
        </div>
      </div>
      ${rail}
    </div>`;

  wireReader(article);
  paintRegister(article.slug, article);
  restoreScroll(article.progress.position);
  updateProgressBar();
}
