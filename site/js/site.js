/* labrat site behaviour: nav, reveals, copy buttons, the coin card (coin.js), the live panel's badge / readout /
   charts, and the recorded training curves. The 3D view itself is js/live.js (imported dynamically, so this page
   works without it).

   Honesty rules this file enforces:
   - "LIVE TRAINING" only when live.js reports live === true (a publisher is streaming a training run), and then the
     caption says what it is: the newest saved checkpoint, playing in its own simulation.
   - otherwise it is a REPLAY of a recorded launch session (replay/session.json names it), or STANDBY when there is
     none, and the readout shows the RECORDED training logs, labelled as recorded / not live.
   - the coin: "Launching soon" until coin.js sets window.LABRAT_COIN with a real contract address; then only its
     public facts (contract, links, transaction, block, date). */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const RM = window.matchMedia ? matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };

/* ------------------------------------------------------------------ nav */
(function nav() {
  const bar = $('#nav'), menu = $('#menu'), links = $('#links');
  const onScroll = () => bar.classList.toggle('scrolled', window.scrollY > 8);
  addEventListener('scroll', onScroll, { passive: true }); onScroll();
  const close = () => { links.classList.remove('open'); menu.setAttribute('aria-expanded', 'false'); };
  menu.addEventListener('click', () => {
    const open = !links.classList.contains('open');
    links.classList.toggle('open', open); menu.setAttribute('aria-expanded', String(open));
  });
  links.addEventListener('click', e => { if (e.target.closest('a')) close(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && links.classList.contains('open')) { close(); menu.focus(); } });
  document.addEventListener('click', e => { if (links.classList.contains('open') && !e.target.closest('#nav')) close(); });

  // highlight the section in view
  if (!('IntersectionObserver' in window)) return;
  const map = new Map($$('a', links).map(a => [a.getAttribute('href').slice(1), a]));
  const seen = new Map();
  const io = new IntersectionObserver(es => {
    es.forEach(e => seen.set(e.target.id, e.isIntersecting ? e.intersectionRatio : 0));
    let best = null, br = 0;
    seen.forEach((r, id) => { if (r > br) { br = r; best = id; } });
    map.forEach((a, id) => a.classList.toggle('on', id === best));
  }, { threshold: [0, .15, .3, .5, .7], rootMargin: '-40% 0px -45% 0px' });
  map.forEach((a, id) => { const s = document.getElementById(id); if (s) io.observe(s); });
})();

/* ------------------------------------------------------------------ reveal on scroll */
(function reveal() {
  const els = $$('.reveal, .steps11');
  if (RM.matches || !('IntersectionObserver' in window)) { els.forEach(e => e.classList.add('in')); return; }
  const io = new IntersectionObserver(es => es.forEach(e => {
    if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
  }), { threshold: 0.08, rootMargin: '0px 0px -6% 0px' });
  els.forEach(e => io.observe(e));
  // anything already on screen at load shows at once (no flash of empty hero)
  requestAnimationFrame(() => els.forEach(e => {
    const r = e.getBoundingClientRect(); if (r.top < innerHeight && r.bottom > 0) e.classList.add('in');
  }));
})();

/* ------------------------------------------------------------------ copy buttons */
(function copy() {
  async function write(t) {
    try { await navigator.clipboard.writeText(t); return true; } catch (e) { /* fall back */ }
    try {
      const ta = document.createElement('textarea'); ta.value = t; ta.setAttribute('readonly', '');
      ta.style.cssText = 'position:fixed;left:-9999px;top:0'; document.body.appendChild(ta); ta.select();
      const ok = document.execCommand('copy'); ta.remove(); return ok;
    } catch (e) { return false; }
  }
  // delegated, so copy buttons filled in later (the coin card) work too
  document.addEventListener('click', async e => {
    const b = e.target.closest && e.target.closest('.copy[data-copy]');
    if (!b || b.dataset.busy) return;
    b.dataset.busy = '1';
    const ok = await write(b.dataset.copy);
    const span = b.querySelector('span'), was = span ? span.textContent : null, label = b.getAttribute('aria-label');
    b.classList.toggle('done', ok);
    if (span) span.textContent = ok ? 'copied' : 'copy failed';
    b.setAttribute('aria-label', ok ? 'Copied' : 'Copy failed');
    setTimeout(() => {
      b.classList.remove('done'); if (span) span.textContent = was;
      if (label) b.setAttribute('aria-label', label); else b.removeAttribute('aria-label');
      delete b.dataset.busy;
    }, 1600);
  });
})();

/* ------------------------------------------------------------------ the coin ($LABRAT), from coin.js
   null (or anything without a real contract address): "Launching soon", no address, no links, no numbers.
   Set after the launch: the contract, pons and explorer links, the transaction and the block. Only the fields
   below are read. */
const COIN = (function coin() {
  const HEX40 = /^0x[0-9a-fA-F]{40}$/, HEX64 = /^0x[0-9a-fA-F]{64}$/;
  const EXPLORER = 'https://robinhoodchain.blockscout.com';
  const c = window.LABRAT_COIN;
  const ok = !!c && typeof c === 'object' && HEX40.test(String(c.address || ''));
  if (c && !ok) console.warn('labrat: coin.js has no valid contract address; showing "Launching soon"');
  document.documentElement.setAttribute('data-coin', ok ? 'live' : 'soon');
  if (!ok) return null;
  // links only to pons and the Robinhood Chain explorer; anything else falls back to the address's own pages
  const onHost = (u, host) => {
    try { const x = new URL(String(u)); return x.protocol === 'https:' && (x.hostname === host || x.hostname.endsWith('.' + host)) ? x.href : null; }
    catch (e) { return null; }
  };
  const addr = String(c.address);
  const tx = HEX64.test(String(c.tx || '')) ? String(c.tx) : null;
  const block = Number.isInteger(+c.block) && +c.block > 0 ? +c.block : null;
  const date = /^\d{4}-\d{2}-\d{2}$/.test(String(c.launched || '')) ? String(c.launched) : null;
  const k = {
    addr, tx, block, date,
    pons: onHost(c.pons, 'ponsfamily.com') || 'https://www.ponsfamily.com/launchpad/' + addr,
    explorer: onHost(c.explorer, 'robinhoodchain.blockscout.com') || EXPLORER + '/token/' + addr,
    txUrl: tx ? EXPLORER + '/tx/' + tx : null,
    blockUrl: block ? EXPLORER + '/block/' + block : null,
  };
  const short = (h, a = 6, b = 4) => h.slice(0, a) + '…' + h.slice(-b);
  const each = (sel, f) => $$(sel).forEach(f);
  each('.js-addr', e => { e.textContent = addr; });
  each('.js-addr-short', e => { e.textContent = short(addr); });
  each('.js-addr-link', e => { e.href = k.explorer; });
  each('.js-addr-copy', e => { e.dataset.copy = addr; });
  each('.js-pons', e => { e.href = k.pons; });
  each('.js-explorer', e => { e.href = k.explorer; });
  each('.js-need-tx', e => { e.hidden = !tx; });
  each('.js-tx', e => { e.textContent = tx || ''; });
  each('.js-tx-short', e => { e.textContent = tx ? short(tx, 10, 6) : ''; });
  each('.js-tx-link', e => { if (tx) e.href = k.txUrl; });
  each('.js-tx-copy', e => { if (tx) e.dataset.copy = tx; });
  each('.js-need-block', e => { e.hidden = !block; });
  each('.js-block', e => { e.textContent = block ? block.toLocaleString('en-US') : ''; });
  each('.js-block-link', e => { if (block) e.href = k.blockUrl; });
  each('.js-need-date', e => { e.hidden = !date; });
  each('.js-date', e => { e.textContent = date || ''; });
  each('.js-launched-sep', e => { e.textContent = date ? ' · ' + date : ''; });
  each('.cc-tx', e => { e.hidden = !tx && !block; });
  return k;
})();
void COIN;

/* ------------------------------------------------------------------ formatting */
const fmtSteps = n => {
  if (typeof n !== 'number' || !isFinite(n)) return '—';
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 1 : 2).replace(/\.?0+$/, '') + ' M';
  if (n >= 1e3) return Math.round(n / 1e3) + ' k';
  return String(Math.round(n));
};
const pct = v => (typeof v === 'number' && isFinite(v)) ? (v * 100).toFixed(1) + '%' : '—';
const num = (v, d = 2) => (typeof v === 'number' && isFinite(v)) ? v.toFixed(d) : '—';
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* the task a training run is doing, in plain words, and which log column counts as "success" for it
   (train.py: lever rows carry clean_rate = clean presses per episode; cursor/steer rows carry hits per episode) */
