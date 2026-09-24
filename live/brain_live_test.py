"""Mocked tests of the brain rig's LIVE path (live/brainrig.py --live), for the owner's test launch.

NOTHING here talks to a chain, signs with the real key, reads .env, or starts a real session:
  * .env is trapped: opening <ratbrain>/.env (builtins.open, io.open, os.open) raises and is recorded;
    launcher.read_env_file raises; launcher.config is a fake that returns a THROWAWAY key made here (Account.create());
    while a DRY test runs, the fake is a tripwire that fails if anything calls it
  * network is sandboxed: requests.post / requests.Session.request raise; socket.connect refuses non-loopback
  * launcher.rpc is a mock chain (nonce, balance, gas, eth_call, receipts); eth_sendRawTransaction only records the raw
    bytes (and what the journal said at that instant)
  * launcher.JOURNAL points into a temp dir; the real launch_journal.json must not exist at the end
  * LocalAccount.sign_transaction is spied and may only ever sign with the throwaway test key
  * the WS / page tests run brainrig's own app on YOUR port (default 4673) with a FAKE Run class: no pons, no chain

  (a) every LIVE gate refuses: no --live (DRY), missing / wrong --confirm, RATBRAIN_LIVE != 1, no key, bad config,
      links set, fractional tax, --dev-oracle, journal present (signed / unsigned), nonce != 0, low balance, failing
      simulation, bad token (f); a fully gated startup prints the ?token= URL and nothing opens before the gates
  (b) a correct create tx -> exactly one sign + one broadcast, the journal holds the raw signed tx BEFORE the broadcast,
      pons gets the real hash, the receipt gives the token, tx phases checked -> signed -> sent -> mined, and the run
      dir gets live_receipt.json + a launch_journal.json copy
  (c) tampered txs (value, developer buy, creator, from, symbol, name, description, to, selector, tax, pair, image,
      links) -> 4001, nothing signed or broadcast, journal aborted_before_sign; and checks that pass but a chain that
      disagrees at sign time (simulation fails, nonce moved, gas over the preflight limit) -> 4001, unsigned
  (d) a second eth_sendTransaction in the same run -> refused; LiveLaunch refuses a second sign; a new start in the same
      process is refused; a restart is refused by the journal and by nonce != 0
  (e) the DRY path never calls launcher.config() and never opens .env (startup, /status, a DRY wallet-hook run)
  (f) WS + page (headless Chromium on your port): a LIVE start without / with a wrong token, without the confirm, from
      a script or a headless browser, or after the launch was used, is refused; ?autostart=1 is ignored in LIVE;
      BEGIN SESSION shows the confirm dialog BEFORE anything is sent; Cancel / Esc send nothing; Start sends
      {type, seed, token, confirm}; the page shows the LIVE tx phases, links and outcome and sets __rigDone /
      __rigDoneMsg (hash + token); without the token the dialog warns and Start is disabled; DRY ?autostart=1 still
      sends exactly {"type":"start","seed":2026}; record.py refuses to start a LIVE brain rig

    python live/brain_live_test.py [--port 4673] [--no-browser] [--shots live/dev_shots]
"""
import argparse
import asyncio
import builtins
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

LIVE_DIR = Path(__file__).resolve().parent
ROOT = LIVE_DIR.parent
ENV_PATH = os.path.normcase(os.path.abspath(ROOT / '.env'))
REAL_JOURNAL = ROOT / 'launch_journal.json'
ENV_TOUCHES = []
NET_BLOCKED = []


# ------------------------------------------------------------------------------------------------------------
# the sandbox, installed before anything else is imported
# ------------------------------------------------------------------------------------------------------------
def _is_env(p):
    try:
        return os.path.normcase(os.path.abspath(os.fspath(p))) == ENV_PATH
    except TypeError:
        return False


_real_open, _real_os_open = builtins.open, os.open


def _open(file, *a, **k):
    if not isinstance(file, int) and _is_env(file):
        ENV_TOUCHES.append(''.join(traceback.format_stack(limit=8)))
        raise PermissionError('test sandbox: .env is never opened by this test')
    return _real_open(file, *a, **k)


def _os_open(path, flags, mode=0o777, *, dir_fd=None):
    if _is_env(path):
        ENV_TOUCHES.append(''.join(traceback.format_stack(limit=8)))
        raise PermissionError('test sandbox: .env is never opened by this test')
    return _real_os_open(path, flags, mode, dir_fd=dir_fd)


builtins.open = _open
io.open = _open
os.open = _os_open

_real_connect = socket.socket.connect


def _connect(self, addr):
    host = addr[0] if isinstance(addr, tuple) else str(addr)
    if host not in ('127.0.0.1', 'localhost', '::1'):
        NET_BLOCKED.append(str(addr))
        raise ConnectionRefusedError(f'test sandbox: no network ({addr})')
    return _real_connect(self, addr)


socket.socket.connect = _connect

import requests  # noqa: E402


def _no_http(*a, **k):
    NET_BLOCKED.append(f'requests {a[:2]}')
    raise ConnectionRefusedError('test sandbox: no HTTP from the test process')


requests.post = _no_http
_real_session_request = requests.Session.request


def _session_request(self, method, url, *a, **k):
    if not str(url).startswith(('http://127.0.0.1', 'http://localhost')):
        return _no_http(method, url)
    return _real_session_request(self, method, url, *a, **k)


requests.Session.request = _session_request

for _p in (str(ROOT), str(LIVE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import launcher  # noqa: E402
from eth_abi import encode  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_account.signers.local import LocalAccount  # noqa: E402
from eth_utils import keccak, to_checksum_address  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix='brain_live_test_'))
launcher.JOURNAL = str(TMP / 'launch_journal.json')     # never the real one


def _no_env_file():
    ENV_TOUCHES.append('launcher.read_env_file called')
    raise AssertionError('test sandbox: launcher.read_env_file must never run')


launcher.read_env_file = _no_env_file

TEST_ACCT = Account.create()                           # throwaway: never funded, never seen by any chain
TEST_KEY = '0x' + bytes(TEST_ACCT.key).hex()
OTHER = to_checksum_address('0x' + '5a' * 20)
CALLS = {'config': 0}
FAKE = {'cfg': None}                                   # None = tripwire (DRY)


def fake_cfg(**over):
    c = {'name': 'ratbrain test', 'symbol': 'RATTEST', 'image': '', 'x': '', 'website': '', 'tax_bps': 100,
         'live_env': True, '_key': TEST_KEY}
    c.update(over)
    return c


def fake_config():
    CALLS['config'] += 1
    if FAKE['cfg'] is None:
        raise AssertionError('launcher.config() called while the test forbids it (DRY path)')
    if isinstance(FAKE['cfg'], Exception):
        raise FAKE['cfg']
    return dict(FAKE['cfg'])


launcher.config = fake_config

SIGNS = []
SIGN_VIOLATIONS = []
_real_sign = LocalAccount.sign_transaction


def _spy_sign(self, tx, *a, **k):
    if self.address != TEST_ACCT.address:
        SIGN_VIOLATIONS.append(self.address)
        raise AssertionError('refusing to sign with a key that is not the throwaway test key')
    SIGNS.append(dict(tx))
    return _real_sign(self, tx, *a, **k)


