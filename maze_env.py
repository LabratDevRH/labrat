"""RAT MAZE: our own maze-escape game, played by the two-network rat brain (steer_env.SteerEnv).

Game rule v1 (RULES = 1):

  screen     the cursor task's virtual screen ([0,1]^2, x right, y down) shows a TOP-DOWN MAZE: a square grid of
             grid x grid cells (3..8, see level()) with walls on the cell edges, filling the screen. The rat is a MARKER
             inside it; the CHEESE sits in an exit cell on the border.
  marker     moves the way the cursor already moves: the head's direction relative to the body is its velocity
             (cursor_env.CursorEnv's head -> cursor mapping, unchanged: dead zone, 1.5-power gain), SPEED cells per
             second per screen unit per second of cursor speed. No lever press is needed to move, and a press has no
             role in the maze: the steering network's PRESS output is ignored (the lever-press network is not called).
  walls      stop the marker: the part of its motion into a closed edge is dropped (it slides along the wall). Pushing
             against a wall costs R_PUSH per step; each new BUMP (pushing after moving freely, at BUMP_MIN cells/s or
             more) costs R_BUMP and is counted.
  cheese     entering the cheese cell is an ESCAPE (+R_ESCAPE) and ends the maze. Training plays ONE maze per
             episode; the live view (mazes_per_episode = LIVE_MAZES) a course: after every escape a new, harder maze
             appears (LIVE_STEP of curriculum difficulty more). A maze not escaped within its time limit, T_BASE plus
             T_PER_CELL per cell of the shortest path (at most T_MAX), is a TIMEOUT (R_TIMEOUT) and ends the episode.
  dead ends  a cell with one open side (other than the cheese's) is a dead end: entering it costs R_DEADEND. Entering a
             cell again that the rat was in within the last TRAIL cells costs R_REVISIT.
  senses     the network NEVER sees the map. Through the cursor task's cue it gets the cheese's direction and a ROUGH
             distance (to half a cell), the cheese cell's size, the marker's velocity and the time left in the maze;
             its "on target" flag is always off (no press is ever right). MAZE_DIM features are appended at the END of
             its observation (train.py --resume widens a checkpoint for them): which of the 4 sides of its cell are
             open (N, E, S, W), where it is within the cell, its velocity in cells, the cheese's direction and rough
             distance in cells, the grid size, a short memory (how recently each open neighbour cell was visited,
             within the last TRAIL cells, and whether this cell was visited before), whether it is pushing a wall, and
             whether the cheese is in this cell or in an open neighbour.
  falls      the cursor task's fall rule (head or trunk on the floor, too low, tipped over) costs its -20 plus
             R_FALL_EXTRA, and the rat is put back in its standing start pose (steer_env.SteerEnv.new_trial, what the
             live rig does between steps); the maze goes on. Between the mazes of a course the rat is put back the same
             way. The steering network's head-DOWN commands are cut while the head is pitched down past PITCH_GUARD
             (the Rat Tiles guard: most falls in the cursor tasks were the head driven into the floor).
  curriculum difficulty 0..1 (train.py --curriculum; the cursor task's `difficulty` attribute), fixed per episode at
             its start, see level(): from a 3x3 open room (every interior wall removed) to an 8x8 perfect maze full of
             dead ends. Mazes are generated procedurally by a seeded recursive backtracker (a spanning tree of the
             grid, so every cell is reachable: always solvable) with a fraction `open` of the remaining interior walls
             removed (loops, fewer dead ends). The start is a random cell; the cheese a far border cell.
  episode    training: one maze. Live view: a course of LIVE_MAZES mazes, ending at the first timeout or after the
             last escape. Its "hits" are the mazes escaped and its "timeouts" the mazes timed out (train.py's log rows,
             the episode messages of live/publish_training.py: hits = escapes, misses = timeouts).

Reward (chosen, disclosed): the cursor task's body terms (posture, height, energy, smoothness, -20 for a fall; its
cursor-progress terms are inert here: the cursor is always "at distance 0" of its target and never "on" it, and the
small hold-still bonus that leaves is subtracted again), plus R_ESCAPE at the cheese, R_TIMEOUT at the time limit,
potential-based shaping R_BFS x the decrease of PHI, where PHI = the BFS distance (cells) of the marker's cell to the
cheese + the marker's distance to the centre of the next cell on the shortest path (a single function of position, so
no loop can farm it), R_STEP per control step, R_PUSH per step pushing a wall and R_BUMP per bump, R_DEADEND per dead
end entered, R_REVISIT per cell re-entered within the memory, and R_FALL_EXTRA per fall.

Nothing here changes env.py, cursor_env.py or steer_env.py (their bytes are in the Labrat brain commit): MazeCursorEnv
subclasses CursorEnv, and MazeEnv subclasses SteerEnv with a MazeCursorEnv inside.

    python maze_env.py --selftest
    python maze_env.py --eval runs/final/steer.pt --difficulty 0.0 --episodes 24      (mean actions, no noise)
"""
import collections
import json
import os

import numpy as np

from env import CTRL_DT
from cursor_env import CursorEnv, TARGET_TIME
from steer_env import SteerEnv, PRESS_NET, NECK, NECK_LIMIT, STEER_ACT
from ptload import load, NumpyPolicy

