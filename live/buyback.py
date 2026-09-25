"""$LABRAT rat buybacks: ONE buy an hour, sized by the rat's hit rate in that hour.

While a training run streams to lab-rat.net, the rat plays in the live view (the newest saved training checkpoint,
playing in its own simulation: live/publish_training.py). Buying at every hit would spend most of each buy on gas, so
the engine buys once an hour, on the hour (UTC): it closes the hour that just ended, counts its hits, misses and wrong
clicks, and books that hour's buy = the hourly budget x the hit rate, cut to the per-buy, hourly, daily and total caps.
    hit_rate = hits / (hits + misses + wrong)
The hourly budget is set by the owner (--hourly-budget-eth). Until it is set, every hour is still recorded and runs a
SIMULATED PREVIEW buy of a nominal amount (--preview-budget-eth, 0.001 ETH) x the hit rate, marked "preview" in the
status and the journal. Preview buys never use up a cap.

Say it this way: the rat's brain is two trained artificial neural networks, not a biological brain, and it does not
understand money. This code sets the rules; the rat's hits in the live view only trigger them.

    python live/buyback.py                               # DRY (the default): listen, count, simulate, journal
    python live/buyback.py --status-port 4750            # DRY + the public status JSON on http://127.0.0.1:4750/status
    python live/buyback.py --hourly-budget-eth 0.002     # DRY with the owner's hourly budget (no longer a preview)
    python live/buyback.py --check                       # one read-only check of the chain and a simulated buy
    python live/buyback.py --live --confirm LABRAT       # LIVE: refused (see LIVE below; not used)

WHAT COUNTS (hits, misses, wrong clicks)
  The relay's public /live stream (Origin https://lab-rat.net) forwards what live/publish_training.py sends. Each
  attempt ends with {"type":"episode","n","presses","hits","misses","fell"}. The engine books it into the hour window
  it arrives in (windows are aligned to the UTC hour):
    cursor / steering tasks: hits = clicks on the lit target, misses = clicks off it (cursor_env.CursorEnv)
    Rat Tiles (task "tiles", tiles_env.py; one attempt is one song, at most TILES_MAX_HITS notes): hits = tiles tapped,
      misses = tiles that slid past untapped, wrong = presses with no tile to tap. The episode's "misses" are the wrong
      clicks (presses = hits + misses) and its "missed" the tiles that slid past; when a publisher does not send
      "missed", the tiles that slid past are counted from its {"type":"tile","result":"miss"} events of that attempt
      (each tile id once), only for a song followed from its start: a song joined midway (the relay's state on
      connect, a publisher resync) keeps its hits in the totals but stays out of the hour's hit rate (its unknown
      misses could only raise it). An episode that carries "wrong" instead is read as misses = tiles that slid past,
      wrong = wrong clicks (presses = hits + wrong).
    lever task (only when --tasks names it): hits = clean presses, at most one per attempt.
  Only episodes of a live training hello are counted: hello.source "training", not a TEST stream (unless
  --accept-test-streams, DRY only), and the state message's last episode only while state.live is true (the relay
  keeps the last episode of an ended run; it is never counted). Each episode is keyed by run | task | hello.started | n
  and counted once: a reconnect, a re-sent state or a restart of this program (the journal holds the keys) never
  counts it twice. An episode claiming more than 4 hits (1 for the lever task, TILES_MAX_HITS for Rat Tiles), counts
  that do not add up, or anything that is not a non-negative integer is rejected and journalled. Binary frames only
  feed an unconfirmed tally of the attempt in progress: money never depends on them. Episodes the relay forwarded
  while this program was disconnected are missed (journalled as a gap when n jumps).

THE HOUR (Config below; the HARD ceilings cannot be raised from the command line)
  At the end of each hour window the engine journals a 'window' record: its hits, misses, wrong clicks, attempts and
  hit rate, the budget used (or the preview amount), and the buy it books. The buy is floor(budget x hits / (hits +
  misses + wrong)) rounded down to 0.00000001 ETH (the precision the buy rig types into pons), cut to the per-buy,
  per-hour-window, rolling-day and total caps. An hour with no attempts, no hits, or whose buy would be under min_buy
  buys nothing (recorded). The booked buy is tried at once; a passing problem (gas price over the cap, a chain read
  that failed, a failed attempt) retries until the next hour closes, which replaces it ('buy_expired'); a final one
  (gas over max_gas_share of the buy, a cap reached) drops that hour's buy ('window_nobuy'). Gas has its own
  rolling-day and total caps. An hour that ended while this program was not running is closed when it restarts,
  late: only the hour just ended can still buy. Preview buys (budget not set) are cut to the per-buy cap only and
  never use up a cap or the gas caps.

WHO BUYS (DRY)
  The owner funds a SEPARATE buyback wallet by hand (BUYBACK_WALLET, pinned below). DRY simulates every buy from it:
  eth_call and eth_estimateGas with a state override that gives it enough ETH for the buy and its gas (its real
  balance is journalled, with whether it would have covered the buy). The creator-fee claims of the launch wallet are
  no longer part of a DRY buy (the fee-claim code stays for the LIVE path, which is paused). This program holds no key
  for either wallet.

WHERE IT BUYS (pinned: token, curve, launch wallet, factory, escrow, hook, router, quoter, pool key)
  Before every buy: token.curve() and the factory record must match the pins (token, curve, creator-fee recipient =
  launch wallet, ETH pair, pons's own buyback off). Phase 0 with graduated() false -> buy(quoteIn, minOut, recipient)
  on the curve. Phase 2 with graduated() true -> the pinned Uniswap v4 pool through the pons Universal Router (the
  pinned PoolKey must hash to the pinned poolId and the factory's memeHook must be the pinned hook). Anything else
  stops the buys (hits are still counted). $LABRAT graduated at block 71688341, so today every buy goes to the pool.
  --venue curve keeps the literal "stop if the curve graduated" rule (it stops at once today); --venue pool the
  opposite. Research: live/BUYBACK_RESEARCH.md; notes: live/BUYBACK.md.

DRY (the default; the only mode that runs now)
  Computes everything and simulates each buy with eth_call from the buyback wallet (tokens out, min out, gas via
  eth_estimateGas) on the real chain. Everything goes to the append-only journal runs/buyback/journal.jsonl; the
  public status (status.json next to it, and --status-port) says "DRY - simulated, not executed". DRY never reads
  .env, never builds a signer and its RPC object can only read (eth_sendRawTransaction / eth_sendTransaction raise
  SendRefused). A journal written before the hourly rule replays: its hits and buys still count, and its pending
  per-hit amount is dropped (those hits show as "counted, bought nothing").

THE RAT ON PONS (DRY only; live/buyrig.py, live/buyrig_runner.py)
  For each simulated buy booked here (one an hour, previews included), the buy rig's runner can have the rat click
  through pons's own buy flow on the real coin page (pons builds the transaction; the rig checks it, simulates it and
  refuses to sign) and report the session: POST /pons_session on the status server, enabled only when the PROCESS
  environment holds BUYBACK_RIG_TOKEN (>= 24 characters; never read from .env) and the engine is DRY. The report is
  validated field by field (pons_session_record: the buy must exist, eth_in must be its batch amount, every check
  passed, the simulation ok, only decimals / times / a sha256 / small counts), journalled as 'pons_session', and
  shown on that buy as "Simulated buy · clicked by the rat on pons". It never changes a cap, the pending amount or any engine figure.

LIVE (implemented, gated like launcher.py / live/brainrig.py, NOT used: the owner said not to buy yet)
  PAUSED: this LIVE path signs from the launch wallet and pays from its claimed creator fees, but the buys are now
  paid from the separate, hand-funded buyback wallet, and no LIVE path for that wallet exists (no key handling), so
  LIVE is refused at its first gate after --confirm (LIVE_BUYER_READY). It also needs a set --hourly-budget-eth (no
  preview in LIVE) and hour windows. The rest below is what the gate would check after that.
  Needs ALL of: --live --confirm LABRAT; the public relay and origin (no --accept-test-streams, no other relay); no
  --journal-dir (LIVE's journal and lock have ONE fixed place, runs/buyback/, so the caps cannot be reset by pointing
  it elsewhere); no RATBRAIN_RPC override in the environment (LIVE reads and sends only through the pinned public
  RPCs); the .env FILE (launcher.read_env_file, the launcher's reading) says BUYBACK_LIVE=1 and holds BUYBACK_RH_KEY
  (or RATBRAIN_RH_KEY), and the shell environment does not disagree; the key's address is the pinned launch wallet;
  chain id 4663; the pinned chain checks pass; no other LIVE buyback runs (runs/buyback/live.lock, exclusive create);
  the LIVE journal (runs/buyback/journal_live.jsonl) has no unreadable line, its last stop is cleared (--clear-stop,
  an explicit operator decision), and the launch wallet's on-chain nonce is exactly what the journal accounts for
  (LIVE_FIRST_NONCE, the nonce after the launch transaction, plus one per journalled transaction). A lost or foreign
  journal, a second engine or a transaction sent from the wallet outside this engine is refused. --first-nonce N
  accepts a wallet that sent transactions elsewhere before its FIRST LIVE run (only while the journal has none).
  Then each claim and buy is simulated again on the real state right before signing (no overrides; min out = the
  fresh quote minus slippage, deadline = now + deadline_s), checked against the caps and the claimed fees again,
  signed at the wallet's pending nonce (only when it is the nonce the journal expects and no other transaction of the
  wallet is pending), written to the journal with its raw bytes BEFORE broadcast, and followed to its receipt. The
  receipt is booked in ONE journal record (the claim / buy / gas record carries the tx hash and resolves it), so a
  crash can never leave a mined buy unbooked. An unconfirmed transaction blocks new ones until it resolves: the same
  signed bytes are re-broadcast if the chain forgot them; it is marked dropped only when EVERY pinned RPC, asked
  separately, has no receipt for it and says its nonce was used, twice at least DROP_CONFIRM_S apart. A stop (e.g.
  max_failures failed claims / buys in a row) is journalled and survives restarts until --clear-stop.
"""
import argparse
import collections
import copy
import hashlib
import hmac
import json
import os
import re
import struct
import sys
import threading
import time
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LIVE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(LIVE_DIR)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import launcher  # noqa: E402  (RPC helpers; launcher.read_env_file is called by the LIVE gate only)
from eth_abi import decode, encode  # noqa: E402
from eth_utils import keccak, to_checksum_address  # noqa: E402

# ---------------------------------------------------------------------------------------------------- the pins
CHAIN_ID = 4663
SYMBOL = 'LABRAT'
TOKEN = to_checksum_address('0xaCa07FE3BC5fF3e7501cA1dCCFCd937fD710680d')
CURVE = to_checksum_address('0x174E4Cc2A44811Ed85eB2589C119B723a71AE024')
WALLET = to_checksum_address('0x4C2661717B97cd23aa87Fe29fE0C50CFf2CBb893')   # launch wallet = creator = fee recipient
# the separate buyback wallet the owner funds by hand: the buyer of every DRY simulated buy (no key here, ever)
BUYBACK_WALLET = to_checksum_address('0x17852f35b597554732C706A8A9FAA534C10e1E23')
FACTORY = to_checksum_address(launcher.FACTORY)
FEE_ESCROW = to_checksum_address('0xd3AFEB2a57f70eF218Aa82451c51B2fb0416Ac9e')
HOOK = to_checksum_address('0xE5e702641Ea86F4ae6cC3cDaeD2B886f976Be044')
ROUTER = to_checksum_address('0x8876789976dEcBfCbBbe364623C63652db8C0904')
QUOTER = to_checksum_address('0xe202BB8dd524eE9C5E679e5B5809f7A373a982Ef')
ZERO = '0x0000000000000000000000000000000000000000'
POOL_FEE, TICK_SPACING = 0, 200
POOL_ID = '0xb0eb2633c73b39d2832643b62f54d99b71af752cf672df6b898a2210b8c2769d'
POOL_KEY_T = '(address,address,uint24,int24,address)'

RELAY_URL = 'wss://labrat-relay-production.up.railway.app/live'
ORIGIN = 'https://lab-rat.net'
TASKS = ('lever', 'cursor', 'steer', 'tiles')
DEFAULT_TASKS = ('cursor', 'steer', 'tiles')   # the tasks with a lit target; the lever task only has a clean press
N_TARGETS = 4                  # cursor_env.N_TARGETS: an attempt ends after 4 targets, so at most 4 hits
TILES_MAX_HITS = 64            # tiles_env.MAX_SONG_NOTES: a Rat Tiles attempt is one song of at most 64 notes
FRAME_MAGIC = 7.0              # live/labrat_frame.py
DEFAULT_JOURNAL_DIR = os.path.join(ROOT, 'runs', 'buyback')
LIVE_JOURNAL_DIR = DEFAULT_JOURNAL_DIR   # LIVE's ONE journal + lock place (--journal-dir is refused in LIVE)
LIVE_FIRST_NONCE = 1           # the launch wallet's nonce after its one transaction, the launch (checked 2026-09-25)
DROP_CONFIRM_S = 120.0         # a tx is marked dropped only when every RPC agrees twice, at least this far apart
STATUS_REWRITE_S = 10.0        # status.json is rewritten at least this often (its "updated" shows it is alive)
HOUR_S = 3600                  # one buy per hour window, aligned to the UTC hour
AMOUNT_STEP_WEI = 10 ** 10     # buys are whole multiples of 0.00000001 ETH: the 8 decimals the buy rig types into pons
BUDGET_RULE = 'hourly budget x hit rate'
FUNDING = 'a separate buyback wallet, funded by hand'
# LIVE below signs from the launch wallet and pays from its claimed creator fees; the buys are now paid from the
# hand-funded buyback wallet, and no LIVE path (no key handling) exists for it: LIVE is refused while this is False
LIVE_BUYER_READY = False


def _sel(sig):
    return '0x' + keccak(text=sig).hex()[:8]


SEL = {
    'curve': _sel('curve()'),                                            # token -> its curve
    'record': _sel('getLaunchedToken(address)'),                         # factory record (15 words)
    'graduated': _sel('graduated()'),
    'meme_hook': _sel('memeHook()'),
    'balance_of': _sel('balanceOf(address)'),                            # FeeEscrow: claimable ETH
    'claim_all': _sel('claim()'),
    'claim': _sel('claim(uint256)'),
    'quote': _sel('quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))'),
    'execute': _sel('execute(bytes,bytes[],uint256)'),
    'curve_buy': _sel('buy(uint256,uint256,address)'),
}
TRANSFER_TOPIC = '0x' + keccak(text='Transfer(address,address,uint256)').hex()
CLAIMED_TOPIC = '0x' + keccak(text='Claimed(address,uint256)').hex()
# FeeEscrow: claim() / claim(0) with no credit -> NoBalance(); claim(x > 0) over the balance, including from any
# address that is not the launch wallet (its balance is 0) -> InsufficientBalance(x, balance). The engine uses
# claim(uint256), so a wrong sender shows up as InsufficientBalance (re-checked with eth_call on 2026-09-25).
ERRORS = {'0x025ac17e': 'CurveGraduated()', '0xbc760cfe': 'NativeValueMismatch', '0x71c4efed': 'SlippageExceeded',
          '0x8b063d73': 'V4TooLittleReceived', '0x5bf6f916': 'TransactionDeadlinePassed()',
          '0xc2caa2a6': 'NoBalance()', '0xcf479181': 'InsufficientBalance'}
READ_METHODS = frozenset({'eth_chainId', 'eth_blockNumber', 'eth_getBlockByNumber', 'eth_gasPrice', 'eth_call',
                          'eth_estimateGas', 'eth_getBalance', 'eth_getTransactionCount', 'eth_getTransactionReceipt',
                          'eth_getTransactionByHash', 'eth_getCode'})