LocalAccount.sign_transaction = _spy_sign


class Chain:
    """launcher.rpc stand-in. eth_sendRawTransaction records the raw tx and the journal as it was at that instant."""

    def __init__(self):
        self.token = to_checksum_address('0x' + 'c0' * 19 + '01')
        self.curve = to_checksum_address('0x' + 'c0' * 19 + '02')
        self.reset()

    def reset(self, **kw):
        self.nonce, self.balance, self.gas_price, self.estimate = 0, 10 ** 17, 365_000_000, 3_760_000
        self.call_ok, self.send_error, self.receipt_ready, self.block = True, None, True, 4_242_424
        self.__dict__.update(kw)
        self.sent, self.journal_at_send, self.methods = [], [], []

    def rpc(self, method, params, all_rpcs_on_error=False):
        self.methods.append(method)
        if method == 'eth_getTransactionCount':
            return hex(self.nonce), None
        if method == 'eth_getBalance':
            return hex(self.balance), None
        if method == 'eth_gasPrice':
            return hex(self.gas_price), None
        if method == 'eth_estimateGas':
            return hex(self.estimate), None
        if method == 'eth_call':
            if not self.call_ok:
                return None, {'code': 3, 'message': 'execution reverted (mock)'}
            return '0x' + encode(['address', 'address'], [self.token, self.curve]).hex(), None
        if method == 'eth_sendRawTransaction':
            raw = params[0]
            try:
                with _real_open(launcher.JOURNAL, encoding='utf-8') as fh:
                    self.journal_at_send.append(json.load(fh))
            except Exception as e:
                self.journal_at_send.append({'unreadable': repr(e)})
            self.sent.append(raw)
            return '0x' + keccak(bytes.fromhex(raw[2:])).hex(), self.send_error
        if method == 'eth_getTransactionReceipt':
            if not self.sent or not self.receipt_ready:
                return None, None
            return self.receipt(params[0]), None
        if method == 'eth_getTransactionByHash':
            return ({'hash': params[0]} if self.sent else None), None
        raise AssertionError(f'unexpected rpc {method}')

    def receipt(self, h):
        pad = lambda a: '0x' + '0' * 24 + a[2:].lower()  # noqa: E731
        return {'transactionHash': h, 'status': '0x1', 'blockNumber': hex(self.block), 'gasUsed': hex(3_512_345),
                'effectiveGasPrice': hex(self.gas_price),
                'logs': [{'address': launcher.FACTORY, 'data': '0x',
                          'topics': [launcher.TOKEN_LAUNCHED_TOPIC, pad(self.token), pad(self.curve),
                                     pad(TEST_ACCT.address)]}]}


CHAIN = Chain()
launcher.rpc = CHAIN.rpc

import ponsbot  # noqa: E402
import brainrig  # noqa: E402

SAY = []
brainrig.say = lambda *parts: SAY.append(' '.join(str(x) for x in parts))
brainrig.SUCCESS_WAIT_S = 1
IMG = 'ipfs://bafybeiedfhkewsq4ljrjenvjpy4qn3jurctmlfzoaslhvco5i7jzqid46a'
B32A = bytes.fromhex('a9fc75d4203a33fe660e8fa32c74c3aa41c1fda4bf23d3a39b6bc22a1f8b1ca7')
B32B = bytes.fromhex('39778730803b5b526a868f897882ac75dbf242b234e3fe5e62ef54623284dacc')
BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 '
              'Safari/537.36')
RESULTS = []


def check(name, ok, detail=''):
    RESULTS.append((name, bool(ok), str(detail)[:230]))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f'  ·  {str(detail)[:230]}' if detail else ''), flush=True)


def clear_journal():
    if os.path.exists(launcher.JOURNAL):
        os.remove(launcher.JOURNAL)


def journal():
    try:
        with _real_open(launcher.JOURNAL, encoding='utf-8') as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None


def make_tx(creator, desc, name='ratbrain test', symbol='RATTEST', image=IMG, tax=100, amount_in=0,
            pair=launcher.ZERO, value=launcher.LAUNCH_FEE_WEI, to=launcher.FACTORY, selector=launcher.SELECTOR,
            socials=('', '', '', '', ''), sender=None):
    """A create tx shaped exactly like the one pons built in the DRY brain runs (its two bytes32 fields included)."""
    params = (name, symbol, image, desc, tuple(socials), creator, tax, 0, B32A, B32B)
    data = selector + encode(launcher.TYPES, [params, amount_in, pair, b'']).hex()
    return {'from': sender or creator, 'to': to, 'data': data, 'value': hex(value)}


def run_main(argv):
    """brainrig.main() with argv; uvicorn.run is replaced, so nothing ever serves. -> (exit message or None, served)."""
    served = []
    real_uv = brainrig.uvicorn
    brainrig.uvicorn = type('UV', (), {'run': staticmethod(lambda *a, **k: served.append(k))})
    old = sys.argv
    sys.argv = ['brainrig.py'] + argv
    try:
        brainrig.main()
        return None, served
    except SystemExit as e:
        return str(e), served
    finally:
        sys.argv = old
        brainrig.uvicorn = real_uv


class FakeWS:
    def __init__(self):
        self.msgs = []

    async def send_text(self, t):
        self.msgs.append(json.loads(t))

    async def send_bytes(self, b):
        pass


class FakeBot:
    def __init__(self, address):
        self.address, self.armed, self.page = address, True, None
        self.signatures, self.refusals = [], []

    async def wait_rejection(self, s):
        return 'Launch failed / You cancelled in your wallet.'

    async def jpeg(self, q=60):
        raise RuntimeError('no page in the test')

    async def close(self):
        pass

    def arm(self):
        self.armed = True


async def make_live_run(k):
    """A LIVE Run as the real one is at the rat's Confirm: prepared, form done, armed (preflight + journal)."""
    r = brainrig.Run(FakeWS(), 2026)
    r.run_dir = TMP / f'run_{k}'
    r.run_dir.mkdir()
    r.out = brainrig.Outbox(r.ws, r.run_dir / 'events.jsonl')
    r._live_prepare()
    r.commit = brainrig.BRAIN['commit']
    r.description = brainrig.DESCRIPTION.format(commit=r.commit)
    r.bot = FakeBot(brainrig.CONFIG['live']['address'])
    r.tax_ok, r.image_uri = True, IMG
    await r._live_arm(r.targets[10])                  # armed before the rat's Confirm (t11)
    return r


async def settle(r):
    await asyncio.sleep(0.2)
    if r.rejection_task:
        await r.rejection_task


def tx_msgs(r):
    return [m for m in r.ws.msgs if m.get('type') == 'tx']


