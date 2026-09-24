# Rat buybacks ($LABRAT, option 1)

**Every target the rat hits in the live view adds a tiny amount to a buyback of $LABRAT, paid only from the coin's
claimed creator fees, until the hourly, daily or total caps are reached.** Hits over the caps are counted but add
nothing. The live view is the newest saved training checkpoint, playing in its own simulation
(`live/publish_training.py`); the training workers themselves are not watched. The code is `live/buyback.py`, the tests
are in `live/buyback_test.py`, and the chain research behind it is in `live/BUYBACK_RESEARCH.md`.

The rat's brain is two trained artificial neural networks. It is not a biological brain, and it does not understand
money. This code sets the rules, and the rat's hits in the live view only trigger them.

**Status: built, DRY only.** The owner said "just dont make the rat buy the coin yet just build". Nothing has been
claimed, bought, signed or sent. DRY is the default and the only mode that has ever run. LIVE is implemented and gated,
and it has not been run.

## The one deviation from the spec: where it buys

The spec said to buy "on its pons curve" and to "stop if the curve graduated". But $LABRAT filled its 4.2 ETH curve and
graduated at block 71688341, about 7 minutes after launch. A curve buy now reverts with `CurveGraduated()`, so an
engine that only buys on the curve would stop at once and could never do option 1.

- **`--venue auto` (the default)** buys on the curve while the coin is on it (phase 0, `graduated()` false). After
  graduation (phase 2, `graduated()` true) it buys in the coin's **pinned** Uniswap v4 pool through the pons Universal
  Router. Anything else stops the buys. Today every buy goes to the pool.
- **`--venue curve`** keeps the literal rule: the curve has graduated, so it stops at once.
- **`--venue pool`** buys only in the pool.

The pool is pinned just as tightly as the curve. The PoolKey (ETH, LABRAT, fee 0, tick spacing 200, the pons hook) has
to hash to the pinned poolId. The factory's `memeHook()` has to be the pinned hook. The record's fee and tick spacing
have to match.

## What counts as a hit

- **The source is the relay's public `/live` stream**, read with Origin `https://lab-rat.net`. It carries the
  `{"type":"episode","n","presses","hits","misses","fell"}` message that `live/publish_training.py` sends at the end of
  each attempt.
- **A hit is that message's `hits` field.** For the cursor and steering tasks it is a click while the cursor was on the
  lit target (`cursor_env.CursorEnv`). The lever task has no target (its "hit" is a clean press, at most one per
  attempt), so **by default only the cursor and steering tasks count**. `--tasks lever,cursor,steer` opts the lever task
  in, and the public rule then says "in the lever task, each clean press".
- **Only a live training session counts.** The hello must have `source` set to `"training"` and must not be a TEST
  stream; `--accept-test-streams` lifts that for DRY tests only, and LIVE refuses the flag. The last episode in the
  relay's `state` message counts only while `state.live` is true. The relay keeps the last episode of a run that has
  ended, and when this program started, that stale episode was not counted.
- **Nothing is counted twice.** Each attempt is keyed by `run | task | hello.started | n`. A reconnect, a `state`
  that repeats the last episode, the publisher resending its hello, or a restart of this program (the keys come back
  from the journal) cannot count an attempt a second time. Tested: in memory, over a real websocket with a relay drop,
  and across a restart.
- **Bad messages are rejected and journalled.** That covers more than 4 hits in one attempt (1 for the lever task),
  `presses != hits + misses`, and anything that is not a non-negative integer.
- **Frames never move money.** The binary frames (a click flag plus the cursor and the lit target) only feed an
  unconfirmed tally of the attempt in progress. The publisher and the relay both drop frames under load. In the
  recorded steering stream used below, 9 attempts had 34 presses but only 33 click frames.
- **Undercounting is possible, overcounting is not.** Attempts that end while this program is offline are missed.
  When `n` jumps, the gap is journalled.

