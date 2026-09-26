"""Rat Maze demo publisher: a scripted TEST stream for a LOCAL relay, to try the website's Rat Maze panel without a
training run.

    PowerShell (the local relay started with RELAY_ALLOW_TEST=1 and SERVE_SITE=1, see relay/README.md):
    $env:LABRAT_PUBLISH_TOKEN = '<the local relay's token>'
    python relay/maze_demo.py --relay ws://localhost:4720/publish --duration 120
    # open http://localhost:4720/buyback/#maze

Everything it sends is marked as a test: the hello has "test": true and a label starting with TEST, so relay.py refuses
it (close 1008) unless the relay runs with RELAY_ALLOW_TEST=1, and the site shows it as TEST STREAM, never as live.
It is not the rat's brain: the rat's poses are the recorded launch replay (site/replay/session.bin) in a loop, and the
marker's path is scripted here (seeded, so a run is repeatable): it follows the shortest path to the cheese, takes a
wrong turn now and then, bumps into the dead end it finds and turns back, and some mazes run out of time. The mazes
are made as maze_env makes them (a seeded recursive backtracker, always solvable; the first ones small and open, then
bigger with dead ends). It speaks the relay's Rat Maze protocol exactly as live/publish_training.py does for a "maze"
run:
    {"type":"hello","source":"training","task":"maze",...}
    {"type":"maze","t","maze_id","w","h","walls","cell":[cx,cy],"pos":[x,y],"cheese":[gx,gy],"trail":[[cx,cy],...],
     "bumps","steps","dist"}                                          8 Hz; walls only in a maze's first snapshot
    {"type":"maze_end","maze_id","result":"escaped"|"timeout","steps","bumps","time_s"}   once per maze
    {"type":"episode","n","presses","hits","misses","fell"}          every EP_MAZES mazes: hits = escapes, misses = timeouts
    binary frames (live/labrat_frame.py layout) at 25 fps, the cheese's rect in the target fields, the marker as the cursor
walls: one hex char per cell, row-major, the bits N=8, E=4, S=2, W=1 set where that side is OPEN. pos is in cell units
(cell (cx,cy) spans x in [cx,cx+1), y in [cy,cy+1); row 0 at the top). It reads no .env and signs nothing.
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
from collections import deque
from datetime import datetime, timezone

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, '..', 'site')
FRAME_FLOATS, FRAME_BYTES, HEADER = 467, 1868, 12
FPS = 25
SNAP_HZ = 8
CTRL_DT = 0.02                 # one control step of the env; 2 per frame at 25 fps
EP_MAZES = 3                   # mazes per "episode" message
TRAIL = 40
N, E, S, W = 8, 4, 2, 1
DIRS = [(0, -1, N, S), (1, 0, E, W), (0, 1, S, N), (-1, 0, W, E)]   # dx, dy, this side's bit, the neighbour's bit back
# the curriculum this demo walks through: (w, h, extra openings), from small open mazes to 8x8 with dead ends
LEVELS = [(3, 3, 5), (3, 3, 2), (4, 4, 3), (4, 4, 1), (5, 5, 2), (5, 5, 0), (6, 6, 1), (7, 7, 0), (8, 8, 1), (8, 8, 0)]


def gen_maze(w, h, extra, rng):
    """A perfect maze by recursive backtracking (every cell reachable), plus `extra` walls knocked out (loops), as a
    list of open-side bits per cell (row-major)."""
    cells = [0] * (w * h)
    seen = [False] * (w * h)
    stack = [(0, 0)]
    seen[0] = True
    while stack:
        x, y = stack[-1]
        nb = [(dx, dy, b, back) for dx, dy, b, back in DIRS
              if 0 <= x + dx < w and 0 <= y + dy < h and not seen[(y + dy) * w + x + dx]]
        if not nb:
            stack.pop()
            continue
        dx, dy, b, back = rng.choice(nb)
        cells[y * w + x] |= b
        cells[(y + dy) * w + x + dx] |= back
        seen[(y + dy) * w + x + dx] = True
        stack.append((x + dx, y + dy))
    for _ in range(extra):
        for _try in range(20):
            x, y = rng.randrange(w), rng.randrange(h)
            dx, dy, b, back = rng.choice(DIRS)
            if 0 <= x + dx < w and 0 <= y + dy < h and not cells[y * w + x] & b:
                cells[y * w + x] |= b
                cells[(y + dy) * w + x + dx] |= back
                break
    return cells


def bfs(cells, w, h, goal):
    """Shortest-path distance (in cells) from every cell to `goal`; -1 where unreachable."""
    dist = [-1] * (w * h)
    gx, gy = goal
    dist[gy * w + gx] = 0
    q = deque([goal])
    while q:
        x, y = q.popleft()
        d = dist[y * w + x]
        for dx, dy, b, _back in DIRS:
            if cells[y * w + x] & b:
                nx, ny = x + dx, y + dy
                if dist[ny * w + nx] < 0:
                    dist[ny * w + nx] = d + 1
                    q.append((nx, ny))
    return dist


def walls_hex(cells):
    return ''.join('%x' % c for c in cells)


class Maze:
    """One scripted maze: the marker runs from (0,0) to the cheese (the cell farthest from the start)."""

    def __init__(self, maze_id, level, rng, lost_every=0):
        self.id, self.rng = maze_id, rng
        w, h, extra = LEVELS[min(level, len(LEVELS) - 1)]
        self.w, self.h = w, h
        self.cells = gen_maze(w, h, extra, rng)
        from_start = bfs(self.cells, w, h, (0, 0))
        far = max(range(w * h), key=lambda i: from_start[i])
        self.cheese = (far % w, far // w)
        self.dist = bfs(self.cells, w, h, self.cheese)
        self.path_len = self.dist[0]
        self.pos = [0.5, 0.5]
        self.cell = (0, 0)
        self.prev_cell = None
        self.target = None             # the cell centre the marker heads for
        self.speed = 1.5 + 0.08 * level
        self.t = 0.0
        self.steps = 0
        self.bumps = 0
        self.trail = [(0, 0)]
        self.limit_s = 6.0 + 1.1 * self.path_len
        self.lost = rng.random() < 0.12 or (lost_every > 0 and maze_id % lost_every == 0)   # it wanders and runs out of time
        self.stall_until = 0.0                   # sim time until which a bump holds the marker
        self.bump_dir = None
        self.done = None                          # 'escaped' | 'timeout'
        self.done_at = None

    def open_dirs(self, cx, cy):
        c = self.cells[cy * self.w + cx]
        return [(dx, dy, b) for dx, dy, b, _back in DIRS if c & b]

    def choose(self):
        """The next cell from the current one: the shortest way, or a wrong turn at a junction now and then."""
        cx, cy = self.cell
        opts = self.open_dirs(cx, cy)
        back = [(dx, dy, b) for dx, dy, b in opts if (cx + dx, cy + dy) == self.prev_cell]
        fwd = [o for o in opts if o not in back] or back
        best = min(fwd, key=lambda o: self.dist[(cy + o[1]) * self.w + cx + o[0]])
        if self.lost:
            pick = self.rng.choice(fwd)
        elif len(fwd) >= 2 and self.rng.random() < 0.28:
            pick = self.rng.choice([o for o in fwd if o != best])
        else:
            pick = best
        return pick

    def step(self, dt):
        """Advance dt seconds of sim time. Returns a list of messages to send (maze_end)."""
        self.t += dt
        self.steps += 1
        if self.done:
            return []
        if self.t >= self.limit_s:
            self.done, self.done_at = 'timeout', self.t
            return [self.end_msg()]
        if self.t < self.stall_until:              # pressed against a wall after a bump
            return []
        cx, cy = self.cell
        if self.target is None:
            if (cx, cy) == self.cheese:
                self.done, self.done_at = 'escaped', self.t
                return [self.end_msg()]
            opts = self.open_dirs(cx, cy)
            fwd = [o for o in opts if (cx + o[0], cy + o[1]) != self.prev_cell]
            if not fwd and self.bump_dir is None:  # a dead end: run into the far wall first, then turn back
                dx, dy = cx - self.prev_cell[0], cy - self.prev_cell[1]
                self.bump_dir = (dx, dy)
                self.target = (cx + dx * 0.42, cy + dy * 0.42, None)
            else:
                self.bump_dir = None
                dx, dy, _b = self.choose()
                self.target = (cx + dx, cy + dy, (cx + dx, cy + dy))
        tx, ty, tcell = self.target
        gx, gy = tx + 0.5, ty + 0.5
        vx, vy = gx - self.pos[0], gy - self.pos[1]
        d = math.hypot(vx, vy)
        move = self.speed * dt
        if d <= move:
            self.pos = [gx, gy]
            if tcell is None:                       # reached the wall: bump
                self.bumps += 1
                self.stall_until = self.t + 0.35
                self.target = None
                self.prev_cell = (cx + self.bump_dir[0], cy + self.bump_dir[1])   # so "back" is the only way on
                # (a cell outside the maze as prev: every real neighbour counts as forward)
            else:
                self.prev_cell, self.cell = self.cell, tcell
                self.target = None
                if self.trail[-1] != tcell:
                    self.trail.append(tcell)
                    del self.trail[:-TRAIL]
        else:
            self.pos = [self.pos[0] + vx / d * move, self.pos[1] + vy / d * move]
        return []

    def end_msg(self):
        return {'type': 'maze_end', 'maze_id': self.id, 'result': self.done, 'steps': self.steps, 'bumps': self.bumps,
                'time_s': round(self.t, 2)}

    def snapshot(self, with_walls):
        return {'type': 'maze', 't': round(self.t, 3), 'maze_id': self.id, 'w': self.w, 'h': self.h,
                'walls': walls_hex(self.cells) if with_walls else None, 'cell': list(self.cell),
                'pos': [round(self.pos[0], 3), round(self.pos[1], 3)], 'cheese': list(self.cheese),
                'trail': [list(c) for c in self.trail], 'bumps': self.bumps, 'steps': self.steps,
                'dist': self.dist[self.cell[1] * self.w + self.cell[0]]}

    def target_rect(self):
        """The cheese's rect in screen units (0..1), as the frame's target fields."""
        return ((self.cheese[0] + 0.5) / self.w, (self.cheese[1] + 0.5) / self.h, 0.5 / self.w, 0.5 / self.h)

    def cursor(self):
        return (self.pos[0] / self.w, self.pos[1] / self.h)

    def finished(self):
        return self.done is not None and self.t - self.done_at > 1.6   # the reset to the standing pose between mazes


def load_poses():
    with open(os.path.join(SITE, 'replay', 'session.bin'), 'rb') as f:
        blob = f.read()
    n = len(blob) // FRAME_BYTES
    return [blob[i * FRAME_BYTES:(i + 1) * FRAME_BYTES] for i in range(n)]


def frame(pose, t, episode, cursor, target):
    f = list(struct.unpack(f'<{FRAME_FLOATS}f', pose))
    f[0], f[1], f[2], f[4] = 7.0, t, float(episode), 0.0
    f[5], f[6] = cursor
    f[7:11] = target if target is not None else (-1.0, -1.0, -1.0, -1.0)
    f[11] = 0.0
    return struct.pack(f'<{FRAME_FLOATS}f', *f)


async def run(a):
    token = os.environ.get('LABRAT_PUBLISH_TOKEN', '').strip()
    if not token:
        raise SystemExit('set LABRAT_PUBLISH_TOKEN to the local relay\'s token')
    poses = load_poses()
    rng = random.Random(a.seed)
    hello = {'type': 'hello', 'source': 'training', 'task': 'maze', 'run': 'maze_demo',
             'label': 'TEST (not a live training run): a scripted Rat Maze demo on the recorded replay poses',
             'fps': FPS, 'started': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'test': True}
    try:
        ws = await connect(a.relay, additional_headers={'Authorization': f'Bearer {token}'}, open_timeout=10,
                           compression=None)
    except InvalidStatus as e:
        raise SystemExit(f'the relay refused the connection (HTTP {e.response.status_code})')
    sent = {'frames': 0, 'maze': 0, 'maze_end': 0}
    try:
        await ws.send(json.dumps(hello))
        level, maze_id, ep, ep_hits, ep_miss, ep_mazes, steps = a.level, 1, 0, 0, 0, 0, 0
        escapes = 0
        maze = Maze(maze_id, level, rng, a.lost)
        walls_due = True
        k, next_snap, next_row, pose_i, pose_dir = 0, 0.0, 0.0, 40, 1
        t_start = time.monotonic()
        t0 = time.monotonic()
        sim_t = 0.0
        while not a.duration or time.monotonic() - t_start < a.duration:
            k += 1
            for _ in range(2):                      # 2 control steps of 20 ms per frame, as the real publisher
                for m in maze.step(CTRL_DT):
                    await ws.send(json.dumps(m, separators=(',', ':')))
                    sent['maze_end'] += 1
                sim_t += CTRL_DT
            steps += 2
            pose_i += pose_dir
            if pose_i >= len(poses) - 1 or pose_i <= 40:
                pose_dir = -pose_dir
            await ws.send(frame(poses[pose_i], sim_t, ep, maze.cursor(), maze.target_rect() if not maze.done else None))
            sent['frames'] += 1
            if not maze.done and sim_t >= next_snap:
                next_snap = sim_t + 1.0 / SNAP_HZ
                snap = maze.snapshot(walls_due)
                walls_due = False
                await ws.send(json.dumps(snap, separators=(',', ':')))
                sent['maze'] += 1
            if time.monotonic() - t_start >= next_row:
                next_row += 4.0
                await ws.send(json.dumps({'type': 'metrics', 'row': {
                    'steps': 50_000 + steps * 40, 'ret': round(1.0 + escapes * 0.9 - maze.bumps * 0.1, 3),
                    'hits': ep_hits, 'difficulty': round(level / (len(LEVELS) - 1), 3)}}))
            if maze.finished():
                print(f'maze {maze.id} ({maze.w}x{maze.h}, path {maze.path_len}): {maze.done} after {maze.steps} steps, '
                      f'{maze.bumps} bumps', flush=True)
                ep_mazes += 1
                if maze.done == 'escaped':
                    ep_hits += 1
                    escapes += 1
                    if escapes % 2 == 0:
                        level = min(level + 1, len(LEVELS) - 1)      # a demo of the curriculum
                else:
                    ep_miss += 1
                if ep_mazes >= EP_MAZES:
                    await ws.send(json.dumps({'type': 'episode', 'n': ep, 'presses': 0, 'hits': ep_hits,
                                              'misses': ep_miss, 'fell': False}))
                    ep += 1
                    ep_hits = ep_miss = ep_mazes = 0
                maze_id += 1
                maze = Maze(maze_id, level, rng, a.lost)
                walls_due = True
                next_snap = sim_t                  # the new maze's layout goes out on the next frame
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
        print(f'sent {sent["frames"]} frames, {sent["maze"]} snapshots, {sent["maze_end"]} maze_end events', flush=True)


def main():
    ap = argparse.ArgumentParser(description='A scripted Rat Maze TEST stream for a local relay (RELAY_ALLOW_TEST=1).')
    ap.add_argument('--relay', required=True, help='ws://localhost:<port>/publish')
    ap.add_argument('--level', type=int, default=0, help=f'the curriculum level to start at (0..{len(LEVELS) - 1})')
    ap.add_argument('--duration', type=float, default=0, help='seconds (0: until Ctrl+C)')
    ap.add_argument('--lost', type=int, default=0, help='every Nth maze runs out of time (0: only the random 12%%)')
    ap.add_argument('--seed', type=int, default=7)
    a = ap.parse_args()
    host = a.relay.split('://', 1)[-1].split('/', 1)[0].rsplit(':', 1)[0].strip('[]')
    if host not in ('localhost', '127.0.0.1', '::1'):
        raise SystemExit('this demo is for a local relay only')
    a.level = max(0, min(len(LEVELS) - 1, a.level))
    try:
        asyncio.run(run(a))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
