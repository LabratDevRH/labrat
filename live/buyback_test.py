"""Tests of live/buyback.py (the $LABRAT rat buybacks), plus one real read-only DRY run.

    python live/buyback_test.py                 # the mocked tests (no network except 127.0.0.1, no .env, no key)
    python live/buyback_test.py --real-run      # + a real read-only DRY run (real relay and chain, then a local relay)
    python live/buyback_test.py --record 110    # record real episode messages for the local relay (publisher --dry-print)

NOTHING here reads .env, holds a real key or sends a transaction:
  * .env is trapped: opening any file named .env (builtins.open, io.open, os.open) raises and is recorded;
    launcher.read_env_file and launcher.config are tripwires; the LIVE gate tests swap buyback._env_file_reader for a
    fake that returns made-up values (a throwaway key from Account.create(), or no key)
  * eth_account's LocalAccount.sign_transaction is a tripwire: no real account ever signs. The LIVE sequencing tests
    use a MockSigner (the launch wallet's address, no key) whose "raw transactions" are JSON only the fake chain reads
  * the mocked tests' network is sandboxed: requests.post / Session.request raise, sockets may only connect to
    127.0.0.1 (the mock relay on port 4752, the status server on 4753); launcher.rpc is a tripwire, every chain call
    goes to FakeChain
  * --real-run reads the real chain (eth_call, eth_estimateGas and reads only: ReadRpc refuses anything else) and the
    public relay; the .env and signing traps stay on, and it asserts that no send method was ever called

What is covered: counting (hits, misses and wrong clicks; live / not live / TEST / other sources, validation, gaps,
frames, the lever task off by default; Rat Tiles' missed tiles from the episode or from its tile events, each tile
once), reconnect and restart de-dup (in memory, over a real websocket with a relay drop, and across a restart from the
journal), the reconnect backoff, the HOURLY rule (windows aligned to the UTC hour, buy = hourly budget x hit rate
rounded down to 8 decimals, the minimum buy, no attempts / no hits, a restart mid-hour, an hour closed late, a buy
that expires when the next hour closes first, a journal written before the hourly rule), the PREVIEW buy while the
budget is not set (marked, cut to the per-buy cap only, never using up a cap), the buyer (every DRY buy simulated from
the hand-funded buyback wallet, no creator-fee claim), every cap (per buy, hourly, daily, total, gas price, gas share,
the daily and total gas caps), the public status (window, last_window, next_buy_at, budget; "next buy" following the
hour, the caps and skips; no address; test streams marked; status.json retried and refreshed), value == amountIn, the
graduation / pin stops, DRY never touching .env or a send method, the budget flags, every LIVE gate (LIVE paused, a
budget and hour windows required, the fixed journal place, RATBRAIN_RPC, the nonce check against the chain,
--first-nonce, a replayed stop and --clear-stop, unreadable journal lines), and the LIVE executor (claim before buy,
write-ahead journal, one booking record per receipt so a crash cannot re-buy, nonces, receipts, unconfirmed ->
re-broadcast of the same bytes, dropped only when every RPC agrees twice, a lagging RPC, a nonce mismatch stop, excess
value never signed) against the fake chain with the mock signer.
"""
import argparse
import base64
import builtins
import hashlib
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
import unittest
import urllib.request
from types import SimpleNamespace

LIVE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(LIVE_DIR)
sys.path.insert(0, ROOT)
sys.path.insert(0, LIVE_DIR)

import launcher  # noqa: E402
import buyback as bb  # noqa: E402
from eth_abi import decode  # noqa: E402
from eth_utils import keccak, to_checksum_address  # noqa: E402

MOCK_RELAY_PORT = 4752
STATUS_PORT = 4753
REAL_STATUS_PORT = 4750
FAKE_RELAY_PORT = 4751
POOL_MANAGER = '0x8366a39CC670B4001A1121B8F6A443A643e40951'

# the research's own calldata (live/BUYBACK_RESEARCH.md, P3 and P1): the encoders must reproduce it byte for byte
RESEARCH_QUOTE = (
    '0xaa9d21cb00000000000000000000000000000000000000000000000000000000000000200000000000000000000000000000000000'
    '000000000000000000000000000000000000000000000000000000aca07fe3bc5ff3e7501ca1dccfcd937fd710680d00000000000000'
    '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
    '0000c8000000000000000000000000e5e702641ea86f4ae6cc3cdaed2b886f976be04400000000000000000000000000000000000000'
    '00000000000000000000000001000000000000000000000000000000000000000000000000000009184e72a000000000000000000000'
    '000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000000'
    '00')
RESEARCH_CURVE_BUY = (
    '0x59a87bc1000000000000000000000000000000000000000000000000000009184e72a0000000000000000000000000000000000000'
    '0000000000000000000000000000000000000000000000000000004c2661717b97cd23aa87fe29fe0c50cff2cbb893')
# the router buy of P3 (amount 1e13, minOut 349132310919230143372, deadline 0x6ab5a04c): 1124 bytes, kept as its sha256
RESEARCH_ROUTER_SHA256 = '8e5dfe9297ba923cd3f0044b5c49b9d6f3b3b9059e7591fdcf1d91c396ff004f'

# ---------------------------------------------------------------------------------------------------- sandbox
TRAPS = {'env_opens': [], 'signs': [], 'net': [], 'launcher_rpc': 0}


def _is_env(path):
    try:
        return os.path.basename(os.fspath(path)) == '.env'
    except TypeError:
        return False


_real_open, _real_io_open, _real_os_open = builtins.open, io.open, os.open
_real_connect = socket.socket.connect
_real_launcher_rpc = launcher.rpc


def _trap_open(file, *a, **k):
    if _is_env(file):
        TRAPS['env_opens'].append(str(file))
        raise PermissionError('buyback_test: .env must never be opened')
    return _real_open(file, *a, **k)


def _trap_os_open(path, *a, **k):
    if _is_env(path):
        TRAPS['env_opens'].append(str(path))
        raise PermissionError('buyback_test: .env must never be opened')
    return _real_os_open(path, *a, **k)


def _tripwire(name):
    def f(*a, **k):
        TRAPS['signs' if 'sign' in name else 'net'].append(name)
        raise AssertionError(f'buyback_test: {name} must never be called')
    return f


_SAVED = {}


def remove_network_sandbox():
    """The real run needs the network and the real launcher.rpc; the .env and signing traps stay."""
    import requests
    if _SAVED:
        requests.post = _SAVED['post']
        requests.Session.request = _SAVED['request']
    socket.socket.connect = _real_connect
    launcher.rpc = _real_launcher_rpc


def install_traps(network=False):
    """The .env and signing traps always; with network=False also the network sandbox (loopback only)."""
    builtins.open = _trap_open
    io.open = _trap_open
    os.open = _trap_os_open
    launcher.read_env_file = _tripwire('launcher.read_env_file')
    launcher.config = _tripwire('launcher.config')
    from eth_account.signers.local import LocalAccount
    LocalAccount.sign_transaction = _tripwire('LocalAccount.sign_transaction')
    LocalAccount.unsafe_sign_hash = _tripwire('LocalAccount.unsafe_sign_hash')
    if not network:
        import requests
        _SAVED.update(post=requests.post, request=requests.Session.request)
        requests.post = _tripwire('requests.post')
        requests.Session.request = _tripwire('requests.Session.request')

        def rpc_trip(*a, **k):
            TRAPS['launcher_rpc'] += 1
            raise AssertionError('buyback_test: launcher.rpc (the real chain) must not be used by mocked tests')
        launcher.rpc = rpc_trip

        def guarded_connect(self, address):
            host = address[0] if isinstance(address, tuple) else address
            if host not in ('127.0.0.1', 'localhost', '::1'):
                TRAPS['net'].append(str(address))
                raise ConnectionRefusedError(f'buyback_test: no network ({address})')
            return _real_connect(self, address)
        socket.socket.connect = guarded_connect


# ---------------------------------------------------------------------------------------------------- fakes
def W(*ints):
    return '0x' + ''.join(hex(int(i))[2:].rjust(64, '0') for i in ints)


def A(addr):
    return int(addr, 16)


def rev(data, msg='execution reverted'):
    return {'code': 3, 'message': msg, 'data': data}


def decode_router(data):
    commands, inputs, deadline = decode(['bytes', 'bytes[]', 'uint256'], bytes.fromhex(data[10:]))
    assert commands == bytes([0x10]), commands
    actions, params = decode(['bytes', 'bytes[]'], inputs[0])
    assert actions == bytes([0x06, 0x0C, 0x0F]), actions
    (key, zfo, amount_in, min_out, extra, hook_data), = decode(
        ['((address,address,uint24,int24,address),bool,uint128,uint128,uint256,bytes)'], params[0])
    settle = decode(['address', 'uint256'], params[1])
    take = decode(['address', 'uint256'], params[2])
    assert zfo is True and extra == 0 and hook_data == b''
    assert to_checksum_address(key[1]) == bb.TOKEN and to_checksum_address(key[4]) == bb.HOOK
    assert settle[1] == amount_in and to_checksum_address(take[0]) == bb.TOKEN and take[1] == min_out
    return amount_in, min_out, deadline


class FakeChain:
    """The pons contracts as far as buyback.py uses them, in memory. Callable as a launcher.rpc-style transport."""

    def __init__(self):
        self.chain_id = bb.CHAIN_ID
        self.block, self.ts = 71_720_000, int(time.time())
        self.gas_price = 44_000_000
        self.balance = 4_319_354_622_052_000                  # the launch wallet
        self.buyback_balance = 0                              # the hand-funded buyback wallet (not funded yet)
        self.froms = []                                       # (to, selector, from, override addresses) of eth_calls
        self.escrow = 1_166_469_550_052_640_996
        self.phase, self.graduated = 2, True
        self.token_curve, self.rec_token, self.rec_curve = bb.CURVE, bb.TOKEN, bb.CURVE
        self.fee_recipient, self.pair, self.buyback_enabled, self.hook = bb.WALLET, bb.ZERO, 0, bb.HOOK
        self.pool_fee, self.tick = 0, 200
        self.rate, self.curve_rate = 34_913, 273_351          # tokens per wei in
        self.nonce = 1
        self.log = []                                         # (method, to, selector)
        self.sent, self.receipts, self.pending_txs = [], {}, {}
        self.on_send = None
        self.auto_mine = True
        self.forget = False

    def __call__(self, method, params, all_rpcs_on_error=False):
        to = params[0].get('to') if params and isinstance(params[0], dict) else None
        data = params[0].get('data', '') if params and isinstance(params[0], dict) else ''
        self.log.append((method, to and to_checksum_address(to), data[:10]))
        if method == 'eth_chainId':
            return hex(self.chain_id), None
        if method == 'eth_getBlockByNumber':
            return {'number': hex(self.block), 'timestamp': hex(self.ts)}, None
        if method == 'eth_gasPrice':
            return hex(self.gas_price), None
        if method == 'eth_getBalance':
            return hex(self.balance_of(params[0])), None
        if method == 'eth_getTransactionCount':
            return hex(self.nonce + (len(self.pending_txs) if params[1] == 'pending' else 0)), None
        if method == 'eth_call':
            return self._call(params[0], params[2] if len(params) > 2 else None)
        if method == 'eth_estimateGas':
            res, err = self._call(params[0], params[2] if len(params) > 2 else None, log=False)
            if err:
                return None, err
            t = to_checksum_address(params[0]['to'])
            return hex({bb.ROUTER: 161_654, bb.CURVE: 140_000, bb.FEE_ESCROW: 42_720}.get(t, 50_000)), None
        if method == 'eth_sendRawTransaction':
            return self._send(params[0])
        if method == 'eth_getTransactionReceipt':
            return self.receipts.get(params[0]), None
        if method == 'eth_getTransactionByHash':
            return (None if self.forget else self.pending_txs.get(params[0])), None
        return None, {'code': -32601, 'message': f'fake chain: {method} not supported'}

    def balance_of(self, addr):
        """The buyback wallet has its own balance; any other address has the launch wallet's (as before)."""
        a = to_checksum_address(addr) if addr else None
        return self.buyback_balance if a == bb.BUYBACK_WALLET else self.balance

    def _call(self, c, override, log=True):
        to, data, sel = to_checksum_address(c['to']), c['data'], c['data'][:10]
        val = int(c.get('value', '0x0'), 16)
        frm = c.get('from')
        if log:
            self.froms.append((to, sel, frm and to_checksum_address(frm), sorted(override or {})))
        over = {to_checksum_address(k): v for k, v in (override or {}).items()}
        f_cs = to_checksum_address(frm) if frm else None
        # the buyback wallet: its override or its own balance; anything else: the launch wallet's override or the
        # launch wallet's balance (live/buyrig_test.py relies on that for its page wallet)
        who = bb.BUYBACK_WALLET if f_cs == bb.BUYBACK_WALLET else bb.WALLET
        bal = int(over[who]['balance'], 16) if who in over else self.balance_of(f_cs)
        if to == bb.TOKEN and sel == bb.SEL['curve']:
            return W(A(self.token_curve)), None
        if to == bb.FACTORY and sel == bb.SEL['record']:
            return W(A(self.rec_token), A(self.rec_curve), A(bb.WALLET), A(self.fee_recipient), A(self.pair),
                     42 * 10 ** 17, self.pool_fee, self.tick, 200, self.buyback_enabled, self.phase, 0, 0, 0, 1), None
        if to == bb.CURVE and sel == bb.SEL['graduated']:
            return W(int(self.graduated)), None
        if to == bb.FACTORY and sel == bb.SEL['meme_hook']:
            return W(A(self.hook)), None
        if to == bb.FEE_ESCROW and sel == bb.SEL['balance_of']:
            return W(self.escrow if data[-40:] == bb.WALLET[2:].lower() else 0), None
        if to == bb.FEE_ESCROW and sel == bb.SEL['claim']:
            # as on chain (eth_call, 2026-09-25): a stranger has balance 0, so claim(0) -> NoBalance() and
            # claim(x > 0) -> InsufficientBalance(x, 0)
            amt = int(data[10:], 16)
            have = self.escrow if frm and frm.lower() == bb.WALLET.lower() else 0
            if have == 0 and amt == 0:
                return None, rev('0xc2caa2a6')
            if amt > have:
                return None, rev('0xcf479181' + W(amt, have)[2:])
            return '0x', None
        if to == bb.QUOTER and sel == bb.SEL['quote']:
            (key, zfo, amount, _hd), = decode(['((address,address,uint24,int24,address),bool,uint128,bytes)'],
                                              bytes.fromhex(data[10:]))
            if not (self.phase == 2 and self.graduated):
                return None, rev('0x486aa307')          # PoolNotInitialized-like: no pool before graduation
            return W(amount * self.rate, 84_989), None
        if to == bb.ROUTER and sel == bb.SEL['execute']:
            amount, min_out, deadline = decode_router(data)
            if not (self.phase == 2 and self.graduated):
                return None, rev('0x486aa307')
            if val < amount:                             # as on chain: MORE than amountIn passes (the router keeps it)
                return None, rev('0x00000000', 'value under amountIn')
            if deadline < self.ts:
                return None, rev('0x5bf6f916')
            if bal < val:
                return None, {'code': -32000, 'message': 'insufficient funds for transfer'}
            if amount * self.rate < min_out:
                return None, rev('0x8b063d73' + W(min_out, amount * self.rate)[2:])
            return '0x', None
        if to == bb.CURVE and sel == bb.SEL['curve_buy']:
            amount, min_out, recipient = decode(['uint256', 'uint256', 'address'], bytes.fromhex(data[10:]))
            if self.graduated:
                return None, rev('0x025ac17e')
            if val != amount:
                return None, rev('0xbc760cfe' + W(val, amount)[2:])
            if bal < val:
                return None, {'code': -32000, 'message': 'insufficient funds for transfer'}
            if amount * self.curve_rate < min_out:
                return None, rev('0x71c4efed' + W(min_out, amount * self.curve_rate)[2:])
            return W(amount * self.curve_rate), None
        return None, {'code': -32000, 'message': f'fake chain: unknown call {to} {sel}'}

    def _send(self, raw):
        self.sent.append(raw)
        b = bytes.fromhex(raw[2:])
        assert b.startswith(b'MOCK'), 'only MockSigner transactions reach the fake chain'
        tx = json.loads(b[4:])
        txh = '0x' + keccak(b).hex()
        if self.on_send:
            self.on_send(raw, txh)
        if txh in self.receipts or txh in self.pending_txs:
            return txh, None
        if tx['nonce'] != self.nonce + len(self.pending_txs):
            return None, {'code': -32000, 'message': 'nonce too low'}
        if not self.auto_mine:
            self.pending_txs[txh] = tx
            return txh, None
        self.mine(txh, tx)
        return txh, None

    def mine(self, txh, tx=None):
        tx = tx or self.pending_txs.pop(txh)
        self.pending_txs.pop(txh, None)
        to = to_checksum_address(tx['to'])
        logs, status = [], 1
        gas_used = {bb.ROUTER: 156_061, bb.FEE_ESCROW: 41_000, bb.CURVE: 120_000}.get(to, 50_000)
        if to == bb.FEE_ESCROW:
            amt = int(tx['data'][10:], 16)
            self.escrow -= amt
            self.balance += amt
            logs.append({'address': bb.FEE_ESCROW, 'topics': [bb.CLAIMED_TOPIC, '0x' + bb.pad_addr(bb.WALLET)],
                         'data': W(amt)})
        elif to == bb.ROUTER:
            amount, min_out, _dl = decode_router(tx['data'])
            out = amount * self.rate
            if out < min_out:
                status = 0
            else:
                self.balance -= int(tx['value'])
                logs.append({'address': bb.TOKEN, 'topics': [bb.TRANSFER_TOPIC, '0x' + bb.pad_addr(POOL_MANAGER),
                                                             '0x' + bb.pad_addr(bb.WALLET)], 'data': W(out)})
        self.balance -= gas_used * self.gas_price
        self.nonce += 1
        self.block += 1
        self.receipts[txh] = {'transactionHash': txh, 'status': hex(status), 'gasUsed': hex(gas_used),
                              'effectiveGasPrice': hex(self.gas_price), 'blockNumber': hex(self.block), 'logs': logs}

    def calls(self, method='eth_call'):
        return [(to, sel) for m, to, sel in self.log if m == method]


