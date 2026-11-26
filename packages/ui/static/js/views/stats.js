/**
 * Progress (#/stats) view: weekly challenge, reading analytics, force-directed corpus map, and reset.
 */

import { $, $$, escapeHtml, toast } from '../lib/dom.js';
import { api } from '../lib/api.js';
import {
  STAGE_ORDER,
  refreshDuePill,
  registerRouteCleanup,
  setActiveTab,
  store,
} from '../lib/state.js';
import { targetName } from '../lib/language.js';
import { closeModal, openModal } from '../components/overlays.js';

let analyticsDays = store.get('analyticsDays', 7);
let activeMapCleanup = null;

/* -- weekly goal ----------------------------------------------------------- */

async function renderChallenge() {
  const host = $('#challenge-panel');
  if (!host) return;
  let data;
  try {
    data = await api('/api/challenge');
  } catch {
    if (host.isConnected) host.innerHTML = '';
    return;
  }
  if (!host.isConnected) return;
  const challenge = data.challenge;
  if (challenge && !challenge.expired && !challenge.completed_at) {
    host.innerHTML = activeChallenge(challenge);
    $('#challenge-give-up').addEventListener('click', () => stopChallenge(challenge.id));
    return;
  }
  const justDone = challenge && challenge.completed_at && challenge.just_completed;
  host.innerHTML = `${justDone ? `<div class="cheer">${escapeHtml(challenge.title)} is done.
      ${challenge.done} ${escapeHtml(challenge.unit)}. That is the week.</div>` : ''}
    ${challenge && challenge.expired ? `<div class="cheer expired">That week ran out at
      ${challenge.done} of ${challenge.target} ${escapeHtml(challenge.unit)}. No matter.
      Here is another.</div>` : ''}
    ${suggestCard(data.suggested, data.kinds)}`;
  wireSuggest(data.kinds);
}

function activeChallenge(challenge) {
  const percent = Math.round(challenge.ratio * 100);
  const left = challenge.days_left;
  return `<div class="panel challenge">
    <div class="challenge-head">
      <h3>${escapeHtml(challenge.title)}</h3>
      <span class="challenge-when">${left > 0
        ? `${left} day${left === 1 ? '' : 's'} left` : 'last day'}</span>
    </div>
    <div class="challenge-bar"><i style="width:${percent}%"></i></div>
    <div class="challenge-count"><strong>${challenge.done}</strong> of ${challenge.target}
      ${escapeHtml(challenge.unit)}</div>
    <p class="small muted">${escapeHtml(challenge.why)}</p>
    <button class="btn ghost sm" id="challenge-give-up">Give up on this</button>
  </div>`;
}

function suggestCard(suggested, kinds) {
  if (!suggested) return '';
  return `<div class="panel challenge">
    <div class="challenge-head"><h3>This week</h3>
      <span class="challenge-when">nothing running</span></div>
    <p class="challenge-what">${escapeHtml(suggested.title)}</p>
    <p class="small muted">${escapeHtml(suggested.reason)}</p>
    <p class="small muted">${escapeHtml(suggested.why)}</p>
    <div class="row" style="margin-top:12px">
      <button class="btn primary" id="challenge-start">Start this</button>
      <select id="challenge-kind" aria-label="Choose a different goal">
        ${(kinds || []).map((kind) => `<option value="${escapeHtml(kind.id)}"
          ${kind.id === suggested.kind ? 'selected' : ''}>${escapeHtml(kind.label)}</option>`).join('')}
      </select>
      <select id="challenge-target" aria-label="Choose a target"></select>
    </div>
  </div>`;
}

function wireSuggest(kinds) {
  const start = $('#challenge-start');
  if (!start) return;
  const kindSelect = $('#challenge-kind');
  const targetSelect = $('#challenge-target');
  const fill = () => {
    const kind = (kinds || []).find((entry) => entry.id === kindSelect.value);
    targetSelect.innerHTML = (kind ? kind.targets : []).map((target, index) => `
      <option value="${escapeHtml(target)}" ${index === 0 ? 'selected' : ''}>${escapeHtml(target)} ${escapeHtml(kind.unit)}</option>`).join('');
  };
  kindSelect.addEventListener('change', fill);
  fill();
  start.addEventListener('click', async () => {
    start.disabled = true;
    try {
      await api('/api/challenge', {
        method: 'POST',
        body: { kind: kindSelect.value, target: Number(targetSelect.value) },
      });
      toast('Started. A week from today.');
      await renderChallenge();
    } catch (err) { start.disabled = false; toast(err.message, 'err'); }
  });
}

