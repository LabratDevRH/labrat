# Rat buybacks ($LABRAT): one buy an hour, sized by the hit rate

**Once an hour, on the hour (UTC), the engine closes the hour the rat just played in the live view, counts its hits,
misses and wrong clicks, and books that hour's $LABRAT buy: the hourly budget x the hit rate, cut to the caps.**

    hit rate = hits / (hits + misses + wrong clicks)
    the hour's buy = hourly budget x hit rate   (rounded down to 0.00000001 ETH, cut to the per-buy, hourly, daily
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

**Status: built, DRY only.** Nothing has been claimed, bought, signed or sent. DRY is the default and the only mode
that has ever run. LIVE is paused (see below).

## Who pays: the buyback wallet

The owner funds a separate buyback wallet by hand: `0x17852f35b597554732C706A8A9FAA534C10e1E23`, pinned in the code as
`BUYBACK_WALLET`. Every DRY buy is simulated from it. The engine holds no key for it (or for any wallet) and has no key
handling for it.

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
- **The rounding** is down to 8 decimals (0.00000001 ETH), which is what the buy rig types into pons.
- **Previews** (budget not set) are cut to the per-buy cap only. They never use up the hourly, daily or total cap or
  the gas caps, so the caps are intact when the budget is set.
- **A journal from before the hourly rule** replays. Its hits and buys still count, and its pending per-hit amount is
  dropped: those hits show as "counted, bought nothing".

| setting | default | hard ceiling (in code) |
|---|---|---|
| hourly budget (`--hourly-budget-eth`, or the process env `BUYBACK_HOURLY_BUDGET_ETH`) | unset = preview | 0.01 |
| preview amount while unset (`--preview-budget-eth`) | 0.001 ETH x hit rate | 0.01 |
| min buy (an hour under it buys nothing) | 0.0001 ETH | |
| max per buy (the budget is cut to it) | 0.001 ETH | 0.01 |
| max per hour window / per rolling day | 0.002 / 0.01 ETH | 0.02 / 0.05 |
| max total (per journal) | 0.05 ETH | 0.25 |
| slippage (min out = simulated out minus this) | 3 % | 10 % |
| router deadline | 180 s | |
| skip while gas price is above | 1 gwei (it is about 0.036) | 10 gwei |
| no buy for an hour whose gas is more than | 20 % of it | 50 % |
| gas per rolling day / total (previews left out) | 0.002 / 0.01 ETH | 0.01 / 0.05 |
| stop after this many failed attempts in a row | 3 | |

**Setting a budget above 0.001 ETH also needs `--max-buy-eth`.** Otherwise the per-buy cap cuts every hour's buy to
0.001 ETH, and the engine logs that at startup. `max_buy` may not exceed the hourly, daily or total cap, so a budget
of 0.005 needs, for example, `--max-buy-eth 0.005 --max-hour-eth 0.005 --max-day-eth 0.05 --max-total-eth 0.25`.

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
`POST /pons_session`. That endpoint opens only in DRY with `BUYBACK_RIG_TOKEN` of 24+ characters in the process
environment. The report is validated field by field and shown on that buy, and on `last_window.buy`, as "Simulated buy
· clicked by the rat on pons". It never changes a cap, the booked amount or any figure the engine computed.
`live/buyrig_test.py` covers it.

## LIVE: paused

The LIVE path signs from the launch wallet and pays from its claimed creator fees. The buys are now paid from the
hand-funded buyback wallet, and no LIVE path exists for that wallet (no key handling). So LIVE is refused right after
`--live --confirm LABRAT`, before `.env` is read (`LIVE_BUYER_READY = False`). It would also need a set
`--hourly-budget-eth` (LIVE never runs a preview) and real hour windows (no `--window-s`).

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

`python live/buyback_test.py` runs 42 mocked tests. They cover:

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

`live/buyrig_test.py` (30 tests) still passes against the hourly engine. `.env`, signing and the network stay trapped
throughout.

**2026-09-25, real and read-only:**

- `--check`: the pool buy simulated from the buyback wallet (balance 0, raised by the override) at block 72278229:
  0.0001 ETH → 794.4 LABRAT, 161,098 gas (5.8 %), quote checked.
- A 150 s DRY run against the public relay and the real chain, with 60 s windows standing in for the hour (`--window-s`
  is for tests only). It counted live Rat Tiles attempts (35, 62, 42 and 32 hits), closed two windows at a 100 % hit
  rate, and simulated two preview buys from the buyback wallet: 0.001 ETH → 7,930.63 and 8,173.5 LABRAT, about 186,000
  gas (0.7 %) each. No claim was simulated, and nothing was sent.

`python live/buyback_test.py --real-run` still exists: a local relay replays recorded attempts, with 30 s windows.

## Deploy (Railway, `~/claude/_deploy/labrat-buyback`)

Copy `live/buyback.py` into `_deploy/labrat-buyback/live/`. `launcher.py` is unchanged. The Procfile:

```
web: python live/buyback.py --status-host 0.0.0.0 --status-port $PORT --journal-dir /data/buyback --hourly-budget-eth unset
```

To set the budget later, replace `unset` with the amount, for example `0.002`, and add `--max-buy-eth 0.002` for
anything above 0.001. Alternatively, drop the flag and set the Railway variable `BUYBACK_HOURLY_BUDGET_ETH`; the flag
wins when both are given. The existing journal on `/data/buyback` replays as described above.

## Not settled

- The Rat Tiles publisher does not yet send `missed`. Until it does, the missed tiles come from its tile events, which
  undercount only when the relay drops an event.
- The ABIs come from bytecode selectors, signature hashes, real transactions and simulations, not verified source (see
  `BUYBACK_RESEARCH.md`).
- In DRY the caps apply per journal, and deleting it resets them.
- The single-instance lock is per machine; run one engine only.

## Run it

```
python live/buyback.py                                # DRY, preview buys (budget unset)
python live/buyback.py --status-port 4750             # + the public status on http://127.0.0.1:4750/status
python live/buyback.py --hourly-budget-eth 0.001      # DRY with an hourly budget
python live/buyback.py --check                        # read-only: pins, venue, a simulated buy from the buyback wallet
python live/buyback_test.py                           # the mocked tests
```

`--check` prints the launch wallet's claimable fee amount locally. Keep that amount off the site.