GAS_GUESS = {'pool': 200_000, 'curve': 160_000, 'claim': 60_000}     # budgeting before a simulation gives the real one
GAS_MULT, FEE_MULT = 1.3, 2                                          # as launcher.py: limit headroom, maxFee headroom
POSSIBLY_SENT = launcher.POSSIBLY_SENT

DRY_LABEL = 'DRY - simulated, not executed'
LIVE_LABEL = 'LIVE - real buys paid from claimed creator fees'
PREVIEW_LABEL = 'Preview · budget not set: simulated with a nominal amount'
STATE = {'env_read': False}    # whether this process ever read .env (DRY never does)

# The rat's pons sessions (live/buyrig.py, live/buyrig_runner.py): for a simulated buy the engine booked, the rat
# clicked through pons's own buy flow on the real coin page; pons built the transaction, the rig checked and simulated
# it and refused to sign. The runner reports each such session here (POST /pons_session, DRY only, Bearer
# BUYBACK_RIG_TOKEN from the process environment, never .env). It adds a label to that buy in the public status; it
# never changes a cap, the pending amount, the budget or any figure the engine computed.
PONS_LABEL = 'Simulated buy · clicked by the rat on pons'
RIG_TOKEN_ENV = 'BUYBACK_RIG_TOKEN'
RIG_TOKEN_MIN = 24
PONS_BODY_MAX = 4096
PONS_KEYS = frozenset({'buy_at', 'eth_in', 'labrat_out', 'session_at', 'proof', 'replay', 'targets_hit', 'misses',
                       'checks_passed', 'checks_total', 'simulation'})
PONS_KEEP = 200                # pons sessions (and buy times) kept in memory for matching; the journal keeps all
ISO_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
DEC_RE = re.compile(r'^\d{1,15}(\.\d{1,18})?$')
HEX64_RE = re.compile(r'^[0-9a-f]{64}$')


# ---------------------------------------------------------------------------------------------------- config
@dataclass(frozen=True)
class Config:
    hourly_budget_wei: object = None            # the owner's hourly budget (wei); None = not set yet -> preview buys
    preview_budget_wei: int = 10 ** 15          # 0.001 ETH: the nominal budget of a preview buy (budget not set)
    min_buy_wei: int = 10 ** 14                 # 0.0001 ETH: an hour whose buy is smaller buys nothing (gas)
    max_buy_wei: int = 10 ** 15                 # 0.001 ETH per buy (the hourly budget is cut to it)
    max_hour_wei: int = 2 * 10 ** 15            # 0.002 ETH per rolling hour
    max_day_wei: int = 10 ** 16                 # 0.01 ETH per rolling day
    max_total_wei: int = 5 * 10 ** 16           # 0.05 ETH ever (per journal)
    gas_reserve_wei: int = 2 * 10 ** 14         # LIVE (fee-funded): 0.0002 ETH of the claimed fees kept back for gas
    claim_min_wei: int = 10 ** 15               # LIVE: claim only when at least 0.001 ETH is claimable
    claim_max_wei: int = 2 * 10 ** 16           # LIVE: at most 0.02 ETH per claim
    slippage_bps: int = 300                     # min out = simulated tokens out - 3 %
    deadline_s: int = 180                       # router deadline after the chain's clock
    max_gas_price_wei: int = 10 ** 9            # skip while gas is above 1 gwei (it is ~0.044 gwei)
    max_gas_share_bps: int = 2000               # no buy for an hour whose gas would be over 20 % of it
    max_gas_day_wei: int = 2 * 10 ** 15         # gas of claims, buys and reverted txs per rolling day (0.002 ETH)
    max_gas_total_wei: int = 10 ** 16           # ... and ever, per journal (0.01 ETH)
    max_hits_per_episode: int = N_TARGETS
    tasks: tuple = DEFAULT_TASKS
    venue: str = 'auto'                         # auto | curve | pool
    max_failures: int = 3                       # consecutive failed claims / buys -> stop
    window_s: int = HOUR_S                      # the buy window: the UTC hour (shorter only in tests; LIVE refuses it)

    def validate(self):
        bad = []
        for f in fields(self):
            v = getattr(self, f.name)
            if f.name == 'hourly_budget_wei' and v is None:
                continue
            if f.name.endswith('_wei') and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
                bad.append(f'{f.name} must be a non-negative integer')
        if bad:
            raise ValueError('; '.join(bad))
        for k, cap in HARD.items():
            v = getattr(self, k)
            if v is not None and v > cap:
                bad.append(f'{k} {v} is over the hard ceiling {cap}')
        for k in ('preview_budget_wei', 'min_buy_wei', 'max_buy_wei', 'max_hour_wei', 'max_day_wei', 'max_total_wei',
                  'claim_max_wei', 'max_gas_price_wei', 'max_gas_day_wei', 'max_gas_total_wei'):
            if getattr(self, k) <= 0:
                bad.append(f'{k} must be > 0')
        if self.hourly_budget_wei is not None and self.hourly_budget_wei <= 0:
            bad.append('hourly_budget must be > 0 (leave it unset for preview buys)')
        if self.min_buy_wei > self.max_buy_wei:
            bad.append('min_buy is over max_buy')
        if self.max_buy_wei > min(self.max_hour_wei, self.max_day_wei, self.max_total_wei):
            bad.append('max_buy is over the hourly, daily or total cap')
        if self.claim_min_wei > self.claim_max_wei:
            bad.append('claim_min is over claim_max')
        if not 1 <= self.slippage_bps <= HARD['slippage_bps']:
            bad.append('slippage_bps must be 1..1000')
        if not 30 <= self.deadline_s <= 1800:
            bad.append('deadline_s must be 30..1800')
        if not 1 <= self.max_gas_share_bps <= HARD['max_gas_share_bps']:
            bad.append('max_gas_share_bps must be 1..5000')
        if not isinstance(self.window_s, int) or not 1 <= self.window_s <= HOUR_S or HOUR_S % self.window_s:
            bad.append('window_s must divide 3600 (the UTC hour)')
        if not set(self.tasks) <= set(TASKS) or not self.tasks:
            bad.append(f'tasks must be some of {TASKS}')
        if not 1 <= self.max_hits_per_episode <= N_TARGETS:
            bad.append(f'max_hits_per_episode must be 1..{N_TARGETS}')
        if self.venue not in ('auto', 'curve', 'pool'):
            bad.append('venue must be auto, curve or pool')
        if bad:
            raise ValueError('; '.join(bad))
        return self

    @property
    def preview(self):
        """True while the owner has not set the hourly budget: each hour runs a simulated preview buy."""
        return self.hourly_budget_wei is None

    @property
    def base_wei(self):
        """The hour's budget before the hit rate: the owner's hourly budget, or the nominal preview amount."""
        return self.preview_budget_wei if self.hourly_budget_wei is None else self.hourly_budget_wei


HARD = {   # ceilings in code: a typo on the command line can never lift these
    # the owner's budget (2026-09-25): 0.1 ETH x hit rate per hour -> at most 0.1 per buy and per hour, 2.4 a day
    # (24 perfect hours), and a 5 ETH lifetime safety cap
    'hourly_budget_wei': 10 ** 17, 'preview_budget_wei': 10 ** 16,
    'max_buy_wei': 10 ** 17, 'max_hour_wei': 10 ** 17, 'max_day_wei': 24 * 10 ** 17,
    'max_total_wei': 5 * 10 ** 18, 'claim_max_wei': 10 ** 17,
    'max_gas_price_wei': 10 * 10 ** 9, 'slippage_bps': 1000, 'max_gas_share_bps': 5000,
    'max_gas_day_wei': 10 ** 16, 'max_gas_total_wei': 5 * 10 ** 16,
}


class SendRefused(RuntimeError):
    """A send was attempted through a read-only connection (a bug; never caught silently)."""


class LiveRefused(RuntimeError):
    pass


class NonceMismatch(LiveRefused):
    """The wallet's nonce is not what the LIVE journal accounts for: stop (an operator must look), never retry."""


class RpcError(RuntimeError):
    def __init__(self, method, err):
        self.method, self.err = method, err
        super().__init__(f'{method}: {short_err(err)}')


class PonsRefused(ValueError):
    """A pons session report the engine does not take. status: the HTTP status for the reporting endpoint."""

    def __init__(self, why, status=400):
        super().__init__(why)
        self.status = status


# ---------------------------------------------------------------------------------------------------- helpers
def log(*parts):
    try:
        stamp = datetime.now(timezone.utc).strftime('%H:%M:%S')
        print(f'[buyback {stamp}] ' + ' '.join(str(p) for p in parts), file=sys.stderr, flush=True)
    except Exception:
        pass


