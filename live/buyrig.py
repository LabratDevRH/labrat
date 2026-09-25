"""RATBRAIN buy rig: the rat's trained brain clicks through pons's own BUY flow for $LABRAT on the REAL coin page,
once per buyback batch, and the rig refuses to sign. DRY ONLY: there is no LIVE path in this file.

    python live/buyrig.py --amount 0.0001 [--seed 2026] [--relay wss://<relay>/publish] [--batch-at <iso>] [--hits N]
                          [--policy runs/final/steer.pt] [--press-policy runs/final/policy.pt] [--headful]
                          [--result-json <path>] [--dev-oracle]

What happens in one session (research: live/BUYRIG_RESEARCH.md)
  * The rig opens https://www.ponsfamily.com/launchpad/<LABRAT> (1280x900, dark) in Playwright Chromium with
    ponsbot's injected EIP-1193 wallet: a fresh throwaway in-memory key per session and the DRY balance override (pons
    reads 1 ETH for it, so it enables Buy). Signing requests of any kind are refused (BuyBot); pons never asked for one
    in the research runs.
  * Before pons loads, a MASK is installed (MASK_JS, the research's tested script): the CSS Custom Highlight API paints
    flat bars over every 0x string, the wallet chip, the balance lines ("1 available", "0 available"), the creator-fee
    amounts and pons's error-toast details; the Holders panel and the toast details are visibility:hidden. It edits no
    pons text and adds no node inside pons's tree, so React, the flow and hit-testing are untouched.
  * The rat's brain (brainrig.Run: session.Session, the two trained networks) runs in real time. The rig lights one
    target at a time; the rat's head steers the cursor and its lever press clicks; a click outside the lit target is
    never forwarded to the page. Targets, in order:
        pons's terms gate (a new browser = a new wallet each session): Terms of Use, Privacy Policy, Accept and continue
        (the owner accepted pons's terms and authorized the rat to click them, as for the launch)
        the ETH amount field  -> the rig types the batch amount at 90 ms/char and waits for pons's quote
        Buy LABRAT            -> pons opens its "Review buy" dialog; the rig reads it (You send = the amount,
                                 Market = Uniswap v4 pool, Max slippage = 1%)
        Confirm buy           -> pons calls eth_sendTransaction once
    pons's trade panel opens in BUY mode, so there is no Buy tab to click; the rig checks the mode and never lights
    the buy/sell toggle, the presets, Max, the pay-asset menu, the profile chip or the Holders tab.
  * The transaction pons built is decoded and CHECKED (inspect_buy): to = the pons router, selector execute, one
    V4_SWAP of the pinned $LABRAT pool (key, id, hook), value == amountIn == SETTLE_ALL == the batch amount, from = the
    page wallet, min out > 0 and within MIN_OUT_FLOOR_PCT..100 % of a fresh quoter quote, a sane deadline, the request
    carries only from / to / value / data, the calldata is byte-identical to buyback.cd_router_buy (nothing else in
    it), pons's review dialog said the same, and an eth_call of the exact transaction (with the 1 ETH override, right
    at capture) succeeds. Then the rig answers EIP-1193 4001 ("Simulated buy: not signed"). Nothing is signed, nothing
    is sent: every chain call here goes through buyback.ReadRpc, which refuses any method that is not a read.
  * Recorded to runs/buyrig_<UTC>_seed<seed>/ (local, not committed): steer.pt + press.pt (read-only), session.json +
    qpos.npy + actions.npy (session.save; `python replay_session.py <dir>` must print MATCH), events.jsonl,
    targets.json, captured_tx.json (the raw request, local only), frames/*.jpg + frames.json (masked screenshots, each
    between two clean mask audits), pons_final.jpg, buyrig_result.json (public-safe: no address, no calldata).
    session.json carries no address and no calldata either.

Streaming (--relay; token from the LABRAT_PUBLISH_TOKEN environment variable, never .env)
  WS <relay>?channel=pons, header Authorization: Bearer <token>. Text JSON messages (MSG, relay/relay.py PONS_TYPES;
  the fields site/js/site.js reads):
    pons_hello   source "buyrig", session, started, label "Simulated", simulated true, amount_eth, targets (short
                 names; re-sent without the terms targets when pons shows no terms gate)
    pons_step    i / n (the target's place), target (its short name), state, phase (PHASES: light, aim, press, miss,
                 type, quote, review, check, done), box while lit
    pons_result  kind "tx" (the checked transaction: ok, eth_in, labrat_out = a fresh quote for the exact swap, min
                 out, the checks by name, the simulation, signed false) and kind "done" (ok, reason when not ok
                 (REASONS), the session proof); then pons_bye.
  PonsLink refuses locally any text holding a 0x string, internal wording or over 4,096 bytes. Binary frames:
  b"PJPG" + one masked JPEG
  (<= RELAY_MAX_FRAME bytes, <= RELAY_FPS per second). A frame is forwarded only when the
  mask audit (window.__ratMaskAudit + a check of the sensitive elements against the painted highlight) is clean both
  right after it was received and at the previous forwarded frame (so every forwarded frame sits between two clean
  audits). A real audit failure withholds every later frame and stops the session. No logs are ever streamed.

Honest scope: the rat is two trained artificial neural networks driving a simulated rat body; the code sets every rule
(which target, which amount, when). pons's own UI shows its own words (its review and its "Trade did not settle" toast
after the refusal); everything this rig publishes is labelled "Simulated".
"""
import base64
import collections
import hashlib
import json
import os
import re
import sys
import threading
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

