"""End-to-end test of relay.py.

Starts the relay with the Procfile's own command line on 127.0.0.1:4762 (RELAY_TEST_PORT overrides it) and drives it
with a fake publisher and several viewers: two fast ones (the websockets client), a deliberately slow one and a stuck
one (hand-rolled websocket clients on a socket with a 4 KB receive buffer, reading at a pace we set), plus short-lived
ones for the caps. Then, on a fresh relay, the pons channel: a fake buy-rig publisher next to a training publisher.

    python relay/test_relay.py

Takes about two minutes (it waits out the real 15 s idle timer four times). Prints PASS/FAIL per check and exits
non-zero on any failure. It only ever stops the relay processes it started itself.
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
PORT = int(os.environ.get('RELAY_TEST_PORT') or 4762)
HOSTPORT = f'127.0.0.1:{PORT}'
PUB = f'ws://{HOSTPORT}/publish'
PONS_PUB = f'ws://{HOSTPORT}/publish?channel=pons'
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


def pons_frame(k, size=2048, magic=b'PJPG', soi=b'\xff\xd8', eoi=b'\xff\xd9'):
    """A pons-channel frame: b"PJPG" + a stand-in JPEG (SOI, a comment marker carrying k, padding, EOI). The relay
    checks the prefix and the JPEG's first and last two bytes; it never decodes the picture."""
    head = magic + soi + b'\xff\xfe' + struct.pack('>I', k)
    return head + b'\x00' * max(0, size - len(head) - len(eoi)) + eoi


def pons_k(b):
    return struct.unpack_from('>I', b, 8)[0]


def is_pons(b):
    return isinstance(b, bytes) and b[:4] == b'PJPG'


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
        code, _ = await refused_status(PONS_PUB, AUTH)
        check(code == 503, '/publish?channel=pons is refused (503) too', f'HTTP {code}')
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
        PQ = await connect(PONS_PUB, additional_headers=AUTH, open_timeout=5)
        tp = dict(PHELLO, test=True)
        await PQ.send(json.dumps(tp))
        await PQ.send(pons_frame(1))
        await until(lambda: any(is_pons(m) for _, m in V.msgs), 3)
        st = await status()
        check(st['pons']['live'] is True and st['pons']['hello'] == as_pons(tp) and as_pons(tp) in V.texts()
              and [m for _, m in V.msgs if is_pons(m)] == [pons_frame(1)],
              'a pons test session ("test": true) reaches viewers, marked as a test')
        await PQ.close()
        await P.close()
        await V.close()
    finally:
        relay.stop()
        check('TEST STREAMS ALLOWED' in relay.text(), 'the relay says so loudly when it starts')


PHELLO = {'type': 'pons_hello', 'source': 'buyrig', 'session': 'buy-0001', 'started': '2026-09-25T12:00:00Z',
          'amount_eth': '0.0001', 'targets': ['Amount field', 'Buy LABRAT', 'Confirm buy'], 'simulated': True}
PHELLO2 = dict(PHELLO, session='buy-0002', started='2026-09-25T12:10:00Z')


def as_pons(d):
    return dict(d, channel='pons')


async def pons_relay():
    relay = Relay({'LABRAT_PUBLISH_TOKEN': TOKEN}, 'pons')
    closers = []
    try:
        await pons_checks(closers)
    finally:
        for c in closers:
            try:
                await c()
            except Exception:
                pass
        relay.stop()
        log = relay.text()
    check('Traceback' not in log and 'Exception in ASGI' not in log, 'the relay logged no exceptions (pons channel)',
          f'log: {relay.logpath}')
    if 'Traceback' in log or 'Exception in ASGI' in log:
        print(log[-4000:])


