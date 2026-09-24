"""THE run: the trained rat is dropped in the box; the instant its paw registers a press, the coin launches.

Rehearse (DRY, default: eth_call only, nothing sent):
    python launch_run.py --policy runs/lever_v3/policy_final.pt --seed 2026 --out runs/rehearsal_2026
Go live (only after a DRY rehearsal of the same policy+seed replayed MATCH):
    python launch_run.py --policy runs/lever_v3/policy_final.pt --seed 2026 --out runs/launch \
        --live --rehearsal runs/rehearsal_2026 --confirm RATBRAIN

The policy file is frozen: copied into <out>/policy.pt (read-only) and the run uses that copy, so
a training job can never change the weights under a launch. LIVE refuses unless:
  --live is given, .env says RATBRAIN_LIVE=1 with a key, --confirm equals the coin symbol,
  launcher.preflight() passes (fresh wallet, balance, image, no journal), and the rehearsal has the
  same policy sha256, seed and pre-roll, replays MATCH, and its proof equals the live proof at the press.
"""
import argparse, json, os, shutil, stat, sys

import launcher
from rollout import run, save, sha_bytes

HERE = os.path.dirname(os.path.abspath(__file__))


def freeze(policy, out):
    os.makedirs(out, exist_ok=True)
    dst = os.path.join(out, 'policy.pt')
    if os.path.exists(dst):   # an interrupted earlier attempt: reuse only if it is the same policy
        if sha_bytes(open(dst, 'rb').read()) != sha_bytes(open(policy, 'rb').read()):
            raise SystemExit(f'{dst} holds a different policy; pick a new --out')
        return dst
    shutil.copyfile(policy, dst)
    os.chmod(dst, stat.S_IREAD)
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--policy', required=True)
    ap.add_argument('--seed', type=int, required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--preroll', type=float, default=1.0, help='seconds the rat stands with the brain off first')
    ap.add_argument('--live', action='store_true')
    ap.add_argument('--rehearsal', help='DRY run dir of the same policy+seed (required for --live)')
    ap.add_argument('--confirm', help='type the coin symbol to confirm a live launch')
    a = ap.parse_args()
    out = os.path.join(HERE, a.out)
    if os.path.exists(os.path.join(out, 'run.json')):
        raise SystemExit(f'{a.out} already holds a run; pick a new --out')
    policy_sha = sha_bytes(open(os.path.join(HERE, a.policy), 'rb').read())
    cfg = launcher.config()

    pre = None
    expected_proof = None
    if a.live:
        if a.confirm != cfg['symbol']:
            raise SystemExit(f"--confirm must equal the symbol {cfg['symbol']!r}")
        if not a.rehearsal:
            raise SystemExit('--live needs --rehearsal <DRY run dir of the same policy and seed>')
        reh = json.load(open(os.path.join(HERE, a.rehearsal, 'run.json')))
        if (reh['policy_sha256'], reh['seed'], reh['preroll_s']) != (policy_sha, a.seed, a.preroll):
            raise SystemExit('rehearsal does not match this policy / seed / pre-roll')
        if (reh.get('launch') or {}).get('mode') != 'dry' or not reh.get('press'):
            raise SystemExit('rehearsal was not a clean DRY run with a press')
        import replay
        if not replay.main(os.path.join(HERE, a.rehearsal)):
            raise SystemExit('rehearsal does not replay MATCH on this machine; not going live')
        expected_proof = reh['press']['proof']
        pre = launcher.preflight(cfg, expected_proof)   # raises LaunchRefused on any problem, BEFORE the rat runs
        print('preflight ok:', json.dumps({k: v for k, v in pre.items() if k != 'acct'}, default=str))
        # exclusive create: from here on no other process can start a launch
        launcher.reserve_journal({'creator': pre['creator'], 'policy_sha256': policy_sha, 'seed': a.seed,
                                  'expected_proof': expected_proof, 'out': a.out})

    print(f"mode: {'LIVE' if a.live else 'DRY (eth_call only, nothing sent)'}  coin: {cfg['name']} (${cfg['symbol']})")

    def on_press(proof):
        print(f'PRESS registered. proof {proof}', flush=True)
        if a.live and proof != expected_proof:
            print('proof differs from the rehearsal: NOT launching', flush=True)
            return {'mode': 'aborted', 'reason': 'proof differs from rehearsal', 'expected': expected_proof}
        res = launcher.launch(proof, live=a.live, pre=pre, log=print)
        print(json.dumps(res, indent=2), flush=True)
        return res

    frozen = freeze(os.path.join(HERE, a.policy), out)
    meta, frames, acts = run(frozen, a.seed, on_press=on_press, preroll_s=a.preroll)
    meta['coin'] = {'name': cfg['name'], 'symbol': cfg['symbol']}
    meta['live_requested'] = bool(a.live)
    meta['source_policy'] = a.policy
    save(out, meta, frames, acts)
    if not meta['press']:
        print('no press this run (the rat did not press). nothing launched. try another seed.')
    print('saved', a.out, '| launch:', (meta.get('launch') or {}).get('mode'))


if __name__ == '__main__':
    main()
