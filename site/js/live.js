/* site/js/live.js - the home page's real-time 3D view of the virtual rat in its operant chamber.

   What it shows, honestly:
   - The rat is DeepMind's open-source rodent model (dm_control, Apache-2.0) simulated in MuJoCo. Its "brain" is
     two trained artificial neural networks. It is not a real rat and not a biological brain.
   - LIVE: only while a publisher (live/publish_training.py) is streaming a training run through the relay. What
     plays then is the latest saved training checkpoint, playing in its own simulation next to the training.
   - REPLAY: otherwise, a recorded launch session (site/replay/session.bin + session.json, written by
     live/export_replay.py; its "label" names it), in a loop up to its "loop_end". It is always labelled as a replay,
     never as live. The recorded timeline (the rig lighting a target, the rat's click, the rig typing / scrolling,
     the transaction check) is narrated on the HUD and the wall screen. No clip (session.json missing): STANDBY.
     The long waits where the rat only stands while the rig types play fast-forward (x4 by default), and the HUD
     says so; the clock always shows recorded session time.
   - TEST: a publisher's test stream (hello "test": true / label "TEST ...", local relays only) plays like a stream
     but is reported as {live: false, test: true, source: 'test'}: never called live.
   - The wall screen in the box is a SCHEMATIC: the cursor dot and the lit target rectangle from the frame data
     (frame fields [5..10]), not a picture of any website.

     import {mountLive} from './js/live.js';
     const handle = await mountLive(document.getElementById('live'), {
       onStatus: s => ...,      // {live, label, task, run, steps, source: 'training'|'test'|'replay'|'none', test?, waiting?,
                                //  loading? (source 'none' while the replay clip is still downloading)}
       onMetrics: (rows, info) => ...,   // rows: last <= 300 log.jsonl rows of the streamed run; info {live, row?}
       onEpisode: msg => ...,   // {type:'episode', n, presses, hits, misses, fell}
       onTarget: t => ...,      // replay: {n, total, label, lit, done}
       onEvent: e => ...,       // replay: the timeline entry now in effect {t, kind, text, until, target}
       onPons: m => ...,        // the relay's pons channel, text: {channel:'pons', type:'pons_hello'|'pons_step'|
                                //  'pons_result'|'pons_bye'|'pons_state'|'pons_idle', ...}; never used by this view
       onPonsFrame: buf => ..., // the pons channel's frames: ArrayBuffer b"PJPG" + a JPEG; never used by this view
       onRelay: (open, gone) => ..., // the relay socket opened (true) or closed (false); gone=true: this view was torn
                                //  down and will not reconnect (anything riding on its socket needs its own)
     });

   mountLive never throws: on failure it puts a short fallback line in the container and resolves to null.
   On success it resolves after the first frame to {dispose(), setQuality(tier), seek(t), get mode(), get status(),
   canvas}; seek(t) jumps the replay to recorded session time t (s).
   Options: relay (URL, or false for replay only; default window.LABRAT_RELAY), base (site root URL), loader
   (false: no loading message of its own, when the host page shows one),
   ratUrl / replayJson / replayBin (asset overrides), quality (0|1|2 forced tier), hud (false: no overlay; 'full':
   also the mode pill, label and caption), marginBottom (px kept clear at the bottom), replayFastForward (1 = off).
   Test hooks: window.__labratFrames (rendered frames), window.__labratMode ('live' | 'replay' | 'none'; 'live' =
   playing the relay's stream, test streams included), window.__labratLiveFrames (live frames received).

   three.js 0.160.0 comes from the page's import map ("three", pinned by sha384); no addons are used, so
   everything this module loads is covered by that integrity pin. */

const THREE_URL = 'https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js';
const HDR = 12;                        // header floats before the bones
const MAGIC = 7.0;
const PAGE_W = 1280, PAGE_H = 900;     // the page viewport the cursor coordinates are normalised to
const LIVE_DELAY = 0.15;               // s: live playback runs this far behind the newest frame (jitter buffer)
const RETRY_MS = 10000;                // relay reconnect interval
const STALL_MS = 20000;                // live but no frames for this long: fall back to the replay
const DPR_MAX = 1.5;
const TIERS = [
  {shells: 10, dpr: 1.0, bloom: 0.25, shadow: 1024, dust: 0},
  {shells: 16, dpr: 1.25, bloom: 0.5, shadow: 1024, dust: 110},
  {shells: 24, dpr: 1.5, bloom: 0.5, shadow: 2048, dust: 220},
];
const PAL = {yellow: 0xFFF40F, goldy: 0xFCF010, gold: 0xF5AC29, orange: 0xE86E3D, coral: 0xEA3560,
  magenta: 0xD60C94, violet: 0x8808B5, purple: 0x4B04C4, blue: 0x1308AE};
const LIVE_NOTE = 'latest saved checkpoint, playing in its own simulation';
const CAPTION = 'Virtual rat: DeepMind’s open-source rodent model (Apache-2.0) in MuJoCo, driven by two ' +
  'trained artificial neural networks, not a real brain. The wall screen is a schematic.';
const CAPTION_SHORT = 'Virtual rat (DeepMind rodent model, MuJoCo) · artificial neural networks, not a real brain';
// tags for the recorded session's timeline kinds (session.json "timeline")
const KIND = {start: 'SESSION', brain_on: 'BRAIN ON', lit: 'TARGET LIT', click: 'RAT CLICK', image: 'RIG', typing: 'RIG TYPES',
  scroll: 'RIG SCROLLS', tx_requested: 'TRANSACTION', tx_checked: 'CHECKED', tx_refused: 'NOT SIGNED',
  tx_signed: 'SIGNED', tx_sent: 'BROADCAST', tx_mined: 'MINED'};
const REPLAY_LABEL = 'Replay: a recorded launch session';   // when session.json carries no label of its own

/* ================================================================ entry point */
export async function mountLive(el, opts = {}) {
  const st = {disposed: false, cleanup: []};
  try {
    if (!el || !el.appendChild) throw new Error('no container element');
    return await mount(el, opts || {}, st);
  } catch (e) {
    console.warn('labrat live: 3D view unavailable -', e && e.message ? e.message : e);
    try { teardown(st); } catch (_) { /* ignore */ }
    try { if (el && el.appendChild) showFallback(el); } catch (_) { /* ignore */ }
    try { window.__labratMode = 'none'; } catch (_) { /* ignore */ }
    return null;
  }
}

function teardown(st) {
  st.disposed = true;
  for (const f of st.cleanup.splice(0).reverse()) { try { f(); } catch (_) { /* ignore */ } }
}

function showFallback(el) {
  for (const n of el.querySelectorAll(':scope > .lr-canvas, :scope > .lr-hud, :scope > .lr-loading')) n.remove();
  if (el.querySelector(':scope > .lr-fallback')) return;
  injectCss();
  const d = document.createElement('div');
  d.className = 'lr-fallback';
  d.innerHTML = '<b>3D view unavailable</b><span>This browser could not start WebGL 2, so the rat’s chamber ' +
    'cannot be drawn here. Everything else on the page still works.</span>';
  if (getComputedStyle(el).position === 'static') el.style.position = 'relative';
  el.appendChild(d);
}

/* ================================================================ small helpers */
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
// a publisher's test stream (hello "test": true, or a label starting with TEST): shown, but never called live
const isTestHello = h => !!h && typeof h === 'object' && (h.test === true || /^\s*TEST\b/.test(String(h.label || '')));
const safe = (fn, ...a) => { try { if (typeof fn === 'function') fn(...a); } catch (e) { console.warn('labrat live: callback failed -', e); } };
// a frame of the relay's pons channel: b"PJPG" + a JPEG (never a rat frame, whatever its size)
const isPonsFrame = b => b instanceof ArrayBuffer && b.byteLength >= 4 &&
  (v => v[0] === 0x50 && v[1] === 0x4A && v[2] === 0x50 && v[3] === 0x47)(new Uint8Array(b, 0, 4));

async function loadThree() {
  try { return await import('three'); }
  catch (e) { return await import(THREE_URL); }      // no import map on the page: the same pinned build
}

async function getJSON(url) {
  const r = await fetch(url, {cache: 'no-cache'});
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}

function paths(opts) {
  let root;
  try { root = opts.base ? new URL(opts.base, location.href) : new URL('../', import.meta.url); }
  catch (_) { root = new URL('./', location.href); }
  const u = p => new URL(p, root).href;
  return {rat: opts.ratUrl || u('assets/rat.json'), replayJson: opts.replayJson || u('replay/session.json'),
    replayBin: opts.replayBin || u('replay/session.bin')};
}

function relayUrl(opts) {
  if (opts.relay === false || opts.relay === null) return null;
  let r = opts.relay || window.LABRAT_RELAY;
  if (!r) {
    const h = location.hostname;
    if (location.protocol === 'file:' || h === 'localhost' || h === '127.0.0.1') r = 'ws://localhost:4720/live';
    else r = '/live';
  }
  try {
    const u = new URL(r, location.href);
    if (u.protocol === 'http:') u.protocol = 'ws:';
    if (u.protocol === 'https:') u.protocol = 'wss:';
    return u.protocol === 'ws:' || u.protocol === 'wss:' ? u.href : null;
  } catch (_) { return null; }
}

function q2m(w, x, y, z, o) {
  const n = 1 / (Math.hypot(w, x, y, z) || 1); w *= n; x *= n; y *= n; z *= n;
  o[0] = 1 - 2 * (y * y + z * z); o[1] = 2 * (x * y - w * z); o[2] = 2 * (x * z + w * y);
  o[3] = 2 * (x * y + w * z); o[4] = 1 - 2 * (x * x + z * z); o[5] = 2 * (y * z - w * x);
  o[6] = 2 * (x * z - w * y); o[7] = 2 * (y * z + w * x); o[8] = 1 - 2 * (x * x + y * y);
}

/* ================================================================ replay clip */
async function loadReplay(P) {
  const meta = await getJSON(P.replayJson);
  const nb = 65, ff = HDR + nb * 7, fb = ff * 4;
  // the loop stops at "loop_end" (after the last event; the rest of the recording is the rig finishing up), so only
  // the frames up to it are fetched. A server that ignores Range sends the whole clip, which works the same.
  const lf = Number(meta.loop_end), lfps = Number(meta.fps) > 0 ? Number(meta.fps) : 20;
  const want = Number.isFinite(lf) && lf > 2 ? (Math.ceil(lf * lfps) + 3) * fb : 0;
  const r = await fetch(P.replayBin, want ? {cache: 'no-cache', headers: {Range: `bytes=0-${want - 1}`}} : {cache: 'no-cache'});
  if (!r.ok) throw new Error(`${P.replayBin}: HTTP ${r.status}`);
  const buf = await r.arrayBuffer();
  const n = Math.floor(buf.byteLength / fb);
  if (n < 2) throw new Error('replay clip is empty');
  const all = new Float32Array(buf, 0, n * ff);
  if (all[0] !== MAGIC) throw new Error('replay clip: bad magic');
  const frames = [];
  for (let i = 0; i < n; i++) frames.push(all.subarray(i * ff, (i + 1) * ff));
  const fps = Number(meta.fps) > 0 ? Number(meta.fps) : 20;
  // frame times: the frames' own sim time when it runs forward through the clip (a click frame carries the exact
  // click instant, a little before its slot), else the slot times
  const times = new Float64Array(n);
  let mono = true;
  for (let i = 0; i < n; i++) { times[i] = frames[i][1]; if (!Number.isFinite(times[i]) || (i && times[i] < times[i - 1])) mono = false; }
  if (!mono || Math.abs(times[n - 1] - times[0] - (n - 1) / fps) > 0.05 * n / fps) for (let i = 0; i < n; i++) times[i] = i / fps;
  const t0 = times[0];
  for (let i = 0; i < n; i++) times[i] -= t0;
  const dur = times[n - 1] + 1 / fps;
  // never blend across a reset (the rig puts the rat back in its start pose between targets) or an episode change
  const brk = new Uint8Array(n);
  for (let i = 0; i + 1 < n; i++) if (frames[i][2] !== frames[i + 1][2]) brk[i] = 1;
  for (const r of Array.isArray(meta.resets) ? meta.resets : []) {
    if (!Number.isFinite(r)) continue;
    let i = 0;
    while (i + 1 < n && times[i + 1] <= r + 0.005) i++;
    if (i + 1 < n) brk[i] = 1;
  }
  const le = Number(meta.loop_end);
  const loopEnd = Number.isFinite(le) && le > 2 && le < dur ? le : dur;
  const timeline = (Array.isArray(meta.timeline) ? meta.timeline : [])
    .filter(e => e && Number.isFinite(e.t) && typeof e.text === 'string' && e.text)
    .map(e => ({t: e.t, kind: String(e.kind || ''), text: e.text, until: Number.isFinite(e.until) ? e.until : null,
      target: Number.isFinite(e.target) ? e.target : null}))
    .sort((a, b) => a.t - b.t);
  const targets = normTargets(meta, dur);
  // long waits between a click and the next lit target (the rig typing, the image picker): the rat stands still
  // there, so the loop fast-forwards through their middle, labelled on the HUD (the clock shows recorded time)
  const gaps = [];
  const clicks = targets.filter(t => t.hit !== null).map(t => t.hit).sort((a, b) => a - b);
  const lits = targets.filter(t => t.lit !== null).map(t => t.lit).sort((a, b) => a - b);
  for (const a of clicks) {
    const b = lits.find(x => x > a);
    if (b !== undefined && b - a > 4) gaps.push([a, b]);
  }
  return {meta, frames, fps, n, dur, times, brk, loopEnd, timeline, gaps, targets,
    brainOn: Number.isFinite(meta.brain_on_at) ? meta.brain_on_at : null};
}

/* the 11 targets, whatever field names the clip's json uses: each gets its label, its boxes in normalised page
   coordinates (cx, cy, half-w, half-h, as the frames carry them) and, when given in clip seconds, lit/hit times */
function normTargets(meta, dur) {
  const vp = Array.isArray(meta.viewport) && meta.viewport.length >= 2 ? meta.viewport : [PAGE_W, PAGE_H];
  const list = Array.isArray(meta.targets) ? meta.targets : [];
  const inClip = v => typeof v === 'number' && isFinite(v) && v >= -1 && v <= dur + 5 ? v : null;
  const first = (...vs) => { for (const v of vs) { const x = inClip(v); if (x !== null) return x; } return null; };
  return list.map((t, i) => {
    const norms = [];
    const addNorm = a => { if (Array.isArray(a) && a.length >= 4 && a.slice(0, 4).every(Number.isFinite)) norms.push(a.slice(0, 4)); };
    const addBox = b => {
      if (Array.isArray(b) && b.length >= 4 && b.slice(0, 4).every(Number.isFinite))
        norms.push([(b[0] + b[2] / 2) / vp[0], (b[1] + b[3] / 2) / vp[1], b[2] / 2 / vp[0], b[3] / 2 / vp[1]]);
    };
    addNorm(t.norm); addNorm(t.target);
    for (const l of Array.isArray(t.lights) ? t.lights : []) { addNorm(l && l.norm); addBox(l && l.box); }
    addBox(t.box); addBox(t.page_box); addBox(t.box_px);
    const hitObj = t.hit && typeof t.hit === 'object' ? t.hit : {};
    const litObj = t.lit && typeof t.lit === 'object' ? t.lit : {};
    return {
      n: Number.isFinite(t.n) ? t.n : i + 1,
      label: String(t.label || t.name || t.key || `target ${i + 1}`),
      norms,
      lit: first(t.lit_at, t.lit_t, t.t_lit, t.lit_s, t.lit_at_s, t.lit_time, typeof t.lit === 'number' ? t.lit : null, litObj.t, t.t0),
      hit: first(t.hit_at, t.hit_t, t.t_hit, t.hit_s, t.hit_at_s, t.hit_time, typeof t.hit === 'number' ? t.hit : null, hitObj.t, hitObj.clip_t, t.t1),
    };
  });
}

