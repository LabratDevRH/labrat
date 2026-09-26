"""Publish a training run to the labrat relay, for the website's live view.

    PowerShell:  $env:LABRAT_PUBLISH_TOKEN = '<token>'       (the relay's LABRAT_PUBLISH_TOKEN)
    python live/publish_training.py --relay wss://<relay host>/publish --watch runs
    python live/publish_training.py --relay ws://localhost:4720/publish --run runs/steer_v2
    python live/publish_training.py --relay wss://<relay host>/publish --watch runs --channel maze
    python live/publish_training.py --dry-print --run runs/steer_v1 --assume-live-for-test --duration 10

RELAY CHANNEL: by default the stream goes to the relay's default training channel (the one the site's 3D view and
Rat Tiles panel read, and the one live/buyback.py counts). --channel maze (or the environment variable
LABRAT_RELAY_CHANNEL=maze; the flag wins) sends it to the relay's second training channel instead, by appending
?channel=maze to the publish URL: the same protocol, the same token, its own publisher slot on the relay, forwarded to
viewers marked as the maze channel's (relay/relay.py "The maze channel"). That channel carries Rat Maze runs only
(the relay closes a hello with another task with 1008) and is what the site's /burn page and the burn engine read.
The Rat Maze trainer on Railway (trainer/README.md, labrat-trainer-maze) runs with LABRAT_RELAY_CHANNEL=maze.
The channel name is a short lower-case word; "training" (or empty) means the default channel, and "pons" is refused
here (that channel is the buy rig's, live/buyrig.py). A relay that does not know the channel refuses the handshake
with HTTP 400, which ends this script (exit 2) like a wrong token would: retrying cannot help.

WHAT VIEWERS SEE (say it this way): the LATEST SAVED TRAINING CHECKPOINT, PLAYING IN ITS OWN SIMULATION. train.py's
workers are not watched. Every 5 PPO iterations train.py appends a row to runs/<name>/log.jsonl and saves
runs/<name>/policy_last.pt atomically (network + observation-normalisation stats). This script loads that file
with ptload (no torch), plays its MEAN action (deterministic, no exploration noise) in ONE env of its own, built
as train.make_env builds the run's task (nominal conditions, no domain randomisation; the cursor tasks use the
curriculum difficulty of the latest log row), in real time (--fps 25, 2 control steps of env.CTRL_DT per
frame), episode after episode. A new checkpoint (sha256 changed) is swapped in at the next episode start and
announced. Every new log.jsonl row is forwarded verbatim.

Messages (the relay contract; binary frames in live/labrat_frame.py's layout):
    {"type":"hello","source":"training","task":..,"run":..,"label":..,"fps":25,"started":iso}
    binary frame (1,868 bytes)            {"type":"metrics","row":{log.jsonl row}}
    {"type":"checkpoint","steps":int,"sha256":str}
    {"type":"episode","n":int,"presses":int,"hits":int,"misses":int,"fell":bool[,"missed":int]}      {"type":"bye"}
On every (re)connect it first sends hello, the checkpoint playing now and the last --backlog log rows (so a
restarted relay, or one that starts a fresh history on each hello, gets the run's curve back).
A relay that closes the stream with 1008 (policy violation: relay/relay.py refuses a hello that is not live
training, e.g. a TEST stream unless it runs with RELAY_ALLOW_TEST=1) or 4401/4403 ends the session: retrying
would be refused the same way.
"presses" counts presses that registered (lever task: a clean press; cursor tasks: every click), "hits" the
clean presses / on-target clicks, "misses" the off-target clicks.

Rat Tiles (task "tiles", tiles_env.py, game rule v2): the live view plays WHOLE SONGS, the songs of assets/songs.json in
turn (training plays EP_TILES-note phrases; the rest is as training builds it), one song per episode, and also sends
    {"type":"tiles","t":sim_time,"song":id,"speed":screen_heights_per_s,"lanes":4,"hit_y":0.86,"window":half_height,
     "cursor":[x,y],"pressing":bool,"tiles":[[id,lane,y_center,h,state],...],"note_i":int}
                                                                   every 3rd frame (<= 10 Hz; droppable like a frame)
    {"type":"tile","id":int,"lane":int,"result":"hit"|"miss"|"wrong","note_i":int,"song":id[,"timing":s]}
                                                                   once per outcome
"tiles" lists every tile on screen (state "up" | "hit" | "miss": a tapped tile scrolls on, grey; a missed one scrolls
on past the hit line; y_center and h in screen heights, y down, a tile may reach past the top edge while it slides in).
Each lane has a red button on the hit line at y = hit_y; the hit band is hit_y +- window (screen heights). The cursor's
x picks the lane (its y does not count); "pressing" is true while a lever press (the lever-press network) is running.
note_i is the lowest untapped tile's note (the next note to play; the song's length when none is left). A "tile" message
says a tile was tapped ("hit": the cursor was in its lane while it overlapped the hit band; play note_i of the song;
"timing" = seconds late (+) or early (-) of perfect, the tile's centre on the hit line), passed the hit band untapped
("miss"), or that a press was anything else ("wrong": the wrong lane, or no tile on the hit band in that lane; id and
note_i are the lowest tile's, lane is the lane the cursor was in). The binary frame's target is the on-screen part of
the lowest untapped tile. Episode messages as for the other tasks: hits = tiles tapped, misses = wrong presses, one
episode per song, plus "missed" = tiles that passed the hit band untapped. After every press, and after a fall, tiles_env puts the rat back in its standing start pose (a new
trial, as the launch rig does between steps; the frames show it); a fall does not end the song, so "fell" says it fell
at least once during the song.

Rat Maze (task "maze", maze_env.py, game rule v1): the live view plays COURSES of maze_env.LIVE_MAZES mazes per episode,
each maze one curriculum notch harder than the one before (training plays one maze per episode; the rest is as training
builds it), and also sends
    {"type":"maze","t":sim_time,"maze_id":int,"w":int,"h":int,"walls":str|null,"cell":[cx,cy],"pos":[x,y],
     "cheese":[gx,gy],"trail":[[cx,cy],...],"bumps":int,"steps":int,"dist":int}
                                                                   every 4th frame (<= 8 Hz; droppable like a frame,
                                                                   except the first snapshot of a maze, which carries
                                                                   "walls" and is never dropped or replaced)
    {"type":"maze_end","maze_id":int,"result":"escaped"|"timeout","steps":int,"bumps":int,"time_s":float}
                                                                   once per maze
"walls" is the maze's layout: one hex char per cell, row-major (row 0 at the top), the bits N=8, E=4, S=2, W=1 set where
that side of the cell is OPEN; it is sent only in a maze's first snapshot (a new maze_id), and again after a reconnect
to the relay, otherwise "walls" is null. "cell" is the marker's cell, "pos" its position in cell units (cell (cx,cy)
spans x in [cx,cx+1), y in [cy,cy+1)), "cheese" the exit cell, "trail" the last 40 cells visited (the current one
last), "bumps" the wall bumps, "steps" the control steps and "dist" the shortest path (cells) to the cheese from the
marker's cell. The binary frame's target is the cheese cell and its cursor the marker. Episode messages as for the
other tasks: hits = mazes escaped in the course, misses = mazes timed out (a course ends at its first timeout or after
LIVE_MAZES escapes), presses = hits + misses. A lever press has no role in the maze (maze_env ignores the PRESS output).
After a fall, and between the mazes of a course, maze_env puts the rat back in its standing start pose.

LIVE only while training: a run is live while its log.jsonl was written in the last --silence s (90). When it
goes silent the script sends bye; --run then exits, --watch waits for the next run whose log.jsonl is written.
--watch logs and skips a run it cannot publish (e.g. an unknown network shape) until that run stops training.
--assume-live-for-test streams an old run anyway (for tests): hello.label then starts with "TEST".
--dry-print sends nothing: stdout gets "TEXT <json>" and "FRAME n=.. t=.. ... b64=<the frame>" lines (logs go
to stderr), through the same queue and resync logic as a relay connection.
Task (lever | cursor | steer | tiles | maze) comes from the checkpoint ("task", which train.py saves for tiles and
maze) or its shapes (5 outputs = steer, or tiles on tiles_env.OBS_DIM inputs, or maze on maze_env.OBS_DIM inputs; 38
outputs on a 200-input network = lever, wider = cursor) unless --task says so.

Light on the machine: one process, one env, 25 fps, one BLAS thread, never blocks on the network (a bounded
send queue drops the oldest frames when the relay is slow; reconnects back off 1 s .. 30 s), and it holds
policy_last.pt open only for one read (on Windows train.py's os.replace fails while a file is held open).
The token comes from the environment variable LABRAT_PUBLISH_TOKEN only; it is sent as an Authorization header
and never printed. This script never reads .env, never touches the launch journal, and signs or sends no
transaction.
"""
import os

