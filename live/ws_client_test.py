"""End-to-end check of a running rig (live/rig.py), driven the way the viewer drives it.

    python live/rig.py --dev-shots            (in another shell / background)
    python live/ws_client_test.py [--url ws://127.0.0.1:4661/run] [--seed 2026] [--timeout 300]

First checks that the /run handshake is REFUSED for a foreign Origin and for no Origin at all. Then sends
{"type":"start"} with an allowed Origin (http://<host>:<port> of --url, i.e. localhost / 127.0.0.1, as the
rig page and record.py do), collects every message until `done`, then checks:
  stages in SPEC order (active -> done, none failed); binary pose frames at ~50 Hz with the SPEC layout;
  a pons shot stream at ~6-8 fps; the proof; the press event with the rehearsal proof; the press's click
  on pons's review Confirm; the captured tx decoded and checked (name / symbol / description with the proof
  / value / factory / pair / creator, and the strict ones: pinned image, empty links, no developer buy,
  zero uint256, empty trailing bytes, creator tax, canonical ABI encoding); pons's 4001 rejection toast;
  the run dir files; no launch_journal.json (a DRY run never reserves one); and finally `python replay.py
  <run dir>` must print MATCH. Exits 1 on any failure. (The old "no .env exists" check is gone: the owner created
  .env for the LIVE test launch. rig.py's DRY path reads .env's public coin fields through launcher.config() by
  design; what DRY must never do is reserve the journal, sign or broadcast, which the checks above cover.)
"""
import argparse
import asyncio
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
import websockets

LIVE_DIR = Path(__file__).resolve().parent
ROOT = LIVE_DIR.parent
sys.path.insert(0, str(ROOT))
import launcher  # noqa: E402

STAGES = ['wallet', 'terms', 'image', 'name', 'ticker', 'description', 'rehearsal',
          'brain_off', 'brain_on', 'press', 'click_launch', 'tx', 'outcome']
N_BONES = 65
FRAME_LEN = 7 + N_BONES * 7
RUN_FILES = ('qpos.npy', 'actions.npy', 'run.json', 'captured_tx.json', 'events.jsonl', 'pons_final.jpg')
STRICT_CHECKS = ('image', 'socials', 'amountIn', 'uint256_0', 'trailingBytes', 'abi_encoding', 'creatorTaxBps')


def allowed_origin(url):
    u = urlsplit(url)
    return f'http://{u.netloc}'


async def handshake_refused(url, origin):
    """True when the rig refuses the /run handshake for this Origin (None = no Origin header)."""
    try:
        async with websockets.connect(url, origin=origin, open_timeout=10) as ws:
            await ws.close()
            return False, 'handshake accepted'
    except websockets.exceptions.InvalidStatus as e:
        return e.response.status_code == 403, f'HTTP {e.response.status_code}'
    except Exception as e:
        return False, f'{e.__class__.__name__}: {e}'[:120]


