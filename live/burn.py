"""$LABRAT burns: ONE burn an hour, sized by the rat's Rat Maze escape rate in that hour.

While the Rat Maze trainer streams to lab-rat.net/burn (the relay's "maze" channel: the newest saved maze checkpoint,
playing courses of mazes in its own simulation, live/publish_training.py --channel maze), this engine counts the mazes
the rat escapes and the mazes it runs out of time in. Once an hour, on the hour (UTC), it closes the hour that just
ended and sizes that hour's burn of the $LABRAT the rat's wallet holds:
    escape rate = escapes / (escapes + timeouts)
    the hour's burn = floor(burn% x the wallet's LABRAT balance at the hour's close x escape rate), whole LABRAT
burn% is 5 by default and can only be LOWERED (--burn-pct <= 5): 5 % of the wallet's balance per hour is a ceiling in
code (BURN_BPS_HARD), whatever the command line says. The wallet is the buyback wallet (live/buyback.py's
BUYBACK_WALLET): the LABRAT it holds came from the rat's hourly buybacks (Rat Tiles drives those, on /buyback; Rat Maze
drives the burns, on /burn). Rat Tiles code and the buyback engine are unchanged; the buyback engine runs with
--tasks cursor,steer,tiles so a maze escape counts here only.

Say it this way: the rat's brain is two trained artificial neural networks, not a biological brain, and it does not
understand money. This code sets the rule; the rat's escapes only trigger it. A simulated burn is never called a burn.

    python live/burn.py                                # DRY (the default): listen, count, simulate each burn, journal
    python live/burn.py --status-port 4930             # DRY + the public status JSON on http://127.0.0.1:4930/status
    python live/burn.py --burn-pct 2                   # DRY, 2 % of the balance x the escape rate (never above 5)
    python live/burn.py --check                        # one read-only check: the token, the wallet, a simulated burn
    python live/burn.py --live-bookings                # LIVE BOOKINGS, only with BURN_LIVE_BOOKINGS=1 (see below)

WHAT COUNTS (escapes, timeouts)
  The relay's public /live stream (Origin https://lab-rat.net) carries the Rat Maze trainer's messages tagged
  "channel":"maze" (the default training channel, untagged, is Rat Tiles'; the pons channel is tagged "pons"; this
  engine ignores every message that is not on its channel, and every binary frame: an "MZ"-prefixed maze frame or a
  plain training frame moves nothing). A course of mazes ends with {"type":"episode","n","presses","hits","misses"}:
  hits = mazes escaped, misses = mazes timed out (maze_env.py; a course ends at its first timeout or after
  MAZE_MAX_HITS escapes), presses = hits + misses. The engine books it into the UTC hour it arrives in, keyed by
  run | task | hello.started | n and counted once (a reconnect, a re-sent state, a publisher resync or a restart of
  this program never counts it twice; the journal holds the keys). Only a live training hello of task "maze" counts
  (source "training", not a TEST stream unless --accept-test-streams, DRY only). A course claiming more than
  MAZE_MAX_HITS escapes, counts that do not add up, or anything that is not a non-negative integer is rejected and
  journalled. {"type":"maze_end"} events only feed the unconfirmed tally of the course in progress: money never
  depends on them. Courses that ended while this program was disconnected are missed (a gap is journalled).

THE HOUR
  At the end of each UTC hour the engine journals a 'window' record (escapes, timeouts, courses, escape rate). If the
  hour had escapes, the burn is due: the engine reads the wallet's LABRAT balance (eth_call balanceOf, right then),
  computes floor(burn% x balance x escape rate) rounded DOWN to a whole LABRAT, cut to the 5 % ceiling (and to
  --max-burn-labrat when set), and either simulates it (DRY) or books it for the buy rig (LIVE BOOKINGS). An hour with
  no courses, no escapes, an empty wallet, or a burn under --min-burn-labrat (1) burns nothing ('window_noburn'). A
  passing problem (the gas price over its cap, a chain read that failed, a failed attempt) retries until the next
  hour closes, which replaces it ('burn_expired'); max_failures failed attempts in a row stop the engine. An hour
  that ended while this program was not running is closed when it restarts, late: only the hour just ended can burn.
  The rate compounds: at a perfect escape rate all day, 24 burns of 5 % leave 0.95^24 = 29 % of the holding.

HOW IT BURNS
  The token's bytecode carries burn(uint256) (selector 0x42966c68; checked read-only on 2026-09-26 with a simulated
  burn of 1 LABRAT from the wallet: 33,972 gas), so the burn is the token's OWN burn: it lowers totalSupply and emits
  Transfer(wallet -> 0x0). A transfer to the zero address itself reverts (ERC20InvalidReceiver), so the only fallback,
  for a token without burn(), is transfer(0x000000000000000000000000000000000000dEaD, amount) (--method dead, or
  automatic when the bytecode has no burn selector). Both are recognised when a reported burn is verified.

DRY (the default; the only mode that has run)
  Simulates each hour's burn with eth_call and eth_estimateGas from the wallet on the real chain, journals it as
  'burn' with simulated true, and the public status labels it "Simulated burn · not executed". DRY never reads .env,
  never builds a signer, holds no key, and its RPC object can only read (buyback.ReadRpc: a send raises SendRefused).
  The journal is runs/burn/journal.jsonl (append-only, one fsync'd line per record); a restart rebuilds the state.

LIVE BOOKINGS (switched OFF: real burns from the rat's wallet, signed by the buy rig; live/BURN.md "Going live")
  Only with BOTH the --live-bookings flag AND the process environment's BURN_LIVE_BOOKINGS=1 (either alone: DRY,
  exactly as above). It also needs real hour windows, the public relay and origin, the "maze" channel and no
  --accept-test-streams. Then:
  * the status says mode LIVE ("Burns live" on the site) and its journal is <journal-dir>/journal_bookings.jsonl;
  * each hour's burn is BOOKED, not simulated as done: after the token check, the gas price cap and a simulation of
    the exact burn from the wallet, it journals a 'booking' (window, exact amount in wei, method) and lists it under
    "bookings" and "recent" with state "booked". This engine holds no key and never sends. The buy rig's runner
    (live/buyrig_runner.py --burn-status-url, service labrat-buyrig: the only place the wallet's key exists) executes
    it as ONE transaction through its LIVE journal (window kind "burn"), gated by BURN_LIVE=1 + BURN_CONFIRM=LABRAT
    and its own key / wallet / chain checks, never concurrently with a buy;
  * the runner reports it: POST /burn_report (Bearer BURN_RIG_TOKEN) with {window, tx, amount}. The engine VERIFIES
    IT ON CHAIN before counting it (read-only): receipt status 1, from = the wallet, to = the token, value 0, the
    calldata a burn(amount) or transfer(dEaD, amount) of exactly the booked amount, a LABRAT Transfer from the wallet
    to 0x0 (burn) or dEaD (transfer) of exactly that amount, mined after the booking, a hash never counted before.
    Then the booking is 'executed' ("Burned · verified on chain", with its tx). Otherwise 422 (does not verify), 404
    (no booking for that hour), 409 (the hour already has another executed burn, or the hash is counted for another
    hour), 503 (no receipt on this engine's RPC yet: the runner reports again);
  * a booking the rig can no longer sign (one hour after its window ended) plus a margin is released as
    'booking_expired' ("Not executed"). A late execution that does verify is still counted: the chain is the truth;
  * a stop survives restarts; an operator clears it with BURN_CLEAR_STOP=<the stop's id> (in the log).
"""
import argparse
import collections
import copy
import hashlib
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LIVE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(LIVE_DIR)
for _p in (ROOT, LIVE_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import buyback  # noqa: E402  (the pins, ReadRpc, Journal, StatusFile, HitCounter, helpers; nothing that signs)
from buyback import (RpcError, SendRefused, LiveRefused, PonsRefused as ReportRefused, ReadRpc, Journal,  # noqa: E402
                     StatusFile, iso, eth_str, token_str, parse_eth, window_start, rate_str, pad_addr, words, w_int,
                     sha, call_info, _bearer, _same_secret, _iso_ts, ISO_RE, DEC_RE, TX_RE, WINDOW_ID_RE)
from eth_abi import decode, encode  # noqa: E402
from eth_utils import keccak, to_checksum_address  # noqa: E402

# ---------------------------------------------------------------------------------------------------- the pins
CHAIN_ID = buyback.CHAIN_ID
SYMBOL = buyback.SYMBOL
TOKEN = buyback.TOKEN
WALLET = buyback.BUYBACK_WALLET            # the rat's wallet: the buyback wallet, holder of the LABRAT its buybacks bought
DEAD = to_checksum_address('0x000000000000000000000000000000000000dEaD')
ZERO = buyback.ZERO
RELAY_URL, ORIGIN = buyback.RELAY_URL, buyback.ORIGIN
TASK = 'maze'
CHANNEL = 'maze'                           # the relay channel of the Rat Maze trainer: its text carries "channel":"maze"
DEFAULT_CHANNEL = 'training'               # the name this program gives untagged text (the relay's default channel)
CHANNEL_RE = re.compile(r'^[a-z][a-z0-9_-]{0,31}$')
MAZE_MAX_HITS = buyback.MAZE_MAX_HITS      # a course is at most 4 mazes (maze_env.LIVE_MAZES)
HOUR_S = buyback.HOUR_S
TOKEN_STEP_WEI = 10 ** 18                  # burns are whole LABRAT (18 decimals)
BURN_BPS_HARD = 500                        # 5 % of the wallet's balance per hour: the ceiling in code
GAS_MULT, FEE_MULT = buyback.GAS_MULT, buyback.FEE_MULT
DEFAULT_JOURNAL_DIR = os.path.join(ROOT, 'runs', 'burn')
STATUS_REWRITE_S = buyback.STATUS_REWRITE_S
BALANCE_EVERY_S = 60.0                     # the wallet's balance is re-read for the status at most this often
EXPLORER = 'https://robinhoodchain.blockscout.com/tx/'
FUNDING = "the rat's wallet: the $LABRAT its hourly buybacks bought"
BURN_RULE = 'burn% x the wallet balance x the escape rate, whole LABRAT, at most 5% of the balance an hour'
METHODS = ('burn', 'dead')
METHOD_TEXT = {'burn': "the token's own burn(uint256): it lowers the total supply",
               'dead': 'transfer(0x…dEaD, amount): sent to the dead address (the token has no burn())'}


def _sel(sig):
    return '0x' + keccak(text=sig).hex()[:8]


SEL = {'burn': _sel('burn(uint256)'), 'burn_from': _sel('burnFrom(address,uint256)'),
       'transfer': _sel('transfer(address,uint256)'), 'balance_of': _sel('balanceOf(address)'),
       'total_supply': _sel('totalSupply()')}
TRANSFER_TOPIC = buyback.TRANSFER_TOPIC
READ_METHODS = buyback.READ_METHODS

DRY_LABEL = 'DRY - simulated, not executed'
LIVE_BOOKINGS_LABEL = "LIVE - real burns from the rat's wallet, signed by the buy rig"
SIMULATED_LABEL = 'Simulated burn · not executed'
BOOKED_LABEL = 'Booked · not executed yet'
EXECUTED_LABEL = 'Burned · verified on chain'
EXPIRED_LABEL = 'Not executed'
STATE = {'env_read': False}                # this program never reads .env (kept for symmetry with buyback.STATE)

LIVE_BOOKINGS_ENV = 'BURN_LIVE_BOOKINGS'   # must be "1", AND the --live-bookings flag
CLEAR_STOP_ENV = 'BURN_CLEAR_STOP'         # an operator's "I checked": the id of the stop to clear
RIG_TOKEN_ENV = 'BURN_RIG_TOKEN'           # the buy rig's Bearer token for POST /burn_report (process env, never .env)
RIG_TOKEN_MIN = buyback.RIG_TOKEN_MIN
BOOKINGS_JOURNAL = 'journal_bookings.jsonl'
SIGN_UNTIL_S = 2 * HOUR_S                  # the rig signs a window's burn at most one hour after the window ends
BOOKING_DEAD_S = SIGN_UNTIL_S + 900        # + a margin for a burn signed at the last moment to be mined and reported
REPORT_KEYS = frozenset({'window', 'tx', 'amount', 'amount_wei', 'method', 'signed_at'})
REPORT_BODY_MAX = 2048
WEI_RE = re.compile(r'^\d{1,40}$')


# ---------------------------------------------------------------------------------------------------- config
@dataclass(frozen=True)
class Config:
    burn_bps: int = BURN_BPS_HARD               # burn% x 100: 500 = 5 % of the balance x the escape rate
    min_burn_wei: int = TOKEN_STEP_WEI          # 1 LABRAT: an hour whose burn is smaller burns nothing
    max_burn_wei: object = None                 # an optional absolute cap per burn (wei); None = the 5 % ceiling only
    max_gas_price_wei: int = 10 ** 9            # skip while gas is above 1 gwei (it is about 0.03)
    max_failures: int = 3                       # consecutive failed attempts -> stop
    window_s: int = HOUR_S                      # the burn window: the UTC hour (shorter only in tests; LIVE refuses it)
    channel: str = CHANNEL                      # the relay channel counted (LIVE: "maze" only)
    method: str = 'auto'                        # auto (from the bytecode) | burn | dead

    def validate(self):
        bad = []
        for k in ('burn_bps', 'min_burn_wei', 'max_gas_price_wei', 'max_failures', 'window_s'):
            v = getattr(self, k)
            if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
                bad.append(f'{k} must be a positive integer')
        if self.max_burn_wei is not None and (not isinstance(self.max_burn_wei, int) or isinstance(self.max_burn_wei, bool)
                                              or self.max_burn_wei <= 0):
            bad.append('max_burn must be a positive amount (or unset)')
        if bad:
            raise ValueError('; '.join(bad))
        for k, cap in HARD.items():
            if getattr(self, k) > cap:
                bad.append(f'{k} {getattr(self, k)} is over the hard ceiling {cap}')
        if self.max_burn_wei is not None and self.max_burn_wei < self.min_burn_wei:
            bad.append('max_burn is under min_burn')
        if not 1 <= self.window_s <= HOUR_S or HOUR_S % self.window_s:
            bad.append('window_s must divide 3600 (the UTC hour)')
        if not isinstance(self.channel, str) or not CHANNEL_RE.match(self.channel):
            bad.append('channel must be a short lowercase name, like maze')
        if self.method not in ('auto',) + METHODS:
            bad.append('method must be auto, burn or dead')
        if bad:
            raise ValueError('; '.join(bad))
        return self


HARD = {'burn_bps': BURN_BPS_HARD, 'max_gas_price_wei': 10 * 10 ** 9}   # a typo on the command line can never lift these


# ---------------------------------------------------------------------------------------------------- helpers
def log(*parts):
    try:
        stamp = datetime.now(timezone.utc).strftime('%H:%M:%S')
        print(f'[burn {stamp}] ' + ' '.join(str(p) for p in parts), file=sys.stderr, flush=True)
    except Exception:
        pass


def pct_str(bps):
    return f'{Decimal(int(bps)) / 100:f}'.rstrip('0').rstrip('.') or '0'


def parse_pct(s):
    """--burn-pct: a percentage with at most 2 decimals -> basis points (int). ValueError otherwise (the ceiling is
    checked by Config.validate)."""
    try:
        d = Decimal(str(s).strip())
    except InvalidOperation:
        raise ValueError(f'not a percentage: {s!r}') from None
    bps = d * 100
    if bps != bps.to_integral_value() or bps <= 0:
        raise ValueError(f'burn-pct must be > 0 with at most 2 decimals: {s!r}')
    return int(bps)


def escape_rate(escapes, timeouts):
    """escapes / (escapes + timeouts), or None for an hour with no mazes."""
    return buyback.hit_rate(escapes, timeouts, 0)


def burn_amount(balance_wei, bps, escapes, timeouts, max_burn_wei=None):
    """The hour's burn: floor(balance x bps/10000 x escapes / (escapes + timeouts)), never above BURN_BPS_HARD of the
    balance nor max_burn_wei, rounded DOWN to a whole LABRAT (integer arithmetic). 0 when nothing was escaped."""
    n = int(escapes) + int(timeouts)
    balance_wei = int(balance_wei)
    if n <= 0 or int(escapes) <= 0 or balance_wei <= 0:
        return 0
    bps = min(int(bps), BURN_BPS_HARD)
    amt = balance_wei * bps * int(escapes) // (10_000 * n)
    amt = min(amt, balance_wei * BURN_BPS_HARD // 10_000)
    if max_burn_wei is not None:
        amt = min(amt, int(max_burn_wei))
    return amt // TOKEN_STEP_WEI * TOKEN_STEP_WEI


def share_bps(amount_wei, balance_wei):
    return None if int(balance_wei) <= 0 else int(int(amount_wei) * 10_000 // int(balance_wei))


# ---------------------------------------------------------------------------------------------------- calldata
def cd_burn(amount):
    return SEL['burn'] + encode(['uint256'], [int(amount)]).hex()


def cd_transfer(to, amount):
    return SEL['transfer'] + encode(['address', 'uint256'], [to_checksum_address(to), int(amount)]).hex()


def cd_balance_of(addr):
    return SEL['balance_of'] + pad_addr(addr)


def burn_calldata(method, amount):
    if method == 'burn':
        return cd_burn(amount)
    if method == 'dead':
        return cd_transfer(DEAD, amount)
    raise ValueError(f'no burn method {method!r}')


def decode_burn(data):
    """-> (method, amount) for a burn(amount) or a transfer(dEaD, amount) of the token; ValueError otherwise."""
    if not isinstance(data, str):
        raise ValueError('no calldata')
    data = data.lower()
    if data[:10] == SEL['burn'] and len(data) == 10 + 64:
        return 'burn', decode(['uint256'], bytes.fromhex(data[10:]))[0]
    if data[:10] == SEL['transfer'] and len(data) == 10 + 128:
        to, amount = decode(['address', 'uint256'], bytes.fromhex(data[10:]))
        if to_checksum_address(to) != DEAD:
            raise ValueError('a transfer, but not to the dead address')
        return 'dead', amount
    raise ValueError('not a burn(uint256) or a transfer(dEaD, uint256)')


def check_burn_tx(to, data, value, amount_wei):
    """The exact shape of a burn transaction: to the token, no ETH, a burn / dead transfer of exactly amount_wei.
    -> the method. ValueError otherwise (checked before simulating, and again on a reported transaction)."""
    if to_checksum_address(to) != TOKEN:
        raise ValueError(f'{to} is not the pinned token')
    if int(value) != 0:
        raise ValueError(f'a burn carries no ETH (value {value})')
    method, amount = decode_burn(data)
    if amount != int(amount_wei):
        raise ValueError(f'calldata burns {amount} wei, not the booked {int(amount_wei)}')
    return method


# ---------------------------------------------------------------------------------------------------- the chain
def check_token(rpc, method_pref='auto'):
    """The pinned token, before every burn (read-only): it has code, and that code carries burn(uint256) (else
    transfer). -> {ok, stop, method, has_burn, code_bytes, block, timestamp, problems, public}. Network trouble raises
    (the caller retries later); a mismatch returns stop=True."""
    blk = rpc.ok('eth_getBlockByNumber', ['latest', False])
    out = {'block': int(blk['number'], 16), 'timestamp': int(blk['timestamp'], 16), 'problems': [], 'method': None,
           'has_burn': None, 'code_bytes': 0}
    p = out['problems']
    code = rpc.ok('eth_getCode', [TOKEN, 'latest'])
    h = code[2:].lower() if isinstance(code, str) and code.startswith('0x') else ''
    out['code_bytes'] = len(h) // 2
    if not h:
        p.append('no contract code at the pinned token')
    else:
        out['has_burn'] = SEL['burn'][2:] in h
        has_transfer = SEL['transfer'][2:] in h
        if not out['has_burn'] and not has_transfer:
            p.append('the token bytecode has neither burn(uint256) nor transfer(address,uint256)')
        elif method_pref == 'burn' and not out['has_burn']:
            p.append('--method burn, but the token bytecode has no burn(uint256)')
        else:
            out['method'] = method_pref if method_pref in METHODS else ('burn' if out['has_burn'] else 'dead')
    out['ok'] = not p
    out['stop'] = bool(p)
    out['public'] = None if not p else 'the chain no longer matches the pinned coin'
    return out


class Sim:
    """eth_call / eth_estimateGas simulations from the rat's wallet, and the reads the engine needs. Read-only."""

    def __init__(self, rpc, cfg, clock=time.time, wallet=WALLET):
        self.rpc, self.cfg, self.clock, self.wallet = rpc, cfg, clock, to_checksum_address(wallet)

    def gas_price(self):
        return int(self.rpc.ok('eth_gasPrice', []), 16)

    def eth_balance(self, addr=None):
        return int(self.rpc.ok('eth_getBalance', [addr or self.wallet, 'latest']), 16)

    def token_balance(self, addr=None):
        """The wallet's LABRAT (wei), from the chain, now."""
        w = words(self.rpc.ok('eth_call', [{'to': TOKEN, 'data': cd_balance_of(addr or self.wallet)}, 'latest']))
        return w_int(w[0]) if w else 0

    def total_supply(self):
        w = words(self.rpc.ok('eth_call', [{'to': TOKEN, 'data': SEL['total_supply']}, 'latest']))
        return w_int(w[0]) if w else 0

    def burn(self, method, amount):
        """The exact burn transaction for `amount` wei, simulated from the wallet (no override: the tokens must
        really be there). -> {ok, to, data, value, gas, method, call} or {ok: False, error}."""
        data = burn_calldata(method, amount)
        check_burn_tx(TOKEN, data, 0, amount)
        call = {'from': self.wallet, 'to': TOKEN, 'data': data, 'value': '0x0'}
        _res, err = self.rpc.raw('eth_call', [call, 'latest'])
        if err is not None:
            return {'ok': False, 'error': f'{method} simulation reverted: {buyback.short_err(err)}'}
        gas = int(self.rpc.ok('eth_estimateGas', [call, 'latest']), 16)
        return {'ok': True, 'to': TOKEN, 'data': data, 'value': 0, 'gas': gas, 'method': method,
                'call': call_info(TOKEN, data, 0, fn=('burn(uint256)' if method == 'burn' else 'transfer(dEaD,uint256)'),
                                  amount_wei=int(amount), result='ok')}


def verify_burn(rpc, txh, amount_wei, booked_t, wallet=None):
    """Read-only, on chain: txh is the wallet's burn of exactly amount_wei LABRAT (the token's burn(), or a transfer to
    the dead address), mined with status 1 after the booking, with the matching Transfer event. -> {burned_wei,
    method, block, block_time, gas...}. ReportRefused 503 while the chain has no receipt (or cannot be read), 422 when
    it does not verify."""
    wallet = to_checksum_address(wallet or WALLET)
    try:
        rc, err = rpc.raw('eth_getTransactionReceipt', [txh])
        tx, err2 = rpc.raw('eth_getTransactionByHash', [txh])
    except (RpcError, RuntimeError) as e:
        raise ReportRefused(f'the chain could not be read ({type(e).__name__}); report it again', 503) from None
    if err is not None or err2 is not None or not rc or not tx:
        raise ReportRefused('the chain has no receipt for that transaction yet; report it again', 503)
    lo = lambda v: str(v or '').lower()  # noqa: E731
    bad = []
    if lo(rc.get('transactionHash')) != txh or lo(tx.get('hash')) != txh:
        bad.append('hash')
    try:
        status = int(rc.get('status') or '0x0', 16)
    except ValueError:
        status = None
    if status != 1:
        bad.append('status (not 1: it reverted)')
    if lo(tx.get('from')) != wallet.lower() or (rc.get('from') is not None and lo(rc.get('from')) != wallet.lower()):
        bad.append("from (not the rat's wallet)")
    if lo(tx.get('to')) != TOKEN.lower() or (rc.get('to') is not None and lo(rc.get('to')) != TOKEN.lower()):
        bad.append('to (not the token)')
    try:
        value = int(tx.get('value') or '0x0', 16)
    except ValueError:
        value = None
    if value != 0:
        bad.append('value (a burn carries no ETH)')
    method = None
    try:
        method, amt = decode_burn(lo(tx.get('input') or tx.get('data')))
        if amt != int(amount_wei):
            bad.append('calldata (another amount)')
    except Exception:
        bad.append('calldata (not a burn of the token)')
    sink = ZERO if method == 'burn' else DEAD if method == 'dead' else None
    burned = 0
    for lg in rc.get('logs') or []:
        topics = lg.get('topics') or []
        if (lo(lg.get('address')) == TOKEN.lower() and len(topics) > 2 and lo(topics[0]) == TRANSFER_TOPIC
                and lo(topics[1])[-40:] == wallet[2:].lower() and sink is not None
                and lo(topics[2])[-40:] == sink[2:].lower()):
            try:
                burned += int(lg.get('data') or '0x0', 16)
            except ValueError:
                pass
    if burned <= 0:
        bad.append('no LABRAT Transfer from the wallet to the zero / dead address')
    elif burned != int(amount_wei):
        bad.append('burned amount (not the booked amount)')
    try:
        block = int(rc['blockNumber'], 16)
        blk = rpc.ok('eth_getBlockByNumber', [hex(block), False])
        block_time = int(blk['timestamp'], 16)
    except (RpcError, RuntimeError) as e:
        raise ReportRefused(f'the block could not be read ({type(e).__name__}); report it again', 503) from None
    except (KeyError, TypeError, ValueError):
        block, block_time = None, None
        bad.append('block')
    if block_time is not None and block_time < int(booked_t) - 60:
        bad.append('time (mined before the burn was booked)')
    if bad:
        raise ReportRefused(f'the transaction does not verify as this booked burn: {"; ".join(bad)}', 422)
    gas_used = int(rc.get('gasUsed') or '0x0', 16)
    price = int(rc.get('effectiveGasPrice') or '0x0', 16)
    return {'burned_wei': burned, 'method': method, 'block': block, 'block_time': block_time, 'gas_used': gas_used,
            'gas_price_wei': price, 'gas_cost_wei': gas_used * price}


# ---------------------------------------------------------------------------------------------------- counting
class MazeCounter:
    """Relay messages -> counted courses (escapes, timeouts). Only text on `channel` is looked at; a course ends with
    an episode message, validated and de-duplicated by buyback.HitCounter (tasks = maze only). maze / maze_end
    messages feed the unconfirmed tally of the course in progress. Pure (no I/O)."""

    def __init__(self, channel=CHANNEL, accept_test=False, seen=None):
        self.channel = channel
        self.inner = buyback.HitCounter(tasks=(TASK,), max_hits=MAZE_MAX_HITS, accept_test=accept_test, seen=seen)
        self.stats = self.inner.stats
        self.course = {'escapes': 0, 'timeouts': 0, 'maze_id': None, 'w': None, 'h': None}

    @property
    def session(self):
        return self.inner.session

    @property
    def live(self):
        return self.inner.live

    @property
    def seen(self):
        return self.inner.seen

    def _reset_course(self):
        self.course = {'escapes': 0, 'timeouts': 0, 'maze_id': None, 'w': None, 'h': None}

    def _counting(self):
        s = self.inner.session
        return bool(s and s['countable'] and self.inner.live)

    def on_text(self, text):
        out = []
        if not isinstance(text, str) or len(text) > 70_000:
            self.stats['dropped_text'] += 1
            return out
        try:
            m = json.loads(text)
        except ValueError:
            self.stats['bad_json'] += 1
            return out
        if not isinstance(m, dict):
            return out
        if m.get('channel', DEFAULT_CHANNEL) != self.channel:
            self.stats['other_channel'] += 1
            return out
        t = m.get('type')
        if t == 'maze_end':
            if self._counting():
                r = m.get('result')
                if r == 'escaped' and self.course['escapes'] < 4 * MAZE_MAX_HITS:
                    self.course['escapes'] += 1
                elif r == 'timeout' and self.course['timeouts'] < 4 * MAZE_MAX_HITS:
                    self.course['timeouts'] += 1
            return out
        if t == 'maze':
            if self._counting():
                for k in ('maze_id', 'w', 'h'):
                    v = m.get(k)
                    self.course[k] = v if isinstance(v, int) and not isinstance(v, bool) else None
            return out
        out = self.inner.on_text(text)
        if t in ('hello', 'state', 'bye', 'idle') or any(e['ev'] == 'episode' for e in out):
            self._reset_course()
        return out

    def on_bytes(self, data):
        """A frame (an "MZ"-prefixed maze frame or a plain training frame): counted, never read. Money never depends
        on a frame, and a maze has no clicks to tally."""
        self.stats['frames'] += 1


# ---------------------------------------------------------------------------------------------------- the ledger
class Ledger:
    """The hour windows (escapes, timeouts), the hour's burn that is due, the simulated / booked / verified burns and
    the totals. Rebuilt from the journal."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.seen = set()
        self.escapes = self.timeouts = self.attempts = 0
        self.windows = {}                          # window start -> {escapes, timeouts, attempts} (not closed yet)
        self.closed = set()
        self.due = None                            # the closed window whose burn is due and not done yet
        self.last_window = None                    # the public summary of the last closed window
        self.recent = collections.deque(maxlen=12)
        self.n_sim = self.sim_burned = 0           # DRY: simulated burns
        self.n_burns = self.burned = 0             # LIVE: burns verified on chain
        self.bookings = {}                         # window start -> {state, amount_wei, t, dead_t, method, entry, tx}
        self.executed_txs = set()
        self.n_booked = 0
        self.failures = 0
        self.stop_rec = None
        self.since = None
        self.balance = None                        # the wallet's LABRAT (wei) as last read, and when / at which block
        self.balance_t = None
        self.balance_block = None
        self.supply = None                         # totalSupply as last read (wei)

    def win_counts(self, start):
        w = self.windows.get(start)
        return dict(w) if w else {'escapes': 0, 'timeouts': 0, 'attempts': 0}

    def outstanding(self):
        return [b for _w, b in sorted(self.bookings.items()) if b['state'] == 'booked']

    def _lw_burn(self, w, **fields):
        if self.last_window is not None and self.last_window.get('start') == iso(w):
            self.last_window['burn'] = {**(self.last_window.get('burn') or {}), **fields}

    def _entry(self, r, state, label, simulated):
        w, amt = int(r['window']), int(r['amount_wei'])
        bal = int(r.get('balance_wei') or 0)
        return {'window': iso(w), 'at': iso(r['t']), 'state': state, 'label': label, 'simulated': simulated,
                'amount': token_str(amt, 18), 'amount_wei': str(amt), 'balance': token_str(bal),
                'share_pct': None if share_bps(amt, bal) is None else round(share_bps(amt, bal) / 100, 2),
                'escape_rate': r.get('escape_rate'), 'escapes': r.get('escapes'), 'timeouts': r.get('timeouts'),
                'method': r.get('method'), 'tx': None, 'block': None, 'executed_at': None, 'explorer': None,
                'note': None}

    def _balance_from(self, r):
        if r.get('balance_wei') is not None:
            self.balance, self.balance_t, self.balance_block = int(r['balance_wei']), r['t'], r.get('block')
        if r.get('total_supply_wei'):
            self.supply = int(r['total_supply_wei'])

    def apply(self, r):
        """One journal record (also used live, so a restart rebuilds exactly this state)."""
        ev = r.get('ev')
        if self.since is None and 't' in r:
            self.since = r['t']
        if ev == 'course':
            self.seen.add(r['key'])
            self.escapes += int(r['escapes'])
            self.timeouts += int(r['timeouts'])
            self.attempts += 1
            if r.get('window') is not None and int(r['window']) not in self.closed:
                w = self.windows.setdefault(int(r['window']), {'escapes': 0, 'timeouts': 0, 'attempts': 0})
                w['escapes'] += int(r['escapes'])
                w['timeouts'] += int(r['timeouts'])
                w['attempts'] += 1
        elif ev == 'window':
            start = int(r['start_t'])
            self.closed.add(start)
            self.windows.pop(start, None)
            pending = bool(r.get('pending'))
            self.last_window = {k: r.get(k) for k in ('start', 'end', 'escapes', 'timeouts', 'attempts',
                                                      'escape_rate', 'late', 'note')}
            self.last_window['burn'] = {'state': 'due' if pending else 'none', 'note': None if pending else r.get('note'),
                                        'label': None, 'simulated': None, 'amount': None, 'amount_wei': None,
                                        'tx': None, 'block': None, 'method': None, 'explorer': None}
            self.due = ({'start': r['start'], 'start_t': start, 'escapes': int(r['escapes']),
                         'timeouts': int(r['timeouts']), 'escape_rate': r.get('escape_rate')} if pending else None)
            self._balance_from(r)
        elif ev in ('burn_expired', 'window_noburn'):
            if self.due is not None and r.get('window') == self.due['start_t']:
                self._lw_burn(self.due['start_t'], state='expired' if ev == 'burn_expired' else 'none',
                              note=r.get('public'), label=EXPIRED_LABEL if ev == 'burn_expired' else None)
                self.due = None
        elif ev == 'burn':                         # DRY: a simulated burn for the hour
            w = int(r['window'])
            e = self._entry(r, 'simulated', SIMULATED_LABEL, True)
            self.recent.append(e)
            self.n_sim += 1
            self.sim_burned += int(r['amount_wei'])
            self.failures = 0
            if self.due is not None and w == self.due['start_t']:
                self.due = None
            self._lw_burn(w, **{k: e[k] for k in ('state', 'label', 'simulated', 'amount', 'amount_wei', 'balance',
                                                  'share_pct', 'method', 'tx', 'block', 'at', 'explorer')})
            self._balance_from(r)
        elif ev == 'booking':
            self._booking(r)
        elif ev == 'executed':
            self._executed(r)
        elif ev == 'booking_expired':
            w = int(r['window'])
            b = self.bookings.get(w)
            if b and b['state'] == 'booked':
                b['state'] = 'expired'
                b['entry'].update(state='expired', label=EXPIRED_LABEL, note=r.get('public') or 'not executed in time')
                self._lw_burn(w, state='expired', label=EXPIRED_LABEL, note=r.get('public') or 'not executed in time')
        elif ev in ('burn_failed', 'batch_failed'):
            self.failures = int(r.get('consecutive', self.failures + 1))
        elif ev == 'stop':
            self.stop_rec = r
        elif ev == 'stop_cleared':
            self.stop_rec = None
            self.failures = 0

    def replay(self, records, mode):
        for r in records:
            if r.get('mode') == mode:
                self.apply(r)

    def _booking(self, r):
        w = int(r['window'])
        e = self._entry(r, 'booked', BOOKED_LABEL, False)
        e['signable_until'] = iso(int(r['signable_until']))
        self.bookings[w] = {'state': 'booked', 'amount_wei': int(r['amount_wei']), 't': r['t'],
                            'dead_t': int(r['dead_t']), 'method': r.get('method'), 'entry': e, 'tx': None,
                            'escape_rate': r.get('escape_rate'), 'escapes': r.get('escapes'),
                            'timeouts': r.get('timeouts')}
        for k in [k for k in self.bookings if k < w - 2 * 86400 and self.bookings[k]['state'] != 'booked']:
            del self.bookings[k]
        self.n_booked += 1
        self.recent.append(e)
        if self.due is not None and w == self.due['start_t']:
            self.due = None
        self._lw_burn(w, **{k: e[k] for k in ('state', 'label', 'simulated', 'amount', 'amount_wei', 'balance',
                                              'share_pct', 'method', 'tx', 'block', 'at', 'explorer')})
        self._balance_from(r)

    def _executed(self, r):
        """An 'executed' record: the booked burn, verified on chain. Only now does it count as burned."""
        w, amt = int(r['window']), int(r['amount_wei'])
        b = self.bookings.get(w)
        self.executed_txs.add(r['tx'])
        self.n_burns += 1
        self.burned += amt
        self.failures = 0
        fields = {'state': 'burned', 'label': EXECUTED_LABEL, 'simulated': False, 'tx': r['tx'], 'block': r.get('block'),
                  'executed_at': iso(r['t']), 'explorer': EXPLORER + r['tx'], 'method': r.get('method'), 'note': None}
        if b is not None:
            b.update(state='burned', tx=r['tx'])
            b['entry'].update(fields)
        else:                                      # a booking older than the ones kept in memory: its own entry
            self.recent.append({**self._entry(r, 'burned', EXECUTED_LABEL, False), **fields})
        self._lw_burn(w, **fields, amount=token_str(amt, 18), amount_wei=str(amt))
        if r.get('total_supply_wei'):
            self.supply = int(r['total_supply_wei'])


def report_check(obj, ledger):
    """The runner's report of an executed burn, validated field by field (no chain read yet). -> {window, tx, already,
    booking, amount_wei, ...}; 'already' True when this exact tx is already counted for this window (idempotent).
    ReportRefused otherwise."""
    if not isinstance(obj, dict):
        raise ReportRefused('the report is not a JSON object')
    extra = set(obj) - REPORT_KEYS
    if extra:
        raise ReportRefused(f'unknown field(s): {", ".join(sorted(extra))[:120]}')
    for k in ('window', 'tx'):
        if k not in obj:
            raise ReportRefused(f'missing field: {k}')
    if 'amount' not in obj and 'amount_wei' not in obj:
        raise ReportRefused('missing field: amount (LABRAT, a decimal string) or amount_wei')
    if not isinstance(obj['window'], str) or not WINDOW_ID_RE.match(obj['window']):
        raise ReportRefused('window must be the booked UTC hour, like 2026-09-26T14:00:00Z')
    if not isinstance(obj['tx'], str) or not TX_RE.match(obj['tx']):
        raise ReportRefused('tx must be a transaction hash: 0x and 64 lowercase hex characters')
    amount = None
    if obj.get('amount') is not None:
        if not isinstance(obj['amount'], str) or not DEC_RE.match(obj['amount']):
            raise ReportRefused('amount must be a plain decimal string (LABRAT)')
        amount = parse_eth(obj['amount'])
    if obj.get('amount_wei') is not None:
        if not isinstance(obj['amount_wei'], str) or not WEI_RE.match(obj['amount_wei']):
            raise ReportRefused('amount_wei must be a string of digits')
        if amount is not None and int(obj['amount_wei']) != amount:
            raise ReportRefused('amount and amount_wei disagree')
        amount = int(obj['amount_wei'])
    if obj.get('method') is not None and obj['method'] not in METHODS:
        raise ReportRefused('method must be burn or dead (or left out)')
    if obj.get('signed_at') is not None and (not isinstance(obj['signed_at'], str) or not ISO_RE.match(obj['signed_at'])):
        raise ReportRefused('signed_at must be a UTC time like 2026-09-26T14:00:31Z')
    w = _iso_ts(obj['window'])
    b = ledger.bookings.get(w)
    if b is None:
        raise ReportRefused('no burn was booked for that window', 404)
    tx = obj['tx']
    if b['state'] == 'burned':
        if b.get('tx') == tx:
            return {'window': w, 'tx': tx, 'already': True, 'booking': b}
        raise ReportRefused('that window already has its executed burn', 409)
    if tx in ledger.executed_txs:
        raise ReportRefused('that transaction is already counted for another window', 409)
    if amount != b['amount_wei']:
        raise ReportRefused(f"amount must be the booked amount ({token_str(b['amount_wei'], 18)} LABRAT)")
    return {'window': w, 'tx': tx, 'already': False, 'booking': b, 'amount_wei': b['amount_wei'],
            'method': obj.get('method'), 'signed_at': obj.get('signed_at')}


# ---------------------------------------------------------------------------------------------------- executors
def due_meta(ctx):
    return {'window': ctx['window'], 'escapes': int(ctx['escapes']), 'timeouts': int(ctx['timeouts']),
            'escape_rate': ctx['escape_rate']}


def _simulate(sim, amount, ctx):
    """The shared DRY / booking simulation: the exact burn from the wallet, its gas, and whether the wallet's ETH covers
    that gas. -> (the journal fields, None) or (None, error)."""
    s = sim.burn(ctx['method'], amount)
    if not s['ok']:
        return None, s['error']
    gp = ctx['gas_price']
    eth = sim.eth_balance()
    need = int(s['gas'] * GAS_MULT) * gp * FEE_MULT
    return {'amount_wei': int(amount), **due_meta(ctx), 'method': ctx['method'], 'balance_wei': int(ctx['balance_wei']),
            'share_bps': share_bps(amount, ctx['balance_wei']), 'total_supply_wei': ctx.get('total_supply_wei'),
            'gas': s['gas'], 'gas_price_wei': gp, 'gas_cost_wei': s['gas'] * gp, 'block': ctx['block'],
            'wallet_eth_wei': eth, 'eth_covers_gas': eth >= need, 'eth_call': s['call']}, None


class DryExecutor:
    """Simulates; journals what a burn would have done, labelled simulated. Never signs, never sends."""
    mode = 'DRY'

    def __init__(self, sim, cfg):
        self.sim, self.cfg = sim, cfg

    def burn(self, amount, ctx, book):
        info, err = _simulate(self.sim, amount, ctx)
        if err:
            return {'ok': False, 'error': err}
        book('burn', {**info, 'simulated': True})
        return {'ok': True}


class BookingExecutor:
    """LIVE BOOKINGS: books the hour's burn for the buy rig instead of doing it. Never signs, never sends (its RPC is
    a ReadRpc). The burn is simulated first exactly as DRY does, so a burn that could not go through is never booked."""
    mode = 'LIVE'

    def __init__(self, sim, cfg):
        if isinstance(sim.rpc, buyback.LiveRpc) or sim.wallet != WALLET:
            raise LiveRefused("bookings simulate from the rat's wallet over a read-only RPC")
        self.sim, self.cfg = sim, cfg

    def burn(self, amount, ctx, book):
        info, err = _simulate(self.sim, amount, ctx)
        if err:
            return {'ok': False, 'error': err}
        w = int(ctx['window'])
        book('booking', {**info, 'state': 'booked', 'simulated': False, 'signable_until': w + SIGN_UNTIL_S,
                         'dead_t': w + BOOKING_DEAD_S,
                         'executor': 'the buy rig (live/buyrig_runner.py --burn-status-url): one transaction through '
                                     'its LIVE journal, never concurrent with a buy'})
        return {'ok': True}


# ---------------------------------------------------------------------------------------------------- the engine
class Engine:
    def __init__(self, cfg, mode, journal, rpc, executor, sim, accept_test=False, clock=time.time, relay_url=RELAY_URL,
                 live_bookings=False):
        self.cfg, self.mode, self.journal, self.rpc, self.executor, self.sim = cfg, mode, journal, rpc, executor, sim
        self.clock = clock
        self.live_bookings = bool(live_bookings)
        if isinstance(rpc, buyback.LiveRpc) or not isinstance(rpc, ReadRpc):
            raise LiveRefused('the burn engine reads only: a ReadRpc, never a LiveRpc')
        if self.live_bookings and (mode != 'LIVE' or not isinstance(executor, BookingExecutor)):
            raise LiveRefused('live bookings need mode LIVE and the BookingExecutor')
        if not self.live_bookings and mode != 'DRY':
            raise LiveRefused('without live bookings the engine is DRY')
        self.lock = threading.RLock()
        self.tick_lock = threading.Lock()
        self.ledger = Ledger(cfg)
        recs = journal.records()
        if mode == 'LIVE' and journal.bad_lines:
            raise LiveRefused(f'{journal.path} has {journal.bad_lines} unreadable line(s): a damaged record could hide '
                              f'a real burn; an operator must repair it by hand first')
        self.ledger.replay(recs, mode)
        self.counter = MazeCounter(cfg.channel, accept_test, seen=self.ledger.seen)
        self.accept_test = bool(accept_test)
        self.started = clock()
        if self.ledger.since is None:
            self.ledger.since = self.started
        self.win_start = window_start(self.started, cfg.window_s)
        self.retry_at = 0.0
        self.skip_public = None
        self.stopped = None
        self.stopped_public = None
        self.failures = 0
        self.method = cfg.method if cfg.method in METHODS else None     # settled by the first token check
        self.balance_read_at = 0.0
        if mode == 'LIVE':
            self.failures = self.ledger.failures
            if self.ledger.stop_rec:
                self.stopped = self.ledger.stop_rec.get('why') or 'stopped'
                self.stopped_public = self.ledger.stop_rec.get('public') or 'stopped'
        self.note = 'starting'
        self.relay = {'url': relay_url, 'connected': False}
        self.changed = True
        cfg_rec = {f.name: getattr(cfg, f.name) for f in fields(cfg)}
        self.journal.append({'mode': mode, 'ev': 'start', 'relay': relay_url, 'channel': cfg.channel,
                             'accept_test_streams': bool(accept_test), 'config': cfg_rec, 'rule': BURN_RULE,
                             'burn_bps_hard': BURN_BPS_HARD, 'wallet': "the rat's wallet (buyback.BUYBACK_WALLET)",
                             'live_bookings': self.live_bookings,
                             'resumed': {'escapes': self.ledger.escapes, 'timeouts': self.ledger.timeouts,
                                         'simulated_burns': self.ledger.n_sim, 'burns': self.ledger.n_burns,
                                         'open_windows': sorted(self.ledger.windows),
                                         'due': self.ledger.due['start'] if self.ledger.due else None}})

    # ---- the relay side (the listener thread)
    def on_relay_connected(self, up):
        with self.lock:
            self.relay['connected'] = bool(up)
            if not up:
                self.counter.inner.live = False
            self.changed = True

    def on_relay_text(self, text):
        with self.lock:
            for ev in self.counter.on_text(text):
                self._on_event(ev)
            self.changed = True

    def on_relay_bytes(self, data):
        with self.lock:
            self.counter.on_bytes(data)

    def _on_event(self, ev):
        kind = ev['ev']
        if kind == 'episode':
            w = max(window_start(self.clock(), self.cfg.window_s), self.win_start)
            rec = self.journal.append({'mode': self.mode, 'ev': 'course', **{k: ev[k] for k in (
                'key', 'run', 'task', 'started', 'n', 'via', 'test', 'fell')}, 'escapes': int(ev['w_hits']),
                'timeouts': int(ev['w_misses']), 'channel': self.cfg.channel, 'window': w, 'window_at': iso(w)})
            self.ledger.apply(rec)
            c = self.ledger.win_counts(w)
            log(f"course {ev['n']} (run {ev['run']}): {ev['w_hits']} escaped, {ev['w_misses']} timed out; this hour "
                f"{c['escapes']} escapes / {c['timeouts']} timeouts")
        elif kind in ('gap', 'rejected'):
            self.journal.append({'mode': self.mode, **ev})
            log(f"{kind}: {ev.get('why') or ev.get('missed')}")
        elif kind == 'session':
            if not ev['countable']:
                what = f"not counted ({ev['why']})"
            elif self.counter.live:
                what = 'live: counting escapes'
            else:
                what = 'the relay says it is not live (a run that ended): nothing to count'
            log(f"training session {ev['run']} ({ev['task']}, channel {self.cfg.channel}): {what}")

    # ---- booking (every burn / booking goes through here)
    def _book(self, kind, info):
        with self.lock:
            rec = self.journal.append({'mode': self.mode, 'ev': kind, **info})
            self.ledger.apply(rec)
            self.changed = True
        if kind == 'burn':
            log(f"simulated burn for the hour {iso(info['window'])}: {token_str(info['amount_wei'], 18)} LABRAT "
                f"({pct_str(info['share_bps'] or 0)}% of {token_str(info['balance_wei'])}) via {info['method']} "
                f"(gas {info['gas']}; the wallet's ETH {'covers' if info['eth_covers_gas'] else 'does NOT cover'} it)")
        elif kind == 'booking':
            log(f"LIVE booking for the hour {iso(info['window'])}: {token_str(info['amount_wei'], 18)} LABRAT via "
                f"{info['method']}, for the buy rig to execute until {iso(info['signable_until'])} (simulated: gas "
                f"{info['gas']}; the wallet's ETH {'covers' if info['eth_covers_gas'] else 'does NOT cover'} it)")

    def add_report(self, obj):
        """LIVE BOOKINGS: the runner's report of an executed burn. Validated (report_check), VERIFIED ON CHAIN
        (verify_burn, read-only, outside the engine lock), then journalled as 'executed': only now does it count as
        burned. -> the journal record ({'already': True, ...} for a repeated report of the same transaction).
        ReportRefused otherwise (503: not on chain yet, report again). A DRY engine takes no report (409)."""
        if not self.live_bookings:
            raise ReportRefused('this engine is DRY: its burns are simulated, and it takes no execution report', 409)
        with self.lock:
            pre = report_check(obj, self.ledger)
        if pre['already']:
            return {'already': True, 'window': pre['window'], 'tx': pre['tx']}
        ver = verify_burn(self.rpc, pre['tx'], pre['amount_wei'], pre['booking']['t'])
        supply = None
        try:
            supply = self.sim.total_supply()
        except (RpcError, RuntimeError):
            pass
        with self.lock:
            pre = report_check(obj, self.ledger)          # again: another report may have won meanwhile
            if pre['already']:
                return {'already': True, 'window': pre['window'], 'tx': pre['tx']}
            b = pre['booking']
            rec = {'mode': self.mode, 'ev': 'executed', 'window': pre['window'], 'amount_wei': pre['amount_wei'],
                   'tx': pre['tx'], **ver, 'escapes': b['escapes'], 'timeouts': b['timeouts'],
                   'escape_rate': b['escape_rate'], 'simulated': False, 'signed_at': pre.get('signed_at'),
                   'reported_method': pre.get('method'), 'total_supply_wei': supply,
                   'verified': "receipt status 1; from the rat's wallet; to the token; no ETH; calldata = a burn of "
                               'the booked amount; LABRAT Transfer from the wallet to the zero / dead address of that '
                               'amount; mined after the booking'}
            r = self.journal.append(rec)
            self.ledger.apply(r)
            self.changed = True
        log(f"LIVE: the burn for the hour {iso(pre['window'])} verified on chain: {token_str(pre['amount_wei'], 18)} "
            f"LABRAT via {ver['method']}, block {ver['block']}, {pre['tx']}")
        return r

    def _expire_bookings(self, now):
        with self.lock:
            for w, b in sorted(self.ledger.bookings.items()):
                if b['state'] == 'booked' and now >= b['dead_t']:
                    r = self.journal.append({'mode': self.mode, 'ev': 'booking_expired', 'window': w,
                                             'amount_wei': b['amount_wei'],
                                             'why': 'the buy rig did not execute it in time', 'public': 'not executed in time'})
                    self.ledger.apply(r)
                    self.changed = True
                    log(f'LIVE: the booking for the hour {iso(w)} expired unexecuted')

    def clear_stop(self, sid):
        rec = self.ledger.stop_rec
        if not sid or not rec or rec.get('id') != sid:
            return False
        self.journal.append({'mode': self.mode, 'ev': 'stop_cleared', 'id': sid, 'cleared': rec.get('why'),
                             'why': f'operator: {CLEAR_STOP_ENV}'})
        self.ledger.stop_rec = None
        self.ledger.failures = 0
        self.failures = 0
        self.stopped = self.stopped_public = None
        self.changed = True
        log(f'the stop {sid} was cleared by the operator')
        return True

    def _stop(self, why, public):
        with self.lock:
            self.stopped, self.stopped_public = why, public
            self.skip_public = None
            self.changed = True
        sid = hashlib.sha256(f'{self.clock()}|{why}'.encode()).hexdigest()[:8]
        try:
            r = self.journal.append({'mode': self.mode, 'ev': 'stop', 'id': sid, 'why': why, 'public': public})
            self.ledger.stop_rec = r
        except OSError as e:
            log(f'the stop could not be journalled ({type(e).__name__})')
        log(f'STOPPED: {why}' + (f' (stop id {sid}; after checking, {CLEAR_STOP_ENV}={sid} clears it)'
                                 if self.live_bookings else ''))

    def _skip(self, why, retry_s, public, ev='burn_skip', **extra):
        self.note = why
        self.retry_at = self.clock() + retry_s
        self.skip_public = public
        self.changed = True
        self.journal.append({'mode': self.mode, 'ev': ev, 'why': why, **extra})
        log(f'{ev}: {why}')

    def _noburn(self, due, why, public):
        with self.lock:
            r = self.journal.append({'mode': self.mode, 'ev': 'window_noburn', 'window': due['start_t'], 'why': why,
                                     'public': public})
            self.ledger.apply(r)
            self.note = why
            self.skip_public = None
            self.retry_at = 0.0
            self.changed = True
        log(f"no burn for the hour {due['start']}: {why}")

    def _failed(self, what, err):
        self.failures += 1
        self.journal.append({'mode': self.mode, 'ev': f'{what}_failed', 'error': err, 'consecutive': self.failures})
        log(f'{what} failed: {err}')
        if self.failures >= self.cfg.max_failures:
            self._stop(f'{self.failures} failed burn attempts in a row (last: {err})', 'repeated failures')
        else:
            self.retry_at = self.clock() + 120
            self.skip_public = 'retrying after a failed attempt'
            self.changed = True

    # ---- the wallet's balance (for the status; the hour's burn reads it again when it is sized)
    def refresh_balance(self, now, force=False):
        if not force and now - self.balance_read_at < BALANCE_EVERY_S:
            return
        self.balance_read_at = now
        try:
            bal = self.sim.token_balance()
        except (RpcError, RuntimeError, SendRefused):
            return
        with self.lock:
            if bal != self.ledger.balance:
                self.changed = True
            self.ledger.balance, self.ledger.balance_t = bal, now

    # ---- the hour windows (the main thread)
    def _roll(self, now):
        c, L = self.cfg, self.ledger
        cur = window_start(now, c.window_s)
        with self.lock:
            ended = {s for s in L.windows if s < cur}
            if self.win_start < cur:
                ended.add(self.win_start)
            for s in sorted(ended - L.closed):
                self._close(s, now, late=s + c.window_s < cur)
            if cur > self.win_start:
                self.win_start = cur

    def _close(self, s, now, late):
        c, L = self.cfg, self.ledger
        if L.due is not None:              # the previous hour's burn never happened: the next hour replaces it
            old = L.due
            r = self.journal.append({'mode': self.mode, 'ev': 'burn_expired', 'window': old['start_t'],
                                     'why': 'the next hour closed before it was burned', 'public': 'the next hour closed first'})
            L.apply(r)
            log(f"the burn for the hour {old['start']} expired (the next hour closed first)")
        w = L.win_counts(s)
        rate = escape_rate(w['escapes'], w['timeouts'])
        projected = burn_amount(L.balance or 0, c.burn_bps, w['escapes'], w['timeouts'], c.max_burn_wei)
        pending, note = True, None
        if late:
            pending, note = False, 'closed late; the engine was not running at the hour'
        elif w['attempts'] == 0 or w['escapes'] + w['timeouts'] == 0:
            pending, note = False, 'no mazes in the hour'
        elif w['escapes'] == 0:
            pending, note = False, 'no escapes in the hour'
        rec = {'mode': self.mode, 'ev': 'window', 'start': iso(s), 'start_t': s, 'end': iso(s + c.window_s),
               'escapes': w['escapes'], 'timeouts': w['timeouts'], 'attempts': w['attempts'],
               'escape_rate': rate_str(rate), 'burn_bps': c.burn_bps, 'balance_wei': L.balance,
               'projected_wei': projected, 'pending': pending, 'late': late, 'note': note, 'rule': BURN_RULE}
        L.apply(self.journal.append(rec))
        self.retry_at = 0.0
        self.skip_public = None
        self.changed = True
        rate_txt = 'none' if rate is None else f'{rate * 100:.1f}%'
        log(f"hour {iso(s)} closed: {w['escapes']} escapes, {w['timeouts']} timeouts, escape rate {rate_txt} -> "
            + (f'burn due (about {token_str(projected, 18)} LABRAT at the last balance read)' if pending
               else f'no burn ({note})'))

    def tick(self):
        if not self.tick_lock.acquire(blocking=False):
            return
        try:
            now = self.clock()
            try:
                if self.live_bookings:
                    self._expire_bookings(now)
                self._roll(now)
            except OSError as e:
                return self._stop(f'the journal could not be written ({type(e).__name__})', 'the burn journal failed')
            with self.lock:
                due = self.ledger.due
                if self.stopped or now < self.retry_at:
                    return
                if due is None:
                    self.note = 'waiting for the hour'
                else:
                    due = dict(due)
            if due is None:
                self.refresh_balance(now)          # a chain read: outside the lock, so the relay thread never waits on it
                return
            self._batch(now, due)
        finally:
            self.tick_lock.release()

    def _batch(self, now, due):
        try:
            return self._batch_inner(now, due)
        except OSError as e:
            return self._stop(f'the journal could not be written ({type(e).__name__})', 'the burn journal failed')

    def _batch_inner(self, now, due):
        c = self.cfg
        try:
            chk = check_token(self.rpc, c.method)
        except (RpcError, RuntimeError, KeyError, ValueError) as e:
            return self._skip(f'chain read failed ({type(e).__name__}: {e}); retrying', 60, 'chain read failed; retrying')
        self.journal.append({'mode': self.mode, 'ev': 'check', **{k: chk[k] for k in (
            'ok', 'method', 'has_burn', 'code_bytes', 'block', 'problems')}})
        if chk['stop']:
            return self._stop('; '.join(chk['problems']), chk['public'])
        self.method = chk['method']
        try:                                             # the reads the burn is sized on: a failure is retried, not counted
            gp = self.sim.gas_price()
            balance = self.sim.token_balance()          # the balance the hour's burn is sized on: read right now
            supply = self.sim.total_supply()
        except (RpcError, RuntimeError, KeyError, ValueError) as e:
            return self._skip(f'chain read failed ({type(e).__name__}: {e}); retrying', 60, 'chain read failed; retrying')
        if gp > c.max_gas_price_wei:
            return self._skip(f'gas price {gp / 1e9:.3f} gwei is over the {c.max_gas_price_wei / 1e9:g} gwei cap',
                              300, 'gas price over the cap')
        with self.lock:
            self.ledger.balance, self.ledger.balance_t, self.ledger.balance_block = balance, now, chk['block']
            self.ledger.supply = supply
            self.balance_read_at = now
            self.changed = True
        amount = burn_amount(balance, c.burn_bps, due['escapes'], due['timeouts'], c.max_burn_wei)
        if balance <= 0:
            return self._noburn(due, 'the wallet holds no LABRAT', 'nothing to burn')
        if amount < c.min_burn_wei:
            return self._noburn(due, f'the burn would be under {token_str(c.min_burn_wei, 18)} LABRAT '
                                     f'({token_str(amount, 18)})', 'under the minimum burn')
        ctx = {'block': chk['block'], 'timestamp': chk['timestamp'], 'gas_price': gp, 'window': due['start_t'],
               'escapes': due['escapes'], 'timeouts': due['timeouts'], 'escape_rate': due['escape_rate'],
               'balance_wei': balance, 'total_supply_wei': supply, 'method': chk['method']}
        try:
            r = self.executor.burn(amount, ctx, self._book)
        except LiveRefused as e:
            return self._skip(f'held back: {e}', 60, 'held back; retrying')
        except (RpcError, RuntimeError, KeyError, ValueError) as e:
            return self._failed('batch', f'{type(e).__name__}: {e}')
        if r.get('skip'):
            if r.get('final'):
                return self._noburn(due, r['error'], r.get('public') or 'no burn this hour')
            return self._skip(r['error'], 120, r.get('public') or 'waiting; retrying')
        if not r['ok']:
            return self._failed('burn', r['error'])
        self.failures = 0
        self.skip_public = None
        self.retry_at = 0.0
        self.note = 'booked for the buy rig' if self.live_bookings else 'simulated a burn'

    # ---- status
    def next_burn(self, now):
        L, c = self.ledger, self.cfg
        if self.stopped:
            return None, 'stopped'
        if L.due is not None:
            if now < self.retry_at:
                return int(self.retry_at - now) + 1, self.skip_public or 'retrying'
            return 0, 'due'
        end = max(self.win_start, window_start(now, c.window_s)) + c.window_s
        return max(0, int(end - now)), 'waiting for the hour'

    def rule_text(self):
        c = self.cfg
        method = METHOD_TEXT.get(self.method or 'burn')
        return (f'Once an hour, on the hour (UTC), the engine looks at the hour the rat just played in Rat Maze in the '
                f'live view (the newest saved training checkpoint, playing courses of mazes in its own simulation): the '
                f'mazes it escaped and the mazes it ran out of time in. The escape rate is escapes / (escapes + '
                f'timeouts), and that hour\'s burn is {pct_str(c.burn_bps)}% of the $LABRAT the rat\'s wallet holds at '
                f'the hour\'s close x the escape rate, rounded down to a whole LABRAT; 5% of the balance is the ceiling '
                f'in code, whatever the settings say. An hour with no mazes, no escapes or a burn under '
                f'{token_str(c.min_burn_wei, 18)} LABRAT burns nothing. The burn is {method}. The wallet is '
                f'{FUNDING}. The code sets this rule; the rat\'s escapes only trigger it. The rat does not understand '
                f'money.' + ('' if self.mode == 'LIVE' else ' Every burn shown is simulated (eth_call), not executed.'))

    def public_status(self):
        """What the website may show: no wallet address, no transaction data beyond verified hashes."""
        c = self.cfg
        with self.lock:
            L = self.ledger
            now = self.clock()
            due, due_why = self.next_burn(now)
            s = self.counter.session
            public_relay = self.relay['url'] == RELAY_URL
            test_stream = bool(s and s['test'])
            cur = max(self.win_start, window_start(now, c.window_s))
            w = L.win_counts(cur)
            rate = escape_rate(w['escapes'], w['timeouts'])
            bal = L.balance
            projected = burn_amount(bal or 0, c.burn_bps, w['escapes'], w['timeouts'], c.max_burn_wei)
            outstanding = [copy.deepcopy(b['entry']) for b in L.outstanding()]
            return {
                'mode': self.mode,
                'label': LIVE_BOOKINGS_LABEL if self.live_bookings else DRY_LABEL,
                'simulated': not self.live_bookings,
                'rule': self.rule_text(),
                'burn_pct': pct_str(c.burn_bps),
                'burn_pct_max': pct_str(BURN_BPS_HARD),
                'task': TASK,
                'channel': c.channel,
                'method': self.method,
                'method_text': METHOD_TEXT.get(self.method) if self.method else None,
                'this_hour': {'start': iso(cur), 'end': iso(cur + c.window_s), 'escapes': w['escapes'],
                              'timeouts': w['timeouts'], 'attempts': w['attempts'], 'escape_rate': rate_str(rate),
                              'wallet_balance': None if bal is None else token_str(bal),
                              'wallet_balance_at': iso(L.balance_t) if L.balance_t else None,
                              'projected_burn': token_str(projected, 18),
                              'projected_pct': None if not bal else round((share_bps(projected, bal) or 0) / 100, 2)},
                'last_hour': copy.deepcopy(L.last_window),
                'totals': {'burned': token_str(L.burned, 18), 'burns': L.n_burns,
                           'simulated_burned': token_str(L.sim_burned, 18), 'simulated_burns': L.n_sim,
                           'booked': len(outstanding), 'escapes': L.escapes, 'timeouts': L.timeouts,
                           'courses': L.attempts, 'total_supply': None if L.supply is None else token_str(L.supply),
                           'since': iso(L.since) if L.since else None},
                'recent': [copy.deepcopy(e) for e in L.recent][::-1],
                'bookings': outstanding if self.live_bookings else [],
                'next_burn_in_s': due,
                'next_burn_note': due_why,
                'next_burn_at': None if due is None else iso(now + due),
                'this_course': {**self.counter.course, 'note': 'unconfirmed until the course ends'},
                'relay': {'connected': self.relay['connected'], 'live': self.counter.live,
                          'run': s['run'] if s else None, 'task': s['task'] if s else None, 'channel': c.channel,
                          'counting': bool(s and s['countable'] and self.counter.live), 'test_stream': test_stream},
                'source': {'public_relay': public_relay, 'accept_test_streams': self.accept_test,
                           'test': (not public_relay) or self.accept_test or test_stream},
                'caps': {'burn_pct': pct_str(c.burn_bps), 'burn_pct_hard': pct_str(BURN_BPS_HARD),
                         'burns_per_hour': 1, 'min_burn': token_str(c.min_burn_wei, 18),
                         'max_burn': None if c.max_burn_wei is None else token_str(c.max_burn_wei, 18),
                         'max_gas_gwei': c.max_gas_price_wei / 1e9},
                'explorer': EXPLORER,
                'stopped': self.stopped_public,
                'updated': iso(now),
            }


# ---------------------------------------------------------------------------------------------------- the relay
class RelayListener:
    """The relay's public /live stream, on its own thread, with reconnects (1 s .. 30 s backoff)."""

    def __init__(self, url, origin, engine, stop):
        self.url, self.origin, self.engine, self.stop = url, origin, engine, stop
        self.thread = threading.Thread(target=self._run, name='relay-listener', daemon=True)
        self.connects = 0

    def start(self):
        self.thread.start()
        return self

    def _run(self):
        from websockets.sync.client import connect
        backoff, last_err = 1.0, None
        while not self.stop.is_set():
            try:
                ws = connect(self.url, origin=self.origin, open_timeout=10, close_timeout=3, compression=None,
                             max_size=2 ** 20, ping_interval=20, ping_timeout=20, user_agent_header='labrat-burn/1')
            except Exception as e:
                err = f'{type(e).__name__}: {e}'
                if err != last_err:
                    log(f'cannot reach the relay ({err}); retrying')
                last_err = err
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            self.connects += 1
            if backoff <= 2.0:
                log(f'listening to {self.url} (channel {self.engine.cfg.channel})')
            self.engine.on_relay_connected(True)
            last_err = None
            t_up = time.monotonic()
            try:
                while not self.stop.is_set():
                    try:
                        msg = ws.recv(timeout=1.0)
                    except TimeoutError:
                        continue
                    if isinstance(msg, str):
                        self.engine.on_relay_text(msg)
                    else:
                        self.engine.on_relay_bytes(msg)
            except Exception as e:
                if not self.stop.is_set():
                    log(f'relay connection lost ({type(e).__name__}); reconnecting')
            finally:
                self.engine.on_relay_connected(False)
                try:
                    ws.close()
                except Exception:
                    pass
            wait, backoff = buyback.reconnect_wait(backoff, time.monotonic() - t_up)
            self.stop.wait(wait)


# ---------------------------------------------------------------------------------------------------- status HTTP
def serve_status(engine, host, port, rig_token=None):
    """GET /status (public), GET /bookings (the outstanding bookings: what the buy rig executes), GET /healthz. With
    rig_token (live bookings only, >= RIG_TOKEN_MIN characters): POST /burn_report for the buy rig's runner
    (Authorization: Bearer <token>, a JSON body of at most REPORT_BODY_MAX bytes, validated by report_check and
    verified on chain). Browsers cannot call it: it needs an Authorization header and no CORS preflight is answered."""
    rig_token = rig_token if (rig_token and len(rig_token) >= RIG_TOKEN_MIN and engine.live_bookings) else None

    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            body = json.dumps(obj, separators=(',', ':')).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split('?', 1)[0]
            if path in ('/status', '/'):
                self._send(200, engine.public_status())
            elif path == '/bookings':
                st = engine.public_status()
                self._send(200, {'mode': st['mode'], 'simulated': st['simulated'], 'channel': st['channel'],
                                 'method': st['method'], 'bookings': st['bookings'], 'updated': st['updated']})
            elif path == '/healthz':
                self._send(200, {'ok': True})
            else:
                self._send(404, {'error': 'not found'})

        def do_POST(self):
            path = self.path.split('?', 1)[0]
            if path != '/burn_report' or not rig_token:
                self.close_connection = True
                return self._send(404, {'error': 'not found'})
            got = _bearer(self.headers.get('Authorization'))
            if got is None or not _same_secret(got, rig_token):
                self.close_connection = True
                return self._send(401, {'error': 'unauthorized'})
            try:
                n = int(self.headers.get('Content-Length') or '-1')
            except ValueError:
                n = -1
            if not 0 < n <= REPORT_BODY_MAX:
                self.close_connection = True
                return self._send(413 if n > REPORT_BODY_MAX else 411,
                                  {'error': f'a JSON body of 1..{REPORT_BODY_MAX} bytes with a Content-Length'})
            try:
                obj = json.loads(self.rfile.read(n).decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                return self._send(400, {'error': 'the body is not JSON'})
            try:
                rec = engine.add_report(obj)
            except ReportRefused as e:
                return self._send(e.status, {'error': str(e)[:300]})
            except OSError as e:
                return self._send(503, {'error': f'the journal could not be written ({type(e).__name__})'})
            except Exception as e:
                log(f'POST /burn_report failed ({type(e).__name__})')
                return self._send(500, {'error': f'the report could not be handled ({type(e).__name__})'})
            return self._send(200, {'ok': True, 'window': iso(int(rec['window'])), 'tx': rec['tx'],
                                    'already': bool(rec.get('already')), 'label': EXECUTED_LABEL})

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name='status-http', daemon=True).start()
    return srv


# ---------------------------------------------------------------------------------------------------- main
def build_config(a):
    kw = {}
    if a.burn_pct is not None:
        kw['burn_bps'] = parse_pct(a.burn_pct)
    if a.min_burn_labrat is not None:
        kw['min_burn_wei'] = parse_eth(a.min_burn_labrat)
    if a.max_burn_labrat is not None and str(a.max_burn_labrat).strip().lower() not in ('unset', 'none', ''):
        kw['max_burn_wei'] = parse_eth(a.max_burn_labrat)
    if a.max_gas_gwei is not None:
        kw['max_gas_price_wei'] = int(Decimal(str(a.max_gas_gwei)) * 10 ** 9)
    for flag in ('window_s', 'channel', 'method', 'max_failures'):
        v = getattr(a, flag)
        if v is not None:
            kw[flag] = v
    return Config(**kw).validate()


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description='$LABRAT burns: one burn an hour, sized by the Rat Maze escape rate; DRY by '
                                             'default (simulated, never sent).')
    ap.add_argument('--live-bookings', action='store_true',
                    help=f"LIVE BOOKINGS: book each hour's burn as a real burn for the buy rig; only together with the "
                         f"process environment's {LIVE_BOOKINGS_ENV}=1 (see the docstring)")
    ap.add_argument('--check', action='store_true', help='one read-only check: the token, the wallet, a simulated burn')
    ap.add_argument('--relay', default=RELAY_URL)
    ap.add_argument('--origin', default=ORIGIN)
    ap.add_argument('--channel', default=None, help=f'the relay channel counted (default {CHANNEL}; "{DEFAULT_CHANNEL}" '
                                                    f'= the untagged default channel, DRY tests only)')
    ap.add_argument('--accept-test-streams', action='store_true', help='DRY only: count a TEST stream (local tests)')
    ap.add_argument('--journal-dir', default=None, help=f'default {DEFAULT_JOURNAL_DIR}')
    ap.add_argument('--status-port', type=int, default=0, help='serve the public status JSON (0 = off)')
    ap.add_argument('--status-host', default='127.0.0.1')
    ap.add_argument('--duration', type=float, default=0, help='stop after this many seconds (0 = run until Ctrl+C)')
    ap.add_argument('--tick', type=float, default=1.0, help=argparse.SUPPRESS)
    ap.add_argument('--window-s', type=int, default=None, help=argparse.SUPPRESS)   # tests only: a shorter "hour"
    ap.add_argument('--burn-pct', default=None,
                    help=f'the share of the wallet\'s LABRAT burned per hour at a perfect escape rate (default and '
                         f'ceiling {pct_str(BURN_BPS_HARD)})')
    ap.add_argument('--min-burn-labrat', default=None, help='an hour whose burn is under this burns nothing (default 1)')
    ap.add_argument('--max-burn-labrat', default=None, help='an optional absolute cap per burn (default none)')
    ap.add_argument('--max-gas-gwei', type=float)
    ap.add_argument('--max-failures', type=int, default=None)
    ap.add_argument('--method', choices=('auto',) + METHODS, default=None,
                    help='auto (default): burn(uint256) when the token bytecode has it, else a transfer to the dead address')
    return ap.parse_args(argv)


