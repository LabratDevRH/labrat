/* rat3d.js - a rotating 3D DISPLAY MODEL of the simulated rat body (DeepMind's rodent model in MuJoCo).

   It is NOT a live view: the mesh is skinned once, on the CPU, into the standing pose the live rig starts from
   (rat_pose.json, written by live/export_pose.py: the end of the 2 s brain-off pre-roll, seed 2026), then turned
   slowly on a turntable. The look (skin + shell fur, coat colours, eyes, whiskers, studio lighting) is ported from
   rig.html, plus two coloured rim lights from the page's palette (the coat colours are unchanged); there is no
   chamber, no lever, only a faint contact shadow.

     import {mountRat} from '/rat3d.js';
     const handle = await mountRat(document.getElementById('rat3d'), {url: '/rat.json', poseUrl: '/rat_pose.json'});

   mountRat never throws: on any failure (no WebGL, three.js CDN down, a fetch failed) it puts a small muted
   "3D model unavailable" line in the container, console.warns why, and resolves to null. On success it resolves,
   after the first frame, to {dispose()}. window.__ratReady = true after that first frame; window.__ratFrames counts
   rendered frames.

   Performance (the page is screen-recorded): DPR capped at 1.5, at most 30 frames/s, 16 fur shells, paused while
   the document is hidden or the container is off screen; prefers-reduced-motion renders one static 3/4 view.
   three.js is imported dynamically (the page's import map "three", else the same pinned jsDelivr URL), so a CDN
   failure is caught here instead of breaking the page's module graph. */

const THREE_URL = 'https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js';
const TURN_S = 18;                 // one full turn every 18 s
const ELEV = 17 * Math.PI / 180;   // camera elevation above the horizontal
const START = -0.62;               // turntable angle of the first frame / the static view: a 3/4 front view
const FPS = 30;
const DPR_MAX = 1.5;
const SHELLS = 16;
const MARGIN_X = 0.93, MARGIN_Y = 0.88;   // NDC half-extent the rat may use (all turntable angles)
const RIM_MAGENTA = 7.0, RIM_GOLD = 4.5;  // palette rim lights (hot magenta #D60C94, warm gold #F5AC29)
const LABEL = 'Display model of the simulated rat body (DeepMind rodent model in MuJoCo), shown in the standing ' +
  'pose the rig starts from and turned on a turntable. Not a live view of its pose.';

export async function mountRat(container, opts = {}) {
  let state = null;
  try {
    if (!container || !container.appendChild) throw new Error('no container element');
    state = {container, disposed: false, cleanup: []};
    return await mount(container, opts, state);
  } catch (e) {
    console.warn('rat3d: 3D model unavailable -', e && e.message ? e.message : e);
    try { if (state) teardown(state); } catch (_) { /* ignore */ }
    try { if (container && container.appendChild) showFallback(container); } catch (_) { /* ignore */ }
    return null;
  }
}

function showFallback(container) {
  const old = container.querySelector(':scope > .rat3d-canvas');
  if (old) old.remove();
  if (container.querySelector(':scope > .rat3d-fallback')) return;
  const d = document.createElement('div');
  d.className = 'rat3d-fallback';
  d.textContent = '3D model unavailable';
  d.style.cssText = 'position:absolute;inset:0;display:flex;align-items:center;justify-content:center;' +
    'color:#ae8ee4;font:11px/1.4 "JetBrains Mono",ui-monospace,monospace;letter-spacing:.06em;pointer-events:none';
  if (getComputedStyle(container).position === 'static') container.style.position = 'relative';
  container.appendChild(d);
}

function teardown(st) {
  st.disposed = true;
  for (const f of st.cleanup.splice(0)) { try { f(); } catch (_) { /* ignore */ } }
}

async function loadThree() {
  try { return await import('three'); }
  catch (e) { return await import(THREE_URL); }       // no import map on the page: same pinned build
}

async function getJSON(url) {
  const r = await fetch(url, {cache: 'no-cache'});
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}