for _k in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ.setdefault(_k, '1')   # idle BLAS threads spin; one thread keeps the cores for training

import argparse  # noqa: E402
import base64  # noqa: E402
import collections  # noqa: E402
import hashlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import random  # noqa: E402
import re  # noqa: E402
import signal  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse  # noqa: E402

# MuJoCo compiling scene.xml needs ~1 MiB of C stack; some python.exe builds give the main thread only 1 MiB,
# so all simulation work runs on a thread with a bigger stack (as live/rig.py does).
THREAD_STACK = 4 * 1024 * 1024
threading.stack_size(THREAD_STACK)

LIVE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(LIVE)
sys.path.insert(0, ROOT)
sys.path.insert(0, LIVE)

import numpy as np  # noqa: E402

from env import CTRL_DT  # noqa: E402
from ptload import load as pt_load, NumpyPolicy  # noqa: E402
import labrat_frame as lf  # noqa: E402

TASKS = ('lever', 'cursor', 'steer', 'tiles', 'maze')
GAME_TASKS = ('tiles', 'maze')   # the games: they also stream a board (tiles / maze) and events (tile / maze_end)
TASK_TEXT = {
    'lever': 'pressing the lever with the whole body',
    'cursor': 'moving a cursor with its head and clicking with a lever press, whole body',
    'steer': 'steering a cursor with its head and clicking with a lever press',
    'tiles': 'playing Rat Tiles: moving between the lanes with its head and pressing the lever as each tile reaches '
             'the red button on the hit line',
    'maze': 'playing Rat Maze: steering a marker through a maze with its head, to the cheese at the exit',
}
TOKEN_ENV = 'LABRAT_PUBLISH_TOKEN'
CHANNEL_ENV = 'LABRAT_RELAY_CHANNEL'   # the relay channel when --channel is not given ("maze" on the maze trainer)
DEFAULT_CHANNEL_WORDS = ('', 'training', 'default', 'none')   # all mean: the relay's default training channel
CHANNEL_RE = re.compile(r'^[a-z][a-z0-9_-]{0,31}$')
PONS_CHANNEL = 'pons'        # the buy rig's channel (live/buyrig.py): never a training stream
POLL_S = 1.0                 # how often the run's files are checked
AUTH_CLOSE_CODES = (4401, 4403)
TILES_MAX_HZ = 10            # Rat Tiles board snapshots per second, at most
MAZE_MAX_HZ = 8              # Rat Maze snapshots per second, at most
WALLS_RESEND_S = 2.0         # Rat Maze: the layout is sent again this long after a (re)connect to the relay (after its
                             # resync, which clears the queue), so a relay that lost it gets it back