async def pons_checks(closers):
    section('pons channel: auth, channels and one publisher per channel')
    for hdrs, what in [(None, 'no Authorization header'), ({'Authorization': 'Bearer nope-nope-nope-nope'}, 'a wrong token')]:
        code, _ = await refused_status(PONS_PUB, hdrs)
        check(code == 401, f'/publish?channel=pons refuses {what}', f'HTTP {code}')
    code, body = await refused_status(f'ws://{HOSTPORT}/publish?channel=nope', AUTH)
    check(code == 400, '/publish with an unknown channel is refused (400)', f'HTTP {code}: {body}')
    A = await Rec('A').start()
    closers.append(A.close)
    await asyncio.sleep(0.4)
    check(len(A.msgs) == 1 and A.texts()[0].get('type') == 'state',
          'a viewer gets no pons message before the pons channel has had a session', f'{len(A.msgs)} messages')
    T = await connect(PUB, additional_headers=AUTH, open_timeout=5)
    closers.append(T.close)
    await T.send(json.dumps(HELLO))
    PP = await connect(PONS_PUB, additional_headers=AUTH, open_timeout=5)
    closers.append(PP.close)
    check(True, 'a pons publisher is accepted while the training publisher streams')
    code, _ = await refused_status(PONS_PUB, AUTH)
    check(code == 409, 'a second pons publisher is refused while the first is fresh (409)', f'HTTP {code}')
    code, _ = await refused_status(PUB, AUTH)
    check(code == 409, 'the training slot is still its own (a second training publisher: 409)', f'HTTP {code}')

    section('pons channel: forwarding, and the training channel unchanged')
    await until(lambda: A.find_text(lambda d: d == HELLO) is not None, 3)
    mark = len(A.msgs)
    await PP.send(pons_frame(1))                                         # before the pons_hello: dropped
    await PP.send(json.dumps({'type': 'pons_step', 'i': 1, 'n': 3}))     # before the pons_hello: dropped
    await PP.send(json.dumps(PHELLO))
    await until(lambda: A.find_text(lambda d: d.get('type') == 'pons_hello', mark) is not None, 3)
    got = A.texts(mark)
    check(got == [as_pons(PHELLO)], 'the pons_hello is forwarded with "channel":"pons" added; nothing sent before it',
          str([t.get('type') for t in got]))
    row_text = json.dumps({'type': 'metrics', 'row': {'steps': 1000, 'ret': 1.5}})
    step1 = {'type': 'pons_step', 'session': 'buy-0001', 'i': 1, 'n': 3, 'target': 'Amount field', 'phase': 'aim'}
    mark = len(A.msgs)
    await T.send(row_text)
    await T.send(make_frame(1))
    await PP.send(json.dumps(step1))
    await PP.send(pons_frame(10))
    await until(lambda: any(is_pons(m) for _, m in A.msgs[mark:]), 3)
    seq = A.msgs[mark:]
    raw_texts = [m for _, m in seq if isinstance(m, str)]
    check(row_text in raw_texts, 'a training text is forwarded verbatim (no channel added)')
    check(any(m == make_frame(1) for _, m in seq), 'a training frame is forwarded byte for byte')
    check(as_pons(step1) in [strict(m) for m in raw_texts], 'a pons_step is forwarded with "channel":"pons"')
    check([m for _, m in seq if is_pons(m)] == [pons_frame(10)], 'a pons frame is forwarded byte for byte, PJPG prefix kept')
    big = pons_frame(11, size=200 * 1024)
    mark = len(A.msgs)
    await PP.send(big)
    await until(lambda: any(is_pons(m) for _, m in A.msgs[mark:]), 3)
    check([m for _, m in A.msgs[mark:] if is_pons(m)] == [big],
          'a 200 KB pons frame (over the old 128 KB socket limit) is forwarded intact')

    section('pons channel: caps and junk')
    await asyncio.sleep(0.3)                                             # the per-viewer frame gap
    mark = len(A.msgs)
    await PP.send(pons_frame(20, size=256 * 1024 + 1))                   # over 256 KB
    await PP.send(pons_frame(21, magic=b'XJPG'))                         # no PJPG prefix
    await PP.send(pons_frame(22, soi=b'\x89P'))                          # not a JPEG
    await PP.send(pons_frame(23, eoi=b'\x00\x00'))                       # a JPEG cut short
    await PP.send(make_frame(24))                                        # a training frame on the pons channel
    await PP.send(json.dumps(dict(step1, target='0x4C26…b893')))         # an address-like string
    await PP.send('{"type":"pons_step","target":"\\u0030x4c26ab"}')      # the same, hidden in a JSON escape
    await PP.send(json.dumps(HELLO))                                     # a training type on the pons channel
    await PP.send(json.dumps(dict(step1, pad='x' * 5000)))               # over 4 KB
    await PP.send('{"type":"pons_step",')                                # not JSON
    await PP.send(json.dumps({'type': 'pons_state', 'live': True}))      # relay-only
    step2 = dict(step1, i=2, target='Buy LABRAT', phase='press')
    await PP.send(json.dumps(step2))
    await PP.send(pons_frame(25))
    await until(lambda: any(is_pons(m) for _, m in A.msgs[mark:]), 3)
    seq = A.msgs[mark:]
    check([pons_k(m) for _, m in seq if is_pons(m)] == [25] and not [m for _, m in seq if isinstance(m, bytes)
                                                                      and not is_pons(m)],
          'oversized, unprefixed, non-JPEG and truncated pons frames (and a rat frame) are not forwarded')
    check([strict(m) for _, m in seq if isinstance(m, str)] == [as_pons(step2)],
          'address-like (even JSON-escaped), oversized, non-JSON, training-type and relay-only pons texts are dropped')
    st = await status()
    c = st['counts']
    check(c.get('pons_dropped_frame_too_big') == 1 and c.get('pons_dropped_bad_frame') == 4
          and c.get('pons_dropped_address') == 2 and c.get('pons_dropped_unknown_type') == 2
          and c.get('pons_dropped_text_too_big') == 1 and c.get('pons_dropped_bad_json') == 1
          and c.get('pons_dropped_outside_session') == 2 and not c.get('dropped_error')
          and not any(k.startswith('dropped_') for k in c),
          'each dropped pons message is counted under pons_* (and none under the training counters)',
          json.dumps({k: v for k, v in c.items() if 'dropped' in k}))
    check(st['pons']['live'] is True and st['pons']['publisher'] is not None and st['live'] is True,
          'both publishers stay connected and live after pons junk')

    section('pons frames: at most 5 per second per viewer, newest wins; training frames unaffected')
    await asyncio.sleep(0.3)
    mark = len(A.msgs)
    fin0 = st['pons']['frames_in']

    async def pons_burst():
        t0 = time.perf_counter()
        for i in range(60):                                              # 20 fps for 3 s
            d = t0 + i * 0.05 - time.perf_counter()
            if d > 0:
                await asyncio.sleep(d)
            await PP.send(pons_frame(1000 + i, size=60 * 1024))

    async def train_burst():
        t0 = time.perf_counter()
        for i in range(75):                                              # 25 fps for 3 s
            d = t0 + i * 0.04 - time.perf_counter()
            if d > 0:
                await asyncio.sleep(d)
            await T.send(make_frame(2000 + i))
    await asyncio.gather(pons_burst(), train_burst())
    await asyncio.sleep(0.6)
    seq = A.msgs[mark:]
    tks = [frame_k(m) for _, m in seq if isinstance(m, bytes) and not is_pons(m)]
    check(tks == list(range(2000, 2075)), 'every training frame still arrives, in order (25 fps next to the pons frames)',
          f'{len(tks)}/75')
    pf = [(t, pons_k(m)) for t, m in seq if is_pons(m)]
    ks = [k for _, k in pf]
    gaps = [b[0] - a[0] for a, b in zip(pf, pf[1:])]
    span = pf[-1][0] - pf[0][0] if len(pf) > 1 else 0
    rate = (len(pf) - 1) / span if span > 0 else 0
    check(12 <= len(pf) <= 17 and rate <= 5.5, 'a viewer gets about 5 pons frames per second of the 20 sent',
          f'{len(pf)} of 60, {rate:.1f} fps')
    check(gaps and min(gaps) >= 0.12, 'pons frames reach a viewer spaced out (0.2 s apart by design)',
          f'min gap {min(gaps) * 1000 if gaps else -1:.0f} ms')
    check(ks == sorted(set(ks)) and ks and ks[-1] == 1059, 'in order, and the last one is the newest (older ones dropped)',
          f'last {ks[-1] if ks else "-"}')
    st = await status()
    check(st['pons']['frames_in'] - fin0 == 60 and st['counts'].get('pons_frames_skipped_for_rate', 0) > 0
          and st['pons']['max_frame_bytes'] == 256 * 1024 and st['pons']['viewer_fps'] == 5,
          '/status pons: frames_in counts all 60, the skipped ones are counted, caps shown',
          f"frames_in +{st['pons']['frames_in'] - fin0}, skipped {st['counts'].get('pons_frames_skipped_for_rate')}")

    section('pons channel: a late joiner, and /status')
    res = {'type': 'pons_result', 'session': 'buy-0001', 'ok': True, 'simulated': True, 'eth_in': '0.0001',
           'labrat_out': '792.7593'}
    await PP.send(json.dumps(res))
    await T.send(make_frame(3000))
    await until(lambda: A.find_text(lambda d: d.get('type') == 'pons_result') is not None, 3)
    await asyncio.sleep(0.1)
    B = await Rec('B').start()
    closers.append(B.close)
    await until(lambda: len(B.msgs) >= 4, 3)
    m = [x for _, x in B.msgs[:4]]
    ok = (len(m) == 4 and isinstance(m[0], str) and strict(m[0]).get('type') == 'state' and strict(m[0]).get('live') is True
          and m[1] == make_frame(3000) and isinstance(m[2], str) and m[3] == pons_frame(1059, size=60 * 1024))
    check(ok, 'a late joiner gets: the training state, its last frame, the pons state, the last pons frame',
          str([('text', strict(x).get('type')) if isinstance(x, str) else ('pons' if is_pons(x) else 'frame') for x in m]))
    ps = strict(m[2]) if len(m) > 2 and isinstance(m[2], str) else {}
    check(ps == {'type': 'pons_state', 'channel': 'pons', 'live': True, 'hello': as_pons(PHELLO), 'step': as_pons(step2),
                 'result': as_pons(res)}, 'the pons state: live, with the session\'s hello, its latest step and result')
    st = await status()
    p = st['pons']
    check(p['live'] is True and p['hello'] == as_pons(PHELLO) and p['step'] == as_pons(step2) and p['result'] == as_pons(res)
          and p['publisher']['in_session'] is True and p['last_frame_bytes'] == 60 * 1024,
          '/status pons: live, hello, step, result, publisher, last frame size')

    section('pons bye, a new session, and a disconnect: the training stream never notices')
    mark = len(A.msgs)
    await PP.send(json.dumps({'type': 'pons_bye', 'session': 'buy-0001'}))
    await until(lambda: A.find_text(lambda d: d.get('type') == 'pons_idle', mark) is not None, 3)
    got = A.texts(mark)
    check(got == [{'type': 'pons_bye', 'session': 'buy-0001', 'channel': 'pons'},
                  {'type': 'pons_idle', 'channel': 'pons', 'reason': 'bye'}],
          'pons_bye: viewers get it, then pons_idle (bye), both marked "channel":"pons"', str(got))
    st = await status()
    check(st['pons']['live'] is False and st['pons']['hello'] == as_pons(PHELLO) and st['live'] is True,
          '/status after the bye: pons not live (last session kept), training still live')
    await T.send(make_frame(3001))
    G = await Rec('G').start()
    closers.append(G.close)
    await until(lambda: len(G.msgs) >= 4, 3)
    gp = [strict(x) for _, x in G.msgs if isinstance(x, str) and strict(x).get('type') == 'pons_state']
    gf = [x for _, x in G.msgs if is_pons(x)]
    check(gp and gp[0]['live'] is False and gp[0]['result'] == as_pons(res) and gf == [pons_frame(1059, size=60 * 1024)],
          'a viewer joining after the session: pons state live=false with its result, and the final frame')
    mark = len(A.msgs)
    await PP.send(pons_frame(4000))                                      # after the bye: dropped
    await PP.send(json.dumps(PHELLO2))
    await until(lambda: A.find_text(lambda d: d.get('type') == 'pons_hello', mark) is not None, 3)
    await asyncio.sleep(0.3)
    check(not [x for _, x in A.msgs[mark:] if is_pons(x)], 'a frame between the bye and the next hello is dropped')
    st = await status()
    check(st['pons']['live'] is True and st['pons']['step'] is None and st['pons']['result'] is None
          and st['pons']['last_frame_bytes'] == 0, 'a new session starts from nothing (no step, result or frame)')
    Hn = await Rec('H').start()
    closers.append(Hn.close)
    await until(lambda: any(isinstance(x, str) and strict(x).get('type') == 'pons_state' for _, x in Hn.msgs), 3)
    await asyncio.sleep(0.4)
    hp = [strict(x) for _, x in Hn.msgs if isinstance(x, str) and strict(x).get('type') == 'pons_state']
    check(hp and hp[0]['live'] is True and hp[0]['hello'] == as_pons(PHELLO2) and not [x for _, x in Hn.msgs if is_pons(x)],
          'a viewer joining now gets the new session\'s state and no frame from the old one')
    mark = len(A.msgs)
    t_close = time.perf_counter()
    await PP.close()
    await until(lambda: A.find_text(lambda d: d.get('type') == 'pons_idle', mark) is not None, 3)
    hit = A.find_text(lambda d: d.get('type') == 'pons_idle', mark)
    dt = (hit[1] - t_close) if hit else -1
    check(hit and hit[2] == {'type': 'pons_idle', 'channel': 'pons', 'reason': 'disconnected'} and dt < 1.0,
          'the buy rig disconnecting: viewers get pons_idle (disconnected) at once', f'after {dt * 1000:.0f} ms')
    await asyncio.sleep(0.2)
    st = await status()
    check(st['live'] is True and not A.find_text(lambda d: d.get('type') in ('idle', 'bye'), 0)
          and st['pons']['publisher'] is None,
          'the training stream stayed live throughout; no training idle or bye was sent')

    section('pons channel honesty: only the buy rig, no test sessions')
    for hello, what, want in [(dict(PHELLO, source='training'), 'a pons_hello with source "training"', 'buyrig'),
                              (dict(PHELLO, test=True), 'a test session ("test": true)', 'RELAY_ALLOW_TEST'),
                              (dict(PHELLO, label='TEST session'), 'a TEST label', 'RELAY_ALLOW_TEST')]:
        Q = await connect(PONS_PUB, additional_headers=AUTH, open_timeout=5)
        mark = len(A.msgs)
        await Q.send(json.dumps(hello))
        await Q.send(pons_frame(5000))
        code = await close_code_of(Q, 5)
        reason = Q.close_reason or ''
        await asyncio.sleep(0.2)
        st = await status()
        check(code == 1008 and want in reason and st['pons']['live'] is False and A.msgs[mark:] == [],
              f'{what} is refused (1008) and never shown', f'close {code} {reason!r}')
    Q = await connect(PONS_PUB, additional_headers=AUTH, open_timeout=5)
    closers.append(Q.close)
    await Q.send(json.dumps(dict(PHELLO, session='')))
    await Q.send(json.dumps(dict(PHELLO, session='x' * 65)))
    await asyncio.sleep(0.3)
    st = await status()
    check(st['pons']['live'] is False and st['counts'].get('pons_dropped_bad_hello') == 2,
          'a pons_hello without a short session id is dropped (not live)')
    await Q.close()

    section('pons channel: quiet for 15 s -> idle; resume; a newer publisher replaces a quiet one')
    stop = asyncio.Event()

    async def keep_training():                                           # the training stream stays fresh meanwhile
        k = 6000
        while not stop.is_set():
            await T.send(make_frame(k))
            k += 1
            await asyncio.sleep(0.5)
    kt = asyncio.create_task(keep_training())
    try:
        P5 = await connect(PONS_PUB, additional_headers=AUTH, open_timeout=5)
        closers.append(P5.close)
        await P5.send(json.dumps(PHELLO))
        await until_status(lambda s: s['pons']['live'] is True, 3)
        mark = len(A.msgs)
        await P5.send(pons_frame(6000))
        t_quiet = time.perf_counter()
        await until(lambda: A.find_text(lambda d: d.get('type') == 'pons_idle', mark) is not None, 20, step=0.05)
        hit = A.find_text(lambda d: d.get('type') == 'pons_idle', mark)
        dt = (hit[1] - t_quiet) if hit else -1
        check(hit and hit[2].get('reason') == 'quiet' and 15.0 <= dt <= 16.5,
              'a buy rig that sends nothing for 15 s: viewers get pons_idle (quiet)', f'after {dt:.2f} s')
        st = await status()
        check(st['live'] is True and not A.find_text(lambda d: d.get('type') == 'idle', mark),
              'the training stream is not affected by the pons channel going quiet')
        mark = len(A.msgs)
        await P5.send(pons_frame(6001))
        await until(lambda: any(is_pons(x) and pons_k(x) == 6001 for _, x in A.msgs[mark:]), 3)
        seq = [x for _, x in A.msgs[mark:] if isinstance(x, str) or is_pons(x)]
        ok = (len(seq) >= 2 and isinstance(seq[0], str) and strict(seq[0]).get('type') == 'pons_state'
              and strict(seq[0]).get('live') is True and seq[1] == pons_frame(6001))
        check(ok, 'when the quiet buy rig sends again: a fresh pons_state (live), then the frame')
        mark = len(A.msgs)
        await until(lambda: A.find_text(lambda d: d.get('type') == 'pons_idle', mark) is not None, 20, step=0.05)
        P6 = await connect(PONS_PUB, additional_headers=AUTH, open_timeout=5)
        closers.append(P6.close)
        check(True, 'a new pons publisher replaces one that has been quiet for 15 s')
        code = await close_code_of(P5, 5)
        check(code == 4001, 'the replaced pons publisher is closed (4001)', f'close {code}')
        await P6.send(json.dumps(PHELLO2))
        st = await until_status(lambda s: s['pons']['live'] is True, 3)
        check(st['pons']['live'] is True and st['pons']['hello'] == as_pons(PHELLO2) and st['live'] is True,
              'the new pons publisher is live; training still live')
    finally:
        stop.set()
        await kt