class Lagging:
    """A node that is behind: it has no receipts and no mempool, but reports the chain's nonce (the RPC fall-over case
    of the review: the receipt lookup hits a lagging node, the nonce lookup a node that is not)."""

    def __init__(self, chain):
        self.chain = chain

    def __call__(self, method, params, all_rpcs_on_error=False):
        if method in ('eth_getTransactionReceipt', 'eth_getTransactionByHash'):
            return None, None
        return self.chain(method, params, all_rpcs_on_error)


class MockSigner:
    """The launch wallet's ADDRESS and no key: its 'raw transactions' are JSON that only FakeChain reads."""
    address = bb.WALLET

    def __init__(self):
        self.signed = []

    def sign_transaction(self, tx):
        raw = b'MOCK' + json.dumps(tx, sort_keys=True).encode()
        self.signed.append(tx)
        return SimpleNamespace(raw_transaction=raw, hash=keccak(raw))


HOUR = 3600


class Clock:
    """A frozen clock. By default one minute into the current UTC hour, so a test only crosses an hour boundary
    where it says so (close_hour)."""

    def __init__(self, t=None):
        self.t = float(t if t is not None else bb.window_start(time.time()) + 60)

    def __call__(self):
        return self.t


HELLO = {'type': 'hello', 'source': 'training', 'task': 'steer', 'run': 'steer_live',
         'label': 'Training run steer_live: the latest saved checkpoint, playing in its own simulation', 'fps': 25,
         'started': '2026-09-25T10:00:00Z'}


def ep(n, hits, misses=0, fell=False, **over):
    m = {'type': 'episode', 'n': n, 'presses': hits + misses, 'hits': hits, 'misses': misses, 'fell': fell}
    m.update(over)
    return json.dumps(m)


def state(live=True, hello=HELLO, episode=None):
    return json.dumps({'type': 'state', 'live': live, 'hello': hello, 'checkpoint': None, 'episode': episode,
                       'history': []})


def frame(click, cursor=(-1, -1), target=(-1, -1, -1, -1), episode=0):
    import struct
    head = [7.0, 1.0, float(episode), 0.3, 1.0 if click else 0.0, *cursor, *target, 0.0]
    return struct.pack('<12f', *head) + bytes(4 * 65 * 7)


def cfg(**kw):
    return bb.Config(**kw).validate()


def make_engine(tmp, chain, config=None, clock=None, accept_test=False, name='journal.jsonl'):
    config = config or cfg()
    clock = clock or Clock()
    journal = bb.Journal(os.path.join(tmp, name), clock=clock)
    rpc = bb.ReadRpc(chain)
    sim = bb.Sim(rpc, config, clock=clock)
    return bb.Engine(config, 'DRY', journal, rpc, bb.DryExecutor(sim, config), sim, accept_test=accept_test,
                     clock=clock)


def feed_hits(engine, hits, start_n=0, hello=HELLO):
    """hits spread over attempts of up to 4 hits each; returns the next n."""
    engine.on_relay_text(json.dumps(hello))
    n = start_n
    while hits > 0:
        h = min(4, hits)
        engine.on_relay_text(ep(n, h))
        hits -= h
        n += 1
    return n


def records(engine, ev=None):
    return [r for r in engine.journal.records() if ev is None or r['ev'] == ev]


def next_hour(clock, extra=1):
    """Move the clock just past the end of the current UTC hour window."""
    clock.t = bb.window_start(clock.t) + HOUR + extra
    return clock.t


def close_hour(engine, clock, extra=1):
    """The hour ends: the next tick closes it and tries its buy."""
    next_hour(clock, extra)
    engine.tick()


def feed(engine, attempts, start_n=0, hello=HELLO):
    """attempts: [(hits, misses), ...] of the steering task (hits <= 4 each); returns the next n."""
    engine.on_relay_text(json.dumps(hello))
    n = start_n
    for h, m in attempts:
        engine.on_relay_text(ep(n, h, m))
        n += 1
    return n


TILES_HELLO = dict(HELLO, task='tiles', run='tiles_live')


def tile(tid, result='miss', lane=0, note_i=0):
    return json.dumps({'type': 'tile', 'id': tid, 'lane': lane, 'result': result, 'note_i': note_i, 'song': 'ode'})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='buyback_test_')
        self.chain = FakeChain()
        self.env_before = list(TRAPS['env_opens'])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertEqual(TRAPS['env_opens'], self.env_before, '.env was opened')
        self.assertEqual(TRAPS['signs'], [], 'a real account signed')
        self.assertFalse(any(m in ('eth_sendRawTransaction', 'eth_sendTransaction') for m, _t, _s in self.chain.log
                             if not getattr(self, 'live_test', False)), 'a DRY test sent a transaction')

    def check_invariants(self, engine):
        L, c = engine.ledger, engine.cfg
        if engine.executor.fee_funded:          # LIVE: paid from the claimed creator fees only
            self.assertLessEqual(L.spent, L.claimed - c.gas_reserve_wei if L.n_buys else L.claimed,
                                 'spent more than the claimed fees minus the gas reserve')
        self.assertLessEqual(L.cap_bought, c.max_total_wei)
        for r in records(engine, 'buy'):
            self.assertLessEqual(r['amount_wei'], c.max_buy_wei)
            self.assertGreaterEqual(r['amount_wei'], c.min_buy_wei)
            self.assertEqual(r['amount_wei'] % bb.AMOUNT_STEP_WEI, 0, 'at most 8 decimals (what the buy rig types)')
        # each closed hour bought at most once
        wins = [r['window'] for r in records(engine, 'buy') if r.get('window') is not None]
        self.assertEqual(len(wins), len(set(wins)), 'an hour bought twice')


# ---------------------------------------------------------------------------------------------------- tests
class TestEncoding(unittest.TestCase):
    def test_calldata_matches_the_research(self):
        self.assertEqual(bb.cd_quote(10 ** 13), RESEARCH_QUOTE)
        self.assertEqual(bb.cd_curve_buy(10 ** 13, 0), RESEARCH_CURVE_BUY)
        router = bb.cd_router_buy(10 ** 13, 349132310919230143372, 0x6ab5a04c)
        self.assertEqual(hashlib.sha256(router.encode()).hexdigest(), RESEARCH_ROUTER_SHA256)
        self.assertEqual(bb.pool_id(), bb.POOL_ID)

    def test_selectors_and_topics(self):
        self.assertEqual(bb.SEL['execute'], '0x3593564c')
        self.assertEqual(bb.SEL['quote'], '0xaa9d21cb')
        self.assertEqual(bb.SEL['curve_buy'], '0x59a87bc1')
        self.assertEqual(bb.SEL['claim'], '0x379607f5')
        self.assertEqual(bb.SEL['balance_of'], '0x70a08231')
        self.assertEqual(bb.SEL['record'], '0x3cf28b5a')
        self.assertEqual(bb.SEL['graduated'], '0xe7c2b772')
        self.assertEqual(bb.CLAIMED_TOPIC, '0xd8138f8a3f377c5259ca548e70e4c2de94f129f5a11036a15b69513cba2b426a')

    def test_config_hard_ceilings(self):
        with self.assertRaises(ValueError):
            cfg(max_buy_wei=10 ** 18)
        with self.assertRaises(ValueError):
            cfg(hourly_budget_wei=10 ** 17 + 1)              # over the owner's 0.1 ETH hard ceiling
        with self.assertRaises(ValueError):
            cfg(max_day_wei=24 * 10 ** 17 + 1)               # over the 2.4 ETH/day hard ceiling
        with self.assertRaises(ValueError):
            cfg(hourly_budget_wei=0)                         # unset is None, not 0
        with self.assertRaises(ValueError):
            cfg(preview_budget_wei=10 ** 17)
        with self.assertRaises(ValueError):
            cfg(min_buy_wei=2 * 10 ** 15)                    # over max_buy
        with self.assertRaises(ValueError):
            cfg(window_s=7)                                  # must divide the hour
        with self.assertRaises(SystemExit):
            bb.main(['--max-total-eth', '6'])                # over the 5 ETH hard ceiling
        with self.assertRaises(SystemExit):
            bb.main(['--hourly-budget-eth', '1'])

    def test_budget_flags(self):
        """--hourly-budget-eth: an amount, or unset (preview); without the flag the process environment's
        BUYBACK_HOURLY_BUDGET_ETH (never .env), else unset."""
        saved = os.environ.pop(bb.BUDGET_ENV, None)
        try:
            c = bb.build_config(bb.parse_args([]))
            self.assertIsNone(c.hourly_budget_wei)
            self.assertTrue(c.preview)
            self.assertEqual((c.base_wei, c.window_s), (10 ** 15, 3600))
            for v in ('unset', 'UNSET', 'none', ''):
                self.assertIsNone(bb.build_config(bb.parse_args(['--hourly-budget-eth', v])).hourly_budget_wei, v)
            c = bb.build_config(bb.parse_args(['--hourly-budget-eth', '0.0008']))
            self.assertEqual((c.hourly_budget_wei, c.preview, c.base_wei), (8 * 10 ** 14, False, 8 * 10 ** 14))
            c = bb.build_config(bb.parse_args(['--preview-budget-eth', '0.0005']))
            self.assertEqual(c.base_wei, 5 * 10 ** 14)
            os.environ[bb.BUDGET_ENV] = '0.0006'
            self.assertEqual(bb.build_config(bb.parse_args([])).hourly_budget_wei, 6 * 10 ** 14)
            self.assertIsNone(bb.build_config(bb.parse_args(['--hourly-budget-eth', 'unset'])).hourly_budget_wei,
                              'the flag wins over the environment')
            os.environ[bb.BUDGET_ENV] = 'not eth'
            with self.assertRaises(SystemExit):
                bb.main([])
        finally:
            os.environ.pop(bb.BUDGET_ENV, None)
            if saved is not None:
                os.environ[bb.BUDGET_ENV] = saved

    def test_the_hour_arithmetic(self):
        self.assertEqual(bb.window_start(1_790_000_000.5), 1_790_000_000 - 1_790_000_000 % 3600)
        self.assertEqual(bb.iso(bb.window_start(1_790_003_599))[14:], '00:00Z')
        self.assertIsNone(bb.hit_rate(0, 0, 0))
        self.assertEqual(bb.hit_rate(3, 1, 0), 0.75)
        self.assertEqual(bb.hit_rate(6, 1, 1), 0.75)
        self.assertEqual(bb.hour_amount(10 ** 15, 3, 1, 0), 75 * 10 ** 13)
        self.assertEqual(bb.hour_amount(10 ** 15, 1, 2, 0), 333_330_000_000_000, 'rounded DOWN to 8 decimals')
        self.assertEqual(bb.eth_str(bb.hour_amount(10 ** 15, 1, 2, 0)), '0.00033333')
        self.assertEqual(bb.hour_amount(10 ** 15, 0, 5, 5), 0)
        self.assertEqual(bb.hour_amount(10 ** 15, 0, 0, 0), 0)


