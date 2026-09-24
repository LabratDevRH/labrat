"""RATBRAIN rig (live/SPEC.md): the virtual rat in its Skinner box, simulated in real time, next to the
REAL ponsfamily.com launchpad create page driven in headless Chromium. The operator script fills the form
and opens pons's launch review; the rat's lever press clicks Confirm, which makes pons request the launch
transaction. pons hands that transaction to the injected wallet, which in DRY (the default, and the only
mode that can run today) decodes it, checks it and refuses to sign (EIP-1193 4001). Every run is recorded
to runs/rig_<UTC stamp>_seed<seed>/ and `python replay.py <run dir>` must MATCH.

    python live/rig.py [--port 4661] [--seed 2026] [--preroll 3.0] [--policy runs/final/policy.pt]
                       [--headful] [--dev-shots] [--live --confirm SYMBOL]

    GET  /          live/web/rig.html
    GET  /rat.json  live/assets/rat.json (the mesh)
    GET  /status    {mode, symbol, name, policy_sha256, seed, busy, last_run, ...}
    WS   /run       client sends {"type":"start","seed":2026[,"token":...]}; the server streams JSON events
                    (log, stage, shot, cursor, proof, press, tx, done) and one binary pose frame per 50 Hz
                    control step: little-endian float32 [1.0, sim_t, brain_on, pressed, lever_angle,
                    paw_dist_m, 65, 65 x (px,py,pz, qw,qx,qy,qz)] in m.skin_bonebodyid order, from
                    env.d.xpos / env.d.xquat. The handshake is refused unless its Origin is
                    http://localhost:<port> or http://127.0.0.1:<port>; LIVE also needs the per-process
                    token printed at startup (page URL ?token=..., echoed in the start message).

LIVE needs ALL of: .env (the file) RATBRAIN_LIVE=1 with RATBRAIN_RH_KEY, --live, --confirm <SYMBOL>,
launcher.preflight() at startup AND again right before the trial, launcher.config() unchanged since
startup (name, symbol, tax, image, links, live flag, key address), pons's creator tax field taking the
configured tax, launcher.reserve_journal(), a press whose proof equals the rehearsal, and calldata that
passes every check (incl. the pinned image, empty links, no developer buy, canonical ABI encoding). --live
without every startup gate exits before anything opens. No .env exists, so LIVE is impossible today.
"""
import faulthandler
import sys
import threading

# Thread stacks, set before any thread exists. MuJoCo compiling assets/scene.xml (the rat's 38-deep body tree)
# peaks at ~1016 KiB of C stack. How big a thread's stack is by default depends on which python.exe started
# the process: pythoncore's python.exe reserves 3,000,000 bytes, but the Python install manager's in-process
# shim (%LOCALAPPDATA%\Python\bin\python.exe, which is what the desktop app's preview launcher finds for
# "python" on PATH) reserves 1 MiB. With that shim, START overflowed the first worker thread that built a
# LeverEnv and the process died with 0xC00000FD. Every thread the rig creates (the asyncio executor running
# the sim, and anyio's) gets THREAD_STACK instead, whatever python.exe launched it; MuJoCo work never runs on
# the main thread (see big_stack). On Windows this size is committed per thread (~4 MiB each, a few threads).
THREAD_STACK = 4 * 1024 * 1024
threading.stack_size(THREAD_STACK)
try:
    faulthandler.enable()             # a fatal native crash prints every thread's Python stack to stderr
except (RuntimeError, ValueError, AttributeError):
    pass                              # no usable stderr (pythonw)

import argparse  # noqa: E402
import asyncio  # noqa: E402
import base64  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import os  # noqa: E402
import secrets  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402
from eth_account import Account  # noqa: E402