# ------------------------------------------------------------------------------------------------------------
# (e) DRY never reads .env  (first: nothing has called launcher.config yet)
# ------------------------------------------------------------------------------------------------------------
def test_dry():
    print('\n== (e) the DRY path never calls launcher.config() and never opens .env')
    FAKE['cfg'] = None
    n0, t0 = CALLS['config'], len(ENV_TOUCHES)
    why, served = run_main(['--port', '4673'])
    check('DRY startup (no --live) serves, mode DRY, no launcher.config(), .env never opened',
          why is None and served and brainrig.CONFIG['mode'] == 'DRY' and brainrig.CONFIG['token'] is None
          and CALLS['config'] == n0 and len(ENV_TOUCHES) == t0 and brainrig.STATE['env_read'] is False,
          f"mode {brainrig.CONFIG.get('mode')} config calls {CALLS['config'] - n0} env opens {len(ENV_TOUCHES) - t0}")
    why, served = run_main(['--port', '4673', '--confirm', 'RATTEST'])
    check('--confirm without --live: still DRY, still no .env', why is None and served and brainrig.CONFIG['mode'] == 'DRY'
          and CALLS['config'] == n0 and any('ignored: DRY' in s for s in SAY), '')
    st = asyncio.run(brainrig.status())
    check('DRY /status: mode DRY, env_read false, no token, DRY balance override disclosed',
          st['mode'] == 'DRY' and st['env_read'] is False and st['token_required'] is False
          and st['dry_balance_override_eth'] == 1.0 and 'address' not in st, json.dumps({k: st[k] for k in ('mode', 'env_read', 'token_required')}))

    async def dry_hook():
        r = brainrig.Run(FakeWS(), 2026)
        r.run_dir = TMP / 'run_dry'
        r.run_dir.mkdir()
        r.out = brainrig.Outbox(r.ws, r.run_dir / 'events.jsonl')
        r.commit = brainrig.BRAIN['commit']
        r.description = brainrig.DESCRIPTION.format(commit=r.commit)
        thr = ponsbot.throwaway_account()
        r.bot = FakeBot(thr.address)
        r.tax_ok, r.image_uri = True, IMG
        real_sim = ponsbot.simulate
        ponsbot.simulate = lambda *a, **k: {'ok': True, 'predicted_token': CHAIN.token, 'predicted_curve': CHAIN.curve,
                                            'gas_estimate': 3745018, 'fake_balance_eth': 1.0}
        try:
            c = brainrig.CONFIG['coin']
            tx = make_tx(thr.address, r.description, name=c['name'], symbol=c['symbol'])
            resp = await r.on_send(tx)
            await settle(r)
            out = r._outcome()
        finally:
            ponsbot.simulate = real_sim
        await r.out.close()
        return r, resp, out
    s0, n_sent = len(SIGNS), len(CHAIN.sent)
    r, resp, out = asyncio.run(dry_hook())
    check('DRY wallet hook: every check passes, refused 4001 "DRY RUN", nothing signed or sent, no config, no journal',
          resp == ponsbot.refusal() and r.capture['verdict'] == 'dry_captured' and out['signed'] is False
          and len(SIGNS) == s0 and len(CHAIN.sent) == n_sent and CALLS['config'] == n0 and journal() is None
          and brainrig.STATE['env_read'] is False and r.capture['mode'] == 'DRY',
          f"resp {resp} verdict {r.capture.get('verdict')}")
    check('DRY tx message unchanged (one tx, mode DRY, no LIVE phase)',
          [m.get('mode') for m in tx_msgs(r)] == ['DRY'] and 'phase' not in tx_msgs(r)[0], '')


# ------------------------------------------------------------------------------------------------------------
# (a) the startup gates
# ------------------------------------------------------------------------------------------------------------
def test_gates():
    print('\n== (a) every LIVE startup gate refuses')
    clear_journal()
    CHAIN.reset()

    def refused(label, cfg, argv, want):
        FAKE['cfg'] = cfg
        n_sent = len(CHAIN.sent)
        why, served = run_main(argv)
        check(f'refused: {label}', why is not None and want in why and not served and len(CHAIN.sent) == n_sent
              and journal() is None, why)
    live = ['--port', '4673', '--live', '--confirm', 'RATTEST']
    refused('--live without --confirm', fake_cfg(), ['--port', '4673', '--live'], '--confirm must equal')
    refused('--confirm is not the .env symbol', fake_cfg(), ['--port', '4673', '--live', '--confirm', 'RATBRAIN'],
            '--confirm must equal')
    refused('RATBRAIN_LIVE != 1', fake_cfg(live_env=False), live, 'RATBRAIN_LIVE=1')
    refused('no RATBRAIN_RH_KEY', fake_cfg(_key=''), live, 'RATBRAIN_RH_KEY')
    refused('launcher.config() refuses (bad .env)', launcher.LaunchRefused('bad symbol (A-Z 0-9, <= 10)'), live,
            'launcher.config()')
    refused('RATBRAIN_X set (the rat fills no links)', fake_cfg(x='someone'), live, 'RATBRAIN_X')
    refused('fractional creator tax', fake_cfg(tax_bps=150), live, 'whole percent')
    refused('--dev-oracle with --live', fake_cfg(), live + ['--dev-oracle'], 'dev-oracle')
    with _real_open(launcher.JOURNAL, 'w') as fh:
        json.dump({'state': 'live', 'tx': '0x' + 'ab' * 32, 'raw': '0x02f8'}, fh)
    FAKE['cfg'] = fake_cfg()
    why, served = run_main(live)
    check('refused: a SIGNED launch in the journal (names --resolve, never a second launch)',
          why and 'SIGNED' in why and '--resolve' in why and not served, why)
    with _real_open(launcher.JOURNAL, 'w') as fh:
        json.dump({'state': 'aborted_before_sign', 'error': 'x'}, fh)
    why, served = run_main(live)
    check('refused: an unsigned reservation in the journal (names --clear-unsigned)',
          why and '--clear-unsigned' in why and not served, why)
    clear_journal()
    for label, kw, want in (('wallet nonce 1 (not fresh)', {'nonce': 1}, 'nonce 1'),
                            ('balance below 1.5x gas + fee', {'balance': 5 * 10 ** 15}, 'balance'),
                            ('the launch does not simulate', {'call_ok': False}, 'simulation failed')):
        CHAIN.reset(**kw)
        FAKE['cfg'] = fake_cfg()
        why, served = run_main(live)
        check(f'refused (preflight): {label}', why and want in why and not served and not CHAIN.sent
              and 'eth_sendRawTransaction' not in CHAIN.methods, why)
    CHAIN.reset()
    SAY.clear()
    FAKE['cfg'] = fake_cfg()
    why, served = run_main(live)
    tok = brainrig.CONFIG.get('token')
    out = '\n'.join(SAY)
    check('all gates pass: serves LIVE, prints http://localhost:4673/?token=<per-process token>, key not printed',
          why is None and served and brainrig.CONFIG['mode'] == 'LIVE' and tok and len(tok) >= 24
          and f'http://localhost:4673/?token={tok}' in out and TEST_KEY[2:] not in out
          and brainrig.CONFIG['live']['address'] == TEST_ACCT.address and not CHAIN.sent and not SIGNS
          and brainrig.STATE['env_read'] is True, f"mode {brainrig.CONFIG.get('mode')} token {bool(tok)}")
    check('startup preflight used the brain commit and coin.png as pons pins it (RATBRAIN_IMAGE empty)',
          brainrig.CONFIG['live']['commit'] == brainrig.BRAIN['commit']
          and brainrig.CONFIG['live']['sim_image'] == IMG, brainrig.CONFIG['live']['sim_image'])
    st = asyncio.run(brainrig.status())
    check('LIVE /status: mode LIVE, real address, no DRY override, token required, 0.0005 fee, explorer, no key',
          st['mode'] == 'LIVE' and st['address'] == TEST_ACCT.address and st['dry_balance_override_eth'] is None
          and st['token_required'] is True and st['launch_fee_eth'] == 0.0005
          and st['explorer'] == launcher.EXPLORER and TEST_KEY[2:] not in json.dumps(st, default=str)
          and st['symbol'] == 'RATTEST' and st['name'] == 'ratbrain test', '')


