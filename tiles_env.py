"""RAT TILES: our own falling-tiles rhythm game, played by the two-network rat brain (steer_env.SteerEnv).

  screen      the cursor task's virtual screen ([0,1]^2, x right, y down), split into LANES = 4 lanes.
  tiles       one black tile per note of a public-domain melody (assets/songs.json) slides down its lane at a constant
              speed. The lane of each note is fixed by the song (song_lanes), so the tiles always spell the melody.
  target      the lowest untapped tile is the lit target once it is on screen (until then the cursor task holds and the
              brain rests, as between steps in the live rig): the steering network sees its moving rectangle through the
              cursor task's cue (cursor_env.CursorEnv._cue), plus TILE_DIM features appended at the END of its
              observation (train.py --resume widens a steering checkpoint for them): the tiles' speed, the time until
              the tile passes the bottom, when its bottom edge reaches and its top edge leaves the cursor's row, whether
              the cursor is in its lane, where the next tile is, and how many tiles are left.
  click       a lever press (the lever-press network, exactly as in steer_env) with the cursor on the lowest tile is a
              HIT: the tile turns grey and its note plays. A click anywhere else is WRONG (penalty). A tile whose top edge
              passes the bottom of the screen untapped is a MISS (penalty; its note is skipped). A tap is a press the
              steering network asked for: only the first click of each press program counts (the lever-press network
              sometimes presses again before it hands the body back), and the lever pushed down while the body rests
              between presses (a paw left on it) is not a click.
  trials      each press is a trial, as each step is in the live rig: when a press program ends (the lever-press
              network hands the body back), the rat is put back in its standing start pose (steer_env's new_trial).
              Without it, a paw left on or near the lever after a press froze the cursor (cursor_env's paw lock) and
              kept the lever from re-arming: measured with a trained tiles network, the cursor was frozen 69% of the
              time a tile was lit, and half the missed tiles were never pressed at all.
  falls       the cursor task's fall rule (head or trunk on the floor, too low, tipped over) costs its -20 plus
              R_FALL_EXTRA, and the rat is put back in its standing start pose (steer_env.SteerEnv.new_trial, what the
              live rig does between steps); the song goes on. The episode message's "fell" says it fell at least once.
              Most falls in the cursor tasks are the head driven into the floor, so here the steering network's
              head-DOWN commands are cut while the head is pitched down past PITCH_GUARD (a one-sided guard, in the
              spirit of steer_env's neck limit).
  episode     training: a phrase of EP_TILES consecutive notes of a random song. The live view (full_songs = True): a
              whole song, the songs in turn. It ends when every tile is resolved or after max_misses missed tiles.
  curriculum  difficulty 0..1 (train.py --curriculum; the cursor task's `difficulty` attribute), fixed per episode at
              its start, see level(): from one slow, tall tile at a time (it appears whole at the top when the one
              before it is resolved) to a column scrolling in from the top on the song's rhythm, faster and shorter.

Reward (chosen, disclosed): the cursor task's reward unchanged (cursor progress toward the lit rectangle, reaching for
the lever only on target, +50 for a click on it, -3 for a click elsewhere, posture, height, smoothness, -20 for a fall),
with the tile's own motion taken out of the progress term, plus R_MISS for every tile that passes the bottom,
R_FALL_EXTRA for every fall, and R_SWEET x the cursor's progress toward the tile's SWEET SPOT (the middle of its lane,
its lower part: the tile keeps covering the cursor there while a press takes its time; a click at the tile's edge
often lands after the tile has moved on).

Nothing here changes env.py, cursor_env.py or steer_env.py (their bytes are in the Labrat brain commit): TilesCursorEnv
subclasses CursorEnv, and TilesEnv subclasses SteerEnv with a TilesCursorEnv inside.

    python tiles_env.py --selftest
    python tiles_env.py --eval runs/final/steer.pt --difficulty 0.2 --episodes 24      (mean actions, no noise)
"""
import json
import os
import re

