"""End-to-end test of relay.py.

Starts the relay with the Procfile's own command line on 127.0.0.1:4723 and drives it with a fake publisher and
several viewers: two fast ones (the websockets client), a deliberately slow one and a stuck one (hand-rolled websocket
clients on a socket with a 4 KB receive buffer, reading at a pace we set), plus short-lived ones for the caps.

    python relay/test_relay.py

Takes about a minute (it waits out the real 15 s idle timer twice). Prints PASS/FAIL per check and exits non-zero on
any failure. It only ever stops the relay processes it started itself.
"""
import asyncio
import base64
import json
import os
import secrets
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 4723
HOSTPORT = f'127.0.0.1:{PORT}'
PUB = f'ws://{HOSTPORT}/publish'
LIVE = f'ws://{HOSTPORT}/live'
TOKEN = 'test-' + secrets.token_urlsafe(24)
AUTH = {'Authorization': 'Bearer ' + TOKEN}
FRAME_BYTES = (12 + 65 * 7) * 4          # 1868, the rat frame size in the protocol
SITE_MARKER = 'labrat-relay-test-index-page'
RELAY_ENV = ('LABRAT_PUBLISH_TOKEN', 'SERVE_SITE', 'SITE_DIR', 'RELAY_MAX_VIEWERS', 'RELAY_MAX_PER_IP', 'LIVE_ORIGINS')

RESULTS = []
TEMP_DIRS = []                            # removed at the end if every check passed (kept for the logs otherwise)