TILES_HELLO = {'type': 'hello', 'source': 'training', 'task': 'tiles', 'run': 'tiles_run_1', 'label': 'relay test tiles',
               'fps': 25, 'started': '2026-09-25T13:00:00Z'}
TILES_HELLO2 = dict(TILES_HELLO, run='tiles_run_2', started='2026-09-25T13:30:00Z')


def tiles_snap(k, n_tiles=4, size=None):
    """A Rat Tiles board snapshot numbered k (its note_i). size: pad it to exactly that many bytes."""
    m = {'type': 'tiles', 't': round(k * 0.1, 3), 'song': 'ode_to_joy', 'speed': 0.12, 'lanes': 4,
         'cursor': [0.5, 0.62], 'tiles': [[k + i, i % 4, round(0.9 - 0.25 * i, 3), 0.2, 'up'] for i in range(n_tiles)],
         'note_i': k}
    s = json.dumps(m, separators=(',', ':'))
    if size is not None:
        m['pad'] = ''
        base = len(json.dumps(m, separators=(',', ':')))
        m['pad'] = 'x' * (size - base)
        s = json.dumps(m, separators=(',', ':'))
        assert len(s) == size, (len(s), size)
    return s


def tile_ev(k, result='hit', size=None):
    m = {'type': 'tile', 'id': k, 'lane': k % 4, 'result': result, 'note_i': k, 'song': 'ode_to_joy'}
    if size is not None:
        m['pad'] = ''
        m['pad'] = 'y' * (size - len(json.dumps(m, separators=(',', ':'))))
    return json.dumps(m, separators=(',', ':'))


