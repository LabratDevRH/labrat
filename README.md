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
  and `LIVE_ORIGINS` there). One publisher in, many viewers out.
- `live/publish_training.py`: runs next to training on the training PC. It plays the run's latest saved checkpoint
  in its own simulation at 25 fps and streams it, with every new `log.jsonl` row, to the relay. It says bye when
  `log.jsonl` has been silent for 90 s, and the site goes back to the replay.
- `trainer/`: training and the publisher together in one container on Railway, so the PC is not needed. A job is
  started and stopped by setting variables on the service; runs live on a Railway volume (see `trainer/README.md`).

Local preview (PowerShell, two shells, the same token in both):

```
$env:LABRAT_PUBLISH_TOKEN = '<a random token, 16+ characters>'
$env:SERVE_SITE = '1'; cd relay; uvicorn relay:app --host 127.0.0.1 --port 4720 --ws-max-size 131072 --ws-per-message-deflate false
# open http://localhost:4720/

$env:LABRAT_PUBLISH_TOKEN = '<the same token>'
python live/publish_training.py --watch runs --relay ws://localhost:4720/publish
python train.py --task steer --name <new run> --curriculum ...        # any training run under runs/
```

During real training: `python live/publish_training.py --watch runs --relay
wss://labrat-relay-production.up.railway.app/publish` (token from `LABRAT_PUBLISH_TOKEN`), then train as usual.
`live/start_live_feed.ps1 [-Python <path to python.exe>]` does the same in the background, reading the token from
`.env` and logging to `runs/publisher.log`; it idles until a run's `log.jsonl` is being written.

## Rat buybacks (built, DRY only)

`live/buyback.py`: every target the rat hits in the live view (the newest saved training checkpoint, playing in its own
simulation) adds 0.00001 ETH to a batched buyback of $LABRAT, paid only from the claimed creator fees, until the hourly,
daily or total caps are reached; hits over the caps are counted but add nothing. Only the cursor and steering tasks
count (they have a lit target). The code sets that rule; the rat does not understand money. It is **DRY**: it counts
the hits from the relay and simulates each fee claim and buy with `eth_call` on the real chain. It reads no `.env`,
signs nothing and sends nothing. The LIVE path is gated like the launcher (plus a fixed journal place and a nonce
check against the chain) and has not been used. The site panel is off (`site/buyback.js`). Notes: `live/BUYBACK.md`;
tests: `python live/buyback_test.py`.

## Also in here

- `launch_run.py`, `rollout.py`, `replay.py`: the first version, where a single lever press launched the coin
- `blender/film.py`, `export_anim.py`, `compose.py`, `finalize.py`, `preview.py`: a film pipeline for recorded
  lever runs
- `live/rig.py`, `live/web/rig.html`: the earlier rig, with the 3D Skinner box on the page

Credits: DeepMind's rodent model (dm_control, Apache-2.0), MuJoCo, three.js (MIT, pinned by sha384), Playwright.