import numpy as np

from env import CTRL_DT
from cursor_env import CursorEnv, TARGET_TIME
from steer_env import SteerEnv, PRESS_NET, NECK, NECK_LIMIT, STEER_ACT
from ptload import load, NumpyPolicy

HERE = os.path.dirname(os.path.abspath(__file__))
SONGS_PATH = os.path.join(HERE, 'assets', 'songs.json')   # identical to site/assets/songs.json

LANES = 4
TILE_HW = 0.11            # tile half-width (a lane is 0.125 half-wide: a small gap between tiles)
EP_TILES = 8              # a training episode: a phrase of this many consecutive notes
MAX_MISSES = 3            # training: missed tiles that end an episode
LIVE_MAX_MISSES = 5       # the live view (whole songs)
MAX_SONG_NOTES = 64       # a song has at most this many notes (live/buyback.py's per-attempt hit cap for this task)
LEAD_IN = 0.5             # s from the episode start until the first tile starts to slide in
ONE_GAP = 0.3             # s after a tile is resolved until the next one appears (one-at-a-time levels)
MAX_EP_S = 300.0          # safety cap on one episode (s)
R_MISS = -10.0            # a tile passed the bottom untapped
R_FALL_EXTRA = -20.0      # a fall, on top of the cursor task's -20 (the rat is set back on its feet; the song goes on)
R_SWEET = 30.0            # per screen unit of cursor progress toward the sweet spot (the cursor task pays 60 for the tile)
SWEET_X = 0.6             # sweet spot: the middle 60% of the tile's width ...
SWEET_TOP = 0.35          # ... and its lower 65% (from 35% of its height below its top edge down to its bottom edge)
# Head-down guard. Most falls in the cursor tasks are the head driven into the floor (measured: a head pitched down
# ~0.6 rad or more from its rest direction, e.g. cervical_extend +0.6, touches the floor = a fall). While the head is
# pitched down more than PITCH_GUARD, the steering network's head-DOWN commands (cervical_extend > 0, atlas < 0) are
# cut to neutral; everything else, and every command below the guard, is untouched (the head overshoots it by up to
# ~0.2 rad). 0.35 rad still moves the cursor down at ~0.55 screen heights a second, faster than the fastest tiles.
# Measured with the final steering network at difficulty 0 (before trials were added): falls per 8-tile phrase 1.86
# without the guard, 0.69 at 0.45, 0.39 at 0.35.
PITCH_GUARD = 0.35        # rad of head-down pitch relative to the body's rest direction (cursor_env's deflection)
TILE_DIM = 9              # features appended to the steering observation
STEER_DIM = 21            # the steering network's own observation (steer_env.SteerEnv._obs)
OBS_DIM = STEER_DIM + TILE_DIM
T_CLAMP = int(round(TARGET_TIME / CTRL_DT)) - 2   # the cursor task's 8 s target timeout never fires on a tile
ABOVE = 1e-3              # target_rect never puts a tile's rectangle higher than this above the top edge


def level(difficulty):
    """The game at a curriculum difficulty in [0, 1]."""
    d = float(np.clip(difficulty, 0.0, 1.0))
    return {'speed': 0.07 + 0.43 * d,        # screen heights per second
            'hh': 0.15 - 0.065 * d,          # tile half-height (tiles 0.30 .. 0.17 of the screen tall)
            'beat_s': 4.0 - 2.8 * d,         # seconds between tiles per beat of the melody (rhythm levels)
            'one_at_a_time': d < 0.15}       # the next tile appears only after the current one is resolved


# ---------------------------------------------------------------------------------------------------- songs
_PC = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}
_NOTE = re.compile(r'^([A-G])([#b]?)(-?\d)$')


def midi(name):
    """'E4' -> 64, 'D#5' -> 75, 'Bb3' -> 58 (scientific pitch: C4 = middle C = 60)."""
    m = _NOTE.match(name)
    if not m:
        raise ValueError(f'bad note name {name!r}')
    return 12 * (int(m.group(3)) + 1) + _PC[m.group(1)] + {'': 0, '#': 1, 'b': -1}[m.group(2)]


