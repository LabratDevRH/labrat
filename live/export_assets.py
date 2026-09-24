"""Export the live rig's static assets (SPEC.md, "Assets").

    python live/export_assets.py [--run runs/film_2026] [--check-only]

Writes
  live/assets/rat.json   the MuJoCo skin of DeepMind's rodent for rig.html: rest mesh (metres), faces,
                         per-bone bind pose, ALL skin influences, coat colour + fur amount (same masks as
                         blender/film.py), rest normals, and the chamber constants from build_scene.py
  live/assets/coin.png   1000x1000 coin image: a square crop of the top-view film frame (hooded rat at the lever)

then checks rat.json (read back from disk, i.e. after rounding) against MuJoCo's own skinning on recorded
frames of the run: v' = sum_b w_b (R_b R_bind_b^T (v - p_bind_b) + x_b) / sum_b w_b must match
mujoco.mjv_updateScene's skinvert to < 1e-5 m, or the script fails.

rat.json layout (all floats rounded to 6 significant digits):
  verts[3*nv], faces[3*nf], rest_normals[3*nv], coat[3*nv] (linear RGB), fur[nv], hood[nv]
  weld[nv]                  canonical index of each vertex's seam group (UV-seam duplicates share position
                            AND weights); accumulate face normals into weld[i] each frame and read them back
                            through weld, or fur shells crack along the seams. rest_normals are welded.
  bones[nb]                 body names, in the order of m.skin_bonebodyid (= the rig's pose-frame order)
  bind_pos[nb][3]           skin bind position of each bone, world frame, metres
  bind_quat[nb][4]          skin bind orientation, w,x,y,z (MuJoCo order, same as the pose stream)
  weights[nb]               [vertex_id_0..vertex_id_{n-1}, weight_0..weight_{n-1}]  (n = len/2)
  chamber{...}              WALL_X LEVER_Z LEVER_LEN SIDE_Y BACK_X WALL_H HINGE_AXIS (+ lever extras)
  face{...}                 skull bone index, eye offset in skull frame, nose-tip vertex (for whiskers)
"""
import argparse, hashlib, json, os, sys

import numpy as np

LIVE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(LIVE)
sys.path.insert(0, ROOT)

import mujoco  # noqa: E402

import build_scene  # noqa: E402  (constants only; build() is not called)
from env import SCENE, PRESS_ANGLE  # noqa: E402

ASSETS = os.path.join(LIVE, 'assets')
COIN_SRC = os.path.join('runs', 'film_2026', 'test_top', 'f_0062.png')
# square crop of the 1920x1080 top view (x0, y0, side): hood, head, whiskers, shoulders, lever housing, cue light
COIN_BOX = (680, 0, 1000)
COIN_SIZE = 1000
COIN_MAX_BYTES = 2_000_000

# coat colours and masks: exactly blender/film.py build_rat() (film scale S=10 -> metres here)
CREAM = np.array([0.82, 0.78, 0.7])
DARK = np.array([0.035, 0.028, 0.025])
PINK = np.array([0.88, 0.55, 0.52])
HOOD_PREFIXES = ['skull', 'jaw', 'vertebra_cervical', 'vertebra_axis', 'vertebra_atlant']
BARE_PREFIXES = ['hand', 'finger', 'foot', 'toe']
TAIL_BONES = [f'vertebra_C{i}' for i in range(3, 31)]
NOSE_TIP_M = 0.005          # film.py: 0.05 Blender units at S=10
EYE_OFFSET = [0.0011, 0.0128, 0.0025]   # skull frame; the right eye is at -y
EYE_RADIUS = 0.0034         # film.py: 0.034 Blender units


def sha256(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def r6(a):
    """Flat list of floats rounded to 6 significant digits."""
    return [float(f'{x:.6g}') for x in np.asarray(a, dtype=np.float64).ravel()]


def dense_weights(weights, nv):
    W = np.zeros((nv, len(weights)))
    for b, wv in enumerate(weights):
        W[wv[:, 0].astype(int), b] = wv[:, 1]
    return W


def area_normals(verts, faces, weld=None, fallback=True):
    """Area-weighted vertex normals (sum of un-normalised face cross products, then normalised).

    weld: per-vertex canonical index; seam duplicates (same rest position, same weights) then share one
    normal, so fur shells don't crack along UV seams. fallback: a vertex whose face normals cancel exactly
    (the rest mesh has one, in a double-sided sliver on the head) takes its neighbours' mean normal.
    """
    v = np.asarray(verts, dtype=np.float64)
    f = np.asarray(faces, dtype=np.int64)
    fn = np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]])
    g = f if weld is None else np.asarray(weld)[f]
    n = np.zeros_like(v)
    for k in range(3):
        np.add.at(n, g[:, k], fn)
    if weld is not None:
        n = n[np.asarray(weld)]
    ln = np.linalg.norm(n, axis=1)
    out = n / np.maximum(ln, 1e-30)[:, None]
    bad = np.nonzero(ln < 1e-15)[0]
    if fallback:
        for i in bad:
            nb = np.setdiff1d(f[(f == i).any(1)].ravel(), bad)
            s = out[nb].sum(0) if len(nb) else np.zeros(3)
            out[i] = s / np.linalg.norm(s) if np.linalg.norm(s) > 0 else (0.0, 0.0, 1.0)
    return out, bad


