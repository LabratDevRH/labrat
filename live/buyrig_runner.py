"""The buy rig's runner: one rat session on pons per buyback batch. DRY (simulated) unless LIVE is switched on below.

    python live/buyrig_runner.py [--status-url https://labrat-buyback-production.up.railway.app/status]
                                 [--report-url <engine>/pons_session] [--relay wss://<relay>/publish]
                                 [--poll 20] [--once] [--catch-up N] [--max-age 3600] [--state runs/buyrig/runner.json]

It polls the buyback engine's PUBLIC status (live/buyback.py). Every simulated buy the engine books there is one batch.
For each NEW batch, oldest first, exactly once:
  1. it marks the batch as started in its state file (so a crash never runs a batch twice),
  2. runs one buy session: `python live/buyrig.py --amount <the batch's eth_in> --batch-at <its time> ...` (a separate
     process: its own Chromium, its own brain session; streamed to the relay's pons channel with --relay),
  3. checks the session proof: `python replay_session.py <run dir>` must print MATCH,
  4. reports it to the engine: POST <report-url> (Bearer BUYBACK_RIG_TOKEN from the process environment) with fixed
     fields only (the batch time and amount, LABRAT out, the session time, the session proof, MATCH, targets, misses,
     checks, simulation). The engine checks them against its own booked buy and shows "Simulated buy · clicked by the
     rat on pons" on it. Only a session whose checks all passed, whose simulation succeeded, whose mask held and whose
     replay matched is reported.
On its first start it takes the buys already listed as history (none of them is run) unless --catch-up N. It never
runs against an engine whose status is not DRY / simulated, never reads .env, never signs and never sends: the session
itself refuses to sign (live/buyrig.py). Tokens: LABRAT_PUBLISH_TOKEN (relay) and BUYBACK_RIG_TOKEN (engine), both from
the process environment.

LIVE (switched OFF): only when the process environment holds the same gates as the rig (BUYRIG_LIVE=1,
BUYRIG_CONFIRM=LABRAT, BUYBACK_RH_KEY of the pinned buyback wallet; live/buyrig_live.py env_gate). Then, besides the
DRY behaviour above (a DRY engine still gets simulated sessions, whose process gets no key), an engine in LIVE BOOKINGS
(status mode LIVE, buys.simulated false) is served: each booked real buy in buys.recent (simulated false, state
"booked", its window and exact eth_in) runs once, while its window can still be signed, as
`buyrig.py --amount <eth_in> --live --window <window>` (the session re-checks every gate and signs at most one
transaction for the window). Every poll first resolves the rig's unfinished windows (receipt, or the identical signed
bytes again) and reports every buy the rig's journal shows as mined and not yet reported: POST /pons_session with the
window, eth_in and the tx hash (plus the session's proof and counts when it has them). The engine verifies it on chain
before counting it; a 503 (no receipt there yet) is retried at the next poll. A LIVE session that ended without a buy
and without a record in the rig's journal is booked there as one failed buy (two in a row stop LIVE).
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LIVE_DIR = Path(__file__).resolve().parent
ROOT = LIVE_DIR.parent
if str(LIVE_DIR) not in sys.path:
    sys.path.insert(0, str(LIVE_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STATUS_URL = 'https://labrat-buyback-production.up.railway.app/status'
STATE_PATH = ROOT / 'runs' / 'buyrig' / 'runner.json'
RIG_TOKEN_ENV = 'BUYBACK_RIG_TOKEN'
RELAY_TOKEN_ENV = 'LABRAT_PUBLISH_TOKEN'
SESSION_TIMEOUT_S = 900
REPLAY_TIMEOUT_S = 1800
KEEP_KEYS = 500
ISO_FMT = '%Y-%m-%dT%H:%M:%SZ'


def log(*parts):
    stamp = datetime.now(timezone.utc).strftime('%H:%M:%S')
    try:
        print(f'[buyrig-runner {stamp}] ' + ' '.join(str(p) for p in parts), file=sys.stderr, flush=True)
    except Exception:
        pass


def iso(t=None):
    return datetime.fromtimestamp(time.time() if t is None else t, timezone.utc).strftime(ISO_FMT)


def iso_ts(s):
    return datetime.strptime(s, ISO_FMT).replace(tzinfo=timezone.utc).timestamp()


def batch_key(r):
    return f"{r['at']}|{r['eth_in']}"


def batch_seed(key):
    """A seed per batch: different sessions for different batches, reproducible from the batch."""
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 1_000_000


def valid_batch(r):
    """A recent-buy entry of the engine's public status that the rig can run: a simulated buy with a UTC time and a
    batch amount the rig accepts (0.00001 .. 0.1 ETH)."""
    import buyback
    if not isinstance(r, dict) or r.get('simulated') is not True:
        return False
    if not isinstance(r.get('at'), str) or not buyback.ISO_RE.match(r['at']):
        return False
    if not isinstance(r.get('eth_in'), str) or not buyback.DEC_RE.match(r['eth_in']):
        return False
    try:
        wei = buyback.parse_eth(r['eth_in'])
    except ValueError:
        return False
    return 10 ** 13 <= wei <= buyback.HARD['max_buy_wei']


def valid_live_batch(r):
    """A booked REAL buy of an engine in live bookings: simulated false, state "booked", the booked UTC hour (window),
    its booking time and an amount the rig accepts (0.00001 .. 0.1 ETH)."""
    import buyback
    if not isinstance(r, dict) or r.get('simulated') is not False or r.get('state') != 'booked':
        return False
    if not isinstance(r.get('window'), str) or not buyback.WINDOW_ID_RE.match(r['window']):
        return False
    if not isinstance(r.get('at'), str) or not buyback.ISO_RE.match(r['at']):
        return False
    if not isinstance(r.get('eth_in'), str) or not buyback.DEC_RE.match(r['eth_in']):
        return False
    try:
        wei = buyback.parse_eth(r['eth_in'])
    except ValueError:
        return False
    return 10 ** 13 <= wei <= buyback.HARD['max_buy_wei']


def live_key(r):
    return f"live|{r['window']}|{r['eth_in']}"


def child_env(live=False, environ=None):
    """The environment of a child process: the buyback wallet's key only for a LIVE session, never for anything else
    (a DRY session, the replay)."""
    import buyrig_live
    env = dict(os.environ if environ is None else environ)
    if not live:
        env.pop(buyrig_live.ENV_KEY, None)
    return env


def fetch_json(url, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': 'labrat-buyrig-runner/1', 'Cache-Control': 'no-store'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def post_json(url, body, token, timeout=20):
    """-> (HTTP status, parsed body or text)."""
    data = json.dumps(body, separators=(',', ':')).encode()
    req = urllib.request.Request(url, data=data, method='POST', headers={
        'Content-Type': 'application/json', 'Authorization': f'Bearer {token}', 'User-Agent': 'labrat-buyrig-runner/1'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode('utf-8', 'replace')
            code = r.status
    except urllib.error.HTTPError as e:
        code, txt = e.code, e.read().decode('utf-8', 'replace')
    try:
        return code, json.loads(txt)
    except ValueError:
        return code, txt[:300]


def report_body(r, res):
    """The fields the engine takes (buyback.PONS_KEYS), from the engine's own batch and the session's result."""
    return {'buy_at': r['at'], 'eth_in': r['eth_in'], 'labrat_out': res['labrat_out'],
            'session_at': res['session_at'], 'proof': res['session_proof'], 'replay': 'MATCH',
            'targets_hit': int(res['targets_hit']), 'misses': int(res['misses']),
            'checks_passed': int(res['checks_passed']), 'checks_total': int(res['checks_total']),
            'simulation': res['simulation']}


def reportable(res):
    """-> None when the session may be reported, else why not. A LIVE session is reportable when it signed and sent a
    buy that was mined with LABRAT received (ok): money moved, so it is reported whatever else happened (the engine
    verifies it on chain)."""
    if not isinstance(res, dict):
        return 'no result'
    if res.get('mode') == 'LIVE':
        import buyrig_live
        if res.get('dev_oracle'):
            return 'a scripted cursor is never LIVE'
        if not (res.get('signed') and res.get('sent')):
            return f"the LIVE session did not sign and send a buy (verdict {res.get('verdict')}: {res.get('error')})"
        if not isinstance(res.get('tx'), str) or not buyrig_live.HASH_RE.match(res['tx']):
            return 'no transaction hash'
        if not res.get('ok') or not res.get('labrat_out'):
            return f"the buy was sent but not mined with LABRAT received (verdict {res.get('verdict')})"
        return None
    if not res.get('ok'):
        return f"the session did not pass (verdict {res.get('verdict')}, failed {res.get('failed_checks')})"
    if res.get('mask_breach') or res.get('dev_oracle') or res.get('signed') or res.get('sent'):
        return 'the session is not reportable (mask, scripted cursor, or a send)'
    if res.get('simulation') != 'ok' or res.get('checks_passed') != res.get('checks_total'):
        return 'not every check passed'
    if not res.get('session_proof') or not res.get('labrat_out'):
        return 'no session proof or no LABRAT out'
    return None


def live_report_body(ex, session=None):
    """The LIVE report (buyback.LIVE_REPORT_KEYS) of one executed buy from the rig's journal (ex: buyrig_live
    State.executed()), with the session's facts when the runner has them."""
    import buyback
    body = {'window': ex['window'], 'eth_in': buyback.eth_str(ex['amount_wei'], 18), 'tx': ex['tx'],
            'labrat_out': buyback.eth_str(ex['labrat_out_wei'], 18)}
    s = session or {}
    for k in ('buy_at', 'session_at', 'proof'):
        if isinstance(s.get(k), str) and s[k]:
            body[k] = s[k]
    if s.get('replay') == 'MATCH':
        body['replay'] = 'MATCH'
    for k in ('targets_hit', 'misses', 'checks_passed', 'checks_total'):
        if isinstance(s.get(k), int) and not isinstance(s.get(k), bool):
            body[k] = s[k]
    if s.get('simulation') == 'ok':
        body['simulation'] = 'ok'
    return body