/* ================================================================ styles (scoped to .lr-*) */
function injectCss() {
  if (document.getElementById('lr-live-css')) return;
  const s = document.createElement('style');
  s.id = 'lr-live-css';
  s.textContent = `
.lr-root{overflow:hidden;background:#000;isolation:isolate}
.lr-canvas{position:absolute;inset:0;width:100%;height:100%;display:block;touch-action:pan-y;cursor:grab;outline:none;
  -webkit-tap-highlight-color:transparent}
.lr-canvas.drag{cursor:grabbing}
.lr-loading,.lr-fallback{position:absolute;inset:0;display:flex;flex-direction:column;gap:6px;align-items:center;
  justify-content:center;text-align:center;padding:24px;pointer-events:none;
  font:12px/1.6 "JetBrains Mono",ui-monospace,monospace;letter-spacing:.04em;color:#ae8ee4;
  background:radial-gradient(50% 45% at 50% 55%,rgba(75,4,196,.28),rgba(19,8,174,.1) 55%,transparent 80%)}
.lr-fallback b{color:#d2c0f0;font-weight:600;letter-spacing:.16em;text-transform:uppercase;font-size:11px}
.lr-fallback span{max-width:34em}
.lr-loading i{display:block;width:120px;height:2px;margin-top:8px;border-radius:2px;
  background:linear-gradient(90deg,#4B04C4,#D60C94 50%,#F5AC29);background-size:200% 100%;animation:lr-slide 1.2s linear infinite}
.lr-hud{position:absolute;inset:0;pointer-events:none;z-index:2;color:#f4f0fb;
  font-family:"JetBrains Mono",ui-monospace,monospace}
.lr-tl{position:absolute;left:16px;top:14px;right:16px;display:flex;flex-direction:column;align-items:flex-start;gap:7px}
.lr-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap;max-width:100%}
.lr-pill{display:inline-flex;align-items:center;gap:8px;font-size:11.5px;font-weight:700;line-height:1;letter-spacing:.2em;
  padding:6px 10px 6px 9px;border-radius:3px;border:1px solid rgba(245,172,41,.75);color:#FCF010;
  background:rgba(10,4,24,.74);-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);white-space:nowrap}
.lr-pill i{width:8px;height:8px;border-radius:50%;background:#F5AC29;box-shadow:0 0 8px rgba(245,172,41,.8)}
.lr-root.is-live .lr-pill{border-color:#ff3b3b;color:#fff;background:rgba(179,18,28,.88);box-shadow:0 0 18px -2px rgba(255,59,59,.6)}
.lr-root.is-live .lr-pill i{background:#fff;box-shadow:0 0 8px #fff;animation:lr-pulse 1.4s infinite}
.lr-root.is-test .lr-pill{border-color:#E86E3D;color:#fff;background:rgba(232,110,61,.82)}
.lr-root.is-test .lr-pill i{background:#fff;box-shadow:none}
.lr-label{font:600 14px/1.3 "Space Grotesk",system-ui,sans-serif;color:#f4f0fb;letter-spacing:.01em;
  text-shadow:0 1px 3px #000,0 0 12px rgba(0,0,0,.8);max-width:100%}
.lr-sub{font-size:11.5px;line-height:1.45;color:#d2c0f0;letter-spacing:.03em;text-shadow:0 1px 2px #000,0 0 8px #000;max-width:100%}
.lr-sub b{color:#FCF010;font-weight:600}
.lr-sub em{font-style:normal;color:#ae8ee4}
.lr-tgt{display:inline-block;font-size:12px;line-height:1.2;padding:5px 10px;border-radius:3px;box-sizing:border-box;
  border:1px solid rgba(136,8,181,.6);background:rgba(10,4,24,.72);-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);
  color:#d2c0f0;white-space:nowrap;max-width:100%;overflow:hidden;text-overflow:ellipsis;transition:border-color .3s,box-shadow .3s}
.lr-tgt b{color:#FCF010;font-weight:600}
.lr-tgt.lit{border-color:#F5AC29;box-shadow:0 0 14px -3px rgba(245,172,41,.7)}
.lr-tgt.hit{border-color:#FCF010}
.lr-tgt:empty{display:none}
.lr-cap{position:absolute;left:16px;right:16px;bottom:12px;font-size:10.5px;line-height:1.5;color:#ae8ee4;
  letter-spacing:.02em;text-shadow:0 1px 2px #000,0 0 8px #000}
.lr-cap .s{display:none}
.lr-hint{position:absolute;right:16px;top:17px;font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:#9c75df;
  text-shadow:0 1px 2px #000;transition:opacity .6s}
.lr-toast{position:absolute;right:16px;top:44px;font-size:11px;letter-spacing:.06em;color:#000;background:#FCF010;
  padding:5px 9px;border-radius:2px;opacity:0;transform:translateY(-4px);transition:opacity .4s,transform .4s}
.lr-toast.on{opacity:1;transform:none}
.lr-log{position:absolute;left:16px;right:16px;bottom:14px;display:flex;flex-direction:column;align-items:flex-start;gap:4px}
.lr-hud.full .lr-log{bottom:36px}
.lr-log:empty{display:none}
.lr-log p{margin:0;display:flex;gap:9px;align-items:baseline;max-width:min(100%,620px);font-size:12px;line-height:1.4;
  padding:5px 10px 5px 9px;border-radius:3px;border-left:2px solid #D60C94;background:rgba(10,4,24,.72);
  -webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);color:#f4f0fb}
.lr-log p b{flex:none;font-size:10px;font-weight:700;letter-spacing:.16em;color:#F5AC29}
.lr-log p.old{font-size:11px;padding-top:3px;padding-bottom:3px;border-left-color:#8808B5;background:rgba(10,4,24,.8);
  color:rgba(244,240,251,.62)}
.lr-log p.old b{color:rgba(245,172,41,.62)}
.lr-log p.gold{border-left-color:#FCF010;box-shadow:0 0 16px -4px rgba(245,172,41,.6)}
.lr-log p.gold span{color:#FCF010}
.lr-log p.now{animation:lr-in .45s ease-out}
@keyframes lr-in{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
@container lrhud (max-width:560px){
  .lr-cap .l{display:none} .lr-cap .s{display:inline} .lr-hint{display:none} .lr-label{font-size:13px}
  .lr-sub{font-size:11px}
  .lr-log p.old{display:none} .lr-log p{font-size:11px} .lr-log{left:12px;right:12px;bottom:10px}
}
.lr-hud{container:lrhud/inline-size}
.lr-hud.compact .lr-tl{left:26px;top:24px;right:26px;gap:6px}
.lr-hud.compact .lr-hint{right:28px;top:29px}
.lr-hud.compact .lr-toast{right:26px;top:52px}
.lr-hud.compact .lr-log{left:26px;right:26px;bottom:var(--lr-log-b,24px)}
@container lrhud (max-width:420px){
  .lr-hud.compact .lr-tl{left:20px;top:18px;right:20px}
  .lr-hud.compact .lr-log{left:20px;right:20px;bottom:var(--lr-log-b,18px)}
  .lr-long{display:none}
  .lr-log p{font-size:10.5px;gap:7px;padding:4px 8px}
  .lr-log p span{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;line-clamp:2;overflow:hidden}
  .lr-tgt{font-size:11px;line-height:1.35;padding:4px 8px;white-space:normal}   /* wrap, never clip, on phones */
  .lr-sub{font-size:10.5px}
}
@keyframes lr-pulse{0%,100%{opacity:1}50%{opacity:.3}}
@keyframes lr-slide{from{background-position:0 0}to{background-position:-200% 0}}
@media (prefers-reduced-motion:reduce){.lr-root *{animation:none!important}}
`;
  document.head.appendChild(s);
}

/* ================================================================ shaders */
const FS_VERT = 'varying vec2 vUv; void main(){ vUv = uv; gl_Position = vec4(position.xy, 0.0, 1.0); }';

const PREFILTER_FRAG = `
  uniform sampler2D tSrc; uniform vec2 uTexel; uniform float uThr; uniform float uKnee; varying vec2 vUv;
  void main(){
    vec3 c = texture2D(tSrc, vUv + uTexel * vec2(-1.0, -1.0)).rgb + texture2D(tSrc, vUv + uTexel * vec2(1.0, -1.0)).rgb
           + texture2D(tSrc, vUv + uTexel * vec2(-1.0, 1.0)).rgb + texture2D(tSrc, vUv + uTexel * vec2(1.0, 1.0)).rgb;
    c = min(c * 0.25, vec3(24.0));
    float br = max(c.r, max(c.g, c.b));
    float rq = clamp(br - uThr + uKnee, 0.0, 2.0 * uKnee);
    rq = rq * rq / (4.0 * uKnee + 1e-4);
    c *= max(rq, br - uThr) / max(br, 1e-4);
    gl_FragColor = vec4(c, 1.0);
  }`;
const DOWN_FRAG = `
  uniform sampler2D tSrc; uniform vec2 uTexel; varying vec2 vUv;
  void main(){
    vec2 h = uTexel;
    vec3 s = texture2D(tSrc, vUv).rgb * 4.0;
    s += texture2D(tSrc, vUv - h).rgb + texture2D(tSrc, vUv + h).rgb;
    s += texture2D(tSrc, vUv + vec2(h.x, -h.y)).rgb + texture2D(tSrc, vUv - vec2(h.x, -h.y)).rgb;
    gl_FragColor = vec4(s / 8.0, 1.0);
  }`;
const UP_FRAG = `
  uniform sampler2D tSrc; uniform vec2 uTexel; uniform float uW; varying vec2 vUv;
  void main(){
    vec2 h = uTexel;
    vec3 s = texture2D(tSrc, vUv + vec2(-h.x * 2.0, 0.0)).rgb;
    s += texture2D(tSrc, vUv + vec2(-h.x, h.y)).rgb * 2.0;
    s += texture2D(tSrc, vUv + vec2(0.0, h.y * 2.0)).rgb;
    s += texture2D(tSrc, vUv + vec2(h.x, h.y)).rgb * 2.0;
    s += texture2D(tSrc, vUv + vec2(h.x * 2.0, 0.0)).rgb;
    s += texture2D(tSrc, vUv + vec2(h.x, -h.y)).rgb * 2.0;
    s += texture2D(tSrc, vUv + vec2(0.0, -h.y * 2.0)).rgb;
    s += texture2D(tSrc, vUv + vec2(-h.x, -h.y)).rgb * 2.0;
    gl_FragColor = vec4(s / 12.0 * uW, 1.0);
  }`;
const COMPOSITE_FRAG = `
  uniform sampler2D tScene; uniform sampler2D tBloom; uniform float uBloom; uniform float uVig; uniform float uFade;
  uniform float uSeed; uniform vec2 uRes; uniform float uCy; varying vec2 vUv;
  float h12(vec2 p){ return fract(sin(dot(p, vec2(12.9898, 78.233)) + uSeed) * 43758.5453); }
  void main(){
    vec3 c = texture2D(tScene, vUv).rgb + texture2D(tBloom, vUv).rgb * uBloom;
    vec2 d = (vUv - vec2(0.5, uCy)) * vec2(uRes.x / uRes.y, 1.0);
    float v = smoothstep(1.15, 0.28, length(d) / max(1.0, uRes.x / uRes.y) * 1.25);
    c *= mix(1.0 - uVig, 1.0, v) * uFade;
    gl_FragColor = vec4(c, 1.0);
    #include <tonemapping_fragment>
    #include <colorspace_fragment>
    gl_FragColor.rgb += (h12(gl_FragCoord.xy) - 0.5) / 255.0;
  }`;

const NEBULA_VERT = 'varying vec3 vDir; void main(){ vDir = position; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }';
const NEBULA_FRAG = `
  uniform float uTime; uniform vec3 cBlue; uniform vec3 cPurple; uniform vec3 cViolet; uniform vec3 cMag;
  uniform vec3 cCoral; uniform vec3 cGold; varying vec3 vDir;
  float hash13(vec3 p){ p = fract(p * 0.1031); p += dot(p, p.zyx + 31.32); return fract((p.x + p.y) * p.z); }
  float vnoise(vec3 p){
    vec3 i = floor(p), f = fract(p); f = f * f * (3.0 - 2.0 * f);
    return mix(mix(mix(hash13(i), hash13(i + vec3(1,0,0)), f.x), mix(hash13(i + vec3(0,1,0)), hash13(i + vec3(1,1,0)), f.x), f.y),
               mix(mix(hash13(i + vec3(0,0,1)), hash13(i + vec3(1,0,1)), f.x), mix(hash13(i + vec3(0,1,1)), hash13(i + vec3(1,1,1)), f.x), f.y), f.z);
  }
  float fbm(vec3 p){ float a = 0.5, s = 0.0; for (int i = 0; i < 5; i++){ s += a * vnoise(p); p = p * 2.03 + vec3(1.7, 9.2, 3.1); a *= 0.5; } return s; }
  void main(){
    vec3 d = normalize(vDir);
    float t = uTime * 0.004;
    float n1 = fbm(d * 1.7 + vec3(0.0, t, -t));
    float n2 = fbm(d * 3.6 + vec3(4.2, -t * 1.3, 1.1 + t));
    float n3 = fbm(d * 7.5 + vec3(-2.0, 3.0, t * 2.0));
    vec3 col = cBlue * 0.05 * smoothstep(0.30, 0.75, n1);
    col += cPurple * 0.10 * smoothstep(0.42, 0.80, n1);
    col += cViolet * 0.14 * smoothstep(0.45, 0.85, n2) * smoothstep(0.35, 0.65, n1);
    float m = smoothstep(0.52, 0.92, n2 * (0.55 + 0.6 * n1) + 0.18 * n3);
    col += cMag * 0.12 * m * m;
    col += cCoral * 0.035 * pow(smoothstep(0.62, 0.95, n3 * n2 * 1.5), 2.0);
    col += cGold * 0.045 * pow(smoothstep(0.70, 1.0, n2 * 0.6 + n3 * 0.5), 3.0);
    col *= 0.35 + 0.65 * smoothstep(-0.55, 0.25, d.z);            // darker below the horizon
    vec3 sp = d * 150.0; vec3 cell = floor(sp); float hs = hash13(cell);
    if (hs > 0.972){
      vec3 f = fract(sp) - 0.5; float s = exp(-dot(f, f) * 42.0);
      float tw = 0.65 + 0.35 * sin(uTime * (1.0 + 3.0 * hash13(cell + 7.1)) + hs * 40.0);
      col += mix(vec3(0.85, 0.78, 1.0), vec3(1.0, 0.85, 0.5), hash13(cell + 3.3)) * s * tw * (hs - 0.972) * 26.0;
    }
    gl_FragColor = vec4(col, 1.0);
  }`;

const FLOOR_VERT = 'varying vec2 vXY; void main(){ vec4 w = modelMatrix * vec4(position, 1.0); vXY = w.xy; gl_Position = projectionMatrix * viewMatrix * w; }';
const FLOOR_FRAG = `
  uniform vec2 uC; uniform float uR; uniform vec3 cViolet; uniform vec3 cMag; uniform vec3 cPurple; uniform vec3 cGold;
  varying vec2 vXY;
  float grid(vec2 p, float s){ vec2 q = p / s; vec2 g = abs(fract(q - 0.5) - 0.5) / max(fwidth(q), vec2(1e-5)); return 1.0 - min(min(g.x, g.y), 1.0); }
  void main(){
    vec2 p = vXY - uC; float r = length(p);
    float fade = 1.0 - smoothstep(uR * 0.18, uR, r);
    vec3 col = vec3(0.004, 0.002, 0.009);
    col += cPurple * 0.16 * grid(vXY, 0.05) * fade;
    col += cViolet * 0.34 * grid(vXY, 0.25) * fade;
    col += cMag * 0.055 * exp(-r * r / 0.07) + cPurple * 0.05 * exp(-r * r / 0.4);
    col += cGold * 0.012 * exp(-dot(p - vec2(0.2, 0.0), p - vec2(0.2, 0.0)) / 0.02);
    gl_FragColor = vec4(col, clamp(fade * 1.15, 0.0, 1.0));
  }`;

const SCREEN_FRAG = `
  uniform float uTime; uniform vec2 uCur; uniform vec2 uTrail[12]; uniform vec4 uTgt; uniform float uTgtA;
  uniform vec4 uClick; uniform float uMode; uniform float uLever; uniform float uThr; uniform float uPress;
  uniform float uOn; uniform sampler2D uText;
  uniform vec3 cGold; uniform vec3 cYellow; uniform vec3 cMag; uniform vec3 cViolet; uniform vec3 cPurple; uniform vec3 cBlue;
  varying vec2 vUv;
  float sdBox(vec2 p, vec2 b){ vec2 d = abs(p) - b; return length(max(d, 0.0)) + min(max(d.x, d.y), 0.0); }
  void main(){
    vec2 pg = vec2(vUv.x, 1.0 - vUv.y);
    vec2 S = vec2(1280.0, 900.0);
    vec2 px = pg * S;
    vec3 col = mix(cBlue * 0.05, cPurple * 0.05, vUv.y) + cViolet * 0.012;
    vec2 gq = px / 64.0; vec2 gd = min(fract(gq), 1.0 - fract(gq)) * 64.0;
    col += cViolet * 0.12 * (1.0 - smoothstep(0.0, 2.2, min(gd.x, gd.y)));
    col *= 0.94 + 0.06 * sin(px.y * 0.9);
    if (uMode < 0.5){
      if (uTgt.z > 0.0){
        float sd = sdBox((pg - uTgt.xy) * S, max(uTgt.zw * S, vec2(3.0)));
        float fill = 1.0 - smoothstep(-1.5, 1.5, sd);
        float edge = 1.0 - smoothstep(1.5, 5.0, abs(sd));
        float glow = exp(-max(sd, 0.0) / 26.0) * (1.0 - fill);
        float pulse = 0.78 + 0.22 * sin(uTime * 6.0);
        col += (cGold * (0.55 * fill + 0.7 * glow) + cYellow * 2.6 * edge) * pulse * uTgtA;
      }
      for (int i = 0; i < 12; i++){
        vec2 tp = uTrail[i];
        if (tp.x < 0.0) continue;
        float k = 1.0 - float(i) / 12.0;
        float d = length((pg - tp) * S);
        col += cMag * 0.55 * k * exp(-d * d / (2.0 * (4.0 + 7.0 * k) * (4.0 + 7.0 * k)));
      }
      if (uCur.x >= 0.0){
        float d = length((pg - uCur) * S);
        col += vec3(1.0, 0.86, 0.96) * 3.2 * (1.0 - smoothstep(6.0, 9.5, d));
        col += cMag * (1.8 * exp(-d / 14.0) + 0.5 * exp(-d / 60.0));
        vec2 a = abs((pg - uCur) * S);
        float crs = (1.0 - smoothstep(0.8, 2.2, min(a.x, a.y))) * step(16.0, max(a.x, a.y)) * (1.0 - smoothstep(24.0, 34.0, max(a.x, a.y)));
        col += cMag * 1.6 * crs;
      }
      if (uClick.z >= 0.0 && uClick.z < 0.9){
        float r = 12.0 + uClick.z * 130.0;
        float d = abs(length((pg - uClick.xy) * S) - r);
        vec3 rc = mix(cMag, cYellow, uClick.w);
        col += rc * 3.2 * (1.0 - uClick.z / 0.9) * (1.0 - smoothstep(0.0, 5.0, d));
        col += rc * 0.09 * uClick.w * exp(-uClick.z * 8.0);
      }
    } else {
      float x0 = 0.10, x1 = 0.90, yc = 0.60, hh = 0.045;
      vec2 c = pg;
      if (abs(c.y - yc) < hh && c.x > x0 && c.x < x1){
        float f = (c.x - x0) / (x1 - x0);
        float on = step(f, uLever);
        col = mix(cPurple * 0.22, mix(cMag, cGold, f) * (1.2 + 1.2 * step(uThr, uLever)), on);
      }
      float fr = abs(abs(c.y - yc) - hh) * S.y;
      col += cViolet * 0.8 * (1.0 - smoothstep(0.5, 2.0, fr)) * step(x0, c.x) * step(c.x, x1);
      float thx = x0 + (x1 - x0) * uThr;
      col += cYellow * 2.6 * (1.0 - smoothstep(1.0, 3.0, abs(c.x - thx) * S.x)) * step(abs(c.y - yc), hh * 1.9);
      col += cGold * 0.12 * exp(-uPress * 6.0);
    }
    vec4 tx = texture2D(uText, vUv);
    col = mix(col, tx.rgb * 1.5, tx.a);
    vec2 e = min(vUv, 1.0 - vUv);
    col *= 0.5 + 0.5 * smoothstep(0.0, 0.035, min(e.x, e.y * 1.42));
    gl_FragColor = vec4(col * uOn, 1.0);
  }`;