async function stopChallenge(id) {
  try {
    await api(`/api/challenge/${encodeURIComponent(id)}`, { method: 'DELETE' });
    toast('Stopped. It stays in the history.');
    await renderChallenge();
  } catch (err) { toast(err.message, 'err'); }
}

/* -- reading analytics ----------------------------------------------------- */

function dailyExposureChart(series) {
  const recent = series.slice(-30);
  const peak = Math.max(1, ...recent.map((d) => d.tokens));
  return `<div class="chart exposure" id="exposure-chart">
    ${recent.map((day) => {
      const total = day.tokens;
      const known = day.known;
      const height = total ? Math.max(4, Math.round(total / peak * 100)) : 2;
      const share = total ? Math.round(known / total * 100) : 0;
      return `<div class="col ${total ? '' : 'zero'}"
        title="${escapeHtml(day.day)}: ${total} words, ${share}% already known">
        <i style="height:${height}%;position:relative;display:flex;align-items:flex-end">
          ${total ? `<span style="display:block;width:100%;height:${share}%;background:var(--accent-ink);
            border-radius:4px 4px 0 0"></span>` : ''}
        </i></div>`;
    }).join('')}
  </div>
  <div class="chart-axis"><span>${escapeHtml(recent[0] ? recent[0].day.slice(5) : '')}</span><span>today</span></div>`;
}

