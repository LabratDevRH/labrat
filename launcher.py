"""Launch a coin on pons (Robinhood Chain, 4663) from a script.

Layout of the pons v2 factory call was read off a real launch ($FLYBRAIN, tx 0x63b2164f...) and
confirmed by re-encoding it byte for byte:
    0xa72101af( (name, symbol, image, description, (x, telegram, website, s4, s5), creator,
                 creatorTaxBps, uint256 0, bytes32 0, bytes32 0), uint256 0, pairToken, bytes "" )
value = 0.0005 ETH launch fee. pairToken 0x0 = ETH-quoted. With zero bytes32 fields the call simulates fine;
random bytes32 revert. pons's own UI (captured by the live rig, 2026-09-24) fills them: the first is a
constant 0xa9fc75d4..., the second changes per launch, so they are pons-issued values, not free fields.
The factory emits TokenLaunched(token indexed, curve indexed, creator indexed, ...) (checked on that receipt).

SAFETY MODEL (nothing is ever sent unless ALL of these hold):
  1. the caller passes live=True (only `launch_run.py --live` does; this file's CLI never can),
  2. .env (the file, not the shell environment) says RATBRAIN_LIVE=1 and holds RATBRAIN_RH_KEY,
  3. preflight() passed: valid image, fresh wallet (nonce 0), enough balance for gas*maxFee + fee,
     simulation succeeds, and no journal exists (created exclusively, so a second launch is impossible),
  4. the proof is a real 64-hex sha256 (never the all-zero test value).
The signed transaction is pinned to nonce 0 of a fresh wallet, so at most ONE launch can ever be mined
from it, whatever happens locally. The raw signed tx is journalled before broadcast; resolve() only ever
re-broadcasts that same raw tx and never re-signs.
"""
import json, os, re, sys, time
import requests
from eth_abi import encode, decode
from eth_account import Account
from eth_utils import to_checksum_address, keccak

HERE = os.path.dirname(os.path.abspath(__file__))
FACTORY = to_checksum_address('0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e')
SELECTOR = '0xa72101af'
LAUNCH_FEE_WEI = 500_000_000_000_000
TYPES = ['(string,string,string,string,(string,string,string,string,string),address,uint256,uint256,bytes32,bytes32)',
         'uint256', 'address', 'bytes']
TOKEN_LAUNCHED_TOPIC = '0x' + keccak(text='TokenLaunched(address,address,address,address,uint256,uint256)').hex()
ZERO = '0x0000000000000000000000000000000000000000'
RPCS = ['https://rpc.mainnet.chain.robinhood.com', 'https://robinhood-rpc.publicnode.com']
JOURNAL = os.path.join(HERE, 'launch_journal.json')
EXPLORER = 'https://robinhoodchain.blockscout.com'
GAS_MULT, FEE_MULT = 1.3, 2          # gas limit headroom, maxFeePerGas headroom over eth_gasPrice
DRY_CREATOR = to_checksum_address('0x' + '11' * 20)
IMAGE_RE = re.compile(r'^ipfs://(Qm[1-9A-HJ-NP-Za-km-z]{44}|b[a-z2-7]{58,})$')
IMAGE_HOSTS = ('https://dd.dexscreener.com/ds-data/', 'https://cdn.dexscreener.com/', 'https://axiomtrading-v2.axiom-cdn.io/')
PROOF_RE = re.compile(r'^[0-9a-f]{64}$')
POSSIBLY_SENT = ('already known', 'nonce too low', 'known transaction', 'replacement transaction underpriced')


class LaunchRefused(RuntimeError):
    pass


