# labrat buy rig (container notes, not deployed)

For each simulated buyback batch the buyback engine books, the rat clicks through pons's own BUY flow on the real
$LABRAT coin page, and the website streams it. Everything is **simulated**: pons builds the buy transaction, the rig
checks it, simulates it with `eth_call` and answers pons with a refusal (EIP-1193 4001, "Simulated buy: not signed").
Nothing is signed or sent. The owner has not asked for real buys.

```
labrat-buyback (Railway)         labrat-buyrig (this image, NOT deployed)                labrat-relay        site
GET /status  ------------------> buyrig_runner.py --> buyrig.py (Chromium + the rat) --wss /publish?channel=pons-->  "Rat on pons"
POST /pons_session <------------ (one session per new simulated buy; replay MATCH first)                  GET /status -> buyback list
```

## What runs

* `live/buyrig_runner.py` polls the engine's public status every 20 s. Every simulated buy listed there is one batch.
  On its first start it takes the listed buys as history. For each new one, oldest first and exactly once (the state
  file marks it "started" before anything runs, so a crash or a redeploy never runs a batch twice), it:
  1. runs `live/buyrig.py --amount <the batch's ETH> --batch-at <its time>` (its own Chromium and brain session),
  2. checks the recording: `python replay_session.py <run dir>` must print `MATCH`,
  3. reports the session to the engine (`POST /pons_session`, fixed fields only). The engine checks them against its
     own booked buy and shows it in `/status` as `buys.recent[i].pons` with the label
     "Simulated buy · clicked by the rat on pons". It changes no cap and no figure.
  Only a session whose 13 checks passed, whose simulation succeeded, whose stream mask held and whose replay matched
  is reported.
* `live/buyrig.py` is one session: the $LABRAT page at 1280x900 (dark), a fresh in-memory key and ponsbot's 1 ETH read
  override, the stream mask installed before pons loads, and the rat's two trained networks. The rig lights each
  target in turn (pons's terms for a new wallet, the ETH amount field, Buy LABRAT, Confirm buy). The rat steers onto it
  with its head and clicks with a lever press, and clicks off the lit target are never forwarded. The rig types the
  amount after the rat clicks the field. The checks and the recording are described in the file's docstring and in
  `live/BUYRIG_RESEARCH.md`.
* `live/buyrig_test.py` runs the mocked tests. `--real-session` adds one real session streamed to a local relay.

## The image

`buyrig/Dockerfile` builds from the repo root. `buyrig/Dockerfile.dockerignore` (BuildKit's per-Dockerfile ignore
file, which replaces the root `.dockerignore` for this build) lets in only the brain session's code (`env.py
cursor_env.py steer_env.py session.py ptload.py`, which are part of the brain commit, so they are copied unchanged),
`replay_session.py`, `launcher.py`, `assets/`, the two final networks and five files of `live/`. `.env`, old runs,
`site/` and `relay/` never reach it. Python 3.13 slim, Playwright's Chromium with its system libraries
(`playwright install --with-deps chromium`), MuJoCo and numpy (no torch), pinned in `buyrig/requirements.txt`. The
build ends by importing the rig and reading both networks.

If Railway's builder ignores `Dockerfile.dockerignore`, the root `.dockerignore` applies instead. It lets in only the
trainer's files, so the build fails loudly and nothing extra is uploaded. Deploying would then need a root ignore file
that also lists the files above, and the same for `.railwayignore` when deploying with `railway up`.

Size it for Chromium plus a real-time MuJoCo session: about 2 vCPU and 2 GB of memory. A session takes about a minute
(the recorded one took 32 s) and its replay about as long again. Playwright runs Chromium without its sandbox by default,
which is what a root container needs.

## Service variables

| variable | |
|---|---|
| `BUYRIG_RELAY` | the relay's publish URL, e.g. `wss://labrat-relay-production.up.railway.app/publish` (the runner adds `?channel=pons`). Unset: sessions run and are recorded but not streamed |
| `LABRAT_PUBLISH_TOKEN` | the relay's publish token (the same one the trainer uses) |
| `BUYBACK_RIG_TOKEN` | a shared secret of 24+ characters, set on **both** this service and `labrat-buyback`. The engine opens `POST /pons_session` only when it runs DRY and has this token in its process environment. Unset: sessions run but are not reported, and the site's buyback list shows no pons label |

No wallet key is needed or present. The rig uses a fresh in-memory key per session, and that key never holds anything.

## Volume

Mount a volume at **`/data`**. The runner passes `--out-root /data/runs` to each session. The two networks stay in
the image at `runs/final/`, because `buyrig.load_brain` refuses networks outside the repo folder and resolves
symlinks. `/data/runs/` keeps every session's recording (`session.json` and its proof,
`qpos.npy`, `actions.npy`, the two networks, `captured_tx.json`, `events.jsonl`, `targets.json`, `frames/` and
`buyrig_result.json`). `/data/buyrig/runner.json` records which batches ran. Without the volume, a redeploy would
forget which batches ran, and the runner would take the listed buys as history again, which is safe but loses the
record. `captured_tx.json` and `events.jsonl` hold the session's throwaway address and pons's raw calldata. They stay
on the volume and are not published.

## Before going live with real buys

This image cannot buy: `buyrig.py` has no LIVE path, and the engine takes no pons report from a LIVE engine. Real buys
would need a separate, gated path like the launch rig's, and the owner has not asked for one.