def log(*parts):
    try:
        stamp = datetime.now(timezone.utc).strftime('%H:%M:%S')
        print(f'[publish {stamp}] ' + ' '.join(str(p) for p in parts), file=sys.stderr, flush=True)
    except Exception:
        pass


def iso_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def clean_json(o):
    """log rows may hold NaN / inf (json.dumps writes them, browsers' JSON.parse rejects them): -> null."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {str(k): clean_json(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean_json(v) for v in o]
    return o


def dumps(msg):
    return json.dumps(clean_json(msg), separators=(',', ':'), allow_nan=False)


# ---------------------------------------------------------------------------------------------- run files
def log_age(run_dir):
    """Seconds since the run's log.jsonl was written, or None if it has none."""
    try:
        return max(0.0, time.time() - os.stat(os.path.join(run_dir, 'log.jsonl')).st_mtime)
    except OSError:
        return None


def find_active(watch_dir, silence, exclude=()):
    """The run under watch_dir whose log.jsonl was written most recently, within `silence` s (runs in `exclude`
    are skipped)."""
    best = None
    try:
        names = os.listdir(watch_dir)
    except OSError:
        return None
    for name in names:
        d = os.path.join(watch_dir, name)
        if d in exclude:
            continue
        age = log_age(d)
        if age is not None and age <= silence and (best is None or age < best[0]):
            best = (age, d)
    return best[1] if best else None