def one_check(cfg):
    """--check: read-only, printed locally (not journalled, not published)."""
    rpc = ReadRpc()
    sim = Sim(rpc, cfg)
    chk = check_token(rpc, cfg.method)
    out = {'check': {k: chk[k] for k in ('ok', 'method', 'has_burn', 'code_bytes', 'block', 'problems')}}
    gp = sim.gas_price()
    out['gas_price_gwei'] = gp / 1e9
    bal = sim.token_balance()
    out['wallet_labrat'] = token_str(bal)
    out['wallet_eth'] = eth_str(sim.eth_balance())
    out['total_supply'] = token_str(sim.total_supply())
    out['burn_pct'] = pct_str(cfg.burn_bps)
    out['burn_at_perfect_hour'] = token_str(burn_amount(bal, cfg.burn_bps, 1, 0, cfg.max_burn_wei), 18)
    if chk['method']:
        b = sim.burn(chk['method'], TOKEN_STEP_WEI)
        out['burn_sim'] = {'amount': '1', 'method': chk['method'],
                           **({'gas': b['gas'], 'gas_eth': eth_str(b['gas'] * gp)} if b['ok'] else {'error': b['error']})}
    return out


def bookings_gate(a, cfg, environ=None):
    """-> True when LIVE BOOKINGS is switched on (the --live-bookings flag AND BURN_LIVE_BOOKINGS=1), False when it is
    off (DRY, exactly as before). SystemExit when it is switched on with a configuration it refuses."""
    env = os.environ if environ is None else environ
    flag, on = bool(a.live_bookings), env.get(LIVE_BOOKINGS_ENV) == '1'
    if not (flag and on):
        if flag or on:
            log(f'live bookings: only half switched on (--live-bookings {flag}, {LIVE_BOOKINGS_ENV}=1 {on}): DRY')
        return False

    def no(why):
        raise SystemExit(f'live bookings refused (nothing booked): {why}')
    if cfg.window_s != HOUR_S:
        no('real burns are booked once per UTC hour (no --window-s)')
    if a.accept_test_streams:
        no('--accept-test-streams is for DRY tests only: a test stream can never trigger a real burn')
    if a.relay != RELAY_URL or a.origin != ORIGIN:
        no(f'live bookings count only the public relay {RELAY_URL} as {ORIGIN}')
    if cfg.channel != CHANNEL:
        no(f'live bookings count the "{CHANNEL}" channel only (no --channel {cfg.channel})')
    if cfg.method == 'dead':
        no('--method dead is for a token without burn(); live burns use the token\'s own burn() when it has one')
    return True