## Money: batching, fees, caps

Each hit adds `per_hit` to *pending*. A buy is due when pending reaches `batch_trigger`, or when `batch_interval` has
passed and pending is at least `min_buy`. The buy is pending cut down by every cap, and it never goes below `min_buy`.

| setting | default | hard ceiling (in code) |
|---|---|---|
| per hit | 0.00001 ETH | 0.0001 |
| min buy (never smaller: gas) | 0.0001 ETH (10 hits) | |
| batch trigger (buy at once) | 0.0005 ETH (50 hits) | |
| batch interval | 10 min | |
| max per buy | 0.001 ETH | 0.01 |
| max per rolling hour / day | 0.002 / 0.01 ETH | 0.02 / 0.05 |
| max total (per journal) | 0.05 ETH | 0.25 |
| max pending (later hits add nothing, journalled as capped) | 0.01 ETH | 0.05 |
| gas reserve kept back from the claimed fees | 0.0002 ETH | |
| claim only when claimable is at least / claim at most | 0.001 / 0.02 ETH | claim max 0.1 |
| slippage (min out = simulated out minus this) | 3 % | 10 % |
| router deadline | 180 s | |
| skip while gas price is above | 1 gwei (it is about 0.044) | 10 gwei |
| skip a buy whose gas is more than | 20 % of it | 50 % |
| gas of claims, buys and reverted transactions per rolling day / total | 0.002 / 0.01 ETH | 0.01 / 0.05 |
| stop after this many failed claims or buys in a row | 3 (LIVE: survives restarts) | |

- **Hits over the caps add nothing, and the status says so.** Pending stops growing at `max_pending`; the hourly and
  daily caps let it drain only slowly. At the one recorded rate (about 2 hits per 15 s attempt), 24 hours of hits is
  about 11,500, of which about 1,000 fit the daily cap. The public status splits "hits counted" into hits in buys,
  pending and **over the caps (counted, added nothing)**, and once the total cap is reached it says "the total cap is
  reached: no more buybacks".
- **"Next buy" follows the same rules** as the batch itself: a cap, a gas cap, a skip (gas price, waiting for creator
  fees, a failed attempt) or the total cap shows as "no buy for now: hourly cap reached" (or the wait and its reason),
  never as "due now".

- **Fees first.** When the claimed but unspent fees cannot pay a buy plus its worst-case gas plus the reserve, the
  engine reads the claimable creator fees (the pons FeeEscrow, in ETH). If they are at least `claim_min`, it claims
  up to `claim_max` *before* the buy. Tested with both the journal order and the order of the chain calls.
- **Gas counts as spending.** The gas for claims and buys is booked against the claimed fees, so the buys only ever
  spend fees. Every test checks `spent ≤ claimed − reserve`.
- **DRY claims are virtual.** The escrow still holds them, so in DRY "claimable" is the escrow balance minus what DRY
  has already claimed.
- **The pinned checks run before every batch.** `token.curve()` and the factory record must match: the token, the
  curve, the creator-fee recipient (the launch wallet), the ETH pair, `exists`, and pons's own buyback switch still
  off. Then the phase and `graduated()` pick the venue, and any mismatch stops the buys. Hits are still counted, and
  the public status says "the chain no longer matches the pinned coin".

## DRY (the default)

- **It simulates, from the launch wallet on the real chain:**
  - the claim: `claim(uint256)` with `eth_call`, plus `eth_estimateGas`
  - the quote: the pons v4 quoter's `quoteExactInputSingle`
  - the exact router `execute(0x10, [0x06 0x0c 0x0f], deadline)` with min out = quote − 3 %
  - the same call with min out = the full quote, which proves that the quote is what the router would deliver
  - `eth_estimateGas` for the buy
- **The wallet is simulated as if the fees had been claimed.** A state override sets its balance to its real ETH plus
  the claimed, unspent fees.