const SCREEN_VERT = 'varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }';

const DUST_VERT = `
  uniform float uTime; uniform float uPx; attribute float seed; varying float vA;
  void main(){
    vec3 p = position;
    p.x += 0.012 * sin(uTime * 0.13 + seed * 6.2831);
    p.y += 0.012 * cos(uTime * 0.11 + seed * 12.566);
    p.z += 0.010 * sin(uTime * 0.09 + seed * 3.1);
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    gl_Position = projectionMatrix * mv;
    gl_PointSize = uPx * (0.6 + seed) / max(0.05, -mv.z);
    vA = (0.25 + 0.75 * fract(seed * 7.13)) * (0.5 + 0.5 * sin(uTime * 0.7 + seed * 40.0));
  }`;
const DUST_FRAG = `
  uniform vec3 cA; uniform vec3 cB; varying float vA;
  void main(){ vec2 q = gl_PointCoord - 0.5; float d = dot(q, q); if (d > 0.25) discard;
    gl_FragColor = vec4(mix(cA, cB, vA) * exp(-d * 22.0) * vA * 0.35, 1.0); }`;

const FUR_GLSL = `
  float fhash13(vec3 p){ p = fract(p * 0.1031); p += dot(p, p.zyx + 31.32); return fract((p.x + p.y) * p.z); }
  vec3 fhash33(vec3 p){ p = fract(p * vec3(0.1031, 0.1030, 0.0973)); p += dot(p, p.yxz + 33.33);
    return fract((p.xxy + p.yxx) * p.zyx); }
  float strandCell(vec3 q, float lay){
    vec3 c = floor(q); vec3 f = fract(q) - 0.5;
    vec3 j = (fhash33(c) - 0.5) * 0.42;
    float len = 0.42 + 0.58 * fhash13(c + 11.7);
    float rad = 0.56 * (1.0 - lay / len);
    float d = length(f - j);
    float aa = max(fwidth(d), 1e-4) * 0.8;
    return (lay > len) ? 0.0 : 1.0 - smoothstep(rad - aa, rad + aa, d);
  }
  float furCover(vec3 rest, float lay){
    vec3 q = rest * uDen;
    return max(strandCell(q, lay), strandCell(q + vec3(0.5, 0.37, 0.61), lay));
  }`;