HERE = os.path.dirname(os.path.abspath(__file__))

RULES = 1                 # the game rule version; train.py saves it in maze checkpoints (a resume of another version
                          # starts the curriculum over)
MIN_GRID, MAX_GRID = 3, 8
SPEED = 2.5               # marker cells per second per (screen unit per second) of cursor speed (measured with the
                          # final steering network: at 5.0 it crossed a 3x3 room in 0.3 s, a cell every 5 control
                          # steps, too twitchy for an 8x8 corridor)
MAX_CELL_STEP = 0.9       # cells the marker may move per axis per control step (never reached; keeps one wall test)
BUMP_MIN = 0.25           # cells/s into a wall that counts as a bump (slower pushes only pay R_PUSH)
TRAIL = 40                # cells of memory: the observation's recency and the "maze" message's trail
LIVE_MAZES = 4            # the live view: a course of this many mazes per episode (live/buyback.py's per-attempt cap)
LIVE_STEP = 0.05          # each maze of a course is this much curriculum difficulty harder than the one before
T_BASE = 5.0              # s: a maze's time limit is T_BASE + T_PER_CELL x the shortest path's cells, at most T_MAX
T_PER_CELL = 1.25
T_MAX = 60.0
MAX_EP_S = 300.0          # safety cap on one episode (s)
R_ESCAPE = 100.0          # the cheese
R_TIMEOUT = -10.0         # the time limit
R_BFS = 6.0               # per cell of PHI (see the module docstring), potential-based
R_STEP = -0.1             # per control step (the body terms pay up to +0.18 a step for standing well: this keeps the
                          # net cost of a step negative, so dawdling is never free)
R_PUSH = -0.05            # per step pushing against a wall
R_BUMP = -1.0             # per bump (pushing a wall after moving freely, at BUMP_MIN cells/s or more)
R_DEADEND = -3.0          # entering a dead end
R_REVISIT = -0.5          # entering a cell visited within the last TRAIL cells
R_FALL_EXTRA = -20.0      # a fall, on top of the cursor task's -20 (the rat is set back on its feet; the maze goes on)
PITCH_GUARD = 0.35        # rad of head-down pitch (cursor_env's deflection) past which head-down commands are cut
MAZE_DIM = 19             # features appended to the steering observation
STEER_DIM = 21            # the steering network's own observation (steer_env.SteerEnv._obs)
OBS_DIM = STEER_DIM + MAZE_DIM
T_CLAMP = int(round(TARGET_TIME / CTRL_DT)) - 2   # the cursor task's 8 s target timeout never fires in a maze
STILL_V = 0.05            # cursor_env's hold-still bonus: 0.03 while |cursor_v| < 0.05 (subtracted again here)
STILL_R = 0.03
N, E, S, W = 0, 1, 2, 3   # sides of a cell (screen y down: N is up on the screen, S down)
DX = (0, 1, 0, -1)
DY = (-1, 0, 1, 0)
OPP = (S, W, N, E)
EDGE = 1e-6               # a blocked marker stops this far inside its cell


def level(difficulty):
    """The game at a curriculum difficulty in [0, 1]: the grid's side and the fraction of the perfect maze's remaining
    interior walls that are removed (1: an open room; 0: a perfect maze, one path between any two cells)."""
    d = float(np.clip(difficulty, 0.0, 1.0))
    grid = int(np.floor(MIN_GRID + (MAX_GRID - MIN_GRID) * d + 0.5 + 1e-9))
    return {'grid': int(np.clip(grid, MIN_GRID, MAX_GRID)), 'open': float(1.0 - d)}