const TASKS = {
  lever: { name: 'Lever press', what: 'press the lever cleanly and stay standing',
           key: 'clean_rate', label: 'Attempts with a clean press', fmt: pct, yMax: 1 },
  cursor: { name: 'Cursor', what: 'move the cursor onto lit targets and click',
            key: 'hits', label: 'Targets clicked per attempt (of 4)', fmt: v => num(v, 2) },
  steer: { name: 'Steering', what: 'point the head at lit targets, press only on target',
           key: 'hits', label: 'Targets clicked per attempt (of 4)', fmt: v => num(v, 2) },
};

/* ------------------------------------------------------------------ charts (canvas, one series each) */
const COL = { gold: '#F5AC29', goldy: '#FCF010', pink: '#e255b4', magenta: '#D60C94', grid: 'rgba(136,8,181,.2)',
              label: '#9c75df', ink: '#f4f0fb' };

function makeChart(fig) {
  const box = $('.c-box', fig), cv = $('canvas', box), tip = $('.c-tip', box);
  const ch = { fig, box, cv, tip, v: $('.c-v', fig), l: $('.c-l', fig), rows: [], key: null, color: COL.gold,
               fmt: v => num(v), yMin: null, yMax: null, hover: -1, marker: null, W: 0, H: 0 };
  ch.set = (rows, o) => {
    Object.assign(ch, o);
    ch.rows = (rows || []).filter(r => r && typeof r.steps === 'number' && typeof r[ch.key] === 'number' && isFinite(r[ch.key]));
    if (o && o.label != null) ch.l.textContent = o.label;
    const last = ch.rows[ch.rows.length - 1];
    ch.v.textContent = last ? ch.fmt(last[ch.key]) : '—';
    if (ch.hover >= ch.rows.length) ch.hover = -1;
    draw(ch);
  };
  const size = () => {
    const r = box.getBoundingClientRect(), d = Math.min(2, window.devicePixelRatio || 1);
    ch.W = Math.max(1, r.width); ch.H = Math.max(1, r.height);
    cv.width = Math.round(ch.W * d); cv.height = Math.round(ch.H * d); ch.d = d; draw(ch);
  };
  if (window.ResizeObserver) new ResizeObserver(size).observe(box); else addEventListener('resize', size);
  size();
  const pick = e => {
    if (!ch.rows.length) return;
    const r = cv.getBoundingClientRect(), x = e.clientX - r.left;
    const g = geom(ch); let best = 0, bd = Infinity;
    ch.rows.forEach((row, i) => { const dx = Math.abs(g.X(row.steps) - x); if (dx < bd) { bd = dx; best = i; } });
    ch.hover = best; draw(ch);
    const row = ch.rows[best];
    tip.innerHTML = '<em>' + esc(fmtSteps(row.steps)) + ' steps</em><br>' + esc(ch.fmt(row[ch.key]));
    tip.hidden = false;
    const px = g.X(row.steps), tw = tip.offsetWidth;
    tip.style.left = Math.max(4, Math.min(ch.W - tw - 4, px + 10 + tw > ch.W ? px - tw - 10 : px + 10)) + 'px';
  };
  cv.addEventListener('pointermove', pick);
  cv.addEventListener('pointerdown', pick);
  cv.addEventListener('pointerleave', () => { ch.hover = -1; tip.hidden = true; draw(ch); });
  return ch;
}

function geom(ch) {
  const pl = 34, pr = 12, pt = 12, pb = 18, rows = ch.rows, k = ch.key;
  let x0 = rows.length ? rows[0].steps : 0, x1 = rows.length ? rows[rows.length - 1].steps : 1;
  if (x1 <= x0) x1 = x0 + 1;
  let lo = Infinity, hi = -Infinity;
  rows.forEach(r => { lo = Math.min(lo, r[k]); hi = Math.max(hi, r[k]); });
  if (!isFinite(lo)) { lo = 0; hi = 1; }
  if (ch.yMin != null) lo = Math.min(lo, ch.yMin);
  if (ch.yMax != null) hi = Math.max(hi, ch.yMax);
  if (hi - lo < 1e-9) { hi += 0.5; lo -= 0.5; }
  const pad = (hi - lo) * 0.08; if (ch.yMax == null) hi += pad; if (ch.yMin == null) lo -= pad;
  const W = ch.W, H = ch.H;
  return { pl, pr, pt, pb, lo, hi, x0, x1,
           X: s => pl + (s - x0) / (x1 - x0) * (W - pl - pr),
           Y: v => pt + (1 - (v - lo) / (hi - lo)) * (H - pt - pb) };
}

function draw(ch) {
  const c = ch.cv.getContext('2d'); if (!c) return;
  const d = ch.d || 1, W = ch.W, H = ch.H;
  c.setTransform(d, 0, 0, d, 0, 0); c.clearRect(0, 0, W, H);
  const g = geom(ch), rows = ch.rows, k = ch.key;
  c.font = '500 9.5px "JetBrains Mono", ui-monospace, monospace'; c.textBaseline = 'middle';
  // recessive grid + y labels
  for (let i = 0; i <= 2; i++) {
    const v = g.lo + (g.hi - g.lo) * (i / 2), y = Math.round(g.Y(v)) + 0.5;
    c.strokeStyle = COL.grid; c.lineWidth = 1; c.beginPath(); c.moveTo(g.pl, y); c.lineTo(W - g.pr, y); c.stroke();
    c.fillStyle = COL.label; c.textAlign = 'right';
    c.fillText(ch.key === 'clean_rate' || ch.key === 'fall_rate' ? Math.round(v * 100) + '%' : shortNum(v), g.pl - 6, y);
  }
  if (!rows.length) {
    c.fillStyle = COL.label; c.textAlign = 'center'; c.fillText('no rows yet', (g.pl + W - g.pr) / 2, H / 2); return;
  }
  // x range
  c.textAlign = 'left'; c.fillStyle = COL.label; c.fillText(fmtSteps(g.x0), g.pl, H - 7);
  c.textAlign = 'right'; c.fillText(fmtSteps(g.x1) + ' steps', W - g.pr, H - 7);
  // a marker (e.g. where a run resumed): the dashed line under the curve, its label on top (below)
  const mk = ch.marker && ch.marker.steps > g.x0 && ch.marker.steps < g.x1 ? Math.round(g.X(ch.marker.steps)) + 0.5 : null;
  if (mk !== null) {
    c.save(); c.setLineDash([3, 4]); c.strokeStyle = 'rgba(210,192,240,.45)'; c.beginPath(); c.moveTo(mk, g.pt); c.lineTo(mk, H - g.pb); c.stroke(); c.restore();
  }
  // area + line
  const grad = c.createLinearGradient(0, g.pt, 0, H - g.pb);
  grad.addColorStop(0, hexA(ch.color, .32)); grad.addColorStop(1, hexA(ch.color, 0));
  c.beginPath();
  rows.forEach((r, i) => { const x = g.X(r.steps), y = g.Y(r[k]); i ? c.lineTo(x, y) : c.moveTo(x, y); });
  c.lineTo(g.X(rows[rows.length - 1].steps), H - g.pb); c.lineTo(g.X(rows[0].steps), H - g.pb); c.closePath();
  c.fillStyle = grad; c.fill();
  c.beginPath();
  rows.forEach((r, i) => { const x = g.X(r.steps), y = g.Y(r[k]); i ? c.lineTo(x, y) : c.moveTo(x, y); });
  c.strokeStyle = ch.color; c.lineWidth = 2; c.lineJoin = 'round'; c.lineCap = 'round'; c.stroke();
  if (mk !== null) {   // low and left of the line, on a dark backing, so the curve never runs through the text
    const tw = c.measureText(ch.marker.label).width, my = H - g.pb - 9;
    c.fillStyle = 'rgba(3,1,8,.85)'; c.fillRect(mk - 9 - tw, my - 7, tw + 8, 14);
    c.fillStyle = COL.label; c.textAlign = 'right'; c.fillText(ch.marker.label, mk - 5, my);
  }
  // the newest point
  const L = rows[rows.length - 1], lx = g.X(L.steps), ly = g.Y(L[k]);
  c.fillStyle = hexA(ch.color, .25); c.beginPath(); c.arc(lx, ly, 7, 0, 7); c.fill();
  c.fillStyle = ch.color; c.beginPath(); c.arc(lx, ly, 3.4, 0, 7); c.fill();
  // hover crosshair
  if (ch.hover >= 0 && rows[ch.hover]) {
    const r = rows[ch.hover], x = g.X(r.steps), y = g.Y(r[k]);
    c.strokeStyle = 'rgba(244,240,251,.35)'; c.lineWidth = 1; c.beginPath(); c.moveTo(Math.round(x) + .5, g.pt); c.lineTo(Math.round(x) + .5, H - g.pb); c.stroke();
    c.fillStyle = '#000'; c.strokeStyle = COL.ink; c.lineWidth = 2; c.beginPath(); c.arc(x, y, 4.5, 0, 7); c.fill(); c.stroke();
  }
}
function shortNum(v) {
  const a = Math.abs(v);
  if (a >= 1000) return (v / 1000).toFixed(a >= 10000 ? 0 : 1) + 'k';
  if (a >= 100) return v.toFixed(0);
  if (a >= 10) return v.toFixed(0);
  return v.toFixed(1);
}
function hexA(hex, a) {
  const h = hex.replace('#', ''), n = parseInt(h.length === 3 ? h.split('').map(x => x + x).join('') : h, 16);
  return 'rgba(' + (n >> 16 & 255) + ',' + (n >> 8 & 255) + ',' + (n & 255) + ',' + a + ')';
}

