"""LIVE for the buy rig (live/buyrig.py): the hour's booked $LABRAT buy, clicked by the rat on pons, signed from the
buyback wallet. SWITCHED OFF: nothing here runs unless EVERY gate below holds, and the owner flips it on.

    python live/buyrig_live.py --status              # the LIVE journal: stop, failures, windows, unresolved (read-only)
    python live/buyrig_live.py --clear-stop <id>     # an operator checked why LIVE stopped (the id is in --status)
    python live/buyrig_live.py --resolve             # unfinished windows: receipt, or the IDENTICAL signed bytes again
    python live/buyrig_live.py --abandon <window>    # a signed buy whose nonce another transaction of the wallet used

THE GATES (all of them, else the session runs exactly as before: simulated, DRY)
  * buyrig.py got --live and --window <hour id> from the runner (the engine's booked hour, e.g. 2026-09-25T20:00:00Z)
  * the process environment says BUYRIG_LIVE=1 and BUYRIG_CONFIRM=LABRAT
  * BUYBACK_RH_KEY is set, parses, and its address is the pinned buyback wallet (buyback.BUYBACK_WALLET)
  * eth_chainId is 4663
  The key is read once, kept only inside the signer object, and removed from the process environment before Chromium
  (or any child process) starts. The page gets the wallet's ADDRESS only (AddressOnly). The key is never in a log, the
  stream, the relay, the status, a journal, an error or the page.

THE JOURNAL (JOURNAL_PATH = /data/buyrig/live_journal.jsonl on the rig's volume; one fsync'd JSON line per record)
  reserved -> signed (the raw signed bytes and their hash) -> sent -> receipt | expired, plus failed, stop,
  stop_cleared, anchor, rebroadcast, unconfirmed. Next to it, windows/<hour>.lock is created exclusively (O_EXCL) when
  a window is reserved, so two processes can never both reserve one window.
  * ONE transaction per window, ever: a window already reserved or signed (journal line or lock file) never signs again.
  * 'reserved' is written BEFORE signing, 'signed' (with the raw bytes) BEFORE eth_sendRawTransaction.
  * On start (and every runner poll) unfinished windows are resolved: by their receipt, else by re-broadcasting the
    IDENTICAL raw bytes. A new transaction is never made for them. A signed buy that never reached the chain, whose
    nonce is still free on every RPC and whose router deadline has passed, can no longer buy anything: it is marked
    'expired' (a failed buy) and its nonce is used by the next window's transaction. A signed buy whose nonce another
    transaction used, with no receipt on any RPC, stops LIVE for an operator (--abandon after checking).
  * The wallet's nonce must be what the journal accounts for (FIRST_NONCE, or the last operator 'anchor', plus one per
    mined transaction of ours): a lost journal or a transaction sent from the wallet outside the rig refuses LIVE until
    an operator sets BUYRIG_ANCHOR_NONCE to the wallet's current nonce.
  * Caps in the rig too (buyback.HARD): at most 0.1 ETH per buy, 2.4 ETH signed per rolling day, 5 ETH ever.

LIVE STOPS (journalled 'stop' with an id; cleared only by an operator: BUYRIG_CLEAR_STOP=<id> or --clear-stop <id>)
  * any check failure on the transaction pons built (the 13 DRY checks, and in LIVE: from = the buyback wallet, value =
    the booked amount to the wei, min out within MIN_OUT_BPS of a fresh quote, the re-simulation from the real wallet
    with no balance override)
  * MAX_FAILURES (2) failed buys in a row: a LIVE session for a booked window that did not end in a mined buy with
    LABRAT received (the rat never got to Confirm, pons never asked, the balance was short, it reverted, it never
    landed). A mined buy resets the count.

BURNS (live/burn.py books them; this rig executes them, because it holds the only key). SWITCHED OFF like the buys.

    python live/buyrig_live.py --burn --window <hour id> --amount-wei <wei> [--burn-method auto|burn|transfer]
                               [--result-json PATH]      # the runner's child: ONE burn for one booked window
    python live/buyrig_live.py --abandon <window> --kind burn

  * Its gates: BURN_LIVE=1, BURN_CONFIRM=LABRAT, BUYBACK_RH_KEY of the pinned wallet, eth_chainId 4663, a mounted /data.
    With any of them missing, --burn is a NO-OP (it prints an "off" result; nothing is read from the chain, nothing is
    simulated, nothing is signed). The key leaves the process environment whatever happens.
  * The SAME journal, the SAME nonce account, the SAME failure count and stop: a burn is a window of kind "burn"
    (journal records carry "kind": "burn"; its lock file is windows/burn_<hour>.lock), so the hour's buy and the
    hour's burn are two slots with one transaction each, and a burn is never signed while any signed transaction of
    the wallet is unresolved or pending (so never concurrently with a buy).
  * The transaction: to the LABRAT token, value 0, burn(uint256) (the token dispatches selector 0x42966c68, checked in
    its bytecode on 2026-09-26; a real burn reduces totalSupply) or, for a token without it, transfer(dead, amount) to
    0x000000000000000000000000000000000000dEaD. The rig reads the bytecode itself (--burn-method auto); forcing
    "burn" on a token without it is refused.
  * Its checks, before anything is reserved: the amount is the booked amount to the wei, at most BURN_PCT_MAX_BPS (5 %)
    of the wallet's LABRAT balance read at signing and never over MAX_BURN_WEI; to = the token; value = 0; the
    calldata decodes to exactly that burn; from = the pinned wallet; eth_call and eth_estimateGas of the exact
    transaction from the real wallet succeed. A failed check stops LIVE (for buys too: one stop).
  * A burn has no on-chain deadline, so a signed burn unknown to every pinned RPC, whose nonce is still free
    everywhere BURN_TTL_S after signing, is marked 'expired' (a failed burn) and its nonce goes to the next
    transaction; if its bytes were to land after all, the nonce account catches it and LIVE stops for an operator.
  * Its receipt counts when it is mined (status 1), from the wallet, to the token, with a Transfer of exactly the
    signed amount from the wallet to the zero address (burn) or the dead address (transfer). Mined without one: stop.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

LIVE_DIR = Path(__file__).resolve().parent
ROOT = LIVE_DIR.parent
for _p in (str(ROOT), str(LIVE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import buyback  # noqa: E402
import launcher  # noqa: E402
from eth_abi import decode, encode  # noqa: E402
from eth_utils import keccak, to_checksum_address  # noqa: E402

WALLET = buyback.BUYBACK_WALLET            # the hand-funded buyback wallet: the only address LIVE ever signs for
CHAIN_ID = buyback.CHAIN_ID
EXPLORER = launcher.EXPLORER               # https://robinhoodchain.blockscout.com
SYMBOL = buyback.SYMBOL
ROUTER = buyback.ROUTER
TOKEN = buyback.TOKEN

ENV_LIVE = 'BUYRIG_LIVE'                   # must be "1"
ENV_CONFIRM = 'BUYRIG_CONFIRM'             # must be "LABRAT"
ENV_KEY = 'BUYBACK_RH_KEY'                 # the buyback wallet's private key (Railway variable; never .env, never logged)
ENV_CLEAR_STOP = 'BUYRIG_CLEAR_STOP'       # an operator's "I checked": the id of the stop to clear
ENV_ANCHOR = 'BUYRIG_ANCHOR_NONCE'         # an operator's "the wallet's nonce is now N" (after an outside transaction)

VOLUME = Path('/data')                     # LIVE needs the journal on a mounted volume (None: not checked; tests)
JOURNAL_PATH = VOLUME / 'buyrig' / 'live_journal.jsonl'
FIRST_NONCE = 0            # the buyback wallet had sent nothing: eth_getTransactionCount 0 (latest and pending, 2026-09-25)
HOUR_S = buyback.HOUR_S
SIGN_UNTIL_S = 2 * HOUR_S  # a window (its start s) may be signed from its end (s + 1 h) until s + 2 h: when the next closes
SESSION_MARGIN_S = 300     # the runner starts a LIVE session only with at least this long left to sign
GAS_LIMIT_NUM, GAS_LIMIT_DEN = 5, 4        # gas limit = eth_estimateGas x 1.25 (rounded up)
BALANCE_NUM, BALANCE_DEN = 6, 5            # the wallet must hold the buy + gas limit x maxFee x 1.2
MAX_GAS_PRICE_WEI = 10 ** 9                # no LIVE buy while eth_gasPrice is over 1 gwei (it is about 0.036)
MAX_FEE_CAP_WEI = 2 * 10 ** 9              # maxFeePerGas = min(2 x eth_gasPrice, 2 gwei)
FEE_MULT = 2
MAX_GAS_LIMIT = 600_000                    # a pool buy estimates about 161,000
RECEIPT_WAIT_S = 120.0
REJECTED_WAIT_S = 15.0                     # every RPC rejected the raw tx: look for it this long, then resolve() decides
MAX_FAILURES = 2
# LIVE min out: pons sets min out = floor(quote x 99 %) at Confirm. A fresh quote read a moment later can be a little
# higher, and pons's own rounding already puts min out just under 99 % of an identical quote, so "at least 99 % of a
# fresh quote" would refuse nearly every buy. LIVE allows pons's 1 % slippage plus 1 % of price movement: min out must be
# 98 %..100 % of a fresh quote (DRY allows 97 %).
MIN_OUT_BPS = 9800
EXPIRE_MARGIN_S = 120      # a never-mined tx whose router deadline passed this long ago (chain time) can no longer buy
MAX_BUY_WEI = buyback.HARD['max_buy_wei']          # 0.1 ETH
MAX_DAY_WEI = buyback.HARD['max_day_wei']          # 2.4 ETH signed per rolling day
MAX_TOTAL_WEI = buyback.HARD['max_total_wei']      # 5 ETH signed ever (per journal)
MIN_BUY_WEI = 10 ** 13
WINDOW_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:00:00Z$')
HASH_RE = re.compile(r'^0x[0-9a-f]{64}$')
POSSIBLY_SENT = launcher.POSSIBLY_SENT
# the extra checks LIVE adds to the 13 DRY ones (key -> what the public stream calls it; no addresses)
LIVE_CHECKS = {
    'buyback_wallet': 'from the buyback wallet',
    'booked_amount': 'ETH sent equals the booked amount, to the wei',
    'min_out_live': 'minimum out within 2% of a fresh quote',
}

# ---- burns (the burn engine live/burn.py books them; this rig executes them with the same key, journal and nonces)
ENV_BURN_LIVE = 'BURN_LIVE'                # must be "1"
ENV_BURN_CONFIRM = 'BURN_CONFIRM'          # must be "LABRAT"
KIND_BUY, KIND_BURN = 'buy', 'burn'        # a journal window's kind: the hour's buy and the hour's burn are two slots
KINDS = (KIND_BUY, KIND_BURN)
DEAD = to_checksum_address('0x000000000000000000000000000000000000dEaD')
ZERO = buyback.ZERO
SEL_BURN = '0x42966c68'                    # burn(uint256): ERC20Burnable, reduces totalSupply (LABRAT dispatches it:
                                           # PUSH4 0x42966c68 in its bytecode, no proxy; checked 2026-09-26)
SEL_TRANSFER = '0xa9059cbb'                # transfer(address,uint256): to the dead address, for a token without burn()
SEL_BALANCE_OF = '0x70a08231'              # balanceOf(address)
BURN_METHODS = ('auto', 'burn', 'transfer')
BURN_PCT_MAX_BPS = 500                     # a burn is at most 5 % of the wallet's LABRAT balance, read at signing
MAX_BURN_WEI = 5 * 10 ** 25                # ... and never over 50,000,000 LABRAT (5 % of the 1,000,000,000 supply),
                                           # whatever a balance read says
MIN_BURN_WEI = 10 ** 15                    # 0.001 LABRAT: anything smaller is not worth a transaction
MAX_BURN_GAS_LIMIT = 200_000               # burn() estimates about 34,000 gas, transfer() about 52,000
BURN_TTL_S = 1800                          # no on-chain deadline: a signed burn nobody knows, with its nonce still free,
                                           # is expired this long after signing (see the docstring)
BURN_CHECKS = {                            # the checks on the burn transaction the rig builds (key -> public wording)
    'token': 'to the LABRAT token',
    'no_value': 'carries no ETH',
    'method': 'burn(uint256), or transfer to the dead address for a token without burn()',
    'amount': 'the booked amount, to the wei',
    'recipient': 'burned, not moved (no recipient but the dead address)',
    'wallet': 'from the buyback wallet',
    'chain': 'chain 4663, EIP-1559, no priority fee',
}


class LiveRefused(RuntimeError):
    """LIVE will not sign (or not now). kind says how it counts:
       'check'   a check failed on what pons built (or a rule of the rig broke): LIVE stops
       'failure' a failed buy (the balance, the gas price, a pending transaction...): counted, MAX_FAILURES in a row stop
       'blocked' a precondition (stopped, unresolved, the window already used, the chain unreadable): not counted"""

    def __init__(self, why, kind='blocked'):
        super().__init__(str(why))
        self.kind = kind if kind in ('check', 'failure', 'blocked') else 'blocked'


def log_default(*parts):
    try:
        print('[buyrig-live] ' + ' '.join(str(p) for p in parts), file=sys.stderr, flush=True)
    except Exception:
        pass


def short(err):
    return None if err is None else buyback.short_err(err)[:200]


def eth(wei):
    return buyback.eth_str(int(wei), 8)


def broadcast_state(err):
    """What a send result says about the raw tx leaving this machine (ponsbot.broadcast_state's rule): True (an RPC
    took it, or already knows it), 'unknown' (every RPC timed out: it may have landed) or False (every RPC rejected it)."""
    if err is None:
        return True
    s = str(err).lower()
    if any(p in s for p in POSSIBLY_SENT):
        return True
    if s.startswith('send raised'):
        return 'unknown'
    return False


# ---------------------------------------------------------------------------------------------------- windows
def window_start_t(window):
    """'2026-09-25T20:00:00Z' (the engine's booked hour) -> its unix start. ValueError for anything else."""
    if not isinstance(window, str) or not WINDOW_RE.match(window):
        raise ValueError('a window is the UTC hour of the booked buy, like 2026-09-25T20:00:00Z')
    return int(datetime.strptime(window, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp())


def time_left(window, now):
    """Seconds left to sign this window's buy (<= 0: too late); None while the window has not ended."""
    s = window_start_t(window)
    if now < s + HOUR_S:
        return None
    return s + SIGN_UNTIL_S - now


def signable(window, now, margin=0.0):
    left = time_left(window, now)
    return left is not None and left > margin


# ---------------------------------------------------------------------------------------------------- the gates
class AddressOnly:
    """The page wallet in LIVE: the buyback wallet's address and nothing else (no key, nothing to sign with)."""

    def __init__(self, address):
        self.address = to_checksum_address(address)

    def __repr__(self):
        return 'AddressOnly(buyback wallet)'


def env_gate(environ=None):
    """The environment gates of a BUY. -> (account, '') or (None, why). The key's text never appears in why."""
    env = os.environ if environ is None else environ
    if env.get(ENV_LIVE) != '1':
        return None, f'{ENV_LIVE} is not 1'
    if env.get(ENV_CONFIRM) != SYMBOL:
        return None, f'{ENV_CONFIRM} is not {SYMBOL}'
    return key_gate(env)


def burn_env_gate(environ=None):
    """The environment gates of a BURN: its own two switches, then the same key. -> (account, '') or (None, why)."""
    env = os.environ if environ is None else environ
    if env.get(ENV_BURN_LIVE) != '1':
        return None, f'{ENV_BURN_LIVE} is not 1'
    if env.get(ENV_BURN_CONFIRM) != SYMBOL:
        return None, f'{ENV_BURN_CONFIRM} is not {SYMBOL}'
    return key_gate(env)


def key_gate(environ=None):
    """The key gate: BUYBACK_RH_KEY is set, parses, and is the pinned wallet's. -> (account, '') or (None, why)."""
    env = os.environ if environ is None else environ
    key = (env.get(ENV_KEY) or '').strip()
    if not key:
        return None, f'{ENV_KEY} is not set'
    acct = None
    try:
        from eth_account import Account
        acct = Account.from_key(key)
    except Exception:
        acct = None
    key = None
    if acct is None:
        return None, f'{ENV_KEY} does not parse as a private key'
    if acct.address != WALLET:
        return None, f'{ENV_KEY} is not the key of the pinned buyback wallet'
    return acct, ''


def chain_gate(rpc):
    """None when eth_chainId is 4663, else why not."""
    try:
        cid = int(rpc.ok('eth_chainId', []), 16)
    except Exception as e:
        return f'eth_chainId could not be read ({type(e).__name__})'
    return None if cid == CHAIN_ID else f'chain id {cid}, not {CHAIN_ID}'


def gate(live_flag, window, environ=None, rpc=None):
    """Every LIVE gate, in order. -> (account, '') or (None, why): None means the session runs exactly as before (DRY)."""
    if not live_flag:
        return None, '--live was not given'
    if not window:
        return None, '--window was not given'
    try:
        window_start_t(window)
    except ValueError as e:
        return None, str(e)
    acct, why = env_gate(environ)
    if acct is None:
        return None, why
    why = chain_gate(rpc or buyback.ReadRpc())
    if why:
        return None, why
    return acct, ''


def burn_gate(window, environ=None, rpc=None):
    """Every gate of a burn, in order: a booked hour, BURN_LIVE=1, BURN_CONFIRM=LABRAT, the pinned wallet's key, chain
    4663. -> (account, '') or (None, why): None means --burn is a no-op."""
    if not window:
        return None, '--window was not given'
    try:
        window_start_t(window)
    except ValueError as e:
        return None, str(e)
    acct, why = burn_env_gate(environ)
    if acct is None:
        return None, why
    why = chain_gate(rpc or buyback.ReadRpc())
    if why:
        return None, why
    return acct, ''


def drop_key(environ=None):
    """The key leaves this process's environment (Chromium, Playwright's driver and every child process start after
    this, so none of them inherits it). The signer keeps its own copy in memory."""
    (os.environ if environ is None else environ).pop(ENV_KEY, None)


# ---------------------------------------------------------------------------------------------------- the RPC
_GATE = object()


class SendRpc(buyback.ReadRpc):
    """buyback.ReadRpc plus eth_sendRawTransaction, and every pinned RPC one by one (each) for the evidence a
    resolution needs. Built only by open_rpc(), for an account that passed the gates."""

    def __init__(self, transport=None, nodes=None, _gate=None):
        if _gate is not _GATE:
            raise LiveRefused('SendRpc is built only by open_rpc(), after the LIVE gates')
        super().__init__(transport)
        self.nodes = list(nodes) if nodes is not None else [buyback._node_transport(u) for u in launcher.RPCS]

    def each(self, method, params):
        """-> [(result, error)] from every pinned RPC separately; an unreachable one gives (None, 'unreachable')."""
        if method not in buyback.READ_METHODS:
            raise buyback.SendRefused(f'{method} is not a read method')
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


def open_rpc(acct, transport=None, nodes=None, environ=None):
    """The send-capable RPC, for the gated account only (the pinned wallet), and only through the pinned public RPCs."""
    if getattr(acct, 'address', None) != WALLET:
        raise LiveRefused('not the pinned buyback wallet')
    env = os.environ if environ is None else environ
    if env.get('RATBRAIN_RPC'):
        raise LiveRefused('RATBRAIN_RPC is set: LIVE reads, simulates and sends only through the pinned public RPCs')
    return SendRpc(transport, nodes, _gate=_GATE)


# ---------------------------------------------------------------------------------------------------- the journal
def slot(window, kind=KIND_BUY):
    """The journal's key for one window of one kind. A buy keeps the bare hour id (every record before burns existed
    is a buy); a burn of the same hour is its own slot, with its own one transaction."""
    return window if kind == KIND_BUY else f'{kind}:{window}'


def rec_kind(r):
    k = r.get('kind') if isinstance(r, dict) else None
    return k if isinstance(k, str) and k else KIND_BUY


def kind_field(kind):
    """What a record carries about its kind: nothing for a buy (records stay as they always were), "kind" else."""
    return {} if kind == KIND_BUY else {'kind': kind}


class State:
    """The journal replayed: each slot's (window x kind) records, the consecutive failed transactions, the stop, the
    nonce account (one account for buys and burns: the wallet has one nonce)."""

    def __init__(self):
        self.windows = {}
        self.failures = 0
        self.stop = None
        self.anchor = None
        self.mined_since_anchor = 0
        self.bad_lines = 0
        self.records = 0

    def w(self, window, kind=KIND_BUY):
        return self.windows.setdefault(slot(window, kind), {'kind': kind, 'window': window, 'reserved': None,
                                                            'signed': None, 'sent': [], 'receipt': None,
                                                            'expired': None, 'failed': None})

    def apply(self, r):
        ev, win, kind = r.get('ev'), r.get('window'), rec_kind(r)
        self.records += 1
        if ev == 'reserved':
            self.w(win, kind)['reserved'] = r
        elif ev == 'signed':
            self.w(win, kind)['signed'] = r
        elif ev in ('sent', 'rebroadcast'):
            self.w(win, kind)['sent'].append(r)
        elif ev == 'receipt':
            w = self.w(win, kind)
            if w['receipt'] is None:
                self.mined_since_anchor += 1          # our transaction was mined: it used a nonce
            w['receipt'] = r
            if r.get('ok'):
                self.failures = 0
        elif ev == 'expired':
            self.w(win, kind)['expired'] = r
        elif ev == 'failed':
            self.w(win, kind)['failed'] = r
            self.failures = int(r.get('consecutive', self.failures + 1))
        elif ev == 'stop':
            self.stop = r
        elif ev == 'stop_cleared':
            if self.stop and r.get('id') == self.stop.get('id'):
                self.stop = None
                self.failures = 0
        elif ev == 'anchor':
            self.anchor = int(r['nonce'])
            self.mined_since_anchor = 0

    def used(self, window, kind=KIND_BUY):
        w = self.windows.get(slot(window, kind))
        return bool(w and (w['reserved'] or w['signed']))

    def unresolved(self):
        """Slots (of any kind) with a signed transaction that has neither a receipt nor an 'expired' record. Each
        value carries its 'window' and 'kind'."""
        return {key: w for key, w in self.windows.items() if w['signed'] and not w['receipt'] and not w['expired']}

    def bought(self, window):
        w = self.windows.get(slot(window, KIND_BUY))
        return bool(w and w['receipt'] and w['receipt'].get('ok'))

    def burned(self, window):
        w = self.windows.get(slot(window, KIND_BURN))
        return bool(w and w['receipt'] and w['receipt'].get('ok'))

    def expected_nonce(self):
        return (FIRST_NONCE if self.anchor is None else self.anchor) + self.mined_since_anchor

    def signed_spend(self, now, seconds=None):
        """ETH (wei) of every signed transaction (a signed one may still land), in the last `seconds` or ever. A burn
        carries no ETH (value 0), so only buys add up."""
        return sum(int(w['signed'].get('value', 0)) for w in self.windows.values()
                   if w['signed'] and (seconds is None or now - float(w['signed'].get('t', 0)) < seconds))

    def executed(self):
        """[{window, tx, amount_wei, labrat_out_wei, block}] for every window whose BUY was mined with LABRAT received."""
        out = []
        for _key, w in sorted(self.windows.items()):
            rc = w['receipt']
            if w['kind'] == KIND_BUY and w['signed'] and rc and rc.get('ok'):
                out.append({'window': w['window'], 'tx': w['signed']['tx'], 'amount_wei': int(w['signed']['value']),
                            'labrat_out_wei': int(rc.get('labrat_out_wei') or 0), 'block': rc.get('block')})
        return out

    def executed_burns(self):
        """[{window, tx, amount_wei, burned_wei, method, block}] for every window whose BURN was mined with the signed
        amount leaving the wallet for the zero or the dead address."""
        out = []
        for _key, w in sorted(self.windows.items()):
            rc = w['receipt']
            if w['kind'] == KIND_BURN and w['signed'] and rc and rc.get('ok'):
                out.append({'window': w['window'], 'tx': w['signed']['tx'],
                            'amount_wei': int(w['signed'].get('amount_wei') or 0),
                            'burned_wei': int(rc.get('burned_wei') or 0), 'method': w['signed'].get('method'),
                            'block': rc.get('block'), 'signed_at': w['signed'].get('at')})
        return out

    def burned_total_wei(self):
        return sum(x['burned_wei'] for x in self.executed_burns())


class Journal:
    """Append-only JSONL on the volume, one fsync'd line per record, plus one lock file per reserved window."""

    def __init__(self, path=None, clock=time.time):
        self.path = Path(path) if path is not None else JOURNAL_PATH
        self.clock = clock
        self.lock = threading.Lock()

    @property
    def lock_dir(self):
        return self.path.parent / 'windows'

    def lock_path(self, window, kind=KIND_BUY):
        base = window.replace('-', '').replace(':', '')
        return self.lock_dir / ((base if kind == KIND_BUY else f'{kind}_{base}') + '.lock')

    def records(self):
        """-> (records, number of unreadable lines)."""
        out, bad = [], 0
        if not self.path.exists():
            return out, 0
        with open(self.path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if isinstance(r, dict):
                    out.append(r)
                else:
                    bad += 1
        return out, bad

    def state(self):
        recs, bad = self.records()
        st = State()
        for r in recs:
            st.apply(r)
        st.bad_lines = bad
        return st

    def append(self, rec):
        t = self.clock()
        rec = {'t': round(t, 3), 'at': buyback.iso(t), **{k: v for k, v in rec.items() if k not in ('t', 'at')}}
        line = json.dumps(rec, separators=(',', ':'), default=str)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, 'a', encoding='utf-8', newline='\n') as f:
                f.write(line + '\n')
                f.flush()
                os.fsync(f.fileno())
        return rec

    def reserve(self, window, info, kind=KIND_BUY):
        """The slot's lock file by exclusive create (two processes can never both get past it), then 'reserved'. From
        here on this window (of this kind) can never be signed again, whatever happens next."""
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        p = self.lock_path(window, kind)
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise LiveRefused(f'the {kind} window {window} is already reserved: one transaction per window, never '
                              'two') from None
        with os.fdopen(fd, 'w') as f:
            f.write(json.dumps({'window': window, 'kind': kind, 'at': buyback.iso(self.clock()), 'pid': os.getpid()}))
            f.flush()
            os.fsync(f.fileno())
        return self.append({'ev': 'reserved', 'window': window, **kind_field(kind), **info})


def stop(journal, why, window=None, kind='check', log=log_default):
    """Journal a LIVE stop (it survives restarts until an operator clears it by its id)."""
    t = journal.clock()
    sid = hashlib.sha256(f'{t}|{why}|{window}'.encode()).hexdigest()[:8]
    rec = journal.append({'ev': 'stop', 'id': sid, 'kind': kind, 'why': str(why)[:400], 'window': window})
    log(f'LIVE STOPPED ({kind}): {str(why)[:300]} · stop id {sid} (clear it with {ENV_CLEAR_STOP}={sid} after checking)')
    return rec


def record_failure(journal, window, why, log=log_default, kind=KIND_BUY):
    """One failed buy (or burn) for this window (at most one per slot; never for a slot whose transaction was mined
    ok, nor while its signed transaction is unresolved: its receipt decides). MAX_FAILURES in a row, buys and burns
    together (the wallet has one nonce, one balance and one operator), stop LIVE."""
    st = journal.state()
    w = st.windows.get(slot(window, kind))
    if w and (w['failed'] or (w['receipt'] and w['receipt'].get('ok'))):
        return None
    if w and w['signed'] and not w['receipt'] and not w['expired']:
        return None
    n = st.failures + 1
    rec = journal.append({'ev': 'failed', 'window': window, **kind_field(kind), 'why': str(why)[:300],
                          'consecutive': n})
    log(f'LIVE: failed {kind} for {window} ({n} in a row): {str(why)[:200]}')
    if n >= MAX_FAILURES and not st.stop:
        stop(journal, f'{n} failed transactions in a row (last, a {kind}: {str(why)[:200]})', window, 'failures',
             log)
    return rec


def clear_stop(journal, sid, why='operator', log=log_default):
    st = journal.state()
    if not st.stop or not sid or st.stop.get('id') != sid:
        return None
    rec = journal.append({'ev': 'stop_cleared', 'id': sid, 'cleared': st.stop.get('why'), 'why': why})
    log(f'LIVE stop {sid} cleared by the operator')
    return rec


def note_refusal(journal, window, e, log=log_default, kind=KIND_BUY):
    """A LiveRefused, booked by its kind: 'check' stops LIVE, 'failure' counts, 'blocked' is only logged."""
    if e.kind == 'check':
        what = 'window' if kind == KIND_BUY else f'{kind} window'
        return stop(journal, f'a check failed for the {what} {window}: {e}', window, 'check', log)
    if e.kind == 'failure':
        return record_failure(journal, window, str(e), log, kind)
    return None


# ---------------------------------------------------------------------------------------------------- receipts
def read_receipt(rc, wallet=None):
    """A receipt -> {status, ok, block, labrat_out_wei, gas_used, gas_price_wei, gas_cost_wei}. ok: mined (status 1),
    sent from the wallet to the pons router, and LABRAT arrived at the wallet (the Transfer logs of the token to it)."""
    wallet = wallet or WALLET
    status = int(rc.get('status') or '0x0', 16)
    got = 0
    for lg in rc.get('logs') or []:
        topics = lg.get('topics') or []
        if (str(lg.get('address', '')).lower() == TOKEN.lower() and len(topics) > 2
                and str(topics[0]).lower() == buyback.TRANSFER_TOPIC and str(topics[2])[-40:].lower() == wallet[2:].lower()):
            try:
                got += int(lg.get('data') or '0x0', 16)
            except ValueError:
                pass
    frm, to = rc.get('from'), rc.get('to')
    ok = (status == 1 and got > 0 and (frm is None or str(frm).lower() == wallet.lower())
          and (to is None or str(to).lower() == ROUTER.lower()))
    gas_used = int(rc.get('gasUsed') or '0x0', 16)
    price = int(rc.get('effectiveGasPrice') or '0x0', 16)
    return {'status': status, 'ok': ok, 'block': int(rc.get('blockNumber') or '0x0', 16), 'labrat_out_wei': got,
            'gas_used': gas_used, 'gas_price_wei': price, 'gas_cost_wei': gas_used * price}


def read_burn_receipt(rc, amount_wei=0, wallet=None):
    """A burn's receipt -> {status, ok, block, burned_wei, burn_to, gas_used, gas_price_wei, gas_cost_wei}. ok: mined
    (status 1), sent from the wallet to the token, and the token's Transfer logs show exactly the signed amount leaving
    the wallet for the zero address (burn(): totalSupply shrinks) or the dead address (transfer to 0x...dEaD)."""
    wallet = wallet or WALLET
    status = int(rc.get('status') or '0x0', 16)
    burned, burn_to = 0, None
    for lg in rc.get('logs') or []:
        topics = lg.get('topics') or []
        if (str(lg.get('address', '')).lower() == TOKEN.lower() and len(topics) > 2
                and str(topics[0]).lower() == buyback.TRANSFER_TOPIC
                and str(topics[1])[-40:].lower() == wallet[2:].lower()):
            dest = str(topics[2])[-40:].lower()
            if dest in (ZERO[2:].lower(), DEAD[2:].lower()):
                try:
                    burned += int(lg.get('data') or '0x0', 16)
                except ValueError:
                    continue
                burn_to = 'zero' if dest == ZERO[2:].lower() else 'dead'
    frm, to = rc.get('from'), rc.get('to')
    want = int(amount_wei or 0)
    ok = (status == 1 and burned > 0 and (want == 0 or burned == want)
          and (frm is None or str(frm).lower() == wallet.lower()) and (to is None or str(to).lower() == TOKEN.lower()))
    gas_used = int(rc.get('gasUsed') or '0x0', 16)
    price = int(rc.get('effectiveGasPrice') or '0x0', 16)
    return {'status': status, 'ok': ok, 'block': int(rc.get('blockNumber') or '0x0', 16), 'burned_wei': burned,
            'burn_to': burn_to, 'gas_used': gas_used, 'gas_price_wei': price, 'gas_cost_wei': gas_used * price}


def record_receipt(journal, window, signed, rc, log=log_default, kind=KIND_BUY):
    """Book a receipt for a signed slot (ONE 'receipt' record resolves it). A revert is a failed buy / burn; mined
    without LABRAT reaching the wallet (a buy) or leaving it for the zero / dead address (a burn) is a broken rule:
    LIVE stops."""
    if kind == KIND_BURN:
        info = read_burn_receipt(rc, int(signed.get('amount_wei') or 0))
    else:
        info = read_receipt(rc)
    journal.append({'ev': 'receipt', 'window': window, **kind_field(kind), 'tx': signed['tx'], **info})
    if info['ok'] and kind == KIND_BURN:
        log(f"LIVE: burn mined in block {info['block']}: {buyback.token_str(info['burned_wei'])} LABRAT burned from "
            f'the buyback wallet ({info["burn_to"]} address) · {launcher.EXPLORER}/tx/{signed["tx"]}')
    elif info['ok']:
        log(f"LIVE: mined in block {info['block']}: {buyback.token_str(info['labrat_out_wei'])} LABRAT to the buyback "
            f'wallet · {launcher.EXPLORER}/tx/{signed["tx"]}')
    elif info['status'] == 1 and kind == KIND_BURN:
        stop(journal, f"the burn for {window} was mined without the signed amount leaving the buyback wallet for the "
                      f"zero or dead address ({signed['tx']})", window, 'check', log)
    elif info['status'] == 1:
        stop(journal, f"the buy for {window} was mined without LABRAT reaching the buyback wallet ({signed['tx']})",
             window, 'check', log)
    else:
        record_failure(journal, window, f"the {kind} reverted on chain ({signed['tx']})", log, kind)
    return info


def _receipt_any(rpc, txh):
    rc, _ = rpc.raw('eth_getTransactionReceipt', [txh])
    if rc:
        return rc
    for r, err in rpc.each('eth_getTransactionReceipt', [txh]):
        if r and err is None:
            return r
    return None


def _known_any(rpc, txh):
    t, _ = rpc.raw('eth_getTransactionByHash', [txh])
    if t:
        return True
    return any(r and err is None for r, err in rpc.each('eth_getTransactionByHash', [txh]))


def _nonces(rpc):
    """Every pinned RPC's latest nonce of the wallet, or None when any of them could not answer."""
    out = []
    for n, err in rpc.each('eth_getTransactionCount', [WALLET, 'latest']):
        try:
            if err is not None:
                return None
            out.append(int(n, 16))
        except (TypeError, ValueError):
            return None
    return out or None


def resolve(journal, rpc, log=log_default):
    """Every signed window without a receipt: its receipt, else the IDENTICAL raw bytes again. Never signs anything.
    -> {'resolved': [...], 'pending': [...], 'stopped': why or None}"""
    st = journal.state()
    out = {'resolved': [], 'pending': [], 'stopped': None}
    for key, w in sorted(st.unresolved().items()):
        rec, win, kind = w['signed'], w['window'], w['kind']
        txh = rec['tx']
        try:
            rc = _receipt_any(rpc, txh)
            if rc:
                record_receipt(journal, win, rec, rc, log, kind)
                out['resolved'].append(key)
                continue
            if _known_any(rpc, txh):
                out['pending'].append(key)                 # in a mempool: wait for it
                continue
            nonces = _nonces(rpc)
            if nonces is None:
                out['pending'].append(key)
                continue
            if all(n <= int(rec['nonce']) for n in nonces):
                # its nonce is still free on every RPC: it never landed. After its router deadline (a buy) or its time
                # to live (a burn, which has no on-chain deadline: see the docstring) it is given up
                blk = rpc.ok('eth_getBlockByNumber', ['latest', False])
                if int(blk['timestamp'], 16) > int(rec.get('deadline') or 0) + EXPIRE_MARGIN_S:
                    why = ('never mined, and its router deadline passed: it can no longer buy anything '
                           if kind == KIND_BUY else
                           'never mined, unknown to every RPC, and its time to live passed: given up ')
                    journal.append({'ev': 'expired', 'window': win, **kind_field(kind), 'tx': txh,
                                    'why': why + '(its nonce goes to the next transaction)'})
                    record_failure(journal, win, f'the signed {kind} never reached the chain', log, kind)
                    out['resolved'].append(key)
                    continue
                _r, err = rpc.send_raw(rec['raw'])
                journal.append({'ev': 'rebroadcast', 'window': win, **kind_field(kind), 'tx': txh, 'error': short(err),
                                'broadcast': broadcast_state(err)})
                log(f'LIVE: re-broadcast the identical signed {kind} for {win} ({txh})'
                    + (f': {short(err)}' if err else ''))
                out['pending'].append(key)
                continue
            if all(n > int(rec['nonce']) for n in nonces):
                why = (f"the buyback wallet used nonce {rec['nonce']} for another transaction, and no RPC has a receipt "
                       f'for the signed {kind} {txh} of {win}: it can never land. An operator must check it '
                       f'(python live/buyrig_live.py --abandon {win}'
                       + ('' if kind == KIND_BUY else f' --kind {kind}') + ')')
                if not st.stop:
                    stop(journal, why, win, 'nonce', log)
                    st = journal.state()
                out['stopped'] = why
            out['pending'].append(key)
        except (buyback.RpcError, RuntimeError, KeyError, TypeError, ValueError) as e:
            log(f'LIVE: resolving {key} failed for now ({type(e).__name__}); it is retried')
            out['pending'].append(key)
    return out


def apply_operator_env(journal, rpc, environ=None, log=log_default):
    """The operator's two switches, from the environment: BUYRIG_CLEAR_STOP=<the stop's id> and BUYRIG_ANCHOR_NONCE=<the
    wallet's nonce on chain now> (only taken when it IS the nonce on chain, nothing is pending and nothing is
    unresolved). A stale value does nothing: a later stop has another id, and the nonce moves on."""
    env = os.environ if environ is None else environ
    sid = (env.get(ENV_CLEAR_STOP) or '').strip()
    if sid:
        clear_stop(journal, sid, f'operator: {ENV_CLEAR_STOP}', log)
    a = (env.get(ENV_ANCHOR) or '').strip()
    if not a:
        return
    try:
        n = int(a)
    except ValueError:
        log(f'{ENV_ANCHOR} is not a number: ignored')
        return
    st = journal.state()
    if st.expected_nonce() == n or st.unresolved():
        return
    try:
        latest = int(rpc.ok('eth_getTransactionCount', [WALLET, 'latest']), 16)
        pending = int(rpc.ok('eth_getTransactionCount', [WALLET, 'pending']), 16)
    except (buyback.RpcError, RuntimeError, ValueError):
        log(f'{ENV_ANCHOR}: the nonce could not be read; not applied')
        return
    if latest == pending == n:
        journal.append({'ev': 'anchor', 'nonce': n, 'why': f'operator: {ENV_ANCHOR}'})
        log(f'LIVE: the journal is anchored at the wallet\'s nonce {n} (operator)')
    else:
        log(f'{ENV_ANCHOR}={n} is not the wallet\'s nonce on chain ({latest}, pending {pending}): ignored')


# ---------------------------------------------------------------------------------------------------- the checks
def live_checks(f, facts, amount_wei, wallet=None):
    """The checks LIVE adds to buyrig.buy_checks (f: buyrig.decode_buy's fields; facts: buyrig.chain_facts WITHOUT a
    balance override, so its 'simulation' is the exact transaction from the real wallet). -> {key: {ok, expected}}"""
    wallet = wallet or WALLET
    shape = bool(f.get('shape_ok'))
    q = int(facts.get('quote') or 0)
    mo = int(f.get('min_out') or 0) if shape else 0
    want = {
        'buyback_wallet': (str(f.get('from') or '').lower() == wallet.lower(), f'from {wallet} (the buyback wallet)'),
        'booked_amount': (shape and f.get('value_wei') == int(amount_wei) == f.get('amount_in') == f.get('settle'),
                          f'value == amountIn == SETTLE_ALL == {int(amount_wei)} wei (the booked amount)'),
        'min_out_live': (shape and q > 0 and mo * 10_000 >= q * MIN_OUT_BPS and mo <= q,
                         f'{MIN_OUT_BPS / 100:g}%..100% of a fresh quote ({q})'),
    }
    return {k: {'ok': bool(ok), 'expected': str(exp)} for k, (ok, exp) in want.items()}


def build_tx(nonce, data, value, gas, max_fee):
    """The EIP-1559 transaction the rig signs: chain 4663, to the pons router, exactly the checked calldata."""
    return {'chainId': CHAIN_ID, 'nonce': int(nonce), 'to': ROUTER, 'value': int(value), 'data': data, 'gas': int(gas),
            'maxFeePerGas': int(max_fee), 'maxPriorityFeePerGas': 0, 'type': 2}


# ---------------------------------------------------------------------------------------------------- burns: the tx
def cd_balance_of(addr):
    return SEL_BALANCE_OF + buyback.pad_addr(addr)


def cd_burn(amount_wei):
    return SEL_BURN + encode(['uint256'], [int(amount_wei)]).hex()


def cd_transfer(to, amount_wei):
    return SEL_TRANSFER + encode(['address', 'uint256'], [to_checksum_address(to), int(amount_wei)]).hex()


def burn_calldata(method, amount_wei):
    """The calldata of a burn by method: burn(amount), or transfer(dead, amount). ValueError for anything else."""
    if method == 'burn':
        return cd_burn(amount_wei)
    if method == 'transfer':
        return cd_transfer(DEAD, amount_wei)
    raise ValueError(f'{method!r} is not a burn method (burn / transfer)')


def decode_burn(data):
    """Calldata -> (method, recipient, amount): exactly burn(uint256) or transfer(address,uint256), nothing else (no
    extra bytes, no other selector). ValueError otherwise."""
    if not isinstance(data, str) or not data.startswith('0x'):
        raise ValueError('calldata is not hex')
    try:
        raw = bytes.fromhex(data[2:])
    except ValueError:
        raise ValueError('calldata is not hex') from None
    sel = '0x' + raw[:4].hex()
    if sel == SEL_BURN:
        if len(raw) != 4 + 32:
            raise ValueError('burn(uint256) takes exactly one word')
        (amount,) = decode(['uint256'], raw[4:])
        return 'burn', None, int(amount)
    if sel == SEL_TRANSFER:
        if len(raw) != 4 + 64:
            raise ValueError('transfer(address,uint256) takes exactly two words')
        to, amount = decode(['address', 'uint256'], raw[4:])
        return 'transfer', to_checksum_address(to), int(amount)
    raise ValueError(f'selector {sel} is neither burn(uint256) nor transfer(address,uint256)')


def token_has_burn(code_hex):
    """Whether a contract's bytecode dispatches burn(uint256): PUSH4 0x42966c68 in its selector table."""
    return isinstance(code_hex, str) and ('63' + SEL_BURN[2:]) in code_hex.lower()


def token_burn_method(rpc, method='auto'):
    """The method the rig burns with, from the token's bytecode (read now): 'burn' when the token dispatches
    burn(uint256) (preferred: it reduces totalSupply), else 'transfer' (to the dead address). method forces one;
    forcing 'burn' on a token without it is refused (a check)."""
    if method not in BURN_METHODS:
        raise LiveRefused(f'{method!r} is not one of {BURN_METHODS}', 'check')
    try:
        code = rpc.ok('eth_getCode', [TOKEN, 'latest'])
    except (buyback.RpcError, RuntimeError) as e:
        raise LiveRefused(f'the token bytecode could not be read ({type(e).__name__})') from None
    if not isinstance(code, str) or len(code) <= 2:
        raise LiveRefused('there is no code at the token address', 'check')
    has = token_has_burn(code)
    if method == 'burn' and not has:
        raise LiveRefused('the token does not dispatch burn(uint256): --burn-method burn is refused', 'check')
    return 'burn' if method == 'burn' or (method == 'auto' and has) else 'transfer'


def build_burn_tx(nonce, data, gas, max_fee):
    """The EIP-1559 transaction of a burn: chain 4663, to the token, no ETH, exactly the checked calldata."""
    return {'chainId': CHAIN_ID, 'nonce': int(nonce), 'to': TOKEN, 'value': 0, 'data': data, 'gas': int(gas),
            'maxFeePerGas': int(max_fee), 'maxPriorityFeePerGas': 0, 'type': 2}


def check_burn_tx(tx, amount_wei, method, signer_address=None, wallet=None):
    """The checks on the burn transaction the rig built, before it is reserved or signed (BURN_CHECKS).
    -> {key: {ok, expected}}. Every check must pass; one that fails stops LIVE (a rule of the rig broke)."""
    wallet = wallet or WALLET
    amount_wei = int(amount_wei)
    try:
        m, to_addr, amt = decode_burn(tx.get('data'))
    except (ValueError, TypeError, AttributeError):
        m, to_addr, amt = None, None, None
    want = {
        'token': (str(tx.get('to') or '').lower() == TOKEN.lower(), f'to {TOKEN} (the token)'),
        'no_value': (tx.get('value') == 0, 'value 0'),
        'method': (m is not None and m == method, f'{method}(...) exactly'),
        'amount': (amt is not None and amt == amount_wei and MIN_BURN_WEI <= amount_wei <= MAX_BURN_WEI,
                   f'{amount_wei} wei (the booked amount, within the rig\'s bounds)'),
        'recipient': (m == 'burn' and to_addr is None or m == 'transfer' and to_addr == DEAD,
                      'no recipient (burn) or the dead address (transfer)'),
        'wallet': ((signer_address or wallet) == wallet, f'signed by {wallet} (the buyback wallet)'),
        'chain': (tx.get('chainId') == CHAIN_ID and tx.get('type') == 2 and tx.get('maxPriorityFeePerGas') == 0,
                  f'chain {CHAIN_ID}, type 2, priority fee 0'),
    }
    return {k: {'ok': bool(ok), 'expected': str(exp)} for k, (ok, exp) in want.items()}


# ---------------------------------------------------------------------------------------------------- the signer
class _LiveTx:
    """What one LIVE transaction of one slot (a buy or a burn of one booked window) shares: the gated signer (kept only
    here), the gated SendRpc, the journal, the preflight (the journal's rules and the nonce account), the write-ahead
    sign-and-broadcast, the receipt. At most ONE transaction for the slot, ever."""
    kind = KIND_BUY
    value_wei = 0               # the ETH the transaction carries (a buy: the amount; a burn: nothing)

    def __init__(self, signer, rpc, journal, window, amount_wei, log=log_default, clock=time.time, sleep=time.sleep,
                 receipt_wait_s=RECEIPT_WAIT_S):
        if getattr(signer, 'address', None) != WALLET:
            raise LiveRefused('the signer is not the pinned buyback wallet')
        if not isinstance(rpc, SendRpc):
            raise LiveRefused('LIVE needs the gated SendRpc')
        window_start_t(window)
        self._signer = signer
        self.address = signer.address
        self.rpc, self.journal, self.window, self.amount_wei = rpc, journal, window, int(amount_wei)
        self.log, self.clock, self.sleep, self.receipt_wait_s = log, clock, sleep, receipt_wait_s
        self._lock = threading.Lock()
        self.used = False
        self.signed = None          # the 'signed' journal record (tx hash, raw bytes, nonce, gas, fee)
        self.sent = None            # {'tx', 'error', 'broadcast'}
        self.receipt = None         # read_receipt(...) / read_burn_receipt(...) once mined
        self.nonce = None

    def _take_turn(self):
        with self._lock:
            if self.used:
                raise LiveRefused(f'this session already handled its one {self.kind}')
            self.used = True

    def redact(self, s):
        """s with the key's hex (with or without 0x) replaced: the key is never in any output."""
        s = str(s)
        try:
            k = bytes(self._signer.key).hex() if hasattr(self._signer, 'key') else None
        except Exception:
            k = None
        return re.sub(re.escape(k), '<redacted>', s, flags=re.I) if k else s

    # ---- reads
    def _nonce(self, tag):
        return int(self.rpc.ok('eth_getTransactionCount', [WALLET, tag]), 16)

    def preflight(self, stage='start'):
        """-> {'nonce': n} when this slot may be signed now, else LiveRefused (its kind says how it counts). Resolves
        unfinished windows of every kind first (receipts / identical re-broadcasts; never a new transaction): nothing
        is signed while any signed transaction of the wallet is unresolved, so a buy and a burn never overlap."""
        j, now = self.journal, self.clock()
        st = j.state()
        if st.bad_lines:
            raise LiveRefused(f'the LIVE journal has {st.bad_lines} unreadable line(s): a damaged line could hide a signed '
                              'transaction; an operator must repair it by hand')
        if st.stop:
            raise LiveRefused(f"LIVE is stopped: {st.stop.get('why')} (stop id {st.stop.get('id')})")
        if st.unresolved():
            resolve(j, self.rpc, self.log)
            st = j.state()
            if st.stop:
                raise LiveRefused(f"LIVE is stopped: {st.stop.get('why')} (stop id {st.stop.get('id')})")
            if st.unresolved():
                raise LiveRefused('an earlier signed transaction is not resolved yet (no receipt): nothing new is '
                                  'signed until it is')
        if st.used(self.window, self.kind) or j.lock_path(self.window, self.kind).exists():
            what = 'window' if self.kind == KIND_BUY else f'{self.kind} window'
            raise LiveRefused(f'the {what} {self.window} already has its transaction reserved or signed: one per window')
        left = time_left(self.window, now)
        if left is None:
            raise LiveRefused(f'the window {self.window} has not ended: its {self.kind} cannot have been booked', 'check')
        if left <= 0:
            raise LiveRefused(f'too late for the window {self.window}: a window is signed at most one hour after it ends',
                              'blocked' if stage == 'start' else 'failure')
        day, total = st.signed_spend(now, 86400), st.signed_spend(now)
        if day + self.value_wei > MAX_DAY_WEI or total + self.value_wei > MAX_TOTAL_WEI:
            raise LiveRefused(f'the rig\'s own caps: {eth(day)} ETH signed in the last day, {eth(total)} ETH ever, plus '
                              f'{eth(self.value_wei)} is over {eth(MAX_DAY_WEI)} / {eth(MAX_TOTAL_WEI)}', 'check')
        try:
            latest, pending = self._nonce('latest'), self._nonce('pending')
        except (buyback.RpcError, RuntimeError, ValueError) as e:
            raise LiveRefused(f'the wallet nonce could not be read ({type(e).__name__})') from None
        if latest != pending:
            raise LiveRefused(f'the buyback wallet has a pending transaction that is not the rig\'s (nonce {latest}, '
                              f'pending {pending})', 'failure')
        exp = st.expected_nonce()
        if latest != exp:
            raise LiveRefused(f'the buyback wallet is at nonce {latest} but the LIVE journal accounts for {exp}: a '
                              'transaction was sent from it outside the rig, or the journal was lost. An operator must '
                              f'check, then set {ENV_ANCHOR}={latest}')
        self.nonce = latest
        return {'nonce': latest}

    # ---- sign and broadcast (after the slot is reserved: from here on it can never be signed again)
    def _sign_and_broadcast(self, tx, record):
        """Sign the built transaction, write it ahead ('signed', with the raw bytes) and broadcast it. record: what the
        'signed' journal line keeps besides tx / raw / nonce. -> self.sent"""
        try:
            signed = self._signer.sign_transaction(tx)
            raw = '0x' + bytes(signed.raw_transaction).hex()
            txh = '0x' + keccak(bytes.fromhex(raw[2:])).hex()
        except Exception as e:                 # the message never reaches the logs: it could hold anything
            raise LiveRefused(f'signing failed ({type(e).__name__})', 'failure') from None
        # write-ahead: the exact signed bytes are on the volume before they leave this machine
        self.signed = self.journal.append({'ev': 'signed', 'window': self.window, **kind_field(self.kind), 'tx': txh,
                                           'raw': raw, 'nonce': int(tx['nonce']), **record})
        self.log(f'LIVE: signed ONCE the {self.kind} for the window {self.window} at nonce {tx["nonce"]}: {txh} (the '
                 'raw bytes are in the journal); broadcasting')
        try:
            _r, err = self.rpc.send_raw(raw)
        except Exception as e:                 # every RPC failed: it may still have landed; the receipt decides
            err = f'send raised {type(e).__name__}'
        state = broadcast_state(err)
        self.journal.append({'ev': 'sent', 'window': self.window, **kind_field(self.kind), 'tx': txh,
                             'error': short(err), 'broadcast': state})
        self.sent = {'tx': txh, 'error': short(err), 'broadcast': state}
        return self.sent

    def wait_receipt(self, seconds=None):
        """Blocking: poll the receipt (up to receipt_wait_s), book it (record_receipt). None when none came in time
        ('unconfirmed': resolve() finishes it later)."""
        if not self.signed:
            return None
        txh = self.signed['tx']
        end = time.monotonic() + (self.receipt_wait_s if seconds is None else seconds)
        while True:
            try:
                rc, _ = self.rpc.raw('eth_getTransactionReceipt', [txh])
            except (buyback.RpcError, RuntimeError):
                rc = None
            if rc:
                self.receipt = record_receipt(self.journal, self.window, self.signed, rc, self.log, self.kind)
                return self.receipt
            if time.monotonic() >= end:
                break
            self.sleep(0.5)
        self.journal.append({'ev': 'unconfirmed', 'window': self.window, **kind_field(self.kind), 'tx': txh})
        self.log(f'LIVE: no receipt yet for {txh}; the next start resolves it (receipt, or the identical bytes again)')
        return None


class LiveBuyer(_LiveTx):
    """One LIVE session's buy for one booked window: preflight -> (checks by buyrig) -> re-simulate -> balance ->
    reserve -> sign -> journal -> broadcast -> receipt. At most ONE transaction for the window, ever. The signer (the
    gated LocalAccount; a MockSigner in tests) never leaves this object."""
    kind = KIND_BUY

    def __init__(self, signer, rpc, journal, window, amount_wei, log=log_default, clock=time.time, sleep=time.sleep,
                 receipt_wait_s=RECEIPT_WAIT_S):
        super().__init__(signer, rpc, journal, window, amount_wei, log, clock, sleep, receipt_wait_s)
        if not MIN_BUY_WEI <= self.amount_wei <= MAX_BUY_WEI:
            raise LiveRefused(f'{self.amount_wei} wei is outside 0.00001..{eth(MAX_BUY_WEI)} ETH', 'check')
        self.value_wei = self.amount_wei

    def __repr__(self):
        return f'LiveBuyer(window {self.window}, {eth(self.amount_wei)} ETH)'

    def balance_short(self, gas_hint=None):
        """-> None when the wallet's ETH covers the buy + gas (the gas hint or a pool buy's usual gas, x 1.25, at the fee
        cap, x 1.2), else why not. Read-only."""
        gas = -(-int(gas_hint or buyback.GAS_GUESS['pool']) * GAS_LIMIT_NUM // GAS_LIMIT_DEN)
        gp = int(self.rpc.ok('eth_gasPrice', []), 16)
        max_fee = min(gp * FEE_MULT, MAX_FEE_CAP_WEI)
        bal = int(self.rpc.ok('eth_getBalance', [WALLET, 'latest']), 16)
        need = self.amount_wei + gas * max_fee * BALANCE_NUM // BALANCE_DEN
        return None if bal >= need else f'it holds {eth(bal)} ETH, under {eth(need)} ETH (the buy + gas)'

    # ---- the one transaction
    def sign_and_send(self, data_hex, deadline):
        """Blocking (run it in an executor). data_hex: exactly the calldata bytes buyrig decoded and checked. Every step
        before the reservation can refuse (LiveRefused); after it the window is used, whatever happens. -> self.sent"""
        self._take_turn()
        try:
            data = '0x' + bytes.fromhex(str(data_hex)[2:]).hex()
        except ValueError:
            raise LiveRefused('no decoded calldata to sign', 'check') from None
        amount = self.amount_wei
        try:
            buyback.check_value(ROUTER, data, amount)
            amount_in, settle, _mo, _dl = buyback.router_amounts(data)
        except ValueError as e:
            raise LiveRefused(f'the calldata is not the booked router buy: {e}', 'check') from None
        if not amount_in == settle == amount:
            raise LiveRefused(f'amountIn {amount_in} is not the booked {amount} wei', 'check')
        pre = self.preflight('sign')
        call = {'from': WALLET, 'to': ROUTER, 'data': data, 'value': hex(amount)}
        try:
            _res, err = self.rpc.raw('eth_call', [call, 'latest'])
            g, gerr = (None, None) if err is not None else self.rpc.raw('eth_estimateGas', [call, 'latest'])
        except (buyback.RpcError, RuntimeError) as e:
            raise LiveRefused(f'the re-simulation could not reach the chain ({type(e).__name__})') from None
        if err is not None:
            raise LiveRefused(f'the re-simulation from the buyback wallet (no balance override) failed: {short(err)}',
                              'check')
        if gerr is not None or not g:
            raise LiveRefused(f'eth_estimateGas from the buyback wallet failed: {short(gerr)}', 'check')
        est = int(g, 16)
        gas = -(-est * GAS_LIMIT_NUM // GAS_LIMIT_DEN)
        if gas > MAX_GAS_LIMIT:
            raise LiveRefused(f'gas {gas} is over the {MAX_GAS_LIMIT} limit for a pool buy', 'check')
        try:
            gp = int(self.rpc.ok('eth_gasPrice', []), 16)
            bal = int(self.rpc.ok('eth_getBalance', [WALLET, 'latest']), 16)
            nonce = self._nonce('pending')
        except (buyback.RpcError, RuntimeError, ValueError) as e:
            raise LiveRefused(f'a chain read failed ({type(e).__name__})') from None
        if gp > MAX_GAS_PRICE_WEI:
            raise LiveRefused(f'gas price {gp / 1e9:.4f} gwei is over the {MAX_GAS_PRICE_WEI / 1e9:g} gwei cap', 'failure')
        max_fee = min(gp * FEE_MULT, MAX_FEE_CAP_WEI)
        need = amount + gas * max_fee * BALANCE_NUM // BALANCE_DEN
        if bal < need:
            raise LiveRefused(f'the buyback wallet holds {eth(bal)} ETH, under {eth(need)} ETH (the buy + gas {gas} x '
                              f'maxFee {max_fee / 1e9:.4f} gwei x 1.2)', 'failure')
        if nonce != pre['nonce']:
            raise LiveRefused(f'the wallet nonce moved ({pre["nonce"]} -> {nonce}) during the session', 'failure')
        info = {'amount_wei': amount, 'nonce': nonce, 'deadline': int(deadline), 'gas': gas, 'max_fee': max_fee,
                'calldata_sha256': hashlib.sha256(bytes.fromhex(data[2:])).hexdigest()}
        # from here on this window is used: it can never be signed again (the lock file and the journal say so)
        self.journal.reserve(self.window, info)
        tx = build_tx(nonce, data, amount, gas, max_fee)
        return self._sign_and_broadcast(tx, {'to': ROUTER, 'value': amount, 'gas': gas, 'max_fee': max_fee,
                                             'deadline': int(deadline), 'calldata_sha256': info['calldata_sha256']})


class LiveBurner(_LiveTx):
    """The rig's one burn for one booked window (no pons page, no rat: the burn engine booked it from the maze's
    escapes): preflight -> the token's method -> the 5 % ceiling on the LABRAT balance read now -> build -> the
    checks -> simulate from the real wallet -> gas / fee / balance -> reserve -> sign -> journal -> broadcast ->
    receipt. At most ONE transaction for the burn window, ever. The signer never leaves this object."""
    kind = KIND_BURN

    def __init__(self, signer, rpc, journal, window, amount_wei, method='auto', log=log_default, clock=time.time,
                 sleep=time.sleep, receipt_wait_s=RECEIPT_WAIT_S):
        super().__init__(signer, rpc, journal, window, amount_wei, log, clock, sleep, receipt_wait_s)
        if not MIN_BURN_WEI <= self.amount_wei <= MAX_BURN_WEI:
            raise LiveRefused(f'{buyback.token_str(self.amount_wei, 6)} LABRAT is outside '
                              f'{buyback.token_str(MIN_BURN_WEI, 6)}..{buyback.token_str(MAX_BURN_WEI)} LABRAT (the rig\'s '
                              'bounds for one burn)', 'check')
        if method not in BURN_METHODS:
            raise LiveRefused(f'{method!r} is not one of {BURN_METHODS}', 'check')
        self.method = method
        self.value_wei = 0
        self.checks = None          # check_burn_tx(...) of the transaction that was signed (or refused)

    def __repr__(self):
        return f'LiveBurner(window {self.window}, {buyback.token_str(self.amount_wei)} LABRAT)'

    def token_balance(self):
        """The wallet's LABRAT balance (wei), read now."""
        return int(self.rpc.ok('eth_call', [{'to': TOKEN, 'data': cd_balance_of(WALLET)}, 'latest']), 16)

    # ---- the one transaction
    def sign_and_send(self):
        """Blocking. Every step before the reservation can refuse (LiveRefused; its kind says how it counts); after
        it the burn window is used, whatever happens. -> self.sent"""
        self._take_turn()
        amount = self.amount_wei
        method = token_burn_method(self.rpc, self.method)          # the bytecode decides (or refuses a forced 'burn')
        pre = self.preflight('sign')
        try:
            bal_tok = self.token_balance()
        except (buyback.RpcError, RuntimeError, ValueError) as e:
            raise LiveRefused(f'the wallet\'s LABRAT balance could not be read ({type(e).__name__})') from None
        ceiling = bal_tok * BURN_PCT_MAX_BPS // 10_000
        if amount > ceiling:
            raise LiveRefused(f'{buyback.token_str(amount)} LABRAT is over {BURN_PCT_MAX_BPS / 100:g}% of the wallet\'s '
                              f'{buyback.token_str(bal_tok)} LABRAT ({buyback.token_str(ceiling)}): the rig\'s ceiling '
                              'for one hour', 'check')
        data = burn_calldata(method, amount)
        call = {'from': WALLET, 'to': TOKEN, 'data': data, 'value': '0x0'}
        try:
            _res, err = self.rpc.raw('eth_call', [call, 'latest'])
            g, gerr = (None, None) if err is not None else self.rpc.raw('eth_estimateGas', [call, 'latest'])
        except (buyback.RpcError, RuntimeError) as e:
            raise LiveRefused(f'the simulation could not reach the chain ({type(e).__name__})') from None
        if err is not None:
            raise LiveRefused(f'the simulation of the burn from the buyback wallet failed: {short(err)}', 'check')
        if gerr is not None or not g:
            raise LiveRefused(f'eth_estimateGas of the burn from the buyback wallet failed: {short(gerr)}', 'check')
        est = int(g, 16)
        gas = -(-est * GAS_LIMIT_NUM // GAS_LIMIT_DEN)
        if gas > MAX_BURN_GAS_LIMIT:
            raise LiveRefused(f'gas {gas} is over the {MAX_BURN_GAS_LIMIT} limit for a burn', 'check')
        try:
            gp = int(self.rpc.ok('eth_gasPrice', []), 16)
            bal = int(self.rpc.ok('eth_getBalance', [WALLET, 'latest']), 16)
            nonce = self._nonce('pending')
        except (buyback.RpcError, RuntimeError, ValueError) as e:
            raise LiveRefused(f'a chain read failed ({type(e).__name__})') from None
        if gp > MAX_GAS_PRICE_WEI:
            raise LiveRefused(f'gas price {gp / 1e9:.4f} gwei is over the {MAX_GAS_PRICE_WEI / 1e9:g} gwei cap', 'failure')
        max_fee = min(gp * FEE_MULT, MAX_FEE_CAP_WEI)
        need = gas * max_fee * BALANCE_NUM // BALANCE_DEN
        if bal < need:
            raise LiveRefused(f'the buyback wallet holds {eth(bal)} ETH, under {eth(need)} ETH (gas {gas} x maxFee '
                              f'{max_fee / 1e9:.4f} gwei x 1.2)', 'failure')
        if nonce != pre['nonce']:
            raise LiveRefused(f'the wallet nonce moved ({pre["nonce"]} -> {nonce}) during the session', 'failure')
        tx = build_burn_tx(nonce, data, gas, max_fee)
        self.checks = check_burn_tx(tx, amount, method, self.address)
        bad = [k for k, v in self.checks.items() if not v['ok']]
        if bad:
            raise LiveRefused('the burn transaction failed its checks: '
                              + ', '.join(f"{k} (expected {self.checks[k]['expected']})" for k in bad), 'check')
        sha = hashlib.sha256(bytes.fromhex(tx['data'][2:])).hexdigest()
        info = {'amount_wei': amount, 'method': method, 'nonce': nonce, 'gas': gas, 'max_fee': max_fee,
                'deadline': int(self.clock()) + BURN_TTL_S, 'calldata_sha256': sha,
                'wallet_labrat_wei': bal_tok, 'ceiling_wei': ceiling}
        # from here on this burn window is used: it can never be signed again (the lock file and the journal say so)
        self.journal.reserve(self.window, info, KIND_BURN)
        return self._sign_and_broadcast(tx, {'to': TOKEN, 'value': 0, 'amount_wei': amount, 'method': method,
                                             'gas': gas, 'max_fee': max_fee, 'deadline': info['deadline'],
                                             'calldata_sha256': sha})


def open_buyer(acct, window, amount_wei, log=log_default, transport=None, nodes=None, journal=None, environ=None,
               clock=time.time, sleep=time.sleep, receipt_wait_s=RECEIPT_WAIT_S):
    """After gate(): the gated RPC, the journal on the volume, the operator's switches, and the preflight at session
    start. -> LiveBuyer, or LiveRefused (the session is refused; its kind says how it counts)."""
    if VOLUME is not None and not os.path.ismount(str(VOLUME)):
        raise LiveRefused(f'{VOLUME} is not a mounted volume: the LIVE journal must survive a redeploy')
    rpc = open_rpc(acct, transport, nodes, environ)
    journal = journal or Journal(clock=clock)
    apply_operator_env(journal, rpc, environ, log)
    b = LiveBuyer(acct, rpc, journal, window, amount_wei, log=log, clock=clock, sleep=sleep,
                  receipt_wait_s=receipt_wait_s)
    try:
        b.preflight('start')
    except LiveRefused as e:
        note_refusal(journal, window, e, log)
        raise
    return b


def open_burner(acct, window, amount_wei, method='auto', log=log_default, transport=None, nodes=None, journal=None,
                environ=None, clock=time.time, sleep=time.sleep, receipt_wait_s=RECEIPT_WAIT_S):
    """After burn_gate(): the gated RPC, the journal on the volume, the operator's switches, and the preflight.
    -> LiveBurner, or LiveRefused (the burn is refused; its kind says how it counts)."""
    if VOLUME is not None and not os.path.ismount(str(VOLUME)):
        raise LiveRefused(f'{VOLUME} is not a mounted volume: the LIVE journal must survive a redeploy')
    rpc = open_rpc(acct, transport, nodes, environ)
    journal = journal or Journal(clock=clock)
    apply_operator_env(journal, rpc, environ, log)
    b = LiveBurner(acct, rpc, journal, window, amount_wei, method, log=log, clock=clock, sleep=sleep,
                   receipt_wait_s=receipt_wait_s)
    try:
        b.preflight('start')
    except LiveRefused as e:
        note_refusal(journal, window, e, log, KIND_BURN)
        raise
    return b


def burn_result(window, amount_wei, **over):
    """The public-safe result of --burn (what the runner reads): never a key, never raw bytes."""
    res = {'kind': KIND_BURN, 'window': window, 'amount_wei': str(int(amount_wei)),
           'amount': buyback.eth_str(int(amount_wei), 18), 'mode': 'OFF', 'ok': False, 'signed': False,
           'sent': False, 'tx': None, 'block': None, 'burned': None, 'method': None, 'verdict': None, 'error': None,
           'refusal_kind': None}
    res.update(over)
    return res


def run_burn(window, amount_wei, method='auto', environ=None, gate_rpc=None, log=log_default, transport=None,
             nodes=None, journal=None, clock=time.time, sleep=time.sleep, receipt_wait_s=RECEIPT_WAIT_S):
    """The rig's one burn for a booked window, gated (burn_gate): with any gate missing it is a NO-OP (mode OFF: no
    chain read, no simulation, nothing signed); else at most one transaction, journalled like a buy. The key leaves
    the environment whatever happens. -> burn_result(...): mode OFF | LIVE, verdict burn_off | live_refused |
    unconfirmed | burned | reverted | mined_without_burn, plus signed / sent / tx / block / burned / error."""
    amount_wei = int(amount_wei)
    burner = None

    def logf(m):
        log(burner.redact(m) if burner is not None else m)

    try:
        acct, why = burn_gate(window, environ, gate_rpc)
        if acct is None:
            return burn_result(window, amount_wei, verdict='burn_off', error=why)
        try:
            burner = open_burner(acct, window, amount_wei, method, log=logf, transport=transport, nodes=nodes,
                                 journal=journal, environ=environ, clock=clock, sleep=sleep,
                                 receipt_wait_s=receipt_wait_s)
        except LiveRefused as e:
            return burn_result(window, amount_wei, mode='LIVE', verdict='live_refused', error=str(e)[:400],
                               refusal_kind=e.kind)
        finally:
            acct = None
        try:
            sent = burner.sign_and_send()
        except LiveRefused as e:
            note_refusal(burner.journal, window, e, logf, KIND_BURN)
            return burn_result(window, amount_wei, mode='LIVE', verdict='live_refused',
                               error=burner.redact(str(e))[:400], refusal_kind=e.kind,
                               method=(burner.signed or {}).get('method'))
        res = burn_result(window, amount_wei, mode='LIVE', signed=True, sent=True, tx=sent['tx'],
                          method=burner.signed.get('method'), broadcast=sent['broadcast'], send_error=sent['error'])
        info = burner.wait_receipt()
        if info is None:
            return dict(res, verdict='unconfirmed')
        return dict(res, ok=bool(info['ok']), block=info['block'],
                    burned=buyback.eth_str(int(info.get('burned_wei') or 0), 18),
                    verdict='burned' if info['ok'] else 'reverted' if info['status'] != 1 else 'mined_without_burn')
    finally:
        drop_key(environ)


def status(journal, now=None):
    """What --status prints: no key, no raw transaction. Buys and burns, each slot with its kind."""
    st = journal.state()
    now = time.time() if now is None else now
    wins = []
    for _key, w in sorted(st.windows.items(), key=lambda kv: (kv[1]['window'], kv[1]['kind']))[-24:]:
        win, kind = w['window'], w['kind']
        ok = bool(w['receipt'] and w['receipt'].get('ok'))
        state = (('bought' if kind == KIND_BUY else 'burned') if ok else 'reverted' if w['receipt'] else
                 'expired' if w['expired'] else 'unresolved' if w['signed'] else
                 'reserved, not signed' if w['reserved'] else 'failed' if w['failed'] else '?')
        row = {'window': win, 'kind': kind, 'state': state, 'tx': (w['signed'] or {}).get('tx'),
               'failed': (w['failed'] or {}).get('why')}
        if kind == KIND_BURN:
            amount = (w['signed'] or {}).get('amount_wei') or (w['reserved'] or {}).get('amount_wei') or 0
            row.update(labrat=buyback.token_str(amount), method=(w['signed'] or w['reserved'] or {}).get('method'),
                       labrat_burned=buyback.token_str((w['receipt'] or {}).get('burned_wei') or 0))
        else:
            amount = (w['signed'] or {}).get('value') or (w['reserved'] or {}).get('amount_wei') or 0
            row.update(eth=eth(amount), labrat_out=buyback.token_str((w['receipt'] or {}).get('labrat_out_wei') or 0))
        wins.append(row)
    return {'journal': str(journal.path), 'records': st.records, 'unreadable_lines': st.bad_lines,
            'stopped': ({k: st.stop.get(k) for k in ('id', 'kind', 'why', 'at', 'window')} if st.stop else None),
            'failures_in_a_row': st.failures, 'expected_nonce': st.expected_nonce(),
            'unresolved': sorted(st.unresolved()), 'signed_last_day_eth': eth(st.signed_spend(now, 86400)),
            'signed_total_eth': eth(st.signed_spend(now)), 'burns': len(st.executed_burns()),
            'burned_total_labrat': buyback.token_str(st.burned_total_wei()), 'windows': wins}


def abandon(journal, window, nodes=None, log=log_default, kind=KIND_BUY):
    """Operator: a signed buy (or burn: kind) whose nonce another transaction of the wallet used, with no receipt on
    ANY pinned RPC and unknown to all of them, can never land: mark it expired. Anything else is refused. Reads only."""
    st = journal.state()
    w = st.unresolved().get(slot(window, kind))
    if not w:
        raise SystemExit(f'{window} has no unresolved signed {kind}')
    rec = w['signed']
    nodes = nodes if nodes is not None else [buyback._node_transport(u) for u in launcher.RPCS]
    for node in nodes:
        rc, err = node('eth_getTransactionReceipt', [rec['tx']])
        known, err2 = node('eth_getTransactionByHash', [rec['tx']])
        n, err3 = node('eth_getTransactionCount', [WALLET, 'latest'])
        if err or err2 or err3 or rc or known or int(n, 16) <= int(rec['nonce']):
            raise SystemExit('refused: an RPC has a receipt for it, knows it, could not answer, or its nonce is still '
                             'free (then resolve() finishes it)')
    journal.append({'ev': 'expired', 'window': window, **kind_field(kind), 'tx': rec['tx'],
                    'why': 'operator --abandon: its nonce was used by another transaction; every RPC has no receipt'})
    record_failure(journal, window, 'abandoned by the operator: its nonce was used by another transaction', log, kind)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description='the buy rig\'s LIVE journal (switched off unless every gate holds)')
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--status', action='store_true')
    g.add_argument('--clear-stop', metavar='ID')
    g.add_argument('--resolve', action='store_true')
    g.add_argument('--abandon', metavar='WINDOW')
    g.add_argument('--burn', action='store_true',
                   help='the runner\'s child: ONE burn for the booked --window of --amount-wei LABRAT wei; a no-op '
                        f'unless {ENV_BURN_LIVE}=1, {ENV_BURN_CONFIRM}={SYMBOL}, the key and chain {CHAIN_ID} hold')
    ap.add_argument('--journal', default=None, help=f'default {JOURNAL_PATH}')
    ap.add_argument('--kind', choices=KINDS, default=KIND_BUY, help='--abandon: the slot\'s kind (default buy)')
    ap.add_argument('--window', help='--burn: the booked UTC hour, e.g. 2026-09-25T20:00:00Z')
    ap.add_argument('--amount-wei', help='--burn: the booked amount in LABRAT wei (an integer)')
    ap.add_argument('--burn-method', choices=BURN_METHODS, default='auto',
                    help='--burn: auto reads the token bytecode (burn(uint256) when it has it, else transfer to dead)')
    ap.add_argument('--result-json', help='--burn: also write the public-safe result here')
    a = ap.parse_args(argv)
    j = Journal(a.journal)
    if a.status:
        print(json.dumps(status(j), indent=2))
        return 0
    if a.clear_stop:
        ok = clear_stop(j, a.clear_stop, 'operator --clear-stop')
        print('cleared' if ok else 'no stop with that id')
        return 0 if ok else 1
    if a.abandon:
        abandon(j, a.abandon, kind=a.kind)
        print('marked expired')
        return 0
    if a.burn:
        try:
            wei = int(str(a.amount_wei or '').strip())
            if not a.window or wei <= 0:
                raise ValueError
        except ValueError:
            drop_key()
            raise SystemExit('--burn needs --window <UTC hour> and --amount-wei <a positive integer>') from None
        res = run_burn(a.window, wei, a.burn_method, journal=j)
        print('BURN_RESULT ' + json.dumps(res, sort_keys=True, default=str), flush=True)
        if a.result_json:
            with open(a.result_json, 'w', encoding='utf-8', newline='\n') as fh:
                json.dump(res, fh, indent=1, default=str)
        return 0 if res['ok'] else 1
    acct, why = env_gate()
    if acct is None:
        acct, why = burn_env_gate()
    drop_key()
    if acct is None:
        raise SystemExit(f'--resolve needs the LIVE environment ({why})')
    out = resolve(j, open_rpc(acct))
    print(json.dumps(out, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