def weld_map(verts, W):
    """Canonical (lowest) index of the vertices sharing each vertex's exact rest position."""
    _, inv = np.unique(np.asarray(verts, dtype=np.float32), axis=0, return_inverse=True)
    inv = inv.ravel()
    canon = np.full(inv.max() + 1, -1)
    for i, k in enumerate(inv):
        if canon[k] < 0:
            canon[k] = i
    weld = canon[inv]
    # duplicates must also carry identical skin weights, or they would separate when posed
    assert np.array_equal(W, W[weld]), 'seam duplicates with different skin weights'
    return weld


def coat_and_fur(verts, names, W):
    def share(prefixes):
        cols = [i for i, n in enumerate(names) if any(n.startswith(p) for p in prefixes)]
        return W[:, cols].sum(1)
    hood = share(HOOD_PREFIXES)
    bare = share(BARE_PREFIXES) + share(TAIL_BONES)
    x = verts[:, 0]
    snout = np.clip((x - (x.max() - NOSE_TIP_M)) / NOSE_TIP_M, 0, 1)
    fur = np.clip(1.0 - bare - snout, 0, 1)
    fur_dark = np.clip(hood, 0, 1) * fur
    bare_w = np.clip(bare + snout, 0, 1)
    col = CREAM * (1 - bare_w)[:, None] + PINK * bare_w[:, None]
    col = col * (1 - fur_dark)[:, None] + DARK * fur_dark[:, None]
    return col, fur, fur_dark


def chamber(m):
    """Chamber constants from build_scene.py, cross-checked against the compiled scene."""
    c = {'WALL_X': build_scene.WALL_X, 'LEVER_Z': build_scene.LEVER_Z, 'LEVER_LEN': build_scene.LEVER_LEN,
         'SIDE_Y': build_scene.SIDE_Y, 'BACK_X': build_scene.BACK_X, 'WALL_H': build_scene.WALL_H}
    spec = {'WALL_X': 0.175, 'LEVER_Z': 0.032, 'LEVER_LEN': 0.045, 'SIDE_Y': 0.20, 'BACK_X': -0.36, 'WALL_H': 0.12}
    assert c == spec, f'build_scene constants changed: {c}'
    mount = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, 'lever_mount')
    jnt = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, 'lever_hinge')
    paddle = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_GEOM, 'lever_paddle')
    tip = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_SITE, 'lever_tip')
    assert np.allclose(m.body_pos[mount], [c['WALL_X'], 0, c['LEVER_Z']])
    assert np.allclose(m.jnt_axis[jnt], [0, -1, 0])
    c.update({
        'HINGE': [c['WALL_X'], 0.0, c['LEVER_Z']],
        'HINGE_AXIS': [0, -1, 0],               # lever_angle rotates the paddle about -y (paddle tip goes down)
        'PRESS_ANGLE': PRESS_ANGLE,             # rad (env.py): 0.20 rad = 11.46 deg
        'PRESS_ANGLE_DEG': round(float(np.degrees(PRESS_ANGLE)), 3),
        'LEVER_RANGE': r6(m.jnt_range[jnt]),
        'PADDLE_POS': r6(m.geom_pos[paddle]),   # paddle box centre, lever frame
        'PADDLE_HALF': r6(m.geom_size[paddle]),  # paddle box half sizes
        'LEVER_TIP': r6(m.site_pos[tip]),       # the site paw_dist is measured to, lever frame
        'WALL_T': 0.01,                         # physics walls are 1 cm thick boxes outside the inner faces
        'ROD_PITCH': 0.008, 'ROD_R': 0.0022,    # grid floor (film.py), rods run along y
    })
    return c


