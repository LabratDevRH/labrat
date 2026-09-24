"""Score a policy. Never imports the launcher: nothing here can touch a chain.

    python eval.py runs/lever_v3/policy_final.pt [--n 200] [--json out.json]

Suites:
  launch   exactly the launch-run conditions: nominal box, 1 s brain-off pre-roll, deterministic actions
  wide     wider start positions/turns, lever stiffness and paw/floor friction +-30%, random pre-roll, shoves
  pushes   launch conditions plus random shoves on the torso before the press (0.3-0.8 N, 0.1 s)
  noisy    launch conditions with the policy's own exploration noise switched on
Each episode runs until 1.2 s after the press (what the film shows) or 5 s without one.
"""
import argparse, json, sys
import numpy as np

from env import LeverEnv
from ptload import load, NumpyPolicy

POST_PRESS_STEPS = 60


def episode(env, pol, seed, suite):
    rng = np.random.default_rng(10_000 + seed)
    randomize = suite == 'wide'
    obs = env.reset(seed=seed, randomize=randomize)
    if suite != 'wide':
        obs = env.preroll(1.0)
    push_at = rng.integers(3, 20) if suite == 'pushes' else -1
    push = None
    press_t = None
    fell_before = fell_after = False
    body_lever_at_press = False
    fell_at_press = False
    after = 0
    min_z_after = 1.0
    while True:
        a = pol(obs)
        if suite == 'noisy':
            a = a + pol.std * rng.standard_normal(a.shape).astype(np.float32)
        if env.t == push_at and press_t is None:   # shoves only before the press, like training
            ang = rng.uniform(0, 2 * np.pi)
            push = (np.array([np.cos(ang), np.sin(ang), 0]) * rng.uniform(0.3, 0.8), 5)
        if push and push[1] > 0 and press_t is None:
            env.d.xfrc_applied[env.torso, :3] = push[0]; push = (push[0], push[1] - 1)
        else:
            env.d.xfrc_applied[env.torso, :3] = 0
        obs, r, done, info = env.step(a)
        if press_t is None and info['pressed']:
            press_t = env.t * 0.02
            body_lever_at_press = bool(info.get('body_on_lever', False))
            fell_at_press = bool(info['fell'])
        if press_t is None:
            if info['fell']:
                fell_before = True
                break
            if env.t >= env.max_steps:
                break
        else:
            after += 1
            min_z_after = min(min_z_after, float(env.d.qpos[2]))
            if info['fell']:
                fell_after = True
            if after >= POST_PRESS_STEPS:
                break
    env.d.xfrc_applied[:] = 0
    return {'pressed': press_t is not None, 'press_t': press_t, 'fell_before': fell_before, 'fell_at_press': fell_at_press,
            'fell_after': fell_after, 'body_lever_at_press': body_lever_at_press, 'min_z_after': min_z_after}


def score(policy_path, n=200, suites=('launch', 'wide', 'pushes', 'noisy')):
    env = LeverEnv(0)
    pol = NumpyPolicy(load(policy_path), env.obs_dim)
    out = {}
    for s in suites:
        eps = [episode(env, pol, seed, s) for seed in range(n)]
        pressed = [e for e in eps if e['pressed']]
        clean = [e for e in pressed if not e['fell_after']]
        out[s] = {
            'n': n,
            'press_rate': round(len(pressed) / n, 4),
            'clean_rate': round(len(clean) / n, 4),          # pressed AND still standing 1.2 s later
            'fell_before_press': sum(e['fell_before'] for e in eps),
            'fell_after_press': sum(e['fell_after'] for e in pressed),
            'no_press_timeout': sum(1 for e in eps if not e['pressed'] and not e['fell_before']),
            'press_t_mean': round(float(np.mean([e['press_t'] for e in pressed])), 3) if pressed else None,
            'press_t_max': round(float(np.max([e['press_t'] for e in pressed])), 3) if pressed else None,
            'failed_seeds': [i for i, e in enumerate(eps) if not e['pressed'] or e['fell_after']][:20],
        }
    return out


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('policy'); ap.add_argument('--n', type=int, default=200); ap.add_argument('--json')
    ap.add_argument('--suites', default='launch,wide,pushes,noisy')
    a = ap.parse_args()
    res = score(a.policy, a.n, tuple(a.suites.split(',')))
    print(json.dumps(res, indent=1))
    if a.json:
        json.dump({'policy': a.policy, **res}, open(a.json, 'w'), indent=1)
