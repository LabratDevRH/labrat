# Rat burns ($LABRAT): one burn an hour, sized by the Rat Maze escape rate

**Once an hour, on the hour (UTC), the engine closes the hour the rat just played in Rat Maze (the live view on
lab-rat.net/burn), counts the mazes it escaped and the mazes it ran out of time in, reads the LABRAT balance of the rat's
wallet, and sizes that hour's burn.**

    escape rate     = escapes / (escapes + timeouts)
    the hour's burn = floor(5% x the wallet's LABRAT balance x escape rate)   (whole LABRAT; at most 5% of the balance
                                                                               in any hour, a ceiling in code)

The owner asked for this ("burn 5% of the coins we hold every hour based on performance"). The wallet is the buyback
wallet `0x17852f35b597554732C706A8A9FAA534C10e1E23` (pinned in `live/buyback.py` as `BUYBACK_WALLET`; the burn engine
calls it "the rat's wallet"): the LABRAT it holds came from the rat's hourly buybacks. Rat Tiles keeps driving those on
`/buyback`; Rat Maze drives the burns on `/burn`. The buyback engine runs with `--tasks cursor,steer,tiles`, so a maze
escape counts here and nowhere else (see "The buyback engine's `--tasks`" below).

The code is `live/burn.py`, the tests are `live/burn_test.py`. The rat's brain is two trained artificial neural
networks. It is not a biological brain, and it does not understand money. This code sets the rule, and the rat's
escapes in the live view only trigger it.