LIVE_DIR = Path(__file__).resolve().parent
ROOT = LIVE_DIR.parent
for _p in (str(ROOT), str(LIVE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import brainrig  # noqa: E402  (first: it sets the 32 MB thread stacks before any thread exists)
import argparse  # noqa: E402
import asyncio  # noqa: E402
import stat  # noqa: E402
import traceback  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

import buyback  # noqa: E402
import ponsbot  # noqa: E402
import session as brain_session  # noqa: E402
from brainrig import StageFailed, RunEnded, Target  # noqa: E402
from eth_abi import decode  # noqa: E402
from eth_utils import to_checksum_address  # noqa: E402

SYMBOL = buyback.SYMBOL                                   # LABRAT
COIN_URL = f'https://www.ponsfamily.com/launchpad/{buyback.TOKEN}'
VIEW_W, VIEW_H = brainrig.VIEW_W, brainrig.VIEW_H         # 1280 x 900
REFUSAL_MSG = 'Simulated buy: not signed'                 # pons repeats it in its (masked) toast details
EXECUTE = buyback.SEL['execute']                          # 0x3593564c
T6 = f'({buyback.POOL_KEY_T},bool,uint128,uint128,uint256,bytes)'
EXTRA_TX_FIELDS = ('gas', 'gasPrice', 'maxFeePerGas', 'maxPriorityFeePerGas', 'nonce', 'type', 'accessList',
                   'authorizationList', 'blobVersionedHashes', 'maxFeePerBlobGas')
MIN_OUT_FLOOR_PCT = 97          # pons sets min out = quote - 1 %; 2 % more for the seconds between its quote and ours
DEADLINE_MIN_S = 30             # pons sets deadline = now + 1200 s
DEADLINE_MAX_S = 1260
AMOUNT_MIN_WEI = 10 ** 13       # 0.00001 ETH, one hit
AMOUNT_MAX_WEI = buyback.HARD['max_buy_wei']              # 0.01 ETH, the engine's hard per-buy ceiling
QUOTE_WAIT_S = 20.0
REVIEW_WAIT_S = 10.0
TOAST_WAIT_S = 10.0
RELAY_FPS = 10.0
RELAY_MAX_FRAME = 120_000       # under 131,072 bytes (the relay's older --ws-max-size; relay.py now takes 256 KB)
FRAME_PREFIX = b'PJPG'
CHECKPOINT_QUALITY = 82
LABEL = 'Simulated'
TOKEN_ENV = 'LABRAT_PUBLISH_TOKEN'
SOURCE = 'buyrig'               # pons_hello.source: the relay's pons channel takes only this
# The message types on the relay's pons channel (relay/relay.py PONS_TYPES). The checked transaction and the final
# summary are both a pons_result ("kind": "tx" / "done"; the relay keeps the latest for late joiners), then pons_bye.
MSG = {'hello': 'pons_hello', 'step': 'pons_step', 'tx': 'pons_result', 'done': 'pons_result', 'bye': 'pons_bye'}
PUBLIC_TEXT_MAX = 4096          # the relay drops a pons text over 4,096 bytes (as ASCII JSON)
PUBLIC_NOTE = ("Simulated buy. The rat's trained neural networks steer the cursor and click; the rig lights each step "
               'and types the amount after the rat clicks the field. pons builds the buy transaction; it is checked and '
               'simulated on the live chain, and it is not signed or sent. Wallet details on the pons page are hidden.')
HONESTY = ("The rat's brain is two trained neural networks driving a simulated rat body (DeepMind rodent model in "
           "MuJoCo): a steering network turns its head, which moves the cursor, and a lever-press network performs "
           'each press, which clicks. They are artificial networks, not a biological brain. The rig lights the next '
           "target on pons's buy page, types the batch amount after the rat clicks the amount field, and ignores "
           "clicks outside the lit target. pons builds the buy transaction; the rig checks it, simulates it and "
           'refuses to sign. Nothing is signed or sent.')

# (stage key, label, kind, arg). The terms targets are skipped when pons shows no terms gate.
BUY_TARGETS = (
    ('b01_terms_tou', 'terms checkbox: Terms of Use', 'terms', 0),
    ('b02_terms_privacy', 'terms checkbox: Privacy Policy', 'terms', 1),
    ('b03_accept', 'Accept and continue', 'accept', None),
    ('b04_amount', 'ETH amount field', 'amount', None),
    ('b05_buy', 'Buy LABRAT', 'buy', None),
    ('b06_confirm', 'Confirm buy', 'confirm_buy', None),
)
# the short target names the public stream carries (site.js shows only names matching /^[A-Za-z][A-Za-z0-9 ()&'.,+-]*$/)
PUBLIC_NAME = {'b01_terms_tou': 'Terms of Use', 'b02_terms_privacy': 'Privacy Policy',
               'b03_accept': 'Accept and continue', 'b04_amount': 'ETH amount', 'b05_buy': 'Buy LABRAT',
               'b06_confirm': 'Confirm buy'}
# pons_step "phase" keywords (site.js PHASE): light, aim, press, miss, type, quote, review, check, done
PHASES = ('light', 'aim', 'press', 'miss', 'type', 'quote', 'review', 'check', 'done')
# pons_result "reason" keywords when ok is false (site.js REASON)
REASONS = ('checks_failed', 'simulation_failed', 'quote_failed', 'timeout', 'aborted')
AIM_AFTER_S = 0.8               # a lit target still waiting for the rat after this long: phase "aim"
MISS_PUBLISH_GAP_S = 1.0        # at most one "miss" step a second on the stream (every miss is recorded locally)
PUBLIC_CHECKS = {       # check key -> what the public stream calls it (no addresses)
    'to': 'sent to the pons router',
    'selector': 'router execute',
    'swap_shape': 'one swap: ETH in, LABRAT out',
    'pool': 'the LABRAT pool on Uniswap v4',
    'value': 'ETH sent equals the swap amount',
    'amount': 'amount equals the batch',
    'from': 'from the page wallet',
    'calldata_exact': 'nothing else in the calldata',
    'min_out': 'minimum out within 3% of a fresh quote',
    'deadline': 'deadline within 21 minutes',
    'request_fields': 'no extra transaction fields',
    'review': "matches pons's review",
    'simulation': 'simulated on the live chain',
}
HEX_RE = re.compile(r'0x[0-9a-f]{3,}', re.I)          # as relay.py's ADDRESS_LIKE (0X counts too)
ADDR_RE = re.compile(r'0x[0-9a-fA-F]{40}')
BANNED_RE = re.compile(r'\b(dry|test|tests|testing|rehearsal|throwaway|mock|dev)\b', re.I)


def say(*parts):
    brainrig.say(*parts)


def rel(p):
    return brainrig.rel(p)


def utc(ts=None):
    return brainrig.utc(ts)


def sha(b):
    return hashlib.sha256(b).hexdigest()


def parse_amount(s):
    """The batch amount as typed into pons -> (canonical string, wei). ValueError unless 0.00001 <= amount <= 0.01
    ETH with at most 8 decimals (the engine's batches are multiples of 0.00001)."""
    try:
        d = Decimal(str(s).strip())
    except InvalidOperation:
        raise ValueError(f'not an ETH amount: {s!r}') from None
    if not d.is_finite() or d <= 0:
        raise ValueError(f'the amount must be above 0: {s!r}')
    if d.normalize().as_tuple().exponent < -8:
        raise ValueError(f'at most 8 decimals: {s!r}')
    wei = int(d * 10 ** 18)
    if Decimal(wei) != d * 10 ** 18:
        raise ValueError(f'not a whole number of wei: {s!r}')
    if not AMOUNT_MIN_WEI <= wei <= AMOUNT_MAX_WEI:
        raise ValueError(f'the amount must be {buyback.eth_str(AMOUNT_MIN_WEI)}..{buyback.eth_str(AMOUNT_MAX_WEI)} ETH')
    return buyback.eth_str(wei, 18), wei


def scrub(obj):
    """A JSON-safe deep copy with every 40-hex address replaced (session.json and the result carry none)."""
    s = json.dumps(obj, default=str)
    return json.loads(ADDR_RE.sub('0x<address>', s))


# ------------------------------------------------------------------------------------------------------------
# the transaction pons builds: decode, chain facts (read-only), checks
# ------------------------------------------------------------------------------------------------------------
def decode_buy(tx):
    """pons's eth_sendTransaction request -> (fields, data_hex). data_hex is '0x' + exactly the calldata bytes (None
    when they are not hex, or data and input differ). Every layer is decoded for the record, each may fail on a
    tampered request; shape_ok means buyback.router_amounts accepted it (one V4_SWAP of the pinned pool)."""
    tx = dict(tx or {})
    f = {'to': tx.get('to'), 'from': tx.get('from'), 'keys': sorted(tx.keys()), 'value_wei': None, 'selector': None,
         'calldata_bytes': 0, 'extra_fields': [k for k in EXTRA_TX_FIELDS if tx.get(k) is not None],
         'chainId': tx.get('chainId'), 'shape_ok': False, 'canonical': False}
    try:
        f['value_wei'] = ponsbot.as_int(tx.get('value'), 0)
    except (TypeError, ValueError):
        f['decode_error'] = 'value is not a number'
        return f, None
    d_raw, i_raw = tx.get('data'), tx.get('input')
    src = d_raw if d_raw is not None else i_raw
    try:
        raw = ponsbot.hex_bytes(src)
        if d_raw is not None and i_raw is not None and ponsbot.hex_bytes(i_raw) != raw:
            f['decode_error'] = 'the request carries both data and input, and they differ'
            return f, None
    except ValueError as e:
        f['decode_error'] = f'calldata is not hex: {e}'
        return f, None
    data_hex = '0x' + raw.hex()
    f.update(calldata_bytes=len(raw), selector=data_hex[:10], calldata_sha256=sha(raw))
    try:
        commands, inputs, deadline = decode(['bytes', 'bytes[]', 'uint256'], raw[4:])
        f.update(commands='0x' + commands.hex(), inputs=len(inputs), deadline=int(deadline))
        if inputs:
            actions, params = decode(['bytes', 'bytes[]'], inputs[0])
            f.update(actions='0x' + actions.hex(), params=len(params))
            if params:
                (key, zfo, a_in, m_out, extra, hd), = decode([T6], params[0])
                k = (to_checksum_address(key[0]), to_checksum_address(key[1]), int(key[2]), int(key[3]),
                     to_checksum_address(key[4]))
                f.update(pool_key=list(k), pool_id=buyback.pool_id(k), zero_for_one=bool(zfo), amount_in=int(a_in),
                         min_out=int(m_out), extra_uint256=int(extra), hook_data='0x' + bytes(hd).hex())
    except Exception as e:                      # a tampered layer: recorded, and the checks below fail
        f['layer_error'] = f'{type(e).__name__}: {e}'[:200]
    try:
        amount_in, settle, min_out, deadline = buyback.router_amounts(data_hex)
        f.update(shape_ok=True, amount_in=int(amount_in), settle=int(settle), min_out=int(min_out),
                 deadline=int(deadline))
        f['canonical'] = data_hex.lower() == buyback.cd_router_buy(amount_in, min_out, deadline).lower()
    except Exception as e:
        f['shape_error'] = f'{type(e).__name__}: {e}'[:200]
    return f, data_hex


def review_ok(review, amount_wei):
    """pons's Review buy dialog: You send = the typed amount + ' ETH', Market 'Uniswap v4 pool', Max slippage '1%'."""
    if not isinstance(review, dict):
        return False
    send = str(review.get('send') or '')
    m = re.fullmatch(r'\s*([0-9]+(?:\.[0-9]+)?)\s*ETH\s*', send)
    try:
        sent = int(Decimal(m.group(1)) * 10 ** 18) if m else None
    except InvalidOperation:
        sent = None
    return (sent == amount_wei and str(review.get('market') or '').strip() == 'Uniswap v4 pool'
            and str(review.get('slippage') or '').strip() == '1%')


def chain_facts(rpc, f, data_hex, wallet, clock=time.time):
    """Read-only, right at capture (rpc: a buyback.ReadRpc; it refuses every method that is not a read):
    the exact transaction as eth_call with a 1 ETH state override for the page wallet (only when it goes to the pinned
    router), a fresh quoter quote for amountIn, the same buy with min out = the full quote (tokens out >= the quote),
    eth_estimateGas, the latest block."""
    out = {'now': clock(), 'quote': None, 'sim': {'ok': False, 'error': 'not simulated'}, 'sim_floor': None,
           'gas': None, 'block': None}
    to = str(f.get('to') or '')
    value = int(f.get('value_wei') or 0)
    ovr = {wallet: {'balance': hex(ponsbot.DRY_BALANCE_WEI)}}
    try:
        if data_hex and to.lower() == buyback.ROUTER.lower():
            call = {'from': wallet, 'to': buyback.ROUTER, 'data': data_hex, 'value': hex(value)}
            _res, err = rpc.raw('eth_call', [call, 'latest', ovr])
            out['sim'] = {'ok': err is None, 'at': clock(), **({'error': buyback.short_err(err),
                                                               'revert': buyback.revert_name(err)} if err else {})}
        if f.get('shape_ok'):
            w = buyback.words(rpc.ok('eth_call', [{'to': buyback.QUOTER, 'data': buyback.cd_quote(f['amount_in'])},
                                                  'latest']))
            out['quote'] = buyback.w_int(w[0]) if w else 0
            if out['quote'] and data_hex and to.lower() == buyback.ROUTER.lower():
                full = buyback.cd_router_buy(f['amount_in'], out['quote'], f['deadline'])
                _r2, e2 = rpc.raw('eth_call', [{'from': wallet, 'to': buyback.ROUTER, 'data': full,
                                                'value': hex(value)}, 'latest', ovr])
                out['sim_floor'] = {'ok': e2 is None, **({'error': buyback.short_err(e2)} if e2 else {})}
        if out['sim'].get('ok'):
            g, ge = rpc.raw('eth_estimateGas', [{'from': wallet, 'to': buyback.ROUTER, 'data': data_hex,
                                                 'value': hex(value)}, 'latest', ovr])
            out['gas'] = int(g, 16) if g and ge is None else None
        blk = rpc.ok('eth_getBlockByNumber', ['latest', False])
        out['block'] = {'number': int(blk['number'], 16), 'timestamp': int(blk['timestamp'], 16)}
    except buyback.SendRefused:
        raise
    except Exception as e:
        out['error'] = f'{type(e).__name__}: {e}'[:200]
    return out


def buy_checks(f, expect, facts):
    """-> {check: {'ok', 'expected'}} (every key of PUBLIC_CHECKS). expect: amount_wei, wallet, review."""
    lo = lambda a: str(a or '').lower()  # noqa: E731
    shape = bool(f.get('shape_ok'))
    q = int(facts.get('quote') or 0)
    mo = int(f.get('min_out') or 0) if shape else 0
    now = float(facts.get('now') or time.time())
    dl = int(f.get('deadline') or 0) if shape else 0
    try:
        value_ok = shape and buyback.check_value(to_checksum_address(f.get('to')), expect['data_hex'] or '0x',
                                                 f.get('value_wei')) is True
    except Exception:
        value_ok = False
    cid = f.get('chainId')
    try:
        cid_ok = cid is None or ponsbot.as_int(cid) == buyback.CHAIN_ID
    except (TypeError, ValueError):
        cid_ok = False
    key = f.get('pool_key') or [None] * 5
    want = {
        'to': (lo(f.get('to')) == buyback.ROUTER.lower(), f'the pons Universal Router {buyback.ROUTER}'),
        'selector': (f.get('selector') == EXECUTE, f'{EXECUTE} execute(bytes,bytes[],uint256)'),
        'swap_shape': (shape, 'commands 0x10 (V4_SWAP) alone; actions 0x06 0x0c 0x0f; the pinned pool key; '
                              'zeroForOne; settle ETH, take LABRAT'),
        'pool': (shape and f.get('pool_id') == buyback.POOL_ID and key[4] == buyback.HOOK,
                 f'pool id {buyback.POOL_ID}, hook {buyback.HOOK}'),
        'value': (value_ok and f.get('value_wei') == f.get('amount_in') == f.get('settle'),
                  'value == amountIn == SETTLE_ALL'),
        'amount': (shape and f.get('amount_in') == expect['amount_wei'], f"{expect['amount_wei']} wei (the batch)"),
        'from': (lo(f.get('from')) == lo(expect['wallet']), 'the page wallet'),
        'calldata_exact': (shape and bool(f.get('canonical')),
                           'byte-identical to buyback.cd_router_buy(amountIn, minOut, deadline): extra uint256 = 0, '
                           'empty hookData, nothing else'),
        'min_out': (shape and q > 0 and mo > 0 and mo * 100 >= q * MIN_OUT_FLOOR_PCT and mo <= q,
                    f'> 0 and {MIN_OUT_FLOOR_PCT}%..100% of a fresh quote ({q})'),
        'deadline': (shape and now + DEADLINE_MIN_S <= dl <= now + DEADLINE_MAX_S,
                     f'now + {DEADLINE_MIN_S}..{DEADLINE_MAX_S} s'),
        'request_fields': (not f.get('extra_fields') and cid_ok and not f.get('decode_error')
                           and set(f.get('keys') or []) <= {'from', 'to', 'value', 'data', 'input', 'chainId'},
                           'only from, to, value, data'),
        'review': (review_ok(expect.get('review'), expect['amount_wei']),
                   "pons's review: You send <amount> ETH, Market Uniswap v4 pool, Max slippage 1%"),
        'simulation': (bool((facts.get('sim') or {}).get('ok')),
                       'eth_call of the exact transaction succeeds (1 ETH balance override), at capture'),
    }
    return {k: {'ok': bool(ok), 'expected': str(exp)} for k, (ok, exp) in want.items()}


def inspect_buy(tx, wallet, amount_wei, review, rpc, clock=time.time):
    """Decode + read-only chain facts + checks of pons's eth_sendTransaction. Never signs, never sends."""
    f, data_hex = decode_buy(tx)
    facts = chain_facts(rpc, f, data_hex, wallet, clock) if data_hex else {'now': clock(), 'quote': None,
                                                                            'sim': {'ok': False,
                                                                                    'error': 'not decoded'}}
    checks = buy_checks(f, {'amount_wei': amount_wei, 'wallet': wallet, 'review': review, 'data_hex': data_hex},
                        facts)
    failed = [k for k, v in checks.items() if not v['ok']]
    return {'fields': f, 'data_hex': data_hex, 'facts': facts, 'checks': checks, 'failed': failed, 'ok': not failed}


# ------------------------------------------------------------------------------------------------------------
# page scripts
# ------------------------------------------------------------------------------------------------------------
# The stream mask, exactly as tested in live/BUYRIG_RESEARCH.md (appendix).
MASK_JS = r"""
(() => {
  // pons stream mask (research draft for the buy rig). Paints over every 0x string, the wallet's balance lines
  // ("N available"), the creator-fee amounts and pons's error-toast details, with the CSS Custom Highlight API:
  // pons's DOM is not edited (no text changed, no node added inside pons's tree), so React and the buy flow are
  // untouched, and the paint lives in pons's own layers (a modal above a masked line still covers it; a masked line
  // inside a modal or toast is masked). Hit-testing is unaffected (highlights take no pointer events).
  if (window.top !== window) return;
  if (location.origin !== "https://www.ponsfamily.com") return;
  if (window.__ratMask) return;
  const SEP = "\n";
  // full or shortened 0x strings; a shortened one may be split over text nodes ("0x6505", "…", "40dc")
  const HEX = /0x[0-9a-fA-F]{3,}(?:\n?(?:…|\.{2,3})\n?[0-9a-fA-F]{2,})?/g;
  const HEX1 = /0x[0-9a-fA-F]{3}/;
  const ETH = /\d[\d.,]*\s*[kKmMbB]?\s*ETH/g;               // amounts: only inside the creator-fee / holder-fee cards
  const FEE_CARD = /^\s*(creator fees|holder fee sharing)/i;
  // elements masked whole (every text inside): the wallet chip, the fee amounts, pons's toast details
  const WHOLE = 'button[aria-label="Profile"],.token-creator-fees-amount,.toast-description';
  const INLINE = /^(SPAN|A|B|STRONG|EM|I|SMALL|CODE|ABBR|TIME|BDI|BDO|LABEL|SUP|SUB|MARK|S|U)$/;
  const SKIP = /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|TITLE|TEXTAREA|INPUT|SELECT|OPTION)$/;
  const hasHL = !!(window.CSS && CSS.highlights && window.Highlight);
  const hl = hasHL ? new Highlight() : null;
  if (hl) { hl.priority = 1000; CSS.highlights.set("ratmask", hl); }
  const byEl = new Map();          // element -> its mask ranges
  const stats = {updates: 0};
  window.__ratMask = {hasHL, byEl, stats};

  function style() {
    if (document.getElementById("__ratmaskstyle") || !document.documentElement) return;
    const s = document.createElement("style");
    s.id = "__ratmaskstyle";
    s.textContent =
      "::highlight(ratmask){color:transparent;background-color:#3a3a42;text-decoration:none;text-shadow:none}" +
      // belt and braces, in pons's own layers: the holders table (balances) and the toast details never show
      "#pons-v2-panel-holders,.toast-description{visibility:hidden!important}" +
      // pons's own trade toasts ("Trade did not settle" after the simulated refusal) are not shown in the stream: the
      // panel states the outcome. opacity keeps them readable to the rig (TOAST_JS reads textContent)
      ".toast-viewport{opacity:0!important}" +
      // fallback when the Highlight API is missing: the whole element is painted over
      "[data-ratmask]{color:transparent!important;background:#3a3a42!important;border-radius:4px}" +
      "[data-ratmask] *{visibility:hidden!important}";
    document.documentElement.appendChild(s);
  }

  function blockOf(n) {
    let e = n && n.nodeType === 3 ? n.parentElement : n;
    for (let i = 0; e && i < 4 && INLINE.test(e.tagName) && e.parentElement && e.parentElement !== document.body; i++)
      e = e.parentElement;
    return e;
  }

  function target(n) {
    const p = n && n.nodeType === 3 ? n.parentElement : n;
    const w = p && p.closest ? p.closest(WHOLE) : null;
    return w || blockOf(n);
  }

  function okText(t) {
    const p = t.parentElement;
    return !!p && !SKIP.test(p.tagName) && !p.closest("#__ratcur,#__ratcue,#__ratmaskstyle");
  }

  // the block's OWN text nodes (not those of nested blocks), joined with SEP
  function ownTexts(e) {
    const out = [];
    const w = document.createTreeWalker(e, NodeFilter.SHOW_TEXT);
    let t, pos = 0;
    while ((t = w.nextNode())) {
      if (!okText(t) || blockOf(t) !== e || t.parentElement.closest(WHOLE)) continue;
      const v = t.nodeValue || "";
      out.push({t, a: pos, b: pos + v.length});
      pos += v.length + SEP.length;
    }
    return out;
  }

  function point(list, off) {
    for (const x of list) if (off <= x.b) return {t: x.t, o: Math.max(0, Math.min(off - x.a, x.b - x.a))};
    const l = list[list.length - 1];
    return {t: l.t, o: l.b - l.a};
  }

  function feeCard(e) {
    for (let d = e, i = 0; d && d !== document.body && i < 7; d = d.parentElement, i++) {
      const s = d.textContent || "";
      if (s.length < 1500 && FEE_CARD.test(s)) return true;
    }
    return false;
  }

  function rangesFor(e) {
    if (!e || !e.isConnected || !e.matches) return [];
    if (e.matches(WHOLE)) { const r = new Range(); r.selectNodeContents(e); return [r]; }
    const list = ownTexts(e);
    if (!list.length) return [];
    const s = list.map(x => x.t.nodeValue || "").join(SEP);
    const spans = [];
    // a balance line ("1 available", its digits may be a rolling-digit strip): the whole line
    if (/available/i.test(s) && /\d/.test(s)) spans.push([0, s.length]);
    else {
      HEX.lastIndex = 0; let m;
      while ((m = HEX.exec(s))) spans.push([m.index, m.index + m[0].length]);
      if (/\dETH|\d\s*ETH/.test(s) && feeCard(e)) { ETH.lastIndex = 0; while ((m = ETH.exec(s))) spans.push([m.index, m.index + m[0].length]); }
    }
    return spans.map(([a, b]) => {
      const r = new Range();
      const p = point(list, a), q = point(list, b);
      r.setStart(p.t, p.o);
      r.setEnd(q.t, q.o);
      return r;
    });
  }

  function update(e) {
    if (!e) return;
    stats.updates++;
    const old = byEl.get(e);
    if (old) {
      if (hl) old.forEach(r => hl.delete(r)); else e.removeAttribute("data-ratmask");
      byEl.delete(e);
    }
    const rs = rangesFor(e);
    if (!rs.length) return;
    byEl.set(e, rs);
    if (hl) rs.forEach(r => hl.add(r)); else e.setAttribute("data-ratmask", "1");
  }

  function scan(root) {
    if (!root) return;
    if (root.nodeType === 3) return update(target(root));
    if (root.nodeType !== 1 && root.nodeType !== 9 && root.nodeType !== 11) return;
    const seen = new Set();
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let t;
    while ((t = w.nextNode())) {
      if (!okText(t)) continue;
      const e = target(t);
      if (e && !seen.has(e)) { seen.add(e); update(e); }
    }
  }

  function prune() {
    for (const [e, rs] of byEl) if (!e.isConnected) { if (hl) rs.forEach(r => hl.delete(r)); byEl.delete(e); }
  }

  const mo = new MutationObserver(recs => {
    const t0 = performance.now();
    stats.batches = (stats.batches || 0) + 1;
    const done = () => { const dt = performance.now() - t0; stats.ms = (stats.ms || 0) + dt; stats.maxMs = Math.max(stats.maxMs || 0, dt); };
    const touched = new Set();
    for (const r of recs) {
      if (r.type === "characterData") touched.add(target(r.target));
      else { touched.add(target(r.target)); r.addedNodes.forEach(scan); }
    }
    touched.forEach(e => e && update(e));
    prune();
    style();
    done();
  });
  mo.observe(document, {childList: true, subtree: true, characterData: true});
  style();
  if (document.body) scan(document.body);

  // audit, independent of the block logic: every visible text node that itself carries a 0x string, a balance line
  // or a fee amount must intersect a mask range (or sit in a hidden element). -> {unmasked, masked, rects}
  window.__ratMaskAudit = () => {
    const unmasked = [], rects = [], all = [];
    for (const rs of byEl.values()) for (const r of rs) {
      all.push(r);
      for (const q of r.getClientRects()) if (q.width > 0 && q.height > 0 && q.bottom > 0 && q.top < innerHeight)
        rects.push([Math.round(q.x), Math.round(q.y), Math.round(q.width), Math.round(q.height)]);
    }
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let t;
    while ((t = w.nextNode())) {
      if (!okText(t)) continue;
      const v = t.nodeValue || "", p = t.parentElement;
      const sens = HEX1.test(v) || /available/i.test(v) || !!p.closest(".token-creator-fees-amount")
        || (/\d\s*ETH/.test(v) && feeCard(p));
      if (!sens) continue;
      const rr = document.createRange(); rr.selectNodeContents(t);
      const vis = [...rr.getClientRects()].some(q => q.width > 0 && q.bottom > 0 && q.top < innerHeight)
        && getComputedStyle(p).visibility !== "hidden";
      if (!vis) continue;
      if (!all.some(r => r.intersectsNode(t))) unmasked.push({text: v.trim().replace(/0x[0-9a-fA-F]{4,}/g, "0x<hex>").slice(0, 80), tag: p.tagName, cls: String(p.className).slice(0, 60)});
    }
    return {hasHL, unmasked, masked: byEl.size, rects, stats};
  };
})();
"""

# The audit the rig runs before forwarding a frame and around every saved screenshot: the page is pons, the mask
# style is in place, __ratMaskAudit finds nothing unmasked, and (independently, against the highlight Chrome actually
# paints) the sensitive elements in the viewport have every sensitive text node covered or hidden.
AUDIT_JS = r"""() => {
  const out = {origin_ok: location.origin === 'https://www.ponsfamily.com', ready: false};
  const st = document.getElementById('__ratmaskstyle');
  const css = st ? (st.textContent || '') : '';
  out.style_ok = css.indexOf('::highlight(ratmask){color:transparent') >= 0
    && css.indexOf('#pons-v2-panel-holders,.toast-description{visibility:hidden') >= 0;
  if (!out.origin_ok || typeof window.__ratMaskAudit !== 'function' || !document.body) return out;
  let a;
  try { a = window.__ratMaskAudit(); } catch (e) { out.error = String(e).slice(0, 120); return out; }
  out.ready = true;
  out.hasHL = !!a.hasHL;
  out.n_unmasked = a.unmasked.length;
  out.unmasked = a.unmasked.slice(0, 4);
  out.masked = a.masked;
  out.rects = a.rects.length;
  const hl = window.CSS && CSS.highlights ? CSS.highlights.get('ratmask') : null;
  const ranges = hl ? [...hl] : [];
  out.ranges = ranges.length;
  const inView = n => { const r = document.createRange(); r.selectNodeContents(n);
    return [...r.getClientRects()].some(q => q.width > 0 && q.height > 0 && q.bottom > 0 && q.top < innerHeight); };
  const hidden = e => !e || getComputedStyle(e).visibility === 'hidden';
  // [selector, which text nodes must be covered]
  const RULES = [['button[aria-label="Profile"]', /\S/], ['.token-creator-fees-amount', /\S/],
                 ['.toast-description', /\S/], ['.convert-balance', /\d|available/i], ['a[href^="/profile/0x"]', /0x/i]];
  const bad = [];
  for (const [sel, rx] of RULES) for (const el of document.querySelectorAll(sel)) {
    const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let t;
    while ((t = w.nextNode())) {
      const v = t.nodeValue || '';
      if (!rx.test(v) || hidden(t.parentElement) || !inView(t)) continue;
      if (!ranges.some(g => g.intersectsNode(t))) {
        bad.push({sel, text: v.trim().replace(/0x[0-9a-fA-F]{4,}/g, '0x<hex>').slice(0, 40)});
        break;
      }
    }
  }
  const hp = document.getElementById('pons-v2-panel-holders');
  if (hp) { const r = hp.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && !hidden(hp)) bad.push({sel: '#pons-v2-panel-holders', text: 'visible'}); }
  out.n_elements_unmasked = bad.length;
  out.elements_unmasked = bad.slice(0, 4);
  return out;
}"""

# The real clickable box of each buy target (the same contract as brainrig.MEASURE_JS: tags the element with
# data-ratcue for VERIFY_JS / FOCUS_JS; ready = present, visible, enabled and on top at its centre). Terms targets use
# brainrig.MEASURE_JS itself.
BUY_MEASURE_JS = r"""([kind, amount]) => {
  const vis = e => { if (!e) return false; const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && +cs.opacity > 0.05; };
  const txt = e => (e.innerText || e.textContent || '').trim();
  const out = {kind};
  const amt = document.querySelector('input[aria-label="Amount of ETH to spend"]');
  out.buyMode = !!amt && !!document.querySelector('button[aria-label="Pay with ETH"]');
  out.value = amt ? amt.value : null;
  let el = null, box = null, hit = null;
  if (kind === 'amount') {
    // pons (2026-09-25): .convert-amount-field holds input.convert-amount-input.is-hitbox (opacity 0.01, read-only
    // until clicked; a click anywhere on it focuses it). The target is the input's box within the field.
    const field = amt && amt.closest('.convert-amount-field');
    if (!field) return null;
    el = field; hit = amt;
    const fr = field.getBoundingClientRect(), ir = amt.getBoundingClientRect();
    const x0 = Math.max(fr.left, ir.left), y0 = Math.max(fr.top, ir.top);
    const x1 = Math.min(fr.right, ir.right), y1 = Math.min(fr.bottom, ir.bottom);
    box = {x: x0, y: y0, w: Math.max(0, x1 - x0), h: Math.max(0, y1 - y0)};
    out.input_readonly = !!amt.readOnly; out.editing = amt.classList.contains('is-editing');
    out.label = 'ETH amount';
  } else if (kind === 'buy') {
    el = [...document.querySelectorAll('button.ui-btn.ui-btn-primary')].find(b => /^buy labrat$/i.test(txt(b)) && vis(b)) || null;
    if (!el) return null;
    out.busy = el.classList.contains('is-busy');
    const route = document.querySelector('p.token-buy-route');
    out.route = route && vis(route) ? txt(route).slice(0, 90) : null;
  } else if (kind === 'confirm_buy') {
    const dlg = [...document.querySelectorAll('[role=dialog][aria-modal=true]')].find(d => {
      const id = d.getAttribute('aria-labelledby'); const t = id ? document.getElementById(id) : null;
      return !!t && /^\s*review buy\s*$/i.test(t.textContent || ''); });
    if (!dlg) return null;
    el = [...dlg.querySelectorAll('button')].find(b => /^\s*confirm buy\s*$/i.test(b.textContent || '') && vis(b)) || null;
    if (!el) return null;
    const lines = (dlg.innerText || '').split(String.fromCharCode(10)).map(s => s.trim()).filter(Boolean);
    out.dialog = lines.slice(0, 24);
    const after = k => { const i = lines.findIndex(l => l.toLowerCase() === k); return i >= 0 && i + 1 < lines.length ? lines[i + 1] : null; };
    out.review = {send: after('you send'), receive: after('you receive'), market: after('market'), slippage: after('max slippage')};
    const d = dlg.getBoundingClientRect(); out.dialog_box = [d.left, d.top, d.width, d.height];
    out.title = 'Review buy';
  } else return null;
  document.querySelectorAll('[data-ratcue]').forEach(e => { if (e !== el) e.removeAttribute('data-ratcue'); });
  el.setAttribute('data-ratcue', kind);
  if (!box) { const r = el.getBoundingClientRect(); box = {x: r.left, y: r.top, w: r.width, h: r.height}; }
  // rounded corners do not take clicks: keep the target rectangle inside the rounded box (0.29 r per side)
  const radOf = e => { const cs = getComputedStyle(e); return Math.max(...['borderTopLeftRadius', 'borderTopRightRadius',
    'borderBottomLeftRadius', 'borderBottomRightRadius'].map(k => parseFloat(cs[k]) || 0)); };
  const rad = Math.min(Math.max(radOf(el), hit ? radOf(hit) : 0), box.w / 2, box.h / 2);
  const ins = Math.ceil(rad * (1 - Math.SQRT1_2));
  if (ins > 0 && box.w > 2 * ins + 4 && box.h > 2 * ins + 4) {
    box = {x: box.x + ins, y: box.y + ins, w: box.w - 2 * ins, h: box.h - 2 * ins}; out.corner_inset = ins;
  }
  out.x = box.x; out.y = box.y; out.w = box.w; out.h = box.h;
  out.vw = innerWidth; out.vh = innerHeight; out.scrollY = scrollY;
  out.disabled = !!el.disabled || el.getAttribute('aria-disabled') === 'true' || (hit ? !!hit.disabled : false);
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
  const why = !out.visible ? 'not visible' : out.disabled ? 'disabled' : !(box.w > 2 && box.h > 2) ? 'no size'
            : !out.centerInView ? 'out of view' : !out.onTop ? 'covered' : '';
  out.why = why; out.ready = !why;
  return out; }"""

AMOUNT_STATE_JS = r"""() => { const i = document.querySelector('input[aria-label="Amount of ETH to spend"]');
  if (!i) return null;
  return {focused: document.activeElement === i, readonly: !!i.readOnly, value: i.value,
          editing: i.classList.contains('is-editing')}; }"""

QUOTE_STATE_JS = r"""() => { const i = document.querySelector('input[aria-label="Amount of ETH to spend"]');
  const b = [...document.querySelectorAll('button.ui-btn.ui-btn-primary')].find(x => /^buy labrat$/i.test((x.innerText || '').trim()));
  const r = document.querySelector('p.token-buy-route');
  const rv = r ? getComputedStyle(r) : null;
  return {value: i ? i.value : null, buy: !!b, enabled: !!b && !b.disabled && !b.classList.contains('is-busy'),
          route: r && rv.visibility !== 'hidden' && rv.display !== 'none' ? (r.textContent || '').trim().slice(0, 90) : null}; }"""

REVIEW_STATE_JS = r"""() => {
  const dlg = [...document.querySelectorAll('[role=dialog][aria-modal=true]')].find(d => {
    const id = d.getAttribute('aria-labelledby'); const t = id ? document.getElementById(id) : null;
    return !!t && /^\s*review buy\s*$/i.test(t.textContent || ''); });
  if (!dlg) return null;
  const lines = (dlg.innerText || '').split(String.fromCharCode(10)).map(s => s.trim()).filter(Boolean);
  const after = k => { const i = lines.findIndex(l => l.toLowerCase() === k); return i >= 0 && i + 1 < lines.length ? lines[i + 1] : null; };
  const c = [...dlg.querySelectorAll('button')].find(b => /^\s*confirm buy\s*$/i.test(b.textContent || ''));
  return {lines: lines.slice(0, 24), confirm: !!c, send: after('you send'), receive: after('you receive'),
          market: after('market'), slippage: after('max slippage')}; }"""

TOAST_JS = r"""() => { const t = [...document.querySelectorAll('.toast-title')].map(e => (e.textContent || '').trim()).filter(Boolean);
  return t.length ? t[t.length - 1].slice(0, 80) : null; }"""

TOAST_RE = re.compile(r'did not settle|failed|cancel|rejected', re.I)


def audit_clean(a):
    return bool(a and a.get('origin_ok') and a.get('style_ok') and a.get('ready') and a.get('hasHL')
                and a.get('n_unmasked') == 0 and a.get('n_elements_unmasked') == 0)


def audit_breach(a):
    """A real failure (the page is pons, the mask is running, and it found something unmasked), as opposed to a page
    that is not ready to be audited (loading, another origin)."""
    return bool(a and a.get('origin_ok') and a.get('ready')
                and (not a.get('hasHL') or not a.get('style_ok') or a.get('n_unmasked') or a.get('n_elements_unmasked')))


def slim_audit(a):
    if not a:
        return None
    return {k: a.get(k) for k in ('origin_ok', 'style_ok', 'ready', 'hasHL', 'n_unmasked', 'n_elements_unmasked',
                                  'masked', 'ranges', 'unmasked', 'elements_unmasked')}


# ------------------------------------------------------------------------------------------------------------
# the relay link (channel "pons")
# ------------------------------------------------------------------------------------------------------------
def channel_url(url, channel='pons'):
    if re.search(r'[?&]channel=', url):
        return url
    return url + ('&' if '?' in url else '?') + f'channel={channel}'


def public_json(msg):
    """The JSON text of a message for the public stream (ASCII, as the relay checks it), or ValueError when it holds
    a 0x string, internal wording, or is over PUBLIC_TEXT_MAX (the rig never builds such a message; this is the
    tripwire)."""
    s = json.dumps(msg, separators=(',', ':'), ensure_ascii=True, default=str)
    if HEX_RE.search(s) or HEX_RE.search(json.dumps(msg, ensure_ascii=False, default=str)):
        raise ValueError('a 0x string in a public message')
    m = BANNED_RE.search(json.dumps(msg, ensure_ascii=False, default=str))
    if m:
        raise ValueError(f'internal wording in a public message: {m.group(0)!r}')
    if len(s) > PUBLIC_TEXT_MAX:
        raise ValueError(f'a public message of {len(s)} bytes (the relay takes {PUBLIC_TEXT_MAX})')
    return s


class PonsLink:
    """The relay connection for the pons channel, on its own thread (the rig never waits on the network). Text is kept
    (at most MAX_TEXTS queued); frames are bounded (the oldest is dropped). On every (re)connect the hello, the latest
    state of each step and the tx summary are sent first, so a relay restart mid-session resyncs."""

    MAX_FRAMES = 3
    MAX_TEXTS = 300

    def __init__(self, url, token, log=say, connect=None):
        self.url = channel_url(url)
        self.token = token
        self.log = log
        self._connect = connect
        self.cv = threading.Condition()
        self.q = collections.deque()
        self.nframes = 0
        self.hello_msg = None
        self.steps = collections.OrderedDict()
        self.tx_msg = None
        self.need_sync = False
        self.stopping = False
        self.connected = False
        self.fatal = None
        self.stats = collections.Counter()
        self.thread = threading.Thread(target=self._run, name='pons-link', daemon=True)

    # ---- producer side (the rig's event loop)
    def start(self):
        self.thread.start()
        return self

    def _text(self, msg, keep=None):
        try:
            s = public_json(msg)
        except ValueError as e:
            self.stats['texts_refused'] += 1
            self.log(f'not streamed ({e})')
            return False
        with self.cv:
            if keep == 'hello':
                self.hello_msg = s
            elif keep == 'tx':
                self.tx_msg = s
            elif keep is not None:
                self.steps[keep] = s
            if len(self.q) - self.nframes >= self.MAX_TEXTS:
                for i, (k, _p) in enumerate(self.q):
                    if k == 't':
                        del self.q[i]
                        break
                self.stats['texts_dropped'] += 1
            self.q.append(('t', s))
            self.cv.notify()
        return True

    def hello(self, msg):
        return self._text(msg, 'hello')

    def step(self, msg):
        return self._text(msg, msg.get('key'))

    def tx(self, msg):
        return self._text(msg, 'tx')

    def done(self, msg):
        return self._text(msg, 'tx')        # the final summary replaces the tx one in a resync

    def bye(self, msg):
        return self._text(msg)

    def frame(self, jpg):
        if len(jpg) + len(FRAME_PREFIX) > RELAY_MAX_FRAME:
            self.stats['frames_too_big'] += 1
            return False
        with self.cv:
            if self.nframes >= self.MAX_FRAMES:
                for i, (k, _p) in enumerate(self.q):
                    if k == 'b':
                        del self.q[i]
                        break
                self.nframes -= 1
                self.stats['frames_dropped'] += 1
            self.q.append(('b', FRAME_PREFIX + bytes(jpg)))
            self.nframes += 1
            self.cv.notify()
        return True

    def close(self, flush_s=4.0):
        deadline = time.monotonic() + flush_s
        with self.cv:
            while self.q and self.connected and time.monotonic() < deadline:
                self.cv.wait(0.05)
            self.stopping = True
            self.cv.notify_all()
        if self.thread.is_alive():
            self.thread.join(timeout=6)

    # ---- sender side (its own thread)
    def _open(self):
        if self._connect is not None:
            return self._connect(self.url, self.token)
        from websockets.sync.client import connect
        from websockets.exceptions import InvalidStatus
        try:
            return connect(self.url, additional_headers={'Authorization': f'Bearer {self.token}'}, open_timeout=10,
                           close_timeout=3, compression=None, max_size=2 ** 20, ping_interval=20, ping_timeout=20,
                           user_agent_header='labrat-buyrig/1')
        except InvalidStatus as e:
            code = e.response.status_code
            if code in (401, 403):
                raise PermissionError(f'the relay refused the publish token (HTTP {code})') from None
            raise ConnectionError(f'HTTP {code}') from None

    def _snapshot(self):
        items = [('t', self.hello_msg)] if self.hello_msg else []
        items += [('t', s) for s in self.steps.values()]
        if self.tx_msg:
            items.append(('t', self.tx_msg))
        return items

    def _run(self):
        backoff = 1.0
        first = True
        while True:
            with self.cv:
                if self.stopping and not self.q:
                    return
            try:
                conn = self._open()
            except PermissionError as e:
                self.fatal = str(e)
                self.log(f'relay: {self.fatal}; the session goes on unstreamed')
                with self.cv:
                    self.q.clear()
                    self.nframes = 0
                    self.cv.notify_all()
                return
            except Exception as e:
                self.stats['connect_errors'] += 1
                if self.stats['connect_errors'] <= 3:
                    self.log(f'relay: cannot connect ({type(e).__name__}: {str(e)[:120]}); retrying')
                with self.cv:
                    if self.stopping:
                        return
                    self.cv.wait(backoff)
                backoff = min(backoff * 2, 15.0)
                continue
            with self.cv:
                self.connected = True
                resync = not first
                first = False
                if resync:
                    self.q = collections.deque(i for i in self.q if i[0] == 'b')
                    self.nframes = len(self.q)
                    for it in reversed(self._snapshot()):
                        self.q.appendleft(it)
            self.stats['connects'] += 1
            backoff = 1.0
            try:
                while True:
                    with self.cv:
                        while not self.q and not self.stopping:
                            self.cv.wait(0.25)
                        if not self.q and self.stopping:
                            break
                        kind, payload = self.q.popleft()
                        if kind == 'b':
                            self.nframes -= 1
                    conn.send(payload)
                    self.stats['frames_sent' if kind == 'b' else 'texts_sent'] += 1
                    self.stats['bytes_sent'] += len(payload)
                    with self.cv:
                        self.cv.notify_all()
                self._shut(conn)
                with self.cv:
                    self.connected = False
                    self.cv.notify_all()
                return
            except Exception as e:
                self.stats['disconnects'] += 1
                self.log(f'relay: connection lost ({type(e).__name__}); reconnecting')
                self._shut(conn)
                with self.cv:
                    self.connected = False
                    self.cv.notify_all()
                    if self.stopping:
                        return
                    self.cv.wait(backoff)
                backoff = min(backoff * 2, 15.0)

    @staticmethod
    def _shut(conn):
        try:
            conn.close()
        except Exception:
            pass


# ------------------------------------------------------------------------------------------------------------
# pons
# ------------------------------------------------------------------------------------------------------------
class BuyBot(ponsbot.PonsBot):
    """ponsbot.PonsBot on the $LABRAT coin page. The provider, the DRY balance override, the frame / origin guards and
    the read-only RPC forwarding are ponsbot's own; this subclass only opens the coin page and refuses EVERY signing
    request (the buy flow never needs one)."""

    SEND_METHODS = frozenset(('eth_sendTransaction', 'eth_sendRawTransaction', 'eth_sendRawTransactionSync',
                              'eth_sendBundle', 'eth_sendPrivateTransaction'))

    def __init__(self, acct, log=None, headful=False):
        super().__init__(acct, 'DRY', log=log, headful=headful, size=(VIEW_W, VIEW_H), theme='dark',
                         page_cursor=False, symbol=SYMBOL)
        self.rpc_sends_blocked = 0

    async def _route_rpc(self, route):
        """pons's own chain requests (its RPC proxy): a send of any kind never leaves the browser (nothing here signs,
        so none is expected); everything else is ponsbot's DRY handling (the 1 ETH balance override)."""
        try:
            req = route.request
            body = json.loads(req.post_data or 'null') if req.method == 'POST' else None
        except Exception:
            body = None
        calls = body if isinstance(body, list) else [body]
        if any(isinstance(c, dict) and c.get('method') in self.SEND_METHODS for c in calls):
            self.rpc_sends_blocked += 1
            self._refused('rpc send', 'the buy rig never sends a transaction')
            return await route.abort()
        return await super()._route_rpc(route)

    async def goto(self):
        await self.page.goto(COIN_URL, wait_until='domcontentloaded', timeout=60000)

    async def settle(self):
        """Wait for the trade panel and the connected wallet; dismiss pons's status strip; set the theme."""
        await self.page.locator('input[aria-label="Amount of ETH to spend"]').first.wait_for(state='attached',
                                                                                          timeout=45000)
        await self.page.wait_for_timeout(1200)
        await self.dismiss_notice()
        theme = await self.set_theme()
        head, tail = self.address[:6].lower(), self.address[-4:].lower()
        connected = False
        for _ in range(60):
            txt = (await self.page.evaluate('() => document.body.innerText')).lower()
            if head in txt and tail in txt:
                connected = True
                break
            await asyncio.sleep(0.3)
        st = await self.page.evaluate(
            '() => ({addr: window.ethereum && window.ethereum.selectedAddress, '
            'rig: !!(window.ethereum && window.ethereum.isRatbrainRig), '
            "chip: !!document.querySelector('button[aria-label=\"Profile\"]')})")
        return {'connected': connected and str(st.get('addr')).lower() == self.addr_l, 'page_wallet': st,
                'theme': theme}

    async def _eth_sign(self, method, params):
        self.signatures.append({'method': str(method), 'refused': True, 'at': time.time()})
        self._refused(str(method), 'the buy rig signs nothing')
        return ponsbot.refusal(REFUSAL_MSG)

    async def _eth_sign_typed(self, method, params):
        self.signatures.append({'method': str(method), 'refused': True, 'at': time.time()})
        self._refused(str(method), 'the buy rig signs nothing')
        return ponsbot.refusal(REFUSAL_MSG)


# ------------------------------------------------------------------------------------------------------------
# one buy session
# ------------------------------------------------------------------------------------------------------------
class BuyRun(brainrig.Run):
    """brainrig.Run (the rat's session, lighting, click forwarding, misses, the screencast, recording) on the coin page,
    with the buy targets. Always DRY."""

    def __init__(self, amount, seed, link=None, batch=None, rpc=None, out_root=None):
        super().__init__(None, seed)
        self.mode, self.live_mode = 'DRY', False            # whatever brainrig.CONFIG says: this rig is DRY only
        self.amount_str, self.amount_wei = parse_amount(amount)
        self.batch = dict(batch or {})
        self.link = link
        self.rpc = rpc or buyback.ReadRpc()
        root = Path(out_root) if out_root else ROOT / 'runs'
        self.run_dir = root / (f'buyrig_{self.stamp}_seed{self.seed}' + ('_DEVORACLE' if self.oracle else ''))
        self.targets = [Target(i + 1, *t) for i, t in enumerate(BUY_TARGETS)]
        self.review = None
        self.typed_ok = False
        self.inspection = None
        self.checkpoints = []
        self.n_ckpt = 0
        self.audits = collections.Counter()
        self.breach = None
        self._prev_clean = False
        self._audit_last = 0.0
        self.saved = False
        self.proof = None
        self.result = None
        self.terms_shown = None
        self.fail_reason = None          # a REASONS keyword for the public result, when the session ends without a buy
        self._last_miss_pub = 0.0

    # ---- events (the public stream: fixed keywords, short names and numbers only) --------------------------------------
    def _shown(self):
        """The targets this session shows: the terms targets drop out once pons showed no terms gate."""
        return [t for t in self.targets if self.terms_shown is not False or t.kind not in ('terms', 'accept')]

    def _pub_step(self, tg, phase=''):
        if self.link is None:
            return
        shown = self._shown()
        if tg not in shown:
            return
        msg = {'type': MSG['step'], 'session': self.stamp, 'i': shown.index(tg) + 1, 'n': len(shown), 'key': tg.key,
               'target': PUBLIC_NAME[tg.key], 'state': tg.state, 'phase': phase if phase in PHASES else '',
               'simulated': True}
        if tg.state == 'active' and tg.lights and phase in ('light', 'aim'):
            msg['box'] = tg.lights[-1]['box']
        self.link.step(msg)

    def stage(self, tg, state, detail=''):
        super().stage(tg, state, detail)
        phase = {'hit': 'press', 'done': 'done'}.get(state, '')
        if state == 'done' and tg.kind == 'confirm_buy' and self.inspection is None:
            phase = 'check'                  # pons asked for the transaction; the rig is checking it
        if state == 'active' and str(detail).startswith('lit '):
            phase = 'light'
            asyncio.ensure_future(self._aim_later(tg, len(tg.lights), AIM_AFTER_S))
        self._pub_step(tg, phase)

    async def _aim_later(self, tg, n_lights, delay):
        await asyncio.sleep(delay)
        if self.lit is tg and tg.state == 'active' and len(tg.lights) == n_lights:
            self._pub_step(tg, 'aim')

    def _report_click(self, step, x, y, env_hit, forwarded, target, why=None):
        rec = super()._report_click(step, x, y, env_hit, forwarded, target, why)
        tg = self.cur
        if (not forwarded and tg is not None and tg.state == 'active'
                and time.time() - self._last_miss_pub >= MISS_PUBLISH_GAP_S):
            self._last_miss_pub = time.time()
            self._pub_step(tg, 'miss')
            asyncio.ensure_future(self._aim_later(tg, len(tg.lights), 1.2))
        return rec

    # ---- measuring ------------------------------------------------------------------------------------------------
    async def _measure(self, tg):
        if tg.kind in ('terms', 'accept'):
            return await self._eval(brainrig.MEASURE_JS, [tg.kind, tg.arg, SYMBOL, None])
        return await self._eval(BUY_MEASURE_JS, [tg.kind, self.amount_str])

    def _ready(self, tg, m):
        ok, why = super()._ready(tg, m)
        if not ok or tg.kind in ('terms', 'accept'):
            return ok, why
        if tg.kind in ('amount', 'buy') and not m.get('buyMode'):
            raise StageFailed(tg.key, "pons's trade panel is not in buy mode (the rig never touches the buy/sell "
                                      'toggle)')
        if tg.kind == 'buy':
            if m.get('busy'):
                return False, 'pons is busy'
            if m.get('value') != self.amount_str or not self.typed_ok:
                return False, 'the amount field does not hold the batch amount'
            if not m.get('route'):
                return False, "waiting for pons's quote"
        if tg.kind == 'confirm_buy':
            rv = m.get('review') or {}
            if not (rv.get('send') and rv.get('market') and rv.get('slippage')):
                return False, "pons's review is still filling in"
            if not review_ok(rv, self.amount_wei):
                raise StageFailed(tg.key, f"pons's review does not match the batch: you send {rv.get('send')!r}, "
                                          f"market {rv.get('market')!r}, max slippage {rv.get('slippage')!r}")
            self.review = rv
        return True, ''

    # ---- consequences -----------------------------------------------------------------------------------------------
    async def _consequence(self, tg, px, py):
        k = tg.kind
        if k in ('terms', 'accept'):
            return await super()._consequence(tg, px, py)
        page = self.bot.page
        if k == 'amount':
            await self._click(px, py)
            await asyncio.sleep(0.2)
            st = await self._eval(AMOUNT_STATE_JS)
            if not st or not st.get('focused') or st.get('readonly'):
                return False, 'the click did not open the amount field for typing'
            self._pub_step(tg, 'type')
            t0 = time.time()
            shots0 = self.out.n['shot']
            val = None
            for attempt in range(2):          # React can drop a keystroke under load: select all and type it again
                await page.keyboard.press('Control+A')
                await page.keyboard.press('Delete')
                await page.keyboard.type(self.amount_str, delay=brainrig.TYPE_DELAY_MS)
                await asyncio.sleep(0.35)
                val = ((await self._eval(AMOUNT_STATE_JS)) or {}).get('value')
                if val == self.amount_str:
                    break
                self.log(f'the amount field read {val!r} after typing; typing it once more')
            dt = time.time() - t0
            n_sh = self.out.n['shot'] - shots0
            self.typing['amount'] = {'chars': len(self.amount_str), 'seconds': round(dt, 2),
                                     'frames_streamed': n_sh, 'ok': val == self.amount_str}
            tg.result = {'typed': self.amount_str, 'reads': val, **self.typing['amount']}
            if val != self.amount_str:
                return False, f'the field reads {val!r}'
            self.typed_ok = True
            self.log(f'typed {self.amount_str} ETH live ({len(self.amount_str)} chars in {dt:.1f} s); waiting for '
                     "pons's quote (the rat holds)")
            self._pub_step(tg, 'quote')
            q = await self._wait_quote(QUOTE_WAIT_S)
            if not q:
                self.typed_ok = False
                self.fail_reason = 'quote_failed'
                return False, f'pons did not quote {self.amount_str} ETH within {QUOTE_WAIT_S:g} s'
            self.fail_reason = None
            tg.result['route'] = q.get('route')
            self.log(f"pons quoted it: {q.get('route')}")
            await self._checkpoint('amount')
            return True, f'typed {self.amount_str} ETH · pons quoted it'
        if k == 'buy':
            await self._click(px, py)
            rv = await self._wait_review(REVIEW_WAIT_S)
            if not rv:
                return False, "pons did not open its review"
            tg.result = {'review': rv}
            self.log("pons's review: " + ' · '.join(rv.get('lines') or []))
            self._pub_step(tg, 'review')
            await asyncio.sleep(0.5)                      # its open animation (~0.4 s)
            await self._checkpoint('review')
            return True, (f"pons's review: you send {rv.get('send')} · you receive {rv.get('receive')} · "
                          f"{rv.get('market')} · max slippage {rv.get('slippage')}")
        if k == 'confirm_buy':
            self.bot.arm()
            await self._click(px, py)
            self.log(f"the rat clicked pons's Confirm buy at ({px:.0f}, {py:.0f}), which makes pons request the buy "
                     'transaction')
            try:
                await asyncio.wait_for(self.send_entered.wait(), brainrig.SEND_WAIT_S)
            except asyncio.TimeoutError:
                return False, 'pons did not call eth_sendTransaction'
            return True, 'pons requested the buy transaction'
        raise StageFailed(tg.key, f'unknown target kind {k}')

    async def _wait_quote(self, timeout):
        t_end = time.time() + timeout
        while time.time() < t_end:
            self._check_end()
            q = await self._eval(QUOTE_STATE_JS)
            if q and q.get('value') == self.amount_str and q.get('enabled') and q.get('route'):
                return q
            await asyncio.sleep(0.25)
        return None

    async def _wait_review(self, timeout):
        t_end = time.time() + timeout
        while time.time() < t_end:
            self._check_end()
            rv = await self._eval(REVIEW_STATE_JS)
            if rv and rv.get('confirm') and rv.get('send') and rv.get('market') and rv.get('slippage'):
                return rv
            await asyncio.sleep(0.2)
        return None

    async def _reveal_panel(self):
        """Rig setup (the rat holds): scroll the trade panel into view, the amount field at ~35 % of the viewport, so
        the field, the quote and Buy LABRAT are on screen together (pons's layout at 1280x900: scrollY ~518)."""
        m = await self._eval(BUY_MEASURE_JS, ['amount', self.amount_str])
        if not m:
            return
        if not 0.2 * m['vh'] <= m['y'] <= 0.5 * m['vh']:
            self.log('scrolling to the trade panel (the rat holds)')
            await self.bot.smooth_scroll(m['scrollY'] + m['y'] - m['vh'] * 0.35, ms=850, steps=26)
            await asyncio.sleep(0.4)

    # ---- the mask audit, the stream and the saved frames -------------------------------------------------------------
    async def _audit(self):
        self.audits['runs'] += 1
        return await self._eval(AUDIT_JS)

    def _on_breach(self, a, where):
        self.audits['breaches'] += 1
        if self.breach is None:
            self.breach = {'where': where, 'at': time.time(), 'audit': slim_audit(a)}
            self.log(f'MASK AUDIT FAILED ({where}): frames are withheld and the session stops: '
                     f'{json.dumps(slim_audit(a))[:300]}')
        if not self.stop_fut.done():
            self.stop_fut.set_result('the mask audit found unmasked text on the pons page')

    async def _frame(self, p):
        b64 = p.get('data')
        now = time.perf_counter()
        if self.streaming and b64 and self.breach is None and now - self._audit_last >= 1.0 / RELAY_FPS:
            self._audit_last = now
            a = await self._audit()
            clean = audit_clean(a)
            prev, self._prev_clean = self._prev_clean, clean
            if not clean:
                self.audits['frames_withheld'] += 1
                if audit_breach(a):
                    self._on_breach(a, 'stream frame')
            elif prev and self.link is not None:
                jpg = base64.b64decode(b64)
                if len(jpg) + len(FRAME_PREFIX) > RELAY_MAX_FRAME:
                    jpg = await asyncio.to_thread(shrink_jpeg, jpg)
                if jpg and self.link.frame(jpg):
                    self.audits['frames_forwarded'] += 1
                else:
                    self.audits['frames_too_big'] += 1
            elif prev:
                self.audits['frames_clean'] += 1
        await super()._frame(p)

    async def _checkpoint(self, name):
        """A masked screenshot for the record: taken only between two clean audits. -> jpeg bytes or None."""
        if not self.bot or not self.bot.page:
            return None
        a1 = await self._audit()
        rec = {'name': name, 'at': utc(), 'audit_before': slim_audit(a1)}
        jpg = None
        if audit_clean(a1):
            try:
                jpg = await self.bot.page.screenshot(type='jpeg', quality=CHECKPOINT_QUALITY)
            except Exception as e:
                rec['error'] = f'{type(e).__name__}'
            a2 = await self._audit()
            rec['audit_after'] = slim_audit(a2)
            if jpg is not None and audit_clean(a2):
                self.n_ckpt += 1
                d = self.run_dir / 'frames'
                d.mkdir(exist_ok=True)
                path = d / f'{self.n_ckpt:02d}_{name}.jpg'
                path.write_bytes(jpg)
                rec.update(file=rel(path), sha256=sha(jpg), bytes=len(jpg), saved=True)
            else:
                if audit_breach(a2):
                    self._on_breach(a2, f'checkpoint {name}')
                jpg = None
        elif audit_breach(a1):
            self._on_breach(a1, f'checkpoint {name}')
        rec.setdefault('saved', False)
        self.checkpoints.append(rec)
        return jpg

    # ---- the run ----------------------------------------------------------------------------------------------------
    async def run(self):
        try:
            return await super().run()
        finally:
            if self.link is not None:
                try:
                    self.link.done(self._done_msg())
                    self.link.bye({'type': MSG['bye'], 'session': self.stamp, 'label': LABEL})
                except Exception as e:
                    self.log(f'relay done message failed: {type(e).__name__}')
                await asyncio.to_thread(self.link.close, 5.0)

    async def _run(self):
        B = brainrig.BRAIN
        self.log(f"buy rig · {LABEL} · the rat buys {self.amount_str} ETH of ${SYMBOL} on pons · seed {self.seed}"
                 + (f" · batch {self.batch.get('at')}" if self.batch.get('at') else '')
                 + f" · steering network sha256 {B['steer_sha256'][:16]} · lever-press network sha256 "
                 f"{B['press_sha256'][:16]}")
        self.log('pons builds the buy transaction; the rig decodes it, checks it, simulates it with eth_call and refuses '
                 'to sign. Nothing is signed or sent.')
        if self.oracle:
            self.log("DEV --dev-oracle: a SCRIPTED cursor drives the targets, NOT the rat's brain; never streamed")
        steer_path, press_path = self.run_dir / 'steer.pt', self.run_dir / 'press.pt'
        for p, b in ((steer_path, B['steer_bytes']), (press_path, B['press_bytes'])):
            p.write_bytes(b)
            os.chmod(p, stat.S_IREAD)
        if self.oracle:
            self.sess = brainrig.Oracle(self.seed)
            self.commit = B['commit']
        else:
            self.sess = await asyncio.to_thread(brain_session.Session, str(steer_path), self.seed, brainrig.PREROLL_S,
                                                (0.5, 0.5), str(press_path))
            self.commit = self.sess.commit
        self.out.json({'type': 'commit', 'commit': self.commit, 'steer_sha256': B['steer_sha256'],
                       'press_sha256': B['press_sha256'], 'units': brainrig.UNITS,
                       'connections': brainrig.CONNECTIONS, 'dev_oracle': self.oracle})
        if self.link is not None:
            self.link.start()
            self.link.hello(self._hello_msg())

        # ---- pons
        acct = ponsbot.throwaway_account()
        self.bot = BuyBot(acct, log=self.log, headful=brainrig.CONFIG.get('headful', False))
        self.bot.on_send = self.on_send
        await self.bot.start()
        await self.bot.page.add_init_script(MASK_JS)
        await self.bot.page.add_init_script(brainrig.OVERLAY_JS.replace('__DEV__', 'true' if self.oracle else 'false'))
        self.log(f'opening {COIN_URL} ({VIEW_W}x{VIEW_H}) with an injected wallet: a fresh in-memory key (DRY); the '
                 'stream mask is installed before the page loads')
        try:
            await self.bot.goto()
        except Exception as e:
            raise RunEnded(f'pons did not load: {e}')
        await self.start_screencast()
        try:
            st = await self.bot.settle()
        except Exception as e:
            raise RunEnded(f'the trade panel did not appear: {e}')
        if not st['connected']:
            raise RunEnded('pons did not show the injected wallet as connected')
        a = await self._audit()
        if not audit_clean(a):
            if audit_breach(a):
                self._on_breach(a, 'page load')
            raise RunEnded(f'the stream mask is not clean on the loaded page: {json.dumps(slim_audit(a))[:300]}')
        await self._eval('() => { window.__ratDowns = []; return true; }')
        self.log(f"wallet connected in pons (masked on the page) · rig setup (not the rat): pons theme {st['theme']}, "
                 "pons's status strip dismissed · mask audit clean")
        self.log('pons reads the wallet balance through its own RPC proxy; the rig answers a simulated 1 ETH for this '
                 'wallet so pons enables Buy. The wallet holds nothing; the rig never signs.')

        # ---- the brain
        self.sim_thread = threading.Thread(target=self._sim, name='brain-session', daemon=True)
        self.sim_thread.start()
        self.mouse_on = True
        self.mouse_task = asyncio.create_task(self._mouse_loop())
        await asyncio.wait({asyncio.ensure_future(self.brain_on.wait()), self.over, self.stop_fut},
                           timeout=60, return_when=asyncio.FIRST_COMPLETED)
        self._check_end()
        if not self.brain_on.is_set():
            raise RunEnded('the brain session did not start within 60 s')

        # ---- the buy targets: the rat clicks each one
        self.terms_shown = await self._wait_terms()
        if not self.terms_shown and self.link is not None:
            self.link.hello(self._hello_msg())      # the same session, without the terms targets
        for tg in self.targets:
            if tg.kind in ('terms', 'accept') and not self.terms_shown:
                self.stage(tg, 'done', 'pons showed no terms dialog')
                continue
            if tg.kind == 'amount':
                if self.terms_shown:
                    await self._checkpoint('page')
                await self._reveal_panel()
            await self._target(tg)

        # ---- the buy request
        last = self.targets[-1]
        try:
            await asyncio.wait_for(self.send_entered.wait(), brainrig.SEND_WAIT_S)
        except asyncio.TimeoutError:
            raise StageFailed(last.key, f'pons never called eth_sendTransaction within {brainrig.SEND_WAIT_S} s')
        try:
            await asyncio.wait_for(self.tx_seen.wait(), brainrig.DRY_HANDLE_WAIT_S)
        except asyncio.TimeoutError:
            raise StageFailed(last.key, f'the wallet hook did not finish within {brainrig.DRY_HANDLE_WAIT_S} s')
        if self.rejection_task:
            await self.rejection_task
        return self._outcome()

    # ---- the wallet: pons's eth_sendTransaction ----------------------------------------------------------------------
    async def on_send(self, tx):
        armed = bool(self.bot.armed)
        self.sends.append({'tx': tx, 'at': time.time(), 'armed': armed})
        n = len(self.sends)
        data = str(tx.get('data') or tx.get('input') or '')
        self.log(f"pons called eth_sendTransaction #{n}: value {tx.get('value')}, calldata "
                 f'{max(0, len(data) - 2) // 2} bytes')
        if not armed or self.handled_send is not None:
            why = "the rat has not clicked Confirm buy" if not armed else 'one buy request per session'
            self.log(f'refused ({why})')
            return ponsbot.refusal(REFUSAL_MSG)
        self.handled_send = n - 1
        self.send_entered.set()
        try:
            return await self._handle_send(tx)
        except Exception as e:                 # whatever went wrong, the answer to pons is the same refusal
            self.log(f'the buy request could not be handled ({type(e).__name__}); refused')
            return ponsbot.refusal(REFUSAL_MSG)
        finally:
            self.tx_seen.set()

    async def _handle_send(self, tx):
        try:
            res = await asyncio.wait_for(self.loop.run_in_executor(
                None, inspect_buy, tx, self.bot.address, self.amount_wei, self.review, self.rpc), 30)
        except Exception as e:                 # buyback.SendRefused included (a bug: this rig only reads)
            if isinstance(e, buyback.SendRefused):
                self.log(f'BUG: a send method reached the read-only RPC ({e}); refused, nothing was sent')
            f, _ = decode_buy(tx)
            res = {'fields': f, 'data_hex': None, 'facts': {'error': f'{type(e).__name__}: {e}'[:200]},
                   'checks': {'inspection': {'ok': False, 'expected': 'the checks ran'}}, 'failed': ['inspection'],
                   'ok': False}
        self.inspection = res
        f, facts, checks, failed = res['fields'], res['facts'], res['checks'], res['failed']
        verdict = 'dry_captured' if res['ok'] else 'dry_refused_mismatch'
        resp = ponsbot.refusal(REFUSAL_MSG)
        self.capture = {'raw_request': tx, 'fields': f, 'facts': facts, 'checks': checks, 'failed': failed,
                        'expected': {'amount_wei': self.amount_wei, 'amount_eth': self.amount_str,
                                     'wallet': self.bot.address, 'review': self.review},
                        'verdict': verdict, 'mode': 'DRY', 'wallet': self.bot.address, 'at': utc(),
                        'at_unix': time.time(), 'response_to_page': resp, 'signed': False, 'broadcast': False,
                        'dev_oracle': self.oracle}
        n = len(checks)
        self.out.json({'type': 'tx', 'mode': 'DRY', 'verdict': verdict, 'checks': checks, 'failed': failed,
                       'quote': facts.get('quote'), 'min_out': f.get('min_out'), 'simulation': facts.get('sim')})
        q = facts.get('quote')
        self.log(f"decoded: execute {f.get('commands')} {f.get('actions')} · amountIn {f.get('amount_in')} · minOut "
                 f"{f.get('min_out')} · deadline {f.get('deadline')} · value {f.get('value_wei')} · fresh quote {q}")
        sim = facts.get('sim') or {}
        self.log('eth_call of the exact transaction (1 ETH balance override): '
                 + ('ok' if sim.get('ok') else f"failed: {sim.get('error')}")
                 + (f" · gas {facts.get('gas')}" if facts.get('gas') else '')
                 + (f" · tokens out >= the quote: {(facts.get('sim_floor') or {}).get('ok')}"
                    if facts.get('sim_floor') else ''))
        self.log(f'{n - len(failed)}/{n} checks passed · {verdict}' + (f' · FAILED {failed}' if failed else ''))
        self.log(f'refused with {ponsbot.USER_REJECTED} "{REFUSAL_MSG}". Nothing signed, nothing sent.')
        if self.link is not None:
            self.link.tx(self._tx_msg())
        self.rejection_task = asyncio.ensure_future(self._watch_rejection())
        return resp

    async def _watch_rejection(self):
        t_end = time.time() + TOAST_WAIT_S
        hit = None
        while time.time() < t_end:
            t = await self._eval(TOAST_JS)
            if t and TOAST_RE.search(t):
                hit = t
                break
            await asyncio.sleep(0.15)
        self.rejection = hit
        await asyncio.sleep(0.6)
        jpg = await self._checkpoint('after')
        if jpg:
            self.final_jpg = jpg
        self.log(f'pons shows: {hit}' if hit else f'pons showed no toast within {TOAST_WAIT_S:g} s')

    # ---- outcome, messages, recording -----------------------------------------------------------------------------------
    def _numbers(self):
        res = self.inspection or {}
        f, facts = res.get('fields') or {}, res.get('facts') or {}
        q = facts.get('quote')
        checks = res.get('checks') or {}
        return {'quote': q, 'labrat_out': buyback.token_str(q) if q else None, 'min_out_wei': f.get('min_out'),
                'min_out': buyback.token_str(f['min_out']) if f.get('min_out') else None,
                'checks_passed': sum(1 for v in checks.values() if v.get('ok')), 'checks_total': len(checks),
                'failed': list(res.get('failed') or []),
                'simulation': ('ok' if (facts.get('sim') or {}).get('ok') else
                               'reverted' if (facts.get('sim') or {}).get('error') else None),
                'tokens_out_at_least_quote': (facts.get('sim_floor') or {}).get('ok'),
                'gas': facts.get('gas'), 'block': (facts.get('block') or {}).get('number')}

    def _outcome(self):
        cap = self.capture or {}
        num = self._numbers()
        out = {'mode': cap.get('verdict'), 'amount_eth': self.amount_str, 'amount_wei': self.amount_wei,
               'labrat_out': num['labrat_out'], 'quote_wei': num['quote'], 'min_out': num['min_out'],
               'min_out_wei': num['min_out_wei'], 'checks_passed': not num['failed'], 'failed_checks': num['failed'],
               'checks': f"{num['checks_passed']}/{num['checks_total']}", 'simulation': num['simulation'],
               'tokens_out_at_least_quote': num['tokens_out_at_least_quote'], 'gas_estimate': num['gas'],
               'block': num['block'], 'refused_with': cap.get('response_to_page'), 'page_showed': self.rejection,
               'signed': False, 'broadcast': False, 'captured_tx': 'captured_tx.json', 'review': self.review}
        self.log(f'{LABEL}: pons built the buy of {self.amount_str} ETH -> {num["labrat_out"]} LABRAT (quote), the rig '
                 f'refused to sign. Nothing was bought.' if not num['failed'] else
                 f'{LABEL}: the captured buy failed {num["failed"]}; refused. Nothing was bought.')
        return out

    def _hello_msg(self):
        return {'type': MSG['hello'], 'v': 1, 'source': SOURCE, 'label': LABEL, 'simulated': True,
                'session': self.stamp, 'coin': SYMBOL, 'venue': 'pons',
                'title': f'The rat buys ${SYMBOL} on pons', 'amount_eth': self.amount_str,
                'batch': ({'at': self.batch.get('at'), 'hits': self.batch.get('hits')} if self.batch.get('at') else None),
                'started': utc(self.t_start), 'viewport': [VIEW_W, VIEW_H],
                'frame': {'prefix': FRAME_PREFIX.decode(), 'format': 'jpeg', 'max_bytes': RELAY_MAX_FRAME,
                          'max_fps': RELAY_FPS},
                'targets': [PUBLIC_NAME[t.key] for t in self._shown()],
                'brain': {'networks': 2, 'units': brainrig.UNITS, 'connections': brainrig.CONNECTIONS,
                          'commit': self.commit},
                'note': PUBLIC_NOTE}

    @staticmethod
    def _fail_kind(failed):
        return 'simulation_failed' if failed and set(failed) <= {'simulation'} else 'checks_failed'

    def _tx_msg(self):
        num = self._numbers()
        res = self.inspection or {}
        ok = bool(res.get('ok'))
        return {'type': MSG['tx'], 'kind': 'tx', 'label': LABEL, 'simulated': True, 'session': self.stamp,
                'ok': ok, 'reason': None if ok else self._fail_kind(num['failed']),
                'eth_in': self.amount_str, 'amount_eth': self.amount_str,
                'labrat_out': num['labrat_out'], 'min_out': num['min_out'], 'market': 'Uniswap v4 pool',
                'max_slippage': '1%', 'checks_passed': num['checks_passed'], 'checks_total': num['checks_total'],
                'checks': [{'name': PUBLIC_CHECKS.get(k, k), 'ok': bool(v.get('ok'))}
                           for k, v in (res.get('checks') or {}).items()],
                'simulation': num['simulation'], 'gas': num['gas'], 'signed': False, 'sent': False,
                'status': 'checked, not signed' if ok else 'refused'}

    def _done_msg(self):
        r = self.result or {}
        num = self._numbers()
        ok = bool(r.get('ok'))
        if ok:
            reason = None
        elif self.inspection is not None and not self.inspection.get('ok'):
            reason = self._fail_kind(num['failed'])
        elif self.fail_reason in REASONS:
            reason = self.fail_reason
        else:
            reason = 'aborted'
        return {'type': MSG['done'], 'kind': 'done', 'label': LABEL, 'simulated': True, 'session': self.stamp,
                'ok': ok, 'reason': reason,
                'outcome': 'checked, not signed' if ok else ('refused' if self.inspection else 'stopped'),
                'eth_in': self.amount_str, 'amount_eth': self.amount_str, 'labrat_out': num['labrat_out'],
                'checks_passed': num['checks_passed'], 'checks_total': num['checks_total'],
                'simulation': num['simulation'], 'targets_hit': sum(1 for c in self.clicks if c['forwarded']),
                'misses': sum(1 for c in self.clicks if not c['forwarded']),
                'seconds': round(time.time() - self.t_start, 1), 'session_proof': self.proof, 'recorded': self.saved,
                'signed': False, 'sent': False, 'ended': utc()}

    def _stream_stats(self):
        st = super()._stream_stats()
        st['audits'] = dict(self.audits)
        if self.link is not None:
            st['relay'] = {'url_channel': 'pons', **dict(self.link.stats), 'fatal': self.link.fatal}
        return st

    async def _finish(self, outcome):
        if self.fail_reason is None and outcome.get('mode') in ('error', 'ended'):
            err = str(outcome.get('error') or outcome.get('reason') or '')
            if 'not clickable after' in err or 'within' in err:
                self.fail_reason = 'timeout'
        with self.cmd_lock:
            self.lit = None
        await self._cue(None)
        if self.bot and self.bot.page and self.page_downs is None:
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
        if self.bot and self.bot.page and self.final_jpg is None and self.breach is None:
            self.final_jpg = await self._checkpoint('final')
        await asyncio.sleep(1.0)
        if self.final_jpg:
            (self.run_dir / 'pons_final.jpg').write_bytes(self.final_jpg)
        cap = dict(self.capture) if self.capture else {'captured': False}
        cap['other_send_requests'] = [s for i, s in enumerate(self.sends) if i != self.handled_send]
        with open(self.run_dir / 'captured_tx.json', 'w', encoding='utf-8', newline='\n') as fh:
            json.dump(cap, fh, indent=2, default=str)
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
              'targets': [t.record() for t in self.targets], 'clicks': self.clicks, 'hits_forwarded': len(fwd),
              'misses_masked': len(miss), 'page_mousedowns': downs, 'page_mousedowns_not_from_a_hit': stray,
              'commands': self.cmd_trace, 'typing': self.typing}
        with open(self.run_dir / 'targets.json', 'w', encoding='utf-8', newline='\n') as fh:
            json.dump(tj, fh, indent=1, default=str)
        with open(self.run_dir / 'frames.json', 'w', encoding='utf-8', newline='\n') as fh:
            json.dump({'checkpoints': self.checkpoints, 'audits': dict(self.audits), 'breach': self.breach,
                       'rule': 'a frame is saved or streamed only between two clean mask audits'}, fh, indent=1,
                      default=str)
        B = brainrig.BRAIN
        rig = {'rig': 'live/buyrig.py', 'research': 'live/BUYRIG_RESEARCH.md', 'mode': 'DRY', 'label': LABEL,
               'dev_oracle': self.oracle, 'pons_url': COIN_URL, 'viewport': [VIEW_W, VIEW_H],
               'wallet_kind': 'fresh in-memory key per session (DRY)',
               'dry_balance_override_eth': ponsbot.DRY_BALANCE_WEI / 1e18, 'amount_eth': self.amount_str,
               'amount_wei': self.amount_wei, 'batch': self.batch, 'terms_shown': self.terms_shown,
               'send_requests': len(self.sends), 'signature_requests': len(self.bot.signatures) if self.bot else 0,
               'wallet_refusals': len(self.bot.refusals) if self.bot else 0,
               'rpc_sends_blocked': getattr(self.bot, 'rpc_sends_blocked', 0) if self.bot else 0,
               'page_showed': self.rejection,
               'stages': {t.key: t.state for t in self.targets}, 'hits_forwarded': len(fwd),
               'misses_masked': len(miss), 'page_mousedowns': len(downs), 'stray_mousedowns': len(stray),
               'stream': self._stream_stats(), 'mask_breach': self.breach is not None,
               'frames_saved': [c.get('file') for c in self.checkpoints if c.get('saved')],
               'steer_policy': B['steer_source'], 'steer_sha256': B['steer_sha256'],
               'press_policy': B['press_source'], 'press_sha256': B['press_sha256'], 'units': brainrig.UNITS,
               'connections': brainrig.CONNECTIONS, 'presses_seen': self.presses_seen,
               'type_delay_ms': brainrig.TYPE_DELAY_MS, 'honesty': HONESTY, 'started_utc': utc(self.t_start),
               'ended_utc': utc()}
        if self.oracle:
            with open(self.run_dir / 'oracle.json', 'w', encoding='utf-8', newline='\n') as fh:
                json.dump(scrub({'kind': 'DEV_ORACLE_NOT_A_BRAIN_RUN', 'buy': outcome, 'rig': rig}), fh, indent=1)
            self._write_result(outcome, rig)
            return True
        if self.sess is None or getattr(self.sess, 'env', None) is None or self.sess.proof is None:
            self.log('no brain session to record')
            self._write_result(outcome, rig)
            return False
        extra = scrub({'buy': outcome, 'rig': rig, 'end_reason': self.sim_end, 'end_info': self.sim_info})
        meta = await asyncio.to_thread(self.sess.save, str(self.run_dir), extra)
        self.proof = meta['session_proof']
        self.saved = True
        self.log(f"recorded {rel(self.run_dir)}: session.json (proof {meta['session_proof'][:16]}…, {meta['steps']} brain "
                 f'steps), qpos.npy, actions.npy, steer.pt, press.pt, captured_tx.json, events.jsonl, targets.json, '
                 f'frames/, pons_final.jpg · python replay_session.py {rel(self.run_dir)}')
        self._write_result(outcome, rig)
        return True

    def _write_result(self, outcome, rig):
        num = self._numbers()
        ok = (outcome.get('mode') == 'dry_captured' and self.saved and self.breach is None and not self.oracle)
        self.result = scrub({
            'ok': ok, 'verdict': outcome.get('mode'), 'label': LABEL, 'amount_eth': self.amount_str,
            'amount_wei': self.amount_wei, 'labrat_out': num['labrat_out'], 'tokens_wei': num['quote'],
            'min_out': num['min_out'], 'checks_passed': num['checks_passed'], 'checks_total': num['checks_total'],
            'failed_checks': num['failed'], 'simulation': num['simulation'],
            'tokens_out_at_least_quote': num['tokens_out_at_least_quote'], 'gas_estimate': num['gas'],
            'targets_hit': rig['hits_forwarded'], 'misses': rig['misses_masked'],
            'session_at': utc(self.t_start), 'ended_at': utc(), 'seconds': round(time.time() - self.t_start, 1),
            'run_dir': rel(self.run_dir), 'session_proof': self.proof, 'brain_commit': self.commit, 'seed': self.seed,
            'batch': self.batch, 'frames': rig['frames_saved'], 'mask_breach': self.breach is not None,
            'stream': rig['stream'], 'dev_oracle': self.oracle, 'signed': False, 'sent': False,
            'error': outcome.get('error') or outcome.get('reason')})
        with open(self.run_dir / 'buyrig_result.json', 'w', encoding='utf-8', newline='\n') as fh:
            json.dump(self.result, fh, indent=1)


def shrink_jpeg(jpg, width=960, quality=60):
    """A frame over the relay's size cap: re-encoded smaller (Pillow). None if that fails or is still too big."""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(jpg)).convert('RGB')
        if im.width > width:
            im = im.resize((width, round(im.height * width / im.width)))
        b = io.BytesIO()
        im.save(b, 'JPEG', quality=quality)
        out = b.getvalue()
        return out if len(out) + len(FRAME_PREFIX) <= RELAY_MAX_FRAME else None
    except Exception:
        return None


