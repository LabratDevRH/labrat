"""pons for the RATBRAIN live rig (live/SPEC.md): Playwright on the REAL ponsfamily.com create page, an
injected EIP-1193 wallet, DRY capture / decode / check / refuse of the launch transaction pons builds, and
the gated LIVE send path.

Adapted (copied, not imported) from flybrain's rhlive.py (terms, banner, theme, glide, smooth scroll,
creator tax, image upload, form fill, launch-button detection) and rhprovider.py (the provider).

Re-verified against the live site on 2026-09-24 (headless Chromium, throwaway wallet, nothing signed):
  * pons connects an injected EIP-1193 provider by itself (window.ethereum + an EIP-6963 announce)
  * a wallet pons has not seen gets a "Review and accept" modal: two checkboxes (Terms of Use, Privacy
    Policy; the not-in-a-restricted-jurisdiction attestation is the modal's own text) and
    "Accept and continue"
  * fields by placeholder: "Token name", "symbol", "A short description of the token"; the image goes
    through the "Choose image" picker, pons pins it (POST /api/ipfs/image) and then says "Image ready"
  * Advanced (aria-expanded) -> input[aria-label="Creator tax percent"]
  * the action button relabels itself: "Fill token details" -> "Insufficient ETH" -> "Launch token"
  * "Launch token" opens pons's review dialog ("Launch <SYMBOL>", Confirm / Cancel). Confirm makes pons
    call wallet_switchEthereumChain, one eth_call, then eth_sendTransaction on the wallet:
    {from, to = v2 factory, data = 0xa72101af..., value = 0.0005 ETH}, no gas, no nonce
  * the two bytes32 fields in that calldata are non-zero, filled by pons
  * pons reads the wallet balance with multicall3 getEthBalance through its own proxy /api/robinhood-rpc
  * a 4001 rejection shows the toast "Launch failed / You cancelled in your wallet."
  * pons never asked this wallet for a signature (personal_sign / typed data) on any rig run so far

Who clicks what: the operator script fills the form and opens pons's launch review; the rat's lever press
clicks Confirm, which makes pons request the launch transaction.

Safety model
  * The private key never enters the page. The page gets an address; signatures and transactions are
    made in Python behind page.expose_binding (flybrain's rhprovider pattern). Every binding call is
    refused unless it comes from the page's MAIN frame on https://www.ponsfamily.com (no iframe, no other
    origin), and the provider script itself installs only there. Each binding re-checks its method in
    Python, so calling a __ratEth* binding directly gains nothing over window.ethereum.request.
  * Reads the wallet forwards to the chain are a fixed read-only allowlist (READ_METHODS); anything else
    gets EIP-1193 4200.
  * DRY (default): the wallet is a throwaway in-memory key. pons would otherwise show "Insufficient ETH",
    so the page's OWN balance reads for that one address are answered as 1 ETH (an eth_call state override
    added to the page's RPC requests, disclosed on screen). eth_sendTransaction is decoded, checked and
    REFUSED with EIP-1193 4001. Nothing is signed, nothing is broadcast. A login signature, if pons ever
    asked for one, would be made with the throwaway key only.
  * LIVE: LiveLaunch below. Reachable only through rig.py's or brainrig.py's gates (.env RATBRAIN_LIVE=1 + key,
    --live --confirm SYMBOL, launcher.preflight before the trial, launcher.reserve_journal). It never signs a
    transaction that fails a check, signs exactly the calldata bytes that were decoded and checked, pins
    nonce 0, and journals the raw signed bytes before broadcast. The funded key refuses ALL typed data
    (eth_signTypedData*) and every personal_sign except a sign-in message for www.ponsfamily.com that
    names our address and chain 4663 and was issued within the last 10 minutes (siwe_login_ok).
"""
import asyncio
import json
import math
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from eth_abi import decode, encode
from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data
from eth_utils import to_checksum_address

LIVE_DIR = Path(__file__).resolve().parent
ROOT = LIVE_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import launcher  # noqa: E402

URL = 'https://www.ponsfamily.com/launchpad/create'
PONS_ORIGIN = 'https://www.ponsfamily.com'
PONS_DOMAIN = 'www.ponsfamily.com'
CHAIN_ID = 4663
DRY_BALANCE_WEI = 10 ** 18
USER_REJECTED = 4001
UNAUTHORIZED = 4100
UNSUPPORTED = 4200
DRY_REFUSAL = 'DRY RUN: rat rig refused to sign'
# 1280x900 is pons's desktop layout at nearly the aspect of the viewer's pons pane (~790x560), so the stream
# is scaled less than the old 1024x960 and stays sharper; pons keeps its 2-column form (verified by a DRY run)
PAGE_SIZE = (1280, 900)
# the only JSON-RPC methods the injected wallet forwards to the chain: reads. Anything else gets 4200.
READ_METHODS = frozenset((
    'eth_call', 'eth_estimateGas', 'eth_getBalance', 'eth_blockNumber', 'eth_getCode', 'eth_getTransactionCount',
    'eth_gasPrice', 'eth_maxPriorityFeePerGas', 'eth_feeHistory', 'eth_getBlockByNumber', 'eth_getBlockByHash',
    'eth_getTransactionReceipt', 'eth_getTransactionByHash', 'eth_getLogs', 'eth_chainId', 'net_version'))
TYPED_METHODS = frozenset(('eth_signTypedData', 'eth_signTypedData_v3', 'eth_signTypedData_v4'))
SIWE_MAX_AGE_S = 600
# LIVE, at sign time: the preflight maxFeePerGas (FEE_MULT x eth_gasPrice when the launch was armed) must still be at
# least this many times the CURRENT eth_gasPrice. The signed bytes are journalled before broadcast and recovery only
# ever re-broadcasts those exact bytes, so an underpriced signature would strand the fresh wallet: refuse unsigned.
SIGN_FEE_HEADROOM = 1.25
SIWE_KEYS =('URI', 'Version', 'Chain ID', 'Nonce', 'Issued At', 'Expiration Time', 'Not Before', 'Request ID')
SEL = {'name': 'input[placeholder="Token name"]',
       'ticker': 'input[placeholder="symbol"]',
       'description': 'textarea[placeholder="A short description of the token"]'}
TAX_SEL = 'input[aria-label="Creator tax percent"]'
RH_RPC_HOSTS = ('rpc.mainnet.chain.robinhood.com', 'robinhood-rpc.publicnode.com')
TERMS_RE = re.compile(r'review and accept', re.I)
REJECTION_RE = r'(cancel+ed in your wallet|user rejected|rejected the request|request rejected|denied)'


class PonsError(RuntimeError):
    pass


def err(code, message):
    return {'error': {'code': int(code), 'message': str(message)}}


def refusal(message=DRY_REFUSAL):
    return err(USER_REJECTED, message)


def short(addr):
    return f'{addr[:6]}...{addr[-4:]}' if addr else str(addr)


def hex0x(b):
    """hexbytes >= 1.0 drops the 0x prefix (it broke flybrain's broadcasts once)."""
    h = b.hex() if hasattr(b, 'hex') else str(b)
    return h if h.startswith('0x') else '0x' + h


def as_int(v, default=0):
    if v is None:
        return default
    if isinstance(v, int):
        return v
    s = str(v)
    return int(s, 16) if s.lower().startswith('0x') else int(s)


def hex_bytes(s):
    """'0x...' -> bytes; ValueError for anything that is not 0x-prefixed, even-length hex."""
    if not isinstance(s, str) or not s[:2].lower() == '0x':
        raise ValueError('not 0x-prefixed hex')
    return bytes.fromhex(s[2:])


def origin_of(url):
    u = urlsplit(str(url or ''))
    return f'{u.scheme}://{u.netloc}'.lower() if u.scheme and u.netloc else ''


def broadcast_state(send_error):
    """What a send result says about the raw tx leaving this machine: True (an RPC accepted it, or said it
    already knows it), 'unknown' (every RPC timed out: it may have landed) or False (every RPC rejected it)."""
    if send_error is None:
        return True
    s = str(send_error).lower()
    if any(p in s for p in launcher.POSSIBLY_SENT):
        return True
    if s.startswith('send raised'):
        return 'unknown'
    return False


