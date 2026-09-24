"""Quick MuJoCo-renderer preview of a rollout (for checking behaviour, not the final film).

    python preview.py runs/lever_v1/policy_best.pt 7 preview.mp4
"""
import sys
import numpy as np
import mujoco
import imageio

from rollout import run
from env import SCENE


def render(frames, path, fps=50, stride=10, cam_az=60, w=640, h=480):
    m = mujoco.MjModel.from_xml_path(SCENE)
    d = mujoco.MjData(m)
    r = mujoco.Renderer(m, h, w)
    cam = mujoco.MjvCamera()
    cam.lookat[:] = [0.1, 0, 0.03]; cam.distance = 0.35; cam.azimuth = cam_az; cam.elevation = -20
    opt = mujoco.MjvOption()
    with imageio.get_writer(path, fps=fps, quality=8) as wr:
        for q in frames[::stride]:
            d.qpos[:] = q
            mujoco.mj_forward(m, d)
            r.update_scene(d, cam, opt)
            wr.append_data(r.render())


if __name__ == '__main__':
    pol, seed, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    meta, frames, _ = run(pol, seed, preroll_s=1.0)
    print({k: meta[k] for k in ('seed', 'press', 'fell')})
    render(frames, out)
    print('wrote', out, len(frames), 'physics frames')
