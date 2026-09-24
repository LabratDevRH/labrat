"""Re-run a recorded rat and check it reproduces the proof, the whole recording, and (if live) the chain.

    python replay.py runs/launch              # full re-run: policy -> actions -> physics
    python replay.py runs/launch --actions    # feed the RECORDED actions to MuJoCo (no policy maths;
                                              # use this on a different machine/BLAS)

MATCH requires: the replayed proof equals run.json's proof, the replayed frames equal the recorded
qpos.npy bit for bit over the WHOLE recording (post-press footage included), and, for a live launch,
the sha256 inside the coin's on-chain description equals the proof.
"""
import hashlib, json, os, re, sys

import numpy as np
import mujoco

from rollout import run

HERE = os.path.dirname(os.path.abspath(__file__))


def onchain_proof(tx):
    import launcher
    t, err = launcher.rpc('eth_getTransactionByHash', [tx])
    if err or not t:
        return None, f'could not fetch tx: {err}'
    desc = launcher.decode_description(t['input'])
    m = re.search(r'sha256 ([0-9a-f]{64})', desc)
    return (m.group(1) if m else None), desc


def main(run_dir, use_actions=False):
    run_dir = os.path.join(HERE, run_dir) if not os.path.isabs(run_dir) else run_dir
    meta = json.load(open(os.path.join(run_dir, 'run.json')))
    env = meta.get('environment', {})
    if env.get('mujoco', meta.get('mujoco')) != mujoco.__version__ or env.get('numpy', np.__version__) != np.__version__:
        print(f"warning: recorded with {env or meta.get('mujoco')}, you have mujoco {mujoco.__version__} numpy {np.__version__}")
    rec = np.load(os.path.join(run_dir, 'qpos.npy'))
    acts = np.load(os.path.join(run_dir, 'actions.npy')) if use_actions else None
    re_meta, frames, _ = run(os.path.join(HERE, meta['policy']), meta['seed'],
                             preroll_s=meta.get('preroll_s', 0.0), actions=acts)
    want = (meta.get('press') or {}).get('proof')
    got = (re_meta.get('press') or {}).get('proof')
    same_len = len(rec) == len(frames)
    n = min(len(rec), len(frames))
    maxdiff = float(np.abs(rec[:n] - frames[:n]).max()) if n else float('inf')
    ok_frames = same_len and maxdiff == 0.0
    print('mode          ', 'recorded actions' if use_actions else 'full re-run (policy + physics)')
    print('policy sha256 ', re_meta['policy_sha256'], 'OK' if re_meta['policy_sha256'] == meta['policy_sha256'] else 'DIFFERENT')
    print('scene  sha256 ', re_meta['scene_sha256'], 'OK' if re_meta['scene_sha256'] == meta['scene_sha256'] else 'DIFFERENT')
    for f, s in re_meta['code_sha256'].items():
        rs = meta.get('code_sha256', {}).get(f)
        print(f'code {f:<10}', s, 'OK' if s == rs else 'DIFFERENT')
    print('frames        ', f'{len(frames)} vs recorded {len(rec)}, max qpos diff {maxdiff}')
    print('recorded proof', want)
    print('replayed proof', got)
    ok = bool(want) and want == got and ok_frames
    launch = meta.get('launch') or {}
    if launch.get('tx') and launch.get('mode') in ('live', 'live_unverified', 'reverted', 'sent_unconfirmed'):
        chain, desc = onchain_proof(launch['tx'])
        print('launch tx     ', launch.get('explorer', launch['tx']), '| mode', launch.get('mode'))
        print('on-chain proof', chain)
        ok = ok and chain == want
    print('MATCH' if ok else 'MISMATCH')
    return ok


if __name__ == '__main__':
    args = [x for x in sys.argv[1:] if not x.startswith('--')]
    sys.exit(0 if main(args[0], use_actions='--actions' in sys.argv) else 1)
