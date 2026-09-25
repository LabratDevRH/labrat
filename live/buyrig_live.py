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
    """The environment gates. -> (account, '') or (None, why). The key's text never appears in why."""
    env = os.environ if environ is None else environ
    if env.get(ENV_LIVE) != '1':
        return None, f'{ENV_LIVE} is not 1'
    if env.get(ENV_CONFIRM) != SYMBOL:
        return None, f'{ENV_CONFIRM} is not {SYMBOL}'
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
class State:
    """The journal replayed: each window's records, the consecutive failed buys, the stop, the nonce account."""

    def __init__(self):
        self.windows = {}
        self.failures = 0
        self.stop = None
        self.anchor = None
        self.mined_since_anchor = 0
        self.bad_lines = 0
        self.records = 0

    def w(self, window):
        return self.windows.setdefault(window, {'reserved': None, 'signed': None, 'sent': [], 'receipt': None,
                                                'expired': None, 'failed': None})

    def apply(self, r):
        ev, win = r.get('ev'), r.get('window')
        self.records += 1
        if ev == 'reserved':
            self.w(win)['reserved'] = r
        elif ev == 'signed':
            self.w(win)['signed'] = r
        elif ev in ('sent', 'rebroadcast'):
            self.w(win)['sent'].append(r)
        elif ev == 'receipt':
            w = self.w(win)
            if w['receipt'] is None:
                self.mined_since_anchor += 1          # our transaction was mined: it used a nonce
            w['receipt'] = r
            if r.get('ok'):
                self.failures = 0
        elif ev == 'expired':
            self.w(win)['expired'] = r
        elif ev == 'failed':
            self.w(win)['failed'] = r
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

    def used(self, window):
        w = self.windows.get(window)
        return bool(w and (w['reserved'] or w['signed']))

    def unresolved(self):
        """Windows with a signed transaction that has neither a receipt nor an 'expired' record."""
        return {win: w for win, w in self.windows.items() if w['signed'] and not w['receipt'] and not w['expired']}

    def bought(self, window):
        w = self.windows.get(window)
        return bool(w and w['receipt'] and w['receipt'].get('ok'))

    def expected_nonce(self):
        return (FIRST_NONCE if self.anchor is None else self.anchor) + self.mined_since_anchor

    def signed_spend(self, now, seconds=None):
        """ETH (wei) of every signed transaction (a signed one may still land), in the last `seconds` or ever."""
        return sum(int(w['signed'].get('value', 0)) for w in self.windows.values()
                   if w['signed'] and (seconds is None or now - float(w['signed'].get('t', 0)) < seconds))

    def executed(self):
        """[{window, tx, amount_wei, labrat_out_wei, block}] for every window whose buy was mined with LABRAT received."""
        out = []
        for win, w in sorted(self.windows.items()):
            rc = w['receipt']
            if w['signed'] and rc and rc.get('ok'):
                out.append({'window': win, 'tx': w['signed']['tx'], 'amount_wei': int(w['signed']['value']),
                            'labrat_out_wei': int(rc.get('labrat_out_wei') or 0), 'block': rc.get('block')})
        return out


class Journal:
    """Append-only JSONL on the volume, one fsync'd line per record, plus one lock file per reserved window."""

    def __init__(self, path=None, clock=time.time):
        self.path = Path(path) if path is not None else JOURNAL_PATH
        self.clock = clock
        self.lock = threading.Lock()

    @property
    def lock_dir(self):
        return self.path.parent / 'windows'

    def lock_path(self, window):
        return self.lock_dir / (window.replace('-', '').replace(':', '') + '.lock')

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

    def reserve(self, window, info):
        """The window's lock file by exclusive create (two processes can never both get past it), then 'reserved'. From
        here on this window can never be signed again, whatever happens next."""
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        p = self.lock_path(window)
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise LiveRefused(f'the window {window} is already reserved: one transaction per window, never two') from None
        with os.fdopen(fd, 'w') as f:
            f.write(json.dumps({'window': window, 'at': buyback.iso(self.clock()), 'pid': os.getpid()}))
            f.flush()
            os.fsync(f.fileno())
        return self.append({'ev': 'reserved', 'window': window, **info})


def stop(journal, why, window=None, kind='check', log=log_default):
    """Journal a LIVE stop (it survives restarts until an operator clears it by its id)."""
    t = journal.clock()
    sid = hashlib.sha256(f'{t}|{why}|{window}'.encode()).hexdigest()[:8]
    rec = journal.append({'ev': 'stop', 'id': sid, 'kind': kind, 'why': str(why)[:400], 'window': window})
    log(f'LIVE STOPPED ({kind}): {str(why)[:300]} · stop id {sid} (clear it with {ENV_CLEAR_STOP}={sid} after checking)')
    return rec


