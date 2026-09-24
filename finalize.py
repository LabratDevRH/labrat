"""After training: pick the final policy, record a DRY rehearsal, check it, and prepare the film.

    python finalize.py stage_a     # wait for training, eval every snapshot, select, rehearse (DRY), replay, export, test frames
    python finalize.py stage_b     # full-quality Blender render of hero/side/top + compose the film

Never goes live: launch_run is always called without --live (eth_call simulation only).
"""
import glob, json, os, shutil, stat, subprocess, sys, time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = os.path.join(HERE, 'runs', 'lever_v3')
FINAL = os.path.join(HERE, 'runs', 'final')
BLENDER = r'C:\Program Files\Blender Foundation\Blender 4.5\blender.exe'
STATE = os.path.join(FINAL, 'finalize_state.json')


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def sh(args, **kw):
    log('$', ' '.join(str(x) for x in args))
    r = subprocess.run(args, cwd=HERE, **kw)
    if r.returncode != 0:
        raise SystemExit(f'failed ({r.returncode}): {args}')
    return r


def training_running():
    out = subprocess.run(['powershell', '-NoProfile', '-c',
                          "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | ? { $_.CommandLine -match 'train.py' }).Count"],
                         capture_output=True, text=True).stdout.strip()
    return out not in ('', '0')


def score_key(ev):
    """Launch conditions first (must be perfect), then robustness, then speed of the press."""
    s = lambda k: ev[k]['clean_rate']
    return (s('launch'), round((s('wide') + s('pushes') + s('noisy')) / 3, 4), -(ev['launch']['press_t_mean'] or 9.9))


def stage_a():
    os.makedirs(FINAL, exist_ok=True)
    while training_running() or not os.path.exists(os.path.join(RUN, 'policy_final.pt')):
        time.sleep(60)
    log('training finished')
    import eval as ev_mod
    cands = sorted(glob.glob(os.path.join(RUN, 'policy_0*M.pt'))) + [os.path.join(RUN, p) for p in ('policy_final.pt', 'policy_best.pt')]
    results = {}
    for c in cands:
        t = time.time()
        results[os.path.basename(c)] = ev_mod.score(c, n=200)
        r = results[os.path.basename(c)]
        log(os.path.basename(c), {k: r[k]['clean_rate'] for k in r}, f'{time.time() - t:.0f}s')
        json.dump(results, open(os.path.join(FINAL, 'evals.json'), 'w'), indent=1)
    best = max(results, key=lambda k: score_key(results[k]))
    log('selected', best, {k: results[best][k]['clean_rate'] for k in results[best]})
    dst = os.path.join(FINAL, 'policy.pt')
    if os.path.exists(dst):
        os.chmod(dst, stat.S_IWRITE); os.remove(dst)
    shutil.copyfile(os.path.join(RUN, best), dst)
    os.chmod(dst, stat.S_IREAD)
    json.dump({'selected': best, 'eval': results[best]}, open(os.path.join(FINAL, 'selected.json'), 'w'), indent=1)

    # DRY rehearsal (eth_call only). Keep the first seed whose run presses cleanly and stays standing.
    chosen = None
    for seed in range(2026, 2046):
        out = f'runs/film_{seed}'
        if not os.path.exists(os.path.join(HERE, out, 'run.json')):
            sh([sys.executable, 'launch_run.py', '--policy', 'runs/final/policy.pt', '--seed', str(seed), '--out', out])
        meta = json.load(open(os.path.join(HERE, out, 'run.json')))
        if meta['press'] and not meta['fell'] and (meta.get('launch') or {}).get('mode') == 'dry':
            chosen = out
            break
        log('seed', seed, 'not clean; trying the next one')
    if not chosen:
        raise SystemExit('no clean rehearsal in 20 seeds')
    sh([sys.executable, 'replay.py', chosen])
    sh([sys.executable, 'replay.py', chosen, '--actions'])
    sh([sys.executable, 'export_anim.py', chosen])
    a = np.load(os.path.join(HERE, chosen, 'blender', 'anim.npz'))
    slow = np.nonzero(a['slow'])[0]
    press = int(a['press_out_frame']) + 1
    st = {'run': chosen, 'frames': int(len(a['lever'])), 'slow': [int(slow.min()) + 1, int(slow.max()) + 1], 'press': press}
    json.dump(st, open(STATE, 'w'), indent=1)
    # one full-quality test frame per camera at the press, for a look check before the long render
    for cam in ('hero', 'side', 'top'):
        sh([BLENDER, '-b', '-P', 'blender/film.py', '--', chosen, '--cam', cam, '--frames', f'{press + 4}:{press + 4}',
            '--out', f'{chosen}/test_{cam}'], stdout=subprocess.DEVNULL)
    log('STAGE A DONE', st)


def render_frames(run, cam, frames, chunk=25):
    # Rendered WITHOUT Cycles motion blur: building the motion-blurred BVH of ~870k deforming hair strands fails
    # ('out of GPU memory' on both OptiX and CUDA from the 2nd frame with motion). The fast part of the action is
    # inside the 1/8 slow-motion window, where per-frame motion is tiny, so the loss is negligible.
    """Render in chunks, skipping frames already on disk; retry a chunk on GPU out-of-memory."""
    out = os.path.join(HERE, run, f'frames_{cam}')
    for c0 in range(0, len(frames), chunk):
        part = frames[c0:c0 + chunk]
        for attempt in range(4):
            have = {int(os.path.basename(p)[2:-4]) for p in glob.glob(os.path.join(out, 'f_*.png'))}
            todo = [f for f in part if f not in have]
            if not todo:
                break
            env = dict(os.environ, RATBRAIN_LOWMEM='1' if attempt >= 3 else '0', RATBRAIN_NOMB='1')
            r = subprocess.run([BLENDER, '-b', '-P', 'blender/film.py', '--', run, '--cam', cam,
                                '--frames', f'{todo[0]}:{todo[-1]}', '--out', f'{run}/frames_{cam}'],
                               cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env)
            if r.returncode != 0:
                log(f'{cam} frames {todo[0]}-{todo[-1]} failed (attempt {attempt + 1}): '
                    f'{(r.stderr or "").strip().splitlines()[-1:] }')
                time.sleep(20)
        else:
            raise SystemExit(f'{cam}: chunk starting {part[0]} kept failing')
        if (c0 + chunk) % 10 == 0 or c0 + chunk >= len(frames):
            log(f'{cam}: {min(c0 + chunk, len(frames))}/{len(frames)} frames')


def stage_b():
    st = json.load(open(STATE))
    run, n, (s0, s1) = st['run'], st['frames'], st['slow']
    t0 = time.time()
    render_frames(run, 'hero', list(range(1, n + 1)))
    log(f'hero done {(time.time() - t0) / 60:.0f} min')
    for cam in ('side', 'top'):
        render_frames(run, cam, list(range(s0, s1 + 1)))
        log(f'{cam} done {(time.time() - t0) / 60:.0f} min')
    sh([sys.executable, 'compose.py', run, '--out', f'{run}/ratbrain_film.mp4'])
    sh(['ffmpeg', '-loglevel', 'error', '-y', '-i', f'{run}/ratbrain_film.mp4', '-c:v', 'libx264', '-preset', 'slow',
        '-crf', '19', '-c:a', 'copy', '-movflags', '+faststart', 'ratbrain_film.mp4'])
    log('STAGE B DONE', f'{(time.time() - t0) / 60:.0f} min total')


if __name__ == '__main__':
    {'stage_a': stage_a, 'stage_b': stage_b}[sys.argv[1]]()