def song_lanes(notes):
    """The lane of every note: the rank of its pitch among the song's distinct pitches (lowest = 0) mod 4; when that is
    the previous note's lane, the next lane to the right (mod 4) instead, so no lane is used twice in a row."""
    pitches = sorted({midi(n) for n, _b in notes})
    rank = {p: i for i, p in enumerate(pitches)}
    lanes, prev = [], None
    for n, _b in notes:
        lane = rank[midi(n)] % LANES
        if lane == prev:
            lane = (lane + 1) % LANES
        lanes.append(lane)
        prev = lane
    return lanes


def load_songs(path=SONGS_PATH):
    with open(path, encoding='utf-8') as f:
        songs = json.load(f)
    if not isinstance(songs, list) or not songs:
        raise ValueError(f'{path}: a list of songs is needed')
    ids = set()
    for s in songs:
        sid = s.get('id')
        if not isinstance(sid, str) or not sid or sid in ids:
            raise ValueError(f'{path}: bad or repeated song id {sid!r}')
        ids.add(sid)
        if s.get('public_domain') is not True:
            raise ValueError(f'{path}: {sid} is not marked public_domain')
        notes = s.get('notes')
        if not isinstance(notes, list) or not EP_TILES <= len(notes) <= MAX_SONG_NOTES:
            raise ValueError(f'{path}: {sid} needs {EP_TILES}..{MAX_SONG_NOTES} notes')
        for nb in notes:
            if (not isinstance(nb, list) or len(nb) != 2 or not isinstance(nb[0], str)
                    or not isinstance(nb[1], (int, float)) or isinstance(nb[1], bool) or not nb[1] > 0):
                raise ValueError(f'{path}: {sid}: bad note {nb!r}')
            midi(nb[0])
        s['lanes'] = song_lanes(notes)
    return songs


def lane_x(lane):
    return (lane + 0.5) / LANES


# ---------------------------------------------------------------------------------------------------- the game
class Tile:
    __slots__ = ('id', 'note_i', 'lane', 'beats', 'spawn', 'show', 'state')

    def __init__(self, tid, note_i, lane, beats):
        self.id, self.note_i, self.lane, self.beats = tid, note_i, lane, beats
        self.spawn = None          # the time its bottom edge is (or would be) at the top edge of the screen
        self.show = None           # the time it appears on the board (None: not scheduled yet)
        self.state = 'up'          # up | hit | miss


