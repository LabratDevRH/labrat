"""labrat live relay: one publisher (the owner's PC, running next to training) -> many public viewers.

    WS  /publish   the publisher. Needs the header "Authorization: Bearer <LABRAT_PUBLISH_TOKEN>". One at a time.
    WS  /live      public viewers (the website's live view). Receive-only; a viewer may send a tiny "ping".
    GET /status    JSON: live, hello, metrics (the last log.jsonl row), viewers, and a few counters.
    GET /healthz   {"ok": true}

What a viewer receives, in order:
    1. {"type":"state","live":bool,"hello":{...}|null,"checkpoint":{...}|null,"episode":{...}|null,
        "history":[the last <=300 log.jsonl rows since that hello]}
       and, when live, the most recent binary frame straight after it. The whole state stays under STATE_MAX bytes
       (site/js/live.js ignores text over 65,536): if 300 rows would not fit, only the newest rows that fit are sent.
    2. then everything the publisher sends, unchanged: hello, metrics, checkpoint, episode, bye, binary frames.
    3. {"type":"idle","reason":...} when the publisher disconnects, says bye, or sends nothing for 15 s.
    4. a fresh "state" with live=true when a publisher that went quiet starts sending again.
    5. {"type":"pong","t":<unix s>} in answer to a viewer's "ping".

Every hello starts a fresh history (the viewer's live.js clears its curve on a hello too): the publisher re-sends its
hello, the checkpoint and its last log rows after every (re)connect, which rebuilds it without duplicates.

The relay invents nothing: it forwards what the publisher sends and replays what it kept. "live" means exactly: a
publisher is connected, has sent a hello with source "training" that is not a test stream, and has sent something
in the last 15 s. A test stream (publish_training.py --assume-live-for-test: hello "test": true, label "TEST ...")
is refused unless RELAY_ALLOW_TEST=1, which is for a local relay only.

Run it (single process only: all state is in memory, so never use --workers > 1 or several replicas):
    uvicorn relay:app --host 0.0.0.0 --port $PORT --ws-max-size 131072 --ws-per-message-deflate false
    SERVE_SITE=1 also serves ../site at / (local preview; see README.md).
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import math
import os
import struct
import time
from collections import deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- the contract -------------------------------------------------------------------------------------------------
IDLE_S = 15.0                 # publisher silent this long -> idle; also the "fresh" window for publisher replacement
HISTORY_ROWS = 300            # log.jsonl rows kept for late joiners
MAX_BINARY = 4096             # publisher binary frame cap (a rat frame is (12 + 65*7) * 4 = 1868 bytes)
MAX_TEXT = 64 * 1024          # publisher text message cap (UTF-8 bytes)
MAX_PART = 8 * 1024           # hello / checkpoint / episode are small; bigger ones are dropped, so a state always fits
STATE_MAX = 60_000            # a state message stays under this many bytes (site/js/live.js drops text over 65,536)
VIEWER_CAP = 500
FRAME_MAGIC = 7.0
MIN_FRAME = 12 * 4            # the 12-float header
TASKS = ('lever', 'cursor', 'steer')
PUBLISHER_TYPES = ('hello', 'metrics', 'checkpoint', 'episode', 'bye')

# ---- per-viewer fanout limits ---------------------------------------------------------------------------------------
VIEWER_MAX_FRAMES = 16        # queued binary frames per viewer (~0.6 s at 25 fps); beyond this the OLDEST is dropped
VIEWER_MAX_TEXTS = 1024       # queued text messages per viewer; beyond this the viewer is hopelessly behind -> closed.
                              # A publisher's resync after a reconnect is ~302 texts in one burst; queued texts are
                              # shared str objects, so the cap costs almost no memory
VIEWER_STUCK_S = 30.0         # a single send blocked this long -> the viewer is gone or hopelessly behind -> closed
VIEWER_MSG_MAX = 512          # largest message a viewer may send (a ping)
VIEWER_MSG_BURST = 10         # viewer messages allowed in a burst ...
VIEWER_MSG_PER_S = 1.0        # ... refilled at this rate; more than that -> closed
MIN_TOKEN_LEN = 16            # a shorter LABRAT_PUBLISH_TOKEN counts as unset
VIEWERS_PER_IP = 8            # /live sockets one client address may hold (RELAY_MAX_PER_IP; 0 = no per-address cap)
REFUSAL_LOG_WINDOW_S = 60.0   # refused publisher handshakes: at most REFUSAL_LOG_MAX log lines per window ...
REFUSAL_LOG_MAX = 3           # ... then one summary line (the full totals are in /status counts)


def _env_int(name, default, lo, hi):
    try:
        v = int(os.environ.get(name, default))
    except ValueError:
        v = default
    return max(lo, min(hi, v))


MAX_VIEWERS = _env_int('RELAY_MAX_VIEWERS', VIEWER_CAP, 1, VIEWER_CAP)
MAX_PER_IP = _env_int('RELAY_MAX_PER_IP', VIEWERS_PER_IP, 0, VIEWER_CAP)
_origins = os.environ.get('LIVE_ORIGINS', '*').strip()
LIVE_ORIGINS = None if _origins in ('', '*') else {o.strip().rstrip('/') for o in _origins.split(',') if o.strip()}
SERVE_SITE = os.environ.get('SERVE_SITE', '').strip().lower() in ('1', 'true', 'yes', 'on')
ALLOW_TEST = os.environ.get('RELAY_ALLOW_TEST', '').strip().lower() in ('1', 'true', 'yes', 'on')
SITE_DIR = os.path.abspath(os.environ.get('SITE_DIR') or os.path.join(HERE, '..', 'site'))

IDLE_QUIET = json.dumps({'type': 'idle', 'reason': 'quiet'})
IDLE_BYE = json.dumps({'type': 'idle', 'reason': 'bye'})
IDLE_GONE = json.dumps({'type': 'idle', 'reason': 'disconnected'})


def say(msg):
    print(time.strftime('%H:%M:%S ') + msg, flush=True)


def _client_ip(ws):
    """The client's address as the platform's proxy saw it. Railway's edge proxy sets X-Real-IP (a client cannot set
    it) and APPENDS the address it saw to X-Forwarded-For, so only the right-most X-Forwarded-For entry is
    trustworthy (the left-most is whatever the client sent). Without a proxy: the socket's peer."""
    h = ws.headers
    real = (h.get('x-real-ip') or '').strip()
    if real:
        return real
    fwd = [p.strip() for p in (h.get('x-forwarded-for') or '').split(',') if p.strip()]
    if fwd:
        return fwd[-1]
    return ws.client.host if ws.client else '?'