**Status: DRY only; real burns are built and switched OFF.** Nothing has been burned, signed or sent. DRY is the
default and the only mode that has run. Real burns ("live bookings": each hour's burn booked by the engine, signed by
the buy rig from the rat's wallet, verified on chain by the engine) need switches on two Railway services; see "Going
live" at the end. The owner confirms the rule before the switches are flipped.

## The rate, in numbers

At the wallet's balance on 2026-09-26 (15,794,438 LABRAT, read-only), a perfect hour burns 789,721 LABRAT; an hour at
15 escapes / 1 timeout burns 740,364. The rate compounds: at a perfect escape rate all day, 24 burns of 5% leave
0.95^24 = 29% of the holding, and the buybacks add to it every hour. A day at 50% escape rate leaves 54%.

| setting | default | hard ceiling (in code) | notes |
|---|---|---|---|
| burn% (`--burn-pct`) | 5 | 5 (`BURN_BPS_HARD` = 500 bps) | can only be lowered; `burn_amount()` clamps to 5% too |
| burns per hour | 1 | 1 | one window, one burn (or one booking) |
| min burn (`--min-burn-labrat`) | 1 LABRAT | | an hour under it burns nothing |
| max burn (`--max-burn-labrat`) | none | | an optional absolute cap per burn |
| skip while gas price is above (`--max-gas-gwei`) | 1 gwei (it is about 0.03) | 10 gwei | |
| stop after this many failed attempts in a row (`--max-failures`) | 3 | | |
| the balance the burn is sized on | read (`balanceOf`) when the hour's burn is sized, right after the hour closes | | |

Gas is the wallet's own ETH: a `burn()` costs about 34,000 gas (about 0.000001 ETH at 0.03 gwei). The engine journals
whether the wallet's ETH covers it; the rig checks it before signing.

## What counts: escapes, timeouts

- **The source is the relay's public `/live` stream** (Origin `https://lab-rat.net`), and on it **only the `maze`
  channel**: the text the relay forwards from the Rat Maze trainer's publisher (`live/publish_training.py --channel
  maze`, the second trainer service `labrat-trainer-maze`) carries `"channel":"maze"`. Untagged text (the default
  training channel: Rat Tiles), `"channel":"pons"` text and every binary frame (an `MZ`-prefixed maze frame or a plain
  training frame) are ignored. `--channel training` counts the untagged channel instead (DRY tests only; live bookings
  refuse it).
- **A course ends with** `{"type":"episode","n","presses","hits","misses","fell"}`: `hits` = mazes escaped, `misses` =
  mazes timed out (`maze_env.py`; a course ends at its first timeout or after `MAZE_MAX_HITS` = 4 escapes), `presses` =
  `hits + misses`. The engine books it into the UTC hour it arrives in.
- **Only a live training session of task `maze` counts.** The hello must have `source` `"training"` and task `"maze"`
  and must not be a TEST stream; `--accept-test-streams` lifts that for DRY tests only. The last episode in the relay's
  `state` message counts only while `state.live` is true. A Rat Tiles hello on the maze channel is not counted.
- **Nothing is counted twice.** Each course is keyed by `run | task | hello.started | n` (as the buyback engine keys
  its attempts). A reconnect, a `state` that repeats the last course, the publisher resending its hello, or a restart
  (the keys come back from the journal) cannot count it again. A rejected course number is spent as well.
- **Bad messages are rejected and journalled:** more than 4 escapes in a course, counts that do not add up, anything
  that is not a non-negative integer.
- **`maze_end` events and frames never move money.** `maze_end` only feeds the unconfirmed tally of the course in
  progress (`this_course` in the status); frames are only counted.
- **Undercounting is possible, overcounting is not.** Courses that end while the engine is offline are missed, and a
  jump in `n` is journalled as a gap.

## The hour

- **Windows are aligned to the UTC hour.** At the first tick after the hour ends, the engine journals a `window` record
  (escapes, timeouts, courses, escape rate, whether a burn is due) and, if the hour had escapes, sizes the burn at
  once: the token check (below), the gas price cap, then `balanceOf(wallet)` and `totalSupply()` read right then, then
  `floor(burn% x balance x escape rate)` rounded down to a whole LABRAT and cut to the 5% ceiling (and `--max-burn-labrat`).
- **DRY** simulates that exact burn from the wallet with `eth_call` and `eth_estimateGas` (no state override: the tokens
  must really be there) and journals it as `burn` with `simulated: true`. **Live bookings** journal it as a `booking`
  for the buy rig instead. Neither ever signs.
- **No burn for:** an hour with no courses (`no mazes in the hour`), no escapes, an empty wallet (`nothing to burn`), or
  a burn under the minimum (`under the minimum burn`); all recorded as `window_noburn` or in the window's note.
- **A passing problem retries until the next hour closes:** a chain read that failed (60 s), the gas price over its cap
  (300 s), a failed simulation (120 s; three failed attempts in a row stop the engine). The next hour's close replaces a
  burn that never happened: `burn_expired` ("the next hour closed first").
- **Restarts.** The open hour survives a restart (its courses are rebuilt from the journal). An hour that ended while
  the engine was down is closed on the restart; only the hour that just ended can still burn, an older one closes late
  without a burn.
- **The rounding** is down to a whole LABRAT (`TOKEN_STEP_WEI` = 10^18), so the amount shown is the amount burned.

## How it burns: the method

Checked read-only on 2026-09-26 (`python live/burn.py --check`, block 72,894,839): the token's bytecode (3,248 bytes, no
proxy) carries `burn(uint256)` (selector `0x42966c68`), `burnFrom`, `transfer`, `balanceOf` and `totalSupply`. A
simulated `burn(1 LABRAT)` from the wallet passes (34,033 gas); a simulated `transfer(0x…dEaD, 1 LABRAT)` passes too
(51,633 gas); a transfer to the zero address reverts with `ERC20InvalidReceiver` (OpenZeppelin v5). So:

- **`--method auto` (the default)** reads the bytecode before every burn: with the burn selector present the burn is
  the token's own **`burn(amount)`**, which lowers `totalSupply` and emits `Transfer(wallet -> 0x0, amount)`. Without it
  the fallback is **`transfer(0x000000000000000000000000000000000000dEaD, amount)`** (`method: "dead"`). No code at the
  token, or neither selector, stops the engine.
- `--method burn` insists on `burn()` (stops when the bytecode lacks it); `--method dead` forces the dead address (DRY
  only; live bookings refuse it while the token has `burn()`).
- The verification of a reported burn accepts **either** form: a `burn(amount)` with `Transfer(wallet -> 0x0)`, or a
  `transfer(dEaD, amount)` with `Transfer(wallet -> dEaD)`, of exactly the booked amount. The executed record keeps the
  method the chain shows.

## DRY (the default)

- Never reads `.env`, never builds a signer, holds no key. Its RPC object is `buyback.ReadRpc`: every method that is
  not a read raises `SendRefused`, and the engine refuses to be built on a `LiveRpc`.
- **Append-only journal** `runs/burn/journal.jsonl` (`--journal-dir`), one fsync'd line per record:
  `start`, `check`, `course` (every counted course, with its window, escapes and timeouts), `gap`, `rejected`, `window`,
  `burn` (simulated), `burn_skip`, `burn_expired`, `window_noburn`, `burn_failed` / `batch_failed`, `stop`,
  `stop_cleared`, `end`; live bookings add `booking`, `executed`, `booking_expired`. A restart rebuilds the state.
- `python live/burn.py --check` prints, read-only: the token check and method, the gas price, the wallet's LABRAT and
  ETH, the total supply, the burn a perfect hour would size now, and a simulated burn of 1 LABRAT with its gas.

## The public status

`runs/burn/status.json` and, with `--status-port`, `GET /status` (CORS `*`, no-store), rewritten on every change and
at least every 10 s. It never contains a wallet or contract address; the only `0x` strings are the hashes of verified
burns. Exact shape:

```
{
 "mode": "DRY" | "LIVE",
 "label": "DRY - simulated, not executed" | "LIVE - real burns from the rat's wallet, signed by the buy rig",
 "simulated": true | false,                         (true in DRY: every burn shown is simulated)
 "rule": "<the rule in plain language>",
 "burn_pct": "5", "burn_pct_max": "5", "task": "maze", "channel": "maze",
 "method": "burn" | "dead" | null,  "method_text": "...",
 "this_hour":  {"start", "end", "escapes", "timeouts", "attempts", "escape_rate" (0..1 or null),
                "wallet_balance" ("15794438.08" LABRAT, or null before the first read), "wallet_balance_at",
                "projected_burn" ("740364": burn% x the last balance read x this hour's escape rate), "projected_pct"},
 "last_hour":  {"start", "end", "escapes", "timeouts", "attempts", "escape_rate", "late", "note",
                "burn": {"state": "due" | "simulated" | "booked" | "burned" | "expired" | "none",
                         "label": "Simulated burn · not executed" | "Booked · not executed yet" |
                                  "Burned · verified on chain" | "Not executed" | null,
                         "simulated", "amount", "amount_wei", "balance", "share_pct", "method",
                         "tx", "block", "explorer", "at", "note"}}          (null until the first hour closes)
 "totals":     {"burned" (LABRAT verified on chain), "burns" (their count),
                "simulated_burned", "simulated_burns", "booked" (outstanding bookings),
                "escapes", "timeouts", "courses", "total_supply", "since"},
 "recent":     [ {"window", "at", "state", "label", "simulated", "amount", "amount_wei", "balance", "share_pct",
                  "escape_rate", "escapes", "timeouts", "method", "tx", "block", "executed_at", "explorer", "note"
                  [, "signable_until"]} ... ],                            (newest first, the last 12)
 "bookings":   [ the entries above with state "booked" ],                (always [] in DRY)
 "next_burn_in_s", "next_burn_note", "next_burn_at",
 "this_course": {"escapes", "timeouts", "maze_id", "w", "h", "note": "unconfirmed until the course ends"},
 "relay":      {"connected", "live", "run", "task", "channel", "counting", "test_stream"},
 "source":     {"public_relay", "accept_test_streams", "test"},
 "caps":       {"burn_pct", "burn_pct_hard", "burns_per_hour", "min_burn", "max_burn", "max_gas_gwei"},
 "explorer":   "https://robinhoodchain.blockscout.com/tx/",
 "stopped":    null | "<short reason>",
 "updated":    ISO time
}
```

`next_burn_note` is a short fixed phrase: `waiting for the hour`, `due`, `chain read failed; retrying`, `gas price over
the cap`, `retrying after a failed attempt`, `held back; retrying`, `stopped`. **Site rule:** a DRY status
(`simulated: true`) is shown as "Simulated" everywhere and the banner stays "Burns start soon"; `mode: "LIVE"` shows
"Burns live"; an entry is called a burn only with `state: "burned"` (it then has `tx` and `explorer`); `booked` is
"booked, not executed yet", `expired` is "not executed".

`GET /bookings` returns `{"mode", "simulated", "channel", "method", "bookings": [...], "updated"}`: the outstanding
bookings only (what the buy rig executes). `GET /healthz` returns `{"ok": true}`.

## Live bookings: real burns (built, switched off)

Three parts, each with its own switch; with any switch off, that part behaves exactly as in DRY.

1. **The engine books** (`live/burn.py`, service `labrat-burn`). Only with the `--live-bookings` flag AND the variable
   `BURN_LIVE_BOOKINGS=1`; either alone logs "only half switched on" and stays DRY. It also refuses to start with
   `--window-s`, `--accept-test-streams`, another relay or origin, `--channel` other than `maze`, or `--method dead`.
   Then:
   - the status says `mode: "LIVE"`, `simulated: false`, and the journal is `<journal-dir>/journal_bookings.jsonl`;
   - each hour closes as before, and after the token check, the gas price cap, the balance read and a simulation of the
     exact burn from the wallet, the burn is journalled as a `booking` and listed under `bookings` and `recent` with
     `state: "booked"`, `window` (the hour, e.g. `2026-09-26T14:00:00Z`), `amount` (LABRAT) and `amount_wei` (exact),
     `method` and `signable_until` (the window's start + 2 h: the rig signs at most one hour after the window ends).
     Nothing is burned yet;
   - **`POST /burn_report`** (`Authorization: Bearer <BURN_RIG_TOKEN>`, the token from the PROCESS environment, 24+
     characters; JSON of at most 2,048 bytes) takes the runner's report of an executed burn:
     `{"window": "2026-09-26T14:00:00Z", "tx": "0x<64 hex>", "amount": "740364"}` (LABRAT as a decimal string; or
     `"amount_wei": "<digits>"`, or both when they agree), optionally `"method": "burn"|"dead"` and
     `"signed_at": ISO`. The engine reads the chain (read-only) and counts the burn only if the receipt has status 1,
     the transaction is from the rat's wallet to the token with value 0, the calldata is `burn(amount)` or
     `transfer(dEaD, amount)` of exactly the booked amount, a LABRAT `Transfer` from the wallet to `0x0` (burn) or to
     `dEaD` (transfer) of exactly that amount is in the logs, it was mined after the booking, and the hash was never
     counted before. Then the booking becomes `state: "burned"` with `tx`, `block`, `explorer` and the label "Burned ·
     verified on chain", and `totals.burned` / `totals.total_supply` follow the chain. Responses: `200 {"ok": true,
     "window", "tx", "already": false|true, "label"}` (a repeated report of the same hash is idempotent), `400` (bad
     fields, or `amount` not the booked amount), `401`, `404` (no booking for that hour), `409` (the hour already has
     another executed burn, or the hash is counted for another hour), `422` (does not verify), `503` (no receipt on the
     engine's RPC yet, or the chain unreadable: report again at the next poll). A DRY engine answers `404` (no
     endpoint) and refuses reports in code (`409`);
   - a booking the rig can no longer sign plus a margin (`BOOKING_DEAD_S` = 2 h 15 min after the window's start) is
     released as `booking_expired` ("not executed in time"). A late execution that does verify is still counted: the
     chain is the truth. The next hour closing does NOT expire an outstanding booking;
   - a stop (three failed simulations in a row, the token check failing) survives restarts; it is cleared with
     `BURN_CLEAR_STOP=<its id>` (the log line "STOPPED: ... (stop id ...)" gives the id).
2. **The runner executes** (`live/buyrig_runner.py --burn-status-url <labrat-burn>/status`, service `labrat-buyrig`:
   the only place the wallet's key exists). It polls `GET /bookings` (or `status.bookings`) and, per booked window,
   runs ONE burn transaction through `live/buyrig_live.py`'s journal: a window kind `burn`, the same `reserved ->
   signed -> broadcast` records, the same nonce accounting, never concurrent with a buy, gated by `BURN_LIVE=1` +
   `BURN_CONFIRM=LABRAT` + the same key / wallet / chain checks as the buys. The transaction is exactly `to` = the
   token, `value` = 0, `data` = `0x42966c68` + `uint256(amount_wei)` (method `burn`; for `dead`: `0xa9059cbb` +
   `dEaD` + `uint256(amount_wei)`), from the rat's wallet, chain 4663. Before signing the rig checks on its own:
   `amount_wei` equals the booking's, `amount_wei <= balanceOf(wallet) x 5%` at that moment (the engine's ceiling,
   re-applied), the window is still signable, the wallet's ETH covers the gas. Then it reports
   `{window, tx, amount}` to `POST /burn_report`; a 503 is retried at its next poll. No pons page is involved.
3. **The site** (`site/burn/index.html`) takes the banner from the engine's mode: DRY keeps "Burns start soon"; LIVE
   shows "Burns live". A verified burn (`state: "burned"`, with its hash) is shown as a burn with a "View transaction"
   link; a booked or expired one as "booked, not executed yet" / "not executed", never as burned; DRY's stay
   "Simulated".

## The buyback engine's `--tasks`

So that a maze escape drives burns only, the buyback engine stops counting the maze task. In
`~/claude/_deploy/labrat-buyback/Procfile` its command carries `--tasks cursor,steer,tiles` (the code's default is
`cursor,steer,tiles,maze`; `live/buyback.py` is unchanged). Its public rule then no longer mentions Rat Maze, and a
maze hello on the default channel logs "not counted (task maze is not counted)". Redeploy `labrat-buyback` for it to
take effect. Until the second trainer streams on the maze channel, nothing drives the burn engine and every hour
closes with "no mazes in the hour".

## Going live (real burns)

Two Railway services: **`labrat-buyrig`** (the runner and the rig; already deployed for the buybacks, holds the key)
and **`labrat-burn`** (the engine; a new folder like `~/claude/_deploy/labrat-burn`: `Procfile`, `requirements.txt`
and `.python-version` copied from `_deploy/labrat-buyback`, `launcher.py`, `live/buyback.py` and `live/burn.py`, a
volume at `/data`). Order: the rig first (with a DRY engine it has nothing real to execute), the engine second (from
then on each hour with escapes books a real burn).

1. **Deploy the new code, switches still off.**
   - `labrat-buyback` → Procfile with `--tasks cursor,steer,tiles`, redeploy (buybacks are unchanged; maze no longer
     counts there).
   - `labrat-burn` (new service): Procfile
     ```
     web: python live/burn.py --status-host 0.0.0.0 --status-port $PORT --journal-dir /data/burn
     ```
     `GET /status` says `mode: "DRY"`, `simulated: true`; lab-rat.net/burn shows "Burns start soon" and every burn
     "Simulated".
   - `labrat-buyrig` → the runner with `--burn-status-url https://<labrat-burn>/status` (its log must say the burn
     side is off), redeploy.
   - `labrat-trainer-maze` (the second trainer, `TRAIN_TASK=maze`, `LABRAT_RELAY_CHANNEL=maze`) streams on the maze
     channel; the burn engine's log says `training session ... (maze, channel maze): live: counting escapes`.
2. **Watch DRY for a day.** Each hour with escapes journals a simulated burn; `status.recent` shows amounts, the
   escape rate and the balance they were sized on. The owner confirms the 5% rule (or sets `--burn-pct` lower).
3. **`labrat-buyrig` → Variables**: `BURN_LIVE` = `1`, `BURN_CONFIRM` = `LABRAT`, `BURN_RIG_TOKEN` = a new random
   token of 24+ characters (it may equal `BUYBACK_RIG_TOKEN`). `BUYBACK_RH_KEY`, `BUYRIG_LIVE` and the volume at `/data`
   are already there. Redeploy. The runner log must say the burn side is on.
4. **`labrat-burn` → Variables**: `BURN_LIVE_BOOKINGS` = `1`, `BURN_RIG_TOKEN` = the same token. **Procfile**: add
   `--live-bookings`:
   ```
   web: python live/burn.py --status-host 0.0.0.0 --status-port $PORT --journal-dir /data/burn --live-bookings
   ```
   Redeploy. `GET /status` says `mode: "LIVE"`, and lab-rat.net/burn shows "Burns live". The next hour's close with
   escapes books the first real burn (`bookings[0].state: "booked"`).
5. **Watch the first burn.** Within a minute or two of the hour: the runner logs the burn transaction and `engine 200`;
   the engine logs `LIVE: the burn for the hour ... verified on chain`; the site shows it with "View transaction";
   `totals.total_supply` fell by the amount.

**How to stop.**
- **Stop signing now:** `labrat-buyrig` → set `BURN_LIVE` = `0` → redeploy. The rig signs no more burns (the buybacks
  keep their own switch); a booked burn is then not executed and expires. A transaction already signed cannot be
  unsigned: the rig's next start finishes it from its journalled bytes, and it is reported then.
- **Back to DRY:** then `labrat-burn` → delete `BURN_LIVE_BOOKINGS` (or remove `--live-bookings`) → redeploy. The
  status says DRY again and the site "Burns start soon"; the bookings journal stays on the volume, and switching back
  on later resumes it.
- **Lower the rate:** `--burn-pct 2` (or any value under 5) in the Procfile; above 5 the engine refuses to start.
- **LIVE stopped itself:** the engine's log says `STOPPED: ... (stop id <id>)`; after checking, set
  `BURN_CLEAR_STOP` = `<id>` on `labrat-burn` and redeploy. A later stop has a new id.
- **Stop counting altogether:** stop the `labrat-trainer-maze` service (no maze stream, no escapes, no burns) or the
  `labrat-burn` service.

## Tests

`python live/burn_test.py` runs 26 mocked tests (no network except 127.0.0.1, `.env` trapped, signing tripwired, the
real chain tripwired; the fake token has no send method at all, and every test asserts the engine used read methods
only). They cover: the arithmetic (floor, whole LABRAT, the 5% ceiling with any bps, a lower `--burn-pct`, the absolute
cap, the minimum, the real wallet's figure), the calldata and its decoding, the counting (the maze channel only,
courses de-duplicated across a reconnect / resync / restart, TEST streams, a tiles hello on the channel, `maze_end` and
frames feeding nothing that moves money), the hour (the balance read when the burn is sized, no courses / no escapes /
empty wallet / under the minimum, a restart mid-hour, a late close, a burn that expires when the next hour closes
first, the retry after a failed read), the method following the bytecode (burn, dead, no code), the gas price cap and
the three-failures stop, the status (fields, labels, no address, no hex at all in DRY), `main()` end to end in DRY over
a real websocket (the untagged Rat Tiles text ignored, `/bookings` empty, `/burn_report` absent), and live bookings:
the switch and every refused configuration, `main()` in live bookings, an hour booked not burned, `/bookings`, a
verified burn counted (idempotent, replayed after a restart; both `burn()` and the dead address), 18 wrong reports
refused with the right status codes, the unreadable chain (503), expiry and a late execution, the next hour not
expiring an outstanding booking, a stop cleared only by its id, and DRY unchanged.

The suite picks free ports in 4930-4959 at start (`BURN_TEST_RELAY_PORT` / `BURN_TEST_STATUS_PORT` override them).

## Not settled

- The relay's maze channel is being built alongside this engine: the engine expects the relay to tag every text of
  that channel (`state`, `hello`, `episode`, `bye`, `idle`, `maze`, `maze_end`) with `"channel":"maze"`. An untagged
  `idle` or `bye` for the maze publisher is ignored here, which only affects the `relay.live` flag in the status, never
  a count.
- The balance the burn is sized on is read within minutes of the hour, so whether it includes the same hour's buyback
  depends on which service reaches the chain first. Either is within the rule; the journal keeps the exact balance.
- The bytecode selector check is a heuristic (a 4-byte string could appear in data); the simulated burn before every
  booking is what proves `burn()` works, and a reverting burn is a failed attempt (three stop the engine).
- Live bookings have no single-instance lock: run one engine (Railway runs one instance with a volume). The rig's
  exclusive per-window lock prevents a second transaction for an hour, and the engine counts one executed burn per hour.

## Run it

```
python live/burn.py                                   # DRY: listen to the maze channel, count, simulate each burn
python live/burn.py --status-port 4930                # + the public status on http://127.0.0.1:4930/status
python live/burn.py --burn-pct 2                      # DRY at 2 %
python live/burn.py --check                           # read-only: the token, the wallet, a simulated burn of 1 LABRAT
python live/burn_test.py                              # the mocked tests
```