async function renderReadingAnalytics(host) {
  let data;
  try {
    data = await api(`/api/analytics/reading?days=${encodeURIComponent(analyticsDays)}`);
  } catch (err) {
    if (host.isConnected) {
      host.innerHTML = `<div class="panel"><h3>Reading</h3>
        <p class="panel-sub">${escapeHtml(err.message)}</p></div>`;
    }
    return;
  }
  if (!host.isConnected) return;

  const exposure = data.exposure;
  const split = data.split;
  const scaffold = data.scaffolding;
  const share = (ratio) => `${Math.round((ratio || 0) * 100)}%`;
  const langLabel = targetName();

  const tiles = [
    [`${langLabel} words read`, exposure.tokens.toLocaleString(), data.window_label],
    ['Already known', share(exposure.understood_ratio), `${exposure.known.toLocaleString()} of ${exposure.tokens.toLocaleString()}`],
    ['Different words', exposure.distinct_words.toLocaleString(), `${exposure.articles} article${exposure.articles === 1 ? '' : 's'}`],
    ['Lookups / 1k words', scaffold.lookups_per_1000.toFixed(0), `${scaffold.lookups} lookups`],
  ];

  host.innerHTML = `
    <div class="panel">
      <h3>Reading</h3>
      <p class="panel-sub">${escapeHtml(data.headline)}</p>
      <div class="row" style="margin-bottom:16px">
        <div class="segmented" role="group" aria-label="Time window">
          ${[7, 30].map((d) => `<button data-window="${d}" class="${d === analyticsDays ? 'active' : ''}">
            ${d} days</button>`).join('')}
        </div>
      </div>

      ${data.has_data ? `
        <div class="tiles tight-tiles">
          ${tiles.map(([k, v, sub]) => `<div class="tile">
            <div class="k">${escapeHtml(k)}</div><div class="v">${escapeHtml(v)}</div><div class="sub">${escapeHtml(sub)}</div>
          </div>`).join('')}
        </div>

        <h4 class="sub-head">How much of it was new</h4>
        <p class="panel-sub">A word met for the first time, versus one you have met before,
          versus one you keep meeting, which is what the reading is actually drilling.</p>
        <div class="stack">
          ${[['new', 'var(--stage-1)'], ['familiar', 'var(--stage-2)'], ['repeated', 'var(--stage-4)']]
            .map(([key, colour]) => split[key]
              ? `<span style="background:${colour};width:${(split[key] / Math.max(split.total, 1) * 100).toFixed(1)}%"
                       title="${escapeHtml(key)}: ${split[key]}"></span>` : '').join('')}
        </div>
        <div class="legend">
          ${[['new', 'New', 'var(--stage-1)'], ['familiar', 'Met before', 'var(--stage-2)'],
             ['repeated', 'Keep meeting', 'var(--stage-4)']]
            .map(([key, label, colour]) => `<span class="item">
              <span class="swatch" style="background:${colour}"></span>${escapeHtml(label)}
              <span class="n">${split[key].toLocaleString()}, ${share(split.shares[key])}</span></span>`).join('')}
        </div>

        <h4 class="sub-head">Words read per day</h4>
        <p class="panel-sub">The filled part is words already in your vocabulary; the pale part
          is words you had not met before.</p>
        ${dailyExposureChart(data.series)}

        <h4 class="sub-head">Depending on the English</h4>
        <p class="panel-sub">Every time you reached for help, per thousand ${escapeHtml(langLabel)} words read.
          A rate rather than a score. 40 lookups is a lot in 800 words and nothing in 12,000.</p>
        <div class="tiles tight-tiles">
          ${[
            ['Lookups', scaffold.lookups, scaffold.lookups_per_1000],
            ['Full entries', scaffold.deep_entries, null],
            ['Explanations', scaffold.counts.explain || 0, scaffold.explains_per_1000],
            ['Translations', scaffold.counts.translate || 0, scaffold.translations_per_1000],
            ['Glosses turned back on', scaffold.gloss_reveals, null],
          ].map(([k, n, rate]) => `<div class="tile">
            <div class="k">${escapeHtml(k)}</div><div class="v">${n}</div>
            <div class="sub">${rate == null ? 'total' : `${rate.toFixed(0)} per 1,000 words`}</div>
          </div>`).join('')}
        </div>

        ${data.top_words.length ? `
          <h4 class="sub-head">Words you keep meeting</h4>
          <p class="panel-sub">Not yet in your deck. These are the ones the reading is
            repeatedly putting in front of you.</p>
          <div class="row">
            ${data.top_words.map((word) => `<button class="btn sm" data-encounter="${escapeHtml(word.lemma)}"
                data-gloss="${escapeHtml(word.gloss || '')}" title="read ${word.times} times">
              ${escapeHtml(word.lemma)} <span class="muted">${word.times}×</span></button>`).join('')}
          </div>` : ''}

        ${data.by_article.length ? `
          <h4 class="sub-head">Where the reading happened</h4>
          <table class="data">
            <thead><tr><th>Article</th><th>Words</th><th>Known</th></tr></thead>
            <tbody>${data.by_article.map((row) => `<tr>
              <td><a href="#/read/${encodeURIComponent(row.slug)}" style="color:var(--es-ink)">${escapeHtml(row.title.slice(0, 58))}</a></td>
              <td>${row.tokens.toLocaleString()}</td>
              <td>${share(row.understood_ratio)}</td>
            </tr>`).join('')}</tbody>
          </table>` : ''}
      ` : `
        <div class="empty" style="padding:28px">
          <h3>Nothing counted yet</h3>
          <p>Open an article and read a little. Words are credited as you scroll.
            The reader already reports its position, so nothing extra is asked of it.</p>
          <p><a class="btn primary sm" href="#/library">Read something</a></p>
        </div>`}
    </div>`;

  $$('[data-window]', host).forEach((button) => button.addEventListener('click', () => {
    analyticsDays = Number(button.dataset.window);
    store.set('analyticsDays', analyticsDays);
    renderReadingAnalytics(host);
  }));
  $$('[data-encounter]', host).forEach((button) => button.addEventListener('click', async () => {
    try {
      const result = await api('/api/word/save', {
        method: 'POST',
        body: {
          term: button.dataset.encounter,
          lemma: button.dataset.encounter,
          gloss: button.dataset.gloss || null,
        },
      });
      button.disabled = true;
      button.innerHTML = `${escapeHtml(button.dataset.encounter)} saved`;
      toast(`Saved. Your deck now holds ${result.total} words.`);
      refreshDuePill();
    } catch (err) { toast(err.message, 'err'); }
  }));
}

/* -- corpus map ------------------------------------------------------------ */

