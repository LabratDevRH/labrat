"""RAT TILES: our own falling-tiles rhythm game, played by the two-network rat brain (steer_env.SteerEnv).

Game rule v2 (RULES = 2):

  screen      the cursor task's virtual screen ([0,1]^2, x right, y down), split into LANES = 4 lanes.
  tiles       one black tile per note of a public-domain melody (assets/songs.json) slides down its lane at a constant
              speed. The lane of each note is fixed by the song (song_lanes), so the tiles always spell the melody.
  hit line    each lane has a RED BUTTON on a hit line near the bottom (y = HIT_Y). The hit band is the hit line +- the
              level's `window` (screen heights). A tile is on time while it overlaps the hit band; it is PERFECT when
              its centre is on the hit line. `timing` = seconds late (+) or early (-) of perfect; a tile overlaps the
              band for +- window_time() = (tile half-height + window) / speed seconds around perfect.
  lanes       the rat moves between the lanes with its head (the cursor's x picks the lane; its y does not matter for
              the rule). The cursor task's lit target is the red button of the lowest untapped tile's lane (while that
              tile is on screen; until then the cursor task holds and the brain rests, as between steps in the rig).
  press       a lever press (the lever-press network, exactly as in steer_env) is a HIT only if the cursor is in the
              lane of the lowest untapped tile AND that tile overlaps the hit band when the click registers: the tile
              turns grey and its note plays. Any other press is WRONG (penalty): an empty band in that lane, the wrong
              lane, too early. A tile whose top edge passes below the hit band untapped is a MISS (penalty; its note is
              skipped). The click registers 0.12-0.16 s after the steering network asks for a press (measured, 32
              presses of a trained network: every press program clicked on its 7th to 9th control step), and the
              cursor is frozen meanwhile, so the rat must press a little before the tile is centred on the line.
              A tap is a press the steering network asked for: only the first click of each press program counts (the
              lever-press network sometimes presses again before it hands the body back), and the lever pushed down
              while the body rests between presses (a paw left on it) is not a click.
  observation the steering network sees the lit button through the cursor task's cue (cursor_env.CursorEnv._cue; its
              "on target" flag says the cursor is on the lit button, as in the cursor task: it does NOT say when to
              press) plus TILE_DIM + TIMING_DIM features appended at the END of its observation (train.py --resume
              widens a checkpoint for them):
                TILE_DIM (the v1 block, now measured at the hit line): the tiles' speed, the time until the lowest tile
                  leaves the hit band (a miss), when its top edge passes and its bottom edge reaches the hit line,
                  whether the cursor is in its lane, where the next tile is, and how many tiles are left;
                TIMING_DIM (new in v2): the time until the lowest tile is perfect (coarse, and fine within +-0.5 s),
                  half the timing window in seconds, for each lane the time until the lowest tile on screen in that
                  lane enters and leaves the hit band, and the cursor's position.
              (A first version lit the cue's flag only while a press would hit. The trained networks then pressed the
              moment it lit, as the tile entered the band, and 1.5M steps of training did not move that: at difficulty
              1, -68 ms before and -84 ms after, always early. With the flag on the button, an early press is a wrong
              press, and the timing inputs are what tells the rat when to press.)
  trials      each press is a trial, as each step is in the live rig: when a press program ends (the lever-press
              network hands the body back), the rat is put back in its standing start pose (steer_env's new_trial).
              Without it, a paw left on or near the lever after a press froze the cursor (cursor_env's paw lock) and
              kept the lever from re-arming.
  falls       the cursor task's fall rule (head or trunk on the floor, too low, tipped over) costs its -20 plus
              R_FALL_EXTRA, and the rat is put back in its standing start pose (steer_env.SteerEnv.new_trial, what the
              live rig does between steps); the song goes on. The episode message's "fell" says it fell at least once.
              Most falls in the cursor tasks are the head driven into the floor, so here the steering network's
              head-DOWN commands are cut while the head is pitched down past PITCH_GUARD (a one-sided guard, in the
              spirit of steer_env's neck limit).
  episode     training: a phrase of EP_TILES consecutive notes of a random song. The live view (full_songs = True): a
              whole song, the songs in turn. It ends when every tile is resolved or after max_misses missed tiles.
  curriculum  difficulty 0..1 (train.py --curriculum; the cursor task's `difficulty` attribute), fixed per episode at
              its start, see level(): from one slow, tall tile at a time with a wide hit band (it appears whole at the
              top ONE_GAP after the one before it was perfect, or was resolved if later) to a column scrolling in from
              the top on the song's rhythm, faster, shorter, with a narrow hit band.

Reward (chosen, disclosed): the cursor task's reward (cursor progress toward the lit button, reaching for the lever
only while a press would hit, +50 for a hit, -3 for a wrong press, posture, height, smoothness, -20 for a fall), plus a
timing bonus of R_TIMING x (1 - |timing| / window_time()) for every hit, with the hit and its bonus valued at the
moment the tile is perfect (an early hit is paid their value discounted back to the click, see R_HIT), R_MISS for
every missed tile, R_FALL_EXTRA for every fall, and R_SWEET x the cursor's progress toward the middle of the lit
button.

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

RULES = 2                 # the game rule version; train.py saves it in tiles checkpoints (a resume of an older one
                          # starts the curriculum over)
LANES = 4
LANE_HW = 0.5 / LANES     # a lane's half-width: the cursor is in a lane anywhere across it
TILE_HW = 0.11            # tile half-width (a small gap between tiles)
HIT_Y = 0.86              # the hit line (the red buttons), screen heights from the top
BTN_HH = 0.06             # the lit target's half-height: the red button of the lowest tile's lane, on the hit line
EP_TILES = 8              # a training episode: a phrase of this many consecutive notes
MAX_MISSES = 3            # training: missed tiles that end an episode
LIVE_MAX_MISSES = 5       # the live view (whole songs)
MAX_SONG_NOTES = 64       # a song has at most this many notes (live/buyback.py's per-attempt hit cap for this task)
LEAD_IN = 0.5             # s from the episode start until the first tile starts to slide in
ONE_GAP = 0.3             # s from a tile's perfect moment (or its resolution, if later) until the next one appears
                          # (one-at-a-time levels)
MAX_EP_S = 300.0          # safety cap on one episode (s)
R_MISS = -10.0            # a tile passed the hit band untapped
R_TIMING = 40.0           # a hit's timing bonus: R_TIMING x (1 - |timing| / window_time()). With +10 (and R_HIT's
                          # discounting) 0.84M steps of training cut the wrong presses by half but did not move the
                          # timing at all (difficulty 0: about -0.85 s, early): waiting 0.9 s for a perfect tap was
                          # worth about +4 there. +40 makes a perfect hit worth 90 and a hit at the band's edge 50.
# A hit is worth R_HIT (CursorEnv's +50 for a click on the lit target) + the timing bonus, AT THE MOMENT THE TILE IS
# PERFECT: a press that lands early is paid that amount discounted back from the perfect moment to the click, with
# train.py's discount (GAMMA per control step). Without that, a hit's +50 paid up to window_time() sooner outweighed
# the timing bonus under the discount (0.99^67 = 0.51 across the widest band), so the best policy was to press as the
# tile entered the band. With it, when to press changes only the timing bonus; one-at-a-time tiles are scheduled from
# the perfect moment too (TileGame.resolve), so an early tap does not bring the rest of the song sooner either.
R_HIT = 50.0
GAMMA = 0.99              # train.py's discount per control step
R_FALL_EXTRA = -20.0      # a fall, on top of the cursor task's -20 (the rat is set back on its feet; the song goes on)
R_SWEET = 30.0            # per screen unit of cursor progress toward the sweet spot (the cursor task pays 60 for the
                          # lit button)
SWEET_X = 0.6             # sweet spot: the middle 60% of the lit button's width
# Head-down guard. Most falls in the cursor tasks are the head driven into the floor (measured: a head pitched down
# ~0.6 rad or more from its rest direction, e.g. cervical_extend +0.6, touches the floor = a fall). While the head is
# pitched down more than PITCH_GUARD, the steering network's head-DOWN commands (cervical_extend > 0, atlas < 0) are
# cut to neutral; everything else, and every command below the guard, is untouched (the head overshoots it by up to
# ~0.2 rad). 0.35 rad still moves the cursor down at ~0.55 screen heights a second.
# Measured with the final steering network at difficulty 0 (before trials were added): falls per 8-tile phrase 1.86
# without the guard, 0.69 at 0.45, 0.39 at 0.35.
PITCH_GUARD = 0.35        # rad of head-down pitch relative to the body's rest direction (cursor_env's deflection)
TILE_DIM = 9              # features appended to the steering observation (rule v1's block, measured at the hit line)
TIMING_DIM = 13           # rule v2's timing features, appended after them
STEER_DIM = 21            # the steering network's own observation (steer_env.SteerEnv._obs)
OBS_DIM = STEER_DIM + TILE_DIM + TIMING_DIM
T_CLAMP = int(round(TARGET_TIME / CTRL_DT)) - 2   # the cursor task's 8 s target timeout never fires on a tile
ABOVE = 1e-3              # a tile's rectangle is never put higher than this above the top edge
LANE_T_MAX = 6.0          # s: the per-lane enter / leave times are clipped here (also: no tile in that lane)


def level(difficulty):
    """The game at a curriculum difficulty in [0, 1]."""
    d = float(np.clip(difficulty, 0.0, 1.0))
    return {'speed': 0.2 + 0.3 * d,          # screen heights per second
            'hh': 0.15 - 0.065 * d,          # tile half-height (tiles 0.30 .. 0.17 of the screen tall)
            'window': 0.12 - 0.09 * d,       # the hit band's half-height (0.24 .. 0.06 of the screen tall)
            'beat_s': 3.5 - 1.8 * d,         # seconds between tiles per beat of the melody (rhythm levels)
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


def hit_reward(timing, window_s):
    """A hit's whole reward at the click: R_HIT + the timing bonus, discounted back from the perfect moment to the
    click when the press landed early (see R_HIT)."""
    bonus = R_TIMING * max(0.0, 1.0 - abs(timing) / window_s)
    early = max(0.0, -timing) / CTRL_DT                  # control steps before perfect
    return (R_HIT + bonus) * GAMMA ** early


def lane_of(x):
    """The lane a screen x is in."""
    return int(np.clip(int(float(x) * LANES), 0, LANES - 1))


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
        self.v, self.hh, self.w = lv['speed'], lv['hh'], lv['window']
        self.beat_s, self.one = lv['beat_s'], lv['one_at_a_time']
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

    # ---- the hit line (rule v2)
    def window_time(self):
        """Half the time a tile overlaps the hit band (s): it is on time within +- this of perfect."""
        return (self.hh + self.w) / self.v

    def timing(self, tile, t):
        """Seconds late (+) or early (-) of perfect (the tile's centre on the hit line) at time t."""
        return (self.y(tile, t) - HIT_Y) / self.v

    def in_window(self, tile, t):
        """The tile overlaps the hit band at time t."""
        return abs(self.y(tile, t) - HIT_Y) <= self.hh + self.w

    def passed(self, tile, t):
        """The tile's top edge is below the hit band (too late: a miss if it was not tapped)."""
        return self.y(tile, t) - self.hh > HIT_Y + self.w

    def active_tile(self):
        return self.tiles[self.active] if self.active < len(self.tiles) else None

    def next_tile(self):
        return self.tiles[self.active + 1] if self.active + 1 < len(self.tiles) else None

    def over(self):
        return self.active >= len(self.tiles)

    def remaining(self):
        return len(self.tiles) - self.active

    def perfect_time(self, tile):
        """The time the tile's centre is on the hit line."""
        return tile.spawn + (HIT_Y + self.hh) / self.v

    def resolve(self, state, now):
        tile = self.tiles[self.active]
        tile.state = state
        self.active += 1
        if self.one and self.active < len(self.tiles):
            # the next tile appears ONE_GAP after this one's perfect moment (or after a late tap / a miss): tapping
            # early does not bring the rest of the song sooner (see R_HIT)
            self._pop_in(self.tiles[self.active], max(now, self.perfect_time(tile)) + ONE_GAP)
        return tile

    def visible(self, tile, t):
        return self.y(tile, t) + self.hh > 0.0

    def target_rect(self, tile, t):
        """The tile's rectangle (it may reach above the top of the screen while it slides in)."""
        y = max(self.y(tile, t), -self.hh - ABOVE)
        return np.array([lane_x(tile.lane), y]), np.array([TILE_HW, self.hh])

    @staticmethod
    def button_rect(lane):
        """The red button of a lane, on the hit line: the cursor task's lit target while that lane's tile is lowest."""
        return np.array([lane_x(lane), HIT_Y]), np.array([TILE_HW, BTN_HH])

    @staticmethod
    def sweet_rect(lane):
        """The lit button's sweet spot (see R_SWEET): its middle SWEET_X of width."""
        return np.array([lane_x(lane), HIT_Y]), np.array([SWEET_X * TILE_HW, BTN_HH])

    def on_screen(self, t):
        out = []
        for tile in self.tiles:
            y = self.y(tile, t)
            if y + self.hh > 0.0 and y - self.hh < 1.0:
                out.append((tile, y))
        return out


# ---------------------------------------------------------------------------------------------------- inner env
class TilesCursorEnv(CursorEnv):
    """CursorEnv whose lit target is the red button of the lowest untapped tile's lane, and whose "on target" is rule
    v2's hit test (the physics, cursor, click and fall rules are CursorEnv's, unchanged)."""

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
        self.timing_abs = []        # |timing| of every hit this episode (s)
        self._new_episode = False
        self._aimed = None          # id of the tile whose lane's button is the cursor task's target (None: holding)
        self.tc = np.array([0.5, 0.5])          # the cursor task's target (a hold ignores it until a tile lights up)
        self.th = np.array([TILE_HW, BTN_HH])
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
        self.timing_abs = []

    def _new_target(self):
        """CursorEnv calls this at reset: start the episode's game (its first tile is not on screen yet: a hold)."""
        if self._new_episode or self.game is None:
            self._new_episode = False
            self._start_game()
        self._aimed = None
        self._aim(self.t * CTRL_DT)

    def _aim(self, now):
        """The cursor task's target is the red button of the lowest untapped tile's lane while that tile is on screen.
        Before it appears (and after the last tile) the cursor task HOLDS: as in the live rig, the brain rests in its
        standing pose and cannot press (steer_env)."""
        g = self.game
        tile = g.active_tile()
        if tile is None or not g.visible(tile, now):
            self.set_hold()
            self._aimed = None
            return
        if self._aimed != tile.id:
            c, h = g.button_rect(tile.lane)
            self.set_target(c, h)                 # a new tile lights up its lane's button: progress starts over
            self._aimed = tile.id

    def cursor_lane(self):
        return lane_of(self.cursor[0])

    def _cue(self):
        """The cursor task's cue, with its "on target" flag (index 8) as the cursor task means it: the cursor is on the
        lit button. It does not say whether a press now would be on time (see "observation" above)."""
        c = super()._cue()
        c[8] = float(self.hold == 0 and self._cursor_dist() == 0.0)
        return c

    def on_target(self):
        """Rule v2's hit test, at the env's clock: the cursor is in the lowest untapped tile's lane and that tile
        overlaps the hit band. CursorEnv uses it for the click (+50 hit / -3 wrong) and its reaching terms (the cue's
        flag is _cue's); the cursor's y does not count (the lit button's rectangle only guides the progress terms)."""
        g = self.game
        tile = g.active_tile() if g is not None else None
        if tile is None or not hasattr(self, 'cursor'):
            return False
        return self.cursor_lane() == tile.lane and bool(g.in_window(tile, self.t * CTRL_DT))

    # ---- one control step
    def _event(self, tile, result, timing=None):
        ev = {'type': 'tile', 'id': int(tile.id), 'lane': int(tile.lane), 'result': result,
              'note_i': int(tile.note_i), 'song': self.game.song['id']}
        if timing is not None:
            ev['timing'] = round(float(timing), 3)
        self.events.append(ev)

    def drain_events(self):
        ev, self.events = self.events, []
        return ev

    def _sweet_dist(self):
        """Distance from the cursor to the lit button's sweet spot (0 inside), or None while holding."""
        tile = self.game.active_tile()
        if self.hold != 0 or tile is None:
            return None
        c, h = self.game.sweet_rect(tile.lane)
        dx = max(abs(self.cursor[0] - c[0]) - h[0], 0.0)
        dy = max(abs(self.cursor[1] - c[1]) - h[1], 0.0)
        return float(np.hypot(dx, dy))

    def step(self, action):
        g = self.game
        now = (self.t + 1) * CTRL_DT          # tile positions at the end of this step, when clicks are judged
        r_tiles = 0.0
        while not g.over() and g.passed(g.active_tile(), now):
            tile = g.resolve('miss', now)     # passed the hit band untapped
            self.timeouts += 1
            r_tiles += R_MISS
            self._event(tile, 'miss')
        self._aim(now)
        if self.hold == 0:
            self.prev_cd = self._cursor_dist()
            self.t_target = min(self.t_target, T_CLAMP)
        sweet0 = self._sweet_dist()
        # a tap is a press the steering network asked for (TilesEnv.tap_allowed): any other lever push is not a click
        gate = getattr(self, 'tap_gate', None)
        blocked = gate is not None and not gate()
        armed = self.armed
        if blocked:
            self.armed = False
        active = g.active_tile()                  # the tile a hit this step taps
        obs, r, _done, info = super().step(action)
        if blocked:
            self.armed = armed or self.armed      # unchanged, or re-armed if the lever came back up
        sweet1 = self._sweet_dist()
        if sweet0 is not None and sweet1 is not None:
            r_tiles += R_SWEET * (sweet0 - sweet1)
        click = info.get('click')
        if click is not None:
            if click[3]:                          # on_target(): in its lane, on time (CursorEnv paid +50 and waits)
                timing = g.timing(active, now)
                r_tiles += hit_reward(timing, g.window_time()) - R_HIT    # CursorEnv paid R_HIT
                self.timing_abs.append(abs(timing))
                tile = g.resolve('hit', now)
                self._event(tile, 'hit', timing)
                self._aim(now)
            else:                                 # anything else (CursorEnv charged its -3, or -2 during a hold)
                tile = g.active_tile() or g.tiles[-1]
                self.events.append({'type': 'tile', 'id': int(tile.id), 'lane': lane_of(click[1]),
                                    'result': 'wrong', 'note_i': int(tile.note_i), 'song': g.song['id']})
        fell_now = bool(info['fell'])             # TilesEnv puts the rat back on its feet; the song goes on
        self.falls += fell_now
        done = g.over() or self.timeouts >= self.max_misses_now or self.t * CTRL_DT >= MAX_EP_S
        info['fell_now'] = fell_now
        info['falls'] = self.falls
        info['fell'] = self.falls > 0 if done else fell_now     # at the end: fell at least once this episode
        info['level'] = g.difficulty
        info['tiles_left'] = g.remaining()
        if done:
            info['timing_abs'] = float(np.mean(self.timing_abs)) if self.timing_abs else float('nan')
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
                'lanes': LANES, 'hit_y': HIT_Y, 'window': round(g.w, 4),
                'cursor': [round(float(self.cursor[0]), 4), round(float(self.cursor[1]), 4)],
                'pressing': bool(self.pressing()) if hasattr(self, 'pressing') else False,
                'tiles': [[int(t.id), int(t.lane), round(float(y), 4), round(2 * g.hh, 4), t.state]
                          for t, y in g.on_screen(now)],
                'note_i': int(tile.note_i) if tile is not None else len(g.song['notes'])}


