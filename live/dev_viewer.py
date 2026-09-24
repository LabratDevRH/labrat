"""Dev harness for live/web/rig.html: replays a RECORDED run over the rig's WebSocket protocol (SPEC.md).

    python live/dev_viewer.py [--port 4662] [--run runs/film_2026] [--speed 1.0]
    open http://localhost:4662/?autostart=1

Serves the viewer exactly as live/rig.py will (GET /, /rat.json, /status, WS /run) so the page can be built
and checked without pons, a wallet or the policy. What it streams:
  - binary pose frames in the SPEC layout, one per control step (every 10th 2 ms physics frame = 50 Hz),
    computed from the run's qpos.npy with mujoco.mj_kinematics: world pose of each skinned body in the order
    of m.skin_bonebodyid, lever angle = qpos[-1], paw distance = min |finger site - lever_tip site|
  - the run's own proof (run.json) and a press event at the recorded press frame
  - FAKE stage/log messages, a PLACEHOLDER pons 'shot' and a FAKE DRY tx event, all labelled as such
  - optional {"type":"cursor","cx","cy","click"} messages (page pixels of the shot) for the glide to Confirm
  - the fake tx carries "dev": true and verdict "dev_fake_tx": the viewer then says
    'DEV REPLAY - fake tx, pons was not involved' instead of any DRY verdict

Nothing here opens pons, loads a key, signs or broadcasts anything.
"""
import argparse, asyncio, base64, io, json, os, sys, time

import numpy as np

LIVE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(LIVE)
sys.path.insert(0, ROOT)

import mujoco  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from env import SCENE, CTRL_DT  # noqa: E402  (read-only import: the scene path and control period)

WEB = os.path.join(LIVE, 'web')
RAT_JSON = os.path.join(LIVE, 'assets', 'rat.json')
SHOT_W, SHOT_H = 1280, 800
SHOT_FPS = 7.0
MAGIC = 1.0
FACTORY = '0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e'   # launcher.FACTORY (pons v2), shown in the fake tx
SELECTOR = '0xa72101af'                                 # launcher.SELECTOR
DRY_CREATOR = '0x' + '11' * 20                          # launcher.DRY_CREATOR
ZERO = '0x' + '00' * 20
LAUNCH_BTN = (1000, 692, 1220, 752)                    # where the placeholder page draws its 'Confirm' box


# ------------------------------------------------------------------ recorded run -> pose frames
def load_run(run_dir):
    meta = json.load(open(os.path.join(run_dir, 'run.json')))
    qpos = np.load(os.path.join(run_dir, 'qpos.npy'))
    m = mujoco.MjModel.from_xml_path(SCENE)
    d = mujoco.MjData(m)
    assert qpos.shape[1] == m.nq, f'qpos has {qpos.shape[1]} columns, scene nq={m.nq}'
    bones = np.array(m.skin_bonebodyid, dtype=int)
    tip = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_SITE, 'lever_tip')
    paws = [mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_SITE, s) for s in ('finger_L', 'finger_R')]
    lever_q = m.jnt_qposadr[mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, 'lever_hinge')]
    assert lever_q == m.nq - 1, 'lever angle is expected to be qpos[-1]'
    dt = float(m.opt.timestep)
    every = int(round(CTRL_DT / dt))                    # 10 physics frames per control step = 50 Hz
    pre = int(meta.get('preroll_frames', -1))
    press = (meta.get('press') or {}).get('frame')
    idx = list(range(0, len(qpos), every))
    if press is not None and press not in idx:          # never skip the press frame itself
        idx = sorted(set(idx) | {press})
    frames = []
    for i in idx:
        d.qpos[:] = qpos[i]
        mujoco.mj_kinematics(m, d)
        paw = min(float(np.linalg.norm(d.site_xpos[p] - d.site_xpos[tip])) for p in paws)
        pressed = press is not None and i >= press
        head = [MAGIC, i * dt, 1.0 if i > pre else 0.0, 1.0 if pressed else 0.0,
                float(qpos[i][lever_q]), paw, float(len(bones))]
        body = np.concatenate([d.xpos[bones], d.xquat[bones]], 1)          # (65, 7): px py pz qw qx qy qz
        arr = np.concatenate([np.asarray(head), body.ravel()]).astype('<f4')
        frames.append({'i': i, 't': i * dt, 'brain': i > pre, 'pressed': pressed,
                       'lever': float(qpos[i][lever_q]), 'paw': paw, 'bin': arr.tobytes()})
    return meta, frames