class Checkpoints:
    """policy_last.pt, re-read only when its (mtime, size) changes, announced only when its sha256 changes."""

    def __init__(self, run_dir):
        self.path = os.path.join(run_dir, 'policy_last.pt')
        self.key = None
        self.sha = None

    def poll(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        key = (st.st_mtime_ns, st.st_size)
        if key == self.key:
            return None
        try:
            with open(self.path, 'rb') as f:      # one read, then closed at once (train.py os.replace()s it)
                data = f.read()
        except OSError:
            return None                            # being replaced right now: next poll
        self.key = key
        sha = hashlib.sha256(data).hexdigest()
        if sha == self.sha:
            return None
        try:
            ck = pt_load(io.BytesIO(data))
            ck['net']['pi.6.weight']
        except Exception as e:
            log(f'could not read {self.path} ({type(e).__name__}: {e}); waiting for the next save')
            return None
        self.sha = sha
        return sha, ck


def infer_task(ck):
    out = int(ck['net']['pi.6.weight'].shape[0])
    obs = int(len(ck['mean']))
    if ck.get('task') in TASKS:
        return ck['task']
    if out == 5:
        from tiles_env import OBS_DIM as TILES_OBS
        from maze_env import OBS_DIM as MAZE_OBS
        return 'tiles' if obs == TILES_OBS else 'maze' if obs == MAZE_OBS else 'steer'
    if out == 38:
        return 'lever' if obs <= 200 else 'cursor'
    raise ValueError(f'unknown checkpoint shape: {obs} inputs, {out} outputs')


class LogTail:
    """New complete rows of log.jsonl (opened, read and closed on each poll; the trainer appends to it)."""

    def __init__(self, path):
        self.path = path
        self.pos = 0
        self.buf = b''

    def read_new(self):
        try:
            with open(self.path, 'rb') as f:
                f.seek(0, 2)
                size = f.tell()
                if size < self.pos:              # truncated or recreated
                    self.pos, self.buf = 0, b''
                f.seek(self.pos)
                data = f.read()
                self.pos = f.tell()
        except OSError:
            return []
        lines = (self.buf + data).split(b'\n')
        self.buf = lines.pop()                   # an incomplete last line waits for its newline
        rows = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                row = json.loads(ln)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows


# ---------------------------------------------------------------------------------------------- the sim
class Player:
    """One env of the run's task, playing a checkpoint's mean action."""

    def __init__(self, task, seed):
        import train                              # train.make_env: the env exactly as training builds it
        self.task = task
        self.env = train.make_env(task, seed, False)
        if task == 'tiles':
            self.env.full_songs = True            # the live view plays whole songs (training: phrases of them)
        if task == 'maze':
            from maze_env import LIVE_MAZES
            self.env.mazes_per_episode = LIVE_MAZES   # the live view plays courses of mazes (training: one maze)
        # SteerEnv (and TilesEnv / MazeEnv, SteerEnvs) wraps a CursorEnv
        self.inner = self.env.e if task in ('steer', 'tiles', 'maze') else self.env
        self.m, self.d = self.inner.m, self.inner.d
        self.pose = lf.PoseReader(self.m)
        self.pol = None
        self.episode = -1
        self.obs = None
        self.events = []                          # Rat Tiles / Rat Maze: tile outcomes / maze ends not yet sent
        self.walls_sent = None                    # Rat Maze: the maze_id whose layout ("walls") went out last

    def set_policy(self, ck):
        self.pol = NumpyPolicy(ck, self.env.obs_dim)

    def reset(self, difficulty=None):
        if difficulty is not None and self.task != 'lever':
            self.env.difficulty = float(min(max(difficulty, 0.0), 1.0))
        self.obs = self.env.reset()
        self.episode += 1
        self.pressed = False
        self.fell = False
        self.click = False
        self.click_target = None

    def lit_target(self):
        e = self.inner
        if self.task == 'tiles':
            return e.visible_active_rect()        # the on-screen part of the lowest untapped tile
        if self.task == 'maze':
            return e.cheese_rect()                # the cheese cell (the marker is the cursor)
        if self.task == 'lever' or e.hold != 0:
            return None
        return (float(e.tc[0]), float(e.tc[1]), float(e.th[0]), float(e.th[1]))

    def step(self):
        """One control step (CTRL_DT). Returns True when the episode is over."""
        before = self.lit_target()
        a = self.pol(self.obs).astype(np.float64)
        obs, _r, done, info = self.env.step(a)
        if self.task in GAME_TASKS:
            self.events.extend(self.env.drain_events())
        if not np.all(np.isfinite(obs)):
            log('the simulation went unstable (non-finite observation); new episode')
            self.fell = True
            return True
        self.obs = obs
        if self.task == 'lever':
            if info.get('pressed') and not self.pressed:
                self.pressed = True
                self.click = True
        elif info.get('click'):
            self.click = True
            self.click_target = before           # a hit moves on to the next target in the same step
        self.fell = bool(info.get('fell'))
        return bool(done)

    def frame(self):
        e = self.inner
        cursor = None if self.task == 'lever' else (float(e.cursor[0]), float(e.cursor[1]))
        target = self.click_target if (self.click and self.click_target is not None) else self.lit_target()
        b = lf.pack(float(self.d.time), self.episode, e.lever_angle(), self.click, cursor, target,
                    self.pose(self.d.qpos))
        self.click = False
        self.click_target = None
        return b

    def tiles_msg(self):
        """Rat Tiles: the board now ({"type":"tiles"}), or None for the other tasks."""
        if self.task != 'tiles' or self.inner.game is None:
            return None
        return self.inner.snapshot(float(self.d.time))

    def maze_msg(self, resend_walls=False):
        """Rat Maze: the maze now ({"type":"maze"}), or None for the other tasks. "walls" (the layout) is in the first
        snapshot of each maze (and when asked again, after a reconnect); the others carry null."""
        if self.task != 'maze' or self.inner.maze is None:
            return None
        mid = int(self.inner.maze_id)
        walls = resend_walls or mid != self.walls_sent
        if walls:
            self.walls_sent = mid
        return self.inner.snapshot(float(self.d.time), walls)

    def drain_events(self):
        ev, self.events = self.events, []
        return ev

    def episode_msg(self):
        if self.task == 'lever':
            hits = presses = int(self.pressed)
            misses = 0
        elif self.task == 'maze':
            hits, misses = int(self.inner.hits), int(self.inner.timeouts)   # mazes escaped, mazes timed out
            presses = hits + misses
        else:
            hits, misses = int(self.inner.hits), int(self.inner.misses)
            presses = hits + misses
        msg = {'type': 'episode', 'n': self.episode, 'presses': presses, 'hits': hits, 'misses': misses,
               'fell': bool(self.fell)}
        if self.task == 'tiles':
            msg['missed'] = int(self.inner.timeouts)          # tiles that passed the hit band untapped
        return msg


# ---------------------------------------------------------------------------------------------- the link
class Fatal(Exception):
    pass


class DryConn:
    """--dry-print: messages go to stdout instead of the relay (TEXT <json> / FRAME <summary> b64=<bytes>)."""

    def __init__(self):
        self.n = 0

    def send(self, payload):
        if isinstance(payload, (bytes, bytearray)):
            f = np.frombuffer(payload, dtype='<f4')
            line = (f'FRAME n={self.n} bytes={len(payload)} t={f[1]:.3f} ep={int(f[2])} lever={f[3]:+.4f} '
                    f'click={int(f[4])} cursor={f[5]:.4f},{f[6]:.4f} target={f[7]:.4f},{f[8]:.4f},{f[9]:.4f},'
                    f'{f[10]:.4f} b64={base64.b64encode(payload).decode()}')
            self.n += 1
        else:
            line = 'TEXT ' + payload
        sys.stdout.write(line + '\n')
        sys.stdout.flush()

    def recv(self, timeout=None):
        raise TimeoutError

    def close(self):
        pass


class Link:
    """The relay connection, on its own thread. The sim thread only appends to a bounded queue and never waits
    on the network; the oldest frames are dropped first when the relay is slow or away."""

    MAX_FRAMES = 50            # 2 s at 25 fps
    MAX_TEXTS = 500

    def __init__(self, relay, token, dry, backlog):
        self.relay, self.token, self.dry = relay, token, dry
        self.cv = threading.Condition()
        self.q = collections.deque()
        self.nframes = 0
        self.hello = None
        self.checkpoint = None
        self.rows = collections.deque(maxlen=max(0, backlog))
        self.need_sync = True
        self.stopping = False
        self.fatal = None
        self.connected = False
        self.stats = collections.Counter()
        self.thread = threading.Thread(target=self._run, name='relay-link', daemon=True)

    # ---- producer side (the sim thread)
    def start(self):
        self.thread.start()

    def set_hello(self, msg, rows=()):
        with self.cv:
            self.hello = msg
            self.checkpoint = None
            self.rows.clear()
            self.rows.extend(rows)
            self.q.clear()
            self.nframes = 0
            self.need_sync = True
            self.cv.notify()

    def set_checkpoint(self, msg):
        with self.cv:
            self.checkpoint = msg
            self._put('t', msg)

    def add_row(self, row):
        with self.cv:
            self.rows.append(row)
            self._put('t', {'type': 'metrics', 'row': row})

    def text(self, msg):
        with self.cv:
            self._put('t', msg)

    def board(self, msg):
        """A Rat Tiles board snapshot: like a frame, only the newest matters, so one not yet sent is replaced (and it
        is never part of a resync)."""
        with self.cv:
            for i, (kind, _p) in enumerate(self.q):
                if kind == 'e':
                    del self.q[i]
                    self.stats['boards_replaced'] += 1
                    break
            self._put('e', msg)

    def frame(self, b):
        with self.cv:
            if self.nframes >= self.MAX_FRAMES:
                for i, (kind, _p) in enumerate(self.q):
                    if kind == 'b':
                        del self.q[i]
                        break
                self.nframes -= 1
                self.stats['frames_dropped'] += 1
            self.q.append(('b', b))
            self.nframes += 1
            self.cv.notify()

    def _put(self, kind, msg):
        if len(self.q) - self.nframes >= self.MAX_TEXTS:
            for i, (k, _p) in enumerate(self.q):
                if k in ('t', 'e'):
                    del self.q[i]
                    break
            self.stats['texts_dropped'] += 1
        self.q.append((kind, dumps(msg)))
        self.cv.notify()

    def close(self, flush_s=3.0):
        """Send what is queued (up to flush_s), then close."""
        deadline = time.monotonic() + flush_s
        with self.cv:
            while (self.q or self.need_sync) and self.connected and time.monotonic() < deadline:
                self.cv.wait(0.05)
            self.stopping = True
            self.cv.notify_all()
        if self.thread.is_alive():
            self.thread.join(timeout=5)

    # ---- sender side (its own thread)
    def _open(self):
        if self.dry:
            return DryConn()
        from websockets.sync.client import connect
        from websockets.exceptions import InvalidStatus
        try:
            return connect(self.relay, additional_headers={'Authorization': f'Bearer {self.token}'},
                           open_timeout=10, close_timeout=3, compression=None, max_size=2 ** 20,
                           ping_interval=20, ping_timeout=20, user_agent_header='labrat-publisher/1')
        except InvalidStatus as e:
            code = e.response.status_code
            if code in (401, 403):
                raise Fatal(f'the relay refused the publish token (HTTP {code}); check {TOKEN_ENV}') from None
            if code == 400:
                raise Fatal(f'the relay does not know this publish channel (HTTP 400); check --channel / '
                            f'{CHANNEL_ENV} (the relay may need redeploying with the channel)') from None
            if code == 409:
                raise ConnectionError('HTTP 409: another publisher is streaming to this relay') from None
            if code == 503:
                raise ConnectionError(f'HTTP 503: publishing is disabled on the relay (its {TOKEN_ENV} is unset '
                                      f'or too short)') from None
            raise

    def _snapshot(self):
        items = []
        if self.hello is not None:
            items.append(('t', dumps(self.hello)))
        if self.checkpoint is not None:
            items.append(('t', dumps(self.checkpoint)))
        items += [('t', dumps({'type': 'metrics', 'row': r})) for r in self.rows]
        return items

    def _drain(self, conn):
        """Read (and ignore) whatever the relay sends, so its receive side keeps moving."""
        for _ in range(100):
            try:
                msg = conn.recv(timeout=0)
            except TimeoutError:
                return
            if isinstance(msg, str) and self.stats['relay_texts'] < 20:
                log('relay says:', msg[:200])
            self.stats['relay_texts'] += 1

    def _pump(self, conn):
        while True:
            with self.cv:
                if not self.q and not self.need_sync:
                    if self.stopping:
                        return
                    self.cv.wait(0.25)
                if self.need_sync:
                    if self.stopping:
                        return                    # the session has ended: never replay its hello / rows now
                    items = self._snapshot()
                    self.q.clear()
                    self.nframes = 0
                    self.need_sync = False
                elif self.q:
                    kind, payload = self.q.popleft()
                    if kind == 'b':
                        self.nframes -= 1
                    items = [(kind, payload)]
                else:
                    items = []
            for kind, payload in items:
                conn.send(payload)
                self.stats['frames_sent' if kind == 'b' else 'texts_sent'] += 1
            if not items:
                with self.cv:
                    self.cv.notify_all()          # close() waits for an empty queue
            self._drain(conn)

    def _wait(self, seconds):
        with self.cv:
            end = time.monotonic() + seconds
            while not self.stopping and time.monotonic() < end:
                self.cv.wait(min(0.25, end - time.monotonic()))

    def _run(self):
        backoff = 1.0
        last_err = None
        while True:
            with self.cv:
                if self.stopping:
                    return
            try:
                conn = self._open()
            except Fatal as e:
                self.fatal = str(e)
                log(self.fatal)
                return
            except Exception as e:
                err = f'{type(e).__name__}: {e}'
                if err != last_err:
                    log(f'cannot reach the relay ({err}); retrying with backoff')
                last_err = err
                self._wait(backoff * random.uniform(1.0, 1.5))
                backoff = min(backoff * 2, 30.0)
                continue
            with self.cv:
                late = self.stopping              # close() gave up while this connect was still in progress
                if not late:
                    self.need_sync = True
                    self.connected = True
            if late:
                self._shut(conn)                  # never resync a session that has already ended
                return
            if not self.dry:
                log(f'connected to {self.relay}')
            last_err = None
            backoff = 1.0
            self.stats['connects'] += 1
            try:
                self._pump(conn)
                self._shut(conn)
                return
            except Exception as e:
                with self.cv:
                    self.connected = False
                self._shut(conn)
                rcvd = getattr(e, 'rcvd', None)
                code = getattr(rcvd, 'code', None)
                reason = getattr(rcvd, 'reason', '') or ''
                if code in AUTH_CLOSE_CODES:
                    self.fatal = f'the relay refused this publisher (close {code} {reason!r}); check {TOKEN_ENV}'
                    log(self.fatal)
                    return
                if code == 1008:                  # policy violation: the relay refuses what we send (e.g. TEST)
                    self.fatal = f'the relay refused this stream (close 1008 {reason!r}); not retrying'
                    log(self.fatal)
                    return
                log(f'relay connection lost ({type(e).__name__}{f" {code} {reason!r}" if code else ""}: {e}); '
                    f'reconnecting')
                self._wait(backoff * random.uniform(1.0, 1.5))
                backoff = min(backoff * 2, 30.0)
            finally:
                with self.cv:
                    self.connected = False

    @staticmethod
    def _shut(conn):
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------------------------- one session
def publish(run_dir, a, token, stop):
    """Stream one run until it goes silent (or --duration, Ctrl+C, a fatal relay refusal).
    Returns 'silent' | 'duration' | 'stopped' | 'fatal' | 'task-changed' | 'no-checkpoint'."""
    name = os.path.basename(os.path.normpath(run_dir))
    test = a.assume_live_for_test

    def live():
        if test:
            return True
        age = log_age(run_dir)
        return age is not None and age <= a.silence

    ckpts = Checkpoints(run_dir)
    tail = LogTail(os.path.join(run_dir, 'log.jsonl'))
    rows = tail.read_new()
    got = ckpts.poll()
    while got is None:                           # a fresh run saves its first checkpoint after 5 iterations
        if stop.is_set():
            return 'stopped'
        if not live():
            log(f'{name}: no readable policy_last.pt while it was training')
            return 'no-checkpoint'
        stop.wait(POLL_S)
        got = ckpts.poll()
        rows += tail.read_new()
    sha, ck = got
    task = a.task or infer_task(ck)
    difficulty = next((r['difficulty'] for r in reversed(rows) if isinstance(r.get('difficulty'), (int, float))), None)

    player = Player(task, a.seed)
    player.set_policy(ck)
    label = (f'Training run {name}: the latest saved checkpoint ({TASK_TEXT[task]}), playing in its own '
             f'simulation')
    if test:
        label = f'TEST (not a live training run): {name}\'s last saved checkpoint ({TASK_TEXT[task]}), ' \
                f'playing in its own simulation'
    hello = {'type': 'hello', 'source': 'training', 'task': task, 'run': name, 'label': label, 'fps': a.fps,
             'started': iso_now()}
    if test:
        hello['test'] = True

    link = Link(a.relay, token, a.dry_print, a.backlog)
    link.set_hello(hello, rows[-a.backlog:] if a.backlog else [])
    link.set_checkpoint({'type': 'checkpoint', 'steps': int(ck.get('steps', 0)), 'sha256': sha})
    link.start()
    reason = None
    try:
        log(f'publishing {name} (task {task}, checkpoint {int(ck.get("steps", 0)):,} steps, sha256 {sha[:16]}..., '
            f'{len(rows)} log rows{", TEST" if test else ""}) at {a.fps} fps'
            + ('' if a.dry_print else f' to {a.relay}'))
        player.reset(difficulty)

        steps_per_frame = 1.0 / (a.fps * CTRL_DT)
        # Rat Tiles board / Rat Maze snapshots: every n-th frame
        board_every = max(1, math.ceil(a.fps / (MAZE_MAX_HZ if task == 'maze' else TILES_MAX_HZ)))
        cpu0 = time.process_time()
        t_start = time.monotonic()
        t0 = t_start
        k = 0                   # frames sent this session
        steps = 0               # control steps simulated (the sim clock, in CTRL_DT)
        pending = None          # a newer checkpoint, swapped in at the next episode start
        next_poll = t_start + POLL_S
        reason = None
        n_eps = 0
        connects = 0            # Rat Maze: the layout goes out again WALLS_RESEND_S after a (re)connect, when the
        walls_due = None        # layout was queued for an earlier connection (the resync of a new one clears the queue)
        walls_conn = None       # the connection count when the layout was last queued
        while reason is None:
            k += 1
            target_steps = int(round(k * steps_per_frame))
            done = False
            try:
                while steps < target_steps:
                    steps += 1
                    if player.step():
                        done = True               # the rest of this frame's sim time is dropped, not carried over
                        break
                link.frame(player.frame())
                for ev in player.drain_events():  # Rat Tiles / Maze: each outcome, after the frame it happened in
                    link.text(ev)
                if k % board_every == 0:
                    if task == 'maze':
                        now_c = link.stats['connects']
                        if now_c != connects:
                            connects, walls_due = now_c, time.monotonic() + WALLS_RESEND_S
                        resend = False
                        if walls_due is not None and time.monotonic() >= walls_due:
                            walls_due = None
                            resend = walls_conn != connects
                        board = player.maze_msg(resend)
                        if board is not None:
                            if board['walls'] is not None:
                                link.text(board)      # a maze's layout: never dropped or replaced
                                walls_conn = connects
                            else:
                                link.board(board)
                    else:
                        board = player.tiles_msg()
                        if board is not None:
                            link.board(board)
            except Exception as e:                    # never let one bad step end the stream
                log(f'simulation error ({type(e).__name__}: {e}); new episode')
                done = True
                player.fell = True
            steps = target_steps
            if done:
                link.text(player.episode_msg())
                n_eps += 1
                if pending is not None:
                    sha, ck = pending
                    pending = None
                    try:
                        if not a.task and infer_task(ck) != task:
                            log(f'{name}: the run now trains a different task ({infer_task(ck)}); restarting')
                            reason = 'task-changed'
                            break
                        player.set_policy(ck)
                        link.set_checkpoint({'type': 'checkpoint', 'steps': int(ck.get('steps', 0)), 'sha256': sha})
                        log(f'checkpoint {int(ck.get("steps", 0)):,} steps (sha256 {sha[:16]}...) now playing')
                    except Exception as e:
                        log(f'could not use the new checkpoint ({type(e).__name__}: {e}); keeping the old one')
                try:
                    player.reset(difficulty)
                except Exception as e:
                    log(f'reset failed ({type(e).__name__}: {e}); rebuilding the env')
                    episode = player.episode
                    player = Player(task, a.seed + n_eps)
                    player.set_policy(ck)
                    player.episode = episode
                    player.reset(difficulty)

            now = time.monotonic()
            if now >= next_poll:
                next_poll = now + POLL_S
                got = ckpts.poll()
                if got is not None:
                    pending = got
                for row in tail.read_new():
                    link.add_row(row)
                    if isinstance(row.get('difficulty'), (int, float)):
                        difficulty = row['difficulty']
                if stop.is_set():
                    reason = 'stopped'
                elif link.fatal:
                    reason = 'fatal'
                elif a.duration and now - t_start >= a.duration:
                    reason = 'duration'
                elif not live():
                    log(f'{name}: log.jsonl silent for over {a.silence:g} s: training stopped')
                    reason = 'silent'
            # real-time pacing: frame k is due at t0 + k / fps
            due = t0 + k / a.fps
            now = time.monotonic()
            if due > now:
                stop.wait(due - now)
            elif now - due > 1.0:                     # fell far behind (machine busy or asleep): skip, don't burst
                t0 = now - k / a.fps
    finally:
        # whatever happens, end this session on the relay (bye, unless it refused us) and stop the link thread
        if reason != 'fatal':
            link.text({'type': 'bye'})
        link.close(flush_s=3.0)
    s = link.stats
    wall = max(time.monotonic() - t_start, 1e-6)
    cpu = time.process_time() - cpu0
    log(f'{name}: {reason}; {k} frames, {n_eps} episodes, sent {s["frames_sent"]} frames + {s["texts_sent"]} '
        f'messages, dropped {s["frames_dropped"]} frames, {s["connects"]} connection(s); CPU {cpu:.1f} s in '
        f'{wall:.1f} s ({100 * cpu / wall:.0f}% of one core)')
    return reason


# ---------------------------------------------------------------------------------------------- main
def check_relay_url(url, allow_insecure):
    u = urlparse(url)
    if u.scheme not in ('ws', 'wss') or not u.hostname:
        raise SystemExit(f'--relay must be a ws:// or wss:// URL (got {url!r})')
    local = u.hostname in ('localhost', '127.0.0.1', '::1')
    if u.scheme == 'ws' and not local and not allow_insecure:
        raise SystemExit('refusing to send the publish token over plain ws:// to a remote host; use wss:// '
                         '(or --allow-insecure)')
    if not u.path.rstrip('/').endswith('/publish'):
        log(f'note: the relay publish endpoint is usually .../publish (got path {u.path or "/"!r})')


def resolve_channel(flag, env=None):
    """The relay channel to publish on: --channel if given, else LABRAT_RELAY_CHANNEL. Returns '' for the default
    training channel, else the channel name. Raises SystemExit on a name the relay could never take."""
    env = os.environ if env is None else env
    raw = flag if flag is not None else env.get(CHANNEL_ENV, '')
    where = '--channel' if flag is not None else CHANNEL_ENV
    channel = str(raw).strip().lower()
    if channel in DEFAULT_CHANNEL_WORDS:
        return ''
    if channel == PONS_CHANNEL:
        raise SystemExit(f'{where}: "pons" is the buy rig\'s channel (live/buyrig.py), not a training channel')
    if not CHANNEL_RE.match(channel):
        raise SystemExit(f'{where}: a channel is a short lower-case word (letters, digits, _ -), got {raw!r}')
    return channel


def with_channel(url, channel):
    """The publish URL with ?channel=<channel> (replacing any channel already in its query); unchanged for ''."""
    if not channel:
        return url
    u = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != 'channel']
    query.append(('channel', channel))
    return urlunparse(u._replace(query=urlencode(query)))


