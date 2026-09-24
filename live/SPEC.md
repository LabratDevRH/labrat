# RATBRAIN live rig — build spec

Modelled on flybrain's Robinhood Chain rig (`C:\Users\USER\claude\flybrain\rhlive.py`, `rhprovider.py`,
`record.py`, `web/live.html`). Read those; reuse and adapt, don't reinvent. The flybrain rig worked on
ponsfamily.com in Sept 2026. Its selectors and flows (terms, image upload, form fields, Launch button)
are the starting point. Re-verify them against the live site, because pons may have changed since.

## What it is

A local web app (`python live/rig.py`, http://localhost:4661) with one page and a START button. It shows
the virtual rat live in its Skinner box on the left and the **real ponsfamily.com launchpad create
page** on the right. The page is streamed as JPEG frames from a headless Playwright Chromium that the
rig drives.

1. **Pre-fill (script, disclosed on screen as "operator script").** Open
   `https://www.ponsfamily.com/launchpad/create` with an injected EIP-1193 wallet. The owner has
   accepted pons's terms and has authorized the rig to click them. Then:
   - upload `live/assets/coin.png`
   - fill in the name, ticker and description
   - leave the pair at the page default (ETH) and set creator tax to 1% if the page asks

   The name and symbol come from `launcher.config()` (default `ratbrain` / `RATBRAIN`). The description
   is `launcher.description(proof)`. The **proof** is computed first by an instant rehearsal:
   `rollout.run(policy, seed, preroll_s=PREROLL)`. It is deterministic, and it is shown on screen.
2. **The rat trial (live simulation, real time).** Call `rollout.run(policy, seed, preroll_s=PREROLL,
   realtime=True, on_step=..., on_press=...)`:
   - The rat stands with its brain off for PREROLL seconds (default 3.0), then the brain comes on.
   - Every control step is streamed to the viewer.
   - When the paw press registers, `on_press(proof)` fires.
3. **The press clicks Confirm.** Who clicks what, in the words used everywhere on screen: *The operator
   script fills the form and opens pons's launch review; the rat's lever press clicks Confirm, which makes
   pons request the launch transaction.* Before the trial the operator script presses pons's "Launch token"
   button, which only opens pons's review dialog ("Launch <SYMBOL>", Confirm / Cancel) and sends nothing.
   `on_press` first checks that the proof equals the rehearsal proof, and refuses otherwise. It then
   schedules, without blocking the sim thread, a visible mouse glide to the review's Confirm and a click.
   The Confirm is measured fresh at the press (and again at the end of the glide): it must sit inside a
   dialog whose text includes the line "Launch <SYMBOL>", be enabled and uncovered, and not have moved.
   Otherwise the click_launch stage fails and nothing is clicked (never stale coordinates). The rat keeps
   streaming its 1.2 s post-press window meanwhile.
4. **pons builds the transaction and calls `eth_sendTransaction`**, which goes to our Python hook.
   Requests before the press are refused and do not count; the first one after it is the launch request;
   any later one is refused.
   - **DRY (default, the only mode that may run now):** decode the calldata with
     `launcher.TYPES`/`launcher.SELECTOR` and check it against what we expect:
     - `to` is the pons v2 factory
     - selector `0xa72101af`
     - value 0.0005 ETH
     - pairToken `0x0`
     - name, symbol and description (including the proof) as filled
     - creator (and `from`) equal our wallet address
     - image equals the `ipfs://` URI pons returned from `/api/ipfs/image` for the uploaded coin.png
     - socials equal `(cfg x, '', cfg website, '', '')` (all empty by default)
     - amountIn (developer buy) 0, the struct's uint256 0, trailing bytes empty
     - creatorTaxBps equals `launcher.config()['tax_bps']` AND pons's creator tax field took that value
     - canonical ABI encoding: `encode(TYPES, decoded) == bytes.fromhex(data[10:])`; a request carrying
       both `data` and `input` with different bytes is refused

     DRY and LIVE run the same checks, so a DRY run proves every one of them.

     Report every field (including the two bytes32 fields pons fills) to the viewer. Then **refuse**:
     throw an EIP-1193 user-rejected error (code 4001, "DRY RUN: rat rig refused to sign"). Nothing is
     signed and nothing is broadcast.
   - **LIVE (build it, never run it):** only when ALL of these hold:
     - `launcher.config()['live_env']` is true
     - the CLI flag `--live` is given with `--confirm SYMBOL`
     - `launcher.config()` at START equals what the rig pinned at startup (name, symbol, tax_bps, image,
       x, website, live_env and the key's address); otherwise the run is refused
     - pons's creator tax field took the configured tax, and pons returned an `ipfs://` image URI
     - `launcher.preflight(cfg, proof)` passed before the trial started (fresh wallet, nonce 0, balance)
     - `launcher.reserve_journal(...)` succeeded
     - the decoded calldata passes every check above

     Then sign **exactly the calldata bytes that were decoded and checked**, pinned to **nonce 0** with
     the preflight gas figures, `launcher.write_journal` the raw signed bytes BEFORE broadcasting,
     broadcast, and `launcher.poll`/`classify` the receipt. `sign_and_send` returns (hash, send_error):
     broadcast is true when no RPC errored or the error says the node already has it
     (`launcher.POSSIBLY_SENT`), "unknown" when every RPC timed out, false when every RPC rejected it
     (the page then gets an error, not a hash). Never sign anything that fails a check. The rig waits
     30 s for pons to ENTER `eth_sendTransaction` after the click, then in LIVE awaits the wallet hook
     to the end (its RPCs retry); every outcome's `signed`/`broadcast` is derived from what happened
     (`LiveLaunch.sent`, or a raw tx in the journal), never assumed, and a sent tx is always polled and
     recorded, even when the run failed afterwards. A reserved journal whose run ends without signing
     (no press, proof mismatch, failed click, refused calldata, any error) is closed as
     `aborted_before_sign` with the reason, after which nothing can sign. DRY never creates, writes or
     reads `launch_journal.json`.
   - personal_sign / typed-data requests: DRY signs them with the throwaway in-memory key only (pons has
     never asked on any rig run so far). LIVE refuses ALL `eth_signTypedData*` (typed data can authorise
     moving funds without a transaction: permit, EIP-3009 ReceiveWithAuthorization, orders) and every
     personal_sign except an EIP-4361 sign-in for `www.ponsfamily.com` naming our address and chain 4663,
     URI on https://www.ponsfamily.com, issued within the last 10 minutes, with no resources and no
     unknown or repeated fields. `eth_sign` is always refused. Every request and refusal is logged to
     the viewer and recorded in run.json.
5. **Record everything.** Write `runs/rig_<UTC stamp>_seed<seed>/` containing:
   - `rollout.save(...)` output (qpos.npy, actions.npy, run.json)
   - run.json['launch'] = the outcome: mode `dry_captured` / `dry_refused_mismatch` / the launcher
     modes for live
   - `captured_tx.json`: raw tx request plus decoded fields plus per-field check results
   - `events.jsonl`: every event sent to the viewer
   - `pons_final.jpg`

   `replay.py <run dir>` must MATCH.
6. **Recorder.** `live/record.py` opens the rig page in Playwright at 1920x1080 with `record_video`,
   triggers START (the page supports `?autostart=1`), waits for the `done` event, then converts the
   webm to `build/recordings/rig_<stamp>.mp4` with ffmpeg. `--dry` refuses to run if `.env` says
   RATBRAIN_LIVE=1. Mirror flybrain's record.py.

## Server: `live/rig.py` (FastAPI + uvicorn, port 4661)

- `GET /` → `live/web/rig.html`
- `GET /rat.json` → the mesh asset (below)
- `GET /status` → `{mode: 'DRY'|'LIVE', symbol, name, policy_sha256, seed, busy, last_run}`
- `WS /run` → the handshake is refused unless its `Origin` is `http://localhost:<port>` or
  `http://127.0.0.1:<port>` (the rig page, `record.py` and the test client all open it on localhost).
  In LIVE the rig also prints a random per-process token at startup; the page URL carries it as
  `?token=...` and the start message must echo it (`record.py --token`). The client sends
  `{"type":"start","seed":2026[,"token":...]}`. The server streams:
  - JSON text messages:
    - `{"type":"log","msg":...}`
    - `{"type":"stage","stage":S,"state":"active|done|failed","detail":...}`
    - `{"type":"shot","jpg":<base64>,"w":,"h":}`: pons frames at ~6-8 fps; throttle so it never lags the rat
    - `{"type":"proof","proof":...}`
    - `{"type":"press","t":..,"proof":..}`
    - `{"type":"tx","fields":{...},"checks":{...},"verdict":"dry_captured"|...}`
    - `{"type":"done","run_dir":...,"outcome":...}`
  - Binary pose frames (little-endian Float32Array), one per control step (50 Hz) including the
    pre-roll: `[1.0 magic, sim_t, brain_on(0/1), pressed(0/1), lever_angle, paw_dist_m, n_bones=65,
    then 65 x (px,py,pz, qw,qx,qy,qz)]`. These are the world pose of each skinned body, in the order
    of `m.skin_bonebodyid`, taken from `env.d.xpos` / `env.d.xquat`.
- Stages, in order: `wallet`, `terms`, `image`, `name`, `ticker`, `description`, `rehearsal`,
  `brain_off`, `brain_on`, `press`, `click_launch`, `tx`, `outcome`.
- Only one run at a time. The sim runs in a worker thread and the Playwright page is driven from the
  asyncio loop. Use `asyncio.run_coroutine_threadsafe` from `on_press`.
- CLI: `python live/rig.py [--port 4661] [--seed 2026] [--preroll 3.0] [--policy runs/final/policy.pt]
  [--headful] [--live --confirm SYMBOL]`.
  - The default is DRY.
  - `--live` without passing every launcher gate must exit before anything opens.
  - No `.env` exists today, so LIVE is impossible now. Keep it that way.

## Assets: `live/export_assets.py`

- `live/assets/rat.json`:
  - `verts`: rest positions, metres, flat
  - `faces`: flat
  - `bones`: names
  - `bind_pos` and `bind_quat`: per bone
  - `weights`: per bone `[vertex_ids..., weights...]`, from MuJoCo's skin, all influences
  - `coat`: per-vertex RGB
  - `fur`: per-vertex fur amount 0..1 (0 = bare paws, tail and nose tip)
  - `rest_normals`
  - `chamber` constants: WALL_X 0.175, LEVER_Z 0.032, LEVER_LEN 0.045, SIDE_Y 0.20, BACK_X -0.36,
    WALL_H 0.12, lever hinge axis (0,-1,0)

  Use the same coat/fur masks as `blender/film.py` (hood = skull/jaw/neck bones, bare = hand/finger/
  foot/toe + tail vertebra_C3..C30, nose tip). Source: `rat_skin.npz` (run `export_anim.py` logic, or
  load `runs/film_2026/blender/rat_skin.npz`).
- `live/assets/coin.png`: 1000x1000 square coin image cropped from `runs/film_2026/test_top/f_0062.png`
  (the hooded rat from above, head and lever in frame). Must be a PNG under pons's size limit (~2 MB).

## Viewer: `live/web/rig.html` (single file, three.js r160+ from cdn.jsdelivr.net/npm, no build step)

- 1920x1080 layout: left ~58% is the 3D rat, right ~42% is the pons page stream (letterboxed at the
  page's aspect), and a strip across the bottom holds the telemetry, the stage stepper and a log console.
- The top bar reads "RATBRAIN · RIG" followed by a prominent mode pill ("DRY RUN" grey / "LIVE" pink),
  then the symbol, the policy sha and seed.
- The pons page is driven at 1280x900 (close to the pons pane's aspect, so it is scaled less). When
  pons's transaction arrives, the tx panel moves under the rat and the pons stream takes the whole right
  side, so pons's own rejection toast stays readable.
- **Rat rendering, all real-time:**
  - CPU linear-blend skinning with ALL influences from `/rat.json`, driven by the streamed bone poses.
    The formula is exactly MuJoCo's: `v' = sum_b w_b (R_b R_bind_b^T (v - p_bind_b) + x_b)`, normalised
    by the weight sum.
  - Recompute normals every frame.
  - Interpolate between 50 Hz pose frames for 60+ fps display.
  - **Fur via shell texturing:** ~24 shells sharing the skinned position and normal buffers. A vertex
    shader offsets each shell along the normal by layer × 4 mm (world units are metres; the rat is
    ~0.24 m long). Strands come from a procedural hash noise on the REST position (so they stick to
    the skin) and are alpha-tested per layer, tapered toward the tip, with slight gravity/back-combing
    toward −x in rest space. Colour comes from `coat`, and fur is suppressed where `fur`≈0. Paws, tail
    and nose are pink skin.
  - Eyes as small glossy black spheres at the skull (skull body pose plus an offset of
    (0.0011, ±0.0128, 0.0025) m in skull frame). Whisker lines from the snout tip.
- **Chamber**, built from the constants:
  - dark floor with steel grid rods along x at 8 mm pitch, 2.2 mm radius
  - brushed-steel front panel with lever housing
  - the lever paddle hinged at (WALL_X, 0, LEVER_Z), rotated by the streamed lever_angle about −y
  - clear acrylic side walls, dark back wall
  - a cue light above the lever that turns pink on the press
  - an overhead house light, soft shadows, ACES/AgX-like tone mapping, subtle bloom (UnrealBloomPass)
    and a vignette
  - camera: cinematic 3/4 front-side, slow orbit, OrbitControls enabled; push in a little at the press
- **HUD, all numbers from the stream:**
  - `BRAIN OFF` / `BRAIN ON` with a dot, and `t = …s`
  - paw → lever in mm, lever angle in degrees with a threshold bar at 11.5°
  - the proof hash, typed out
  - a PRESS flash
  - the tx panel with decoded fields and ✓/✗ per check; the DRY verdict is shown in big type:
    "DRY RUN — pons built the launch tx, the rig refused to sign. No coin was created."
    A dev-harness replay (`live/dev_viewer.py`, tx flagged `dev`) shows "DEV REPLAY — fake tx, pons was
    not involved" instead.
  - the stepper for the stages above
  - a log console
- **Honesty text** at the bottom in small type: "Virtual rat: DeepMind's rodent model in MuJoCo, driven
  by a trained neural network (not a real brain). The operator script fills the form and opens pons's
  launch review; the rat's lever press clicks Confirm, which makes pons request the launch transaction.
  The proof in the description is reproduced by replay.py."
- **Style:** near-black background (#07070a), one pink accent (#ff3c82), Bahnschrift/DIN-like
  headings (use "Bahnschrift", "Segoe UI", system-ui fallbacks) and Consolas/monospace numbers. No
  glow gradients on the UI chrome.
- `?autostart=1` starts on load. The START button is hidden after starting.
- Must also work at 1280x720 (scaled).

## Hard rules for everyone

- **Never go live. Never create a `.env`. Never sign or broadcast a transaction.** DRY capture +
  refuse only.
- Clicking pons's terms/attestation and uploading the coin image during DRY runs is authorized by
  the owner.
- The private key never enters the page. Only an address plus signatures cross into the page
  (flybrain's rhprovider pattern). The wallet's Python bindings (`page.expose_binding`) answer only the
  page's main frame on https://www.ponsfamily.com; the provider script installs only there (never in an
  iframe). Each binding re-checks its method in Python, and chain calls are a read-only allowlist
  (anything else gets 4200).
- Don't modify `env.py`, `rollout.py`, `ptload.py`, `launcher.py`, `launch_run.py` or `replay.py`.
  They're proof-critical and already audited. Import them.
- All new text files use LF line endings.
