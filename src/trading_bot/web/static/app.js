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
          ${esc((s.issued_at || '').slice(0, 16).replace('T', ' '))} UTC</span>
      </div>
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
      <div class="why"><div class="why__h">Why — ${s.score.toFixed(0)} of
        ${s.max_score.toFixed(0)} points</div>
        <div class="chips">${reasons}</div></div>
      ${warns}
      <div class="card__foot">You place this trade. This tool does not and will not.</div>
    </article>`;
}

function noSetupCard(row) {
  const confl = row.confluence === null || row.confluence === undefined
    ? 'not scored' : `best confluence ${fmtPct(row.confluence, 0)}`;
  return `
    <article class="card">
      <div class="card__head">
        <span class="card__sym">${esc(row.symbol)}</span>
        <span class="chip">no setup</span>
        <span class="card__meta">${esc(row.timeframe)} &middot; ${esc(row.session || '')}<br>
          last ${esc(fmtPrice(row.last_price, row.digits))}</span>
      </div>
      ${candleChart(row.candles, null)}
      <div class="card__foot">Nothing met the rules here — ${esc(confl)}.
        No setup is a position too.</div>
    </article>`;
}

/* -------------------------------------------------------------- scan view */

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
  summary.innerHTML =
    `<span><strong>${data.found}</strong> setup${data.found === 1 ? '' : 's'} found</span>
     <span>min R:R <strong>${data.min_risk_reward.toFixed(0)}:1</strong></span>
     <span>min confluence <strong>${fmtPct(data.min_confluence, 0)}</strong></span>
     <span>${esc(data.scanned_at.slice(11, 16))} UTC</span>`;

  const out = $('#scan-results');
  if (!data.results.length) {
    out.innerHTML = `<div class="empty"><div class="empty__big">Nothing scanned</div>
      <div class="empty__sub">Add a symbol above.</div></div>`;
    return;
  }
  out.innerHTML = data.results.map((row) => {
    if (row.status === 'error') {
      return `<div class="err"><strong>${esc(row.symbol)}</strong> — ${esc(row.message)}</div>`;
    }
    return row.status === 'signal' ? signalCard(row) : noSetupCard(row);
  }).join('');

  if (data.found > 0) toast(`${data.found} setup${data.found === 1 ? '' : 's'} found`);
}

/* ---------------------------------------------------------- backtest view */

function metricsBlock(payload) {
  const m = payload.metrics, g = payload.gate;
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

    $('#scan-symbols').value = settings.data.symbols.join(', ');
    $('#bt-symbol').value = settings.data.symbols[0] || 'EURUSD';
    $('#cal-symbol').value = settings.data.symbols[0] || 'EURUSD';
    $('#risk-symbol').value = settings.data.symbols[0] || 'EURUSD';

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