def main(argv=None):
    a = parse_args(argv)
    try:
        cfg = build_config(a)
    except (ValueError, TypeError) as e:
        raise SystemExit(f'bad config: {e}')
    if a.check:
        print(json.dumps(one_check(cfg), indent=2))
        return 0
    bookings = bookings_gate(a, cfg)
    mode = 'LIVE' if bookings else 'DRY'
    jdir = os.path.abspath(a.journal_dir or DEFAULT_JOURNAL_DIR)
    journal = Journal(os.path.join(jdir, BOOKINGS_JOURNAL if bookings else 'journal.jsonl'))
    rpc = ReadRpc()
    sim = Sim(rpc, cfg)
    executor = BookingExecutor(sim, cfg) if bookings else DryExecutor(sim, cfg)
    try:
        engine = Engine(cfg, mode, journal, rpc, executor, sim, accept_test=a.accept_test_streams, relay_url=a.relay,
                        live_bookings=bookings)
    except LiveRefused as e:
        raise SystemExit(f'refused, nothing was booked: {e}') from None
    if bookings:
        sid = os.environ.get(CLEAR_STOP_ENV, '').strip()
        if sid:
            engine.clear_stop(sid)
        if engine.stopped:
            log(f"live bookings are STOPPED: {engine.stopped} (stop id {(engine.ledger.stop_rec or {}).get('id')}; "
                f'after checking, {CLEAR_STOP_ENV}=<that id> clears it)')
    log(f'{mode}: {LIVE_BOOKINGS_LABEL if bookings else DRY_LABEL}; journal {journal.path}; channel {cfg.channel}; '
        f'{engine.ledger.escapes} escapes, {engine.ledger.n_sim} simulated and {engine.ledger.n_burns} verified burns so far')
    log(f'rule: {pct_str(cfg.burn_bps)}% of the wallet\'s LABRAT x the escape rate per hour (ceiling '
        f'{pct_str(BURN_BPS_HARD)}%), whole LABRAT, min {token_str(cfg.min_burn_wei, 18)}'
        + (f', max {token_str(cfg.max_burn_wei, 18)}' if cfg.max_burn_wei else ''))
    try:
        chk = check_token(rpc, cfg.method)
        engine.journal.append({'mode': mode, 'ev': 'check', **{k: chk[k] for k in (
            'ok', 'method', 'has_burn', 'code_bytes', 'block', 'problems')}})
        if chk['ok']:
            engine.method = chk['method']
            engine.refresh_balance(engine.clock(), force=True)
            log(f"chain: block {chk['block']}, token code {chk['code_bytes']} bytes, burn(uint256) "
                f"{'present' if chk['has_burn'] else 'absent'} -> method {chk['method']}; the wallet holds "
                f"{token_str(engine.ledger.balance or 0)} LABRAT")
        else:
            log('chain: STOP: ' + '; '.join(chk['problems']))
            engine._stop('; '.join(chk['problems']), chk['public'])
    except (RpcError, RuntimeError) as e:
        log(f'chain check failed at startup ({e}); it runs again before every burn')
    stop = threading.Event()
    listener = RelayListener(a.relay, a.origin, engine, stop).start()
    rig_token = os.environ.get(RIG_TOKEN_ENV, '').strip() if bookings else ''
    if rig_token and len(rig_token) < RIG_TOKEN_MIN:
        log(f'{RIG_TOKEN_ENV} is shorter than {RIG_TOKEN_MIN} characters: POST /burn_report stays off')
        rig_token = ''
    if bookings and not (rig_token and a.status_port):
        log(f'WARNING: live bookings without {RIG_TOKEN_ENV} and --status-port: the buy rig cannot report its executed '
            'burns, so none is verified or counted here (the rig\'s own journal still keeps them)')
    srv = serve_status(engine, a.status_host, a.status_port, rig_token or None) if a.status_port else None
    if srv:
        log(f'status on http://{a.status_host}:{a.status_port}/status (+ /bookings'
            + (', POST /burn_report for the buy rig)' if rig_token else ')'))
    status_file = StatusFile(os.path.join(jdir, 'status.json'))
    t_end = time.monotonic() + a.duration if a.duration else None
    try:
        while not stop.is_set():
            engine.tick()
            status_file.update(engine)
            if t_end and time.monotonic() >= t_end:
                break
            stop.wait(a.tick)
    except KeyboardInterrupt:
        log('stopping')
    finally:
        stop.set()
        listener.thread.join(timeout=5)
        if srv:
            srv.shutdown()
            srv.server_close()
        status_file.update(engine, force=True)
        L = engine.ledger
        engine.journal.append({'mode': mode, 'ev': 'end', 'escapes': L.escapes, 'timeouts': L.timeouts,
                               'simulated_burns': L.n_sim, 'simulated_burned_wei': L.sim_burned, 'burns': L.n_burns,
                               'burned_wei': L.burned, 'stats': dict(engine.counter.stats)})
        log(f'end: {L.escapes} escapes and {L.timeouts} timeouts counted, {L.n_sim} simulated burns '
            f'({token_str(L.sim_burned, 18)} LABRAT), {L.n_burns} verified burns ({token_str(L.burned, 18)} LABRAT)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