class TileGame:
    """The falling tiles of one episode. Pure (no physics): times are episode seconds."""

    def __init__(self, song, start, count, difficulty, first_id, now):
        lv = level(difficulty)
        self.song = song
        self.difficulty = float(difficulty)
        self.v, self.hh, self.beat_s, self.one = lv['speed'], lv['hh'], lv['beat_s'], lv['one_at_a_time']
        notes, lanes = song['notes'], song['lanes']
        self.tiles = [Tile(first_id + k, start + k, lanes[start + k], float(notes[start + k][1])) for k in range(count)]
        self.active = 0            # index of the lowest untapped tile (len(tiles) when all are resolved)
        if self.one:
            self._pop_in(self.tiles[0], now + LEAD_IN)     # the others follow as their predecessors are resolved
        else:
            t = now + LEAD_IN
            for tile in self.tiles:                        # a column on the song's rhythm, sliding in from the top
                tile.spawn = tile.show = t
                t += self.gap(tile.beats)

    def _pop_in(self, tile, t):
        """One-at-a-time levels: the tile appears whole at the top of the screen at time t, then falls."""
        tile.show = t
        tile.spawn = t - 2.0 * self.hh / self.v

    def gap(self, beats):
        return self.beat_s * float(np.clip(beats, 1.0, 2.0))

    def y(self, tile, t):
        """Centre y of a tile at time t (y down), -inf before it appears."""
        if tile.show is None or t < tile.show:
            return -np.inf
        return -self.hh + self.v * (t - tile.spawn)

    def active_tile(self):
        return self.tiles[self.active] if self.active < len(self.tiles) else None

    def next_tile(self):
        return self.tiles[self.active + 1] if self.active + 1 < len(self.tiles) else None

    def over(self):
        return self.active >= len(self.tiles)

    def remaining(self):
        return len(self.tiles) - self.active

    def resolve(self, state, now):
        tile = self.tiles[self.active]
        tile.state = state
        self.active += 1
        if self.one and self.active < len(self.tiles):
            self._pop_in(self.tiles[self.active], now + ONE_GAP)
        return tile

    def visible(self, tile, t):
        return self.y(tile, t) + self.hh > 0.0

    def target_rect(self, tile, t):
        """The tile's rectangle (it may reach above the top of the screen while it slides in)."""
        y = max(self.y(tile, t), -self.hh - ABOVE)
        return np.array([lane_x(tile.lane), y]), np.array([TILE_HW, self.hh])

    def sweet_rect(self, tile, t):
        """The tile's sweet spot (see R_SWEET): its middle SWEET_X of width, from SWEET_TOP of its height down."""
        y = self.y(tile, t)
        top, bot = y - self.hh + SWEET_TOP * 2 * self.hh, y + self.hh
        return (np.array([lane_x(tile.lane), (top + bot) / 2]), np.array([SWEET_X * TILE_HW, (bot - top) / 2]))

    def on_screen(self, t):
        out = []
        for tile in self.tiles:
            y = self.y(tile, t)
            if y + self.hh > 0.0 and y - self.hh < 1.0:
                out.append((tile, y))
        return out