# ---------------------------------------------------------------------------------------------------- the maze
class Maze:
    """A w x h grid maze. open[y, x, side] says whether that side of cell (x, y) is open (row 0 at the top). Pure."""

    def __init__(self, w, h, open_frac, rng):
        self.w, self.h = int(w), int(h)
        self.open = np.zeros((self.h, self.w, 4), bool)
        self._backtracker(rng)
        self._braid(float(open_frac), rng)
        # the start: a random cell; the cheese: a far border cell (at least 3/4 of the farthest border cell's path)
        sx, sy = int(rng.integers(self.w)), int(rng.integers(self.h))
        self.start = (sx, sy)
        d0 = self.bfs(sx, sy)
        border = [(x, y) for y in range(self.h) for x in range(self.w)
                  if (x in (0, self.w - 1) or y in (0, self.h - 1)) and (x, y) != (sx, sy)]
        far = max(d0[y, x] for x, y in border)
        cands = [c for c in border if d0[c[1], c[0]] >= 0.75 * far]
        self.cheese = cands[int(rng.integers(len(cands)))]
        self.cheese_centre = np.array(self.cheese, float) + 0.5
        self.dist = self.bfs(*self.cheese)                  # BFS distance of every cell to the cheese
        self.nxt = np.full((self.h, self.w), -1, int)       # the side toward the next cell on the shortest path
        for y in range(self.h):
            for x in range(self.w):
                if (x, y) == self.cheese:
                    continue
                best = None
                for d in range(4):
                    if self.open[y, x, d] and self.dist[y + DY[d], x + DX[d]] == self.dist[y, x] - 1:
                        best = d
                        break
                assert best is not None, 'unreachable cell'      # the backtracker leaves none
                self.nxt[y, x] = best
        self.dead = (self.open.sum(2) == 1)
        self.dead[self.cheese[1], self.cheese[0]] = False

    @property
    def n(self):
        return max(self.w, self.h)

    def _backtracker(self, rng):
        """Recursive backtracker (iterative): a random spanning tree, every cell reachable."""
        w, h = self.w, self.h
        seen = np.zeros((h, w), bool)
        x, y = int(rng.integers(w)), int(rng.integers(h))
        seen[y, x] = True
        stack = [(x, y)]
        while stack:
            x, y = stack[-1]
            nb = [d for d in range(4)
                  if 0 <= x + DX[d] < w and 0 <= y + DY[d] < h and not seen[y + DY[d], x + DX[d]]]
            if not nb:
                stack.pop()
                continue
            d = nb[int(rng.integers(len(nb)))]
            nx, ny = x + DX[d], y + DY[d]
            self.open[y, x, d] = True
            self.open[ny, nx, OPP[d]] = True
            seen[ny, nx] = True
            stack.append((nx, ny))

    def _braid(self, frac, rng):
        """Remove a fraction of the remaining interior walls (loops, fewer dead ends; 1.0 = an open room)."""
        walls = [(x, y, d) for y in range(self.h) for x in range(self.w) for d in (E, S)
                 if x + DX[d] < self.w and y + DY[d] < self.h and not self.open[y, x, d]]
        k = int(round(np.clip(frac, 0.0, 1.0) * len(walls)))
        for i in rng.permutation(len(walls))[:k]:
            x, y, d = walls[i]
            self.open[y, x, d] = True
            self.open[y + DY[d], x + DX[d], OPP[d]] = True

    def bfs(self, sx, sy):
        dist = np.full((self.h, self.w), -1, int)
        dist[sy, sx] = 0
        q = collections.deque([(sx, sy)])
        while q:
            x, y = q.popleft()
            for d in range(4):
                if self.open[y, x, d]:
                    nx, ny = x + DX[d], y + DY[d]
                    if dist[ny, nx] < 0:
                        dist[ny, nx] = dist[y, x] + 1
                        q.append((nx, ny))
        return dist

    def hex(self):
        """One hex char per cell, row-major: the bits N=8, E=4, S=2, W=1 set where that side is open."""
        out = []
        for y in range(self.h):
            for x in range(self.w):
                o = self.open[y, x]
                out.append('%x' % ((8 if o[N] else 0) | (4 if o[E] else 0) | (2 if o[S] else 0) | (1 if o[W] else 0)))
        return ''.join(out)

    def next_centre(self, cell):
        """Centre of the next cell on the shortest path from `cell` (the cheese's own centre in the cheese cell)."""
        x, y = cell
        d = self.nxt[y, x]
        if d < 0:
            return self.cheese_centre
        return np.array([x + DX[d] + 0.5, y + DY[d] + 0.5])

    def phi(self, pos, cell):
        """The shaping potential: cells of BFS distance to the cheese + the distance to the next cell's centre."""
        x, y = cell
        return float(self.dist[y, x]) + float(np.linalg.norm(np.asarray(pos, float) - self.next_centre(cell)))

    def move(self, pos, step):
        """Move the marker by `step` (cells, per axis at most MAX_CELL_STEP), x first then y, stopped by closed edges.
        Returns (new pos, blocked_x, blocked_y)."""
        x, y = float(pos[0]), float(pos[1])
        sx, sy = (float(np.clip(v, -MAX_CELL_STEP, MAX_CELL_STEP)) for v in step)
        cx, cy = int(np.floor(x)), int(np.floor(y))
        bx = by = False
        nx = x + sx
        if nx >= cx + 1:
            if not self.open[cy, cx, E]:
                nx, bx = cx + 1 - EDGE, True
        elif nx < cx:
            if not self.open[cy, cx, W]:
                nx, bx = float(cx), True
        x = nx
        cx = int(np.floor(x))
        ny = y + sy
        if ny >= cy + 1:
            if not self.open[cy, cx, S]:
                ny, by = cy + 1 - EDGE, True
        elif ny < cy:
            if not self.open[cy, cx, N]:
                ny, by = float(cy), True
        return np.array([x, ny]), bx, by


