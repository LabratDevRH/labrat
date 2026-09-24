"""Lever-press task for the DeepMind rodent in a Skinner box.

Control: 50 Hz, 10 physics substeps of 2 ms. Actions are the rat's 38 position targets in [-1, 1].
Reward (chosen, disclosed): paw-to-lever progress (potential-based), a press bonus, staying upright,
and a small smoothness cost. The rat is never told what a coin is; it is rewarded for pressing.
"""
import os
import numpy as np
import mujoco

HERE = os.path.dirname(os.path.abspath(__file__))
SCENE = os.path.join(HERE, 'assets', 'scene.xml')

CTRL_DT = 0.02
SUBSTEPS = 10
EP_SECONDS = 5.0
PRESS_ANGLE = 0.20
POST_PRESS_STEPS = 60      # 1.2 s: the film keeps rolling this long after the press, so the rat must stay up
PAWS = ('finger_L', 'finger_R')


def quat_rot(q, v):
    w, x, y, z = q
    r = np.array([[1 - 2*(y*y+z*z), 2*(x*y-w*z), 2*(x*z+w*y)],
                  [2*(x*y+w*z), 1 - 2*(x*x+z*z), 2*(y*z-w*x)],
                  [2*(x*z-w*y), 2*(y*z+w*x), 1 - 2*(x*x+y*y)]])
    return r @ v


