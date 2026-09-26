# labrat

A virtual rat's trained brain launches a coin on the real pons launchpad (Robinhood Chain). The rat's head
steers the mouse cursor and its lever press is the click, for all 11 steps of the launch flow.

**It is not a real rat brain.** It is two trained artificial neural networks driving a simulated rat body.

## $LABRAT

On 2026-09-25 the rat launched **Labrat ($LABRAT)** on pons:

- contract [`0xaCa07FE3BC5fF3e7501cA1dCCFCd937fD710680d`](https://robinhoodchain.blockscout.com/token/0xaCa07FE3BC5fF3e7501cA1dCCFCd937fD710680d)
  · [on pons](https://www.ponsfamily.com/launchpad/0xaCa07FE3BC5fF3e7501cA1dCCFCd937fD710680d)
- tx [`0x0f618ed9…1d26`](https://robinhoodchain.blockscout.com/tx/0x0f618ed9fa31c8ba5d224a620f514d3ff03504c7943a9e3ec937e316ea021d26), block 71684103
- 11 of 11 targets clicked by the rat, 0 misses; the transaction passed 17 of 17 checks before it was signed once
- the recorded session is in `runs/brainrig_20260924T211408Z_seed2026/`
- website: **https://lab-rat.net**

## The subject

- **Body:** DeepMind's rodent model (`dm_control/locomotion/walkers/assets/rodent.xml`, Apache-2.0), the rat
  from Aldarondo et al., *Nature* 2024. 67 joints, 38 actuators, 0.34 kg, simulated in MuJoCo with 2 ms physics
  steps, in a walled operant box with a spring lever (`build_scene.py`, `assets/`).
- **Brain:** two networks trained with PPO (`train.py`), 2,312 units and 643,072 connections in total:
  - the **lever-press network** (`runs/final/policy.pt`, 200 → 512 → 512 → 256 → 38) presses the lever with the
    whole body and stays standing. `env.py` only counts a clean press: a paw or finger on the lever past 11.5°, no
    head or trunk on the lever, upright. It scored 800 of 800 across four test suites (`eval.py`,
    `runs/final/selected.json`).
  - the **steering network** (`runs/final/steer.pt`, 21 → 256 → 256 → 256 → 5) turns the head (4 neck actuators)
    and decides when to press (`steer_env.py`).
- **Cursor:** the head's direction relative to the torso moves the cursor, and a lever press clicks
  (`cursor_env.py`). Between steps the rat rests in its standing pose.

## The rig

`live/brainrig.py` opens the real `ponsfamily.com/launchpad` create page in Playwright Chromium with an injected
wallet and runs the rat's brain in real time next to it:

- it lights the next of 11 targets (like a cue light), scrolls it into view, and waits for the rat
- a click outside the lit target is ignored (never forwarded to the page)
- after the rat clicks into a field, the rig types that field's text
- the page (`live/web/brain.html`, "Operant Lab") streams pons and both networks' activity at 25 Hz, with a
  rotating display model of the rat's body (`live/web/rat3d.js`; a model, not a live view of its pose)

The 11 targets: Terms of Use, Privacy Policy, continue, image, name, ticker, description, advanced, creator tax,
Launch, Confirm.

**DRY (the default):** pons builds the launch transaction, the rig decodes it, runs every check and refuses to
sign. `.env` is never read.

**LIVE** needs all of these:
- the `.env` **file** says `RATBRAIN_LIVE=1` and holds `RATBRAIN_RH_KEY`
- `--live --confirm <SYMBOL>`
- preflight passes: a fresh wallet (nonce 0), balance ≥ 1.5 × (gas + the 0.0005 ETH fee), the launch simulates
- no launch journal exists
- the owner opens the printed `http://localhost:4665/?token=…` URL, clicks BEGIN SESSION and then Start in the
  confirm dialog. Nothing else can start a LIVE session.

Before signing, the transaction must match exactly: the factory, the selector, the fee, our wallet as creator,
the name, symbol and description, the tax, no developer buy. It is signed once at nonce 0, and the signed bytes
are journalled before broadcast. STOP, closing the page or Ctrl+C abort the run until the transaction reaches
the signer. `python launcher.py --resolve` finishes an interrupted launch by re-broadcasting the same bytes.

## The proof

Before the run, a sha256 **brain commit** over `scene.xml`, both networks and the code files (`env.py`,
`cursor_env.py`, `steer_env.py`, `session.py`, `ptload.py`) is typed into the coin's description. The Labrat
description ends in `brain sha256 9778ea092a293940497dc94e98b62328b6e11fad8d0bf634a04124329a8dcec8`.

Every session saves its command log, actions and every physics frame. `python replay_session.py <run dir>`
re-runs the brain and prints MATCH only if everything reproduces bit for bit.

Honest scope: the recording machine reproduces sessions exactly. A different CPU, BLAS or numpy build can
round the networks' float32 maths differently. `session.json` records the versions used (Python 3.14.2,
numpy 2.4.2, MuJoCo 3.13.0, Windows 11).

## Run it

```
python live/brainrig.py                                  # DRY, then open http://localhost:4665
python live/record.py --dry --hq --port 4665             # record a DRY run to build/recordings/
python replay_session.py runs/brainrig_20260924T211408Z_seed2026
```

Needs: mujoco, numpy, fastapi, uvicorn, websockets, playwright (Chromium), eth-account, eth-abi, requests,
Pillow, imageio. torch is only needed for training.

## The website and its live view

Live at **https://lab-rat.net** (site on Vercel, relay on Railway at `labrat-relay-production.up.railway.app`).

- `site/`: the static website (deployable to Vercel as is). Its 3D view plays a replay of a recorded launch session,
  a DRY rehearsal of the Labrat launch (`site/replay/session.bin` + `session.json`, made by
  `python live/export_replay.py --run runs/<brainrig run> --label "<public label>"`, which refuses to write it
  unless the re-run prints MATCH, and refuses a json with any address, fee or wallet in it), shows STANDBY while
  there is no clip, and switches to **LIVE TRAINING** only while a training run is being streamed. The coin card
  reads `site/coin.js`: `window.LABRAT_COIN = null` shows "Launching soon"; set it after the launch.
- `relay/relay.py`: the relay (deployable to Railway; see `relay/README.md`, and set both `LABRAT_PUBLISH_TOKEN`
  and `LIVE_ORIGINS` there). One training publisher in, many viewers out, plus a second channel
  (`/publish?channel=pons`) for the buy rig: the rat clicking through each buyback on the real pons page, as masked
  JPEG frames and step messages. The site's **Rat on pons** panel (next to Rat buybacks) shows that stream while a
  session runs, and the last session's final frame and result between sessions, every buy labelled Simulated.
- `live/publish_training.py`: runs next to training on the training PC. It plays the run's latest saved checkpoint
  in its own simulation at 25 fps and streams it, with every new `log.jsonl` row, to the relay. It says bye when
  `log.jsonl` has been silent for 90 s, and the site goes back to the replay.
- `trainer/`: training and the publisher together in one container on Railway, so the PC is not needed. A job is
  started and stopped by setting variables on the service; runs live on a Railway volume (see `trainer/README.md`).

Local preview (PowerShell, two shells, the same token in both):

```
$env:LABRAT_PUBLISH_TOKEN = '<a random token, 16+ characters>'
$env:SERVE_SITE = '1'; cd relay; uvicorn relay:app --host 127.0.0.1 --port 4720 --ws-max-size 266240 --ws-per-message-deflate false
# open http://localhost:4720/

$env:LABRAT_PUBLISH_TOKEN = '<the same token>'
python live/publish_training.py --watch runs --relay ws://localhost:4720/publish
python train.py --task steer --name <new run> --curriculum ...        # any training run under runs/
```

During real training: `python live/publish_training.py --watch runs --relay
wss://labrat-relay-production.up.railway.app/publish` (token from `LABRAT_PUBLISH_TOKEN`), then train as usual.
`live/start_live_feed.ps1 [-Python <path to python.exe>]` does the same in the background, reading the token from
`.env` and logging to `runs/publisher.log`; it idles until a run's `log.jsonl` is being written.

## Rat Tiles

`tiles_env.py`: our own falling-tiles rhythm game, played by the same two-network brain. The rat's screen has 4
lanes; one black tile per note of a public-domain melody (`assets/songs.json`, identical to
`site/assets/songs.json`: Ode to Joy, Twinkle Twinkle Little Star, Frère Jacques, the opening of Für Elise) slides
down its lane toward a red button on a hit line near the bottom. The rat turns its head to move between the lanes
and presses the lever as the tile reaches the button; each tap plays that tile's note, so the tiles in order spell
the melody (a note's lane comes from its pitch, see `tiles_env.song_lanes`).

- **Rules (game rule v2):** a press is a hit only if the cursor is in the lowest tile's lane and that tile overlaps
  the hit band (the hit line ± the level's window) when the click registers: +50 (as a target click in the cursor
  task) plus a timing bonus of up to +40, `40 × (1 − |timing| / half the time the tile overlaps the band)`, where
  timing is how many seconds early or late the tile's centre was on the hit line (with +10, 0.84M steps of training
  halved the wrong presses but did not move the timing at all). The hit and its bonus are valued at
  the moment the tile is centred: a press that lands early is paid that value discounted back to the click with
  training's discount (0.99 per 20 ms step), and one-at-a-time tiles appear a fixed time after the previous tile's
  centred moment. Without that, being paid up to 1.35 s sooner outweighed the bonus, and the best policy was to press
  as the tile entered the band; with it, pressing early gains nothing. Any other press is wrong (−3): the
  wrong lane, or nothing on the band in that lane yet. A tile that passes the band untapped is a miss (−10, its note
  is skipped). The click lands 0.12–0.16 s after the steering network asks for a press (measured: every press
  program clicked on its 7th to 9th control step), and the cursor is frozen meanwhile, so the rat has to press a
  little before the tile is centred. A tap is a press the steering network asked for: only the first click of each
  press counts. Each press is a trial, as each step is in the launch rig: when the lever-press network hands the
  body back, the rat is put back in its standing start pose (without that, a paw left on the lever froze the cursor
  and kept the lever from re-arming). A fall costs 40 and the rat is put back on its feet the same way; the song
  goes on. Until a tile is on screen the brain rests, as during the rig's holds. The steering network's head-down
  commands are cut while the head is pitched far down (most falls in the cursor tasks were the head driven into the
  floor); head up and sideways stay free. A small shaping reward leads the cursor to the middle of the lit button.
- **What the steering network sees:** the red button of the lowest tile's lane as the lit target (the cursor task's
  cue; its "on target" input says the cursor is on that button, not whether a press now would be on time), plus 22
  inputs appended to its observation: the tiles' speed, when the lowest tile reaches, passes and leaves the hit line,
  when it is perfect (coarse, and fine within ±0.5 s), whether the cursor is in its lane, where the next tile is,
  tiles left, half the timing window in seconds, for each lane when its lowest tile on screen enters and leaves the
  hit band, and the cursor's position. `train.py --resume` widens the 21-input steering network (or a 30-input rule
  v1 tiles network) to 43 inputs with zero weights on the new ones, so it starts out playing exactly as it did. (A
  first version lit the "on target" input only while a press would hit: the networks pressed the moment it lit, as
  the tile entered the band, and training did not change that. With the input on the button, an early press is a
  wrong press, and the timing inputs are what tell the rat when to press.)