# ------------------------------------------------------------------------------------------------------------
# (b) + (d) the good launch; (c) tampered
# ------------------------------------------------------------------------------------------------------------
def test_launch():
    print('\n== (b) a correct create tx: exactly one sign + one broadcast, journal first')
    clear_journal()
    CHAIN.reset()
    FAKE['cfg'] = fake_cfg()
    SIGNS.clear()

    async def go():
        r = await make_live_run('good')
        j_armed = journal()
        tx = make_tx(r.bot.address, r.description)
        resp = await r.on_send(tx)
        mid = (len(SIGNS), len(CHAIN.sent))
        out = await r._outcome_live()
        resp2 = await r.on_send(tx)                     # (d) a second request in the same run
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, r.live.sign_and_send, tx, *ponsbot.decode_launch(tx)[:1], {'x': {'ok': True}},
                ponsbot.decode_launch(tx)[1])
            again = 'signed again'
        except launcher.LaunchRefused as e:
            again = str(e)
        saved = await r._finish(out)
        await r.out.close()
        return r, tx, j_armed, resp, mid, out, resp2, again, saved
    r, tx, j_armed, resp, mid, out, resp2, again, saved = asyncio.run(go())
    check('armed: journal reserved (state reserved, via live/brainrig.py, brain commit) before any request',
          j_armed and j_armed['state'] == 'reserved' and j_armed['via'] == 'live/brainrig.py'
          and j_armed['brain_commit'] == brainrig.BRAIN['commit'], j_armed)
    h = (resp or {}).get('result')
    check('pons gets the real tx hash (keccak of the broadcast raw tx)', isinstance(h, str) and len(h) == 66
          and CHAIN.sent and h == '0x' + keccak(bytes.fromhex(CHAIN.sent[0][2:])).hex(), resp)
    check('exactly ONE sign and ONE broadcast', mid == (1, 1) and len(SIGNS) == 1 and len(CHAIN.sent) == 1, mid)
    s = SIGNS[0] if SIGNS else {}
    check('signed: nonce 0, chain 4663, to factory, value 0.0005 ETH, exactly the checked calldata, preflight gas',
          s.get('nonce') == 0 and s.get('chainId') == 4663 and s.get('to') == launcher.FACTORY
          and s.get('value') == launcher.LAUNCH_FEE_WEI and s.get('data') == tx['data'].lower()
          and s.get('gas') == brainrig.CONFIG['live']['gas'] and s.get('maxPriorityFeePerGas') == 0
          and s.get('type') == 2, {k: s.get(k) for k in ('nonce', 'chainId', 'to', 'value', 'gas', 'type')})
    ja = CHAIN.journal_at_send[0] if CHAIN.journal_at_send else {}
    check('the journal held the raw SIGNED tx before the broadcast (write-ahead)', ja.get('state') == 'signed'
          and ja.get('raw') == CHAIN.sent[0] and ja.get('nonce') == 0 and ja.get('via') == 'live/brainrig.py'
          and ja.get('description') == r.description, {k: ja.get(k) for k in ('state', 'nonce', 'via')})
    phases = [m.get('phase') for m in tx_msgs(r)]
    check('tx phases to the viewer: checked -> signed -> sent -> mined', phases == ['checked', 'signed', 'sent', 'mined'],
          phases)
    mined = tx_msgs(r)[-1] if tx_msgs(r) else {}
    check('mined: block, token from TokenLaunched, explorer + pons links', out.get('mode') == 'live'
          and out.get('token') == CHAIN.token and mined.get('token') == CHAIN.token and mined.get('block') == CHAIN.block
          and mined.get('explorer_token') == f'{launcher.EXPLORER}/token/{CHAIN.token}'
          and mined.get('pons_coin') == f'https://www.ponsfamily.com/launchpad/{CHAIN.token}', mined)
    check('outcome: signed True, broadcast True, checks passed, brain commit', out.get('signed') is True
          and out.get('broadcast') is True and out.get('checks_passed') is True
          and out.get('brain_commit') == brainrig.BRAIN['commit'], {k: out.get(k) for k in ('mode', 'signed', 'broadcast')})
    rc = json.load(_real_open(r.run_dir / 'live_receipt.json', encoding='utf-8')) \
        if (r.run_dir / 'live_receipt.json').exists() else {}
    jc = json.load(_real_open(r.run_dir / 'launch_journal.json', encoding='utf-8')) \
        if (r.run_dir / 'launch_journal.json').exists() else {}
    check('run dir: live_receipt.json (hash, block, token, gas used, fee) + launch_journal.json copy',
          rc.get('tx') == h and rc.get('block') == CHAIN.block and rc.get('token') == CHAIN.token
          and rc.get('gas_used') == 3_512_345 and rc.get('gas_fee_eth') and rc.get('signed') is True
          and jc.get('state') == 'live' and jc.get('raw') == CHAIN.sent[0] and os.path.exists(launcher.JOURNAL),
          {k: rc.get(k) for k in ('tx', 'block', 'token', 'gas_used')})
    cap = json.load(_real_open(r.run_dir / 'captured_tx.json', encoding='utf-8'))
    check('captured_tx.json: mode LIVE, verdict live_sent, hash, response to pons = the hash', cap.get('mode') == 'LIVE'
          and cap.get('verdict') == 'live_sent' and cap.get('hash') == h and cap.get('response_to_page') == {'result': h}, '')
    check('nothing of the key in the run dir or the events', all(TEST_KEY[2:] not in p.read_text(encoding='utf-8', errors='ignore')
                                                               for p in r.run_dir.iterdir() if p.is_file()), '')

    print('\n== (d) never twice')
    check('a second eth_sendTransaction in the same run is refused (4001), nothing signed',
          (resp2 or {}).get('error', {}).get('code') == 4001 and len(SIGNS) == 1 and len(CHAIN.sent) == 1, resp2)
    check('LiveLaunch itself refuses a second sign', 'already handled' in again and len(SIGNS) == 1, again)

    class H(dict):
        def get(self, k, d=None):
            return super().get(k.lower(), d)
    ws_like = type('W', (), {'headers': H({'user-agent': BROWSER_UA})})()
    why = brainrig.live_start_refusal(ws_like, {'type': 'start', 'token': brainrig.CONFIG['token'], 'confirm': 'RATTEST'})
    check('a new start in the same process is refused (its one launch is used)', why and 'already used' in why, why)
    why, served = run_main(['--port', '4673', '--live', '--confirm', 'RATTEST'])
    check('a restart is refused by the journal (signed launch -> --resolve)', why and '--resolve' in why and not served, why)
    saved_j = journal()
    clear_journal()
    CHAIN.reset(nonce=1)
    why, served = run_main(['--port', '4673', '--live', '--confirm', 'RATTEST'])
    check('... and, even without the journal, by the wallet nonce (1 != 0)', why and 'nonce 1' in why and not served, why)
    with _real_open(launcher.JOURNAL, 'w') as fh:
        json.dump(saved_j, fh)
    # restore a LIVE CONFIG for the tampered tests and the page (the refused restarts left CONFIG as it was)
    clear_journal()
    CHAIN.reset()
    brainrig.STATE['live_used'] = False


