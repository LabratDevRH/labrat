"""RATBRAIN brain rig (live/SPEC_BRAIN.md): the rat's trained brain does everything on the REAL ponsfamily.com
create page. Its head direction steers a cursor, its lever press clicks, and the rig lights up one target at a
time (a cue, like a cue light in an operant chamber). There is no 3D rat on the page: the body is simulated
underneath (DeepMind rodent in MuJoCo, session.Session), because its head moves the cursor and its lever press
is the click. DRY (the default): pons builds the launch transaction, the injected wallet decodes it, checks it and
refuses to sign (EIP-1193 4001); nothing is signed or broadcast. LIVE (--live --confirm SYMBOL, every gate below):
the same checks, then the transaction is signed ONCE at nonce 0 with the funded .env wallet and broadcast.

The brain is TWO trained networks (not a biological brain; artificial network units, not anatomy):
  * the STEERING network (--policy, default runs/final/steer.pt): 21 inputs (the target cue + its own head/neck
    state) -> 256 -> 256 -> 256 -> 5 outputs (the 4 neck actuators, which turn the head and so move the cursor, and
    a PRESS signal). 794 units, 137,728 connections.
  * the lever-PRESS network (--press-policy, default runs/final/policy.pt): 200 body inputs -> 512 -> 512 -> 256 ->
    38 actuators. When the steering network signals PRESS it takes the whole body, presses the lever (the click)
    and recovers. 1,518 units, 505,344 connections.
  Together: 2,312 units, 643,072 connections. The brain commit (session.brain_commit) covers both networks.

    python live/brainrig.py [--port 4665] [--seed 2026] [--policy runs/final/steer.pt]
                            [--press-policy runs/final/policy.pt] [--dev-oracle] [--dev-shots] [--headful]
    python live/brainrig.py --live --confirm <SYMBOL>      (LIVE: see "LIVE" below)

    GET  /                live/web/brain.html (the viewer)
    GET  /status          {mode: DRY|LIVE, dev_oracle, symbol, brain_commit, steer_sha256, press_sha256, units,
                           connections, networks, frame layout, busy, last_run, env_read, ...; LIVE adds address,
                           balance_eth_at_startup, launch_fee_eth, explorer, live_used, token_required}
    GET  /weights_summary for BOTH networks, each layer pair's top ~400 |w| links [src, dst, w], plus unit labels
    WS   /run             client -> {"type":"start","seed":2026} | {"type":"stop"}
                          (LIVE: {"type":"start","seed":..,"token":<token>,"confirm":<SYMBOL>,"code":<code>})
                          server -> JSON: log, commit, stage, shot, click, tx, done (LIVE also: refused, started
                          {token}, and tx phases checked / signed / sent / rejected /
                          mined / reverted / unconfirmed); binary brain frames at 25 Hz.
                          The handshake is refused unless its Origin is http://localhost:<port> or
                          http://127.0.0.1:<port> (cross-site WebSocket hijacking).

LIVE (the owner's test launch; the rig never starts one by itself)
  Startup needs ALL of: .env (the FILE, via launcher.config()) RATBRAIN_LIVE=1 and RATBRAIN_RH_KEY; --live;
  --confirm equal to .env's RATBRAIN_SYMBOL; no launch_journal.json (else it names the recovery:
  `python launcher.py --resolve` for a signed launch, `--clear-unsigned` for an unsigned reservation); the coin
  rules (whole-percent tax, no X / website links: the rat fills neither); launcher.preflight() with the brain
  commit (fresh wallet nonce 0, balance >= 1.5x gas + fee, the launch simulates); the preflight wallet = the .env
  key. Any failure exits before anything opens. It then prints, once, http://localhost:<port>/?token=<token>.
  A LIVE session starts ONLY from that page: BEGIN SESSION opens a confirm dialog and only its Start sends
  {"type":"start", token, confirm}. What gates a start is the token (in the owner's URL) plus that click; the
  Origin and User-Agent checks (a browser, not headless) only stop accidents such as record.py's headless browser
  or a script: they are client-supplied headers, not a security boundary. (A process running as the same user can
  read .env too; no gate in the rig can stop that.) A start is also refused after this process used its one launch;
  ?autostart=1 is ignored in LIVE and live/record.py refuses to start a LIVE session. The page's only third-party
  script, three.js 0.160.0 from jsDelivr (the rotating display rat), is pinned by sha384 in the import map's
  "integrity": a modified file is refused by the browser, so no CDN code can drive the page.
  Per run: launcher.config() is re-read and must equal startup (name, symbol, tax, links, live flag, key address),
  the brain commit must equal the preflighted one; once pons's review is open and before the rat's Confirm target is
  lit, the rig checks the tax and the pinned image, runs launcher.preflight() again and reserves launch_journal.json
  (exclusive create). The rat then has LIVE_CONFIRM_MAX_S to hit Confirm, else the run ends unsigned.
  ABORT (nothing is signed): until the launch tx is handed to the signer, a LIVE run stops when the owner presses
  STOP in the page, when the page's websocket goes away (tab closed / reloaded / crashed, record.py's window closed),
  or on the first Ctrl+C in the rig's terminal. An unarmed run just ends (start again from the page); an armed one
  closes launch_journal.json as aborted_before_sign (`python launcher.py --clear-unsigned`, then restart the rig).
  Once pons's eth_sendTransaction has passed every check and gone to the signer, nothing stops it: the run goes on
  to the receipt and is recorded.
  pons's eth_sendTransaction is decoded and checked exactly as in DRY (to = factory, selector, value = the
  0.0005 ETH fee, creator = our address, name / symbol / description with the brain commit, image = pons's pin,
  tax, pair ETH, no developer buy, empty links, canonical ABI); any failure -> 4001, nothing signed. Otherwise
  ponsbot.LiveLaunch (the code rig.py uses) re-simulates, re-checks the gas price and balance against the armed fee
  cap, signs exactly those calldata bytes pinned to nonce 0, journals the raw signed tx BEFORE broadcasting, and the
  rig returns the real hash to pons, waits for the receipt, reads the token from TokenLaunched, and shows the coin's
  pons page. If every RPC rejects the signed tx, pons gets an error (not a hash), the page says REJECTED and
  `python launcher.py --resolve` is the only way on (it re-broadcasts the same signed bytes). The key never enters
  the page: the page's wallet holds only the address (and reads the real balance; no DRY override). Errors are
  reported by type and message with the key redacted, never as repr() (a decode error's repr holds raw file bytes).

Who does what
  * The rat (Session, the two trained networks): every cursor movement and every click. A click inside the lit
    target (hit=True) is forwarded to pons with page.mouse.click at the rat's cursor pixel. A click anywhere
    else is a MISS: never forwarded to the page (a masked touchscreen), shown as a grey ring and logged.
  * The rig: opens the page (setup: dark theme, pons's status strip dismissed), scrolls the next target into
    view while the rat is on hold, measures its real clickable box, lights it (session.target), and after the
    rat's hit runs the consequence: types the field's text at 90 ms/char (keyboard.type), picks coin.png in
    the file chooser the rat's click opened, waits for pons. It re-measures the lit target and re-lights it
    if pons moves it; a target is never padded beyond its real clickable area (a terms checkbox's target is its
    <label>: the row's padding does not tick the box and the Terms of Use / Privacy Policy links open a document;
    rounded corners, which take no clicks, are kept out of every target).
  * --dev-oracle (testing only): a SCRIPTED cursor drives the targets instead of the brain. /status says
    dev_oracle: true, the in-page cursor is labelled "DEV script", record.py must refuse it, and nothing it
    does is a brain run (no session.json is written).

Binary brain frame (25 Hz, little-endian; 2,384 bytes): Float32 x18 header
    [3.0, sim_t, brain_on, cursor_x, cursor_y, tgt_cx, tgt_cy, tgt_hw, tgt_hh, holding, locked, lever_angle,
     head_yaw_defl, head_pitch_defl, hits, misses, pressing, press_step]
  cursor and target in normalized page-viewport coordinates ((0,0) top-left, (1,1) bottom-right of the
  1280x900 pons viewport; the target is -1 while holding); deflections in rad (env._deflection: head left /
  up -> cursor left / up); pressing = 1 while the lever-press network drives the body (this control step),
  press_step = the press program's step (0 when idle; the cursor is frozen while > 0).
  Then Uint8 x2312 activations:
    steering network  input 21, h1 256, h2 256, h3 256, output 5    (794)
    press network     input 200, h1 512, h2 512, h3 256, motor 38   (1518; ALL ZERO while it is idle)
  Fixed per-layer mapping to 0..255 (STEER_SCALE / PRESS_SCALE): input |z| / 3 (z = the normalized input the
  network sees), hidden max(a, 0) / scale (ELU outputs >= -1: negative = dark), neck / motor outputs |a| / 1 (the
  env clips at 1), the steering PRESS output max(a, 0) / 0.5 (a > 0 starts a press).
  In --dev-oracle mode and during the brain-off pre-roll the activations are all zero (brain_on = 0).

Recording: runs/brainrig_<UTC>_seed<seed>/ gets steer.pt + press.pt (the two frozen networks, read-only),
session.json + qpos.npy + actions.npy (session.save), captured_tx.json, events.jsonl, pons_final.jpg,
targets.json. `python replay_session.py <run dir>` must print MATCH. --dev-oracle runs go to
runs/brainrig_<UTC>_seed<seed>_DEVORACLE/ with oracle.json instead of the session files. LIVE runs also get
live_receipt.json (hash, block, token, curve, gas used, fee, explorer links, the raw receipt) and a copy of
launch_journal.json (which stays in place: it is what makes a second launch impossible).

DRY never reads or creates .env and never calls launcher.config(): the coin's public fields come from the process
environment (RATBRAIN_NAME, RATBRAIN_SYMBOL, RATBRAIN_TAX_BPS, RATBRAIN_X, RATBRAIN_WEBSITE) or launcher's defaults,
never a key. Only --live reads .env (launcher.config(), through live_config(); /status env_read says if it did).
"""
import faulthandler
import sys
import threading

# Thread stacks, set before any thread exists (SPEC_BRAIN: 32 MB). The preview launcher's python gives threads a
# 1 MiB stack and MuJoCo compiling the rat's scene peaks at ~1 MiB, so every thread this process creates gets
# THREAD_STACK. On Windows that size is COMMITTED per thread, so the thread count is bounded: the asyncio default
# executor is capped at EXECUTOR_THREADS (startup hook), the session runs on one thread, and the HTTP handlers
# never touch anyio's thread pool (no FileResponse, no sync endpoints).
THREAD_STACK = 32 * 1024 * 1024
threading.stack_size(THREAD_STACK)
EXECUTOR_THREADS = 6
try:
    faulthandler.enable()
except (RuntimeError, ValueError, AttributeError):
    pass

import argparse  # noqa: E402
import asyncio  # noqa: E402
import base64  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import queue  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import shutil  # noqa: E402
import stat  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402