function layoutGraph(nodes, edges, width, height, iterations = 400) {
  const bySlug = new Map(nodes.map((n) => [n.slug, n]));
  const radius = Math.min(width, height) * 0.36;
  nodes.forEach((node, index) => {
    const angle = (index / nodes.length) * Math.PI * 2;
    node.x = width / 2 + Math.cos(angle) * radius;
    node.y = height / 2 + Math.sin(angle) * radius;
  });

  const area = width * height;
  const k = Math.sqrt(area / Math.max(nodes.length, 1));
  for (let step = 0; step < iterations; step += 1) {
    const cooling = 1 - step / iterations;
    nodes.forEach((node) => { node.dx = 0; node.dy = 0; });

    for (let i = 0; i < nodes.length; i += 1) {
      for (let j = i + 1; j < nodes.length; j += 1) {
        const a = nodes[i];
        const b = nodes[j];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        const distance = Math.hypot(dx, dy) || 0.01;
        const force = (k * k) / distance;
        dx /= distance;
        dy /= distance;
        a.dx += dx * force;
        a.dy += dy * force;
        b.dx -= dx * force;
        b.dy -= dy * force;
      }
    }
    for (const edge of edges) {
      const a = bySlug.get(edge.source);
      const b = bySlug.get(edge.target);
      if (!a || !b) continue;
      let dx = a.x - b.x;
      let dy = a.y - b.y;
      const distance = Math.hypot(dx, dy) || 0.01;
      const force = (distance * distance) / k * (0.4 + edge.weight);
      dx /= distance;
      dy /= distance;
      a.dx -= dx * force;
      a.dy -= dy * force;
      b.dx += dx * force;
      b.dy += dy * force;
    }
    for (const node of nodes) {
      const length = Math.hypot(node.dx, node.dy) || 0.01;
      const limit = Math.min(length, k * cooling);
      node.x += (node.dx / length) * limit;
      node.y += (node.dy / length) * limit;
      node.x = Math.max(30, Math.min(width - 30, node.x));
      node.y = Math.max(24, Math.min(height - 24, node.y));
    }
  }
  return nodes;
}

const shortTitle = (title) => (title.length > 26 ? `${title.slice(0, 24)}…` : title);

function placeLabels(nodes, radius, width, height) {
  const LINE = 12;
  const CHAR = 5.7;
  const boxes = [];
  let placed = 0;
  for (const node of [...nodes].sort((a, b) => b.words - a.words)) {
    const text = shortTitle(node.title);
    const boxWidth = text.length * CHAR;
    const y = node.y + radius(node.words) + LINE - 1;
    const x = Math.max(boxWidth / 2 + 4, Math.min(width - boxWidth / 2 - 4, node.x));
    const box = { x1: x - boxWidth / 2 - 2, x2: x + boxWidth / 2 + 2, y1: y - LINE + 3, y2: y + 3 };
    if (y > height - 4) continue;
    const clash = boxes.some((other) =>
      box.x1 < other.x2 && box.x2 > other.x1 && box.y1 < other.y2 && box.y2 > other.y1);
    if (clash) {
      const top = node.y - radius(node.words) - 5;
      const above = { x1: box.x1, x2: box.x2, y1: top - LINE + 3, y2: top + 3 };
      const clashAbove = top < LINE
        || boxes.some((other) => above.x1 < other.x2 && above.x2 > other.x1 && above.y1 < other.y2 && above.y2 > other.y1);
      if (clashAbove) continue;
      node.__label = { x, y: top };
      boxes.push(above);
      placed += 1;
      continue;
    }
    node.__label = { x, y };
    boxes.push(box);
    placed += 1;
  }
  return placed;
}

function sharedFor(edges, slug) {
  const found = edges.filter((edge) => edge.source === slug || edge.target === slug)
    .sort((a, b) => b.weight - a.weight)[0];
  return found ? found.shared.join(', ') : '';
}

function clusterLabel(cluster) {
  const topic = (cluster.topic || []).join(', ');
  if (topic) return topic;
  return cluster.size === 1 ? 'stands alone' : 'no shared subject';
}