def temp_dir(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    TEMP_DIRS.append(d)
    return d


def check(cond, label, detail=''):
    ok = bool(cond)
    RESULTS.append((ok, label))
    print(('  PASS  ' if ok else '  FAIL  ') + label + (f'  ({detail})' if detail else ''), flush=True)
    return ok


def section(title):
    print(f'\n== {title}', flush=True)


# ---- the relay process ------------------------------------------------------------------------------------------------
def procfile_argv(port):
    with open(os.path.join(HERE, 'Procfile'), encoding='utf-8') as f:
        line = next(ln for ln in f if ln.startswith('web:'))
    argv = shlex.split(line[len('web:'):])
    assert argv[0] == 'uvicorn', argv
    out = []
    for a in argv[1:]:
        a = a.replace('$PORT', str(port))
        out.append('127.0.0.1' if a == '0.0.0.0' else a)      # loopback only for the test
    return [sys.executable, '-m', 'uvicorn'] + out


def http_get(path):
    try:
        with urllib.request.urlopen(f'http://{HOSTPORT}{path}', timeout=5) as r:
            return r.status, r.headers, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode('utf-8', 'replace')


def port_busy():
    try:
        socket.create_connection(('127.0.0.1', PORT), 0.5).close()
        return True
    except OSError:
        return False


class Relay:
    def __init__(self, env, name):
        base = {k: v for k, v in os.environ.items() if k not in RELAY_ENV and not k.startswith('UVICORN_')}
        base.update(env)
        base['PYTHONUNBUFFERED'] = '1'
        self.logpath = os.path.join(temp_dir('labrat_relay_test_'), f'relay_{name}.log')
        self.log = open(self.logpath, 'w', encoding='utf-8')
        self.p = subprocess.Popen(procfile_argv(PORT), cwd=HERE, env=base, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.p.poll() is not None:
                raise RuntimeError(f'the relay exited with {self.p.returncode}:\n{self.text()}')
            try:
                if http_get('/healthz')[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.2)
        self.stop()
        raise RuntimeError('the relay did not come up within 30 s')

    def text(self):
        if not self.log.closed:
            self.log.flush()
        with open(self.logpath, encoding='utf-8', errors='replace') as f:
            return f.read()

    def stop(self):
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(10)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait(5)
        self.log.close()


async def status():
    s, _, body = await asyncio.to_thread(http_get, '/status')
    assert s == 200, s
    return json.loads(body)


async def until(pred, timeout, step=0.02):
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        await asyncio.sleep(step)
    try:
        return bool(pred())
    except Exception:
        return False


async def until_status(pred, timeout):
    end = time.perf_counter() + timeout
    while True:
        st = await status()
        if pred(st) or time.perf_counter() > end:
            return st
        await asyncio.sleep(0.1)


# ---- frames and messages -------------------------------------------------------------------------------------------
def make_frame(k, size=FRAME_BYTES, magic=7.0):
    n = size // 4
    vals = [0.0] * n
    vals[0], vals[1], vals[2] = magic, k * 0.04, float(k)
    return struct.pack(f'<{n}f', *vals)


def frame_k(b):
    return int(struct.unpack_from('<f', b, 8)[0])


def _no_nan(c):
    raise ValueError(f'non-standard JSON constant {c}')


def strict(text):
    """Parse like a browser's JSON.parse would (no NaN / Infinity)."""
    try:
        return json.loads(text, parse_constant=_no_nan)
    except ValueError as e:
        return {'type': '__invalid_json__', 'error': str(e)}


async def refused_status(url, headers=None):
    """HTTP status of a websocket handshake: 101 if accepted, else the refusal's status and body."""
    try:
        ws = await connect(url, additional_headers=headers or {}, open_timeout=5)
    except InvalidStatus as e:
        return e.response.status_code, (e.response.body or b'').decode('utf-8', 'replace').strip()
    await ws.close()
    return 101, ''


async def close_code_of(ws, timeout):
    try:
        await asyncio.wait_for(ws.wait_closed(), timeout)
    except asyncio.TimeoutError:
        return None
    return ws.close_code


# ---- viewers -------------------------------------------------------------------------------------------------------
class Rec:
    """A fast viewer (the websockets client) that records every message with its arrival time."""

    def __init__(self, name):
        self.name = name
        self.msgs = []                 # (perf_counter, str | bytes)
        self.ws = None
        self.code = None
        self.done = False

    async def start(self, **kw):
        self.ws = await connect(LIVE, max_size=None, open_timeout=5, **kw)
        asyncio.create_task(self._run())
        return self

    async def _run(self):
        try:
            async for m in self.ws:
                self.msgs.append((time.perf_counter(), m))
        except ConnectionClosed:
            pass
        self.code = self.ws.close_code
        self.done = True

    def texts(self, after=0):
        return [strict(m) for _, m in self.msgs[after:] if isinstance(m, str)]

    def frames(self, after=0):
        return [(t, m) for t, m in self.msgs[after:] if isinstance(m, bytes)]

    def find_text(self, pred, after=0):
        for i in range(after, len(self.msgs)):
            t, m = self.msgs[i]
            if isinstance(m, str):
                d = strict(m)
                if pred(d):
                    return i, t, d
        return None

    async def close(self):
        if self.ws is not None and not self.done:
            await self.ws.close()


class RawViewer(threading.Thread):
    """A hand-rolled websocket viewer on a socket with a tiny receive buffer, so its speed is real TCP backpressure:
    mode 'slow' reads CHUNK bytes every PAUSE s, 'stuck' reads nothing, 'fast' reads as fast as it can. It answers
    pings and close frames like a browser would."""

    def __init__(self, name, mode, rcvbuf=4096, chunk=512, pause=0.1, origin='https://example.test'):
        super().__init__(daemon=True)
        self.name, self.mode, self.chunk, self.pause = name, mode, chunk, pause
        self.msgs = []                 # (perf_counter, 'text' | 'bin', payload)
        self.close_code = None
        self.eof = False
        self.stop_flag = False
        self.buf = bytearray()
        self.lock = threading.Lock()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        s.settimeout(5)
        s.connect(('127.0.0.1', PORT))
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f'GET /live HTTP/1.1\r\nHost: {HOSTPORT}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                   f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: {origin}\r\n\r\n').encode())
        head = b''
        while b'\r\n\r\n' not in head:          # byte by byte, so nothing after the headers is consumed here
            c = s.recv(1)
            if not c:
                break
            head += c
        self.status = int(head.split(b' ', 2)[1]) if head else 0
        s.settimeout(0.2)
        self.sock = s

    def send_frame(self, op, payload=b''):
        mask = os.urandom(4)
        n = len(payload)
        hdr = bytes([0x80 | op])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack('>H', n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack('>Q', n)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self.lock:
            try:
                self.sock.sendall(hdr + mask + body)
            except OSError:
                pass

    def run(self):
        while not self.stop_flag and not self.eof:
            if self.mode == 'stuck':
                time.sleep(0.05)
                continue
            try:
                data = self.sock.recv(self.chunk if self.mode == 'slow' else 65536)
            except socket.timeout:
                continue
            except OSError:
                self.eof = True
                break
            if not data:
                self.eof = True
                break
            self.buf += data
            self._parse()
            if self.mode == 'slow':
                time.sleep(self.pause)

    def _parse(self):
        b = self.buf
        while len(b) >= 2:
            op, n, h = b[0] & 0x0F, b[1] & 0x7F, 2
            if n == 126:
                if len(b) < 4:
                    return
                n, h = struct.unpack_from('>H', b, 2)[0], 4
            elif n == 127:
                if len(b) < 10:
                    return
                n, h = struct.unpack_from('>Q', b, 2)[0], 10
            if len(b) < h + n:
                return
            payload = bytes(b[h:h + n])
            del b[:h + n]
            now = time.perf_counter()
            if op == 1:
                self.msgs.append((now, 'text', payload.decode('utf-8')))
            elif op == 2:
                self.msgs.append((now, 'bin', payload))
            elif op == 8:
                self.close_code = struct.unpack('>H', payload[:2])[0] if len(payload) >= 2 else 1005
                self.send_frame(8, payload[:2])
            elif op == 9:
                self.send_frame(10, payload)

    def texts(self):
        return [strict(p) for _, kind, p in self.msgs if kind == 'text']

    def frame_ks(self):
        return [frame_k(p) for _, kind, p in self.msgs if kind == 'bin']

    def shutdown(self):
        self.stop_flag = True
        try:
            self.sock.close()
        except OSError:
            pass


# ---- the tests -----------------------------------------------------------------------------------------------------
HELLO = {'type': 'hello', 'source': 'training', 'task': 'lever', 'run': 'test_run_1', 'label': 'relay test run',
         'fps': 25, 'started': '2026-09-24T00:00:00Z'}
HELLO2 = {'type': 'hello', 'source': 'training', 'task': 'cursor', 'run': 'test_run_2', 'label': 'relay test run 2',
          'fps': 25, 'started': '2026-09-24T00:10:00Z'}


async def main_relay():
    site = temp_dir('labrat_site_')
    with open(os.path.join(site, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(f'<!doctype html><title>t</title><p>{SITE_MARKER}</p>\n')
    relay = Relay({'LABRAT_PUBLISH_TOKEN': TOKEN, 'SERVE_SITE': '1', 'SITE_DIR': site, 'RELAY_MAX_VIEWERS': '6',
                   'RELAY_MAX_PER_IP': '2'}, 'main')
    raws = []
    try:
        await main_checks(raws)
    finally:
        for r in raws:
            r.shutdown()
        relay.stop()
        log = relay.text()
    check('Traceback' not in log and 'Exception in ASGI' not in log, 'the relay logged no exceptions',
          f'log: {relay.logpath}')
    n_logged = log.count('missing or wrong token')
    check(n_logged == 3, 'refused publisher handshakes are logged at most 3 times a minute (6 were sent)',
          f'{n_logged} lines')
    if 'Traceback' in log or 'Exception in ASGI' in log:
        print(log[-4000:])


async def main_checks(raws):
    section('HTTP')
    s, h, body = await asyncio.to_thread(http_get, '/status')
    st = json.loads(body)
    check(s == 200 and st['live'] is False and st['hello'] is None and st['metrics'] is None and st['viewers'] == 0
          and st['allow_test_streams'] is False,
          'GET /status before any publisher: not live, no hello, no metrics, 0 viewers, test streams refused')
    check(h.get('Access-Control-Allow-Origin') == '*', '/status is readable cross-origin (ACAO *)')
    s, _, body = await asyncio.to_thread(http_get, '/healthz')
    check(s == 200 and json.loads(body) == {'ok': True}, 'GET /healthz')
    s, _, body = await asyncio.to_thread(http_get, '/')
    check(s == 200 and SITE_MARKER in body, 'SERVE_SITE=1 serves the site directory at /')
    s, _, _ = await asyncio.to_thread(http_get, '/status')
    check(s == 200, 'the API routes still win over the static site')

    section('publisher auth')
    for hdrs, what in [(None, 'no Authorization header'),
                       ({'Authorization': 'Bearer not-the-token-at-all'}, 'a wrong token'),
                       ({'Authorization': 'Bearer ' + TOKEN[:-1]}, 'the token minus its last character'),
                       ({'Authorization': 'Bearer ' + TOKEN + 'x'}, 'the token plus one character'),
                       ({'Authorization': 'Basic ' + TOKEN}, 'the right token under the wrong scheme'),
                       ({'Authorization': TOKEN}, 'the bare token without "Bearer"')]:
        code, _ = await refused_status(PUB, hdrs)
        check(code == 401, f'/publish refuses {what}', f'HTTP {code}')

    section('state, hello, metrics, frames')
    A = await Rec('A').start()
    await until(lambda: len(A.msgs) >= 1, 3)
    s0 = A.texts()[0] if A.msgs else {}
    check(s0.get('type') == 'state' and s0.get('live') is False and s0.get('hello') is None
          and s0.get('history') == [], 'a viewer first gets the state: not live, no hello, empty history')

    P = await connect(PUB, additional_headers=AUTH, open_timeout=5)
    check(True, '/publish accepts the right token')
    await P.send(make_frame(99999))                                        # before the hello: dropped
    await P.send(json.dumps({'type': 'metrics', 'row': {'steps': 1}}))     # before the hello: dropped
    await P.send(json.dumps(HELLO))
    rows = [{'steps': (i + 1) * 1000, 'ret': round(i * 0.5, 2), 'press_rate': round(i / 310, 4)} for i in range(310)]
    for r in rows:
        await P.send(json.dumps({'type': 'metrics', 'row': r}))
    await P.send(json.dumps({'type': 'metrics', 'row': {'steps': 311000, 'ret': float('nan')}}))  # Python writes NaN
    rows.append({'steps': 311000, 'ret': None})
    await P.send('{"type":"metrics","row":{"steps":312000,"ret":1e400,"len":-1e400}}')   # parses to inf in Python
    rows.append({'steps': 312000, 'ret': None, 'len': None})
    ck = {'type': 'checkpoint', 'steps': 312000, 'sha256': 'ab' * 32}
    await P.send(json.dumps(ck))
    for k in range(5):
        await P.send(make_frame(k))
        await asyncio.sleep(0.04)
    await until(lambda: len(A.frames()) >= 5, 3)
    ta = A.texts()
    check(len(ta) > 1 and ta[1] == HELLO, 'the hello is forwarded; nothing sent before it got through')
    got_rows = [m['row'] for m in ta if m.get('type') == 'metrics']
    check(got_rows == rows, 'metrics rows forwarded in order; NaN and out-of-range numbers (1e400) turned into null',
          f'{len(got_rows)} rows')
    check(ck in ta, 'the checkpoint message is forwarded')
    fa = A.frames()
    check([frame_k(b) for _, b in fa] == [0, 1, 2, 3, 4] and all(b == make_frame(i) for i, (_, b) in enumerate(fa)),
          'frames forwarded byte for byte; the frame sent before the hello was dropped',
          str([frame_k(b) for _, b in fa]))
    st = await status()
    check(st['live'] is True and st['hello'] == HELLO and st['metrics'] == rows[-1] and st['viewers'] == 1
          and st['checkpoint'] == ck and st['history_rows'] == 300,
          '/status while live: live, the hello, the last metrics row, 1 viewer')

    B = await Rec('B').start()
    await until(lambda: len(B.msgs) >= 2, 3)
    s0 = B.texts()[0] if B.msgs else {}
    check(s0.get('type') == 'state' and s0.get('live') is True and s0.get('hello') == HELLO,
          'a late joiner gets the state: live, with the hello')
    hist = s0.get('history') or []
    check(hist == rows[-300:], 'a late joiner gets the last 300 metrics rows',
          f"{len(hist)} rows, steps {hist[0]['steps'] if hist else '-'}..{hist[-1]['steps'] if hist else '-'}")
    check(s0.get('checkpoint') == ck, 'a late joiner gets the last checkpoint')
    check(len(B.msgs) >= 2 and B.msgs[1][1] == make_frame(4),
          'a late joiner gets the newest frame straight after the state')

    section('fanout: two fast viewers and a deliberately slow one')
    C = RawViewer('C', 'slow', rcvbuf=4096, chunk=512, pause=0.1)
    raws.append(C)
    check(C.status == 101, 'a viewer with a foreign Origin is accepted (/live is public)', f'HTTP {C.status}')
    C.start()
    await until(lambda: len(C.msgs) >= 1, 3)
    K0, N = 100, 200
    sent, slowest = {}, 0.0
    t0 = time.perf_counter()
    for i in range(N):
        d = t0 + i * 0.04 - time.perf_counter()
        if d > 0:
            await asyncio.sleep(d)
        ts = time.perf_counter()
        await P.send(make_frame(K0 + i))
        slowest = max(slowest, time.perf_counter() - ts)
        sent[K0 + i] = ts
    await asyncio.sleep(0.6)
    for R in (A, B):
        fs = [(t, b) for t, b in R.frames() if K0 <= frame_k(b) < K0 + N]
        ks = [frame_k(b) for _, b in fs]
        check(ks == list(range(K0, K0 + N)) and all(b == make_frame(frame_k(b)) for _, b in fs),
              f'fast viewer {R.name} got all {N} frames, in order, byte for byte', f'{len(ks)}/{N}')
        if len(fs) >= 2:
            fps = (len(fs) - 1) / (fs[-1][0] - fs[0][0])
            lat = sorted(t - sent[frame_k(b)] for t, b in fs)
            p95 = lat[int(0.95 * (len(lat) - 1))]
            check(22.0 <= fps <= 28.0, f'{R.name} receives at the publisher rate (25 fps)', f'{fps:.1f} fps')
            check(p95 < 0.25, f'{R.name} latency stays low while the slow viewer lags',
                  f'p95 {p95 * 1000:.0f} ms, max {lat[-1] * 1000:.0f} ms')
    check(slowest < 0.1, 'the publisher is never blocked', f'slowest send {slowest * 1000:.1f} ms')
    # then a burst far larger than any OS socket buffer (2000 frames, 3.7 MB), so the slow viewer must fall behind
    KB, NB = 10000, 2000
    tb = time.perf_counter()
    for i in range(NB):
        await P.send(make_frame(KB + i))
    t_burst = time.perf_counter()
    last = KB + NB - 1
    await until(lambda: all(any(frame_k(b) == last for _, b in R.frames()) for R in (A, B)), 3)
    for R in (A, B):
        got = next((t for t, b in R.frames() if frame_k(b) == last), None)
        check(got is not None and got - t_burst < 1.0, f'{R.name} gets the last frame of a burst promptly',
              f'{NB} frames sent in {(t_burst - tb) * 1000:.0f} ms, last one here '
              f'{(got - t_burst) * 1000 if got else -1:.0f} ms after')
    st = await status()
    c_during = [k for k in C.frame_ks() if k >= K0]
    dropped = st['counts'].get('frames_dropped_for_slow_viewers', 0)
    check(dropped > 0, 'the relay drops old frames for the slow viewer instead of queueing them',
          f'{dropped} dropped; the slow viewer had read {len(c_during)} of {N + NB} so far')
    check(not C.eof and C.close_code is None and st['viewers'] == 3, 'the slow viewer is still connected')
    C.mode = 'fast'
    await until(lambda: last in C.frame_ks(), 5)
    cks = [k for k in C.frame_ks() if k >= K0]
    check(C.texts()[:1] and C.texts()[0].get('type') == 'state', 'the slow viewer got its state first')
    check(cks == sorted(set(cks)), 'the slow viewer\'s frames arrive in order, no duplicates')
    check(cks and cks[-1] == last and len(cks) < N + NB,
          'once it catches up, the slow viewer ends on the newest frame (drop-oldest)',
          f'{len(cks)}/{N + NB} frames, last {cks[-1] if cks else "-"}')

    section('a publisher resync (as after a reconnect): hello, checkpoint and 300 rows in one burst')
    back = rows[-300:]
    burst = [json.dumps(HELLO), json.dumps(ck)] + [json.dumps({'type': 'metrics', 'row': r}) for r in back]
    marks = {R: len(R.msgs) for R in (A, B)}
    cmark = len(C.msgs)
    for m in burst:                                   # written back to back, so the relay reads one buffered burst
        await P.send(m)
    want = [HELLO, ck] + [{'type': 'metrics', 'row': r} for r in back]
    await until(lambda: all(len(R.texts(marks[R])) >= len(want) for R in (A, B)), 5)
    for R in (A, B):
        check(R.texts(marks[R]) == want, f'{R.name} got the whole burst in order (hello, checkpoint, 300 rows)',
              f'{len(R.texts(marks[R]))} texts')
    await until(lambda: len(C.msgs) - cmark >= len(want), 5)
    ctx = [strict(p) for _, kind, p in C.msgs[cmark:] if kind == 'text']
    check(ctx == want and not C.eof, 'the viewer that was slow got it too and stays connected')
    st = await status()
    check(st['viewers'] == 3 and st['counts'].get('viewers_closed_too_far_behind', 0) == 0,
          'nobody is closed as too far behind by a resync burst', f"viewers {st['viewers']}")
    check(st['live'] is True and st['history_rows'] == 300 and st['metrics'] == rows[-1] and st['checkpoint'] == ck,
          'the resync rebuilt the history: 300 rows, ending on the last one, no duplicates')
    L = await Rec('L').start()
    await until(lambda: len(L.msgs) >= 1, 3)
    s0 = L.texts()[0] if L.msgs else {}
    check(s0.get('live') is True and s0.get('history') == back and s0.get('checkpoint') == ck,
          'a viewer joining after the resync gets exactly those 300 rows once')
    await L.close()
    await until_status(lambda s: s['viewers'] == 3, 3)

    section('a stuck viewer is closed, nobody else notices')
    D = RawViewer('D', 'stuck', rcvbuf=4096)
    raws.append(D)
    D.start()
    await until_status(lambda s: s['viewers'] == 4, 3)
    for i in range(150):                              # 3 s of frames at 50 fps fill D's socket and the relay's buffer
        await P.send(make_frame(1000 + i))
        await asyncio.sleep(0.02)
    NR = 1100                                         # more texts than a viewer may have queued (1024)
    rows2 = [{'steps': 400000 + i * 1000, 'ret': 1.0} for i in range(NR)]
    for r in rows2:
        await P.send(json.dumps({'type': 'metrics', 'row': r}))
    st = await until_status(lambda s: s['viewers'] == 3, 5)
    check(st['viewers'] == 3 and st['counts'].get('viewers_closed_too_far_behind', 0) >= 1,
          'a viewer with over 1024 unsent text messages is closed as hopelessly behind', f"viewers {st['viewers']}")
    await until(lambda: all(len([m for m in R.texts() if m.get('type') == 'metrics'
                                 and m['row']['steps'] >= 400000]) >= NR for R in (A, B)), 5)
    for R in (A, B):
        r2 = [m['row'] for m in R.texts() if m.get('type') == 'metrics' and m['row']['steps'] >= 400000]
        f2 = [frame_k(b) for _, b in R.frames() if 1000 <= frame_k(b) < 1150]
        check(r2 == rows2 and f2 == list(range(1000, 1150)), f'{R.name} still got all 150 frames and {NR} rows',
              f'{len(f2)} frames, {len(r2)} rows')
    await until(lambda: len([m for m in C.texts() if m.get('type') == 'metrics'
                             and m['row']['steps'] >= 400000]) >= NR, 5)
    r2c = [m['row'] for m in C.texts() if m.get('type') == 'metrics' and m['row']['steps'] >= 400000]
    check(r2c == rows2 and not C.eof, f'the formerly slow viewer C got all {NR} rows and stays connected')
    D.mode = 'fast'
    await until(lambda: D.close_code is not None or D.eof, 12)
    check(D.close_code == 1013, 'the stuck viewer is told 1013 (try again later) once it reads', f'close {D.close_code}')

    section('publisher caps and junk')
    mark = len(A.msgs)
    await P.send(make_frame(7777, size=4100))                      # over 4096 bytes
    await P.send(make_frame(7778, magic=6.0))                      # wrong magic
    await P.send(b'\x00\x00')                                      # not a frame
    await P.send(json.dumps({'type': 'metrics', 'row': {'steps': 900000, 'pad': 'x' * 70000}}))   # over 64 KB
    await P.send(json.dumps({'type': 'episode', 'n': 2, 'pad': 'y' * 9000}))  # an episode over 8 KB (kept in state)
    await P.send('not json at all')
    await P.send('[' * 60000)                                                 # nested too deep for the parser
    await P.send(json.dumps({'type': 'state', 'live': False, 'hello': None, 'history': []}))    # relay-only
    await P.send(json.dumps({'type': 'idle'}))                                                  # relay-only
    await P.send(json.dumps(['hello']))
    await P.send(make_frame(5000))
    ep = {'type': 'episode', 'n': 3, 'presses': 2, 'hits': 1, 'misses': 1, 'fell': False}
    await P.send(json.dumps(ep))
    await until(lambda: A.find_text(lambda d: d == ep, mark) is not None, 3)
    bins = [frame_k(b) for _, b in A.frames(mark)]
    txts = A.texts(mark)
    check(bins == [5000], 'oversized (>4096 B), wrong-magic and truncated frames are not forwarded', str(bins))
    check(txts == [ep], 'oversized (>64 KB, or an episode >8 KB), non-JSON, too-deeply-nested, non-object and '
          'relay-only texts are not forwarded', str([t.get('type') for t in txts]))
    st = await status()
    c = st['counts']
    check(c.get('dropped_frame_too_big') == 1 and c.get('dropped_bad_frame') == 2 and c.get('dropped_text_too_big') == 1
          and c.get('dropped_part_too_big') == 1 and c.get('dropped_bad_json') == 2
          and c.get('dropped_unknown_type') == 3 and not c.get('dropped_error'),
          'each dropped message is counted in /status', json.dumps({k: v for k, v in c.items() if k.startswith('dropped')}))
    check(st['live'] is True and st['episode'] == ep and st['publisher'] is not None,
          'the publisher stays connected and live after junk')

    section('the state message stays small enough for the site (site/js/live.js drops text over 65,536)')
    HELLO3 = dict(HELLO, run='test_run_big', task='steer', started='2026-09-24T00:05:00Z')
    big = [{'steps': 500000 + i, 'note': 'x' * 400} for i in range(300)]
    await P.send(json.dumps(HELLO3))
    for r in big:
        await P.send(json.dumps({'type': 'metrics', 'row': r}))
    await until_status(lambda s: s['history_rows'] == 300 and s['metrics'] == big[-1], 3)
    L = await Rec('L').start()
    await until(lambda: len(L.msgs) >= 1, 3)
    raw = L.msgs[0][1] if L.msgs else ''
    s0 = strict(raw) if isinstance(raw, str) else {}
    hist = s0.get('history') or []
    n = len(hist)
    check(isinstance(raw, str) and len(raw.encode('utf-8')) <= 60000 and s0.get('type') == 'state'
          and s0.get('hello') == HELLO3 and s0.get('checkpoint') is None,
          'with 300 rows of ~430 bytes the state still stays under 60,000 bytes', f'{len(raw)} bytes')
    check(0 < n < 300 and hist == big[-n:], 'it carries the newest rows that fit, in order', f'{n} of 300 rows')
    st = await status()
    check(st['history_rows'] == 300 and st['state_rows'] == n, '/status: 300 rows kept, state_rows says how many fit',
          f"{st['history_rows']} kept, {st['state_rows']} in the state")
    await L.close()
    await P.send(json.dumps(HELLO))                   # back to the first run (a new hello: a fresh, empty history)
    st = await until_status(lambda s: s['hello'] == HELLO, 3)
    check(st['history_rows'] == 0 and st['checkpoint'] is None and st['episode'] is None,
          'a hello for a different run starts from nothing')
    await until_status(lambda s: s['viewers'] == 3, 3)

    section('viewer caps: receive-only, pings, size, flood, count')
    E = await Rec('E').start()
    await until(lambda: len(E.msgs) >= 1, 3)
    await E.ws.send('ping')
    await E.ws.send(json.dumps({'type': 'ping'}))
    await until(lambda: len([m for m in E.texts() if m.get('type') == 'pong']) >= 2, 3)
    check(len([m for m in E.texts() if m.get('type') == 'pong']) == 2, 'a viewer\'s "ping" (text or JSON) gets a pong')
    mark = len(A.msgs)
    await E.ws.send(json.dumps({'type': 'hello', 'source': 'training', 'task': 'lever', 'run': 'evil'}))
    await E.ws.send(json.dumps({'type': 'idle'}))
    await asyncio.sleep(0.4)
    check(A.texts(mark) == [] and not E.done, 'anything a viewer sends besides a ping is ignored, never broadcast')
    await E.ws.send(b'\x01\x02\x03')
    await until(lambda: E.done, 3)
    check(E.code == 1003, 'a viewer sending binary data is closed (1003)', f'close {E.code}')
    E2 = await Rec('E2').start()
    await E2.ws.send('x' * 600)
    await until(lambda: E2.done, 3)
    check(E2.code == 1009, 'a viewer sending an oversized message is closed (1009)', f'close {E2.code}')
    E3 = await Rec('E3').start()
    for _ in range(30):
        try:
            await E3.ws.send('ping')
        except ConnectionClosed:
            break
    await until(lambda: E3.done, 3)
    check(E3.code == 1008, 'a viewer flooding pings is closed (1008)', f'close {E3.code}')
    st = await until_status(lambda s: s['viewers'] == 3, 3)
    fills = [await Rec(f'F{i}').start() for i in range(6 - st['viewers'])]
    await until(lambda: all(len(f.msgs) >= 1 for f in fills), 3)
    X = await Rec('X').start()
    await until(lambda: X.done, 3)
    check(X.code == 1013 and not X.msgs, 'a viewer beyond RELAY_MAX_VIEWERS (6 here, 500 by default) is closed 1013',
          f'close {X.code}, {len(fills)} fillers')
    for f in fills:
        await f.close()
    await until_status(lambda s: s['viewers'] == 3, 3)

    section('per-address cap on /live (RELAY_MAX_PER_IP, 2 here, 8 by default)')
    # the proxy's headers name the client: X-Real-IP, else the RIGHT-most X-Forwarded-For entry (the one the proxy
    # appended; the left-most is whatever the client sent). Loopback clients (A, B, C above) are never capped.
    same = [await Rec(f'I{i}').start(additional_headers={'X-Forwarded-For': '8.8.4.4'}) for i in range(2)]
    await until(lambda: all(len(r.msgs) >= 1 for r in same), 3)
    forged = await Rec('I2').start(additional_headers={'X-Forwarded-For': '9.9.9.9, 8.8.4.4'})
    await until(lambda: forged.done, 3)
    reason = forged.ws.close_reason or ''
    check(forged.code == 1013 and not forged.msgs and 'address' in reason,
          'a third socket from one address is closed 1013, even with a forged left-most X-Forwarded-For entry',
          f'close {forged.code} {reason!r}')
    real = await Rec('I3').start(additional_headers={'X-Real-IP': '8.8.4.4', 'X-Forwarded-For': '1.0.0.1'})
    await until(lambda: real.done, 3)
    check(real.code == 1013, 'X-Real-IP (set by the proxy) is used first', f'close {real.code}')
    other = await Rec('I4').start(additional_headers={'X-Forwarded-For': '8.8.4.4, 1.0.0.1'})
    await until(lambda: len(other.msgs) >= 1 or other.done, 3)
    check(not other.done and other.msgs, 'another address still gets in')
    st = await status()
    check(st['counts'].get('viewers_refused_per_ip') == 2 and st['viewers'] == 6 and st['max_viewers_per_address'] == 2,
          '/status counts the per-address refusals', f"viewers {st['viewers']}")
    await same[0].close()
    await until_status(lambda s: s['viewers'] == 5, 3)
    again = await Rec('I5').start(additional_headers={'X-Forwarded-For': '8.8.4.4'})
    await until(lambda: len(again.msgs) >= 1 or again.done, 3)
    check(not again.done and again.msgs, "a closed socket frees its address's slot")
    for r in (same[1], other, again):
        await r.close()
    await until_status(lambda s: s['viewers'] == 3, 3)

    section('one publisher at a time; idle; resume; replacement')
    await P.send(make_frame(5999))            # fresh, whatever the sections above took
    code, body = await refused_status(PUB, AUTH)
    check(code == 409, 'a second publisher is refused while the first is streaming', f'HTTP {code}: {body}')
    mark = len(A.msgs)
    await P.send(make_frame(6000))
    t_quiet = time.perf_counter()
    await until(lambda: A.find_text(lambda d: d.get('type') == 'idle', mark) is not None, 20, step=0.05)
    hit = A.find_text(lambda d: d.get('type') == 'idle', mark)
    dt = (hit[1] - t_quiet) if hit else -1
    check(hit and hit[2].get('reason') == 'quiet' and 15.0 <= dt <= 16.5,
          'a publisher that sends nothing for 15 s: viewers get idle', f'after {dt:.2f} s')
    st = await status()
    check(st['live'] is False and st['hello'] == HELLO and st['publisher'] is not None and st['fps_in'] == 0,
          '/status after idle: not live, still shows the last hello')
    G = await Rec('G').start()
    await until(lambda: len(G.msgs) >= 1, 3)
    await asyncio.sleep(0.2)                  # give a (wrong) frame time to arrive
    g0 = G.texts()[0] if G.msgs else {}
    check(g0.get('live') is False and g0.get('hello') == HELLO and len(G.msgs) == 1,
          'a viewer joining now gets state live=false (the last hello, no frame)')
    await G.close()
    mark = len(A.msgs)
    await P.send(make_frame(6001))
    await until(lambda: any(isinstance(m, bytes) and frame_k(m) == 6001 for _, m in A.msgs[mark:]), 3)
    seq = A.msgs[mark:]
    ok = (len(seq) >= 2 and isinstance(seq[0][1], str) and strict(seq[0][1]).get('type') == 'state'
          and strict(seq[0][1]).get('live') is True and seq[1][1] == make_frame(6001))
    check(ok, 'when the quiet publisher sends again: a fresh state (live=true), then the frame')
    code, _ = await refused_status(PUB, AUTH)
    check(code == 409, 'and it holds the publisher slot again', f'HTTP {code}')
    mark = len(A.msgs)
    await until(lambda: A.find_text(lambda d: d.get('type') == 'idle', mark) is not None, 20, step=0.05)
    P2 = await connect(PUB, additional_headers=AUTH, open_timeout=5)
    check(True, 'a new publisher replaces one that has been quiet for 15 s')
    code = await close_code_of(P, 5)
    check(code == 4001, 'the replaced publisher is closed (4001)', f'close {code}')
    mark = len(A.msgs)
    await P2.send(json.dumps(HELLO2))
    await until(lambda: A.find_text(lambda d: d == HELLO2, mark) is not None, 3)
    st = await status()
    check(st['live'] is True and st['hello'] == HELLO2 and st['metrics'] is None and st['history_rows'] == 0
          and st['checkpoint'] is None and st['episode'] is None,
          'a different run: live, the new hello, its history starts empty')
    for k in range(3):
        await P2.send(make_frame(8000 + k))
    await P2.send(json.dumps({'type': 'bye'}))
    await until(lambda: A.find_text(lambda d: d.get('type') == 'idle', mark) is not None, 3)
    seq = [('bin', frame_k(m)) if isinstance(m, bytes) else ('text', strict(m).get('type'))
           for _, m in A.msgs[mark:]]
    check(seq == [('text', 'hello'), ('bin', 8000), ('bin', 8001), ('bin', 8002), ('text', 'bye'), ('text', 'idle')],
          'hello, frames, bye, then idle: in order', str(seq))
    st = await status()
    check(st['live'] is False and st['publisher'] is not None, 'after bye: not live, publisher still connected')
    mark = len(A.msgs)
    await P2.send(make_frame(8003))                               # after bye, before a new hello: dropped
    await P2.send(json.dumps(HELLO2))                             # --watch: the next run on the same connection
    await until(lambda: A.find_text(lambda d: d == HELLO2, mark) is not None, 3)
    await asyncio.sleep(0.2)
    check(not A.frames(mark) and (await status())['live'] is True,
          'a frame between bye and the next hello is dropped; the next hello is live again')
    mark = len(A.msgs)
    t_close = time.perf_counter()
    await P2.close()
    await until(lambda: A.find_text(lambda d: d.get('type') == 'idle', mark) is not None, 3)
    hit = A.find_text(lambda d: d.get('type') == 'idle', mark)
    dt = (hit[1] - t_close) if hit else -1
    check(hit and hit[2].get('reason') == 'disconnected' and dt < 1.0,
          'the publisher disconnecting: viewers get idle at once', f'after {dt * 1000:.0f} ms')
    st = await status()
    check(st['live'] is False and st['publisher'] is None, '/status: not live, no publisher')

    section('honesty: the relay only carries live training')
    P3 = await connect(PUB, additional_headers=AUTH, open_timeout=5)
    mark = len(A.msgs)
    await P3.send(json.dumps(dict(HELLO, source='replay')))
    code = await close_code_of(P3, 5)
    await asyncio.sleep(0.2)
    st = await status()
    check(code == 1008 and st['live'] is False and A.texts(mark) == [],
          'a hello with source "replay" is refused (1008) and never shown as live', f'close {code}')
    test_label = "TEST (not a live training run): test_run_1's last saved checkpoint, playing in its own simulation"
    for hello, what in [(dict(HELLO, test=True), '"test": true'), (dict(HELLO, label=test_label), 'a TEST label')]:
        P4 = await connect(PUB, additional_headers=AUTH, open_timeout=5)
        mark = len(A.msgs)
        await P4.send(json.dumps(hello))
        await P4.send(make_frame(9000))
        code = await close_code_of(P4, 5)
        reason = P4.close_reason or ''
        await asyncio.sleep(0.2)
        st = await status()
        check(code == 1008 and 'RELAY_ALLOW_TEST' in reason and st['live'] is False and A.msgs[mark:] == [],
              f'a test stream ({what}) is refused (1008) and never shown as live', f'close {code} {reason!r}')
    check(not any(w in reason.lower() for w in ('auth', 'token')),
          'the refusal does not read as a token problem (publish_training.py stops for good on those)')
    for R in (A, B):
        await R.close()


async def other_relays():
    section('publishing disabled when LABRAT_PUBLISH_TOKEN is unset')
    relay = Relay({}, 'unset')
    try:
        code, body = await refused_status(PUB, AUTH)
        check(code == 503, '/publish is refused (503) even with a token', f'HTTP {code}: {body}')
        code, _ = await refused_status(PUB)
        check(code == 503, '/publish without a token is refused (503)', f'HTTP {code}')
        V = await Rec('V').start()
        await until(lambda: len(V.msgs) >= 1, 3)
        check(V.msgs and strict(V.msgs[0][1]).get('live') is False, 'viewers still connect and see not-live')
        await V.close()
        s, _, body = await asyncio.to_thread(http_get, '/')
        check(s == 200 and json.loads(body).get('service') == 'labrat relay',
              'without SERVE_SITE, / answers with a small JSON pointer')
    finally:
        relay.stop()
    section('a LABRAT_PUBLISH_TOKEN shorter than 16 characters counts as unset')
    relay = Relay({'LABRAT_PUBLISH_TOKEN': 'short'}, 'short')
    try:
        code, _ = await refused_status(PUB, {'Authorization': 'Bearer short'})
        check(code == 503, '/publish is refused (503) with a 5-character token configured', f'HTTP {code}')
    finally:
        relay.stop()
    section('RELAY_ALLOW_TEST=1 (a local relay only) lets a test stream through, labelled as such')
    relay = Relay({'LABRAT_PUBLISH_TOKEN': TOKEN, 'RELAY_ALLOW_TEST': '1'}, 'allowtest')
    try:
        V = await Rec('V').start()
        await until(lambda: len(V.msgs) >= 1, 3)
        P = await connect(PUB, additional_headers=AUTH, open_timeout=5)
        th = dict(HELLO, test=True, label='TEST (not a live training run): relay test')
        await P.send(json.dumps(th))
        await P.send(make_frame(1))
        await until(lambda: len(V.frames()) >= 1, 3)
        st = await status()
        check(st['allow_test_streams'] is True and st['live'] is True and st['hello'] == th
              and V.texts()[1:] == [th] and len(V.frames()) == 1,
              'the test hello (with "test": true and its TEST label) and its frames reach viewers')
        await P.close()
        await V.close()
    finally:
        relay.stop()
        check('TEST STREAMS ALLOWED' in relay.text(), 'the relay says so loudly when it starts')


async def amain():
    await main_relay()
    await other_relays()


def main():
    if port_busy():
        print(f'port {PORT} is already in use; stop whatever is on it first (this test will not touch it)')
        return 2
    t = time.time()
    asyncio.run(amain())
    failed = [label for ok, label in RESULTS if not ok]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed in {time.time() - t:.0f} s')
    for label in failed:
        print('  FAILED: ' + label)
    if not failed:
        for d in TEMP_DIRS:
            shutil.rmtree(d, ignore_errors=True)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