def _per_ip_key(ip):
    """The key the per-address viewer cap counts under, or None for no cap: loopback, private and other non-global
    addresses (local tests, or a proxy address when the real client is unknown) are never capped per address, so an
    unexpected proxy layout fails open instead of locking everyone behind one internal address out."""
    try:
        a = ipaddress.ip_address(ip.split('%', 1)[0])
    except ValueError:
        return None
    if getattr(a, 'ipv4_mapped', None):
        a = a.ipv4_mapped
    if not a.is_global:
        return None
    if a.version == 6:                    # one client usually holds a whole /64
        return str(ipaddress.ip_network(f'{a}/64', strict=False))
    return str(a)


class _RefusalLog:
    """Rate-limited logging for refused publisher handshakes: anyone can send those, as fast as they like, and each
    one would otherwise print a line (flooding the platform's logs, where rate limits then drop real events)."""

    def __init__(self):
        self.t0 = 0.0
        self.n = 0                        # refusals in the current window

    def flush(self, now):
        """Close the window once it has run out (called by the ticker too, so the summary is not held back)."""
        if self.n and now - self.t0 >= REFUSAL_LOG_WINDOW_S:
            if self.n > REFUSAL_LOG_MAX:
                say(f'{self.n - REFUSAL_LOG_MAX} more refused publisher handshake(s) in the last '
                    f'{REFUSAL_LOG_WINDOW_S:.0f} s were not logged (totals: /status counts)')
            self.n = 0

    def say(self, msg):
        now = time.monotonic()
        self.flush(now)
        if self.n == 0:
            self.t0 = now
        self.n += 1
        if self.n <= REFUSAL_LOG_MAX:
            say(msg)


REFUSALS = _RefusalLog()