# ---------------------------------------------------------------------------------------------------- inner env
class TilesCursorEnv(CursorEnv):
    """CursorEnv whose lit target is the lowest untapped falling tile (the physics, cursor, click and fall rules are
    CursorEnv's, unchanged)."""

    def __init__(self, seed=0, record=False, randomize=False, songs=None):
        self.songs = songs if songs is not None else load_songs()
        self.full_songs = False     # the live view: whole songs in turn (training: EP_TILES-note phrases)
        self.max_misses = MAX_MISSES
        self.game = None
        self.events = []            # tile outcomes since the last drain_events()
        self.next_id = 0            # tile ids are unique for the life of this env
        self.n_games = 0
        self.n_songs = 0            # whole songs played (full_songs: the songs in turn, from the first)
        self.falls = 0              # falls this episode
        self._new_episode = False
        self._aimed = None          # id of the tile the cursor task's target is on (None: holding)
        self.tc = np.array([0.5, 0.5])          # the cursor task's target (a hold ignores it until a tile lights up)
        self.th = np.array([TILE_HW, 0.1])
        super().__init__(seed, record, randomize)

    # ---- episode
    def reset(self, seed=None, randomize=None, cursor=None):
        self.external_target = 'tiles'     # CursorEnv: a hit waits for the next target (no random target, no holds)
        self._new_episode = True
        return super().reset(seed=seed, randomize=randomize, cursor=cursor)

    def _start_game(self):
        if self.full_songs:
            song = self.songs[self.n_songs % len(self.songs)]
            self.n_songs += 1
            start, count = 0, len(song['notes'])
            self.max_misses_now = LIVE_MAX_MISSES
        else:
            song = self.songs[int(self.rng.integers(len(self.songs)))]
            count = EP_TILES
            start = int(self.rng.integers(len(song['notes']) - count + 1))
            self.max_misses_now = self.max_misses
        self.game = TileGame(song, start, count, self.difficulty, self.next_id, self.t * CTRL_DT)
        self.next_id += count
        self.n_games += 1
        self.events = []
        self.falls = 0

    def _new_target(self):
        """CursorEnv calls this at reset: start the episode's game (its first tile is not on screen yet: a hold)."""
        if self._new_episode or self.game is None:
            self._new_episode = False
            self._start_game()
        self._aimed = None
        self._aim(self.t * CTRL_DT)

    def _aim(self, now):
        """The cursor task's target follows the lowest untapped tile while it is on screen. Before it appears (and
        after the last tile) the cursor task HOLDS: as in the live rig, the brain rests in its standing pose and cannot
        press (steer_env), instead of straining its head toward a tile above the screen."""
        g = self.game
        tile = g.active_tile()
        if tile is None or not g.visible(tile, now):
            self.set_hold()
            self._aimed = None
            return
        c, h = g.target_rect(tile, now)
        if self._aimed != tile.id:
            self.set_target(c, h)                 # a new tile lights up: target time and progress start over
            self._aimed = tile.id
        else:
            self.tc = c                           # the lit tile moves
            self.th = h

    # ---- one control step
    def _event(self, tile, result):
        self.events.append({'type': 'tile', 'id': int(tile.id), 'lane': int(tile.lane), 'result': result,
                            'note_i': int(tile.note_i), 'song': self.game.song['id']})

    def drain_events(self):
        ev, self.events = self.events, []
        return ev

    def _sweet_dist(self, now):
        """Distance from the cursor to the lit tile's sweet spot (0 inside), or None while holding."""
        tile = self.game.active_tile()
        if self.hold != 0 or tile is None:
            return None
        c, h = self.game.sweet_rect(tile, now)
        dx = max(abs(self.cursor[0] - c[0]) - h[0], 0.0)
        dy = max(abs(self.cursor[1] - c[1]) - h[1], 0.0)
        return float(np.hypot(dx, dy))

    def step(self, action):
        g = self.game
        now = (self.t + 1) * CTRL_DT          # tile positions at the end of this step, when clicks are judged
        r_tiles = 0.0
        while not g.over() and g.y(g.active_tile(), now) - g.hh >= 1.0:
            tile = g.resolve('miss', now)     # passed the bottom untapped
            self.timeouts += 1
            r_tiles += R_MISS
            self._event(tile, 'miss')
        self._aim(now)
        if self.hold == 0:
            self.prev_cd = self._cursor_dist()    # progress counts the cursor's motion, not the tile's
            self.t_target = min(self.t_target, T_CLAMP)
        sweet0 = self._sweet_dist(now)
        # a tap is a press the steering network asked for (TilesEnv.tap_allowed): any other lever push is not a click
        gate = getattr(self, 'tap_gate', None)
        blocked = gate is not None and not gate()
        armed = self.armed
        if blocked:
            self.armed = False
        obs, r, _done, info = super().step(action)
        if blocked:
            self.armed = armed or self.armed      # unchanged, or re-armed if the lever came back up
        sweet1 = self._sweet_dist(now)
        if sweet0 is not None and sweet1 is not None:
            r_tiles += R_SWEET * (sweet0 - sweet1)
        click = info.get('click')
        if click is not None:
            if click[3]:                          # on the lowest tile (CursorEnv paid its +50 and waits: set_hold)
                tile = g.resolve('hit', now)
                self._event(tile, 'hit')
                self._aim(now)
            else:                                 # anywhere else (CursorEnv charged its -3, or -2 during a hold)
                tile = g.active_tile() or g.tiles[-1]
                self.events.append({'type': 'tile', 'id': int(tile.id),
                                    'lane': int(np.clip(int(click[1] * LANES), 0, LANES - 1)), 'result': 'wrong',
                                    'note_i': int(tile.note_i), 'song': g.song['id']})
        fell_now = bool(info['fell'])             # TilesEnv puts the rat back on its feet; the song goes on
        self.falls += fell_now
        done = g.over() or self.timeouts >= self.max_misses_now or self.t * CTRL_DT >= MAX_EP_S
        info['fell_now'] = fell_now
        info['falls'] = self.falls
        info['fell'] = self.falls > 0 if done else fell_now     # at the end: fell at least once this episode
        info['level'] = g.difficulty
        info['tiles_left'] = g.remaining()
        return obs, r + r_tiles, done, info

    # ---- for the live view
    def visible_active_rect(self):
        """(cx, cy, hw, hh) of the on-screen part of the lowest untapped tile, or None."""
        g = self.game
        tile = g.active_tile() if g is not None else None
        if tile is None:
            return None
        y = g.y(tile, self.t * CTRL_DT)
        top, bot = max(y - g.hh, 0.0), min(y + g.hh, 1.0)
        if bot <= top:
            return None
        return (float(lane_x(tile.lane)), float((top + bot) / 2), float(TILE_HW), float((bot - top) / 2))

    def snapshot(self, sim_t):
        """The {"type":"tiles"} message: the board now."""
        g = self.game
        now = self.t * CTRL_DT
        tile = g.active_tile()
        return {'type': 'tiles', 't': round(float(sim_t), 3), 'song': g.song['id'], 'speed': round(g.v, 4),
                'lanes': LANES, 'cursor': [round(float(self.cursor[0]), 4), round(float(self.cursor[1]), 4)],
                'tiles': [[int(t.id), int(t.lane), round(float(y), 4), round(2 * g.hh, 4), t.state]
                          for t, y in g.on_screen(now)],
                'note_i': int(tile.note_i) if tile is not None else len(g.song['notes'])}