def iso(t=None):
    return datetime.fromtimestamp(time.time() if t is None else t, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def eth_str(wei, places=8):
    s = f'{Decimal(int(wei)) / Decimal(10 ** 18):.{places}f}'.rstrip('0').rstrip('.')
    return s if s not in ('', '-0') else '0'


def token_str(amount, places=2):
    return eth_str(amount, places)          # $LABRAT has 18 decimals, like ETH


def parse_eth(s):
    try:
        d = Decimal(str(s).strip())
    except InvalidOperation:
        raise ValueError(f'not an ETH amount: {s!r}') from None
    w = d * (10 ** 18)
    if d < 0 or w != w.to_integral_value():
        raise ValueError(f'not a valid ETH amount (>= 0, at most 18 decimals): {s!r}')
    return int(w)


def window_start(t, window_s=HOUR_S):
    """The start (unix seconds, an int) of the window holding t: the UTC hour (window_s 3600)."""
    return int(t // window_s) * window_s


def hit_rate(hits, misses, wrong):
    """hits / (hits + misses + wrong), or None for a window with nothing in it."""
    n = int(hits) + int(misses) + int(wrong)
    return None if n <= 0 else int(hits) / n


def rate_str(rate):
    return None if rate is None else round(rate, 4)


def hour_amount(base_wei, hits, misses, wrong):
    """The hour's buy before the caps: floor(base x hits / (hits + misses + wrong)), rounded DOWN to AMOUNT_STEP_WEI
    (integer arithmetic, no float). 0 for an empty window."""
    n = int(hits) + int(misses) + int(wrong)
    if n <= 0 or hits <= 0:
        return 0
    return int(base_wei) * int(hits) // n // AMOUNT_STEP_WEI * AMOUNT_STEP_WEI


def short_err(err):
    if isinstance(err, dict):
        name = revert_name(err)
        msg = str(err.get('message', ''))[:160]
        return f'{msg} [{name}]' if name else msg
    return str(err)[:200]


def revert_name(err):
    """The custom error of a JSON-RPC revert, by selector (None when there is no revert data)."""
    data = err.get('data') if isinstance(err, dict) else None
    if isinstance(data, dict):
        data = data.get('data')
    if isinstance(data, str) and data.startswith('0x') and len(data) >= 10:
        return ERRORS.get(data[:10].lower(), data[:10].lower())
    return None


def pad_addr(a):
    return a[2:].lower().rjust(64, '0')


def words(res):
    h = res[2:] if isinstance(res, str) and res.startswith('0x') else ''
    return [h[i:i + 64] for i in range(0, len(h) - len(h) % 64, 64)]


def w_int(w):
    return int(w, 16)


def w_addr(w):
    return to_checksum_address('0x' + w[-40:])


def sha(data_hex):
    return hashlib.sha256(bytes.fromhex(data_hex[2:])).hexdigest()


def write_json_atomic(path, obj):
    """Best effort (the status file): on Windows os.replace fails while a reader holds the file open."""
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        return False


class StatusFile:
    """status.json: rewritten on every change AND at least every STATUS_REWRITE_S (its "updated" then proves the
    engine is alive; the site treats an old one as stale). A failed write (Windows: a reader holds the file) keeps
    the change pending, so the next loop retries it instead of leaving e.g. "connected" after the relay dropped."""

    def __init__(self, path, every=STATUS_REWRITE_S, clock=time.monotonic):
        self.path, self.every, self.clock = path, every, clock
        self.last = None
        self.failed = 0

    def update(self, engine, force=False):
        now = self.clock()
        if not (force or engine.changed or self.last is None or now - self.last >= self.every):
            return None
        engine.changed = False                 # cleared BEFORE the snapshot: a change made meanwhile is kept
        if write_json_atomic(self.path, engine.public_status()):
            self.last = now
            return True
        engine.changed = True
        self.failed += 1
        return False


# ---------------------------------------------------------------------------------------------------- calldata
def pool_key():
    return (ZERO, TOKEN, POOL_FEE, TICK_SPACING, HOOK)


def pool_id(key=None):
    return '0x' + keccak(encode([POOL_KEY_T], [key or pool_key()])).hex()


def cd_balance_of(addr):
    return SEL['balance_of'] + pad_addr(addr)


def cd_claim(amount):
    return SEL['claim'] + encode(['uint256'], [int(amount)]).hex()


def cd_quote(amount):
    return SEL['quote'] + encode([f'({POOL_KEY_T},bool,uint128,bytes)'], [(pool_key(), True, int(amount), b'')]).hex()


def cd_router_buy(amount, min_out, deadline):
    """pons Universal Router execute(0x10 V4_SWAP, [actions 0x06 0x0c 0x0f], deadline): swap exact-in single
    (ETH -> LABRAT, the 6-field struct real pons buys use), settle all ETH, take all LABRAT (to msg.sender)."""
    swap = encode([f'({POOL_KEY_T},bool,uint128,uint128,uint256,bytes)'],
                  [(pool_key(), True, int(amount), int(min_out), 0, b'')])
    settle = encode(['address', 'uint256'], [ZERO, int(amount)])
    take = encode(['address', 'uint256'], [TOKEN, int(min_out)])
    v4 = encode(['bytes', 'bytes[]'], [bytes([0x06, 0x0C, 0x0F]), [swap, settle, take]])
    return SEL['execute'] + encode(['bytes', 'bytes[]', 'uint256'], [bytes([0x10]), [v4], int(deadline)]).hex()


def cd_curve_buy(amount, min_out, recipient=WALLET):
    return SEL['curve_buy'] + encode(['uint256', 'uint256', 'address'], [int(amount), int(min_out), recipient]).hex()


def router_amounts(data):
    """Decode a router buy built by cd_router_buy -> (amountIn, SETTLE_ALL amount, minOut, deadline). ValueError
    when it is not exactly that shape (one V4_SWAP of the pinned pool, ETH in, LABRAT out)."""
    if not isinstance(data, str) or data[:10] != SEL['execute']:
        raise ValueError('not a router execute call')
    commands, inputs, deadline = decode(['bytes', 'bytes[]', 'uint256'], bytes.fromhex(data[10:]))
    if commands != bytes([0x10]) or len(inputs) != 1:
        raise ValueError('the router call is not one V4_SWAP')
    actions, params = decode(['bytes', 'bytes[]'], inputs[0])
    if actions != bytes([0x06, 0x0C, 0x0F]) or len(params) != 3:
        raise ValueError('the V4_SWAP is not swap exact-in single, settle all, take all')
    (key, zfo, amount_in, min_out, _extra, _hd), = decode(
        [f'({POOL_KEY_T},bool,uint128,uint128,uint256,bytes)'], params[0])
    s_cur, s_amt = decode(['address', 'uint256'], params[1])
    t_cur, t_min = decode(['address', 'uint256'], params[2])
    key = (to_checksum_address(key[0]), to_checksum_address(key[1]), key[2], key[3], to_checksum_address(key[4]))
    if key != pool_key() or zfo is not True:
        raise ValueError('the swap is not ETH -> LABRAT in the pinned pool')
    if to_checksum_address(s_cur) != ZERO or to_checksum_address(t_cur) != TOKEN or t_min != min_out:
        raise ValueError('settle / take do not match the swap')
    return amount_in, s_amt, min_out, deadline


def check_value(to, data, value):
    """The ETH a transaction carries must be exactly what it spends. The pons router keeps any msg.value above the
    swap's amountIn (no refund: execute with value = 2 x amountIn succeeds), so value == amountIn == SETTLE_ALL; the
    curve needs value == quoteIn; a claim carries none. ValueError otherwise (checked before simulating and signing)."""
    value = int(value)
    if to == ROUTER:
        amount_in, settle, _mo, _dl = router_amounts(data)
        if not value == amount_in == settle:
            raise ValueError(f'router buy value {value} != amountIn {amount_in} / settle {settle}')
    elif to == CURVE:
        if data[:10] != SEL['curve_buy']:
            raise ValueError('not a curve buy')
        quote_in = decode(['uint256', 'uint256', 'address'], bytes.fromhex(data[10:]))[0]
        if value != quote_in:
            raise ValueError(f'curve buy value {value} != quoteIn {quote_in}')
    elif to == FEE_ESCROW:
        if value != 0 or data[:10] != SEL['claim']:
            raise ValueError('a claim is claim(uint256) and carries no ETH')
    else:
        raise ValueError(f'{to} is not a pinned target')
    return True


def call_info(to, data, value, **extra):
    """What the journal keeps of a call: the target, the selector, the value and the calldata's sha256."""
    return {'to': to, 'selector': data[:10], 'value_wei': int(value), 'data_sha256': sha(data), **extra}


# ---------------------------------------------------------------------------------------------------- RPC
class ReadRpc:
    """Read-only chain access through launcher.rpc (the launcher's RPC list and retries). It refuses every method that
    is not a read, so nothing built on it can send a transaction. DRY uses nothing else."""

    def __init__(self, transport=None):
        self._transport = transport if transport is not None else launcher.rpc
        self.calls = collections.Counter()

    def raw(self, method, params):
        if method not in READ_METHODS:
            raise SendRefused(f'{method} is not a read method: this connection only reads')
        self.calls[method] += 1
        return self._transport(method, params)

    def ok(self, method, params):
        res, err = self.raw(method, params)
        if err is not None or res is None:
            raise RpcError(method, err)
        return res


def _node_transport(url):
    """One RPC endpoint on its own (no fall-over), for evidence that must come from every node separately."""
    def call(method, params):
        import requests
        last = None
        for attempt in range(2):
            try:
                j = requests.post(url, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params},
                                  timeout=20).json()
            except Exception as e:
                last = e
                time.sleep(1 + attempt)
                continue
            if 'error' in j:
                return None, j['error']
            return j.get('result'), None
        raise RuntimeError(f'{url}: {type(last).__name__}')
    return call


class LiveRpc(ReadRpc):
    """ReadRpc plus eth_sendRawTransaction. Built only by live_gate(), after every gate passed. `nodes`: one
    transport per pinned RPC (launcher.RPCS), asked one by one before a transaction is ever marked dropped."""

    def __init__(self, transport=None, _gate=None, nodes=None):
        if _gate is not _GATE_PASSED:
            raise LiveRefused('LiveRpc is built only by live_gate()')
        super().__init__(transport)
        self.nodes = list(nodes) if nodes is not None else [_node_transport(u) for u in launcher.RPCS]

    def each(self, method, params):
        """-> [(result, error)] from every node separately; a node that cannot be reached gives (None, 'unreachable')."""
        if method not in READ_METHODS:
            raise SendRefused(f'{method} is not a read method')
        out = []
        for node in self.nodes:
            self.calls[method] += 1
            try:
                out.append(node(method, params))
            except Exception:
                out.append((None, 'unreachable'))
        return out

    def send_raw(self, raw):
        self.calls['eth_sendRawTransaction'] += 1
        return self._transport('eth_sendRawTransaction', [raw], all_rpcs_on_error=True)


_GATE_PASSED = object()


# ---------------------------------------------------------------------------------------------------- the chain
def check_chain(rpc, venue_pref='auto'):
    """The pinned checks, before every buy. -> {ok, stop, venue, phase, graduated, block, timestamp, problems,
    public}. Network trouble raises (the caller retries later); a mismatch returns stop=True."""
    blk = rpc.ok('eth_getBlockByNumber', ['latest', False])
    out = {'block': int(blk['number'], 16), 'timestamp': int(blk['timestamp'], 16), 'problems': [],
           'venue': None, 'phase': None, 'graduated': None}
    p = out['problems']

    def call(to, data):
        return rpc.ok('eth_call', [{'to': to, 'data': data}, 'latest'])

    tc = words(call(TOKEN, SEL['curve']))
    if not tc or w_addr(tc[0]) != CURVE:
        p.append(f'token.curve() is {w_addr(tc[0]) if tc else "empty"}, not the pinned curve {CURVE}')
    rec = words(call(FACTORY, SEL['record'] + pad_addr(TOKEN)))
    if len(rec) < 15:
        p.append(f'factory getLaunchedToken returned {len(rec)} words, expected 15')
    else:
        checks = [(w_addr(rec[0]) == TOKEN, 'record token'), (w_addr(rec[1]) == CURVE, 'record curve'),
                  (w_addr(rec[3]) == WALLET, 'record creator-fee recipient (the launch wallet)'),
                  (w_int(rec[4]) == 0, 'record pair token (ETH)'), (w_int(rec[14]) == 1, 'record exists'),
                  (w_int(rec[9]) == 0, "pons's own buyback switch (off when pinned)")]
        p.extend(f'{what} differs from the pin' for good, what in checks if not good)
        out['phase'] = w_int(rec[10])
    g = words(call(CURVE, SEL['graduated']))
    out['graduated'] = bool(g and w_int(g[0]))
    if not p:
        if out['phase'] == 0 and not out['graduated']:
            out['venue'] = 'curve'
        elif out['phase'] == 2 and out['graduated']:
            hook = words(call(FACTORY, SEL['meme_hook']))
            if not hook or w_addr(hook[0]) != HOOK:
                p.append('factory memeHook() is not the pinned hook')
            if w_int(rec[6]) != POOL_FEE or w_int(rec[7]) != TICK_SPACING:
                p.append('record pool fee / tick spacing differ from the pinned pool key')
            if pool_id() != POOL_ID:
                p.append('the pinned pool key does not hash to the pinned pool id')
            out['venue'] = 'pool'
        else:
            p.append(f"phase {out['phase']} with graduated() {out['graduated']}: neither a live curve nor the pool")
    if not p and venue_pref == 'curve' and out['venue'] == 'pool':
        p.append('the curve has graduated and --venue curve says to stop then')
    if not p and venue_pref == 'pool' and out['venue'] == 'curve':
        p.append('the coin is still on its curve and --venue pool says to buy only in the pool')
    out['ok'] = not p
    out['stop'] = bool(p)
    out['public'] = None if not p else ('the curve graduated (buys set to the curve only)'
                                         if 'graduated and --venue curve' in p[0]
                                         else 'the chain no longer matches the pinned coin')
    return out


class Sim:
    """eth_call / eth_estimateGas simulations. The buys come from `buyer` (DRY: the hand-funded buyback wallet; LIVE:
    the launch wallet, the signer), the creator-fee claims always from the launch wallet (only it can claim).
    override: a state override (DRY only)."""

    def __init__(self, rpc, cfg, clock=time.time, buyer=BUYBACK_WALLET):
        self.rpc, self.cfg, self.clock, self.buyer = rpc, cfg, clock, to_checksum_address(buyer)

    def _call(self, to, data, value=0, override=None, frm=None):
        params = [{'from': frm or self.buyer, 'to': to, 'data': data, 'value': hex(int(value))}, 'latest']
        if override:
            params.append(override)
        return self.rpc.raw('eth_call', params)

    def _gas(self, to, data, value, override=None, frm=None):
        base = [{'from': frm or self.buyer, 'to': to, 'data': data, 'value': hex(int(value))}, 'latest']
        if override:
            res, err = self.rpc.raw('eth_estimateGas', base + [override])
            if err is None and res:
                return int(res, 16)
        return int(self.rpc.ok('eth_estimateGas', base), 16)

    def gas_price(self):
        return int(self.rpc.ok('eth_gasPrice', []), 16)

    def balance(self, addr=None):
        """The buyer's ETH (or addr's)."""
        return int(self.rpc.ok('eth_getBalance', [addr or self.buyer, 'latest']), 16)

    def claimable(self):
        w = words(self.rpc.ok('eth_call', [{'to': FEE_ESCROW, 'data': cd_balance_of(WALLET)}, 'latest']))
        return w_int(w[0]) if w else 0

    def claim(self, amount):
        data = cd_claim(amount)
        check_value(FEE_ESCROW, data, 0)
        _res, err = self._call(FEE_ESCROW, data, 0, frm=WALLET)
        if err is not None:
            return {'ok': False, 'error': f'claim simulation reverted: {short_err(err)}'}
        return {'ok': True, 'to': FEE_ESCROW, 'data': data, 'value': 0,
                'gas': self._gas(FEE_ESCROW, data, 0, frm=WALLET),
                'call': call_info(FEE_ESCROW, data, 0, fn='claim(uint256)', amount_wei=int(amount), result='ok')}

    def buy(self, venue, amount, block_ts, override=None):
        """The exact buy transaction for `amount` wei, simulated. -> {ok, to, data, value, tokens_out, min_out,
        deadline, gas, quote_checked, calls} or {ok: False, error}."""
        c = self.cfg
        deadline = max(int(self.clock()), int(block_ts)) + c.deadline_s
        calls = []
        if venue == 'pool':
            q_data = cd_quote(amount)
            res, err = self._call(QUOTER, q_data, 0)
            if err is not None:
                return {'ok': False, 'error': f'quote reverted: {short_err(err)}'}
            quote = w_int(words(res)[0]) if words(res) else 0
            calls.append(call_info(QUOTER, q_data, 0, fn='quoteExactInputSingle', amount_out=quote))
            if quote <= 0:
                return {'ok': False, 'error': 'the quoter returned 0 tokens'}
            min_out = quote * (10_000 - c.slippage_bps) // 10_000
            to, data = ROUTER, cd_router_buy(amount, min_out, deadline)
            check_value(to, data, amount)                   # the router keeps any excess ETH: value == amountIn
            _r, err = self._call(ROUTER, data, amount, override)
            if err is not None:
                return {'ok': False, 'error': f'router buy reverted: {short_err(err)}'}
            calls.append(call_info(ROUTER, data, amount, fn='execute (the transaction)', min_out=min_out,
                                   deadline=deadline, result='ok'))
            # the quote as a floor: the same buy with minOut = the full quote must pass too
            full = cd_router_buy(amount, quote, deadline)
            _r2, err2 = self._call(ROUTER, full, amount, override)
            calls.append(call_info(ROUTER, full, amount, fn='execute (minOut = quote)', min_out=quote,
                                   result='ok' if err2 is None else short_err(err2)))
            tokens_out, checked = quote, err2 is None
        elif venue == 'curve':
            probe = cd_curve_buy(amount, 0, self.buyer)
            res, err = self._call(CURVE, probe, amount, override)
            if err is not None:
                return {'ok': False, 'error': f'curve buy reverted: {short_err(err)}'}
            tokens_out = w_int(words(res)[0]) if words(res) else 0
            calls.append(call_info(CURVE, probe, amount, fn='buy (minOut 0)', tokens_out=tokens_out))
            if tokens_out <= 0:
                return {'ok': False, 'error': 'the curve returned 0 tokens'}
            min_out = tokens_out * (10_000 - c.slippage_bps) // 10_000
            to, data = CURVE, cd_curve_buy(amount, min_out, self.buyer)
            check_value(to, data, amount)
            res2, err = self._call(CURVE, data, amount, override)
            if err is not None:
                return {'ok': False, 'error': f'curve buy reverted: {short_err(err)}'}
            got = w_int(words(res2)[0]) if words(res2) else 0
            calls.append(call_info(CURVE, data, amount, fn='buy (the transaction)', min_out=min_out, tokens_out=got,
                                   result='ok'))
            checked = got == tokens_out
            deadline = None
        else:
            return {'ok': False, 'error': f'no venue {venue!r}'}
        gas = self._gas(to, data, amount, override)
        return {'ok': True, 'to': to, 'data': data, 'value': int(amount), 'tokens_out': tokens_out, 'min_out': min_out,
                'deadline': deadline, 'gas': gas, 'quote_checked': checked, 'calls': calls}


def gas_share_bps(gas_cost, amount):
    return 10_000 if amount <= 0 else int(gas_cost * 10_000 // amount)


# ---------------------------------------------------------------------------------------------------- hits
class HitCounter:
    """Relay messages -> counted attempts (hits, misses, wrong clicks). Pure (no I/O). See "WHAT COUNTS" at the top."""

    def __init__(self, tasks=DEFAULT_TASKS, max_hits=N_TARGETS, accept_test=False, seen=None):
        self.tasks = tuple(tasks)
        self.max_hits = int(max_hits)
        self.accept_test = bool(accept_test)
        self.seen = seen if seen is not None else set()     # episode keys already counted (shared with the ledger)
        self.session = None
        self.live = False
        self.last_n = {}
        self.stats = collections.Counter()
        self.attempt = {'clicks': 0, 'on_target': 0}
        self.tiles_missed = set()      # Rat Tiles: ids of the tiles that slid past in the attempt in progress
        self.whole = False             # the attempt in progress was followed from its start (every tile event seen)

    def _session(self, h):
        if not isinstance(h, dict):
            return None
        run, task, started = h.get('run'), h.get('task'), h.get('started')
        if h.get('source') != 'training' or task not in TASKS or not isinstance(run, str) or not run \
                or not isinstance(started, str) or not started:
            return {'key': None, 'countable': False, 'why': 'not a live-training hello', 'run': None, 'task': None,
                    'started': None, 'test': False}
        label = h.get('label')
        test = bool(h.get('test')) or (isinstance(label, str) and label.lstrip().startswith('TEST'))
        countable = task in self.tasks and (not test or self.accept_test)
        why = None if countable else ('a TEST stream, not live training' if test else f'task {task} is not counted')
        return {'key': f'{run}|{task}|{started}', 'run': run, 'task': task, 'started': started, 'test': test,
                'countable': countable, 'why': why}

    def _set_session(self, s, out):
        old = self.session['key'] if self.session else None
        new = s['key'] if s else None
        self.session = s
        if new != old:
            self.attempt = {'clicks': 0, 'on_target': 0}
            self.tiles_missed = set()
            if s:
                out.append({'ev': 'session', **{k: s[k] for k in ('key', 'run', 'task', 'started', 'test',
                                                                  'countable', 'why')}})

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
        t = m.get('type')
        if t == 'state':
            self.live = m.get('live') is True
            s = self._session(m.get('hello')) if m.get('hello') else None
            self._set_session(s, out)
            self.whole = False                     # joined mid-stream: the attempt in progress was not seen whole
            ep = m.get('episode')
            if isinstance(ep, dict):
                if self.live:
                    self._episode(ep, out, 'state')
                else:
                    self.stats['state_episode_not_live'] += 1      # the last episode of a run that ended: never
        elif t == 'hello':
            self.live = True
            old = self.session['key'] if self.session else None
            self._set_session(self._session(m), out)
            # a new publisher session starts a new attempt; the same one again is a resync (events may be lost)
            self.whole = bool(self.session and self.session['key'] and self.session['key'] != old)
        elif t == 'episode':
            self._episode(m, out, 'stream')
        elif t == 'tile':
            self._tile(m)
        elif t == 'bye':
            self.live = False
            self.whole = False
            self._set_session(None, out)
        elif t == 'idle':
            self.live = False
        return out

    def _tile(self, m):
        """A Rat Tiles outcome: only a tile that slid past untapped ("miss") is kept, once per tile id, as the fallback
        count of missed tiles for a publisher whose episode message carries no "missed"."""
        s = self.session
        if not (s and s['countable'] and self.live and s['task'] == 'tiles') or m.get('result') != 'miss':
            return
        tid = m.get('id')
        if isinstance(tid, int) and not isinstance(tid, bool) and len(self.tiles_missed) < 4 * TILES_MAX_HITS:
            self.tiles_missed.add(tid)

    @staticmethod
    def _count(v):
        return isinstance(v, int) and not isinstance(v, bool) and v >= 0

    def _episode(self, m, out, via):
        s = self.session
        if not s or not s['countable']:
            self.stats['episodes_ignored'] += 1
            return
        n = m.get('n')
        vals = {k: m.get(k) for k in ('n', 'presses', 'hits', 'misses')}
        if not all(self._count(v) for v in vals.values()):
            self.stats['rejected'] += 1
            out.append({'ev': 'rejected', 'session': s['key'], 'why': 'n, presses, hits and misses must be '
                        'non-negative integers', 'msg': {k: m.get(k) for k in ('n', 'presses', 'hits', 'misses')}})
            return
        key = f"{s['key']}#{n}"
        if key in self.seen:
            self.stats['duplicates'] += 1
            return
        self.seen.add(key)
        cap = 1 if s['task'] == 'lever' else (TILES_MAX_HITS if s['task'] == 'tiles' else self.max_hits)
        why = None
        # the window's three counts: hits, misses and wrong clicks (see "WHAT COUNTS" at the top)
        missed, wrong, source = 0, 0, None
        if s['task'] == 'tiles':
            if 'wrong' in m:                       # misses = tiles that slid past, wrong = wrong clicks
                wrong, missed, source = m.get('wrong'), vals['misses'], 'episode'
                if not self._count(wrong):
                    why = 'wrong must be a non-negative integer'
                elif vals['presses'] != vals['hits'] + wrong:
                    why = 'presses != hits + wrong'
            else:                                  # misses = wrong clicks; the tiles that slid past: "missed" or events
                wrong = vals['misses']
                if 'missed' in m:
                    missed, source = m.get('missed'), 'episode'
                    if not self._count(missed):
                        why = 'missed must be a non-negative integer'
                elif self.whole:
                    missed, source = len(self.tiles_missed), 'tile events'
                else:                              # not followed from its start: its missed tiles are unknown
                    missed, source = 0, 'unknown'
                if why is None and vals['presses'] != vals['hits'] + vals['misses']:
                    why = 'presses != hits + misses'
            if why is None and missed > TILES_MAX_HITS:
                why = f'{missed} missed tiles in one song (at most {TILES_MAX_HITS})'
            w_misses = missed
        else:
            if vals['presses'] != vals['hits'] + vals['misses']:
                why = 'presses != hits + misses'
            w_misses = vals['misses']
        if why is None and vals['hits'] > cap:
            why = f"{vals['hits']} hits in one attempt (at most {cap})"
        self.tiles_missed = set()
        self.whole = True                          # the next attempt starts now
        if why:
            self.stats['rejected'] += 1
            out.append({'ev': 'rejected', 'key': key, 'why': why,
                        'msg': {**vals, **{k: m.get(k) for k in ('missed', 'wrong') if k in m}}})
            return
        last = self.last_n.get(s['key'])
        if last is not None and n > last + 1:
            out.append({'ev': 'gap', 'session': s['key'], 'after_n': last, 'next_n': n, 'missed': n - last - 1})
        if last is None or n > last:
            self.last_n[s['key']] = n
        self.stats['episodes'] += 1
        self.attempt = {'clicks': 0, 'on_target': 0}
        # an attempt whose missed tiles are unknown (joined mid-song) stays out of the hour's hit rate: it could only
        # raise it. Its hits still count in the totals
        partial = source == 'unknown'
        ev = {'ev': 'episode', 'key': key, 'run': s['run'], 'task': s['task'], 'started': s['started'],
              'test': s['test'], 'n': n, 'hits': vals['hits'], 'presses': vals['presses'], 'misses': vals['misses'],
              'fell': bool(m.get('fell')), 'via': via, 'partial': partial,
              'w_hits': 0 if partial else vals['hits'], 'w_misses': 0 if partial else int(w_misses),
              'w_wrong': 0 if partial else int(wrong)}
        if s['task'] == 'tiles':
            ev.update(missed=int(missed), wrong=int(wrong), missed_from=source)
        out.append(ev)

    def on_bytes(self, data):
        """A frame: only the unconfirmed tally of the attempt in progress (never money)."""
        if not isinstance(data, (bytes, bytearray)) or len(data) < 48:
            return
        f = struct.unpack_from('<12f', data, 0)
        if f[0] != FRAME_MAGIC or f[4] != 1.0:
            return
        s = self.session
        if not (s and s['countable'] and self.live):
            return
        self.attempt['clicks'] += 1
        cx, cy, tx, ty, hw, hh = f[5], f[6], f[7], f[8], f[9], f[10]
        if s['task'] == 'lever' or cx == -1.0:
            on = True                         # the lever task's click flag is a clean press
        elif tx == -1.0:
            on = False                        # no lit target (a hold): a miss
        else:
            on = abs(cx - tx) <= hw and abs(cy - ty) <= hh
        if on:
            self.attempt['on_target'] += 1


# ---------------------------------------------------------------------------------------------------- the ledger
class Ledger:
    """The hour windows (hits, misses, wrong clicks), the hour's buy that is due, the fee budget (LIVE: claimed -
    spent) and the caps' windows. Rebuilt from the journal."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.seen = set()
        self.hits = self.hit_episodes = self.pending = self.capped = 0
        self.claimed = self.spent = 0
        self.buys = collections.deque()            # (t, wei) of the last day, previews left out (they use no cap)
        self.gas = collections.deque()             # (t, wei) gas of claims, buys and reverted txs, the last day
        self.gas_total = 0
        self.n_buys = self.bought = self.tokens = self.n_claims = self.hits_bought = 0
        self.cap_bought = 0                        # bought, previews left out: what the total cap counts
        self.n_previews = 0
        self.recent = collections.deque(maxlen=10)
        self.unresolved = {}                       # LIVE: tx hash -> its 'signed' record
        self.signed_nonces = []                    # LIVE: the nonce of every journalled transaction
        self.nonce_base = LIVE_FIRST_NONCE         # LIVE: the wallet's nonce before the journal's first transaction
        self.anchored = False                      # LIVE: --first-nonce was journalled
        self.failures = 0                          # consecutive failed claims / buys (LIVE replays it)
        self.stop_rec = None                       # the last uncleared 'stop' record (LIVE replays it)
        self.since = None
        self.buy_amounts = collections.OrderedDict()   # buy time (iso) -> amount wei, the last PONS_KEEP buys
        self.pons = collections.OrderedDict()          # buy time (iso) -> the public note of the rat's pons session
        self.n_pons = 0
        # the hour windows
        self.windows = {}                          # window start -> {hits, misses, wrong, attempts} (not closed yet)
        self.closed = set()                        # starts of the windows already closed ('window' records)
        self.due = None                            # the closed window whose buy is booked and not done yet
        self.last_window = None                    # the public summary of the last closed window
        self.due_hits = 0                          # the hits of the due window (shown as pending)
        self.win_bought = {}                       # window start -> wei bought for it (previews left out)

    def budget(self):
        return self.claimed - self.spent

    def next_nonce(self):
        return max(self.signed_nonces) + 1 if self.signed_nonces else self.nonce_base

    def total_done(self):
        """True once the total cap leaves no room for a minimum buy: no more buybacks, ever (per journal). Preview
        buys use no cap, so while the budget is not set this is never true."""
        return not self.cfg.preview and self.cfg.max_total_wei - self.cap_bought < self.cfg.min_buy_wei

    def gas_room(self, now):
        c = self.cfg
        day = c.max_gas_day_wei - sum(w for t, w in self.gas if now - t < 86400)
        total = c.max_gas_total_wei - self.gas_total
        return (max(0, day), 'daily gas') if day <= total else (max(0, total), 'total gas')

    def _gas(self, r):
        if r.get('preview'):                       # a preview's simulated gas uses no gas cap
            return
        cost = int(r.get('gas_cost_wei', 0))
        self.spent += cost
        self.gas_total += cost
        self.gas.append((r['t'], cost))
        while self.gas and r['t'] - self.gas[0][0] >= 86400:
            self.gas.popleft()

    def window(self, now, seconds):
        return sum(w for t, w in self.buys if now - t < seconds)

    def room(self, now, window=None):
        """-> (the largest buy the caps allow now, the name of the tightest cap). With `window` (an hour window's
        start) the hourly cap counts that window's buys (one buy per hour window, whenever in the next hour it
        happens); without it, the buys of the rolling hour. The daily cap is a rolling day, the total per journal."""
        c = self.cfg
        hour = self.win_bought.get(window, 0) if window is not None else self.window(now, 3600)
        caps = [('per-buy', c.max_buy_wei), ('hourly', c.max_hour_wei - hour),
                ('daily', c.max_day_wei - self.window(now, 86400)), ('total', c.max_total_wei - self.cap_bought)]
        name, val = min(caps, key=lambda kv: kv[1])
        return max(0, val), name

    def win_counts(self, start):
        w = self.windows.get(start)
        return dict(w) if w else {'hits': 0, 'misses': 0, 'wrong': 0, 'attempts': 0}

    def _buy_note(self, state, note=None, **extra):
        if self.last_window is not None and self.due is not None \
                and self.last_window.get('start') == self.due.get('start'):
            self.last_window['buy'] = {**self.last_window.get('buy', {}), 'state': state, 'note': note, **extra}

    def apply(self, r):
        """One journal record (also used live, so a restart rebuilds exactly this state)."""
        ev = r.get('ev')
        if self.since is None and 't' in r:
            self.since = r['t']
        if ev == 'hit':
            # an attempt; since the hourly rule it carries its window. A record from before it (added_wei, no window)
            # still counts its hits; its per-hit pending amount is dropped: those hits were never bought
            self.seen.add(r['key'])
            self.hits += int(r['hits'])
            if int(r['hits']) > 0:
                self.hit_episodes += 1
            self.capped += int(r.get('capped_wei', 0))
            if r.get('window') is not None and int(r['window']) not in self.closed:
                w = self.windows.setdefault(int(r['window']), {'hits': 0, 'misses': 0, 'wrong': 0, 'attempts': 0})
                w['hits'] += int(r.get('w_hits', r['hits']))
                w['misses'] += int(r.get('w_misses', 0))
                w['wrong'] += int(r.get('w_wrong', 0))
                w['attempts'] += 1
        elif ev == 'window':
            start = int(r['start_t'])
            self.closed.add(start)
            self.windows.pop(start, None)
            amt = int(r.get('amount_wei', 0))
            self.last_window = {k: r.get(k) for k in ('start', 'end', 'hits', 'misses', 'wrong', 'attempts')}
            self.last_window.update(
                hit_rate=r.get('hit_rate'), preview=bool(r.get('preview')),
                budget_eth=None if r.get('budget_wei') is None else eth_str(r['budget_wei']),
                preview_eth=eth_str(r['base_wei']) if r.get('preview') else None,
                amount_eth=eth_str(amt), cap=r.get('cap'), late=bool(r.get('late')), note=r.get('note'),
                buy={'state': 'due' if amt > 0 else 'none', 'note': None if amt > 0 else r.get('note')})
            if amt > 0:
                self.due = {'start': r['start'], 'start_t': start, 'amount_wei': amt, 'hits': int(r.get('hits', 0)),
                            'preview': bool(r.get('preview')), 'hit_rate': r.get('hit_rate')}
                self.pending = amt
                self.due_hits = int(r.get('hits', 0))
            else:
                self.due, self.pending, self.due_hits = None, 0, 0
        elif ev in ('buy_expired', 'window_nobuy'):
            if self.due is not None and r.get('window') == self.due['start_t']:
                self._buy_note('expired' if ev == 'buy_expired' else 'none', r.get('public'))
                self.due, self.pending, self.due_hits = None, 0, 0
        elif ev == 'claim':
            self.claimed += int(r['amount_wei'])
            self._gas(r)
            self.n_claims += 1
        elif ev == 'buy':
            amt = int(r['amount_wei'])
            preview = bool(r.get('preview'))
            at = iso(r['t'])
            if r.get('window') is not None:        # the hour's buy: done, whatever the fees cut it to
                if self.due is not None and int(r['window']) == self.due['start_t']:
                    self._buy_note('bought' if not r.get('simulated') else 'simulated', None, at=at,
                                   eth_in=eth_str(amt), labrat_out=token_str(r.get('tokens_wei', 0)),
                                   simulated=bool(r.get('simulated')), preview=preview, venue=r.get('venue'))
                    self.due, self.pending, self.due_hits = None, 0, 0
            else:                                  # a buy from before the hourly rule
                self.pending = max(0, self.pending - amt)
            self.spent += amt
            self._gas(r)
            if not preview:
                self.buys.append((r['t'], amt))
                self.cap_bought += amt
                if r.get('window') is not None:
                    w = int(r['window'])
                    self.win_bought[w] = self.win_bought.get(w, 0) + amt
                    for k in [k for k in self.win_bought if k < w - 2 * 86400]:
                        del self.win_bought[k]
            while self.buys and r['t'] - self.buys[0][0] >= 86400:
                self.buys.popleft()
            self.n_buys += 1
            self.n_previews += int(preview)
            self.bought += amt
            self.hits_bought += int(r.get('hits_covered', 0))
            self.tokens += int(r.get('tokens_wei', 0))
            self.failures = 0
            entry = {'at': at, 'eth_in': eth_str(amt), 'labrat_out': token_str(r.get('tokens_wei', 0)),
                     'venue': r.get('venue'), 'simulated': bool(r.get('simulated')),
                     'hits_covered': int(r.get('hits_covered', 0)), 'preview': preview}
            if r.get('window') is not None:
                entry.update(window=iso(int(r['window'])), hit_rate=r.get('hit_rate'))
            if preview:
                entry['label'] = PREVIEW_LABEL
            self.recent.append(entry)
            if r.get('simulated'):
                self.buy_amounts[at] = amt
                while len(self.buy_amounts) > PONS_KEEP:
                    self.buy_amounts.popitem(last=False)
        elif ev == 'pons_session':
            self._pons(r)
        elif ev == 'gas':
            self._gas(r)
        elif ev == 'signed':
            self.unresolved[r['tx']] = r
            self.signed_nonces.append(int(r['nonce']))
        elif ev == 'dropped':
            self.unresolved.pop(r.get('tx'), None)
        elif ev == 'anchor':
            self.nonce_base = int(r['nonce'])
            self.anchored = True
        elif ev in ('claim_failed', 'buy_failed', 'batch_failed'):
            self.failures = int(r.get('consecutive', self.failures + 1))
        elif ev == 'stop':
            self.stop_rec = r
        elif ev == 'stop_cleared':
            self.stop_rec = None
            self.failures = 0
        # a LIVE transaction is resolved by the ONE record that books its receipt (claim / buy / gas with its hash):
        # there is no separate 'mined' line, so a crash can never leave a mined transaction unbooked
        if ev in ('claim', 'buy', 'gas') and r.get('tx'):
            self.unresolved.pop(r['tx'], None)

    def replay(self, records, mode):
        for r in records:
            if r.get('mode') == mode:
                self.apply(r)

    def _pons(self, r):
        """A 'pons_session' record: the public note shown on that simulated buy (never a figure the engine uses)."""
        pub = {'label': PONS_LABEL, 'clicked_by_rat': True, 'simulated': True, 'at': r['session_at'],
               'eth_in': eth_str(r['amount_wei']), 'labrat_out': token_str(r['tokens_wei']),
               'targets_hit': int(r['targets_hit']), 'misses': int(r['misses']),
               'checks': f"{int(r['checks_passed'])}/{int(r['checks_total'])}", 'proof': r['proof'],
               'replay': r.get('replay')}
        self.pons[r['buy_at']] = pub
        while len(self.pons) > PONS_KEEP:
            self.pons.popitem(last=False)
        self.n_pons += 1
        for e in self.recent:
            if e['at'] == r['buy_at'] and e.get('simulated') and 'pons' not in e:
                e['pons'] = pub
                break
        lw = (self.last_window or {}).get('buy') or {}
        if lw.get('at') == r['buy_at'] and 'pons' not in lw:
            lw['pons'] = pub


def pons_session_record(obj, ledger, now, mode):
    """Validate one pons session report from the runner -> the journal record, or PonsRefused. Only fixed fields in
    fixed formats (decimals, times, a sha256, small counts), for a simulated buy this engine booked, reported once,
    with every check passed and the simulation ok. Nothing free-form reaches the public status."""
    if mode != 'DRY':
        raise PonsRefused("the rat's pons sessions are simulated: a LIVE engine does not take them", 409)
    if not isinstance(obj, dict):
        raise PonsRefused('the report is not a JSON object')
    extra = set(obj) - PONS_KEYS
    missing = (PONS_KEYS - {'replay'}) - set(obj)
    if extra:
        raise PonsRefused(f'unknown field(s): {", ".join(sorted(extra))[:120]}')
    if missing:
        raise PonsRefused(f'missing field(s): {", ".join(sorted(missing))}')
    for k in ('buy_at', 'session_at'):
        if not isinstance(obj[k], str) or not ISO_RE.match(obj[k]):
            raise PonsRefused(f'{k} must be a UTC time like 2026-09-25T03:22:58Z')
    for k in ('eth_in', 'labrat_out'):
        if not isinstance(obj[k], str) or not DEC_RE.match(obj[k]):
            raise PonsRefused(f'{k} must be a plain decimal string')
    if not isinstance(obj['proof'], str) or not HEX64_RE.match(obj['proof']):
        raise PonsRefused('proof must be the session proof: 64 lowercase hex characters')
    if obj.get('replay') not in (None, 'MATCH'):
        raise PonsRefused('replay must be "MATCH" (or left out)')
    ints = {}
    for k, hi in (('targets_hit', 50), ('misses', 100_000), ('checks_passed', 100), ('checks_total', 100)):
        v = obj[k]
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= hi:
            raise PonsRefused(f'{k} must be an integer 0..{hi}')
        ints[k] = v
    if ints['checks_total'] < 1 or ints['checks_passed'] != ints['checks_total']:
        raise PonsRefused('only a session whose checks all passed is shown')
    if ints['targets_hit'] < 1:
        raise PonsRefused('the rat hit no target in that session')
    if obj['simulation'] != 'ok':
        raise PonsRefused('only a session whose simulation succeeded is shown')
    buy_at = obj['buy_at']
    if buy_at not in ledger.buy_amounts:
        raise PonsRefused('no simulated buy was booked at that time', 404)
    if buy_at in ledger.pons:
        raise PonsRefused('that buy already has a pons session', 409)
    amount = ledger.buy_amounts[buy_at]
    if parse_eth(obj['eth_in']) != amount:
        raise PonsRefused(f'eth_in must be the batch amount ({eth_str(amount)} ETH): the rat buys exactly the batch')
    tokens = parse_eth(obj['labrat_out'])
    if not 0 < tokens <= 10 ** 30:
        raise PonsRefused('labrat_out must be above 0')

    def ts(s):
        return datetime.strptime(s, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
    t_buy, t_sess = ts(buy_at), ts(obj['session_at'])
    if not t_buy - 60 <= t_sess <= now + 60:
        raise PonsRefused('session_at must be after the buy was booked and not in the future')
    return {'buy_at': buy_at, 'amount_wei': amount, 'tokens_wei': tokens, 'session_at': obj['session_at'],
            'proof': obj['proof'], 'replay': obj.get('replay'), **ints, 'simulation': 'ok', 'simulated': True}


# ---------------------------------------------------------------------------------------------------- journal
class Journal:
    """Append-only JSONL, one fsync'd line per record."""

    def __init__(self, path, clock=time.time):
        self.path = path
        self.clock = clock
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.bad_lines = 0

    def records(self):
        """Every parseable record. Unparseable lines are counted in bad_lines (LIVE refuses to start on any)."""
        out = []
        self.bad_lines = 0
        if not os.path.exists(self.path):
            return out
        with open(self.path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    self.bad_lines += 1
                    continue
                if isinstance(r, dict):
                    out.append(r)
                else:
                    self.bad_lines += 1
        return out

    def append(self, rec):
        t = rec.get('t', self.clock())
        rec = {'t': round(t, 3), 'at': iso(t), **{k: v for k, v in rec.items() if k != 't'}}
        line = json.dumps(rec, separators=(',', ':'), default=str)
        with self.lock:
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
                f.flush()
                os.fsync(f.fileno())
        return rec


# ---------------------------------------------------------------------------------------------------- executors
def due_meta(ctx):
    """What a buy record carries of the hour it pays for: its window, hits, hit rate and whether it is a preview."""
    return {'window': ctx.get('window'), 'hits_covered': int(ctx.get('hits', 0)), 'hit_rate': ctx.get('hit_rate'),
            'preview': bool(ctx.get('preview'))}


class DryExecutor:
    """Simulates; books what a buy would have done, labelled simulated. Never signs, never sends. The buyer is the
    hand-funded buyback wallet (sim.buyer); creator-fee claims are not part of a DRY buy (fee_funded False)."""
    mode = 'DRY'
    fee_funded = False

    def __init__(self, sim, cfg):
        self.sim, self.cfg = sim, cfg

    def claim(self, amount, ctx, book):
        s = self.sim.claim(amount)
        if not s['ok']:
            return {'ok': False, 'error': s['error']}
        gp = ctx['gas_price']
        book('claim', {'amount_wei': int(amount), 'claimable_wei': ctx['claimable'], 'gas': s['gas'],
                       'gas_price_wei': gp, 'gas_cost_wei': s['gas'] * gp, 'simulated': True, 'block': ctx['block'],
                       'eth_call': s['call']})
        return {'ok': True}

    def buy(self, venue, amount, ctx, book):
        gp = ctx['gas_price']
        # the buyback wallet as if the owner had funded it for this buy: its real ETH, or the buy + its worst-case gas
        real = self.sim.balance()
        need = int(amount) + int(GAS_GUESS[venue] * GAS_MULT) * gp * FEE_MULT
        override = {self.sim.buyer: {'balance': hex(max(real, need))}}
        s = self.sim.buy(venue, amount, ctx['timestamp'], override)
        if not s['ok']:
            return {'ok': False, 'error': s['error']}
        cost = s['gas'] * gp
        share = gas_share_bps(cost, amount)
        if share > self.cfg.max_gas_share_bps:
            return {'ok': False, 'skip': True, 'final': True, 'public': 'gas too large a share of the buy',
                    'error': f'gas would be {share / 100:.1f}% of the buy '
                    f'(max {self.cfg.max_gas_share_bps / 100:.1f}%): no buy for this hour'}
        book('buy', {'venue': venue, 'amount_wei': int(amount), **due_meta(ctx),
                     'tokens_wei': s['tokens_out'], 'min_out_wei': s['min_out'], 'deadline': s['deadline'],
                     'gas': s['gas'], 'gas_price_wei': gp, 'gas_cost_wei': cost, 'gas_share_bps': share,
                     'quote_checked': s['quote_checked'], 'simulated': True, 'block': ctx['block'],
                     'buyer': self.sim.buyer, 'buyer_balance_wei': real, 'buyer_funded': real >= need,
                     'eth_calls': s['calls'],
                     'state_override': 'buyback wallet balance = max(its ETH, the buy + its worst-case gas)'})
        return {'ok': True}


class LiveExecutor:
    """Signs and sends, under the rules in the docstring at the top. Built only after live_gate() passed (which
    refuses while LIVE_BUYER_READY is False). Signs from the launch wallet and pays from its claimed creator fees."""
    mode = 'LIVE'
    fee_funded = True

    def __init__(self, rpc, account, journal, ledger, sim, cfg, sleep=time.sleep, poll_s=120.0):
        if not isinstance(rpc, LiveRpc):
            raise LiveRefused('LIVE needs the gated LiveRpc')
        if getattr(account, 'address', None) != WALLET:
            raise LiveRefused('the signer is not the pinned launch wallet')
        if getattr(sim, 'buyer', None) != WALLET:
            raise LiveRefused('LIVE simulates from the signer (the launch wallet): Sim(buyer=WALLET)')
        self.rpc, self.account, self.journal, self.ledger, self.sim, self.cfg = rpc, account, journal, ledger, sim, cfg
        self.sleep, self.poll_s = sleep, poll_s
        self.drop_suspects = {}                    # tx hash -> when every node first said "nonce used, no receipt"

    # ---- transactions
    def _nonce(self, tag):
        return int(self.rpc.ok('eth_getTransactionCount', [WALLET, tag]), 16)

    def _drop_evidence(self, txh, nonce):
        """Ask every pinned RPC separately. -> ('receipt', rc) when any node has the receipt; ('dropped', None) only
        when EVERY node answered: no receipt, not pending, and its own nonce is past ours; else ('unsure', None)."""
        receipts = self.rpc.each('eth_getTransactionReceipt', [txh])
        for rc, err in receipts:
            if rc and err is None:
                return 'receipt', rc
        if any(err is not None for _rc, err in receipts):
            return 'unsure', None
        for tx, err in self.rpc.each('eth_getTransactionByHash', [txh]):
            if err is not None or tx:
                return 'unsure', None
        for n, err in self.rpc.each('eth_getTransactionCount', [WALLET, 'latest']):
            try:
                if err is not None or int(n, 16) <= nonce:
                    return 'unsure', None
            except (TypeError, ValueError):
                return 'unsure', None
        return 'dropped', None

    def _poll(self, txh):
        end = time.monotonic() + self.poll_s
        while True:
            rc, _ = self.rpc.raw('eth_getTransactionReceipt', [txh])
            if rc:
                return rc
            if time.monotonic() >= end:
                return None
            self.sleep(0.5)

    def _finish(self, rec, rc, book):
        """A receipt for a journalled transaction: ONE booking record (claim / buy / gas, carrying the tx hash)
        journals what it did AND resolves it (Ledger.apply). If that write fails or the process dies first, the tx
        stays unresolved and the next resolve() books it from its receipt: never lost, never booked twice."""
        status = int(rc['status'], 16)
        gas_used = int(rc['gasUsed'], 16)
        price = int(rc.get('effectiveGasPrice') or hex(rec['max_fee']), 16)
        cost = gas_used * price
        block = int(rc['blockNumber'], 16)
        self.drop_suspects.pop(rec['tx'], None)
        if status != 1:
            book('gas', {'tx': rec['tx'], 'kind': rec['kind'], 'status': 'reverted', 'gas_used': gas_used,
                         'gas_cost_wei': cost, 'block': block})
            return {'state': 'reverted', 'tx': rec['tx']}
        if rec['kind'] == 'claim':
            got = sum(int(l['data'], 16) for l in rc['logs']
                      if l['address'].lower() == FEE_ESCROW.lower() and l['topics'] and l['topics'][0] == CLAIMED_TOPIC
                      and len(l['topics']) > 1 and l['topics'][1][-40:].lower() == WALLET[2:].lower())
            book('claim', {'amount_wei': got, 'claimable_wei': rec.get('claimable_wei'), 'gas': gas_used,
                           'gas_price_wei': price, 'gas_cost_wei': cost, 'simulated': False, 'block': block,
                           'status': 'mined', 'tx': rec['tx']})
        else:
            got = sum(int(l['data'], 16) for l in rc['logs']
                      if l['address'].lower() == TOKEN.lower() and l['topics'] and l['topics'][0] == TRANSFER_TOPIC
                      and len(l['topics']) > 2 and l['topics'][2][-40:].lower() == WALLET[2:].lower())
            amt = int(rec['value'])
            book('buy', {'venue': rec['venue'], 'amount_wei': amt, 'window': rec.get('window'),
                         'hits_covered': int(rec.get('hits_covered', 0)), 'hit_rate': rec.get('hit_rate'),
                         'preview': False,
                         'tokens_wei': got, 'min_out_wei': rec['min_out_wei'], 'deadline': rec.get('deadline'),
                         'gas': gas_used, 'gas_price_wei': price, 'gas_cost_wei': cost,
                         'gas_share_bps': gas_share_bps(cost, amt), 'simulated': False, 'block': block,
                         'status': 'mined', 'tx': rec['tx']})
        return {'state': 'mined', 'tx': rec['tx']}

    def resolve(self, book):
        """True when no journalled transaction of ours is unresolved. Never signs anything new."""
        for txh, rec in list(self.ledger.unresolved.items()):
            rc, _ = self.rpc.raw('eth_getTransactionReceipt', [txh])
            if rc:
                self._finish(rec, rc, book)
                continue
            known, _ = self.rpc.raw('eth_getTransactionByHash', [txh])
            if known:
                return False                               # still in the mempool
            if self._nonce('latest') > int(rec['nonce']):
                # "no receipt" is negative evidence from ONE node: ask every pinned RPC separately, and drop only when
                # all of them agree twice, DROP_CONFIRM_S apart (a lagging node must never un-book a mined buy)
                verdict, rc = self._drop_evidence(txh, int(rec['nonce']))
                if verdict == 'receipt':
                    self._finish(rec, rc, book)
                    continue
                now = self.sim.clock()
                if verdict != 'dropped':
                    self.drop_suspects.pop(txh, None)
                    return False
                first = self.drop_suspects.get(txh)
                if first is None:
                    self.drop_suspects[txh] = now
                    self.journal.append({'mode': 'LIVE', 'ev': 'drop_suspect', 'tx': txh, 'kind': rec['kind'],
                                         'why': f"every RPC: no receipt, nonce {rec['nonce']} used; checking again in "
                                                f"{DROP_CONFIRM_S:g} s"})
                    return False
                if now - first < DROP_CONFIRM_S:
                    return False
                d = self.journal.append({'mode': 'LIVE', 'ev': 'dropped', 'tx': txh, 'kind': rec['kind'],
                                         'why': f"nonce {rec['nonce']} was used by a different transaction (every RPC, "
                                                f"twice, {now - first:.0f} s apart)"})
                self.ledger.apply(d)
                self.drop_suspects.pop(txh, None)
                continue
            # the chain forgot it: the SAME signed bytes again (same nonce; a buy's deadline and min out still hold)
            _, err = self.rpc.send_raw(rec['raw'])
            self.journal.append({'mode': 'LIVE', 'ev': 'rebroadcast', 'tx': txh, 'error': short_err(err) if err else None})
            return False
        return True

    def _send(self, kind, to, data, value, gas_est, meta, book):
        check_value(to, data, value)              # ValueError (a failure, never signed) on any mismatch
        if not self.resolve(book):
            raise LiveRefused('an earlier transaction is still unresolved')
        gas_price = self.sim.gas_price()
        if gas_price > self.cfg.max_gas_price_wei:
            raise LiveRefused(f'gas price {gas_price} wei is over the cap')
        gas = int(gas_est * GAS_MULT)
        max_fee = gas_price * FEE_MULT
        pending, latest = self._nonce('pending'), self._nonce('latest')
        if pending != latest:
            raise LiveRefused(f'the launch wallet has a pending transaction (nonce {latest}..{pending}) that is not '
                              f'ours: waiting')
        expected = self.ledger.next_nonce()
        if pending != expected:
            raise NonceMismatch(f'the launch wallet is at nonce {pending} but the LIVE journal expects {expected}: a '
                                f'transaction was sent outside this engine, or another engine runs')
        bal = self.sim.balance()
        if bal < int(value) + gas * max_fee:
            raise LiveRefused(f'the launch wallet holds {bal} wei, under {int(value) + gas * max_fee} (value + gas)')
        tx = {'chainId': CHAIN_ID, 'nonce': pending, 'to': to, 'value': int(value), 'data': data, 'gas': gas,
              'maxFeePerGas': max_fee, 'maxPriorityFeePerGas': 0, 'type': 2}
        signed = self.account.sign_transaction(tx)
        raw = '0x' + bytes(signed.raw_transaction).hex().removeprefix('0x')
        txh = '0x' + bytes(signed.hash).hex().removeprefix('0x')
        # write-ahead: the exact signed bytes are on disk before they leave this machine
        rec = self.journal.append({'mode': 'LIVE', 'ev': 'signed', 'kind': kind, 'tx': txh, 'raw': raw,
                                   'nonce': pending, 'to': to, 'value': int(value), 'gas': gas, 'max_fee': max_fee,
                                   **meta})
        self.ledger.apply(rec)
        try:
            _, err = self.rpc.send_raw(raw)
        except Exception as e:                    # every RPC failed: it may still have landed, so poll regardless
            err = f'send raised {type(e).__name__}'
        self.journal.append({'mode': 'LIVE', 'ev': 'sent', 'tx': txh, 'error': short_err(err) if err else None})
        if err and not any(s in str(err).lower() for s in POSSIBLY_SENT):
            log(f'send error ({short_err(err)}); checking whether it landed anyway')
        rc = self._poll(txh)
        if rc is None:
            self.journal.append({'mode': 'LIVE', 'ev': 'unconfirmed', 'tx': txh})
            return {'state': 'unconfirmed', 'tx': txh}
        return self._finish(rec, rc, book)

    # ---- the two actions
    def claim(self, amount, ctx, book):
        s = self.sim.claim(amount)                              # again, on the real state, right now
        if not s['ok']:
            return {'ok': False, 'error': s['error']}
        r = self._send('claim', s['to'], s['data'], 0, s['gas'],
                       {'amount_wei': int(amount), 'claimable_wei': ctx['claimable']}, book)
        return {'ok': r['state'] == 'mined', 'error': None if r['state'] == 'mined' else r['state'], **r}

    def buy(self, venue, amount, ctx, book):
        c = self.cfg
        meta = due_meta(ctx)
        if meta['preview']:
            raise LiveRefused('a preview buy (budget not set) is never sent')
        room, cap = self.ledger.room(self.sim.clock(), meta['window'])
        if amount > room or amount > self.ledger.pending:
            raise LiveRefused(f"buy of {amount} wei is over the {cap} cap or the hour's booked buy")
        s = self.sim.buy(venue, amount, ctx['timestamp'], None)  # real state, no override, fresh quote
        if not s['ok']:
            return {'ok': False, 'error': s['error']}
        gp = self.sim.gas_price()
        worst = int(s['gas'] * GAS_MULT) * gp * FEE_MULT
        if amount + worst > self.ledger.budget() - c.gas_reserve_wei:
            return {'ok': False, 'skip': True, 'public': 'waiting for creator fees',
                    'error': 'the claimed fees minus the gas reserve cannot pay it'}
        share = gas_share_bps(s['gas'] * gp, amount)
        if share > c.max_gas_share_bps:
            return {'ok': False, 'skip': True, 'public': 'gas too large a share of the buy',
                    'error': f'gas would be {share / 100:.1f}% of the buy'}
        r = self._send('buy', s['to'], s['data'], amount, s['gas'],
                       {'venue': venue, 'min_out_wei': s['min_out'], 'deadline': s['deadline'],
                        'tokens_sim_wei': s['tokens_out'], 'window': meta['window'],
                        'hits_covered': meta['hits_covered'], 'hit_rate': meta['hit_rate']}, book)
        return {'ok': r['state'] == 'mined', 'error': None if r['state'] == 'mined' else r['state'], **r}


# ---------------------------------------------------------------------------------------------------- the engine
class Engine:
    def __init__(self, cfg, mode, journal, rpc, executor, sim, accept_test=False, clock=time.time, relay_url=RELAY_URL):
        self.cfg, self.mode, self.journal, self.rpc, self.executor, self.sim = cfg, mode, journal, rpc, executor, sim
        self.clock = clock
        self.lock = threading.RLock()
        self.tick_lock = threading.Lock()
        self.ledger = Ledger(cfg)
        recs = journal.records()
        if mode == 'LIVE' and journal.bad_lines:
            raise LiveRefused(f'{journal.path} has {journal.bad_lines} unreadable line(s): a damaged record could hide '
                              f'a real transaction; an operator must repair it by hand first')
        self.ledger.replay(recs, mode)
        if isinstance(executor, LiveExecutor):
            executor.ledger = self.ledger
        self.counter = HitCounter(cfg.tasks, cfg.max_hits_per_episode, accept_test, seen=self.ledger.seen)
        self.accept_test = bool(accept_test)
        self.started = clock()
        if self.ledger.since is None:
            self.ledger.since = self.started
        self.win_start = window_start(self.started, cfg.window_s)     # the open window: the UTC hour now
        self.retry_at = 0.0
        self.skip_public = None
        self.stopped = None
        self.stopped_public = None
        self.failures = 0
        if mode == 'LIVE':      # a LIVE stop and the failure count survive restarts (cleared only by --clear-stop)
            self.failures = self.ledger.failures
            if self.ledger.stop_rec:
                self.stopped = self.ledger.stop_rec.get('why') or 'stopped'
                self.stopped_public = self.ledger.stop_rec.get('public') or 'stopped'
        self.note = 'starting'
        self.relay = {'url': relay_url, 'connected': False}
        self.changed = True
        cfg_rec = {f.name: getattr(cfg, f.name) for f in fields(cfg)}
        self.journal.append({'mode': mode, 'ev': 'start', 'relay': relay_url, 'accept_test_streams': bool(accept_test),
                             'config': cfg_rec, 'rule': BUDGET_RULE, 'preview': cfg.preview,
                             'buyer': getattr(sim, 'buyer', WALLET),
                             'fee_funded': bool(getattr(executor, 'fee_funded', True)),
                             'resumed': {'hits': self.ledger.hits, 'pending_wei': self.ledger.pending,
                                         'buys': self.ledger.n_buys, 'claims': self.ledger.n_claims,
                                         'open_windows': sorted(self.ledger.windows)}})

    # ---- the relay side (the listener thread)
    def on_relay_connected(self, up):
        with self.lock:
            self.relay['connected'] = bool(up)
            if not up:
                self.counter.live = False
            self.changed = True

    def on_relay_text(self, text):
        with self.lock:
            for ev in self.counter.on_text(text):
                self._on_event(ev)
            self.changed = True

    def on_relay_bytes(self, data):
        with self.lock:
            before = dict(self.counter.attempt)
            self.counter.on_bytes(data)
            if self.counter.attempt != before:
                self.changed = True

    def _on_event(self, ev):
        kind = ev['ev']
        if kind == 'episode':
            # booked into the window it arrives in; a clock that stepped back gives it to the open window
            w = max(window_start(self.clock(), self.cfg.window_s), self.win_start)
            rec = self.journal.append({'mode': self.mode, 'ev': 'hit', **{k: ev[k] for k in (
                'key', 'run', 'task', 'started', 'n', 'hits', 'presses', 'misses', 'fell', 'via', 'test', 'partial',
                'w_hits', 'w_misses', 'w_wrong')}, **{k: ev[k] for k in ('missed', 'wrong', 'missed_from') if k in ev},
                'window': w, 'window_at': iso(w)})
            self.ledger.apply(rec)
            c = self.ledger.win_counts(w)
            log(f"attempt {ev['n']} ({ev['task']}, run {ev['run']}): "
                + (f"{ev['hits']} hits, joined mid-song (missed tiles unknown): not in the hit rate"
                   if ev['partial'] else f"{ev['w_hits']} hits, {ev['w_misses']} misses, {ev['w_wrong']} wrong")
                + f"; this hour {c['hits']}/{c['misses']}/{c['wrong']}")
        elif kind in ('gap', 'rejected'):
            self.journal.append({'mode': self.mode, **ev})
            log(f"{kind}: {ev.get('why') or ev.get('missed')}")
        elif kind == 'session':
            if not ev['countable']:
                what = f"not counted ({ev['why']})"
            elif self.counter.live:
                what = 'live: counting hits'
            else:
                what = 'the relay says it is not live (a run that ended): nothing to count'
            log(f"training session {ev['run']} ({ev['task']}): {what}")

    # ---- booking (every claim / buy / gas goes through here, DRY and LIVE)
    def _book(self, kind, info):
        with self.lock:
            rec = self.journal.append({'mode': self.mode, 'ev': kind, **info})
            self.ledger.apply(rec)
            self.changed = True
        if kind == 'claim':
            log(f"{'simulated ' if info.get('simulated') else ''}claim of {eth_str(info['amount_wei'])} ETH creator fees "
                f"(gas {info['gas']})")
        elif kind == 'buy':
            log(f"{'simulated ' if info.get('simulated') else ''}{'preview ' if info.get('preview') else ''}buy for "
                f"the hour {iso(info['window']) if info.get('window') is not None else '?'}: "
                f"{eth_str(info['amount_wei'])} ETH -> {token_str(info['tokens_wei'])} LABRAT on the {info['venue']} "
                f"(gas {info['gas']}, {info['gas_share_bps'] / 100:.1f}% of the buy)")

    def add_pons_session(self, obj):
        """The runner's report of the rat's pons session for one simulated buy (see PONS_LABEL). Validated by
        pons_session_record, journalled, and shown on that buy in the public status. -> the journal record; raises
        PonsRefused. It changes no cap, no pending amount and no figure the engine computed."""
        with self.lock:
            rec = pons_session_record(obj, self.ledger, self.clock(), self.mode)
            r = self.journal.append({'mode': self.mode, 'ev': 'pons_session', **rec})
            self.ledger.apply(r)
            self.changed = True
        log(f"the rat's pons session for the simulated buy of {rec['buy_at']}: {eth_str(rec['amount_wei'])} ETH -> "
            f"{token_str(rec['tokens_wei'])} LABRAT (simulated), replay {rec.get('replay') or 'not reported'}")
        return r

    def _stop(self, why, public):
        with self.lock:
            self.stopped, self.stopped_public = why, public
            self.skip_public = None
            self.changed = True
        try:        # journalled so a LIVE restart stays stopped until --clear-stop; a failing journal still stops
            self.journal.append({'mode': self.mode, 'ev': 'stop', 'why': why, 'public': public})
        except OSError as e:
            log(f'the stop could not be journalled ({type(e).__name__})')
        log(f'STOPPED: {why}')

    def _skip(self, why, retry_s, public, ev='buy_skip', **extra):
        """Wait retry_s, then try the hour's buy again (until the next hour closes). `public`: the short fixed
        reason the status may show."""
        self.note = why
        self.retry_at = self.clock() + retry_s
        self.skip_public = public
        self.changed = True
        self.journal.append({'mode': self.mode, 'ev': ev, 'why': why, **extra})
        log(f'{ev}: {why}')

    def _nobuy(self, due, why, public):
        """The hour's buy is dropped for good (e.g. gas too large a share of it, a cap reached): journalled."""
        with self.lock:
            r = self.journal.append({'mode': self.mode, 'ev': 'window_nobuy', 'window': due['start_t'], 'why': why,
                                     'public': public})
            self.ledger.apply(r)
            self.note = why
            self.skip_public = None
            self.retry_at = 0.0
            self.changed = True
        log(f"no buy for the hour {due['start']}: {why}")

    def _failed(self, what, err):
        self.failures += 1
        self.journal.append({'mode': self.mode, 'ev': f'{what}_failed', 'error': err, 'consecutive': self.failures})
        log(f'{what} failed: {err}')
        if self.failures >= self.cfg.max_failures:
            self._stop(f'{self.failures} failed claims / buys in a row (last: {err})', 'repeated failures')
        else:
            self.retry_at = self.clock() + 120
            self.skip_public = 'retrying after a failed attempt'
            self.changed = True

    # ---- the hour windows (the main thread)
    def _roll(self, now):
        """Close every window that has ended: the open one, and any the journal holds that were never closed (this
        program was not running at their end). Only the window that ended last can still buy; older ones close late."""
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
        if L.due is not None:              # the previous hour's buy never happened: the next hour replaces it
            old = L.due
            r = self.journal.append({'mode': self.mode, 'ev': 'buy_expired', 'window': old['start_t'],
                                     'amount_wei': old['amount_wei'], 'why': 'the next hour closed before it was bought',
                                     'public': 'the next hour closed first'})
            L.apply(r)
            log(f"the buy for the hour {old['start']} expired (the next hour closed first)")
        w = L.win_counts(s)
        rate = hit_rate(w['hits'], w['misses'], w['wrong'])
        base = c.base_wei
        raw = hour_amount(base, w['hits'], w['misses'], w['wrong'])
        amount, cap, note = raw, None, None
        if late:
            amount, note = 0, 'closed late; the engine was not running at the hour'
        elif w['hits'] + w['misses'] + w['wrong'] == 0:
            amount, note = 0, 'no attempts in the hour' if not w['attempts'] else 'no hits or misses in the hour'
        elif w['hits'] == 0:
            amount, note = 0, 'no hits in the hour'
        elif self.mode == 'LIVE' and c.preview:
            amount, note = 0, 'budget not set'
        else:
            room, capname = (c.max_buy_wei, 'per-buy') if c.preview else L.room(now, s)
            if amount > room:
                amount, cap = room // AMOUNT_STEP_WEI * AMOUNT_STEP_WEI, capname
            if amount < c.min_buy_wei:
                note = f'the {cap} cap is reached' if cap and room < c.min_buy_wei \
                    else 'hit rate too low for the minimum buy'
                amount = 0
        rec = {'mode': self.mode, 'ev': 'window', 'start': iso(s), 'start_t': s, 'end': iso(s + c.window_s),
               'hits': w['hits'], 'misses': w['misses'], 'wrong': w['wrong'], 'attempts': w['attempts'],
               'hit_rate': rate_str(rate), 'budget_wei': c.hourly_budget_wei, 'preview': c.preview, 'base_wei': base,
               'raw_wei': raw, 'amount_wei': amount, 'cap': cap, 'late': late, 'note': note, 'rule': BUDGET_RULE}
        L.apply(self.journal.append(rec))
        self.retry_at = 0.0
        self.skip_public = None
        self.changed = True
        rate_txt = 'none' if rate is None else f'{rate * 100:.1f}%'
        log(f"hour {iso(s)} closed: {w['hits']} hits, {w['misses']} misses, {w['wrong']} wrong, hit rate {rate_txt} "
            f"-> {'preview ' if c.preview else ''}buy {eth_str(amount)} ETH" + (f' ({note})' if note else '')
            + (f' (cut to the {cap} cap)' if cap and amount else ''))

    def tick(self):
        if not self.tick_lock.acquire(blocking=False):
            return
        try:
            now = self.clock()
            try:
                self._roll(now)
            except OSError as e:
                return self._stop(f'the journal could not be written ({type(e).__name__})', 'the buyback journal failed')
            with self.lock:
                due = self.ledger.due
                if self.stopped or now < self.retry_at:
                    return
                if due is None:
                    self.note = 'waiting for the hour'
                    return
                due = dict(due)
            self._batch(now, due)
        finally:
            self.tick_lock.release()

    def _batch(self, now, due):
        try:
            return self._batch_inner(now, due)
        except OSError as e:
            # the journal could not be written (disk full, a reader holding the file, ...): stop, so nothing is
            # claimed or bought that could go unbooked; a LIVE restart books any mined transaction from its receipt
            return self._stop(f'the journal could not be written ({type(e).__name__})', 'the buyback journal failed')

    def _batch_inner(self, now, due):
        c = self.cfg
        amount, preview = int(due['amount_wei']), bool(due['preview'])
        fee_funded = bool(getattr(self.executor, 'fee_funded', True))
        try:
            if isinstance(self.executor, LiveExecutor):
                if not self.executor.resolve(self._book):
                    return self._skip('an earlier transaction is still unresolved', 30,
                                      'an earlier transaction is unconfirmed')
                with self.lock:            # resolve() may just have booked this hour's buy
                    if self.ledger.due is None or self.ledger.due['start_t'] != due['start_t']:
                        return None
            chk = check_chain(self.rpc, c.venue)
        except (RpcError, RuntimeError, KeyError, ValueError) as e:
            return self._skip(f'chain read failed ({type(e).__name__}: {e}); retrying', 60, 'chain read failed; retrying')
        self.journal.append({'mode': self.mode, 'ev': 'check', **{k: chk[k] for k in (
            'ok', 'venue', 'phase', 'graduated', 'block', 'problems')}})
        if chk['stop']:
            return self._stop('; '.join(chk['problems']), chk['public'])
        venue = chk['venue']
        try:
            gp = self.sim.gas_price()
            if gp > c.max_gas_price_wei:
                return self._skip(f'gas price {gp / 1e9:.3f} gwei is over the {c.max_gas_price_wei / 1e9:g} gwei cap',
                                  300, 'gas price over the cap')
            reserve_buy = int(GAS_GUESS[venue] * GAS_MULT) * gp * FEE_MULT
            claim_gas = int(GAS_GUESS['claim'] * GAS_MULT) * gp * FEE_MULT
            gas_room, gas_cap = None, None
            if not preview:                # a preview uses no cap: only real-budget buys are held to them
                with self.lock:
                    gas_room, gas_cap = self.ledger.gas_room(now)
                    room, cap = self.ledger.room(now, due['start_t'])
                if gas_room < reserve_buy:
                    return self._skip(f'the {gas_cap} cap is reached', 600, f'{gas_cap} cap reached')
                if amount > room:
                    amount = room // AMOUNT_STEP_WEI * AMOUNT_STEP_WEI
                    if amount < c.min_buy_wei:
                        return self._nobuy(due, f'the {cap} cap is reached', f'{cap} cap reached')
            ctx = {'block': chk['block'], 'timestamp': chk['timestamp'], 'gas_price': gp, 'claimable': None,
                   'window': due['start_t'], 'hits': due['hits'], 'hit_rate': due['hit_rate'], 'preview': preview}
            if fee_funded:
                # LIVE (paused): paid from the launch wallet's claimed creator fees. 1. claim first when the claimed,
                # unspent fees cannot pay this buy
                with self.lock:
                    budget = self.ledger.budget()
                if budget - c.gas_reserve_wei - reserve_buy < amount:
                    if gas_room is not None and gas_room < reserve_buy + claim_gas:
                        return self._skip(f'the {gas_cap} cap leaves no room for a claim and a buy', 600,
                                          f'{gas_cap} cap reached')
                    escrow = self.sim.claimable()
                    unclaimed = escrow - (self.ledger.claimed if self.mode == 'DRY' else 0)   # DRY claims: virtual
                    ctx['claimable'] = unclaimed
                    if unclaimed < c.claim_min_wei:
                        self.journal.append({'mode': self.mode, 'ev': 'claim_skip', 'claimable_wei': unclaimed,
                                             'why': f'claimable fees are under the {eth_str(c.claim_min_wei)} ETH '
                                                    f'threshold'})
                    else:
                        r = self.executor.claim(min(unclaimed, c.claim_max_wei), ctx, self._book)
                        if not r['ok']:
                            return self._failed('claim', r['error'])
                # 2. the buy, cut to what the claimed fees can pay
                with self.lock:
                    budget = self.ledger.budget()
                room = budget - c.gas_reserve_wei - reserve_buy
                if room < amount:
                    amount = max(0, room) // AMOUNT_STEP_WEI * AMOUNT_STEP_WEI
                if amount < c.min_buy_wei:
                    return self._skip('not enough claimed creator fees for the minimum buy', 300,
                                      'waiting for creator fees', budget_wei=budget)
                ctx['budget'] = budget
            r = self.executor.buy(venue, amount, ctx, self._book)
        except NonceMismatch as e:
            return self._stop(f'LIVE: {e}', 'stopped for an operator check')
        except LiveRefused as e:
            return self._skip(f'LIVE held back: {e}', 60, 'held back; retrying')
        except (RpcError, RuntimeError, KeyError, ValueError) as e:
            return self._failed('batch', f'{type(e).__name__}: {e}')
        if r.get('skip'):
            if r.get('final'):
                return self._nobuy(due, r['error'], r.get('public') or 'no buy this hour')
            return self._skip(r['error'], 120, r.get('public') or 'waiting; retrying')
        if not r['ok']:
            return self._failed('buy', r['error'])
        self.failures = 0
        self.skip_public = None
        self.retry_at = 0.0
        self.note = 'bought' if self.mode == 'LIVE' else ('simulated a preview buy' if preview else 'simulated a buy')

    # ---- status
    def next_buy(self, now):
        """-> (seconds until the next buy can happen or None, a short fixed public reason). A buy happens once an
        hour, when the hour closes: never "due now" while a cap, a skip or a stop blocks it."""
        L, c = self.ledger, self.cfg
        if self.stopped:
            return None, 'stopped'
        if L.total_done():
            return None, 'total cap reached'
        if L.due is not None:
            if now < self.retry_at:
                return int(self.retry_at - now) + 1, self.skip_public or 'retrying'
            return 0, 'due'
        end = max(self.win_start, window_start(now, c.window_s)) + c.window_s
        if not c.preview:
            gas_room, gas_cap = L.gas_room(end)
            if gas_room <= 0:
                return None, f'{gas_cap} cap reached'
            room, cap = L.room(end, end - c.window_s)
            if room < c.min_buy_wei:
                return None, f'{cap} cap reached'
        return max(0, int(end - now)), 'waiting for the hour'

    def next_buy_at(self, now, wait):
        if wait is None:
            return None
        return iso(now + wait)

    def rule_text(self):
        c = self.cfg
        what = []
        if 'cursor' in c.tasks or 'steer' in c.tasks:
            what.append('in the steering tasks a hit is a click on the lit target and a miss a click off it')
        if 'tiles' in c.tasks:
            what.append('in Rat Tiles a hit is a tile tapped in time, a miss a tile that slid past untapped and a '
                        'wrong click a press with no tile to tap')
        if 'lever' in c.tasks:
            what.append('in the lever task, each clean press is a hit')
        budget = (f'The hourly budget is {eth_str(c.hourly_budget_wei)} ETH, paid from {FUNDING}.' if not c.preview
                  else f'The hourly budget is not set yet: until it is, each hour runs a simulated preview buy of '
                       f'{eth_str(c.preview_budget_wei)} ETH x the hit rate.')
        return (f'Once an hour, on the hour (UTC), the engine looks at the hour the rat just played in the live view '
                f'(the newest saved training checkpoint, playing in its own simulation): its hits, misses and wrong '
                f'clicks ({"; ".join(what)}). The hit rate is hits / (hits + misses + wrong clicks), and that hour\'s '
                f'$LABRAT buyback is the hourly budget x the hit rate, cut to the per-buy, hourly, daily and total '
                f'caps; an hour whose buy would be under {eth_str(c.min_buy_wei)} ETH buys nothing. {budget} The code '
                f'sets this rule and the caps; the rat\'s hits only trigger it. The rat does not understand money.')

    def public_status(self):
        """What the website may show: no wallet address, no creator-fee amounts, no transaction data."""
        c = self.cfg
        with self.lock:
            L = self.ledger
            now = self.clock()
            due, due_why = self.next_buy(now)
            s = self.counter.session
            public_relay = self.relay['url'] == RELAY_URL
            test_stream = bool(s and s['test'])
            stopped = self.stopped_public or ('the total cap is reached: no more buybacks' if L.total_done() else None)
            cur = max(self.win_start, window_start(now, c.window_s))
            w = L.win_counts(cur)
            rate = hit_rate(w['hits'], w['misses'], w['wrong'])
            projected = min(hour_amount(c.base_wei, w['hits'], w['misses'], w['wrong']), c.max_buy_wei)
            pending_hits = w['hits'] + (L.due_hits if L.due is not None else 0)
            return {
                'mode': self.mode,
                'label': LIVE_LABEL if self.mode == 'LIVE' else DRY_LABEL,
                'rule': self.rule_text(),
                'per_hit_eth': None,              # no per-hit amount since the hourly rule (kept for old readers)
                'tasks': list(c.tasks),
                'hits': {'counted': L.hits, 'attempts_with_hits': L.hit_episodes,
                         'in_buys': L.hits_bought, 'pending': pending_hits,
                         'over_caps': max(0, L.hits - L.hits_bought - pending_hits),
                         'since': iso(L.since) if L.since else None},
                'pending': {'eth': eth_str(L.pending), 'hits': pending_hits},
                'this_attempt': {'clicks': self.counter.attempt['clicks'], 'on_target': self.counter.attempt['on_target'],
                                 'note': 'unconfirmed until the attempt ends'},
                'relay': {'connected': self.relay['connected'], 'live': self.counter.live,
                          'run': s['run'] if s else None, 'task': s['task'] if s else None,
                          'counting': bool(s and s['countable'] and self.counter.live),
                          'test_stream': test_stream},
                'source': {'public_relay': public_relay, 'accept_test_streams': self.accept_test,
                           'test': (not public_relay) or self.accept_test or test_stream},
                'buys': {'count': L.n_buys, 'eth_in': eth_str(L.bought), 'labrat_out': token_str(L.tokens),
                         'simulated': self.mode != 'LIVE', 'recent': [dict(e) for e in L.recent][::-1],
                         'pons_sessions': L.n_pons, 'previews': L.n_previews},
                'next_buy_in_s': due,
                'next_buy_note': due_why,
                'next_buy_at': self.next_buy_at(now, due),
                'window': {'start': iso(cur), 'end': iso(cur + c.window_s), 'hits': w['hits'], 'misses': w['misses'],
                           'wrong': w['wrong'], 'hit_rate': rate_str(rate), 'attempts': w['attempts'],
                           'preview': c.preview, 'projected_eth': eth_str(projected)},
                'last_window': copy.deepcopy(L.last_window),
                'budget': {'hourly_eth': None if c.preview else eth_str(c.hourly_budget_wei), 'rule': BUDGET_RULE,
                           'set': not c.preview, 'preview_eth': eth_str(c.preview_budget_wei) if c.preview else None,
                           'funding': FUNDING},
                'caps': {'min_buy_eth': eth_str(c.min_buy_wei), 'per_buy_eth': eth_str(c.max_buy_wei),
                         'per_hour_eth': eth_str(c.max_hour_wei), 'per_day_eth': eth_str(c.max_day_wei),
                         'total_eth': eth_str(c.max_total_wei), 'max_pending_eth': None,
                         'gas_per_day_eth': eth_str(c.max_gas_day_wei), 'gas_total_eth': eth_str(c.max_gas_total_wei)},
                'stopped': stopped,
                'updated': iso(now),
            }


# ---------------------------------------------------------------------------------------------------- the relay
STABLE_S = 30.0    # a relay connection counts as healthy (backoff back to 1 s) only after staying up this long


def reconnect_wait(backoff, up_s):
    """After a connection that was up for up_s -> (the wait before reconnecting, the next backoff). Only a connection
    that stayed up STABLE_S resets the backoff to 1 s; one that closed sooner (a full relay closing right after
    accepting, a per-address cap, a restart mid-handshake) keeps doubling it up to 30 s, so the engine never
    reconnects every second."""
    if up_s >= STABLE_S:
        backoff = 1.0
    return backoff, min(backoff * 2, 30.0)


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
                             max_size=2 ** 20, ping_interval=20, ping_timeout=20, user_agent_header='labrat-buyback/1')
            except Exception as e:
                err = f'{type(e).__name__}: {e}'
                if err != last_err:
                    log(f'cannot reach the relay ({err}); retrying')
                last_err = err
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            self.connects += 1
            if backoff <= 2.0:                     # quiet while a flapping relay is being backed off
                log(f'listening to {self.url}')
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
            wait, backoff = reconnect_wait(backoff, time.monotonic() - t_up)
            self.stop.wait(wait)


# ---------------------------------------------------------------------------------------------------- status HTTP
def _bearer(header):
    parts = str(header or '').split(None, 1)
    if len(parts) != 2 or parts[0].lower() != 'bearer' or not parts[1].strip():
        return None
    return parts[1].strip()


def _same_secret(a, b):
    return hmac.compare_digest(hashlib.sha256(a.encode('utf-8')).digest(), hashlib.sha256(b.encode('utf-8')).digest())


def serve_status(engine, host, port, rig_token=None):
    """GET /status (public), GET /healthz. With rig_token (DRY only, >= RIG_TOKEN_MIN characters): also POST
    /pons_session for the buy rig's runner (Authorization: Bearer <token>, a JSON body of at most PONS_BODY_MAX
    bytes, validated by pons_session_record). Browsers cannot call it: it needs an Authorization header, and
    the CORS preflight that would need is not answered."""
    rig_token = rig_token if (rig_token and len(rig_token) >= RIG_TOKEN_MIN and engine.mode == 'DRY') else None

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
            elif path == '/healthz':
                self._send(200, {'ok': True})
            else:
                self._send(404, {'error': 'not found'})

        def do_POST(self):
            path = self.path.split('?', 1)[0]
            if path != '/pons_session' or not rig_token:
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
            if not 0 < n <= PONS_BODY_MAX:
                self.close_connection = True
                return self._send(413 if n > PONS_BODY_MAX else 411, {'error': f'a JSON body of 1..{PONS_BODY_MAX} '
                                                                               'bytes with a Content-Length'})
            try:
                obj = json.loads(self.rfile.read(n).decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                return self._send(400, {'error': 'the body is not JSON'})
            try:
                rec = engine.add_pons_session(obj)
            except PonsRefused as e:
                return self._send(e.status, {'error': str(e)[:200]})
            except OSError as e:
                return self._send(503, {'error': f'the journal could not be written ({type(e).__name__})'})
            return self._send(200, {'ok': True, 'buy_at': rec['buy_at'], 'label': PONS_LABEL})

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name='status-http', daemon=True).start()
    return srv


# ---------------------------------------------------------------------------------------------------- LIVE gate
def _env_file_reader():
    """The .env FILE, read the launcher's way (launcher.read_env_file). LIVE only."""
    return launcher.read_env_file()


def read_env():
    STATE['env_read'] = True
    try:
        return _env_file_reader()
    except launcher.LaunchRefused:
        raise
    except Exception as e:      # never the exception's text: a decode error's repr holds the raw .env bytes
        raise launcher.LaunchRefused(f'.env could not be read ({type(e).__name__})') from None


def lock_path(journal_dir):
    return os.path.join(journal_dir, 'live.lock')


def live_journal_path(journal_dir):
    return os.path.join(journal_dir, 'journal_live.jsonl')


def nonce_problem(ledger, latest, pending):
    """None when the launch wallet's on-chain nonce is what the LIVE journal accounts for, else why not. Every
    journalled transaction used one nonce, consecutively from ledger.nonce_base (LIVE_FIRST_NONCE, or --first-nonce)."""
    base, nonces = ledger.nonce_base, sorted(ledger.signed_nonces)
    if nonces != list(range(base, base + len(nonces))):
        return f'the LIVE journal\'s nonces {nonces[:5]}... are not consecutive from {base}'
    exp = ledger.next_nonce()
    lo = min(int(r['nonce']) for r in ledger.unresolved.values()) if ledger.unresolved else exp
    if lo <= latest <= exp and latest <= pending <= exp:
        return None
    return (f'the launch wallet is at nonce {latest} (pending {pending}) but the LIVE journal accounts for {exp}: a '
            f'lost or foreign journal, another engine, or a transaction sent from the wallet outside this engine. '
            f'Nothing is signed until an operator has looked (--first-nonce only before the first LIVE transaction)')


def live_gate(a, cfg, journal_dir, transport=None, nodes=None):
    """Every LIVE gate, in order. -> (LiveRpc, account, lock file). SystemExit on the first one that fails, before
    anything is signed or sent."""
    def no(why):
        raise SystemExit(f'LIVE refused, nothing was signed or sent: {why}')
    if not a.live:
        no('--live was not given')
    if a.confirm != SYMBOL:
        no(f'--confirm must be {SYMBOL} (got {a.confirm!r})')
    if not LIVE_BUYER_READY:
        no('LIVE is paused: the buys are now paid from the separate buyback wallet the owner funds by hand, and this '
           'LIVE path signs from the launch wallet; no LIVE path (and no key handling) exists for the buyback wallet')
    if cfg.hourly_budget_wei is None:
        no('--hourly-budget-eth is not set: LIVE never runs a preview')
    if cfg.window_s != HOUR_S:
        no('LIVE buys once per UTC hour (no --window-s)')
    if a.accept_test_streams:
        no('--accept-test-streams is for DRY tests only: a test stream can never trigger a real buy')
    if a.relay != RELAY_URL or a.origin != ORIGIN:
        no(f'LIVE listens only to the public relay {RELAY_URL} as {ORIGIN}')
    if a.journal_dir is not None:
        no(f'--journal-dir is refused in LIVE: its journal, caps and lock live only in {LIVE_JOURNAL_DIR}')
    if os.environ.get('RATBRAIN_RPC'):
        no('RATBRAIN_RPC is set: LIVE reads, simulates and sends only through the pinned public RPCs; unset it')
    try:
        env = read_env()
    except launcher.LaunchRefused as e:
        no(str(e))
    for k in ('BUYBACK_LIVE', 'BUYBACK_RH_KEY', 'RATBRAIN_RH_KEY'):
        if k in os.environ and os.environ[k] != env.get(k, os.environ[k]):
            no(f'{k} in the shell environment differs from .env; unset it')
    if env.get('BUYBACK_LIVE') != '1':
        no('.env does not say BUYBACK_LIVE=1')
    key = env.get('BUYBACK_RH_KEY') or env.get('RATBRAIN_RH_KEY') or ''
    env = None
    if not key:
        no('.env has no BUYBACK_RH_KEY (or RATBRAIN_RH_KEY)')
    try:
        from eth_account import Account
        acct = Account.from_key(key)
    except Exception:
        acct = None
    key = None
    if acct is None:
        no('the .env key does not parse as a private key')
    if acct.address != WALLET:
        no('the .env key is not the launch wallet pinned in live/buyback.py')
    rpc = LiveRpc(transport, _gate=_GATE_PASSED, nodes=nodes)
    try:
        cid = int(rpc.ok('eth_chainId', []), 16)
        if cid != CHAIN_ID:
            no(f'chain id {cid}, expected {CHAIN_ID}')
        chk = check_chain(rpc, cfg.venue)
    except (RpcError, RuntimeError) as e:
        no(f'chain read failed: {e}')
    if chk['stop']:
        no('pinned chain checks failed: ' + '; '.join(chk['problems']))
    lp = lock_path(journal_dir)
    os.makedirs(journal_dir, exist_ok=True)
    try:
        fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        no(f'{lp} exists: another LIVE buyback runs (or one crashed: check, then delete the lock by hand)')
    with os.fdopen(fd, 'w') as f:
        f.write(json.dumps({'pid': os.getpid(), 'at': iso()}))
    try:
        # the journal, under the lock: readable, not stopped, and in step with the wallet's nonce on chain
        journal = Journal(live_journal_path(journal_dir))
        recs = journal.records()
        if journal.bad_lines:
            no(f'{journal.path} has {journal.bad_lines} unreadable line(s): a damaged record could hide a real '
               f'transaction; repair it by hand first')
        ledger = Ledger(cfg)
        ledger.replay(recs, 'LIVE')
        if ledger.stop_rec:
            if not a.clear_stop:
                no(f"the last LIVE run stopped ({ledger.stop_rec.get('why')}); check it, then start with --clear-stop")
        try:
            latest, pending = (int(rpc.ok('eth_getTransactionCount', [WALLET, t]), 16) for t in ('latest', 'pending'))
        except (RpcError, RuntimeError) as e:
            no(f'chain read failed: {e}')
        if a.first_nonce is not None:
            if ledger.signed_nonces or ledger.anchored:
                no('--first-nonce is only for the first LIVE run (the LIVE journal already has transactions)')
            if not a.first_nonce == latest == pending:
                no(f'--first-nonce {a.first_nonce} is not the launch wallet\'s nonce on chain ({latest}, pending '
                   f'{pending})')
        else:
            why = nonce_problem(ledger, latest, pending)
            if why:
                no(why)
        if a.first_nonce is not None:
            journal.append({'mode': 'LIVE', 'ev': 'anchor', 'nonce': int(a.first_nonce),
                            'why': 'operator --first-nonce: the wallet sent transactions outside this engine before '
                                   'its first LIVE run'})
        if ledger.stop_rec and a.clear_stop:
            journal.append({'mode': 'LIVE', 'ev': 'stop_cleared', 'cleared': ledger.stop_rec.get('why'),
                            'why': 'operator --clear-stop'})
    except BaseException:
        os.remove(lp)
        raise
    return rpc, acct, lp


# ---------------------------------------------------------------------------------------------------- main
BUDGET_ENV = 'BUYBACK_HOURLY_BUDGET_ETH'     # the hourly budget from the PROCESS environment (never .env)
UNSET = ('unset', 'none', 'null', '')


def parse_budget(v):
    """--hourly-budget-eth: an ETH amount, or unset / none (-> None: preview buys)."""
    if v is None or str(v).strip().lower() in UNSET:
        return None
    return parse_eth(v)


def build_config(a):
    kw = {}
    for flag, name in (('min_buy_eth', 'min_buy_wei'), ('max_buy_eth', 'max_buy_wei'),
                       ('max_hour_eth', 'max_hour_wei'), ('max_day_eth', 'max_day_wei'),
                       ('max_total_eth', 'max_total_wei'), ('gas_reserve_eth', 'gas_reserve_wei'),
                       ('claim_min_eth', 'claim_min_wei'), ('claim_max_eth', 'claim_max_wei'),
                       ('preview_budget_eth', 'preview_budget_wei')):
        v = getattr(a, flag)
        if v is not None:
            kw[name] = parse_eth(v)
    budget = a.hourly_budget_eth if a.hourly_budget_eth is not None else os.environ.get(BUDGET_ENV)
    kw['hourly_budget_wei'] = parse_budget(budget)
    if a.max_gas_gwei is not None:
        kw['max_gas_price_wei'] = int(Decimal(str(a.max_gas_gwei)) * 10 ** 9)
    for flag in ('slippage_bps', 'deadline_s', 'max_gas_share_bps', 'venue', 'window_s'):
        v = getattr(a, flag)
        if v is not None:
            kw[flag] = v
    if a.tasks:
        kw['tasks'] = tuple(t.strip() for t in a.tasks.split(',') if t.strip())
    return Config(**kw).validate()


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description='$LABRAT rat buybacks: one buy an hour, sized by the hit rate; DRY by '
                                             'default (simulated, never sent).')
    ap.add_argument('--live', action='store_true', help='LIVE (refused: paused; see the docstring)')
    ap.add_argument('--confirm', help=f'LIVE only: must be {SYMBOL}')
    ap.add_argument('--check', action='store_true', help='one read-only check: the pins and a simulated buy')
    ap.add_argument('--relay', default=RELAY_URL)
    ap.add_argument('--origin', default=ORIGIN)
    ap.add_argument('--accept-test-streams', action='store_true', help='DRY only: count a TEST stream (local tests)')
    ap.add_argument('--journal-dir', default=None,
                    help=f'DRY only (default {DEFAULT_JOURNAL_DIR}); LIVE refuses it: its journal has one fixed place')
    ap.add_argument('--first-nonce', type=int, default=None,
                    help="LIVE, first run only: the launch wallet's current nonce, when it sent transactions outside "
                         f'this engine after the launch (the journal otherwise starts at nonce {LIVE_FIRST_NONCE})')
    ap.add_argument('--clear-stop', action='store_true',
                    help='LIVE: an operator checked why the last LIVE run stopped; journal that and run again')
    ap.add_argument('--status-port', type=int, default=0, help='serve the public status JSON (0 = off)')
    ap.add_argument('--status-host', default='127.0.0.1')
    ap.add_argument('--duration', type=float, default=0, help='stop after this many seconds (0 = run until Ctrl+C)')
    ap.add_argument('--tick', type=float, default=1.0, help=argparse.SUPPRESS)
    ap.add_argument('--window-s', type=int, default=None, help=argparse.SUPPRESS)   # tests only: a shorter "hour"
    ap.add_argument('--hourly-budget-eth', default=None,
                    help="the owner's hourly budget in ETH; each hour buys this x the hit rate (cut to the caps). "
                         '"unset" = not set yet: each hour runs a simulated preview buy of --preview-budget-eth x the '
                         f"hit rate. Without the flag: the process environment's {BUDGET_ENV}, else unset")
    ap.add_argument('--preview-budget-eth', default=None,
                    help='the nominal hourly amount of a preview buy while the budget is unset (default 0.001)')
    for f in ('min_buy_eth', 'max_buy_eth', 'max_hour_eth', 'max_day_eth', 'max_total_eth', 'gas_reserve_eth',
              'claim_min_eth', 'claim_max_eth'):
        ap.add_argument('--' + f.replace('_', '-'), dest=f)
    ap.add_argument('--slippage-bps', type=int)
    ap.add_argument('--deadline-s', type=int)
    ap.add_argument('--max-gas-gwei', type=float)
    ap.add_argument('--max-gas-share-bps', type=int)
    ap.add_argument('--venue', choices=('auto', 'curve', 'pool'))
    ap.add_argument('--tasks', help='comma list of lever,cursor,steer,tiles (default cursor,steer,tiles: the tasks '
                                    'with a lit target; lever counts clean presses, and the public rule then says so)')
    return ap.parse_args(argv)