# ------------------------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------------------------
def load_brain(policy, press_policy):
    """The two networks, read once and frozen (brainrig's helpers). -> fills brainrig.BRAIN."""
    path, note = brainrig.resolve_policy(policy)
    ppath = Path(press_policy)
    ppath = (ppath if ppath.is_absolute() else ROOT / ppath).resolve()
    for p in (path, ppath):
        try:
            p.relative_to(ROOT)
        except ValueError:
            raise SystemExit('both networks must live under the ratbrain folder')
    if not ppath.exists():
        raise SystemExit(f'press network not found: {ppath}')
    steer_bytes = brainrig.snapshot_policy(path, brainrig.STEER_SIZES, 'steering network')
    press_bytes = brainrig.snapshot_policy(ppath, brainrig.PRESS_SIZES, 'lever-press network')
    commit = brainrig.brain_commit_for(steer_bytes, press_bytes)
    brainrig.BRAIN.update(steer_bytes=steer_bytes, press_bytes=press_bytes, steer_source=rel(path),
                          press_source=rel(ppath), note=note, steer_sha256=sha(steer_bytes),
                          press_sha256=sha(press_bytes), commit=commit)
    return note


def configure(seed, headful=False, dev_oracle=False):
    """brainrig.Run reads brainrig.CONFIG; in this process it always describes a DRY buy session."""
    brainrig.CONFIG.clear()
    brainrig.CONFIG.update(coin={'name': 'Labrat', 'symbol': SYMBOL, 'tax_bps': 0, 'x': '', 'website': ''},
                           seed=int(seed), port=0, dev_oracle=bool(dev_oracle), dev_shots=False, headful=bool(headful),
                           mode='DRY', live=None, token=None, pinned=None)