- **It never reads `.env`, never builds a signer, and can only read.** Its RPC object is a `ReadRpc`: every method that
  is not a read, including `eth_sendRawTransaction` and `eth_sendTransaction`, raises `SendRefused`.
- **It keeps an append-only journal**, `runs/buyback/journal.jsonl`, one fsync'd line per record: `start`, `check`,
  `hit`, `gap`, `rejected`, `claim`, `claim_skip`, `buy`, `buy_skip`, `*_failed`, `stop`, `end`. Claim and buy
  records keep each `eth_call`'s target, selector, value and calldata sha256. A restart rebuilds the whole state from
  this file. The `runs/` folder is not committed.

## The public status and the site panel

- **The status JSON** goes to `runs/buyback/status.json` and, with `--status-port`, to `http://<host>:<port>/status`
  (CORS `*`, no-store). It holds the mode and the label `DRY - simulated, not executed`, the rule text, the counted
  tasks, hits counted (split into in buys, pending and over the caps), pending (in ETH and hits), the unconfirmed tally
  of the attempt in progress, whether the relay is live and being counted, the source (`public_relay`,
  `accept_test_streams`, `test`), the simulated buys (ETH in, LABRAT out, pool or curve, the hits each covered), the
  next buy (seconds and a short fixed reason), the caps, a stopped reason and `updated`.
- **status.json stays fresh.** It is rewritten on every change and at least every 10 s. A failed write (on Windows a
  reader can hold the file) is retried on the next loop instead of being dropped.
- **It never contains** a wallet address, any creator-fee amount (claimable, claimed or budget), or transaction data.
  A test checks that no `0x…40` string appears in it.
- **The site panel** is `site/index.html` `#bb-wrap` plus `site/js/site.js`, and it sits under the live panel. It is
  hidden by default: `site/buyback.js` holds `window.LABRAT_BUYBACK = {enabled:false, statusUrl:''}`.
- **To show it**, set `enabled: true` and an https `statusUrl` (http is allowed only for localhost).
- **What it shows:** everything except the mode `LIVE` gets the yellow `DRY - simulated, not executed` badge, and a
  simulated buy is always worded "simulated buy". A TEST stream, `--accept-test-streams`, a relay other than the public
  one (or a status that does not say) gets a second badge, `TEST stream - not live training`, and is never called
  "counting live hits". A status whose `updated` is more than 90 s old shows "status stale". Hits over the caps are
  shown as "counted, added nothing". It refuses to render any string that looks like an address.
- **Checked** at 1280 px and 375 px in a local preview: 16 px gutter, no horizontal scroll.
- **Before it can go public**, the engine needs a public host for `statusUrl`. The relay is public, so the engine can
  run anywhere, for example next to the relay on Railway.

## LIVE (implemented, not used)

Every one of these is needed, in this order. Any failure exits with "LIVE refused, nothing was signed or sent":

1. `--live --confirm LABRAT`
2. no `--accept-test-streams`, and the public relay and origin only (no other `--relay` or `--origin`)
3. no `--journal-dir`: LIVE's journal, caps and lock have one fixed place, `runs/buyback/`, so they cannot be reset by
   pointing the engine at a fresh folder
4. no `RATBRAIN_RPC` in the environment: LIVE reads, simulates and sends only through the two pinned public RPCs
   (a stray local fork keeps chain id 4663 and would otherwise be the only source of receipts)