class TestHitCounter(unittest.TestCase):
    def test_counting_rules(self):
        c = bb.HitCounter()
        self.assertEqual(c.on_text(state(live=False, episode={'type': 'episode', 'n': 15, 'presses': 3, 'hits': 2,
                                                               'misses': 1, 'fell': False})), [
            {'ev': 'session', 'key': 'steer_live|steer|2026-09-25T10:00:00Z', 'run': 'steer_live', 'task': 'steer',
             'started': '2026-09-25T10:00:00Z', 'test': False, 'countable': True, 'why': None}])
        self.assertEqual(c.stats['state_episode_not_live'], 1, "a stopped run's last episode is never counted")
        out = c.on_text(state(live=True, episode={'type': 'episode', 'n': 3, 'presses': 2, 'hits': 2, 'misses': 0,
                                                  'fell': False}))
        self.assertEqual([(e['n'], e['hits'], e['via']) for e in out if e['ev'] == 'episode'], [(3, 2, 'state')])
        self.assertEqual([e['hits'] for e in c.on_text(ep(4, 3, 1))], [3])
        self.assertEqual(c.on_text(ep(4, 3, 1)), [], 'the same episode twice')
        # a reconnect: a fresh state repeats the last episode; the publisher's resync repeats its hello
        self.assertEqual([e for e in c.on_text(state(True, episode=json.loads(ep(4, 3, 1)))) if e['ev'] == 'episode'], [])
        self.assertEqual(c.on_text(json.dumps(HELLO)), [])
        self.assertEqual(c.on_text(ep(4, 3, 1)), [])
        self.assertEqual([e['n'] for e in c.on_text(ep(5, 1))], [5])
        # a gap is reported, then counted from there
        out = c.on_text(ep(8, 2))
        self.assertEqual([e['ev'] for e in out], ['gap', 'episode'])
        self.assertEqual(out[0]['missed'], 2)
        # a new session (a new publisher session of the same run): n starts again and counts
        h2 = dict(HELLO, started='2026-09-25T11:00:00Z')
        out = c.on_text(json.dumps(h2)) + c.on_text(ep(0, 1))
        self.assertEqual([e['ev'] for e in out], ['session', 'episode'])
        # bye: nothing after it counts until a new hello
        c.on_text(json.dumps({'type': 'bye'}))
        self.assertEqual(c.on_text(ep(1, 4)), [])

    def test_what_is_not_a_hit(self):
        c = bb.HitCounter()
        test_hello = dict(HELLO, label='TEST (not a live training run): x', test=True)
        c.on_text(json.dumps(test_hello))
        self.assertEqual(c.on_text(ep(0, 4)), [], 'a TEST stream never counts by default')
        c.on_text(json.dumps(dict(HELLO, source='replay')))
        self.assertEqual(c.on_text(ep(0, 4)), [], 'only source "training"')
        c2 = bb.HitCounter(accept_test=True)
        c2.on_text(json.dumps(test_hello))
        self.assertEqual([e['hits'] for e in c2.on_text(ep(0, 4)) if e['ev'] == 'episode'], [4])
        c3 = bb.HitCounter(tasks=('steer',))
        c3.on_text(json.dumps(dict(HELLO, task='cursor')))
        self.assertEqual(c3.on_text(ep(0, 4)), [], 'task filter')

    def test_validation(self):
        c = bb.HitCounter()
        c.on_text(json.dumps(HELLO))
        bad = [ep(0, 5), json.dumps({'type': 'episode', 'n': 1, 'presses': 3, 'hits': 2, 'misses': 0}),
               json.dumps({'type': 'episode', 'n': 2, 'presses': 1, 'hits': True, 'misses': 0}),
               json.dumps({'type': 'episode', 'n': 3, 'presses': 1.0, 'hits': 1.0, 'misses': 0}),
               json.dumps({'type': 'episode', 'n': -1, 'presses': 1, 'hits': 1, 'misses': 0})]
        for m in bad:
            out = c.on_text(m)
            self.assertEqual([e['ev'] for e in out], ['rejected'], m)
        lever = bb.HitCounter(tasks=bb.TASKS)          # only when --tasks names the lever task
        lever.on_text(json.dumps(dict(HELLO, task='lever')))
        self.assertEqual([e['ev'] for e in lever.on_text(ep(0, 2))], ['rejected'], 'the lever task has 1 press max')
        self.assertEqual([e['hits'] for e in lever.on_text(ep(1, 1))], [1])
        self.assertEqual(c.on_text('not json'), [])
        self.assertEqual(c.on_text('x' * 80_000), [])

    def test_lever_presses_are_not_target_hits_by_default(self):
        """Review finding: the lever task has no lit target, so by default its clean presses are not counted as
        'targets the rat hits'; counting them is opt-in and the public rule then says so."""
        self.assertEqual(bb.Config().tasks, ('cursor', 'steer', 'tiles'))
        c = bb.HitCounter()
        out = c.on_text(json.dumps(dict(HELLO, task='lever')))
        self.assertEqual(out[0]['countable'], False)
        self.assertIn('lever is not counted', out[0]['why'])
        self.assertEqual(c.on_text(ep(0, 1)), [])
        chain = FakeChain()
        tmp = tempfile.mkdtemp(prefix='buyback_test_')
        try:
            e = make_engine(tmp, chain)
            self.assertNotIn('lever', e.public_status()['rule'])
            e2 = make_engine(os.path.join(tmp, 'l'), chain, cfg(tasks=bb.TASKS))
            self.assertIn('in the lever task, each clean press', e2.public_status()['rule'])
            self.assertEqual(bb.build_config(bb.parse_args([])).tasks, ('cursor', 'steer', 'tiles'))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_rat_tiles_hits_count_like_steer_hits(self):
        """Rat Tiles (task "tiles"): each tile tapped is a hit; an attempt is one song, so up to TILES_MAX_HITS."""
        import tiles_env
        songs = tiles_env.load_songs()
        self.assertLessEqual(max(len(s['notes']) for s in songs), bb.TILES_MAX_HITS)
        self.assertEqual(tiles_env.MAX_SONG_NOTES, bb.TILES_MAX_HITS)
        tiles_hello = TILES_HELLO
        c = bb.HitCounter()
        out = c.on_text(json.dumps(tiles_hello))
        self.assertEqual((out[0]['task'], out[0]['countable']), ('tiles', True))
        self.assertEqual([e['hits'] for e in c.on_text(ep(0, 30, 4)) if e['ev'] == 'episode'], [30])
        self.assertEqual([e['ev'] for e in c.on_text(ep(1, bb.TILES_MAX_HITS + 1))], ['rejected'])
        self.assertEqual([e['ev'] for e in c.on_text(ep(2, 3, 1, presses=5))], ['rejected'], 'presses != hits + misses')
        c.on_bytes(frame(True, (0.36, 0.52), (0.375, 0.5, 0.11, 0.12)))          # the cursor on the lit tile
        c.on_bytes(frame(True, (0.60, 0.52), (0.375, 0.5, 0.11, 0.12)))          # a wrong click
        self.assertEqual(c.attempt, {'clicks': 2, 'on_target': 1})
        steer_only = bb.HitCounter(tasks=('cursor', 'steer'))
        self.assertIn('tiles is not counted', steer_only.on_text(json.dumps(tiles_hello))[0]['why'])
        chain = FakeChain()
        tmp = tempfile.mkdtemp(prefix='buyback_test_')
        try:
            e = make_engine(tmp, chain)
            self.assertIn('in Rat Tiles a hit is a tile tapped in time, a miss a tile that slid past untapped and a '
                          'wrong click a press with no tile to tap', e.public_status()['rule'])
            e.on_relay_text(json.dumps(tiles_hello))
            e.on_relay_text(tile(7))
            e.on_relay_text(ep(0, 12, 2))
            self.assertEqual(e.ledger.hits, 12)
            w = e.public_status()['window']
            self.assertEqual((w['hits'], w['misses'], w['wrong'], w['attempts']), (12, 1, 2, 1))
            self.assertEqual(w['hit_rate'], 0.8)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_rat_tiles_misses_and_wrong_clicks(self):
        """Rat Tiles: misses = tiles that slid past untapped, wrong = presses with no tile to tap. From the episode's
        "missed" when the publisher sends it, else from its tile events (each tile id once); an episode carrying
        "wrong" is read as misses = tiles that slid past."""
        def one(*msgs, hello=TILES_HELLO):
            c = bb.HitCounter()
            c.on_text(json.dumps(hello))
            out = []
            for m in msgs:
                out += c.on_text(m)
            return out
        # 1. the episode says it: hits 9, wrong clicks 2 (its "misses"), 3 tiles slid past ("missed")
        e, = one(ep(0, 9, 2, missed=3))
        self.assertEqual((e['w_hits'], e['w_misses'], e['w_wrong'], e['missed_from']), (9, 3, 2, 'episode'))
        # 2. no "missed": the tile events of that attempt, each tile once (a repeated event is not a second miss);
        #    hits and wrong clicks do not count as missed tiles; the tally starts again for the next song
        out = one(tile(1), tile(1), tile(2), tile(3, 'hit'), tile(4, 'wrong'), ep(0, 9, 2), tile(5), ep(1, 4, 0))
        eps = [e for e in out if e['ev'] == 'episode']
        self.assertEqual([(e['w_hits'], e['w_misses'], e['w_wrong'], e['missed_from']) for e in eps],
                         [(9, 2, 2, 'tile events'), (4, 1, 0, 'tile events')])
        # 3. "wrong" given: misses = tiles that slid past, presses = hits + wrong
        e, = one(json.dumps({'type': 'episode', 'n': 0, 'presses': 11, 'hits': 9, 'misses': 3, 'wrong': 2,
                             'fell': False}))
        self.assertEqual((e['w_hits'], e['w_misses'], e['w_wrong']), (9, 3, 2))
        # bad counts are rejected
        for m in (ep(0, 9, 2, missed=-1), ep(0, 9, 2, missed=True), ep(0, 9, 2, missed=bb.TILES_MAX_HITS + 1),
                  json.dumps({'type': 'episode', 'n': 0, 'presses': 12, 'hits': 9, 'misses': 3, 'wrong': 2}),
                  json.dumps({'type': 'episode', 'n': 0, 'presses': 11, 'hits': 9, 'misses': 3, 'wrong': '2'})):
            self.assertEqual([x['ev'] for x in one(m)], ['rejected'], m)
        # tile events of a stream that is not counted, or not live, count nothing
        c = bb.HitCounter()
        c.on_text(json.dumps(dict(TILES_HELLO, label='TEST x', test=True)))
        c.on_text(tile(1))
        self.assertEqual(c.tiles_missed, set())
        c = bb.HitCounter()
        c.on_text(state(live=False, hello=TILES_HELLO))
        c.on_text(tile(1))
        self.assertEqual(c.tiles_missed, set())
        # the steering task: misses = clicks off the target, never "wrong"
        e, = [x for x in one(ep(0, 3, 2), hello=HELLO) if x['ev'] == 'episode']
        self.assertEqual((e['w_hits'], e['w_misses'], e['w_wrong']), (3, 2, 0))
        # joined mid-song (the relay's state on connect, or a publisher resync): that song's missed tiles are
        # unknown, so it stays out of the hit rate (it could only raise it); the next song is followed whole
        c = bb.HitCounter()
        c.on_text(state(True, hello=TILES_HELLO))
        c.on_text(tile(1))
        e, = [x for x in c.on_text(ep(0, 9, 2)) if x['ev'] == 'episode']
        self.assertEqual((e['partial'], e['missed_from'], e['hits'], e['w_hits'], e['w_misses'], e['w_wrong']),
                         (True, 'unknown', 9, 0, 0, 0))
        c.on_text(tile(2))
        e, = [x for x in c.on_text(ep(1, 5, 0)) if x['ev'] == 'episode']
        self.assertEqual((e['partial'], e['w_hits'], e['w_misses']), (False, 5, 1))
        c.on_text(json.dumps(TILES_HELLO))                          # the same session again: a resync
        c.on_text(tile(3))
        e, = [x for x in c.on_text(ep(2, 5, 0)) if x['ev'] == 'episode']
        self.assertTrue(e['partial'])
        e, = [x for x in c.on_text(ep(3, 4, 1, missed=2)) if x['ev'] == 'episode']
        self.assertEqual((e['partial'], e['w_misses'], e['w_wrong']), (False, 2, 1), '"missed" in the episode: whole')
        c.on_text(state(True, hello=TILES_HELLO))
        e, = [x for x in c.on_text(ep(4, 4, 1, missed=2)) if x['ev'] == 'episode']
        self.assertFalse(e['partial'], 'an episode that says its missed tiles needs no events')

    def test_frames_feed_only_the_unconfirmed_tally(self):
        c = bb.HitCounter()
        c.on_text(state(True))
        c.on_bytes(frame(True, (0.48, 0.80), (0.61, 0.73, 0.30, 0.19)))       # on target
        c.on_bytes(frame(True, (0.10, 0.10), (0.61, 0.73, 0.05, 0.05)))       # off target
        c.on_bytes(frame(True, (0.10, 0.10)))                                 # no lit target (a hold)
        c.on_bytes(frame(False, (0.61, 0.73), (0.61, 0.73, 0.3, 0.3)))        # not a click
        self.assertEqual(c.attempt, {'clicks': 3, 'on_target': 1})
        c.on_text(ep(0, 1, 2))
        self.assertEqual(c.attempt, {'clicks': 0, 'on_target': 0})
        c.on_text(json.dumps({'type': 'idle', 'reason': 'quiet'}))
        c.on_bytes(frame(True, (0.5, 0.5), (0.5, 0.5, 0.1, 0.1)))
        self.assertEqual(c.attempt['clicks'], 0, 'not while the stream is not live')