def one_check(cfg):
    """--check: read-only, printed locally (not journalled, not published). The buy is simulated from the buyback
    wallet (its balance raised by a state override); the launch wallet's claimable fees are shown for reference."""
    rpc = ReadRpc()
    sim = Sim(rpc, cfg, buyer=BUYBACK_WALLET)
    chk = check_chain(rpc, cfg.venue)
    out = {'check': {k: chk[k] for k in ('ok', 'venue', 'phase', 'graduated', 'block', 'problems')}}
    gp = sim.gas_price()
    out['gas_price_gwei'] = gp / 1e9
    claimable = sim.claimable()
    out['claimable_eth'] = eth_str(claimable)
    if claimable >= cfg.claim_min_wei:
        c = sim.claim(min(claimable, cfg.claim_max_wei))
        out['claim_sim'] = {k: c[k] for k in ('ok', 'gas', 'error') if k in c}
    real = sim.balance()
    out['buyback_wallet_eth'] = eth_str(real)
    out['hourly_budget_eth'] = None if cfg.preview else eth_str(cfg.hourly_budget_wei)
    if chk['venue']:
        amt = cfg.min_buy_wei
        b = sim.buy(chk['venue'], amt, chk['timestamp'], {BUYBACK_WALLET: {'balance': hex(real + amt * 2)}})
        out['buy_sim'] = {'eth_in': eth_str(amt), **({'labrat_out': token_str(b['tokens_out']),
                          'min_out': token_str(b['min_out']), 'gas': b['gas'],
                          'gas_share_pct': gas_share_bps(b['gas'] * gp, amt) / 100, 'quote_checked': b['quote_checked']}
                          if b['ok'] else {'error': b['error']})}
    return out


