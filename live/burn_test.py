"""Tests of live/burn.py (the $LABRAT burns sized by the Rat Maze escape rate). All mocked.

    python live/burn_test.py                 # no network except 127.0.0.1, no .env, no key, nothing sent

NOTHING here reads .env, holds a key or sends a transaction:
  * .env is trapped: opening any file named .env (builtins.open, io.open, os.open) raises and is recorded;
    launcher.read_env_file and launcher.config are tripwires
  * eth_account's LocalAccount.sign_transaction is a tripwire: no account ever signs; the "rig's" burns are put on the
    fake chain directly as receipts (FakeToken.mine_burn), never through an RPC send
  * the network is sandboxed: requests.post / Session.request raise, sockets may only connect to 127.0.0.1 (the mock
    relay on port 4940, the status server on 4941), launcher.rpc is a tripwire, every chain call goes to FakeToken,
    which has no eth_sendRawTransaction at all
  * every test asserts that the engine used read methods only

What is covered: the arithmetic (floor(burn% x balance x escape rate), whole LABRAT, the 5 % ceiling in code, a lower
--burn-pct, the optional absolute cap, the minimum), the calldata (burn(uint256), transfer(dEaD)) and its decoding, the
counting (the maze channel only: untagged and pons text ignored; courses validated and de-duplicated across a
reconnect, a resync and a restart; TEST streams; maze_end and frames feeding nothing that moves money), the hour
(windows aligned to the UTC hour, the balance read when the burn is sized, no courses / no escapes / an empty wallet /
under the minimum, a restart mid-hour, an hour closed late, a burn that expires when the next hour closes first, the
retry after a failed chain read), the method following the bytecode (burn(), else the dead address; no code stops),
the gas price cap and the three-failures stop, the public status (fields, "Simulated" labels, no address anywhere),
main() end to end in DRY over a real websocket, and LIVE BOOKINGS: the switch (flag AND environment, every refused
configuration), main() in live bookings, an hour booked and not burned, GET /bookings, a correct burn verified on
chain and counted (idempotent, replayed after a restart; both burn() and the dead address), every wrong report refused
(no receipt, from, to, value, reverted, another amount, not a burn, a Transfer to a third address, another amount in
the event, another token's event, the report's amount, bad fields, no booking, a hash counted for another window,
mined before the booking, an unreadable chain), expiry and a late execution, a stop cleared only by its id, and DRY
unchanged.
"""
import builtins
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

LIVE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(LIVE_DIR)
sys.path.insert(0, ROOT)
sys.path.insert(0, LIVE_DIR)

import launcher  # noqa: E402
import buyback as bb  # noqa: E402
import burn  # noqa: E402
from eth_utils import keccak, to_checksum_address  # noqa: E402

PORT_RANGE = range(4930, 4960)   # the local ports this project may use; other builders' tests share them


