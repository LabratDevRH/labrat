"""Rat Tiles demo publisher: a scripted TEST stream for a LOCAL relay, to try the website's Rat Tiles panel without a
training run.

    PowerShell (the local relay started with RELAY_ALLOW_TEST=1 and SERVE_SITE=1, see relay/README.md):
    $env:LABRAT_PUBLISH_TOKEN = '<the local relay's token>'
    python relay/tiles_demo.py --relay ws://localhost:4801/publish --duration 90
    # open http://localhost:4801/buyback/#piano

Everything it sends is marked as a test: the hello has "test": true and a label starting with TEST, so relay.py refuses
it (close 1008) unless the relay runs with RELAY_ALLOW_TEST=1, and the site shows it as TEST STREAM, never as live.
It is not the rat's brain: the rat's poses are the recorded launch replay (site/replay/session.bin) in a loop, and the
cursor, the tiles and every hit, miss and off-tile press are scripted here (seeded, so a run is repeatable). It speaks
the relay's Rat Tiles protocol exactly as live/publish_training.py does for a "tiles" run:
    {"type":"hello","source":"training","task":"tiles",...}
    {"type":"tiles","t","song","speed","lanes":4,"cursor":[x,y],"tiles":[[id,lane,y_center,h,state],...],"note_i"}  10 Hz
    {"type":"tile","id","lane","result":"hit"|"miss"|"wrong","note_i","song"}      once per outcome
    {"type":"episode","n","presses","hits","misses","fell"}                        at the end of each attempt
    binary frames (live/labrat_frame.py layout) at 25 fps, the active tile's rect in the target fields
Lanes follow the shared rule: a note's lane is the rank of its pitch among the song's distinct pitches, mod 4, moved
one lane right when it would repeat the previous tile's lane. It reads no .env and signs nothing.
"""
import argparse
import asyncio
import json
import math
import os
import random
import struct
import sys
import time
from datetime import datetime, timezone

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, '..', 'site')
FRAME_FLOATS, FRAME_BYTES, HEADER = 467, 1868, 12
FPS = 25
SNAP_HZ = 10
LANES = 4
TILE_H = 0.25          # a tile's height, in screen heights (the env: 0.30 .. 0.20)
SPACING = 0.5          # centre to centre, in screen heights (the env: a beat is 0.3 .. 0.7 of the screen)
SEMI = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}


def midi(name):
    acc = 1 if '#' in name else -1 if (len(name) > 2 and name[1] == 'b') else 0
    return 12 * (int(name[-1]) + 1) + SEMI[name[0]] + acc


def lanes_for(notes):
    """The shared rule: rank of the pitch among the song's distinct pitches, mod 4; never the same lane twice in a row."""
    ranks = {p: i for i, p in enumerate(sorted({midi(n) for n, _ in notes}))}
    out, prev = [], None
    for n, _ in notes:
        lane = ranks[midi(n)] % LANES
        if lane == prev:
            lane = (lane + 1) % LANES
        out.append(lane)
        prev = lane
    return out


def load_poses():
    """The recorded launch replay's frames (the rat's body only; this demo overwrites every header field)."""
    with open(os.path.join(SITE, 'replay', 'session.bin'), 'rb') as f:
        blob = f.read()
    n = len(blob) // FRAME_BYTES
    return [blob[i * FRAME_BYTES:(i + 1) * FRAME_BYTES] for i in range(n)]