/* ================================================================ mount */
async function mount(el, opts, st) {
  injectCss();
  if (getComputedStyle(el).position === 'static') el.style.position = 'relative';
  el.classList.add('lr-root');
  st.cleanup.push(() => el.classList.remove('lr-root', 'is-live', 'is-replay'));
  // loader: false when the host page shows its own loading message (the site's placeholder), so only one shows
  const loadEl = opts.loader === false ? null : document.createElement('div');
  if (loadEl) {
    loadEl.className = 'lr-loading';
    loadEl.innerHTML = '<span>preparing the operant chamber…</span><i></i>';
    el.appendChild(loadEl);
    st.cleanup.push(() => loadEl.remove());
  }

  const probe = document.createElement('canvas');
  const pgl = probe.getContext('webgl2');
  if (!pgl) throw new Error('WebGL 2 is not available');
  const lose = pgl.getExtension('WEBGL_lose_context'); if (lose) lose.loseContext();

  const P = paths(opts);
  let replay = null, replayLoading = true;      // loading: the recorded clip is still downloading (not "standby")
  const replayP = loadReplay(P).then(r => { replay = r; return r; })
    .catch(e => { console.warn('labrat live: replay clip unavailable -', e && e.message ? e.message : e); return null; })
    .finally(() => { replayLoading = false; });
  const [THREE, R] = await Promise.all([loadThree(), getJSON(P.rat)]);
  if (st.disposed) return null;
  if (!R || R.format !== 'ratbrain-rat-1') throw new Error('rat.json: not a ratbrain-rat-1 asset');
  const CH = Object.assign({WALL_X: 0.175, LEVER_Z: 0.032, LEVER_LEN: 0.045, SIDE_Y: 0.2, BACK_X: -0.36, WALL_H: 0.12,
    PADDLE_HALF: [0.0225, 0.022, 0.003], WALL_T: 0.01, ROD_PITCH: 0.008, ROD_R: 0.0022, PRESS_ANGLE_DEG: 11.459}, R.chamber || {});
  const NB = R.n_bones, FF = HDR + NB * 7, FBYTES = FF * 4;
  const col = hex => new THREE.Color(hex);

  /* ---------------------------------------------------------------- quality tier */
  const mq = q => { try { return window.matchMedia(q).matches; } catch (_) { return false; } };
  const forced = opts.quality !== undefined && opts.quality !== null && Number.isFinite(+opts.quality);
  let tier = forced ? clamp(Math.round(+opts.quality), 0, 2)
    : (mq('(pointer: coarse)') || Math.min(window.screen.width || 1e4, window.screen.height || 1e4) < 700 ? 1 : 2);
  const reduceMotion = mq('(prefers-reduced-motion: reduce)');

  /* ---------------------------------------------------------------- renderer + post targets */
  const canvas = document.createElement('canvas');
  canvas.className = 'lr-canvas';
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', 'Real-time 3D view of the virtual rat (DeepMind’s rodent model in MuJoCo) in its ' +
    'operant chamber. Shows the live training run when one is streaming, otherwise a labelled replay of a recorded ' +
    'launch session.');
  el.insertBefore(canvas, el.firstChild);
  const renderer = new THREE.WebGLRenderer({canvas, antialias: false, alpha: false, depth: false, stencil: false,
    powerPreference: 'high-performance'});
  st.cleanup.push(() => { renderer.dispose(); canvas.remove(); });
  renderer.autoClear = false;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.AgXToneMapping !== undefined ? THREE.AgXToneMapping : THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.08;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.setClearColor(0x000000, 1);

  const sceneRT = new THREE.WebGLRenderTarget(4, 4, {type: THREE.HalfFloatType, samples: 4, depthBuffer: true,
    stencilBuffer: false});
  const NLEV = 5;
  const bloomRT = Array.from({length: NLEV}, () => new THREE.WebGLRenderTarget(4, 4, {type: THREE.HalfFloatType,
    depthBuffer: false, stencilBuffer: false}));
  st.cleanup.push(() => { sceneRT.dispose(); for (const r of bloomRT) r.dispose(); });
  const fsScene = new THREE.Scene(), fsCam = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
  const fsQuad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2));
  fsQuad.frustumCulled = false; fsScene.add(fsQuad);
  const fsMat = (frag, uniforms, extra = {}) => new THREE.ShaderMaterial(Object.assign({uniforms, vertexShader: FS_VERT,
    fragmentShader: frag, depthTest: false, depthWrite: false}, extra));
  const mPre = fsMat(PREFILTER_FRAG, {tSrc: {value: null}, uTexel: {value: new THREE.Vector2()}, uThr: {value: 1.1},
    uKnee: {value: 0.55}}, {toneMapped: false});
  const mDown = fsMat(DOWN_FRAG, {tSrc: {value: null}, uTexel: {value: new THREE.Vector2()}}, {toneMapped: false});
  const mUp = fsMat(UP_FRAG, {tSrc: {value: null}, uTexel: {value: new THREE.Vector2()}, uW: {value: 1}},
    {toneMapped: false, transparent: true, blending: THREE.AdditiveBlending});
  const mComp = fsMat(COMPOSITE_FRAG, {tScene: {value: null}, tBloom: {value: null}, uBloom: {value: 0.55},
    uVig: {value: 0.55}, uFade: {value: 0}, uSeed: {value: 0}, uRes: {value: new THREE.Vector2(1, 1)}, uCy: {value: 0.5}});
  st.cleanup.push(() => { for (const m of [mPre, mDown, mUp, mComp]) m.dispose(); fsQuad.geometry.dispose(); });

  /* ---------------------------------------------------------------- scene */
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(30, 1.6, 0.01, 30);
  camera.up.set(0, 0, 1);
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = (() => {             // reflections: a dark studio with warm softboxes and palette strips
    const s = new THREE.Scene();
    const add = (geo, mat, f) => { const m = new THREE.Mesh(geo, mat); if (f) f(m); s.add(m); };
    add(new THREE.BoxGeometry(12, 12, 12), new THREE.MeshBasicMaterial({color: 0x07050c, side: THREE.BackSide}));
    const panel = (w, h, p, rgb, k) => add(new THREE.PlaneGeometry(w, h),
      new THREE.MeshBasicMaterial({color: new THREE.Color(rgb).multiplyScalar(k), side: THREE.DoubleSide}),
      m => { m.position.set(...p); m.up.set(0, 0, 1); m.lookAt(0, 0, 0); });
    panel(5, 3, [0.3, 0, 5.8], 0xfff1e0, 3.0);
    panel(1.0, 5, [-5.8, 1.5, 1.2], PAL.magenta, 2.6);
    panel(0.8, 4, [1.5, 5.8, 1.0], PAL.gold, 2.2);
    panel(3.5, 2, [2.5, -5.8, 1.6], 0xffe4c8, 1.2);
    panel(2, 1.2, [-2.0, -5.8, 0.4], PAL.violet, 2.0);
    const t = pmrem.fromScene(s, 0.03).texture;
    s.traverse(o => { if (o.geometry) o.geometry.dispose(); if (o.material) o.material.dispose(); });
    return t;
  })();
  pmrem.dispose();
  st.cleanup.push(() => scene.environment && scene.environment.dispose());

  // nebula backdrop + star speckle (a big sphere drawn first, behind everything)
  const nebU = {uTime: {value: 0}, cBlue: {value: col(PAL.blue)}, cPurple: {value: col(PAL.purple)},
    cViolet: {value: col(PAL.violet)}, cMag: {value: col(PAL.magenta)}, cCoral: {value: col(PAL.coral)}, cGold: {value: col(PAL.gold)}};
  const nebula = new THREE.Mesh(new THREE.SphereGeometry(12, 48, 24), new THREE.ShaderMaterial({uniforms: nebU,
    vertexShader: NEBULA_VERT, fragmentShader: NEBULA_FRAG, side: THREE.BackSide, depthWrite: false, depthTest: false}));
  nebula.renderOrder = -20; nebula.frustumCulled = false;
  scene.add(nebula);

  /* ---------------------------------------------------------------- lights */
  const hemi = new THREE.HemisphereLight(0xa9a0c8, 0x0b0908, 0.32);
  scene.add(hemi);
  const house = new THREE.SpotLight(0xfff0dc, 1.7, 0, 0.72, 0.85, 2);
  house.position.set(0.03, 0.05, 0.62); house.target.position.set(0.0, 0.0, 0.0);
  house.castShadow = true; house.shadow.mapSize.set(TIERS[tier].shadow, TIERS[tier].shadow);
  house.shadow.camera.near = 0.25; house.shadow.camera.far = 0.9;
  house.shadow.bias = -0.00004; house.shadow.normalBias = 0.0006; house.shadow.radius = 3;
  scene.add(house, house.target);
  const mkDir = (c, k) => { const l = new THREE.DirectionalLight(c, k); scene.add(l, l.target); return l; };
  const key = mkDir(0xffe2c4, 1.35), fill = mkDir(0xcfd8ff, 0.3);
  const rimM = mkDir(PAL.magenta, 4.2), rimG = mkDir(PAL.gold, 2.2);
  const screenLight = new THREE.PointLight(0x8a5cff, 0.006, 0.6, 2);
  const pressLight = new THREE.PointLight(PAL.gold, 0, 0.3, 2);
  scene.add(screenLight, pressLight);

  /* ---------------------------------------------------------------- chamber */
  const disposables = [];
  const brushed = (() => {
    const c = document.createElement('canvas'); c.width = 16; c.height = 512;
    const g = c.getContext('2d');
    for (let y = 0; y < 512; y++) {
      const v = 168 + 16 * Math.sin(y * 0.37) * Math.sin(y * 0.071) + 16 * (Math.random() - 0.5);
      g.fillStyle = `rgb(${v | 0},${v | 0},${v | 0})`; g.fillRect(0, y, 16, 1);
    }
    const t = new THREE.CanvasTexture(c); t.wrapS = t.wrapT = THREE.RepeatWrapping; t.repeat.set(1, 3);
    disposables.push(t); return t;
  })();
  const hdr = (hex, k) => new THREE.Color(hex).multiplyScalar(k);
  const M = {
    panel: new THREE.MeshPhysicalMaterial({color: 0x241d31, metalness: 0.88, roughness: 0.34, roughnessMap: brushed, envMapIntensity: 0.9}),
    dark: new THREE.MeshStandardMaterial({color: 0x120e19, metalness: 0.85, roughness: 0.42, envMapIntensity: 0.7}),
    rod: new THREE.MeshStandardMaterial({color: 0x6c6e78, metalness: 1, roughness: 0.4, envMapIntensity: 0.35}),
    tray: new THREE.MeshStandardMaterial({color: 0x050308, metalness: 0.3, roughness: 0.55}),
    hole: new THREE.MeshBasicMaterial({color: 0x010101}),
    acrylic: new THREE.MeshPhysicalMaterial({color: 0x000000, metalness: 0, roughness: 0.05, transparent: true, opacity: 1,
      blending: THREE.AdditiveBlending, envMapIntensity: 0.55, depthWrite: false, side: THREE.DoubleSide}),
    lever: new THREE.MeshPhysicalMaterial({color: 0xe3e5ea, metalness: 1, roughness: 0.14, clearcoat: 0.4, envMapIntensity: 1.1}),
    edge: new THREE.MeshBasicMaterial({vertexColors: true, toneMapped: false}),
    gold: new THREE.MeshBasicMaterial({color: hdr(PAL.gold, 2.6), toneMapped: false}),
    lamp: new THREE.MeshBasicMaterial({color: hdr(0xfff0dc, 3.0), toneMapped: false}),
    plinth: new THREE.MeshPhysicalMaterial({color: 0x0d0a14, metalness: 0.6, roughness: 0.3, clearcoat: 0.6, envMapIntensity: 0.8}),
  };
  // the clear walls show only the studio's reflections: no sharp highlights from the lights (on a glossy sheet they
  // read as glowing dots floating in the nebula)
  M.acrylic.onBeforeCompile = sh => {
    sh.fragmentShader = sh.fragmentShader.replace('#include <lights_fragment_end>',
      '#include <lights_fragment_end>\nreflectedLight.directSpecular = vec3(0.0); reflectedLight.directDiffuse = vec3(0.0);');
  };
  M.acrylic.customProgramCacheKey = () => 'labrat-live-acrylic-v1';
  for (const m of Object.values(M)) disposables.push(m);
  st.cleanup.push(() => { for (const d of disposables) d.dispose(); });

  const WX = CH.WALL_X, BX = CH.BACK_X, SY = CH.SIDE_Y, H2 = 2 * CH.WALL_H, LZ = CH.LEVER_Z, LL = CH.LEVER_LEN;
  const T = CH.WALL_T || 0.01, LEN = WX - BX, CX = (WX + BX) / 2;
  const box = (sx, sy, sz, m, x, y, z, shadow = true) => {
    const o = new THREE.Mesh(new THREE.BoxGeometry(sx, sy, sz), m);
    o.position.set(x, y, z); o.castShadow = shadow; o.receiveShadow = shadow; scene.add(o); return o;
  };
  // a glowing bar with a colour ramp along its length (for the chamber's edges and the plinth trim)
  const ramp = [PAL.purple, PAL.violet, PAL.magenta, PAL.coral, PAL.orange, PAL.gold].map(h => new THREE.Color(h));
  const rampAt = (t, k) => {
    t = clamp(t, 0, 1) * (ramp.length - 1); const i = Math.min(ramp.length - 2, Math.floor(t));
    return ramp[i].clone().lerp(ramp[i + 1], t - i).multiplyScalar(k);
  };
  const glowBar = (a, b, th, t0, t1, k, mat = M.edge) => {
    const d = new THREE.Vector3().subVectors(b, a), L = d.length();
    const g = new THREE.BoxGeometry(th, th, L, 1, 1, 8);
    const cols = [], p = g.attributes.position;
    for (let i = 0; i < p.count; i++) { const c = rampAt(t0 + (t1 - t0) * (p.getZ(i) / L + 0.5), k); cols.push(c.r, c.g, c.b); }
    g.setAttribute('color', new THREE.Float32BufferAttribute(cols, 3));
    const m = new THREE.Mesh(g, mat);
    m.position.copy(a).addScaledVector(d, 0.5);
    m.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), d.clone().normalize());
    scene.add(m); return m;
  };
  // plinth the chamber stands on, with a ramp trim along its top edge
  const PZ0 = -0.018, PH = 0.022, PLX = LEN + 0.06, PLY = 2 * SY + 0.06;
  box(PLX, PLY, PH, M.plinth, CX, 0, PZ0 - PH / 2);
  {
    const z = PZ0 + 0.0008, x0 = CX - PLX / 2, x1 = CX + PLX / 2, y0 = -PLY / 2, y1 = PLY / 2, th = 0.0022;
    const V = (x, y) => new THREE.Vector3(x, y, z);
    glowBar(V(x0, y0), V(x1, y0), th, 0.0, 0.6, 1.3);
    glowBar(V(x1, y0), V(x1, y1), th, 0.6, 1.0, 1.3);
    glowBar(V(x1, y1), V(x0, y1), th, 0.6, 0.0, 1.3);
    glowBar(V(x0, y1), V(x0, y0), th, 0.0, 0.0, 1.3);
  }
  // grid floor: steel rods (along y) at ROD_PITCH over a dark tray; rod tops at z = 0 (the physics floor)
  const rr = CH.ROD_R || 0.0022, pitch = CH.ROD_PITCH || 0.008;
  box(LEN, 2 * SY, 0.01, M.tray, CX, 0, -0.013);
  const nRods = Math.floor((LEN - 0.004) / pitch) + 1;
  const rods = new THREE.InstancedMesh(new THREE.CylinderGeometry(rr, rr, 2 * SY, 14, 1), M.rod, nRods);
  const m4 = new THREE.Matrix4();
  for (let i = 0; i < nRods; i++) { m4.makeTranslation(BX + 0.004 + i * pitch, 0, -rr); rods.setMatrixAt(i, m4); }
  rods.castShadow = true; rods.receiveShadow = true; scene.add(rods);
  for (const s of [-1, 1]) box(LEN, 0.006, 0.008, M.dark, CX, s * (SY - 0.003), -0.006);
  // front panel (graphite, brushed) with the lever housing, food magazine and house-light fixture
  box(T, 2 * SY + 2 * T, H2, M.panel, WX + T / 2, 0, H2 / 2);
  box(0.008, 0.06, 0.018, M.dark, WX - 0.003, 0, LZ);
  box(0.0015, 0.05, 0.008, M.hole, WX - 0.0072, 0, LZ, false);
  box(0.006, 0.05, 0.045, M.dark, WX - 0.002, 0.13, 0.035);
  box(0.002, 0.036, 0.03, M.hole, WX - 0.0052, 0.13, 0.03, false);
  box(0.012, 0.05, 0.008, M.dark, WX - 0.005, -0.15, H2 - 0.03);
  box(0.002, 0.038, 0.003, M.lamp, WX - 0.011, -0.15, H2 - 0.0335, false);
  const lever = new THREE.Group();
  lever.position.set(WX, 0, LZ);
  const PH3 = CH.PADDLE_HALF || [0.0225, 0.022, 0.003];
  const paddle = new THREE.Mesh(new THREE.BoxGeometry(LL, 2 * PH3[1], 2 * PH3[2]), M.lever);
  paddle.position.set(-LL / 2, 0, 0); paddle.castShadow = true; paddle.receiveShadow = true;
  lever.add(paddle); scene.add(lever);
  // the lever is the click, so it must read at a glance: a thin gold rim on the paddle's top edges and a faint warm
  // tint (the polished steel alone mirrors the dark studio and vanishes against the housing)
  M.lever.emissive = new THREE.Color(PAL.gold); M.lever.emissiveIntensity = 0.07;
  {
    const rimMat = new THREE.MeshBasicMaterial({color: hdr(PAL.gold, 1.15), toneMapped: false});
    disposables.push(rimMat);
    const rt = 0.0011, zt = PH3[2] + rt / 2;
    const bar = (sx, sy, x, y) => {
      const g = new THREE.BoxGeometry(sx, sy, rt); disposables.push(g);
      const m = new THREE.Mesh(g, rimMat); m.position.set(x, y, zt); lever.add(m);
    };
    bar(LL, rt, -LL / 2, PH3[1] - rt / 2); bar(LL, rt, -LL / 2, -PH3[1] + rt / 2); bar(rt, 2 * PH3[1], -LL + rt / 2, 0);
  }
  const posts = [];
  // clear walls on the other three sides; dark rails on top; glowing ramp edges; gold corner brackets
  // (single sheets, not boxes: one reflecting surface each, so nothing is mirrored twice)
  for (const s of [-1, 1]) {
    const w = new THREE.Mesh(new THREE.PlaneGeometry(LEN, H2), M.acrylic);
    w.rotation.x = Math.PI / 2;
    w.position.set(CX, s * (SY + T / 2), H2 / 2); w.renderOrder = 5; scene.add(w);
    box(LEN + 0.004, T + 0.004, 0.005, M.dark, CX, s * (SY + T / 2), H2 + 0.0025);
  }
  {
    const w = new THREE.Mesh(new THREE.PlaneGeometry(2 * SY, H2), M.acrylic);
    w.rotation.set(Math.PI / 2, Math.PI / 2, 0);
    w.position.set(BX - T / 2, 0, H2 / 2); w.renderOrder = 5; scene.add(w);
    box(T + 0.004, 2 * SY + 2 * T, 0.005, M.dark, BX - T / 2, 0, H2 + 0.0025);
  }
  {
    const th = 0.0016, yo = SY + T + 0.0005, xb = BX - T - 0.0005, xf = WX, zt = H2 + 0.0055;
    const V = (x, y, z) => new THREE.Vector3(x, y, z);
    // each vertical corner post (+ its gold bracket) has its own materials, so it can fade when the orbiting
    // camera passes right next to it (a cutaway: a post a few cm from the lens would slice through the view)
    const bl = 0.03, bt = 0.0026, zc = zt + 0.002;
    for (const [x, sx] of [[xb, 1], [xf, -1]]) for (const s of [-1, 1]) {
      const me = M.edge.clone(), mg = M.gold.clone();
      me.transparent = mg.transparent = true; me.depthWrite = mg.depthWrite = false;
      disposables.push(me, mg);
      glowBar(V(x, s * yo, 0), V(x, s * yo, zt), th, 0.0, 0.55, 1.5, me);   // vertical corner post
      box(bl, bt, bt, mg, x + sx * bl / 2, s * yo, zc, false);             // gold corner bracket (the site's panel corners)
      box(bt, bl, bt, mg, x, s * (yo - bl / 2), zc, false);
      box(bt, bt, bl, mg, x, s * yo, zc - bl / 2, false);
      posts.push({x, y: s * yo, mats: [me, mg]});
    }
    for (const s of [-1, 1]) glowBar(V(xb, s * yo, zt), V(xf, s * yo, zt), th, 0.55, 0.62, 1.2);    // top long edges
    glowBar(V(xb, -yo, zt), V(xb, yo, zt), th, 0.55, 0.55, 1.2);
    glowBar(V(xf, -yo, zt), V(xf, yo, zt), th, 0.55, 0.55, 1.2);
  }
  // the floor the whole rig stands on: a dark disc with a lab grid that fades into the nebula
  const floorMat = new THREE.ShaderMaterial({uniforms: {uC: {value: new THREE.Vector2(CX * 0.5, 0)}, uR: {value: 1.6},
    cViolet: {value: col(PAL.violet)}, cMag: {value: col(PAL.magenta)}, cPurple: {value: col(PAL.purple)}, cGold: {value: col(PAL.gold)}},
    vertexShader: FLOOR_VERT, fragmentShader: FLOOR_FRAG, transparent: true, depthWrite: true, extensions: {derivatives: true}});
  const floorMesh = new THREE.Mesh(new THREE.CircleGeometry(1.6, 96), floorMat);
  floorMesh.position.set(CX * 0.5, 0, PZ0 - PH); floorMesh.renderOrder = -10; scene.add(floorMesh);
  disposables.push(floorMat);

  /* ---------------------------------------------------------------- the stimulus screen (schematic) */
  const SW = 0.2, SHh = SW * PAGE_H / PAGE_W, SZ = 0.064 + SHh / 2 + 0.006;
  const scrText = document.createElement('canvas'); scrText.width = 1024; scrText.height = 720;
  const scrCtx = scrText.getContext('2d');
  const scrTex = new THREE.CanvasTexture(scrText);
  scrTex.colorSpace = THREE.SRGBColorSpace; scrTex.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy());
  disposables.push(scrTex);
  const trail = Array.from({length: 12}, () => new THREE.Vector2(-1, -1));
  const scrU = {uTime: {value: 0}, uCur: {value: new THREE.Vector2(-1, -1)}, uTrail: {value: trail},
    uTgt: {value: new THREE.Vector4(-1, -1, -1, -1)}, uTgtA: {value: 0}, uClick: {value: new THREE.Vector4(0, 0, -1, 0)},
    uMode: {value: 0}, uLever: {value: 0}, uThr: {value: (CH.PRESS_ANGLE_DEG || 11.459) / 30}, uPress: {value: 9},
    uOn: {value: 1}, uText: {value: scrTex},
    cGold: {value: col(PAL.gold)}, cYellow: {value: col(PAL.yellow)}, cMag: {value: col(PAL.magenta)},
    cViolet: {value: col(PAL.violet)}, cPurple: {value: col(PAL.purple)}, cBlue: {value: col(PAL.blue)}};
  const scrMat = new THREE.ShaderMaterial({uniforms: scrU, vertexShader: SCREEN_VERT, fragmentShader: SCREEN_FRAG});
  disposables.push(scrMat);
  const scrMesh = new THREE.Mesh(new THREE.PlaneGeometry(SW, SHh), scrMat);
  // plane basis: u -> world -y (the rat's right, it faces +x), v -> +z, normal -> -x (into the chamber)
  scrMesh.quaternion.setFromRotationMatrix(new THREE.Matrix4().makeBasis(new THREE.Vector3(0, -1, 0),
    new THREE.Vector3(0, 0, 1), new THREE.Vector3(-1, 0, 0)));
  scrMesh.position.set(WX - 0.0046, 0, SZ);
  scene.add(scrMesh);
  box(0.004, SW + 0.012, SHh + 0.012, M.dark, WX - 0.002, 0, SZ, false);
  {   // gold corner brackets around the screen
    const x = WX - 0.0046, hw = SW / 2 + 0.011, hh = SHh / 2 + 0.011, bl = 0.016, bt = 0.0016;
    for (const sy of [-1, 1]) for (const sz of [-1, 1]) {
      box(bt, bl, bt, M.gold, x, sy * (hw - bl / 2), SZ + sz * hh, false);
      box(bt, bt, bl, M.gold, x, sy * hw, SZ + sz * (hh - bl / 2), false);
    }
  }
  screenLight.position.set(WX - 0.06, 0, SZ);

  /* ---------------------------------------------------------------- press fx: a gold shockwave at the lever */
  const ringMat = new THREE.MeshBasicMaterial({color: hdr(PAL.gold, 2.2), transparent: true, opacity: 0,
    blending: THREE.AdditiveBlending, depthWrite: false, toneMapped: false, side: THREE.DoubleSide});
  const ring = new THREE.Mesh(new THREE.RingGeometry(0.9, 1.0, 64), ringMat);
  ring.position.set(WX - LL * 0.6, 0, 0.0015); ring.renderOrder = 6; ring.visible = false; scene.add(ring);
  disposables.push(ringMat);
  pressLight.position.set(WX - LL * 0.7, 0, LZ + 0.03);

  /* ---------------------------------------------------------------- dust motes */
  let dust = null;
  const dustMat = new THREE.ShaderMaterial({uniforms: {uTime: {value: 0}, uPx: {value: 3}, cA: {value: col(PAL.violet).multiplyScalar(1.4)},
    cB: {value: col(0xffe6c0).multiplyScalar(1.2)}}, vertexShader: DUST_VERT, fragmentShader: DUST_FRAG, transparent: true,
    depthWrite: false, blending: THREE.AdditiveBlending});
  disposables.push(dustMat);
  function buildDust(n) {
    if (dust) { scene.remove(dust); dust.geometry.dispose(); dust = null; }
    if (!n) return;
    const pos = new Float32Array(n * 3), seed = new Float32Array(n);
    let s = 7;
    const rnd = () => { s = (s * 16807) % 2147483647; return s / 2147483647; };
    for (let i = 0; i < n; i++) {
      pos[i * 3] = BX + rnd() * LEN; pos[i * 3 + 1] = (rnd() * 2 - 1) * SY * 0.95; pos[i * 3 + 2] = 0.01 + rnd() * (H2 - 0.02);
      seed[i] = rnd();
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    g.setAttribute('seed', new THREE.BufferAttribute(seed, 1));
    dust = new THREE.Points(g, dustMat); dust.renderOrder = 4; dust.frustumCulled = false; scene.add(dust);
  }
  buildDust(TIERS[tier].dust);
  st.cleanup.push(() => { if (dust) dust.geometry.dispose(); });

  /* ---------------------------------------------------------------- the rat: CPU LBS + shell fur (rig.html) */
  const RAT = buildRat(THREE, R, scene, TIERS[tier].shells);
  st.cleanup.push(() => RAT.dispose());

  /* ---------------------------------------------------------------- HUD */
  const hud = opts.hud === false ? null : buildHud(el, opts.hud === 'full', opts.marginBottom);
  if (hud) st.cleanup.push(() => hud.root.remove());

  /* ================================================================ data: replay + live */
  const cur = new Float32Array(FF);          // the interpolated frame being shown
  const LV = {ws: null, open: false, relayLive: false, hello: null, history: [], steps: null, frames: [], seq: 0,
    shownSeq: 0, ph: null, stream: 0, lastFrameAt: 0, lastEpisode: null, retry: 0, epFlash: 0};
  const RP = {tc: 0, i: 0, lastIdx: -1, speed: 1, ev: -2, tkey: ''};
  const FFWD = Number.isFinite(+opts.replayFastForward) && +opts.replayFastForward >= 1 ? +opts.replayFastForward : 4;
  let mode = 'none', pending = null, fade = 0, fadeTo = 1;
  let target = {idx: -1, lit: false, doneN: 0, lastLabel: ''};
  let presses = 0, lastEp = null;
  window.__labratFrames = window.__labratFrames || 0;
  window.__labratLiveFrames = window.__labratLiveFrames || 0;
  window.__labratMode = 'none';

  const relay = relayUrl(opts);
  function status() {
    if (mode === 'live') {
      const h = LV.hello || {};
      if (isTestHello(h)) {
        // a test stream (publish_training.py --assume-live-for-test, local relays only): it plays like a stream but
        // it is not live training, so it is never reported as live
        const l = String(h.label || '').trim();
        return {live: false, test: true, source: 'test', label: /^TEST\b/.test(l) ? l : 'TEST · ' + (l || `run ${h.run || ''}`),
          task: h.task || null, run: h.run || null, steps: LV.steps, fps: h.fps || 25, started: h.started || null,
          note: 'a saved checkpoint playing in its own simulation (test stream, not live training)'};
      }
      return {live: true, source: 'training', label: 'LIVE · ' + (h.label || `training run ${h.run || ''}`).trim(),
        task: h.task || null, run: h.run || null, steps: LV.steps, fps: h.fps || 25, started: h.started || null, note: LIVE_NOTE};
    }
    if (mode === 'replay' && replay) {
      const m = replay.meta || {};
      const lbl = String(m.label || REPLAY_LABEL).replace(/^\s*replay\s*[:·-]\s*/i, '');
      return {live: false, source: 'replay', label: 'REPLAY · ' + lbl, task: m.task || 'cursor', run: m.run || null,
        steps: null, waiting: LV.relayLive};
    }
    return {live: false, source: 'none', label: 'STANDBY · waiting for a training run', task: null, run: null,
      steps: null, waiting: LV.relayLive, loading: replayLoading || replay !== null};   // (loaded: switching to it)
  }
  let lastStatusKey = '';
  function pushStatus(force) {
    const s = status();
    const k = JSON.stringify(s);
    if (!force && k === lastStatusKey) return;
    lastStatusKey = k;
    el.classList.toggle('is-live', s.live === true);
    el.classList.toggle('is-test', s.test === true);
    safe(opts.onStatus, s);
    if (hud) hud.setStatus(s, mode);
  }

  function wantMode(now) {
    const liveOk = LV.relayLive && LV.frames.length > 0 && now - LV.lastFrameAt < STALL_MS;
    if (liveOk) return 'live';
    if (replay) return 'replay';
    return 'none';
  }
  function enterMode(m) {
    mode = m;
    window.__labratMode = m;
    const test = m === 'live' && isTestHello(LV.hello);
    el.classList.toggle('is-live', m === 'live' && !test);
    el.classList.toggle('is-test', test);
    el.classList.toggle('is-replay', m === 'replay');
    presses = 0; lastEp = null; target = {idx: -1, lit: false, doneN: 0, lastLabel: ''};
    trail.forEach(v => v.set(-1, -1));
    scrU.uClick.value.z = -1;
    if (m === 'live') { LV.ph = null; LV.shownSeq = LV.frames.length ? LV.frames[LV.frames.length - 1].seq - 1 : 0; }
    if (m === 'replay') { RP.tc = 0; RP.i = 0; RP.lastIdx = -1; RP.ev = -2; RP.tkey = ''; }
    textKey = '';
    pushStatus(true);
  }

  /* ---- live relay */
  let retryTimer = 0;
  function connect() {
    if (st.disposed || !relay) return;
    clearTimeout(retryTimer);
    let ws;
    try { ws = new WebSocket(relay); } catch (e) { retryTimer = setTimeout(connect, RETRY_MS); return; }
    LV.ws = ws; ws.binaryType = 'arraybuffer';
    ws.onopen = () => { LV.open = true; safe(opts.onRelay, true); };
    ws.onmessage = ev => {
      if (st.disposed) return;
      if (typeof ev.data !== 'string') {
        // the relay's pons channel (b"PJPG" + a JPEG): not a rat frame; handed to the page's pons panel as is
        if (isPonsFrame(ev.data)) { safe(opts.onPonsFrame, ev.data); return; }
        onBinary(ev.data); return;
      }
      if (ev.data.length > 65536) return;
      let m; try { m = JSON.parse(ev.data); } catch (_) { return; }
      if (!m || typeof m !== 'object') return;
      // pons channel messages ("channel":"pons": pons_hello / step / result / bye / state / idle) never touch the
      // training view (a pons idle must not end a live training stream); the page's pons panel gets them
      if (m.channel === 'pons' || (typeof m.type === 'string' && m.type.startsWith('pons_'))) { safe(opts.onPons, m); return; }
      onText(m);
    };
    ws.onclose = () => {
      if (LV.ws !== ws) return;
      LV.ws = null; LV.open = false; liveOff();
      safe(opts.onRelay, false);
      if (!st.disposed) retryTimer = setTimeout(connect, RETRY_MS);
    };
    ws.onerror = () => { try { ws.close(); } catch (_) { /* ignore */ } };
  }
  st.cleanup.push(() => {
    clearTimeout(retryTimer); const ws = LV.ws; LV.ws = null; if (ws) try { ws.close(); } catch (_) { /* ignore */ }
    // the view is gone for good (WebGL lost, or a failed start): the page's pons panel must get its own socket
    safe(opts.onRelay, false, true);
  });

  function liveOff() {
    LV.relayLive = false; LV.frames.length = 0; LV.ph = null;
    pushStatus();
  }
  function onText(m) {
    switch (m.type) {
      case 'state': {
        if (Array.isArray(m.history)) {
          LV.history = m.history.slice(-300);
          if (LV.history.length) safe(opts.onMetrics, LV.history.slice(), {live: !!m.live});
        }
        // the checkpoint playing now (its step count, not the newest training log row's)
        LV.steps = m.checkpoint && typeof m.checkpoint === 'object' && Number.isFinite(m.checkpoint.steps) ? m.checkpoint.steps : null;
        if (m.live && m.hello && typeof m.hello === 'object') {
          LV.hello = m.hello; LV.relayLive = true;
          if (m.episode && typeof m.episode === 'object') { LV.lastEpisode = m.episode; if (hud) hud.setEpisode(m.episode); }
        } else if (!m.live) liveOff();
        pushStatus();
        break;
      }
      case 'hello':
        LV.hello = m; LV.relayLive = true; LV.frames.length = 0; LV.ph = null; LV.steps = null;
        LV.history = [];
        pushStatus();
        break;
      case 'metrics':
        if (m.row && typeof m.row === 'object') {
          LV.history.push(m.row); if (LV.history.length > 300) LV.history.splice(0, LV.history.length - 300);
          safe(opts.onMetrics, LV.history.slice(), {live: LV.relayLive, row: m.row});
          if (hud) hud.setMetric(m.row);
        }
        break;
      case 'checkpoint':
        if (Number.isFinite(m.steps)) LV.steps = m.steps;
        if (hud && mode === 'live') hud.toast('new checkpoint loaded · ' + fmtSteps(m.steps));
        pushStatus();
        break;
      case 'episode':
        LV.lastEpisode = m;
        safe(opts.onEpisode, m);
        if (hud) hud.setEpisode(m);
        break;
      case 'bye': case 'idle':
        liveOff();
        break;
      default: break;
    }
  }
  function onBinary(buf) {
    if (!LV.relayLive || !(buf instanceof ArrayBuffer) || buf.byteLength !== FBYTES) return;
    const d = new Float32Array(buf);
    if (d[0] !== MAGIC) return;
    for (let i = HDR; i < FF; i += 7) if (!Number.isFinite(d[i]) || !Number.isFinite(d[i + 3])) return;
    const prev = LV.frames[LV.frames.length - 1];
    const fps = (LV.hello && Number(LV.hello.fps)) || 25;
    let step = 1 / fps;
    if (prev && prev.d[2] === d[2]) { const ds = d[1] - prev.d[1]; if (ds > 1e-4 && ds < 1.0) step = ds; }
    LV.stream = prev ? LV.stream + step : 0;
    LV.frames.push({t: LV.stream, d, seq: ++LV.seq});
    const newest = LV.stream;
    while (LV.frames.length > 2 && (LV.frames[0].t < newest - 6 || LV.frames.length > 300)) LV.frames.shift();
    LV.lastFrameAt = performance.now();
    window.__labratLiveFrames++;
  }

  /* ---- sampling (interpolation between frames: positions lerp, quaternions nlerp; discrete fields snap) */
  function blend(a, b, al) {
    if (!b || a === b) { cur.set(a); cur[4] = 0; return; }
    const near = al < 0.5 ? a : b;
    cur[0] = MAGIC; cur[1] = a[1] + (b[1] - a[1]) * al; cur[2] = near[2];
    cur[3] = a[3] + (b[3] - a[3]) * al; cur[4] = 0;
    if (a[5] >= 0 && b[5] >= 0) { cur[5] = a[5] + (b[5] - a[5]) * al; cur[6] = a[6] + (b[6] - a[6]) * al; }
    else { cur[5] = near[5]; cur[6] = near[6]; }
    for (let k = 7; k < 11; k++) cur[k] = near[k];
    cur[11] = 0;
    for (let o = HDR; o < FF; o += 7) {
      cur[o] = a[o] + (b[o] - a[o]) * al; cur[o + 1] = a[o + 1] + (b[o + 1] - a[o + 1]) * al; cur[o + 2] = a[o + 2] + (b[o + 2] - a[o + 2]) * al;
      let w = b[o + 3], x = b[o + 4], y = b[o + 5], z = b[o + 6];
      if (a[o + 3] * w + a[o + 4] * x + a[o + 5] * y + a[o + 6] * z < 0) { w = -w; x = -x; y = -y; z = -z; }
      cur[o + 3] = a[o + 3] + (w - a[o + 3]) * al; cur[o + 4] = a[o + 4] + (x - a[o + 4]) * al;
      cur[o + 5] = a[o + 5] + (y - a[o + 5]) * al; cur[o + 6] = a[o + 6] + (z - a[o + 6]) * al;
    }
  }
  const events = [];                            // frames crossed this tick: presses / clicks / episode changes
  function sampleLive(dt) {
    const F = LV.frames; if (!F.length) return false;
    const newest = F[F.length - 1].t, goal = newest - LIVE_DELAY;
    if (LV.ph === null) LV.ph = Math.max(F[0].t, goal);
    LV.ph += dt;
    if (Math.abs(goal - LV.ph) > 0.6) LV.ph = goal;
    else LV.ph += (goal - LV.ph) * Math.min(1, dt * 1.5);
    LV.ph = clamp(LV.ph, F[0].t, newest);
    let i = F.length - 1;
    while (i > 0 && F[i].t > LV.ph) i--;
    const a = F[i], b = F[Math.min(i + 1, F.length - 1)];
    let al = b.t > a.t ? clamp((LV.ph - a.t) / (b.t - a.t), 0, 1) : 0;
    if (a.d[2] !== b.d[2]) al = al < 0.5 ? 0 : 1;         // never blend across an episode reset
    for (const f of F) if (f.seq > LV.shownSeq && f.t <= LV.ph + 1e-6) { events.push(f.d); LV.shownSeq = f.seq; }
    blend(a.d, b.d, al);
    return true;
  }
  // fast-forward weight at clip time tc: 0 outside the long waits, easing to 1 in their middle
  function ffAt(tc) {
    if (!replay || FFWD <= 1) return 0;
    for (const [a, b] of replay.gaps) {
      const lo = a + 0.8, hi = b - 0.8;
      if (tc > lo && tc < hi) return clamp(Math.min(tc - lo, hi - tc) / 0.5, 0, 1);
    }
    return 0;
  }
  function sampleReplay(dt) {
    const Rp = replay; if (!Rp) return false;
    RP.speed = 1 + (FFWD - 1) * ffAt(RP.tc);
    RP.tc += dt * RP.speed;
    if (RP.tc >= Rp.loopEnd) {
      RP.tc = 0; RP.i = 0; RP.lastIdx = -1; RP.ev = -2; RP.tkey = '';
      presses = 0; target = {idx: -1, lit: false, doneN: 0, lastLabel: ''}; trail.forEach(v => v.set(-1, -1));
    }
    const tt = Rp.times;
    let i = RP.i;
    while (i + 1 < Rp.n && tt[i + 1] <= RP.tc) i++;
    RP.i = i;
    const j = Math.min(Rp.n - 1, i + 1);
    let jumped = false;
    for (let k = RP.lastIdx + 1; k <= i; k++) { events.push(Rp.frames[k]); if (k > 0 && Rp.brk[k - 1]) jumped = true; }
    RP.lastIdx = i;
    if (jumped) trail.forEach(v => v.set(-1, -1));      // the rig reset the rat: no cursor streak across the jump
    let al = tt[j] > tt[i] ? clamp((RP.tc - tt[i]) / (tt[j] - tt[i]), 0, 1) : 0;
    if (Rp.brk[i]) al = 0;
    blend(Rp.frames[i], Rp.frames[j], al);
    return true;
  }
  // which of the 11 targets is lit / done at clip time tc, from the recorded lit and click times
  function replayTargetAt(tc) {
    const T = replay ? replay.targets : [];
    if (!T.length || T.some(t => t.lit === null || t.hit === null)) return null;
    let idx = -1, done = 0;
    for (let k = 0; k < T.length; k++) { if (T[k].lit <= tc) idx = k; if (T[k].hit <= tc) done++; }
    return {idx, lit: idx >= 0 && tc < T[idx].hit, done};
  }
  // the recorded timeline entry in effect at clip time tc (-1: before the first one)
  function eventAt(tc) {
    const E = replay ? replay.timeline : [];
    let k = -1;
    while (k + 1 < E.length && E[k + 1].t <= tc) k++;
    return k;
  }
  function timelineEvent(k) {
    if (!replay) return null;
    if (k >= 0) return replay.timeline[k];
    return replay.brainOn !== null ? {t: 0, kind: 'start', text: `Session start: the brain switches on at ${replay.brainOn.toFixed(1)} s`} : null;
  }
  function idlePose() {                                   // before any data: the bind pose, standing in the box
    cur.fill(0); cur[0] = MAGIC; cur[5] = cur[6] = cur[7] = cur[8] = cur[9] = cur[10] = -1;
    for (let b = 0; b < NB; b++) {
      const o = HDR + b * 7, p = R.bind_pos[b], q = R.bind_quat[b];
      cur[o] = p[0] - 0.066; cur[o + 1] = p[1]; cur[o + 2] = p[2] + 0.003;
      cur[o + 3] = q[0]; cur[o + 4] = q[1]; cur[o + 5] = q[2]; cur[o + 6] = q[3];
    }
  }

  /* ---- replay targets: which of the 11 is lit, from the frame's box (and the clip time when the json gives it) */
  function findTarget(f, tClip) {
    const T = replay ? replay.targets : [];
    if (!T.length || !(f[9] > 0)) return -1;
    const inWin = t => t.lit !== null && t.hit !== null && tClip >= t.lit - 0.5 && tClip <= t.hit + 0.5;
    let best = -1, bestScore = Infinity;
    for (let i = 0; i < T.length; i++) {
      let e = Infinity;
      for (const nm of T[i].norms)
        e = Math.min(e, Math.max(Math.abs(nm[0] - f[7]), Math.abs(nm[1] - f[8]), Math.abs(nm[2] - f[9]), Math.abs(nm[3] - f[10])));
      if (!(e <= 3e-3)) continue;
      // same box twice: prefer the one whose time window holds the clip time, then the one not behind the last
      const score = e - (inWin(T[i]) ? 1 : 0) + (i < target.idx ? 0.5 : 0);
      if (score < bestScore) { bestScore = score; best = i; }
    }
    if (best >= 0) return best;
    for (let i = 0; i < T.length; i++) if (inWin(T[i])) return i;
    return -1;
  }

  /* ---- apply the shown frame to the scene */
  let screenMode = 0, leverAng = 0, pressAge = 9, ringAge = 9, clickAge = 9, trailT = 0;
  function taskNow() {
    if (mode === 'live') return (LV.hello && LV.hello.task) || 'lever';
    if (mode === 'replay') return (replay && replay.meta && replay.meta.task) || 'cursor';
    return 'cursor';
  }
  function applyFrame(dt) {
    // bones -> RAT.P / RAT.Qt -> skin
    const P = RAT.P, Q = RAT.Qt;
    for (let b = 0; b < NB; b++) {
      const o = HDR + b * 7;
      P[b * 3] = cur[o]; P[b * 3 + 1] = cur[o + 1]; P[b * 3 + 2] = cur[o + 2];
      Q[b * 4] = cur[o + 3]; Q[b * 4 + 1] = cur[o + 4]; Q[b * 4 + 2] = cur[o + 5]; Q[b * 4 + 3] = cur[o + 6];
    }
    RAT.applyPose();
    leverAng = Number.isFinite(cur[3]) ? cur[3] : 0;
    lever.rotation.y = -leverAng;                    // hinge axis (0,-1,0): a positive angle takes the tip down
    const task = taskNow();
    screenMode = task === 'lever' && !(cur[5] >= 0) && !(cur[9] > 0) ? 1 : 0;
    scrU.uMode.value = screenMode;
    scrU.uLever.value = clamp(leverAng * 180 / Math.PI / 30, 0, 1);
    // cursor + trail
    const hasCur = cur[5] >= 0 && cur[6] >= 0;
    scrU.uCur.value.set(hasCur ? cur[5] : -1, hasCur ? cur[6] : -1);
    trailT += dt;
    if (trailT > 0.035) {
      trailT = 0;
      for (let i = trail.length - 1; i > 0; i--) trail[i].copy(trail[i - 1]);
      trail[0].set(hasCur ? cur[5] : -1, hasCur ? cur[6] : -1);
    }
    // target
    const hasTgt = cur[9] > 0 && cur[10] > 0;
    if (hasTgt) scrU.uTgt.value.set(cur[7], cur[8], cur[9], cur[10]);
    scrU.uTgtA.value += ((hasTgt ? 1 : 0) - scrU.uTgtA.value) * Math.min(1, dt * (hasTgt ? 10 : 4));
    if (!hasTgt && scrU.uTgtA.value < 0.01) scrU.uTgt.value.z = -1;
    if (mode === 'replay') {
      const tClip = RP.tc;
      const tt = replayTargetAt(tClip);
      if (tt) { target.idx = tt.idx; target.lit = tt.lit; target.doneN = tt.done; }
      else {
        const idx = hasTgt ? findTarget(cur, tClip) : -1;
        if (idx >= 0 && idx !== target.idx) { target.idx = idx; target.lit = true; target.doneN = Math.max(target.doneN, idx); }
        if (!hasTgt && target.lit) { target.lit = false; }
      }
      const key = `${target.idx}|${target.lit}|${target.doneN}`;
      if (key !== RP.tkey) {
        RP.tkey = key;
        const T = replay.targets[target.idx];
        safe(opts.onTarget, {n: target.idx + 1, total: replay.targets.length, label: T ? T.label : '', lit: target.lit,
          done: target.doneN});
      }
      const k = eventAt(tClip);
      if (k !== RP.ev) {
        RP.ev = k;
        const e = timelineEvent(k);
        if (e) { safe(opts.onEvent, e); if (hud) hud.event(e, replay.timeline, k); }
      }
    } else if (hasTgt) { target.lit = true; } else target.lit = false;
    // events crossed since the last tick
    for (const f of events) {
      if (lastEp !== null && f[2] !== lastEp) { presses = 0; }
      lastEp = f[2];
      if (f[4] === 1) {
        presses++;
        const cx = f[5], cy = f[6];
        const inT = f[9] > 0 && Math.abs(cx - f[7]) <= f[9] + 1e-4 && Math.abs(cy - f[8]) <= f[10] + 1e-4;
        pressAge = 0; ringAge = 0;
        if (cx >= 0) { clickAge = 0; scrU.uClick.value.set(cx, cy, 0, inT ? 1 : 0); }
        if (mode === 'replay' && inT && target.idx >= 0) target.doneN = Math.max(target.doneN, target.idx + 1);
        if (hud) hud.pulse(inT || cx < 0);
      }
    }
    events.length = 0;
  }
  function updateFx(dt, t) {
    pressAge += dt; ringAge += dt; clickAge += dt;
    scrU.uTime.value = t;
    scrU.uPress.value = pressAge;
    scrU.uClick.value.z = clickAge < 0.9 ? clickAge : -1;
    const lit = scrU.uTgtA.value;
    screenLight.intensity = 0.0045 + 0.004 * lit + 0.01 * Math.exp(-clickAge * 5);
    screenLight.color.setHex(0x7a4cff).lerp(new THREE.Color(PAL.gold), 0.45 * lit);
    pressLight.intensity = 0.02 * Math.exp(-pressAge * 4.5);
    ring.visible = ringAge < 1.1;
    if (ring.visible) {
      const s = 0.01 + 0.13 * (1 - Math.pow(1 - Math.min(1, ringAge / 1.1), 3));
      ring.scale.setScalar(s); ringMat.opacity = 0.9 * (1 - ringAge / 1.1);
    }
    nebU.uTime.value = t;
    dustMat.uniforms.uTime.value = t;
  }

  /* ---- the screen's text layer: header, target label, footer (redrawn only when it changes) */
  let textKey = '', fontsReady = false;
  if (document.fonts && document.fonts.load) {
    Promise.all([document.fonts.load('600 30px "JetBrains Mono"'), document.fonts.load('600 30px "Space Grotesk"')])
      .then(() => { fontsReady = true; textKey = ''; }).catch(() => { /* fallback fonts */ });
  }
  function screenText() {
    const task = taskNow();
    let label = '', right = '', note = '', evTag = '', evText = '', evGold = false;
    const hasTgt = scrU.uTgt.value.z > 0 && scrU.uTgtA.value > 0.05;
    if (mode === 'replay' && replay) {
      const T = replay.targets, n = T.length;
      if (target.idx >= 0 && T[target.idx]) {
        label = (target.lit ? '' : '✓ ') + T[target.idx].label;
        right = `TARGET ${target.idx + 1} / ${n}`;
      } else right = n ? `TARGET – / ${n}` : '';
      note = 'cursor = head direction · click = lever press';
      const e = timelineEvent(RP.ev);
      if (e && !hasTgt && e.kind !== 'lit' && e.kind !== 'click') {
        evTag = KIND[e.kind] || e.kind.toUpperCase(); evText = e.text; evGold = e.kind === 'tx_mined';
      }
    } else if (mode === 'live') {
      right = `EPISODE ${Number.isFinite(cur[2]) ? cur[2] | 0 : '–'}`;
      if (screenMode === 1) note = `lever task · presses this episode: ${presses}`;
      else { note = 'cursor = head direction · click = lever press'; label = hasTgt ? 'target' : ''; }
    } else note = 'waiting for data';
    const key = [mode, task, screenMode, label, right, note, evTag, evText, hasTgt ? scrU.uTgt.value.toArray().map(v => v.toFixed(3)).join() : '', fontsReady].join('|');
    if (key === textKey) return;
    textKey = key;
    const c = scrCtx, W = scrText.width, H = scrText.height, mono = '"JetBrains Mono", ui-monospace, monospace';
    c.clearRect(0, 0, W, H);
    c.textBaseline = 'alphabetic';
    c.font = `600 25px ${mono}`;
    c.fillStyle = 'rgba(210,192,240,0.92)';
    c.fillText('STIMULUS SCREEN', 34, 50);
    const w0 = c.measureText('STIMULUS SCREEN ').width;
    c.fillStyle = 'rgba(174,142,228,0.8)';
    c.fillText('· SCHEMATIC', 34 + w0, 50);
    if (right) { c.textAlign = 'right'; c.fillStyle = 'rgba(252,240,16,0.9)'; c.fillText(right, W - 34, 50); c.textAlign = 'left'; }
    if (screenMode === 1) {
      c.font = `700 58px "Space Grotesk", system-ui, sans-serif`;
      c.fillStyle = 'rgba(244,240,251,0.95)'; c.textAlign = 'center';
      c.fillText('LEVER', W / 2, H * 0.4);
      c.font = `500 24px ${mono}`; c.fillStyle = 'rgba(210,192,240,0.9)';
      const thx = (0.10 + 0.80 * scrU.uThr.value) * W;
      c.fillText(`press = past ${(CH.PRESS_ANGLE_DEG || 11.459).toFixed(1)}°`, thx, H * 0.75);
      c.textAlign = 'left';
      c.fillStyle = 'rgba(174,142,228,0.85)'; c.fillText('0°', 0.10 * W, H * 0.52);
      c.textAlign = 'right'; c.fillText('30°', 0.90 * W, H * 0.52); c.textAlign = 'left';
    }
    if (label && hasTgt) {
      const t = scrU.uTgt.value, cx = t.x * W, top = (t.y - t.w) * H, bot = (t.y + t.w) * H;
      c.font = `600 34px "Space Grotesk", system-ui, sans-serif`;
      const tw = c.measureText(label).width;
      let x = clamp(cx - tw / 2, 30, W - 30 - tw);
      let y = top - 26 > 96 ? top - 26 : bot + 50;
      c.fillStyle = 'rgba(6,2,16,0.78)';
      roundRect(c, x - 14, y - 34, tw + 28, 48, 6); c.fill();
      c.fillStyle = 'rgba(252,240,16,0.98)';
      c.fillText(label, x, y);
    } else if (evText) {
      // what the rig (not the rat) is doing between targets, from the recorded timeline
      c.font = `600 40px "Space Grotesk", system-ui, sans-serif`;
      const lines = wrapText(c, evText, W * 0.8, 3);
      const lh = 52, y0 = H * 0.5 - (lines.length - 1) * lh / 2 + 10;
      c.textAlign = 'center';
      c.font = `700 24px ${mono}`; c.fillStyle = evGold ? 'rgba(252,240,16,0.95)' : 'rgba(245,172,41,0.95)';
      c.fillText(evTag.split('').join(String.fromCharCode(8202)), W / 2, y0 - 62);
      c.font = `600 40px "Space Grotesk", system-ui, sans-serif`;
      c.fillStyle = evGold ? 'rgba(252,240,16,0.98)' : 'rgba(244,240,251,0.94)';
      lines.forEach((l, i) => c.fillText(l, W / 2, y0 + i * lh));
      c.textAlign = 'left';
    } else if (label && mode === 'replay') {
      c.font = `600 34px "Space Grotesk", system-ui, sans-serif`;
      c.fillStyle = 'rgba(252,240,16,0.85)'; c.textAlign = 'center';
      c.fillText(label, W / 2, H * 0.52); c.textAlign = 'left';
    }
    if (note) { c.font = `500 21px ${mono}`; c.fillStyle = 'rgba(156,117,223,0.9)'; c.fillText(note, 34, H - 34); }
    scrTex.needsUpdate = true;
  }

  /* ================================================================ camera: slow cinematic orbit + drag */
  const cam = {az: -2.52, el: 0.17, dist: 0.7, distTo: 0.7, tgt: new THREE.Vector3(0.03, 0, 0.08), tgtTo: new THREE.Vector3(0.03, 0, 0.08),
    userAt: -1e9, drag: null, fitAt: 0, shift: 0};
  const AZ0 = -2.52, AZA = 0.36, AZP = 74, EL0 = 0.17, ELA = 0.035, ELP = 53;
  const autoAz = t => reduceMotion ? AZ0 : AZ0 + AZA * Math.sin(2 * Math.PI * t / AZP);
  const autoEl = t => reduceMotion ? EL0 : EL0 + ELA * Math.sin(2 * Math.PI * t / ELP + 1.0);
  const wrapA = a => { while (a > Math.PI) a -= 2 * Math.PI; while (a < -Math.PI) a += 2 * Math.PI; return a; };
  const keepOffPanel = a => { a = wrapA(a); return Math.abs(a) < 0.5 ? (a < 0 ? -0.5 : 0.5) : a; };
  const onDown = e => {
    if (e.button !== undefined && e.button !== 0) return;
    cam.drag = {id: e.pointerId, x: e.clientX, y: e.clientY};
    try { canvas.setPointerCapture(e.pointerId); } catch (_) { /* ignore */ }
    canvas.classList.add('drag'); cam.userAt = performance.now();
    if (hud) hud.hint(false);
  };
  const onMove = e => {
    if (!cam.drag || cam.drag.id !== e.pointerId) return;
    const dx = e.clientX - cam.drag.x, dy = e.clientY - cam.drag.y;
    cam.drag.x = e.clientX; cam.drag.y = e.clientY;
    cam.az = keepOffPanel(cam.az - dx * 0.0065);
    cam.el = clamp(cam.el + dy * 0.0045, 0.06, 1.25);
    cam.userAt = performance.now();
    kick();
  };
  const onUp = e => {
    if (!cam.drag || cam.drag.id !== e.pointerId) return;
    cam.drag = null; canvas.classList.remove('drag'); cam.userAt = performance.now();
  };
  const onDbl = () => { cam.userAt = -1e9; };
  canvas.addEventListener('pointerdown', onDown);
  canvas.addEventListener('pointermove', onMove);
  canvas.addEventListener('pointerup', onUp);
  canvas.addEventListener('pointercancel', onUp);
  canvas.addEventListener('dblclick', onDbl);
  st.cleanup.push(() => { canvas.removeEventListener('pointerdown', onDown); canvas.removeEventListener('pointermove', onMove);
    canvas.removeEventListener('pointerup', onUp); canvas.removeEventListener('pointercancel', onUp); canvas.removeEventListener('dblclick', onDbl); });

  // auto-framing: the smallest distance at which the rat (tail beyond its base left out), the lever and the
  // screen fit the free part of the view, at every angle of the auto orbit (so the orbit never pumps)
  const TAIL = new Set(R.bones.map((n, i) => /^vertebra_C(\d+)$/.test(n) && +/^vertebra_C(\d+)$/.exec(n)[1] >= 5 ? i : -1).filter(i => i >= 0));
  const fixedPts = [[WX - LL, 0, LZ], [WX, SW / 2, SZ - SHh / 2], [WX, -SW / 2, SZ - SHh / 2], [WX, SW / 2, SZ + SHh / 2],
    [WX, -SW / 2, SZ + SHh / 2]];
  function fit(W, H, mT, mB) {
    const pts = fixedPts.slice();
    for (let b = 0; b < NB; b++) if (!TAIL.has(b)) pts.push([RAT.P[b * 3], RAT.P[b * 3 + 1], RAT.P[b * 3 + 2]]);
    let x0 = 1e9, x1 = -1e9, y0 = 1e9, y1 = -1e9, z0 = 1e9, z1 = -1e9;
    for (const p of pts) { x0 = Math.min(x0, p[0]); x1 = Math.max(x1, p[0]); y0 = Math.min(y0, p[1]); y1 = Math.max(y1, p[1]);
      z0 = Math.min(z0, p[2]); z1 = Math.max(z1, p[2]); }
    cam.tgtTo.set((x0 + x1) / 2, (y0 + y1) / 2 * 0.5, Math.max(0.05, (z0 + z1) / 2));
    const tV = Math.tan(camera.fov * Math.PI / 360) * clamp((H - mT - mB) / H, 0.4, 1) * 0.92;
    const tH = Math.tan(camera.fov * Math.PI / 360) * (W / H) * 0.93;
    // the angles the camera is at now and will pass through over the next few seconds (the orbit is slow and the
    // distance eases, so the framing breathes gently instead of pumping)
    const pad = 0.012, views = [[cam.az, cam.el]];
    if (!reduceMotion && !cam.drag && cam.userAt < performance.now() - 6000)
      for (const s of [2.5, 5, 7.5]) views.push([autoAz(tClock + s), autoEl(tClock + s)]);
    let d = 0.2;
    for (const [az, el2] of views) {
      const c = [Math.cos(el2) * Math.cos(az), Math.cos(el2) * Math.sin(az), Math.sin(el2)];
      const rt = [Math.sin(az), -Math.cos(az), 0];                         // camera right
      const up = [-Math.sin(el2) * Math.cos(az), -Math.sin(el2) * Math.sin(az), Math.cos(el2)];
      for (const p of pts) {
        const q = [p[0] - cam.tgtTo.x, p[1] - cam.tgtTo.y, p[2] - cam.tgtTo.z];
        const qc = q[0] * c[0] + q[1] * c[1] + q[2] * c[2];
        const r = Math.abs(q[0] * rt[0] + q[1] * rt[1]) + pad;
        const u = Math.abs(q[0] * up[0] + q[1] * up[1] + q[2] * up[2]) + pad;
        d = Math.max(d, qc + r / tH, qc + u / tV);
      }
    }
    cam.distTo = clamp(d, 0.25, 2.2);
    cam.shift = (mB - mT) / 2;
  }
  let tClock = 0;
  const _pv = [new THREE.Vector3(), new THREE.Vector3(), new THREE.Vector3()];
  const SKULL = Math.max(0, R.face && Number.isInteger(R.face.skull_bone) ? R.face.skull_bone : R.bones.indexOf('skull'));
  const BODY = [...new Set([0, SKULL, ...['pelvis', 'upper_leg_L', 'upper_leg_R', 'vertebra_C1', 'hand_L', 'hand_R']
    .map(n => R.bones.indexOf(n)).filter(i => i >= 0)])];
  function updateCamera(dt, now) {
    tClock += dt;
    if (now - cam.fitAt > 700) { cam.fitAt = now; fit(Wc, Hc, hud ? hud.top() : 0, hud ? hud.bottom() : 0); }
    cam.dist += (cam.distTo - cam.dist) * Math.min(1, dt * 0.8);
    cam.tgt.lerp(cam.tgtTo, Math.min(1, dt * 0.9));
    const idle = now - cam.userAt > 6000 && !cam.drag;
    if (idle) {
      const k = Math.min(1, dt * 0.45);
      cam.az = wrapA(cam.az + wrapA(autoAz(tClock) - cam.az) * k);
      cam.el += (autoEl(tClock) - cam.el) * k;
    }
    const d = cam.dist, az = cam.az, el = cam.el;
    camera.position.set(cam.tgt.x + d * Math.cos(el) * Math.cos(az), cam.tgt.y + d * Math.cos(el) * Math.sin(az),
      cam.tgt.z + d * Math.sin(el));
    camera.lookAt(cam.tgt);
    if (Math.abs(cam.shift) > 0.5) camera.setViewOffset(Wc, Hc, 0, cam.shift, Wc, Hc);
    else if (camera.view && camera.view.enabled) camera.clearViewOffset();
    mComp.uniforms.uCy.value = 0.5 + cam.shift / Hc;
    // cutaway: a corner post that stands between the camera and the rat (or right next to the lens) fades out
    camera.updateMatrixWorld();
    let x0 = Infinity, x1 = -Infinity, zr = Infinity;       // the rat's body (tail left out) on screen
    for (const b of BODY) {
      const v = _pv[0].set(RAT.P[b * 3], RAT.P[b * 3 + 1], RAT.P[b * 3 + 2]).project(camera);
      x0 = Math.min(x0, v.x); x1 = Math.max(x1, v.x); zr = Math.min(zr, v.z);
    }
    for (const p of posts) {
      const v = _pv[2].set(p.x, p.y, 0.1).project(camera);
      let o = 0.1 + 0.9 * clamp((Math.hypot(camera.position.x - p.x, camera.position.y - p.y) - 0.1) / 0.16, 0, 1);
      if (v.z < zr) {                                    // in front of the rat: dim it near the rat's body on screen
        const dx = v.x < x0 ? x0 - v.x : v.x > x1 ? v.x - x1 : 0;
        o = Math.min(o, 0.3 + 0.7 * clamp((dx - 0.12) / 0.5, 0, 1));
      }
      p.o = p.o === undefined ? o : p.o + (o - p.o) * Math.min(1, dt * 3);
      for (const m of p.mats) m.opacity = p.o;
    }
    // lights that ride with the camera: warm key front-right, rims behind the rat in the palette's magenta and gold
    const L = (l, dAz, el2, r) => {
      l.position.set(cam.tgt.x + r * Math.cos(el2) * Math.cos(az + dAz), cam.tgt.y + r * Math.cos(el2) * Math.sin(az + dAz),
        cam.tgt.z + r * Math.sin(el2));
      l.target.position.copy(cam.tgt);
    };
    L(key, -0.75, 0.62, 0.8); L(fill, 0.9, 0.12, 0.8); L(rimM, Math.PI - 0.95, 0.42, 0.9); L(rimG, Math.PI + 0.95, 0.34, 0.9);
  }

  /* ================================================================ size, quality, render */
  let Wc = 1, Hc = 1, dprNow = 1;
  function resize(force) {
    const w = Math.max(1, Math.round(el.clientWidth)), h = Math.max(1, Math.round(el.clientHeight));
    const dpr = Math.min(DPR_MAX, window.devicePixelRatio || 1, TIERS[tier].dpr);
    if (!force && w === Wc && h === Hc && dpr === dprNow) return;
    Wc = w; Hc = h; dprNow = dpr;
    renderer.setPixelRatio(dpr);
    renderer.setSize(w, h, false);
    const bw = Math.max(2, Math.round(w * dpr)), bh = Math.max(2, Math.round(h * dpr));
    sceneRT.setSize(bw, bh);
    let lw = Math.max(2, Math.round(bw * TIERS[tier].bloom)), lh = Math.max(2, Math.round(bh * TIERS[tier].bloom));
    for (const r of bloomRT) { r.setSize(lw, lh); lw = Math.max(2, lw >> 1); lh = Math.max(2, lh >> 1); }
    camera.aspect = w / h; camera.updateProjectionMatrix();
    mComp.uniforms.uRes.value.set(w, h);
    dustMat.uniforms.uPx.value = 0.0022 * bh / (2 * Math.tan(camera.fov * Math.PI / 360));
    cam.fitAt = 0;
  }
  function setTier(t) {
    t = clamp(t | 0, 0, 2);
    if (t === tier) return;
    tier = t;
    RAT.setShells(TIERS[t].shells);
    buildDust(TIERS[t].dust);
    const sm = TIERS[t].shadow;
    if (house.shadow.mapSize.x !== sm) { house.shadow.mapSize.set(sm, sm); if (house.shadow.map) { house.shadow.map.dispose(); house.shadow.map = null; } }
    resize(true);
  }
  function render() {
    renderer.setRenderTarget(sceneRT);
    renderer.clear(true, true, false);
    renderer.render(scene, camera);
    // bloom: soft-threshold prefilter at the first level, dual-filter blur down the chain and back up
    fsQuad.material = mPre; mPre.uniforms.tSrc.value = sceneRT.texture;
    mPre.uniforms.uTexel.value.set(1 / sceneRT.width, 1 / sceneRT.height);
    renderer.setRenderTarget(bloomRT[0]); renderer.render(fsScene, fsCam);
    fsQuad.material = mDown;
    for (let i = 1; i < NLEV; i++) {
      mDown.uniforms.tSrc.value = bloomRT[i - 1].texture;
      mDown.uniforms.uTexel.value.set(1 / bloomRT[i - 1].width, 1 / bloomRT[i - 1].height);
      renderer.setRenderTarget(bloomRT[i]); renderer.render(fsScene, fsCam);
    }
    fsQuad.material = mUp;
    for (let i = NLEV - 2; i >= 0; i--) {
      mUp.uniforms.tSrc.value = bloomRT[i + 1].texture;
      mUp.uniforms.uTexel.value.set(1 / bloomRT[i + 1].width, 1 / bloomRT[i + 1].height);
      mUp.uniforms.uW.value = 1.0;
      renderer.setRenderTarget(bloomRT[i]); renderer.render(fsScene, fsCam);
    }
    fsQuad.material = mComp;
    mComp.uniforms.tScene.value = sceneRT.texture; mComp.uniforms.tBloom.value = bloomRT[0].texture;
    renderer.setRenderTarget(null); renderer.render(fsScene, fsCam);
  }

  /* ================================================================ loop */
  let raf = 0, lastNow = 0, visible = true, running = false, perfT = 0, perfN = 0, slow = 0, frameN = 0, hudAt = 0;
  let live = false, firstResolve = null;          // live: programs compiled, the loop may run
  // render at most ~60 times a second (the data is 20-25 fps; a 120-165 Hz display would otherwise redraw the whole
  // pipeline every refresh). capAcc collects elapsed time; a frame renders once a 60th of a second has built up.
  const FRAME_MS = 1000 / 60;
  let capAcc = 0, capPrev = 0, startAt = 0, fastWins = 0, stepUps = 0;
  const tier0 = tier;                             // the starting tier: the guard may step back up to it, not past
  function tick(now) {
    raf = 0;
    if (st.disposed || document.hidden || !visible) { running = false; return; }
    raf = requestAnimationFrame(tick);
    capAcc = capPrev ? Math.min(capAcc + (now - capPrev), 4 * FRAME_MS) : FRAME_MS;
    capPrev = now;
    if (capAcc < FRAME_MS - 1) return;          // (1 ms of slack: a 60 Hz display never skips a frame)
    capAcc = Math.max(0, Math.min(capAcc - FRAME_MS, FRAME_MS));
    if (!startAt) startAt = now;
    const rawDt = lastNow ? Math.max(0, (now - lastNow) / 1000) : 1 / 60;
    const dt = Math.min(0.25, rawDt);           // a slow device plays a little slow-motion rather than jumping
    lastNow = now;
    // mode: live when frames flow, else the replay; switch through a short fade to black
    const want = wantMode(now);
    if (want !== mode && !pending) { pending = want; fadeTo = 0; }
    if (pending && fade < 0.03) { enterMode(pending); pending = null; fadeTo = 1; }
    fade += (fadeTo - fade) * Math.min(1, dt * (fadeTo > fade ? 3.2 : 7));
    let loopFade = 1;
    if (mode === 'live' && sampleLive(dt)) { /* sampled */ }
    else if (mode === 'replay' && sampleReplay(dt)) {
      const tc = RP.tc;
      loopFade = clamp(Math.min(tc / 0.45, (replay.loopEnd - tc) / 0.45), 0, 1);
    } else if (frameN === 0 || mode === 'none') idlePose();
    applyFrame(dt);
    updateFx(dt, now / 1000);
    screenText();
    updateCamera(dt, now);
    mComp.uniforms.uFade.value = fade * (0.08 + 0.92 * loopFade);
    mComp.uniforms.uSeed.value = (now * 0.001) % 10;
    render();
    frameN++;
    window.__labratFrames = (window.__labratFrames || 0) + 1;
    if (frameN === 1) { if (loadEl) loadEl.remove(); if (firstResolve) firstResolve(); }
    if (hud && now - hudAt > 100) { hudAt = now; hud.update(cur, mode, target, replay, presses, RP, LV); }
    // frame-rate guard: step down a tier (fewer fur shells, lower DPR / bloom, no dust) if the machine cannot keep up
    // (real elapsed time; a very slow device, under 24 fps, steps down after one window instead of two)
    // (not in the first 3 s after mounting, while shaders and textures settle), and step back up after ~10 s at a
    // steady 55+ fps, so one hitch does not cost the rest of the visit its quality
    if (now - startAt < 3000) { perfT = 0; perfN = 0; }
    else { perfT += Math.min(1, rawDt); perfN++; }
    if (perfT > 2) {
      const fps = perfN / perfT;
      if (!forced && fps < 45 && tier > 0) { slow += fps < 24 ? 2 : 1; fastWins = 0; if (slow >= 2) { setTier(tier - 1); slow = 0; } }
      else {
        slow = 0;
        fastWins = fps >= 55 ? fastWins + 1 : 0;       // 55: a 144 Hz display capped to 60 renders ~58
        if (!forced && fastWins >= 5 && tier < tier0 && stepUps < 3) { stepUps++; fastWins = 0; setTier(tier + 1); }
      }
      perfT = 0; perfN = 0;
    }
  }
  function kick() {
    if (st.disposed || !live || running || document.hidden || !visible) return;
    running = true; lastNow = 0; capPrev = 0; raf = requestAnimationFrame(tick);
  }
  st.cleanup.push(() => { if (raf) cancelAnimationFrame(raf); raf = 0; running = false; });
  const ro = new ResizeObserver(() => { resize(); if (hud) hud.measure(); });
  ro.observe(el);
  st.cleanup.push(() => ro.disconnect());
  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver(es => { for (const e of es) visible = e.isIntersecting; if (visible) kick(); });
    io.observe(el);
    st.cleanup.push(() => io.disconnect());
  }
  const onVis = () => { if (!document.hidden) kick(); };
  document.addEventListener('visibilitychange', onVis);
  st.cleanup.push(() => document.removeEventListener('visibilitychange', onVis));
  canvas.addEventListener('webglcontextlost', ev => {
    ev.preventDefault();
    console.warn('labrat live: WebGL context lost');
    teardown(st); showFallback(el);
  });

  if (opts.debug) window.__labratDebug = {THREE, scene, camera, M, nebula, floorMesh, renderer, tier: () => tier};
  resize(true);
  idlePose(); applyFrame(0);
  try {
    if (renderer.compileAsync) await renderer.compileAsync(scene, camera);
    else renderer.compile(scene, camera);
  } catch (_) { /* compile is only a warm-up */ }
  if (st.disposed) return null;
  const first = new Promise(r => { firstResolve = r; });
  live = true;
  kick();
  replayP.then(() => { if (!st.disposed) pushStatus(); });
  connect();
  pushStatus(true);
  // a hidden tab or an off-screen container renders nothing: resolve anyway after a moment
  await Promise.race([first, new Promise(r => setTimeout(r, 4000))]);
  if (st.disposed) return null;
  return {
    dispose: () => teardown(st),
    setQuality: t => setTier(t),
    // jump the replay to clip time t (seconds of the recorded session); no effect while live
    seek(t) {
      if (!replay || !Number.isFinite(+t)) return;
      RP.tc = clamp(+t, 0, replay.loopEnd - 0.01);
      let i = 0; while (i + 1 < replay.n && replay.times[i + 1] <= RP.tc) i++;
      RP.i = i; RP.lastIdx = i; RP.ev = -2; RP.tkey = '';
      trail.forEach(v => v.set(-1, -1));
      kick();
    },
    get mode() { return mode; },
    get status() { return status(); },
    canvas,
  };
}

