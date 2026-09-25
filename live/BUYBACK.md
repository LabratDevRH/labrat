# Rat buybacks ($LABRAT): one buy an hour, sized by the hit rate

**Once an hour, on the hour (UTC), the engine closes the hour the rat just played in the live view, counts its hits,
misses and wrong clicks, and books that hour's $LABRAT buy: the hourly budget x the hit rate, cut to the caps.**

    hit rate = hits / (hits + misses + wrong clicks)
    the hour's buy = hourly budget x hit rate   (rounded down to 0.000001 ETH, cut to the per-buy, hourly, daily
                                                 and total caps; under the 0.0001 ETH minimum it buys nothing)

The owner asked for this: buying at every tile would spend most of each tiny buy on gas, so the engine buys once an
hour after checking the hit rate and the misses, and the owner allocates the hourly budget when it is ready. **Until the
budget is set, every hour is still recorded and runs a simulated PREVIEW buy of a nominal 0.001 ETH x the hit rate**,
marked "preview" in the status and the journal.

The live view is the newest saved training checkpoint, playing in its own simulation (`live/publish_training.py`); the
training workers themselves are not watched. The code is `live/buyback.py`, the tests are in `live/buyback_test.py`,
and the chain research behind it is in `live/BUYBACK_RESEARCH.md`.

The rat's brain is two trained artificial neural networks. It is not a biological brain, and it does not understand
money. This code sets the rules, and the rat's hits in the live view only trigger them.

**Status: DRY runs; real buys are built and switched OFF.** Nothing has been claimed, bought, signed or sent. DRY is the
default and the only mode that has ever run. Real buys ("live bookings": each hour's buy booked by the engine, clicked
through on pons by the rat and signed from the buyback wallet by the buy rig, verified on chain by the engine) need
switches on both Railway services; see "Going live" at the end. The older fee-funded LIVE path stays paused.

## Who pays: the buyback wallet

The owner funds a separate buyback wallet by hand: `0x17852f35b597554732C706A8A9FAA534C10e1E23`, pinned in the code as
`BUYBACK_WALLET`. Every DRY buy is simulated from it. The engine holds no key for it (or for any wallet) and has no key
handling for it. Only the buy rig can sign for it, and only with live bookings switched on ("Going live").

- **The simulation gives it enough ETH.** A state override sets its balance to the larger of its real balance and the
  buy plus its worst-case gas. The journal keeps its real balance and whether that would have covered the buy
  (`buyer_balance_wei`, `buyer_funded`).
- **No creator-fee claim in a DRY buy.** The launch wallet's creator fees are no longer what pays for the buys, so DRY
  never simulates a claim. The fee-claim code stays for the LIVE path, which is paused.
- **The wallet address is never in the public status.** A test checks that no `0x` + 40 hex string appears in it.

## What counts: hits, misses, wrong clicks

- **The source is the relay's public `/live` stream**, read with Origin `https://lab-rat.net`. Each attempt ends with
  `{"type":"episode","n","presses","hits","misses","fell"}`. The engine books it into the hour window it arrives in.
- **The steering and cursor tasks:** hits are clicks on the lit target, and misses are clicks off it
  (`cursor_env.CursorEnv`). There are no wrong clicks.
- **Rat Tiles (`tiles_env.py`):** one attempt is one song, with up to `TILES_MAX_HITS` (64) hits. A hit is a tile
  tapped in time, a miss is a tile that slid past untapped, and a wrong click is a press with no tile to tap. The
  engine reads them like this:
  - The episode's `misses` are the wrong clicks (`presses = hits + misses`), and its optional `missed` field holds the
    tiles that slid past.
  - When a publisher does not send `missed`, the tiles that slid past are counted from its
    `{"type":"tile","result":"miss"}` events for that song, each tile id once. That count is only trusted for a song the
    engine followed from its start. A song it joined midway (the relay's `state` message on connect, or a publisher
    resync) keeps its hits in the totals but stays out of the hour's hit rate, because its unknown misses could only
    raise the rate.
  - An episode that carries `wrong` instead is read as `misses` = tiles that slid past and `wrong` = wrong clicks
    (`presses = hits + wrong`).
- **The lever task** has no target. Its "hit" is a clean press, at most one per attempt, so by default only the cursor,
  steering and Rat Tiles tasks count. `--tasks lever,cursor,steer,tiles` opts it in, and the public rule then says "in
  the lever task, each clean press is a hit".