- **Curriculum** (`--curriculum`): from one slow, tall tile at a time with a wide hit band (0.2 screen heights a
  second, a band 0.24 tall: on time within ±1.35 s) to a column scrolling in on the song's rhythm (0.5 a second,
  1.7 s a beat, a band 0.06 tall: ±0.23 s), stepped up when the rat taps more than 75% of the tiles at its level
  (twice as fast over 95%) and down under 45%. A checkpoint of another rule version starts the curriculum at 0.
- **Training** plays 8-note phrases of random songs; the **live view** plays whole songs in turn (one episode per
  song) and also streams the board (`tiles`, up to 10 a second, with the hit line `hit_y`, the band's `window` and
  `pressing`) and every tile's outcome (`tile`, with `timing` for a hit); see `live/publish_training.py`. A tile
  tapped counts as a hit for the buybacks, like a target hit in the steering task.
- **Measured** (`runs/tiles_v2_sanity`, a local sanity run under rule v2, resumed from the rule v1 sanity network
  `runs/tiles_sanity/policy_final.pt`: +2.0M steps with 6 × 6 envs, about 16 minutes on a 12-thread PC; its
  `log.jsonl` is kept, as is the rule v1 run's): the curriculum held at 0 for the first 0.66M steps while the rat
  unlearned pressing as soon as its cursor reached the button (wrong presses per 8-tile phrase 18.3 → 5.6), then
  climbed to 0.65 (0.4 screen heights a second, on time within ±0.43 s) by the end; the tile rate never fell below
  98%. With mean actions (`tiles_env.py --eval`, 24 phrases each), the network it started from → the trained one:
  - at 0.65: hits per phrase 7.42 → 8.00, missed 0.58 → 0, wrong presses 11.96 → 0, |timing| 214 → 137 ms (the
    buyback engine's hit rate, hits / (hits + misses + wrong), 0.37 → 1.00); whole songs at that level
    (`--full-songs`, 8 plays of Twinkle, Twinkle, 42 notes): 36.75 → 42 hits, 59 → 0 wrong presses, |timing|
    225 → 134 ms.
  - at 0 and 0.3 the wrong presses fell from 20.0 → 0 and 16.9 → 1.9 per phrase, but the hits there still land early
    in the wide bands (−840 ms and −368 ms): the timing is learnt at the level being trained.
  - at 1.0 (not reached yet) the trained network taps 0.8 of 8 tiles, late (the one it started from, 2.4); the live
    job goes on from 0.65. `trainer/tiles_v2_start.pt` is this network (see `trainer/README.md`).

```
python train.py --task tiles --name tiles_v2_sanity --resume runs/tiles_sanity/policy_final.pt --steps 4502144 --workers 6 --envs-per-worker 6 --curriculum
python tiles_env.py --eval runs/tiles_v2_sanity/policy_final.pt --difficulty 0.65     # mean actions, 24 phrases
```

## Rat Maze

`maze_env.py`: our own maze-escape game, played by the same two-network brain (a separate game: Rat Tiles is
unchanged). The rat's screen shows a top-down maze, 3x3 to 8x8 cells, with the rat as a marker inside it and cheese in
an exit cell on the border. The marker moves the way the cursor already moves: the head's direction relative to the
body is its velocity (`cursor_env`'s head-to-cursor mapping, 2.5 cells a second per screen width a second of cursor
speed); no lever press is needed, and a press has no role in the maze (the PRESS output is ignored). Walls stop the
marker (it slides along them); reaching the cheese is an escape and a new, harder maze appears; a maze not escaped in
its time limit (5 s plus 1.25 s per cell of the shortest path, at most 60 s) is a timeout.

- **What the network senses** (never the map): through the cursor task's cue, the cheese's direction and a rough
  distance (to half a cell), the cheese cell's size, the marker's velocity and the time left; plus 19 inputs appended
  to its observation: which of the 4 sides of its cell are open, where it is within the cell, its velocity in cells,
  the cheese's direction and rough distance in cells, the grid size, a short memory (how recently each open neighbour
  cell was visited within the last 40 cells, and whether this cell was visited before), whether it is pushing a wall,
  and whether the cheese is in this cell or an open neighbour. `train.py --resume` widens the 21-input steering
  network to 40 inputs with zero weights on the new ones.
- **Reward** (chosen, disclosed): +100 at the cheese, −10 at the time limit, potential-based shaping of 6 per cell of
  the BFS distance to the cheese (plus the distance to the next cell's centre, so it is one function of position and
  no loop can farm it), −0.1 per control step, −0.05 per step pushing a wall and −1 per bump, −3 per dead end
  entered, −0.5 per cell re-entered within the memory, the cursor task's body terms, and −40 per fall (the rat is set
  back on its feet and the maze goes on; the Rat Tiles head-down guard is reused).