class Attempt:
    """One scripted attempt: a song's tiles falling at `speed`, and a scripted cursor and presses."""

    def __init__(self, n, song, speed, rng):
        self.n, self.song, self.speed, self.rng = n, song, speed, rng
        self.notes = song['notes']
        self.lanes = lanes_for(self.notes)
        self.state = ['up'] * len(self.notes)
        self.t = 0.0
        self.cursor = [0.5, 0.55]
        self.press_end = None          # sim time a press started now completes
        self.press_tile = None
        self.plan = {}                 # tile -> 'hit' | 'miss' | 'wrong'
        for k in range(len(self.notes)):
            r = rng.random()
            self.plan[k] = 'miss' if r < 0.14 else 'wrong' if r < 0.24 else 'hit'
        self.wrong_done = set()
        self.press_at = {}             # tile -> the tile height at which the rat decides to press
        self.hits = self.misses = self.presses = 0
        self.done_at = None

    def y(self, k):
        """Tile k's centre (screen heights, y down) now."""
        return -0.15 + self.speed * self.t - k * SPACING

    def active(self):
        for k, s in enumerate(self.state):
            if s == 'up':
                return k
        return None

    def rect(self, k):
        """(cx, cy, half-width, half-height) of tile k, as the frame's target fields."""
        return (self.lanes[k] + 0.5) / LANES, self.y(k), 0.5 / LANES - 0.012, TILE_H / 2

    def step(self, dt):
        """Advance dt seconds. Returns (events, click) where events are tile messages and click is True on a press."""
        self.t += dt
        ev, click = [], False
        k = self.active()
        # a tile whose top edge slides past the bottom: missed
        if k is not None and self.y(k) - TILE_H / 2 > 1.0:
            self.state[k] = 'miss'
            self.misses += 1
            ev.append(self._ev(k, 'miss'))
            self.press_end = None
            k = self.active()
        if k is None:
            if self.done_at is None:
                self.done_at = self.t
            return ev, click
        plan, lane_x = self.plan[k], (self.lanes[k] + 0.5) / LANES
        ty = self.y(k)
        if self.press_end is not None:
            if self.t >= self.press_end:              # the press lands: on the tile, or not
                self.press_end = None
                click = True
                self.presses += 1
                kk = self.press_tile
                cx, cy, hw, hh = self.rect(kk)
                on = abs(self.cursor[0] - cx) <= hw and abs(self.cursor[1] - self.y(kk)) <= hh
                if on and self.state[kk] == 'up':
                    self.state[kk] = 'hit'
                    self.hits += 1
                    ev.append(self._ev(kk, 'hit'))
                else:
                    ev.append(self._ev(kk, 'wrong', lane=min(LANES - 1, int(self.cursor[0] * LANES))))
            return ev, click                           # the head holds still during a press
        # aim: the head turns toward the lowest tile (a little below its centre, where it will be), with some lag
        if plan == 'miss':
            gx = ((self.lanes[k] + 2) % LANES + 0.5) / LANES      # distracted: looks at another lane
            gy = 0.45 + 0.1 * math.sin(self.t * 1.3)
        elif plan == 'wrong' and k not in self.wrong_done:
            gx = lane_x + (0.9 / LANES if self.lanes[k] < LANES - 1 else -0.9 / LANES)   # the neighbouring lane
            gy = min(0.9, max(0.1, ty + 0.08))
        else:
            gx, gy = lane_x, min(0.92, max(0.08, ty + 0.1 + self.speed * 0.28))   # leads the tile by the lag
        a = 1 - math.exp(-dt / 0.28)
        self.cursor[0] += (gx - self.cursor[0]) * a + self.rng.gauss(0, 0.002)
        self.cursor[1] += (gy - self.cursor[1]) * a + self.rng.gauss(0, 0.002)
        self.cursor = [min(1.0, max(0.0, v)) for v in self.cursor]
        if plan == 'miss':
            return ev, click
        want = self.press_at.setdefault(k, self.rng.uniform(0.15, 0.4))
        near = abs(self.cursor[0] - gx) < 0.03 and abs(self.cursor[1] - gy) < 0.1
        if near and ty >= want:
            self.press_end = self.t + self.rng.uniform(0.45, 0.9)
            self.press_tile = k
            if plan == 'wrong':
                self.wrong_done.add(k)
        return ev, click

    def _ev(self, k, result, lane=None):
        return {'type': 'tile', 'id': self.n * 1000 + k, 'lane': self.lanes[k] if lane is None else lane,
                'result': result, 'note_i': k, 'song': self.song['id']}

    def snapshot(self):
        tiles = []
        for k in range(len(self.notes)):
            y = self.y(k)
            if y - TILE_H / 2 > 1.05:
                continue
            if y + TILE_H / 2 < -0.05:
                break
            tiles.append([self.n * 1000 + k, self.lanes[k], round(y, 4), TILE_H, self.state[k]])
        a = self.active()
        return {'type': 'tiles', 't': round(self.t, 3), 'song': self.song['id'], 'speed': round(self.speed, 4),
                'lanes': LANES, 'cursor': [round(self.cursor[0], 4), round(self.cursor[1], 4)], 'tiles': tiles,
                'note_i': a if a is not None else len(self.notes)}

    def finished(self):
        return self.done_at is not None and self.t - self.done_at > 1.0


def frame(pose, t, episode, click, cursor, target):
    f = list(struct.unpack(f'<{FRAME_FLOATS}f', pose))
    f[0], f[1], f[2], f[4] = 7.0, t, float(episode), 1.0 if click else 0.0
    f[5], f[6] = cursor
    f[7:11] = target if target is not None else (-1.0, -1.0, -1.0, -1.0)
    f[11] = 0.0
    return struct.pack(f'<{FRAME_FLOATS}f', *f)