class TestEngineDry(Base):
    def test_restart_does_not_count_twice(self):
        e = make_engine(self.tmp, self.chain)
        e.on_relay_text(state(True, episode=None))
        e.on_relay_text(ep(0, 3))
        e.on_relay_text(ep(1, 2, 1))
        self.assertEqual(e.ledger.hits, 5)
        self.assertEqual(e.public_status()['window']['misses'], 1)
        e2 = make_engine(self.tmp, self.chain)                       # a restart: the same journal, the same hour
        self.assertEqual(e2.ledger.hits, 5)
        w = e2.public_status()['window']
        self.assertEqual((w['hits'], w['misses'], w['wrong'], w['attempts']), (5, 1, 0, 2), 'the hour survives it')
        e2.on_relay_text(state(True, episode=json.loads(ep(1, 2, 1))))  # the relay repeats the last episode
        e2.on_relay_text(ep(0, 3))                                    # and the publisher resyncs
        e2.on_relay_text(ep(2, 4))
        self.assertEqual(e2.ledger.hits, 9)
        self.assertEqual([r['n'] for r in records(e2, 'hit')], [0, 1, 2])
        self.assertEqual(e2.public_status()['window']['hits'], 9)

    def test_hourly_buy_is_budget_times_hit_rate(self):
        """Once an hour, on the hour: the hour's buy = the hourly budget x hits / (hits + misses + wrong), rounded
        down to 8 decimals, simulated from the hand-funded buyback wallet, with no creator-fee claim."""
        clock = Clock()
        e = make_engine(self.tmp, self.chain, cfg(hourly_budget_wei=10 ** 15), clock)
        hour0 = bb.window_start(clock.t)
        feed(e, [(4, 0), (4, 1), (4, 3)])             # 12 hits, 4 misses: hit rate 0.75
        e.tick()
        self.assertEqual(records(e, 'buy'), [], 'nothing is bought before the hour ends')
        st = e.public_status()
        self.assertEqual(st['window'], {'start': bb.iso(hour0), 'end': bb.iso(hour0 + HOUR), 'hits': 12, 'misses': 4,
                                        'wrong': 0, 'hit_rate': 0.75, 'attempts': 3, 'preview': False,
                                        'projected_eth': '0.00075'})
        self.assertEqual((st['next_buy_note'], st['next_buy_at']), ('waiting for the hour', bb.iso(hour0 + HOUR)))
        self.assertEqual(st['next_buy_in_s'], HOUR - 60)
        self.assertIsNone(st['last_window'])
        self.assertEqual(st['budget'], {'hourly_eth': '0.001', 'rule': 'hourly budget x hit rate', 'set': True,
                                        'preview_eth': None, 'funding': 'a separate buyback wallet, funded by hand'})
        self.assertEqual((st['hits']['pending'], st['pending']), (12, {'eth': '0', 'hits': 12}))
        self.assertIsNone(st['per_hit_eth'])
        close_hour(e, clock)
        win, = records(e, 'window')
        self.assertEqual({k: win[k] for k in ('start', 'hits', 'misses', 'wrong', 'attempts', 'hit_rate', 'budget_wei',
                                              'preview', 'amount_wei', 'cap', 'late', 'note')},
                         {'start': bb.iso(hour0), 'hits': 12, 'misses': 4, 'wrong': 0, 'attempts': 3, 'hit_rate': 0.75,
                          'budget_wei': 10 ** 15, 'preview': False, 'amount_wei': 75 * 10 ** 13, 'cap': None,
                          'late': False, 'note': None})
        b, = records(e, 'buy')
        self.assertEqual((b['amount_wei'], b['window'], b['hits_covered'], b['hit_rate'], b['preview']),
                         (75 * 10 ** 13, hour0, 12, 0.75, False))
        self.assertEqual(b['tokens_wei'], 75 * 10 ** 13 * self.chain.rate)
        self.assertEqual(b['min_out_wei'], 75 * 10 ** 13 * self.chain.rate * 97 // 100)
        self.assertTrue(b['simulated'] and b['quote_checked'])
        self.assertEqual((b['venue'], b['buyer'], b['buyer_funded']), ('pool', bb.BUYBACK_WALLET, False))
        # the buyer: every buy call from the buyback wallet, with a balance override on it; no claim at all
        buys = [f for f in self.chain.froms if f[0] == bb.ROUTER]
        self.assertTrue(buys and all(f[2] == bb.BUYBACK_WALLET and f[3] == [bb.BUYBACK_WALLET] for f in buys), buys)
        self.assertNotIn((bb.FEE_ESCROW, bb.SEL['claim']), self.chain.calls())
        self.assertEqual(records(e, 'claim'), [])
        st = e.public_status()
        lw = st['last_window']
        self.assertEqual({k: lw[k] for k in ('start', 'end', 'hits', 'misses', 'wrong', 'hit_rate', 'preview',
                                             'budget_eth', 'amount_eth')},
                         {'start': bb.iso(hour0), 'end': bb.iso(hour0 + HOUR), 'hits': 12, 'misses': 4, 'wrong': 0,
                          'hit_rate': 0.75, 'preview': False, 'budget_eth': '0.001', 'amount_eth': '0.00075'})
        self.assertEqual({k: lw['buy'][k] for k in ('state', 'eth_in', 'simulated', 'preview', 'venue')},
                         {'state': 'simulated', 'eth_in': '0.00075', 'simulated': True, 'preview': False,
                          'venue': 'pool'})
        r0 = st['buys']['recent'][0]
        self.assertEqual((r0['eth_in'], r0['hits_covered'], r0['window'], r0['hit_rate'], r0['preview'], r0['simulated']),
                         ('0.00075', 12, bb.iso(hour0), 0.75, False, True))
        self.assertEqual((st['hits']['in_buys'], st['hits']['pending'], st['pending']['eth']), (12, 0, '0'))
        self.assertEqual(st['window']['start'], bb.iso(hour0 + HOUR))
        self.assertEqual(st['next_buy_at'], bb.iso(hour0 + 2 * HOUR))
        # a funded buyback wallet is journalled as such
        self.chain.buyback_balance = 10 ** 16
        feed(e, [(4, 0)], start_n=3)
        close_hour(e, clock)
        self.assertTrue(records(e, 'buy')[-1]['buyer_funded'])
        self.assertEqual(records(e, 'buy')[-1]['amount_wei'], 10 ** 15, '100 % of the budget')
        # an hour with no attempts, an hour with no hits, an hour whose buy is under the minimum: recorded, no buy
        close_hour(e, clock)
        feed(e, [(0, 3)], start_n=4)
        close_hour(e, clock)
        feed(e, [(1, 0)] + [(0, 4)] * 5, start_n=5)    # 1 / 21 of 0.001 ETH = 0.0000476 < 0.0001
        close_hour(e, clock)
        self.assertEqual([r['note'] for r in records(e, 'window')][2:],
                         ['no attempts in the hour', 'no hits in the hour', 'hit rate too low for the minimum buy'])
        self.assertEqual(len(records(e, 'buy')), 2)
        self.assertEqual(e.public_status()['last_window']['buy'], {'state': 'none',
                                                                   'note': 'hit rate too low for the minimum buy'})
        self.check_invariants(e)

    def test_preview_buy_while_the_budget_is_not_set(self):
        """No budget yet: every hour is still recorded and runs a SIMULATED PREVIEW buy of the nominal amount x the
        hit rate, clearly marked, cut to the per-buy cap only and never using up a cap or the gas caps."""
        clock = Clock()
        c = cfg(max_total_wei=10 ** 15, max_day_wei=10 ** 15, max_hour_wei=10 ** 15, max_gas_total_wei=10 ** 12)
        e = make_engine(self.tmp, self.chain, c, clock)
        st = e.public_status()
        self.assertEqual(st['budget'], {'hourly_eth': None, 'rule': 'hourly budget x hit rate', 'set': False,
                                        'preview_eth': '0.001', 'funding': 'a separate buyback wallet, funded by hand'})
        self.assertIn('The hourly budget is not set yet: until it is, each hour runs a simulated preview buy of 0.001 '
                      'ETH x the hit rate.', st['rule'])
        self.assertTrue(st['window']['preview'])
        for h in range(3):                            # 3 hours at 3/4: 0.00075 each, over the 0.001 total cap
            feed(e, [(3, 1)], start_n=h)
            close_hour(e, clock)
        buys = records(e, 'buy')
        self.assertEqual([b['amount_wei'] for b in buys], [75 * 10 ** 13] * 3, 'previews use no cap')
        self.assertTrue(all(b['preview'] and b['simulated'] for b in buys))
        self.assertTrue(all(w['preview'] and w['budget_wei'] is None and w['base_wei'] == 10 ** 15
                            for w in records(e, 'window')))
        L = e.ledger
        self.assertEqual((L.cap_bought, len(L.buys), L.gas_total, L.n_previews), (0, 0, 0, 3))
        st = e.public_status()
        self.assertIsNone(st['stopped'])
        self.assertEqual(st['next_buy_note'], 'waiting for the hour')
        self.assertEqual((st['buys']['count'], st['buys']['previews'], st['buys']['eth_in']), (3, 3, '0.00225'))
        r0 = st['buys']['recent'][0]
        self.assertEqual((r0['preview'], r0['label'], r0['simulated']), (True, bb.PREVIEW_LABEL, True))
        self.assertEqual((st['last_window']['preview'], st['last_window']['preview_eth'],
                          st['last_window']['budget_eth'], st['last_window']['buy']['preview']),
                         (True, '0.001', None, True))
        # a perfect hour with a preview amount over the per-buy cap: cut to it
        e2 = make_engine(os.path.join(self.tmp, 'p'), self.chain,
                         cfg(preview_budget_wei=5 * 10 ** 15, max_buy_wei=10 ** 15), clock)
        feed(e2, [(4, 0)])
        close_hour(e2, clock)
        self.assertEqual((records(e2, 'window')[0]['cap'], records(e2, 'buy')[0]['amount_wei']), ('per-buy', 10 ** 15))
        self.check_invariants(e)

    def test_every_cap(self):
        clock = Clock()
        c = cfg(hourly_budget_wei=10 ** 15, min_buy_wei=10 ** 14, max_buy_wei=10 ** 15, max_hour_wei=10 ** 15,
                max_day_wei=25 * 10 ** 14, max_total_wei=3 * 10 ** 15)
        e = make_engine(self.tmp, self.chain, c, clock)
        amounts, n = [], 0

        def hour(extra=1):
            nonlocal n
            n = feed(e, [(4, 0)], start_n=n)
            k = len(records(e, 'buy'))
            close_hour(e, clock, extra)
            b = records(e, 'buy')
            amounts.append(b[-1]['amount_wei'] if len(b) > k else 0)
            return records(e, 'window')[-1]

        self.assertIsNone(hour()['cap'])               # 0.001
        self.assertIsNone(hour()['cap'])               # 0.001: the rolling hour holds one buy
        w = hour()                                     # the day has 0.0005 left: cut to it
        self.assertEqual((w['cap'], w['amount_wei']), ('daily', 5 * 10 ** 14))
        w = hour()
        self.assertEqual((w['cap'], w['note'], w['amount_wei']), ('daily', 'the daily cap is reached', 0))
        self.assertEqual(e.public_status()['next_buy_note'], 'daily cap reached')
        self.assertIsNone(e.public_status()['next_buy_in_s'])
        clock.t += 86400                               # a new day: the total has 0.0005 left
        w = hour()
        self.assertEqual((w['cap'], w['amount_wei']), ('total', 5 * 10 ** 14))
        st = e.public_status()
        self.assertEqual((st['next_buy_in_s'], st['next_buy_note']), (None, 'total cap reached'))
        self.assertEqual(st['stopped'], 'the total cap is reached: no more buybacks')
        w = hour()
        self.assertEqual((w['note'], w['amount_wei']), ('the total cap is reached', 0))
        self.assertEqual(amounts, [10 ** 15, 10 ** 15, 5 * 10 ** 14, 0, 5 * 10 ** 14, 0])
        self.assertEqual(e.ledger.cap_bought, c.max_total_wei)
        self.check_invariants(e)

    def test_a_skipped_buy_retries_until_the_next_hour_then_expires(self):
        """A passing problem (gas price over the cap) retries the hour's buy; when the next hour closes first, the
        old buy expires and only the new hour's buy is tried. Gas too large a share of the buy: no buy that hour."""
        clock = Clock()
        e = make_engine(self.tmp, self.chain, cfg(hourly_budget_wei=10 ** 15), clock)
        hour0 = bb.window_start(clock.t)
        feed(e, [(4, 0)])
        self.chain.gas_price = 2 * 10 ** 9
        n_calls = len(self.chain.calls())
        close_hour(e, clock)
        self.assertIn('gas price', records(e, 'buy_skip')[-1]['why'])
        self.assertEqual(len(self.chain.calls()), n_calls + 4, 'only the pinned checks ran (4 eth_calls)')
        st = e.public_status()
        self.assertEqual(st['next_buy_note'], 'gas price over the cap')
        self.assertTrue(290 <= st['next_buy_in_s'] <= 301, st['next_buy_in_s'])
        self.assertEqual(st['next_buy_at'], bb.iso(clock.t + st['next_buy_in_s']))
        self.assertEqual((st['last_window']['buy']['state'], st['pending']), ('due', {'eth': '0.001', 'hits': 4}))
        clock.t += 100
        e.tick()
        self.assertEqual(len(records(e, 'buy_skip')), 1, 'waits for its retry time')
        clock.t += 250
        e.tick()
        self.assertEqual(len(records(e, 'buy_skip')), 2)
        feed(e, [(2, 2)], start_n=1)
        close_hour(e, clock)                           # still over the cap: the first hour's buy expires
        exp, = records(e, 'buy_expired')
        self.assertEqual((exp['window'], exp['amount_wei']), (hour0, 10 ** 15))
        self.chain.gas_price = 44_000_000
        clock.t += 301
        e.tick()
        b, = records(e, 'buy')
        self.assertEqual((b['window'], b['amount_wei']), (hour0 + HOUR, 5 * 10 ** 14))
        # gas share over the cap: no buy for that hour (the amount will not grow)
        e2 = make_engine(os.path.join(self.tmp, 'g'), self.chain,
                         cfg(hourly_budget_wei=10 ** 15, max_gas_share_bps=50), clock)
        feed(e2, [(4, 0)])
        close_hour(e2, clock)
        self.assertEqual(records(e2, 'buy'), [])
        nb, = records(e2, 'window_nobuy')
        self.assertIn('gas would be', nb['why'])
        st = e2.public_status()
        self.assertEqual(st['last_window']['buy'], {'state': 'none', 'note': 'gas too large a share of the buy'})
        self.assertEqual((st['next_buy_note'], st['pending']['eth']), ('waiting for the hour', '0'))
        clock.t += 500
        e2.tick()
        self.assertEqual(records(e2, 'buy'), [], 'never retried')

    def test_restart_mid_hour_and_an_hour_closed_late(self):
        """The hour survives a restart; an hour that ended while the engine was down is closed on the restart: the
        hour just ended still buys, an older one closes late without a buy."""
        clock = Clock()
        c = cfg(hourly_budget_wei=10 ** 15)
        e = make_engine(self.tmp, self.chain, c, clock)
        hour0 = bb.window_start(clock.t)
        feed(e, [(4, 0), (2, 2)])
        clock.t += 1200
        e2 = make_engine(self.tmp, self.chain, c, clock)          # a restart in the same hour
        feed(e2, [(4, 0)], start_n=2)
        self.assertEqual(e2.public_status()['window']['hits'], 10)
        next_hour(clock, 600)                                     # down over the hour's end, back 10 min later
        e3 = make_engine(self.tmp, self.chain, c, clock)
        e3.tick()
        w, = records(e3, 'window')
        self.assertEqual((w['start_t'], w['hits'], w['misses'], w['late'], w['amount_wei']),
                         (hour0, 10, 2, False, bb.hour_amount(10 ** 15, 10, 2, 0)))
        self.assertEqual(records(e3, 'buy')[0]['window'], hour0)
        # hits in the next hour, then down for more than an hour: that hour closes late, without a buy
        feed(e3, [(4, 0)], start_n=3)
        clock.t += 2 * HOUR
        e4 = make_engine(self.tmp, self.chain, c, clock)
        e4.tick()
        late = records(e4, 'window')[-1]
        self.assertEqual((late['start_t'], late['late'], late['amount_wei'], late['hits']), (hour0 + HOUR, True, 0, 4))
        self.assertEqual(late['note'], 'closed late; the engine was not running at the hour')
        self.assertEqual(len(records(e4, 'buy')), 1)
        e5 = make_engine(self.tmp, self.chain, c, clock)          # and it is not closed twice
        e5.tick()
        self.assertEqual(len(records(e5, 'window')), 2)
        self.check_invariants(e5)

    def test_a_journal_from_before_the_hourly_rule(self):
        """Old per-hit records replay: their hits and buys count, their pending amount is dropped (shown as counted,
        bought nothing), and the first hour after the switch buys only its own hour."""
        clock = Clock()
        path = os.path.join(self.tmp, 'journal.jsonl')
        t = clock.t - 5000
        old = [{'t': t, 'mode': 'DRY', 'ev': 'hit', 'key': 'steer_live|steer|x#0', 'hits': 50, 'added_wei': 5 * 10 ** 14,
                'capped_wei': 0},
               {'t': t + 1, 'mode': 'DRY', 'ev': 'claim', 'amount_wei': 2 * 10 ** 16, 'gas_cost_wei': 10 ** 12},
               {'t': t + 2, 'mode': 'DRY', 'ev': 'buy', 'amount_wei': 5 * 10 ** 14, 'hits_covered': 50,
                'tokens_wei': 10 ** 18, 'venue': 'pool', 'simulated': True, 'gas_cost_wei': 7 * 10 ** 12},
               {'t': t + 3, 'mode': 'DRY', 'ev': 'hit', 'key': 'steer_live|steer|x#1', 'hits': 7, 'added_wei': 7 * 10 ** 13,
                'capped_wei': 0}]
        with _real_open(path, 'w', encoding='utf-8') as f:
            for r in old:
                f.write(json.dumps(r) + '\n')
        e = make_engine(self.tmp, self.chain, cfg(hourly_budget_wei=10 ** 15), clock)
        st = e.public_status()
        self.assertEqual((e.ledger.pending, e.ledger.windows, e.ledger.n_buys), (0, {}, 1))
        self.assertEqual({k: st['hits'][k] for k in ('counted', 'in_buys', 'pending', 'over_caps')},
                         {'counted': 57, 'in_buys': 50, 'pending': 0, 'over_caps': 7})
        self.assertEqual(st['buys']['recent'][0]['preview'], False)
        feed(e, [(4, 0)])
        close_hour(e, clock)
        self.assertEqual([b['amount_wei'] for b in records(e, 'buy')], [5 * 10 ** 14, 10 ** 15])
        self.assertEqual(e.public_status()['hits']['in_buys'], 54)

    def test_graduation_and_pin_stops(self):
        def run(**chain_attrs):
            ch = FakeChain()
            for k, v in chain_attrs.items():
                setattr(ch, k, v)
            venue = chain_attrs.pop('venue', 'auto') if 'venue' in chain_attrs else 'auto'
            clock = Clock()
            e = make_engine(tempfile.mkdtemp(dir=self.tmp), ch, cfg(hourly_budget_wei=12 * 10 ** 13, venue=venue),
                            clock)
            feed_hits(e, 12)
            close_hour(e, clock)
            return e, ch, clock

        e, ch, _ = run(venue='curve')                  # the literal rule: the curve graduated -> stop
        self.assertIn('graduated', e.stopped)
        self.assertEqual(e.public_status()['stopped'], 'the curve graduated (buys set to the curve only)')
        self.assertEqual(records(e, 'buy'), [])
        self.assertNotIn((bb.ROUTER, bb.SEL['execute']), ch.calls())
        e, ch, _ = run(phase=0, graduated=False)       # a live curve: bought on the curve, never the router
        b = records(e, 'buy')
        self.assertEqual(b[0]['venue'], 'curve')
        self.assertEqual(b[0]['tokens_wei'], 12 * 10 ** 13 * ch.curve_rate)
        self.assertNotIn((bb.ROUTER, bb.SEL['execute']), ch.calls())
        cb = [f for f in ch.froms if f[0] == bb.CURVE and f[1] == bb.SEL['curve_buy']]
        self.assertTrue(cb and all(f[2] == bb.BUYBACK_WALLET for f in cb), 'the curve buy is the buyback wallet\'s')
        calldata = bb.cd_curve_buy(1, 0, bb.BUYBACK_WALLET)
        self.assertEqual(decode(['uint256', 'uint256', 'address'], bytes.fromhex(calldata[10:]))[2].lower(),
                         bb.BUYBACK_WALLET.lower(), 'a curve buy sends the tokens to the buyer')
        e.tick()
        for attrs, what in (({'phase': 1}, 'phase 1'), ({'phase': 2, 'graduated': False}, 'phase 2'),
                            ({'token_curve': '0x' + '12' * 20}, 'token.curve()'),
                            ({'rec_curve': '0x' + '12' * 20}, 'record curve'),
                            ({'fee_recipient': '0x' + '34' * 20}, 'creator-fee recipient'),
                            ({'hook': '0x' + '56' * 20}, 'memeHook'), ({'tick': 60}, 'tick spacing'),
                            ({'buyback_enabled': 1}, 'buyback switch'), ({'pair': '0x' + '78' * 20}, 'pair token')):
            e, ch, clock = run(**attrs)
            self.assertIsNotNone(e.stopped, what)
            self.assertIn(what.split()[0], e.stopped)
            self.assertEqual(records(e, 'buy'), [], what)
            self.assertEqual(e.public_status()['stopped'], 'the chain no longer matches the pinned coin')
            feed_hits(e, 60, start_n=10)
            close_hour(e, clock)
            self.assertEqual(records(e, 'buy'), [], 'stopped stays stopped')

    def test_status_is_public_safe(self):
        clock = Clock()
        e = make_engine(self.tmp, self.chain, cfg(hourly_budget_wei=10 ** 15), clock)
        feed_hits(e, 12)
        close_hour(e, clock)
        feed_hits(e, 3, start_n=5)
        st = json.dumps(e.public_status())
        self.assertNotIn(bb.WALLET[2:].lower(), st.lower())
        self.assertNotIn(bb.BUYBACK_WALLET[2:].lower(), st.lower())
        import re
        self.assertIsNone(re.search(r'0x[0-9a-fA-F]{40}', st), 'no address of any kind')
        for k in ('claim', 'escrow', 'claimable', 'reserve'):
            self.assertNotIn(k, st.lower().replace('claimed creator fees', ''), k)
        # "budget" is the owner's hourly budget (the contract's status.budget), never the creator fees
        self.assertEqual(set(e.public_status()['budget']), {'hourly_eth', 'rule', 'set', 'preview_eth', 'funding'})
        self.assertIn(bb.DRY_LABEL, st)
        self.assertIn('does not understand money', st)
        self.assertIn('in the live view (the newest saved training checkpoint, playing in its own simulation)', st)
        self.assertIn('The hit rate is hits / (hits + misses + wrong clicks)', st)
        self.assertIn("that hour's $LABRAT buyback is the hourly budget x the hit rate, cut to the per-buy, hourly, "
                      'daily and total caps', st)
        # every string the status gives the site's "next buy" line is a fixed phrase the site accepts
        import re as _re
        self.assertTrue(_re.match(r'^[a-z][a-z ;.-]{0,59}$', e.public_status()['next_buy_note']))

    def test_status_splits_hits_and_never_says_due_while_blocked(self):
        """Hits counted = in buys + pending (this hour and a booked buy) + counted, bought nothing; "next buy"
        follows the hour, the caps and the skips; never "due now" while blocked."""
        clock = Clock()
        c = cfg(hourly_budget_wei=10 ** 15, max_day_wei=10 ** 15, max_hour_wei=10 ** 15, max_total_wei=10 ** 16)
        e = make_engine(self.tmp, self.chain, c, clock)
        feed_hits(e, 12)
        st = e.public_status()
        self.assertEqual((st['next_buy_in_s'], st['next_buy_note']), (HOUR - 60, 'waiting for the hour'))
        close_hour(e, clock)
        feed_hits(e, 8, start_n=3)
        st = e.public_status()
        self.assertEqual((st['next_buy_in_s'], st['next_buy_note']), (None, 'daily cap reached'))
        close_hour(e, clock)                                  # bought nothing: the daily cap
        feed_hits(e, 4, start_n=5)
        st = e.public_status()
        self.assertEqual({k: st['hits'][k] for k in ('counted', 'in_buys', 'pending', 'over_caps')},
                         {'counted': 24, 'in_buys': 12, 'pending': 4, 'over_caps': 8})
        self.assertIsNone(st['stopped'])
        # a skip is shown with its wait and a fixed reason, not "due now"; the booked hour's hits stay pending
        e2 = make_engine(os.path.join(self.tmp, 'g'), self.chain, cfg(hourly_budget_wei=10 ** 15), clock)
        feed_hits(e2, 4)
        self.chain.gas_price = 2 * 10 ** 9
        close_hour(e2, clock)
        feed_hits(e2, 2, start_n=1)
        st = e2.public_status()
        self.assertEqual(st['next_buy_note'], 'gas price over the cap')
        self.assertTrue(250 <= st['next_buy_in_s'] <= 301, st['next_buy_in_s'])
        self.assertEqual((st['hits']['pending'], st['pending']), (6, {'eth': '0.001', 'hits': 6}))
        # stopped: no next buy
        e2._stop('test', 'repeated failures')
        self.assertEqual((e2.public_status()['next_buy_in_s'], e2.public_status()['next_buy_note']), (None, 'stopped'))

    def test_status_marks_test_streams_and_other_relays(self):
        e = make_engine(self.tmp, self.chain)
        self.assertEqual(e.public_status()['source'], {'public_relay': True, 'accept_test_streams': False,
                                                       'test': False})
        e.on_relay_text(json.dumps(dict(HELLO, label='TEST x', test=True)))
        self.assertTrue(e.public_status()['source']['test'])
        journal = bb.Journal(os.path.join(self.tmp, 'o.jsonl'))
        rpc = bb.ReadRpc(self.chain)
        sim = bb.Sim(rpc, cfg())
        other = bb.Engine(cfg(), 'DRY', journal, rpc, bb.DryExecutor(sim, cfg()), sim,
                          relay_url='ws://127.0.0.1:4751/live')
        self.assertEqual(other.public_status()['source']['test'], True)
        other2 = bb.Engine(cfg(), 'DRY', journal, rpc, bb.DryExecutor(sim, cfg()), sim, accept_test=True)
        self.assertEqual(other2.public_status()['source'], {'public_relay': True, 'accept_test_streams': True,
                                                            'test': True})

    def test_gas_has_its_own_cap(self):
        clock = Clock()
        c = cfg(hourly_budget_wei=10 ** 15, max_gas_day_wei=25 * 10 ** 12)
        e = make_engine(self.tmp, self.chain, c, clock)
        feed_hits(e, 12)
        close_hour(e, clock)
        self.assertEqual(len(records(e, 'buy')), 1)
        feed_hits(e, 12, start_n=3)
        close_hour(e, clock)
        self.assertEqual(len(records(e, 'buy')), 1)
        self.assertIn('daily gas cap', records(e, 'buy_skip')[-1]['why'])
        self.assertEqual(e.public_status()['next_buy_note'], 'daily gas cap reached')
        clock.t += 86400
        feed_hits(e, 12, start_n=6)
        close_hour(e, clock)
        self.assertEqual(len(records(e, 'buy')), 2)
        e2 = make_engine(os.path.join(self.tmp, 't'), self.chain, cfg(hourly_budget_wei=10 ** 15,
                                                                        max_gas_total_wei=10 ** 13), clock)
        feed_hits(e2, 12)
        close_hour(e2, clock)
        self.assertEqual(records(e2, 'buy'), [])
        self.assertIn('total gas cap', records(e2, 'buy_skip')[-1]['why'])
        # a preview uses no gas cap
        e3 = make_engine(os.path.join(self.tmp, 'p'), self.chain, cfg(max_gas_total_wei=10 ** 13), clock)
        for h in range(2):
            feed_hits(e3, 12, start_n=h * 3)
            close_hour(e3, clock)
        self.assertEqual(len(records(e3, 'buy')), 2)

    def test_value_must_equal_what_the_tx_spends(self):
        """Review finding: the router keeps any msg.value above amountIn, so value == amountIn == SETTLE_ALL."""
        data = bb.cd_router_buy(10 ** 14, 10 ** 18, 2 ** 40)
        self.assertTrue(bb.check_value(bb.ROUTER, data, 10 ** 14))
        for v in (2 * 10 ** 14, 10 ** 14 + 1, 10 ** 14 - 1, 0):
            with self.assertRaises(ValueError):
                bb.check_value(bb.ROUTER, data, v)
        self.assertEqual(bb.router_amounts(data), (10 ** 14, 10 ** 14, 10 ** 18, 2 ** 40))
        self.assertTrue(bb.check_value(bb.CURVE, bb.cd_curve_buy(10 ** 13, 1), 10 ** 13))
        with self.assertRaises(ValueError):
            bb.check_value(bb.CURVE, bb.cd_curve_buy(10 ** 13, 1), 2 * 10 ** 13)
        self.assertTrue(bb.check_value(bb.FEE_ESCROW, bb.cd_claim(5), 0))
        with self.assertRaises(ValueError):
            bb.check_value(bb.FEE_ESCROW, bb.cd_claim(5), 1)
        with self.assertRaises(ValueError):
            bb.check_value(bb.TOKEN, '0x', 0)
        # the fake chain, like the real router, accepts the excess: only check_value stops it
        res, err = self.chain('eth_call', [{'from': bb.WALLET, 'to': bb.ROUTER, 'data': data,
                                            'value': hex(2 * 10 ** 14)}, 'latest'])
        self.assertIsNone(err)
        # a stranger's claim(uint256) reverts InsufficientBalance, as on chain
        _r, err = self.chain('eth_call', [{'from': '0x' + '99' * 20, 'to': bb.FEE_ESCROW, 'data': bb.cd_claim(10 ** 15)},
                                          'latest'])
        self.assertEqual(bb.revert_name(err), 'InsufficientBalance')

    def test_status_file_retries_a_failed_write_and_stays_fresh(self):
        e = make_engine(self.tmp, self.chain)
        clock = Clock(1000.0)
        path = os.path.join(self.tmp, 'status.json')
        sf = bb.StatusFile(path, every=10, clock=clock)
        real = bb.write_json_atomic
        try:
            bb.write_json_atomic = lambda p, o: False           # e.g. a reader holds the file on Windows
            e.on_relay_connected(False)
            self.assertIs(sf.update(e), False)
            self.assertTrue(e.changed, 'a failed write keeps the change pending')
        finally:
            bb.write_json_atomic = real
        self.assertIs(sf.update(e), True)
        self.assertFalse(e.changed)
        self.assertIsNone(sf.update(e), 'nothing changed and it is fresh')
        clock.t += 10
        self.assertIs(sf.update(e), True, 'rewritten every 10 s so "updated" shows the engine is alive')
        with _real_open(path, encoding='utf-8') as f:
            self.assertEqual(json.load(f)['relay']['connected'], False)

    def test_reconnect_backoff(self):
        waits, b = [], 1.0
        for _ in range(7):                            # a relay that accepts and closes at once, again and again
            w, b = bb.reconnect_wait(b, 0.1)
            waits.append(w)
        self.assertEqual(waits, [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0])
        self.assertEqual(bb.reconnect_wait(b, 45.0), (1.0, 2.0), 'a connection that stayed up resets it')


class TestRelayOverWebsocket(Base):
    def test_reconnect_dedup_and_origin(self):
        from websockets.sync.server import serve
        seen_origins, conns = [], []
        script = [
            [state(True, episode=None), ep(0, 2), frame(True, (0.5, 0.5), (0.5, 0.5, 0.1, 0.1)), ep(1, 3)],   # then a drop
            [state(True, episode=json.loads(ep(1, 3))), json.dumps(HELLO), ep(0, 2), ep(1, 3), ep(2, 4)],  # resync
        ]

        def handler(ws):
            seen_origins.append(ws.request.headers.get('Origin'))
            i = len(conns)
            conns.append(ws)
            for m in script[i] if i < len(script) else [state(True, episode=json.loads(ep(2, 4)))]:
                ws.send(m)
            if i == 0:
                time.sleep(0.2)
                ws.close()
                return
            try:
                ws.recv(timeout=5)
            except Exception:
                pass

        srv = serve(handler, '127.0.0.1', MOCK_RELAY_PORT)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        e = make_engine(self.tmp, self.chain)
        stop = threading.Event()
        lis = bb.RelayListener(f'ws://127.0.0.1:{MOCK_RELAY_PORT}/live', bb.ORIGIN, e, stop).start()
        try:
            deadline = time.time() + 10
            while time.time() < deadline and e.ledger.hits < 9:
                time.sleep(0.05)
            time.sleep(0.5)
        finally:
            stop.set()
            lis.thread.join(5)
            srv.shutdown()
        self.assertEqual(e.ledger.hits, 9, '2 + 3 + 4: the repeats after the reconnect were not counted again')
        self.assertEqual([r['n'] for r in records(e, 'hit')], [0, 1, 2])
        self.assertGreaterEqual(lis.connects, 2)
        self.assertTrue(seen_origins and all(o == 'https://lab-rat.net' for o in seen_origins), seen_origins)
        self.assertGreaterEqual(e.counter.stats['duplicates'], 3)


class TestDryNeverTouchesEnvOrSend(Base):
    def test_read_rpc_refuses_sends(self):
        rpc = bb.ReadRpc(self.chain)
        for m in ('eth_sendRawTransaction', 'eth_sendTransaction', 'eth_sign', 'personal_sendTransaction'):
            with self.assertRaises(bb.SendRefused):
                rpc.raw(m, ['0x00'])
        with self.assertRaises(bb.LiveRefused):
            bb.LiveRpc(self.chain)
        with self.assertRaises(bb.LiveRefused):
            bb.LiveExecutor(rpc, MockSigner(), None, None, None, cfg())

    def test_main_dry_end_to_end(self):
        """main() in DRY with the fake chain and the mock relay: hits counted into an hour window (a 2 s window here,
        --window-s, tests only), the window closed and its PREVIEW buy simulated from the buyback wallet, status
        served, and no .env read, no signer, no claim, no send method anywhere."""
        from websockets.sync.server import serve
        msgs = [state(True, episode=None)] + [ep(n, 4) for n in range(13)]

        def handler(ws):
            for m in msgs:
                ws.send(m)
            try:
                ws.recv(timeout=10)
            except Exception:
                pass
        srv = serve(handler, '127.0.0.1', MOCK_RELAY_PORT)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old_rpc = bb.launcher.rpc
        bb.launcher.rpc = self.chain                  # ReadRpc()'s default transport, for main() only
        bb.STATE['env_read'] = False
        out = {}

        def run():
            out['rc'] = bb.main(['--relay', f'ws://127.0.0.1:{MOCK_RELAY_PORT}/live', '--journal-dir', self.tmp,
                                 '--duration', '5', '--status-port', str(STATUS_PORT), '--tick', '0.2',
                                 '--window-s', '2', '--hourly-budget-eth', 'unset'])
        t = threading.Thread(target=run)
        t.start()
        status = None
        try:
            for _ in range(40):
                time.sleep(0.1)
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{STATUS_PORT}/status', timeout=2) as r:
                        status = json.loads(r.read())
                    if status['buys']['count']:
                        break
                except OSError:
                    pass
            t.join(15)
        finally:
            srv.shutdown()
            bb.launcher.rpc = old_rpc
        self.assertEqual(out.get('rc'), 0)
        self.assertIsNotNone(status)
        self.assertEqual(status['mode'], 'DRY')
        self.assertEqual(status['label'], 'DRY - simulated, not executed')
        self.assertEqual(status['hits']['counted'], 52)
        self.assertGreaterEqual(status['buys']['count'], 1)
        self.assertTrue(status['buys']['recent'][0]['preview'])
        self.assertEqual(status['budget']['hourly_eth'], None)
        self.assertIsNotNone(status['last_window'])
        self.assertNotIn(bb.WALLET[2:].lower(), json.dumps(status).lower())
        self.assertNotIn(bb.BUYBACK_WALLET[2:].lower(), json.dumps(status).lower())
        self.assertFalse(bb.STATE['env_read'], '.env was read in DRY')
        methods = {m for m, _t, _s in self.chain.log}
        self.assertFalse(methods & {'eth_sendRawTransaction', 'eth_sendTransaction'})
        self.assertLessEqual(methods, bb.READ_METHODS)
        with _real_open(os.path.join(self.tmp, 'journal.jsonl'), encoding='utf-8') as f:
            j = [json.loads(l) for l in f]
        self.assertTrue(all(r['mode'] == 'DRY' for r in j))
        self.assertEqual([r['ev'] for r in j if r['ev'] == 'claim'], [], 'DRY buys come from the buyback wallet')
        buys = [r for r in j if r['ev'] == 'buy']
        self.assertTrue(buys and all(b['preview'] and b['buyer'] == bb.BUYBACK_WALLET for b in buys))
        self.assertEqual(sum(w['hits'] for w in j if w['ev'] == 'window'), 52)
        self.assertEqual(sum(b['hits_covered'] for b in buys), 52)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, 'status.json')))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'journal_live.jsonl')))