def record_failure(journal, window, why, log=log_default):
    """One failed buy for this window (at most one per window; never for a window whose buy was mined, nor while its
    signed transaction is unresolved: its receipt decides). MAX_FAILURES in a row stop LIVE."""
    st = journal.state()
    w = st.windows.get(window)
    if w and (w['failed'] or st.bought(window)):
        return None
    if w and w['signed'] and not w['receipt'] and not w['expired']:
        return None
    n = st.failures + 1
    rec = journal.append({'ev': 'failed', 'window': window, 'why': str(why)[:300], 'consecutive': n})
    log(f'LIVE: failed buy for {window} ({n} in a row): {str(why)[:200]}')
    if n >= MAX_FAILURES and not st.stop:
        stop(journal, f'{n} failed buys in a row (last: {str(why)[:200]})', window, 'failures', log)
    return rec


def clear_stop(journal, sid, why='operator', log=log_default):
    st = journal.state()
    if not st.stop or not sid or st.stop.get('id') != sid:
        return None
    rec = journal.append({'ev': 'stop_cleared', 'id': sid, 'cleared': st.stop.get('why'), 'why': why})
    log(f'LIVE stop {sid} cleared by the operator')
    return rec


def note_refusal(journal, window, e, log=log_default):
    """A LiveRefused, booked by its kind: 'check' stops LIVE, 'failure' counts, 'blocked' is only logged."""
    if e.kind == 'check':
        return stop(journal, f'a check failed for the window {window}: {e}', window, 'check', log)
    if e.kind == 'failure':
        return record_failure(journal, window, str(e), log)
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


def record_receipt(journal, window, signed, rc, log=log_default):
    """Book a receipt for a signed window (ONE 'receipt' record resolves it). A revert is a failed buy; mined without
    LABRAT reaching the wallet is a broken rule: LIVE stops."""
    info = read_receipt(rc)
    journal.append({'ev': 'receipt', 'window': window, 'tx': signed['tx'], **info})
    if info['ok']:
        log(f"LIVE: mined in block {info['block']}: {buyback.token_str(info['labrat_out_wei'])} LABRAT to the buyback "
            f'wallet · {launcher.EXPLORER}/tx/{signed["tx"]}')
    elif info['status'] == 1:
        stop(journal, f"the buy for {window} was mined without LABRAT reaching the buyback wallet ({signed['tx']})",
             window, 'check', log)
    else:
        record_failure(journal, window, f"the buy reverted on chain ({signed['tx']})", log)
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
    for win, w in sorted(st.unresolved().items()):
        rec = w['signed']
        txh = rec['tx']
        try:
            rc = _receipt_any(rpc, txh)
            if rc:
                record_receipt(journal, win, rec, rc, log)
                out['resolved'].append(win)
                continue
            if _known_any(rpc, txh):
                out['pending'].append(win)                 # in a mempool: wait for it
                continue
            nonces = _nonces(rpc)
            if nonces is None:
                out['pending'].append(win)
                continue
            if all(n <= int(rec['nonce']) for n in nonces):
                # its nonce is still free on every RPC: it never landed. After its router deadline it can buy nothing
                blk = rpc.ok('eth_getBlockByNumber', ['latest', False])
                if int(blk['timestamp'], 16) > int(rec.get('deadline') or 0) + EXPIRE_MARGIN_S:
                    journal.append({'ev': 'expired', 'window': win, 'tx': txh,
                                    'why': 'never mined, and its router deadline passed: it can no longer buy anything '
                                           '(its nonce goes to the next transaction)'})
                    record_failure(journal, win, 'the signed buy never reached the chain', log)
                    out['resolved'].append(win)
                    continue
                _r, err = rpc.send_raw(rec['raw'])
                journal.append({'ev': 'rebroadcast', 'window': win, 'tx': txh, 'error': short(err),
                                'broadcast': broadcast_state(err)})
                log(f'LIVE: re-broadcast the identical signed buy for {win} ({txh})'
                    + (f': {short(err)}' if err else ''))
                out['pending'].append(win)
                continue
            if all(n > int(rec['nonce']) for n in nonces):
                why = (f"the buyback wallet used nonce {rec['nonce']} for another transaction, and no RPC has a receipt "
                       f'for the signed buy {txh} of {win}: it can never land. An operator must check it '
                       f'(python live/buyrig_live.py --abandon {win})')
                if not st.stop:
                    stop(journal, why, win, 'nonce', log)
                    st = journal.state()
                out['stopped'] = why
            out['pending'].append(win)
        except (buyback.RpcError, RuntimeError, KeyError, TypeError, ValueError) as e:
            log(f'LIVE: resolving {win} failed for now ({type(e).__name__}); it is retried')
            out['pending'].append(win)
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