/* ------------------------------------------------------------------ recorded training logs (assets/training.json) */
let RECORDED = null;
const recordedReady = fetch('assets/training.json').then(r => r.ok ? r.json() : null).then(j => (RECORDED = j)).catch(() => null);

function resumeMarker(rows) {
  // where a run resumed from a saved checkpoint with its difficulty dial reset (a sharp drop in "difficulty")
  for (let i = 1; i < rows.length; i++) {
    const a = rows[i - 1].difficulty, b = rows[i].difficulty;
    if (typeof a === 'number' && typeof b === 'number' && a - b > 0.5) return { steps: rows[i].steps, label: 'resumed' };
  }
  return null;
}

(function brainCurves() {
  const fl = $('#c-lever'), fs = $('#c-steer'); if (!fl || !fs) return;
  const cl = makeChart(fl), cs = makeChart(fs);
  recordedReady.then(j => {
    if (!j || !j.runs) return;
    const lv = j.runs.lever_v3 && j.runs.lever_v3.rows, st = j.runs.steer_v1 && j.runs.steer_v1.rows;
    if (lv) cl.set(lv, { key: 'clean_rate', color: COL.gold, fmt: pct, yMax: 1 });
    if (st) cs.set(st, { key: 'hits', color: COL.pink, fmt: v => num(v, 2) + ' of 4', yMin: 0, marker: resumeMarker(st) });
  });
})();

/* ------------------------------------------------------------------ the live panel */
const LIVE = {
  mode: null, st: {}, rows: [], run: null, runKey: null, lastWindow: [], phGone: false, failed: false, tgt: null,
  el: $('#live'), badge: $('#badge'), badgeT: $('#badge-t'), label: $('#live-label'),
  nav: $('#nav-status'), navT: $('#nav-status-t'), hudMode: $('#hud-mode'), note: $('#hud-note'),
  ph: $('#live-ph'), phTitle: $('#ph-title'), phSub: $('#ph-sub'),
  cReward: makeChart($('#c-reward')), cSuccess: makeChart($('#c-success')),
};
const REPLAY_LABEL = 'Replay: a recorded launch session';   // when replay/session.json carries no label of its own

const stripTag = s => String(s || '').replace(/^\s*(LIVE|REPLAY|STANDBY)\s*[·:-]\s*/i, '').replace(/^\s*replay\s*[:·-]\s*/i, '').trim();

function setMode(mode) {
  const L = LIVE, st = L.st || {};
  L.mode = mode; L.el.dataset.mode = mode;
  const B = { connecting: ['conn', 'CONNECTING', 'connecting'], live: ['live', 'LIVE TRAINING', 'live'],
              test: ['test', 'TEST STREAM', 'test'],
              replay: ['replay', 'REPLAY', 'replay'], standby: ['conn', 'STANDBY', 'standby'],
              offline: ['off', 'OFFLINE', 'offline'] }[mode];
  L.badge.className = 'badge ' + B[0]; L.badgeT.textContent = B[1];
  L.nav.className = 'nav-status ' + B[0]; L.navT.textContent = B[2];
  L.nav.title = mode === 'live' ? 'A training run is streaming now'
    : mode === 'test' ? 'A publisher test stream (not a live training run)'
    : mode === 'replay' ? 'Showing a replay of a recorded launch session' : 'Status of the live panel';
  L.nav.setAttribute('aria-label', 'Live panel: ' + B[2]);

  if (mode === 'live') {
    const run = st.run || L.run;
    L.label.textContent = 'Training run ' + (run || '(unnamed)') + ' · the newest saved checkpoint' +
      (typeof st.steps === 'number' ? ' (' + fmtSteps(st.steps) + ' steps)' : '') + ', playing in its own simulation';
  } else if (mode === 'test') {
    // a publisher test stream (an old run streamed on a local relay with RELAY_ALLOW_TEST=1): never called live
    const l = String(st.label || '').trim();
    L.label.textContent = (/^TEST\b/.test(l) ? l : 'TEST (not a live training run): ' + (l || 'run ' + (st.run || ''))) +
      (typeof st.steps === 'number' ? ' · checkpoint ' + fmtSteps(st.steps) + ' steps' : '');
  } else if (mode === 'replay') {
    L.label.textContent = 'Replay: ' + (stripTag(st.label) || stripTag(REPLAY_LABEL)) + ' · not live' +
      (st.waiting ? ' · a training stream is connecting…' : '');
  } else if (mode === 'standby') {
    L.label.textContent = st.waiting ? 'A training stream is connecting…'
      : 'No training run is streaming right now. The recorded launch replay plays here once it is published.';
  } else if (mode === 'offline') {
    L.label.textContent = 'The 3D view could not load here. The code and the recorded sessions are on GitHub.';
    L.phTitle.textContent = '3D view unavailable';
    L.phSub.textContent = 'the code and the recorded sessions are on GitHub';
    showPlaceholder();
  } else {
    L.label.textContent = st.loading === true ? 'Loading the recorded launch replay…' : 'Connecting to the lab…';
  }
  renderHUD();
}

/* run names are long (brainrig_<utc time>_seed<n>): let them wrap after underscores, not mid-word */
function setRun(el, name) {
  const html = esc(name).replace(/_/g, '_<wbr>');
  if (el.innerHTML !== html) el.innerHTML = html;
  el.title = String(name);
}

function renderHUD() {
  const L = LIVE, st = L.st || {};
  const task = $('#h-task'), run = $('#h-run'), stepsK = $('#h-steps-k'), steps = $('#h-steps');
  const fallsK = $('#h-falls').previousElementSibling, falls = $('#h-falls');
  const loggedK = $('#h-logged').previousElementSibling, logged = $('#h-logged');

  if (L.mode === 'live' || L.mode === 'test') {
    const test = L.mode === 'test';
    const T = TASKS[st.task] || TASKS.lever;
    L.hudMode.textContent = test ? 'test · not live' : 'live · streaming';
    task.textContent = TASKS[st.task] ? T.name : (st.task ? String(st.task) : '—');
    task.title = TASKS[st.task] ? 'Learning to ' + T.what : '';
    setRun(run, st.run || L.run || '—');
    stepsK.textContent = 'Checkpoint';
    steps.textContent = typeof st.steps === 'number' ? fmtSteps(st.steps) + ' steps' : 'waiting for the first save';
    const rows = L.rows;
    L.cReward.set(rows, { key: 'ret', color: COL.gold, fmt: v => num(v, 1), label: 'Reward per attempt', yMin: null, yMax: null, marker: null });
    L.cSuccess.set(rows, { key: T.key, color: COL.pink, fmt: T.fmt, label: T.label, yMin: T.yMax ? null : 0, yMax: T.yMax || null, marker: null });
    const last = rows[rows.length - 1];
    fallsK.textContent = 'Attempts that fell'; falls.textContent = last && typeof last.fall_rate === 'number' ? pct(last.fall_rate) : '—';
    loggedK.textContent = 'Trained so far'; logged.textContent = last ? fmtSteps(last.steps) + ' steps' : '—';
    L.note.textContent = test
      ? 'Test stream, not a live training run: an old run’s saved checkpoint and log, streamed to check the pipeline.'
      : rows.length
        ? 'Charts: this run’s training log, row by row as it is written. The 3D view plays the newest saved checkpoint.'
        : 'Waiting for the first training log row…';
    return;
  }

  // not live: a recorded launch session (the replay), or nothing yet (standby), and the recorded training logs of
  // the two networks the brain uses
  const replay = L.mode === 'replay';
  L.hudMode.textContent = L.mode === 'connecting' ? 'waiting' : replay ? 'recorded · not live' : 'standby';
  task.textContent = replay ? 'Coin launch, 11 steps (recorded)' : 'Coin launch, 11 steps';
  task.title = replay ? 'A replay of a recorded launch session, not live' : '';
  setRun(run, replay && st.run ? String(st.run) : '—');
  stepsK.textContent = 'Brain';
  steps.textContent = 'lever 32.5 M + steering 1.15 M steps';
  // the replay's own progress, from live.js (the target lit now, and how many the rat has clicked)
  const T = replay && L.tgt && L.tgt.total ? L.tgt : null;
  fallsK.textContent = 'Target'; falls.textContent = T && T.n > 0 ? T.n + ' / ' + T.total : '—';
  loggedK.textContent = 'Clicked'; logged.textContent = T ? Math.min(T.done | 0, T.total) + ' / ' + T.total : '—';
  const j = RECORDED && RECORDED.runs;
  if (j) {
    L.cReward.set(j.lever_v3 ? j.lever_v3.rows : [], { key: 'clean_rate', color: COL.gold, fmt: pct, label: 'Lever net · clean presses', yMin: null, yMax: 1, marker: null });
    const sr = j.steer_v1 ? j.steer_v1.rows : [];
    L.cSuccess.set(sr, { key: 'hits', color: COL.pink, fmt: v => num(v, 2), label: 'Steering net · targets per attempt (of 4)', yMin: 0, yMax: null, marker: resumeMarker(sr) });
  }
  L.note.textContent = 'Charts: the recorded training logs of the two networks (not live). Live charts appear here whenever a training run is streaming.';
}

