# labrat relay

The relay carries a training run to the website's live view. `live/publish_training.py` runs on the owner's PC next to
training: it plays the latest saved checkpoint in its own simulation and streams it here. The relay then forwards the
stream to every open copy of the site.

```
owner's PC                              relay (Railway)                     website (Vercel)
publish_training.py  --wss /publish-->  relay.py  --wss /live (public)-->   site/js/live.js
  (token in a header)                     keeps the latest hello +          shows LIVE only while
                                          last 300 log rows                 the relay says live
```

The relay adds nothing to the stream. It forwards what the publisher sends and replays what it kept. **live** means
exactly this: a publisher is connected, it has sent a `hello` with `"source":"training"` that is not a test stream, and
it has sent something in the last 15 s. The relay refuses a `hello` with any other source, so a replay can never show
up as live. It also refuses a test stream (`publish_training.py --assume-live-for-test`, which marks its hello
`"test": true` and starts its label with `TEST`), because that plays an old run, not one that is training. Only a
local relay started with `RELAY_ALLOW_TEST=1` lets one through. The relay holds no keys and cannot sign anything.

## Endpoints

| | |
|---|---|
| `WS /publish` | the training publisher (the default channel: Rat Tiles, and whatever else trains on the main trainer). Needs `Authorization: Bearer <LABRAT_PUBLISH_TOKEN>`. Only one publisher at a time |
| `WS /publish?channel=maze` | the second training channel, same token and same protocol: the Rat Maze trainer's publisher (`live/publish_training.py --channel maze`, set by `LABRAT_RELAY_CHANNEL=maze` on the `labrat-trainer-maze` service). Rat Maze runs only. One at a time, independent of the default channel's publisher. See [The maze channel](#the-maze-channel) |
| `WS /publish?channel=pons` | the buy rig (`live/buyrig.py`), same token: the rat clicking through a $LABRAT buy on the real pons page, as masked JPEG frames plus step messages. One at a time, independent of the training publisher. See [The pons channel](#the-pons-channel) |
| `WS /live` | public viewers. They can only receive. A viewer may send `ping` (or `{"type":"ping"}`) and gets `{"type":"pong","t":<unix s>}` back. Viewers get all three channels on this one socket |
| `GET /status` | JSON with `live`, `hello`, `metrics` (the last log.jsonl row), `viewers`, and also `checkpoint`, `episode`, `publisher` (`in_session`, `quiet_s`), `history_rows` (kept), `state_rows` (in the state message), `allow_test_streams`, `frames_in`, `fps_in`, `max_viewers`, `max_viewers_per_address`, `counts`, `maze_channel` (the same stream fields for the maze channel: `live`, `hello`, `metrics`, `checkpoint`, `episode`, `publisher`, `history_rows`, `state_rows`, `frames_in`, `fps_in`, `maze`), and `pons` (`live`, `hello`, `step`, `result`, `publisher`, `frames_in`, `fps_in`, `last_frame_bytes`, `max_frame_bytes`, `viewer_fps`). Readable cross-origin, with no IP addresses |
| `GET /healthz` | `{"ok":true}` |
| `GET /` | the site when `SERVE_SITE=1`, otherwise a small JSON pointer |

### What a viewer receives

1. `{"type":"state","live":bool,"hello":{...}|null,"checkpoint":{...}|null,"episode":{...}|null,"history":[up to 300 rows]}`.
   If the stream is live, the newest binary frame follows straight after (and, in a Rat Tiles run, the newest `tiles`
   snapshot after that, see [Rat Tiles](#rat-tiles); in a Rat Maze run, the current maze's layout snapshot and then the
   newest `maze` snapshot, see [Rat Maze](#rat-maze)). The state message stays under 60,000
   bytes, because `site/js/live.js` ignores text over 65,536. Real `log.jsonl` rows are about 120 to 215 bytes, so
   300 rows fit. If they don't, the state carries the newest rows that do.
2. Everything the publisher sends, unchanged and in order: `hello`, `metrics`, `checkpoint`, `episode`, `bye`, binary
   frames (1868 bytes each for the rat: `(12 + 65*7) * 4`), in a Rat Tiles run `tiles` and `tile`, and in a Rat Maze
   run `maze` and `maze_end`.
3. `{"type":"idle","reason":"quiet"|"bye"|"disconnected"}` when the publisher goes quiet for 15 s, says bye or
   disconnects. The last `hello` and its history are kept, and late joiners get them with `live:false`.
4. A fresh `state` with `live:true` if a quiet publisher starts sending again (same session, no new hello).

Every `hello` starts a fresh history. `live.js` also clears its curve on a `hello`. After every (re)connect,
`publish_training.py` sends its hello, the checkpoint it is playing and its last `--backlog` (300) log rows, which
rebuilds the history on the relay and on every open page without duplicates. The relay does not reorder or
de-duplicate rows: they are the run's `log.jsonl`, verbatim. A `hello` from a different run or publisher session (a
different `run`, `task` or `started`) also clears the checkpoint, the episode and the last frame. Frames and metrics
sent before a `hello`, or after a `bye`, are dropped.

### What the publisher gets back

A refused handshake gets an HTTP status the client can read (with `websockets`, that is `InvalidStatus.response.status_code`):

| status | meaning | what to do |
|---|---|---|
| 401 | missing or wrong token | stop; fix the token |
| 409 | another publisher is streaming (it sent something in the last 15 s) | retry later |
| 503 | `LABRAT_PUBLISH_TOKEN` is not set on the relay (or is shorter than 16 characters) | stop; set it on the relay |

A publisher that has gone quiet for over 15 s (crashed, or a half-open connection) is replaced by the next one that
authenticates. The old one is closed with **4001**. A `hello` whose source is not `"training"`, or a test stream on a
relay without `RELAY_ALLOW_TEST=1`, closes the publisher with **1008**. The close reason never mentions a token, so
`publish_training.py` does not mistake it for an auth failure.

Anyone can try `/publish`, so refused handshakes are logged at most 3 times a minute, then as one summary line; the
full totals are in `/status` `counts` (`publishers_refused_auth`, `_busy`, `_disabled`, `_channel`).

### The maze channel

One trainer service can stream one run, and the site needs two at once: Rat Tiles drives the buybacks (`/buyback`)
and Rat Maze drives the burns (`/burn`). So the relay has a second training channel. The Rat Maze trainer's publisher
(`live/publish_training.py --channel maze`, which `trainer/entry.py` passes on from `LABRAT_RELAY_CHANNEL=maze`)
connects to `/publish?channel=maze` with the same token. The channel is a second copy of the default channel: its own
publisher slot (a second one gets **409**, a quiet one is replaced and closed with **4001**), its own `hello`,
history, checkpoint, episode, last frame and maze snapshots, its own state replay, 15 s idle timer and **live**
rule. It takes exactly what the default channel takes (`hello`, `metrics`, `checkpoint`, `episode`, `bye`, `maze`,
`maze_end`, binary frames; the same caps, junk handling and test-stream refusal), with one rule of its own: its
`hello` must say `"task":"maze"`. Any other task closes the publisher with **1008** ("the maze channel carries Rat
Maze runs only"), because the `/burn` page and the burn engine read this channel as Rat Maze.

Viewers get it on the same `/live` socket, marked so that the default channel's view (`site/js/live.js`) and
`live/buyback.py` can skip it and the `/burn` page can pick it out:

- **Text.** Every text of the channel carries `"channel":"maze"`, set by the relay when forwarding (the publisher's
  own `hello`, `metrics`, `checkpoint`, `episode`, `bye`, `maze` and `maze_end`, re-serialised as compact ASCII
  JSON with the key added last), and so do the relay's own messages for it: `{"type":"state","channel":"maze",
  "live":..,"hello":..,"checkpoint":..,"episode":..,"history":[..]}` (the key comes right after `type`) and
  `{"type":"idle","channel":"maze","reason":"quiet"|"bye"|"disconnected"}`. The default channel's texts are
  unchanged: **no `channel` key is ever added to them**, so a client that treats every text without one as the
  default channel's is right.
- **Frames.** A frame of the channel is `b"MZ"` + the publisher's frame (1868 bytes for the rat, so 1870 in all),
  the prefix added by the relay. The default channel's frames are unchanged (they start with the float32 magic 7.0,
  never with `MZ`). Each viewer's outbox keeps a **separate** drop-oldest budget of 16 for them
  (`maze_channel_frames_dropped_for_slow_viewers`), so neither channel's frames are dropped for the other's.
- **Late joiners** get, after the default channel's replay (its state, frame and game snapshot) and before the pons
  channel's, the maze channel's state and then, while it is live, its last frame (`MZ`), the current maze's layout
  snapshot and the newest snapshot (marked), exactly as the default channel replays a Rat Maze run. Nothing at all
  is sent for the channel until it has had a session (a `hello`), so a relay with this channel changes nothing for a
  site or an engine that does not know it, until the maze trainer actually streams. **Deploy in this order:** the
  relay, then the site (`live.js` ignoring `MZ` frames and `"channel":"maze"` texts, the `/burn` page reading
  them) and the buyback engine (ignoring texts with a `channel` key), then the maze trainer.
- **Snapshots** go through the same per-viewer rate caps as on the default channel, but each stream has its own
  slot and bucket: a maze-channel position snapshot never displaces an unsent default-channel one. Drops are counted
  under `maze_channel_maze_dropped_for_rate` / `_replaced_for_slow_viewers`.
- **Counters.** Everything the channel drops, refuses or accepts is counted under `maze_channel_*` in `counts`
  (`maze_channel_dropped_bad_json`, `maze_channel_refused_wrong_task_hello`, `maze_channel_publishers_accepted`,
  ...); the default channel's counters are untouched by it. `/status` shows the stream under `maze_channel`.

A Rat Maze run may still stream on the **default** channel (an older publisher without `--channel`, or
`relay/maze_demo.py`): that path is unchanged, but it then drives the default channel's page, not `/burn`.

### The pons channel

`live/buyrig.py --relay wss://<relay>/publish` connects to `/publish?channel=pons` (an unknown `channel` is refused
with **400**). The channel has its own publisher slot, its own 15 s idle timer and its own state, so the training
stream never notices it, and the other way round. Viewers get it on the same `/live` socket, marked so that the 3D
view (`site/js/live.js`) skips it and the site's "Rat on pons" panel picks it out:

- **Text.** The rig sends `pons_hello` (`"source":"buyrig"` and a short `session` id; any other source closes it with
  **1008**, and so does a test session, `"test": true` or a label starting with `TEST`, unless `RELAY_ALLOW_TEST=1`),
  then `pons_step`, `pons_result` and `pons_bye`. The relay adds `"channel":"pons"` when forwarding, and sends
  `{"type":"pons_idle","channel":"pons","reason":"bye"|"quiet"|"disconnected"}` itself. Each text is at most 4,096
  bytes. **Any text with an address-like `0x` string in it (checked on its ASCII JSON, so an escape cannot hide one)
  is dropped**: nothing on this channel may show an address.
- **Frames.** `b"PJPG"` + one JPEG (it must start with SOI and end with EOI), at most 256 KB, forwarded unchanged with
  the prefix, so the site can tell them from the rat's pose frames (which are unchanged too). The rig masks every
  frame before sending it (wallet chip, balances, any `0x` string); the relay cannot look inside a JPEG.
- **Rate.** A viewer gets at most 5 pons frames a second. Each viewer holds one pending pons frame: a newer one
  replaces it (`pons_frames_skipped_for_rate` in `counts`). It goes out after any queued text and ahead of queued
  training frames, so neither channel starves the other.
- **Late joiners** get, after the training state (and its frame), `{"type":"pons_state","channel":"pons","live":bool,
  "hello":..,"step":..,"result":..}` and then the channel's last frame. Both are kept after a session ends, so the
  site shows the last session's final frame between sessions. Nothing is sent for the channel before its first
  session. A `pons_hello` with a new `session` (or `started`) clears the last step, result and frame.
- Frames and steps sent before a `pons_hello`, or after a `pons_bye`, are dropped. Drops are counted under `pons_*`
  in `counts`.

### Rat Tiles

A training run whose `hello` has `"task":"tiles"` (the rat playing Rat Tiles: tiles fall down four lanes of its screen,
and each one it taps plays the next note of a public-domain melody on the site) also sends two text messages on the
training channel. The site's Rat Tiles panel (`site/buyback/index.html#piano`, at `/buyback#piano`) draws the board from them and plays the
notes (`site/assets/songs.json`, the same file as `assets/songs.json`).

- `{"type":"tiles","t":<sim s>,"song":<id>,"speed":<screen heights/s>,"lanes":4,"cursor":[x,y],"tiles":[[id,lane,y,h,state],...],"note_i":<int>}`:
  a snapshot of the board, at most 10 a second. Screen units are 0..1 with y down; `y` is a tile's centre, `h` its
  height, `state` is `up`, `hit` or `miss`. At most **4,096 bytes**, and it must carry a `tiles` list. Each viewer gets
  at most **12 snapshots a second** (a burst of 3); the rest are dropped for that viewer (`tiles_dropped_for_rate`).
  A viewer holds at most one unsent snapshot: a newer one replaces it at the end of its outbox, so a slow viewer skips
  to the newest board and the order with the tile events is kept (`tiles_replaced_for_slow_viewers`).
- `{"type":"tile","id":<int>,"lane":<int>,"result":"hit"|"miss"|"wrong","note_i":<int>,"song":<id>}`: one tile's outcome.
  At most **512 bytes**. Never dropped for rate, never replayed.
- A late joiner gets the newest snapshot straight after the state (and its frame) while the run is live, never the
  tile events. The state message and the history never carry tiles. A new run (a `hello` with a different `run`,
  `task` or `started`) clears the kept snapshot. `/status` shows the newest one's `song`, `t`, `speed`, `note_i` and
  tile count under `tiles`.
- Both are dropped unless the current `hello` says `"task":"tiles"` (`dropped_tiles_wrong_task`); a snapshot without a
  `tiles` list or an event without a `hit`/`miss`/`wrong` result is dropped too (`dropped_bad_tiles`, `dropped_bad_tile`).
  Episode messages are unchanged (`hits` = tiles hit), so `live/buyback.py` counts them as before.

### Rat Maze

A training run whose `hello` has `"task":"maze"` (the rat steering a marker through a maze on its screen to the cheese at
the exit, with its head; no lever press) also sends two text messages on the training channel. The site's Rat Maze
panel (`site/buyback/index.html#maze`, at `/buyback#maze`) draws the maze from them.

- `{"type":"maze","t":<sim s>,"maze_id":<int>,"w":<int>,"h":<int>,"walls":<hex string>|null,"cell":[cx,cy],"pos":[x,y],"cheese":[gx,gy],"trail":[[cx,cy],...],"bumps":<int>,"steps":<int>,"dist":<int>}`:
  a snapshot of the maze, at most 8 a second, at most **8,192 bytes**. `walls` is the layout: one hex char per cell,
  row-major, the bits N=8, E=4, S=2, W=1 set where that side of the cell is **open**; it comes only in a maze's first
  snapshot (a new `maze_id`), the others carry `"walls":null`. `pos` is continuous, in cell units (cell `(cx,cy)` spans
  `x` in `[cx,cx+1)` and `y` in `[cy,cy+1)`; row 0 is the top row); `trail` is the last cells visited (newest last);
  `dist` the shortest-path distance to the cheese in cells. A snapshot must carry an int `maze_id`, ints `w` and `h`
  (1..64) and `walls` that is null or a `w*h`-character hex string (`dropped_bad_maze`).
  - A **layout snapshot** (walls not null) is never dropped for rate and never replaced: without it a viewer cannot
    draw the maze.
  - A **position snapshot** (walls null) is treated like a tiles snapshot: each viewer gets at most **10 a second** (a
    burst of 3), the rest are dropped for that viewer (`maze_dropped_for_rate`), and a viewer holds at most one unsent
    one: a newer one replaces it at the end of its outbox (`maze_replaced_for_slow_viewers`), so the order with the
    layout snapshots and the `maze_end` events is kept.
- `{"type":"maze_end","maze_id":<int>,"result":"escaped"|"timeout","steps":<int>,"bumps":<int>,"time_s":<s>}`: one maze's
  outcome. At most **512 bytes**. Never dropped for rate, never replayed (`dropped_bad_maze_end` without an
  `escaped`/`timeout` result or an int `maze_id`).
- A late joiner gets, while the run is live and after the state (and its frame), the current maze's layout snapshot
  and then the newest snapshot (when that is a different message), never the events. The state message and the
  history never carry them. A new run clears the kept snapshots. `/status` shows the newest snapshot's `maze_id`, `w`,
  `h`, `t`, `dist`, `steps`, `bumps` and whether it carried the `layout`, under `maze`.
- Both are dropped unless the current `hello` says `"task":"maze"` (`dropped_maze_wrong_task`). Episode messages are
  as for the other tasks (`hits` = mazes escaped, `misses` = mazes that ran out of time).
- On the [maze channel](#the-maze-channel) (the Rat Maze trainer) the same messages arrive with `"channel":"maze"`
  added and the frames with the `MZ` prefix; that is the stream the `/burn` page and the burn engine read.

### Caps

- Publisher: binary frames of at most 4096 bytes that start with the float32 magic 7.0, and text of at most 64 KB
  (`hello`, `checkpoint` and `episode` at most 8 KB, since they are kept for the state message). A message over a cap
  is dropped and counted in `/status` `counts`, and the connection stays open. The same goes for bad JSON, including
  junk nested too deep to parse. Pons frames may be up to 256 KB (see above). The Procfile's `--ws-max-size 266240`
  closes anything over 260 KB at the protocol level.
- NaN and Infinity in the publisher's JSON (Python's `json` writes them; browsers reject them), and numbers too big
  for a float (`1e400`), become `null`.
- Viewers: at most 500 (`RELAY_MAX_VIEWERS` can lower it). Past the cap a viewer is accepted and closed at once with
  **1013**. One client address may hold at most 8 `/live` sockets (`RELAY_MAX_PER_IP`), so nobody can take every slot
  with idle sockets; the 9th is closed with **1013** too. The address is `X-Real-IP` (Railway's edge proxy sets it and
  a client cannot), else the **right-most** `X-Forwarded-For` entry (the one the proxy appended; the left-most is
  whatever the client sent), else the socket's peer. Loopback, private and other non-public addresses are never
  capped per address, so a local test or an unexpected proxy layout cannot lock everyone out. A viewer that sends binary is closed with 1003, one that sends a message over 512 bytes with 1009, and one
  that floods messages (more than a burst of 10, then 1 per second) with 1008. Anything else a viewer sends is ignored.
- Slow viewers: each viewer has its own bounded outbox, drained by its own task, so the publisher is never blocked.
  Frames are whole poses, so past 16 queued frames the **oldest** is dropped and a lagging viewer skips ahead. Text
  is never dropped. A viewer with more than 1024 unsent texts, or stuck on one send for 30 s, is closed with **1013**
  (try again later). It can reconnect and get a fresh state. A publisher's resync after a reconnect is about 302
  texts in one burst, well under that. Queued texts are shared, not copied per viewer, so the cap costs little memory.

## Environment

| variable | default | |
|---|---|---|
| `LABRAT_PUBLISH_TOKEN` | unset, so publishing is off | the publisher's secret, at least 16 characters. Make one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Set it on Railway and in the owner's shell. Never put it in `site/`, git or a `.env` that gets committed |
| `PORT` | set by Railway | |
| `RELAY_MAX_VIEWERS` | 500 | 1 to 500 |
| `LIVE_ORIGINS` | `*` | comma-separated list of Origins allowed on `/live` (for example `https://your-site.vercel.app`). `*` means any Origin. **Set it on the public relay** (step 2 below): otherwise any other page can embed the stream and spend the relay's viewer slots and bandwidth through its visitors' browsers |
| `RELAY_MAX_PER_IP` | 8 | `/live` sockets one client address may hold, 0 to 500 (0 = no per-address cap) |
| `SERVE_SITE` | off | `1` also serves `../site` at `/` (for local preview) |
| `SITE_DIR` | `../site` | the directory `SERVE_SITE` serves |
| `RELAY_ALLOW_TEST` | off | `1` lets a test stream (`--assume-live-for-test`) through, so the whole site can be tried locally with an old run. The page shows it with a TEST STREAM badge and its `TEST` label, never as LIVE. **Never set this on the public relay** |

## Deploy on Railway

1. New service → deploy from the GitHub repo. Under **Settings → Source**, set **Root Directory** to `relay`.
   Railway installs `requirements.txt` and starts the `web:` line of the `Procfile`.
2. **Variables:** set `LABRAT_PUBLISH_TOKEN`, and `LIVE_ORIGINS` to the site's origin(s), for example
   `https://your-site.vercel.app` (comma-separated if the site has several domains). Leave `RELAY_MAX_PER_IP` at 8.
3. **Settings:** set the health check path to `/healthz`. Keep it at **one replica** and don't use `--workers`: all
   state is in memory in one process, so a second copy would split publishers from viewers. Leave app sleeping off,
   because viewers hold open websockets.
4. **Networking → Generate Domain.** Then point the site at it in `site/config.js`:
   `var RELAY = 'wss://<that-domain>/live';` (it becomes `window.LABRAT_RELAY`).
5. Check: `https://<that-domain>/status` should show `"live": false`.

The Procfile turns off per-message compression (`--ws-per-message-deflate false`). Pose frames barely compress,
and compressing each frame once per viewer costs a lot of CPU.

**Traffic:** while live, each viewer receives 1868 bytes × 25 fps ≈ 46.7 KB/s, or about 168 MB per viewer-hour, plus
websocket framing. During a pons session each viewer also gets up to 5 masked JPEG frames a second (about 40 to 65
KB each at 1280x900), so up to about 300 KB/s per viewer for the minute or two a session lasts. When nobody is
publishing, viewers get almost nothing besides keep-alive pings.

**Measured capacity** (2026-09-24, the owner's Windows 11 PC, one relay process, viewers on the same machine, 20 s
of 25 fps rat-sized frames): 50, 150, 300 and 500 viewers each received every frame, with the relay using 5.7%,
16.4%, 39.1% and 61.1% of one CPU core. A Railway container will differ.

## Run it locally

With the site served from the relay (`site/config.js` connects a page served over http to its own origin's `/live`,
so any port works; a page opened as a file uses `ws://localhost:4720/live`, and on localhost `?relay=ws://...` in the
page URL overrides it):

```powershell
cd relay
pip install -r requirements.txt
$env:LABRAT_PUBLISH_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:SERVE_SITE = '1'
uvicorn relay:app --host 127.0.0.1 --port 4720 --ws-max-size 266240 --ws-per-message-deflate false
# open http://localhost:4720/ ; in another shell with the same LABRAT_PUBLISH_TOKEN:
# python live/publish_training.py --watch runs/ --relay ws://localhost:4720/publish
```

To try the live view without a training run, start this local relay with `$env:RELAY_ALLOW_TEST = '1'` as well and
publish an old run: `python live/publish_training.py --run runs/steer_v1 --assume-live-for-test --relay
ws://localhost:4720/publish`. Its label starts with `TEST`, and the page shows a TEST STREAM badge, never LIVE.

To try the Rat Tiles panel the same way: `python relay/tiles_demo.py --relay ws://localhost:4720/publish --duration 90`,
then open `http://localhost:4720/buyback/#piano`. It is a scripted TEST stream (the recorded replay's poses with a
scripted cursor, hits, misses and off-tile presses, not the rat's brain), refused by any relay without
`RELAY_ALLOW_TEST=1`, and it only connects to a relay on localhost.

The Rat Maze panel: `python relay/maze_demo.py --relay ws://localhost:4720/publish --duration 120`, then open
`http://localhost:4720/buyback/#maze`. The same kind of scripted TEST stream (seeded mazes from a recursive backtracker,
a scripted marker that takes wrong turns and bumps into dead ends, some mazes running out of time; not the rat's brain).

## Test

```
python relay/test_relay.py
```

This starts the relay with the Procfile's own command line on `127.0.0.1:4762` (`RELAY_TEST_PORT` overrides it), then
drives it with a fake publisher, two fast viewers, a deliberately slow viewer and a stuck one (raw sockets with a 4 KB
receive buffer), plus short-lived viewers for the caps, and then, on a fresh relay, a fake buy rig on the pons channel
next to a training publisher. It checks:

- auth, including with no token set or a short one
- state replay to late joiners, and the state size cap with big rows
- 25 fps delivery
- drop-oldest for the slow viewer without stalling anyone else
- a publisher's resync burst reaching everyone
- closing a hopelessly-behind viewer
- every cap and junk message, including the per-address cap on `/live` (with a forged `X-Forwarded-For`)
- that refused publisher handshakes are logged at most 3 times a minute
- the idle timer (the real 15 s), resuming, publisher replacement and bye
- refusing replay and test streams, and `RELAY_ALLOW_TEST=1`
- Rat Tiles: `tiles` / `tile` only in a run whose hello says `"task":"tiles"`, forwarded verbatim and in order at
  10 Hz, the 4 KB / 512 B caps and junk, at most 12 snapshots a second per viewer while every tile event gets through,
  the newest snapshot (and never an event) for a late joiner, one queued snapshot at most for a slow viewer, and no
  old snapshot after a bye or in the next run
- Rat Maze: `maze` / `maze_end` only in a run whose hello says `"task":"maze"` (and tiles messages dropped there),
  forwarded verbatim and in order at 8 Hz, the 8 KB / 512 B caps and junk (bad walls, maze_id, size, result), at most
  10 position snapshots a second per viewer while every layout snapshot and every `maze_end` gets through, the current
  layout and then the newest snapshot (never an event) for a late joiner, one queued position snapshot at most for a
  slow viewer, and no old snapshot after a bye or in the next run
- the pons channel: its own slot and token check, forwarding with `"channel":"pons"` and the `PJPG` prefix, the
  training stream unchanged next to it, the 256 KB cap and junk frames, dropping any text with a `0x` string (even
  JSON-escaped), at most 5 pons frames a second per viewer with the newest winning, late joiners, bye, a new
  session, disconnects, the 15 s idle timer, resuming, replacement, and refusing other sources and test sessions
- the maze channel: its own slot and token check next to a Rat Tiles publisher on the default channel, refusing a
  hello whose task is not `maze` (1008), every text forwarded with `"channel":"maze"` and every frame with the `MZ`
  prefix while the default channel's texts and frames stay exactly as before, `/status` `maze_channel` and the
  `maze_channel_*` counters, a late joiner getting both states in order (the default channel's replay, then the maze
  channel's), caps and junk counted apart, 50 frames of each channel side by side in order, a stuck viewer keeping
  the newest 16 `MZ` frames on a budget of their own, bye / a new run / a disconnect on either channel leaving the
  other untouched, the 15 s idle timer, resuming and replacement, and a maze-channel test stream with
  `RELAY_ALLOW_TEST=1`

It takes about three minutes (234 checks) and only stops the relay processes it started.