function fmtSteps(n) {
  if (!Number.isFinite(n)) return '';
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 1 : 2).replace(/\.?0+$/, '') + 'M steps';
  if (n >= 1e3) return Math.round(n / 1e3) + 'k steps';
  return n + ' steps';
}

function wrapText(c, s, maxW, maxLines) {
  const words = String(s).split(/\s+/), out = [];
  let line = '';
  for (const w of words) {
    const t = line ? line + ' ' + w : w;
    if (line && c.measureText(t).width > maxW) { out.push(line); line = w; } else line = t;
  }
  if (line) out.push(line);
  if (out.length > maxLines) { out.length = maxLines; out[maxLines - 1] = out[maxLines - 1].replace(/\s*\S*$/, '') + '…'; }
  return out;
}

function roundRect(c, x, y, w, h, r) {
  c.beginPath(); c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + h, r); c.arcTo(x + w, y + h, x, y + h, r);
  c.arcTo(x, y + h, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath();
}

/* ================================================================ HUD (DOM overlay)
   compact (default): a chip with the replay's current target (or the live episode), one line saying what is
   playing (recorded clip time / "latest saved checkpoint"), a drag hint and a checkpoint toast. The page around
   the view carries the mode badge, the label and the caption.
   full: also the mode pill (LIVE / REPLAY), the label and the honesty caption, for a standalone view. */