- **Only a live training session counts.** The hello must have `source` set to `"training"` and must not be a TEST
  stream; `--accept-test-streams` lifts that for DRY tests only. The last episode in the relay's `state` message counts
  only while `state.live` is true.
- **Nothing is counted twice.** Each attempt is keyed by `run | task | hello.started | n`. A reconnect, a `state` that
  repeats the last episode, the publisher resending its hello, or a restart (the keys come back from the journal)
  cannot count it again.
- **Bad messages are rejected and journalled:** too many hits for the task, counts that do not add up, a `missed` or
  `wrong` that is not a non-negative integer, or more than 64 missed tiles in a song.
- **Frames never move money.** Binary frames only feed an unconfirmed tally of the attempt in progress.
- **Undercounting is possible, overcounting is not.** Attempts that end while the engine is offline are missed, and a
  jump in `n` is journalled as a gap.

## The hour

- **Windows are aligned to the UTC hour.** At the first tick after the hour ends, the engine journals a `window` record
  with the hour's hits, misses, wrong clicks, attempts and hit rate, the budget it used (or the preview amount), the
  buy it booked, the cap that cut it, and a note.
- **The buy is tried at once.** The pinned chain checks run first, then the simulation.
  - A passing problem (gas price over the cap, a chain read that failed, a failed attempt) retries the buy until the
    next hour closes. That replaces it: `buy_expired`.
  - A final problem (gas would be over 20 % of the buy, or a cap reached) drops that hour's buy: `window_nobuy`.
  - Three failed attempts in a row stop the engine.
- **No buy for:** an hour with no attempts, an hour with no hits, or an hour whose buy would be under the 0.0001 ETH
  minimum (below a 10 % hit rate at a 0.001 ETH budget).
- **Restarts.** The open hour survives a restart, because its attempts are rebuilt from the journal. An hour that ended
  while the engine was down is closed on the restart. Only the hour that just ended can still buy; an older one closes
  late without a buy ("closed late; the engine was not running at the hour").
- **The rounding** is down to 6 decimals (0.000001 ETH): pons's Review dialog displays the amount to 6 decimals, and
  the buy rig refuses to sign when that display differs from the booked amount.
- **Previews** (budget not set) are cut to the per-buy cap only. They never use up the hourly, daily or total cap or
  the gas caps, so the caps are intact when the budget is set.
- **A journal from before the hourly rule** replays. Its hits and buys still count, and its pending per-hit amount is
  dropped: those hits show as "counted, bought nothing".

| setting | default | hard ceiling (in code, `HARD`) | on Railway (Procfile) |
|---|---|---|---|
| hourly budget (`--hourly-budget-eth`, or the process env `BUYBACK_HOURLY_BUDGET_ETH`) | unset = preview | 0.1 | 0.1 |
| preview amount while unset (`--preview-budget-eth`) | 0.001 ETH x hit rate | 0.01 | |
| min buy (an hour under it buys nothing) | 0.0001 ETH | | |
| max per buy (the budget is cut to it) | 0.001 ETH | 0.1 | 0.1 |
| max per hour window / per rolling day | 0.002 / 0.01 ETH | 0.1 / 2.4 | 0.1 / 2.4 |
| max total (per journal) | 0.05 ETH | 5 | 5 |
| slippage (min out = simulated out minus this) | 3 % | 10 % | |
| router deadline | 180 s | | |
| skip while gas price is above | 1 gwei (it is about 0.036) | 10 gwei | |
| no buy for an hour whose gas is more than | 20 % of it | 50 % | |
| gas per rolling day / total (previews left out) | 0.002 / 0.01 ETH | 0.01 / 0.05 | |
| stop after this many failed attempts in a row | 3 | | |

With live bookings the caps count the buys verified on chain plus the bookings still outstanding, and the buy rig
applies the same hard ceilings to what it signs (0.1 per buy, 2.4 signed per rolling day, 5 ever).

**Setting a budget above 0.001 ETH also needs `--max-buy-eth`.** Otherwise the per-buy cap cuts every hour's buy to
0.001 ETH, and the engine logs that at startup. `max_buy` may not exceed the hourly, daily or total cap, so the owner's
budget of 0.1 runs with `--max-buy-eth 0.1 --max-hour-eth 0.1 --max-day-eth 2.4 --max-total-eth 5` (the Procfile).