function wireMapInteractions(svg, host) {
  const content = $('.map-view', svg);
  if (!content) return null;

  if (activeMapCleanup) {
    activeMapCleanup();
    activeMapCleanup = null;
  }

  const MIN_ZOOM = 0.35;
  const MAX_ZOOM = 6;
  const view = { x: 0, y: 0, k: 1 };
  let drag = null;
  let suppressClick = false;

  const apply = () => content.setAttribute(
    'transform', `translate(${view.x.toFixed(2)} ${view.y.toFixed(2)}) scale(${view.k.toFixed(4)})`);
  apply();

  const toView = (clientX, clientY) => {
    const rect = svg.getBoundingClientRect();
    const box = svg.viewBox.baseVal;
    return {
      x: ((clientX - rect.left) / Math.max(rect.width, 1)) * box.width,
      y: ((clientY - rect.top) / Math.max(rect.height, 1)) * box.height,
    };
  };

  const zoomAt = (point, factor) => {
    const next = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, view.k * factor));
    if (Math.abs(next - view.k) < 1e-4) return false;
    const ratio = next / view.k;
    view.x = point.x - (point.x - view.x) * ratio;
    view.y = point.y - (point.y - view.y) * ratio;
    view.k = next;
    apply();
    return true;
  };

  svg.addEventListener('wheel', (event) => {
    const point = toView(event.clientX, event.clientY);
    const steps = Math.min(Math.abs(event.deltaY) / 120, 4);
    if (steps < 0.05) return;
    const factor = Math.pow(event.deltaY < 0 ? 1.15 : 1 / 1.15, steps);
    if (!zoomAt(point, factor)) return;
    event.preventDefault();
  }, { passive: false });

  svg.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    drag = { id: event.pointerId, x: event.clientX, y: event.clientY, vx: view.x, vy: view.y, moved: 0 };
    svg.classList.add('dragging');
  });

  const onMove = (event) => {
    if (!drag || event.pointerId !== drag.id) return;
    drag.moved = Math.max(drag.moved, Math.hypot(event.clientX - drag.x, event.clientY - drag.y));
    const from = toView(drag.x, drag.y);
    const now = toView(event.clientX, event.clientY);
    view.x = drag.vx + (now.x - from.x);
    view.y = drag.vy + (now.y - from.y);
    apply();
  };

  const onUp = (event) => {
    if (!drag || event.pointerId !== drag.id) return;
    suppressClick = drag.moved > 4;
    drag = null;
    svg.classList.remove('dragging');
  };

  window.addEventListener('pointermove', onMove);
  window.addEventListener('pointerup', onUp);
  window.addEventListener('pointercancel', onUp);

  const removeListeners = () => {
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onUp);
    window.removeEventListener('pointercancel', onUp);
  };
  const unregister = registerRouteCleanup(() => {
    removeListeners();
    activeMapCleanup = null;
  });
  activeMapCleanup = () => {
    removeListeners();
    unregister();
  };

  svg.addEventListener('click', () => { setTimeout(() => { suppressClick = false; }, 0); }, true);

  $$('[data-map-zoom]', host).forEach((button) => button.addEventListener('click', () => {
    const action = button.dataset.mapZoom;
    const box = svg.viewBox.baseVal;
    const centre = { x: box.width / 2, y: box.height / 2 };
    if (action === 'in') zoomAt(centre, 1.3);
    else if (action === 'out') zoomAt(centre, 1 / 1.3);
    else { view.x = 0; view.y = 0; view.k = 1; apply(); }
  }));

  host.__mapSuppressed = () => suppressClick;
  return view;
}