def tkind(text):
    """('tiles', k) / ('tile', k) for a Rat Tiles text, else None."""
    d = strict(text)
    if d.get('type') == 'tiles':
        return 'tiles', d.get('note_i')
    if d.get('type') == 'tile':
        return 'tile', d.get('note_i')
    return None


async def tiles_relay():
    relay = Relay({'LABRAT_PUBLISH_TOKEN': TOKEN}, 'tiles')
    closers, raws = [], []
    try:
        await tiles_checks(closers, raws)
    finally:
        for r in raws:
            r.shutdown()
        for c in closers:
            try:
                await c()
            except Exception:
                pass
        relay.stop()
        log = relay.text()
    check('Traceback' not in log and 'Exception in ASGI' not in log, 'the relay logged no exceptions (Rat Tiles)',
          f'log: {relay.logpath}')
    if 'Traceback' in log or 'Exception in ASGI' in log:
        print(log[-4000:])


async def tiles_checks(closers, raws):
    section('Rat Tiles: tiles / tile messages only in a run whose hello says task "tiles"')
    V = await Rec('V').start()
    closers.append(V.close)
    await until(lambda: len(V.msgs) >= 1, 3)
    P = await connect(PUB, additional_headers=AUTH, open_timeout=5)
    closers.append(P.close)
    await P.send(tiles_snap(0))                                      # before any hello: dropped
    await P.send(json.dumps(HELLO))                                  # a lever run
    await P.send(tiles_snap(1))
    await P.send(tile_ev(1))
    await until(lambda: V.find_text(lambda d: d == HELLO) is not None, 3)
    await asyncio.sleep(0.3)
    st = await status()
    c = st['counts']
    check(not [m for m in V.texts() if m.get('type') in ('tiles', 'tile')] and c.get('dropped_outside_session') == 1
          and c.get('dropped_tiles_wrong_task') == 2 and st['tiles'] is None,
          'a snapshot before any hello, and tiles / tile in a lever run, are dropped and counted',
          json.dumps({k: v for k, v in c.items() if 'dropped' in k}))
    mark = len(V.msgs)
    await P.send(json.dumps(TILES_HELLO))
    rows = [{'steps': 1000 * (i + 1), 'ret': 1.0 + i, 'hits': 2 + i} for i in range(3)]
    for r in rows:
        await P.send(json.dumps({'type': 'metrics', 'row': r}))
    await until(lambda: V.find_text(lambda d: d == TILES_HELLO, mark) is not None, 3)
    st = await status()
    check(st['live'] is True and st['hello'] == TILES_HELLO, 'a hello with task "tiles" is accepted and live')

    section('Rat Tiles: snapshots at 10 Hz and tile events reach viewers verbatim, in the order sent')
    mark = len(V.msgs)
    sent = []
    t0 = time.perf_counter()
    for k in range(30):
        d = t0 + k * 0.1 - time.perf_counter()
        if d > 0:
            await asyncio.sleep(d)
        s = tiles_snap(100 + k)
        await P.send(s)
        await P.send(make_frame(100 + k))
        sent.append(s)
        if k % 3 == 2:
            e = tile_ev(100 + k, 'hit' if k % 2 else 'miss')
            await P.send(e)
            sent.append(e)
    await until(lambda: [m for _, m in V.msgs[mark:] if isinstance(m, str)] == sent, 3)
    got = [m for _, m in V.msgs[mark:] if isinstance(m, str)]
    check(got == sent, 'all 30 snapshots and 10 tile events arrive, byte for byte, in order', f'{len(got)}/{len(sent)}')
    check([frame_k(m) for _, m in V.msgs[mark:] if isinstance(m, bytes)] == list(range(100, 130)),
          'the rat frames in between are unaffected')

    section('Rat Tiles: caps and junk')
    mark = len(V.msgs)
    await P.send(tiles_snap(200, size=4097))                         # over 4 KB
    await P.send(tile_ev(201, size=513))                             # over 512 B
    await P.send(json.dumps({'type': 'tiles', 't': 1.0, 'song': 'ode_to_joy', 'tiles': 'not a list'}))
    await P.send(json.dumps({'type': 'tiles', 't': 1.0, 'song': 'ode_to_joy'}))
    await P.send(tile_ev(202, 'maybe'))
    await P.send(json.dumps({'type': 'tile', 'id': 203}))
    ok_snap, ok_ev = tiles_snap(204, size=4096), tile_ev(205, 'wrong', size=512)
    await P.send(ok_snap)
    await P.send(ok_ev)
    await until(lambda: any(m == ok_ev for _, m in V.msgs[mark:]), 3)
    got = [m for _, m in V.msgs[mark:] if isinstance(m, str)]
    check(got == [ok_snap, ok_ev], 'a 4,096-byte snapshot and a 512-byte event pass; bigger ones, a snapshot without a '
          'tiles list and an event without a hit / miss / wrong result are dropped', str([tkind(m) for m in got]))
    c = (await status())['counts']
    check(c.get('dropped_tiles_too_big') == 1 and c.get('dropped_tile_too_big') == 1 and c.get('dropped_bad_tiles') == 2
          and c.get('dropped_bad_tile') == 2 and not c.get('dropped_error'), 'each is counted in /status',
          json.dumps({k: v for k, v in c.items() if 'tile' in k}))

    section('Rat Tiles: at most 12 snapshots a second per viewer; tile events are never dropped')
    await asyncio.sleep(0.5)                                         # the per-viewer burst refills
    mark = len(V.msgs)
    c0 = (await status())['counts'].get('tiles_dropped_for_rate', 0)
    N = 90
    t0 = time.perf_counter()
    for k in range(N):                                               # 30 snapshots a second for 3 s
        d = t0 + k / 30 - time.perf_counter()
        if d > 0:
            await asyncio.sleep(d)
        await P.send(tiles_snap(1000 + k))
        await P.send(tile_ev(1000 + k))
    span = time.perf_counter() - t0
    await until(lambda: any(tkind(m) == ('tile', 1000 + N - 1) for _, m in V.msgs[mark:] if isinstance(m, str)), 3)
    seq = [(t, tkind(m)) for t, m in V.msgs[mark:] if isinstance(m, str)]
    evs = [k for _, (kind, k) in seq if kind == 'tile']
    snaps = [(t, k) for t, (kind, k) in seq if kind == 'tiles']
    check(evs == list(range(1000, 1000 + N)), f'all {N} tile events arrive, in order', f'{len(evs)}/{N}')
    lo, hi = int(12 * span) - 2, int(12 * span + 3) + 1
    check(lo <= len(snaps) <= hi, f'{len(snaps)} of {N} snapshots reach the viewer in {span:.2f} s (12 a second, '
          f'plus a burst of 3)', f'allowed {lo}..{hi}')
    win = max((sum(1 for u, _ in snaps if t <= u < t + 1.0) for t, _ in snaps), default=0)
    check(win <= 15, 'no one-second window carries more than 12 + the burst of 3', f'max {win} in a second')
    ks = [k for _, k in snaps]
    order = [kind_k for _, kind_k in seq]
    check(ks == sorted(ks) and all(order.index(('tiles', k)) + 1 == order.index(('tile', k)) for k in ks),
          'the snapshots that pass stay in order, each straight ahead of its own tile event')
    c = (await status())['counts']
    check(c.get('tiles_dropped_for_rate', 0) - c0 == N - len(snaps), '/status counts the snapshots dropped for rate',
          f"{c.get('tiles_dropped_for_rate', 0) - c0}")

    section('Rat Tiles: a late joiner gets the newest snapshot (never the events), outside the state message')
    await asyncio.sleep(0.5)
    last_snap, last_ev = tiles_snap(500), tile_ev(500)
    await P.send(make_frame(500))
    await P.send(last_snap)
    await P.send(last_ev)
    await until(lambda: any(m == last_ev for _, m in V.msgs), 3)
    L = await Rec('L').start()
    closers.append(L.close)
    await until(lambda: len(L.msgs) >= 3, 3)
    await asyncio.sleep(0.3)
    m = [x for _, x in L.msgs]
    s0 = strict(m[0]) if m and isinstance(m[0], str) else {}
    check(len(m) == 3 and s0.get('type') == 'state' and s0.get('live') is True and m[1] == make_frame(500)
          and m[2] == last_snap, 'it gets: the state, the last frame, then the newest snapshot, verbatim',
          str([('text', strict(x).get('type')) if isinstance(x, str) else 'frame' for x in m]))
    check(set(s0) == {'type', 'live', 'hello', 'checkpoint', 'episode', 'history'} and s0.get('history') == rows,
          'the state itself is unchanged: its history holds only the log rows')
    st = await status()
    check(st['tiles'] == {'song': 'ode_to_joy', 't': 50.0, 'speed': 0.12, 'note_i': 500, 'tiles': 4},
          '/status shows the newest snapshot\'s song, time, speed, note and tile count', json.dumps(st['tiles']))

    section('Rat Tiles: a slow viewer holds one unsent snapshot at most (the newest), in order with the events')
    D = RawViewer('D', 'stuck', rcvbuf=4096)
    raws.append(D)
    D.start()
    await until_status(lambda s: s['viewers'] == 3, 3)
    for i in range(150):                                             # fill D's socket and the relay's send buffer
        await P.send(make_frame(3000 + i))
        await asyncio.sleep(0.02)
    c0 = (await status())['counts'].get('tiles_replaced_for_slow_viewers', 0)
    vmark = len(V.msgs)
    sent = []
    t0 = time.perf_counter()
    for k in range(20):                                              # 10 Hz, as the publisher sends them
        d = t0 + k * 0.1 - time.perf_counter()
        if d > 0:
            await asyncio.sleep(d)
        s, e = tiles_snap(2000 + k), tile_ev(2000 + k)
        await P.send(s)
        await P.send(e)
        sent += [s, e]
    D.mode = 'fast'
    want_last = tile_ev(2019)
    await until(lambda: any(kind == 'text' and p == want_last for _, kind, p in D.msgs), 8)
    dt = [p for _, kind, p in D.msgs if kind == 'text' and tkind(p) and tkind(p)[1] >= 2000]
    it = iter(sent)
    subseq = all(any(x == y for y in it) for x in dt)
    d_snaps = [tkind(p)[1] for p in dt if tkind(p)[0] == 'tiles']
    d_evs = [tkind(p)[1] for p in dt if tkind(p)[0] == 'tile']
    check(d_evs == list(range(2000, 2020)), 'the slow viewer gets every tile event, in order', f'{len(d_evs)}/20')
    check(subseq and d_snaps and d_snaps[-1] == 2019 and len(d_snaps) < 20,
          'it gets fewer snapshots, ending on the newest, and everything in the order it was sent',
          f'{len(d_snaps)} of 20 snapshots, last {d_snaps[-1] if d_snaps else "-"}')
    c = (await status())['counts']
    check(c.get('tiles_replaced_for_slow_viewers', 0) > c0 and not D.eof,
          '/status counts the replaced snapshots; the slow viewer stays connected',
          f"{c.get('tiles_replaced_for_slow_viewers', 0) - c0} replaced")
    got_v = [m for _, m in V.msgs[vmark:] if isinstance(m, str)]
    check(got_v == sent, 'the fast viewer still got all 20 snapshots and 20 events', f'{len(got_v)}/40')

    section('Rat Tiles: after bye, and in the next run, no old snapshot is replayed')
    await P.send(json.dumps({'type': 'bye'}))
    await until(lambda: V.find_text(lambda d: d.get('type') == 'idle') is not None, 3)
    G = await Rec('G').start()
    closers.append(G.close)
    await until(lambda: len(G.msgs) >= 1, 3)
    await asyncio.sleep(0.3)
    check(len(G.msgs) == 1 and strict(G.msgs[0][1]).get('live') is False,
          'a viewer joining after the bye gets the state (live=false) and no snapshot')
    await P.send(json.dumps(TILES_HELLO2))
    await until_status(lambda s: s['hello'] == TILES_HELLO2, 3)
    H2 = await Rec('H2').start()
    closers.append(H2.close)
    await until(lambda: len(H2.msgs) >= 1, 3)
    await asyncio.sleep(0.3)
    st = await status()
    check(len(H2.msgs) == 1 and strict(H2.msgs[0][1]).get('live') is True and st['tiles'] is None,
          'a new tiles run starts without the old run\'s snapshot (or frame)')
    await P.send(tiles_snap(9000))
    await until(lambda: any(tkind(m) == ('tiles', 9000) for _, m in H2.msgs if isinstance(m, str)), 3)
    check(True, 'its first snapshot goes straight through')


