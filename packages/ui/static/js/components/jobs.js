/**
 * Background jobs: SSE EventSource stream, polling fallback, Activity drawer, and job cards.
 */

import { $, $$, escapeHtml, toast } from '../lib/dom.js';
import { api } from '../lib/api.js';
import { targetName } from '../lib/language.js';
import { closeModal } from './overlays.js';

export const jobState = new Map();
const notifiedJobs = new Set();
const jobListeners = new Set();
let eventSource = null;
let pollTimer = null;
let reconnectTimer = null;
let jobsInitialized = false;

const JOB_LABEL = {
  import: 'Importing',
  recommend: 'Finding something to read',
  writing: 'Weaving your writing',
};
const JOB_STATUS = {
  running: 'Working',
  queued: 'Waiting',
  done: 'Done',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

export function onJobUpdate(fn) {
  if (typeof fn !== 'function') return () => {};
  jobListeners.add(fn);
  return () => jobListeners.delete(fn);
}

function activeJobCount() {
  return [...jobState.values()].filter((j) => j.status === 'running' || j.status === 'queued').length;
}

/* Jobs made before the titles were cleaned still carry the old separators in
   the database. Clean at the point of display so history reads like new work. */
function cleanJobTitle(title) {
  return String(title || '').replace(/\s+·\s+/g, ', ').replace(/\s+—\s+/g, '. ');
}

function elapsedText(seconds) {
  const total = Math.round(seconds || 0);
  if (total < 60) return `${total}s`;
  const minutes = Math.round(total / 60);
  if (minutes < 60) return `${minutes} min`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

export function refreshJobPill() {
  const pill = $('#job-pill');
  if (!pill) return;
  const count = activeJobCount();
  pill.hidden = !count;
  pill.textContent = count;
}

export function updateJob(job) {
  jobState.set(job.id, job);
  const settled = job.status === 'done' || job.status === 'failed' || job.status === 'cancelled';
  let justSettled = false;

  if (settled && !notifiedJobs.has(job.id)) {
    notifiedJobs.add(job.id);
    justSettled = true;
    if (job.status === 'done') {
      const detail = (job.kind === 'import' || job.kind === 'writing') && job.result
        ? `${job.result.words} words, ${Math.round((job.result.spanish_ratio || 0) * 100)}% ${targetName()}`
        : 'finished';
      toast(`Done. ${cleanJobTitle(job.title).slice(0, 60)}. ${detail}`, 'ok');
    } else if (job.status === 'failed') {
      toast(`Failed. ${cleanJobTitle(job.title).slice(0, 50)}. ${(job.error || 'failed').slice(0, 140)}`, 'err');
    } else {
      toast(`Cancelled: ${cleanJobTitle(job.title).slice(0, 50)}`);
    }
  }

  renderActivity();
  renderJobDetail(job.id);
  refreshJobPill();
  for (const listener of [...jobListeners]) {
    try { listener(job, { justSettled }); } catch { /* listener errors must not break updates */ }
  }
}

function formatTokens(n) {
  const value = Number(n || 0);
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(value);
}

export function jobCard(job) {
  const pct = Math.round((job.fraction || 0) * 100);
  const running = job.status === 'running' || job.status === 'queued';
  const slug = job.result && job.result.slug;
  const langLabel = targetName();

  let body = '';
  if (job.status === 'running') {
    body = `<div class="job-step">${escapeHtml(job.step || 'working')}${job.detail ? `. ${escapeHtml(job.detail)}` : ''}</div>
      <div class="bar thin"><i style="width:${pct}%"></i></div>`;
  } else if (job.status === 'queued') {
    body = '<div class="job-step muted">waiting for a free worker</div>';
  } else if (job.status === 'done' && job.result) {
    const r = job.result;
    const partial = r.anchors && !r.anchors.vocab && !r.anchors.grammar
      ? '<div class="job-step" style="color:var(--warn)">the post-reading notes could not be generated</div>'
      : '';
    const uneven = (r.spread || 0) > 0.45
      ? `<div class="job-step" style="color:var(--warn)">The ${escapeHtml(langLabel)} is unevenly spread; paragraphs differ by ${Math.round(r.spread * 100)} points.</div>`
      : '';
    const lost = (r.retained || 1) < 0.9
      ? `<div class="job-step" style="color:var(--warn)">
           only ${Math.round((r.retained || 0) * 100)}% of the source text survived the weave</div>`
      : '';
    const usage = r.usage || null;
    const usageBits = [];
    if (usage && usage.total_tokens > 0) {
      let tok = `${formatTokens(usage.total_tokens)} tokens`;
      if (usage.cached_tokens > 0 && usage.prompt_tokens > 0) {
        tok += ` (${Math.round((usage.cached_tokens / usage.prompt_tokens) * 100)}% cached)`;
      }
      usageBits.push(tok);
    }
    if (r.reused > 0) usageBits.push(`${r.reused} cached`);
    const usageLine = usageBits.length
      ? `<div class="job-step small muted">${escapeHtml(usageBits.join(' · '))}</div>`
      : '';
    body = r.words
      ? `<div class="job-step job-facts"><span>${r.words} words</span><span>${Math.round((r.spanish_ratio || 0) * 100)}% ${escapeHtml(langLabel)}</span>
           ${r.level ? `<span>${escapeHtml(r.level)}</span>` : ''}
           <span>${(r.focus || []).length} focus phrases</span></div>${usageLine}${partial}${uneven}${lost}`
      : `<div class="job-step">${(r.candidates || []).length} candidates found</div>`;
  } else if (job.status === 'failed') {
    body = `<div class="job-step" style="color:var(--bad)">${escapeHtml((job.error || 'failed').slice(0, 200))}</div>`;
  } else {
    body = '<div class="job-step muted">cancelled</div>';
  }

  return `<div class="job ${escapeHtml(job.status)}" data-job-id="${escapeHtml(job.id)}" data-job-status="${escapeHtml(job.status)}">
    <div class="job-head">
      <span class="job-title" title="${escapeHtml(cleanJobTitle(job.title))}">${escapeHtml(cleanJobTitle(job.title).slice(0, 90))}</span>
      <span class="spacer"></span>
      <span class="job-kind ${escapeHtml(job.status)}">${escapeHtml(JOB_STATUS[job.status] || JOB_LABEL[job.kind] || job.kind)}</span>
      ${running
        ? `<button class="icon-btn" data-job-cancel="${escapeHtml(job.id)}" title="Stop this job" aria-label="Stop this job">×</button>`
        : `<button class="icon-btn" data-job-dismiss="${escapeHtml(job.id)}" title="Dismiss" aria-label="Dismiss this job">×</button>`}
    </div>
    ${body}
    ${slug ? `<div class="row" style="margin-top:8px">
      <button class="btn sm" data-job-open="${escapeHtml(slug)}">Read it</button>
      <span class="small muted">${elapsedText(job.elapsed)}</span></div>`
      : `<div class="small muted" style="margin-top:4px">${elapsedText(job.elapsed)}</div>`}
  </div>`;
}

export function renderActivity() {
  const list = $('#activity-list');
  if (!list) return;
  const jobs = [...jobState.values()].sort((a, b) => (b.created_at || 0) - (a.created_at || 0));

  if (!jobs.length) {
    list.innerHTML = `<p class="muted small" style="padding:20px 4px">
      Nothing running. Imported articles and searches show up here while they work,
      and stay afterwards so you can see how they went.</p>`;
    return;
  }
  list.innerHTML = jobs.map(jobCard).join('');
  $$('[data-job-cancel]', list).forEach((button) => button.addEventListener('click', async () => {
    button.disabled = true;
    try { await api(`/api/jobs/${encodeURIComponent(button.dataset.jobCancel)}/cancel`, { method: 'POST' }); }
    catch (err) { toast(err.message, 'err'); }
  }));
  $$('[data-job-dismiss]', list).forEach((button) => button.addEventListener('click', async () => {
    const id = button.dataset.jobDismiss;
    try {
      await api(`/api/jobs/${encodeURIComponent(id)}`, { method: 'DELETE' });
      jobState.delete(id);
      renderActivity();
      refreshJobPill();
    } catch (err) { toast(err.message, 'err'); }
  }));
  $$('[data-job-open]', list).forEach((button) => button.addEventListener('click', () => {
    const slug = button.dataset.jobOpen;
    closeActivity();
    location.hash = `#/read/${encodeURIComponent(slug)}`;
  }));
}

/* The import dialog embeds a live view of its own job, so it and the Activity
   panel are never showing different things. */
export function renderJobDetail(jobId) {
  const host = $$(`[data-job-detail]`).find((el) => el.dataset.jobDetail === String(jobId));
  if (!host) return;
  const job = jobState.get(jobId);
  if (!job) return;
  host.innerHTML = jobCard(job);
  const open = $('[data-job-open]', host);
  if (open) open.addEventListener('click', () => {
    closeModal();
    location.hash = `#/read/${encodeURIComponent(open.dataset.jobOpen)}`;
  });
  const cancel = $('[data-job-cancel]', host);
  if (cancel) cancel.addEventListener('click', async () => {
    cancel.disabled = true;
    try { await api(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' }); }
    catch { /* shown in the panel */ }
  });
}

export function openActivity() {
  const panel = $('#activity');
  if (panel) panel.hidden = false;
  renderActivity();
}

export function closeActivity() {
  const panel = $('#activity');
  if (panel) panel.hidden = true;
}

export function initJobs() {
  if (jobsInitialized || typeof document === 'undefined') return;
  jobsInitialized = true;

  $('#btn-activity')?.addEventListener('click', () => {
    if ($('#activity').hidden) openActivity(); else closeActivity();
  });
  $('#activity-close')?.addEventListener('click', closeActivity);

  /* Clicking anywhere outside the Activity drawer dismisses it.
   * Exclude `#btn-activity` so clicking the toggle button closes cleanly
   * instead of closing on mousedown and immediately re-opening on click. */
  document.addEventListener('mousedown', (event) => {
    const panel = $('#activity');
    if (!panel || panel.hidden) return;
    const target = event.target;
    if (target && target.closest && (target.closest('#activity') || target.closest('#btn-activity'))) return;
    closeActivity();
  }, true);
  $('#activity-clear')?.addEventListener('click', async () => {
    try {
      await api('/api/jobs', { method: 'DELETE' });
      for (const [id, job] of jobState) {
        if (job.status !== 'running' && job.status !== 'queued') jobState.delete(id);
      }
      renderActivity();
      refreshJobPill();
    } catch (err) { toast(err.message, 'err'); }
  });
}

export function connectJobStream() {
  if (typeof window === 'undefined' || !window.EventSource) {
    pollJobs();
    return;
  }
  eventSource = new EventSource('/api/jobs/stream');
  eventSource.onmessage = (event) => {
    let message;
    try { message = JSON.parse(event.data); } catch { return; }
    if (message.type === 'snapshot') {
      // Arriving here after a reconnect: adopt the server's view wholesale.
      (message.jobs || []).forEach((job) => {
        if (job.status !== 'running' && job.status !== 'queued') notifiedJobs.add(job.id);
        jobState.set(job.id, job);
      });
      renderActivity();
      refreshJobPill();
    } else if (message.type === 'job') {
      updateJob(message.job);
    }
  };
  eventSource.onerror = () => {
    if (eventSource) { eventSource.close(); eventSource = null; }
    pollJobs();
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(() => {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      connectJobStream();
    }, 30000);
  };
}

export function pollJobs() {
  if (pollTimer) return;
  const tick = async () => {
    try {
      const data = await api('/api/jobs');
      const jobs = data.jobs || [];
      jobs.forEach((job) => {
        const known = jobState.get(job.id);
        if (job.status !== 'running' && job.status !== 'queued' && !known) notifiedJobs.add(job.id);
        updateJob(job);
      });
      if (!jobs.length) {
        renderActivity();
        refreshJobPill();
      }
    } catch { /* offline; try again next tick */ }
  };
  tick();
  pollTimer = setInterval(tick, 4000);
}

export async function loadJobs() {
  try {
    const data = await api('/api/jobs');
    (data.jobs || []).forEach((job) => {
      if (job.status !== 'running' && job.status !== 'queued') notifiedJobs.add(job.id);
      jobState.set(job.id, job);
    });
  } catch { /* the panel will fill in when the stream connects */ }
  renderActivity();
  refreshJobPill();
}