async function renderCorpusMap(host) {
  let data;
  try {
    data = await api('/api/analytics/corpus');
  } catch (err) {
    if (host.isConnected) {
      host.innerHTML = `<div class="panel"><h3>Passage map</h3>
        <p class="panel-sub">${escapeHtml(err.message)}</p></div>`;
    }
    return;
  }
  if (!host.isConnected) return;

  const width = 860;
  const height = 520;
  const nodes = layoutGraph(data.nodes.map((n) => ({ ...n })), data.edges, width, height);
  const bySlug = new Map(nodes.map((n) => [n.slug, n]));
  const maxWords = Math.max(...nodes.map((n) => n.words), 1);
  const radius = (words) => 5 + Math.sqrt(words / maxWords) * 13;
  const clusterColour = ['var(--accent)', 'var(--es-ink)', 'var(--warn)', 'var(--bad)'];
  const labelled = placeLabels(nodes, radius, width, height);
  const clusterOf = new Map(nodes.map((node) => [node.slug, node.cluster]));
  const langLabel = targetName();

  host.innerHTML = `<div class="panel">
    <h3>Passage map</h3>
    <p class="panel-sub">Two passages are joined when they share vocabulary. Rare words
      count for more than common ones, so the links are real subjects rather than
      articles that both happen to say "ejemplo". Node size is length; colour is the
      group the layout found, and each group is named from the words its members'
      titles share, so you can check the name against the passages in it. The ring is
      how much of it you already know. Hover a node to see what it shares, click it to
      read it, and click a group in the legend to see only that group.</p>
    <div class="map-wrap">
      <svg id="corpus-map" viewBox="0 0 ${width} ${height}" role="img"
           aria-label="Articles connected by shared vocabulary">
        <g class="map-view">
          <g class="map-edges">
            ${data.edges.map((edge) => {
              const a = bySlug.get(edge.source);
              const b = bySlug.get(edge.target);
              if (!a || !b) return '';
              return `<line x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}"
                            x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}"
                            data-a="${escapeHtml(edge.source)}" data-b="${escapeHtml(edge.target)}"
                            stroke-width="${(0.6 + edge.weight * 2.4).toFixed(2)}"
                            stroke-opacity="${(0.18 + edge.weight * 0.5).toFixed(2)}"></line>`;
            }).join('')}
          </g>
          <g class="map-nodes">
            ${nodes.map((node) => `
              <g class="map-node" data-slug="${escapeHtml(node.slug)}"
                 data-cluster="${escapeHtml(node.cluster)}"
                 data-title="${escapeHtml(node.title)}"
                 data-shared="${escapeHtml(sharedFor(data.edges, node.slug))}">
                <circle cx="${node.x.toFixed(1)}" cy="${node.y.toFixed(1)}" r="${radius(node.words).toFixed(1)}"
                        fill="${clusterColour[node.cluster % clusterColour.length]}"
                        stroke="var(--surface)" stroke-width="2"></circle>
                <circle cx="${node.x.toFixed(1)}" cy="${node.y.toFixed(1)}" r="${(radius(node.words) + 3.5).toFixed(1)}"
                        fill="none" stroke="var(--ink-2)" stroke-width="1.6"
                        stroke-dasharray="${(2 * Math.PI * (radius(node.words) + 3.5) * Math.max(node.coverage, 0.02)).toFixed(1)} 999"
                        transform="rotate(-90 ${node.x.toFixed(1)} ${node.y.toFixed(1)})"
                        opacity="${node.coverage > 0.02 ? 0.85 : 0}"></circle>
                ${node.__label ? `<text x="${node.__label.x.toFixed(1)}" y="${node.__label.y.toFixed(1)}"
                      text-anchor="middle">${escapeHtml(shortTitle(node.title))}</text>` : ''}
              </g>`).join('')}
          </g>
        </g>
      </svg>
      <div class="map-controls">
        <button data-map-zoom="in" title="Zoom in">＋</button>
        <button data-map-zoom="out" title="Zoom out">−</button>
        <button data-map-zoom="reset" title="Fit the whole map">Fit</button>
      </div>
    </div>
    <p class="small muted map-hint">Drag to pan. Scroll to zoom. Click a passage to read it.
      click a group to see only it</p>
    <div class="legend legend-groups">
      ${data.clusters.map((cluster) => `<button class="item" data-cluster="${escapeHtml(cluster.id)}"
          title="${escapeHtml(cluster.terms.length
            ? `Shares the vocabulary: ${cluster.terms.join(', ')}` : 'No words in common with the rest')}">
        <span class="swatch" style="background:${clusterColour[cluster.id % clusterColour.length]}"></span>
        ${cluster.size} ${cluster.size === 1 ? 'passage' : 'passages'}
        <span class="n">${escapeHtml(clusterLabel(cluster))}</span>
      </button>`).join('')}
      <span class="item"><span class="swatch ring-swatch"></span>ring = how much you already know</span>
    </div>
    ${labelled < nodes.length ? `<p class="small muted" style="margin-top:8px">
      ${nodes.length - labelled} of ${nodes.length} labels are hidden to keep the map legible.
      Hover any node for its title.</p>` : ''}

    ${data.within_reach.length ? `
      <h4 class="sub-head">Closest to being readable</h4>
      <p class="panel-sub">Ranked by how much of their ${escapeHtml(langLabel)} is already in your vocabulary.</p>
      <div class="row">
        ${data.within_reach.map((item) => `<a class="btn sm" href="#/read/${encodeURIComponent(item.slug)}">
          ${escapeHtml(shortTitle(item.title))}
          <span class="muted">${Math.round(item.coverage * 100)}% known</span></a>`).join('')}
      </div>` : ''}
  </div>`;

  wireMapInteractions($('#corpus-map', host), host);

  let focusedCluster = null;
  let hovered = null;
  const paint = () => {
    const near = new Set();
    if (hovered) {
      near.add(hovered);
      data.edges.forEach((edge) => {
        if (edge.source === hovered) near.add(edge.target);
        if (edge.target === hovered) near.add(edge.source);
      });
    }
    $$('.map-node', host).forEach((other) => {
      const inCluster = focusedCluster === null || Number(other.dataset.cluster) === focusedCluster;
      const nearHover = !hovered || near.has(other.dataset.slug);
      other.classList.toggle('dim', !(inCluster && nearHover));
    });
    $$('.map-edges line', host).forEach((line) => {
      const both = clusterOf.get(line.dataset.a) === focusedCluster
        && clusterOf.get(line.dataset.b) === focusedCluster;
      const touches = !hovered || line.dataset.a === hovered || line.dataset.b === hovered;
      line.classList.toggle('dim', !((focusedCluster === null || both) && touches));
    });
  };

  $$('.legend-groups [data-cluster]', host).forEach((button) => button.addEventListener('click', () => {
    const id = Number(button.dataset.cluster);
    focusedCluster = focusedCluster === id ? null : id;
    $$('.legend-groups [data-cluster]', host).forEach((other) =>
      other.classList.toggle('active', Number(other.dataset.cluster) === focusedCluster));
    paint();
  }));

  $$('.map-node', host).forEach((group) => {
    group.addEventListener('click', () => {
      if (host.__mapSuppressed && host.__mapSuppressed()) return;
      location.hash = `#/read/${encodeURIComponent(group.dataset.slug)}`;
    });
    group.addEventListener('mouseenter', () => { hovered = group.dataset.slug; paint(); });
    group.addEventListener('mouseleave', () => { hovered = null; paint(); });
    const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
    title.textContent = `${group.dataset.title}${group.dataset.shared ? `\nshares: ${group.dataset.shared}` : ''}`;
    group.appendChild(title);
  });
}

