# RATBRAIN brain rig: flybrain-style (spec v2)

The owner wants it **exactly like flybrain** (`C:\Users\USER\claude\flybrain\rhlive.py` + `web/live.html`),
with the rat's brain in place of the fly's. **The rat's brain does everything on the page:** it steers the
cursor, clicks every checkbox and button, and clicks into each field. **There is no 3D rat on the page.**
The body is still simulated underneath (MuJoCo, DeepMind rodent), because its head moves the cursor and its
lever press is the click, but it is not rendered.

## The brain

The brain is a trained neural network (PPO): 211 inputs → 512 → 512 → 256 → 38 motor outputs.
- Policy file: `--policy`, default `runs/cursor_v1/policy_best.pt`. Snapshots are in `runs/cursor_v1/`.
- It runs through `session.Session` (`session.py`, proof-critical, **do not modify**; read it). The rig only
  sends **commands**:
  - `session.target(cx, cy, hw, hh)`: normalized page-viewport coordinates, (0,0) top-left, (1,1)
    bottom-right of the 1280x900 pons viewport, plus half sizes
  - `session.hold()`: no target, the rat waits
- Everything else comes from the network:
  - head direction relative to the body → cursor velocity
  - a lever press → a click
  - `on_click(click, env)` gives `(step, x, y, hit)`, where `hit` means the click fell inside the cued target
- `Session.run(on_step, on_click, realtime=True)` runs in a worker thread. The rig calls `session.stop()` at
  the end, then `session.save(dir, extra)`. `python replay_session.py <dir>` must print MATCH.
- **Brain commit:** `session.commit` is known before the run. It goes into the coin description:
  `"launched by a virtual rat: its trained brain steered the cursor and clicked every button. brain sha256 <commit>"`.
  The full session proof is published with the recording afterwards.

## Server: `live/brainrig.py` (new file; port 4665 by default)

Reuse `live/ponsbot.py` for the provider, DRY capture/decode/checks/refuse, terms/image/field selectors and
the Confirm dialog check. Reuse `live/rig.py` patterns (Outbox, Origin check, run recording). Put
`threading.stack_size(32 MB)` at the top of the file, as rig.py does: the preview launcher's python gives
threads only a 1 MB stack, and MuJoCo scene compilation needs about 1 MB.
**DRY by default.** (Superseded 2026-09-24: the owner asked for a real test launch by the rat. `--live --confirm <SYMBOL>` now runs the gated LIVE path documented at the top of `live/brainrig.py`: .env RATBRAIN_LIVE=1 + key, preflight, journal, per-process token, and a live session starts ONLY from the owner's click on BEGIN SESSION, then Start in the page's confirm dialog. Mocked tests: `live/brain_live_test.py`.)

### Flow (one START)

1. Open the pons create page (1280x900) with the injected wallet (throwaway key, DRY balance override as
   in rig.py).
2. Start the Session in a worker thread. It runs 2 s brain-off pre-roll, then waits in `hold` until the
   first target.
