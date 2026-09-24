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
| `WS /publish` | the publisher. Needs `Authorization: Bearer <LABRAT_PUBLISH_TOKEN>`. Only one publisher at a time |
| `WS /live` | public viewers. They can only receive. A viewer may send `ping` (or `{"type":"ping"}`) and gets `{"type":"pong","t":<unix s>}` back |
| `GET /status` | JSON with `live`, `hello`, `metrics` (the last log.jsonl row), `viewers`, and also `checkpoint`, `episode`, `publisher` (`in_session`, `quiet_s`), `history_rows` (kept), `state_rows` (in the state message), `allow_test_streams`, `frames_in`, `fps_in`, `max_viewers`, `max_viewers_per_address`, `counts`. Readable cross-origin, with no IP addresses |
| `GET /healthz` | `{"ok":true}` |
| `GET /` | the site when `SERVE_SITE=1`, otherwise a small JSON pointer |

### What a viewer receives

1. `{"type":"state","live":bool,"hello":{...}|null,"checkpoint":{...}|null,"episode":{...}|null,"history":[up to 300 rows]}`.
   If the stream is live, the newest binary frame follows straight after. The state message stays under 60,000
   bytes, because `site/js/live.js` ignores text over 65,536. Real `log.jsonl` rows are about 120 to 215 bytes, so
   300 rows fit. If they don't, the state carries the newest rows that do.
2. Everything the publisher sends, unchanged and in order: `hello`, `metrics`, `checkpoint`, `episode`, `bye` and
   binary frames (1868 bytes each for the rat: `(12 + 65*7) * 4`).
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
full totals are in `/status` `counts` (`publishers_refused_auth`, `_busy`, `_disabled`).

### Caps

- Publisher: binary frames of at most 4096 bytes that start with the float32 magic 7.0, and text of at most 64 KB
  (`hello`, `checkpoint` and `episode` at most 8 KB, since they are kept for the state message). A message over a cap
  is dropped and counted in `/status` `counts`, and the connection stays open. The same goes for bad JSON, including
  junk nested too deep to parse. The Procfile's `--ws-max-size 131072` closes anything over 128 KB at the protocol level.
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
websocket framing. When nobody is publishing, viewers get almost nothing besides keep-alive pings.

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
uvicorn relay:app --host 127.0.0.1 --port 4720 --ws-max-size 131072 --ws-per-message-deflate false
# open http://localhost:4720/ ; in another shell with the same LABRAT_PUBLISH_TOKEN:
# python live/publish_training.py --watch runs/ --relay ws://localhost:4720/publish
```

To try the live view without a training run, start this local relay with `$env:RELAY_ALLOW_TEST = '1'` as well and
publish an old run: `python live/publish_training.py --run runs/steer_v1 --assume-live-for-test --relay
ws://localhost:4720/publish`. Its label starts with `TEST`, and the page shows a TEST STREAM badge, never LIVE.

## Test

```
python relay/test_relay.py
```

This starts the relay with the Procfile's own command line on `127.0.0.1:4723`, then drives it with a fake publisher,
two fast viewers, a deliberately slow viewer and a stuck one (raw sockets with a 4 KB receive buffer), plus short-lived
viewers for the caps. It checks:

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

It takes about a minute (94 checks) and only stops the relay processes it started.