# ---------------------------------------------------------------------------------------------------- the brain's env
class TilesEnv(SteerEnv):
    """SteerEnv (steering network + lever-press network) on the Rat Tiles board. The observation is SteerEnv's 21
    features followed by TILE_DIM tile features."""

    def __init__(self, seed=0, record=False, randomize=False, press_net=PRESS_NET, songs=None, full_songs=False):
        # SteerEnv.__init__, with the tiles game as its cursor env (steer_env.py itself is not changed)
        self.e = TilesCursorEnv(seed, record, randomize, songs=songs)
        self.e.full_songs = bool(full_songs)
        self.m, self.d = self.e.m, self.e.d
        self.neck = [i for i in range(self.m.nu) if self.m.actuator(i).name in NECK]
        self.neck_limit = np.array([NECK_LIMIT if self.m.actuator(i).name in ('cervical_extend', 'atlas') else 1.0
                                    for i in self.neck])
        self.press_pol = NumpyPolicy(load(press_net), 200)
        names = [self.m.actuator(i).name for i in self.neck]
        self.i_extend, self.i_atlas = names.index('cervical_extend'), names.index('atlas')   # in the action vector
        self.program, self._clicked_prog = 0, False
        self.e.tap_gate = self.tap_allowed
        self.nu = STEER_ACT
        self.obs_dim = len(self.reset())
        assert self.obs_dim == OBS_DIM, self.obs_dim

    @property
    def full_songs(self):
        return self.e.full_songs

    @full_songs.setter
    def full_songs(self, v):
        self.e.full_songs = bool(v)

    def _tile_obs(self):
        e = self.e
        f = np.zeros(TILE_DIM, np.float32)
        g = getattr(e, 'game', None)
        if g is None or not hasattr(e, 'cursor'):
            return f
        now = e.t * CTRL_DT
        cx, cy = float(e.cursor[0]), float(e.cursor[1])
        tile = g.active_tile()
        if tile is not None:
            y = g.y(tile, now)
            top, bot = y - g.hh, y + g.hh
            f[0] = g.v * 4.0                                          # tile speed
            f[1] = np.clip((1.0 - top) / g.v, 0.0, 10.0) / 5.0       # s until it passes the bottom (a miss)
            f[2] = np.clip((cy - top) / g.v, -2.0, 4.0) / 2.0        # s until its top edge passes the cursor's row
            f[3] = np.clip((cy - bot) / g.v, -4.0, 4.0) / 2.0        # s until its bottom edge reaches the cursor's row
            f[4] = float(abs(cx - lane_x(tile.lane)) <= TILE_HW)     # cursor in its lane
            nxt = g.next_tile()
            if nxt is not None:
                ny = max(g.y(nxt, now), -g.hh - ABOVE)
                f[5] = np.clip((lane_x(nxt.lane) - cx) * 3.0, -3.0, 3.0)
                f[6] = np.clip((ny - cy) * 3.0, -3.0, 3.0)
                f[7] = 1.0
        f[8] = min(g.remaining(), EP_TILES) / EP_TILES
        return f

    def _obs(self):
        return np.concatenate([super()._obs(), self._tile_obs()]).astype(np.float32)

    def tap_allowed(self):
        """A click counts only as the first click of a press program (see "click" above)."""
        return self.program > 0 and not self._clicked_prog

    def step(self, action):
        e = self.e
        action = np.array(action, dtype=np.float64)
        if e._deflection()[1] > PITCH_GUARD:                 # head-down guard (PITCH_GUARD): no head on the floor
            action[self.i_extend] = min(action[self.i_extend], 0.0)
            action[self.i_atlas] = max(action[self.i_atlas], 0.0)
        program = self.program
        obs, r, done, info = super().step(action)
        if info.get('fell_now'):
            r += R_FALL_EXTRA
        pressed_out = program > 0 and self.program == 0          # a press program just ended ("trials" above)
        info['new_trial'] = bool((info.get('fell_now') or pressed_out) and not done)
        if info['new_trial']:
            self.new_trial()         # back in its standing start pose, as the live rig does between steps
            obs = self._obs()
        return obs, r, done, info

    def drain_events(self):
        return self.e.drain_events()