# ---------------------------------------------------------------------------------------------------- inner env
class MazeCursorEnv(CursorEnv):
    """CursorEnv whose cursor is the marker in the maze (the physics, head -> velocity mapping and fall rule are
    CursorEnv's, unchanged). Its lit target is the cheese cell; no click ever hits it."""

    def __init__(self, seed=0, record=False, randomize=False):
        self.mazes_per_episode = 1      # the live view sets LIVE_MAZES (a course)
        self.maze = None
        self.maze_id = -1
        self.next_maze_id = 0
        self.events = []                # maze_end events since the last drain_events()
        self.falls = 0
        self._new_episode = False
        self.tc = np.array([0.5, 0.5])
        self.th = np.array([0.1, 0.1])
        super().__init__(seed, record, randomize)

    # ---- episode
    def reset(self, seed=None, randomize=None, cursor=None):
        self.external_target = 'maze'   # CursorEnv: no random targets or holds; a hit never moves on by itself
        self._new_episode = True
        return super().reset(seed=seed, randomize=randomize, cursor=cursor)

    def _new_target(self):
        """CursorEnv calls this at reset: start the episode's first maze at the curriculum difficulty."""
        if self._new_episode or self.maze is None:
            self._new_episode = False
            self.n_in_episode = 0
            self.falls = 0
            self.escape_s = []          # seconds to the cheese of every escape this episode
            self.bumps_ep = self.deadends_ep = self.revisits_ep = 0
            self.level = float(np.clip(self.difficulty, 0.0, 1.0))
            self._start_maze(self.level)

    def _start_maze(self, difficulty):
        lv = level(difficulty)
        self.maze = Maze(lv['grid'], lv['grid'], lv['open'], self.rng)
        self.maze_difficulty = float(np.clip(difficulty, 0.0, 1.0))
        self.maze_id = self.next_maze_id
        self.next_maze_id += 1
        mz = self.maze
        self.cell = mz.start
        self.pos = np.array(mz.start, float) + 0.5
        self.trail = collections.deque([self.cell], maxlen=TRAIL)
        self.v_cells = np.zeros(2)
        self.pushing = False
        self.t_maze = 0
        self.bumps = self.deadends = self.revisits = 0
        self.dist0 = int(mz.dist[mz.start[1], mz.start[0]])
        self.limit = int(round(min(T_MAX, T_BASE + T_PER_CELL * self.dist0) / CTRL_DT))
        self.phi = mz.phi(self.pos, self.cell)
        self.cursor = self.screen_of(self.pos)
        self.cursor_v[:] = 0.0
        c = self.cell_size()
        self.set_target(self.screen_of(mz.cheese_centre), np.array([c / 2, c / 2]))   # the lit target: the cheese

    # ---- geometry
    def cell_size(self):
        return 1.0 / self.maze.n

    def screen_of(self, pos):
        """Cell coordinates -> the screen ([0,1]^2): square cells, the grid centred."""
        c = self.cell_size()
        off = np.array([(1.0 - self.maze.w * c) / 2, (1.0 - self.maze.h * c) / 2])
        return np.asarray(pos, float) * c + off

    def cheese_rect(self):
        """(cx, cy, hw, hh) of the cheese cell on the screen (the frame's lit target)."""
        c = self.cell_size()
        sc = self.screen_of(self.maze.cheese_centre)
        return (float(sc[0]), float(sc[1]), c / 2, c / 2)

    def rough_dist(self):
        """Distance to the cheese's centre in cells, to half a cell (what the rat smells)."""
        return float(np.round(2.0 * np.linalg.norm(self.maze.cheese_centre - self.pos)) / 2.0)

    def recency(self, cell):
        """1 for the cell left a moment ago, down to 0 for one not among the last TRAIL cells."""
        tr = self.trail
        for i in range(len(tr) - 2, -1, -1):          # the last entry is the current cell
            if tr[i] == cell:
                return 1.0 - (len(tr) - 1 - i) / TRAIL
        return 0.0

    def cheese_near(self):
        mz = self.maze
        x, y = self.cell
        if (x, y) == mz.cheese:
            return True
        return any(mz.open[y, x, d] and (x + DX[d], y + DY[d]) == mz.cheese for d in range(4))

    # ---- the cursor task's hooks
    def _cursor_dist(self):
        """The cursor is never "away" from its target: CursorEnv's progress and aim terms stay at zero."""
        return 0.0

    def on_target(self):
        """No press is ever right in the maze: a click is a wrong click (-3), never a hit."""
        return False

    def _cue(self):
        c = super()._cue()
        if self.maze is None or not hasattr(self, 'pos'):
            return c
        mz = self.maze
        delta = mz.cheese_centre - self.pos
        n = float(np.linalg.norm(delta))
        cs = self.cell_size()
        rel = delta / n * self.rough_dist() * cs if n > 1e-9 else np.zeros(2)     # screen units, rough distance
        c[0:2] = rel * 3.0
        c[2:4] = (cs / 2) * 10.0
        c[4:6] = self.v_cells * cs                       # the marker's velocity in screen units per second
        c[8] = 0.0
        c[9] = min(self.t_maze / max(self.limit, 1), 1.0)
        c[10] = 0.0
        return c

    def maze_obs(self):
        """The MAZE_DIM features appended to the steering observation (see "senses" above)."""
        f = np.zeros(MAZE_DIM, np.float32)
        mz = self.maze
        if mz is None or not hasattr(self, 'pos'):
            return f
        x, y = self.cell
        f[0:4] = mz.open[y, x]
        f[4] = (self.pos[0] - x - 0.5) * 2.0
        f[5] = (self.pos[1] - y - 0.5) * 2.0
        f[6:8] = np.clip(self.v_cells / 4.0, -1.5, 1.5)
        delta = mz.cheese_centre - self.pos
        n = float(np.linalg.norm(delta))
        if n > 1e-9:
            f[8:10] = delta / n
        f[10] = self.rough_dist() / MAX_GRID
        f[11] = mz.n / MAX_GRID
        for d in range(4):
            if mz.open[y, x, d]:
                f[12 + d] = self.recency((x + DX[d], y + DY[d]))
        earlier = sum(1 for c in list(self.trail)[:-1] if c == self.cell)
        f[16] = min(earlier, 3) / 3.0
        f[17] = float(self.pushing)
        f[18] = float(self.cheese_near())
        return f

    # ---- one control step
    def _event(self, result):
        self.events.append({'type': 'maze_end', 'maze_id': int(self.maze_id), 'result': result,
                            'steps': int(self.t_maze), 'bumps': int(self.bumps),
                            'time_s': round(self.t_maze * CTRL_DT, 2)})

    def drain_events(self):
        ev, self.events = self.events, []
        return ev

    def step(self, action):
        mz = self.maze
        self.t_target = min(self.t_target, T_CLAMP)
        obs, r, _done, info = super().step(action)
        # CursorEnv's cursor reward here is only its hold-still bonus (the cursor is always at distance 0): take it back
        r -= STILL_R * float(np.linalg.norm(self.cursor_v) < STILL_V)
        r_maze = R_STEP
        # the marker: the cursor's velocity (screen units/s) drives it at SPEED cells/s per unit, stopped by walls
        pos0 = self.pos
        v = self.cursor_v * SPEED
        self.pos, bx, by = mz.move(pos0, v * CTRL_DT)
        self.v_cells = (self.pos - pos0) / CTRL_DT
        self.cursor = self.screen_of(self.pos)
        self.t_maze += 1
        pushing = bx or by
        if pushing:
            r_maze += R_PUSH
            speed = max(abs(v[0]) if bx else 0.0, abs(v[1]) if by else 0.0)
            if not self.pushing and speed >= BUMP_MIN:
                self.bumps += 1
                self.bumps_ep += 1
                r_maze += R_BUMP
        self.pushing = pushing
        cell = (int(np.floor(self.pos[0])), int(np.floor(self.pos[1])))
        if cell != self.cell:
            self.cell = cell
            if cell in self.trail:
                self.revisits += 1
                self.revisits_ep += 1
                r_maze += R_REVISIT
            self.trail.append(cell)
            if mz.dead[cell[1], cell[0]]:
                self.deadends += 1
                self.deadends_ep += 1
                r_maze += R_DEADEND
        phi = mz.phi(self.pos, self.cell)
        r_maze += R_BFS * (self.phi - phi)
        self.phi = phi
        result = None
        if self.cell == mz.cheese:
            result = 'escaped'
            r_maze += R_ESCAPE
            self.hits += 1
            self.escape_s.append(self.t_maze * CTRL_DT)
        elif self.t_maze >= self.limit:
            result = 'timeout'
            r_maze += R_TIMEOUT
            self.timeouts += 1
        fell_now = bool(info['fell'])             # MazeEnv puts the rat back on its feet; the maze goes on
        self.falls += fell_now
        done = False
        if result is not None:
            self._event(result)
            self.n_in_episode += 1
            done = result == 'timeout' or self.n_in_episode >= self.mazes_per_episode
            if not done:
                self._start_maze(self.maze_difficulty + LIVE_STEP)   # a new, harder maze
        if self.t * CTRL_DT >= MAX_EP_S:
            done = True
        info['hits'], info['misses'], info['timeouts'] = self.hits, self.misses, self.timeouts   # after this step
        info['maze_end'] = result
        info['fell_now'] = fell_now
        info['falls'] = self.falls
        info['fell'] = self.falls > 0 if done else fell_now     # at the end: fell at least once this episode
        info['level'] = self.level
        info['bumps'] = self.bumps_ep
        info['deadends'] = self.deadends_ep
        info['revisits'] = self.revisits_ep
        info['dist'] = int(mz.dist[self.cell[1], self.cell[0]]) if result is None else 0
        if done:
            info['cheese_s'] = float(np.mean(self.escape_s)) if self.escape_s else float('nan')
        return obs, r + r_maze, done, info

    # ---- for the live view
    def snapshot(self, sim_t, walls):
        """The {"type":"maze"} message: the maze now (walls only when asked: the first snapshot of a maze)."""
        mz = self.maze
        # pos to 3 decimals, kept inside its cell (a marker stopped against a wall sits EDGE inside the cell)
        pos = [min(round(float(p), 3), c + 0.999) for p, c in zip(self.pos, self.cell)]
        return {'type': 'maze', 't': round(float(sim_t), 3), 'maze_id': int(self.maze_id), 'w': int(mz.w),
                'h': int(mz.h), 'walls': mz.hex() if walls else None, 'cell': [int(self.cell[0]), int(self.cell[1])],
                'pos': pos,
                'cheese': [int(mz.cheese[0]), int(mz.cheese[1])],
                'trail': [[int(c[0]), int(c[1])] for c in self.trail], 'bumps': int(self.bumps),
                'steps': int(self.t_maze), 'dist': int(mz.dist[self.cell[1], self.cell[0]])}


