"""The website's binary frame (site/js/live.js reads it), shared by live/publish_training.py (live training
frames, sent through the relay) and live/export_replay.py (the recorded RatTest replay clip).

Little-endian Float32Array, 467 floats = 1,868 bytes per frame:
  [0]   magic 7.0
  [1]   sim time in the episode (s)
  [2]   episode index
  [3]   lever angle (rad, the lever_hinge joint: 0 = up; env.PRESS_ANGLE 0.20 rad = a press)
  [4]   1 if a lever press / click registered during this frame, else 0
  [5]   cursor x (page coordinates 0..1, x right; -1 if the task has no cursor)
  [6]   cursor y (0..1, y down)
  [7]   lit target centre x (0..1; -1 if no target is lit)
  [8]   lit target centre y
  [9]   lit target half-width
  [10]  lit target half-height
  [11]  0 (reserved)
  [12 + 7*b ... 12 + 7*b + 6]  bone b, in rat.json "bones" order (= m.skin_bonebodyid):
        px, py, pz, qw, qx, qy, qz = the world pose of the bone's body (MuJoCo xpos / xquat; metres, z up).
        Skin rat.json with it by rat.json "conventions" (the same frames live/export_pose.py writes).
On a frame whose [4] is 1, [7..10] hold the target that was lit when the click registered (the env moves on to
the next target, or to a hold, in the same control step); the next frame shows the new state. In the replay
clip a click frame also shows the pose AT the click instant ([1] = that time, at most one frame earlier),
because the rig puts the rat back in its start pose right after each click (see ratest.json "resets").
"""
import json
import os

import numpy as np
import mujoco

LIVE = os.path.dirname(os.path.abspath(__file__))
RAT_JSON = os.path.join(LIVE, 'assets', 'rat.json')

MAGIC = 7.0
HEADER = 12
N_BONES = 65
BONE_FLOATS = 7
FRAME_FLOATS = HEADER + N_BONES * BONE_FLOATS          # 467
FRAME_BYTES = 4 * FRAME_FLOATS                          # 1868
FIELDS = ['magic', 'sim_t', 'episode', 'lever_angle', 'click', 'cursor_x', 'cursor_y',
          'target_cx', 'target_cy', 'target_hw', 'target_hh', 'reserved']


def rat_bone_names(path=RAT_JSON):
    with open(path) as f:
        return json.load(f)['bones']


class PoseReader:
    """World pose (65 x [px,py,pz,qw,qx,qy,qz]) of the skin bones for a qpos, computed exactly with
    mj_kinematics on a scratch MjData. (After mj_step, env.d.xpos still holds the kinematics of the state
    before the last physics substep, and this never touches the env's own data.)"""

    def __init__(self, m, bone_names=None):
        names = bone_names if bone_names is not None else rat_bone_names()
        ids = [mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, n) for n in names]
        assert -1 not in ids, 'a rat.json bone is not a body of this model'
        assert ids == [int(i) for i in m.skin_bonebodyid], 'rat.json bone order != this model\'s skin bones'
        assert len(ids) == N_BONES, len(ids)
        self.m = m
        self.d = mujoco.MjData(m)
        self.ids = np.asarray(ids, dtype=np.int64)

    def __call__(self, qpos):
        self.d.qpos[:] = qpos
        mujoco.mj_kinematics(self.m, self.d)
        return np.concatenate([self.d.xpos[self.ids], self.d.xquat[self.ids]], axis=1)


def pack(sim_t, episode, lever_angle, click, cursor=None, target=None, pose=None):
    """One frame as bytes. cursor: (x, y) or None; target: (cx, cy, hw, hh) or None; pose: (65, 7)."""
    f = np.zeros(FRAME_FLOATS, dtype='<f4')
    f[0] = MAGIC
    f[1] = sim_t
    f[2] = episode
    f[3] = lever_angle
    f[4] = 1.0 if click else 0.0
    f[5:7] = cursor if cursor is not None else -1.0
    f[7:11] = target if target is not None else -1.0
    f[11] = 0.0
    f[HEADER:] = np.asarray(pose, dtype=np.float64).reshape(-1)
    return f.tobytes()


def unpack(buf):
    """bytes -> dict of the header fields + 'pose' (65, 7) float32 (for tests and tools)."""
    assert len(buf) == FRAME_BYTES, f'frame is {len(buf)} bytes, expected {FRAME_BYTES}'
    f = np.frombuffer(buf, dtype='<f4')
    out = {k: float(f[i]) for i, k in enumerate(FIELDS)}
    out['pose'] = f[HEADER:].reshape(N_BONES, BONE_FLOATS)
    return out


def quat_up_z(q):
    """z component of the body's local z axis in the world, for (..., 4) w,x,y,z quaternions."""
    q = np.asarray(q, dtype=np.float64)
    return 1.0 - 2.0 * (q[..., 1] ** 2 + q[..., 2] ** 2)


def check_frame(fr, lever_range=(-0.02, 0.5), torso=0, limit_slack=0.1):
    """Contract checks on one unpacked frame; returns a list of problems (empty = fine).
    MuJoCo joint limits are soft: a hard press can push the lever a little past its 0.5 rad limit (the
    RatTest recording peaks at 0.576 rad), so the lever range gets limit_slack rad of slack."""
    bad = []
    if fr['magic'] != MAGIC:
        bad.append(f"magic {fr['magic']}")
    if fr['reserved'] != 0.0:
        bad.append('reserved field not 0')
    if fr['click'] not in (0.0, 1.0):
        bad.append(f"click flag {fr['click']}")
    if not (lever_range[0] - limit_slack <= fr['lever_angle'] <= lever_range[1] + limit_slack):
        bad.append(f"lever angle {fr['lever_angle']:.4f} outside {lever_range} (+-{limit_slack})")
    for k in ('cursor_x', 'cursor_y'):
        if fr[k] != -1.0 and not 0.0 <= fr[k] <= 1.0:
            bad.append(f'{k} {fr[k]}')
    tgt = [fr[k] for k in ('target_cx', 'target_cy', 'target_hw', 'target_hh')]
    if not (all(v == -1.0 for v in tgt) or all(0.0 <= v <= 1.0 for v in tgt)):
        bad.append(f'target {tgt}')
    p = fr['pose']
    if not np.all(np.isfinite(p)):
        bad.append('non-finite pose')
    qn = np.linalg.norm(p[:, 3:7], axis=1)
    if np.abs(qn - 1.0).max() > 1e-3:
        bad.append(f'quaternion norm off by {np.abs(qn - 1.0).max():.2e}')
    if not 0.0 < p[torso, 2] < 0.2:
        bad.append(f'torso z {p[torso, 2]:.4f} m')
    return bad