# ---- viewers --------------------------------------------------------------------------------------------------------
class Viewer:
    """One public viewer: a bounded outbox drained by its own sender task, so a slow viewer never slows anyone else.

    The outbox is one ordered deque of str (text) and bytes (binary frames). Frames are whole poses, so a viewer that
    falls behind only needs the newest ones: past VIEWER_MAX_FRAMES the oldest queued frame is dropped. Text (hello,
    metrics, checkpoint, episode, idle, state) is never dropped; a viewer that has VIEWER_MAX_TEXTS of it queued, or is
    stuck on one send for VIEWER_STUCK_S, is hopelessly behind and is closed (1013, try again later), after which its
    page can reconnect and get a fresh state."""
    __slots__ = ('ws', 'q', 'n_bin', 'n_text', 'dropped', 'wake', 'done', 'closed', 'kill_code', 'kill_reason',
                 'sending_since', 'bucket', 'bucket_t')

    def __init__(self, ws):
        self.ws = ws
        self.q = deque()
        self.n_bin = 0
        self.n_text = 0
        self.dropped = 0
        self.wake = asyncio.Event()
        self.done = asyncio.Event()
        self.closed = False
        self.kill_code = None
        self.kill_reason = ''
        self.sending_since = 0.0
        self.bucket = float(VIEWER_MSG_BURST)
        self.bucket_t = time.monotonic()

    def push_bytes(self, b):
        if self.closed:
            return
        if self.n_bin >= VIEWER_MAX_FRAMES:
            q = self.q
            for i, item in enumerate(q):
                if item.__class__ is bytes:
                    del q[i]
                    break
            self.n_bin -= 1
            self.dropped += 1
            HUB.count('frames_dropped_for_slow_viewers')
        self.q.append(b)
        self.n_bin += 1
        self.wake.set()

    def push_text(self, s):
        if self.closed:
            return
        if self.n_text >= VIEWER_MAX_TEXTS:
            HUB.count('viewers_closed_too_far_behind')
            self.kill(1013, 'too far behind; reconnect for a fresh state')
            return
        self.q.append(s)
        self.n_text += 1
        self.wake.set()

    def kill(self, code, reason):
        if self.closed:
            return
        self.closed = True
        self.kill_code, self.kill_reason = code, reason
        self.q.clear()
        self.n_bin = self.n_text = 0
        self.wake.set()
        self.done.set()

    def _take_token(self):
        now = time.monotonic()
        self.bucket = min(float(VIEWER_MSG_BURST), self.bucket + (now - self.bucket_t) * VIEWER_MSG_PER_S)
        self.bucket_t = now
        if self.bucket < 1.0:
            return False
        self.bucket -= 1.0
        return True

    async def send_loop(self):
        ws, q = self.ws, self.q
        try:
            while not self.closed:
                if not q:
                    self.wake.clear()
                    await self.wake.wait()
                    continue
                item = q.popleft()
                self.sending_since = time.monotonic()
                if item.__class__ is bytes:
                    self.n_bin -= 1
                    await ws.send_bytes(item)
                else:
                    self.n_text -= 1
                    await ws.send_text(item)
                self.sending_since = 0.0
        except asyncio.CancelledError:
            raise
        except Exception:
            pass                          # the viewer went away
        finally:
            self.done.set()

    async def read_loop(self):
        """Viewers are receive-only. Protocol pings are answered by the server itself; an app-level "ping" text
        (browsers cannot send protocol pings) gets a pong. Anything else is ignored, binary or oversized data or a
        flood of messages closes the viewer."""
        ws = self.ws
        try:
            while True:
                msg = await ws.receive()
                if msg['type'] == 'websocket.disconnect':
                    return
                if msg.get('bytes') is not None:
                    HUB.count('viewers_closed_sent_data')
                    self.kill(1003, 'viewers are receive-only')
                    return
                text = msg.get('text') or ''
                if len(text) > VIEWER_MSG_MAX:
                    HUB.count('viewers_closed_sent_data')
                    self.kill(1009, 'viewers are receive-only')
                    return
                if not self._take_token():
                    HUB.count('viewers_closed_flooding')
                    self.kill(1008, 'too many messages')
                    return
                if _is_ping(text):
                    self.push_text(json.dumps({'type': 'pong', 't': round(time.time(), 3)}))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self.done.set()


def _is_ping(text):
    t = text.strip()
    if t.lower() == 'ping':
        return True
    if t.startswith('{'):
        try:
            m = json.loads(t)
        except ValueError:
            return False
        return isinstance(m, dict) and m.get('type') == 'ping'
    return False