# ---------------------------------------------------------------------------------------------------- the signer
class LiveBuyer:
    """One LIVE session's buy for one booked window: preflight -> (checks by buyrig) -> re-simulate -> balance ->
    reserve -> sign -> journal -> broadcast -> receipt. At most ONE transaction for the window, ever. The signer (the
    gated LocalAccount; a MockSigner in tests) never leaves this object."""

    def __init__(self, signer, rpc, journal, window, amount_wei, log=log_default, clock=time.time, sleep=time.sleep,
                 receipt_wait_s=RECEIPT_WAIT_S):
        if getattr(signer, 'address', None) != WALLET:
            raise LiveRefused('the signer is not the pinned buyback wallet')
        if not isinstance(rpc, SendRpc):
            raise LiveRefused('LIVE needs the gated SendRpc')
        window_start_t(window)
        amount_wei = int(amount_wei)
        if not MIN_BUY_WEI <= amount_wei <= MAX_BUY_WEI:
            raise LiveRefused(f'{amount_wei} wei is outside 0.00001..{eth(MAX_BUY_WEI)} ETH', 'check')
        self._signer = signer
        self.address = signer.address
        self.rpc, self.journal, self.window, self.amount_wei = rpc, journal, window, amount_wei
        self.log, self.clock, self.sleep, self.receipt_wait_s = log, clock, sleep, receipt_wait_s
        self._lock = threading.Lock()
        self.used = False
        self.signed = None          # the 'signed' journal record (tx hash, raw bytes, nonce, gas, fee)
        self.sent = None            # {'tx', 'error', 'broadcast'}
        self.receipt = None         # read_receipt(...) once mined
        self.nonce = None

    def __repr__(self):
        return f'LiveBuyer(window {self.window}, {eth(self.amount_wei)} ETH)'

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

    def balance_short(self, gas_hint=None):
        """-> None when the wallet's ETH covers the buy + gas (the gas hint or a pool buy's usual gas, x 1.25, at the fee
        cap, x 1.2), else why not. Read-only."""
        gas = -(-int(gas_hint or buyback.GAS_GUESS['pool']) * GAS_LIMIT_NUM // GAS_LIMIT_DEN)
        gp = int(self.rpc.ok('eth_gasPrice', []), 16)
        max_fee = min(gp * FEE_MULT, MAX_FEE_CAP_WEI)
        bal = int(self.rpc.ok('eth_getBalance', [WALLET, 'latest']), 16)
        need = self.amount_wei + gas * max_fee * BALANCE_NUM // BALANCE_DEN
        return None if bal >= need else f'it holds {eth(bal)} ETH, under {eth(need)} ETH (the buy + gas)'

    def preflight(self, stage='start'):
        """-> {'nonce': n} when this window may be signed now, else LiveRefused (its kind says how it counts). Resolves
        unfinished windows first (receipts / identical re-broadcasts; never a new transaction)."""
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
                raise LiveRefused('an earlier signed buy is not resolved yet (no receipt): nothing new is signed until '
                                  'it is')
        if st.used(self.window) or j.lock_path(self.window).exists():
            raise LiveRefused(f'the window {self.window} already has its transaction reserved or signed: one per window')
        left = time_left(self.window, now)
        if left is None:
            raise LiveRefused(f'the window {self.window} has not ended: its buy cannot have been booked', 'check')
        if left <= 0:
            raise LiveRefused(f'too late for the window {self.window}: a window is signed at most one hour after it ends',
                              'blocked' if stage == 'start' else 'failure')
        day, total = st.signed_spend(now, 86400), st.signed_spend(now)
        if day + self.amount_wei > MAX_DAY_WEI or total + self.amount_wei > MAX_TOTAL_WEI:
            raise LiveRefused(f'the rig\'s own caps: {eth(day)} ETH signed in the last day, {eth(total)} ETH ever, plus '
                              f'{eth(self.amount_wei)} is over {eth(MAX_DAY_WEI)} / {eth(MAX_TOTAL_WEI)}', 'check')
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

    # ---- the one transaction
    def sign_and_send(self, data_hex, deadline):
        """Blocking (run it in an executor). data_hex: exactly the calldata bytes buyrig decoded and checked. Every step
        before the reservation can refuse (LiveRefused); after it the window is used, whatever happens. -> self.sent"""
        with self._lock:
            if self.used:
                raise LiveRefused('this session already handled its one buy')
            self.used = True
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
        try:
            signed = self._signer.sign_transaction(tx)
            raw = '0x' + bytes(signed.raw_transaction).hex()
            txh = '0x' + keccak(bytes.fromhex(raw[2:])).hex()
        except Exception as e:                 # the message never reaches the logs: it could hold anything
            raise LiveRefused(f'signing failed ({type(e).__name__})', 'failure') from None
        # write-ahead: the exact signed bytes are on the volume before they leave this machine
        self.signed = self.journal.append({'ev': 'signed', 'window': self.window, 'tx': txh, 'raw': raw, 'nonce': nonce,
                                           'to': ROUTER, 'value': amount, 'gas': gas, 'max_fee': max_fee,
                                           'deadline': int(deadline), 'calldata_sha256': info['calldata_sha256']})
        self.log(f'LIVE: signed ONCE for the window {self.window} at nonce {nonce}: {txh} (the raw bytes are in the '
                 'journal); broadcasting')
        try:
            _r, err = self.rpc.send_raw(raw)
        except Exception as e:                 # every RPC failed: it may still have landed; the receipt decides
            err = f'send raised {type(e).__name__}'
        state = broadcast_state(err)
        self.journal.append({'ev': 'sent', 'window': self.window, 'tx': txh, 'error': short(err), 'broadcast': state})
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
                self.receipt = record_receipt(self.journal, self.window, self.signed, rc, self.log)
                return self.receipt
            if time.monotonic() >= end:
                break
            self.sleep(0.5)
        self.journal.append({'ev': 'unconfirmed', 'window': self.window, 'tx': txh})
        self.log(f'LIVE: no receipt yet for {txh}; the next start resolves it (receipt, or the identical bytes again)')
        return None


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


def status(journal, now=None):
    """What --status prints: no key, no raw transaction."""
    st = journal.state()
    now = time.time() if now is None else now
    wins = []
    for win, w in sorted(st.windows.items())[-24:]:
        state = ('bought' if st.bought(win) else 'reverted' if w['receipt'] else 'expired' if w['expired']
                 else 'unresolved' if w['signed'] else 'reserved, not signed' if w['reserved'] else
                 'failed' if w['failed'] else '?')
        amount = (w['signed'] or {}).get('value') or (w['reserved'] or {}).get('amount_wei') or 0
        wins.append({'window': win, 'state': state, 'tx': (w['signed'] or {}).get('tx'), 'eth': eth(amount),
                     'labrat_out': buyback.token_str((w['receipt'] or {}).get('labrat_out_wei') or 0),
                     'failed': (w['failed'] or {}).get('why')})
    return {'journal': str(journal.path), 'records': st.records, 'unreadable_lines': st.bad_lines,
            'stopped': ({k: st.stop.get(k) for k in ('id', 'kind', 'why', 'at', 'window')} if st.stop else None),
            'failures_in_a_row': st.failures, 'expected_nonce': st.expected_nonce(),
            'unresolved': sorted(st.unresolved()), 'signed_last_day_eth': eth(st.signed_spend(now, 86400)),
            'signed_total_eth': eth(st.signed_spend(now)), 'windows': wins}


def abandon(journal, window, nodes=None, log=log_default):
    """Operator: a signed buy whose nonce another transaction of the wallet used, with no receipt on ANY pinned RPC and
    unknown to all of them, can never land: mark it expired. Anything else is refused. Reads only."""
    st = journal.state()
    w = st.unresolved().get(window)
    if not w:
        raise SystemExit(f'{window} has no unresolved signed buy')
    rec = w['signed']
    nodes = nodes if nodes is not None else [buyback._node_transport(u) for u in launcher.RPCS]
    for node in nodes:
        rc, err = node('eth_getTransactionReceipt', [rec['tx']])
        known, err2 = node('eth_getTransactionByHash', [rec['tx']])
        n, err3 = node('eth_getTransactionCount', [WALLET, 'latest'])
        if err or err2 or err3 or rc or known or int(n, 16) <= int(rec['nonce']):
            raise SystemExit('refused: an RPC has a receipt for it, knows it, could not answer, or its nonce is still '
                             'free (then resolve() finishes it)')
    journal.append({'ev': 'expired', 'window': window, 'tx': rec['tx'],
                    'why': 'operator --abandon: its nonce was used by another transaction; every RPC has no receipt'})
    record_failure(journal, window, 'abandoned by the operator: its nonce was used by another transaction', log)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description='the buy rig\'s LIVE journal (switched off unless every gate holds)')
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--status', action='store_true')
    g.add_argument('--clear-stop', metavar='ID')
    g.add_argument('--resolve', action='store_true')
    g.add_argument('--abandon', metavar='WINDOW')
    ap.add_argument('--journal', default=None, help=f'default {JOURNAL_PATH}')
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
        abandon(j, a.abandon)
        print('marked expired')
        return 0
    acct, why = env_gate()
    drop_key()
    if acct is None:
        raise SystemExit(f'--resolve needs the LIVE environment ({why})')
    out = resolve(j, open_rpc(acct))
    print(json.dumps(out, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