function q2m(w, x, y, z, o) {
  const n = 1 / Math.hypot(w, x, y, z); w *= n; x *= n; y *= n; z *= n;
  o[0] = 1 - 2 * (y * y + z * z); o[1] = 2 * (x * y - w * z); o[2] = 2 * (x * z + w * y);
  o[3] = 2 * (x * y + w * z); o[4] = 1 - 2 * (x * x + z * z); o[5] = 2 * (y * z - w * x);
  o[6] = 2 * (x * z - w * y); o[7] = 2 * (y * z + w * x); o[8] = 1 - 2 * (x * x + y * y);
}

/* ================================================================ CPU skinning (rig.html buildRat + applyPose)
   v' = sum_b w_b (R_b R_bind_b^T (v - p_bind_b) + x_b), weights normalised per vertex; normals area-weighted
   through the weld map (seam duplicates share one normal, so the fur shells don't crack) */
function skinRat(R, pose) {
  const NV = R.n_verts, NB = R.n_bones;
  if (!pose || !Array.isArray(pose.xpos) || pose.xpos.length !== NB || pose.xquat.length !== NB)
    throw new Error('rat_pose.json does not match rat.json (bone count)');
  if (Array.isArray(pose.bones) && pose.bones.join(',') !== R.bones.join(','))
    throw new Error('rat_pose.json bone order differs from rat.json');
  const rest = Float32Array.from(R.verts);
  const faces = Uint32Array.from(R.faces);
  const weld = Int32Array.from(R.weld || [...Array(NV).keys()]);
  const wsum = new Float64Array(NV);
  const infl = R.weights.map(flat => {
    const n = flat.length / 2;
    const ids = Int32Array.from(flat.slice(0, n)), w = Float64Array.from(flat.slice(n));
    for (let k = 0; k < n; k++) wsum[ids[k]] += w[k];
    return {ids, w};
  });
  for (const b of infl) for (let k = 0; k < b.ids.length; k++) b.w[k] /= wsum[b.ids[k]];

  const pos = new Float64Array(NV * 3), comb = new Float64Array(NV * 3);
  const COMB = (() => { const v = [-1, 0, -0.35], l = Math.hypot(...v); return v.map(x => x / l); })();
  const Rb = new Float64Array(9), Rbind = new Float64Array(9), m = new Float64Array(9);
  for (let b = 0; b < NB; b++) {
    q2m(...pose.xquat[b], Rb);
    q2m(...R.bind_quat[b], Rbind);
    for (let r = 0; r < 3; r++) for (let c = 0; c < 3; c++)       // m = R_b R_bind^T
      m[r * 3 + c] = Rb[r * 3] * Rbind[c * 3] + Rb[r * 3 + 1] * Rbind[c * 3 + 1] + Rb[r * 3 + 2] * Rbind[c * 3 + 2];
    const bp = R.bind_pos[b], P = pose.xpos[b];
    const tx = P[0] - (m[0] * bp[0] + m[1] * bp[1] + m[2] * bp[2]);
    const ty = P[1] - (m[3] * bp[0] + m[4] * bp[1] + m[5] * bp[2]);
    const tz = P[2] - (m[6] * bp[0] + m[7] * bp[1] + m[8] * bp[2]);
    const cx = m[0] * COMB[0] + m[1] * COMB[1] + m[2] * COMB[2];
    const cy = m[3] * COMB[0] + m[4] * COMB[1] + m[5] * COMB[2];
    const cz = m[6] * COMB[0] + m[7] * COMB[1] + m[8] * COMB[2];
    const {ids, w} = infl[b];
    for (let k = 0; k < ids.length; k++) {
      const i3 = ids[k] * 3, wk = w[k], x = rest[i3], y = rest[i3 + 1], z = rest[i3 + 2];
      pos[i3] += wk * (m[0] * x + m[1] * y + m[2] * z + tx);
      pos[i3 + 1] += wk * (m[3] * x + m[4] * y + m[5] * z + ty);
      pos[i3 + 2] += wk * (m[6] * x + m[7] * y + m[8] * z + tz);
      comb[i3] += wk * cx; comb[i3 + 1] += wk * cy; comb[i3 + 2] += wk * cz;
    }
  }
  const acc = new Float64Array(NV * 3);
  for (let f = 0; f < faces.length; f += 3) {
    const a = faces[f] * 3, b = faces[f + 1] * 3, c = faces[f + 2] * 3;
    const e1x = pos[b] - pos[a], e1y = pos[b + 1] - pos[a + 1], e1z = pos[b + 2] - pos[a + 2];
    const e2x = pos[c] - pos[a], e2y = pos[c + 1] - pos[a + 1], e2z = pos[c + 2] - pos[a + 2];
    const nx = e1y * e2z - e1z * e2y, ny = e1z * e2x - e1x * e2z, nz = e1x * e2y - e1y * e2x;
    for (const v of [weld[faces[f]] * 3, weld[faces[f + 1]] * 3, weld[faces[f + 2]] * 3]) {
      acc[v] += nx; acc[v + 1] += ny; acc[v + 2] += nz;
    }
  }
  const nrm = Float32Array.from(R.rest_normals);
  for (let i = 0; i < NV; i++) {
    const j = weld[i] * 3, x = acc[j], y = acc[j + 1], z = acc[j + 2], l = Math.hypot(x, y, z);
    if (l > 1e-24) { nrm[i * 3] = x / l; nrm[i * 3 + 1] = y / l; nrm[i * 3 + 2] = z / l; }
  }
  // fur amount from rat.json, thinned on the ears (as rig.html): skull-carried verts high and behind the eyes
  const furA = Float32Array.from(R.fur);
  const face = R.face || {skull_bone: R.bones.indexOf('skull'), eye_offset: [0.0011, 0.0128, 0.0025], eye_radius: 0.0034};
  const sk = face.skull_bone;
  {
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
  // eyes (rest = bind frame, for the fur shader's eye clearing; posed = skull pose) and whiskers (skull frame)
  const RbS = new Float64Array(9); q2m(...R.bind_quat[sk], RbS);
  const bS = R.bind_pos[sk], eo = face.eye_offset;
  const RpS = new Float64Array(9); q2m(...pose.xquat[sk], RpS);
  const pS = pose.xpos[sk];
  const xf = (Rm, p, o) => [p[0] + Rm[0] * o[0] + Rm[1] * o[1] + Rm[2] * o[2],
                            p[1] + Rm[3] * o[0] + Rm[4] * o[1] + Rm[5] * o[2],
                            p[2] + Rm[6] * o[0] + Rm[7] * o[1] + Rm[8] * o[2]];
  const eyeRest = [1, -1].map(s => xf(RbS, bS, [eo[0], s * eo[1], eo[2]]));
  const eyePos = [1, -1].map(s => xf(RpS, pS, [eo[0], s * eo[1], eo[2]]));
  const wLocal = buildWhiskers(rest, face, bS, RbS);
  const whisk = new Float32Array(wLocal.length);
  for (let i = 0; i < wLocal.length; i += 3) whisk.set(xf(RpS, pS, [wLocal[i], wLocal[i + 1], wLocal[i + 2]]), i);
  return {NV, rest, faces, pos: Float32Array.from(pos), nrm, comb: Float32Array.from(comb), furA,
    hood: Float32Array.from(R.hood || new Array(NV).fill(0)), coat: Float32Array.from(R.coat),
    eyeRest, eyePos, eyeR: face.eye_radius || 0.0034, whisk};
}

function buildWhiskers(rest, face, skP, RbS) {     // rig.html buildWhiskers (film.py layout), in skull-local frame
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

/* ================================================================ materials (rig.html makeRatMaterials) */
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

function makeMaterials(THREE, U) {
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
  fur.customProgramCacheKey = () => 'rat3d-fur-v1';
  skin.customProgramCacheKey = () => 'rat3d-skin-v1';
  const eye = new THREE.MeshPhysicalMaterial({color: 0x030102, roughness: 0.06, metalness: 0, clearcoat: 1,
    clearcoatRoughness: 0.03, envMapIntensity: 1.4});
  return {skin, fur, eye};
}

/* reflections: rig.html studioEnv - a dark studio with a few softboxes */
function studioEnv(THREE, renderer) {
  const pmrem = new THREE.PMREMGenerator(renderer);
  const s = new THREE.Scene();
  const disposables = [];
  const add = (geo, mat, setup) => { const m = new THREE.Mesh(geo, mat); disposables.push(geo, mat); if (setup) setup(m); s.add(m); };
  add(new THREE.BoxGeometry(12, 12, 12), new THREE.MeshBasicMaterial({color: 0x0b0b0e, side: THREE.BackSide}));
  const panel = (w, h, p, rgb, k) => add(new THREE.PlaneGeometry(w, h),
    new THREE.MeshBasicMaterial({color: new THREE.Color(rgb).multiplyScalar(k), side: THREE.DoubleSide}),
    m => { m.position.set(...p); m.lookAt(0, 0, 0); });
  panel(5, 3, [0.3, 0, 5.8], 0xfff1e0, 4.0);        // house softbox overhead
  panel(1.0, 5, [-5.8, 1.5, 1.2], 0xc4d4ff, 3.0);    // cool strip behind
  panel(3.5, 2, [2.5, -5.8, 1.6], 0xffe4c8, 1.6);   // warm key, camera side
  panel(2, 1.2, [5.8, 2.5, 0.6], 0xffffff, 0.8);    // small kicker
  const rt = pmrem.fromScene(s, 0.03);
  for (const d of disposables) d.dispose();
  pmrem.dispose();
  return rt;
}

/* a soft contact shadow, baked once from the posed mesh: vertices near the floor splat a blurred dark footprint */
function contactShadow(THREE, pos, NV) {
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (let i = 0; i < NV; i++) {
    const x = pos[i * 3], y = pos[i * 3 + 1];
    if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y;
  }
  const pad = 0.035;
  x0 -= pad; x1 += pad; y0 -= pad; y1 += pad;
  const res = Math.max(x1 - x0, y1 - y0) / 160;
  const gw = Math.ceil((x1 - x0) / res), gh = Math.ceil((y1 - y0) / res);
  const acc = new Float32Array(gw * gh);
  const splat = (x, y, r, w) => {
    const rr = r / res, gx = (x - x0) / res, gy = (y - y0) / res, R = Math.ceil(rr * 2.2);
    const cx = Math.round(gx), cy = Math.round(gy), inv = 1 / (2 * rr * rr);
    for (let j = Math.max(0, cy - R); j <= Math.min(gh - 1, cy + R); j++)
      for (let i = Math.max(0, cx - R); i <= Math.min(gw - 1, cx + R); i++) {
        const d2 = (i - gx) ** 2 + (j - gy) ** 2;
        acc[j * gw + i] += w * Math.exp(-d2 * inv);
      }
  };
  for (let i = 0; i < NV; i++) {
    const h = pos[i * 3 + 2];
    if (h < 0.028) splat(pos[i * 3], pos[i * 3 + 1], 0.0035 + 0.35 * h, Math.exp(-h / 0.006));   // contact: feet, tail
    if (i % 6 === 0 && h < 0.07)
      splat(pos[i * 3], pos[i * 3 + 1], 0.012 + 0.3 * h, 0.48 * Math.exp(-h / 0.03));          // broad occlusion under the body
  }
  const sorted = Float32Array.from(acc.filter(v => v > 1e-4)).sort();
  const k = sorted.length ? sorted[Math.floor(sorted.length * 0.9)] : 1;
  const data = new Uint8Array(gw * gh * 4);
  for (let i = 0; i < gw * gh; i++) {
    const a = 0.62 * (1 - Math.exp(-acc[i] / (0.8 * k)));
    data[i * 4 + 3] = Math.round(255 * Math.min(1, a));
  }
  const tex = new THREE.DataTexture(data, gw, gh, THREE.RGBAFormat);
  tex.magFilter = THREE.LinearFilter; tex.minFilter = THREE.LinearFilter; tex.needsUpdate = true;
  const geo = new THREE.PlaneGeometry(gw * res, gh * res);
  const mat = new THREE.MeshBasicMaterial({map: tex, transparent: true, depthWrite: false, toneMapped: false});
  const mesh = new THREE.Mesh(geo, mat);
  mesh.position.set(x0 + gw * res / 2, y0 + gh * res / 2, 0.0002);
  mesh.renderOrder = 0;
  return mesh;
}

/* smallest enclosing circle (approx., Badoiu-Clarkson) of the footprint: the turntable axis, so the rat (tail
   included) sweeps the smallest disc and can be framed as large as possible */
function turntableCentre(pts, n) {
  let cx = 0, cy = 0;
  for (let i = 0; i < n; i++) { cx += pts[i * 3]; cy += pts[i * 3 + 1]; }
  cx /= n; cy /= n;
  for (let it = 1; it <= 400; it++) {
    let best = -1, bx = 0, by = 0;
    for (let i = 0; i < n; i++) {
      const dx = pts[i * 3] - cx, dy = pts[i * 3 + 1] - cy, d = dx * dx + dy * dy;
      if (d > best) { best = d; bx = pts[i * 3]; by = pts[i * 3 + 1]; }
    }
    cx += (bx - cx) / (it + 1); cy += (by - cy) / (it + 1);
  }
  return [cx, cy];
}

/* ================================================================ mount */
async function mount(container, opts, st) {
  const url = opts.url || '/rat.json', poseUrl = opts.poseUrl || '/rat_pose.json';
  const probe = document.createElement('canvas');
  if (!(probe.getContext('webgl2') || probe.getContext('webgl'))) throw new Error('WebGL is not available');

  const mark = (what, t0) => { if (opts.debug) console.debug(`rat3d: ${what} ${(performance.now() - t0).toFixed(1)} ms`); };
  const t0 = performance.now();
  const [THREE, R, pose] = await Promise.all([loadThree(), getJSON(url), getJSON(poseUrl)]);
  if (st.disposed) return null;
  mark('load', t0);
  if (!R || R.format !== 'ratbrain-rat-1') throw new Error(`${url}: not a ratbrain-rat-1 asset`);
  const ts = performance.now();
  const S = skinRat(R, pose);
  mark('skin', ts);

  // ---- centre: turntable axis through the footprint's enclosing-circle centre, floor at z = 0
  const NV = S.NV;
  let zmin = Infinity;
  for (let i = 0; i < NV; i++) zmin = Math.min(zmin, S.pos[i * 3 + 2]);
  const [cx, cy] = turntableCentre(S.pos, NV);
  const shift = (a) => { for (let i = 0; i < a.length; i += 3) { a[i] -= cx; a[i + 1] -= cy; a[i + 2] -= zmin; } };
  shift(S.pos); shift(S.whisk);
  const eyePos = S.eyePos.map(p => [p[0] - cx, p[1] - cy, p[2] - zmin]);

  // ---- canvas + renderer
  if (getComputedStyle(container).position === 'static') container.style.position = 'relative';
  const canvas = document.createElement('canvas');
  canvas.className = 'rat3d-canvas';
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', opts.label || LABEL);
  canvas.title = opts.label || LABEL;
  canvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;display:block';
  const bg = opts.background || null;           // null: transparent, the panel shows through
  const renderer = new THREE.WebGLRenderer({canvas, antialias: true, alpha: !bg, premultipliedAlpha: true,
    powerPreference: 'default', preserveDrawingBuffer: false});
  st.cleanup.push(() => { renderer.dispose(); canvas.remove(); });
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.AgXToneMapping !== undefined ? THREE.AgXToneMapping : THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.0;
  if (bg) renderer.setClearColor(new THREE.Color(bg), 1); else renderer.setClearColor(0x000000, 0);
  const old = container.querySelector(':scope > .rat3d-fallback');
  if (old) old.remove();
  container.appendChild(canvas);

  const scene = new THREE.Scene();
  const envRT = studioEnv(THREE, renderer);
  scene.environment = envRT.texture;
  st.cleanup.push(() => envRT.dispose());

  // ---- lights: rig.html's rig, fixed to the camera side (the camera looks along +y from -y, +x is screen right).
  // The house, key and fill lights stay neutral so the coat keeps its natural colours; two rim lights from the
  // page's palette (hot magenta back-left, warm gold back-right) seat the rat in the nebula-tinted pane.
  scene.add(new THREE.HemisphereLight(0xa9a0c8, 0x0b0908, 0.35));
  const dl = (col, k, p) => { const l = new THREE.DirectionalLight(col, k); l.position.set(...p); l.target.position.set(0, 0, 0.035);
    scene.add(l, l.target); return l; };
  dl(0xfff0dc, 3.2, [0.03, 0.05, 0.62]);     // house light overhead (a spot in the rig)
  dl(0xffe2c4, 1.7, [0.55, -0.62, 0.52]);    // warm key, front-right of camera
  dl(0xcfd8ff, 0.35, [0.1, -0.8, 0.1]);      // fill
  dl(0xd60c94, RIM_MAGENTA, [-0.8, 0.55, 0.30]);    // rim: hot magenta, back-left
  dl(0xf5ac29, RIM_GOLD, [0.8, 0.55, 0.26]);        // rim: warm gold, back-right (golden yellow turns the dark hood olive)

  // ---- the rat
  const U = {
    uNL: {value: SHELLS}, uLen: {value: 0.0040}, uComb: {value: 1.15}, uGrav: {value: 0.22},
    uDen: {value: new THREE.Vector3(1100, 1750, 1750)},
    uEyeL: {value: new THREE.Vector3(...S.eyeRest[0])}, uEyeR: {value: new THREE.Vector3(...S.eyeRest[1])},
  };
  const mats = makeMaterials(THREE, U);
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(S.pos, 3));
  geo.setAttribute('normal', new THREE.BufferAttribute(S.nrm, 3));
  geo.setAttribute('comb', new THREE.BufferAttribute(S.comb, 3));
  geo.setAttribute('restPos', new THREE.BufferAttribute(S.rest, 3));
  geo.setAttribute('color', new THREE.BufferAttribute(S.coat, 3));
  geo.setAttribute('fur', new THREE.BufferAttribute(S.furA, 1));
  geo.setAttribute('hood', new THREE.BufferAttribute(S.hood, 1));
  geo.setIndex(new THREE.BufferAttribute(S.faces, 1));
  geo.computeBoundingSphere();
  const turntable = new THREE.Group();
  const base = new THREE.Mesh(geo, mats.skin);
  base.frustumCulled = false; base.renderOrder = 1;
  const shells = new THREE.InstancedMesh(geo, mats.fur, SHELLS);
  shells.frustumCulled = false; shells.renderOrder = 2;
  turntable.add(base, shells);
  const eyeGeo = new THREE.SphereGeometry(S.eyeR, 24, 14);
  for (const p of eyePos) { const e = new THREE.Mesh(eyeGeo, mats.eye); e.position.set(...p); turntable.add(e); }
  const wGeo = new THREE.BufferGeometry();
  wGeo.setAttribute('position', new THREE.BufferAttribute(S.whisk, 3));
  const nW = S.whisk.length / 3, wCol = new Float32Array(nW * 4);
  for (let i = 0; i < nW; i++) {             // 24 points per whisker (12 segments x 2 ends): fade root -> tip
    const t = (Math.floor((i % 24) / 2) + (i % 2)) / 12;
    wCol.set([0.93, 0.9, 0.85, 0.62 * (1 - t) ** 1.3 + 0.04], i * 4);
  }
  wGeo.setAttribute('color', new THREE.BufferAttribute(wCol, 4));
  const wMat = new THREE.LineBasicMaterial({vertexColors: true, transparent: true, depthWrite: false});
  const whiskers = new THREE.LineSegments(wGeo, wMat);
  whiskers.frustumCulled = false; whiskers.renderOrder = 3;
  turntable.add(whiskers);
  const tsh = performance.now();
  const shadow = contactShadow(THREE, S.pos, NV);
  turntable.add(shadow);
  mark('contact shadow', tsh);
  scene.add(turntable);

  // on a transparent canvas the fur's alpha-to-coverage samples carry alpha = strand coverage; this last pass sets
  // alpha to 1 on every sample the rat wrote depth to (per MSAA sample), leaving colour untouched, so the canvas
  // stays correctly premultiplied (shadow + whiskers keep their own blended alpha: they write no depth)
  if (!bg) {
    const fixMat = new THREE.ShaderMaterial({
      vertexShader: 'void main(){ gl_Position = vec4(position.xy, 0.99995, 1.0); }',
      fragmentShader: 'void main(){ gl_FragColor = vec4(0.0, 0.0, 0.0, 1.0); }',
      depthTest: true, depthWrite: false, depthFunc: THREE.GreaterDepth, transparent: true, toneMapped: false,
      blending: THREE.CustomBlending, blendEquation: THREE.AddEquation, blendSrc: THREE.ZeroFactor, blendDst: THREE.OneFactor,
      blendEquationAlpha: THREE.AddEquation, blendSrcAlpha: THREE.OneFactor, blendDstAlpha: THREE.ZeroFactor,
    });
    const fix = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), fixMat);
    fix.frustumCulled = false; fix.renderOrder = 999;
    scene.add(fix);
  }
  st.cleanup.push(() => scene.traverse(o => {
    if (o.geometry) o.geometry.dispose();
    if (o.material) { if (o.material.map) o.material.map.dispose(); o.material.dispose(); }
  }));

  // ---- camera + framing: fixed camera, the rat turns; the lens is fitted once per size so that the rat, at
  // every turntable angle, stays inside the margins (no pumping while it turns)
  const fitPts = [];
  for (let i = 0; i < NV; i += 2) fitPts.push(S.pos[i * 3], S.pos[i * 3 + 1], S.pos[i * 3 + 2]);
  for (let i = 0; i < S.whisk.length; i += 12) fitPts.push(S.whisk[i], S.whisk[i + 1], S.whisk[i + 2]);
  let zTop = 0, rMax = 0;
  for (let i = 0; i < fitPts.length; i += 3) {
    zTop = Math.max(zTop, fitPts[i + 2]); rMax = Math.max(rMax, Math.hypot(fitPts[i], fitPts[i + 1]));
  }
  const zc = zTop * 0.45, D = Math.max(0.3, rMax * 3.2);
  const camera = new THREE.PerspectiveCamera(20, 1, Math.max(0.01, D - rMax * 1.6), D + rMax * 1.6 + 0.1);
  camera.up.set(0, 0, 1);
  camera.position.set(0, -D * Math.cos(ELEV), zc + D * Math.sin(ELEV));
  camera.lookAt(0, 0, zc);
  camera.updateMatrixWorld();
  const reduceMQ = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  let still = !!(reduceMQ && reduceMQ.matches) || opts.rotate === false;

  const V = camera.matrixWorldInverse.elements;
  function fit(aspect) {
    const angles = still ? [START] : Array.from({length: 48}, (_, k) => k * Math.PI * 2 / 48);
    let u0 = Infinity, u1 = -Infinity, v0 = Infinity, v1 = -Infinity;
    for (const a of angles) {
      const c = Math.cos(a), s = Math.sin(a);
      for (let i = 0; i < fitPts.length; i += 3) {
        const px = fitPts[i], py = fitPts[i + 1], z = fitPts[i + 2];
        const x = c * px - s * py, y = s * px + c * py;
        const ex = V[0] * x + V[4] * y + V[8] * z + V[12];
        const ey = V[1] * x + V[5] * y + V[9] * z + V[13];
        const ez = V[2] * x + V[6] * y + V[10] * z + V[14];
        const u = ex / -ez, v = ey / -ez;
        if (u < u0) u0 = u; if (u > u1) u1 = u; if (v < v0) v0 = v; if (v > v1) v1 = v;
      }
    }
    const hv = Math.max((v1 - v0) / (2 * MARGIN_Y), (u1 - u0) / (2 * MARGIN_X * aspect)), hu = hv * aspect;
    const uc = (u0 + u1) / 2, vc = (v0 + v1) / 2, n = camera.near;
    camera.projectionMatrix.makePerspective(n * (uc - hu), n * (uc + hu), n * (vc + hv), n * (vc - hv), n, camera.far);
    camera.projectionMatrixInverse.copy(camera.projectionMatrix).invert();
    return hu;
  }
  // a whisker is ~0.1 mm thick but always drawn 1 device pixel wide: fade the whiskers as the rat gets smaller on
  // screen, or at panel size they read as a white smear at the nose
  function whiskerFade(hu, dpr) {
    const pxPerMm = (W * dpr / 2) / (hu * D) / 1000;
    wMat.opacity = Math.min(1, Math.max(0.28, pxPerMm / 2.6));
  }

  // ---- size
  let W = 0, H = 0;
  function resize() {
    const w = Math.max(1, Math.round(container.clientWidth)), h = Math.max(1, Math.round(container.clientHeight));
    if (w === W && h === H) return false;
    W = w; H = h;
    const dpr = Math.min(DPR_MAX, window.devicePixelRatio || 1);
    renderer.setPixelRatio(dpr);
    renderer.setSize(W, H, false);
    whiskerFade(fit(W / H), dpr);
    return true;
  }

  // ---- loop: <= 30 fps, paused while hidden / off screen; reduced motion = one static frame per change
  let angle = START, raf = 0, last = 0, lastT = 0, visible = true, frames = 0;
  let firstResolve;
  const firstFrame = new Promise(r => { firstResolve = r; });
  function draw() {
    turntable.rotation.z = still ? START : angle;
    renderer.render(scene, camera);
    frames++;
    window.__ratFrames = (window.__ratFrames || 0) + 1;
    if (frames === 1) { window.__ratReady = true; firstResolve(); }
  }
  const IV = 1000 / FPS;
  function tick(now) {
    raf = 0;
    if (st.disposed || still || document.hidden || !visible) return;
    raf = requestAnimationFrame(tick);
    if (last && now - last < IV - 2) return;
    // frame slots on a fixed 30 Hz grid (never more than 30 frames/s on average, whatever the display rate)
    last = last && now - last < 2 * IV ? last + IV : now;
    const dt = lastT ? Math.min(0.1, (now - lastT) / 1000) : 0;
    lastT = now;
    angle += dt * Math.PI * 2 / TURN_S;
    if (angle > Math.PI * 2) angle -= Math.PI * 2;
    draw();
  }
  let live = false;                // set once the programs are compiled and the first frame is drawn
  function kick() {
    if (st.disposed || !live) return;
    if (still) { if (W > 1 && H > 1) draw(); return; }
    if (!raf && !document.hidden && visible) { lastT = 0; last = 0; raf = requestAnimationFrame(tick); }
  }
  st.cleanup.push(() => { if (raf) cancelAnimationFrame(raf); raf = 0; });

  const ro = new ResizeObserver(() => { if (resize() && still && live) draw(); });
  ro.observe(container);
  st.cleanup.push(() => ro.disconnect());
  let io = null;
  if ('IntersectionObserver' in window) {
    io = new IntersectionObserver(es => { for (const e of es) visible = e.isIntersecting; if (visible) kick(); });
    io.observe(container);
    st.cleanup.push(() => io.disconnect());
  }
  const onVis = () => { if (!document.hidden) kick(); };
  document.addEventListener('visibilitychange', onVis);
  st.cleanup.push(() => document.removeEventListener('visibilitychange', onVis));
  if (reduceMQ && opts.rotate !== false) {
    const onMQ = () => { still = reduceMQ.matches; W = H = 0; resize(); if (!live) return; if (still) draw(); else kick(); };
    reduceMQ.addEventListener ? reduceMQ.addEventListener('change', onMQ) : reduceMQ.addListener(onMQ);
    st.cleanup.push(() => reduceMQ.removeEventListener ? reduceMQ.removeEventListener('change', onMQ) : reduceMQ.removeListener(onMQ));
  }
  canvas.addEventListener('webglcontextlost', ev => {
    ev.preventDefault();
    console.warn('rat3d: WebGL context lost');
    teardown(st);
    showFallback(container);
  });

  resize();
  // compile before the first frame, off the main thread where the browser can (KHR_parallel_shader_compile), so
  // mounting does not stall the page (the physical skin + fur programs are large under ANGLE/D3D11)
  const tc = performance.now();
  try {
    if (renderer.compileAsync) await renderer.compileAsync(scene, camera);
    else renderer.compile(scene, camera);
  } catch (_) { /* compile is only a warm-up */ }
  if (st.disposed) return null;
  mark('compile', tc);
  resize();                        // the container may have changed size while the programs compiled
  const tf = performance.now();
  draw();
  mark('first frame', tf);
  live = true;
  if (!still) kick();
  await firstFrame;
  return {dispose: () => teardown(st), canvas};
}