class TestLiveGates(Base):
    """LIVE is refused at every gate; the tests never have the launch wallet's key, so the key gate is always the
    last one a real run could pass. Past it, only a patched Account.from_key (a fake with the wallet's ADDRESS and
    no key) reaches the chain gates, and only with the fake chain."""

    def setUp(self):
        super().setUp()
        self._reader = bb._env_file_reader
        self._live_dir = bb.LIVE_JOURNAL_DIR
        self._ready = bb.LIVE_BUYER_READY
        bb.LIVE_BUYER_READY = True                     # past the pause gate, to test the gates behind it
        bb.LIVE_JOURNAL_DIR = self.tmp                 # LIVE's one fixed journal place, for the test
        self.env = {}
        bb._env_file_reader = lambda: dict(self.env)
        self._env_keys = {k: os.environ.pop(k) for k in ('BUYBACK_LIVE', 'BUYBACK_RH_KEY', 'RATBRAIN_RH_KEY',
                                                         'RATBRAIN_RPC') if k in os.environ}

    def tearDown(self):
        bb._env_file_reader = self._reader
        bb.LIVE_JOURNAL_DIR = self._live_dir
        bb.LIVE_BUYER_READY = self._ready
        os.environ.update(self._env_keys)
        super().tearDown()

    def refused(self, args, contains):
        with self.assertRaises(SystemExit) as cm:
            bb.main(args)
        msg = str(cm.exception.code)
        self.assertIn('LIVE refused', msg)
        self.assertIn(contains, msg)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'journal_live.jsonl')))
        self.assertFalse(os.path.exists(bb.lock_path(self.tmp)))
        return msg

    def test_cli_and_env_gates(self):
        from eth_account import Account
        L = ['--live', '--confirm', 'LABRAT', '--hourly-budget-eth', '0.001']
        self.refused(['--confirm', 'LABRAT'], '--live was not given')
        self.refused(['--clear-stop'], '--live was not given')
        self.refused(['--first-nonce', '1'], '--live was not given')
        self.refused(['--live'], '--confirm must be LABRAT')
        self.refused(['--live', '--confirm', 'labrat'], '--confirm must be LABRAT')
        # the pause: this LIVE path signs from the launch wallet, the buys are now the buyback wallet's
        bb.LIVE_BUYER_READY = False
        try:
            bb.STATE['env_read'] = False
            self.env = {'BUYBACK_LIVE': '1', 'BUYBACK_RH_KEY': 'anything'}
            self.refused(L, 'LIVE is paused')
            self.assertFalse(bb.STATE['env_read'], 'refused before .env is read')
        finally:
            bb.LIVE_BUYER_READY = True
        # LIVE never runs a preview, and buys only once per UTC hour
        self.refused(['--live', '--confirm', 'LABRAT', '--hourly-budget-eth', 'unset'], 'never runs a preview')
        self.refused(L + ['--window-s', '2'], 'once per UTC hour')
        self.refused(L + ['--accept-test-streams'], 'DRY tests only')
        self.refused(L + ['--relay', f'ws://127.0.0.1:{MOCK_RELAY_PORT}/live'], 'only to the public relay')
        self.refused(L + ['--origin', 'https://evil.example'], 'only to the public relay')
        # review finding: a fresh --journal-dir would reset the caps, the lock and the unresolved gate
        self.refused(L + ['--journal-dir', os.path.join(self.tmp, 'elsewhere')], '--journal-dir is refused in LIVE')
        # review finding: a stray RPC override must never be LIVE's only source of truth
        os.environ['RATBRAIN_RPC'] = 'http://127.0.0.1:8545'
        try:
            self.refused(L, 'RATBRAIN_RPC is set')
        finally:
            del os.environ['RATBRAIN_RPC']
        self.env = {}
        self.refused(L, 'BUYBACK_LIVE=1')
        self.env = {'BUYBACK_LIVE': '0'}
        self.refused(L, 'BUYBACK_LIVE=1')
        self.env = {'BUYBACK_LIVE': '1'}
        self.refused(L, 'no BUYBACK_RH_KEY')
        self.env = {'BUYBACK_LIVE': '1', 'BUYBACK_RH_KEY': 'not a key'}
        self.refused(L, 'does not parse')
        throwaway = Account.create()                # a fresh random key made here: not the launch wallet
        self.env = {'BUYBACK_LIVE': '1', 'BUYBACK_RH_KEY': '0x' + bytes(throwaway.key).hex()}
        msg = self.refused(L, 'not the launch wallet')
        self.assertNotIn(bytes(throwaway.key).hex(), msg)
        self.env = {'BUYBACK_LIVE': '1', 'RATBRAIN_RH_KEY': '0x' + bytes(throwaway.key).hex()}
        self.refused(L, 'not the launch wallet')
        os.environ['BUYBACK_LIVE'] = '0'
        try:
            self.env = {'BUYBACK_LIVE': '1'}
            self.refused(L, 'differs from .env')
        finally:
            del os.environ['BUYBACK_LIVE']

        def raising():
            raise launcher.LaunchRefused('.env could not be read (UnicodeDecodeError): save it as plain UTF-8 text')
        bb._env_file_reader = raising
        self.refused(L, 'could not be read')
        self.assertEqual(TRAPS['signs'], [])

    def gate(self, chain, *extra, c=None):
        a = bb.parse_args(['--live', '--confirm', 'LABRAT', *extra])
        return bb.live_gate(a, c or cfg(hourly_budget_wei=10 ** 15), self.tmp, transport=chain, nodes=[chain])

    def gate_refused(self, chain, contains, *extra, c=None):
        with self.assertRaises(SystemExit) as cm:
            self.gate(chain, *extra, c=c)
        self.assertIn(contains, str(cm.exception.code))
        self.assertFalse(os.path.exists(bb.lock_path(self.tmp)), 'a refusal after the lock removes it')

    def append(self, *recs):
        with _real_open(os.path.join(self.tmp, 'journal_live.jsonl'), 'a', encoding='utf-8') as f:
            for r in recs:
                f.write((r if isinstance(r, str) else json.dumps(dict({'t': time.time(), 'mode': 'LIVE'}, **r))) + '\n')

    def test_chain_gates_with_a_fake_wallet_address(self):
        import eth_account
        real_from_key = eth_account.Account.__dict__['from_key']
        eth_account.Account.from_key = staticmethod(lambda k: SimpleNamespace(address=bb.WALLET))
        self.env = {'BUYBACK_LIVE': '1', 'BUYBACK_RH_KEY': 'placeholder'}
        try:
            ch = FakeChain()
            ch.chain_id = 1
            self.gate_refused(ch, 'chain id 1')
            ch = FakeChain()
            ch.fee_recipient = '0x' + '34' * 20
            self.gate_refused(ch, 'pinned chain checks failed')
            self.gate_refused(FakeChain(), 'graduated', c=cfg(venue='curve', hourly_budget_wei=10 ** 15))
            # every gate passes -> the gated LiveRpc and the lock; a second one is refused by the lock
            rpc, acct, lock = self.gate(FakeChain())
            self.assertIsInstance(rpc, bb.LiveRpc)
            self.assertTrue(os.path.exists(lock))
            with self.assertRaises(SystemExit) as cm:
                self.gate(FakeChain())
            self.assertIn('live.lock exists', str(cm.exception.code))
            os.remove(lock)
        finally:
            setattr(eth_account.Account, 'from_key', real_from_key)
        self.assertEqual(TRAPS['signs'], [])

    def test_journal_gates_nonce_stop_and_bad_lines(self):
        """Review findings: the journal must match the wallet's nonce on chain (a lost / foreign journal, a second
        engine or an outside transaction is refused), a stop survives restarts until --clear-stop, and an
        unreadable line refuses LIVE."""
        import eth_account
        real_from_key = eth_account.Account.__dict__['from_key']
        eth_account.Account.from_key = staticmethod(lambda k: SimpleNamespace(address=bb.WALLET))
        self.env = {'BUYBACK_LIVE': '1', 'BUYBACK_RH_KEY': 'placeholder'}
        jl = os.path.join(self.tmp, 'journal_live.jsonl')
        try:
            ch = FakeChain()
            ch.nonce = 2                                   # the wallet sent something outside this engine
            self.gate_refused(ch, 'accounts for 1')
            self.gate_refused(ch, 'is not the launch wallet\'s nonce', '--first-nonce', '3')
            _rpc, _a, lock = self.gate(ch, '--first-nonce', '2')     # the operator's explicit anchor: accepted
            os.remove(lock)
            self.assertEqual([r['ev'] for r in bb.Journal(jl).records()], ['anchor'])
            _rpc, _a, lock = self.gate(ch)                 # and remembered
            os.remove(lock)
            self.gate_refused(ch, 'only for the first LIVE run', '--first-nonce', '2')
            # a journalled transaction the chain does not account for (e.g. a journal from another wallet state)
            self.append({'ev': 'signed', 'kind': 'buy', 'tx': '0x' + 'ab' * 32, 'raw': '0x00', 'nonce': 2},
                        {'ev': 'gas', 'tx': '0x' + 'ab' * 32, 'kind': 'buy', 'gas_cost_wei': 1})
            self.gate_refused(ch, 'accounts for 3')
            ch.nonce = 3
            _rpc, _a, lock = self.gate(ch)
            os.remove(lock)
            ch.nonce = 4                                   # one more than the journal: another engine / outside tx
            self.gate_refused(ch, 'accounts for 3')
            ch.nonce = 3
            # a stop survives the restart until the operator clears it
            self.append({'ev': 'stop', 'why': '3 failed claims / buys in a row', 'public': 'repeated failures'})
            self.gate_refused(ch, 'start with --clear-stop')
            _rpc, _a, lock = self.gate(ch, '--clear-stop')
            os.remove(lock)
            self.assertEqual(bb.Journal(jl).records()[-1]['ev'], 'stop_cleared')
            _rpc, _a, lock = self.gate(ch)
            os.remove(lock)
            # an unreadable line: refused (a damaged 'signed' line would hide a real transaction)
            self.append('{"t": 1, "mode": "LIVE", "ev": "sig')
            self.gate_refused(ch, 'unreadable line')
        finally:
            setattr(eth_account.Account, 'from_key', real_from_key)
        self.assertEqual(TRAPS['signs'], [])