# ---------------------------------------------------------------------------------------------------- the brain's env
class MazeEnv(SteerEnv):
    """SteerEnv (the steering network; the lever-press network is loaded but idle) in the Rat Maze. The observation is
    SteerEnv's 21 features followed by MAZE_DIM maze features."""

    def __init__(self, seed=0, record=False, randomize=False, press_net=PRESS_NET, mazes_per_episode=1):
        # SteerEnv.__init__, with the maze as its cursor env (steer_env.py itself is not changed)
        self.e = MazeCursorEnv(seed, record, randomize)
        self.e.mazes_per_episode = int(mazes_per_episode)
        self.m, self.d = self.e.m, self.e.d
        self.neck = [i for i in range(self.m.nu) if self.m.actuator(i).name in NECK]
        self.neck_limit = np.array([NECK_LIMIT if self.m.actuator(i).name in ('cervical_extend', 'atlas') else 1.0
                                    for i in self.neck])
        self.press_pol = NumpyPolicy(load(press_net), 200)
        names = [self.m.actuator(i).name for i in self.neck]
        self.i_extend, self.i_atlas = names.index('cervical_extend'), names.index('atlas')   # in the action vector
        self.program, self._clicked_prog = 0, False
        self.nu = STEER_ACT
        self.obs_dim = len(self.reset())
        assert self.obs_dim == OBS_DIM, self.obs_dim

    @property
    def mazes_per_episode(self):
        return self.e.mazes_per_episode

    @mazes_per_episode.setter
    def mazes_per_episode(self, v):
        self.e.mazes_per_episode = int(v)

    def _obs(self):
        return np.concatenate([super()._obs(), self.e.maze_obs()]).astype(np.float32)

    def step(self, action):
        e = self.e
        action = np.array(action, dtype=np.float64)
        action[4] = -1.0                                     # a press has no role in the maze (see "marker" above)
        if e._deflection()[1] > PITCH_GUARD:                 # head-down guard (PITCH_GUARD): no head on the floor
            action[self.i_extend] = min(action[self.i_extend], 0.0)
            action[self.i_atlas] = max(action[self.i_atlas], 0.0)
        obs, r, done, info = super().step(action)
        if info.get('fell_now'):
            r += R_FALL_EXTRA
        info['new_trial'] = bool((info.get('fell_now') or info.get('maze_end')) and not done)
        if info['new_trial']:
            self.new_trial()         # back in its standing start pose, as the live rig does between steps
            obs = self._obs()
        return obs, r, done, info

    def drain_events(self):
        return self.e.drain_events()