The daily cap is a rolling day: at 0.001 ETH a perfect hour, the default 0.01 ETH/day fills after ten hours.

## Where it buys

$LABRAT filled its 4.2 ETH curve and graduated at block 71688341, about 7 minutes after launch.

- **`--venue auto` (the default)** buys on the curve while the coin is on it (phase 0, `graduated()` false). After
  graduation (phase 2, `graduated()` true) it buys in the coin's pinned Uniswap v4 pool through the pons Universal
  Router. Anything else stops the buys. Today every buy goes to the pool.
- **`--venue curve`** keeps the literal "stop if the curve graduated" rule, so it stops at once.
- **`--venue pool`** buys only in the pool.

The pool is pinned as tightly as the curve. The PoolKey (ETH, LABRAT, fee 0, tick spacing 200, the pons hook) must hash
to the pinned poolId, the factory's `memeHook()` must be the pinned hook, and the record's fee and tick spacing must
match. Before every buy, `token.curve()` and the factory record must match: the token, the curve, the creator-fee
recipient (the launch wallet), the ETH pair, `exists`, and pons's own buyback switch still off.

## DRY (the default)

- **It simulates each buy from the buyback wallet on the real chain:**
  - the pons v4 quoter's `quoteExactInputSingle`
  - the exact router `execute(0x10, [0x06 0x0c 0x0f], deadline)` with min out = quote − 3 %
  - the same call with min out = the full quote
  - `eth_estimateGas`
- **It never reads `.env`, never builds a signer, and can only read.** Its RPC object is a `ReadRpc`, and every method
  that is not a read raises `SendRefused`.
- **It keeps an append-only journal**, `runs/buyback/journal.jsonl`, one fsync'd line per record:
  - `start`, `check`, `hit` (every counted attempt, with its window and its hits, misses and wrong clicks), `gap`,
    `rejected`
  - `window`, `buy`, `buy_skip`, `buy_expired`, `window_nobuy`, `*_failed`, `stop`, `pons_session`, `end`

  A restart rebuilds the whole state from this file.

## The public status

`runs/buyback/status.json` and, with `--status-port`, `http://<host>:<port>/status` (CORS `*`, no-store). It is
rewritten on every change and at least every 10 s. It never contains a wallet address, a creator-fee amount or
transaction data. The existing fields stay:

- `mode`, `label`, `rule`, `tasks`
- `hits` (`counted`, `attempts_with_hits`, `in_buys`, `pending` = this hour + a booked buy, `over_caps` = counted,
  bought nothing)
- `pending` (`eth` = the booked buy not yet done, `hits`), `this_attempt`, `relay`, `source`
- `buys` (with `previews`; each `recent` entry adds `preview`, `window`, `hit_rate` and, for a preview, `label`)
- `next_buy_in_s`, `next_buy_note`, `caps`, `stopped`, `updated`

`per_hit_eth` and `caps.max_pending_eth` are `null` now: there is no per-hit amount. The hourly rule adds:

```
"window":      {start, end, hits, misses, wrong, hit_rate (0..1 or null), attempts, preview, projected_eth}
"last_window": {start, end, hits, misses, wrong, hit_rate, attempts, preview, budget_eth, preview_eth, amount_eth,
                cap, late, note, buy: {state: due|simulated|bought|none|expired, note, at, eth_in, labrat_out,
                simulated, preview, venue, pons}}          (null until the first hour closes)
"next_buy_at": ISO time (the end of this hour, or the retry time of a booked buy)
"budget":      {hourly_eth: null|"0.002", rule: "hourly budget x hit rate", set, preview_eth, funding}
```

`next_buy_note` is a short fixed phrase, such as "waiting for the hour", "due", "gas price over the cap",
"daily cap reached", "total cap reached" or "stopped".

## The rat on pons

