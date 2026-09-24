"""Cursor task: the rat drives a mouse cursor with its head and clicks with the lever.

Same Skinner box, same body and physics as env.LeverEnv (scene.xml is untouched). What is new:

  cursor   a point on a virtual screen, coordinates in [0,1]^2 (x right, y down), moved by where the
           rat's HEAD points RELATIVE TO ITS BODY: the skull's forward direction in the torso's frame (yaw and
           pitch), relative to the rest pose. Head straight = cursor still, head turned left = cursor moves left.
           Velocity control with a dead zone and a 1.5-power gain, for both large sweeps and fine adjustments.
  lock     the cursor freezes while either forepaw is within LOCK_R of the lever tip, so the reach and
           lunge of a press do not drag it.
  click    a lever press, by the same rule as the lever task (paw on the lever, past 11.5 deg, standing,
           no head/trunk on the lever or floor). The lever must be released (and the paws off it) before
           the next click counts.
  cue      the only thing the rat is told about the page: where the current target is relative to the
           cursor, and its size. Like a cue light in an operant chamber. Clicks outside the cued target
           are misses (a masked touchscreen: only the lit window responds).

Reward (chosen, disclosed): cursor progress toward the target, reaching toward the lever only once the
cursor is on the target, a click bonus inside the target and a penalty outside it, and the same
posture/height/smoothness terms as the lever task. Several targets per episode.
"""
import numpy as np
import mujoco

from env import LeverEnv, CTRL_DT, PRESS_ANGLE, quat_rot

DEAD = 0.03             # rad of head deflection before the cursor moves
GAIN = 3.0              # screen widths per second per rad^1.5 beyond the dead zone
LOCK_R = 0.025          # m: a forepaw this close to the lever tip freezes the cursor
RELEASE_ANGLE = 0.05    # rad: lever must come back up past this (and paws off it) to re-arm
TARGET_TIME = 8.0       # s per target before it times out
N_TARGETS = 4
EP_STEPS = 750          # 15 s
POST_CLICK = 25         # steps the 'just clicked' flag stays on
CUE_DIM = 11
HOLD_P = 0.7            # after a hit, how often a hold (wait) phase follows in training
HOLD_S = (0.4, 3.0)     # seconds: the live rig holds while it types a field, scrolls, or pons uploads


def sample_target(rng, difficulty=1.0, cursor=None):
    """UI-element-like rectangles: from checkbox-sized to wide buttons, anywhere on the page.
    difficulty < 1 (training curriculum): bigger targets, closer to the cursor."""
    kind = rng.random()
    if kind < 0.3:     # small (checkbox, icon)
        hw, hh = rng.uniform(0.006, 0.02), rng.uniform(0.008, 0.02)
    elif kind < 0.75:  # field / button
        hw, hh = rng.uniform(0.03, 0.2), rng.uniform(0.015, 0.04)
    else:              # large
        hw, hh = rng.uniform(0.15, 0.35), rng.uniform(0.03, 0.08)
    grow = 1.0 + 3.0 * (1.0 - difficulty)
    hw, hh = min(hw * grow, 0.3), min(hh * grow, 0.2)
    cx = rng.uniform(0.04 + hw, 0.96 - hw) if hw < 0.46 else 0.5
    cy = rng.uniform(0.04 + hh, 0.96 - hh)
    if cursor is not None and difficulty < 1.0:   # not too far from the cursor early on
        r = 0.15 + 0.85 * difficulty
        c = np.array([cx, cy]); d = c - cursor; n = np.linalg.norm(d)
        if n > r:
            c = cursor + d / n * r
        cx = float(np.clip(c[0], 0.04 + hw, 0.96 - hw)); cy = float(np.clip(c[1], 0.04 + hh, 0.96 - hh))
    return np.array([cx, cy]), np.array([hw, hh])


