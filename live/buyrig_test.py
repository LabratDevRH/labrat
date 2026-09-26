"""Tests of the buy rig (live/buyrig.py), its runner (live/buyrig_runner.py) and the engine's report endpoint for it
(live/buyback.py POST /pons_session), plus one REAL buy session on the real pons page (nothing is signed or sent).

    python live/buyrig_test.py                     # the mocked tests + the stream mask on a synthetic page (Chromium)
    python live/buyrig_test.py --no-browser        # the mocked tests only
    python live/buyrig_test.py --real-session      # + one real session: the rat buys 0.0001 ETH of LABRAT on pons,
                                                   #   streamed to a LOCAL relay (127.0.0.1:4771), recorded, replayed
    python live/buyrig_test.py --only-real [--frames-out DIR]

LIVE (switched off in the product; here with mocks only): every gate falls back to DRY, the key leaves the environment,
the page wallet is an address only, one transaction per window (journal + lock file) across restarts, 'reserved' before
signing and 'signed' (raw bytes) before the broadcast, a crash between signing and broadcasting (the identical bytes go
out on the next start, nothing is signed again), a signed buy that never landed (expired, its nonce reused), a nonce used
elsewhere (LIVE stops), tampered from / to / value / token / amount (refused, nothing signed, LIVE stops), a short balance
and the two-failures stop, the runner's LIVE batches (run once with --live --window, reported, verified by the engine,
retried on 503, recovered after a crash), and the key never in any output. The signer is buyback_test.MockSigner (an
address, no key), except in the tests that sign with a key made in the test itself (Account.create(), never funded)
against the in-memory fake chain: the real transaction format, and the key-leak scans.

BURNS (the maze's hourly burn, executed by this rig for live/burn.py; switched off in the product; mocks only, on a
BurnChain that adds the burnable token): the token method from the bytecode (burn(uint256) preferred, transfer to
dead otherwise), every tampered burn transaction refused, one burn journalled 'reserved' -> 'signed' before the
broadcast and verified by its receipt (a real burn shrinks the supply), one burn per window across restarts beside
the same hour's buy (two slots, one nonce account), a crash between signing and broadcasting (the identical bytes
again, nothing new signed, not even the buy), amounts over 5 % of the wallet's balance and tampered builds refused
with a stop, an expired burn and the two-failures stop, the receipt rules, every gate falling back to a NO-OP (no
chain read, nothing journalled, the key gone from the environment; also through the CLI), the child's whole path with
a key made here and the key in no output, and the runner (each booked burn once, reported, 503 retried, a crashed
child resolved, no-op children counted, a stopped LIVE skipping, only a live burn engine and valid rows, buys then
burns in turn on one nonce account, the child command and its environment).

NOTHING here reads .env, holds a funded key, signs or sends a transaction (buyback_test's traps):
  * opening any file named .env raises and is recorded; launcher.read_env_file / launcher.config are tripwires
  * LocalAccount.sign_transaction / unsafe_sign_hash / sign_message / sign_typed_data are tripwires: no account signs
  * the mocked tests' network is loopback only (the engine's status server on 127.0.0.1:4772); every chain call goes to
    buyback_test.FakeChain; the synthetic pons page is served by Playwright's router, nothing is fetched
  * --real-session reads the real chain (reads only: every method is recorded and checked) and loads the real pons
    page with a fresh in-memory key and the 1 ETH read override; pons's one eth_sendTransaction is captured, checked,
    simulated and answered 4001. It asserts that no send method reached any RPC and that nothing was signed.

What is covered: every field of pons's buy transaction (a tampered to / selector / command / action / pool key / hook /
zeroForOne / amounts / settle / take / min out / deadline / extra word / hook data / trailing bytes / from / extra
request fields / data vs input / review / simulation / quote is refused), the read-only chain access, that the rig
answers every signing request and every send with 4001 and routes no RPC send, the "forward a frame only between two
clean mask audits" rule, the mask itself in Chromium (every address, balance line and fee amount painted over or
hidden, nothing else, new text masked on arrival, a missing mask detected), the buy targets' measuring on pons's
shapes, the public messages (the fields site.js reads, no 0x string, no internal wording, the relay's types, PJPG
frames, a resync after a relay drop), the runner (history on first start, each batch once even across a crash or a
restart, only DRY / simulated engines, old batches and batches with a session skipped, only a passing replayed session
reported) and the engine's report endpoint (token, validation of every field, once per buy, the label on that buy in
the public status, kept across a restart, off in LIVE and without a token).
"""
import argparse
import asyncio
import base64
import hashlib
import inspect
import io
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