function openReset(stats) {
  openModal('Start over?', `
    <p style="margin-top:0">This clears everything the app has measured about you:</p>
    <ul class="reset-list">
      <li>your saved words and their review schedule: <strong>${stats.words}</strong> cards</li>
      <li>every review, every word lookup, and the reading history behind your streak</li>
      <li>where you are in each article, and which ones you finished</li>
      <li>the sentences you kept, your writing, your weekly goal</li>
    </ul>
    <p>It keeps <strong>your lessons</strong>: the corpus, and everything in your library folder.
      Files are not touched by this.</p>
    <p class="small muted">A copy of the database is written to <code>data/backups</code> first, so this
      is recoverable, but only by hand, from that file.</p>
    <div class="row">
      <button class="btn danger" id="reset-go">Clear all records and progress</button>
      <button class="btn ghost" id="reset-cancel">Cancel</button>
    </div>`);
  $('#reset-cancel').addEventListener('click', closeModal);
  $('#reset-go').addEventListener('click', async () => {
    const button = $('#reset-go');
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span> clearing';
    try {
      const result = await api('/api/reset', { method: 'POST' });
      closeModal();
      toast(`Cleared ${result.cleared.words} words and ${result.cleared.reviews} reviews. `
        + 'lessons untouched, backup in data/backups');
      refreshDuePill();
      viewStats();
    } catch (err) {
      button.disabled = false;
      button.textContent = 'Clear all records and progress';
      toast(err.message, 'err');
    }
  });
}