def test_tampered():
    print('\n== (c) tampered transactions: 4001, nothing signed or broadcast')
    FAKE['cfg'] = fake_cfg()
    commit = brainrig.BRAIN['commit']
    desc = brainrig.DESCRIPTION.format(commit=commit)
    bad_commit = ('0' if commit[0] != '0' else '1') + commit[1:]
    me = TEST_ACCT.address
    cases = [
        ('value: extra ETH (a developer buy paid in value)', dict(value=launcher.LAUNCH_FEE_WEI + 10 ** 15), 'value'),
        ('developer buy (amountIn > 0)', dict(amount_in=10 ** 15), 'amountIn'),
        ('creator: another address', dict(creator=OTHER, sender=me), 'creator'),
        ('from: another address', dict(sender=OTHER), 'from'),
        ('symbol', dict(symbol='RATTESX'), 'symbol'),
        ('name', dict(name='ratbrain test2'), 'name'),
        ('description: brain commit altered', dict(desc=brainrig.DESCRIPTION.format(commit=bad_commit)), 'description'),
        ('to: not the pons factory', dict(to=OTHER), 'to'),
        ('selector: not the pons launch', dict(selector='0xdeadbeef'), 'decode'),
        ('creator tax 2%', dict(tax=200), 'creatorTaxBps'),
        ('pair token not ETH', dict(pair=OTHER), 'pairToken'),
        ('image not pons\'s pin', dict(image='ipfs://bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi'), 'image'),
        ('links (X handle)', dict(socials=('someone', '', '', '', '')), 'socials'),
    ]
    for i, (label, kw, key) in enumerate(cases):
        clear_journal()
        CHAIN.reset()
        s0 = len(SIGNS)

        async def go(kw=kw, i=i):
            r = await make_live_run(f'bad{i}')
            k = dict(kw)
            creator = k.pop('creator', me)
            d = k.pop('desc', desc)
            resp = await r.on_send(make_tx(creator, d, **k))
            await settle(r)
            out = await r._outcome_live()
            await r.out.close()
            return r, resp, out
        r, resp, out = asyncio.run(go())
        j = journal() or {}
        failed = (tx_msgs(r)[-1] if tx_msgs(r) else {}).get('failed') or []
        check(f'tampered {label} -> 4001, unsigned', (resp or {}).get('error', {}).get('code') == 4001
              and len(SIGNS) == s0 and not CHAIN.sent and j.get('state') == 'aborted_before_sign' and 'raw' not in j
              and key in failed and out.get('signed') is False and r.live.used,
              f"failed {failed} journal {j.get('state')}")
    for label, kw in (('the launch no longer simulates at sign time', {'call_ok': False}),
                      ('the wallet nonce moved to 1 after arming', {'nonce': 1}),
                      ('gas estimate above the preflight limit', {'estimate': 9_000_000})):
        clear_journal()
        CHAIN.reset()
        s0 = len(SIGNS)

        async def go2(kw=kw):
            r = await make_live_run('late_' + '_'.join(kw))
            CHAIN.__dict__.update(kw)                  # the chain changes between the arm and the rat's Confirm
            resp = await r.on_send(make_tx(me, desc))
            await settle(r)
            out = await r._outcome_live()
            await r.out.close()
            return r, resp, out
        r, resp, out = asyncio.run(go2())
        j = journal() or {}
        check(f'checks pass but {label} -> 4001, unsigned', (resp or {}).get('error', {}).get('code') == 4001
              and len(SIGNS) == s0 and not CHAIN.sent and j.get('state') == 'aborted_before_sign' and 'raw' not in j
              and out.get('signed') is False, j.get('error'))
    clear_journal()
    CHAIN.reset()
    brainrig.STATE['live_used'] = False


# ------------------------------------------------------------------------------------------------------------
# (f) WS + page, on our own port, with a FAKE Run
# ------------------------------------------------------------------------------------------------------------
class FakeRun:
    started = []
    hang = False            # test_abort: the run stays in flight (before signing) until stop_fut is set
    hang_signed = False     # ... and pretends pons's tx already reached the signer (too late to stop)
    last = None
    abort_live = brainrig.Run.abort_live          # the REAL abort rule, on this fake's fields

    def __init__(self, ws, seed):
        self.ws, self.seed = ws, seed
        self.stop_fut = asyncio.get_running_loop().create_future()
        self.live_mode = brainrig.CONFIG['mode'] == 'LIVE'
        self.sign_started = FakeRun.hang_signed
        FakeRun.last = self
        FakeRun.started.append({'seed': seed, 'ua': ws.headers.get('user-agent'), 'mode': brainrig.CONFIG['mode']})

    async def run(self):
        if FakeRun.hang:
            await self.ws.send_text(json.dumps({'type': 'log', 'msg': 'FAKE run in flight (test)'}))
            await self.stop_fut
            return
        h = '0x' + '7a' * 32
        tok = CHAIN.token
        if brainrig.CONFIG['mode'] == 'LIVE':
            brainrig.STATE['live_used'] = True           # what the real Run's _live_arm does
        ok = {k: {'ok': True, 'expected': '-'} for k in ('to', 'selector', 'value', 'creator', 'name', 'symbol')}
        script = [{'type': 'log', 'msg': 'FAKE run (test): no pons, no chain'},
                  {'type': 'stage', 'stage': 't11_confirm', 'n': 11, 'label': 'Confirm', 'state': 'active', 'detail': 'lit 90x40 px'},
                  {'type': 'stage', 'stage': 't11_confirm', 'n': 11, 'label': 'Confirm', 'state': 'hit', 'detail': 'at (640, 500)'}]
        if brainrig.CONFIG['mode'] == 'LIVE':
            script += [{'type': 'tx', 'mode': 'LIVE', 'phase': 'checked', 'verdict': 'live_checked', 'checks': ok, 'failed': []},
                       {'type': 'tx', 'mode': 'LIVE', 'phase': 'signed', 'verdict': 'live_signed', 'hash': h, 'nonce': 0},
                       {'type': 'tx', 'mode': 'LIVE', 'phase': 'sent', 'verdict': 'live_sent', 'hash': h, 'broadcast': True},
                       {'type': 'tx', 'mode': 'LIVE', 'phase': 'mined', 'verdict': 'live_mined', 'hash': h,
                        'block': CHAIN.block, 'token': tok},
                       {'type': 'done', 'run_dir': 'runs/brainrig_TEST', 'outcome': 'live', 'mode': 'LIVE', 'tx': h,
                        'token': tok, 'block': CHAIN.block, 'signed': True, 'broadcast': True, 'live_used': True,
                        'launch': {'mode': 'live', 'tx': h, 'token': tok}}]
        else:
            script += [{'type': 'done', 'run_dir': 'runs/brainrig_TEST', 'outcome': 'dry_captured',
                        'launch': {'mode': 'dry_captured', 'signed': False, 'broadcast': False}}]
        for m in script:
            await self.ws.send_text(json.dumps(m))
            await asyncio.sleep(0.12)