# ---------------------------------------------------------------------------------------------------- the brain's env
class TilesEnv(SteerEnv):
    """SteerEnv (steering network + lever-press network) on the Rat Tiles board. The observation is SteerEnv's 21
    features followed by TILE_DIM + TIMING_DIM tile features."""

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
        self.e.pressing = lambda: self.program > 0            # for the board snapshot: a press program is running
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
        f = np.zeros(TILE_DIM + TIMING_DIM, np.float32)
        g = getattr(e, 'game', None)
        if g is None or not hasattr(e, 'cursor'):
            return f
        now = e.t * CTRL_DT
        cx, cy = float(e.cursor[0]), float(e.cursor[1])
        band_top, band_bot = HIT_Y - g.w, HIT_Y + g.w
        tile = g.active_tile()
        # TILE_DIM: rule v1's block, measured at the hit line (v1 measured at the cursor's row and the screen bottom)
        if tile is not None:
            y = g.y(tile, now)                                        # -inf until it appears (the clips hold)
            top, bot = y - g.hh, y + g.hh
            f[0] = g.v * 4.0                                          # tile speed
            f[1] = np.clip((band_bot - top) / g.v, 0.0, 10.0) / 5.0  # s until it leaves the hit band (a miss)
            f[2] = np.clip((HIT_Y - top) / g.v, -2.0, 4.0) / 2.0     # s until its top edge passes the hit line
            f[3] = np.clip((HIT_Y - bot) / g.v, -4.0, 4.0) / 2.0     # s until its bottom edge reaches the hit line
            f[4] = float(lane_of(cx) == tile.lane)                    # cursor in its lane
            nxt = g.next_tile()
            if nxt is not None:
                ny = max(g.y(nxt, now), -g.hh - ABOVE)
                f[5] = np.clip((lane_x(nxt.lane) - cx) * 3.0, -3.0, 3.0)
                f[6] = np.clip((ny - cy) * 3.0, -3.0, 3.0)
                f[7] = 1.0
        f[8] = min(g.remaining(), EP_TILES) / EP_TILES
        # TIMING_DIM (rule v2)
        k = TILE_DIM
        if tile is not None:
            ahead = (HIT_Y - g.y(tile, now)) / g.v                        # s until it is perfect (< 0: late)
            f[k] = np.clip(ahead, -2.0, 6.0) / 2.0
            f[k + 1] = np.clip(ahead, -0.5, 0.5) * 4.0                    # the same, fine: a control step is 0.08
        f[k + 2] = min(g.window_time(), 4.0) / 2.0                        # half the timing window (s)
        lanes = np.full((LANES, 2), LANE_T_MAX)                           # per lane: s until enter / leave the band
        seen = 0
        for t in g.tiles[g.active:]:                                      # untapped, lowest first
            if seen == LANES or t.show is None or t.show > now:
                break                                                     # later tiles are not on screen either
            if lanes[t.lane, 1] < LANE_T_MAX:
                continue                                                  # a lower tile in this lane came first
            y = g.y(t, now)
            lanes[t.lane] = ((band_top - (y + g.hh)) / g.v, (band_bot - (y - g.hh)) / g.v)
            lanes[t.lane] = np.clip(lanes[t.lane], 0.0, LANE_T_MAX - 1e-6)
            seen += 1
        f[k + 3:k + 3 + 2 * LANES] = lanes.reshape(-1) / (LANE_T_MAX / 2.0)
        f[k + 11] = cx * 2.0 - 1.0
        f[k + 12] = cy * 2.0 - 1.0
        return f

    def _obs(self):
        return np.concatenate([super()._obs(), self._tile_obs()]).astype(np.float32)

    def tap_allowed(self):
        """A click counts only as the first click of a press program (see "press" above)."""
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
    assert [lane_of(x) for x in (0.0, 0.2499, 0.25, 0.74, 0.999, 1.0)] == [0, 0, 1, 2, 3, 3]
    # the game alone: at the easy level a tile appears whole at the top, is perfect on the hit line, overlaps the hit
    # band for +- window_time() around that, then counts as passed; at the hard level every tile is scheduled at once
    # and slides in from the top edge
    g = TileGame(songs[0], 0, 3, 0.0, 0, 0.0)
    t0 = g.tiles[0]
    assert abs(g.y(t0, LEAD_IN) - g.hh) < 1e-9 and g.tiles[1].show is None
    assert g.y(t0, LEAD_IN - 0.01) == -np.inf and not g.in_window(t0, LEAD_IN - 0.01) and not g.passed(t0, 0.0)
    perfect = LEAD_IN + (HIT_Y - g.hh) / g.v
    wt = g.window_time()
    assert abs(g.timing(t0, perfect)) < 1e-9 and abs(g.timing(t0, perfect + 0.5) - 0.5) < 1e-9
    assert g.in_window(t0, perfect - wt + 1e-6) and g.in_window(t0, perfect + wt - 1e-6)
    assert not g.in_window(t0, perfect - wt - 1e-6) and not g.passed(t0, perfect - wt - 1e-6)
    assert not g.in_window(t0, perfect + wt + 1e-6) and g.passed(t0, perfect + wt + 1e-6)
    assert abs(g.perfect_time(t0) - perfect) < 1e-9
    g.resolve('hit', perfect - 1.0)               # tapped early: the next tile appears ONE_GAP after the perfect moment
    assert abs(g.tiles[1].show - (perfect + ONE_GAP)) < 1e-9 and abs(g.y(g.tiles[1], g.tiles[1].show) - g.hh) < 1e-9
    g.resolve('miss', 30.0)                       # resolved late: ONE_GAP after that
    assert g.tiles[2].show == 30.0 + ONE_GAP
    # a hit's reward: R_HIT + R_TIMING when perfect; early, the same discounted back: waiting for perfect always pays
    assert abs(hit_reward(0.0, 1.0) - (R_HIT + R_TIMING)) < 1e-9
    assert abs(hit_reward(0.5, 1.0) - (R_HIT + R_TIMING / 2)) < 1e-9
    assert abs(hit_reward(-0.5, 1.0) - (R_HIT + R_TIMING / 2) * GAMMA ** 25) < 1e-9
    for w in (0.23, 0.55, 1.35):                  # a step later (still early) is worth more, discounted to now
        for k0 in range(1, int(w / CTRL_DT)):
            now_v = hit_reward(-k0 * CTRL_DT, w)
            assert GAMMA * hit_reward(-(k0 - 1) * CTRL_DT, w) > now_v, (w, k0)
    g2 = TileGame(songs[0], 0, 3, 1.0, 0, 0.0)
    assert all(t.show is not None for t in g2.tiles) and g2.tiles[1].spawn > g2.tiles[0].spawn
    assert g2.y(g2.tiles[0], LEAD_IN) == -g2.hh
    # the tiles never overlap the hit band two at a time, at any level (so "the lowest tile" is the one on the line)
    for d in np.linspace(0.15, 1.0, 18):
        lv = level(d)
        assert lv['speed'] * lv['beat_s'] > 2 * (lv['hh'] + lv['window']), d
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
    # rule v2's hit test (CursorEnv's on_target): the tile's lane, on time; the cursor's y does not count
    e = env.e
    env.difficulty = 0.0
    env.reset()
    g = e.game
    tile = g.active_tile()
    perfect = LEAD_IN + (HIT_Y - g.hh) / g.v
    t_keep = e.t
    for dt_s, lane, y, want in ((0.0, tile.lane, HIT_Y, True), (0.0, tile.lane, 0.1, True),
                                (0.0, (tile.lane + 1) % LANES, HIT_Y, False),
                                (-g.window_time() - 0.05, tile.lane, HIT_Y, False),
                                (g.window_time() - 0.05, tile.lane, HIT_Y, True)):
        e.t = int(round((perfect + dt_s) / CTRL_DT))
        e.cursor = np.array([lane_x(lane), y])
        assert e.on_target() == want, (dt_s, lane, y)
    e.t = t_keep
    # the timing features 2 s before perfect: 2 s to perfect; the lowest tile's lane enters the band 2 - window_time()
    # s from now and leaves it 2 + window_time() s from now; the other lanes read "none" (one tile at a time here)
    e.t = int(round((perfect - 2.0) / CTRL_DT))
    obs = env._obs()
    k = STEER_DIM + TILE_DIM
    lanes = obs[k + 3:k + 11].reshape(LANES, 2) * (LANE_T_MAX / 2.0)
    wt = g.window_time()
    ahead = perfect - e.t * CTRL_DT                           # 2 s, to the control step
    assert abs(obs[k] * 2.0 - ahead) < 1e-4 and abs(obs[k + 1] - 2.0) < 1e-6 and abs(obs[k + 2] * 2.0 - wt) < 1e-4
    assert np.allclose(lanes[tile.lane], [ahead - wt, ahead + wt], atol=1e-4), lanes
    assert np.all(lanes[np.arange(LANES) != tile.lane] >= LANE_T_MAX - 1e-4), lanes
    # the cue's "on target" flag is the cursor on the lit button, on time or not; the hit test is lane + band
    e._aim(e.t * CTRL_DT)
    e.cursor = np.array([lane_x(tile.lane), HIT_Y])
    assert e._cue()[8] == 1.0 and not e.on_target()           # 2 s early: on the button, a press would be wrong
    e.t = int(round((perfect - 0.1) / CTRL_DT))
    obs = env._obs()
    assert e._cue()[8] == 1.0 and e.on_target() and abs(obs[k + 1] - 4.0 * (perfect - e.t * CTRL_DT)) < 1e-4
    e.cursor = np.array([lane_x(tile.lane), 0.2])
    assert e._cue()[8] == 0.0 and e.on_target()               # off the button in y: still a hit
    e.t = t_keep
    snap = e.snapshot(0.0)
    assert snap['hit_y'] == HIT_Y and snap['window'] == round(g.w, 4) and snap['pressing'] is False
    json.dumps(snap)
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
    # each press followed by a new trial, every hit on time and in order, with its timing (it presses as soon as its
    # "on target" cue lights up, i.e. as the tile enters the hit band: early, about -window_time())
    steer = os.path.join(HERE, 'runs', 'final', 'steer.pt')
    if os.path.exists(steer):
        pol = NumpyPolicy(load(steer), OBS_DIM)
        env.difficulty = 0.0
        obs, done, trials, events = env.reset(), False, 0, []
        while not done:
            obs, r, done, info = env.step(pol(obs).astype(np.float64))
            trials += info['new_trial']
            events += env.drain_events()
        hits = [x for x in events if x['result'] == 'hit']
        g = env.e.game
        print(f'steering network at difficulty 0: {info["hits"]} of {len(g.tiles)} tapped, {info["misses"]} wrong, '
              f'{trials} trials, timing {[x["timing"] for x in hits]}')
        assert info['hits'] >= 4 and trials >= info['hits'] - 1 and len(hits) == info['hits']   # the last ends it
        assert [x['note_i'] for x in hits] == sorted(x['note_i'] for x in hits)
        assert all(abs(x['timing']) <= g.window_time() + CTRL_DT for x in hits)
        assert np.isfinite(info['timing_abs'])
    print('tiles_env selftest passed')


