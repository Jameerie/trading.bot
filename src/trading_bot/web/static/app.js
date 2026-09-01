/* trading.bot UI logic.
 *
 * No framework and no build step: the server is a stdlib HTTP server, and the
 * client stays in the same spirit so the whole thing runs anywhere Python does.
 *
 * Charts are hand-drawn SVG rather than a charting library, both to keep the
 * strict same-origin CSP (no CDN scripts) and because what needs drawing is
 * narrow: candles plus three horizontal levels.
 */
'use strict';

/* ------------------------------------------------------------------ utils */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** Access token, if the server is running with one. Kept out of the URL bar. */
const TOKEN = new URLSearchParams(location.search).get('token') || '';

/** Escape text before it goes anywhere near innerHTML. */
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

const fmtPrice = (value, digits = 5) => Number(value).toFixed(digits);
const fmtPct = (value, dp = 1) =>
  value === null || value === undefined ? '-' : `${(value * 100).toFixed(dp)}%`;
const fmtR = (value, dp = 2) =>
  value === null || value === undefined ? '-' : `${value >= 0 ? '+' : ''}${value.toFixed(dp)}R`;
const fmtNum = (value, dp = 2) =>
  value === null || value === undefined ? '-' : Number(value).toLocaleString(undefined,
    { minimumFractionDigits: dp, maximumFractionDigits: dp });
const signClass = (value) => (value > 0 ? 'pos' : value < 0 ? 'neg' : '');

/** Fetch JSON from the API, surfacing server-side errors as thrown Errors. */
async function api(path, { method = 'GET', body = null, params = null } = {}) {
  const url = new URL(path, location.origin);
  if (params) {
    Object.entries(params).forEach(([k, v]) => {
      if (v !== '' && v !== null && v !== undefined) url.searchParams.set(k, v);
    });
  }
  if (TOKEN) url.searchParams.set('token', TOKEN);

  const options = { method, headers: {} };
  if (TOKEN) options.headers.Authorization = `Bearer ${TOKEN}`;
  if (body) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }

  const response = await fetch(url, options);
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error(`server returned ${response.status} with no JSON body`);
  }
  if (!response.ok) throw new Error(payload.error || `request failed (${response.status})`);
  return payload;
}

let toastTimer = null;
function toast(message, kind = 'info') {
  const node = $('#toast');
  node.textContent = message;
  node.dataset.kind = kind;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 4200);
}

function setStatus(text, state) {
  const pill = $('#status-pill');
  pill.textContent = text;
  pill.dataset.state = state;
}

/** Run an async action with button + status feedback, never leaving them stuck. */
async function withBusy(button, label, fn) {
  const original = button ? button.textContent : '';
  if (button) { button.disabled = true; button.innerHTML = `<span class="spin"></span>${label}`; }
  setStatus(label, 'busy');
  try {
    const result = await fn();
    setStatus('ready', 'ok');
    return result;
  } catch (error) {
    setStatus('error', 'error');
    toast(error.message, 'error');
    throw error;
  } finally {
    if (button) { button.disabled = false; button.textContent = original; }
  }
}

/* ------------------------------------------------------------------ charts */

/**
 * Candlestick chart with entry / stop / target overlaid.
 *
 * Scaling is candles-first. Including a distant target in the price range is the
 * obvious approach and the wrong one: a 4R target sits far outside the recent
 * range by construction, so honouring it squashes every candle into a band too
 * thin to read. Instead the range comes from the candles, levels are drawn where
 * they fall, and a level outside the range is pinned to the edge with a marker
 * saying it continues beyond. The exact prices are in the ladder directly below,
 * so nothing is lost by not labelling them here.
 *
 * Level text lives in HTML rather than SVG because the chart stretches
 * non-uniformly to fill its column, which would distort any text inside it.
 */