# ---- the publisher --------------------------------------------------------------------------------------------------
class Publisher:
    __slots__ = ('ws', 'peer', 'last_rx', 'in_session')

    def __init__(self, ws, peer):
        self.ws = ws
        self.peer = peer
        self.last_rx = time.monotonic()   # connecting counts as activity: a fresh connection holds the slot 15 s
        self.in_session = False           # True between a (training) hello and a bye


class Hub:
    def __init__(self):
        self.pub = None
        self.live = False
        self.hello = None                 # the latest hello, kept after the run ends (with live=false)
        self.history = deque(maxlen=HISTORY_ROWS)   # (row dict, its compact ASCII JSON) since the latest hello
        self.state_rows = 0               # rows the current state message carries (fewer than kept if they are big)
        self.checkpoint = None
        self.episode = None
        self.last_frame = None
        self.viewers = set()
        self.pending = 0                  # viewer handshakes in progress (count toward the cap)
        self.per_ip = {}                  # per-address key -> /live sockets held (accepted or in handshake)
        self.counts = {}
        self._state = None
        self.started = time.time()
        self.frames_in = 0
        self._fps_n = 0
        self._fps_t = time.monotonic()
        self.fps_in = 0.0

    def count(self, key, n=1):
        self.counts[key] = self.counts.get(key, 0) + n

    def dirty(self):
        self._state = None

    def last_row(self):
        return self.history[-1][0] if self.history else None

    def state_text(self):
        """The state message, cached until something changes. Everything in it is ASCII JSON (so its length in
        characters is its length in bytes), and it stays under STATE_MAX: hello, checkpoint and episode are at most
        MAX_PART each, and the history is the newest run of rows that fits in what is left."""
        if self._state is None:
            head = ('{"type":"state","live":' + ('true' if self.live else 'false')
                    + ',"hello":' + _compact(self.hello) + ',"checkpoint":' + _compact(self.checkpoint)
                    + ',"episode":' + _compact(self.episode) + ',"history":[')
            room = STATE_MAX - len(head) - 2
            parts = []
            for _row, js in reversed(self.history):
                need = len(js) + (1 if parts else 0)
                if need > room:
                    break
                parts.append(js)
                room -= need
            parts.reverse()
            self.state_rows = len(parts)
            self._state = head + ','.join(parts) + ']}'
        return self._state

    def broadcast_text(self, s):
        for v in list(self.viewers):
            v.push_text(s)

    def broadcast_bytes(self, b):
        for v in list(self.viewers):
            v.push_bytes(b)

    def set_idle(self, text, why):
        if not self.live:
            return
        self.live = False
        self.dirty()
        say(f'idle: {why}')
        self.broadcast_text(text)

    def touch(self, pub):
        """A usable message from the publisher: it is fresh; if it had gone quiet mid-run, the run is live again."""
        pub.last_rx = time.monotonic()
        if pub.in_session and not self.live:
            self.live = True
            self.dirty()
            say('live again: the publisher is sending again')
            self.broadcast_text(self.state_text())


HUB = Hub()


def _compact(obj):
    return json.dumps(obj, separators=(',', ':'), allow_nan=False)


def _is_test_stream(msg):
    label = msg.get('label')
    return bool(msg.get('test')) or (isinstance(label, str) and label.lstrip().startswith('TEST'))


def _loads_browser_safe(text):
    """json.loads that turns NaN/Infinity (which Python's json writes but browsers' JSON.parse rejects) and numbers
    too big for a float (1e400, which would parse to inf) into null. Returns (obj, changed)."""
    changed = []

    def const(_):
        changed.append(1)
        return None

    def num(s):
        f = float(s)
        if math.isfinite(f):
            return f
        changed.append(1)
        return None
    return json.loads(text, parse_constant=const, parse_float=num), bool(changed)