def start_server(port):
    import uvicorn
    brainrig.CONFIG['port'] = port
    cfg = uvicorn.Config(brainrig.app, host='127.0.0.1', port=port, log_level='warning', ws_max_size=16 * 1024 * 1024)
    server = uvicorn.Server(cfg)
    th = threading.Thread(target=server.run, name='test-server', daemon=True)
    th.start()
    t_end = time.time() + 20
    while not server.started and time.time() < t_end:
        time.sleep(0.1)
    if not server.started:
        raise SystemExit(f'the test server did not start on port {port}')
    return server, th


async def ws_start(url, origin, msg, ua=None):
    import websockets
    kw = {'origin': origin, 'proxy': None, 'open_timeout': 10}
    if ua is not None:
        kw['user_agent_header'] = ua
    async with websockets.connect(url, **kw) as ws:
        await ws.send(json.dumps(msg))
        first = None
        try:
            first = json.loads(await asyncio.wait_for(ws.recv(), 4))
        except asyncio.TimeoutError:
            pass
        if first and first.get('type') != 'refused':      # accepted: read to done
            t_end = time.time() + 20
            while time.time() < t_end:
                m = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if m.get('type') == 'done':
                    break
        return first


def test_ws(port):
    print(f'\n== (f) WS start gate on port {port} (brainrig app, LIVE CONFIG, FAKE Run)')
    url, origin = f'ws://127.0.0.1:{port}/run', f'http://127.0.0.1:{port}'
    tok = brainrig.CONFIG['token']
    brainrig.STATE['live_used'] = False
    FakeRun.started.clear()
    cases = [('no token', {'type': 'start', 'seed': 2026, 'confirm': 'RATTEST'}, BROWSER_UA, 'token'),
             ('a wrong token', {'type': 'start', 'seed': 2026, 'token': tok[:-2] + 'xx', 'confirm': 'RATTEST'}, BROWSER_UA, 'token'),
             ('no dialog confirm', {'type': 'start', 'seed': 2026, 'token': tok}, BROWSER_UA, 'confirmed'),
             ('a wrong confirm', {'type': 'start', 'seed': 2026, 'token': tok, 'confirm': 'RATBRAIN'}, BROWSER_UA, 'confirmed'),
             ('a script (python websockets UA)', {'type': 'start', 'seed': 2026, 'token': tok, 'confirm': 'RATTEST'}, None, 'browser'),
             ('a headless browser', {'type': 'start', 'seed': 2026, 'token': tok, 'confirm': 'RATTEST'},
              BROWSER_UA.replace('Chrome/', 'HeadlessChrome/'), 'browser')]
    for label, msg, ua, want in cases:
        first = asyncio.run(ws_start(url, origin, msg, ua))
        check(f'LIVE start refused: {label}', first and first.get('type') == 'refused' and want in first.get('msg', '')
              and not FakeRun.started, (first or {}).get('msg'))
    first = asyncio.run(ws_start(url, origin, {'type': 'start', 'seed': 2026, 'token': tok, 'confirm': 'RATTEST'},
                                 BROWSER_UA))
    check('LIVE start accepted only with token + confirm + a real browser', first and first.get('type') != 'refused'
          and len(FakeRun.started) == 1 and FakeRun.started[0]['ua'] == BROWSER_UA, first)
    first = asyncio.run(ws_start(url, origin, {'type': 'start', 'seed': 2026, 'token': tok, 'confirm': 'RATTEST'},
                                 BROWSER_UA))
    check('... and once only: the next start is refused (launch used)', first and first.get('type') == 'refused'
          and 'already used' in first.get('msg', '') and len(FakeRun.started) == 1, (first or {}).get('msg'))
    brainrig.STATE['live_used'] = False
    FakeRun.started.clear()


async def ws_open_then(url, origin, msg, then):
    """Start (accepted), read the first frame, then: 'close' the socket, or send 'stop' and close."""
    import websockets
    async with websockets.connect(url, origin=origin, proxy=None, open_timeout=10,
                                  user_agent_header=BROWSER_UA) as ws:
        await ws.send(json.dumps(msg))
        first = json.loads(await asyncio.wait_for(ws.recv(), 4))
        if then == 'stop':
            await ws.send(json.dumps({'type': 'stop'}))
            await asyncio.sleep(0.5)
    await asyncio.sleep(0.8)                      # let the server's handler run its finally
    return first


def test_abort(port):
    print(f'\n== (g) LIVE abort: STOP / closing the page before signing ends the run unsigned; after, it cannot')
    url, origin = f'ws://127.0.0.1:{port}/run', f'http://127.0.0.1:{port}'
    start = {'type': 'start', 'seed': 2026, 'token': brainrig.CONFIG['token'], 'confirm': 'RATTEST'}
    FakeRun.hang = True
    try:
        def idle():
            t = time.time() + 5
            while brainrig.STATE['busy'] and time.time() < t:
                time.sleep(0.05)
        for label, then in (('the viewer closes the page', 'close'), ('the viewer presses STOP', 'stop')):
            idle()
            brainrig.STATE['live_used'] = False
            FakeRun.hang_signed = False
            first = asyncio.run(ws_open_then(url, origin, start, then))
            r = FakeRun.last
            got = r.stop_fut.result() if r is not None and r.stop_fut.done() else None
            check(f'before signing, {label}: the run is aborted (nothing will be signed)',
                  first and first.get('type') != 'refused' and got in ('the viewer left', 'viewer'), f'{got} · first {first}')
        idle()
        brainrig.STATE['live_used'] = False
        FakeRun.hang_signed = True
        first = asyncio.run(ws_open_then(url, origin, start, 'close'))
        r = FakeRun.last
        check('after the tx reached the signer, closing the page does NOT stop the run (it must finish + record)',
              first and first.get('type') != 'refused' and r is not None and not r.stop_fut.done(), '')
        if r is not None and not r.stop_fut.done():
            r.stop_fut.get_loop().call_soon_threadsafe(r.stop_fut.set_result, 'test cleanup')
            time.sleep(0.5)
    finally:
        FakeRun.hang = FakeRun.hang_signed = False
        brainrig.STATE['live_used'] = False
        FakeRun.started.clear()