export async function viewStats() {
  setActiveTab('stats');
  $('#reader-bar').hidden = true;
  if (activeMapCleanup) {
    activeMapCleanup();
    activeMapCleanup = null;
  }
  const view = $('#view');
  view.className = 'view';
  view.innerHTML = '<div class="empty"><span class="spinner"></span></div>';

  const [stats, library, words] = await Promise.all([
    api('/api/stats'),
    api('/api/library'),
    api('/api/words'),
  ]);

  const activity = stats.activity || [];
  const peak = Math.max(1, ...activity.map((d) => d.reviews));
  const totalReviews = activity.reduce((sum, d) => sum + d.reviews, 0);
  const stageOrder = STAGE_ORDER;
  const stageTotal = Math.max(1, Object.values(stats.stages).reduce((a, b) => a + b, 0));
  const langLabel = targetName();

  view.innerHTML = `
    <div class="page-head">
      <h1>Progress</h1>
      <p>How much you have read, and how well the words have stuck.</p>
    </div>

    <div id="challenge-panel"></div>
    <div id="reading-analytics"></div>
    <div id="corpus-map-panel" class="panel"><div class="loading">
      <span class="spinner"></span> mapping the corpus</div></div>

    <div class="tiles">
      <div class="tile"><div class="k">Streak</div><div class="v">${stats.streak.current}</div>
        <div class="sub">day${stats.streak.current === 1 ? '' : 's'}. Best ${stats.streak.longest}.</div></div>
      <div class="tile"><div class="k">Words saved</div><div class="v">${stats.words}</div>
        <div class="sub">${stats.stages.mature} mature</div></div>
      <div class="tile"><div class="k">Due now</div><div class="v">${stats.due}</div>
        <div class="sub">${stats.reviews_today} reviewed today</div></div>
      <div class="tile"><div class="k">Articles read</div><div class="v">${stats.articles_completed}</div>
        <div class="sub">of ${library.articles.length} in the library</div></div>
      <div class="tile"><div class="k">Sentences kept</div><div class="v">${stats.quotes || 0}</div>
        <div class="sub">${(stats.quotes || 0) ? '<a href="#/quotes">your quotes</a>' : 'keep one while reading'}</div></div>
    </div>

    <div class="panel">
      <h3>Reviews, last 30 days</h3>
      <p class="panel-sub">${totalReviews} review${totalReviews === 1 ? '' : 's'} in this window, with a peak of ${peak} in a day.</p>
      <div class="chart" id="reviews-chart">
        ${activity.map((d) => `<div class="col ${d.reviews ? '' : 'zero'}" title="${escapeHtml(d.day)}: ${d.reviews} review${d.reviews === 1 ? '' : 's'}">
          <i style="height:${d.reviews ? Math.max(4, Math.round(d.reviews / peak * 100)) : 2}%"></i></div>`).join('')}
      </div>
      <div class="chart-axis"><span>${escapeHtml(activity[0] ? activity[0].day.slice(5) : '')}</span>
        <span>today</span></div>
    </div>

    <div class="panel">
      <h3>How well the vocabulary has stuck</h3>
      <p class="panel-sub">Spaced repetition stages. A word graduates to <em>mature</em> after three weeks of intervals.</p>
      <div class="stack">
        ${stageOrder.map(([key, , color]) => stats.stages[key]
          ? `<span style="background:${color};width:${(stats.stages[key] / stageTotal * 100).toFixed(1)}%"
                   title="${escapeHtml(key)}: ${stats.stages[key]}"></span>` : '').join('')}
      </div>
      <div class="legend">
        ${stageOrder.map(([key, label, color]) => `<span class="item">
          <span class="swatch" style="background:${color}"></span>${escapeHtml(label)}
          <span class="n">${stats.stages[key]}</span></span>`).join('')}
      </div>
    </div>

    <div class="panel">
      <h3>Words</h3>
      <p class="panel-sub">${words.words.length} saved${stats.reviews_total ? `, ${stats.reviews_total} reviews all time` : ''}.
        <a href="#/cards">See every card</a>. <a href="/api/export.csv">Export for Anki</a>.</p>
      ${words.words.length ? `<p class="small muted">Most recent:
        ${words.words.slice(0, 6).map((w) => `<span class="chip es">${escapeHtml(w.term)}</span>`).join(' ')}</p>`
        : `<p class="muted small">Nothing saved yet. Click any ${escapeHtml(langLabel)} word while reading.</p>`}
    </div>

    <div class="panel danger-zone">
      <h3>Start over</h3>
      <p class="panel-sub">Clears the deck, the schedule, your reading positions, the sentences you kept,
        your writing, and all of the history measured from them. <strong>Your lessons stay.</strong>
        They are files, and this does not touch files.</p>
      <div class="row">
        <button class="btn ghost sm danger-hover" id="btn-reset">Reset all records and progress</button>
        <span class="small muted">A copy of the database is saved to <code>data/backups</code> first.</span>
      </div>
    </div>`;

  renderChallenge();
  renderReadingAnalytics($('#reading-analytics'));
  renderCorpusMap($('#corpus-map-panel'));
  $('#btn-reset').addEventListener('click', () => openReset(stats));
}