class LeverEnv:
    def __init__(self, seed=0, record=False, randomize=False):
        self.randomize_default = randomize
        self.m = mujoco.MjModel.from_xml_path(SCENE)
        self.d = mujoco.MjData(self.m)
        self.rng = np.random.default_rng(seed)
        self.record = record
        self.nu = self.m.nu
        self.lever_q = self.m.jnt_qposadr[mujoco.mj_name2id(self.m, mujoco.mjtObj.mjOBJ_JOINT, 'lever_hinge')]
        self.tip = mujoco.mj_name2id(self.m, mujoco.mjtObj.mjOBJ_SITE, 'lever_tip')
        self.paw_sites = [mujoco.mj_name2id(self.m, mujoco.mjtObj.mjOBJ_SITE, p) for p in PAWS]
        self.torso = mujoco.mj_name2id(self.m, mujoco.mjtObj.mjOBJ_BODY, 'torso')
        self.paddle = mujoco.mj_name2id(self.m, mujoco.mjtObj.mjOBJ_GEOM, 'lever_paddle')
        def geoms_of(names):
            ids = {mujoco.mj_name2id(self.m, mujoco.mjtObj.mjOBJ_BODY, b) for b in names}
            assert -1 not in ids, names
            return {g for g in range(self.m.ngeom) if self.m.geom_bodyid[g] in ids}
        # a paw is the hand and fingers only (not the forearm)
        self.paw_geoms = geoms_of(('hand_L', 'hand_R', 'finger_L', 'finger_R'))
        # head, neck and trunk: touching the floor = fell; touching the lever = not a paw press
        # (vertebra_C* are TAIL bones and may touch the floor; vertebra_cervical_*/axis/atlant are the neck)
        trunk = ({'torso', 'pelvis', 'skull', 'jaw', 'vertebra_axis', 'vertebra_atlant'}
                 | {f'vertebra_{i}' for i in range(1, 7)} | {f'vertebra_cervical_{i}' for i in range(1, 6)})
        self.floor_forbidden = geoms_of(trunk)
        # shoulders and upper arms may not do the pressing either
        self.lever_forbidden = geoms_of(trunk | {'scapula_L', 'scapula_R', 'upper_arm_L', 'upper_arm_R'})
        self.body_geoms = self.lever_forbidden
        self.floor = mujoco.mj_name2id(self.m, mujoco.mjtObj.mjOBJ_GEOM, 'floor')
        self.lever_jnt = mujoco.mj_name2id(self.m, mujoco.mjtObj.mjOBJ_JOINT, 'lever_hinge')
        self.nominal_stiffness = float(self.m.jnt_stiffness[self.lever_jnt])
        self.nominal_friction = self.m.geom_friction[self.floor].copy()
        # paw/foot geoms have contact priority 1, so THEIR friction governs paw-floor contacts
        self.prio_geoms = np.nonzero(self.m.geom_priority > 0)[0]
        self.nominal_prio_friction = self.m.geom_friction[self.prio_geoms].copy()
        self.push = None
        self.max_steps = int(EP_SECONDS / CTRL_DT)
        self._settle()
        self.obs_dim = len(self.reset())

    def _settle(self):
        """Rest pose: let the rat settle on the floor with zero control, away from the lever."""
        mujoco.mj_resetData(self.m, self.d)
        self.d.qpos[0] = -0.04
        for _ in range(1500):
            mujoco.mj_step(self.m, self.d)
        self.rest_qpos = self.d.qpos.copy()
        self.rest_act = self.d.act.copy()
        self.rest_joints = self.rest_qpos[7:self.m.nq - 1].copy()
        self.rest_z = float(self.rest_qpos[2])

    def reset(self, seed=None, randomize=None):
        """Nominal reset (randomize=False) is the launch-run distribution and must not change:
        replay proofs depend on it. randomize=True is training-only domain randomization."""
        if seed is not None:
            self.rng = np.random.default_rng(seed)
        self.randomized = self.randomize_default if randomize is None else randomize
        m, d = self.m, self.d
        mujoco.mj_resetData(m, d)
        m.jnt_stiffness[self.lever_jnt] = self.nominal_stiffness
        m.geom_friction[self.floor] = self.nominal_friction
        m.geom_friction[self.prio_geoms] = self.nominal_prio_friction
        d.xfrc_applied[:] = 0
        self.push = None
        q = self.rest_qpos.copy()
        # start somewhere in front of the lever, slightly turned (so it learns a skill, not one clip)
        q[0] += self.rng.uniform(0.04, 0.075)   # paws start ~4-8 cm from the lever tip
        q[1] += self.rng.uniform(-0.02, 0.02)
        yaw = self.rng.uniform(-0.3, 0.3)
        q[3:7] = [np.cos(yaw/2), 0, 0, np.sin(yaw/2)]
        q[7:] += self.rng.normal(0, 0.02, len(q) - 7)
        q[self.lever_q] = 0.0
        if self.randomized:
            r = self.rng
            q[0] = self.rest_qpos[0] + r.uniform(0.02, 0.095)
            q[1] = self.rest_qpos[1] + r.uniform(-0.035, 0.035)
            yaw = r.uniform(-0.55, 0.55)
            q[3:7] = [np.cos(yaw/2), 0, 0, np.sin(yaw/2)]
            q[7:self.lever_q] = self.rest_qpos[7:self.lever_q] + r.normal(0, 0.03, self.lever_q - 7)
            m.jnt_stiffness[self.lever_jnt] = self.nominal_stiffness * r.uniform(0.7, 1.3)
            fr = r.uniform(0.7, 1.3)
            m.geom_friction[self.floor, 0] = self.nominal_friction[0] * fr
            m.geom_friction[self.prio_geoms, 0] = self.nominal_prio_friction[:, 0] * fr
        d.qpos[:] = q
        d.act[:] = self.rest_act
        mujoco.mj_forward(m, d)
        if self.randomized and self.rng.random() < 0.4:
            # like the film's brain-off pre-roll: settle for a while with the controls at zero
            d.ctrl[:] = 0
            for _ in range(int(self.rng.uniform(0.2, 1.2) / m.opt.timestep)):
                mujoco.mj_step(m, d)
        self.t = 0
        self.pressed = False
        self.press_step = None
        self.prev_action = np.zeros(self.nu)
        self.prev_pot = self._potential()
        self.frames = [d.qpos.copy()] if self.record else None
        return self._obs()

    def preroll(self, seconds):
        """Brain off: hold the controls at zero and let physics run (film pre-roll). Recorded, not rewarded."""
        return self.preroll_steps(int(round(seconds / self.m.opt.timestep)))

    def preroll_steps(self, n):
        """Brain off for n physics steps. Calling it in pieces gives exactly the same physics as one call."""
        self.d.ctrl[:] = 0
        for _ in range(n):
            mujoco.mj_step(self.m, self.d)
            if self.record:
                self.frames.append(self.d.qpos.copy())
        self.prev_pot = self._potential()
        return self._obs()

    def _paw_dist(self):
        tip = self.d.site_xpos[self.tip]
        return min(np.linalg.norm(self.d.site_xpos[s] - tip) for s in self.paw_sites)

    def _potential(self):
        return -self._paw_dist()

    def paw_on_lever(self):
        d = self.d
        for i in range(d.ncon):
            c = d.contact[i]
            g1, g2 = c.geom1, c.geom2
            if (g1 == self.paddle and g2 in self.paw_geoms) or (g2 == self.paddle and g1 in self.paw_geoms):  # noqa
                return True
        return False

    def contacts(self):
        """(paw on lever, body on floor, body on lever)"""
        paw = body_floor = body_lever = False
        for i in range(self.d.ncon):
            c = self.d.contact[i]
            g1, g2 = c.geom1, c.geom2
            for a, b in ((g1, g2), (g2, g1)):
                if a == self.paddle:
                    paw |= b in self.paw_geoms
                    body_lever |= b in self.lever_forbidden
                elif a == self.floor:
                    body_floor |= b in self.floor_forbidden
        return paw, body_floor, body_lever

    def lever_angle(self):
        return float(self.d.qpos[self.lever_q])

    def _obs(self):
        d = self.d
        q = d.qpos
        quat = q[3:7]
        inv = np.array([quat[0], -quat[1], -quat[2], -quat[3]])
        root = q[:3]
        tip_local = quat_rot(inv, d.site_xpos[self.tip] - root)
        paws_local = [quat_rot(inv, d.site_xpos[s] - root) for s in self.paw_sites]
        up = quat_rot(quat, np.array([0, 0, 1.0]))
        return np.concatenate([
            [q[2]], quat, q[7:self.lever_q], q[self.lever_q + 1:],
            np.clip(d.qvel[:], -30, 30) * 0.1,
            d.act, tip_local * 10, np.concatenate(paws_local) * 10, up,
            [self.lever_angle(), float(self.pressed), min(self.t / self.max_steps, 1.0),
             min((self.t - self.press_step) / POST_PRESS_STEPS, 1.0) if self.pressed else 0.0],
        ]).astype(np.float32)

    def step(self, action):
        m, d = self.m, self.d
        a = np.clip(action, -1, 1)
        d.ctrl[:] = a
        if self.randomized and not self.pressed:
            # occasional shove on the torso before the press (0.3-0.8 N for 0.1 s)
            if self.push is None and self.rng.random() < 0.02:
                ang = self.rng.uniform(0, 2 * np.pi)
                self.push = [np.array([np.cos(ang), np.sin(ang), 0]) * self.rng.uniform(0.3, 0.8), 5]
            if self.push is not None and self.push[1] > 0:
                d.xfrc_applied[self.torso, :3] = self.push[0]; self.push[1] -= 1
            else:
                d.xfrc_applied[self.torso, :3] = 0
        elif self.randomized:
            d.xfrc_applied[self.torso, :3] = 0
        for _ in range(SUBSTEPS):
            mujoco.mj_step(m, d)
            if self.record:
                self.frames.append(d.qpos.copy())
        self.t += 1

        pot = self._potential()
        r_approach = 20.0 * (pot - self.prev_pot)
        self.prev_pot = pot
        up = quat_rot(d.qpos[3:7], np.array([0, 0, 1.0]))[2]
        z = d.qpos[2]
        paw, body_floor, body_lever = self.contacts()
        # look like a rat: stay near the natural standing pose and height, no thrashing
        dev = (d.qpos[7:self.lever_q] - self.rest_joints) / 0.35
        r_pose = 0.15 * np.exp(-float(np.mean(dev ** 2))) * (2.0 if self.pressed else 1.0)
        r_height = 0.08 * float(np.clip((z - 0.033) / (self.rest_z - 0.033), 0, 1))
        r_energy = -2e-5 * float(np.sum(d.qvel[6:self.m.nv - 1] ** 2))
        r_smooth = -0.002 * float(np.sum((a - self.prev_action) ** 2))
        self.prev_action = a

        r_press = 0.0
        ang = self.lever_angle()
        # a press counts only with a paw on the lever, standing, and no face/belly on it
        if (not self.pressed and ang > PRESS_ANGLE and paw and not body_lever and not body_floor
                and up > 0.8 and z > 0.035):
            self.pressed = True
            self.press_step = self.t
            r_press = 50.0
        r_hold = (0.02 * min(ang / PRESS_ANGLE, 1.0) + 0.02) * paw if not self.pressed else 0.0
        r_bad = -0.05 * body_lever

        fell = body_floor or z < 0.028 or up < 0.3
        reward = r_approach + r_pose + r_height + r_energy + r_smooth + r_press + r_hold + r_bad - 10.0 * fell
        # after a press the episode always runs the full post-press window (even past max_steps)
        if self.pressed:
            done = fell or self.t >= self.press_step + POST_PRESS_STEPS
        else:
            done = fell or self.t >= self.max_steps
        info = {'pressed': self.pressed, 'fell': fell, 'paw_dist': -pot, 'paw_on_lever': paw,
                'body_on_lever': body_lever}
        return self._obs(), reward, done, info