LIVE_DIR = Path(__file__).resolve().parent
ROOT = LIVE_DIR.parent
for p in (str(ROOT), str(LIVE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import launcher  # noqa: E402
import rollout  # noqa: E402
from env import LeverEnv, CTRL_DT  # noqa: E402
from launch_run import freeze  # noqa: E402
import ponsbot  # noqa: E402

import uvicorn  # noqa: E402
from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse  # noqa: E402

STAGES = ['wallet', 'terms', 'image', 'name', 'ticker', 'description', 'rehearsal',
          'brain_off', 'brain_on', 'press', 'click_launch', 'tx', 'outcome']
SHOT_FPS = 7.0            # SPEC: ~6-8 fps, and never more than one shot waiting to be sent
SHOT_QUALITY = 70
STAGE_DETAIL_MAX = 44     # a stepper cell; longer text goes to the log
POSE_HEADER = 7           # magic, sim_t, brain_on, pressed, lever_angle, paw_dist_m, n_bones
COIN_PNG = LIVE_DIR / 'assets' / 'coin.png'
COIN_MAX_BYTES = 2_000_000
SEND_WAIT_S = 30          # after the click: pons must ENTER eth_sendTransaction within this
DRY_HANDLE_WAIT_S = 60    # DRY: decode + check + eth_call + refuse must finish within this (LIVE: no limit)
PIN_KEYS = ('name', 'symbol', 'tax_bps', 'image', 'x', 'website', 'live_env')
WHO_CLICKS = ("The operator script fills the form and opens pons's launch review; the rat's lever press clicks "
              "Confirm, which makes pons request the launch transaction.")

CONFIG = {}
STATE = {'busy': False, 'last_run': None, 'live_used': False}


def key_address(cfg):
    """The address of launcher.config()'s key (None when there is no key). The key itself is never kept."""
    k = cfg.get('_key') or ''
    if not k:
        return None
    try:
        return Account.from_key(k).address
    except Exception:
        return 'unparseable key'


def pin_config(cfg):
    """What must not change between startup and START (LIVE refuses the run if it does)."""
    pub = launcher.public(cfg)
    return {**{k: pub.get(k) for k in PIN_KEYS}, 'key_address': key_address(cfg)}


def config_drift(pinned, cfg):
    now = pin_config(cfg)
    return [k for k in pinned if pinned[k] != now.get(k)]


def big_stack(fn, *args, **kwargs):
    """fn(*args, **kwargs) on a fresh THREAD_STACK thread, for MuJoCo work outside the asyncio executor: the
    main thread's stack is whatever python.exe reserved (1 MiB for some), too small to compile the scene."""
    with ThreadPoolExecutor(1, thread_name_prefix='rig-sim') as ex:
        return ex.submit(fn, *args, **kwargs).result()


def say(*parts):
    try:
        print(' '.join(str(x) for x in parts), flush=True)
    except Exception:
        pass


def rel(p):
    return os.path.relpath(str(p), str(ROOT)).replace('\\', '/')


def utc(ts=None):
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def pose_frame(env, bones, brain_on, pressed):
    """One binary pose frame. Reads env.d only (never calls mj_forward: that would touch the warm start)."""
    d = env.d
    tip = d.site_xpos[env.tip]
    paw = min(float(np.linalg.norm(d.site_xpos[s] - tip)) for s in env.paw_sites)
    out = np.empty(POSE_HEADER + 7 * len(bones), dtype='<f4')
    out[:POSE_HEADER] = (1.0, d.time, 1.0 if brain_on else 0.0, 1.0 if pressed else 0.0,
                         env.lever_angle(), paw, float(len(bones)))
    out[POSE_HEADER:] = np.concatenate([d.xpos[bones], d.xquat[bones]], axis=1).ravel()
    return out.tobytes()


class StageFailed(RuntimeError):
    def __init__(self, stage, detail):
        super().__init__(f'{stage}: {detail}')
        self.stage, self.detail = stage, str(detail)[:400]


class Outbox:
    """One sender for the websocket, fed from the loop and (thread-safely) from the sim thread. Every JSON
    event is also appended to events.jsonl (shot JPEGs replaced by their size + sha256). A viewer that
    disconnects does not stop the run: the run finishes and is recorded regardless."""

    def __init__(self, ws, events_path):
        self.ws = ws
        self.loop = asyncio.get_running_loop()
        self.q = asyncio.Queue()
        self.ok = ws is not None
        self.shots_pending = 0
        self.n = {'json': 0, 'shot': 0, 'pose': 0, 'cursor': 0}
        self.ev = open(events_path, 'w', encoding='utf-8', newline='\n')
        self.task = asyncio.create_task(self._pump())

    def shot(self, raw, w, h):                # loop thread only
        msg = {'type': 'shot', 'jpg': base64.b64encode(raw).decode(), 'w': w, 'h': h}
        rec = {'type': 'shot', 'w': w, 'h': h, 'jpg_bytes': len(raw), 'jpg_sha256': hashlib.sha256(raw).hexdigest()}
        self.shots_pending += 1
        self.n['shot'] += 1
        self.json(msg, rec)

    def json(self, msg, rec=None):            # loop thread only
        rec = rec or msg
        if msg.get('type') == 'cursor':
            self.n['cursor'] += 1
        self.n['json'] += 1
        try:
            self.ev.write(json.dumps({'ts': round(time.time(), 3), **rec}, default=str) + '\n')
        except Exception:
            pass
        self.q.put_nowait(('t', msg))

    def json_ts(self, msg):                   # any thread
        self.loop.call_soon_threadsafe(self.json, msg)

    def pose_ts(self, b):                     # any thread
        self.loop.call_soon_threadsafe(self._pose, b)

    def _pose(self, b):
        self.n['pose'] += 1
        self.q.put_nowait(('b', b))

    async def _pump(self):
        while True:
            kind, item = await self.q.get()
            if kind == 'end':
                return
            if kind == 't' and item.get('type') == 'shot':
                self.shots_pending -= 1
            if not self.ok:
                continue
            try:
                if kind == 't':
                    await self.ws.send_text(json.dumps(item, default=str))
                else:
                    await self.ws.send_bytes(item)
            except Exception:
                self.ok = False               # viewer gone; keep recording

    async def close(self):
        self.q.put_nowait(('end', None))
        try:
            await asyncio.wait_for(self.task, 30)
        except Exception:
            pass
        try:
            self.ev.close()
        except Exception:
            pass


class Session:
    def __init__(self, ws, seed):
        self.ws = ws
        self.seed = int(seed)
        self.mode = CONFIG['mode']
        self.dry = self.mode != 'LIVE'
        self.preroll = CONFIG['preroll']
        self.loop = asyncio.get_running_loop()
        self.stages = {s: 'pending' for s in STAGES}
        self.current = None
        self.stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        self.run_dir = ROOT / 'runs' / f'rig_{self.stamp}_seed{self.seed}'
        self.out = None
        self.cfg = None
        self.bot = None
        self.bones = None
        self.policy = None
        self.proof = None
        self.description = None
        self.tax_bps = None
        self.streaming = False
        self.stream_task = None
        self.env_ref = None
        self.brain_seen = False
        self.click_future = None
        self.click_result = None
        self.sends = []
        self.handled_send = None          # index in self.sends of the one request the rig handled
        self.capture = None
        self.send_entered = asyncio.Event()   # set the moment the press's eth_sendTransaction reaches the rig
        self.tx_seen = asyncio.Event()        # set when the rig has finished handling it (every path)
        self.rejection = None
        self.rejection_task = None
        self.final_jpg = None
        self.image_uri = None
        self.live = None
        self.live_hash = None
        self.live_result = None
        self.trial = None
        self.dev_paths = []
        self.t_start = time.time()

    # ---- events ----------------------------------------------------------------------------------------
    def log(self, msg):
        say('  ' + str(msg)[:400])
        self.out.json({'type': 'log', 'msg': str(msg)})

    def log_ts(self, msg):
        self.loop.call_soon_threadsafe(self.log, msg)

    def stage(self, s, state, detail=''):
        """Stage details stay short (a stepper cell); anything longer also goes to the log in full."""
        if self.stages.get(s) == state:
            return
        self.stages[s] = state
        if state == 'active':
            self.current = s
        detail = str(detail)
        say(f'[stage] {s:<12} {state:<6} {detail[:200]}')
        if len(detail) > STAGE_DETAIL_MAX:
            self.log(f'{s} {state}: {detail}')
            detail = detail[:STAGE_DETAIL_MAX - 1].rstrip() + '…'
        self.out.json({'type': 'stage', 'stage': s, 'state': state, 'detail': detail})

    def on_cursor(self, x, y, click, who):
        self.out.json({'type': 'cursor', 'cx': round(float(x), 1), 'cy': round(float(y), 1),
                       'click': bool(click), 'who': who})

    async def dev_shot(self, name):
        if not CONFIG.get('dev_shots') or not self.bot or not self.bot.page:
            return
        p = LIVE_DIR / 'dev_shots' / f'{self.stamp}_{name}.png'
        try:
            p.parent.mkdir(exist_ok=True)
            await self.bot.page.screenshot(path=str(p))
            self.dev_paths.append(rel(p))
        except Exception as e:
            say(f'  dev shot {name} failed: {e!r}'[:200])

    # ---- the pons stream ---------------------------------------------------------------------------------
    def start_stream(self):
        if not self.streaming:
            self.streaming = True
            self.stream_task = asyncio.create_task(self._stream())

    async def _stream(self):
        period = 1.0 / SHOT_FPS
        w, h = CONFIG['size']
        while self.streaming:
            t0 = time.perf_counter()
            if self.out.shots_pending < 1:        # throttle: never queue shots behind the rat's frames
                try:
                    raw = await self.bot.jpeg(SHOT_QUALITY)
                    self.out.shot(raw, w, h)
                except Exception:
                    pass
            await asyncio.sleep(max(0.01, period - (time.perf_counter() - t0)))

    async def stop_stream(self):
        self.streaming = False
        if self.stream_task:
            try:
                await asyncio.wait_for(self.stream_task, 5)
            except Exception:
                pass

    # ---- the run -----------------------------------------------------------------------------------------
    async def run(self):
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.out = Outbox(self.ws, self.run_dir / 'events.jsonl')
        outcome = {'mode': 'error'}
        try:
            outcome = await self._run()
        except StageFailed as e:
            traceback.print_exc()
            self.stage(e.stage, 'failed', e.detail)
            outcome = {'mode': 'error', 'stage': e.stage, 'error': e.detail, **self._signed_broadcast()}
            self.log(f'run stopped at {e.stage}: {e.detail}')
        except Exception as e:
            traceback.print_exc()
            st = self.current or 'wallet'
            self.stage(st, 'failed', repr(e)[:300])
            outcome = {'mode': 'error', 'stage': st, 'error': repr(e)[:400], **self._signed_broadcast()}
            self.log(f'run failed at {st}: {e!r}'[:300])
        if outcome.get('mode') == 'error' and self.stages['outcome'] != 'done':
            self.stage('outcome', 'failed', outcome.get('error', 'error'))
        saved = False
        try:
            saved = await self._finish(outcome)
        except Exception as e:
            traceback.print_exc()
            self.log(f'recording failed: {e!r}'[:300])
        finally:
            await self.stop_stream()
            if self.bot:
                await self.bot.close()
        STATE['last_run'] = {'run_dir': rel(self.run_dir), 'outcome': outcome.get('mode'), 'ended': utc(),
                             'recorded': saved}
        self.out.json({'type': 'done', 'run_dir': rel(self.run_dir), 'outcome': outcome.get('mode'),
                       'launch': outcome, 'recorded': saved,
                       'replay': f'python replay.py {rel(self.run_dir)}' if saved else None})
        await self.out.close()
        return outcome

    async def _run(self):
        cfg = self.cfg = launcher.config()
        drift = config_drift(CONFIG['pinned'], cfg)
        if drift:
            self.current = 'wallet'
            raise StageFailed('wallet', f'launcher.config() changed since the rig started ({", ".join(drift)}); '
                                        'refusing the run: restart the rig')
        self.log(f"RATBRAIN rig · {'DRY RUN' if self.dry else 'LIVE'} · coin {cfg['name']} (${cfg['symbol']}) · "
                 f"seed {self.seed} · pre-roll {self.preroll:g} s · policy sha256 {CONFIG['policy_sha256'][:16]}")
        if self.dry:
            self.log('DRY: pons builds the launch transaction, the rig decodes it and refuses to sign. '
                     'Nothing is signed or broadcast.')
        self.log(WHO_CLICKS)
        # the policy is frozen into the run dir (read-only), so nothing can change the weights under the run
        self.policy = await asyncio.to_thread(freeze, CONFIG['policy'], str(self.run_dir))
        # the instant rehearsal runs while pons loads: it gives the proof that goes into the description
        rehearsal = asyncio.ensure_future(asyncio.to_thread(
            rollout.run, CONFIG['policy'], self.seed, preroll_s=self.preroll))
        await asyncio.to_thread(self._rest_pose)

        # ---- wallet
        self.stage('wallet', 'active', 'injecting an EIP-1193 wallet')
        if self.dry:
            acct, kind = ponsbot.throwaway_account(), 'throwaway in-memory key (DRY)'
        else:
            acct, kind = CONFIG['live']['pre']['acct'], 'funded .env wallet (LIVE)'
        self.bot = ponsbot.PonsBot(acct, self.mode, log=self.log, headful=CONFIG['headful'],
                                   size=CONFIG['size'], theme=CONFIG['theme'], page_cursor=CONFIG['page_cursor'],
                                   symbol=cfg['symbol'])
        self.bot.on_send = self.on_send
        self.bot.on_cursor = self.on_cursor
        await self.bot.start()
        self.log(f'opening {ponsbot.URL}')
        try:
            await self.bot.goto()
        except Exception as e:
            raise StageFailed('wallet', f'pons did not load: {e}')
        self.start_stream()
        try:
            st = await self.bot.settle()
        except Exception as e:
            raise StageFailed('wallet', f'the create form did not appear: {e}')
        if not st['connected']:
            raise StageFailed('wallet', f"pons did not show the injected wallet as connected ({st['page_wallet']})")
        if self.dry:
            self.log('DRY: pons reads the wallet balance through its own RPC proxy. For this throwaway address '
                     'the rig answers those reads with a simulated 1 ETH (eth_call state override) so pons '
                     'enables its launch button. The wallet holds nothing; the rig never signs.')
        self.log(f'wallet {acct.address} connected in pons · {kind} · pons theme {st["theme"]}')
        self.stage('wallet', 'done', f"{ponsbot.short(acct.address)} · {'throwaway (DRY)' if self.dry else 'funded (LIVE)'}")
        await self.dev_shot('wallet')

        meta_r, _, _ = await rehearsal
        press = meta_r.get('press')
        if not press:
            raise StageFailed('rehearsal', f'the instant rehearsal did not press with seed {self.seed}; '
                                           'there is nothing to launch')
        self.proof = press['proof']
        if not self.dry and self.proof != CONFIG['live']['proof']:
            raise StageFailed('rehearsal', 'the rehearsal proof differs from the one preflighted at startup')
        self.log(f"instant rehearsal: press at brain step {press['ctrl_step']} ({press['sim_time']:.2f} s after "
                 f"brain-on), lever {math.degrees(press['lever_angle']):.1f}°, proof {self.proof}")
        self.out.json({'type': 'proof', 'proof': self.proof})

        # ---- terms
        self.log("pons's Terms of Use, Privacy Policy and jurisdiction attestation: accepted by the owner, "
                 'who authorized the rig to click them')
        self.stage('terms', 'active', 'owner-authorized accept')
        try:
            r = await self.bot.accept_terms(before_accept=lambda: self.dev_shot('terms'))
        except Exception as e:
            raise StageFailed('terms', str(e))
        self.stage('terms', 'done', f"accepted ({r.get('ticked', 0)} boxes)" if r.get('shown') else 'no terms dialog shown')

        # ---- image
        self.stage('image', 'active', 'coin.png via "Choose image"')
        if not COIN_PNG.exists():
            raise StageFailed('image', f'{rel(COIN_PNG)} is missing (run live/export_assets.py)')
        size = COIN_PNG.stat().st_size
        if size > COIN_MAX_BYTES:
            raise StageFailed('image', f'coin.png is {size} bytes, over pons\'s ~2 MB limit')
        try:
            r = await self.bot.upload_image(COIN_PNG)
        except Exception as e:
            raise StageFailed('image', str(e))
        ip = r.get('ipfs') or {}
        self.image_uri = r.get('uri')
        try:
            body = json.loads(ip.get('body') or '{}')
            pinned = f"cid {body.get('cid')} · {body.get('uri')}" if isinstance(body, dict) and body.get('cid') else None
        except ValueError:
            pinned = None
        # (spaces on purpose: one long unbroken token can stretch a log pane)
        self.log(f"pons pinned the image ({r['how']}, HTTP {ip.get('status')}): "
                 + (pinned or str(ip.get('body') or ip.get('error'))[:120].replace(',', ', ')))
        if self.image_uri:
            self.log(f'the launch calldata must carry exactly this image: {self.image_uri}')
        elif not self.dry:
            raise StageFailed('image', 'pons did not return an ipfs:// URI for the image; LIVE cannot check it')
        else:
            self.log("pons did not return an ipfs:// URI for the image: the tx 'image' check will fail")
        self.stage('image', 'done', f'coin.png {size / 1e6:.2f} MB · Image ready')

        # ---- name, ticker
        for key, want in (('name', cfg['name']), ('ticker', cfg['symbol'])):
            self.stage(key, 'active', f'typing {want}')
            try:
                val = await self.bot.type_field(key, want)
            except Exception as e:
                raise StageFailed(key, str(e))
            if val != want:
                raise StageFailed(key, f'the field reads {val!r}, not {want!r}')
            self.stage(key, 'done', val)

        # ---- description (+ creator tax)
        self.description = launcher.description(self.proof)
        self.stage('description', 'active', 'typing, with the proof')
        try:
            val = await self.bot.type_field('description', self.description, delay=16)
        except Exception as e:
            raise StageFailed('description', str(e))
        if val != self.description:
            raise StageFailed('description', f'the field reads {val!r}')
        await self.dev_shot('filled')
        pct = f"{cfg['tax_bps'] / 100:g}"
        tax_why = None
        try:
            tv = await self.bot.set_creator_tax(pct)
            if tv == pct:
                self.tax_bps = cfg['tax_bps']
                self.log(f'creator tax set to {tv}% (Advanced), launcher.config tax_bps {cfg["tax_bps"]}')
            else:
                tax_why = f'the creator tax field reads {tv!r}, wanted {pct}'
        except Exception as e:
            tax_why = f'the creator tax could not be set ({str(e)[:120]})'
        if tax_why:
            if not self.dry:
                raise StageFailed('description', f'{tax_why}; LIVE needs the tax set to launcher.config')
            self.log(f"{tax_why}: the tx 'creatorTaxBps' check will fail")
        await self.dev_shot('tax')
        self.stage('description', 'done', f'proof {self.proof[:10]}… · tax '
                                          f"{pct + '%' if self.tax_bps is not None else 'unset'}")

        # ---- rehearsal: reproduce the proof, read it back off the page, arm pons's launch
        self.log('re-running the instant rehearsal on the frozen policy copy, reading the proof back off the page')
        self.stage('rehearsal', 'active', 're-running, checking the page')
        meta2, _, _ = await asyncio.to_thread(rollout.run, self.policy, self.seed, preroll_s=self.preroll)
        p2 = (meta2.get('press') or {}).get('proof')
        if p2 != self.proof:
            raise StageFailed('rehearsal', f'the rehearsal is not reproducible: {p2} vs {self.proof}')
        on_page = await self.bot.read_field('description')
        if on_page != self.description:
            raise StageFailed('rehearsal', 'the description on the page no longer carries the proof')
        if not self.dry:
            await self._live_arm()
        try:
            rv = await self.bot.open_review()
        except Exception as e:
            raise StageFailed('rehearsal', f"could not open pons's review dialog: {e}")
        self.log("the operator script pressed pons's \"" + rv['launch_label'] + "\", which opens pons's launch "
                 "review (it sends nothing); the review: " + ' · '.join(rv['dialog'][:24]))
        await self.bot.park()
        await self.dev_shot('review')
        self.log(f"proof reproduced ({p2}) and on the page; pons's review \"{rv['title']}\" is open and the "
                 f"rat's press will click its {rv['target']}, which makes pons request the launch transaction")
        self.stage('rehearsal', 'done', f"reproduced {p2[:10]}… · {rv['target']} armed")

        # ---- the trial, in real time
        self.log(f'the trial: the rat stands in the box with its brain off for {self.preroll:g} s, then the '
                 'trained network drives its 38 actuators at 50 Hz, in real time')
        self.stage('brain_off', 'active', f'{self.preroll:g} s pre-roll')
        meta, frames, acts = await self.loop.run_in_executor(None, self._trial)
        self.trial = (meta, frames, acts)
        press = meta.get('press')
        say(f'  trial over: {len(frames)} frames, {len(acts)} actions, press {bool(press)}')
        if not press:
            if not self.brain_seen:
                self._brain_on_stage()
            why = 'the rat did not press this run' + (' (it fell)' if meta.get('fell') else '')
            self._live_abort_unsigned(why)
            self.stage('press', 'failed', why)
            self.stage('outcome', 'done', 'no press, nothing launched')
            return {'mode': 'no_press', 'fell': bool(meta.get('fell')), **self._signed_broadcast()}
        if press['proof'] != self.proof:
            self._live_abort_unsigned(f"the press proof {press['proof']} differs from the rehearsal {self.proof}",
                                      press_proof=press['proof'])
            self.stage('outcome', 'done', 'the press proof differs from the rehearsal: nothing was clicked')
            return {'mode': 'refused_proof_mismatch', 'expected': self.proof, 'got': press['proof'],
                    **self._signed_broadcast()}
        if self.click_future:
            try:
                await asyncio.wait_for(asyncio.wrap_future(self.click_future), 20)
            except Exception as e:
                raise StageFailed('click_launch', f'the launch click failed: {e!r}')
        # pons must ENTER eth_sendTransaction soon after the click (the rig sets send_entered first thing)
        try:
            await asyncio.wait_for(self.send_entered.wait(), SEND_WAIT_S)
        except asyncio.TimeoutError:
            raise StageFailed('tx', f'pons never called eth_sendTransaction within {SEND_WAIT_S} s of the click')
        if self.dry:
            try:
                await asyncio.wait_for(self.tx_seen.wait(), DRY_HANDLE_WAIT_S)
            except asyncio.TimeoutError:
                raise StageFailed('tx', f'the DRY wallet hook did not finish within {DRY_HANDLE_WAIT_S} s')
        else:
            # LIVE: sign_and_send makes blocking RPCs with retries; never abandon it half way on a short timer
            await self.tx_seen.wait()
        if self.rejection_task:
            await self.rejection_task
        return await self._outcome()

    async def _outcome(self):
        cap = self.capture or {}
        if self.dry:
            sim = cap.get('simulation') or {}
            failed = [k for k, v in (cap.get('checks') or {}).items() if not v.get('ok')]
            outcome = {'mode': cap.get('verdict'), 'wallet': 'throwaway in-memory key (DRY)',
                       'creator': self.bot.address, 'description': (cap.get('fields') or {}).get('description'),
                       'checks_passed': not failed, 'failed_checks': failed,
                       'predicted_token': sim.get('predicted_token'), 'predicted_curve': sim.get('predicted_curve'),
                       'simulation_ok': sim.get('ok'), 'refused_with': cap.get('response_to_page'),
                       'page_showed': self.rejection, 'signed': False, 'broadcast': False,
                       'captured_tx': 'captured_tx.json'}
            msg = ('DRY RUN: pons built the launch tx, the rig refused to sign. No coin was created.'
                   if not failed else f'DRY RUN: the captured tx failed {failed}; refused. No coin was created.')
            self.log(msg)
            self.stage('outcome', 'done', 'DRY: refused · no coin' if not failed else 'DRY: mismatch · refused')
            return outcome
        if not self.live or not self.live.sent:
            self.stage('outcome', 'done', f"LIVE: nothing was sent ({cap.get('verdict')})")
            return {'mode': cap.get('verdict') or 'live_not_sent', 'error': cap.get('error'),
                    **self._signed_broadcast()}
        self.log(f'LIVE: waiting for the receipt of {self.live_hash}')
        out = await self.loop.run_in_executor(None, self.live.finish)
        self.live_result = out
        self.stage('outcome', 'done', f"LIVE: {out.get('mode')} {out.get('explorer', '')}")
        return {**out, **self._signed_broadcast()}

    # ---- LIVE bookkeeping (all no-ops in DRY: DRY never reserves, writes or reads launch_journal.json) --------
    def _signed_broadcast(self):
        """{'signed', 'broadcast'} from what actually happened, never assumed: LiveLaunch.sent, else a raw
        signed tx in the journal this run reserved. broadcast is True / False / 'unknown'."""
        if self.dry or self.live is None:
            return {'signed': False, 'broadcast': False}
        sent = self.live.sent
        if sent:
            return {'signed': True, 'broadcast': sent.get('broadcast', ponsbot.broadcast_state(sent.get('send_error'))),
                    'send_error': sent.get('send_error')}
        try:
            with open(launcher.JOURNAL, encoding='utf-8') as fh:
                if 'raw' in json.load(fh):
                    return {'signed': True, 'broadcast': 'unknown'}
        except Exception:
            pass
        return {'signed': False, 'broadcast': False}

    def _live_abort_unsigned(self, reason, **extra):
        """LIVE: close the reserved journal as aborted_before_sign with the reason (and make any later sign
        attempt impossible). Never in DRY."""
        if self.dry or self.live is None:
            return False
        try:
            done = self.live.abort_unsigned(str(reason)[:400], **extra)
        except Exception as e:
            self.log(f'LIVE: could not write the journal: {e!r}'[:300])
            return False
        if done:
            self.log(f'LIVE: nothing was signed; launch_journal.json says aborted_before_sign ({str(reason)[:160]})')
        return done

    async def _live_finalize(self, outcome):
        """LIVE, before recording: let an in-flight wallet hook finish, close an unsigned launch in the journal,
        poll the receipt of a sent one (live.finish) if that has not happened yet, and derive signed/broadcast.
        Mutates outcome in place."""
        if self.dry or self.live is None:
            return
        if self.send_entered.is_set() and not self.tx_seen.is_set():
            self.log('LIVE: waiting for the wallet hook to finish before recording')
            await self.tx_seen.wait()
        if not self.live.sent:
            why = outcome.get('error') or outcome.get('mode') or 'the run ended'
            if not self._live_abort_unsigned(f"run ended without signing: {why}", stage=outcome.get('stage')) \
                    and self.send_entered.is_set():
                await self.tx_seen.wait()
        if self.live.sent and self.live_result is None:
            self.log(f"LIVE: a signed tx exists ({self.live.sent['tx']}); polling its receipt before recording")
            try:
                res = await self.loop.run_in_executor(None, self.live.finish)
            except Exception as e:
                res = {'mode': 'sent_unconfirmed', 'tx': self.live.sent['tx'], 'finish_error': repr(e)[:300]}
            self.live_result = res
            prev = dict(outcome)
            outcome.clear()
            outcome.update(res)
            if prev.get('mode') == 'error':
                outcome['rig_error'] = {'stage': prev.get('stage'), 'error': prev.get('error')}
        outcome.update(self._signed_broadcast())

    async def _finish(self, outcome):
        """Record: rollout.save (qpos.npy, actions.npy, run.json with run.json['launch'] = outcome),
        captured_tx.json, pons_final.jpg. events.jsonl is written as the run goes. LIVE: _live_finalize first
        (a sent tx is always polled and recorded, even when the run failed after sending)."""
        await self._live_finalize(outcome)
        if self.bot and self.bot.page and self.final_jpg is None:
            try:
                self.final_jpg = await self.bot.jpeg(88)
            except Exception:
                pass
        await asyncio.sleep(1.2)                 # let the final state stream for a moment
        if self.final_jpg:
            (self.run_dir / 'pons_final.jpg').write_bytes(self.final_jpg)
        cap = dict(self.capture) if self.capture else {'captured': False}
        cap['other_send_requests'] = [s for i, s in enumerate(self.sends) if i != self.handled_send]
        with open(self.run_dir / 'captured_tx.json', 'w', encoding='utf-8', newline='\n') as f:
            json.dump(cap, f, indent=2, default=str)
        if not self.trial:
            return False
        meta, frames, acts = self.trial
        meta['press_hook'] = meta.get('launch')
        meta['launch'] = outcome
        meta['coin'] = {'name': self.cfg['name'], 'symbol': self.cfg['symbol']}
        meta['live_requested'] = not self.dry
        meta['source_policy'] = rel(CONFIG['policy'])
        meta['rig'] = {
            'spec': 'live/SPEC.md', 'mode': self.mode, 'pons_url': ponsbot.URL, 'viewport': list(CONFIG['size']),
            'wallet': self.bot.address if self.bot else None,
            'wallet_kind': 'throwaway in-memory key' if self.dry else 'funded .env wallet',
            'dry_balance_override_eth': ponsbot.DRY_BALANCE_WEI / 1e18 if self.dry else None,
            'rehearsal_proof': self.proof, 'description_typed': self.description,
            'creator_tax_bps_set': self.tax_bps, 'image_pinned': self.image_uri, 'click': self.click_result,
            'send_requests': len(self.sends), 'signature_requests': self.bot.signatures if self.bot else [],
            'wallet_refusals': self.bot.refusals if self.bot else [],
            'config_pinned_at_startup': CONFIG.get('pinned'),
            'page_rejection': self.rejection, 'stages': self.stages,
            'streamed': dict(self.out.n), 'dev_shots': self.dev_paths,
            'started_utc': utc(self.t_start), 'ended_utc': utc(),
        }
        await asyncio.to_thread(rollout.save, str(self.run_dir), meta, frames, acts)
        self.log(f'recorded {rel(self.run_dir)}: qpos.npy {len(frames)} frames, actions.npy {len(acts)}, '
                 f'run.json launch={outcome.get("mode")}, captured_tx.json, events.jsonl, pons_final.jpg')
        return True

    # ---- sim thread --------------------------------------------------------------------------------------
    def _rest_pose(self):
        env = LeverEnv(self.seed)
        env.reset(seed=self.seed)
        self.bones = np.asarray(env.m.skin_bonebodyid, dtype=np.int64)
        self.out.pose_ts(pose_frame(env, self.bones, False, False))

    def _trial(self):
        return rollout.run(self.policy, self.seed, on_press=self._on_press, preroll_s=self.preroll,
                           on_step=self._on_step, realtime=True)

    def _on_step(self, env, phase, info):
        self.env_ref = env
        brain = phase == 'brain'
        if brain and not self.brain_seen:
            self.brain_seen = True
            self.loop.call_soon_threadsafe(self._brain_on_stage)
        pressed = bool(info and info.get('pressed'))
        self.out.pose_ts(pose_frame(env, self.bones, brain, pressed))

    def _on_press(self, proof):
        """Sim thread, the instant the press registers. Never blocks the sim: the click is scheduled on the
        loop and this returns at once."""
        env = self.env_ref
        info = {}
        if env is not None:
            info = {'t': float(env.d.time), 'brain_t': round(env.t * CTRL_DT, 4), 'ctrl_step': int(env.t),
                    'lever_angle': float(env.lever_angle())}
        ok = proof == self.proof
        self.loop.call_soon_threadsafe(self._press_stage, proof, info, ok)
        if not ok:
            return {'mode': 'refused_proof_mismatch', 'expected': self.proof, 'got': proof}
        self.click_future = asyncio.run_coroutine_threadsafe(self._click_launch(), self.loop)
        return {'mode': 'launch_click_dispatched', 'wall_time': time.time()}

    # ---- loop callbacks ----------------------------------------------------------------------------------
    def _brain_on_stage(self):
        self.stage('brain_off', 'done', f'{self.preroll:g} s brain off')
        self.stage('brain_on', 'active', 'policy at 50 Hz')

    def _press_stage(self, proof, info, ok):
        self.out.json({'type': 'press', 't': info.get('t'), 'brain_t': info.get('brain_t'),
                       'ctrl_step': info.get('ctrl_step'), 'lever_angle': info.get('lever_angle'),
                       'proof': proof, 'matches_rehearsal': ok})
        if self.stages['brain_on'] == 'pending':
            self._brain_on_stage()
        bt = info.get('brain_t')
        self.stage('brain_on', 'done', f'pressed at +{bt:.2f} s' if bt is not None else 'pressed')
        deg = math.degrees(info['lever_angle']) if info.get('lever_angle') is not None else float('nan')
        self.stage('press', 'active', f'lever {deg:.1f}°')
        if ok:
            self.log(f'PRESS: lever {deg:.1f}°, proof {proof} equals the rehearsal; dispatching the launch click')
            self.stage('press', 'done', f'lever {deg:.1f}° · proof matches')
            self.stage('click_launch', 'active', 'gliding to Confirm')
        else:
            self.log(f'the press proof {proof} differs from the rehearsal {self.proof}: refusing to click')
            self.stage('press', 'failed', 'proof mismatch: no click')

    async def _click_launch(self):
        self.bot.arm()
        try:
            r = await self.bot.click_launch()
        except Exception as e:
            self.stage('click_launch', 'failed', repr(e)[:200])
            raise
        self.click_result = r
        self.log(f"the press clicked pons's {r['label']} at ({r['x']}, {r['y']})")
        self._click_done()
        asyncio.ensure_future(self._after_click_shot())
        return r

    async def _after_click_shot(self):
        await asyncio.sleep(0.3)
        await self.dev_shot('after_click')

    def _click_done(self):
        if self.stages['click_launch'] != 'done':
            lab = (self.click_result or {}).get('label', 'Confirm')
            self.stage('click_launch', 'done', f'clicked {lab}')
        if self.stages['tx'] == 'pending':
            self.stage('tx', 'active', 'waiting for eth_sendTransaction')

    async def on_send(self, tx):
        """pons's eth_sendTransaction. Requests before the lever press are refused and do not count; the
        first one after it is THE launch request (send_entered is set at once, tx_seen when it has been
        handled, on every path); any later one is refused. DRY: decode, check, report, refuse with 4001.
        LIVE: LiveLaunch.sign_and_send, awaited to the end however long its RPCs take."""
        armed = bool(self.bot.armed)
        self.sends.append({'tx': tx, 'at': time.time(), 'armed': armed})
        n = len(self.sends)
        data = str(tx.get('data') or tx.get('input') or '')
        self.log(f"pons called eth_sendTransaction #{n}: to {tx.get('to')} value {tx.get('value')} "
                 f"calldata {max(0, len(data) - 2) // 2} bytes")
        if not armed or self.handled_send is not None:
            why = 'no lever press yet' if not armed else 'one launch per press'
            self.log(f'refused ({why})')
            return ponsbot.refusal(ponsbot.DRY_REFUSAL if self.dry else f'rat rig refused: {why}')
        self.handled_send = n - 1
        self.send_entered.set()
        try:
            return await self._handle_send(tx)
        finally:
            self.tx_seen.set()

    async def _handle_send(self, tx):
        self._click_done()
        fields, data_hex = ponsbot.decode_launch(tx)
        expect = {'name': self.cfg['name'], 'symbol': self.cfg['symbol'], 'proof': self.proof,
                  'creator': self.bot.address, 'tax_bps': self.cfg['tax_bps'],
                  'tax_set': self.tax_bps == self.cfg['tax_bps'], 'image': self.image_uri,
                  'x': self.cfg['x'], 'website': self.cfg['website']}
        checks = ponsbot.check_launch(fields, expect)
        ok = ponsbot.all_ok(checks) and data_hex is not None
        failed = [k for k, v in checks.items() if not v['ok']] + ([] if data_hex else ['decode'])
        if fields.get('decode_error'):
            self.log(f"decode error: {fields['decode_error']}")
        self.log(f"decoded: {fields.get('name')} / {fields.get('symbol')} · creator {fields.get('creator')} · "
                 f"pair {fields.get('pairToken')} · {fields.get('value')} · image {fields.get('image')}")
        self.log(f"pons's two bytes32 fields: {fields.get('bytes32_a')} {fields.get('bytes32_b')}")
        self.log(f"links {json.dumps(fields.get('socials'))} · developer buy (amountIn) {fields.get('amountIn')} · "
                 f"uint256 {fields.get('uint256_0')} · trailing bytes {fields.get('trailingBytes')} · creator tax "
                 f"{fields.get('creatorTaxBps')} bps · ABI encoding {fields.get('abi_encoding')}")
        base = {'raw_request': tx, 'fields': fields, 'checks': checks, 'expected': expect, 'mode': self.mode,
                'wallet': self.bot.address, 'at': utc(), 'at_unix': time.time(),
                'decoded_calldata_sha256': hashlib.sha256(bytes.fromhex(data_hex[2:])).hexdigest() if data_hex else None}
        if self.dry:
            try:
                sim = await asyncio.wait_for(self.loop.run_in_executor(
                    None, ponsbot.simulate, tx, self.bot.address, True, data_hex), 15)
            except Exception as e:
                sim = {'ok': False, 'error': f'simulation skipped: {e!r}'[:200]}
            verdict = 'dry_captured' if ok else 'dry_refused_mismatch'
            resp = ponsbot.refusal()
            self.capture = {**base, 'verdict': verdict, 'simulation': sim, 'response_to_page': resp,
                            'signed': False, 'broadcast': False}
            self.out.json({'type': 'tx', 'fields': fields, 'checks': checks, 'verdict': verdict, 'mode': 'DRY',
                           'simulation': sim})
            if sim.get('ok'):
                self.log(f"eth_call of pons's exact tx (simulated 1 ETH balance): would create token "
                         f"{sim.get('predicted_token')}, curve {sim.get('predicted_curve')}, gas {sim.get('gas_estimate')}")
            else:
                self.log(f"eth_call of pons's exact tx failed: {sim.get('error')}")
            self.stage('tx', 'done' if ok else 'failed', f'{len(checks) - len(failed)}/{len(checks)} checks · {verdict}')
            self.stage('outcome', 'active', f'refusing ({ponsbot.USER_REJECTED})')
            self.log(f'refused with {ponsbot.USER_REJECTED} "{ponsbot.DRY_REFUSAL}". Nothing signed, nothing broadcast.')
            self.rejection_task = asyncio.ensure_future(self._watch_rejection())
            return resp
        # ---- LIVE (unreachable without .env + --live --confirm + preflight + journal; never run here)
        if not ok or self.live is None:
            reason = f'calldata failed checks {failed}' if not ok else 'LIVE was not armed'
            self._live_abort_unsigned(reason, fields=fields)   # the journal was reserved: nothing was signed
            self.capture = {**base, 'verdict': 'live_refused_mismatch', 'error': reason, **self._signed_broadcast()}
            self.out.json({'type': 'tx', 'fields': fields, 'checks': checks, 'verdict': 'live_refused_mismatch',
                           'mode': 'LIVE'})
            self.stage('tx', 'failed', reason)
            return ponsbot.refusal(f'rat rig refused: {reason}')
        try:
            h, send_error = await self.loop.run_in_executor(
                None, self.live.sign_and_send, tx, fields, checks, data_hex)
        except Exception as e:
            self.capture = {**base, 'verdict': 'live_refused', 'error': str(e)[:300], **self._signed_broadcast()}
            self.out.json({'type': 'tx', 'fields': fields, 'checks': checks, 'verdict': 'live_refused',
                           'mode': 'LIVE', 'error': str(e)[:200]})
            self.stage('tx', 'failed', f'refused: {e}')
            return ponsbot.refusal(f'rat rig refused: {e}'[:200])
        self.live_hash = h
        sb = self._signed_broadcast()
        verdict = 'live_sent' if sb['broadcast'] is True else (
            'live_send_unknown' if sb['broadcast'] == 'unknown' else 'live_send_rejected')
        self.capture = {**base, 'verdict': verdict, 'hash': h, **sb}
        self.out.json({'type': 'tx', 'fields': fields, 'checks': checks, 'verdict': verdict, 'mode': 'LIVE',
                       'hash': h, 'broadcast': sb['broadcast'], 'send_error': send_error})
        if send_error:
            self.log(f'LIVE send error: {send_error}')
        self.stage('tx', 'done' if sb['broadcast'] is True else 'failed',
                   f"signed (nonce 0) · broadcast {sb['broadcast']} · {h}")
        self.stage('outcome', 'active', 'waiting for the receipt')
        if sb['broadcast'] is False:        # every RPC rejected the raw tx: do not hand pons a hash as if sent
            return ponsbot.err(-32000, f'rat rig: the launch tx was rejected by every RPC: {send_error}'[:200])
        return {'result': h}

    async def _watch_rejection(self):
        hit = await self.bot.wait_rejection(8.0)
        self.rejection = hit
        try:
            self.final_jpg = await self.bot.jpeg(88)
        except Exception:
            pass
        if hit:
            self.log(f'pons shows: {hit}')
            await self.dev_shot('rejected')
        else:
            self.log('pons showed no rejection message within 8 s')

    async def _live_arm(self):
        """LIVE only: preflight again with fresh gas figures, right before the trial, then reserve the
        journal (exclusive create: a second launch is impossible)."""
        drift = config_drift(CONFIG['pinned'], launcher.config())
        if drift:
            raise StageFailed('rehearsal', f'launcher.config() changed since the rig started ({", ".join(drift)})')
        if self.tax_bps != self.cfg['tax_bps']:
            raise StageFailed('rehearsal', 'the creator tax on the page is not launcher.config tax_bps')
        if not self.image_uri:
            raise StageFailed('rehearsal', 'no pinned image URI to check the calldata against')
        try:
            pre = await asyncio.to_thread(launcher.preflight, self.cfg, self.proof)
        except launcher.LaunchRefused as e:
            raise StageFailed('rehearsal', f'preflight refused: {e}')
        if pre['creator'].lower() != self.bot.address.lower():
            raise StageFailed('rehearsal', 'the preflight wallet is not the wallet in the page')
        try:
            launcher.reserve_journal({'creator': pre['creator'], 'policy_sha256': CONFIG['policy_sha256'],
                                      'seed': self.seed, 'expected_proof': self.proof, 'out': rel(self.run_dir),
                                      'via': 'live/rig.py'})
        except FileExistsError:
            raise StageFailed('rehearsal', f'{launcher.JOURNAL} exists: a launch was already attempted')
        STATE['live_used'] = True
        self.live = ponsbot.LiveLaunch(self.cfg, pre, self.proof, log=self.log_ts)
        self.log(f"LIVE armed: preflight passed (gas {pre['gas']}, maxFee {pre['max_fee'] / 1e9:.3f} gwei, "
                 f"balance {pre['balance'] / 1e18:.6f} ETH), journal reserved; the press sends ONE tx at nonce 0")


# ------------------------------------------------------------------------------------------------------------
# HTTP + WS
# ------------------------------------------------------------------------------------------------------------
app = FastAPI()
app.add_middleware(GZipMiddleware, minimum_size=2048)


@app.get('/')
async def index():
    p = LIVE_DIR / 'web' / 'rig.html'
    if not p.exists():
        return HTMLResponse('<!doctype html><title>RATBRAIN rig</title><body style="background:#07070a;'
                            'color:#ddd;font:14px system-ui">live/web/rig.html is not built yet.</body>')
    return FileResponse(p, media_type='text/html')


@app.get('/rat.json')
async def rat_json():
    p = LIVE_DIR / 'assets' / 'rat.json'
    if not p.exists():
        return JSONResponse({'error': 'live/assets/rat.json missing: run python live/export_assets.py'}, 404)
    return FileResponse(p, media_type='application/json')


@app.get('/coin.png')
async def coin_png():
    return FileResponse(COIN_PNG, media_type='image/png') if COIN_PNG.exists() else JSONResponse({}, 404)


@app.get('/status')
async def status():
    return {'mode': CONFIG['mode'], 'symbol': CONFIG['symbol'], 'name': CONFIG['name'],
            'policy_sha256': CONFIG['policy_sha256'], 'policy': rel(CONFIG['policy']), 'seed': CONFIG['seed'],
            'preroll_s': CONFIG['preroll'], 'busy': STATE['busy'], 'last_run': STATE['last_run'],
            'venue': 'ponsfamily.com/launchpad', 'chain': 'Robinhood Chain 4663',
            'pons_viewport': list(CONFIG['size']), 'token_required': bool(CONFIG.get('token')),
            'dry_balance_override_eth': ponsbot.DRY_BALANCE_WEI / 1e18 if CONFIG['mode'] != 'LIVE' else None}


def allowed_origins():
    return {f"http://localhost:{CONFIG['port']}", f"http://127.0.0.1:{CONFIG['port']}"}


@app.websocket('/run')
async def ws_run(ws: WebSocket):
    # only this rig's own page (or record.py / the test client, which open it on localhost) may drive it:
    # a page on any other origin cannot open this socket (cross-site WebSocket hijacking)
    origin = ws.headers.get('origin')
    if origin not in allowed_origins():
        say(f'  refused a /run websocket from origin {origin!r}')
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        while True:
            try:
                msg = json.loads(await ws.receive_text())
            except (ValueError, TypeError):
                continue
            if not isinstance(msg, dict) or (msg.get('type') or msg.get('action')) != 'start':
                continue
            if CONFIG.get('token') and not hmac.compare_digest(str(msg.get('token') or ''), CONFIG['token']):
                say('  refused a LIVE start without the startup token')
                await ws.send_text(json.dumps({'type': 'log', 'msg': 'LIVE: the start message lacks the startup '
                                                                     'token (open the URL the rig printed)'}))
                continue
            if STATE['busy']:
                await ws.send_text(json.dumps({'type': 'log', 'msg': 'the rig is busy: one run at a time'}))
                continue
            try:
                seed = int(msg.get('seed', CONFIG['seed']))
            except (TypeError, ValueError):
                await ws.send_text(json.dumps({'type': 'log', 'msg': f"bad seed {msg.get('seed')!r}"}))
                continue
            if CONFIG['mode'] == 'LIVE':
                if seed != CONFIG['seed']:
                    await ws.send_text(json.dumps({'type': 'log', 'msg': f"LIVE runs only the preflighted seed {CONFIG['seed']}"}))
                    continue
                if STATE['live_used']:
                    await ws.send_text(json.dumps({'type': 'log', 'msg': 'LIVE: this rig already used its one launch'}))
                    continue
            STATE['busy'] = True
            try:
                await Session(ws, seed).run()
            except Exception:
                traceback.print_exc()
            finally:
                STATE['busy'] = False
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        pass


# ------------------------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------------------------
def live_gate(a, cfg, policy):
    """Every startup gate for LIVE. Any failure exits before anything opens (no browser, no server)."""
    def no(why):
        raise SystemExit(f'LIVE refused, nothing was opened: {why}')
    if not cfg['live_env']:
        no('.env does not say RATBRAIN_LIVE=1')
    if not cfg['_key']:
        no('.env has no RATBRAIN_RH_KEY')
    if a.confirm != cfg['symbol']:
        no(f"--confirm must equal the symbol {cfg['symbol']!r}")
    if os.path.exists(launcher.JOURNAL):
        no(f'{launcher.JOURNAL} exists: a launch was already attempted (python launcher.py --resolve)')
    meta, _, _ = big_stack(rollout.run, str(policy), a.seed, preroll_s=a.preroll)
    if not meta.get('press'):
        no(f'the rehearsal with seed {a.seed} does not press')
    proof = meta['press']['proof']
    try:
        launcher.check_proof(proof)
        pre = launcher.preflight(cfg, proof)
    except launcher.LaunchRefused as e:
        no(f'preflight: {e}')
    say('LIVE gates passed:', json.dumps({k: v for k, v in pre.items() if k != 'acct'}, default=str))
    return {'pre': pre, 'proof': proof}


def main():
    # UTF-8 whatever the launcher gave us: a pipe defaults to the ANSI code page (cp1252), so '·' and '°'
    # went out as single cp1252 bytes and showed as '�' in UTF-8 readers (the preview log, Git Bash).
    # A real console is already UTF-8 in Python, so this changes nothing there.
    for stream, kw in ((sys.stdout, {'line_buffering': True}), (sys.stderr, {})):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace', **kw)
        except Exception:
            pass
    ap = argparse.ArgumentParser(description='RATBRAIN live rig (DRY by default)')
    ap.add_argument('--port', type=int, default=4661)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--seed', type=int, default=2026)
    ap.add_argument('--preroll', type=float, default=3.0)
    ap.add_argument('--policy', default='runs/final/policy.pt')
    ap.add_argument('--headful', action='store_true', help='show the Chromium window')
    ap.add_argument('--pons-size', default=f'{ponsbot.PAGE_SIZE[0]}x{ponsbot.PAGE_SIZE[1]}')
    ap.add_argument('--pons-theme', choices=('dark', 'light'), default='dark')
    ap.add_argument('--page-cursor', action='store_true', help='also draw the cursor into the pons page')
    ap.add_argument('--dev-shots', action='store_true',
                    help='save PNGs of the pons page at key stages to live/dev_shots/ (also draws the page cursor)')
    ap.add_argument('--live', action='store_true')
    ap.add_argument('--confirm', help='type the coin symbol to confirm LIVE')
    a = ap.parse_args()

    policy = Path(a.policy)
    policy = policy if policy.is_absolute() else (ROOT / policy)
    policy = policy.resolve()
    if not policy.exists():
        raise SystemExit(f'policy not found: {policy}')
    try:
        policy.relative_to(ROOT)
    except ValueError:
        raise SystemExit('the policy must live under the ratbrain folder (replay.py resolves it from there)')
    cfg = launcher.config()
    live = None
    if a.live:
        live = live_gate(a, cfg, policy)
    elif a.confirm:
        say('--confirm without --live is ignored: DRY')
    w, h = (int(x) for x in a.pons_size.lower().split('x'))
    pinned = pin_config(cfg)
    if live and pinned['key_address'] != live['pre']['creator']:
        raise SystemExit('LIVE refused, nothing was opened: the preflight wallet is not the .env key')
    token = secrets.token_urlsafe(24) if live else None
    CONFIG.update(
        mode='LIVE' if live else 'DRY', live=live, policy=policy, seed=a.seed, preroll=a.preroll,
        headful=a.headful, size=(w, h), theme=a.pons_theme, dev_shots=a.dev_shots,
        page_cursor=a.page_cursor or a.dev_shots, name=cfg['name'], symbol=cfg['symbol'],
        policy_sha256=rollout.sha_bytes(policy.read_bytes()), port=a.port, token=token, pinned=pinned)
    cfg.clear()                       # the key is re-read from .env per run (launcher.config); drop this copy
    url = f'http://localhost:{a.port}/' + (f'?token={token}' if token else '')
    say(f"\n  RATBRAIN rig · {'LIVE' if live else 'DRY RUN'} · {CONFIG['name']} (${CONFIG['symbol']}) · "
        f"seed {a.seed} · pre-roll {a.preroll:g} s\n  policy {rel(policy)} sha256 {CONFIG['policy_sha256']}\n"
        f"  open {url}\n" + (f"  LIVE token (per process; the page needs it): {token}\n" if token else ''))
    uvicorn.run(app, host=a.host, port=a.port, log_level='warning', ws_max_size=16 * 1024 * 1024)


if __name__ == '__main__':
    main()
