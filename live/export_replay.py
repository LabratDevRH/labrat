"""Export the RatTest launch session as the website's replay clip: site/replay/ratest.bin + ratest.json.

    python live/export_replay.py [--run runs/brainrig_20260924T133937Z_seed2026] [--out site/replay] [--fps 20]

It RE-RUNS the recorded session through session.py's replay path, the way session.replay() and
replay_session.py do: the same two networks (the run's steer.pt + press.pt copies), seed, pre-roll, start
cursor and command log. It then checks that the re-run reproduces the recording (brain commit, every physics
frame bit for bit against qpos.npy, the 11 clicks, the session proof) and writes the clip only if it does
(--allow-mismatch writes it anyway, marked "verified": false). Read-only use of session.py; this script never
reads .env or the launch journal and signs or sends nothing.

ratest.bin   the frames, back to back, in live/labrat_frame.py's layout (1,868 bytes each, --fps per second,
             one episode: index 0). Frame j shows the physics state at t = j / fps s after the reset (the
             first 2 s are the brain-off pre-roll), the cursor and lit target after the last completed 50 Hz
             control step, and click = 1 when a click registered in (t - 1/fps, t]. A click frame shows the
             state at the click instant instead (lever down; [1] = that time, at most 1/fps earlier), because
             the rig starts a new trial the moment a click lands: the rat is put back in its start pose
             (ratest.json "resets"; do not interpolate across those).
ratest.json  fps, n_frames, duration, layout, the 11 targets (labels, page boxes, lit / hit times from
             targets.json + the command log), the rig's typing, an event timeline, the coin facts, the brain,
             the verification, source "replay" and the label.
"""
import argparse
import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

# MuJoCo compiling scene.xml needs ~1 MiB of C stack; some python.exe builds give the main thread only 1 MiB,
# so the simulation runs on a thread with a bigger stack (as live/rig.py does).
THREAD_STACK = 4 * 1024 * 1024
threading.stack_size(THREAD_STACK)

LIVE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(LIVE)
sys.path.insert(0, ROOT)
sys.path.insert(0, LIVE)

import numpy as np  # noqa: E402
import mujoco  # noqa: E402

from env import CTRL_DT  # noqa: E402
from session import Session, CODE_FILES  # noqa: E402
import ptload  # noqa: E402
import labrat_frame as lf  # noqa: E402

DEFAULT_RUN = os.path.join('runs', 'brainrig_20260924T133937Z_seed2026')
LABEL = 'Replay: the RatTest launch session, recorded 2026-09-24'


def rel(p):
    return os.path.relpath(p, ROOT).replace('\\', '/')