function candleChart(candles, signal) {
  if (!candles || candles.length < 2) return '';
  const W = 640, H = 148, padY = 10;

  let lo = Math.min(...candles.map((c) => c.l));
  let hi = Math.max(...candles.map((c) => c.h));
  if (hi === lo) { hi += 0.0001; lo -= 0.0001; }

  // A little headroom so a level sitting just outside still reads as "just".
  const margin = (hi - lo) * 0.06;
  lo -= margin; hi += margin;
  const span = hi - lo;

  const y = (p) => padY + (hi - p) / span * (H - padY * 2);
  const clampY = (p) => Math.min(H - 2, Math.max(2, y(p)));
  const step = W / candles.length;
  const bw = Math.max(1, Math.min(7, step * 0.62));

  let body = '';
  candles.forEach((c, i) => {
    const cx = i * step + step / 2;
    const cls = c.c >= c.o ? 'chart__up' : 'chart__down';
    const yo = y(c.o), yc = y(c.c);
    body += `<line class="chart__wick" x1="${cx.toFixed(1)}" x2="${cx.toFixed(1)}"`
         +  ` y1="${y(c.h).toFixed(1)}" y2="${y(c.l).toFixed(1)}"/>`
         +  `<rect class="${cls}" x="${(cx - bw / 2).toFixed(1)}" y="${Math.min(yo, yc).toFixed(1)}"`
         +  ` width="${bw.toFixed(1)}" height="${Math.max(1, Math.abs(yc - yo)).toFixed(1)}"/>`;
  });

  let levels = '';
  let legend = '';
  if (signal) {
    const digits = signal.digits ?? 5;
    [
      ['tp', signal.take_profit, 'Target'],
      ['entry', signal.entry, 'Entry'],
      ['sl', signal.stop_loss, 'Stop'],
    ].forEach(([kind, price, label]) => {
      const offScale = price > hi || price < lo;
      const yy = clampY(price);
      levels += `<line class="chart__lvl chart__lvl--${kind}${offScale ? ' is-off' : ''}"`
             +  ` x1="0" x2="${W}" y1="${yy.toFixed(1)}" y2="${yy.toFixed(1)}"/>`;
      legend += `<span class="legend__item legend__item--${kind}">${esc(label)}`
             +  ` <b>${esc(fmtPrice(price, digits))}</b>`
             +  `${offScale ? '<i title="beyond the visible range">&#8599;</i>' : ''}</span>`;
    });
  }

  return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
    role="img" aria-label="Recent price action with trade levels">${body}${levels}</svg>
    ${legend ? `<div class="legend">${legend}</div>` : ''}</div>`;
}

/** Cumulative-R line for a backtest. */
function equityChart(curve) {
  if (!curve || curve.length < 2) return '';
  const W = 640, H = 76, pad = 6;
  const lo = Math.min(0, ...curve), hi = Math.max(0, ...curve);
  const span = (hi - lo) || 1;
  const x = (i) => (i / (curve.length - 1)) * W;
  const y = (v) => pad + (hi - v) / span * (H - pad * 2);
  const path = curve.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join('');
  const zero = y(0).toFixed(1);
  return `<div class="equity"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
    role="img" aria-label="Cumulative R over the trade sequence">
    <line class="equity__zero" x1="0" x2="${W}" y1="${zero}" y2="${zero}"/>
    <path class="equity__line" d="${path}"/></svg></div>`;
}

/* ----------------------------------------------------------- signal cards */

function ladder(signal) {
  const d = signal.digits ?? 5;
  const risk = signal.risk_pips, reward = signal.reward_pips;
  const total = risk + reward || 1;
  return `
    <div class="ladder">
      <div class="ladder__row">
        <span class="ladder__label">Target</span>
        <span class="ladder__bar"><span class="ladder__fill ladder__fill--tp"
          style="width:${(reward / total * 100).toFixed(1)}%"></span></span>
        <span><span class="ladder__val">${esc(fmtPrice(signal.take_profit, d))}</span>
          <span class="ladder__sub"> +${reward.toFixed(1)}p</span></span>
      </div>
      <div class="ladder__row">
        <span class="ladder__label">Entry</span>
        <span class="ladder__bar"></span>
        <span><span class="ladder__val">${esc(fmtPrice(signal.entry, d))}</span></span>
      </div>
      <div class="ladder__row">
        <span class="ladder__label">Stop</span>
        <span class="ladder__bar"><span class="ladder__fill ladder__fill--risk"
          style="width:${(risk / total * 100).toFixed(1)}%"></span></span>
        <span><span class="ladder__val">${esc(fmtPrice(signal.stop_loss, d))}</span>
          <span class="ladder__sub"> -${risk.toFixed(1)}p</span></span>
      </div>
    </div>`;
}

/* The prediction a signal amounts to: a claim, a deadline in the reader's own
   clock, and the measured frequency of claims like it coming true. When there is
   no sample worth quoting the block says so rather than showing a number. */
function predictionBlock(s) {
  const p = s.prediction;
  if (!p) return '';
  const rate = p.base_rate || {};
  const odds = rate.measured
    ? `${fmtPct(rate.win_rate, 0)} of ${rate.sample} comparable ${esc(s.symbol)} setups
       reached target before stop (${fmtPct(rate.interval_low, 0)}&ndash;${fmtPct(rate.interval_high, 0)}).
       <span class="pred__src">measured on ${esc(rate.source || '')}</span>`
    : `<span class="pred__none">No measured base rate for ${esc(s.symbol)} yet
       &mdash; ${rate.sample || 0} comparable setups. This prediction is unscored:
       treat it as an untested idea and size it as one.</span>`;
  return `
    <div class="pred">
      <div class="pred__h">The prediction</div>
      <p class="pred__claim">${esc(p.claim)}</p>
      <div class="pred__grid">
        <div><span class="pred__k">Enter by</span>
          <span class="pred__v">${esc(p.entry_deadline_local || p.entry_deadline)}</span></div>
        <div><span class="pred__k">Resolves by</span>
          <span class="pred__v">${esc(p.resolve_by_local || p.resolve_by)}</span></div>
      </div>
      <p class="pred__rate">${odds}</p>
    </div>`;
}

/* The playbook, collapsed. A first-time reader opens every section; someone who
   has placed fifty of these opens none, and neither has to scroll past the other. */
function playbookBlock(s) {
  const pb = s.playbook;
  if (!pb) return '';
  const section = (title, lines, open) => {
    if (!lines || !lines.length) return '';
    const body = lines.map((l) => esc(l)).join('\n');
    return `<details class="plan"${open ? ' open' : ''}>
        <summary class="plan__h">${esc(title)}</summary>
        <pre class="plan__body">${body}</pre>
      </details>`;
  };
  return `<div class="plans">
      ${section('How to place it', pb.order, true)}
      ${section('When to watch it', pb.timing, false)}
      ${section('What would make this wrong', pb.invalidation, false)}
      ${section('While it is running', pb.management, false)}
      ${section('What if...', pb.contingencies, false)}
      ${section('Afterwards', pb.aftercare, false)}
    </div>`;
}

function signalCard(row) {
  const s = row.signal;
  const long = s.direction === 'long';
  const reasons = (s.reasons || []).map((r) =>
    `<span class="chip">${esc(r.detail)}<b>+${r.weight.toFixed(0)}</b></span>`).join('');
  const warns = (s.warnings || []).length
    ? `<div class="warns"><div class="warns__h">Check before you take it</div><ul>${
        s.warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul></div>`
    : '';

  return `
    <article class="card card--${long ? 'long' : 'short'}">
      <div class="card__head">
        <span class="side side--${long ? 'long' : 'short'}">${long ? 'Buy' : 'Sell'}</span>
        <span class="card__sym">${esc(s.symbol)}</span>
        <span class="grade" data-g="${esc(s.grade)}">${esc(s.grade)}</span>
        <span class="rr">${s.risk_reward.toFixed(1)}R</span>
        <span class="card__meta">${esc(s.timeframe)} &middot; ${esc(s.session)}<br>
          ${esc(s.issued_local || (s.issued_at || '').slice(0, 16).replace('T', ' ') + ' UTC')}</span>
      </div>
      ${s.description ? `<p class="card__desc">${esc(s.description)}</p>` : ''}
      ${candleChart(row.candles, s)}
      ${ladder(s)}
      <div class="kv">
        <div class="kv__item"><div class="kv__k">Size</div>
          <div class="kv__v">${s.position_lots.toFixed(2)} lots</div></div>
        <div class="kv__item"><div class="kv__k">Risking</div>
          <div class="kv__v kv__v--neg">${esc(fmtNum(s.risk_amount))}</div></div>
        <div class="kv__item"><div class="kv__k">To make</div>
          <div class="kv__v kv__v--pos">${esc(fmtNum(s.reward_amount))}</div></div>
        <div class="kv__item"><div class="kv__k">Confidence</div>
          <div class="kv__v">${esc(fmtPct(s.confidence, 0))}</div></div>
      </div>
      ${predictionBlock(s)}
      <div class="why"><div class="why__h">Why — ${s.score.toFixed(0)} of
        ${s.max_score.toFixed(0)} points</div>
        <div class="chips">${reasons}</div></div>
      ${warns}
      ${playbookBlock(s)}
      <div class="card__foot">You place this trade. This tool does not and will not.</div>
    </article>`;
}

/* "No setup" is the answer most of the time, so it is worth more than one line:
   which conditions are already met, which are not, and what would have to change.
   That is the part a reader can act on tomorrow. */
function noSetupCard(row) {
  const confl = row.confluence === null || row.confluence === undefined
    ? 'not scored' : `best confluence ${fmtPct(row.confluence, 0)}`;
  const g = row.guidance || {};
  const met = (g.met || []).map((m) =>
    `<span class="chip chip--met">${esc(m.detail)}<b>+${m.weight.toFixed(0)}</b></span>`).join('');
  const missing = (g.missing || []).slice(0, 5).map((m) =>
    `<li><strong>${esc(m.title)}</strong> (+${m.weight.toFixed(0)}) — ${esc(m.detail)}</li>`).join('');

  const detail = (g.met || []).length || missing
    ? `<div class="why">
         <div class="why__h">${esc(g.summary || '')}</div>
         ${met ? `<div class="chips">${met}</div>` : ''}
         ${missing ? `<div class="need"><div class="need__h">Still needed</div>
            <ul>${missing}</ul></div>` : ''}
       </div>`
    : '';

  return `
    <article class="card${g.watchlist ? ' card--watch' : ''}">
      <div class="card__head">
        <span class="card__sym">${esc(row.symbol)}</span>
        <span class="chip">${g.watchlist ? 'close — watch it' : 'no setup'}</span>
        <span class="card__meta">${esc(row.timeframe)} &middot; ${esc(row.session || '')}<br>
          last ${esc(fmtPrice(row.last_price, row.digits))}</span>
      </div>
      ${candleChart(row.candles, null)}
      ${detail}
      <div class="card__foot">Nothing met the rules here — ${esc(confl)}.
        No setup is a position too.</div>
    </article>`;
}

/* Netted currency exposure across every signal on the table. Four euro longs are
   one euro bet at four times the size, and this is where that becomes visible. */
function exposureBlock(exp) {
  if (!exp || !exp.exposures || exp.exposures.length === 0) return '';
  const rows = exp.exposures
    .filter((e) => e.legs.length > 1 || Math.abs(e.net_risk_pct) > 0.0001)
    .map((e) => `<tr>
        <td class="mono">${esc(e.code)}</td>
        <td class="${signClass(e.net_risk_pct)}">${esc(e.direction)}</td>
        <td class="mono">${Math.abs(e.net_risk_pct).toFixed(2)}%</td>
        <td class="dim">${esc(e.legs.join(', '))}</td>
      </tr>`).join('');
  const warns = (exp.warnings || []).map((w) => `<li>${esc(w)}</li>`).join('');
  const picks = (exp.suggested || []).map((r, i) => `<li>${i + 1}. <strong>${esc(r.direction)}
      ${esc(r.symbol)}</strong> — ${r.measured ? `${fmtR(r.expected_r)} expected` : 'unmeasured'}
      <span class="dim">${esc(r.basis)}</span></li>`).join('');

  return `<article class="card">
      <div class="card__head"><span class="card__sym">If you took all ${exp.signals}</span>
        <span class="chip">${exp.total_risk_pct.toFixed(1)}% at risk</span>
        <span class="card__meta">limit ${exp.max_concurrent_pct.toFixed(1)}%</span>
      </div>
      <div class="tablewrap"><table>
        <thead><tr><th>Ccy</th><th>Net</th><th>Risk</th><th>From</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
      ${warns ? `<div class="warns"><div class="warns__h">Read this before taking more
        than one</div><ul>${warns}</ul></div>` : ''}
      ${picks ? `<div class="why"><div class="why__h">If you only take
        ${exp.suggested.length}, take these</div><ul class="picks">${picks}</ul></div>` : ''}
      <div class="card__foot">Arithmetic on your own config, not a decision.
        Nothing here cancels a signal.</div>
    </article>`;
}

/* ------------------------------------------------------------- pairs view */

/* Win rate by pair. Two interval columns on purpose: the raw one is what the
   pair looked like, the corrected one is what it looks like once you account for
   having inspected every pair before picking this one. */
async function runPairs() {
  const params = {
    symbols: $('#pairs-symbols').value.trim(),
    timeframe: $('#pairs-timeframe').value,
    source: $('#pairs-source').value,
    bars: $('#pairs-bars').value,
    split: $('#pairs-oos').checked ? '0.7' : '0',
    persistence: $('#pairs-persistence').checked ? '1' : '0',
  };
  const data = await withBusy($('#pairs-run'), 'Measuring', () => api('/api/pairs', { params }));
  const c = data.counts;

  const rows = data.pairs.map((p) => {
    if (!p.trades) {
      return `<tr class="dim"><td class="mono">${esc(p.symbol)}</td><td>${esc(p.group)}</td>
        <td class="mono">0</td><td colspan="5">${esc(p.note || '—')}</td>
        <td><span class="verdict__tag" data-v="${esc(p.verdict)}">${esc(p.verdict)}</span></td></tr>`;
    }
    return `<tr>
      <td class="mono">${esc(p.symbol)}</td>
      <td>${esc(p.group)}</td>
      <td class="mono">${p.trades}</td>
      <td class="mono">${fmtPct(p.win_rate, 0)}</td>
      <td class="mono dim">${fmtPct(p.baseline, 0)}</td>
      <td class="mono dim">${fmtPct(p.interval_low, 0)}–${fmtPct(p.interval_high, 0)}</td>
      <td class="mono">${fmtPct(p.family_low, 0)}–${fmtPct(p.family_high, 0)}</td>
      <td class="mono ${signClass(p.expectancy_r)}">${fmtR(p.expectancy_r)}</td>
      <td><span class="verdict__tag" data-v="${esc(p.verdict)}">${esc(p.verdict)}</span></td>
    </tr>`;
  }).join('');

  const ccy = data.currencies.map((r) => `<tr>
      <td class="mono">${esc(r.code)}</td><td class="mono">${r.trades}</td>
      <td class="mono">${fmtPct(r.win_rate, 0)}</td>
      <td class="mono ${signClass(r.expectancy_r)}">${fmtR(r.expectancy_r)}</td>
      <td class="dim">${esc(r.pairs.slice(0, 5).join(', '))}</td></tr>`).join('');

  const pooled = data.pooled;
  // expectancy_low is null when the sample is a single trade: unbounded, not zero.
  const tradable = data.pairs.filter(
    (p) => p.survives_correction && p.expectancy_low !== null && p.expectancy_low > 0);

  // The walk-forward answer goes first when it was asked for: it decides whether
  // the table below is worth acting on at all.
  const pc = data.persistence;
  const persistence = pc ? `
    <article class="card">
      <div class="card__head"><span class="card__sym">Does picking pairs help?</span>
        <span class="verdict__tag" data-v="${esc(pc.verdict)}">${esc(pc.verdict)}</span>
        <span class="card__meta">chosen on the first half of history,<br>
          measured on the second</span>
      </div>
      <div class="kv">
        <div class="kv__item"><div class="kv__k">Trade everything</div>
          <div class="kv__v ${signClass(pc.everything.expectancy_r)}">${
            fmtR(pc.everything.expectancy_r)}</div></div>
        <div class="kv__item"><div class="kv__k">Trade the chosen</div>
          <div class="kv__v ${signClass(pc.chosen.expectancy_r)}">${
            fmtR(pc.chosen.expectancy_r)}</div></div>
        <div class="kv__item"><div class="kv__k">Selection bought</div>
          <div class="kv__v ${signClass(pc.gain_r)}">${fmtR(pc.gain_r)}</div></div>
        <div class="kv__item"><div class="kv__k">Sign carried over</div>
          <div class="kv__v">${fmtPct(pc.sign_agreement, 0)}</div></div>
      </div>
      <div class="card__foot">${pc.selected.length} of
        ${pc.selected.length + pc.rejected.length} pairs were chosen on the first half.
        Chance alone carries the sign of expectancy 50% of the time. If selection bought
        nothing, filtering your universe costs trades and buys nothing — trade the lot and
        spend the effort on correlation instead.</div>
    </article>` : '';

  $('#pairs-results').innerHTML = persistence + `
    <article class="card">
      <div class="card__head"><span class="card__sym">Win rate by pair</span>
        <span class="chip">${data.out_of_sample ? 'out-of-sample' : 'in-sample'}</span>
        <span class="card__meta">${c.with_data} of ${c.asked} had data ·
          ${c.with_trades} produced setups · ${c.tradable} survive the correction</span>
      </div>
      <div class="tablewrap"><table>
        <thead><tr><th>Pair</th><th>Group</th><th>Trades</th><th>Win</th><th>Chance</th>
          <th>${fmtPct(data.confidence, 0)} interval</th><th>Corrected</th><th>Exp R</th>
          <th>Verdict</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
      <div class="warns"><div class="warns__h">Why two intervals</div><ul>
        <li><strong>Chance</strong> is the win rate a coin flip gives at the ratio those
          trades actually reached — not the ratio that was planned.</li>
        <li><strong>Corrected</strong> widens each interval to
          ${(data.family_confidence * 100).toFixed(2)}% so that all ${c.asked} of them hold
          together at ${fmtPct(data.confidence, 0)}. That is the price of having looked at
          ${c.asked} pairs before choosing one.</li>
        <li>A pair reads TRADE IT only if its corrected low beats chance <em>and</em> its
          expectancy stays positive at the low bound.</li>
      </ul></div>
    </article>

    <article class="card">
      <div class="card__head"><span class="card__sym">Pooled across every pair</span>
        <span class="chip">${pooled.trades} trades</span>
        <span class="verdict__tag" data-v="${esc(pooled.edge_verdict)}">${esc(pooled.edge_verdict)}</span>
      </div>
      <div class="kv">
        <div class="kv__item"><div class="kv__k">Win rate</div>
          <div class="kv__v">${fmtPct(pooled.win_rate)}</div></div>
        <div class="kv__item"><div class="kv__k">${fmtPct(data.confidence, 0)} interval</div>
          <div class="kv__v">${fmtPct(pooled.interval_low, 0)}–${fmtPct(pooled.interval_high, 0)}</div></div>
        <div class="kv__item"><div class="kv__k">Expectancy</div>
          <div class="kv__v ${signClass(pooled.expectancy_r)}">${fmtR(pooled.expectancy_r)}</div></div>
        <div class="kv__item"><div class="kv__k">Total</div>
          <div class="kv__v ${signClass(pooled.total_r)}">${fmtR(pooled.total_r, 1)}</div></div>
      </div>
      <div class="verdict"><span class="verdict__txt">${esc(pooled.edge_detail)}</span></div>
      <div class="card__foot">Pooling is fair here — parameters are not fitted per pair —
        but pairs sharing a currency move together, so this interval is narrower than
        the truth.</div>
    </article>

    <article class="card">
      <div class="card__head"><span class="card__sym">By currency leg</span>
        <span class="card__meta">each trade counted under both its currencies</span></div>
      <div class="tablewrap"><table>
        <thead><tr><th>Ccy</th><th>Trades</th><th>Win</th><th>Exp R</th><th>Pairs</th></tr></thead>
        <tbody>${ccy}</tbody></table></div>
      <div class="card__foot">${tradable.length
        ? `Put these in data.symbols and scan them: <strong>${
            tradable.map((p) => esc(p.symbol)).join(', ')}</strong>. Re-measure monthly —
            an edge that survives today can still decay.`
        : 'No pair survives the correction on this sample. That is usually a sample too '
          + 'short, not a universe with no edge in it.'}</div>
    </article>` + gapNotice(data.data_gaps, 'measured');
}

/* ---------------------------------------------------------- forecast view */

async function loadForecast() {
  const data = await api('/api/forecast');
  const board = data.scoreboard;
  const live = data.live.map((p) => `<tr class="${p.overdue ? 'dim' : ''}">
      <td class="mono">${esc(p.symbol)}</td>
      <td class="${p.direction === 'long' ? 'pos' : 'neg'}">${esc(p.direction)}</td>
      <td class="mono">${esc(String(p.entry))}</td>
      <td class="mono">${esc(String(p.take_profit))}</td>
      <td class="mono">${esc(String(p.stop_loss))}</td>
      <td>${esc(p.made_at_local)}</td>
      <td>${esc(p.resolve_by_local)}${p.overdue ? ' <span class="chip">overdue</span>' : ''}</td>
    </tr>`).join('');

  $('#fc-results').innerHTML = `
    <article class="card">
      <div class="card__head"><span class="card__sym">Forward record</span>
        <span class="chip">${board.resolved} resolved / ${board.made} made</span></div>
      <div class="verdict"><span class="verdict__txt">${
        board.lines.map((l) => esc(l)).join('<br>')}</span></div>
      <div class="card__foot">${esc(data.note)}</div>
    </article>
    ${data.live.length ? `<article class="card">
      <div class="card__head"><span class="card__sym">Live predictions</span>
        <span class="chip">${data.live.length} awaiting an answer</span></div>
      <div class="tablewrap"><table>
        <thead><tr><th>Pair</th><th>Side</th><th>Entry</th><th>Target</th><th>Stop</th>
          <th>Made</th><th>Resolves by</th></tr></thead>
        <tbody>${live}</tbody></table></div>
      <div class="card__foot">Settle these against real candles with the button above.
        Each is scored by the same rule the backtest uses.</div>
    </article>` : ''}`;
}

async function resolveForecasts() {
  const data = await withBusy($('#fc-resolve'), 'Settling',
    () => api('/api/forecast/resolve', { method: 'POST', body: {} }));
  toast(`${data.resolved} of ${data.checked} settled`);
  await loadForecast();
}

/* -------------------------------------------------------------- scan view */

/* The one message in this app that says stop. It is rendered above the results
   and never in place of them: the advice still shows, the human still decides. */
function limitBanner(limits) {
  if (!limits || !limits.breached) return '';
  const rows = limits.breaches
    .map((b) => `<strong>${esc(b.name)}</strong> ${b.actual_pct.toFixed(2)}% of ${b.limit_pct.toFixed(2)}% limit`)
    .join(' &middot; ');
  return `<div class="limit" role="alert">
      <span aria-hidden="true">&#9888;</span>
      <span class="limit__body">Risk limit reached &mdash; ${rows}.<br>
        Advice below continues. The rules you set say stop for now.</span>
    </div>`;
}

/* Sixty pairs with no file is one setup problem, not sixty results. Rendering
   it as sixty red rows buries the pairs that were actually scanned and reads
   like a broken program, so it collapses to one notice that says what to run.
   The pairs are still named inside it: a pair that vanished from the page would
   be indistinguishable from one that was looked at and found nothing. */
function gapNotice(gaps, verb = 'scanned') {
  if (!gaps || !gaps.count) return '';
  const where = gaps.directory ? ` in <code>${esc(gaps.directory)}</code>` : '';
  const commands = (gaps.commands || []).map((c) => `
    <li><code class="gap__cmd">${esc(c.command)}</code>
        <span class="gap__note">${esc(c.note)}</span></li>`).join('');
  return `<section class="gap">
      <div class="gap__h">${gaps.count} pair${gaps.count === 1 ? '' : 's'} have no
        ${esc(gaps.timeframe)} data${where} and were not ${verb}</div>
      <p class="gap__sub">Unmeasured is not the same as no setup. Fill them in one
        command:</p>
      <ul class="gap__cmds">${commands}</ul>
      <details class="gap__list">
        <summary>Which pairs</summary>
        <p class="gap__syms">${gaps.symbols.map(esc).join(', ')}</p>
      </details>
    </section>`;
}

async function runScan() {
  const params = {
    symbols: $('#scan-symbols').value.trim(),
    timeframe: $('#scan-timeframe').value,
    source: $('#scan-source').value,
    journal: $('#scan-journal').checked ? '1' : '0',
  };
  const data = await withBusy($('#scan-run'), 'Scanning', () => api('/api/scan', { params }));

  const summary = $('#scan-summary');
  summary.hidden = false;
  const windows = (data.sessions || [])
    .map((w) => `${esc(w.name)} ${esc(w.local)}`).join(' &middot; ');
  const gaps = data.data_gaps;
  summary.innerHTML =
    `<span><strong>${data.found}</strong> setup${data.found === 1 ? '' : 's'} found
       of ${data.scanned} scanned</span>
     ${gaps ? `<span class="dim">${gaps.count} of ${data.requested} had no data</span>` : ''}
     <span>min R:R <strong>${data.min_risk_reward.toFixed(0)}:1</strong></span>
     <span>min confluence <strong>${fmtPct(data.min_confluence, 0)}</strong></span>
     <span>${esc(data.scanned_at_local || data.scanned_at)}</span>
     ${windows ? `<span class="dim">${windows} ${esc(data.timezone_abbrev || '')}</span>` : ''}`;

  const out = $('#scan-results');
  const banner = limitBanner(data.limits);
  if (!data.results.length) {
    out.innerHTML = `<div class="empty"><div class="empty__big">Nothing scanned</div>
      <div class="empty__sub">Add a symbol above.</div></div>`;
    return;
  }
  // Signals first, then near misses, then files that exist but could not be
  // read. A page that opens on sixty "no setup" cards has buried the one thing
  // worth reading. Pairs with no file at all are not rows here at all: they go
  // into the single notice below, which is the only place with a fix to offer.
  const order = { signal: 0, no_setup: 1, error: 2 };
  const rows = data.results
    .filter((row) => row.status !== 'no_data')
    .sort((a, b) => (order[a.status] ?? 3) - (order[b.status] ?? 3)
      || (b.confluence || 0) - (a.confluence || 0));

  out.innerHTML = banner + exposureBlock(data.exposure) + rows.map((row) => {
    if (row.status === 'error') {
      return `<div class="err"><strong>${esc(row.symbol)}</strong> — ${esc(row.message)}</div>`;
    }
    return row.status === 'signal' ? signalCard(row) : noSetupCard(row);
  }).join('') + gapNotice(gaps);

  if (data.found > 0) toast(`${data.found} setup${data.found === 1 ? '' : 's'} found`);
}

/* ---------------------------------------------------------- backtest view */

function metricsBlock(payload) {
  const m = payload.metrics, g = payload.gate, e = payload.edge;
  return `
    <article class="card">
      <div class="card__head">
        <span class="card__sym">${esc(payload.symbol)}</span>
        <span class="chip">${esc(payload.label)}</span>
        <span class="card__meta">${esc((payload.first_bar || '').slice(0, 10))} to
          ${esc((payload.last_bar || '').slice(0, 10))}<br>${payload.bars_tested} bars</span>
      </div>
      <div class="kv">
        <div class="kv__item"><div class="kv__k">Trades</div>
          <div class="kv__v">${m.trades}</div></div>
        <div class="kv__item"><div class="kv__k">Win rate</div>
          <div class="kv__v">${esc(fmtPct(m.win_rate))}</div></div>
        <div class="kv__item"><div class="kv__k">95% interval</div>
          <div class="kv__v" style="font-size:13px">${esc(fmtPct(m.interval_low, 0))}–${esc(fmtPct(m.interval_high, 0))}</div></div>
        <div class="kv__item"><div class="kv__k">Expectancy</div>
          <div class="kv__v ${signClass(m.expectancy_r)}">${esc(fmtR(m.expectancy_r))}</div></div>
        <div class="kv__item"><div class="kv__k">Total</div>
          <div class="kv__v ${signClass(m.total_r)}">${esc(fmtR(m.total_r, 1))}</div></div>
        <div class="kv__item"><div class="kv__k">Max drawdown</div>
          <div class="kv__v">${m.max_drawdown_r.toFixed(1)}R</div></div>
        <div class="kv__item"><div class="kv__k">Profit factor</div>
          <div class="kv__v">${m.profit_factor === null ? 'n/a' : m.profit_factor.toFixed(2)}</div></div>
        <div class="kv__item"><div class="kv__k">Worst streak</div>
          <div class="kv__v">${m.max_loss_streak}L</div></div>
      </div>
      ${equityChart(payload.equity_curve)}
      <div class="verdict">
        <span class="verdict__tag" data-v="${esc(g.verdict)}">${esc(g.verdict)}</span>
        <span class="verdict__txt">${esc(g.detail)}</span>
      </div>
      ${e ? `<div class="verdict">
        <span class="verdict__tag" data-v="${esc(e.verdict)}">${esc(e.verdict)}</span>
        <span class="verdict__txt">${esc(e.detail)}</span>
      </div>` : ''}
    </article>`;
}

function comparisonBlock(inSample, outSample) {
  const a = inSample.metrics, b = outSample.metrics;
  if (!a.trades || !b.trades) return '';
  const gap = a.win_rate - b.win_rate;
  const note = gap > 0.15
    ? 'Materially worse on data it was not tuned on. Quote the out-of-sample number.'
    : gap < -0.15
      ? 'Out-of-sample beat in-sample — usually different conditions, not a bigger edge.'
      : 'The two halves agree, which is what a robust setting looks like.';
  return `
    <article class="card">
      <div class="card__head"><span class="card__sym">In-sample vs out-of-sample</span></div>
      <div class="kv">
        <div class="kv__item"><div class="kv__k">In-sample</div>
          <div class="kv__v">${esc(fmtPct(a.win_rate))} <span class="ladder__sub">${a.trades}t</span></div></div>
        <div class="kv__item"><div class="kv__k">Out-of-sample</div>
          <div class="kv__v">${esc(fmtPct(b.win_rate))} <span class="ladder__sub">${b.trades}t</span></div></div>
        <div class="kv__item"><div class="kv__k">Gap</div>
          <div class="kv__v ${gap > 0.15 ? 'neg' : ''}">${gap >= 0 ? '+' : ''}${(gap * 100).toFixed(1)}%</div></div>
      </div>
      <div class="card__foot">${esc(note)}</div>
    </article>`;
}

async function runBacktest() {
  const params = {
    symbol: $('#bt-symbol').value.trim(),
    timeframe: $('#bt-timeframe').value,
    source: $('#bt-source').value,
    bars: $('#bt-bars').value,
    split: $('#bt-split').value,
  };
  const data = await withBusy($('#bt-run'), 'Running', () => api('/api/backtest', { params }));
  const out = $('#bt-results');
  out.innerHTML = data.split
    ? metricsBlock(data.out_of_sample) + metricsBlock(data.in_sample)
      + comparisonBlock(data.in_sample, data.out_of_sample)
    : metricsBlock(data.result);
}

/* --------------------------------------------------------- calibrate view */

async function runCalibrate() {
  const params = {
    symbol: $('#cal-symbol').value.trim(),
    source: $('#cal-source').value,
    bars: $('#cal-bars').value,
    split: $('#cal-oos').checked ? '0.7' : '0',
  };
  const data = await withBusy($('#cal-run'), 'Sweeping', () => api('/api/calibrate', { params }));

  const rows = data.rows.map((r) => `
    <tr data-hl="${data.recommended !== null && Math.abs(r.threshold - data.recommended) < 1e-9 ? 1 : 0}">
      <td>${(r.threshold * 100).toFixed(0)}%</td>
      <td>${r.trades}</td>
      <td>${fmtPct(r.win_rate)}</td>
      <td>${fmtPct(r.interval_low, 0)}–${fmtPct(r.interval_high, 0)}</td>
      <td class="${signClass(r.expectancy_r)}">${fmtR(r.expectancy_r)}</td>
      <td class="${signClass(r.total_r)}">${fmtR(r.total_r, 1)}</td>
      <td style="text-align:left">${esc(r.verdict)}</td>
    </tr>`).join('');

  $('#cal-results').innerHTML = `
    <article class="card">
      <div class="card__head">
        <span class="card__sym">${esc(data.symbol)}</span>
        <span class="chip">${data.out_of_sample_only ? 'out-of-sample' : 'full series'}</span>
        <span class="card__meta">${data.bars} bars</span>
      </div>
      <div class="tablewrap"><table>
        <thead><tr><th>Min confluence</th><th>Trades</th><th>Win rate</th><th>95% interval</th>
          <th>Expectancy</th><th>Total</th><th style="text-align:left">Verdict</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
      <div class="verdict">
        <span class="verdict__tag" data-v="${data.recommended === null ? 'INSUFFICIENT DATA' : 'UNPROVEN'}">
          ${data.recommended === null ? 'No recommendation' : `Set ${(data.recommended * 100).toFixed(0)}%`}</span>
        <span class="verdict__txt">${esc(data.rationale)}</span>
      </div>
      <div class="card__foot">Picking the best row is itself a fit to this data.
        Confirm any choice on a fresh period.</div>
    </article>`;
}

/* -------------------------------------------------------------- risk view */

async function runRisk() {
  const mode = $('#risk-mode').value;
  const params = { trades: $('#risk-trades').value };
  if (mode === 'backtest') {
    params.from_backtest = '1';
    params.symbol = $('#risk-symbol').value.trim();
  } else {
    params.win_rate = (Number($('#risk-rate').value) / 100).toFixed(4);
  }
  const data = await withBusy($('#risk-run'), 'Simulating', () => api('/api/risk', { params }));

  if (!data.profitable) {
    $('#risk-results').innerHTML = `<div class="err">${esc(data.caution)}</div>`;
    return;
  }

  const rows = data.rows.map((r) => `
    <tr data-hl="${r.label === 'recommended' ? 1 : 0}">
      <td>${fmtPct(r.risk_fraction)}</td>
      <td>${r.median_multiple.toFixed(2)}x</td>
      <td>${r.p05_multiple.toFixed(2)}x</td>
      <td>${r.p95_multiple.toFixed(2)}x</td>
      <td>${fmtPct(r.median_drawdown, 0)}</td>
      <td>${fmtPct(r.p95_drawdown, 0)}</td>
      <td class="${r.prob_lose_half > 0.02 ? 'neg' : ''}">${fmtPct(r.prob_lose_half)}</td>
      <td style="text-align:left">${esc(r.label)}</td>
    </tr>`).join('');

  $('#risk-results').innerHTML = `
    <article class="card">
      <div class="card__head">
        <span class="card__sym">Position sizing</span>
        <span class="chip">${data.reward.toFixed(0)}:1</span>
        <span class="card__meta">${data.trades_per_period} trades per period</span>
      </div>
      <div class="kv">
        <div class="kv__item"><div class="kv__k">Win rate used</div>
          <div class="kv__v">${fmtPct(data.win_rate)}</div></div>
        <div class="kv__item"><div class="kv__k">Breakeven</div>
          <div class="kv__v">${fmtPct(data.breakeven)}</div></div>
        <div class="kv__item"><div class="kv__k">Expectancy</div>
          <div class="kv__v pos">${fmtR(data.expectancy)}</div></div>
        <div class="kv__item"><div class="kv__k">Full Kelly</div>
          <div class="kv__v">${fmtPct(data.kelly)}</div></div>
        <div class="kv__item"><div class="kv__k">Recommended risk</div>
          <div class="kv__v pos">${fmtPct(data.recommended_risk, 2)}</div></div>
        <div class="kv__item"><div class="kv__k">Per trade</div>
          <div class="kv__v">${fmtNum(data.recommended_amount, 0)} ${esc(data.currency)}</div></div>
      </div>
      <div class="tablewrap"><table>
        <thead><tr><th>Risk</th><th>Median</th><th>5th pct</th><th>95th pct</th>
          <th>Med DD</th><th>95th DD</th><th>P(-50%)</th>
          <th style="text-align:left"></th></tr></thead>
        <tbody>${rows}</tbody></table></div>
      <div class="verdict">
        <span class="verdict__tag" data-v="UNPROVEN">Sizing</span>
        <span class="verdict__txt">${esc(data.caution)}</span>
      </div>
      <div class="card__foot">Past the Kelly fraction the median return falls while drawdown
        keeps rising — betting harder than optimal is worse on both counts.</div>
    </article>`;
}

/* ----------------------------------------------------------- journal view */

async function loadJournal() {
  const data = await withBusy($('#journal-refresh'), 'Loading', () => api('/api/journal'));
  const out = $('#journal-results');

  if (!data.count) {
    out.innerHTML = `<div class="empty"><div class="empty__big">Nothing journalled yet</div>
      <div class="empty__sub">Tick “Journal signals” on the Scan tab to start recording
      what you were advised.</div></div>`;
    return;
  }

  const live = data.live;
  const liveBlock = live.trades ? `
    <article class="card">
      <div class="card__head"><span class="card__sym">Live performance</span>
        <span class="chip">closed trades only</span></div>
      <div class="kv">
        <div class="kv__item"><div class="kv__k">Trades</div>
          <div class="kv__v">${live.trades}</div></div>
        <div class="kv__item"><div class="kv__k">Win rate</div>
          <div class="kv__v">${fmtPct(live.win_rate)}</div></div>
        <div class="kv__item"><div class="kv__k">95% interval</div>
          <div class="kv__v" style="font-size:13px">${fmtPct(live.interval_low, 0)}–${fmtPct(live.interval_high, 0)}</div></div>
        <div class="kv__item"><div class="kv__k">Expectancy</div>
          <div class="kv__v ${signClass(live.expectancy_r)}">${fmtR(live.expectancy_r)}</div></div>
        <div class="kv__item"><div class="kv__k">Total</div>
          <div class="kv__v ${signClass(live.total_r)}">${fmtR(live.total_r, 1)}</div></div>
      </div>
      ${live.trades < 30 ? `<div class="card__foot">${live.trades} closed trade${
        live.trades === 1 ? '' : 's'} is too few to judge. The interval is the honest
        width of what you know.</div>` : ''}
    </article>` : '';

  const rows = data.entries.map((e) => {
    const d = e.digits ?? 5;
    const action = e.is_open
      ? `<input type="number" step="0.00001" placeholder="exit" data-exit="${esc(e.id)}">
         <button class="btn btn--sm" data-close="${esc(e.id)}">Close</button>`
      : `<span class="outcome" data-o="${esc(e.outcome)}">${esc(e.outcome)}</span>
         <span class="ladder__val ${signClass(e.r_multiple)}">${fmtR(e.r_multiple)}</span>`;
    return `
      <div class="jrow">
        <div class="jrow__main">
          <div class="jrow__top">
            <span class="side side--${e.direction === 'long' ? 'long' : 'short'}">${
              e.direction === 'long' ? 'Buy' : 'Sell'}</span>
            <strong>${esc(e.symbol)}</strong>
            <span class="grade" data-g="${esc(e.grade)}">${esc(e.grade)}</span>
            ${e.is_open ? '<span class="outcome" data-o="open">open</span>' : ''}
          </div>
          <div class="jrow__sub">${esc((e.issued_at || '').slice(0, 16).replace('T', ' '))}
            &nbsp; in ${esc(fmtPrice(e.entry, d))}
            &nbsp; sl ${esc(fmtPrice(e.stop_loss, d))}
            &nbsp; tp ${esc(fmtPrice(e.take_profit, d))}
            ${e.exit_price ? `&nbsp; out ${esc(fmtPrice(e.exit_price, d))}` : ''}</div>
        </div>
        <div class="jrow__act">${action}</div>
      </div>`;
  }).join('');

  out.innerHTML = liveBlock + `
    <article class="card">
      <div class="card__head"><span class="card__sym">Signals</span>
        <span class="chip">${data.open} open</span>
        <span class="chip">${data.closed} closed</span></div>
      ${rows}
    </article>`;
}

/** Close a journalled trade from the row's own exit input. */
async function closeTrade(id) {
  const input = $(`[data-exit="${CSS.escape(id)}"]`);
  const value = Number(input?.value);
  if (!input || !input.value || !isFinite(value) || value <= 0) {
    toast('Enter the price you actually exited at', 'error');
    input?.focus();
    return;
  }
  const result = await withBusy(null, 'Closing',
    () => api('/api/journal/close', { method: 'POST', body: { id, exit_price: value } }));
  toast(`Closed ${result.outcome} at ${fmtR(result.r_multiple)}`);
  await loadJournal();
}

/* ------------------------------------------------------------ ledger view */

/* Every prediction the model made, what it saw, and what happened next. The
   forward record and a replay share this renderer; the origin label on the
   first card is what tells them apart, and it is never omitted. */

const VERDICT_CLASS = { RIGHT: 'right', WRONG: 'wrong', EXPIRED: 'expired', OPEN: 'open', FLAT: 'expired' };
const VERDICT_OUTCOME = { RIGHT: 'win', WRONG: 'loss', EXPIRED: 'expired', OPEN: 'open', FLAT: 'flat' };

function bucketRows(buckets) {
  return buckets.map((b) => `<tr>
      <td>${esc(b.label)}</td>
      <td class="mono">${b.trades}</td>
      <td class="mono">${b.wins}</td>
      <td class="mono">${b.losses}</td>
      <td class="mono">${fmtPct(b.win_rate, 0)}</td>
      <td class="mono dim">${fmtPct(b.interval_low, 0)}–${fmtPct(b.interval_high, 0)}</td>
      <td class="mono ${signClass(b.expectancy_r)}">${fmtR(b.expectancy_r)}</td>
      <td class="mono ${signClass(b.total_r)}">${fmtR(b.total_r, 1)}</td>
      <td class="mono ${signClass(b.cash)}">${fmtNum(b.cash, 0)}</td>
    </tr>`).join('');
}

function bucketTable(title, buckets, note) {
  if (!buckets || !buckets.length) return '';
  return `<div class="tablewrap"><table>
      <thead><tr><th>${esc(title)}</th><th>n</th><th>Right</th><th>Wrong</th><th>Win</th>
        <th>95% interval</th><th>Exp</th><th>Total</th><th>Cash</th></tr></thead>
      <tbody>${bucketRows(buckets)}</tbody></table></div>
    ${note ? `<div class="card__foot">${note}</div>` : ''}`;
}

function ledgerSummaryCard(data) {
  const s = data.summary;
  const replay = data.origin === 'replay';
  const title = replay
    ? `Replay — ${esc(data.symbol || '')} ${esc(data.timeframe || '')}`
    : 'Forward record';
  const meta = replay
    ? `${data.bars} bars · ${esc((data.first_bar || '').slice(0, 10))} to
       ${esc((data.last_bar || '').slice(0, 10))}${data.split ? ' · out-of-sample tail' : ''}`
    : `${esc(data.path || '')}`;
  // The edge verdict has its own row above; the same sentence in the list
  // would be the one thing on the card said twice.
  const lines = (s.lines || []).filter((l) => !l.startsWith('Edge over chance'))
    .map((l) => `<li>${esc(l)}</li>`).join('');
  const kv = s.resolved ? `
    <div class="kv">
      <div class="kv__item"><div class="kv__k">Predictions</div>
        <div class="kv__v">${s.made}</div></div>
      <div class="kv__item"><div class="kv__k">Right / wrong</div>
        <div class="kv__v"><span class="pos">${s.wins}</span> / <span class="neg">${s.losses}</span>
          ${s.expired ? `<span class="ladder__sub">${s.expired} expired</span>` : ''}</div></div>
      <div class="kv__item"><div class="kv__k">Win rate</div>
        <div class="kv__v">${fmtPct(s.win_rate)} <span class="ladder__sub">${
          fmtPct(s.interval_low, 0)}–${fmtPct(s.interval_high, 0)}</span></div></div>
      <div class="kv__item"><div class="kv__k">Expectancy</div>
        <div class="kv__v ${signClass(s.expectancy_r)}">${fmtR(s.expectancy_r)}</div></div>
      <div class="kv__item"><div class="kv__k">Total</div>
        <div class="kv__v ${signClass(s.total_r)}">${fmtR(s.total_r, 1)}</div></div>
      <div class="kv__item"><div class="kv__k">Cash at card sizes</div>
        <div class="kv__v ${signClass(s.cash_total)}">${fmtNum(s.cash_total, 0)} ${esc(s.currency)}</div></div>
      <div class="kv__item"><div class="kv__k">Avg win / loss</div>
        <div class="kv__v">${fmtR(s.average_win_r)} / ${fmtR(s.average_loss_r)}</div></div>
      <div class="kv__item"><div class="kv__k">Deepest drawdown</div>
        <div class="kv__v">${(s.max_drawdown_r || 0).toFixed(1)}R
          <span class="ladder__sub">${s.max_loss_streak}L run</span></div></div>
    </div>` : '';
  return `
    <article class="card">
      <div class="card__head"><span class="card__sym">${title}</span>
        <span class="chip">${replay ? 'replay — not a track record' : 'written down before the outcome'}</span>
        <span class="chip">${s.resolved} resolved / ${s.made} made</span>
        <span class="card__meta">${meta}</span></div>
      ${kv}
      ${s.resolved ? `<div class="verdict">
        <span class="verdict__tag" data-v="${esc(s.edge.verdict)}">${esc(s.edge.verdict)}</span>
        <span class="verdict__txt">${esc(s.edge.detail)}</span></div>` : ''}
      <ul class="ledger__lines">${lines}</ul>
      <div class="card__foot">${esc(data.note)}</div>
    </article>`;
}

function liveCard(item) {
  const long = item.direction === 'long';
  const kv = item.current_price !== null && item.current_price !== undefined ? `
    <div class="kv">
      <div class="kv__item"><div class="kv__k">Price now</div>
        <div class="kv__v">${esc(String(item.current_price))}</div></div>
      <div class="kv__item"><div class="kv__k">From the fill</div>
        <div class="kv__v ${signClass(item.unrealised_r)}">${fmtR(item.unrealised_r)}</div></div>
      <div class="kv__item"><div class="kv__k">To target</div>
        <div class="kv__v">${item.to_target_pips === null ? '-' : `${item.to_target_pips.toFixed(1)}p`}</div></div>
      <div class="kv__item"><div class="kv__k">To stop</div>
        <div class="kv__v">${item.to_stop_pips === null ? '-' : `${item.to_stop_pips.toFixed(1)}p`}</div></div>
    </div>` : '';
  const advice = (item.advice || []).map((a) => `<li>${esc(a)}</li>`).join('');
  return `
    <article class="card card--${long ? 'long' : 'short'}">
      <div class="card__head">
        <span class="side side--${long ? 'long' : 'short'}">${long ? 'Buy' : 'Sell'}</span>
        <span class="card__sym">${esc(item.symbol)}</span>
        <span class="state" data-s="${esc(item.state)}">${esc(item.state)}</span>
        <span class="card__meta">${item.entry_deadline_local ? `enter by ${esc(item.entry_deadline_local)}<br>` : ''}
          ${item.resolve_by_local ? `resolves by ${esc(item.resolve_by_local)}` : ''}</span>
      </div>
      ${kv}
      <div class="why"><div class="why__h">What to do now</div>
        <ul class="advice">${advice}</ul></div>
    </article>`;
}

function checksList(checks) {
  return `<ul class="checks">${(checks || []).map((c) => `
    <li class="check ${c.fired ? 'is-on' : 'is-off'}">
      <span class="check__mark">${c.fired ? '&#10003;' : '&#10007;'}</span>
      <span class="check__code">${esc(c.code)}</span>
      <span class="check__w">+${Number(c.weight).toFixed(0)}</span>
      <span class="check__txt">${esc(c.fired ? c.detail : `not met: ${c.title}`)}</span>
    </li>`).join('')}</ul>`;
}

function readingsLine(r, digits) {
  if (!r || r.price === undefined) return '';
  const p = (v) => (v === null || v === undefined ? '-' : Number(v).toFixed(digits));
  const parts = [];
  if (r.atr !== null && r.atr !== undefined) parts.push(`ATR ${p(r.atr)}`);
  if (r.rsi !== null && r.rsi !== undefined) parts.push(`RSI ${Number(r.rsi).toFixed(1)}`);
  if (r.adx !== null && r.adx !== undefined) parts.push(`ADX ${Number(r.adx).toFixed(1)}`);
  if (r.plus_di !== null && r.plus_di !== undefined) {
    parts.push(`+DI ${Number(r.plus_di).toFixed(1)} / -DI ${Number(r.minus_di).toFixed(1)}`);
  }
  if (r.ema_fast !== null && r.ema_fast !== undefined) {
    parts.push(`EMA ${p(r.ema_fast)} · ${p(r.ema_slow)} · ${p(r.ema_trend)}`);
  }
  if (r.htf_trend) parts.push(`HTF ${r.htf_trend}`);
  if (r.trend) parts.push(`structure ${r.trend}`);
  if (r.last_break) {
    parts.push(`${r.last_break.kind} ${r.last_break.direction} at ${p(r.last_break.level)},
      ${r.last_break.bars_ago} bars before`);
  }
  return `<p class="readings">${esc(parts.join('  ·  '))}</p>`;
}

function caseCard(c, number) {
  const s = c.signal;
  const long = s.direction === 'long';
  const d = c.digits ?? 5;
  const verdict = c.verdict || 'OPEN';
  const res = c.result;
  const p = c.prediction || {};
  const chart = res && res.path && res.path.length > 1
    ? candleChart(res.path, { ...s, digits: d }) : '';
  const outcomeKv = res ? `
    <div class="kv">
      <div class="kv__item"><div class="kv__k">Filled</div>
        <div class="kv__v">${res.fill_price === null || res.fill_price === undefined
          ? '-' : esc(fmtPrice(res.fill_price, d))}</div></div>
      <div class="kv__item"><div class="kv__k">Exit</div>
        <div class="kv__v">${res.exit_price === null || res.exit_price === undefined
          ? '-' : esc(fmtPrice(res.exit_price, d))}</div></div>
      <div class="kv__item"><div class="kv__k">Bars held</div>
        <div class="kv__v">${res.bars_held ?? '-'}</div></div>
      <div class="kv__item"><div class="kv__k">Best / worst</div>
        <div class="kv__v">${fmtR(res.mfe_r)} / ${fmtR(res.mae_r)}</div></div>
    </div>` : '';
  return `
    <details class="case case--${VERDICT_CLASS[verdict] || 'open'}">
      <summary class="case__sum">
        <span class="case__num">#${number}</span>
        <span class="side side--${long ? 'long' : 'short'}">${long ? 'Buy' : 'Sell'}</span>
        <strong>${esc(s.symbol)}</strong>
        <span class="grade" data-g="${esc(s.grade)}">${esc(s.grade)}</span>
        <span class="dim">${fmtPct(s.confidence, 0)} · ${Number(s.risk_reward).toFixed(1)}R</span>
        <span class="case__when">${esc(c.made_at_local || (s.issued_at || '').slice(0, 16))}</span>
        <span class="outcome" data-o="${VERDICT_OUTCOME[verdict] || 'open'}">${esc(verdict)}</span>
        ${res ? `<span class="case__r ${signClass(res.r_multiple)}">${fmtR(res.r_multiple)}</span>
          <span class="case__cash">${fmtNum(c.cash)} ${esc(s.account_currency || '')}</span>` : ''}
      </summary>
      <div class="case__body">
        ${p.claim ? `<p class="case__claim">${esc(p.claim)}</p>` : ''}
        ${ladder({ ...s, digits: d })}
        <div class="kv">
          <div class="kv__item"><div class="kv__k">Size</div>
            <div class="kv__v">${Number(s.position_lots).toFixed(2)} lots</div></div>
          <div class="kv__item"><div class="kv__k">Risking</div>
            <div class="kv__v kv__v--neg">${esc(fmtNum(s.risk_amount))}</div></div>
          <div class="kv__item"><div class="kv__k">Enter by</div>
            <div class="kv__v" style="font-size:12px">${esc(p.entry_deadline_local || '-')}</div></div>
          <div class="kv__item"><div class="kv__k">Resolves by</div>
            <div class="kv__v" style="font-size:12px">${esc(p.resolve_by_local || '-')}</div></div>
        </div>
        <div class="why">
          <div class="why__h">What the model saw — ${Number(s.score).toFixed(0)} of
            ${Number(s.max_score).toFixed(0)} points · ${esc(c.session || '')}</div>
          ${c.snapshot_complete ? '' : `<p class="partial">Recorded before the ledger kept
            snapshots: the checks that did not fire are inferred, not recorded.</p>`}
          ${checksList(c.checks)}
          ${readingsLine(c.readings, d)}
        </div>
        <div class="happened">
          <div class="why__h">What happened</div>
          <p>${esc(res ? res.narrative : 'Nothing yet. The market has not answered.')}</p>
          ${chart}
        </div>
        ${outcomeKv}
        ${c.origin === 'replay' ? `<div class="card__foot">Replayed from history: the outcome
          was in the file before this call was made.</div>` : ''}
      </div>
    </details>`;
}

function renderLedger(data) {
  const out = $('#lg-results');
  const sc = data.scorecards || {};
  const live = (data.live || []).map(liveCard).join('');
  const total = data.count || 0;
  const cases = (data.cases || []).map((c, i) => caseCard(c, total - i)).join('');

  if (!total) {
    out.innerHTML = ledgerSummaryCard(data) + `<div class="empty">
      <div class="empty__big">${data.origin === 'replay'
        ? 'The model made no calls on this history' : 'No predictions on record yet'}</div>
      <div class="empty__sub">${data.origin === 'replay'
        ? 'Try a longer window or another pair.'
        : 'Tick “Journal signals” on the Scan tab. Every signal issued is written down here before its outcome exists.'}</div>
    </div>`;
    return;
  }

  const checkRows = (sc.checks || []).map((e) => {
    const f = e.fired, m = e.missing;
    return `<tr>
      <td class="mono">${esc(e.code)}</td>
      <td class="mono">${Number(e.weight).toFixed(0)}</td>
      <td class="mono">${f.trades ? `${fmtPct(f.win_rate, 0)} <span class="dim">n=${f.trades}</span>` : '-'}</td>
      <td class="mono">${m.trades ? `${fmtPct(m.win_rate, 0)} <span class="dim">n=${m.trades}</span>` : '-'}</td>
      <td class="mono ${e.difference === null ? '' : signClass(e.difference)}">${
        e.difference === null ? 'n/a' : `${e.difference >= 0 ? '+' : ''}${(e.difference * 100).toFixed(0)} pts`}</td>
    </tr>`;
  }).join('');

  const scorecards = data.summary.resolved ? `
    <article class="card">
      <div class="card__head"><span class="card__sym">Does the confidence number mean anything?</span>
        <span class="card__meta">win rate by the confidence the model quoted</span></div>
      ${bucketTable('Quoted', sc.calibration,
        'If higher bands are not winning more often, the number counts boxes ticked, not odds. Read n first.')}
    </article>
    <article class="card">
      <div class="card__head"><span class="card__sym">Which reasons travelled with the winners</span>
        <span class="card__meta">win rate when a check fired vs when it did not</span></div>
      <div class="tablewrap"><table>
        <thead><tr><th>Check</th><th>Weight</th><th>Fired</th><th>Missing</th><th>Diff</th></tr></thead>
        <tbody>${checkRows}</tbody></table></div>
      <div class="card__foot">Every signal cleared the threshold, so most checks fired on most
        calls and the missing column is thin by construction.</div>
    </article>
    <article class="card">
      <div class="card__head"><span class="card__sym">By pair</span></div>
      ${bucketTable('Pair', sc.symbol)}
    </article>
    <details class="card plan">
      <summary class="plan__h">By grade, direction, session and month</summary>
      ${bucketTable('Grade', sc.grade)}
      ${bucketTable('Direction', sc.direction)}
      ${bucketTable('Session', sc.session)}
      ${bucketTable('Month', sc.month)}
      ${(sc.strategy || []).length > 1 ? bucketTable('Model', sc.strategy) : ''}
    </details>` : '';

  out.innerHTML = ledgerSummaryCard(data)
    + (live ? `<h2 class="stack__h">Open predictions — where each stands, and what to do now</h2>${live}` : '')
    + scorecards
    + `<h2 class="stack__h">Case files — ${data.shown} of ${total}, newest first</h2>`
    + cases;
}

async function loadLedger() {
  const data = await withBusy($('#lg-refresh'), 'Loading', () => api('/api/ledger'));
  renderLedger(data);
}

async function resolveLedger() {
  const data = await withBusy($('#lg-resolve'), 'Settling',
    () => api('/api/forecast/resolve', { method: 'POST', body: {} }));
  toast(`${data.resolved} of ${data.checked} settled`);
  await loadLedger();
}

async function runReplay() {
  const params = {
    symbol: $('#lg-symbol').value.trim(),
    source: $('#lg-source').value,
    bars: $('#lg-bars').value,
    split: $('#lg-split').value,
  };
  const data = await withBusy($('#lg-replay'), 'Replaying',
    () => api('/api/ledger/replay', { params }));
  renderLedger(data);
  toast(`${data.count} call${data.count === 1 ? '' : 's'} replayed`);
}

/* --------------------------------------------------------------- bootstrap */

function setupTabs() {
  $$('.tabs__btn').forEach((button) => {
    button.addEventListener('click', () => {
      $$('.tabs__btn').forEach((b) => {
        b.classList.toggle('is-active', b === button);
        b.setAttribute('aria-selected', String(b === button));
      });
      $$('.view').forEach((v) => v.classList.toggle('is-active', v.id === `view-${button.dataset.view}`));
      if (button.dataset.view === 'journal') loadJournal().catch(() => {});
      if (button.dataset.view === 'forecast') loadForecast().catch(() => {});
      if (button.dataset.view === 'ledger') loadLedger().catch(() => {});
    });
  });
}

function setupTheme() {
  const stored = localStorage.getItem('tb-theme');
  if (stored) document.documentElement.dataset.theme = stored;
  $('#theme-toggle').addEventListener('click', () => {
    const current = document.documentElement.dataset.theme;
    const isDark = current === 'dark'
      || (current !== 'light' && matchMedia('(prefers-color-scheme: dark)').matches);
    const next = isDark ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('tb-theme', next); } catch { /* private mode */ }
  });
}

async function bootstrap() {
  setupTabs();
  setupTheme();

  $('#scan-run').addEventListener('click', () => runScan().catch(() => {}));
  $('#bt-run').addEventListener('click', () => runBacktest().catch(() => {}));
  $('#cal-run').addEventListener('click', () => runCalibrate().catch(() => {}));
  $('#risk-run').addEventListener('click', () => runRisk().catch(() => {}));
  $('#journal-refresh').addEventListener('click', () => loadJournal().catch(() => {}));
  $('#pairs-run').addEventListener('click', () => runPairs().catch(() => {}));
  $('#fc-refresh').addEventListener('click', () => loadForecast().catch(() => {}));
  $('#fc-resolve').addEventListener('click', () => resolveForecasts().catch(() => {}));
  $('#lg-refresh').addEventListener('click', () => loadLedger().catch(() => {}));
  $('#lg-resolve').addEventListener('click', () => resolveLedger().catch(() => {}));
  $('#lg-replay').addEventListener('click', () => runReplay().catch(() => {}));

  $('#risk-mode').addEventListener('change', (event) => {
    const fromBacktest = event.target.value === 'backtest';
    $('#risk-rate-field').classList.toggle('is-hidden', fromBacktest);
    $('#risk-symbol-field').classList.toggle('is-hidden', !fromBacktest);
  });

  // Delegated so buttons rendered later still work.
  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-close]');
    if (button) closeTrade(button.dataset.close).catch(() => {});
  });
  $('#scan-symbols').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') runScan().catch(() => {});
  });

  try {
    const [health, settings, symbols] = await Promise.all([
      api('/api/health'), api('/api/settings'), api('/api/symbols'),
    ]);
    setStatus('ready', 'ok');

    const options = symbols.timeframes.map((tf) =>
      `<option value="${tf}"${tf === settings.data.timeframe ? ' selected' : ''}>${tf}</option>`).join('');
    $('#scan-timeframe').innerHTML = options;
    $('#bt-timeframe').innerHTML = options;
    $('#pairs-timeframe').innerHTML = options;

    $('#scan-symbols').value = settings.data.symbols.join(', ');
    $('#pairs-symbols').value = settings.data.symbols.join(', ');
    // data.symbols may hold a group name like "all"; the single-symbol views need
    // an actual pair, so they read the resolved list.
    const firstPair = (settings.data.resolved_symbols || settings.data.symbols)[0] || 'EURUSD';
    $('#bt-symbol').value = firstPair;
    $('#cal-symbol').value = firstPair;
    $('#risk-symbol').value = firstPair;
    $('#lg-symbol').value = firstPair;

    $('#foot-meta').textContent =
      `v${health.version} · ${settings.strategy.name} · min ${settings.risk.min_risk_reward}:1 · `
      + `${settings.account.risk_per_trade_pct}% risk on `
      + `${settings.account.balance.toLocaleString()} ${settings.account.currency}`;

    await runScan();
  } catch (error) {
    setStatus('offline', 'error');
    $('#scan-results').innerHTML =
      `<div class="err">Could not reach the server — ${esc(error.message)}</div>`;
  }

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => { /* offline is a bonus */ });
  }
}

document.addEventListener('DOMContentLoaded', bootstrap);