def _eval_worker(args):
    policy, difficulty, seed, full = args
    env = TilesEnv(seed, full_songs=full)
    pol = NumpyPolicy(load(policy), env.obs_dim)
    env.difficulty = difficulty
    obs = env.reset()
    done, ret, info, timing = False, 0.0, {}, []
    while not done:
        obs, r, done, info = env.step(pol(obs).astype(np.float64))
        ret += r
        timing += [x['timing'] for x in env.drain_events() if x['result'] == 'hit']
    return {'seed': seed, 'ret': ret, 'hits': info['hits'], 'missed': info['timeouts'], 'wrong': info['misses'],
            'fell': bool(info['fell']), 'falls': int(info['falls']), 'tiles': info['hits'] + info['timeouts'],
            'steps': env.e.t, 'song': env.e.game.song['id'], 'timing': timing,
            'window_s': env.e.game.window_time()}


def _eval(a):
    from multiprocessing import Pool
    jobs = [(a.eval, a.difficulty, a.seed + i, a.full_songs) for i in range(a.episodes)]
    with Pool(a.procs) as p:
        res = p.map(_eval_worker, jobs)
    hits = sum(r['hits'] for r in res)
    tiles = sum(r['tiles'] for r in res)
    tm = np.array([x for r in res for x in r['timing']], float)
    wt = res[0]['window_s']
    lv = level(a.difficulty)
    out = {'policy': a.eval, 'difficulty': a.difficulty, 'speed': round(lv['speed'], 3),
           'window': round(lv['window'], 3), 'window_s': round(wt, 3), 'episodes': len(res),
           'hits_per_episode': round(hits / len(res), 3),
           'hit_rate': round(hits / max(tiles, 1), 3),
           'missed_per_episode': round(sum(r['missed'] for r in res) / len(res), 3),
           'wrong_per_episode': round(sum(r['wrong'] for r in res) / len(res), 3),
           # hit_rate as the buyback engine counts it (hits / (hits + misses + wrong))
           'hit_rate_all': round(hits / max(tiles + sum(r['wrong'] for r in res), 1), 3),
           'timing_abs_ms': round(1000 * float(np.mean(np.abs(tm))), 1) if len(tm) else None,
           'timing_mean_ms': round(1000 * float(np.mean(tm)), 1) if len(tm) else None,
           'timing_bonus': round(float(np.mean(1 - np.abs(tm) / wt)), 3) if len(tm) else None,
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
