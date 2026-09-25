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
  // Rat Tiles: log rows carry "hits" = tiles hit per attempt (as the cursor tasks' targets)
  tiles: { name: 'Rat Tiles', what: 'tap the falling tiles with a lever press, in time, to play a melody',
           key: 'hits', label: 'Tiles hit per attempt', fmt: v => num(v, 1) },
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
  try { TILES.onStatus(st); } catch (e) { console.warn('labrat: tiles status', e); }
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
   - every buy is labelled "Simulated", except a LIVE buy that was executed: the rig's session says signed and sent
     (not "Simulated") and the engine is LIVE, or the engine verified it on chain (state executed, a tx hash);
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
    quote_failed: 'pons could not price the buy', timeout: 'the session ran out of time', aborted: 'the session was stopped',
    balance_low: 'the buyback wallet could not cover the buy', not_settled: 'the buy did not go through on chain' };
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
  // the latest buy the buyback engine recorded as clicked by the rat on pons (when the relay has none): a simulated
  // one, or (LIVE) one the engine verified on chain (state executed, a transaction hash); anything else is simulated
  function bbPonsOf(j) {
    const b = obj(j && j.buys), recent = b && Array.isArray(b.recent) ? b.recent : [];
    const live = !!j && j.mode === 'LIVE';
    for (const e of recent) {
      const p = obj(e) && obj(e.pons);
      if (p && p.clicked_by_rat === true) {
        const m = typeof p.checks === 'string' && /^(\d{1,3})\/(\d{1,3})$/.exec(p.checks);
        const executed = live && e.simulated === false && e.state === 'executed' && /^0x[0-9a-f]{64}$/.test(e.tx || '');
        return { at: iso(p.at) || iso(e.at), eth: dec(p.eth_in), out: fmtTok(p.labrat_out), hits: int(p.targets_hit, 0, 99),
                 misses: int(p.misses, 0, 100000), checks: m ? m[1] + ' of ' + m[2] + ' passed' : '', sim: !executed };
      }
    }
    return null;
  }

  function simulated() {
    const h = S.hello, r = S.result, bb = S.bb;
    if (!h && S.bbPons) return S.bbPons.sim;          // no session on the relay: the engine's record says which
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
    if (!j || (typeof j.stopped === 'string' && j.stopped)) return '';
    const upd = typeof j.updated === 'string' ? Date.parse(j.updated) : NaN;
    const age = Number.isFinite(upd) ? Math.max(0, (Date.now() - upd) / 1000) : Infinity;
    if (age > BB_STALE_S) return '';
    // one session an hour, on the hour (UTC): next_buy_at from the engine, else its older seconds-to-go
    const at = iso(j.next_buy_at) ? Date.parse(j.next_buy_at) : NaN;
    if (Number.isFinite(at)) {
      const left = (at - Date.now()) / 1000;
      return left < 60 ? 'Next buyback session starting shortly'
        : 'Next buyback session at ' + hhmm(j.next_buy_at) + ' (in about ' + Math.round(left / 60) + ' min)';
    }
    const next = int(j.next_buy_in_s, 0, 1e7);
    const why = typeof j.next_buy_note === 'string' && NOTE.test(j.next_buy_note) ? j.next_buy_note : '';
    if (next !== null) {
      const left = next - age;
      return left < 60 ? 'Next buyback session starting shortly' : 'Next buyback session in about ' + Math.round(left / 60) + ' min';
    }
    if (why && why !== 'waiting for hits') return 'No buyback session scheduled right now (' + why + ')';
    return 'Buyback sessions run once an hour, on the hour (UTC)';
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
      target = buyLine(bbp.sim, bbp.eth, bbp.out);
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
    const bt = h && obj(h.batch), hr = bt && typeof bt.hit_rate === 'number' && bt.hit_rate >= 0 && bt.hit_rate <= 1 ? bt.hit_rate : null;
    const hits = bt ? int(bt.hits, 1, 1e6) : null;
    put(E.amtK, 'Buy size' + (hr !== null ? ' · hit rate ' + (hr * 100).toFixed(hr === 1 || hr === 0 ? 0 : 1) + '%'
      : hits ? ' · ' + hits + ' hits' : ''));
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
  // the 3D view could not start, or was torn down (so live.js holds no relay socket): a socket of the page's own.
  // other: {onText(m), onRelay(open)} for the page's other panels (Rat Tiles reads the training channel's texts)
  function ownSocket(other) {
    const url = window.LABRAT_RELAY;
    if (S.own || typeof url !== 'string' || !/^wss?:\/\//i.test(url)) return;
    S.own = true;
    const o = other && typeof other === 'object' ? other : {};
    const call = (f, x) => { try { if (typeof f === 'function') f(x); } catch (e) { console.warn('labrat: socket hook', e); } };
    let wait = 2000;
    const open = () => {
      let ws;
      try { ws = new WebSocket(url); } catch (e) { setTimeout(open, wait); return; }
      ws.binaryType = 'arraybuffer';
      ws.onopen = () => { wait = 2000; onRelay(true); call(o.onRelay, true); };
      ws.onmessage = ev => {
        if (typeof ev.data !== 'string') { onFrame(ev.data); return; }
        if (ev.data.length > 65536) return;
        let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
        if (!m || typeof m !== 'object') return;
        if (m.channel === 'pons') onPons(m); else call(o.onText, m);
      };
      ws.onclose = () => { onRelay(false); call(o.onRelay, false); call(o.onText, { type: 'idle' }); setTimeout(open, wait); wait = Math.min(wait * 2, 30000); };
      ws.onerror = () => { try { ws.close(); } catch (e) { /* ignore */ } };
    };
    open();
  }
  setInterval(() => { if (!wrap.hidden && !document.hidden) render(); }, 2000);
  render();
  return { onPons, onFrame, onRelay, onBuyback, ownSocket };
})();

/* ------------------------------------------------------------------ Rat Tiles: R-01 plays piano
   A training run whose hello says task "tiles" also streams, on the relay socket the 3D view uses (live.js hands them
   over through onTiles; the relay replays the newest snapshot to a late joiner):
     {"type":"tiles","t","song","speed","lanes":4,"cursor":[x,y],"tiles":[[id,lane,y,h,state],...],"note_i",
      "hit_y","window"}
         about 8 a second; x, y and h in screen units (0..1, y down; y is a tile's centre); state "up"|"hit"|"miss";
         speed in screen heights per second; the lanes split the screen's width equally. hit_y is the line of red
         buttons near the bottom and window the half-height of the timing window around it (0.86 / 0.06 when absent).
     {"type":"tile","id","lane","result":"hit"|"miss"|"wrong","note_i","song","timing"}   once per tile outcome
   The rule: the cursor's x (where R-01's head points) picks the lane; a lever press presses that lane's button. It is
   a hit only while a tile is on the hit line in that lane; a press with no tile on the line there is "wrong"; a tile
   that slides past the line untapped is a "miss". timing: seconds early (-) or late (+) of perfect, for hits.
   This panel draws that screen as a runway in perspective (a drawing of the game's state: every tile, the cursor's
   lane and each outcome come from the stream; the rat on the runway is the labrat logo, standing in the cursor's
   lane), a quarter of a second behind the stream, interpolating between snapshots, and keeps the score.
   Sound: nothing is created until the viewer taps "Tap to hear R-01 play" (no AudioContext exists before that tap).
   After it, each hit plays that tile's note of the song (assets/songs.json: public-domain melodies) with a piano-like
   tone made in the browser (WebAudio oscillators and a short noise knock; no recordings).
   Honesty: LIVE only when live.js reports a live training run; a test stream is labelled as one; every value from the
   stream is checked before it is drawn or written. The home page only has the teaser (#rt-teaser). */