class TestLiveExecutorOnTheFakeChain(Base):
    """The LIVE path's sequencing with MockSigner (no key) and FakeChain (no network). LIVE is paused (live_gate
    refuses it); these tests build the executor directly, as live_gate would after every gate. The hourly budget
    is 0.00012 ETH, so a perfect hour buys 0.00012 ETH."""
    live_test = True

    def make(self, clock, c=None, transport=None, nodes=None, journal_name='journal_live.jsonl'):
        c = c or cfg(hourly_budget_wei=12 * 10 ** 13)
        journal = bb.Journal(os.path.join(self.tmp, journal_name), clock=clock)
        rpc = bb.LiveRpc(transport or self.chain, _gate=bb._GATE_PASSED, nodes=nodes or [self.chain])
        sim = bb.Sim(rpc, c, clock=clock, buyer=bb.WALLET)
        self.signer = MockSigner()
        ex = bb.LiveExecutor(rpc, self.signer, journal, bb.Ledger(c), sim, c, sleep=lambda s: None, poll_s=0)
        eng = bb.Engine(c, 'LIVE', journal, rpc, ex, sim, clock=clock)
        journal_path = journal.path

        def on_send(raw, txh):     # write-ahead: the signed bytes are in the journal before the broadcast
            with _real_open(journal_path, encoding='utf-8') as f:
                lines = [json.loads(l) for l in f]
            self.assertTrue(any(r['ev'] == 'signed' and r['raw'] == raw and r['tx'] == txh for r in lines))
            if self.chain.sent.count(raw) == 1:          # its first broadcast: nothing says 'sent' yet
                self.assertFalse(any(r['ev'] == 'sent' and r['tx'] == txh for r in lines))
        self.chain.on_send = on_send
        return eng

    def restart(self, e, clock, transport=None, nodes=None):
        """A new process on the same journal: a fresh executor, ledger and engine."""
        return self.make(clock, e.cfg, transport, nodes)

    def test_claim_then_buy_nonces_receipts(self):
        clock = Clock()
        e = self.make(clock)
        feed_hits(e, 12)
        e.tick()
        self.assertEqual(self.signer.signed, [], 'nothing before the hour ends')
        close_hour(e, clock)
        tos = [to_checksum_address(t['to']) for t in self.signer.signed]
        self.assertEqual(tos, [bb.FEE_ESCROW, bb.ROUTER], 'the claim is signed and mined before the buy')
        self.assertEqual([t['nonce'] for t in self.signer.signed], [1, 2])
        self.assertEqual(self.signer.signed[1]['value'], 12 * 10 ** 13)
        self.assertEqual(self.signer.signed[0]['chainId'], 4663)
        # ONE booking record per receipt: it journals what the tx did and resolves it
        evs = [r['ev'] for r in records(e) if r['ev'] in ('signed', 'sent', 'mined', 'claim', 'buy')]
        self.assertEqual(evs, ['signed', 'sent', 'claim', 'signed', 'sent', 'buy'])
        self.assertEqual([r['tx'] for r in records(e, 'claim') + records(e, 'buy')],
                         [r['tx'] for r in records(e, 'signed')])
        b = records(e, 'buy')[0]
        self.assertFalse(b['simulated'])
        self.assertEqual(b['status'], 'mined')
        self.assertEqual(b['tokens_wei'], 12 * 10 ** 13 * self.chain.rate)
        self.assertEqual(e.ledger.claimed, e.cfg.claim_max_wei)
        self.assertEqual(e.ledger.unresolved, {})
        self.assertEqual(e.ledger.next_nonce(), 3)
        self.assertEqual(e.public_status()['label'], bb.LIVE_LABEL)
        self.check_invariants(e)
        # min out was the fresh simulation minus 3 %
        _amt, min_out, deadline = decode_router(self.signer.signed[1]['data'])
        self.assertEqual(min_out, 12 * 10 ** 13 * self.chain.rate * 97 // 100)
        self.assertGreater(deadline, self.chain.ts)

    def test_crash_between_receipt_and_booking_books_once(self):
        """Review finding: a crash (or a failing journal write) after the buy was mined but before it was booked
        must not buy the same batch again after a restart."""
        clock = Clock()
        e = self.make(clock)
        feed_hits(e, 12)
        real_append = e.journal.append

        def crash_on_buy(rec):
            if rec.get('ev') == 'buy':
                raise OSError(28, 'No space left on device')
            return real_append(rec)
        e.journal.append = crash_on_buy
        close_hour(e, clock)
        self.assertIsNotNone(e.stopped, 'a journal that cannot be written stops the engine')
        self.assertEqual(len(self.signer.signed), 2)
        self.assertEqual(len(self.chain.receipts), 2, 'the buy WAS mined')
        self.assertEqual(records(e, 'buy'), [])
        # the restart (after an operator cleared the stop): the mined buy is booked from its receipt, not re-bought
        real_append({'mode': 'LIVE', 'ev': 'stop_cleared', 'why': 'test'})
        clock.t += 60
        e2 = self.restart(e, clock)
        self.assertEqual(len(e2.ledger.unresolved), 1)
        self.assertEqual(e2.ledger.pending, 12 * 10 ** 13)
        e2.tick()
        self.assertEqual(len(self.signer.signed), 0, 'nothing new was signed')
        self.assertEqual(len(records(e2, 'buy')), 1)
        self.assertEqual((e2.ledger.pending, e2.ledger.bought, e2.ledger.unresolved), (0, 12 * 10 ** 13, {}))
        self.assertEqual(len(self.chain.sent), 2, 'the batch was bought exactly once')
        e3 = self.restart(e2, clock)
        self.assertEqual((e3.ledger.n_buys, e3.ledger.pending), (1, 0))

    def test_bad_journal_line_refuses_live(self):
        clock = Clock()
        with _real_open(os.path.join(self.tmp, 'journal_live.jsonl'), 'w', encoding='utf-8') as f:
            f.write('{"t": 1, "mode": "LIVE", "ev": "signed", "tx": "0xab\n')
        with self.assertRaises(bb.LiveRefused):
            self.make(clock)

    def test_unconfirmed_blocks_then_rebroadcast_then_dropped(self):
        clock = Clock()
        e = self.make(clock)
        feed_hits(e, 12)
        self.chain.auto_mine = False
        close_hour(e, clock)                          # the claim is sent but not mined
        self.assertEqual([r['ev'] for r in records(e) if r['ev'] in ('signed', 'unconfirmed')], ['signed', 'unconfirmed'])
        self.assertEqual(len(self.signer.signed), 1)
        clock.t += 200
        e.tick()                                      # still in the mempool: nothing new is signed
        self.assertEqual(len(self.signer.signed), 1)
        self.chain.forget = True                      # the chain forgot it: the SAME bytes again
        raw0 = self.chain.sent[0]
        clock.t += 200
        e.tick()
        self.assertEqual(self.chain.sent[-1], raw0)
        self.assertEqual(len(self.signer.signed), 1)
        self.assertEqual(records(e, 'rebroadcast')[0]['tx'], records(e, 'signed')[0]['tx'])
        # it lands: the claim is booked late, then the buy goes out on the next batch
        self.chain.forget = False
        self.chain.auto_mine = True
        self.chain.mine(records(e, 'signed')[0]['tx'])
        clock.t += 200
        e.tick()
        self.assertEqual(len(records(e, 'claim')), 1)
        self.assertEqual(len(records(e, 'buy')), 1)
        # a restart replays the journal: nothing unresolved, the same ledger
        e2 = bb.Engine(e.cfg, 'LIVE', e.journal, e.rpc, e.executor, e.sim, clock=clock)
        self.assertEqual((e2.ledger.n_buys, e2.ledger.claimed, e2.ledger.unresolved),
                         (1, e.ledger.claimed, {}))
        # a transaction whose nonce another transaction used is marked dropped (every RPC, twice, 120 s apart),
        # never re-sent
        feed_hits(e2, 12, start_n=10)
        self.chain.auto_mine = False
        close_hour(e2, clock)
        stuck = [r for r in records(e2, 'signed')][-1]
        self.chain.pending_txs.clear()
        self.chain.forget = True
        self.chain.nonce += 1                          # the owner sent something else at that nonce
        n_sent = len(self.chain.sent)
        clock.t += 200
        self.chain.auto_mine = True
        e2.tick()
        self.assertEqual(records(e2, 'dropped'), [], 'one observation is not enough')
        self.assertEqual(records(e2, 'drop_suspect')[0]['tx'], stuck['tx'])
        self.assertEqual(len(self.chain.sent), n_sent)
        clock.t += 60
        e2.tick()                                      # still inside DROP_CONFIRM_S
        self.assertEqual(records(e2, 'dropped'), [])
        clock.t += bb.DROP_CONFIRM_S
        e2.tick()
        self.assertEqual(records(e2, 'dropped')[0]['tx'], stuck['tx'])
        self.assertEqual(len(self.chain.sent), n_sent + 1, 'after the drop, a NEW buy (new nonce), not the old bytes')
        self.assertNotEqual(self.chain.sent[-1], stuck['raw'])

    def test_a_lagging_rpc_never_drops_a_mined_buy(self):
        """Review finding: the receipt lookup can hit a lagging node while the nonce lookup hits one that is not;
        every pinned RPC is asked separately, so a node that has the receipt books it."""
        clock = Clock()
        lag = Lagging(self.chain)
        e = self.make(clock, transport=lag, nodes=[lag, self.chain])
        feed_hits(e, 12)
        close_hour(e, clock)                          # the claim: 'unconfirmed' on the lagging primary
        self.assertEqual(len(self.chain.receipts), 1, 'it WAS mined')
        clock.t += 200
        e.tick()                                      # the second node has the receipt: booked, then the buy
        self.assertEqual(records(e, 'dropped') + records(e, 'drop_suspect'), [])
        self.assertEqual(len(records(e, 'claim')), 1)
        self.assertEqual(e.ledger.claimed, e.cfg.claim_max_wei)
        # a node that cannot be reached is never "no receipt"
        self.chain = FakeChain()
        lag2 = Lagging(self.chain)
        e2 = self.make(clock, transport=lag2, nodes=[lag2, lambda m, p: (_ for _ in ()).throw(OSError('down'))],
                       journal_name='j2.jsonl')
        feed_hits(e2, 12)
        close_hour(e2, clock)
        for _ in range(3):
            clock.t += 200
            e2.tick()
        self.assertEqual(records(e2, 'dropped') + records(e2, 'drop_suspect'), [])
        self.assertEqual(len(e2.ledger.unresolved), 1, 'left unresolved (blocks new txs), never dropped')

    def test_stop_survives_restart_and_gas_counts(self):
        """Review finding: 3 failures stop LIVE, and a restart stays stopped (an operator clears it); reverted
        transactions' gas is booked into the gas caps."""
        clock = Clock()
        e = self.make(clock)
        feed_hits(e, 12)
        close_hour(e, clock)                          # claim + buy
        feed_hits(e, 12, start_n=3)
        self.chain.rate = 0                           # every buy now reverts in simulation
        close_hour(e, clock)                          # the hour's buy fails, and is retried twice
        for _ in range(2):
            clock.t += 700
            e.tick()
        self.assertIsNotNone(e.stopped)
        self.assertEqual(records(e, 'stop')[-1]['public'], 'repeated failures')
        e2 = self.restart(e, clock)
        self.assertIsNotNone(e2.stopped, 'the stop survives a restart')
        self.assertEqual(e2.public_status()['stopped'], 'repeated failures')
        n = len(self.signer.signed)
        clock.t += 700
        e2.tick()
        self.assertEqual(len(self.signer.signed), n)
        # a mined-but-reverted buy books its gas (into the gas window) and resolves the tx in one record
        e.journal.append({'mode': 'LIVE', 'ev': 'stop_cleared', 'why': 'test'})
        e3 = self.restart(e, clock)
        self.assertIsNone(e3.stopped)
        self.assertEqual(e3.failures, 0)
        self.chain.rate = 34_913
        gas_before = e3.ledger.gas_total
        tx = {'nonce': self.chain.nonce, 'to': bb.ROUTER, 'value': 10 ** 14,
              'data': bb.cd_router_buy(10 ** 14, 10 ** 30, 2 ** 40)}
        raw = '0x' + (b'MOCK' + json.dumps(tx, sort_keys=True).encode()).hex()
        txh = '0x' + keccak(bytes.fromhex(raw[2:])).hex()
        e3.ledger.apply(e3.journal.append({'mode': 'LIVE', 'ev': 'signed', 'kind': 'buy', 'tx': txh, 'raw': raw,
                                           'nonce': tx['nonce'], 'to': bb.ROUTER, 'value': 10 ** 14, 'max_fee': 1,
                                           'venue': 'pool', 'min_out_wei': 10 ** 30}))
        self.chain.mine(txh, dict(tx))                # min out not met: status 0
        self.assertTrue(e3.executor.resolve(e3._book))
        g = records(e3, 'gas')[-1]
        self.assertEqual((g['tx'], g['status']), (txh, 'reverted'))
        self.assertGreater(e3.ledger.gas_total, gas_before)
        self.assertEqual(e3.ledger.unresolved, {})

    def test_nonce_mismatch_stops_live(self):
        """Review finding: a transaction sent from the wallet outside this engine (or a second engine) is caught
        before anything is signed, and LIVE stops for an operator."""
        clock = Clock()
        e = self.make(clock)
        feed_hits(e, 12)
        self.chain.nonce = 5
        close_hour(e, clock)
        self.assertEqual(self.signer.signed, [])
        self.assertIn('expects 1', e.stopped)
        self.assertEqual(e.public_status()['stopped'], 'stopped for an operator check')

    def test_live_never_sends_a_preview_and_simulates_from_the_signer(self):
        clock = Clock()
        e = self.make(clock, cfg())                   # no hourly budget
        feed_hits(e, 12)
        close_hour(e, clock)
        w, = records(e, 'window')
        self.assertEqual((w['note'], w['amount_wei']), ('budget not set', 0))
        self.assertEqual((self.signer.signed, self.chain.sent), ([], []))
        with self.assertRaises(bb.LiveRefused):
            e.executor.buy('pool', 10 ** 14, {'timestamp': self.chain.ts, 'preview': True, 'window': 0}, e._book)
        rpc = bb.LiveRpc(self.chain, _gate=bb._GATE_PASSED, nodes=[self.chain])
        with self.assertRaises(bb.LiveRefused):         # the buyback wallet as the buyer: not the signer
            bb.LiveExecutor(rpc, MockSigner(), e.journal, bb.Ledger(e.cfg), bb.Sim(rpc, e.cfg), e.cfg)
        self.assertEqual(self.signer.signed, [])

    def test_excess_value_is_never_signed(self):
        clock = Clock()
        e = self.make(clock)
        data = bb.cd_router_buy(10 ** 14, 1, 2 ** 40)
        with self.assertRaises(ValueError):
            e.executor._send('buy', bb.ROUTER, data, 2 * 10 ** 14, 160_000, {}, e._book)
        self.assertEqual(self.signer.signed, [])
        self.assertEqual(self.chain.sent, [])


# ---------------------------------------------------------------------------------------------------- real run
DEMO_DIR = os.path.join(ROOT, 'runs', 'buyback_demo')


def record(seconds, run='runs/steer_v1'):
    """Real episode messages: publish_training.py --dry-print plays a saved checkpoint (TEST-labelled) and prints."""
    os.makedirs(DEMO_DIR, exist_ok=True)
    out = os.path.join(DEMO_DIR, 'recorded.txt')
    with _real_open(out, 'w', encoding='utf-8') as f:
        subprocess.run([sys.executable, os.path.join(LIVE_DIR, 'publish_training.py'), '--dry-print', '--run', run,
                        '--assume-live-for-test', '--duration', str(seconds)], cwd=ROOT, stdout=f, check=True)
    return out


def load_recording(path):
    """-> [(kind, payload, episode_index)]: the text messages and the click frames, in order."""
    out = []
    with _real_open(path, encoding='utf-8') as f:
        for line in f:
            if line.startswith('TEXT '):
                m = json.loads(line[5:])
                if m.get('type') in ('hello', 'checkpoint', 'episode', 'bye'):
                    out.append(('text', line[5:].strip(), m.get('n')))
            elif line.startswith('FRAME ') and ' click=1 ' in line:
                epi = int(line.split(' ep=')[1].split()[0])
                out.append(('frame', base64.b64decode(line.split('b64=')[1].strip()), epi))
    return out


def fake_relay(port, items, pace):
    """A local relay on 127.0.0.1:<port>/live that plays a recording: the first connection gets the state, the click
    frames and episodes up to episode 3, then drops; the reconnect gets a state repeating episode 3, a resent
    episode 2 (a publisher resync), then the rest and bye. Refuses any Origin but https://lab-rat.net."""
    from websockets.sync.server import serve
    hello = next(p for k, p, _ in items if k == 'text' and '"hello"' in p)
    body = [(k, p, n) for k, p, n in items if not (k == 'text' and '"hello"' in p)]
    episodes = {n: p for k, p, n in body if k == 'text' and '"episode"' in p}
    cut = next(i for i, (k, p, n) in enumerate(body) if k == 'text' and n == 3) + 1
    conns = []

    def handler(ws):
        if ws.request.headers.get('Origin') != 'https://lab-rat.net':
            ws.close(1008)
            return
        i = len(conns)
        conns.append(1)
        h = json.loads(hello)
        if i == 0:
            ws.send(json.dumps({'type': 'state', 'live': True, 'hello': h, 'checkpoint': None, 'episode': None,
                                'history': []}))
            part = body[:cut]
        else:
            ws.send(json.dumps({'type': 'state', 'live': True, 'hello': h, 'checkpoint': None,
                                'episode': json.loads(episodes[3]), 'history': []}))
            ws.send(hello)
            ws.send(episodes[2])
            part = body[cut:]
        for k, p, n in part:
            ws.send(p)
            if k == 'text' and '"episode"' in p:
                time.sleep(pace)
        if i == 0:
            ws.close()
            return
        try:
            ws.recv(timeout=600)
        except Exception:
            pass
    srv = serve(handler, '127.0.0.1', port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def journal_tail(path, start):
    with _real_open(path, encoding='utf-8') as f:
        f.seek(start)
        return [l.rstrip('\n') for l in f if l.strip()]


def real_run(a):
    install_traps(network=True)
    ok = True
    calls = []
    real_raw = bb.ReadRpc.raw

    def spy(self, method, params):
        calls.append(method)
        return real_raw(self, method, params)
    bb.ReadRpc.raw = spy
    jA = os.path.join(ROOT, 'runs', 'buyback')
    os.makedirs(jA, exist_ok=True)
    pA = os.path.join(jA, 'journal.jsonl')
    startA = os.path.getsize(pA) if os.path.exists(pA) else 0
    print(f'\n=== A. DRY against the REAL relay ({bb.RELAY_URL}, Origin {bb.ORIGIN}) and the real chain, '
          f'{a.real_seconds:g} s ===', flush=True)
    bb.main(['--journal-dir', jA, '--duration', str(a.real_seconds), '--status-port', str(REAL_STATUS_PORT)])
    linesA = journal_tail(pA, startA)
    for l in linesA:
        print(l)
    hitsA = sum(1 for l in linesA if json.loads(l)['ev'] == 'hit')
    if hitsA == 0:
        rec = a.recorded or os.path.join(DEMO_DIR, 'recorded.txt')
        if not os.path.exists(rec):
            rec = record(110)
        items = load_recording(rec)
        n_eps = sum(1 for k, p, _ in items if k == 'text' and '"episode"' in p)
        print(f'\n=== B. the real relay had no live training, so: a LOCAL relay on 127.0.0.1:{FAKE_RELAY_PORT} '
              f'playing {n_eps} recorded episodes ({rec}; publish_training.py --dry-print of runs/steer_v1, '
              f'TEST-labelled, counted only because of --accept-test-streams), DRY on the REAL chain ===', flush=True)
        srv = fake_relay(FAKE_RELAY_PORT, items, a.pace)
        # a fresh journal per run: the recording's attempt keys are the same every time, so an old journal would
        # (correctly) treat them as already counted
        dB = os.path.join(DEMO_DIR, 'run_' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))
        os.makedirs(dB, exist_ok=True)
        pB = os.path.join(dB, 'journal.jsonl')
        startB = os.path.getsize(pB) if os.path.exists(pB) else 0
        try:
            bb.main(['--relay', f'ws://127.0.0.1:{FAKE_RELAY_PORT}/live', '--accept-test-streams',
                     '--journal-dir', dB, '--duration', str(a.demo_seconds), '--status-port',
                     str(REAL_STATUS_PORT), '--window-s', '30', '--hourly-budget-eth', 'unset'])
        finally:
            srv.shutdown()
        linesB = journal_tail(pB, startB)
        for l in linesB:
            print(l)
        recs = [json.loads(l) for l in linesB]
        want = sum(json.loads(p)['hits'] for k, p, _ in items if k == 'text' and '"episode"' in p)
        got = sum(r['hits'] for r in recs if r['ev'] == 'hit')
        print(f'\nhits in the recording: {want}; hits counted (after a relay drop and resent episodes): {got}')
        ok &= got == want
        # 30 s windows stand in for the hour here: each closed window with hits books a PREVIEW buy from the buyback
        # wallet, simulated on the real chain (no claim: DRY buys are the hand-funded buyback wallet's)
        ok &= any(r['ev'] == 'buy' and r['preview'] and r['buyer'] == bb.BUYBACK_WALLET for r in recs)
        ok &= not any(r['ev'] == 'claim' for r in recs)
        wins = [r for r in recs if r['ev'] == 'window']
        print('windows: ' + '; '.join(f"{w['start'][11:]} {w['hits']}/{w['misses']}/{w['wrong']} rate {w['hit_rate']} "
                                      f"-> {bb.eth_str(w['amount_wei'])} ETH" for w in wins if w['attempts']))
        with _real_open(os.path.join(dB, 'status.json'), encoding='utf-8') as f:
            st = json.load(f)
        print(f"status.json: hits {st['hits']}, source {st['source']}, next buy {st['next_buy_in_s']} "
              f"({st['next_buy_note']}), stopped {st['stopped']}")
        ok &= st['source']['test'] is True and st['hits']['counted'] == got     # a test stream is marked as one
    sends = [m for m in calls if m not in bb.READ_METHODS]
    print(f'\nRPC methods used: {sorted(set(calls))}; non-read methods: {sends}; .env opened: {TRAPS["env_opens"]}; '
          f'env_read: {bb.STATE["env_read"]}; real signs: {TRAPS["signs"]}')
    ok &= not sends and not TRAPS['env_opens'] and not bb.STATE['env_read'] and not TRAPS['signs']
    print('REAL RUN', 'OK' if ok else 'FAILED')
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--real-run', action='store_true')
    ap.add_argument('--real-seconds', type=float, default=60)
    ap.add_argument('--demo-seconds', type=float, default=75)
    ap.add_argument('--pace', type=float, default=3.0, help='seconds between recorded episodes on the local relay')
    ap.add_argument('--recorded', help='a publish_training --dry-print recording (default runs/buyback_demo/)')
    ap.add_argument('--record', type=float, help='record this many seconds of episodes and exit')
    ap.add_argument('--only-real', action='store_true')
    a, rest = ap.parse_known_args()
    if a.record:
        print(record(a.record))
        return 0
    rc = 0
    if not a.only_real:
        install_traps(network=False)
        prog = unittest.main(argv=[sys.argv[0]] + rest, exit=False, verbosity=2)
        rc = 0 if prog.result.wasSuccessful() else 1
        print(f"traps: .env opened {TRAPS['env_opens']}, real signs {TRAPS['signs']}, blocked network {TRAPS['net']}, "
              f"launcher.rpc calls {TRAPS['launcher_rpc']}")
        if TRAPS['env_opens'] or TRAPS['signs'] or TRAPS['launcher_rpc']:
            rc = 1
    if a.real_run or a.only_real:
        remove_network_sandbox()
        rc |= real_run(a)
    return rc


if __name__ == '__main__':
    sys.exit(main())