def read_env_file():
    """.env is authoritative for RATBRAIN_*: the shell environment cannot switch LIVE on or supply the key.
    Any failure to read it is a LaunchRefused that carries only the exception's TYPE: never its text or repr (a
    UnicodeDecodeError's repr holds the raw bytes it was decoding, i.e. the whole file, key line included). A UTF-8
    byte-order mark is tolerated (Windows PowerShell's `Out-File -Encoding utf8` writes one)."""
    vals = {}
    p = os.path.join(HERE, '.env')
    if os.path.exists(p):
        try:
            with open(p, encoding='utf-8-sig', errors='strict') as fh:
                lines = fh.read().splitlines()
        except Exception as e:
            raise LaunchRefused(f'.env could not be read ({type(e).__name__}): save it as plain UTF-8 text') from None
        for line in lines:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    for k in ('RATBRAIN_LIVE', 'RATBRAIN_RH_KEY'):
        if k in os.environ and os.environ[k] != vals.get(k, os.environ[k]):
            raise LaunchRefused(f'{k} in the shell environment differs from .env; unset it')
    return vals


def rpc(method, params, all_rpcs_on_error=False):
    """-> (result, error). Network failures back off and move to the next RPC.
    With all_rpcs_on_error, a JSON-RPC error also moves on (used for sendRawTransaction)."""
    last, last_err = None, None
    for url in ([os.environ['RATBRAIN_RPC']] if os.environ.get('RATBRAIN_RPC') else []) + RPCS:
        for attempt in range(3):
            try:
                j = requests.post(url, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params},
                                  timeout=30).json()
            except Exception as e:  # 429s / timeouts
                last = e; time.sleep(1 + attempt); continue
            if 'error' in j:
                last_err = j['error']
                if all_rpcs_on_error:
                    break          # try the next RPC
                return None, j['error']
            return j['result'], None
    if last_err is not None:
        return None, last_err
    raise RuntimeError(f'all RPCs failed: {last}')


def rpc_ok(method, params):
    res, err = rpc(method, params)
    if err is not None or res is None:
        raise LaunchRefused(f'{method} failed: {err}')
    return res


def config():
    env = read_env_file()
    g = lambda k, d='': env.get(k, os.environ.get(k, d)) if k not in ('RATBRAIN_LIVE', 'RATBRAIN_RH_KEY') else env.get(k, d)
    c = {
        'name': g('RATBRAIN_NAME', 'ratbrain'),
        'symbol': g('RATBRAIN_SYMBOL', 'RATBRAIN'),
        'image': g('RATBRAIN_IMAGE', ''),
        'x': g('RATBRAIN_X', ''),
        'website': g('RATBRAIN_WEBSITE', ''),
        'tax_bps': int(g('RATBRAIN_TAX_BPS', '100')),
        'live_env': g('RATBRAIN_LIVE') == '1',
        '_key': g('RATBRAIN_RH_KEY', ''),
    }
    # pons's own form rules: name letters/digits/spaces <= 32, symbol A-Z0-9 <= 10
    if not (0 < len(c['name']) <= 32 and all(ch.isalnum() or ch == ' ' for ch in c['name'])):
        raise LaunchRefused('bad name (letters, digits, spaces, <= 32)')
    if not (0 < len(c['symbol']) <= 10 and c['symbol'].isalnum() and c['symbol'].upper() == c['symbol']):
        raise LaunchRefused('bad symbol (A-Z 0-9, <= 10)')
    return c


def public(c):
    return {k: v for k, v in c.items() if not k.startswith('_')}


def description(proof):
    return f'launched by a virtual rat pressing a lever. replay proof sha256 {proof}'


def calldata(c, creator, proof):
    params = (c['name'], c['symbol'], c['image'], description(proof), (c['x'], '', c['website'], '', ''),
              creator, c['tax_bps'], 0, b'\0' * 32, b'\0' * 32)
    return SELECTOR + encode(TYPES, [params, 0, ZERO, b'']).hex()


def decode_description(input_hex):
    """Description string from a launch tx's calldata (used by replay.py to check the on-chain proof)."""
    assert input_hex[:10].lower() == SELECTOR
    return decode(TYPES, bytes.fromhex(input_hex[10:]))[0][3]