def on_pub_text(pub, text):
    """Handle one text message from the publisher. Returns None, or (close_code, reason) to close the publisher."""
    H = HUB
    if len(text) > MAX_TEXT or len(text.encode('utf-8')) > MAX_TEXT:
        H.count('dropped_text_too_big')
        return None
    try:
        msg, changed = _loads_browser_safe(text)
    except (ValueError, RecursionError):  # RecursionError: deeply nested junk like "[[[[..."
        H.count('dropped_bad_json')
        return None
    if not isinstance(msg, dict) or msg.get('type') not in PUBLISHER_TYPES:
        H.count('dropped_unknown_type')
        return None
    kind = msg['type']
    if changed:
        text = json.dumps(msg, separators=(',', ':'), ensure_ascii=False)
        if len(text.encode('utf-8')) > MAX_TEXT:
            H.count('dropped_text_too_big')
            return None
    if kind in ('hello', 'checkpoint', 'episode') and len(_compact(msg)) > MAX_PART:
        H.count('dropped_part_too_big')   # kept for the state message (as ASCII JSON), which must stay small
        return None

    if kind == 'hello':
        if msg.get('source') != 'training':
            H.count('refused_non_training_hello')
            say(f"refused a hello with source {msg.get('source')!r}: this relay only carries live training")
            return 1008, 'this relay only carries live training (hello.source must be "training")'
        if _is_test_stream(msg) and not ALLOW_TEST:
            H.count('refused_test_hello')
            say('refused a test stream (hello "test" or a label starting with TEST): it is not a live training run')
            return 1008, 'test streams are not live training; this relay shows them only with RELAY_ALLOW_TEST=1'
        run, task = msg.get('run'), msg.get('task')
        if task not in TASKS or not isinstance(run, str) or not run:
            H.count('dropped_bad_hello')
            return None
        prev = H.hello
        same = bool(prev) and all(prev.get(k) == msg.get(k) for k in ('run', 'task', 'started'))
        H.history.clear()                 # every hello starts a fresh curve; the publisher re-sends its rows after it
        if not same:                      # a new run or a new publisher session: nothing of the old one carries over
            H.checkpoint = H.episode = H.last_frame = None
        H.hello = msg
        pub.in_session = True
        pub.last_rx = time.monotonic()
        H.live = True
        H.dirty()
        say(f'live: run {run!r}, task {task}')
        H.broadcast_text(text)
        return None

    if not pub.in_session:
        H.count('dropped_outside_session')
        return None

    if kind == 'bye':
        pub.in_session = False
        pub.last_rx = time.monotonic()
        H.broadcast_text(text)
        H.set_idle(IDLE_BYE, 'the publisher said bye')
        return None

    if kind == 'metrics':
        row = msg.get('row')
        if not isinstance(row, dict):
            H.count('dropped_bad_metrics')
            return None
        H.touch(pub)
        H.history.append((row, _compact(row)))
    elif kind == 'checkpoint':
        H.touch(pub)
        H.checkpoint = msg
    else:
        H.touch(pub)
        H.episode = msg
    H.dirty()
    H.broadcast_text(text)
    return None


def on_pub_bytes(pub, data):
    H = HUB
    n = len(data)
    if n > MAX_BINARY:
        H.count('dropped_frame_too_big')
        return
    if n < MIN_FRAME or n % 4 or struct.unpack_from('<f', data, 0)[0] != FRAME_MAGIC:
        H.count('dropped_bad_frame')
        return
    if not pub.in_session:
        H.count('dropped_outside_session')
        return
    H.touch(pub)
    H.last_frame = data
    H.frames_in += 1
    H._fps_n += 1
    H.broadcast_bytes(data)


def _bearer(header):
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        return None
    return parts[1].strip() or None


def _same_secret(a, b):
    # hash first so the comparison is constant-time and length-independent
    return hmac.compare_digest(hashlib.sha256(a.encode('utf-8')).digest(), hashlib.sha256(b.encode('utf-8')).digest())


async def _deny(ws, status, text):
    """Refuse a websocket handshake with an HTTP status the client can read (401 / 409 / 503); falls back to a plain
    pre-accept close (HTTP 403) if the server lacks the denial-response extension."""
    try:
        await ws.send_denial_response(PlainTextResponse(text + '\n', status_code=status))
    except RuntimeError:
        await ws.close(code=1008)


async def _close_quietly(ws, code, reason):
    try:
        await ws.close(code=code, reason=reason)
    except Exception:
        pass


_BACKGROUND = set()


def _spawn(coro):
    """create_task that keeps a reference until the task is done (asyncio only holds tasks weakly)."""
    t = asyncio.create_task(coro)
    _BACKGROUND.add(t)
    t.add_done_callback(_BACKGROUND.discard)
    return t


