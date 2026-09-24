"""End-to-end DRY check of a running brain rig (live/brainrig.py, the two-network brain), driven the way the viewer
drives it.

    python live/brainrig.py --dev-oracle --dev-shots        (another shell; scripted cursor, NOT the rat)
    python live/brain_ws_test.py --oracle

    python live/brainrig.py --dev-shots                     (the brain: steering + lever-press networks)
    python live/brain_ws_test.py                            (the full launch sequence, 11 targets, to Confirm)
    python live/brain_ws_test.py --brain-seconds 60         (smoke: stop 60 s after the brain comes on)

First checks that the /run handshake is REFUSED for a foreign Origin and for no Origin, reads /status and
/weights_summary (both networks), then sends {"type":"start"} with an allowed Origin (http://<host>:<port> of --url)
and collects every message until `done`.

Full run (--oracle, or the brain without --brain-seconds): all 11 targets active -> hit -> done in order; misses
masked (never forwarded: the page's own mousedown log holds exactly the forwarded hits); live typing (~90 ms/char,
the description char by char on the stream); the captured tx decoded with every check passing (description = the
brain-commit description, proof = the brain commit); pons's 4001 cancel toast; nothing signed or broadcast.
Brain runs also: frames with the brain off (pre-roll) then on, steering activations non-zero, press-network
activations non-zero exactly while `pressing` and all zero otherwise, the session files, and
`python replay_session.py <run dir>` prints MATCH. Always: the DRY rig never read .env (/status env_read false; .env
now exists, created by the owner for the LIVE test launch) and no launch_journal.json exists. The mocked LIVE path is
tested by live/brain_live_test.py. A target lit for --stall-seconds without a hit makes the test
send {"type":"stop"} (the rig itself keeps a target lit until it is hit) and report where it stalled.
Every run prints a per-target table (lit box, seconds to the hit, masked misses). Exits 1 on any failure.
"""
import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
import requests
import websockets

LIVE_DIR = Path(__file__).resolve().parent
ROOT = LIVE_DIR.parent
sys.path.insert(0, str(ROOT))
import launcher  # noqa: E402  (constants only)

STEER_SIZES = (21, 256, 256, 256, 5)
PRESS_SIZES = (200, 512, 512, 256, 38)
STEER_UNITS, PRESS_UNITS = sum(STEER_SIZES), sum(PRESS_SIZES)
UNITS = STEER_UNITS + PRESS_UNITS                       # 2,312
CONNECTIONS = 643072
HEADER_FIELDS = ('magic', 'sim_t', 'brain_on', 'cursor_x', 'cursor_y', 'tgt_cx', 'tgt_cy', 'tgt_hw', 'tgt_hh',
                 'holding', 'locked', 'lever_angle', 'head_yaw_defl', 'head_pitch_defl', 'hits', 'misses',
                 'pressing', 'press_step')
H = {k: i for i, k in enumerate(HEADER_FIELDS)}
HEADER_N = len(HEADER_FIELDS)
FRAME_BYTES = HEADER_N * 4 + UNITS                      # 2,384
KEYS = ['t01_terms_tou', 't02_terms_privacy', 't03_accept', 't04_image', 't05_name', 't06_ticker',
        't07_description', 't08_advanced', 't09_tax', 't10_launch', 't11_confirm']
DESCRIPTION = ('launched by a virtual rat: its trained brain steered the cursor and clicked every button. '
               'brain sha256 {commit}')
STRICT_CHECKS = ('image', 'socials', 'amountIn', 'uint256_0', 'trailingBytes', 'abi_encoding', 'creatorTaxBps',
                 'description', 'proof')


def allowed_origin(url):
    return f'http://{urlsplit(url).netloc}'


def http_base(url):
    return f'http://{urlsplit(url).netloc}'


def is_hex64(s):
    return isinstance(s, str) and len(s) == 64 and all(c in '0123456789abcdef' for c in s)


async def handshake_refused(url, origin):
    try:
        async with websockets.connect(url, origin=origin, open_timeout=10) as ws:
            await ws.close()
            return False, 'handshake accepted'
    except websockets.exceptions.InvalidStatus as e:
        return e.response.status_code == 403, f'HTTP {e.response.status_code}'
    except Exception as e:
        return False, f'{e.__class__.__name__}: {e}'[:120]


