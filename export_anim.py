"""Export a recorded run for Blender.

Writes <run>/blender/:
  rat_skin.npz   skin mesh (bind pose), faces, per-bone bind pose + vertex weights (MuJoCo skin = linear blend skinning)
  anim.npz       per output frame: world pose of every skinned body, lever angle, source physics frame, flags
The time map plays the recording at real time, then drops into slow motion around the press.

    python export_anim.py runs/launch_live   [--fps 30] [--slowmo 8]
"""
import argparse, json, os
import numpy as np
import mujoco

from env import SCENE

HERE = os.path.dirname(os.path.abspath(__file__))
PHYS_HZ = 500  # 2 ms physics step; one recorded qpos per step


def quat_mul(a, b):
    w1, x1, y1, z1 = a; w2, x2, y2, z2 = b
    return np.array([w1*w2 - x1*x2 - y1*y2 - z1*z2, w1*x2 + x1*w2 + y1*z2 - z1*y2,
                     w1*y2 - x1*z2 + y1*w2 + z1*x2, w1*z2 + x1*y2 - y1*x2 + z1*w2])


def quat_mat(q):
    m = np.zeros(9); mujoco.mju_quat2Mat(m, q); return m.reshape(3, 3)


def skin_lbs(m, d):
    """Our own linear-blend-skinning, checked against MuJoCo's renderer below."""
    V = np.zeros((m.skin_vertnum[0], 3))
    W = np.zeros(m.skin_vertnum[0])
    vert = m.skin_vert
    for b in range(m.skin_bonenum[0]):
        body = m.skin_bonebodyid[b]
        q = quat_mul(d.xquat[body], m.skin_bonebindquat[b] * np.array([1, -1, -1, -1]))
        R = quat_mat(q)
        t = d.xpos[body] - R @ m.skin_bonebindpos[b]
        a, n = m.skin_bonevertadr[b], m.skin_bonevertnum[b]
        ids = m.skin_bonevertid[a:a + n]; w = m.skin_bonevertweight[a:a + n]
        V[ids] += w[:, None] * (vert[ids] @ R.T + t)
        W[ids] += w
    return V / W[:, None]


def time_map(n_phys, press_frame, fps, slowmo, pre_s=1.5, slow_before_s=0.12, slow_after_s=0.25):
    """Physics frame index for every output frame: real time, then slow motion around the press."""
    step = PHYS_HZ / fps
    out, flags = [], []
    start = max(0, press_frame - int(pre_s * PHYS_HZ))
    s0 = max(start, press_frame - int(slow_before_s * PHYS_HZ))
    s1 = min(n_phys - 1, press_frame + int(slow_after_s * PHYS_HZ))
    f = float(start)
    while f < n_phys - 1:
        slow = s0 <= f <= s1
        out.append(int(round(f))); flags.append(1 if slow else 0)
        f += step / slowmo if slow else step
    return np.array(out), np.array(flags)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('run'); ap.add_argument('--fps', type=int, default=30); ap.add_argument('--slowmo', type=float, default=8)
    a = ap.parse_args()
    run_dir = os.path.join(HERE, a.run) if not os.path.isabs(a.run) else a.run
    meta = json.load(open(os.path.join(run_dir, 'run.json')))
    qpos = np.load(os.path.join(run_dir, 'qpos.npy'))
    m = mujoco.MjModel.from_xml_path(SCENE); d = mujoco.MjData(m)

    # sanity: our skinning == MuJoCo's skinning at a mid-run frame
    d.qpos[:] = qpos[len(qpos) // 2]; mujoco.mj_forward(m, d)
    scn = mujoco.MjvScene(m, 2000); opt = mujoco.MjvOption(); cam = mujoco.MjvCamera()
    mujoco.mjv_updateScene(m, d, opt, None, cam, mujoco.mjtCatBit.mjCAT_ALL, scn)
    err = np.abs(skin_lbs(m, d) - np.asarray(scn.skinvert).reshape(-1, 3)).max()
    print('skin check max err (m):', err)
    assert err < 1e-5, 'skinning mismatch'

    out = os.path.join(run_dir, 'blender'); os.makedirs(out, exist_ok=True)
    bones = list(m.skin_bonebodyid)
    weights = []
    for b in range(m.skin_bonenum[0]):
        a0, n = m.skin_bonevertadr[b], m.skin_bonevertnum[b]
        weights.append(np.stack([m.skin_bonevertid[a0:a0 + n].astype(np.float64), m.skin_bonevertweight[a0:a0 + n]], 1))
    np.savez_compressed(os.path.join(out, 'rat_skin.npz'), verts=m.skin_vert, faces=m.skin_face,
                        bone_names=np.array([m.body(i).name for i in bones]),
                        bindpos=m.skin_bonebindpos, bindquat=m.skin_bonebindquat,
                        weights=np.array(weights, dtype=object), allow_pickle=True)

    if not meta.get('press'):
        raise SystemExit('this run has no press; there is nothing to film (the film never invents a press)')
    press = meta['press']['frame']
    idx, slow = time_map(len(qpos), press, a.fps, a.slowmo)
    lever_q = m.nq - 1
    pos = np.zeros((len(idx), len(bones), 3)); quat = np.zeros((len(idx), len(bones), 4))
    lever = np.zeros(len(idx)); pressed = np.zeros(len(idx), bool); paw = np.zeros(len(idx))
    tip = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_SITE, 'lever_tip')
    paws = [mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_SITE, s) for s in ('finger_L', 'finger_R')]
    for k, i in enumerate(idx):
        d.qpos[:] = qpos[i]; mujoco.mj_kinematics(m, d)
        pos[k] = d.xpos[bones]; quat[k] = d.xquat[bones]
        lever[k] = qpos[i][lever_q]; pressed[k] = i >= press
        paw[k] = min(np.linalg.norm(d.site_xpos[p] - d.site_xpos[tip]) for p in paws)
    np.savez_compressed(os.path.join(out, 'anim.npz'), pos=pos, quat=quat, lever=lever, phys=idx, slow=slow, paw=paw, sim_t=idx / PHYS_HZ,
                        pressed=pressed, brain_on=idx > meta.get('preroll_frames', -1), fps=a.fps, slowmo=a.slowmo, press_out_frame=int(np.argmax(pressed)))
    json.dump({'frames': len(idx), 'fps': a.fps, 'press_out_frame': int(np.argmax(pressed)),
               'seconds': len(idx) / a.fps}, open(os.path.join(out, 'anim.json'), 'w'), indent=2)
    print('frames', len(idx), 'seconds', round(len(idx) / a.fps, 2), 'press at out frame', int(np.argmax(pressed)))


if __name__ == '__main__':
    main()