def main(argv=None):
    a = parse_args(argv)
    try:
        cfg = build_config(a)
    except (ValueError, TypeError) as e:
        raise SystemExit(f'bad config: {e}')
    if a.check:
        print(json.dumps(one_check(cfg), indent=2))
        return 0
    live = bool(a.live or a.confirm or a.first_nonce is not None or a.clear_stop)
    lock = None
    if live:
        jdir = os.path.abspath(LIVE_JOURNAL_DIR)
        rpc, acct, lock = live_gate(a, cfg, jdir)
        mode = 'LIVE'
    else:
        jdir = os.path.abspath(a.journal_dir or DEFAULT_JOURNAL_DIR)
        rpc, acct, mode = ReadRpc(), None, 'DRY'
    try:
        journal = Journal(live_journal_path(jdir) if live else os.path.join(jdir, 'journal.jsonl'))
        sim = Sim(rpc, cfg, buyer=WALLET if live else BUYBACK_WALLET)
        if live:
            executor = LiveExecutor(rpc, acct, journal, Ledger(cfg), sim, cfg)
        else:
            executor = DryExecutor(sim, cfg)
        acct = None
        try:
            engine = Engine(cfg, mode, journal, rpc, executor, sim, accept_test=a.accept_test_streams,
                            relay_url=a.relay)
        except LiveRefused as e:
            raise SystemExit(f'LIVE refused, nothing was signed or sent: {e}') from None
        log(f'{mode}: {"LIVE: real claims and buys from the launch wallet" if live else DRY_LABEL}; journal '
            f'{journal.path}; {engine.ledger.hits} hits and {engine.ledger.n_buys} buys so far')
        if cfg.preview:
            log(f'hourly budget not set: each hour runs a simulated PREVIEW buy of {eth_str(cfg.preview_budget_wei)} '
                f'ETH x the hit rate')
        else:
            log(f'hourly budget {eth_str(cfg.hourly_budget_wei)} ETH x the hit rate, from {FUNDING}'
                + (f'; NOTE: the per-buy cap {eth_str(cfg.max_buy_wei)} ETH cuts it (--max-buy-eth)'
                   if cfg.hourly_budget_wei > cfg.max_buy_wei else ''))
        try:
            chk = check_chain(rpc, cfg.venue)
            engine.journal.append({'mode': mode, 'ev': 'check', **{k: chk[k] for k in (
                'ok', 'venue', 'phase', 'graduated', 'block', 'problems')}})
            log(f"chain: block {chk['block']}, phase {chk['phase']}, graduated {chk['graduated']} -> "
                + (f"buys go to the {chk['venue']}" if chk['ok'] else 'STOP: ' + '; '.join(chk['problems'])))
            if chk['stop']:
                engine._stop('; '.join(chk['problems']), chk['public'])
        except (RpcError, RuntimeError) as e:
            log(f'chain check failed at startup ({e}); it runs again before every buy')
        stop = threading.Event()
        listener = RelayListener(a.relay, a.origin, engine, stop).start()
        # the buy rig's reports (POST /pons_session): DRY only, and only with a token in the PROCESS environment
        rig_token = os.environ.get(RIG_TOKEN_ENV, '').strip() if not live else ''
        if rig_token and len(rig_token) < RIG_TOKEN_MIN:
            log(f'{RIG_TOKEN_ENV} is shorter than {RIG_TOKEN_MIN} characters: POST /pons_session stays off')
            rig_token = ''
        srv = serve_status(engine, a.status_host, a.status_port, rig_token or None) if a.status_port else None
        if srv:
            log(f'status on http://{a.status_host}:{a.status_port}/status'
                + (' (+ POST /pons_session for the buy rig)' if rig_token else ''))
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
            engine.journal.append({'mode': mode, 'ev': 'end', 'hits': L.hits, 'pending_wei': L.pending,
                                   'buys': L.n_buys, 'bought_wei': L.bought, 'tokens_wei': L.tokens,
                                   'claims': L.n_claims, 'stats': dict(engine.counter.stats)})
            log(f'end: {L.hits} hits counted, {L.n_buys} {"" if live else "simulated "}buys, '
                f'{eth_str(L.bought)} ETH -> {token_str(L.tokens)} LABRAT')
    finally:
        if lock and os.path.exists(lock):
            os.remove(lock)
    return 0


if __name__ == '__main__':
    sys.exit(main())