# ---------------------------------------------------------------------------------------------------- checks / eval
def _selftest():
    songs = load_songs()
    for s in songs:
        lanes = s['lanes']
        assert len(lanes) == len(s['notes']) and all(0 <= x < LANES for x in lanes)
        assert all(a != b for a, b in zip(lanes, lanes[1:])), s['id']
        assert song_lanes(s['notes']) == lanes
    site = os.path.join(HERE, 'site', 'assets', 'songs.json')
    if os.path.exists(site):
        assert open(site, 'rb').read() == open(SONGS_PATH, 'rb').read(), 'site/assets/songs.json differs'
    assert midi('C4') == 60 and midi('A4') == 69 and midi('D#5') == 75 and midi('Bb3') == 58
    # the game alone: at the easy level a tile appears whole at the top and passes the bottom 1 / v seconds later;
    # at the hard level every tile is scheduled at once and slides in from the top edge
    g = TileGame(songs[0], 0, 3, 0.0, 0, 0.0)
    assert abs(g.y(g.tiles[0], LEAD_IN) - g.hh) < 1e-9 and g.tiles[1].show is None
    assert g.y(g.tiles[0], LEAD_IN - 0.01) == -np.inf
    assert g.y(g.tiles[0], LEAD_IN + 1.0 / g.v) - g.hh >= 1.0 - 1e-9
    g.resolve('hit', 2.0)
    assert g.tiles[1].show == 2.0 + ONE_GAP and abs(g.y(g.tiles[1], 2.0 + ONE_GAP) - g.hh) < 1e-9
    g2 = TileGame(songs[0], 0, 3, 1.0, 0, 0.0)
    assert all(t.show is not None for t in g2.tiles) and g2.tiles[1].spawn > g2.tiles[0].spawn
    assert g2.y(g2.tiles[0], LEAD_IN) == -g2.hh
    # the env: an idle brain misses tiles, the episode ends, the observation has the right size
    env = TilesEnv(0)
    assert env.obs_dim == OBS_DIM
    env.difficulty = 1.0
    obs = env.reset()
    n, done, info = 0, False, {}
    while not done:
        obs, r, done, info = env.step(np.zeros(STEER_ACT))
        n += 1
        assert np.all(np.isfinite(obs)) and obs.shape == (OBS_DIM,)
    ev = env.drain_events()
    print(f'idle brain at difficulty 1: {n} steps, {info["timeouts"]} missed, {info["hits"]} hits, '
          f'fell {info["fell"]}, events {[e["result"] for e in ev]}')
    assert info['timeouts'] == MAX_MISSES and [e['result'] for e in ev] == ['miss'] * MAX_MISSES
    # a fall: the cursor task's fall rule fires, the rat is put back on its feet and the episode goes on
    import mujoco
    env.reset()
    env.d.qpos[3:7] = [np.cos(np.pi / 4), np.sin(np.pi / 4), 0.0, 0.0]     # rolled onto its side
    mujoco.mj_forward(env.m, env.d)
    obs, r, done, info = env.step(np.zeros(STEER_ACT))
    assert info['fell_now'] and info['falls'] == 1 and not done and r < -35, (info, r)
    assert env.d.qpos[2] > 0.03 and env.program == 0
    obs, r, done, info = env.step(np.zeros(STEER_ACT))
    assert not info['fell_now'] and info['falls'] == 1
    # the final steering network (widened, the new inputs at zero weight) plays an easy phrase: taps, one per press,
    # each press followed by a new trial, and the events spell the phrase's notes in order
    steer = os.path.join(HERE, 'runs', 'final', 'steer.pt')
    if os.path.exists(steer):
        pol = NumpyPolicy(load(steer), OBS_DIM)
        env.difficulty = 0.0
        obs, done, trials, events = env.reset(), False, 0, []
        while not done:
            obs, r, done, info = env.step(pol(obs).astype(np.float64))
            trials += info['new_trial']
            events += env.drain_events()
        hits = [e for e in events if e['result'] == 'hit']
        g = env.e.game
        print(f'steering network at difficulty 0: {info["hits"]} of {len(g.tiles)} tapped, {trials} trials')
        assert info['hits'] >= 6 and trials >= info['hits'] and len(hits) == info['hits']
        assert [e['note_i'] for e in hits] == sorted(e['note_i'] for e in hits)
    print('tiles_env selftest passed')