async def run(a):
    token = os.environ.get('LABRAT_PUBLISH_TOKEN', '').strip()
    if not token:
        raise SystemExit('set LABRAT_PUBLISH_TOKEN to the local relay\'s token')
    with open(os.path.join(SITE, 'assets', 'songs.json'), encoding='utf-8') as f:
        songs = json.load(f)
    if a.song:
        songs = [s for s in songs if s['id'] == a.song] or sys.exit(f'no song {a.song!r} in site/assets/songs.json')
    poses = load_poses()
    rng = random.Random(a.seed)
    hello = {'type': 'hello', 'source': 'training', 'task': 'tiles', 'run': 'tiles_demo',
             'label': 'TEST (not a live training run): a scripted Rat Tiles demo on the recorded replay poses',
             'fps': FPS, 'started': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'test': True}
    try:
        ws = await connect(a.relay, additional_headers={'Authorization': f'Bearer {token}'}, open_timeout=10,
                           compression=None)
    except InvalidStatus as e:
        raise SystemExit(f'the relay refused the connection (HTTP {e.response.status_code})')
    sent = {'frames': 0, 'tiles': 0, 'tile': 0}
    try:
        await ws.send(json.dumps(hello))
        speed, ep, steps, t_start = a.speed, 0, 0, time.monotonic()
        att = Attempt(ep, songs[0], speed, rng)
        k, next_snap, next_row, pose_i, pose_dir = 0, 0.0, 0.0, 40, 1
        t0 = time.monotonic()
        while not a.duration or time.monotonic() - t_start < a.duration:
            k += 1
            click = False
            for _ in range(2):                      # 2 control steps of 20 ms per frame, as the real publisher
                evs, c = att.step(0.02)
                click = click or c
                for e in evs:
                    await ws.send(json.dumps(e, separators=(',', ':')))
                    sent['tile'] += 1
            steps += 2
            act = att.active()
            tgt = None
            if act is not None:
                cx, cy, hw, hh = att.rect(act)
                if 0.0 <= cy <= 1.0:
                    tgt = (cx, cy, hw, hh)
            pose_i += pose_dir
            if pose_i >= len(poses) - 1 or pose_i <= 40:
                pose_dir = -pose_dir
            await ws.send(frame(poses[pose_i], att.t, ep, click, att.cursor, tgt))
            sent['frames'] += 1
            if att.t >= next_snap:
                next_snap = att.t + 1.0 / SNAP_HZ
                await ws.send(json.dumps(att.snapshot(), separators=(',', ':')))
                sent['tiles'] += 1
            if time.monotonic() - t_start >= next_row:
                next_row += 4.0
                await ws.send(json.dumps({'type': 'metrics', 'row': {
                    'steps': 50_000 + steps * 40, 'ret': round(2.0 + att.hits * 0.8 - att.misses * 0.3, 3),
                    'hits': att.hits, 'difficulty': round(att.speed, 3)}}))
            if att.finished():
                await ws.send(json.dumps({'type': 'episode', 'n': ep, 'presses': att.presses, 'hits': att.hits,
                                          'misses': att.misses, 'fell': False}))
                print(f'attempt {ep}: {att.song["id"]} at {att.speed:.3f} screens/s: {att.hits} hit, '
                      f'{att.misses} missed, {att.presses} presses', flush=True)
                ep += 1
                speed *= 1.08                        # a demo of the curriculum: a little faster each attempt
                att = Attempt(ep, songs[ep % len(songs)], speed, rng)
                next_snap = 0.0
            due = t0 + k / FPS
            d = due - time.monotonic()
            if d > 0:
                await asyncio.sleep(d)
            elif d < -1.0:
                t0 = time.monotonic() - k / FPS
            try:                                     # read (and ignore) whatever the relay says
                while True:
                    await asyncio.wait_for(ws.recv(), 0.0001)
            except (asyncio.TimeoutError, TimeoutError):
                pass
        await ws.send(json.dumps({'type': 'bye'}))
    except ConnectionClosed as e:
        code = e.rcvd.code if e.rcvd else None
        if code == 1008:
            raise SystemExit('the relay refused this TEST stream (close 1008): start the local relay with '
                             'RELAY_ALLOW_TEST=1 (never on the public relay)')
        raise SystemExit(f'the relay closed the connection ({code})')
    finally:
        try:
            await ws.close()
        except Exception:
            pass
        print(f'sent {sent["frames"]} frames, {sent["tiles"]} snapshots, {sent["tile"]} tile events', flush=True)


def main():
    ap = argparse.ArgumentParser(description='A scripted Rat Tiles TEST stream for a local relay (RELAY_ALLOW_TEST=1).')
    ap.add_argument('--relay', required=True, help='ws://localhost:<port>/publish')
    ap.add_argument('--song', help='one song id from site/assets/songs.json (default: each in turn)')
    ap.add_argument('--speed', type=float, default=0.16, help='screen heights per second at the start')
    ap.add_argument('--duration', type=float, default=0, help='seconds (0: until Ctrl+C)')
    ap.add_argument('--seed', type=int, default=7)
    a = ap.parse_args()
    host = a.relay.split('://', 1)[-1].split('/', 1)[0].rsplit(':', 1)[0].strip('[]')
    if host not in ('localhost', '127.0.0.1', '::1'):
        raise SystemExit('this demo is for a local relay only')
    try:
        asyncio.run(run(a))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