- **Curriculum** (`--curriculum`): mazes are generated by a seeded recursive backtracker (always solvable) with a
  fraction of the remaining interior walls removed: from a 3x3 open room at difficulty 0 to an 8x8 perfect maze full
  of dead ends at 1, stepped up when the rat escapes more than 75% of its mazes at the level (twice as fast over 95%)
  and down under 45%.
- **Training** plays one maze per episode; the **live view** plays courses of 4 mazes, each one notch harder, and also
  streams the maze (`maze`, up to 8 a second, the layout in the first snapshot of each maze) and every maze's outcome
  (`maze_end`); see `live/publish_training.py`. An escape counts as a hit for the buybacks and a timeout as a miss.
- **Measured** (`runs/maze_sanity`, a local sanity run: `runs/final/steer.pt` resumed for 1.5M steps with 6 × 8 envs
  and `--curriculum`, about 12 minutes at 2,080 steps a second on a 12-thread PC; its `log.jsonl` is kept): the
  steering network escapes every 3x3 room from the start (it steers straight at the cheese), so the curriculum climbed
  to 0.65 in the first 0.1M steps, fell back to 0.45 once walls were in the way, and ended at 0.75 (7x7 mazes, a
  quarter of the extra walls removed). With mean actions (`maze_env.py --eval`, 24 mazes each), the network it
  started from → the trained one (`policy_final.pt`, also `trainer/maze_v1_start.pt`):
  - mazes escaped: 0.71 → 0.96 at 0.3 (5x5), 0.17 → 0.63 at 0.5 (6x6), 0.17 → 0.54 at 0.6, 0.08 → 0.29 at 0.75
    (7x7); 0 → 0 at 1.0 (8x8 perfect mazes, not reached yet)
  - seconds per maze, a timeout counted at its limit: 5.2 → 3.3 at 0.3, 12.9 → 8.2 at 0.5, 13.7 → 9.9 at 0.6,
    17.8 → 16.2 at 0.75 (the mean time of the escapes alone rises, 2.2 → 2.9 s at 0.3, because the trained network
    also escapes the mazes the first one never did)
  - what it learnt: the first network sits pushing into the wall between it and the cheese (0.04 cells re-entered per
    maze); the trained one backtracks and explores (11 to 23 cells re-entered per maze at 0.5 to 0.75, 1 to 3 dead
    ends), slides along walls (8 to 17 bumps per maze, up from 1.5) and swings its head harder (falls in 4 to 8% of
    the mazes, up from 0). The live job goes on from 0.75.