def _eval_worker(args):
    policy, difficulty, seed, full = args
    env = TilesEnv(seed, full_songs=full)
    pol = NumpyPolicy(load(policy), env.obs_dim)
    env.difficulty = difficulty
    obs = env.reset()
    done, ret, info = False, 0.0, {}
    while not done:
        obs, r, done, info = env.step(pol(obs).astype(np.float64))
        ret += r
    return {'seed': seed, 'ret': ret, 'hits': info['hits'], 'missed': info['timeouts'], 'wrong': info['misses'],
            'fell': bool(info['fell']), 'falls': int(info['falls']), 'tiles': info['hits'] + info['timeouts'],
            'steps': env.e.t, 'song': env.e.game.song['id']}


def _eval(a):
    from multiprocessing import Pool
    jobs = [(a.eval, a.difficulty, a.seed + i, a.full_songs) for i in range(a.episodes)]
    with Pool(a.procs) as p:
        res = p.map(_eval_worker, jobs)
    hits = sum(r['hits'] for r in res)
    tiles = sum(r['tiles'] for r in res)
    out = {'policy': a.eval, 'difficulty': a.difficulty, 'speed': round(level(a.difficulty)['speed'], 3),
           'episodes': len(res), 'hits_per_episode': round(hits / len(res), 3),
           'hit_rate': round(hits / max(tiles, 1), 3),
           'missed_per_episode': round(sum(r['missed'] for r in res) / len(res), 3),
           'wrong_per_episode': round(sum(r['wrong'] for r in res) / len(res), 3),
           'fall_rate': round(sum(r['fell'] for r in res) / len(res), 3),
           'falls_per_episode': round(sum(r['falls'] for r in res) / len(res), 3),
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
    ap.add_argument('--full-songs', action='store_true')
    a = ap.parse_args()
    if a.eval:
        _eval(a)
    else:
        _selftest()