function buildHud(el, full, marginBottom) {
  const root = document.createElement('div');
  root.className = 'lr-hud' + (full ? ' full' : ' compact');
  root.innerHTML = `
    <div class="lr-tl">
      ${full ? '<div class="lr-row"><span class="lr-pill"><i></i><b data-k="mode">STANDBY</b></span></div><div class="lr-label" data-k="label"></div>' : ''}
      <span class="lr-tgt" data-k="tgt"></span>
      <div class="lr-sub" data-k="sub"></div>
    </div>
    <div class="lr-log" data-k="log" aria-live="polite"></div>
    <div class="lr-hint" data-k="hint">drag to orbit</div>
    <div class="lr-toast" data-k="toast"></div>
    ${full ? `<div class="lr-cap" data-k="cap"><span class="l">${CAPTION}</span><span class="s">${CAPTION_SHORT}</span></div>` : ''}`;
  el.appendChild(root);
  if (Number.isFinite(marginBottom)) root.style.setProperty('--lr-log-b', `${Math.max(8, marginBottom)}px`);
  const q = k => root.querySelector(`[data-k="${k}"]`);
  const E = {mode: q('mode'), tgt: q('tgt'), label: q('label'), sub: q('sub'), hint: q('hint'), toast: q('toast'), cap: q('cap'),
    log: q('log')};
  let mTop = 70, mBot = Number.isFinite(marginBottom) ? marginBottom : (full ? 34 : 44), toastT = 0, st = null, episode = null;
  const set = (e, s) => { if (e && e.textContent !== s) e.textContent = s; };
  const setH = (e, s) => { if (e && e.innerHTML !== s) e.innerHTML = s; };
  const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const tl = root.querySelector('.lr-tl');
  return {
    root,
    top: () => mTop, bottom: () => mBot,
    measure() {
      const r = root.getBoundingClientRect(), b = tl.getBoundingClientRect();
      if (r.height > 0) mTop = Math.max(0, b.bottom - r.top + 8);
      // the timeline log may overlay the floor: it does not push the framing up
      let bot = Number.isFinite(marginBottom) ? marginBottom : (full ? 34 : 44);
      if (E.cap && r.height > 0) bot = Math.max(0, r.bottom - E.cap.getBoundingClientRect().top + 6);
      mBot = bot;
    },
    // the recorded session's timeline: the entry in effect now, with the one before it (dimmer) on wide views
    event(e, all, k) {
      if (!E.log) return;
      const rows = [];
      for (let j = Math.max(0, k - 1); j < k; j++) if (all[j]) rows.push(all[j]);
      rows.push(e);
      E.log.innerHTML = rows.map((x, j) => `<p class="${j === rows.length - 1 ? 'now' : 'old'}${/^tx_mined$/.test(x.kind) ? ' gold' : ''}">` +
        `<b>${esc(KIND[x.kind] || x.kind.toUpperCase().replace(/_/g, ' '))}</b><span>${esc(x.text)}</span></p>`).join('');
      this.measure();
    },
    clearEvents() { if (E.log && E.log.childElementCount) { E.log.innerHTML = ''; this.measure(); } },
    setStatus(s, mode) {
      st = s;
      set(E.mode, s.test ? 'TEST' : s.live ? 'LIVE' : mode === 'replay' ? 'REPLAY' : 'STANDBY');
      if (s.live || s.test) set(E.label, (s.label || '').replace(/^(LIVE|TEST)\s*·\s*/, '') + (s.task ? ` · task: ${s.task}` : ''));
      else set(E.label, (s.label || '').replace(/^(REPLAY|STANDBY)\s*·\s*/, ''));
      if (!s.live && !s.test) episode = null;
      this.measure();
    },
    setMetric() { /* the page's readout shows the metrics */ },
    setEpisode(m) { episode = m; },
    toast(msg) {
      set(E.toast, msg); E.toast.classList.add('on');
      clearTimeout(toastT); toastT = setTimeout(() => E.toast.classList.remove('on'), 3200);
    },
    hint(on) { E.hint.style.opacity = on ? '' : '0'; },
    pulse() { /* reserved */ },
    update(f, mode, target, replay, presses, RP, LV) {
      const t = Number.isFinite(f[1]) ? f[1].toFixed(1) : '–';
      if (mode !== 'replay') this.clearEvents();
      if (mode === 'live') {
        const steps = Number.isFinite(LV.steps) ? ` (${fmtSteps(LV.steps)})` : '';
        if (st && st.test) setH(E.sub, `<em>test stream, not live training</em> · a saved checkpoint playing in its own simulation${esc(steps)}`);
        else setH(E.sub, `<em>the</em> ${LIVE_NOTE}${esc(steps)}`);
        let chip = `<b>episode ${Number.isFinite(f[2]) ? f[2] | 0 : '–'}</b> t ${t} s`;
        if (episode) chip += ` · last: ${episode.presses | 0} press${(episode.presses | 0) === 1 ? '' : 'es'}` +
          (Number.isFinite(episode.hits) && (episode.hits || episode.misses) ? `, ${episode.hits | 0} hit${(episode.hits | 0) === 1 ? '' : 's'}, ${episode.misses | 0} miss${(episode.misses | 0) === 1 ? '' : 'es'}` : '') +
          (episode.fell ? ', fell' : '');
        setH(E.tgt, chip);
        E.tgt.classList.remove('lit', 'hit');
      } else if (mode === 'replay' && replay) {
        const n = replay.targets.length, tc = RP.tc;
        setH(E.sub, `<em>recorded, not live</em> · <span class="lr-long">session time </span>${tc.toFixed(1)} s` +
          (RP.speed > 1.5 ? ` · <b>▸▸ ×${Math.round(RP.speed)}</b><em class="lr-long"> fast-forward while the rat waits</em>` : '') +
          (st && st.waiting ? ' · <em>training stream connecting…</em>' : ''));
        if (n && target.doneN >= n && !target.lit) {
          setH(E.tgt, `✓ <b>${n}/${n}</b> targets clicked by the rat`);
          E.tgt.classList.remove('lit'); E.tgt.classList.add('hit');
        } else if (target.idx >= 0 && replay.targets[target.idx]) {
          const T = replay.targets[target.idx];
          setH(E.tgt, `${target.lit ? 'target' : '✓'} <b>${target.idx + 1}/${n}</b> ${esc(T.label)}`);
          E.tgt.classList.toggle('lit', target.lit);
          E.tgt.classList.toggle('hit', !target.lit);
        } else if (n) { setH(E.tgt, `<b>${n}</b> targets, one at a time`); E.tgt.classList.remove('lit', 'hit'); }
        else set(E.tgt, '');
      } else {
        set(E.sub, st && st.waiting ? 'a training stream is connecting…' : 'no training run is streaming right now');
        set(E.tgt, '');
      }
      void presses;
    },
  };
}