3. **The task list: the rat clicks each one.** Before each target the rig:
   - scrolls it into view smoothly (the rat is on `hold`)
   - measures its box in viewport CSS px
   - sends `session.target(...)`

   Use the element's real clickable box. For checkboxes, use the whole clickable row or label, which gives
   a bigger target. Never pad a target beyond its real clickable area. Targets, in order:
   1. terms checkbox 1 (Terms of Use)
   2. terms checkbox 2 (Privacy Policy / attestation)
   3. the accept/continue button

      (The owner has accepted pons's terms and authorized the rat to click them.)
   4. the "Choose image" drop zone. Consequence: `set_input_files(live/assets/coin.png)`, then wait for
      "Image ready".
   5. Name field. Consequence: type the name live.
   6. Ticker field. Consequence: type the ticker live.
   7. Description field. Consequence: type the description with the brain commit, live.
   8. The "Advanced" toggle.
   9. Creator tax field. Consequence: type "1".
   10. "Launch token", which opens pons's review dialog.
   11. "Confirm" in the dialog whose text has the line "Launch <SYMBOL>". Consequence: pons calls
       eth_sendTransaction → DRY decode, all checks, refuse 4001, and pons shows its cancel toast.
4. **When the rat hits (`hit=True`):** the rig immediately sends `session.hold()` and does
   `page.mouse.click` at the rat's cursor pixel, which by definition is inside the target. It then runs the
   consequence. Typing uses `keyboard.type(text, delay=90)`, like flybrain. Then it moves to the next target.
5. **Misses** (a click outside the cued target) are never forwarded to the page. The page shows them as a
   grey ring and the log records them, like a masked touchscreen. If a target isn't hit within 20 s, the rig
   keeps it lit (the rat keeps trying).
6. **The cursor on the page:** every stream tick (25 Hz), `page.mouse.move` to the rat's cursor pixel, so pons
   hover states show. An injected in-page cursor labelled "rat" is drawn at that point, plus a translucent
   highlight of the current cued target ("cue window").
7. **Streaming pons:** use the CDP screencast (`Page.startScreencast`, JPEG q70, everyNthFrame 1) at about
   15-20 fps, with at most one frame in flight. The typing must be visible **character by character**.
   Replace rig.py's 7 fps screenshot loop.
8. **Record:** `runs/brainrig_<UTC>_seed<seed>/` gets:
   - `session.save(...)` output (session.json, qpos.npy, actions.npy)
   - `captured_tx.json`
   - `events.jsonl`
   - `pons_final.jpg`
   - `targets.json` (each target's page box, the hit time and the misses)

   `replay_session.py` must MATCH.

### WS protocol (`/run`)

- **JSON messages:**
  - `log`, `stage` (per target: `{"stage": "t03_accept", "label": ..., "state": "active|hit|done|failed"}`)
  - `shot` (`{jpg, w, h}`)
  - `click` (`{x, y, hit, target}`, in page px)
  - `commit`
  - `tx` (fields, checks, verdict)
  - `done`
- **Binary brain frames at 25 Hz**, little-endian:
  - Header, Float32 ×16:
    `[2.0 magic, sim_t, brain_on, cursor_x, cursor_y, tgt_cx, tgt_cy, tgt_hw, tgt_hh (-1 if hold),
    holding, locked, lever_angle, head_yaw_defl, head_pitch_defl, hits, misses]`
  - Then Uint8 activations for input 211, h1 512, h2 512, h3 256 and motor 38 (1529 bytes). Map each unit to
    0..255 with a per-layer fixed scale; ELU outputs are ≥ -1. Use `session.activations(pol, obs)` for the
    maths. It needs the policy object, so construct your own NumpyPolicy from `session.pol_bytes`, or add a
    small hook in brainrig.
- **dev mode `--dev-oracle`:** for testing only. A scripted cursor drives the targets instead of the brain
  (for exercising the pons automation while the brain is still training). The viewer must show a huge red
  banner "DEV: SCRIPTED CURSOR — NOT THE RAT". record.py refuses to record it (the server's `/status` says
  `dev_oracle: true`).

## Viewer: `live/web/brain.html` (single file)

It mirrors flybrain's `web/live.html` layout and feel. Read that file and adapt its CSS and structure;
the IBM Plex fonts and dark palette are fine.
- **Header:**
  - "RATBRAIN" + tag "a rat's brain on the launchpad"
  - chips: `1,529 units · 510,976 connections`, DRY RUN, `$SYMBOL`, brain commit (short), seed
  - START button
- **Main left (large): the pons stream**, with the rat's cursor, the cue-window highlight, click rings (pink
  for hit, grey for miss) and a caption of the current target.
- **Main right: the brain canvas.** The network drawn as columns of dots, one dot per unit, brightness =
  activation:
  - "senses 211": body + the target cue, with the cue inputs highlighted
  - "layer 1 · 512"
  - "layer 2 · 512"
  - "layer 3 · 256"
  - "motor 38", with the 4 neck/head outputs labelled "steers the cursor" and the forelimb outputs labelled
    "presses the lever"

  Faint sampled connection lines between layers, weighted by the actual weights of the strongest links. The
  weights come from a `GET /weights_summary`, served by brainrig from the policy: for each layer pair, the top
  ~400 |w| links. A readout "units firing" (the count above a threshold). Label honestly: "artificial
  network units (not anatomy)".
- **Under the brain:** telemetry.
  - head yaw/pitch deflection → cursor velocity arrows
  - lever angle bar with the click threshold
  - hits / misses
  - a stepper of the 11 targets with states
- **Log console** at the bottom, plus an honesty footer: "The rat's brain is a trained neural network driving
  a simulated rat body (DeepMind rodent model in MuJoCo). Its head direction moves the cursor and its lever
  press clicks. The rig lights up the next target (a cue, like a cue light in an operant chamber), scrolls
  the page, and types the text of a field after the rat clicks into it. Clicks outside the lit target are
  ignored. DRY RUN: pons builds the launch transaction, the rig refuses to sign."
- `?autostart=1`. `window.__rigDone` / `window.__rigDoneMsg` for record.py. Works at 1920x1080 and 1280x720.

## Hard rules

- The DRY path never creates or reads `.env` and never signs or broadcasts. LIVE (above) is started only by the owner; agents never start it, never read `.env` and never sign.
- The throwaway key only; the private key never enters the page.
- Don't modify `session.py`, `cursor_env.py`, `env.py`, `rollout.py`, `ptload.py`, `launcher.py`,
  `replay_session.py` or `train.py`.
- **Memory is tight:** a training job is running. Before starting a browser or server, check free commit
  (PowerShell `(Get-CimInstance Win32_OperatingSystem).FreeVirtualMemory/1MB` GB) is ≥ 2.5 GB. Keep at most
  ONE headless Chromium and ONE server at a time. Kill what you start.
- LF line endings.
