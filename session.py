"""A live cursor session: the rat's trained brain steers a cursor with its head and clicks with the lever.
The brain is two trained networks: a steering network (head direction + when to press) and the lever-press network.

The rig drives it with COMMANDS, each applied at the start of a control step and recorded with that step:
    ('target', cx, cy, hw, hh)   light the next target (page coordinates in [0,1], half-sizes)
    ('hold',)                    between steps: a new trial (the rat is put back in its standing start pose and rests,
                                 controls at zero, while the rig types, scrolls, or pons is busy)
Everything else (every head movement, every reach, every click) comes from the policy.

SESSION PROOF = sha256 over, in order:
    scene.xml, env.py, cursor_env.py, session.py, ptload.py (text, CRLF normalised to LF), the policy bytes,
    'seed=<seed>;preroll=<s>;cursor=<x>,<y>', the reset frame and pre-roll frames, then for every control step:
    the command applied at that step (canonical JSON, or 'none'), the float32 action, and its 10 physics frames.
BRAIN COMMIT = sha256(policy bytes + the four code files + scene): known before the session starts, so it can be
    typed into the coin's description; the full session proof is published with the recording afterwards.
`python replay_session.py <dir>` feeds the recorded commands back and must reproduce every frame bit for bit.
"""
import hashlib, io, json, os, platform, queue, sys, threading, time
import numpy as np
import mujoco

from env import SCENE, CTRL_DT
from steer_env import SteerEnv, PRESS_NET
from ptload import load, NumpyPolicy

HERE = os.path.dirname(os.path.abspath(__file__))
CODE_FILES = ('env.py', 'cursor_env.py', 'steer_env.py', 'session.py', 'ptload.py')
PLACEHOLDER = (np.array([0.5, 0.5]), np.array([0.01, 0.01]))


def text_bytes(path):
    return open(path, 'rb').read().replace(b'\r\n', b'\n')


def sha(b):
    return hashlib.sha256(b).hexdigest()


def brain_commit(pol_bytes, scene_bytes, code, press_bytes=b''):
    h = hashlib.sha256()
    h.update(scene_bytes); h.update(pol_bytes); h.update(press_bytes)
    for f in CODE_FILES:
        h.update(code[f])
    return h.hexdigest()


def canon(cmd):
    return json.dumps(cmd, separators=(',', ':')) if cmd is not None else 'none'


def activations(pol, obs):
    """Display only: the hidden-layer and output activations for this observation (same maths as NumpyPolicy)."""
    x = np.clip((obs - pol.mean) / pol.sd, -10, 10).astype(np.float32)
    acts = [x]
    for i, (w, b) in enumerate(pol.layers):
        x = x @ w.T + b
        if i < 3:
            x = np.where(x > 0, x, np.expm1(np.minimum(x, 0)))
        acts.append(x)
    return acts    # [input 211, h1 512, h2 512, h3 256, motor 38]