WS_SPY = r"""(() => {
  const W = window.WebSocket; window.__wsLog = [];
  function Spy(u, p) { const s = p ? new W(u, p) : new W(u); window.__wsLog.push({open: String(u)});
    const send = s.send.bind(s); s.send = d => { window.__wsLog.push({send: String(d)}); return send(d); }; return s; }
  Spy.prototype = W.prototype; Spy.CONNECTING = 0; Spy.OPEN = 1; Spy.CLOSING = 2; Spy.CLOSED = 3;
  window.WebSocket = Spy; })();"""


async def test_page(port, shots):
    from playwright.async_api import async_playwright
    print(f'\n== (f) the page (headless Chromium, http://localhost:{port}/, mocked LIVE /status)')
    base = f'http://localhost:{port}'
    tok = brainrig.CONFIG['token']
    shots.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            # ---- A: the default (headless) UA; ?autostart=1 must do nothing
            ctx = await browser.new_context(viewport={'width': 1920, 'height': 1080})
            await ctx.add_init_script(WS_SPY)
            page = await ctx.new_page()
            await page.goto(f'{base}/?autostart=1&token={tok}', wait_until='load')
            await page.wait_for_function("document.getElementById('mode').textContent.includes('LIVE')", timeout=15000)
            await page.wait_for_timeout(2500)
            st = await page.evaluate("""() => ({ws: window.__wsLog, mode: document.getElementById('mode').textContent,
                smode: document.getElementById('s-mode').textContent, wallet: document.getElementById('wallet').textContent,
                log: document.getElementById('log').innerText, search: location.search, live: document.body.classList.contains('live'),
                stored: sessionStorage.getItem('ratrigToken'), honesty: document.getElementById('honesty').innerText,
                dialog: !document.getElementById('confirm').hidden})""")
            check('LIVE page: ?autostart=1 ignored (no socket opened, no start sent), says so in the log',
                  st['ws'] == [] and 'AUTOSTART OFF' in st['log'] and not FakeRun.started and not st['dialog'],
                  st['ws'])
            check('LIVE wording: "LIVE · rat launch · signs once", strip "LIVE · SIGNS ONCE", real wallet, no DRY text',
                  st['mode'] == 'LIVE · rat launch · signs once' and st['smode'] == 'LIVE · SIGNS ONCE' and st['live']
                  and TEST_ACCT.address[:6] in st['wallet'] and 'DRY' not in st['mode'] + st['smode'] + st['honesty']
                  and 'signs it once' in st['honesty'], f"{st['mode']} | {st['smode']} | {st['wallet']}")
            check('the token is taken out of the address bar (kept for the tab)', 'token' not in st['search']
                  and st['stored'] == tok, st['search'])
            await page.screenshot(path=str(shots / 'brain_live_test_idle_1920.png'))
            await page.click('#start')
            await page.wait_for_selector('#confirm:not([hidden])', timeout=5000)
            d = await page.evaluate("""() => ({ws: window.__wsLog, lead: document.getElementById('cf-lead').innerText,
                title: document.getElementById('cf-title').innerText, facts: document.getElementById('cf-facts').innerText,
                startDisabled: document.getElementById('cf-start').disabled, warn: !document.getElementById('cf-warn').hidden})""")
            want = f'LIVE: the rat will launch $RATTEST on pons from {TEST_ACCT.address}; signs once; costs the 0.0005 ETH launch fee + gas. Start?'
            check('BEGIN SESSION opens the confirm dialog and sends NOTHING', d['ws'] == [] and not FakeRun.started
                  and d['lead'].strip() == want and 'RATTEST' in d['title'] and not d['startDisabled'] and not d['warn'],
                  d['lead'])
            await page.screenshot(path=str(shots / 'brain_live_test_confirm_1920.png'))
            await page.set_viewport_size({'width': 1280, 'height': 720})
            await page.wait_for_timeout(300)
            await page.screenshot(path=str(shots / 'brain_live_test_confirm_1280.png'))
            await page.set_viewport_size({'width': 1920, 'height': 1080})
            await page.click('#cf-cancel')
            await page.wait_for_timeout(300)
            c1 = await page.evaluate("() => ({ws: window.__wsLog, hidden: document.getElementById('confirm').hidden, log: document.getElementById('log').innerText})")
            await page.click('#start')
            await page.wait_for_selector('#confirm:not([hidden])', timeout=5000)
            await page.keyboard.press('Escape')
            await page.wait_for_timeout(300)
            c2 = await page.evaluate("() => ({ws: window.__wsLog, hidden: document.getElementById('confirm').hidden})")
            check('Cancel and Esc close the dialog and send nothing', c1['hidden'] and c2['hidden'] and c1['ws'] == []
                  and c2['ws'] == [] and 'CANCELLED' in c1['log'] and not FakeRun.started, '')
            await page.click('#start')
            await page.wait_for_selector('#confirm:not([hidden])', timeout=5000)
            await page.click('#cf-start')
            await page.wait_for_function("document.getElementById('log').innerText.includes('START REFUSED')", timeout=10000)
            await page.wait_for_timeout(500)
            s = await page.evaluate("""() => ({ws: window.__wsLog, log: document.getElementById('log').innerText,
                btn: document.getElementById('start').disabled, label: document.getElementById('start').textContent})""")
            sends = [json.loads(x['send']) for x in s['ws'] if 'send' in x]
            check('Start sends {type:start, seed, token, confirm}; the server refuses a HEADLESS browser; button re-enabled',
                  sends == [{'type': 'start', 'seed': 2026, 'token': tok, 'confirm': 'RATTEST'}] and not FakeRun.started
                  and 'browser' in s['log'] and not s['btn'] and s['label'] == 'BEGIN SESSION', sends)
            await ctx.close()

            # ---- B: a real browser's UA: the owner's click starts it (FAKE run) and the page shows the LIVE phases
            ctx = await browser.new_context(viewport={'width': 1920, 'height': 1080}, user_agent=BROWSER_UA)
            await ctx.add_init_script(WS_SPY)
            page = await ctx.new_page()
            await page.goto(f'{base}/?token={tok}', wait_until='load')
            await page.wait_for_function("document.getElementById('mode').textContent.includes('LIVE')", timeout=15000)
            await page.click('#start')
            await page.wait_for_selector('#confirm:not([hidden])', timeout=5000)
            await page.click('#cf-start')
            await page.wait_for_function('window.__rigDone === true', timeout=20000)
            await page.wait_for_timeout(600)
            s = await page.evaluate("""() => ({msg: window.__rigDoneMsg, log: document.getElementById('log').innerText,
                out: document.getElementById('t-out').innerText, outHref: (document.querySelector('#t-out a')||{}).href,
                links: [...document.querySelectorAll('#log a')].map(a => a.href),
                btn: document.getElementById('start').disabled, label: document.getElementById('start').textContent})""")
            m = s['msg'] or {}
            check('the owner\'s click (real-browser UA, token, confirm) starts it; FAKE run started once',
                  len(FakeRun.started) == 1 and FakeRun.started[0]['ua'] == BROWSER_UA, FakeRun.started)
            check('the page shows TX CHECKED / SIGNED / SENT / MINED with explorer + pons links',
                  all(k in s['log'] for k in ('TX CHECKED', 'TX SIGNED', 'TX SENT', 'TX MINED', 'LIVE RECORD'))
                  and f'{launcher.EXPLORER}/tx/0x' + '7a' * 32 in s['links']
                  and f'{launcher.EXPLORER}/token/{CHAIN.token}' in s['links']
                  and f'https://www.ponsfamily.com/launchpad/{CHAIN.token}' in s['links'], s['log'][-300:])
            check('outcome cell: LAUNCHED $RATTEST linking the coin on pons; START locked (one launch)',
                  s['out'] == 'LAUNCHED $RATTEST' and s['outHref'] == f'https://www.ponsfamily.com/launchpad/{CHAIN.token}'
                  and s['btn'] and s['label'] == 'LAUNCHED', f"{s['out']} {s['label']}")
            check('window.__rigDone / __rigDoneMsg carry the tx hash and the token', m.get('tx') == '0x' + '7a' * 32
                  and m.get('token') == CHAIN.token and m.get('mode') == 'LIVE', {k: m.get(k) for k in ('tx', 'token')})
            await page.screenshot(path=str(shots / 'brain_live_test_done_1920.png'))
            await ctx.close()
            brainrig.STATE['live_used'] = False
            FakeRun.started.clear()

            # ---- C: no token in the URL (and none stored in this new context): the dialog warns, Start is disabled
            ctx = await browser.new_context(viewport={'width': 1920, 'height': 1080}, user_agent=BROWSER_UA)
            await ctx.add_init_script(WS_SPY)
            page = await ctx.new_page()
            await page.goto(f'{base}/', wait_until='load')
            await page.wait_for_function("document.getElementById('mode').textContent.includes('LIVE')", timeout=15000)
            await page.click('#start')
            await page.wait_for_selector('#confirm:not([hidden])', timeout=5000)
            c = await page.evaluate("""() => ({ws: window.__wsLog, dis: document.getElementById('cf-start').disabled,
                warn: document.getElementById('cf-warn').innerText, log: document.getElementById('log').innerText})""")
            await page.click('#cf-start', force=True)
            await page.wait_for_timeout(500)
            c2 = await page.evaluate('() => window.__wsLog')
            check('without the token: NO TOKEN logged, the dialog warns, Start disabled, nothing sent',
                  c['dis'] and 'token' in c['warn'] and 'NO TOKEN' in c['log'] and c['ws'] == [] and c2 == []
                  and not FakeRun.started, c['warn'][:90])
            await ctx.close()

            # ---- D: the same page against a DRY rig: ?autostart=1 still starts it, exactly as before
            brainrig.CONFIG['mode'] = 'DRY'
            try:
                ctx = await browser.new_context(viewport={'width': 1920, 'height': 1080})
                await ctx.add_init_script(WS_SPY)
                page = await ctx.new_page()
                await page.goto(f'{base}/?autostart=1', wait_until='load')
                await page.wait_for_function('window.__rigDone === true', timeout=20000)
                s = await page.evaluate("""() => ({ws: window.__wsLog, mode: document.getElementById('mode').textContent,
                    smode: document.getElementById('s-mode').textContent, dialog: !document.getElementById('confirm').hidden,
                    out: document.getElementById('t-out').innerText, wallet: document.getElementById('wallet').textContent})""")
                sends = [x['send'] for x in s['ws'] if 'send' in x]
                check('DRY page unchanged: autostart sends exactly {"type":"start","seed":2026}, no dialog, DRY wording',
                      sends == ['{"type":"start","seed":2026}'] and not s['dialog'] and s['mode'] == 'DRY RUN'
                      and s['smode'] == 'DRY · NO SIGNING' and s['out'] == 'dry run' and 'throwaway' in s['wallet']
                      and len(FakeRun.started) == 1 and FakeRun.started[0]['mode'] == 'DRY', sends)
                await ctx.close()
            finally:
                brainrig.CONFIG['mode'] = 'LIVE'
                FakeRun.started.clear()
        finally:
            await browser.close()