LIVE_DIR = Path(__file__).resolve().parent
ROOT = LIVE_DIR.parent
for _p in (str(ROOT), str(LIVE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import launcher  # noqa: E402  (DRY: constants + rpc only; launcher.config() reads .env and is called in LIVE only)
import ponsbot  # noqa: E402
from eth_account import Account  # noqa: E402
import session as brain_session  # noqa: E402
from env import SCENE, CTRL_DT  # noqa: E402
from ptload import load as pt_load, NumpyPolicy  # noqa: E402

import uvicorn  # noqa: E402
from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response  # noqa: E402

VIEW_W, VIEW_H = ponsbot.PAGE_SIZE           # 1280 x 900 CSS px, device scale 1
FRAME_MAGIC = 3.0                            # 3.0 = the two-network frame (2.0 was the old single network)
HEADER_FIELDS = ('magic', 'sim_t', 'brain_on', 'cursor_x', 'cursor_y', 'tgt_cx', 'tgt_cy', 'tgt_hw', 'tgt_hh',
                 'holding', 'locked', 'lever_angle', 'head_yaw_defl', 'head_pitch_defl', 'hits', 'misses',
                 'pressing', 'press_step')
HEADER_N = len(HEADER_FIELDS)                # 18
STEER_SIZES = (21, 256, 256, 256, 5)
PRESS_SIZES = (200, 512, 512, 256, 38)
STEER_LAYERS = ('input', 'h1', 'h2', 'h3', 'output')
PRESS_LAYERS = ('input', 'h1', 'h2', 'h3', 'motor')
# display scales, from the activation ranges on the pons target sequence offline (~p99 of each layer)
STEER_SCALE = {'input': 3.0, 'h1': 1.5, 'h2': 1.5, 'h3': 2.0, 'output': 1.0, 'press_output': 0.5}
PRESS_SCALE = {'input': 3.0, 'h1': 3.0, 'h2': 8.0, 'h3': 4.0, 'motor': 1.0}


def n_conn(sizes):
    return sum(a * b for a, b in zip(sizes[:-1], sizes[1:]))


STEER_UNITS, PRESS_UNITS = sum(STEER_SIZES), sum(PRESS_SIZES)     # 794, 1,518
UNITS = STEER_UNITS + PRESS_UNITS                                 # 2,312
CONNECTIONS = n_conn(STEER_SIZES) + n_conn(PRESS_SIZES)           # 137,728 + 505,344 = 643,072
assert (UNITS, CONNECTIONS) == (2312, 643072)
FRAME_BYTES = HEADER_N * 4 + UNITS                                # 2,384
TOP_LINKS = 400
SHOT_QUALITY = 70
SHOT_MAX_FPS = 20.0          # SPEC_BRAIN: ~15-20 fps, at most one frame in flight
MOUSE_HZ = 25.0
TYPE_DELAY_MS = 90           # flybrain's live typing
PREROLL_S = 2.0
NO_HIT_NOTE_S = 20.0         # a target not hit in 20 s stays lit (the rat keeps trying); the log says so
REMEASURE_S = 0.4
MOVE_TOL_PX = 2.0
MAX_RELIGHTS = 8
ACQUIRE_S = 60.0
TERMS_WAIT_S = 15.0
SEND_WAIT_S = 30
DRY_HANDLE_WAIT_S = 60
STAGE_DETAIL_MAX = 60
COIN_PNG = LIVE_DIR / 'assets' / 'coin.png'
COIN_MAX_BYTES = 2_000_000
# the coin description (SPEC_BRAIN): the brain commit covers BOTH networks (steering + lever-press), the code and
# the scene
DESCRIPTION = ('launched by a virtual rat: its trained brain steered the cursor and clicked every button. '
               'brain sha256 {commit}')
NECK = ('cervical_extend', 'cervical_bend', 'cervical_twist', 'atlas')
FORELIMB_RE = re.compile(r'^(scapula|shoulder|elbow|wrist|finger)')
CUE_FEATURES = ('target dx', 'target dy', 'target half-width', 'target half-height', 'cursor vx', 'cursor vy',
                'cursor locked', 'lever armed', 'cursor on target', 'time on target', 'holding')
HONESTY = ("The rat's brain is two trained neural networks driving a simulated rat body (DeepMind rodent model in "
           "MuJoCo): a steering network turns its head, which moves the cursor, and decides when to press; a "
           "lever-press network then performs each press with the whole body, and the press clicks. They are "
           "artificial networks, not a biological brain. The rig lights up the next target (a cue, like a cue "
           "light in an operant chamber), scrolls the page, and types the text of a field after the rat clicks "
           "into it. Clicks outside the lit target are ignored. DRY RUN: pons builds the launch transaction, the "
           "rig refuses to sign.")
HONESTY_LIVE = HONESTY.replace(
    'DRY RUN: pons builds the launch transaction, the rig refuses to sign.',
    'LIVE: pons builds the launch transaction; the rig decodes it, checks every field against the configured coin '
    'and the brain commit, and signs it ONCE (nonce 0) with the funded wallet. The key never enters the page.')
assert HONESTY_LIVE != HONESTY

# ---- LIVE
LIVE_VIA = 'live/brainrig.py'
LAUNCH_FEE_ETH = launcher.LAUNCH_FEE_WEI / 1e18                      # 0.0005
PIN_KEYS = ('name', 'symbol', 'tax_bps', 'image', 'x', 'website', 'live_env')   # rig.py's: must not change after startup
# launcher.preflight() simulates launcher.calldata(cfg), which carries cfg['image']. In the brain rig pons pins coin.png
# itself and the calldata's image is checked against pons's pin, so .env's RATBRAIN_IMAGE only feeds that simulation.
# When it is empty the simulation uses coin.png as pons pinned it: the CID pons returned for live/assets/coin.png in
# every DRY brain run (2026-09-24). Content-addressed, so the same file pins to the same URI.
SIM_IMAGE = 'ipfs://bafybeiedfhkewsq4ljrjenvjpy4qn3jurctmlfzoaslhvco5i7jzqid46a'
PONS_COIN_URL = 'https://www.ponsfamily.com/launchpad/{}'   # flybrain's rhlive.py ends on the coin's pons page too
SUCCESS_RE = (r'(token launched|launched successfully|launch successful|successfully launched|launch complete|'
              r'view (your )?(token|coin)|transaction (submitted|sent|confirmed))')
SUCCESS_WAIT_S = 30             # at most this long after pons got the hash, for pons's own success text
SUCCESS_AFTER_RECEIPT_S = 8     # ... and at most this long after the receipt (flybrain waited <= 9 s, then opened the coin)
REJECTED_POLL_S = 15            # every RPC rejected the signed tx: a short look for it on chain, not the full 120 s
COIN_PAGE_S = 12
LIVE_CONFIRM_MAX_S = 120        # after arming (journal reserved), the rat must hit pons's Confirm within this (it takes ~1 s)
# accident guards only (client-supplied headers): see the LIVE section above
BROWSER_UA_RE = re.compile(r'^Mozilla/5\.0 .*(Chrome|Firefox|Safari|Edg)/', re.I)

# (stage key, label, kind, arg)
TARGETS = (
    ('t01_terms_tou', 'terms checkbox: Terms of Use', 'terms', 0),
    ('t02_terms_privacy', 'terms checkbox: Privacy Policy', 'terms', 1),
    ('t03_accept', 'Accept and continue', 'accept', None),
    ('t04_image', 'Choose image', 'image', None),
    ('t05_name', 'Name field', 'name', None),
    ('t06_ticker', 'Ticker field', 'ticker', None),
    ('t07_description', 'Description field', 'description', None),
    ('t08_advanced', 'Advanced', 'advanced', None),
    ('t09_tax', 'Creator tax field', 'tax', None),
    ('t10_launch', 'Launch token', 'launch', None),
    ('t11_confirm', 'Confirm', 'confirm', None),
)

CONFIG = {}
STATE = {'busy': False, 'last_run': None, 'runs': 0, 'live_used': False, 'env_read': False}
ACTIVE = {'run': None}      # the Run in flight (for LIVE abort on viewer loss / shutdown); not in /status
BRAIN = {}
ACTIVE = {'run': None}          # the Run in progress (LIVE: Ctrl+C stops it before it signs)


def say(*parts):
    try:
        print(' '.join(str(x) for x in parts), flush=True)
    except Exception:
        pass


def rel(p):
    return os.path.relpath(str(p), str(ROOT)).replace('\\', '/')


def utc(ts=None):
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def sha(b):
    return hashlib.sha256(b).hexdigest()


def big_stack(fn, *args, **kwargs):
    """fn on a fresh THREAD_STACK thread (the main thread's stack may be 1 MiB: too small for MuJoCo)."""
    with ThreadPoolExecutor(1, thread_name_prefix='brainrig-boot') as ex:
        return ex.submit(fn, *args, **kwargs).result()


# ------------------------------------------------------------------------------------------------------------
# the coin (public fields only) and the brain
# ------------------------------------------------------------------------------------------------------------
def coin_config():
    """The coin's public fields WITHOUT reading .env (launcher.config() reads .env; the DRY brain rig never
    does). Same defaults and the same form rules as launcher.config()."""
    g = os.environ.get
    c = {'name': g('RATBRAIN_NAME', 'ratbrain'), 'symbol': g('RATBRAIN_SYMBOL', 'RATBRAIN'),
         'x': g('RATBRAIN_X', ''), 'website': g('RATBRAIN_WEBSITE', ''),
         'tax_bps': int(g('RATBRAIN_TAX_BPS', '100'))}
    if not (0 < len(c['name']) <= 32 and all(ch.isalnum() or ch == ' ' for ch in c['name'])):
        raise SystemExit('bad RATBRAIN_NAME (letters, digits, spaces, <= 32)')
    if not (0 < len(c['symbol']) <= 10 and c['symbol'].isalnum() and c['symbol'].upper() == c['symbol']):
        raise SystemExit('bad RATBRAIN_SYMBOL (A-Z 0-9, <= 10)')
    if not (0 <= c['tax_bps'] <= 1000 and c['tax_bps'] % 100 == 0):
        raise SystemExit('RATBRAIN_TAX_BPS must be a whole percent between 0 and 10 (0..1000, step 100)')
    return c


# ------------------------------------------------------------------------------------------------------------
# LIVE: config, gates (never used by the DRY path)
# ------------------------------------------------------------------------------------------------------------
def live_config():
    """launcher.config(): reads the .env FILE (RATBRAIN_LIVE, RATBRAIN_RH_KEY, the coin). LIVE only; every call in this
    module goes through here, so /status env_read says whether this process ever read .env (DRY never does)."""
    STATE['env_read'] = True
    try:
        return launcher.config()
    except launcher.LaunchRefused:
        raise
    except Exception as e:
        # never the exception's text or repr: a UnicodeDecodeError's repr holds the raw .env bytes, key line included
        raise launcher.LaunchRefused(f'.env could not be read or parsed ({type(e).__name__})') from None


def err_text(e, limit=300):
    """An exception for the LIVE page / log / events / run dir: its type and str() (never repr(): a decode error's
    repr holds the raw bytes it was decoding), bytes literals dropped, and the funded key, if it appears, redacted."""
    try:
        s = f'{type(e).__name__}: {e}' if not isinstance(e, UnicodeError) else \
            f"{type(e).__name__}: {getattr(e, 'reason', '')}"
    except Exception:
        s = type(e).__name__
    s = re.sub(r"b'(?:[^'\\]|\\.)*'|b\"(?:[^\"\\]|\\.)*\"", "b'<bytes>'", s)
    key = None
    try:
        acct = ((CONFIG.get('live') or {}).get('pre') or {}).get('acct')
        key = bytes(acct.key).hex() if acct is not None else None
    except Exception:
        key = None
    if key:
        s = re.sub(re.escape(key), '<redacted>', s, flags=re.I)
    return s[:limit]


def err_repr(e, limit=300):
    """DRY: repr(e) exactly as before (the DRY path is unchanged). LIVE: err_text(e)."""
    return err_text(e, limit) if CONFIG.get('mode') == 'LIVE' else repr(e)[:limit]


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
    """What must not change between startup and START (a LIVE run is refused if it does). As rig.py."""
    pub = launcher.public(cfg)
    return {**{k: pub.get(k) for k in PIN_KEYS}, 'key_address': key_address(cfg)}


def config_drift(pinned, cfg):
    now = pin_config(cfg)
    return [k for k in pinned if pinned[k] != now.get(k)]


def live_coin(cfg):
    """The coin's public fields from launcher.config() (the .env file) under the brain rig's own rules: the tax is a
    whole percent (pons's field), and there are no X / website links (the rat fills neither field, so a calldata with
    links could never pass the 'socials' check). Raises launcher.LaunchRefused."""
    c = {'name': cfg['name'], 'symbol': cfg['symbol'], 'x': cfg.get('x') or '', 'website': cfg.get('website') or '',
         'tax_bps': int(cfg['tax_bps'])}
    if not (0 <= c['tax_bps'] <= 1000 and c['tax_bps'] % 100 == 0):
        raise launcher.LaunchRefused('RATBRAIN_TAX_BPS must be a whole percent between 0 and 10 (0..1000, step 100)')
    if c['x'] or c['website']:
        raise launcher.LaunchRefused("the brain rig does not fill pons's link fields: leave RATBRAIN_X and "
                                     'RATBRAIN_WEBSITE empty in .env')
    return c


def preflight_cfg(cfg):
    """cfg for launcher.preflight(): its simulation needs a valid image (see SIM_IMAGE)."""
    return cfg if launcher.image_ok(cfg.get('image') or '') else {**cfg, 'image': SIM_IMAGE}


def journal_refusal():
    """None when launch_journal.json does not exist, else why a new launch is impossible and the recovery path.
    The journal is never deleted or rewritten here."""
    if not os.path.exists(launcher.JOURNAL):
        return None
    try:
        with open(launcher.JOURNAL, encoding='utf-8') as fh:
            j = json.load(fh)
    except Exception as e:
        return (f'{launcher.JOURNAL} exists but cannot be read ({type(e).__name__}); a launch may have been signed: '
                'inspect it by hand, never launch again from this wallet until you know')
    if 'raw' in j:
        return (f"{launcher.JOURNAL} holds a SIGNED launch (state {j.get('state')}, tx {j.get('tx')}): this wallet "
                'can never launch again. Finish it with `python launcher.py --resolve`')
    return (f"{launcher.JOURNAL} exists (state {j.get('state')}): an earlier run reserved a launch and signed nothing. "
            'Clear it with `python launcher.py --clear-unsigned`, then start the rig again')


def live_gate_static(a, cfg):
    """LIVE startup gates that need no chain and no brain: .env LIVE + key, --confirm, no --dev-oracle, the journal,
    the coin rules. -> the coin. SystemExit on any failure (before anything opens)."""
    def no(why):
        raise SystemExit(f'LIVE refused, nothing was opened: {why}')
    if not cfg.get('live_env'):
        no('.env does not say RATBRAIN_LIVE=1')
    if not cfg.get('_key'):
        no('.env has no RATBRAIN_RH_KEY')
    if a.confirm != cfg['symbol']:
        no(f"--confirm must equal the symbol in .env ({cfg['symbol']!r}), got {a.confirm!r}")
    if a.dev_oracle:
        no('--dev-oracle is a scripted cursor, never the rat: it can never run LIVE')
    why = journal_refusal()
    if why:
        no(why)
    try:
        return live_coin(cfg)
    except launcher.LaunchRefused as e:
        no(str(e))


def live_gate_chain(cfg, commit):
    """LIVE startup gates on the chain: launcher.preflight() with the brain commit (fresh wallet nonce 0, balance >=
    1.5x gas + fee, the launch simulates), and the preflight wallet must be the .env key. -> CONFIG['live']."""
    def no(why):
        raise SystemExit(f'LIVE refused, nothing was opened: {why}')
    try:
        launcher.check_proof(commit)
        pre = launcher.preflight(preflight_cfg(cfg), commit)
    except launcher.LaunchRefused as e:
        no(f'preflight: {e}')
    if key_address(cfg) != pre['creator']:
        no('the preflight wallet is not the .env key')
    say('LIVE gates passed:', json.dumps({k: v for k, v in pre.items() if k != 'acct'}, default=str))
    return {'pre': pre, 'commit': commit, 'address': pre['creator'], 'balance': pre['balance'], 'need': pre['need'],
            'gas': pre['gas'], 'max_fee': pre['max_fee'], 'predicted_token': pre.get('predicted_token'),
            'sim_image': preflight_cfg(cfg)['image']}




def show_code(c):
    return f'{c[:3]}-{c[3:]}' if c else ''



def live_start_refusal(ws, msg):
    """Why a LIVE {"type":"start"} must be refused (None = allowed). Only the owner's click in the page (BEGIN SESSION,
    then Start in its confirm dialog) may start a LIVE session. The token (in the owner's URL) and that click are
    the gate; the User-Agent check below only stops accidents (a headless recorder, a script): it is a client-supplied
    header, not enforcement."""
    if not CONFIG.get('token') or not hmac.compare_digest(str(msg.get('token') or ''), CONFIG['token']):
        return 'the start message lacks the rig\'s token (open the exact URL the rig printed: .../?token=...)'
    if msg.get('confirm') != CONFIG['coin']['symbol']:
        return "the start was not confirmed in the page's dialog"
    ua = ws.headers.get('user-agent') or ''
    if not BROWSER_UA_RE.match(ua) or 'headless' in ua.lower():
        return f"a LIVE session starts only from the owner's own browser (this client says {ua[:70]!r})"
    if STATE['live_used']:
        return 'this rig process already used its one launch'
    why = journal_refusal()
    if why:
        return why
    return None


def resolve_policy(arg):
    """-> (path, note). A missing file falls back to the newest policy_*.pt snapshot in the same folder, and the
    note says so."""
    p = Path(arg)
    p = p if p.is_absolute() else ROOT / p
    if p.exists():
        return p.resolve(), None
    cands = sorted(p.parent.glob('policy_*.pt'), key=lambda q: q.stat().st_mtime) if p.parent.is_dir() else []
    if not cands:
        raise SystemExit(f'policy not found: {p} (and no policy_*.pt snapshot next to it)')
    return cands[-1].resolve(), f'{rel(p)} does not exist yet: using the newest snapshot {rel(cands[-1])}'


def snapshot_policy(path, sizes, what, tries=20):
    """The network's bytes, read ONCE into memory and validated against its layer sizes. Training replaces its
    checkpoints atomically (os.replace), so one read gets a whole old or a whole new file; every run then freezes
    exactly these bytes into its run dir (steer.pt / press.pt, read-only) and the Session reads those copies."""
    last = None
    for _ in range(tries):
        try:
            b = Path(path).read_bytes()
            ck = pt_load(io.BytesIO(b))
            got = [ck['net'][f'pi.{i}.weight'].shape for i in (0, 2, 4, 6)]
            want = [(o, i) for i, o in zip(sizes[:-1], sizes[1:])]
            # an input narrower than the network's (new features appended later) is widened by ptload.widen
            ok = (len(ck['mean']) <= sizes[0] and got[0][0] == want[0][0] and got[0][1] <= sizes[0]
                  and got[1:] == want[1:])
            if not ok:
                raise ValueError(f"{what}: unexpected shapes {got} (obs {len(ck['mean'])}), want {want}")
            return b
        except Exception as e:           # mid-replace (Windows sharing violation) or not a checkpoint
            last = e
            time.sleep(0.5)
    raise SystemExit(f'could not read a whole {what} from {path}: {last!r}')


def brain_commit_for(steer_bytes, press_bytes):
    """session.brain_commit: sha256(scene + steering net + press net + the code files), both networks."""
    code = {f: brain_session.text_bytes(os.path.join(str(ROOT), f)) for f in brain_session.CODE_FILES}
    return brain_session.brain_commit(steer_bytes, brain_session.text_bytes(SCENE), code, press_bytes)


def body_layout():
    """Unit labels from the MuJoCo model (compiled on a big-stack thread): the steering network's 21 inputs and 5
    outputs, the press network's 200 body inputs and 38 actuators."""
    import mujoco
    m = mujoco.MjModel.from_xml_path(SCENE)
    names = [mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_ACTUATOR, i) for i in range(m.nu)]
    neck = [n for n in names if n in NECK]                      # steer_env.SteerEnv.neck order (actuator index)
    lever_q = int(m.jnt_qposadr[mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, 'lever_hinge')])
    # the press network: the lever task's own observation (steer_env.SteerEnv._lever_obs)
    parts = [('body height', 1), ('body orientation', 4), ('joint angles', lever_q - 7),
             ('joint angles (after the lever)', m.nq - lever_q - 1), ('joint velocities (incl. the lever)', m.nv),
             ('muscle activations', m.na), ('lever tip (body frame)', 3), ('paws (body frame)', 6),
             ('up vector', 3), ('lever angle, press clock', 4)]
    pgroups, i = [], 0
    for name, n in parts:
        if n:
            pgroups.append({'name': name, 'from': i, 'to': i + n})
            i += n
    if i != PRESS_SIZES[0] or len(names) != PRESS_SIZES[-1] or len(neck) != 4:
        raise SystemExit(f'the model does not match the networks: {i} press inputs, {len(names)} actuators, '
                         f'{len(neck)} neck actuators')
    # the steering network: steer_env.SteerEnv._obs
    sparts = [('target cue', list(CUE_FEATURES), True),
              ('head deflection', ['head yaw deflection', 'head pitch deflection'], False),
              ('its own neck commands', [f'neck: {n}' for n in neck], False),
              ('press state', ['pressing', 'recovering after a press'], False),
              ('posture', ['body upright', 'body height'], False)]
    sgroups, s_labels, i = [], [], 0
    for name, labs, cue in sparts:
        sgroups.append({'name': name, 'from': i, 'to': i + len(labs), 'cue': cue})
        s_labels += labs
        i += len(labs)
    if i != STEER_SIZES[0]:
        raise SystemExit(f'the steering network expects {STEER_SIZES[0]} inputs, the layout has {i}')
    s_out = [{'i': k, 'name': n, 'group': 'steers the cursor'} for k, n in enumerate(neck)]
    s_out.append({'i': 4, 'name': 'PRESS', 'group': 'starts a lever press (the click)'})
    motor = [{'i': k, 'name': n, 'group': 'neck' if n in NECK else
              ('presses the lever' if FORELIMB_RE.match(n or '') else 'body')} for k, n in enumerate(names)]
    return {'steer': {'input_groups': sgroups, 'input_labels': s_labels, 'cue_features': list(CUE_FEATURES),
                      'cue_from': 0, 'outputs': s_out},
            'press': {'input_groups': pgroups, 'motor': motor}}