def run(a, token, stop):
    if a.run:
        run_dir = a.run if os.path.isabs(a.run) else os.path.join(os.getcwd(), a.run)
        if not os.path.isdir(run_dir):
            raise SystemExit(f'no such run directory: {a.run}')
        while True:
            if not a.assume_live_for_test:
                age = log_age(run_dir)
                if age is None or age > a.silence:
                    log(f'{os.path.basename(run_dir)} is not training (log.jsonl last written '
                        f'{"never" if age is None else f"{age:.0f} s ago"}); nothing to publish. '
                        f'(--assume-live-for-test streams it anyway, labelled TEST)')
                    return 0
            reason = publish(run_dir, a, token, stop)
            if reason != 'task-changed':
                return 2 if reason == 'fatal' else 0
    watch = a.watch if os.path.isabs(a.watch) else os.path.join(os.getcwd(), a.watch)
    if not os.path.isdir(watch):
        raise SystemExit(f'no such directory: {a.watch}')
    log(f'watching {watch} for a run whose log.jsonl is being written')
    waiting = False
    skipped = set()          # runs that could not be published: left alone until their log.jsonl goes silent
    while not stop.is_set():
        for d in list(skipped):
            age = log_age(d)
            if age is None or age > a.silence:
                skipped.discard(d)
        run_dir = find_active(watch, a.silence, skipped)
        if run_dir is None:
            if not waiting:
                log('no run is training; waiting')
                waiting = True
            stop.wait(5.0)
            continue
        waiting = False
        try:
            reason = publish(run_dir, a, token, stop)
        except Exception as e:                   # one odd run (a new network shape, an env that fails to build)
            import traceback                     # must not switch the live view off for every later run
            traceback.print_exc()
            log(f'{os.path.basename(run_dir)}: cannot publish this run ({type(e).__name__}: {e}); skipping it '
                f'until its log.jsonl goes silent')
            skipped.add(run_dir)
            stop.wait(POLL_S)
            continue
        if reason in ('fatal',):
            return 2
        if reason in ('stopped', 'duration'):
            return 0
        if reason == 'no-checkpoint':
            stop.wait(30.0)
    return 0