# ---------------------------------------------------------------------------------------------------- checks / eval
def _selftest():
    rng = np.random.default_rng(7)
    lv0, lv1 = level(0.0), level(1.0)
    assert lv0 == {'grid': 3, 'open': 1.0} and lv1['grid'] == 8 and lv1['open'] == 0.0, (lv0, lv1)
    grids = [level(d)['grid'] for d in np.arange(0.0, 1.0001, 0.05)]
    assert grids == sorted(grids) and set(grids) == set(range(3, 9)), grids
    assert [level(d)['grid'] for d in (0.05, 0.1, 0.3, 0.5, 0.7, 0.9)] == [3, 4, 5, 6, 7, 8]
    # an open room: no interior wall, no dead end; a perfect maze: a spanning tree (n*n - 1 open edges), every cell
    # reachable, dead ends, the cheese on the border, the hex string consistent with its neighbours
    room = Maze(3, 3, 1.0, rng)
    assert room.open[1, 1].all() and not room.dead.any() and room.dist.max() <= 4 and len(room.hex()) == 9
    for _ in range(20):
        mz = Maze(8, 8, 0.0, rng)
        assert mz.open.sum() == 2 * (64 - 1) and (mz.dist >= 0).all() and mz.dead.sum() >= 2
        gx, gy = mz.cheese
        assert gx in (0, 7) or gy in (0, 7)
        assert mz.dist[mz.start[1], mz.start[0]] > 0 and mz.dist[gy, gx] == 0
        hx = mz.hex()
        assert len(hx) == 64
        for y in range(8):
            for x in range(8):
                v = int(hx[y * 8 + x], 16)
                assert [bool(v & 8), bool(v & 4), bool(v & 2), bool(v & 1)] == list(mz.open[y, x])
                for d in range(4):
                    nx, ny = x + DX[d], y + DY[d]
                    if 0 <= nx < 8 and 0 <= ny < 8:
                        assert mz.open[y, x, d] == mz.open[ny, nx, OPP[d]]
                    else:
                        assert not mz.open[y, x, d]                      # the border is closed
        # following nxt from the start reaches the cheese in dist0 steps; PHI falls by 1 per cell along it
        cell, n = mz.start, 0
        while cell != mz.cheese:
            d = mz.nxt[cell[1], cell[0]]
            cell = (cell[0] + DX[d], cell[1] + DY[d])
            n += 1
        assert n == mz.dist[mz.start[1], mz.start[0]]
        p_a = mz.phi(np.array(mz.start) + 0.5, mz.start)
        d = mz.nxt[mz.start[1], mz.start[0]]
        nb = (mz.start[0] + DX[d], mz.start[1] + DY[d])
        assert abs(p_a - (mz.phi(np.array(nb) + 0.5, nb) + 1.0)) < 1e-9
    # walls stop the marker, open sides let it through, it slides along a wall, the border is closed
    mz = Maze(4, 4, 0.0, rng)
    for y in range(4):
        for x in range(4):
            pos = np.array([x + 0.5, y + 0.5])
            for d, step in ((E, (0.8, 0.0)), (W, (-0.8, 0.0)), (S, (0.0, 0.8)), (N, (0.0, -0.8))):
                p, bx, by = mz.move(pos, step)
                cell = (int(np.floor(p[0])), int(np.floor(p[1])))
                if mz.open[y, x, d]:
                    assert cell == (x + DX[d], y + DY[d]) and not (bx or by), (x, y, d)
                else:
                    assert cell == (x, y) and (bx or by), (x, y, d)
            p, bx, by = mz.move(pos, (0.0, 0.0))
            assert np.allclose(p, pos) and not (bx or by)
    x, y, side, along = next((x, y, d, a) for y in range(4) for x in range(4) for d in range(4) for a in range(4)
                             if not mz.open[y, x, d] and a not in (d, OPP[d]) and mz.open[y, x, a])
    diag = np.array([0.8 * DX[side] + 0.3 * DX[along], 0.8 * DY[side] + 0.3 * DY[along]])   # into the wall + along it
    p, bx, by = mz.move(np.array([x + 0.5, y + 0.5]), diag)
    assert (bx or by) and (int(np.floor(p[0])), int(np.floor(p[1]))) == (x, y)
    moved = abs(p[1] - (y + 0.5)) if DX[side] else abs(p[0] - (x + 0.5))
    assert abs(moved - 0.3) < 1e-9, moved                                       # the along-wall part went through
    # the potential telescopes: a random walk's shaping sums to PHI(start) - PHI(end), exactly
    pos, cell = np.array(mz.start) + 0.5, mz.start
    total, phi = 0.0, mz.phi(pos, cell)
    for _ in range(400):
        pos, _bx, _by = mz.move(pos, rng.uniform(-0.3, 0.3, 2))
        cell = (int(np.floor(pos[0])), int(np.floor(pos[1])))
        p2 = mz.phi(pos, cell)
        total += phi - p2
        phi = p2
    assert abs(total - (mz.phi(np.array(mz.start) + 0.5, mz.start) - phi)) < 1e-9
    # the env: an idle brain times out, the episode ends, the observation has the right size
    env = MazeEnv(0)
    assert env.obs_dim == OBS_DIM
    env.difficulty = 1.0
    obs = env.reset()
    e = env.e
    assert e.maze.n == 8 and e.limit == int(round(min(T_MAX, T_BASE + T_PER_CELL * e.dist0) / CTRL_DT))
    n, done, info = 0, False, {}
    while not done:
        obs, r, done, info = env.step(np.zeros(STEER_ACT))
        n += 1
        assert np.all(np.isfinite(obs)) and obs.shape == (OBS_DIM,)
    ev = env.drain_events()
    print(f'idle brain at difficulty 1: {n} steps, {info["timeouts"]} timed out, {info["hits"]} escaped, '
          f'fell {info["fell"]}, events {[(x["result"], x["steps"]) for x in ev]}')
    assert info['timeouts'] == 1 and info['hits'] == 0 and info['pressed'] is False and n == e.limit
    assert [x['result'] for x in ev] == ['timeout'] and ev[0]['steps'] == n and info['level'] == 1.0
    assert np.isnan(info['cheese_s'])
    # the senses: the open sides, the rough cheese vector (cue), the memory
    env.difficulty = 0.0
    obs = env.reset()
    mz = e.maze
    x, y = e.cell
    assert mz.n == 3 and np.array_equal(obs[STEER_DIM:STEER_DIM + 4], mz.open[y, x].astype(np.float32))
    cue = e._cue()
    rel = cue[0:2] / 3.0 / e.cell_size()
    assert abs(np.linalg.norm(rel) - e.rough_dist()) < 1e-6 and abs(e.rough_dist() % 0.5) < 1e-9
    assert cue[8] == 0.0 and cue[10] == 0.0 and abs(cue[2] - 10.0 * e.cell_size() / 2) < 1e-6
    assert obs[STEER_DIM + 11] == 3 / MAX_GRID and obs[STEER_DIM + 12:STEER_DIM + 16].sum() == 0.0
    snap = e.snapshot(0.0, True)
    assert snap['walls'] == mz.hex() and len(snap['walls']) == 9 and snap['cell'] == list(mz.start)
    assert snap['dist'] == e.dist0 and snap['trail'] == [list(mz.start)] and e.snapshot(0.0, False)['walls'] is None
    json.dumps(snap)
    # a course (the live view): teleporting the marker into the cheese cell escapes, a new harder maze appears with a
    # new id and the rat back in its standing pose; the course ends after LIVE_MAZES escapes with hits = LIVE_MAZES
    env.mazes_per_episode = LIVE_MAZES
    env.difficulty = 0.2
    env.reset()
    ids, done = [], False
    for k in range(LIVE_MAZES):
        ids.append(e.maze_id)
        d_before = e.maze_difficulty
        e.pos = e.maze.cheese_centre.copy()
        obs, r, done, info = env.step(np.zeros(STEER_ACT))
        assert info['maze_end'] == 'escaped' and r > R_ESCAPE / 2, (info, r)
        if k < LIVE_MAZES - 1:
            assert not done and info['new_trial'] and e.maze_id == ids[-1] + 1 and env.program == 0
            assert abs(e.maze_difficulty - (d_before + LIVE_STEP)) < 1e-9 and e.t_maze == 0
        else:
            assert done and not info['new_trial']
    ev = env.drain_events()
    assert [x['result'] for x in ev] == ['escaped'] * LIVE_MAZES and [x['maze_id'] for x in ev] == ids
    assert info['hits'] == LIVE_MAZES and info['timeouts'] == 0 and np.isfinite(info['cheese_s'])
    assert info['level'] == 0.2 and e.escape_s == [CTRL_DT] * LIVE_MAZES
    env.mazes_per_episode = 1
    # a fall: the cursor task's fall rule fires, the rat is put back on its feet and the maze goes on
    import mujoco
    env.reset()
    env.d.qpos[3:7] = [np.cos(np.pi / 4), np.sin(np.pi / 4), 0.0, 0.0]     # rolled onto its side
    mujoco.mj_forward(env.m, env.d)
    obs, r, done, info = env.step(np.zeros(STEER_ACT))
    assert info['fell_now'] and info['falls'] == 1 and not done and r < -35, (info, r)
    assert env.d.qpos[2] > 0.03 and env.program == 0 and e.t_maze == 1
    obs, r, done, info = env.step(np.zeros(STEER_ACT))
    assert not info['fell_now'] and info['falls'] == 1
    # the final steering network (widened, the new inputs at zero weight) in open rooms: it steers the marker straight
    # at the cheese (the cue), so it escapes most 3x3 rooms
    steer = os.path.join(HERE, 'runs', 'final', 'steer.pt')
    if os.path.exists(steer):
        pol = NumpyPolicy(load(steer), OBS_DIM)
        env.difficulty = 0.0
        res = []
        for k in range(6):
            obs, done = env.reset(), False
            while not done:
                obs, r, done, info = env.step(pol(obs).astype(np.float64))
            ev = env.drain_events()
            res.append((ev[-1]['result'], ev[-1]['steps'], info['bumps']))
        print(f'steering network at difficulty 0: {res}')
        assert sum(r == 'escaped' for r, _s, _b in res) >= 3, res
    print('maze_env selftest passed')