def siwe_login_ok(text, address, now=None):
    """LIVE personal_sign policy: -> (ok, why). Only an EIP-4361 sign-in message for www.ponsfamily.com, for OUR
    address, on chain 4663, URI on https://www.ponsfamily.com, issued within SIWE_MAX_AGE_S of now, with no
    resources and no duplicated or unknown fields. Anything else is refused."""
    now = time.time() if now is None else now
    lines = str(text).replace('\r\n', '\n').split('\n')
    while lines and lines[-1] == '':
        lines.pop()
    if len(lines) < 3 or lines[0] != f'{PONS_DOMAIN} wants you to sign in with your Ethereum account:':
        return False, f'not a sign-in message for {PONS_DOMAIN}'
    if lines[1].strip().lower() != str(address).lower():
        return False, 'the sign-in names a different address'
    starts = [i for i, ln in enumerate(lines) if ln.startswith('URI: ')]
    if len(starts) != 1:
        return False, 'the sign-in has no single URI field'
    fields = {}
    for ln in lines[starts[0]:]:
        if ': ' not in ln:
            return False, f'unexpected line in the sign-in fields: {ln[:60]!r}'
        k, v = ln.split(': ', 1)
        if k not in SIWE_KEYS:
            return False, f'the sign-in carries a field {k[:40]!r} (only {", ".join(SIWE_KEYS)} are allowed)'
        if k in fields:
            return False, f'the sign-in repeats {k!r}'
        fields[k] = v.strip()
    if any(ln.strip().lower().startswith('resources') for ln in lines):
        return False, 'the sign-in requests resources'
    for k in ('URI', 'Version', 'Chain ID', 'Nonce', 'Issued At'):
        if not fields.get(k):
            return False, f'the sign-in has no {k}'
    if origin_of(fields['URI']) != PONS_ORIGIN:
        return False, f"the sign-in URI {fields['URI'][:80]!r} is not on {PONS_ORIGIN}"
    if fields['Version'] != '1':
        return False, f"sign-in version {fields['Version']!r}"
    if fields['Chain ID'] != str(CHAIN_ID):
        return False, f"the sign-in is for chain {fields['Chain ID']!r}, not {CHAIN_ID}"

    def ts(v):
        return datetime.fromisoformat(v.replace('Z', '+00:00')).astimezone(timezone.utc).timestamp()
    try:
        issued = ts(fields['Issued At'])
        expires = ts(fields['Expiration Time']) if 'Expiration Time' in fields else None
        not_before = ts(fields['Not Before']) if 'Not Before' in fields else None
    except (ValueError, TypeError):
        return False, 'the sign-in has an unreadable timestamp'
    if abs(now - issued) > SIWE_MAX_AGE_S:
        return False, f'the sign-in was issued {int(now - issued)} s from now (limit {SIWE_MAX_AGE_S} s)'
    if expires is not None and expires <= now:
        return False, 'the sign-in has expired'
    if not_before is not None and not_before > now:
        return False, 'the sign-in is not valid yet'
    return True, 'pons sign-in'


# --------------------------------------------------------------------------------------------------------
# the injected wallet (rhprovider.py, adapted): only an address and signatures ever cross into the page
# --------------------------------------------------------------------------------------------------------
PROVIDER_JS = r"""
(() => {
  // main frame on pons only: never in an iframe (ads, embeds) and never on any other origin. The Python
  // bindings refuse such calls anyway (PonsBot._guarded); this keeps the wallet from even appearing there.
  if (window.top !== window) return;
  if (location.origin !== "__ORIGIN__") return;
  if (window.__ratProvider) return;
  const ADDR = "__ADDR__";
  const CHAIN_HEX = "__CHAIN_HEX__";
  const RDNS = "xyz.ratbrain.rig";
  const listeners = {};
  let announced = false;
  const emit = (ev, ...a) => (listeners[ev] || []).forEach(f => { try { f(...a); } catch (e) {} });
  const fail = (er) => {
    const e = new Error((er && er.message) || "wallet error");
    e.code = (er && er.code) || -32603;
    if (er && er.data !== undefined) e.data = er.data;
    throw e;
  };
  const unwrap = (r) => { if (r && r.error) fail(r.error); return r ? r.result : null; };

  async function request(args) {
    const method = args && args.method;
    const params = (args && args.params) || [];
    switch (method) {
      case "eth_requestAccounts":
        if (!announced) { announced = true; emit("connect", { chainId: CHAIN_HEX }); emit("accountsChanged", [ADDR]); }
        return [ADDR];
      case "eth_accounts":
        return [ADDR];
      case "eth_chainId":
        return CHAIN_HEX;
      case "net_version":
        return String(parseInt(CHAIN_HEX, 16));
      case "wallet_switchEthereumChain": {
        const want = params[0] && params[0].chainId;
        if (want && parseInt(want, 16) !== parseInt(CHAIN_HEX, 16))
          fail({ code: 4902, message: "the rig wallet is on Robinhood Chain (4663) only" });
        return null;
      }
      case "wallet_addEthereumChain":
        return null;
      case "wallet_requestPermissions":
      case "wallet_getPermissions":
        return [{ parentCapability: "eth_accounts" }];
      case "wallet_revokePermissions":
        return null;
      case "personal_sign":
      case "eth_sign":
        return unwrap(await window.__ratEthSign(method, params));
      case "eth_signTypedData":
      case "eth_signTypedData_v3":
      case "eth_signTypedData_v4":
        return unwrap(await window.__ratEthSignTyped(method, params));
      case "eth_sendTransaction":
        return unwrap(await window.__ratEthSend(params[0] || {}));
      case "eth_signTransaction":
      case "eth_sendRawTransaction":
      case "wallet_sendCalls":
      case "wallet_getCapabilities":
        fail({ code: 4200, message: "the rig wallet does not support " + method });
      default:
        // plain reads: let the chain answer (DRY adds the disclosed balance override in Python)
        return unwrap(await window.__ratEthRpc(method, params));
    }
  }

  const provider = {
    isMetaMask: true,
    isRatbrainRig: true,
    chainId: CHAIN_HEX,
    networkVersion: String(parseInt(CHAIN_HEX, 16)),
    selectedAddress: ADDR,
    request,
    enable: () => request({ method: "eth_requestAccounts" }),
    send: (m, p) => (typeof m === "string" ? request({ method: m, params: p }) : request(m)),
    sendAsync: (payload, cb) => request(payload)
      .then(r => cb(null, { id: payload.id, jsonrpc: "2.0", result: r }))
      .catch(e => cb(e)),
    on(ev, f) { (listeners[ev] = listeners[ev] || []).push(f); return provider; },
    removeListener(ev, f) { listeners[ev] = (listeners[ev] || []).filter(x => x !== f); return provider; },
    removeAllListeners() { for (const k in listeners) delete listeners[k]; },
    isConnected: () => true,
  };
  window.__ratProvider = provider;
  try {
    Object.defineProperty(window, "ethereum", { value: provider, writable: false, configurable: true });
  } catch (e) { window.ethereum = provider; }

  // EIP-6963, complete or not at all
  const info = {
    uuid: "3c1e8a52-7b0d-4f7e-9a51-5d2b7c9e0f14",
    name: "RATBRAIN rig",
    icon: "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxNiIgaGVpZ2h0PSIxNiI+PHJlY3Qgd2lkdGg9IjE2IiBoZWlnaHQ9IjE2IiBmaWxsPSIjZmYzYzgyIi8+PC9zdmc+",
    rdns: RDNS,
  };
  const announce = () => window.dispatchEvent(new CustomEvent("eip6963:announceProvider",
    { detail: Object.freeze({ info, provider }) }));
  window.addEventListener("eip6963:requestProvider", announce);
  announce();
  window.dispatchEvent(new Event("ethereum#initialized"));
})();
"""