MAZE_HELLO = {'type': 'hello', 'source': 'training', 'task': 'maze', 'run': 'maze_run_1', 'label': 'relay test maze',
              'fps': 25, 'started': '2026-09-26T09:00:00Z'}
MAZE_HELLO2 = dict(MAZE_HELLO, run='maze_run_2', started='2026-09-26T09:30:00Z')


def maze_snap(k, maze_id=1, w=3, h=3, layout=False, size=None, **over):
    """A Rat Maze snapshot numbered k (its steps). layout=True: the maze's first snapshot, carrying its walls (one hex
    char per cell); otherwise "walls": null, as the publisher sends them. size: pad it to exactly that many bytes."""
    cw, ch = max(1, w), max(1, h)                       # (w or h may be junk on purpose)
    walls = ''.join('%x' % ((i * 7 + maze_id) % 16) for i in range(cw * ch)) if layout else None
    m = {'type': 'maze', 't': round(k * 0.125, 3), 'maze_id': maze_id, 'w': w, 'h': h, 'walls': walls,
         'cell': [k % cw, (k // cw) % ch], 'pos': [k % cw + 0.5, (k // cw) % ch + 0.5], 'cheese': [cw - 1, ch - 1],
         'trail': [[i % cw, (i // cw) % ch] for i in range(max(0, k - 5), k)], 'bumps': k // 7, 'steps': k,
         'dist': max(0, 8 - k % 9)}
    m.update(over)
    s = json.dumps(m, separators=(',', ':'))
    if size is not None:
        m['pad'] = ''
        base = len(json.dumps(m, separators=(',', ':')))
        m['pad'] = 'x' * (size - base)
        s = json.dumps(m, separators=(',', ':'))
        assert len(s) == size, (len(s), size)
    return s


def maze_end(k, result='escaped', size=None, **over):
    m = {'type': 'maze_end', 'maze_id': k, 'result': result, 'steps': 40 + k, 'bumps': 2, 'time_s': round(3.5 + k * 0.1, 2)}
    m.update(over)
    if size is not None:
        m['pad'] = ''
        m['pad'] = 'y' * (size - len(json.dumps(m, separators=(',', ':'))))
    return json.dumps(m, separators=(',', ':'))


def mkind(text):
    """('maze', steps) / ('maze_end', maze_id) for a Rat Maze text, else None."""
    d = strict(text)
    if d.get('type') == 'maze':
        return 'maze', d.get('steps')
    if d.get('type') == 'maze_end':
        return 'maze_end', d.get('maze_id')
    return None


def is_layout(text):
    d = strict(text)
    return d.get('type') == 'maze' and d.get('walls') is not None


async def maze_relay():
    relay = Relay({'LABRAT_PUBLISH_TOKEN': TOKEN}, 'maze')
    closers, raws = [], []
    try:
        await maze_checks(closers, raws)
    finally:
        for r in raws:
            r.shutdown()
        for c in closers:
            try:
                await c()
            except Exception:
                pass
        relay.stop()
        log = relay.text()
    check('Traceback' not in log and 'Exception in ASGI' not in log, 'the relay logged no exceptions (Rat Maze)',
          f'log: {relay.logpath}')
    if 'Traceback' in log or 'Exception in ASGI' in log:
        print(log[-4000:])


async def maze_checks(closers, raws):
    section('Rat Maze: maze / maze_end messages only in a run whose hello says task "maze"')
    V = await Rec('V').start()
    closers.append(V.close)
    await until(lambda: len(V.msgs) >= 1, 3)
    P = await connect(PUB, additional_headers=AUTH, open_timeout=5)
    closers.append(P.close)
    await P.send(maze_snap(0, layout=True))                          # before any hello: dropped
    await P.send(json.dumps(HELLO))                                  # a lever run
    await P.send(maze_snap(1, layout=True))
    await P.send(maze_end(1))
    await P.send(json.dumps(TILES_HELLO))                            # a Rat Tiles run
    await P.send(maze_snap(2, layout=True))
    await until(lambda: V.find_text(lambda d: d == TILES_HELLO) is not None, 3)
    await asyncio.sleep(0.3)
    st = await status()
    c = st['counts']
    check(not [m for m in V.texts() if m.get('type') in ('maze', 'maze_end')] and c.get('dropped_outside_session') == 1
          and c.get('dropped_maze_wrong_task') == 3 and st['maze'] is None,
          'a snapshot before any hello, and maze / maze_end in a lever or tiles run, are dropped and counted',
          json.dumps({k: v for k, v in c.items() if 'dropped' in k}))
    mark = len(V.msgs)
    await P.send(json.dumps(MAZE_HELLO))
    rows = [{'steps': 1000 * (i + 1), 'ret': 1.0 + i, 'hits': 2 + i} for i in range(3)]
    for r in rows:
        await P.send(json.dumps({'type': 'metrics', 'row': r}))
    await until(lambda: V.find_text(lambda d: d == MAZE_HELLO, mark) is not None, 3)
    st = await status()
    check(st['live'] is True and st['hello'] == MAZE_HELLO, 'a hello with task "maze" is accepted and live')
    mark = len(V.msgs)
    await P.send(tiles_snap(3))                                      # Rat Tiles messages in a maze run: dropped
    await P.send(tile_ev(3))
    await asyncio.sleep(0.3)
    c = (await status())['counts']
    check(V.texts(mark) == [] and c.get('dropped_tiles_wrong_task') == 2,
          'tiles / tile messages in a maze run are dropped and counted')

    section('Rat Maze: a layout snapshot, position snapshots at 8 Hz and maze_end events reach viewers verbatim, in order')
    mark = len(V.msgs)
    sent = []
    t0 = time.perf_counter()
    for k in range(24):
        d = t0 + k * 0.125 - time.perf_counter()
        if d > 0:
            await asyncio.sleep(d)
        s = maze_snap(100 + k, maze_id=1 + k // 12, layout=(k % 12 == 0))
        await P.send(s)
        await P.send(make_frame(100 + k))
        sent.append(s)
        if k % 12 == 11:
            e = maze_end(1 + k // 12, 'escaped' if k < 12 else 'timeout')
            await P.send(e)
            sent.append(e)
    await until(lambda: [m for _, m in V.msgs[mark:] if isinstance(m, str)] == sent, 3)
    got = [m for _, m in V.msgs[mark:] if isinstance(m, str)]
    check(got == sent, 'all 24 snapshots (2 with walls) and 2 maze_end events arrive, byte for byte, in order',
          f'{len(got)}/{len(sent)}')
    check([frame_k(m) for _, m in V.msgs[mark:] if isinstance(m, bytes)] == list(range(100, 124)),
          'the rat frames in between are unaffected')
    st = await status()
    check(st['maze'] == {'maze_id': 2, 'w': 3, 'h': 3, 't': 123 * 0.125, 'dist': max(0, 8 - 123 % 9), 'steps': 123,
                         'bumps': 123 // 7, 'layout': False},
          '/status shows the newest snapshot\'s maze_id, size, time, distance, steps, bumps and whether it carried walls',
          json.dumps(st['maze']))

    section('Rat Maze: caps and junk')
    mark = len(V.msgs)
    await P.send(maze_snap(200, layout=True, size=8193))             # over 8 KB
    await P.send(maze_end(201, size=513))                            # over 512 B
    await P.send(maze_snap(202, layout=True, walls='0f0f0f0f'))      # walls of the wrong length (3x3 needs 9)
    await P.send(maze_snap(203, layout=True, walls='0f0f0f0fg'))     # not hex
    await P.send(maze_snap(204, maze_id='7'))                        # maze_id not an int
    await P.send(maze_snap(205, w=0))                                # a zero-width maze
    await P.send(maze_snap(206, h=65))                               # too tall (64 at most)
    await P.send(maze_end(207, 'maybe'))                             # not escaped / timeout
    await P.send(json.dumps({'type': 'maze_end', 'result': 'escaped'}))    # no maze_id
    ok_snap, ok_ev = maze_snap(208, layout=True, size=8192), maze_end(209, 'timeout', size=512)
    await P.send(ok_snap)
    await P.send(ok_ev)
    await until(lambda: any(m == ok_ev for _, m in V.msgs[mark:]), 3)
    got = [m for _, m in V.msgs[mark:] if isinstance(m, str)]
    check(got == [ok_snap, ok_ev], 'an 8,192-byte snapshot and a 512-byte event pass; bigger ones, bad walls, a bad '
          'maze_id or size, and an event without an escaped / timeout result or a maze_id are dropped',
          str([mkind(m) for m in got]))
    c = (await status())['counts']
    check(c.get('dropped_maze_too_big') == 1 and c.get('dropped_maze_end_too_big') == 1 and c.get('dropped_bad_maze') == 5
          and c.get('dropped_bad_maze_end') == 2 and not c.get('dropped_error'), 'each is counted in /status',
          json.dumps({k: v for k, v in c.items() if 'maze' in k}))

    section('Rat Maze: at most 10 position snapshots a second per viewer; layout snapshots and maze_end are never dropped')
    await asyncio.sleep(0.5)                                         # the per-viewer burst refills
    mark = len(V.msgs)
    c0 = (await status())['counts'].get('maze_dropped_for_rate', 0)
    N = 90
    t0 = time.perf_counter()
    for k in range(N):                                               # 30 snapshots a second for 3 s, a new maze every 30
        d = t0 + k / 30 - time.perf_counter()
        if d > 0:
            await asyncio.sleep(d)
        await P.send(maze_snap(1000 + k, maze_id=10 + k // 30, layout=(k % 30 == 0)))
        await P.send(maze_end(1000 + k))
    span = time.perf_counter() - t0
    await until(lambda: any(mkind(m) == ('maze_end', 1000 + N - 1) for _, m in V.msgs[mark:] if isinstance(m, str)), 3)
    seq = [(t, mkind(m), is_layout(m)) for t, m in V.msgs[mark:] if isinstance(m, str)]
    evs = [k for _, (kind, k), _ in seq if kind == 'maze_end']
    lays = [k for _, (kind, k), lay in seq if kind == 'maze' and lay]
    snaps = [(t, k) for t, (kind, k), lay in seq if kind == 'maze' and not lay]
    check(evs == list(range(1000, 1000 + N)), f'all {N} maze_end events arrive, in order', f'{len(evs)}/{N}')
    check(lays == [1000, 1030, 1060], 'all 3 layout snapshots (the ones with walls) arrive', str(lays))
    lo, hi = int(10 * span) - 2, int(10 * span + 3) + 1
    check(lo <= len(snaps) <= hi, f'{len(snaps)} of {N - 3} position snapshots reach the viewer in {span:.2f} s '
          f'(10 a second, plus a burst of 3)', f'allowed {lo}..{hi}')
    win = max((sum(1 for u, _ in snaps if t <= u < t + 1.0) for t, _ in snaps), default=0)
    check(win <= 13, 'no one-second window carries more than 10 + the burst of 3', f'max {win} in a second')
    ks = [k for _, k in snaps]
    order = [kind_k for _, kind_k, _ in seq]
    check(ks == sorted(ks) and all(order.index(('maze', k)) + 1 == order.index(('maze_end', k)) for k in ks + lays),
          'the snapshots that pass stay in order, each straight ahead of its own maze_end')
    c = (await status())['counts']
    check(c.get('maze_dropped_for_rate', 0) - c0 == N - 3 - len(snaps), '/status counts the snapshots dropped for rate',
          f"{c.get('maze_dropped_for_rate', 0) - c0}")

    section('Rat Maze: a late joiner gets the current maze\'s layout, then the newest snapshot (never the events)')
    await asyncio.sleep(0.5)
    lay7 = maze_snap(500, maze_id=7, w=5, h=4, layout=True)
    await P.send(make_frame(500))
    await P.send(lay7)
    for k in (501, 502, 503):
        await P.send(maze_snap(k, maze_id=7, w=5, h=4))
    last_ev = maze_end(7)
    await P.send(last_ev)
    await until(lambda: any(m == last_ev for _, m in V.msgs), 3)
    L = await Rec('L').start()
    closers.append(L.close)
    await until(lambda: len(L.msgs) >= 4, 3)
    await asyncio.sleep(0.3)
    m = [x for _, x in L.msgs]
    s0 = strict(m[0]) if m and isinstance(m[0], str) else {}
    check(len(m) == 4 and s0.get('type') == 'state' and s0.get('live') is True and m[1] == make_frame(500)
          and m[2] == lay7 and m[3] == maze_snap(503, maze_id=7, w=5, h=4),
          'it gets: the state, the last frame, the layout snapshot, then the newest snapshot, verbatim',
          str([('text', strict(x).get('type'), is_layout(x)) if isinstance(x, str) else 'frame' for x in m]))
    check(set(s0) == {'type', 'live', 'hello', 'checkpoint', 'episode', 'history'} and s0.get('history') == rows,
          'the state itself is unchanged: its history holds only the log rows')
    st = await status()
    check(st['maze'] == {'maze_id': 7, 'w': 5, 'h': 4, 't': 503 * 0.125, 'dist': max(0, 8 - 503 % 9), 'steps': 503,
                         'bumps': 503 // 7, 'layout': False}, '/status shows the newest snapshot', json.dumps(st['maze']))
    lay8 = maze_snap(600, maze_id=8, w=4, h=4, layout=True)
    await P.send(lay8)                                               # a new maze: its layout is the newest snapshot
    await until_status(lambda s: s['maze'] and s['maze']['maze_id'] == 8, 3)
    L2 = await Rec('L2').start()
    closers.append(L2.close)
    await until(lambda: len(L2.msgs) >= 3, 3)
    await asyncio.sleep(0.3)
    m = [x for _, x in L2.msgs]
    check(len(m) == 3 and m[1] == make_frame(500) and m[2] == lay8,
          'when the newest snapshot is the layout itself, it is sent once', f'{len(m)} messages')
    await P.send(maze_snap(700, maze_id=9, w=4, h=4))                # a maze whose layout never came (position only)
    await until_status(lambda s: s['maze'] and s['maze']['maze_id'] == 9, 3)
    L3 = await Rec('L3').start()
    closers.append(L3.close)
    await until(lambda: len(L3.msgs) >= 3, 3)
    await asyncio.sleep(0.3)
    m = [x for _, x in L3.msgs]
    check(len(m) == 3 and m[2] == maze_snap(700, maze_id=9, w=4, h=4),
          'a position snapshot of a maze whose layout never came is replayed alone (no stale layout of another maze)',
          f'{len(m)} messages')

    section('Rat Maze: a slow viewer holds one unsent position snapshot at most; layouts and events all get through')
    D = RawViewer('D', 'stuck', rcvbuf=4096)
    raws.append(D)
    D.start()
    await until_status(lambda s: s['viewers'] == 5, 3)
    for i in range(150):                                             # fill D's socket and the relay's send buffer
        await P.send(make_frame(3000 + i))
        await asyncio.sleep(0.02)
    c0 = (await status())['counts'].get('maze_replaced_for_slow_viewers', 0)
    vmark = len(V.msgs)
    sent = []
    t0 = time.perf_counter()
    for k in range(20):                                              # 8 Hz, as the publisher sends them
        d = t0 + k * 0.125 - time.perf_counter()
        if d > 0:
            await asyncio.sleep(d)
        s, e = maze_snap(2000 + k, maze_id=20 + k // 10, layout=(k % 10 == 0)), maze_end(2000 + k)
        await P.send(s)
        await P.send(e)
        sent += [s, e]
    D.mode = 'fast'
    want_last = maze_end(2019)
    await until(lambda: any(kind == 'text' and p == want_last for _, kind, p in D.msgs), 8)
    dt = [p for _, kind, p in D.msgs if kind == 'text' and mkind(p) and mkind(p)[1] >= 2000]
    it = iter(sent)
    subseq = all(any(x == y for y in it) for x in dt)
    d_snaps = [mkind(p)[1] for p in dt if mkind(p)[0] == 'maze' and not is_layout(p)]
    d_lays = [mkind(p)[1] for p in dt if is_layout(p)]
    d_evs = [mkind(p)[1] for p in dt if mkind(p)[0] == 'maze_end']
    check(d_evs == list(range(2000, 2020)), 'the slow viewer gets every maze_end event, in order', f'{len(d_evs)}/20')
    check(d_lays == [2000, 2010], 'and both layout snapshots', str(d_lays))
    check(subseq and d_snaps and d_snaps[-1] == 2019 and len(d_snaps) < 18,
          'it gets fewer position snapshots, ending on the newest, and everything in the order it was sent',
          f'{len(d_snaps)} of 18 position snapshots, last {d_snaps[-1] if d_snaps else "-"}')
    c = (await status())['counts']
    check(c.get('maze_replaced_for_slow_viewers', 0) > c0 and not D.eof,
          '/status counts the replaced snapshots; the slow viewer stays connected',
          f"{c.get('maze_replaced_for_slow_viewers', 0) - c0} replaced")
    got_v = [m for _, m in V.msgs[vmark:] if isinstance(m, str)]
    check(got_v == sent, 'the fast viewer still got all 20 snapshots and 20 events', f'{len(got_v)}/40')

    section('Rat Maze: after bye, and in the next run, no old snapshot is replayed')
    await P.send(json.dumps({'type': 'bye'}))
    await until(lambda: V.find_text(lambda d: d.get('type') == 'idle') is not None, 3)
    G = await Rec('G').start()
    closers.append(G.close)
    await until(lambda: len(G.msgs) >= 1, 3)
    await asyncio.sleep(0.3)
    check(len(G.msgs) == 1 and strict(G.msgs[0][1]).get('live') is False,
          'a viewer joining after the bye gets the state (live=false) and no snapshot')
    await P.send(json.dumps(MAZE_HELLO2))
    await until_status(lambda s: s['hello'] == MAZE_HELLO2, 3)
    H2 = await Rec('H2').start()
    closers.append(H2.close)
    await until(lambda: len(H2.msgs) >= 1, 3)
    await asyncio.sleep(0.3)
    st = await status()
    check(len(H2.msgs) == 1 and strict(H2.msgs[0][1]).get('live') is True and st['maze'] is None,
          'a new maze run starts without the old run\'s snapshots (or frame)')
    await P.send(maze_snap(9000, maze_id=90, layout=True))
    await until(lambda: any(mkind(m) == ('maze', 9000) for _, m in H2.msgs if isinstance(m, str)), 3)
    check(True, 'its first (layout) snapshot goes straight through')


async def amain():
    await main_relay()
    await pons_relay()
    await tiles_relay()
    await maze_relay()
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