def _eval_worker(args):
    policy, difficulty, seed, course = args
    env = MazeEnv(seed, mazes_per_episode=LIVE_MAZES if course else 1)
    pol = NumpyPolicy(load(policy), env.obs_dim)
    env.difficulty = difficulty
    obs = env.reset()
    done, ret, info, steps = False, 0.0, {}, 0
    while not done:
        obs, r, done, info = env.step(pol(obs).astype(np.float64))
        ret += r
        steps += 1
    ev = env.drain_events()
    e = env.e
    return {'seed': seed, 'ret': ret, 'escaped': info['hits'], 'timeouts': info['timeouts'], 'mazes': len(ev),
            'cheese_s': [x['time_s'] for x in ev if x['result'] == 'escaped'],
            'bumps': info['bumps'], 'deadends': info['deadends'], 'revisits': info['revisits'],
            'fell': bool(info['fell']), 'falls': int(info['falls']), 'steps': steps, 'dist0': e.dist0,
            'grid': e.maze.n}


def _eval(a):
    from multiprocessing import Pool
    jobs = [(a.eval, a.difficulty, a.seed + i, a.course) for i in range(a.episodes)]
    with Pool(a.procs) as p:
        res = p.map(_eval_worker, jobs)
    n = len(res)
    mazes = sum(r['mazes'] for r in res)
    esc = sum(r['escaped'] for r in res)
    cs = [t for r in res for t in r['cheese_s']]
    lv = level(a.difficulty)
    out = {'policy': a.eval, 'difficulty': a.difficulty, 'grid': lv['grid'], 'open': round(lv['open'], 3),
           'episodes': n, 'mazes': mazes, 'escape_rate': round(esc / max(mazes, 1), 3),
           'escaped_per_episode': round(esc / n, 3), 'timeouts_per_episode': round(sum(r['timeouts'] for r in res) / n, 3),
           'cheese_s': round(float(np.mean(cs)), 2) if cs else None,
           'cheese_s_median': round(float(np.median(cs)), 2) if cs else None,
           'bumps_per_maze': round(sum(r['bumps'] for r in res) / max(mazes, 1), 2),
           'deadends_per_maze': round(sum(r['deadends'] for r in res) / max(mazes, 1), 2),
           'revisits_per_maze': round(sum(r['revisits'] for r in res) / max(mazes, 1), 2),
           'fall_rate': round(sum(r['fell'] for r in res) / n, 3),
           'falls_per_episode': round(sum(r['falls'] for r in res) / n, 3),
           'ret': round(float(np.mean([r['ret'] for r in res])), 2),
           'seconds_per_episode': round(float(np.mean([r['steps'] for r in res])) * CTRL_DT, 1)}
    print(json.dumps(out))
    return out


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--eval', metavar='CHECKPOINT', help='play episodes with this steering checkpoint (mean actions)')
    ap.add_argument('--difficulty', type=float, default=0.0)
    ap.add_argument('--episodes', type=int, default=24)
    ap.add_argument('--seed', type=int, default=1000)
    ap.add_argument('--procs', type=int, default=6)
    ap.add_argument('--course', action='store_true', help=f'the live view\'s courses of {LIVE_MAZES} mazes')
    a = ap.parse_args()
    if a.eval:
        _eval(a)
    else:
        _selftest()