def simulate(c, creator, proof, fake_balance):
    call = {'from': creator, 'to': FACTORY, 'data': calldata(c, creator, proof), 'value': hex(LAUNCH_FEE_WEI)}
    args = [call, 'latest']
    if fake_balance:  # dry runs without a funded wallet: pretend it holds 1 ETH
        args.append({creator: {'balance': hex(10 ** 18)}})
    res, err = rpc('eth_call', args)
    if err or not res or len(res) < 130:
        return None, err or f'bad eth_call result {res!r}'
    token, curve = decode(['address', 'address'], bytes.fromhex(res[2:]))
    return {'token': to_checksum_address(token), 'curve': to_checksum_address(curve)}, None


def check_proof(proof):
    if not PROOF_RE.match(proof or '') or len(set(proof)) < 8:
        raise LaunchRefused('proof must be a real sha256 from rollout.run (64 lowercase hex, not a test value)')


def image_ok(img):
    return bool(IMAGE_RE.match(img)) or any(img.startswith(h) and len(img) > len(h) for h in IMAGE_HOSTS)


def preflight(c, proof_for_sim):
    """Everything that can be checked BEFORE the rat runs. Raises LaunchRefused on any problem.
    Returns the funded account plus the gas figures that will be used."""
    if not c['live_env']:
        raise LaunchRefused('.env does not say RATBRAIN_LIVE=1')
    if not c['_key']:
        raise LaunchRefused('.env has no RATBRAIN_RH_KEY')
    try:
        acct = Account.from_key(c['_key'])
    except Exception:
        raise LaunchRefused('RATBRAIN_RH_KEY does not parse as a private key') from None
    if not image_ok(c['image']):
        raise LaunchRefused(f"RATBRAIN_IMAGE must be ipfs://<CID> or an allow-listed CDN URL, got {c['image']!r}")
    if os.path.exists(JOURNAL):
        raise LaunchRefused(f'{JOURNAL} exists: a launch was already attempted. Run `python launcher.py --resolve`.')
    nonce = int(rpc_ok('eth_getTransactionCount', [acct.address, 'pending']), 16)
    if nonce != 0:
        raise LaunchRefused(f'wallet {acct.address} has nonce {nonce}; use a FRESH wallet (the launch is pinned to nonce 0)')
    sim, err = simulate(c, acct.address, proof_for_sim, fake_balance=False)
    if err:
        raise LaunchRefused(f'simulation failed: {err}')
    gas = int(int(rpc_ok('eth_estimateGas', [{'from': acct.address, 'to': FACTORY,
                                                'data': calldata(c, acct.address, proof_for_sim),
                                                'value': hex(LAUNCH_FEE_WEI)}]), 16) * GAS_MULT)
    max_fee = int(int(rpc_ok('eth_gasPrice', []), 16) * FEE_MULT)
    need = gas * max_fee + LAUNCH_FEE_WEI
    bal = int(rpc_ok('eth_getBalance', [acct.address, 'latest']), 16)
    # 1.5x margin: the gas price can move between now and the press
    if bal < need * 1.5:
        raise LaunchRefused(f'balance {bal / 1e18:.6f} ETH < {need * 1.5 / 1e18:.6f} ETH needed '
                            f'(gas {gas} x maxFee {max_fee / 1e9:.3f} gwei + 0.0005 fee, x1.5 margin)')
    return {'acct': acct, 'creator': acct.address, 'gas': gas, 'max_fee': max_fee, 'balance': bal, 'need': need,
            'predicted_token': sim['token']}


def reserve_journal(info):
    """Exclusive create: two processes can never both get past this."""
    fd = os.open(JOURNAL, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, 'w') as f:
        json.dump({'state': 'reserved', **info}, f, indent=2); f.flush(); os.fsync(f.fileno())


def write_journal(obj):
    tmp = JOURNAL + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(obj, f, indent=2); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, JOURNAL)


