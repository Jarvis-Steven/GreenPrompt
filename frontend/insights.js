/* GreenPrompt — request insights panel, header counter, "what happened" strip.
   PROPOSAL for the frontend member. Plain DOM, no dependency, no build step.

   Every number rendered here comes from the /chat response. Nothing is
   derived, estimated or invented on the client. A field the backend did not
   send is simply not drawn.
*/

const GP_REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)');

function gpEl(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* label / value row */
function gpRow(label, value, valueClass) {
  const row = gpEl('div', 'insights-row');
  row.append(gpEl('span', '', label), gpEl('strong', valueClass || '', value));
  return row;
}

function gpHeading(text) {
  return gpEl('h4', 'insights-heading', text);
}

const gpTier = (t) => (t ? t.charAt(0).toUpperCase() + t.slice(1) : '—');
const gpNum = (v, digits) => (Number.isFinite(v) ? v.toFixed(digits) : null);

/* Units kept identical to the contract: Wh, g, mL, INR. */
function gpMetricRows(target, metrics) {
  if (!metrics) return;
  target.append(gpRow('Energy', `${gpNum(metrics.energy_wh, 2)} Wh`));
  target.append(gpRow('CO2', `${gpNum(metrics.co2_g, 3)} g`));
  target.append(gpRow('Water', `${gpNum(metrics.water_ml, 2)} mL`));
  target.append(gpRow('Cost', `₹${gpNum(metrics.cost_inr, 3)}`));
}

/* One table instead of repeating four measures per section.
   Columns: what this request cost, and what it saved against always-Big. */
function gpMetricTable(impact, savings) {
  const rows = [
    ['Energy', (m) => `${gpNum(m.energy_wh, 2)} Wh`, 'energy_wh'],
    ['CO2', (m) => `${gpNum(m.co2_g, 3)} g`, 'co2_g'],
    ['Water', (m) => `${gpNum(m.water_ml, 2)} mL`, 'water_ml'],
    ['Cost', (m) => `₹${gpNum(m.cost_inr, 3)}`, 'cost_inr'],
  ];
  const table = gpEl('table', 'insights-table');
  const head = document.createElement('tr');
  head.append(gpEl('th', '', ''), gpEl('th', '', 'This request'), gpEl('th', '', 'Saved'));
  table.append(head);
  for (const [label, fmt, key] of rows) {
    const tr = document.createElement('tr');
    tr.append(gpEl('td', 'insights-measure', label));
    tr.append(gpEl('td', '', impact ? fmt(impact) : '—'));
    const saved = savings ? savings[key] : null;
    const cell = gpEl('td', Number.isFinite(saved) && saved < 0 ? 'negative' : 'saved',
      savings ? `${Number.isFinite(saved) && saved >= 0 ? '+' : ''}${fmt(savings)}` : '—');
    tr.append(cell);
    table.append(tr);
  }
  return table;
}


/* ---------- the panel ---------- */
function renderInsights(data) {
  const live = document.getElementById('insights-live');
  const empty = document.getElementById('insights-empty');
  if (!live || !data) return;
  empty.hidden = true;
  live.hidden = false;

  const mock = document.getElementById('insights-mock');
  if (mock) mock.hidden = !(typeof CONFIG !== 'undefined' && CONFIG.USE_MOCK_API);

  // Path
  const path = document.getElementById('insights-path');
  path.replaceChildren(gpHeading('Routing path'));
  path.append(gpRow('Difficulty', gpTier(data.difficulty)));
  path.append(gpRow('Path', `${gpTier(data.initial_model)} → ${gpTier(data.final_model)}`));
  path.append(gpRow('Escalated', data.escalated ? 'Yes' : 'No'));
  const classifier = data.classifier || {};
  path.append(gpRow('Classifier', classifier.used
    ? `Called (${classifier.method})`
    : `Not called (${classifier.method || 'n/a'})`));

  const attempts = gpEl('div', 'insights-attempts');
  (data.attempts || []).forEach((a, i) => {
    const item = gpEl('div', `insights-attempt ${a.status === 'error' ? 'is-error' : ''}`);
    item.append(gpEl('strong', '', `${i + 1}. ${gpTier(a.tier)} — ${a.model_name}`));
    const tokens = (a.input_tokens == null && a.output_tokens == null)
      ? 'tokens not reported'
      : `${a.input_tokens ?? '—'} in / ${a.output_tokens ?? '—'} out`;
    item.append(gpEl('small', '', `${a.status} · ${a.latency_ms} ms · ${tokens}`));
    attempts.append(item);
  });
  if (attempts.childElementCount) path.append(attempts);

  // Quality
  const quality = document.getElementById('insights-quality');
  quality.replaceChildren(gpHeading('Quality check'));
  const status = data.quality?.status || 'unchecked';
  quality.append(gpRow('Status', status, `quality-${status}`));
  if (data.quality?.reason) quality.append(gpEl('p', 'insights-reason', data.quality.reason));

  // Impact + savings as ONE table: four measures, two columns.
  const impact = document.getElementById('insights-impact');
  impact.replaceChildren(gpHeading('Estimated impact'));
  impact.append(gpMetricTable(data.impact, data.savings));

  // Classifier overhead — optional, drawn only when the backend sent it
  const overhead = document.getElementById('insights-overhead');
  overhead.replaceChildren();
  if (data.classifier_overhead) {
    const o = data.classifier_overhead;
    const withIt = data.impact_including_classifier;
    overhead.append(gpRow('Classifier overhead (est.)',
      `${gpNum(o.energy_wh, 2)} Wh · ₹${gpNum(o.cost_inr, 3)}`));
    if (withIt) {
      overhead.append(gpRow('Total incl. classifier',
        `${gpNum(withIt.energy_wh, 2)} Wh · ₹${gpNum(withIt.cost_inr, 3)}`));
    }
  }

  // Session summary
  const summary = document.getElementById('insights-summary');
  summary.replaceChildren(gpHeading('This session'));
  const s = data.summary || {};
  const cumulative = s.cumulative_savings;
  summary.append(gpRow('Prompts',
    `${s.total_prompts ?? '—'} · ${Number.isFinite(s.small_model_percentage)
      ? s.small_model_percentage.toFixed(0) : '—'}% on Small · ${s.escalations ?? '—'} escalated`));
  if (cumulative) {
    summary.append(gpRow('Saved so far',
      `${gpNum(cumulative.energy_wh, 2)} Wh · ₹${gpNum(cumulative.cost_inr, 3)}`,
      cumulative.energy_wh < 0 ? 'negative' : ''));
  }

  updateInsightsBadge(data);
  updateHeaderSaving(s.cumulative_savings);
}

/* Badge shows the latest REAL saving only. */
function updateInsightsBadge(data) {
  const badge = document.getElementById('insights-badge');
  if (!badge) return;
  const value = data?.savings?.energy_wh;
  if (!Number.isFinite(value)) { badge.hidden = true; return; }
  badge.hidden = false;
  badge.textContent = `${value >= 0 ? '+' : ''}${value.toFixed(2)} Wh`;
  badge.classList.toggle('negative', value < 0);
}

/* Header counter, animated gently unless reduced motion is requested. */
let gpCounterFrom = 0;
function updateHeaderSaving(cumulative) {
  const wrap = document.getElementById('header-saving');
  const out = document.getElementById('header-saving-value');
  if (!wrap || !out || !cumulative || !Number.isFinite(cumulative.energy_wh)) return;
  wrap.hidden = false;
  const to = cumulative.energy_wh;
  if (GP_REDUCED.matches) { out.textContent = `${to.toFixed(2)} Wh`; gpCounterFrom = to; return; }
  const from = gpCounterFrom;
  const start = performance.now();
  const step = (now) => {
    const t = Math.min(1, (now - start) / 600);
    const eased = 1 - Math.pow(1 - t, 3);
    out.textContent = `${(from + (to - from) * eased).toFixed(2)} Wh`;
    if (t < 1) requestAnimationFrame(step); else gpCounterFrom = to;
  };
  requestAnimationFrame(step);
  // Guarantee the final value even where rAF is throttled (background tab,
  // headless run). The counter must never be left showing a stale number.
  setTimeout(() => { out.textContent = `${to.toFixed(2)} Wh`; gpCounterFrom = to; }, 700);
}

/* ---------- "what happened" strip under an answer ---------- */
function buildWhatHappened(data) {
  const strip = gpEl('button', 'what-happened');
  strip.type = 'button';
  strip.setAttribute('aria-label', 'Open request insights for this answer');
  const status = data.quality?.status || 'unchecked';
  const add = (text, cls) => strip.append(gpEl('span', `wh-chip ${cls || ''}`, text));
  // The tier and model already appear in the meta line above, so the strip
  // carries only what is NOT shown there.
  add(`${gpTier(data.difficulty)} prompt`);
  add((data.classifier && data.classifier.used) ? 'classifier used' : 'rules only');
  if (data.escalated) add(`escalated to ${gpTier(data.final_model)}`, 'wh-escalated');
  add(status, `wh-quality quality-${status}`);
  const saved = data.savings?.energy_wh;
  if (Number.isFinite(saved)) {
    add(`${saved >= 0 ? '+' : ''}${saved.toFixed(2)} Wh`, saved < 0 ? 'negative' : 'wh-saving');
  }
  strip.addEventListener('click', () => {
    renderInsights(data);
    if (typeof toggleMatrix === 'function') toggleMatrix(true);
  });
  return strip;
}

/* ---------- focus handling for the panel ---------- */
const GP_FOCUSABLE =
  'a[href],button:not([disabled]),textarea,input,select,[tabindex]:not([tabindex="-1"])';
let gpLastFocus = null;

function gpTrapFocus(event) {
  const drawer = document.getElementById('model-matrix');
  if (!drawer || !drawer.classList.contains('open') || event.key !== 'Tab') return;
  const items = [...drawer.querySelectorAll(GP_FOCUSABLE)].filter(el => el.offsetParent !== null);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}

function gpRememberFocus(open) {
  if (open) {
    gpLastFocus = document.activeElement;
  } else if (gpLastFocus && document.contains(gpLastFocus)) {
    gpLastFocus.focus();
    gpLastFocus = null;
  }
}

document.addEventListener('keydown', gpTrapFocus);


/* Meta line under an answer. Shows the REAL model name, and only mentions a
   path when the request actually escalated - no more "Medium Medium Medium". */
function buildAnswerMeta(response) {
  const attempts = response.attempts || [];
  const producing = [...attempts].reverse()
    .find(a => a.tier === response.final_model && a.status === 'success') || attempts.at(-1) || {};
  const quality = response.quality?.status || 'unchecked';
  const qualityText = quality === 'passed' ? 'Passed'
    : quality === 'failed' ? 'Failed' : 'Not checked';

  const pairs = [[`${gpTier(producing.tier || response.final_model)} · ${producing.model_name || '—'}`,
                  'Answered by']];
  if (response.escalated && attempts.length > 1) {
    // Every attempt, so an escalation shows what was tried and what replaced it.
    pairs.push([attempts.map(a => `${gpTier(a.tier)} · ${a.model_name}`
      + (a.status === 'error' ? ' (failed)' : '')).join('  →  '), 'Path']);
  }
  pairs.push([qualityText, 'Quality']);
  return pairs;
}