def _pairs(pol, layers):
    pairs = []
    for k, (w, _b) in enumerate(pol.layers):
        n_out, n_in = w.shape
        flat = np.abs(w).ravel()
        n = min(TOP_LINKS, flat.size)
        idx = np.argpartition(flat, -n)[-n:]
        idx = idx[np.argsort(-flat[idx])]
        links = [[int(j % n_in), int(j // n_in), round(float(w.ravel()[j]), 5)] for j in idx]
        pairs.append({'from': layers[k], 'to': layers[k + 1], 'n_from': int(n_in), 'n_to': int(n_out),
                      'max_abs_w': round(float(flat.max()), 5), 'links': links})
    return pairs


def weights_summary(steer_bytes, press_bytes, layout, sources):
    steer = NumpyPolicy(pt_load(io.BytesIO(steer_bytes)), STEER_SIZES[0])
    press = NumpyPolicy(pt_load(io.BytesIO(press_bytes)), PRESS_SIZES[0])
    offset = {'steer': 0, 'press': STEER_UNITS}
    nets = [
        {'key': 'steer', 'name': 'steering network',
         'role': 'turns the head (the head moves the cursor) and decides when to press',
         'sizes': list(STEER_SIZES), 'layers': list(STEER_LAYERS), 'units': STEER_UNITS,
         'connections': n_conn(STEER_SIZES), 'policy': sources['steer'], 'sha256': sha(steer_bytes),
         'frame_offset': offset['steer'], 'pairs': _pairs(steer, STEER_LAYERS), 'activation_scale': STEER_SCALE,
         **layout['steer']},
        {'key': 'press', 'name': 'lever-press network',
         'role': 'performs each press with the whole body (the click), then recovers; idle (all zero) otherwise',
         'sizes': list(PRESS_SIZES), 'layers': list(PRESS_LAYERS), 'units': PRESS_UNITS,
         'connections': n_conn(PRESS_SIZES), 'policy': sources['press'], 'sha256': sha(press_bytes),
         'frame_offset': offset['press'], 'pairs': _pairs(press, PRESS_LAYERS), 'activation_scale': PRESS_SCALE,
         **layout['press']},
    ]
    return {'units': UNITS, 'connections': CONNECTIONS, 'networks': nets,
            'link_format': '[src unit in "from", dst unit in "to", weight]; the top |w| links of each layer pair',
            'activation_mapping': {
                'input': 'u8 = 255 * min(|z| / 3, 1), z = the normalized input the network sees',
                'hidden': 'u8 = 255 * min(max(a, 0) / scale, 1) (ELU >= -1; negative = 0)',
                'output': 'neck / motor: u8 = 255 * min(|a|, 1) (the env clips the command at 1); steering PRESS: '
                          'u8 = 255 * min(max(a, 0) / 0.5, 1) (a > 0 starts a press)',
                'press_idle': 'the press network is all zero while it is not driving the body'},
            'frame': frame_layout(),
            'label': 'artificial network units (not anatomy)'}


def frame_layout():
    return {'magic': FRAME_MAGIC, 'hz': 25, 'endian': 'little', 'header_float32': HEADER_N,
            'header_fields': list(HEADER_FIELDS), 'activations_uint8': UNITS, 'bytes': FRAME_BYTES,
            'activation_order': [{'net': 'steer', 'layer': l, 'n': n} for l, n in zip(STEER_LAYERS, STEER_SIZES)]
            + [{'net': 'press', 'layer': l, 'n': n} for l, n in zip(PRESS_LAYERS, PRESS_SIZES)]}


def _u8(q):
    return (np.clip(q, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def quantize_steer(acts):
    x, h1, h2, h3, out = acts
    s = STEER_SCALE
    o = np.abs(out) / s['output']
    o[4] = max(float(out[4]), 0.0) / s['press_output']
    return _u8(np.concatenate([np.abs(x) / s['input'], np.maximum(h1, 0) / s['h1'], np.maximum(h2, 0) / s['h2'],
                               np.maximum(h3, 0) / s['h3'], o]))


def quantize_press(acts):
    x, h1, h2, h3, mo = acts
    s = PRESS_SCALE
    return _u8(np.concatenate([np.abs(x) / s['input'], np.maximum(h1, 0) / s['h1'], np.maximum(h2, 0) / s['h2'],
                               np.maximum(h3, 0) / s['h3'], np.abs(mo) / s['motor']]))


ZERO_STEER = np.zeros(STEER_UNITS, np.uint8)
ZERO_PRESS = np.zeros(PRESS_UNITS, np.uint8)


def brain_frame(sim_t, brain_on, cursor, tgt, holding, locked, lever, defl, hits, misses, pressing=False,
                press_step=0, steer_acts=None, press_acts=None):
    if tgt is None:
        t = (-1.0, -1.0, -1.0, -1.0)
    else:
        (cx, cy), (hw, hh) = tgt
        t = (cx, cy, hw, hh)
    hdr = np.array([FRAME_MAGIC, sim_t, 1.0 if brain_on else 0.0, cursor[0], cursor[1], *t,
                    1.0 if holding else 0.0, 1.0 if locked else 0.0, lever, defl[0], defl[1], hits, misses,
                    1.0 if pressing else 0.0, float(press_step)], '<f4')
    s = ZERO_STEER if steer_acts is None else quantize_steer(steer_acts)
    p = ZERO_PRESS if press_acts is None else quantize_press(press_acts)
    return hdr.tobytes() + s.tobytes() + p.tobytes()


# ------------------------------------------------------------------------------------------------------------
# page scripts
# ------------------------------------------------------------------------------------------------------------
# The rat's cursor, the cue window and click rings, drawn INTO the pons page (so they are in every streamed frame;
# pointer-events:none, so they never take a click). The cursor follows the page mouse, which the rig moves to the
# rat's cursor pixel 25 times a second. __ratDowns records every real (trusted) mousedown the page receives, so a
# run can prove that misses were never forwarded.
OVERLAY_JS = r"""
(() => {
  if (window.top !== window) return;
  if (window.__ratOverlay) return;
  window.__ratOverlay = true;
  const DEV = __DEV__;
  // the rig page's palette: a hot-magenta rat, a golden-yellow cue window, electric-yellow hit rings, grey misses.
  // DEV keeps its amber ARROW, so a scripted cursor never looks like the rat.
  const COL = DEV ? "#ffb020" : "#D60C94";
  const CUE = DEV ? "#ffb020" : "#FCF010";
  const HIT = DEV ? "#ffb020" : "#FFF40F";
  const LAB = DEV ? "DEV script" : "rat";
  const ARROW = '<svg width="22" height="28" viewBox="0 0 22 28" style="display:block;overflow:visible;' +
    'filter:drop-shadow(0 1px 2px rgba(0,0,0,.7))"><path d="M1 1 L1 22 L6.5 16.8 L10.2 26 L14 24.4 L10.4 15.4 ' +
    'L18 15.4 Z" fill="' + COL + '" stroke="#07070a" stroke-width="1.6" stroke-linejoin="round"/></svg>';
  // the rat icon, ~38 px nose to tail. The SVG's user-space origin (0,0) sits exactly on the cursor point and the
  // rat's NOSE TIP (the yellow dot) is drawn at that origin, facing up-left like an arrow: a click lands on the nose.
  const RATSVG = '<svg width="52" height="48" viewBox="-6 -6 52 48" style="position:absolute;left:-6px;top:-6px;' +
    'display:block;overflow:visible;filter:drop-shadow(0 1px 2px rgba(0,0,0,.8))">' +
    '<g transform="rotate(36)" stroke-linejoin="round" stroke-linecap="round">' +
    '<path d="M31 1.6C37.5 3 40.5 8.6 36.6 11.8C33.4 14.4 29.4 12.2 31 9.6" fill="none" stroke="#FCF010" stroke-width="3.3"/>' +
    '<path d="M31 1.6C37.5 3 40.5 8.6 36.6 11.8C33.4 14.4 29.4 12.2 31 9.6" fill="none" stroke="#D60C94" stroke-width="1.6"/>' +
    '<ellipse cx="9.6" cy="4.7" rx="2.1" ry="1.35" fill="#D60C94" stroke="#FCF010" stroke-width=".9"/>' +
    '<ellipse cx="24.6" cy="5.3" rx="2.7" ry="1.45" fill="#D60C94" stroke="#FCF010" stroke-width=".9"/>' +
    '<path d="M0 0C2-1.6 5-3.4 8.5-4.6C11-7.2 16-9.2 22-8.6C28-8 32.5-5 32.5-.6C32.5 2.6 30 4.4 26.5 4.6' +
    'C20 5.6 13 5.4 9 4C6 3.2 2.5 1.8 0 0Z" fill="#D60C94" stroke="#FCF010" stroke-width="1.1"/>' +
    '<circle cx="11.6" cy="-6.4" r="2.9" fill="#F59AAF" stroke="#FCF010" stroke-width=".9"/>' +
    '<circle cx="11.9" cy="-6.1" r="1.4" fill="#EA3560"/>' +
    '<circle cx="6.1" cy="-1.5" r="1" fill="#000"/>' +
    '<circle cx="0" cy="0" r="1.25" fill="#FFF40F" stroke="#000" stroke-width=".5"/>' +
    '</g></svg>';
  let x = -200, y = -200;
  window.__ratDowns = [];
  const root = () => document.documentElement;
  // the cursor's label breathes gently: the page never sits perfectly still, so the compositor keeps producing
  // frames and the CDP screencast holds its ~15-20 fps (a still page makes no screencast frames at all)
  function style() {
    if (document.getElementById("__ratstyle") || !root()) return;
    const s = document.createElement("style");
    s.id = "__ratstyle";
    s.textContent = "@keyframes __ratbreath{0%,100%{opacity:1}50%{opacity:.8}}";
    root().appendChild(s);
  }
  function cursor() {
    let c = document.getElementById("__ratcur");
    if (c) return c;
    if (!root()) return null;
    c = document.createElement("div");
    c.id = "__ratcur";
    c.style.cssText = "position:fixed;left:0;top:0;pointer-events:none;z-index:2147483647;will-change:transform;";
    c.innerHTML = (DEV ? ARROW : RATSVG) +
      '<div style="position:absolute;left:' + (DEV ? '18px;top:24px' : '31px;top:13px') + ';' +
      'font:700 10px/1 Bahnschrift,Segoe UI,system-ui,sans-serif;' +
      'letter-spacing:.08em;padding:3px 6px;border-radius:3px;white-space:nowrap;text-transform:uppercase;' +
      'color:#07070a;background:' + (DEV ? COL : CUE) + (DEV ? '' : ';box-shadow:0 0 0 1px ' + COL) + '">' + LAB + '</div>';
    root().appendChild(c);
    style();
    c.lastChild.style.animation = "__ratbreath 1.6s ease-in-out infinite";
    return c;
  }
  function place() { const c = cursor(); if (c) c.style.transform = "translate(" + x + "px," + y + "px)"; }
  addEventListener("mousemove", e => { x = e.clientX; y = e.clientY; place(); }, { capture: true, passive: true });
  addEventListener("mousedown", e => {
    if (e.isTrusted) window.__ratDowns.push([Math.round(e.clientX * 10) / 10, Math.round(e.clientY * 10) / 10, Date.now()]);
  }, { capture: true, passive: true });
  window.__ratCue = (b) => {
    let q = document.getElementById("__ratcue");
    if (!b) { if (q) q.remove(); return false; }
    if (!root()) return false;
    if (!q) {
      q = document.createElement("div");
      q.id = "__ratcue";
      q.style.cssText = "position:fixed;pointer-events:none;z-index:2147483646;box-sizing:border-box;" +
        "border:2px solid " + CUE + ";background:" + CUE + (DEV ? "26" : "1f") + ";border-radius:4px;" +
        "box-shadow:0 0 0 3px rgba(7,7,10,.55)" + (DEV ? "" : ",0 0 18px 2px rgba(245,172,41,.5)") + ";";
      q.innerHTML = '<span style="position:absolute;left:-2px;bottom:100%;margin-bottom:3px;' +
        'font:700 10px/1 Bahnschrift,Segoe UI,system-ui,sans-serif;letter-spacing:.08em;text-transform:uppercase;' +
        'white-space:nowrap;padding:3px 6px;border-radius:3px;color:#07070a;background:' + CUE + '"></span>';
      root().appendChild(q);
    }
    q.style.left = b.x + "px"; q.style.top = b.y + "px"; q.style.width = b.w + "px"; q.style.height = b.h + "px";
    q.firstChild.textContent = "cue · " + (b.label || "");
    return true;
  };
  window.__ratRing = (px, py, hit) => {
    if (!root()) return;
    const r = document.createElement("div");
    const c = hit ? HIT : "#9a9aa6";
    r.style.cssText = "position:fixed;left:" + (px - 20) + "px;top:" + (py - 20) + "px;width:40px;height:40px;" +
      "border-radius:50%;border:3px solid " + c + ";pointer-events:none;z-index:2147483646;box-sizing:border-box;";
    root().appendChild(r);
    const a = r.animate([{ transform: "scale(.35)", opacity: 1 }, { transform: "scale(1.9)", opacity: 0 }],
      { duration: hit ? 700 : 900, easing: "ease-out" });
    a.onfinish = () => r.remove();
  };
})();
"""

# The real clickable box of each target, measured now. Tags the element with data-ratcue (VERIFY_JS checks that
# the rat's click lands on it). ready = present, visible, enabled and not covered at its centre.
MEASURE_JS = r"""([kind, arg, sym, sel]) => {
  const vis = e => { if (!e) return false; const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && +cs.opacity > 0.05; };
  const lines = e => (e.innerText || '').split(String.fromCharCode(10)).map(s => s.trim()).filter(Boolean);
  const txt = e => (e.innerText || e.textContent || '').trim();
  let el = null, box = null;
  const out = {kind};
  if (kind === 'terms') {
    // pons (2026-09-24): <div class=legal-acceptance-option><input id=legal-accept-terms type=checkbox><span>
    // <label for=legal-accept-terms>I have read and accept the </label><a href=/terms target=_blank>Terms of Use</a>.
    // </span></div>. Only the checkbox and its label tick it (the row's padding does nothing; the link opens a
    // document), so the target is the label: the biggest rectangle that is all clickable.
    const cbs = [...document.querySelectorAll('input[type=checkbox]')].filter(vis);
    const rx = arg === 0 ? /terms of use/i : /privacy policy/i;
    // the checkbox's own row: its nearest ancestor with any text (never a wrapper holding both rows)
    const rowText = c => { for (let d = c.parentElement, i = 0; d && i < 3; d = d.parentElement, i++) {
      const t = (d.innerText || '').trim(); if (t) return rx.test(t); } return false; };
    const labOf = c => (c.id && document.querySelector('label[for="' + CSS.escape(c.id) + '"]')) || c.closest('label') || null;
    let cb = cbs.find(rowText) || cbs[arg];
    if (!cb) return null;
    const lab = labOf(cb);
    el = lab && vis(lab) ? lab : cb;
    const r = el.getBoundingClientRect(), cr = cb.getBoundingClientRect();
    let x1 = r.right;
    // a link in the row opens a document instead of ticking the box: the target stops short of it
    for (const a of el.querySelectorAll('a')) { const ar = a.getBoundingClientRect(); if (ar.width && ar.left > cr.right) x1 = Math.min(x1, ar.left - 2); }
    box = {x: r.left, y: r.top, w: x1 - r.left, h: r.height};
    out.checked = cb.checked; out.via = el === cb ? 'checkbox' : 'label'; out.label = txt(el).slice(0, 90);
    out.cut_at_link = x1 < r.right;
  } else if (kind === 'accept') {
    el = [...document.querySelectorAll('button')].find(b => /^\s*accept and continue\s*$/i.test(b.textContent || '') && vis(b));
  } else if (kind === 'image') {
    const leaf = [...document.querySelectorAll('body *')].find(e => e.children.length === 0 && vis(e) && /^\s*choose image\s*$/i.test(e.textContent || ''));
    if (!leaf) return null;
    for (let d = leaf, i = 0; d && d !== document.body && i < 7; d = d.parentElement, i++) {
      if (d.tagName === 'LABEL' || d.tagName === 'BUTTON' || d.getAttribute('role') === 'button'
          || (d.querySelector && d.querySelector('input[type=file]'))) { el = d; break; }
    }
    el = el || leaf;
    out.zone = el.tagName + (el.getAttribute('role') ? '[role=' + el.getAttribute('role') + ']' : '');
  } else if (kind === 'field' || kind === 'tax') {
    el = document.querySelector(sel);
    if (el) { out.value = el.value; out.readonly = !!el.readOnly; }
  } else if (kind === 'advanced') {
    el = [...document.querySelectorAll('button')].find(b => /^\s*advanced\s*$/i.test(b.textContent || '') && vis(b));
    if (el) out.expanded = el.getAttribute('aria-expanded') === 'true';
  } else if (kind === 'launch') {
    const bs = [...document.querySelectorAll('button')]
      .map(b => ({b, r: b.getBoundingClientRect(), t: (b.textContent || '').trim()}))
      .filter(o => o.r.width > 300 && o.r.height > 40 && !/^(advanced|eth|usdc)$/i.test(o.t) && !/creator fees/i.test(o.t));
    const byText = bs.find(o => /^launch token$/i.test(o.t));
    const pick = byText || bs.sort((p, q) => (q.r.y + scrollY) - (p.r.y + scrollY))[0];
    if (!pick) return null;
    el = pick.b; out.label = pick.t;
  } else if (kind === 'confirm') {
    const want = ('launch ' + String(sym || '')).toLowerCase();
    const btns = [...document.querySelectorAll('button')].filter(x => /^\s*confirm\s*$/i.test(x.textContent || '') && vis(x));
    for (const b of btns) {
      let d = b, hit = null;
      for (let i = 0; i < 14 && d.parentElement && d.parentElement !== document.body; i++) {
        d = d.parentElement;
        if (lines(d).some(l => l.toLowerCase() === want)) { hit = d; break; }
      }
      if (hit && sym) { el = b; out.title = 'Launch ' + sym; out.dialog = lines(hit).slice(0, 32); break; }
    }
  }
  if (!el) return null;
  document.querySelectorAll('[data-ratcue]').forEach(e => { if (e !== el) e.removeAttribute('data-ratcue'); });
  el.setAttribute('data-ratcue', kind);
  if (!box) { const r = el.getBoundingClientRect(); box = {x: r.left, y: r.top, w: r.width, h: r.height}; }
  // rounded corners do not take clicks: keep the target rectangle inside the rounded box (0.29 r per side)
  const cs = getComputedStyle(el);
  const rad = Math.min(Math.max(...['borderTopLeftRadius', 'borderTopRightRadius', 'borderBottomLeftRadius',
    'borderBottomRightRadius'].map(k => parseFloat(cs[k]) || 0)), box.w / 2, box.h / 2);
  const ins = Math.ceil(rad * (1 - Math.SQRT1_2));
  if (ins > 0 && box.w > 2 * ins + 4 && box.h > 2 * ins + 4) {
    box = {x: box.x + ins, y: box.y + ins, w: box.w - 2 * ins, h: box.h - 2 * ins}; out.corner_inset = ins;
  }
  out.x = box.x; out.y = box.y; out.w = box.w; out.h = box.h;
  out.vw = innerWidth; out.vh = innerHeight; out.scrollY = scrollY;
  out.disabled = !!el.disabled || el.getAttribute('aria-disabled') === 'true';
  if (out.label === undefined) out.label = txt(el).slice(0, 60);
  let fixed = false;
  for (let d = el; d && d !== document.documentElement; d = d.parentElement) if (getComputedStyle(d).position === 'fixed') { fixed = true; break; }
  out.fixed = fixed;
  const cx = box.x + box.w / 2, cy = box.y + box.h / 2;
  out.centerInView = cx >= 0 && cx < innerWidth && cy >= 0 && cy < innerHeight;
  const top = out.centerInView ? document.elementFromPoint(cx, cy) : null;
  out.onTop = !!top && (top === el || el.contains(top));
  if (top && !out.onTop) out.covered_by = top.tagName.toLowerCase() + (top.id ? '#' + top.id : '')
    + (typeof top.className === 'string' && top.className.trim() ? '.' + top.className.trim().split(/\s+/).slice(0, 2).join('.') : '');
  out.visible = vis(el);
  const why = !out.visible ? 'not visible' : out.disabled ? 'disabled' : out.readonly ? 'read-only'
            : !(box.w > 2 && box.h > 2) ? 'no size' : !out.centerInView ? 'out of view' : !out.onTop ? 'covered' : '';
  out.why = why; out.ready = !why;
  return out; }"""

VERIFY_JS = r"""([x, y, kind]) => {
  const t = document.querySelector('[data-ratcue]');
  const e = document.elementFromPoint(x, y);
  if (!t) return {ok: false, why: 'the target element is gone'};
  if (!e) return {ok: false, why: 'nothing at that point'};
  const inside = t === e || t.contains(e);
  const link = e.closest('a');
  if (!inside) return {ok: false, why: 'the point is on ' + e.tagName.toLowerCase() + ', not the target'};
  if (kind === 'terms' && link) return {ok: false, why: 'the point is on a link'};
  return {ok: true, on: e.tagName.toLowerCase()}; }"""

FOCUS_JS = """() => { const a = document.activeElement, t = document.querySelector('[data-ratcue]');
  return !!(a && t && (a === t || t.contains(a))); }"""


class StageFailed(RuntimeError):
    def __init__(self, stage, detail):
        super().__init__(f'{stage}: {detail}')
        self.stage, self.detail = stage, str(detail)[:400]


class RunEnded(RuntimeError):
    pass


class Target:
    def __init__(self, n, key, label, kind, arg):
        self.n, self.key, self.label, self.kind, self.arg = n, key, label, kind, arg
        self.state = 'pending'
        self.lights = []            # every time it was lit: {at, box (page px), norm, cmd}
        self.misses = []
        self.hit = None
        self.relights = 0
        self.detail = ''
        self.result = {}
        self.fut = None

    def record(self):
        return {'n': self.n, 'key': self.key, 'label': self.label, 'kind': self.kind, 'state': self.state,
                'detail': self.detail, 'lights': self.lights, 'hit': self.hit, 'misses': self.misses,
                'relights': self.relights, 'result': self.result}


# ------------------------------------------------------------------------------------------------------------
# --dev-oracle: a scripted cursor (NOT the rat)
# ------------------------------------------------------------------------------------------------------------
class Oracle:
    """--dev-oracle ONLY. A scripted cursor with the Session's command interface (target / hold / stop, run at 50
    Hz in real time) that eases to each lit target and clicks it, so the pons automation can be exercised while the
    brain is still training. It is not the rat, it has no brain, and nothing it does is recorded as a brain run.
    On the first target it makes one deliberate click outside the lit box, to exercise the miss mask end to end."""

    def __init__(self, seed, preroll_s=PREROLL_S, cursor=(0.5, 0.5)):
        self.seed, self.preroll_s = seed, preroll_s
        self.cmds = queue.Queue()
        self.stop_flag = threading.Event()
        self.rng = np.random.default_rng(seed)
        self.cursor = np.array(cursor, float)
        self.tc = self.th = None
        self.holding = True
        self.aim = None
        self.dwell = 0
        self.armed, self.rearm = True, 0
        self.t = 0
        self.hits = self.misses = 0
        self.n_targets = 0
        self.miss_pending = False
        self.log, self.clicks = [], []

    def target(self, cx, cy, hw, hh):
        self.cmds.put(('target', round(float(cx), 6), round(float(cy), 6), round(float(hw), 6), round(float(hh), 6)))

    def hold(self):
        self.cmds.put(('hold',))

    def stop(self):
        self.stop_flag.set()

    def _inside(self, p):
        return (self.tc is not None and abs(p[0] - self.tc[0]) <= self.th[0] and abs(p[1] - self.tc[1]) <= self.th[1])

    def _aim(self):
        if self.miss_pending:
            p = np.array([self.tc[0] - self.th[0] - 0.035, self.tc[1]])
            if p[0] > 0.01 and not self._inside(p):
                return p
            self.miss_pending = False
        return self.tc + self.rng.uniform(-0.3, 0.3, 2) * self.th

    def run(self, on_tick, on_click):
        t0 = time.perf_counter()
        tick = 0

        def pace():
            nonlocal tick
            tick += 1
            dt = t0 + tick * CTRL_DT - time.perf_counter()
            if dt > 0:
                time.sleep(dt)
        sim_t = 0.0
        for _ in range(int(round(self.preroll_s / CTRL_DT))):
            sim_t += CTRL_DT
            on_tick(self, sim_t, False)
            pace()
            if self.stop_flag.is_set():
                return None, {}
        while not self.stop_flag.is_set():
            try:
                cmd = self.cmds.get_nowait()
            except queue.Empty:
                cmd = None
            if cmd is not None:
                if cmd[0] == 'target':
                    self.tc, self.th = np.array(cmd[1:3]), np.array(cmd[3:5])
                    self.holding = False
                    self.n_targets += 1
                    self.miss_pending = self.n_targets == 1
                    self.aim, self.dwell = self._aim(), 0
                else:
                    self.holding, self.aim = True, None
                self.log.append((self.t, list(cmd)))
            if self.aim is not None:
                d = self.aim - self.cursor
                dist = float(np.hypot(*d))
                speed = min(0.8, 5.0 * dist)                  # screen widths per second, easing in
                stepv = d if dist < 1e-9 or speed * CTRL_DT >= dist else d / dist * speed * CTRL_DT
                self.cursor = np.clip(self.cursor + stepv, 0.0, 1.0)
                self.dwell = self.dwell + 1 if dist < 0.0015 else 0
                if self.armed and self.dwell >= 12:
                    hit = (not self.holding) and self._inside(self.cursor)
                    click = (self.t, float(self.cursor[0]), float(self.cursor[1]), bool(hit))
                    self.clicks.append(list(click))
                    self.armed, self.rearm = False, 15
                    if hit:
                        self.hits += 1
                        self.holding, self.aim = True, None
                    else:
                        self.misses += 1
                        self.miss_pending = False
                        self.aim, self.dwell = self._aim(), 0
                    on_click(click, self)
            if not self.armed:
                self.rearm -= 1
                if self.rearm <= 0:
                    self.armed = True
            self.t += 1
            sim_t += CTRL_DT
            on_tick(self, sim_t, True)
            pace()
        return None, {}


# ------------------------------------------------------------------------------------------------------------
# the websocket outbox
# ------------------------------------------------------------------------------------------------------------
class Outbox:
    """One sender for the websocket, fed from the loop and (thread-safely) from the sim thread. Every JSON event
    is also appended to events.jsonl (shot JPEGs replaced by their size + sha256). A viewer that disconnects does
    not stop the run: it finishes and is recorded regardless. At most one pons frame waits to be sent."""

    def __init__(self, ws, events_path):
        self.ws = ws
        self.loop = asyncio.get_running_loop()
        self.q = asyncio.Queue()
        self.ok = ws is not None
        self.shots_pending = 0
        self.shot_idle = asyncio.Event()
        self.shot_idle.set()
        self.n = {'json': 0, 'shot': 0, 'frame': 0, 'shot_dropped': 0}
        self.shot_times = []
        self.ev = open(events_path, 'w', encoding='utf-8', newline='\n')
        self.task = asyncio.create_task(self._pump())

    def shot_b64(self, b64, w, h):              # loop thread only
        raw = base64.b64decode(b64)
        rec = {'type': 'shot', 'w': w, 'h': h, 'jpg_bytes': len(raw), 'jpg_sha256': sha(raw)}
        self.shots_pending += 1
        self.shot_idle.clear()
        self.n['shot'] += 1
        self.shot_times.append(time.time())
        self.json({'type': 'shot', 'jpg': b64, 'w': w, 'h': h}, rec)

    def json(self, msg, rec=None):              # loop thread only
        rec = rec or msg
        self.n['json'] += 1
        try:
            self.ev.write(json.dumps({'ts': round(time.time(), 3), **rec}, default=str) + '\n')
        except Exception:
            pass
        self.q.put_nowait(('t', msg))

    def json_ts(self, msg):                     # any thread
        self.loop.call_soon_threadsafe(self.json, msg)

    def frame_ts(self, b):                      # any thread
        self.loop.call_soon_threadsafe(self._frame, b)

    def _frame(self, b):
        self.n['frame'] += 1
        self.q.put_nowait(('b', b))

    async def _pump(self):
        while True:
            kind, item = await self.q.get()
            if kind == 'end':
                return
            is_shot = kind == 't' and item.get('type') == 'shot'
            try:
                if self.ok:
                    if kind == 't':
                        await self.ws.send_text(json.dumps(item, default=str))
                    else:
                        await self.ws.send_bytes(item)
            except Exception:
                self.ok = False                 # viewer gone; keep recording
            if is_shot:
                self.shots_pending -= 1
                if self.shots_pending <= 0:
                    self.shots_pending = 0
                    self.shot_idle.set()

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


# ------------------------------------------------------------------------------------------------------------
# one run
# ------------------------------------------------------------------------------------------------------------
class Run:
    def __init__(self, ws, seed):
        self.ws = ws
        self.seed = int(seed)
        self.oracle = bool(CONFIG['dev_oracle'])
        self.loop = asyncio.get_running_loop()
        self.coin = CONFIG['coin']
        self.stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        self.run_dir = ROOT / 'runs' / (f'brainrig_{self.stamp}_seed{self.seed}' + ('_DEVORACLE' if self.oracle else ''))
        self.targets = [Target(i + 1, *t) for i, t in enumerate(TARGETS)]
        self.out = None
        self.bot = None
        self.cdp = None
        self.sess = None
        self.steer = None                           # the session's SteerEnv (read-only; for the press display)
        self.steer_missing = False
        self.was_pressing = False
        self.presses_seen = 0
        self.commit = None
        self.description = None
        self.sim_thread = None
        self.over = self.loop.create_future()      # the session thread ended (stop, fall, max steps, crash)
        self.stop_fut = self.loop.create_future()  # the viewer asked to stop
        self.sim_end = None
        self.sim_info = {}
        self.brain_on = asyncio.Event()
        self.brain_seen = False
        self.nstep = 0
        self.cursor = None                          # normalized, written by the sim thread
        self.cmd_lock = threading.Lock()
        self.cmd_trace = []                         # every command queued, in queue order
        self.lit = None                             # the Target the rat may hit now (None = hold)
        self.cur = None                             # the Target being worked on (misses are attributed to it)
        self.clicks = []
        self.mouse_on = False
        self.mouse_task = None
        self.mouse_lock = asyncio.Lock()
        self.streaming = False
        self.frame_lock = asyncio.Lock()
        self.last_b64 = None
        self.last_ack = 0.0
        self.image_uri = None
        self.tax_ok = False
        self.sends = []
        self.handled_send = None
        self.capture = None
        self.send_entered = asyncio.Event()
        self.tx_seen = asyncio.Event()
        self.rejection = None
        self.rejection_task = None
        self.final_jpg = None
        self.page_downs = None
        self.dev_paths = []
        self.typing = {}
        self.t_start = time.time()
        # LIVE (all unused in DRY)
        self.mode = CONFIG.get('mode', 'DRY')
        self.live_mode = self.mode == 'LIVE'
        self.cfg = None                             # launcher.config() re-read for this run (LIVE)
        self.live = None                            # ponsbot.LiveLaunch, once armed
        self.live_hash = None
        self.live_result = None
        self.live_receipt = None
        self.success = None
        self.success_task = None
        self.success_deadline = None                # _watch_success stops at this time (shortened after the receipt)
        self.coin_page = None
        self.sign_started = False                   # pons's tx was handed to the signer: nothing can stop it now
        self.armed_at = None                        # when _live_arm reserved the journal (LIVE_CONFIRM_MAX_S counts)

    # ---- events ----------------------------------------------------------------------------------------------
    def log(self, msg):
        say('  ' + str(msg)[:400])
        if self.out:
            self.out.json({'type': 'log', 'msg': str(msg)})

    def log_ts(self, msg):                          # any thread
        self.loop.call_soon_threadsafe(self.log, msg)

    def abort_live(self, reason):
        """LIVE, loop thread: stop this run unless pons's launch tx was already handed to the signer. -> True if the run
        will end without signing (the reason is kept), False if it is too late (or DRY: never used there)."""
        if not self.live_mode or self.sign_started:
            return False
        if not self.stop_fut.done():
            self.stop_fut.set_result(str(reason))
        return True

    def stage(self, tg, state, detail=''):
        tg.state = state
        tg.detail = str(detail)
        say(f'[stage] {tg.key:<18} {state:<6} {str(detail)[:160]}')
        d = str(detail)
        if len(d) > STAGE_DETAIL_MAX:
            self.log(f'{tg.key} {state}: {d}')
            d = d[:STAGE_DETAIL_MAX - 1].rstrip() + '…'
        msg = {'type': 'stage', 'stage': tg.key, 'n': tg.n, 'label': tg.label, 'state': state, 'detail': d}
        if state == 'active' and tg.lights and tg.lights[-1].get('box'):
            msg['box'] = tg.lights[-1]['box']
        self.out.json(msg)

    # ---- commands to the brain (every queue put goes through here, under cmd_lock) ------------------------------
    def _cmd(self, cmd, target=None):
        """Caller holds cmd_lock."""
        if cmd[0] == 'target':
            self.sess.target(*cmd[1:])
        else:
            self.sess.hold()
        self.cmd_trace.append({'cmd': list(cmd), 'target': target, 'at': time.time()})
        return len(self.cmd_trace) - 1

    def _light(self, tg, m, only_if_lit=False):
        """Light tg at its measured box. only_if_lit: re-light only if tg is still the lit target (atomically:
        a hit the sim thread takes in the meantime wins, and nothing is re-lit). -> the box, or None."""
        # 1 px inside the element's real clickable box (never padded beyond it)
        x, y, w, h = m['x'] + 1, m['y'] + 1, max(1.0, m['w'] - 2), max(1.0, m['h'] - 2)
        norm = [(x + w / 2) / VIEW_W, (y + h / 2) / VIEW_H, (w / 2) / VIEW_W, (h / 2) / VIEW_H]
        with self.cmd_lock:
            if only_if_lit and self.lit is not tg:
                return None
            if not only_if_lit or tg.fut is None or tg.fut.done():
                tg.fut = self.loop.create_future()
            self.lit = tg
            idx = self._cmd(('target', *norm), tg.key)
        box = [round(x, 1), round(y, 1), round(w, 1), round(h, 1)]
        tg.lights.append({'at': time.time(), 'box': box, 'norm': [round(v, 6) for v in norm], 'cmd': idx})
        asyncio.ensure_future(self._cue({'x': x, 'y': y, 'w': w, 'h': h, 'label': tg.label}))
        return box

    def _withdraw(self, tg):
        with self.cmd_lock:
            if self.lit is not tg:
                return False
            self.lit = None
            self._cmd(('hold',), tg.key)
        asyncio.ensure_future(self._cue(None))
        return True

    # ---- sim thread --------------------------------------------------------------------------------------------
    def _sim(self):
        reason = None
        try:
            if self.oracle:
                self.sess.run(self._on_tick_oracle, self._on_click)
                reason = 'stopped'
            else:
                _, info = self.sess.run(on_step=self._on_step, on_click=self._on_click, realtime=True)
                self.sim_info = {k: (bool(v) if isinstance(v, (bool, np.bool_)) else v) for k, v in (info or {}).items()
                                 if k in ('fell', 'hits', 'misses', 'timeouts', 'program')}
                st = getattr(self.sess, 'steer_env', None)
                if st is not None:
                    self.sim_info['presses'] = int(getattr(st, 'presses', 0))
                reason = ('stopped' if self.sess.stop_flag.is_set() else
                          'the rat fell' if (info or {}).get('fell') else 'the session reached its step limit')
        except Exception as e:
            traceback.print_exc()
            reason = f'the session crashed: {e!r}'[:300]
        self.loop.call_soon_threadsafe(self._sim_over, reason)

    def _sim_over(self, reason):
        self.sim_end = reason
        if not self.over.done():
            self.over.set_result(reason)
        if reason != 'stopped':
            self.log(f'the brain session ended: {reason}')

    def _steer_env_of(self, env):
        """The SteerEnv (steering net's env + the press network) wrapping this CursorEnv, READ-ONLY. Session.run
        only publishes it as session.steer_env when the run ends, so during the run it is found as the local `env`
        of Session.run, the frame that called on_step (checked: its .e must be this CursorEnv)."""
        s = getattr(self.sess, 'steer_env', None)
        if s is not None and getattr(s, 'e', None) is env:
            return s
        f = sys._getframe(2)                        # _on_step's caller: Session.run
        for _ in range(4):
            if f is None:
                break
            cand = f.f_locals.get('env')
            if cand is not None and getattr(cand, 'e', None) is env and hasattr(cand, 'press_pol'):
                return cand
            f = f.f_back
        return None

    def _on_step(self, env, phase, info, obs):
        """Session.run, every control step (50 Hz). Reads env only; never changes it."""
        self.cursor = (float(env.cursor[0]), float(env.cursor[1]))
        brain = phase == 'brain'
        if self.steer is None and not self.steer_missing:
            self.steer = self._steer_env_of(env)
            if self.steer is None:
                self.steer_missing = True
                self.loop.call_soon_threadsafe(self.log, 'note: the press network could not be located for the '
                                               'display; its activations are shown as zero')
        st = self.steer
        press_obs = getattr(st, 'last_press_obs', None) if (st is not None and brain) else None
        pressing = press_obs is not None               # the press network drove the body on this control step
        if pressing and not self.was_pressing:
            self.presses_seen += 1
        self.was_pressing = pressing
        if brain and not self.brain_seen:
            self.brain_seen = True
            self.loop.call_soon_threadsafe(self._brain_on)
        self.nstep += 1
        if self.nstep % 2:
            return                                  # frames at 25 Hz (a press program runs for >= 0.8 s)
        holding = env.hold != 0
        tgt = None if holding else ((float(env.tc[0]), float(env.tc[1])), (float(env.th[0]), float(env.th[1])))
        s_acts = brain_session.activations(self.sess.pol, obs) if (brain and obs is not None) else None
        p_acts = brain_session.activations(st.press_pol, press_obs) if pressing else None
        prog = int(getattr(st, 'program', 0) or 0) if st is not None else 0
        self.out.frame_ts(brain_frame(env.d.time, brain, self.cursor, tgt, holding, env.locked, env.lever_angle(),
                                      env._deflection(), env.hits, env.misses, pressing=pressing, press_step=prog,
                                      steer_acts=s_acts, press_acts=p_acts))

    def _on_tick_oracle(self, o, sim_t, active):
        self.cursor = (float(o.cursor[0]), float(o.cursor[1]))
        if active and not self.brain_seen:
            self.brain_seen = True
            self.loop.call_soon_threadsafe(self._brain_on)
        self.nstep += 1
        if self.nstep % 2:
            return
        tgt = None if o.holding or o.tc is None else ((float(o.tc[0]), float(o.tc[1])), (float(o.th[0]), float(o.th[1])))
        self.out.frame_ts(brain_frame(sim_t, False, self.cursor, tgt, o.holding, False, 0.0, (0.0, 0.0),
                                      o.hits, o.misses))

    def _on_click(self, click, env):
        """Sim thread, the instant a click registers: (step, x, y, hit). A hit on the lit target holds the brain at
        once and is forwarded; everything else is a miss and is never forwarded to the page."""
        step, x, y, hit = click
        with self.cmd_lock:
            tg = self.lit
            forward = bool(hit) and tg is not None
            if forward:
                self.lit = None
                self._cmd(('hold',), tg.key)
        self.loop.call_soon_threadsafe(self._rat_click, int(step), float(x), float(y), bool(hit), tg, forward)

    # ---- loop side of a click -------------------------------------------------------------------------------------
    def _brain_on(self):
        if self.oracle:
            self.log('DEV: the scripted cursor is on (NOT the rat)')
        else:
            self.log(f'brain on after {PREROLL_S:g} s brain-off pre-roll: the steering network turns the head at 50 Hz '
                     '(the head moves the cursor) and decides when to press; each press is performed by the '
                     'lever-press network with the whole body')
        self.brain_on.set()

    def _rat_click(self, step, x, y, hit, tg, forward):
        """A hit on the lit target goes to _target (which checks the page point, then forwards it and reports it);
        anything else is a miss: reported, ringed grey, never forwarded."""
        if forward and tg.fut is not None and not tg.fut.done():
            tg.fut.set_result((step, x, y))
            return
        why = ('a hit on a target the rig had just withdrawn' if hit else
               'while the rig held (no target lit)' if tg is None else 'outside the lit target')
        self._report_click(step, x, y, hit, False, self.cur.key if self.cur else None, why)

    def _report_click(self, step, x, y, env_hit, forwarded, target, why=None):
        px, py = x * VIEW_W, y * VIEW_H
        rec = {'step': step, 'x': round(px, 1), 'y': round(py, 1), 'norm': [round(x, 6), round(y, 6)],
               'env_hit': env_hit, 'forwarded': forwarded, 'target': target, 'at': time.time()}
        if why:
            rec['why'] = why
        self.clicks.append(rec)
        self.out.json({'type': 'click', 'x': rec['x'], 'y': rec['y'], 'hit': forwarded, 'target': target,
                       'forwarded': forwarded, 'step': step, **({'why': why} if why else {})})
        asyncio.ensure_future(self._ring(px, py, forwarded))
        if not forwarded:
            if self.cur is not None:
                self.cur.misses.append({'step': step, 'x': rec['x'], 'y': rec['y'], 'at': rec['at'], 'why': why})
            self.log(f'miss at ({px:.0f}, {py:.0f}), {why}: not forwarded to pons (masked)')
        return rec

    # ---- page helpers ---------------------------------------------------------------------------------------------
    async def _eval(self, js, arg=None):
        try:
            return await self.bot.page.evaluate(js, arg) if arg is not None else await self.bot.page.evaluate(js)
        except Exception:
            return None

    async def _cue(self, box):
        await self._eval('(b) => window.__ratCue ? window.__ratCue(b) : false', box if box else False)

    async def _ring(self, px, py, hit):
        await self._eval('([x, y, h]) => window.__ratRing && window.__ratRing(x, y, h)', [px, py, bool(hit)])

    async def _measure(self, tg):
        kind = {'name': 'field', 'ticker': 'field', 'description': 'field'}.get(tg.kind, tg.kind)
        sel = ponsbot.SEL.get(tg.kind) if kind == 'field' else (ponsbot.TAX_SEL if tg.kind == 'tax' else None)
        return await self._eval(MEASURE_JS, [kind, tg.arg, self.coin['symbol'], sel])

    @staticmethod
    def _in_view(m, margin=6):
        return m['y'] >= margin + 16 and m['y'] + m['h'] <= m['vh'] - margin and m['x'] >= 0 and m['x'] + m['w'] <= m['vw']

    def _ready(self, tg, m):
        """Extra per-target readiness on top of MEASURE_JS's."""
        if not m or not m.get('ready'):
            why = (m or {}).get('why') or 'not on the page yet'
            if why == 'covered' and m.get('covered_by'):
                why = f"covered by {m['covered_by']}"
            return False, why
        if tg.kind == 'launch' and not re.fullmatch(r'\s*launch token\s*', m.get('label') or '', re.I):
            return False, f"pons's button says {m.get('label')!r}"
        return True, ''

    async def _acquire(self, tg, timeout=ACQUIRE_S):
        """Wait until the target is on the page, clickable and in view (scrolling to it smoothly while the rat
        holds), stable for 150 ms. -> the measurement."""
        t_end = time.time() + timeout
        why = ''
        noted = False
        scrolls = 0
        while True:
            self._check_end()
            m = await self._measure(tg)
            ok, why = self._ready(tg, m)
            # on the page but (partly) outside the viewport: scroll it in first, whatever else it is waiting for
            if (m and m.get('visible') and not self._in_view(m)
                    and m.get('why') in ('', 'out of view', 'disabled', 'covered')):
                if m['fixed']:
                    ok, why = False, 'outside the viewport (in a fixed dialog)'
                elif scrolls < 6:
                    scrolls += 1
                    y = m['scrollY'] + m['y'] + m['h'] / 2 - m['vh'] * 0.45
                    self.log(f'scrolling {tg.label} into view (the rat holds)')
                    await self.bot.smooth_scroll(y, ms=850, steps=26)
                    await asyncio.sleep(0.3)
                    continue
                else:
                    ok, why = False, 'cannot scroll it fully into view'
            if ok:
                await asyncio.sleep(0.15)
                m2 = await self._measure(tg)
                ok2, _ = self._ready(tg, m2)
                if ok2 and self._in_view(m2) and all(abs(m2[k] - m[k]) <= 1.0 for k in ('x', 'y', 'w', 'h')):
                    return m2
                continue
            if time.time() > t_end:
                raise StageFailed(tg.key, f'{tg.label}: not clickable after {timeout:g} s ({why})')
            if not noted and time.time() > t_end - timeout + 3:
                noted = True
                self.log(f'waiting for {tg.label}: {why}')
            await asyncio.sleep(0.25)

    def _check_end(self):
        if self.stop_fut.done():
            r = self.stop_fut.result()
            raise RunEnded('stopped by the viewer' if r == 'viewer' else f'stopped: {r}')
        if self.over.done():
            raise RunEnded(self.sim_end or 'the brain session ended')
        if (self.live_mode and self.armed_at is not None and not self.sign_started
                and time.time() - self.armed_at > LIVE_CONFIRM_MAX_S):
            raise StageFailed('t11_confirm', f"the rat did not hit pons's Confirm within {LIVE_CONFIRM_MAX_S} s of "
                                             'arming; nothing was signed')

    async def _await_hit(self, tg):
        t0 = time.time()
        next_note = t0 + NO_HIT_NOTE_S
        while True:
            fut = tg.fut
            await asyncio.wait({fut, self.over, self.stop_fut}, timeout=REMEASURE_S,
                               return_when=asyncio.FIRST_COMPLETED)
            if fut.done():
                return fut.result()
            self._check_end()
            now = time.time()
            if now >= next_note:
                self.log(f'{tg.label}: {int(now - t0)} s without a hit; the target stays lit and the rat keeps trying')
                next_note += NO_HIT_NOTE_S
            m = await self._measure(tg)
            ok, why = self._ready(tg, m)
            if ok and self._in_view(m):
                last = tg.lights[-1]['box']
                if max(abs(m['x'] + 1 - last[0]), abs(m['y'] + 1 - last[1]),
                       abs(m['w'] - 2 - last[2]), abs(m['h'] - 2 - last[3])) > MOVE_TOL_PX:
                    box = self._light(tg, m, only_if_lit=True)
                    if box:
                        self.log(f'{tg.label} moved on the page: re-lit at {box}')
                continue
            if self._withdraw(tg):
                self.log(f'{tg.label} is {why or "out of view"}: withdrawn (the rat holds) and re-measured')
                m = await self._acquire(tg)
                self._light(tg, m)

    async def _verify(self, tg, px, py):
        r = await self._eval(VERIFY_JS, [px, py, tg.kind])
        return (bool(r and r.get('ok')), (r or {}).get('why') or 'page did not answer')

    async def _click(self, px, py):
        async with self.mouse_lock:
            await self.bot.page.mouse.click(px, py)

    # ---- the pons stream (CDP screencast) -------------------------------------------------------------------------
    async def start_screencast(self):
        self.cdp = await self.bot.ctx.new_cdp_session(self.bot.page)
        self.cdp.on('Page.screencastFrame', lambda p: asyncio.ensure_future(self._on_frame(p)))
        self.streaming = True
        await self.cdp.send('Page.startScreencast', {'format': 'jpeg', 'quality': SHOT_QUALITY, 'everyNthFrame': 1,
                                                     'maxWidth': VIEW_W, 'maxHeight': VIEW_H})

    async def _on_frame(self, p):
        """One screencast frame: forward it unless one is still waiting to be sent, and ack it (Chrome sends the next
        frame only after the ack) no sooner than 1/SHOT_MAX_FPS after the previous ack. Frames are handled one at a
        time (frame_lock), so at most one is ever in flight to the viewer."""
        async with self.frame_lock:
            await self._frame(p)

    async def _frame(self, p):
        try:
            b64 = p.get('data')
            if self.streaming and b64:
                self.last_b64 = b64
                if self.out.shots_pending < 1:
                    self.out.shot_b64(b64, VIEW_W, VIEW_H)
                    try:
                        await asyncio.wait_for(self.out.shot_idle.wait(), 1.0)
                    except asyncio.TimeoutError:
                        pass
                else:
                    self.out.n['shot_dropped'] += 1
            wait = 1.0 / SHOT_MAX_FPS - (time.perf_counter() - self.last_ack)
            if wait > 0:
                await asyncio.sleep(wait)
            self.last_ack = time.perf_counter()
            if self.cdp is not None:
                await self.cdp.send('Page.screencastFrameAck', {'sessionId': p.get('sessionId')})
        except Exception:
            pass

    async def stop_screencast(self):
        self.streaming = False
        try:
            if self.cdp is not None:
                await asyncio.wait_for(self.cdp.send('Page.stopScreencast'), 5)
        except Exception:
            pass

    async def _mouse_loop(self):
        """25 Hz: the page mouse follows the rat's cursor (pons's hover states show; the in-page cursor follows)."""
        last = None
        while self.mouse_on:
            t0 = time.perf_counter()
            c = self.cursor
            if c is not None:
                px = min(max(c[0] * VIEW_W, 0.0), VIEW_W - 1.0)
                py = min(max(c[1] * VIEW_H, 0.0), VIEW_H - 1.0)
                if last is None or abs(px - last[0]) + abs(py - last[1]) >= 0.5:
                    try:
                        async with self.mouse_lock:
                            await self.bot.page.mouse.move(px, py)
                        last = (px, py)
                    except Exception:
                        pass
            await asyncio.sleep(max(0.005, 1.0 / MOUSE_HZ - (time.perf_counter() - t0)))

    def dev_frame(self, name):
        """--dev-shots: the latest streamed pons frame (exactly what the viewer got) to live/dev_shots/brain2_*.jpg."""
        if not CONFIG.get('dev_shots') or not self.last_b64:
            return
        p = LIVE_DIR / 'dev_shots' / f"brain2_{self.stamp}{'_DEV' if self.oracle else ''}_{name}.jpg"
        try:
            p.parent.mkdir(exist_ok=True)
            p.write_bytes(base64.b64decode(self.last_b64))
            self.dev_paths.append(rel(p))
        except Exception as e:
            say(f'  dev frame {name} failed: {e!r}'[:200])

    async def _dev_frames_while(self, name, seconds, fracs=(0.3, 0.55, 0.8)):
        for i, f in enumerate(fracs):
            await asyncio.sleep(seconds * (f - (fracs[i - 1] if i else 0.0)))
            self.dev_frame(f'{name}_mid{i + 1}')

    async def _dev_frame_later(self, name, delay):
        await asyncio.sleep(delay)
        self.dev_frame(name)

    # ---- the run ----------------------------------------------------------------------------------------------------
    async def run(self):
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.out = Outbox(self.ws, self.run_dir / 'events.jsonl')
        outcome = {'mode': 'error'}
        try:
            outcome = await self._run()
        except StageFailed as e:
            traceback.print_exc()
            tg = next((t for t in self.targets if t.key == e.stage), None)
            if tg:
                self.stage(tg, 'failed', e.detail)
            outcome = {'mode': 'error', 'stage': e.stage, 'error': e.detail, **self._signed_broadcast()}
            self.log(f'run stopped at {e.stage}: {e.detail}')
        except RunEnded as e:
            if self.cur is not None and self.cur.state not in ('done', 'failed'):
                self.stage(self.cur, 'failed', str(e))
            outcome = {'mode': 'ended', 'reason': str(e), **self._signed_broadcast(),
                       'targets_done': sum(t.state == 'done' for t in self.targets)}
            self.log(f'the run ended before the launch: {e}')
        except Exception as e:
            traceback.print_exc()                   # the full traceback: the terminal (stderr) only
            if self.cur is not None and self.cur.state not in ('done', 'failed'):
                self.stage(self.cur, 'failed', err_repr(e, 300))
            outcome = {'mode': 'error', 'error': err_repr(e, 400), **self._signed_broadcast()}
            self.log(f'run failed: {err_repr(e, 400)}'[:300])
        saved = False
        try:
            saved = await self._finish(outcome)
        except Exception as e:
            traceback.print_exc()
            self.log(f'recording failed: {err_repr(e, 400)}'[:300])
        finally:
            await self._teardown()
            if ACTIVE.get('run') is self:
                ACTIVE['run'] = None
        st = self._stream_stats()
        replay = f'python replay_session.py {rel(self.run_dir)}' if saved and not self.oracle else None
        STATE['last_run'] = {'run_dir': rel(self.run_dir), 'outcome': outcome.get('mode'), 'ended': utc(),
                             'recorded': saved, 'dev_oracle': self.oracle}
        live_done = {}
        if self.live_mode:
            h, tok = outcome.get('tx') or self.live_hash, outcome.get('token')
            live_done = {'mode': 'LIVE', 'tx': h, 'token': tok, 'block': outcome.get('block'),
                         'signed': outcome.get('signed'), 'broadcast': outcome.get('broadcast'),
                         'explorer_tx': f'{launcher.EXPLORER}/tx/{h}' if h else None,
                         'explorer_token': f'{launcher.EXPLORER}/token/{tok}' if tok else None,
                         'pons_coin': PONS_COIN_URL.format(tok) if tok else None, 'live_used': STATE['live_used']}
        self.out.json({'type': 'done', 'run_dir': rel(self.run_dir), 'outcome': outcome.get('mode'), 'launch': outcome,
                       'recorded': saved, 'replay': replay, 'dev_oracle': self.oracle, 'commit': self.commit,
                       'hits': sum(1 for c in self.clicks if c['forwarded']),
                       'misses': sum(1 for c in self.clicks if not c['forwarded']),
                       'page_mousedowns': self.page_downs, 'stream': st,
                       'targets': [{'stage': t.key, 'state': t.state} for t in self.targets], **live_done})
        await self.out.close()
        return outcome

    async def _run(self):
        c = self.coin
        if self.live_mode:
            self._live_prepare()
        self.log(f"RATBRAIN brain rig · {'LIVE' if self.live_mode else 'DRY RUN'} · {c['name']} (${c['symbol']}) · "
                 f"seed {self.seed} · "
                 f"steering network {BRAIN['steer_source']} sha256 {BRAIN['steer_sha256'][:16]} · lever-press network "
                 f"{BRAIN['press_source']} sha256 {BRAIN['press_sha256'][:16]}")
        if self.live_mode:
            self.log('LIVE: pons builds the launch transaction; the rig decodes it, checks every field against .env '
                     'and the brain commit, re-simulates it and signs it ONCE (nonce 0) with the funded wallet, then '
                     'broadcasts it. The key never enters the page.')
        else:
            self.log('DRY: pons builds the launch transaction, the rig decodes it and refuses to sign. Nothing is signed '
                     'or broadcast.')
        if self.oracle:
            self.log('DEV --dev-oracle: a SCRIPTED cursor drives the targets, NOT the rat\'s brain. This is a test '
                     'of the pons automation, not a brain run, and must not be recorded or published.')
        # the brain (both networks), frozen into the run dir: the session reads these copies, never the training folder
        steer_path, press_path = self.run_dir / 'steer.pt', self.run_dir / 'press.pt'
        for p, b in ((steer_path, BRAIN['steer_bytes']), (press_path, BRAIN['press_bytes'])):
            p.write_bytes(b)
            os.chmod(p, stat.S_IREAD)
        if self.oracle:
            self.sess = Oracle(self.seed)
            self.commit = BRAIN['commit']
        else:
            self.sess = await asyncio.to_thread(brain_session.Session, str(steer_path), self.seed, PREROLL_S,
                                                (0.5, 0.5), str(press_path))
            self.commit = self.sess.commit
            if self.commit != BRAIN['commit']:
                self.log(f"note: the brain commit is {self.commit} (the code or scene changed since the rig started, "
                         f"which showed {BRAIN['commit']})")
        if self.live_mode and self.commit != CONFIG['live']['commit']:
            raise StageFailed('live', f"the brain commit {self.commit} is not the one preflighted at startup "
                                      f"({CONFIG['live']['commit']}); refusing the run: restart the rig")
        self.description = DESCRIPTION.format(commit=self.commit)
        self.out.json({'type': 'commit', 'commit': self.commit, 'description': self.description,
                       'steer_sha256': BRAIN['steer_sha256'], 'press_sha256': BRAIN['press_sha256'],
                       'steer_policy': BRAIN['steer_source'], 'press_policy': BRAIN['press_source'],
                       'policy_sha256': BRAIN['steer_sha256'], 'policy': BRAIN['steer_source'],
                       'units': UNITS, 'connections': CONNECTIONS, 'dev_oracle': self.oracle})
        self.log(f'brain commit {self.commit}: sha256 of BOTH networks (steering + lever-press) + env.py, '
                 'cursor_env.py, steer_env.py, session.py, ptload.py + the scene. It goes into the coin description; '
                 'the full session proof is published with the recording')

        # ---- pons
        if self.live_mode:
            # the funded wallet: only its address crosses into the page; ponsbot signs in Python (LiveLaunch)
            acct = CONFIG['live']['pre']['acct']
        else:
            acct = ponsbot.throwaway_account()
        self.bot = ponsbot.PonsBot(acct, self.mode, log=self.log, headful=CONFIG['headful'], size=(VIEW_W, VIEW_H),
                                   theme='dark', page_cursor=False, symbol=c['symbol'])
        self.bot.on_send = self.on_send
        await self.bot.start()
        await self.bot.page.add_init_script(OVERLAY_JS.replace('__DEV__', 'true' if self.oracle else 'false'))
        if self.live_mode:
            self.log(f'opening {ponsbot.URL} ({VIEW_W}x{VIEW_H}) with an injected wallet: the funded .env wallet '
                     f'{acct.address} (LIVE; the page gets its address only)')
        else:
            self.log(f'opening {ponsbot.URL} ({VIEW_W}x{VIEW_H}) with an injected wallet: throwaway in-memory key (DRY)')
        try:
            await self.bot.goto()
        except Exception as e:
            raise RunEnded(f'pons did not load: {e}')
        await self.start_screencast()
        try:
            st = await self.bot.settle()
        except Exception as e:
            raise RunEnded(f'the create form did not appear: {e}')
        if not st['connected']:
            raise RunEnded(f"pons did not show the injected wallet as connected ({st['page_wallet']})")
        await self._eval('() => { window.__ratDowns = []; return true; }')
        self.log(f'wallet {acct.address} connected in pons · rig setup (not the rat): pons theme {st["theme"]}, '
                 "pons's status strip dismissed")
        if self.live_mode:
            self.log(f"LIVE: pons reads this wallet's REAL balance (no override; "
                     f"{CONFIG['live']['balance'] / 1e18:.6f} ETH at startup). The rig signs one transaction, only "
                     "after the rat clicks pons's Confirm and every check passes.")
        else:
            self.log('DRY: pons reads the wallet balance through its own RPC proxy; for this throwaway address the rig '
                     'answers with a simulated 1 ETH (eth_call state override) so pons enables its launch button. '
                     'The wallet holds nothing; the rig never signs.')

        # ---- the brain (or, in DEV, the scripted cursor)
        self.sim_thread = threading.Thread(target=self._sim, name='brain-session', daemon=True)
        self.sim_thread.start()
        self.mouse_on = True
        self.mouse_task = asyncio.create_task(self._mouse_loop())
        if not self.oracle:
            self.log(f'the rat stands in the box with its brain off for {PREROLL_S:g} s; then its steering network '
                     '(794 units) turns the head, which steers the cursor, and its lever-press network (1,518 units) '
                     'performs every press, which clicks')
        await asyncio.wait({asyncio.ensure_future(self.brain_on.wait()), self.over, self.stop_fut},
                           timeout=60, return_when=asyncio.FIRST_COMPLETED)
        self._check_end()
        if not self.brain_on.is_set():
            raise RunEnded('the brain session did not start within 60 s')

        # ---- the task list: the rat clicks each one
        terms_shown = await self._wait_terms()
        for tg in self.targets:
            if tg.kind in ('terms', 'accept') and not terms_shown:
                self.stage(tg, 'done', 'pons showed no terms dialog')
                continue
            if self.live_mode and tg.kind == 'confirm':
                await self._live_arm(tg)       # re-preflight + journal reservation, before the rat may click Confirm
                self._check_end()              # a STOP / the viewer leaving / Ctrl+C while arming: end, unsigned
            await self._target(tg)

        # ---- the launch request
        t11 = self.targets[-1]
        try:
            await asyncio.wait_for(self.send_entered.wait(), SEND_WAIT_S)
        except asyncio.TimeoutError:
            raise StageFailed(t11.key, f'pons never called eth_sendTransaction within {SEND_WAIT_S} s of the click')
        if self.live_mode:
            # sign_and_send makes blocking RPCs with retries: never abandon it half way on a short timer (rig.py)
            await self.tx_seen.wait()
        else:
            try:
                await asyncio.wait_for(self.tx_seen.wait(), DRY_HANDLE_WAIT_S)
            except asyncio.TimeoutError:
                raise StageFailed(t11.key, f'the DRY wallet hook did not finish within {DRY_HANDLE_WAIT_S} s')
        if self.rejection_task:
            await self.rejection_task
        if self.live_mode:
            return await self._outcome_live()
        return self._outcome()

    async def _wait_terms(self):
        t_end = time.time() + TERMS_WAIT_S
        while time.time() < t_end:
            self._check_end()
            if await self.bot.terms_shown():
                self.log("pons's Terms of Use, Privacy Policy and jurisdiction attestation: accepted by the owner, who "
                         'authorized the rat to click them')
                await asyncio.sleep(0.6)
                return True
            await asyncio.sleep(0.3)
        self.log(f'pons showed no terms dialog within {TERMS_WAIT_S:g} s')
        return False

    async def _target(self, tg):
        self.cur = tg
        self.stage(tg, 'active', tg.label)
        while True:
            self._check_end()
            m = await self._acquire(tg)
            if tg.kind == 'terms' and m.get('checked'):
                self.stage(tg, 'done', 'already ticked')
                return
            if tg.kind == 'advanced' and m.get('expanded'):
                self.stage(tg, 'done', 'already open')
                return
            if tg.kind == 'confirm':
                self.log(f"pons's review dialog \"{m.get('title')}\": " + ' · '.join((m.get('dialog') or [])[:24]))
            box = self._light(tg, m)
            self.stage(tg, 'active', f'lit {box[2]:.0f}x{box[3]:.0f} px'
                       + (f" · the {m.get('via')}" if tg.kind == 'terms' else '')
                       + (' · stops before the link' if m.get('cut_at_link') else ''))
            if CONFIG.get('dev_shots'):
                asyncio.ensure_future(self._dev_frame_later(f'{tg.key}_lit', 0.6))
            t_lit = time.time()
            step, nx, ny = await self._await_hit(tg)
            px, py = nx * VIEW_W, ny * VIEW_H
            self.dev_frame(f'{tg.key}_hit')
            await self._cue(None)
            ok, why = await self._verify(tg, px, py)
            if ok:
                self._report_click(step, nx, ny, True, True, tg.key)
                secs = time.time() - (tg.lights[0]['at'] if tg.lights else t_lit)
                tg.hit = {'step': step, 'x': round(px, 1), 'y': round(py, 1), 'at': time.time(),
                          'seconds_since_first_lit': round(secs, 2), 'misses_before': len(tg.misses),
                          'box': tg.lights[-1]['box'] if tg.lights else None}
                self.stage(tg, 'hit', f'at ({px:.0f}, {py:.0f}), brain step {step} · {secs:.1f} s, '
                                      f'{len(tg.misses)} miss{"" if len(tg.misses) == 1 else "es"}')
                done, detail = await self._consequence(tg, px, py)
                if done:
                    self.stage(tg, 'done', detail)
                    self.dev_frame(f'{tg.key}_done')
                    self.cur = None
                    return
                why = detail
            else:
                why = f'the page point failed the check ({why})'
                self._report_click(step, nx, ny, True, False, self.cur.key if self.cur else tg.key, why)
            tg.relights += 1
            self.log(f'{tg.label}: {why}; lighting it again')
            if tg.relights > MAX_RELIGHTS:
                raise StageFailed(tg.key, f'{tg.label}: {why} ({tg.relights} tries)')
            self.stage(tg, 'active', f'again: {why}')

    async def _consequence(self, tg, px, py):
        """-> (done, detail). The rat's hit has been verified on the target; forward it and run what follows."""
        page = self.bot.page
        k = tg.kind
        if k == 'terms':
            await self._click(px, py)
            await asyncio.sleep(0.35)
            m = await self._measure(tg)
            if m and m.get('checked'):
                return True, 'ticked'
            return False, 'the box did not tick'
        if k == 'accept':
            await self._click(px, py)
            for _ in range(48):
                await asyncio.sleep(0.25)
                if not await self.bot.terms_shown():
                    return True, 'terms accepted, dialog closed'
            return False, 'the terms dialog did not close'
        if k == 'image':
            if not COIN_PNG.exists() or COIN_PNG.stat().st_size > COIN_MAX_BYTES:
                raise StageFailed(tg.key, f'{rel(COIN_PNG)} is missing or over pons\'s ~2 MB limit')
            try:
                r = await self.bot.pick_image_at(px, py, COIN_PNG, click=self._click)
            except Exception as e:
                return False, f'the image did not go through: {str(e)[:160]}'
            ip = r.get('ipfs') or {}
            self.image_uri = r.get('uri')
            tg.result = {'how': r.get('how'), 'ipfs_status': ip.get('status'), 'uri': self.image_uri}
            self.log(f"the rat's click opened pons's image picker ({r.get('how')}); the rig picked coin.png; pons "
                     f"pinned it (HTTP {ip.get('status')}): {self.image_uri or str(ip.get('body') or ip.get('error'))[:120]}")
            if not self.image_uri:
                self.log("pons did not return an ipfs:// URI for the image: the tx 'image' check will fail")
            return True, 'coin.png · Image ready'
        if k in ('name', 'ticker', 'description', 'tax'):
            text = {'name': self.coin['name'], 'ticker': self.coin['symbol'], 'description': self.description,
                    'tax': f"{self.coin['tax_bps'] / 100:g}"}[k]
            loc = page.locator(ponsbot.TAX_SEL if k == 'tax' else ponsbot.SEL[k]).first
            await self._click(px, py)
            await asyncio.sleep(0.15)
            if not await self._eval(FOCUS_JS):
                return False, 'the click did not put the caret in the field'
            await page.keyboard.press('Control+A')
            await page.keyboard.press('Delete')
            secs = len(text) * TYPE_DELAY_MS / 1000.0
            shots0 = self.out.n['shot']
            dev = asyncio.ensure_future(self._dev_frames_while(k, secs)) if CONFIG.get('dev_shots') and len(text) > 3 else None
            t0 = time.time()
            await page.keyboard.type(text, delay=TYPE_DELAY_MS)
            dt = time.time() - t0
            if dev:
                await dev
            await asyncio.sleep(0.35)
            val = await loc.input_value()
            fixed = False
            if val != text:        # React can drop a keystroke under load: set it once more, still through the input
                self.log(f'{tg.label} read {val!r} after typing; setting it once more through the input')
                await loc.fill(text, timeout=5000)
                await asyncio.sleep(0.35)
                val = await loc.input_value()
                fixed = True
            n_sh = self.out.n['shot'] - shots0
            self.typing[k] = {'chars': len(text), 'seconds': round(dt, 2), 'ms_per_char': round(dt * 1000 / max(1, len(text)), 1),
                              'frames_streamed': n_sh, 'fps': round(n_sh / dt, 1) if dt > 0 else None,
                              'refilled': fixed, 'ok': val == text}
            tg.result = {'typed': text, 'reads': val, **self.typing[k]}
            self.dev_frame(f'{k}_typed')
            if val != text:
                return False, f'the field reads {val!r}'
            if k == 'tax':
                self.tax_ok = True
            self.log(f"typed {tg.label.lower()} live: {len(text)} chars in {dt:.1f} s ({self.typing[k]['ms_per_char']} ms/char), "
                     f"{n_sh} pons frames streamed meanwhile")
            return True, (f'{text[:40]}…' if len(text) > 42 else text)
        if k == 'advanced':
            await self._click(px, py)
            for _ in range(20):
                await asyncio.sleep(0.15)
                m = await self._measure(tg)
                if m and m.get('expanded'):
                    await asyncio.sleep(0.5)
                    return True, 'open'
            return False, 'Advanced did not open'
        if k == 'launch':
            await self._click(px, py)
            cb = await self.bot.confirm_box(wait_s=10)
            if not cb:
                return False, f'pons did not open a review dialog saying "Launch {self.coin["symbol"]}"'
            return True, f"pons's review \"{cb['title']}\" is open"
        if k == 'confirm':
            if self.live_mode:
                self._check_end()              # LIVE: a stop that came in since the hit wins; the click is not forwarded
            self.bot.arm()
            await self._click(px, py)
            self.log(f"the rat clicked pons's Confirm at ({px:.0f}, {py:.0f}), which makes pons request the launch "
                     'transaction')
            asyncio.ensure_future(self._after_confirm_frames())
            try:
                await asyncio.wait_for(self.send_entered.wait(), SEND_WAIT_S)
            except asyncio.TimeoutError:
                return False, 'pons did not call eth_sendTransaction'
            return True, 'pons requested the launch transaction'
        raise StageFailed(tg.key, f'unknown target kind {k}')

    async def _after_confirm_frames(self):
        await asyncio.sleep(0.5)
        self.dev_frame('confirm_after')

    # ---- the wallet: pons's eth_sendTransaction ------------------------------------------------------------------------
    async def on_send(self, tx):
        armed = bool(self.bot.armed)
        self.sends.append({'tx': tx, 'at': time.time(), 'armed': armed})
        n = len(self.sends)
        data = str(tx.get('data') or tx.get('input') or '')
        self.log(f"pons called eth_sendTransaction #{n}: to {tx.get('to')} value {tx.get('value')} calldata "
                 f"{max(0, len(data) - 2) // 2} bytes")
        if not armed or self.handled_send is not None:
            why = "the rat has not clicked Confirm" if not armed else 'one launch request per run'
            self.log(f'refused ({why})')
            return ponsbot.refusal(f'rat rig refused: {why}') if self.live_mode else ponsbot.refusal()
        self.handled_send = n - 1
        self.send_entered.set()
        try:
            return await self._handle_send(tx)
        finally:
            self.tx_seen.set()

    async def _handle_send(self, tx):
        fields, data_hex = ponsbot.decode_launch(tx)
        c = self.coin
        expect = {'name': c['name'], 'symbol': c['symbol'], 'proof': self.commit, 'description': self.description,
                  'creator': self.bot.address, 'tax_bps': c['tax_bps'], 'tax_set': self.tax_ok,
                  'image': self.image_uri, 'x': c['x'], 'website': c['website']}
        checks = ponsbot.check_launch(fields, expect)
        ok = ponsbot.all_ok(checks) and data_hex is not None
        failed = [k for k, v in checks.items() if not v['ok']] + ([] if data_hex else ['decode'])
        if fields.get('decode_error'):
            self.log(f"decode error: {fields['decode_error']}")
        self.log(f"decoded: {fields.get('name')} / {fields.get('symbol')} · creator {fields.get('creator')} · pair "
                 f"{fields.get('pairToken')} · {fields.get('value')} · image {fields.get('image')}")
        self.log(f"description: {fields.get('description')}")
        self.log(f"links {json.dumps(fields.get('socials'))} · developer buy (amountIn) {fields.get('amountIn')} · "
                 f"creator tax {fields.get('creatorTaxBps')} bps · ABI encoding {fields.get('abi_encoding')}")
        base = {'raw_request': tx, 'fields': fields, 'checks': checks, 'expected': expect, 'mode': self.mode,
                'wallet': self.bot.address, 'at': utc(), 'at_unix': time.time(), 'dev_oracle': self.oracle,
                'decoded_calldata_sha256': sha(bytes.fromhex(data_hex[2:])) if data_hex else None}
        if self.live_mode:
            return await self._handle_send_live(tx, fields, checks, ok, failed, data_hex, base)
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
                       'simulation': sim, 'failed': failed})
        if sim.get('ok'):
            self.log(f"eth_call of pons's exact tx (simulated 1 ETH balance): would create token "
                     f"{sim.get('predicted_token')}, curve {sim.get('predicted_curve')}, gas {sim.get('gas_estimate')}")
        else:
            self.log(f"eth_call of pons's exact tx failed: {sim.get('error')}")
        self.log(f'{len(checks) - len(failed)}/{len(checks)} checks passed · {verdict}' + (f' · FAILED {failed}' if failed else ''))
        self.log(f'refused with {ponsbot.USER_REJECTED} "{ponsbot.DRY_REFUSAL}". Nothing signed, nothing broadcast.')
        self.rejection_task = asyncio.ensure_future(self._watch_rejection())
        return resp

    async def _watch_rejection(self):
        hit = await self.bot.wait_rejection(8.0)
        self.rejection = hit
        await asyncio.sleep(0.4)
        try:
            self.final_jpg = await self.bot.jpeg(88)
        except Exception:
            pass
        if hit:
            self.log(f'pons shows: {hit}')
            self.dev_frame('rejected')
        else:
            self.log('pons showed no rejection message within 8 s')

    def _outcome(self):
        cap = self.capture or {}
        sim = cap.get('simulation') or {}
        failed = [k for k, v in (cap.get('checks') or {}).items() if not v.get('ok')]
        out = {'mode': cap.get('verdict'), 'wallet': 'throwaway in-memory key (DRY)', 'creator': self.bot.address,
               'description': (cap.get('fields') or {}).get('description'), 'brain_commit': self.commit,
               'checks_passed': not failed, 'failed_checks': failed,
               'predicted_token': sim.get('predicted_token'), 'predicted_curve': sim.get('predicted_curve'),
               'simulation_ok': sim.get('ok'), 'refused_with': cap.get('response_to_page'),
               'page_showed': self.rejection, 'signed': False, 'broadcast': False, 'captured_tx': 'captured_tx.json'}
        self.log('DRY RUN: pons built the launch tx, the rig refused to sign. No coin was created.' if not failed else
                 f'DRY RUN: the captured tx failed {failed}; refused. No coin was created.')
        return out

    # ---- LIVE (every method here is a no-op or unreachable in DRY) ---------------------------------------------------------
    def _live_prepare(self):
        """LIVE, first thing in a run: launcher.config() re-read (the .env file) must equal startup, no journal."""
        try:
            self.cfg = live_config()
        except launcher.LaunchRefused as e:
            raise StageFailed('live', f'launcher.config() refused: {e}')
        drift = config_drift(CONFIG['pinned'], self.cfg)
        if drift:
            raise StageFailed('live', f'launcher.config() changed since the rig started ({", ".join(drift)}); '
                                      'refusing the run: restart the rig')
        why = journal_refusal()
        if why:
            raise StageFailed('live', why)

    async def _live_arm(self, tg):
        """LIVE, after pons's review "Launch <SYMBOL>" is open (pons enabled "Launch token" on the wallet's REAL balance)
        and before the rat's Confirm target is lit (that click makes pons request the transaction): .env unchanged, the
        page took the tax, pons pinned the image, launcher.preflight() again with fresh gas figures, then
        launcher.reserve_journal() (exclusive create: a second launch is impossible), then the one ponsbot.LiveLaunch.
        As rig.py's _live_arm (there too the journal is reserved before the review's Confirm can be clicked). Any
        failure -> StageFailed, nothing signed; the rat holds meanwhile."""
        self.log("LIVE: arming before the rat may click pons's Confirm (config, tax, pinned image, preflight again, "
                 'journal reservation); the rat holds')
        try:
            now = live_config()
        except launcher.LaunchRefused as e:
            raise StageFailed(tg.key, f'launcher.config() refused: {e}')
        drift = config_drift(CONFIG['pinned'], now)
        now.clear()
        if drift:
            raise StageFailed(tg.key, f'launcher.config() changed since the rig started ({", ".join(drift)})')
        if not self.tax_ok:
            raise StageFailed(tg.key, 'the creator tax on the page is not launcher.config tax_bps')
        if not self.image_uri:
            raise StageFailed(tg.key, 'pons returned no ipfs:// URI for the image; LIVE cannot check the calldata')
        try:
            pre = await asyncio.to_thread(launcher.preflight, preflight_cfg(self.cfg), self.commit)
        except launcher.LaunchRefused as e:
            raise StageFailed(tg.key, f'preflight refused: {e}')
        if pre['creator'].lower() != self.bot.address.lower():
            raise StageFailed(tg.key, 'the preflight wallet is not the wallet in the page')
        try:
            launcher.reserve_journal({'creator': pre['creator'], 'brain_commit': self.commit,
                                      'expected_proof': self.commit, 'description': self.description,
                                      'steer_sha256': BRAIN['steer_sha256'], 'press_sha256': BRAIN['press_sha256'],
                                      'seed': self.seed, 'out': rel(self.run_dir), 'via': LIVE_VIA})
        except FileExistsError:
            raise StageFailed(tg.key, f'{launcher.JOURNAL} exists: a launch was already attempted')
        STATE['live_used'] = True
        try:
            self.live = ponsbot.LiveLaunch(self.cfg, pre, self.commit, log=self.log_ts, description=self.description,
                                           via=LIVE_VIA,
                                           on_signed=lambda h: self.loop.call_soon_threadsafe(self._tx_signed, h))
        except Exception as e:
            launcher.write_journal({'state': 'aborted_before_sign', 'error': f'LiveLaunch refused: {err_text(e)}',
                                    'proof': self.commit, 'via': LIVE_VIA})
            raise StageFailed(tg.key, f'LiveLaunch refused: {err_text(e)}')
        self.armed_at = time.time()
        self.log(f"LIVE armed: preflight passed (gas {pre['gas']}, maxFee {pre['max_fee'] / 1e9:.3f} gwei, balance "
                 f"{pre['balance'] / 1e18:.6f} ETH), launch_journal.json reserved; the rat's Confirm leads to ONE "
                 f'transaction at nonce 0 (within {LIVE_CONFIRM_MAX_S} s, else the run ends unsigned; STOP still '
                 'aborts until the tx is checked)')

    async def _handle_send_live(self, tx, fields, checks, ok, failed, data_hex, base):
        """LIVE: every DRY check must pass, then ponsbot.LiveLaunch.sign_and_send (re-simulate, sign exactly these
        calldata bytes at nonce 0, journal, broadcast) and pons gets the real hash. Anything else -> 4001, unsigned."""
        n = len(checks)
        stopped = self.stop_fut.done()
        if not ok or self.live is None or stopped:
            reason = (f'calldata failed checks {failed}' if not ok else 'LIVE was not armed' if self.live is None
                      else f'the session was stopped before signing ({self.stop_fut.result()})')
            self._live_abort_unsigned(reason, fields=fields)   # the journal was reserved: close it, nothing signed
            verdict = 'live_refused_mismatch' if not ok else 'live_refused_stopped' if stopped else 'live_refused'
            resp = ponsbot.refusal(f'rat rig refused: {reason}'[:200])
            self.capture = {**base, 'verdict': verdict, 'error': reason, 'response_to_page': resp,
                            **self._signed_broadcast()}
            self.out.json({'type': 'tx', 'mode': 'LIVE', 'phase': 'refused', 'verdict': verdict,
                           'fields': fields, 'checks': checks, 'failed': failed, 'error': reason})
            self.log(f'{n - len(failed)}/{n} checks passed · {verdict}' + (f' · FAILED {failed}' if failed else ''))
            self.log(f'LIVE: refused with {ponsbot.USER_REJECTED} ({reason}). Nothing signed, nothing broadcast.')
            self.rejection_task = asyncio.ensure_future(self._watch_rejection())
            return resp
        # from here on the tx belongs to the signer: a STOP / the viewer leaving / Ctrl+C no longer aborts it (set with
        # no await since the stop check above, so a stop can never slip in between)
        self.sign_started = True
        self.out.json({'type': 'tx', 'mode': 'LIVE', 'phase': 'checked', 'verdict': 'live_checked', 'fields': fields,
                       'checks': checks, 'failed': []})
        self.log(f'{n}/{n} checks passed · LIVE: re-simulating with the real balance, then signing ONCE at nonce 0')
        try:
            h, send_error = await self.loop.run_in_executor(
                None, self.live.sign_and_send, tx, fields, checks, data_hex)
        except Exception as e:
            err = (str(e) if isinstance(e, launcher.LaunchRefused) else err_text(e))[:300]
            resp = ponsbot.refusal(f'rat rig refused: {err}'[:200])
            self.capture = {**base, 'verdict': 'live_refused', 'error': err, 'response_to_page': resp,
                            **self._signed_broadcast()}
            self.out.json({'type': 'tx', 'mode': 'LIVE', 'phase': 'refused', 'verdict': 'live_refused',
                           'fields': fields, 'checks': checks, 'failed': [], 'error': err[:200]})
            self.log(f'LIVE: refused before signing ({err}). Nothing signed, nothing broadcast.')
            self.rejection_task = asyncio.ensure_future(self._watch_rejection())
            return resp
        self.live_hash = h
        sb = self._signed_broadcast()
        verdict = 'live_sent' if sb['broadcast'] is True else (
            'live_send_unknown' if sb['broadcast'] == 'unknown' else 'live_send_rejected')
        self.capture = {**base, 'verdict': verdict, 'hash': h, **sb}
        self.out.json({'type': 'tx', 'mode': 'LIVE', 'phase': 'sent', 'verdict': verdict, 'hash': h,
                       'broadcast': sb['broadcast'], 'send_error': send_error,
                       'explorer_tx': f'{launcher.EXPLORER}/tx/{h}'})
        if send_error:
            self.log(f'LIVE send error: {send_error}')
        if sb['broadcast'] is False:
            self.log(f'LIVE: every RPC REJECTED the signed tx {h}: it was not accepted, pons gets an error (not a hash)')
        else:
            self.log(f"LIVE: broadcast {sb['broadcast']} · {h} · {launcher.EXPLORER}/tx/{h}")
        if sb['broadcast'] is False:        # every RPC rejected the raw tx: do not hand pons a hash as if sent
            resp = ponsbot.err(-32000, f'rat rig: the launch tx was rejected by every RPC: {send_error}'[:200])
            self.capture['response_to_page'] = resp
            return resp
        self.capture['response_to_page'] = {'result': h}
        self.success_task = asyncio.ensure_future(self._watch_success())
        return {'result': h}

    def _tx_signed(self, h):
        """Loop thread, from LiveLaunch.on_signed: the raw signed tx is in the journal and not broadcast yet."""
        self.log(f'LIVE: signed ONCE at nonce 0 · {h} · the raw signed bytes are in launch_journal.json; broadcasting')
        self.out.json({'type': 'tx', 'mode': 'LIVE', 'phase': 'signed', 'verdict': 'live_signed', 'hash': h,
                       'nonce': 0, 'explorer_tx': f'{launcher.EXPLORER}/tx/{h}'})

    def success_re(self):
        """SUCCESS_RE plus wording naming our coin ("Launched $RATTEST", "$RATTEST is live"). The symbol is A-Z0-9."""
        s = re.escape(self.coin['symbol'])
        return f'{SUCCESS_RE[:-1]}|launched \\$?{s}\\b|\\${s} (is )?(live|launched))'

    async def _watch_success(self):
        """After pons got the hash: what pons shows (for the record; the receipt decides the outcome). Stops at
        self.success_deadline: SUCCESS_WAIT_S after the hash, cut to SUCCESS_AFTER_RECEIPT_S once the receipt is in
        (pons stays on its create form after a launch: flybrain waited <= 9 s, then opened the coin page itself)."""
        js = """(rx) => { if (/\\/launchpad\\/0x[0-9a-fA-F]{40}/.test(location.pathname)) return 'pons opened ' + location.pathname;
                 const L = document.body.innerText.split(String.fromCharCode(10))
                   .map(s => s.trim()).filter(Boolean);
                 const i = L.findIndex(l => new RegExp(rx, 'i').test(l));
                 return i < 0 ? null : L.slice(Math.max(0, i - 1), i + 2).join(' / '); }"""
        t0 = time.time()
        if self.success_deadline is None:
            self.success_deadline = t0 + SUCCESS_WAIT_S
        rx = self.success_re()
        hit = None
        while time.time() < self.success_deadline:
            hit = await self._eval(js, rx)
            if hit:
                break
            await asyncio.sleep(0.3)
        self.success = hit
        if hit:
            self.log(f'pons shows: {hit}')
            self.dev_frame('success')
        elif self.success_note:
            self.log(f'no pons success text matched within {time.time() - t0:.0f} s; {self.success_note}')

    success_note = 'the receipt decides'

    def _live_summary(self, cap):
        failed = [k for k, v in (cap.get('checks') or {}).items() if not v.get('ok')]
        return {'wallet': 'funded .env wallet (LIVE)', 'creator': self.bot.address if self.bot else None,
                'description': (cap.get('fields') or {}).get('description'), 'brain_commit': self.commit,
                'checks_passed': bool(cap.get('checks')) and not failed, 'failed_checks': failed,
                'response_to_page': cap.get('response_to_page'), 'page_showed': self.rejection or self.success,
                'captured_tx': 'captured_tx.json'}

    async def _outcome_live(self):
        cap = self.capture or {}
        if self.live is None or not self.live.sent:
            self.log(f"LIVE: nothing was signed or broadcast ({cap.get('verdict')}). No coin was created.")
            return {'mode': cap.get('verdict') or 'live_not_sent', 'error': cap.get('error'),
                    **self._live_summary(cap), **self._signed_broadcast()}
        rejected = self.live.sent.get('broadcast') is False
        if rejected:
            # every RPC rejected the signed tx: look for it briefly (a timed-out RPC may still have taken it), no 120 s
            self.log(f'LIVE: checking for {self.live_hash} on chain for {REJECTED_POLL_S} s (every RPC rejected it)')
            out = await self.loop.run_in_executor(None, self.live.finish, REJECTED_POLL_S)
        else:
            self.log(f'LIVE: waiting for the receipt of {self.live_hash}')
            out = await self.loop.run_in_executor(None, self.live.finish)
        if rejected and out.get('mode') == 'sent_unconfirmed':
            out = {**out, 'mode': 'send_rejected'}
        self.live_result = out
        await self._emit_receipt(out)
        if self.success_task and not self.success_task.done():
            mined = out.get('mode') in ('live', 'live_unverified')
            # the receipt is in: pons gets a few more seconds to show its own success, then the rig moves on; for a
            # revert / no receipt there is nothing to wait for
            self.success_note = 'the receipt says mined' if mined else ''
            self.success_deadline = min(self.success_deadline or time.time(),
                                        time.time() + (SUCCESS_AFTER_RECEIPT_S if mined else 0))
            try:
                await asyncio.wait_for(asyncio.shield(self.success_task), SUCCESS_AFTER_RECEIPT_S + 3)
            except Exception:
                self.success_task.cancel()
        if out.get('token'):
            await self._show_coin(out['token'])
        return {**self._live_summary(cap), **out, **self._signed_broadcast()}

    def _receipt_details(self, h, out):
        """Blocking: the receipt's gas figures + links, for live_receipt.json. Read-only RPC."""
        tok = out.get('token')
        d = {'tx': h, 'mode': out.get('mode'), 'status': out.get('status'), 'block': out.get('block'), 'token': tok,
             'curve': out.get('curve'), 'creator': out.get('creator'), 'nonce': 0,
             'value_wei': str(launcher.LAUNCH_FEE_WEI), 'send_error': out.get('send_error'),
             'explorer_tx': f'{launcher.EXPLORER}/tx/{h}',
             'explorer_token': f'{launcher.EXPLORER}/token/{tok}' if tok else None,
             'pons_coin': PONS_COIN_URL.format(tok) if tok else None}
        try:
            rc, err = launcher.rpc('eth_getTransactionReceipt', [h])
        except Exception as e:
            rc, err = None, repr(e)
        if rc:
            gu = int(rc.get('gasUsed') or '0x0', 16)
            gp = int(rc.get('effectiveGasPrice') or '0x0', 16)
            d.update(gas_used=gu, effective_gas_price_wei=gp, gas_fee_eth=gu * gp / 1e18,
                     total_cost_eth=(gu * gp + launcher.LAUNCH_FEE_WEI) / 1e18, receipt=rc)
        else:
            d['receipt_error'] = str(err or 'no receipt')[:200]
        return d

    async def _emit_receipt(self, out):
        """The receipt (or its absence) to the viewer, the log and self.live_receipt."""
        h = out.get('tx') or self.live_hash
        try:
            self.live_receipt = await self.loop.run_in_executor(None, self._receipt_details, h, out)
        except Exception as e:
            self.live_receipt = {'tx': h, 'mode': out.get('mode'), 'receipt_error': repr(e)[:200]}
        r = self.live_receipt
        mode = out.get('mode')
        phase = {'live': 'mined', 'live_unverified': 'mined', 'reverted': 'reverted'}.get(mode, 'unconfirmed')
        verdict = {'live': 'live_mined', 'live_unverified': 'live_mined_unverified', 'reverted': 'live_reverted',
                   'sent_unconfirmed': 'live_unconfirmed'}.get(mode, f'live_{mode}')
        self.out.json({'type': 'tx', 'mode': 'LIVE', 'phase': phase, 'verdict': verdict, 'hash': h,
                       'block': out.get('block'), 'status': out.get('status'), 'token': out.get('token'),
                       'curve': out.get('curve'), 'gas_used': r.get('gas_used'), 'gas_fee_eth': r.get('gas_fee_eth'),
                       'explorer_tx': r.get('explorer_tx'), 'explorer_token': r.get('explorer_token'),
                       'pons_coin': r.get('pons_coin')})
        c = self.coin
        if mode == 'live':
            self.log(f"LIVE: mined in block {out.get('block')} · the rat launched {c['name']} (${c['symbol']}): token "
                     f"{out.get('token')} · {r.get('explorer_token')} · {r.get('pons_coin')}")
        elif mode == 'live_unverified':
            self.log(f"LIVE: mined in block {out.get('block')} (status 1) but no TokenLaunched event naming our wallet "
                     f"was found; check {r.get('explorer_tx')}")
        elif mode == 'reverted':
            self.log(f"LIVE: the transaction REVERTED in block {out.get('block')}: no coin was created · "
                     f"{r.get('explorer_tx')}")
        else:
            self.log(f'LIVE: no receipt yet for {h}; `python launcher.py --resolve` finishes it later (it only ever '
                     're-broadcasts the same signed bytes)')

    async def _show_coin(self, token):
        """LIVE epilogue, by the rig (not the rat), the way flybrain's rhlive.py ends: open the new coin's pons page in
        the streamed tab, so the recording ends on the coin. Nothing here signs or sends anything."""
        if not self.bot or not self.bot.page:
            return
        url = PONS_COIN_URL.format(token)
        self.mouse_on = False                         # the rig's page mouse stops following the rat's cursor
        if self.mouse_task:
            try:
                await asyncio.wait_for(self.mouse_task, 3)
            except Exception:
                pass
        with self.cmd_lock:
            self.lit = None
        await self._cue(None)
        # the create page's own mousedown log (targets.json checks it against the forwarded hits): read it before the
        # navigation replaces the document
        self.page_downs = await self._eval('() => window.__ratDowns || null')
        self.log(f'the rig (not the rat) opens the new coin on pons: {url}')
        try:
            await asyncio.wait_for(self._coin_page(url), COIN_PAGE_S + 60)
            self.coin_page = url
        except Exception as e:
            self.log(f'the coin page did not open: {str(e)[:160]}')

    async def _coin_page(self, url):
        page = self.bot.page
        await page.goto(url, wait_until='domcontentloaded', timeout=45000)
        # the epilogue is the rig's, not the rat's: the in-page "rat" cursor and cue are hidden so a rig mouse move
        # (e.g. accepting a terms gate) is never shown as the rat
        try:
            await page.add_style_tag(content='#__ratcur,#__ratcue{display:none!important}')
        except Exception:
            pass
        await asyncio.sleep(3.0)
        await self.bot.dismiss_notice()
        if await self.bot.terms_shown():
            self.log("pons shows its terms gate again on the coin page; the rig accepts it (owner-authorized, not the "
                     'rat)')
            try:
                await self.bot.accept_terms()
            except Exception as e:
                self.log(f'the terms gate on the coin page: {str(e)[:120]}')
        await asyncio.sleep(1.5)
        self.dev_frame('coin_page')
        try:
            self.final_jpg = await self.bot.jpeg(88)
        except Exception:
            pass
        height = await page.evaluate('document.body.scrollHeight')
        for frac in (0.4, 0.0):
            await self.bot.smooth_scroll(max(0.0, (height - VIEW_H) * frac), ms=1500, steps=30)
            await asyncio.sleep(COIN_PAGE_S / 2)

    def _signed_broadcast(self):
        """{'signed', 'broadcast'} from what actually happened, never assumed (rig.py's): LiveLaunch.sent, else a raw
        signed tx in the journal. broadcast is True / False / 'unknown'. DRY: always unsigned."""
        if not self.live_mode or self.live is None:
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
        """LIVE: close the reserved journal as aborted_before_sign (and make any later sign attempt impossible)."""
        if not self.live_mode or self.live is None:
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
        """LIVE, before recording (rig.py's): let an in-flight wallet hook finish, close an unsigned launch in the
        journal, poll the receipt of a sent one if that has not happened yet, derive signed/broadcast. Mutates
        outcome in place. No-op in DRY."""
        if not self.live_mode or self.live is None:
            return
        if self.send_entered.is_set() and not self.tx_seen.is_set():
            self.log('LIVE: waiting for the wallet hook to finish before recording')
            await self.tx_seen.wait()
        if not self.live.sent:
            why = outcome.get('error') or outcome.get('reason') or outcome.get('mode') or 'the run ended'
            if not self._live_abort_unsigned(f'run ended without signing: {why}', stage=outcome.get('stage')) \
                    and self.send_entered.is_set():
                await self.tx_seen.wait()
        if self.live.sent and self.live_result is None:
            self.log(f"LIVE: a signed tx exists ({self.live.sent['tx']}); polling its receipt before recording")
            try:
                res = await self.loop.run_in_executor(None, self.live.finish)
            except Exception as e:
                res = {'mode': 'sent_unconfirmed', 'tx': self.live.sent['tx'], 'finish_error': repr(e)[:300]}
            self.live_result = res
            await self._emit_receipt(res)
            prev = dict(outcome)
            outcome.clear()
            outcome.update(res)
            if prev.get('mode') in ('error', 'ended'):
                outcome['rig_error'] = {k: prev.get(k) for k in ('mode', 'stage', 'error', 'reason')}
        outcome.update(self._signed_broadcast())

    def _record_live(self, outcome):
        """LIVE: live_receipt.json (whatever happened, stated plainly) + a copy of launch_journal.json."""
        rec = dict(self.live_receipt) if self.live_receipt else {
            'tx': self.live_hash, 'mode': outcome.get('mode'),
            'note': 'no receipt was read' if self.live_hash else 'nothing was signed'}
        rec.update({'signed': outcome.get('signed'), 'broadcast': outcome.get('broadcast'),
                    'wallet': self.bot.address if self.bot else None, 'brain_commit': self.commit,
                    'description': self.description, 'coin': self.coin, 'recorded_utc': utc(),
                    'journal': 'launch_journal.json (copy; the original stays in the ratbrain folder)'})
        with open(self.run_dir / 'live_receipt.json', 'w', encoding='utf-8', newline='\n') as f:
            json.dump(rec, f, indent=2, default=str)
        if os.path.exists(launcher.JOURNAL):
            shutil.copyfile(launcher.JOURNAL, self.run_dir / 'launch_journal.json')

    # ---- recording ------------------------------------------------------------------------------------------------------
    def _stream_stats(self):
        ts = self.out.shot_times if self.out else []
        span = (ts[-1] - ts[0]) if len(ts) > 1 else 0.0
        return {'shots': len(ts), 'fps': round((len(ts) - 1) / span, 2) if span > 0 else None,
                'shots_dropped': self.out.n['shot_dropped'] if self.out else 0,
                'brain_frames': self.out.n['frame'] if self.out else 0, 'typing': self.typing}

    async def _finish(self, outcome):
        """Stop the brain, then record: session.save (brain runs) or oracle.json (DEV), captured_tx.json,
        targets.json, pons_final.jpg. events.jsonl is written as the run goes. LIVE: _live_finalize first (a sent tx
        is always polled and recorded, even when the run failed after sending), then live_receipt.json and a copy of
        launch_journal.json."""
        await self._live_finalize(outcome)
        with self.cmd_lock:
            self.lit = None
        await self._cue(None)
        if self.bot and self.bot.page and self.coin_page is None and self.page_downs is None:
            self.page_downs = await self._eval('() => window.__ratDowns || null')
        if self.sess is not None:
            self.sess.stop()
            if self.sim_thread is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(self.over), 15)
                except asyncio.TimeoutError:
                    self.log('the brain session did not stop within 15 s')
        self.mouse_on = False
        if self.mouse_task:
            try:
                await asyncio.wait_for(self.mouse_task, 3)
            except Exception:
                pass
        if self.bot and self.bot.page and self.final_jpg is None:
            try:
                self.final_jpg = await self.bot.jpeg(88)
            except Exception:
                pass
        await asyncio.sleep(1.2)                      # let the final state stream for a moment
        if self.final_jpg:
            (self.run_dir / 'pons_final.jpg').write_bytes(self.final_jpg)
        cap = dict(self.capture) if self.capture else {'captured': False}
        cap['other_send_requests'] = [s for i, s in enumerate(self.sends) if i != self.handled_send]
        with open(self.run_dir / 'captured_tx.json', 'w', encoding='utf-8', newline='\n') as f:
            json.dump(cap, f, indent=2, default=str)
        if self.live_mode:
            try:
                self._record_live(outcome)
            except Exception as e:
                self.log(f'LIVE: could not write live_receipt.json: {e!r}'[:300])
        # each light's command -> the brain step it was applied at (commands are applied in queue order)
        applied = list(getattr(self.sess, 'log', []) or [])
        for tg in self.targets:
            for li in tg.lights:
                i = li.get('cmd')
                li['step'] = applied[i][0] if i is not None and i < len(applied) else None
        downs = self.page_downs or []
        fwd = [c for c in self.clicks if c['forwarded']]
        miss = [c for c in self.clicks if not c['forwarded']]
        stray = [d for d in downs if not any(abs(d[0] - c['x']) <= 1.5 and abs(d[1] - c['y']) <= 1.5 for c in fwd)]
        tj = {'dev_oracle': self.oracle, 'viewport': [VIEW_W, VIEW_H], 'coords': 'page CSS px (norm = /1280, /900)',
              'targets': [t.record() for t in self.targets], 'clicks': self.clicks,
              'hits_forwarded': len(fwd), 'misses_masked': len(miss), 'page_mousedowns': downs,
              'page_mousedowns_not_from_a_hit': stray, 'commands': self.cmd_trace, 'typing': self.typing}
        with open(self.run_dir / 'targets.json', 'w', encoding='utf-8', newline='\n') as f:
            json.dump(tj, f, indent=1, default=str)
        rig = {'spec': 'live/SPEC_BRAIN.md', 'rig': 'live/brainrig.py', 'mode': self.mode, 'dev_oracle': self.oracle,
               'pons_url': ponsbot.URL, 'viewport': [VIEW_W, VIEW_H],
               'wallet': self.bot.address if self.bot else None,
               'wallet_kind': 'funded .env wallet' if self.live_mode else 'throwaway in-memory key',
               'dry_balance_override_eth': None if self.live_mode else ponsbot.DRY_BALANCE_WEI / 1e18, 'coin': self.coin,
               'description_typed': self.description, 'image_pinned': self.image_uri, 'creator_tax_set': self.tax_ok,
               'send_requests': len(self.sends), 'signature_requests': self.bot.signatures if self.bot else [],
               'wallet_refusals': self.bot.refusals if self.bot else [], 'page_rejection': self.rejection,
               'stages': {t.key: t.state for t in self.targets}, 'hits_forwarded': len(fwd), 'misses_masked': len(miss),
               'page_mousedowns': len(downs), 'stream': self._stream_stats(), 'dev_shots': self.dev_paths,
               'steer_policy': BRAIN['steer_source'], 'steer_sha256': BRAIN['steer_sha256'],
               'press_policy': BRAIN['press_source'], 'press_sha256': BRAIN['press_sha256'],
               'units': UNITS, 'connections': CONNECTIONS, 'presses_seen': self.presses_seen,
               'type_delay_ms': TYPE_DELAY_MS, 'honesty': HONESTY_LIVE if self.live_mode else HONESTY,
               'started_utc': utc(self.t_start), 'ended_utc': utc()}
        if self.live_mode:
            lv = CONFIG['live']
            rig['live'] = {'address': lv['address'], 'balance_eth_at_startup': lv['balance'] / 1e18,
                           'preflight_gas_limit': lv['gas'], 'preflight_max_fee_gwei': lv['max_fee'] / 1e9,
                           'preflight_sim_image': lv['sim_image'], 'config_pinned_at_startup': CONFIG.get('pinned'),
                           'tx': self.live_hash, 'result': self.live_result, 'receipt': 'live_receipt.json',
                           'journal_copy': 'launch_journal.json', 'pons_success_text': self.success,
                           'coin_page': self.coin_page}
        if self.oracle:
            o = self.sess
            with open(self.run_dir / 'oracle.json', 'w', encoding='utf-8', newline='\n') as f:
                json.dump({'kind': 'DEV_ORACLE_NOT_A_BRAIN_RUN', 'launch': outcome, 'rig': rig,
                           'commands': o.log if o else [], 'clicks': o.clicks if o else []}, f, indent=1, default=str)
            self.log(f'DEV run written to {rel(self.run_dir)} (oracle.json, targets.json, captured_tx.json, '
                     'events.jsonl, pons_final.jpg); not a brain run')
            return True
        if self.sess is None or getattr(self.sess, 'env', None) is None or self.sess.proof is None:
            self.log('no brain session to record')
            return False
        extra = {'launch': outcome, 'rig': rig, 'end_reason': self.sim_end, 'end_info': self.sim_info}
        meta = await asyncio.to_thread(self.sess.save, str(self.run_dir), extra)
        self.log(f"recorded {rel(self.run_dir)}: session.json (proof {meta['session_proof'][:16]}…, {meta['steps']} "
                 f"brain steps), qpos.npy, actions.npy, steer.pt, press.pt, captured_tx.json, events.jsonl, pons_final.jpg, "
                 f"targets.json" + (', live_receipt.json, launch_journal.json' if self.live_mode else '')
                 + f" · python replay_session.py {rel(self.run_dir)}")
        return True

    async def _teardown(self):
        self.mouse_on = False
        await self.stop_screencast()
        if self.bot:
            await self.bot.close()
        if self.cfg:
            self.cfg.pop('_key', None)             # LIVE: this run's copy of the key (LiveLaunch signs with pre['acct'])


# ------------------------------------------------------------------------------------------------------------
# HTTP + WS
# ------------------------------------------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app):
    # every executor thread commits THREAD_STACK on Windows: keep their number small
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(EXECUTOR_THREADS, thread_name_prefix='brainrig'))
    yield
    # shutting down (Ctrl+C): a LIVE run that has not reached the signer ends without signing
    r = ACTIVE.get('run')
    if r is not None and CONFIG.get('mode') == 'LIVE' and r.abort_live('the rig is shutting down'):
        say('  LIVE: shutting down before the launch was signed; nothing will be signed')


app = FastAPI(lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=2048)


@app.get('/')
async def index():
    p = LIVE_DIR / 'web' / 'brain.html'
    if not p.exists():
        return HTMLResponse('<!doctype html><title>RATBRAIN brain rig</title><body style="background:#07070a;'
                            'color:#ddd;font:14px system-ui">live/web/brain.html is not built yet.</body>')
    return HTMLResponse(p.read_text(encoding='utf-8'))


# the page's rotating rat: a DISPLAY MODEL of the simulated body (live/web/rat3d.js), not a live view of its pose.
# Bytes are read on request, in the async handler (no FileResponse, no thread pool); rat.json is cached after the
# first read.
_STATIC_CACHE = {}


def _static_bytes(p, media_type, cache=False):
    data = _STATIC_CACHE.get(p) if cache else None
    if data is None:
        try:
            data = p.read_bytes()
        except OSError:
            return PlainTextResponse(f'{p.name} not found', status_code=404)
        if cache:
            _STATIC_CACHE[p] = data
    return Response(content=data, media_type=media_type)


@app.get('/rat.json')
async def rat_json():
    return _static_bytes(LIVE_DIR / 'assets' / 'rat.json', 'application/json', cache=True)


@app.get('/rat_pose.json')
async def rat_pose_json():
    return _static_bytes(LIVE_DIR / 'assets' / 'rat_pose.json', 'application/json')


@app.get('/rat3d.js')
async def rat3d_js():
    return _static_bytes(LIVE_DIR / 'web' / 'rat3d.js', 'text/javascript; charset=utf-8')


@app.get('/status')
async def status():
    c = CONFIG['coin']
    live = CONFIG.get('mode') == 'LIVE'
    st = _status_common(c)
    if live:
        lv = CONFIG['live']
        st.update(mode='LIVE', honesty=HONESTY_LIVE, dry_balance_override_eth=None, token_required=True,
                  address=lv['address'], balance_eth_at_startup=lv['balance'] / 1e18,
                  need_eth=lv['need'] / 1e18, launch_fee_eth=LAUNCH_FEE_ETH, explorer=launcher.EXPLORER,
                  pons_coin_url=PONS_COIN_URL, live_used=STATE['live_used'],
                  journal_exists=os.path.exists(launcher.JOURNAL), signs='once, nonce 0')
    return st


def _status_common(c):
    return {'mode': 'DRY', 'dev_oracle': CONFIG['dev_oracle'], 'rig': 'brain', 'name': c['name'],
            'symbol': c['symbol'], 'tax_bps': c['tax_bps'],
            'brain': 'two trained networks (not a biological brain): a steering network and a lever-press network',
            'steer_policy': BRAIN['steer_source'], 'steer_sha256': BRAIN['steer_sha256'],
            'press_policy': BRAIN['press_source'], 'press_sha256': BRAIN['press_sha256'],
            'policy': BRAIN['steer_source'], 'policy_sha256': BRAIN['steer_sha256'], 'policy_note': BRAIN.get('note'),
            'brain_commit': BRAIN['commit'],
            'brain_commit_covers': ['scene.xml', 'steering network', 'lever-press network',
                                    *brain_session.CODE_FILES],
            'description': DESCRIPTION.format(commit=BRAIN['commit']), 'seed': CONFIG['seed'], 'preroll_s': PREROLL_S,
            'busy': STATE['busy'], 'last_run': STATE['last_run'], 'venue': 'ponsfamily.com/launchpad',
            'chain': 'Robinhood Chain 4663', 'pons_viewport': [VIEW_W, VIEW_H], 'units': UNITS,
            'connections': CONNECTIONS,
            'networks': [{'key': 'steer', 'name': 'steering network', 'sizes': list(STEER_SIZES), 'units': STEER_UNITS,
                          'connections': n_conn(STEER_SIZES), 'sha256': BRAIN['steer_sha256'],
                          'policy': BRAIN['steer_source']},
                         {'key': 'press', 'name': 'lever-press network', 'sizes': list(PRESS_SIZES),
                          'units': PRESS_UNITS, 'connections': n_conn(PRESS_SIZES), 'sha256': BRAIN['press_sha256'],
                          'policy': BRAIN['press_source']}],
            'targets': [{'stage': k, 'n': i + 1, 'label': lab} for i, (k, lab, _, _) in enumerate(TARGETS)],
            'frame': frame_layout(),
            'type_delay_ms': TYPE_DELAY_MS, 'stream': {'format': 'jpeg', 'quality': SHOT_QUALITY,
                                                       'max_fps': SHOT_MAX_FPS, 'source': 'CDP Page.startScreencast'},
            'dry_balance_override_eth': ponsbot.DRY_BALANCE_WEI / 1e18, 'honesty': HONESTY,
            'env_read': STATE['env_read'], 'token_required': False}


@app.get('/weights_summary')
async def weights():
    return JSONResponse(BRAIN['weights'])


def allowed_origins():
    return {f"http://localhost:{CONFIG['port']}", f"http://127.0.0.1:{CONFIG['port']}"}


@app.websocket('/run')
async def ws_run(ws: WebSocket):
    # only this rig's own page (or record.py / the test client, on localhost) may drive it
    origin = ws.headers.get('origin')
    if origin not in allowed_origins():
        say(f'  refused a /run websocket from origin {origin!r}')
        await ws.close(code=1008)
        return
    await ws.accept()
    run, task = None, None
    try:
        while True:
            try:
                msg = json.loads(await ws.receive_text())
            except (ValueError, TypeError):
                continue
            if not isinstance(msg, dict):
                continue
            kind = msg.get('type') or msg.get('action')
            if kind == 'stop':
                if run is not None and task is not None and not task.done() and not run.stop_fut.done():
                    if CONFIG.get('mode') == 'LIVE':
                        # LIVE: STOP works until pons's launch tx reaches the signer; after that it is too late
                        if run.abort_live('viewer'):
                            say('  LIVE: stop requested by the viewer; nothing will be signed')
                        else:
                            say('  LIVE: stop requested, but the launch tx is already with the signer')
                            await ws.send_text(json.dumps({'type': 'log', 'msg': 'too late to stop: the launch '
                                                           'transaction is already with the signer'}))
                    else:
                        say('  stop requested by the viewer')
                        run.stop_fut.set_result('viewer')
                continue
            if kind != 'start':
                continue
            if CONFIG.get('mode') == 'LIVE':
                why = live_start_refusal(ws, msg)
                if why:
                    say(f'  refused a LIVE start: {why}')
                    await ws.send_text(json.dumps({'type': 'refused', 'msg': f'LIVE start refused: {why}'}))
                    continue
            if STATE['busy']:
                if CONFIG.get('mode') == 'LIVE':
                    await ws.send_text(json.dumps({'type': 'refused', 'msg': 'the rig is busy: one run at a time'}))
                else:
                    await ws.send_text(json.dumps({'type': 'log', 'msg': 'the rig is busy: one run at a time'}))
                continue
            try:
                seed = int(msg.get('seed', CONFIG['seed']))
            except (TypeError, ValueError):
                await ws.send_text(json.dumps({'type': 'log', 'msg': f"bad seed {msg.get('seed')!r}"}))
                continue
            STATE['busy'] = True
            STATE['runs'] += 1
            run = Run(ws, seed)
            ACTIVE['run'] = run

            async def go(r=run):
                try:
                    await r.run()
                except Exception:
                    traceback.print_exc()
                finally:
                    STATE['busy'] = False
                    if ACTIVE.get('run') is r:
                        ACTIVE['run'] = None
            task = asyncio.create_task(go())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if task is not None and not task.done():
            # LIVE: the owner's recording is gone with the viewer, so a launch that has not reached the signer is
            # aborted (journal -> aborted_before_sign). Once pons's tx is with the signer the run must finish and record.
            if CONFIG.get('mode') == 'LIVE' and run is not None and run.abort_live('the viewer left'):
                say('  LIVE: the viewer left before the launch was signed; aborting, nothing will be signed')
            else:
                say('  the viewer left; the run continues and is recorded')
            try:
                await task
            except Exception:
                pass


# ------------------------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------------------------
def main():
    for stream, kw in ((sys.stdout, {'line_buffering': True}), (sys.stderr, {})):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace', **kw)
        except Exception:
            pass
    ap = argparse.ArgumentParser(description='RATBRAIN brain rig (DRY by default)')
    ap.add_argument('--port', type=int, default=4665)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--seed', type=int, default=2026)
    ap.add_argument('--policy', default='runs/final/steer.pt',
                    help='the STEERING network (a missing file falls back to the newest policy_*.pt next to it)')
    ap.add_argument('--press-policy', default='runs/final/policy.pt', help='the lever-PRESS network')
    ap.add_argument('--dev-oracle', action='store_true',
                    help='TESTING ONLY: a scripted cursor drives the targets instead of the brain (not a brain run)')
    ap.add_argument('--dev-shots', action='store_true', help='save streamed pons frames to live/dev_shots/brain2_*.jpg')
    ap.add_argument('--headful', action='store_true', help='show the Chromium window')
    ap.add_argument('--live', action='store_true',
                    help='LIVE: sign and broadcast ONE launch (needs .env RATBRAIN_LIVE=1 + key, --confirm, preflight)')
    ap.add_argument('--confirm', help='LIVE: type the coin symbol from .env (RATBRAIN_SYMBOL) to confirm')
    a = ap.parse_args()

    cfg = None
    if a.live:
        # LIVE: the .env FILE (launcher.config) is authoritative; the static gates run before anything loads
        try:
            cfg = live_config()
        except launcher.LaunchRefused as e:
            raise SystemExit(f'LIVE refused, nothing was opened: launcher.config(): {e}')
        coin = live_gate_static(a, cfg)
    else:
        if a.confirm:
            say('--confirm without --live is ignored: DRY')
        coin = coin_config()
    path, note = resolve_policy(a.policy)
    ppath = Path(a.press_policy)
    ppath = (ppath if ppath.is_absolute() else ROOT / ppath).resolve()
    for p in (path, ppath):
        try:
            p.relative_to(ROOT)
        except ValueError:
            raise SystemExit('both networks must live under the ratbrain folder')
    if not ppath.exists():
        raise SystemExit(f'press network not found: {ppath}')
    steer_bytes = snapshot_policy(path, STEER_SIZES, 'steering network')
    press_bytes = snapshot_policy(ppath, PRESS_SIZES, 'lever-press network')
    commit = brain_commit_for(steer_bytes, press_bytes)
    live = pinned = token = None
    if a.live:
        live = live_gate_chain(cfg, commit)        # preflight with the brain commit; exits on any failure
        pinned = pin_config(cfg)
        if pinned['key_address'] != live['address']:
            raise SystemExit('LIVE refused, nothing was opened: the preflight wallet is not the .env key')
        token = secrets.token_urlsafe(24)
        cfg.clear()                                # the key is re-read from .env per run (launcher.config)
    layout = big_stack(body_layout)
    BRAIN.update(steer_bytes=steer_bytes, press_bytes=press_bytes, steer_source=rel(path), press_source=rel(ppath),
                 note=note, steer_sha256=sha(steer_bytes), press_sha256=sha(press_bytes), commit=commit,
                 weights=weights_summary(steer_bytes, press_bytes, layout, {'steer': rel(path), 'press': rel(ppath)}))
    CONFIG.update(coin=coin, seed=a.seed, port=a.port, dev_oracle=a.dev_oracle, dev_shots=a.dev_shots,
                  headful=a.headful, mode='LIVE' if live else 'DRY', live=live, token=token, pinned=pinned)
    if live:
        url = f'http://localhost:{a.port}/?token={token}'
        say(f"\n  RATBRAIN brain rig · LIVE · {coin['name']} (${coin['symbol']}) · seed {a.seed}\n"
            + (f'  {note}\n' if note else '')
            + f"  steering network  {rel(path)} sha256 {BRAIN['steer_sha256']}\n"
            f"  lever-press net   {rel(ppath)} sha256 {BRAIN['press_sha256']}\n"
            f"  {UNITS:,} units · {CONNECTIONS:,} connections · brain commit {commit}\n"
            f"  wallet {live['address']} · balance {live['balance'] / 1e18:.6f} ETH · needs {live['need'] / 1e18:.6f} "
            f"ETH (x1.5 checked) · signs ONCE at nonce 0\n"
            f"  open {url}\n"
            f"  Nothing happens until YOU click BEGIN SESSION in that page and then Start in its confirm dialog.\n")
    else:
        say(f"\n  RATBRAIN brain rig · DRY RUN{' · DEV ORACLE (scripted cursor, NOT the rat)' if a.dev_oracle else ''} · "
            f"{coin['name']} (${coin['symbol']}) · seed {a.seed}\n"
            + (f'  {note}\n' if note else '')
            + f"  steering network  {rel(path)} sha256 {BRAIN['steer_sha256']}\n"
            f"  lever-press net   {rel(ppath)} sha256 {BRAIN['press_sha256']}\n"
            f"  {UNITS:,} units · {CONNECTIONS:,} connections · brain commit {commit}\n"
            f"  open http://localhost:{a.port}/\n")
    uvicorn.run(app, host=a.host, port=a.port, log_level='warning', ws_max_size=16 * 1024 * 1024)


if __name__ == '__main__':
    main()