/* ================================================================ the rat (ported from live/web/rig.html) */
function buildRat(THREE, R, scene, shellsN) {
  const NV = R.n_verts, NB = R.n_bones;
  const rest = Float32Array.from(R.verts);
  const faces = (NV < 65536 ? Uint16Array : Uint32Array).from(R.faces);
  const weld = Int32Array.from(R.weld || [...Array(NV).keys()]);
  const wsum = new Float64Array(NV);
  const infl = R.weights.map(flat => {
    const n = flat.length / 2;
    const ids = Int32Array.from(flat.slice(0, n)), w = Float32Array.from(flat.slice(n));
    for (let k = 0; k < n; k++) wsum[ids[k]] += w[k];
    return {ids, w};
  });
  for (const b of infl) for (let k = 0; k < b.ids.length; k++) b.w[k] /= wsum[b.ids[k]];
  const bindPos = R.bind_pos.map(p => Float64Array.from(p));
  const bindRT = R.bind_quat.map(q => { const m = new Float64Array(9); q2m(q[0], q[1], q[2], q[3], m);
    return Float64Array.of(m[0], m[3], m[6], m[1], m[4], m[7], m[2], m[5], m[8]); });

  const geo = new THREE.BufferGeometry();
  const pos = new Float32Array(rest), nrm = Float32Array.from(R.rest_normals), comb = new Float32Array(NV * 3);
  const aPos = new THREE.BufferAttribute(pos, 3).setUsage(THREE.DynamicDrawUsage);
  const aNrm = new THREE.BufferAttribute(nrm, 3).setUsage(THREE.DynamicDrawUsage);
  const aComb = new THREE.BufferAttribute(comb, 3).setUsage(THREE.DynamicDrawUsage);
  geo.setAttribute('position', aPos);
  geo.setAttribute('normal', aNrm);
  geo.setAttribute('comb', aComb);
  geo.setAttribute('restPos', new THREE.BufferAttribute(rest, 3));
  geo.setAttribute('color', new THREE.BufferAttribute(Float32Array.from(R.coat), 3));
  const furA = Float32Array.from(R.fur);
  const face = R.face || {skull_bone: R.bones.indexOf('skull'), eye_offset: [0.0011, 0.0128, 0.0025], eye_radius: 0.0034};
  const sk = face.skull_bone;
  {   // thin the fur on the ears (a rat's ears are nearly bare)
    const flat = R.weights[sk], n = flat.length / 2;
    const Rs = new Float64Array(9); q2m(...R.bind_quat[sk], Rs); const p0 = R.bind_pos[sk];
    for (let k = 0; k < n; k++) {
      const i = flat[k]; if (flat[n + k] / wsum[i] < 0.5) continue;
      const d = [rest[i * 3] - p0[0], rest[i * 3 + 1] - p0[1], rest[i * 3 + 2] - p0[2]];
      const lx = Rs[0] * d[0] + Rs[3] * d[1] + Rs[6] * d[2], lz = Rs[2] * d[0] + Rs[5] * d[1] + Rs[8] * d[2];
      const ear = Math.min(1, Math.max(0, (lz - 0.0105) / 0.0045)) * Math.min(1, Math.max(0, (-lx - 0.003) / 0.004));
      furA[i] *= 1 - 0.8 * ear;
    }
  }
  geo.setAttribute('fur', new THREE.BufferAttribute(furA, 1));
  geo.setAttribute('hood', new THREE.BufferAttribute(Float32Array.from(R.hood || new Array(NV).fill(0)), 1));
  geo.setIndex(new THREE.BufferAttribute(faces, 1));
  geo.boundingSphere = new THREE.Sphere(new THREE.Vector3(0, 0, 0.05), 0.6);

  const U = {
    uNL: {value: shellsN}, uLen: {value: 0.0040}, uComb: {value: 1.15}, uGrav: {value: 0.22},
    uDen: {value: new THREE.Vector3(1100, 1750, 1750)},
    uEyeL: {value: new THREE.Vector3(9, 9, 9)}, uEyeR: {value: new THREE.Vector3(9, 9, 9)},
  };
  const skin = new THREE.MeshPhysicalMaterial({vertexColors: true, roughness: 0.8, metalness: 0,
    sheen: 0.6, sheenRoughness: 0.5, sheenColor: new THREE.Color(0xffffff), envMapIntensity: 0.3});
  skin.onBeforeCompile = sh => {
    sh.vertexShader = sh.vertexShader
      .replace('#include <common>', '#include <common>\nattribute float fur; attribute float hood; varying float vFur; varying float vHood;')
      .replace('#include <begin_vertex>', '#include <begin_vertex>\nvFur = fur; vHood = hood;');
    sh.fragmentShader = sh.fragmentShader
      .replace('#include <common>', '#include <common>\nvarying float vFur; varying float vHood;')
      .replace('#include <color_fragment>', '#include <color_fragment>\ndiffuseColor.rgb *= mix(1.0, 0.42, smoothstep(0.05, 0.5, vFur));')
      .replace('#include <roughnessmap_fragment>', '#include <roughnessmap_fragment>\nroughnessFactor = mix(0.46, 0.9, smoothstep(0.0, 0.4, vFur));')
      .replace('#include <lights_physical_fragment>', '#include <lights_physical_fragment>\nmaterial.sheenColor *= (0.15 + 0.85 * smoothstep(0.05, 0.5, vFur)) * mix(1.0, 0.2, vHood);');
  };
  const fur = new THREE.MeshPhysicalMaterial({vertexColors: true, roughness: 0.82, metalness: 0,
    sheen: 1.0, sheenRoughness: 0.42, sheenColor: new THREE.Color(0xf4f0ea), envMapIntensity: 0.3, alphaToCoverage: true});
  fur.onBeforeCompile = sh => {
    Object.assign(sh.uniforms, U);
    sh.vertexShader = sh.vertexShader
      .replace('#include <common>', `#include <common>
        attribute float fur; attribute float hood; attribute vec3 restPos; attribute vec3 comb;
        uniform float uNL; uniform float uLen; uniform float uComb; uniform float uGrav; uniform vec3 uEyeL; uniform vec3 uEyeR;
        varying vec3 vRest; varying float vFur; varying float vLay; varying float vHood;`)
      .replace('#include <begin_vertex>', `#include <begin_vertex>
        float lay = (float(gl_InstanceID) + 1.0) / uNL;
        float eye = min(distance(restPos, uEyeL), distance(restPos, uEyeR));
        float fa = fur * smoothstep(0.0035, 0.0072, eye);
        vRest = restPos; vFur = fa; vLay = lay; vHood = hood;
        vec3 nn = normalize(objectNormal);
        vec3 cb = comb - nn * dot(nn, comb);
        float h = uLen * mix(1.0, 0.72, hood) * fa * lay;
        transformed += nn * h * (1.0 - 0.3 * lay) + (cb * uComb + vec3(0.0, 0.0, -uGrav)) * h * lay;`);
    sh.fragmentShader = sh.fragmentShader
      .replace('#include <common>', `#include <common>
        uniform vec3 uDen; varying vec3 vRest; varying float vFur; varying float vLay; varying float vHood;
        ${FUR_GLSL}`)
      .replace('#include <color_fragment>', `#include <color_fragment>
        if (vFur < 0.05) discard;
        float furA = furCover(vRest, vLay / clamp(vFur * 1.4, 0.3, 1.0));
        if (furA < 0.01) discard;
        float ao = mix(0.34, 1.0, pow(vLay, 0.75));
        diffuseColor.rgb *= ao * mix(1.0, 0.62, vHood);
        diffuseColor.rgb += vec3(0.022, 0.016, 0.012) * vHood * vLay * vLay;`)
      .replace('#include <lights_physical_fragment>', '#include <lights_physical_fragment>\nmaterial.sheenColor *= mix(1.0, 0.08, vHood);')
      .replace('#include <opaque_fragment>', '#include <opaque_fragment>\ngl_FragColor.a = furA;');
  };
  fur.customProgramCacheKey = () => 'labrat-live-fur-v1';
  skin.customProgramCacheKey = () => 'labrat-live-skin-v1';

  const base = new THREE.Mesh(geo, skin);
  base.castShadow = true; base.receiveShadow = true; base.frustumCulled = false; base.renderOrder = 1;
  const shells = new THREE.InstancedMesh(geo, fur, 24);
  shells.count = shellsN;
  shells.receiveShadow = true; shells.castShadow = false; shells.frustumCulled = false; shells.renderOrder = 2;
  scene.add(base, shells);

  const eyeMat = new THREE.MeshPhysicalMaterial({color: 0x030102, roughness: 0.06, metalness: 0, clearcoat: 1,
    clearcoatRoughness: 0.03, envMapIntensity: 1.4});
  const eyeGeo = new THREE.SphereGeometry(face.eye_radius || 0.0034, 24, 14);
  const eyes = [1, -1].map(() => { const e = new THREE.Mesh(eyeGeo, eyeMat); scene.add(e); return e; });
  const eo = face.eye_offset;
  const RbS = new Float64Array(9); q2m(...R.bind_quat[sk], RbS);
  const eyeRest = [1, -1].map(s => { const o = [eo[0], s * eo[1], eo[2]];
    return new THREE.Vector3(bindPos[sk][0] + RbS[0] * o[0] + RbS[1] * o[1] + RbS[2] * o[2],
                             bindPos[sk][1] + RbS[3] * o[0] + RbS[4] * o[1] + RbS[5] * o[2],
                             bindPos[sk][2] + RbS[6] * o[0] + RbS[7] * o[1] + RbS[8] * o[2]); });
  U.uEyeL.value.copy(eyeRest[0]); U.uEyeR.value.copy(eyeRest[1]);

  const wLocal = buildWhiskers(R, rest, face, bindPos[sk], RbS);
  const wPos = new Float32Array(wLocal.length);
  const wGeo = new THREE.BufferGeometry();
  wGeo.setAttribute('position', new THREE.BufferAttribute(wPos, 3).setUsage(THREE.DynamicDrawUsage));
  const nW = wLocal.length / 3, wCol = new Float32Array(nW * 4);
  for (let i = 0; i < nW; i++) {
    const t = (Math.floor((i % 24) / 2) + (i % 2)) / 12;
    wCol.set([0.93, 0.9, 0.85, 0.55 * (1 - t) ** 1.3 + 0.04], i * 4);
  }
  wGeo.setAttribute('color', new THREE.BufferAttribute(wCol, 4));
  const wMat = new THREE.LineBasicMaterial({vertexColors: true, transparent: true, depthWrite: false});
  const whiskers = new THREE.LineSegments(wGeo, wMat);
  whiskers.frustumCulled = false; whiskers.renderOrder = 3; scene.add(whiskers);

  const P = new Float32Array(NB * 3), Qt = new Float32Array(NB * 4), acc = new Float64Array(NV * 3);
  const _Rb = new Float64Array(9), _M = new Float64Array(9);
  const COMB_REST = (() => { const v = [-1, 0, -0.35], l = Math.hypot(...v); return v.map(x => x / l); })();
  /* v' = sum_b w_b (R_b R_bind_b^T (v - p_bind_b) + x_b), weights normalised per vertex; normals area-weighted
     through the weld map (seam duplicates share one normal, so the shells don't crack) */
  function applyPose() {
    pos.fill(0); comb.fill(0);
    for (let b = 0; b < NB; b++) {
      q2m(Qt[b * 4], Qt[b * 4 + 1], Qt[b * 4 + 2], Qt[b * 4 + 3], _Rb);
      const Tm = bindRT[b], Rb = _Rb, m = _M;
      for (let r = 0; r < 3; r++) for (let c = 0; c < 3; c++)
        m[r * 3 + c] = Rb[r * 3] * Tm[c] + Rb[r * 3 + 1] * Tm[3 + c] + Rb[r * 3 + 2] * Tm[6 + c];
      const bp = bindPos[b];
      const tx = P[b * 3] - (m[0] * bp[0] + m[1] * bp[1] + m[2] * bp[2]);
      const ty = P[b * 3 + 1] - (m[3] * bp[0] + m[4] * bp[1] + m[5] * bp[2]);
      const tz = P[b * 3 + 2] - (m[6] * bp[0] + m[7] * bp[1] + m[8] * bp[2]);
      const cx = m[0] * COMB_REST[0] + m[1] * COMB_REST[1] + m[2] * COMB_REST[2];
      const cy = m[3] * COMB_REST[0] + m[4] * COMB_REST[1] + m[5] * COMB_REST[2];
      const cz = m[6] * COMB_REST[0] + m[7] * COMB_REST[1] + m[8] * COMB_REST[2];
      const ids = infl[b].ids, ws = infl[b].w, n = ids.length;
      const m0 = m[0], m1 = m[1], m2 = m[2], m3 = m[3], m4 = m[4], m5 = m[5], m6 = m[6], m7 = m[7], m8 = m[8];
      for (let k = 0; k < n; k++) {
        const i3 = ids[k] * 3, w = ws[k], x = rest[i3], y = rest[i3 + 1], z = rest[i3 + 2];
        pos[i3] += w * (m0 * x + m1 * y + m2 * z + tx);
        pos[i3 + 1] += w * (m3 * x + m4 * y + m5 * z + ty);
        pos[i3 + 2] += w * (m6 * x + m7 * y + m8 * z + tz);
        comb[i3] += w * cx; comb[i3 + 1] += w * cy; comb[i3 + 2] += w * cz;
      }
    }
    acc.fill(0);
    for (let f = 0; f < faces.length; f += 3) {
      const a = faces[f] * 3, b = faces[f + 1] * 3, c = faces[f + 2] * 3;
      const e1x = pos[b] - pos[a], e1y = pos[b + 1] - pos[a + 1], e1z = pos[b + 2] - pos[a + 2];
      const e2x = pos[c] - pos[a], e2y = pos[c + 1] - pos[a + 1], e2z = pos[c + 2] - pos[a + 2];
      const nx = e1y * e2z - e1z * e2y, ny = e1z * e2x - e1x * e2z, nz = e1x * e2y - e1y * e2x;
      const va = weld[faces[f]] * 3, vb = weld[faces[f + 1]] * 3, vc = weld[faces[f + 2]] * 3;
      acc[va] += nx; acc[va + 1] += ny; acc[va + 2] += nz;
      acc[vb] += nx; acc[vb + 1] += ny; acc[vb + 2] += nz;
      acc[vc] += nx; acc[vc + 1] += ny; acc[vc + 2] += nz;
    }
    for (let i = 0; i < NV; i++) {
      const j = weld[i] * 3, x = acc[j], y = acc[j + 1], z = acc[j + 2], l = Math.hypot(x, y, z);
      if (l > 1e-24) { nrm[i * 3] = x / l; nrm[i * 3 + 1] = y / l; nrm[i * 3 + 2] = z / l; }
    }
    aPos.needsUpdate = true; aNrm.needsUpdate = true; aComb.needsUpdate = true;
    const px = P[sk * 3], py = P[sk * 3 + 1], pz = P[sk * 3 + 2];
    q2m(Qt[sk * 4], Qt[sk * 4 + 1], Qt[sk * 4 + 2], Qt[sk * 4 + 3], _Rb);
    [1, -1].forEach((sg, k) => {
      const lx = eo[0], ly = sg * eo[1], lz = eo[2];
      eyes[k].position.set(px + _Rb[0] * lx + _Rb[1] * ly + _Rb[2] * lz, py + _Rb[3] * lx + _Rb[4] * ly + _Rb[5] * lz,
                           pz + _Rb[6] * lx + _Rb[7] * ly + _Rb[8] * lz);
    });
    for (let i = 0; i < wLocal.length; i += 3) {
      const x = wLocal[i], y = wLocal[i + 1], z = wLocal[i + 2];
      wPos[i] = px + _Rb[0] * x + _Rb[1] * y + _Rb[2] * z;
      wPos[i + 1] = py + _Rb[3] * x + _Rb[4] * y + _Rb[5] * z;
      wPos[i + 2] = pz + _Rb[6] * x + _Rb[7] * y + _Rb[8] * z;
    }
    wGeo.attributes.position.needsUpdate = true;
  }
  return {
    P, Qt, NB, applyPose,
    setShells(n) { shells.count = Math.min(24, n); U.uNL.value = shells.count; },
    dispose() { geo.dispose(); skin.dispose(); fur.dispose(); eyeGeo.dispose(); eyeMat.dispose(); wGeo.dispose(); wMat.dispose(); },
  };
}