class LiveHooks:
    """The runner's LIVE side: the rig's journal (buyrig_live) and its gated RPC. Built only when the env gates hold.
    It never signs: resolve() finishes unfinished windows (receipt, or the identical signed bytes again)."""

    def __init__(self, rpc, journal, log_fn=log):
        import buyrig_live
        self.L, self.rpc, self.journal, self.log = buyrig_live, rpc, journal, log_fn

    def resolve(self):
        st = self.journal.state()
        if not st.unresolved():
            return None
        return self.L.resolve(self.journal, self.rpc, self.log)

    def stopped(self):
        return self.journal.state().stop

    def executed(self):
        return self.journal.state().executed()

    def signable(self, window, now):
        return self.L.signable(window, now, self.L.SESSION_MARGIN_S)

    def settle(self, window, res):
        """A LIVE session that ended without a record of its own in the journal (it crashed, timed out, or ran DRY
        because a gate failed in the child): one failed buy. A signed window is left to its receipt."""
        st = self.journal.state()
        w = st.windows.get(window)
        if w and (w['failed'] or w['signed'] or w['receipt'] or w['expired']):
            return None
        if st.stop:
            return None
        if isinstance(res, dict) and res.get('mode') == 'LIVE' and res.get('refusal_kind') == 'blocked':
            return None                     # refused before anything was attempted (stopped, unresolved...): not a buy
        why = ((res or {}).get('error') or (res or {}).get('verdict') or 'the session ended without a result')
        if isinstance(res, dict) and res.get('mode') != 'LIVE':
            why = f'the session ran simulated (a LIVE gate did not hold in the session: {why})'
        return self.L.record_failure(self.journal, window, str(why)[:300], self.log)