def sha256_file(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


class Recorder:
    """session.Session.run's on_step callback: the time of every recorded physics frame (in physics steps
    since the reset) and, per control step, the cursor, the lit target and any click."""

    def __init__(self, per):
        self.per = per              # physics steps per control step
        self.n = 1                  # inner.frames starts with the reset frame, at time 0
        self.ti = 0
        self.times = [0]
        self.step_end = []          # physics-step time at which each control step ended
        self.cursor = []
        self.target = []            # (cx, cy, hw, hh) if lit after the step, else None
        self.click = []             # (x, y, hit, (cx, cy, hw, hh) lit at the click) or None
        self.resets = []            # physics-step times of new-trial resets (the rat put back in its start pose)

    def __call__(self, inner, phase, info, obs):
        n = len(inner.frames)
        new = n - self.n
        if phase == 'preroll':
            self.times.extend(range(self.ti + 1, self.ti + new + 1))
            self.ti += new
        else:
            extra = new - self.per
            assert extra in (0, 1), f'unexpected {new} frames in one control step'
            if extra:               # env.new_trial() -> reset_body() appends the reset pose at the step start
                self.times.append(self.ti)
                self.resets.append(self.ti)
            self.times.extend(range(self.ti + 1, self.ti + self.per + 1))
            self.ti += self.per
            self.step_end.append(self.ti)
            self.cursor.append((float(inner.cursor[0]), float(inner.cursor[1])))
            tgt = (float(inner.tc[0]), float(inner.tc[1]), float(inner.th[0]), float(inner.th[1]))
            self.target.append(tgt if inner.hold == 0 else None)
            c = info.get('click') if info else None
            # with the rig's targets a hit only switches to a hold (tc/th stay), so tc/th are the clicked target
            self.click.append((float(c[1]), float(c[2]), bool(c[3]), tgt) if c else None)
        self.n = n


def rerun(run_dir):
    """session.replay(), with an on_step recorder. Returns (session, recorder, proof, checks)."""
    meta = json.load(open(os.path.join(run_dir, 'session.json')))
    s = Session(os.path.join(ROOT, meta['policy']), meta['seed'], meta['preroll_s'], tuple(meta['cursor0']),
                os.path.join(ROOT, meta.get('press_policy', 'runs/final/policy.pt')))
    m = mujoco.MjModel.from_xml_path(os.path.join(ROOT, 'assets', 'scene.xml'))
    rec = Recorder(int(round(CTRL_DT / m.opt.timestep)))
    proof, info = s.run(on_step=rec, realtime=False,
                        commands=[(st, tuple(c)) for st, c in meta['commands']], max_steps=meta['steps'])
    recorded = np.load(os.path.join(run_dir, 'qpos.npy'))
    fr = np.array(s.env.frames)
    same = recorded.shape == fr.shape and float(np.abs(recorded - fr).max()) == 0.0
    checks = {
        'brain_commit_ok': s.commit == meta['brain_commit'],
        'frames_identical': bool(same),
        'clicks_identical': s.clicks == meta['clicks'],
        'proof_ok': proof == meta['session_proof'],
    }
    checks['match'] = all(checks.values())
    assert len(rec.times) == len(s.env.frames), (len(rec.times), len(s.env.frames))
    return meta, s, rec, proof, checks


def sample(s, rec, fps, timestep):
    """The clip: frames at t = j / fps. Returns (list of frame bytes, per-frame summary arrays)."""
    stride = 1.0 / (fps * timestep)
    assert abs(stride - round(stride)) < 1e-9, f'{fps} fps is not a whole number of {timestep * 1e3:g} ms physics steps'
    stride = int(round(stride))
    inner = s.env
    frames = s.env.frames
    times = np.asarray(rec.times)
    ends = np.asarray(rec.step_end)
    n_out = rec.ti // stride + 1
    pose_of = lf.PoseReader(inner.m)
    lever_q = inner.lever_q
    out, lever, clicks, cur, up, torso_z = [], [], [], [], [], []
    for j in range(n_out):
        T = j * stride
        fi = int(np.searchsorted(times, T, side='right') - 1)       # latest physics frame at or before T
        k = int(np.searchsorted(ends, T, side='right'))              # control steps completed by T
        cursor = rec.cursor[k - 1] if k else tuple(s.cursor0)
        target = rec.target[k - 1] if k else None
        lo = int(np.searchsorted(ends, T - stride, side='right'))    # steps that ended in (T - stride, T]
        click = None
        shown_t = T
        if j:
            for i in range(lo, k):
                if rec.click[i] is not None:
                    click = rec.click[i]
                    shown_t = int(ends[i])
        if click is not None:
            # the rig starts a new trial the moment a click lands (the rat is put back in its start pose), so a
            # click frame shows the state AT the click (the step's last physics frame, before that reset):
            # lever down, paw on it. [1] then says when that state is (at most 1/fps before j / fps).
            target = click[3]
            cursor = (click[0], click[1])
            fi = int(np.searchsorted(times, shown_t, side='left'))
        pose = pose_of(frames[fi])
        ang = float(frames[fi][lever_q])
        out.append(lf.pack(shown_t * timestep, 0, ang, click is not None, cursor, target, pose))
        lever.append(ang)
        clicks.append(click)
        cur.append(cursor)
        up.append(lf.quat_up_z(pose[0, 3:7]))
        torso_z.append(pose[0, 2])
    return out, {'lever': np.array(lever), 'clicks': clicks, 'cursor': cur, 'up': np.array(up),
                 'torso_z': np.array(torso_z), 'stride': stride, 'n_out': n_out}


def clip_time(step, timestep, preroll_steps, per):
    """Clip time of a command logged at control step `step` (applied at the START of the step that takes
    inner.t from step to step + 1), or of a click logged at `step` (inner.t AFTER its step, i.e. the END of the
    step from step - 1 to step). Both are preroll + per * step physics steps after the reset."""
    return round((preroll_steps + per * step) * timestep, 3)


def build_json(meta, rec, checks, proof, targets_json, events, receipt, n_frames, fps, timestep, run_dir,
               bin_bytes, bin_sha):
    per = rec.per
    pre = int(round(meta['preroll_s'] / timestep))
    viewport = targets_json.get('viewport', [1280, 900])

    # ---- the rig's wall clock -> clip time (only for the non-brain events: typing, pons, the transaction)
    offs = [c['at'] - clip_time(c['step'], timestep, pre, per) for c in targets_json['clicks']]
    offset = float(np.median(offs))
    wall_err = float(np.max(np.abs(np.asarray(offs) - offset)))

    def wall(ts):
        return round(float(ts) - offset, 2)

    stage_done = {}
    for e in events:
        if e.get('type') == 'stage' and e.get('state') == 'done':
            stage_done[e['stage']] = e['ts']

    tlist, timeline = [], [{'t': round(meta['preroll_s'], 3), 'kind': 'brain_on', 'exact': True,
                            'text': 'Brain on: the steering network starts turning the head, which moves the cursor'}]
    for t in targets_json['targets']:
        light = t['lights'][0]
        hit = t.get('hit') or {}
        lit_at = clip_time(light['step'], timestep, pre, per)
        hit_at = clip_time(hit['step'], timestep, pre, per) if hit else None
        box = light['box']
        item = {
            'n': t['n'], 'key': t['key'], 'label': t['label'], 'kind': t['kind'],
            'lit_at': lit_at, 'hit_at': hit_at,
            'seconds_to_hit': round(hit_at - lit_at, 3) if hit else None,
            'box_px': box, 'norm': light['norm'],
            'click_px': [hit['x'], hit['y']] if hit else None,
            'click_norm': [round(hit['x'] / viewport[0], 6), round(hit['y'] / viewport[1], 6)] if hit else None,
            'misses_before': hit.get('misses_before', 0) if hit else None,
            'lights': len(t['lights']), 'state': t['state'], 'detail': t.get('detail'),
        }
        res = t.get('result') or {}
        if 'typed' in res:
            item['typed'] = res['typed']
            item['typing_seconds'] = res.get('seconds')
            if t['key'] in stage_done:
                end = wall(stage_done[t['key']])
                item['typed_from'] = round(max(end - (res.get('seconds') or 0), hit_at or 0), 2)
                item['typed_until'] = end
        tlist.append(item)
        timeline.append({'t': lit_at, 'kind': 'lit', 'target': t['n'], 'exact': True,
                         'text': f"Target {t['n']} of 11 lights up: {t['label']}"})
        if hit:
            timeline.append({'t': hit_at, 'kind': 'click', 'target': t['n'], 'exact': True,
                             'text': f"The rat clicks it (lever press) after {item['seconds_to_hit']:.2f} s"})
        if 'typed_from' in item:
            text = item['typed'] if len(item['typed']) <= 40 else item['typed'][:37] + '...'
            timeline.append({'t': item['typed_from'], 'kind': 'typing', 'target': t['n'], 'exact': False,
                             'until': item['typed_until'],
                             'text': f'The rig types "{text}" into the field the rat clicked'})

    tx = {}
    for e in events:
        typ, msg = e.get('type'), e.get('msg', '')
        if typ == 'log' and 'image picker' in msg and 'pinned' in msg:
            timeline.append({'t': wall(e['ts']), 'kind': 'image', 'exact': False,
                             'text': 'The click opened the image picker; the rig chose the coin picture'})
        elif typ == 'log' and msg.startswith('scrolling '):
            timeline.append({'t': wall(e['ts']), 'kind': 'scroll', 'exact': False,
                             'text': 'The rig scrolls the page to the next target; the rat waits'})
        elif typ == 'log' and 'eth_sendTransaction' in msg:
            timeline.append({'t': wall(e['ts']), 'kind': 'tx_requested', 'exact': False,
                             'text': 'pons asks the wallet to send the launch transaction'})
        elif typ == 'tx':
            tx[e['phase']] = e
            if e['phase'] == 'checked':
                n_ok = sum(1 for c in e['checks'].values() if c.get('ok'))
                timeline.append({'t': wall(e['ts']), 'kind': 'tx_checked', 'exact': False,
                                 'text': f"The rig decodes it: {n_ok}/{len(e['checks'])} checks pass"})
            elif e['phase'] == 'signed':
                timeline.append({'t': wall(e['ts']), 'kind': 'tx_signed', 'exact': False,
                                 'text': f"Signed once (nonce {e.get('nonce')})"})
            elif e['phase'] == 'sent':
                timeline.append({'t': wall(e['ts']), 'kind': 'tx_sent', 'exact': False,
                                 'text': 'Broadcast to Robinhood Chain'})
            elif e['phase'] == 'mined':
                timeline.append({'t': wall(e['ts']), 'kind': 'tx_mined', 'exact': False,
                                 'text': f"Mined in block {e.get('block')}: RatTest ($RATTEST) exists"})
    timeline.sort(key=lambda x: (x['t'], 0 if x.get('exact') else 1))
    mined_t = next((x['t'] for x in timeline if x['kind'] == 'tx_mined'), None)

    launch = meta['launch']
    rig = meta['rig']
    checked = tx.get('checked', {})
    n_checks = len(checked.get('checks', {}))
    n_ok = sum(1 for c in checked.get('checks', {}).values() if c.get('ok'))
    token = launch['token']
    assert receipt.get('tx', launch['tx']) == launch['tx'] and receipt.get('token', token) == token
    coin = {
        'name': rig['coin']['name'], 'symbol': rig['coin']['symbol'],
        'chain': 'Robinhood Chain', 'launchpad': 'pons',
        'contract': token, 'tx': launch['tx'], 'block': launch['block'], 'status': launch.get('status'),
        'explorer_tx': receipt.get('explorer_tx', launch['explorer']),
        'explorer_token': receipt.get('explorer_token'),
        'pons_page': receipt.get('pons_coin'),
        'creator': launch['creator'], 'creator_fee_bps': rig['coin']['tax_bps'],
        'creator_fee': f"{rig['coin']['tax_bps'] / 100:g}%",
        'description': launch['description'], 'brain_sha256': launch['brain_commit'],
        'image': rig.get('image_pinned'),
        'targets': len(targets_json['targets']),
        'clicks_by_the_rat': targets_json['hits_forwarded'], 'misses': targets_json['misses_masked'],
        'checks_passed': n_ok, 'checks_total': n_checks,
        'signed': f"once (nonce {receipt.get('nonce', tx.get('signed', {}).get('nonce'))})"
        if receipt.get('signed') or 'signed' in tx else None,
        'send_requests': rig.get('send_requests'),
        'receipt_seen_utc': datetime.fromtimestamp(tx['mined']['ts'], timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        if 'mined' in tx else None,
    }

    def net(path, name, role):
        ck = ptload.load(path)
        sd = ck['net']
        layers = [sd['pi.0.weight'].shape[1]] + [sd[f'pi.{i}.weight'].shape[0] for i in (0, 2, 4, 6)]
        conns = sum(sd[f'pi.{i}.weight'].size for i in (0, 2, 4, 6))
        return {'name': name, 'role': role, 'layers': layers, 'units': int(sum(layers)), 'connections': int(conns),
                'sha256': sha256_file(path), 'file': rel(path)}
    nets = [net(os.path.join(ROOT, meta['policy']), 'steering network',
                'turns the head (4 neck actuators), which moves the cursor, and decides when to press'),
            net(os.path.join(ROOT, meta['press_policy']), 'lever-press network',
                'performs each press with the whole body (38 actuators) and stays standing')]
    brain = {'networks': nets, 'units': sum(n['units'] for n in nets),
             'connections': sum(n['connections'] for n in nets), 'commit': meta['brain_commit'],
             'commit_covers': ['assets/scene.xml', 'both networks'] + list(CODE_FILES)}

    stamp = datetime.fromtimestamp(events[0]['ts'], timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if events else None
    return {
        'format': 'labrat-replay-1',
        'source': 'replay',
        'label': LABEL,
        'task': 'steer',
        'run': os.path.basename(os.path.normpath(run_dir)),
        'recorded_utc': stamp,
        'fps': fps,
        'n_frames': n_frames,
        'duration': round(n_frames / fps, 3),
        'sim_seconds': round(rec.ti * timestep, 3),
        'brain_on_at': meta['preroll_s'],
        'loop_end': round(min(mined_t + 4.0, n_frames / fps), 2) if mined_t is not None else None,
        'loop_note': 'after the coin was mined the rat only stands while the rig finishes; a player may loop '
                     'at loop_end',
        'file': 'ratest.bin', 'bytes': bin_bytes, 'sha256': bin_sha,
        'layout': {
            'dtype': 'float32', 'endianness': 'little', 'frame_floats': lf.FRAME_FLOATS,
            'frame_bytes': lf.FRAME_BYTES, 'header_floats': lf.HEADER, 'header': lf.FIELDS, 'magic': lf.MAGIC,
            'bones': lf.N_BONES, 'bone_floats': lf.BONE_FLOATS,
            'bone_layout': 'px,py,pz,qw,qx,qy,qz: world pose of each bone body (MuJoCo xpos/xquat, metres, z up)',
            'bone_order': 'rat.json "bones"',
            'click_frames': 'a frame with click = 1 shows the state at the click instant (sim_t = that time, at '
                            'most 1/fps before the frame\'s slot) and the target that was lit at the click',
        },
        'viewport': viewport,
        'coords': f'cursor and targets in page coordinates 0..1 (x / {viewport[0]}, y / {viewport[1]}, y down)',
        'resets': [round(r * timestep, 3) for r in rec.resets],
        'resets_note': 'between targets the rig starts a new trial: the rat is put back in its standing start '
                       'pose and rests while the rig types, scrolls or pons is busy (do not interpolate across)',
        'targets': tlist,
        'timeline': timeline,
        'timeline_note': 'brain_on, lit and click times are exact (from the command log); typing, image, scroll '
                         f'and transaction times come from the rig\'s wall clock mapped onto the replay '
                         f'(within about {max(wall_err, 0.01):.2f} s)',
        'coin': coin,
        'brain': brain,
        'verification': {
            'match': checks['match'], 'brain_commit_ok': checks['brain_commit_ok'],
            'frames_identical': checks['frames_identical'], 'clicks_identical': checks['clicks_identical'],
            'session_proof': meta['session_proof'], 'replayed_proof': proof,
            'physics_frames': len(rec.times), 'control_steps': meta['steps'],
            'check_it': f'python replay_session.py {rel(run_dir)}',
            'recorded_with': meta.get('environment'),
            'exported_with': {'python': sys.version.split()[0], 'numpy': np.__version__,
                              'mujoco': mujoco.__version__},
        },
        'honesty': 'A replay, not live. This clip re-simulates the recorded RatTest launch session from its saved '
                   'brain, seed and command log, and reproduces the recording bit for bit. The brain is two '
                   'trained artificial neural networks driving a simulated rat body (DeepMind\'s open-source '
                   'rodent model in MuJoCo), not a biological brain. The rig lit each target, forwarded only '
                   'on-target clicks and typed a field\'s text after the rat clicked into it; the rat did every '
                   'click, not the typing.',
        'credits': {'body': 'DeepMind rodent model (dm_control, Apache-2.0), the rat from Aldarondo et al., '
                            'Nature 2024', 'physics': 'MuJoCo'},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--run', default=DEFAULT_RUN)
    ap.add_argument('--out', default=os.path.join('site', 'replay'))
    ap.add_argument('--fps', type=int, default=20)
    ap.add_argument('--name', default='ratest')
    ap.add_argument('--allow-mismatch', action='store_true', help='write the clip even if the re-run differs')
    a = ap.parse_args()
    run_dir = a.run if os.path.isabs(a.run) else os.path.join(ROOT, a.run)
    out_dir = a.out if os.path.isabs(a.out) else os.path.join(ROOT, a.out)

    t0 = time.time()
    print(f're-running {rel(run_dir)} through session.py (same brain, seed and command log)...', flush=True)
    with ThreadPoolExecutor(1, thread_name_prefix='replay-sim') as ex:
        meta, s, rec, proof, checks = ex.submit(rerun, run_dir).result()
    print(f'  {meta["steps"]} control steps, {len(rec.times)} physics frames in {time.time() - t0:.1f} s')
    print(f'  brain commit   {meta["brain_commit"]} {"OK" if checks["brain_commit_ok"] else "DIFFERENT"}')
    print(f'  frames         {"identical" if checks["frames_identical"] else "DIFFERENT"} to qpos.npy')
    print(f'  clicks         {len(meta["clicks"])} recorded, {"identical" if checks["clicks_identical"] else "DIFFERENT"}')
    print(f'  recorded proof {meta["session_proof"]}')
    print(f'  replayed proof {proof}')
    print('  MATCH' if checks['match'] else '  MISMATCH')
    if not checks['match'] and not a.allow_mismatch:
        print('the re-run does not reproduce the recorded session: no clip written (--allow-mismatch to force)')
        return 1

    timestep = float(s.env.m.opt.timestep)
    with ThreadPoolExecutor(1, thread_name_prefix='replay-pose') as ex:
        frames, summ = ex.submit(sample, s, rec, a.fps, timestep).result()
    blob = b''.join(frames)
    targets_json = json.load(open(os.path.join(run_dir, 'targets.json')))
    events = [json.loads(ln) for ln in open(os.path.join(run_dir, 'events.jsonl'), encoding='utf-8') if ln.strip()]
    events = [e for e in events if e.get('type') != 'shot']

    # ---- sanity checks on the clip
    lever_range = json.load(open(lf.RAT_JSON))['chamber']['LEVER_RANGE']
    problems = []
    for j, b in enumerate(frames):
        fr = lf.unpack(b)
        problems += [f'frame {j}: {p}' for p in lf.check_frame(fr, lever_range)]
        late = j / a.fps - fr['sim_t']
        if fr['episode'] != 0 or late < -1e-4 or late > (1.0 / a.fps if fr['click'] else 1e-4):
            problems.append(f"frame {j}: episode {fr['episode']} sim_t {fr['sim_t']}")
    n_clicks = sum(1 for c in summ['clicks'] if c is not None)
    hits_inside = 0
    for c in summ['clicks']:
        if c is not None:
            x, y, hit, (cx, cy, hw, hh) = c
            hits_inside += int(hit and abs(x - cx) <= hw and abs(y - cy) <= hh)
    if n_clicks != len(meta['clicks']):
        problems.append(f'{n_clicks} click frames, {len(meta["clicks"])} clicks recorded')
    if hits_inside != n_clicks:
        problems.append(f'only {hits_inside} of {n_clicks} click frames have the cursor inside the lit target')
    if summ['up'].min() < 0.5:
        problems.append(f'torso tilted: min up z {summ["up"].min():.3f}')
    print(f'clip: {len(frames)} frames at {a.fps} fps = {len(frames) / a.fps:.2f} s, {len(blob):,} bytes')
    print(f'  lever angle {summ["lever"].min():+.4f} .. {summ["lever"].max():+.4f} rad (hinge range {lever_range}, '
          f'soft limit), frames past the press angle: {int((summ["lever"] > 0.2).sum())}')
    print(f'  torso up z min {summ["up"].min():.3f}, torso height {summ["torso_z"].min():.4f} .. '
          f'{summ["torso_z"].max():.4f} m')
    print(f'  click frames {n_clicks}, cursor inside the lit target on {hits_inside}')
    print(f'  new-trial resets at {[round(r * timestep, 2) for r in rec.resets]} s')
    if problems:
        print('PROBLEMS:\n  ' + '\n  '.join(problems[:20]))
        return 1

    os.makedirs(out_dir, exist_ok=True)
    bin_path = os.path.join(out_dir, a.name + '.bin')
    json_path = os.path.join(out_dir, a.name + '.json')
    with open(bin_path + '.tmp', 'wb') as f:
        f.write(blob)
    os.replace(bin_path + '.tmp', bin_path)
    rpath = os.path.join(run_dir, 'live_receipt.json')
    receipt = json.load(open(rpath)) if os.path.exists(rpath) else {}
    doc = build_json(meta, rec, checks, proof, targets_json, events, receipt, len(frames), a.fps, timestep,
                     run_dir, len(blob), hashlib.sha256(blob).hexdigest())
    with open(json_path + '.tmp', 'w', encoding='utf-8', newline='\n') as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
        f.write('\n')
    os.replace(json_path + '.tmp', json_path)
    print(f'wrote {rel(bin_path)} ({len(blob):,} bytes) and {rel(json_path)} '
          f'({os.path.getsize(json_path):,} bytes) in {time.time() - t0:.1f} s')
    return 0


if __name__ == '__main__':
    sys.exit(main())