def header(b):
    return np.frombuffer(b[:HEADER_N * 4], dtype='<f4')


async def collect(url, seed, timeout, brain_seconds, stall_seconds):
    msgs, frames = [], []
    t0 = time.time()
    t_brain = None
    stop_sent = None
    active = {}                          # stage -> first 'active' time (reset by 'hit')
    async with websockets.connect(url, max_size=64 * 1024 * 1024, origin=allowed_origin(url)) as ws:
        await ws.send(json.dumps({'type': 'start', 'seed': seed}))
        while True:
            left = timeout - (time.time() - t0)
            if left <= 0:
                raise TimeoutError(f'no done within {timeout}s ({len(msgs)} messages)')
            now = time.time()
            if stop_sent is None:
                if brain_seconds and t_brain and now - t_brain >= brain_seconds:
                    stop_sent = f'{brain_seconds:g} s after brain on (--brain-seconds)'
                elif stall_seconds:
                    for k, ta in active.items():
                        if now - ta >= stall_seconds:
                            stop_sent = f'{k} lit for {now - ta:.0f} s without a hit (--stall-seconds {stall_seconds:g})'
                            break
                if stop_sent:
                    await ws.send(json.dumps({'type': 'stop'}))
                    print(f'  [{now - t0:6.1f}s] sent stop: {stop_sent}')
            try:
                m = await asyncio.wait_for(ws.recv(), timeout=min(left, 1.0))
            except asyncio.TimeoutError:
                continue
            now = time.time()
            if isinstance(m, (bytes, bytearray)):
                frames.append((now, bytes(m)))
                if t_brain is None and len(m) >= 12 and header(m)[H['brain_on']] > 0.5:
                    t_brain = now
                continue
            j = json.loads(m)
            j['_rt'] = now
            msgs.append(j)
            t = j.get('type')
            if t == 'log':
                print(f"  [{now - t0:6.1f}s] {j['msg'][:170]}")
            elif t == 'stage':
                print(f"  [{now - t0:6.1f}s] STAGE {j['stage']} {j['state']} {str(j.get('detail'))[:110]}")
                if j['state'] == 'active':
                    active.setdefault(j['stage'], now)
                elif j['state'] in ('hit', 'done', 'failed'):
                    active.pop(j['stage'], None)
            elif t == 'click':
                print(f"  [{now - t0:6.1f}s] CLICK ({j['x']:.0f}, {j['y']:.0f}) hit={j['hit']} forwarded={j['forwarded']} "
                      f"target={j['target']}" + (f" ({j['why']})" if j.get('why') else ''))
            elif t == 'tx':
                print(f"  [{now - t0:6.1f}s] TX verdict {j.get('verdict')} failed {j.get('failed')}")
            if t == 'done':
                return msgs, frames, t0, stop_sent