def main():
    ap = argparse.ArgumentParser(description='Stream a training run to the labrat relay: the latest saved '
                                             'checkpoint, playing in its own simulation.')
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--watch', metavar='DIR', help='publish whichever run under DIR is training (e.g. runs)')
    src.add_argument('--run', metavar='DIR', help='publish this run (e.g. runs/steer_v2)')
    ap.add_argument('--relay', help='the relay publish URL, ws(s)://<host>/publish')
    ap.add_argument('--channel', metavar='NAME',
                    help='the relay channel to publish on, appended as ?channel=NAME to --relay: "maze" is the '
                         'relay\'s second training channel (Rat Maze runs only; read by the site\'s /burn page and '
                         'the burn engine). Default: the environment variable LABRAT_RELAY_CHANNEL, else the default '
                         'training channel')
    ap.add_argument('--fps', type=int, default=25)
    ap.add_argument('--dry-print', action='store_true', help='print messages and frames instead of sending')
    ap.add_argument('--task', choices=TASKS, help='override the task read from the checkpoint')
    ap.add_argument('--seed', type=int, default=1, help='env seed (episode starts)')
    ap.add_argument('--silence', type=float, default=90.0, help='log.jsonl silent this long = training stopped')
    ap.add_argument('--backlog', type=int, default=300, help='log rows re-sent after each (re)connect')
    ap.add_argument('--duration', type=float, default=0, help='stop after this many seconds (tests)')
    ap.add_argument('--assume-live-for-test', action='store_true',
                    help='stream a run that is not training (hello.label starts with TEST)')
    ap.add_argument('--allow-insecure', action='store_true', help='allow ws:// to a non-local relay')
    a = ap.parse_args()
    if not 1 <= a.fps <= 50:
        raise SystemExit('--fps must be 1..50 (the env runs 50 control steps a second)')
    channel = resolve_channel(a.channel)
    token = ''
    if not a.dry_print:
        if not a.relay:
            raise SystemExit('--relay ws(s)://<host>/publish is required (or --dry-print)')
        check_relay_url(a.relay, a.allow_insecure)
        a.relay = with_channel(a.relay, channel)
        if channel:
            log(f'publishing on the relay\'s {channel!r} channel ({a.relay})')
        token = os.environ.get(TOKEN_ENV, '').strip()
        if not token:
            raise SystemExit(f'set the environment variable {TOKEN_ENV} to the relay\'s publish token')
        if len(token) < 16:
            log(f'warning: {TOKEN_ENV} is shorter than 16 characters; relay/relay.py treats a token that short '
                f'as unset and refuses every publisher')

    stop = threading.Event()
    result = {}

    def work():
        try:
            result['code'] = run(a, token, stop)
        except SystemExit as e:
            result['exit'] = e
        except BaseException as e:               # report, never a silent death
            import traceback
            traceback.print_exc()
            result['code'] = 1
            log(f'stopped by an error: {type(e).__name__}: {e}')

    def on_signal(signum, _frame):                # Ctrl+Break / SIGTERM: stop like Ctrl+C (bye, then exit)
        raise KeyboardInterrupt

    for name in ('SIGBREAK', 'SIGTERM'):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), on_signal)
            except (ValueError, OSError):
                pass

    t = threading.Thread(target=work, name='publisher', daemon=True)
    t.start()
    try:
        while t.is_alive():
            t.join(0.5)
    except KeyboardInterrupt:
        log('stopping: sending bye')
        stop.set()
        try:
            t.join(10)
        except KeyboardInterrupt:
            pass
    if 'exit' in result:
        raise result['exit']
    return result.get('code', 0)


if __name__ == '__main__':
    sys.exit(main())