class CursorEnv(LeverEnv):
    def __init__(self, seed=0, record=False, randomize=False):
        self.external_target = None   # the live rig sets targets from the real page instead of sampling
        self.hold = 0
        self.difficulty = 1.0          # training curriculum (the trainer lowers it early on)
        self._neutral = None           # neutral head direction in the body frame: the settled rest pose
        super().__init__(seed, record, randomize)   # calls self.reset() once (needs self.m first)
        self.max_steps = EP_STEPS

    # ------------------------------------------------------------------ head -> cursor
    def head_dir(self):
        """Skull forward direction in the torso's frame: (yaw, pitch)."""
        f = self.d.xmat[self.skull].reshape(3, 3)[:, 0]
        R = self.d.xmat[self.torso].reshape(3, 3)
        g = R.T @ f
        return float(np.arctan2(g[1], g[0])), float(np.arctan2(g[2], np.hypot(g[0], g[1])))

    def _deflection(self):
        yaw, pitch = self.head_dir()
        return -(yaw - self._neutral[0]), -(pitch - self._neutral[1])   # head left -> cursor left; up -> up

    def paws_near_lever(self):
        tip = self.d.site_xpos[self.tip]
        return any(np.linalg.norm(self.d.site_xpos[s] - tip) < LOCK_R for s in self.paw_sites)

    # ------------------------------------------------------------------ targets
    def set_target(self, center, half):
        self.tc = np.asarray(center, float); self.th = np.asarray(half, float)
        self.hold = 0
        self.t_target = 0
        self.prev_cd = self._cursor_dist()

    def set_hold(self, steps=None):
        """No target: the rat should wait calmly (no clicks). The live rig uses this while it types or scrolls."""
        self.hold = int(steps) if steps is not None else -1   # -1 = until the rig sets a target
        self.t_target = 0

    def _new_target(self):
        if self.external_target is not None:
            c, h = self.external_target
        else:
            c, h = sample_target(self.rng, self.difficulty, getattr(self, 'cursor', None))
        self.hold = 0
        self.set_target(c, h)

    def _cursor_dist(self):
        """Distance from the cursor to the target rectangle (0 inside)."""
        dx = max(abs(self.cursor[0] - self.tc[0]) - self.th[0], 0.0)
        dy = max(abs(self.cursor[1] - self.tc[1]) - self.th[1], 0.0)
        return float(np.hypot(dx, dy))

    def on_target(self):
        return (abs(self.cursor[0] - self.tc[0]) <= self.th[0]) and (abs(self.cursor[1] - self.tc[1]) <= self.th[1])

    # ------------------------------------------------------------------ episode
    def reset(self, seed=None, randomize=None, cursor=None):
        if not hasattr(self, 'skull'):
            self.skull = mujoco.mj_name2id(self.m, mujoco.mjtObj.mjOBJ_BODY, 'skull')
        self.max_steps = EP_STEPS
        obs = super().reset(seed=seed, randomize=randomize)
        if self._neutral is None:
            q = self.d.qpos.copy()
            self.d.qpos[:] = self.rest_qpos; mujoco.mj_kinematics(self.m, self.d)
            self._neutral = self.head_dir()
            self.d.qpos[:] = q; mujoco.mj_forward(self.m, self.d)
        self.cursor = np.array(cursor if cursor is not None else self.rng.uniform(0.1, 0.9, 2), float)
        self.cursor_v = np.zeros(2)
        self.locked = False
        self.armed = True
        self.clicks = []            # (step, x, y, hit)
        self.hits = self.misses = self.timeouts = 0
        self.targets_done = 0
        self.last_click = None
        self._new_target()
        if self.external_target is None and self.rng.random() < 0.5:   # page still loading: wait first
            self.set_hold(int(self.rng.uniform(*HOLD_S) / CTRL_DT))
        self.prev_pot = self._potential()
        return self._obs()

    def reset_body(self):
        """A new trial: put the rat back in its standing start pose (same distribution as reset), lever up.
        Keeps the cursor, the clock, the counters and the recording. The live rig uses this between steps."""
        m, d = self.m, self.d
        q = self.rest_qpos.copy()
        q[0] += self.rng.uniform(0.04, 0.075)
        q[1] += self.rng.uniform(-0.02, 0.02)
        yaw = self.rng.uniform(-0.3, 0.3)
        q[3:7] = [np.cos(yaw / 2), 0, 0, np.sin(yaw / 2)]
        q[7:] += self.rng.normal(0, 0.02, len(q) - 7)
        q[self.lever_q] = 0.0
        d.qpos[:] = q
        d.qvel[:] = 0
        d.act[:] = self.rest_act
        d.ctrl[:] = 0
        mujoco.mj_forward(m, d)
        if self.record:
            self.frames.append(d.qpos.copy())
        self.armed = True
        self.locked = False
        self.prev_action = np.zeros(self.nu)
        self.prev_pot = self._potential()

    def _cue(self):
        holding = self.hold != 0
        rel = np.zeros(2) if holding else (self.tc - self.cursor) * 3.0
        size = np.zeros(2) if holding else self.th * 10
        return np.array([rel[0], rel[1], size[0], size[1],
                         self.cursor_v[0], self.cursor_v[1], float(self.locked), float(self.armed),
                         float(self.on_target() and not holding), min(self.t_target * CTRL_DT / TARGET_TIME, 1.0),
                         float(holding)], np.float32)

    def _obs(self):
        base = super()._obs()
        if not hasattr(self, 'cursor'):
            return np.concatenate([base, np.zeros(CUE_DIM, np.float32)])
        # reuse the lever task's last four features with click semantics: lever angle, just-clicked,
        # time on this target, time since the last click
        since = (self.t - self.last_click) if self.last_click is not None else None
        base[-3] = float(since is not None and since < POST_CLICK)
        base[-2] = min(self.t_target * CTRL_DT / TARGET_TIME, 1.0)
        base[-1] = min(since / POST_CLICK, 1.0) if since is not None else 0.0
        return np.concatenate([base, self._cue()])

    def step(self, action):
        m, d = self.m, self.d
        a = np.clip(action, -1, 1)
        d.ctrl[:] = a
        if self.randomized:   # occasional shove on the torso (0.3-0.8 N for 0.1 s), as in the lever task
            if self.push is None and self.rng.random() < 0.01:
                ang = self.rng.uniform(0, 2 * np.pi)
                self.push = [np.array([np.cos(ang), np.sin(ang), 0]) * self.rng.uniform(0.3, 0.8), 5]
            if self.push is not None and self.push[1] > 0:
                d.xfrc_applied[self.torso, :3] = self.push[0]; self.push[1] -= 1
            else:
                d.xfrc_applied[self.torso, :3] = 0
                self.push = None
        for _ in range(10):
            mujoco.mj_step(m, d)
            if self.record:
                self.frames.append(d.qpos.copy())
        self.t += 1
        self.t_target += 1

        # cursor
        self.locked = self.paws_near_lever() or getattr(self, '_force_lock', False)
        if self.locked:
            self.cursor_v[:] = 0
        else:
            dx, dy = self._deflection()
            f = lambda u: np.sign(u) * max(abs(u) - DEAD, 0.0) ** 1.5 * GAIN
            self.cursor_v = np.array([f(dx), f(dy)])
            self.cursor = np.clip(self.cursor + self.cursor_v * CTRL_DT, 0.0, 1.0)

        # posture terms (as in the lever task)
        up = quat_rot(d.qpos[3:7], np.array([0, 0, 1.0]))[2]
        z = d.qpos[2]
        paw, body_floor, body_lever = self.contacts()
        dev = (d.qpos[7:self.lever_q] - self.rest_joints) / 0.35
        r_pose = 0.12 * np.exp(-float(np.mean(dev ** 2)))
        r_height = 0.06 * float(np.clip((z - 0.033) / (self.rest_z - 0.033), 0, 1))
        r_energy = -2e-5 * float(np.sum(d.qvel[6:self.m.nv - 1] ** 2))
        r_smooth = -0.002 * float(np.sum((a - self.prev_action) ** 2))
        self.prev_action = a

        # cursor progress, and reaching for the lever only when the cursor is on the target
        holding = self.hold != 0
        cd = self._cursor_dist()
        r_cursor = 0.0 if holding else 60.0 * (self.prev_cd - cd)
        if not holding and cd > 0:
            # dense steering signal: cursor velocity pointing at the target
            to = self.tc - self.cursor
            n = np.linalg.norm(to)
            vn = np.linalg.norm(self.cursor_v)
            if n > 1e-6 and vn > 1e-6:
                r_cursor += 0.06 * float(np.dot(self.cursor_v / vn, to / n)) * min(vn / 0.3, 1.0)
            if n > 1e-6:
                # denser still: turning the head toward the target is rewarded before the cursor moves
                defl = np.array(self._deflection())
                r_cursor += 1.0 * float(np.clip(np.dot(defl, to / n), -0.08, 0.3))
        elif not holding:
            r_cursor += 0.03 * float(np.linalg.norm(self.cursor_v) < 0.05)   # on target: hold the cursor still
        self.prev_cd = cd
        pot = self._potential()
        on = self.on_target() and not holding
        r_reach = (20.0 * (pot - self.prev_pot)) if on else (-0.03 * float(self.locked))
        if on and self.armed:
            # on target: pushing the lever down is rewarded as it goes (the press itself pays the bonus)
            r_reach += 0.3 * float(np.clip(self.lever_angle() / PRESS_ANGLE, 0, 1)) * float(paw)
        self.prev_pot = pot

        # clicks
        ang = self.lever_angle()
        r_click = 0.0
        click = None
        if (self.armed and ang > PRESS_ANGLE and paw and not body_lever and not body_floor
                and up > 0.8 and z > 0.035):
            self.armed = False
            hit = on
            click = (self.t, float(self.cursor[0]), float(self.cursor[1]), bool(hit))
            self.clicks.append(click)
            self.last_click = self.t
            if hit:
                self.hits += 1
                r_click = 50.0
                self.targets_done += 1
                if self.external_target is None:
                    self._new_target()
                    if self.rng.random() < HOLD_P:
                        self.set_hold(int(self.rng.uniform(*HOLD_S) / CTRL_DT))
                else:
                    self.set_hold()      # live: wait until the rig sets the next target
            else:
                self.misses += 1
                r_click = -2.0 if holding else -3.0
        if not self.armed and ang < RELEASE_ANGLE and not paw:
            self.armed = True
        r_timeout = 0.0
        if self.hold > 0:
            self.hold -= 1
            if self.hold == 0:
                self.set_target(self.tc, self.th)   # the (already chosen) next target lights up
        elif self.hold == 0 and self.t_target * CTRL_DT > TARGET_TIME:
            self.timeouts += 1
            r_timeout = -3.0
            self.targets_done += 1
            self._new_target()

        fell = body_floor or z < 0.028 or up < 0.3
        reward = (r_cursor + r_reach + r_click + r_timeout + r_pose + r_height + r_energy + r_smooth
                  - 0.05 * body_lever - 20.0 * fell)
        done = fell or self.t >= self.max_steps or (self.external_target is None and self.targets_done >= N_TARGETS)
        info = {'pressed': click is not None, 'click': click, 'fell': fell, 'paw_dist': -pot,
                'paw_on_lever': paw, 'body_on_lever': body_lever, 'cursor': self.cursor.copy(),
                'on_target': on, 'hits': self.hits, 'misses': self.misses, 'timeouts': self.timeouts,
                'locked': self.locked}
        return self._obs(), reward, done, info