const TILES = (function ratTiles() {
  const teaser = $('#rt-teaser'), teaserSt = $('#rt-teaser-st');
  const sec = $('#piano');
  const E = sec ? { badge: $('#rt-badge'), badgeT: $('#rt-badge-t'), conn: $('#rt-conn'), screen: $('#rt-screen'),
    cv: $('#rt-canvas'), emptyT: $('#rt-empty-t'), emptyS: $('#rt-empty-s'), cap: $('#rt-cap'), songK: $('#rt-song-k'),
    song: $('#rt-song'), comp: $('#rt-composer'), pd: $('#rt-pd'), notes: $('#rt-notes'), notesT: $('#rt-notes-t'),
    hits: $('#rt-hits'), miss: $('#rt-miss'), rate: $('#rt-rate'), best: $('#rt-best'), sub: $('#rt-sub'),
    listen: $('#rt-listen'), mute: $('#rt-mute'), bb: $('#rt-bb') } : null;
  const cx = E && Object.values(E).every(Boolean) && E.cv.getContext ? E.cv.getContext('2d') : null;
  const panel = !!cx;

  const DELAY = 0.25;        // s: the board plays this far behind the newest snapshot (the stream's jitter buffer)
  const STALE_S = 4;         // no snapshot for this long: the board stops (the run may be between attempts)
  const LATE_S = 0.6;        // a hit due longer ago than this makes no sound (a hidden tab catching up)
  const BEAT_S = 0.6;        // one beat of a melody: how long a note's key is held (at least 0.8 s: the rat is slow)
  const MAX_LANES = 8;
  const HIT_Y = 0.86, WINDOW = 0.06;   // the hit line and the timing window's half-height, when a snapshot has none
  const fin = v => typeof v === 'number' && Number.isFinite(v);
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const sid = s => (typeof s === 'string' && /^[a-z0-9_-]{1,40}$/.test(s)) ? s : null;
  const plain = (s, n = 120) => typeof s === 'string' ? s.replace(/[\u0000-\u001f\u007f<>]/g, '').trim().slice(0, n) : '';
  const isTestHello = h => !!h && (h.test === true || /^\s*TEST\b/.test(String(h.label || '')));
  const put = (el, s) => { const t = String(s == null ? '' : s); if (el.textContent !== t) el.textContent = t; };
  const plural = (n, one, many) => n + ' ' + (n === 1 ? one : many);
  const ratePct = (h, n) => n > 0 ? (h / n * 100).toFixed(h === n || h === 0 ? 0 : 1) + '%' : '—';

  /* ---- the melodies (assets/songs.json, the same file the training env reads) */
  const SEMI = { C: 0, D: 2, E: 4, F: 5, G: 7, A: 9, B: 11 };
  const midiOf = s => { const m = typeof s === 'string' && /^([A-G])(#|b)?(-?\d)$/.exec(s);
    return m ? 12 * (+m[3] + 1) + SEMI[m[1]] + (m[2] === '#' ? 1 : m[2] === 'b' ? -1 : 0) : null; };
  const noteLabel = s => String(s).replace('#', '♯').replace(/^([A-G])b/, '$1♭');
  const SONGS = new Map();
  let songsAsked = false;
  function loadSongs() {
    if (songsAsked || !panel) return;
    songsAsked = true;
    fetch('assets/songs.json', { cache: 'no-cache' }).then(r => r.ok ? r.json() : null).then(j => {
      if (!Array.isArray(j)) return;
      for (const s of j.slice(0, 64)) {
        if (!s || typeof s !== 'object' || !sid(s.id) || !Array.isArray(s.notes) || !s.notes.length) continue;
        const notes = s.notes.slice(0, 2000).map(n => {
          const midi = Array.isArray(n) ? midiOf(n[0]) : null;
          return midi === null ? null : { midi, name: noteLabel(n[0]), beats: fin(n[1]) && n[1] > 0 ? Math.min(n[1], 8) : 1 };
        });
        if (notes.some(n => !n)) continue;
        const ms = notes.map(n => n.midi);
        SONGS.set(s.id, { id: s.id, title: plain(s.title) || s.id.replace(/_/g, ' '), composer: plain(s.composer),
                          pd: s.public_domain === true, notes, lo: Math.min(...ms), hi: Math.max(...ms) });
      }
      stripKey = null;
      renderUi();
    }).catch(() => { /* the board still works; notes are named from the stream only */ });
  }

  /* ---- the sound: made in the browser, only after the viewer's tap */
  const Piano = {
    ctx: null, bus: null, master: null, wave: null, knock: null, muted: false,
    unlock() {
      if (this.ctx) { try { this.ctx.resume(); } catch (e) { /* ignore */ } return true; }
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return false;
      let c;
      try { c = new AC(); } catch (e) { return false; }
      this.ctx = c;
      const comp = c.createDynamicsCompressor();
      comp.threshold.value = -16; comp.knee.value = 12; comp.ratio.value = 3.5; comp.attack.value = 0.004; comp.release.value = 0.25;
      const master = c.createGain(); master.gain.value = 0.55;
      master.connect(comp); comp.connect(c.destination);
      this.master = master; this.bus = master;
      try {   // a small room: a short, synthetic impulse response (decaying noise)
        const len = Math.round(c.sampleRate * 1.3), ir = c.createBuffer(2, len, c.sampleRate);
        for (let ch = 0; ch < 2; ch++) { const d = ir.getChannelData(ch); for (let i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, 3.4); }
        const conv = c.createConvolver(); conv.buffer = ir;
        const wet = c.createGain(); wet.gain.value = 0.15;
        const bus = c.createGain();
        bus.connect(master); bus.connect(conv); conv.connect(wet); wet.connect(master);
        this.bus = bus;
      } catch (e) { /* dry */ }
      // a struck string's partials (the filter below darkens the tone as it dies away, like a piano's)
      const H = [0, 1, 0.52, 0.32, 0.2, 0.14, 0.1, 0.07, 0.05, 0.035, 0.025, 0.018];
      this.wave = c.createPeriodicWave(new Float32Array(H.length), Float32Array.from(H));
      const kb = c.createBuffer(1, Math.round(c.sampleRate * 0.06), c.sampleRate), kd = kb.getChannelData(0);
      for (let i = 0; i < kd.length; i++) kd[i] = (Math.random() * 2 - 1) * (1 - i / kd.length);
      this.knock = kb;
      try { c.resume(); } catch (e) { /* ignore */ }
      return true;
    },
    setMuted(m) {
      this.muted = !!m;
      if (this.ctx) this.master.gain.setTargetAtTime(this.muted ? 0 : 0.55, this.ctx.currentTime, 0.03);
    },
    play(midi, beats) {
      const c = this.ctx;
      if (!c || this.muted || c.state === 'closed' || !fin(midi)) return;
      const t = c.currentTime + 0.015, f = 440 * Math.pow(2, (midi - 69) / 12);
      const hold = clamp(beats * BEAT_S, 0.8, 2.4), end = t + hold + 0.9;
      const tau = clamp(1.5 * Math.pow(261.63 / f, 0.55), 0.3, 2.4);         // lower strings ring longer
      const peak = 0.3 * clamp(Math.pow(261.63 / f, 0.18), 0.75, 1.2);
      const env = c.createGain();
      env.gain.setValueAtTime(0.0001, t);
      env.gain.linearRampToValueAtTime(peak, t + 0.005);
      env.gain.setTargetAtTime(peak * 0.42, t + 0.005, 0.09);                 // the quick first drop of a struck string
      env.gain.setTargetAtTime(0.0001, t + 0.22, tau);                         // then the long decay
      env.gain.setTargetAtTime(0.0001, t + hold, 0.11);                        // the damper, when the key is let go
      const lp = c.createBiquadFilter();
      lp.type = 'lowpass'; lp.Q.value = 0.4;
      lp.frequency.setValueAtTime(Math.min(16000, f * 12), t);
      lp.frequency.setTargetAtTime(Math.min(12000, f * 3.2), t + 0.01, 0.5);
      lp.connect(env); env.connect(this.bus);
      for (const cents of [-1.6, 1.6]) {                                        // two strings of one key, a hair apart
        const o = c.createOscillator();
        o.setPeriodicWave(this.wave); o.frequency.value = f; o.detune.value = cents;
        o.connect(lp); o.start(t); o.stop(end);
      }
      const n = c.createBufferSource(), bp = c.createBiquadFilter(), ng = c.createGain();   // the hammer's soft knock
      n.buffer = this.knock; bp.type = 'bandpass'; bp.frequency.value = clamp(f * 5, 1200, 5200); bp.Q.value = 0.9;
      ng.gain.setValueAtTime(peak * 0.2, t); ng.gain.exponentialRampToValueAtTime(0.0001, t + 0.045);
      n.connect(bp); bp.connect(ng); ng.connect(this.bus); n.start(t); n.stop(t + 0.06);
    },
  };

  /* ---- state */
  const S = {
    st: null,                  // {live, test, streaming, task, run} from the 3D view (or this panel's own socket)
    open: false, wasOpen: false,
    snaps: [], lastAt: 0, play: null, lastT: 0,
    lanes: 4, hitY: HIT_Y, win: WINDOW, song: null, noteI: null, firstSeen: null, speed: null,
    results: new Map(),        // note index -> 'hit' | 'miss', this play of the song
    tileState: new Map(), tileNote: new Map(), hitAt: new Map(),   // tile id -> result / note index / when it was hit
    hits: 0, misses: 0, wrong: 0, streak: 0, best: 0, attempt: null, streakAt: -9,
    run: null, had: false,
    pending: [], fx: [], btn: [],
    rat: { x: 1.5, v: 0, face: 1, faceK: 1, pressAt: -9, pressLane: null, run: 0 },
    sound: false, bbTiles: false,
  };
  let stripKey = null, bars = [], lastMode = '', raf = 0, visible = true, W = 0, H = 0, dpr = 1, lastDraw = 0;

  function mode(now) {
    const st = S.st;
    if (!st || !S.wasOpen) return 'connecting';
    if (st.streaming && st.task === 'tiles') {
      if (S.snaps.length && now - S.lastAt < STALE_S) return st.live ? 'live' : 'test';
      return 'wait';
    }
    return 'standby';
  }

  function newRun(run) {
    S.run = run; S.had = false;
    S.hits = S.misses = S.wrong = S.streak = S.best = 0; S.attempt = null;
    S.song = null; S.noteI = S.firstSeen = null; S.results.clear(); S.tileState.clear(); S.tileNote.clear(); S.hitAt.clear();
    S.snaps.length = 0; S.play = null; S.pending.length = 0; S.fx.length = 0; S.btn.length = 0;
  }
  function newPlay(song, noteI) {
    S.song = song; S.noteI = noteI; S.firstSeen = noteI;
    S.results.clear(); S.tileState.clear(); S.tileNote.clear(); S.hitAt.clear();
  }

  /* st.stream (from live.js, or from onRaw): the relay's training session, null or {live, test, task, run}. It does
     not wait for the 3D view, which only changes mode while it is on screen (this panel is usually scrolled to). */
  function onStatus(st) {
    if (!st || typeof st !== 'object') return;
    const s = st.stream && typeof st.stream === 'object' ? st.stream : null;
    const streaming = !!s || st.live === true || st.test === true || st.source === 'test';
    const live = s ? s.live === true && s.test !== true : st.live === true;
    const src = s || st;
    const next = { live, test: streaming && !live, streaming,
                   task: typeof src.task === 'string' ? src.task : null, run: typeof src.run === 'string' ? src.run : null };
    if (streaming && next.task === 'tiles' && next.run && next.run !== S.run) newRun(next.run);
    S.st = next;
    teaserUpdate();
    if (!panel) return;
    if (streaming && next.task === 'tiles') loadSongs();
    renderUi(); kick();
  }
  function onRelay(open) {
    S.open = !!open;
    if (S.open) S.wasOpen = true;
    if (panel) { renderUi(); kick(); }
  }

  /* ---- the stream */
  function readSnap(m) {
    if (!Array.isArray(m.tiles)) return null;
    const lanes = Number.isInteger(m.lanes) && m.lanes >= 1 && m.lanes <= MAX_LANES ? m.lanes : 4;
    const tiles = [];
    for (const r of m.tiles.slice(0, 64)) {
      if (!Array.isArray(r) || r.length < 5) continue;
      const [id, lane, y, h, state] = r;
      if (!Number.isInteger(id) || !Number.isInteger(lane) || lane < 0 || lane >= lanes || !fin(y) || !fin(h) ||
          h <= 0 || h > 1 || y < -3 || y > 3) continue;
      tiles.push({ id, lane, y, h, state: state === 'hit' || state === 'miss' ? state : 'up' });
    }
    const c = m.cursor;
    return { t: fin(m.t) ? m.t : null, song: sid(m.song), lanes, tiles, byId: new Map(tiles.map(t => [t.id, t])),
             speed: fin(m.speed) && m.speed > 0 && m.speed < 20 ? m.speed : null,
             hitY: fin(m.hit_y) && m.hit_y >= 0.5 && m.hit_y <= 0.98 ? m.hit_y : null,
             win: fin(m.window) && m.window > 0 && m.window <= 0.3 ? m.window : null,
             cursor: Array.isArray(c) && fin(c[0]) && fin(c[1]) && c[0] >= 0 && c[1] >= 0 ? [clamp(c[0], 0, 1), clamp(c[1], 0, 1)] : null,
             noteI: Number.isInteger(m.note_i) && m.note_i >= 0 && m.note_i < 100000 ? m.note_i : null };
  }
  function onTiles(m) {
    if (!panel || !m || typeof m !== 'object') return;
    const now = performance.now() / 1000;
    if (m.type === 'tiles') {
      const s = readSnap(m);
      if (!s) return;
      const prev = S.snaps[S.snaps.length - 1], prevNote = S.noteI, prevSong = S.song;
      let dt = prev && s.t !== null && prev.t !== null ? s.t - prev.t : NaN;     // sim time between snapshots
      if (!(dt > 0 && dt < 1.5)) dt = prev ? clamp(now - prev.at, 0.02, 0.5) : 0; // a new attempt (its clock restarts)
      s.st = prev ? prev.st + dt : 0; s.at = now;
      S.snaps.push(s);
      if (S.snaps.length > 40) S.snaps.splice(0, S.snaps.length - 40);
      S.lastAt = now; S.had = true; S.lanes = s.lanes;
      if (s.speed) S.speed = s.speed;
      S.hitY = s.hitY !== null ? s.hitY : HIT_Y;
      S.win = s.win !== null ? s.win : WINDOW;
      if (s.song && (s.song !== S.song || (s.noteI !== null && S.noteI !== null && s.noteI < S.noteI))) newPlay(s.song, s.noteI);
      if (s.noteI !== null) { S.noteI = s.noteI; if (S.firstSeen === null) S.firstSeen = s.noteI; }
      loadSongs();
      kick();
      if ((lastMode !== 'live' && lastMode !== 'test') || S.noteI !== prevNote || S.song !== prevSong) renderUi();
      return;
    }
    if (m.type !== 'tile' || !['hit', 'miss', 'wrong'].includes(m.result)) return;
    const ev = { result: m.result, id: Number.isInteger(m.id) ? m.id : null, lane: Number.isInteger(m.lane) && m.lane >= 0 && m.lane < MAX_LANES ? m.lane : null,
                 noteI: Number.isInteger(m.note_i) && m.note_i >= 0 && m.note_i < 100000 ? m.note_i : null, song: sid(m.song),
                 timing: fin(m.timing) && Math.abs(m.timing) < 10 ? m.timing : null };
    S.pending.push({ due: now + DELAY, kind: 'tile', ev });
    kick();
  }
  function onEpisode(m) {
    if (!panel || !m || typeof m !== 'object' || !(S.st && S.st.task === 'tiles')) return;
    S.pending.push({ due: performance.now() / 1000 + DELAY, kind: 'episode',
                     ev: { hits: Number.isInteger(m.hits) && m.hits >= 0 ? m.hits : null, n: Number.isInteger(m.n) ? m.n : null } });
  }

  /* ---- playback: the playhead runs on the wall clock, DELAY behind the newest snapshot */
  function advance(now) {
    const dt = S.lastT ? clamp(now - S.lastT, 0, 0.25) : 0;
    S.lastT = now;
    const N = S.snaps[S.snaps.length - 1];
    if (!N) return;
    const target = N.st + (now - N.at) - DELAY;
    if (S.play === null) { S.play = target; return; }
    S.play += dt;
    const err = target - S.play;
    if (Math.abs(err) > 0.5) S.play = target; else S.play += err * Math.min(1, dt * 2);
  }
  function sample() {
    const A = S.snaps;
    if (!A.length || S.play === null) return null;
    const p = S.play;
    let i = A.length - 1;
    while (i > 0 && A[i].st > p) i--;
    const a = A[i], b = A[i + 1];
    if (!b || p < a.st) {                       // before the first or after the newest snapshot: slide on at its speed
      const x = clamp(p - a.st, -0.3, 0.4), sp = a.speed || 0;
      return { tiles: a.tiles.map(t => ({ ...t, y: t.y + sp * x })), cursor: a.cursor, lanes: a.lanes };
    }
    const span = Math.max(1e-6, b.st - a.st), al = clamp((p - a.st) / span, 0, 1), near = al < 0.5 ? a : b;
    const tiles = b.tiles.map(t => {
      const o = a.byId.get(t.id);
      const y = o ? o.y + (t.y - o.y) * al : t.y - (b.speed || 0) * (1 - al) * span;
      return { ...t, y, state: (near.byId.get(t.id) || t).state };
    });
    for (const t of a.tiles) if (!b.byId.has(t.id) && al < 0.5) tiles.push({ ...t, y: t.y + (a.speed || 0) * al * span });
    const ca = a.cursor, cb = b.cursor;
    const cursor = ca && cb ? [ca[0] + (cb[0] - ca[0]) * al, ca[1] + (cb[1] - ca[1]) * al] : near.cursor;
    return { tiles, cursor, lanes: b.lanes };
  }

  /* ---- tile events, applied when the board reaches them */
  function judge(t) {                           // a hit's timing, in words
    if (!fin(t)) return '';
    return Math.abs(t) * (S.speed || 0.3) <= Math.max(0.015, S.win * 0.5) ? 'Perfect' : t < 0 ? 'Early' : 'Late';
  }
  function press(now, lane, kind) {
    const R = S.rat;
    R.pressAt = now; R.pressLane = lane;
    S.btn[lane] = { at: now, kind };
  }
  function makeFx(kind, at, lane, o) {
    const f = Object.assign({ kind, at, lane }, o || {});
    if (!RM.matches) {
      const n = kind === 'hit' ? 26 : kind === 'miss' ? 11 : 0;
      f.parts = [];
      for (let i = 0; i < n; i++) {
        f.parts.push(kind === 'hit'
          ? { a: -Math.PI * (0.06 + 0.88 * Math.random()), sp: 0.28 + Math.random() * 0.62, r: 1.1 + Math.random() * 2.3, c: Math.random() }
          : { a: Math.random() * Math.PI * 2, sp: 0.03 + Math.random() * 0.06, r: 0.012 + Math.random() * 0.016, c: Math.random() });
      }
    }
    return f;
  }
  function pump(now) {
    advance(now);
    let changed = false;
    while (S.pending.length && S.pending[0].due <= now) {
      const p = S.pending.shift();
      changed = true;
      if (p.kind === 'episode') {
        S.attempt = p.ev; S.tileState.clear(); S.tileNote.clear(); S.hitAt.clear();
        continue;
      }
      const ev = p.ev, late = now - p.due > LATE_S;
      if (ev.song && ev.song !== S.song) newPlay(ev.song, ev.noteI);
      const smp = sample(), L = S.lanes;
      const tile = smp && ev.id !== null ? smp.tiles.find(t => t.id === ev.id) : null;
      const cur = smp && smp.cursor;
      const curLane = cur ? Math.min(L - 1, Math.floor(cur[0] * L)) : 0;
      // a wrong press names the lane the press landed in (its id is the lowest tile's); the others, the tile's lane
      const lane = Math.min(L - 1, ev.result === 'wrong' ? (ev.lane !== null ? ev.lane : curLane)
        : tile ? tile.lane : ev.lane !== null ? ev.lane : curLane);
      if (ev.result === 'hit') {
        S.hits++; S.streak++; S.best = Math.max(S.best, S.streak); S.streakAt = now;
        if (ev.noteI !== null) S.results.set(ev.noteI, 'hit');
        if (ev.id !== null) { S.tileState.set(ev.id, 'hit'); S.hitAt.set(ev.id, now); if (ev.noteI !== null) S.tileNote.set(ev.id, ev.noteI); }
        const song = SONGS.get(S.song), note = song && ev.noteI !== null ? song.notes[ev.noteI] : null;
        if (note && S.sound && !late) Piano.play(note.midi, note.beats);
        press(now, lane, 'hit');
        S.fx.push(makeFx('hit', now, lane, { label: note ? note.name : '', judge: judge(ev.timing) }));
      } else if (ev.result === 'miss') {
        S.misses++; S.streak = 0;
        if (ev.noteI !== null) S.results.set(ev.noteI, 'miss');
        if (ev.id !== null) S.tileState.set(ev.id, 'miss');
        S.fx.push(makeFx('miss', now, lane));
      } else {
        S.wrong++; S.streak = 0;
        press(now, lane, 'wrong');
        S.fx.push(makeFx('wrong', now, lane));
      }
    }
    if (S.fx.length) S.fx = S.fx.filter(f => now - f.at < 1.3);
    if (changed) renderUi();
  }

  /* ---- the scene: the rat's screen as a runway under a glowing sky, the lanes meeting at a sun on the horizon.
     Screen y (down) maps to the runway's depth with a true perspective, so a tile's speed along its lane is the
     stream's speed and it grows as it comes closer. */
  const Y_FAR = -0.2;                       // the far end of the rat's screen that has room on the runway
  const V = { key: '', sky: null, track: null, g: null, rat: null, ratW: 0, ratH: 0, ratPad: 0, notes: null, bokeh: null, amb: null };
  const TAU = Math.PI * 2;

  function geom() {
    const portrait = H > W * 1.02;
    const hy = H * (portrait ? 0.15 : 0.165);          // the horizon: where the lanes meet
    const Sa = H * (portrait ? 0.34 : 0.35);           // the row of y = Y_FAR
    const Sc = H * (portrait ? 0.85 : 0.86);           // the row of y = 1 (the bottom edge of the rat's screen)
    const p = Sa - hy, q = Sc - hy;
    const yc = (q - p * Y_FAR) / (q - p), A = q * (yc - 1);
    const nearHalf = Math.min(W * (portrait ? 0.54 : 0.47), H * 0.66);
    const g = { W, H, hy, Sa, Sc, q, yc, A, nearHalf, vx: W / 2, portrait };
    g.sy = y => hy + A / Math.max(0.03, yc - y);
    g.yOf = s => yc - A / Math.max(1e-3, s - hy);
    g.half = s => nearHalf * (s - hy) / q;
    g.X = (u, s) => g.vx + (u - 0.5) * 2 * g.half(s);
    return g;
  }
  function btnGeom(g, L) {
    const sb = g.sy(S.hitY), lw = 2 * g.half(sb) / L, rx = lw * 0.3, ry = rx * 0.44;
    return { sb, lw, rx, ry, d: ry * 0.72 };
  }

  /* sprites, made once: soft bokeh discs, music-note glyphs (drawn as paths), and the rat (the labrat logo with its
     black background keyed out) */
  function sprites() {
    if (V.notes) return;
    const mk = (w, h) => { const c = document.createElement('canvas'); c.width = w; c.height = h; return [c, c.getContext('2d')]; };
    V.bokeh = [[255, 214, 130], [255, 140, 210], [205, 175, 255], [255, 255, 255]].map(([r, gg, b]) => {
      const [c, x] = mk(64, 64), gr = x.createRadialGradient(32, 32, 0, 32, 32, 32);
      gr.addColorStop(0, `rgba(${r},${gg},${b},.95)`); gr.addColorStop(0.55, `rgba(${r},${gg},${b},.5)`);
      gr.addColorStop(0.8, `rgba(${r},${gg},${b},.18)`); gr.addColorStop(1, `rgba(${r},${gg},${b},0)`);
      x.fillStyle = gr; x.fillRect(0, 0, 64, 64); return c;
    });
    const glyph = (kind, col) => {
      const [c, x] = mk(96, 96);
      x.fillStyle = x.strokeStyle = col; x.lineCap = 'round'; x.lineJoin = 'round';
      const head = (hx, hy) => { x.save(); x.translate(hx, hy); x.rotate(-0.42); x.beginPath(); x.ellipse(0, 0, 11, 7.6, 0, 0, TAU); x.fill(); x.restore(); };
      const shape = () => {
        x.lineWidth = 4.2;
        if (kind === 0) {             // an eighth note
          head(38, 68); x.beginPath(); x.moveTo(47.8, 64); x.lineTo(47.8, 20); x.stroke();
          x.beginPath(); x.moveTo(47.8, 20); x.bezierCurveTo(56, 29, 68, 33, 62, 52); x.stroke();
        } else if (kind === 1) {      // two beamed eighths
          head(27, 70); head(63, 62);
          x.beginPath(); x.moveTo(36.8, 66); x.lineTo(36.8, 27); x.moveTo(72.8, 58); x.lineTo(72.8, 19); x.stroke();
          x.beginPath(); x.moveTo(35, 24); x.lineTo(74.6, 15); x.lineTo(74.6, 25); x.lineTo(35, 34); x.closePath(); x.fill();
        } else {                      // a quarter note
          head(40, 68); x.beginPath(); x.moveTo(49.8, 64); x.lineTo(49.8, 18); x.stroke();
        }
      };
      x.shadowColor = col; x.shadowBlur = 16; shape(); x.shadowBlur = 6; shape();
      return c;
    };
    V.notes = [0, 1, 2].map(k => ['#fff1fb', '#ffd776', '#ffb3e6'].map(col => glyph(k, col)));
    // floating notes and bokeh: fixed pseudo-random layout (the same on every visit)
    let seed = 7;
    const R = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
    V.amb = {
      bokeh: Array.from({ length: 16 }, () => ({ x: R(), y: 0.02 + R() * 0.5, r: 0.012 + R() * 0.04, c: Math.floor(R() * 4),
        a: 0.25 + R() * 0.45, sp: 0.1 + R() * 0.25, tw: 0.6 + R() * 1.4, ph: R() * TAU })),
      notes: Array.from({ length: 11 }, () => ({ xs: Array.from({ length: 6 }, () => R()), y0: 0.4 + R() * 0.18,
        rise: 0.32 + R() * 0.28, dur: 7 + R() * 6, l0: R(), s: 0.05 + R() * 0.045, k: Math.floor(R() * 3), c: Math.floor(R() * 3),
        a: 0.45 + R() * 0.4, ph: R() * TAU, rot: (R() - 0.5) * 0.5 })),
    };
    // the rat: the owner's pixel-art logo (a rat on black), cropped to the rat, its black keyed to transparent, with a
    // soft violet outline so it reads on the bright runway
    const img = new Image();
    img.decoding = 'async';
    img.onload = () => {
      const nw = img.naturalWidth, nh = img.naturalHeight;
      const sx = 60 / 1254 * nw, sy0 = 318 / 1254 * nh, sw = 1150 / 1254 * nw, sh = 650 / 1254 * nh;
      const tw = 520, th = Math.round(tw * 650 / 1150), pad = 16;
      const [k, kx] = mk(tw, th);
      kx.imageSmoothingQuality = 'high';
      kx.drawImage(img, sx, sy0, sw, sh, 0, 0, tw, th);
      let keyed = true;
      try {
        const d = kx.getImageData(0, 0, tw, th), px = d.data;
        for (let i = 0; i < px.length; i += 4) {
          const m = Math.max(px[i], px[i + 1], px[i + 2]);
          px[i + 3] = Math.round(clamp((m - 28) / 44, 0, 1) * 255);
        }
        kx.putImageData(d, 0, 0);
      } catch (e) { keyed = false; }
      const [c, x] = mk(tw + 2 * pad, th + 2 * pad);
      if (keyed) { x.shadowColor = 'rgba(46,8,96,.85)'; x.shadowBlur = 9; x.shadowOffsetY = 2; }
      x.drawImage(k, pad, pad);
      V.rat = c; V.ratW = tw; V.ratH = th; V.ratPad = pad; V.ratKeyed = keyed;
      if (!raf) draw(performance.now() / 1000, mode(performance.now() / 1000), 0);
    };
    img.src = 'assets/labrat-logo.jpg';
  }

  /* the parts of the picture that only change with the size: the sky, and the runway with its hit line and sockets */
  function layers(g, L) {
    const key = [W, H, dpr, L, S.hitY, S.win].join(',');
    if (V.key === key && V.sky) return;
    V.key = key;
    const mk = () => {
      const c = document.createElement('canvas');
      c.width = Math.max(1, Math.round(W * dpr)); c.height = Math.max(1, Math.round(H * dpr));
      const x = c.getContext('2d'); x.setTransform(dpr, 0, 0, dpr, 0, 0); return [c, x];
    };
    const off = (s, a, b) => clamp((s - a) / (b - a), 0, 1);
    // the sky: deep violet overhead, a sunset band on the horizon, magenta haze under it
    const [sky, a] = mk();
    const hf = g.hy / H;
    let gr = a.createLinearGradient(0, 0, 0, H);
    gr.addColorStop(0, '#16043a'); gr.addColorStop(hf * 0.45, '#3b0c8c'); gr.addColorStop(hf * 0.82, '#a01ea4');
    gr.addColorStop(hf, '#ff9a5e'); gr.addColorStop(Math.min(1, hf + 0.06), '#ea3f8a');
    gr.addColorStop(Math.min(1, hf + 0.3), '#8a18ac'); gr.addColorStop(1, '#2c0768');
    a.fillStyle = gr; a.fillRect(0, 0, W, H);
    const R0 = Math.max(W, H) * 0.8;
    gr = a.createRadialGradient(g.vx, g.hy, 0, g.vx, g.hy, R0);
    gr.addColorStop(0, 'rgba(255,252,232,1)'); gr.addColorStop(0.025, 'rgba(255,240,170,.95)');
    gr.addColorStop(0.08, 'rgba(252,196,92,.55)'); gr.addColorStop(0.2, 'rgba(245,116,84,.25)');
    gr.addColorStop(0.45, 'rgba(214,12,148,.1)'); gr.addColorStop(1, 'rgba(214,12,148,0)');
    a.fillStyle = gr; a.fillRect(0, 0, W, H);
    const blob = (x, y, rw, rh, col, al) => {
      a.save(); a.translate(x, y); a.scale(1, rh / rw);
      const b = a.createRadialGradient(0, 0, 0, 0, 0, rw);
      b.addColorStop(0, `rgba(${col},${al})`); b.addColorStop(0.6, `rgba(${col},${al * 0.45})`); b.addColorStop(1, `rgba(${col},0)`);
      a.fillStyle = b; a.beginPath(); a.arc(0, 0, rw, 0, TAU); a.fill(); a.restore();
    };
    blob(g.vx, g.hy, W * 0.75, H * 0.07, '255,210,150', 0.55);                         // the horizon's glow
    [[0.1, 0.012, 0.3, 0.03, 0.5], [0.9, 0.006, 0.34, 0.034, 0.45], [0.3, -0.01, 0.2, 0.018, 0.35],
     [0.72, -0.014, 0.22, 0.02, 0.35], [0.02, 0.1, 0.3, 0.05, 0.2], [0.98, 0.13, 0.3, 0.055, 0.2],
     [0.16, -0.1, 0.24, 0.022, 0.12], [0.84, -0.12, 0.28, 0.025, 0.12]]
      .forEach(([x, dy, w, h, al]) => blob(x * W, g.hy + dy * H, w * W, h * H, '255,222,240', al));   // clouds
    V.sky = sky;

    // the runway
    const [trk, b] = mk();
    const top = g.hy + 0.5, bot = H + 4;
    const quad = (u0, u1, s0, s1) => { b.beginPath(); b.moveTo(g.X(u0, s0), s0); b.lineTo(g.X(u1, s0), s0); b.lineTo(g.X(u1, s1), s1); b.lineTo(g.X(u0, s1), s1); b.closePath(); };
    b.save(); b.shadowColor = 'rgba(255,120,200,.6)'; b.shadowBlur = 34;
    quad(0, 1, g.Sa - (g.Sa - g.hy) * 0.35, bot); b.fillStyle = 'rgba(255,236,248,.5)'; b.fill(); b.restore();
    gr = b.createLinearGradient(0, g.hy, 0, H);
    gr.addColorStop(0, 'rgba(255,224,190,0)');
    gr.addColorStop(off(g.Sa, g.hy, H) * 0.55, 'rgba(255,232,232,.62)');
    gr.addColorStop(off(g.Sa, g.hy, H), 'rgba(255,244,252,.93)');
    gr.addColorStop(1, 'rgba(238,226,252,.98)');
    quad(0, 1, top, bot); b.fillStyle = gr; b.fill();
    for (let k = 1; k < L; k += 2) { quad(k / L, (k + 1) / L, top, bot); b.fillStyle = 'rgba(120,64,196,.045)'; b.fill(); }
    // lane lines and the glowing rails, fading into the haze at the far end
    const fade = (rgb, a0) => { const l = b.createLinearGradient(0, g.hy, 0, H); l.addColorStop(0, `rgba(${rgb},0)`);
      l.addColorStop(off(g.Sa, g.hy, H), `rgba(${rgb},${a0})`); l.addColorStop(1, `rgba(${rgb},${a0})`); return l; };
    b.lineCap = 'round';
    for (let k = 1; k < L; k++) {
      b.strokeStyle = fade('104,44,168', 0.34); b.lineWidth = 1.3;
      b.beginPath(); b.moveTo(g.vx, g.hy); b.lineTo(g.X(k / L, bot), bot); b.stroke();
    }
    for (const u of [0, 1]) {
      b.strokeStyle = fade('255,120,190', 0.32); b.lineWidth = 9;
      b.beginPath(); b.moveTo(g.vx, g.hy); b.lineTo(g.X(u, bot), bot); b.stroke();
      b.strokeStyle = fade('255,196,110', 0.95); b.lineWidth = 2.4;
      b.beginPath(); b.moveTo(g.vx, g.hy); b.lineTo(g.X(u, bot), bot); b.stroke();
    }
    // the timing window and the hit line
    const B = btnGeom(g, L), s0 = g.sy(S.hitY - S.win), s1 = g.sy(S.hitY + S.win);
    gr = b.createLinearGradient(0, s0, 0, s1);
    gr.addColorStop(0, 'rgba(255,190,90,.05)'); gr.addColorStop(0.5, 'rgba(255,190,90,.26)'); gr.addColorStop(1, 'rgba(255,190,90,.05)');
    quad(0, 1, s0, s1); b.fillStyle = gr; b.fill();
    b.strokeStyle = 'rgba(255,90,150,.28)'; b.lineWidth = 10;
    b.beginPath(); b.moveTo(g.X(0, B.sb), B.sb); b.lineTo(g.X(1, B.sb), B.sb); b.stroke();
    gr = b.createLinearGradient(g.X(0, B.sb), 0, g.X(1, B.sb), 0);
    gr.addColorStop(0, 'rgba(234,53,96,.9)'); gr.addColorStop(0.5, 'rgba(255,200,90,1)'); gr.addColorStop(1, 'rgba(234,53,96,.9)');
    b.strokeStyle = gr; b.lineWidth = 2.2;
    b.beginPath(); b.moveTo(g.X(0, B.sb), B.sb); b.lineTo(g.X(1, B.sb), B.sb); b.stroke();
    // the buttons' sockets: a chrome ring and a dark well
    for (let k = 0; k < L; k++) {
      const x = g.X((k + 0.5) / L, B.sb), y = B.sb;
      b.fillStyle = 'rgba(46,10,80,.22)';
      b.beginPath(); b.ellipse(x, y + B.ry * 0.35, B.rx * 1.42, B.ry * 1.42, 0, 0, TAU); b.fill();
      gr = b.createLinearGradient(0, y - B.ry * 1.3, 0, y + B.ry * 1.3);
      gr.addColorStop(0, '#fff8ff'); gr.addColorStop(0.5, '#cdb9ea'); gr.addColorStop(1, '#6f55a0');
      b.fillStyle = gr; b.beginPath(); b.ellipse(x, y, B.rx * 1.3, B.ry * 1.3, 0, 0, TAU); b.fill();
      b.fillStyle = '#2a0617'; b.beginPath(); b.ellipse(x, y, B.rx * 1.06, B.ry * 1.06, 0, 0, TAU); b.fill();
    }
    // a soft vignette over everything
    gr = b.createRadialGradient(W / 2, H * 0.45, Math.min(W, H) * 0.3, W / 2, H * 0.45, Math.max(W, H) * 0.85);
    gr.addColorStop(0, 'rgba(10,2,28,0)'); gr.addColorStop(1, 'rgba(10,2,28,.42)');
    b.fillStyle = gr; b.fillRect(0, 0, W, H);
    V.track = trk;
  }

  function glow(x, y, r, rgb, al) {
    if (al <= 0.01 || r <= 0) return;
    const gr = cx.createRadialGradient(x, y, 0, x, y, r);
    gr.addColorStop(0, `rgba(${rgb},${al.toFixed(3)})`); gr.addColorStop(0.4, `rgba(${rgb},${(al * 0.4).toFixed(3)})`); gr.addColorStop(1, `rgba(${rgb},0)`);
    cx.fillStyle = gr;                          // plain alpha: an additive glow would vanish on the bright runway
    cx.beginPath(); cx.arc(x, y, r, 0, TAU); cx.fill();
  }
  function ellipse(x, y, rx, ry) { cx.beginPath(); cx.ellipse(x, y, Math.max(0.1, rx), Math.max(0.1, ry), 0, 0, TAU); }

  /* the sky's life: drifting bokeh and music notes floating up (behind the runway) */
  function ambient(g, now, still) {
    const t = still ? 0 : now;
    cx.globalCompositeOperation = 'lighter';
    for (const b of V.amb.bokeh) {
      const r = b.r * H, x = (b.x + Math.sin(t * b.sp + b.ph) * 0.02) * W, y = (b.y + Math.cos(t * b.sp * 0.7 + b.ph) * 0.012) * H;
      cx.globalAlpha = b.a * (0.6 + 0.4 * Math.sin(t * b.tw + b.ph));
      cx.drawImage(V.bokeh[b.c], x - r, y - r, 2 * r, 2 * r);
    }
    cx.globalCompositeOperation = 'source-over';
    for (const n of V.amb.notes) {
      const cyc = still ? 0 : Math.floor(n.l0 + t / n.dur), life = still ? n.l0 : (n.l0 + t / n.dur) - cyc;
      const y = (n.y0 - life * n.rise) * H;
      // across the sky, and never over the runway at its row
      const u = n.xs[cyc % n.xs.length];
      const half = y > g.hy ? g.half(y) + W * 0.035 : W * 0.06, side = Math.max(0, W / 2 - half);
      const x = (u < 0.5 ? (u / 0.5) * side : W - ((u - 0.5) / 0.5) * side) + Math.sin(life * TAU * 0.8 + n.ph) * W * 0.01;
      const s = n.s * H;
      cx.globalAlpha = n.a * Math.pow(Math.sin(Math.PI * life), 0.8);
      cx.save(); cx.translate(x, y); cx.rotate(n.rot + (still ? 0 : Math.sin(t * 0.9 + n.ph) * 0.14));
      cx.drawImage(V.notes[n.k][n.c], -s / 2, -s / 2, s, s); cx.restore();
    }
    cx.globalAlpha = 1;
  }

  /* lines across the runway, moving toward the viewer at the tiles' speed */
  function beatLines(g, now, playing, still) {
    const step = 0.2, sp = playing ? (S.speed || 0.2) : 0.06;
    const ph = still ? 0 : ((playing && S.play !== null ? S.play : now) * sp) % step;
    const bottomY = g.yOf(H + 2);
    cx.lineWidth = 1;
    for (let y = Y_FAR - 0.8 + ph; y < bottomY; y += step) {
      const s = g.sy(y);
      if (s < g.hy + 2) continue;
      const a = 0.16 * clamp((s - g.hy) / (g.Sa - g.hy), 0, 1);
      cx.strokeStyle = `rgba(136,70,200,${a.toFixed(3)})`;
      cx.beginPath(); cx.moveTo(g.X(0, s), s); cx.lineTo(g.X(1, s), s); cx.stroke();
    }
  }

  /* the lane the rat's cursor is in: a soft beam from the rat up past its button */
  function laneBeam(g, B, L, smp) {
    const c = smp && smp.cursor;
    if (!c) return;
    const k = Math.min(L - 1, Math.floor(clamp(c[0], 0, 0.9999) * L));
    const s0 = g.sy(S.hitY - 0.32), s1 = H + 4, u0 = k / L, u1 = (k + 1) / L;
    const gr = cx.createLinearGradient(0, s0, 0, s1);
    gr.addColorStop(0, 'rgba(214,12,148,0)'); gr.addColorStop(clamp((B.sb - s0) / (s1 - s0), 0, 1), 'rgba(214,12,148,.2)');
    gr.addColorStop(1, 'rgba(214,12,148,.1)');
    cx.fillStyle = gr;
    cx.beginPath(); cx.moveTo(g.X(u0, s0), s0); cx.lineTo(g.X(u1, s0), s0); cx.lineTo(g.X(u1, s1), s1); cx.lineTo(g.X(u0, s1), s1); cx.closePath(); cx.fill();
  }

  function tileLook(st) { return st === 'up' || st === 'miss' || st === 'hit' ? st : 'up'; }
  function drawTiles(g, smp, now, L) {
    const list = smp.tiles.slice().sort((a, b) => a.y - b.y);
    const bottomY = g.yOf(H + 30);
    for (const t of list) {
      const st = tileLook(S.tileState.get(t.id) || t.state);
      const y0 = t.y - t.h / 2, y1 = t.y + t.h / 2;
      if (y0 > bottomY || y1 < Y_FAR - 0.6) continue;
      let al = clamp((t.y + 0.32) / 0.34, 0, 1);                        // out of the haze at the far end
      if (st === 'hit') {
        const at = S.hitAt.get(t.id), age = at !== undefined ? now - at : (t.y - S.hitY) / (S.speed || 0.3);
        al *= clamp(1 - age / 0.3, 0, 1);
      } else if (st === 'miss') al *= 0.85 * clamp(1 - (y0 - 0.95) / 0.2, 0, 1);
      if (al <= 0.01) continue;
      const s0 = g.sy(Math.max(y0, -3)), s1 = g.sy(y1);
      const u0 = (t.lane + 0.075) / L, u1 = (t.lane + 0.925) / L;
      const a0 = g.X(u0, s0), a1 = g.X(u1, s0), b0 = g.X(u0, s1), b1 = g.X(u1, s1);
      const k = (s1 - g.hy) / g.q, th = Math.max(1.2, 0.013 * H * k);
      cx.globalAlpha = al;
      // contact shadow
      cx.fillStyle = 'rgba(52,16,100,.16)';
      cx.beginPath(); cx.moveTo(a0 + 2, s0 + th); cx.lineTo(a1 + 4, s0 + th); cx.lineTo(b1 + 6 * k, s1 + th * 2.2); cx.lineTo(b0 + 3 * k, s1 + th * 2.2); cx.closePath(); cx.fill();
      // the slab's near face
      cx.fillStyle = st === 'miss' ? '#3d3647' : st === 'hit' ? '#c77d18' : '#06020c';
      cx.beginPath(); cx.moveTo(b0, s1); cx.lineTo(b1, s1); cx.lineTo(b1, s1 + th); cx.lineTo(b0, s1 + th); cx.closePath(); cx.fill();
      // the top: glossy black, a sheen on its far part, and a rim light from the sun behind it
      let gr = cx.createLinearGradient(0, s0, 0, s1);
      if (st === 'miss') { gr.addColorStop(0, '#9a92a8'); gr.addColorStop(1, '#5d5669'); }
      else if (st === 'hit') { gr.addColorStop(0, '#fff6c4'); gr.addColorStop(1, '#f5ac29'); }
      else { gr.addColorStop(0, '#34203f'); gr.addColorStop(0.2, '#150b1f'); gr.addColorStop(1, '#030106'); }
      cx.fillStyle = gr;
      cx.beginPath(); cx.moveTo(a0, s0); cx.lineTo(a1, s0); cx.lineTo(b1, s1); cx.lineTo(b0, s1); cx.closePath(); cx.fill();
      if (st === 'up') {
        const sh = s0 + (s1 - s0) * 0.42;
        gr = cx.createLinearGradient(0, s0, 0, sh);
        gr.addColorStop(0, 'rgba(255,236,255,.22)'); gr.addColorStop(1, 'rgba(255,236,255,0)');
        cx.fillStyle = gr;
        cx.beginPath(); cx.moveTo(a0, s0); cx.lineTo(a1, s0); cx.lineTo(g.X(u1, sh), sh); cx.lineTo(g.X(u0, sh), sh); cx.closePath(); cx.fill();
        cx.strokeStyle = 'rgba(255,170,228,.38)'; cx.lineWidth = 1;
        cx.beginPath(); cx.moveTo(a0, s0); cx.lineTo(a1, s0); cx.lineTo(b1, s1); cx.lineTo(b0, s1); cx.closePath(); cx.stroke();
        cx.strokeStyle = 'rgba(255,214,150,.9)'; cx.lineWidth = Math.max(1, 1.8 * k);
        cx.beginPath(); cx.moveTo(a0 + 1, s0); cx.lineTo(a1 - 1, s0); cx.stroke();
      } else if (st === 'hit') {
        cx.strokeStyle = 'rgba(255,248,200,.9)'; cx.lineWidth = 1.5;
        cx.beginPath(); cx.moveTo(a0, s0); cx.lineTo(a1, s0); cx.lineTo(b1, s1); cx.lineTo(b0, s1); cx.closePath(); cx.stroke();
      }
    }
    cx.globalAlpha = 1;
  }

  function drawButtons(g, B, now, L, smp, still) {
    for (let k = 0; k < L; k++) {
      const bx = g.X((k + 0.5) / L, B.sb), b = S.btn[k], age = b ? now - b.at : 9;
      let pr = 0, shake = 0;
      if (age >= 0 && age < 0.42) pr = age < 0.06 ? age / 0.06 : Math.max(0, 1 - (age - 0.06) / 0.3);
      if (b && b.kind === 'wrong' && age < 0.45 && !still) shake = Math.sin(age * 72) * (1 - age / 0.45) * B.rx * 0.16;
      // a tile on the line in this lane: the button is live
      const ready = !!smp && smp.tiles.some(t => t.lane === k && tileLook(S.tileState.get(t.id) || t.state) === 'up' &&
        Math.abs(t.y - S.hitY) <= t.h / 2 + S.win);
      const x = bx + shake, top = B.sb - B.d * (1 - pr * 0.72);
      if (pr > 0) glow(x, top, B.rx * 2.6, b.kind === 'hit' ? '255,178,30' : '255,30,60', 0.7 * pr);
      else if (ready) glow(x, top, B.rx * 2.1, '255,50,90', 0.26 + (still ? 0 : 0.12 * Math.sin(now * 10)));
      // the side of the cap
      let gr = cx.createLinearGradient(x - B.rx, 0, x + B.rx, 0);
      gr.addColorStop(0, '#5e0410'); gr.addColorStop(0.35, '#c8142c'); gr.addColorStop(0.7, '#9a0c1f'); gr.addColorStop(1, '#4a030c');
      cx.fillStyle = gr;
      cx.beginPath(); cx.moveTo(x - B.rx, top); cx.lineTo(x - B.rx, B.sb); cx.ellipse(x, B.sb, B.rx, B.ry, 0, Math.PI, 0, true);
      cx.lineTo(x + B.rx, top); cx.closePath(); cx.fill();
      // the top of the cap: glossy red, brighter while pressed or live
      const lit = pr > 0 || ready;
      gr = cx.createRadialGradient(x - B.rx * 0.3, top - B.ry * 0.45, B.rx * 0.05, x, top, B.rx * 1.08);
      gr.addColorStop(0, lit ? '#ffc2b8' : '#ff948c'); gr.addColorStop(0.45, lit ? '#ff3048' : '#e8172f'); gr.addColorStop(1, lit ? '#b80d24' : '#8e0819');
      cx.fillStyle = gr; ellipse(x, top, B.rx, B.ry); cx.fill();
      cx.strokeStyle = 'rgba(255,214,214,.55)'; cx.lineWidth = 1; ellipse(x, top, B.rx, B.ry); cx.stroke();
      gr = cx.createRadialGradient(x - B.rx * 0.34, top - B.ry * 0.34, 0, x - B.rx * 0.34, top - B.ry * 0.34, B.rx * 0.42);
      gr.addColorStop(0, 'rgba(255,255,255,.8)'); gr.addColorStop(1, 'rgba(255,255,255,0)');
      cx.fillStyle = gr; ellipse(x - B.rx * 0.34, top - B.ry * 0.34, B.rx * 0.42, B.ry * 0.36); cx.fill();
    }
  }

  function stepRat(dt, now, smp, playing) {
    const R = S.rat, L = S.lanes;
    let target = Math.min(L - 1, Math.max(0, Math.floor(L / 2 - 0.5))) + 0.5;
    const c = playing && smp ? smp.cursor : null;
    if (c) { const u = clamp(c[0], 0, 0.9999) * L, k = Math.floor(u); target = k + 0.5 + (u - k - 0.5) * 0.3; }
    if (now - R.pressAt < 0.3 && R.pressLane !== null) target = R.pressLane + 0.5;
    if (RM.matches) { R.x = target; R.v = 0; }
    else {
      const w = 15;
      let t = Math.min(dt, 0.1);
      while (t > 1e-4) { const h = Math.min(t, 1 / 120), acc = w * w * (target - R.x) - 2 * w * R.v; R.v += acc * h; R.x += R.v * h; t -= h; }
    }
    if (R.v > 0.6) R.face = -1; else if (R.v < -0.6) R.face = 1;
    R.faceK = RM.matches ? R.face : R.faceK + (R.face - R.faceK) * Math.min(1, dt * 14);
    R.run += Math.abs(R.v) * dt * 5.2;
  }
  function drawRat(g, B, now, L, still) {
    const R = S.rat, feet = B.sb + (H - B.sb) * 0.84;
    const lw = 2 * g.half(feet) / L;
    const w = Math.min(lw * 0.96, H * 0.36, W * 0.44), h = w * (V.ratH || 650) / (V.ratW || 1150);
    const x = clamp(g.X(R.x / L, feet), w * 0.5 + 4, W - w * 0.5 - 4), pa = now - R.pressAt;   // kept whole on the screen
    let sq = 0, lift = 0;
    if (!still && pa >= 0 && pa < 0.46) {           // a press: crouch, hop up at the button, land
      if (pa < 0.07) sq = 0.16 * (pa / 0.07);
      else if (pa < 0.22) { const k = (pa - 0.07) / 0.15; sq = 0.16 - 0.3 * Math.sin(k * Math.PI / 2); lift = Math.sin(k * Math.PI) * h * 0.42; }
      else { const k = (pa - 0.22) / 0.24; sq = 0.14 * Math.sin(k * Math.PI) * (1 - k * 0.5) - 0.14 * (1 - k) * (k < 0.15 ? 1 : 0); }
    }
    const bob = still ? 0 : Math.sin(now * 2.4) * h * 0.012 + Math.abs(Math.sin(R.run)) * h * 0.07 * clamp(Math.abs(R.v) / 2.5, 0, 1);
    const shs = 1 - clamp(lift / h, 0, 0.6) * 0.6;
    cx.fillStyle = 'rgba(40,8,80,.3)'; ellipse(x, feet, w * 0.42 * shs, h * 0.085 * shs); cx.fill();
    if (!V.rat) return;
    const sc = w / V.ratW, p = V.ratPad * sc;
    cx.save();
    cx.translate(x, feet - lift - bob);
    const fk = Math.abs(R.faceK) < 0.15 ? (R.faceK < 0 ? -0.15 : 0.15) : R.faceK;
    cx.scale(fk * (1 + sq * 0.55), 1 - sq);
    if (!V.ratKeyed) cx.globalCompositeOperation = 'screen';
    cx.drawImage(V.rat, -w / 2 - p, -h * 0.96 - p, w + 2 * p, h + 2 * p);   // its paws (4% above the crop's edge) on the row
    cx.restore();
    cx.globalCompositeOperation = 'source-over';
  }

  function drawFx(g, B, now, L, still) {
    const fs = clamp(B.lw * 0.26, 15, 34);
    for (const f of S.fx) {
      const age = now - f.at;
      if (age < 0) continue;
      const bx = g.X((f.lane + 0.5) / L, B.sb), by = B.sb - B.d;
      if (f.kind === 'hit') {
        if (age < 0.32) glow(bx, by, B.rx * (2.4 + 4 * age), '255,190,40', 0.8 * (1 - age / 0.32));
        if (age < 0.5) {
          const k = age / 0.5;
          cx.strokeStyle = `rgba(255,224,130,${(1 - k).toFixed(3)})`; cx.lineWidth = 3.5 * (1 - k) + 0.8;
          ellipse(bx, B.sb - B.d * 0.4, B.rx * (1.2 + 2.4 * k), B.ry * (1.2 + 2.4 * k)); cx.stroke();
        }
        if (f.parts && age < 0.85) {             // sparks: saturated golds and a few magentas, so they read on white
          const fa = 1 - age / 0.85;
          for (const p of f.parts) {
            const x = bx + Math.cos(p.a) * p.sp * H * age, y = by + Math.sin(p.a) * p.sp * H * age + 0.7 * H * age * age;
            const r = p.r * (0.7 + fa * 0.7) * clamp(H / 600, 0.8, 1.6);
            cx.fillStyle = p.c < 0.45 ? `rgba(255,176,24,${fa})` : p.c < 0.8 ? `rgba(255,214,40,${fa})` : `rgba(236,40,150,${fa})`;
            cx.beginPath(); cx.moveTo(x, y - r * 2.2); cx.lineTo(x + r * 0.6, y - r * 0.6); cx.lineTo(x + r * 2.2, y);
            cx.lineTo(x + r * 0.6, y + r * 0.6); cx.lineTo(x, y + r * 2.2); cx.lineTo(x - r * 0.6, y + r * 0.6);
            cx.lineTo(x - r * 2.2, y); cx.lineTo(x - r * 0.6, y - r * 0.6); cx.closePath(); cx.fill();
          }
        }
        if (age < 1.05) {
          const k = age / 1.05, al = k < 0.65 ? 1 : 1 - (k - 0.65) / 0.35;
          const rise = still ? 0 : H * 0.08 * (1 - Math.pow(1 - Math.min(1, age * 2), 3));
          const y = by - B.ry * 2.6 - rise, pop = still ? 1 : 1 + 0.35 * Math.max(0, 1 - age / 0.14);
          cx.globalAlpha = al; cx.textAlign = 'center'; cx.textBaseline = 'alphabetic';
          if (f.judge) {
            cx.font = `700 ${Math.round(fs * 0.5)}px "JetBrains Mono", ui-monospace, monospace`;
            cx.lineWidth = 3; cx.strokeStyle = 'rgba(60,8,90,.65)'; cx.strokeText(f.judge.toUpperCase(), bx, y - fs * 1.05 * pop);
            cx.fillStyle = f.judge === 'Perfect' ? '#fff37a' : '#ffd0ec'; cx.fillText(f.judge.toUpperCase(), bx, y - fs * 1.05 * pop);
          }
          if (f.label) {
            cx.font = `700 ${Math.round(fs * pop)}px "Space Grotesk", system-ui, sans-serif`;
            cx.lineWidth = 4; cx.strokeStyle = 'rgba(120,30,20,.55)'; cx.strokeText(f.label, bx, y);
            const gr = cx.createLinearGradient(0, y - fs, 0, y);
            gr.addColorStop(0, '#ffffff'); gr.addColorStop(1, '#ffc440');
            cx.fillStyle = gr; cx.fillText(f.label, bx, y);
          }
          cx.globalAlpha = 1;
        }
      } else if (f.kind === 'miss') {
        const dur = 0.8;
        if (age < dur) {
          const fa = 1 - age / dur;
          if (f.parts) for (const p of f.parts) {
            const x = bx + Math.cos(p.a) * p.sp * H * age * 1.4, y = B.sb + Math.sin(p.a) * p.sp * H * age * 0.6 - 0.06 * H * age;
            cx.fillStyle = `rgba(186,176,200,${(0.5 * fa).toFixed(3)})`;
            cx.beginPath(); cx.arc(x, y, p.r * H * (1 + age * 2.2), 0, TAU); cx.fill();
          } else { cx.fillStyle = `rgba(186,176,200,${(0.4 * fa).toFixed(3)})`; ellipse(bx, B.sb, B.rx * 1.6, B.ry * 1.6); cx.fill(); }
          cx.globalAlpha = fa; cx.textAlign = 'center'; cx.textBaseline = 'alphabetic';
          cx.font = `700 ${Math.round(fs * 0.5)}px "JetBrains Mono", ui-monospace, monospace`;
          cx.lineWidth = 3; cx.strokeStyle = 'rgba(255,255,255,.85)';
          const y = B.sb - B.d - B.ry * 2.2 - (still ? 0 : H * 0.03 * age);
          cx.strokeText('MISS', bx, y); cx.fillStyle = '#5a5070'; cx.fillText('MISS', bx, y);
          cx.globalAlpha = 1;
        }
      } else if (f.kind === 'wrong') {
        const dur = 0.6;
        if (age < dur) {
          const k = age / dur;
          cx.strokeStyle = `rgba(255,60,80,${(1 - k).toFixed(3)})`; cx.lineWidth = 3;
          ellipse(bx, B.sb - B.d * 0.5, B.rx * (1.3 + 1.2 * k), B.ry * (1.3 + 1.2 * k)); cx.stroke();
          const s = fs * 0.32, y = by - B.ry * 2.4;
          cx.lineWidth = 3.2; cx.lineCap = 'round';
          cx.beginPath(); cx.moveTo(bx - s, y - s); cx.lineTo(bx + s, y + s); cx.moveTo(bx + s, y - s); cx.lineTo(bx - s, y + s); cx.stroke();
        }
      }
    }
  }

  function chip(x, y, text, align) {
    cx.font = `600 ${Math.round(clamp(H * 0.024, 10.5, 13))}px "Space Grotesk", system-ui, sans-serif`;
    const tw = cx.measureText(text).width, ph = Math.round(clamp(H * 0.045, 20, 26)), pw = tw + 20;
    const x0 = align === 'right' ? x - pw : x;
    cx.fillStyle = 'rgba(22,6,48,.5)';
    cx.beginPath();
    if (cx.roundRect) cx.roundRect(x0, y, pw, ph, ph / 2); else cx.rect(x0, y, pw, ph);
    cx.fill();
    cx.strokeStyle = 'rgba(255,220,250,.22)'; cx.lineWidth = 1; cx.stroke();
    cx.fillStyle = '#fff4fb'; cx.textAlign = 'left'; cx.textBaseline = 'middle';
    cx.fillText(text, x0 + 10, y + ph / 2 + 0.5);
  }
  function hud(g, now, still) {
    const song = S.song ? SONGS.get(S.song) : null;
    if (song) {
      const p = clamp((S.noteI || 0) / song.notes.length, 0, 1);
      cx.fillStyle = 'rgba(255,255,255,.14)'; cx.fillRect(0, 0, W, 3);
      const gr = cx.createLinearGradient(0, 0, W, 0);
      gr.addColorStop(0, '#D60C94'); gr.addColorStop(0.6, '#F5AC29'); gr.addColorStop(1, '#FCF010');
      cx.fillStyle = gr; cx.fillRect(0, 0, W * p, 3);
    }
    const pad = clamp(W * 0.02, 8, 14);
    const title = song ? song.title : S.song ? S.song.replace(/_/g, ' ') : '';
    if (title) chip(pad, pad + 4, W < 440 && title.length > 18 ? title.slice(0, 17) + '…' : title, 'left');
    const n = S.hits + S.misses + S.wrong;
    if (n) chip(W - pad, pad + 4, ratePct(S.hits, n) + ' hit rate', 'right');
    if (S.streak >= 2) {
      const pop = still ? 0 : clamp(1 - (now - S.streakAt) / 0.22, 0, 1);
      const fs = clamp(H * 0.08, 26, 56) * (1 + 0.2 * pop), y = pad + 4 + clamp(H * 0.045, 20, 26) + fs * 0.9;
      cx.textAlign = 'center'; cx.textBaseline = 'alphabetic';
      cx.font = `700 ${Math.round(fs)}px "Space Grotesk", system-ui, sans-serif`;
      cx.lineWidth = 5; cx.strokeStyle = 'rgba(60,8,100,.5)'; cx.strokeText(String(S.streak), W / 2, y);
      const gr = cx.createLinearGradient(0, y - fs * 0.8, 0, y);
      gr.addColorStop(0, '#fffbe6'); gr.addColorStop(1, '#F5AC29');
      cx.fillStyle = gr; cx.fillText(String(S.streak), W / 2, y);
      cx.font = `700 ${Math.round(clamp(H * 0.02, 9, 11))}px "JetBrains Mono", ui-monospace, monospace`;
      const ly = y + clamp(H * 0.03, 12, 18);
      cx.lineWidth = 3; cx.strokeStyle = 'rgba(70,10,100,.7)'; cx.strokeText('S T R E A K', W / 2, ly);
      cx.fillStyle = '#fff'; cx.fillText('S T R E A K', W / 2, ly);
    }
  }

  function draw(now, m, dt) {
    cx.setTransform(dpr, 0, 0, dpr, 0, 0);
    cx.lineJoin = 'round'; cx.miterLimit = 2;          // no miter spikes on outlined text
    const playing = m === 'live' || m === 'test', still = RM.matches, L = S.lanes;
    sprites();
    const g = V.g && V.g.W === W && V.g.H === H ? V.g : (V.g = geom());
    layers(g, L);
    const smp = playing && S.snaps.length ? sample() : null;
    stepRat(dt, now, smp, playing);
    const B = btnGeom(g, L);
    cx.drawImage(V.sky, 0, 0, W, H);
    ambient(g, now, still);
    cx.drawImage(V.track, 0, 0, W, H);
    beatLines(g, now, playing, still);
    laneBeam(g, B, L, smp);
    if (smp) drawTiles(g, smp, now, L);
    drawButtons(g, B, now, L, smp, still);
    drawRat(g, B, now, L, still);
    drawFx(g, B, now, L, still);
    if (playing) hud(g, now, still);
  }
  function size() {
    const r = E.screen.getBoundingClientRect();
    dpr = Math.min(2, window.devicePixelRatio || 1);
    W = Math.max(1, r.width); H = Math.max(1, r.height);
    E.cv.width = Math.round(W * dpr); E.cv.height = Math.round(H * dpr);
    V.g = null;
    const now = performance.now() / 1000;
    draw(now, mode(now), 0);
  }
  function frame(ms) {
    raf = 0;
    const now = ms / 1000, m = mode(now), playing = m === 'live' || m === 'test';
    pump(now);
    const busy = playing || S.fx.length > 0, animate = busy || !RM.matches;
    // between sessions the sky still drifts, at half the frame rate
    if (busy || now - lastDraw > 1 / 31 || m !== lastMode) {
      draw(now, m, lastDraw ? clamp(now - lastDraw, 0, 0.1) : 0.016);
      lastDraw = now;
    }
    if (m !== lastMode) renderUi();
    if ((animate || S.pending.length) && visible && !document.hidden) raf = requestAnimationFrame(frame);
  }
  function kick() {
    if (panel && !raf && visible && !document.hidden) raf = requestAnimationFrame(frame);
  }

  /* ---- the text around the board */
  function renderUi() {
    if (!panel) return;
    const now = performance.now() / 1000, m = mode(now);
    const changed = m !== lastMode;
    lastMode = m;
    sec.dataset.state = m;
    const live = S.st && S.st.live;
    const B = { connecting: ['conn', 'CONNECTING'], live: ['live', 'LIVE TRAINING'], test: ['test', 'TEST STREAM'],
                wait: live ? ['live', 'LIVE TRAINING'] : ['test', 'TEST STREAM'], standby: ['off static', 'STANDBY'] }[m];
    E.badge.className = 'badge ' + B[0]; put(E.badgeT, B[1]);
    const other = S.st && S.st.streaming && S.st.task !== 'tiles' ? (TASKS[S.st.task] ? TASKS[S.st.task].name : 'another task') : null;
    put(E.conn, m === 'live' || m === 'test' ? 'playing now' : m === 'wait' ? 'starting'
      : other ? 'training on another task' : !S.open && S.wasOpen ? 'reconnecting' : '');
    let t = '', s = '';
    if (m === 'connecting') t = 'Connecting to the lab';
    else if (m === 'wait') { t = 'R-01 is about to play'; s = 'The first tiles appear in a moment.'; }
    else if (m === 'standby') {
      t = 'R-01 isn’t playing right now';
      s = other ? 'It is training on another task (' + other + '). Rat Tiles sessions appear here while they stream.'
        : 'The next session will appear here.';
      if (S.had && S.hits + S.misses > 0) s = 'Last session: ' + plural(S.hits, 'tile', 'tiles') + ' hit, ' + S.misses + ' missed. ' + s;
    }
    put(E.emptyT, t); put(E.emptyS, s);
    const how = 'R-01’s cursor (where its head points) picks the lane, and its lever press presses that lane’s button; the rat on the runway stands in the cursor’s lane.';
    put(E.cap, m === 'live' ? 'Live training: the newest saved checkpoint of run ' + (S.st.run || '') + ', playing Rat Tiles in its own simulation. ' + how
      : m === 'test' ? 'Test stream, not a live training run. ' + how : '');

    // the melody
    const song = S.song ? SONGS.get(S.song) : null;
    const playing = m === 'live' || m === 'test';
    if (song || S.song) {
      put(E.songK, playing ? 'Now playing' : 'Last melody');
      put(E.song, song ? song.title : S.song.replace(/_/g, ' '));
      put(E.comp, song ? song.composer : ''); E.pd.hidden = !(song && song.pd);
    } else {
      put(E.songK, 'Melodies');
      put(E.song, 'Public-domain tunes');
      put(E.comp, SONGS.size ? Array.from(SONGS.values()).map(x => x.title).join(' · ') : ''); E.pd.hidden = true;
    }
    const key = song ? song.id + ':' + song.notes.length : '';
    if (key !== stripKey) {
      stripKey = key;
      E.notes.innerHTML = song ? song.notes.map(n => '<i style="--h:' + ((n.midi - song.lo) / Math.max(1, song.hi - song.lo)).toFixed(3) + '"></i>').join('') : '';
      bars = Array.from(E.notes.children);
    }
    let nh = 0, nm = 0;
    bars.forEach((b, i) => {
      const r = S.results.get(i);
      if (r === 'hit') nh++; else if (r === 'miss') nm++;
      const cls = r === 'hit' ? 'hit' : r === 'miss' ? 'miss' : playing && i === S.noteI ? 'now'
        : S.firstSeen !== null && i < S.firstSeen ? 'pre' : '';
      if (b.className !== cls) b.className = cls;
    });
    if (song) {
      const cur = S.noteI !== null && S.noteI < song.notes.length ? S.noteI : null;
      put(E.notesT, (playing && cur !== null ? 'Note ' + (cur + 1) + ' of ' + song.notes.length + ' (' + song.notes[cur].name + ') · ' : '') +
        nh + ' played, ' + nm + ' missed');
    } else put(E.notesT, '');

    // the score
    const any = S.had || S.hits + S.misses + S.wrong > 0, n = S.hits + S.misses + S.wrong;
    put(E.hits, any ? S.hits : '—'); put(E.miss, any ? S.misses : '—');
    put(E.rate, any ? ratePct(S.hits, n) : '—'); put(E.best, any ? S.best : '—');
    const sub = [];
    if (S.speed && (playing || m === 'wait')) sub.push('A tile reaches the buttons ' + (S.hitY / S.speed).toFixed(1) + ' s after it appears');
    if (S.wrong) sub.push(plural(S.wrong, 'press', 'presses') + ' with no tile on the button');
    if (S.streak > 1) sub.push('streak ' + S.streak);
    if (S.attempt && S.attempt.hits !== null) sub.push('Last attempt: ' + plural(S.attempt.hits, 'tile', 'tiles') + ' hit');
    put(E.sub, sub.join(' · '));

    // sound
    E.listen.hidden = S.sound; E.mute.hidden = !S.sound;
    E.mute.setAttribute('aria-pressed', String(Piano.muted));
    put(E.mute.lastElementChild, Piano.muted ? 'Muted' : 'Sound on');
    E.bb.hidden = !S.bbTiles;
    if (changed && !raf) { const t2 = performance.now() / 1000; draw(t2, m, 0); kick(); }
  }

  function teaserUpdate() {
    if (!teaser) return;
    const st = S.st, on = !!(st && st.streaming && st.task === 'tiles');
    const state = on ? (st.live ? 'live' : 'test') : 'idle';
    if (teaser.dataset.state !== state) teaser.dataset.state = state;
    put(teaserSt, state === 'live' ? 'Playing now' : state === 'test' ? 'Test stream' : 'New');
  }

  /* the panel's own socket (when the 3D view cannot start): the training channel's messages, read as live.js would */
  let rawHello = null;
  function onRaw(m) {
    if (!m || typeof m !== 'object') return;
    switch (m.type) {
      case 'state': rawHello = m.live === true && m.hello && typeof m.hello === 'object' ? m.hello : null; break;
      case 'hello': rawHello = m; break;
      case 'bye': case 'idle': rawHello = null; break;
      case 'tiles': case 'tile': onTiles(m); return;
      case 'episode': onEpisode(m); return;
      default: return;
    }
    const h = rawHello;
    onStatus({ stream: h ? { live: !isTestHello(h), test: isTestHello(h), task: h.task, run: h.run } : null });
  }
  function onBuyback(j) {
    const on = !!(j && Array.isArray(j.tasks) && j.tasks.includes('tiles'));
    if (on !== S.bbTiles) { S.bbTiles = on; if (panel) renderUi(); }
  }

  if (panel) {
    E.listen.addEventListener('click', () => {
      if (Piano.unlock()) { S.sound = true; Piano.setMuted(false); }
      else { put(E.listen, 'Sound is not available in this browser'); E.listen.disabled = true; }
      renderUi();
      if (S.sound) E.mute.focus();
    });
    E.mute.addEventListener('click', () => { Piano.setMuted(!Piano.muted); renderUi(); });
    if (window.ResizeObserver) new ResizeObserver(size).observe(E.screen); else addEventListener('resize', size);
    if ('IntersectionObserver' in window) new IntersectionObserver(es => { visible = es[0].isIntersecting; kick(); }).observe(E.screen);
    document.addEventListener('visibilitychange', kick);
    if (RM.addEventListener) RM.addEventListener('change', () => { V.key = ''; kick(); size(); });
    // tile events and sound keep going while the board is scrolled away (the frame loop only runs while it is visible)
    setInterval(() => { const now = performance.now() / 1000; pump(now); if (mode(now) !== lastMode) renderUi(); }, 200);
    loadSongs();
    size();
    renderUi();
    kick();
  }
  teaserUpdate();
  return { onStatus, onTiles, onEpisode, onRelay, onRaw, onBuyback };
})();

/* ------------------------------------------------------------------ rat buybacks (buyback.js)
   Reads the buyback engine's public status JSON. One buy an hour, on the hour (UTC), sized by that hour's hit rate:
   the hourly budget x hit rate, where hit rate = hits / (hits + misses + wrong presses) and the caps clamp the result.
     window       {start, end, hits, misses, wrong, hit_rate}     the hour being counted now
     last_window  {... the same, plus buy: its buy}               the hour that ended last
     next_buy_at  ISO time of the next buy;  budget {hourly_eth: null|"0.01", rule}   (null: not set yet)
   An engine without these fields (the older one) still shows its totals and its next buy.
   Every figure carries its mode: anything not executed on-chain is labelled "Simulated" and never called a buy; a
   preview amount (while the budget is not set) is marked as one; a test stream is never called live; a stale status
   is shown as stale; no address-like string is ever shown.
   LIVE (the engine's live bookings: mode "LIVE", buys.simulated false): the banner says "Buybacks live". Each hour's
   buy is booked (state "booked": not executed yet, never shown as bought) and executed by the rat on pons; only an
   entry the engine verified on chain (state "executed", its 0x + 64-hex tx hash) is shown as a buy, with its explorer
   link and "clicked by the rat on pons". The tx hash is used only in that link's href, checked character by character. */
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
  const obj = v => (v && typeof v === 'object' && !Array.isArray(v)) ? v : null;
  const isoMs = s => (typeof s === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?Z$/.test(s)) ? Date.parse(s) : NaN;
  const hm = ms => new Date(ms).toISOString().slice(11, 16);
  const fmtTok = s => { const d = dec(s); if (d === null) return '—'; const n = Number(d);
    return n >= 100 ? Math.round(n).toLocaleString('en-US') : n.toLocaleString('en-US', { maximumFractionDigits: 2 }); };
  const fmtEth = s => { const d = dec(s); return d === null ? '—' : d + ' ETH'; };
  const fmtN = v => v.toLocaleString('en-US');
  // a window's hit rate: the engine's figure, else hits / (hits + misses + wrong)
  const rateOf = w => {
    if (!w) return null;
    const r = typeof w.hit_rate === 'string' && /^(0(\.\d{1,8})?|1(\.0{1,8})?)$/.test(w.hit_rate) ? Number(w.hit_rate) : w.hit_rate;
    if (typeof r === 'number' && isFinite(r) && r >= 0 && r <= 1) return r;
    const h = int(w.hits), m = int(w.misses), x = int(w.wrong);
    return h === null || m === null || x === null || h + m + x === 0 ? null : h / (h + m + x);
  };
  const pctTxt = r => r === null ? '—' : (r * 100).toFixed(r === 0 || r === 1 ? 0 : 1) + '%';
  const E = { mode: $('#bb-mode'), test: $('#bb-test'), conn: $('#bb-conn'), lever: $('#bb-lever'),
              winT: $('#bb-win-t'), next: $('#bb-next'), bar: $('#bb-win-bar'),
              wHits: $('#bb-w-hits'), wMiss: $('#bb-w-miss'), wWrong: $('#bb-w-wrong'), wRate: $('#bb-w-rate'),
              fBudget: $('#bb-f-budget'), fSub: $('#bb-f-sub'), fRate: $('#bb-f-rate'), fK: $('#bb-f-k'), fBuy: $('#bb-f-buy'), fNote: $('#bb-f-note'), fEq: $('#bb-f-eq'),
              last: $('#bb-last'), lastK: $('#bb-last-k'), lastStats: $('#bb-last-stats'), lastBuy: $('#bb-last-buy'),
              hits: $('#bb-hits'), buysK: $('#bb-buys-k'), buys: $('#bb-buys'), outK: $('#bb-out-k'), out: $('#bb-out'),
              list: $('#bb-list'), note: $('#bb-note') };
  if (Object.values(E).some(x => !x)) return;
  // the banner at the top of /buyback (optional: another page may not have it)
  const BN = { box: $('#bbk-banner'), pill: $('#bbk-pill'), text: $('#bbk-banner-t') };
  const BANNER_DRY = BN.pill && BN.text ? { pill: BN.pill.textContent, text: BN.text.textContent } : null;
  const BANNER_LIVE = { pill: 'Buybacks live',
    text: 'Every hour, the rat clicks that hour’s $LABRAT buyback through on the real pons page. The rig checks ' +
          'the transaction before it is sent from the buyback wallet below, and every buy is verified on chain and ' +
          'linked to its transaction on the explorer.' };
  const TX = /^0x[0-9a-f]{64}$/;                           // a transaction hash, and nothing else, becomes a link
  const TX_URL = 'https://robinhoodchain.blockscout.com/tx/';
  const txLink = h => TX.test(h) ? '<a class="bb-tx" href="' + TX_URL + h + '" target="_blank" rel="noopener">View transaction</a>' : '';
  const STALE_S = 90;          // the engine rewrites its status at least every 10 s; older than this = not running
  const NOTE = /^[a-z][a-z ;.-]{0,59}$/;   // short fixed phrases from the engine
  // an engine note shown as text: plain words only, nothing address- or number-heavy
  const safeNote = s => (typeof s === 'string' && s.length <= 140 && /^[A-Za-z][A-Za-z0-9 ,;:.%()'’\/-]*$/.test(s) && !/0x/i.test(s)) ? s.replace(/\.$/, '') : '';
  wrap.hidden = false;
  let last = null;             // the newest status, for the countdown between polls

  function tick() {            // the hour's progress bar and the countdown to the next buy (every second)
    const j = last;
    if (!j) return;
    const upd = isoMs(j.updated), stale = !Number.isFinite(upd) || (Date.now() - upd) / 1000 > STALE_S;
    const w = obj(j.window), s = w ? isoMs(w.start) : NaN, e = w ? isoMs(w.end) : NaN, now = Date.now();
    const frac = Number.isFinite(s) && Number.isFinite(e) && e > s ? Math.min(1, Math.max(0, (now - s) / (e - s))) : 0;
    E.bar.style.transform = 'scaleX(' + frac.toFixed(4) + ')';
    const stopped = txt(j.stopped);
    let at = isoMs(j.next_buy_at);
    if (!Number.isFinite(at) && int(j.next_buy_in_s) !== null && Number.isFinite(upd)) at = upd + j.next_buy_in_s * 1000;
    let t = '';
    if (stopped) t = 'Buys stopped';
    else if (stale) t = 'Status not updating';
    else if (Number.isFinite(at)) {
      const left = Math.max(0, Math.round((at - now) / 1000));
      const mm = Math.floor(left / 60), ss = left % 60;
      t = 'Next buy ' + hm(at) + ' UTC · ' + (left <= 0 ? 'due now' : 'in ' + mm + ':' + String(ss).padStart(2, '0'));
    }
    if (E.next.textContent !== t) E.next.textContent = t;
  }

  function banner(live) {
    if (!BANNER_DRY) return;
    const b = live ? BANNER_LIVE : BANNER_DRY;
    if (BN.pill.textContent !== b.pill) BN.pill.textContent = b.pill;
    if (BN.text.textContent !== b.text) BN.text.textContent = b.text;
    BN.box.classList.toggle('live', live);
  }

  function render(j) {
    last = j;
    const live = j.mode === 'LIVE';
    banner(live);
    E.mode.textContent = live ? 'LIVE' : 'Simulated';
    E.mode.classList.toggle('dry', !live);
    const rl = j.relay || {}, src = j.source || {};
    // a TEST stream, test streams accepted, another relay, or an engine too old to say: never shown as live hits
    const test = src.test !== false || src.public_relay !== true || src.accept_test_streams === true || rl.test_stream === true;
    E.test.hidden = !test;
    const upd = isoMs(j.updated);
    const stale = !Number.isFinite(upd) || (Date.now() - upd) / 1000 > STALE_S;
    E.lever.hidden = !(Array.isArray(j.tasks) && j.tasks.includes('lever'));
    const b = obj(j.buys) || {}, simulated = !live || b.simulated !== false;

    // this hour
    const w = obj(j.window), ws = w ? isoMs(w.start) : NaN, we = w ? isoMs(w.end) : NaN;
    E.winT.textContent = Number.isFinite(ws) && Number.isFinite(we) ? hm(ws) + '–' + hm(we) + ' UTC' : '';
    const n = v => int(v) !== null ? fmtN(v) : '—';
    E.wHits.textContent = w ? n(w.hits) : '—'; E.wMiss.textContent = w ? n(w.misses) : '—'; E.wWrong.textContent = w ? n(w.wrong) : '—';
    const rate = rateOf(w);
    E.wRate.textContent = pctTxt(rate);

    // the rule, with this hour's numbers: hourly budget x hit rate. Until the owner sets the budget, a nominal preview
    // budget (budget.preview_eth) stands in for it and every buy it sizes is a simulated preview
    const bud = obj(j.budget), budget = bud ? dec(bud.hourly_eth) : null;
    const nominal = !budget && bud ? dec(bud.preview_eth) : null;
    E.fBudget.textContent = budget ? budget + ' ETH' : 'To be announced';
    E.fBudget.classList.toggle('tba', !budget);
    E.fSub.textContent = nominal ? 'previews use ' + nominal + ' ETH' : '';
    E.fRate.textContent = pctTxt(rate);
    const base = budget || nominal;
    E.fEq.hidden = !base;
    const proj = w && dec(w.projected_eth);
    const amt = proj || (base && rate !== null ? (Number(base) * rate).toFixed(8).replace(/\.?0+$/, '') : null);
    if (budget) {
      E.fK.textContent = (simulated ? 'Simulated buy' : 'Buy') + ' on the hour';
      E.fBuy.textContent = amt ? (proj ? '' : '≈ ') + amt + ' ETH' : '—';
      E.fNote.textContent = 'At this hour’s hit rate so far, within the per-buy, hourly and daily caps; the buy is sized when the hour closes.';
    } else if (nominal) {
      E.fK.textContent = 'Simulated preview on the hour';
      E.fBuy.textContent = amt ? (proj ? '' : '≈ ') + amt + ' ETH' : '—';
      E.fNote.textContent = 'The hourly budget is announced when it is ready. Until then every hour is still counted, and a nominal ' +
        nominal + ' ETH stands in for the budget: each hour runs a simulated preview buy of it × the hit rate, marked as a preview, never sent.';
    } else {
      E.fK.textContent = 'Buy on the hour';
      E.fBuy.textContent = 'Set by the budget';
      E.fNote.textContent = 'The hourly budget is announced when it is ready. Until then every hour is still counted and each buy is simulated, never sent.';
    }

    // the last hour
    const lw = obj(j.last_window);
    E.last.hidden = !lw;
    if (lw) {
      const ls = isoMs(lw.start), le = isoMs(lw.end);
      E.lastK.textContent = 'Last hour' + (Number.isFinite(ls) && Number.isFinite(le) ? ' · ' + hm(ls) + '–' + hm(le) + ' UTC' : '');
      const lr = rateOf(lw);
      E.lastStats.textContent = [int(lw.hits) !== null ? fmtN(lw.hits) + ' hits' : '', int(lw.misses) !== null ? fmtN(lw.misses) + ' missed' : '',
        int(lw.wrong) !== null ? fmtN(lw.wrong) + ' off-tile' : '', 'hit rate ' + pctTxt(lr)].filter(Boolean).join(' · ');
      // its buy: {state: due | simulated | booked | bought | none | expired, note, eth_in, labrat_out, simulated,
      // preview, tx (LIVE, once verified)}
      const lb = obj(lw.buy) || {};
      const why = [lb.note, lw.note].map(safeNote).find(Boolean) || '';
      const prev = lb.preview === true || lw.preview === true;
      const bought = live && lb.simulated === false && lb.state === 'bought' && TX.test(lb.tx || '');
      const sim = !bought;
      const eth = dec(lb.eth_in) || dec(lw.amount_eth);
      const tok = dec(lb.labrat_out) && Number(lb.labrat_out) > 0 ? fmtTok(lb.labrat_out) : '';
      const kind = prev ? 'simulated preview buy' : sim ? 'simulated buy' : 'buy';
      let line = '';
      if (bought) line = 'Buy: ' + (eth ? eth + ' ETH' : '') + (tok ? ' → ' + tok + ' LABRAT' : '');
      else if (lb.state === 'simulated') line = kind[0].toUpperCase() + kind.slice(1) + ': ' + (eth ? eth + ' ETH' : '') + (tok ? ' → ' + tok + ' LABRAT' : '');
      else if (live && lb.state === 'booked') line = (eth ? 'Buy of ' + eth + ' ETH booked' : 'Buy booked') + ': the rat clicks it through on pons, not executed yet';
      else if (lb.state === 'due') line = (eth ? kind[0].toUpperCase() + kind.slice(1) + ' of ' + eth + ' ETH booked' : 'Buy booked') + ': the rat clicks it through on pons next';
      else if (lb.state === 'expired') line = (live ? 'The hour’s buy was not executed in time' : 'The hour’s buy was not completed in time') + (why ? ': ' + why : '');
      else if (lb.state === 'none' || (!eth && Object.keys(lb).length)) line = 'No buy this hour' + (why ? ': ' + why : '');
      else if (eth && !live) line = kind[0].toUpperCase() + kind.slice(1) + ': ' + eth + ' ETH' + (tok ? ' → ' + tok + ' LABRAT' : '');
      const p = obj(lb.pons);
      if (line && p && p.clicked_by_rat === true && (bought || lb.simulated !== false)) line += ' · clicked through by the rat on pons';
      E.lastBuy.innerHTML = esc(line) + (bought ? ' · ' + txLink(lb.tx) : '');
      E.lastBuy.hidden = !line;
    }

    // totals
    const h = obj(j.hits) || {};
    E.hits.textContent = int(h.counted) !== null ? fmtN(h.counted) : '—';
    E.buysK.textContent = simulated ? 'Simulated buys' : 'Buys';
    E.outK.textContent = simulated ? 'LABRAT (simulated)' : 'LABRAT bought';
    E.buys.textContent = int(b.count) !== null ? b.count + (dec(b.eth_in) ? ' · ' + b.eth_in + ' ETH' : '') : '—';
    E.out.textContent = fmtTok(b.labrat_out);
    const recent = Array.isArray(b.recent) ? b.recent.slice(0, 5) : [];
    E.list.innerHTML = recent.map(r => {
      if (!r || typeof r !== 'object') return '';
      const sim = !live || r.simulated !== false;
      // LIVE: a buy only once the engine verified it on chain (state executed, with its transaction hash)
      const executed = !sim && r.state === 'executed' && TX.test(r.tx || '');
      const at = txt(r.at) && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(r.at) ? r.at.slice(11, 16) + ' UTC' : '';
      const venue = r.venue === 'pool' ? 'pool' : r.venue === 'curve' ? 'curve' : '';
      const rr = typeof r.hit_rate === 'number' && isFinite(r.hit_rate) && r.hit_rate >= 0 && r.hit_rate <= 1 ? r.hit_rate : null;
      const rate = rr !== null ? ' <em>(hit rate ' + esc(pctTxt(rr)) + ')</em>' : '';
      if (!sim && !executed) {             // LIVE, booked or expired: never shown as bought
        const what = r.state === 'expired' ? 'not executed' : 'booked, not executed yet';
        return '<li><span class="bb-t">' + esc(at) + '</span><span>' + esc(fmtEth(r.eth_in)) + ' buy ' + what + rate +
          '</span></li>';
      }
      return '<li><span class="bb-t">' + esc(at) + '</span><span>' + (sim ? 'simulated buy' : 'buy') + ': ' +
        esc(fmtEth(r.eth_in)) + ' &rarr; ' + esc(fmtTok(r.labrat_out)) + ' LABRAT' + rate +
        (r.preview === true ? ' <em>preview amount</em>' : '') +
        (venue ? ' <em>' + venue + '</em>' : '') +
        (obj(r.pons) && r.pons.clicked_by_rat === true ? ' <em>clicked by the rat on pons</em>' : '') +
        (executed ? ' ' + txLink(r.tx) : '') +
        '</span></li>';
    }).join('');

    const stopped = txt(j.stopped);
    const counting = rl.counting === true && !stale;
    E.conn.textContent = stale ? 'status stale' : stopped ? 'buys stopped'
      : counting ? (test ? 'counting a test stream' : 'counting live play')
      : rl.connected === true ? 'waiting for live training' : 'not connected';
    E.conn.className = 'bb-conn' + (stale || stopped ? ' off' : counting && !test ? ' on' : '');
    const updTxt = Number.isFinite(upd) ? hm(upd) + ' UTC' : 'an unknown time';
    E.note.textContent = (stale ? 'This status has not updated since ' + updTxt + ': the counter may not be running. ' : '') +
      (test ? 'These counts come from a test stream. ' : '') +
      (stopped ? 'Buys stopped: ' + stopped + '. ' : '') +
      (simulated ? 'Buybacks shown are simulated against the live chain: checked, not sent. '
                 : 'A buy counts only once its transaction is verified on chain: sent from the buyback wallet, to the pons pool, for the booked amount, with the LABRAT received. ') +
      'Hits, misses and off-tile presses are counted as each training attempt ends; one buy an hour, on the hour (UTC).';
    tick();
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
      TILES.onBuyback(j);                // Rat Tiles says its play counts only if the engine counts the tiles task
    } catch (e) {
      E.conn.textContent = 'status unavailable'; E.conn.className = 'bb-conn off';
    }
    if (!document.hidden) timer = setTimeout(poll, 15000);
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden && !timer) poll(); });
  setInterval(() => { if (!document.hidden) tick(); }, 1000);
  poll();
})();