5. the `.env` **file**, read with `launcher.read_env_file` (the launcher's reading), says `BUYBACK_LIVE=1` and holds
   `BUYBACK_RH_KEY` (or `RATBRAIN_RH_KEY`). The shell environment must not disagree with it.
6. the key's address is the pinned launch wallet. Any other key is refused.
7. chain id 4663, and the pinned chain checks pass
8. no other LIVE engine is running on this machine: `runs/buyback/live.lock` is created exclusively and removed on exit
9. `runs/buyback/journal_live.jsonl` has no unreadable line (a damaged `signed` line could hide a real transaction)
10. the last LIVE stop is cleared: a stop (3 failed claims or buys in a row, a chain mismatch, a nonce mismatch, a
    journal that could not be written) is journalled and survives restarts until an operator starts with `--clear-stop`
11. **the launch wallet's nonce on chain is exactly what the journal accounts for**: 1 (its nonce after the launch
    transaction) plus one per journalled transaction. A lost or foreign journal, a second engine on another machine,
    or a transaction sent from the wallet outside this engine is refused. If the wallet sent transactions elsewhere
    before the FIRST LIVE run, `--first-nonce <its nonce>` records that once (refused as soon as the journal has a
    transaction). Deleting the journal after LIVE transactions therefore cannot reset the caps.

Then, for each claim and each buy:

- **It is simulated again on the real state just before signing**, with no overrides, a fresh quote, min out = quote
  − slippage and deadline = the chain's time + 180 s. The caps, the pending amount, the claimed fees minus the
  reserve, the wallet balance, the gas share and the gas caps are all checked again.
- **Its value is exactly what it spends.** The pons router keeps any ETH above the swap's amountIn (no refund), so the
  value must equal amountIn and the SETTLE_ALL amount (`check_value`, before simulating and before signing).
- **It is signed at the wallet's pending nonce**, only when no other transaction from the wallet is pending and only
  when that nonce is the one the journal expects (otherwise LIVE stops for an operator).
- **It is journalled before broadcast**, with its raw bytes, then followed to its receipt. The receipt is booked in
  **one** record (the claim, buy or gas record carries the transaction hash and resolves it). There is no separate
  "mined" line, so a crash between the receipt and the booking leaves the transaction unresolved, and the restart books
  it from its receipt: never lost, never bought twice. Tested by failing the journal write right after a mined buy.