def test_record(port):
    print('\n== (f) record.py never starts a LIVE brain-rig session')
    r = subprocess.run([sys.executable, str(LIVE_DIR / 'record.py'), '--port', str(port)], cwd=str(ROOT),
                       capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    check('record.py against a LIVE brain rig: refuses (exit 1), no browser, says only your click starts it',
          r.returncode == 1 and 'never starts a LIVE session' in out and 'opening' not in out and not FakeRun.started,
          out.strip().splitlines()[-1][:200] if out.strip() else r.returncode)
    r = subprocess.run([sys.executable, str(LIVE_DIR / 'record.py'), '--port', str(port), '--watch-live'], cwd=str(ROOT),
                       capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    check('record.py --watch-live without --token: refuses before any window opens', r.returncode == 1
          and 'needs --token' in out and 'opening' not in out, out.strip().splitlines()[-1][:200] if out.strip() else '')


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=4673)
    ap.add_argument('--no-browser', action='store_true')
    ap.add_argument('--shots', default=str(LIVE_DIR / 'dev_shots'))
    a = ap.parse_args()
    if not 4671 <= a.port <= 4679:
        raise SystemExit('use a port in 4671..4679 (4665 is the owner\'s rig)')
    real_journal_before = REAL_JOURNAL.exists()
    brainrig.Run_real = brainrig.Run
    try:
        test_dry()
        test_gates()
        test_launch()
        test_tampered()
        # the gates left CONFIG as the last successful LIVE startup; make sure it is LIVE for the WS/page tests
        FAKE['cfg'] = fake_cfg()
        clear_journal()
        CHAIN.reset()
        why, served = run_main(['--port', str(a.port), '--live', '--confirm', 'RATTEST'])
        if why or brainrig.CONFIG['mode'] != 'LIVE':
            raise SystemExit(f'could not set up the mocked LIVE CONFIG: {why}')
        brainrig.STATE['live_used'] = False
        brainrig.Run = FakeRun
        server, _ = start_server(a.port)
        try:
            test_ws(a.port)
            test_abort(a.port)
            if not a.no_browser:
                asyncio.run(test_page(a.port, Path(a.shots)))
            test_record(a.port)
        finally:
            server.should_exit = True
            time.sleep(1.0)
    except Exception:
        traceback.print_exc()
        check('the test ran to the end', False, 'exception (see above)')
    finally:
        brainrig.Run = brainrig.Run_real
    print('\n== sandbox')
    check('.env was never opened by this test process', not ENV_TOUCHES, ENV_TOUCHES[:1])
    check('no network left the test process (no RPC, no HTTP)', not NET_BLOCKED, NET_BLOCKED[:3])
    check('the real launch_journal.json was never created', REAL_JOURNAL.exists() == real_journal_before
          and not REAL_JOURNAL.exists(), str(REAL_JOURNAL))
    check('every signature used the throwaway test key only', not SIGN_VIOLATIONS,
          f'{len(SIGNS)} signature(s), all by the throwaway {TEST_ACCT.address}')
    shutil.rmtree(TMP, ignore_errors=True)
    n_bad = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f'\n{len(RESULTS) - n_bad}/{len(RESULTS)} checks passed')
    sys.exit(1 if n_bad else 0)


if __name__ == '__main__':
    main()