class Runner:
    """The polling logic, with every side effect injectable (tests): fetch(url) -> status dict, launch(batch, seed)
    -> the session's result dict (buyrig_result.json) or None, replay(run_dir) -> bool (MATCH), report(body) ->
    (status, response) or None when reporting is off. live: a LiveHooks (LIVE switched on) or None (DRY only); a LIVE
    session is launch(batch, seed, live=True)."""

    def __init__(self, status_url, state_path, launch, replay, report, fetch=fetch_json, clock=time.time,
                 max_age_s=3600.0, catch_up=0, live=None):
        self.status_url, self.state_path = status_url, Path(state_path)
        self.launch, self.replay, self.report, self.fetch, self.clock = launch, replay, report, fetch, clock
        self.max_age_s, self.catch_up = float(max_age_s), int(catch_up)
        self.live = live
        self.state = self._load()
        self.last_refusal = None
        self.sessions = 0

    # ---- state (one JSON file, written atomically after every change)
    def _load(self):
        try:
            with open(self.state_path, encoding='utf-8') as fh:
                st = json.load(fh)
            if isinstance(st, dict) and isinstance(st.get('seen'), dict):
                return st
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            raise SystemExit(f'{self.state_path} is unreadable ({type(e).__name__}); a batch could run twice: '
                             'look at it by hand first')
        return {'version': 1, 'baselined': False, 'seen': {}}

    def _save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        seen = self.state['seen']
        while len(seen) > KEEP_KEYS:
            seen.pop(next(iter(seen)))
        fd, tmp = tempfile.mkstemp(prefix='runner_', suffix='.tmp', dir=str(self.state_path.parent))
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as fh:
            json.dump(self.state, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.state_path)

    def _mark(self, key, state, **extra):
        self.state['seen'][key] = {'state': state, 'at': iso(self.clock()), **extra}
        self._save()

    # ---- one poll
    def refuse(self, why):
        if why != self.last_refusal:
            log(f'not running sessions: {why}')
        self.last_refusal = why
        return []

    def poll_once(self):
        """-> the keys of the batches handled in this poll (run, skipped or failed)."""
        try:
            st = self.fetch(self.status_url)
        except Exception as e:
            return self.refuse(f'the engine status could not be read ({type(e).__name__})')
        if not isinstance(st, dict):
            return self.refuse('the engine status is not a JSON object')
        buys = st.get('buys') if isinstance(st.get('buys'), dict) else {}
        if self.live is not None and st.get('mode') == 'LIVE' and buys.get('simulated') is False:
            return self._poll_live(buys)
        if self.live is not None:
            self._live_upkeep()                 # a signed buy is resolved and reported whatever the engine says now
        if st.get('mode') != 'DRY' or buys.get('simulated') is not True:
            return self.refuse('the engine is not simulated (mode DRY): the buy rig runs only for simulated buys'
                               if self.live is None else 'the engine is neither simulated (DRY) nor in live bookings')
        recent = buys.get('recent') if isinstance(buys.get('recent'), list) else []
        batches = [r for r in reversed(recent) if valid_batch(r)]          # oldest first
        self.last_refusal = None
        if not self.state.get('baselined'):
            keep = {batch_key(r) for r in batches[-self.catch_up:]} if self.catch_up > 0 else set()
            for r in batches:
                k = batch_key(r)
                if k not in keep and k not in self.state['seen']:
                    self.state['seen'][k] = {'state': 'baseline', 'at': iso(self.clock())}
            self.state['baselined'] = True
            self._save()
            log(f'first start: {len(batches) - len(keep)} earlier batch(es) taken as history'
                + (f', catching up on {len(keep)}' if keep else ''))
        handled = []
        for r in batches:
            k = batch_key(r)
            if k in self.state['seen']:
                continue
            handled.append(k)
            if r.get('pons'):
                self._mark(k, 'skipped', why='the engine already shows a pons session for it')
                continue
            age = self.clock() - iso_ts(r['at'])
            if age > self.max_age_s:
                self._mark(k, 'skipped', why=f'booked {int(age)} s ago (over --max-age)')
                log(f'batch {k}: too old ({int(age)} s), skipped')
                continue
            self._run_batch(k, r)
        return handled

    def _run_batch(self, k, r):
        seed = batch_seed(k)
        self._mark(k, 'started', seed=seed)                  # before anything runs: never twice, even after a crash
        self.sessions += 1
        log(f"batch {k}: the rat buys {r['eth_in']} ETH of LABRAT on pons (simulated), seed {seed}")
        try:
            res = self.launch(r, seed)
        except Exception as e:
            res = None
            log(f'batch {k}: the session failed to run ({type(e).__name__}: {str(e)[:160]})')
        why = reportable(res)
        if why:
            self._mark(k, 'failed', seed=seed, why=why[:200], run_dir=(res or {}).get('run_dir'))
            log(f'batch {k}: not reported: {why}')
            return
        run_dir = res['run_dir']
        try:
            match = bool(self.replay(run_dir))
        except Exception as e:
            match = False
            log(f'batch {k}: replay failed to run ({type(e).__name__})')
        if not match:
            self._mark(k, 'failed', seed=seed, why='replay_session.py did not print MATCH', run_dir=run_dir)
            log(f'batch {k}: the session proof did not replay: not reported')
            return
        body = report_body(r, res)
        try:
            out = self.report(body)
        except Exception as e:
            out = ('error', f'{type(e).__name__}: {str(e)[:160]}')
        if out is None:
            self._mark(k, 'done', seed=seed, run_dir=run_dir, reported=False, why='reporting is off',
                       labrat_out=res['labrat_out'])
            log(f"batch {k}: done ({res['labrat_out']} LABRAT, simulated; replay MATCH); not reported (reporting is "
                'off)')
            return
        code, resp = out
        ok = code == 200
        self._mark(k, 'done', seed=seed, run_dir=run_dir, reported=ok, report_status=code,
                   report_error=None if ok else str(resp)[:200], labrat_out=res['labrat_out'])
        log(f"batch {k}: done ({res['labrat_out']} LABRAT, simulated; replay MATCH); engine {code}"
            + ('' if ok else f': {str(resp)[:160]}'))

    # ---- LIVE (only with LiveHooks: the env gates held at start) --------------------------------------------------------
    def _live_upkeep(self):
        """Every LIVE poll: finish the rig's unfinished windows (never a new transaction), then report every buy its
        journal shows as mined and not yet reported."""
        try:
            self.live.resolve()
        except Exception as e:
            log(f'LIVE: resolving unfinished windows failed for now ({type(e).__name__})')
        self._report_live()

    def _live_state(self, window):
        live = self.state.setdefault('live', {})
        while len(live) > KEEP_KEYS:
            live.pop(next(iter(live)))
        return live.setdefault(window, {})

    def _report_live(self):
        try:
            executed = self.live.executed()
        except Exception as e:
            log(f'LIVE: the rig journal could not be read ({type(e).__name__})')
            return
        for ex in executed:
            ent = self._live_state(ex['window'])
            if ent.get('reported') or ent.get('report_final'):
                continue
            body = live_report_body(ex, ent.get('session'))
            try:
                out = self.report(body)
            except Exception as e:
                out = ('error', f'{type(e).__name__}: {str(e)[:160]}')
            if out is None:
                if ent.get('why') != 'reporting is off':
                    ent.update(tx=ex['tx'], why='reporting is off')
                    self._save()
                    log(f"LIVE: the buy for {ex['window']} ({ex['tx']}) is not reported: reporting is off")
                continue
            code, resp = out
            ent.update(tx=ex['tx'], report_status=code, report_error=None if code == 200 else str(resp)[:200],
                       reported=code == 200, at=iso(self.clock()))
            # 503 (no receipt on the engine's RPC yet), 500 or a network error: retried at the next poll; any other
            # refusal is final (and loud: a real buy the engine does not count)
            if code not in (200, 500, 503, 'error'):
                ent['report_final'] = True
            self._save()
            log(f"LIVE: reported the buy for {ex['window']} ({ex['tx']}): engine {code}"
                + ('' if code == 200 else f': {str(resp)[:200]}' + ('' if ent.get('report_final') else ' (retried)')))

    def _poll_live(self, buys):
        self.last_refusal = None
        self._live_upkeep()
        recent = buys.get('recent') if isinstance(buys.get('recent'), list) else []
        batches = [r for r in reversed(recent) if valid_live_batch(r)]          # oldest first
        handled = []
        for r in batches:
            k = live_key(r)
            if k in self.state['seen']:
                continue
            handled.append(k)
            now = self.clock()
            if not self.live.signable(r['window'], now):
                self._mark(k, 'skipped', why='too late: a window is signed at most one hour after it ends',
                           window=r['window'])
                log(f'LIVE batch {k}: too late to execute, skipped')
                continue
            stop = self.live.stopped()
            if stop:
                self._mark(k, 'skipped', why=f"LIVE is stopped ({str(stop.get('why'))[:160]}; stop id {stop.get('id')})",
                           window=r['window'])
                log(f"LIVE batch {k}: skipped, LIVE is stopped (stop id {stop.get('id')})")
                continue
            self._run_live_batch(k, r)
        return handled

    def _run_live_batch(self, k, r):
        seed = batch_seed(k)
        w = r['window']
        self._mark(k, 'started', seed=seed, window=w, live=True)    # before anything runs: never twice
        self.sessions += 1
        log(f"LIVE batch {k}: the rat buys {r['eth_in']} ETH of LABRAT on pons for the window {w}, seed {seed}")
        try:
            res = self.launch(r, seed, live=True)
        except Exception as e:
            res = None
            log(f'LIVE batch {k}: the session failed to run ({type(e).__name__}: {str(e)[:160]})')
        try:
            self.live.settle(w, res)
        except Exception as e:
            log(f'LIVE batch {k}: the rig journal could not be updated ({type(e).__name__})')
        res = res if isinstance(res, dict) else {}
        match = False
        if res.get('run_dir') and res.get('session_proof'):
            try:
                match = bool(self.replay(res['run_dir']))
            except Exception as e:
                log(f'LIVE batch {k}: replay failed to run ({type(e).__name__})')
        session = {'buy_at': r['at'], 'session_at': res.get('session_at'), 'proof': res.get('session_proof'),
                   'replay': 'MATCH' if match else None, 'targets_hit': res.get('targets_hit'),
                   'misses': res.get('misses'), 'checks_passed': res.get('checks_passed'),
                   'checks_total': res.get('checks_total'), 'simulation': res.get('simulation'),
                   'run_dir': res.get('run_dir'), 'verdict': res.get('verdict'), 'tx': res.get('tx')}
        self._live_state(w)['session'] = session
        why = reportable(res)
        self._mark(k, 'done' if why is None else 'failed', seed=seed, window=w, run_dir=res.get('run_dir'),
                   verdict=res.get('verdict'), tx=res.get('tx'), why=None if why is None else why[:200],
                   replay='MATCH' if match else None)
        log(f"LIVE batch {k}: " + (f"bought ({res.get('labrat_out')} LABRAT, {res.get('tx')})" if why is None
                                   else f'no buy: {why}'))
        self._report_live()


