"""Tests of the buy rig (live/buyrig.py), its runner (live/buyrig_runner.py) and the engine's report endpoint for it
(live/buyback.py POST /pons_session), plus one REAL buy session on the real pons page (nothing is signed or sent).

    python live/buyrig_test.py                     # the mocked tests + the stream mask on a synthetic page (Chromium)
    python live/buyrig_test.py --no-browser        # the mocked tests only
    python live/buyrig_test.py --real-session      # + one real session: the rat buys 0.0001 ETH of LABRAT on pons,
                                                   #   streamed to a LOCAL relay (127.0.0.1:4771), recorded, replayed
    python live/buyrig_test.py --only-real [--frames-out DIR]

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
import buyrig_runner as rn  # noqa: E402
import launcher  # noqa: E402
import ponsbot  # noqa: E402
from eth_abi import encode  # noqa: E402
from eth_utils import to_checksum_address  # noqa: E402

STATUS_PORT = 4772
RELAY_PORT = 4771
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
        src = (LIVE_DIR / 'buyrig.py').read_text(encoding='utf-8')
        for bad in ('sign_transaction', 'send_raw', 'LiveRpc', 'LiveLaunch', 'read_env_file', 'launcher.config(',
                    'unsafe_sign_hash', "'LIVE'"):
            self.assertNotIn(bad, src, f'buyrig.py mentions {bad}')


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
        for bad in ('0', '-0.0001', '0.000001', '0.02', 'abc', 'NaN', '0.000012345'):
            with self.assertRaises(ValueError):
                buyrig.parse_amount(bad)
        self.assertEqual(buyrig.parse_amount('0.00047'), ('0.00047', 47 * 10 ** 13))


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
        e.tick()
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