function buildWhiskers(R, rest, face, skP, RbS) {     // rig.html buildWhiskers (film.py layout), in skull-local frame
  let s = 3 >>> 0;
  const rnd = () => { s = (s + 0x6D2B79F5) >>> 0; let t = s; t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  const nv = face.nose_vert;
  const nose = nv !== undefined ? [rest[nv * 3], rest[nv * 3 + 1], rest[nv * 3 + 2]] : [skP[0] + 0.04, skP[1], skP[2]];
  const out = [], SEG = 12;
  for (const side of [1, -1]) for (let i = 0; i < 14; i++) {
    const row = i % 4, col = Math.floor(i / 4);
    const p0 = [nose[0] - 0.008 - col * 0.0035, nose[1] + side * (0.005 + row * 0.0012), nose[2] - 0.003 - row * 0.0018];
    const ln = 0.035 + rnd() * 0.035 - col * 0.004;
    let d = [-0.1 + (rnd() * 0.4 - 0.15), side, -0.15 + row * 0.12 + (rnd() * 0.2 - 0.1)];
    const dl = Math.hypot(...d); d = d.map(v => v / dl);
    const p1 = [p0[0] + d[0] * ln * 0.5 + 0.005, p0[1] + d[1] * ln * 0.5, p0[2] + d[2] * ln * 0.5 + 0.003];
    const p2 = [p0[0] + d[0] * ln - 0.006, p0[1] + d[1] * ln, p0[2] + d[2] * ln - 0.004];
    const c = [0, 1, 2].map(k => 2 * p1[k] - 0.5 * (p0[k] + p2[k]));
    const at = t => [0, 1, 2].map(k => (1 - t) * (1 - t) * p0[k] + 2 * (1 - t) * t * c[k] + t * t * p2[k]);
    for (let k = 0; k < SEG; k++) {
      for (const p of [at(k / SEG), at((k + 1) / SEG)]) {
        const r = [p[0] - skP[0], p[1] - skP[1], p[2] - skP[2]];
        out.push(RbS[0] * r[0] + RbS[3] * r[1] + RbS[6] * r[2], RbS[1] * r[0] + RbS[4] * r[1] + RbS[7] * r[2],
                 RbS[2] * r[0] + RbS[5] * r[1] + RbS[8] * r[2]);
      }
    }
  }
  return Float32Array.from(out);
}