async def collect(url, seed, timeout):
    msgs, poses = [], []
    t0 = time.time()
    async with websockets.connect(url, max_size=64 * 1024 * 1024, origin=allowed_origin(url)) as ws:
        await ws.send(json.dumps({'type': 'start', 'seed': seed}))
        while True:
            left = timeout - (time.time() - t0)
            if left <= 0:
                raise TimeoutError(f'no done within {timeout}s ({len(msgs)} messages)')
            m = await asyncio.wait_for(ws.recv(), timeout=left)
            now = time.time()
            if isinstance(m, (bytes, bytearray)):
                poses.append((now, np.frombuffer(m, dtype='<f4')))
                continue
            j = json.loads(m)
            j['_rt'] = now
            msgs.append(j)
            if j.get('type') == 'log':
                print(f"  [{now - t0:6.1f}s] {j['msg'][:150]}")
            elif j.get('type') == 'stage':
                print(f"  [{now - t0:6.1f}s] STAGE {j['stage']} {j['state']} {str(j.get('detail'))[:110]}")
            if j.get('type') == 'done':
                return msgs, poses, t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', default='ws://127.0.0.1:4661/run')
    ap.add_argument('--seed', type=int, default=2026)
    ap.add_argument('--timeout', type=float, default=300)
    a = ap.parse_args()
    results = []

    def check(name, ok, detail=''):
        results.append((name, bool(ok), detail))

    # ---- the handshake is refused for any other page (cross-site WebSocket hijacking)
    for label, origin in (('foreign Origin', 'http://evil.example'), ('no Origin', None)):
        ok, detail = asyncio.run(handshake_refused(a.url, origin))
        check(f'/run refuses {label}', ok, f'{origin!r}: {detail}')
    print(f'  allowed Origin for the run: {allowed_origin(a.url)}')

    msgs, poses, t0 = asyncio.run(collect(a.url, a.seed, a.timeout))

    by = lambda t: [m for m in msgs if m.get('type') == t]  # noqa: E731
    done = by('done')[-1]
    run_dir = ROOT / done['run_dir']
    launch = done.get('launch') or {}

    # ---- stages
    st = by('stage')
    idx = {}
    for i, m in enumerate(st):
        idx.setdefault((m['stage'], m['state']), i)
    failed = [m for m in st if m['state'] == 'failed']
    check('no stage failed', not failed, '; '.join(f"{m['stage']}: {m.get('detail')}" for m in failed))
    missing = [s for s in STAGES if (s, 'active') not in idx or (s, 'done') not in idx]
    check('every stage went active -> done', not missing, f'missing {missing}' if missing else f'{len(st)} stage events')
    order_ok = not missing and all(
        idx[(s, 'active')] < idx[(s, 'done')] for s in STAGES) and all(
        idx[(STAGES[k], 'done')] < idx[(STAGES[k + 1], 'active')] for k in range(len(STAGES) - 1))
    check('stages in SPEC order', order_ok, ' > '.join(STAGES))

    # ---- pose frames
    lens = {len(f) for _, f in poses}
    check('pose frame layout', lens == {FRAME_LEN} and all(f[0] == 1.0 and int(f[6]) == N_BONES for _, f in poses),
          f'{len(poses)} frames, lengths {sorted(lens)} (want {FRAME_LEN} float32), magic 1.0, n_bones {N_BONES}')
    trial = [(t, f) for t, f in poses if f[1] > 0]        # the first frame is the rest pose at sim_t 0
    if len(trial) > 10:
        span = trial[-1][0] - trial[0][0]
        hz = (len(trial) - 1) / span if span > 0 else 0
        dts = np.diff([f[1] for _, f in trial])
        brain = np.array([f[2] for _, f in trial])
        pressed = np.array([f[3] for _, f in trial])
        qn = np.array([np.linalg.norm(f[7:].reshape(N_BONES, 7)[:, 3:], axis=1) for _, f in trial])
        check('pose frames at ~50 Hz', 45 <= hz <= 55, f'{hz:.2f} Hz over {span:.2f} s wall, {len(trial)} trial frames')
        check('sim_t steps 0.02 s', np.allclose(dts, 0.02, atol=2e-4), f'dt min {dts.min():.5f} max {dts.max():.5f}')
        n_off = int((brain < 0.5).sum())
        check('brain off then on (once)', np.all(np.diff(brain) >= 0) and 0 < n_off < len(brain),
              f'{n_off} brain-off frames ({n_off * 0.02:.2f} s), {int((brain > 0.5).sum())} brain-on')
        check('pressed flips once', np.all(np.diff(pressed) >= 0) and pressed[-1] == 1.0,
              f'first pressed frame sim_t {trial[int(np.argmax(pressed))][1][1]:.3f}')
        check('bone quaternions unit', np.allclose(qn, 1, atol=1e-4), f'|q| {qn.min():.6f}..{qn.max():.6f}')
        paw = np.array([f[5] for _, f in trial])
        lever = np.array([f[4] for _, f in trial])
        print(f'  paw->lever {paw[0] * 1000:.1f} mm -> {paw.min() * 1000:.1f} mm; lever max {np.degrees(lever.max()):.1f} deg')
    else:
        check('pose frames at ~50 Hz', False, f'only {len(trial)} trial frames')

    # ---- shots
    shots = by('shot')
    if len(shots) > 2:
        span = shots[-1]['_rt'] - shots[0]['_rt']
        fps = (len(shots) - 1) / span if span > 0 else 0
        from PIL import Image
        im = Image.open(io.BytesIO(__import__('base64').b64decode(shots[-1]['jpg'])))
        check('pons shot stream ~6-8 fps', 5.0 <= fps <= 8.5 and im.size == (shots[-1]['w'], shots[-1]['h']),
              f'{len(shots)} shots, {fps:.2f} fps over {span:.1f} s, jpeg {im.size}')
    else:
        check('pons shot stream ~6-8 fps', False, f'{len(shots)} shots')

    # ---- proof + press
    proofs = by('proof')
    presses = by('press')
    proof = proofs[0]['proof'] if proofs else None
    check('proof message', bool(proof) and len(proof) == 64, proof or '')
    check('press event with the rehearsal proof', len(presses) == 1 and presses[0]['proof'] == proof
          and presses[0].get('matches_rehearsal'),
          f"t {presses[0].get('t'):.3f} s, brain_t {presses[0].get('brain_t')}, lever "
          f"{np.degrees(presses[0].get('lever_angle') or 0):.1f} deg" if presses else 'no press')

    # ---- the click on the real page
    clicks = [m for m in by('cursor') if m.get('click') and m.get('who') == 'rat']
    clicked_log = [m for m in by('log') if "the press clicked pons's" in m['msg']]
    lat = (clicks[0]['_rt'] - presses[0]['_rt']) if clicks and presses else None
    check("the press clicked pons's review Confirm", len(clicks) == 1 and bool(clicked_log)
          and 'Confirm' in clicked_log[0]['msg'],
          (clicked_log[0]['msg'] if clicked_log else 'no click log') + (f'; {lat:.2f} s after the press event' if lat else ''))

    # ---- the tx
    txs = by('tx')
    tx = txs[0] if txs else {}
    f, c = tx.get('fields') or {}, tx.get('checks') or {}
    check('tx captured once', len(txs) == 1, f'{len(txs)} tx events')
    check('verdict dry_captured', tx.get('verdict') == 'dry_captured', str(tx.get('verdict')))
    bad = [k for k, v in c.items() if not v.get('ok')]
    check('all tx checks pass', c and not bad, f'{len(c)} checks: {sorted(c)}' + (f' FAILED {bad}' if bad else ''))
    strict = {k: (c.get(k) or {}).get('ok') for k in STRICT_CHECKS}
    check('strict tx checks present and pass', all(strict.values()),
          ', '.join(f'{k} {"ok" if v else ("MISSING" if v is None else "FAILED")}' for k, v in strict.items()))
    wallet = f.get('from')
    want = {
        'name': f.get('name') == 'ratbrain' or f.get('name') == launcher.config()['name'],
        'symbol': f.get('symbol') == launcher.config()['symbol'],
        'description+proof': f.get('description') == launcher.description(proof or '') and f.get('proof') == proof,
        'value 0.0005 ETH': f.get('value_wei') == str(launcher.LAUNCH_FEE_WEI),
        'to factory': str(f.get('to')).lower() == launcher.FACTORY.lower(),
        'selector': f.get('selector') == launcher.SELECTOR,
        'pair ETH': str(f.get('pairToken')).lower() == launcher.ZERO.lower(),
        'creator = wallet': bool(wallet) and str(f.get('creator')).lower() == str(wallet).lower(),
    }
    check('decoded fields as filled', all(want.values()), ', '.join(f'{k} {"ok" if v else "WRONG"}' for k, v in want.items()))
    print(f"  bytes32 filled by pons: {f.get('bytes32_a')} / {f.get('bytes32_b')}")
    print(f"  image {f.get('image')} (expected {(c.get('image') or {}).get('expected')}) · creatorTaxBps "
          f"{f.get('creatorTaxBps')} · amountIn {f.get('amountIn')} · uint256_0 {f.get('uint256_0')} · socials "
          f"{f.get('socials')} · trailing {f.get('trailingBytes')} · abi {f.get('abi_encoding')}")
    print(f"  simulation {json.dumps(tx.get('simulation'))[:200]}")

    # ---- the page got the 4001
    shown = launch.get('page_showed') or ''
    check('pons got the 4001 rejection', (launch.get('refused_with') or {}).get('error', {}).get('code') == 4001
          and any(w in shown.lower() for w in ('cancel', 'reject', 'denied')), shown)
    check('nothing signed or broadcast', launch.get('signed') is False and launch.get('broadcast') is False, '')

    # ---- run dir
    have = {p: (run_dir / p).exists() for p in RUN_FILES}
    check('run dir files', all(have.values()), f"{done['run_dir']}: " + ', '.join(p for p, ok in have.items() if ok)
          + ('' if all(have.values()) else f" MISSING {[p for p, ok in have.items() if not ok]}"))
    if have['run.json']:
        meta = json.load(open(run_dir / 'run.json'))
        check("run.json['launch'] = dry_captured", (meta.get('launch') or {}).get('mode') == 'dry_captured',
              f"press {meta.get('press', {}).get('proof', '')[:16]}..., hook {meta.get('press_hook')}")
        rig = meta.get('rig') or {}
        print(f"  dev shots: {rig.get('dev_shots')}")
        print(f"  pons viewport {rig.get('viewport')} · click {rig.get('click')} · wallet refusals "
              f"{len(rig.get('wallet_refusals') or [])} {[r.get('what') for r in rig.get('wallet_refusals') or []][:8]} "
              f"· signature requests {len(rig.get('signature_requests') or [])}")
    if have['captured_tx.json']:
        cap = json.load(open(run_dir / 'captured_tx.json'))
        check('captured_tx.json has raw request + decode + checks', bool(cap.get('raw_request', {}).get('data'))
              and cap.get('verdict') == 'dry_captured' and cap.get('checks'), cap.get('verdict'))
    if have['events.jsonl']:
        n_ev = sum(1 for _ in open(run_dir / 'events.jsonl', encoding='utf-8'))
        check('events.jsonl = every JSON event', n_ev == len(msgs), f'{n_ev} lines vs {len(msgs)} JSON messages received')

    # ---- DRY never touches the launch journal (.env exists now: the owner created it for the LIVE test launch)
    check('no launch_journal.json (DRY never reserves one)', not os.path.exists(launcher.JOURNAL),
          f'{launcher.JOURNAL}')

    # ---- replay
    if have['run.json']:
        r = subprocess.run([sys.executable, 'replay.py', str(run_dir)], cwd=str(ROOT), capture_output=True, text=True,
                           timeout=300)
        tail = [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
        print('  replay.py:\n    ' + '\n    '.join(tail[-8:]))
        check('replay.py MATCH', r.returncode == 0 and tail and tail[-1].strip() == 'MATCH', tail[-1] if tail else r.stderr[-200:])

    print()
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<44} {detail}")
    n_bad = sum(1 for _, ok, _ in results if not ok)
    print(f"\n{len(results) - n_bad}/{len(results)} checks passed · run {done['run_dir']} · outcome {done.get('outcome')}")
    sys.exit(1 if n_bad else 0)


if __name__ == '__main__':
    main()