/* live.js onTarget (replay only): {n, total, label, lit, done}; the readout shows it while the replay plays */
function onTarget(t) {
  if (!t || typeof t !== 'object' || !Number.isFinite(t.total)) return;
  const was = LIVE.tgt;
  LIVE.tgt = {n: t.n | 0, total: t.total | 0, done: t.done | 0};
  if (LIVE.mode === 'replay' && (!was || was.n !== LIVE.tgt.n || was.done !== LIVE.tgt.done)) renderHUD();
}

function hidePlaceholder() {
  if (LIVE.phGone) return;
  LIVE.phGone = true; LIVE.ph.classList.add('gone');
  setTimeout(() => { if (LIVE.phGone) LIVE.ph.hidden = true; }, 900);
}
function showPlaceholder() {
  if (!LIVE.phGone) return;
  LIVE.phGone = false; LIVE.ph.hidden = false; LIVE.ph.classList.remove('gone');
  if (LIVE.phKick) LIVE.phKick();
}

/* live.js status: {live, source: 'training'|'test'|'replay'|'none', label, task, run, steps, test?, waiting?, loading?} */
function onStatus(st) {
  if (!st || typeof st !== 'object') return;
  const mode = st.live === true ? 'live'
    : (st.test === true || st.source === 'test') ? 'test'
    : st.source === 'replay' ? 'replay'
    : st.source === 'none' ? (st.loading === true ? 'connecting' : 'standby')
    : st.connecting === true ? 'connecting' : 'replay';
  const streaming = mode === 'live' || mode === 'test';
  if (streaming && st.run) {
    const key = mode + ':' + st.run;
    // a different run (or a test stream after a live one): start its charts from live.js's history of the current
    // hello (live.js clears that history on every hello), not from the rows of the run before it
    if (LIVE.runKey && key !== LIVE.runKey) LIVE.rows = (LIVE.lastWindow || []).slice();
    LIVE.runKey = key; LIVE.run = st.run;
  }
  LIVE.st = st;
  if (streaming && st.metrics && typeof st.metrics.steps === 'number') addRows([st.metrics]);
  if (streaming || mode === 'replay') hidePlaceholder();
  setMode(mode);
}

/* rows arrive as the live run's log history (live.js keeps the newest <= 300); keep a longer history in this tab by
   merging: older rows we already have + the new window. A window that starts before ours is a new run: replace. */
function addRows(rows) {
  const ok = rows.filter(r => r && typeof r === 'object' && typeof r.steps === 'number' && isFinite(r.steps));
  if (!ok.length) return;
  const cur = LIVE.rows;
  if (cur.length && ok[0].steps >= cur[0].steps && ok[ok.length - 1].steps >= cur[cur.length - 1].steps) {
    const first = ok[0].steps;
    LIVE.rows = cur.filter(r => r.steps < first).concat(ok);
  } else LIVE.rows = ok.slice();
  if (LIVE.rows.length > 1500) LIVE.rows = LIVE.rows.slice(-1500);
}

function onMetrics(rows, info) {
  if (!Array.isArray(rows)) return;
  if (info && info.live === false) return;       // the history of a run that is not streaming: never shown as live
  LIVE.lastWindow = rows.filter(r => r && typeof r === 'object' && typeof r.steps === 'number' && isFinite(r.steps));
  addRows(rows);
  if (LIVE.mode === 'live' || LIVE.mode === 'test') renderHUD();
}

/* the placeholder: a slowly turning rat made of dots in the logo ramp (decoration while the 3D view loads) */
(function placeholder() {
  const cv = $('#ph-canvas'); if (!cv) return;
  const cx = cv.getContext('2d'); if (!cx) return;
  const RAMP = [[19, 8, 174], [75, 4, 196], [136, 8, 181], [214, 12, 148], [234, 53, 96], [232, 110, 61], [245, 172, 41], [252, 240, 16], [255, 244, 15]];
  const ramp = t => { t = Math.max(0, Math.min(1, t)) * (RAMP.length - 1); const k = Math.min(RAMP.length - 2, Math.floor(t)), f = t - k, a = RAMP[k], b = RAMP[k + 1];
    return [0, 1, 2].map(j => Math.round(a[j] + (b[j] - a[j]) * f)); };
  // the same dot rat as live/web/brain.html's idle view
  let seed = 2026; const R = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const gauss = () => { let u = 0, v = 0; while (!u) u = R(); while (!v) v = R(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v); };
  const n = 2600, P = new Float32Array(n * 3); let i = 0;
  const put = (x, y, z) => { if (i >= n) return; P[i * 3] = x; P[i * 3 + 1] = y; P[i * 3 + 2] = z; i++; };
  const ell = (x0, y0, z0, rx, ry, rz, k, shell) => { for (let j = 0; j < k; j++) {
    let x = gauss(), y = gauss(), z = gauss(); const d = Math.hypot(x, y, z) || 1; x /= d; y /= d; z /= d;
    const r = shell ? 0.86 + 0.14 * R() : Math.cbrt(R()); put(x0 + x * rx * r, y0 + y * ry * r, z0 + z * rz * r); } };
  ell(-0.05, 0.02, 0, 0.46, 0.24, 0.24, Math.floor(n * .34), true);
  ell(0.44, 0.07, 0, 0.18, 0.14, 0.14, Math.floor(n * .12), true);
  ell(0.64, 0.03, 0, 0.10, 0.06, 0.07, Math.floor(n * .05), true);
  ell(0.40, 0.24, 0.10, 0.06, 0.07, 0.03, Math.floor(n * .03), false);
  ell(0.40, 0.24, -0.10, 0.06, 0.07, 0.03, Math.floor(n * .03), false);
  for (let j = 0, k = Math.floor(n * .16); j < k; j++) { const t = R();
    put(-0.50 - t * 0.85, -0.05 - Math.sin(t * 2.6) * 0.16 + 0.02 * gauss(), 0.25 * Math.sin(t * 1.7) + 0.015 * gauss()); }
  for (let Lg = 0; Lg < 4; Lg++) { const sg = Lg % 2 ? 1 : -1, fore = Lg < 2, bx = fore ? 0.28 : -0.30;
    for (let j = 0, k = Math.floor(n * .03); j < k; j++) { const t = R(); put(bx + (fore ? 0.05 : -0.02) * t, -0.18 - t * 0.24, sg * (0.12 + 0.02 * gauss())); } }
  for (let j = 0; j < 30; j++) put(0.72 + 0.18 * R(), 0.03 + (R() - 0.5) * 0.12, (R() - 0.5) * 0.35);
  while (i < n) put(gauss() * 0.45, gauss() * 0.2, gauss() * 0.2);
  const col = new Array(n), tw = new Float32Array(n);
  for (let j = 0; j < n; j++) { col[j] = ramp((P[j * 3] + 1.35) / 2.25); tw[j] = R() * 6.28; }
  // soft glow sprite
  const spr = document.createElement('canvas'); spr.width = spr.height = 32;
  { const g = spr.getContext('2d'), rg = g.createRadialGradient(16, 16, 0, 16, 16, 16);
    rg.addColorStop(0, 'rgba(255,244,15,.9)'); rg.addColorStop(.35, 'rgba(245,172,41,.35)'); rg.addColorStop(1, 'rgba(232,110,61,0)');
    g.fillStyle = rg; g.fillRect(0, 0, 32, 32); }

  let W = 0, H = 0, d = 1, th = 0.3, visible = true, raf = 0, t0 = performance.now();
  const size = () => { const r = cv.getBoundingClientRect(); d = Math.min(2, devicePixelRatio || 1);
    W = r.width; H = r.height; cv.width = Math.max(2, Math.round(W * d)); cv.height = Math.max(2, Math.round(H * d)); frame(performance.now()); };
  function frame(now) {
    const t = (now - t0) / 1000;
    cx.setTransform(d, 0, 0, d, 0, 0); cx.clearRect(0, 0, W, H);
    const S = Math.min(W * 0.42, H * 0.62), ox = W * 0.52, oy = H * 0.44, c = Math.cos(th), s = Math.sin(th);
    // a faint floor shadow
    const fl = cx.createRadialGradient(ox, oy + S * 0.36, 0, ox, oy + S * 0.36, S * 0.9);
    fl.addColorStop(0, 'rgba(214,12,148,.22)'); fl.addColorStop(1, 'rgba(214,12,148,0)');
    cx.fillStyle = fl; cx.beginPath(); cx.ellipse(ox, oy + S * 0.36, S * 0.9, S * 0.16, 0, 0, 7); cx.fill();
    for (let j = 0; j < n; j++) {
      const x = P[j * 3], y = P[j * 3 + 1], z = P[j * 3 + 2];
      const xr = x * c - z * s, zr = x * s + z * c;
      const px = ox + xr * S, py = oy - y * S + zr * S * 0.18;
      const depth = 0.55 + 0.45 * (zr + 0.5);
      const a = Math.max(0.15, Math.min(1, depth * (0.75 + 0.25 * Math.sin(t * 1.3 + tw[j]))));
      const k = col[j]; cx.fillStyle = 'rgba(' + k[0] + ',' + k[1] + ',' + k[2] + ',' + a.toFixed(2) + ')';
      const sz = 1.3 + depth * 0.9; cx.fillRect(px - sz / 2, py - sz / 2, sz, sz);
    }
    // a few glints near the head (decoration)
    cx.globalCompositeOperation = 'lighter';
    for (let j = 0; j < 14; j++) { const q = (j * 97 + Math.floor(t * 2)) % n; if (P[q * 3] < 0.2) continue;
      const x = P[q * 3], y = P[q * 3 + 1], z = P[q * 3 + 2], xr = x * c - z * s, zr = x * s + z * c;
      const px = ox + xr * S, py = oy - y * S + zr * S * 0.18, r = 9; cx.globalAlpha = 0.35; cx.drawImage(spr, px - r, py - r, 2 * r, 2 * r); }
    cx.globalAlpha = 1; cx.globalCompositeOperation = 'source-over';
  }
  function loop(now) {
    raf = 0; if (LIVE.phGone && LIVE.ph.hidden) return;
    th = 0.3 + 0.5 * Math.sin((now - t0) / 1000 * 0.32);     // a gentle sway around the side view, so it reads as a rat
    frame(now);
    if (visible && !document.hidden) raf = requestAnimationFrame(loop);
  }
  const kick = () => { if (!raf && !RM.matches && visible && !document.hidden && !LIVE.ph.hidden) raf = requestAnimationFrame(loop); };
  if (window.ResizeObserver) new ResizeObserver(size).observe(cv); else addEventListener('resize', size);
  if ('IntersectionObserver' in window) new IntersectionObserver(es => { visible = es[0].isIntersecting; kick(); }).observe(cv);
  document.addEventListener('visibilitychange', kick);
  LIVE.phKick = () => { size(); kick(); };
  size(); kick();
})();