async def run_session(amount, seed, relay=None, token=None, batch=None, out_root=None, rpc=None, link=None):
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(brainrig.EXECUTOR_THREADS, thread_name_prefix='buyrig'))
    if link is None and relay:
        link = PonsLink(relay, token)
    r = BuyRun(amount, seed, link=link, batch=batch, rpc=rpc, out_root=out_root)
    brainrig.ACTIVE['run'] = r
    brainrig.STATE['busy'] = True
    try:
        await r.run()
    finally:
        brainrig.STATE['busy'] = False
    return r


def main(argv=None):
    for stream, kw in ((sys.stdout, {'line_buffering': True}), (sys.stderr, {})):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace', **kw)
        except Exception:
            pass
    ap = argparse.ArgumentParser(description='RATBRAIN buy rig: the rat clicks through a simulated $LABRAT buy on pons '
                                             '(DRY only: nothing is signed or sent)')
    ap.add_argument('--amount', required=True, help='the batch amount in ETH, e.g. 0.0001 (0.00001 .. 0.01)')
    ap.add_argument('--seed', type=int, default=2026)
    ap.add_argument('--policy', default='runs/final/steer.pt', help='the STEERING network')
    ap.add_argument('--press-policy', default='runs/final/policy.pt', help='the lever-PRESS network')
    ap.add_argument('--relay', help=f'publish to <relay>?channel=pons (token: the {TOKEN_ENV} environment variable)')
    ap.add_argument('--batch-at', help="the engine's booked buy time (iso), for the record and the stream")
    ap.add_argument('--hits', type=int, help='the hits the batch covers, for the record and the stream')
    ap.add_argument('--out-root', help='where run dirs go (default runs/)')
    ap.add_argument('--result-json', help='also write the public-safe result here')
    ap.add_argument('--headful', action='store_true', help='show the Chromium window')
    ap.add_argument('--dev-oracle', action='store_true',
                    help='TESTING ONLY: a scripted cursor instead of the brain (never streamed, never reported)')
    a = ap.parse_args(argv)
    try:
        amount, _wei = parse_amount(a.amount)
    except ValueError as e:
        raise SystemExit(f'bad --amount: {e}')
    token = None
    if a.relay:
        if a.dev_oracle:
            raise SystemExit('--dev-oracle is a scripted cursor, never the rat: it is never streamed')
        if not re.match(r'^wss?://', a.relay):
            raise SystemExit('--relay must be a ws:// or wss:// URL')
        token = os.environ.get(TOKEN_ENV, '').strip()
        if len(token) < 16:
            raise SystemExit(f'--relay needs the {TOKEN_ENV} environment variable (16+ characters)')
    if a.batch_at and not buyback.ISO_RE.match(a.batch_at):
        raise SystemExit('--batch-at must look like 2026-09-25T03:22:58Z')
    note = load_brain(a.policy, a.press_policy)
    configure(a.seed, a.headful, a.dev_oracle)
    say(f"\n  RATBRAIN buy rig · {LABEL} · {amount} ETH of ${SYMBOL} on pons · seed {a.seed}"
        + (' · DEV ORACLE (scripted cursor, NOT the rat)' if a.dev_oracle else '') + '\n'
        + (f'  {note}\n' if note else '')
        + f"  brain commit {brainrig.BRAIN['commit']} · relay {'pons channel' if a.relay else 'off'}\n")
    batch = {'at': a.batch_at, 'hits': a.hits} if (a.batch_at or a.hits is not None) else None
    r = asyncio.run(run_session(amount, a.seed, relay=a.relay, token=token, batch=batch, out_root=a.out_root))
    res = r.result or {'ok': False, 'verdict': None, 'run_dir': rel(r.run_dir), 'error': 'no result was written'}
    if a.result_json:
        with open(a.result_json, 'w', encoding='utf-8', newline='\n') as fh:
            json.dump(res, fh, indent=1)
    say('BUYRIG_RESULT ' + json.dumps({k: res.get(k) for k in ('ok', 'verdict', 'amount_eth', 'labrat_out',
                                                                  'checks_passed', 'checks_total', 'simulation',
                                                                  'targets_hit', 'misses', 'run_dir',
                                                                  'session_proof')}))
    return 0 if res.get('ok') else 2


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