LIVE_DIR = Path(__file__).resolve().parent
ROOT = LIVE_DIR.parent
for _p in (str(ROOT), str(LIVE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import buyrig  # noqa: E402  (first: brainrig sets the thread stacks before any thread exists)
import brainrig  # noqa: E402
import buyback as bb  # noqa: E402
import buyback_test as bt  # noqa: E402  (the sandbox traps and the fake chain)
import buyrig_live as bl  # noqa: E402
import buyrig_runner as rn  # noqa: E402
import launcher  # noqa: E402
import ponsbot  # noqa: E402
from eth_abi import encode  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_account.signers.local import LocalAccount  # noqa: E402
from eth_utils import keccak, to_checksum_address  # noqa: E402

# the real signing function, saved before the traps replace it: used ONLY with a key a test creates itself
# (Account.create(), never funded, never the buyback wallet's), against the in-memory fake chain
_REAL_SIGN_TX = LocalAccount.sign_transaction

STATUS_PORT = int(os.environ.get('BUYRIG_TEST_STATUS_PORT') or 4772)
RELAY_PORT = int(os.environ.get('BUYRIG_TEST_RELAY_PORT') or 4771)
AMOUNT = '0.0001'
AMOUNT_WEI = 10 ** 14
PAGE_WALLET = to_checksum_address('0x' + 'ab' * 20)
OTHER = to_checksum_address('0x' + '5e' * 20)
GOOD_REVIEW = {'send': '0.0001 ETH', 'receive': '3.4913 LABRAT', 'market': 'Uniswap v4 pool', 'slippage': '1%'}
ADDR40 = re.compile(r'0x[0-9a-fA-F]{40}')
SITE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9 ()&'.,+-]{0,47}$")           # site.js name()
SITE_DEC = re.compile(r'^\d{1,15}(\.\d{1,18})?$')                         # site.js dec()
SEND_METHODS = ('eth_sendTransaction', 'eth_sendRawTransaction')
OPTS = {'browser': True}
EXTRA_TRAPS = {'signs': []}


def install_sign_traps():
    """On top of buyback_test's: no LocalAccount signs a message or typed data either."""
    from eth_account.signers.local import LocalAccount

    def trip(name):
        def f(*a, **k):
            EXTRA_TRAPS['signs'].append(name)
            raise AssertionError(f'buyrig_test: {name} must never be called')
        return f
    LocalAccount.sign_message = trip('LocalAccount.sign_message')
    LocalAccount.sign_typed_data = trip('LocalAccount.sign_typed_data')


# ---------------------------------------------------------------------------------------------------- tx builders
def build_data(amount=AMOUNT_WEI, min_out=0, deadline=0, *, key=None, zfo=True, extra=0, hook_data=b'', settle=None,
               take=None, actions=b'\x06\x0c\x0f', commands=b'\x10', more_inputs=(), more_params=(), selector=None,
               trailing=b''):
    """pons's router buy with any field changed (the defaults = buyback.cd_router_buy)."""
    key = key or bb.pool_key()
    swap = encode([f'({bb.POOL_KEY_T},bool,uint128,uint128,uint256,bytes)'],
                  [(key, zfo, int(amount), int(min_out), int(extra), hook_data)])
    s = encode(['address', 'uint256'], list(settle or (bb.ZERO, int(amount))))
    t = encode(['address', 'uint256'], list(take or (bb.TOKEN, int(min_out))))
    v4 = encode(['bytes', 'bytes[]'], [actions, [swap, s, t, *more_params]])
    body = encode(['bytes', 'bytes[]', 'uint256'], [commands, [v4, *more_inputs], int(deadline)])
    return (selector or bb.SEL['execute']) + body.hex() + trailing.hex()


class TxCase:
    """A good pons buy against a FakeChain, and the numbers it was built from."""

    def __init__(self, chain, now=None):
        self.chain = chain
        self.now = float(now if now is not None else time.time())
        self.quote = AMOUNT_WEI * chain.rate
        self.min_out = self.quote * 99 // 100            # pons: floor(quote x 99 / 100)
        self.deadline = int(self.now) + 1200             # pons: floor(now) + 1200 s

    def data(self, **kw):
        a = dict(amount=AMOUNT_WEI, min_out=self.min_out, deadline=self.deadline)
        a.update({k: kw.pop(k) for k in list(kw) if k in a})
        return build_data(a['amount'], a['min_out'], a['deadline'], **kw)

    def tx(self, **kw):
        tx = {'from': PAGE_WALLET, 'to': bb.ROUTER, 'value': hex(AMOUNT_WEI), 'data': self.data()}
        tx.update(kw)
        return tx

    def inspect(self, tx, review=GOOD_REVIEW, amount=AMOUNT_WEI, wallet=PAGE_WALLET):
        return buyrig.inspect_buy(tx, wallet, amount, review, bb.ReadRpc(self.chain), clock=lambda: self.now)


# ---------------------------------------------------------------------------------------------------- tests: the tx
class TestTxChecks(bt.Base):
    def setUp(self):
        super().setUp()
        self.c = TxCase(self.chain)

    def test_the_default_builder_is_pons_buy(self):
        self.assertEqual(self.c.data(), bb.cd_router_buy(AMOUNT_WEI, self.c.min_out, self.c.deadline))
        self.assertEqual(len(bytes.fromhex(self.c.data()[2:])), 1124)          # the research's 1124 bytes

    def test_a_good_buy_passes_every_check(self):
        res = self.c.inspect(self.c.tx())
        self.assertTrue(res['ok'], res['failed'])
        self.assertEqual(set(res['checks']), set(buyrig.PUBLIC_CHECKS))
        f, facts = res['fields'], res['facts']
        self.assertEqual((f['amount_in'], f['settle'], f['min_out'], f['deadline']),
                         (AMOUNT_WEI, AMOUNT_WEI, self.c.min_out, self.c.deadline))
        self.assertEqual(f['pool_id'], bb.POOL_ID)
        self.assertTrue(f['canonical'] and f['zero_for_one'])
        self.assertEqual(facts['quote'], self.c.quote)
        self.assertTrue(facts['sim']['ok'] and facts['sim_floor']['ok'])
        self.assertEqual(facts['gas'], 161_654)
        # the same with pons's hex spellings (value as hex, the chain id pons may add, input instead of data)
        tx = self.c.tx()
        tx['input'] = tx.pop('data')
        tx['chainId'] = hex(bb.CHAIN_ID)
        self.assertTrue(self.c.inspect(tx)['ok'])

    def test_every_tampered_field_is_refused(self):
        c, key = self.c, bb.pool_key()
        big = 2 * AMOUNT_WEI
        cases = [
            # (name, tx, review, expected failing checks (a subset of what fails))
            ('to: another contract', c.tx(to=bb.QUOTER), None, {'to', 'simulation'}),
            ('to: the token', c.tx(to=bb.TOKEN), None, {'to'}),
            ('to: an unknown address', c.tx(to=OTHER), None, {'to'}),
            ('to: missing', {k: v for k, v in c.tx().items() if k != 'to'}, None, {'to'}),
            ('selector: execute(bytes,bytes[])', c.tx(data='0x24856bc3' + c.data()[10:]), None,
             {'selector', 'swap_shape'}),
            ('commands: V4_SWAP + SWEEP', c.tx(data=c.data(commands=b'\x10\x04', more_inputs=[encode(
                ['address', 'address', 'uint256'], [bb.ZERO, OTHER, 0])])), None, {'swap_shape', 'calldata_exact'}),
            ('commands: WRAP_ETH', c.tx(data=c.data(commands=b'\x0b')), None, {'swap_shape'}),
            ('actions: an extra action', c.tx(data=c.data(actions=b'\x06\x0c\x0f\x0e', more_params=[encode(
                ['address', 'address', 'uint256'], [bb.TOKEN, OTHER, 0])])), None, {'swap_shape'}),
            ('actions: reordered', c.tx(data=c.data(actions=b'\x06\x0f\x0c')), None, {'swap_shape'}),
            ('pool key: fee 3000', c.tx(data=c.data(key=(key[0], key[1], 3000, key[3], key[4]))), None,
             {'swap_shape', 'pool'}),
            ('pool key: tick spacing 60', c.tx(data=c.data(key=(key[0], key[1], key[2], 60, key[4]))), None,
             {'swap_shape', 'pool'}),
            ('pool key: another hook', c.tx(data=c.data(key=(key[0], key[1], key[2], key[3], OTHER))), None,
             {'swap_shape', 'pool'}),
            ('pool key: another token', c.tx(data=c.data(key=(key[0], OTHER, key[2], key[3], key[4]))), None,
             {'swap_shape', 'pool'}),
            ('zeroForOne false (a sell)', c.tx(data=c.data(zfo=False)), None, {'swap_shape'}),
            ('extra uint256 not 0', c.tx(data=c.data(extra=1)), None, {'calldata_exact'}),
            ('hook data not empty', c.tx(data=c.data(hook_data=b'\x01\x02')), None, {'calldata_exact'}),
            ('trailing bytes', c.tx(data=c.data(trailing=b'\xde\xad\xbe\xef')), None, {'calldata_exact'}),
            ('value: twice amountIn', c.tx(value=hex(big)), None, {'value'}),
            ('value: 0', c.tx(value='0x0'), None, {'value'}),
            ('value: not a number', c.tx(value='lots'), None, {'value'}),
            ('amountIn = value = settle, not the batch', c.tx(value=hex(big), data=c.data(
                amount=big, min_out=big * self.chain.rate * 99 // 100)), None, {'amount'}),
            ('settle: another amount', c.tx(data=c.data(settle=(bb.ZERO, big))), None, {'value'}),
            ('settle: another currency', c.tx(data=c.data(settle=(bb.TOKEN, AMOUNT_WEI))), None, {'swap_shape'}),
            ('take: another currency', c.tx(data=c.data(take=(bb.ZERO, c.min_out))), None, {'swap_shape'}),
            ('take: another minimum', c.tx(data=c.data(take=(bb.TOKEN, 1))), None, {'swap_shape'}),
            ('min out 0', c.tx(data=c.data(min_out=0)), None, {'min_out'}),
            ('min out 90% of the quote', c.tx(data=c.data(min_out=c.quote * 90 // 100)), None, {'min_out'}),
            ('min out over the quote', c.tx(data=c.data(min_out=c.quote + 1)), None, {'min_out', 'simulation'}),
            ('deadline passed', c.tx(data=c.data(deadline=int(c.now) - 10)), None, {'deadline'}),
            ('deadline in 5 s', c.tx(data=c.data(deadline=int(c.now) + 5)), None, {'deadline'}),
            ('deadline in a day', c.tx(data=c.data(deadline=int(c.now) + 86400)), None, {'deadline'}),
            ('from: another address', c.tx(**{'from': OTHER}), None, {'from'}),
            ('request: gas', c.tx(gas=hex(300_000)), None, {'request_fields'}),
            ('request: nonce', c.tx(nonce='0x0'), None, {'request_fields'}),
            ('request: maxFeePerGas', c.tx(maxFeePerGas=hex(10 ** 9)), None, {'request_fields'}),
            ('request: another chain', c.tx(chainId='0x1'), None, {'request_fields'}),
            ('request: an unknown field', c.tx(foo='bar'), None, {'request_fields'}),
            ('request: data and input differ', c.tx(input=c.data(min_out=1)), None, {'request_fields'}),
            ('calldata not hex', c.tx(data='0x3593564czz'), None, {'selector', 'swap_shape'}),
            ('calldata empty', c.tx(data='0x'), None, {'selector', 'swap_shape'}),
            ("review: another amount", c.tx(), dict(GOOD_REVIEW, send='0.001 ETH'), {'review'}),
            ("review: another market", c.tx(), dict(GOOD_REVIEW, market='pons curve'), {'review'}),
            ("review: 5% slippage", c.tx(), dict(GOOD_REVIEW, slippage='5%'), {'review'}),
            ("review: missing", c.tx(), 'none', {'review'}),
        ]
        for name, tx, review, want in cases:
            with self.subTest(name):
                rv = None if review == 'none' else (review or GOOD_REVIEW)
                res = self.c.inspect(tx, review=rv)
                self.assertFalse(res['ok'], f'{name}: accepted')
                self.assertTrue(want <= set(res['failed']), f"{name}: failed {res['failed']}, expected {want}")

    def test_a_reverting_simulation_or_a_missing_quote_is_refused(self):
        self.chain.balance = 0                        # eth_call: insufficient funds for the exact transaction
        res = self.c.inspect(self.c.tx())
        self.assertEqual(res['failed'], ['simulation'])
        self.chain.balance = 10 ** 18
        self.chain.graduated = False                  # the quoter and the pool revert (no pool)
        res = self.c.inspect(self.c.tx())
        self.assertIn('min_out', res['failed'])
        self.assertIn('simulation', res['failed'])

    def test_the_checks_only_read(self):
        for tx in (self.c.tx(), self.c.tx(to=OTHER), self.c.tx(data=self.c.data(extra=1))):
            self.c.inspect(tx)
        methods = {m for m, _t, _s in self.chain.log}
        self.assertTrue(methods <= bb.READ_METHODS, methods)
        rpc = bb.ReadRpc(self.chain)
        for m in SEND_METHODS:
            with self.assertRaises(bb.SendRefused):
                rpc.raw(m, [{}])
        # the signing code lives only in live/buyrig_live.py (switched off by default); buyrig.py has none of it
        src = (LIVE_DIR / 'buyrig.py').read_text(encoding='utf-8')
        for bad in ('sign_transaction', 'send_raw', 'LiveRpc', 'LiveLaunch', 'read_env_file', 'launcher.config(',
                    'unsafe_sign_hash', 'from_key', 'os.environ.get(buyrig_live.ENV_KEY'):
            self.assertNotIn(bad, src, f'buyrig.py mentions {bad}')
        live_src = (LIVE_DIR / 'buyrig_live.py').read_text(encoding='utf-8')
        for bad in ('read_env_file', 'launcher.config(', 'unsafe_sign_hash', 'sign_message', 'sign_typed_data'):
            self.assertNotIn(bad, live_src, f'buyrig_live.py mentions {bad}')


# ---------------------------------------------------------------------------------------------------- tests: signing
class StubOut:
    """brainrig.Outbox's surface, recording the events."""

    def __init__(self):
        self.events = []
        self.n = {'json': 0, 'shot': 0, 'frame': 0, 'shot_dropped': 0}
        self.shots_pending = 0
        self.shot_times = []
        self.shot_idle = None

    def json(self, msg, rec=None):
        self.events.append(msg)

    def shot_b64(self, b64, w, h):
        self.n['shot'] += 1
        self.shot_times.append(time.time())

    def frame_ts(self, b):
        self.n['frame'] += 1


class FakeLink:
    def __init__(self):
        self.msgs, self.frames = [], []

    def _t(self, m):
        buyrig.public_json(m)                  # the same tripwire PonsLink applies
        self.msgs.append(m)
        return True

    hello = step = tx = done = bye = _t

    def frame(self, jpg):
        self.frames.append(bytes(jpg))
        return True


def new_run(tmp, chain, link=None, amount=AMOUNT):
    """A BuyRun inside a running loop, with no browser: bot / out stubs (for the wallet and message tests)."""
    buyrig.configure(7)
    r = buyrig.BuyRun(amount, 7, link=link, rpc=bb.ReadRpc(chain), out_root=tmp)
    r.out = StubOut()
    r.out.shot_idle = asyncio.Event()
    r.out.shot_idle.set()
    r.bot = SimpleNamespace(armed=False, address=PAGE_WALLET, page=None, signatures=[], refusals=[],
                            arm=lambda: setattr(r.bot, 'armed', True))
    r.commit = hashlib.sha256(b'brain').hexdigest()
    return r


class TestNeverSigns(bt.Base):
    def test_the_wallet_refuses_every_signature(self):
        bot = buyrig.BuyBot(ponsbot.throwaway_account())

        async def go():
            typed = json.dumps({'types': {'EIP712Domain': []}, 'primaryType': 'Permit', 'domain': {}, 'message': {}})
            return [await bot._eth_sign('personal_sign', ['0x68656c6c6f', bot.address]),
                    await bot._eth_sign('eth_sign', [bot.address, '0x' + '11' * 32]),
                    await bot._eth_sign_typed('eth_signTypedData_v4', [bot.address, typed]),
                    await bot._eth_sign_typed('eth_signTypedData', [bot.address, typed])]
        for r in asyncio.run(go()):
            self.assertEqual(r, {'error': {'code': 4001, 'message': buyrig.REFUSAL_MSG}})
        self.assertEqual(len(bot.signatures), 4)
        self.assertTrue(all(s['refused'] for s in bot.signatures))
        self.assertNotIn('DRY', buyrig.REFUSAL_MSG)        # pons repeats it in its toast
        self.assertEqual(EXTRA_TRAPS['signs'], [])

    def test_no_rpc_send_leaves_the_browser(self):
        bot = buyrig.BuyBot(ponsbot.throwaway_account())
        done = []

        class Route:
            def __init__(self, body, method='POST'):
                self.request = SimpleNamespace(method=method, post_data=json.dumps(body))

            async def abort(self):
                done.append('abort')

            async def continue_(self):
                done.append('continue')

        async def go():
            await bot._route_rpc(Route({'jsonrpc': '2.0', 'id': 1, 'method': 'eth_sendRawTransaction',
                                        'params': ['0x02f8']}))
            await bot._route_rpc(Route([{'id': 1, 'method': 'eth_chainId'},
                                        {'id': 2, 'method': 'eth_sendTransaction', 'params': [{}]}]))
            await bot._route_rpc(Route({'id': 3, 'method': 'eth_chainId'}))
            await bot._route_rpc(Route(None, method='GET'))
        asyncio.run(go())
        self.assertEqual(done, ['abort', 'abort', 'continue', 'continue'])
        self.assertEqual(bot.rpc_sends_blocked, 2)

    def test_pons_send_is_checked_simulated_and_refused(self):
        c = TxCase(self.chain)
        old = buyrig.TOAST_WAIT_S
        buyrig.TOAST_WAIT_S = 0.2

        async def go():
            r = new_run(self.tmp, self.chain)
            r.review = GOOD_REVIEW
            before = await r.on_send(c.tx())                       # before the rat clicked Confirm
            r.bot.arm()
            first = await r.on_send(c.tx())
            if r.rejection_task:
                await r.rejection_task
            again = await r.on_send(c.tx())                        # one buy request per session
            return r, before, first, again
        try:
            r, before, first, again = asyncio.run(go())
        finally:
            buyrig.TOAST_WAIT_S = old
        want = {'error': {'code': 4001, 'message': buyrig.REFUSAL_MSG}}
        self.assertEqual((before, first, again), (want, want, want))
        self.assertEqual(len(r.sends), 3)
        self.assertEqual(r.handled_send, 1)
        self.assertTrue(r.inspection['ok'], r.inspection['failed'])
        self.assertEqual(r.capture['verdict'], 'dry_captured')
        self.assertFalse(r.capture['signed'] or r.capture['broadcast'])
        self.assertEqual(r.capture['response_to_page'], want)
        out = r._outcome()
        self.assertEqual((out['signed'], out['broadcast'], out['simulation']), (False, False, 'ok'))
        self.assertEqual(out['labrat_out'], bb.token_str(c.quote))

    def test_a_tampered_send_is_refused_the_same_way(self):
        c = TxCase(self.chain)
        old = buyrig.TOAST_WAIT_S
        buyrig.TOAST_WAIT_S = 0.1

        async def go():
            r = new_run(self.tmp, self.chain)
            r.review = GOOD_REVIEW
            r.bot.arm()
            resp = await r.on_send(c.tx(value=hex(5 * AMOUNT_WEI)))
            if r.rejection_task:
                await r.rejection_task
            return r, resp
        try:
            r, resp = asyncio.run(go())
        finally:
            buyrig.TOAST_WAIT_S = old
        self.assertEqual(resp['error']['code'], 4001)
        self.assertEqual(r.capture['verdict'], 'dry_refused_mismatch')
        self.assertIn('value', r.inspection['failed'])

    def test_the_rig_is_dry_whatever_the_config_says(self):
        async def go():
            r = new_run(self.tmp, self.chain)
            brainrig.CONFIG['mode'] = 'LIVE'
            r2 = buyrig.BuyRun(AMOUNT, 1, rpc=bb.ReadRpc(self.chain), out_root=self.tmp)
            return r, r2
        r, r2 = asyncio.run(go())
        for x in (r, r2):
            self.assertEqual((x.mode, x.live_mode), ('DRY', False))
        self.assertIsInstance(r.bot, SimpleNamespace)
        self.assertFalse(issubclass(buyrig.BuyBot, ponsbot.LiveLaunch))
        # the hard per-buy ceiling is 0.1 ETH (buyback.HARD, the owner's budget of 2026-09-25)
        for bad in ('0', '-0.0001', '0.000001', '0.2', '0.10000001', 'abc', 'NaN', '0.000012345'):
            with self.assertRaises(ValueError):
                buyrig.parse_amount(bad)
        self.assertEqual(buyrig.parse_amount('0.00047'), ('0.00047', 47 * 10 ** 13))
        self.assertEqual(buyrig.parse_amount('0.1'), ('0.1', 10 ** 17))


# ---------------------------------------------------------------------------------------------------- tests: the mask
class TestMaskRules(unittest.TestCase):
    def test_the_mask_script_and_css(self):
        m = buyrig.MASK_JS
        for part in ('::highlight(ratmask){color:transparent;background-color:#3a3a42',
                     '#pons-v2-panel-holders,.toast-description{visibility:hidden!important}',
                     'button[aria-label="Profile"],.token-creator-fees-amount,.toast-description',
                     'CSS.highlights.set("ratmask", hl)', 'new MutationObserver',
                     'if (window.top !== window) return;',
                     'if (location.origin !== "https://www.ponsfamily.com") return;',
                     '/available/i.test(s) && /\\d/.test(s)', 'window.__ratMaskAudit'):
            self.assertIn(part, m)
        self.assertIn("css.indexOf('::highlight(ratmask){color:transparent') >= 0", buyrig.AUDIT_JS)
        run_src = inspect.getsource(buyrig.BuyRun._run)
        i_mask = run_src.index('add_init_script(MASK_JS)')
        self.assertLess(i_mask, run_src.index('await self.bot.goto()'), 'the mask must be installed before pons loads')
        self.assertLess(i_mask, run_src.index('await self.start_screencast()'))

    def test_audit_verdicts(self):
        clean = {'origin_ok': True, 'style_ok': True, 'ready': True, 'hasHL': True, 'n_unmasked': 0,
                 'n_elements_unmasked': 0}
        self.assertTrue(buyrig.audit_clean(clean))
        self.assertFalse(buyrig.audit_breach(clean))
        for k, v in (('n_unmasked', 1), ('n_elements_unmasked', 2), ('hasHL', False), ('style_ok', False)):
            a = dict(clean, **{k: v})
            self.assertFalse(buyrig.audit_clean(a), k)
            self.assertTrue(buyrig.audit_breach(a), k)
        for k, v in (('origin_ok', False), ('ready', False)):          # not auditable yet: withheld, not a breach
            a = dict(clean, **{k: v})
            self.assertFalse(buyrig.audit_clean(a))
            self.assertFalse(buyrig.audit_breach(a))
        self.assertFalse(buyrig.audit_clean(None))

    def test_a_frame_is_forwarded_only_between_two_clean_audits(self):
        clean = {'origin_ok': True, 'style_ok': True, 'ready': True, 'hasHL': True, 'n_unmasked': 0,
                 'n_elements_unmasked': 0}
        loading = dict(clean, ready=False)
        breach = dict(clean, n_unmasked=1, unmasked=[{'text': '0x<hex>'}])
        seq = [clean, clean, loading, clean, clean, clean, breach, clean, clean]
        jpg = b'\xff\xd8' + b'x' * 64 + b'\xff\xd9'
        tmp = tempfile.mkdtemp(prefix='buyrig_test_')

        async def go():
            link = FakeLink()
            r = new_run(tmp, bt.FakeChain(), link=link)
            r.streaming = True
            audits = iter(seq)

            async def audit():
                r.audits['runs'] += 1
                return next(audits)
            r._audit = audit
            got = []
            for _ in seq:
                r._audit_last = 0.0
                n0 = len(link.frames)
                await r._frame({'data': base64.b64encode(jpg).decode(), 'sessionId': 1})
                got.append(len(link.frames) > n0)
            return r, got
        try:
            r, got = asyncio.run(go())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        # frame 1: no clean audit before it; 3: not auditable; 4: the one before was not clean; 7: a breach
        # (and nothing after a breach, even with clean audits)
        self.assertEqual(got, [False, True, False, False, True, True, False, False, False])
        self.assertIsNotNone(r.breach)
        self.assertTrue(r.stop_fut.done())
        self.assertEqual(r.audits['breaches'], 1)


SYNTH_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>pons</title><style>
body{margin:0;background:#0f0f12;color:#e8e8ea;font:15px/1.4 Arial,sans-serif}
header{position:sticky;top:0;height:48px;background:rgba(15,15,18,.85);display:flex;gap:24px;align-items:center;
  padding:0 24px}
button{font:inherit;color:inherit;background:#26262c;border:1px solid #333;border-radius:8px;padding:6px 10px}
.left{position:absolute;left:40px;top:80px;width:320px}.mid{position:absolute;left:400px;top:80px;width:420px}
.convert-amount-field{position:relative;width:272px;height:39px;border:1px solid #444;border-radius:10px;margin:8px 0}
.convert-amount-field .shown{position:absolute;left:12px;top:9px}
.convert-amount-input{position:absolute;left:0;top:0;width:100%;height:100%;opacity:.01;border:0;padding:0;margin:0}
.ui-btn{display:block;width:310px;height:56px;margin-top:12px;border-radius:12px}
[role=dialog]{position:fixed;left:860px;top:80px;width:380px;height:420px;background:#1b1b20;border:1px solid #444;
  border-radius:14px;padding:16px;box-sizing:border-box}
dl{margin:0}dt{color:#999;margin-top:8px}dd{margin:0}
.toast-viewport{position:fixed;left:40px;bottom:16px;width:340px}
.toast-item{background:#2a1a1a;border-radius:10px;padding:10px}
p{margin:6px 0}
</style></head><body>
<header><a href="/launchpad" data-ns>Back to explore</a><a href="https://explorer.example/" data-ns>Explorer</a>
<button aria-label="Profile" data-s><svg width="12" height="12" aria-hidden="true"></svg><span>0xAbC1</span><span>…</span><span>9f3E</span></button></header>
<div class="left">
  <p data-ns>Buy LABRAT with ETH</p>
  <div class="convert-amount-field"><span class="shown" data-ns>0</span><input aria-label="Amount of ETH to spend" class="convert-amount-input is-hitbox" readonly></div>
  <button aria-label="Pay with ETH" data-ns>ETH</button>
  <p class="convert-balance" data-s><span class="roll"><span>1</span></span> available<button class="convert-max">Max</button></p>
  <p class="convert-balance" data-s>0 available</p>
  <p class="token-buy-route" data-ns>Routed through the pons pool, 0.15% better</p>
  <button class="ui-btn ui-btn-primary" disabled>Buy LABRAT</button>
</div>
<div class="mid">
  <section><h3 data-ns>About</h3><p><span data-ns>Creator</span> <a href="/profile/0x4C2661717b97cd23aa87fe29fe0c50cff2cbb893" data-s>0x4C26…b893</a></p>
    <button aria-label="Copy contract address" data-s>0xaCa0…680d</button></section>
  <section><h3>Creator fees</h3><div class="token-creator-fees-amount" data-s>4.460506 ETH</div>
    <p>Claimable now <span data-s>0.25 ETH</span> paid to <span data-s>0x4C26…b893</span></p></section>
  <section><div>Holder fee sharing</div><p>Fees go to <span data-s>0x4C2661717b97cd23aa87fe29fe0c50cff2cbb893</span> holders earn <span data-s>1.2 ETH</span></p></section>
  <table><tr><td><a href="/profile/0x1111111111111111111111111111111111111111" title="0x1111111111111111111111111111111111111111" data-s>0x1111…1111</a></td><td data-ns>0.0001 ETH</td></tr></table>
  <p id="split"><span data-ns>Wallet</span> <span data-s>0x6505</span><span data-s>…</span><span data-s>40dc</span></p>
  <div id="pons-v2-panel-holders" data-h><p>0x2222…2222 12,345 LABRAT</p></div>
</div>
<div role="dialog" aria-modal="true" aria-labelledby="_R_edb_"><h2 id="_R_edb_" data-ns>Review buy</h2><dl>
  <dt>You send</dt><dd>0.0001 ETH</dd><dt>You receive</dt><dd>927.1565 LABRAT</dd><dt>Market</dt><dd>Uniswap v4 pool</dd>
  <dt>Max slippage</dt><dd>1%</dd></dl><button>Confirm buy</button> <button>Cancel</button></div>
<div class="toast-viewport" aria-label="Notifications"><div class="toast-item" role="status"><p class="toast-title" data-ns>Trade did not settle</p>
  <p class="toast-description" data-h>From 0x3333333333333333333333333333333333333333 data 0x3593564c0000</p></div></div>
</body></html>"""

CHECK_JS = r"""() => {
  const hl = CSS.highlights.get('ratmask'); const ranges = hl ? [...hl] : [];
  const covered = t => ranges.some(r => r.intersectsNode(t));
  const hidden = e => getComputedStyle(e).visibility === 'hidden';
  const texts = sel => { const out = []; for (const el of document.querySelectorAll(sel)) {
    const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT); let t;
    while ((t = w.nextNode())) if ((t.nodeValue || '').trim()) out.push(t); } return out; };
  const out = {sensitive: 0, uncovered: [], over: [], shown_hidden: [], rects: []};
  for (const t of texts('[data-s]')) {
    if (t.parentElement.closest('.convert-max')) continue;
    out.sensitive++;
    if (!covered(t) && !hidden(t.parentElement)) out.uncovered.push(t.nodeValue.trim().slice(0, 40));
    const r = document.createRange(); r.selectNodeContents(t);
    for (const q of r.getClientRects()) if (q.width > 3 && q.height > 3 && q.bottom > 0 && q.top < innerHeight)
      out.rects.push([q.left, q.top, q.width, q.height]);
  }
  for (const t of texts('[data-ns]')) if (covered(t)) out.over.push(t.nodeValue.trim().slice(0, 40));
  for (const e of document.querySelectorAll('[data-h]')) if (!hidden(e)) out.shown_hidden.push(e.id || e.className);
  return out; }"""


def browser_ok():
    if not OPTS['browser']:
        return False
    try:
        import playwright.async_api  # noqa: F401
        return True
    except ImportError:
        return False


class TestMaskInChromium(unittest.TestCase):
    """The mask on a synthetic page shaped like pons's coin page (served by Playwright's router: nothing is fetched)."""

    def setUp(self):
        if not browser_ok():
            self.skipTest('no browser (--no-browser, or Playwright missing)')

    def test_the_mask_on_a_pons_shaped_page(self):
        res = asyncio.run(self._run())
        a, chk, dyn, broken, style_gone, m, other = (res[k] for k in ('audit', 'check', 'dynamic', 'broken',
                                                                         'style_gone', 'measure', 'other_origin'))
        self.assertTrue(buyrig.audit_clean(a), a)
        self.assertGreaterEqual(a['masked'], 8)
        self.assertGreaterEqual(chk['sensitive'], 14)
        self.assertEqual(chk['uncovered'], [])
        self.assertEqual(chk['over'], [], 'text that is not an address or a balance was masked')
        self.assertEqual(chk['shown_hidden'], [])
        # the pixels: every visible sensitive text box is one flat colour (the mask's #3a3a42), no glyph shows
        from PIL import Image
        im = Image.open(io.BytesIO(res['png'])).convert('RGB')
        flat = 0
        for x, y, w, h in chk['rects']:
            box = (int(x) + 1, int(y) + 1, int(x + w) - 1, int(y + h) - 1)
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            px = list(im.crop(box).getdata())
            share = sum(1 for p in px if p == (58, 58, 66)) / len(px)
            self.assertGreaterEqual(share, 0.98, f'masked text box {box}: only {share:.0%} mask colour')
            flat += 1
        self.assertGreaterEqual(flat, 10)
        # text that appears later (a new holder line, a changed chip, a rolling digit) is masked when it appears
        self.assertTrue(buyrig.audit_clean(dyn['audit']), dyn['audit'])
        self.assertEqual(dyn['check']['uncovered'], [])
        # its style comes back if the page removes it; a mask that stops painting is caught (a breach)
        self.assertTrue(buyrig.audit_clean(res['style_back']), res['style_back'])
        self.assertTrue(buyrig.audit_breach(broken), broken)
        self.assertTrue(buyrig.audit_breach(style_gone), style_gone)
        # the buy targets measured on pons's shapes
        amt, buy, conf = m['amount'], m['buy'], m['confirm']
        self.assertTrue(amt['ready'], amt)
        self.assertTrue(amt['buyMode'] and amt['input_readonly'])
        self.assertEqual(buy['why'], 'disabled')
        self.assertTrue(conf['ready'], conf)
        self.assertEqual(conf['review'], {'send': '0.0001 ETH', 'receive': '927.1565 LABRAT',
                                          'market': 'Uniswap v4 pool', 'slippage': '1%'})
        self.assertTrue(buyrig.review_ok(conf['review'], AMOUNT_WEI))
        self.assertTrue(conf['fixed'])
        # the mask runs only on pons (another origin: nothing installed)
        self.assertFalse(other)

    async def _run(self):
        from playwright.async_api import async_playwright
        out = {}
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                ctx = await browser.new_context(viewport={'width': 1280, 'height': 900}, device_scale_factor=1,
                                                color_scheme='dark')

                async def router(route):
                    u = route.request.url
                    if u.startswith(('https://www.ponsfamily.com/', 'https://other.example/')):
                        await route.fulfill(status=200, content_type='text/html', body=SYNTH_PAGE)
                    else:
                        await route.abort()
                await ctx.route('**/*', router)
                page = await ctx.new_page()
                await page.add_init_script(buyrig.MASK_JS)
                await page.goto(f'https://www.ponsfamily.com/launchpad/{bb.TOKEN}', wait_until='load')
                await page.wait_for_timeout(300)
                out['audit'] = await page.evaluate(buyrig.AUDIT_JS)
                out['check'] = await page.evaluate(CHECK_JS)
                out['png'] = await page.screenshot(type='png')
                out['measure'] = {
                    'amount': await page.evaluate(buyrig.BUY_MEASURE_JS, ['amount', AMOUNT]),
                    'buy': await page.evaluate(buyrig.BUY_MEASURE_JS, ['buy', AMOUNT]),
                    'confirm': await page.evaluate(buyrig.BUY_MEASURE_JS, ['confirm_buy', AMOUNT])}
                await page.evaluate("""() => {
                  const p = document.createElement('p');
                  p.innerHTML = 'New holder <span data-s>0x9999999999999999999999999999999999999999</span>';
                  document.querySelector('.mid').appendChild(p);
                  document.querySelector('button[aria-label="Profile"] span').firstChild.nodeValue = '0xFfFf';
                  document.querySelector('.roll span').firstChild.nodeValue = '7';
                  const q = document.createElement('p');
                  q.innerHTML = 'Split <b data-s>0x7777</b><i data-s>…</i><b data-s>ab12</b>';
                  document.querySelector('.mid').appendChild(q); }""")
                await page.evaluate('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')
                out['dynamic'] = {'audit': await page.evaluate(buyrig.AUDIT_JS), 'check': await page.evaluate(CHECK_JS)}
                # the mask puts its style back when pons's page removes it
                await page.evaluate("() => document.getElementById('__ratmaskstyle').remove()")
                out['style_back'] = await page.evaluate(buyrig.AUDIT_JS)
                await page.evaluate("() => { CSS.highlights.delete('ratmask'); }")
                out['broken'] = await page.evaluate(buyrig.AUDIT_JS)
                await page.reload(wait_until='load')
                await page.wait_for_timeout(200)
                await page.evaluate("() => { document.getElementById('__ratmaskstyle').textContent = ''; }")
                out['style_gone'] = await page.evaluate(buyrig.AUDIT_JS)
                p2 = await ctx.new_page()
                await p2.add_init_script(buyrig.MASK_JS)
                await p2.goto('https://other.example/', wait_until='load')
                out['other_origin'] = await p2.evaluate('() => !!window.__ratMask')
            finally:
                await browser.close()
        return out


# ---------------------------------------------------------------------------------------------------- tests: stream
class TestPublicStream(bt.Base):
    def test_public_json_tripwire(self):
        ok = {'type': 'pons_step', 'target': 'Buy LABRAT', 'phase': 'press'}
        self.assertTrue(buyrig.public_json(ok))
        for bad in ({'x': 'wallet 0x12ab'}, {'x': 'wallet 0X12AB'}, {'x': 'a dry session'}, {'x': 'Test run'},
                    {'x': 'a throwaway key'}, {'x': 'mock'}, {'x': 'dev'}, {'x': 'y' * 5000}):
            with self.assertRaises(ValueError, msg=str(bad)[:40]):
                buyrig.public_json(bad)

    def test_messages_carry_what_the_site_reads_and_nothing_else(self):
        c = TxCase(self.chain)
        tmp = self.tmp

        async def go():
            link = FakeLink()
            r = new_run(tmp, self.chain, link=link)
            hello = r._hello_msg()
            tgs = {t.key: t for t in r.targets}
            r.stage(tgs['b01_terms_tou'], 'active', 'terms checkbox: Terms of Use')
            tgs['b04_amount'].lights.append({'box': [68.0, 316.0, 270.0, 37.0]})
            r.lit = tgs['b04_amount']
            r.stage(tgs['b04_amount'], 'active', 'lit 270x37 px')
            await asyncio.sleep(buyrig.AIM_AFTER_S + 0.2)
            r.cur = tgs['b04_amount']
            r._report_click(10, 0.1, 0.9, False, False, 'b04_amount', 'outside the lit target')
            r._report_click(11, 0.1, 0.9, False, False, 'b04_amount', 'outside the lit target')   # rate-limited
            r.stage(tgs['b04_amount'], 'hit', 'at (200, 330)')
            r._pub_step(tgs['b04_amount'], 'type')
            r.stage(tgs['b06_confirm'], 'done', 'pons requested the buy transaction')
            r.review = GOOD_REVIEW
            r.inspection = c.inspect(c.tx())
            tx = r._tx_msg()
            r.result = {'ok': True}
            r.proof = 'ab' * 32
            done = r._done_msg()
            r.result = {'ok': False}
            r.inspection = None
            r.fail_reason = 'quote_failed'
            done_q = r._done_msg()
            r.terms_shown = False
            hello2 = r._hello_msg()
            return link, hello, tx, done, done_q, hello2, r
        link, hello, tx, done, done_q, hello2, r = asyncio.run(go())
        for m in [hello, tx, done, done_q, hello2] + link.msgs:
            s = buyrig.public_json(m)
            self.assertIsNone(ADDR40.search(s))
            self.assertNotRegex(s, r'0x[0-9a-fA-F]{3,}')
        # pons_hello: the relay's source, "Simulated", short target names in order, the amount
        self.assertEqual((hello['type'], hello['source'], hello['label'], hello['simulated']),
                         ('pons_hello', 'buyrig', 'Simulated', True))
        self.assertNotIn('test', hello)
        self.assertEqual(hello['targets'], ['Terms of Use', 'Privacy Policy', 'Accept and continue', 'ETH amount',
                                            'Buy LABRAT', 'Confirm buy'])
        self.assertTrue(all(SITE_NAME.match(t) and not re.search(r'available|balance', t, re.I)
                            for t in hello['targets']))
        self.assertEqual(hello2['targets'], ['ETH amount', 'Buy LABRAT', 'Confirm buy'])
        self.assertEqual(hello2['started'], hello['started'])
        self.assertTrue(SITE_DEC.match(hello['amount_eth']))
        # pons_step: i / n, the short name, a phase keyword
        steps = [m for m in link.msgs if m['type'] == 'pons_step']
        phases = [m['phase'] for m in steps]
        self.assertEqual(phases, ['', 'light', 'aim', 'miss', 'press', 'type', 'check'])
        for m in steps:
            self.assertIn(m['phase'], ('',) + buyrig.PHASES)
            self.assertTrue(SITE_NAME.match(m['target']))
            self.assertTrue(1 <= m['i'] <= m['n'] == 6)
        self.assertEqual(steps[1]['box'], [68.0, 316.0, 270.0, 37.0])
        # pons_result: ok, eth_in, labrat_out (decimals), reasons from the site's list
        self.assertEqual((tx['type'], tx['kind'], tx['ok'], tx['reason']), ('pons_result', 'tx', True, None))
        self.assertEqual(tx['eth_in'], AMOUNT)
        self.assertTrue(SITE_DEC.match(tx['labrat_out']))
        self.assertEqual(tx['checks_passed'], tx['checks_total'])
        self.assertFalse(tx['signed'] or tx['sent'])
        self.assertEqual((done['kind'], done['ok'], done['reason']), ('done', True, None))
        self.assertEqual((done_q['ok'], done_q['reason']), (False, 'quote_failed'))
        self.assertIn(done_q['reason'], buyrig.REASONS)

    def test_the_relay_speaks_the_same_protocol(self):
        sys.path.insert(0, str(ROOT / 'relay'))
        try:
            import relay
        finally:
            sys.path.remove(str(ROOT / 'relay'))
        self.assertTrue(set(buyrig.MSG.values()) <= set(relay.PONS_TYPES))
        self.assertEqual(relay.PONS_SOURCE, buyrig.SOURCE)
        self.assertEqual(relay.PONS_MAGIC, buyrig.FRAME_PREFIX)
        self.assertLessEqual(buyrig.PUBLIC_TEXT_MAX, relay.PONS_MAX_TEXT)
        self.assertLessEqual(buyrig.RELAY_MAX_FRAME + 4, relay.PONS_MAX_BINARY)

    def test_the_link_prefixes_frames_and_resyncs_after_a_drop(self):
        sent, conns = [], []

        class Conn:
            def __init__(self, fail_after=None):
                self.n, self.fail_after = 0, fail_after

            def send(self, p):
                if self.fail_after is not None and self.n >= self.fail_after:
                    raise ConnectionError('dropped')
                self.n += 1
                sent.append((len(conns), p))

            def close(self):
                pass

        def connect(url, token):
            self.assertTrue(url.endswith('?channel=pons'))
            self.assertEqual(token, 'tok' * 8)
            conns.append(1)
            return Conn(fail_after=3 if len(conns) == 1 else None)

        link = buyrig.PonsLink('ws://127.0.0.1:1/publish', 'tok' * 8, log=lambda m: None, connect=connect).start()
        link.hello({'type': 'pons_hello', 'source': 'buyrig', 'session': 's1'})
        link.step({'type': 'pons_step', 'key': 'b04_amount', 'phase': 'light'})
        link.frame(b'\xff\xd8jpeg\xff\xd9')
        link.step({'type': 'pons_step', 'key': 'b04_amount', 'phase': 'press'})       # the first connection drops here
        link.tx({'type': 'pons_result', 'kind': 'tx', 'ok': True})
        self.assertFalse(link.step({'type': 'pons_step', 'key': 'x', 'target': 'to 0xabcdef'}))  # refused locally
        self.assertFalse(link.frame(b'\xff\xd8' + b'0' * buyrig.RELAY_MAX_FRAME))            # too big
        time.sleep(1.6)
        link.close(3)
        second = [p for c, p in sent if c == 2]
        firsts = [json.loads(p)['type'] for p in second[:3] if isinstance(p, str)]
        self.assertEqual(firsts[0], 'pons_hello', 'a reconnect starts with the hello again')
        frames = [p for _c, p in sent if isinstance(p, bytes)]
        self.assertTrue(frames and all(f.startswith(b'PJPG\xff\xd8') for f in frames))
        texts = [json.loads(p) for _c, p in sent if isinstance(p, str)]
        self.assertTrue(any(t['type'] == 'pons_result' for t in texts))
        self.assertFalse(any('0xabcdef' in json.dumps(t) for t in texts))
        self.assertEqual(link.stats['texts_refused'], 1)
        self.assertEqual(link.stats['frames_too_big'], 1)


# ---------------------------------------------------------------------------------------------------- tests: engine
def good_report(buy, s_at, **over):
    body = {'buy_at': buy['at'], 'eth_in': buy['eth_in'], 'labrat_out': '3.49', 'session_at': s_at,
            'proof': hashlib.sha256(b'session').hexdigest(), 'replay': 'MATCH', 'targets_hit': 6, 'misses': 4,
            'checks_passed': 13, 'checks_total': 13, 'simulation': 'ok'}
    body.update(over)
    return body


class EngineBase(bt.Base):
    def book_one(self, clock=None):
        self.clock = clock or bt.Clock()
        e = bt.make_engine(self.tmp, self.chain, clock=self.clock)
        bt.feed_hits(e, 50)
        bt.close_hour(e, self.clock)                      # one buy an hour: the hour's (preview) buy
        buys = e.public_status()['buys']['recent']
        self.assertEqual(len(buys), 1)
        return e, buys[0]


class TestEngineReport(EngineBase):
    def test_validation_of_every_field(self):
        e, buy = self.book_one()
        s_at = bb.iso(self.clock.t + 20)
        bad = [
            ({'extra': 1}, 400), ({'proof': 'AB' * 32}, 400), ({'proof': '0x' + 'ab' * 31}, 400),
            ({'buy_at': '2026-09-25 03:22:58'}, 400), ({'session_at': 'yesterday'}, 400),
            ({'eth_in': '1e-4'}, 400), ({'labrat_out': '0x10'}, 400), ({'labrat_out': '0'}, 400),
            ({'replay': 'MISMATCH'}, 400), ({'targets_hit': 0}, 400), ({'targets_hit': True}, 400),
            ({'misses': -1}, 400), ({'checks_passed': 12}, 400), ({'checks_total': 0, 'checks_passed': 0}, 400),
            ({'simulation': 'reverted'}, 400), ({'eth_in': '0.0001'}, 400),
            ({'buy_at': bb.iso(self.clock.t - 3600)}, 404),
            ({'session_at': bb.iso(self.clock.t - 3600)}, 400), ({'session_at': bb.iso(self.clock.t + 3600)}, 400),
        ]
        for over, status in bad:
            with self.subTest(over=over):
                with self.assertRaises(bb.PonsRefused) as cm:
                    e.add_pons_session(good_report(buy, s_at, **over))
                self.assertEqual(cm.exception.status, status, str(cm.exception))
        missing = good_report(buy, s_at)
        del missing['proof']
        with self.assertRaises(bb.PonsRefused):
            e.add_pons_session(missing)
        with self.assertRaises(bb.PonsRefused):
            e.add_pons_session(['not', 'an', 'object'])
        before = json.dumps({k: v for k, v in e.public_status().items() if k not in ('updated',)}, sort_keys=True)
        e.add_pons_session(good_report(buy, s_at))
        with self.assertRaises(bb.PonsRefused) as cm:                      # once per buy
            e.add_pons_session(good_report(buy, s_at))
        self.assertEqual(cm.exception.status, 409)
        st = e.public_status()
        after = dict(st)
        # nothing the engine computed changed: only the label on that buy and the count
        for k in ('pending', 'caps', 'hits', 'next_buy_in_s', 'budget'):
            self.assertEqual(json.dumps(after.get(k), sort_keys=True),
                             json.dumps(json.loads(before).get(k), sort_keys=True), k)
        p = st['buys']['recent'][0]['pons']
        self.assertEqual((p['label'], p['clicked_by_rat'], p['simulated']), (bb.PONS_LABEL, True, True))
        self.assertEqual((p['eth_in'], p['labrat_out'], p['at'], p['checks'], p['replay']),
                         (buy['eth_in'], '3.49', s_at, '13/13', 'MATCH'))
        self.assertEqual(st['buys']['pons_sessions'], 1)
        self.assertIsNone(ADDR40.search(json.dumps(st)))
        # a restart from the journal keeps it
        e2 = bt.make_engine(self.tmp, self.chain, clock=self.clock)
        self.assertEqual(e2.public_status()['buys']['recent'][0]['pons']['label'], bb.PONS_LABEL)

    def test_a_live_engine_takes_no_report(self):
        e, buy = self.book_one()
        with self.assertRaises(bb.PonsRefused) as cm:
            bb.pons_session_record(good_report(buy, bb.iso(self.clock.t)), e.ledger, self.clock(), 'LIVE')
        self.assertEqual(cm.exception.status, 409)

    def test_the_http_endpoint(self):
        e, buy = self.book_one()
        tok = secrets.token_urlsafe(24)
        srv = bb.serve_status(e, '127.0.0.1', STATUS_PORT, tok)
        base = f'http://127.0.0.1:{STATUS_PORT}'
        try:
            body = good_report(buy, bb.iso(self.clock.t + 5))
            self.assertEqual(rn.post_json(base + '/pons_session', body, 'wrong-token-' + 'x' * 20)[0], 401)
            self.assertEqual(rn.post_json(base + '/pons_session', body, '')[0], 401)
            req = urllib.request.Request(base + '/pons_session', data=b'{not json', method='POST', headers={
                'Authorization': f'Bearer {tok}', 'Content-Type': 'application/json'})
            try:
                urllib.request.urlopen(req, timeout=5)
                code = 200
            except urllib.error.HTTPError as err:
                code = err.code
            self.assertEqual(code, 400)
            self.assertEqual(rn.post_json(base + '/pons_session', {'x': 'y' * 5000}, tok)[0], 413)
            code, resp = rn.post_json(base + '/pons_session', body, tok)
            self.assertEqual((code, resp.get('label')), (200, bb.PONS_LABEL))
            self.assertEqual(rn.post_json(base + '/pons_session', body, tok)[0], 409)
            st = rn.fetch_json(base + '/status')
            self.assertEqual(st['buys']['recent'][0]['pons']['label'], bb.PONS_LABEL)
        finally:
            srv.shutdown()
            srv.server_close()
        # no token (or a short one): no endpoint
        srv = bb.serve_status(e, '127.0.0.1', STATUS_PORT, 'short')
        try:
            self.assertEqual(rn.post_json(base + '/pons_session', body, 'short')[0], 404)
        finally:
            srv.shutdown()
            srv.server_close()


# ---------------------------------------------------------------------------------------------------- tests: runner
def status(buys, mode='DRY', simulated=True):
    return {'mode': mode, 'label': bb.DRY_LABEL if mode == 'DRY' else bb.LIVE_LABEL,
            'buys': {'count': len(buys), 'simulated': simulated, 'recent': list(buys)}}


def buy_row(at, eth='0.0005', **kw):
    r = {'at': at, 'eth_in': eth, 'labrat_out': '16000', 'venue': 'pool', 'simulated': True, 'hits_covered': 50}
    r.update(kw)
    return r


def good_result(r, **over):
    res = {'ok': True, 'verdict': 'dry_captured', 'labrat_out': '16012.5', 'session_at': bb.iso(),
           'session_proof': hashlib.sha256(r['at'].encode()).hexdigest(), 'targets_hit': 6, 'misses': 3,
           'checks_passed': 13, 'checks_total': 13, 'simulation': 'ok', 'run_dir': f"runs/buyrig_x_{r['at']}",
           'mask_breach': False, 'dev_oracle': False, 'signed': False, 'sent': False}
    res.update(over)
    return res


class TestRunner(bt.Base):
    def make(self, st, launch=None, replay=None, report=None, clock=None, **kw):
        self.st = st
        self.launched, self.reported = [], []

        def default_launch(r, seed):
            self.launched.append((r['at'], r['eth_in'], seed))
            return good_result(r)
        rep = report if report is not None else (lambda body: self.reported.append(body) or (200, {'ok': True}))
        return rn.Runner('https://engine.example/status', os.path.join(self.tmp, 'runner.json'),
                         launch or default_launch, replay or (lambda d: True), rep,
                         fetch=lambda url: self.st, clock=clock or time.time, **kw)

    def test_history_on_first_start_then_each_new_batch_once(self):
        now = time.time()
        old = [buy_row(bb.iso(now - 600)), buy_row(bb.iso(now - 300))]
        run = self.make(status(list(reversed(old))))
        self.assertEqual(run.poll_once(), [])
        self.assertEqual(self.launched, [])
        new = buy_row(bb.iso(now - 5), eth='0.00047')
        self.st = status([new] + list(reversed(old)))
        self.assertEqual(len(run.poll_once()), 1)
        self.assertEqual([x[:2] for x in self.launched], [(new['at'], '0.00047')])
        for _ in range(3):
            self.assertEqual(run.poll_once(), [])
        self.assertEqual(len(self.launched), 1)
        # the report: the engine's own batch time and amount, once
        self.assertEqual([(b['buy_at'], b['eth_in'], b['replay']) for b in self.reported],
                         [(new['at'], '0.00047', 'MATCH')])
        self.assertEqual(run.state['seen'][rn.batch_key(new)]['state'], 'done')
        self.assertTrue(run.state['seen'][rn.batch_key(new)]['reported'])
        run2 = self.make(self.st)                          # a restart: the same state file
        self.assertEqual(run2.poll_once(), [])
        self.assertEqual((self.launched, self.reported), ([], []))

    def test_the_report_body(self):
        r = buy_row(bb.iso(time.time() - 5), eth='0.00052')
        body = rn.report_body(r, good_result(r))
        self.assertEqual(set(body), bb.PONS_KEYS)
        self.assertEqual((body['buy_at'], body['eth_in'], body['replay']), (r['at'], '0.00052', 'MATCH'))
        self.assertEqual(rn.batch_seed(rn.batch_key(r)), rn.batch_seed(rn.batch_key(dict(r))))

    def test_a_crash_never_runs_a_batch_twice(self):
        now = time.time()
        run = self.make(status([]))
        run.poll_once()
        b = buy_row(bb.iso(now - 5))
        self.st = status([b])

        def crash(r, seed):
            self.launched.append(r['at'])
            raise RuntimeError('chromium crashed')
        run.launch = crash
        run.poll_once()
        self.assertEqual(run.state['seen'][rn.batch_key(b)]['state'], 'failed')
        run2 = self.make(self.st)
        run2.poll_once()
        self.assertEqual(self.launched, [])
        # "started" is written before the session runs: a kill in the middle leaves it started, never re-run
        c = buy_row(bb.iso(now - 2))
        self.st = status([c, b])
        seen = {}

        def peek(r, seed):
            with open(os.path.join(self.tmp, 'runner.json'), encoding='utf-8') as fh:
                seen.update(json.load(fh)['seen'][rn.batch_key(r)])
            return good_result(r)
        run2.launch = peek
        run2.poll_once()
        self.assertEqual(seen.get('state'), 'started')

    def test_only_dry_simulated_engines(self):
        now = time.time()
        for st in (status([], mode='LIVE'), status([], simulated=False), {'mode': 'DRY'}, ['x'], None):
            run = self.make(st)
            run.poll_once()
            self.assertFalse(run.state.get('baselined'), st)
        run = self.make(status([]))
        run.poll_once()
        self.st = status([buy_row(bb.iso(now))], mode='LIVE')
        self.assertEqual(run.poll_once(), [])
        self.st = status([buy_row(bb.iso(now), simulated=False)])
        self.assertEqual(run.poll_once(), [])
        self.st = status([buy_row(bb.iso(now), eth='0.5'), buy_row(bb.iso(now), eth='0x10'), buy_row('now')])
        self.assertEqual(run.poll_once(), [])
        self.assertEqual(self.launched, [])

        def boom(url):
            raise OSError('down')
        run.fetch = boom
        self.assertEqual(run.poll_once(), [])

    def test_old_batches_and_batches_with_a_session_are_skipped(self):
        now = time.time()
        run = self.make(status([]), max_age_s=600)
        run.poll_once()
        old, done = buy_row(bb.iso(now - 1200)), buy_row(bb.iso(now - 10), pons={'label': bb.PONS_LABEL})
        self.st = status([done, old])
        self.assertEqual(len(run.poll_once()), 2)
        self.assertEqual(self.launched, [])
        self.assertEqual({v['state'] for v in run.state['seen'].values()}, {'skipped'})

    def test_only_a_passing_replayed_session_is_reported(self):
        now = time.time()
        cases = [({'ok': False, 'verdict': 'dry_refused_mismatch'}, True),
                 ({'mask_breach': True}, True), ({'dev_oracle': True}, True), ({'simulation': 'reverted'}, True),
                 ({'checks_passed': 12}, True), ({'session_proof': None}, True), ({}, False)]
        for i, (over, replay_ok) in enumerate(cases):
            with self.subTest(over=over, replay=replay_ok):
                shutil.rmtree(self.tmp, ignore_errors=True)
                os.makedirs(self.tmp)
                run = self.make(status([]), launch=lambda r, s, o=over: good_result(r, **o),
                                replay=lambda d, ok=replay_ok: ok)
                run.poll_once()
                self.st = status([buy_row(bb.iso(now - i))])
                run.poll_once()
                self.assertEqual(self.reported, [])
                self.assertEqual(list(run.state['seen'].values())[-1]['state'], 'failed')

    def test_catch_up_on_first_start(self):
        now = time.time()
        rows = [buy_row(bb.iso(now - 30)), buy_row(bb.iso(now - 60)), buy_row(bb.iso(now - 90))]
        run = self.make(status(rows), catch_up=1)
        run.poll_once()
        self.assertEqual([x[0] for x in self.launched], [rows[0]['at']])

    def test_an_unreadable_state_file_stops_the_runner(self):
        p = os.path.join(self.tmp, 'runner.json')
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write('{broken')
        with self.assertRaises(SystemExit):
            self.make(status([]))

    def test_end_to_end_with_the_engine_over_http(self):
        e, _ = EngineBase.book_one(self)
        tok = secrets.token_urlsafe(24)
        srv = bb.serve_status(e, '127.0.0.1', STATUS_PORT, tok)
        url = f'http://127.0.0.1:{STATUS_PORT}/status'
        launched = []
        try:
            def launch(r, seed):
                launched.append(r['at'])
                return good_result(r, session_at=bb.iso(self.clock.t + 30), labrat_out='3.49')
            run = rn.Runner(url, os.path.join(self.tmp, 'runner.json'), launch, lambda d: True,
                            rn.make_report(rn.default_report_url(url), tok), fetch=rn.fetch_json,
                            clock=self.clock, catch_up=1)
            run.poll_once()
            run.poll_once()
            self.assertEqual(len(launched), 1)
            key = next(iter(run.state['seen']))
            self.assertEqual(run.state['seen'][key]['report_status'], 200)
            st = rn.fetch_json(url)
            row = st['buys']['recent'][0]
            self.assertEqual(row['pons']['label'], bb.PONS_LABEL)
            self.assertEqual((row['pons']['eth_in'], row['pons']['labrat_out']), (row['eth_in'], '3.49'))
            self.assertIsNone(ADDR40.search(json.dumps(st)))
            # the next poll sees the label and runs nothing; a new runner would skip it too
            run3 = rn.Runner(url, os.path.join(self.tmp, 'runner2.json'), launch, lambda d: True,
                             lambda b: None, fetch=rn.fetch_json, clock=self.clock, catch_up=1)
            run3.poll_once()
            self.assertEqual(len(launched), 1)
        finally:
            srv.shutdown()
            srv.server_close()


# ---------------------------------------------------------------------------------------------------- LIVE (mocked)
LIVE_WEI = AMOUNT_WEI                      # 0.0001 ETH, the booked amount in these tests


class Crash(BaseException):
    """The process dying at a given line (a BaseException: no `except Exception` in the code catches it)."""


def live_window(now=None, hours_ahead=0):
    """The hour that ended last (signable now: from its end until an hour later)."""
    t = (time.time() if now is None else now) + 3600 * hours_ahead
    return bb.iso(bb.window_start(t) - 3600)


def key_env(key_hex):
    return {bl.ENV_LIVE: '1', bl.ENV_CONFIRM: 'LABRAT', bl.ENV_KEY: '0x' + key_hex}


class LiveBase(bt.Base):
    """A temp LIVE journal, the fake chain as the buyback wallet's chain (nonce 0, 1 ETH), a MockSigner (no key)."""
    live_test = True

    def setUp(self):
        super().setUp()
        self._saved = (bl.JOURNAL_PATH, bl.VOLUME, bl.WALLET, buyrig.TOAST_WAIT_S)
        bl.VOLUME = None                        # tests have no /data volume
        bl.JOURNAL_PATH = Path(self.tmp) / 'buyrig' / 'live_journal.jsonl'
        buyrig.TOAST_WAIT_S = 0.1
        self.chain.buyback_balance = 10 ** 18
        self.chain.nonce = bl.FIRST_NONCE       # the buyback wallet has sent nothing yet
        self.journal = bl.Journal(bl.JOURNAL_PATH)
        self.window = live_window()
        self.signer = bt.MockSigner(bl.WALLET)
        self.logs = []

    def tearDown(self):
        bl.JOURNAL_PATH, bl.VOLUME, bl.WALLET, buyrig.TOAST_WAIT_S = self._saved
        super().tearDown()

    def rpc(self, chain=None):
        ch = chain or self.chain
        return bl.open_rpc(SimpleNamespace(address=bl.WALLET), ch, [ch], environ={})

    def buyer(self, window=None, amount=LIVE_WEI, signer=None, journal=None, **kw):
        kw.setdefault('sleep', lambda s: None)
        kw.setdefault('receipt_wait_s', 0.3)
        return bl.LiveBuyer(signer or self.signer, self.rpc(), journal or self.journal, window or self.window, amount,
                            log=self.logs.append, **kw)

    def data(self, amount=LIVE_WEI, deadline=None, min_out=None):
        q = amount * self.chain.rate
        return bb.cd_router_buy(amount, q * 99 // 100 if min_out is None else min_out,
                                int(time.time()) + 1200 if deadline is None else deadline)

    def recs(self, ev=None, journal=None):
        recs, _bad = (journal or self.journal).records()
        return [r for r in recs if ev is None or r['ev'] == ev]


def new_live_run(tmp, buyer, link=None):
    """A LIVE BuyRun inside a running loop, with no browser (the page wallet stub has the buyback wallet's address)."""
    buyrig.configure(7)
    r = buyrig.BuyRun(AMOUNT, 7, link=link, out_root=tmp, buyer=buyer)
    r.out = StubOut()
    r.out.shot_idle = asyncio.Event()
    r.out.shot_idle.set()
    r.bot = SimpleNamespace(armed=False, address=bl.WALLET, page=None, signatures=[], refusals=[],
                            arm=lambda: setattr(r.bot, 'armed', True))
    r.commit = hashlib.sha256(b'brain').hexdigest()
    r.review = GOOD_REVIEW
    return r


def live_tx(case, **kw):
    """pons's buy from the buyback wallet (TxCase's, with from = the wallet)."""
    return case.tx(**{'from': bl.WALLET, **kw})


async def live_send(r, tx):
    r.bot.arm()
    resp = await r.on_send(tx)
    if r.receipt_task:
        await r.receipt_task
    if r.rejection_task:
        await r.rejection_task
    return resp


class TestLiveGates(LiveBase):
    def decide(self, env, live=True, window='default', dev_oracle=False, chain=None):
        ch = chain or self.chain
        return buyrig.decide_live(live, self.window if window == 'default' else window, LIVE_WEI, dev_oracle,
                                  environ=env, gate_rpc=bb.ReadRpc(ch), log=self.logs.append, transport=ch, nodes=[ch],
                                  journal=self.journal, sleep=lambda s: None, receipt_wait_s=0.3)

    def test_every_gate_else_the_session_runs_dry(self):
        acct = Account.create()                      # a key made here: never funded, never the buyback wallet's
        key = bytes(acct.key).hex()
        other = bytes(Account.create().key).hex()
        good = key_env(key)
        bl.WALLET = acct.address                     # this test's stand-in for the pinned wallet
        wrong_chain = bt.FakeChain()
        wrong_chain.chain_id = 1
        cases = [
            ('no --live, no --window', dict(good), dict(live=False, window=None), 'not asked for'),
            ('--window without --live', dict(good), dict(live=False), '--live was not given'),
            ('--live without --window', dict(good), dict(window=None), '--window was not given'),
            ('a window that is not an hour', dict(good), dict(window='2026-09-25T20:30:00Z'), 'UTC hour'),
            ('BUYRIG_LIVE missing', {k: v for k, v in good.items() if k != bl.ENV_LIVE}, {}, 'BUYRIG_LIVE is not 1'),
            ('BUYRIG_LIVE=0', dict(good, **{bl.ENV_LIVE: '0'}), {}, 'BUYRIG_LIVE is not 1'),
            ('BUYRIG_CONFIRM missing', {k: v for k, v in good.items() if k != bl.ENV_CONFIRM}, {}, 'BUYRIG_CONFIRM'),
            ('BUYRIG_CONFIRM=labrat', dict(good, **{bl.ENV_CONFIRM: 'labrat'}), {}, 'BUYRIG_CONFIRM'),
            ('no key', {k: v for k, v in good.items() if k != bl.ENV_KEY}, {}, 'is not set'),
            ('a key that does not parse', dict(good, **{bl.ENV_KEY: 'not-a-key'}), {}, 'does not parse'),
            ('the key of another wallet', dict(good, **{bl.ENV_KEY: other}), {}, 'not the key of the pinned'),
            ('chain id 1', dict(good), dict(chain=wrong_chain), 'chain id 1'),
            ('the scripted cursor', dict(good), dict(dev_oracle=True), 'scripted cursor'),
        ]
        for name, env, kw, why in cases:
            with self.subTest(name):
                buyer, refusal, got = self.decide(env, **kw)
                self.assertIsNone(buyer)
                self.assertIsNone(refusal)
                self.assertIn(why, got)
                self.assertNotIn(bl.ENV_KEY, env, 'the key leaves the environment whatever the outcome')
                self.assertNotIn(key, got.lower())
                self.assertNotIn(other, got.lower())
        # every gate holds: LIVE, and the key has left the environment
        env = dict(good)
        buyer, refusal, why = self.decide(env)
        self.assertIsInstance(buyer, bl.LiveBuyer)
        self.assertIsNone(refusal)
        self.assertNotIn(bl.ENV_KEY, env)
        self.assertEqual((buyer.window, buyer.amount_wei, buyer.address), (self.window, LIVE_WEI, acct.address))
        # every gate holds but LIVE is stopped: the session is refused (nothing runs), not quietly DRY
        bl.stop(self.journal, 'an operator must look', self.window, 'check', log=self.logs.append)
        buyer, refusal, why = self.decide(dict(good))
        self.assertIsNone(buyer)
        self.assertEqual(refusal.kind, 'blocked')
        self.assertIn('LIVE is stopped', str(refusal))
        # RATBRAIN_RPC: LIVE only through the pinned public RPCs
        with self.assertRaises(bl.LiveRefused):
            bl.open_rpc(acct, self.chain, [self.chain], environ={'RATBRAIN_RPC': 'http://127.0.0.1:8545'})
        self.assertEqual(self.chain.sent, [])

    def test_the_page_wallet_is_the_address_only_with_its_real_balance(self):
        with self.assertRaises(ValueError):
            buyrig.BuyBot(ponsbot.throwaway_account(), live=True)       # never a key in LIVE's page wallet
        bot = buyrig.BuyBot(bl.AddressOnly(bl.WALLET), live=True)
        self.assertFalse(bot.dry)
        self.assertEqual(bot.address, bl.WALLET)
        done = []

        class Route:
            def __init__(self, body):
                self.request = SimpleNamespace(method='POST', post_data=json.dumps(body))

            async def abort(self):
                done.append('abort')

            async def continue_(self):
                done.append('continue')

            async def fetch(self, **kw):
                done.append('fetch')                  # DRY's balance override rewrites the body: never in LIVE

        async def go():
            call = {'id': 1, 'method': 'eth_call', 'params': [{'to': bb.ROUTER, 'data': '0x'}, 'latest']}
            await bot._route_rpc(Route(call))
            await bot._route_rpc(Route({'id': 2, 'method': 'eth_getBalance', 'params': [bl.WALLET, 'latest']}))
            await bot._route_rpc(Route({'id': 3, 'method': 'eth_sendRawTransaction', 'params': ['0x02']}))
            return [await bot._eth_sign('personal_sign', ['0x68656c6c6f', bot.address]),
                    await bot._eth_sign_typed('eth_signTypedData_v4', [bot.address, '{}'])]
        signs = asyncio.run(go())
        self.assertEqual(done, ['continue', 'continue', 'abort'])
        self.assertEqual(signs, [{'error': {'code': 4001, 'message': buyrig.REFUSAL_LIVE}}] * 2)
        self.assertNotIn('DRY', buyrig.REFUSAL_LIVE)


class TestLiveBuyer(LiveBase):
    def test_one_transaction_journalled_before_it_is_broadcast(self):
        journal_path = self.journal.path
        seen = {}

        def on_send(raw, txh):                        # at the broadcast: 'reserved' and 'signed' are already on disk
            with open(journal_path, encoding='utf-8') as f:
                lines = [json.loads(l) for l in f]
            seen['evs'] = [r['ev'] for r in lines]
            seen['signed'] = [r for r in lines if r['ev'] == 'signed']
            seen['raw'], seen['txh'] = raw, txh
        self.chain.on_send = on_send
        b = self.buyer()
        self.assertEqual(b.preflight(), {'nonce': 0})
        data, deadline = self.data(), int(time.time()) + 1200
        sent = b.sign_and_send(data, deadline)
        self.assertEqual(seen['evs'], ['reserved', 'signed'], 'reserved, then signed, then the broadcast')
        s, = seen['signed']
        self.assertEqual((s['raw'], s['tx']), (seen['raw'], seen['txh']))
        self.assertEqual((sent['tx'], sent['broadcast'], sent['error']), (seen['txh'], True, None))
        self.assertEqual([r['ev'] for r in self.recs()], ['reserved', 'signed', 'sent'])
        self.assertTrue(self.journal.lock_path(self.window).exists())
        tx, = self.signer.signed                      # ONE transaction, exactly what was checked
        gp = self.chain.gas_price
        self.assertEqual(tx, {'chainId': 4663, 'nonce': 0, 'to': bb.ROUTER, 'value': LIVE_WEI, 'data': data,
                              'gas': -(-161_654 * 5 // 4), 'maxFeePerGas': min(2 * gp, bl.MAX_FEE_CAP_WEI),
                              'maxPriorityFeePerGas': 0, 'type': 2})
        info = b.wait_receipt()
        self.assertTrue(info['ok'])
        self.assertEqual(info['labrat_out_wei'], LIVE_WEI * self.chain.rate)
        rc, = self.recs('receipt')
        self.assertEqual((rc['tx'], rc['ok'], rc['block']), (sent['tx'], True, self.chain.block))
        st = self.journal.state()
        self.assertTrue(st.bought(self.window))
        self.assertEqual((st.expected_nonce(), st.failures), (1, 0))
        self.assertEqual(st.executed(), [{'window': self.window, 'tx': sent['tx'], 'amount_wei': LIVE_WEI,
                                          'labrat_out_wei': LIVE_WEI * self.chain.rate, 'block': self.chain.block}])
        with self.assertRaises(bl.LiveRefused):          # this session had its one buy
            b.sign_and_send(data, deadline)
        self.assertEqual(len(self.signer.signed), 1)

    def test_one_transaction_per_window_across_restarts(self):
        b = self.buyer()
        b.sign_and_send(self.data(), int(time.time()) + 1200)
        b.wait_receipt()
        for name in ('the next process', 'another one'):
            with self.subTest(name):
                with self.assertRaises(bl.LiveRefused) as cm:          # a restart: a new journal object, same file
                    self.buyer(journal=bl.Journal(self.journal.path)).preflight()
                self.assertIn('already has its transaction', str(cm.exception))
                self.assertEqual(cm.exception.kind, 'blocked')
        # the lock file alone refuses (a journal line lost), and the journal line alone refuses (a lock file lost)
        w2 = live_window(hours_ahead=1)
        clock2 = lambda: time.time() + 3600           # noqa: E731  (an hour later: w2 is the signable window)
        j2 = bl.Journal(Path(self.tmp) / 'j2' / 'live_journal.jsonl')
        j2.lock_dir.mkdir(parents=True)
        j2.lock_path(w2).write_text('{}', encoding='utf-8')
        with self.assertRaises(bl.LiveRefused) as cm:
            bl.LiveBuyer(self.signer, self.rpc(), j2, w2, LIVE_WEI, clock=clock2).preflight()
        self.assertIn('already has its transaction', str(cm.exception))
        j3 = bl.Journal(Path(self.tmp) / 'j3' / 'live_journal.jsonl')
        j3.append({'ev': 'reserved', 'window': w2, 'amount_wei': LIVE_WEI})
        with self.assertRaises(bl.LiveRefused) as cm:
            bl.LiveBuyer(self.signer, self.rpc(), j3, w2, LIVE_WEI, clock=clock2).preflight()
        self.assertIn('already has its transaction', str(cm.exception))
        # two processes past the preflight at once: the exclusive lock lets only one reserve
        j4 = bl.Journal(Path(self.tmp) / 'j4' / 'live_journal.jsonl')
        j4.reserve(w2, {'amount_wei': LIVE_WEI})
        with self.assertRaises(bl.LiveRefused):
            j4.reserve(w2, {'amount_wei': LIVE_WEI})
        self.assertEqual(len(self.signer.signed), 1)
        self.assertEqual(len(self.chain.sent), 1)

    def test_a_crash_between_signing_and_broadcasting(self):
        """The process dies after 'signed' is on disk and before eth_sendRawTransaction: the next start sends the
        IDENTICAL bytes (resolve), never signs again, and the window is never signed twice."""
        b = self.buyer()
        rpc = b.rpc

        def die(raw):
            raise Crash('killed at the broadcast')
        rpc.send_raw = die
        with self.assertRaises(Crash):
            b.sign_and_send(self.data(), int(time.time()) + 1200)
        self.assertEqual([r['ev'] for r in self.recs()], ['reserved', 'signed'])
        self.assertEqual(self.chain.sent, [], 'nothing left the machine')
        signed_raw = self.recs('signed')[0]['raw']
        # the restart: a new journal object and RPC; the window's preflight resolves first
        j = bl.Journal(self.journal.path)
        with self.assertRaises(bl.LiveRefused) as cm:
            self.buyer(journal=j).preflight()                     # re-broadcast now; resolved on the next look
        self.assertIn('not resolved yet', str(cm.exception))
        self.assertEqual(self.chain.sent, [signed_raw], 'the identical signed bytes, nothing else')
        out = bl.resolve(j, self.rpc(), self.logs.append)
        self.assertEqual(out['resolved'], [self.window])
        st = j.state()
        self.assertTrue(st.bought(self.window))
        self.assertEqual(st.unresolved(), {})
        self.assertEqual(len(self.signer.signed), 1, 'never signed again')
        self.assertEqual([r['ev'] for r in self.recs(journal=j)],
                         ['reserved', 'signed', 'rebroadcast', 'receipt'])
        with self.assertRaises(bl.LiveRefused):
            self.buyer(journal=j).preflight()                     # and the window is used for good

    def test_a_crash_after_the_broadcast_is_finished_by_its_receipt(self):
        self.chain.auto_mine = False
        b = self.buyer()
        sent = b.sign_and_send(self.data(), int(time.time()) + 1200)   # in the mempool; the process dies here
        out = bl.resolve(self.journal, self.rpc(), self.logs.append)
        self.assertEqual(out['pending'], [self.window], 'still in the mempool: wait')
        self.chain.mine(sent['tx'])
        out = bl.resolve(self.journal, self.rpc(), self.logs.append)
        self.assertEqual(out['resolved'], [self.window])
        self.assertEqual(len(self.chain.sent), 1, 'no re-broadcast was needed')
        self.assertTrue(self.journal.state().bought(self.window))

    def test_a_signed_buy_that_never_landed_expires_and_its_nonce_is_reused(self):
        self.chain.reject_sends = {'code': -32000, 'message': 'insufficient funds for gas * price + value'}
        b = self.buyer()
        sent = b.sign_and_send(self.data(), int(time.time()) + 1200)
        self.assertIs(sent['broadcast'], False)
        self.assertIsNone(b.wait_receipt(0))
        out = bl.resolve(self.journal, self.rpc(), self.logs.append)          # deadline not passed: the same bytes
        self.assertEqual(out['pending'], [self.window])
        self.assertEqual(self.chain.sent[0], self.chain.sent[1])
        self.chain.ts = int(time.time()) + 1200 + bl.EXPIRE_MARGIN_S + 5       # its router deadline has passed
        out = bl.resolve(self.journal, self.rpc(), self.logs.append)
        self.assertEqual(out['resolved'], [self.window])
        st = self.journal.state()
        self.assertEqual((st.unresolved(), st.failures, st.expected_nonce()), ({}, 1, 0))
        self.assertTrue(st.windows[self.window]['expired'])
        # the next window's transaction takes the free nonce 0
        self.chain.reject_sends = None
        self.chain.ts = int(time.time())
        w2 = live_window(hours_ahead=1)
        b2 = self.buyer(window=w2, clock=lambda: time.time() + 3600)
        self.assertEqual(b2.preflight(), {'nonce': 0})
        b2.sign_and_send(self.data(), int(time.time()) + 1200)
        self.assertEqual(self.signer.signed[-1]['nonce'], 0)
        self.assertTrue(b2.wait_receipt()['ok'])

    def test_a_nonce_used_elsewhere_stops_live_until_an_operator_acts(self):
        self.chain.auto_mine = False
        b = self.buyer()
        b.sign_and_send(self.data(), int(time.time()) + 1200)
        self.chain.pending_txs.clear()            # the chain dropped it, and the owner sent a transaction by hand
        self.chain.nonce = 1
        out = bl.resolve(self.journal, self.rpc(), self.logs.append)
        self.assertIn('used nonce 0 for another transaction', out['stopped'])
        st = self.journal.state()
        self.assertEqual(st.stop['kind'], 'nonce')
        with self.assertRaises(bl.LiveRefused):
            self.buyer(window=live_window(hours_ahead=1), clock=lambda: time.time() + 3600).preflight()
        # the operator checked: abandon it (every RPC agrees), clear the stop, anchor the nonce
        self.assertTrue(bl.abandon(self.journal, self.window, nodes=[self.chain], log=self.logs.append))
        self.assertEqual(self.journal.state().unresolved(), {})
        self.assertTrue(bl.clear_stop(self.journal, st.stop['id'], log=self.logs.append))
        b2 = self.buyer(window=live_window(hours_ahead=1), clock=lambda: time.time() + 3600)
        with self.assertRaises(bl.LiveRefused) as cm:
            b2.preflight()
        self.assertIn(f'{bl.ENV_ANCHOR}=1', str(cm.exception))
        bl.apply_operator_env(self.journal, self.rpc(), {bl.ENV_ANCHOR: '1'}, self.logs.append)
        self.assertEqual(b2.preflight(), {'nonce': 1})

    def test_preflight_refusals(self):
        def refused(b, kind, words, stage='start'):
            with self.assertRaises(bl.LiveRefused) as cm:
                b.preflight(stage)
            self.assertEqual(cm.exception.kind, kind, str(cm.exception))
            self.assertIn(words, str(cm.exception))
        now = time.time()
        refused(self.buyer(window=bb.iso(bb.window_start(now))), 'check', 'has not ended')
        old = bb.iso(bb.window_start(now) - 3 * 3600)
        refused(self.buyer(window=old), 'blocked', 'too late')
        refused(self.buyer(window=old), 'failure', 'too late', stage='sign')
        self.chain.pending_txs['0xab'] = {'nonce': 0}            # a transaction of the wallet that is not the rig's
        refused(self.buyer(), 'failure', 'pending transaction')
        self.chain.pending_txs.clear()
        self.chain.nonce = 3                                      # the journal accounts for 0
        refused(self.buyer(), 'blocked', 'accounts for 0')
        self.chain.nonce = 0
        # the rig's own caps (2.4 ETH signed a day, 5 ETH ever)
        j = bl.Journal(Path(self.tmp) / 'caps' / 'live_journal.jsonl')
        for i in range(3):
            w = bb.iso(bb.window_start(now) - (i + 5) * 3600)
            j.append({'ev': 'signed', 'window': w, 'tx': '0x' + f'{i:02x}' * 32, 'raw': '0x', 'nonce': i,
                      'value': 8 * 10 ** 17})
            j.append({'ev': 'receipt', 'window': w, 'tx': '0x' + f'{i:02x}' * 32, 'ok': True, 'status': 1})
        self.chain.nonce = 3
        refused(self.buyer(journal=j), 'check', "the rig's own caps")
        self.chain.nonce = 0
        # a damaged journal line refuses everything
        self.journal.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.journal.path, 'a', encoding='utf-8') as f:
            f.write('{"ev": "sig\n')
        refused(self.buyer(), 'blocked', 'unreadable line')
        with self.assertRaises(bl.LiveRefused):
            bl.LiveBuyer(self.signer, self.rpc(), self.journal, self.window, 2 * 10 ** 17)    # over 0.1 ETH
        with self.assertRaises(bl.LiveRefused):
            bl.LiveBuyer(bt.MockSigner(bb.WALLET), self.rpc(), self.journal, self.window, LIVE_WEI)
        with self.assertRaises(bl.LiveRefused):
            bl.LiveBuyer(self.signer, bb.ReadRpc(self.chain), self.journal, self.window, LIVE_WEI)
        self.assertEqual((self.signer.signed, self.chain.sent), ([], []))

    def test_the_real_transaction_format(self):
        """The dict LiveBuyer signs, signed OFFLINE with a key made here (never funded): an EIP-1559 transaction on
        chain 4663 to the pons router with exactly the checked calldata. Nothing is sent anywhere."""
        from eth_account.typed_transactions import TypedTransaction
        from hexbytes import HexBytes
        acct = Account.create()
        data = self.data()
        tx = bl.build_tx(7, data, LIVE_WEI, 202_068, 71_656_000)
        signed = _REAL_SIGN_TX(acct, tx)
        raw = bytes(signed.raw_transaction)
        self.assertEqual(raw[0], 2, 'EIP-1559 (type 2)')
        d = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
        self.assertEqual((d['chainId'], d['nonce'], to_checksum_address(d['to']), d['value'], d['gas'],
                          d['maxFeePerGas'], d['maxPriorityFeePerGas'], '0x' + bytes(d['data']).hex()),
                         (4663, 7, bb.ROUTER, LIVE_WEI, 202_068, 71_656_000, 0, data))
        self.assertEqual(Account.recover_transaction(raw), acct.address)
        self.assertEqual(bytes(signed.hash), keccak(raw), 'the hash the journal keeps is the transaction hash')
        self.assertEqual(self.chain.sent, [])


class TestLiveSession(LiveBase):
    """BuyRun's LIVE path after the rat's Confirm (no browser: the page stub has the buyback wallet's address)."""

    def run_send(self, tx, buyer=None, link=None):
        async def go():
            r = new_live_run(self.tmp, buyer or self.buyer(), link=link)
            resp = await live_send(r, tx)
            return r, resp
        return asyncio.run(go())

    def test_a_live_buy_on_the_fake_chain(self):
        c = TxCase(self.chain)
        link = FakeLink()
        r, resp = self.run_send(live_tx(c), link=link)
        txh = self.recs('signed')[0]['tx']
        self.assertEqual(resp, {'result': txh}, 'pons gets the hash')
        self.assertTrue(r.inspection['ok'], r.inspection['failed'])
        self.assertEqual(set(r.inspection['checks']), set(buyrig.PUBLIC_CHECKS_ALL))
        self.assertFalse(r.inspection['facts']['override'], 'LIVE simulates from the real balance')
        self.assertEqual(len(self.signer.signed), 1)
        self.assertEqual([x['ev'] for x in self.recs()], ['reserved', 'signed', 'sent', 'receipt'])
        lf = r._live_facts()
        self.assertEqual((lf['signed'], lf['sent'], lf['bought'], lf['tx']), (True, True, True, txh))
        self.assertEqual(lf['labrat_out'], bb.token_str(LIVE_WEI * self.chain.rate))
        out = r._outcome()
        self.assertEqual((out['mode'], out['tx'], out['signed'], out['sent']), ('live_bought', txh, True, True))
        r.run_dir.mkdir(parents=True, exist_ok=True)
        r._write_result(out, {'hits_forwarded': 3, 'misses_masked': 1, 'frames_saved': [], 'stream': {}})
        res = r.result
        self.assertEqual((res['ok'], res['mode'], res['window'], res['tx'], res['block'], res['signed'], res['sent']),
                         (True, 'LIVE', self.window, txh, self.chain.block, True, True))
        self.assertEqual(res['labrat_out'], bb.token_str(LIVE_WEI * self.chain.rate))
        self.assertIsNone(rn.reportable(res))
        # the stream: "Live", not simulated, signed and sent, and never a 0x string (the hash is not streamed)
        hello, done = r._hello_msg(), r._done_msg()
        self.assertEqual((hello['label'], hello['simulated']), ('Live', False))
        self.assertEqual((done['ok'], done['outcome'], done['signed'], done['sent'], done['simulated']),
                         (True, 'bought', True, True, False))
        txm = [m for m in link.msgs if m.get('kind') == 'tx']
        self.assertEqual(txm[-1]['status'], 'bought')
        for m in link.msgs + [hello, done]:
            buyrig.public_json(m)
            self.assertNotIn(txh[2:], json.dumps(m))

    def test_tampered_buys_are_refused_nothing_signed_and_live_stops(self):
        key = bb.pool_key()
        cases = [
            ('from another address', lambda c: c.tx(**{'from': OTHER})),
            ('to another contract', lambda c: live_tx(c, to=bb.QUOTER)),
            ('value over the booked amount', lambda c: live_tx(c, value=hex(2 * AMOUNT_WEI))),
            ('another token (pool key)', lambda c: live_tx(c, data=c.data(key=(key[0], OTHER, key[2], key[3], key[4])))),
            ('another amount (amountIn = value = settle)', lambda c: live_tx(
                c, value=hex(2 * AMOUNT_WEI), data=c.data(amount=2 * AMOUNT_WEI,
                                                          min_out=2 * AMOUNT_WEI * self.chain.rate * 99 // 100))),
            # 97.5 %: passes DRY's 97 % floor, fails LIVE's 98 %
            ('min out 97.5% of the quote', lambda c: live_tx(c, data=c.data(min_out=c.quote * 975 // 1000))),
            ('a deadline a day away', lambda c: live_tx(c, data=c.data(deadline=int(c.now) + 86400))),
        ]
        for i, (name, make) in enumerate(cases):
            with self.subTest(name):
                self.journal = bl.Journal(Path(self.tmp) / f'case{i}' / 'live_journal.jsonl')
                n_sent = len(self.chain.sent)
                r, resp = self.run_send(make(TxCase(self.chain)))
                self.assertEqual(resp, {'error': {'code': 4001, 'message': buyrig.REFUSAL_LIVE}})
                self.assertEqual(self.signer.signed, [], 'nothing signed')
                self.assertEqual(len(self.chain.sent), n_sent, 'nothing sent')
                self.assertFalse(r.inspection['ok'])
                if name.startswith('min out'):
                    self.assertEqual(r.inspection['failed'], ['min_out_live'], 'the LIVE floor is the tighter one')
                self.assertEqual((r.live_kind, r.live_verdict), ('check', 'live_refused_checks'))
                evs = [x['ev'] for x in self.recs()]
                self.assertNotIn('reserved', evs)
                self.assertEqual(self.journal.state().stop['kind'], 'check', 'a check failure stops LIVE')
                with self.assertRaises(bl.LiveRefused):
                    self.buyer().preflight()

    def test_a_short_balance_is_a_failed_buy_and_two_in_a_row_stop_live(self):
        self.chain.buyback_balance = 0
        c = TxCase(self.chain)
        r, resp = self.run_send(live_tx(c))
        self.assertEqual(resp['error']['code'], 4001)
        self.assertEqual((r.live_kind, r.fail_reason), ('failure', 'balance_low'))
        self.assertEqual(r._done_msg()['reason'] if r.result else r._live_reason(r._numbers()), 'balance_low')
        st = self.journal.state()
        self.assertEqual((st.failures, st.stop), (1, None), 'one failed buy: not stopped yet')
        # the next hour's buy fails the same way: two in a row stop LIVE
        w2 = live_window(hours_ahead=1)
        b2 = self.buyer(window=w2, clock=lambda: time.time() + 3600)
        r2, _ = self.run_send(live_tx(TxCase(self.chain)), buyer=b2)
        st = self.journal.state()
        self.assertEqual((st.failures, st.stop['kind']), (2, 'failures'))
        with self.assertRaises(bl.LiveRefused) as cm:
            self.buyer(window=live_window(hours_ahead=2), clock=lambda: time.time() + 7200).preflight()
        self.assertIn('LIVE is stopped', str(cm.exception))
        self.assertEqual(self.signer.signed, [])
        # the operator funds the wallet and clears the stop: a mined buy resets the count
        self.assertTrue(bl.clear_stop(self.journal, st.stop['id'], log=self.logs.append))
        self.chain.buyback_balance = 10 ** 18
        b3 = self.buyer(window=live_window(hours_ahead=2), clock=lambda: time.time() + 7200)
        r3, resp3 = self.run_send(live_tx(TxCase(self.chain)), buyer=b3)
        self.assertIn('result', resp3)
        self.assertEqual(self.journal.state().failures, 0)
        # a session that never reached pons's request (e.g. the rat timed out) is a failed buy too
        w4 = live_window(hours_ahead=3)
        b4 = self.buyer(window=w4, clock=lambda: time.time() + 3 * 3600)

        async def ended():
            r4 = new_live_run(self.tmp, b4)
            await r4.loop.run_in_executor(None, r4._live_settle, {'mode': 'ended', 'reason': 'the rat did not hit it'})
            return r4
        asyncio.run(ended())
        self.assertEqual(self.journal.state().windows[w4]['failed']['why'], 'the rat did not hit it')

    def test_the_key_never_appears_in_any_output(self):
        """The whole LIVE path with a key made here (never funded) in the environment: gate, signer, a real signature,
        the fake chain, the receipt, the result, the stream, the journal, the runner's report. The key's hex appears
        nowhere, and a log line that somehow held it is redacted."""
        acct = Account.create()
        key = bytes(acct.key).hex()
        bl.WALLET = acct.address
        self.chain.accept_signed = True
        env = key_env(key)
        trap = LocalAccount.sign_transaction
        out_buf, err_buf = io.StringIO(), io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        LocalAccount.sign_transaction = _REAL_SIGN_TX
        link = FakeLink()
        try:
            sys.stdout, sys.stderr = out_buf, err_buf
            buyer, refusal, why = buyrig.decide_live(
                True, self.window, LIVE_WEI, environ=env, gate_rpc=bb.ReadRpc(self.chain), transport=self.chain,
                nodes=[self.chain], journal=self.journal, sleep=lambda s: None, receipt_wait_s=0.3)
            self.assertIsNotNone(buyer, why)
            self.assertNotIn(bl.ENV_KEY, env)

            async def go():
                r = new_live_run(self.tmp, buyer, link=link)
                resp = await live_send(r, TxCase(self.chain).tx(**{'from': acct.address}))
                r.log(f'a line that somehow holds the key {key} and 0x{key.upper()}')
                r.log_ts(f'from a thread: {key}')
                await asyncio.sleep(0.05)
                r.run_dir.mkdir(parents=True, exist_ok=True)
                r._write_result(r._outcome(), {'hits_forwarded': 3, 'misses_masked': 1, 'frames_saved': [],
                                               'stream': {}})
                return r, resp
            r, resp = asyncio.run(go())
            self.assertIn('result', resp)
            report = rn.live_report_body(self.journal.state().executed()[0])
            print(buyer, repr(buyer), r.result)
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            LocalAccount.sign_transaction = trap
        self.assertTrue(r.result['ok'])
        texts = {'stdout': out_buf.getvalue(), 'stderr': err_buf.getvalue(),
                 'journal': self.journal.path.read_text(encoding='utf-8'),
                 'locks': ' '.join(p.read_text(encoding='utf-8') for p in self.journal.lock_dir.iterdir()),
                 'result': json.dumps(r.result), 'capture': json.dumps(r.capture, default=str),
                 'events': json.dumps(r.out.events, default=str), 'stream': json.dumps(link.msgs),
                 'report': json.dumps(report), 'logs': json.dumps(self.logs, default=str),
                 'result_file': (r.run_dir / 'buyrig_result.json').read_text(encoding='utf-8')}
        for where, text in texts.items():
            self.assertNotIn(key, text.lower(), f'the key is in the {where}')
        self.assertIn('<redacted>', texts['stdout'])
        self.assertIn(r.result['tx'], texts['journal'])


class TestLiveRunner(LiveBase):
    """The runner in LIVE against a real engine in live bookings (over HTTP on the loopback), with a fake session that
    does what buyrig.py --live does (a LiveBuyer: sign with the MockSigner, broadcast, receipt)."""

    def setUp(self):
        super().setUp()
        now = time.time()
        self.eclock = bt.Clock(bb.window_start(now) - 3600 + 60)       # the engine books the hour that just ended
        self.engine = bt.bookings_engine(self.tmp, self.chain, self.eclock)
        bt.feed(self.engine, [(4, 0)] * 3)
        self.chain.ts = int(now)
        bt.close_hour(self.engine, self.eclock)
        self.booking, = bt.records(self.engine, 'booking')
        self.tok = 'rig-token-' + 'z' * 24
        self.srv = bb.serve_status(self.engine, '127.0.0.1', STATUS_PORT, self.tok)
        self.url = f'http://127.0.0.1:{STATUS_PORT}/status'
        self.launched = []

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def fake_session(self, crash_at_broadcast=False):
        def launch(r, seed, live=False):
            self.launched.append((r['window'], r['eth_in'], live))
            if not live:
                return None
            b = self.buyer(window=r['window'], amount=bb.parse_eth(r['eth_in']))
            b.preflight()
            if crash_at_broadcast:
                def die(raw):
                    raise Crash('the child died at the broadcast')
                b.rpc.send_raw = die
                try:
                    b.sign_and_send(self.data(bb.parse_eth(r['eth_in'])), int(time.time()) + 1200)
                except Crash:
                    return None                                     # the child is gone: no result
            sent = b.sign_and_send(self.data(bb.parse_eth(r['eth_in'])), int(time.time()) + 1200)
            info = b.wait_receipt()
            return {'ok': bool(info and info['ok']), 'mode': 'LIVE', 'verdict': 'live_bought', 'window': r['window'],
                    'tx': sent['tx'], 'signed': True, 'sent': True, 'block': info['block'],
                    'labrat_out': bb.token_str(info['labrat_out_wei']), 'session_at': bb.iso(), 'targets_hit': 3,
                    'misses': 1, 'checks_passed': 16, 'checks_total': 16, 'simulation': 'ok',
                    'session_proof': hashlib.sha256(b'live').hexdigest(), 'run_dir': 'runs/buyrig_live_x'}
        return launch

    def runner(self, launch, report=None, state='runner.json'):
        return rn.Runner(self.url, os.path.join(self.tmp, state), launch, lambda d: True,
                         report or rn.make_report(rn.default_report_url(self.url), self.tok), fetch=rn.fetch_json,
                         live=rn.LiveHooks(self.rpc(), self.journal, self.logs.append))

    def test_a_booked_buy_runs_once_live_and_is_verified_by_the_engine(self):
        run = self.runner(self.fake_session())
        handled = run.poll_once()
        w = bb.iso(self.booking['window'])
        self.assertEqual(handled, [f'live|{w}|0.001'])
        self.assertEqual(self.launched, [(w, '0.001', True)], 'run with --live and its window')
        st = rn.fetch_json(self.url)
        row = st['buys']['recent'][0]
        txh = self.recs('signed')[0]['tx']
        self.assertEqual((row['state'], row['tx'], row['simulated']), ('executed', txh, False))
        self.assertEqual(row['pons']['label'], bb.EXECUTED_LABEL)
        self.assertEqual((row['pons']['replay'], row['pons']['checks']), ('MATCH', '16/16'))
        self.assertTrue(run.state['live'][w]['reported'])
        self.assertEqual(run.state['seen'][f'live|{w}|0.001']['state'], 'done')
        for _ in range(2):
            self.assertEqual(run.poll_once(), [])
        run2 = self.runner(self.fake_session())                       # a restart: the same state file
        self.assertEqual(run2.poll_once(), [])
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(len(self.signer.signed), 1)

    def test_a_report_the_engine_cannot_verify_yet_is_retried(self):
        answers = []
        real = rn.make_report(rn.default_report_url(self.url), self.tok)

        def report(body):
            answers.append(body['tx'])
            if len(answers) == 1:
                return 503, {'error': 'the chain has no receipt for that transaction yet; report it again'}
            return real(body)
        run = self.runner(self.fake_session(), report=report)
        run.poll_once()
        w = bb.iso(self.booking['window'])
        self.assertFalse(run.state['live'][w]['reported'])
        self.assertEqual(rn.fetch_json(self.url)['buys']['recent'][0]['state'], 'booked')
        run.poll_once()
        self.assertTrue(run.state['live'][w]['reported'])
        self.assertEqual(rn.fetch_json(self.url)['buys']['recent'][0]['state'], 'executed')
        self.assertEqual(len(set(answers)), 1, 'the same transaction, reported again')

    def test_a_crashed_session_is_resolved_and_reported(self):
        run = self.runner(self.fake_session(crash_at_broadcast=True))
        run.poll_once()
        w = bb.iso(self.booking['window'])
        self.assertEqual(self.journal.state().failures, 0, 'a signed window is decided by its receipt, not counted')
        self.assertEqual(rn.fetch_json(self.url)['buys']['recent'][0]['state'], 'booked')
        for _ in range(3):                         # resolve: the identical bytes again, then the receipt, the report
            run.poll_once()
        self.assertEqual(rn.fetch_json(self.url)['buys']['recent'][0]['state'], 'executed')
        self.assertTrue(run.state['live'][w]['reported'])
        self.assertEqual(len(self.signer.signed), 1, 'never signed again')
        self.assertEqual(len(self.launched), 1, 'never run again')

    def test_a_failed_session_counts_and_a_stopped_live_skips(self):
        run = self.runner(lambda r, seed, live=False: self.launched.append(r['window']) or None)
        run.poll_once()
        w = bb.iso(self.booking['window'])
        st = self.journal.state()
        self.assertEqual((st.failures, st.windows[w]['failed']['consecutive']), (1, 1))
        # the next hour's booking, with LIVE stopped meanwhile: skipped, not run
        bl.stop(self.journal, 'a check failed', w, 'check', log=self.logs.append)
        bt.feed(self.engine, [(4, 0)] * 3, start_n=50)
        self.eclock.t = bb.window_start(time.time()) + 60
        self.chain.ts = int(time.time())
        bt.close_hour(self.engine, self.eclock)          # books the current hour
        run.clock = lambda: time.time() + 3600           # an hour later: the current hour's buy is the signable one
        handled = run.poll_once()
        self.assertEqual(len(handled), 1)
        self.assertEqual(len(self.launched), 1, 'not run')
        skipped = run.state['seen'][handled[0]]
        self.assertEqual(skipped['state'], 'skipped')
        self.assertIn('LIVE is stopped', skipped['why'])

    def test_child_processes_and_the_launch_command(self):
        env = {**key_env('11' * 32), 'PATH': 'x'}
        self.assertNotIn(bl.ENV_KEY, rn.child_env(False, env), 'a simulated session never gets the key')
        self.assertEqual(rn.child_env(True, env)[bl.ENV_KEY], '0x' + '11' * 32)
        calls = []
        real_run = rn.subprocess.run

        def fake_run(cmd, **kw):
            calls.append((cmd, kw.get('env')))
            return SimpleNamespace(returncode=2, stdout='BUYRIG_RESULT {}')
        rn.subprocess.run = fake_run
        old_key = os.environ.get(bl.ENV_KEY)
        os.environ[bl.ENV_KEY] = '0x' + '22' * 32
        try:
            launch = rn.make_launch()
            row = {'at': bb.iso(), 'eth_in': '0.001', 'window': self.window, 'hits_covered': 12}
            launch(row, 5, live=True)
            launch(row, 5)
        finally:
            rn.subprocess.run = real_run
            if old_key is None:
                os.environ.pop(bl.ENV_KEY, None)
            else:
                os.environ[bl.ENV_KEY] = old_key
        (live_cmd, live_env), (dry_cmd, dry_env) = calls
        self.assertEqual(live_cmd[-3:], ['--live', '--window', self.window])
        self.assertNotIn('--live', dry_cmd)
        self.assertEqual(live_env[bl.ENV_KEY], '0x' + '22' * 32)
        self.assertNotIn(bl.ENV_KEY, dry_env)

    def test_reportable_takes_a_live_buy_that_was_signed_and_sent(self):
        good = {'mode': 'LIVE', 'ok': True, 'signed': True, 'sent': True, 'tx': '0x' + 'ab' * 32, 'labrat_out': '3.49',
                'verdict': 'live_bought'}
        self.assertIsNone(rn.reportable(good))
        for over in ({'signed': False}, {'sent': False}, {'tx': None}, {'ok': False}, {'dev_oracle': True}):
            self.assertIsNotNone(rn.reportable({**good, **over}), over)
        self.assertIsNotNone(rn.reportable({**good, 'mode': 'DRY'}), 'a DRY session never signs')


# ---------------------------------------------------------------------------------------------------- BURNS (mocked)
WALLET_LABRAT = 10_000_000 * 10 ** 18         # what the fake buyback wallet holds
BURN_WEI = 500_000 * 10 ** 18                 # the booked burn in these tests: exactly 5 % of it
SUPPLY = 10 ** 27                             # 1,000,000,000 LABRAT


class BurnChain(bt.FakeChain):
    """FakeChain plus the LABRAT token as a burnable ERC-20: its bytecode (PUSH4 selectors), balanceOf, totalSupply,
    burn(uint256) (when has_burn) and transfer(address,uint256), with the Transfer log a burn (to the zero address)
    or a transfer to dead leaves in its receipt. eth_call reverts as the real token does (checked 2026-09-26)."""

    def __init__(self, has_burn=True):
        super().__init__()
        self.has_burn = has_burn
        self.token_balances = {bl.WALLET: WALLET_LABRAT}
        self.total_supply = SUPPLY
        self.burn_reverts = None              # a revert every burn() / transfer() gets (a paused token)

    def code(self):
        sels = ['70a08231', 'a9059cbb', '18160ddd'] + (['42966c68'] if self.has_burn else [])
        return '0x6080604052' + ''.join('63' + s + '14' for s in sels) + '00'

    def __call__(self, method, params, all_rpcs_on_error=False):
        if method == 'eth_getCode':
            self.log.append((method, to_checksum_address(params[0]), ''))
            return (self.code() if to_checksum_address(params[0]) == bb.TOKEN else '0x'), None
        if method == 'eth_estimateGas' and to_checksum_address(params[0].get('to')) == bb.TOKEN:
            self.log.append((method, bb.TOKEN, params[0].get('data', '')[:10]))
            _res, err = self._token_call(params[0])
            if err:
                return None, err
            return ('0x84b5' if params[0]['data'][:10] == bl.SEL_BURN else '0xc9b1'), None   # as on chain
        return super().__call__(method, params, all_rpcs_on_error)

    def _token_call(self, c):
        data, sel = c['data'], c['data'][:10]
        frm = to_checksum_address(c['from']) if c.get('from') else None
        if sel == bl.SEL_BALANCE_OF:
            return bt.W(self.token_balances.get(to_checksum_address('0x' + data[-40:]), 0)), None
        if sel == '0x18160ddd':
            return bt.W(self.total_supply), None
        if (sel == bl.SEL_BURN and self.has_burn) or sel == bl.SEL_TRANSFER:
            if int(c.get('value', '0x0'), 16):
                return None, bt.rev('0x')
            if self.burn_reverts:
                return None, self.burn_reverts
            method, to, amount = bl.decode_burn(data)
            if method == 'transfer' and to == bb.ZERO:
                return None, bt.rev('0xec442f05' + bt.W(0)[2:])                   # ERC20InvalidReceiver(0)
            have = self.token_balances.get(frm, 0)
            if amount > have:
                return None, bt.rev('0xe450d38c' + bt.W(bt.A(frm), have, amount)[2:])   # ERC20InsufficientBalance
            return (bt.W(1) if method == 'transfer' else '0x'), None
        return None, bt.rev('0x')                                                 # no such function

    def _call(self, c, override, log=True):
        if to_checksum_address(c['to']) == bb.TOKEN and c['data'][:10] != bb.SEL['curve']:
            return self._token_call(c)
        return super()._call(c, override, log)

    def mine(self, txh, tx=None):
        tx = tx or self.pending_txs.pop(txh)
        self.pending_txs.pop(txh, None)
        if to_checksum_address(tx['to']) != bb.TOKEN:
            return super().mine(txh, tx)
        frm = to_checksum_address(tx.get('from') or bb.WALLET)
        method, to, amount = bl.decode_burn(tx['data'])
        logs, status = [], 1
        gas_used = 33_900 if method == 'burn' else 51_500
        if (int(tx['value']) or amount > self.token_balances.get(frm, 0) or (method == 'burn' and not self.has_burn)
                or self.burn_reverts):
            status = 0
        else:
            self.token_balances[frm] -= amount
            if method == 'burn':
                self.total_supply -= amount
                dest = bb.ZERO
            else:
                dest = to
                self.token_balances[dest] = self.token_balances.get(dest, 0) + amount
            logs.append({'address': bb.TOKEN, 'topics': [bb.TRANSFER_TOPIC, '0x' + bb.pad_addr(frm),
                                                         '0x' + bb.pad_addr(dest)], 'data': bt.W(amount)})
        self._debit(frm, gas_used * self.gas_price)
        self.nonce += 1
        self.block += 1
        self.receipts[txh] = {'transactionHash': txh, 'status': hex(status), 'gasUsed': hex(gas_used),
                              'effectiveGasPrice': hex(self.gas_price), 'blockNumber': hex(self.block), 'logs': logs,
                              'from': frm.lower(), 'to': bb.TOKEN.lower()}
        self.mined[txh] = {'hash': txh, 'from': frm.lower(), 'to': bb.TOKEN.lower(), 'value': hex(int(tx['value'])),
                           'input': tx['data'], 'nonce': hex(int(tx['nonce'])), 'blockNumber': hex(self.block)}


class BurnBase(LiveBase):
    """LiveBase on a BurnChain (the token is burnable, the wallet holds 10M LABRAT and 1 ETH, nonce 0)."""

    def setUp(self):
        super().setUp()
        self.chain = self.fresh_chain()

    def fresh_chain(self, has_burn=True):
        c = BurnChain(has_burn)
        c.buyback_balance = 10 ** 18
        c.nonce = bl.FIRST_NONCE
        return c

    def burner(self, window=None, amount=BURN_WEI, method='auto', signer=None, journal=None, chain=None, **kw):
        kw.setdefault('sleep', lambda s: None)
        kw.setdefault('receipt_wait_s', 0.3)
        return bl.LiveBurner(signer or self.signer, self.rpc(chain), journal or self.journal, window or self.window,
                             amount, method, log=self.logs.append, **kw)

    def burn_env(self, key_hex):
        return {bl.ENV_BURN_LIVE: '1', bl.ENV_BURN_CONFIRM: 'LABRAT', bl.ENV_KEY: '0x' + key_hex}

    def run_burn(self, env, window='default', amount=BURN_WEI, method='auto', chain=None, journal=None, log=None):
        ch = chain or self.chain
        return bl.run_burn(self.window if window == 'default' else window, amount, method, environ=env,
                           gate_rpc=bb.ReadRpc(ch), log=log or self.logs.append, transport=ch, nodes=[ch],
                           journal=journal or self.journal, sleep=lambda s: None, receipt_wait_s=0.3)


class TestBurnTx(BurnBase):
    def test_the_token_method_and_the_calldata(self):
        rpc = self.rpc()
        self.assertEqual(bl.token_burn_method(rpc, 'auto'), 'burn', 'the token dispatches burn(uint256): preferred')
        self.assertEqual(bl.token_burn_method(rpc, 'burn'), 'burn')
        self.assertEqual(bl.token_burn_method(rpc, 'transfer'), 'transfer')
        plain = self.fresh_chain(has_burn=False)
        self.assertEqual(bl.token_burn_method(self.rpc(plain), 'auto'), 'transfer')
        with self.assertRaises(bl.LiveRefused) as cm:
            bl.token_burn_method(self.rpc(plain), 'burn')
        self.assertEqual(cm.exception.kind, 'check')
        with self.assertRaises(bl.LiveRefused):
            bl.token_burn_method(rpc, 'incinerate')
        self.assertTrue(bl.token_has_burn('0x6342966c6814'))
        self.assertFalse(bl.token_has_burn('0x42966c68'), 'not a PUSH4: the bytes in some data')
        self.assertFalse(bl.token_has_burn(None))
        # the calldata, both ways
        self.assertEqual(bl.decode_burn(bl.cd_burn(BURN_WEI)), ('burn', None, BURN_WEI))
        self.assertEqual(bl.decode_burn(bl.cd_transfer(bl.DEAD, BURN_WEI)), ('transfer', bl.DEAD, BURN_WEI))
        self.assertEqual(bl.burn_calldata('burn', 7), '0x42966c68' + '0' * 63 + '7')
        self.assertEqual(bl.burn_calldata('transfer', 7), bl.cd_transfer(bl.DEAD, 7))
        for bad in (bl.cd_burn(1) + 'ff', bl.cd_burn(1)[:-2], bb.SEL['execute'] + '0' * 64, '0x79cc6790' + '0' * 128,
                    'zz', '0x', None, 42):
            with self.assertRaises(ValueError, msg=str(bad)[:24]):
                bl.decode_burn(bad)
        with self.assertRaises(ValueError):
            bl.burn_calldata('auto', 1)
        tx = bl.build_burn_tx(3, bl.cd_burn(BURN_WEI), 42_467, 88_000_000)
        self.assertEqual(tx, {'chainId': 4663, 'nonce': 3, 'to': bb.TOKEN, 'value': 0, 'data': bl.cd_burn(BURN_WEI),
                              'gas': 42_467, 'maxFeePerGas': 88_000_000, 'maxPriorityFeePerGas': 0, 'type': 2})
        checks = bl.check_burn_tx(tx, BURN_WEI, 'burn', bl.WALLET)
        self.assertEqual(set(checks), set(bl.BURN_CHECKS))
        self.assertTrue(all(v['ok'] for v in checks.values()), checks)

    def test_every_tampered_burn_transaction_is_refused(self):
        good = bl.build_burn_tx(0, bl.cd_burn(BURN_WEI), 42_467, 88_000_000)
        cases = [
            # (name, tx, booked amount, decided method, checks that must fail)
            ('to: the router', dict(good, to=bb.ROUTER), BURN_WEI, 'burn', {'token'}),
            ('to: missing', {k: v for k, v in good.items() if k != 'to'}, BURN_WEI, 'burn', {'token'}),
            ('value: 1 wei', dict(good, value=1), BURN_WEI, 'burn', {'no_value'}),
            ('amount: twice the booked', dict(good, data=bl.cd_burn(2 * BURN_WEI)), BURN_WEI, 'burn', {'amount'}),
            ('amount: the booking doubled', good, 2 * BURN_WEI, 'burn', {'amount'}),
            ('amount: over the absolute ceiling', dict(good, data=bl.cd_burn(bl.MAX_BURN_WEI + 1)),
             bl.MAX_BURN_WEI + 1, 'burn', {'amount'}),
            ('amount: dust', dict(good, data=bl.cd_burn(1)), 1, 'burn', {'amount'}),
            ('method: a transfer when burn was decided', dict(good, data=bl.cd_transfer(bl.DEAD, BURN_WEI)),
             BURN_WEI, 'burn', {'method'}),
            ('method: a burn when transfer was decided', good, BURN_WEI, 'transfer', {'method'}),
            ('transfer: to another address', dict(good, data=bl.cd_transfer(OTHER, BURN_WEI)), BURN_WEI, 'transfer',
             {'recipient'}),
            ('transfer: to the zero address', dict(good, data=bl.cd_transfer(bb.ZERO, BURN_WEI)), BURN_WEI,
             'transfer', {'recipient'}),
            ('selector: burnFrom', dict(good, data='0x79cc6790' + '0' * 128), BURN_WEI, 'burn',
             {'method', 'amount', 'recipient'}),
            ('trailing bytes', dict(good, data=bl.cd_burn(BURN_WEI) + 'ab'), BURN_WEI, 'burn', {'method'}),
            ('calldata: not hex', dict(good, data='0xzz'), BURN_WEI, 'burn', {'method', 'amount'}),
            ('chain 1', dict(good, chainId=1), BURN_WEI, 'burn', {'chain'}),
            ('a priority fee', dict(good, maxPriorityFeePerGas=1), BURN_WEI, 'burn', {'chain'}),
            ('a legacy transaction', dict(good, type=0), BURN_WEI, 'burn', {'chain'}),
        ]
        for name, tx, amount, method, want in cases:
            with self.subTest(name):
                res = bl.check_burn_tx(tx, amount, method, bl.WALLET)
                failed = {k for k, v in res.items() if not v['ok']}
                self.assertTrue(want <= failed, f'{name}: failed {failed}, expected {want}')
        self.assertFalse(bl.check_burn_tx(good, BURN_WEI, 'burn', OTHER)['wallet']['ok'], 'another signer')
        self.assertTrue(all(v['ok'] for v in bl.check_burn_tx(good, BURN_WEI, 'burn').values()))


class TestLiveBurner(BurnBase):
    def test_one_burn_journalled_before_it_is_broadcast_and_verified(self):
        seen = {}

        def on_send(raw, txh):                        # at the broadcast: 'reserved' and 'signed' are already on disk
            with open(self.journal.path, encoding='utf-8') as f:
                lines = [json.loads(ln) for ln in f]
            seen['evs'] = [(r['ev'], r.get('kind')) for r in lines]
            seen['raw'], seen['txh'] = raw, txh
        self.chain.on_send = on_send
        b = self.burner()
        self.assertEqual(b.preflight(), {'nonce': 0})
        sent = b.sign_and_send()
        self.assertEqual(seen['evs'], [('reserved', 'burn'), ('signed', 'burn')], 'reserved, signed, then broadcast')
        self.assertEqual((sent['tx'], sent['broadcast'], sent['error']), (seen['txh'], True, None))
        self.assertTrue(self.journal.lock_path(self.window, 'burn').exists())
        self.assertFalse(self.journal.lock_path(self.window).exists(), "the hour's BUY slot is untouched")
        tx, = self.signer.signed                      # ONE transaction: to the token, no ETH, burn(amount)
        gp = self.chain.gas_price
        self.assertEqual(tx, {'chainId': 4663, 'nonce': 0, 'to': bb.TOKEN, 'value': 0, 'data': bl.cd_burn(BURN_WEI),
                              'gas': -(-33_973 * 5 // 4), 'maxFeePerGas': min(2 * gp, bl.MAX_FEE_CAP_WEI),
                              'maxPriorityFeePerGas': 0, 'type': 2})
        self.assertTrue(all(v['ok'] for v in b.checks.values()))
        info = b.wait_receipt()
        self.assertTrue(info['ok'])
        self.assertEqual((info['burned_wei'], info['burn_to']), (BURN_WEI, 'zero'))
        self.assertEqual(self.chain.token_balances[bl.WALLET], WALLET_LABRAT - BURN_WEI)
        self.assertEqual(self.chain.total_supply, SUPPLY - BURN_WEI, 'a real burn: the supply shrank')
        st = self.journal.state()
        self.assertTrue(st.burned(self.window))
        self.assertFalse(st.bought(self.window))
        self.assertEqual((st.expected_nonce(), st.failures), (1, 0))
        self.assertEqual(st.executed(), [], 'not a buy')
        self.assertEqual(st.executed_burns(), [{'window': self.window, 'tx': sent['tx'], 'amount_wei': BURN_WEI,
                                                'burned_wei': BURN_WEI, 'method': 'burn', 'block': self.chain.block,
                                                'signed_at': self.recs('signed')[0]['at']}])
        self.assertEqual(st.burned_total_wei(), BURN_WEI)
        self.assertEqual([(r['ev'], r['kind']) for r in self.recs()],
                         [(ev, 'burn') for ev in ('reserved', 'signed', 'sent', 'receipt')])
        self.assertNotIn('raw', json.dumps(bl.status(self.journal)))
        row, = bl.status(self.journal)['windows']
        self.assertEqual((row['kind'], row['state'], row['labrat'], row['method'], row['labrat_burned']),
                         ('burn', 'burned', '500000', 'burn', '500000'))
        self.assertEqual((bl.status(self.journal)['burns'], bl.status(self.journal)['burned_total_labrat']),
                         (1, '500000'))
        with self.assertRaises(bl.LiveRefused):          # this session had its one burn
            b.sign_and_send()
        self.assertEqual(len(self.signer.signed), 1)
        # a token without burn(): transfer to the dead address, the supply unchanged
        plain = self.fresh_chain(has_burn=False)
        j2 = bl.Journal(Path(self.tmp) / 'plain' / 'live_journal.jsonl')
        b2 = self.burner(journal=j2, chain=plain)
        b2.sign_and_send()
        self.assertEqual(self.signer.signed[-1]['data'], bl.cd_transfer(bl.DEAD, BURN_WEI))
        info2 = b2.wait_receipt()
        self.assertEqual((info2['ok'], info2['burn_to']), (True, 'dead'))
        self.assertEqual((plain.token_balances[bl.DEAD], plain.total_supply), (BURN_WEI, SUPPLY))
        self.assertEqual(j2.state().executed_burns()[0]['method'], 'transfer')

    def test_one_burn_per_window_across_restarts_and_beside_the_hours_buy(self):
        b = self.burner()
        b.sign_and_send()
        b.wait_receipt()
        for name in ('the next process', 'another one'):
            with self.subTest(name):
                with self.assertRaises(bl.LiveRefused) as cm:          # a restart: a new journal object, same file
                    self.burner(journal=bl.Journal(self.journal.path)).preflight()
                self.assertIn('already has its transaction', str(cm.exception))
                self.assertEqual(cm.exception.kind, 'blocked')
        # the same hour's BUY is another slot: signed at the next nonce; then neither can be signed again
        buyer = self.buyer(journal=bl.Journal(self.journal.path))
        self.assertEqual(buyer.preflight(), {'nonce': 1})
        buyer.sign_and_send(self.data(), int(time.time()) + 1200)
        self.assertTrue(buyer.wait_receipt()['ok'])
        st = self.journal.state()
        self.assertTrue(st.bought(self.window) and st.burned(self.window))
        self.assertEqual(st.expected_nonce(), 2)
        self.assertEqual(sorted(st.windows), sorted([self.window, f'burn:{self.window}']))
        self.assertEqual(len(st.executed()), 1)
        self.assertEqual(len(st.executed_burns()), 1)
        for make in (self.buyer, self.burner):
            with self.assertRaises(bl.LiveRefused):
                make(journal=bl.Journal(self.journal.path)).preflight()
        # the lock file alone refuses (a journal line lost), the journal line alone refuses (a lock file lost), and a
        # reserved burn does not touch the hour's buy slot
        w2 = live_window(hours_ahead=1)
        clock2 = lambda: time.time() + 3600           # noqa: E731
        fresh = self.fresh_chain()
        j2 = bl.Journal(Path(self.tmp) / 'j2' / 'live_journal.jsonl')
        j2.lock_dir.mkdir(parents=True)
        j2.lock_path(w2, 'burn').write_text('{}', encoding='utf-8')
        with self.assertRaises(bl.LiveRefused) as cm:
            self.burner(window=w2, journal=j2, chain=fresh, clock=clock2).preflight()
        self.assertIn('already has its transaction', str(cm.exception))
        j3 = bl.Journal(Path(self.tmp) / 'j3' / 'live_journal.jsonl')
        j3.append({'ev': 'reserved', 'kind': 'burn', 'window': w2, 'amount_wei': BURN_WEI})
        with self.assertRaises(bl.LiveRefused):
            self.burner(window=w2, journal=j3, chain=fresh, clock=clock2).preflight()
        self.assertEqual(bl.LiveBuyer(self.signer, self.rpc(fresh), j3, w2, LIVE_WEI, clock=clock2).preflight(),
                         {'nonce': 0}, "a reserved burn leaves the hour's buy slot free")
        # two processes past the preflight at once: the exclusive lock lets only one reserve the burn
        j4 = bl.Journal(Path(self.tmp) / 'j4' / 'live_journal.jsonl')
        j4.reserve(w2, {'amount_wei': BURN_WEI}, 'burn')
        with self.assertRaises(bl.LiveRefused):
            j4.reserve(w2, {'amount_wei': BURN_WEI}, 'burn')
        j4.reserve(w2, {'amount_wei': LIVE_WEI})                    # the buy slot of the same hour is its own
        self.assertEqual(len(self.signer.signed), 2)
        self.assertEqual(len(self.chain.sent), 2)

    def test_a_crash_between_signing_and_broadcasting_a_burn(self):
        """The process dies after 'signed' is on disk and before eth_sendRawTransaction: the next start sends the
        IDENTICAL bytes, never signs again, and meanwhile nothing else (not even the hour's buy) is signed."""
        b = self.burner()

        def die(raw):
            raise Crash('killed at the broadcast')
        b.rpc.send_raw = die
        with self.assertRaises(Crash):
            b.sign_and_send()
        self.assertEqual([(r['ev'], r['kind']) for r in self.recs()], [('reserved', 'burn'), ('signed', 'burn')])
        self.assertEqual(self.chain.sent, [], 'nothing left the machine')
        signed_raw = self.recs('signed')[0]['raw']
        j = bl.Journal(self.journal.path)
        with self.assertRaises(bl.LiveRefused) as cm:            # a BUY's preflight resolves the burn first
            self.buyer(journal=j).preflight()
        self.assertIn('not resolved yet', str(cm.exception))
        self.assertEqual(self.chain.sent, [signed_raw], 'the identical signed bytes, nothing else')
        out = bl.resolve(j, self.rpc(), self.logs.append)
        self.assertEqual(out['resolved'], [f'burn:{self.window}'])
        st = j.state()
        self.assertTrue(st.burned(self.window))
        self.assertEqual(st.unresolved(), {})
        self.assertEqual(len(self.signer.signed), 1, 'never signed again')
        self.assertEqual([r['ev'] for r in self.recs(journal=j)], ['reserved', 'signed', 'rebroadcast', 'receipt'])
        with self.assertRaises(bl.LiveRefused):
            self.burner(journal=j).preflight()                     # the burn window is used for good
        self.assertEqual(self.buyer(journal=j).preflight(), {'nonce': 1}, "and the hour's buy may go now")
        # a crash after the broadcast: finished by its receipt, no re-broadcast
        c2 = self.fresh_chain()
        c2.auto_mine = False
        j2 = bl.Journal(Path(self.tmp) / 'j2' / 'live_journal.jsonl')
        b2 = self.burner(journal=j2, chain=c2)
        sent = b2.sign_and_send()
        out = bl.resolve(j2, self.rpc(c2), self.logs.append)
        self.assertEqual(out['pending'], [f'burn:{self.window}'], 'in the mempool: wait')
        c2.mine(sent['tx'])
        out = bl.resolve(j2, self.rpc(c2), self.logs.append)
        self.assertEqual(out['resolved'], [f'burn:{self.window}'])
        self.assertEqual(len(c2.sent), 1)
        self.assertTrue(j2.state().burned(self.window))

    def test_tampered_amounts_are_refused_and_live_stops(self):
        # over 5 % of the wallet's LABRAT balance (read at signing): a check, nothing reserved, LIVE stops
        over = WALLET_LABRAT * 5 // 100 + 10 ** 18
        b = self.burner(amount=over)
        with self.assertRaises(bl.LiveRefused) as cm:
            b.sign_and_send()
        self.assertEqual(cm.exception.kind, 'check')
        self.assertIn('over 5% of the wallet', str(cm.exception))
        self.assertEqual((self.signer.signed, self.chain.sent, self.recs()), ([], [], []))
        bl.note_refusal(self.journal, self.window, cm.exception, self.logs.append, 'burn')
        self.assertEqual(self.journal.state().stop['kind'], 'check')
        with self.assertRaises(bl.LiveRefused):
            self.buyer().preflight()                    # one stop for buys and burns
        # exactly 5 % passes the ceiling (a fresh journal)
        self.journal = bl.Journal(Path(self.tmp) / 'five' / 'live_journal.jsonl')
        b = self.burner(amount=WALLET_LABRAT * 5 // 100)
        b.sign_and_send()
        self.assertTrue(b.wait_receipt()['ok'])
        # the rig's own bounds and gates at construction
        for bad in (0, -1, bl.MIN_BURN_WEI - 1, bl.MAX_BURN_WEI + 1):
            with self.assertRaises(bl.LiveRefused, msg=bad):
                bl.LiveBurner(self.signer, self.rpc(), self.journal, self.window, bad)
        with self.assertRaises(bl.LiveRefused):
            bl.LiveBurner(self.signer, self.rpc(), self.journal, self.window, BURN_WEI, 'incinerate')
        with self.assertRaises(bl.LiveRefused):
            bl.LiveBurner(bt.MockSigner(bb.WALLET), self.rpc(), self.journal, self.window, BURN_WEI)
        with self.assertRaises(bl.LiveRefused):
            bl.LiveBurner(self.signer, bb.ReadRpc(self.chain), self.journal, self.window, BURN_WEI)
        # a build that comes out tampered (the amount, the target, the value, the recipient): refused before the
        # reservation, nothing signed, LIVE stops
        real_build = bl.build_burn_tx
        tampers = [
            ('amount', lambda tx: dict(tx, data=bl.cd_burn(2 * BURN_WEI))),
            ('token', lambda tx: dict(tx, to=bb.ROUTER)),
            ('value', lambda tx: dict(tx, value=10 ** 15)),
            ('recipient', lambda tx: dict(tx, data=bl.cd_transfer(OTHER, BURN_WEI))),
        ]
        try:
            for i, (name, tamper) in enumerate(tampers):
                with self.subTest(name):
                    bl.build_burn_tx = lambda *a, t=tamper: t(real_build(*a))
                    c = self.fresh_chain()
                    j = bl.Journal(Path(self.tmp) / f'tamper{i}' / 'live_journal.jsonl')
                    b = self.burner(journal=j, chain=c)
                    with self.assertRaises(bl.LiveRefused) as cm:
                        b.sign_and_send()
                    self.assertEqual(cm.exception.kind, 'check')
                    self.assertIn('failed its checks', str(cm.exception))
                    self.assertIn(name, str(cm.exception))
                    self.assertEqual(c.sent, [])
                    self.assertEqual(self.recs(journal=j), [], 'nothing reserved')
                    bl.note_refusal(j, self.window, cm.exception, self.logs.append, 'burn')
                    self.assertEqual(j.state().stop['kind'], 'check')
        finally:
            bl.build_burn_tx = real_build
        self.assertEqual(len(self.signer.signed), 1)
        # a token that reverts the burn (paused): a check failure too (nothing reserved)
        c = self.fresh_chain()
        c.burn_reverts = bt.rev('0xd93c0665', 'paused')
        with self.assertRaises(bl.LiveRefused) as cm:
            self.burner(journal=bl.Journal(Path(self.tmp) / 'paused' / 'j.jsonl'), chain=c).sign_and_send()
        self.assertEqual(cm.exception.kind, 'check')
        self.assertEqual(c.sent, [])

    def test_a_burn_that_never_landed_expires_and_two_failures_stop_live(self):
        self.chain.reject_sends = {'code': -32000, 'message': 'insufficient funds for gas * price + value'}
        b = self.burner()
        sent = b.sign_and_send()
        self.assertIs(sent['broadcast'], False)
        self.assertIsNone(b.wait_receipt(0))
        out = bl.resolve(self.journal, self.rpc(), self.logs.append)     # within its time to live: the same bytes
        self.assertEqual(out['pending'], [f'burn:{self.window}'])
        self.assertEqual(self.chain.sent[0], self.chain.sent[1])
        self.chain.ts = int(time.time()) + bl.BURN_TTL_S + bl.EXPIRE_MARGIN_S + 5
        out = bl.resolve(self.journal, self.rpc(), self.logs.append)
        self.assertEqual(out['resolved'], [f'burn:{self.window}'])
        st = self.journal.state()
        self.assertEqual((st.unresolved(), st.failures, st.expected_nonce()), ({}, 1, 0))
        self.assertTrue(st.windows[f'burn:{self.window}']['expired'])
        self.assertEqual(st.executed_burns(), [])
        # the next hour: no ETH for gas -> a failed burn, the second in a row: LIVE stops (for buys too)
        self.chain.reject_sends = None
        self.chain.ts = int(time.time())
        self.chain.buyback_balance = 0
        w2 = live_window(hours_ahead=1)
        b2 = self.burner(window=w2, clock=lambda: time.time() + 3600)
        with self.assertRaises(bl.LiveRefused) as cm:
            b2.sign_and_send()
        self.assertEqual(cm.exception.kind, 'failure')
        self.assertIn('under', str(cm.exception))
        bl.note_refusal(self.journal, w2, cm.exception, self.logs.append, 'burn')
        st = self.journal.state()
        self.assertEqual((st.failures, st.stop['kind']), (2, 'failures'))
        self.assertIn('a burn', st.stop['why'])
        with self.assertRaises(bl.LiveRefused) as cm:
            self.buyer(window=live_window(hours_ahead=2), clock=lambda: time.time() + 7200).preflight()
        self.assertIn('LIVE is stopped', str(cm.exception))
        self.assertEqual(len(self.signer.signed), 1)
        # the operator clears the stop and funds the wallet: a mined burn resets the count
        self.assertTrue(bl.clear_stop(self.journal, st.stop['id'], log=self.logs.append))
        self.chain.buyback_balance = 10 ** 18
        b3 = self.burner(window=live_window(hours_ahead=2), clock=lambda: time.time() + 7200)
        b3.sign_and_send()
        self.assertTrue(b3.wait_receipt()['ok'])
        self.assertEqual(self.journal.state().failures, 0)

    def test_the_receipt_rules(self):
        base = {'status': '0x1', 'blockNumber': '0x10', 'gasUsed': '0x8400', 'effectiveGasPrice': '0x1', 'logs': [],
                'from': bl.WALLET.lower(), 'to': bb.TOKEN.lower()}

        def transfer(frm, to, amount):
            return {'address': bb.TOKEN, 'topics': [bb.TRANSFER_TOPIC, '0x' + bb.pad_addr(frm), '0x' + bb.pad_addr(to)],
                    'data': bt.W(amount)}
        ok_zero = dict(base, logs=[transfer(bl.WALLET, bb.ZERO, BURN_WEI)])
        ok_dead = dict(base, logs=[transfer(bl.WALLET, bl.DEAD, BURN_WEI)])
        self.assertEqual((bl.read_burn_receipt(ok_zero, BURN_WEI)['ok'], bl.read_burn_receipt(ok_zero, BURN_WEI)['burn_to']),
                         (True, 'zero'))
        self.assertEqual((bl.read_burn_receipt(ok_dead, BURN_WEI)['ok'], bl.read_burn_receipt(ok_dead, BURN_WEI)['burn_to']),
                         (True, 'dead'))
        self.assertTrue(bl.read_burn_receipt(ok_zero)['ok'], 'no signed amount known: any burn from the wallet')
        bad = [
            ('reverted', dict(ok_zero, status='0x0')),
            ('mined without a Transfer', base),
            ('not the signed amount', dict(base, logs=[transfer(bl.WALLET, bb.ZERO, BURN_WEI - 1)])),
            ('moved to a wallet, not burned', dict(base, logs=[transfer(bl.WALLET, OTHER, BURN_WEI)])),
            ('burned from another wallet', dict(base, logs=[transfer(OTHER, bb.ZERO, BURN_WEI)])),
            ('another token', dict(base, logs=[dict(transfer(bl.WALLET, bb.ZERO, BURN_WEI), address=bb.CURVE)])),
            ('to another contract', dict(ok_zero, to=bb.ROUTER.lower())),
            ('from another address', dict(ok_zero, **{'from': OTHER.lower()})),
        ]
        for name, rc in bad:
            with self.subTest(name):
                self.assertFalse(bl.read_burn_receipt(rc, BURN_WEI)['ok'])
        # booked: mined without the burn is a broken rule (stop); a revert is a failed burn
        j = bl.Journal(Path(self.tmp) / 'rc' / 'live_journal.jsonl')
        signed = {'tx': '0x' + 'cd' * 32, 'amount_wei': BURN_WEI}
        bl.record_receipt(j, self.window, signed, base, self.logs.append, 'burn')
        self.assertEqual(j.state().stop['kind'], 'check')
        j2 = bl.Journal(Path(self.tmp) / 'rc2' / 'live_journal.jsonl')
        j2.append({'ev': 'signed', 'kind': 'burn', 'window': self.window, **signed, 'raw': '0x', 'nonce': 0})
        bl.record_receipt(j2, self.window, signed, dict(ok_zero, status='0x0'), self.logs.append, 'burn')
        st = j2.state()
        self.assertEqual((st.failures, st.expected_nonce(), st.burned(self.window)), (1, 1, False))


class TestBurnGatesAndTheChild(BurnBase):
    def test_every_gate_else_the_burn_is_a_no_op(self):
        acct = Account.create()                      # a key made here: never funded, never the buyback wallet's
        key = bytes(acct.key).hex()
        other = bytes(Account.create().key).hex()
        good = self.burn_env(key)
        bl.WALLET = acct.address                     # this test's stand-in for the pinned wallet
        wrong_chain = self.fresh_chain()
        wrong_chain.chain_id = 1
        cases = [
            ('no window', dict(good), dict(window=None), '--window was not given'),
            ('a window that is not an hour', dict(good), dict(window='2026-09-25T20:30:00Z'), 'UTC hour'),
            ('BURN_LIVE missing', {k: v for k, v in good.items() if k != bl.ENV_BURN_LIVE}, {}, 'BURN_LIVE is not 1'),
            ('BURN_LIVE=0', dict(good, **{bl.ENV_BURN_LIVE: '0'}), {}, 'BURN_LIVE is not 1'),
            ('BURN_CONFIRM missing', {k: v for k, v in good.items() if k != bl.ENV_BURN_CONFIRM}, {}, 'BURN_CONFIRM'),
            ('BURN_CONFIRM=labrat', dict(good, **{bl.ENV_BURN_CONFIRM: 'labrat'}), {}, 'BURN_CONFIRM'),
            ('the BUY switches alone', {**key_env(key)}, {}, 'BURN_LIVE is not 1'),
            ('no key', {k: v for k, v in good.items() if k != bl.ENV_KEY}, {}, 'is not set'),
            ('a key that does not parse', dict(good, **{bl.ENV_KEY: 'not-a-key'}), {}, 'does not parse'),
            ('the key of another wallet', dict(good, **{bl.ENV_KEY: other}), {}, 'not the key of the pinned'),
            ('chain id 1', dict(good), dict(chain=wrong_chain), 'chain id 1'),
        ]
        for name, env, kw, why in cases:
            with self.subTest(name):
                res = self.run_burn(env, **kw)
                self.assertEqual((res['mode'], res['verdict'], res['ok'], res['signed'], res['sent'], res['tx']),
                                 ('OFF', 'burn_off', False, False, False, None))
                self.assertIn(why, res['error'])
                self.assertNotIn(bl.ENV_KEY, env, 'the key leaves the environment whatever the outcome')
                self.assertNotIn(key, json.dumps(res).lower())
                self.assertNotIn(other, json.dumps(res).lower())
                self.assertEqual(self.recs(), [], 'nothing journalled')
        methods = {m for m, _t, _s in self.chain.log} | {m for m, _t, _s in wrong_chain.log}
        self.assertTrue(methods <= {'eth_chainId'}, f'a no-op reads nothing but the chain id: {methods}')
        self.assertEqual((self.chain.sent, EXTRA_TRAPS['signs'], bt.TRAPS['signs']), ([], [], []))
        # every gate holds but LIVE is stopped: refused (blocked), nothing signed, the stop is booked in the result
        bl.stop(self.journal, 'an operator must look', self.window, 'check', log=self.logs.append)
        res = self.run_burn(dict(good))
        self.assertEqual((res['mode'], res['verdict'], res['refusal_kind'], res['signed']),
                         ('LIVE', 'live_refused', 'blocked', False))
        self.assertIn('LIVE is stopped', res['error'])
        self.assertEqual(self.chain.sent, [])
        # the runner's hooks: the burn gates alone open the burn side only, the buy gates the buy side only, both both
        hooks, why = rn.live_hooks(dict(good), self.logs.append)
        self.assertEqual((hooks.buys, hooks.burns, why), (False, True, ''))
        self.assertIn('BUYRIG_LIVE', hooks.why_buys)
        hooks, _ = rn.live_hooks({**key_env(key)}, self.logs.append)
        self.assertEqual((hooks.buys, hooks.burns), (True, False))
        hooks, _ = rn.live_hooks({**key_env(key), **good}, self.logs.append)
        self.assertEqual((hooks.buys, hooks.burns), (True, True))
        hooks, why = rn.live_hooks({bl.ENV_KEY: '0x' + key}, self.logs.append)
        self.assertIsNone(hooks)
        self.assertIn('burns: BURN_LIVE is not 1', why)
        # the CLI with no gates in the process environment: the same no-op, exit 1, a result line without the key
        for k in (bl.ENV_BURN_LIVE, bl.ENV_BURN_CONFIRM, bl.ENV_KEY):
            self.assertNotIn(k, os.environ)
        buf, old = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            rc = bl.main(['--burn', '--window', self.window, '--amount-wei', str(BURN_WEI), '--journal',
                          str(self.journal.path)])
        finally:
            sys.stdout = old
        line = next(ln for ln in buf.getvalue().splitlines() if ln.startswith('BURN_RESULT '))
        res = json.loads(line[len('BURN_RESULT '):])
        self.assertEqual((rc, res['mode'], res['verdict']), (1, 'OFF', 'burn_off'))
        self.assertIn('BURN_LIVE is not 1', res['error'])
        for argv in (['--burn', '--window', self.window], ['--burn', '--amount-wei', '5'],
                     ['--burn', '--window', self.window, '--amount-wei', '0'],
                     ['--burn', '--window', self.window, '--amount-wei', 'lots']):
            with self.assertRaises(SystemExit):
                bl.main(argv)
        self.assertEqual(bt.TRAPS['launcher_rpc'], 0, 'a no-op never touched the real chain')

    def test_the_child_burns_once_and_the_key_is_in_no_output(self):
        """The whole burn path with a key made here (never funded) in the environment: the gates, the signer, a real
        signature, the fake chain, the receipt, the result, the journal, the status, the runner's report. The key's
        hex appears nowhere, and a second child for the same window is refused without signing."""
        acct = Account.create()
        key = bytes(acct.key).hex()
        bl.WALLET = acct.address
        self.chain.accept_signed = True
        self.chain.token_balances[acct.address] = WALLET_LABRAT
        env = self.burn_env(key)
        trap = LocalAccount.sign_transaction
        out_buf, err_buf = io.StringIO(), io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        LocalAccount.sign_transaction = _REAL_SIGN_TX
        logs = []
        try:
            sys.stdout, sys.stderr = out_buf, err_buf
            res = self.run_burn(env, log=lambda m: logs.append(m) or print(m))
            print(res)
            b = bl.LiveBurner(acct, self.rpc(), bl.Journal(Path(self.tmp) / 'r' / 'j.jsonl'), self.window, BURN_WEI)
            print(b, repr(b), b.redact(f'a line that somehow holds the key {key} and 0x{key.upper()}'))
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            LocalAccount.sign_transaction = trap
        self.assertEqual((res['ok'], res['mode'], res['verdict'], res['signed'], res['sent'], res['method']),
                         (True, 'LIVE', 'burned', True, True, 'burn'), res)
        self.assertTrue(bl.HASH_RE.match(res['tx']))
        self.assertEqual((res['burned'], res['amount'], res['amount_wei'], res['block']),
                         (bb.eth_str(BURN_WEI, 18), bb.eth_str(BURN_WEI, 18), str(BURN_WEI), self.chain.block))
        self.assertNotIn(bl.ENV_KEY, env)
        # the raw transaction: the real EIP-1559 format, signed by the key made here, burn(amount) on the token
        raw = self.recs('signed')[0]['raw']
        d = self.chain.decode_raw(raw)
        self.assertEqual((d['from'], d['to'], d['value'], d['data'], d['chainId'], d['nonce']),
                         (acct.address, bb.TOKEN, 0, bl.cd_burn(BURN_WEI), 4663, 0))
        self.assertEqual(self.chain.total_supply, SUPPLY - BURN_WEI)
        report = rn.burn_report_body(self.journal.state().executed_burns()[0])
        self.assertEqual({k: report[k] for k in ('window', 'tx', 'amount', 'amount_wei', 'method')},
                         {'window': self.window, 'tx': res['tx'], 'amount': bb.eth_str(BURN_WEI, 18),
                          'amount_wei': str(BURN_WEI), 'method': 'burn'})
        texts = {'stdout': out_buf.getvalue(), 'stderr': err_buf.getvalue(),
                 'journal': self.journal.path.read_text(encoding='utf-8'),
                 'locks': ' '.join(p.read_text(encoding='utf-8') for p in self.journal.lock_dir.iterdir()),
                 'result': json.dumps(res), 'logs': json.dumps(logs, default=str), 'report': json.dumps(report),
                 'status': json.dumps(bl.status(self.journal))}
        for where, text in texts.items():
            self.assertNotIn(key, text.lower(), f'the key is in the {where}')
        self.assertIn('<redacted>', texts['stdout'])
        self.assertIn(res['tx'], texts['journal'])
        # the second child for the same window (a restart, the booking still listed): refused, nothing signed again
        res2 = self.run_burn(self.burn_env(key), journal=bl.Journal(self.journal.path))
        self.assertEqual((res2['mode'], res2['verdict'], res2['refusal_kind'], res2['signed'], res2['tx']),
                         ('LIVE', 'live_refused', 'blocked', False, None))
        self.assertIn('already has its transaction', res2['error'])
        self.assertEqual(len(self.chain.sent), 1)
        self.assertEqual(self.journal.state().failures, 0, 'a blocked refusal is not a failed burn')


class TestBurnRunner(BurnBase):
    """The runner's burn side against a fake burn engine (fetch injected), with a fake child that does what
    `buyrig_live.py --burn` does (a LiveBurner on the fake chain) and the buyback engine simulated (DRY) beside it."""

    BURN_URL = 'https://burn.example/status'

    def setUp(self):
        super().setUp()
        self.launched, self.burn_launched, self.answers = [], [], []
        self.buy_status = status([])
        self.burn_rows = []
        # live/burn.py's public status: mode / simulated / bookings (the outstanding ones) / recent at the top level
        self.burn_status = {'mode': 'LIVE', 'simulated': False, 'bookings': self.burn_rows, 'recent': self.burn_rows}
        self.reads = []
        self.now = time.time                          # the runner's and the fake child's clock (advanced together)

    def advance(self, run, hours):
        self.now = lambda: time.time() + 3600 * hours
        run.clock = self.now

    def fetch(self, url):
        self.reads.append(url)
        return self.burn_status if url == self.BURN_URL else self.buy_status

    def fake_child(self, crash_at_broadcast=False, no_op=False):
        def launch(row):
            self.burn_launched.append((row['window'], row['amount_wei']))
            if no_op:
                return bl.burn_result(row['window'], row['amount_wei'], verdict='burn_off', error='BURN_LIVE is not 1')
            try:
                b = self.burner(window=row['window'], amount=row['amount_wei'], clock=lambda: self.now())
                b.preflight()
                if crash_at_broadcast:
                    def die(raw):
                        raise Crash('the child died at the broadcast')
                    b.rpc.send_raw = die
                    try:
                        b.sign_and_send()
                    except Crash:
                        return None                                     # the child is gone: no result
                sent = b.sign_and_send()
            except bl.LiveRefused as e:                # as run_burn does: a check stops LIVE, a failure counts
                bl.note_refusal(self.journal, row['window'], e, self.logs.append, 'burn')
                return bl.burn_result(row['window'], row['amount_wei'], mode='LIVE', verdict='live_refused',
                                      refusal_kind=e.kind, error=str(e)[:400])
            info = b.wait_receipt()
            return bl.burn_result(row['window'], row['amount_wei'], mode='LIVE', ok=bool(info and info['ok']),
                                  signed=True, sent=True, tx=sent['tx'], block=info and info['block'], method='burn',
                                  burned=bb.eth_str(info['burned_wei'], 18) if info else None,
                                  verdict='burned' if info and info['ok'] else 'reverted')
        return launch

    def report(self, body):
        self.answers.append(body)
        return 200, {'ok': True}

    def hooks(self, buys=True, burns=True):
        return rn.LiveHooks(self.rpc(), self.journal, self.logs.append, buys=buys, burns=burns)

    def runner(self, launch=None, report=None, state='runner.json', burn_url=BURN_URL, hooks='default'):
        return rn.Runner('https://engine.example/status', os.path.join(self.tmp, state),
                         lambda r, s, live=False: self.launched.append(r) or None, lambda d: True, lambda b: None,
                         fetch=self.fetch, live=self.hooks() if hooks == 'default' else hooks,
                         burn_status_url=burn_url, burn_launch=launch or self.fake_child(),
                         burn_report=self.report if report is None else report)

    def book(self, window=None, wei=BURN_WEI, **over):
        row = {'window': window or self.window, 'state': 'booked', 'simulated': False,
               'amount': bb.eth_str(wei, 18), 'at': bb.iso()}
        row.update(over)
        self.burn_rows.insert(0, row)                 # newest first, as an engine lists them
        return row

    def test_a_booked_burn_runs_once_and_is_reported(self):
        self.book()
        run = self.runner()
        handled = run.poll_once()
        k = f'burn|{self.window}|{BURN_WEI}'
        self.assertEqual(handled, [k])
        self.assertEqual(self.burn_launched, [(self.window, BURN_WEI)])
        signed = self.recs('signed')[0]
        txh = signed['tx']
        self.assertEqual(self.answers, [{'window': self.window, 'tx': txh, 'amount': bb.eth_str(BURN_WEI, 18),
                                         'amount_wei': str(BURN_WEI), 'method': 'burn', 'signed_at': signed['at']}])
        self.assertTrue(bb.ISO_RE.match(signed['at']))
        self.assertTrue(run.state['burns'][self.window]['reported'])
        ent = run.state['seen'][k]
        self.assertEqual((ent['state'], ent['tx'], ent['burn'], ent['method']), ('done', txh, True, 'burn'))
        for _ in range(2):
            self.assertEqual(run.poll_once(), [])
        run2 = self.runner()                                        # a restart: the same state file
        self.assertEqual(run2.poll_once(), [])
        # a lost state file: the journal still refuses a second burn for the window (the child is refused, blocked)
        run3 = self.runner(state='runner_lost.json')
        self.assertEqual(run3.poll_once(), [k])
        self.assertEqual(run3.state['seen'][k]['state'], 'failed')
        self.assertIn('already has its transaction', run3.state['seen'][k]['why'])
        self.assertEqual(len(self.burn_launched), 2)
        self.assertEqual(len(self.signer.signed), 1, 'one burn per window, whatever the runner forgot')
        self.assertEqual({a['tx'] for a in self.answers}, {txh}, 'only that one burn was ever reported')
        self.assertEqual(self.journal.state().failures, 0)
        self.assertEqual(self.launched, [], 'the DRY buy side had nothing to do')

    def test_a_report_the_burn_engine_cannot_verify_yet_is_retried_and_a_refusal_is_final(self):
        self.book()

        def report(body):
            self.answers.append(body)
            return (503, {'error': 'no receipt for that transaction yet'}) if len(self.answers) == 1 else (200, {})
        run = self.runner(report=report)
        run.poll_once()
        self.assertFalse(run.state['burns'][self.window]['reported'])
        run.poll_once()
        self.assertTrue(run.state['burns'][self.window]['reported'])
        self.assertEqual(len({a['tx'] for a in self.answers}), 1, 'the same transaction, reported again')
        self.assertEqual(len(self.answers), 2)
        # a 400 is final: not retried (the later hours book less: 5 % of what is left after each burn)
        w2 = live_window(hours_ahead=1)
        self.book(window=w2, wei=400_000 * 10 ** 18)
        self.advance(run, 1)
        run.burn_report = lambda body: self.answers.append(body) or (400, {'error': 'no such booking'})
        run.poll_once()
        run.poll_once()
        self.assertTrue(run.state['burns'][w2]['report_final'])
        self.assertFalse(run.state['burns'][w2]['reported'])
        self.assertEqual(len(self.answers), 3)
        # reporting off: booked as such, once
        run.burn_report = None
        w3 = live_window(hours_ahead=2)
        self.book(window=w3, wei=300_000 * 10 ** 18)
        self.advance(run, 2)
        run.poll_once()
        self.assertEqual(run.state['burns'][w3]['why'], 'reporting is off')
        self.assertEqual(len(self.signer.signed), 3)
        self.assertEqual(self.chain.token_balances[bl.WALLET], WALLET_LABRAT - 1_200_000 * 10 ** 18)
        # a booking over 5 % of what the wallet holds NOW is a check failure in the child: LIVE stops
        w4 = live_window(hours_ahead=3)
        self.book(window=w4, wei=500_000 * 10 ** 18)              # 5 % of 8.8M is 440,000
        self.advance(run, 3)
        run.poll_once()
        st = self.journal.state()
        self.assertEqual(st.stop['kind'], 'check')
        self.assertIn('over 5% of the wallet', st.stop['why'])
        self.assertEqual(len(self.signer.signed), 3, 'nothing signed')

    def test_a_crashed_child_is_resolved_and_reported_never_run_again(self):
        self.book()
        run = self.runner(launch=self.fake_child(crash_at_broadcast=True))
        run.poll_once()
        self.assertEqual(self.journal.state().failures, 0, 'a signed burn is decided by its receipt, not counted')
        self.assertEqual(self.answers, [])
        for _ in range(3):                         # resolve: the identical bytes again, then the receipt, the report
            run.poll_once()
        self.assertTrue(run.state['burns'][self.window]['reported'])
        self.assertEqual(len(self.signer.signed), 1, 'never signed again')
        self.assertEqual(len(self.burn_launched), 1, 'never run again')
        self.assertEqual(len(self.chain.sent), 1)

    def test_failed_children_count_and_a_stopped_live_skips(self):
        self.book()
        run = self.runner(launch=self.fake_child(no_op=True))
        run.poll_once()
        st = self.journal.state()
        self.assertEqual(st.failures, 1)
        self.assertIn('a gate did not hold in the child', st.windows[f'burn:{self.window}']['failed']['why'])
        w2 = live_window(hours_ahead=1)
        self.book(window=w2)
        self.advance(run, 1)
        run.burn_launch = lambda row: None                    # a child that gives nothing at all
        run.poll_once()
        st = self.journal.state()
        self.assertEqual((st.failures, st.stop['kind']), (2, 'failures'))
        w3 = live_window(hours_ahead=2)
        self.book(window=w3)
        self.advance(run, 2)
        n = len(self.burn_launched)
        handled = run.poll_once()
        self.assertEqual(len(handled), 1)
        self.assertEqual(len(self.burn_launched), n, 'not run: LIVE is stopped')
        self.assertIn('LIVE is stopped', run.state['seen'][handled[0]]['why'])
        self.assertEqual(self.signer.signed, [])

    def test_only_a_live_burn_engine_only_valid_rows_and_only_with_the_gates(self):
        self.book()
        run = self.runner()
        for mode, simulated in (('DRY', True), ('LIVE', True), ('DRY', False)):
            self.burn_status['mode'], self.burn_status['simulated'] = mode, simulated
            self.assertEqual(run.poll_once(), [], (mode, simulated))
        self.burn_status['mode'], self.burn_status['simulated'] = 'LIVE', False
        self.burn_rows[:] = []
        for over in ({'state': 'executed'}, {'simulated': True}, {'window': '2026-09-25T20:30:00Z'},
                     {'amount': '0'}, {'amount': '1e5'}, {'amount': bb.eth_str(bl.MAX_BURN_WEI + 1, 18)},
                     {'amount': None, 'amount_wei': 'abc'}, {'amount': None, 'amount_wei': -5},
                     {'amount': None, 'amount_wei': True}):
            self.book(**over)
        self.assertEqual(run.poll_once(), [])
        self.assertEqual(self.burn_launched, [])
        self.assertEqual(rn.burn_row_wei({'amount_wei': 7, 'amount': '1'}), 7, 'amount_wei wins')
        self.assertEqual(rn.burn_row_wei({'amount_wei': '70'}), 70)
        self.assertEqual(rn.burn_row_wei({'amount': '1.5'}), 15 * 10 ** 17)
        self.assertIsNone(rn.burn_row_wei({}))
        self.assertTrue(rn.valid_burn_row({'window': self.window, 'state': 'booked', 'simulated': False,
                                           'amount_wei': str(BURN_WEI)}))
        # too late: skipped, not run
        self.burn_rows[:] = []
        self.book(window=bb.iso(bb.window_start(time.time()) - 3 * 3600))
        handled = run.poll_once()
        self.assertEqual(run.state['seen'][handled[0]]['state'], 'skipped')
        self.assertEqual(self.burn_launched, [])
        # a booking listed in both lists is one booking; a status with a "burns" section is read the same way
        self.burn_rows[:] = []
        row = self.book()
        self.burn_status = {'mode': 'LIVE', 'burns': {'simulated': False, 'recent': [row], 'bookings': [dict(row)]}}
        self.assertEqual(run.poll_once(), [f'burn|{self.window}|{BURN_WEI}'])
        self.assertEqual(len(self.burn_launched), 1)
        self.assertEqual(len(self.signer.signed), 1)
        self.burn_status = {'mode': 'LIVE', 'simulated': False, 'bookings': self.burn_rows, 'recent': self.burn_rows}
        self.burn_rows[:] = []
        self.signer.signed.clear()
        self.burn_launched.clear()
        # without the burn gates (buys only), without a burn URL, or with no hooks at all (DRY): the burn engine is
        # never even read, and the buy side runs exactly as before
        self.burn_rows[:] = []
        self.book()
        self.reads[:] = []
        for hooks, url in ((self.hooks(burns=False), self.BURN_URL), (self.hooks(), None), (None, self.BURN_URL)):
            run = self.runner(hooks=hooks, burn_url=url, state=f'r_{id(hooks)}_{bool(url)}.json')
            self.assertEqual(run.poll_once(), [])
        self.assertEqual(self.reads, ['https://engine.example/status'] * 3)
        self.assertEqual(self.burn_launched, [])
        self.assertEqual(self.signer.signed, [])

    def test_a_buy_and_a_burn_of_one_hour_run_in_turn_on_one_nonce_account(self):
        """Both engines booked the hour: the buy runs first, then the burn, each at the next nonce, both reported,
        never at the same time (one session lock, and the journal signs nothing while anything is unresolved)."""
        order = []
        self.buy_status = {'mode': 'LIVE', 'label': bb.LIVE_BOOKINGS_LABEL,
                           'buys': {'count': 1, 'simulated': False, 'recent': [
                               {'at': bb.iso(), 'window': self.window, 'eth_in': '0.001', 'labrat_out': None,
                                'simulated': False, 'state': 'booked'}]}}
        self.book()
        reports = []

        def buy_launch(r, seed, live=False):
            self.assertTrue(live)
            self.assertTrue(run._session.locked(), 'the session lock is held while a child runs')
            order.append(('buy', r['window']))
            b = self.buyer(window=r['window'], amount=bb.parse_eth(r['eth_in']))
            b.preflight()
            sent = b.sign_and_send(self.data(bb.parse_eth(r['eth_in'])), int(time.time()) + 1200)
            info = b.wait_receipt()
            return {'ok': True, 'mode': 'LIVE', 'verdict': 'live_bought', 'window': r['window'], 'tx': sent['tx'],
                    'signed': True, 'sent': True, 'block': info['block'],
                    'labrat_out': bb.token_str(info['labrat_out_wei']), 'session_at': bb.iso(), 'targets_hit': 3,
                    'misses': 1, 'checks_passed': 16, 'checks_total': 16, 'simulation': 'ok',
                    'session_proof': hashlib.sha256(b'live').hexdigest(), 'run_dir': 'runs/buyrig_live_x'}
        child = self.fake_child()

        def burn_launch(row):
            self.assertTrue(run._session.locked())
            order.append(('burn', row['window']))
            return child(row)
        run = rn.Runner('https://engine.example/status', os.path.join(self.tmp, 'runner.json'), buy_launch,
                        lambda d: True, lambda body: reports.append(('buy', body)) or (200, {}), fetch=self.fetch,
                        live=self.hooks(), burn_status_url=self.BURN_URL, burn_launch=burn_launch,
                        burn_report=lambda body: reports.append(('burn', body)) or (200, {}))
        handled = run.poll_once()
        self.assertEqual(handled, [f'live|{self.window}|0.001', f'burn|{self.window}|{BURN_WEI}'])
        self.assertEqual(order, [('buy', self.window), ('burn', self.window)], 'the buy first, then the burn')
        self.assertEqual([tx['nonce'] for tx in self.signer.signed], [0, 1])
        self.assertEqual([tx['to'] for tx in self.signer.signed], [bb.ROUTER, bb.TOKEN])
        st = self.journal.state()
        self.assertTrue(st.bought(self.window) and st.burned(self.window))
        self.assertEqual(st.expected_nonce(), 2)
        self.assertEqual([k for k, _b in reports], ['buy', 'burn'])
        self.assertEqual(reports[1][1], rn.burn_report_body(st.executed_burns()[0]))
        self.assertEqual((reports[1][1]['tx'], reports[1][1]['amount_wei']), (self.recs('signed')[1]['tx'], str(BURN_WEI)))
        self.assertEqual(run.poll_once(), [])
        self.assertEqual(len(self.signer.signed), 2)
        # a burn cannot start while the lock is held by a buy session (a second thread): it waits, never overlaps
        run2 = self.runner(state='runner2.json')
        w2 = live_window(hours_ahead=1)
        self.book(window=w2, wei=100_000 * 10 ** 18)
        self.advance(run2, 1)
        run2._session.acquire()
        t = threading.Thread(target=run2.poll_once, daemon=True)
        t.start()
        t.join(0.5)
        self.assertTrue(t.is_alive(), 'the burn waits for the session lock')
        self.assertEqual(len(self.burn_launched), 1)
        run2._session.release()
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(self.burn_launched), 2)

    def test_the_child_command_and_its_environment(self):
        calls = []
        real_run = rn.subprocess.run

        def fake_run(cmd, **kw):
            calls.append((cmd, kw.get('env')))
            with open(cmd[cmd.index('--result-json') + 1], 'w', encoding='utf-8') as fh:
                json.dump(bl.burn_result(self.window, BURN_WEI, verdict='burn_off', error='BURN_LIVE is not 1'), fh)
            return SimpleNamespace(returncode=1, stdout='BURN_RESULT {}')
        rn.subprocess.run = fake_run
        old_key = os.environ.get(bl.ENV_KEY)
        os.environ[bl.ENV_KEY] = '0x' + '22' * 32
        try:
            res = rn.make_burn_launch('auto')({'window': self.window, 'amount_wei': BURN_WEI})
        finally:
            rn.subprocess.run = real_run
            if old_key is None:
                os.environ.pop(bl.ENV_KEY, None)
            else:
                os.environ[bl.ENV_KEY] = old_key
        (cmd, env), = calls
        self.assertEqual(cmd[1:-1], [str(LIVE_DIR / 'buyrig_live.py'), '--burn', '--window', self.window,
                                     '--amount-wei', str(BURN_WEI), '--burn-method', 'auto', '--result-json'])
        self.assertEqual(env[bl.ENV_KEY], '0x' + '22' * 32, 'the burn child gets the key, like a LIVE buy session')
        self.assertEqual(res['verdict'], 'burn_off')
        self.assertEqual(rn.default_burn_report_url(self.BURN_URL), 'https://burn.example/burn_report')
        self.assertEqual(rn.default_report_url('https://engine.example/status'), 'https://engine.example/pons_session')
        self.assertEqual(rn.burn_key({'window': self.window, 'amount': '1'}), f'burn|{self.window}|{10 ** 18}')
        # the report body: what live/burn.py's REPORT_KEYS take, the transfer method spelled 'dead' there
        ex = {'window': self.window, 'tx': '0x' + 'ab' * 32, 'amount_wei': BURN_WEI, 'burned_wei': BURN_WEI,
              'method': 'transfer', 'block': 1, 'signed_at': '2026-09-26T14:00:31Z'}
        self.assertEqual(rn.burn_report_body(ex), {'window': self.window, 'tx': ex['tx'],
                                                   'amount': bb.eth_str(BURN_WEI, 18), 'amount_wei': str(BURN_WEI),
                                                   'method': 'dead', 'signed_at': '2026-09-26T14:00:31Z'})
        self.assertEqual(set(rn.burn_report_body({**ex, 'method': None, 'signed_at': None})),
                         {'window', 'tx', 'amount', 'amount_wei'})


# ---------------------------------------------------------------------------------------------------- the real session
RESULTS = []


def check(cond, label, detail=''):
    ok = bool(cond)
    RESULTS.append((ok, label))
    print(('  PASS  ' if ok else '  FAIL  ') + label + (f'  ({detail})' if detail else ''), flush=True)
    return ok


def relay_argv(port):
    with open(ROOT / 'relay' / 'Procfile', encoding='utf-8') as f:
        line = next(ln for ln in f if ln.startswith('web:'))
    out = []
    for a in shlex.split(line[len('web:'):])[1:]:
        a = a.replace('$PORT', str(port))
        out.append('127.0.0.1' if a == '0.0.0.0' else a)
    return [sys.executable, '-m', 'uvicorn'] + out


class Viewer(threading.Thread):
    """A /live viewer of the local relay: keeps every pons text and frame."""

    def __init__(self, url):
        super().__init__(daemon=True)
        self.url, self.texts, self.frames, self.stop, self.error = url, [], [], threading.Event(), None

    def run(self):
        from websockets.sync.client import connect
        try:
            with connect(self.url, max_size=2 ** 22, open_timeout=10) as ws:
                while not self.stop.is_set():
                    try:
                        m = ws.recv(timeout=0.5)
                    except TimeoutError:
                        continue
                    if isinstance(m, bytes):
                        if m[:4] == b'PJPG':
                            self.frames.append(m)
                    else:
                        j = json.loads(m)
                        if j.get('channel') == 'pons':
                            self.texts.append(j)
        except Exception as e:
            self.error = f'{type(e).__name__}: {e}'


def real_session(a):
    """One real session on the real pons page, streamed to a local relay, recorded and replayed."""
    print('\n== one real session: the rat buys 0.0001 ETH of $LABRAT on the real pons page (nothing signed or sent)')
    bt.remove_network_sandbox()
    methods = []
    real_rpc = launcher.rpc

    def spy(method, params, *args, **kw):
        methods.append(method)
        if method in SEND_METHODS:
            raise AssertionError(f'{method} must never be called')
        return real_rpc(method, params, *args, **kw)
    launcher.rpc = spy
    token = 'local-' + secrets.token_urlsafe(24)
    logf = tempfile.NamedTemporaryFile('w', prefix='buyrig_relay_', suffix='.log', delete=False, encoding='utf-8')
    env = {k: v for k, v in os.environ.items() if k not in ('LABRAT_PUBLISH_TOKEN', 'LIVE_ORIGINS', 'SERVE_SITE')}
    env.update(LABRAT_PUBLISH_TOKEN=token, PYTHONUNBUFFERED='1')
    relay = subprocess.Popen(relay_argv(RELAY_PORT), cwd=str(ROOT / 'relay'), env=env, stdout=logf,
                             stderr=subprocess.STDOUT)
    viewer = None
    r = None
    try:
        for _ in range(150):
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{RELAY_PORT}/healthz', timeout=2) as h:
                    if h.status == 200:
                        break
            except OSError:
                time.sleep(0.2)
        viewer = Viewer(f'ws://127.0.0.1:{RELAY_PORT}/live')
        viewer.start()
        time.sleep(1.0)
        buyrig.load_brain('runs/final/steer.pt', 'runs/final/policy.pt')
        buyrig.configure(a.seed)
        r = asyncio.run(buyrig.run_session(AMOUNT, a.seed, relay=f'ws://127.0.0.1:{RELAY_PORT}/publish', token=token))
        time.sleep(2.5)
    finally:
        if viewer:
            viewer.stop.set()
            viewer.join(5)
        relay.terminate()
        try:
            relay.wait(10)
        except subprocess.TimeoutExpired:
            relay.kill()
        logf.close()
        launcher.rpc = real_rpc
    res = (r.result if r else None) or {}
    print('  result: ' + json.dumps({k: res.get(k) for k in ('ok', 'verdict', 'amount_eth', 'labrat_out', 'min_out',
                                                               'checks_passed', 'checks_total', 'simulation',
                                                               'targets_hit', 'misses', 'seconds', 'run_dir')}))
    check(r is not None and res.get('ok'), 'the session passed (every check, simulation ok, recorded, mask held)',
          f"verdict {res.get('verdict')}, failed {res.get('failed_checks')}, error {res.get('error')}")
    if r is None:
        return 1
    run_dir = ROOT / res.get('run_dir', 'missing')
    check(res.get('checks_passed') == res.get('checks_total') == len(buyrig.PUBLIC_CHECKS),
          f"{res.get('checks_passed')}/{res.get('checks_total')} checks passed")
    check(res.get('simulation') == 'ok' and res.get('tokens_out_at_least_quote') is True,
          'eth_call of the exact transaction succeeded at capture, and yields at least the fresh quote',
          f"{res.get('labrat_out')} LABRAT, gas {res.get('gas_estimate')}")
    cap = r.capture or {}
    check(cap.get('response_to_page') == {'error': {'code': 4001, 'message': buyrig.REFUSAL_MSG}},
          'pons got 4001 "Simulated buy: not signed"')
    check(cap.get('signed') is False and cap.get('broadcast') is False and not r.bot.signatures,
          'nothing signed', f'signature requests {len(r.bot.signatures)}')
    check(not any(m in SEND_METHODS for m in methods) and not getattr(r.bot, 'rpc_sends_blocked', 0),
          'no send method reached any RPC', f'{len(methods)} reads: {sorted(set(methods))}')
    check(len(r.sends) == 1, 'pons asked for exactly one transaction', f'{len(r.sends)}')
    check(EXTRA_TRAPS['signs'] == [] and bt.TRAPS['signs'] == [] and bt.TRAPS['env_opens'] == [],
          'the sign and .env traps never fired')
    f = (r.inspection or {}).get('fields') or {}
    check(f.get('to', '').lower() == bb.ROUTER.lower() and f.get('selector') == bb.SEL['execute']
          and f.get('commands') == '0x10' and f.get('actions') == '0x060c0f' and f.get('canonical'),
          'pons built execute(0x10 V4_SWAP: 0x06 0x0c 0x0f) to the pons router, byte-identical to cd_router_buy')
    check(f.get('amount_in') == f.get('value_wei') == AMOUNT_WEI, 'value == amountIn == 0.0001 ETH')
    rp = subprocess.run([sys.executable, str(ROOT / 'replay_session.py'), str(run_dir)], cwd=str(ROOT),
                        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=1800)
    last = (rp.stdout or '').strip().splitlines()[-1:] or ['']
    check(rp.returncode == 0 and last[0].strip() == 'MATCH', f'python replay_session.py {res.get("run_dir")} -> MATCH',
          last[0][:120])
    fj = json.loads((run_dir / 'frames.json').read_text(encoding='utf-8'))
    saved = [c for c in fj['checkpoints'] if c.get('saved')]
    check(len(saved) >= 3 and all(buyrig.audit_clean(c['audit_before']) and buyrig.audit_clean(c['audit_after'])
                                  for c in saved),
          f'{len(saved)} masked pons frames saved, each between two clean DOM audits (every 0x string, balance line '
          'and fee amount covered or hidden)', ', '.join(c['name'] for c in saved))
    check(all(c['audit_before'].get('masked', 0) >= 1 for c in saved), 'the mask was painting in every saved frame')
    check(not fj.get('breach') and r.audits.get('breaches', 0) == 0, 'no mask breach during the session',
          f"audits {dict(r.audits)}")
    for name in ('session.json', 'buyrig_result.json'):
        txt = (run_dir / name).read_text(encoding='utf-8')
        check(ADDR40.search(txt) is None, f'{name} carries no address')
    # the stream, as a viewer of the local relay received it
    types = [t['type'] for t in viewer.texts]
    hello = next((t for t in viewer.texts if t['type'] == 'pons_hello'), {})
    results = [t for t in viewer.texts if t['type'] == 'pons_result']
    check(viewer.error is None, 'the viewer stayed connected', str(viewer.error))
    check(hello.get('source') == 'buyrig' and hello.get('simulated') is True and hello.get('label') == 'Simulated',
          'the viewer got the pons_hello: source buyrig, "Simulated"')
    check(any(t['type'] == 'pons_step' and t.get('phase') == 'press' for t in viewer.texts),
          'the viewer got the steps', ', '.join(sorted({t.get('phase') for t in viewer.texts
                                                       if t['type'] == 'pons_step'} - {None})))
    check(any(x.get('kind') == 'tx' and x.get('ok') for x in results)
          and any(x.get('kind') == 'done' and x.get('ok') for x in results) and 'pons_bye' in types,
          'the viewer got the checked result, the final summary and the bye')
    check(not any(re.search(r'0x[0-9a-fA-F]{3,}', json.dumps(t)) for t in viewer.texts),
          'no text on the stream holds a 0x string')
    good = [fr for fr in viewer.frames if fr[4:6] == b'\xff\xd8' and fr[-2:] == b'\xff\xd9']
    check(len(viewer.frames) >= 20 and len(good) == len(viewer.frames), f'{len(viewer.frames)} masked frames streamed '
          '(b"PJPG" + JPEG), each between two clean audits', f"forwarded {r.audits.get('frames_forwarded')}, "
          f"withheld {r.audits.get('frames_withheld')}")
    if a.frames_out and viewer.frames:
        d = Path(a.frames_out)
        d.mkdir(parents=True, exist_ok=True)
        n = len(viewer.frames)
        for i in sorted({0, n // 4, n // 2, 3 * n // 4, n - 1}):
            (d / f'stream_{i:04d}.jpg').write_bytes(viewer.frames[i][4:])
        print(f'  stream frames for a look: {d}')
    return 0 if all(ok for ok, _ in RESULTS) else 1


def runner_readonly_check():
    """The runner against the production engine's public status, first start: it takes history, runs nothing."""
    print('\n== the runner against the public engine status (first start: history only)')
    tmp = tempfile.mkdtemp(prefix='buyrig_runner_')
    try:
        launched = []
        run = rn.Runner(rn.STATUS_URL, os.path.join(tmp, 'runner.json'),
                        lambda r, s: launched.append(r) or None, lambda d: True, lambda b: None)
        run.poll_once()
        check(run.state.get('baselined') and not launched, 'the runner read the live status and ran nothing',
              f"{len(run.state['seen'])} earlier batches taken as history")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--no-browser', action='store_true')
    ap.add_argument('--real-session', action='store_true')
    ap.add_argument('--only-real', action='store_true')
    ap.add_argument('--seed', type=int, default=2026)
    ap.add_argument('--frames-out', help='save a few of the streamed frames here')
    a, rest = ap.parse_known_args()
    OPTS['browser'] = not a.no_browser
    rc = 0
    if not a.only_real:
        bt.install_traps(network=False)
        install_sign_traps()
        prog = unittest.main(argv=[sys.argv[0]] + rest, exit=False, verbosity=2)
        rc = 0 if prog.result.wasSuccessful() else 1
        print(f"traps: .env opened {bt.TRAPS['env_opens']}, real signs {bt.TRAPS['signs'] + EXTRA_TRAPS['signs']}, "
              f"blocked network {bt.TRAPS['net']}, launcher.rpc calls {bt.TRAPS['launcher_rpc']}")
        if bt.TRAPS['env_opens'] or bt.TRAPS['signs'] or EXTRA_TRAPS['signs'] or bt.TRAPS['launcher_rpc']:
            rc = 1
    if a.real_session or a.only_real:
        if a.only_real:
            bt.install_traps(network=True)
            install_sign_traps()
        rc |= real_session(a)
        runner_readonly_check()
        n_bad = sum(1 for ok, _ in RESULTS if not ok)
        print(f'\nreal session: {len(RESULTS) - n_bad}/{len(RESULTS)} checks passed')
        rc |= 1 if n_bad else 0
    return rc


if __name__ == '__main__':
    sys.exit(main())