def export(run_dir):
    npz = os.path.join(run_dir, 'blender', 'rat_skin.npz')
    skin = np.load(npz, allow_pickle=True)
    verts = skin['verts'].astype(np.float64)
    faces = skin['faces'].astype(np.int64)
    names = [str(n) for n in skin['bone_names']]
    weights = list(skin['weights'])
    nv, nb = len(verts), len(names)
    W = dense_weights(weights, nv)

    m = mujoco.MjModel.from_xml_path(SCENE)
    # the npz must be this scene's skin, bone for bone
    assert m.skin_vertnum[0] == nv and m.skin_facenum[0] == len(faces) and m.skin_bonenum[0] == nb
    assert np.array_equal(m.skin_vert.reshape(-1, 3), skin['verts'])
    assert np.array_equal(m.skin_face.reshape(-1, 3), skin['faces'])
    assert names == [m.body(int(i)).name for i in m.skin_bonebodyid]

    coat, fur, hood = coat_and_fur(verts, names, W)
    weld = weld_map(skin['verts'], W)
    normals, degenerate = area_normals(verts, faces, weld)
    skull = names.index('skull')
    nose = int(np.argmax(np.where(W[:, skull] > 0.5, verts[:, 0], -1e9)))   # film.py whisker anchor

    rat = {
        'format': 'ratbrain-rat-1',
        'units': 'm',
        'source': {'npz': os.path.relpath(npz, ROOT).replace('\\', '/'), 'npz_sha256': sha256(npz),
                   'scene_sha256': sha256(SCENE)},
        'conventions': {'quat': 'w,x,y,z', 'coat': 'linear RGB',
                        'weights': 'per bone: [vertex ids..., weights...]; normalise by the per-vertex sum',
                        'skinning': "v' = sum_b w_b (R_b R_bind_b^T (v - p_bind_b) + x_b) / sum_b w_b",
                        'normals': 'area-weighted; sum face cross products into weld[i], normalise, copy back'},
        'n_verts': nv, 'n_faces': len(faces), 'n_bones': nb,
        'verts': r6(verts),
        'faces': [int(i) for i in faces.ravel()],
        'bones': names,
        'bind_pos': [r6(p) for p in skin['bindpos']],
        'bind_quat': [r6(q) for q in skin['bindquat']],
        'weights': [[int(i) for i in wv[:, 0]] + r6(wv[:, 1]) for wv in weights],
        'coat': r6(coat),
        'fur': r6(fur),
        'hood': r6(hood),
        'rest_normals': r6(normals),
        'weld': [int(i) for i in weld],
        'chamber': chamber(m),
        'face': {'skull_bone': skull, 'eye_offset': EYE_OFFSET, 'eye_radius': EYE_RADIUS, 'nose_vert': nose},
    }
    os.makedirs(ASSETS, exist_ok=True)
    out = os.path.join(ASSETS, 'rat.json')
    with open(out, 'w', newline='\n') as f:
        json.dump(rat, f, separators=(',', ':'))
    print(f'rat.json: {nv} verts, {len(faces)} faces, {nb} bones, '
          f'{sum(len(w) for w in weights)} influences, {os.path.getsize(out):,} bytes')
    print(f'  fur=0 (bare) verts: {(fur < 0.01).sum()}, hood verts: {(hood > 0.5).sum()}, nose vert {nose}')
    print(f'  seam duplicates: {int((weld != np.arange(nv)).sum())} verts welded onto '
          f'{len(np.unique(weld))} positions; zero-normal verts given a neighbour normal: {degenerate.tolist()}')
    return out


def export_coin():
    from PIL import Image
    src = os.path.join(ROOT, COIN_SRC)
    im = Image.open(src).convert('RGB')
    x0, y0, side = COIN_BOX
    assert x0 >= 0 and y0 >= 0 and x0 + side <= im.width and y0 + side <= im.height, 'coin crop out of frame'
    coin = im.crop((x0, y0, x0 + side, y0 + side))
    if coin.size != (COIN_SIZE, COIN_SIZE):
        coin = coin.resize((COIN_SIZE, COIN_SIZE), Image.LANCZOS)
    out = os.path.join(ASSETS, 'coin.png')
    coin.save(out, 'PNG', optimize=True)
    size = os.path.getsize(out)
    print(f'coin.png: {coin.size[0]}x{coin.size[1]} from {COIN_SRC} box {COIN_BOX}, {size:,} bytes')
    assert size < COIN_MAX_BYTES, f'coin.png is {size} bytes, over the {COIN_MAX_BYTES} limit'
    return out


# ---------------------------------------------------------------- check: rat.json vs MuJoCo's own skin
def quat_to_mat(q):
    """w,x,y,z unit quaternion(s) -> rotation matrix(es); the same maths rig.html uses."""
    q = np.asarray(q, dtype=np.float64)
    q = q / np.linalg.norm(q, axis=-1, keepdims=True)
    w, x, y, z = np.moveaxis(q, -1, 0)
    return np.stack([
        np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)], -1),
        np.stack([2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)], -1),
        np.stack([2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)], -1)], -2)