# ---- the app --------------------------------------------------------------------------------------------------------
async def _ticker():
    H = HUB
    while True:
        await asyncio.sleep(0.5)
        now = time.monotonic()
        p = H.pub
        if H.live and (p is None or now - p.last_rx > IDLE_S):
            H.set_idle(IDLE_QUIET, f'the publisher sent nothing for {IDLE_S:.0f} s')
        for v in list(H.viewers):
            if v.sending_since and now - v.sending_since > VIEWER_STUCK_S:
                H.count('viewers_closed_too_far_behind')
                v.kill(1013, 'too far behind; reconnect for a fresh state')
        REFUSALS.flush(now)
        dt = now - H._fps_t
        if dt >= 2.0:
            H.fps_in = round(H._fps_n / dt, 1)
            H._fps_n, H._fps_t = 0, now


@asynccontextmanager
async def lifespan(_app):
    tok = os.environ.get('LABRAT_PUBLISH_TOKEN', '').strip()
    say(f'labrat relay: max {MAX_VIEWERS} viewers, idle after {IDLE_S:.0f} s, '
        f"publishing {'ENABLED' if len(tok) >= MIN_TOKEN_LEN else 'DISABLED (LABRAT_PUBLISH_TOKEN unset or short)'}"
        f"{', TEST STREAMS ALLOWED (RELAY_ALLOW_TEST=1: local use only)' if ALLOW_TEST else ''}"
        f"{', serving ' + SITE_DIR + ' at /' if SERVE_SITE else ''}")
    task = asyncio.create_task(_ticker())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title='labrat relay', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.websocket('/publish')
async def ws_publish(ws: WebSocket):
    H = HUB
    peer = _client_ip(ws)
    expected = os.environ.get('LABRAT_PUBLISH_TOKEN', '').strip()
    if len(expected) < MIN_TOKEN_LEN:
        H.count('publishers_refused_disabled')
        REFUSALS.say(f'refused a publisher from {peer}: LABRAT_PUBLISH_TOKEN is not set (or shorter than '
                     f'{MIN_TOKEN_LEN})')
        await _deny(ws, 503, 'publishing is disabled on this relay (LABRAT_PUBLISH_TOKEN is not set)')
        return
    got = _bearer(ws.headers.get('authorization'))
    if got is None or not _same_secret(got, expected):
        H.count('publishers_refused_auth')
        REFUSALS.say(f'refused a publisher from {peer}: missing or wrong token')
        await _deny(ws, 401, 'unauthorized')
        return
    cur = H.pub
    if cur is not None and time.monotonic() - cur.last_rx <= IDLE_S:
        H.count('publishers_refused_busy')
        REFUSALS.say(f'refused a second publisher from {peer}: one is already streaming')
        await _deny(ws, 409, 'another publisher is streaming; try again when it has stopped')
        return
    pub = Publisher(ws, peer)
    if cur is not None:
        # the old one has been quiet for over IDLE_S (crashed, half-open connection): the new one takes over
        say('replacing a publisher that went quiet')
        H.count('publishers_replaced')
        H.pub = None
        H.set_idle(IDLE_GONE, 'publisher replaced')
        _spawn(_close_quietly(cur.ws, 4001, 'replaced by a newer publisher'))
    H.pub = pub                       # claim the slot before any await, so two handshakes cannot both win
    try:
        await ws.accept()
    except Exception:
        if H.pub is pub:
            H.pub = None
        return
    H.count('publishers_accepted')
    say(f'publisher connected from {peer}')
    try:
        while True:
            msg = await ws.receive()
            if msg['type'] == 'websocket.disconnect' or H.pub is not pub:
                break
            data = msg.get('bytes')
            refusal = None
            try:
                if data is not None:
                    on_pub_bytes(pub, data)
                else:
                    refusal = on_pub_text(pub, msg.get('text') or '')
            except Exception as e:        # one odd message never takes the stream down
                H.count('dropped_error')
                if H.counts['dropped_error'] <= 5:
                    say(f'dropped a publisher message: {type(e).__name__}: {e}')
            if refusal is not None:
                await _close_quietly(ws, *refusal)
                break
            # let the viewers' senders run between messages: a burst the publisher had buffered (its resync after a
            # reconnect) would otherwise be read in one go, without a single context switch
            await asyncio.sleep(0)
    except Exception:
        pass
    finally:
        if H.pub is pub:
            H.pub = None
            H.set_idle(IDLE_GONE, 'the publisher disconnected')
            say('publisher disconnected')