# ------------------------------------------------------------------ placeholder pons page
def _font(size):
    for name in ('bahnschrift.ttf', 'segoeui.ttf', 'arial.ttf'):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


_SHOTS = {}


def placeholder_shot(caption):
    """1280x800 dark JPEG, 'pons page stream' + a caption. Base64, cached per caption."""
    if caption in _SHOTS:
        return _SHOTS[caption]
    im = Image.new('RGB', (SHOT_W, SHOT_H), (13, 13, 17))
    g = ImageDraw.Draw(im)
    g.rectangle((0, 0, SHOT_W, 64), fill=(19, 19, 24))
    g.text((32, 20), 'placeholder page  (dev harness, not pons)', font=_font(22), fill=(110, 110, 124))
    g.text((SHOT_W // 2, 300), 'pons page stream', font=_font(76), fill=(214, 214, 222), anchor='mm')
    g.text((SHOT_W // 2, 390), caption, font=_font(30), fill=(140, 140, 154), anchor='mm')
    for k, lab in enumerate(('coin image', 'name', 'ticker', 'description')):
        y = 480 + k * 48
        g.rectangle((150, y, 820, y + 34), outline=(44, 44, 54), width=2)
        g.text((166, y + 7), lab, font=_font(18), fill=(84, 84, 96))
    g.rectangle(LAUNCH_BTN, fill=(36, 36, 44), outline=(70, 70, 84), width=2)
    g.text(((LAUNCH_BTN[0] + LAUNCH_BTN[2]) // 2, (LAUNCH_BTN[1] + LAUNCH_BTN[3]) // 2), 'Confirm (placeholder)',
           font=_font(20), fill=(170, 170, 184), anchor='mm')
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=78)
    _SHOTS[caption] = base64.b64encode(buf.getvalue()).decode()
    return _SHOTS[caption]


def fake_tx(meta, proof):
    """Plausible DRY capture for the viewer's tx panel. FAKE: no page built it, nothing decoded it."""
    coin = meta.get('coin') or {'name': 'ratbrain', 'symbol': 'RATBRAIN'}
    desc = f'launched by a virtual rat pressing a lever. replay proof sha256 {proof}'
    fields = {
        'to': FACTORY, 'from': DRY_CREATOR, 'selector': SELECTOR, 'value': '0.0005 ETH',
        'pairToken': ZERO, 'name': coin['name'], 'symbol': coin['symbol'],
        'image': 'ipfs://QmDevHarnessPlaceholderImageCid000000000000000',
        'description': desc, 'socials': ['', '', '', '', ''], 'creator': DRY_CREATOR,
        'creatorTaxBps': 100, 'uint256_0': 0, 'bytes32_a': '0x' + '00' * 32, 'bytes32_b': '0x' + '00' * 32,
        'amountIn': 0,
    }
    checks = {k: True for k in ('to', 'selector', 'value', 'pairToken', 'name', 'symbol', 'description', 'creator')}
    return {'type': 'tx', 'fields': fields, 'checks': checks, 'verdict': 'dev_fake_tx', 'dev': True}


# ------------------------------------------------------------------ app
def make_app(run_dir, speed):
    meta, frames = load_run(run_dir)
    press = meta.get('press') or {}
    proof = press.get('proof') or ''
    rel_run = os.path.relpath(run_dir, ROOT).replace('\\', '/')
    coin = meta.get('coin') or {'name': 'ratbrain', 'symbol': 'RATBRAIN'}
    state = {'busy': False, 'last_run': None}
    app = FastAPI()
    print(f'dev_viewer: {len(frames)} pose frames from {rel_run} ({len(frames) * CTRL_DT:.2f} s sim), '
          f'press frame {press.get("frame")}, proof {proof[:16]}...', flush=True)

    @app.get('/')
    def index():
        return FileResponse(os.path.join(WEB, 'rig.html'), media_type='text/html',
                            headers={'Cache-Control': 'no-store'})

    @app.get('/rat.json')
    def rat_json():
        return FileResponse(RAT_JSON, media_type='application/json')

    @app.get('/status')
    def status():
        return JSONResponse({'mode': 'DRY', 'symbol': coin['symbol'], 'name': coin['name'],
                             'policy_sha256': meta.get('policy_sha256'), 'seed': meta.get('seed'),
                             'busy': state['busy'], 'last_run': state['last_run'], 'dev': True,
                             'dev_note': f'DEV REPLAY of {rel_run}: placeholder pons page, fake tx'})

    @app.websocket('/run')
    async def run_ws(ws: WebSocket):
        await ws.accept()
        try:
            first = json.loads(await ws.receive_text())
        except (WebSocketDisconnect, ValueError):
            return
        if first.get('type') != 'start':
            await ws.send_text(json.dumps({'type': 'log', 'msg': 'expected {"type":"start"}'}))
            await ws.close()
            return
        if state['busy']:
            await ws.send_text(json.dumps({'type': 'log', 'msg': 'busy: one run at a time'}))
            await ws.close()
            return
        state['busy'] = True
        try:
            await replay(ws, first.get('seed'))
        except (WebSocketDisconnect, RuntimeError) as e:   # client went away mid-run
            print('dev_viewer: client left:', repr(e)[:120], flush=True)
        finally:
            state['busy'] = False

    async def replay(ws, seed):
        lock = asyncio.Lock()
        shot = {'caption': 'waiting', 'on': True}

        async def send(m):
            async with lock:
                if isinstance(m, (bytes, bytearray)):
                    await ws.send_bytes(m)
                else:
                    await ws.send_text(json.dumps(m))

        async def log(msg):
            await send({'type': 'log', 'msg': msg})

        async def stage(s, st, detail=''):
            await send({'type': 'stage', 'stage': s, 'state': st, 'detail': detail})

        async def pause(s):
            await asyncio.sleep(s / speed)

        async def shooter():
            while shot['on']:
                await send({'type': 'shot', 'jpg': placeholder_shot(shot['caption']), 'w': SHOT_W, 'h': SHOT_H})
                await asyncio.sleep(1.0 / SHOT_FPS / speed)

        async def click_and_tx():
            """What rig.py does off the sim thread: glide to Confirm, click, then pons asks for a signature."""
            await stage('click_launch', 'active', 'mouse glide to Confirm')
            shot['caption'] = 'the rat pressed: clicking Confirm'
            x0, y0 = 640.0, 420.0
            x1, y1 = (LAUNCH_BTN[0] + LAUNCH_BTN[2]) / 2, (LAUNCH_BTN[1] + LAUNCH_BTN[3]) / 2
            n = 18
            for k in range(n + 1):
                e = k / n
                e = e * e * (3 - 2 * e)
                await send({'type': 'cursor', 'cx': x0 + (x1 - x0) * e, 'cy': y0 + (y1 - y0) * e, 'click': False})
                await pause(0.6 / n)
            await send({'type': 'cursor', 'cx': x1, 'cy': y1, 'click': True})
            await log('clicked Confirm (placeholder page)')
            await stage('click_launch', 'done', 'clicked')
            await stage('tx', 'active', 'waiting for eth_sendTransaction')
            shot['caption'] = 'pons is building the launch transaction'
            await pause(0.45)
            tx = fake_tx(meta, proof)
            await send(tx)
            await log('FAKE tx from the dev harness: pons was not involved, nothing was decoded or checked')
            await stage('tx', 'done', 'FAKE tx (dev harness)')
            shot['caption'] = 'dev replay: no pons, fake tx'

        shooter_task = asyncio.create_task(shooter())
        try:
            if seed is not None and seed != meta.get('seed'):
                await log(f'DEV: seed {seed} requested; this harness replays the recorded seed {meta.get("seed")}')
            await log(f'DEV REPLAY of {rel_run}: the poses are the recorded physics; '
                      'the pons page is a placeholder and the tx is FAKE')
            # --- pre-fill (fake) ---
            pre = [('wallet', 'injected EIP-1193 wallet', f'{DRY_CREATOR[:6]}…{DRY_CREATOR[-4:]} (DRY, no key)'),
                   ('terms', 'terms dialog', 'accepted (owner-authorised)'),
                   ('image', 'uploading live/assets/coin.png', 'coin.png 1000×1000'),
                   ('name', 'typing name', coin['name']),
                   ('ticker', 'typing ticker', coin['symbol']),
                   ('description', 'typing description', 'with the replay proof')]
            for s, cap, detail in pre:
                shot['caption'] = cap
                await stage(s, 'active')
                await log(f'operator script: {cap}')
                await pause(0.55)
                await stage(s, 'done', detail)
            shot['caption'] = 'form filled by the operator script'
            await stage('rehearsal', 'active', 'instant rollout')
            await pause(0.4)
            await send({'type': 'proof', 'proof': proof})
            await log(f'rehearsal proof sha256 {proof}')
            await stage('rehearsal', 'done', proof[:12] + '…')

            # --- the trial: stream the recorded poses at 50 Hz real time ---
            await stage('brain_off', 'active', f'{meta.get("preroll_s")} s pre-roll')
            await log(f'trial: brain OFF for {meta.get("preroll_s")} s, then the policy drives the rat')
            shot['caption'] = 'waiting for the rat'
            t0 = time.perf_counter()
            brain_seen = pressed_seen = False
            click_task = None
            for k, f in enumerate(frames):
                wait = t0 + k * CTRL_DT / speed - time.perf_counter()
                if wait > 0:
                    await asyncio.sleep(wait)
                await send(f['bin'])
                if f['brain'] and not brain_seen:
                    brain_seen = True
                    await stage('brain_off', 'done')
                    await stage('brain_on', 'active', 'policy drives 38 actuators')
                    await log(f'brain ON at t = {f["t"]:.3f} s')
                if f['pressed'] and not pressed_seen:
                    pressed_seen = True
                    deg = float(np.degrees(f['lever']))
                    await send({'type': 'press', 't': round(f['t'], 4), 'proof': proof})
                    await stage('brain_on', 'done')
                    await stage('press', 'done', f'lever {deg:.1f}° at t = {f["t"]:.3f} s')
                    await log(f'PRESS at t = {f["t"]:.3f} s, lever {deg:.1f}°, proof equals the rehearsal')
                    click_task = asyncio.create_task(click_and_tx())
            if click_task:
                await click_task
            await stage('outcome', 'done', 'DEV replay · fake tx')
            state['last_run'] = {'run_dir': rel_run, 'outcome': 'dev_replay', 'dev': True}
            await log('DEV: done. Nothing was signed or sent; this harness never talks to pons.')
            await send({'type': 'done', 'run_dir': rel_run + ' (dev replay)', 'outcome': 'dev_replay'})
        finally:
            shot['on'] = False
            shooter_task.cancel()

    return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=4662)
    ap.add_argument('--run', default=os.path.join('runs', 'film_2026'))
    ap.add_argument('--speed', type=float, default=1.0, help='playback speed (1.0 = real time)')
    a = ap.parse_args()
    run_dir = a.run if os.path.isabs(a.run) else os.path.join(ROOT, a.run)
    app = make_app(run_dir, a.speed)
    uvicorn.run(app, host='127.0.0.1', port=a.port, log_level='warning')


if __name__ == '__main__':
    main()