def skin_from_json(rat, xpos, xquat):
    """SPEC formula: v' = sum_b w_b (R_b R_bind_b^T (v - p_bind_b) + x_b), normalised by the weight sum."""
    v = np.asarray(rat['verts']).reshape(-1, 3)
    out = np.zeros_like(v)
    wsum = np.zeros(len(v))
    Rb = quat_to_mat(xquat)
    Rbind = quat_to_mat(rat['bind_quat'])
    for b, flat in enumerate(rat['weights']):
        n = len(flat) // 2
        ids = np.asarray(flat[:n], dtype=np.int64)
        w = np.asarray(flat[n:])
        R = Rb[b] @ Rbind[b].T
        out[ids] += w[:, None] * ((v[ids] - np.asarray(rat['bind_pos'][b])) @ R.T + xpos[b])
        wsum[ids] += w
    return out / wsum[:, None]


def check(run_dir, path):
    with open(path) as f:
        rat = json.load(f)
    m = mujoco.MjModel.from_xml_path(SCENE)
    d = mujoco.MjData(m)
    bodies = [mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, n) for n in rat['bones']]
    assert bodies == [int(i) for i in m.skin_bonebodyid], 'bone order != m.skin_bonebodyid'
    # weights are MuJoCo's own, all influences
    for b in range(m.skin_bonenum[0]):
        a, n = m.skin_bonevertadr[b], m.skin_bonevertnum[b]
        flat = rat['weights'][b]
        assert len(flat) == 2 * n and flat[:n] == [int(i) for i in m.skin_bonevertid[a:a + n]]
        assert np.abs(np.asarray(flat[n:]) - m.skin_bonevertweight[a:a + n]).max() < 1e-6

    qpos = np.load(os.path.join(run_dir, 'qpos.npy'))
    meta = json.load(open(os.path.join(run_dir, 'run.json')))
    press = (meta.get('press') or {}).get('frame')
    frames = sorted({0, len(qpos) // 2, len(qpos) - 1} | ({press} if press is not None else set()))
    scn = mujoco.MjvScene(m, 2000)
    opt, cam = mujoco.MjvOption(), mujoco.MjvCamera()
    faces = np.asarray(rat['faces']).reshape(-1, 3)
    worst = 0.0
    for i in frames:
        d.qpos[:] = qpos[i]
        mujoco.mj_forward(m, d)
        mujoco.mjv_updateScene(m, d, opt, None, cam, mujoco.mjtCatBit.mjCAT_ALL, scn)
        mj_v = np.asarray(scn.skinvert).reshape(-1, 3)[:m.skin_vertnum[0]]
        mj_n = np.asarray(scn.skinnormal).reshape(-1, 3)[:m.skin_vertnum[0]]
        ours = skin_from_json(rat, d.xpos[bodies], d.xquat[bodies])
        err = float(np.abs(ours - mj_v).max())
        # per-frame normals recomputed from the skinned positions (unwelded, as MuJoCo does) vs MuJoCo's own;
        # vertices whose face normals cancel exactly are excluded (MuJoCo gives those an arbitrary (1,0,0))
        n_ours, bad = area_normals(ours, faces, fallback=False)
        ok = np.setdiff1d(np.arange(len(ours)), bad)
        ang = float(np.degrees(np.arccos(np.clip((n_ours[ok] * mj_n[ok]).sum(1), -1, 1))).max())
        tag = ' (press)' if i == press else ''
        print(f'  frame {i:5d}{tag}: max |v_json - v_mujoco| = {err:.3e} m, '
              f'max normal angle {ang:.4f} deg (excl. {len(bad)} zero-normal vert)')
        worst = max(worst, err)
    weld = np.asarray(rat['weld'])
    assert np.array_equal(np.asarray(rat['verts']).reshape(-1, 3), np.asarray(rat['verts']).reshape(-1, 3)[weld])
    # rest normals point outward: positive signed volume with the face winding they were built from
    v = np.asarray(rat['verts']).reshape(-1, 3)
    vol = float(np.einsum('ij,ij->i', v[faces[:, 0]], np.cross(v[faces[:, 1]], v[faces[:, 2]])).sum() / 6)
    print(f'  rest mesh signed volume {vol * 1e6:.1f} cm^3 (> 0: outward normals)')
    assert vol > 0
    print(f'skin check: worst max error {worst:.3e} m over {len(frames)} recorded frames (limit 1e-5)')
    assert worst < 1e-5, 'rat.json skinning does not match MuJoCo'
    return worst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', default=os.path.join('runs', 'film_2026'))
    ap.add_argument('--check-only', action='store_true')
    ap.add_argument('--no-check', action='store_true')
    a = ap.parse_args()
    run_dir = a.run if os.path.isabs(a.run) else os.path.join(ROOT, a.run)
    path = os.path.join(ASSETS, 'rat.json')
    if not a.check_only:
        path = export(run_dir)
        export_coin()
    if not a.no_check:
        check(run_dir, path)


if __name__ == '__main__':
    main()