# A headless screenshot has no mouse pointer, so the rig draws its own: white and labelled
# "operator script" for the pre-fill, pink and labelled "rat press" for the click the press triggers.
CURSOR_JS = r"""
(() => {
  if (window.top !== window) return;
  if (window.__ratCursorInstalled) return;
  window.__ratCursorInstalled = true;
  let mode = "script", x = -200, y = -200;
  const COL = { script: "#f4f4f6", rat: "#ff3c82" };
  const LAB = { script: "operator script", rat: "rat press" };
  function paint() {
    const p = document.getElementById("__ratcurp"), l = document.getElementById("__ratcurl");
    if (p) p.setAttribute("fill", COL[mode]);
    if (l) {
      l.textContent = LAB[mode];
      l.style.color = mode === "rat" ? "#07070a" : "#f4f4f6";
      l.style.background = mode === "rat" ? "#ff3c82" : "rgba(7,7,10,.86)";
    }
  }
  function place() {
    const c = document.getElementById("__ratcur");
    if (c) c.style.transform = "translate(" + x + "px," + y + "px)";
  }
  function el() {
    let c = document.getElementById("__ratcur");
    if (c) return c;
    if (!document.documentElement) return null;
    c = document.createElement("div");
    c.id = "__ratcur";
    c.style.cssText = "position:fixed;left:0;top:0;pointer-events:none;z-index:2147483647;will-change:transform;";
    c.innerHTML = '<svg width="22" height="28" viewBox="0 0 22 28" style="display:block;overflow:visible;' +
      'filter:drop-shadow(0 1px 2px rgba(0,0,0,.6))"><path id="__ratcurp" d="M1 1 L1 22 L6.5 16.8 L10.2 26 ' +
      'L14 24.4 L10.4 15.4 L18 15.4 Z" fill="#f4f4f6" stroke="#07070a" stroke-width="1.6" ' +
      'stroke-linejoin="round"/></svg><div id="__ratcurl" style="position:absolute;left:18px;top:24px;' +
      'font:600 10px/1 Bahnschrift,Segoe UI,system-ui,sans-serif;letter-spacing:.06em;padding:3px 6px;' +
      'border-radius:3px;white-space:nowrap;text-transform:uppercase"></div>';
    document.documentElement.appendChild(c);
    paint(); place();
    return c;
  }
  window.addEventListener("mousemove", e => { x = e.clientX; y = e.clientY; el(); place(); },
    { capture: true, passive: true });
  window.addEventListener("mousedown", e => {
    if (!document.documentElement) return;
    const r = document.createElement("div");
    r.style.cssText = "position:fixed;left:" + (e.clientX - 20) + "px;top:" + (e.clientY - 20) + "px;" +
      "width:40px;height:40px;border-radius:50%;border:3px solid " + COL[mode] + ";" +
      "pointer-events:none;z-index:2147483646;";
    document.documentElement.appendChild(r);
    const a = r.animate([{ transform: "scale(.35)", opacity: 1 }, { transform: "scale(1.9)", opacity: 0 }],
      { duration: 700, easing: "ease-out" });
    a.onfinish = () => r.remove();
  }, { capture: true, passive: true });
  window.__ratCursorMode = (m) => { mode = m === "rat" ? "rat" : "script"; el(); paint(); return mode; };
})();
"""

LAUNCH_BTN_JS = """() => {
  const bs = [...document.querySelectorAll('button')]
    .map(b => ({b, r: b.getBoundingClientRect(), t: (b.textContent || '').trim()}))
    .filter(o => o.r.width > 300 && o.r.height > 40
                 && !/^(advanced|eth|usdc)$/i.test(o.t) && !/creator fees/i.test(o.t));
  // pons relabels this button by state ("Fill token details", "Insufficient ETH", "Launch token"), so
  // prefer the label and fall back to flybrain's shape rule: the full-width button lowest in the form
  const byText = bs.find(o => /^launch token$/i.test(o.t));
  const pick = byText || bs.sort((p, q) => (q.r.y + scrollY) - (p.r.y + scrollY))[0];
  if (!pick) return null;
  const r = pick.r;
  return {x: r.x, y: r.y, w: r.width, h: r.height, docY: r.y + scrollY, label: pick.t,
          disabled: !!pick.b.disabled}; }"""

# pons's review dialog's Confirm, measured now. Only a visible "Confirm" button whose own dialog (the nearest
# ancestor below <body> that has one) carries the exact line "Launch <SYMBOL>" counts; onTop says the button
# itself is what a click at its centre would hit (nothing covers it). null when there is no such button.
CONFIRM_JS = """(sym) => {
  const want = ('launch ' + String(sym || '')).toLowerCase();
  const lines = el => (el.innerText || '').split(String.fromCharCode(10)).map(s => s.trim()).filter(Boolean);
  const btns = [...document.querySelectorAll('button')]
    .filter(x => /^\\s*confirm\\s*$/i.test(x.textContent || '') && x.getBoundingClientRect().width > 0);
  for (const b of btns) {
    let d = b, hit = null;
    for (let i = 0; i < 14 && d.parentElement && d.parentElement !== document.body; i++) {
      d = d.parentElement;
      if (lines(d).some(l => l.toLowerCase() === want)) { hit = d; break; }
    }
    if (!hit || !sym) continue;
    const r = b.getBoundingClientRect();
    const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
    return {x: r.x, y: r.y, w: r.width, h: r.height, label: (b.textContent || '').trim(),
            disabled: !!b.disabled, onTop: !!top && (top === b || b.contains(top)),
            title: 'Launch ' + sym, dialog: lines(hit).slice(0, 32)};
  }
  return null; }"""