# ---------------------------------------------------------------------------------------------------- side effects
def make_launch(relay=None, out_root=None, python=sys.executable):
    def launch(r, seed, live=False):
        fd, res_path = tempfile.mkstemp(prefix='buyrig_result_', suffix='.json')
        os.close(fd)
        try:
            cmd = [python, str(LIVE_DIR / 'buyrig.py'), '--amount', r['eth_in'], '--batch-at', r['at'], '--seed', str(seed),
                   '--result-json', res_path]
            if isinstance(r.get('hits_covered'), int):
                cmd += ['--hits', str(r['hits_covered'])]
            if relay:
                cmd += ['--relay', relay]
            if out_root:
                cmd += ['--out-root', str(out_root)]
            if live:
                cmd += ['--live', '--window', r['window']]
            p = subprocess.run(cmd, cwd=str(ROOT), timeout=SESSION_TIMEOUT_S, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                               env=child_env(live))
            tail = [ln for ln in (p.stdout or '').splitlines() if ln.startswith('BUYRIG_RESULT ')]
            log(f'session exit {p.returncode}: ' + (tail[-1][:300] if tail else '(no result line)'))
            try:
                with open(res_path, encoding='utf-8') as fh:
                    return json.load(fh)
            except (OSError, ValueError):
                return None
        finally:
            try:
                os.remove(res_path)
            except OSError:
                pass
    return launch