/* ------------------------------------------------------------------ mount the 3D viewer (js/live.js) */
setMode('connecting');
recordedReady.then(() => renderHUD());

// the page's own relay socket, for when the 3D view (and so its socket) is not there: the pons panel and Rat Tiles
const fallbackSocket = () => PONS.ownSocket({ onText: m => TILES.onRaw(m), onRelay: o => TILES.onRelay(o) });

(async function mountViewer() {
  const el = $('#live-view');
  let mod = null;
  try { mod = await import('./live.js'); }
  catch (e) { console.warn('labrat: live viewer unavailable -', e && e.message ? e.message : e); }
  if (!mod || typeof mod.mountLive !== 'function') { LIVE.failed = true; setMode('offline'); fallbackSocket(); return; }
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
      // Rat Tiles (a training run with task "tiles"): board snapshots and tile outcomes, and each attempt's result
      onTiles: m => { try { TILES.onTiles(m); } catch (e) { console.warn('labrat: onTiles', e); } },
      onEpisode: m => { try { TILES.onEpisode(m); } catch (e) { console.warn('labrat: onEpisode', e); } },
      onRelay: (open, gone) => { PONS.onRelay(open); TILES.onRelay(open); if (gone) fallbackSocket(); },
    });
    // null: the 3D view could not start here; the dot-rat placeholder stays up with the offline message
    if (!handle) { LIVE.failed = true; setMode('offline'); fallbackSocket(); return; }
    // if the viewer draws but never reports a status, do not leave the placeholder over it
    setTimeout(() => { if (!LIVE.phGone && !LIVE.failed) hidePlaceholder(); }, 5000);
  } catch (e) {
    console.warn('labrat: live viewer failed to start -', e && e.message ? e.message : e);
    LIVE.failed = true; setMode('offline'); fallbackSocket();
  }
})();
