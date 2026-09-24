"""Two-part rat brain for the cursor task: a small STEERING network drives the head (and decides when to
press); the trained LEVER-PRESS network (runs/final/policy.pt, 100% on 800 test runs) performs each press.

  steering net   observes the target cue + its own head/neck state; outputs the 4 neck actuators
                 (cervical_extend, cervical_bend, cervical_twist, atlas) and a PRESS signal.
  body           holds its standing pose (all other actuators at their rest target, as in the brain-off pre-roll)
  press program  when the steering net signals PRESS (and the lever is armed), the lever-press network takes the
                 whole body for up to 1.2 s, until the click registers; the cursor is frozen meanwhile.
Head direction relative to the body moves the cursor and a lever press is the click, exactly as in
cursor_env.CursorEnv (this wraps it, so the physics, targets, holds and click rules are the same).
"""
import os
import numpy as np

from cursor_env import CursorEnv, CUE_DIM
from env import LeverEnv
from ptload import load, NumpyPolicy

HERE = os.path.dirname(os.path.abspath(__file__))
PRESS_NET = os.path.join(HERE, 'runs', 'final', 'policy.pt')
NECK = ('cervical_extend', 'cervical_bend', 'cervical_twist', 'atlas')
PROGRAM_MAX = 60        # control steps (1.2 s) the press network gets to register the click
POST_PRESS = 40         # steps the press network keeps the body after the click: it was trained to recover and
                        # stand for 1.2 s after a press, so let it finish (without this, 6/10 pons sequences fell)
RECOVER = 15            # steps after a press before PRESS can fire again
STEER_ACT = 5
NECK_LIMIT = 1.0        # head-pitch command limit. 0.5 was needed before the brain rested during holds; with rest +
                        # reset_body it starved the last (bottom) target: 1.0 (no limit) = 11/11, no falls, 6/6 seeds


class SteerEnv:
    """Gym-like wrapper with the same (obs, reward, done, info) contract as the other envs."""

    def __init__(self, seed=0, record=False, randomize=False, press_net=PRESS_NET):
        self.e = CursorEnv(seed, record, randomize)
        self.m, self.d = self.e.m, self.e.d
        self.neck = [i for i in range(self.m.nu) if self.m.actuator(i).name in NECK]
        # limit only the head-PITCH actuators (extend, atlas): hard head-down sweeps tipped the rat over; turning
        # sideways (bend, twist) stays free, or the rat cannot make the small sideways moves small targets need
        self.neck_limit = np.array([NECK_LIMIT if self.m.actuator(i).name in ('cervical_extend', 'atlas') else 1.0
                                    for i in self.neck])
        self.press_pol = NumpyPolicy(load(press_net), 200)
        self.nu = STEER_ACT
        self.obs_dim = len(self.reset())

    # passthroughs used by the trainer/session
    def __getattr__(self, k):
        if k == 'e':
            raise AttributeError(k)
        return getattr(self.e, k)

    @property
    def difficulty(self):
        return self.e.difficulty

    @difficulty.setter
    def difficulty(self, v):
        self.e.difficulty = v

    def reset(self, seed=None, randomize=None, cursor=None):
        self.e.reset(seed=seed, randomize=randomize, cursor=cursor)
        self.program = 0
        self.post = 0
        self._clicked_prog = False
        self.recover = 0
        self.presses = 0
        return self._obs()

    def new_trial(self):
        """Between steps: the rat starts the next one from its standing pose (see CursorEnv.reset_body)."""
        self.e.reset_body()
        self.program = 0
        self.post = 0
        self._clicked_prog = False
        self.recover = 0

    def _lever_obs(self):
        """The press network's own observation (the lever task's 200-dim layout), program time as its clock."""
        e = self.e
        t = e.t
        e.t = self.program
        o = LeverEnv._obs(e)
        e.t = t
        o[-3] = 0.0; o[-1] = 0.0
        return o[:200]

    def _obs(self):
        e = self.e
        cue = e._cue() if hasattr(e, 'cursor') else np.zeros(CUE_DIM, np.float32)
        yaw, pitch = e.head_dir() if hasattr(e, 'skull') else (0.0, 0.0)
        dx, dy = e._deflection() if getattr(e, '_neutral', None) is not None else (0.0, 0.0)
        neck_ctrl = self.d.ctrl[self.neck]
        up = self.d.xmat[e.torso].reshape(3, 3)[2, 2]
        return np.concatenate([cue, [dx * 5, dy * 5], neck_ctrl, [self.program > 0, self.recover / RECOVER,
                               float(up), self.d.qpos[2] * 20]]).astype(np.float32)

    def step(self, action):
        e = self.e
        action = np.clip(np.asarray(action, dtype=np.float64), -1, 1)
        want_press = action[4] > 0.0
        if self.program == 0 and self.recover == 0 and want_press and e.armed and e.hold == 0:
            self.program = 1
            self.post = 0
            self._clicked_prog = False
            self.presses += 1
        self.last_press_obs = None
        if self.program > 0:
            self.last_press_obs = self._lever_obs()
            full = self.press_pol(self.last_press_obs).astype(np.float64)
            e._force_lock = True
        else:
            full = np.zeros(self.m.nu)
            if e.hold == 0:
                full[self.neck] = np.clip(action[:4], -self.neck_limit, self.neck_limit)
            # while the rig holds (typing, scrolling, pons busy) the brain rests: controls at their rest target, the
            # rat just stands, as in the brain-off pre-roll. Live holds are far longer than the <=3 s the steering
            # network practised, and holding its own head through them made it drift and fall.
            e._force_lock = False
        obs, r, done, info = e.step(full)
        if self.program > 0:
            self.program += 1
            if info.get('click') and self.post == 0:
                self.post = POST_PRESS if POST_PRESS > 0 else -1
            if self.post > 0:
                self.post -= 1
            if self.post == -1 or (self.post == 0 and self._clicked_prog) or self.program > PROGRAM_MAX + POST_PRESS:
                self.program = 0
                self.post = 0
                self.recover = RECOVER
                e._force_lock = False
            self._clicked_prog = self._clicked_prog or bool(info.get('click'))
        elif self.recover > 0:
            self.recover -= 1
        # shaping specific to the steering net: pressing when off target costs a little up front
        if want_press and not e.on_target() and self.program <= 2:
            r -= 0.02
        info['program'] = self.program
        return self._obs(), r, done, info