@app.websocket('/live')
async def ws_live(ws: WebSocket):
    H = HUB
    if LIVE_ORIGINS is not None:
        origin = ws.headers.get('origin')
        if origin is not None and origin.rstrip('/') not in LIVE_ORIGINS:
            H.count('viewers_refused_origin')
            await ws.close(code=1008)
            return
    refusal = None
    if len(H.viewers) + H.pending >= MAX_VIEWERS:
        H.count('viewers_refused_full')
        refusal = 'the relay is full; try again later'
    key = _per_ip_key(_client_ip(ws)) if MAX_PER_IP else None
    if refusal is None and key is not None and H.per_ip.get(key, 0) >= MAX_PER_IP:
        # one client may not hold every slot (idle sockets are never evicted: the browser answers keep-alive pings)
        H.count('viewers_refused_per_ip')
        refusal = 'too many connections from your address; try again later'
    if refusal is not None:
        try:
            await ws.accept()
            await ws.close(code=1013, reason=refusal)
        except Exception:
            pass
        return
    if key is not None:
        H.per_ip[key] = H.per_ip.get(key, 0) + 1   # claimed before any await, like the global cap
    try:
        H.pending += 1
        try:
            await ws.accept()
        except Exception:
            return
        finally:
            H.pending -= 1
        v = Viewer(ws)
        H.viewers.add(v)
        H.count('viewers_accepted')
        v.push_text(H.state_text())
        if H.live and H.last_frame is not None:
            v.push_bytes(H.last_frame)
        sender = asyncio.create_task(v.send_loop())
        reader = asyncio.create_task(v.read_loop())
        try:
            await v.done.wait()
        finally:
            H.viewers.discard(v)
            v.closed = True
            for t in (sender, reader):
                t.cancel()
            await asyncio.gather(sender, reader, return_exceptions=True)
            if v.kill_code is not None:
                await _close_quietly(ws, v.kill_code, v.kill_reason)
    finally:
        if key is not None:
            n = H.per_ip.get(key, 0) - 1
            if n > 0:
                H.per_ip[key] = n
            else:
                H.per_ip.pop(key, None)


_NO_CACHE = {'Access-Control-Allow-Origin': '*', 'Cache-Control': 'no-store'}


@app.get('/status')
async def status():   # async: runs on the event loop, never alongside a HUB update
    H = HUB
    p = H.pub
    now = time.monotonic()
    H.state_text()                        # (cached) so state_rows is current
    return JSONResponse({
        'live': H.live,
        'hello': H.hello,
        'metrics': H.last_row(),
        'viewers': len(H.viewers),
        'checkpoint': H.checkpoint,
        'episode': H.episode,
        'publisher': None if p is None else {'in_session': p.in_session, 'quiet_s': round(now - p.last_rx, 1)},
        'history_rows': len(H.history),
        'state_rows': H.state_rows,
        'allow_test_streams': ALLOW_TEST,
        'frames_in': H.frames_in,
        'fps_in': H.fps_in if H.live else 0.0,
        'max_viewers': MAX_VIEWERS,
        'max_viewers_per_address': MAX_PER_IP,
        'idle_after_s': IDLE_S,
        'uptime_s': round(time.time() - H.started),
        'counts': dict(H.counts),
    }, headers=_NO_CACHE)


@app.get('/healthz')
async def healthz():
    return JSONResponse({'ok': True}, headers=_NO_CACHE)


class _SiteFiles(StaticFiles):
    """StaticFiles for http only (a websocket to an unknown path is closed, not crashed on)."""
    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            await super().__call__(scope, receive, send)
        elif scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 1008})


if SERVE_SITE and os.path.isdir(SITE_DIR):
    app.mount('/', _SiteFiles(directory=SITE_DIR, html=True), name='site')   # last, so the routes above win
else:
    if SERVE_SITE:
        say(f'SERVE_SITE=1 but {SITE_DIR} does not exist; not serving the site')

    @app.get('/')
    async def root():
        return JSONResponse({'service': 'labrat relay', 'live': '/live', 'status': '/status', 'healthz': '/healthz'},
                            headers=_NO_CACHE)