/* ------------------------------------------------------------------ the rat on pons (the relay's pons channel)
   The buy rig (live/buyrig.py) streams the real pons page while the rat clicks through a $LABRAT buy: masked JPEG
   frames (b"PJPG" + JPEG) and messages marked "channel":"pons" (pons_hello / pons_step / pons_result / pons_bye from the
   rig, pons_state / pons_idle from the relay). They come on the same relay socket as the 3D view (live.js hands them
   over); if the 3D view cannot start, or is torn down, this panel opens a socket of its own.
   Rules this panel keeps:
   - hidden until there is something to show: a session on the relay, or a session the buyback engine recorded;
   - every buy is labelled "Simulated" (the rig has no path that signs; only an explicit, all-round LIVE would differ);
   - a test session (local relays only) is shown as a recording, never as live;
   - the copy is fixed here: from the stream only numbers, known target keys, short plain names and fixed status words
     are used, and any text that looks like an address ("0x...") or a balance is dropped. The frames are masked by the
     rig before they leave the rig. */
const PONS = (function ratOnPons() {
  const wrap = $('#rp-wrap');
  const E = { sec: $('#rat-on-pons'), badge: $('#rp-badge'), badgeT: $('#rp-badge-t'), sim: $('#rp-sim'), conn: $('#rp-conn'),
              screen: $('#rp-screen'), cv: $('#rp-canvas'), emptyT: $('#rp-empty-t'), emptyS: $('#rp-empty-s'), cap: $('#rp-cap'),
              now: $('#rp-now'),
              k: $('#rp-k'), target: $('#rp-target'), phase: $('#rp-phase'), steps: $('#rp-steps'), amtK: $('#rp-amt-k'), amt: $('#rp-amt'),
              outK: $('#rp-out-k'), out: $('#rp-out'), checks: $('#rp-checks'), clicks: $('#rp-clicks'), next: $('#rp-next'),
              note: $('#rp-note') };
  const api = { onPons() {}, onFrame() {}, onRelay() {}, onBuyback() {}, ownSocket() {} };
  if (!wrap || !E.cv || !E.cv.getContext || Object.values(E).some(x => !x)) return api;
  // frames are decoded off the main thread (createImageBitmap) and handed to the canvas without a copy
  // (bitmaprenderer); a 2d canvas where that is missing
  const bmr = window.createImageBitmap ? (() => { try { return E.cv.getContext('bitmaprenderer'); } catch (e) { return null; } })() : null;
  const cx = bmr ? null : E.cv.getContext('2d');
  if (!bmr && !cx) return api;

  // the buy rig's targets (live/buyrig.py BUY_TARGETS), in the page's own words
  const TARGET = {
    b01_terms_tou: { name: 'Terms of Use checkbox', kind: 'terms' },
    b02_terms_privacy: { name: 'Privacy Policy checkbox', kind: 'terms' },
    b03_accept: { name: 'Accept and continue', kind: 'accept' },
    b04_amount: { name: 'Amount field', kind: 'amount' },
    b05_buy: { name: 'Buy LABRAT', kind: 'buy' },
    b06_confirm: { name: 'Confirm buy', kind: 'confirm' },
  };
  const own = (o, k) => (typeof k === 'string' && Object.prototype.hasOwnProperty.call(o, k)) ? o[k] : undefined;
  const tkey = k => own(TARGET, k) ? k : '';
  const HIT = { amount: 'The rat clicks it (lever press); the rig types the amount',
                buy: 'The rat clicks it (lever press); pons opens its buy review',
                confirm: 'The rat clicks it (lever press); pons prepares the transaction' };
  const DONE = { terms: 'Ticked', accept: 'Terms accepted', amount: 'Amount entered; pons priced the buy',
                 buy: 'pons shows its buy review', confirm: 'pons prepared the transaction; the rig checked it' };
  // a rig that sends short phase keywords instead ({i, n, target, phase})
  const PHASE = {
    light: 'The rig lights the target', aim: 'Lit: the rat steers the cursor onto it', press: 'The rat clicks it (lever press)',
    miss: 'A press off the target: ignored, nothing reaches the page', type: 'The rig types the amount',
    quote: 'pons prices the buy', review: 'pons shows its buy review', check: 'pons prepared the transaction; the rig checked it',
    done: 'Done',
  };
  const REASON = { checks_failed: 'a transaction check did not pass', simulation_failed: 'the simulation did not go through',
    quote_failed: 'pons could not price the buy', timeout: 'the session ran out of time', aborted: 'the session was stopped' };
  const STALL_MS = 8000;              // live, but no frame for this long: say so on the picture
  const BB_STALE_S = 90;              // the buyback status is rewritten every 10 s; older = not running (as its panel says)
  const NOTE = /^[a-z][a-z ;.-]{0,59}$/;
  const obj = v => (v && typeof v === 'object' && !Array.isArray(v)) ? v : null;
  const unsafe = s => /0x/i.test(s) || /\b(available|balance|test|dry)\b/i.test(s);
  const name = s => (typeof s === 'string' && /^[A-Za-z][A-Za-z0-9 ()&'.,:+-]{0,47}$/.test(s.trim()) && !unsafe(s)) ? s.trim() : null;
  const dec = s => (typeof s === 'string' && /^\d{1,15}(\.\d{1,18})?$/.test(s)) ? s : null;
  const int = (v, lo, hi) => (Number.isInteger(v) && v >= lo && v <= hi) ? v : null;
  const iso = s => (typeof s === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?Z$/.test(s)) ? s : null;
  const hhmm = s => { const t = iso(s) ? Date.parse(s) : NaN; return Number.isFinite(t) ? new Date(t).toISOString().slice(11, 16) + ' UTC' : ''; };
  const fmtTok = s => { const d = dec(s); if (d === null) return null; const n = Number(d);
    return n >= 100 ? Math.round(n).toLocaleString('en-US') : n.toLocaleString('en-US', { maximumFractionDigits: 2 }); };
  const isTest = h => !!h && (h.test === true || /^\s*TEST\b/.test(String(h.label || '')));
  const isPonsFrame = b => b instanceof ArrayBuffer && b.byteLength >= 8 &&
    (v => v[0] === 0x50 && v[1] === 0x4A && v[2] === 0x50 && v[3] === 0x47)(new Uint8Array(b, 0, 4));
  // every string this panel writes goes through here: nothing address-like ever reaches the page
  const put = (el, s) => { const t = String(s == null ? '' : s).replace(/0x[0-9a-f.…]*/gi, '').replace(/\s{2,}/g, ' ').trim();
    if (el.textContent !== t) el.textContent = t; };

  const S = { open: false, wasOpen: false, live: false, hello: null, step: null, result: null, bb: null, bbPons: null, skipped: new Set(),
              frameAt: 0, hasFrame: false, decoding: false, pending: null, gen: 0, own: false };

  /* ---- the rig's messages, read into plain facts */
  function targetsOf(h) {
    const list = h && Array.isArray(h.targets) ? h.targets.slice(0, 12) : [];
    return list.map((t, k) => {
      if (typeof t === 'string') return { n: k + 1, key: '', name: name(t) };
      const o = obj(t);
      if (!o) return null;
      const key = tkey(o.key);
      return { n: int(o.n, 1, 99) || k + 1, key, name: key ? TARGET[key].name : name(o.label) };
    }).filter(t => t && t.name);
  }
  function stepOf(st, total) {
    if (!st) return null;
    if (typeof st.state === 'string') {         // live/buyrig.py: {n, key, label, state, detail}
      const key = tkey(st.key);
      const kind = key ? TARGET[key].kind : '';
      const detail = typeof st.detail === 'string' ? st.detail : '';
      let phase = '';
      if (st.state === 'active') phase = /^lit\b/.test(detail) ? 'Lit: the rat steers the cursor onto it'
        : /^again\b/.test(detail) ? 'The rig lights it again' : 'The rig lights the target';
      else if (st.state === 'hit') phase = HIT[kind] || 'The rat clicks it (lever press)';
      else if (st.state === 'done') phase = /no terms dialog/.test(detail) ? 'Not needed this session' : (DONE[kind] || 'Done');
      else if (st.state === 'failed') phase = 'This step did not complete';
      return { i: int(st.n, 1, 99), total: total || null, name: key ? TARGET[key].name : name(st.label), phase,
               done: st.state === 'done', key };
    }
    return { i: int(st.i, 1, 99), total: int(st.n, 1, 99) || total || null, name: name(st.target),
             phase: own(PHASE, st.phase) || '', done: st.phase === 'done', key: '' };
  }
  function resultOf(r, h) {
    if (!r) return null;
    const status = typeof r.outcome === 'string' ? r.outcome : typeof r.status === 'string' ? r.status : '';
    const ok = r.ok === true || status === 'checked, not signed';
    const cp = int(r.checks_passed, 0, 100), ct = int(r.checks_total, 1, 100);
    return {
      ok, final: r.kind !== 'tx',
      eth: dec(r.amount_eth) || dec(r.eth_in) || dec(h && h.amount_eth), out: fmtTok(r.labrat_out),
      checks: cp !== null && ct !== null ? cp + ' of ' + ct + ' passed' : '', simOk: r.simulation === 'ok',
      hits: int(r.targets_hit, 0, 99), misses: int(r.misses, 0, 100000), secs: Number.isFinite(r.seconds) ? r.seconds : null,
      executed: r.sent === true && r.signed === true,
      why: status === 'refused' ? 'a transaction check did not pass' : status === 'stopped' ? 'the session stopped before the buy'
        : own(REASON, r.reason) || 'the session ended without a buy',
    };
  }
  // the latest simulated buy the buyback engine recorded as clicked by the rat on pons (when the relay has none)
  function bbPonsOf(j) {
    const b = obj(j && j.buys), recent = b && Array.isArray(b.recent) ? b.recent : [];
    for (const e of recent) {
      const p = obj(e) && obj(e.pons);
      if (p && p.clicked_by_rat === true) {
        const m = typeof p.checks === 'string' && /^(\d{1,3})\/(\d{1,3})$/.exec(p.checks);
        return { at: iso(p.at), eth: dec(p.eth_in), out: fmtTok(p.labrat_out), hits: int(p.targets_hit, 0, 99),
                 misses: int(p.misses, 0, 100000), checks: m ? m[1] + ' of ' + m[2] + ' passed' : '' };
      }
    }
    return null;
  }

  function simulated() {
    const h = S.hello, r = S.result, bb = S.bb;
    return !(h && h.simulated === false && h.label !== 'Simulated' && r && r.sent === true && r.signed === true &&
             bb && bb.mode === 'LIVE');
  }
  function buyLine(sim, eth, out) {
    return (sim ? 'Simulated buy' : 'Buy') + (eth ? ' · ' + eth + ' ETH' : '') + (out ? ' → ' + out + ' LABRAT' : '');
  }
  function clicksText(hits, misses) {
    if (hits === null) return '';
    return hits + ' on target' + (misses !== null ? ' · ' + misses + ' off' : '');
  }
  function nextLine() {
    const j = S.bb;
    if (!j || j.mode === 'LIVE' || (typeof j.stopped === 'string' && j.stopped)) return '';
    const upd = typeof j.updated === 'string' ? Date.parse(j.updated) : NaN;
    const age = Number.isFinite(upd) ? Math.max(0, (Date.now() - upd) / 1000) : Infinity;
    if (age > BB_STALE_S) return '';
    const next = int(j.next_buy_in_s, 0, 1e7);
    const why = typeof j.next_buy_note === 'string' && NOTE.test(j.next_buy_note) ? j.next_buy_note : '';
    if (next !== null) {
      const left = next - age;
      return left < 60 ? 'Next buyback session starting shortly' : 'Next buyback session in about ' + Math.round(left / 60) + ' min';
    }
    if (why && why !== 'waiting for hits') return 'No buyback session scheduled right now (' + why + ')';
    return 'The next buyback session starts once enough hits are pending';
  }

  function render() {
    const h = S.hello, test = isTest(h), live = S.open && S.live && !!h, sim = simulated();
    const targets = targetsOf(h);
    const st = stepOf(S.step, targets.length), res = resultOf(S.result, h);
    const stalled = live && S.hasFrame && performance.now() - S.frameAt > STALL_MS;
    const state = !S.open ? 'connecting' : live ? (test ? 'replay' : 'live') : 'standby';
    E.sec.dataset.state = state;
    E.badge.className = 'badge ' + ({ connecting: 'conn', live: 'live', replay: 'replay static', standby: 'off static' }[state]);
    put(E.badgeT, { connecting: 'CONNECTING', live: 'LIVE', replay: 'RECORDED', standby: 'STANDBY' }[state]);
    E.sim.hidden = !sim;
    put(E.conn, !S.open ? (S.wasOpen ? 'reconnecting' : 'connecting') : live ? (test ? 'recorded session' : 'session in progress')
      : (h || S.bbPons) ? 'between sessions' : '');
    E.screen.classList.toggle('idle', !live);

    // the picture: its caption, or what stands in for it
    let cap = '';
    if (S.hasFrame && !live && h) cap = 'Final frame of the last session';
    else if (stalled) cap = 'Waiting for the next frame';
    else if (live && test) cap = 'Recorded session';
    put(E.cap, cap); E.cap.hidden = !cap;
    if (!S.open && !S.hasFrame) { put(E.emptyT, 'Connecting'); put(E.emptyS, ''); }
    else if (live) { put(E.emptyT, 'Opening the $LABRAT page on pons'); put(E.emptyS, 'the first frame appears in a moment'); }
    else { put(E.emptyT, 'The next session streams here'); put(E.emptyS, 'live, as the rat clicks through the buy'); }

    // the session: where the rat is, or how it ended
    const bbp = !h ? S.bbPons : null;
    let k = 'Buy session', target = '—', phase = '';
    if (live) {
      k = test ? 'Recorded buy session' : 'Buy session in progress';
      if (res && res.ok) {
        target = buyLine(sim, res.eth, res.out);
        phase = res.final ? 'Session complete' : 'pons built the transaction; the rig checked it' +
          (res.simOk ? ' and simulated it on the live chain' : '');
      } else if (res) target = 'No buy this session: ' + res.why;
      else if (st && (st.i || st.name)) {
        target = (st.i && st.total ? 'Target ' + st.i + ' of ' + st.total : 'Target') + (st.name ? ' · ' + st.name : '');
        phase = st.phase;
      } else target = 'Opening the $LABRAT page on pons';
    } else if (h) {
      k = 'Last session' + (hhmm(h.started) ? ' · ' + hhmm(h.started) : '');
      target = res ? (res.ok ? buyLine(sim, res.eth, res.out) : 'No buy this session: ' + res.why) : 'The session ended before a result';
    } else if (bbp) {
      k = 'Last session' + (hhmm(bbp.at) ? ' · ' + hhmm(bbp.at) : '');
      target = buyLine(true, bbp.eth, bbp.out);
    }
    put(E.k, k); put(E.target, target); put(E.phase, phase);
    // on narrow screens (CSS) the current step also rides on the picture
    const now = live && S.hasFrame ? (res && res.ok ? buyLine(sim, res.eth, res.out)
      : st && st.i && st.total ? st.i + '/' + st.total + (st.name ? ' · ' + st.name : '') : '') : '';
    put(E.now, now); E.now.hidden = !now;
    E.target.classList.toggle('res', !!(res && res.ok && (live || h)) || !!bbp);

    // the session's targets: done / now / to come
    const cur = res && res.ok ? 99 : st && st.i ? st.i : 0;
    const html = targets.map(t => {
      const skip = t.key && S.skipped.has(t.key);
      const cls = t.n < cur || (t.n === cur && (!live || (st && st.done))) ? 'done' : t.n === cur ? 'on' : '';
      return '<li class="' + cls + (skip ? ' skip' : '') + '">' + esc(t.name) + (skip ? ' <em>not needed</em>' : '') + '</li>';
    }).join('');
    if (E.steps.innerHTML !== html) E.steps.innerHTML = html;

    // figures
    const amt = dec(h && h.amount_eth) || (res && res.eth) || (bbp && bbp.eth);
    const hits = h && obj(h.batch) ? int(h.batch.hits, 1, 1e6) : null;
    put(E.amtK, 'Buy size' + (hits ? ' · ' + hits + ' hits' : ''));
    put(E.amt, amt ? amt + ' ETH' : '—');
    put(E.outK, sim ? 'LABRAT (simulated)' : 'LABRAT bought');
    put(E.out, res && res.ok && res.out ? res.out : bbp && bbp.out ? bbp.out : live && !res ? 'pending' : '—');
    put(E.checks, res && res.checks ? res.checks : bbp && bbp.checks ? bbp.checks : live ? 'pending' : '—');
    put(E.clicks, (res && res.final && clicksText(res.hits, res.misses)) || (bbp && clicksText(bbp.hits, bbp.misses)) ||
      (live ? 'pending' : '—'));

    const nx = live ? '' : nextLine();
    put(E.next, nx); E.next.hidden = !nx;
    put(E.note, (test ? 'A recorded session. ' : '') +
      (sim ? 'Simulated: every click happens on the real pons page. pons builds the buy transaction; it is checked and ' +
             'simulated on the live chain, and not signed or sent.'
           : 'Every click happens on the real pons page, and the transaction is checked before it is signed.'));
  }

  function show() {
    if (wrap.hidden && (S.hello || S.hasFrame || S.bbPons)) wrap.hidden = false;
  }
  function clearScreen() {
    S.hasFrame = false; S.pending = null; S.gen++;     // a frame still decoding belongs to the old session: dropped
    E.screen.classList.remove('has-frame');
    try { if (bmr) bmr.transferFromImageBitmap(null); else cx.clearRect(0, 0, E.cv.width, E.cv.height); } catch (e) { /* hidden anyway */ }
  }

  function onPons(m) {
    if (!m || typeof m !== 'object' || typeof m.type !== 'string') return;
    switch (m.type) {
      case 'pons_state': {
        const h = obj(m.hello);
        const was = S.hello;
        S.hello = h && h.source === 'buyrig' ? h : null;
        if (!S.hello || !was || was.session !== S.hello.session || was.started !== S.hello.started) S.skipped.clear();
        S.step = S.hello ? obj(m.step) : null; S.result = S.hello ? obj(m.result) : null;
        S.live = m.live === true && !!S.hello;
        break;
      }
      case 'pons_hello': {
        if (m.source !== 'buyrig') return;
        const same = S.hello && S.hello.session === m.session && S.hello.started === m.started;
        if (!same) { S.step = S.result = null; S.skipped.clear(); clearScreen(); }
        S.hello = m; S.live = true;
        break;
      }
      case 'pons_step':
        if (!S.hello) return;
        S.step = m; S.live = true;
        if (m.state === 'done' && typeof m.key === 'string' && /no terms dialog/.test(String(m.detail || ''))) S.skipped.add(m.key);
        break;
      case 'pons_result': if (!S.hello) return; S.result = m; break;
      case 'pons_bye': case 'pons_idle': S.live = false; break;
      default: return;
    }
    show(); render();
  }

  const loadImg = blob => new Promise((ok, no) => {
    const u = URL.createObjectURL(blob), im = new Image();
    im.onload = () => { URL.revokeObjectURL(u); ok(im); };
    im.onerror = () => { URL.revokeObjectURL(u); no(new Error('bad frame')); };
    im.src = u;
  });
  async function decodeNext() {
    const buf = S.pending; S.pending = null;
    if (!buf) return;
    S.decoding = true;
    const gen = S.gen;
    try {
      const blob = new Blob([new Uint8Array(buf, 4)], { type: 'image/jpeg' });
      const img = window.createImageBitmap ? await createImageBitmap(blob) : await loadImg(blob);
      const w = img.width, hgt = img.height;
      if (gen === S.gen && w > 0 && hgt > 0 && w <= 4096 && hgt <= 4096) {
        if (E.cv.width !== w || E.cv.height !== hgt) { E.cv.width = w; E.cv.height = hgt; E.screen.style.aspectRatio = w + ' / ' + hgt; }
        if (bmr) bmr.transferFromImageBitmap(img); else cx.drawImage(img, 0, 0);
        if (!S.hasFrame) { S.hasFrame = true; E.screen.classList.add('has-frame'); show(); render(); }
      }
      if (img.close) img.close();       // (a transferred bitmap is already detached: harmless)
    } catch (e) { /* an undecodable frame: skip it */ }
    S.decoding = false;
    if (S.pending && !document.hidden) decodeNext();
  }
  function onFrame(buf) {
    if (!isPonsFrame(buf) || buf.byteLength > 300 * 1024) return;
    S.frameAt = performance.now();
    S.pending = buf;                  // only the newest waits; an older undecoded one is dropped
    if (!S.decoding && !document.hidden) decodeNext();   // a hidden tab decodes nothing; the newest waits
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden && S.pending && !S.decoding) decodeNext(); });
  function onRelay(open) {
    S.open = !!open;
    if (S.open) S.wasOpen = true;
    if (!S.open) S.live = false;
    render();
  }
  function onBuyback(j) {
    S.bb = obj(j);
    S.bbPons = bbPonsOf(S.bb);
    show();
    if (!wrap.hidden) render();
  }
  // the 3D view could not start, or was torn down (so live.js holds no relay socket): a socket for this panel alone
  function ownSocket() {
    const url = window.LABRAT_RELAY;
    if (S.own || typeof url !== 'string' || !/^wss?:\/\//i.test(url)) return;
    S.own = true;
    let wait = 2000;
    const open = () => {
      let ws;
      try { ws = new WebSocket(url); } catch (e) { setTimeout(open, wait); return; }
      ws.binaryType = 'arraybuffer';
      ws.onopen = () => { wait = 2000; onRelay(true); };
      ws.onmessage = ev => {
        if (typeof ev.data !== 'string') { onFrame(ev.data); return; }
        if (ev.data.length > 65536) return;
        let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
        if (m && typeof m === 'object' && m.channel === 'pons') onPons(m);
      };
      ws.onclose = () => { onRelay(false); setTimeout(open, wait); wait = Math.min(wait * 2, 30000); };
      ws.onerror = () => { try { ws.close(); } catch (e) { /* ignore */ } };
    };
    open();
  }
  setInterval(() => { if (!wrap.hidden && !document.hidden) render(); }, 2000);
  render();
  return { onPons, onFrame, onRelay, onBuyback, ownSocket };
})();

/* ------------------------------------------------------------------ rat buybacks (buyback.js)
   Reads the buyback engine's public status JSON. Every figure carries its mode: anything not executed on-chain is
   labelled "Simulated" and never called a buy; a test stream is never called live; a stale status is shown as stale;
   no address-like string is ever shown. */
(function buybacks() {
  const C = window.LABRAT_BUYBACK, wrap = $('#bb-wrap');
  if (!wrap || !C || typeof C !== 'object' || C.enabled !== true || typeof C.statusUrl !== 'string' || !C.statusUrl) return;
  let url;
  try { url = new URL(C.statusUrl, location.href); } catch (e) { return; }
  const localHost = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
  if (!(url.protocol === 'https:' || (url.protocol === 'http:' && localHost))) {
    console.warn('labrat: LABRAT_BUYBACK.statusUrl must be https (or http on localhost)'); return;
  }
  const ADDR = /0x[0-9a-fA-F]{40}/;
  const txt = s => (typeof s === 'string' && !ADDR.test(s)) ? s : null;
  const dec = s => (typeof s === 'string' && /^\d{1,15}(\.\d{1,18})?$/.test(s)) ? s : null;
  const int = v => (Number.isInteger(v) && v >= 0) ? v : null;
  const fmtTok = s => { const d = dec(s); if (d === null) return '—'; const n = Number(d);
    return n >= 100 ? Math.round(n).toLocaleString('en-US') : n.toLocaleString('en-US', { maximumFractionDigits: 2 }); };
  const fmtEth = s => { const d = dec(s); return d === null ? '—' : d + ' ETH'; };
  const E = { mode: $('#bb-mode'), test: $('#bb-test'), conn: $('#bb-conn'), per: $('#bb-per'), lever: $('#bb-lever'),
              hits: $('#bb-hits'), pending: $('#bb-pending'), buysK: $('#bb-buys-k'), buys: $('#bb-buys'),
              outK: $('#bb-out-k'), out: $('#bb-out'), split: $('#bb-split'), list: $('#bb-list'), note: $('#bb-note') };
  const STALE_S = 90;          // the engine rewrites its status at least every 10 s; older than this = not running
  const NOTE = /^[a-z][a-z ;.-]{0,59}$/;   // next_buy_note: a short fixed phrase from the engine
  wrap.hidden = false;

  function render(j) {
    const live = j.mode === 'LIVE';
    const DRY = 'Simulated';
    E.mode.textContent = live ? 'LIVE' : DRY;
    E.mode.classList.toggle('dry', !live);
    const rl = j.relay || {}, src = j.source || {};
    // a TEST stream, test streams accepted, another relay, or an engine too old to say: never shown as live hits
    const test = src.test !== false || src.public_relay !== true || src.accept_test_streams === true ||
      rl.test_stream === true;
    E.test.hidden = !test;
    const upd = typeof j.updated === 'string' ? Date.parse(j.updated) : NaN;
    const age = Number.isFinite(upd) ? (Date.now() - upd) / 1000 : Infinity;
    const stale = age > STALE_S;
    const per = dec(j.per_hit_eth); E.per.textContent = per ? per + ' ETH' : 'a tiny amount of ETH';
    E.lever.hidden = !(Array.isArray(j.tasks) && j.tasks.includes('lever'));
    const h = j.hits || {}, p = j.pending || {}, b = j.buys || {};
    E.hits.textContent = int(h.counted) !== null ? h.counted.toLocaleString('en-US') : '—';
    const fmtN = v => v.toLocaleString('en-US');
    E.split.textContent = [int(h.in_buys) !== null ? fmtN(h.in_buys) + ' hits in ' + (live ? '' : 'simulated ') + 'buys' : '',
      int(h.pending) !== null ? fmtN(h.pending) + ' pending' : '',
      int(h.over_caps) !== null ? fmtN(h.over_caps) + ' over the cap' : '']
      .filter(Boolean).join(' · ');
    E.pending.textContent = fmtEth(p.eth) + (int(p.hits) !== null ? ' · ' + p.hits + ' hits' : '');
    const simulated = !live || b.simulated !== false;
    E.buysK.textContent = simulated ? 'Simulated buys' : 'Buys';
    E.outK.textContent = simulated ? 'LABRAT (simulated)' : 'LABRAT bought';
    E.buys.textContent = int(b.count) !== null ? b.count + (dec(b.eth_in) ? ' · ' + b.eth_in + ' ETH' : '') : '—';
    E.out.textContent = fmtTok(b.labrat_out);
    const recent = Array.isArray(b.recent) ? b.recent.slice(0, 5) : [];
    E.list.innerHTML = recent.map(r => {
      if (!r || typeof r !== 'object') return '';
      const sim = !live || r.simulated !== false;
      const at = txt(r.at) && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(r.at) ? r.at.slice(11, 16) + ' UTC' : '';
      const venue = r.venue === 'pool' ? 'pool' : r.venue === 'curve' ? 'curve' : '';
      return '<li><span class="bb-t">' + esc(at) + '</span><span>' + (sim ? 'simulated buy' : 'buy') + ': ' +
        esc(fmtEth(r.eth_in)) + ' &rarr; ' + esc(fmtTok(r.labrat_out)) + ' LABRAT' +
        (int(r.hits_covered) !== null ? ' <em>(' + r.hits_covered + ' hits)</em>' : '') +
        (venue ? ' <em>' + venue + '</em>' : '') +
        (sim && r.pons && typeof r.pons === 'object' && r.pons.clicked_by_rat === true ? ' <em>clicked by the rat on pons</em>' : '') +
        '</span></li>';
    }).join('');
    const stopped = txt(j.stopped);
    const counting = rl.counting === true && !stale;
    E.conn.textContent = stale ? 'status stale' : stopped ? 'buys stopped'
      : counting ? (test ? 'counting a test stream' : 'counting live hits')
      : rl.connected === true ? 'waiting for live training' : 'not connected';
    E.conn.className = 'bb-conn' + (stale || stopped ? ' off' : counting && !test ? ' on' : '');
    const next = int(j.next_buy_in_s);
    const why = typeof j.next_buy_note === 'string' && NOTE.test(j.next_buy_note) ? j.next_buy_note : '';
    const sim = simulated ? 'simulated ' : '';
    let nextTxt = '';
    if (!stopped && !stale) {
      if (next === null) nextTxt = why && why !== 'waiting for hits' ? 'No ' + sim + 'buy for now: ' + why + '. ' : '';
      else if (next === 0) nextTxt = 'Next ' + sim + 'buy due now. ';
      else nextTxt = 'Next ' + sim + 'buy in about ' + Math.max(1, Math.round(next / 60)) + ' min' + (why && why !== 'due' ? ' (' + why + ')' : '') + '. ';
    }
    const updTxt = Number.isFinite(upd) ? new Date(upd).toISOString().slice(11, 16) + ' UTC' : 'an unknown time';
    E.note.textContent = (stale ? 'This status has not updated since ' + updTxt + ': the counter may not be running. ' : '') +
      (test ? 'These hits come from a test stream. ' : '') +
      (stopped ? 'Buys stopped: ' + stopped + '. ' : '') +
      (simulated ? 'Buybacks shown are simulated against the live chain. ' : '') + nextTxt +
      'Hits are counted as each training attempt ends.';
  }

  let timer = 0;
  async function poll() {
    timer = 0;
    try {
      const r = await fetch(url.href, { cache: 'no-store', credentials: 'omit' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const j = await r.json();
      if (!j || typeof j !== 'object') throw new Error('bad status');
      render(j);
      PONS.onBuyback(j);                 // the rat-on-pons panel's "next session" line
    } catch (e) {
      E.conn.textContent = 'status unavailable'; E.conn.className = 'bb-conn off';
    }
    if (!document.hidden) timer = setTimeout(poll, 15000);
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden && !timer) poll(); });
  poll();
})();

/* ------------------------------------------------------------------ mount the 3D viewer (js/live.js) */
setMode('connecting');
recordedReady.then(() => renderHUD());

(async function mountViewer() {
  const el = $('#live-view');
  let mod = null;
  try { mod = await import('./live.js'); }
  catch (e) { console.warn('labrat: live viewer unavailable -', e && e.message ? e.message : e); }
  if (!mod || typeof mod.mountLive !== 'function') { LIVE.failed = true; setMode('offline'); PONS.ownSocket(); return; }
  try {
    // mountLive never throws: it resolves to a handle after the first frame, or to null (and draws its own
    // fallback line) when the 3D view cannot start
    const handle = await mod.mountLive(el, {
      relay: window.LABRAT_RELAY,
      loader: false,             // the dot-rat placeholder (#live-ph) is this page's one loading message
      onStatus: s => { try { onStatus(s); } catch (e) { console.warn('labrat: onStatus', e); } },
      onMetrics: (r, info) => { try { onMetrics(r, info); } catch (e) { console.warn('labrat: onMetrics', e); } },
      onTarget: t => { try { onTarget(t); } catch (e) { console.warn('labrat: onTarget', e); } },
      // the relay's pons channel rides on the same socket; live.js ignores it and hands it to the pons panel
      onPons: m => PONS.onPons(m),
      onPonsFrame: b => PONS.onFrame(b),
      onRelay: (open, gone) => { PONS.onRelay(open); if (gone) PONS.ownSocket(); },
    });
    // null: the 3D view could not start here; the dot-rat placeholder stays up with the offline message
    if (!handle) { LIVE.failed = true; setMode('offline'); PONS.ownSocket(); return; }
    // if the viewer draws but never reports a status, do not leave the placeholder over it
    setTimeout(() => { if (!LIVE.phGone && !LIVE.failed) hidePlaceholder(); }, 5000);
  } catch (e) {
    console.warn('labrat: live viewer failed to start -', e && e.message ? e.message : e);
    LIVE.failed = true; setMode('offline'); PONS.ownSocket();
  }
})();