- **An unconfirmed transaction blocks new ones.** If the chain forgot it, the same signed bytes are broadcast again (the
  buy's deadline and min out still apply). It is marked dropped only when **every** pinned RPC, asked separately, has no
  receipt for it, does not know it, and says its nonce was used, twice at least 120 s apart. A node that cannot be
  reached or that still has the receipt keeps it unresolved (or books it).

The tests reach the LIVE code only with a `MockSigner`, which has the launch wallet's address and no key, and a fake
chain. No test ever holds a real key or reads `.env`.

## Tests and the real read-only run

`python live/buyback_test.py` runs 34 mocked tests. They check:

- that the calldata reproduces the research's calldata byte for byte, and that a transaction's value must equal what it
  spends (the router keeps any excess)
- the counting rules, validation and gaps; frames count only toward the unconfirmed tally; the lever task is off by
  default
- that nothing is counted twice: over a real websocket with a relay drop and the Origin header checked, and across a
  restart; the reconnect backoff against a relay that keeps closing at once
- batching; every cap; the claim threshold and claim max; the gas reserve, gas price and gas share; the daily and total
  gas caps
- the graduation and pin stops (curve-only, phase 1, a graduation mismatch, token.curve, the record curve, the fee
  recipient, the hook, the tick spacing, the buyback switch, the pair)
- claim before buy, and that the public status is safe to publish: capped hits shown as adding nothing, "next buy"
  following the caps and skips (never "due now" while blocked), the total cap, test streams and other relays marked,
  and status.json retried after a failed write and rewritten every 10 s
- DRY end to end through `main()` with the status served over HTTP
- every LIVE gate, including `--journal-dir`, `RATBRAIN_RPC`, the nonce check against the chain, `--first-nonce`, a
  replayed stop and `--clear-stop`, and an unreadable journal line
- the LIVE executor: write-ahead journal, one booking record per receipt (a journal write failing right after a mined
  buy: the restart books it once and buys nothing again), nonces, receipts, unconfirmed → same-bytes rebroadcast →
  dropped only when every RPC agrees twice, a lagging RPC that must not drop a mined claim, an unreachable RPC, a
  nonce mismatch stop, a stop that survives a restart, a reverted buy's gas booked, and excess value never signed

The traps stay on for the whole run: opening `.env`, the launcher's `.env` readers and real signing all raise; the
network is loopback only; `launcher.rpc` is a tripwire.

`python live/buyback_test.py --real-run` (or `--only-real`) is read-only:

- **A. The real relay and chain, 60 s.** Re-run on 2026-09-24 at 22:53 UTC, after the review fixes: the relay had no
  live training (its state held `railway_check`, not live). No hits were counted, and the stale last episode was
  ignored. The chain check at block 71743001 gave phase 2, graduated, venue pool.
- **B. The relay was idle, so a local relay on `127.0.0.1:4751`** (a fresh journal each run, in
  `runs/buyback_demo/run_<time>/`). It played 9 real recorded attempts (`publish_training.py --dry-print` of
  `runs/steer_v1`, TEST-labelled, counted only because of `--accept-test-streams`). It dropped the connection after
  attempt 3 and resent attempts 2 and 3. The engine counted 19 of the recording's 19 hits and journalled 2 duplicates.
  On the real chain (about 0.044 gwei) it recorded:
  - a simulated claim of 0.02 ETH: `claim(uint256)` from the launch wallet succeeded, 42,496 gas
  - a simulated buy of 0.00012 ETH (12 hits) → 1,345.25 LABRAT (min out 1,304.90), 161,094 gas = 5.8 % of the buy
  - a simulated buy of 0.00007 ETH (7 hits) → 766.37 LABRAT, 10.2 % gas

  Its status.json said `source.test: true` (a test stream, so the site would show the TEST badge) and split the 19 hits
  into 19 in simulated buys, 0 pending, 0 over the caps. The RPC methods used were `eth_call`, `eth_estimateGas`,
  `eth_gasPrice`, `eth_getBalance` and `eth_getBlockByNumber`: no send. `.env` was never opened. The journals are in
  `runs/buyback/` and `runs/buyback_demo/`.
- **The site panel** was checked in a local preview (a scratch copy of `site/` with the panel switched on) at 1280 px
  and 375 px: a fresh status with capped hits ("1,500 over the caps (counted, added nothing)", "No simulated buy for
  now: hourly cap reached") and the stale test-stream status above ("status stale", the TEST badge). No horizontal
  scroll; 16 px gutter.

## Not settled

- The ABIs come from bytecode selectors, signature hashes, real transactions and simulations. They are not verified
  source (blockscout was behind a Cloudflare check). See `BUYBACK_RESEARCH.md`.
- The router struct's extra `uint256` is sent as 0, as real pons buys send it.
- Hits per minute in real training have not been measured. The one recorded steering run gave about 2 hits per
  15 s attempt.
- pons's own buyback switch (`setBuybackEnabled`) is untouched. The engine stops if it is ever switched on, because the
  fee flow would change.
- In DRY the caps apply per journal file, and deleting it resets them. LIVE's journal has one fixed place, and the nonce
  check refuses a LIVE start whose journal does not account for the wallet's transactions.
- The single-instance lock is per machine. A second LIVE engine elsewhere is caught by the nonce check (at start and
  before every signature), not by the lock. Two engines started in the same second on fresh journals could each get one
  transaction in before one of them stops, so run LIVE in one place only.

## Run it

```
python live/buyback.py                          # DRY: listen to lab-rat.net's relay, count, simulate, journal
python live/buyback.py --status-port 4750       # + the public status on http://127.0.0.1:4750/status
python live/buyback.py --check                  # read-only: pins, venue, claimable, a simulated claim and 0.0001 ETH buy
python live/buyback_test.py                     # the mocked tests
python live/buyback_test.py --only-real         # the real read-only run (A: real relay, B: local relay on 4751)
```

`--check` prints the claimable fee amount locally. Keep that amount off the site.
