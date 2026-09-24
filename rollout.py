"""Deterministic rollout of a trained policy, with an optional hook fired on the press.

REPLAY PROOF = sha256 over, in order:
    scene.xml and env.py + rollout.py + ptload.py (text, CRLF normalised to LF so a git checkout on any OS
    hashes the same), the policy file bytes, the string 'seed=<seed>;preroll=<seconds>', the reset frame and
    every pre-roll frame, then for every control step up to and including the press: the float32 action
    bytes followed by every 2 ms physics qpos (float64 little-endian) it produced.
The hook fires on the press step, after hashing and before any further simulation.

Honest scope: MuJoCo and numpy are deterministic on one machine (replay here gives max diff 0.0).
On a different CPU / BLAS / numpy build the policy's float32 maths can round differently, so a
full re-run there may diverge. `replay.py --actions` feeds the RECORDED actions to MuJoCo instead,
which removes the policy's maths from the check.
"""
import hashlib, io, json, os, platform, sys, time
import numpy as np
import mujoco

from env import LeverEnv, SCENE, CTRL_DT
from ptload import load, NumpyPolicy

HERE = os.path.dirname(os.path.abspath(__file__))
CODE_FILES = ('env.py', 'rollout.py', 'ptload.py')


def sha_bytes(b):
    return hashlib.sha256(b).hexdigest()


def text_bytes(path):
    return open(path, 'rb').read().replace(b'\r\n', b'\n')


def environment():
    return {'python': sys.version.split()[0], 'numpy': np.__version__, 'mujoco': mujoco.__version__,
            'platform': platform.platform(), 'machine': platform.machine(), 'processor': platform.processor()}


def run(policy_path, seed, on_press=None, max_extra_steps=60, preroll_s=0.0, actions=None,
        on_step=None, realtime=False):
    """actions: optional recorded (T, 38) float32 array to replay instead of querying the policy.
    on_step(env, phase, info): called after every control step ('preroll' or 'brain'), e.g. to stream the pose.
    realtime: pace the simulation at wall-clock speed (50 Hz control). Neither changes the physics or the proof:
    the pre-roll is stepped in 10-step pieces, which is bit-identical to stepping it in one go."""
    pol_bytes = open(policy_path, 'rb').read()          # read ONCE: hash, sha and weights come from these bytes
    scene_bytes = text_bytes(SCENE)
    code = {f: text_bytes(os.path.join(HERE, f)) for f in CODE_FILES}
    env = LeverEnv(seed, record=True)
    pol = NumpyPolicy(load(io.BytesIO(pol_bytes)), env.obs_dim)
    obs = env.reset(seed=seed)
    t0 = time.perf_counter()
    tick = [0]

    def pace():
        tick[0] += 1
        if realtime:
            dt = t0 + tick[0] * CTRL_DT - time.perf_counter()
            if dt > 0:
                time.sleep(dt)

    if preroll_s:
        n = int(round(preroll_s / env.m.opt.timestep))
        if on_step or realtime:
            per = int(round(CTRL_DT / env.m.opt.timestep))
            done_n = 0
            while done_n < n:
                k = min(per, n - done_n)
                obs = env.preroll_steps(k); done_n += k
                if on_step:
                    on_step(env, 'preroll', None)
                pace()
        else:
            obs = env.preroll_steps(n)
    preroll_frames = len(env.frames) - 1               # index of the last brain-off frame

    h = hashlib.sha256()
    h.update(scene_bytes); h.update(pol_bytes)
    for f in CODE_FILES:
        h.update(code[f])
    h.update(f'seed={seed};preroll={preroll_s!r}'.encode())
    for f in env.frames:
        h.update(np.ascontiguousarray(f, dtype='<f8').tobytes())
    hashed = len(env.frames)

    press = None
    hook_result = None
    steps_after = 0
    info = {}
    acts = []
    while True:
        if actions is not None:
            if env.t >= len(actions):
                break
            a = np.asarray(actions[env.t], dtype=np.float32)
        else:
            a = pol(obs).astype(np.float32)
        acts.append(a)
        obs, r, done, info = env.step(a.astype(np.float64))
        if press is None:
            h.update(np.ascontiguousarray(a, dtype='<f4').tobytes())
            for f in env.frames[hashed:]:
                h.update(np.ascontiguousarray(f, dtype='<f8').tobytes())
            hashed = len(env.frames)
            if info['pressed'] and not info['fell']:
                press = {'ctrl_step': env.t, 'frame': len(env.frames) - 1, 'sim_time': round(env.t * CTRL_DT, 4),
                         'lever_angle': env.lever_angle(), 'proof': h.hexdigest(), 'wall_time': time.time()}
                if on_press:
                    try:
                        hook_result = on_press(press['proof'])
                    except Exception as e:  # never lose the recording because the hook failed
                        hook_result = {'mode': 'error', 'error': repr(e)}
        else:
            steps_after += 1
        if on_step:
            on_step(env, 'brain', info)
        pace()
        if press is not None and steps_after >= max_extra_steps:
            break
        if press is None and (info.get('fell') or env.t >= env.max_steps):
            break
    meta = {
        'seed': seed, 'policy': os.path.relpath(policy_path, HERE).replace('\\', '/'),
        'policy_sha256': sha_bytes(pol_bytes), 'scene_sha256': sha_bytes(scene_bytes),
        'code_sha256': {f: sha_bytes(code[f]) for f in CODE_FILES}, 'environment': environment(),
        'physics_dt': env.m.opt.timestep, 'preroll_s': preroll_s, 'preroll_frames': preroll_frames,
        'press': press, 'launch': hook_result, 'fell': bool(info.get('fell')),
    }
    return meta, np.array(env.frames), np.array(acts, dtype=np.float32)


def save(out_dir, meta, frames, acts):
    os.makedirs(out_dir, exist_ok=True)
    np.save(os.path.join(out_dir, 'qpos.npy'), frames)
    np.save(os.path.join(out_dir, 'actions.npy'), acts)
    meta['qpos_sha256'] = sha_bytes(open(os.path.join(out_dir, 'qpos.npy'), 'rb').read())
    meta['actions_sha256'] = sha_bytes(open(os.path.join(out_dir, 'actions.npy'), 'rb').read())
    tmp = os.path.join(out_dir, 'run.json.tmp')
    json.dump(meta, open(tmp, 'w'), indent=2)
    os.replace(tmp, os.path.join(out_dir, 'run.json'))