def make_replay(python=sys.executable):
    def replay(run_dir):
        p = subprocess.run([python, str(ROOT / 'replay_session.py'), str(ROOT / run_dir)], cwd=str(ROOT),
                           timeout=REPLAY_TIMEOUT_S, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           encoding='utf-8', errors='replace', env=child_env(False))
        lines = (p.stdout or '').strip().splitlines()
        return p.returncode == 0 and bool(lines) and lines[-1].strip() == 'MATCH'
    return replay


def make_report(url, token):
    if not url or not token:
        return lambda body: None
    return lambda body: post_json(url, body, token)


def default_report_url(status_url):
    base = status_url.split('?', 1)[0]
    if base.endswith('/status'):
        base = base[:-len('/status')]
    return base.rstrip('/') + '/pons_session'


def live_hooks(environ=None, log_fn=log):
    """-> (LiveHooks, '') when the LIVE env gates hold (buyrig_live.env_gate), else (None, why): DRY only. The runner
    keeps no key: the account is used here only to open the gated RPC (resolve() re-broadcasts, never signs)."""
    import buyrig_live
    acct, why = buyrig_live.env_gate(environ)
    if acct is None:
        return None, why
    try:
        if buyrig_live.VOLUME is not None and not os.path.ismount(str(buyrig_live.VOLUME)):
            return None, f'{buyrig_live.VOLUME} is not a mounted volume (the LIVE journal must survive a redeploy)'
        rpc = buyrig_live.open_rpc(acct, environ=environ)
    except buyrig_live.LiveRefused as e:
        return None, str(e)
    finally:
        acct = None
    journal = buyrig_live.Journal()
    buyrig_live.apply_operator_env(journal, rpc, environ, log_fn)
    return LiveHooks(rpc, journal, log_fn), ''