# --------------------------------------------------------------------------------------------------------
# decode + check the launch transaction pons builds
# --------------------------------------------------------------------------------------------------------
def decode_launch(tx):
    """Every field of pons's eth_sendTransaction request, decoded with launcher.TYPES / SELECTOR.
    Field names follow the viewer's tx panel (live/web/rig.html TX_ORDER).
    -> (fields, data_hex): data_hex is '0x' + exactly the calldata bytes that were decoded (the only bytes
    LIVE may sign), or None when nothing decoded. A request that carries BOTH data and input with different
    bytes is refused (decode_error), since it is unclear which one a wallet would sign."""
    wei = as_int(tx.get('value'), 0)
    d_raw, i_raw = tx.get('data'), tx.get('input')
    src = d_raw if d_raw is not None else i_raw
    f = {'to': tx.get('to'), 'from': tx.get('from'), 'selector': str(src or '')[:10].lower(),
         'value': f'{wei / 1e18:.6g} ETH', 'value_wei': str(wei),
         'calldata_bytes': max(0, (len(str(src or '0x')) - 2) // 2), 'abi_encoding': 'not decoded'}
    for k in ('gas', 'gasPrice', 'maxFeePerGas', 'maxPriorityFeePerGas', 'nonce', 'chainId', 'type'):
        if tx.get(k) is not None:
            f[k] = str(as_int(tx.get(k)))
    try:
        raw = hex_bytes(src)
        if d_raw is not None and i_raw is not None and hex_bytes(i_raw) != raw:
            f['decode_error'] = 'the request carries both data and input, and they differ'
            return f, None
    except ValueError as e:
        f['decode_error'] = f'calldata is not hex: {e}'
        return f, None
    f['calldata_bytes'] = len(raw)
    if '0x' + raw[:4].hex() != launcher.SELECTOR:
        f['decode_error'] = f"selector {f['selector']} is not the pons v2 launch ({launcher.SELECTOR})"
        return f, None
    try:
        decoded = decode(launcher.TYPES, raw[4:])
    except Exception as e:
        f['decode_error'] = f'calldata does not decode as launcher.TYPES: {e!r}'[:300]
        return f, None
    params, arg1, pair, extra = decoded
    name, symbol, image, desc, links, creator, tax, u7, b8, b9 = params
    m = re.search(r'sha256 ([0-9a-f]{64})', desc)
    try:        # canonical: re-encoding the decoded values gives back the very same bytes (no hidden slack)
        canonical = encode(launcher.TYPES, list(decoded)) == raw[4:]
    except Exception:
        canonical = False
    f.update({
        'name': name, 'symbol': symbol, 'image': image, 'description': desc,
        'proof': m.group(1) if m else None,
        'socials': list(links),                   # (x, telegram, website, s4, s5)
        'creator': to_checksum_address(creator), 'creatorTaxBps': int(tax),
        'uint256_0': str(u7),                     # launcher.calldata writes 0 here
        'bytes32_a': hex0x(b8), 'bytes32_b': hex0x(b9),   # pons fills these (non-zero on 2026-09-24)
        'amountIn': str(arg1),                    # the uint256 after the struct (0: no developer buy)
        'pairToken': to_checksum_address(pair), 'trailingBytes': hex0x(extra),
        'abi_encoding': 'canonical' if canonical else 'NON-CANONICAL',
    })
    return f, '0x' + raw.hex()


def check_launch(f, expect):
    """Per-field checks against what the rig filled: {field: {'ok': bool, 'expected': str}}.
    expect: name, symbol, proof, creator, tax_bps (launcher.config), tax_set (the page's creator tax field
    took that value), image (the ipfs URI pons returned from /api/ipfs/image; None = none captured), x, website,
    and optionally description (the exact text the rig typed; default launcher.description(proof), the lever
    rig's; the brain rig passes its brain-commit description, and 'proof' is then the brain commit).
    The same checks run in DRY and LIVE, so a DRY run proves every one of them."""
    lo = lambda a: str(a or '').lower()  # noqa: E731
    desc = expect.get('description') or launcher.description(expect['proof'])
    socials = [expect.get('x') or '', '', expect.get('website') or '', '', '']
    img = expect.get('image')
    tax = int(expect['tax_bps'])
    want = {
        'to': (lo(f.get('to')) == launcher.FACTORY.lower(), launcher.FACTORY),
        'selector': (f.get('selector') == launcher.SELECTOR, launcher.SELECTOR),
        'value': (f.get('value_wei') == str(launcher.LAUNCH_FEE_WEI), f'{launcher.LAUNCH_FEE_WEI} wei (0.0005 ETH)'),
        'pairToken': (lo(f.get('pairToken')) == launcher.ZERO.lower(), f'{launcher.ZERO} (ETH)'),
        'name': (f.get('name') == expect['name'], expect['name']),
        'symbol': (f.get('symbol') == expect['symbol'], expect['symbol']),
        'description': (f.get('description') == desc, desc),
        'proof': (f.get('proof') == expect['proof'], expect['proof']),
        'creator': (lo(f.get('creator')) == lo(expect['creator']), expect['creator']),
        'image': (bool(img) and f.get('image') == img, img or 'the ipfs URI pons pinned (none was captured)'),
        'socials': (f.get('socials') == socials, json.dumps(socials)),
        'creatorTaxBps': (bool(expect.get('tax_set')) and f.get('creatorTaxBps') == tax,
                          f'{tax}' + ('' if expect.get('tax_set') else ' (the page did not take it)')),
        'amountIn': (f.get('amountIn') == '0', '0 (no developer buy)'),
        'uint256_0': (f.get('uint256_0') == '0', '0'),
        'trailingBytes': (f.get('trailingBytes') == '0x', '0x (empty)'),
        'abi_encoding': (f.get('abi_encoding') == 'canonical' and not f.get('decode_error'),
                         'canonical (decodes as launcher.TYPES and re-encodes byte for byte)'),
    }
    if f.get('from') is not None:
        want['from'] = (lo(f.get('from')) == lo(expect['creator']), expect['creator'])
    if f.get('chainId') is not None:
        want['chainId'] = (f.get('chainId') == str(CHAIN_ID), str(CHAIN_ID))
    return {k: {'ok': bool(ok), 'expected': str(exp)} for k, (ok, exp) in want.items()}


def all_ok(checks):
    return bool(checks) and all(bool(c.get('ok')) if isinstance(c, dict) else bool(c) for c in checks.values())


def simulate(tx, creator, fake_balance, data_hex=None):
    """eth_call the exact transaction pons built (data_hex: the decoded calldata bytes, when given).
    DRY: with a simulated 1 ETH for the throwaway wallet."""
    call = {'from': creator, 'to': tx.get('to'), 'data': data_hex or tx.get('data') or '0x',
            'value': hex(as_int(tx.get('value'), 0))}
    args = [call, 'latest'] + ([{creator: {'balance': hex(DRY_BALANCE_WEI)}}] if fake_balance else [])
    out = {'fake_balance_eth': DRY_BALANCE_WEI / 1e18 if fake_balance else None}
    try:
        res, e = launcher.rpc('eth_call', args)
    except Exception as ex:
        return {**out, 'ok': False, 'error': f'rpc failed: {ex!r}'[:200]}
    if e or not res or len(res) < 130:
        return {**out, 'ok': False, 'error': str(e or f'bad eth_call result {res!r}')[:300]}
    token, curve = decode(['address', 'address'], bytes.fromhex(res[2:130]))
    out.update(ok=True, predicted_token=to_checksum_address(token), predicted_curve=to_checksum_address(curve))
    try:
        g, e2 = launcher.rpc('eth_estimateGas', args)
        if g:
            out['gas_estimate'] = int(g, 16)
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------------------------------------
# LIVE: reachable only through rig.py's or brainrig.py's gates
# --------------------------------------------------------------------------------------------------------
class LiveLaunch:
    """SPEC 4 LIVE. rig.py / brainrig.py create this only after: .env RATBRAIN_LIVE=1 + key (launcher.config), --live
    --confirm SYMBOL, launcher.preflight(cfg, proof) passed before the trial, and launcher.reserve_journal
    succeeded. sign_and_send refuses anything that fails a check, signs exactly the decoded calldata bytes
    pinned to nonce 0 with the preflight gas figures, journals the raw signed bytes BEFORE broadcasting, and
    returns (hash, send_error). ONE call only: after sign_and_send or abort_unsigned, nothing else can sign.

    description: the exact coin description the calldata carries (journalled with the launch); default
    launcher.description(expected_proof), the lever rig's. The brain rig passes its brain-commit description.
    via: which rig wrote the journal. on_signed(tx_hash): called (from the signing thread) after the raw signed
    bytes are in launch_journal.json and BEFORE they are broadcast; an exception in it is ignored."""

    def __init__(self, cfg, pre, expected_proof, log=print, description=None, via='live/rig.py', on_signed=None):
        if not cfg.get('live_env'):
            raise launcher.LaunchRefused('.env does not say RATBRAIN_LIVE=1')
        launcher.check_proof(expected_proof)
        self.cfg, self.pre, self.expected, self.log = cfg, pre, expected_proof, log
        self.description = description if description is not None else launcher.description(expected_proof)
        self.via = via
        self.on_signed = on_signed
        self.acct = pre['acct']
        self.creator = self.acct.address
        self.lock = threading.Lock()
        self.used = False
        self.sent = None
        self.finished = None

    def _journal_abort(self, reason, **extra):
        launcher.write_journal({'state': 'aborted_before_sign', 'error': reason, 'proof': self.expected,
                                'via': self.via, **extra})

    def abort_unsigned(self, reason, **extra):
        """Close this launch without signing: the journal says aborted_before_sign with the reason. Returns
        False (and writes nothing) if sign_and_send already started or the launch was already closed."""
        with self.lock:
            if self.used:
                return False
            self.used = True
        self._journal_abort(reason, **extra)
        return True

    def sign_and_send(self, tx, fields, checks, data_hex):
        """Blocking (run it in an executor). -> (tx hash, send_error or None), or raises LaunchRefused
        (the journal then says aborted_before_sign, and nothing was signed)."""
        with self.lock:
            if self.used:
                raise launcher.LaunchRefused('this rig already handled (or closed) its one launch')
            self.used = True
        try:
            bad = sorted(k for k, v in checks.items() if not (v.get('ok') if isinstance(v, dict) else v))
            if not checks:
                bad = ['no checks were run']
            if bad:
                raise launcher.LaunchRefused(f'calldata failed checks: {bad}')
            if fields.get('proof') != self.expected:
                raise launcher.LaunchRefused('proof in the calldata differs from the rehearsal')
            if not isinstance(data_hex, str) or not data_hex.lower().startswith(launcher.SELECTOR):
                raise launcher.LaunchRefused('no decoded launch calldata to sign')
            # sign exactly the bytes that were decoded and checked, and only if they ARE the request's bytes
            req = tx.get('data') if tx.get('data') is not None else tx.get('input')
            if hex_bytes(str(req)) != hex_bytes(data_hex) or (
                    tx.get('input') is not None and hex_bytes(str(tx['input'])) != hex_bytes(data_hex)):
                raise launcher.LaunchRefused('the request calldata is not the calldata that was decoded')
            data = '0x' + hex_bytes(data_hex).hex()
            call = {'from': self.creator, 'to': launcher.FACTORY, 'data': data,
                    'value': hex(launcher.LAUNCH_FEE_WEI)}
            nonce = int(launcher.rpc_ok('eth_getTransactionCount', [self.creator, 'pending']), 16)
            if nonce != 0:
                raise launcher.LaunchRefused(f'wallet nonce is {nonce}, not 0')
            res, e = launcher.rpc('eth_call', [call, 'latest'])      # real balance, no override
            if e or not res or len(res) < 130:
                raise launcher.LaunchRefused(f'simulation of the pons calldata failed: {e or res!r}')
            g, e = launcher.rpc('eth_estimateGas', [call])
            if e or not g or int(g, 16) > self.pre['gas']:
                raise launcher.LaunchRefused(f"gas estimate {g} exceeds the preflight limit {self.pre['gas']} ({e})")
            # the fee cap was fixed when the launch was armed; the rat's Confirm can come much later. Re-check it
            # against the gas price NOW, and the balance against the worst case, before anything is signed.
            gp = int(launcher.rpc_ok('eth_gasPrice', []), 16)
            if gp * SIGN_FEE_HEADROOM > self.pre['max_fee']:
                raise launcher.LaunchRefused(
                    f"the gas price rose to {gp / 1e9:.4f} gwei since the launch was armed: the preflight maxFeePerGas "
                    f"{self.pre['max_fee'] / 1e9:.4f} gwei no longer has {SIGN_FEE_HEADROOM}x headroom (a signed tx "
                    'could be rejected and strand the wallet). Nothing was signed: `python launcher.py '
                    '--clear-unsigned`, then start the rig again')
            bal = int(launcher.rpc_ok('eth_getBalance', [self.creator, 'latest']), 16)
            worst = self.pre['gas'] * self.pre['max_fee'] + launcher.LAUNCH_FEE_WEI
            if bal < worst:
                raise launcher.LaunchRefused(f'balance {bal / 1e18:.6f} ETH < {worst / 1e18:.6f} ETH '
                                             '(gas limit x maxFeePerGas + the 0.0005 fee)')
        except Exception as ex:
            reason = str(ex) if isinstance(ex, launcher.LaunchRefused) else f'pre-sign step failed: {ex!r}'
            self._journal_abort(reason[:400], fields=fields)
            raise launcher.LaunchRefused(reason[:400]) from None
        body = {'chainId': CHAIN_ID, 'nonce': 0, 'to': launcher.FACTORY, 'value': launcher.LAUNCH_FEE_WEI,
                'data': data, 'gas': self.pre['gas'], 'maxFeePerGas': self.pre['max_fee'],
                'maxPriorityFeePerGas': 0, 'type': 2}
        signed = self.acct.sign_transaction(body)
        raw = hex0x(signed.raw_transaction)
        txh = hex0x(signed.hash)
        # write-ahead: the exact signed bytes are on disk before they leave this machine
        launcher.write_journal({'state': 'signed', 'tx': txh, 'raw': raw, 'nonce': 0, 'creator': self.creator,
                                'proof': self.expected, 'description': self.description,
                                'at': time.time(), 'via': self.via})
        if self.on_signed:
            try:
                self.on_signed(txh)
            except Exception:
                pass
        try:
            _, e = launcher.rpc('eth_sendRawTransaction', [raw], all_rpcs_on_error=True)
        except Exception as ex:        # every RPC timed out: it may still have landed; finish() polls
            e = f'send raised {ex!r}'
        send_error = str(e) if e else None
        self.sent = {'tx': txh, 'raw': raw, 'send_error': send_error, 'broadcast': broadcast_state(send_error)}
        if send_error and self.sent['broadcast'] is not True:
            self.log(f'send error: {send_error}; polling for it anyway')
        else:
            self.log(f'launch tx sent {txh}')
        return txh, send_error

    def finish(self, seconds=120):
        """Blocking: poll + classify the receipt (up to `seconds`), journal the result (launcher.poll / classify)."""
        if not self.sent:
            return {'mode': 'not_sent'}
        out = launcher.poll(self.sent['tx'], self.creator, seconds=seconds, log=self.log)
        if self.sent.get('send_error'):
            out['send_error'] = self.sent['send_error']
        out['description'] = self.description
        launcher.write_journal({'state': out['mode'], **out, 'raw': self.sent['raw'], 'proof': self.expected})
        self.finished = out
        return out


# --------------------------------------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------------------------------------
class PonsBot:
    """Drives the real pons create page. log(msg) is a sync callback (the rig's viewer log).
    on_send(tx) is an async callback set by the rig; it returns {'result': hash} or {'error': {...}}."""

    def __init__(self, acct, mode='DRY', log=None, headful=False, size=PAGE_SIZE, theme='dark',
                 page_cursor=False, symbol=None):
        self.acct = acct
        self.address = acct.address
        self.addr_l = acct.address.lower()
        self.mode = mode
        self.dry = mode != 'LIVE'
        self.symbol = symbol             # pons's review dialog must say "Launch <symbol>" (CONFIRM_JS)
        self.refusals = []               # every wallet call the rig refused (frame, method, signature policy)
        self._refusal_counts = {}
        self.log = log or (lambda m: None)
        self.headful = headful
        self.size = tuple(size)
        self.theme = theme
        self.on_send = None
        self.armed = False
        self.pw = self.browser = self.ctx = self.page = None
        self.cur = (self.size[0] * 0.5, self.size[1] * 0.4)
        self.signatures = []
        self.balance_overrides = 0
        self.ipfs = None
        self.target = None
        self.page_errors = []
        self.page_cursor = page_cursor   # draw a cursor INTO the page (dev shots); the viewer draws its own
        self.on_cursor = None            # sync callback (x, y, click, who): page pixels, for the viewer
        self.who = 'script'

    # ---- lifecycle -------------------------------------------------------------------------------------
    async def start(self):
        from playwright.async_api import async_playwright
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=not self.headful)
        self.ctx = await self.browser.new_context(
            viewport={'width': self.size[0], 'height': self.size[1]}, device_scale_factor=1,
            color_scheme='dark' if self.theme == 'dark' else 'light')
        self.page = await self.ctx.new_page()
        self.page.on('pageerror', lambda e: self.page_errors.append(str(e)[:200]))
        # expose_binding (not expose_function): each call says which frame made it, and _guarded refuses
        # anything but the main frame on pons (Playwright installs bindings in every frame, iframes included)
        for name, fn in (('__ratEthSign', self._eth_sign), ('__ratEthSignTyped', self._eth_sign_typed),
                         ('__ratEthRpc', self._eth_rpc), ('__ratEthSend', self._eth_send)):
            await self.page.expose_binding(name, self._guarded(name, fn))
        await self.page.add_init_script(PROVIDER_JS.replace('__ADDR__', self.address)
                                        .replace('__CHAIN_HEX__', hex(CHAIN_ID))
                                        .replace('__ORIGIN__', PONS_ORIGIN))
        if self.page_cursor:
            await self.page.add_init_script(CURSOR_JS)
        if self.dry:
            await self.page.route(self._is_rpc_url, self._route_rpc)
        return self.page

    async def close(self):
        for obj, meth in ((self.browser, 'close'), (self.pw, 'stop')):
            try:
                if obj is not None:
                    await asyncio.wait_for(getattr(obj, meth)(), 15)
            except Exception:
                pass
        self.browser = self.pw = None

    async def goto(self):
        await self.page.goto(URL, wait_until='domcontentloaded', timeout=60000)

    async def settle(self):
        """Wait for the form and the connected wallet; dismiss pons's status strip; set the theme."""
        await self.page.locator(SEL['name']).first.wait_for(state='attached', timeout=45000)
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
            'chain: window.ethereum && window.ethereum.chainId, rig: !!(window.ethereum && window.ethereum.isRatbrainRig)})')
        return {'connected': connected and str(st.get('addr')).lower() == self.addr_l, 'page_wallet': st,
                'theme': theme}

    async def dismiss_notice(self):
        """pons's "Degraded performance" strip: use its own dismiss button, else hide it (flybrain's rule)."""
        try:
            b = self.page.locator('button[aria-label="Dismiss status notice"]').first
            if await b.count() and await b.is_visible():
                await b.click(timeout=2500)
                await self.page.wait_for_timeout(400)
                return True
        except Exception:
            pass
        try:
            await self.page.evaluate("""() => {
                const rx = /degraded performance|we are upgrading/i;
                [...document.querySelectorAll('div,section,aside,header')]
                  .filter(e => rx.test(e.innerText||'') && e.offsetHeight > 0 && e.offsetHeight < 90
                            && !e.querySelector('input,textarea,button[type=submit]'))
                  .forEach(e => e.style.display='none'); }""")
        except Exception:
            pass
        return False

    async def set_theme(self):
        """Measure first, flip only if needed: pons's control is a toggle (flybrain's go_dark lesson)."""
        want_dark = self.theme == 'dark'
        bg_js = '() => getComputedStyle(document.body).backgroundColor'

        def is_dark(css):
            n = re.findall('[0-9.]+', css or '')
            if len(n) < 3:
                return False
            r, g, b = (float(x) for x in n[:3])
            return (0.2126 * r + 0.7152 * g + 0.0722 * b) < 110
        try:
            if is_dark(await self.page.evaluate(bg_js)) == want_dark:
                return 'dark' if want_dark else 'light'
            label = 'dark mode' if want_dark else 'light mode'
            b = self.page.locator(f'button[aria-label*="{label}" i]').first
            if await b.count():
                await b.click(timeout=3000)
                await self.page.wait_for_timeout(900)
            now = is_dark(await self.page.evaluate(bg_js))
            return ('dark' if now else 'light')
        except Exception:
            return 'unknown'

    # ---- motion ----------------------------------------------------------------------------------------
    def _cursor(self, x, y, click=False):
        if self.on_cursor:
            try:
                self.on_cursor(x, y, click, self.who)
            except Exception:
                pass

    async def mouse_click(self, x, y):
        await self.page.mouse.click(x, y)
        self._cursor(x, y, True)

    async def cursor_mode(self, mode):
        self.who = 'rat' if mode == 'rat' else 'script'
        try:
            await self.page.evaluate('(m) => window.__ratCursorMode && window.__ratCursorMode(m)', mode)
        except Exception:
            pass

    async def glide(self, x, y, dur=None, steps=None, hold=0.0):
        """Ease the mouse there over real time (Playwright's move teleports; flybrain's glide)."""
        x0, y0 = self.cur
        dist = math.hypot(x - x0, y - y0)
        steps = steps or int(max(10, min(34, dist / 16)))
        dur = dur if dur is not None else max(0.3, min(0.85, dist / 850))
        for i in range(1, steps + 1):
            t = i / steps
            t = t * t * (3 - 2 * t)
            await self.page.mouse.move(x0 + (x - x0) * t, y0 + (y - y0) * t)
            self._cursor(x0 + (x - x0) * t, y0 + (y - y0) * t)
            await asyncio.sleep(dur / steps)
        self.cur = (x, y)
        if hold:
            await asyncio.sleep(hold)

    async def smooth_scroll(self, target_y, ms=900, steps=24):
        start = await self.page.evaluate('window.scrollY')
        target_y = max(0.0, float(target_y))
        if abs(target_y - start) < 4:
            return
        for i in range(1, steps + 1):
            t = i / steps
            t = t * t * (3 - 2 * t)
            await self.page.evaluate(f'window.scrollTo(0, {start + (target_y - start) * t})')
            await asyncio.sleep(ms / 1000.0 / steps)

    async def reveal(self, loc, frac=0.42, margin_top=110, margin_bottom=110):
        """Scroll visibly until the element sits well inside the viewport (never teleport)."""
        top = await loc.evaluate('e => e.getBoundingClientRect().top')
        h = await loc.evaluate('e => e.getBoundingClientRect().height')
        vh = self.size[1]
        if top >= margin_top and top + h <= vh - margin_bottom:
            return
        y = await self.page.evaluate('window.scrollY')
        await self.smooth_scroll(y + top - vh * frac)
        await asyncio.sleep(0.25)

    async def click_box(self, bb, fx=0.5, fy=0.5, hold=0.3):
        x, y = bb['x'] + bb['width'] * fx, bb['y'] + bb['height'] * fy
        await self.glide(x, y, hold=hold)
        await self.mouse_click(x, y)
        return x, y

    # ---- the stages ------------------------------------------------------------------------------------
    async def terms_shown(self):
        try:
            return bool(await self.page.evaluate('() => /review and accept/i.test(document.body.innerText)'))
        except Exception:
            return False

    async def accept_terms(self, wait_s=15, before_accept=None):
        """pons's Terms of Use / Privacy Policy / jurisdiction gate. The owner accepted these terms and
        authorized the rig to click them (SPEC hard rules)."""
        t_end = time.time() + wait_s
        while time.time() < t_end and not await self.terms_shown():
            await asyncio.sleep(0.3)
        if not await self.terms_shown():
            return {'shown': False}
        await asyncio.sleep(0.9)
        boxes = self.page.locator('input[type="checkbox"]')
        ticked = 0
        for i in range(await boxes.count()):
            b = boxes.nth(i)
            try:
                if not await b.is_visible() or await b.is_checked():
                    continue
                bb = await b.bounding_box()
                if not bb:
                    continue
                await self.click_box(bb, hold=0.35)
                await asyncio.sleep(0.35)
                if not await b.is_checked():
                    await b.check(timeout=2500, force=True)
                ticked += 1
            except Exception as e:
                self.log(f'terms checkbox {i}: {str(e)[:90]}')
        btn = self.page.get_by_role('button', name=re.compile(r'^\s*accept and continue\s*$', re.I)).first
        await btn.wait_for(state='visible', timeout=6000)
        for _ in range(30):
            if await btn.is_enabled():
                break
            await asyncio.sleep(0.2)
        if not await btn.is_enabled():
            raise PonsError('"Accept and continue" stayed disabled after ticking the boxes')
        if before_accept:
            await before_accept()
        await self.click_box(await btn.bounding_box(), hold=0.45)
        for _ in range(48):
            await asyncio.sleep(0.25)
            if not await self.terms_shown():
                return {'shown': True, 'accepted': True, 'ticked': ticked}
        raise PonsError('the terms dialog did not close after "Accept and continue"')

    async def upload_image(self, path):
        """Click pons's "Choose image" and pick the file through the page's own file chooser. pons pins it
        (POST /api/ipfs/image) and then says "Image ready"."""
        path = str(Path(path).resolve())
        zone = self.page.get_by_text('Choose image', exact=False).first
        await zone.wait_for(state='visible', timeout=15000)
        await self.reveal(zone)
        bb = await zone.bounding_box()
        x, y = bb['x'] + bb['width'] / 2, bb['y'] + bb['height'] / 2
        await self.glide(x, y, hold=0.45)
        return await self.pick_image_at(x, y, path)

    async def pick_image_at(self, x, y, path, click=None):
        """Click (x, y), a point inside pons's "Choose image" zone, and pick the file through the file chooser
        that click opens (else the page's file input). Captures pons's /api/ipfs/image response (the pinned
        URI) and waits for "Image ready". The brain rig calls this with the rat's click pixel and its own
        click coroutine (click(x, y); default: mouse_click)."""
        path = str(Path(path).resolve())
        how = 'file chooser'
        try:
            async with self.page.expect_response(lambda r: '/api/ipfs/image' in r.url, timeout=60000) as resp_info:
                try:
                    async with self.page.expect_file_chooser(timeout=6000) as fc_info:
                        await (click or self.mouse_click)(x, y)
                    fc = await fc_info.value
                    await fc.set_files(path)
                except Exception:
                    how = 'file input'
                    await self.page.locator('input[type="file"]').first.set_input_files(path, timeout=15000)
            resp = await resp_info.value
            body = await resp.text()
            self.ipfs = {'status': resp.status, 'body': body[:4000]}
            # the URI pons pinned: its launch calldata must carry exactly this image (check_launch 'image')
            try:
                j = json.loads(body)
                uri = (j.get('uri') or (f"ipfs://{j['cid']}" if j.get('cid') else None)) if isinstance(j, dict) else None
            except (ValueError, TypeError):
                uri = None
            self.ipfs['uri'] = uri if isinstance(uri, str) and launcher.IMAGE_RE.match(uri) else None
            if uri and not self.ipfs['uri']:
                self.ipfs['uri_rejected'] = str(uri)[:200]
        except Exception as e:
            self.ipfs = {'error': str(e)[:160], 'uri': None}
        for _ in range(120):
            if await self.page.get_by_text('Image ready', exact=False).count():
                return {'how': how, 'ready': True, 'ipfs': self.ipfs, 'uri': self.ipfs.get('uri')}
            await asyncio.sleep(0.25)
        raise PonsError(f'pons never said "Image ready" (upload: {self.ipfs})')

    async def type_field(self, key, text, delay=55):
        loc = self.page.locator(SEL[key]).first
        await loc.wait_for(state='visible', timeout=15000)
        await self.reveal(loc)
        bb = await loc.bounding_box()
        await self.click_box(bb, fx=min(0.3, 110 / max(bb['width'], 1)), hold=0.25)
        await self.page.keyboard.press('Control+A')
        await self.page.keyboard.press('Delete')
        await self.page.keyboard.type(text, delay=delay)
        await asyncio.sleep(0.35)
        val = await loc.input_value()
        if val != text:   # React can drop a keystroke under load: set it once more, still through the input
            await loc.fill(text, timeout=5000)
            await asyncio.sleep(0.35)
            val = await loc.input_value()
        return val

    async def read_field(self, key):
        return await self.page.locator(SEL[key]).first.input_value(timeout=5000)

    async def set_creator_tax(self, pct):
        """Open Advanced and type the creator tax percent (pons caps it at 10)."""
        adv = self.page.get_by_role('button', name=re.compile(r'^\s*advanced\s*$', re.I)).first
        await adv.wait_for(state='visible', timeout=10000)
        if (await adv.get_attribute('aria-expanded')) != 'true':
            await self.reveal(adv, frac=0.3)
            await self.click_box(await adv.bounding_box(), fx=0.2, hold=0.35)
            for _ in range(20):
                await asyncio.sleep(0.15)
                if (await adv.get_attribute('aria-expanded')) == 'true':
                    break
            await asyncio.sleep(0.6)
        tax = self.page.locator(TAX_SEL).first
        await tax.wait_for(state='visible', timeout=8000)
        await self.reveal(tax, frac=0.5)
        await self.click_box(await tax.bounding_box(), fx=0.15, hold=0.3)
        await self.page.keyboard.press('Control+A')
        await self.page.keyboard.press('Delete')
        await self.page.keyboard.type(str(pct), delay=160)
        await asyncio.sleep(0.5)
        return await tax.input_value()

    async def launch_button(self):
        return await self.page.evaluate(LAUNCH_BTN_JS)

    async def confirm_box(self, wait_s=0.0):
        """pons's review Confirm, measured now: only inside a dialog that says "Launch <symbol>"."""
        if not self.symbol:
            raise PonsError('no coin symbol: the review dialog cannot be identified')
        t_end = time.time() + wait_s
        while True:
            c = await self.page.evaluate(CONFIRM_JS, self.symbol)
            if c or time.time() >= t_end:
                return c
            await asyncio.sleep(0.2)

    async def open_review(self):
        """Operator script: press pons's "Launch token", which opens pons's launch review. The rat's lever
        press later clicks that review's Confirm, which makes pons request the launch transaction."""
        b = await self.launch_button()
        if not b:
            raise PonsError('launch button not found')
        vh = self.size[1]
        y = await self.page.evaluate('window.scrollY')
        await self.smooth_scroll(y + b['y'] - vh * 0.55)
        await asyncio.sleep(0.5)
        for _ in range(40):                       # pons re-reads the balance; give the label time to settle
            b = await self.launch_button()
            if b and not b['disabled'] and re.search(r'launch', b['label'], re.I):
                break
            await asyncio.sleep(0.25)
        if not b or b['disabled'] or not re.search(r'launch', b['label'], re.I):
            raise PonsError(f"pons's button says {b and b['label']!r} (disabled={b and b['disabled']})")
        await self.glide(b['x'] + b['w'] / 2, b['y'] + b['h'] / 2, hold=0.6)
        await self.mouse_click(b['x'] + b['w'] / 2, b['y'] + b['h'] / 2)
        c = await self.confirm_box(wait_s=10)
        if not c:
            raise PonsError(f'pons did not open a review dialog saying "Launch {self.symbol}" with a Confirm')
        if c['disabled']:
            raise PonsError("the review dialog's Confirm is disabled")
        self.target = c
        return {'launch_label': b['label'], 'target': c['label'], 'title': c['title'], 'dialog': c['dialog']}

    async def park(self):
        """Rest the cursor inside the dialog, clear of Confirm, so the press's glide is short and visible."""
        c = self.target
        x = min(self.size[0] - 40, c['x'] + c['w'] / 2 + 190)
        y = max(60, c['y'] + c['h'] / 2 - 95)
        await self.glide(x, y, hold=0.2)

    def arm(self):
        self.armed = True

    async def click_launch(self):
        """The press's click: glide to pons's Confirm and click it. The Confirm must be measured NOW, inside
        a dialog that says "Launch <symbol>", enabled and not covered; it is measured again at the end of the
        glide and must not have moved. Otherwise PonsError and NO click (never stale coordinates)."""
        c = await self.confirm_box(wait_s=1.0)
        if not c:
            raise PonsError(f'no Confirm inside a dialog saying "Launch {self.symbol}" on the page: not clicked')
        if c['disabled'] or not c['onTop']:
            raise PonsError(f"pons's Confirm is {'disabled' if c['disabled'] else 'covered'}: not clicked")
        await self.cursor_mode('rat')
        x, y = c['x'] + c['w'] / 2, c['y'] + c['h'] / 2
        await self.glide(x, y, dur=0.42, steps=14)
        c2 = await self.confirm_box()
        if (not c2 or c2['disabled'] or not c2['onTop']
                or abs(c2['x'] + c2['w'] / 2 - x) > 2 or abs(c2['y'] + c2['h'] / 2 - y) > 2):
            raise PonsError("pons's Confirm moved, closed or was covered during the glide: not clicked")
        await self.mouse_click(x, y)
        return {'label': c2['label'], 'dialog_title': c2['title'], 'x': round(x), 'y': round(y), 'at': time.time()}

    async def wait_rejection(self, wait_s=8.0):
        """After the 4001, pons shows a toast ("Launch failed / You cancelled in your wallet.")."""
        t_end = time.time() + wait_s
        js = """(rx) => { const L = document.body.innerText.split(String.fromCharCode(10))
                   .map(s => s.trim()).filter(Boolean);
                 const i = L.findIndex(l => new RegExp(rx, 'i').test(l));
                 return i < 0 ? null : L.slice(Math.max(0, i - 1), i + 1).join(' / '); }"""
        while time.time() < t_end:
            try:
                hit = await self.page.evaluate(js, REJECTION_RE)
            except Exception:
                hit = None
            if hit:
                return hit
            await asyncio.sleep(0.15)
        return None

    async def jpeg(self, quality=60):
        return await self.page.screenshot(type='jpeg', quality=quality)

    # ---- wallet handlers (run on the rig's event loop) -------------------------------------------------
    def _override(self, params):
        """Add {our address: 1 ETH} to an eth_call / eth_estimateGas state override (DRY only)."""
        p = list(params or [])
        if not p:
            return p
        if len(p) == 1:
            p.append('latest')
        if len(p) == 2:
            p.append({})
        if isinstance(p[2], dict):
            ovr = dict(p[2])
            cur = dict(ovr.get(self.address) or {})
            cur['balance'] = hex(DRY_BALANCE_WEI)
            ovr[self.address] = cur
            p[2] = ovr
        return p

    @staticmethod
    def _is_rpc_url(url):
        return '/api/robinhood-rpc' in url or any(h in url for h in RH_RPC_HOSTS)

    async def _route_rpc(self, route):
        """DRY: the page's own chain reads. eth_call / eth_estimateGas get the balance override; an
        eth_getBalance of our address is answered as 1 ETH. Everything else passes through untouched."""
        req = route.request
        try:
            body = json.loads(req.post_data or 'null') if req.method == 'POST' else None
        except Exception:
            body = None
        if body is None:
            return await route.continue_()
        batch = body if isinstance(body, list) else [body]
        changed, bal_ids = False, set()
        for c in batch:
            if not isinstance(c, dict):
                continue
            m = c.get('method')
            if m in ('eth_call', 'eth_estimateGas'):
                c['params'] = self._override(c.get('params'))
                changed = True
            elif m == "eth_getBalance" and str((c.get("params") or [""])[0]).lower() == self.addr_l:
                bal_ids.add(c.get('id'))
        if not changed and not bal_ids:
            return await route.continue_()
        try:
            resp = await route.fetch(post_data=json.dumps(body))
            txt = await resp.text()
            if bal_ids:
                j = json.loads(txt)
                for r in (j if isinstance(j, list) else [j]):
                    if isinstance(r, dict) and r.get('id') in bal_ids:
                        r['result'] = hex(DRY_BALANCE_WEI)
                        r.pop('error', None)
                txt = json.dumps(j)
            self.balance_overrides += 1
            await route.fulfill(response=resp, body=txt)
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    def _refused(self, what, why, **extra):
        """Record every refusal; log the first few of each kind to the viewer (a polling page can't flood it)."""
        self.refusals.append({'what': str(what)[:80], 'why': str(why)[:300], 'at': time.time(), **extra})
        n = self._refusal_counts[what] = self._refusal_counts.get(what, 0) + 1
        if n <= 3:
            self.log(f'wallet refused {what}: {why}' + (' (further ones are counted, not logged)' if n == 3 else ''))

    def _frame_ok(self, frame):
        if frame is None or self.page is None or frame is not self.page.main_frame:
            return False, 'the call came from a sub-frame (iframe), not the pons page itself'
        o = origin_of(frame.url)
        if o != PONS_ORIGIN:
            return False, f'the call came from {o or frame.url[:60]!r}, not {PONS_ORIGIN}'
        return True, ''

    def _guarded(self, name, fn):
        """A binding callback that runs fn(*args) only for the main frame on pons (4100 otherwise)."""
        async def cb(source, *args):
            ok, why = self._frame_ok(source.get('frame') if isinstance(source, dict) else None)
            if not ok:
                self._refused(name, why)
                return err(UNAUTHORIZED, f'rat rig wallet: {why}')
            return await fn(*args)
        return cb

    async def _eth_rpc(self, method, params):
        if method not in READ_METHODS:
            self._refused(str(method), 'not a read-only method; the rig wallet forwards reads only')
            return err(UNSUPPORTED, f'the rig wallet does not forward {method}')
        params = list(params or [])
        if self.dry and method in ('eth_call', 'eth_estimateGas'):
            params = self._override(params)
        loop = asyncio.get_running_loop()
        try:
            res, e = await loop.run_in_executor(None, launcher.rpc, method, params)
        except Exception as ex:
            return err(-32603, f'rpc failed: {ex!r}'[:200])
        if e is not None:
            code = e.get('code', -32000) if isinstance(e, dict) else -32000
            msg = e.get('message', str(e)) if isinstance(e, dict) else str(e)
            out = err(code, msg)
            if isinstance(e, dict) and e.get('data') is not None:
                out['error']['data'] = e['data']
            return out
        if self.dry and method == 'eth_getBalance' and params and str(params[0]).lower() == self.addr_l:
            res = hex(DRY_BALANCE_WEI)
        return {'result': res}

    async def _eth_sign(self, method, params):
        params = list(params or [])
        if method == 'eth_sign':
            # eth_sign signs a raw 32-byte hash, which can be a transaction: never
            self.signatures.append({'method': method, 'refused': True, 'at': time.time()})
            self._refused(method, 'eth_sign signs a raw hash: never')
            return refusal('rat rig never signs raw hashes')
        if method != 'personal_sign':
            self._refused(str(method), 'not a signing method this wallet supports')
            return err(UNSUPPORTED, f'the rig wallet does not support {method}')
        raw, who = (params[0] if params else ''), (params[1] if len(params) > 1 else None)
        if len(params) > 1 and str(params[0]).lower() == self.addr_l:   # some dapps swap the order
            raw, who = params[1], params[0]
        if who is not None and str(who).lower() != self.addr_l:
            self.signatures.append({'method': method, 'refused': True, 'why': 'other address', 'at': time.time()})
            self._refused(method, f'asked to sign for {str(who)[:44]}, not this wallet')
            return refusal('rat rig: personal_sign for a different address')
        try:
            data = hex_bytes(raw) if isinstance(raw, str) and raw[:2].lower() == '0x' else str(raw).encode()
        except ValueError:
            data = str(raw).encode()
        text = data.decode('utf-8', 'replace')
        preview = text[:140]
        if not self.dry:
            ok, why = siwe_login_ok(text, self.address)
            if not ok:
                self.signatures.append({'method': method, 'message': preview, 'refused': True, 'why': why,
                                        'at': time.time()})
                self._refused(method, f'LIVE signs only a fresh pons sign-in: {why}')
                return refusal(f'rat rig refuses to sign in LIVE: {why}'[:200])
        key = 'throwaway in-memory key (DRY)' if self.dry else 'funded key (LIVE, pons sign-in only)'
        self.log(f'pons asked for a login signature (personal_sign, {len(data)} bytes): {preview!r}; '
                 f'signed with the {key}')
        self.signatures.append({'method': method, 'message': preview, 'key': key, 'at': time.time()})
        sig = self.acct.sign_message(encode_defunct(primitive=data))
        return {'result': hex0x(sig.signature)}

    async def _eth_sign_typed(self, method, params):
        if method not in TYPED_METHODS:
            self._refused(str(method), 'not a typed-data method')
            return err(UNSUPPORTED, f'the rig wallet does not support {method}')
        if not self.dry:
            # typed data can authorise moving funds without a transaction (permit, EIP-3009
            # ReceiveWithAuthorization, orders, delegations...): the funded key signs none of it
            self.signatures.append({'method': method, 'refused': True, 'at': time.time()})
            self._refused(method, 'LIVE never signs typed data with the funded key')
            return refusal('rat rig never signs typed data in LIVE')
        params = list(params or [])
        payload = params[1] if len(params) > 1 else (params[0] if params else {})
        try:
            if isinstance(payload, str):
                payload = json.loads(payload)
            primary = str(payload.get('primaryType', ''))
        except (ValueError, AttributeError):
            self._refused(method, 'typed data that does not parse')
            return err(-32602, 'rat rig: typed data does not parse')
        key = 'throwaway in-memory key (DRY)'
        self.log(f'pons asked to sign typed data ({primary}); signed with the {key}')
        self.signatures.append({'method': method, 'primaryType': primary, 'key': key, 'at': time.time()})
        sig = self.acct.sign_message(encode_typed_data(full_message=payload))
        return {'result': hex0x(sig.signature)}

    async def _eth_send(self, tx):
        if self.on_send is None:
            return refusal('rat rig: no send handler')
        try:
            return await self.on_send(dict(tx or {}))
        except Exception as e:
            self.log(f'send handler failed: {e!r}'[:200])
            return refusal(DRY_REFUSAL if self.dry else f'rat rig refused: {e}'[:200])


def throwaway_account():
    """DRY wallet: a fresh in-memory key per run. Never written anywhere; it never holds anything."""
    return Account.create()