def free_port(preferred, taken=()):
    """preferred when it can be bound now, else the first free port of PORT_RANGE (a parallel session may hold one)."""
    for p in [preferred] + [q for q in PORT_RANGE if q != preferred]:
        if p in taken:
            continue
        s = socket.socket()
        try:
            s.bind(('127.0.0.1', p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError(f'no free port in {PORT_RANGE}')


MOCK_RELAY_PORT = int(os.environ.get('BURN_TEST_RELAY_PORT') or free_port(4952))
STATUS_PORT = int(os.environ.get('BURN_TEST_STATUS_PORT') or free_port(4953, taken=(MOCK_RELAY_PORT,)))
WALLET, TOKEN, DEAD, ZERO = burn.WALLET, burn.TOKEN, burn.DEAD, burn.ZERO
T = 10 ** 18                     # one LABRAT

# ---------------------------------------------------------------------------------------------------- sandbox
TRAPS = {'env_opens': [], 'signs': [], 'net': [], 'launcher_rpc': 0}
_real_open, _real_io_open, _real_os_open = builtins.open, io.open, os.open
_real_connect = socket.socket.connect


def _is_env(path):
    try:
        return os.path.basename(os.fspath(path)) == '.env'
    except TypeError:
        return False


def _trap_open(file, *a, **k):
    if _is_env(file):
        TRAPS['env_opens'].append(str(file))
        raise PermissionError('burn_test: .env must never be opened')
    return _real_open(file, *a, **k)


def _trap_os_open(path, *a, **k):
    if _is_env(path):
        TRAPS['env_opens'].append(str(path))
        raise PermissionError('burn_test: .env must never be opened')
    return _real_os_open(path, *a, **k)


def _tripwire(name):
    def f(*a, **k):
        TRAPS['signs' if 'sign' in name else 'net'].append(name)
        raise AssertionError(f'burn_test: {name} must never be called')
    return f


def install_traps():
    builtins.open = _trap_open
    io.open = _trap_open
    os.open = _trap_os_open
    launcher.read_env_file = _tripwire('launcher.read_env_file')
    launcher.config = _tripwire('launcher.config')
    from eth_account.signers.local import LocalAccount
    LocalAccount.sign_transaction = _tripwire('LocalAccount.sign_transaction')
    LocalAccount.unsafe_sign_hash = _tripwire('LocalAccount.unsafe_sign_hash')
    import requests
    requests.post = _tripwire('requests.post')
    requests.Session.request = _tripwire('requests.Session.request')

    def rpc_trip(*a, **k):
        TRAPS['launcher_rpc'] += 1
        raise AssertionError('burn_test: launcher.rpc (the real chain) must not be used by mocked tests')
    launcher.rpc = rpc_trip

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ('127.0.0.1', 'localhost', '::1'):
            TRAPS['net'].append(str(address))
            raise ConnectionRefusedError(f'burn_test: no network ({address})')
        return _real_connect(self, address)
    socket.socket.connect = guarded_connect


# ---------------------------------------------------------------------------------------------------- fakes
def W(*ints):
    return '0x' + ''.join(hex(int(i))[2:].rjust(64, '0') for i in ints)


def rev(data, msg='execution reverted'):
    return {'code': 3, 'message': msg, 'data': data}


class FakeToken:
    """The $LABRAT token as far as burn.py reads it, in memory. Callable as a launcher.rpc-style transport. It has no
    eth_sendRawTransaction: the engine never sends, and a 'rig' burn is put on chain by mine_burn() directly."""

    def __init__(self):
        self.chain_id = burn.CHAIN_ID
        self.block, self.ts = 72_890_000, int(time.time())
        self.gas_price = 31_000_000
        self.balances = {WALLET: 15_794_438 * T}
        self.supply = 10 ** 27
        self.eth = {WALLET: 107 * 10 ** 15}
        self.code = ('0x6080604052' + ''.join(burn.SEL[k][2:] for k in ('burn', 'burn_from', 'transfer', 'balance_of',
                                                                      'total_supply')) + '00' * 16)
        self.log = []                                          # (method, to, selector)
        self.froms = []                                        # (to, selector, from) of eth_calls
        self.receipts, self.txs = {}, {}
        self.burn_error = None                                 # a JSON-RPC error every burn simulation gets
        self.fail_reads = set()                                # methods that raise (network trouble)
        self.nonce = 9

    def __call__(self, method, params, all_rpcs_on_error=False):
        to = params[0].get('to') if params and isinstance(params[0], dict) else None
        data = params[0].get('data', '') if params and isinstance(params[0], dict) else ''
        self.log.append((method, to and to_checksum_address(to), data[:10]))
        if method in self.fail_reads:
            raise RuntimeError(f'fake chain: {method} unreachable')
        if method == 'eth_chainId':
            return hex(self.chain_id), None
        if method == 'eth_getBlockByNumber':
            return {'number': hex(self.block), 'timestamp': hex(self.ts)}, None
        if method == 'eth_gasPrice':
            return hex(self.gas_price), None
        if method == 'eth_getBalance':
            return hex(self.eth.get(to_checksum_address(params[0]), 0)), None
        if method == 'eth_getCode':
            return (self.code if to_checksum_address(params[0]) == TOKEN else '0x'), None
        if method == 'eth_call':
            return self._call(params[0])
        if method == 'eth_estimateGas':
            res, err = self._call(params[0], log=False)
            if err:
                return None, err
            sel = params[0]['data'][:10]
            return hex(34_033 if sel == burn.SEL['burn'] else 51_633 if sel == burn.SEL['transfer'] else 30_000), None
        if method == 'eth_getTransactionReceipt':
            return self.receipts.get(params[0]), None
        if method == 'eth_getTransactionByHash':
            return self.txs.get(params[0]), None
        if method == 'eth_getTransactionCount':
            return hex(self.nonce), None
        return None, {'code': -32601, 'message': f'fake chain: {method} not supported'}

    def _call(self, c, log=True):
        to, data, sel = to_checksum_address(c['to']), c['data'], c['data'][:10]
        frm = to_checksum_address(c['from']) if c.get('from') else None
        if log:
            self.froms.append((to, sel, frm))
        if to != TOKEN:
            return None, {'code': -32000, 'message': f'fake chain: unknown contract {to}'}
        if sel == burn.SEL['balance_of']:
            return W(self.balances.get(to_checksum_address('0x' + data[-40:]), 0)), None
        if sel == burn.SEL['total_supply']:
            return W(self.supply), None
        if sel in (burn.SEL['burn'], burn.SEL['transfer']):
            if self.burn_error is not None:
                return None, self.burn_error
            if sel == burn.SEL['burn']:
                amount = int(data[10:], 16)
            else:
                recipient = to_checksum_address('0x' + data[34:74])
                amount = int(data[74:], 16)
                if recipient == ZERO:
                    return None, rev('0xec442f05' + '0' * 64)         # ERC20InvalidReceiver(address(0)), as on chain
            if self.balances.get(frm, 0) < amount:
                return None, rev('0xe450d38c' + W(0, amount)[2:])   # ERC20InsufficientBalance
            return ('0x' if sel == burn.SEL['burn'] else W(1)), None
        return None, {'code': -32000, 'message': f'fake chain: unknown call {sel}'}

    def mine_burn(self, amount, frm=None, to=None, value=0, method='burn', status=1, data=None, log_from=None,
                  log_to=None, log_amount=None, log_token=None):
        """What the buy rig puts on chain for a booking: the wallet's burn(amount) (or its transfer to the dead
        address), mined. -> its hash. The keyword arguments make the WRONG transactions the tests refuse."""
        frm = to_checksum_address(frm or WALLET)
        to = to_checksum_address(to or TOKEN)
        data = data or burn.burn_calldata(method, amount)
        self.nonce += 1
        self.block += 1
        txh = '0x' + keccak(f'{frm}|{to}|{data}|{value}|{self.nonce}|{self.block}'.encode()).hex()
        sink = ZERO if method == 'burn' else DEAD
        logs = []
        if status == 1:
            la = amount if log_amount is None else log_amount
            logs.append({'address': (log_token or TOKEN).lower(),
                         'topics': [burn.TRANSFER_TOPIC, '0x' + bb.pad_addr(log_from or frm), '0x' + bb.pad_addr(log_to or sink)],
                         'data': W(la)})
            if to == TOKEN and frm in self.balances:
                self.balances[frm] -= min(la, self.balances[frm])
                if method == 'burn':
                    self.supply -= la
                else:
                    self.balances[DEAD] = self.balances.get(DEAD, 0) + la
        gas_used = 34_000 if method == 'burn' else 51_600
        self.receipts[txh] = {'transactionHash': txh, 'status': hex(status), 'gasUsed': hex(gas_used),
                              'effectiveGasPrice': hex(self.gas_price), 'blockNumber': hex(self.block), 'logs': logs,
                              'from': frm.lower(), 'to': to.lower()}
        self.txs[txh] = {'hash': txh, 'from': frm.lower(), 'to': to.lower(), 'value': hex(int(value)), 'input': data,
                         'nonce': hex(self.nonce), 'blockNumber': hex(self.block)}
        return txh

    def methods(self):
        return {m for m, _t, _s in self.log}


HOUR = 3600


class Clock:
    """A frozen clock, one minute into the current UTC hour: a test crosses an hour boundary only where it says so."""

    def __init__(self, t=None):
        self.t = float(t if t is not None else bb.window_start(time.time()) + 60)

    def __call__(self):
        return self.t


MAZE_HELLO = {'type': 'hello', 'source': 'training', 'task': 'maze', 'run': 'maze_live', 'channel': 'maze',
              'label': 'Training run maze_live: the latest saved checkpoint, playing in its own simulation', 'fps': 25,
              'started': '2026-09-26T10:00:00Z'}
TILES_HELLO = {**MAZE_HELLO, 'task': 'tiles', 'run': 'tiles_live'}
del TILES_HELLO['channel']


def ep(n, escapes, timeouts=0, fell=False, channel='maze', **over):
    m = {'type': 'episode', 'n': n, 'presses': escapes + timeouts, 'hits': escapes, 'misses': timeouts, 'fell': fell}
    if channel is not None:
        m['channel'] = channel
    m.update(over)
    return json.dumps(m)


def state(live=True, hello=MAZE_HELLO, episode=None, channel='maze'):
    m = {'type': 'state', 'live': live, 'hello': hello, 'checkpoint': None, 'episode': episode, 'history': []}
    if channel is not None:
        m['channel'] = channel
    return json.dumps(m)


def maze_end(result, maze_id=1, channel='maze'):
    m = {'type': 'maze_end', 'maze_id': maze_id, 'result': result, 'steps': 120, 'bumps': 2, 'time_s': 4.2}
    if channel is not None:
        m['channel'] = channel
    return json.dumps(m)


def maze_snap(maze_id=1, w=5, h=5, channel='maze'):
    return json.dumps({'type': 'maze', 'channel': channel, 't': 1.0, 'maze_id': maze_id, 'w': w, 'h': h, 'walls': None,
                       'cell': [0, 0], 'pos': [0.5, 0.5], 'cheese': [4, 4], 'trail': [], 'bumps': 0, 'steps': 3, 'dist': 8})


def frame(prefix=b''):
    import struct
    return prefix + struct.pack('<12f', 7.0, 1.0, 0.0, 0.3, 1.0, 0.5, 0.5, 0.6, 0.6, 0.1, 0.1, 0.0) + bytes(4 * 65 * 7)


def cfg(**kw):
    return burn.Config(**kw).validate()


def make_engine(tmp, chain, config=None, clock=None, accept_test=False):
    config = config or cfg()
    clock = clock or Clock()
    journal = burn.Journal(os.path.join(tmp, 'journal.jsonl'), clock=clock)
    rpc = burn.ReadRpc(chain)
    sim = burn.Sim(rpc, config, clock=clock)
    return burn.Engine(config, 'DRY', journal, rpc, burn.DryExecutor(sim, config), sim, accept_test=accept_test,
                       clock=clock)


def bookings_engine(tmp, chain, clock, c=None):
    """The engine in LIVE BOOKINGS, as main() builds it: mode LIVE, BookingExecutor, a read-only RPC."""
    c = c or cfg()
    journal = burn.Journal(os.path.join(tmp, burn.BOOKINGS_JOURNAL), clock=clock)
    rpc = burn.ReadRpc(chain)
    sim = burn.Sim(rpc, c, clock=clock)
    return burn.Engine(c, 'LIVE', journal, rpc, burn.BookingExecutor(sim, c), sim, clock=clock, live_bookings=True)


def feed(engine, courses, start_n=0, hello=MAZE_HELLO):
    """courses: [(escapes, timeouts), ...] (escapes <= 4 each); returns the next n."""
    engine.on_relay_text(json.dumps(hello))
    n = start_n
    for e, t in courses:
        engine.on_relay_text(ep(n, e, t))
        n += 1
    return n


def records(engine, ev=None):
    return [r for r in engine.journal.records() if ev is None or r['ev'] == ev]


def next_hour(clock, extra=1):
    clock.t = bb.window_start(clock.t) + HOUR + extra
    return clock.t


def close_hour(engine, clock, extra=1):
    """The hour ends: the next tick closes it and sizes its burn."""
    next_hour(clock, extra)
    engine.tick()


def report(e, window_t, tx, amount=None, **extra):
    b = e.ledger.bookings[window_t]
    body = {'window': bb.iso(window_t), 'tx': tx, **extra}
    if amount is not False:
        body['amount'] = amount or bb.token_str(b['amount_wei'], 18)
    return e.add_report(body)


def expected_burn(balance, escapes, timeouts, bps=500):
    return balance * bps * escapes // (10_000 * (escapes + timeouts)) // T * T


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='burn_test_')
        self.chain = FakeToken()
        self.env_before = list(TRAPS['env_opens'])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertEqual(TRAPS['env_opens'], self.env_before, '.env was opened')
        self.assertEqual(TRAPS['signs'], [], 'an account signed')
        self.assertFalse(self.chain.methods() - burn.READ_METHODS, f'the engine used {self.chain.methods() - burn.READ_METHODS}')

    def check_invariants(self, engine):
        for r in records(engine, 'burn') + records(engine, 'booking'):
            self.assertLessEqual(r['amount_wei'], r['balance_wei'] * burn.BURN_BPS_HARD // 10_000, 'over 5 % of the balance')
            self.assertEqual(r['amount_wei'] % T, 0, 'not a whole LABRAT')
            self.assertGreaterEqual(r['amount_wei'], engine.cfg.min_burn_wei)
        wins = [r['window'] for r in records(engine, 'burn') + records(engine, 'booking')]
        self.assertEqual(len(wins), len(set(wins)), 'an hour burned twice')

    def no_address(self, st):
        s = json.dumps(st)
        for a in (WALLET, TOKEN, bb.WALLET, bb.ROUTER, DEAD):
            self.assertNotIn(a[2:].lower(), s.lower(), f'the status holds {a}')
        return s


# ---------------------------------------------------------------------------------------------------- tests
class TestArithmeticAndCalldata(unittest.TestCase):
    def test_selectors_and_pins(self):
        self.assertEqual(burn.SEL['burn'], '0x42966c68')
        self.assertEqual(burn.SEL['transfer'], '0xa9059cbb')
        self.assertEqual(burn.SEL['balance_of'], '0x70a08231')
        self.assertEqual(burn.SEL['total_supply'], '0x18160ddd')
        self.assertEqual(burn.DEAD, '0x000000000000000000000000000000000000dEaD')
        self.assertEqual(burn.TRANSFER_TOPIC, bb.TRANSFER_TOPIC)
        self.assertEqual((burn.WALLET, burn.TOKEN), (bb.BUYBACK_WALLET, bb.TOKEN))
        self.assertEqual(burn.BURN_BPS_HARD, 500)
        self.assertEqual(burn.MAZE_MAX_HITS, 4)

    def test_burn_amount(self):
        b = 1_000_000 * T
        self.assertEqual(burn.burn_amount(b, 500, 1, 0), 50_000 * T, '5 % at a perfect hour')
        self.assertEqual(burn.burn_amount(b, 500, 3, 1), 37_500 * T, '5 % x 0.75')
        self.assertEqual(burn.burn_amount(b, 200, 1, 0), 20_000 * T, 'a lower percentage')
        self.assertEqual(burn.burn_amount(100 * T, 500, 2, 1), 3 * T, 'rounded DOWN to a whole LABRAT (3.33)')
        self.assertEqual(burn.burn_amount(T + 1, 500, 1, 0), 0, '0.05 LABRAT rounds to nothing')
        self.assertEqual(burn.burn_amount(b, 900, 1, 0), 50_000 * T, 'never above the 5 % ceiling, whatever bps says')
        self.assertEqual(burn.burn_amount(b, 10_000, 1, 0), 50_000 * T)
        self.assertEqual(burn.burn_amount(b, 500, 1, 0, max_burn_wei=1000 * T), 1000 * T, 'the optional absolute cap')
        self.assertEqual(burn.burn_amount(b, 500, 0, 3), 0, 'no escapes')
        self.assertEqual(burn.burn_amount(b, 500, 0, 0), 0, 'no mazes')
        self.assertEqual(burn.burn_amount(0, 500, 4, 0), 0, 'an empty wallet')
        # the real wallet on 2026-09-26: 15,794,438.075... LABRAT at 15/16 escaped
        real = 15_794_438_075_429_223 * 10 ** 9
        self.assertEqual(burn.burn_amount(real, 500, 15, 1), 740_364 * T)
        self.assertEqual(burn.share_bps(50_000 * T, b), 500)
        self.assertIsNone(burn.share_bps(1, 0))
        self.assertIsNone(burn.escape_rate(0, 0))
        self.assertEqual(burn.escape_rate(3, 1), 0.75)

    def test_percent_and_config(self):
        self.assertEqual((burn.parse_pct('5'), burn.parse_pct('2.5'), burn.parse_pct('0.25')), (500, 250, 25))
        for bad in ('0', '-1', '5.001', 'x', ''):
            with self.assertRaises(ValueError, msg=bad):
                burn.parse_pct(bad)
        self.assertEqual((burn.pct_str(500), burn.pct_str(250), burn.pct_str(25)), ('5', '2.5', '0.25'))
        self.assertEqual(cfg().burn_bps, 500)
        with self.assertRaises(ValueError):
            cfg(burn_bps=501)
        with self.assertRaises(ValueError):
            cfg(burn_bps=0)
        with self.assertRaises(ValueError):
            cfg(window_s=7)
        with self.assertRaises(ValueError):
            cfg(channel='Maze!')
        with self.assertRaises(ValueError):
            cfg(method='zero')
        with self.assertRaises(ValueError):
            cfg(max_burn_wei=T // 2)                        # under the minimum burn
        with self.assertRaises(ValueError):
            cfg(max_gas_price_wei=11 * 10 ** 9)
        c = burn.build_config(burn.parse_args(['--burn-pct', '2', '--max-burn-labrat', '1000', '--min-burn-labrat', '5']))
        self.assertEqual((c.burn_bps, c.max_burn_wei, c.min_burn_wei, c.channel, c.method), (200, 1000 * T, 5 * T, 'maze', 'auto'))
        self.assertIsNone(burn.build_config(burn.parse_args(['--max-burn-labrat', 'unset'])).max_burn_wei)
        with self.assertRaises(SystemExit):
            burn.main(['--burn-pct', '6'])                  # over the ceiling: refused before anything runs
        with self.assertRaises(SystemExit):
            burn.main(['--burn-pct', '5.5'])
        self.assertEqual(burn.build_config(burn.parse_args(['--channel', 'training'])).channel, 'training')

    def test_calldata_roundtrip(self):
        d = burn.cd_burn(5 * T)
        self.assertEqual(d, '0x42966c68' + hex(5 * T)[2:].rjust(64, '0'))
        self.assertEqual(burn.decode_burn(d), ('burn', 5 * T))
        self.assertEqual(burn.decode_burn(burn.cd_transfer(DEAD, 7)), ('dead', 7))
        self.assertEqual(burn.burn_calldata('dead', 7), burn.cd_transfer(DEAD, 7))
        for bad in (burn.cd_transfer(bb.ROUTER, 7), '0x', '0x42966c68', d + '00', bb.cd_balance_of(WALLET), None):
            with self.assertRaises(ValueError, msg=bad):
                burn.decode_burn(bad)
        self.assertEqual(burn.check_burn_tx(TOKEN, d, 0, 5 * T), 'burn')
        self.assertEqual(burn.check_burn_tx(TOKEN, burn.cd_transfer(DEAD, 5 * T), 0, 5 * T), 'dead')
        with self.assertRaises(ValueError):
            burn.check_burn_tx(bb.ROUTER, d, 0, 5 * T)
        with self.assertRaises(ValueError):
            burn.check_burn_tx(TOKEN, d, 1, 5 * T)
        with self.assertRaises(ValueError):
            burn.check_burn_tx(TOKEN, d, 0, 4 * T)
        with self.assertRaises(ValueError):
            burn.burn_calldata('zero', 1)

    def test_the_hour_arithmetic(self):
        self.assertEqual(bb.iso(bb.window_start(1_790_003_599))[14:], '00:00Z')
        self.assertEqual(burn.HOUR_S, 3600)
        self.assertEqual(burn.SIGN_UNTIL_S, 2 * 3600)
        self.assertGreater(burn.BOOKING_DEAD_S, burn.SIGN_UNTIL_S)


class TestMazeCounter(unittest.TestCase):
    def test_only_the_maze_channel_counts(self):
        c = burn.MazeCounter()
        untagged = {k: v for k, v in MAZE_HELLO.items() if k != 'channel'}
        c.on_text(json.dumps(untagged))                                   # the default (Rat Tiles) channel
        self.assertEqual(c.on_text(ep(0, 4, channel=None)), [], 'an untagged episode is not this channel')
        self.assertEqual(c.stats['other_channel'], 2)
        self.assertEqual(c.on_text(json.dumps({'type': 'pons_step', 'channel': 'pons', 'step': 3})), [])
        out = c.on_text(json.dumps(MAZE_HELLO))
        self.assertEqual((out[0]['ev'], out[0]['task'], out[0]['countable']), ('session', 'maze', True))
        eps = [e for e in c.on_text(ep(0, 3, 1)) if e['ev'] == 'episode']
        self.assertEqual([(e['w_hits'], e['w_misses'], e['n']) for e in eps], [(3, 1, 0)])
        self.assertEqual(c.on_text(ep(1, 4, channel='pons')), [], 'the pons channel')
        self.assertEqual(c.on_text(ep(1, 4, channel=None)), [], 'untagged')
        self.assertEqual([e['w_hits'] for e in c.on_text(ep(1, 4)) if e['ev'] == 'episode'], [4])
        # a counter on the default channel (DRY tests only) counts the untagged text and ignores the tagged
        d = burn.MazeCounter(channel='training')
        d.on_text(json.dumps(untagged))
        self.assertEqual([e['w_hits'] for e in d.on_text(ep(0, 2, channel=None)) if e['ev'] == 'episode'], [2])
        self.assertEqual(d.on_text(ep(1, 2)), [])

    def test_courses_dedup_and_validation(self):
        c = burn.MazeCounter()
        out = c.on_text(state(live=False, episode=json.loads(ep(15, 3))))
        self.assertEqual([e['ev'] for e in out], ['session'])
        self.assertEqual(c.stats['state_episode_not_live'], 1, "a stopped run's last course is never counted")
        out = c.on_text(state(True, episode=json.loads(ep(3, 2, 0))))
        self.assertEqual([(e['n'], e['w_hits'], e['via']) for e in out if e['ev'] == 'episode'], [(3, 2, 'state')])
        self.assertEqual([e['w_hits'] for e in c.on_text(ep(4, 3, 1)) if e['ev'] == 'episode'], [3])
        self.assertEqual(c.on_text(ep(4, 3, 1)), [], 'the same course twice')
        self.assertEqual([e for e in c.on_text(state(True, episode=json.loads(ep(4, 3, 1)))) if e['ev'] == 'episode'], [])
        self.assertEqual(c.on_text(json.dumps(MAZE_HELLO)), [], 'a resync of the same session')
        self.assertEqual(c.on_text(ep(4, 3, 1)), [])
        out = c.on_text(ep(7, 1))
        self.assertEqual([e['ev'] for e in out], ['gap', 'episode'])
        self.assertEqual(out[0]['missed'], 2)
        for bad in (ep(8, 5), ep(9, 2, 1, presses=4), json.dumps({'type': 'episode', 'channel': 'maze', 'n': 10, 'presses': 1,
                                                                  'hits': True, 'misses': 0})):
            self.assertEqual([e['ev'] for e in c.on_text(bad)], ['rejected'], bad)
        self.assertEqual(c.on_text(ep(8, 4)), [], 'a rejected course number is spent: never counted later either')
        c.on_text(json.dumps({'type': 'bye', 'channel': 'maze'}))
        self.assertEqual(c.on_text(ep(9, 4)), [], 'nothing after bye until a new hello')
        # a TEST stream never counts by default; a tiles run on the maze channel is not this task
        t = burn.MazeCounter()
        t.on_text(json.dumps(dict(MAZE_HELLO, label='TEST (not live training): x', test=True)))
        self.assertEqual(t.on_text(ep(0, 4)), [])
        t2 = burn.MazeCounter(accept_test=True)
        t2.on_text(json.dumps(dict(MAZE_HELLO, label='TEST x', test=True)))
        self.assertEqual([e['w_hits'] for e in t2.on_text(ep(0, 4)) if e['ev'] == 'episode'], [4])
        t3 = burn.MazeCounter()
        out = t3.on_text(json.dumps(dict(TILES_HELLO, channel='maze')))
        self.assertIn('tiles is not counted', out[0]['why'])
        self.assertEqual(t3.on_text(ep(0, 4)), [])
        self.assertEqual(c.on_text('not json'), [])
        self.assertEqual(c.on_text('x' * 80_000), [])
        self.assertEqual(c.on_text('[1,2]'), [])

    def test_maze_end_and_frames_feed_only_the_unconfirmed_tally(self):
        c = burn.MazeCounter()
        c.on_text(maze_end('escaped'))
        self.assertEqual(c.course['escapes'], 0, 'nothing before a live hello')
        c.on_text(json.dumps(MAZE_HELLO))
        c.on_text(maze_snap(7, 6, 6))
        c.on_text(maze_end('escaped', 7))
        c.on_text(maze_end('escaped', 8))
        c.on_text(maze_end('timeout', 9))
        c.on_text(maze_end('nonsense', 10))
        self.assertEqual((c.course['escapes'], c.course['timeouts'], c.course['maze_id'], c.course['w']), (2, 1, 7, 6))
        c.on_bytes(frame(b'MZ'))
        c.on_bytes(frame())
        self.assertEqual(c.stats['frames'], 2)
        self.assertEqual((c.course['escapes'], c.course['timeouts']), (2, 1), 'frames change nothing')
        self.assertEqual([e['w_hits'] for e in c.on_text(ep(0, 2, 1)) if e['ev'] == 'episode'], [2])
        self.assertEqual((c.course['escapes'], c.course['timeouts'], c.course['maze_id']), (0, 0, None), 'the course ended')
        self.assertEqual(c.on_text(maze_end('escaped', 11, channel=None)), [])
        self.assertEqual(c.course['escapes'], 0, 'an untagged maze_end is not this channel')
        c.on_text(json.dumps({'type': 'idle', 'channel': 'maze', 'reason': 'quiet'}))
        c.on_text(maze_end('escaped', 12))
        self.assertEqual(c.course['escapes'], 0, 'not while the stream is not live')


class TestEngineDry(Base):
    def test_hourly_burn_is_pct_times_balance_times_escape_rate(self):
        clock = Clock()
        e = make_engine(self.tmp, self.chain, clock=clock)
        hour0 = bb.window_start(clock.t)
        st = e.public_status()
        self.assertEqual((st['mode'], st['label'], st['simulated']), ('DRY', burn.DRY_LABEL, True))
        self.assertIsNone(st['this_hour']['wallet_balance'], 'nothing read yet')
        feed(e, [(4, 0), (4, 0), (4, 0), (3, 1)])            # 15 escapes, 1 timeout: escape rate 0.9375
        e.tick()                                             # waiting for the hour: reads the balance for the status
        self.assertEqual(records(e, 'burn'), [], 'nothing is burned before the hour ends')
        st = e.public_status()
        bal = self.chain.balances[WALLET]
        self.assertEqual(st['this_hour'], {'start': bb.iso(hour0), 'end': bb.iso(hour0 + HOUR), 'escapes': 15, 'timeouts': 1,
                                           'attempts': 4, 'escape_rate': 0.9375, 'wallet_balance': bb.token_str(bal),
                                           'wallet_balance_at': bb.iso(clock.t),
                                           'projected_burn': bb.token_str(expected_burn(bal, 15, 1), 18),
                                           'projected_pct': 4.68})           # 4.6875, floored in basis points
        self.assertEqual((st['next_burn_note'], st['next_burn_at'], st['next_burn_in_s']),
                         ('waiting for the hour', bb.iso(hour0 + HOUR), HOUR - 60))
        self.assertIsNone(st['last_hour'])
        self.assertEqual(st['this_course']['note'], 'unconfirmed until the course ends')
        self.assertEqual((st['burn_pct'], st['burn_pct_max'], st['task'], st['channel']), ('5', '5', 'maze', 'maze'))
        close_hour(e, clock)
        win, = records(e, 'window')
        self.assertEqual({k: win[k] for k in ('start', 'escapes', 'timeouts', 'attempts', 'escape_rate', 'pending', 'late',
                                              'note', 'burn_bps')},
                         {'start': bb.iso(hour0), 'escapes': 15, 'timeouts': 1, 'attempts': 4, 'escape_rate': 0.9375,
                          'pending': True, 'late': False, 'note': None, 'burn_bps': 500})
        b, = records(e, 'burn')
        want = expected_burn(bal, 15, 1)
        self.assertEqual((b['amount_wei'], b['window'], b['escapes'], b['timeouts'], b['escape_rate'], b['method']),
                         (want, hour0, 15, 1, 0.9375, 'burn'))
        self.assertEqual((b['balance_wei'], b['simulated'], b['share_bps'], b['eth_covers_gas'], b['gas']),
                         (bal, True, 468, True, 34_033))
        self.assertEqual(b['total_supply_wei'], self.chain.supply)
        self.assertEqual(want % T, 0)
        self.assertLessEqual(want, bal * 5 // 100)
        # the simulation: the exact burn(amount) from the wallet, no override, then its gas
        self.assertIn((TOKEN, burn.SEL['burn'], WALLET), self.chain.froms)
        self.assertEqual(b['eth_call']['data_sha256'], bb.sha(burn.cd_burn(want)))
        self.assertEqual(self.chain.balances[WALLET], bal, 'a simulation moves nothing')
        st = e.public_status()
        lh = st['last_hour']
        self.assertEqual((lh['escapes'], lh['timeouts'], lh['escape_rate'], lh['late']), (15, 1, 0.9375, False))
        self.assertEqual((lh['burn']['state'], lh['burn']['label'], lh['burn']['simulated'], lh['burn']['amount'],
                          lh['burn']['tx'], lh['burn']['method']),
                         ('simulated', burn.SIMULATED_LABEL, True, bb.token_str(want, 18), None, 'burn'))
        self.assertEqual(st['totals']['simulated_burns'], 1)
        self.assertEqual(st['totals']['simulated_burned'], bb.token_str(want, 18))
        self.assertEqual((st['totals']['burned'], st['totals']['burns'], st['totals']['booked']), ('0', 0, 0))
        self.assertEqual((st['totals']['escapes'], st['totals']['timeouts'], st['totals']['courses']), (15, 1, 4))
        r, = st['recent']
        self.assertEqual((r['state'], r['simulated'], r['label'], r['amount'], r['amount_wei'], r['window'], r['share_pct']),
                         ('simulated', True, burn.SIMULATED_LABEL, bb.token_str(want, 18), str(want), bb.iso(hour0), 4.68))
        self.assertEqual(st['bookings'], [])
        self.assertEqual(st['method'], 'burn')
        self.assertIn('Every burn shown is simulated', st['rule'])
        self.assertIn('5% of the $LABRAT', st['rule'])
        s = self.no_address(st)
        self.assertNotRegex(s, r'0x[0-9a-fA-F]{3,}', 'no hex strings at all in a DRY status')
        self.assertEqual(st['next_burn_note'], 'waiting for the hour')
        self.assertTrue(all(r['mode'] == 'DRY' for r in records(e)))
        self.check_invariants(e)
        # the next hour: the same course numbers of a new session count again, and a restart keeps the totals
        feed(e, [(4, 0)], hello=dict(MAZE_HELLO, started='2026-09-26T12:00:00Z'))
        close_hour(e, clock)
        self.assertEqual(len(records(e, 'burn')), 2)
        e2 = make_engine(self.tmp, self.chain, clock=clock)
        st2 = e2.public_status()
        self.assertEqual((st2['totals']['simulated_burns'], st2['totals']['escapes'], len(st2['recent'])), (2, 19, 2))
        self.assertEqual(st2['recent'][0]['window'], bb.iso(hour0 + HOUR))
        self.assertEqual(st2['last_hour']['burn']['state'], 'simulated')

    def test_the_ceiling_and_a_lower_pct(self):
        clock = Clock()
        e = make_engine(self.tmp, self.chain, cfg(burn_bps=200), clock)
        feed(e, [(4, 0)] * 2)                               # a perfect hour
        close_hour(e, clock)
        b, = records(e, 'burn')
        bal = self.chain.balances[WALLET]
        self.assertEqual(b['amount_wei'], bal * 2 // 100 // T * T)
        self.assertEqual(e.public_status()['burn_pct'], '2')
        self.assertIn('2% of the $LABRAT', e.public_status()['rule'])
        # 5 % is a ceiling in code: a perfect hour at the default burns exactly floor(5 %) whole LABRAT
        e5 = make_engine(os.path.join(self.tmp, 'five'), self.chain, clock=Clock(clock.t))
        c5 = Clock(clock.t)
        e5.clock = c5
        feed(e5, [(4, 0)] * 2)
        close_hour(e5, c5)
        b5, = records(e5, 'burn')
        self.assertEqual(b5['amount_wei'], bal * 5 // 100 // T * T)
        self.assertIn(b5['share_bps'], (499, 500), 'whole-LABRAT rounding can only bring it under 5 %')
        with self.assertRaises(ValueError):
            cfg(burn_bps=1000)
        # the optional absolute cap
        e_cap = make_engine(os.path.join(self.tmp, 'cap'), self.chain, cfg(max_burn_wei=1234 * T), Clock(clock.t))
        ck = e_cap.clock
        feed(e_cap, [(4, 0)])
        close_hour(e_cap, ck)
        self.assertEqual(records(e_cap, 'burn')[0]['amount_wei'], 1234 * T)
        self.assertEqual(e_cap.public_status()['caps']['max_burn'], '1234')
        self.check_invariants(e)
        self.check_invariants(e5)

    def test_no_burn_cases(self):
        clock = Clock()
        e = make_engine(self.tmp, self.chain, clock=clock)
        close_hour(e, clock)                                # no courses at all
        w = records(e, 'window')[-1]
        self.assertEqual((w['pending'], w['note']), (False, 'no mazes in the hour'))
        self.assertEqual(e.public_status()['last_hour']['burn'], {'state': 'none', 'note': 'no mazes in the hour', 'label': None,
                                                                 'simulated': None, 'amount': None, 'amount_wei': None,
                                                                 'tx': None, 'block': None, 'method': None, 'explorer': None})
        feed(e, [(0, 1), (0, 1)])                           # only timeouts
        close_hour(e, clock)
        w = records(e, 'window')[-1]
        self.assertEqual((w['escapes'], w['timeouts'], w['escape_rate'], w['pending'], w['note']), (0, 2, 0.0, False, 'no escapes in the hour'))
        self.assertEqual(records(e, 'burn'), [])
        # an empty wallet
        self.chain.balances[WALLET] = 0
        feed(e, [(4, 0)], start_n=10)
        close_hour(e, clock)
        nb = records(e, 'window_noburn')[-1]
        self.assertEqual(nb['public'], 'nothing to burn')
        st = e.public_status()
        self.assertEqual((st['last_hour']['burn']['state'], st['last_hour']['burn']['note']), ('none', 'nothing to burn'))
        self.assertEqual(st['this_hour']['wallet_balance'], '0')
        # under the minimum: 10 LABRAT x 5 % x 1/4 = 0.125 -> 0
        self.chain.balances[WALLET] = 10 * T
        feed(e, [(1, 3)], start_n=20)
        close_hour(e, clock)
        nb = records(e, 'window_noburn')[-1]
        self.assertEqual(nb['public'], 'under the minimum burn')
        self.assertIn('under 1 LABRAT', nb['why'])
        self.assertEqual(records(e, 'burn'), [])
        self.assertEqual(e.public_status()['next_burn_note'], 'waiting for the hour')

    def test_restart_mid_hour_late_close_and_expiry(self):
        clock = Clock()
        e = make_engine(self.tmp, self.chain, clock=clock)
        feed(e, [(4, 0), (2, 1)])
        e2 = make_engine(self.tmp, self.chain, clock=clock)              # a restart in the same hour
        h = e2.public_status()['this_hour']
        self.assertEqual((h['escapes'], h['timeouts'], h['attempts']), (6, 1, 2))
        e2.on_relay_text(state(True, episode=json.loads(ep(1, 2, 1))))  # the relay repeats the last course
        e2.on_relay_text(ep(0, 4))                                       # and the publisher resyncs
        e2.on_relay_text(ep(2, 4))
        self.assertEqual([r['n'] for r in records(e2, 'course')], [0, 1, 2])
        self.assertEqual(e2.public_status()['this_hour']['escapes'], 10)
        # the engine was down for two hours: the old hour closes late, without a burn
        clock.t = bb.window_start(clock.t) + 2 * HOUR + 5
        e3 = make_engine(self.tmp, self.chain, clock=clock)
        e3.tick()
        w = records(e3, 'window')[-1]
        self.assertEqual((w['escapes'], w['late'], w['pending']), (10, True, False))
        self.assertIn('closed late', w['note'])
        self.assertEqual(records(e3, 'burn'), [])
        # a burn that cannot be sized (the chain unreadable) retries, then expires when the next hour closes first
        feed(e3, [(4, 0)], start_n=50)
        self.chain.fail_reads = {'eth_getCode'}
        close_hour(e3, clock)
        self.assertEqual(records(e3, 'burn'), [])
        st = e3.public_status()
        self.assertEqual((st['last_hour']['burn']['state'], st['next_burn_note']), ('due', 'chain read failed; retrying'))
        self.assertGreater(st['next_burn_in_s'], 0)
        self.assertEqual(records(e3, 'burn_skip')[-1]['why'][:17], 'chain read failed')
        clock.t += 61                                                    # the retry, still unreadable
        e3.tick()
        self.assertEqual(len(records(e3, 'burn_skip')), 2)
        close_hour(e3, clock)                                            # the next hour closes: the due burn expires
        ex, = records(e3, 'burn_expired')
        self.assertEqual(ex['public'], 'the next hour closed first')
        self.assertEqual(records(e3, 'burn'), [])
        self.chain.fail_reads = set()
        # the retry within the hour succeeds
        feed(e3, [(4, 0)], start_n=60)
        self.chain.fail_reads = {'eth_gasPrice'}
        close_hour(e3, clock)
        self.assertEqual(records(e3, 'burn'), [])
        self.chain.fail_reads = set()
        clock.t += 61
        e3.tick()
        b, = records(e3, 'burn')
        self.assertEqual(b['escapes'], 4)
        self.assertEqual(e3.public_status()['last_hour']['burn']['state'], 'simulated')
        self.check_invariants(e3)

    def test_method_follows_the_bytecode(self):
        clock = Clock()
        # no burn(uint256) in the code: the dead address
        self.chain.code = '0x6080' + burn.SEL['transfer'][2:] + burn.SEL['balance_of'][2:] + burn.SEL['total_supply'][2:]
        e = make_engine(self.tmp, self.chain, clock=clock)
        feed(e, [(4, 0)])
        close_hour(e, clock)
        b, = records(e, 'burn')
        self.assertEqual(b['method'], 'dead')
        self.assertIn((TOKEN, burn.SEL['transfer'], WALLET), self.chain.froms)
        self.assertEqual(b['eth_call']['data_sha256'], bb.sha(burn.cd_transfer(DEAD, b['amount_wei'])))
        st = e.public_status()
        self.assertEqual(st['method'], 'dead')
        self.assertIn('dead address', st['rule'])
        self.assertEqual(records(e, 'check')[-1]['has_burn'], False)
        # --method burn on such a token stops (nothing is simulated)
        e_b = make_engine(os.path.join(self.tmp, 'b'), self.chain, cfg(method='burn'), Clock(clock.t))
        feed(e_b, [(4, 0)])
        close_hour(e_b, e_b.clock)
        self.assertTrue(e_b.stopped)
        self.assertIn('no burn(uint256)', e_b.stopped)
        self.assertEqual(records(e_b, 'burn'), [])
        # burn() present: the token's own burn; --method dead still allowed in DRY
        self.chain.code = FakeToken().code
        e_d = make_engine(os.path.join(self.tmp, 'd'), self.chain, cfg(method='dead'), Clock(clock.t))
        feed(e_d, [(4, 0)])
        close_hour(e_d, e_d.clock)
        self.assertEqual(records(e_d, 'burn')[0]['method'], 'dead')
        chk = burn.check_token(burn.ReadRpc(self.chain))
        self.assertEqual((chk['ok'], chk['method'], chk['has_burn']), (True, 'burn', True))
        # no code at all: stop
        self.chain.code = '0x'
        e_n = make_engine(os.path.join(self.tmp, 'n'), self.chain, clock=Clock(clock.t))
        feed(e_n, [(4, 0)])
        close_hour(e_n, e_n.clock)
        self.assertIn('no contract code', e_n.stopped)
        self.assertEqual(e_n.public_status()['stopped'], 'the chain no longer matches the pinned coin')
        self.assertEqual(e_n.public_status()['next_burn_note'], 'stopped')

    def test_gas_price_cap_and_the_failures_stop(self):
        clock = Clock()
        e = make_engine(self.tmp, self.chain, clock=clock)
        feed(e, [(4, 0)])
        self.chain.gas_price = 2 * 10 ** 9
        close_hour(e, clock)
        self.assertEqual(records(e, 'burn'), [])
        st = e.public_status()
        self.assertEqual(st['next_burn_note'], 'gas price over the cap')
        self.assertEqual(st['last_hour']['burn']['state'], 'due')
        self.chain.gas_price = 31_000_000
        clock.t += 301
        e.tick()
        self.assertEqual(len(records(e, 'burn')), 1)
        # a reverting burn: three failed attempts in a row stop the engine
        feed(e, [(4, 0)], start_n=10)
        self.chain.burn_error = rev('0xe450d38c' + W(0, 1)[2:])
        close_hour(e, clock)
        for i in (2, 3):
            self.assertFalse(e.stopped)
            self.assertEqual(e.public_status()['next_burn_note'], 'retrying after a failed attempt')
            clock.t += 121
            e.tick()
        self.assertEqual([r['consecutive'] for r in records(e, 'burn_failed')], [1, 2, 3])
        self.assertIn('3 failed burn attempts', e.stopped)
        self.assertEqual(e.public_status()['stopped'], 'repeated failures')
        self.assertEqual(len(records(e, 'burn')), 1)
        self.chain.burn_error = None
        clock.t += 121
        e.tick()
        self.assertEqual(len(records(e, 'burn')), 1, 'stopped: nothing more')
        self.check_invariants(e)

    def test_status_file_and_test_streams(self):
        clock = Clock()
        e = make_engine(self.tmp, self.chain, clock=clock, accept_test=True)
        e.on_relay_text(json.dumps(dict(MAZE_HELLO, label='TEST x', test=True)))
        e.on_relay_text(ep(0, 3, 1))
        st = e.public_status()
        self.assertEqual((st['source']['test'], st['relay']['test_stream'], st['relay']['counting'], st['relay']['channel']),
                         (True, True, True, 'maze'))
        self.assertEqual(st['this_hour']['escapes'], 3)
        sf = burn.StatusFile(os.path.join(self.tmp, 'status.json'), every=10, clock=lambda: 0.0)
        self.assertTrue(sf.update(e))
        with _real_open(os.path.join(self.tmp, 'status.json'), encoding='utf-8') as f:
            self.assertEqual(json.load(f)['this_hour']['escapes'], 3)
        e_pub = make_engine(os.path.join(self.tmp, 'p'), self.chain, clock=clock)
        self.assertEqual(e_pub.public_status()['source'], {'public_relay': True, 'accept_test_streams': False, 'test': False})
        with self.assertRaises(burn.ReportRefused) as cm:              # a DRY engine takes no execution report
            e_pub.add_report({'window': bb.iso(bb.window_start(clock.t)), 'tx': '0x' + '12' * 32, 'amount': '1'})
        self.assertEqual(cm.exception.status, 409)

    def test_engine_reads_only(self):
        rpc = burn.ReadRpc(self.chain)
        for m in ('eth_sendRawTransaction', 'eth_sendTransaction', 'eth_sign'):
            with self.assertRaises(burn.SendRefused):
                rpc.raw(m, ['0x00'])
        live = bb.LiveRpc(self.chain, _gate=bb._GATE_PASSED, nodes=[self.chain])
        c = cfg()
        with self.assertRaises(burn.LiveRefused):                      # the burn engine never takes a send-capable RPC
            burn.Engine(c, 'DRY', burn.Journal(os.path.join(self.tmp, 'j.jsonl')), live,
                        burn.DryExecutor(burn.Sim(live, c), c), burn.Sim(live, c))
        with self.assertRaises(burn.LiveRefused):
            burn.BookingExecutor(burn.Sim(live, c), c)
        with self.assertRaises(burn.LiveRefused):                      # no LIVE mode without live bookings
            e = make_engine(self.tmp, self.chain)
            burn.Engine(c, 'LIVE', e.journal, e.rpc, e.executor, e.sim)
        self.assertFalse(burn.STATE['env_read'])

    def test_main_dry_end_to_end(self):
        """main() in DRY with the fake token and a mock relay: maze-channel courses counted into a 2 s window
        (--window-s, tests only), the window closed and its burn simulated, status served, the untagged Rat Tiles
        text ignored, and no .env read, no signer, no send method anywhere."""
        from websockets.sync.server import serve
        msgs = ([state(True, episode=None), json.dumps(TILES_HELLO), ep(0, 30, 2, channel=None)]
                + [ep(n, 4) for n in range(6)] + [ep(6, 2, 1), maze_end('escaped', 3)])
        frames = [frame(b'MZ'), frame()]

        def handler(ws):
            for m in msgs:
                ws.send(m)
            for f in frames:
                ws.send(f)
            try:
                ws.recv(timeout=10)
            except Exception:
                pass
        srv = serve(handler, '127.0.0.1', MOCK_RELAY_PORT)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old_rpc = launcher.rpc
        launcher.rpc = self.chain                       # ReadRpc()'s default transport, for main() only
        out = {}

        def run():
            out['rc'] = burn.main(['--relay', f'ws://127.0.0.1:{MOCK_RELAY_PORT}/live', '--journal-dir', self.tmp,
                                   '--duration', '5', '--status-port', str(STATUS_PORT), '--tick', '0.2',
                                   '--window-s', '2'])
        t = threading.Thread(target=run)
        t.start()
        status = bookings = post = None
        base = f'http://127.0.0.1:{STATUS_PORT}'
        try:
            for _ in range(40):
                time.sleep(0.1)
                try:
                    with urllib.request.urlopen(base + '/status', timeout=2) as r:
                        status = json.loads(r.read())
                    if status['totals']['simulated_burns']:
                        with urllib.request.urlopen(base + '/bookings', timeout=2) as r:
                            bookings = json.loads(r.read())
                        req = urllib.request.Request(base + '/burn_report', data=b'{}', method='POST',
                                                     headers={'Authorization': 'Bearer x' * 30})
                        try:
                            urllib.request.urlopen(req, timeout=2)
                        except urllib.error.HTTPError as err:
                            post = err.code
                        break
                except OSError:
                    pass
            t.join(15)
        finally:
            srv.shutdown()
            launcher.rpc = old_rpc
        self.assertEqual(out.get('rc'), 0)
        self.assertIsNotNone(status)
        self.assertEqual((status['mode'], status['label'], status['simulated']), ('DRY', burn.DRY_LABEL, True))
        self.assertEqual((status['totals']['escapes'], status['totals']['timeouts'], status['totals']['courses']), (26, 1, 7))
        self.assertGreaterEqual(status['totals']['simulated_burns'], 1)
        self.assertEqual((status['totals']['burns'], status['totals']['burned']), (0, '0'))
        self.assertTrue(status['recent'][0]['simulated'])
        self.assertEqual(status['recent'][0]['label'], burn.SIMULATED_LABEL)
        self.assertEqual(status['method'], 'burn')
        self.assertIsNotNone(status['last_hour'])
        self.assertEqual(bookings['bookings'], [])
        self.assertEqual(post, 404, 'no report endpoint in DRY')
        self.no_address(status)
        self.assertFalse(burn.STATE['env_read'])
        self.assertLessEqual(self.chain.methods(), burn.READ_METHODS)
        with _real_open(os.path.join(self.tmp, 'journal.jsonl'), encoding='utf-8') as f:
            j = [json.loads(l) for l in f]
        self.assertTrue(all(r['mode'] == 'DRY' for r in j))
        self.assertEqual(sum(r['escapes'] for r in j if r['ev'] == 'course'), 26)
        self.assertTrue(all(b['simulated'] for b in j if b['ev'] == 'burn'))
        self.assertEqual(j[0]['ev'], 'start')
        self.assertEqual(j[-1]['ev'], 'end')
        self.assertTrue(os.path.exists(os.path.join(self.tmp, 'status.json')))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, burn.BOOKINGS_JOURNAL)))


class TestLiveBookings(Base):
    """LIVE BOOKINGS: the switch (flag AND environment), a booked (not burned) hour, the on-chain verification of the
    rig's report, expiry, stops, DRY unchanged. The engine never signs or sends: its RPC only reads; the 'rig'
    transactions here are receipts placed on the fake chain by mine_burn (no key, no send)."""

    def booked(self, clock=None, escapes=8, timeouts=0, e=None):
        clock = clock or getattr(self, 'clock', None) or Clock()
        self.clock = clock
        e = e or bookings_engine(self.tmp, self.chain, clock)
        courses = [(min(4, escapes - 4 * i), 0) for i in range(-(-escapes // 4))] + ([(0, timeouts)] if timeouts else [])
        feed(e, courses, start_n=len(records(e, 'course')))
        self.chain.ts = int(clock.t)
        close_hour(e, clock)
        self.chain.ts = int(clock.t)
        return e

    def test_the_switch_needs_the_flag_and_the_environment(self):
        def gate(args, env):
            a = burn.parse_args(args)
            return burn.bookings_gate(a, burn.build_config(a), environ=env)
        on = {burn.LIVE_BOOKINGS_ENV: '1'}
        self.assertFalse(gate([], {}))
        self.assertFalse(gate(['--live-bookings'], {}))                             # the flag alone: DRY
        self.assertFalse(gate([], on))                                              # the environment alone: DRY
        self.assertFalse(gate(['--live-bookings'], {burn.LIVE_BOOKINGS_ENV: 'yes'}))
        self.assertTrue(gate(['--live-bookings'], on))
        self.assertTrue(gate(['--live-bookings', '--burn-pct', '3'], on))
        for extra, why in ((['--window-s', '2'], 'once per UTC hour'), (['--accept-test-streams'], 'DRY tests only'),
                           (['--relay', f'ws://127.0.0.1:{MOCK_RELAY_PORT}/live'], 'public relay'),
                           (['--origin', 'https://evil.example'], 'public relay'),
                           (['--channel', 'training'], '"maze" channel only'), (['--method', 'dead'], 'own burn()')):
            with self.subTest(extra=extra):
                with self.assertRaises(SystemExit) as cm:
                    gate(['--live-bookings'] + extra, on)
                self.assertIn(why, str(cm.exception.code))
        with self.assertRaises(burn.LiveRefused):                # the engine refuses a half-built live bookings
            e = make_engine(self.tmp, self.chain)
            burn.Engine(e.cfg, 'LIVE', e.journal, e.rpc, e.executor, e.sim, live_bookings=True)
        with self.assertRaises(burn.LiveRefused):
            e = make_engine(self.tmp, self.chain)
            burn.Engine(e.cfg, 'DRY', e.journal, e.rpc, burn.BookingExecutor(e.sim, e.cfg), e.sim, live_bookings=True)

    def test_main_runs_live_bookings_only_with_both(self):
        class NoRelay:                        # live bookings listen only to the public relay: not in a mocked test
            def __init__(self, *a):
                self.thread = threading.Thread(target=lambda: None)

            def start(self):
                self.thread.start()
                return self
        old = (launcher.rpc, burn.RelayListener, os.environ.get(burn.LIVE_BOOKINGS_ENV))
        launcher.rpc, burn.RelayListener = self.chain, NoRelay
        args = ['--journal-dir', self.tmp, '--duration', '1.5', '--status-port', str(STATUS_PORT), '--tick', '0.2']
        seen = {}

        def run(argv, env_on, key):
            if env_on:
                os.environ[burn.LIVE_BOOKINGS_ENV] = '1'
            else:
                os.environ.pop(burn.LIVE_BOOKINGS_ENV, None)
            t = threading.Thread(target=lambda: seen.setdefault(key + '_rc', burn.main(argv)))
            t.start()
            for _ in range(20):
                time.sleep(0.1)
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{STATUS_PORT}/status', timeout=2) as r:
                        seen[key] = json.loads(r.read())
                        break
                except OSError:
                    pass
            t.join(10)
        try:
            run(args + ['--live-bookings'], True, 'both')
            run(args + ['--live-bookings'], False, 'flag_only')
            run(args, True, 'env_only')
        finally:
            launcher.rpc, burn.RelayListener = old[0], old[1]
            if old[2] is None:
                os.environ.pop(burn.LIVE_BOOKINGS_ENV, None)
            else:
                os.environ[burn.LIVE_BOOKINGS_ENV] = old[2]
        self.assertEqual((seen['both']['mode'], seen['both']['label'], seen['both']['simulated']),
                         ('LIVE', burn.LIVE_BOOKINGS_LABEL, False))
        self.assertEqual(seen['both']['totals']['booked'], 0)
        self.assertNotIn('simulated (eth_call)', seen['both']['rule'])
        for k in ('flag_only', 'env_only'):
            self.assertEqual((seen[k]['mode'], seen[k]['label'], seen[k]['simulated']), ('DRY', burn.DRY_LABEL, True), k)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, burn.BOOKINGS_JOURNAL)))
        with _real_open(os.path.join(self.tmp, burn.BOOKINGS_JOURNAL), encoding='utf-8') as f:
            self.assertTrue(all(json.loads(l)['mode'] == 'LIVE' for l in f))
        self.assertFalse(self.chain.methods() - burn.READ_METHODS, 'the engine only reads')

    def test_an_hour_is_booked_not_burned(self):
        e = self.booked(escapes=6, timeouts=2)                   # 6 / 8 = 75 %
        bal = self.chain.balances[WALLET]
        amt = expected_burn(bal, 6, 2)
        bk, = records(e, 'booking')
        self.assertEqual((bk['amount_wei'], bk['state'], bk['simulated'], bk['mode'], bk['method'], bk['balance_wei']),
                         (amt, 'booked', False, 'LIVE', 'burn', bal))
        self.assertEqual((bk['signable_until'], bk['dead_t']), (bk['window'] + burn.SIGN_UNTIL_S, bk['window'] + burn.BOOKING_DEAD_S))
        self.assertEqual(records(e, 'burn'), [], 'a booked hour is not a simulated burn')
        st = e.public_status()
        self.assertEqual((st['mode'], st['label'], st['simulated']), ('LIVE', burn.LIVE_BOOKINGS_LABEL, False))
        b, = st['bookings']
        self.assertEqual({k: b[k] for k in ('window', 'state', 'amount', 'amount_wei', 'method', 'simulated', 'label',
                                            'signable_until', 'escape_rate', 'escapes', 'timeouts', 'tx')},
                         {'window': bb.iso(bk['window']), 'state': 'booked', 'amount': bb.token_str(amt, 18),
                          'amount_wei': str(amt), 'method': 'burn', 'simulated': False, 'label': burn.BOOKED_LABEL,
                          'signable_until': bb.iso(bk['window'] + burn.SIGN_UNTIL_S), 'escape_rate': 0.75, 'escapes': 6,
                          'timeouts': 2, 'tx': None})
        self.assertEqual(st['recent'][0]['state'], 'booked')
        self.assertEqual((st['last_hour']['burn']['state'], st['last_hour']['burn']['label']), ('booked', burn.BOOKED_LABEL))
        self.assertEqual((st['totals']['burned'], st['totals']['burns'], st['totals']['booked']), ('0', 0, 1))
        self.assertIn((TOKEN, burn.SEL['burn'], WALLET), self.chain.froms, 'the exact burn was simulated from the wallet')
        self.assertFalse(self.chain.methods() - burn.READ_METHODS)
        s = self.no_address(st)
        self.assertNotRegex(s, r'0x[0-9a-fA-F]{3,}')
        # GET /bookings serves it; a restart keeps it booked
        srv = burn.serve_status(e, '127.0.0.1', STATUS_PORT)
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{STATUS_PORT}/bookings', timeout=5) as r:
                got = json.loads(r.read())
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual((got['mode'], got['method'], got['bookings'][0]['amount_wei']), ('LIVE', 'burn', str(amt)))
        e2 = bookings_engine(self.tmp, self.chain, self.clock)
        self.assertEqual(e2.public_status()['bookings'][0]['state'], 'booked')
        self.assertEqual(e2.ledger.bookings[bk['window']]['amount_wei'], amt)
        self.check_invariants(e)

    def test_a_verified_report_executes_the_booking(self):
        e = self.booked()
        bk, = records(e, 'booking')
        amt, w = bk['amount_wei'], bk['window']
        self.clock.t += 90
        self.chain.ts = int(self.clock.t)
        supply_before = self.chain.supply
        txh = self.chain.mine_burn(amt)
        rec = report(e, w, txh, method='burn', signed_at=bb.iso(self.clock.t))
        self.assertEqual((rec['ev'], rec['tx'], rec['amount_wei'], rec['burned_wei'], rec['method']),
                         ('executed', txh, amt, amt, 'burn'))
        self.assertEqual(rec['total_supply_wei'], supply_before - amt, 'the supply after the burn, from the chain')
        st = e.public_status()
        r = st['recent'][0]
        self.assertEqual((r['state'], r['tx'], r['simulated'], r['label'], r['explorer'], r['block']),
                         ('burned', txh, False, burn.EXECUTED_LABEL, burn.EXPLORER + txh, self.chain.block))
        self.assertEqual((st['totals']['burned'], st['totals']['burns'], st['totals']['booked']), (bb.token_str(amt, 18), 1, 0))
        self.assertEqual(st['totals']['total_supply'], bb.token_str(supply_before - amt))
        self.assertEqual((st['last_hour']['burn']['state'], st['last_hour']['burn']['tx']), ('burned', txh))
        self.assertEqual(st['bookings'], [])
        s = self.no_address(st)
        self.assertEqual(set(re.findall(r'0x[0-9a-fA-F]+', s)), {txh}, 'the only hex string is the transaction')
        # the same report again: idempotent; another tx for that window: refused
        self.assertTrue(report(e, w, txh)['already'])
        with self.assertRaises(burn.ReportRefused) as cm:
            report(e, w, self.chain.mine_burn(amt))
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(len(records(e, 'executed')), 1)
        # a restart replays it
        e2 = bookings_engine(self.tmp, self.chain, self.clock)
        self.assertEqual(e2.public_status()['recent'][0]['tx'], txh)
        self.assertEqual((e2.ledger.burned, e2.ledger.n_burns), (amt, 1))
        self.assertTrue(report(e2, w, txh)['already'])
        # the next hour, executed as a transfer to the dead address: verified too (the chain is the truth)
        e2 = self.booked(e=e2)
        w2 = max(e2.ledger.bookings)
        amt2 = e2.ledger.bookings[w2]['amount_wei']
        self.assertLess(amt2, amt, 'the balance fell, so the next 5 % is smaller')
        tx2 = self.chain.mine_burn(amt2, method='dead')
        rec2 = report(e2, w2, tx2, amount=None, amount_wei=str(amt2))
        self.assertEqual((rec2['method'], rec2['burned_wei']), ('dead', amt2))
        self.assertEqual(e2.public_status()['totals']['burned'], bb.token_str(amt + amt2, 18))
        self.check_invariants(e2)

    def test_the_http_endpoint_takes_reports(self):
        e = self.booked()
        bk, = records(e, 'booking')
        txh = self.chain.mine_burn(bk['amount_wei'])
        tok = 'rig-token-' + 'x' * 24
        base = f'http://127.0.0.1:{STATUS_PORT}'
        body = {'window': bb.iso(bk['window']), 'amount': bb.token_str(bk['amount_wei'], 18), 'tx': txh}

        def post(b, token=tok):
            req = urllib.request.Request(base + '/burn_report', data=json.dumps(b).encode(), method='POST', headers={
                'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as err:
                return err.code, json.loads(err.read() or b'{}')
        srv = burn.serve_status(e, '127.0.0.1', STATUS_PORT, 'short')       # a short token: the endpoint stays off
        try:
            self.assertEqual(post(body, 'short')[0], 404)
        finally:
            srv.shutdown()
            srv.server_close()
        srv = burn.serve_status(e, '127.0.0.1', STATUS_PORT, tok)
        try:
            self.assertEqual(post(body, 'wrong-' + 'y' * 24)[0], 401)
            self.assertEqual(post(dict(body, tx='0x' + '12' * 32))[0], 503)          # not on chain (yet)
            code, resp = post(body)
            self.assertEqual((code, resp['tx'], resp['label'], resp['already']), (200, txh, burn.EXECUTED_LABEL, False))
            code, resp = post(body)
            self.assertEqual((code, resp['already']), (200, True))
            with urllib.request.urlopen(base + '/status', timeout=5) as r:
                st = json.loads(r.read())
            self.assertEqual(st['recent'][0]['state'], 'burned')
            with urllib.request.urlopen(base + '/bookings', timeout=5) as r:
                self.assertEqual(json.loads(r.read())['bookings'], [])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_unverified_reports_are_refused(self):
        e = self.booked()
        bk, = records(e, 'booking')
        amt, w = bk['amount_wei'], bk['window']
        ch = self.chain
        cases = [
            ('unknown tx (no receipt yet)', lambda: '0x' + '34' * 32, {}, 503, 'no receipt'),
            ('from the launch wallet', lambda: ch.mine_burn(amt, frm=bb.WALLET), {}, 422, 'from (not'),
            ('to another contract', lambda: ch.mine_burn(amt, to=bb.ROUTER), {}, 422, 'to (not the token)'),
            ('carrying ETH', lambda: ch.mine_burn(amt, value=1), {}, 422, 'value (a burn carries no ETH)'),
            ('reverted', lambda: ch.mine_burn(amt, status=0), {}, 422, 'status (not 1'),
            ('another amount in the calldata', lambda: ch.mine_burn(amt // 2, log_amount=amt), {}, 422, 'calldata (another amount)'),
            ('not a burn (a transfer to a third address)', lambda: ch.mine_burn(amt, data=burn.cd_transfer(bb.ROUTER, amt)),
             {}, 422, 'calldata (not a burn'),
            ('the event to a third address', lambda: ch.mine_burn(amt, log_to=bb.ROUTER), {}, 422, 'no LABRAT Transfer'),
            ('the event from another address', lambda: ch.mine_burn(amt, log_from=bb.WALLET), {}, 422, 'no LABRAT Transfer'),
            ('the event of another amount', lambda: ch.mine_burn(amt, log_amount=amt - T), {}, 422, 'burned amount (not'),
            ("another token's event", lambda: ch.mine_burn(amt, log_token=bb.ROUTER), {}, 422, 'no LABRAT Transfer'),
            ('a dead transfer whose event says 0x0', lambda: ch.mine_burn(amt, method='dead', log_to=ZERO), {}, 422,
             'no LABRAT Transfer'),
            ('the report amount is not the booked one', lambda: ch.mine_burn(amt), {'amount': '1'}, 400, 'booked amount'),
            ('amount and amount_wei disagree', lambda: ch.mine_burn(amt), {'amount_wei': str(amt + 1)}, 400, 'disagree'),
            ('tx not a hash', lambda: '0x' + 'AB' * 32, {}, 400, 'transaction hash'),
            ('unknown field', lambda: ch.mine_burn(amt), {'extra': {'foo': 1}}, 400, 'unknown field'),
            ('a bad method', lambda: ch.mine_burn(amt), {'method': 'zero'}, 400, 'method must be'),
            ('a bad time', lambda: ch.mine_burn(amt), {'signed_at': 'yesterday'}, 400, 'signed_at'),
        ]
        for name, make, kw, status, phrase in cases:
            with self.subTest(name):
                txh = make()
                body = {'window': bb.iso(w), 'amount': kw.get('amount') or bb.token_str(amt, 18), 'tx': txh,
                        **{k: v for k, v in kw.items() if k != 'amount'}}
                with self.assertRaises(burn.ReportRefused) as cm:
                    e.add_report(body)
                self.assertEqual(cm.exception.status, status, str(cm.exception))
                self.assertIn(phrase, str(cm.exception))
        with self.assertRaises(burn.ReportRefused) as cm:                   # no booking for that window
            e.add_report({'window': bb.iso(w - HOUR), 'amount': '1', 'tx': ch.mine_burn(amt)})
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(burn.ReportRefused) as cm:                   # not an hour
            e.add_report({'window': bb.iso(w + 60), 'amount': '1', 'tx': '0x' + '12' * 32})
        self.assertEqual(cm.exception.status, 400)
        with self.assertRaises(burn.ReportRefused) as cm:                   # no amount at all
            e.add_report({'window': bb.iso(w), 'tx': '0x' + '12' * 32})
        self.assertIn('missing field: amount', str(cm.exception))
        old = ch.ts                                                         # mined before the booking
        txh = ch.mine_burn(amt)
        ch.ts = int(bk['t']) - 3600
        with self.assertRaises(burn.ReportRefused) as cm:
            report(e, w, txh)
        ch.ts = old
        self.assertEqual(cm.exception.status, 422)
        self.assertIn('time', str(cm.exception))
        ch.fail_reads = {'eth_getTransactionReceipt'}                       # the chain unreadable: report again
        with self.assertRaises(burn.ReportRefused) as cm:
            report(e, w, ch.mine_burn(amt))
        ch.fail_reads = set()
        self.assertEqual(cm.exception.status, 503)
        self.assertEqual(records(e, 'executed'), [])
        self.assertEqual(e.public_status()['recent'][0]['state'], 'booked')
        self.assertEqual(e.ledger.burned, 0)
        # a transaction counted for one window is never counted for another
        good = ch.mine_burn(amt)
        report(e, w, good)
        self.clock.t += 60
        e = self.booked(e=e)
        w2 = max(e.ledger.bookings)
        self.assertGreater(w2, w)
        with self.assertRaises(burn.ReportRefused) as cm:
            report(e, w2, good)
        self.assertEqual(cm.exception.status, 409)
        self.check_invariants(e)

    def test_expiry_a_late_execution_and_the_next_hour(self):
        e = self.booked()
        bk, = records(e, 'booking')
        w = bk['window']
        # the next hour closing does NOT expire an outstanding booking (the rig may still be signing it)
        feed(e, [(4, 0)], start_n=10)
        self.chain.ts = int(self.clock.t)
        close_hour(e, self.clock)
        self.assertEqual(len(records(e, 'booking')), 2)
        self.assertEqual(records(e, 'burn_expired'), [])
        self.assertEqual([b['state'] for b in e.public_status()['bookings']], ['booked', 'booked'])
        # its own time is up: released
        self.clock.t = w + burn.BOOKING_DEAD_S + 5
        e.tick()
        self.assertEqual(e.ledger.bookings[w]['state'], 'expired')
        st = e.public_status()
        self.assertEqual([r['state'] for r in st['recent']], ['booked', 'expired'])
        self.assertEqual((st['recent'][1]['label'], st['recent'][1]['note']), (burn.EXPIRED_LABEL, 'not executed in time'))
        self.assertEqual(len(st['bookings']), 1)
        # it did execute after all: the chain is the truth
        self.chain.ts = int(self.clock.t)
        txh = self.chain.mine_burn(bk['amount_wei'])
        report(e, w, txh)
        self.assertEqual((e.ledger.burned, e.ledger.n_burns), (bk['amount_wei'], 1))
        self.assertEqual(e.public_status()['recent'][1]['state'], 'burned')
        self.check_invariants(e)

    def test_a_stop_survives_a_restart_and_clears_by_its_id(self):
        clock = Clock()
        e = bookings_engine(self.tmp, self.chain, clock)
        e._stop('the chain no longer matches the pinned coin', 'stopped')
        sid = records(e, 'stop')[-1]['id']
        e2 = bookings_engine(self.tmp, self.chain, clock)
        self.assertTrue(e2.stopped)
        self.assertFalse(e2.clear_stop('00000000'))
        self.assertTrue(e2.stopped)
        self.assertTrue(e2.clear_stop(sid))
        self.assertIsNone(e2.stopped)
        e3 = bookings_engine(self.tmp, self.chain, clock)
        self.assertIsNone(e3.stopped)
        self.assertFalse(e3.clear_stop(sid), 'a cleared stop id clears nothing again')
        # a stopped engine books nothing
        e3._stop('x', 'stopped')
        feed(e3, [(4, 0)])
        close_hour(e3, clock)
        self.assertEqual(records(e3, 'booking'), [])

    def test_dry_is_unchanged(self):
        clock = Clock()
        e = make_engine(self.tmp, self.chain, clock=clock)
        feed(e, [(4, 0)] * 2)
        close_hour(e, clock)
        self.assertEqual(records(e, 'booking'), [])
        b, = records(e, 'burn')
        self.assertTrue(b['simulated'])
        st = e.public_status()
        self.assertEqual((st['mode'], st['label'], st['simulated'], st['bookings']), ('DRY', burn.DRY_LABEL, True, []))
        self.assertEqual(e.ledger.bookings, {})
        with self.assertRaises(burn.ReportRefused) as cm:
            e.add_report({'window': bb.iso(b['window']), 'amount': '1', 'tx': '0x' + '12' * 32})
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(self.chain.balances[WALLET], 15_794_438 * T, 'nothing moved')


def main():
    install_traps()
    prog = unittest.main(argv=[sys.argv[0]] + sys.argv[1:], exit=False, verbosity=2)
    rc = 0 if prog.result.wasSuccessful() else 1
    print(f"traps: .env opened {TRAPS['env_opens']}, signs {TRAPS['signs']}, blocked network {TRAPS['net']}, "
          f"launcher.rpc calls {TRAPS['launcher_rpc']}")
    if TRAPS['env_opens'] or TRAPS['signs'] or TRAPS['launcher_rpc']:
        rc = 1
    return rc


if __name__ == '__main__':
    sys.exit(main())