```
python train.py --task maze --name maze_sanity --resume runs/final/steer.pt --steps 2652000 --workers 6 --curriculum
python maze_env.py --eval runs/maze_sanity/policy_final.pt --difficulty 0.6     # mean actions, 24 mazes
python maze_env.py --selftest
```

## Rat buybacks (built, DRY only)

`live/buyback.py`: every target the rat hits in the live view (the newest saved training checkpoint, playing in its own
simulation) adds 0.00001 ETH to a batched buyback of $LABRAT, paid only from the claimed creator fees, until the hourly,
daily or total caps are reached; hits over the caps are counted but add nothing. Only the cursor, steering and Rat
Tiles tasks count (they have a lit target; in Rat Tiles a hit is a tile tapped). The code sets that rule; the rat does
not understand money. It is **DRY**: it counts the hits from the relay and simulates each fee claim and buy with
`eth_call` on the real chain. It reads no `.env`, signs nothing and sends nothing. The LIVE path is gated like the launcher (plus a fixed journal place and a nonce
check against the chain) and has not been used. The site panel is off (`site/buyback.js`). Notes: `live/BUYBACK.md`;
tests: `python live/buyback_test.py`.

## Also in here

- `launch_run.py`, `rollout.py`, `replay.py`: the first version, where a single lever press launched the coin
- `blender/film.py`, `export_anim.py`, `compose.py`, `finalize.py`, `preview.py`: a film pipeline for recorded
  lever runs
- `live/rig.py`, `live/web/rig.html`: the earlier rig, with the 3D Skinner box on the page

Credits: DeepMind's rodent model (dm_control, Apache-2.0), MuJoCo, three.js (MIT, pinned by sha384), Playwright.
