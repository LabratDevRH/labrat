# labrat trainer (Railway)

Training and the website's live view without the owner's PC. One Railway service, `labrat-trainer`, in the same
project as the relay, runs `train.py` and `live/publish_training.py --watch runs` side by side, supervised by
`trainer/entry.py`. The site shows exactly what it showed before: the run's **latest saved checkpoint, playing in
its own simulation** (the publisher's own label), and LIVE TRAINING only while the run's `log.jsonl` is being
written.

```
labrat-trainer (Railway)                                   labrat-relay (Railway)      site (Vercel)
tini -> entry.py -> train.py + its workers   (CPU)
                 -> publish_training.py  --wss /publish-->  relay.py  --wss /live-->   LIVE TRAINING
/data (volume): runs/<name>/  runs/final/
```

## What is in the image

`trainer/Dockerfile` builds from the repo root. The root `.dockerignore` lets only these files into the build:
`train.py policy.py env.py cursor_env.py steer_env.py tiles_env.py maze_env.py ptload.py`, `assets/` (the scene, and
`songs.json`: Rat Tiles' melodies), `runs/final/policy.pt`, `runs/final/steer.pt`, `live/publish_training.py`,
`live/labrat_frame.py`, `live/assets/rat.json` and `trainer/` (with `trainer/tiles_v2_start.pt`, a Rat Tiles start,
and `trainer/maze_v2_start.pt`, a Rat Maze start, rule v2).
`.env`, `.env.*`, `launch_journal.json`, old runs, `site/`, `relay/`, `build/`, `shots/` and videos never reach it,
and the root `.railwayignore` keeps them out of the `railway up` upload as well (22 files, about 7.3 MB).

Python 3.13 (slim), the **CPU** build of torch, MuJoCo, numpy and websockets, all pinned in
`trainer/requirements.txt`. The build ends with `python trainer/entry.py --selftest`: the imports, both final
networks (read with ptload and loaded by torch the way `train.py --resume` loads them), the Rat Tiles and Rat Maze
start networks if the image has them (their game rules and input counts must match `tiles_env.py` / `maze_env.py`), the
songs, and a few steps of all five
environments (lever, cursor, steer, tiles, maze). A broken image fails the build instead of a job.

No wallet key is needed or present. The trainer has nothing to do with launching coins; if a variable that looks
like a key is set on the service, `entry.py` names it in the log (never its value) and passes it to nobody.
`LABRAT_PUBLISH_TOKEN` comes only from the service's variables and only the publisher gets it. Nothing prints it.

## The volume

Mount a volume at **`/data`**. On start `entry.py` creates `/data/runs`, copies the image's two final networks to
`/data/runs/final/` (steer_env loads `runs/final/policy.pt`; `TRAIN_RESUME=runs/final/steer.pt` resumes the
steering network), and makes `/app/runs` a link to `/data/runs`, where `train.py` writes. On Railway it refuses to
train when nothing is mounted at `/data`, because a redeploy would then lose the run and train it again from its
start (`TRAIN_ALLOW_EPHEMERAL=1` overrides that).

## Start, stop and resume a job

Every change to a service variable redeploys the service. That is how a job starts and stops.

| variable | |
|---|---|
| `TRAIN_TASK` | `lever`, `cursor`, `steer`, `tiles` (Rat Tiles, `tiles_env.py`) or `maze` (Rat Maze, `maze_env.py`). Empty or `none`: **idle**. Nothing runs and the publisher is not started |
| `TRAIN_NAME` | the run's name. Its files go to `/data/runs/<TRAIN_NAME>/`. Not `final` |
| `TRAIN_STEPS` | environment steps **this job** trains, for example `2000000` (or `2e6`, `2M`). They are added to the start checkpoint's own count, because `train.py --steps` is a running total |
| `TRAIN_RESUME` | optional: the checkpoint to start from, for example `runs/final/steer.pt` |
| `TRAIN_ARGS` | optional: more `train.py` flags, for example `--curriculum --init-std 0.3`. Not `--task --name --steps --resume --workers --envs-per-worker` (they come from the variables here) |
| `TRAIN_WORKERS` | simulation worker processes. Default: usable CPUs − 1. Set it to the service's vCPU limit − 1 if the log's "usable CPUs" line shows the host's cores instead of the limit |
| `TRAIN_ENVS_PER_WORKER` | optional, `train.py`'s default is 8 |
| `TRAIN_FORCE` | see "Restarts" |
| `TRAIN_RETRIES` | how often a `train.py` that crashed after saving a checkpoint is resumed (default 2) |
| `PUBLISH_GRACE_S` | seconds the publisher keeps running after training ends (default 120; it says bye 90 s after the last `log.jsonl` row) |
| `LABRAT_PUBLISH_TOKEN` | the relay's publish token. Unset: the job trains without the live view |
| `LABRAT_RELAY_PUBLISH` | default `wss://labrat-relay-production.up.railway.app/publish` |
| `LABRAT_RELAY_CHANNEL` | optional: the relay channel the publisher streams on (`publish_training.py --channel`). Unset: the default channel (the site's 3D view and Rat Tiles panel, counted by the buyback engine). `maze`: the relay's second training channel, read by the site's `/burn` page and the burn engine; Rat Maze runs only, so it needs `TRAIN_TASK=maze` (otherwise the publisher is not started and the log says why). See "A second trainer for Rat Maze" |

Example, a new steering run from the final steering network with the curriculum:

```
TRAIN_TASK=steer
TRAIN_NAME=steer_rw1
TRAIN_STEPS=2000000
TRAIN_RESUME=runs/final/steer.pt
TRAIN_ARGS=--curriculum
TRAIN_WORKERS=7
```

Rat Tiles (`tiles_env.py`, game rule v2: red buttons on a hit line, presses timed to the tiles) the same way. The
publisher then plays whole songs and sends the board and every tile's outcome. Start from the Rat Tiles network
trained locally under rule v2, which the image carries as `trainer/tiles_v2_start.pt` (the job goes on at the
curriculum difficulty saved in it):

```
TRAIN_TASK=tiles
TRAIN_NAME=tiles_v2_live
TRAIN_STEPS=3000000
TRAIN_RESUME=trainer/tiles_v2_start.pt
TRAIN_ARGS=--curriculum
```

or from the final steering network, `TRAIN_RESUME=runs/final/steer.pt` (its first layer is widened for the 22 tile
inputs and the curriculum starts at 0). Use a new `TRAIN_NAME`: a rule v1 tiles run (30 inputs) resumed after this
image is deployed would be widened and start its curriculum over in the same log.

Rat Maze (`maze_env.py`, game rule v2: the rat steers a marker through a maze to the cheese with its head; no lever
press) the same way. The publisher then plays courses of 4 mazes and sends the maze and every maze's outcome (`maze` /
`maze_end`), and the relay needs the maze protocol (relay/relay.py with `maze` in its TASKS). Start from the Rat Maze
network trained locally under rule v2, which the image carries as `trainer/maze_v2_start.pt` (the job goes on at the
curriculum difficulty saved in it, 0.425: 5x5 mazes, and climbs by itself):

```
TRAIN_TASK=maze
TRAIN_NAME=maze_v2_live
TRAIN_STEPS=60000000
TRAIN_RESUME=trainer/maze_v2_start.pt
TRAIN_ARGS=--curriculum
```

**Why rule v2 (2026-09-26).** The rule v1 network (`maze_v1_start.pt`, 2.65M steps) raced its curriculum to 7x7 mazes
on the strength of open rooms and never learnt to go round a wall: on the live page it flicked left-right against
walls (6.4 reversals of the marker a second) instead of finding the cheese. Rule v2 (`maze_env.py`, `RULES = 2`) adds
a "which way is open / blocked" cue to the observation (51 inputs, was 40), rewards progress toward the cheese without
rewarding reversals (anti-dither shaping), charges a small cost for neck commands past the actuator's range (the
commands that did nothing to the body but killed the exploration noise at a wall), and steps the curriculum up only on
a sustained 85% escape rate over 100 mazes (`train.py`, `MAZE_CURR_*`). Measured the way the live page plays (the
policy's mean action, courses of 4 mazes, 32 courses per row, `python maze_env.py --eval <ckpt> --course`):

| network | level 0.4 (5x5) escapes | level 0.5 (5x5) escapes | marker reversals / s | wall bumps per maze |
|---|---|---|---|---|
| rule v1, `maze_v1_start.pt` (2.65M steps) | 26% | 8% (48 mazes, one maze per episode) | 6.4 | 16.9 |
| rule v2, `maze_v2_start.pt` (10.1M steps: variant B of the A/B, with the saturation cost, continued) | 76% | 56% | 2.0 | 3.2 |

At 7.1M steps (the end of the A/B) variant B escaped 63% / 51% and variant A (rule v2 without the saturation cost)
56% / 43%; the A/B started from one 5.9M-step rule v2 network. `maze_v2_start.pt` is variant B continued to 10.1M
steps (`runs/maze_v2_B_cont/policy_last.pt`, curriculum level 0.425).

or from the final steering network, `TRAIN_RESUME=runs/final/steer.pt` (widened for the 19 maze inputs; the
curriculum starts at 0 and climbs by itself). Use a new `TRAIN_NAME`.

- **Start:** set the variables. The deploy log shows `new job runs/steer_rw1`, `publisher started`, `train.py`'s
  JSON rows, then `publishing steer_rw1 ... connected to wss://...`. The relay's `/status` shows `"live": true`
  once the first checkpoint is saved (every 5 PPO iterations).
- **When it finishes:** `policy_final.pt` is saved, the publisher says bye, and `entry.py` idles (it does not exit,
  so Railway does not restart it). The CPU stops being used.
- **Stop or pause:** set `TRAIN_TASK=none`. The redeploy stops `train.py`, its workers and the publisher (which
  says bye). Set `TRAIN_TASK` back with the same `TRAIN_NAME` to resume from the last checkpoint.
- **A new job:** use a new `TRAIN_NAME`. Old runs stay on the volume.

## A second trainer for Rat Maze: `labrat-trainer-maze`

One service streams one run, and the site wants two at once: Rat Tiles on `labrat-trainer` drives the buybacks
(`/buyback`, the relay's default channel) and Rat Maze drives the burns (`/burn`, the relay's **maze channel**,
`relay/README.md`). The second trainer is the **same image**, run as a second Railway service with two variables
that differ: `TRAIN_TASK=maze` and `LABRAT_RELAY_CHANNEL=maze`. `entry.py` passes the channel to the publisher as
`--channel maze`, which connects to `/publish?channel=maze`; the relay forwards that stream marked
`"channel":"maze"` (frames prefixed `MZ`), so the default channel and everything that reads it are untouched.
Nothing new is built or written for it; only configuration.

Order matters: deploy the relay with the maze channel first, then the site and the buyback engine that know to skip
it, and only then this service (the relay sends nothing for the channel until a publisher streams on it).

**Create it (Railway dashboard, same project as `labrat-relay` and `labrat-trainer`):**

1. **New → Empty Service**, name it `labrat-trainer-maze`.
2. **Settings → Source:** the same source as `labrat-trainer`. Deployed with the CLI from the repo root, that is
   `railway up --service labrat-trainer-maze` (the same upload as for `labrat-trainer`, so the same files and the
   same `trainer/Dockerfile` build the same image; if `labrat-trainer` deploys from GitHub instead, connect the same
   repo and branch). Root directory: the repo root (not `trainer/`).
3. **Settings → Build:** the variable `RAILWAY_DOCKERFILE_PATH=trainer/Dockerfile` (step 5), no build or start
   command (the image's `tini -- python /app/trainer/entry.py` runs).
4. **Volume:** attach a **new** volume mounted at `/data` (a volume belongs to one service; this one starts empty and
   `entry.py` seeds its `runs/final/` from the image on first start). One replica. No public domain, no health check
   path (the trainer serves no HTTP). Restart policy **On Failure**. App sleeping **off**.
5. **Variables** (every change redeploys; set them all, then deploy once):

   ```
   RAILWAY_DOCKERFILE_PATH=trainer/Dockerfile
   RAILWAY_DEPLOYMENT_DRAINING_SECONDS=30
   TRAIN_TASK=maze
   TRAIN_NAME=maze_v2_live
   TRAIN_STEPS=60000000
   TRAIN_RESUME=trainer/maze_v2_start.pt
   TRAIN_ARGS=--curriculum
   TRAIN_WORKERS=<the service's vCPU limit - 1>
   LABRAT_RELAY_CHANNEL=maze
   LABRAT_PUBLISH_TOKEN=<the relay's LABRAT_PUBLISH_TOKEN, the same value labrat-trainer has>
   LABRAT_RELAY_PUBLISH=wss://labrat-relay-production.up.railway.app/publish   (the default; set it only if it differs)
   ```

   Nothing else. In particular no wallet variable of any kind: `entry.py` names one in the log and passes it to nobody.
6. **Deploy** (`railway up --service labrat-trainer-maze` from the repo root, or the dashboard's deploy). The log
   should show `runs/ -> /data/runs (a mounted volume)`, `new job runs/maze_v2_live: maze, 60,000,000 steps from
   trainer/maze_v2_start.pt`, `publisher started (pid ...) -> wss://.../publish (channel maze)`, then the
   publisher's `publishing on the relay's 'maze' channel (wss://.../publish?channel=maze)` and `connected to`.
   The relay's `/status` then shows `"maze_channel": {"live": true, "hello": {"task": "maze", ...}}` once the
   first checkpoint is saved (every 5 PPO iterations), while its top-level `live`/`hello` keep describing
   `labrat-trainer`'s Rat Tiles run.

**Checks and failure modes.** `publishing on the relay's 'maze' channel` missing from the log: the variable is not
set (the run then streams on the default channel and the log says so: `note: a Rat Maze job without
LABRAT_RELAY_CHANNEL=maze streams on the relay's default channel`). `ERROR: LABRAT_RELAY_CHANNEL=maze carries Rat Maze
runs only, but TRAIN_TASK is ...`: the publisher is not started until `TRAIN_TASK=maze`. `the relay does not know
this publish channel (HTTP 400)` then `the relay refused the publisher (exit 2)`: the relay running is older than
the maze channel; redeploy the relay first, then redeploy this service. `HTTP 409: another publisher is streaming`:
something else already holds the maze channel (a local test publisher, or an older deployment of this service
still draining); it retries by itself. Stop or pause it like any job: `TRAIN_TASK=none`.

### Restarts

A restart or redeploy never trains a job from zero again:

- `runs/<name>/policy_final.pt` exists: the job is done and the service idles.
- `runs/<name>/policy_last.pt` exists: the job resumes from it, towards the same total (recorded in
  `runs/<name>/trainer_job.json`). At most the last 5 PPO iterations are lost. `--init-std` is not applied again,
  because it resets the exploration noise and is meant for a job's start. Note that `train.py --resume` itself
  starts the `--curriculum` difficulty and `--lr-end` annealing over from their initial values (a tiles job goes on
  at the difficulty saved in its checkpoint).
- `TRAIN_FORCE`: to train a finished job again, set `TRAIN_FORCE` to any value that job was not trained with (`1`,
  then `2` the time after). The old run directory is kept as `runs/<name>.prev-<time>`. The value is recorded, so
  a later restart with the same `TRAIN_FORCE` does not train it again.

### Getting a trained network out

The runs stay on the volume, in `/data/runs/<name>/`. Only the image's `runs/final` is ever copied onto the volume.
Runs on the PC are not. One way to copy a file off is a shell in the service with `railway ssh`, for example
`base64 /data/runs/<name>/policy_final.pt`, and decode that on the PC.

## Cost

Railway bills this service by use: vCPU-minutes and GB-minutes of memory, plus the volume's storage and network
egress. Check Railway's pricing page and the service's usage tab for the actual rates; none are given here.

- **A run** costs its length × the cores it keeps busy, plus its memory over that time. The cores are
  `TRAIN_WORKERS` simulation workers, `train.py`'s PPO update (2 torch threads, in bursts) and the publisher. On
  the owner's PC the publisher used 9 to 16% of one core. The length follows from the `sps` in the log rows:
  minutes ≈ `TRAIN_STEPS / sps / 60`. More workers give more steps per second, so a run is shorter but uses more
  cores at once. Memory grows with the number of workers and envs. Watch the service's metrics during the first
  run.
- **Idle** (`TRAIN_TASK=none`, or a finished job): no training processes, one sleeping Python process. It still
  uses a little memory, and the volume is billed while it exists.
- **The live view** sends 1,868 bytes × 25 frames a second (about 168 MB an hour) from this service to the relay
  over its public URL. Railway may bill that as egress.

## Railway settings

- Same project as the relay. Deploy from the repo root with `railway up --service labrat-trainer`. The upload
  respects `.gitignore` and `.railwayignore`. Set `RAILWAY_DOCKERFILE_PATH=trainer/Dockerfile`.
- Volume mounted at `/data`. One replica.
- No start command (the image's `tini -- python /app/trainer/entry.py` runs). No public domain and no health check
  path, because the trainer serves no HTTP.
- Restart policy **On Failure**. `entry.py` idles instead of exiting, and exits 0 when Railway stops it.
- App sleeping (serverless) **off**. A job trained without the live view sends no traffic and could be put to sleep.
- `RAILWAY_DEPLOYMENT_DRAINING_SECONDS=30` gives the publisher time to say bye on a redeploy. Without it the job
  still resumes from its last checkpoint.

## Test it

Docker (not run on the owner's PC, which has no Docker; Railway builds the image):

```
docker build -f trainer/Dockerfile -t labrat-trainer .
docker run --rm -e TRAIN_TASK=none -v labrat-data:/data labrat-trainer
```

Without Docker, `entry.py` runs from a copy of the files the image gets, never from the working checkout. There
`runs/` is a real directory, and `entry.py` refuses to replace it with a link. With `LABRAT_DATA_DIR` pointing at a
scratch folder, a local relay (`relay/README.md`) and `LABRAT_RELAY_PUBLISH=ws://localhost:<port>/publish`, it runs
the same way as on Railway.