def classify(rc, creator):
    """Receipt -> result. LIVE only when status 1 AND the factory's TokenLaunched event names our creator."""
    txh = rc['transactionHash']
    base = {'tx': txh, 'status': int(rc['status'], 16), 'block': int(rc['blockNumber'], 16),
            'creator': creator, 'explorer': f'{EXPLORER}/tx/{txh}'}
    if base['status'] != 1:
        return {'mode': 'reverted', **base}
    ev = [l for l in rc['logs'] if l['address'].lower() == FACTORY.lower() and l['topics'][0] == TOKEN_LAUNCHED_TOPIC
          and len(l['topics']) >= 4 and l['topics'][3][-40:].lower() == creator[2:].lower()]
    if not ev:
        return {'mode': 'live_unverified', **base}
    return {'mode': 'live', 'token': to_checksum_address('0x' + ev[0]['topics'][1][-40:]),
            'curve': to_checksum_address('0x' + ev[0]['topics'][2][-40:]), **base}


def poll(txh, creator, seconds=120, log=print):
    for _ in range(int(seconds / 0.5)):
        rc, _ = rpc('eth_getTransactionReceipt', [txh])
        if rc:
            return classify(rc, creator)
        time.sleep(0.5)
    log(f'no receipt yet for {txh}; run `python launcher.py --resolve` later')
    return {'mode': 'sent_unconfirmed', 'tx': txh, 'creator': creator, 'explorer': f'{EXPLORER}/tx/{txh}'}


def launch(proof, live=False, pre=None, log=print):
    """Called at the instant the rat's press registers. Returns a dict describing what happened.
    live=False (default) is a dry run: eth_call only, with a fake creator and a fake balance."""
    c = config()
    check_proof(proof)
    if not live:
        sim, err = simulate(c, DRY_CREATOR, proof, fake_balance=True)
        if err:
            return {'mode': 'sim_failed', 'error': str(err), 'description': description(proof)}
        return {'mode': 'dry', 'creator': DRY_CREATOR, 'predicted_token': sim['token'],
                'predicted_curve': sim['curve'], 'description': description(proof)}

    if pre is None:
        raise LaunchRefused('live launch without a preflight')
    acct = pre['acct']
    data = calldata(c, acct.address, proof)
    # re-simulate with the real proof and real balance at the moment of the press
    sim, err = simulate(c, acct.address, proof, fake_balance=False)
    if err:
        write_journal({'state': 'aborted_before_sign', 'error': str(err), 'proof': proof})
        return {'mode': 'sim_failed', 'error': str(err), 'description': description(proof)}
    tx = {'chainId': 4663, 'nonce': 0, 'to': FACTORY, 'value': LAUNCH_FEE_WEI, 'data': data,
          'gas': pre['gas'], 'maxFeePerGas': pre['max_fee'], 'maxPriorityFeePerGas': 0, 'type': 2}
    signed = acct.sign_transaction(tx)
    raw = '0x' + signed.raw_transaction.hex().removeprefix('0x')
    txh = '0x' + signed.hash.hex().removeprefix('0x')
    # write-ahead: the exact signed bytes are on disk before they leave this machine
    write_journal({'state': 'signed', 'tx': txh, 'raw': raw, 'nonce': 0, 'creator': acct.address, 'proof': proof,
                   'description': description(proof), 'at': time.time()})
    try:
        _, err = rpc('eth_sendRawTransaction', [raw], all_rpcs_on_error=True)
    except Exception as e:      # every RPC timed out: the tx may still have landed, so poll regardless
        err = f'send raised {e!r}'
    if err and not any(s in str(err).lower() for s in POSSIBLY_SENT):
        log(f'send error: {err}; checking whether it landed anyway')
    else:
        log(f'launch tx sent {txh}')
    out = poll(txh, acct.address, log=log)
    if out['mode'] == 'sent_unconfirmed' and err:
        out['send_error'] = str(err)
    out['description'] = description(proof)
    write_journal({'state': out['mode'], **out, 'raw': raw, 'proof': proof})
    return out