class Session:
    def __init__(self, policy_path, seed, preroll_s=2.0, cursor=(0.5, 0.5), press_path=PRESS_NET):
        """policy_path: the STEERING network; press_path: the lever-PRESS network. Together: the rat's brain."""
        self.pol_bytes = open(policy_path, 'rb').read()
        self.press_bytes = open(press_path, 'rb').read()
        self.scene_bytes = text_bytes(SCENE)
        self.code = {f: text_bytes(os.path.join(HERE, f)) for f in CODE_FILES}
        self.commit = brain_commit(self.pol_bytes, self.scene_bytes, self.code, self.press_bytes)
        self.policy_path = policy_path
        self.press_path = press_path
        self.seed, self.preroll_s, self.cursor0 = seed, preroll_s, tuple(float(c) for c in cursor)
        self.cmds = queue.Queue()
        self.log = []                  # (step, command)
        self.clicks = []               # (step, x, y, hit)
        self.acts = []
        self.stop_flag = threading.Event()
        self.proof = None

    # rig side -----------------------------------------------------------
    def target(self, cx, cy, hw, hh):
        self.cmds.put(('target', round(float(cx), 6), round(float(cy), 6), round(float(hw), 6), round(float(hh), 6)))

    def hold(self):
        self.cmds.put(('hold',))

    def stop(self):
        self.stop_flag.set()

    # sim side (run in a worker thread) ---------------------------------------
    def run(self, on_step=None, on_click=None, realtime=True, commands=None, max_steps=30000):
        """commands: optional recorded [(step, cmd), ...] to replay instead of reading the queue."""
        env = SteerEnv(self.seed, record=True, press_net=io.BytesIO(self.press_bytes))
        inner = env.e                      # the CursorEnv underneath (targets, holds, frames)
        pol = NumpyPolicy(load(io.BytesIO(self.pol_bytes)), env.obs_dim)
        self.pol = pol
        inner.external_target = PLACEHOLDER
        obs = env.reset(seed=self.seed, cursor=self.cursor0)
        inner.set_hold()
        h = hashlib.sha256()
        h.update(self.scene_bytes); h.update(self.pol_bytes); h.update(self.press_bytes)
        for f in CODE_FILES:
            h.update(self.code[f])
        h.update(f'seed={self.seed};preroll={self.preroll_s!r};cursor={self.cursor0[0]!r},{self.cursor0[1]!r}'.encode())
        t0 = time.perf_counter()
        tick = 0

        def pace():
            nonlocal tick
            tick += 1
            if realtime:
                dt = t0 + tick * CTRL_DT - time.perf_counter()
                if dt > 0:
                    time.sleep(dt)

        n_pre = int(round(self.preroll_s / inner.m.opt.timestep))
        per = int(round(CTRL_DT / inner.m.opt.timestep))
        done_n = 0
        while done_n < n_pre:          # brain off: controls at zero
            k = min(per, n_pre - done_n)
            inner.preroll_steps(k); done_n += k
            if on_step:
                on_step(inner, 'preroll', None, None)
            pace()
        obs = env._obs()
        for f in inner.frames:
            h.update(np.ascontiguousarray(f, dtype='<f8').tobytes())
        hashed = len(inner.frames)
        rec = dict(commands or [])
        info = {}
        while not self.stop_flag.is_set() and inner.t < max_steps:
            # 1. the rig's command for this step
            if commands is not None:
                cmd = rec.get(inner.t)
            else:
                cmd = None
                try:
                    cmd = self.cmds.get_nowait()
                except queue.Empty:
                    pass
            if cmd is not None:
                cmd = tuple(cmd)
                if cmd[0] == 'target':
                    inner.external_target = (np.array(cmd[1:3]), np.array(cmd[3:5]))
                    inner.set_target(*inner.external_target)
                elif cmd[0] == 'hold':
                    env.new_trial()        # between steps: a new trial, the rat settles in its standing pose
                    inner.set_hold()
                self.log.append((inner.t, list(cmd)))
            h.update(canon(list(cmd) if cmd else None).encode())
            # 2. the brain
            a = pol(obs).astype(np.float32)
            self.acts.append(a)
            h.update(np.ascontiguousarray(a, dtype='<f4').tobytes())
            obs, r, done, info = env.step(a.astype(np.float64))
            for f in inner.frames[hashed:]:
                h.update(np.ascontiguousarray(f, dtype='<f8').tobytes())
            hashed = len(inner.frames)
            if info.get('click'):
                self.clicks.append(list(info['click']))
                if on_click:
                    on_click(info['click'], inner)
            if on_step:
                on_step(inner, 'brain', info, obs)
            if info.get('fell'):
                break
            pace()
        self.proof = h.hexdigest()
        self.env = inner
        self.steer_env = env
        return self.proof, info

    def save(self, out_dir, extra=None):
        os.makedirs(out_dir, exist_ok=True)
        frames = np.array(self.env.frames)
        np.save(os.path.join(out_dir, 'qpos.npy'), frames)
        np.save(os.path.join(out_dir, 'actions.npy'), np.array(self.acts, dtype=np.float32))
        meta = {
            'kind': 'cursor_session', 'seed': self.seed, 'preroll_s': self.preroll_s, 'cursor0': self.cursor0,
            'policy': os.path.relpath(self.policy_path, HERE).replace('\\', '/'), 'policy_sha256': sha(self.pol_bytes),
            'press_policy': os.path.relpath(self.press_path, HERE).replace(os.sep, '/'), 'press_sha256': sha(self.press_bytes),
            'scene_sha256': sha(self.scene_bytes), 'code_sha256': {f: sha(self.code[f]) for f in CODE_FILES},
            'brain_commit': self.commit, 'session_proof': self.proof, 'steps': self.env.t,
            'commands': self.log, 'clicks': self.clicks,
            'environment': {'python': sys.version.split()[0], 'numpy': np.__version__, 'mujoco': mujoco.__version__,
                            'platform': platform.platform()},
            'qpos_sha256': sha(open(os.path.join(out_dir, 'qpos.npy'), 'rb').read()),
        }
        meta.update(extra or {})
        tmp = os.path.join(out_dir, 'session.json.tmp')
        json.dump(meta, open(tmp, 'w'), indent=1)
        os.replace(tmp, os.path.join(out_dir, 'session.json'))
        return meta


def replay(out_dir):
    """Re-run a saved session from its recorded commands; returns (ok, details)."""
    meta = json.load(open(os.path.join(out_dir, 'session.json')))
    s = Session(os.path.join(HERE, meta['policy']), meta['seed'], meta['preroll_s'], tuple(meta['cursor0']),
                os.path.join(HERE, meta.get('press_policy', 'runs/final/policy.pt')))
    proof, _ = s.run(realtime=False, commands=[(st, tuple(c)) for st, c in meta['commands']], max_steps=meta['steps'])
    rec = np.load(os.path.join(out_dir, 'qpos.npy'))
    fr = np.array(s.env.frames)
    same = rec.shape == fr.shape and float(np.abs(rec - fr).max()) == 0.0
    ok = same and proof == meta['session_proof'] and s.commit == meta['brain_commit'] and s.clicks == meta['clicks']
    return ok, {'proof': proof, 'recorded': meta['session_proof'], 'frames_identical': same,
                'commit_ok': s.commit == meta['brain_commit'], 'clicks_ok': s.clicks == meta['clicks']}