def main(argv=None):
    ap = argparse.ArgumentParser(description='one rat buy on pons per buyback batch (simulated unless LIVE is on)')
    ap.add_argument('--status-url', default=STATUS_URL)
    ap.add_argument('--report-url', help='default: <status-url without /status>/pons_session')
    ap.add_argument('--relay', help=f'stream each session to <relay>?channel=pons ({RELAY_TOKEN_ENV} from the environment)')
    ap.add_argument('--poll', type=float, default=20.0)
    ap.add_argument('--once', action='store_true', help='one poll, then exit')
    ap.add_argument('--catch-up', type=int, default=0, help='first start only: also run the newest N listed batches')
    ap.add_argument('--max-age', type=float, default=3600.0, help='skip batches booked longer ago than this (s)')
    ap.add_argument('--state', default=str(STATE_PATH))
    ap.add_argument('--out-root', help='where the sessions write their run dirs (default runs/)')
    a = ap.parse_args(argv)
    if not a.status_url.startswith('https://') and not a.status_url.startswith(('http://127.0.0.1', 'http://localhost')):
        raise SystemExit('--status-url must be https (or http on localhost)')
    if a.relay and len(os.environ.get(RELAY_TOKEN_ENV, '').strip()) < 16:
        raise SystemExit(f'--relay needs {RELAY_TOKEN_ENV} in the environment')
    token = os.environ.get(RIG_TOKEN_ENV, '').strip()
    report_url = a.report_url or default_report_url(a.status_url)
    if not token:
        log(f'{RIG_TOKEN_ENV} is not set: sessions run and are recorded, but not reported to the engine')
    hooks, why = live_hooks()
    log('LIVE: on (booked real buys are executed; every session re-checks every gate)' if hooks else
        f'LIVE: off ({why}); simulated sessions only')
    runner = Runner(a.status_url, a.state, make_launch(a.relay, a.out_root), make_replay(),
                    make_report(report_url, token), max_age_s=a.max_age, catch_up=a.catch_up, live=hooks)
    log(f'watching {a.status_url} every {a.poll:g} s; state {a.state}'
        + (f"; streaming to {a.relay.split('?')[0]} (pons channel)" if a.relay else ''))
    try:
        while True:
            runner.poll_once()
            if a.once:
                break
            time.sleep(a.poll)
    except KeyboardInterrupt:
        log('stopping')
    return 0


if __name__ == '__main__':
    sys.exit(main())