For each simulated buy the engine books (one an hour, previews included), `live/buyrig_runner.py` has the rat click
through pons's BUY flow for that amount on the real $LABRAT page (`live/buyrig.py`). pons builds the transaction, and
the rig checks it field by field, simulates it with `eth_call` and refuses to sign. The runner reads the buys from
`buys.recent` (unchanged: `at`, `eth_in`, `simulated`, `hits_covered` = the hour's hits) and reports each session with
`POST /pons_session`. That endpoint opens only in DRY (or live bookings) with `BUYBACK_RIG_TOKEN` of 24+ characters in
the process environment. The report is validated field by field and shown on that buy, and on `last_window.buy`, as
"Simulated buy · clicked by the rat on pons". It never changes a cap, the booked amount or any figure the engine
computed. `live/buyrig_test.py` covers it.

## Live bookings: real buys (built, switched off)

Three parts, each with its own switch; with any switch off, that part behaves exactly as in DRY.

1. **The engine books** (`live/buyback.py`, service `labrat-buyback`). Only with the `--live-bookings` flag AND the
   variable `BUYBACK_LIVE_BOOKINGS=1`; either alone logs "only half switched on" and stays DRY. It also refuses to start
   without a set hourly budget, with `--window-s`, `--accept-test-streams`, another relay or origin, `--venue curve`, or
   the paused LIVE flags. Then:
   - the status says `mode: "LIVE"` and `label: "LIVE - real buys from the buyback wallet, clicked by the rat on pons"`,
     and the journal is `<journal-dir>/journal_bookings.jsonl` (real money only; DRY's simulated buys never count);
   - each hour closes as before, and after the pinned chain checks, the gas price cap and a simulation of the route from
     the buyback wallet, its buy is journalled as a `booking` and listed in `buys.recent` with `simulated: false`,
     `state: "booked"`, `window` (the hour, e.g. `2026-09-25T20:00:00Z`) and the exact `eth_in`. Nothing is bought yet;
   - `POST /pons_session` takes the runner's report of an executed buy (`window`, `eth_in`, `tx`, and the session's
     proof and counts when it has them). The engine reads the chain (read-only) and counts the buy only if the receipt
     has status 1, the transaction is from the buyback wallet to the pons router with value = the booked amount and
     calldata = a router buy of exactly that amount, a LABRAT `Transfer` reached the wallet, it was mined after the
     booking, and the hash was never counted before. Then the entry becomes `state: "executed"` with `tx`, `block` and
     the LABRAT received (from the chain), and `pons.label` "Bought · clicked by the rat on pons". Otherwise: 422 (does
     not verify), 404 (no booking for that hour), 409 (the hour already has another executed buy), 503 (no receipt on
     the engine's RPC yet: the runner reports again at its next poll);
   - the caps count verified buys plus the bookings still outstanding, so bookings can never add up past a cap. A booking
     the rig can no longer sign (one hour after its window ended) plus pons's 21-minute router deadline is released as
     `booking_expired` ("not executed in time"). A late execution that does verify is still counted: the chain is the
     truth;
   - a stop (e.g. the pins no longer match, or three failed simulations in a row) survives restarts; it is cleared with
     `BUYBACK_CLEAR_STOP=<its id>` (the log line "STOPPED: ... (stop id ...)" gives the id).
2. **The runner executes** (`live/buyrig_runner.py`, service `labrat-buyrig`). Only with `BUYRIG_LIVE=1`,
   `BUYRIG_CONFIRM=LABRAT`, `BUYBACK_RH_KEY` of the buyback wallet, and a mounted volume at `/data`. Then each booked buy
   runs ONCE (its state file marks it started first) while its hour can still be signed, as `buyrig.py --amount <eth_in>
   --live --window <hour>`. Every poll first resolves unfinished windows and reports every buy the rig's journal shows as
   mined and not yet reported (so a crash never loses a report). A simulated session, and the replay, never get the key.
   A DRY engine still gets simulated sessions.
3. **The rig signs** (`live/buyrig.py` + `live/buyrig_live.py`). The session re-checks every gate (and `eth_chainId`
   4663); if one is missing it runs exactly as DRY. Otherwise the page wallet is the buyback wallet's ADDRESS only, with
   its real balance (pons decides whether it is enough). After the rat's Confirm buy, pons's transaction must pass the 13
   DRY checks (the simulation now from the real wallet, no override) plus: from = the buyback wallet, value = the booked
   amount to the wei, min out 98 %..100 % of a fresh quote (pons's own 1 % slippage plus 1 % of price movement; a literal
   "99 % of a fresh quote" fails on pons's own rounding). Then it re-simulates, checks the balance (the buy + gas x maxFee
   x 1.2), journals `reserved` (plus an exclusive lock file for the hour), signs ONE EIP-1559 transaction (chain 4663,
   nonce = pending, gas = estimate x 1.25, maxFee = min(2 x gas price, 2 gwei), refused above 1 gwei), journals `signed`
   with the raw bytes, broadcasts, gives pons the hash, and reads the LABRAT received from the receipt. The journal is
   `/data/buyrig/live_journal.jsonl`; `python live/buyrig_live.py --status` shows it (no key needed).
   - one transaction per hour, ever: a reserved or signed hour (journal line or lock file) never signs again;
   - an unfinished hour is finished by its receipt or by re-broadcasting the IDENTICAL raw bytes, never a new
     transaction. A signed buy that never reached the chain and whose router deadline passed can buy nothing any more: it
     is marked expired (a failed buy) and its nonce goes to the next hour's transaction;
   - the wallet's nonce must match the journal (0 + one per mined transaction): a transaction sent from the wallet by
     hand, or a lost journal, refuses LIVE until `BUYRIG_ANCHOR_NONCE=<the wallet's nonce now>`;
   - LIVE stops itself (journalled, with an id) on any check failure, or after 2 failed buys in a row (a booked hour that
     did not end in a mined buy: the rat never got to Confirm, the balance was short, it reverted, it never landed). A
     mined buy resets the count. `BUYRIG_CLEAR_STOP=<id>` clears it after an operator checked;
   - the rig's own caps: 0.1 ETH per buy, 2.4 ETH signed per rolling day, 5 ETH ever;
   - the key is read once, kept in the signer object only, removed from the environment before Chromium starts, and
     redacted from every log line: it is never in a log, the stream, the relay, the status, a journal, an error or the
     page. The stream says "Live" (not "Simulated") only for such a session, and never carries the transaction hash.
4. **The site** (`site/buyback/index.html`, `site/js/site.js`) takes the banner from the engine's mode: DRY keeps
   "Buybacks start soon"; LIVE shows "Buybacks live". An executed buy (verified, with its hash) is shown as a buy with
   "clicked by the rat on pons" and a "View transaction" link to `https://robinhoodchain.blockscout.com/tx/<hash>`. A
   booked or expired one is shown as "booked, not executed yet" / "not executed", never as bought; DRY's stay
   "Simulated".

## Going live (real buys)

Two Railway services: **`labrat-buyrig`** (the runner and the rig; `buyrig/Dockerfile`, a volume at `/data`) and
**`labrat-buyback`** (the engine; `~/claude/_deploy/labrat-buyback`, a volume at `/data`). Do the steps in this order: the
rig is switched on first (with a DRY engine it has nothing real to execute), the engine second (from then on each hour
books a real buy).

1. **Deploy the new code, switches still off.**
   - `labrat-buyback`: copy `live/buyback.py` into `_deploy/labrat-buyback/live/`, redeploy. The status still says
     `mode: "DRY"`.
   - `labrat-buyrig`: copy `live/buyback.py`, `live/buyrig.py`, `live/buyrig_live.py` (new), `live/buyrig_runner.py`,
     `buyrig/Dockerfile` and `buyrig/Dockerfile.dockerignore` into `~/claude/_deploy/labrat-buyrig/` (same paths; the
     Dockerfile now copies `live/buyrig_live.py` and fails the build without it), redeploy. The runner log says
     `LIVE: off (BUYRIG_LIVE is not 1); simulated sessions only`, and sessions stay "Simulated".
2. **Fund the buyback wallet** `0x17852f35b597554732C706A8A9FAA534C10e1E23` by hand, from another wallet (receiving ETH
   does not change its nonce; do not send anything FROM it). At the owner's budget a perfect day is 2.4 ETH plus about
   0.0001 ETH of gas per buy. `python live/buyback.py --check` (read-only) prints `buyback_wallet_eth`.
3. **`labrat-buyrig` → Variables**, all three, then redeploy:
   - `BUYBACK_RH_KEY` = the buyback wallet's private key (a sealed variable)
   - `BUYRIG_CONFIRM` = `LABRAT`
   - `BUYRIG_LIVE` = `1`

   Check `BUYBACK_RIG_TOKEN`, `LABRAT_PUBLISH_TOKEN` and `BUYRIG_RELAY` are still set, and that a volume is mounted at
   `/data`. The runner log must say `LIVE: on (booked real buys are executed; every session re-checks every gate)`. If it
   says `LIVE: off (...)`, the reason is in the brackets. With the engine still DRY it keeps running simulated sessions.
4. **`labrat-buyback` → Variables**: `BUYBACK_LIVE_BOOKINGS` = `1`, and `BUYBACK_RIG_TOKEN` = the same value as on
   `labrat-buyrig` (24+ characters). **Procfile**: add `--live-bookings`:
   ```
   web: python live/buyback.py --status-host 0.0.0.0 --status-port $PORT --journal-dir /data/buyback --hourly-budget-eth 0.1 --max-buy-eth 0.1 --max-hour-eth 0.1 --max-day-eth 2.4 --max-total-eth 5 --live-bookings
   ```
   Redeploy. `GET /status` says `mode: "LIVE"`, and lab-rat.net/buyback shows "Buybacks live". The next hour's close
   books the first real buy (`buys.recent[0].state: "booked"`).
5. **Watch the first buy.** Within a minute or two of the hour: the runner logs `LIVE batch live|<hour>|<eth>: bought
   (... LABRAT, 0x...)` and `LIVE: reported the buy ... engine 200`; the engine logs `LIVE: executed buy for the hour ...
   verified on chain`; the site shows it with "View transaction". `python live/buyrig_live.py --status` in the rig's
   container (`railway ssh`) shows the journal.

**How to stop.**
- **Stop signing now:** `labrat-buyrig` → set `BUYRIG_LIVE` = `0` (or delete `BUYBACK_RH_KEY`) → redeploy. The rig
  signs nothing more; a booked buy is then not executed and expires. A transaction already signed cannot be unsigned:
  the next LIVE start (or `python live/buyrig_live.py --resolve` with the LIVE variables) finishes it from its journalled
  bytes, and it is reported then.
- **Back to DRY:** then `labrat-buyback` → delete `BUYBACK_LIVE_BOOKINGS` (or remove `--live-bookings`) → redeploy. The
  status says DRY again and the site "Buybacks start soon"; the bookings journal stays on the volume, and switching back
  on later resumes it (its caps included).
- **LIVE stopped itself** (the runner log says `LIVE STOPPED (...) ... stop id <id>`; `--status` shows why): after
  checking, set `BUYRIG_CLEAR_STOP` = `<id>` on `labrat-buyrig` and redeploy. A later stop has a new id, so the old value
  clears nothing. The engine's own stop: `BUYBACK_CLEAR_STOP` = `<id>` on `labrat-buyback`.
- **A transaction was sent from the wallet by hand** (e.g. a withdrawal): LIVE refuses ("the buyback wallet is at nonce
  N but the LIVE journal accounts for M"). After checking, set `BUYRIG_ANCHOR_NONCE` = `N` on `labrat-buyrig` and
  redeploy.

## The fee-funded LIVE path: paused

The LIVE path signs from the launch wallet and pays from its claimed creator fees. The buys are now paid from the
hand-funded buyback wallet, and the real buys go through live bookings and the buy rig instead (above). So this path is
refused right after `--live --confirm LABRAT`, before `.env` is read (`LIVE_BUYER_READY = False`). It would also need a
set `--hourly-budget-eth` (LIVE never runs a preview) and real hour windows (no `--window-s`).

Behind that pause, the earlier gates are unchanged:

- the public relay and origin only, and no `--accept-test-streams`, `--journal-dir` or `RATBRAIN_RPC`
- the `.env` file says `BUYBACK_LIVE=1` and its key is the pinned launch wallet's
- chain id 4663, and the pinned checks pass
- the single-instance lock, a readable journal, a cleared stop
- the wallet's nonce matches the journal

The LIVE executor is also unchanged: a simulation right before signing, value = amountIn, a write-ahead journal, one
booking record per receipt, same-bytes rebroadcast, and a drop only when every RPC agrees twice. The tests reach it
only with a `MockSigner` and a fake chain.

## Tests and the real read-only runs

`python live/buyback_test.py` runs 52 mocked tests. They cover:

- the counting: steering hits and misses; Rat Tiles hits, missed tiles (from `missed` or from tile events, each tile
  once, and a song joined midway kept out of the rate) and wrong clicks
- de-dup over a real websocket and across restarts
- the hourly rule: budget x hit rate, rounding, the minimum, no attempts or no hits, a restart mid-hour, an hour closed
  late, a buy that expires when the next hour closes first, and a journal from before the rule
- previews (marked, per-buy cap only, never using up a cap)
- the buyback wallet as the buyer, with no claim
- every cap, gas price, gas share and gas cap
- the public status fields and their safety (no address)
- the budget flags and the environment variable
- the pin and graduation stops
- `main()` end to end, every LIVE gate (the pause first), and the LIVE executor on the fake chain
- live bookings: the switch (the flag AND the variable; every refused configuration), `main()` in live bookings, an
  hour booked and not bought, a correct execution verified and counted (idempotent, replayed after a restart), every
  wrong one refused (no receipt yet, from, to, value, reverted, another amount, before the booking, a hash counted for
  another hour, bad fields), the caps on verified buys plus outstanding bookings, expiry and a late execution, a stop
  cleared only by its id, and DRY unchanged

`python live/buyrig_test.py` runs 50 tests (the 30 before, plus 20 for LIVE, all mocked: every gate falling back to DRY
and the key leaving the environment, the page wallet as an address only, one transaction per hour across restarts and
two processes, 'reserved' before signing and the raw bytes before the broadcast, a crash between signing and
broadcasting (the identical bytes go out on the next start), a signed buy that never landed (expired, nonce reused), a
nonce used elsewhere (stop, abandon, anchor), tampered from / to / value / token / amount / min out / deadline (refused,
nothing signed, LIVE stopped), a short balance and the two-failures stop, the runner's LIVE batches verified by a real
engine over HTTP (once, retried on 503, recovered after a crash), the real EIP-1559 format, and the key in no output).
The signer in these tests is a `MockSigner` (an address, no key), except two that sign with a key made in the test
itself, never funded, against the in-memory fake chain. `.env`, the real chain and any send stay trapped throughout.

**2026-09-25, real and read-only:**

- `--check`: the pool buy simulated from the buyback wallet (balance 0, raised by the override) at block 72278229:
  0.0001 ETH → 794.4 LABRAT, 161,098 gas (5.8 %), quote checked.
- A 150 s DRY run against the public relay and the real chain, with 60 s windows standing in for the hour (`--window-s`
  is for tests only). It counted live Rat Tiles attempts (35, 62, 42 and 32 hits), closed two windows at a 100 % hit
  rate, and simulated two preview buys from the buyback wallet: 0.001 ETH → 7,930.63 and 8,173.5 LABRAT, about 186,000
  gas (0.7 %) each. No claim was simulated, and nothing was sent.
- The buyback wallet (read on 2026-09-25): nonce 0 (latest and pending), balance 0. `buyrig_live.FIRST_NONCE` is 0.

`python live/buyback_test.py --real-run` still exists: a local relay replays recorded attempts, with 30 s windows.

## Deploy (Railway, `~/claude/_deploy/labrat-buyback`)

Copy `live/buyback.py` into `_deploy/labrat-buyback/live/`. `launcher.py` is unchanged. The Procfile (DRY, the owner's
budget):

```
web: python live/buyback.py --status-host 0.0.0.0 --status-port $PORT --journal-dir /data/buyback --hourly-budget-eth 0.1 --max-buy-eth 0.1 --max-hour-eth 0.1 --max-day-eth 2.4 --max-total-eth 5
```

Alternatively, drop `--hourly-budget-eth` and set the Railway variable `BUYBACK_HOURLY_BUDGET_ETH`; the flag wins when
both are given. The existing journal on `/data/buyback` replays as described above. For real buys see "Going live".

## Not settled

- The Rat Tiles publisher does not yet send `missed`. Until it does, the missed tiles come from its tile events, which
  undercount only when the relay drops an event.
- The ABIs come from bytecode selectors, signature hashes, real transactions and simulations, not verified source (see
  `BUYBACK_RESEARCH.md`).
- In DRY the caps apply per journal, and deleting it resets them.
- The single-instance lock is per machine; run one engine only.
- Live bookings have no single-instance lock: run one engine (Railway runs one instance with a volume). A second rig
  process can never sign a second transaction for an hour (the exclusive lock file), and the engine counts one
  executed buy per hour.
- What pons shows after a real, settled buy (its toast) has not been seen yet: the rig records it, the receipt decides.
- What pons builds when the 0x route quotes better than its pool was never observed; the rig refuses anything that is
  not the pinned router buy (a check failure, which stops LIVE).

## Run it

```
python live/buyback.py                                # DRY, preview buys (budget unset)
python live/buyback.py --status-port 4750             # + the public status on http://127.0.0.1:4750/status
python live/buyback.py --hourly-budget-eth 0.001      # DRY with an hourly budget
python live/buyback.py --check                        # read-only: pins, venue, a simulated buy from the buyback wallet
python live/buyback_test.py                           # the mocked tests
```

`--check` prints the launch wallet's claimable fee amount locally. Keep that amount off the site.