def resolve(run_dir=None, log=print):
    """Finish an interrupted launch: poll the journalled tx; if unknown to the chain, re-broadcast the SAME raw tx.
    Optionally write the result into <run_dir>/run.json. Never signs anything."""
    if not os.path.exists(JOURNAL):
        raise SystemExit('no journal: nothing was ever signed')
    j = json.load(open(JOURNAL))
    if 'raw' not in j:
        raise SystemExit(f"journal state {j.get('state')}: nothing was signed, so nothing can be on chain")
    rc, _ = rpc('eth_getTransactionReceipt', [j['tx']])
    if not rc:
        known, _ = rpc('eth_getTransactionByHash', [j['tx']])
        if not known:
            used = int(rpc_ok('eth_getTransactionCount', [j['creator'], 'latest']), 16)
            if used > 0:
                raise SystemExit(f"nonce 0 of {j['creator']} was consumed by a DIFFERENT tx; ours can never land")
            log('chain does not know the tx; re-broadcasting the identical signed bytes (nonce 0, cannot double-launch)')
            _, err = rpc('eth_sendRawTransaction', [j['raw']], all_rpcs_on_error=True)
            if err:
                log(f're-broadcast error: {err}')
    out = poll(j['tx'], j['creator'], log=log)
    out['description'] = j.get('description')
    write_journal({'state': out['mode'], **out, 'raw': j['raw'], 'proof': j['proof']})
    if run_dir:
        p = os.path.join(run_dir, 'run.json')
        meta = json.load(open(p))
        if (meta.get('press') or {}).get('proof') != j['proof']:
            raise SystemExit(f'{p} is a different run (its proof does not match the journalled launch)')
        meta['launch'] = out
        json.dump(meta, open(p, 'w'), indent=2)
    return out


if __name__ == '__main__':
    # This CLI never signs anything. --resolve can only re-broadcast bytes that launch_run already signed
    # and attempted to send (same tx, nonce 0), to finish an interrupted launch.
    if '--funding' in sys.argv:
        # how much ETH the fresh wallet needs; works before LIVE is set and before the wallet is funded
        c = config()
        call = {'from': DRY_CREATOR, 'to': FACTORY, 'data': calldata(c, DRY_CREATOR, 'ab' * 32),
                'value': hex(LAUNCH_FEE_WEI)}
        g, err = rpc('eth_estimateGas', [call, 'latest', {DRY_CREATOR: {'balance': hex(10 ** 18)}}])
        gas = int(int(g, 16) * GAS_MULT) if g else int(3_700_000 * GAS_MULT)
        fee = int(int(rpc_ok('eth_gasPrice', []), 16) * FEE_MULT)
        need = gas * fee + LAUNCH_FEE_WEI
        print(json.dumps({'gas_limit': gas, 'gas_estimated': bool(g), 'max_fee_gwei': fee / 1e9,
                          'minimum_eth': need / 1e18, 'fund_at_least_eth': round(need * 1.5 / 1e18 + 0.0005, 6)}, indent=2))
    elif '--clear-unsigned' in sys.argv:
        # a reservation from a run that never signed (no press, aborted, refused) can be cleared safely
        j = json.load(open(JOURNAL)) if os.path.exists(JOURNAL) else None
        if j is None:
            print('no journal')
        elif 'raw' in j:
            raise SystemExit('journal holds a SIGNED tx: never delete it; use --resolve')
        else:
            os.remove(JOURNAL); print(f"cleared unsigned journal (state {j.get('state')})")
    elif '--resolve' in sys.argv:
        i = sys.argv.index('--resolve')
        print(json.dumps(resolve(sys.argv[i + 1] if len(sys.argv) > i + 1 else None), indent=2))
    elif '--preflight' in sys.argv:
        pre = preflight(config(), 'ab' * 32)
        print(json.dumps({k: v for k, v in pre.items() if k != 'acct'}, indent=2, default=str))
    else:
        print('dry check (eth_call only, fake creator, fake balance):')
        print(json.dumps(launch('0123456789abcdef' * 4, live=False), indent=2))
        print(json.dumps(public(config()), indent=2))