def target_table(run_dir):
    p = run_dir / 'targets.json'
    if not p.exists():
        return []
    tj = json.load(open(p, encoding='utf-8'))
    rows = []
    for t in tj['targets']:
        box = t['lights'][-1]['box'] if t['lights'] else None
        first = t['lights'][0]['at'] if t['lights'] else None
        hit = t.get('hit') or {}
        secs = (hit.get('at') - first) if (hit.get('at') and first) else None
        rows.append({'key': t['key'], 'state': t['state'], 'box_px': box,
                     'seconds_to_hit': round(secs, 1) if secs is not None else None, 'misses': len(t['misses']),
                     'lights': len(t['lights']), 'detail': t.get('detail')})
    return rows


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', default='ws://127.0.0.1:4665/run')
    ap.add_argument('--seed', type=int, default=2026)
    ap.add_argument('--timeout', type=float, default=1200)
    ap.add_argument('--oracle', action='store_true', help='the rig runs with --dev-oracle: expect the full launch')
    ap.add_argument('--brain-seconds', type=float, default=0.0,
                    help='brain smoke test: stop this long after brain on (0 = the full launch sequence)')
    ap.add_argument('--stall-seconds', type=float, default=90.0,
                    help='brain: stop if one target stays lit this long without a hit (0 = never)')
    ap.add_argument('--save-frames', default=str(LIVE_DIR / 'dev_shots'), help='save a few received typing frames here')
    ap.add_argument('--frame-prefix', default='brain2_ws')
    a = ap.parse_args()
    full = a.oracle or not a.brain_seconds
    results = []

    def check(name, ok, detail=''):
        results.append((name, bool(ok), detail))

    # ---- the handshake is refused for any other page
    for label, origin in (('foreign Origin', 'http://evil.example'), ('no Origin', None)):
        ok, detail = asyncio.run(handshake_refused(a.url, origin))
        check(f'/run refuses {label}', ok, f'{origin!r}: {detail}')

    # ---- /status, /weights_summary
    base = http_base(a.url)
    st = requests.get(f'{base}/status', timeout=10).json()
    check('/status: DRY, dev_oracle as expected', st.get('mode') == 'DRY' and st.get('dev_oracle') is bool(a.oracle),
          f"mode {st.get('mode')} dev_oracle {st.get('dev_oracle')}")
    nets = {n['key']: n for n in st.get('networks') or []}
    check('/status: both networks, 2,312 units, 643,072 connections, both sha256, brain commit',
          st.get('units') == UNITS and st.get('connections') == CONNECTIONS and is_hex64(st.get('steer_sha256'))
          and is_hex64(st.get('press_sha256')) and is_hex64(st.get('brain_commit'))
          and nets.get('steer', {}).get('sizes') == list(STEER_SIZES)
          and nets.get('press', {}).get('sizes') == list(PRESS_SIZES),
          f"steer {st.get('steer_policy')} {str(st.get('steer_sha256'))[:12]} · press {st.get('press_policy')} "
          f"{str(st.get('press_sha256'))[:12]} · commit {str(st.get('brain_commit'))[:16]} · {st.get('units')} units "
          f"{st.get('connections')} connections")
    fl = st.get('frame') or {}
    check('/status: frame layout', fl.get('magic') == 3.0 and fl.get('bytes') == FRAME_BYTES
          and tuple(fl.get('header_fields') or ()) == HEADER_FIELDS, json.dumps(fl)[:160])
    ws_ = requests.get(f'{base}/weights_summary', timeout=20).json()
    wn = {n['key']: n for n in ws_.get('networks') or []}

    def pairs_ok(n, sizes):
        ps = n.get('pairs') or []
        return (n.get('sizes') == list(sizes) and len(ps) == 4
                and all(0 < len(p['links']) <= 400 for p in ps)
                and all(p['n_from'] == sizes[i] and p['n_to'] == sizes[i + 1] for i, p in enumerate(ps))
                and all(0 <= s < p['n_from'] and 0 <= d < p['n_to'] for p in ps for s, d, _ in p['links']))
    shapes_ok = (ws_.get('units') == UNITS and ws_.get('connections') == CONNECTIONS and set(wn) == {'steer', 'press'}
                 and pairs_ok(wn['steer'], STEER_SIZES) and pairs_ok(wn['press'], PRESS_SIZES)
                 and wn['steer'].get('sha256') == st.get('steer_sha256') and wn['press'].get('sha256') == st.get('press_sha256'))
    check('/weights_summary: both networks, 4 layer pairs each, top <=400 links', shapes_ok,
          ' | '.join(f"{k}: " + ', '.join(f"{p['from']}->{p['to']} {len(p['links'])}" for p in n.get('pairs') or [])
                     for k, n in wn.items()))
    s_out = (wn.get('steer') or {}).get('outputs') or []
    motor = (wn.get('press') or {}).get('motor') or []
    lever = [m['name'] for m in motor if m['group'] == 'presses the lever']
    check('labels: steering outputs = 4 neck (steer the cursor) + PRESS; press motor has the forelimbs',
          len(s_out) == 5 and sum(o['group'] == 'steers the cursor' for o in s_out) == 4 and s_out[4]['name'] == 'PRESS'
          and len(motor) == 38 and len(lever) >= 8 and len((wn.get('steer') or {}).get('input_labels') or []) == 21,
          f"steer outputs {[o['name'] for o in s_out]}; press: {len(lever)} forelimb outputs")

    msgs, frames, t0, stop_sent = asyncio.run(collect(a.url, a.seed, a.timeout, 0 if a.oracle else a.brain_seconds,
                                                      0 if a.oracle else a.stall_seconds))
    by = lambda t: [m for m in msgs if m.get('type') == t]  # noqa: E731
    done = by('done')[-1]
    run_dir = ROOT / done['run_dir']
    launch = done.get('launch') or {}
    commit_msgs = by('commit')
    commit = commit_msgs[0]['commit'] if commit_msgs else None
    check('commit message (64 hex) = /status brain_commit', is_hex64(commit) and commit == st.get('brain_commit'),
          str(commit))

    # ---- binary brain frames
    lens = {len(b) for _, b in frames}
    hdrs = np.array([header(b) for _, b in frames]) if frames else np.zeros((0, HEADER_N))
    check('brain frame layout (2,384 bytes, magic 3.0)', lens == {FRAME_BYTES} and len(hdrs) and np.all(hdrs[:, 0] == 3.0),
          f'{len(frames)} frames, lengths {sorted(lens)} (want {FRAME_BYTES})')
    if len(frames) > 50:
        span = frames[-1][0] - frames[0][0]
        hz = (len(frames) - 1) / span if span > 0 else 0
        simdt = np.diff(hdrs[:, H['sim_t']])
        check('brain frames at ~25 Hz', 22 <= hz <= 27.5, f'{hz:.2f} Hz over {span:.1f} s wall; sim dt median {np.median(simdt):.4f} s')
        cx, cy = hdrs[:, H['cursor_x']], hdrs[:, H['cursor_y']]
        on = hdrs[:, H['brain_on']] > 0.5
        pr = hdrs[:, H['pressing']] > 0.5
        acts = np.array([np.frombuffer(b[HEADER_N * 4:], np.uint8) for _, b in frames])
        sa, pa = acts[:, :STEER_UNITS], acts[:, STEER_UNITS:]
        check('cursor moves', (cx.max() - cx.min()) + (cy.max() - cy.min()) > 0.02,
              f'x {cx.min():.3f}..{cx.max():.3f} y {cy.min():.3f}..{cy.max():.3f}')
        if a.oracle:
            check('DEV: activations all zero, brain_on 0, pressing 0 (a scripted cursor has no brain)',
                  not on.any() and not pr.any() and int(acts.sum()) == 0,
                  f'{int(on.sum())} brain-on frames, activation sum {int(acts.sum())}')
        else:
            n_off = int((~on).sum())
            flips = int(np.sum(np.diff(on.astype(int)) != 0))
            check('brain off (pre-roll) then on, once', flips == 1 and on[-1] and 0 < n_off,
                  f'{n_off} pre-roll frames ({n_off / 25:.2f} s), {int(on.sum())} brain-on frames')
            firing = (sa[on] > 64).sum(axis=1) if on.any() else np.array([0])
            check('steering activations: zero in pre-roll, non-zero with the brain on',
                  int(sa[~on].sum()) == 0 and int(sa[on].sum()) > 0,
                  f'steering units above 25% of scale per frame: median {int(np.median(firing))} of {STEER_UNITS}')
            starts = int(np.sum(np.diff(pr.astype(int)) == 1) + (1 if pr[0] else 0))
            check('press network: non-zero exactly while pressing, all zero while idle',
                  pr.any() and int(pa[~pr].sum()) == 0 and bool(np.all(pa[pr].sum(axis=1) > 0))
                  and bool(np.all(hdrs[pr, H['press_step']] >= 0)),
                  f'{int(pr.sum())} pressing frames in {starts} presses; idle press-net sum {int(pa[~pr].sum())}; '
                  f'press_step max {hdrs[:, H["press_step"]].max():.0f}')
            lay = np.cumsum((0,) + STEER_SIZES + PRESS_SIZES)
            names = ['s.in', 's.h1', 's.h2', 's.h3', 's.out', 'p.in', 'p.h1', 'p.h2', 'p.h3', 'p.motor']
            per = []
            for i, n in enumerate(names):
                rows = acts[on & (pr if n.startswith('p.') else True)][:, lay[i]:lay[i + 1]]
                per.append(f'{n} {(rows > 64).mean():.2f}' if len(rows) else f'{n} -')
            print('  fraction of units above 25% of scale, by layer:', ' '.join(per))
            print(f"  head deflection yaw {hdrs[:, H['head_yaw_defl']].min():.3f}..{hdrs[:, H['head_yaw_defl']].max():.3f} rad, "
                  f"pitch {hdrs[:, H['head_pitch_defl']].min():.3f}..{hdrs[:, H['head_pitch_defl']].max():.3f} rad; lever max "
                  f"{np.degrees(hdrs[:, H['lever_angle']].max()):.1f} deg; locked {int(hdrs[:, H['locked']].sum())} frames; "
                  f"brain hits {int(hdrs[-1, H['hits']])} misses {int(hdrs[-1, H['misses']])}")
    else:
        check('brain frames at ~25 Hz', False, f'only {len(frames)} frames')

    # ---- the pons stream
    shots = by('shot')
    fps = None
    if len(shots) > 2:
        span = shots[-1]['_rt'] - shots[0]['_rt']
        fps = (len(shots) - 1) / span if span > 0 else 0
        from PIL import Image
        im = Image.open(io.BytesIO(base64.b64decode(shots[-1]['jpg'])))
        gaps = np.diff([s['_rt'] for s in shots])
        check('pons screencast frames (JPEG, page size)', im.size == (shots[-1]['w'], shots[-1]['h']) == (1280, 900),
              f'{len(shots)} frames, {fps:.2f} fps average over {span:.1f} s, jpeg {im.size}, '
              f'gap median {np.median(gaps) * 1000:.0f} ms p90 {np.percentile(gaps, 90) * 1000:.0f} ms')
    else:
        check('pons screencast frames (JPEG, page size)', False, f'{len(shots)} shots')

    # ---- clicks: misses masked, hits forwarded, the page saw exactly the hits
    clicks = by('click')
    fwd = [c for c in clicks if c['forwarded']]
    miss = [c for c in clicks if not c['forwarded']]
    downs = done.get('page_mousedowns') or []
    near = lambda d, c: abs(d[0] - c['x']) <= 1.5 and abs(d[1] - c['y']) <= 1.5  # noqa: E731
    stray = [d for d in downs if not any(near(d, c) for c in fwd)]
    miss_seen = [c for c in miss if any(near(d, c) for d in downs)
                 and not any(abs(f['x'] - c['x']) <= 1.5 and abs(f['y'] - c['y']) <= 1.5 for f in fwd)]
    check('misses never forwarded (the page got a mousedown only for each forwarded hit)',
          len(downs) == len(fwd) and not stray and not miss_seen and all(not c['hit'] for c in miss),
          f'{len(fwd)} forwarded hits, {len(miss)} masked misses, {len(downs)} page mousedowns, {len(stray)} stray')

    stages = by('stage')
    if full:
        first = {}
        for i, m in enumerate(stages):
            first.setdefault((m['stage'], m['state']), i)
        failed = [m for m in stages if m['state'] == 'failed']
        check('no stage failed', not failed, '; '.join(f"{m['stage']}: {m.get('detail')}" for m in failed))
        missing = [k for k in KEYS if any((k, s) not in first for s in ('active', 'hit', 'done'))]
        check('all 11 targets active -> hit -> done', not missing, f'missing {missing}' if missing else f'{len(stages)} stage events')
        order = not missing and all(first[(k, 'active')] < first[(k, 'hit')] < first[(k, 'done')] for k in KEYS) and all(
            first[(KEYS[i], 'done')] < first[(KEYS[i + 1], 'active')] for i in range(len(KEYS) - 1))
        check('targets in SPEC order', order, ' > '.join(k[4:] for k in KEYS))
        check('one forwarded hit per target', sorted(c['target'] for c in fwd) == sorted(KEYS),
              f"{[c['target'] for c in fwd]}")
        if a.oracle:
            check('at least one miss (the oracle clicks outside the first target once)', len(miss) >= 1,
                  f"{[(round(c['x']), round(c['y']), c['target']) for c in miss]}")

        # ---- live typing
        typ = (done.get('stream') or {}).get('typing') or {}
        print('  typing (rig):', json.dumps(typ))
        slow = {k: v for k, v in typ.items() if not (85 <= v.get('ms_per_char', 0) <= 140)}
        check('typing at ~90 ms/char', typ and not slow and all(v.get('ok') and not v.get('refilled') for v in typ.values()),
              ', '.join(f"{k} {v.get('chars')} chars {v.get('ms_per_char')} ms/char {v.get('frames_streamed')} frames"
                        for k, v in typ.items()))
        # frames the viewer got while the description was being typed (between its 'hit' and 'done' stages)
        th = next((m['_rt'] for m in stages if m['stage'] == 't07_description' and m['state'] == 'hit'), None)
        td = next((m['_rt'] for m in stages if m['stage'] == 't07_description' and m['state'] == 'done'), None)
        if th and td:
            win = [s for s in shots if th <= s['_rt'] <= td]
            shas = [hashlib.sha256(base64.b64decode(s['jpg'])).hexdigest() for s in win]
            desc_len = len(DESCRIPTION.format(commit=commit or '0' * 64))
            check('description typed char by char on the stream', len(set(shas)) >= 0.8 * desc_len,
                  f'{len(win)} frames ({len(set(shas))} distinct) received in {td - th:.1f} s for {desc_len} chars '
                  f'({len(win) / max(td - th, 1e-6):.1f} fps)')
            if a.save_frames and win:
                outd = Path(a.save_frames)
                outd.mkdir(exist_ok=True)
                for f in (0.25, 0.5, 0.75):
                    s = win[int(len(win) * f)]
                    p = outd / f"{a.frame_prefix}{'_DEV' if a.oracle else ''}_description_{int(f * 100)}pct.jpg"
                    p.write_bytes(base64.b64decode(s['jpg']))
                    print(f'  saved {p.relative_to(ROOT)} (received {s["_rt"] - th:.1f} s into typing)')
        else:
            check('description typed char by char on the stream', False, 'no t07 hit/done window')

        # ---- the tx
        txs = by('tx')
        tx = txs[0] if txs else {}
        f, c = tx.get('fields') or {}, tx.get('checks') or {}
        check('tx captured once, verdict dry_captured', len(txs) == 1 and tx.get('verdict') == 'dry_captured',
              f"{len(txs)} tx events, verdict {tx.get('verdict')}")
        bad = [k for k, v in c.items() if not v.get('ok')]
        check('all tx checks pass', c and not bad, f'{len(c)} checks' + (f' FAILED {bad}' if bad else ''))
        strict = {k: (c.get(k) or {}).get('ok') for k in STRICT_CHECKS}
        check('strict checks present and pass (incl. description/proof = brain commit)', all(strict.values()),
              ', '.join(f'{k} {"ok" if v else ("MISSING" if v is None else "FAILED")}' for k, v in strict.items()))
        want_desc = DESCRIPTION.format(commit=commit)
        check('the calldata carries the brain-commit description', f.get('description') == want_desc
              and f.get('proof') == commit, f.get('description'))
        print(f"  decoded: {f.get('name')} / {f.get('symbol')} · image {f.get('image')} · tax {f.get('creatorTaxBps')} bps · "
              f"value {f.get('value')} · to {f.get('to')} · creator {f.get('creator')}")
        print(f"  simulation {json.dumps(tx.get('simulation'))[:220]}")
        shown = launch.get('page_showed') or ''
        check("pons got the 4001 and showed its cancel toast", (launch.get('refused_with') or {}).get('error', {}).get('code') == 4001
              and 'cancel' in shown.lower(), shown)
    check('nothing signed or broadcast', launch.get('signed') is False and launch.get('broadcast') is False, '')

    if a.oracle:
        files = ('steer.pt', 'press.pt', 'oracle.json', 'targets.json', 'captured_tx.json', 'events.jsonl', 'pons_final.jpg')
        have = {p: (run_dir / p).exists() for p in files}
        check('DEV run dir files (no session.json: not a brain run)', all(have.values()) and not (run_dir / 'session.json').exists(),
              f"{done['run_dir']}: " + ', '.join(p for p, ok in have.items() if ok))
    else:
        # ---- brain run
        if not full:
            check('stopped (or ended) and recorded', done.get('recorded') and launch.get('mode') in ('ended', 'dry_captured'),
                  f"outcome {launch.get('mode')} reason {launch.get('reason')} · stop sent {stop_sent}")
        act = [m for m in stages if m['state'] == 'active']
        check('targets lit', bool(act), f"{len(act)} 'active' stage events, first {act[0]['stage'] if act else None}")
        files = ('steer.pt', 'press.pt', 'session.json', 'qpos.npy', 'actions.npy', 'targets.json', 'captured_tx.json',
                 'events.jsonl', 'pons_final.jpg')
        have = {p: (run_dir / p).exists() for p in files}
        check('brain run dir files', all(have.values()), f"{done['run_dir']}: " + ', '.join(p for p, ok in have.items() if ok)
              + ('' if all(have.values()) else f" MISSING {[p for p, ok in have.items() if not ok]}"))
        if have['session.json']:
            meta = json.load(open(run_dir / 'session.json'))
            print(f"  session: {meta['steps']} brain steps, {len(meta['commands'])} commands, {len(meta['clicks'])} clicks, "
                  f"commit {meta['brain_commit'][:16]}…, proof {meta['session_proof'][:16]}…, end {meta.get('end_reason')} "
                  f"{meta.get('end_info')}")
            check('session.json: commit = the commit message; both network sha256 = /status',
                  meta['brain_commit'] == commit and meta.get('policy_sha256') == st.get('steer_sha256')
                  and meta.get('press_sha256') == st.get('press_sha256'),
                  f"commit {meta['brain_commit'][:16]} steer {str(meta.get('policy_sha256'))[:12]} press "
                  f"{str(meta.get('press_sha256'))[:12]} ({meta.get('policy')}, {meta.get('press_policy')})")
            tj = json.load(open(run_dir / 'targets.json'))
            check('targets.json: forwarded + masked = every brain click', tj['hits_forwarded'] + tj['misses_masked']
                  == len(meta['clicks']) == len(clicks), f"{tj['hits_forwarded']} + {tj['misses_masked']} vs "
                  f"{len(meta['clicks'])} session clicks, {len(clicks)} click events")
            r = subprocess.run([sys.executable, 'replay_session.py', str(run_dir)], cwd=str(ROOT), capture_output=True,
                               text=True, timeout=1800)
            tail = [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
            print('  replay_session.py:\n    ' + '\n    '.join(tail[-8:]))
            check('replay_session.py MATCH', r.returncode == 0 and tail and tail[-1].strip() == 'MATCH',
                  tail[-1] if tail else r.stderr[-300:])

    # ---- common
    if (run_dir / 'events.jsonl').exists():
        n_ev = sum(1 for _ in open(run_dir / 'events.jsonl', encoding='utf-8'))
        check('events.jsonl = every JSON event', n_ev == len(msgs), f'{n_ev} lines vs {len(msgs)} JSON messages')
    # .env exists now (the owner created it for the LIVE test launch): what DRY must guarantee is that it never READS
    # it (brainrig's only path to .env is live_config() -> launcher.config(), which sets /status env_read) and never
    # reserves the launch journal. live/brain_live_test.py proves the same in-process with .env access trapped.
    st_after = requests.get(f'{base}/status', timeout=10).json()
    check('DRY never read .env (/status env_read false, before and after the run); no launch_journal.json',
          st.get('env_read') is False and st_after.get('env_read') is False and st_after.get('mode') == 'DRY'
          and not os.path.exists(launcher.JOURNAL),
          f"env_read {st.get('env_read')} -> {st_after.get('env_read')} · journal {os.path.exists(launcher.JOURNAL)}")
    st2 = done.get('stream') or {}
    print(f"\n  screencast: {st2.get('shots')} frames sent at {st2.get('fps')} fps (rig side), {st2.get('shots_dropped')} "
          f"dropped; received {len(shots)} at {fps and round(fps, 2)} fps; brain frames {len(frames)}")
    print(f"  end: outcome {done.get('outcome')} · {launch.get('reason') or launch.get('error') or ''}"
          + (f' · test sent stop: {stop_sent}' if stop_sent else ''))
    print('\n  per target (lit box in page px [x, y, w, h], seconds from first lit to the hit, masked misses):')
    for row in target_table(run_dir):
        print(f"    {row['key']:<18} {row['state']:<8} box {row['box_px']} · "
              f"{'-' if row['seconds_to_hit'] is None else str(row['seconds_to_hit']) + ' s'} · {row['misses']} misses"
              f" · lit {row['lights']}x" + (f" · {row['detail'][:80]}" if row['state'] not in ('done', 'pending') else ''))
    print()
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<66} {detail}")
    n_bad = sum(1 for _, ok, _ in results if not ok)
    print(f"\n{len(results) - n_bad}/{len(results)} checks passed · run {done['run_dir']} · outcome {done.get('outcome')}")
    sys.exit(1 if n_bad else 0)


if __name__ == '__main__':
    main()
