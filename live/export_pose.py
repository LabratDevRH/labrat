"""Export the standing pose the live brain rig starts from, for the rotating display model (rat3d.js).

    python live/export_pose.py [--seed 2026] [--preroll 2.0]

Builds the start state exactly as session.Session.run does (read-only use of session.py / steer_env.py):
    env = SteerEnv(seed) ; inner.external_target = PLACEHOLDER ; env.reset(seed=seed, cursor=(0.5, 0.5))
    inner.set_hold() ; then the brain-off pre-roll: inner.preroll_steps() in CTRL_DT chunks for preroll_s seconds
and writes live/assets/rat_pose.json:
    {format, source{...}, seed, preroll_s, cursor0, sim_t, bones[nb], xpos[nb][3], xquat[nb][4]}
xpos / xquat are env.d.xpos / env.d.xquat (world frame, quat w,x,y,z) of each skinned body, in rat.json's bone
order (= m.skin_bonebodyid), i.e. the same frames the live pose stream carries and export_assets.py skins with.

It is a DISPLAY pose (one frame, the rat standing at the end of the pre-roll), not a live view.
The script then skins rat.json with this pose using export_assets.py's own maths and prints the bounding box,
checking that the rat is upright (feet on the floor, head up and forward along +x).
"""
import argparse, hashlib, json, os, sys

import numpy as np

LIVE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(LIVE)
sys.path.insert(0, ROOT)
sys.path.insert(0, LIVE)

import mujoco  # noqa: E402

from env import SCENE, CTRL_DT  # noqa: E402
from steer_env import SteerEnv  # noqa: E402
from session import PLACEHOLDER  # noqa: E402
from export_assets import skin_from_json, r6  # noqa: E402

ASSETS = os.path.join(LIVE, 'assets')


def sha256(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def start_pose(seed, preroll_s, cursor=(0.5, 0.5)):
    """The live rig's start state (session.Session.run up to the end of the brain-off pre-roll)."""
    env = SteerEnv(seed)
    inner = env.e
    inner.external_target = PLACEHOLDER
    env.reset(seed=seed, cursor=cursor)
    inner.set_hold()
    n_pre = int(round(preroll_s / inner.m.opt.timestep))
    per = int(round(CTRL_DT / inner.m.opt.timestep))
    done_n = 0
    while done_n < n_pre:          # brain off: controls at zero
        k = min(per, n_pre - done_n)
        inner.preroll_steps(k)
        done_n += k
    return env, inner


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seed', type=int, default=2026)
    ap.add_argument('--preroll', type=float, default=2.0)
    a = ap.parse_args()

    with open(os.path.join(ASSETS, 'rat.json')) as f:
        rat = json.load(f)
    env, inner = start_pose(a.seed, a.preroll)
    m, d = inner.m, inner.d
    bodies = [mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, n) for n in rat['bones']]
    assert bodies == [int(i) for i in m.skin_bonebodyid], 'rat.json bone order != m.skin_bonebodyid'
    scene_sha = sha256(SCENE)
    if rat['source'].get('scene_sha256') and rat['source']['scene_sha256'] != scene_sha:
        print('WARNING: scene.xml changed since rat.json was exported')

    xpos = d.xpos[bodies].copy()
    xquat = d.xquat[bodies].copy()
    pose = {
        'format': 'ratbrain-rat-pose-1',
        'what': 'display pose: the simulated rodent standing at the end of the brain-off pre-roll (not live)',
        'source': {'script': 'live/export_pose.py', 'rat_json_format': rat['format'],
                   'scene_sha256': scene_sha, 'method': 'steer_env.SteerEnv + session.Session.run pre-roll'},
        'seed': a.seed, 'preroll_s': a.preroll, 'cursor0': [0.5, 0.5], 'sim_t': round(float(d.time), 6),
        'conventions': {'quat': 'w,x,y,z', 'frame': 'world, metres, z up; env.d.xpos / env.d.xquat of each bone body'},
        'bones': rat['bones'],
        'xpos': [r6(p) for p in xpos],
        'xquat': [r6(q) for q in xquat],
    }
    out = os.path.join(ASSETS, 'rat_pose.json')
    with open(out, 'w', newline='\n') as f:
        json.dump(pose, f, separators=(',', ':'))
    print(f'rat_pose.json: {len(bodies)} bones, seed {a.seed}, pre-roll {a.preroll} s, sim t {d.time:.3f} s, '
          f'{os.path.getsize(out):,} bytes')

    # ---- sanity check: skin with the ROUNDED pose read back from disk, export_assets.py maths
    with open(out) as f:
        back = json.load(f)
    v = skin_from_json(rat, np.asarray(back['xpos']), np.asarray(back['xquat']))
    lo, hi = v.min(0), v.max(0)
    print(f'  skinned bbox min {np.round(lo, 4).tolist()}  max {np.round(hi, 4).tolist()}  '
          f'size {np.round(hi - lo, 4).tolist()} m')
    # compare with MuJoCo's own skin of the live state
    scn = mujoco.MjvScene(m, 2000)
    mujoco.mjv_updateScene(m, d, mujoco.MjvOption(), None, mujoco.MjvCamera(), mujoco.mjtCatBit.mjCAT_ALL, scn)
    mj_v = np.asarray(scn.skinvert).reshape(-1, 3)[:m.skin_vertnum[0]]
    err = float(np.abs(v - mj_v).max())
    print(f'  max |v_pose_json - v_mujoco| = {err:.3e} m')
    assert err < 1e-4, 'skinned display pose does not match MuJoCo'

    names = rat['bones']
    sk = names.index('skull')
    W = np.zeros((len(v), len(names)))
    for b, flat in enumerate(rat['weights']):
        n = len(flat) // 2
        W[np.asarray(flat[:n], int), b] = flat[n:]
    W /= W.sum(1, keepdims=True)
    feet = [i for i, n in enumerate(names) if n.startswith(('foot', 'toe'))]
    tail = [i for i, n in enumerate(names) if n.startswith('vertebra_C')]
    foot_v = v[W[:, feet].sum(1) > 0.5]
    head_v = v[W[:, sk] > 0.5]
    body_v = v[W[:, tail].sum(1) < 0.5]
    floor_z = 0.0
    print(f'  feet z min {foot_v[:, 2].min():.4f} m (floor z {floor_z}), head centre '
          f'{np.round(head_v.mean(0), 4).tolist()}, torso {np.round(xpos[names.index("torso")], 4).tolist()}')
    print(f'  body without tail: bbox min {np.round(body_v.min(0), 4).tolist()} max {np.round(body_v.max(0), 4).tolist()}')
    skull_fwd = np.asarray(d.xmat[bodies[sk]]).reshape(3, 3)[:, 0]
    print(f'  skull forward axis (world) {np.round(skull_fwd, 3).tolist()}')
    assert abs(foot_v[:, 2].min() - floor_z) < 0.01, 'feet are not on the floor'
    assert head_v[:, 2].mean() > foot_v[:, 2].mean() + 0.01, 'head is not above the feet'
    assert head_v[:, 0].mean() > xpos[names.index('pelvis')][0], 'head is not forward of the pelvis (+x)'
    print('  upright: OK')


if __name__ == '__main__':
    main()
